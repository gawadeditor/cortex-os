"""
tests/test_evidence_based_scheduling.py —
hypogeum.publishing.evidence_based_scheduling

Refreshes ChannelOperationsProfile.deliverable_schedule_policy from real
evidence via hypogeum.schedule_optimizer, without any change to
hypogeum.publishing.schedule_compiler (which keeps reading that same
field exactly as before). Covers:
  TestNoOp              — missing profile / unconfigured policy is a no-op
  TestUpdatesOnEvidence  — real evidence rewrites the static entry
  TestLeavesUnchangedOnFallback — insufficient evidence never overwrites
  TestSnapshotWritten    — audit snapshot file is always written
  TestMultiPlatformGoverning — highest-confidence platform governs
"""

from __future__ import annotations

import json
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.channel_config import (
    create_channel_operations_profile,
    create_channel_profile,
    get_channel_operations_profile,
)
from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing.evidence_based_scheduling import refresh_analytics_scheduling_policy

_NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def _make_channel(db_path, channel_id="ch1", **profile_kwargs):
    create_channel_profile(channel_id=channel_id, name=channel_id, db_path=db_path)
    return create_channel_operations_profile(
        channel_id=channel_id, channel_name=channel_id, timezone="UTC",
        db_path=db_path, **profile_kwargs,
    )


def _seed_evidence(db_path, *, channel_id, platform, deliverable_id, n=6, weekday=1, hour=15, avg_view_pct=0.85):
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
            (f"po_{eid}", eid, platform, deliverable_id, "success", pub_at.isoformat(), _NOW.isoformat()),
        )
        conn.execute(
            "INSERT INTO platform_analytics (record_id, episode_id, platform, snapshot_date, "
            "avg_view_pct, source, imported_at) VALUES (?,?,?,?,?,?,?)",
            (f"pa_{eid}", eid, platform, _NOW.date().isoformat(), avg_view_pct, "api", _NOW.isoformat()),
        )
    conn.commit()
    conn.close()


class TestNoOp:
    def test_no_profile_is_a_noop(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        r = refresh_analytics_scheduling_policy("nonexistent", db_path=db_path, now=_NOW, assets_root=tmp_path)
        assert r.deliverables_evaluated == []
        assert r.deliverables_updated == []

    def test_no_deliverable_schedule_policy_is_a_noop(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _make_channel(db_path, analytics_scheduling_policy={"main_episode": {"youtube": {"enabled": True}}})
        r = refresh_analytics_scheduling_policy("ch1", db_path=db_path, now=_NOW, assets_root=tmp_path)
        assert r.deliverables_evaluated == []

    def test_no_analytics_scheduling_policy_is_a_noop(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _make_channel(
            db_path,
            deliverable_schedule_policy={"main_episode": {"day_of_week": "thursday", "time_of_day": "18:00"}},
        )
        r = refresh_analytics_scheduling_policy("ch1", db_path=db_path, now=_NOW, assets_root=tmp_path)
        assert r.deliverables_evaluated == []


class TestUpdatesOnEvidence:
    def test_real_evidence_rewrites_the_static_entry(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _make_channel(
            db_path,
            deliverable_schedule_policy={"main_episode": {"day_of_week": "thursday", "time_of_day": "18:00", "timezone": "UTC"}},
            analytics_scheduling_policy={"main_episode": {"youtube": {"enabled": True, "min_sample_size": 3, "min_confidence": 0.5}}},
        )
        _seed_evidence(db_path, channel_id="ch1", platform="youtube", deliverable_id="main_episode", n=6, weekday=1, hour=15)

        r = refresh_analytics_scheduling_policy("ch1", db_path=db_path, now=_NOW, assets_root=tmp_path)
        assert r.deliverables_updated == ["main_episode"]

        profile = get_channel_operations_profile("ch1", db_path=db_path)
        entry = profile.deliverable_schedule_policy["main_episode"]
        assert entry["day_of_week"] == "tuesday"
        assert entry["time_of_day"] == "15:00"
        assert entry["selection_mode"] == "analytics"
        assert entry["governing_platform"] == "youtube"


class TestLeavesUnchangedOnFallback:
    def test_insufficient_evidence_never_overwrites_existing_entry(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        original_entry = {"day_of_week": "thursday", "time_of_day": "18:00", "timezone": "UTC"}
        _make_channel(
            db_path,
            deliverable_schedule_policy={"main_episode": dict(original_entry)},
            analytics_scheduling_policy={"main_episode": {"youtube": {"enabled": True, "min_sample_size": 10}}},
        )
        # Only 2 samples -- below min_sample_size=10.
        _seed_evidence(db_path, channel_id="ch1", platform="youtube", deliverable_id="main_episode", n=2)

        r = refresh_analytics_scheduling_policy("ch1", db_path=db_path, now=_NOW, assets_root=tmp_path)
        assert r.deliverables_updated == []
        assert r.deliverables_evaluated == ["main_episode"]

        profile = get_channel_operations_profile("ch1", db_path=db_path)
        entry = profile.deliverable_schedule_policy["main_episode"]
        assert entry["day_of_week"] == "thursday"
        assert entry["time_of_day"] == "18:00"
        assert "selection_mode" not in entry


class TestSnapshotWritten:
    def test_evidence_snapshot_always_written(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _make_channel(
            db_path,
            deliverable_schedule_policy={"main_episode": {"day_of_week": "thursday", "time_of_day": "18:00", "timezone": "UTC"}},
            analytics_scheduling_policy={"main_episode": {"youtube": {"enabled": True, "min_sample_size": 3}}},
        )
        r = refresh_analytics_scheduling_policy("ch1", db_path=db_path, now=_NOW, assets_root=tmp_path)
        assert r.snapshot_path is not None
        snapshot = json.loads(Path(r.snapshot_path).read_text())
        assert snapshot["channel_id"] == "ch1"
        assert "main_episode" in snapshot["evidence_by_deliverable"]


class TestMultiPlatformGoverning:
    def test_highest_confidence_platform_governs(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _make_channel(
            db_path,
            deliverable_schedule_policy={"short_001": {"day_of_week": "thursday", "time_of_day": "18:00", "timezone": "UTC"}},
            analytics_scheduling_policy={
                "shorts": {
                    "youtube": {"enabled": True, "min_sample_size": 3, "min_confidence": 0.5},
                    "tiktok": {"enabled": True, "min_sample_size": 10, "min_confidence": 0.5},
                },
            },
            platform_deliverable_policy={
                "youtube": {"shorts": {"enabled": True}},
                "tiktok": {"shorts": {"enabled": True}},
            },
        )
        # YouTube: 6 samples, meets min_sample_size=3 -> confidence 1.0 (analytics)
        _seed_evidence(db_path, channel_id="ch1", platform="youtube", deliverable_id="short_001", n=6, weekday=1, hour=15)
        # TikTok: 4 samples, below its own min_sample_size=10 -> fallback (confidence 0.0)
        _seed_evidence(db_path, channel_id="ch1", platform="tiktok", deliverable_id="short_001", n=4, weekday=4, hour=9)

        r = refresh_analytics_scheduling_policy("ch1", db_path=db_path, now=_NOW, assets_root=tmp_path)
        assert r.deliverables_updated == ["short_001"]

        profile = get_channel_operations_profile("ch1", db_path=db_path)
        entry = profile.deliverable_schedule_policy["short_001"]
        assert entry["governing_platform"] == "youtube"
        assert entry["day_of_week"] == "tuesday"

        # Both platforms' independent evaluations are preserved in the evidence snapshot
        assert set(r.evidence_by_deliverable["short_001"].keys()) == {"youtube", "tiktok"}
        assert r.evidence_by_deliverable["short_001"]["tiktok"]["selection_mode"] == "fallback"
