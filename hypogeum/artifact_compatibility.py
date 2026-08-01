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
from typing import List, Optional

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


def evaluate_video_clip_compatibility(clip_path: Optional[Path]) -> ArtifactCheckResult:
    """
    Technical validity of one already-generated video clip, independent of
    the render/assembly step that might later consume it. A clip that
    passes this check is real, playable, forward-only source footage —
    exactly the kind of artifact the canonical policy says to preserve and
    reuse rather than quarantine by age.

    Deliberately does NOT evaluate anything about how the clip is later
    concatenated/stretched — that's the renderer's job (see
    hypogeum.agents.video_renderer.InsufficientCoverageError) and is
    evaluated separately by evaluate_render_compatibility() for the
    DERIVED render, not the source clip.
    """
    if clip_path is None:
        return ArtifactCheckResult(
            CompatibilityDecision.INVALID_OR_INCOMPATIBLE, ["no clip path"],
        )
    if not clip_path.exists():
        return ArtifactCheckResult(
            CompatibilityDecision.INVALID_OR_INCOMPATIBLE, ["file does not exist"],
        )
    size = clip_path.stat().st_size
    if size < _MIN_VALID_CLIP_BYTES:
        return ArtifactCheckResult(
            CompatibilityDecision.INVALID_OR_INCOMPATIBLE,
            [f"file too small to be real footage ({size} bytes)"],
        )
    duration = _probe_duration_seconds(clip_path)
    if duration <= 0.0:
        return ArtifactCheckResult(
            CompatibilityDecision.INVALID_OR_INCOMPATIBLE,
            ["ffprobe could not read a real duration (corrupted or unreadable)"],
        )
    if is_dry_run_clip(clip_path):
        return ArtifactCheckResult(
            CompatibilityDecision.INVALID_OR_INCOMPATIBLE,
            ["matches the MUAPI dry-run placeholder signature (flat 0x141414 fill)"],
        )
    return ArtifactCheckResult(
        CompatibilityDecision.VALID_AND_COMPATIBLE,
        [f"real footage, {duration:.2f}s, {size} bytes"],
    )


# ---------------------------------------------------------------------------
# Derived-render (final_render_package) compatibility
# ---------------------------------------------------------------------------

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
