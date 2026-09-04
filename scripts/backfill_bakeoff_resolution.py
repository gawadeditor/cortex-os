#!/usr/bin/env python3
"""
scripts/backfill_bakeoff_resolution.py — one-off (2026-09-04).

Video bake-off manifests written before 2026-09-04 recorded price, latency,
duration and motion for each model's clip but not its pixel dimensions —
which is how seedance-pro-t2v-fast (496x864) was adopted for twinkle over
wan2.7 (720x1280) without the resolution gap being visible.

video_bakeoff._generate_one now records width/height on every new clip.
This script fills the gap for older runs by re-probing the .mp4 files that
are still on disk next to each manifest, so video_bakeoff.latest_model_
resolution() and the adoption gate have evidence for already-run models.

Idempotent. Safe to run repeatedly. Only touches manifest.json files under
.hypogeum-assets/video_bakeoffs/.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.artifact_compatibility import probe_resolution  # noqa: E402

BAKEOFF_ROOT = _ROOT / ".hypogeum-assets" / "video_bakeoffs"


def main() -> int:
    if not BAKEOFF_ROOT.exists():
        print("no bake-off directory — nothing to do")
        return 0

    touched = 0
    for run_dir in sorted(BAKEOFF_ROOT.iterdir()):
        manifest_path = run_dir / "manifest.json"
        if not run_dir.is_dir() or not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception as exc:  # noqa: BLE001
            print(f"skip {run_dir.name}: unreadable manifest ({exc})")
            continue

        changed = False
        for result in manifest.get("results", []):
            if result.get("width") and result.get("height"):
                continue
            clip_name = result.get("clip_file")
            if not clip_name:
                continue
            clip = run_dir / clip_name
            if not clip.exists():
                continue
            w, h = probe_resolution(clip)
            if w > 0 and h > 0:
                result["width"], result["height"] = w, h
                changed = True
                print(f"{run_dir.name}  {result.get('model'):<32} -> {w}x{h}")

        if changed:
            tmp = manifest_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(manifest, indent=2))
            tmp.replace(manifest_path)
            touched += 1

    print(f"\nupdated {touched} manifest(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
