"""
tests/test_publication_record_reconciliation.py — Stale historical
upload-attempt record reconciliation.

Covers hypogeum.publishing.publication_record_reconciliation.
reconcile_stale_publishing_records_for_episode(): reproduces the real
ep_f3619022 shape (4 publishing_outcomes rows at publishing_status=
'pending' + 4 publish_calendar rows at publish_status='scheduled', per
the operator-confirmed correction that those uploads were deleted from
YouTube), dry-run preview vs. apply, idempotency, channel isolation, and
that a CONFIRMED live/success row is never touched.

All state uses an isolated tmp_path SQLite database — never the production
.hypogeum-assets/hypogeum.db.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hypogeum.db import get_connection, initialize_schema, insert_episode, insert_episode_topic
from hypogeum.publishing.publication_record_reconciliation import (
    reconcile_stale_publishing_records_for_episode,
)

CHANNEL_ID = "hypogeum"
EPISODE_ID = "ep_f3619022"


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "record_reconciliation_test.db"
    initialize_schema(p)
    return p


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _attribute_episode(db_path, episode_id=EPISODE_ID, channel_id=CHANNEL_ID):
    conn = get_connection(db_path)
    try:
        insert_episode_topic(
            conn, topic_id=f"topic_{episode_id}", channel_id=channel_id, topic_text="A topic",
            created_at=_now(), updated_at=_now(), episode_id=episode_id,
        )
        conn.commit()
    finally:
        conn.close()


def _seed_real_ep_f3619022_shape(db_path, episode_id=EPISODE_ID):
    """Reproduces the exact real production shape: 4 publishing_outcomes
    rows (main + 3 shorts) at publishing_status='pending' with real
    platform_video_ids, and 4 matching publish_calendar rows at
    publish_status='scheduled'."""
    conn = get_connection(db_path)
    try:
        outcomes = [
            (f"out_main_{episode_id}", "main_episode", "heCS91MkfdE"),
            (f"out_s1_{episode_id}", "short_short_001", "ItY0SXWUNeA"),
            (f"out_s2_{episode_id}", "short_short_002", "CHPhES0lALA"),
            (f"out_s3_{episode_id}", "short_short_003", "tnb4cyWcDZU"),
        ]
        for outcome_id, deliverable_id, video_id in outcomes:
            conn.execute(
                """
                INSERT INTO publishing_outcomes
                    (outcome_id, episode_id, platform, deliverable_id, publishing_status,
                     platform_video_id, published_at, url, recorded_at)
                VALUES (?, ?, 'youtube', ?, 'pending', ?, ?, ?, ?)
                """,
                (outcome_id, episode_id, deliverable_id, video_id, _now(),
                 f"https://www.youtube.com/watch?v={video_id}", _now()),
            )
        calendars = [
            (f"cal_main_{episode_id}", None, "heCS91MkfdE"),
            (f"cal_s1_{episode_id}", "short_001", "ItY0SXWUNeA"),
            (f"cal_s2_{episode_id}", "short_002", "CHPhES0lALA"),
            (f"cal_s3_{episode_id}", "short_003", "tnb4cyWcDZU"),
        ]
        for calendar_id, short_id, video_id in calendars:
            conn.execute(
                """
                INSERT INTO publish_calendar
                    (calendar_id, episode_id, short_id, platform, target_publish_at,
                     upload_status, publish_status, platform_video_id, created_at, updated_at)
                VALUES (?, ?, ?, 'youtube', ?, 'uploaded', 'scheduled', ?, ?, ?)
                """,
                (calendar_id, episode_id, short_id, _now(), video_id, _now(), _now()),
            )
        conn.commit()
    finally:
        conn.close()


class TestEligibility:
    def test_not_eligible_when_channel_id_missing(self, db_path):
        _attribute_episode(db_path)
        _seed_real_ep_f3619022_shape(db_path)
        result = reconcile_stale_publishing_records_for_episode(EPISODE_ID, "", db_path=db_path, dry_run=False)
        assert result.eligible is False
        assert result.applied_count == 0

    def test_not_eligible_when_channel_mismatched(self, db_path):
        _attribute_episode(db_path, channel_id="channel_a")
        _seed_real_ep_f3619022_shape(db_path)
        result = reconcile_stale_publishing_records_for_episode(EPISODE_ID, "channel_b", db_path=db_path, dry_run=False)
        assert result.eligible is False
        assert "channel_a" in result.reason
        assert "channel_b" in result.reason
        assert result.applied_count == 0

        conn = get_connection(db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM publishing_outcomes WHERE episode_id = ? AND publishing_status = 'pending'",
                (EPISODE_ID,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 4, "wrong-channel assertion must never mutate another channel's rows"


class TestReconciliation:
    def test_dry_run_lists_all_eight_without_mutating(self, db_path):
        _attribute_episode(db_path)
        _seed_real_ep_f3619022_shape(db_path)
        result = reconcile_stale_publishing_records_for_episode(EPISODE_ID, CHANNEL_ID, db_path=db_path, dry_run=True)

        assert result.eligible is True
        assert result.dry_run is True
        assert result.applied_count == 0
        assert len(result.rows) == 8
        assert {r.table for r in result.rows} == {"publishing_outcomes", "publish_calendar"}
        assert {r.platform_video_id for r in result.rows} == {"heCS91MkfdE", "ItY0SXWUNeA", "CHPhES0lALA", "tnb4cyWcDZU"}

        conn = get_connection(db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM publishing_outcomes WHERE episode_id = ? AND publishing_status = 'pending'",
                (EPISODE_ID,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 4, "dry_run must never mutate any row"

    def test_apply_reconciles_all_eight_preserving_video_ids(self, db_path):
        _attribute_episode(db_path)
        _seed_real_ep_f3619022_shape(db_path)
        result = reconcile_stale_publishing_records_for_episode(EPISODE_ID, CHANNEL_ID, db_path=db_path, dry_run=False)

        assert result.eligible is True
        assert result.applied_count == 8

        conn = get_connection(db_path)
        try:
            outcomes = conn.execute(
                "SELECT outcome_id, publishing_status, platform_video_id, failure_reason FROM publishing_outcomes WHERE episode_id = ?",
                (EPISODE_ID,),
            ).fetchall()
            calendars = conn.execute(
                "SELECT calendar_id, publish_status, platform_video_id FROM publish_calendar WHERE episode_id = ?",
                (EPISODE_ID,),
            ).fetchall()
        finally:
            conn.close()
        assert all(r["publishing_status"] == "skipped" for r in outcomes)
        assert all(r["platform_video_id"] for r in outcomes), "video IDs must be preserved, not erased"
        assert all("operator_confirmed_stale_reconciliation" in (r["failure_reason"] or "") for r in outcomes)
        assert all(r["publish_status"] == "skipped" for r in calendars)
        assert all(r["platform_video_id"] for r in calendars)

    def test_idempotent_second_apply_is_a_safe_noop(self, db_path):
        _attribute_episode(db_path)
        _seed_real_ep_f3619022_shape(db_path)
        first = reconcile_stale_publishing_records_for_episode(EPISODE_ID, CHANNEL_ID, db_path=db_path, dry_run=False)
        second = reconcile_stale_publishing_records_for_episode(EPISODE_ID, CHANNEL_ID, db_path=db_path, dry_run=False)

        assert first.applied_count == 8
        assert second.applied_count == 0
        assert second.eligible is True
        assert "nothing to reconcile" in second.reason

    def test_confirmed_success_or_live_row_is_never_touched(self, db_path):
        _attribute_episode(db_path)
        conn = get_connection(db_path)
        try:
            conn.execute(
                """
                INSERT INTO publishing_outcomes
                    (outcome_id, episode_id, platform, deliverable_id, publishing_status,
                     platform_video_id, recorded_at)
                VALUES ('out_live', ?, 'youtube', 'main_episode', 'success', 'REAL_LIVE_ID', ?)
                """,
                (EPISODE_ID, _now()),
            )
            conn.execute(
                """
                INSERT INTO publish_calendar
                    (calendar_id, episode_id, platform, target_publish_at, publish_status,
                     platform_video_id, created_at, updated_at)
                VALUES ('cal_live', ?, 'youtube', ?, 'live', 'REAL_LIVE_ID', ?, ?)
                """,
                (EPISODE_ID, _now(), _now(), _now()),
            )
            conn.commit()
        finally:
            conn.close()

        result = reconcile_stale_publishing_records_for_episode(EPISODE_ID, CHANNEL_ID, db_path=db_path, dry_run=False)
        assert result.applied_count == 0
        assert "nothing to reconcile" in result.reason

        conn = get_connection(db_path)
        try:
            outcome = conn.execute(
                "SELECT publishing_status FROM publishing_outcomes WHERE outcome_id = 'out_live'"
            ).fetchone()
            calendar = conn.execute(
                "SELECT publish_status FROM publish_calendar WHERE calendar_id = 'cal_live'"
            ).fetchone()
        finally:
            conn.close()
        assert outcome["publishing_status"] == "success"
        assert calendar["publish_status"] == "live"

    def test_reconciling_one_episode_never_touches_another(self, db_path):
        _attribute_episode(db_path, episode_id="ep_a", channel_id=CHANNEL_ID)
        _seed_real_ep_f3619022_shape(db_path, episode_id="ep_a")
        _attribute_episode(db_path, episode_id="ep_b", channel_id=CHANNEL_ID)
        _seed_real_ep_f3619022_shape(db_path, episode_id="ep_b")

        reconcile_stale_publishing_records_for_episode("ep_a", CHANNEL_ID, db_path=db_path, dry_run=False)

        conn = get_connection(db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM publishing_outcomes WHERE episode_id = 'ep_b' AND publishing_status = 'pending'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 4
