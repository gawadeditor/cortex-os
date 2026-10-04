#!/usr/bin/env python3
"""
scripts/encrypt_secrets.py — encrypt the plaintext secrets already stored in
secret_profiles (one-time, 2026-10-04; see hypogeum/vault.py).

    python3 scripts/encrypt_secrets.py --dry-run
    python3 scripts/encrypt_secrets.py
    python3 scripts/encrypt_secrets.py --copy-key     # put the vault key on the clipboard

Creates ~/.cortex/vault.key on first use. Every row is encrypted, then
decrypted again and compared with the original before the transaction
commits — any mismatch aborts with nothing changed. Idempotent: rows already
encrypted are skipped.

SAVE THE KEY: without ~/.cortex/vault.key every stored secret is unreadable.
--copy-key copies it to the clipboard so it can be pasted into a password
manager; it is never printed.

Note: the DB copies already in .hypogeum-assets/backups/ (and on the NAS)
were written before this and still hold plaintext — this script lists them
but deliberately deletes nothing.
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hypogeum import vault  # noqa: E402

DB = ROOT / ".hypogeum-assets" / "hypogeum.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--copy-key", action="store_true")
    args = ap.parse_args()

    if args.copy_key:
        if not vault.key_path().exists():
            print("no vault key yet — run the migration first")
            return 1
        subprocess.run(["pbcopy"], input=vault.key_path().read_text().strip(), text=True)
        print("✓ vault key copied to the clipboard — paste it into your password manager, then copy "
              "something else to clear the clipboard")
        return 0

    conn = sqlite3.connect(str(DB), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT secret_profile_id, provider, credential_data FROM secret_profiles").fetchall()
        todo = [r for r in rows if r["credential_data"] and not vault.is_encrypted(r["credential_data"])]
        print(f"{len(rows)} secrets, {len(todo)} still plaintext")
        if args.dry_run or not todo:
            return 0
        conn.execute("BEGIN IMMEDIATE")
        for r in todo:
            enc = vault.encrypt(r["credential_data"])
            if vault.decrypt(enc) != r["credential_data"]:
                conn.rollback()
                print(f"✗ round-trip mismatch on {r['provider']} — nothing changed")
                return 1
            conn.execute("UPDATE secret_profiles SET credential_data=? WHERE secret_profile_id=?",
                         (enc, r["secret_profile_id"]))
        conn.commit()
        left = sum(1 for r in conn.execute("SELECT credential_data FROM secret_profiles")
                   if r[0] and not vault.is_encrypted(r[0]))
    finally:
        conn.close()
    print(f"✓ encrypted {len(todo)} secrets ({left} plaintext left). Key: {vault.key_path()}")
    backups = sorted((DB.parent / "backups").glob("*.db"))
    if backups:
        print(f"! {len(backups)} older DB backups in {DB.parent / 'backups'} still contain plaintext secrets "
              "(nothing deleted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
