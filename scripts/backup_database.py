#!/usr/bin/env python3
"""
scripts/backup_database.py — P0.2: Scheduled database backup entry point.

Intended to be invoked on a schedule (cron/launchd) to run one full backup
cycle: DB snapshot via SQLite's backup API, restore verification, .env
backup, and a health report written to backup_dir/backup_health.json.

Usage:
    python3 scripts/backup_database.py
    python3 scripts/backup_database.py --db-path /path/to/hypogeum.db
    python3 scripts/backup_database.py --backup-dir /path/to/backups
    python3 scripts/backup_database.py --restore /path/to/backup.gz [--force]
    python3 scripts/backup_database.py --status

Exit codes:
    0 — backup (or restore) succeeded and passed verification
    1 — backup, verification, or restore failed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from project root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.recovery import read_backup_health, restore_backup, run_scheduled_backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument(
        "--restore", type=Path, default=None,
        help="Restore the given backup file over --db-path instead of backing up",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="With --restore, override the restore-safety guarantee",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Print the last backup health report and exit",
    )
    args = parser.parse_args()

    if args.status:
        health = read_backup_health(backup_dir=args.backup_dir)
        print(json.dumps(health, indent=2))
        return 0 if health.get("healthy") else 1

    if args.restore:
        result = restore_backup(
            args.restore, target_db_path=args.db_path, force=args.force,
        )
        print(json.dumps(result.__dict__, indent=2))
        return 0 if result.success else 1

    report = run_scheduled_backup(db_path=args.db_path, backup_dir=args.backup_dir)
    print(json.dumps(report, indent=2))
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
