#!/usr/bin/env python3
"""
scripts/supersede_changed_adapters.py — re-register every gate adapter whose
code changed since it was registered.

The adapter registry pins each (provider, model, operation) binding to the
sha256 of its adapter module's source. Editing hypogeum/providers/*_adapter.py
without superseding the registration makes EVERY gated call for it fail with
"already registered with implementation checksum ...; attempted registration
has different checksum" (2026-10-04: Phase 2 edits -> three failed runs, spend
breaker tripped). This walks the active registrations, finds the ones whose
live module checksum differs, and moves each onto the committed code through
adapter_registry.supersede_adapter_registration() (history kept, evidence
recorded). The adapter code must be committed (it checks against HEAD).

    python3 scripts/supersede_changed_adapters.py                 # dry run
    python3 scripts/supersede_changed_adapters.py --apply --reason "..."
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hypogeum import adapter_registry as ar  # noqa: E402
from hypogeum.db import get_connection  # noqa: E402

DB = ROOT / ".hypogeum-assets" / "hypogeum.db"


def stale_registrations(db_path=DB) -> list[dict]:
    conn = get_connection(db_path)
    try:
        rows = {(r["provider"], r["model"], r["provider_operation"]): dict(r)
                for r in conn.execute("SELECT * FROM adapter_registrations WHERE status='active'")}
    finally:
        conn.close()
    out = []
    for st in ar.stale_registrations(db_path):
        r = rows[(st["provider"], st["model"], st["provider_operation"])]
        out.append({**r, "cls": ar.load_adapter_class(st["qualified_name"]), "live_checksum": st["live"]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--reason", default="")
    ap.add_argument("--actor", default="claude-session")
    args = ap.parse_args()

    stale = stale_registrations()
    for r in stale:
        print(f"stale: {r['provider']}:{r['model']}:{r['provider_operation']}  "
              f"{r['adapter_implementation_checksum'][:10]} -> {r['live_checksum'][:10]}")
    if not stale:
        print("all active adapter registrations match the code")
        return 0
    if not args.apply:
        print(f"{len(stale)} stale — re-run with --apply --reason '...' to supersede")
        return 0
    if not args.reason:
        print("--reason is required with --apply")
        return 1
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    for r in stale:
        ar.supersede_adapter_registration(
            r["provider"], r["model"], r["provider_operation"], r["cls"],
            previous_checksum=r["adapter_implementation_checksum"], actor=args.actor,
            reason=args.reason, source_commit=head, confirm_supersede=True, db_path=DB,
        )
        print(f"superseded: {r['provider']}:{r['model']}:{r['provider_operation']}")
    left = stale_registrations()
    print(f"done; still stale: {len(left)}")
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
