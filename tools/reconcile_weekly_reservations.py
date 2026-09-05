#!/usr/bin/env python3
"""
tools/reconcile_weekly_reservations.py — run the weekly-budget reaper by
hand.

The worker runs hypogeum.weekly_reservation_reaper every iteration; this
is the operator-facing one-shot for when the worker is stopped, or to see
exactly what it would reconcile. It trues each DEAD episode's committed
channel_weekly plan hold (superseded / failed-with-no-later-success, never
published, not currently live) down to that episode's real post-plan
spend, freeing the padding between the plan-maximum estimate and the
actual charge back to the channel's weekly ceiling.

    python3 tools/reconcile_weekly_reservations.py                # all channels
    python3 tools/reconcile_weekly_reservations.py --channel hypogeum
"""
import argparse
import json
import sys

from hypogeum.db import get_connection, sum_reserved_and_committed
from hypogeum.weekly_reservation_reaper import (
    reconcile_terminal_episode_weekly_reservations,
)
from hypogeum.weekly_budget import compute_weekly_window
from datetime import datetime, timezone


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default=None, help="limit to one channel_id")
    args = ap.parse_args()

    out = reconcile_terminal_episode_weekly_reservations(channel_id=args.channel)
    print(json.dumps(out, indent=2))

    if out["reaped"]:
        wk = datetime.now(timezone.utc).isocalendar()
        week_key = f"{wk[0]}-W{wk[1]:02d}"
        conn = get_connection()
        channels = sorted({r["channel_id"] for r in out["reaped"]})
        for ch in channels:
            s = float(sum_reserved_and_committed(conn, "channel_weekly", f"{ch}:{week_key}"))
            print(f"  {ch} {week_key}: ${s:.2f} committed/reserved")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
