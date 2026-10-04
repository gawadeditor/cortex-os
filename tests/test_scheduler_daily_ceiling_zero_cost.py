"""A failed attempt that spent nothing doesn't use up the channel's daily
production attempt (2026-10-05: a $0 failure left the buffer short a day)."""
from __future__ import annotations

import uuid

from hypogeum import scheduler as sch
from hypogeum.db import get_connection, initialize_schema


def _attempt(conn, channel, created, queue_status, episode, cost=0.0):
    sr = str(uuid.uuid4())
    conn.execute("INSERT INTO scheduled_runs (scheduled_run_id, schedule_id, channel_id, scheduled_for, status, "
                 "created_at) VALUES (?,?,?,?,?,?)", (sr, "sch1", channel, created, "triggered", created))
    conn.execute("INSERT INTO run_queue (queue_id, scheduled_run_id, channel_id, episode_id, priority, status, "
                 "queued_at, created_at) VALUES (?,?,?,?,0,?,?,?)",
                 (str(uuid.uuid4()), sr, channel, episode, queue_status, created, created))
    if cost:
        conn.execute("INSERT INTO costs (cost_id, run_id, episode_id, provider, model, call_type, tokens_or_credits, "
                     "cost_usd, called_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (str(uuid.uuid4()), "run_" + episode, episode, "muapi", "m", "scene", 0, cost, created))


def test_zero_cost_failure_not_counted(tmp_path):
    db = tmp_path / "s.db"
    initialize_schema(db)
    conn = get_connection(db)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        _attempt(conn, "ch", "2026-10-05T01:00:00+00:00", "failed", "ep_free")
        conn.commit()
        assert sch._scheduled_runs_since("ch", conn, "2026-10-05T00:00:00+00:00") == 0
        _attempt(conn, "ch", "2026-10-05T02:00:00+00:00", "failed", "ep_paid", cost=4.5)
        _attempt(conn, "ch", "2026-10-05T03:00:00+00:00", "completed", "ep_ok")
        conn.commit()
        assert sch._scheduled_runs_since("ch", conn, "2026-10-05T00:00:00+00:00") == 2
    finally:
        conn.close()
