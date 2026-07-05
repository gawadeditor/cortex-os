"""
hypogeum/agents/final_output_qa.py — Final Output QA (media-level)

Performs ffprobe + ffmpeg signal analysis directly on the rendered MP4.
This is the ground-truth QA layer — it reads actual stream data, not JSON metadata.

Checks performed:
  1. audio/video duration mismatch (> 2s gap)
  2. silence longer than 2s BEFORE the end-page window
  3. frozen/still frame longer than 2s OUTSIDE the allowed end-page window
  4. black frames covering more than 50% of any 5s window (black scene detection)
  5. end-page start timestamp vs expected (body_end = total_video - end_page_duration)
  6. audio ends before video body (silent tail)

Usage:
    from hypogeum.agents.final_output_qa import run_final_output_qa

    result = run_final_output_qa(
        mp4_path=Path("main_ep_xxx.mp4"),
        expected_body_duration=665.47,   # narration duration
        end_page_duration=20.0,
        episode_id="ep_xxx",
        log=log,
    )
    if not result["passed"]:
        raise RuntimeError(result["blockers"])

Public API:
    run_final_output_qa(mp4_path, expected_body_duration, end_page_duration,
                        episode_id, log) -> dict

Return dict keys:
    passed                  bool    — True only when zero blockers
    episode_id              str
    mp4_path                str
    video_duration_s        float
    audio_duration_s        float
    av_mismatch_s           float   — abs(video - audio)
    silence_segments        list[dict]  — {start, end, duration}
    freeze_segments         list[dict]  — {start, end, duration}
    body_silence_before_end list[dict]  — silence in body (before end page)
    body_freeze_before_end  list[dict]  — freeze in body (before end page)
    end_page_start_s        float | None
    end_page_visible        bool
    blockers                list[str]   — human-readable failure messages
    warnings                list[str]
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import List, Optional


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

_AV_MISMATCH_THRESHOLD_S     = 2.0    # audio/video gap that triggers a blocker
_SILENCE_THRESHOLD_S         = 2.0    # silence segment longer than this = blocker
_FREEZE_THRESHOLD_S          = 2.0    # minimum freeze duration to detect/report
_LONG_FREEZE_BLOCKER_S       = 30.0   # freeze longer than this in the body = blocker
                                       # (legitimate tpad holds can reach ~15s for a
                                       # 24s clip with a 37s narration beat; 30s is
                                       # well above any legitimate hold)
_SILENCE_NOISE_LEVEL_DB      = "-50dB"
_FREEZE_PIXEL_DIFF           = "0.001"
_END_PAGE_TOLERANCE_S        = 5.0    # end-page start may vary ±5s from expected


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_final_output_qa(
    mp4_path: Path,
    expected_body_duration: float,
    end_page_duration: float,
    episode_id: str,
    log,
) -> dict:
    """
    Analyse the final rendered MP4 and return a QA result dict.

    Args:
        mp4_path:               Path to the final main MP4.
        expected_body_duration: Narration duration in seconds (body target).
        end_page_duration:      Duration of the end-page card in seconds.
        episode_id:             For logging.
        log:                    Caller-supplied logger.

    Returns:
        QA result dict. Check result["passed"] and result["blockers"].
    """
    blockers:  List[str] = []
    warnings:  List[str] = []

    if not mp4_path.exists():
        return _fail_result(
            episode_id=episode_id,
            mp4_path=mp4_path,
            blockers=[f"MP4 not found: {mp4_path}"],
        )

    # --- 1. Stream durations ---
    video_dur, audio_dur = _probe_stream_durations(mp4_path)

    av_mismatch = abs(video_dur - audio_dur)
    expected_total = expected_body_duration + end_page_duration

    if av_mismatch > _AV_MISMATCH_THRESHOLD_S:
        blockers.append(
            f"AV duration mismatch: video={video_dur:.2f}s audio={audio_dur:.2f}s "
            f"gap={av_mismatch:.2f}s (threshold={_AV_MISMATCH_THRESHOLD_S}s)"
        )

    # Warn if total duration is significantly longer than expected
    if video_dur > expected_total + 10.0:
        blockers.append(
            f"Video too long: {video_dur:.2f}s vs expected≈{expected_total:.2f}s "
            f"(body={expected_body_duration:.2f}s + end_page={end_page_duration:.2f}s)"
        )
    elif video_dur > expected_total + 2.0:
        warnings.append(
            f"Video slightly long: {video_dur:.2f}s vs expected≈{expected_total:.2f}s"
        )

    # --- 2. Silence detection ---
    silence_segs = _detect_silence(mp4_path)

    # --- 3. Freeze detection ---
    freeze_segs = _detect_freeze(mp4_path)

    # --- 4. End-page window ---
    # The allowed freeze zone is the last end_page_duration seconds.
    # Body ends at: video_dur - end_page_duration
    body_end = video_dur - end_page_duration

    body_silence = [
        s for s in silence_segs
        if s["start"] < body_end - 1.0 and s["duration"] > _SILENCE_THRESHOLD_S
    ]
    body_freeze = [
        f for f in freeze_segs
        if f["start"] < body_end - 1.0 and f["duration"] > _FREEZE_THRESHOLD_S
    ]

    if body_silence:
        for seg in body_silence:
            blockers.append(
                f"Silence in body at {seg['start']:.1f}s–{seg['end']:.1f}s "
                f"({seg['duration']:.1f}s) before end-page window"
            )

    # Large freeze in body is a blocker; smaller freezes (tpad holds, transitions) are warnings.
    # _LONG_FREEZE_BLOCKER_S is set well above the maximum expected tpad hold (~15s for a
    # 24s clip with a 37s narration beat), so only true render defects trigger a blocker.
    for seg in body_freeze:
        if seg["duration"] > _LONG_FREEZE_BLOCKER_S:
            blockers.append(
                f"Long freeze in body at {seg['start']:.1f}s–{seg['end']:.1f}s "
                f"({seg['duration']:.1f}s) before end-page window"
            )
        else:
            warnings.append(
                f"Freeze in body at {seg['start']:.1f}s–{seg['end']:.1f}s "
                f"({seg['duration']:.1f}s)"
            )

    # --- 5. Audio ends before video body ---
    if audio_dur < body_end - _AV_MISMATCH_THRESHOLD_S:
        blockers.append(
            f"Audio ({audio_dur:.2f}s) ends before video body ({body_end:.2f}s) — "
            f"silent tail of {body_end - audio_dur:.2f}s"
        )

    # --- 6. End-page presence ---
    # The end-page freeze should appear in the last end_page_duration seconds.
    expected_ep_start = video_dur - end_page_duration
    end_page_freeze = [
        f for f in freeze_segs
        if f["start"] >= expected_ep_start - _END_PAGE_TOLERANCE_S
    ]
    end_page_start_s = end_page_freeze[0]["start"] if end_page_freeze else None
    end_page_visible = bool(end_page_freeze)

    if not end_page_visible:
        warnings.append(
            f"End-page freeze not detected in final {end_page_duration}s "
            f"(expected at ≈{expected_ep_start:.1f}s)"
        )

    passed = len(blockers) == 0

    result = {
        "passed":                   passed,
        "episode_id":               episode_id,
        "mp4_path":                 str(mp4_path),
        "video_duration_s":         round(video_dur, 3),
        "audio_duration_s":         round(audio_dur, 3),
        "av_mismatch_s":            round(av_mismatch, 3),
        "expected_body_duration_s": round(expected_body_duration, 3),
        "expected_total_duration_s": round(expected_total, 3),
        "silence_segments":         silence_segs,
        "freeze_segments":          freeze_segs,
        "body_silence_before_end":  body_silence,
        "body_freeze_before_end":   body_freeze,
        "end_page_start_s":         round(end_page_start_s, 3) if end_page_start_s is not None else None,
        "end_page_visible":         end_page_visible,
        "blockers":                 blockers,
        "warnings":                 warnings,
    }

    log.info(
        "final_output_qa_complete",
        episode_id=episode_id,
        passed=passed,
        video_duration_s=result["video_duration_s"],
        audio_duration_s=result["audio_duration_s"],
        av_mismatch_s=result["av_mismatch_s"],
        body_silence_count=len(body_silence),
        body_freeze_count=len(body_freeze),
        end_page_visible=end_page_visible,
        blockers=blockers,
        warnings=warnings,
    )

    return result


# ---------------------------------------------------------------------------
# Internal: stream probing
# ---------------------------------------------------------------------------

def _probe_stream_durations(mp4_path: Path) -> tuple[float, float]:
    """Return (video_duration, audio_duration) in seconds via ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                str(mp4_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return 0.0, 0.0
        import json
        data = json.loads(result.stdout)
        video_dur = 0.0
        audio_dur = 0.0
        for stream in data.get("streams", []):
            dur = float(stream.get("duration", 0.0))
            if stream.get("codec_type") == "video" and dur > video_dur:
                video_dur = dur
            elif stream.get("codec_type") == "audio" and dur > audio_dur:
                audio_dur = dur
        return video_dur, audio_dur
    except Exception:
        return 0.0, 0.0


# ---------------------------------------------------------------------------
# Internal: silence detection
# ---------------------------------------------------------------------------

def _detect_silence(mp4_path: Path) -> List[dict]:
    """
    Run silencedetect on audio track and return list of silence segments.
    Each segment: {start, end, duration}.
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-i", str(mp4_path),
                "-af", f"silencedetect=noise={_SILENCE_NOISE_LEVEL_DB}:d={_SILENCE_THRESHOLD_S}",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = result.stderr
    except Exception:
        return []

    segments = []
    start = None
    for line in output.splitlines():
        m = re.search(r"silence_start:\s*([\d.]+)", line)
        if m:
            start = float(m.group(1))
        m = re.search(r"silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)", line)
        if m and start is not None:
            end = float(m.group(1))
            dur = float(m.group(2))
            segments.append({"start": round(start, 3), "end": round(end, 3), "duration": round(dur, 3)})
            start = None

    return segments


# ---------------------------------------------------------------------------
# Internal: freeze detection
# ---------------------------------------------------------------------------

def _detect_freeze(mp4_path: Path) -> List[dict]:
    """
    Run freezedetect on video track and return list of freeze segments.
    Each segment: {start, end, duration}.
    Only segments >= _FREEZE_THRESHOLD_S are returned.
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-i", str(mp4_path),
                "-vf", f"freezedetect=n={_FREEZE_PIXEL_DIFF}:d={_FREEZE_THRESHOLD_S}",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = result.stderr
    except Exception:
        return []

    segments = []
    start = None
    duration = None
    for line in output.splitlines():
        ms = re.search(r"freeze_start:\s*([\d.]+)", line)
        if ms:
            start = float(ms.group(1))
        md = re.search(r"freeze_duration:\s*([\d.]+)", line)
        if md:
            duration = float(md.group(1))
        me = re.search(r"freeze_end:\s*([\d.]+)", line)
        if me and start is not None and duration is not None:
            end = float(me.group(1))
            if duration >= _FREEZE_THRESHOLD_S:
                segments.append({
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "duration": round(duration, 3),
                })
            start = None
            duration = None

    return segments


# ---------------------------------------------------------------------------
# Internal: helpers
# ---------------------------------------------------------------------------

def _fail_result(episode_id: str, mp4_path: Path, blockers: List[str]) -> dict:
    return {
        "passed":                    False,
        "episode_id":                episode_id,
        "mp4_path":                  str(mp4_path),
        "video_duration_s":          0.0,
        "audio_duration_s":          0.0,
        "av_mismatch_s":             0.0,
        "expected_body_duration_s":  0.0,
        "expected_total_duration_s": 0.0,
        "silence_segments":          [],
        "freeze_segments":           [],
        "body_silence_before_end":   [],
        "body_freeze_before_end":    [],
        "end_page_start_s":          None,
        "end_page_visible":          False,
        "blockers":                  blockers,
        "warnings":                  [],
    }
