#!/usr/bin/env python3
"""
tools/cleanup_hypogeum_schedules_and_queue.py

One-time hygiene for the hypogeum channel's schedule + queue state, found
during the 2026-08-31 dashboard review:

  1. Duplicate channel schedules. run_dashboard.channel_operations_edit
     used to disable-all + create-new on every save, leaving a trail of
     dead `hypogeum_daily_shorts` / `hypogeum_daily` rows (the reuse fix
     landed in the same change as this script). Delete the disabled
     channel schedules that never ran (last_run_at IS NULL) and have no
     scheduled_runs referencing them; the rest are left disabled.

  2. Orphan factory schedule `daily_production_run` (scope=factory,
     channel_id=NULL). It fires daily and produces channel-less
     scheduled_runs that immediately cancel — superseded by the
     channel-scoped hypogeum_daily_shorts. Set status='disabled'.

  3. Stale claimed topic. "The Erdstall Tunnels..." (topic 99233dc6) has
     been status='claimed' since 2026-07-31 for episode ep_f3619022 which
     is already produced + published. cancel_topic() -> 'cancelled'
     (never requeued — that episode is done).

  4. Obsolete bundle_cycle_policy. The channel is shorts-only
     (videos_per_week=0) but still carries hypogeum_weekly_release_v1,
     which demands a main_episode and blocks every autonomous Short as a
     "partial bundle". Clear bundle_cycle_policy + deliverable_schedule_policy
     so the publish-now path (run_worker, videos_per_week==0) is used.

SAFETY: dry-run by default. --apply performs the writes. Refuses the
production DB without --i-understand-this-is-production.

    python3 tools/cleanup_hypogeum_schedules_and_queue.py
    python3 tools/cleanup_hypogeum_schedules_and_queue.py --apply \
        --i-understand-this-is-production
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

CHANNEL = "hypogeum"
_STALE_CLAIMED_TOPIC = "99233dc6-57a9-4bd3-b210-47d21c99f00b"
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
    from hypogeum.scheduler import set_schedule_status
    from hypogeum.topics import cancel_topic
    from hypogeum.channel_config import (
        get_channel_operations_profile, update_channel_operations_profile,
    )

    _prof = get_channel_operations_profile(CHANNEL, db_path=db)
    _cad = (_prof.production_cadence or {}) if _prof else {}
    shorts_only = _cad.get("videos_per_week") == 0 and (_cad.get("shorts_per_week") or 0) >= 1
    obsolete_cycle = bool(shorts_only and (_prof.bundle_cycle_policy or _prof.deliverable_schedule_policy))

    conn = get_connection(db)
    try:
        chan_scheds = list(conn.execute(
            "SELECT schedule_id, label, status, last_run_at FROM schedules "
            "WHERE scope='channel' AND channel_id=? ORDER BY created_at", (CHANNEL,)))
        factory_scheds = list(conn.execute(
            "SELECT schedule_id, label, status FROM schedules "
            "WHERE scope='factory' AND status != 'disabled'"))
        sr_counts = {
            r["schedule_id"]: r["n"] for r in conn.execute(
                "SELECT schedule_id, COUNT(*) n FROM scheduled_runs GROUP BY schedule_id")
        }
        topic = conn.execute(
            "SELECT topic_id, substr(topic_text,1,50) t, status, episode_id "
            "FROM episode_topics WHERE topic_id=?", (_STALE_CLAIMED_TOPIC,)).fetchone()
    finally:
        conn.close()

    # ---- plan ----------------------------------------------------------
    active_chan = [r for r in chan_scheds if r["status"] == "active"]
    deletable = [
        r for r in chan_scheds
        if r["status"] == "disabled" and not r["last_run_at"]
        and sr_counts.get(r["schedule_id"], 0) == 0
    ]
    keep_disabled = [
        r for r in chan_scheds
        if r["status"] == "disabled" and r not in deletable
    ]

    print(f"channel schedules for {CHANNEL} ({len(chan_scheds)}):")
    for r in chan_scheds:
        tag = ("  [ACTIVE — keep]" if r["status"] == "active"
               else "  [DELETE]" if r in deletable
               else "  [disabled — leave, has history]")
        print(f"   {r['label']:28} {r['status']:9} runs={sr_counts.get(r['schedule_id'],0)}{tag}")

    print(f"\nfactory schedules still active ({len(factory_scheds)}):")
    for r in factory_scheds:
        print(f"   {r['label']:28} {r['status']:9}  -> DISABLE")

    print("\nstale claimed topic:")
    if topic is None:
        print("   (not found — already gone)")
    else:
        act = "  -> CANCEL" if topic["status"] == "claimed" else "  (not claimed — leave)"
        print(f"   {topic['t']!r}  status={topic['status']}  ep={topic['episode_id']}{act}")

    print("\nbundle-cycle policy:")
    if obsolete_cycle:
        print("   shorts-only channel still carries bundle_cycle_policy / "
              "deliverable_schedule_policy  -> CLEAR both")
    else:
        print("   (clean — nothing to clear)")

    if len(active_chan) != 1:
        print(f"\nWARNING: expected exactly 1 active channel schedule, found {len(active_chan)}.",
              file=sys.stderr)

    if not args.apply:
        print(f"\nDRY RUN. Would delete {len(deletable)} schedule row(s), disable "
              f"{len(factory_scheds)} factory schedule(s), cancel "
              f"{1 if topic and topic['status'] == 'claimed' else 0} topic(s). "
              "Re-run with --apply --i-understand-this-is-production.")
        return 0

    conn = get_connection(db)
    try:
        n_del = 0
        for r in deletable:
            n_del += conn.execute(
                "DELETE FROM schedules WHERE schedule_id=? AND status='disabled'",
                (r["schedule_id"],)).rowcount
        conn.commit()
    finally:
        conn.close()

    for r in factory_scheds:
        set_schedule_status(r["schedule_id"], "disabled", db_path=db)

    n_topic = 0
    if topic is not None and topic["status"] == "claimed":
        cancel_topic(_STALE_CLAIMED_TOPIC, db_path=db)
        n_topic = 1

    n_policy = 0
    if obsolete_cycle:
        update_channel_operations_profile(
            CHANNEL, db_path=db, bundle_cycle_policy={}, deliverable_schedule_policy={})
        n_policy = 1

    print(f"\nDeleted {n_del} duplicate schedule row(s); disabled {len(factory_scheds)} "
          f"orphan factory schedule(s); cancelled {n_topic} stale topic(s); "
          f"cleared {n_policy} obsolete bundle-cycle policy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
