"""
hypogeum/agents/caption_agent.py — Caption Agent (M6.x)

Sole authority for SRT caption generation from narration segment results.
Receives raw NarrationSegmentResult objects from the Audio Agent (passed from VGO)
and produces one SRT file per episode component plus a schema-validated
caption_package.

Responsibilities (owns):
  ✓ SRT construction from ElevenLabs word_timings (elevenlabs_alignment path)
  ✓ Detection of degraded alignment (whisper/none sources → whisper_fallback path)
  ✓ Whisper stitched-track fallback when primary alignment data is absent
  ✓ SRT file writes to output_dir/captions/
  ✓ Per-component caption_checks (content completeness)
  ✓ caption_package assembly and schema validation

Responsibilities (does not own):
  ✗ Voice generation          → voice_generation_operator.py
  ✗ Audio QA / production_ready → audio_qa.py
  ✗ Narration package assembly  → audio_agent.py
  ✗ Segment stitching           → voice_generation_operator.py
  ✗ Video assembly              → assembly_agent.py

Primary caption path (per component):
  1. Collect successful NarrationSegmentResult objects (non-None).
  2. If any segment has alignment_source in ("whisper", "none") → caption_method = "whisper_fallback".
     Otherwise → caption_method = "elevenlabs_alignment".
  3. Call build_srt() on successful segments to produce SRT content.
  4. If no SRT content was produced AND OPENAI_API_KEY is set AND stitched track exists:
       Run whisper_fallback() on the stitched audio. Set caption_method = "whisper_fallback".
  5. Write SRT file (may be empty string if all paths fail).
  6. Record caption entry and per-component check.

Public API:
    run_caption_agent(
        main_results, shorts_results, main_track_path, short_track_paths,
        config, episode_id, output_dir
    ) -> dict  (caption_package, schema-validated)

    caption_package structure:
      package_metadata        — caption_package_id, episode_id
      captions[]              — {caption_id, srt_asset_id, caption_method, has_content}
                                  one per component (1 main + 0–3 shorts)
      caption_checks[]        — per-component content check (pass / warn)

Logging events emitted:
  caption_agent_started         — info  — before processing, with component count
  caption_srt_generated         — info  — SRT file written, with caption_method, has_content
  caption_srt_whisper_fallback  — warn  — falling back to whisper on stitched track
  caption_srt_whisper_failed    — error — whisper on stitched track also failed
  caption_schema_warn           — warn  — caption_package validation issue (non-blocking)
  caption_agent_completed       — info  — all components processed
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from hypogeum.config import Config
from hypogeum.elevenlabs import (
    NarrationSegmentResult,
    build_srt,
    whisper_fallback,
)
from hypogeum.logger import get_logger
from hypogeum.schemas import SchemaError, validate


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "caption_agent"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_caption_agent(
    main_results: List[Optional[NarrationSegmentResult]],
    shorts_results: List[Tuple[str, List[Optional[NarrationSegmentResult]]]],
    main_track_path: Path,
    short_track_paths: List[Tuple[str, Path]],
    config: Config,
    episode_id: str,
    output_dir: Path,
) -> dict:
    """
    Generate SRT caption files for all episode components.

    Args:
        main_results:       Raw NarrationSegmentResult list for main episode beats
                            (None entries = failed beats).
        shorts_results:     [(short_id, [result | None]), ...] for each Short.
        main_track_path:    Path to the stitched main narration MP3 (for Whisper
                            fallback when alignment data is absent).
        short_track_paths:  [(short_id, Path), ...] for Short stitched tracks.
        config:             Loaded Config instance.
        episode_id:         Episode identifier for asset paths and log correlation.
        output_dir:         Root episode output directory. SRT files are written
                            to output_dir/captions/.

    Returns:
        caption_package dict (schema-validated).

    The returned caption_package.captions list is ready to be slotted directly
    into narration_package.captions. Extra field (has_content) is permitted by
    the narration_package schema's non-exhaustive field checks.
    """
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)
    captions_dir = output_dir / "captions"
    captions_dir.mkdir(parents=True, exist_ok=True)

    total_components = 1 + len(shorts_results)
    log.info(
        "caption_agent_started",
        episode_id=episode_id,
        total_components=total_components,
    )

    captions: List[dict] = []
    caption_checks: List[dict] = []

    # ------------------------------------------------------------------
    # Main episode component
    # ------------------------------------------------------------------
    main_srt_path, main_caption_method, main_has_content = _build_srt_for_component(
        results=main_results,
        output_path=captions_dir / "main.srt",
        config=config,
        episode_id=episode_id,
        component="main_episode",
        stitched_audio_path=main_track_path,
        log=log,
    )

    captions.append({
        "caption_id": f"cap_main_{episode_id}",
        "srt_asset_id": f"episodes/{episode_id}/captions/main",
        "caption_method": main_caption_method,
        "has_content": main_has_content,
    })
    caption_checks.append(_make_content_check("main_episode", main_has_content))

    # ------------------------------------------------------------------
    # Short components
    # ------------------------------------------------------------------
    for (short_id, track_path), (_, seg_results) in zip(short_track_paths, shorts_results):
        srt_path, caption_method, has_content = _build_srt_for_component(
            results=seg_results,
            output_path=captions_dir / f"sh_{short_id}.srt",
            config=config,
            episode_id=episode_id,
            component=short_id,
            stitched_audio_path=track_path,
            log=log,
        )
        captions.append({
            "caption_id": f"cap_{short_id}_{episode_id}",
            "srt_asset_id": f"episodes/{episode_id}/captions/sh_{short_id}",
            "caption_method": caption_method,
            "has_content": has_content,
        })
        caption_checks.append(_make_content_check(short_id, has_content))

    # ------------------------------------------------------------------
    # Assemble caption_package
    # ------------------------------------------------------------------
    caption_package: dict = {
        "package_metadata": {
            "caption_package_id": f"cp_{episode_id}",
            "episode_id": episode_id,
        },
        "captions": captions,
        "caption_checks": caption_checks,
    }

    try:
        validate("caption_package", caption_package)
    except SchemaError as exc:
        log.warn(
            "caption_schema_warn",
            episode_id=episode_id,
            path=exc.path,
            reason=exc.reason,
        )

    log.info(
        "caption_agent_completed",
        episode_id=episode_id,
        total_components=total_components,
        captions_with_content=sum(1 for c in captions if c["has_content"]),
    )

    return caption_package


# ---------------------------------------------------------------------------
# Internal: per-component check builder
# ---------------------------------------------------------------------------

def _make_content_check(component: str, has_content: bool) -> dict:
    """Build one caption_checks entry for a single component."""
    return {
        "check_id": f"caption_content_{component}",
        "component": component,
        "status": "pass" if has_content else "warn",
        "detail": (
            f"SRT content present for {component}."
            if has_content
            else f"SRT file for {component} is empty — no alignment data available."
        ),
    }


# ---------------------------------------------------------------------------
# Internal: SRT generation for one component
# ---------------------------------------------------------------------------

def _build_srt_for_component(
    results: List[Optional[NarrationSegmentResult]],
    output_path: Path,
    config: Config,
    episode_id: str,
    component: str,
    stitched_audio_path: Path,
    log,
) -> Tuple[Path, str, bool]:
    """
    Build and write an SRT caption file for one episode component.

    Primary path:
        Use word_timings from narration segments (tts or forced_alignment source).
        caption_method = "elevenlabs_alignment"

    Fallback path (any segment has whisper/none alignment source, or all
    segments have empty word_timings):
        Run whisper_fallback() on the stitched audio track.
        caption_method = "whisper_fallback"

    Returns (output_path, caption_method, has_content).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    successful = [r for r in results if r is not None]

    # Determine if any segment lacks reliable alignment
    has_degraded_alignment = any(
        r.alignment_source in ("whisper", "none") for r in successful
    )

    # Caption method is based on the primary alignment source
    caption_method = "whisper_fallback" if has_degraded_alignment else "elevenlabs_alignment"

    # Build SRT from segment alignment data (build_srt skips empty word_timings)
    srt_content = build_srt(successful) if successful else ""

    # If no SRT content was produced AND whisper is available, try stitched-track fallback
    if not srt_content and config.OPENAI_API_KEY:
        try:
            track_exists = (
                stitched_audio_path.exists()
                and stitched_audio_path.stat().st_size > 0
            )
        except OSError:
            track_exists = False

        if track_exists:
            log.warn(
                "caption_srt_whisper_fallback",
                component=component,
                reason="no_alignment_data_in_segments",
            )
            try:
                word_timings = whisper_fallback(
                    beat_id=f"{component}_stitched",
                    audio_path=stitched_audio_path,
                    config=config,
                    episode_id=episode_id,
                    agent=_AGENT_NAME,
                )
                if word_timings:
                    synthetic = NarrationSegmentResult(
                        beat_id=f"{component}_stitched",
                        asset_id=f"episodes/{episode_id}/narration/{component}_stitched",
                        local_path=stitched_audio_path,
                        duration_seconds=(
                            word_timings[-1].end_seconds if word_timings else 0.0
                        ),
                        request_id="",
                        character_cost=0,
                        word_timings=word_timings,
                        alignment_source="whisper",
                    )
                    srt_content = build_srt([synthetic])
                    caption_method = "whisper_fallback"
            except Exception as exc:
                log.error(
                    "caption_srt_whisper_failed",
                    component=component,
                    error=str(exc),
                )

    output_path.write_text(srt_content, encoding="utf-8")
    has_content = bool(srt_content)

    log.info(
        "caption_srt_generated",
        component=component,
        caption_method=caption_method,
        has_content=has_content,
        output_path=str(output_path),
    )

    return output_path, caption_method, has_content
