"""
hypogeum/voder_music.py — VODER local music generation (2026-09-20).

Thin adapter around VODER (github.com/HAKORADev/VODER, vendored at
config.VODER_HOME, never committed — see .gitignore), used as a free,
local alternative to ElevenLabs Music. Wired in as a direct call inside
hypogeum.elevenlabs.generate_music_track() when config.MUSIC_PROVIDER ==
"voder" — this module has no knowledge of the music library / gate /
finance machinery; it only turns a prompt into an audio file.

Scope: MUSIC (TTM) generation only. VODER's voice modes (TTS voice
design, cloning, trained-voice reuse) are deliberately NOT wired in
anywhere — evaluated 2026-09-20 and found to have an unreliable, still
unexplained length-control bug (a one-sentence input produced either a
truncated 2.8s clip or a runaway 655s clip depending on invocation path,
uncorrelated with reference length). Narration stays on ElevenLabs.
See memory: project_voder_evaluation_20260920.

VODER runs as a local CLI subprocess inside its own venv — there is no
network call and no per-call cost. Every generation still goes through
the SAME cost-recording call as the ElevenLabs path (at $0.00) so the
`costs` ledger stays a complete record of every track sourced, not just
the paid ones.

VODER's own TTM duration range is 10-300 seconds (its own hard cap);
requests outside that range are clamped here BEFORE invoking it, and the
actual (possibly clamped) duration is what's returned — never the
pre-clamp request — matching generate_music_track()'s own convention for
the ElevenLabs path.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from hypogeum.config import Config

__all__ = ["VoderMusicError", "VoderNotConfiguredError", "generate_music_via_voder"]

_MIN_DURATION_SECONDS = 10.0
_MAX_DURATION_SECONDS = 300.0

_SUCCESS_LINE_RE = re.compile(r"Output saved to:\s*(.+\.wav)\s*$", re.MULTILINE)


class VoderMusicError(RuntimeError):
    """VODER's ttm subprocess failed, timed out, or produced no usable output."""


class VoderNotConfiguredError(VoderMusicError):
    """config.VODER_HOME is unset or does not point at a real VODER checkout."""


def _resolve_voder_python(voder_home: Path) -> Path:
    python_bin = voder_home / ".venv" / "bin" / "python3"
    if not python_bin.exists():
        raise VoderNotConfiguredError(
            f"VODER venv python not found at {python_bin} — run its setup.py first"
        )
    return python_bin


def generate_music_via_voder(
    *,
    style_prompt: str,
    required_duration_seconds: float,
    output_path: Path,
    config: Config,
    log=None,
) -> float:
    """
    Generate an instrumental music track via VODER and save it to
    `output_path`. Returns the actual (ffprobe-measured) duration in
    seconds — never trusts the requested duration blindly, matching
    hypogeum.elevenlabs's own duration-verification convention.

    Args:
        style_prompt:               Free-text music style description
                                    (VODER's `styling` argument). Instrumental
                                    only — lyrics are always "..." (VODER's
                                    own syntax for "no vocals").
        required_duration_seconds: Requested length; clamped to VODER's own
                                    10-300s range before the call.
        output_path:                Where the final .wav is written. Parent
                                    directories are created as needed.
        config:                     Loaded Config instance (VODER_HOME,
                                    VODER_TTM_TIMEOUT_SECONDS).
        log:                        Optional hypogeum logger for info/error events.

    Raises:
        VoderNotConfiguredError — config.VODER_HOME unset or invalid.
        VoderMusicError         — subprocess failure, timeout, or no
                                  parseable/existing output file.
    """
    if not config.VODER_HOME:
        raise VoderNotConfiguredError("config.VODER_HOME is not set")
    voder_home = Path(config.VODER_HOME)
    if not (voder_home / "src" / "voder.py").exists():
        raise VoderNotConfiguredError(f"no VODER checkout found at {voder_home}")

    python_bin = _resolve_voder_python(voder_home)

    duration = max(_MIN_DURATION_SECONDS, min(_MAX_DURATION_SECONDS, required_duration_seconds))
    duration_int = int(round(duration))

    cmd = [
        str(python_bin), "src/voder.py", "ttm",
        "lyrics", "...",
        "styling", style_prompt,
        str(duration_int),
    ]

    if log is not None:
        log.info(
            "voder_music_generation_started",
            style_prompt=style_prompt,
            requested_duration=required_duration_seconds,
            clamped_duration=duration_int,
        )

    try:
        result = subprocess.run(
            cmd, cwd=str(voder_home), capture_output=True, text=True,
            timeout=config.VODER_TTM_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise VoderMusicError(
            f"VODER ttm timed out after {config.VODER_TTM_TIMEOUT_SECONDS}s"
        ) from exc

    combined_output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        raise VoderMusicError(
            f"VODER ttm exited {result.returncode}: {combined_output[-2000:]}"
        )

    match = _SUCCESS_LINE_RE.search(combined_output)
    if not match:
        raise VoderMusicError(
            f"VODER ttm produced no parseable output path: {combined_output[-2000:]}"
        )

    generated_path = Path(match.group(1).strip())
    if not generated_path.is_absolute():
        generated_path = voder_home / generated_path
    if not generated_path.exists():
        raise VoderMusicError(f"VODER reported output at {generated_path} but it does not exist")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(generated_path, output_path)

    from hypogeum.elevenlabs import _probe_audio_duration_seconds  # noqa: PLC0415
    actual_duration = _probe_audio_duration_seconds(output_path)
    if actual_duration is None:
        raise VoderMusicError(f"generated file at {output_path} is not a readable audio file")

    if log is not None:
        log.info(
            "voder_music_generation_completed",
            output_path=str(output_path),
            actual_duration=actual_duration,
        )

    return actual_duration
