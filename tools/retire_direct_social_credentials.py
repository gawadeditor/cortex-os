#!/usr/bin/env python3
"""
tools/retire_direct_social_credentials.py

Zernio is now the single social integration for this factory: it owns the
OAuth links to YouTube / TikTok / Instagram / Facebook, publishes to them,
and reports their health (hypogeum/publishing/zernio_accounts.py). The
per-platform direct-OAuth secret_profiles are no longer read by the
dashboard or the publish path.

This retires them: sets secret_profiles.status = 'revoked' for the four
social-platform OAuth profiles. The rows are kept (history / audit); they
just stop being treated as live credentials by _load_credentials(),
provider_verifier, and the legacy direct-publish path.

NOT touched:
  - The `zernio` secret profile (the key everything now uses).
  - LLM / media provider keys (openrouter, perplexity, muapi, elevenlabs,
    veo, seedance, openai, runway).
  - `.env` YOUTUBE_* / META_* / TIKTOK_* — the worker's direct YouTube
    analytics sync still reads those; that path is replaced when Zernio
    analytics ingestion lands (part 2), and .env is cleaned up then.

SAFETY: dry-run by default. --apply performs the UPDATE. Refuses the
production DB without --i-understand-this-is-production.

    python3 tools/retire_direct_social_credentials.py
    python3 tools/retire_direct_social_credentials.py --apply \
        --i-understand-this-is-production
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_PROD_DB = (_ROOT / ".hypogeum-assets" / "hypogeum.db").resolve()
_SOCIAL_PROVIDERS = ("youtube", "tiktok", "instagram", "facebook")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--i-understand-this-is-production", action="store_true")
    args = ap.parse_args(argv)

    db = (args.db or _PROD_DB).resolve()
    if db == _PROD_DB and args.apply and not args.i_understand_this_is_production:
        print("ERROR: refusing the production DB without --i-understand-this-is-production.",
              file=sys.stderr)
        return 2

    from hypogeum.db import get_connection

    conn = get_connection(db)
    try:
        rows = list(conn.execute(
            """
            SELECT secret_profile_id, provider, label, status
            FROM secret_profiles
            WHERE provider IN (?, ?, ?, ?)
            ORDER BY provider, created_at
            """,
            _SOCIAL_PROVIDERS,
        ))
    finally:
        conn.close()

    to_revoke = [r for r in rows if r["status"] == "active"]
    print(f"social-platform secret_profiles ({len(rows)} total):")
    for r in rows:
        mark = "  -> REVOKE" if r["status"] == "active" else ""
        print(f"   {r['provider']:10} {r['status']:9} {r['label']}{mark}")

    if not to_revoke:
        print("\nNothing active to retire — already done (idempotent no-op).")
        return 0

    if not args.apply:
        print(f"\nDRY RUN. {len(to_revoke)} profile(s) would move active -> revoked. "
              "Re-run with --apply --i-understand-this-is-production.")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db)
    try:
        n = 0
        for r in to_revoke:
            n += conn.execute(
                "UPDATE secret_profiles SET status = 'revoked', updated_at = ? "
                "WHERE secret_profile_id = ? AND status = 'active'",
                (now, r["secret_profile_id"]),
            ).rowcount
        conn.commit()
    finally:
        conn.close()

    print(f"\nRetired {n} direct social-platform credential profile(s) -> 'revoked'. "
          "Zernio remains the live integration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
