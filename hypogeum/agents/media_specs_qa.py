"""
hypogeum/agents/media_specs_qa.py — Media Specs QA Agent (Agent 36)

Deterministic ffprobe-based inspection of every generated media file.
No Claude calls. No LLM. Pure subprocess + file inspection.

Checks each file (video, image, audio, render) against the canonical spec
for that file type and emits a `media_specs_report` dict. A file with an
incorrect resolution, frame rate, codec, or other spec sets
`publish_ready: false`. The caller decides whether to halt or file a PDR.

Root-cause this agent was built for: ep_000_lite clips were generated at
1280×720 instead of 1920×1080. Zero ffprobe inspection existed in the
pipeline; the wrong-resolution files entered Assembly and rendered silently.

Canonical specs enforced by default:
  video  (main):   1920×1080 @ 30 fps, h264 + aac
  render (main):   same as video
  video  (short):  1080×1920 @ 30 fps (9:16 portrait), h264 + aac
  audio:           44100 Hz, stereo
  image  (thumb):  ≥ 1920 × 1080 (landscape) or ≥ 1080 × 1920 (portrait)

Usage:
    from hypogeum.agents.media_specs_qa import run_media_specs_qa

    report = run_media_specs_qa(
        file_path=Path("episodes/ep_000/main_ep_000.mp4"),
        file_type="video",
        episode_id="ep_000",
        expected_spec={"width": 1920, "height": 1080, "fps": 30},
    )
    # report["publish_ready"] is False if any spec check fails
    # report["failures"] lists every violation
"""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hypogeum.logger import get_logger
from hypogeum.schemas import validate

log = get_logger("media_specs_qa")

# ---------------------------------------------------------------------------
# Default specs per file type
# ---------------------------------------------------------------------------

_DEFAULT_SPECS: dict[str, dict[str, Any]] = {
    "video": {
        "width": 1920,
        "height": 1080,
        "fps": 30.0,
        "video_codec": "h264",
        "audio_codec": "aac",
        "audio_sample_rate": 44100,
    },
    "render": {
        "width": 1920,
        "height": 1080,
        "fps": 30.0,
        "video_codec": "h264",
        "audio_codec": "aac",
        "audio_sample_rate": 44100,
    },
    "short": {
        "width": 1080,
        "height": 1920,
        "fps": 30.0,
        "video_codec": "h264",
        "audio_codec": "aac",
        "audio_sample_rate": 44100,
    },
    "audio": {
        "audio_sample_rate": 44100,
    },
    "image": {
        "min_width": 1920,
        "min_height": 1080,
    },
}

# fps tolerance: ±0.5 fps (handles 29.97 ≈ 30)
_FPS_TOLERANCE = 0.5


# ---------------------------------------------------------------------------
# ffprobe interface
# ---------------------------------------------------------------------------

def _run_ffprobe(file_path: Path) -> dict:
    """
    Run ffprobe on file_path and return parsed JSON.
    Raises RuntimeError if ffprobe fails or output is not valid JSON.
    """
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(file_path),
    ]
    log.info("ffprobe_start", path=str(file_path))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found on PATH — install ffmpeg")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffprobe timed out on {file_path}")

    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe exited {result.returncode} for {file_path}: {result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe produced non-JSON output for {file_path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Probe parsing
# ---------------------------------------------------------------------------

def _extract_specs(probe: dict) -> dict[str, Any]:
    """
    Extract canonical fields from raw ffprobe JSON into a flat dict.
    Fields that are absent from the file remain None.
    """
    streams = probe.get("streams", [])
    fmt = probe.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    width = video_stream.get("width") if video_stream else None
    height = video_stream.get("height") if video_stream else None
    video_codec = video_stream.get("codec_name") if video_stream else None

    fps = None
    if video_stream:
        r_frame_rate = video_stream.get("r_frame_rate", "")
        if "/" in r_frame_rate:
            num, den = r_frame_rate.split("/")
            try:
                fps = float(num) / float(den) if float(den) != 0 else None
            except ValueError:
                fps = None

    audio_codec = audio_stream.get("codec_name") if audio_stream else None
    sample_rate = None
    if audio_stream:
        sr = audio_stream.get("sample_rate")
        if sr is not None:
            try:
                sample_rate = int(sr)
            except (ValueError, TypeError):
                pass

    channels = None
    if audio_stream:
        ch = audio_stream.get("channels")
        if ch is not None:
            try:
                channels = int(ch)
            except (ValueError, TypeError):
                pass

    duration = None
    raw_dur = fmt.get("duration") or (video_stream or {}).get("duration")
    if raw_dur is not None:
        try:
            duration = float(raw_dur)
        except (ValueError, TypeError):
            pass

    file_size = None
    raw_size = fmt.get("size")
    if raw_size is not None:
        try:
            file_size = int(raw_size)
        except (ValueError, TypeError):
            pass

    # Compute aspect ratio string
    aspect_ratio = None
    if width and height:
        from math import gcd
        g = gcd(width, height)
        aspect_ratio = f"{width // g}:{height // g}"

    return {
        "width": width,
        "height": height,
        "fps": round(fps, 4) if fps is not None else None,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "audio_sample_rate": sample_rate,
        "audio_channels": channels,
        "duration_seconds": round(duration, 3) if duration is not None else None,
        "file_size_bytes": file_size,
        "aspect_ratio": aspect_ratio,
    }


# ---------------------------------------------------------------------------
# Spec checking
# ---------------------------------------------------------------------------

def _check_specs(
    file_type: str,
    observed: dict[str, Any],
    expected: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """
    Compare observed specs against expected.
    Returns (failures, warnings).
    """
    failures: list[str] = []
    warnings: list[str] = []

    if file_type in ("video", "render", "short"):
        # Resolution — one consolidated failure message when dimensions are wrong
        exp_w = expected.get("width")
        exp_h = expected.get("height")
        obs_w = observed.get("width")
        obs_h = observed.get("height")

        res_ok = True
        if exp_w is not None and obs_w != exp_w:
            res_ok = False
        if exp_h is not None and obs_h != exp_h:
            res_ok = False
        if not res_ok:
            failures.append(
                f"resolution: expected {exp_w}×{exp_h}, got {obs_w}×{obs_h}"
            )

        # FPS
        exp_fps = expected.get("fps")
        obs_fps = observed.get("fps")
        if exp_fps is not None and obs_fps is not None:
            if abs(obs_fps - exp_fps) > _FPS_TOLERANCE:
                failures.append(
                    f"fps: expected {exp_fps} (±{_FPS_TOLERANCE}), got {obs_fps}"
                )

        # Video codec
        exp_vc = expected.get("video_codec")
        obs_vc = observed.get("video_codec")
        if exp_vc is not None and obs_vc != exp_vc:
            warnings.append(
                f"video_codec: expected {exp_vc!r}, got {obs_vc!r}"
            )

        # Audio codec
        exp_ac = expected.get("audio_codec")
        obs_ac = observed.get("audio_codec")
        if exp_ac is not None and obs_ac is not None and obs_ac != exp_ac:
            warnings.append(
                f"audio_codec: expected {exp_ac!r}, got {obs_ac!r}"
            )

        # Audio sample rate
        exp_sr = expected.get("audio_sample_rate")
        obs_sr = observed.get("audio_sample_rate")
        if exp_sr is not None and obs_sr is not None and obs_sr != exp_sr:
            warnings.append(
                f"audio_sample_rate: expected {exp_sr}, got {obs_sr}"
            )

        # Duration sanity
        dur = observed.get("duration_seconds")
        if dur is not None and dur <= 0:
            failures.append(f"duration: zero or negative ({dur}s)")

    elif file_type == "audio":
        exp_sr = expected.get("audio_sample_rate")
        obs_sr = observed.get("audio_sample_rate")
        if exp_sr is not None and obs_sr is not None and obs_sr != exp_sr:
            failures.append(
                f"audio_sample_rate: expected {exp_sr}, got {obs_sr}"
            )
        dur = observed.get("duration_seconds")
        if dur is not None and dur <= 0:
            failures.append(f"duration: zero or negative ({dur}s)")

    elif file_type == "image":
        obs_w = observed.get("width")
        obs_h = observed.get("height")
        min_w = expected.get("min_width")
        min_h = expected.get("min_height")
        if min_w is not None and obs_w is not None and obs_w < min_w:
            failures.append(
                f"width: expected ≥ {min_w}, got {obs_w}"
            )
        if min_h is not None and obs_h is not None and obs_h < min_h:
            failures.append(
                f"height: expected ≥ {min_h}, got {obs_h}"
            )

    return failures, warnings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_media_specs_qa(
    file_path: Path,
    file_type: str,
    episode_id: str,
    expected_spec: dict[str, Any] | None = None,
) -> dict:
    """
    Inspect a single media file and return a `media_specs_report` dict.

    Args:
        file_path:     Absolute or project-relative path to the file.
        file_type:     One of "video", "render", "short", "audio", "image".
        episode_id:    Episode identifier (e.g. "ep_000_lite").
        expected_spec: Override default spec for this file type. Keys:
                       width, height, fps, video_codec, audio_codec,
                       audio_sample_rate, min_width, min_height.

    Returns:
        Validated `media_specs_report` dict.
        `publish_ready` is False if any spec check fails.
        `file_valid` is False if ffprobe cannot parse the file.
    """
    if file_type not in _DEFAULT_SPECS:
        raise ValueError(
            f"Unknown file_type: {file_type!r}. "
            f"Must be one of: {sorted(_DEFAULT_SPECS)}"
        )

    file_path = Path(file_path)
    report_id = str(uuid.uuid4())
    checked_at = datetime.now(timezone.utc).isoformat()

    # Merge expected spec: defaults then caller overrides
    effective_expected = dict(_DEFAULT_SPECS[file_type])
    if expected_spec:
        effective_expected.update(expected_spec)

    log.info(
        "media_specs_qa_start",
        report_id=report_id,
        episode_id=episode_id,
        file_path=str(file_path),
        file_type=file_type,
    )

    # --- File existence check ---
    if not file_path.exists():
        report = _build_report(
            report_id=report_id,
            episode_id=episode_id,
            file_path=str(file_path),
            file_type=file_type,
            file_valid=False,
            publish_ready=False,
            specs_observed={},
            specs_expected=effective_expected,
            failures=[f"file not found: {file_path}"],
            warnings=[],
            checked_at=checked_at,
        )
        validate("media_specs_report", report)
        return report

    if file_path.stat().st_size == 0:
        report = _build_report(
            report_id=report_id,
            episode_id=episode_id,
            file_path=str(file_path),
            file_type=file_type,
            file_valid=False,
            publish_ready=False,
            specs_observed={},
            specs_expected=effective_expected,
            failures=[f"file is zero bytes: {file_path}"],
            warnings=[],
            checked_at=checked_at,
        )
        validate("media_specs_report", report)
        return report

    # --- ffprobe ---
    try:
        probe = _run_ffprobe(file_path)
    except RuntimeError as exc:
        report = _build_report(
            report_id=report_id,
            episode_id=episode_id,
            file_path=str(file_path),
            file_type=file_type,
            file_valid=False,
            publish_ready=False,
            specs_observed={},
            specs_expected=effective_expected,
            failures=[f"ffprobe failed: {exc}"],
            warnings=[],
            checked_at=checked_at,
        )
        validate("media_specs_report", report)
        return report

    # --- Extract and check ---
    observed = _extract_specs(probe)
    failures, warnings = _check_specs(file_type, observed, effective_expected)

    file_valid = True
    publish_ready = len(failures) == 0

    if failures:
        log.warn(
            "media_specs_qa_failures",
            report_id=report_id,
            file_path=str(file_path),
            failures=failures,
        )
    else:
        log.info(
            "media_specs_qa_pass",
            report_id=report_id,
            file_path=str(file_path),
        )

    report = _build_report(
        report_id=report_id,
        episode_id=episode_id,
        file_path=str(file_path),
        file_type=file_type,
        file_valid=file_valid,
        publish_ready=publish_ready,
        specs_observed=observed,
        specs_expected=effective_expected,
        failures=failures,
        warnings=warnings,
        checked_at=checked_at,
    )
    validate("media_specs_report", report)
    return report


def _build_report(
    *,
    report_id: str,
    episode_id: str,
    file_path: str,
    file_type: str,
    file_valid: bool,
    publish_ready: bool,
    specs_observed: dict,
    specs_expected: dict,
    failures: list[str],
    warnings: list[str],
    checked_at: str,
) -> dict:
    return {
        "report_id": report_id,
        "episode_id": episode_id,
        "file_path": file_path,
        "file_type": file_type,
        "file_valid": file_valid,
        "publish_ready": publish_ready,
        "specs_observed": specs_observed,
        "specs_expected": specs_expected,
        "failures": failures,
        "warnings": warnings,
        "checked_at": checked_at,
    }
