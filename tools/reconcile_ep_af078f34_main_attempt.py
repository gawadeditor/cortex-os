#!/usr/bin/env python3
"""
tools/reconcile_ep_af078f34_main_attempt.py

The 2026-08-31 scheduled-publish call for the ep_af078f34 main episode
(tools/schedule_ep_af078f34_main_only.py) got a read timeout from Zernio
AFTER the POST had actually been accepted -- so the local
publishing_attempts row is stuck at state='ambiguous', while Zernio DID
create the post:

    6a956d16e7861c9383f9e1f4  status=scheduled  2026-09-06T14:00:00Z
    platforms[youtube] = pending   account "The Hypogeum" (@thehypogeum)

This script reconciles that one attempt: it re-reads the Zernio post to
CONFIRM it exists / is scheduled / has the youtube target, then, in one
transaction, transitions the ambiguous attempt -> succeeded, writes the
publishing_receipts row and the confirmed provider_content_mappings row
(exactly what submission_ledger.record_success() would have written had
the HTTP response come back in time). The state machine already permits
ambiguous -> succeeded; there is just no public helper that also writes
the receipt+mapping, so this operator script does it directly.

SAFETY: dry-run by default. --apply performs the DB writes. Refuses if
the Zernio post is not found / not scheduled / missing the youtube target.

    python3 tools/reconcile_ep_af078f34_main_attempt.py
    python3 tools/reconcile_ep_af078f34_main_attempt.py --apply \
        --i-understand-this-is-production
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

EP = "ep_af078f34"
CHANNEL = "hypogeum"
PLATFORM = "youtube"
ZERNIO_POST_ID = "6a956d16e7861c9383f9e1f4"
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
    yt = next((p for p in (post.get("platforms") or []) if p.get("platform") == PLATFORM), None)
    if post.get("status") != "scheduled" or yt is None:
        print(f"ERROR: post status={post.get('status')!r}, youtube target present={yt is not None} "
              f"-- not the expected scheduled+youtube shape; aborting.", file=sys.stderr)
        return 3
    acct = yt.get("accountId") or {}
    zernio_account_id = acct.get("_id") if isinstance(acct, dict) else (acct or None)
    zernio_profile_id = acct.get("profileId") if isinstance(acct, dict) else None
    # provider_content_mappings.provider_profile_id / provider_account_id are
    # FKs into Cortex's own provider_profiles / provider_accounts tables --
    # NOT Zernio's ids. The Shorts' own mappings were written with both NULL
    # (no channel_provider_binding resolved), which is the sanctioned
    # "not yet known" value; match that. Zernio's ids are kept in the
    # receipt's raw_receipt_metadata_json instead.
    print(f"CONFIRMED Zernio post {ZERNIO_POST_ID}: status={post.get('status')} "
          f"scheduledFor={post.get('scheduledFor')} youtube={yt.get('status')} "
          f"zernio_account={zernio_account_id} zernio_profile={zernio_profile_id}")

    # ---- 2. find the ambiguous attempt -----------------------------
    conn = get_connection(db)
    try:
        att = conn.execute(
            "SELECT * FROM publishing_attempts WHERE episode_id=? AND deliverable_id='main_episode' "
            "AND platform=?", (EP, PLATFORM)).fetchone()
    finally:
        conn.close()
    if att is None:
        print("ERROR: no main_episode/youtube publishing_attempts row found.", file=sys.stderr)
        return 4
    att = dict(att)
    print(f"local attempt {att['attempt_id']} state={att['state']} "
          f"intent={att['publication_intent_id']} err={att['last_error_category']}")
    if att["state"] == "succeeded":
        print("already succeeded -- nothing to do (idempotent no-op).")
        return 0
    if att["state"] != "ambiguous":
        print(f"ERROR: attempt state is {att['state']!r}, expected 'ambiguous'.", file=sys.stderr)
        return 4
    _validate_transition("ambiguous", "succeeded")  # raises if not allowed

    if not args.apply:
        print("\nDRY RUN. Re-run with --apply --i-understand-this-is-production to:")
        print(f"  - INSERT publishing_receipts (provider_publication_id={ZERNIO_POST_ID})")
        print(f"  - transition attempt {att['attempt_id']}  ambiguous -> succeeded")
        print(f"  - UPDATE publication_intents {att['publication_intent_id']} -> succeeded")
        print(f"  - INSERT provider_content_mappings (status=confirmed)")
        return 0

    # ---- 3. one transaction, mirroring record_success() ------------
    conn = get_connection(db)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now_iso()
            receipt_id = _new_id("receipt")
            conn.execute(
                """
                INSERT INTO publishing_receipts
                    (receipt_id, attempt_id, provider_id, platform, provider_publication_id,
                     native_platform_content_id, provider_status, acknowledged_at,
                     raw_receipt_metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (receipt_id, att["attempt_id"], att["provider_id"], PLATFORM, ZERNIO_POST_ID,
                 None, "scheduled", now,
                 json.dumps({"reconciled_from": "ambiguous_read_timeout",
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
                conn, provider_id=att["provider_id"], platform=PLATFORM, channel_id=CHANNEL,
                episode_id=EP, deliverable_id="main_episode",
                provider_profile_id=None, provider_account_id=None,
                provider_content_id=ZERNIO_POST_ID, native_platform_content_id=None,
                publication_intent_id=att["publication_intent_id"], attempt_id=att["attempt_id"],
                receipt_id=receipt_id, status="confirmed", now=now,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()

    print(f"\nRECONCILED. attempt {att['attempt_id']} -> succeeded; "
          f"provider_content_mappings -> {ZERNIO_POST_ID} (confirmed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
