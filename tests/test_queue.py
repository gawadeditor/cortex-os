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
from hypogeum.topics import enqueue_topic, get_topic
from hypogeum.scheduler import compute_next_publish_slot, create_schedule, get_scheduled_run, tick
from hypogeum.channel_config import create_channel_operations_profile, create_channel_profile


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


def _make_operations_profile(db_path, channel_id, **overrides):
    """Minimal valid ChannelOperationsProfile — mirrors the helper in
    tests/test_worker_integration.py. Default publishing_schedule is a
    concrete, always-computable slot for target_publish_at derivation."""
    create_channel_profile(channel_id=channel_id, name="Test Channel", db_path=db_path)
    kwargs = dict(
        channel_id=channel_id,
        channel_name="Test Channel",
        timezone="UTC",
        operator_enabled=True,
        production_cadence={"videos_per_week": 1},
        publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
        enabled_platforms={"youtube": True, "facebook": False, "tiktok": False, "instagram": False},
        blocked_platform_reasons={},
        discovery_enabled=True,
        production_enabled=True,
        publishing_enabled=True,
        analytics_sync_enabled=True,
        unattended_operation_enabled=False,
        max_concurrent_episodes=1,
        retry_policy={"max_retries_per_stage": 1, "backoff_seconds": 60},
        failure_halt_policy={
            "halt_on_production_failure": True,
            "halt_on_validation_failure": True,
            "halt_on_enabled_platform_publish_failure": True,
            "blocked_or_disabled_platforms_do_not_halt": True,
        },
        db_path=db_path,
    )
    kwargs.update(overrides)
    return create_channel_operations_profile(**kwargs)


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
            "source", "recovery_from_run_id", "retry_count", "next_attempt_at",
        }
        assert required <= cols

    def test_retry_count_defaults_to_zero(self, db_path):
        qid = enqueue(db_path=db_path)
        row = _queue_row(db_path, qid)
        assert row["retry_count"] == 0

    def test_next_attempt_at_defaults_to_none(self, db_path):
        qid = enqueue(db_path=db_path)
        row = _queue_row(db_path, qid)
        assert row["next_attempt_at"] is None

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

    def test_future_next_attempt_at_not_claimable(self, db_path):
        future = _iso(_now() + timedelta(minutes=10))
        qid = enqueue(db_path=db_path, next_attempt_at=future)
        claimed = claim_next(worker_id=_worker(), db_path=db_path)
        assert claimed is None

    def test_past_next_attempt_at_is_claimable(self, db_path):
        past = _iso(_now() - timedelta(minutes=1))
        qid = enqueue(db_path=db_path, next_attempt_at=past)
        claimed = claim_next(worker_id=_worker(), db_path=db_path)
        assert claimed is not None
        assert claimed["queue_id"] == qid

    def test_null_next_attempt_at_claimable_alongside_backoff_item(self, db_path):
        # A normal item must still be claimed even while a backed-off retry
        # item exists — backoff gating must not stall the whole queue.
        future = _iso(_now() + timedelta(minutes=10))
        q_retry = enqueue(db_path=db_path, priority=-5, next_attempt_at=future)
        q_normal = enqueue(db_path=db_path, priority=0)
        claimed = claim_next(worker_id=_worker(), db_path=db_path)
        assert claimed["queue_id"] == q_normal


# ===========================================================================
# 6. Scheduler integration (consume_scheduled_runs)
# ===========================================================================

class TestSchedulerIntegration:

    def test_consume_empty_returns_empty(self, db_path):
        result = consume_scheduled_runs(db_path=db_path)
        assert result == []

    def test_consume_promotes_pending_to_queued(self, db_path):
        past = _now() - timedelta(minutes=1)
        # Must use channel scope — factory scope with no channel/episode is skipped
        create_schedule(db_path=db_path, scope="channel", channel_id="ch_t1",
                        cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())  # creates scheduled_run status='pending'
        queue_ids = consume_scheduled_runs(db_path=db_path)
        assert len(queue_ids) == 1
        row = _queue_row(db_path, queue_ids[0])
        assert row["status"] == "queued"

    def test_consume_marks_scheduled_run_triggered(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, scope="channel", channel_id="ch_t2",
                        cadence="daily", next_run_at=past)
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
        create_schedule(db_path=db_path, scope="channel", channel_id="ch_t3",
                        cadence="daily", next_run_at=past)
        sr_ids = tick(db_path=db_path, now=_now())
        queue_ids = consume_scheduled_runs(db_path=db_path)
        row = _queue_row(db_path, queue_ids[0])
        assert row["scheduled_run_id"] == sr_ids[0]

    def test_consume_twice_does_not_double_enqueue(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, scope="channel", channel_id="ch_t4",
                        cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())
        first = consume_scheduled_runs(db_path=db_path)
        second = consume_scheduled_runs(db_path=db_path)  # nothing pending now
        assert len(first) == 1
        assert len(second) == 0

    def test_consume_multiple_pending_runs(self, db_path):
        past = _now() - timedelta(minutes=1)
        for i in range(4):
            create_schedule(db_path=db_path, scope="channel", channel_id=f"ch_m{i}",
                            cadence="daily", next_run_at=past)
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

    def test_consume_tags_source_schedule_for_on_time_promotion(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, scope="channel", channel_id="ch_src1",
                        cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())
        queue_ids = consume_scheduled_runs(db_path=db_path)
        row = _queue_row(db_path, queue_ids[0])
        assert row["source"] == "schedule"

    def test_consume_tags_source_schedule_missed_for_stale_pending_row(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, scope="channel", channel_id="ch_src2",
                        cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())

        # Simulate the scheduled_run having sat pending for a long time
        # (worker/queue was down) before this consume() pass reaches it.
        conn = get_connection(db_path)
        try:
            stale = _iso(_now() - timedelta(minutes=10))
            conn.execute(
                "UPDATE scheduled_runs SET scheduled_for = ? WHERE channel_id = ?",
                (stale, "ch_src2"),
            )
            conn.commit()
        finally:
            conn.close()

        queue_ids = consume_scheduled_runs(db_path=db_path)
        row = _queue_row(db_path, queue_ids[0])
        assert row["source"] == "schedule_missed"

    def test_consume_never_touches_episode_topics(self, db_path, monkeypatch):
        """Race-condition correction: consume_scheduled_runs() must never
        peek at or write episode_topics — that derivation now happens only
        in process_next(), strictly after its own topic claim."""
        channel_id = "ch_no_peek"
        _make_operations_profile(db_path, channel_id)
        topic_id = enqueue_topic(channel_id, "A topic with no explicit target", db_path=db_path)

        from hypogeum import topics as topics_module
        def _fail(*a, **k):
            raise AssertionError("consume_scheduled_runs() must not touch episode_topics")
        monkeypatch.setattr(topics_module, "set_topic_target_publish_at", _fail)
        monkeypatch.setattr(topics_module, "claim_next_topic", _fail)

        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, scope="channel", channel_id=channel_id,
                        cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())
        consume_scheduled_runs(db_path=db_path)

        assert get_topic(topic_id, db_path=db_path)["target_publish_at"] is None


# ===========================================================================
# 6b. NULL context queue item guard (regression)
#
# Root cause: factory-scope schedules with channel_id=NULL and episode_id=NULL
# were promoted to run_queue unconditionally. poll() can't resolve a topic
# without a channel_id, returns no_topic, RELEASES (not cancels) the item,
# which loops indefinitely and blocks real reruns on every worker tick.
# ===========================================================================

class TestNullContextQueueGuard:

    def _factory_schedule(self, db_path, past):
        """Create a factory-scope schedule (channel_id=None) firing in the past."""
        return create_schedule(
            db_path=db_path,
            scope="factory",
            channel_id=None,
            cadence="daily",
            next_run_at=past,
        )

    def _channel_schedule(self, db_path, past, channel_id="ch_prod"):
        """Create a channel-scope schedule (channel_id set) firing in the past."""
        return create_schedule(
            db_path=db_path,
            scope="channel",
            channel_id=channel_id,
            cadence="daily",
            next_run_at=past,
        )

    def test_factory_schedule_no_topic_not_promoted_to_queue(self, db_path):
        """
        A factory-scope scheduled_run (channel_id=None, episode_id=None) must NOT
        be promoted to run_queue. It would loop forever as no_topic.
        """
        past = _now() - timedelta(minutes=1)
        self._factory_schedule(db_path, past)
        tick(db_path=db_path, now=_now())
        queue_ids = consume_scheduled_runs(db_path=db_path)

        assert queue_ids == [], (
            "Factory-scope schedule with no channel/episode must not create a queue item"
        )

    def test_factory_schedule_scheduled_run_marked_cancelled(self, db_path):
        """Skipped NULL-context scheduled_runs are marked cancelled, not triggered."""
        past = _now() - timedelta(minutes=1)
        self._factory_schedule(db_path, past)
        sr_ids = tick(db_path=db_path, now=_now())
        consume_scheduled_runs(db_path=db_path)

        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT status FROM scheduled_runs WHERE scheduled_run_id=?",
                (sr_ids[0],),
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "cancelled", (
            f"NULL-context scheduled_run must be cancelled, got {row['status']!r}"
        )

    def test_existing_null_queue_items_are_cancelled_on_next_consume(self, db_path):
        """
        Stale NULL-context items already sitting in run_queue (queued or claimed)
        are cancelled when consume_scheduled_runs is next called.
        """
        now_str = _iso(_now())
        queue_id = str(uuid.uuid4())
        conn = get_connection(db_path)
        try:
            conn.execute(
                """
                INSERT INTO run_queue
                    (queue_id, channel_id, episode_id, status, priority, queued_at, created_at)
                VALUES (?, NULL, NULL, 'queued', 0, ?, ?)
                """,
                (queue_id, now_str, now_str),
            )
            conn.commit()
        finally:
            conn.close()

        consume_scheduled_runs(db_path=db_path)  # no new pending rows; cleanup fires

        row = _queue_row(db_path, queue_id)
        assert row["status"] == "cancelled", (
            f"Stale NULL queue item must be cancelled by consume_scheduled_runs, "
            f"got {row['status']!r}"
        )
        assert "Cancelled invalid scheduler queue item" in (row.get("error_message") or "")

    def test_null_queue_item_not_claimed_by_poll(self, db_path):
        """
        A NULL-context queue item must not be claimable — it would block
        the worker with a no_topic loop. After cancellation by consume it
        must not appear as claimable.
        """
        now_str = _iso(_now())
        null_qid = str(uuid.uuid4())
        conn = get_connection(db_path)
        try:
            conn.execute(
                """
                INSERT INTO run_queue
                    (queue_id, channel_id, episode_id, status, priority, queued_at, created_at)
                VALUES (?, NULL, NULL, 'queued', 0, ?, ?)
                """,
                (null_qid, now_str, now_str),
            )
            conn.commit()
        finally:
            conn.close()

        # consume cancels the NULL item
        consume_scheduled_runs(db_path=db_path)

        # claim_next must find nothing
        worker_id = _worker()
        claimed = claim_next(worker_id=worker_id, db_path=db_path)
        assert claimed is None, (
            "claim_next must not claim a cancelled NULL-context queue item"
        )

    def test_channel_schedule_still_promoted(self, db_path):
        """Channel-scope scheduled_runs (channel_id set) are still promoted normally."""
        past = _now() - timedelta(minutes=1)
        self._channel_schedule(db_path, past, channel_id="ch_valid")

        # Channel must exist in CALOG for consume to proceed without error
        from hypogeum.calog import set_channel_state
        set_channel_state("ch_valid", "active", db_path=db_path)

        tick(db_path=db_path, now=_now())
        queue_ids = consume_scheduled_runs(db_path=db_path)

        assert len(queue_ids) == 1, "Channel-scope schedule must still produce a queue item"
        row = _queue_row(db_path, queue_ids[0])
        assert row["channel_id"] == "ch_valid"
        assert row["status"] == "queued"

    def test_valid_rerun_remains_claimable_alongside_null_item(self, db_path):
        """
        When a NULL-context item exists in the queue alongside a real rerun,
        the real rerun is still claimable after consume_scheduled_runs cancels
        the NULL item.
        """
        from hypogeum.topics import enqueue_topic
        from hypogeum.calog import set_channel_state

        set_channel_state("ch_rerun", "active", db_path=db_path)

        # Insert a real rerun queue item
        now_str = _iso(_now() - timedelta(seconds=10))
        rerun_qid = str(uuid.uuid4())
        topic_id = enqueue_topic(
            channel_id="ch_rerun",
            topic_text="Colosseum Hypogeum",
            db_path=db_path,
            source="rerun",
        )
        conn = get_connection(db_path)
        try:
            conn.execute(
                """
                INSERT INTO run_queue
                    (queue_id, channel_id, episode_id, status, priority, source, queued_at, created_at)
                VALUES (?, 'ch_rerun', NULL, 'queued', -10, 'rerun', ?, ?)
                """,
                (rerun_qid, now_str, now_str),
            )
            # Insert NULL-context item with lower priority (higher number = lower priority)
            null_qid = str(uuid.uuid4())
            null_str = _iso(_now())
            conn.execute(
                """
                INSERT INTO run_queue
                    (queue_id, channel_id, episode_id, status, priority, queued_at, created_at)
                VALUES (?, NULL, NULL, 'queued', 0, ?, ?)
                """,
                (null_qid, null_str, null_str),
            )
            conn.commit()
        finally:
            conn.close()

        # consume cancels the NULL item
        consume_scheduled_runs(db_path=db_path)

        # Claim should return the real rerun, not the null item
        worker_id = _worker()
        claimed = claim_next(worker_id=worker_id, db_path=db_path)
        assert claimed is not None, "Real rerun must still be claimable"
        assert claimed["queue_id"] == rerun_qid, (
            f"Expected rerun queue_id {rerun_qid}, got {claimed['queue_id']!r}"
        )


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
    # Scheduler Integration — claim-time target_publish_at derivation
    # (race-condition correction: computed strictly after this function's
    # own claim_next_topic() call, for the exact topic just claimed — never
    # a pre-write consume_scheduled_runs() has to hope survives untouched)
    # -----------------------------------------------------------------------

    def test_scheduled_invocation_claims_topic_and_derives_target(self, db_path):
        channel_id = "ch_sched_claim"
        set_channel_state(channel_id, "active", db_path=db_path)
        _make_operations_profile(db_path, channel_id)
        topic_id = enqueue_topic(channel_id, "A scheduled topic", db_path=db_path)
        assert get_topic(topic_id, db_path=db_path)["target_publish_at"] is None

        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, scope="channel", channel_id=channel_id,
                        cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())
        consume_scheduled_runs(db_path=db_path)

        with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "completed"
        assert get_topic(topic_id, db_path=db_path)["target_publish_at"] is not None

    def test_scheduled_claim_target_uses_the_deliverable_schedule_policy(self, db_path):
        """2026-10-04: the target came from the legacy publishing_schedule
        (Thursday) while the release compiler booked against weekly_slots
        (Mon/Wed/Sat) at-or-after that target — Wednesday got skipped. The
        claim-time target must come from the same policy the compiler uses."""
        from datetime import datetime
        channel_id = "ch_sched_slots"
        set_channel_state(channel_id, "active", db_path=db_path)
        _make_operations_profile(db_path, channel_id, deliverable_schedule_policy={"main_episode": {
            "weekly_slots": [{"day_of_week": "monday", "time_of_day": "10:00"},
                             {"day_of_week": "wednesday", "time_of_day": "18:00"}],
            "timezone": "UTC"}})
        topic_id = enqueue_topic(channel_id, "A scheduled topic", db_path=db_path)
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, scope="channel", channel_id=channel_id,
                        cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())
        consume_scheduled_runs(db_path=db_path)
        with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()):
            process_next(worker_id=_worker(), db_path=db_path, dry_run=False,
                         config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0))
        target = datetime.fromisoformat(get_topic(topic_id, db_path=db_path)["target_publish_at"])
        assert (target.strftime("%A"), target.strftime("%H:%M")) in {("Monday", "10:00"), ("Wednesday", "18:00")}

    def test_manual_invocation_does_not_derive_target(self, db_path):
        """A plain, non-scheduler-linked run_queue item (no scheduled_run_id)
        must not get a target_publish_at derived/written at claim time."""
        channel_id = "ch_manual_claim"
        set_channel_state(channel_id, "active", db_path=db_path)
        _make_operations_profile(db_path, channel_id)
        topic_id = enqueue_topic(channel_id, "A manually enqueued topic", db_path=db_path)
        enqueue(db_path=db_path, channel_id=channel_id)  # no scheduled_run_id

        with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "completed"
        assert get_topic(topic_id, db_path=db_path)["target_publish_at"] is None

    def test_scheduled_claim_does_not_overwrite_explicit_target(self, db_path):
        channel_id = "ch_sched_explicit"
        set_channel_state(channel_id, "active", db_path=db_path)
        _make_operations_profile(db_path, channel_id)
        topic_id = enqueue_topic(channel_id, "Topic with explicit target", db_path=db_path)
        explicit = "2026-01-01T00:00:00+00:00"
        conn = get_connection(db_path)
        try:
            conn.execute(
                "UPDATE episode_topics SET target_publish_at = ? WHERE topic_id = ?",
                (explicit, topic_id),
            )
            conn.commit()
        finally:
            conn.close()

        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, scope="channel", channel_id=channel_id,
                        cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())
        consume_scheduled_runs(db_path=db_path)

        with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert get_topic(topic_id, db_path=db_path)["target_publish_at"] == explicit

    # -----------------------------------------------------------------------
    # MAR Production Repair, H2 — episode_id identity contract: the value
    # target_publish_at is written under (topic_id) must be findable by the
    # key it's read under (episode_id) — see queue.py's Step 5 comment.
    # -----------------------------------------------------------------------

    def test_scheduled_claim_persists_episode_id_onto_topic(self, db_path):
        channel_id = "ch_sched_epid"
        set_channel_state(channel_id, "active", db_path=db_path)
        _make_operations_profile(db_path, channel_id)
        topic_id = enqueue_topic(channel_id, "Organically queued topic", db_path=db_path)
        assert get_topic(topic_id, db_path=db_path)["episode_id"] is None

        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, scope="channel", channel_id=channel_id,
                        cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())
        consume_scheduled_runs(db_path=db_path)

        with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "completed"
        topic_after = get_topic(topic_id, db_path=db_path)
        # episode_id was consumed off the row by the time we read it back
        # (consume_topic doesn't touch episode_id), so it must be present.
        assert topic_after.get("episode_id"), "episode_id must be persisted onto the topic"

    def test_scheduled_claim_persists_episode_id_onto_queue_row(self, db_path):
        channel_id = "ch_sched_qepid"
        set_channel_state(channel_id, "active", db_path=db_path)
        _make_operations_profile(db_path, channel_id)
        enqueue_topic(channel_id, "Organically queued topic 2", db_path=db_path)

        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, scope="channel", channel_id=channel_id,
                        cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())
        consume_scheduled_runs(db_path=db_path)

        with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "completed"
        row = _queue_row(db_path, result.queue_id)
        assert row["episode_id"], "run_queue.episode_id must be persisted for H1 Tier-1 association"

    def test_lookup_episode_target_publish_at_resolves_after_scheduled_claim(self, db_path):
        """The actual identity-contract regression: worker_integration's
        episode_id-keyed lookup must find what queue.py's topic_id-keyed
        write produced, for a topic with no pre-assigned episode_id."""
        from hypogeum.worker_integration import lookup_episode_target_publish_at

        channel_id = "ch_sched_lookup"
        set_channel_state(channel_id, "active", db_path=db_path)
        _make_operations_profile(db_path, channel_id)
        enqueue_topic(channel_id, "Organically queued topic 3", db_path=db_path)

        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, scope="channel", channel_id=channel_id,
                        cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())
        consume_scheduled_runs(db_path=db_path)

        with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()) as mock_pl:
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )
        _, kwargs = mock_pl.call_args
        episode_id = kwargs["episode_id"]

        resolved = lookup_episode_target_publish_at(episode_id, db_path=db_path)
        assert resolved is not None, (
            "lookup_episode_target_publish_at() must resolve the target_publish_at "
            "derived at claim time for a topic with no pre-assigned episode_id"
        )

    def test_manual_topic_with_preassigned_episode_id_is_not_overwritten(self, db_path):
        """Regression guard: a topic that already carries an episode_id
        (e.g. manual submission via production_launcher.py) must not have
        it touched — set_topic_episode_id() only fills NULLs."""
        channel_id = "ch_manual_epid"
        set_channel_state(channel_id, "active", db_path=db_path)
        _make_operations_profile(db_path, channel_id)
        topic_id = enqueue_topic(
            channel_id, "Manually submitted topic", db_path=db_path, episode_id="ep_preassigned",
        )
        enqueue(db_path=db_path, channel_id=channel_id)

        with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert get_topic(topic_id, db_path=db_path)["episode_id"] == "ep_preassigned"

    def test_identity_contract_regression_guard_logs_on_mismatch(self, db_path):
        """If persistence succeeds but the downstream lookup somehow still
        can't resolve it, the regression guard must log a warning and must
        NOT block or fail the claim."""
        channel_id = "ch_sched_guard"
        set_channel_state(channel_id, "active", db_path=db_path)
        _make_operations_profile(db_path, channel_id)
        enqueue_topic(channel_id, "Guard test topic", db_path=db_path)

        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, scope="channel", channel_id=channel_id,
                        cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())
        consume_scheduled_runs(db_path=db_path)

        with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()), \
             patch(
                 "hypogeum.worker_integration.lookup_episode_target_publish_at",
                 return_value=None,
             ), \
             patch("hypogeum.logger.get_logger") as mock_get_logger:
            mock_log = MagicMock()
            mock_get_logger.return_value = mock_log
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "completed", "regression guard must never block the claim"
        warned_events = [c.args[0] for c in mock_log.warn.call_args_list]
        assert "target_publish_at_identity_contract_broken" in warned_events

    def test_two_scheduled_runs_same_channel_same_tick_both_get_correct_targets(self, db_path):
        """Regression test for the exact bug the race-condition correction
        fixes: under the old peek-then-write design, promoting two
        scheduled_runs for the same channel in one consume_scheduled_runs()
        pass meant the second row's write silently no-op'd (the topic the
        peek would select already had a non-NULL target from the first
        row's write), leaving the second run's actual topic with no target
        at all. With derivation moved to claim time, each independent claim
        derives and writes its own topic's target correctly."""
        channel_id = "ch_sched_multi"
        set_channel_state(channel_id, "active", db_path=db_path)
        _make_operations_profile(db_path, channel_id)
        topic_a = enqueue_topic(channel_id, "Topic A", db_path=db_path)
        topic_b = enqueue_topic(channel_id, "Topic B", db_path=db_path)

        past = _now() - timedelta(minutes=1)
        # Two schedules on one channel is an artificial setup for this
        # regression; give each headroom under the per-channel daily ceiling
        # (2026-09-07) so what is actually under test — claim-time target
        # derivation for two concurrent scheduled_runs — still exercises.
        create_schedule(db_path=db_path, scope="channel", channel_id=channel_id,
                        cadence="daily", next_run_at=past, max_runs_per_day=2)
        create_schedule(db_path=db_path, scope="channel", channel_id=channel_id,
                        cadence="daily", next_run_at=past, max_runs_per_day=2)
        sr_ids = tick(db_path=db_path, now=_now())
        assert len(sr_ids) == 2
        queue_ids = consume_scheduled_runs(db_path=db_path)
        assert len(queue_ids) == 2

        claimed_topic_ids = []
        for _ in range(2):
            with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()):
                result = process_next(
                    worker_id=_worker(), db_path=db_path, dry_run=False,
                    config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
                )
            assert result.status == "completed"
            claimed_topic_ids.append(result.topic_id)

        assert set(claimed_topic_ids) == {topic_a, topic_b}
        assert get_topic(topic_a, db_path=db_path)["target_publish_at"] is not None
        assert get_topic(topic_b, db_path=db_path)["target_publish_at"] is not None

    # -----------------------------------------------------------------------
    # Claim-time derivation is anchored to scheduled_for, not the live clock
    # at claim time — a second race-condition correction. Without this, a
    # claim delayed past the intended slot's own time-of-day would silently
    # roll target_publish_at forward by a full cadence period.
    # -----------------------------------------------------------------------

    def test_delayed_claim_produces_same_target_as_scheduled_for_anchor(self, db_path):
        """No matter how much real wall-clock time has elapsed since
        promotion (here: years, since scheduled_for is fixed far in the
        past relative to whenever this test actually runs), the derived
        target_publish_at must equal what compute_next_publish_slot()
        produces directly from scheduled_for — never from "now"."""
        channel_id = "ch_anchor1"
        set_channel_state(channel_id, "active", db_path=db_path)
        profile = _make_operations_profile(
            db_path, channel_id, timezone="UTC",
            publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
        )
        topic_id = enqueue_topic(channel_id, "Anchored topic", db_path=db_path)

        # scheduled_for is fixed to a Thursday morning years in the past —
        # nowhere near the real "now" this test executes at.
        fixed_scheduled_for = datetime(2020, 1, 2, 8, 0, tzinfo=timezone.utc)
        create_schedule(db_path=db_path, scope="channel", channel_id=channel_id,
                        cadence="daily", next_run_at=fixed_scheduled_for)
        tick(db_path=db_path, now=fixed_scheduled_for)
        consume_scheduled_runs(db_path=db_path)

        expected = compute_next_publish_slot(
            profile.publishing_schedule, profile.timezone, fixed_scheduled_for,
        )

        with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "completed"
        actual = get_topic(topic_id, db_path=db_path)["target_publish_at"]
        assert actual == expected.isoformat()

    def test_boundary_crossing_delay_does_not_shift_to_following_week(self, db_path):
        """The exact scenario in question: scheduled at 08:00, but the claim
        happens (in real wall-clock terms) long after the intended slot's
        own 20:00 time-of-day has passed for that calendar day. The target
        must still be that same Thursday at 20:00 — not the following
        Thursday — because the anchor is scheduled_for (08:00 that day),
        not the live clock at claim time."""
        channel_id = "ch_anchor2"
        set_channel_state(channel_id, "active", db_path=db_path)
        _make_operations_profile(
            db_path, channel_id, timezone="UTC",
            publishing_schedule={"day_of_week": "thursday", "time_of_day": "20:00"},
        )
        topic_id = enqueue_topic(channel_id, "Boundary topic", db_path=db_path)

        # 2020-01-02 is a Thursday. scheduled_for = 08:00 that day — before
        # the 20:00 slot. The real "now" this test runs at is years later,
        # i.e. long past 2020-01-02 20:00 — the exact delayed-claim scenario.
        scheduled_for = datetime(2020, 1, 2, 8, 0, tzinfo=timezone.utc)
        expected_same_day_slot = datetime(2020, 1, 2, 20, 0, tzinfo=timezone.utc)

        create_schedule(db_path=db_path, scope="channel", channel_id=channel_id,
                        cadence="daily", next_run_at=scheduled_for)
        tick(db_path=db_path, now=scheduled_for)
        consume_scheduled_runs(db_path=db_path)

        with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "completed"
        actual = get_topic(topic_id, db_path=db_path)["target_publish_at"]
        assert actual == expected_same_day_slot.isoformat()
        # Explicitly rule out the bug: must not have rolled to the next week.
        assert actual != (expected_same_day_slot + timedelta(weeks=1)).isoformat()

    def test_release_and_reclaim_still_derives_from_original_scheduled_for(self, db_path):
        """A run_queue item claimed, then released back to 'queued' before
        topic resolution ever ran (simulating a worker crash/transient
        failure recovery — the same release() primitive
        hypogeum.worker_integration's concurrency-deferral and
        hypogeum.orphan_runs both use), and later genuinely reclaimed, must
        still derive target_publish_at from the same scheduled_for."""
        channel_id = "ch_anchor3"
        set_channel_state(channel_id, "active", db_path=db_path)
        profile = _make_operations_profile(
            db_path, channel_id, timezone="UTC",
            publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
        )
        topic_id = enqueue_topic(channel_id, "Reclaimed topic", db_path=db_path)

        fixed_scheduled_for = datetime(2020, 1, 2, 8, 0, tzinfo=timezone.utc)
        create_schedule(db_path=db_path, scope="channel", channel_id=channel_id,
                        cadence="daily", next_run_at=fixed_scheduled_for)
        tick(db_path=db_path, now=fixed_scheduled_for)
        queue_ids = consume_scheduled_runs(db_path=db_path)
        queue_id = queue_ids[0]

        # Simulate a crash before topic resolution ever ran: claim, then
        # release without touching any topic.
        first_claim = claim_next(worker_id="crashed-worker", db_path=db_path)
        assert first_claim["queue_id"] == queue_id
        release(queue_id, "crashed-worker", db_path=db_path)
        assert _queue_row(db_path, queue_id)["status"] == "queued"

        expected = compute_next_publish_slot(
            profile.publishing_schedule, profile.timezone, fixed_scheduled_for,
        )

        # Genuine reclaim by a different worker.
        with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()):
            result = process_next(
                worker_id="recovered-worker", db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "completed"
        actual = get_topic(topic_id, db_path=db_path)["target_publish_at"]
        assert actual == expected.isoformat()

    def test_repeated_claims_for_same_scheduled_run_derive_identical_targets(self, db_path):
        """Two independent run_queue items both linked to the same
        scheduled_run_id (the realistic shape of a retried/re-enqueued
        attempt for one scheduled production slot) must derive the exact
        same target_publish_at for whichever topic each one claims —
        proving the derivation is a function of the scheduled_run, not of
        claim order or timing."""
        channel_id = "ch_anchor4"
        set_channel_state(channel_id, "active", db_path=db_path)
        _make_operations_profile(
            db_path, channel_id, timezone="UTC",
            publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
        )
        topic_1 = enqueue_topic(channel_id, "First attempt topic", db_path=db_path)

        fixed_scheduled_for = datetime(2020, 1, 2, 8, 0, tzinfo=timezone.utc)
        create_schedule(db_path=db_path, scope="channel", channel_id=channel_id,
                        cadence="daily", next_run_at=fixed_scheduled_for)
        sr_ids = tick(db_path=db_path, now=fixed_scheduled_for)
        sr_id = sr_ids[0]
        consume_scheduled_runs(db_path=db_path)

        with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()):
            result_1 = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )
        assert result_1.status == "completed"
        target_1 = get_topic(topic_1, db_path=db_path)["target_publish_at"]
        assert target_1 is not None

        # A second attempt tied to the SAME scheduled_run_id — the row
        # get_scheduled_run() will resolve is identical, and scheduled_for
        # was never mutated by the first claim/complete cycle.
        assert get_scheduled_run(sr_id, db_path=db_path)["scheduled_for"] == fixed_scheduled_for.isoformat()
        topic_2 = enqueue_topic(channel_id, "Second attempt topic", db_path=db_path)
        row = get_scheduled_run(sr_id, db_path=db_path)
        enqueue(db_path=db_path, channel_id=channel_id, scheduled_run_id=sr_id,
               schedule_id=row["schedule_id"], priority=0, source="schedule")

        with patch("hypogeum.pipeline.run_pipeline", self._mock_pipeline()):
            result_2 = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )
        assert result_2.status == "completed"
        target_2 = get_topic(topic_2, db_path=db_path)["target_publish_at"]

        assert target_1 == target_2

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


# ===========================================================================
# 14. Consumer hygiene — run_registry and topic cleanup on pipeline failure
# ===========================================================================

class TestConsumerHygiene:
    """
    Verifies that process_next() and run_pipeline() leave no orphaned state
    after a pipeline failure:

      Path 1 — run_registry: run_pipeline must call fail_run() for any
        exception raised after register_run(), so the runs row never
        stays 'running' after the process exits.

      Path 2 — topic backlog: process_next must release or skip the claimed
        topic after a pipeline exception, so the topic never stays 'claimed':
          PipelineRejectedError, QARejectedError, ComplianceRejectedError,
          GatedExecutionRefused → topic 'skipped' (the content/plan itself
            is the problem — permanent, not retried)
          Retry budget exhausted (any exception) → topic 'skipped' (a plain
            release would silently reset the cap on the next fresh claim)
          Any other exception (genuine infrastructure error, still within
            retry budget) → topic 'queued' or reused as the retry identity

    run_pipeline is always patched — no providers, no content generation.
    """

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _active_channel(self, db_path, channel_id="ch_hygiene"):
        from hypogeum.calog import set_channel_state
        set_channel_state(channel_id, "active", db_path=db_path)
        return channel_id

    def _setup(self, db_path, channel_id="ch_hygiene"):
        """Enqueue one topic and one queue item. Returns (topic_id, queue_id)."""
        self._active_channel(db_path, channel_id)
        topic_id = enqueue_topic(channel_id, "test topic for hygiene", db_path=db_path)
        queue_id = enqueue(db_path=db_path, channel_id=channel_id)
        return topic_id, queue_id

    # -----------------------------------------------------------------------
    # Path 1: run_registry hygiene — run_pipeline marks its own run failed
    # -----------------------------------------------------------------------

    def test_run_pipeline_calls_fail_run_on_exception(self, monkeypatch):
        """
        When run_pipeline raises after register_run(), fail_run must be called
        with the matching run_id so the runs row is never left 'running'.
        """
        from hypogeum.pipeline import run_pipeline

        fail_run_calls = []

        monkeypatch.setattr("hypogeum.pipeline.register_run",
                            lambda *a, **kw: "run-hygiene-001")
        monkeypatch.setattr("hypogeum.pipeline.set_run_context",
                            lambda **kw: None)
        monkeypatch.setattr("hypogeum.pipeline.fail_run",
                            lambda run_id, episode_id, error_message, **kw:
                                fail_run_calls.append((run_id, error_message[:40])))
        monkeypatch.setattr("hypogeum.pipeline.clear_run_context", lambda: None)
        monkeypatch.setattr("hypogeum.pipeline.is_factory_blocked",
                            lambda **kw: False)
        monkeypatch.setattr("hypogeum.pipeline.run_startup_checks",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                RuntimeError("provider unreachable")))

        with pytest.raises(RuntimeError, match="provider unreachable"):
            run_pipeline(
                topic_input="test topic",
                episode_id="ep_hyg_test",
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
                channel_id=None,
            )

        assert len(fail_run_calls) == 1, (
            f"fail_run must be called exactly once; got {len(fail_run_calls)}"
        )
        assert fail_run_calls[0][0] == "run-hygiene-001"
        assert "provider unreachable" in fail_run_calls[0][1]

    def test_run_pipeline_fail_run_called_for_startup_error(self, monkeypatch):
        """StartupError after register_run → fail_run is called."""
        from hypogeum.pipeline import run_pipeline
        from hypogeum.startup import StartupError, StartupReport

        fail_run_calls = []
        stub_report = StartupReport(
            overall_status="unavailable",
            openrouter_reachable=False,
            models_verified=False,
            fallback_active=True,
            fallback_configured=False,
            checked_at="2026-01-01T00:00:00Z",
        )

        monkeypatch.setattr("hypogeum.pipeline.register_run",
                            lambda *a, **kw: "run-startup-001")
        monkeypatch.setattr("hypogeum.pipeline.set_run_context", lambda **kw: None)
        monkeypatch.setattr("hypogeum.pipeline.fail_run",
                            lambda run_id, *a, **kw: fail_run_calls.append(run_id))
        monkeypatch.setattr("hypogeum.pipeline.clear_run_context", lambda: None)
        monkeypatch.setattr("hypogeum.pipeline.is_factory_blocked",
                            lambda **kw: False)
        monkeypatch.setattr("hypogeum.pipeline.run_startup_checks",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                StartupError("API unavailable", stub_report)))

        with pytest.raises(StartupError):
            run_pipeline(
                topic_input="test",
                episode_id="ep_startup_hyg",
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
                channel_id=None,
            )

        assert "run-startup-001" in fail_run_calls

    def test_run_pipeline_no_fail_run_when_registration_not_reached(self, monkeypatch):
        """
        If the exception fires BEFORE register_run (e.g. ChannelNotFoundError),
        fail_run must NOT be called — there is no run_id to fail.
        """
        from hypogeum.pipeline import run_pipeline, ChannelNotFoundError

        fail_run_calls = []
        monkeypatch.setattr("hypogeum.pipeline.fail_run",
                            lambda *a, **kw: fail_run_calls.append(True))

        # channel_id="nonexistent" will trigger ChannelNotFoundError before register_run
        # Mock load_channel_config to return None
        import hypogeum.channel_config as cc_mod
        monkeypatch.setattr(cc_mod, "load_channel_config", lambda cid, **kw: None)

        with pytest.raises(ChannelNotFoundError):
            run_pipeline(
                topic_input="test",
                episode_id="ep_norun",
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
                channel_id="nonexistent",
            )

        assert fail_run_calls == [], (
            "fail_run must not be called when no run was registered"
        )

    # -----------------------------------------------------------------------
    # Path 2: topic hygiene — process_next releases or skips the topic
    # -----------------------------------------------------------------------

    def test_topic_released_to_queued_on_generic_pipeline_failure(self, db_path):
        """
        A generic pipeline failure (RuntimeError) must release the claimed topic
        back to 'queued' so it is retried on the next poll pass.
        """
        topic_id, _ = self._setup(db_path)

        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=RuntimeError("provider timeout")):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "failed"
        topic = get_topic(topic_id, db_path=db_path)
        assert topic is not None
        assert topic["status"] == "queued", (
            f"expected 'queued' after infrastructure failure; got {topic['status']!r}"
        )
        assert topic["claimed_at"] is None

    def test_topic_skipped_on_pipeline_rejected_error(self, db_path):
        """
        PipelineRejectedError (EP scored topic < 6.0) must mark the topic 'skipped'
        permanently — it is not re-queued for retry.
        """
        import hypogeum.pipeline as pl_mod

        topic_id, _ = self._setup(db_path, channel_id="ch_rejected")

        rejection = pl_mod.PipelineRejectedError(
            episode_id="ep_rejected",
            scorecard={"overall_score": 2.5, "decision": "REJECT"},
        )
        with patch("hypogeum.pipeline.run_pipeline", side_effect=rejection):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "failed"
        topic = get_topic(topic_id, db_path=db_path)
        assert topic is not None
        assert topic["status"] == "skipped", (
            f"expected 'skipped' after EP rejection; got {topic['status']!r}"
        )

    def test_topic_released_on_finance_blocked_error(self, db_path):
        """
        FinanceBudgetBlockedError is an infrastructure constraint, not a topic
        fault — the topic must be released back to 'queued' for retry.
        """
        import hypogeum.pipeline as pl_mod

        topic_id, _ = self._setup(db_path, channel_id="ch_finance")

        blocked = pl_mod.FinanceBudgetBlockedError(
            episode_id="ep_finance",
            record={"estimated_spend_usd": 30.0, "budget_ceiling_usd": 15.0},
        )
        with patch("hypogeum.pipeline.run_pipeline", side_effect=blocked):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "failed"
        topic = get_topic(topic_id, db_path=db_path)
        assert topic["status"] == "queued", (
            f"expected 'queued' after budget block; got {topic['status']!r}"
        )

    def test_topic_skipped_on_qa_rejected_error(self, db_path):
        """
        QARejectedError means the episode itself failed QA at the end of a
        full run -- the topic/plan is the problem, same as an EP rejection,
        not an infrastructure hiccup. Before this fix it was release_topic()'d
        like a transient error, so the very next scheduler tick re-claimed
        the same topic and reproduced the identical QA failure forever
        (observed live 2026-09-04: ep_90382de2, ~20 retries / ~90 minutes,
        each one a real paid MUAPI attempt) -- must be 'skipped', not 'queued'.
        """
        import hypogeum.pipeline as pl_mod

        topic_id, _ = self._setup(db_path, channel_id="ch_qa_rejected")

        rejected = pl_mod.QARejectedError(
            episode_id="ep_qa", report={"failures": ["render_package: production_ready=False"]},
        )
        with patch("hypogeum.pipeline.run_pipeline", side_effect=rejected):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "failed"
        topic = get_topic(topic_id, db_path=db_path)
        assert topic["status"] == "skipped", (
            f"expected 'skipped' after QA rejection; got {topic['status']!r}"
        )

    def test_qa_rejection_also_supersedes_the_in_progress_episode(self, db_path):
        """A permanently-skipped topic must also move its episode off
        'in_progress' — otherwise the episode is stuck live forever and its
        weekly budget hold is never reaped (2026-09-07: 5 such stuck
        episodes, ~$34 leaked)."""
        import hypogeum.pipeline as pl_mod
        from hypogeum.db import get_connection

        self._active_channel(db_path, "ch_qa_sup")
        topic_id = enqueue_topic("ch_qa_sup", "qa sup topic", db_path=db_path)
        enqueue(db_path=db_path, channel_id="ch_qa_sup", episode_id="ep_qa_sup")
        conn = get_connection(db_path)
        conn.execute("INSERT INTO episodes (episode_id, title, status, created_at) "
                     "VALUES ('ep_qa_sup', 'T', 'in_progress', '2026-09-07T00:00:00+00:00')")
        conn.commit(); conn.close()

        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=pl_mod.QARejectedError(episode_id="ep_qa_sup", report={"failures": ["x"]})):
            process_next(worker_id=_worker(), db_path=db_path, dry_run=False,
                         config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0))

        conn = get_connection(db_path)
        st = conn.execute("SELECT status FROM episodes WHERE episode_id='ep_qa_sup'").fetchone()[0]
        conn.close()
        assert st == "superseded", f"expected 'superseded'; got {st!r}"

    def test_topic_skipped_on_compliance_rejected_error(self, db_path):
        """Same reasoning as QARejectedError -- Rights & Compliance rejection
        is a judgment on this exact content, not infrastructure."""
        import hypogeum.pipeline as pl_mod

        topic_id, _ = self._setup(db_path, channel_id="ch_compliance_rejected")

        rejected = pl_mod.ComplianceRejectedError(
            episode_id="ep_compliance", report={"failures": ["unlicensed source footage"]},
        )
        with patch("hypogeum.pipeline.run_pipeline", side_effect=rejected):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "failed"
        topic = get_topic(topic_id, db_path=db_path)
        assert topic["status"] == "skipped", (
            f"expected 'skipped' after compliance rejection; got {topic['status']!r}"
        )

    def test_topic_skipped_on_gated_execution_refused(self, db_path):
        """
        GatedExecutionRefused (worker_integration.py) covers a profile
        misconfig or a refused/infeasible/corrupted backward plan -- release
        would put the topic straight back to backlog with the SAME broken
        target_publish_at / config, guaranteeing the identical refusal on
        the very next claim (observed live 2026-09-04: a channel's topic
        looping on "backward plan infeasible" every scheduler tick).
        """
        from hypogeum.worker_integration import GatedExecutionRefused

        topic_id, _ = self._setup(db_path, channel_id="ch_gate_refused")

        refused = GatedExecutionRefused("backward plan infeasible: required_lead_time_seconds=130830")
        with patch("hypogeum.pipeline.run_pipeline", side_effect=refused):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "failed"
        topic = get_topic(topic_id, db_path=db_path)
        assert topic["status"] == "skipped", (
            f"expected 'skipped' after a terminal gate refusal; got {topic['status']!r}"
        )

    def test_topic_skipped_when_retry_budget_exhausted_not_released_forever(self, db_path):
        """
        A TRANSIENT exception (RuntimeError, no failure_policy) that has
        already used up its bounded auto-retry budget must be skipped, not
        plain-released -- release_topic() puts the row back in ordinary
        backlog, where the next scheduler tick claims it into a BRAND NEW
        run_queue row whose own retry_count starts at 0, silently resetting
        the cap this test is protecting and reproducing the same failure
        forever, exactly like the QA-rejection and gate-refusal cases above.
        """
        topic_id, _ = self._setup(db_path, channel_id="ch_retry_exhausted")

        with patch("hypogeum.pipeline.run_pipeline", side_effect=RuntimeError("provider timeout")):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0, QUEUE_MAX_AUTO_RETRIES=0),
            )

        assert result.status == "failed"
        topic = get_topic(topic_id, db_path=db_path)
        assert topic["status"] == "skipped", (
            f"expected 'skipped' once the retry budget is exhausted; got {topic['status']!r}"
        )

    def test_hard_episode_failure_ceiling_skips_topic_and_supersedes(self, db_path):
        """An episode with MAX_EPISODE_TOTAL_FAILURES terminally-failed runs
        already is abandoned regardless of the exception's failure_policy —
        the backstop under retry_count for a deterministic loop (BGA reject
        before it was TERMINAL, plan-hash drift on VPD re-run, ...)."""
        from hypogeum.db import get_connection
        self._active_channel(db_path, "ch_ceiling")
        topic_id = enqueue_topic("ch_ceiling", "poison topic", db_path=db_path)
        ep = "ep_ceiling_poison"
        enqueue(db_path=db_path, channel_id="ch_ceiling", episode_id=ep)

        conn = get_connection(db_path)
        try:
            for i in range(3):
                conn.execute(
                    "INSERT INTO runs (run_id, episode_id, started_at, status, channel_id) "
                    "VALUES (?,?,?,?,?)",
                    (f"r{i}", ep, "2026-09-06T00:00:00+00:00", "failed", "ch_ceiling"),
                )
            conn.execute(
                "INSERT INTO episodes (episode_id, title, created_at, status) VALUES (?,?,?,?)",
                (ep, "Poison", "2026-09-06T00:00:00+00:00", "in_progress"),
            )
            conn.commit()
        finally:
            conn.close()

        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=RuntimeError("deterministic failure again")):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0,
                                 QUEUE_MAX_AUTO_RETRIES=2, MAX_EPISODE_TOTAL_FAILURES=3),
            )
        assert result.status == "failed"
        assert get_topic(topic_id, db_path=db_path)["status"] == "skipped"
        conn = get_connection(db_path)
        try:
            st = conn.execute("SELECT status FROM episodes WHERE episode_id=?", (ep,)).fetchone()[0]
        finally:
            conn.close()
        assert st == "superseded", f"episode should be superseded; got {st!r}"

    def test_episode_below_the_ceiling_still_retries(self, db_path):
        from hypogeum.db import get_connection
        self._active_channel(db_path, "ch_below")
        topic_id = enqueue_topic("ch_below", "ok topic", db_path=db_path)
        ep = "ep_below_ceiling"
        enqueue(db_path=db_path, channel_id="ch_below", episode_id=ep)
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT INTO runs (run_id, episode_id, started_at, status, channel_id) "
                "VALUES (?,?,?,?,?)", ("r0", ep, "2026-09-06T00:00:00+00:00", "failed", "ch_below"),
            )
            conn.commit()
        finally:
            conn.close()
        with patch("hypogeum.pipeline.run_pipeline", side_effect=RuntimeError("transient")):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0,
                                 QUEUE_MAX_AUTO_RETRIES=2, MAX_EPISODE_TOTAL_FAILURES=3),
            )
        # 1 prior failure < ceiling 3 -> topic marked for retry, not skipped
        assert get_topic(topic_id, db_path=db_path)["status"] != "skipped"

    def test_topic_not_touched_on_dry_run(self, db_path):
        """dry_run=True bypasses pipeline entirely — topic lifecycle is unchanged."""
        self._active_channel(db_path, "ch_dryrun")
        topic_id = enqueue_topic("ch_dryrun", "dry run topic", db_path=db_path)
        enqueue(db_path=db_path, channel_id="ch_dryrun")

        with patch("hypogeum.pipeline.run_pipeline") as mock_pl:
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=True,
            )

        mock_pl.assert_not_called()
        assert result.status == "completed"
        topic = get_topic(topic_id, db_path=db_path)
        # dry_run never claims topics — topic stays queued
        assert topic["status"] == "queued"

    def test_topic_cleanup_does_not_suppress_original_exception(self, db_path):
        """The original pipeline exception must propagate even if topic cleanup fails."""
        topic_id, _ = self._setup(db_path, channel_id="ch_suppress")

        # Make release_topic raise to verify it doesn't suppress the pipeline error
        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=RuntimeError("original error")):
            with patch("hypogeum.topics.release_topic",
                       side_effect=RuntimeError("cleanup also failed")):
                result = process_next(
                    worker_id=_worker(), db_path=db_path, dry_run=False,
                    config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
                )

        # process_next must return a ProcessResult even when cleanup raises
        assert result.status == "failed"
        assert "original error" in result.error_message


# ===========================================================================
# Auto-retry (Roadmap priority 3 — recovery hardening)
# ===========================================================================

class TestAutoRetry:
    """
    Verifies hypogeum.queue._retry_eligible()/_schedule_retry_queue_item(),
    wired into process_next()'s exception handler. TRANSIENT (default)
    failures get a bounded, backed-off auto-retry run_queue item and reuse
    the original topic row as the retry identity (topics.mark_topic_retry());
    TERMINAL failures (FailurePolicy.TERMINAL, read via getattr — no
    isinstance taxonomy in queue.py) never do. See TestAutoRetryIdentity
    for the single-identity invariant this reuse enforces.
    """

    def _active_channel(self, db_path, channel_id="ch_retry"):
        from hypogeum.calog import set_channel_state
        set_channel_state(channel_id, "active", db_path=db_path)
        return channel_id

    def _setup(self, db_path, channel_id="ch_retry", topic_text="retry test topic"):
        self._active_channel(db_path, channel_id)
        topic_id = enqueue_topic(channel_id, topic_text, db_path=db_path)
        queue_id = enqueue(db_path=db_path, channel_id=channel_id)
        return topic_id, queue_id

    def _config(self, max_retries=2, backoff="5,20"):
        return MagicMock(
            FINANCE_BUDGET_CEILING_USD=10.0,
            QUEUE_MAX_AUTO_RETRIES=max_retries,
            QUEUE_RETRY_BACKOFF_MINUTES=backoff,
        )

    def _auto_retry_rows(self, db_path):
        return [r for r in _all_queue_rows(db_path) if r["source"] == "auto_retry"]

    def test_retryable_generic_exception_schedules_auto_retry(self, db_path):
        topic_id, queue_id = self._setup(db_path)

        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=RuntimeError("provider timeout")):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=self._config(),
            )

        assert result.status == "failed"
        retries = self._auto_retry_rows(db_path)
        assert len(retries) == 1
        assert retries[0]["retry_count"] == 1
        assert retries[0]["next_attempt_at"] is not None
        assert retries[0]["next_attempt_at"] > _iso(_now())

    def test_auto_retry_creates_matching_topic(self, db_path):
        """
        Historical name kept for git-blame continuity; the mechanism
        changed 2026-08-23 (see TestAutoRetryIdentity) -- this no longer
        CREATES a second row, it reuses the original topic's own row
        (source stamped 'auto_retry' in place). The row-count/content/
        episode_id assertions below hold either way.
        """
        self._setup(db_path, topic_text="unique retry content")

        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=RuntimeError("provider timeout")):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=self._config(),
            )

        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM episode_topics WHERE source = 'auto_retry'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0]["topic_text"] == "unique retry content"

        retry_queue_row = self._auto_retry_rows(db_path)[0]
        assert rows[0]["episode_id"] == retry_queue_row["episode_id"]

    def test_terminal_exception_does_not_schedule_retry(self, db_path):
        from hypogeum.pipeline import PipelineRejectedError

        self._setup(db_path)

        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=PipelineRejectedError("ep_x", {"overall_score": 3.0})):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=self._config(),
            )

        assert result.status == "failed"
        assert self._auto_retry_rows(db_path) == []

    def test_synthetic_terminal_exception_needs_no_queue_py_change(self, db_path):
        """
        Proves classification is generic: a brand-new exception class,
        unknown to queue.py, is treated as terminal purely by setting
        failure_policy — no isinstance/taxonomy update required.
        """
        from hypogeum.failure_policy import FailurePolicy

        class _SomeFutureTerminalError(RuntimeError):
            failure_policy = FailurePolicy.TERMINAL

        self._setup(db_path)

        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=_SomeFutureTerminalError("new kind of terminal failure")):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=self._config(),
            )

        assert self._auto_retry_rows(db_path) == []

    def test_exception_with_no_failure_policy_attribute_defaults_transient(self, db_path):
        """A plain exception with no failure_policy attribute at all is retryable."""
        class _UnknownProviderError(Exception):
            pass

        self._setup(db_path)

        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=_UnknownProviderError("some brand new provider error")):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=self._config(),
            )

        assert len(self._auto_retry_rows(db_path)) == 1

    def test_retry_exhausted_at_configured_limit(self, db_path):
        self._setup(db_path)

        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=RuntimeError("provider timeout")):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=self._config(max_retries=0),
            )

        # max_retries=0 means the very first failure is already exhausted.
        assert self._auto_retry_rows(db_path) == []

    def test_original_topic_release_unaffected_by_retry_scheduling(self, db_path):
        """
        The original topic still ends up 'queued' whether or not an
        auto-retry was scheduled -- via mark_topic_retry() (reusing this
        SAME row, source='auto_retry') when eligible, or plain
        release_topic() otherwise. See TestAutoRetryIdentity for the
        stronger claim that eligible-retry reuses this exact row rather
        than creating a second one.
        """
        topic_id, _ = self._setup(db_path)

        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=RuntimeError("provider timeout")):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=self._config(),
            )

        original_topic = get_topic(topic_id, db_path=db_path)
        assert original_topic["status"] == "queued"
        assert len(self._auto_retry_rows(db_path)) == 1

    def test_custom_backoff_minutes_respected(self, db_path):
        self._setup(db_path)

        before = _now()
        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=RuntimeError("provider timeout")):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=self._config(backoff="1,2"),
            )

        retry_row = self._auto_retry_rows(db_path)[0]
        next_attempt = datetime.fromisoformat(retry_row["next_attempt_at"])
        delta_minutes = (next_attempt - before).total_seconds() / 60.0
        assert 0.9 <= delta_minutes <= 1.5  # ~1 minute, first configured value


class TestAutoRetryIdentity:
    """
    Fixed 2026-08-23: exactly one claimable episode_topics row ever exists
    per failed episode. The previous design released the original topic
    row to plain 'queued' AND separately enqueue_topic()'d a second
    'auto_retry' row for the same episode_id -- both simultaneously
    'queued', both claimable by claim_next_topic() (channel-scoped only,
    no per-episode awareness), so an unrelated schedule tick reaching
    topic resolution during that window could claim the stale original
    row and re-execute an episode a real retry was already handling or
    had already completed. hypogeum.topics.mark_topic_retry() now reuses
    the SAME row as the one and only retry identity; see its own
    docstring and hypogeum.queue._retry_eligible()/_schedule_retry_queue_item().
    """

    def _active_channel(self, db_path, channel_id="ch_retry_identity"):
        from hypogeum.calog import set_channel_state
        set_channel_state(channel_id, "active", db_path=db_path)
        return channel_id

    def _config(self, max_retries=2, backoff="0,0"):
        return MagicMock(
            FINANCE_BUDGET_CEILING_USD=10.0,
            QUEUE_MAX_AUTO_RETRIES=max_retries,
            QUEUE_RETRY_BACKOFF_MINUTES=backoff,
        )

    def test_one_failure_leaves_exactly_one_claimable_topic_identity(self, db_path):
        channel_id = self._active_channel(db_path)
        original_topic_id = enqueue_topic(channel_id, "single identity topic", db_path=db_path)
        enqueue(db_path=db_path, channel_id=channel_id)

        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=RuntimeError("provider timeout")):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=self._config(),
            )

        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT topic_id, status, source FROM episode_topics WHERE topic_text = ?",
                ("single identity topic",),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1, rows
        assert rows[0]["topic_id"] == original_topic_id
        assert rows[0]["status"] == "queued"
        assert rows[0]["source"] == "auto_retry"

    def test_retry_uses_the_same_episode_id(self, db_path):
        channel_id = self._active_channel(db_path)
        topic_id = enqueue_topic(channel_id, "same episode id topic", db_path=db_path)
        enqueue(db_path=db_path, channel_id=channel_id)

        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=RuntimeError("provider timeout")):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=self._config(),
            )

        topic_after = get_topic(topic_id, db_path=db_path)
        retry_queue_row = [r for r in _all_queue_rows(db_path) if r["source"] == "auto_retry"][0]
        assert topic_after["episode_id"] is not None
        assert topic_after["episode_id"] == retry_queue_row["episode_id"]

    def test_repeated_retry_scheduling_creates_no_duplicate_queue_item(self, db_path):
        from hypogeum.queue import _schedule_retry_queue_item

        channel_id = self._active_channel(db_path)
        episode_id = "ep_dup_guard"
        kwargs = dict(
            channel_id=channel_id, episode_id=episode_id, retry_count=0,
            priority=0, config=self._config(), db_path=db_path,
        )
        first = _schedule_retry_queue_item(**kwargs)
        second = _schedule_retry_queue_item(**kwargs)

        assert first is True
        assert second is False, "repeated retry scheduling for the same episode must not duplicate"
        rows = [
            r for r in _all_queue_rows(db_path)
            if r["source"] == "auto_retry" and r["episode_id"] == episode_id
        ]
        assert len(rows) == 1

    def test_successful_retry_closes_the_retry_identity_no_future_reclaim(self, db_path, tmp_path):
        """
        End-to-end: failure -> reused retry identity -> the REAL production
        completion path (hypogeum.run_registry.register_run()/
        complete_run(), exactly as hypogeum.pipeline.run_pipeline() itself
        calls them) -> reconcile_stale_topics_for_episode() fires
        automatically inside complete_run() -> nothing claimable remains
        for this episode -> a subsequent claim on the channel finds
        nothing at all, proving a future schedule tick cannot reclaim the
        original row and re-execute an already-published episode.
        """
        from hypogeum.run_registry import complete_run, register_run
        from hypogeum.topics import claim_next_topic

        channel_id = self._active_channel(db_path)
        enqueue_topic(channel_id, "closes on success topic", db_path=db_path)
        enqueue(db_path=db_path, channel_id=channel_id)

        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=RuntimeError("provider timeout")):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=self._config(),
            )

        retry_queue_row = [r for r in _all_queue_rows(db_path) if r["source"] == "auto_retry"][0]
        episode_id = retry_queue_row["episode_id"]
        assert episode_id is not None

        runs_dir = tmp_path / "runs"

        # register_run() snapshots model assignments via getattr(config,
        # "EP_MODEL", ...) etc. -- a bare MagicMock() auto-creates those
        # attributes as MagicMock instances rather than raising, which then
        # fail JSON serialization inside complete_run()'s own run-record
        # write. A real pipeline run always carries a real Config with
        # real string model IDs, so this fixture matches that shape rather
        # than relying on getattr's default (only reached for a genuinely
        # *missing* attribute, not one MagicMock auto-vivifies).
        succeed_config = self._config()
        for _attr in ("EP_MODEL", "RD_MODEL", "TD_PLANNING_MODEL", "BGA_PASS1_MODEL", "BGA_PASS2_MODEL"):
            setattr(succeed_config, _attr, "test/model")

        def _succeed(*, topic_input, episode_id, config, channel_id, cancel_check=None, **_kwargs):
            run_id = register_run(
                episode_id=episode_id, config=succeed_config, db_path=db_path,
                runs_dir=runs_dir, channel_id=channel_id,
            )
            complete_run(run_id=run_id, episode_id=episode_id, db_path=db_path, runs_dir=runs_dir)

        with patch("hypogeum.pipeline.run_pipeline", side_effect=_succeed):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=self._config(),
            )
        assert result.status == "completed"

        # Nothing claimable remains for this channel -- a subsequent
        # schedule-triggered claim finds no topic (never re-executes ep_X).
        assert claim_next_topic(channel_id, db_path=db_path) is None
