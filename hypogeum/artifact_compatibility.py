"""
hypogeum/artifact_compatibility.py — Artifact-Compatibility Reuse Policy

Permanent factory policy (2026-08-01): artifact-aware incremental
production. Applies to every channel and every episode. Supersedes any
age-based ("older than N days/commits -> quarantine everything") approach.

This module answers two related questions, never by artifact age alone:

  1. Subject level (evaluate_subject_production_decision): given a topic,
     has its canonical subject already been published? If so, production
     of a duplicate is blocked unless explicitly authorized. Otherwise,
     production proceeds — reusing this module's canonical_subject() (a
     thin public wrapper around the existing, already-relied-upon
     hypogeum.topic_discovery._make_canonical_subject(), not a
     reimplementation) so a "new vs. existing subject" decision is
     consistent with the discovery layer's own dedup identity.

  2. Artifact level (evaluate_video_clip_compatibility,
     evaluate_render_compatibility): given one already-generated artifact,
     is it still valid to reuse? Evaluated on real, inspectable dimensions
     — file presence/size, real vs. dry-run pixel content, structural
     validity (ffprobe), and the producing code's policy version (the
     agent's own _AGENT_VERSION, already written into every package it
     emits) — never on the artifact's file timestamp.

Decision outcomes (CompatibilityDecision):
  VALID_AND_COMPATIBLE    — reuse as-is.
  VALID_BUT_INCOMPLETE    — keep it; more coverage/dependencies are needed
                             alongside it, not instead of it.
  INVALID_OR_INCOMPATIBLE — regenerate only this artifact (and whatever
                             depends on it) — never triggers a full-episode
                             restart by itself.
  PUBLISHED_CANONICAL_SUBJECT — a duplicate-production block; requires an
                             explicit override reason to proceed.

Integration seams (extend existing capability, not parallel logic):
  - hypogeum/agents/coverage_check.py::compute_beat_coverage() calls
    evaluate_video_clip_compatibility() so a dry-run/corrupted/
    policy-incompatible clip never counts toward available coverage —
    the existing coverage-deficit machinery (fill_coverage_deficits())
    then regenerates exactly that clip, nothing else.
  - hypogeum/pipeline.py's `_CheckpointSpec.compat_check` (new optional
    field) calls evaluate_render_compatibility() so a resumed pipeline run
    revalidates the Assembly checkpoint against the current
    assembly_agent._AGENT_VERSION instead of trusting file presence alone.
  - Both reuse the already-published _AGENT_VERSION field every
    final_render_package already carries in package_metadata — no new
    schema field, no backfill required for existing packages.

Public API:
    canonical_subject(topic_text) -> str
    evaluate_subject_production_decision(
        topic_text, db_path=None, explicit_authorization=None,
    ) -> SubjectDecision
    evaluate_video_clip_compatibility(clip_path) -> ArtifactCheckResult
    evaluate_render_compatibility(
        final_render_package, current_agent_version,
    ) -> ArtifactCheckResult
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hypogeum.topic_discovery import _make_canonical_subject


# ---------------------------------------------------------------------------
# Decision outcomes
# ---------------------------------------------------------------------------

class CompatibilityDecision(Enum):
    VALID_AND_COMPATIBLE = "valid_and_compatible"
    VALID_BUT_INCOMPLETE = "valid_but_incomplete"
    INVALID_OR_INCOMPATIBLE = "invalid_or_incompatible"
    PUBLISHED_CANONICAL_SUBJECT = "published_canonical_subject"


@dataclass
class ArtifactCheckResult:
    decision: CompatibilityDecision
    reasons: List[str] = field(default_factory=list)
    # Structured fields (2026-08-10) — a caller that needs to distinguish
    # "corrupt/unreadable/placeholder" from "real footage but under the
    # required resolution" (e.g. to decide whether an explicit upscale
    # attempt is worth making) must branch on these, never by parsing
    # `reasons` text. None means "not evaluated by this check" (e.g. a
    # non-video-clip decision path that doesn't populate them at all);
    # only evaluate_video_clip_compatibility() currently sets them.
    integrity_valid: Optional[bool] = None
    resolution_valid: Optional[bool] = None
    measured_resolution: Optional[Tuple[int, int]] = None
    required_resolution: Optional[Tuple[int, int]] = None
    failure_codes: List[str] = field(default_factory=list)

    @property
    def reusable(self) -> bool:
        return self.decision in (
            CompatibilityDecision.VALID_AND_COMPATIBLE,
            CompatibilityDecision.VALID_BUT_INCOMPLETE,
        )


@dataclass
class SubjectDecision:
    canonical_subject_id: str
    decision: CompatibilityDecision
    action: str  # "generate_new" | "resume_incremental" | "blocked"
    blocking_episode_id: Optional[str] = None
    reasons: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Canonical subject
# ---------------------------------------------------------------------------

def canonical_subject(topic_text: str) -> str:
    """
    Public wrapper around hypogeum.topic_discovery._make_canonical_subject()
    — the existing, already-relied-upon topic-normalization identity used
    for discovery-time dedup. Reused here (not reimplemented) so subject
    identity is consistent everywhere it's computed: angle-independent,
    deterministic, article/punctuation-stripped.
    """
    return _make_canonical_subject(topic_text)


def evaluate_subject_production_decision(
    topic_text: str,
    db_path: Optional[Path] = None,
    explicit_authorization: Optional[str] = None,
) -> SubjectDecision:
    """
    Canonical behavior, rules 1 and 4 (rules 2/3 — reuse existing artifacts
    for an unpublished episode, generate only what's missing — are the
    existing episode_id-keyed checkpoint/coverage-gate mechanism operating
    on ARTIFACTS, not a subject-level decision; see module docstring).

    Queries `episodes` joined with `publishing_outcomes` for any episode
    whose stored topic normalizes to the same canonical subject and has a
    successful main_episode publish. If found, production is blocked
    unless explicit_authorization is a non-empty string naming the reason
    (e.g. "material_update", "remaster", "new_platform_format",
    "corrective_replacement") — the caller decides whether that reason is
    itself legitimate; this function only decides whether one was given.
    """
    from hypogeum.db import get_connection  # noqa: PLC0415 — leaf-module import convention

    subject = canonical_subject(topic_text)
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT e.episode_id, e.topic
            FROM episodes e
            JOIN publishing_outcomes po ON po.episode_id = e.episode_id
            WHERE po.publishing_status = 'success'
              AND po.deliverable_id = 'main_episode'
              AND e.topic IS NOT NULL
              AND e.topic != ''
            """
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        if canonical_subject(row["topic"]) == subject:
            if explicit_authorization:
                return SubjectDecision(
                    canonical_subject_id=subject,
                    decision=CompatibilityDecision.VALID_AND_COMPATIBLE,
                    action="generate_new",
                    blocking_episode_id=row["episode_id"],
                    reasons=[
                        f"canonical subject already published as {row['episode_id']}; "
                        f"proceeding under explicit authorization: {explicit_authorization}"
                    ],
                )
            return SubjectDecision(
                canonical_subject_id=subject,
                decision=CompatibilityDecision.PUBLISHED_CANONICAL_SUBJECT,
                action="blocked",
                blocking_episode_id=row["episode_id"],
                reasons=[
                    f"canonical subject already published successfully as "
                    f"{row['episode_id']}; duplicate production requires an "
                    f"explicit approved reason (material update, remaster, "
                    f"new platform format, or corrective replacement)"
                ],
            )

    return SubjectDecision(
        canonical_subject_id=subject,
        decision=CompatibilityDecision.VALID_AND_COMPATIBLE,
        action="generate_new",
        reasons=["no prior successful publish found for this canonical subject"],
    )


# ---------------------------------------------------------------------------
# Video clip artifact compatibility
# ---------------------------------------------------------------------------

_MIN_VALID_CLIP_BYTES = 10_000  # matches muapi._MIN_VIDEO_BYTES

# The MUAPI dry-run placeholder is a deterministic, uniform solid fill at
# this exact color (see hypogeum/muapi.py::_generate_dry_run_placeholder_clip,
# "color=c=0x141414"). A real generated clip — even a genuinely dark scene —
# has photographic texture (non-zero pixel spread); the placeholder has
# none. Same principle already used for thumbnail placeholder detection in
# hypogeum/agents/thumbnail_qa.py — reused here for video, not reinvented.
# RGB (0x14,0x14,0x14) = (20,20,20) encodes to Y' (luma, BT.601/709 studio
# range) ≈ 33, not raw 20 — measured empirically via ffmpeg signalstats
# against the real placeholder generator (this is what YAVG actually
# reports, not the source RGB value).
_DRY_RUN_MEAN_LUMINANCE = 33.0
_DRY_RUN_LUMINANCE_TOLERANCE = 3.0
_DRY_RUN_MAX_SPREAD = 2.0  # YMAX-YMIN; a flat fill is 0, real footage is tens of units

# Required source resolution (2026-08-02 Production Output Gate policy).
# Sourced from config.MUAPI_VIDEO_RESOLUTION's intent (the "1080p" already
# requested in every veo3.1 payload — see hypogeum/muapi.py::_build_job_payload)
# expressed as literal pixel dimensions, since the provider has been measured
# to sometimes ignore the requested resolution string entirely (confirmed via
# ffprobe: ep_f3619022's 29/29 real source clips measured 1280x720 despite
# every payload requesting "1080p"). A 720p source scaled up to a 1920x1080
# canvas by Video Renderer's letterbox/pad filter is NOT 1080p compliance —
# this checks the SOURCE clip's own pixel dimensions, never the final render's.
REQUIRED_SOURCE_WIDTH = 1920
REQUIRED_SOURCE_HEIGHT = 1080

# Deliverable-aware required source resolution. hypogeum.source_enhancement
# (committed 10ce0b4) imports required_source_resolution() unconditionally
# at module load time to resolve the correct target per scene's
# source_type — main_episode is landscape, a Short is portrait (1080x1920),
# never the landscape default. Keyed by the same source_type string every
# scene already carries (visual_package.scenes[].source_type —
# "main_episode" | "short"; see hypogeum/schemas.py's _validate_visual_package).
DELIVERABLE_RESOLUTION_POLICY: Dict[str, Tuple[int, int]] = {
    "main_episode": (REQUIRED_SOURCE_WIDTH, REQUIRED_SOURCE_HEIGHT),
    "short": (1080, 1920),
}


def required_source_resolution(
    source_type: Optional[str], *, short_native: bool = False,
) -> Tuple[int, int]:
    """
    Canonical (required_width, required_height) for a deliverable, derived
    from DELIVERABLE_RESOLUTION_POLICY by its source_type. An unrecognized
    or missing source_type falls back to the policy's own "main_episode"
    entry, read live from the table rather than a separate literal.

    `short_native` (2026-09-01): a shorts-only channel's single vertical
    deliverable is still tagged source_type="main_episode" (it IS the
    episode — there is no separate "short" alongside it), but its real shape
    is portrait, not the landscape "main_episode" policy assumes. Passing
    short_native=True (config.SHORT_NATIVE_MODE) routes "main_episode" to
    the SAME portrait policy as "short" here, so every clip for that channel
    is qualified against the resolution it will actually be produced at —
    without a source_type value ever needing to change. Never affects a
    channel whose "main_episode" really is long-form landscape.
    """
    if short_native and source_type == "main_episode":
        return DELIVERABLE_RESOLUTION_POLICY["short"]
    return DELIVERABLE_RESOLUTION_POLICY.get(
        source_type, DELIVERABLE_RESOLUTION_POLICY["main_episode"]
    )


def probe_resolution(path: Path) -> Tuple[int, int]:
    """Real (width, height) of a video file's first video stream via
    ffprobe. Returns (0, 0) on any failure (missing file, corrupted,
    unreadable) — callers must treat (0, 0) as "resolution unknown," never
    as "compliant."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split(",")
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return (0, 0)


def is_resolution_compliant_clip(
    path: Optional[Path],
    required_width: int = REQUIRED_SOURCE_WIDTH,
    required_height: int = REQUIRED_SOURCE_HEIGHT,
) -> bool:
    """
    True only when path's actual measured resolution meets or exceeds the
    required source resolution. Conservative in the OPPOSITE direction from
    is_dry_run_clip(): an unprovable resolution (probe failure, missing
    file) is NOT compliant — "never silently upscale and declare
    compliance" means the burden of proof is on demonstrating the source
    really is high-resolution, not on disproving it. This deliberately
    checks strictly >= the requirement (not exact-match) so a provider that
    returns something larger than required still passes.
    """
    if path is None or not path.exists():
        return False
    width, height = probe_resolution(path)
    if width <= 0 or height <= 0:
        return False
    return width >= required_width and height >= required_height


def _probe_duration_seconds(path: Path) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        val = result.stdout.strip()
        if result.returncode == 0 and val:
            return float(val)
    except Exception:
        pass
    return 0.0


def _frame_luminance_stats(path: Path) -> Optional[tuple]:
    """
    Extract one frame and return (mean, spread) grayscale luma (Y'), or
    None if extraction fails. Uses ffmpeg's own signalstats filter — no
    extra image-library dependency beyond what's already required elsewhere
    (PIL is used for thumbnails; ffmpeg signalstats keeps this check purely
    ffprobe/ffmpeg-based, consistent with every other clip inspection in
    this codebase).

    spread = YMAX - YMIN for the sampled frame. signalstats does not emit a
    YSTDEV field by default in this ffmpeg build (verified empirically —
    only YMIN/YLOW/YAVG/YHIGH/YMAX are printed); YMAX-YMIN is 0 for a truly
    flat single-color fill and reliably large (tens of units) for any real
    photographic content, including genuinely dark scenes, making it a
    robust flatness proxy without needing a field that isn't actually
    produced.
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(path),
                "-vframes", "1",
                "-vf", "signalstats,metadata=print",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=30,
        )
        out = result.stderr
        mean = None
        ymin = None
        ymax = None
        for line in out.splitlines():
            if "lavfi.signalstats.YAVG" in line:
                mean = float(line.rsplit("=", 1)[-1])
            elif "lavfi.signalstats.YMIN" in line:
                ymin = float(line.rsplit("=", 1)[-1])
            elif "lavfi.signalstats.YMAX" in line:
                ymax = float(line.rsplit("=", 1)[-1])
        if mean is not None and ymin is not None and ymax is not None:
            return (mean, ymax - ymin)
    except Exception:
        pass
    return None


def is_dry_run_clip(path: Optional[Path]) -> bool:
    """
    True only when path is confirmed to match the MUAPI dry-run placeholder
    signature (flat 0x141414 fill, ~zero pixel variance — see
    hypogeum/muapi.py::_generate_dry_run_placeholder_clip). False for
    anything that can't be proven to be a placeholder, including a file
    ffmpeg can't decode at all — that's a separate corruption/missing-clip
    concern already handled upstream (timeline_builder's clip-duration
    fallback, evaluate_video_clip_compatibility() for a full artifact
    audit). This narrow, single-purpose check is what
    hypogeum.agents.coverage_check.compute_beat_coverage() gates on, so a
    real clip is never excluded from coverage for reasons other than
    "provably a dry-run placeholder."
    """
    if path is None or not path.exists():
        return False
    stats = _frame_luminance_stats(path)
    if stats is None:
        return False  # cannot prove it's a placeholder -> don't claim it is
    mean, spread = stats
    return (
        abs(mean - _DRY_RUN_MEAN_LUMINANCE) <= _DRY_RUN_LUMINANCE_TOLERANCE
        and spread <= _DRY_RUN_MAX_SPREAD
    )


def evaluate_video_clip_compatibility(
    clip_path: Optional[Path],
    source_type: Optional[str] = None,
) -> ArtifactCheckResult:
    """
    Technical validity of one already-generated video clip, independent of
    the render/assembly step that might later consume it. A clip that
    passes this check is real, playable, forward-only source footage —
    exactly the kind of artifact the canonical policy says to preserve and
    reuse rather than quarantine by age.

    source_type (2026-08-10), when given, resolves the required source
    resolution via required_source_resolution(source_type) — long-form
    vs. Short — instead of this function's fixed landscape default
    (REQUIRED_SOURCE_WIDTH/HEIGHT). None (default) preserves every
    existing caller's exact prior behavior. This is the single canonical
    place deliverable-aware technical validity is decided; a caller must
    never reimplement its own copy of this logic (e.g. by re-deriving
    "integrity OK but under-resolution" from the human-readable
    `reasons` text) — see the structured fields below instead.

    The returned ArtifactCheckResult's structured fields let a caller
    distinguish genuine corruption from a resolution-only shortfall
    without ever parsing `reasons`:
      integrity_valid      — real, playable, non-corrupt, non-placeholder
                              footage, independent of resolution.
      resolution_valid     — measured dimensions meet the resolution
                              requirement (only meaningful when
                              integrity_valid is True; None otherwise —
                              resolution was never assessed).
      measured_resolution  — the clip's actual ffprobe-measured
                              (width, height); never a provider's claimed
                              resolution label, which this function never
                              reads at all.
      required_resolution  — the (width, height) this evaluation was
                              judged against.
      failure_codes        — stable machine-checkable codes (never
                              free text) for why integrity or resolution
                              failed; empty on a full pass.

    Deliberately does NOT evaluate anything about how the clip is later
    concatenated/stretched — that's the renderer's job (see
    hypogeum.agents.video_renderer.InsufficientCoverageError) and is
    evaluated separately by evaluate_render_compatibility() for the
    DERIVED render, not the source clip.
    """
    required_width, required_height = (
        required_source_resolution(source_type)
        if source_type is not None
        else (REQUIRED_SOURCE_WIDTH, REQUIRED_SOURCE_HEIGHT)
    )

    if clip_path is None:
        return ArtifactCheckResult(
            CompatibilityDecision.INVALID_OR_INCOMPATIBLE, ["no clip path"],
            integrity_valid=False, failure_codes=["no_clip_path"],
        )
    if not clip_path.exists():
        return ArtifactCheckResult(
            CompatibilityDecision.INVALID_OR_INCOMPATIBLE, ["file does not exist"],
            integrity_valid=False, failure_codes=["file_missing"],
        )
    size = clip_path.stat().st_size
    if size < _MIN_VALID_CLIP_BYTES:
        return ArtifactCheckResult(
            CompatibilityDecision.INVALID_OR_INCOMPATIBLE,
            [f"file too small to be real footage ({size} bytes)"],
            integrity_valid=False, failure_codes=["file_too_small"],
        )
    duration = _probe_duration_seconds(clip_path)
    if duration <= 0.0:
        return ArtifactCheckResult(
            CompatibilityDecision.INVALID_OR_INCOMPATIBLE,
            ["ffprobe could not read a real duration (corrupted or unreadable)"],
            integrity_valid=False, failure_codes=["unreadable_duration"],
        )
    if is_dry_run_clip(clip_path):
        return ArtifactCheckResult(
            CompatibilityDecision.INVALID_OR_INCOMPATIBLE,
            ["matches the MUAPI dry-run placeholder signature (flat 0x141414 fill)"],
            integrity_valid=False, failure_codes=["dry_run_placeholder"],
        )

    measured = probe_resolution(clip_path)
    if not is_resolution_compliant_clip(clip_path, required_width, required_height):
        return ArtifactCheckResult(
            CompatibilityDecision.INVALID_OR_INCOMPATIBLE,
            [
                f"source resolution {measured[0]}x{measured[1]} is below the required "
                f"{required_width}x{required_height} — a low-resolution "
                f"source scaled up at render time is not compliance"
            ],
            integrity_valid=True, resolution_valid=False,
            measured_resolution=measured, required_resolution=(required_width, required_height),
            failure_codes=["resolution_below_requirement"],
        )
    return ArtifactCheckResult(
        CompatibilityDecision.VALID_AND_COMPATIBLE,
        [f"real footage, {duration:.2f}s, {size} bytes"],
        integrity_valid=True, resolution_valid=True,
        measured_resolution=measured, required_resolution=(required_width, required_height),
        failure_codes=[],
    )


# ---------------------------------------------------------------------------
# Derived-render (final_render_package) compatibility
# ---------------------------------------------------------------------------

def evaluate_source_enhancement_compatibility(
    asset: dict,
    clip_path: Optional[Path],
    config,
    source_type: Optional[str] = None,
) -> ArtifactCheckResult:
    """
    Decide whether a previously recorded source_enhancement outcome (see
    hypogeum/source_enhancement.py) for one asset_manifest entry is still
    trustworthy under CURRENT policy, or must be redone.

    Four independent provenance dimensions are compared against the asset's
    recorded `source_enhancement` block — any mismatch makes this ONE
    asset incompatible; every other asset in the episode is evaluated
    independently and unaffected (selective regeneration, never a
    whole-episode restart):

      1. generation model  — the clip's own recorded producing model no
         longer matches the current tier -> model mapping. The pre-
         enhancement source itself is stale; re-enhancing it would just
         polish an outdated clip. (Full regeneration of the base clip is
         VPD's concern, not this function's — this only reports the
         mismatch so a caller can route to it; see module note below.)
      2. enhancement provider  — config.ENHANCEMENT_PROVIDER no longer
         matches the provider this asset was actually enhanced with.
      3. enhancement version   — the resolved provider's `.version` no
         longer matches what was recorded (same provider, bumped internally).
      4. required source resolution — required_source_resolution(source_type)
         no longer matches what this asset was validated against (deliverable-
         aware, not a single global constant — a Short's asset_manifest entry
         carries no source_type of its own, so the caller must resolve it
         from the owning scene and pass it in here; source_type=None
         resolves to the landscape default).

    A clip with no source_enhancement record at all is not "incompatible"
    in the policy-drift sense this function evaluates — it simply hasn't
    been evaluated yet, which is the ordinary state for a fresh asset. This
    function only judges EXISTING records, so an absent block is reported
    as INVALID_OR_INCOMPATIBLE with a distinct reason ("not yet evaluated")
    the caller should treat identically to "must run source enhancement,"
    not as an error.
    """
    se = (asset or {}).get("source_enhancement")
    if not se:
        return ArtifactCheckResult(
            CompatibilityDecision.INVALID_OR_INCOMPATIBLE,
            ["no source_enhancement record — not yet evaluated"],
        )

    reasons: List[str] = []

    recorded_model = asset.get("model") or asset.get("model_used")
    tier_used = asset.get("tier_used")
    current_model = _current_tier_model(tier_used, config)
    if current_model and recorded_model and recorded_model != current_model:
        reasons.append(
            f"generation model changed: clip produced by {recorded_model!r}, "
            f"tier {tier_used} currently maps to {current_model!r}"
        )

    required_width, required_height = required_source_resolution(
        source_type,
        short_native=getattr(config, "SHORT_NATIVE_MODE", False) is True,
    )
    if se.get("required_width") != required_width or se.get("required_height") != required_height:
        reasons.append(
            f"required source resolution policy changed: recorded "
            f"{se.get('required_width')}x{se.get('required_height')}, "
            f"current policy requires {required_width}x{required_height}"
        )

    if se.get("applied"):
        if se.get("validation_result") != "passed":
            reasons.append(
                f"prior enhancement attempt did not pass validation "
                f"(validation_result={se.get('validation_result')!r})"
            )
        else:
            from hypogeum.providers.enhancement import get_enhancement_provider  # noqa: PLC0415

            current_provider = get_enhancement_provider(config)
            if se.get("provider") != current_provider.name:
                reasons.append(
                    f"enhancement provider changed: recorded {se.get('provider')!r}, "
                    f"current policy uses {current_provider.name!r}"
                )
            elif se.get("model") != current_provider.model:
                reasons.append(
                    f"enhancement model changed: recorded {se.get('model')!r}, "
                    f"current provider uses {current_provider.model!r}"
                )
            elif se.get("provider_version") != current_provider.version:
                reasons.append(
                    f"enhancement version changed: recorded "
                    f"{se.get('provider_version')!r}, current provider is "
                    f"v{current_provider.version}"
                )

    if reasons:
        return ArtifactCheckResult(CompatibilityDecision.INVALID_OR_INCOMPATIBLE, reasons)

    if not is_resolution_compliant_clip(clip_path, required_width, required_height):
        width, height = probe_resolution(clip_path) if clip_path else (0, 0)
        return ArtifactCheckResult(
            CompatibilityDecision.INVALID_OR_INCOMPATIBLE,
            [f"on-disk clip measures {width}x{height}, no longer resolution-compliant"],
        )

    return ArtifactCheckResult(
        CompatibilityDecision.VALID_AND_COMPATIBLE,
        ["prior source_enhancement decision still valid under current policy"],
    )


def _current_tier_model(tier_used: Optional[int], config) -> Optional[str]:
    """Mirrors hypogeum.muapi._tier_to_model()'s tier->model mapping for
    text-to-video tiers, without importing hypogeum.muapi (that module
    imports this one for its own resolution checks — importing it back
    would be circular). Reference-image (I2V) routing is intentionally
    not modeled here; this is only used to detect a stale non-I2V tier
    mapping."""
    if tier_used == 1:
        return getattr(config, "MUAPI_T1_MODEL", None)
    if tier_used == 3:
        return getattr(config, "MUAPI_T3_MODEL", None)
    if tier_used == 2:
        return getattr(config, "MUAPI_T2_MODEL", None)
    return None


def evaluate_render_compatibility(
    final_render_package: dict,
    current_agent_version: str,
) -> ArtifactCheckResult:
    """
    Policy/version compatibility of a DERIVED render (main episode mp4,
    Shorts mp4s, and the final_render_package describing them) — distinct
    from the source clips it was assembled from, which are evaluated
    independently by evaluate_video_clip_compatibility().

    A render produced by an older assembly_agent._AGENT_VERSION was
    assembled under different (possibly now-forbidden) rules and must be
    regenerated regardless of how recent its timestamp is. A render
    produced by the current version is compatible. This is the file's own
    recorded provenance field (package_metadata.assembly_agent_version,
    already written by every final_render_package) — no new schema field,
    no backfill needed.
    """
    produced_version = (
        final_render_package.get("package_metadata", {}).get("assembly_agent_version")
    )
    if not produced_version:
        return ArtifactCheckResult(
            CompatibilityDecision.INVALID_OR_INCOMPATIBLE,
            ["no assembly_agent_version recorded on this package"],
        )
    if produced_version != current_agent_version:
        return ArtifactCheckResult(
            CompatibilityDecision.INVALID_OR_INCOMPATIBLE,
            [
                f"produced by assembly_agent v{produced_version}, "
                f"current policy version is v{current_agent_version} — "
                f"regenerate this render (source clips are unaffected)"
            ],
        )
    return ArtifactCheckResult(
        CompatibilityDecision.VALID_AND_COMPATIBLE,
        [f"produced by current assembly_agent v{produced_version}"],
    )
