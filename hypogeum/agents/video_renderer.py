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
    end_page_path: Optional[Path] = None,
    end_page_duration: float = 20.0,
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
    elif end_page_path is not None and end_page_path.exists():
        # Append static end-page card to the main render (main episode only, never shorts).
        end_page_ok = _append_end_page(
            main_output_path=main_output_path,
            end_page_path=end_page_path,
            duration=end_page_duration,
            episode_id=episode_id,
            timeout=ffmpeg_timeout,
            log=log,
        )
        if not end_page_ok:
            # Non-fatal: log the failure but keep the body render as-is.
            log.warn(
                "assembly_end_page_append_failed",
                episode_id=episode_id,
                end_page_path=str(end_page_path),
            )
        else:
            log.info(
                "assembly_end_page_appended",
                episode_id=episode_id,
                end_page_path=str(end_page_path),
                end_page_duration_seconds=end_page_duration,
            )

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

    Post-render duration guard:
    After rendering, checks the body duration against the expected timeline
    duration. If ffmpeg produced a body that is >1s longer than expected (a
    known ffmpeg 8.x regression where -t and filtergraph trim both fail with
    large filter complexes), performs a stream-copy trim pass to correct it.
    This is a hard-safety fallback — the filtergraph already contains an
    explicit trim filter, but the stream-copy pass guarantees correctness.
    """
    if not timeline and not narration_path:
        log.error(
            "assembly_ffmpeg_failed",
            episode_id=episode_id,
            component="main_episode",
            reason="no_timeline_and_no_narration",
        )
        return False

    expected_duration = sum_timeline_duration(timeline)

    # Render to a raw temp path first, then always apply a duration trim pass.
    # This guarantees the final body is exactly expected_duration regardless of
    # any ffmpeg 8.x filter-graph or -t interaction issues.
    raw_output = output_path.with_suffix(".body_raw.mp4")

    cmd = _build_ffmpeg_concat_command(
        timeline=timeline,
        narration_path=narration_path,
        music_track=music_track,
        output_path=raw_output,
        resolution=MAIN_RESOLUTION,
        episode_id=episode_id,
    )
    ok = _run_ffmpeg(
        cmd=cmd,
        output_path=raw_output,
        component="main_episode",
        episode_id=episode_id,
        timeout=timeout,
        log=log,
    )
    if not ok or not raw_output.exists() or raw_output.stat().st_size == 0:
        return False

    # --- Duration guard: always trim raw body to expected_duration ---
    # Probe both streams for diagnostic clarity; log before trim.
    import json as _json_diag
    try:
        _pr2 = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", str(raw_output)],
            capture_output=True, text=True, timeout=30,
        )
        _ss = _json_diag.loads(_pr2.stdout).get("streams", []) if _pr2.returncode == 0 else []
        _raw_v = next((float(s["duration"]) for s in _ss if s.get("codec_type") == "video" and s.get("duration")), 0.0)
        _raw_a = next((float(s["duration"]) for s in _ss if s.get("codec_type") == "audio" and s.get("duration")), 0.0)
    except Exception:
        _raw_v, _raw_a = 0.0, 0.0
    log.info(
        "assembly_body_raw_probe",
        episode_id=episode_id,
        raw_video_s=round(_raw_v, 3),
        raw_audio_s=round(_raw_a, 3),
        expected_s=round(expected_duration, 3),
    )
    actual_dur = max(_raw_v, _raw_a)
    needs_trim = actual_dur > expected_duration + 0.5

    if needs_trim:
        log.warn(
            "assembly_body_overlong_trimming",
            episode_id=episode_id,
            actual_seconds=round(actual_dur, 3),
            expected_seconds=round(expected_duration, 3),
            excess_seconds=round(actual_dur - expected_duration, 3),
        )

    trimmed = _trim_video_to_duration(
        input_path=raw_output,
        target_duration=expected_duration,
        output_path=output_path,
        episode_id=episode_id,
        timeout=timeout,
        log=log,
    )
    if not trimmed:
        # Trim failed; fall back to raw (may be overlong)
        log.warn(
            "assembly_body_trim_failed_using_raw",
            episode_id=episode_id,
            fallback_duration=round(actual_dur, 3),
        )
        try:
            raw_output.rename(output_path)
        except OSError:
            pass
        return output_path.exists() and output_path.stat().st_size > 0

    log.info(
        "assembly_body_trim_done",
        episode_id=episode_id,
        target_seconds=round(expected_duration, 3),
        was_overlong=needs_trim,
    )

    # Cleanup raw
    try:
        if raw_output.exists():
            raw_output.unlink()
    except OSError:
        pass

    return True


def _probe_output_duration(path: Path) -> float:
    """
    Return the video duration of a rendered file via ffprobe.

    Tries stream-level duration first; falls back to container format duration.
    Some ffmpeg renders omit stream-level duration in the header (storing it only
    in the moov container atom), so the fallback ensures the guard always fires
    correctly when the body is overlong.

    Returns 0.0 only when both probes fail — the guard condition (actual > expected + 1)
    is safely False in that case, preventing a spurious trim on an unreadable file.
    """
    for args in [
        # 1st try: video stream duration
        [
            "ffprobe", "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", "stream=duration",
            "-of", "csv=p=0",
            str(path),
        ],
        # 2nd try: container format duration
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(path),
        ],
    ]:
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=30,
            )
            val = result.stdout.strip()
            if result.returncode == 0 and val:
                return float(val)
        except Exception:
            pass
    return 0.0


def _trim_video_to_duration(
    input_path: Path,
    target_duration: float,
    episode_id: str,
    timeout: int,
    log,
    output_path: Optional[Path] = None,
) -> bool:
    """
    Trim a rendered video to target_duration seconds via re-encode with -t flag.

    Uses a simple re-encode with `-t target_duration` as output flag (no filter_complex).
    This is reliable even when the source video contains tpad-cloned frames whose PTS
    may cause filter-based `trim=end=X` to behave unexpectedly (observed: audio trim
    works correctly but video trim does not honour `trim=end=X` on tpad-generated frames).

    Stream-copy (-c copy) is NOT used because keyframe-aligned cuts can leave
    the video stream significantly longer than target_duration when the video
    contains long tpad-frozen sections with sparse keyframes.

    Args:
        input_path:      Source video to trim (may be overlong).
        target_duration: Maximum output duration in seconds.
        output_path:     Destination path. Defaults to input_path (in-place via temp).

    Returns True on success.
    """
    if output_path is None:
        output_path = input_path

    tmp_path = input_path.with_suffix(".trim_tmp.mp4")
    try:
        # Input-side -t stops ffmpeg from reading past target_duration seconds of the
        # input stream. This prevents the libx264 encoder's lookahead buffer from
        # seeing frames beyond the target and flushing them after the output -t cutoff.
        # Output-side -t is a redundant second guard.
        cmd = [
            "ffmpeg", "-y",
            "-t", f"{target_duration:.6f}",   # INPUT: stop reading input at target
            "-i", str(input_path),
            "-t", f"{target_duration:.6f}",   # OUTPUT: second guard
            "-map", "0:v:0",
            "-map", "0:a:0",
            "-c:v", _VIDEO_CODEC,
            "-c:a", _AUDIO_CODEC,
            "-b:a", _AUDIO_BITRATE,
            "-crf", _CRF,
            "-pix_fmt", "yuv420p",
            str(tmp_path),
        ]
        ok = _run_ffmpeg(
            cmd=cmd,
            output_path=tmp_path,
            component="main_episode_trim",
            episode_id=episode_id,
            timeout=timeout,
            log=log,
        )
        if ok and tmp_path.exists() and tmp_path.stat().st_size > 0:
            import json as _json
            try:
                _pr = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-print_format", "json",
                     "-show_streams", str(tmp_path)],
                    capture_output=True, text=True, timeout=30,
                )
                _streams = _json.loads(_pr.stdout).get("streams", []) if _pr.returncode == 0 else []
                _vdur = next((float(s["duration"]) for s in _streams if s.get("codec_type") == "video" and s.get("duration")), 0.0)
                _adur = next((float(s["duration"]) for s in _streams if s.get("codec_type") == "audio" and s.get("duration")), 0.0)
            except Exception:
                _vdur, _adur = 0.0, 0.0
            log.info(
                "assembly_body_trim_probe",
                episode_id=episode_id,
                target_seconds=round(target_duration, 3),
                trimmed_video_s=round(_vdur, 3),
                trimmed_audio_s=round(_adur, 3),
            )
            if output_path == input_path and input_path.exists():
                input_path.unlink()
            tmp_path.rename(output_path)
            return True
        return False
    except Exception as exc:
        log.error(
            "assembly_body_trim_error",
            episode_id=episode_id,
            error=str(exc),
        )
        return False
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


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
    - Each clip is first trimmed to its beat_duration when clip_duration > beat_duration,
      then padded (last frame frozen via tpad) when beat_duration > clip_duration.
    - A hard trim=[vout]trim=0:total_duration[vout_safe];setpts=PTS-STARTPTS is applied
      after concat to guarantee body duration never exceeds total_duration regardless
      of any ffmpeg filter-graph or -t interaction issues (observed in ffmpeg 8.1 with
      large filter graphs: -t output flag alone fails to limit output).
    - This preserves exact audio/video sync without re-encoding audio.

    Audio mixing:
    - Narration at 0 dB reference (volume=1.0).
    - Music at −18 dB (volume=_MUSIC_AMPLITUDE_FACTOR ≈ 0.126).
    - amix with duration=first so audio never outlasts the video.
    - atrim applied to narration to hard-limit audio to total_duration as well.
    """
    width, height = resolution.split("x")

    inputs: List[str] = []
    filter_parts: List[str] = []
    has_clips = any(e.clip_path is not None for e in timeline)

    total_duration = sum_timeline_duration(timeline)
    if total_duration <= 0:
        total_duration = 30.0  # fallback for empty timelines

    if has_clips:
        # Strategy B: per-entry trim+tpad.
        # 1. When clip_duration > beat_duration: trim the clip to beat_duration.
        #    Previously the clip played in full (only tpad hold was skipped), which
        #    caused the concat output to exceed total_duration.
        # 2. When beat_duration > clip_duration: tpad clones the last frame.
        # 3. After concat: hard-trim the video stream to total_duration and reset PTS.
        #    This is a redundant safety guard — in ffmpeg 8.1 with large filter graphs
        #    (35+ inputs), the -t output flag can fail to limit the rendered body.
        #
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
                trim_needed = entry.clip_duration_seconds > entry.duration_seconds + 0.001

                # Normalize resolution — output label [sv{n}]
                scale_label = f"[sv{clip_idx}]"
                if is_short:
                    tpad_filter_parts.append(
                        f"[{clip_idx}:v]scale={width}:{height}"
                        f":force_original_aspect_ratio=increase,"
                        f"crop={width}:{height},setsar=1{scale_label}"
                    )
                else:
                    tpad_filter_parts.append(
                        f"[{clip_idx}:v]scale={width}:{height}"
                        f":force_original_aspect_ratio=decrease,"
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black{scale_label}"
                    )

                if trim_needed:
                    # Clip is longer than beat: trim it to exact beat_duration,
                    # then reset PTS so the concat filter sees continuous timestamps.
                    trim_label = f"[tv{clip_idx}]"
                    tpad_filter_parts.append(
                        f"{scale_label}trim=duration={entry.duration_seconds:.3f},"
                        f"setpts=PTS-STARTPTS{trim_label}"
                    )
                    concat_input_labels.append(trim_label)
                elif hold_seconds > 1.0:
                    # Motion extension: stretch clip to beat duration.
                    # tpad (freeze last frame) is FORBIDDEN for holds > 1.0s.
                    # Strategy: slow-motion via setpts (factor ≤ 2.0) or seamless
                    # loop (factor > 2.0) so the frame plays real motion throughout.
                    out_label = f"[pv{clip_idx}]"
                    factor = entry.duration_seconds / max(entry.clip_duration_seconds, 0.001)
                    if factor <= 2.0:
                        # Slow-motion: each frame's PTS is scaled by factor,
                        # then fps filter re-samples to FRAME_RATE for the encoder.
                        tpad_filter_parts.append(
                            f"{scale_label}setpts={factor:.6f}*PTS,"
                            f"fps=fps={FRAME_RATE}{out_label}"
                        )
                    else:
                        # Loop the clip to fill the beat, then trim to exact duration.
                        # size=32767 is the ffmpeg loop filter max (safe for clips ≤18 min).
                        tpad_filter_parts.append(
                            f"{scale_label}loop=loop=-1:size=32767:start=0,"
                            f"trim=duration={entry.duration_seconds:.3f},"
                            f"setpts=PTS-STARTPTS{out_label}"
                        )
                    concat_input_labels.append(out_label)
                elif hold_seconds > 0.001:
                    # Small gap (≤ 1.0s): tpad emergency fallback only.
                    out_label = f"[pv{clip_idx}]"
                    tpad_filter_parts.append(
                        f"{scale_label}tpad=stop_mode=clone"
                        f":stop_duration={hold_seconds:.3f}{out_label}"
                    )
                    concat_input_labels.append(out_label)
                else:
                    # Clip duration == beat duration (within tolerance)
                    concat_input_labels.append(scale_label)
                clip_idx += 1

        clip_count = clip_idx
        if clip_count > 0:
            filter_parts.extend(tpad_filter_parts)
            concat_inputs = "".join(concat_input_labels)
            # After concat: hard-trim to total_duration + reset PTS.
            # This prevents any per-clip rounding or tpad overflow from accumulating
            # into a body that exceeds the narration duration.
            filter_parts.append(
                f"{concat_inputs}concat=n={clip_count}:v=1:a=0[vconcat];"
                f"[vconcat]trim=0:{total_duration:.6f},"
                f"setpts=PTS-STARTPTS[vout]"
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

    # Audio filter: mix narration + music, or narration only, or music only, or silence.
    # atrim on narration hard-limits audio to total_duration to match the video body.
    if has_narration and has_music:
        filter_parts.append(
            f"[{narration_idx}:a]atrim=0:{total_duration:.6f},asetpts=PTS-STARTPTS,volume=1.0[narr];"
            f"[{music_idx}:a]volume={_MUSIC_AMPLITUDE_FACTOR}[msc];"
            f"[narr][msc]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
        audio_map = "[aout]"
    elif has_narration:
        filter_parts.append(
            f"[{narration_idx}:a]atrim=0:{total_duration:.6f},asetpts=PTS-STARTPTS,volume=1.0[aout]"
        )
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
        "-movflags", "+faststart",
        "-t", f"{total_duration:.6f}",
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
# Internal: end-page append
# ---------------------------------------------------------------------------

def _append_end_page(
    main_output_path: Path,
    end_page_path: Path,
    duration: float,
    episode_id: str,
    timeout: int,
    log,
) -> bool:
    """
    Append a static end-page PNG card to the main episode video.

    Two-pass strategy:
      1. Generate a short video clip from the PNG (same codec/resolution as main).
      2. Concat the body + end-page clip using the ffmpeg concat demuxer (-c copy).

    Both clips are encoded at MAIN_RESOLUTION with yuv420p / libx264 / aac so
    the concat demuxer can stream-copy without re-encoding.

    Returns True on success, False on any failure. Non-fatal — caller logs a warn.
    """
    body_path  = main_output_path.with_suffix(".body.mp4")
    clip_path  = main_output_path.with_suffix(".endpage.mp4")
    list_path  = main_output_path.with_suffix(".concat.txt")
    width, height = MAIN_RESOLUTION.split("x")

    try:
        # --- 1. Rename body to temp ---
        main_output_path.rename(body_path)

        # --- 2. Create end-page video clip ---
        ep_cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-framerate", str(FRAME_RATE),
            "-t", f"{duration:.3f}",
            "-i", str(end_page_path),
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex",
            (
                f"[0:v]scale={width}:{height}"
                f":force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black[vout];"
                f"[1:a]volume=0.0[aout]"
            ),
            "-map", "[vout]",
            "-map", "[aout]",
            "-c:v", _VIDEO_CODEC,
            "-c:a", _AUDIO_CODEC,
            "-b:a", _AUDIO_BITRATE,
            "-crf", _CRF,
            "-pix_fmt", "yuv420p",
            "-t", f"{duration:.3f}",
            str(clip_path),
        ]
        clip_ok = _run_ffmpeg(
            cmd=ep_cmd,
            output_path=clip_path,
            component="end_page_clip",
            episode_id=episode_id,
            timeout=timeout,
            log=log,
        )
        if not clip_ok:
            body_path.rename(main_output_path)
            return False

        # --- 3. Concat body + end-page clip via filter_complex re-encode ---
        # Stream-copy (-c copy) is NOT used here because body.mp4 may have an
        # inconsistency between its tkhd duration (665.5s from -t flag) and the
        # stts sample table (836.875s from encoder pipeline flush after -t cutoff).
        # ffprobe reads tkhd and reports 665.5s; the concat demuxer reads stts and
        # copies 836.875s of samples, placing the endpage 171.4s too late.
        # filter_complex decodes via the sample PTS (last sample at 665.5s), which
        # gives correct frame-accurate timestamps in the re-encoded output.
        concat_cmd = [
            "ffmpeg", "-y",
            "-i", str(body_path),
            "-i", str(clip_path),
            "-filter_complex",
            "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[vout][aout]",
            "-map", "[vout]",
            "-map", "[aout]",
            "-c:v", _VIDEO_CODEC,
            "-c:a", _AUDIO_CODEC,
            "-b:a", _AUDIO_BITRATE,
            "-crf", _CRF,
            "-pix_fmt", "yuv420p",
            str(main_output_path),
        ]
        concat_ok = _run_ffmpeg(
            cmd=concat_cmd,
            output_path=main_output_path,
            component="end_page_concat",
            episode_id=episode_id,
            timeout=timeout,
            log=log,
        )
        if not concat_ok:
            # Restore body as the best available output
            if main_output_path.exists():
                main_output_path.unlink()
            body_path.rename(main_output_path)
            return False

        return True

    except Exception as exc:
        log.error(
            "assembly_end_page_error",
            episode_id=episode_id,
            error=str(exc),
        )
        # Best-effort restore
        if body_path.exists() and not main_output_path.exists():
            body_path.rename(main_output_path)
        return False
    finally:
        # Clean up temp files regardless of outcome
        for p in (body_path, clip_path):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass


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
