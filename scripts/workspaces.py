#!/usr/bin/env python3
"""
scripts/workspaces.py — manage Workspaces (multi-tenant plan, Phase 1).

    python3 scripts/workspaces.py list
    python3 scripts/workspaces.py backfill [--dry-run]      # one-time, 2026-10-04
    python3 scripts/workspaces.py create "Acme Media" [--id acme] [--language ar]
    python3 scripts/workspaces.py assign channel <channel_id> <workspace_id> [--move]
    python3 scripts/workspaces.py set-key <workspace_id> <provider>     # key from the clipboard
        provider: openrouter | muapi | elevenlabs | perplexity | anthropic | zernio | postiz
    python3 scripts/workspaces.py keys <workspace_id>                   # which keys it has (never values)

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


def _set_key(workspace_id: str, provider: str) -> int:
    """Store a provider key for a workspace from the clipboard (cleared right
    after; never typed on the command line or pasted in a chat). Replaces
    the workspace's existing key for that provider. Encrypted at rest."""
    import subprocess
    from hypogeum.credential_manager import (create_secret_profile, list_secret_profiles,
                                             revoke_secret_profile)
    if workspace_id not in {w["workspace_id"] for w in ws.list_workspaces(DB)}:
        print(f"unknown workspace {workspace_id!r}")
        return 1
    key = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["pbcopy"], input="", text=True)
    if not key or " " in key or len(key) < 16:
        print("the clipboard doesn't look like an API key — copy it and re-run")
        return 1
    print(f"✓ key read from clipboard ({len(key)} chars) and clipboard cleared")
    owned = set(ws.resources_of(workspace_id, "secret_profile", DB))
    for p in list_secret_profiles(provider=provider, db_path=DB):
        if p.secret_profile_id in owned and p.status == "active":
            revoke_secret_profile(p.secret_profile_id, db_path=DB)
    create_secret_profile(provider, f"{workspace_id} {provider}", "api_key", key,
                          scope="factory", workspace_id=workspace_id, db_path=DB)
    print(f"✓ {provider} key stored for {workspace_id} (encrypted)")
    return 0


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
    k = sub.add_parser("set-key")
    k.add_argument("workspace_id")
    k.add_argument("provider", choices=["openrouter", "muapi", "elevenlabs", "perplexity", "anthropic", "zernio", "postiz"])
    ks = sub.add_parser("keys")
    ks.add_argument("workspace_id")
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
    if args.cmd == "set-key":
        return _set_key(args.workspace_id, args.provider)
    if args.cmd == "keys":
        from hypogeum.credential_manager import list_secret_profiles
        owned = set(ws.resources_of(args.workspace_id, "secret_profile", DB))
        for p in list_secret_profiles(db_path=DB):
            if p.secret_profile_id in owned:
                print(f"{p.provider:<12} {p.status:<8} {p.label}  (updated {p.updated_at[:10]})")
        return 0
    if args.cmd == "assign":
        ws.assign(args.kind, args.resource_id, args.workspace_id, move=args.move, db_path=DB)
        print(f"{args.kind} {args.resource_id} → {args.workspace_id}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
