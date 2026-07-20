"""
tests/test_channel_operations_profile.py — ChannelOperationsProfile tests

ChannelOperationsProfile is part of the canonical hypogeum.channel_config
system (no standalone module) — a sibling profile to Brand/Publishing/Model,
layered over an existing ChannelProfile. Unlike its siblings it hard-
validates (raises ValueError) rather than silently ignoring bad input.

Coverage:
    TestCreateAndRead            — full create/read JSON round-trip
    TestRequiresCanonicalChannel — missing canonical channel_id
    TestUpdate                   — partial update, unknown field rejection
    TestValidation                — every hard-validation rule
    TestHypogeumSeed              — exact required initial values, idempotent
    TestPublishingProfileUnaffected — sibling profile untouched
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from hypogeum.channel_config import (
    ChannelOperationsProfile,
    create_channel_operations_profile,
    create_channel_profile,
    create_publishing_profile,
    get_channel_operations_profile,
    get_publishing_profile,
    update_channel_operations_profile,
)
from hypogeum.db import get_connection, initialize_schema

# scripts/bootstrap_channel.py is not a package — load it by path so the
# test uses the exact same _build_operations_profile_dict() the real
# bootstrap script uses, rather than duplicating the required values here
# (which would risk silently drifting out of sync).
_BOOTSTRAP_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "bootstrap_channel.py"
_spec = importlib.util.spec_from_file_location("bootstrap_channel", _BOOTSTRAP_SCRIPT_PATH)
bootstrap_channel_script = importlib.util.module_from_spec(_spec)
sys.modules["bootstrap_channel"] = bootstrap_channel_script
_spec.loader.exec_module(bootstrap_channel_script)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "channel_operations_profile_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def canonical_channel(db_path):
    """A canonical ChannelProfile must exist before an operations profile can be created."""
    return create_channel_profile(
        channel_id="hypogeum", name="The Hypogeum Chronicles", db_path=db_path,
    )


def _valid_kwargs(**overrides) -> dict:
    kwargs = {
        "channel_id": "hypogeum",
        "channel_name": "The Hypogeum",
        "timezone": "Asia/Dubai",
        "operator_enabled": True,
        "production_cadence": {"videos_per_week": 1},
        "publishing_schedule": {"day_of_week": "thursday", "time_of_day": "18:00"},
        "enabled_platforms": {"youtube": True, "facebook": False, "tiktok": False, "instagram": False},
        "blocked_platform_reasons": {
            "facebook": "Meta app permission/configuration",
            "tiktok": "upload-init endpoint HTTP 404",
            "instagram": "public staging URL not configured",
        },
        "discovery_enabled": True,
        "production_enabled": True,
        "publishing_enabled": True,
        "analytics_sync_enabled": True,
        "unattended_operation_enabled": False,
        "max_concurrent_episodes": 1,
        "retry_policy": {"max_retries_per_stage": 1, "backoff_seconds": 60},
        "failure_halt_policy": {
            "halt_on_production_failure": True,
            "halt_on_validation_failure": True,
            "halt_on_enabled_platform_publish_failure": True,
            "blocked_or_disabled_platforms_do_not_halt": True,
        },
        "cost_ceiling_usd_per_episode": None,
        "default_privacy_state": "private",
    }
    kwargs.update(overrides)
    return kwargs


# ===========================================================================
# TestCreateAndRead
# ===========================================================================

class TestCreateAndRead:
    def test_create_returns_channel_operations_profile(self, db_path, canonical_channel):
        profile = create_channel_operations_profile(db_path=db_path, **_valid_kwargs())
        assert isinstance(profile, ChannelOperationsProfile)
        assert profile.channel_id == "hypogeum"

    def test_full_json_round_trip(self, db_path, canonical_channel):
        create_channel_operations_profile(db_path=db_path, **_valid_kwargs())
        fetched = get_channel_operations_profile("hypogeum", db_path=db_path)

        assert fetched is not None
        assert fetched.channel_name == "The Hypogeum"
        assert fetched.timezone == "Asia/Dubai"
        assert fetched.operator_enabled is True
        assert fetched.production_cadence == {"videos_per_week": 1}
        assert fetched.publishing_schedule == {"day_of_week": "thursday", "time_of_day": "18:00"}
        assert fetched.enabled_platforms == {
            "youtube": True, "facebook": False, "tiktok": False, "instagram": False,
        }
        assert fetched.blocked_platform_reasons == {
            "facebook": "Meta app permission/configuration",
            "tiktok": "upload-init endpoint HTTP 404",
            "instagram": "public staging URL not configured",
        }
        assert fetched.discovery_enabled is True
        assert fetched.production_enabled is True
        assert fetched.publishing_enabled is True
        assert fetched.analytics_sync_enabled is True
        assert fetched.unattended_operation_enabled is False
        assert fetched.max_concurrent_episodes == 1
        assert fetched.retry_policy == {"max_retries_per_stage": 1, "backoff_seconds": 60}
        assert fetched.failure_halt_policy == {
            "halt_on_production_failure": True,
            "halt_on_validation_failure": True,
            "halt_on_enabled_platform_publish_failure": True,
            "blocked_or_disabled_platforms_do_not_halt": True,
        }
        assert fetched.cost_ceiling_usd_per_episode is None
        assert fetched.default_privacy_state == "private"
        assert fetched.created_at
        assert fetched.updated_at
        assert fetched.metadata_version == "v1"

    def test_get_unknown_channel_returns_none(self, db_path):
        assert get_channel_operations_profile("does_not_exist", db_path=db_path) is None


# ===========================================================================
# TestRequiresCanonicalChannel
# ===========================================================================

class TestRequiresCanonicalChannel:
    def test_missing_canonical_channel_id_raises(self, db_path):
        """No ChannelProfile exists for 'ghost' — operations profile must refuse."""
        with pytest.raises(ValueError, match="does not exist in channel_profiles"):
            create_channel_operations_profile(db_path=db_path, **_valid_kwargs(channel_id="ghost"))


# ===========================================================================
# TestUpdate
# ===========================================================================

class TestUpdate:
    def test_partial_update_preserves_untouched_values(self, db_path, canonical_channel):
        create_channel_operations_profile(db_path=db_path, **_valid_kwargs())
        ok = update_channel_operations_profile(
            "hypogeum", db_path=db_path, max_concurrent_episodes=2,
        )
        assert ok is True

        fetched = get_channel_operations_profile("hypogeum", db_path=db_path)
        assert fetched.max_concurrent_episodes == 2
        # Everything else untouched
        assert fetched.timezone == "Asia/Dubai"
        assert fetched.enabled_platforms == {
            "youtube": True, "facebook": False, "tiktok": False, "instagram": False,
        }
        assert fetched.default_privacy_state == "private"

    def test_unknown_update_field_is_rejected(self, db_path, canonical_channel):
        create_channel_operations_profile(db_path=db_path, **_valid_kwargs())
        with pytest.raises(ValueError, match="unknown ChannelOperationsProfile field"):
            update_channel_operations_profile(
                "hypogeum", db_path=db_path, not_a_real_field="x",
            )

    def test_update_on_nonexistent_profile_raises(self, db_path):
        with pytest.raises(ValueError, match="no ChannelOperationsProfile exists"):
            update_channel_operations_profile("hypogeum", db_path=db_path, timezone="UTC")


# ===========================================================================
# TestValidation — every hard-validation rule raises ValueError, never warns
# ===========================================================================

class TestValidation:
    def test_invalid_timezone_raises(self, db_path, canonical_channel):
        with pytest.raises(ValueError, match="invalid timezone"):
            create_channel_operations_profile(
                db_path=db_path, **_valid_kwargs(timezone="Mars/Olympus_Mons"),
            )

    def test_invalid_privacy_state_raises(self, db_path, canonical_channel):
        with pytest.raises(ValueError, match="default_privacy_state"):
            create_channel_operations_profile(
                db_path=db_path, **_valid_kwargs(default_privacy_state="public_and_loud"),
            )

    def test_invalid_concurrency_raises(self, db_path, canonical_channel):
        with pytest.raises(ValueError, match="max_concurrent_episodes"):
            create_channel_operations_profile(
                db_path=db_path, **_valid_kwargs(max_concurrent_episodes=0),
            )

    def test_negative_cost_ceiling_raises(self, db_path, canonical_channel):
        with pytest.raises(ValueError, match="cost_ceiling_usd_per_episode"):
            create_channel_operations_profile(
                db_path=db_path, **_valid_kwargs(cost_ceiling_usd_per_episode=-5.0),
            )

    def test_zero_cost_ceiling_raises(self, db_path, canonical_channel):
        """Zero must NOT mean unlimited — null means inherit, > 0 means a real ceiling."""
        with pytest.raises(ValueError, match="cost_ceiling_usd_per_episode"):
            create_channel_operations_profile(
                db_path=db_path, **_valid_kwargs(cost_ceiling_usd_per_episode=0.0),
            )

    def test_null_cost_ceiling_is_valid(self, db_path, canonical_channel):
        profile = create_channel_operations_profile(
            db_path=db_path, **_valid_kwargs(cost_ceiling_usd_per_episode=None),
        )
        assert profile.cost_ceiling_usd_per_episode is None

    def test_positive_cost_ceiling_is_valid(self, db_path, canonical_channel):
        profile = create_channel_operations_profile(
            db_path=db_path, **_valid_kwargs(cost_ceiling_usd_per_episode=25.0),
        )
        assert profile.cost_ceiling_usd_per_episode == 25.0

    def test_blocked_platform_enabled_conflict_raises(self, db_path, canonical_channel):
        """A platform cannot be both enabled=true and carry a blocked reason."""
        with pytest.raises(ValueError, match="cannot be both enabled and blocked"):
            create_channel_operations_profile(
                db_path=db_path,
                **_valid_kwargs(
                    enabled_platforms={"youtube": True, "facebook": True, "tiktok": False, "instagram": False},
                    blocked_platform_reasons={"facebook": "some reason"},
                ),
            )

    def test_blocked_platform_without_reason_raises(self, db_path, canonical_channel):
        with pytest.raises(ValueError, match="must be non-empty"):
            create_channel_operations_profile(
                db_path=db_path,
                **_valid_kwargs(
                    blocked_platform_reasons={"facebook": "", "tiktok": "x", "instagram": "y"},
                ),
            )

    def test_enabled_platform_with_blocked_reason_raises(self, db_path, canonical_channel):
        """Inverse of the enabled/blocked conflict: enabled_platforms marks it
        enabled while blocked_platform_reasons also names it — same
        underlying constraint checked from the other direction."""
        with pytest.raises(ValueError, match="cannot be both enabled and blocked"):
            create_channel_operations_profile(
                db_path=db_path,
                **_valid_kwargs(
                    enabled_platforms={"youtube": True, "facebook": False, "tiktok": False, "instagram": False},
                    blocked_platform_reasons={"youtube": "oops", "facebook": "x", "tiktok": "y", "instagram": "z"},
                ),
            )

    def test_unsupported_platform_name_raises(self, db_path, canonical_channel):
        with pytest.raises(ValueError, match="unsupported platform"):
            create_channel_operations_profile(
                db_path=db_path,
                **_valid_kwargs(enabled_platforms={"myspace": True}),
            )

    def test_videos_per_week_below_one_raises(self, db_path, canonical_channel):
        with pytest.raises(ValueError, match="videos_per_week"):
            create_channel_operations_profile(
                db_path=db_path,
                **_valid_kwargs(production_cadence={"videos_per_week": 0}),
            )

    def test_invalid_publishing_time_format_raises(self, db_path, canonical_channel):
        with pytest.raises(ValueError, match="HH:MM"):
            create_channel_operations_profile(
                db_path=db_path,
                **_valid_kwargs(publishing_schedule={"day_of_week": "thursday", "time_of_day": "6pm"}),
            )


# ===========================================================================
# TestHypogeumSeed — exact required initial values, idempotent bootstrap
# ===========================================================================

class TestHypogeumSeed:
    def test_build_operations_profile_dict_matches_required_values(self):
        """Pure-function check — no DB involved."""
        d = bootstrap_channel_script._build_operations_profile_dict()
        assert d["channel_id"] == "hypogeum"
        assert d["channel_name"] == "The Hypogeum"
        assert d["timezone"] == "Asia/Dubai"
        assert d["operator_enabled"] is True
        assert d["production_cadence"] == {"videos_per_week": 1}
        assert d["publishing_schedule"] == {"day_of_week": "thursday", "time_of_day": "18:00"}
        assert d["enabled_platforms"] == {
            "youtube": True, "facebook": False, "tiktok": False, "instagram": False,
        }
        assert d["blocked_platform_reasons"] == {
            "facebook": "Meta app permission/configuration",
            "tiktok": "upload-init endpoint HTTP 404",
            "instagram": "public staging URL not configured",
        }
        assert d["discovery_enabled"] is True
        assert d["production_enabled"] is True
        assert d["publishing_enabled"] is True
        assert d["analytics_sync_enabled"] is True
        assert d["unattended_operation_enabled"] is False
        assert d["max_concurrent_episodes"] == 1
        assert d["retry_policy"] == {"max_retries_per_stage": 1, "backoff_seconds": 60}
        assert d["failure_halt_policy"] == {
            "halt_on_production_failure": True,
            "halt_on_validation_failure": True,
            "halt_on_enabled_platform_publish_failure": True,
            "blocked_or_disabled_platforms_do_not_halt": True,
        }
        assert d["cost_ceiling_usd_per_episode"] is None
        assert d["default_privacy_state"] == "private"

    def test_seeded_profile_persists_exact_required_values(self, db_path, canonical_channel):
        d = bootstrap_channel_script._build_operations_profile_dict()
        create_channel_operations_profile(db_path=db_path, **d)

        fetched = get_channel_operations_profile("hypogeum", db_path=db_path)
        assert fetched.channel_name == "The Hypogeum"
        assert fetched.timezone == "Asia/Dubai"
        assert fetched.enabled_platforms["youtube"] is True
        assert fetched.enabled_platforms["facebook"] is False
        assert fetched.enabled_platforms["tiktok"] is False
        assert fetched.enabled_platforms["instagram"] is False
        assert fetched.blocked_platform_reasons["facebook"] == "Meta app permission/configuration"
        assert fetched.blocked_platform_reasons["tiktok"] == "upload-init endpoint HTTP 404"
        assert fetched.blocked_platform_reasons["instagram"] == "public staging URL not configured"

    def test_bootstrap_run_twice_produces_one_row(self, db_path, canonical_channel):
        """The idempotency pattern the real bootstrap script uses: check-then-create."""
        d = bootstrap_channel_script._build_operations_profile_dict()

        for _ in range(2):
            if get_channel_operations_profile("hypogeum", db_path=db_path) is None:
                create_channel_operations_profile(db_path=db_path, **d)

        conn = get_connection(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM channel_operations_profiles WHERE channel_id='hypogeum'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1


# ===========================================================================
# TestPublishingProfileUnaffected — sibling profile untouched
# ===========================================================================

class TestPublishingProfileUnaffected:
    def test_existing_publishing_profile_remains_unchanged(self, db_path, canonical_channel):
        pub = create_publishing_profile(
            name="The Hypogeum Chronicles Publishing",
            enabled_platforms=["youtube"],
            schedule={"cadence": "weekly", "day_of_week": "thursday", "time_of_day": "18:00", "timezone": "UTC"},
            db_path=db_path,
        )
        before = get_publishing_profile(pub.publishing_profile_id, db_path=db_path)

        create_channel_operations_profile(db_path=db_path, **_valid_kwargs())
        update_channel_operations_profile("hypogeum", db_path=db_path, max_concurrent_episodes=3)

        after = get_publishing_profile(pub.publishing_profile_id, db_path=db_path)
        assert after == before
