"""
tests/test_topic_reconciliation.py — Channel-scoped stale queued
episode_topics closure.

Covers hypogeum.topics.reconcile_stale_topics_for_episode(): eligibility
(only episodes.status='complete'), the mandatory channel_id isolation
invariant (required argument; every query scoped by BOTH episode_id AND
channel_id; a wrong channel_id assertion reconciles nothing, ever), dry-run
preview vs. apply, idempotency, scoping to exactly one episode_id, and the
root-cause hook wired into hypogeum.run_registry._sync_episode_row()
(called automatically by complete_run(), which now resolves channel_id
itself and skips the call entirely if it cannot be resolved unambiguously).

Reproduces the real 8-row ep_f3619022 shape (7 auto_retry + 1 manual, all
'queued') using an isolated tmp_path database only — never the production
.hypogeum-assets/hypogeum.db.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hypogeum.db import get_connection, initialize_schema, insert_episode, insert_episode_topic, insert_run
from hypogeum.topics import reconcile_stale_topics_for_episode

CHANNEL_ID = "hypogeum"


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "reconciliation_test.db"
    initialize_schema(p)
    return p


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_topic(db_path, *, episode_id, source, priority, status="queued", channel_id=CHANNEL_ID):
    conn = get_connection(db_path)
    try:
        topic_id = str(uuid.uuid4())
        now = _now()
        insert_episode_topic(
            conn, topic_id=topic_id, channel_id=channel_id, topic_text=f"topic for {episode_id}",
            created_at=now, updated_at=now, priority=priority, source=source, episode_id=episode_id,
        )
        if status != "queued":
            conn.execute("UPDATE episode_topics SET status = ? WHERE topic_id = ?", (status, topic_id))
        conn.commit()
        return topic_id
    finally:
        conn.close()


def _insert_episode(db_path, episode_id, status):
    conn = get_connection(db_path)
    try:
        insert_episode(conn, episode_id=episode_id, created_at=_now(), topic="Some topic", status=status)
        conn.commit()
    finally:
        conn.close()


def _seed_eight_stale_rows(db_path, episode_id="ep_f3619022", channel_id=CHANNEL_ID):
    ids = []
    ids.append(_insert_topic(db_path, episode_id=episode_id, source="manual", priority=9, channel_id=channel_id))
    for _ in range(7):
        ids.append(_insert_topic(db_path, episode_id=episode_id, source="auto_retry", priority=0, channel_id=channel_id))
    return ids


class TestEligibility:
    def test_not_eligible_when_episode_unknown(self, db_path):
        result = reconcile_stale_topics_for_episode("ep_missing", CHANNEL_ID, db_path=db_path, dry_run=True)
        assert result.eligible is False
        assert result.episode_status is None
        assert result.applied_count == 0
        assert result.rows == []

    def test_not_eligible_when_episode_in_progress(self, db_path):
        _insert_episode(db_path, "ep_x", "in_progress")
        _seed_eight_stale_rows(db_path, "ep_x")
        result = reconcile_stale_topics_for_episode("ep_x", CHANNEL_ID, db_path=db_path, dry_run=False)
        assert result.eligible is False
        assert result.applied_count == 0
        # No row was touched — still queued.
        conn = get_connection(db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM episode_topics WHERE episode_id = ? AND status = 'queued'", ("ep_x",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 8

    def test_not_eligible_when_episode_failed(self, db_path):
        _insert_episode(db_path, "ep_y", "failed")
        _seed_eight_stale_rows(db_path, "ep_y")
        result = reconcile_stale_topics_for_episode("ep_y", CHANNEL_ID, db_path=db_path, dry_run=False)
        assert result.eligible is False
        assert result.applied_count == 0

    def test_not_eligible_when_channel_id_missing(self, db_path):
        _insert_episode(db_path, "ep_f3619022", "complete")
        _seed_eight_stale_rows(db_path)
        result = reconcile_stale_topics_for_episode("ep_f3619022", "", db_path=db_path, dry_run=False)
        assert result.eligible is False
        assert result.applied_count == 0
        assert "channel_id is required" in result.reason


class TestChannelIsolation:
    """A ready item / stale row belonging to one channel must never be
    mutated by a reconciliation call asserting a different channel_id."""

    def test_wrong_channel_id_reconciles_nothing(self, db_path):
        _insert_episode(db_path, "ep_f3619022", "complete")
        _seed_eight_stale_rows(db_path, channel_id="channel_a")

        result = reconcile_stale_topics_for_episode("ep_f3619022", "channel_b", db_path=db_path, dry_run=False)

        assert result.eligible is False
        assert "channel_a" in result.reason
        assert "channel_b" in result.reason
        assert result.applied_count == 0

        conn = get_connection(db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM episode_topics WHERE episode_id = ? AND status = 'queued'",
                ("ep_f3619022",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 8, "a mismatched channel_id assertion must never mutate another channel's rows"

    def test_correct_channel_id_reconciles_normally(self, db_path):
        _insert_episode(db_path, "ep_f3619022", "complete")
        _seed_eight_stale_rows(db_path, channel_id="channel_a")

        result = reconcile_stale_topics_for_episode("ep_f3619022", "channel_a", db_path=db_path, dry_run=False)
        assert result.eligible is True
        assert result.applied_count == 8

    def test_reconciling_channel_a_episode_never_touches_channel_b_episode(self, db_path):
        _insert_episode(db_path, "ep_a", "complete")
        _seed_eight_stale_rows(db_path, "ep_a", channel_id="channel_a")
        _insert_episode(db_path, "ep_b", "complete")
        _seed_eight_stale_rows(db_path, "ep_b", channel_id="channel_b")

        result_a = reconcile_stale_topics_for_episode("ep_a", "channel_a", db_path=db_path, dry_run=False)
        assert result_a.applied_count == 8

        conn = get_connection(db_path)
        try:
            b_still_queued = conn.execute(
                "SELECT COUNT(*) FROM episode_topics WHERE episode_id = ? AND status = 'queued'", ("ep_b",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert b_still_queued == 8, "reconciling channel A's episode must never mutate channel B's rows"

        result_b = reconcile_stale_topics_for_episode("ep_b", "channel_b", db_path=db_path, dry_run=False)
        assert result_b.applied_count == 8


class TestEightRowReconciliation:
    def test_dry_run_preview_lists_all_eight_without_mutating(self, db_path):
        _insert_episode(db_path, "ep_f3619022", "complete")
        _seed_eight_stale_rows(db_path)

        result = reconcile_stale_topics_for_episode("ep_f3619022", CHANNEL_ID, db_path=db_path, dry_run=True)

        assert result.eligible is True
        assert result.dry_run is True
        assert result.applied_count == 0
        assert len(result.rows) == 8
        assert sorted(r.source for r in result.rows) == sorted(["manual"] + ["auto_retry"] * 7)

        conn = get_connection(db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM episode_topics WHERE episode_id = ? AND status = 'queued'",
                ("ep_f3619022",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 8, "dry_run must never mutate any row"

    def test_apply_reconciles_all_eight(self, db_path):
        _insert_episode(db_path, "ep_f3619022", "complete")
        _seed_eight_stale_rows(db_path)

        result = reconcile_stale_topics_for_episode("ep_f3619022", CHANNEL_ID, db_path=db_path, dry_run=False)

        assert result.eligible is True
        assert result.applied_count == 8

        conn = get_connection(db_path)
        try:
            remaining_queued = conn.execute(
                "SELECT COUNT(*) FROM episode_topics WHERE episode_id = ? AND status = 'queued'",
                ("ep_f3619022",),
            ).fetchone()[0]
            cancelled = conn.execute(
                "SELECT COUNT(*) FROM episode_topics WHERE episode_id = ? AND status = 'cancelled' "
                "AND reason = 'terminal_state_reconciliation'",
                ("ep_f3619022",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert remaining_queued == 0
        assert cancelled == 8

    def test_apply_never_touches_claimed_rows(self, db_path):
        _insert_episode(db_path, "ep_f3619022", "complete")
        _seed_eight_stale_rows(db_path)
        claimed_id = _insert_topic(db_path, episode_id="ep_f3619022", source="auto_retry", priority=0, status="claimed")

        result = reconcile_stale_topics_for_episode("ep_f3619022", CHANNEL_ID, db_path=db_path, dry_run=False)
        assert result.applied_count == 8

        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT status FROM episode_topics WHERE topic_id = ?", (claimed_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "claimed"

    def test_apply_never_touches_a_different_episode(self, db_path):
        _insert_episode(db_path, "ep_f3619022", "complete")
        _seed_eight_stale_rows(db_path)
        _insert_episode(db_path, "ep_other", "complete")
        other_id = _insert_topic(db_path, episode_id="ep_other", source="auto_retry", priority=0)

        reconcile_stale_topics_for_episode("ep_f3619022", CHANNEL_ID, db_path=db_path, dry_run=False)

        conn = get_connection(db_path)
        try:
            row = conn.execute("SELECT status FROM episode_topics WHERE topic_id = ?", (other_id,)).fetchone()
        finally:
            conn.close()
        assert row["status"] == "queued"


class TestIdempotency:
    def test_second_apply_is_a_safe_noop(self, db_path):
        _insert_episode(db_path, "ep_f3619022", "complete")
        _seed_eight_stale_rows(db_path)

        first = reconcile_stale_topics_for_episode("ep_f3619022", CHANNEL_ID, db_path=db_path, dry_run=False)
        second = reconcile_stale_topics_for_episode("ep_f3619022", CHANNEL_ID, db_path=db_path, dry_run=False)

        assert first.applied_count == 8
        assert second.applied_count == 0
        assert second.eligible is True
        assert "nothing to reconcile" in second.reason


class TestTerminalStateAutomaticClosure:
    """The root-cause hook: hypogeum.run_registry._sync_episode_row()
    resolves channel_id from episode_topics and calls
    reconcile_stale_topics_for_episode(dry_run=False) automatically right
    after it sets episodes.status='complete'; it skips the call entirely
    if channel_id cannot be resolved unambiguously."""

    def test_complete_run_closes_stale_topics_for_that_episode(self, db_path, monkeypatch, tmp_path):
        from hypogeum import run_registry

        episode_id = "ep_new"
        run_id = "run_new"
        _seed_eight_stale_rows(db_path, episode_id)

        conn = get_connection(db_path)
        try:
            insert_run(conn, run_id=run_id, episode_id=episode_id, started_at=_now())
            conn.commit()
        finally:
            conn.close()

        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        run_registry.complete_run(run_id, episode_id, db_path=db_path, runs_dir=runs_dir)

        conn = get_connection(db_path)
        try:
            ep_status = conn.execute(
                "SELECT status FROM episodes WHERE episode_id = ?", (episode_id,),
            ).fetchone()["status"]
            remaining_queued = conn.execute(
                "SELECT COUNT(*) FROM episode_topics WHERE episode_id = ? AND status = 'queued'",
                (episode_id,),
            ).fetchone()[0]
        finally:
            conn.close()

        assert ep_status == "complete"
        assert remaining_queued == 0

    def test_complete_run_skips_reconciliation_when_channel_id_ambiguous(self, db_path, tmp_path):
        """episode_topics rows attributing episode_id to TWO different
        channels (a real-world data problem, not something this function
        should ever guess through) -- complete_run() must still succeed,
        and must not reconcile anything under a guessed channel_id."""
        from hypogeum import run_registry

        episode_id = "ep_ambiguous"
        run_id = "run_ambiguous"
        _insert_topic(db_path, episode_id=episode_id, source="auto_retry", priority=0, channel_id="channel_a")
        _insert_topic(db_path, episode_id=episode_id, source="auto_retry", priority=0, channel_id="channel_b")

        conn = get_connection(db_path)
        try:
            insert_run(conn, run_id=run_id, episode_id=episode_id, started_at=_now())
            conn.commit()
        finally:
            conn.close()

        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        run_registry.complete_run(run_id, episode_id, db_path=db_path, runs_dir=runs_dir)

        conn = get_connection(db_path)
        try:
            ep_status = conn.execute(
                "SELECT status FROM episodes WHERE episode_id = ?", (episode_id,),
            ).fetchone()["status"]
            still_queued = conn.execute(
                "SELECT COUNT(*) FROM episode_topics WHERE episode_id = ? AND status = 'queued'",
                (episode_id,),
            ).fetchone()[0]
        finally:
            conn.close()

        assert ep_status == "complete"
        assert still_queued == 2, "ambiguous channel attribution must never be guessed through"

    def test_complete_run_never_raises_if_reconciliation_fails(self, monkeypatch, db_path, tmp_path):
        from hypogeum import run_registry

        episode_id = "ep_broken_reconcile"
        run_id = "run_broken"
        # Exactly one attributing row so channel_id resolves unambiguously
        # and the reconcile call is actually attempted (and then fails).
        _insert_topic(db_path, episode_id=episode_id, source="auto_retry", priority=0, channel_id=CHANNEL_ID)
        conn = get_connection(db_path)
        try:
            insert_run(conn, run_id=run_id, episode_id=episode_id, started_at=_now())
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setattr(
            "hypogeum.topics.reconcile_stale_topics_for_episode",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        # Must not raise — reconciliation failure is best-effort only.
        run_registry.complete_run(run_id, episode_id, db_path=db_path, runs_dir=runs_dir)

        conn = get_connection(db_path)
        try:
            ep_status = conn.execute(
                "SELECT status FROM episodes WHERE episode_id = ?", (episode_id,),
            ).fetchone()["status"]
        finally:
            conn.close()
        assert ep_status == "complete"
