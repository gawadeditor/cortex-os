"""
tests/test_zernio_platform_contracts.py — hypogeum.publishing.
zernio_platform_contracts: provider-documented platform contracts and
the Channel-Package-policy-driven settings translation
(build_platform_settings()).

Provider-generic, channel-agnostic: every test uses a synthetic
PlatformContract or the real "tiktok" contract, but no test asserts on
Hypogeum-specific policy VALUES as if they were baked into this module
-- only that the documented FIELD NAMES/mechanism are correct and that
values always come from the policy_entry a caller supplies.
"""

from __future__ import annotations

from hypogeum.publishing.zernio_platform_contracts import (
    PlatformContract,
    build_platform_settings,
    get_platform_contract,
)


class TestPolicyPassthroughIsGeneric:
    def test_passthrough_field_copied_only_when_present_in_policy(self):
        contract = PlatformContract(
            platform="fake", verified=True, duration_seconds_range=None,
            max_size_bytes=None, aspect_ratio=None, caption_field="content",
            caption_max_chars=None, title_supported=False, title_field=None,
            privacy_required=False, privacy_field=None, privacy_allowed_values=None,
            scheduling_supported=True, thumbnail_supported=False, thumbnail_field=None,
            policy_passthrough_fields=("some_flag",), settings_body_key="fakeSettings",
        )
        import hypogeum.publishing.zernio_platform_contracts as mod
        mod.ZERNIO_PLATFORM_CONTRACTS["fake"] = contract
        try:
            assert build_platform_settings("fake", {"enabled": True}) is None
            assert build_platform_settings("fake", {"enabled": True, "some_flag": True}) == {"some_flag": True}
            assert build_platform_settings("fake", {"enabled": True, "some_flag": False}) == {"some_flag": False}
        finally:
            del mod.ZERNIO_PLATFORM_CONTRACTS["fake"]

    def test_never_invents_a_value_for_an_unconfigured_passthrough_field(self):
        contract = PlatformContract(
            platform="fake2", verified=True, duration_seconds_range=None,
            max_size_bytes=None, aspect_ratio=None, caption_field="content",
            caption_max_chars=None, title_supported=False, title_field=None,
            privacy_required=False, privacy_field=None, privacy_allowed_values=None,
            scheduling_supported=True, thumbnail_supported=False, thumbnail_field=None,
            policy_passthrough_fields=("a", "b"), settings_body_key="fakeSettings",
        )
        import hypogeum.publishing.zernio_platform_contracts as mod
        mod.ZERNIO_PLATFORM_CONTRACTS["fake2"] = contract
        try:
            result = build_platform_settings("fake2", {"enabled": True, "a": True})
            assert result == {"a": True}
            assert "b" not in result
        finally:
            del mod.ZERNIO_PLATFORM_CONTRACTS["fake2"]


class TestTikTokRealContract:
    def test_tiktok_contract_has_no_hardcoded_required_settings(self):
        """content_preview_confirmed/express_consent_given moved to
        policy-driven fields -- Factory Core no longer asserts consent on
        any channel's behalf."""
        contract = get_platform_contract("tiktok")
        assert contract.required_settings == {}

    def test_tiktok_documented_passthrough_fields(self):
        contract = get_platform_contract("tiktok")
        for field_name in (
            "content_preview_confirmed", "express_consent_given",
            "allow_comment", "allow_duet", "allow_stitch",
        ):
            assert field_name in contract.policy_passthrough_fields
            assert field_name in contract.required_policy_fields

    def test_build_settings_omits_everything_when_policy_has_nothing(self):
        assert build_platform_settings("tiktok", {"enabled": True}) is None

    def test_build_settings_includes_only_configured_fields(self):
        result = build_platform_settings("tiktok", {
            "enabled": True, "privacy": "PUBLIC_TO_EVERYONE",
            "content_preview_confirmed": True, "express_consent_given": True,
        })
        assert result == {
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "content_preview_confirmed": True,
            "express_consent_given": True,
        }
        assert "allow_comment" not in result

    def test_build_settings_full_authorized_hypogeum_policy(self):
        policy_entry = {
            "enabled": True, "privacy": "PUBLIC_TO_EVERYONE",
            "content_preview_confirmed": True, "express_consent_given": True,
            "allow_comment": True, "allow_duet": True, "allow_stitch": True,
        }
        result = build_platform_settings("tiktok", policy_entry)
        assert result == {
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "content_preview_confirmed": True,
            "express_consent_given": True,
            "allow_comment": True,
            "allow_duet": True,
            "allow_stitch": True,
        }

    def test_never_sets_commercial_content_type(self):
        """Branded-content declaration is never invented -- this contract
        does not even list commercialContentType as a passthrough field,
        so it can never appear regardless of what a policy dict contains
        unless a future, explicit contract change adds it."""
        policy_entry = {
            "enabled": True, "privacy": "PUBLIC_TO_EVERYONE",
            "content_preview_confirmed": True, "express_consent_given": True,
            "allow_comment": True, "allow_duet": True, "allow_stitch": True,
            "commercialContentType": "brand_content",  # even if a caller supplies it
        }
        result = build_platform_settings("tiktok", policy_entry)
        assert "commercialContentType" not in result
