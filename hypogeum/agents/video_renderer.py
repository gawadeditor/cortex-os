"""
hypogeum/agents/video_renderer.py — Video Renderer (Priority 16 extraction)

Extracted from assembly_agent.py.

Owns:
- ffmpeg command construction
- concat/render execution
- audio mix execution (narration 0dB, music −18dB)
- main/short render file creation
- render_output_manifest construction

Coverage policy (2026-08-01, supersedes the interim setpts-only fix in
commit 44ae397): Video Renderer must never compensate for missing visual
duration. Every beat it renders is concatenated from real, distinct,
forward-only clips (TimelineEntry.clip_path + .extra_clip_paths), each
shown exactly once. The only permitted adjustment is a bounded speed
correction within coverage_check.MAX_EDITORIAL_SPEED_FACTOR (≈15%), applied
only to smooth a sub-beat mismatch on the last clip in a beat — never to
fill a real gap. If a beat still doesn't have enough duration at that
point, that is an upstream contract violation (Assembly Agent's coverage
gate should have already halted or filled it — see
hypogeum/agents/coverage_check.py and clip_repair_agent.fill_coverage_deficits())
and this module raises InsufficientCoverageError rather than concealing it
with tpad/loop/reverse/an uncapped setpts factor.

Single source of truth (2026-08-01): coverage_check.py is the ONLY module
that computes coverage. This module never re-derives which clips are
usable or how much duration is available — _build_ffmpeg_concat_command()
calls coverage_check.compute_beat_coverage() per beat and consumes its
usable_clips list directly. Video Renderer remains the final enforcement
point (it still actively checks and raises), but it is never a second
implementation of the coverage math itself.

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

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hypogeum.agents.coverage_check import MAX_EDITORIAL_SPEED_FACTOR, compute_beat_coverage
from hypogeum.agents.timeline_builder import TimelineEntry, TimelinePlan, sum_timeline_duration


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

MAIN_RESOLUTION  = "1920x1080"
SHORT_RESOLUTION = "1080x1920"
FRAME_RATE       = 30

# A short-native deliverable must stay under a minute (every social platform's
# Shorts/Reels ceiling, and the operator requirement). Narration is already
# word-budgeted to land well under this; this is the hard, deterministic
# backstop applied at render time so the invariant cannot be violated by a
# script that overran its word budget. Overrun is logged loudly.
SHORT_NATIVE_HARD_MAX_SECONDS = 58.0


class InsufficientCoverageError(Exception):
    """
    Raised when a beat reaches the Video Renderer without enough real,
    forward-only clip coverage to fill its narration duration within the
    approved editorial speed limit (coverage_check.MAX_EDITORIAL_SPEED_FACTOR).

    This should never happen in normal operation — Assembly Agent's
    pre-render coverage gate (hypogeum/agents/coverage_check.py) is
    responsible for catching this before render_episode() is ever called.
    Reaching this exception means that gate was bypassed. Video Renderer
    intentionally has no fallback filter (tpad/loop/reverse/uncapped
    setpts) for this case — per the 2026-08-01 policy, concealing missing
    footage is not an acceptable renderer behavior.
    """


class SubtitleFilterUnavailableError(Exception):
    """
    Raised when a caller supplied srt_path (burned-in captions were
    required, not optional) but the resolved ffmpeg binary's build has no
    'subtitles'/'ass' filter (no libass) to burn them with.

    2026-08-22 incident this fixes: ep_f3619022's 3 Shorts were rendered
    successfully (exit 0, valid playable output) with zero burned-in
    caption pixels and no error anywhere in the pipeline — the caption
    .srt/.ass assets existed and were correctly timed, but the renderer's
    ffmpeg build silently could not have burned them in even if the
    filtergraph reached it. See docs/operations/audits/
    EP_F3619022_ASSET_CONTINUITY_AND_BURNED_CAPTIONS_AUDIT_2026-08-22.md.
    Never catch this and fall back to a caption-less render — per this
    codebase's fail-closed convention, an environment that cannot fulfill
    a required capability must block, not degrade silently.
    """


class CaptionBurnInError(Exception):
    """
    Raised when srt_path was supplied (captions were required for this
    Short) but _build_caption_ass() failed to produce a usable .ass file.
    Never silently proceed to render without captions in this case.
    """


def _probe_subtitle_filter_available(ffmpeg_binary: str) -> bool:
    """
    Read-only capability probe: does `ffmpeg_binary -filters` list both the
    'ass' and 'subtitles' filters (both come from the same libass build
    flag, but checking both is cheap and avoids depending on ffmpeg
    exposing exactly one alias)? Not cached — called at most a handful of
    times per episode (once per Short), and caching across the process
    lifetime would make it untestable against multiple simulated ffmpeg
    binaries/capability states within the same test run.
    """
    try:
        result = subprocess.run(
            [ffmpeg_binary, "-filters"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    out = result.stdout
    return (" subtitles " in out or "subtitles " in out) and (" ass " in out or "ass " in out)


def assert_subtitle_filter_available(ffmpeg_binary: str) -> None:
    """
    Fail closed before ever attempting a caption burn-in: raises
    SubtitleFilterUnavailableError if `ffmpeg_binary` lacks libass. Called
    by _build_ffmpeg_concat_command() the moment a real srt_path is
    supplied — never after the fact, never caught-and-ignored by a caller
    that still proceeds to render without captions.
    """
    if not _probe_subtitle_filter_available(ffmpeg_binary):
        raise SubtitleFilterUnavailableError(
            f"ffmpeg binary {ffmpeg_binary!r} has no 'subtitles'/'ass' filter "
            f"(no libass in its build) -- burned-in captions were required for "
            f"this render and cannot be produced by this ffmpeg build. Set "
            f"Config.FFMPEG_BINARY to a libass-enabled build (e.g. Homebrew's "
            f"ffmpeg-full keg: /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg)."
        )


# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_VIDEO_CODEC           = "libx264"
_AUDIO_CODEC           = "aac"
_AUDIO_BITRATE         = "128k"
_CRF                   = "23"
_MUSIC_AMPLITUDE_FACTOR = 0.20    # ≈ −14 dB beneath narration (audible background bed)
# End-card music fade (2026-08-06, production-review directing rule): the ONE
# natural music fade-out for the whole episode starts exactly when the end
# card enters (body_duration) and reaches silence within the first few
# seconds of the end card — never at the tail of the whole file, and never
# paired with a video fade (see _append_end_page's concat step: video is a
# clean cut, audio fades independently on its own timeline).
_END_CARD_MUSIC_FADE_SECONDS = 2.0
# Was 2.5 — held the last frame on a frozen still while music played over it as an
# "outro pause" before the end page. Removed 2026-08-01: production review flagged the
# freeze-frame itself as an unwanted regression, not a designed transition. The final
# shot must finish normally and cut straight to the end page (see _append_end_page's
# music-continuity fix); the tpad/apad branches below stay in place, parameterized on
# this constant, in case a non-zero outro pause is ever reinstated deliberately.
_MUSIC_TAIL_SECONDS    = 0.0
_DEFAULT_TIMEOUT        = 600     # seconds
# Clean-cut policy (2026-08-06, production-review directing rule): the default
# transition everywhere is a clean cut. No fade=t=out/fade=t=in, no dissolve,
# no xfade, no dip-to-black may appear at a normal shot boundary or at the
# entrance to the end card — a visible fade is not a "smoothing" convenience,
# it is a transition, and the only transition this factory's channel policy
# permits is a clean cut. Audio smoothing (e.g. the end-card music fade in
# _append_end_page) is handled entirely independently and must never imply,
# require, or produce a black video frame. A former 2-frame tail micro-fade
# (_CLIP_TAIL_FADE_SECS) and end-page fade-in (_END_PAGE_FADE_IN_SECONDS)
# were removed for exactly this reason — see tests/test_video_renderer.py::
# TestCleanCutPolicy for the regression coverage proving no fade filter of
# any kind appears in the constructed filter graph at a normal shot boundary.


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
    short_srt_paths: Optional[Dict[str, Optional[Path]]] = None,
    ffmpeg_binary: str = "ffmpeg",
    main_vertical: bool = False,
) -> dict:
    """
    Execute all ffmpeg renders for one episode and return a render_output_manifest.

    main_vertical (SHORT_NATIVE mode): render the single main deliverable at
    SHORT_RESOLUTION (1080x1920, 9:16) with scale-to-fill/crop. There are no
    derived Shorts in this mode (timeline_plan.short_ids is empty).

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
        short_srt_paths:        Optional {short_id: Path | None}. When a
                                path is supplied and exists, that Short's
                                captions are burned into its video pixels
                                (see _build_subtitles_filter()) instead of
                                relying solely on platform CC. Main episode
                                is never affected — burned-in captions are
                                a Shorts-only requirement.

    Returns:
        render_output_manifest dict — factual record of what was produced.
        Does NOT include production_ready (that is Final Render Reviewer's job).
    """
    main_timeline   = timeline_plan.main_timeline
    shorts_timelines = timeline_plan.shorts_timelines
    short_ids        = timeline_plan.short_ids

    ffmpeg_errors: List[str] = []
    render_status = "success"

    main_resolution = SHORT_RESOLUTION if main_vertical else MAIN_RESOLUTION

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
    # The offset math in _append_end_page needs time elapsed *since this segment
    # started* looping, not raw elapsed time since t=0 of the whole body — under
    # Option B multi-segment music the segment actually playing at the end of the
    # body is not the one that started at t=0. Legacy single-track music has an
    # implicit segment start of 0.0 (it plays from t=0 for the whole body).
    end_page_music_segment_start = (
        _segments_with_tracks[-1]["start_seconds"] if _segments_with_tracks else 0.0
    )

    if not main_timeline:
        # A caller scoped this render_episode() call to zero main-episode
        # scenes (e.g. a Shorts-only pass over a filtered visual_package).
        # main_output_path is a fixed, deterministic path shared across
        # every call for this episode_id -- unconditionally invoking
        # _render_main_episode() here would write a near-empty file to
        # that same path and silently destroy any previously valid main
        # render that already exists there. Treat "zero main scenes this
        # call" as "main not requested this call", never as "render an
        # empty main episode": never touch main_output_path, and report
        # main_success from whatever (if anything) already exists there.
        main_success = main_output_path.exists() and main_output_path.stat().st_size > 0
        log.info(
            "assembly_main_timeline_empty_skipping_render",
            episode_id=episode_id,
            existing_main_present=main_success,
        )
    else:
        main_success = _render_main_episode(
            timeline=main_timeline,
            narration_path=main_narration_path,
            music_track=main_music_track,
            output_path=main_output_path,
            episode_id=episode_id,
            timeout=ffmpeg_timeout,
            log=log,
            music_segments=main_music_segments,
            ffmpeg_binary=ffmpeg_binary,
            resolution=main_resolution,
            vertical=main_vertical,
        )
    _end_page_seconds_added = 0.0
    if not main_timeline:
        pass
    elif not (main_success and main_output_path.exists() and main_output_path.stat().st_size > 0):
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
            music_segment_start_seconds=end_page_music_segment_start,
            ffmpeg_binary=ffmpeg_binary,
            resolution=main_resolution,
        )
        if not end_page_ok:
            # Non-fatal: log the failure but keep the body render as-is.
            log.warn(
                "assembly_end_page_append_failed",
                episode_id=episode_id,
                end_page_path=str(end_page_path),
            )
        else:
            _end_page_seconds_added = float(end_page_duration)
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
            ffmpeg_binary=ffmpeg_binary,
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
            srt_path=(short_srt_paths or {}).get(short_id),
            ffmpeg_binary=ffmpeg_binary,
        )
        if not (short_success and short_output_path.exists() and short_output_path.stat().st_size > 0):
            if render_status != "failed":
                render_status = "partial"
            ffmpeg_errors.append(f"short_{short_index}_render_failed")

    # Clip availability below 100% downgrades success → partial
    if render_status == "success" and clip_availability_rate < 1.0:
        render_status = "partial"

    # duration_coverage_gap = aggregate deficit from clips that EXIST but don't reach
    # their narration beat's duration even with any attached extra coverage clips and
    # the approved editorial speed factor (coverage_check.compute_beat_coverage — the
    # same math Assembly Agent's pre-render gate uses). Missing clips are already in
    # missing_clips and clip_availability_rate — including their time here double-counts.
    # Under the 2026-08-01 coverage policy this should be ~0 for every beat that reached
    # the renderer at all (a real deficit halts assembly before render_episode() runs).
    # max_scene_hold_seconds = worst-case single-scene deficit, used by the Final Render
    # Reviewer to match the Timeline QA per-scene threshold.
    existing_holds = [
        compute_beat_coverage(e).deficit_seconds
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
        main_resolution=main_resolution,
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
    # Seconds the end-page card actually added to the main render (0.0 when no
    # end page, or the append failed) — the caller folds this into the
    # recorded episode duration, which otherwise counts only the timeline.
    manifest["end_page_seconds"] = round(_end_page_seconds_added, 3)

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
    ffmpeg_binary: str = "ffmpeg",
    resolution: str = MAIN_RESOLUTION,
    vertical: bool = False,
) -> bool:
    """
    Render the main episode using ffmpeg. Default 16:9 (MAIN_RESOLUTION);
    when vertical=True (SHORT_NATIVE mode) the single deliverable is
    rendered 9:16 with scale-to-fill/crop, exactly like a Short.
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

    # Hard backstop: a short-native deliverable never exceeds a minute, no
    # matter what the script engine produced. The word budget should keep
    # narration well under this; if it didn't, cut and shout.
    if vertical and expected_duration > SHORT_NATIVE_HARD_MAX_SECONDS:
        log.warn(
            "short_native_duration_capped",
            episode_id=episode_id,
            narration_seconds=round(narr_duration, 2),
            expected_seconds=round(expected_duration, 2),
            capped_to=SHORT_NATIVE_HARD_MAX_SECONDS,
            reason="script overran the short-native word budget",
        )
        expected_duration = SHORT_NATIVE_HARD_MAX_SECONDS

    # Render to a raw temp path first, then always apply a duration trim pass.
    # This guarantees the final body is exactly expected_duration regardless of
    # any ffmpeg 8.x filter-graph or -t interaction issues.
    raw_output = output_path.with_suffix(".body_raw.mp4")

    cmd = _build_ffmpeg_concat_command(
        timeline=timeline,
        narration_path=narration_path,
        music_track=music_track,
        output_path=raw_output,
        resolution=resolution,
        episode_id=episode_id,
        music_tail_seconds=_MUSIC_TAIL_SECONDS,
        music_segments=music_segments,
        ffmpeg_binary=ffmpeg_binary,
        is_short=vertical,
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
        ffmpeg_binary=ffmpeg_binary,
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
    ffmpeg_binary: str = "ffmpeg",
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
                ffmpeg_binary, "-i", str(output_path),
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


def _probe_music_usable_duration(music_path: Path, ffmpeg_binary: str = "ffmpeg") -> float:
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
            [ffmpeg_binary, "-i", str(music_path),
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
    ffmpeg_binary: str = "ffmpeg",
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
            ffmpeg_binary, "-y",
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
    srt_path: Optional[Path] = None,
    ffmpeg_binary: str = "ffmpeg",
) -> bool:
    """
    Render one Short (9:16) using ffmpeg.
    Returns True on success, False on failure.

    srt_path: When supplied and the file exists, captions are burned into
    the video pixels — see _build_subtitles_filter(). Shorts must not
    depend on platform CC (2026-08-01 quality rejection).
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
        srt_path=srt_path,
        ffmpeg_binary=ffmpeg_binary,
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


# Burned-in caption styling for Shorts (9:16) — high-contrast white fill,
# black outline, bottom-center, kept clear of the bottom ~14.6% of frame
# height where TikTok/Reels/Shorts platform UI (username, action buttons,
# CTA) typically overlaps the video.
#
# A plain .srt has no embedded PlayResX/PlayResY, so ffmpeg's subtitles
# filter (via libass) falls back to an internal default script resolution
# when converting it. force_style values (FontSize, MarginV, ...) are
# interpreted in THAT reference space, not the real output resolution —
# confirmed empirically (2026-08-01): burning a raw .srt with
# original_size=1080x1920 + force_style still rendered the text gigantic
# and pinned to the top of frame, because original_size scales for a
# *mismatched authoring resolution*, it does not establish one for a
# plain SRT with none. The reliable fix is to convert the SRT to a
# minimal ASS document with an explicit [Script Info] PlayResX/PlayResY
# matching the actual render resolution and the style baked into a
# [V4+ Styles] Style line — the same native libass renderer, just given
# an unambiguous coordinate system. Verified empirically to render
# correctly positioned, high-contrast text at the intended size.
_CAPTION_FONT_SIZE  = 48
_CAPTION_MARGIN_V   = 280
_CAPTION_OUTLINE_PX = 3

_SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _srt_timestamp_to_ass(hh: str, mm: str, ss: str, ms: str) -> str:
    """SRT 'HH:MM:SS,mmm' -> ASS 'H:MM:SS.cc' (centiseconds, single-digit hour)."""
    centiseconds = int(ms) // 10
    return f"{int(hh)}:{mm}:{ss}.{centiseconds:02d}"


def _parse_srt_cues(srt_text: str) -> List[Tuple[str, str, str]]:
    """
    Parse SRT text into a list of (ass_start, ass_end, text) tuples.
    Multi-line cue text is joined with ASS's \\N line-break escape.
    Skips malformed blocks rather than raising — a burned-in caption
    render must never fail the whole assembly over one bad cue.
    """
    cues: List[Tuple[str, str, str]] = []
    blocks = re.split(r"\r?\n\r?\n+", srt_text.strip())
    for block in blocks:
        lines = [l for l in block.splitlines() if l.strip() != ""]
        if len(lines) < 2:
            continue
        # First line is the cue index (if numeric) or already the timing line.
        timing_line_idx = 0
        if lines[0].strip().isdigit():
            timing_line_idx = 1
        if timing_line_idx >= len(lines):
            continue
        m = _SRT_TIME_RE.search(lines[timing_line_idx])
        if not m:
            continue
        start = _srt_timestamp_to_ass(*m.groups()[0:4])
        end = _srt_timestamp_to_ass(*m.groups()[4:8])
        text_lines = lines[timing_line_idx + 1:]
        if not text_lines:
            continue
        text = "\\N".join(t.strip() for t in text_lines)
        cues.append((start, end, text))
    return cues


def _build_caption_ass(srt_path: Path, resolution: str, dest_path: Path) -> bool:
    """
    Convert srt_path into a minimal ASS file at dest_path with explicit
    PlayResX/PlayResY matching `resolution` (WxH) and the burned-in
    caption Style baked in. Returns True on success (dest_path written
    with at least one cue), False on any failure (missing/empty/
    unparseable source) — callers must treat False as "no captions to
    burn", never as a hard error.
    """
    try:
        srt_text = srt_path.read_text(encoding="utf-8")
    except Exception:
        return False

    cues = _parse_srt_cues(srt_text)
    if not cues:
        return False

    width, height = resolution.split("x")
    style = (
        f"Style: Default,Arial,{_CAPTION_FONT_SIZE},"
        "&H00FFFFFF,"   # PrimaryColour: white fill, opaque
        "&H000000FF,"   # SecondaryColour (karaoke, unused)
        "&H00000000,"   # OutlineColour: black, opaque
        "&H00000000,"   # BackColour (unused, BorderStyle=1 has no box)
        "1,0,0,0,"       # Bold,Italic,Underline,StrikeOut
        "100,100,0,0,"   # ScaleX,ScaleY,Spacing,Angle
        "1,"             # BorderStyle: outline + shadow, not an opaque box
        f"{_CAPTION_OUTLINE_PX},0,"  # Outline,Shadow
        "2,"             # Alignment: bottom-center
        f"40,40,{_CAPTION_MARGIN_V},1"  # MarginL,MarginR,MarginV,Encoding
    )
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        style,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for start, end, text in cues:
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _escape_ffmpeg_filter_path(path: Path) -> str:
    """
    Escape a filesystem path for use inside an ffmpeg filtergraph string
    argument (e.g. subtitles=filename='...'). Per ffmpeg's own documented
    convention: backslashes and colons must be backslash-escaped, and the
    whole value is then wrapped in single quotes by the caller — so any
    literal single quote in the path must also be escaped.
    """
    s = str(path)
    s = s.replace("\\", "\\\\")
    s = s.replace(":", "\\:")
    s = s.replace("'", "\\'")
    return s


def _build_subtitles_filter(ass_path: Path) -> str:
    """Return the `subtitles=...` ffmpeg filter fragment (no leading input label) for burning ass_path in. Style is already baked into ass_path — see _build_caption_ass()."""
    escaped = _escape_ffmpeg_filter_path(ass_path)
    return f"subtitles=filename='{escaped}'"


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
    srt_path: Optional[Path] = None,
    ffmpeg_binary: str = "ffmpeg",
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
    - amix with duration=longest: the mix runs as long as its longest real
      input. Music is always looped/atrim'd to body_dur regardless of the
      source track's length, so it is the side that guarantees full
      coverage; narration is real, unlooped audio that can legitimately
      run a little short of its planned atrim endpoint (TTS output timing
      vs. planned beat duration), and duration=longest lets the mix keep
      going on music alone for any such shortfall instead of truncating
      the whole mix early (see the fix note at the call site below).

    music_segments (Option B, 2026-07-19, main episode only): when supplied
    and at least one segment carries a real track, the [msc] label is built
    by _build_multi_segment_music_filter() instead of the single-track loop
    below — each segment is looped/trimmed to its own [start, end) window
    and adelay-shifted into place, then summed into one [msc] stream. Every
    line downstream of [msc] (the narration mix, loudnorm) is unchanged.
    Falls back to the single music_track path when no segment has a track
    (empty/None music_segments, or an older single-key music_track_package).

    srt_path: When supplied and the file exists, burns the SRT captions into
    the video pixels via ffmpeg's `subtitles` filter — readable without
    sound and independent of platform CC, which is required for Shorts
    (2026-08-01 quality rejection: Shorts had platform CC only, no
    burned-in text). Styled high-contrast (white fill, black outline) and
    kept clear of the bottom UI-overlap zone on 9:16 delivery.
    """
    width, height = resolution.split("x")

    inputs: List[str] = []
    filter_parts: List[str] = []
    has_clips = any(e.clip_path is not None for e in timeline)

    total_duration = sum_timeline_duration(timeline)
    if total_duration <= 0:
        total_duration = 30.0  # fallback for empty timelines

    if has_clips:
        # Coverage policy (2026-08-01): every beat is concatenated from real,
        # distinct, forward-only clips — the beat's primary clip plus any
        # TimelineEntry.extra_clip_paths already attached by Assembly Agent's
        # pre-render coverage gate (see hypogeum/agents/coverage_check.py and
        # clip_repair_agent.fill_coverage_deficits()). Every clip is shown
        # in full, once, forward, at normal-or-near-normal speed:
        #   - Earlier clips in a beat (if any extras) play at their full
        #     native length — no trim, no stretch.
        #   - The LAST clip in a beat absorbs whatever small adjustment is
        #     needed to land exactly on the beat's duration: trimmed down if
        #     the clips ran long, or given a bounded speed nudge (capped at
        #     MAX_EDITORIAL_SPEED_FACTOR, ~15%) if they're still a hair
        #     short. This is a minor sync correction, never a coverage
        #     strategy — see InsufficientCoverageError below.
        # After concat: hard-trim the video stream to total_duration and reset PTS.
        # This is a redundant safety guard — in ffmpeg 8.1 with large filter graphs
        # (35+ inputs), the -t output flag can fail to limit the rendered body.
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
                # Coverage is computed ONCE, by coverage_check.py, and consumed here —
                # not recomputed. usable_clips is the canonical, already-filtered
                # (missing/dry-run excluded) list of (path, real_duration) pairs;
                # video_renderer never re-derives which clips are usable or how much
                # duration is available, it only enforces the decision.
                beat_coverage = compute_beat_coverage(entry)

                if not beat_coverage.sufficient:
                    raise InsufficientCoverageError(
                        f"scene {entry.scene_id}: available "
                        f"{beat_coverage.available_duration_seconds:.3f}s of real footage "
                        f"({len(beat_coverage.usable_clips)} clip(s)) cannot reach the "
                        f"{entry.duration_seconds:.3f}s narration beat even at the maximum "
                        f"editorial speed factor ({MAX_EDITORIAL_SPEED_FACTOR}x) — deficit "
                        f"{beat_coverage.deficit_seconds:.3f}s. Assembly Agent's coverage "
                        f"gate should have blocked this beat before render."
                    )

                beat_clip_paths: List[Path] = [p for p, _ in beat_coverage.usable_clips]
                beat_clip_durations: List[float] = [d for _, d in beat_coverage.usable_clips]

                for i, (clip_path, clip_dur) in enumerate(zip(beat_clip_paths, beat_clip_durations)):
                    inputs += ["-i", str(clip_path)]
                    is_last = i == len(beat_clip_paths) - 1

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

                    if not is_last:
                        # Earlier clip in a multi-clip beat: full native length,
                        # forward, shown exactly once. Clean cut into the next
                        # clip — no fade of any kind at this boundary.
                        out_label = f"[pv{clip_idx}]"
                        tpad_filter_parts.append(f"{scale_label}null{out_label}")
                        concat_input_labels.append(out_label)
                    else:
                        # Last (or only) clip in the beat: land exactly on the
                        # beat's remaining duration after the earlier clips.
                        remaining_for_last = max(
                            0.05, entry.duration_seconds - sum(beat_clip_durations[:-1])
                        )
                        if remaining_for_last <= clip_dur + 0.001:
                            # Clips ran long (or landed exactly): trim the tail.
                            # Cutting real excess footage is not "compensating for
                            # missing duration" — there was never a deficit here.
                            # Clean cut at the trim point — no fade.
                            trim_label = f"[tv{clip_idx}]"
                            tpad_filter_parts.append(
                                f"{scale_label}trim=duration={remaining_for_last:.3f},"
                                f"setpts=PTS-STARTPTS{trim_label}"
                            )
                            concat_input_labels.append(trim_label)
                        else:
                            # Still a hair short even with every available clip:
                            # bounded speed nudge only (guaranteed <= MAX_EDITORIAL_
                            # SPEED_FACTOR by the check above — this is a sync
                            # correction, not a way to fill a real gap). Never
                            # reverse, never loop/repeat. Clean cut — no fade.
                            out_label = f"[pv{clip_idx}]"
                            factor = remaining_for_last / max(clip_dur, 0.001)
                            tpad_filter_parts.append(
                                f"{scale_label}setpts={factor:.6f}*PTS,"
                                f"fps=fps={FRAME_RATE}{out_label}"
                            )
                            concat_input_labels.append(out_label)
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

    if srt_path is not None and srt_path.exists():
        # Fail closed rather than silently produce a caption-less render: a
        # caller that supplied srt_path explicitly wants burned-in captions,
        # so an ffmpeg build without the subtitles/ass filter (no libass)
        # must never be allowed to quietly drop them -- see
        # assert_subtitle_filter_available()'s own docstring for the
        # 2026-08-22 incident this fixes (ep_f3619022's 3 Shorts rendered
        # successfully with zero burned-in captions and no error anywhere).
        assert_subtitle_filter_available(ffmpeg_binary)
        ass_path = srt_path.with_suffix(".ass")
        if not _build_caption_ass(srt_path, resolution, ass_path):
            raise CaptionBurnInError(
                f"srt_path={srt_path} was supplied but _build_caption_ass() failed to "
                f"produce a valid .ass file at {ass_path} -- captions were required, "
                f"not optional, for this render"
            )
        filter_parts.append(
            f"{video_map}{_build_subtitles_filter(ass_path)}[vsub]"
        )
        video_map = "[vsub]"

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
    # amix does not cut the music early. Music plays at full level through both
    # the VO section and the music tail.
    #
    # duration=longest (not duration=first) in BOTH branches below (2026-08-07
    # fix): narration's atrim target (narr_dur) is the TIMELINE's beat-duration
    # sum, not a guarantee about the real stitched narration file's actual
    # length -- TTS-rendered audio can legitimately come in a little short of
    # its planned beat duration. atrim cannot manufacture samples that were
    # never recorded, so when that happens the narration stream ends early,
    # inside the filtergraph, regardless of its declared atrim endpoint.
    # duration=first ties the WHOLE mix's length to that (possibly-truncated)
    # first input, silencing the music bed too for the remainder -- silence
    # exactly at the body's tail, i.e. exactly at the seam into the end page
    # (confirmed on ep_f3619022: narration ran ~0.84s short of the video's
    # frame-accurate body duration). Music is always looped via aloop to
    # cover the full body_dur regardless of its source length, so it is
    # never the one that's short; duration=longest lets the mix continue
    # using music alone for however much of body_dur narration didn't
    # reach, with no video-timing change and no fade/pad of any kind --
    # the pre-existing "-t body_dur" cap on the whole command already
    # bounds the result exactly as before whenever there is no shortfall.
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
                f"[narr][msc]amix=inputs=2:duration=longest:dropout_transition=2[aout]"
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

    cmd: List[str] = [ffmpeg_binary, "-y"]
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
    music_segment_start_seconds: float = 0.0,
    ffmpeg_binary: str = "ffmpeg",
    resolution: str = MAIN_RESOLUTION,
) -> bool:
    """
    Append a static end-page PNG card to the main episode video.

    When music_track is supplied the end-page clip carries music continuing from
    where the body left off (offset = elapsed % usable_dur, where elapsed is time
    since the currently-playing segment started — NOT raw body_duration; under
    Option B multi-segment music the last segment's own aloop clock starts at
    music_segment_start_seconds, not t=0 of the body), so the music bed plays
    seamlessly through the end page without a cut, restart, or jump to a
    different position in the track.

    Two-pass strategy:
      1. Generate a short video clip from the PNG (same codec/resolution as main).
      2. Concat the body + end-page clip via filter_complex re-encode.

    Both clips are encoded at `resolution` (default MAIN_RESOLUTION 1920x1080;
    the caller passes SHORT_RESOLUTION 1080x1920 for a SHORT_NATIVE vertical
    main deliverable) with yuv420p / libx264 / aac — the end-page clip MUST
    match the body's dimensions or the filter_complex concat fails with
    "Input link parameters ... do not match".

    Returns True on success, False on any failure. Non-fatal — caller logs a warn.
    """
    body_path  = main_output_path.with_suffix(".body.mp4")
    clip_path  = main_output_path.with_suffix(".endpage.mp4")
    list_path  = main_output_path.with_suffix(".concat.txt")
    width, height = resolution.split("x")

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
            # The playing segment's aloop clock starts at music_segment_start_seconds
            # (0.0 for legacy single-track music, which plays from t=0 of the body),
            # not at t=0 of the body itself — so the elapsed time to fold into the
            # loop is (body_duration - music_segment_start_seconds), matching exactly
            # how _build_multi_segment_music_filter / the single-track loop in
            # _build_ffmpeg_concat_command position that segment's own aloop.
            # _probe_music_usable_duration must be called the same way here as it is
            # for the body's own loop (no extra fallback) so both sides agree on
            # exactly the same usable_dur — any divergence reintroduces a jump.
            usable_dur = _probe_music_usable_duration(music_track.local_path)
            elapsed = max(0.0, body_duration - music_segment_start_seconds)
            music_offset = elapsed % usable_dur if usable_dur > 0 else 0.0
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
            # The ONE natural music fade-out for the whole episode: starts at
            # st=0 of this clip, which IS the end-card entrance (this clip's
            # own timeline begins exactly where the body ends), and reaches
            # silence well before the end card's own duration is over.
            # Independent of video — video is a clean cut (no fade=t=in below).
            _ep_music_fade = min(_END_CARD_MUSIC_FADE_SECONDS, max(0.01, duration))
            ep_cmd = [
                ffmpeg_binary, "-y",
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
                    f"volume={_MUSIC_AMPLITUDE_FACTOR},"
                    f"afade=t=out:st=0.000:d={_ep_music_fade:.3f}[aout]"
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
                ffmpeg_binary, "-y",
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
        # No video or audio fade here (2026-08-06 directing-rule fix): the body
        # cuts cleanly into the end-page clip (no video fade of any kind), and
        # the end-page clip already carries its own single music fade-out,
        # positioned at ITS OWN t=0 (the end-card entrance) in the ep_cmd step
        # above — adding a second fade here would stack two fades on the same
        # music bed, which is exactly what this policy forbids.
        total_dur = body_duration + duration
        concat_cmd = [
            ffmpeg_binary, "-y",
            "-i", str(body_path),
            "-i", str(clip_path),
            "-filter_complex",
            "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[vout][aout]",
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
    main_resolution: str = MAIN_RESOLUTION,
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
        "main_resolution":  main_resolution,
        "short_resolution": SHORT_RESOLUTION,
        "frame_rate":       FRAME_RATE,
    }
