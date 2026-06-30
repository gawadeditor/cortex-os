"""
tests/test_orphan_runs.py — Orphaned run reconciliation tests

Covers:
  - No worker + running run → marked failed with ORPHAN_ERROR
  - Active worker running pipeline → running run left unchanged
  - Active worker idle/claiming → running run marked orphaned
  - Multiple running runs all get marked
  - Already-terminal runs (failed/completed) are never touched
  - Checkpoints on disk are untouched
  - reconcile_orphaned_runs returns the correct list of run_ids
  - Dashboard /runs no longer shows impossible RUNNING state after reconcile
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.orphan_runs import ORPHAN_ERROR, reconcile_orphaned_runs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(tmp_path: Path) -> Path:
    from hypogeum.db import initialize_schema
    db = tmp_path / "h.db"
    initialize_schema(db)
    return db


def _insert_run(db: Path, status: str = "running") -> str:
    run_id = str(uuid.uuid4())
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status, metadata_version) "
            "VALUES (?, ?, datetime('now'), ?, 'v1')",
            (run_id, f"ep_{run_id[:8]}", status),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id


def _run_status(db: Path, run_id: str) -> str:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return row["status"] if row else "missing"
    finally:
        conn.close()


def _run_error(db: Path, run_id: str) -> str | None:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT error_message FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return row["error_message"] if row else None
    finally:
        conn.close()


# ===========================================================================
# 1. No worker + running run → orphaned
# ===========================================================================

class TestNoWorkerOrphansRun:
    def test_running_run_marked_failed(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        result = reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        assert run_id in result
        assert _run_status(db, run_id) == "failed"

    def test_error_message_is_orphan_error(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        assert _run_error(db, run_id) == ORPHAN_ERROR

    def test_returns_list_of_orphaned_ids(self, tmp_path):
        db = _db(tmp_path)
        r1 = _insert_run(db, "running")
        r2 = _insert_run(db, "running")

        result = reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        assert set(result) == {r1, r2}

    def test_no_running_runs_returns_empty(self, tmp_path):
        db = _db(tmp_path)
        result = reconcile_orphaned_runs(db_path=db, worker_process_alive=False)
        assert result == []

    def test_multiple_running_runs_all_marked(self, tmp_path):
        db = _db(tmp_path)
        ids = [_insert_run(db, "running") for _ in range(5)]

        result = reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        assert set(result) == set(ids)
        for run_id in ids:
            assert _run_status(db, run_id) == "failed"


# ===========================================================================
# 2. Active worker running pipeline → running run left unchanged
# ===========================================================================

class TestActiveWorkerRunningPreservesRun:
    def test_running_run_not_touched_when_worker_running(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        result = reconcile_orphaned_runs(
            db_path=db,
            worker_process_alive=True,
            worker_db_status="running",
        )

        assert result == []
        assert _run_status(db, run_id) == "running"

    def test_cancelling_status_also_preserves(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        result = reconcile_orphaned_runs(
            db_path=db,
            worker_process_alive=True,
            worker_db_status="cancelling",
        )

        assert result == []
        assert _run_status(db, run_id) == "running"


# ===========================================================================
# 3. Active worker idle/claiming → running run still orphaned
# ===========================================================================

class TestWorkerAliveButIdleOrphansRun:
    def test_idle_worker_orphans_run(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        result = reconcile_orphaned_runs(
            db_path=db,
            worker_process_alive=True,
            worker_db_status="idle",
        )

        assert run_id in result
        assert _run_status(db, run_id) == "failed"

    def test_claiming_worker_orphans_run(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        result = reconcile_orphaned_runs(
            db_path=db,
            worker_process_alive=True,
            worker_db_status="claiming",
        )

        assert run_id in result

    def test_stopped_worker_orphans_run(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        result = reconcile_orphaned_runs(
            db_path=db,
            worker_process_alive=True,
            worker_db_status="stopped",
        )

        assert run_id in result


# ===========================================================================
# 4. Terminal runs are never touched
# ===========================================================================

class TestTerminalRunsUntouched:
    @pytest.mark.parametrize("status", ["failed", "completed", "cancelled", "blocked"])
    def test_terminal_run_not_touched(self, tmp_path, status):
        db = _db(tmp_path)
        run_id = _insert_run(db, status)

        reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        assert _run_status(db, run_id) == status

    def test_only_running_runs_are_orphaned(self, tmp_path):
        db = _db(tmp_path)
        running_id  = _insert_run(db, "running")
        failed_id   = _insert_run(db, "failed")
        complete_id = _insert_run(db, "completed")

        result = reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        assert result == [running_id]
        assert _run_status(db, failed_id)   == "failed"
        assert _run_status(db, complete_id) == "completed"


# ===========================================================================
# 5. Checkpoints on disk are untouched
# ===========================================================================

class TestCheckpointsUntouched:
    def test_checkpoint_files_not_deleted(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        # Create a fake checkpoint file
        ep_dir = tmp_path / "episodes" / f"ep_{run_id[:8]}"
        ep_dir.mkdir(parents=True)
        checkpoint = ep_dir / f"visual_package_skeleton_{run_id[:8]}.json"
        checkpoint.write_text('{"ok": true}')

        reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        assert checkpoint.exists(), "checkpoint file must not be deleted"
        assert checkpoint.read_text() == '{"ok": true}'

    def test_stage_executions_not_modified(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        # Insert a stage execution record
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT INTO stage_executions (stage_exec_id, run_id, episode_id, stage_name, "
                "started_at, status) VALUES (?, ?, ?, ?, datetime('now'), 'completed')",
                (str(uuid.uuid4()), run_id, f"ep_{run_id[:8]}", "executive_producer"),
            )
            conn.commit()
        finally:
            conn.close()

        reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        # Stage execution should still be 'completed' — reconcile doesn't touch it
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            se = conn.execute(
                "SELECT status FROM stage_executions WHERE run_id=?", (run_id,)
            ).fetchone()
        finally:
            conn.close()
        assert se["status"] == "completed"


# ===========================================================================
# 6. /runs no longer shows RUNNING state (integration-level)
# ===========================================================================

class TestRunsPageNoStaleRunning:
    def test_list_runs_no_running_after_reconcile(self, tmp_path):
        db = _db(tmp_path)
        _insert_run(db, "running")

        # Reconcile (no worker)
        reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        # Now list_runs should show no 'running' rows
        from hypogeum.dashboard_runs import list_runs
        runs = list_runs(db_path=db, status="running")
        assert runs == [], f"Expected no running runs, got: {runs}"

    def test_list_runs_running_preserved_when_worker_active(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        # Reconcile with active worker
        reconcile_orphaned_runs(
            db_path=db,
            worker_process_alive=True,
            worker_db_status="running",
        )

        from hypogeum.dashboard_runs import list_runs
        runs = list_runs(db_path=db, status="running")
        assert any(r["run_id"] == run_id for r in runs), \
            "Running run should remain when worker is active"


# ===========================================================================
# 7. reconcile_orphaned_runs is idempotent
# ===========================================================================

class TestIdempotent:
    def test_double_reconcile_is_safe(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        r1 = reconcile_orphaned_runs(db_path=db, worker_process_alive=False)
        r2 = reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        assert run_id in r1
        assert r2 == []  # second call finds nothing to orphan
        assert _run_status(db, run_id) == "failed"


# ===========================================================================
# 8. Dashboard startup reconcile via _reconcile_stale_runs (smoke test)
# ===========================================================================

class TestDashboardStartupReconcile:
    def test_startup_reconcile_marks_orphaned(self, tmp_path):
        """Smoke: reconcile called at dashboard startup clears stale running runs."""
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        # Simulate what dashboard startup does: worker_process_alive=False
        result = reconcile_orphaned_runs(
            db_path=db,
            worker_process_alive=False,
            worker_db_status="stopped",
        )

        assert run_id in result
        assert _run_status(db, run_id) == "failed"
        assert _run_error(db, run_id) == ORPHAN_ERROR


# ===========================================================================
# 9. CLI worker visibility — regression for eb4ebc17 false-positive orphan
#
# Root cause: run_worker.py never wrote worker.pid, so _get_worker_pid()
# returned None for CLI-started workers. The dashboard then called
# reconcile_orphaned_runs(worker_process_alive=False, worker_db_status='running')
# which orphaned the active run because worker_process_alive=False short-circuits
# the AND guard. Fix: run_worker.py writes its own PID file so both launch paths
# are identical from the dashboard's perspective.
# ===========================================================================

class TestCLIWorkerVisibility:
    """
    Regression: CLI-started worker writes its own PID file so the dashboard
    treats it identically to a dashboard-started worker.
    """

    def test_cli_worker_writes_pid_file_with_current_pid(self, tmp_path):
        """
        run_worker writes worker.pid containing the current PID.

        Patches Config and schema init so the worker reaches the PID write
        even without real environment variables.
        """
        import os
        from unittest.mock import MagicMock
        from run_worker import run_worker

        pid_file = tmp_path / "worker.pid"

        from hypogeum.db import initialize_schema
        db = tmp_path / "h.db"
        initialize_schema(db)

        with patch("run_worker._WORKER_PID_FILE", pid_file), \
             patch("run_worker._shutdown_requested", False), \
             patch("hypogeum.config.Config", return_value=MagicMock(
                 AUTO_DISCOVERY_ENABLED=False,
             )), \
             patch("run_worker.run_iteration", side_effect=SystemExit(0)):
            try:
                run_worker(db_path=db, dry_run=True, interval=0, once=True, channel_id=None)
            except SystemExit:
                pass

        assert pid_file.exists(), "worker.pid must be written by the time run_iteration is called"
        assert pid_file.read_text().strip() == str(os.getpid()), (
            f"worker.pid must contain the current PID ({os.getpid()})"
        )

    def test_active_cli_worker_run_not_orphaned(self, tmp_path):
        """
        Simulate the eb4ebc17 scenario: CLI worker running, dashboard calls reconcile.

        With the fix, the dashboard reads the PID file, finds the process alive
        (worker_process_alive=True), and reconcile returns early.

        We test the reconcile layer directly: worker_process_alive=True +
        worker_db_status='running' → run must NOT be orphaned.
        """
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        result = reconcile_orphaned_runs(
            db_path=db,
            worker_process_alive=True,   # PID file present, process alive
            worker_db_status="running",  # DB confirms pipeline is executing
        )

        assert result == [], "Active CLI worker's run must not be orphaned"
        assert _run_status(db, run_id) == "running", \
            "Run must remain 'running' while CLI worker holds a live PID file"

    def test_dashboard_worker_run_not_orphaned(self, tmp_path):
        """Dashboard-started worker with PID file alive → run preserved."""
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        result = reconcile_orphaned_runs(
            db_path=db,
            worker_process_alive=True,
            worker_db_status="running",
        )

        assert result == []
        assert _run_status(db, run_id) == "running"

    def test_dead_worker_no_pid_file_orphans_run(self, tmp_path):
        """No PID file (dead worker), DB not updated → run is orphaned."""
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        # worker_process_alive=False simulates _get_worker_pid() returning None
        # worker_db_status='idle' simulates DB updated before crash (or stopped worker)
        result = reconcile_orphaned_runs(
            db_path=db,
            worker_process_alive=False,
            worker_db_status="idle",
        )

        assert run_id in result
        assert _run_status(db, run_id) == "failed"
        assert _run_error(db, run_id) == ORPHAN_ERROR

    def test_dead_worker_stale_db_running_status_orphans_run(self, tmp_path):
        """
        Worker crashed without updating DB status (still 'running') and no PID file.
        This is the genuine crash scenario: worker died mid-pipeline, PID file absent,
        DB status was never reset. Run must be orphaned.
        """
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        result = reconcile_orphaned_runs(
            db_path=db,
            worker_process_alive=False,   # no PID file — process is gone
            worker_db_status="running",   # DB was not updated (crash)
        )

        assert run_id in result, (
            "Crashed worker (no PID file, stale DB status) must still trigger orphan detection"
        )
        assert _run_status(db, run_id) == "failed"
