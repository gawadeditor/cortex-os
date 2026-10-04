#!/usr/bin/env python3
"""
scripts/workspaces.py — manage Workspaces (multi-tenant plan, Phase 1).

    python3 scripts/workspaces.py list
    python3 scripts/workspaces.py backfill [--dry-run]      # one-time, 2026-10-04
    python3 scripts/workspaces.py create "Acme Media" [--id acme] [--language ar]
    python3 scripts/workspaces.py assign channel <channel_id> <workspace_id> [--move]

backfill backs up the production DB first. Operator-only tool until the
dashboard gets workspace pages (Phases 6-7, docs/plans/MULTI_TENANT_PLAN.md).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hypogeum import workspaces as ws  # noqa: E402

DB = ROOT / ".hypogeum-assets" / "hypogeum.db"


def _backup() -> Path:
    dest = DB.parent / "backups" / f"hypogeum_pre_workspaces_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3
    src = sqlite3.connect(str(DB))
    try:
        out = sqlite3.connect(str(dest))
        src.backup(out)
        out.close()
    finally:
        src.close()
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    b = sub.add_parser("backfill")
    b.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("create")
    c.add_argument("name")
    c.add_argument("--id")
    c.add_argument("--language", default="en")
    a = sub.add_parser("assign")
    a.add_argument("kind", choices=ws.KINDS)
    a.add_argument("resource_id")
    a.add_argument("workspace_id")
    a.add_argument("--move", action="store_true")
    args = ap.parse_args()

    if args.cmd == "list":
        for w in ws.list_workspaces(DB):
            print(f"{w['workspace_id']:<24} {w['kind']:<15} {w['status']:<8} {w['name']}  {w['counts']}")
            for ch in ws.resources_of(w["workspace_id"], "channel", DB):
                print(f"    channel  {ch}")
        return 0
    if args.cmd == "backfill":
        if not args.dry_run:
            print(f"backup: {_backup()}")
        out = ws.backfill_platform_workspace(DB, dry_run=args.dry_run)
        print(out)
        return 0
    if args.cmd == "create":
        print(ws.create_workspace(args.name, workspace_id=args.id, language=args.language, db_path=DB))
        return 0
    if args.cmd == "assign":
        ws.assign(args.kind, args.resource_id, args.workspace_id, move=args.move, db_path=DB)
        print(f"{args.kind} {args.resource_id} → {args.workspace_id}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
