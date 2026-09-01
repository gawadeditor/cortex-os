"""Queue/run janitor: prune old terminal rows, never touch in-flight episodes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypogeum.db import get_connection, initialize_schema
from hypogeum.queue_janitor import prune_terminal_rows


def _old():
    return (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()


def _recent():
    return (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()


def _seed(db):
    initialize_schema(db)
    conn = get_connection(db)
    try:
        for ep, st in (("ep_done", "published"), ("ep_live", "in_progress")):
            conn.execute(
                "INSERT INTO episodes (episode_id, status, created_at) VALUES (?,?,?)",
                (ep, st, _old()))

        def _q(qid, ep, st, ts):
            conn.execute(
                "INSERT INTO run_queue (queue_id, channel_id, episode_id, status, "
                "queued_at, completed_at, created_at) VALUES (?,?,?,?,?,?,?)",
                (qid, "c", ep, st, ts, ts, ts))

        _q("q1", "ep_done", "completed", _old())    # old + done   -> prune
        _q("q2", "ep_live", "failed", _old())        # old + live   -> keep
        _q("q3", "ep_done", "completed", _recent())  # recent       -> keep
        _q("q4", None, "cancelled", _old())          # old + no ep  -> prune

        def _r(rid, ep, st, ts):
            conn.execute(
                "INSERT INTO runs (run_id, episode_id, started_at, completed_at, status) "
                "VALUES (?,?,?,?,?)", (rid, ep, ts, ts, st))

        _r("r1", "ep_done", "completed", _old())     # old + done   -> prune
        _r("r2", "ep_live", "failed", _old())         # old + live   -> keep
        conn.commit()
    finally:
        conn.close()


def test_prune_only_old_done_rows(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)
    res = prune_terminal_rows(retention_days=45, db_path=db)
    assert res["queue_pruned"] == 2   # q1 + q4
    assert res["runs_pruned"] == 1    # r1

    conn = get_connection(db)
    try:
        q = {r[0] for r in conn.execute("SELECT queue_id FROM run_queue")}
        r = {r[0] for r in conn.execute("SELECT run_id FROM runs")}
    finally:
        conn.close()
    assert q == {"q2", "q3"}
    assert r == {"r2"}


def test_dry_run_changes_nothing(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)
    res = prune_terminal_rows(retention_days=45, db_path=db, dry_run=True)
    assert res["queue_pruned"] == 2 and res["dry_run"] is True
    conn = get_connection(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM run_queue").fetchone()[0] == 4
    finally:
        conn.close()
