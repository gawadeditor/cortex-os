"""
tests/test_channel_operations_profile_schedule_policy.py — Idempotent
deliverable_schedule_policy install path.

Covers hypogeum.channel_config.update_channel_operations_profile()'s
existing, generic field-update mechanism as the exact executable
activation command for installing ChannelOperationsProfile.
deliverable_schedule_policy on an EXISTING row — without recreating,
replacing, or losing any of its other fields. No new function was needed:
this only registers the field in the existing _OPERATIONS_FIELD_MAP
(hypogeum/channel_config.py), which already provides idempotent update,
fail-closed-on-missing-channel, and read-back verification generically
for every registered field.

All state uses an isolated tmp_path SQLite database — never the production
.hypogeum-assets/hypogeum.db.
"""

from __future__ import annotations

import pytest

from hypogeum.channel_config import (
    create_brand_profile,
    create_channel_operations_profile,
    create_channel_profile,
    create_model_profile,
    create_publishing_profile,
    get_channel_operations_profile,
    update_channel_operations_profile,
)
from hypogeum.db import initialize_schema

HYPOGEUM_POLICY = {
    "main_episode": {"day_of_week": "sunday", "time_of_day": "14:00", "timezone": "UTC"},
    "short_001": {"day_of_week": "monday", "time_of_day": "18:00", "timezone": "UTC"},
    "short_002": {"day_of_week": "wednesday", "time_of_day": "18:00", "timezone": "UTC"},
    "short_003": {"day_of_week": "friday", "time_of_day": "18:00", "timezone": "UTC"},
}


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "schedule_policy_update_test.db"
    initialize_schema(p)
    return p


def _make_channel(db_path, channel_id, *, timezone="Asia/Dubai"):
    brand = create_brand_profile(name=f"{channel_id} Brand", db_path=db_path)
    model = create_model_profile(name=f"{channel_id} Model", db_path=db_path)
    pub = create_publishing_profile(name=f"{channel_id} Publishing", db_path=db_path)
    create_channel_profile(
        channel_id=channel_id, name=channel_id,
        brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
        publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
    )
    return create_channel_operations_profile(
        channel_id=channel_id, channel_name=channel_id, timezone=timezone,
        publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
        enabled_platforms={"youtube": True},
        max_concurrent_episodes=1,
        db_path=db_path,
    )


class TestScheduleyPolicyInstallPath:
    def test_updates_existing_profile(self, db_path):
        _make_channel(db_path, "hypogeum")
        ok = update_channel_operations_profile("hypogeum", deliverable_schedule_policy=HYPOGEUM_POLICY, db_path=db_path)
        assert ok is True
        after = get_channel_operations_profile("hypogeum", db_path=db_path)
        assert after.deliverable_schedule_policy == HYPOGEUM_POLICY

    def test_repeated_update_is_harmless(self, db_path):
        _make_channel(db_path, "hypogeum")
        update_channel_operations_profile("hypogeum", deliverable_schedule_policy=HYPOGEUM_POLICY, db_path=db_path)
        first = get_channel_operations_profile("hypogeum", db_path=db_path)
        ok2 = update_channel_operations_profile("hypogeum", deliverable_schedule_policy=HYPOGEUM_POLICY, db_path=db_path)
        second = get_channel_operations_profile("hypogeum", db_path=db_path)
        assert ok2 is True
        assert second.deliverable_schedule_policy == first.deliverable_schedule_policy == HYPOGEUM_POLICY

    def test_unrelated_fields_remain_unchanged(self, db_path):
        before = _make_channel(db_path, "hypogeum")
        update_channel_operations_profile("hypogeum", deliverable_schedule_policy=HYPOGEUM_POLICY, db_path=db_path)
        after = get_channel_operations_profile("hypogeum", db_path=db_path)

        assert after.timezone == before.timezone == "Asia/Dubai"
        assert after.channel_name == before.channel_name
        assert after.publishing_schedule == before.publishing_schedule == {"day_of_week": "thursday", "time_of_day": "18:00"}
        assert after.enabled_platforms == before.enabled_platforms == {"youtube": True}
        assert after.max_concurrent_episodes == before.max_concurrent_episodes == 1
        assert after.operator_enabled == before.operator_enabled
        assert after.discovery_enabled == before.discovery_enabled
        assert after.production_enabled == before.production_enabled
        assert after.publishing_enabled == before.publishing_enabled
        assert after.default_privacy_state == before.default_privacy_state
        assert after.created_at == before.created_at, "created_at must never change on an update"

    def test_unrelated_channel_remains_unchanged(self, db_path):
        _make_channel(db_path, "channel_a")
        before_b = _make_channel(db_path, "channel_b")

        update_channel_operations_profile("channel_a", deliverable_schedule_policy=HYPOGEUM_POLICY, db_path=db_path)

        after_b = get_channel_operations_profile("channel_b", db_path=db_path)
        assert after_b.deliverable_schedule_policy == {}
        assert after_b == before_b

    def test_missing_channel_fails_closed(self, db_path):
        with pytest.raises(ValueError, match="no ChannelOperationsProfile exists"):
            update_channel_operations_profile("channel_missing", deliverable_schedule_policy=HYPOGEUM_POLICY, db_path=db_path)

    def test_installing_the_real_hypogeum_scheduling_policy_artifact(self, db_path):
        """Loads channel_packages/hypogeum/scheduling_policy_v1.json's own
        'deliverable_schedule' object and installs it exactly, proving the
        committed artifact is directly usable by this update path with no
        transformation."""
        import json
        from pathlib import Path

        policy_path = Path(__file__).resolve().parent.parent / "channel_packages" / "hypogeum" / "scheduling_policy_v1.json"
        policy_doc = json.loads(policy_path.read_text(encoding="utf-8"))

        _make_channel(db_path, "hypogeum")
        ok = update_channel_operations_profile(
            "hypogeum", deliverable_schedule_policy=policy_doc["deliverable_schedule"], db_path=db_path,
        )
        assert ok is True
        after = get_channel_operations_profile("hypogeum", db_path=db_path)
        assert after.deliverable_schedule_policy == policy_doc["deliverable_schedule"]
        assert set(after.deliverable_schedule_policy.keys()) == {"main_episode", "short_001", "short_002", "short_003"}
