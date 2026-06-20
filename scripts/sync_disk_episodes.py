#!/usr/bin/env python3
"""
scripts/sync_disk_episodes.py

Scan .hypogeum-assets/episodes/* and upsert each episode into the SQLite DB
so the dashboard shows accurate Episode History counts and per-episode details.

Usage:
    python3 scripts/sync_disk_episodes.py
    python3 scripts/sync_disk_episodes.py --episode ep_003
    python3 scripts/sync_disk_episodes.py --assets /path/to/assets --db /path/to/hypogeum.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.disk_sync import sync_disk_episode_to_db, sync_all_disk_episodes_to_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync disk episode artifacts into SQLite DB")
    parser.add_argument("--episode", "-e", help="Sync a single episode by ID")
    parser.add_argument("--assets", help="Path to .hypogeum-assets root")
    parser.add_argument("--db",     help="Path to hypogeum.db")
    parser.add_argument("--quiet",  action="store_true", help="Suppress per-episode output")
    args = parser.parse_args()

    assets_root = Path(args.assets) if args.assets else None
    db_path     = Path(args.db)     if args.db     else None

    if args.episode:
        results = [sync_disk_episode_to_db(args.episode, assets_root=assets_root, db_path=db_path)]
    else:
        results = sync_all_disk_episodes_to_db(assets_root=assets_root, db_path=db_path)

    ok_count  = sum(1 for r in results if r.get("ok"))
    err_count = len(results) - ok_count

    if not args.quiet:
        for r in results:
            status_flag = "OK" if r.get("ok") else "ERR"
            ep_id = r.get("episode_id", "?")
            action = r.get("action", "?")
            status = r.get("status", "?")
            cost   = r.get("cost_usd")
            cost_s = f"  ${cost:.4f}" if cost is not None else ""
            err    = r.get("error")
            if err:
                print(f"[{status_flag}] {ep_id}: {err}")
            else:
                print(f"[{status_flag}] {ep_id}  action={action}  status={status}{cost_s}")

    print(f"\nSynced {ok_count} episode(s), {err_count} error(s).")
    sys.exit(0 if err_count == 0 else 1)


if __name__ == "__main__":
    main()
