"""
tests/test_provider_registry.py — Phase 1A provider registry tests

Tests:
  TestProviderCatalog
    test_openai_in_catalog_without_asset_versions
        OpenAI appears in get_provider_catalog() even with an empty DB and no env key.

    test_openrouter_has_image_capability
        OpenRouter ProviderCatalogEntry lists "image" in capabilities.

    test_muapi_delegated_providers_labelled
        Seedance, Veo, Kling, Runway are labelled access_mode="muapi_delegated".

  TestThumbnailProviderDispatch
    test_openai_dispatch_calls_openai_function
        generate_thumbnail() with THUMBNAIL_IMAGE_PROVIDER=openai calls
        _generate_thumbnail_openai(), not _attempt_thumbnail_generation().

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
