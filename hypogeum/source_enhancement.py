"""
hypogeum/source_enhancement.py — Source Enhancement pipeline stage.

Canonical pipeline (2026-08-02):

    Source Generation -> Source Validation -> Source Enhancement ->
    Source Validation -> Coverage -> Assembly

Source Generation is Visual Package Director Phase 2 (hypogeum.agents.
visual_package_director / hypogeum.muapi). The first Source Validation pass
is this module's own per-asset resolution probe, deciding what needs
enhancing. Source Enhancement (this module) then conditionally raises
under-resolution clips to the required source resolution. The second Source
Validation pass is the pre-existing hypogeum.artifact_compatibility.
evaluate_video_clip_compatibility() call already embedded in hypogeum.
agents.coverage_check.compute_beat_coverage() — unchanged by this module,
and now naturally passing for any clip this stage successfully enhanced.

Architecture rule: this module is PROVIDER-AGNOSTIC. It knows nothing about
MUAPI, Topaz, or any other vendor — it only calls hypogeum.providers.
enhancement.get_enhancement_provider(config) and works entirely in terms of
the EnhancementProvider/EnhancementResult abstraction. Swapping the
configured provider (config.ENHANCEMENT_PROVIDER) never requires touching
this file.

Policy (per asset):

    if source_resolution >= required_resolution:
        reuse source (no provider call, no cost)
    else:
        enhance source via the configured provider

Every enhancement attempt — success or failure — re-probes the candidate
output with ffprobe (width, height, codec, duration, fps) independently of
whatever the provider itself claimed. Only a validated output ever replaces
the canonical source clip.

Asset lifecycle (2026-08-04 — corrects a real architectural bug found in
production): a validation failure must NEVER destroy a generated,
already-paid-for artifact. The lifecycle is:

    Provider -> Generated Asset -> Archive/Asset Registry -> Validation -> Decision

A candidate the provider produces is written to a stable sibling path next
to the canonical clip (`{clip}.enhanced.mp4`) — that path, together with a
sha256 checksum, IS this stage's asset registry entry, recorded on the
asset's `source_enhancement` block regardless of outcome. Validation then
runs against that registered file. The Decision step is purely additive:
PASS moves the candidate onto the canonical clip_path; FAIL leaves the
candidate exactly where it is, quarantined and fully described (provider,
model, version, cost, generation time, resolution, checksum, failure
reason) — never deleted as a side effect of validation. Deletion is only
ever an Archive/Lifecycle-policy concern, which does not yet exist in this
codebase; until it does, every candidate this stage has ever produced
stays on disk.

This is what makes reconsideration possible without paying again: on a
later run, if the ONLY thing that changed is the validation policy itself
(e.g. a required-resolution correction), this stage re-probes the
quarantined candidate directly — zero provider call, zero new cost — and
promotes it if it now passes. Reconsideration only trusts a quarantined
candidate whose provider/model/version still match the currently
configured provider AND whose checksum still matches what was recorded at
archive time; either mismatch means a fresh attempt is required instead of
a free reconsideration.

The original source clip itself is untouched throughout this entire
lifecycle until an enhancement is genuinely promoted — this is what keeps
the stage fail-closed on the SOURCE side: a clip that still doesn't meet
the resolution requirement is caught by the second Source Validation pass
in coverage_check.py exactly as it was before this stage existed, and
existing coverage-deficit/clip-repair machinery handles it unchanged.

Idempotent resume: hypogeum.artifact_compatibility.
evaluate_source_enhancement_compatibility() decides whether an existing
source_enhancement record (from a prior run of this stage) is still valid
under the CURRENT enhancement provider/version/required-resolution policy.
An asset whose record is still compatible is skipped entirely (zero cost);
one whose provenance has drifted (provider swapped, version bumped,
required resolution changed) is either reconsidered from its quarantined
archive (zero cost, see above) or re-evaluated from scratch via a real
provider call.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.artifact_compatibility import (
    evaluate_source_enhancement_compatibility,
    is_dry_run_clip,
    probe_resolution,
    required_source_resolution,
)
from hypogeum.logger import get_logger
from hypogeum.providers.enhancement import get_enhancement_provider

_ASSETS_ROOT = Path(__file__).resolve().parent.parent / ".hypogeum-assets"

_DURATION_TOLERANCE_S = 1.0


def _episode_dir(episode_id: str, output_dir: Optional[Path]) -> Path:
    """Mirrors hypogeum.pipeline._load_package()'s base-path convention
    exactly, so this stage reads/writes the same visual_package.json and
    the same clip files VPD (Source Generation) just produced in the same
    pipeline call."""
    return Path(output_dir) if output_dir is not None else (_ASSETS_ROOT / "episodes" / episode_id)


def _resolve_clip_path(asset_id: Optional[str], episode_id: str, episode_dir: Path) -> Optional[Path]:
    """asset_id is root-relative ("episodes/{episode_id}/video/sc_s02");
    episode_dir is that same asset's episode-relative base. Stripping the
    "episodes/{episode_id}/" prefix and joining onto episode_dir reproduces
    exactly the path hypogeum.muapi.scene_file_path() wrote to, without
    needing to separately resolve an assets_root (avoids the walk-up
    ambiguity hypogeum.agents.publishing_agent._resolve_assets_root() has
    to handle for a later, standalone invocation — this stage runs in the
    same call, with the same output_dir, VPD just used)."""
    if not asset_id:
        return None
    prefix = f"episodes/{episode_id}/"
    rel = asset_id[len(prefix):] if asset_id.startswith(prefix) else asset_id
    return episode_dir / f"{rel}.mp4"


def _probe_video_stream(path: Path) -> Optional[dict]:
    """One combined ffprobe call: width, height, codec_name, fps, duration."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,codec_name,r_frame_rate:format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams:
            return None
        s = streams[0]
        fmt = data.get("format", {})
        duration_raw = fmt.get("duration")
        return {
            "width": int(s.get("width") or 0),
            "height": int(s.get("height") or 0),
            "codec_name": s.get("codec_name") or None,
            "fps": s.get("r_frame_rate") or None,
            "duration": float(duration_raw) if duration_raw else 0.0,
        }
    except Exception:
        return None


_BLACK_TAIL_MIN_BLACK_SECONDS = 0.5   # ffmpeg blackdetect's own minimum run length
_BLACK_TAIL_END_TOLERANCE_S = 0.2     # a black region ending within this of EOF counts as "the tail"


def _clip_has_black_tail(clip_path: Path) -> bool:
    """
    True if `clip_path`'s content fades/cuts to black and stays black
    through (or almost through) the end of the file — the real
    ep_f3619022 Short 001 defect: an 8s provider-generated clip played
    real content for ~4.8s then held pure black for its remaining ~3.1s.

    A clip like this is technically valid (decodable, correct resolution,
    correct duration) so no other check in this module catches it. It
    matters specifically because hypogeum.agents.video_renderer only
    trims/stretches the LAST clip in a multi-clip beat — any EARLIER clip
    is trusted to play in full, so a black tail on a non-last clip reaches
    the final render completely unexamined.

    Uses ffmpeg's blackdetect filter (the same primitive already used
    elsewhere in this codebase for pixel-level black-region evidence) and
    checks only whether the LAST detected black region's end lands at or
    near the clip's own end — a black flash or dark passage earlier in the
    clip, or one that recovers to real content before the file ends, is
    not this defect and is not flagged.
    """
    try:
        probe = _probe_video_stream(clip_path)
        duration = probe["duration"] if probe else 0.0
        if duration <= 0:
            return False  # corrupt/unreadable source — the dedicated check above handles this

        result = subprocess.run(
            [
                "ffmpeg", "-i", str(clip_path),
                "-vf", f"blackdetect=d={_BLACK_TAIL_MIN_BLACK_SECONDS}:pic_th=0.98",
                "-an", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=60,
        )
        stderr = result.stderr or ""
        last_black_end: Optional[float] = None
        for line in stderr.splitlines():
            if "black_end:" not in line:
                continue
            try:
                last_black_end = float(line.split("black_end:", 1)[1].split()[0])
            except (IndexError, ValueError):
                continue
        if last_black_end is None:
            return False
        return (duration - last_black_end) <= _BLACK_TAIL_END_TOLERANCE_S
    except Exception:
        return False


def _sha256_file(path: Path) -> Optional[str]:
    """sha256 of a file's contents, or None if it can't be read. Used to
    prove a quarantined candidate hasn't changed since it was archived
    before ever trusting it as "the same paid output" for a free
    reconsideration."""
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _record(
    applied: bool,
    required: bool,
    validation_result: str,
    required_width: int,
    required_height: int,
    original_resolution: Optional[str] = None,
    enhanced_resolution: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    provider_version: Optional[str] = None,
    cost_usd: Optional[float] = None,
    generation_time_s: Optional[float] = None,
    failure_reason: Optional[str] = None,
    archived_path: Optional[str] = None,
    archived_checksum: Optional[str] = None,
    archived_at: Optional[str] = None,
    request_id: Optional[str] = None,
    upgrade_submitted_claim: Optional[bool] = None,
    upgrade_base_video_url_claim: Optional[str] = None,
) -> dict:
    """The source_enhancement block recorded onto one asset_manifest entry.
    Every field named in the architecture's manifest-recording requirement
    is present on every asset this stage touches, success or failure.

    archived_path/archived_checksum/archived_at (2026-08-04): set only when
    validation_result == "failed" AND the provider actually produced a
    real candidate file — this is the asset registry entry that makes the
    candidate reconsiderable later without paying again. None on every
    other outcome (nothing was quarantined, or the candidate was already
    promoted to canonical)."""
    return {
        "applied": applied,
        "required": required,
        "original_resolution": original_resolution,
        "enhanced_resolution": enhanced_resolution,
        "provider": provider,
        "model": model,
        "provider_version": provider_version,
        "cost_usd": cost_usd,
        "generation_time_s": generation_time_s,
        "required_width": required_width,
        "required_height": required_height,
        "validation_result": validation_result,
        "failure_reason": failure_reason,
        "archived_path": archived_path,
        "archived_checksum": archived_checksum,
        "archived_at": archived_at,
        # The upscale provider's own job/request id (2026-08-10) — kept
        # separate from any generation-side request_id on the owning
        # asset_manifest entry, since the two are distinct, separately
        # billed provider calls.
        "request_id": request_id,
        # The provider's own upgrade_submitted/upgrade_base_video_url
        # CLAIMS (2026-08-10), recorded verbatim for traceability only —
        # never read anywhere in this module's PASS/FAIL decision, which
        # is made exclusively from the independent re-probe of
        # output_path above (see the module docstring's "provider
        # claiming success is not proof" requirement).
        "upgrade_submitted_claim": upgrade_submitted_claim,
        "upgrade_base_video_url_claim": upgrade_base_video_url_claim,
    }


def _archive_candidate(dest_path: Path) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Register a generated candidate as a quarantined asset instead of
    deleting it: computes its checksum and returns (path, checksum,
    archived_at) for the source_enhancement record. The file is left
    exactly where the provider/this stage wrote it (dest_path,
    "{clip}.enhanced.mp4") — that stable sibling path next to the
    canonical clip IS the archive location; no separate directory tree is
    needed. Returns (None, None, None) if dest_path was never actually
    written (a provider failure before any real output existed — nothing
    to quarantine).
    """
    if not dest_path.exists():
        return None, None, None
    checksum = _sha256_file(dest_path)
    archived_at = datetime.now(timezone.utc).isoformat()
    return str(dest_path), checksum, archived_at


def _same_provider_identity(prior_se: dict, provider) -> bool:
    return (
        prior_se.get("provider") == provider.name
        and prior_se.get("model") == provider.model
        and prior_se.get("provider_version") == provider.version
    )


def _try_promote_from_archive(
    asset: dict,
    clip_path: Path,
    provider,
    required_width: int,
    required_height: int,
    log,
    scene_id: str,
) -> bool:
    """
    Zero-cost reconsideration of a previously quarantined (validation-
    failed) candidate under CURRENT policy. Only trusts the archived file
    when the provider/model/version that produced it still match the
    currently configured provider (no vendor/version drift — that would
    need a fresh attempt, not a free reconsideration) and its checksum
    still matches what was recorded at archive time (proves the file
    hasn't been altered). Promotes and returns True on a genuine pass;
    otherwise leaves the quarantined record untouched and returns False —
    never triggers a new provider call itself.
    """
    prior_se = asset.get("source_enhancement") or {}
    if not (prior_se.get("applied") is True and prior_se.get("validation_result") == "failed"):
        return False
    archived_path_str = prior_se.get("archived_path")
    if not archived_path_str:
        return False
    archived_path = Path(archived_path_str)
    if not archived_path.exists():
        return False
    if not _same_provider_identity(prior_se, provider):
        return False
    if _sha256_file(archived_path) != prior_se.get("archived_checksum"):
        log.warn(
            "source_enhancement_archive_checksum_mismatch",
            scene_id=scene_id, archived_path=str(archived_path),
        )
        return False

    probe = _probe_video_stream(archived_path)
    valid = bool(
        probe
        and probe["width"] >= required_width
        and probe["height"] >= required_height
        and probe["codec_name"]
        and probe["fps"]
        and probe["duration"] > 0
    )
    if not valid:
        log.info(
            "source_enhancement_revalidation_still_failed",
            scene_id=scene_id, probe=probe,
            required=f"{required_width}x{required_height}",
        )
        return False

    archived_path.replace(clip_path)
    enhanced_resolution = f"{probe['width']}x{probe['height']}"
    asset["source_enhancement"] = _record(
        applied=True, required=True,
        validation_result="passed",
        original_resolution=prior_se.get("original_resolution"),
        enhanced_resolution=enhanced_resolution,
        provider=provider.name, model=provider.model, provider_version=provider.version,
        cost_usd=prior_se.get("cost_usd"),  # no new spend — this is the original charge
        generation_time_s=prior_se.get("generation_time_s"),
        required_width=required_width, required_height=required_height,
    )
    log.info(
        "source_enhancement_revalidated_from_archive",
        scene_id=scene_id, enhanced_resolution=enhanced_resolution,
        note="zero new cost — promoted a previously-archived paid candidate under corrected policy",
    )
    return True


def _persist_visual_package(visual_package: dict, episode_id: str, output_dir: Optional[Path]) -> None:
    episode_dir = _episode_dir(episode_id, output_dir)
    episode_dir.mkdir(parents=True, exist_ok=True)
    path = episode_dir / f"visual_package_{episode_id}.json"
    path.write_text(json.dumps(visual_package, indent=2), encoding="utf-8")


def run_source_enhancement(
    visual_package: dict,
    config,
    episode_id: str,
    output_dir: Optional[Path] = None,
    channel_id: Optional[str] = None,
) -> dict:
    """
    Source Enhancement pipeline stage. Mutates and returns visual_package
    with a source_enhancement block recorded on every real, evaluated
    asset. Persists the updated package to disk when anything changed, so
    a resumed run sees this stage's own decisions on the next pass.
    """
    log = get_logger(episode_id=episode_id, agent="source_enhancement")

    if not getattr(config, "ENHANCEMENT_ENABLED", True):
        log.info("source_enhancement_disabled", episode_id=episode_id)
        return visual_package

    episode_dir = _episode_dir(episode_id, output_dir)

    # Deliverable-aware required resolution (2026-08-03 fix): the
    # asset_manifest entry itself carries no source_type, only the owning
    # scene does (visual_package.scenes[].source_type — "main_episode" |
    # "short"), so build a scene_id -> source_type lookup once. Confirmed
    # in production: applying a single global landscape (1920x1080)
    # requirement to portrait Shorts caused MUAPI's AI Video Upscaler's
    # correct 1080x1920 output to be rejected as non-compliant.
    #
    # Coverage-fill identity tolerance (2026-08-26 fix): clip_repair_agent
    # persists extra clips as manifest entries whose scene_id carries an
    # "__extra<N>" suffix ("sh_short_001_s01__extra1") that has no base
    # scene row of its own. The bare-scene_id lookup missed every such
    # entry, so portrait Short extras were re-evaluated against the
    # landscape requirement and their correct 1080x1920 enhancement output
    # was rejected after real paid calls. The lookup now strips the suffix
    # back to the owning base scene.
    import re as _re

    scene_source_type = {
        s.get("scene_id"): s.get("source_type")
        for s in visual_package.get("scenes", [])
    }

    def _resolve_scene_source_type(sid: Optional[str]):
        if sid is None:
            return None
        if sid in scene_source_type:
            return scene_source_type[sid]
        base = _re.sub(r"__extra\d+$", "", sid)
        return scene_source_type.get(base)

    assets = visual_package.get("asset_manifest", {}).get("assets", [])
    changed = False

    for asset in assets:
        scene_id = asset.get("scene_id")
        asset_id = asset.get("asset_id")
        clip_path = _resolve_clip_path(asset_id, episode_id, episode_dir)
        if clip_path is None or not clip_path.exists():
            continue  # nothing to evaluate — Source Generation/coverage's concern, not this stage's

        source_type = _resolve_scene_source_type(scene_id)
        if qualify_asset(
            asset, clip_path, source_type, config, episode_id, log,
            channel_id=channel_id, output_dir=episode_dir,
        ):
            changed = True

    if changed:
        _persist_visual_package(visual_package, episode_id, output_dir)

    return visual_package


def qualify_asset(
    asset: dict,
    clip_path: Path,
    source_type: Optional[str],
    config,
    episode_id: str,
    log,
    channel_id: Optional[str] = None,
    output_dir: Optional[Path] = None,
) -> bool:
    """
    The one canonical asset-qualification boundary (2026-08-05). Every
    video clip that will become eligible for Assembly — a VPD primary
    clip, a clip_repair_agent coverage-fill extra, a retry, or a reused
    clip — must pass through this function before it is trusted.

    Sequence: idempotent-resume compatibility check -> zero-cost archive
    reconsideration -> reject corrupt/unreadable source -> skip if already
    native-compliant -> else invoke the configured AI enhancement provider
    -> re-probe the provider's output independently -> promote only on a
    confirmed pass; fail closed (original clip left untouched, candidate
    archived, never deleted) otherwise.

    `asset` is any dict shaped like an asset_manifest entry — at minimum
    `scene_id`; optionally `model`/`tier_used`/`source_enhancement` for a
    prior record to reconsider. A brand-new clip (no prior record) simply
    passes `{"scene_id": ..., "source_enhancement": None}` and is
    evaluated fresh. `asset["source_enhancement"]` is mutated in place —
    the caller decides where that record lives (an existing asset_manifest
    entry, or a new one it appends for a clip that didn't have one).

    Never branches on provider name, model name, or a "primary" vs.
    "extra"/"repair" classification — only on ffprobe-measured properties
    of clip_path compared against the required_width/required_height
    contract. This is what makes the same function correct for every
    caller without each needing its own copy of this logic.

    Returns True if `asset["source_enhancement"]` was set or changed (the
    caller should persist), False if nothing needed to change.
    """
    required_width, required_height = required_source_resolution(
        source_type,
        short_native=getattr(config, "SHORT_NATIVE_MODE", False) is True,
    )
    scene_id = asset.get("scene_id", "")

    # --- Idempotent resume: is a prior decision for THIS asset still valid? ---
    compat = evaluate_source_enhancement_compatibility(
        asset, clip_path, config, source_type=source_type,
    )
    if compat.reusable:
        log.info("source_enhancement_reused", scene_id=scene_id, reasons=compat.reasons)
        return False

    # A clip that was previously enhanced (provenance now stale — see
    # compat.reasons above) must go through the provider again even if
    # its current pixels already happen to satisfy the resolution
    # requirement: those pixels were produced by a provider/version this
    # policy no longer trusts. Only a clip that has NEVER been enhanced
    # is eligible for the native-resolution skip below.
    prior_se = asset.get("source_enhancement") or {}
    previously_enhanced = (
        prior_se.get("applied") is True and prior_se.get("validation_result") == "passed"
    )

    # --- Zero-cost reconsideration: a prior FAILED attempt may already
    # have produced a real, still-quarantined candidate that now
    # passes under corrected policy (e.g. a required-resolution fix).
    # Must run before any provider call — see _try_promote_from_archive's
    # own docstring for the exact trust conditions.
    provider = get_enhancement_provider(config)
    if _try_promote_from_archive(
        asset, clip_path, provider, required_width, required_height, log, scene_id,
    ):
        return True

    # A real prior attempt from the SAME provider identity left a
    # verifiable archived candidate that was just reconsidered above and
    # still couldn't be promoted (still non-compliant, or the archive is
    # unverifiable/tampered) — that specific case must never fall
    # through to an automatic fresh provider call. Spending again on a
    # genuinely-still-failing asset is a decision for an operator to
    # make explicitly (e.g. after fixing the actual root cause), not a
    # side effect of resuming the pipeline.
    #
    # This guard only fires when archived_path is present — i.e. a real
    # reconsideration attempt actually happened above. A failed record
    # with NO archived_path (produced by a pre-2026-08-04 run, before
    # this archive-on-failure behavior existed — its candidate was
    # deleted, not quarantined) has nothing to reconsider, so it must
    # fall through to a genuine fresh attempt like any other
    # never-successfully-enhanced asset. Provider/version DRIFT is also
    # deliberately NOT covered by this guard — that is the other case
    # an automatic fresh attempt is still correct (see the module
    # docstring's "re-evaluated from scratch" case).
    if (
        prior_se.get("applied") is True
        and prior_se.get("validation_result") == "failed"
        and prior_se.get("archived_path")
        and _same_provider_identity(prior_se, provider)
    ):
        log.info(
            "source_enhancement_still_quarantined_no_auto_retry",
            scene_id=scene_id,
            archived_path=prior_se.get("archived_path"),
        )
        return False

    # --- Dry-run placeholder content is never a real enhancement target ---
    if is_dry_run_clip(clip_path):
        asset["source_enhancement"] = _record(
            applied=False, required=False,
            validation_result="skipped_dry_run_placeholder",
            required_width=required_width, required_height=required_height,
        )
        return True

    width, height = probe_resolution(clip_path)
    if width <= 0 or height <= 0:
        # Reject corrupt/unreadable source explicitly (2026-08-05): mark it
        # unusable on the record itself rather than silently no-op'ing, so
        # a caller building coverage from asset_manifest never has to
        # separately re-derive "this clip doesn't count." coverage_check/
        # evaluate_video_clip_compatibility() still independently catches
        # this too — belt and suspenders, not a replacement for that check.
        asset["source_enhancement"] = _record(
            applied=False, required=False,
            validation_result="rejected_corrupt_source",
            required_width=required_width, required_height=required_height,
        )
        log.warn("source_enhancement_rejected_corrupt", scene_id=scene_id, clip_path=str(clip_path))
        return True

    # --- Trailing black content is never usable coverage (2026-08-07) ---
    # Real production defect found in ep_f3619022 Short 001: a coverage-
    # fill extra clip's own generated content faded to pure black 4.83s
    # into its 8s duration and never recovered. hypogeum.agents.
    # video_renderer only trims/stretches the LAST clip in a multi-clip
    # beat -- every earlier clip is trusted to play in full, so a black
    # tail on a non-last clip reached the final render unexamined. A clip
    # is technically decodable and correctly resolved here (not corrupt),
    # so the corrupt-source check above does not catch it; this is a
    # distinct content-quality defect, checked independently.
    if _clip_has_black_tail(clip_path):
        asset["source_enhancement"] = _record(
            applied=False, required=False,
            validation_result="rejected_black_tail",
            required_width=required_width, required_height=required_height,
        )
        log.warn("source_enhancement_rejected_black_tail", scene_id=scene_id, clip_path=str(clip_path))
        return True

    original_resolution = f"{width}x{height}"

    # --- Conditional policy: reuse if already compliant (and never
    # previously enhanced — see previously_enhanced note above) ---
    if not previously_enhanced and width >= required_width and height >= required_height:
        asset["source_enhancement"] = _record(
            applied=False, required=False,
            validation_result="skipped_native_resolution",
            original_resolution=original_resolution,
            required_width=required_width, required_height=required_height,
        )
        log.info("source_enhancement_skipped_native", scene_id=scene_id, resolution=original_resolution)
        return True

    # --- Enhancement required: call the configured provider ---
    # (provider was already resolved above for the archive-reconsideration
    # attempt — same instance reused here, no duplicate registry lookup)
    dest_path = clip_path.with_name(clip_path.stem + ".enhanced.mp4")
    result = provider.enhance(
        source_path=clip_path,
        dest_path=dest_path,
        target_width=required_width,
        target_height=required_height,
        target_resolution_label=config.ENHANCEMENT_TARGET_RESOLUTION_LABEL,
        config=config,
        episode_id=episode_id,
        scene_id=scene_id,
        log=log,
        channel_id=channel_id,
        output_dir=output_dir,
    )

    if not result.success:
        archived_path, archived_checksum, archived_at = _archive_candidate(dest_path)
        asset["source_enhancement"] = _record(
            applied=True, required=True,
            validation_result="failed",
            original_resolution=original_resolution,
            provider=provider.name, model=provider.model, provider_version=provider.version,
            cost_usd=result.cost_usd, generation_time_s=result.generation_time_s,
            request_id=result.request_id,
            upgrade_submitted_claim=result.upgrade_submitted,
            upgrade_base_video_url_claim=result.upgrade_base_video_url,
            failure_reason=result.failure_reason,
            required_width=required_width, required_height=required_height,
            archived_path=archived_path, archived_checksum=archived_checksum, archived_at=archived_at,
        )
        log.warn(
            "source_enhancement_provider_failed",
            scene_id=scene_id, reason=result.failure_reason, archived_path=archived_path,
        )
        return True  # FAIL CLOSED on the source clip — original clip untouched, still
        # non-compliant. Any generated candidate is quarantined, never deleted.

    # --- Provider claims success is not proof: re-probe independently ---
    probe = _probe_video_stream(dest_path)
    original_duration = _probe_video_stream(clip_path)
    original_duration_s = original_duration["duration"] if original_duration else 0.0

    valid = bool(
        probe
        and probe["width"] >= required_width
        and probe["height"] >= required_height
        and probe["codec_name"]
        and probe["fps"]
        and probe["duration"] > 0
        and (
            original_duration_s <= 0
            or abs(probe["duration"] - original_duration_s) <= _DURATION_TOLERANCE_S
        )
    )

    if not valid:
        reason = (
            f"post-enhancement validation failed: probe={probe}, "
            f"expected>={required_width}x{required_height}, "
            f"original_duration={original_duration_s}"
        )
        archived_path, archived_checksum, archived_at = _archive_candidate(dest_path)
        asset["source_enhancement"] = _record(
            applied=True, required=True,
            validation_result="failed",
            original_resolution=original_resolution,
            enhanced_resolution=(
                f"{probe['width']}x{probe['height']}" if probe else None
            ),
            provider=provider.name, model=provider.model, provider_version=provider.version,
            cost_usd=result.cost_usd, generation_time_s=result.generation_time_s,
            request_id=result.request_id,
            upgrade_submitted_claim=result.upgrade_submitted,
            upgrade_base_video_url_claim=result.upgrade_base_video_url,
            failure_reason=reason,
            required_width=required_width, required_height=required_height,
            archived_path=archived_path, archived_checksum=archived_checksum, archived_at=archived_at,
        )
        log.warn(
            "source_enhancement_validation_failed",
            scene_id=scene_id, reason=reason, archived_path=archived_path,
        )
        return True  # FAIL CLOSED on the source clip — original clip untouched.
        # The generated candidate is quarantined at archived_path, never
        # deleted, and can be reconsidered later at zero cost if policy
        # changes (see _try_promote_from_archive).

    # --- Validated: atomically commit the enhanced clip as canonical ---
    dest_path.replace(clip_path)
    enhanced_resolution = f"{probe['width']}x{probe['height']}"
    asset["source_enhancement"] = _record(
        applied=True, required=True,
        validation_result="passed",
        original_resolution=original_resolution,
        enhanced_resolution=enhanced_resolution,
        provider=provider.name, model=provider.model, provider_version=provider.version,
        cost_usd=result.cost_usd, generation_time_s=result.generation_time_s,
            request_id=result.request_id,
            upgrade_submitted_claim=result.upgrade_submitted,
            upgrade_base_video_url_claim=result.upgrade_base_video_url,
        required_width=required_width, required_height=required_height,
    )
    log.info(
        "source_enhancement_completed",
        scene_id=scene_id,
        original_resolution=original_resolution,
        enhanced_resolution=enhanced_resolution,
        provider=provider.name,
        cost_usd=result.cost_usd,
    )
    return True
