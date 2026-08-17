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


def _insert_queue_item(
    db: Path, episode_id: str | None, status: str = "claimed", worker_id: str = "dead-worker",
) -> str:
    queue_id = str(uuid.uuid4())
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO run_queue (queue_id, channel_id, episode_id, priority, status, "
            "worker_id, queued_at, claimed_at, created_at, source) "
            "VALUES (?, 'hypogeum', ?, 0, ?, ?, datetime('now'), datetime('now'), datetime('now'), 'manual')",
            (queue_id, episode_id, status, worker_id),
        )
        conn.commit()
    finally:
        conn.close()
    return queue_id


def _insert_stage_execution(db: Path, run_id: str, episode_id: str, status: str = "running") -> str:
    stage_exec_id = str(uuid.uuid4())
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO stage_executions (stage_exec_id, run_id, episode_id, stage_name, "
            "started_at, status) VALUES (?, ?, ?, 'research_director', datetime('now'), ?)",
            (stage_exec_id, run_id, episode_id, status),
        )
        conn.commit()
    finally:
        conn.close()
    return stage_exec_id


def _row(db: Path, table: str, id_col: str, id_val: str) -> dict | None:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE {id_col}=?", (id_val,)).fetchone()
        return dict(row) if row else None
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
    @pytest.mark.parametrize("status", ["failed", "completed", "cancelled", "blocked", "paused"])
    def test_terminal_run_not_touched(self, tmp_path, status):
        """
        'paused' is not terminal in run_registry.RUN_STATES (it can resume to
        'running'), but reconcile_orphaned_runs() only ever queries
        status='running' -- so a run a cancel_check callback deliberately
        parked in 'paused' immediately before a paid boundary (2026-08-17
        incident: run ecb0f87a) must never be touched by orphan
        reconciliation, exactly like the genuinely terminal states below.
        """
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


# ===========================================================================
# 10. H1 — run_queue / stage_executions reconciliation (MAR Production Repair)
# ===========================================================================

class TestQueueAndStageReconciliation:
    def test_claimed_queue_item_associated_with_orphaned_run_is_reconciled(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")
        episode_id = f"ep_{run_id[:8]}"
        queue_id = _insert_queue_item(db, episode_id, "claimed")

        result = reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        assert run_id in result
        assert _run_status(db, run_id) == "failed"
        q = _row(db, "run_queue", "queue_id", queue_id)
        assert q["status"] == "failed"
        assert q["error_message"] == ORPHAN_ERROR

    def test_orphaned_mid_stage_execution_is_reconciled(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")
        episode_id = f"ep_{run_id[:8]}"
        queue_id = _insert_queue_item(db, episode_id, "claimed")
        stage_exec_id = _insert_stage_execution(db, run_id, episode_id, "running")

        reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        se = _row(db, "stage_executions", "stage_exec_id", stage_exec_id)
        assert se["status"] == "failed"
        assert se["error_message"] == ORPHAN_ERROR
        assert se["completed_at"] is not None
        q = _row(db, "run_queue", "queue_id", queue_id)
        assert q["status"] == "failed"

    def test_full_reconciliation_consistent_across_all_three_tables(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")
        episode_id = f"ep_{run_id[:8]}"
        queue_id = _insert_queue_item(db, episode_id, "claimed")
        stage_exec_id = _insert_stage_execution(db, run_id, episode_id, "running")

        reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        assert _run_status(db, run_id) == "failed"
        assert _row(db, "run_queue", "queue_id", queue_id)["status"] == "failed"
        assert _row(db, "stage_executions", "stage_exec_id", stage_exec_id)["status"] == "failed"

    def test_second_reconciliation_is_a_no_op(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")
        episode_id = f"ep_{run_id[:8]}"
        queue_id = _insert_queue_item(db, episode_id, "claimed")
        stage_exec_id = _insert_stage_execution(db, run_id, episode_id, "running")

        r1 = reconcile_orphaned_runs(db_path=db, worker_process_alive=False)
        q1 = _row(db, "run_queue", "queue_id", queue_id)
        se1 = _row(db, "stage_executions", "stage_exec_id", stage_exec_id)

        r2 = reconcile_orphaned_runs(db_path=db, worker_process_alive=False)
        q2 = _row(db, "run_queue", "queue_id", queue_id)
        se2 = _row(db, "stage_executions", "stage_exec_id", stage_exec_id)

        assert run_id in r1
        assert r2 == []
        assert q1 == q2, "second reconciliation must not modify an already-reconciled row"
        assert se1 == se2

    def test_no_duplicate_queue_or_topic_rows_created(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")
        episode_id = f"ep_{run_id[:8]}"
        _insert_queue_item(db, episode_id, "claimed")

        def _count(table):
            conn = sqlite3.connect(str(db))
            try:
                return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            finally:
                conn.close()

        before_queue = _count("run_queue")
        before_topics = _count("episode_topics")

        reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        assert _count("run_queue") == before_queue, "reconciliation must never insert new run_queue rows"
        assert _count("episode_topics") == before_topics, "reconciliation must never touch episode_topics"

    def test_active_worker_protects_queue_and_stage_rows_too(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")
        episode_id = f"ep_{run_id[:8]}"
        queue_id = _insert_queue_item(db, episode_id, "claimed")
        stage_exec_id = _insert_stage_execution(db, run_id, episode_id, "running")

        result = reconcile_orphaned_runs(
            db_path=db, worker_process_alive=True, worker_db_status="running",
        )

        assert result == []
        assert _run_status(db, run_id) == "running"
        assert _row(db, "run_queue", "queue_id", queue_id)["status"] == "claimed"
        assert _row(db, "stage_executions", "stage_exec_id", stage_exec_id)["status"] == "running"

    def test_unassociated_claimed_queue_item_still_reconciled_tier2(self, tmp_path):
        """
        A run_queue row can be 'claimed' with no corresponding orphaned runs
        row at all (worker crashed after claiming but before registering its
        runs row). Tier 2 must still reconcile it — must not remain
        invisibly claimed.
        """
        db = _db(tmp_path)
        queue_id = _insert_queue_item(db, None, "claimed")  # no runs row exists at all

        result = reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        assert result == [], "no orphaned runs row exists, so orphaned_ids stays empty"
        q = _row(db, "run_queue", "queue_id", queue_id)
        assert q["status"] == "failed"
        assert q["error_message"] == ORPHAN_ERROR

    def test_queued_and_completed_queue_items_untouched(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")
        episode_id = f"ep_{run_id[:8]}"
        queued_id = _insert_queue_item(db, episode_id, "queued", worker_id=None)
        completed_id = _insert_queue_item(db, episode_id, "completed")

        reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        assert _row(db, "run_queue", "queue_id", queued_id)["status"] == "queued"
        assert _row(db, "run_queue", "queue_id", completed_id)["status"] == "completed"


# ===========================================================================
# 3. Tier 3 — auto-retry (Roadmap priority 3 — recovery hardening)
# ===========================================================================

def _insert_episode_topic(
    db: Path, channel_id: str, episode_id: str, topic_text: str,
) -> str:
    """Minimal episode_topics row so Tier 3 can resolve topic_text for retry."""
    topic_id = str(uuid.uuid4())
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO episode_topics (topic_id, channel_id, topic_text, "
            "status, created_at, updated_at, episode_id) "
            "VALUES (?, ?, ?, 'consumed', datetime('now'), datetime('now'), ?)",
            (topic_id, channel_id, topic_text, episode_id),
        )
        conn.commit()
    finally:
        conn.close()
    return topic_id


def _auto_retry_queue_rows(db: Path, episode_id: str) -> list:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM run_queue WHERE episode_id = ? AND source = 'auto_retry'",
                (episode_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


class TestOrphanAutoRetryTier3:
    def test_orphaned_item_with_resolvable_topic_gets_retry_scheduled(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")
        episode_id = f"ep_{run_id[:8]}"
        _insert_queue_item(db, episode_id, "claimed")
        _insert_episode_topic(db, "hypogeum", episode_id, "orphan retry topic")

        reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        retries = _auto_retry_queue_rows(db, episode_id)
        assert len(retries) == 1
        assert retries[0]["retry_count"] == 1
        assert retries[0]["next_attempt_at"] is not None

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            topic_rows = conn.execute(
                "SELECT * FROM episode_topics WHERE source = 'auto_retry'"
            ).fetchall()
        finally:
            conn.close()
        assert len(topic_rows) == 1
        assert topic_rows[0]["topic_text"] == "orphan retry topic"

    def test_orphaned_item_without_resolvable_topic_gets_no_retry(self, tmp_path):
        """
        No episode_topics/episodes row exists for this episode_id — Tier 3
        must not guess; it does nothing rather than risk retrying with
        wrong or missing content (matches the pre-existing "no duplicate
        rows" invariant in TestQueueAndStageReconciliation).
        """
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")
        episode_id = f"ep_{run_id[:8]}"
        _insert_queue_item(db, episode_id, "claimed")

        reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        assert _auto_retry_queue_rows(db, episode_id) == []

    def test_exhausted_retry_budget_creates_no_new_retry(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")
        episode_id = f"ep_{run_id[:8]}"
        # retry_count already at the default limit (2) via a direct insert —
        # simulates this being the item's third attempt.
        queue_id = str(uuid.uuid4())
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT INTO run_queue (queue_id, channel_id, episode_id, priority, "
                "status, worker_id, queued_at, claimed_at, created_at, source, retry_count) "
                "VALUES (?, 'hypogeum', ?, 0, 'claimed', 'dead-worker', datetime('now'), "
                "datetime('now'), datetime('now'), 'auto_retry', 2)",
                (queue_id, episode_id),
            )
            conn.commit()
        finally:
            conn.close()
        _insert_episode_topic(db, "hypogeum", episode_id, "exhausted retry topic")

        reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        # No NEW retry item beyond the pre-existing one already at the limit.
        retries = _auto_retry_queue_rows(db, episode_id)
        assert [r["queue_id"] for r in retries] == [queue_id]
        # The item itself is still reconciled to failed — dead-letter, not stuck.
        q = _row(db, "run_queue", "queue_id", queue_id)
        assert q["status"] == "failed"

    def test_second_reconciliation_pass_does_not_duplicate_retry(self, tmp_path):
        db = _db(tmp_path)
        run_id = _insert_run(db, "running")
        episode_id = f"ep_{run_id[:8]}"
        _insert_queue_item(db, episode_id, "claimed")
        _insert_episode_topic(db, "hypogeum", episode_id, "idempotent retry topic")

        reconcile_orphaned_runs(db_path=db, worker_process_alive=False)
        reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        # The original orphaned run is only reconciled once (no longer
        # 'running' on the second pass), so no second retry item is created.
        assert len(_auto_retry_queue_rows(db, episode_id)) == 1


# ===========================================================================
# TestLeaseBasedOwnership — Defect C (2026-08-17) regression coverage
# ===========================================================================
#
# Root cause of the 2026-08-17 incident: this module used to treat "does the
# ONE tracked worker's own in-memory/DB status say it's actively running a
# pipeline" as the sole ownership signal. A live, legitimate, out-of-band
# run_pipeline() call -- an entrypoint other than the tracked worker daemon
# -- was invisible to that signal and got marked orphaned/failed while it
# was genuinely still executing. reconcile_orphaned_runs() now checks each
# running row's durable runtime_leases lease (hypogeum.run_registry
# acquire_run_lease()/renew_run_lease()) independently of any single
# worker's tracked status.

class TestLeaseBasedOwnership:
    def test_live_out_of_band_run_is_not_orphaned(self, tmp_path):
        """
        Item 6 / the exact 2026-08-17 scenario: a run with a live lease is
        never marked orphaned, even though worker_process_alive=False (no
        tracked worker at all) -- this is precisely what run ecb0f87a needed
        and did not have.
        """
        from hypogeum.run_registry import acquire_run_lease
        from datetime import datetime, timezone

        db = _db(tmp_path)
        run_id = _insert_run(db, "running")
        now = datetime.now(timezone.utc).isoformat()
        assert acquire_run_lease(run_id, now, db_path=db) is True

        result = reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        assert result == []
        assert _run_status(db, run_id) == "running"

    def test_stale_expired_leased_run_is_orphaned(self, tmp_path):
        """Item 11: a run whose lease has expired is genuinely orphaned and reconciled."""
        from hypogeum.db import get_connection, acquire_lease
        from datetime import datetime, timezone, timedelta

        db = _db(tmp_path)
        run_id = _insert_run(db, "running")

        # Acquire a lease that already expired in the past.
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        also_past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        conn = get_connection(db)
        acquire_lease(
            conn, resource_id=f"run:{run_id}", owner_id="dead-process",
            acquired_at=past, expires_at=also_past,
        )
        conn.close()

        result = reconcile_orphaned_runs(db_path=db, worker_process_alive=False)

        assert result == [run_id]
        assert _run_status(db, run_id) == "failed"

    def test_leased_run_survives_even_when_legacy_signal_says_no_worker(self, tmp_path):
        """
        A run with a live lease must not be orphaned regardless of the
        legacy worker_process_alive/worker_db_status values -- the lease is
        authoritative once it exists, not merely an additional OR condition.
        """
        from hypogeum.run_registry import acquire_run_lease
        from datetime import datetime, timezone

        db = _db(tmp_path)
        run_id = _insert_run(db, "running")
        now = datetime.now(timezone.utc).isoformat()
        acquire_run_lease(run_id, now, db_path=db)

        result = reconcile_orphaned_runs(
            db_path=db, worker_process_alive=False, worker_db_status="stopped",
        )

        assert result == []
        assert _run_status(db, run_id) == "running"
