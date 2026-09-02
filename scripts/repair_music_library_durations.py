"""
scripts/repair_music_library_durations.py — One-time music-library.json duration repair

Corrects duration_seconds metadata for entries written before the 2026-07-19
generate_music_track() fix (which previously recorded the pre-clamp
requested duration instead of the actual generated/clamped duration).

Probes each track's real on-disk duration via ffprobe and compares it to
the stored metadata. Reports mismatches; only writes corrections when
--apply is passed.

This is the ONLY place real-file-duration probing is meant to happen for
the music library — see hypogeum.elevenlabs.repair_music_library_durations()
and _search_music_library()'s own docstring for why per-lookup probing was
evaluated and rejected (2026-07-19 decision).

Usage:
    python3 scripts/repair_music_library_durations.py              # dry run (report only)
    python3 scripts/repair_music_library_durations.py --apply       # apply corrections

Exit codes:
    0 — no mismatches found (or corrections applied successfully)
    1 — mismatches found in dry-run mode (informational; not an error)
    2 — unreadable files encountered (files missing or ffprobe failed)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hypogeum.elevenlabs import repair_music_library_durations
from hypogeum.logger import get_logger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Write corrections to music-library.json (default: dry run, report only)",
    )
    parser.add_argument(
        "--assets-root", type=Path,
        default=_ROOT / ".hypogeum-assets",
        help="Root of .hypogeum-assets/ tree (default: project .hypogeum-assets/)",
    )
    args = parser.parse_args(argv)

    library_path = args.assets_root / "channel" / "music" / "music-library.json"
    log = get_logger(episode_id="repair_script", agent="repair_music_library_durations")

    if not library_path.exists():
        print(f"No music library found at {library_path} — nothing to repair.")
        return 0

    result = repair_music_library_durations(
        library_path=library_path,
        assets_root=args.assets_root,
        log=log,
        dry_run=not args.apply,
    )

    print(f"Checked {result['checked']} track(s) in {library_path}")
    print(f"Mismatched: {len(result['mismatched'])}")
    for m in result["mismatched"]:
        print(
            f"  {m['track_id']}: claimed {m['claimed_duration_seconds']:.1f}s, "
            f"real {m['real_duration_seconds']:.1f}s ({m['path']})"
        )
    if result["unreadable"]:
        print(f"Unreadable (missing file or ffprobe failure): {len(result['unreadable'])}")
        for tid in result["unreadable"]:
            print(f"  {tid}")

    if args.apply:
        print(f"Applied {result['repaired']} correction(s).")
        return 0 if not result["unreadable"] else 2

    if result["mismatched"]:
        print("\nDry run only — re-run with --apply to write corrections.")
        return 1
    return 2 if result["unreadable"] else 0


if __name__ == "__main__":
    sys.exit(main())
