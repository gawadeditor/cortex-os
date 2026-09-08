"""
tests/test_orphan_episodes.py — Orphaned episode + topic reconciliation

Covers the rows a mid-pipeline crash strands that orphan_runs.py does not:
  - episode 'in_progress' + terminal-dead run   → superseded
  - episode 'in_progress' + still-running run    → left alone
  - episode 'in_progress' + live lease on run    → left alone
  - episode 'in_progress' + no run row at all    → superseded
  - 'claimed' topic pointing at a superseded ep  → released to 'queued'
  - 'claimed' topic pointing at a live ep        → left alone
  - 'claimed' topic with episode_id NULL         → left alone
  - 'complete' episode                           → never touched
  - clean database                               → no-op
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from hypogeum.orphan_episodes import reconcile_orphaned_episodes


def _db(tmp_path: Path) -> Path:
    from hypogeum.db import initialize_schema
    db = tmp_path / "h.db"
    initialize_schema(db)
    return db


def _conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def _insert_run(db: Path, run_id: str, status: str) -> None:
    conn = _conn(db)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status, metadata_version) "
            "VALUES (?, ?, datetime('now'), ?, 'v1')",
            (run_id, f"ep_for_{run_id[:8]}", status),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_episode(db: Path, episode_id: str, status: str, run_id: str | None) -> None:
    conn = _conn(db)
    try:
        conn.execute(
            "INSERT INTO episodes (episode_id, title, created_at, status, run_id, metadata_version) "
            "VALUES (?, ?, datetime('now'), ?, ?, 'v1')",
            (episode_id, f"T {episode_id}", status, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_topic(db: Path, topic_id: str, status: str, episode_id: str | None) -> None:
    conn = _conn(db)
    try:
        conn.execute(
            """
            INSERT INTO episode_topics
                (topic_id, channel_id, topic_text, status, priority,
                 claimed_at, created_at, updated_at, source, episode_id)
            VALUES (?, 'hypogeum', 'a topic', ?, 0,
                    datetime('now'), datetime('now'), datetime('now'), 'manual', ?)
            """,
            (topic_id, status, episode_id),
        )
        conn.commit()
    finally:
        conn.close()


def _episode_status(db: Path, episode_id: str) -> str:
    conn = _conn(db)
    try:
        row = conn.execute(
            "SELECT status FROM episodes WHERE episode_id=?", (episode_id,)
        ).fetchone()
        return row["status"] if row else "missing"
    finally:
        conn.close()


def _topic(db: Path, topic_id: str) -> sqlite3.Row:
    conn = _conn(db)
    try:
        return conn.execute(
            "SELECT status, episode_id, claimed_at FROM episode_topics WHERE topic_id=?",
            (topic_id,),
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------


def test_in_progress_episode_with_failed_run_is_superseded(tmp_path):
    db = _db(tmp_path)
    run_id = str(uuid.uuid4())
    _insert_run(db, run_id, "failed")
    _insert_episode(db, "ep_dead", "in_progress", run_id)

    out = reconcile_orphaned_episodes(db_path=db)

    assert out["superseded_episode_ids"] == ["ep_dead"]
    assert _episode_status(db, "ep_dead") == "superseded"


def test_in_progress_episode_with_cancelled_run_is_superseded(tmp_path):
    db = _db(tmp_path)
    run_id = str(uuid.uuid4())
    _insert_run(db, run_id, "cancelled")
    _insert_episode(db, "ep_cx", "in_progress", run_id)

    reconcile_orphaned_episodes(db_path=db)

    assert _episode_status(db, "ep_cx") == "superseded"


def test_in_progress_episode_with_running_run_is_left_alone(tmp_path):
    db = _db(tmp_path)
    run_id = str(uuid.uuid4())
    _insert_run(db, run_id, "running")
    _insert_episode(db, "ep_live", "in_progress", run_id)

    out = reconcile_orphaned_episodes(db_path=db)

    assert out["superseded_episode_ids"] == []
    assert _episode_status(db, "ep_live") == "in_progress"


def test_in_progress_episode_with_live_lease_is_left_alone(tmp_path):
    db = _db(tmp_path)
    run_id = str(uuid.uuid4())
    # Run row already 'failed' but a lease is still live — a legitimate
    # out-of-band executor. Must not be clobbered.
    _insert_run(db, run_id, "failed")
    _insert_episode(db, "ep_leased", "in_progress", run_id)

    from datetime import datetime, timedelta, timezone
    from hypogeum.db import get_connection, acquire_lease

    now = datetime.now(timezone.utc)
    conn = get_connection(db)
    try:
        acquire_lease(
            conn, f"run:{run_id}", "other-proc",
            acquired_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=30)).isoformat(),
        )
        conn.commit()
    finally:
        conn.close()

    out = reconcile_orphaned_episodes(db_path=db)

    assert out["superseded_episode_ids"] == []
    assert _episode_status(db, "ep_leased") == "in_progress"


def test_in_progress_episode_with_no_run_row_is_superseded(tmp_path):
    db = _db(tmp_path)
    _insert_episode(db, "ep_norun", "in_progress", None)

    reconcile_orphaned_episodes(db_path=db)

    assert _episode_status(db, "ep_norun") == "superseded"


def test_claimed_topic_for_superseded_episode_is_released(tmp_path):
    db = _db(tmp_path)
    run_id = str(uuid.uuid4())
    _insert_run(db, run_id, "failed")
    _insert_episode(db, "ep_dead", "in_progress", run_id)
    _insert_topic(db, "tp_1", "claimed", "ep_dead")

    out = reconcile_orphaned_episodes(db_path=db)

    assert "tp_1" in out["released_topic_ids"]
    row = _topic(db, "tp_1")
    assert row["status"] == "queued"
    assert row["episode_id"] is None
    assert row["claimed_at"] is None


def test_claimed_topic_for_missing_episode_is_released(tmp_path):
    db = _db(tmp_path)
    _insert_topic(db, "tp_ghost", "claimed", "ep_never_existed")

    out = reconcile_orphaned_episodes(db_path=db)

    assert "tp_ghost" in out["released_topic_ids"]
    assert _topic(db, "tp_ghost")["status"] == "queued"


def test_claimed_topic_for_live_episode_is_left_alone(tmp_path):
    db = _db(tmp_path)
    run_id = str(uuid.uuid4())
    _insert_run(db, run_id, "running")
    _insert_episode(db, "ep_live", "in_progress", run_id)
    _insert_topic(db, "tp_live", "claimed", "ep_live")

    out = reconcile_orphaned_episodes(db_path=db)

    assert out["released_topic_ids"] == []
    assert _topic(db, "tp_live")["status"] == "claimed"


def test_claimed_topic_with_null_episode_id_is_left_alone(tmp_path):
    db = _db(tmp_path)
    _insert_topic(db, "tp_null", "claimed", None)

    out = reconcile_orphaned_episodes(db_path=db)

    assert out["released_topic_ids"] == []
    assert _topic(db, "tp_null")["status"] == "claimed"


def test_complete_episode_is_never_touched(tmp_path):
    db = _db(tmp_path)
    run_id = str(uuid.uuid4())
    _insert_run(db, run_id, "completed")
    _insert_episode(db, "ep_done", "complete", run_id)

    reconcile_orphaned_episodes(db_path=db)

    assert _episode_status(db, "ep_done") == "complete"


def test_clean_database_is_a_noop(tmp_path):
    db = _db(tmp_path)
    out = reconcile_orphaned_episodes(db_path=db)
    assert out == {"superseded_episode_ids": [], "released_topic_ids": []}


def test_idempotent_second_pass_does_nothing_new(tmp_path):
    db = _db(tmp_path)
    run_id = str(uuid.uuid4())
    _insert_run(db, run_id, "failed")
    _insert_episode(db, "ep_dead", "in_progress", run_id)
    _insert_topic(db, "tp_1", "claimed", "ep_dead")

    reconcile_orphaned_episodes(db_path=db)
    out2 = reconcile_orphaned_episodes(db_path=db)

    assert out2 == {"superseded_episode_ids": [], "released_topic_ids": []}
