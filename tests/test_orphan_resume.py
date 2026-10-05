"""2026-10-05: a production run stopped by the computer must not be lost.
- Asleep past the lease TTL: the worker still owns the run -> not orphaned.
- Process really died: the orphaned episode gets one bounded auto-retry that
  resumes from its checkpoints (topic reused, never a second topic row) --
  existing behaviour (orphan_runs._schedule_orphan_retries), pinned here."""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from hypogeum.db import initialize_schema, set_worker_control
from hypogeum.orphan_runs import reconcile_orphaned_runs


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "h.db"
    initialize_schema(db)
    return db


def _seed(db: Path, *, failed_before: int = 0):
    run_id, ep, qid, tid = str(uuid.uuid4()), "ep_sleep", str(uuid.uuid4()), str(uuid.uuid4())
    c = sqlite3.connect(str(db))
    c.execute("INSERT INTO runs (run_id, episode_id, channel_id, started_at, status, metadata_version) "
              "VALUES (?, ?, 'hypogeum', datetime('now'), 'running', 'v1')", (run_id, ep))
    for i in range(failed_before):
        c.execute("INSERT INTO runs (run_id, episode_id, channel_id, started_at, status, metadata_version) "
                  "VALUES (?, ?, 'hypogeum', datetime('now','-1 day'), 'failed', 'v1')", (f"old{i}", ep))
    c.execute("INSERT INTO run_queue (queue_id, channel_id, episode_id, priority, status, worker_id, queued_at, "
              "claimed_at, created_at, source) VALUES (?, 'hypogeum', ?, 0, 'claimed', 'w', datetime('now'), "
              "datetime('now'), datetime('now'), 'schedule')", (qid, ep))
    c.execute("INSERT INTO episode_topics (topic_id, channel_id, topic_text, status, priority, created_at, "
              "updated_at, episode_id) VALUES (?, 'hypogeum', 'The Nebra Disc', 'claimed', 0, datetime('now'), "
              "datetime('now'), ?)", (tid, ep))
    c.commit()
    c.close()
    return run_id, ep, qid, tid


def _q(db, sql, *a):
    c = sqlite3.connect(str(db))
    try:
        return c.execute(sql, a).fetchall()
    finally:
        c.close()


def test_sleeping_worker_keeps_its_run(tmp_path):
    db = _db(tmp_path)
    run_id, ep, qid, _ = _seed(db)
    set_worker_control("active_queue_id", qid, db_path=db)
    assert reconcile_orphaned_runs(db, worker_process_alive=True, worker_db_status="running") == []
    assert _q(db, "SELECT status FROM runs WHERE run_id=?", run_id)[0][0] == "running"


def test_dead_run_resumes_with_one_auto_retry(tmp_path):
    db = _db(tmp_path)
    run_id, ep, _, tid = _seed(db)
    assert reconcile_orphaned_runs(db, worker_process_alive=False, worker_db_status="stopped") == [run_id]
    retries = _q(db, "SELECT retry_count, status FROM run_queue WHERE episode_id=? AND source='auto_retry'", ep)
    assert retries == [(1, "queued")]
    assert _q(db, "SELECT status FROM episode_topics WHERE topic_id=?", tid)[0][0] == "queued"
    assert _q(db, "SELECT COUNT(*) FROM episode_topics WHERE episode_id=?", ep)[0][0] == 1   # no second topic
