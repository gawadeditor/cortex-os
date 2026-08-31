#!/usr/bin/env python3
"""
Local trim of ep_af078f34's three Shorts to <= 60s so they clear
Facebook's (and Instagram's) documented Reels duration limit, matching
what scripts/repair_ep_f3619022_shorts.py did for the Erdstall episode.

DIFFERENCE from the Erdstall repair: ep_af078f34's Shorts ALREADY carry
burned-in captions from Assembly (production_output_gate D_SHORTS_CAPTIONS
passes on the originals). This script therefore only TRIMS at a
sentence-complete caption boundary <= 60s -- it never re-burns a caption
layer (that would double the on-screen text).

Writes to .hypogeum-assets/episodes/ep_af078f34/repair/ -- never
overwrites short_{i}_ep_af078f34.mp4. No network call, no Zernio mutation.
A later, explicitly authorized step promotes the trimmed renders and
re-schedules the posts.

    python3 scripts/trim_ep_af078f34_shorts_60s.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.publishing.short_caption_repair import select_coherent_cutoff

EPISODE_ID = "ep_af078f34"
EP_DIR = _ROOT / ".hypogeum-assets" / "episodes" / EPISODE_ID
CAP_DIR = EP_DIR / "captions"
OUT_DIR = EP_DIR / "repair"
MAX_DURATION_S = 60.0

# deliverable_id -> (source mp4, srt)
SHORTS = [
    ("short_001", "short_0_ep_af078f34.mp4", "sh_short_001.srt"),
    ("short_002", "short_1_ep_af078f34.mp4", "sh_short_002.srt"),
    ("short_003", "short_2_ep_af078f34.mp4", "sh_short_003.srt"),
]


def _ffprobe_duration(p: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(p)], capture_output=True, text=True, timeout=30)
    return float(r.stdout.strip()) if r.stdout.strip() else 0.0


def _ffprobe_wh(p: Path) -> str:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0:s=x", str(p)],
        capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


def _has_audio(p: Path) -> bool:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", str(p)],
        capture_output=True, text=True, timeout=30)
    return "audio" in r.stdout


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict = {}

    for did, mp4_name, srt_name in SHORTS:
        src = EP_DIR / mp4_name
        srt = CAP_DIR / srt_name
        srt_text = srt.read_text(encoding="utf-8")
        blocks = [b for b in srt_text.strip().split("\n\n") if b.strip()]

        plan = select_coherent_cutoff(srt_text, max_duration_s=MAX_DURATION_S, trailing_buffer_s=0.6)
        out_mp4 = OUT_DIR / f"{did}.trimmed.mp4"
        out_srt = OUT_DIR / f"{did}.trimmed.srt"

        # Frame-accurate re-encode trim; keep the existing burned captions
        # (no subtitles filter). Audio re-encoded so -t cuts it cleanly.
        cmd = [
            "ffmpeg", "-y", "-i", str(src),
            "-t", f"{plan.trim_seconds:.3f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_mp4),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            report[did] = {"status": "ffmpeg_failed", "stderr": r.stderr[-3000:]}
            continue

        out_srt.write_text("\n\n".join(blocks[: plan.kept_cue_count]) + "\n", encoding="utf-8")

        dur = _ffprobe_duration(out_mp4)
        report[did] = {
            "status": "trimmed",
            "source_mp4": str(src),
            "source_duration_s": round(_ffprobe_duration(src), 2),
            "output_mp4": str(out_mp4),
            "output_srt": str(out_srt),
            "resolution": _ffprobe_wh(out_mp4),
            "measured_duration_s": round(dur, 3),
            "within_60s": dur <= MAX_DURATION_S,
            "has_audio": _has_audio(out_mp4),
            "kept_cues": plan.kept_cue_count,
            "total_cues": plan.total_cue_count,
            "last_cue_end_s": plan.last_kept_cue_end_s,
        }

    print(json.dumps(report, indent=2))
    (OUT_DIR / "trim_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    ok = all(v.get("within_60s") and v.get("has_audio") for v in report.values())
    print("\nALL 3 shorts <=60s with audio:" , ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
