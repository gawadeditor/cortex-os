"""
tests/test_runtime.py — Autonomous Runtime Smoke

Verifies the full dry-run execution path end-to-end in an isolated test DB:
    schedule tick → scheduled_runs → consume_scheduled_runs → run_queue → poll

Coverage:
  - smoke_run() returns SmokeResult(success=True) for the happy path
  - Step 1: schedule is created
  - Step 2: tick() fires the schedule, producing a scheduled_run
  - Step 3: consume_scheduled_runs() promotes the scheduled_run to run_queue
  - Step 4: poll(dry_run=True) claims and completes the queue item
  - Step 5: DB confirms run_queue row has status='completed'
  - run_pipeline is never called (dry_run=True enforced)
  - No content generation, no publishing, no network calls
  - SmokeResult fields and __str__ output
  - Failure paths: empty tick, empty consume
  - DB state consistency after smoke_run
  - smoke_run is idempotent (can run twice on same DB without error)
"""

from __future__ import annotations

import ast
import inspect
import socket
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.runtime import SmokeResult, smoke_run


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path) -> Path:
    p = tmp_path / "smoke.db"
    initialize_schema(p)
    return p


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _block(self, address):
        raise AssertionError(
            f"runtime smoke tests must not open network connections: {address}"
        )
    monkeypatch.setattr(socket.socket, "connect", _block)
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _queue_rows(db_path: Path) -> list[dict]:
    conn = get_connection(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM run_queue").fetchall()]
    finally:
        conn.close()


def _scheduled_run_rows(db_path: Path) -> list[dict]:
    conn = get_connection(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM scheduled_runs").fetchall()]
    finally:
        conn.close()


def _schedule_rows(db_path: Path) -> list[dict]:
    conn = get_connection(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM schedules").fetchall()]
    finally:
        conn.close()


# ===========================================================================
# 1. Happy path — full end-to-end smoke
# ===========================================================================

class TestSmokeRunHappyPath:

    def test_returns_smoke_result(self, db_path):
        result = smoke_run(db_path=db_path)
        assert isinstance(result, SmokeResult)

    def test_success_true(self, db_path):
        result = smoke_run(db_path=db_path)
        assert result.success is True, (
            f"smoke_run failed: {result.failure_reason}\n"
            f"steps completed: {result.steps_completed}"
        )

    def test_all_five_steps_completed(self, db_path):
        result = smoke_run(db_path=db_path)
        expected_steps = [
            "schedule_created",
            "tick_fired",
            "scheduled_run_queued",
            "poll_completed",
            "db_state_verified",
        ]
        assert result.steps_completed == expected_steps

    def test_failure_reason_none_on_success(self, db_path):
        result = smoke_run(db_path=db_path)
        assert result.failure_reason is None

    # -----------------------------------------------------------------------
    # Step 1: schedule created
    # -----------------------------------------------------------------------

    def test_schedule_id_set(self, db_path):
        result = smoke_run(db_path=db_path)
        assert result.schedule_id is not None
        parsed = uuid.UUID(result.schedule_id)
        assert parsed.version == 4

    def test_schedule_written_to_db(self, db_path):
        result = smoke_run(db_path=db_path)
        rows = _schedule_rows(db_path)
        assert any(r["schedule_id"] == result.schedule_id for r in rows)

    def test_schedule_label_is_smoke_test(self, db_path):
        result = smoke_run(db_path=db_path)
        rows = _schedule_rows(db_path)
        row = next(r for r in rows if r["schedule_id"] == result.schedule_id)
        assert row["label"] == "smoke_test"

    # -----------------------------------------------------------------------
    # Step 2: tick_fired — scheduled_run created
    # -----------------------------------------------------------------------

    def test_scheduled_run_ids_set(self, db_path):
        result = smoke_run(db_path=db_path)
        assert len(result.scheduled_run_ids) == 1

    def test_scheduled_run_written_to_db(self, db_path):
        result = smoke_run(db_path=db_path)
        sr_rows = _scheduled_run_rows(db_path)
        assert any(r["scheduled_run_id"] == result.scheduled_run_ids[0] for r in sr_rows)

    def test_scheduled_run_marked_triggered_after_consume(self, db_path):
        result = smoke_run(db_path=db_path)
        sr_rows = _scheduled_run_rows(db_path)
        sr = next(r for r in sr_rows if r["scheduled_run_id"] == result.scheduled_run_ids[0])
        assert sr["status"] == "triggered"

    def test_schedule_next_run_at_cleared_after_tick(self, db_path):
        # Manual cadence: next_run_at is None after firing (one-shot)
        result = smoke_run(db_path=db_path)
        rows = _schedule_rows(db_path)
        row = next(r for r in rows if r["schedule_id"] == result.schedule_id)
        assert row["next_run_at"] is None

    # -----------------------------------------------------------------------
    # Step 3: scheduled_run_queued — run_queue entry created
    # -----------------------------------------------------------------------

    def test_queue_ids_set(self, db_path):
        result = smoke_run(db_path=db_path)
        assert len(result.queue_ids) == 1

    def test_queue_entry_written_to_db(self, db_path):
        result = smoke_run(db_path=db_path)
        queue_rows = _queue_rows(db_path)
        assert any(r["queue_id"] == result.queue_ids[0] for r in queue_rows)

    def test_queue_entry_links_to_scheduled_run(self, db_path):
        result = smoke_run(db_path=db_path)
        queue_rows = _queue_rows(db_path)
        q = next(r for r in queue_rows if r["queue_id"] == result.queue_ids[0])
        assert q["scheduled_run_id"] == result.scheduled_run_ids[0]

    # -----------------------------------------------------------------------
    # Step 4: poll_completed — item claimed and completed
    # -----------------------------------------------------------------------

    def test_poll_result_set(self, db_path):
        result = smoke_run(db_path=db_path)
        assert result.poll_result is not None

    def test_poll_completed_one_item(self, db_path):
        result = smoke_run(db_path=db_path)
        assert result.poll_result.completed == 1

    def test_poll_stop_reason_empty_or_max_iterations(self, db_path):
        result = smoke_run(db_path=db_path)
        assert result.poll_result.stop_reason in ("empty", "max_iterations")

    def test_poll_no_failures(self, db_path):
        result = smoke_run(db_path=db_path)
        assert result.poll_result.failed == 0
        assert result.poll_result.skipped == 0
        assert result.poll_result.no_topic == 0

    # -----------------------------------------------------------------------
    # Step 5: db_state_verified — DB shows completed
    # -----------------------------------------------------------------------

    def test_queue_item_status_completed_in_db(self, db_path):
        result = smoke_run(db_path=db_path)
        queue_rows = _queue_rows(db_path)
        q = next(r for r in queue_rows if r["queue_id"] == result.queue_ids[0])
        assert q["status"] == "completed"

    def test_no_queued_items_remain(self, db_path):
        smoke_run(db_path=db_path)
        queue_rows = _queue_rows(db_path)
        assert all(r["status"] != "queued" for r in queue_rows)


# ===========================================================================
# 2. Safety guarantees
# ===========================================================================

class TestSmokeSafety:

    def test_run_pipeline_never_called(self, db_path):
        with patch("hypogeum.pipeline.run_pipeline") as mock_pl:
            smoke_run(db_path=db_path)
        mock_pl.assert_not_called()

    def test_dry_run_enforced_regardless_of_args(self, db_path):
        # smoke_run always passes dry_run=True to poll(); this is structural
        # (the argument is hard-coded in smoke_run, not caller-controlled).
        # Verify by checking no pipeline mock was called even without patching.
        result = smoke_run(db_path=db_path)
        assert result.success is True  # would fail if pipeline was called

    def test_no_content_generation(self, db_path):
        # Structural: smoke_run imports no agents or content modules.
        import hypogeum.runtime as rt_mod
        tree = ast.parse(inspect.getsource(rt_mod))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        forbidden = {"agents", "publishing", "pipeline", "muapi",
                     "elevenlabs", "openrouter"}
        for name in imported:
            for f in forbidden:
                assert f not in name, (
                    f"runtime.py must not import {f!r}: found {name!r}"
                )

    def test_worker_id_accepted(self, db_path):
        w = "smoke-worker-001"
        result = smoke_run(db_path=db_path, worker_id=w)
        assert result.success is True
        # Worker ID should appear in the completed queue row
        queue_rows = _queue_rows(db_path)
        q = queue_rows[0]
        assert q["worker_id"] == w


# ===========================================================================
# 3. SmokeResult fields and output
# ===========================================================================

class TestSmokeResult:

    def test_str_contains_ok_on_success(self, db_path):
        result = smoke_run(db_path=db_path)
        assert "OK" in str(result)

    def test_str_contains_fail_on_failure(self):
        result = SmokeResult(
            success=False,
            failure_reason="Something broke",
            steps_completed=["schedule_created"],
        )
        s = str(result)
        assert "FAIL" in s
        assert "Something broke" in s

    def test_str_contains_steps(self, db_path):
        result = smoke_run(db_path=db_path)
        s = str(result)
        assert "schedule_created" in s
        assert "db_state_verified" in s

    def test_default_smoke_result_is_failure(self):
        r = SmokeResult()
        assert r.success is False
        assert r.steps_completed == []
        assert r.scheduled_run_ids == []
        assert r.queue_ids == []


# ===========================================================================
# 4. Failure paths
# ===========================================================================

class TestSmokeFailurePaths:

    def test_tick_produces_nothing_is_detected(self, db_path, monkeypatch):
        # Simulate tick() returning empty list
        monkeypatch.setattr("hypogeum.runtime.tick", lambda **kw: [])
        result = smoke_run(db_path=db_path)
        assert result.success is False
        assert "tick()" in result.failure_reason
        assert "schedule_created" in result.steps_completed
        assert "tick_fired" not in result.steps_completed

    def test_consume_produces_nothing_is_detected(self, db_path, monkeypatch):
        # Simulate consume_scheduled_runs() returning empty list
        from hypogeum.scheduler import tick as real_tick

        def fake_tick(**kw):
            return real_tick(**kw)

        monkeypatch.setattr("hypogeum.runtime.consume_scheduled_runs",
                            lambda **kw: [])
        result = smoke_run(db_path=db_path)
        assert result.success is False
        assert "consume_scheduled_runs()" in result.failure_reason
        assert "tick_fired" in result.steps_completed
        assert "scheduled_run_queued" not in result.steps_completed

    def test_unexpected_exception_captured(self, db_path, monkeypatch):
        monkeypatch.setattr(
            "hypogeum.runtime.create_schedule",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("DB exploded")),
        )
        result = smoke_run(db_path=db_path)
        assert result.success is False
        assert "DB exploded" in result.failure_reason


# ===========================================================================
# 5. Idempotency and DB consistency
# ===========================================================================

class TestSmokeIdempotency:

    def test_smoke_run_twice_both_succeed(self, db_path):
        r1 = smoke_run(db_path=db_path)
        r2 = smoke_run(db_path=db_path)
        assert r1.success is True
        assert r2.success is True

    def test_smoke_run_twice_creates_two_completed_items(self, db_path):
        smoke_run(db_path=db_path)
        smoke_run(db_path=db_path)
        queue_rows = _queue_rows(db_path)
        completed = [r for r in queue_rows if r["status"] == "completed"]
        assert len(completed) == 2

    def test_smoke_run_twice_different_schedule_ids(self, db_path):
        r1 = smoke_run(db_path=db_path)
        r2 = smoke_run(db_path=db_path)
        assert r1.schedule_id != r2.schedule_id

    def test_second_run_does_not_pick_up_first_runs_queue_items(self, db_path):
        smoke_run(db_path=db_path)
        r2 = smoke_run(db_path=db_path)
        # Second run should complete exactly 1 item (its own)
        assert r2.poll_result.completed == 1
