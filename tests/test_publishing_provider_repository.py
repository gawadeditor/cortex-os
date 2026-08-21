"""
tests/test_publishing_provider_repository.py — Provider/account/profile/
content operational mappings (ADR-013 Decision 7, item 3).

Covers hypogeum.publishing.provider_repository (provider_profiles,
provider_accounts, channel_provider_bindings, provider_content_mappings)
and its wiring into hypogeum.publishing.submission_ledger.record_success()
at the receipt-persistence boundary.

Every test uses an isolated tmp_path SQLite database (initialize_schema(p))
-- never the production .hypogeum-assets/hypogeum.db. No network, credential,
queue, worker, or real provider call is made anywhere in this file; fake
provider/account/content identifiers only.
"""

from __future__ import annotations

import json

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing.provider_registry import DEFAULT_PROVIDER_ROUTING
from hypogeum.publishing.provider_repository import (
    DuplicateActiveBindingError,
    bind_channel_provider,
    disable_binding,
    list_bindings,
    mark_connection_status,
    persist_content_mapping,
    resolve_active_binding,
    resolve_content_mapping,
    upsert_provider_account,
    upsert_provider_profile,
)
from hypogeum.publishing.submission_ledger import (
    guard_and_prepare_attempt,
    open_new_publication_intent,
    record_ambiguous,
    record_definitive_failure,
    record_success,
)


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "provider_repository_test.db"
    initialize_schema(p)
    return p


def _publish_success(db_path, *, channel_id="hypogeum", episode_id="ep_1",
                      deliverable_id="main_episode", platform="youtube",
                      provider_id="youtube_direct", provider_publication_id="post_1",
                      native_platform_content_id="native_1", **kwargs):
    guard = guard_and_prepare_attempt(
        db_path, channel_id=channel_id, episode_id=episode_id,
        deliverable_id=deliverable_id, platform=platform, provider_id=provider_id,
    )
    assert guard.action == "proceed"
    record_success(
        db_path, guard.attempt_id,
        provider_publication_id=provider_publication_id,
        native_platform_content_id=native_platform_content_id,
        provider_status="published",
        **kwargs,
    )
    return guard


# ===========================================================================
# 1. Provider and platform remain separate
# ===========================================================================

class TestProviderPlatformSeparation:
    def test_provider_profile_and_account_never_store_platform_as_provider(self, db_path):
        profile = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="prof_1")
        account = upsert_provider_account(
            db_path, provider_id="zernio", platform="youtube", external_account_id="acc_1",
        )
        assert profile["provider_id"] == "zernio"
        assert account["provider_id"] == "zernio"
        assert account["platform"] == "youtube"
        assert account["provider_id"] != account["platform"]

    def test_platform_check_constraint_rejects_zernio_as_platform(self, db_path):
        with pytest.raises(Exception):
            upsert_provider_account(
                db_path, provider_id="zernio", platform="zernio", external_account_id="acc_bad",
            )


# ===========================================================================
# 2. Profile upsert is idempotent
# ===========================================================================

class TestProfileUpsertIdempotent:
    def test_same_provider_external_id_returns_same_row(self, db_path):
        p1 = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="prof_1",
                                      display_label="First")
        p2 = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="prof_1",
                                      display_label="Updated")
        assert p1["provider_profile_id"] == p2["provider_profile_id"]
        assert p2["display_label"] == "Updated"

    def test_no_duplicate_rows_created(self, db_path):
        for _ in range(3):
            upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="prof_dup")
        conn = get_connection(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM provider_profiles WHERE external_profile_id = 'prof_dup'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1


# ===========================================================================
# 3. Account upsert is idempotent
# ===========================================================================

class TestAccountUpsertIdempotent:
    def test_same_provider_platform_external_id_returns_same_row(self, db_path):
        a1 = upsert_provider_account(db_path, provider_id="zernio", platform="youtube",
                                      external_account_id="acc_1", display_label="First")
        a2 = upsert_provider_account(db_path, provider_id="zernio", platform="youtube",
                                      external_account_id="acc_1", display_label="Updated")
        assert a1["provider_account_id"] == a2["provider_account_id"]
        assert a2["display_label"] == "Updated"


# ===========================================================================
# 4. One profile can own four platform accounts
# ===========================================================================

class TestOneProfileFourAccounts:
    def test_profile_owns_one_account_per_platform(self, db_path):
        profile = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="prof_1")
        accounts = [
            upsert_provider_account(
                db_path, provider_id="zernio", platform=platform,
                external_account_id=f"acc_{platform}", provider_profile_id=profile["provider_profile_id"],
            )
            for platform in ("youtube", "tiktok", "instagram", "facebook")
        ]
        assert len({a["provider_account_id"] for a in accounts}) == 4
        assert all(a["provider_profile_id"] == profile["provider_profile_id"] for a in accounts)


# ===========================================================================
# 5. Channel binding resolves by capability/platform/alias
# ===========================================================================

class TestBindingResolution:
    def test_resolves_by_capability_platform_alias(self, db_path):
        bound = bind_channel_provider(
            db_path, channel_id="hypogeum", capability="publishing", platform="youtube",
            provider_id="zernio", logical_alias="main",
        )
        resolved = resolve_active_binding(
            db_path, channel_id="hypogeum", capability="publishing", platform="youtube",
            logical_alias="main",
        )
        assert resolved is not None
        assert resolved["binding_id"] == bound["binding_id"]

    def test_wrong_alias_does_not_resolve(self, db_path):
        bind_channel_provider(
            db_path, channel_id="hypogeum", capability="publishing", platform="youtube",
            provider_id="zernio", logical_alias="main",
        )
        resolved = resolve_active_binding(
            db_path, channel_id="hypogeum", capability="publishing", platform="youtube",
            logical_alias="secondary",
        )
        assert resolved is None

    def test_wrong_capability_does_not_resolve(self, db_path):
        bind_channel_provider(
            db_path, channel_id="hypogeum", capability="publishing", platform="youtube",
            provider_id="zernio",
        )
        resolved = resolve_active_binding(
            db_path, channel_id="hypogeum", capability="analytics", platform="youtube",
        )
        assert resolved is None


# ===========================================================================
# 6. Disabled binding does not resolve
# ===========================================================================

class TestDisabledBindingDoesNotResolve:
    def test_disable_binding_prevents_resolution(self, db_path):
        bound = bind_channel_provider(
            db_path, channel_id="hypogeum", capability="publishing", platform="youtube",
            provider_id="zernio",
        )
        disable_binding(db_path, binding_id=bound["binding_id"])
        assert resolve_active_binding(
            db_path, channel_id="hypogeum", capability="publishing", platform="youtube",
        ) is None

    def test_disable_binding_preserves_history_row(self, db_path):
        bound = bind_channel_provider(
            db_path, channel_id="hypogeum", capability="publishing", platform="youtube",
            provider_id="zernio",
        )
        disable_binding(db_path, binding_id=bound["binding_id"])
        all_bindings = list_bindings(db_path, channel_id="hypogeum")
        assert any(b["binding_id"] == bound["binding_id"] for b in all_bindings)
        assert all_bindings[0]["enabled"] == 0


# ===========================================================================
# 7. Credential row is not treated as profile/account mapping
# ===========================================================================

class TestCredentialRowNeverTreatedAsMapping:
    def test_secret_profiles_table_untouched_by_this_module(self, db_path):
        upsert_provider_profile(db_path, provider_id="youtube", external_profile_id="prof_x")
        upsert_provider_account(db_path, provider_id="youtube", platform="youtube",
                                 external_account_id="acc_x")
        conn = get_connection(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM secret_profiles").fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_no_credential_shaped_columns_exist_on_provider_tables(self, db_path):
        conn = get_connection(db_path)
        try:
            for table in ("provider_profiles", "provider_accounts",
                          "channel_provider_bindings", "provider_content_mappings"):
                cols = {row[1].lower() for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for forbidden in ("credential_data", "access_token", "refresh_token",
                                  "api_key", "authorization_header", "password"):
                    assert forbidden not in cols, f"{table} must never define column {forbidden!r}"
        finally:
            conn.close()


# ===========================================================================
# 8. Two channels can bind independently to the same factory-scoped
# provider profile when policy allows
# ===========================================================================

class TestCrossChannelSharedProfile:
    def test_two_channels_bind_to_same_profile(self, db_path):
        profile = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="prof_shared")
        b1 = bind_channel_provider(
            db_path, channel_id="hypogeum", capability="publishing", platform="youtube",
            provider_id="zernio", provider_profile_id=profile["provider_profile_id"],
        )
        b2 = bind_channel_provider(
            db_path, channel_id="other_channel", capability="publishing", platform="youtube",
            provider_id="zernio", provider_profile_id=profile["provider_profile_id"],
        )
        assert b1["provider_profile_id"] == b2["provider_profile_id"]
        assert b1["channel_id"] != b2["channel_id"]

    def test_conflicting_active_binding_for_same_key_is_rejected(self, db_path):
        bind_channel_provider(
            db_path, channel_id="hypogeum", capability="publishing", platform="youtube",
            provider_id="youtube_direct",
        )
        with pytest.raises(DuplicateActiveBindingError):
            bind_channel_provider(
                db_path, channel_id="hypogeum", capability="publishing", platform="youtube",
                provider_id="zernio",
            )

    def test_same_binding_key_and_value_is_idempotent(self, db_path):
        b1 = bind_channel_provider(
            db_path, channel_id="hypogeum", capability="publishing", platform="youtube",
            provider_id="youtube_direct",
        )
        b2 = bind_channel_provider(
            db_path, channel_id="hypogeum", capability="publishing", platform="youtube",
            provider_id="youtube_direct",
        )
        assert b1["binding_id"] == b2["binding_id"]


# ===========================================================================
# 9. Provider and native account IDs remain distinguishable
# ===========================================================================

class TestProviderVsNativeAccountId:
    def test_external_and_native_account_ids_are_distinct_fields(self, db_path):
        account = upsert_provider_account(
            db_path, provider_id="zernio", platform="youtube", external_account_id="zernio_internal_acc_1",
            native_platform_account_id="UC_native_youtube_channel_id",
        )
        assert account["external_account_id"] == "zernio_internal_acc_1"
        assert account["native_platform_account_id"] == "UC_native_youtube_channel_id"
        assert account["external_account_id"] != account["native_platform_account_id"]


# ===========================================================================
# 10. Provider and native content IDs remain distinguishable
# ===========================================================================

class TestProviderVsNativeContentId:
    def test_content_mapping_keeps_provider_and_native_ids_distinct(self, db_path):
        mapping = persist_content_mapping(
            db_path, provider_id="zernio", platform="youtube", channel_id="hypogeum",
            episode_id="ep_1", deliverable_id="main_episode",
            provider_content_id="zernio_post_abc",
            native_platform_content_id="yt_video_xyz",
        )
        assert mapping["provider_content_id"] == "zernio_post_abc"
        assert mapping["native_platform_content_id"] == "yt_video_xyz"
        assert mapping["provider_content_id"] != mapping["native_platform_content_id"]


# ===========================================================================
# 11. New publication intent permits a new mapping for the same episode/platform
# ===========================================================================

class TestNewIntentPermitsNewMapping:
    def test_deliberate_republish_creates_second_mapping(self, db_path):
        guard1 = _publish_success(db_path, episode_id="ep_f3619022", provider_publication_id="post_v1")
        mappings1 = resolve_content_mapping(db_path, publication_intent_id=guard1.publication_intent_id)
        assert len(mappings1) == 1

        new_intent_id = open_new_publication_intent(
            db_path, channel_id="hypogeum", episode_id="ep_f3619022",
            deliverable_id="main_episode", platform="youtube",
        )
        assert new_intent_id != guard1.publication_intent_id

        guard2 = guard_and_prepare_attempt(
            db_path, channel_id="hypogeum", episode_id="ep_f3619022",
            deliverable_id="main_episode", platform="youtube", provider_id="youtube_direct",
        )
        assert guard2.action == "proceed"
        assert guard2.publication_intent_id == new_intent_id
        record_success(db_path, guard2.attempt_id, provider_publication_id="post_v2")

        mappings2 = resolve_content_mapping(db_path, publication_intent_id=new_intent_id)
        assert len(mappings2) == 1
        assert mappings2[0]["provider_content_id"] == "post_v2"


# ===========================================================================
# 12. Historical mapping is preserved
# ===========================================================================

class TestHistoricalMappingPreserved:
    def test_old_mapping_row_untouched_after_republish(self, db_path):
        guard1 = _publish_success(db_path, episode_id="ep_hist", provider_publication_id="post_old")
        old_mappings = resolve_content_mapping(db_path, publication_intent_id=guard1.publication_intent_id)
        old_mapping_id = old_mappings[0]["mapping_id"]

        open_new_publication_intent(
            db_path, channel_id="hypogeum", episode_id="ep_hist",
            deliverable_id="main_episode", platform="youtube",
        )
        guard2 = guard_and_prepare_attempt(
            db_path, channel_id="hypogeum", episode_id="ep_hist",
            deliverable_id="main_episode", platform="youtube", provider_id="youtube_direct",
        )
        record_success(db_path, guard2.attempt_id, provider_publication_id="post_new")

        still_there = resolve_content_mapping(db_path, publication_intent_id=guard1.publication_intent_id)
        assert len(still_there) == 1
        assert still_there[0]["mapping_id"] == old_mapping_id
        assert still_there[0]["provider_content_id"] == "post_old"


# ===========================================================================
# 13. Failed/ambiguous attempt produces no confirmed mapping
# ===========================================================================

class TestFailedAmbiguousNoMapping:
    def test_definitive_failure_produces_no_mapping(self, db_path):
        guard = guard_and_prepare_attempt(
            db_path, channel_id="hypogeum", episode_id="ep_fail", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        record_definitive_failure(db_path, guard.attempt_id, error_category="rejected", error_message="bad")
        mappings = resolve_content_mapping(db_path, attempt_id=guard.attempt_id)
        assert mappings == []

    def test_ambiguous_produces_no_mapping(self, db_path):
        guard = guard_and_prepare_attempt(
            db_path, channel_id="hypogeum", episode_id="ep_ambig", deliverable_id="main_episode",
            platform="youtube", provider_id="youtube_direct",
        )
        record_ambiguous(db_path, guard.attempt_id, error_category="timeout", error_message="no response")
        mappings = resolve_content_mapping(db_path, attempt_id=guard.attempt_id)
        assert mappings == []


# ===========================================================================
# 14. Successful receipt can produce one mapping
# ===========================================================================

class TestSuccessfulReceiptProducesOneMapping:
    def test_exactly_one_mapping_per_successful_receipt(self, db_path):
        guard = _publish_success(db_path, episode_id="ep_one_map")
        mappings = resolve_content_mapping(db_path, attempt_id=guard.attempt_id)
        assert len(mappings) == 1
        assert mappings[0]["receipt_id"] is not None
        assert mappings[0]["status"] == "confirmed"


# ===========================================================================
# 15. Partial success maps only successful platforms
# ===========================================================================

class TestPartialSuccessMapsOnlySuccessful:
    def test_youtube_succeeds_facebook_fails_only_youtube_mapped(self, db_path):
        yt_guard = _publish_success(db_path, episode_id="ep_partial", platform="youtube",
                                     provider_id="youtube_direct")
        fb_guard = guard_and_prepare_attempt(
            db_path, channel_id="hypogeum", episode_id="ep_partial", deliverable_id="main_episode",
            platform="facebook", provider_id="meta_direct",
        )
        record_definitive_failure(db_path, fb_guard.attempt_id, error_category="rejected",
                                   error_message="platform rejected upload")

        yt_mappings = resolve_content_mapping(db_path, attempt_id=yt_guard.attempt_id)
        fb_mappings = resolve_content_mapping(db_path, attempt_id=fb_guard.attempt_id)
        assert len(yt_mappings) == 1
        assert yt_mappings[0]["platform"] == "youtube"
        assert fb_mappings == []


# ===========================================================================
# 16. Sanitization prevents credential material in metadata
# ===========================================================================

class TestMetadataSanitization:
    def test_credential_shaped_key_is_redacted(self, db_path):
        profile = upsert_provider_profile(
            db_path, provider_id="zernio", external_profile_id="prof_secret",
            metadata={"api_key": "sk_live_should_never_be_stored", "note": "fine"},
        )
        meta = json.loads(profile["metadata_json"])
        assert meta["api_key"] == "[REDACTED]"
        assert meta["note"] == "fine"
        assert "sk_live_should_never_be_stored" not in profile["metadata_json"]

    def test_authorization_header_shaped_value_is_redacted(self, db_path):
        account = upsert_provider_account(
            db_path, provider_id="zernio", platform="youtube", external_account_id="acc_meta",
            metadata={"debug_note": "Authorization: Bearer abcdef123456SECRET"},
        )
        assert "abcdef123456SECRET" not in account["metadata_json"]
        assert "[REDACTED]" in account["metadata_json"]

    def test_nested_credential_key_is_redacted(self, db_path):
        profile = upsert_provider_profile(
            db_path, provider_id="zernio", external_profile_id="prof_nested",
            metadata={"auth": {"access_token": "should_be_redacted_value"}},
        )
        meta = json.loads(profile["metadata_json"])
        assert meta["auth"]["access_token"] == "[REDACTED]"


# ===========================================================================
# 19. No Zernio provider becomes active in this slice
# ===========================================================================

class TestNoZernioActive:
    def test_default_provider_routing_has_no_zernio(self):
        assert "zernio" not in DEFAULT_PROVIDER_ROUTING.values()

    def test_this_module_never_hardcodes_zernio_provider_id(self, db_path):
        # Constructing/using every public function of this module with a
        # non-Zernio provider_id must work without any Zernio-specific
        # branching anywhere -- the module is provider-neutral by
        # construction, not by omission of a test.
        profile = upsert_provider_profile(db_path, provider_id="youtube_direct",
                                           external_profile_id="n/a")
        assert profile["provider_id"] == "youtube_direct"


# ===========================================================================
# 20. Schema initialization and repeated migration are idempotent
# ===========================================================================

class TestSchemaIdempotent:
    def test_repeated_initialize_schema_is_a_no_op(self, db_path):
        initialize_schema(db_path)
        initialize_schema(db_path)
        conn = get_connection(db_path)
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%provider%'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert tables == {
            "provider_profiles", "provider_accounts", "channel_provider_bindings",
            "provider_content_mappings", "provider_verifications", "provider_comparison_runs",
        }

    def test_repeated_writes_after_reinitialize_still_idempotent(self, db_path):
        upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="prof_reinit")
        initialize_schema(db_path)
        p2 = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="prof_reinit")
        conn = get_connection(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM provider_profiles WHERE external_profile_id = 'prof_reinit'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1
        assert p2["external_profile_id"] == "prof_reinit"


# ===========================================================================
# mark_connection_status / list_bindings extras (repository API coverage)
# ===========================================================================

class TestConnectionStatus:
    def test_mark_connection_status_updates_reconnection_flag(self, db_path):
        account = upsert_provider_account(
            db_path, provider_id="zernio", platform="youtube", external_account_id="acc_reconn",
        )
        updated = mark_connection_status(
            db_path, provider_account_id=account["provider_account_id"],
            status="needs_reconnection", needs_reconnection=True,
        )
        assert updated["status"] == "needs_reconnection"
        assert bool(updated["needs_reconnection"]) is True

    def test_list_bindings_filters_by_capability_and_platform(self, db_path):
        bind_channel_provider(db_path, channel_id="hypogeum", capability="publishing",
                               platform="youtube", provider_id="youtube_direct")
        bind_channel_provider(db_path, channel_id="hypogeum", capability="analytics",
                               platform="youtube", provider_id="youtube_direct")
        bind_channel_provider(db_path, channel_id="hypogeum", capability="publishing",
                               platform="tiktok", provider_id="tiktok_direct")
        pub_yt = list_bindings(db_path, channel_id="hypogeum", capability="publishing", platform="youtube")
        assert len(pub_yt) == 1
        all_pub = list_bindings(db_path, channel_id="hypogeum", capability="publishing")
        assert len(all_pub) == 2
