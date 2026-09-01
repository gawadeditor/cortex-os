#!/usr/bin/env python3
"""
tools/relocate_published_episode.py

Move a published episode's asset tree from the working
``.hypogeum-assets/episodes/<episode_id>/`` directory to the configured
archive root, and repoint the catalog (archive_index + JSONL + assets).

Used to relocate episodes that were published before the pipeline gained
its terminal archive-relocation step, and for any manual case. The pipeline
does this automatically for new runs.

The archive root is resolved by hypogeum.archive_storage.resolve_archive_root
(factory_settings['archive_root'] -> Config.HYPOGEUM_ARCHIVE_ROOT -> the
built-in default). Set it from the dashboard Settings page.

SAFETY: dry-run by default. --apply performs the move. Refuses the
production DB without --i-understand-this-is-production.

    python3 tools/relocate_published_episode.py --episode ep_af078f34
    python3 tools/relocate_published_episode.py --episode ep_af078f34 --apply \
        --i-understand-this-is-production
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_PROD_DB = (_ROOT / ".hypogeum-assets" / "hypogeum.db").resolve()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", required=True, help="episode_id to relocate")
    ap.add_argument("--channel", default=None,
                    help="owning channel_id (default: resolved from publication_intents)")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--i-understand-this-is-production", action="store_true")
    ap.add_argument("--allow-unpublished", action="store_true",
                    help="relocate even without a succeeded publication (not recommended)")
    args = ap.parse_args(argv)

    db = (args.db or _PROD_DB).resolve()
    if db == _PROD_DB and args.apply and not args.i_understand_this_is_production:
        print("ERROR: refusing the production DB without --i-understand-this-is-production.",
              file=sys.stderr)
        return 2

    from hypogeum.db import get_connection
    from hypogeum.archive_storage import resolve_archive_root
    from hypogeum.archive_relocation import relocate_episode_assets

    channel = args.channel
    if not channel:
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT channel_id FROM publication_intents WHERE episode_id = ? "
                "ORDER BY updated_at DESC LIMIT 1", (args.episode,)).fetchone()
            channel = row["channel_id"] if row else "hypogeum"
        finally:
            conn.close()

    root = resolve_archive_root(db_path=db)
    print(f"episode      : {args.episode}")
    print(f"channel      : {channel}")
    print(f"archive root : {root}")
    print(f"destination  : {root / channel / args.episode}")
    print(f"mode         : {'APPLY' if args.apply else 'dry-run'}")
    print()

    rec = relocate_episode_assets(
        args.episode, channel, db_path=db,
        dry_run=not args.apply,
        require_published=not args.allow_unpublished,
    )
    print(json.dumps(rec, indent=2, default=str))

    if rec["verdict"] == "FAILED":
        return 1
    if not args.apply and rec["verdict"] in ("RELOCATED", "WARNING"):
        print(f"\nDry-run: would move {rec['files_moved']} file(s), "
              f"{rec['bytes_moved'] / 1e6:.1f} MB. Re-run with --apply.")
    elif args.apply and rec["relocated"]:
        print(f"\nMoved {rec['files_moved']} file(s). "
              f"archive_index rows repointed: {rec['archive_index_rows_updated']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
