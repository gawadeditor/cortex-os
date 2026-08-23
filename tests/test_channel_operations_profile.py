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
    TestPersistenceFailsClosed   — INSERT/UPDATE/read failures raise RuntimeError,
                                    never masquerade as success
    TestHypogeumSeed              — exact required initial values, idempotent
    TestBootstrapFailsClosed     — bootstrap script surfaces persistence failure
    TestPublishingProfileUnaffected — sibling profile untouched
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from hypogeum.channel_config import (
    ChannelOperationsProfile,
    ChannelPackagePolicyDriftError,
    create_channel_operations_profile,
    create_channel_profile,
    create_publishing_profile,
    get_channel_operations_profile,
    get_publishing_profile,
    load_platform_distribution_policy,
    sync_platform_distribution_policy_to_runtime,
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


class _FlakyConnection:
    """
    Delegates everything to a real sqlite3.Connection except .execute(),
    which raises sqlite3.OperationalError when the SQL contains a given
    substring. sqlite3.Connection.execute is a read-only C attribute, so it
    can't be monkeypatched directly on the object — this wrapper is the
    substitute.
    """
    def __init__(self, real_conn, fail_when_sql_contains: str):
        self._real_conn = real_conn
        self._fail_when_sql_contains = fail_when_sql_contains

    def execute(self, sql, *args, **kwargs):
        if self._fail_when_sql_contains in sql:
            raise sqlite3.OperationalError("simulated disk I/O error")
        return self._real_conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real_conn, name)


def _flaky_get_connection(db_path, fail_when_sql_contains: str):
    """
    A side_effect for patching hypogeum.channel_config.get_connection that
    returns a REAL connection to db_path, but makes any .execute() call whose
    SQL contains `fail_when_sql_contains` raise sqlite3.OperationalError.

    This lets a test simulate "the UPDATE/INSERT statement itself failed"
    without breaking the unrelated reads (e.g. the existence check a create/
    update call performs first) that happen over the same connection factory.
    """
    def factory(path):
        return _FlakyConnection(get_connection(path), fail_when_sql_contains)

    return factory


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
# TestPersistenceFailsClosed — a bounded unattended-runtime reliability fix.
#
# create/get/update must never report success (or silently return None/
# False) when the underlying database operation actually failed — a caller
# relying on this profile to gate autonomous execution must be able to trust
# a returned value.
# ===========================================================================

class TestPersistenceFailsClosed:
    def test_create_raises_runtime_error_when_insert_fails(self, db_path, canonical_channel):
        with patch(
            "hypogeum.channel_config.get_connection",
            side_effect=_flaky_get_connection(db_path, "INSERT INTO channel_operations_profiles"),
        ):
            with pytest.raises(RuntimeError, match="INSERT failed"):
                create_channel_operations_profile(db_path=db_path, **_valid_kwargs())

    def test_create_never_returns_success_after_persistence_failure(self, db_path, canonical_channel):
        """No row must exist after a failed create — no in-memory-only "success"."""
        with patch(
            "hypogeum.channel_config.get_connection",
            side_effect=_flaky_get_connection(db_path, "INSERT INTO channel_operations_profiles"),
        ):
            with pytest.raises(RuntimeError):
                create_channel_operations_profile(db_path=db_path, **_valid_kwargs())

        assert get_channel_operations_profile("hypogeum", db_path=db_path) is None

    def test_create_returns_the_persisted_readback_profile(self, db_path, canonical_channel):
        returned = create_channel_operations_profile(db_path=db_path, **_valid_kwargs())
        independently_fetched = get_channel_operations_profile("hypogeum", db_path=db_path)
        assert returned == independently_fetched

    def test_get_returns_none_only_for_genuinely_missing_row(self, db_path, canonical_channel):
        create_channel_operations_profile(db_path=db_path, **_valid_kwargs())
        assert get_channel_operations_profile("hypogeum", db_path=db_path) is not None
        assert get_channel_operations_profile("some_other_channel", db_path=db_path) is None

    def test_get_raises_runtime_error_on_database_failure(self, db_path, canonical_channel):
        create_channel_operations_profile(db_path=db_path, **_valid_kwargs())
        with patch(
            "hypogeum.channel_config.get_connection",
            side_effect=_flaky_get_connection(db_path, "SELECT * FROM channel_operations_profiles"),
        ):
            with pytest.raises(RuntimeError, match="read failed"):
                get_channel_operations_profile("hypogeum", db_path=db_path)

    def test_update_raises_runtime_error_on_database_failure(self, db_path, canonical_channel):
        create_channel_operations_profile(db_path=db_path, **_valid_kwargs())
        with patch(
            "hypogeum.channel_config.get_connection",
            side_effect=_flaky_get_connection(db_path, "UPDATE channel_operations_profiles"),
        ):
            with pytest.raises(RuntimeError, match="UPDATE failed"):
                update_channel_operations_profile(
                    "hypogeum", db_path=db_path, max_concurrent_episodes=5,
                )
        # The field must not appear changed — the failed UPDATE never committed.
        assert get_channel_operations_profile("hypogeum", db_path=db_path).max_concurrent_episodes == 1

    def test_update_verifies_persisted_field_values(self, db_path, canonical_channel):
        create_channel_operations_profile(db_path=db_path, **_valid_kwargs())
        ok = update_channel_operations_profile(
            "hypogeum", db_path=db_path,
            max_concurrent_episodes=2, default_privacy_state="unlisted",
        )
        assert ok is True

        fetched = get_channel_operations_profile("hypogeum", db_path=db_path)
        assert fetched.max_concurrent_episodes == 2
        assert fetched.default_privacy_state == "unlisted"


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
# TestBootstrapFailsClosed
#
# _run_operations_profile_bootstrap() has no db_path parameter (matching
# every other function in scripts/bootstrap_channel.py — it always targets
# the real default DB). To exercise it against a temp DB without touching
# .hypogeum-assets/hypogeum.db, patch channel_config's module-level
# _DEFAULT_DB_PATH for the duration of the test — every function in that
# module resolves it at call time, so this safely redirects all of
# _run_operations_profile_bootstrap()'s internal (db_path=None) calls.
# ===========================================================================

class TestBootstrapFailsClosed:
    def test_bootstrap_fails_when_persistence_fails(self, db_path, canonical_channel):
        with patch("hypogeum.channel_config._DEFAULT_DB_PATH", db_path), \
             patch(
                 "hypogeum.channel_config.get_connection",
                 side_effect=_flaky_get_connection(db_path, "INSERT INTO channel_operations_profiles"),
             ):
            ok = bootstrap_channel_script._run_operations_profile_bootstrap()

        assert ok is False
        assert get_channel_operations_profile("hypogeum", db_path=db_path) is None

    def test_idempotent_bootstrap_succeeds_for_existing_row(self, db_path, canonical_channel):
        create_channel_operations_profile(
            db_path=db_path, **bootstrap_channel_script._build_operations_profile_dict()
        )

        with patch("hypogeum.channel_config._DEFAULT_DB_PATH", db_path):
            ok = bootstrap_channel_script._run_operations_profile_bootstrap()

        assert ok is True
        # Still exactly one row — idempotent, no duplicate insert attempted.
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


# ===========================================================================
# TestPlatformDistributionPolicySync — the versioned Channel Package file
# is authoritative for platform_deliverable_policy, never the database.
# Fixed 2026-08-23, see docs/operations/audits/
# ZERNIO_CANONICAL_METADATA_CONTRACT_2026-08-23.md item 8: the runtime
# database had drifted ahead of platform_distribution_policy_v1.json
# (TikTok consent fields were added directly via
# update_channel_operations_profile(), bypassing the versioned file
# entirely). All fixtures here use an isolated tmp_path packages_root --
# never channel_packages/hypogeum/ and never .hypogeum-assets/hypogeum.db.
# ===========================================================================



def _write_policy_package(packages_root: Path, channel_id: str, policy_doc: dict):
    pkg_dir = packages_root / channel_id
    pkg_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = pkg_dir / "platform_distribution_policy_v1.json"
    artifact_bytes = json.dumps(policy_doc, sort_keys=True).encode()
    artifact_path.write_bytes(artifact_bytes)

    manifest = {
        "channel_id": channel_id,
        "package_version": "1.0.0",
        "artifacts": [{
            "path": "platform_distribution_policy_v1.json",
            "role": "platform_distribution_policy",
            "policy_id": f"{channel_id}_platform_distribution_policy_v1",
            "policy_version": policy_doc.get("policy_version", "1.0.0"),
            "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        }],
    }
    (pkg_dir / "channel_package_manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    return artifact_path, artifact_bytes


_MINIMAL_POLICY_DOC = {
    "channel_id": "hypogeum",
    "policy_version": "1.0.0",
    "platform_deliverable_policy": {
        "youtube": {
            "main_episode": {"enabled": True, "categoryId": "27", "ai_disclosure": True},
            "shorts": {"enabled": True, "categoryId": "27", "ai_disclosure": True},
        },
    },
}


class TestPlatformDistributionPolicySync:

    def test_load_reads_live_file_and_verifies_checksum(self, tmp_path):
        _write_policy_package(tmp_path, "hypogeum", _MINIMAL_POLICY_DOC)
        doc = load_platform_distribution_policy("hypogeum", packages_root=tmp_path)
        assert doc["platform_deliverable_policy"]["youtube"]["main_episode"]["categoryId"] == "27"

    def test_editing_artifact_without_bumping_manifest_checksum_fails_closed(self, tmp_path):
        artifact_path, _ = _write_policy_package(tmp_path, "hypogeum", _MINIMAL_POLICY_DOC)
        # Simulate drift: the file on disk changes, but the manifest's
        # registered sha256 is never updated to match.
        edited = dict(_MINIMAL_POLICY_DOC)
        edited["platform_deliverable_policy"] = dict(edited["platform_deliverable_policy"])
        edited["platform_deliverable_policy"]["youtube"] = {
            "main_episode": {"enabled": True, "categoryId": "99"},
        }
        artifact_path.write_bytes(json.dumps(edited, sort_keys=True).encode())

        with pytest.raises(ChannelPackagePolicyDriftError, match="no longer matches"):
            load_platform_distribution_policy("hypogeum", packages_root=tmp_path)

    def test_missing_manifest_entry_fails_closed(self, tmp_path):
        pkg_dir = tmp_path / "hypogeum"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "channel_package_manifest.json").write_text(
            json.dumps({"channel_id": "hypogeum", "package_version": "1.0.0", "artifacts": []})
        )
        with pytest.raises(ChannelPackagePolicyDriftError, match="no 'platform_distribution_policy' artifact"):
            load_platform_distribution_policy("hypogeum", packages_root=tmp_path)

    def test_missing_artifact_file_fails_closed(self, tmp_path):
        pkg_dir = tmp_path / "hypogeum"
        pkg_dir.mkdir(parents=True)
        manifest = {
            "channel_id": "hypogeum", "package_version": "1.0.0",
            "artifacts": [{
                "path": "platform_distribution_policy_v1.json",
                "role": "platform_distribution_policy",
                "sha256": "0" * 64,
            }],
        }
        (pkg_dir / "channel_package_manifest.json").write_text(json.dumps(manifest))
        with pytest.raises(ChannelPackagePolicyDriftError, match="unreadable"):
            load_platform_distribution_policy("hypogeum", packages_root=tmp_path)

    def test_malformed_json_fails_closed(self, tmp_path):
        pkg_dir = tmp_path / "hypogeum"
        pkg_dir.mkdir(parents=True)
        artifact_path = pkg_dir / "platform_distribution_policy_v1.json"
        artifact_bytes = b"{not valid json"
        artifact_path.write_bytes(artifact_bytes)
        manifest = {
            "channel_id": "hypogeum", "package_version": "1.0.0",
            "artifacts": [{
                "path": "platform_distribution_policy_v1.json",
                "role": "platform_distribution_policy",
                "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            }],
        }
        (pkg_dir / "channel_package_manifest.json").write_text(json.dumps(manifest))
        with pytest.raises(ChannelPackagePolicyDriftError, match="not valid JSON"):
            load_platform_distribution_policy("hypogeum", packages_root=tmp_path)

    def test_sync_compiles_the_verified_file_into_the_runtime_profile(
        self, tmp_path, db_path, canonical_channel,
    ):
        create_channel_operations_profile(db_path=db_path, **_valid_kwargs())
        _write_policy_package(tmp_path, "hypogeum", _MINIMAL_POLICY_DOC)

        profile = sync_platform_distribution_policy_to_runtime(
            "hypogeum", packages_root=tmp_path, db_path=db_path,
        )
        assert profile.platform_deliverable_policy["youtube"]["main_episode"]["categoryId"] == "27"
        assert profile.platform_deliverable_policy["youtube"]["shorts"]["ai_disclosure"] is True

        # Re-read from the database directly -- proves it actually
        # persisted, not just returned in-memory.
        reread = get_channel_operations_profile("hypogeum", db_path=db_path)
        assert reread.platform_deliverable_policy["youtube"]["main_episode"]["categoryId"] == "27"

    def test_sync_never_writes_to_the_database_when_the_file_fails_verification(
        self, tmp_path, db_path, canonical_channel,
    ):
        create_channel_operations_profile(db_path=db_path, **_valid_kwargs())
        before = get_channel_operations_profile("hypogeum", db_path=db_path)
        # No package written at tmp_path at all -- manifest missing entirely.

        with pytest.raises(ChannelPackagePolicyDriftError):
            sync_platform_distribution_policy_to_runtime(
                "hypogeum", packages_root=tmp_path, db_path=db_path,
            )

        after = get_channel_operations_profile("hypogeum", db_path=db_path)
        assert after.platform_deliverable_policy == before.platform_deliverable_policy

    def test_real_hypogeum_channel_package_file_verifies_against_its_own_manifest(self):
        # The real, checked-in channel_packages/hypogeum/ directory --
        # proves the actual production artifact this repo ships passes
        # its own checksum verification, not just a synthetic fixture.
        doc = load_platform_distribution_policy("hypogeum")
        policy = doc["platform_deliverable_policy"]
        assert policy["youtube"]["main_episode"]["categoryId"] == "27"
        assert policy["youtube"]["main_episode"]["ai_disclosure"] is True
        assert policy["youtube"]["shorts"]["categoryId"] == "27"
        assert policy["youtube"]["shorts"]["ai_disclosure"] is True
        # The database drift this fix closed: TikTok's consent fields
        # must still be present in the versioned file, not lost.
        assert policy["tiktok"]["shorts"]["content_preview_confirmed"] is True
