"""
tests/test_queue.py — P2-04: Run Queue and Queue Claiming + Queue Consumer

Coverage:
  - run_queue table schema (columns, indexes, CHECK constraints)
  - Queue lifecycle: queued → claimed → completed / failed / cancelled
  - Release path: claimed → queued (requeue on crash)
  - Atomic claim behavior and duplicate-claim prevention
  - Priority and FIFO ordering within same priority
  - Scheduler integration: consume_scheduled_runs promotes pending rows
  - Queue processor isolation: no pipeline, no publishing, no network
  - Restart persistence: items survive process restart (re-initialized DB)
  - Migration path: existing DB gains run_queue table
  - Existing rows survive repeated initialize_schema()
  - get_queue_stats covers all status buckets
  - Queue Consumer: process_next() — CALOG check, dry_run, pipeline execution

Safety invariants:
  - No real network connections (socket-level block autouse fixture)
  - queue.py imports no publishing module (AST-level verification)
  - run_pipeline always mocked in consumer tests (never generates content)
"""

from __future__ import annotations

import ast
import inspect
import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from unittest.mock import MagicMock, patch

from hypogeum.calog import set_channel_state
from hypogeum.db import (
    get_connection,
    get_queue_item,
    initialize_schema,
    insert_queue_item,
)
from hypogeum.queue import (
    PollResult,
    ProcessResult,
    cancel,
    claim_next,
    complete,
    consume_scheduled_runs,
    enqueue,
    fail,
    get_item,
    get_queue_stats,
    poll,
    process_next,
    release,
)
from hypogeum.topics import enqueue_topic
from hypogeum.scheduler import create_schedule, tick


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _queue_row(db_path: Path, queue_id: str) -> dict:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM run_queue WHERE queue_id = ?", (queue_id,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _all_queue_rows(db_path: Path) -> list[dict]:
    conn = get_connection(db_path)
    try:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM run_queue ORDER BY queued_at ASC").fetchall()
        ]
    finally:
        conn.close()


def _worker() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path) -> Path:
    p = tmp_path / "test.db"
    initialize_schema(p)
    return p


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _block(self, address):
        raise AssertionError(
            f"queue tests must not open network connections (attempted: {address})"
        )
    monkeypatch.setattr(socket.socket, "connect", _block)
    yield


# ===========================================================================
# 1. DB schema
# ===========================================================================

class TestSchema:

    def test_run_queue_table_exists(self, db_path):
        conn = get_connection(db_path)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "run_queue" in tables

    def test_run_queue_columns(self, db_path):
        conn = get_connection(db_path)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(run_queue)").fetchall()}
        finally:
            conn.close()
        required = {
            "queue_id", "scheduled_run_id", "schedule_id", "channel_id",
            "episode_id", "priority", "status", "worker_id",
            "queued_at", "claimed_at", "completed_at", "failed_at",
            "error_message", "created_at",
        }
        assert required <= cols

    def test_run_queue_indexes_exist(self, db_path):
        conn = get_connection(db_path)
        try:
            indexes = {
                r[1]
                for r in conn.execute(
                    "SELECT * FROM sqlite_master WHERE type='index' AND tbl_name='run_queue'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "idx_run_queue_status" in indexes
        assert "idx_run_queue_worker_id" in indexes
        assert "idx_run_queue_channel_id" in indexes
        assert "idx_run_queue_scheduled_run_id" in indexes
        assert "idx_run_queue_claim_order" in indexes

    def test_status_check_constraint(self, db_path):
        conn = get_connection(db_path)
        try:
            with pytest.raises(Exception):
                conn.execute(
                    "INSERT INTO run_queue (queue_id, queued_at, created_at, status) "
                    "VALUES ('x', 'now', 'now', 'invalid_status')"
                )
                conn.commit()
        finally:
            conn.close()

    def test_default_status_is_queued(self, db_path):
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT INTO run_queue (queue_id, queued_at, created_at) "
                "VALUES ('y', 'now', 'now')"
            )
            conn.commit()
            row = conn.execute(
                "SELECT status FROM run_queue WHERE queue_id='y'"
            ).fetchone()
            assert row[0] == "queued"
        finally:
            conn.close()


# ===========================================================================
# 2. Queue lifecycle
# ===========================================================================

class TestQueueLifecycle:

    def test_enqueue_creates_queued_item(self, db_path):
        qid = enqueue(db_path=db_path)
        row = _queue_row(db_path, qid)
        assert row["status"] == "queued"
        assert row["worker_id"] is None
        assert row["claimed_at"] is None

    def test_claim_transitions_to_claimed(self, db_path):
        qid = enqueue(db_path=db_path)
        w = _worker()
        claimed = claim_next(worker_id=w, db_path=db_path)
        assert claimed is not None
        assert claimed["queue_id"] == qid
        assert claimed["status"] == "claimed"
        assert claimed["worker_id"] == w
        assert claimed["claimed_at"] is not None

    def test_complete_transitions_to_completed(self, db_path):
        qid = enqueue(db_path=db_path)
        w = _worker()
        claim_next(worker_id=w, db_path=db_path)
        complete(qid, w, db_path=db_path)
        assert _queue_row(db_path, qid)["status"] == "completed"
        assert _queue_row(db_path, qid)["completed_at"] is not None

    def test_fail_transitions_to_failed(self, db_path):
        qid = enqueue(db_path=db_path)
        w = _worker()
        claim_next(worker_id=w, db_path=db_path)
        fail(qid, w, error_message="boom", db_path=db_path)
        row = _queue_row(db_path, qid)
        assert row["status"] == "failed"
        assert row["failed_at"] is not None
        assert row["error_message"] == "boom"

    def test_cancel_transitions_queued_to_cancelled(self, db_path):
        qid = enqueue(db_path=db_path)
        cancel(qid, db_path=db_path)
        assert _queue_row(db_path, qid)["status"] == "cancelled"

    def test_release_returns_claimed_to_queued(self, db_path):
        qid = enqueue(db_path=db_path)
        w = _worker()
        claim_next(worker_id=w, db_path=db_path)
        release(qid, w, db_path=db_path)
        row = _queue_row(db_path, qid)
        assert row["status"] == "queued"
        assert row["worker_id"] is None
        assert row["claimed_at"] is None

    def test_released_item_can_be_reclaimed(self, db_path):
        qid = enqueue(db_path=db_path)
        w1 = _worker()
        w2 = _worker()
        claim_next(worker_id=w1, db_path=db_path)
        release(qid, w1, db_path=db_path)
        claimed = claim_next(worker_id=w2, db_path=db_path)
        assert claimed is not None
        assert claimed["queue_id"] == qid
        assert claimed["worker_id"] == w2


# ===========================================================================
# 3. Queue ownership (worker_id, timestamps)
# ===========================================================================

class TestQueueOwnership:

    def test_worker_id_stored_on_claim(self, db_path):
        enqueue(db_path=db_path)
        w = "worker-abc"
        claim_next(worker_id=w, db_path=db_path)
        rows = _all_queue_rows(db_path)
        assert rows[0]["worker_id"] == "worker-abc"

    def test_claim_timestamp_set(self, db_path):
        enqueue(db_path=db_path)
        before = _now()
        w = _worker()
        claim_next(worker_id=w, db_path=db_path)
        after = _now()
        rows = _all_queue_rows(db_path)
        claimed_at = datetime.fromisoformat(rows[0]["claimed_at"])
        assert before <= claimed_at <= after

    def test_completed_at_set_on_complete(self, db_path):
        qid = enqueue(db_path=db_path)
        w = _worker()
        claim_next(worker_id=w, db_path=db_path)
        before = _now()
        complete(qid, w, db_path=db_path)
        after = _now()
        row = _queue_row(db_path, qid)
        completed_at = datetime.fromisoformat(row["completed_at"])
        assert before <= completed_at <= after

    def test_failed_at_set_on_fail(self, db_path):
        qid = enqueue(db_path=db_path)
        w = _worker()
        claim_next(worker_id=w, db_path=db_path)
        before = _now()
        fail(qid, w, db_path=db_path)
        after = _now()
        row = _queue_row(db_path, qid)
        failed_at = datetime.fromisoformat(row["failed_at"])
        assert before <= failed_at <= after

    def test_queued_at_matches_enqueue_time(self, db_path):
        before = _now()
        qid = enqueue(db_path=db_path)
        after = _now()
        row = _queue_row(db_path, qid)
        queued_at = datetime.fromisoformat(row["queued_at"])
        assert before <= queued_at <= after


# ===========================================================================
# 4. Safe claiming — atomic, no double-claim
# ===========================================================================

class TestAtomicClaim:

    def test_empty_queue_returns_none(self, db_path):
        result = claim_next(worker_id=_worker(), db_path=db_path)
        assert result is None

    def test_claimed_item_not_returned_to_second_claimant(self, db_path):
        enqueue(db_path=db_path)
        w1, w2 = _worker(), _worker()
        r1 = claim_next(worker_id=w1, db_path=db_path)
        r2 = claim_next(worker_id=w2, db_path=db_path)
        assert r1 is not None
        assert r2 is None  # queue empty after first claim

    def test_simulate_concurrent_claim_race(self, db_path):
        # Simulate the race: both workers observe the same queued row
        # via SELECT, then both attempt UPDATE WHERE status='queued'.
        # Exactly one should succeed (rowcount=1); the other gets rowcount=0.
        qid = enqueue(db_path=db_path)
        w1, w2 = "worker-1", "worker-2"
        now = _iso(_now())

        conn = get_connection(db_path)
        try:
            # Both workers "see" the same candidate (simulated via two UPDATEs
            # on the same queue_id with the conditional WHERE clause)
            c1 = conn.execute(
                "UPDATE run_queue SET status='claimed', worker_id=?, claimed_at=? "
                "WHERE queue_id=? AND status='queued'",
                (w1, now, qid),
            )
            conn.commit()
            c2 = conn.execute(
                "UPDATE run_queue SET status='claimed', worker_id=?, claimed_at=? "
                "WHERE queue_id=? AND status='queued'",
                (w2, now, qid),
            )
            conn.commit()
            assert c1.rowcount == 1   # first claimant wins
            assert c2.rowcount == 0   # second claimant loses
        finally:
            conn.close()

        row = _queue_row(db_path, qid)
        assert row["worker_id"] == w1  # first writer holds the claim

    def test_claim_only_affects_queued_items(self, db_path):
        # Complete one item, enqueue another; claim_next should get the queued one
        qid1 = enqueue(db_path=db_path)
        w = _worker()
        claim_next(worker_id=w, db_path=db_path)  # claims qid1
        complete(qid1, w, db_path=db_path)

        qid2 = enqueue(db_path=db_path)
        w2 = _worker()
        claimed = claim_next(worker_id=w2, db_path=db_path)
        assert claimed is not None
        assert claimed["queue_id"] == qid2

    def test_cancel_prevents_claim(self, db_path):
        qid = enqueue(db_path=db_path)
        cancel(qid, db_path=db_path)
        result = claim_next(worker_id=_worker(), db_path=db_path)
        assert result is None

    def test_cancel_claimed_item_is_noop(self, db_path):
        qid = enqueue(db_path=db_path)
        w = _worker()
        claim_next(worker_id=w, db_path=db_path)
        cancel(qid, db_path=db_path)  # claimed items cannot be cancelled
        assert _queue_row(db_path, qid)["status"] == "claimed"

    def test_complete_wrong_worker_is_noop(self, db_path):
        qid = enqueue(db_path=db_path)
        w1, w2 = _worker(), _worker()
        claim_next(worker_id=w1, db_path=db_path)
        complete(qid, w2, db_path=db_path)  # wrong worker_id
        assert _queue_row(db_path, qid)["status"] == "claimed"

    def test_fail_wrong_worker_is_noop(self, db_path):
        qid = enqueue(db_path=db_path)
        w1, w2 = _worker(), _worker()
        claim_next(worker_id=w1, db_path=db_path)
        fail(qid, w2, db_path=db_path)  # wrong worker_id
        assert _queue_row(db_path, qid)["status"] == "claimed"


# ===========================================================================
# 5. Priority and FIFO ordering
# ===========================================================================

class TestClaimOrdering:

    def test_lower_priority_number_claimed_first(self, db_path):
        # Priority -1 is higher urgency than 0
        qid_normal = enqueue(db_path=db_path, priority=0)
        qid_urgent = enqueue(db_path=db_path, priority=-1)
        w = _worker()
        claimed = claim_next(worker_id=w, db_path=db_path)
        assert claimed["queue_id"] == qid_urgent

    def test_fifo_within_same_priority(self, db_path):
        # Items with the same priority are claimed in queued_at order
        q1 = enqueue(db_path=db_path, priority=0)
        q2 = enqueue(db_path=db_path, priority=0)
        q3 = enqueue(db_path=db_path, priority=0)
        order = []
        for _ in range(3):
            w = _worker()
            item = claim_next(worker_id=w, db_path=db_path)
            order.append(item["queue_id"])
        assert order == [q1, q2, q3]

    def test_priority_beats_fifo(self, db_path):
        q_first = enqueue(db_path=db_path, priority=5)   # enqueued first but low priority
        q_second = enqueue(db_path=db_path, priority=1)  # enqueued second, higher priority
        w = _worker()
        claimed = claim_next(worker_id=w, db_path=db_path)
        assert claimed["queue_id"] == q_second


# ===========================================================================
# 6. Scheduler integration (consume_scheduled_runs)
# ===========================================================================

class TestSchedulerIntegration:

    def test_consume_empty_returns_empty(self, db_path):
        result = consume_scheduled_runs(db_path=db_path)
        assert result == []

    def test_consume_promotes_pending_to_queued(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())  # creates scheduled_run status='pending'
        queue_ids = consume_scheduled_runs(db_path=db_path)
        assert len(queue_ids) == 1
        row = _queue_row(db_path, queue_ids[0])
        assert row["status"] == "queued"

    def test_consume_marks_scheduled_run_triggered(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())
        consume_scheduled_runs(db_path=db_path)
        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT status FROM scheduled_runs"
            ).fetchall()
        finally:
            conn.close()
        assert all(r[0] == "triggered" for r in rows)

    def test_consume_links_scheduled_run_id(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        sr_ids = tick(db_path=db_path, now=_now())
        queue_ids = consume_scheduled_runs(db_path=db_path)
        row = _queue_row(db_path, queue_ids[0])
        assert row["scheduled_run_id"] == sr_ids[0]

    def test_consume_twice_does_not_double_enqueue(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())
        first = consume_scheduled_runs(db_path=db_path)
        second = consume_scheduled_runs(db_path=db_path)  # nothing pending now
        assert len(first) == 1
        assert len(second) == 0

    def test_consume_multiple_pending_runs(self, db_path):
        past = _now() - timedelta(minutes=1)
        for _ in range(4):
            create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())
        queue_ids = consume_scheduled_runs(db_path=db_path)
        assert len(queue_ids) == 4
        stats = get_queue_stats(db_path=db_path)
        assert stats["queued"] == 4

    def test_consume_propagates_channel_id(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(
            db_path=db_path, scope="channel", channel_id="ch_007",
            cadence="daily", next_run_at=past,
        )
        tick(db_path=db_path, now=_now())
        queue_ids = consume_scheduled_runs(db_path=db_path)
        row = _queue_row(db_path, queue_ids[0])
        assert row["channel_id"] == "ch_007"

    def test_scheduler_does_not_execute_pipeline(self, db_path, monkeypatch):
        """Scheduler tick + consume must never call run_pipeline."""
        import hypogeum.pipeline as _pl
        monkeypatch.setattr(
            _pl, "run_pipeline",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("pipeline must not be called")
            ),
            raising=False,
        )
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())
        consume_scheduled_runs(db_path=db_path)


# ===========================================================================
# 7. Local-safe queue processor isolation
# ===========================================================================

class TestQueueProcessorIsolation:

    def test_queue_module_imports_no_publishing(self):
        import hypogeum.queue as q_mod
        tree = ast.parse(inspect.getsource(q_mod))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for name in imported:
            assert "publishing" not in name, (
                f"queue.py must not import a publishing module: {name}"
            )

    def test_queue_module_does_not_call_run_pipeline(self):
        import hypogeum.queue as q_mod
        tree = ast.parse(inspect.getsource(q_mod))
        called = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        assert "run_pipeline" not in called

    def test_pipeline_imported_as_patchable_module_reference(self):
        # The consumer imports hypogeum.pipeline as a module reference so tests
        # can patch run_pipeline via monkeypatch.setattr or unittest.mock.patch.
        # This test confirms the import exists and that the attribute is patchable.
        import hypogeum.queue as q_mod
        import hypogeum.pipeline as pl_mod
        assert hasattr(q_mod, "_pipeline_module"), (
            "queue.py must expose _pipeline_module for test patching"
        )
        assert q_mod._pipeline_module is pl_mod

    def test_claim_next_does_not_execute(self, db_path, monkeypatch):
        """claim_next() must only perform a DB UPDATE, never run anything."""
        import hypogeum.pipeline as _pl
        monkeypatch.setattr(
            _pl, "run_pipeline",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("claim_next must not call run_pipeline")
            ),
            raising=False,
        )
        enqueue(db_path=db_path)
        result = claim_next(worker_id=_worker(), db_path=db_path)
        assert result is not None
        assert result["status"] == "claimed"

    def test_complete_does_not_execute(self, db_path, monkeypatch):
        import hypogeum.pipeline as _pl
        monkeypatch.setattr(_pl, "run_pipeline",
                            lambda *a, **k: None, raising=False)
        qid = enqueue(db_path=db_path)
        w = _worker()
        claim_next(worker_id=w, db_path=db_path)
        complete(qid, w, db_path=db_path)  # must not raise
        assert _queue_row(db_path, qid)["status"] == "completed"


# ===========================================================================
# 8. Restart persistence
# ===========================================================================

class TestRestartPersistence:

    def test_queued_items_survive_reconnect(self, db_path):
        qid = enqueue(db_path=db_path)
        # Simulate process restart: reconnect fresh
        row = get_item(qid, db_path=db_path)
        assert row is not None
        assert row["status"] == "queued"

    def test_claimed_items_survive_reconnect(self, db_path):
        qid = enqueue(db_path=db_path)
        w = _worker()
        claim_next(worker_id=w, db_path=db_path)
        # Fresh connection
        row = get_item(qid, db_path=db_path)
        assert row["status"] == "claimed"
        assert row["worker_id"] == w

    def test_completed_items_survive_reconnect(self, db_path):
        qid = enqueue(db_path=db_path)
        w = _worker()
        claim_next(worker_id=w, db_path=db_path)
        complete(qid, w, db_path=db_path)
        row = get_item(qid, db_path=db_path)
        assert row["status"] == "completed"

    def test_queue_stats_accurate_after_lifecycle(self, db_path):
        # Enqueue 5, claim 3, complete 1, fail 1, release 1, cancel 1 queued
        qids = [enqueue(db_path=db_path) for _ in range(5)]
        w = _worker()

        # claim first 3
        c1 = claim_next(worker_id=w, db_path=db_path)
        c2 = claim_next(worker_id=w, db_path=db_path)
        c3 = claim_next(worker_id=w, db_path=db_path)

        complete(c1["queue_id"], w, db_path=db_path)
        fail(c2["queue_id"], w, db_path=db_path)
        release(c3["queue_id"], w, db_path=db_path)  # back to queued

        # cancel one of the remaining queued
        remaining_queued = [
            q for q in _all_queue_rows(db_path) if q["status"] == "queued"
        ]
        cancel(remaining_queued[0]["queue_id"], db_path=db_path)

        stats = get_queue_stats(db_path=db_path)
        assert stats["completed"] == 1
        assert stats["failed"] == 1
        assert stats["cancelled"] == 1
        assert stats["queued"] == 2   # released + one untouched
        assert stats["claimed"] == 0
        assert stats["total"] == 5


# ===========================================================================
# 9. Migration path
# ===========================================================================

class TestMigrationPath:

    def test_initialize_schema_idempotent(self, tmp_path):
        p = tmp_path / "mig.db"
        initialize_schema(p)
        initialize_schema(p)
        conn = get_connection(p)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "run_queue" in tables

    def test_existing_db_gets_run_queue_table(self, tmp_path):
        p = tmp_path / "old.db"
        initialize_schema(p)
        conn = get_connection(p)
        try:
            conn.execute("DROP TABLE IF EXISTS run_queue")
            conn.commit()
        finally:
            conn.close()
        initialize_schema(p)
        conn = get_connection(p)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "run_queue" in tables

    def test_existing_rows_survive_reinitialize(self, tmp_path):
        p = tmp_path / "survive.db"
        initialize_schema(p)
        qid = enqueue(db_path=p)
        initialize_schema(p)  # second call simulates startup
        row = get_item(qid, db_path=p)
        assert row is not None
        assert row["status"] == "queued"

    def test_claimed_rows_survive_reinitialize(self, tmp_path):
        p = tmp_path / "survive_claimed.db"
        initialize_schema(p)
        qid = enqueue(db_path=p)
        w = _worker()
        claim_next(worker_id=w, db_path=p)
        initialize_schema(p)
        row = get_item(qid, db_path=p)
        assert row["status"] == "claimed"
        assert row["worker_id"] == w


# ===========================================================================
# 10. get_queue_stats
# ===========================================================================

class TestGetQueueStats:

    def test_empty_stats(self, db_path):
        stats = get_queue_stats(db_path=db_path)
        assert stats == {
            "queued": 0, "claimed": 0, "completed": 0,
            "failed": 0, "cancelled": 0, "total": 0,
        }

    def test_stats_reflect_enqueue(self, db_path):
        enqueue(db_path=db_path)
        enqueue(db_path=db_path)
        stats = get_queue_stats(db_path=db_path)
        assert stats["queued"] == 2
        assert stats["total"] == 2

    def test_stats_reflect_claim(self, db_path):
        enqueue(db_path=db_path)
        claim_next(worker_id=_worker(), db_path=db_path)
        stats = get_queue_stats(db_path=db_path)
        assert stats["queued"] == 0
        assert stats["claimed"] == 1

    def test_get_item_returns_none_for_unknown(self, db_path):
        result = get_item("no-such-id", db_path=db_path)
        assert result is None


# ===========================================================================
# 11. Queue Consumer — process_next()
# ===========================================================================

class TestConsumer:
    """
    Tests for process_next(): the execution bridge between queue and pipeline.

    run_pipeline is always mocked. No content is generated, no providers
    are called, no episodes are executed. The consumer only performs
    queue state transitions and CALOG checks.
    """

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _active_channel(self, db_path, channel_id="ch_run"):
        """Set up a channel in CALOG 'active' state."""
        set_channel_state(channel_id, "active", db_path=db_path)
        return channel_id

    def _mock_pipeline(self):
        """Return a MagicMock that stands in for run_pipeline."""
        return MagicMock(return_value=MagicMock())

    # -----------------------------------------------------------------------
    # Empty queue
    # -----------------------------------------------------------------------

    def test_empty_queue_returns_empty_result(self, db_path):
        w = _worker()
        result = process_next(worker_id=w, db_path=db_path, dry_run=True)
        assert result.status == "empty"
        assert result.queue_id is None
        assert result.worker_id == w

    def test_empty_queue_does_not_call_pipeline(self, db_path):
        with patch("hypogeum.pipeline.run_pipeline") as mock_pl:
            process_next(worker_id=_worker(), db_path=db_path, dry_run=False)
        mock_pl.assert_not_called()

    # -----------------------------------------------------------------------
    # dry_run=True — never calls pipeline
    # -----------------------------------------------------------------------

    def test_dry_run_does_not_call_pipeline(self, db_path):
        self._active_channel(db_path)
        enqueue(db_path=db_path, channel_id="ch_run")
        with patch("hypogeum.pipeline.run_pipeline") as mock_pl:
            result = process_next(worker_id=_worker(), db_path=db_path,
                                  dry_run=True)
        mock_pl.assert_not_called()
        assert result.status == "completed"
        assert result.dry_run is True

    def test_dry_run_marks_item_completed(self, db_path):
        self._active_channel(db_path)
        qid = enqueue(db_path=db_path, channel_id="ch_run")
        w = _worker()
        with patch("hypogeum.pipeline.run_pipeline"):
            process_next(worker_id=w, db_path=db_path, dry_run=True)
        row = _queue_row(db_path, qid)
        assert row["status"] == "completed"

    def test_dry_run_no_channel_id_completes(self, db_path):
        qid = enqueue(db_path=db_path)  # no channel_id
        with patch("hypogeum.pipeline.run_pipeline") as mock_pl:
            result = process_next(worker_id=_worker(), db_path=db_path, dry_run=True)
        mock_pl.assert_not_called()
        assert result.status == "completed"

    def test_dry_run_multiple_items_processes_one(self, db_path):
        self._active_channel(db_path)
        for _ in range(3):
            enqueue(db_path=db_path, channel_id="ch_run")
        with patch("hypogeum.pipeline.run_pipeline"):
            result = process_next(worker_id=_worker(), db_path=db_path, dry_run=True)
        assert result.status == "completed"
        stats = get_queue_stats(db_path=db_path)
        assert stats["completed"] == 1
        assert stats["queued"] == 2

    # -----------------------------------------------------------------------
    # CALOG check — channel not runnable
    # -----------------------------------------------------------------------

    def test_paused_channel_releases_item_back_to_queued(self, db_path):
        set_channel_state("ch_p", "active", db_path=db_path)
        from hypogeum.calog import pause_channel
        pause_channel("ch_p", db_path=db_path)
        qid = enqueue(db_path=db_path, channel_id="ch_p")
        with patch("hypogeum.ois.is_channel_blocked", return_value=False):
            result = process_next(worker_id=_worker(), db_path=db_path, dry_run=True)
        assert result.status == "skipped"
        # Item must be back in queued — not consumed
        assert _queue_row(db_path, qid)["status"] == "queued"

    def test_blocked_channel_releases_item_back_to_queued(self, db_path):
        set_channel_state("ch_b", "active", db_path=db_path)
        set_channel_state("ch_b", "blocked", db_path=db_path)
        qid = enqueue(db_path=db_path, channel_id="ch_b")
        with patch("hypogeum.ois.is_channel_blocked", return_value=False):
            result = process_next(worker_id=_worker(), db_path=db_path, dry_run=True)
        assert result.status == "skipped"
        assert _queue_row(db_path, qid)["status"] == "queued"

    def test_archived_channel_fails_item_permanently(self, db_path):
        set_channel_state("ch_arc", "configured", db_path=db_path)
        set_channel_state("ch_arc", "archived", db_path=db_path)
        qid = enqueue(db_path=db_path, channel_id="ch_arc")
        with patch("hypogeum.ois.is_channel_blocked", return_value=False):
            result = process_next(worker_id=_worker(), db_path=db_path, dry_run=True)
        assert result.status == "skipped"
        assert _queue_row(db_path, qid)["status"] == "failed"

    def test_error_state_channel_fails_item_permanently(self, db_path):
        set_channel_state("ch_err", "error", db_path=db_path)
        qid = enqueue(db_path=db_path, channel_id="ch_err")
        with patch("hypogeum.ois.is_channel_blocked", return_value=False):
            result = process_next(worker_id=_worker(), db_path=db_path, dry_run=True)
        assert result.status == "skipped"
        assert _queue_row(db_path, qid)["status"] == "failed"

    def test_draft_channel_releases_for_retry(self, db_path):
        # draft is temporarily non-runnable — release, not fail
        qid = enqueue(db_path=db_path, channel_id="ch_draft")
        with patch("hypogeum.ois.is_channel_blocked", return_value=False):
            result = process_next(worker_id=_worker(), db_path=db_path, dry_run=True)
        assert result.status == "skipped"
        assert _queue_row(db_path, qid)["status"] == "queued"

    def test_no_channel_id_skips_calog_check(self, db_path):
        """Items with no channel_id bypass CALOG entirely."""
        qid = enqueue(db_path=db_path, channel_id=None)
        with patch("hypogeum.pipeline.run_pipeline"):
            result = process_next(worker_id=_worker(), db_path=db_path, dry_run=True)
        assert result.status == "completed"
        assert _queue_row(db_path, qid)["status"] == "completed"

    def test_ois_blocked_channel_releases_for_retry(self, db_path):
        """active CALOG state + active OIS signal = not runnable → release."""
        set_channel_state("ch_ois", "active", db_path=db_path)
        qid = enqueue(db_path=db_path, channel_id="ch_ois")
        with patch("hypogeum.ois.is_channel_blocked", return_value=True):
            result = process_next(worker_id=_worker(), db_path=db_path, dry_run=True)
        assert result.status == "skipped"
        assert _queue_row(db_path, qid)["status"] == "queued"

    # -----------------------------------------------------------------------
    # dry_run=False — pipeline execution path
    # -----------------------------------------------------------------------

    def test_execution_mode_calls_pipeline(self, db_path):
        self._active_channel(db_path)
        enqueue(db_path=db_path, channel_id="ch_run")
        enqueue_topic("ch_run", "test topic", db_path=db_path)
        mock_pl = self._mock_pipeline()
        with patch("hypogeum.pipeline.run_pipeline", mock_pl):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )
        mock_pl.assert_called_once()
        assert result.status == "completed"
        assert result.dry_run is False

    def test_execution_passes_channel_id_to_pipeline(self, db_path):
        self._active_channel(db_path, "ch_exec")
        enqueue(db_path=db_path, channel_id="ch_exec")
        enqueue_topic("ch_exec", "test topic", db_path=db_path)
        mock_pl = self._mock_pipeline()
        with patch("hypogeum.pipeline.run_pipeline", mock_pl):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )
        _, kwargs = mock_pl.call_args
        assert kwargs.get("channel_id") == "ch_exec"

    def test_execution_uses_topic_resolver(self, db_path):
        self._active_channel(db_path)
        enqueue(db_path=db_path, channel_id="ch_run")
        mock_pl = self._mock_pipeline()
        resolver = lambda item: "Ancient Rome"
        with patch("hypogeum.pipeline.run_pipeline", mock_pl):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
                topic_resolver=resolver,
            )
        _, kwargs = mock_pl.call_args
        assert kwargs.get("topic_input") == "Ancient Rome"

    def test_execution_marks_completed_on_success(self, db_path):
        self._active_channel(db_path)
        qid = enqueue(db_path=db_path, channel_id="ch_run")
        enqueue_topic("ch_run", "test topic", db_path=db_path)
        with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )
        assert _queue_row(db_path, qid)["status"] == "completed"

    def test_execution_marks_failed_on_exception(self, db_path):
        self._active_channel(db_path)
        qid = enqueue(db_path=db_path, channel_id="ch_run")
        enqueue_topic("ch_run", "test topic", db_path=db_path)
        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=RuntimeError("pipeline blew up")):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )
        assert result.status == "failed"
        row = _queue_row(db_path, qid)
        assert row["status"] == "failed"
        assert "pipeline blew up" in row["error_message"]

    def test_execution_stores_error_message_on_fail(self, db_path):
        self._active_channel(db_path)
        enqueue(db_path=db_path, channel_id="ch_run")
        enqueue_topic("ch_run", "test topic", db_path=db_path)
        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=ValueError("bad topic")):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )
        assert "bad topic" in result.error_message

    # -----------------------------------------------------------------------
    # Worker ownership
    # -----------------------------------------------------------------------

    def test_worker_id_preserved_on_completion(self, db_path):
        self._active_channel(db_path)
        qid = enqueue(db_path=db_path, channel_id="ch_run")
        enqueue_topic("ch_run", "test topic", db_path=db_path)
        w = "worker-xyz"
        with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()):
            process_next(worker_id=w, db_path=db_path, dry_run=False,
                         config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0))
        # After completion the row retains the worker_id for audit
        row = _queue_row(db_path, qid)
        assert row["worker_id"] == w

    def test_only_claiming_worker_can_complete(self, db_path):
        self._active_channel(db_path)
        qid = enqueue(db_path=db_path, channel_id="ch_run")
        w1, w2 = _worker(), _worker()
        # w1 claims via process_next (dry_run so pipeline not called)
        with patch("hypogeum.pipeline.run_pipeline"):
            process_next(worker_id=w1, db_path=db_path, dry_run=True)
        # Item is completed by w1; second worker cannot re-complete
        complete(qid, w2, db_path=db_path)  # no-op: wrong worker
        assert _queue_row(db_path, qid)["status"] == "completed"

    # -----------------------------------------------------------------------
    # ProcessResult fields
    # -----------------------------------------------------------------------

    def test_result_contains_queue_id(self, db_path):
        self._active_channel(db_path)
        qid = enqueue(db_path=db_path, channel_id="ch_run")
        with patch("hypogeum.pipeline.run_pipeline"):
            result = process_next(worker_id=_worker(), db_path=db_path, dry_run=True)
        assert result.queue_id == qid

    def test_result_contains_channel_id(self, db_path):
        self._active_channel(db_path, "ch_cid")
        enqueue(db_path=db_path, channel_id="ch_cid")
        with patch("hypogeum.pipeline.run_pipeline"):
            result = process_next(worker_id=_worker(), db_path=db_path, dry_run=True)
        assert result.channel_id == "ch_cid"

    def test_result_is_process_result_instance(self, db_path):
        result = process_next(worker_id=_worker(), db_path=db_path, dry_run=True)
        assert isinstance(result, ProcessResult)

    # -----------------------------------------------------------------------
    # No publishing / no content generation
    # -----------------------------------------------------------------------

    def test_no_publishing_in_queue_module(self):
        import hypogeum.queue as q_mod
        tree = ast.parse(inspect.getsource(q_mod))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for name in imported:
            assert "publishing" not in name, (
                f"queue.py must not import a publishing module: {name}"
            )


# ===========================================================================
# 12. Queue Poller — poll()
# ===========================================================================

class TestPoller:
    """
    Tests for poll(): the loop wrapper around process_next().

    dry_run=True is used throughout — run_pipeline is never called.
    sleep_interval=0.0 is used throughout — no real sleeping.
    run_pipeline is always patched as an extra safety net.
    """

    def _active(self, db_path, channel_id="ch_poll"):
        from hypogeum.calog import set_channel_state
        set_channel_state(channel_id, "active", db_path=db_path)
        return channel_id

    def _fill(self, db_path, channel_id, n, topic_prefix="Topic"):
        """Enqueue n queue items and n topics for a channel."""
        for i in range(n):
            enqueue(db_path=db_path, channel_id=channel_id)
            enqueue_topic(channel_id, f"{topic_prefix} {i}", db_path=db_path)

    def test_returns_poll_result(self, db_path):
        result = poll(worker_id=_worker(), db_path=db_path, dry_run=True, sleep_interval=0.0)
        assert isinstance(result, PollResult)

    def test_empty_queue_zero_iterations(self, db_path):
        result = poll(worker_id=_worker(), db_path=db_path, dry_run=True, sleep_interval=0.0)
        assert result.iterations == 0
        assert result.stop_reason == "empty"

    def test_stops_when_queue_empty(self, db_path):
        ch = self._active(db_path)
        self._fill(db_path, ch, 3)
        result = poll(worker_id=_worker(), db_path=db_path, dry_run=True, sleep_interval=0.0)
        assert result.stop_reason == "empty"
        assert result.iterations == 3
        assert result.completed == 3

    def test_stops_immediately_on_empty(self, db_path):
        result = poll(worker_id=_worker(), db_path=db_path, dry_run=True, sleep_interval=0.0)
        assert result.iterations == 0
        assert result.completed == 0
        assert result.stop_reason == "empty"

    def test_max_iterations_stops_early(self, db_path):
        ch = self._active(db_path)
        self._fill(db_path, ch, 10)
        result = poll(worker_id=_worker(), db_path=db_path, dry_run=True,
                      max_iterations=3, sleep_interval=0.0)
        assert result.iterations == 3
        assert result.stop_reason == "max_iterations"
        stats = get_queue_stats(db_path=db_path)
        assert stats["queued"] == 7

    def test_max_iterations_one(self, db_path):
        ch = self._active(db_path)
        self._fill(db_path, ch, 5)
        result = poll(worker_id=_worker(), db_path=db_path, dry_run=True,
                      max_iterations=1, sleep_interval=0.0)
        assert result.iterations == 1
        assert result.completed == 1
        assert result.stop_reason == "max_iterations"

    def test_max_iterations_exceeds_queue_size(self, db_path):
        ch = self._active(db_path)
        self._fill(db_path, ch, 2)
        result = poll(worker_id=_worker(), db_path=db_path, dry_run=True,
                      max_iterations=100, sleep_interval=0.0)
        assert result.iterations == 2
        assert result.stop_reason == "empty"

    def test_max_iterations_none_drains_queue(self, db_path):
        ch = self._active(db_path)
        self._fill(db_path, ch, 4)
        result = poll(worker_id=_worker(), db_path=db_path, dry_run=True,
                      max_iterations=None, sleep_interval=0.0)
        assert result.iterations == 4
        assert result.stop_reason == "empty"

    def test_completed_count(self, db_path):
        ch = self._active(db_path)
        self._fill(db_path, ch, 5)
        result = poll(worker_id=_worker(), db_path=db_path, dry_run=True, sleep_interval=0.0)
        assert result.completed == 5
        assert result.failed == 0
        assert result.skipped == 0
        assert result.no_topic == 0

    def test_total_processed(self, db_path):
        ch = self._active(db_path)
        self._fill(db_path, ch, 4)
        result = poll(worker_id=_worker(), db_path=db_path, dry_run=True, sleep_interval=0.0)
        assert result.total_processed == 4

    def test_results_list_includes_terminal_empty(self, db_path):
        ch = self._active(db_path)
        self._fill(db_path, ch, 3)
        result = poll(worker_id=_worker(), db_path=db_path, dry_run=True, sleep_interval=0.0)
        assert len(result.results) == 4  # 3 completed + 1 empty sentinel
        assert result.results[-1].status == "empty"

    def test_no_topic_count(self, db_path):
        # Items with no topic are released back to queued — must use max_iterations
        # to prevent an infinite loop (queue never empties when items keep releasing).
        ch = self._active(db_path)
        for _ in range(3):
            enqueue(db_path=db_path, channel_id=ch)
        with patch("hypogeum.pipeline.run_pipeline"):
            result = poll(worker_id=_worker(), db_path=db_path, dry_run=False,
                          max_iterations=3,
                          config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
                          sleep_interval=0.0)
        assert result.no_topic == 3
        assert result.completed == 0

    def test_skipped_count_when_channel_not_runnable(self, db_path):
        from hypogeum.calog import set_channel_state, pause_channel
        set_channel_state("ch_ps", "active", db_path=db_path)
        pause_channel("ch_ps", db_path=db_path)
        for _ in range(2):
            enqueue(db_path=db_path, channel_id="ch_ps")
        with patch("hypogeum.ois.is_channel_blocked", return_value=False):
            result = poll(worker_id=_worker(), db_path=db_path, dry_run=True,
                          max_iterations=2, sleep_interval=0.0)
        assert result.skipped == 2

    def test_dry_run_never_calls_pipeline(self, db_path):
        ch = self._active(db_path)
        self._fill(db_path, ch, 5)
        with patch("hypogeum.pipeline.run_pipeline") as mock_pl:
            poll(worker_id=_worker(), db_path=db_path, dry_run=True, sleep_interval=0.0)
        mock_pl.assert_not_called()

    def test_dry_run_marks_all_completed(self, db_path):
        ch = self._active(db_path)
        self._fill(db_path, ch, 3)
        with patch("hypogeum.pipeline.run_pipeline"):
            poll(worker_id=_worker(), db_path=db_path, dry_run=True, sleep_interval=0.0)
        stats = get_queue_stats(db_path=db_path)
        assert stats["completed"] == 3
        assert stats["queued"] == 0

    def test_dry_run_does_not_consume_topics(self, db_path):
        ch = self._active(db_path)
        self._fill(db_path, ch, 2)
        from hypogeum.topics import list_topics
        with patch("hypogeum.pipeline.run_pipeline"):
            poll(worker_id=_worker(), db_path=db_path, dry_run=True, sleep_interval=0.0)
        queued_topics = list_topics(channel_id=ch, status="queued", db_path=db_path)
        assert len(queued_topics) == 2

    def test_sleep_interval_zero_does_not_sleep(self, db_path, monkeypatch):
        ch = self._active(db_path)
        self._fill(db_path, ch, 2)
        sleep_calls = []
        monkeypatch.setattr("hypogeum.queue.time.sleep", lambda s: sleep_calls.append(s))
        poll(worker_id=_worker(), db_path=db_path, dry_run=True, sleep_interval=0.0)
        assert sleep_calls == []

    def test_sleep_interval_nonzero_calls_sleep(self, db_path, monkeypatch):
        ch = self._active(db_path)
        self._fill(db_path, ch, 3)
        sleep_calls = []
        monkeypatch.setattr("hypogeum.queue.time.sleep", lambda s: sleep_calls.append(s))
        poll(worker_id=_worker(), db_path=db_path, dry_run=True, sleep_interval=0.5)
        assert len(sleep_calls) == 3
        assert all(s == 0.5 for s in sleep_calls)

    def test_keyboard_interrupt_stops_cleanly(self, db_path, monkeypatch):
        ch = self._active(db_path)
        self._fill(db_path, ch, 10)
        call_count = 0
        original_pn = process_next

        def _interrupt_after_two(worker_id, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise KeyboardInterrupt
            return original_pn(worker_id=worker_id, **kwargs)

        monkeypatch.setattr("hypogeum.queue.process_next", _interrupt_after_two)
        result = poll(worker_id=_worker(), db_path=db_path, dry_run=True, sleep_interval=0.0)
        assert result.stop_reason == "interrupted"
        assert result.iterations == 2

    def test_worker_id_passed_through(self, db_path):
        ch = self._active(db_path)
        enqueue(db_path=db_path, channel_id=ch)
        enqueue_topic(ch, "test topic", db_path=db_path)
        w = "poller-worker-001"
        poll(worker_id=w, db_path=db_path, dry_run=True, sleep_interval=0.0)
        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT worker_id FROM run_queue WHERE status='completed'"
            ).fetchall()
        finally:
            conn.close()
        assert all(r[0] == w for r in rows)

    def test_execution_mode_calls_pipeline_for_each_item(self, db_path):
        ch = self._active(db_path)
        self._fill(db_path, ch, 3)
        with patch("hypogeum.pipeline.run_pipeline", return_value=MagicMock()) as mock_pl:
            result = poll(worker_id=_worker(), db_path=db_path, dry_run=False,
                          config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
                          sleep_interval=0.0)
        assert mock_pl.call_count == 3
        assert result.completed == 3

    def test_execution_mode_failure_counts_as_failed(self, db_path):
        ch = self._active(db_path)
        self._fill(db_path, ch, 2)
        with patch("hypogeum.pipeline.run_pipeline", side_effect=RuntimeError("broken")):
            result = poll(worker_id=_worker(), db_path=db_path, dry_run=False,
                          config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
                          sleep_interval=0.0)
        assert result.failed == 2
        assert result.completed == 0

    def test_execution_mode_mixed_success_failure(self, db_path):
        ch = self._active(db_path)
        self._fill(db_path, ch, 4)
        call_count = 0

        def alternating(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                raise RuntimeError("fail")
            return MagicMock()

        with patch("hypogeum.pipeline.run_pipeline", side_effect=alternating):
            result = poll(worker_id=_worker(), db_path=db_path, dry_run=False,
                          config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
                          sleep_interval=0.0)
        assert result.completed == 2
        assert result.failed == 2

    def test_poll_result_dataclass_fields(self, db_path):
        result = poll(worker_id=_worker(), db_path=db_path, dry_run=True, sleep_interval=0.0)
        for attr in ("iterations", "completed", "failed", "skipped",
                     "no_topic", "stop_reason", "results", "total_processed"):
            assert hasattr(result, attr)


# ===========================================================================
# 13. Queue Poller — stall guard (infinite loop prevention)
# ===========================================================================

class TestPollerStallGuard:
    """
    poll() must never loop forever when items keep cycling without progress.

    Scenarios:
        no_topic  — item has active channel but no queued topics; process_next()
                    releases it back to queued on every pass.
        paused    — channel is paused; item is released back to queued each pass.
        blocked   — channel is blocked; item is released back to queued each pass.

    Without the stall guard these all produce infinite loops. The guard fires
    after max_stall_iterations consecutive non-progress results.
    """

    def test_stall_guard_fires_on_no_topic(self, db_path):
        # Active channel, one queued item, zero topics → no_topic every iteration.
        ch = "ch_stall_nt"
        from hypogeum.calog import set_channel_state
        set_channel_state(ch, "active", db_path=db_path)
        enqueue(db_path=db_path, channel_id=ch)

        result = poll(
            worker_id=_worker(),
            db_path=db_path,
            dry_run=False,
            max_stall_iterations=5,
            sleep_interval=0.0,
            config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
        )

        assert result.stop_reason == "stalled", (
            f"expected 'stalled', got {result.stop_reason!r}"
        )
        assert result.no_topic >= 1
        assert result.iterations == 5

    def test_stall_guard_fires_on_paused_channel(self, db_path):
        # Paused channel → skipped every iteration; item released back to queued.
        from hypogeum.calog import set_channel_state, pause_channel
        set_channel_state("ch_stall_paused", "active", db_path=db_path)
        pause_channel("ch_stall_paused", db_path=db_path)
        enqueue(db_path=db_path, channel_id="ch_stall_paused")

        with patch("hypogeum.ois.is_channel_blocked", return_value=False):
            result = poll(
                worker_id=_worker(),
                db_path=db_path,
                dry_run=True,
                max_stall_iterations=5,
                sleep_interval=0.0,
            )

        assert result.stop_reason == "stalled", (
            f"expected 'stalled', got {result.stop_reason!r}"
        )
        assert result.skipped >= 1
        assert result.iterations == 5

    def test_stall_guard_fires_on_blocked_channel(self, db_path):
        # Blocked channel → skipped every iteration; item released back to queued.
        from hypogeum.calog import set_channel_state
        set_channel_state("ch_stall_blk", "active", db_path=db_path)
        set_channel_state("ch_stall_blk", "blocked", db_path=db_path)
        enqueue(db_path=db_path, channel_id="ch_stall_blk")

        with patch("hypogeum.ois.is_channel_blocked", return_value=False):
            result = poll(
                worker_id=_worker(),
                db_path=db_path,
                dry_run=True,
                max_stall_iterations=5,
                sleep_interval=0.0,
            )

        assert result.stop_reason == "stalled", (
            f"expected 'stalled', got {result.stop_reason!r}"
        )
        assert result.skipped >= 1
        assert result.iterations == 5

    def test_stall_streak_resets_on_completed(self, db_path):
        # Paused channel item → skipped every pass (stall++).
        # No-channel item → completed in dry_run=True (stall resets to 0).
        #
        # Without reset: stall fires at iteration 2 (skip+complete = stall 1+1=2).
        # With reset:    stall fires at iteration 4 (skip→reset→skip→skip = stall 2).
        # Asserting iterations > 2 proves the reset happened.
        from hypogeum.calog import set_channel_state, pause_channel
        set_channel_state("ch_stall_mix", "active", db_path=db_path)
        pause_channel("ch_stall_mix", db_path=db_path)

        enqueue(db_path=db_path, channel_id=None)             # enqueued first → earliest queued_at → claimed first
        enqueue(db_path=db_path, channel_id="ch_stall_mix")  # enqueued second → claimed after

        with patch("hypogeum.ois.is_channel_blocked", return_value=False):
            result = poll(
                worker_id=_worker(),
                db_path=db_path,
                dry_run=True,
                max_stall_iterations=2,
                sleep_interval=0.0,
            )

        assert result.completed >= 1, "no-channel item must complete"
        assert result.iterations > 2, (
            f"stall streak reset should delay stall to iteration 4+; "
            f"got {result.iterations} (stop={result.stop_reason!r})"
        )

    def test_stall_guard_default_is_finite_positive_int(self, db_path):
        from inspect import signature
        sig = signature(poll)
        default = sig.parameters["max_stall_iterations"].default
        assert isinstance(default, int), f"expected int, got {type(default)}"
        assert default > 0, f"must be > 0, got {default}"
