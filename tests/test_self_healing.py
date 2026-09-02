"""
tests/test_self_healing.py — Unit verification for hypogeum/self_healing.py

Exercises Recovery & Self-Healing (Phase 3 Production Hardening, P0.4):
  - periodic_check() calls the existing, unmodified
    reconcile_orphaned_runs() (H1) rather than reimplementing it
  - periodic_check() is a safe no-op when called while the worker is
    itself mid-pipeline (worker_db_status='running')
  - Exhausted-retry queue items (status='failed', retry_count >=
    QUEUE_MAX_AUTO_RETRIES) get an OIS signal emitted via the existing
    hypogeum.ois substrate — not a parallel alerting path
  - The same channel's exhausted item is not re-alerted every call
    (dedup_any_open behavior), matching the existing OIS convention for
    persistent conditions

All tests use tmp_path — never the production database, per
CLAUDE.md's Test Isolation rules.

Run with:  python3 -m pytest tests/test_self_healing.py -v
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.db import initialize_schema
from hypogeum.self_healing import periodic_check


def make_db(path: Path) -> Path:
    initialize_schema(path)
    return path


def insert_run(db_path: Path, run_id: str, status: str, started_at_iso: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status) VALUES (?, ?, ?, ?)",
            (run_id, f"ep_{run_id}", started_at_iso, status),
        )
        conn.commit()
    finally:
        conn.close()


def insert_queue_item(
    db_path: Path, queue_id: str, status: str, retry_count: int = 0,
    channel_id: str = "hypogeum", episode_id: str = "ep_1",
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO run_queue "
            "(queue_id, status, retry_count, channel_id, episode_id, queued_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (queue_id, status, retry_count, channel_id, episode_id, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def iso(minutes_ago: float = 0) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


class FakeConfig:
    QUEUE_MAX_AUTO_RETRIES = 2


# ---------------------------------------------------------------------------
# Orphan reconciliation (delegates to existing H1 mechanism)
# ---------------------------------------------------------------------------

class TestPeriodicCheckOrphanReconciliation:
    def test_reconciles_an_orphaned_running_run(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        insert_run(db_path, "r1", "running", iso(200))

        result = periodic_check(
            db_path=db_path, config=FakeConfig(),
            worker_process_alive=True, worker_db_status="idle",
        )

        assert "r1" in result["orphaned_run_ids"]
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("SELECT status FROM runs WHERE run_id='r1'").fetchone()
        finally:
            conn.close()
        assert row[0] == "failed"

    def test_no_op_while_worker_is_mid_pipeline(self, tmp_path):
        """Calling periodic_check with worker_db_status='running' must not
        reconcile anything this worker might itself legitimately be running."""
        db_path = make_db(tmp_path / "hypogeum.db")
        insert_run(db_path, "r1", "running", iso(200))

        result = periodic_check(
            db_path=db_path, config=FakeConfig(),
            worker_process_alive=True, worker_db_status="running",
        )

        assert result["orphaned_run_ids"] == []
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("SELECT status FROM runs WHERE run_id='r1'").fetchone()
        finally:
            conn.close()
        assert row[0] == "running"

    def test_no_orphans_returns_empty_list(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        result = periodic_check(db_path=db_path, config=FakeConfig())
        assert result["orphaned_run_ids"] == []


# ---------------------------------------------------------------------------
# Exhausted-retry alerting
# ---------------------------------------------------------------------------

class TestExhaustedRetryAlerting:
    def test_exhausted_item_emits_a_real_ois_signal(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        insert_queue_item(db_path, "q1", "failed", retry_count=2)

        result = periodic_check(db_path=db_path, config=FakeConfig())

        assert "q1" in result["retry_exhausted_alerted"]
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT signal_type, severity, channel_id FROM signal_records "
                "WHERE signal_type = 'retry_exhausted'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "retry_exhausted"
        assert row[1] == "WARNING"
        assert row[2] == "hypogeum"

    def test_below_retry_limit_is_not_alerted(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        insert_queue_item(db_path, "q1", "failed", retry_count=1)  # below limit of 2

        result = periodic_check(db_path=db_path, config=FakeConfig())

        assert result["retry_exhausted_alerted"] == []

    def test_non_failed_status_is_not_alerted(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        insert_queue_item(db_path, "q1", "completed", retry_count=5)

        result = periodic_check(db_path=db_path, config=FakeConfig())

        assert result["retry_exhausted_alerted"] == []

    def test_repeated_calls_do_not_flood_the_signal_table(self, tmp_path):
        """Same channel's exhausted item must not create a new open signal
        on every call — matches OIS's existing dedup_any_open convention
        for persistent conditions."""
        db_path = make_db(tmp_path / "hypogeum.db")
        insert_queue_item(db_path, "q1", "failed", retry_count=2, channel_id="hypogeum")

        periodic_check(db_path=db_path, config=FakeConfig())
        periodic_check(db_path=db_path, config=FakeConfig())
        periodic_check(db_path=db_path, config=FakeConfig())

        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM signal_records WHERE signal_type = 'retry_exhausted'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_default_max_retries_used_when_config_missing_attribute(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        insert_queue_item(db_path, "q1", "failed", retry_count=2)

        class EmptyConfig:
            pass

        result = periodic_check(db_path=db_path, config=EmptyConfig())
        assert "q1" in result["retry_exhausted_alerted"]  # default max_retries=2
