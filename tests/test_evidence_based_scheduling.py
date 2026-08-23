"""
tests/test_evidence_based_scheduling.py —
hypogeum.publishing.evidence_based_scheduling

Refreshes ChannelOperationsProfile.deliverable_schedule_policy AND
.bundle_cycle_policy from real evidence via hypogeum.schedule_optimizer,
writing ONLY platform_overrides sub-entries (never a deliverable's own
base day_of_week/time_of_day) — reaching BOTH policy layers hypogeum.
publishing.schedule_compiler already consumes, with zero change needed to
that module (its 2026-08-23 platform_overrides mechanism is exactly what
this bridge writes into). Covers:
  TestNoOp                    — missing profile / unconfigured policy is a no-op
  TestFlatLayerUpdatesOnEvidence — real evidence writes platform_overrides
                                    on a flat deliverable_schedule_policy entry
  TestBundleCycleLayerUpdatesOnEvidence — same, reaching bundle_cycle_policy
  TestLeavesUnchangedOnFallback — insufficient evidence never writes anything
  TestSnapshotWritten          — audit snapshot file is always written
  TestEveryPlatformIndependent  — multiple platforms each get their OWN
                                    override, never one "winning" platform
                                    collapsing the others
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


def _seed_evidence(db_path, *, channel_id, platform, deliverable_id, n=6, weekday=1, hour=15, avg_view_pct=0.85, id_offset=0):
    conn = get_connection(db_path)
    for i in range(id_offset, id_offset + n):
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

    def test_no_declared_deliverables_in_either_layer_is_a_noop(self, tmp_path):
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


class TestFlatLayerUpdatesOnEvidence:
    def test_real_evidence_writes_platform_overrides_never_the_base_entry(self, tmp_path):
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
        # Base entry untouched.
        assert entry["day_of_week"] == "thursday"
        assert entry["time_of_day"] == "18:00"
        # Platform override added.
        override = entry["platform_overrides"]["youtube"]
        assert override["day_of_week"] == "tuesday"
        assert override["time_of_day"] == "15:00"
        assert override["selection_mode"] == "analytics"


class TestBundleCycleLayerUpdatesOnEvidence:
    def test_real_evidence_reaches_bundle_cycle_policy(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _make_channel(
            db_path,
            bundle_cycle_policy={
                "cycle1": {
                    "anchor_deliverable_id": "main_episode",
                    "required_deliverable_ids": ["main_episode"],
                    "deliverables": {
                        "main_episode": {"day_of_week": "sunday", "time_of_day": "14:00", "timezone": "UTC"},
                    },
                },
            },
            analytics_scheduling_policy={"main_episode": {"youtube": {"enabled": True, "min_sample_size": 3, "min_confidence": 0.5}}},
        )
        _seed_evidence(db_path, channel_id="ch1", platform="youtube", deliverable_id="main_episode", n=6, weekday=1, hour=15)

        r = refresh_analytics_scheduling_policy("ch1", db_path=db_path, now=_NOW, assets_root=tmp_path)
        assert r.deliverables_updated == ["main_episode"]

        profile = get_channel_operations_profile("ch1", db_path=db_path)
        member_entry = profile.bundle_cycle_policy["cycle1"]["deliverables"]["main_episode"]
        # Base bundle-cycle entry untouched.
        assert member_entry["day_of_week"] == "sunday"
        assert member_entry["time_of_day"] == "14:00"
        # Platform override reaches the real live mechanism.
        override = member_entry["platform_overrides"]["youtube"]
        assert override["day_of_week"] == "tuesday"
        assert override["time_of_day"] == "15:00"
        # deliverable_schedule_policy (the flat layer) is untouched -- this
        # deliverable was never declared there.
        assert profile.deliverable_schedule_policy == {}

    def test_compiled_schedule_actually_uses_the_written_override(self, tmp_path):
        """End-to-end: after refresh writes a bundle-cycle platform
        override, compile_schedule_plan_from_channel_package() (unchanged
        by this module) picks it up automatically on its next call."""
        from hypogeum.publishing.schedule_compiler import compile_schedule_plan_from_channel_package

        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _make_channel(
            db_path,
            bundle_cycle_policy={
                "cycle1": {
                    "anchor_deliverable_id": "main_episode",
                    "required_deliverable_ids": ["main_episode"],
                    "deliverables": {
                        "main_episode": {"day_of_week": "sunday", "time_of_day": "14:00", "timezone": "UTC"},
                    },
                },
            },
            analytics_scheduling_policy={"main_episode": {"youtube": {"enabled": True, "min_sample_size": 3, "min_confidence": 0.5}}},
        )
        _seed_evidence(db_path, channel_id="ch1", platform="youtube", deliverable_id="main_episode", n=6, weekday=1, hour=15)

        refresh_analytics_scheduling_policy("ch1", db_path=db_path, now=_NOW, assets_root=tmp_path)

        result = compile_schedule_plan_from_channel_package(
            "ch1", ["main_episode"], db_path=db_path, now=_NOW, require_explicit_policy=True,
        )
        assert result.blocked is False, result.blocked_reason
        sched = result.schedule_plan.entries["main_episode"]
        assert sched.scheduled_for.strftime("%A").lower() == "sunday"  # base slot unchanged
        assert "youtube" in sched.platform_scheduled_for_by_platform
        assert sched.platform_scheduled_for_by_platform["youtube"].strftime("%A").lower() == "tuesday"


class TestLeavesUnchangedOnFallback:
    def test_insufficient_evidence_writes_nothing(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _make_channel(
            db_path,
            deliverable_schedule_policy={"main_episode": {"day_of_week": "thursday", "time_of_day": "18:00", "timezone": "UTC"}},
            analytics_scheduling_policy={"main_episode": {"youtube": {"enabled": True, "min_sample_size": 10}}},
        )
        # Only 2 samples -- below min_sample_size=10.
        _seed_evidence(db_path, channel_id="ch1", platform="youtube", deliverable_id="main_episode", n=2)

        r = refresh_analytics_scheduling_policy("ch1", db_path=db_path, now=_NOW, assets_root=tmp_path)
        assert r.deliverables_updated == []
        assert r.deliverables_evaluated == ["main_episode"]

        profile = get_channel_operations_profile("ch1", db_path=db_path)
        entry = profile.deliverable_schedule_policy["main_episode"]
        assert entry == {"day_of_week": "thursday", "time_of_day": "18:00", "timezone": "UTC"}


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


class TestEveryPlatformIndependent:
    def test_every_qualifying_platform_gets_its_own_override_not_one_winner(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _make_channel(
            db_path,
            deliverable_schedule_policy={"short_001": {"day_of_week": "thursday", "time_of_day": "18:00", "timezone": "UTC"}},
            analytics_scheduling_policy={
                "shorts": {
                    "youtube": {"enabled": True, "min_sample_size": 3, "min_confidence": 0.5},
                    "tiktok": {"enabled": True, "min_sample_size": 3, "min_confidence": 0.5},
                },
            },
            platform_deliverable_policy={
                "youtube": {"shorts": {"enabled": True}},
                "tiktok": {"shorts": {"enabled": True}},
            },
        )
        # Both platforms individually have sufficient, DIFFERENT evidence.
        _seed_evidence(db_path, channel_id="ch1", platform="youtube", deliverable_id="short_001", n=6, weekday=1, hour=15)
        _seed_evidence(db_path, channel_id="ch1", platform="tiktok", deliverable_id="short_001", n=6, weekday=4, hour=9)

        r = refresh_analytics_scheduling_policy("ch1", db_path=db_path, now=_NOW, assets_root=tmp_path)
        assert r.deliverables_updated == ["short_001"]

        profile = get_channel_operations_profile("ch1", db_path=db_path)
        overrides = profile.deliverable_schedule_policy["short_001"]["platform_overrides"]
        assert set(overrides.keys()) == {"youtube", "tiktok"}
        assert overrides["youtube"]["day_of_week"] == "tuesday"
        assert overrides["tiktok"]["day_of_week"] == "friday"

    def test_only_platforms_reaching_analytics_mode_get_an_override(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _make_channel(
            db_path,
            deliverable_schedule_policy={"short_001": {"day_of_week": "thursday", "time_of_day": "18:00", "timezone": "UTC"}},
            analytics_scheduling_policy={
                "shorts": {
                    "youtube": {"enabled": True, "min_sample_size": 3, "min_confidence": 0.5},
                    "tiktok": {"enabled": True, "min_sample_size": 10, "min_confidence": 0.5},  # never met
                },
            },
            platform_deliverable_policy={
                "youtube": {"shorts": {"enabled": True}},
                "tiktok": {"shorts": {"enabled": True}},
            },
        )
        _seed_evidence(db_path, channel_id="ch1", platform="youtube", deliverable_id="short_001", n=6)
        _seed_evidence(db_path, channel_id="ch1", platform="tiktok", deliverable_id="short_001", n=2)  # below threshold

        refresh_analytics_scheduling_policy("ch1", db_path=db_path, now=_NOW, assets_root=tmp_path)

        profile = get_channel_operations_profile("ch1", db_path=db_path)
        overrides = profile.deliverable_schedule_policy["short_001"]["platform_overrides"]
        assert set(overrides.keys()) == {"youtube"}


class TestFallbackToAnalyticsTransition:
    """
    2026-08-23 (Phase 4 requirement): 'when evidence becomes sufficient,
    the periodic analytics sync must allow later cycles to move
    automatically to analytics mode'. Proves the actual TRANSITION across
    two sequential refresh calls -- not just two independent static
    snapshots -- for the SAME deliverable/platform: first refresh (thin
    evidence) leaves the platform on the base fallback slot; more
    evidence accumulates; second refresh (same call, no code change, no
    manual step) promotes it to analytics mode automatically.
    """

    def test_second_refresh_promotes_once_evidence_accumulates(self, tmp_path):
        db_path = tmp_path / "t.db"
        initialize_schema(db_path)
        _make_channel(
            db_path,
            deliverable_schedule_policy={"main_episode": {"day_of_week": "thursday", "time_of_day": "18:00", "timezone": "UTC"}},
            analytics_scheduling_policy={"main_episode": {"youtube": {"enabled": True, "min_sample_size": 5, "min_confidence": 0.5}}},
        )

        # First refresh: only 2 real samples -- below min_sample_size=5.
        _seed_evidence(db_path, channel_id="ch1", platform="youtube", deliverable_id="main_episode", n=2, weekday=1, hour=15)
        r1 = refresh_analytics_scheduling_policy("ch1", db_path=db_path, now=_NOW, assets_root=tmp_path)
        assert r1.deliverables_updated == []
        profile = get_channel_operations_profile("ch1", db_path=db_path)
        assert "platform_overrides" not in profile.deliverable_schedule_policy["main_episode"]

        # Evidence accumulates: 3 more samples land (total 5, meets threshold).
        _seed_evidence(db_path, channel_id="ch1", platform="youtube", deliverable_id="main_episode", n=3, weekday=1, hour=15, id_offset=2)

        # Second refresh, same call, no manual intervention -- promotes automatically.
        r2 = refresh_analytics_scheduling_policy("ch1", db_path=db_path, now=_NOW, assets_root=tmp_path)
        assert r2.deliverables_updated == ["main_episode"]
        profile = get_channel_operations_profile("ch1", db_path=db_path)
        override = profile.deliverable_schedule_policy["main_episode"]["platform_overrides"]["youtube"]
        assert override["selection_mode"] == "analytics"
        assert override["sample_count"] >= 5
