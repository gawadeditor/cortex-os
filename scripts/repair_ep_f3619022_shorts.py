#!/usr/bin/env python3
"""
Local repair of ep_f3619022's three Shorts: burn their existing timed
captions into the video pixels and trim each to a coherent,
sentence-complete cutoff under the strictest selected-platform duration
limit (Facebook/Zernio's 60s), using the libass-enabled ffmpeg keg
already installed on this machine (Homebrew's `ffmpeg-full`, not the
default `ffmpeg` on PATH, which has no subtitles filter -- see
hypogeum.agents.production_output_gate._check_ffmpeg_subtitle_support()).

Writes repaired output next to the originals under a `repair/`
subdirectory -- never overwrites short_{i}_ep_f3619022.mp4 itself, so the
existing media relationship (what Zernio currently has staged/attached)
is left untouched until a repaired render is deliberately promoted in a
later, explicitly authorized phase.

This script performs no network call and no Zernio mutation. It only
reads/writes local files under .hypogeum-assets/episodes/ep_f3619022/.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.agents.production_output_gate import (
    _ass_time_to_seconds,
    _verify_burned_captions,
    _verify_visual_coverage_spans_captions,
)
from hypogeum.publishing.short_caption_repair import (
    build_repair_ffmpeg_command,
    build_trimmed_ass_text,
    select_coherent_cutoff,
)

EPISODE_ID = "ep_f3619022"
ASSETS_ROOT = Path(".hypogeum-assets")
EPISODE_DIR = ASSETS_ROOT / "episodes" / EPISODE_ID
CAPTIONS_DIR = EPISODE_DIR / "captions"
REPAIR_DIR = EPISODE_DIR / "repair"
LIBASS_FFMPEG = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
MAX_DURATION_S = 60.0  # strictest selected-platform limit (Facebook/Zernio) -- see
                        # docs/operations/audits/ZERNIO_PUT_CONTRACT_AND_DURATION_VERIFICATION_2026-08-22.md

# index -> (deliverable_id, source mp4 filename, srt filename, ass filename)
SHORTS = [
    (0, "short_001", "short_0_ep_f3619022.mp4", "sh_short_001.srt", "sh_short_001.ass"),
    (1, "short_002", "short_1_ep_f3619022.mp4", "sh_short_002.srt", "sh_short_002.ass"),
    (2, "short_003", "short_2_ep_f3619022.mp4", "sh_short_003.srt", "sh_short_003.ass"),
]


def _ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    return float(result.stdout.strip())


def _has_audio_stream(path: Path) -> bool:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    return "audio" in result.stdout


def main() -> None:
    REPAIR_DIR.mkdir(parents=True, exist_ok=True)
    report = {}

    for index, deliverable_id, mp4_name, srt_name, ass_name in SHORTS:
        source_mp4 = EPISODE_DIR / mp4_name
        srt_path = CAPTIONS_DIR / srt_name
        ass_path = CAPTIONS_DIR / ass_name
        srt_text = srt_path.read_text(encoding="utf-8")
        ass_text = ass_path.read_text(encoding="utf-8")

        plan = select_coherent_cutoff(srt_text, max_duration_s=MAX_DURATION_S, trailing_buffer_s=0.6)
        trimmed_ass_text = build_trimmed_ass_text(ass_text, plan.kept_cue_count)

        trimmed_ass_path = REPAIR_DIR / f"{deliverable_id}.repaired.ass"
        trimmed_srt_path = REPAIR_DIR / f"{deliverable_id}.repaired.srt"
        trimmed_ass_path.write_text(trimmed_ass_text, encoding="utf-8")
        # Also write a matching truncated .srt -- used only by this
        # script's own verification calls below (_verify_burned_captions/
        # _verify_visual_coverage_spans_captions expect .srt input), never
        # burned in itself (the .ass carries the burn-in style).
        srt_cues_kept = []
        blocks = srt_text.strip().split("\n\n")
        srt_cues_kept = blocks[: plan.kept_cue_count]
        trimmed_srt_path.write_text("\n\n".join(srt_cues_kept) + "\n", encoding="utf-8")

        output_mp4 = REPAIR_DIR / f"{deliverable_id}.repaired.mp4"
        cmd = build_repair_ffmpeg_command(
            source_mp4, trimmed_ass_path, output_mp4, plan.trim_seconds,
            ffmpeg_binary=LIBASS_FFMPEG,
        )
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            report[deliverable_id] = {"status": "ffmpeg_failed", "stderr": result.stderr[-4000:]}
            continue

        measured_duration = _ffprobe_duration(output_mp4)
        caption_verdict = _verify_burned_captions(output_mp4, trimmed_srt_path)
        coverage_verdict = _verify_visual_coverage_spans_captions(output_mp4, trimmed_srt_path)
        has_audio = _has_audio_stream(output_mp4)

        report[deliverable_id] = {
            "status": "repaired",
            "source_mp4": str(source_mp4),
            "output_mp4": str(output_mp4),
            "cutoff_plan": {
                "kept_cue_count": plan.kept_cue_count,
                "total_cue_count": plan.total_cue_count,
                "last_kept_cue_end_s": plan.last_kept_cue_end_s,
                "trim_seconds": plan.trim_seconds,
            },
            "measured_duration_s": round(measured_duration, 3),
            "duration_within_60s": measured_duration <= MAX_DURATION_S,
            "burned_caption_verification": caption_verdict,
            "visual_coverage_verification": coverage_verdict,
            "has_audio_stream": has_audio,
        }

    print(json.dumps(report, indent=2))
    (REPAIR_DIR / "repair_verification_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8",
    )


if __name__ == "__main__":
    main()
