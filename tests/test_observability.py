"""
tests/test_observability.py — Unit verification for hypogeum/observability.py

Exercises Runtime Observability (Phase 3 Production Hardening, P0.3):
signals layer (worker heartbeat, queue growth, stuck runs, repeated
failures, provider failures, disk usage, memory usage, backup health)
and that unhealthy checks emit an OIS signal via the existing
hypogeum.ois/hypogeum.notifications substrate rather than a parallel
alerting path.

All tests use tmp_path — never the production database, per
CLAUDE.md's Test Isolation rules.

Run with:  python3 -m pytest tests/test_observability.py -v
"""

from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.db import initialize_schema
from hypogeum.observability import (
    check_backup_health,
    check_disk_usage,
    check_memory_usage,
    check_provider_failures,
    check_queue_growth,
    check_repeated_failures,
    check_stuck_runs,
    check_worker_heartbeat,
    run_all_checks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_db(path: Path) -> Path:
    initialize_schema(path)
    return path


def set_worker_control_at(db_path: Path, key: str, value: str, updated_at_iso: str) -> None:
    """Insert/overwrite a worker_control row with an explicit updated_at,
    bypassing set_worker_control() so tests can simulate staleness."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO worker_control (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, updated_at_iso),
        )
        conn.commit()
    finally:
        conn.close()


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


def insert_stage_execution(
    db_path: Path, stage_exec_id: str, run_id: str, stage_name: str,
    status: str, started_at_iso: str, error_message: str = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO stage_executions "
            "(stage_exec_id, run_id, episode_id, stage_name, started_at, status, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (stage_exec_id, run_id, f"ep_{run_id}", stage_name, started_at_iso, status, error_message),
        )
        conn.commit()
    finally:
        conn.close()


def insert_queue_item(db_path: Path, queue_id: str, status: str, claimed_at_iso: str = None) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO run_queue (queue_id, status, queued_at, claimed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (queue_id, status, now, claimed_at_iso, now),
        )
        conn.commit()
    finally:
        conn.close()


def iso(minutes_ago: float = 0) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


# ---------------------------------------------------------------------------
# Worker heartbeat
# ---------------------------------------------------------------------------

class TestWorkerHeartbeat:
    def test_no_worker_control_row_is_unhealthy(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        result = check_worker_heartbeat(db_path=db_path)
        assert not result.healthy

    def test_fresh_heartbeat_is_healthy(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        set_worker_control_at(db_path, "worker_status", "idle", iso(minutes_ago=0.1))
        result = check_worker_heartbeat(db_path=db_path)
        assert result.healthy

    def test_stale_heartbeat_is_unhealthy(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        set_worker_control_at(db_path, "worker_status", "idle", iso(minutes_ago=30))
        result = check_worker_heartbeat(db_path=db_path)
        assert not result.healthy
        assert "stale" in result.detail


# ---------------------------------------------------------------------------
# Queue growth
# ---------------------------------------------------------------------------

class TestQueueGrowth:
    def test_below_threshold_is_healthy(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        for i in range(3):
            insert_queue_item(db_path, f"q{i}", "queued")
        result = check_queue_growth(db_path=db_path)
        assert result.healthy

    def test_above_threshold_is_unhealthy(self, tmp_path, monkeypatch):
        db_path = make_db(tmp_path / "hypogeum.db")
        import hypogeum.observability as obs
        for i in range(5):
            insert_queue_item(db_path, f"q{i}", "queued")

        class FakeConfig:
            QUEUE_GROWTH_WARNING_THRESHOLD = 3

        result = check_queue_growth(db_path=db_path, config=FakeConfig())
        assert not result.healthy


# ---------------------------------------------------------------------------
# Stuck runs
# ---------------------------------------------------------------------------

class TestStuckRuns:
    def test_no_stuck_runs_is_healthy(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        insert_run(db_path, "r1", "completed", iso(minutes_ago=200))
        result = check_stuck_runs(db_path=db_path)
        assert result.healthy

    def test_old_running_run_is_unhealthy(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        insert_run(db_path, "r1", "running", iso(minutes_ago=200))

        class FakeConfig:
            STUCK_RUN_THRESHOLD_MINUTES = 120

        result = check_stuck_runs(db_path=db_path, config=FakeConfig())
        assert not result.healthy
        assert "stuck run" in result.detail

    def test_recent_running_run_is_healthy(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        insert_run(db_path, "r1", "running", iso(minutes_ago=5))

        class FakeConfig:
            STUCK_RUN_THRESHOLD_MINUTES = 120

        result = check_stuck_runs(db_path=db_path, config=FakeConfig())
        assert result.healthy


# ---------------------------------------------------------------------------
# Repeated failures
# ---------------------------------------------------------------------------

class TestRepeatedFailures:
    def test_no_failures_is_healthy(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        insert_stage_execution(db_path, "s1", "r1", "audio_agent", "completed", iso(5))
        result = check_repeated_failures(db_path=db_path)
        assert result.healthy

    def test_consecutive_failures_past_threshold_is_unhealthy(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        for i in range(3):
            insert_stage_execution(db_path, f"s{i}", "r1", "audio_agent", "failed", iso(minutes_ago=3 - i))

        class FakeConfig:
            REPEATED_FAILURE_THRESHOLD = 3

        result = check_repeated_failures(db_path=db_path, config=FakeConfig())
        assert not result.healthy
        assert "audio_agent" in result.detail

    def test_one_success_breaks_the_streak(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        insert_stage_execution(db_path, "s1", "r1", "audio_agent", "failed", iso(3))
        insert_stage_execution(db_path, "s2", "r1", "audio_agent", "completed", iso(2))
        insert_stage_execution(db_path, "s3", "r1", "audio_agent", "failed", iso(1))

        class FakeConfig:
            REPEATED_FAILURE_THRESHOLD = 3

        result = check_repeated_failures(db_path=db_path, config=FakeConfig())
        assert result.healthy


# ---------------------------------------------------------------------------
# Provider failures
# ---------------------------------------------------------------------------

class TestProviderFailures:
    def test_no_failures_is_healthy(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        result = check_provider_failures(db_path=db_path)
        assert result.healthy

    def test_elevated_provider_failures_is_unhealthy(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        for i in range(3):
            insert_stage_execution(
                db_path, f"s{i}", "r1", "audio_agent", "failed", iso(3 - i),
                error_message="ElevenLabsError: 503 service unavailable",
            )

        class FakeConfig:
            PROVIDER_FAILURE_THRESHOLD = 3

        result = check_provider_failures(db_path=db_path, config=FakeConfig())
        assert not result.healthy
        assert "elevenlabs" in result.detail


# ---------------------------------------------------------------------------
# Disk usage
# ---------------------------------------------------------------------------

class TestDiskUsage:
    def test_real_filesystem_reports_a_result(self, tmp_path):
        result = check_disk_usage(path=tmp_path)
        assert isinstance(result.healthy, bool)
        assert "%" in result.detail

    def test_absurd_threshold_forces_unhealthy(self, tmp_path):
        class FakeConfig:
            DISK_FREE_WARNING_PCT = 100.1  # impossible to satisfy
        result = check_disk_usage(path=tmp_path, config=FakeConfig())
        assert not result.healthy


# ---------------------------------------------------------------------------
# Memory usage
# ---------------------------------------------------------------------------

class TestMemoryUsage:
    def test_no_pid_file_is_healthy(self, tmp_path, monkeypatch):
        import hypogeum.observability as obs
        monkeypatch.setattr(obs, "_WORKER_PID_FILE", tmp_path / "worker.pid")
        result = check_memory_usage()
        assert result.healthy
        assert "not running" in result.detail

    def test_current_process_under_threshold_is_healthy(self, tmp_path, monkeypatch):
        import hypogeum.observability as obs
        pid_file = tmp_path / "worker.pid"
        pid_file.write_text(str(os.getpid()))
        monkeypatch.setattr(obs, "_WORKER_PID_FILE", pid_file)

        class FakeConfig:
            WORKER_MEMORY_WARNING_MB = 10_000_000  # effectively unreachable

        result = check_memory_usage(config=FakeConfig())
        assert result.healthy

    def test_low_threshold_forces_unhealthy(self, tmp_path, monkeypatch):
        import hypogeum.observability as obs
        pid_file = tmp_path / "worker.pid"
        pid_file.write_text(str(os.getpid()))
        monkeypatch.setattr(obs, "_WORKER_PID_FILE", pid_file)

        class FakeConfig:
            WORKER_MEMORY_WARNING_MB = 0  # any real process exceeds this

        result = check_memory_usage(config=FakeConfig())
        assert not result.healthy


# ---------------------------------------------------------------------------
# Backup health
# ---------------------------------------------------------------------------

class TestBackupHealth:
    def test_never_run_is_unhealthy(self, tmp_path):
        result = check_backup_health(backup_dir=tmp_path / "never_run")
        assert not result.healthy

    def test_healthy_after_a_real_scheduled_backup(self, tmp_path):
        from hypogeum.recovery import run_scheduled_backup
        db_path = make_db(tmp_path / "hypogeum.db")
        backup_dir = tmp_path / "backups"
        run_scheduled_backup(db_path=db_path, backup_dir=backup_dir)

        result = check_backup_health(backup_dir=backup_dir)
        assert result.healthy


# ---------------------------------------------------------------------------
# Aggregate: run_all_checks emits signals through the existing OIS substrate
# ---------------------------------------------------------------------------

class TestRunAllChecks:
    def test_all_healthy_produces_no_signals(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        set_worker_control_at(db_path, "worker_status", "idle", iso(0.1))
        from hypogeum.recovery import run_scheduled_backup
        run_scheduled_backup(db_path=db_path, backup_dir=tmp_path / "backups")

        # Point backup_health lookups at the same dir we just populated by
        # using the default resolution (Config().BACKUP_DIR unset -> default
        # path) would hit the real project dir, so patch check_backup_health's
        # default via config instead.
        class FakeConfig:
            WORKER_HEARTBEAT_STALE_SECONDS = 300
            QUEUE_GROWTH_WARNING_THRESHOLD = 20
            STUCK_RUN_THRESHOLD_MINUTES = 120
            REPEATED_FAILURE_THRESHOLD = 3
            PROVIDER_FAILURE_THRESHOLD = 3
            DISK_FREE_WARNING_PCT = 0.0  # never trip in CI regardless of real disk state
            WORKER_MEMORY_WARNING_MB = 10_000_000
            BACKUP_DIR = str(tmp_path / "backups")

        report = run_all_checks(db_path=db_path, config=FakeConfig())
        assert report["healthy"] is True
        assert all(c["signal_id"] is None for c in report["checks"])

    def test_unhealthy_check_emits_a_signal_id(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        set_worker_control_at(db_path, "worker_status", "idle", iso(minutes_ago=30))

        report = run_all_checks(db_path=db_path)
        worker_check = next(c for c in report["checks"] if c["check"] == "worker_heartbeat")
        assert worker_check["healthy"] is False
        assert worker_check["signal_id"] is not None

        # And the signal really was persisted via the existing OIS table.
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT signal_type FROM signal_records WHERE signal_id = ?",
                (worker_check["signal_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "worker_offline"
