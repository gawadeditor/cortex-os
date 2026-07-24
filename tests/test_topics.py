"""
tests/test_topics.py — Topic Backlog / Episode Queue + Topic Intake Layer

Coverage:
  - episode_topics table schema (columns, indexes, CHECK constraints)
  - enqueue_topic: stores topic with correct defaults
  - enqueue_topic: validates channel exists (validate_channel=True)
  - enqueue_topic: raises ChannelNotFoundError for unknown channel
  - enqueue_topic: raises DuplicateTopicError for queued/claimed duplicates
  - enqueue_topic: allows re-enqueue after consumed/cancelled/skipped
  - claim_next_topic: priority ordering, FIFO within same priority
  - claim_next_topic: atomic — claimed topics are not claimed twice
  - claim_next_topic: returns None when no queued topics remain
  - consume_topic: marks consumed; consume_at set
  - skip_topic: queued → skipped; reason stored
  - cancel_topic: queued → cancelled; claimed → cancelled
  - get_topic / list_topics: read helpers
  - make_topic_resolver: returns a callable that claims topic and returns text
  - make_topic_resolver: returns None when no topic available
  - import_topics_from_csv: adds topics from file, returns ImportResult
  - import_topics_from_csv: skips empty lines, skips duplicates, skips headers
  - process_next(dry_run=False): passes real topic_text to run_pipeline
  - process_next(dry_run=False): returns no_topic when backlog empty
  - process_next(dry_run=False): never passes "pending_topic_assignment" to pipeline
  - process_next(dry_run=False): consumes topic after successful pipeline run
  - CLI: enqueue_topics.py single topic, CSV import, dry-run, channel validation
  - Migration: existing DB gains episode_topics table
  - No publishing, no content generation, no provider/network calls
"""

from __future__ import annotations

import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.calog import set_channel_state
from hypogeum.db import get_connection, initialize_schema
from hypogeum.queue import ProcessResult, enqueue, process_next
from hypogeum.channel_config import (
    create_brand_profile,
    create_channel_profile,
    create_model_profile,
    create_publishing_profile,
)
from hypogeum.topics import (
    ChannelNotFoundError,
    DuplicateTopicError,
    ImportResult,
    cancel_topic,
    claim_next_topic,
    consume_topic,
    enqueue_topic,
    get_topic,
    import_topics_from_csv,
    list_topics,
    make_topic_resolver,
    set_topic_target_publish_at,
    skip_topic,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _worker() -> str:
    return str(uuid.uuid4())


def _active_channel(db_path: Path, channel_id: str = "ch_t") -> str:
    set_channel_state(channel_id, "active", db_path=db_path)
    return channel_id


def _registered_channel(db_path: Path, channel_id: str = "ch_reg") -> str:
    """Create a channel profile row so validate_channel=True passes."""
    bp = create_brand_profile("Brand", db_path=db_path)
    pp = create_publishing_profile("Publishing", db_path=db_path)
    mp = create_model_profile("Models", db_path=db_path)
    create_channel_profile(
        channel_id, "Test Channel",
        brand_profile_id=bp.brand_profile_id,
        publishing_profile_id=pp.publishing_profile_id,
        model_profile_id=mp.model_profile_id,
        db_path=db_path,
    )
    return channel_id


def _write_csv(tmp_path: Path, lines: list[str], filename: str = "topics.csv") -> Path:
    p = tmp_path / filename
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _enqueue_with_topic(db_path: Path, channel_id: str, topic: str = "Ancient Rome",
                         priority: int = 0) -> tuple[str, str]:
    """Return (queue_id, topic_id)."""
    queue_id = enqueue(db_path=db_path, channel_id=channel_id)
    topic_id = enqueue_topic(channel_id, topic, priority=priority, db_path=db_path)
    return queue_id, topic_id


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
            f"topic tests must not open network connections (attempted: {address})"
        )
    monkeypatch.setattr(socket.socket, "connect", _block)
    yield


# ===========================================================================
# 1. Schema
# ===========================================================================

class TestSchema:

    def test_table_exists(self, db_path):
        conn = get_connection(db_path)
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "episode_topics" in tables

    def test_columns(self, db_path):
        conn = get_connection(db_path)
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(episode_topics)"
            ).fetchall()}
        finally:
            conn.close()
        required = {
            "topic_id", "channel_id", "topic_text", "status", "priority",
            "reason", "claimed_at", "consumed_at", "created_at", "updated_at",
        }
        assert required <= cols

    def test_indexes_exist(self, db_path):
        conn = get_connection(db_path)
        try:
            indexes = {
                r[1] for r in conn.execute(
                    "SELECT * FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='episode_topics'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "idx_ep_topics_channel_status" in indexes
        assert "idx_ep_topics_claim_order" in indexes

    def test_status_check_constraint(self, db_path):
        conn = get_connection(db_path)
        try:
            with pytest.raises(Exception):
                conn.execute(
                    "INSERT INTO episode_topics "
                    "(topic_id, channel_id, topic_text, status, created_at, updated_at) "
                    "VALUES ('x', 'ch', 'text', 'bad_status', 'now', 'now')"
                )
                conn.commit()
        finally:
            conn.close()

    def test_default_status_is_queued(self, db_path):
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT INTO episode_topics "
                "(topic_id, channel_id, topic_text, created_at, updated_at) "
                "VALUES ('y', 'ch', 'text', 'now', 'now')"
            )
            conn.commit()
            row = conn.execute(
                "SELECT status FROM episode_topics WHERE topic_id='y'"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "queued"

    def test_existing_db_gains_table(self, tmp_path):
        p = tmp_path / "old.db"
        initialize_schema(p)
        conn = get_connection(p)
        try:
            conn.execute("DROP TABLE IF EXISTS episode_topics")
            conn.commit()
        finally:
            conn.close()
        initialize_schema(p)
        conn = get_connection(p)
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "episode_topics" in tables


# ===========================================================================
# 2. enqueue_topic
# ===========================================================================

class TestEnqueueTopic:

    def test_returns_uuid_string(self, db_path):
        tid = enqueue_topic("ch_e", "The Roman Empire", db_path=db_path)
        parsed = uuid.UUID(tid)
        assert parsed.version == 4

    def test_stored_with_queued_status(self, db_path):
        tid = enqueue_topic("ch_e", "The Roman Empire", db_path=db_path)
        row = get_topic(tid, db_path=db_path)
        assert row["status"] == "queued"

    def test_stores_topic_text(self, db_path):
        tid = enqueue_topic("ch_e", "  Ancient Greece  ", db_path=db_path)
        row = get_topic(tid, db_path=db_path)
        assert row["topic_text"] == "Ancient Greece"  # stripped

    def test_stores_channel_id(self, db_path):
        tid = enqueue_topic("ch_xyz", "topic", db_path=db_path)
        assert get_topic(tid, db_path=db_path)["channel_id"] == "ch_xyz"

    def test_stores_priority(self, db_path):
        tid = enqueue_topic("ch_p", "topic", priority=-1, db_path=db_path)
        assert get_topic(tid, db_path=db_path)["priority"] == -1

    def test_default_priority_zero(self, db_path):
        tid = enqueue_topic("ch_p0", "topic", db_path=db_path)
        assert get_topic(tid, db_path=db_path)["priority"] == 0

    def test_empty_topic_text_raises(self, db_path):
        with pytest.raises(ValueError, match="topic_text"):
            enqueue_topic("ch_e", "", db_path=db_path)

    def test_empty_channel_id_raises(self, db_path):
        with pytest.raises(ValueError, match="channel_id"):
            enqueue_topic("", "some topic", db_path=db_path)

    def test_multiple_topics_same_channel(self, db_path):
        t1 = enqueue_topic("ch_m", "T1", db_path=db_path)
        t2 = enqueue_topic("ch_m", "T2", db_path=db_path)
        assert t1 != t2
        topics = list_topics(channel_id="ch_m", db_path=db_path)
        assert len(topics) == 2


# ===========================================================================
# 3. claim_next_topic
# ===========================================================================

class TestClaimNextTopic:

    def test_returns_none_when_empty(self, db_path):
        assert claim_next_topic("ch_empty", db_path=db_path) is None

    def test_claims_queued_topic(self, db_path):
        tid = enqueue_topic("ch_c", "topic", db_path=db_path)
        result = claim_next_topic("ch_c", db_path=db_path)
        assert result is not None
        assert result["topic_id"] == tid
        assert result["status"] == "claimed"

    def test_claimed_topic_not_returned_again(self, db_path):
        enqueue_topic("ch_c2", "topic", db_path=db_path)
        claim_next_topic("ch_c2", db_path=db_path)
        assert claim_next_topic("ch_c2", db_path=db_path) is None

    def test_claimed_at_timestamp_set(self, db_path):
        before = _now()
        enqueue_topic("ch_ct", "topic", db_path=db_path)
        result = claim_next_topic("ch_ct", db_path=db_path)
        after = _now()
        claimed_at = datetime.fromisoformat(result["claimed_at"])
        assert before <= claimed_at <= after

    def test_priority_ordering_lower_first(self, db_path):
        t_low = enqueue_topic("ch_pr", "Low priority", priority=5, db_path=db_path)
        t_high = enqueue_topic("ch_pr", "High priority", priority=-1, db_path=db_path)
        result = claim_next_topic("ch_pr", db_path=db_path)
        assert result["topic_id"] == t_high

    def test_fifo_within_same_priority(self, db_path):
        t1 = enqueue_topic("ch_fifo", "First", priority=0, db_path=db_path)
        t2 = enqueue_topic("ch_fifo", "Second", priority=0, db_path=db_path)
        t3 = enqueue_topic("ch_fifo", "Third", priority=0, db_path=db_path)
        order = []
        for _ in range(3):
            row = claim_next_topic("ch_fifo", db_path=db_path)
            order.append(row["topic_id"])
        assert order == [t1, t2, t3]

    def test_priority_beats_fifo(self, db_path):
        t_first = enqueue_topic("ch_pb", "First in, low priority", priority=10,
                                 db_path=db_path)
        t_second = enqueue_topic("ch_pb", "Second in, high priority", priority=1,
                                  db_path=db_path)
        result = claim_next_topic("ch_pb", db_path=db_path)
        assert result["topic_id"] == t_second

    def test_channels_are_independent(self, db_path):
        t_a = enqueue_topic("ch_a", "Topic A", db_path=db_path)
        t_b = enqueue_topic("ch_b", "Topic B", db_path=db_path)
        r_a = claim_next_topic("ch_a", db_path=db_path)
        r_b = claim_next_topic("ch_b", db_path=db_path)
        assert r_a["topic_id"] == t_a
        assert r_b["topic_id"] == t_b

    def test_atomic_double_claim_prevention(self, db_path):
        enqueue_topic("ch_atom", "topic", db_path=db_path)
        conn = get_connection(db_path)
        try:
            from hypogeum.db import claim_next_episode_topic
            now = _now().isoformat()
            r1 = claim_next_episode_topic(conn, "ch_atom", now)
            conn.commit()
            r2 = claim_next_episode_topic(conn, "ch_atom", now)
            conn.commit()
        finally:
            conn.close()
        assert r1 is not None
        assert r2 is None


# ===========================================================================
# 3b. set_topic_target_publish_at (Scheduler Integration, race-condition
# correction: exact-topic_id write, no "next queued" peek/ordering at all —
# the caller already holds the specific topic_id of a row it just claimed)
# ===========================================================================

class TestSetTopicTargetPublishAt:

    def test_fills_null_target_by_exact_topic_id(self, db_path):
        tid = enqueue_topic("ch_tp", "topic", db_path=db_path)
        target = "2026-07-30T18:00:00+00:00"
        result = set_topic_target_publish_at(tid, target, db_path=db_path)
        assert result is True
        row = get_topic(tid, db_path=db_path)
        assert row["target_publish_at"] == target

    def test_does_not_overwrite_existing_explicit_value(self, db_path):
        tid = enqueue_topic("ch_tp2", "topic", db_path=db_path)
        conn = get_connection(db_path)
        try:
            conn.execute(
                "UPDATE episode_topics SET target_publish_at = ? WHERE topic_id = ?",
                ("2026-01-01T00:00:00+00:00", tid),
            )
            conn.commit()
        finally:
            conn.close()

        result = set_topic_target_publish_at(
            tid, "2026-07-30T18:00:00+00:00", db_path=db_path,
        )
        assert result is False
        row = get_topic(tid, db_path=db_path)
        assert row["target_publish_at"] == "2026-01-01T00:00:00+00:00"

    def test_returns_false_for_unknown_topic_id(self, db_path):
        result = set_topic_target_publish_at(
            "topic_does_not_exist", "2026-07-30T18:00:00+00:00", db_path=db_path,
        )
        assert result is False

    def test_ignores_other_topics_in_same_channel(self, db_path):
        """Unlike the removed peek-based setter, this writes only to the
        exact topic_id given — never to 'whichever topic ranks first'."""
        t_low = enqueue_topic("ch_tp3", "Low priority", priority=5, db_path=db_path)
        t_high = enqueue_topic("ch_tp3", "High priority", priority=-1, db_path=db_path)
        set_topic_target_publish_at(t_low, "2026-07-30T18:00:00+00:00", db_path=db_path)
        assert get_topic(t_low, db_path=db_path)["target_publish_at"] is not None
        assert get_topic(t_high, db_path=db_path)["target_publish_at"] is None

    def test_does_not_claim_or_change_status(self, db_path):
        tid = enqueue_topic("ch_tp4", "topic", db_path=db_path)
        set_topic_target_publish_at(tid, "2026-07-30T18:00:00+00:00", db_path=db_path)
        row = get_topic(tid, db_path=db_path)
        assert row["status"] == "queued"
        claimed = claim_next_topic("ch_tp4", db_path=db_path)
        assert claimed["topic_id"] == tid
        assert claimed["target_publish_at"] == "2026-07-30T18:00:00+00:00"


# ===========================================================================
# 4. consume_topic
# ===========================================================================

class TestConsumeTopic:

    def test_claimed_to_consumed(self, db_path):
        tid = enqueue_topic("ch_con", "topic", db_path=db_path)
        claim_next_topic("ch_con", db_path=db_path)
        consume_topic(tid, db_path=db_path)
        assert get_topic(tid, db_path=db_path)["status"] == "consumed"

    def test_consumed_at_set(self, db_path):
        before = _now()
        tid = enqueue_topic("ch_con2", "topic", db_path=db_path)
        claim_next_topic("ch_con2", db_path=db_path)
        consume_topic(tid, db_path=db_path)
        after = _now()
        row = get_topic(tid, db_path=db_path)
        consumed_at = datetime.fromisoformat(row["consumed_at"])
        assert before <= consumed_at <= after

    def test_consume_queued_topic_is_noop(self, db_path):
        tid = enqueue_topic("ch_con3", "topic", db_path=db_path)
        consume_topic(tid, db_path=db_path)
        assert get_topic(tid, db_path=db_path)["status"] == "queued"


# ===========================================================================
# 5. skip_topic
# ===========================================================================

class TestSkipTopic:

    def test_queued_to_skipped(self, db_path):
        tid = enqueue_topic("ch_sk", "topic", db_path=db_path)
        skip_topic(tid, db_path=db_path)
        assert get_topic(tid, db_path=db_path)["status"] == "skipped"

    def test_reason_stored(self, db_path):
        tid = enqueue_topic("ch_sk2", "topic", db_path=db_path)
        skip_topic(tid, reason="Not relevant anymore", db_path=db_path)
        assert get_topic(tid, db_path=db_path)["reason"] == "Not relevant anymore"

    def test_skip_claimed_topic_marks_skipped(self, db_path):
        # claimed topics can be skipped (e.g. ep_rejected path in process_next)
        tid = enqueue_topic("ch_sk3", "topic", db_path=db_path)
        claim_next_topic("ch_sk3", db_path=db_path)
        skip_topic(tid, reason="ep_rejected", db_path=db_path)
        row = get_topic(tid, db_path=db_path)
        assert row["status"] == "skipped"
        assert row["reason"] == "ep_rejected"

    def test_skipped_topic_not_claimed(self, db_path):
        tid = enqueue_topic("ch_sk4", "topic", db_path=db_path)
        skip_topic(tid, db_path=db_path)
        assert claim_next_topic("ch_sk4", db_path=db_path) is None


# ===========================================================================
# 6. cancel_topic
# ===========================================================================

class TestCancelTopic:

    def test_queued_to_cancelled(self, db_path):
        tid = enqueue_topic("ch_can", "topic", db_path=db_path)
        cancel_topic(tid, db_path=db_path)
        assert get_topic(tid, db_path=db_path)["status"] == "cancelled"

    def test_claimed_to_cancelled(self, db_path):
        tid = enqueue_topic("ch_can2", "topic", db_path=db_path)
        claim_next_topic("ch_can2", db_path=db_path)
        cancel_topic(tid, db_path=db_path)
        assert get_topic(tid, db_path=db_path)["status"] == "cancelled"

    def test_consumed_cannot_be_cancelled(self, db_path):
        tid = enqueue_topic("ch_can3", "topic", db_path=db_path)
        claim_next_topic("ch_can3", db_path=db_path)
        consume_topic(tid, db_path=db_path)
        cancel_topic(tid, db_path=db_path)  # no-op
        assert get_topic(tid, db_path=db_path)["status"] == "consumed"

    def test_cancelled_topic_not_claimed(self, db_path):
        tid = enqueue_topic("ch_can4", "topic", db_path=db_path)
        cancel_topic(tid, db_path=db_path)
        assert claim_next_topic("ch_can4", db_path=db_path) is None


# ===========================================================================
# 7. list_topics
# ===========================================================================

class TestListTopics:

    def test_returns_all_when_no_filter(self, db_path):
        enqueue_topic("ch_l1", "T1", db_path=db_path)
        enqueue_topic("ch_l2", "T2", db_path=db_path)
        topics = list_topics(db_path=db_path)
        assert len(topics) >= 2

    def test_filter_by_channel_id(self, db_path):
        enqueue_topic("ch_lf", "T1", db_path=db_path)
        enqueue_topic("ch_lf", "T2", db_path=db_path)
        enqueue_topic("ch_other", "T3", db_path=db_path)
        topics = list_topics(channel_id="ch_lf", db_path=db_path)
        assert len(topics) == 2
        assert all(t["channel_id"] == "ch_lf" for t in topics)

    def test_filter_by_status(self, db_path):
        enqueue_topic("ch_ls", "T1", db_path=db_path)
        t2 = enqueue_topic("ch_ls", "T2", db_path=db_path)
        skip_topic(t2, db_path=db_path)
        queued = list_topics(channel_id="ch_ls", status="queued", db_path=db_path)
        assert len(queued) == 1
        assert queued[0]["topic_text"] == "T1"

    def test_returns_list_of_dicts(self, db_path):
        enqueue_topic("ch_ld", "T", db_path=db_path)
        topics = list_topics(db_path=db_path)
        assert isinstance(topics, list)
        assert isinstance(topics[0], dict)
        assert "topic_id" in topics[0]


# ===========================================================================
# 8. make_topic_resolver
# ===========================================================================

class TestMakeTopicResolver:

    def test_returns_topic_text_when_available(self, db_path):
        enqueue_topic("ch_r", "The Colosseum", db_path=db_path)
        resolver = make_topic_resolver(db_path=db_path)
        result = resolver({"channel_id": "ch_r"})
        assert result == "The Colosseum"

    def test_returns_none_when_no_topics(self, db_path):
        resolver = make_topic_resolver(db_path=db_path)
        result = resolver({"channel_id": "ch_empty_r"})
        assert result is None

    def test_returns_none_when_no_channel_id(self, db_path):
        enqueue_topic("ch_r2", "topic", db_path=db_path)
        resolver = make_topic_resolver(db_path=db_path)
        result = resolver({})
        assert result is None

    def test_claims_topic_as_side_effect(self, db_path):
        enqueue_topic("ch_r3", "topic", db_path=db_path)
        resolver = make_topic_resolver(db_path=db_path)
        resolver({"channel_id": "ch_r3"})
        # Topic is now claimed — second call returns None
        result2 = resolver({"channel_id": "ch_r3"})
        assert result2 is None

    def test_never_returns_placeholder(self, db_path):
        resolver = make_topic_resolver(db_path=db_path)
        result = resolver({"channel_id": "ch_placeholder"})
        assert result != "pending_topic_assignment"
        assert result is None


# ===========================================================================
# 9. process_next() + topic backlog integration
# ===========================================================================

class TestProcessNextTopicIntegration:
    """
    run_pipeline is always mocked. No content is generated.
    Tests verify topic_text reaches run_pipeline and placeholder never does.
    """

    def _setup(self, db_path, channel_id="ch_pi", topic="The Pantheon"):
        _active_channel(db_path, channel_id)
        queue_id = enqueue(db_path=db_path, channel_id=channel_id)
        topic_id = enqueue_topic(channel_id, topic, db_path=db_path)
        return queue_id, topic_id

    def test_real_topic_passed_to_pipeline(self, db_path):
        self._setup(db_path, topic="The Pantheon")
        captured = {}

        def fake_pipeline(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("hypogeum.pipeline.run_pipeline", side_effect=fake_pipeline):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "completed"
        assert captured.get("topic_input") == "The Pantheon"

    def test_placeholder_never_reaches_pipeline(self, db_path):
        _active_channel(db_path)
        enqueue(db_path=db_path, channel_id="ch_t")
        # No topic in backlog — placeholder must not be passed

        with patch("hypogeum.pipeline.run_pipeline") as mock_pl:
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        mock_pl.assert_not_called()
        assert result.status == "no_topic"
        # Verify the sentinel string never went anywhere near run_pipeline
        all_calls = mock_pl.call_args_list
        for call in all_calls:
            assert "pending_topic_assignment" not in str(call)

    def test_no_topic_releases_queue_item(self, db_path):
        _active_channel(db_path)
        qid = enqueue(db_path=db_path, channel_id="ch_t")

        with patch("hypogeum.pipeline.run_pipeline"):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        from hypogeum.db import get_queue_item
        conn = get_connection(db_path)
        try:
            row = get_queue_item(conn, qid)
        finally:
            conn.close()
        assert row["status"] == "queued", (
            "Queue item must be released back to queued when no topic is available"
        )

    def test_topic_consumed_after_pipeline_success(self, db_path):
        _, topic_id = self._setup(db_path, "ch_tc", "Roman Forum")

        with patch("hypogeum.pipeline.run_pipeline", return_value=MagicMock()):
            process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        row = get_topic(topic_id, db_path=db_path)
        assert row["status"] == "consumed"

    def test_topic_released_to_queued_on_pipeline_failure(self, db_path):
        # Non-rejection failures release the topic for retry on the next pass.
        _, topic_id = self._setup(db_path, "ch_tf", "Pompeii")

        with patch("hypogeum.pipeline.run_pipeline",
                   side_effect=RuntimeError("provider timeout")):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "failed"
        row = get_topic(topic_id, db_path=db_path)
        assert row["status"] == "queued"
        assert row["claimed_at"] is None

    def test_result_contains_topic_id_on_success(self, db_path):
        _, topic_id = self._setup(db_path, "ch_tr", "The Circus Maximus")

        with patch("hypogeum.pipeline.run_pipeline", return_value=MagicMock()):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.topic_id == topic_id

    def test_dry_run_does_not_consume_topic(self, db_path):
        _, topic_id = self._setup(db_path, "ch_dry", "Dry run topic")

        with patch("hypogeum.pipeline.run_pipeline"):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=True,
            )

        assert result.status == "completed"
        # dry_run bypasses topic resolution entirely — topic stays queued
        row = get_topic(topic_id, db_path=db_path)
        assert row["status"] == "queued"

    def test_multiple_topics_processes_one_per_run(self, db_path):
        _active_channel(db_path, "ch_multi")
        enqueue(db_path=db_path, channel_id="ch_multi")
        t1 = enqueue_topic("ch_multi", "Topic One", priority=0, db_path=db_path)
        t2 = enqueue_topic("ch_multi", "Topic Two", priority=1, db_path=db_path)

        with patch("hypogeum.pipeline.run_pipeline", return_value=MagicMock()):
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        assert result.status == "completed"
        # First (higher priority / earlier) topic was consumed
        assert get_topic(t1, db_path=db_path)["status"] == "consumed"
        # Second topic still queued
        assert get_topic(t2, db_path=db_path)["status"] == "queued"

    def test_no_channel_id_no_topic_no_pipeline(self, db_path):
        """Items with no channel_id bypass CALOG and topic backlog."""
        enqueue(db_path=db_path, channel_id=None)

        with patch("hypogeum.pipeline.run_pipeline") as mock_pl:
            result = process_next(
                worker_id=_worker(), db_path=db_path, dry_run=False,
                config=MagicMock(FINANCE_BUDGET_CEILING_USD=10.0),
            )

        # No channel_id and no topic_resolver → no_topic, pipeline not called
        assert result.status == "no_topic"
        mock_pl.assert_not_called()


# ===========================================================================
# 10. Topic Intake — channel validation
# ===========================================================================

class TestTopicIntakeValidation:

    def test_validate_channel_true_rejects_unknown(self, db_path):
        with pytest.raises(ChannelNotFoundError) as exc_info:
            enqueue_topic("no_such_channel", "topic", validate_channel=True,
                          db_path=db_path)
        assert exc_info.value.channel_id == "no_such_channel"

    def test_validate_channel_false_skips_check(self, db_path):
        # No channel profile exists; validate_channel=False must not raise
        tid = enqueue_topic("ghost_ch", "topic", validate_channel=False, db_path=db_path)
        assert get_topic(tid, db_path=db_path) is not None

    def test_validate_channel_true_accepts_known(self, db_path):
        ch = _registered_channel(db_path, "ch_known")
        tid = enqueue_topic(ch, "Valid topic", validate_channel=True, db_path=db_path)
        assert get_topic(tid, db_path=db_path)["channel_id"] == ch

    def test_channel_not_found_error_message_contains_id(self, db_path):
        try:
            enqueue_topic("missing_ch", "topic", validate_channel=True, db_path=db_path)
        except ChannelNotFoundError as exc:
            assert "missing_ch" in str(exc)

    def test_enqueue_topic_explicit_validates_channel(self, db_path):
        # Passing validate_channel=True should reject unknown channel
        with pytest.raises(ChannelNotFoundError):
            enqueue_topic("unknown", "topic", validate_channel=True, db_path=db_path)


# ===========================================================================
# 11. Topic Intake — duplicate prevention
# ===========================================================================

class TestDuplicatePrevention:

    def test_exact_duplicate_queued_raises(self, db_path):
        enqueue_topic("ch_dup", "Roman Aqueducts", validate_channel=False, db_path=db_path)
        with pytest.raises(DuplicateTopicError) as exc_info:
            enqueue_topic("ch_dup", "Roman Aqueducts", validate_channel=False, db_path=db_path)
        assert exc_info.value.channel_id == "ch_dup"
        assert "Roman Aqueducts" in str(exc_info.value)

    def test_case_insensitive_duplicate_detected(self, db_path):
        enqueue_topic("ch_ci", "roman aqueducts", validate_channel=False, db_path=db_path)
        with pytest.raises(DuplicateTopicError):
            enqueue_topic("ch_ci", "ROMAN AQUEDUCTS", validate_channel=False, db_path=db_path)

    def test_whitespace_normalised_duplicate_detected(self, db_path):
        enqueue_topic("ch_ws", "Roman  Aqueducts", validate_channel=False, db_path=db_path)
        with pytest.raises(DuplicateTopicError):
            enqueue_topic("ch_ws", "  Roman Aqueducts  ", validate_channel=False, db_path=db_path)

    def test_claimed_topic_counts_as_duplicate(self, db_path):
        enqueue_topic("ch_cl", "Topic X", validate_channel=False, db_path=db_path)
        claim_next_topic("ch_cl", db_path=db_path)
        with pytest.raises(DuplicateTopicError):
            enqueue_topic("ch_cl", "Topic X", validate_channel=False, db_path=db_path)

    def test_consumed_topic_allows_reenqueue(self, db_path):
        tid = enqueue_topic("ch_rc", "Pompeii", validate_channel=False, db_path=db_path)
        claim_next_topic("ch_rc", db_path=db_path)
        consume_topic(tid, db_path=db_path)
        # After consuming, re-enqueue is allowed
        tid2 = enqueue_topic("ch_rc", "Pompeii", validate_channel=False, db_path=db_path)
        assert tid2 != tid

    def test_cancelled_topic_allows_reenqueue(self, db_path):
        tid = enqueue_topic("ch_rcc", "Herculaneum", validate_channel=False, db_path=db_path)
        cancel_topic(tid, db_path=db_path)
        tid2 = enqueue_topic("ch_rcc", "Herculaneum", validate_channel=False, db_path=db_path)
        assert tid2 != tid

    def test_skipped_topic_allows_reenqueue(self, db_path):
        tid = enqueue_topic("ch_rsk", "Ostia Antica", validate_channel=False, db_path=db_path)
        skip_topic(tid, db_path=db_path)
        tid2 = enqueue_topic("ch_rsk", "Ostia Antica", validate_channel=False, db_path=db_path)
        assert tid2 != tid

    def test_different_channels_not_duplicates(self, db_path):
        enqueue_topic("ch_d1", "Same Topic", validate_channel=False, db_path=db_path)
        # Same text, different channel — must succeed
        tid = enqueue_topic("ch_d2", "Same Topic", validate_channel=False, db_path=db_path)
        assert get_topic(tid, db_path=db_path) is not None


# ===========================================================================
# 12. CSV import
# ===========================================================================

class TestCSVImport:

    def test_basic_import(self, db_path, tmp_path):
        ch = _registered_channel(db_path, "ch_csv")
        csv_path = _write_csv(tmp_path, ["The Forum", "The Colosseum", "Pompeii"])
        result = import_topics_from_csv(ch, csv_path, db_path=db_path)
        assert result.added == 3
        assert result.skipped_empty == 0
        assert result.skipped_duplicates == 0
        assert result.errors == []

    def test_header_row_skipped(self, db_path, tmp_path):
        ch = _registered_channel(db_path, "ch_hdr")
        for idx, header in enumerate(("topic", "topic_text", "text", "title",
                                      "TOPIC", "Topic_Text")):
            # Each header variant gets a unique channel ID to avoid cross-contamination
            cid = f"ch_hdr_{idx}"
            create_channel_profile(cid, cid, db_path=db_path)
            csv_path = _write_csv(tmp_path, [header, "Ancient Rome"],
                                  f"h_{idx}.csv")
            result = import_topics_from_csv(cid, csv_path,
                                            validate_channel=False,
                                            db_path=db_path)
            assert result.added == 1, f"header {header!r} was not skipped"

    def test_empty_lines_skipped(self, db_path, tmp_path):
        ch = _registered_channel(db_path, "ch_emp")
        # 3 empty/whitespace entries: "", "", "  " — all skipped; 2 real topics added
        csv_path = _write_csv(tmp_path, ["", "Valid Topic", "", "  ", "Another"])
        result = import_topics_from_csv(ch, csv_path, db_path=db_path)
        assert result.added == 2
        assert result.skipped_empty == 3

    def test_duplicate_lines_skipped(self, db_path, tmp_path):
        ch = _registered_channel(db_path, "ch_csvdup")
        csv_path = _write_csv(tmp_path, ["Roman Forum", "Roman Forum", "Colosseum"])
        result = import_topics_from_csv(ch, csv_path, db_path=db_path)
        assert result.added == 2
        assert result.skipped_duplicates == 1

    def test_pre_existing_duplicate_skipped(self, db_path, tmp_path):
        ch = _registered_channel(db_path, "ch_predup")
        enqueue_topic(ch, "Pompeii", validate_channel=False, db_path=db_path)
        csv_path = _write_csv(tmp_path, ["Pompeii", "Herculaneum"])
        result = import_topics_from_csv(ch, csv_path, db_path=db_path)
        assert result.added == 1
        assert result.skipped_duplicates == 1

    def test_file_not_found_raises(self, db_path, tmp_path):
        ch = _registered_channel(db_path, "ch_fnf")
        with pytest.raises(FileNotFoundError):
            import_topics_from_csv(ch, tmp_path / "nonexistent.csv", db_path=db_path)

    def test_unknown_channel_raises(self, db_path, tmp_path):
        csv_path = _write_csv(tmp_path, ["Topic"])
        with pytest.raises(ChannelNotFoundError):
            import_topics_from_csv("ghost_channel", csv_path,
                                   validate_channel=True, db_path=db_path)

    def test_import_result_str(self, db_path, tmp_path):
        ch = _registered_channel(db_path, "ch_str")
        csv_path = _write_csv(tmp_path, ["T1", "T2"])
        result = import_topics_from_csv(ch, csv_path, db_path=db_path)
        s = str(result)
        assert "added=2" in s
        assert ch in s

    def test_import_sets_priority(self, db_path, tmp_path):
        ch = _registered_channel(db_path, "ch_pri")
        csv_path = _write_csv(tmp_path, ["Urgent topic"])
        import_topics_from_csv(ch, csv_path, priority=-1, db_path=db_path)
        topics = list_topics(channel_id=ch, db_path=db_path)
        assert topics[0]["priority"] == -1

    def test_topics_stored_in_db_after_import(self, db_path, tmp_path):
        ch = _registered_channel(db_path, "ch_store")
        csv_path = _write_csv(tmp_path, ["Topic A", "Topic B", "Topic C"])
        import_topics_from_csv(ch, csv_path, db_path=db_path)
        stored = list_topics(channel_id=ch, status="queued", db_path=db_path)
        texts = {t["topic_text"] for t in stored}
        assert texts == {"Topic A", "Topic B", "Topic C"}

    def test_single_column_csv_format(self, db_path, tmp_path):
        ch = _registered_channel(db_path, "ch_scsv")
        # True CSV: single column, no header
        csv_path = tmp_path / "single.csv"
        csv_path.write_text('"The Pantheon"\n"The Colosseum"\n', encoding="utf-8")
        result = import_topics_from_csv(ch, csv_path, db_path=db_path)
        assert result.added == 2


# ===========================================================================
# 13. CLI — enqueue_topics.py
# ===========================================================================

class TestCLI:
    """Tests call CLI functions directly (no subprocess) for speed and isolation."""

    def _get_main(self):
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location(
            "enqueue_topics",
            Path(__file__).parent.parent / "scripts" / "enqueue_topics.py",
        )
        mod = importlib.util.load_from_spec(spec)   # type: ignore[attr-defined]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    def _run_main(self, args: list[str]) -> int:
        import sys
        from scripts.enqueue_topics import main as _main  # direct import
        old_argv = sys.argv
        sys.argv = ["enqueue_topics.py"] + args
        try:
            return _main()
        except SystemExit as exc:
            return int(exc.code) if exc.code is not None else 0
        finally:
            sys.argv = old_argv

    def test_single_topic_success(self, db_path, tmp_path):
        ch = _registered_channel(db_path, "ch_cli")
        rc = self._run_main([
            "--channel-id", ch,
            "--topic", "CLI Topic",
            "--db-path", str(db_path),
        ])
        assert rc == 0
        topics = list_topics(channel_id=ch, db_path=db_path)
        assert any(t["topic_text"] == "CLI Topic" for t in topics)

    def test_unknown_channel_returns_1(self, db_path):
        rc = self._run_main([
            "--channel-id", "ghost_ch_cli",
            "--topic", "Topic",
            "--db-path", str(db_path),
        ])
        assert rc == 1

    def test_duplicate_single_topic_returns_0(self, db_path):
        ch = _registered_channel(db_path, "ch_clidup")
        enqueue_topic(ch, "Duplicate", validate_channel=False, db_path=db_path)
        rc = self._run_main([
            "--channel-id", ch,
            "--topic", "Duplicate",
            "--db-path", str(db_path),
        ])
        # Duplicate is a warning, not a hard error — exit 0
        assert rc == 0

    def test_csv_import_via_cli(self, db_path, tmp_path):
        ch = _registered_channel(db_path, "ch_clicsv")
        csv_path = _write_csv(tmp_path, ["Forum", "Colosseum"])
        rc = self._run_main([
            "--channel-id", ch,
            "--csv", str(csv_path),
            "--db-path", str(db_path),
        ])
        assert rc == 0
        topics = list_topics(channel_id=ch, db_path=db_path)
        assert len(topics) == 2

    def test_dry_run_single_does_not_insert(self, db_path):
        ch = _registered_channel(db_path, "ch_clidry")
        rc = self._run_main([
            "--channel-id", ch,
            "--topic", "Dry run topic",
            "--dry-run",
            "--db-path", str(db_path),
        ])
        assert rc == 0
        assert list_topics(channel_id=ch, db_path=db_path) == []

    def test_dry_run_csv_does_not_insert(self, db_path, tmp_path):
        ch = _registered_channel(db_path, "ch_clidrycsv")
        csv_path = _write_csv(tmp_path, ["T1", "T2"])
        rc = self._run_main([
            "--channel-id", ch,
            "--csv", str(csv_path),
            "--dry-run",
            "--db-path", str(db_path),
        ])
        assert rc == 0
        assert list_topics(channel_id=ch, db_path=db_path) == []

    def test_missing_csv_returns_1(self, db_path, tmp_path):
        ch = _registered_channel(db_path, "ch_clinocsv")
        rc = self._run_main([
            "--channel-id", ch,
            "--csv", str(tmp_path / "nonexistent.csv"),
            "--db-path", str(db_path),
        ])
        assert rc == 1
