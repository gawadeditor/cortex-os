"""
hypogeum/agents/voice_generation_operator.py — Voice Generation Operator (Agent 38)

Sole authority for ElevenLabs TTS execution. Generates per-beat narration
audio, chains prosody across beats, stitches segments into narration tracks,
and returns a VoiceGenerationResult.

Inputs:
  main_beats     — flat list of main episode narration beat dicts
  shorts_info    — [(short_id, [beat_dicts]), ...] for each Short
  config         — Config instance (ElevenLabs credentials)
  episode_id     — for asset path construction and log correlation
  output_dir     — episode output directory root

Output (VoiceGenerationResult):
  narration_segments  — per-beat segment records (JSON-serializable)
  stitched_tracks     — {track_id, audio_asset_id, source_type} per component
  voice_configuration — {voice_id, model_id, environment_source}
  total_chars_billed  — sum of character costs across all segments
  main_results        — raw NarrationSegmentResult list (for caption layer)
  shorts_results      — [(short_id, [result | None]), ...] (for caption layer)
  short_ids           — ordered Short IDs
  short_track_paths   — [(short_id, Path), ...] stitched track paths

Responsibilities:
  ✓ Per-beat ElevenLabs generation
  ✓ Prosody chaining (up to 3 preceding request IDs per component)
  ✓ Segment file writing (via generate_narration_segment)
  ✓ Stitched narration track creation
  ✗ Caption / SRT generation  → audio_agent.py (until caption_agent.py created)
  ✗ Whisper fallback          → audio_agent.py
  ✗ Audio QA verdict          → audio_agent.py
  ✗ production_ready flag     → audio_agent.py

Halt conditions (propagated immediately to caller):
  ElevenLabsAuthFailure    — HTTP 401; no further generation possible
  ElevenLabsQuotaExhausted — credits exhausted; no further generation possible

Non-halt conditions (beat recorded as failed, generation continues):
  Any other exception during generate_narration_segment()

No Claude calls. No LLM.

Logging events:
  vgo_started             — info  — episode_id, beat counts
  vgo_beat_started        — info  — before each generate_narration_segment call
  vgo_beat_completed      — info  — beat generated successfully
  vgo_beat_failed         — warn  — non-halt failure; continuing to next beat
  vgo_stitching_started   — info  — before stitching each component
  vgo_stitching_completed — info  — stitched track written
  vgo_completed           — info  — all beats and tracks produced
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from hypogeum.config import Config
from hypogeum.elevenlabs import (
    ElevenLabsAuthFailure,
    ElevenLabsQuotaExhausted,
    NarrationSegmentResult,
    generate_narration_segment,
)
from hypogeum.logger import get_logger


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "voice_generation_operator"

# Maximum number of preceding request IDs to pass for prosody chaining
_PROSODY_CHAIN_DEPTH = 3


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class VoiceGenerationResult:
    """
    Result of run_voice_generation_operator().

    Contains both JSON-serializable data (narration_segments, stitched_tracks,
    voice_configuration) and raw Python objects needed by the caption layer
    (main_results, shorts_results, short_track_paths).
    """
    narration_segments: List[dict]
    """Per-beat segment records — JSON-serializable."""

    stitched_tracks: List[dict]
    """[{track_id, audio_asset_id, source_type}] per component."""

    voice_configuration: dict
    """{"voice_id": ..., "model_id": ..., "environment_source": ...}"""

    total_chars_billed: int
    """Sum of character_cost across all successfully generated segments."""

    main_results: List[Optional[NarrationSegmentResult]]
    """Raw results for main episode beats; None for failed beats."""

    shorts_results: List[Tuple[str, List[Optional[NarrationSegmentResult]]]]
    """[(short_id, [result | None]), ...] for caption layer."""

    short_ids: List[str]
    """Ordered list of Short IDs that were processed."""

    short_track_paths: List[Tuple[str, Path]]
    """[(short_id, stitched_track_path), ...] for caption layer."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_voice_generation_operator(
    main_beats: List[dict],
    shorts_info: List[Tuple[str, List[dict]]],
    config: Config,
    episode_id: str,
    output_dir: Path,
    channel_id: Optional[str] = None,
) -> VoiceGenerationResult:
    """
    Generate narration audio for all beats and stitch component tracks.

    Args:
        main_beats:   Flat list of main episode narration beat dicts.
                      Each beat: {"beat_id": str, "text": str, ...}
        shorts_info:  [(short_id, [beat_dicts]), ...] for each Short.
                      Prosody chain resets at the start of each Short.
        config:       Loaded Config instance (ElevenLabs credentials).
        episode_id:   Episode identifier for asset paths and log correlation.
        output_dir:   Root episode output directory. Narration segments are
                      written to output_dir/narration/.

    Returns:
        VoiceGenerationResult with generated segments, stitched tracks, and
        raw results for the caption layer.

    Raises:
        ElevenLabsAuthFailure    — HTTP 401; propagated immediately.
        ElevenLabsQuotaExhausted — Credit quota exhausted; propagated immediately.
    """
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    short_ids = [sid for sid, _ in shorts_info]
    total_beats = len(main_beats) + sum(len(b) for _, b in shorts_info)

    log.info(
        "vgo_started",
        episode_id=episode_id,
        main_beats=len(main_beats),
        shorts_count=len(shorts_info),
        total_beats=total_beats,
    )

    # ------------------------------------------------------------------
    # Generate main episode beats (prosody chain spans all main beats)
    # ------------------------------------------------------------------
    main_results: List[Optional[NarrationSegmentResult]] = []
    prev_ids: List[str] = []

    for beat in main_beats:
        result = _generate_beat_safe(
            beat=beat,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            previous_request_ids=prev_ids,
            log=log,
            channel_id=channel_id,
        )
        main_results.append(result)
        if result is not None:
            prev_ids = (prev_ids + [result.request_id])[-_PROSODY_CHAIN_DEPTH:]

    # ------------------------------------------------------------------
    # Generate Short beats (prosody chain resets per Short)
    # ------------------------------------------------------------------
    shorts_results: List[Tuple[str, List[Optional[NarrationSegmentResult]]]] = []

    for short_id, beats in shorts_info:
        short_segment_results: List[Optional[NarrationSegmentResult]] = []
        prev_ids = []

        for beat in beats:
            result = _generate_beat_safe(
                beat=beat,
                config=config,
                episode_id=episode_id,
                output_dir=output_dir,
                previous_request_ids=prev_ids,
                log=log,
            )
            short_segment_results.append(result)
            if result is not None:
                prev_ids = (prev_ids + [result.request_id])[-_PROSODY_CHAIN_DEPTH:]

        shorts_results.append((short_id, short_segment_results))

    # ------------------------------------------------------------------
    # Stitch tracks
    # ------------------------------------------------------------------
    narration_dir = output_dir / "narration"
    narration_dir.mkdir(parents=True, exist_ok=True)

    main_track_path = _stitch_segments(
        results=[r for r in main_results if r is not None],
        output_path=narration_dir / "main_narration.mp3",
        component="main_episode",
        log=log,
    )

    short_track_paths: List[Tuple[str, Path]] = []
    for short_id, seg_results in shorts_results:
        track_path = _stitch_segments(
            results=[r for r in seg_results if r is not None],
            output_path=narration_dir / f"sh_{short_id}_narration.mp3",
            component=short_id,
            log=log,
        )
        short_track_paths.append((short_id, track_path))

    # ------------------------------------------------------------------
    # Build narration_segments[] records
    # ------------------------------------------------------------------
    narration_segments: List[dict] = []

    for beat, result in zip(main_beats, main_results):
        narration_segments.append(
            _make_segment_record(beat=beat, result=result, source_type="main_episode")
        )

    for (short_id, beats), (_, seg_results) in zip(shorts_info, shorts_results):
        for beat, result in zip(beats, seg_results):
            narration_segments.append(
                _make_segment_record(beat=beat, result=result, source_type="short")
            )

    # ------------------------------------------------------------------
    # Build stitched_tracks[] records
    # ------------------------------------------------------------------
    stitched_tracks: List[dict] = [
        {
            "track_id": f"track_main_{episode_id}",
            "audio_asset_id": f"episodes/{episode_id}/narration/main_narration",
            "source_type": "main_episode",
        }
    ] + [
        {
            "track_id": f"track_{short_id}_{episode_id}",
            "audio_asset_id": f"episodes/{episode_id}/narration/sh_{short_id}_narration",
            "source_type": "short",
        }
        for short_id in short_ids
    ]

    # ------------------------------------------------------------------
    # Compute total characters billed
    # ------------------------------------------------------------------
    total_chars_billed = sum(
        r.character_cost
        for results_list in ([main_results] + [sr for _, sr in shorts_results])
        for r in results_list
        if r is not None
    )

    voice_configuration = {
        "voice_id": config.ELEVENLABS_VOICE_ID,
        "model_id": config.ELEVENLABS_MODEL_ID,
        "environment_source": "ELEVENLABS_VOICE_ID",
    }

    log.info(
        "vgo_completed",
        episode_id=episode_id,
        segments_generated=sum(1 for s in narration_segments if s["generation_status"] == "generated"),
        segments_failed=sum(1 for s in narration_segments if s["generation_status"] == "failed"),
        tracks_stitched=1 + len(short_track_paths),
        total_chars_billed=total_chars_billed,
    )

    return VoiceGenerationResult(
        narration_segments=narration_segments,
        stitched_tracks=stitched_tracks,
        voice_configuration=voice_configuration,
        total_chars_billed=total_chars_billed,
        main_results=main_results,
        shorts_results=shorts_results,
        short_ids=short_ids,
        short_track_paths=short_track_paths,
    )


# ---------------------------------------------------------------------------
# Internal: beat generation with error isolation
# ---------------------------------------------------------------------------

def _generate_beat_safe(
    beat: dict,
    config: Config,
    episode_id: str,
    output_dir: Path,
    previous_request_ids: List[str],
    log,
    channel_id: Optional[str] = None,
) -> Optional[NarrationSegmentResult]:
    """
    Attempt to generate narration for one beat.

    Returns NarrationSegmentResult on success.
    Returns None on non-halt failure (logs warning, continues pipeline).
    Propagates ElevenLabsAuthFailure and ElevenLabsQuotaExhausted immediately.
    """
    beat_id = beat.get("beat_id", "")
    text = beat.get("text", "")

    log.info(
        "vgo_beat_started",
        beat_id=beat_id,
        character_count=len(text),
    )

    try:
        result = generate_narration_segment(
            beat_id=beat_id,
            text=text,
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            previous_request_ids=previous_request_ids or None,
            agent=_AGENT_NAME,
            channel_id=channel_id,
        )
        log.info(
            "vgo_beat_completed",
            beat_id=beat_id,
            duration_seconds=round(result.duration_seconds, 3),
            alignment_source=result.alignment_source,
            character_cost=result.character_cost,
        )
        return result

    except (ElevenLabsAuthFailure, ElevenLabsQuotaExhausted):
        raise  # halt conditions — propagate immediately

    except Exception as exc:
        log.warn(
            "vgo_beat_failed",
            beat_id=beat_id,
            error=str(exc),
        )
        return None


# ---------------------------------------------------------------------------
# Internal: segment stitching (MP3 byte concatenation)
# ---------------------------------------------------------------------------

def _stitch_segments(
    results: List[NarrationSegmentResult],
    output_path: Path,
    component: str,
    log,
) -> Path:
    """
    Stitch MP3 segment files into a single narration track via ffmpeg acrossfade.

    Uses a 30ms crossfade (acrossfade) between consecutive segments to eliminate
    the audible dead-air gaps (0.4–0.8s) that raw byte concatenation introduces at
    MP3 frame boundaries. Falls back to raw byte concatenation if ffmpeg fails.

    Writes an empty file if no valid segments are provided.
    Always returns output_path.
    """
    valid = [r for r in results if r.local_path.exists()]

    log.info(
        "vgo_stitching_started",
        component=component,
        segment_count=len(results),
        valid_count=len(valid),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not valid:
        output_path.write_bytes(b"")
        log.info(
            "vgo_stitching_completed",
            component=component,
            output_path=str(output_path),
            total_bytes=0,
            segments_included=0,
        )
        return output_path

    if len(valid) == 1:
        output_path.write_bytes(valid[0].local_path.read_bytes())
        log.info(
            "vgo_stitching_completed",
            component=component,
            output_path=str(output_path),
            total_bytes=output_path.stat().st_size,
            segments_included=1,
        )
        return output_path

    # Build ffmpeg acrossfade chain: 30ms crossfade eliminates dead-air at beat boundaries.
    _XFADE_DUR = 0.030  # 30ms — long enough to bridge gaps, short enough to be inaudible
    cmd: List[str] = ["ffmpeg", "-y"]
    for r in valid:
        cmd += ["-i", str(r.local_path)]

    # Chain: [0:a][1:a]acrossfade...[a1]; [a1][2:a]acrossfade...[a2]; ... [aout]
    filter_parts = []
    prev_label = "[0:a]"
    for i in range(1, len(valid)):
        is_last = (i == len(valid) - 1)
        next_label = "[aout]" if is_last else f"[a{i}]"
        filter_parts.append(
            f"{prev_label}[{i}:a]acrossfade=d={_XFADE_DUR}:c1=tri:c2=tri{next_label}"
        )
        prev_label = next_label

    cmd += [
        "-filter_complex", ";".join(filter_parts),
        "-map", "[aout]",
        "-q:a", "4",   # VBR ~165 kbps — matches ElevenLabs MP3 output quality
        str(output_path),
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg acrossfade exit {proc.returncode}: {proc.stderr[-500:]}"
            )
    except Exception as exc:
        log.warn(
            "vgo_stitching_acrossfade_fallback",
            component=component,
            error=str(exc),
        )
        # Fallback: raw byte concatenation
        all_bytes = bytearray()
        for r in valid:
            all_bytes.extend(r.local_path.read_bytes())
        output_path.write_bytes(bytes(all_bytes))

    log.info(
        "vgo_stitching_completed",
        component=component,
        output_path=str(output_path),
        total_bytes=output_path.stat().st_size if output_path.exists() else 0,
        segments_included=len(valid),
    )
    return output_path


# ---------------------------------------------------------------------------
# Internal: segment record builder
# ---------------------------------------------------------------------------

def _make_segment_record(
    beat: dict,
    result: Optional[NarrationSegmentResult],
    source_type: str,
) -> dict:
    """
    Build one narration_segments[] entry compatible with narration_package schema.

    Only the fields required by the narration_package schema are mandatory;
    additional diagnostic fields are included for downstream traceability.
    """
    beat_id = beat.get("beat_id", "")

    if result is not None:
        return {
            "segment_id": f"seg_{beat_id}",
            "beat_reference": beat_id,
            "source_type": source_type,
            "generation_status": "generated",
            "audio_asset_id": result.asset_id,
            "duration_seconds": round(result.duration_seconds, 3),
            "character_cost": result.character_cost,
            "alignment_source": result.alignment_source,
            "word_count": len(result.word_timings),
        }
    else:
        return {
            "segment_id": f"seg_{beat_id}",
            "beat_reference": beat_id,
            "source_type": source_type,
            "generation_status": "failed",
            "audio_asset_id": None,
            "duration_seconds": 0.0,
            "character_cost": 0,
            "alignment_source": "none",
            "word_count": 0,
        }
