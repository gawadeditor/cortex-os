#!/usr/bin/env python3
"""
tools/reconcile_ep_5f72565c_main_attempts.py

The 2026-09-05T14:00 scheduled-publish call for the ep_5f72565c main
episode (twinkle_mini_tales) got a content_hash_conflict on ALL FOUR
platforms (youtube/tiktok/instagram/facebook) -- the provider reports
the post already exists as:

    6a9c20560f31bf4775839c26  status=scheduled  scheduledFor=2026-09-16T19:00:00Z

i.e. an earlier call (same content hash) actually landed on Zernio, but
whatever local call came right after it got a duplicate-submission 409,
leaving all 4 local publishing_attempts rows stuck at state='ambiguous'
with NO publishing_receipts row -- even though the post is genuinely,
correctly scheduled. Found 2026-09-11 while verifying the publish
schedule end-to-end (channel_publish_slots said ep_5f72565c was
scheduled; scripts/backfill_vertical_covers.py said "no Zernio post id"
because it can only see posts through our own receipts table).

This script mirrors tools/reconcile_ep_af078f34_main_attempt.py (the
2026-08-31 precedent for the exact same failure shape) but for all 4
platform attempts of one post instead of one.

SAFETY: dry-run by default. --apply performs the DB writes. Refuses if
the Zernio post is not found / not scheduled / missing a platform target.

    python3 tools/reconcile_ep_5f72565c_main_attempts.py
    python3 tools/reconcile_ep_5f72565c_main_attempts.py --apply \
        --i-understand-this-is-production
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

EP = "ep_5f72565c"
CHANNEL = "twinkle_mini_tales"
PLATFORMS = ["youtube", "tiktok", "instagram", "facebook"]
ZERNIO_POST_ID = "6a9c20560f31bf4775839c26"
_PROD_DB = (_ROOT / ".hypogeum-assets" / "hypogeum.db").resolve()


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
    from hypogeum.publishing.zernio import ZernioProvider
    from hypogeum.publishing.submission_ledger import (
        _new_id, _now_iso, _transition_attempt, _validate_transition,
    )
    from hypogeum.publishing.provider_repository import _insert_content_mapping_in_txn

    # ---- 1. CONFIRM the Zernio post ----------------------------------
    d = ZernioProvider().get_post(ZERNIO_POST_ID, db_path=db)
    post = d.get("post") or {}
    if d.get("status_code") != 200 or not post:
        print(f"ERROR: Zernio GET {ZERNIO_POST_ID} -> {d.get('status_code')} / {d.get('error')!r}",
              file=sys.stderr)
        return 3
    if post.get("status") != "scheduled":
        print(f"ERROR: post status={post.get('status')!r}, expected 'scheduled'; aborting.",
              file=sys.stderr)
        return 3
    plat_entries = {p.get("platform"): p for p in (post.get("platforms") or [])}
    missing = [p for p in PLATFORMS if p not in plat_entries]
    if missing:
        print(f"ERROR: post is missing platform target(s) {missing}; aborting.", file=sys.stderr)
        return 3
    print(f"CONFIRMED Zernio post {ZERNIO_POST_ID}: status={post.get('status')} "
          f"scheduledFor={post.get('scheduledFor')} platforms={sorted(plat_entries)}")

    conn = get_connection(db)
    try:
        rows = conn.execute(
            "SELECT * FROM publishing_attempts WHERE episode_id=? AND deliverable_id='main_episode'",
            (EP,),
        ).fetchall()
    finally:
        conn.close()
    attempts = {r["platform"]: dict(r) for r in rows}
    to_fix = []
    for p in PLATFORMS:
        att = attempts.get(p)
        if att is None:
            print(f"ERROR: no local attempt row for platform={p}", file=sys.stderr)
            return 4
        print(f"  local attempt {att['attempt_id']} platform={p} state={att['state']} "
              f"err={att['last_error_category']}")
        if att["state"] == "succeeded":
            print(f"    already succeeded -- skipping (idempotent).")
            continue
        if att["state"] != "ambiguous":
            print(f"ERROR: platform={p} attempt state is {att['state']!r}, expected 'ambiguous'.",
                  file=sys.stderr)
            return 4
        _validate_transition("ambiguous", "succeeded")  # raises if not allowed
        to_fix.append(att)

    if not to_fix:
        print("\nNothing to do -- all attempts already succeeded.")
        return 0

    if not args.apply:
        print(f"\nDRY RUN. Re-run with --apply --i-understand-this-is-production to reconcile "
              f"{len(to_fix)} attempt(s):")
        for att in to_fix:
            print(f"  - {att['platform']}: INSERT receipt, ambiguous -> succeeded, "
                  f"intent -> succeeded, INSERT content mapping")
        return 0

    # ---- one transaction per platform, mirroring record_success() ----
    conn = get_connection(db)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now_iso()
            for att in to_fix:
                p = att["platform"]
                entry = plat_entries[p]
                acct = entry.get("accountId") or {}
                zernio_account_id = acct.get("_id") if isinstance(acct, dict) else (acct or None)
                zernio_profile_id = acct.get("profileId") if isinstance(acct, dict) else None
                receipt_id = _new_id("receipt")
                conn.execute(
                    """
                    INSERT INTO publishing_receipts
                        (receipt_id, attempt_id, provider_id, platform, provider_publication_id,
                         native_platform_content_id, provider_status, acknowledged_at,
                         raw_receipt_metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (receipt_id, att["attempt_id"], att["provider_id"], p, ZERNIO_POST_ID,
                     None, "scheduled", now,
                     json.dumps({"reconciled_from": "ambiguous_content_hash_conflict",
                                 "zernio_post_id": ZERNIO_POST_ID,
                                 "zernio_account_id": zernio_account_id,
                                 "zernio_profile_id": zernio_profile_id,
                                 "scheduled_for": post.get("scheduledFor")})),
                )
                _transition_attempt(conn, att["attempt_id"], "succeeded", now=now)
                conn.execute(
                    "UPDATE publication_intents SET status='succeeded', updated_at=? "
                    "WHERE publication_intent_id=?", (now, att["publication_intent_id"]))
                _insert_content_mapping_in_txn(
                    conn, provider_id=att["provider_id"], platform=p, channel_id=CHANNEL,
                    episode_id=EP, deliverable_id="main_episode",
                    provider_profile_id=None, provider_account_id=None,
                    provider_content_id=ZERNIO_POST_ID, native_platform_content_id=None,
                    publication_intent_id=att["publication_intent_id"], attempt_id=att["attempt_id"],
                    receipt_id=receipt_id, status="confirmed", now=now,
                )
                print(f"  reconciled {p}: attempt {att['attempt_id']} -> succeeded")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()

    print(f"\nRECONCILED {len(to_fix)} attempt(s) -> provider_content_mappings -> "
          f"{ZERNIO_POST_ID} (confirmed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
