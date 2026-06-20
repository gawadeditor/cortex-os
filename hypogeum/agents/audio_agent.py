"""
hypogeum/agents/audio_agent.py — Audio Agent (M3.2)

Orchestrates narration production from Script Package. Delegates:
  - Voice generation → voice_generation_operator.py
  - Caption / SRT generation → caption_agent.py
  - Audio QA → audio_qa.py

Retains ownership of:
  - Config validation
  - Beat extraction from script_package
  - Final narration_package assembly and disk write

Execution sequence:
  1. Validate config — ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID must be set.
  2. Extract main episode beats from script_package.sections[].narration_beats[].
  3. Extract Short beats from script_package.shorts_scripts[].narration_beats[].
  4. Delegate voice generation to Voice Generation Operator (Agent 38).
  5. Delegate caption generation to Caption Agent.
  6. Delegate audio QA to Audio QA Agent.
  7. Build and validate narration_package dict.
  8. Write narration_package_{episode_id}.json to disk.

Package structure:
  package_metadata         — narration_package_id, episode_id
  voice_configuration      — voice_id, model_id, environment_source
  narration_segments[]     — one record per beat (main + all Shorts, or main only)
  stitched_tracks[1–4]     — 1 main_episode + 0–3 short tracks
  captions[1–4]            — 1 main_episode + 0–3 short SRT references
  audio_qa_report          — overall_status, beat counts, failure_rate, chars_billed
  package_integrity        — production_ready (True if ≤ 10% of beats failed)

Public API:
    run_audio_agent(script_package, config, episode_id, output_dir=None,
                    skip_shorts=False)
        → dict  (Narration Package, schema-validated)

    skip_shorts=True: suppress all Short narration generation (Episode Zero /
        validation runs where VPD set generation_constraints.shorts_count=0).
        Short tracks, SRT files, and narration_segments for Shorts are all
        omitted from the output package.

Integration contracts (must not be violated):
  - All ElevenLabs calls through voice_generation_operator — no direct TTS here
  - All config access through Config — never os.getenv()
  - All logging through get_logger() — never print() or stdlib logging
  - Output validated through schemas.py validate()

Logging events emitted:
  audio_agent_started        — info  — before VGO call, with beat counts
  audio_schema_warn          — warn  — narration_package validation issue (non-blocking)
  audio_package_finalized    — info  — package written to disk
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hypogeum.agents.audio_qa import run_audio_qa
from hypogeum.agents.caption_agent import run_caption_agent
from hypogeum.agents.voice_generation_operator import (
    _generate_beat_safe,
    _make_segment_record,
    _stitch_segments,
    run_voice_generation_operator,
)
from hypogeum.config import Config
from hypogeum.elevenlabs import NarrationSegmentResult
from hypogeum.logger import get_logger
from hypogeum.schemas import SchemaError, validate


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "audio_agent"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AudioAgentError(RuntimeError):
    """Raised for Audio Agent configuration or orchestration errors."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_audio_agent(
    script_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path] = None,
    *,
    skip_shorts: bool = False,
) -> dict:
    """
    Execute the Audio Agent end-to-end.

    Reads the Script Package, generates ElevenLabs narration for every beat,
    stitches segments into tracks, builds SRT captions, and returns a
    schema-validated Narration Package.

    Args:
        script_package: Validated Script Package dict from Script Engine.
        config:         Loaded Config instance.
        episode_id:     Episode identifier for asset paths and log correlation.
        output_dir:     Optional output directory override (defaults to
                        .hypogeum-assets/episodes/{episode_id}/).
        skip_shorts:    When True, suppress all Short narration generation.
                        Use when VPD generation_constraints.shorts_count=0
                        (Episode Zero / validation runs). Short tracks, SRT
                        files, and narration_segments for Shorts are omitted.

    Returns:
        Narration Package dict (schema-validated, written to disk).

    Raises:
        AudioAgentError          — Missing ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID.
        ElevenLabsAuthFailure    — HTTP 401 during generation (halts immediately).
        ElevenLabsQuotaExhausted — Credit quota exhausted (halts immediately).
    """
    _resolve_output_dir: Path = output_dir or (
        Path(__file__).resolve().parent.parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
    )
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    # ------------------------------------------------------------------
    # Config validation — must fail loudly before the first API call
    # ------------------------------------------------------------------
    if not config.ELEVENLABS_API_KEY:
        raise AudioAgentError(
            "ELEVENLABS_API_KEY is not set. "
            "Audio Agent requires ElevenLabs credentials before the first beat."
        )
    if not config.ELEVENLABS_VOICE_ID:
        raise AudioAgentError(
            "ELEVENLABS_VOICE_ID is not set. "
            "This is the permanent channel voice identity lock — "
            "set it before the first production run."
        )

    # ------------------------------------------------------------------
    # Extract beats
    # ------------------------------------------------------------------
    main_beats = _collect_main_beats(script_package)

    if config.SMOKE_TEST_MAX_NARRATION_BEATS > 0:
        original_count = len(main_beats)
        main_beats = main_beats[:config.SMOKE_TEST_MAX_NARRATION_BEATS]
        log.info(
            "audio_smoke_beat_cap_applied",
            original_count=original_count,
            capped_to=config.SMOKE_TEST_MAX_NARRATION_BEATS,
        )

    # M5.6 — Episode Zero: skip Short narration when shorts_count=0 was set in VPD
    if skip_shorts:
        shorts_info: List[Tuple[str, List[dict]]] = []
        log.info("audio_shorts_skipped", episode_id=episode_id, reason="skip_shorts=True")
    else:
        shorts_info = _collect_shorts_beats(script_package)

    total_beats = len(main_beats) + sum(len(b) for _, b in shorts_info)

    log.info(
        "audio_agent_started",
        episode_id=episode_id,
        main_beats=len(main_beats),
        shorts_count=len(shorts_info),
        total_beats=total_beats,
        voice_id=config.ELEVENLABS_VOICE_ID,
        model_id=config.ELEVENLABS_MODEL_ID,
    )

    # ------------------------------------------------------------------
    # Voice generation — delegated to Voice Generation Operator (Agent 38)
    # Audio Agent must not call generate_narration_segment directly.
    # ------------------------------------------------------------------
    vgo = run_voice_generation_operator(
        main_beats=main_beats,
        shorts_info=shorts_info,
        config=config,
        episode_id=episode_id,
        output_dir=_resolve_output_dir,
    )

    # Unpack VGO result for local use
    narration_segments = vgo.narration_segments
    stitched_tracks = vgo.stitched_tracks
    main_results = vgo.main_results
    shorts_results = vgo.shorts_results
    short_track_paths = vgo.short_track_paths
    main_track_path = (
        _resolve_output_dir / "narration" / "main_narration.mp3"
    )

    # ------------------------------------------------------------------
    # Caption generation — delegated to Caption Agent
    # ------------------------------------------------------------------
    caption_pkg = run_caption_agent(
        main_results=main_results,
        shorts_results=shorts_results,
        main_track_path=main_track_path,
        short_track_paths=short_track_paths,
        config=config,
        episode_id=episode_id,
        output_dir=_resolve_output_dir,
    )

    # ------------------------------------------------------------------
    # Audio QA — delegated to Audio QA Agent
    # ------------------------------------------------------------------
    qa_verdict = run_audio_qa(
        narration_segments=narration_segments,
        total_chars_billed=vgo.total_chars_billed,
        episode_id=episode_id,
    )

    qa_status = qa_verdict["overall_status"]
    production_ready: bool = qa_verdict["production_ready"]
    failed_count: int = qa_verdict["beats_failed"]
    total_count: int = qa_verdict["total_beats"]
    total_chars_billed: int = qa_verdict["total_characters_billed"]
    fail_rate: float = qa_verdict["failure_rate"]

    # ------------------------------------------------------------------
    # Assemble Narration Package
    # ------------------------------------------------------------------
    narration_package: dict = {
        "package_metadata": {
            "narration_package_id": f"np_{episode_id}",
            "episode_id": episode_id,
        },
        "voice_configuration": {
            "voice_id": config.ELEVENLABS_VOICE_ID,
            "model_id": config.ELEVENLABS_MODEL_ID,
            "environment_source": "ELEVENLABS_VOICE_ID",
        },
        "narration_segments": narration_segments,
        "stitched_tracks": stitched_tracks,
        "captions": caption_pkg["captions"],
        "audio_qa_report": {
            "overall_status": qa_status,
            "total_beats": total_count,
            "beats_generated": total_count - failed_count,
            "beats_failed": failed_count,
            "failure_rate": round(fail_rate, 4),
            "total_characters_billed": total_chars_billed,
        },
        "package_integrity": {
            "production_ready": production_ready,
        },
    }

    try:
        validate("narration_package", narration_package)
    except SchemaError as exc:
        log.warn(
            "audio_schema_warn",
            path=exc.path,
            reason=exc.reason,
        )

    out_path = _resolve_output_dir / f"narration_package_{episode_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(narration_package, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log.info(
        "audio_package_finalized",
        narration_package_id=narration_package["package_metadata"]["narration_package_id"],
        total_beats=total_count,
        beats_generated=total_count - failed_count,
        beats_failed=failed_count,
        production_ready=production_ready,
        output_path=str(out_path),
    )

    return narration_package


# ---------------------------------------------------------------------------
# Internal: beat extraction
# ---------------------------------------------------------------------------

def _collect_main_beats(script_package: dict) -> List[dict]:
    """
    Extract all main episode narration beats in section order.
    Returns a flat list — sections and their narration_beats flattened.
    """
    beats: List[dict] = []
    for section in script_package.get("sections", []):
        for beat in section.get("narration_beats", []):
            beats.append(beat)
    return beats


def _collect_shorts_beats(
    script_package: dict,
) -> List[Tuple[str, List[dict]]]:
    """
    Extract Short narration beats.
    Returns [(short_id, [beat_dicts]), ...] for each Short in script order.
    """
    result: List[Tuple[str, List[dict]]] = []
    for short in script_package.get("shorts_scripts", []):
        short_id = short.get("short_id", "")
        beats = list(short.get("narration_beats", []))
        result.append((short_id, beats))
    return result


# ---------------------------------------------------------------------------
# Resume API — audio-only partial re-run
# ---------------------------------------------------------------------------

def resume_narration_agent(
    script_package: dict,
    existing_narration_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path] = None,
    *,
    skip_shorts: bool = True,
) -> dict:
    """
    Resume narration generation for an episode with a partial narration_package.

    Identifies beats from script_package that already have audio files on disk
    (using existing_narration_package as the checkpoint manifest) and skips them.
    Generates only the missing beats via ElevenLabs, then re-stitches the full
    narration track from all beats (existing + newly generated) and regenerates
    captions and audio QA.

    Preserves all existing per-beat .mp3 files. Never overwrites a beat that
    is already generated and whose file is present on disk.

    Args:
        script_package:             Validated Script Package (source of truth for beats).
        existing_narration_package: Partial narration_package already on disk — used as
                                    checkpoint manifest.  May have 0 to N beats present.
        config:                     Loaded Config instance.
        episode_id:                 Episode identifier for asset paths and log correlation.
        output_dir:                 Optional output directory override (defaults to
                                    .hypogeum-assets/episodes/{episode_id}/).
        skip_shorts:                When True, suppress Short narration.  Defaults True
                                    for resume runs to match the original episode config.

    Returns:
        Complete narration_package dict (schema-validated, written to disk) with
        all beats from script_package represented.

    Raises:
        AudioAgentError          — Missing ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID.
        ElevenLabsAuthFailure    — HTTP 401 during generation (halts immediately).
        ElevenLabsQuotaExhausted — Credit quota exhausted (halts immediately).
    """
    _resolve_output_dir: Path = output_dir or (
        Path(__file__).resolve().parent.parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
    )
    # assets_root is two levels above output_dir: .hypogeum-assets/
    _assets_root: Path = _resolve_output_dir.parent.parent

    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    if not config.ELEVENLABS_API_KEY:
        raise AudioAgentError(
            "ELEVENLABS_API_KEY is not set. "
            "Audio Agent requires ElevenLabs credentials before the first beat."
        )
    if not config.ELEVENLABS_VOICE_ID:
        raise AudioAgentError(
            "ELEVENLABS_VOICE_ID is not set. "
            "This is the permanent channel voice identity lock — "
            "set it before the first production run."
        )

    # ------------------------------------------------------------------
    # Build checkpoint lookup: beat_id → existing segment dict
    # ------------------------------------------------------------------
    existing_segments: Dict[str, dict] = {
        seg["beat_reference"]: seg
        for seg in existing_narration_package.get("narration_segments", [])
        if seg.get("generation_status") == "generated" and seg.get("beat_reference")
    }

    # ------------------------------------------------------------------
    # Extract beats from script
    # ------------------------------------------------------------------
    main_beats = _collect_main_beats(script_package)

    if not skip_shorts:
        shorts_info = _collect_shorts_beats(script_package)
    else:
        shorts_info = []

    total_beats = len(main_beats) + sum(len(b) for _, b in shorts_info)

    skipped = [
        b["beat_id"] for b in main_beats
        if _beat_file_exists(b["beat_id"], episode_id, _resolve_output_dir, existing_segments)
    ]
    to_generate = [
        b["beat_id"] for b in main_beats
        if not _beat_file_exists(b["beat_id"], episode_id, _resolve_output_dir, existing_segments)
    ]

    log.info(
        "audio_resume_started",
        episode_id=episode_id,
        total_main_beats=len(main_beats),
        beats_already_present=len(skipped),
        beats_to_generate=len(to_generate),
        skip_shorts=skip_shorts,
    )

    # ------------------------------------------------------------------
    # Generate main episode beats — skip existing, generate missing
    # ------------------------------------------------------------------
    main_results: List[Optional[NarrationSegmentResult]] = []
    prev_ids: List[str] = []

    for beat in main_beats:
        beat_id = beat["beat_id"]
        if _beat_file_exists(beat_id, episode_id, _resolve_output_dir, existing_segments):
            # Re-use existing audio — build stub from checkpoint data
            stub = _build_existing_stub(
                beat_id=beat_id,
                episode_id=episode_id,
                output_dir=_resolve_output_dir,
                existing_seg=existing_segments[beat_id],
            )
            main_results.append(stub)
            log.info(
                "audio_resume_beat_skipped",
                beat_id=beat_id,
                reason="file_present_on_disk",
                duration_seconds=stub.duration_seconds,
            )
            # Don't push stub request_id into prev_ids — it's empty.
            # Prosody chain resets at first new beat (acceptable trade-off for resume).
        else:
            result = _generate_beat_safe(
                beat=beat,
                config=config,
                episode_id=episode_id,
                output_dir=_resolve_output_dir,
                previous_request_ids=prev_ids,
                log=log,
            )
            main_results.append(result)
            if result is not None:
                prev_ids = (prev_ids + [result.request_id])[-3:]

    # ------------------------------------------------------------------
    # Generate Short beats (only if not skipped)
    # ------------------------------------------------------------------
    shorts_results: List[Tuple[str, List[Optional[NarrationSegmentResult]]]] = []
    for short_id, beats in shorts_info:
        short_segment_results: List[Optional[NarrationSegmentResult]] = []
        short_prev_ids: List[str] = []
        for beat in beats:
            beat_id = beat["beat_id"]
            if _beat_file_exists(beat_id, episode_id, _resolve_output_dir, existing_segments):
                stub = _build_existing_stub(
                    beat_id=beat_id,
                    episode_id=episode_id,
                    output_dir=_resolve_output_dir,
                    existing_seg=existing_segments.get(beat_id, {}),
                )
                short_segment_results.append(stub)
            else:
                result = _generate_beat_safe(
                    beat=beat,
                    config=config,
                    episode_id=episode_id,
                    output_dir=_resolve_output_dir,
                    previous_request_ids=short_prev_ids,
                    log=log,
                )
                short_segment_results.append(result)
                if result is not None:
                    short_prev_ids = (short_prev_ids + [result.request_id])[-3:]
        shorts_results.append((short_id, short_segment_results))

    short_ids = [sid for sid, _ in shorts_results]

    # ------------------------------------------------------------------
    # Re-stitch full narration track from all beats (existing + new)
    # ------------------------------------------------------------------
    narration_dir = _resolve_output_dir / "narration"
    narration_dir.mkdir(parents=True, exist_ok=True)

    successful_main = [r for r in main_results if r is not None]
    main_track_path = _stitch_segments(
        results=successful_main,
        output_path=narration_dir / "main_narration.mp3",
        component="main_episode",
        log=log,
    )

    short_track_paths: List[Tuple[str, Path]] = []
    for short_id, seg_results in shorts_results:
        successful_short = [r for r in seg_results if r is not None]
        track_path = _stitch_segments(
            results=successful_short,
            output_path=narration_dir / f"sh_{short_id}_narration.mp3",
            component=short_id,
            log=log,
        )
        short_track_paths.append((short_id, track_path))

    # ------------------------------------------------------------------
    # Rebuild narration_segments records (all beats)
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
    # Caption generation — existing stubs have no word_timings so their
    # duration is accumulated into the time offset but no SRT lines are
    # emitted for them. New beats contribute full SRT entries.
    # ------------------------------------------------------------------
    caption_pkg = run_caption_agent(
        main_results=main_results,
        shorts_results=shorts_results,
        main_track_path=main_track_path,
        short_track_paths=short_track_paths,
        config=config,
        episode_id=episode_id,
        output_dir=_resolve_output_dir,
    )

    # ------------------------------------------------------------------
    # Audio QA — evaluates narration_segments from this run
    # ------------------------------------------------------------------
    total_chars_billed = sum(
        (r.character_cost if r is not None else 0)
        for r in main_results
        + [r for _, seg_results in shorts_results for r in seg_results]
    )
    qa_verdict = run_audio_qa(
        narration_segments=narration_segments,
        total_chars_billed=total_chars_billed,
        episode_id=episode_id,
    )

    qa_status = qa_verdict["overall_status"]
    production_ready: bool = qa_verdict["production_ready"]
    failed_count: int = qa_verdict["beats_failed"]
    total_count: int = qa_verdict["total_beats"]
    fail_rate: float = qa_verdict["failure_rate"]

    # ------------------------------------------------------------------
    # Assemble and write updated narration_package
    # ------------------------------------------------------------------
    narration_package: dict = {
        "package_metadata": {
            "narration_package_id": f"np_{episode_id}",
            "episode_id": episode_id,
        },
        "voice_configuration": {
            "voice_id": config.ELEVENLABS_VOICE_ID,
            "model_id": config.ELEVENLABS_MODEL_ID,
            "environment_source": "ELEVENLABS_VOICE_ID",
        },
        "narration_segments": narration_segments,
        "stitched_tracks": stitched_tracks,
        "captions": caption_pkg["captions"],
        "audio_qa_report": {
            "overall_status": qa_status,
            "total_beats": total_count,
            "beats_generated": total_count - failed_count,
            "beats_failed": failed_count,
            "failure_rate": round(fail_rate, 4),
            "total_characters_billed": total_chars_billed,
        },
        "package_integrity": {
            "production_ready": production_ready,
        },
    }

    try:
        validate("narration_package", narration_package)
    except SchemaError as exc:
        log.warn("audio_schema_warn", path=exc.path, reason=exc.reason)

    out_path = _resolve_output_dir / f"narration_package_{episode_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(narration_package, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log.info(
        "audio_resume_completed",
        episode_id=episode_id,
        beats_skipped=len(skipped),
        beats_generated=len(to_generate),
        total_beats=total_count,
        beats_failed=failed_count,
        production_ready=production_ready,
        output_path=str(out_path),
    )

    return narration_package


# ---------------------------------------------------------------------------
# Internal helpers for resume
# ---------------------------------------------------------------------------

def _beat_file_exists(
    beat_id: str,
    episode_id: str,
    output_dir: Path,
    existing_segments: Dict[str, dict],
) -> bool:
    """
    Return True if this beat already has an audio file on disk AND is recorded
    in the existing narration_package as successfully generated.

    Both conditions must hold: the package record prevents re-using a stale
    file from a prior failed run, and the disk check guards against a package
    record that points to a deleted file.
    """
    if beat_id not in existing_segments:
        return False
    beat_path = output_dir / "narration" / f"{beat_id}.mp3"
    return beat_path.exists() and beat_path.stat().st_size > 0


def _build_existing_stub(
    beat_id: str,
    episode_id: str,
    output_dir: Path,
    existing_seg: dict,
) -> NarrationSegmentResult:
    """
    Build a NarrationSegmentResult stub for a beat that already exists on disk.

    The stub carries the real duration (needed for correct SRT time offsets in
    build_srt) but has empty word_timings (ElevenLabs alignment data was not
    persisted). build_srt skips segments with empty word_timings but still
    accumulates their duration, so subsequent beats get correct timestamps.
    """
    local_path = output_dir / "narration" / f"{beat_id}.mp3"
    return NarrationSegmentResult(
        beat_id=beat_id,
        asset_id=f"episodes/{episode_id}/narration/{beat_id}",
        local_path=local_path,
        duration_seconds=float(existing_seg.get("duration_seconds", 0.0)),
        request_id="",          # lost — prosody chain resets at first new beat
        character_cost=int(existing_seg.get("character_cost", 0)),
        word_timings=[],        # lost — captions skipped for this beat
        alignment_source="none",
    )

