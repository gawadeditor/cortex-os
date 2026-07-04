"""
tests/test_provider_registry.py — Phase 1A–1D provider registry tests

Tests:
  TestProviderCatalog (Phase 1A)
    test_openai_in_catalog_without_asset_versions
    test_openrouter_has_image_capability
    test_muapi_delegated_providers_labelled
    test_all_nine_required_providers_present
    test_catalog_shows_configured_openai_when_key_set

  TestThumbnailProviderDispatch (Phase 1A)
    test_openai_dispatch_calls_openai_function
    test_openrouter_dispatch_calls_openrouter_function
    test_muapi_default_dispatch_unchanged

  TestOpenRouterEndpointCorrectness (Phase 1B correction)
    test_openrouter_function_does_not_use_images_generations_endpoint
    test_openrouter_function_uses_chat_completions_endpoint
    test_openrouter_function_uses_modalities
    test_openrouter_function_does_not_use_response_format_b64_json_in_payload
    test_discover_openrouter_image_models_uses_correct_query_param

  TestProviderReadinessModel (Phase 1D)
    test_openai_appears_on_providers_page_with_operational_readiness
        OpenAI entry has both credential_status and operational_readiness.
    test_openrouter_card_shows_both_llm_and_image_capabilities
        OpenRouter capabilities include both "llm" and "image".
    test_muapi_operational_readiness_distinct_from_credential_status
        MUAPI has separate credential_status and operational_readiness fields.
    test_seedance_veo_kling_runway_have_both_access_modes
        Each provider has a muapi_delegated entry AND a direct_placeholder entry.
    test_direct_placeholders_have_credential_key_name
        Official/direct placeholder entries have credential_key_name set.
    test_muapi_delegated_readiness_inherits_from_muapi
        Delegated providers show muapi_dependent or ready_via_muapi based on MUAPI.
    test_smoke_test_success_sets_operational_readiness_ready
        A success smoke test result drives OR_READY for the provider.
    test_smoke_test_403_sets_auth_failed_readiness
        A 403 error in the smoke test drives OR_AUTH_FAILED for the provider.

    test_openrouter_dispatch_calls_openrouter_function
        generate_thumbnail() with THUMBNAIL_IMAGE_PROVIDER=openrouter calls
        _generate_thumbnail_openrouter(), not _attempt_thumbnail_generation().

    test_muapi_default_dispatch_unchanged
        generate_thumbnail() with THUMBNAIL_IMAGE_PROVIDER=muapi (default) calls
        _attempt_thumbnail_generation() — existing behaviour preserved.

No network calls. No API keys required. No image generation.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.config import Config
from hypogeum.dashboard_api import (
    ProviderCatalogEntry,
    get_provider_catalog,
    OR_READY,
    OR_NOT_TESTED,
    OR_AUTH_FAILED,
    OR_BILLING_FAILED,
    OR_GENERATION_FAILED,
    OR_SKIPPED_NO_KEY,
    OR_MUAPI_DEPENDENT,
    OR_READY_VIA_MUAPI,
    OR_MUAPI_NOT_READY,
    OR_NOT_CONFIGURED,
    _derive_operational_readiness,
    _PROVIDER_CATALOG_META,
)
import inspect

from hypogeum.muapi import (
    ThumbnailResult,
    _generate_thumbnail_openrouter,
    _discover_openrouter_image_models,
    generate_thumbnail,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> Config:
    """Minimal Config with no real credentials — only required field OPENROUTER_API_KEY."""
    env_base = {
        "OPENROUTER_API_KEY": "test-key-openrouter",
        "THUMBNAIL_IMAGE_PROVIDER": "muapi",
        "THUMBNAIL_IMAGE_MODEL": "gpt-image-2-text-to-image",
        "THUMBNAIL_FALLBACK_MODEL": "",
        "THUMBNAIL_IMAGE_QUALITY": "high",
        "THUMBNAIL_IMAGE_RESOLUTION": "2K",
        "OPENAI_IMAGE_BASE_URL": "https://api.openai.com/v1",
        "OPENAI_IMAGE_MODEL": "gpt-image-2",
        "OPENROUTER_IMAGE_MODEL": "openai/gpt-image-2",
    }
    env_base.update(overrides)
    with patch.dict(os.environ, env_base, clear=False):
        return Config()


# ---------------------------------------------------------------------------
# TestProviderCatalog
# ---------------------------------------------------------------------------

class TestProviderCatalog:

    def test_openai_in_catalog_without_asset_versions(self, tmp_path):
        """
        OpenAI must appear in get_provider_catalog() even when:
        - OPENAI_API_KEY is not set in the environment
        - The database has no asset_versions rows for 'openai'

        Previously get_provider_health() would omit OpenAI entirely because it
        only returned providers that had generated at least one asset.
        """
        db = tmp_path / "test.db"
        # Ensure OPENAI_API_KEY is absent
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            catalog = get_provider_catalog(db_path=db)

        providers = [e.provider for e in catalog]
        assert "openai" in providers, (
            f"'openai' not found in provider catalog. Got: {providers}"
        )

        openai_entry = next(e for e in catalog if e.provider == "openai")
        assert isinstance(openai_entry, ProviderCatalogEntry)
        assert openai_entry.display_name == "OpenAI"
        # No assets means 0 attempts
        assert openai_entry.total_attempts == 0
        # No key means not configured
        assert openai_entry.is_configured is False
        assert openai_entry.credential_status == "missing"

    def test_openrouter_has_image_capability(self, tmp_path):
        """
        OpenRouter ProviderCatalogEntry must list 'image' in its capabilities
        alongside 'llm', reflecting that THUMBNAIL_IMAGE_PROVIDER=openrouter is valid.
        """
        db = tmp_path / "test.db"
        catalog = get_provider_catalog(db_path=db)

        providers = {e.provider: e for e in catalog}
        assert "openrouter" in providers

        or_entry = providers["openrouter"]
        assert "image" in or_entry.capabilities, (
            f"Expected 'image' in openrouter capabilities, got {or_entry.capabilities}"
        )
        assert "llm" in or_entry.capabilities

    def test_muapi_delegated_providers_labelled(self, tmp_path):
        """
        Seedance, Veo, Kling, Runway must be labelled access_mode='muapi_delegated'
        and delegate_via='muapi' — they share the MUAPI credential and have no
        direct API of their own in the current implementation.
        """
        db = tmp_path / "test.db"
        catalog = get_provider_catalog(db_path=db)
        by_provider = {e.provider: e for e in catalog}

        for name in ("seedance", "veo", "kling", "runway"):
            assert name in by_provider, f"'{name}' missing from catalog"
            entry = by_provider[name]
            assert entry.access_mode == "muapi_delegated", (
                f"{name}.access_mode should be 'muapi_delegated', got {entry.access_mode!r}"
            )
            assert entry.delegate_via == "muapi", (
                f"{name}.delegate_via should be 'muapi', got {entry.delegate_via!r}"
            )

    def test_all_nine_required_providers_present(self, tmp_path):
        """All nine providers required by the spec are in the catalog."""
        db = tmp_path / "test.db"
        catalog = get_provider_catalog(db_path=db)
        found = {e.provider for e in catalog}
        required = {"openai", "openrouter", "muapi", "elevenlabs",
                    "freepik", "seedance", "veo", "kling", "runway"}
        assert required <= found, f"Missing providers: {required - found}"

    def test_catalog_shows_configured_openai_when_key_set(self, tmp_path):
        """
        When OPENAI_API_KEY is set, OpenAI entry shows has_env_var=True
        and is_configured=True even before first generation.
        """
        db = tmp_path / "test.db"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}, clear=False):
            catalog = get_provider_catalog(db_path=db)

        openai_entry = next(e for e in catalog if e.provider == "openai")
        assert openai_entry.has_env_var is True
        assert openai_entry.is_configured is True


# ---------------------------------------------------------------------------
# TestThumbnailProviderDispatch
# ---------------------------------------------------------------------------

class TestThumbnailProviderDispatch:

    def _fake_jpg(self, path: Path) -> None:
        """Write a minimal valid JPEG so size checks pass."""
        # 15 KB of dummy bytes — above _MIN_IMAGE_BYTES (10 KB)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * (15_000))

    def test_openai_dispatch_calls_openai_function(self, tmp_path):
        """
        generate_thumbnail() with THUMBNAIL_IMAGE_PROVIDER=openai must call
        _generate_thumbnail_openai() and return provider_used='openai'.
        The MUAPI path (_attempt_thumbnail_generation) must NOT be called.
        """
        config = _make_config(
            THUMBNAIL_IMAGE_PROVIDER="openai",
            OPENAI_API_KEY="sk-test",
        )
        output_dir = tmp_path

        fake_model = "gpt-image-2"

        with patch("hypogeum.muapi._generate_thumbnail_openai") as mock_openai, \
             patch("hypogeum.muapi._attempt_thumbnail_generation") as mock_muapi, \
             patch("hypogeum.muapi._normalize_thumbnail"), \
             patch("hypogeum.muapi._apply_wordmark_overlay"):

            mock_openai.side_effect = lambda **kw: (
                self._fake_jpg(kw["local_path"]) or fake_model
            )

            result = generate_thumbnail(
                deliverable_id="test_thumb",
                image_prompt="Ancient mechanism, dark chamber",
                aspect_ratio="16:9",
                config=config,
                episode_id="ep_test",
                output_dir=output_dir,
            )

        mock_openai.assert_called_once()
        mock_muapi.assert_not_called()
        assert result.provider_used == "openai"
        assert result.model_used == fake_model

    def test_openrouter_dispatch_calls_openrouter_function(self, tmp_path):
        """
        generate_thumbnail() with THUMBNAIL_IMAGE_PROVIDER=openrouter must call
        _generate_thumbnail_openrouter() and return provider_used='openrouter'.
        The MUAPI path must NOT be called.
        """
        config = _make_config(
            THUMBNAIL_IMAGE_PROVIDER="openrouter",
            OPENROUTER_API_KEY="sk-or-test",
        )
        output_dir = tmp_path

        fake_model = "openai/gpt-image-2"

        with patch("hypogeum.muapi._generate_thumbnail_openrouter") as mock_or, \
             patch("hypogeum.muapi._attempt_thumbnail_generation") as mock_muapi, \
             patch("hypogeum.muapi._normalize_thumbnail"), \
             patch("hypogeum.muapi._apply_wordmark_overlay"):

            mock_or.side_effect = lambda **kw: (
                self._fake_jpg(kw["local_path"]) or fake_model
            )

            result = generate_thumbnail(
                deliverable_id="test_thumb",
                image_prompt="Ancient mechanism, dark chamber",
                aspect_ratio="16:9",
                config=config,
                episode_id="ep_test",
                output_dir=output_dir,
            )

        mock_or.assert_called_once()
        mock_muapi.assert_not_called()
        assert result.provider_used == "openrouter"
        assert result.model_used == fake_model

    def test_muapi_default_dispatch_unchanged(self, tmp_path):
        """
        generate_thumbnail() with THUMBNAIL_IMAGE_PROVIDER=muapi (the default)
        must call _attempt_thumbnail_generation() — the existing MUAPI path.
        The OpenAI and OpenRouter functions must NOT be called.
        """
        config = _make_config(
            THUMBNAIL_IMAGE_PROVIDER="muapi",
            MUAPI_API_KEY="muapi-test-key",
        )
        output_dir = tmp_path

        fake_result = ("gpt-image-2-text-to-image", False)

        # _attempt_thumbnail_generation writes the file as a side-effect;
        # we need to create the file so ThumbnailResult.local_path exists.
        def _fake_attempt(**kw):
            out = output_dir / "thumbnails" / "test_thumb.jpg"
            self._fake_jpg(out)
            return fake_result

        with patch("hypogeum.muapi._attempt_thumbnail_generation",
                   side_effect=_fake_attempt) as mock_muapi, \
             patch("hypogeum.muapi._generate_thumbnail_openai") as mock_openai, \
             patch("hypogeum.muapi._generate_thumbnail_openrouter") as mock_or:

            result = generate_thumbnail(
                deliverable_id="test_thumb",
                image_prompt="Ancient mechanism, dark chamber",
                aspect_ratio="16:9",
                config=config,
                episode_id="ep_test",
                output_dir=output_dir,
            )

        mock_muapi.assert_called_once()
        mock_openai.assert_not_called()
        mock_or.assert_not_called()
        assert result.provider_used == "muapi"


# ---------------------------------------------------------------------------
# TestOpenRouterEndpointCorrectness
# ---------------------------------------------------------------------------

class TestOpenRouterEndpointCorrectness:
    """
    Phase 1B correction: OpenRouter image generation must use
    POST /api/v1/chat/completions with modalities, NOT /api/v1/images/generations.
    """

    def test_openrouter_function_does_not_use_images_generations_endpoint(self):
        """
        Inspect the source of _generate_thumbnail_openrouter to assert that
        /api/v1/images/generations does not appear anywhere in the implementation.

        This test fails if someone accidentally reverts to the invalid endpoint.
        """
        source = inspect.getsource(_generate_thumbnail_openrouter)
        assert "/api/v1/images/generations" not in source, (
            "_generate_thumbnail_openrouter must NOT use /api/v1/images/generations. "
            "OpenRouter image generation uses POST /api/v1/chat/completions with modalities."
        )

    def test_openrouter_function_uses_chat_completions_endpoint(self):
        """
        Inspect the source of _generate_thumbnail_openrouter to assert that
        /api/v1/chat/completions IS used.
        """
        source = inspect.getsource(_generate_thumbnail_openrouter)
        assert "/api/v1/chat/completions" in source, (
            "_generate_thumbnail_openrouter must use POST /api/v1/chat/completions. "
            "Found source does not contain the correct endpoint."
        )

    def test_openrouter_function_uses_modalities(self):
        """
        Inspect the source to assert that modalities: ["image"] is present in payload.
        """
        source = inspect.getsource(_generate_thumbnail_openrouter)
        assert "modalities" in source, (
            "_generate_thumbnail_openrouter payload must include 'modalities' key "
            "per OpenRouter image generation API."
        )

    def test_openrouter_function_does_not_use_response_format_b64_json_in_payload(self):
        """
        The old /images/generations endpoint used response_format='b64_json' in the payload.
        The chat/completions endpoint does not use this field.
        Inspecting payload construction to ensure it's absent from the dict literal.
        """
        source = inspect.getsource(_generate_thumbnail_openrouter)
        # The payload dict should NOT contain "response_format" as a key
        # (that was an /images/generations field)
        # We check the payload construction specifically — the string "response_format"
        # might appear in comments, so we look for the dict key pattern.
        assert '"response_format"' not in source, (
            "_generate_thumbnail_openrouter should not send 'response_format' in payload. "
            "That field belongs to the old /images/generations API, not chat/completions."
        )

    def test_discover_openrouter_image_models_uses_correct_query_param(self):
        """
        _discover_openrouter_image_models() must call the model discovery endpoint
        with output_modalities=image query parameter.
        """
        source = inspect.getsource(_discover_openrouter_image_models)
        assert "output_modalities=image" in source, (
            "_discover_openrouter_image_models must call "
            "GET /api/v1/models?output_modalities=image"
        )


# ---------------------------------------------------------------------------
# TestProviderReadinessModel  (Phase 1D)
# ---------------------------------------------------------------------------

class TestProviderReadinessModel:
    """Phase 1D: two-row status model and direct-placeholder entries."""

    def test_openai_appears_on_providers_page_with_operational_readiness(self, tmp_path):
        """
        OpenAI entry must have both credential_status and operational_readiness.
        Both fields are present and non-empty even with no env key and no smoke test.
        """
        db = tmp_path / "test.db"
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            catalog = get_provider_catalog(db_path=db)

        entry = next((e for e in catalog if e.provider == "openai"), None)
        assert entry is not None, "OpenAI not found in provider catalog"
        assert entry.credential_status, "credential_status must be non-empty"
        assert entry.operational_readiness, "operational_readiness must be non-empty"
        # These are two distinct fields with independent values
        assert hasattr(entry, "credential_status")
        assert hasattr(entry, "operational_readiness")

    def test_openrouter_card_shows_both_llm_and_image_capabilities(self, tmp_path):
        """
        OpenRouter must list both 'llm' and 'image' in capabilities — it handles
        both chat completions (LLM routing) and image generation.
        """
        db = tmp_path / "test.db"
        catalog = get_provider_catalog(db_path=db)
        entry = next((e for e in catalog if e.provider == "openrouter"), None)
        assert entry is not None
        assert "llm" in entry.capabilities, (
            f"OpenRouter must list 'llm' capability. Got: {entry.capabilities}"
        )
        assert "image" in entry.capabilities, (
            f"OpenRouter must list 'image' capability. Got: {entry.capabilities}"
        )

    def test_muapi_operational_readiness_distinct_from_credential_status(self, tmp_path):
        """
        MUAPI has two separate status dimensions:
          credential_status     — result of the lightweight probe (e.g. probe endpoint 404)
          operational_readiness — result of smoke test generation attempt (e.g. auth_failed)

        These must be different fields and must not be the same string
        when a smoke test has been run with a credential failure.
        The test injects a smoke result showing 403 and verifies both are present.
        """
        import json as _json

        # Write a fake smoke report showing MUAPI had a 403 on generation
        smoke_dir = tmp_path / ".hypogeum-assets" / "provider_smoke_tests"
        smoke_dir.mkdir(parents=True)
        report = {
            "providers": {
                "muapi": {
                    "provider": "muapi",
                    "status": "error_submit",
                    "model": "gpt-image-2-text-to-image",
                    "endpoint": "https://api.muapi.ai/api/v1/gpt-image-2-text-to-image",
                    "latency_ms": 1200,
                    "error": "403 Client Error: Forbidden",
                }
            }
        }
        (smoke_dir / "provider_live_smoke_report.json").write_text(_json.dumps(report))

        # Patch _load_smoke_test_results_with_age (what get_provider_catalog actually calls).
        # Returns (providers_dict, age_days) — age_days=1 keeps smoke fresh (not stale).
        with patch(
            "hypogeum.dashboard_api._load_smoke_test_results_with_age",
            return_value=(report["providers"], 1),
        ):
            db = tmp_path / "test.db"
            catalog = get_provider_catalog(db_path=db)

        muapi = next((e for e in catalog if e.provider == "muapi"), None)
        assert muapi is not None
        # Both fields must exist
        assert hasattr(muapi, "credential_status")
        assert hasattr(muapi, "operational_readiness")
        # 403 is model/tier access denial — classified as OR_GENERATION_FAILED (not OR_AUTH_FAILED).
        # Only 401 (credential rejection) maps to OR_AUTH_FAILED.
        assert muapi.operational_readiness == OR_GENERATION_FAILED, (
            f"MUAPI 403 smoke test should set OR_GENERATION_FAILED. Got: {muapi.operational_readiness}"
        )
        # credential_status is independent (from verifier probe, not from generation)
        # They should not be the same string (credential_status is about probe, OR is about generation)
        # (unless by coincidence both are "failed" — but their *meaning* is different)
        assert muapi.credential_status != muapi.operational_readiness, (
            "credential_status and operational_readiness should represent different checks. "
            f"Both are '{muapi.credential_status}' — check derivation logic."
        )

    def test_seedance_veo_kling_runway_have_both_access_modes(self, tmp_path):
        """
        Each of Seedance, Veo, Kling, Runway must appear TWICE in the catalog:
          1. As access_mode='muapi_delegated' (currently active MUAPI-aggregated path)
          2. As access_mode='direct' (official direct API slot — implemented in Phase 1J)
        """
        db = tmp_path / "test.db"
        catalog = get_provider_catalog(db_path=db)
        by_provider = {e.provider: e for e in catalog}

        for name in ("seedance", "veo", "kling", "runway"):
            delegated_key  = name
            placeholder_key = f"{name}_official"

            assert delegated_key in by_provider, (
                f"'{delegated_key}' (muapi_delegated entry) missing from catalog"
            )
            assert placeholder_key in by_provider, (
                f"'{placeholder_key}' (direct official entry) missing from catalog. "
                f"Each provider needs its own future credential slot."
            )

            delegated   = by_provider[delegated_key]
            placeholder = by_provider[placeholder_key]

            assert delegated.access_mode == "muapi_delegated", (
                f"{delegated_key}.access_mode should be 'muapi_delegated', got {delegated.access_mode!r}"
            )
            # Phase 1J: official providers upgraded from direct_placeholder → direct
            assert placeholder.access_mode == "direct", (
                f"{placeholder_key}.access_mode should be 'direct', got {placeholder.access_mode!r}"
            )

    def test_direct_placeholders_have_credential_key_name(self, tmp_path):
        """
        Official (direct_placeholder) provider entries must have a credential_key_name
        documenting which env var would authenticate them when implemented.
        """
        db = tmp_path / "test.db"
        catalog = get_provider_catalog(db_path=db)
        by_provider = {e.provider: e for e in catalog}

        for name in ("seedance_official", "veo_official", "kling_official", "runway_official"):
            assert name in by_provider, f"'{name}' missing from catalog"
            entry = by_provider[name]
            assert entry.credential_key_name, (
                f"{name}.credential_key_name must be set (future env var). Got: {entry.credential_key_name!r}"
            )

    def test_muapi_delegated_readiness_inherits_from_muapi(self, tmp_path):
        """
        When MUAPI smoke test shows success, delegated providers (Seedance etc.)
        should show OR_READY_VIA_MUAPI. When MUAPI is not tested, they show OR_MUAPI_DEPENDENT.
        """
        # get_provider_catalog calls _load_smoke_test_results_with_age() (returns tuple).
        # Case 1: MUAPI not tested → delegated = muapi_dependent
        with patch("hypogeum.dashboard_api._load_smoke_test_results_with_age", return_value=({}, None)):
            db = tmp_path / "test.db"
            catalog = get_provider_catalog(db_path=db)
        by_p = {e.provider: e for e in catalog}
        for name in ("seedance", "veo", "kling", "runway"):
            entry = by_p[name]
            assert entry.operational_readiness == OR_MUAPI_DEPENDENT, (
                f"{name} should be OR_MUAPI_DEPENDENT when MUAPI untested. "
                f"Got: {entry.operational_readiness}"
            )

        # Case 2: MUAPI smoke test passed → delegated = ready_via_muapi
        with patch("hypogeum.dashboard_api._load_smoke_test_results_with_age", return_value=(
            {"muapi": {"status": "success", "model": "gpt-image-2-text-to-image", "latency_ms": 5000}},
            1,
        )):
            catalog2 = get_provider_catalog(db_path=db)
        by_p2 = {e.provider: e for e in catalog2}
        for name in ("seedance", "veo", "kling", "runway"):
            entry2 = by_p2[name]
            assert entry2.operational_readiness == OR_READY_VIA_MUAPI, (
                f"{name} should be OR_READY_VIA_MUAPI when MUAPI passed. "
                f"Got: {entry2.operational_readiness}"
            )

    def test_smoke_test_success_sets_operational_readiness_ready(self):
        """
        _derive_operational_readiness() with a success smoke result returns OR_READY.
        """
        meta  = {"access_mode": "direct"}
        smoke = {"status": "success", "model": "gpt-image-1", "latency_ms": 3000}
        readiness, note = _derive_operational_readiness("openai", meta, smoke, OR_NOT_TESTED)
        assert readiness == OR_READY, f"Expected OR_READY, got {readiness!r}"
        assert note is not None and "gpt-image-1" in note

    def test_smoke_test_403_sets_generation_failed_readiness(self):
        """
        _derive_operational_readiness() with a 403 error in smoke result returns OR_GENERATION_FAILED.

        403 means model/account-tier access is forbidden — not a credential rejection.
        Only 401 maps to OR_AUTH_FAILED. 403 maps to OR_GENERATION_FAILED.
        """
        meta  = {"access_mode": "direct"}
        smoke = {
            "status": "error_submit",
            "model": "gpt-image-2-text-to-image",
            "error": "403 Client Error: Forbidden for url: https://api.muapi.ai/api/v1/...",
        }
        readiness, note = _derive_operational_readiness("muapi", meta, smoke, OR_NOT_TESTED)
        assert readiness == OR_GENERATION_FAILED, f"Expected OR_GENERATION_FAILED, got {readiness!r}"
        assert note is not None


# ---------------------------------------------------------------------------
# TestThumbnailConfigDefaults — point 2: new field has correct code-level default
# ---------------------------------------------------------------------------

class TestThumbnailConfigDefaults:
    """
    Verify that THUMBNAIL_FALLBACK2_PROVIDER is a recognized config field and
    that all three chain fields default correctly in code (independent of .env).
    """

    def _config_no_dotenv(self, extra_env: dict | None = None) -> Config:
        """Return a Config instance with dotenv loading suppressed."""
        env = {"OPENROUTER_API_KEY": "test"}
        if extra_env:
            env.update(extra_env)
        with patch("hypogeum.config._load_dotenv"), \
             patch.dict(os.environ, env, clear=True):
            return Config()

    def test_thumbnail_image_provider_defaults_to_openrouter(self):
        """Code default for THUMBNAIL_IMAGE_PROVIDER must be 'openrouter'."""
        cfg = self._config_no_dotenv()
        assert cfg.THUMBNAIL_IMAGE_PROVIDER == "openrouter", (
            f"Expected default 'openrouter', got {cfg.THUMBNAIL_IMAGE_PROVIDER!r}. "
            "This is the primary thumbnail provider after the 2026-06-21 comparison."
        )

    def test_thumbnail_fallback_provider_defaults_to_openai(self):
        """Code default for THUMBNAIL_FALLBACK_PROVIDER must be 'openai'."""
        cfg = self._config_no_dotenv()
        assert cfg.THUMBNAIL_FALLBACK_PROVIDER == "openai", (
            f"Expected default 'openai', got {cfg.THUMBNAIL_FALLBACK_PROVIDER!r}."
        )

    def test_thumbnail_fallback2_provider_defaults_to_muapi(self):
        """Code default for THUMBNAIL_FALLBACK2_PROVIDER must be 'muapi'."""
        cfg = self._config_no_dotenv()
        assert cfg.THUMBNAIL_FALLBACK2_PROVIDER == "muapi", (
            f"Expected default 'muapi', got {cfg.THUMBNAIL_FALLBACK2_PROVIDER!r}."
        )

    def test_thumbnail_fallback2_provider_is_overridable_via_env(self):
        """THUMBNAIL_FALLBACK2_PROVIDER must be overridable via environment variable."""
        cfg = self._config_no_dotenv({"THUMBNAIL_FALLBACK2_PROVIDER": "openrouter"})
        assert cfg.THUMBNAIL_FALLBACK2_PROVIDER == "openrouter"

    def test_chain_order_is_openrouter_openai_muapi_by_default(self):
        """The three default fields assemble to the expected priority chain."""
        cfg = self._config_no_dotenv()
        chain = [
            cfg.THUMBNAIL_IMAGE_PROVIDER,
            cfg.THUMBNAIL_FALLBACK_PROVIDER,
            cfg.THUMBNAIL_FALLBACK2_PROVIDER,
        ]
        assert chain == ["openrouter", "openai", "muapi"], (
            f"Expected ['openrouter', 'openai', 'muapi'], got {chain}."
        )


# ---------------------------------------------------------------------------
# TestThumbnailProviderUnknown — point 4: unknown provider raises ValueError
# ---------------------------------------------------------------------------

class TestThumbnailProviderUnknown:
    """
    Verify that passing an unrecognised provider name to _generate_thumbnail_one_provider
    raises ValueError immediately — not silently routed through MUAPI.
    """

    def test_unknown_provider_raises_value_error(self, tmp_path):
        """An unknown provider string must raise ValueError, not fall through to MUAPI."""
        from hypogeum.muapi import _generate_thumbnail_one_provider

        cfg = _make_config(
            **{
                "THUMBNAIL_IMAGE_PROVIDER": "muapi",
                "THUMBNAIL_FALLBACK_MODEL": "flux-2-dev",
            }
        )

        with pytest.raises(ValueError, match="Unknown thumbnail provider"):
            _generate_thumbnail_one_provider(
                provider="bogus_provider",
                deliverable_id="main",
                image_prompt="test",
                aspect_ratio="16:9",
                config=cfg,
                output_dir=tmp_path,
                episode_id="ep_001",
                budget_guard=None,
                text_overlay=None,
                typography_plan=None,
                typography_integration="none",
                log=MagicMock(),
                is_fallback=False,
            )

    def test_unknown_provider_error_names_the_bad_value(self, tmp_path):
        """The ValueError message must include the offending provider name."""
        from hypogeum.muapi import _generate_thumbnail_one_provider

        cfg = _make_config(**{"THUMBNAIL_IMAGE_PROVIDER": "muapi"})

        with pytest.raises(ValueError, match="typo_provider"):
            _generate_thumbnail_one_provider(
                provider="typo_provider",
                deliverable_id="main",
                image_prompt="test",
                aspect_ratio="16:9",
                config=cfg,
                output_dir=tmp_path,
                episode_id="ep_001",
                budget_guard=None,
                text_overlay=None,
                typography_plan=None,
                typography_integration="none",
                log=MagicMock(),
                is_fallback=False,
            )

    def test_known_providers_do_not_raise_value_error(self, tmp_path):
        """The three valid names must NOT raise ValueError (they raise MUAPIError on real failure)."""
        from hypogeum.muapi import _generate_thumbnail_one_provider, MUAPIError

        cfg = _make_config(**{"THUMBNAIL_IMAGE_PROVIDER": "muapi"})

        for provider in ("openrouter", "openai", "muapi"):
            with patch("hypogeum.muapi._generate_thumbnail_openrouter",
                       side_effect=MUAPIError("no key")), \
                 patch("hypogeum.muapi._generate_thumbnail_openai",
                       side_effect=MUAPIError("no key")), \
                 patch("hypogeum.muapi._attempt_thumbnail_generation",
                       side_effect=MUAPIError("no key")):
                with pytest.raises(MUAPIError):  # fails but NOT as ValueError
                    _generate_thumbnail_one_provider(
                        provider=provider,
                        deliverable_id="main",
                        image_prompt="test",
                        aspect_ratio="16:9",
                        config=cfg,
                        output_dir=tmp_path,
                        episode_id="ep_001",
                        budget_guard=None,
                        text_overlay=None,
                        typography_plan=None,
                        typography_integration="none",
                        log=MagicMock(),
                        is_fallback=False,
                    )
