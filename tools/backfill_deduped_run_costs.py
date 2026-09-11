#!/usr/bin/env python3
"""
tools/backfill_deduped_run_costs.py

hypogeum/finance.py::write_episode_rollup() was fixed (commit b5c76db,
2026-09-11) to roll the de-duplicated episode total (get_deduped_episode_total)
into runs.total_cost_usd instead of the old get_reconciled_episode_total,
which double-counted grant-ledger charges against the legacy `costs` rows
that duplicate them. That fix only applies going forward -- every run
already sitting in the DB still carries the stale, wrong total.

Swept all 56 runs with a non-NULL total_cost_usd on 2026-09-11: every one
differs from the deduped total by more than $0.01. Most were UNDERcounted
by ~$9-14 (the well-known "dashboard shows ~$2, true cost is ~$10-16 per
grant-era episode" gap); two multi-deliverable episodes (ep_f3619022,
ep_af078f34) were OVERcounted by $20-70 (double-booked legacy+ledger
charges). Grand total: stored $611.60 vs deduped $591.17.

This tool re-runs write_episode_rollup(run_id, episode_id) for every run
that currently has a non-NULL total_cost_usd -- the exact same call the
pipeline makes on completion, just replayed now that the fix is live. It
also rewrites each episode's .hypogeum-assets/finance/{episode_id}_finance.json
(harmless -- that file only ever reflected the legacy `costs` table, same
as before). Runs with total_cost_usd IS NULL (96 of them, mostly failed/
pre-grant runs) are intentionally left alone -- out of scope, unverified
whether "should be zero" or "should be populated".

SAFETY: dry-run by default (prints before/after per run_id, no writes).
--apply performs the DB + JSON writes.

    python3 tools/backfill_deduped_run_costs.py
    python3 tools/backfill_deduped_run_costs.py --apply \
        --i-understand-this-is-production
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--i-understand-this-is-production", action="store_true")
    args = ap.parse_args(argv)

    _PROD_DB = (_ROOT / ".hypogeum-assets" / "hypogeum.db").resolve()
    db = (args.db or _PROD_DB).resolve()
    if db == _PROD_DB and args.apply and not args.i_understand_this_is_production:
        print("ERROR: refusing the production DB without --i-understand-this-is-production.",
              file=sys.stderr)
        return 2

    from hypogeum.db import get_connection
    from hypogeum.cost_projection import get_deduped_episode_total
    from hypogeum.finance import write_episode_rollup

    conn = get_connection(db)
    try:
        rows = conn.execute(
            "SELECT run_id, episode_id, total_cost_usd FROM runs "
            "WHERE total_cost_usd IS NOT NULL ORDER BY episode_id, run_id"
        ).fetchall()
    finally:
        conn.close()

    print(f"{len(rows)} run(s) with a stored total_cost_usd to check.\n")

    changed = 0
    unchanged = 0
    total_delta = 0.0
    for r in rows:
        run_id, episode_id, stored = r["run_id"], r["episode_id"], r["total_cost_usd"]
        conn = get_connection(db)
        try:
            deduped = float(get_deduped_episode_total(conn, episode_id))
        finally:
            conn.close()
        delta = round(deduped - stored, 2)
        if abs(delta) <= 0.01:
            unchanged += 1
            continue
        changed += 1
        total_delta += delta
        tag = f"{episode_id} / {run_id[:8]}"
        if not args.apply:
            print(f"  PLAN  {tag}: ${stored:.2f} -> ${deduped:.2f}  ({delta:+.2f})")
            continue
        write_episode_rollup(run_id, episode_id, db_path=db)
        print(f"  DONE  {tag}: ${stored:.2f} -> ${deduped:.2f}  ({delta:+.2f})")

    verb = "would change" if not args.apply else "changed"
    print(f"\n{changed} run(s) {verb}, {unchanged} already correct. "
          f"Net delta: {total_delta:+.2f}")
    if not args.apply:
        print("\nDRY RUN. Re-run with --apply --i-understand-this-is-production to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
