"""
tests/test_queue_rerun.py — Queue/claim ordering for rerun recovery

Covers:
  TestClaimOrderEpisodeTopics  — claim_next_episode_topic prefers rerun, then newest
  TestClaimOrderRunQueue       — claim_next_queue_item prefers rerun, then newest
  TestRerunCancelsStale        — cancel_queued_topics_for_episode + run_queue variant
  TestRunQueueSchema           — source + recovery_from_run_id stored and returned
  TestWorkerPicksNewRerun      — old queued run exists + new rerun → worker picks new
  TestEpisodeIdPreserved       — rerun episode_id flows through queue to claim
  TestNoStaleClaimAfterCancel  — claimed rows not touched by cancel helpers
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.db import (
    cancel_queued_run_queue_items_for_episode,
    cancel_queued_topics_for_episode,
    claim_next_episode_topic,
    claim_next_queue_item,
    get_connection,
    initialize_schema,
    insert_queue_item,
)
from hypogeum.queue import enqueue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    initialize_schema(p)
    return p


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(offset_seconds: float = 0.0) -> str:
    """Return a fixed timestamp offset from a base time for ordering tests."""
    from datetime import timedelta
    base = datetime(2026, 6, 29, 8, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=offset_seconds)).isoformat()


def _insert_topic(
    conn,
    topic_id: str,
    channel_id: str,
    source: str,
    episode_id: str | None,
    priority: int,
    created_at: str,
    status: str = "queued",
) -> None:
    conn.execute(
        """
        INSERT INTO episode_topics
            (topic_id, channel_id, topic_text, status, priority,
             source, episode_id, created_at, updated_at)
        VALUES (?, ?, 'Test Topic', ?, ?, ?, ?, ?, ?)
        """,
        (topic_id, channel_id, status, priority,
         source, episode_id, created_at, created_at),
    )
    conn.commit()


def _insert_queue(
    conn,
    queue_id: str,
    channel_id: str | None,
    episode_id: str | None,
    source: str | None,
    priority: int,
    created_at: str,
    status: str = "queued",
    recovery_from_run_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO run_queue
            (queue_id, channel_id, episode_id, priority, status,
             source, recovery_from_run_id, queued_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (queue_id, channel_id, episode_id, priority, status,
         source, recovery_from_run_id, created_at, created_at),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# TestClaimOrderEpisodeTopics
# ---------------------------------------------------------------------------

class TestClaimOrderEpisodeTopics:
    """claim_next_episode_topic: rerun before manual, newest rerun first."""

    def test_rerun_claimed_before_manual_same_priority(self, tmp_path):
        """At equal priority, source='rerun' wins over source='manual'."""
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            _insert_topic(conn, "t_manual", "ch1", "manual", None, 0, _iso(0))
            _insert_topic(conn, "t_rerun",  "ch1", "rerun",  "ep_01", 0, _iso(10))
            claimed = claim_next_episode_topic(conn, "ch1", _now())
            conn.commit()
        finally:
            conn.close()
        assert claimed["topic_id"] == "t_rerun"

    def test_priority_beats_source(self, tmp_path):
        """Lower priority number always wins, regardless of source."""
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            _insert_topic(conn, "t_rerun",  "ch1", "rerun",  "ep_01", 5, _iso(10))
            _insert_topic(conn, "t_manual", "ch1", "manual", None,    0, _iso(0))
            claimed = claim_next_episode_topic(conn, "ch1", _now())
            conn.commit()
        finally:
            conn.close()
        assert claimed["topic_id"] == "t_manual"

    def test_newest_rerun_wins_over_stale_rerun(self, tmp_path):
        """
        Two reruns at the same priority: the NEWER one (created_at DESC) wins.
        This prevents stale older reruns for a different episode from being claimed.
        """
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            # Older rerun = wrong episode (simulates the production bug)
            _insert_topic(conn, "t_old_ep", "ch1", "rerun", "ep_WRONG",  -1, _iso(0))
            # Newer rerun = correct episode
            _insert_topic(conn, "t_new_ep", "ch1", "rerun", "ep_CORRECT", -1, _iso(600))
            claimed = claim_next_episode_topic(conn, "ch1", _now())
            conn.commit()
        finally:
            conn.close()
        assert claimed["topic_id"] == "t_new_ep"
        assert claimed["episode_id"] == "ep_CORRECT"

    def test_newest_rerun_wins_over_many_stale(self, tmp_path):
        """8 stale reruns + 1 fresh correct rerun → fresh one is claimed."""
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            for i in range(8):
                _insert_topic(conn, f"t_stale_{i}", "ch1", "rerun", "ep_WRONG",
                              -1, _iso(i * 60))
            _insert_topic(conn, "t_fresh", "ch1", "rerun", "ep_CORRECT",
                          -1, _iso(9 * 60))
            claimed = claim_next_episode_topic(conn, "ch1", _now())
            conn.commit()
        finally:
            conn.close()
        assert claimed["topic_id"] == "t_fresh"
        assert claimed["episode_id"] == "ep_CORRECT"

    def test_no_double_claim(self, tmp_path):
        """Two consecutive claims return different rows."""
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            _insert_topic(conn, "t1", "ch1", "rerun", "ep_01", -1, _iso(0))
            _insert_topic(conn, "t2", "ch1", "rerun", "ep_02", -1, _iso(60))
            c1 = claim_next_episode_topic(conn, "ch1", _now())
            conn.commit()
            c2 = claim_next_episode_topic(conn, "ch1", _now())
            conn.commit()
        finally:
            conn.close()
        assert c1 is not None
        assert c2 is not None
        assert c1["topic_id"] != c2["topic_id"]


# ---------------------------------------------------------------------------
# TestClaimOrderRunQueue
# ---------------------------------------------------------------------------

class TestClaimOrderRunQueue:
    """claim_next_queue_item: rerun before schedule, newest rerun first."""

    def test_rerun_claimed_before_schedule_same_priority(self, tmp_path):
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            _insert_queue(conn, "q_sched",  "ch1", None,   "schedule", -10, _iso(0))
            _insert_queue(conn, "q_rerun",  "ch1", "ep_01", "rerun",   -10, _iso(5))
            claimed = claim_next_queue_item(conn, "worker_1", _now())
            conn.commit()
        finally:
            conn.close()
        assert claimed["queue_id"] == "q_rerun"

    def test_newest_rerun_wins(self, tmp_path):
        """Older rerun (wrong episode) vs newer rerun (correct) → newer wins."""
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            _insert_queue(conn, "q_old", "ch1", "ep_WRONG",   "rerun", -10, _iso(0))
            _insert_queue(conn, "q_new", "ch1", "ep_CORRECT", "rerun", -10, _iso(600))
            claimed = claim_next_queue_item(conn, "worker_1", _now())
            conn.commit()
        finally:
            conn.close()
        assert claimed["queue_id"] == "q_new"
        assert claimed["episode_id"] == "ep_CORRECT"

    def test_claimed_row_not_reclaimable(self, tmp_path):
        """A claimed row cannot be claimed a second time."""
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            _insert_queue(conn, "q1", "ch1", "ep_01", "rerun", -10, _iso(0))
            c1 = claim_next_queue_item(conn, "worker_1", _now())
            conn.commit()
            c2 = claim_next_queue_item(conn, "worker_2", _now())
            conn.commit()
        finally:
            conn.close()
        assert c1 is not None
        assert c2 is None  # no more queued items

    def test_source_and_recovery_stored(self, tmp_path):
        """source and recovery_from_run_id are stored and returned on claim."""
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            _insert_queue(conn, "q1", "ch1", "ep_01", "rerun", -10, _iso(0),
                          recovery_from_run_id="run_abc123")
            claimed = claim_next_queue_item(conn, "worker_1", _now())
            conn.commit()
        finally:
            conn.close()
        assert claimed["source"] == "rerun"
        assert claimed["recovery_from_run_id"] == "run_abc123"
        assert claimed["episode_id"] == "ep_01"


# ---------------------------------------------------------------------------
# TestRerunCancelsStale
# ---------------------------------------------------------------------------

class TestRerunCancelsStale:
    """cancel_queued_topics_for_episode and cancel_queued_run_queue_items_for_episode."""

    def test_cancels_queued_topics_for_episode(self, tmp_path):
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            _insert_topic(conn, "t1", "ch1", "rerun", "ep_01", -1, _iso(0))
            _insert_topic(conn, "t2", "ch1", "rerun", "ep_01", -1, _iso(60))
            _insert_topic(conn, "t3", "ch1", "rerun", "ep_02", -1, _iso(0))  # different ep
            n = cancel_queued_topics_for_episode(conn, "ep_01", _now())
            conn.commit()
            rows = conn.execute(
                "SELECT topic_id, status FROM episode_topics"
            ).fetchall()
        finally:
            conn.close()
        status = {r[0]: r[1] for r in rows}
        assert n == 2
        assert status["t1"] == "cancelled"
        assert status["t2"] == "cancelled"
        assert status["t3"] == "queued"   # different episode untouched

    def test_does_not_cancel_claimed_topics(self, tmp_path):
        """Claimed topics are owned by an active worker and must not be touched."""
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            _insert_topic(conn, "t_claimed", "ch1", "rerun", "ep_01", -1, _iso(0),
                          status="claimed")
            _insert_topic(conn, "t_queued",  "ch1", "rerun", "ep_01", -1, _iso(60))
            n = cancel_queued_topics_for_episode(conn, "ep_01", _now())
            conn.commit()
            rows = conn.execute(
                "SELECT topic_id, status FROM episode_topics"
            ).fetchall()
        finally:
            conn.close()
        status = {r[0]: r[1] for r in rows}
        assert n == 1                         # only the queued one
        assert status["t_claimed"] == "claimed"  # untouched
        assert status["t_queued"]  == "cancelled"

    def test_cancels_queued_run_queue_for_episode(self, tmp_path):
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            _insert_queue(conn, "q1", "ch1", "ep_01", "rerun", -10, _iso(0))
            _insert_queue(conn, "q2", "ch1", "ep_01", "rerun", -10, _iso(60))
            _insert_queue(conn, "q3", "ch1", "ep_02", "rerun", -10, _iso(0))
            n = cancel_queued_run_queue_items_for_episode(conn, "ep_01", _now())
            conn.commit()
            rows = conn.execute(
                "SELECT queue_id, status FROM run_queue"
            ).fetchall()
        finally:
            conn.close()
        status = {r[0]: r[1] for r in rows}
        assert n == 2
        assert status["q1"] == "cancelled"
        assert status["q2"] == "cancelled"
        assert status["q3"] == "queued"

    def test_does_not_cancel_claimed_run_queue(self, tmp_path):
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            _insert_queue(conn, "q_claimed", "ch1", "ep_01", "rerun", -10, _iso(0),
                          status="claimed")
            _insert_queue(conn, "q_queued",  "ch1", "ep_01", "rerun", -10, _iso(60))
            n = cancel_queued_run_queue_items_for_episode(conn, "ep_01", _now())
            conn.commit()
            rows = conn.execute(
                "SELECT queue_id, status FROM run_queue"
            ).fetchall()
        finally:
            conn.close()
        status = {r[0]: r[1] for r in rows}
        assert n == 1
        assert status["q_claimed"] == "claimed"
        assert status["q_queued"]  == "cancelled"


# ---------------------------------------------------------------------------
# TestRunQueueSchema
# ---------------------------------------------------------------------------

class TestRunQueueSchema:
    """enqueue() stores source and recovery_from_run_id correctly."""

    def test_enqueue_stores_source_and_recovery(self, tmp_path):
        db = _db(tmp_path)
        qid = enqueue(
            channel_id="hypogeum",
            episode_id="ep_01",
            priority=-10,
            source="rerun",
            recovery_from_run_id="run_deadbeef",
            db_path=db,
        )
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT source, recovery_from_run_id, episode_id FROM run_queue WHERE queue_id = ?",
                (qid,),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "rerun"
        assert row[1] == "run_deadbeef"
        assert row[2] == "ep_01"

    def test_enqueue_without_source_stores_null(self, tmp_path):
        db = _db(tmp_path)
        qid = enqueue(channel_id="hypogeum", priority=0, db_path=db)
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT source, recovery_from_run_id FROM run_queue WHERE queue_id = ?",
                (qid,),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] is None
        assert row[1] is None


# ---------------------------------------------------------------------------
# TestWorkerPicksNewRerun
# ---------------------------------------------------------------------------

class TestWorkerPicksNewRerun:
    """
    End-to-end scenario: old queued run exists + new rerun exists
    → worker's claim picks the new rerun, not the old queued item.
    """

    def test_old_queued_plus_new_rerun_worker_picks_rerun(self, tmp_path):
        """
        Old manual topic (priority 0, created earlier) exists for a different run.
        New rerun topic (priority -1, created later) is inserted.
        Worker must claim the rerun topic.
        """
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            # Old normal topic for a different run (no episode_id, lower urgency)
            _insert_topic(conn, "t_old_manual", "hypogeum", "manual", None, 0, _iso(0))
            # New rerun for the correct episode
            _insert_topic(conn, "t_rerun", "hypogeum", "rerun", "ep_c7abfd", -1, _iso(3600))
            claimed = claim_next_episode_topic(conn, "hypogeum", _now())
            conn.commit()
        finally:
            conn.close()
        assert claimed["topic_id"] == "t_rerun"
        assert claimed["episode_id"] == "ep_c7abfd"

    def test_wrong_episode_rerun_plus_correct_rerun_worker_picks_correct(self, tmp_path):
        """
        Stale rerun for ep_WRONG (created earlier) + fresh rerun for ep_CORRECT.
        Worker must claim ep_CORRECT.
        """
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            _insert_topic(conn, "t_wrong",   "hypogeum", "rerun", "ep_WRONG",   -1, _iso(0))
            _insert_topic(conn, "t_correct", "hypogeum", "rerun", "ep_CORRECT", -1, _iso(600))
            claimed = claim_next_episode_topic(conn, "hypogeum", _now())
            conn.commit()
        finally:
            conn.close()
        assert claimed["episode_id"] == "ep_CORRECT"

    def test_episode_id_flows_through_to_claim(self, tmp_path):
        """episode_id set on the rerun topic is accessible on the claimed row."""
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            _insert_topic(conn, "t1", "ch1", "rerun", "ep_20260629075717_c7abfd", -1, _iso(0))
            claimed = claim_next_episode_topic(conn, "ch1", _now())
            conn.commit()
        finally:
            conn.close()
        assert claimed["episode_id"] == "ep_20260629075717_c7abfd"
        assert claimed["source"] == "rerun"

    def test_claimed_run_cannot_be_reclaimed(self, tmp_path):
        """A run_queue item already claimed by a worker cannot be claimed again."""
        db = _db(tmp_path)
        conn = get_connection(db)
        try:
            _insert_queue(conn, "q1", "ch1", "ep_01", "rerun", -10, _iso(0))
            c1 = claim_next_queue_item(conn, "worker_A", _now())
            conn.commit()
            c2 = claim_next_queue_item(conn, "worker_B", _now())
            conn.commit()
        finally:
            conn.close()
        assert c1 is not None
        assert c2 is None   # no unclaimed items remain
