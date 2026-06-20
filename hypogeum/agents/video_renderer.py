"""
hypogeum/agents/video_renderer.py — Video Renderer (Priority 16 extraction)

Extracted from assembly_agent.py.

Owns:
- ffmpeg command construction
- concat/render execution
- tpad hold application (Strategy B: freeze last frame for narration hold)
- audio mix execution (narration 0dB, music −18dB)
- main/short render file creation
- render_output_manifest construction

Does NOT own:
- Timeline construction (timeline_builder owns this)
- Music sourcing (music_agent owns this)
- Asset inventory validation (assembly_agent orchestration)
- production_ready verdict (final_render_reviewer owns this)
- Final Render Package assembly (assembly_agent owns this)

Multi-channel note:
- MAIN_RESOLUTION and SHORT_RESOLUTION are public constants so channel adapters
  (TikTok, Instagram Reels) can override resolution without touching ffmpeg logic.
- New channel formats subclass or wrap render_episode() with different resolution args.

Public API:
    render_episode(
        timeline_plan, main_narration_path, short_narration_paths,
        music_tracks, clip_availability_rate, missing_clips,
        sections_without_music, episode_dir, episode_id,
        ffmpeg_timeout, log,
    ) -> dict  (render_output_manifest)

Public constants (imported by assembly_agent for Final Render Package field values):
    MAIN_RESOLUTION  = "1920x1080"
    SHORT_RESOLUTION = "1080x1920"
    FRAME_RATE       = 30
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hypogeum.agents.timeline_builder import TimelineEntry, TimelinePlan, sum_timeline_duration


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

MAIN_RESOLUTION  = "1920x1080"
SHORT_RESOLUTION = "1080x1920"
FRAME_RATE       = 30


# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_VIDEO_CODEC           = "libx264"
_AUDIO_CODEC           = "aac"
_AUDIO_BITRATE         = "128k"
_CRF                   = "23"
_MUSIC_AMPLITUDE_FACTOR = 0.126   # ≈ −18 dB beneath narration
_DEFAULT_TIMEOUT        = 600     # seconds


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_episode(
    timeline_plan: TimelinePlan,
    main_narration_path: Optional[Path],
    short_narration_paths: Dict[str, Optional[Path]],
    music_tracks: dict,
    clip_availability_rate: float,
    missing_clips: List[str],
    sections_without_music: List[str],
    episode_dir: Path,
    episode_id: str,
    ffmpeg_timeout: int,
    log,
) -> dict:
    """
    Execute all ffmpeg renders for one episode and return a render_output_manifest.

    Renders the main episode (16:9) and all Shorts (9:16) from the supplied
    TimelinePlan. Applies narration-anchored sync with tpad freeze holds where
    the narration beat is longer than the clip. Mixes narration + music (−18 dB).

    Args:
        timeline_plan:          TimelinePlan from build_timelines().
        main_narration_path:    Path to the main episode stitched narration mp3.
        short_narration_paths:  {short_id: Path | None} for each Short.
        music_tracks:           {section_key: MusicTrackResult | None}.
        clip_availability_rate: Pre-computed fraction of clips present (for manifest).
        missing_clips:          Scene ids of missing clips (for manifest).
        sections_without_music: Section keys with no music track (for manifest).
        episode_dir:            Output directory for rendered mp4 files.
        episode_id:             Episode identifier for file naming and logging.
        ffmpeg_timeout:         subprocess timeout in seconds.
        log:                    Caller-supplied logger.

    Returns:
        render_output_manifest dict — factual record of what was produced.
        Does NOT include production_ready (that is Final Render Reviewer's job).
    """
    main_timeline   = timeline_plan.main_timeline
    shorts_timelines = timeline_plan.shorts_timelines
    short_ids        = timeline_plan.short_ids

    ffmpeg_errors: List[str] = []
    render_status = "success"

    # --- Main episode render ---
    main_output_path = episode_dir / f"main_{episode_id}.mp4"
    main_music_track = music_tracks.get("main_episode")

    main_success = _render_main_episode(
        timeline=main_timeline,
        narration_path=main_narration_path,
        music_track=main_music_track,
        output_path=main_output_path,
        episode_id=episode_id,
        timeout=ffmpeg_timeout,
        log=log,
    )
    if not (main_success and main_output_path.exists() and main_output_path.stat().st_size > 0):
        render_status = "failed"
        ffmpeg_errors.append("main_episode_render_failed")

    # --- Shorts renders ---
    short_output_paths: List[Tuple[str, Path]] = []
    for short_index, short_id in enumerate(short_ids):
        short_timeline       = shorts_timelines.get(short_id, [])
        short_narration_path = short_narration_paths.get(short_id)
        short_music_track    = music_tracks.get(f"short_{short_id}")

        short_output_path = episode_dir / f"short_{short_index}_{episode_id}.mp4"
        short_output_paths.append((short_id, short_output_path))

        short_success = _render_short(
            timeline=short_timeline,
            narration_path=short_narration_path,
            music_track=short_music_track,
            output_path=short_output_path,
            short_id=short_id,
            episode_id=episode_id,
            timeout=ffmpeg_timeout,
            log=log,
        )
        if not (short_success and short_output_path.exists() and short_output_path.stat().st_size > 0):
            if render_status != "failed":
                render_status = "partial"
            ffmpeg_errors.append(f"short_{short_index}_render_failed")

    # Clip availability below 100% downgrades success → partial
    if render_status == "success" and clip_availability_rate < 1.0:
        render_status = "partial"

    # duration_coverage_gap = aggregate hold time from clips that EXIST but are
    # shorter than their narration beat. Missing clips are already in missing_clips
    # and clip_availability_rate — including their time in the gap double-counts.
    # max_scene_hold_seconds = worst-case single-scene hold for existing clips,
    # used by the Final Render Reviewer to match the Timeline QA per-scene threshold.
    existing_holds = [
        max(0.0, e.duration_seconds - e.clip_duration_seconds)
        for e in main_timeline
        if e.clip_path is not None and e.clip_path.exists()
    ]
    hold_gap_dur = sum(existing_holds)
    max_scene_hold = max(existing_holds) if existing_holds else 0.0
    covered_dur = sum(
        e.duration_seconds
        for e in main_timeline
        if e.clip_path is not None and e.clip_path.exists()
    )

    return _build_render_output_manifest(
        episode_id=episode_id,
        render_status=render_status,
        main_output_path=main_output_path,
        short_output_paths=short_output_paths,
        clip_availability_rate=clip_availability_rate,
        missing_clips=missing_clips,
        sections_without_music=sections_without_music,
        ffmpeg_errors=ffmpeg_errors,
        total_narration_dur=sum_timeline_duration(main_timeline),
        covered_dur=covered_dur,
        hold_gap_dur=hold_gap_dur,
        max_scene_hold=max_scene_hold,
    )


# ---------------------------------------------------------------------------
# Internal: render dispatch
# ---------------------------------------------------------------------------

def _render_main_episode(
    timeline: List[TimelineEntry],
    narration_path: Optional[Path],
    music_track,
    output_path: Path,
    episode_id: str,
    timeout: int,
    log,
) -> bool:
    """
    Render main episode (16:9) using ffmpeg.
    Returns True on success, False on failure.
    """
    if not timeline and not narration_path:
        log.error(
            "assembly_ffmpeg_failed",
            episode_id=episode_id,
            component="main_episode",
            reason="no_timeline_and_no_narration",
        )
        return False

    cmd = _build_ffmpeg_concat_command(
        timeline=timeline,
        narration_path=narration_path,
        music_track=music_track,
        output_path=output_path,
        resolution=MAIN_RESOLUTION,
        episode_id=episode_id,
    )
    return _run_ffmpeg(
        cmd=cmd,
        output_path=output_path,
        component="main_episode",
        episode_id=episode_id,
        timeout=timeout,
        log=log,
    )


def _render_short(
    timeline: List[TimelineEntry],
    narration_path: Optional[Path],
    music_track,
    output_path: Path,
    short_id: str,
    episode_id: str,
    timeout: int,
    log,
) -> bool:
    """
    Render one Short (9:16) using ffmpeg.
    Returns True on success, False on failure.
    """
    if not timeline and not narration_path:
        log.error(
            "assembly_ffmpeg_failed",
            episode_id=episode_id,
            component=short_id,
            reason="no_timeline_and_no_narration",
        )
        return False

    cmd = _build_ffmpeg_concat_command(
        timeline=timeline,
        narration_path=narration_path,
        music_track=music_track,
        output_path=output_path,
        resolution=SHORT_RESOLUTION,
        episode_id=episode_id,
        is_short=True,
    )
    return _run_ffmpeg(
        cmd=cmd,
        output_path=output_path,
        component=short_id,
        episode_id=episode_id,
        timeout=timeout,
        log=log,
    )


def _build_ffmpeg_concat_command(
    timeline: List[TimelineEntry],
    narration_path: Optional[Path],
    music_track,
    output_path: Path,
    resolution: str,
    episode_id: str,
    is_short: bool = False,
) -> List[str]:
    """
    Build an ffmpeg command list for concatenating scenes with audio.

    Strategy B — tpad hold-based assembly:
    - Each clip plays at native duration.
    - When narration beat is longer than clip duration, the last frame is frozen
      via tpad=stop_mode=clone until the narration beat ends.
    - This preserves exact audio/video sync without re-encoding audio.

    Audio mixing:
    - Narration at 0 dB reference (volume=1.0).
    - Music at −18 dB (volume=_MUSIC_AMPLITUDE_FACTOR ≈ 0.126).
    - amix with duration=first so audio never outlasts the video.
    """
    width, height = resolution.split("x")

    inputs: List[str] = []
    filter_parts: List[str] = []
    has_clips = any(e.clip_path is not None for e in timeline)

    total_duration = sum_timeline_duration(timeline)
    if total_duration <= 0:
        total_duration = 30.0  # fallback for empty timelines

    if has_clips:
        # Strategy B: per-entry tpad hold where narration beat > clip duration.
        # Each clip is first normalized to the target resolution so that ffmpeg's
        # concat filter receives matching dimensions regardless of source model output.
        # Main (16:9): scale-to-fit + pad with black (letterbox, no crop).
        # Short (9:16): scale-to-fill + crop (fills frame, same as prior -vf behaviour).
        clip_idx = 0
        concat_input_labels: List[str] = []
        tpad_filter_parts: List[str] = []

        for entry in timeline:
            if entry.clip_path and entry.clip_path.exists():
                inputs += ["-i", str(entry.clip_path)]
                hold_seconds = max(0.0, entry.duration_seconds - entry.clip_duration_seconds)

                # Normalize resolution — output label [sv{n}]
                scale_label = f"[sv{clip_idx}]"
                if is_short:
                    tpad_filter_parts.append(
                        f"[{clip_idx}:v]scale={width}:{height}"
                        f":force_original_aspect_ratio=increase,"
                        f"crop={width}:{height}{scale_label}"
                    )
                else:
                    tpad_filter_parts.append(
                        f"[{clip_idx}:v]scale={width}:{height}"
                        f":force_original_aspect_ratio=decrease,"
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black{scale_label}"
                    )

                if hold_seconds > 0.001:
                    out_label = f"[pv{clip_idx}]"
                    tpad_filter_parts.append(
                        f"{scale_label}tpad=stop_mode=clone"
                        f":stop_duration={hold_seconds:.3f}{out_label}"
                    )
                    concat_input_labels.append(out_label)
                else:
                    # Clip covers its full beat — use scale output directly
                    concat_input_labels.append(scale_label)
                clip_idx += 1

        clip_count = clip_idx
        if clip_count > 0:
            filter_parts.extend(tpad_filter_parts)
            concat_inputs = "".join(concat_input_labels)
            filter_parts.append(
                f"{concat_inputs}concat=n={clip_count}:v=1:a=0[vout]"
            )
        video_map = "[vout]"
    else:
        # No clips — generate a black video of the required duration
        inputs += [
            "-f", "lavfi",
            "-i", f"color=c=black:s={resolution}:r={FRAME_RATE}:d={total_duration:.3f}",
        ]
        video_map = "0:v"

    # Audio inputs
    audio_input_index = len(inputs) // 2  # number of ffmpeg -i inputs so far
    has_narration = narration_path is not None and narration_path.exists()
    has_music = music_track is not None and music_track.local_path.exists()

    if has_narration:
        inputs += ["-i", str(narration_path)]
        narration_idx = audio_input_index
        audio_input_index += 1
    else:
        narration_idx = None

    if has_music:
        inputs += ["-i", str(music_track.local_path)]
        music_idx = audio_input_index
        audio_input_index += 1
    else:
        music_idx = None

    # Audio filter: mix narration + music, or narration only, or music only, or silence
    if has_narration and has_music:
        filter_parts.append(
            f"[{narration_idx}:a]volume=1.0[narr];"
            f"[{music_idx}:a]volume={_MUSIC_AMPLITUDE_FACTOR}[msc];"
            f"[narr][msc]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
        audio_map = "[aout]"
    elif has_narration:
        filter_parts.append(f"[{narration_idx}:a]volume=1.0[aout]")
        audio_map = "[aout]"
    elif has_music:
        filter_parts.append(
            f"[{music_idx}:a]volume={_MUSIC_AMPLITUDE_FACTOR}[aout]"
        )
        audio_map = "[aout]"
    else:
        # Generate silent audio
        inputs += [
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]
        audio_map = f"{audio_input_index}:a"

    cmd: List[str] = ["ffmpeg", "-y"]
    cmd += inputs

    if filter_parts:
        cmd += ["-filter_complex", ";".join(filter_parts)]

    cmd += ["-map", video_map]
    cmd += ["-map", audio_map]

    cmd += [
        "-c:v", _VIDEO_CODEC,
        "-c:a", _AUDIO_CODEC,
        "-b:a", _AUDIO_BITRATE,
        "-crf", _CRF,
        "-pix_fmt", "yuv420p",
        "-t", str(total_duration),
        str(output_path),
    ]

    return cmd


def _run_ffmpeg(
    cmd: List[str],
    output_path: Path,
    component: str,
    episode_id: str,
    timeout: int,
    log,
) -> bool:
    """
    Execute an ffmpeg command via subprocess.
    Returns True on success (exit code 0), False on failure.
    Never logs credential values.
    """
    log.info(
        "assembly_ffmpeg_start",
        episode_id=episode_id,
        component=component,
        cmd=" ".join(str(c) for c in cmd),
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.error(
            "assembly_ffmpeg_failed",
            episode_id=episode_id,
            component=component,
            reason="timeout",
            timeout_seconds=timeout,
        )
        return False
    except Exception as exc:
        log.error(
            "assembly_ffmpeg_failed",
            episode_id=episode_id,
            component=component,
            reason="subprocess_error",
            error=str(exc),
        )
        return False

    if result.returncode != 0:
        log.error(
            "assembly_ffmpeg_failed",
            episode_id=episode_id,
            component=component,
            returncode=result.returncode,
            stderr=result.stderr[-2000:] if result.stderr else "",
        )
        return False

    log.info(
        "assembly_ffmpeg_done",
        episode_id=episode_id,
        component=component,
        output_path=str(output_path),
    )
    return True


# ---------------------------------------------------------------------------
# Internal: manifest construction
# ---------------------------------------------------------------------------

def _build_render_output_manifest(
    episode_id: str,
    render_status: str,
    main_output_path: Path,
    short_output_paths: List[Tuple[str, Path]],
    clip_availability_rate: float,
    missing_clips: List[str],
    sections_without_music: List[str],
    ffmpeg_errors: List[str],
    total_narration_dur: float,
    covered_dur: float,
    hold_gap_dur: float = 0.0,
    max_scene_hold: float = 0.0,
) -> dict:
    """
    Build the render_output_manifest — a factual record of what Video Renderer produced.

    This manifest is the canonical input to Final Render Reviewer.
    Contains only file facts and render metrics; no QA verdicts.
    production_ready is NOT set here — that is Final Render Reviewer's responsibility.

    duration_coverage_gap_seconds: aggregate hold time from clips that EXIST but are
        shorter than their narration beat. Missing clips are not counted — they are
        captured in missing_clips and clip_availability_rate.
    max_scene_hold_seconds: worst-case single-scene hold for existing clips.
        Used by the Final Render Reviewer alongside the Timeline QA per-scene threshold.
    """
    main_exists = main_output_path.exists() and main_output_path.stat().st_size > 0
    main_size   = main_output_path.stat().st_size if main_output_path.exists() else 0

    shorts_list: List[dict] = []
    for short_id, path in short_output_paths:
        exists = path.exists() and path.stat().st_size > 0
        size   = path.stat().st_size if path.exists() else 0
        shorts_list.append({
            "short_id":         short_id,
            "file_path":        str(path),
            "file_exists":      exists,
            "file_size_bytes":  size,
        })

    return {
        "manifest_id":               f"rom_{episode_id}",
        "episode_id":                episode_id,
        "generated_at":              datetime.now(timezone.utc).isoformat(),
        "render_status":             render_status,
        "main_render": {
            "file_path":             str(main_output_path),
            "file_exists":           main_exists,
            "file_size_bytes":       main_size,
        },
        "shorts_renders":            shorts_list,
        "clip_availability_rate":    round(clip_availability_rate, 4),
        "missing_clips":             missing_clips,
        "sections_without_music":    sections_without_music,
        "ffmpeg_errors":             ffmpeg_errors,
        "duration_coverage_gap_seconds": round(hold_gap_dur, 3),
        "max_scene_hold_seconds":        round(max_scene_hold, 3),
        "total_narration_seconds":       round(total_narration_dur, 3),
        "covered_narration_seconds":     round(covered_dur, 3),
        # Resolution/frame-rate facts — consumed by assembly_agent for Final Render Package
        "main_resolution":  MAIN_RESOLUTION,
        "short_resolution": SHORT_RESOLUTION,
        "frame_rate":       FRAME_RATE,
    }
