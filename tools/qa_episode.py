#!/usr/bin/env python3
"""
tools/qa_episode.py — Standalone media-level QA for a rendered episode.

Probes the final MP4 with ffprobe + ffmpeg signal analysis and prints a
human-readable QA report. Does not render, generate, or upload anything.

Usage:
    python3 tools/qa_episode.py \
        --episode-id ep_reg_vi_colosseum \
        --video .hypogeum-assets/episodes/ep_reg_vi_colosseum/main_ep_reg_vi_colosseum.mp4
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.agents.final_output_qa import run_final_output_qa  # noqa: E402
from hypogeum.logger import get_logger                            # noqa: E402

END_PAGE_DURATION = 20.0


def _probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    return float(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else 0.0


def _probe_streams(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        return {}
    d = json.loads(r.stdout)
    vs = next((s for s in d.get("streams", []) if s["codec_type"] == "video"), {})
    as_ = next((s for s in d.get("streams", []) if s["codec_type"] == "audio"), {})
    fmt = d.get("format", {})
    return {
        "width":      vs.get("width"),
        "height":     vs.get("height"),
        "fps":        vs.get("avg_frame_rate", "?"),
        "pix_fmt":    vs.get("pix_fmt"),
        "v_codec":    vs.get("codec_name"),
        "v_duration": float(vs.get("duration", 0)),
        "a_codec":    as_.get("codec_name"),
        "a_samplerate": as_.get("sample_rate"),
        "a_channels": as_.get("channels"),
        "a_duration": float(as_.get("duration", 0)),
        "total_duration": float(fmt.get("duration", 0)),
        "size_bytes": int(fmt.get("size", 0)),
        "bit_rate":   int(fmt.get("bit_rate", 0)),
        "nb_streams": len(d.get("streams", [])),
    }


def _get_expected_body_duration(episode_id: str) -> float:
    """
    Derive expected body duration from the narration audio on disk.
    Falls back to reading narration_package declared durations.
    """
    ep_dir   = _ROOT / ".hypogeum-assets" / "episodes" / episode_id
    narr_mp3 = ep_dir / "narration" / "main_narration.mp3"

    if narr_mp3.exists():
        dur = _probe_duration(narr_mp3)
        if dur > 0:
            return dur

    # Fallback: sum declared durations from narration_package
    np_path = ep_dir / f"narration_package_{episode_id}.json"
    if np_path.exists():
        with open(np_path) as f:
            np = json.load(f)
        total = sum(float(s.get("duration_seconds", 0))
                    for s in np.get("narration_segments", []))
        if total > 0:
            return total

    return 0.0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--episode-id", required=True)
    p.add_argument("--video", required=True, help="Path to the rendered .mp4")
    p.add_argument("--end-page-duration", type=float, default=END_PAGE_DURATION)
    args = p.parse_args()

    mp4 = (_ROOT / args.video).resolve()
    if not mp4.exists():
        print(f"ERROR: video not found: {mp4}", file=sys.stderr)
        sys.exit(1)

    log = get_logger()

    # --- 1. ffprobe stream summary ---
    info = _probe_streams(mp4)
    print("=" * 62)
    print("EPISODE QA REPORT")
    print("=" * 62)
    print(f"episode_id : {args.episode_id}")
    print(f"file       : {mp4.name}  ({info['size_bytes'] // 1024 // 1024} MB)")
    print()
    print("STREAMS")
    print(f"  video : {info['v_codec']}  {info['width']}×{info['height']}  "
          f"{info['fps']}fps  {info['pix_fmt']}  {info['v_duration']:.3f}s")
    if info.get("a_codec"):
        print(f"  audio : {info['a_codec']}  {info['a_samplerate']}Hz  "
              f"{info['a_channels']}ch  {info['a_duration']:.3f}s")
    else:
        print("  audio : NONE (no audio stream)")
    print(f"  total duration : {info['total_duration']:.3f}s  "
          f"bitrate={info['bit_rate'] // 1000}kbps")
    print()

    # --- 2. Body duration expectation ---
    expected_body = _get_expected_body_duration(args.episode_id)
    end_pg = args.end_page_duration
    expected_total = expected_body + end_pg

    print("DURATION CHECK")
    print(f"  expected_body      : {expected_body:.3f}s  (from narration audio)")
    print(f"  end_page_duration  : {end_pg:.1f}s")
    print(f"  expected_total     : {expected_total:.3f}s")
    print(f"  actual_total       : {info['total_duration']:.3f}s")
    delta = info["total_duration"] - expected_total
    flag = "✓" if abs(delta) < 5 else ("⚠ LONG" if delta > 0 else "⚠ SHORT")
    print(f"  delta              : {delta:+.3f}s  {flag}")
    print()

    if expected_body <= 0:
        print("WARNING: could not determine expected_body_duration — skipping signal QA")
        sys.exit(0)

    # --- 3. Signal QA (silence / freeze / AV sync / end-page) ---
    print("SIGNAL QA (ffmpeg analysis)…")
    result = run_final_output_qa(
        mp4_path=mp4,
        expected_body_duration=expected_body,
        end_page_duration=end_pg,
        episode_id=args.episode_id,
        log=log,
    )
    print()

    print("RESULTS")
    print(f"  video_duration     : {result['video_duration_s']:.3f}s")
    print(f"  audio_duration     : {result['audio_duration_s']:.3f}s")
    print(f"  av_mismatch        : {result['av_mismatch_s']:.3f}s")
    print(f"  silence_segments   : {len(result['silence_segments'])}")
    print(f"  freeze_segments    : {len(result['freeze_segments'])}")
    print(f"  body_silence       : {len(result['body_silence_before_end'])}")
    print(f"  body_freeze_long   : "
          f"{len([f for f in result['body_freeze_before_end'] if f['duration'] > 10])}")
    print(f"  end_page_start     : {result['end_page_start_s']}")
    print(f"  end_page_visible   : {result['end_page_visible']}")
    print(f"  passed             : {result['passed']}")

    if result.get("blockers"):
        print()
        print("BLOCKERS")
        for b in result["blockers"]:
            print(f"  ✗ {b}")

    if result.get("warnings"):
        print()
        print("WARNINGS")
        for w in result["warnings"]:
            print(f"  ⚠ {w}")

    print()
    if result["silence_segments"]:
        print("SILENCE DETAIL")
        for s in result["silence_segments"][:10]:
            print(f"  {s['start']:.1f}s – {s['end']:.1f}s  ({s['duration']:.1f}s)")

    if result["freeze_segments"]:
        print("FREEZE DETAIL")
        for f in result["freeze_segments"][:10]:
            print(f"  {f['start']:.1f}s – {f['end']:.1f}s  ({f['duration']:.1f}s)")

    print()
    print("=" * 62)
    if result["passed"]:
        print("QA PASSED ✓")
    else:
        print("QA FAILED ✗")
    print("=" * 62)

    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
