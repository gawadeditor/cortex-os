#!/usr/bin/env python3
"""
tools/reconcile_stuck_submitting_auths.py — one-shot sweep for scene
authorizations permanently stranded at 'submitting' (2026-09-06).

THE LEAK THIS CLOSES
--------------------
A per-scene generation whose poll times out post-submission returns
outcome="unknown_acceptance"; hypogeum.authorization_store.
record_submission_outcome() then advances that scene's AuthorizationRecord
AUTHORIZED -> SUBMITTING and correctly leaves it there (an honest "we
submitted, acceptance unknown"). hypogeum.muapi.
reconcile_stuck_scene_authorization() is DELIBERATELY narrow — it only
releases such a record when a re-poll proves the job failed/cancelled or
never reached the provider; when the re-poll shows the job actually
COMPLETED it logs `reconcile_scene_authorization_recoverable_but_unhandled`
and leaves the record at 'submitting' for an operator.

submission_gate check `03_authorization_active_and_unconsumed` then denies
every later attempt on that same identity ("authorization state is
'submitting', not 'authorized'"). As of 2026-09-06 hypogeum.muapi.
submit_scene() escalates to a fresh ':recovery{n}' identity on that exact
denial, so NEW episodes recover — but the historical stranded records
(50 as of 2026-09-06, some holding still-'reserved' CALL/EPISODE budget
rows) stay on the books.

WHAT THIS DOES
--------------
For every `authorization_records` row at current_state='submitting':
  * transition SUBMITTING -> CANCELLED (the state machine's documented
    "withdrawn by policy" edge; evidence cancelled_by/reason), persist;
  * release any `budget_reservations` row whose idempotency_key equals the
    authorization_id and whose status is still 'reserved' (the per-scene
    CALL / EPISODE holds that reconcile never got to settle).

It does NOT try to recover the paid-for completed clip (that is the
separate, riskier "auto-recovery" path the operator has not asked for) and
it never touches a 'committed' row.

SAFETY
------
Refuses to run while ANY `runs` row is status='running' — a stranded
'submitting' record could be a live in-flight scene. Idempotent: a record
already CANCELLED is not in the query. Dry-run by default; --apply writes.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hypogeum.db as db  # noqa: E402
from hypogeum.authorization_lifecycle import AuthorizationState  # noqa: E402
from hypogeum.authorization_store import (  # noqa: E402
    get_authorization_record,
    persist_authorization_record,
)

_CANCEL_REASON = (
    "poll-timeout unknown-acceptance never reconciled (job later found complete "
    "but not recoverable in place); cancelled so a fresh ':recovery{n}' identity "
    "can regenerate the scene — sweep 2026-09-06"
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(db._DEFAULT_DB_PATH), help="SQLite DB path")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="cap rows processed (0 = all)")
    args = ap.parse_args()

    db_path = Path(args.db)
    conn = db.get_connection(db_path)
    conn.row_factory = __import__("sqlite3").Row
    now = datetime.now(timezone.utc).isoformat()

    running = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE status = 'running'"
    ).fetchone()[0]
    if running:
        print(f"REFUSING: {running} run(s) currently 'running' — rerun when the worker is idle.")
        return 2

    rows = conn.execute(
        "SELECT authorization_id FROM authorization_records "
        "WHERE current_state = 'submitting' ORDER BY updated_at"
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]

    print(f"{'APPLY' if args.apply else 'DRY-RUN'} — {len(rows)} authorization(s) at 'submitting'")
    cancelled = 0
    released_rows = 0
    released_usd = 0.0
    errors = 0

    for r in rows:
        aid = r["authorization_id"]
        try:
            rec = get_authorization_record(conn, aid)
            if rec is None or rec.current_state != AuthorizationState.SUBMITTING:
                continue

            reserved = conn.execute(
                "SELECT reservation_id, scope_kind, reserved_amount FROM budget_reservations "
                "WHERE idempotency_key = ? AND status = 'reserved'",
                (aid,),
            ).fetchall()
            r_usd = sum(float(x["reserved_amount"] or 0) for x in reserved)

            print(
                f"  {aid}  -> CANCELLED   "
                f"(+{len(reserved)} reserved row(s), ${r_usd:.2f})"
            )

            if args.apply:
                new_rec = rec.transition(
                    AuthorizationState.CANCELLED,
                    evidence={
                        "cancelled_by": "reconcile_stuck_submitting_auths_sweep",
                        "reason": _CANCEL_REASON,
                    },
                    at=now,
                )
                persist_authorization_record(conn, new_rec)
                for x in reserved:
                    if db.release_budget_reservation(conn, x["reservation_id"], at=now):
                        released_rows += 1
                        released_usd += float(x["reserved_amount"] or 0)
                conn.commit()

            cancelled += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"  {aid}  !! {type(exc).__name__}: {exc}")

    conn.close()
    print(
        f"\n{'applied' if args.apply else 'would cancel'}: {cancelled} authorization(s); "
        f"released {released_rows} reserved budget row(s) (${released_usd:.2f}); errors: {errors}"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
