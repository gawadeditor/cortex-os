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
_MUSIC_AMPLITUDE_FACTOR = 0.20    # ≈ −14 dB beneath narration (audible background bed)
_MUSIC_TAIL_SECONDS    = 2.5      # music continues this many seconds after VO ends
_DEFAULT_TIMEOUT        = 600     # seconds
_CLIP_TAIL_FADE_SECS   = 2 / FRAME_RATE  # 2-frame fade-out at each clip boundary (≈ 67ms)


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
    music_segments: Optional[List[dict]] = None,
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
        music_segments:         Optional ordered list of timed music segments
                                for the main episode only (Option B,
                                2026-07-19): [{start_seconds, end_seconds,
                                track}, ...]. Video Renderer does not derive
                                narrative categories or section boundaries —
                                it only places whatever ready-made segments
                                it is given. When omitted or when no segment
                                carries a real track, falls back to the
                                legacy single music_tracks["main_episode"]
                                track unchanged. Shorts are never affected.

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
    # Legacy single-track lookup (used as fallback when no segment carries a
    # real track — see _build_ffmpeg_concat_command). Option B, 2026-07-19.
    main_music_track = music_tracks.get("main_episode")
    main_music_segments = music_segments or []
    # For end-page continuation and manifest facts: the last segment's track
    # is what's actually playing at the end of the body when multi-segment
    # music is active; otherwise the legacy single track.
    _segments_with_tracks = [s for s in main_music_segments if s.get("track") is not None]
    end_page_music_track = (
        _segments_with_tracks[-1]["track"] if _segments_with_tracks else main_music_track
    )

    main_success = _render_main_episode(
        timeline=main_timeline,
        narration_path=main_narration_path,
        music_track=main_music_track,
        output_path=main_output_path,
        episode_id=episode_id,
        timeout=ffmpeg_timeout,
        log=log,
        music_segments=main_music_segments,
    )
    if not (main_success and main_output_path.exists() and main_output_path.stat().st_size > 0):
        render_status = "failed"
        ffmpeg_errors.append("main_episode_render_failed")
    elif end_page_path is not None and end_page_path.exists():
        # Probe body duration so _append_end_page can start music at the correct offset.
        body_duration = _probe_output_duration(main_output_path)

        # Append static end-page card to the main render (main episode only, never shorts).
        end_page_ok = _append_end_page(
            main_output_path=main_output_path,
            end_page_path=end_page_path,
            duration=end_page_duration,
            episode_id=episode_id,
            timeout=ffmpeg_timeout,
            log=log,
            music_track=end_page_music_track,
            body_duration=body_duration,
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

    # --- Post-render blackdetect QA gate ---
    if main_success and main_output_path.exists() and main_output_path.stat().st_size > 0:
        _run_blackdetect_qa(
            output_path=main_output_path,
            episode_id=episode_id,
            timeout=ffmpeg_timeout,
            log=log,
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

    manifest = _build_render_output_manifest(
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

    # Music QA facts — used by rerender_ep.py report
    music_dur_used = 0.0
    if end_page_music_track is not None:
        music_dur_used = _probe_output_duration(end_page_music_track.local_path)
    music_continues = (
        end_page_music_track is not None
        and end_page_path is not None
        and end_page_path.exists()
    )
    manifest["music_duration_used_s"] = round(music_dur_used, 3)
    manifest["music_continues_under_end_page"] = music_continues

    return manifest


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
    music_segments: Optional[List[dict]] = None,
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

    music_segments (Option B, 2026-07-19): optional list of timed music
    segments for the main episode, each {start_seconds, end_seconds, track}.
    When at least one segment carries a real track, multi-segment music
    placement is used (see _build_multi_segment_music_filter()). Otherwise
    falls back to the legacy single music_track parameter unchanged.
    """
    if not timeline and not narration_path:
        log.error(
            "assembly_ffmpeg_failed",
            episode_id=episode_id,
            component="main_episode",
            reason="no_timeline_and_no_narration",
        )
        return False

    narr_duration = sum_timeline_duration(timeline)
    expected_duration = narr_duration + _MUSIC_TAIL_SECONDS

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
        music_tail_seconds=_MUSIC_TAIL_SECONDS,
        music_segments=music_segments,
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

    Collects durations from all three probes (video stream, audio stream,
    container format) and returns the maximum.  This guards against the ffmpeg
    8.x regression where the video-stream tkhd header reports a shorter duration
    than the actual sample table (stts) — the audio stream and format duration
    always reflect the true length.

    Returns 0.0 only when all probes fail — the guard condition (actual > expected + 1)
    is safely False in that case, preventing a spurious trim on an unreadable file.
    """
    probe_args_list = [
        # video stream duration (may be short due to tkhd/stts mismatch in ffmpeg 8.x)
        [
            "ffprobe", "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", "stream=duration",
            "-of", "csv=p=0",
            str(path),
        ],
        # audio stream duration
        [
            "ffprobe", "-v", "quiet",
            "-select_streams", "a:0",
            "-show_entries", "stream=duration",
            "-of", "csv=p=0",
            str(path),
        ],
        # container format duration (most reliable)
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(path),
        ],
    ]
    durations = []
    for args in probe_args_list:
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=30,
            )
            val = result.stdout.strip()
            if result.returncode == 0 and val:
                durations.append(float(val))
        except Exception:
            pass
    return max(durations) if durations else 0.0


def _run_blackdetect_qa(
    output_path: Path,
    episode_id: str,
    timeout: int,
    log,
) -> None:
    """
    Post-render blackdetect QA gate.

    Runs ffmpeg blackdetect (d=0.5s, pix_th=0.10) on the final output and logs
    any detected black frame clusters as warnings. Non-blocking — never fails the
    render, only surfaces QA signals for editorial review.
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-i", str(output_path),
                "-vf", "blackdetect=d=0.5:pix_th=0.10",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=min(timeout, 180),
        )
        # ffmpeg writes blackdetect events to stderr
        black_events = [
            line.strip() for line in result.stderr.splitlines()
            if "black_start" in line
        ]
        if black_events:
            log.warn(
                "assembly_blackdetect_events",
                episode_id=episode_id,
                event_count=len(black_events),
                events=black_events[:5],
            )
        else:
            log.info(
                "assembly_blackdetect_clean",
                episode_id=episode_id,
            )
    except Exception as exc:
        log.warn(
            "assembly_blackdetect_failed",
            episode_id=episode_id,
            error=str(exc),
        )


def _probe_music_usable_duration(music_path: Path) -> float:
    """
    Return the usable looping duration of a music track — i.e., the duration
    BEFORE any end-of-track fade-out silence.

    Runs silencedetect and looks for a silence whose end is within 3s of the
    track's total duration (indicating a fade-out). Returns the start of that
    silence as the usable duration so the loop unit excludes the fade.

    Falls back to the full track duration if no end-fade is detected.
    """
    import re as _re
    track_dur = _probe_output_duration(music_path)
    if track_dur <= 0:
        return 0.0
    try:
        r = subprocess.run(
            ["ffmpeg", "-i", str(music_path),
             "-af", "silencedetect=noise=-50dB:d=0.5",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
        output = r.stderr
    except Exception:
        return track_dur

    # Parse silence segments
    segments: list = []
    start = None
    for line in output.splitlines():
        ms = _re.search(r"silence_start:\s*([\d.]+)", line)
        if ms:
            start = float(ms.group(1))
        me = _re.search(r"silence_end:\s*([\d.]+)", line)
        if me and start is not None:
            segments.append({"start": start, "end": float(me.group(1))})
            start = None

    # Find last silence whose end is within 3s of track end (end-fade indicator)
    for seg in reversed(segments):
        if abs(seg["end"] - track_dur) < 3.0:
            # Return start of this fade-out, with 0.1s margin
            usable = max(1.0, seg["start"] - 0.1)
            return usable

    return track_dur


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


def _build_multi_segment_music_filter(
    segments: List[dict],
    start_input_index: int,
    music_tail_seconds: float,
) -> Tuple[List[str], str, int]:
    """
    Build ffmpeg -i inputs and a filter_complex fragment for multiple timed
    music segments, combined into a single [msc] label (Option B, 2026-07-19,
    main episode only).

    Each segment's track is looped via the same atrim->aloop->atrim idiom
    used for the single-track case (see _build_ffmpeg_concat_command) to
    fill exactly its own [start_seconds, end_seconds) window, then shifted
    into absolute timeline position with adelay. The last segment's window
    is extended by music_tail_seconds so music continues into the outro
    hold, matching the single-track behavior. All segment streams are
    summed via amix into one [msc] output — everything downstream of [msc]
    (the narration mix, the loudnorm pass) is unchanged.

    Args:
        segments:           Ordered list of {start_seconds, end_seconds, track}
                            dicts. Every entry must have a real track (callers
                            filter out None tracks before calling this).
        start_input_index:  Next available ffmpeg -i input index.
        music_tail_seconds: Seconds to extend the final segment by.

    Returns:
        (extra_inputs, filter_fragment, next_input_index) — filter_fragment
        is a single string (already ";"-joined) ending in a [msc] label.
    """
    inputs: List[str] = []
    seg_filter_parts: List[str] = []
    seg_labels: List[str] = []
    idx = start_input_index

    for i, seg in enumerate(segments):
        track = seg["track"]
        inputs += ["-i", str(track.local_path)]

        seg_duration = seg["end_seconds"] - seg["start_seconds"]
        if i == len(segments) - 1:
            seg_duration += music_tail_seconds
        seg_duration = max(0.1, seg_duration)

        usable_dur = _probe_music_usable_duration(track.local_path)
        try:
            sr_result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "stream=sample_rate",
                 "-of", "csv=p=0", str(track.local_path)],
                capture_output=True, text=True, timeout=10,
            )
            sample_rate = int(sr_result.stdout.strip().splitlines()[0])
        except Exception:
            sample_rate = 44100
        usable_samples = int(usable_dur * sample_rate)
        delay_ms = max(0, int(round(seg["start_seconds"] * 1000)))

        label = f"[mscseg{i}]"
        seg_filter_parts.append(
            f"[{idx}:a]"
            f"atrim=0:{usable_dur:.6f},asetpts=PTS-STARTPTS,"
            f"aloop=loop=-1:size={usable_samples},"
            f"atrim=0:{seg_duration:.6f},asetpts=PTS-STARTPTS,"
            f"adelay=delays={delay_ms}:all=1,"
            f"volume={_MUSIC_AMPLITUDE_FACTOR}{label}"
        )
        seg_labels.append(label)
        idx += 1

    if len(seg_labels) == 1:
        seg_filter_parts.append(f"{seg_labels[0]}anull[msc]")
    else:
        seg_filter_parts.append(
            f"{''.join(seg_labels)}amix=inputs={len(seg_labels)}:"
            f"duration=longest:dropout_transition=0[msc]"
        )

    return inputs, ";".join(seg_filter_parts), idx


def _build_ffmpeg_concat_command(
    timeline: List[TimelineEntry],
    narration_path: Optional[Path],
    music_track,
    output_path: Path,
    resolution: str,
    episode_id: str,
    is_short: bool = False,
    music_tail_seconds: float = 0.0,
    music_segments: Optional[List[dict]] = None,
) -> List[str]:
    """
    Build an ffmpeg command list for concatenating scenes with audio.

    Strategy B — motion-extension assembly:
    - Each clip is trimmed to its beat_duration when clip_duration > beat_duration.
    - When beat_duration > clip_duration by >1s: setpts slow-motion (factor ≤2.0)
      or loop+trim (factor >2.0) — NO tpad freeze for main content.
    - When beat_duration > clip_duration by ≤1s: tpad emergency fallback.
    - A hard trim=0:narr_dur after concat prevents any accumulation past the total.
    - When music_tail_seconds > 0 (main episode only): a short tpad hold is appended
      to the video AFTER the narration ends so music can continue over a static frame
      before the end page. This is an intentional outro pause, not a mid-content freeze.

    Audio mixing:
    - Narration at 0 dB reference (volume=1.0), hard-trimmed to narration duration.
    - Narration padded with silence for music_tail_seconds so amix does not cut early.
    - Music input uses -stream_loop -1 so it covers the entire body regardless of length.
    - Music at _MUSIC_AMPLITUDE_FACTOR (≈ −14 dB) — audible background bed under VO.
    - amix with duration=first; first input is narration padded to body_dur.

    music_segments (Option B, 2026-07-19, main episode only): when supplied
    and at least one segment carries a real track, the [msc] label is built
    by _build_multi_segment_music_filter() instead of the single-track loop
    below — each segment is looped/trimmed to its own [start, end) window
    and adelay-shifted into place, then summed into one [msc] stream. Every
    line downstream of [msc] (the narration mix, loudnorm) is unchanged.
    Falls back to the single music_track path when no segment has a track
    (empty/None music_segments, or an older single-key music_track_package).
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
                    # Add a 2-frame tail fade-out to eliminate hard black end-frame artifacts.
                    trim_label = f"[tv{clip_idx}]"
                    _fade_st = max(0.0, entry.duration_seconds - _CLIP_TAIL_FADE_SECS)
                    tpad_filter_parts.append(
                        f"{scale_label}trim=duration={entry.duration_seconds:.3f},"
                        f"setpts=PTS-STARTPTS,"
                        f"fade=t=out:st={_fade_st:.4f}:d={_CLIP_TAIL_FADE_SECS:.4f}{trim_label}"
                    )
                    concat_input_labels.append(trim_label)
                elif hold_seconds > 1.0:
                    # Motion extension: stretch clip to beat duration.
                    # tpad (freeze last frame) is FORBIDDEN for holds > 1.0s.
                    # Strategy: slow-motion via setpts, unconditionally, for every
                    # factor. Never reverse, never loop/repeat.
                    # A prior version (commit 2a10a34) split at factor>2.0 into a
                    # forward+reverse "ping-pong" loop specifically to hide the
                    # jump-cut of an even earlier forward-only loop=-1 (see that
                    # commit's own message) — both predecessors are forbidden now
                    # (reverse motion, and >1 repeat, respectively). Pure setpts
                    # has no loop boundary and shows the real clip content exactly
                    # once, so it satisfies both constraints unconditionally
                    # regardless of how large the factor is.
                    out_label = f"[pv{clip_idx}]"
                    factor = entry.duration_seconds / max(entry.clip_duration_seconds, 0.001)
                    _fade_st = max(0.0, entry.duration_seconds - _CLIP_TAIL_FADE_SECS)
                    tpad_filter_parts.append(
                        f"{scale_label}setpts={factor:.6f}*PTS,"
                        f"fps=fps={FRAME_RATE},"
                        f"fade=t=out:st={_fade_st:.4f}:d={_CLIP_TAIL_FADE_SECS:.4f}{out_label}"
                    )
                    concat_input_labels.append(out_label)
                elif hold_seconds > 0.001:
                    # Small gap (≤ 1.0s): tpad emergency fallback only.
                    # Add a 2-frame tail fade-out to eliminate hard black end-frame artifacts.
                    out_label = f"[pv{clip_idx}]"
                    _fade_st = max(0.0, entry.duration_seconds - _CLIP_TAIL_FADE_SECS)
                    tpad_filter_parts.append(
                        f"{scale_label}tpad=stop_mode=clone"
                        f":stop_duration={hold_seconds:.3f},"
                        f"fade=t=out:st={_fade_st:.4f}:d={_CLIP_TAIL_FADE_SECS:.4f}{out_label}"
                    )
                    concat_input_labels.append(out_label)
                else:
                    # Clip duration == beat duration (within tolerance).
                    # Add a 2-frame tail fade-out to eliminate hard black end-frame artifacts.
                    exact_label = f"[ev{clip_idx}]"
                    _fade_st = max(0.0, entry.clip_duration_seconds - _CLIP_TAIL_FADE_SECS)
                    tpad_filter_parts.append(
                        f"{scale_label}fade=t=out:st={_fade_st:.4f}:d={_CLIP_TAIL_FADE_SECS:.4f}{exact_label}"
                    )
                    concat_input_labels.append(exact_label)
                clip_idx += 1
            else:
                # Missing clip: insert a black-frame lavfi source for the beat duration so the
                # video timeline stays in sync with the narration. Without this the video stream
                # ends early (sum of present beats only) while audio runs the full body.
                _beat_dur = max(0.1, entry.duration_seconds)
                inputs += [
                    "-f", "lavfi",
                    "-i", f"color=c=black:s={width}x{height}:r={FRAME_RATE}:d={_beat_dur:.3f}",
                ]
                black_label = f"[blk{clip_idx}]"
                tpad_filter_parts.append(f"[{clip_idx}:v]null{black_label}")
                concat_input_labels.append(black_label)
                clip_idx += 1

        clip_count = clip_idx
        if clip_count > 0:
            filter_parts.extend(tpad_filter_parts)
            concat_inputs = "".join(concat_input_labels)
            # After concat: hard-trim to narration duration + reset PTS.
            # For main episode with a music tail: hold the last frame for music_tail_seconds
            # so music can continue over a static frame before the end page. This is an
            # intentional outro pause (not a mid-content freeze).
            narr_dur = total_duration  # = sum of actual narration beat durations
            body_dur = narr_dur + music_tail_seconds
            if music_tail_seconds > 0.001:
                filter_parts.append(
                    f"{concat_inputs}concat=n={clip_count}:v=1:a=0[vconcat];"
                    f"[vconcat]trim=0:{narr_dur:.6f},setpts=PTS-STARTPTS[vbody];"
                    f"[vbody]tpad=stop_mode=clone:stop_duration={music_tail_seconds:.3f}[vout]"
                )
            else:
                filter_parts.append(
                    f"{concat_inputs}concat=n={clip_count}:v=1:a=0[vconcat];"
                    f"[vconcat]trim=0:{narr_dur:.6f},setpts=PTS-STARTPTS[vout]"
                )
        else:
            narr_dur = total_duration
            body_dur = narr_dur + music_tail_seconds
        video_map = "[vout]"
    else:
        # No clips — generate a black video of the required duration
        narr_dur = total_duration
        body_dur = narr_dur + music_tail_seconds
        inputs += [
            "-f", "lavfi",
            "-i", f"color=c=black:s={resolution}:r={FRAME_RATE}:d={body_dur:.3f}",
        ]
        video_map = "0:v"

    # Audio inputs
    # Count actual -i arguments (not len//2 — lavfi adds "-f lavfi -i" = 4 items for 1 input)
    audio_input_index = inputs.count("-i")
    has_narration = narration_path is not None and narration_path.exists()

    # Multi-segment music (Option B, 2026-07-19): only segments carrying a real,
    # on-disk track are used. When none do, this falls through to the legacy
    # single music_track path below unchanged.
    segments_with_tracks = [
        seg for seg in (music_segments or [])
        if seg.get("track") is not None and Path(seg["track"].local_path).exists()
    ]
    use_multi_segment_music = len(segments_with_tracks) > 0
    has_music = (not use_multi_segment_music) and music_track is not None and music_track.local_path.exists()
    has_music_any = use_multi_segment_music or has_music

    if has_narration:
        inputs += ["-i", str(narration_path)]
        narration_idx = audio_input_index
        audio_input_index += 1
    else:
        narration_idx = None

    msc_filter_str: Optional[str] = None
    if use_multi_segment_music:
        seg_inputs, msc_filter_str, audio_input_index = _build_multi_segment_music_filter(
            segments=segments_with_tracks,
            start_input_index=audio_input_index,
            music_tail_seconds=music_tail_seconds,
        )
        inputs += seg_inputs
    elif has_music:
        # Load music file once (no stream_loop — looping is done in the filter graph via
        # aloop to avoid including any end-of-track fade-out in the loop unit).
        inputs += ["-i", str(music_track.local_path)]
        music_idx = audio_input_index
        audio_input_index += 1
        # Probe the usable portion of the track (before any fade-out silence near the end).
        # aloop will loop only [0, usable_dur] to prevent the fade from re-appearing on
        # each loop boundary. usable_samples is the loop unit size in samples.
        _music_usable_dur = _probe_music_usable_duration(music_track.local_path)
        try:
            _music_sr_result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "stream=sample_rate",
                 "-of", "csv=p=0", str(music_track.local_path)],
                capture_output=True, text=True, timeout=10,
            )
            _music_sample_rate = int(_music_sr_result.stdout.strip().splitlines()[0])
        except Exception:
            _music_sample_rate = 44100
        _music_usable_samples = int(_music_usable_dur * _music_sample_rate)
    else:
        music_idx = None
        _music_usable_dur = 0.0
        _music_usable_samples = 0
        _music_sample_rate = 44100

    # Audio filter: mix narration + music.
    # narration is hard-trimmed to narr_dur (VO content only).
    # When music_tail_seconds > 0: narration is padded with silence to body_dur so
    # amix does not cut the music early when duration=first. Music plays at full level
    # through both the VO section and the music tail.
    if has_narration and has_music_any:
        if use_multi_segment_music:
            # msc_filter_str already ends in a [msc] label (see
            # _build_multi_segment_music_filter) — everything below is
            # byte-identical to the single-track path.
            _music_loop_prefix = msc_filter_str
        else:
            # Music loop prefix: loop only the usable portion of the track (before any
            # end-of-track fade-out). aloop=loop=-1:size=N loops the first N samples
            # indefinitely, then atrim caps the result to exactly body_dur.
            # This prevents the track's natural fade-out from reappearing on each loop.
            _music_loop_prefix = (
                f"[{music_idx}:a]"
                f"atrim=0:{_music_usable_dur:.6f},asetpts=PTS-STARTPTS,"
                f"aloop=loop=-1:size={_music_usable_samples},"
                f"atrim=0:{body_dur:.6f},asetpts=PTS-STARTPTS,"
                f"volume={_MUSIC_AMPLITUDE_FACTOR}[msc]"
            )
        if music_tail_seconds > 0.001:
            # apad=pad_dur unconditionally appends exactly music_tail_seconds of silence
            # after the narration content, regardless of declared stream duration.
            # apad=whole_dur=X is unreliable here because atrim/asetpts may strip the
            # duration metadata, causing apad to add no padding at all.
            filter_parts.append(
                f"[{narration_idx}:a]atrim=0:{narr_dur:.6f},asetpts=PTS-STARTPTS,"
                f"volume=1.0,apad=pad_dur={music_tail_seconds:.6f}[narr];"
                f"{_music_loop_prefix};"
                f"[narr][msc]amix=inputs=2:duration=longest:dropout_transition=0[aout]"
            )
        else:
            filter_parts.append(
                f"[{narration_idx}:a]atrim=0:{narr_dur:.6f},asetpts=PTS-STARTPTS,volume=1.0[narr];"
                f"{_music_loop_prefix};"
                f"[narr][msc]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            )
        audio_map = "[aout]"
    elif has_narration:
        filter_parts.append(
            f"[{narration_idx}:a]atrim=0:{body_dur:.6f},asetpts=PTS-STARTPTS,volume=1.0[aout]"
        )
        audio_map = "[aout]"
    elif has_music_any:
        if use_multi_segment_music:
            filter_parts.append(f"{msc_filter_str};[msc]anull[aout]")
        else:
            filter_parts.append(
                f"[{music_idx}:a]"
                f"atrim=0:{_music_usable_dur:.6f},asetpts=PTS-STARTPTS,"
                f"aloop=loop=-1:size={_music_usable_samples},"
                f"atrim=0:{body_dur:.6f},asetpts=PTS-STARTPTS,"
                f"volume={_MUSIC_AMPLITUDE_FACTOR}[aout]"
            )
        audio_map = "[aout]"
    else:
        # Generate silent audio
        inputs += [
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]
        audio_map = f"{audio_input_index}:a"

    # Loudnorm: target −16 LUFS / −1.5 TP for broadcast-safe output.
    # Single-pass mode — avoids a second encode while providing a reasonable
    # approximation of the integrated loudness target.
    if audio_map == "[aout]":
        filter_parts.append(
            "[aout]loudnorm=I=-16:TP=-1.5:LRA=11[aout_final]"
        )
        audio_map = "[aout_final]"

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
        "-ac", "2",
        "-crf", _CRF,
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-t", f"{body_dur:.6f}",
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
    music_track=None,
    body_duration: float = 0.0,
) -> bool:
    """
    Append a static end-page PNG card to the main episode video.

    When music_track is supplied the end-page clip carries music continuing from
    where the body left off (offset = body_duration % music_track_duration), so
    the music bed plays seamlessly through the end page without a cut.

    Two-pass strategy:
      1. Generate a short video clip from the PNG (same codec/resolution as main).
      2. Concat the body + end-page clip via filter_complex re-encode.

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

        # --- 2. Create end-page video clip (with music if available) ---
        has_ep_music = (
            music_track is not None
            and music_track.local_path.exists()
            and body_duration > 0
        )
        if has_ep_music:
            # Music continues seamlessly from where the body ended.
            # The body uses aloop over [0, usable_dur], so at body_duration the music
            # is at position (body_duration % usable_dur) within the usable portion.
            usable_dur = _probe_music_usable_duration(music_track.local_path)
            usable_dur = usable_dur if usable_dur > 1.0 else _probe_output_duration(music_track.local_path)
            music_offset = body_duration % usable_dur if usable_dur > 0 else 0.0
            # For the end page, seek to music_offset within [0, usable_dur] and loop
            # only the usable portion (via aloop) for the end-page duration.
            try:
                _ep_sr_result = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-show_entries", "stream=sample_rate",
                     "-of", "csv=p=0", str(music_track.local_path)],
                    capture_output=True, text=True, timeout=10,
                )
                _ep_sr = int(_ep_sr_result.stdout.strip().splitlines()[0])
            except Exception:
                _ep_sr = 44100
            # Remaining usable samples after music_offset, used as the aloop loop size
            ep_remaining = usable_dur - music_offset
            ep_remaining_samples = int(ep_remaining * _ep_sr)
            ep_usable_samples = int(usable_dur * _ep_sr)
            ep_cmd = [
                "ffmpeg", "-y",
                "-loop", "1",
                "-framerate", str(FRAME_RATE),
                "-t", f"{duration:.3f}",
                "-i", str(end_page_path),
                "-ss", f"{music_offset:.6f}",
                "-i", str(music_track.local_path),
                "-filter_complex",
                (
                    f"[0:v]scale={width}:{height}"
                    f":force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black[vout];"
                    f"[1:a]atrim=0:{ep_remaining:.6f},asetpts=PTS-STARTPTS,"
                    f"aloop=loop=-1:size={ep_usable_samples},"
                    f"atrim=0:{duration:.6f},asetpts=PTS-STARTPTS,"
                    f"volume={_MUSIC_AMPLITUDE_FACTOR}[aout]"
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
        else:
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
        #
        # -t cap: prevents audio tail bleed that extended final duration by ~16.6s.
        # Fade-out: 2s video+audio fade to black at the very end of the episode.
        total_dur = body_duration + duration
        fade_start = max(0.0, total_dur - 2.0)
        concat_cmd = [
            "ffmpeg", "-y",
            "-i", str(body_path),
            "-i", str(clip_path),
            "-filter_complex",
            (
                "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[vc][ac];"
                f"[vc]fade=t=out:st={fade_start:.3f}:d=2[vout];"
                f"[ac]afade=t=out:st={fade_start:.3f}:d=2[aout]"
            ),
            "-map", "[vout]",
            "-map", "[aout]",
            "-c:v", _VIDEO_CODEC,
            "-c:a", _AUDIO_CODEC,
            "-b:a", _AUDIO_BITRATE,
            "-ac", "2",
            "-crf", _CRF,
            "-pix_fmt", "yuv420p",
            "-t", f"{total_dur:.6f}",
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
