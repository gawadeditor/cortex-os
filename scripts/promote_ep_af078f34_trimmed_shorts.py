#!/usr/bin/env python3
"""
Promote the <=60s trimmed Shorts from
scripts/trim_ep_af078f34_shorts_60s.py into the canonical deliverable
files, then re-verify cross-platform readiness for all four platforms.

  - short_{i}_ep_af078f34.mp4        <- repair/short_00N.trimmed.mp4
      (original preserved as short_{i}_ep_af078f34.pre60s.mp4)
  - captions/sh_short_00N.srt / .ass <- truncated to the kept cue count
      (originals preserved as *.pre60s)
  - final_render_package shorts_renders[].duration_seconds updated

No network call, no Zernio mutation. Dry-run by default; --apply writes.

    python3 scripts/promote_ep_af078f34_trimmed_shorts.py
    python3 scripts/promote_ep_af078f34_trimmed_shorts.py --apply
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

EP = "ep_af078f34"
EP_DIR = _ROOT / ".hypogeum-assets" / "episodes" / EP
CAP_DIR = EP_DIR / "captions"
REPAIR = EP_DIR / "repair"
FRP = EP_DIR / f"final_render_package_{EP}.json"

# (index, deliverable_id, canonical mp4, srt, ass)
SHORTS = [
    (0, "short_001", "short_0_ep_af078f34.mp4", "sh_short_001.srt", "sh_short_001.ass"),
    (1, "short_002", "short_1_ep_af078f34.mp4", "sh_short_002.srt", "sh_short_002.ass"),
    (2, "short_003", "short_2_ep_af078f34.mp4", "sh_short_003.srt", "sh_short_003.ass"),
]


def _dur(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(p)], capture_output=True, text=True, timeout=30)
    return float(r.stdout.strip()) if r.stdout.strip() else 0.0


def _truncate_ass(ass_text: str, keep: int) -> str:
    from hypogeum.publishing.short_caption_repair import build_trimmed_ass_text
    return build_trimmed_ass_text(ass_text, keep)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    trim_report = json.loads((REPAIR / "trim_report.json").read_text())
    frp = json.loads(FRP.read_text())
    renders = frp.get("shorts_renders", [])

    print(f"{'APPLY' if args.apply else 'DRY RUN'} — promote trimmed shorts for {EP}\n")
    for idx, did, mp4, srt, ass in SHORTS:
        tr = trim_report.get(did, {})
        trimmed_mp4 = REPAIR / f"{did}.trimmed.mp4"
        trimmed_srt = REPAIR / f"{did}.trimmed.srt"
        if not (trimmed_mp4.exists() and trimmed_srt.exists()):
            print(f"  {did}: MISSING trimmed output — run trim_ep_af078f34_shorts_60s.py first")
            return 2
        keep = tr["kept_cues"]
        print(f"  {did}: {mp4}  {tr['source_duration_s']}s -> {tr['measured_duration_s']}s "
              f"(keep {keep}/{tr['total_cues']} cues)")

        if not args.apply:
            continue

        canon_mp4 = EP_DIR / mp4
        shutil.copy2(canon_mp4, canon_mp4.with_suffix(".pre60s.mp4"))
        shutil.copy2(trimmed_mp4, canon_mp4)

        srt_p = CAP_DIR / srt
        ass_p = CAP_DIR / ass
        shutil.copy2(srt_p, srt_p.with_name(srt_p.name + ".pre60s"))
        shutil.copy2(ass_p, ass_p.with_name(ass_p.name + ".pre60s"))
        srt_p.write_text(trimmed_srt.read_text(encoding="utf-8"), encoding="utf-8")
        ass_p.write_text(_truncate_ass(ass_p.with_name(ass_p.name + ".pre60s").read_text(encoding="utf-8"), keep),
                         encoding="utf-8")

        new_dur = round(_dur(canon_mp4), 3)
        for r in renders:
            if r.get("short_id") == did or r.get("video_asset_id", "").endswith(f"short_{idx}_{EP}"):
                r["duration_seconds"] = new_dur
        print(f"     promoted; canonical duration now {new_dur}s")

    if args.apply:
        FRP.write_text(json.dumps(frp, indent=2), encoding="utf-8")
        print("\nfinal_render_package updated. Re-checking cross-platform readiness...\n")
        from hypogeum.publishing.cross_platform_readiness import check_cross_platform_readiness
        res = check_cross_platform_readiness(
            EP, channel_id="hypogeum", assets_root=_ROOT / ".hypogeum-assets",
            db_path=_ROOT / ".hypogeum-assets" / "hypogeum.db", provider_id="zernio")
        for c in getattr(res, "checks", getattr(res, "results", [])):
            if c.status == "skipped_not_enabled":
                continue
            mark = "OK " if c.status == "pass" else "XX "
            print(f"  {mark}[{c.platform}/{c.deliverable_id}] {c.status}  {getattr(c,'reason','')[:90]}")
    else:
        print("\nDRY RUN. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
