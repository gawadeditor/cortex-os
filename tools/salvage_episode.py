#!/usr/bin/env python3
"""
tools/salvage_episode.py — operator wrapper for hypogeum.episode_salvage.

Re-admits a failed-but-near-complete episode and resumes it through
run_pipeline() (VPD Phase 2 / clip-repair only — see the module docstring
for the no-plan-hash-drift contract). Use tools/episode_cost_audit.py to
pick which episodes are worth it (high 'prio' = cheap to finish).

  python3 tools/salvage_episode.py --episode ep_XXXX --check      # preconditions only
  python3 tools/salvage_episode.py --episode ep_XXXX --dry-run    # what it would do
  python3 tools/salvage_episode.py --episode ep_XXXX --apply --i-understand-this-spends-money

SAFETY: refuses to --apply while the autonomous worker is unpaused
(worker_control.queue_paused != '1') OR any run is 'running' — a
concurrent run_pipeline would corrupt state. Pause the worker first
(hypogeum.muapi_spend_breaker.clear_breaker's inverse, or
set_worker_control('queue_paused','1')), or stop it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hypogeum.db as db  # noqa: E402
from hypogeum.config import Config  # noqa: E402
from hypogeum.episode_salvage import salvage_episode, salvage_preconditions  # noqa: E402
from hypogeum.logger import get_logger  # noqa: E402


def _worker_is_safe(db_path: Path) -> tuple[bool, str]:
    conn = db.get_connection(db_path)
    try:
        running = conn.execute("SELECT COUNT(*) FROM runs WHERE status = 'running'").fetchone()[0]
        row = conn.execute(
            "SELECT value FROM worker_control WHERE key = 'queue_paused'"
        ).fetchone()
        paused = row is not None and str(row[0]) == "1"
    finally:
        conn.close()
    if running:
        return False, f"{running} run(s) currently 'running'"
    if not paused:
        return False, "worker queue is not paused (worker_control.queue_paused != '1')"
    return True, "worker idle and queue paused"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--db", default=str(db._DEFAULT_DB_PATH))
    ap.add_argument("--check", action="store_true", help="print preconditions and exit")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--i-understand-this-spends-money", action="store_true")
    args = ap.parse_args()

    db_path = Path(args.db)

    pre = salvage_preconditions(args.episode, db_path=db_path)
    print(f"episode        : {args.episode}")
    print(f"channel        : {pre['channel_id']}")
    print(f"last run       : {pre['run_status']}")
    print(f"admission      : {pre['admission_status']}")
    print(f"clips on disk  : {pre['clip_count']}")
    print(f"plan file      : {pre['plan_file']}")
    print(f"finance auth   : {pre['finance_auth']}")
    print(f"vp resumable   : {pre['visual_package_resumable']} ({pre['manifest_asset_count']} assets)")
    print(f"preconditions  : {'OK' if pre['ok'] else 'BLOCKED'}")
    for b in pre["blockers"]:
        print(f"   - {b}")
    if args.check:
        return 0 if pre["ok"] else 1
    if not pre["ok"]:
        return 1

    if args.dry_run or not args.apply:
        res = salvage_episode(args.episode, config=Config(), db_path=db_path, dry_run=True)
        print(f"\nDRY-RUN: {res['reason']}")
        return 0

    if not args.i_understand_this_spends_money:
        print("\nrefusing: --apply needs --i-understand-this-spends-money")
        return 2

    safe, why = _worker_is_safe(db_path)
    if not safe:
        print(f"\nrefusing to --apply: {why}")
        return 2

    log = get_logger(episode_id=args.episode, agent="salvage_episode")
    res = salvage_episode(args.episode, config=Config(), db_path=db_path, log=log)
    print(f"\nran={res['ran']}  pipeline_status={res['pipeline_status']}  ok={res['ok']}")
    print(f"reason: {res['reason']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
