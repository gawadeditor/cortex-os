#!/usr/bin/env python3
"""
tools/configure_hypogeum_shorts_only.py

Phase 3 config flip: switch the `hypogeum` channel from (1 long video + 3
Shorts) per week to **7 Shorts per week, one per day**.

This is the ONE-SHOT operator action that turns on everything the
SHORT_NATIVE code path already supports:

  1. ChannelOperationsProfile.production_cadence -> {"videos_per_week": 0,
     "shorts_per_week": 7}
  2. ChannelOperationsProfile.deliverable_bundle_definitions ->
     [{"bundle_id": "short", "target_buffer_count": 7}]
  3. channel_profile_settings.yt_videos_per_week = 0 / yt_shorts_per_week = 7
  4. a single `channel`-scoped daily schedule (cadence="daily",
     max_runs_per_day=1) firing at 14:00 UTC

SAFETY: dry-run by default. It prints the exact writes and does NOTHING
unless you pass --apply AND an explicit --db path. It refuses to touch the
production DB (.hypogeum-assets/hypogeum.db) unless you ALSO pass
--i-understand-this-is-production.

    # inspect the plan (no writes):
    python3 tools/configure_hypogeum_shorts_only.py

    # apply to a scratch DB:
    python3 tools/configure_hypogeum_shorts_only.py --apply --db /tmp/x.db

    # apply to production (deliberate):
    python3 tools/configure_hypogeum_shorts_only.py --apply \
        --db .hypogeum-assets/hypogeum.db --i-understand-this-is-production
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CHANNEL_ID = "hypogeum"
_PROD_DB = (Path(__file__).resolve().parent.parent / ".hypogeum-assets" / "hypogeum.db").resolve()

CADENCE = {"videos_per_week": 0, "shorts_per_week": 7}
BUNDLES = [{"bundle_id": "short", "target_buffer_count": 7}]


def _next_1400_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    cand = datetime.combine(now.date(), time(14, 0), tzinfo=timezone.utc)
    if cand <= now:
        cand += timedelta(days=1)
    return cand


def _plan_lines(schedule_at: datetime) -> list[str]:
    return [
        f"channel_operations_profiles[{CHANNEL_ID}].production_cadence  = {CADENCE}",
        f"channel_operations_profiles[{CHANNEL_ID}].deliverable_bundle_definitions = {BUNDLES}",
        f"channel_profile_settings.yt_videos_per_week = 0",
        f"channel_profile_settings.yt_shorts_per_week = 7",
        f"schedules: + channel/{CHANNEL_ID} cadence=daily max_runs_per_day=1 "
        f"next_run_at={schedule_at.isoformat()} label=hypogeum_daily_shorts",
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually perform the writes")
    ap.add_argument("--db", type=Path, default=None, help="explicit SQLite path (required with --apply)")
    ap.add_argument("--i-understand-this-is-production", action="store_true",
                    help="required to target .hypogeum-assets/hypogeum.db")
    args = ap.parse_args(argv)

    schedule_at = _next_1400_utc()

    print("Planned changes:")
    for line in _plan_lines(schedule_at):
        print(f"  - {line}")
    print()

    if not args.apply:
        print("DRY RUN — nothing written. Re-run with --apply --db <path> to execute.")
        return 0

    if args.db is None:
        print("ERROR: --apply requires an explicit --db <path>.", file=sys.stderr)
        return 2

    db = args.db.resolve()
    if db == _PROD_DB and not args.i_understand_this_is_production:
        print("ERROR: refusing to write to the production DB without "
              "--i-understand-this-is-production.", file=sys.stderr)
        return 2

    from hypogeum.channel_config import (
        get_channel_operations_profile,
        update_channel_operations_profile,
    )
    from hypogeum.channel_profile import update_channel_profile
    from hypogeum.db import get_connection
    from hypogeum.scheduler import create_schedule, set_schedule_status

    if get_channel_operations_profile(CHANNEL_ID, db_path=db) is None:
        print(f"ERROR: no ChannelOperationsProfile for {CHANNEL_ID!r} in {db}.", file=sys.stderr)
        return 1

    update_channel_operations_profile(
        CHANNEL_ID, db_path=db,
        production_cadence=CADENCE,
        deliverable_bundle_definitions=BUNDLES,
    )
    update_channel_profile({"yt_videos_per_week": 0, "yt_shorts_per_week": 7}, db_path=db)

    # Idempotent: retire every existing channel-scoped schedule for this
    # channel so we never end up with two daily schedules double-dispatching
    # (max_runs_per_day is NOT enforced by scheduler.tick).
    conn = get_connection(db)
    try:
        existing = conn.execute(
            "SELECT schedule_id, label, status FROM schedules "
            "WHERE scope = 'channel' AND channel_id = ?",
            (CHANNEL_ID,),
        ).fetchall()
    finally:
        conn.close()
    for row in existing:
        if row["status"] != "disabled":
            set_schedule_status(row["schedule_id"], "disabled", db_path=db)
            print(f"  retired prior schedule {row['schedule_id']} ({row['label']})")

    sched = create_schedule(
        scope="channel", channel_id=CHANNEL_ID, cadence="daily",
        next_run_at=schedule_at, max_runs_per_day=1,
        label="hypogeum_daily_shorts", db_path=db,
    )
    print(f"Applied. schedule_id={getattr(sched, 'schedule_id', sched)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
