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
the canonical source clip. On any failure (provider-side or validation-
side), the original clip is left completely untouched and the failure is
recorded — this is what makes the stage fail closed: a clip that still
doesn't meet the resolution requirement is still caught by the second
Source Validation pass in coverage_check.py exactly as it was before this
stage existed, and existing coverage-deficit/clip-repair machinery handles
it unchanged.

Idempotent resume: hypogeum.artifact_compatibility.
evaluate_source_enhancement_compatibility() decides whether an existing
source_enhancement record (from a prior run of this stage) is still valid
under the CURRENT enhancement provider/version/required-resolution policy.
An asset whose record is still compatible is skipped entirely (zero cost);
one whose provenance has drifted (provider swapped, version bumped,
required resolution changed) is re-evaluated from scratch.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from hypogeum.artifact_compatibility import (
    evaluate_source_enhancement_compatibility,
    is_dry_run_clip,
    probe_resolution,
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
    exactly the path hypogeum.muapi._scene_file_path() wrote to, without
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
) -> dict:
    """The source_enhancement block recorded onto one asset_manifest entry.
    Every field named in the architecture's manifest-recording requirement
    is present on every asset this stage touches, success or failure."""
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
    }


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

    required_width = config.ENHANCEMENT_REQUIRED_WIDTH
    required_height = config.ENHANCEMENT_REQUIRED_HEIGHT
    episode_dir = _episode_dir(episode_id, output_dir)

    assets = visual_package.get("asset_manifest", {}).get("assets", [])
    changed = False

    for asset in assets:
        scene_id = asset.get("scene_id")
        asset_id = asset.get("asset_id")
        clip_path = _resolve_clip_path(asset_id, episode_id, episode_dir)
        if clip_path is None or not clip_path.exists():
            continue  # nothing to evaluate — Source Generation/coverage's concern, not this stage's

        # --- Idempotent resume: is a prior decision for THIS asset still valid? ---
        compat = evaluate_source_enhancement_compatibility(asset, clip_path, config)
        if compat.reusable:
            log.info("source_enhancement_reused", scene_id=scene_id, reasons=compat.reasons)
            continue

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

        # --- Dry-run placeholder content is never a real enhancement target ---
        if is_dry_run_clip(clip_path):
            asset["source_enhancement"] = _record(
                applied=False, required=False,
                validation_result="skipped_dry_run_placeholder",
                required_width=required_width, required_height=required_height,
            )
            changed = True
            continue

        width, height = probe_resolution(clip_path)
        if width <= 0 or height <= 0:
            # Unprobeable — corruption is coverage_check/evaluate_video_clip_
            # compatibility()'s concern; this stage only enhances real,
            # readable footage.
            continue

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
            changed = True
            log.info("source_enhancement_skipped_native", scene_id=scene_id, resolution=original_resolution)
            continue

        # --- Enhancement required: call the configured provider ---
        provider = get_enhancement_provider(config)
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
        )

        if not result.success:
            asset["source_enhancement"] = _record(
                applied=True, required=True,
                validation_result="failed",
                original_resolution=original_resolution,
                provider=provider.name, model=provider.model, provider_version=provider.version,
                cost_usd=result.cost_usd, generation_time_s=result.generation_time_s,
                failure_reason=result.failure_reason,
                required_width=required_width, required_height=required_height,
            )
            dest_path.unlink(missing_ok=True)
            changed = True
            log.warn(
                "source_enhancement_provider_failed",
                scene_id=scene_id, reason=result.failure_reason,
            )
            continue  # FAIL CLOSED — original clip untouched, still non-compliant

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
            asset["source_enhancement"] = _record(
                applied=True, required=True,
                validation_result="failed",
                original_resolution=original_resolution,
                enhanced_resolution=(
                    f"{probe['width']}x{probe['height']}" if probe else None
                ),
                provider=provider.name, model=provider.model, provider_version=provider.version,
                cost_usd=result.cost_usd, generation_time_s=result.generation_time_s,
                failure_reason=reason,
                required_width=required_width, required_height=required_height,
            )
            dest_path.unlink(missing_ok=True)
            changed = True
            log.warn("source_enhancement_validation_failed", scene_id=scene_id, reason=reason)
            continue  # FAIL CLOSED — original clip untouched

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
            required_width=required_width, required_height=required_height,
        )
        changed = True
        log.info(
            "source_enhancement_completed",
            scene_id=scene_id,
            original_resolution=original_resolution,
            enhanced_resolution=enhanced_resolution,
            provider=provider.name,
            cost_usd=result.cost_usd,
        )

    if changed:
        _persist_visual_package(visual_package, episode_id, output_dir)

    return visual_package
