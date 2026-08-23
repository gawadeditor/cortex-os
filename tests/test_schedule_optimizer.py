"""
tests/test_schedule_optimizer.py — hypogeum.schedule_optimizer

Evidence-based publication-slot optimizer. Covers:
  TestNoPolicy            — policy None/disabled short-circuits to fallback
  TestNoEvidence          — zero real rows -> fallback, never invents a time
  TestInsufficientEvidence — below min_sample_size/min_confidence -> fallback
  TestSufficientEvidence  — real analytics rows -> analytics-mode slot
  TestChannelIsolation    — evidence from another channel never leaks in
  TestDeliverableClassIsolation — main_episode vs shorts evidence never mixed
  TestSourceFilter        — only source='api' rows count as evidence (F8)
  TestAllowedDaysHours    — policy-constrained buckets are respected
  TestInvalidTimezone     — blocked, never crashes
"""

from __future__ import annotations

import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.schedule_optimizer import optimize_publication_slot

_NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
_FALLBACK_SCHEDULE = {"day_of_week": "thursday", "time_of_day": "18:00"}


def _seed_evidence(
    db_path, *, channel_id="ch1", platform="youtube", deliverable_id="main_episode",
    n=6, weekday=1, hour=15, avg_view_pct=0.8, source="api", status="success",
    views=None,
):
    conn = get_connection(db_path)
    for i in range(n):
        pub_at = _NOW - timedelta(days=7 * i)
        while pub_at.weekday() != weekday:
            pub_at -= timedelta(days=1)
        pub_at = pub_at.replace(hour=hour, minute=0, second=0, microsecond=0)
        eid = f"ep_{channel_id}_{platform}_{deliverable_id}_{i}"
        conn.execute(
            "INSERT INTO run_queue (queue_id, channel_id, episode_id, priority, status, queued_at, created_at) "
            "VALUES (?,?,?,0,'completed',?,?)",
            (f"q_{eid}", channel_id, eid, _NOW.isoformat(), _NOW.isoformat()),
        )
        conn.execute(
            "INSERT INTO publishing_outcomes (outcome_id, episode_id, platform, deliverable_id, "
            "publishing_status, published_at, recorded_at) VALUES (?,?,?,?,?,?,?)",
            (f"po_{eid}", eid, platform, deliverable_id, status, pub_at.isoformat(), _NOW.isoformat()),
        )
        conn.execute(
            "INSERT INTO platform_analytics (record_id, episode_id, platform, snapshot_date, "
            "avg_view_pct, views, source, imported_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"pa_{eid}", eid, platform, _NOW.date().isoformat(), avg_view_pct, views, source, _NOW.isoformat()),
        )
    conn.commit()
    conn.close()


class TestNoPolicy:
    def test_policy_none_short_circuits_to_fallback(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        r = optimize_publication_slot(
            "ch1", "main_episode", "youtube", db_path=db_path, now=_NOW,
            fallback_schedule=_FALLBACK_SCHEDULE, fallback_timezone="UTC", policy=None,
        )
        assert r.selection_mode == "fallback"
        assert r.selected_publication_timestamp is not None
        assert r.selected_publication_timestamp.weekday() == 3  # thursday

    def test_policy_explicitly_disabled(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _seed_evidence(db_path, n=10)
        r = optimize_publication_slot(
            "ch1", "main_episode", "youtube", db_path=db_path, now=_NOW,
            fallback_schedule=_FALLBACK_SCHEDULE, fallback_timezone="UTC",
            policy={"enabled": False},
        )
        assert r.selection_mode == "fallback"
        assert r.sample_count == 0


class TestNoEvidence:
    def test_zero_rows_never_invents_a_time(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        r = optimize_publication_slot(
            "ch1", "main_episode", "youtube", db_path=db_path, now=_NOW,
            fallback_schedule=_FALLBACK_SCHEDULE, fallback_timezone="UTC",
            policy={"enabled": True, "min_sample_size": 3},
        )
        assert r.selection_mode == "fallback"
        assert "no real" in r.reason


class TestInsufficientEvidence:
    def test_below_min_sample_size_falls_back(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _seed_evidence(db_path, n=2)
        r = optimize_publication_slot(
            "ch1", "main_episode", "youtube", db_path=db_path, now=_NOW,
            fallback_schedule=_FALLBACK_SCHEDULE, fallback_timezone="UTC",
            policy={"enabled": True, "min_sample_size": 5, "min_confidence": 0.6},
        )
        assert r.selection_mode == "fallback"
        assert r.sample_count == 2
        assert "insufficient evidence" in r.reason


class TestSufficientEvidence:
    def test_real_evidence_selects_analytics_slot(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _seed_evidence(db_path, n=6, weekday=1, hour=15, avg_view_pct=0.8)
        r = optimize_publication_slot(
            "ch1", "main_episode", "youtube", db_path=db_path, now=_NOW,
            fallback_schedule=_FALLBACK_SCHEDULE, fallback_timezone="UTC",
            policy={"enabled": True, "min_sample_size": 3, "min_confidence": 0.5},
        )
        assert r.selection_mode == "analytics"
        assert r.sample_count == 6
        assert r.confidence_score == 1.0
        assert r.selected_publication_timestamp.weekday() == 1  # tuesday
        assert r.selected_publication_timestamp.hour == 15
        assert r.selected_publication_timestamp > _NOW

    def test_falls_back_to_view_count_when_avg_view_pct_null(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _seed_evidence(db_path, n=5, weekday=2, hour=10, avg_view_pct=None, views=5000)
        r = optimize_publication_slot(
            "ch1", "main_episode", "youtube", db_path=db_path, now=_NOW,
            fallback_schedule=_FALLBACK_SCHEDULE, fallback_timezone="UTC",
            policy={"enabled": True, "min_sample_size": 3, "min_confidence": 0.5},
        )
        assert r.selection_mode == "analytics"
        assert r.selected_publication_timestamp.weekday() == 2


class TestChannelIsolation:
    def test_other_channel_evidence_never_leaks_in(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _seed_evidence(db_path, channel_id="ch2", n=10)  # different channel
        r = optimize_publication_slot(
            "ch1", "main_episode", "youtube", db_path=db_path, now=_NOW,
            fallback_schedule=_FALLBACK_SCHEDULE, fallback_timezone="UTC",
            policy={"enabled": True, "min_sample_size": 3},
        )
        assert r.selection_mode == "fallback"
        assert r.sample_count == 0


class TestDeliverableClassIsolation:
    def test_shorts_evidence_never_counts_for_main_episode(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _seed_evidence(db_path, deliverable_id="short_001", n=10)
        r = optimize_publication_slot(
            "ch1", "main_episode", "youtube", db_path=db_path, now=_NOW,
            fallback_schedule=_FALLBACK_SCHEDULE, fallback_timezone="UTC",
            policy={"enabled": True, "min_sample_size": 3},
        )
        assert r.selection_mode == "fallback"
        assert r.sample_count == 0

        r2 = optimize_publication_slot(
            "ch1", "shorts", "youtube", db_path=db_path, now=_NOW,
            fallback_schedule=_FALLBACK_SCHEDULE, fallback_timezone="UTC",
            policy={"enabled": True, "min_sample_size": 3, "min_confidence": 0.5},
        )
        assert r2.selection_mode == "analytics"
        assert r2.sample_count == 10


class TestSourceFilter:
    def test_mock_source_rows_never_count_as_evidence(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _seed_evidence(db_path, n=10, source="mock")
        r = optimize_publication_slot(
            "ch1", "main_episode", "youtube", db_path=db_path, now=_NOW,
            fallback_schedule=_FALLBACK_SCHEDULE, fallback_timezone="UTC",
            policy={"enabled": True, "min_sample_size": 3},
        )
        assert r.selection_mode == "fallback"
        assert r.sample_count == 0

    def test_failed_publish_rows_never_count_as_evidence(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _seed_evidence(db_path, n=10, status="failed")
        r = optimize_publication_slot(
            "ch1", "main_episode", "youtube", db_path=db_path, now=_NOW,
            fallback_schedule=_FALLBACK_SCHEDULE, fallback_timezone="UTC",
            policy={"enabled": True, "min_sample_size": 3},
        )
        assert r.selection_mode == "fallback"
        assert r.sample_count == 0


class TestAllowedDaysHours:
    def test_allowed_days_excludes_the_best_bucket_when_configured(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        # Best evidence lands on Tuesday, but policy only allows Friday --
        # with zero Friday evidence, must fall back rather than pick Tuesday.
        _seed_evidence(db_path, n=6, weekday=1, hour=15, avg_view_pct=0.9)
        r = optimize_publication_slot(
            "ch1", "main_episode", "youtube", db_path=db_path, now=_NOW,
            fallback_schedule=_FALLBACK_SCHEDULE, fallback_timezone="UTC",
            policy={"enabled": True, "min_sample_size": 3, "min_confidence": 0.5,
                    "allowed_days": ["friday"]},
        )
        assert r.selection_mode == "fallback"
        assert r.sample_count == 0


class TestInvalidTimezone:
    def test_invalid_fallback_timezone_blocks_never_crashes(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        r = optimize_publication_slot(
            "ch1", "main_episode", "youtube", db_path=db_path, now=_NOW,
            fallback_schedule=_FALLBACK_SCHEDULE, fallback_timezone="Not/A_Real_Zone",
            policy={"enabled": True, "min_sample_size": 3},
        )
        assert r.selection_mode == "blocked"
        assert r.selected_publication_timestamp is None
