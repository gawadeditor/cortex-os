#!/usr/bin/env python3
"""
tools/inject_normalized_missing_scenes.py

Copies normalized comparison clips into the episode video directory so
rerender_ep.py can find them through the standard asset_lookup.

Convention: episodes/{episode_id}/video/sc_{scene_id}.mp4
Source files must be named sc_{scene_id}_norm.mp4 in --source-dir.

Usage:
    python3 tools/inject_normalized_missing_scenes.py \
        --episode-id ep_reg_vi_colosseum \
        --source-dir .hypogeum-assets/provider_compare/colosseum_missing_scenes/normalized \
        --scenes s02 s05 s07 s10 \
        --backup-existing
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _probe(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, timeout=15,
    )
    import json
    d = json.loads(r.stdout) if r.returncode == 0 else {}
    vs = next((s for s in d.get("streams", []) if s["codec_type"] == "video"), {})
    return {
        "width": vs.get("width"),
        "height": vs.get("height"),
        "fps": vs.get("avg_frame_rate", "?"),
        "pix_fmt": vs.get("pix_fmt"),
        "duration": float(d.get("format", {}).get("duration", 0)),
        "audio": any(s["codec_type"] == "audio" for s in d.get("streams", [])),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--episode-id", required=True)
    p.add_argument("--source-dir", required=True)
    p.add_argument("--scenes", nargs="+", required=True,
                   help="scene IDs to inject, e.g. s02 s05 s07 s10")
    p.add_argument("--backup-existing", action="store_true",
                   help="rename any existing sc_{scene}.mp4 to sc_{scene}.pre_inject.mp4")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    src_dir = (_ROOT / args.source_dir).resolve()
    ep_dir  = _ROOT / ".hypogeum-assets" / "episodes" / args.episode_id
    vid_dir = ep_dir / "video"

    if not src_dir.exists():
        print(f"ERROR: source-dir not found: {src_dir}", file=sys.stderr)
        sys.exit(1)
    if not ep_dir.exists():
        print(f"ERROR: episode dir not found: {ep_dir}", file=sys.stderr)
        sys.exit(1)

    vid_dir.mkdir(parents=True, exist_ok=True)

    print(f"Episode : {args.episode_id}")
    print(f"Source  : {src_dir}")
    print(f"Target  : {vid_dir}")
    print(f"Scenes  : {args.scenes}")
    print(f"Dry-run : {args.dry_run}")
    print()

    injected = []
    errors   = []

    for scene_id in args.scenes:
        src_name = f"sc_{scene_id}_norm.mp4"
        dst_name = f"sc_{scene_id}.mp4"
        src = src_dir / src_name
        dst = vid_dir / dst_name

        if not src.exists():
            errors.append(f"  MISSING source: {src_name}")
            continue

        info = _probe(src)
        w, h, fps, pix = info["width"], info["height"], info["fps"], info["pix_fmt"]
        dur  = info["duration"]
        aud  = "audio" if info["audio"] else "no-audio"
        print(f"  {src_name}  →  {dst_name}")
        print(f"    {w}×{h}  {fps}fps  {pix}  {dur:.3f}s  {aud}")

        # Sanity gate: reject if not 1280×720 / yuv420p — normalizer must have run
        if w != 1280 or h != 720:
            errors.append(f"  REJECTED {src_name}: wrong dimensions {w}×{h} (expected 1280×720)")
            continue
        if pix != "yuv420p":
            errors.append(f"  REJECTED {src_name}: wrong pix_fmt {pix} (expected yuv420p)")
            continue

        if args.dry_run:
            print(f"    [DRY RUN] would copy to {dst}")
            injected.append(scene_id)
            continue

        # Backup existing
        if dst.exists():
            if args.backup_existing:
                bak = vid_dir / f"sc_{scene_id}.pre_inject.mp4"
                if bak.exists():
                    bak.unlink()
                dst.rename(bak)
                print(f"    backed up existing → {bak.name}")
            else:
                print(f"    overwriting existing {dst_name}")

        shutil.copy2(src, dst)
        actual_size = dst.stat().st_size
        print(f"    copied  {actual_size // 1024} KB  ✓")
        injected.append(scene_id)

    print()
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        sys.exit(1)

    print(f"Injected {len(injected)}/{len(args.scenes)} scenes: {injected}")
    if not args.dry_run:
        print(f"Video dir now has {len(list(vid_dir.glob('sc_*.mp4')))} sc_*.mp4 clips")


if __name__ == "__main__":
    main()
