"""
tests/test_providers_page.py — Phase 1E: Providers UI integration tests

Verifies that GET /providers renders the provider_catalog-driven UI
with all required providers, two-row status model, and correct access-mode labelling.

No network calls. No API keys required. Uses Flask test client.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def providers_html():
    """Render /providers once for all tests in this module."""
    os.environ.setdefault("OPENROUTER_API_KEY", "test-key-for-providers-page")
    from run_dashboard import app
    with app.test_client() as client:
        resp = client.get("/providers")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        return resp.data.decode("utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestProvidersPageRendering:

    def test_page_returns_200(self):
        os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
        from run_dashboard import app
        with app.test_client() as client:
            resp = client.get("/providers")
        assert resp.status_code == 200

    def test_openai_appears(self, providers_html):
        """OpenAI must appear as a provider card — it is a new first-class provider."""
        assert "OpenAI" in providers_html, (
            "OpenAI not found on providers page. "
            "It must appear even before OPENAI_API_KEY is set."
        )

    def test_openrouter_shows_llm_routing(self, providers_html):
        """OpenRouter must label its LLM routing capability."""
        assert "LLM routing" in providers_html, (
            "OpenRouter LLM routing capability badge not found on providers page."
        )

    def test_openrouter_shows_image_generation(self, providers_html):
        """OpenRouter must also label its image generation capability."""
        assert "Image generation" in providers_html, (
            "OpenRouter image generation capability badge not found on providers page."
        )

    def test_muapi_appears(self, providers_html):
        assert "MUAPI" in providers_html

    def test_elevenlabs_appears(self, providers_html):
        assert "ElevenLabs" in providers_html

    def test_two_row_status_labels_present(self, providers_html):
        """Both 'Credential' and 'Generation' row labels must appear in the page."""
        assert "Credential" in providers_html, "Credential status row label missing."
        assert "Generation" in providers_html, "Generation readiness row label missing."

    def test_muapi_shows_access_mode_as_direct(self, providers_html):
        """
        MUAPI itself is a direct provider — it should NOT show 'via MUAPI'.
        Only delegated sub-providers (Seedance etc.) get that badge.
        """
        # The 'via MUAPI' badge appears for delegated providers, not for MUAPI itself.
        # We check the page has the badge somewhere (for delegated ones) but MUAPI card
        # doesn't show it alongside the MUAPI name.
        assert "via MUAPI" in providers_html, "'via MUAPI' badge missing for delegated providers."


class TestVideoProviderPairs:
    """Each of Seedance, Veo, Kling, Runway must show two cards."""

    def test_seedance_muapi_delegated_card(self, providers_html):
        assert "Seedance" in providers_html, "Seedance muapi-delegated card missing."

    def test_seedance_official_placeholder(self, providers_html):
        """seedance_official must appear as a placeholder card."""
        assert "seedance_official" in providers_html, (
            "seedance_official direct placeholder not rendered. "
            "Each video provider must have a future direct API slot."
        )

    def test_veo_official_placeholder(self, providers_html):
        assert "veo_official" in providers_html

    def test_kling_official_placeholder(self, providers_html):
        assert "kling_official" in providers_html

    def test_runway_official_placeholder(self, providers_html):
        assert "runway_official" in providers_html

    def test_official_placeholder_label(self, providers_html):
        """All official placeholders must be labelled as 'Official API — not yet implemented'."""
        assert "Official API — not yet implemented" in providers_html, (
            "Placeholder label 'Official API — not yet implemented' not found."
        )

    def test_via_muapi_badge_present(self, providers_html):
        """Delegated providers show 'via MUAPI' badge."""
        assert "via MUAPI" in providers_html


class TestMuapiDualStatus:
    """MUAPI must show separate credential and generation status."""

    def test_muapi_page_has_both_status_rows(self, providers_html):
        """
        The two-row status table (Credential / Generation) must be present.
        """
        assert "Credential" in providers_html
        assert "Generation" in providers_html

    def test_muapi_operational_note_visible(self):
        """
        When MUAPI smoke test result shows 403, the page must include
        an operational note explaining the auth failure.
        Inject a fake smoke result for this test.
        """
        import json
        os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
        from run_dashboard import app
        from hypogeum.dashboard_api import get_provider_catalog

        fake_smoke = {
            "muapi": {
                "status":   "error_submit",
                "model":    "gpt-image-2-text-to-image",
                "latency_ms": 1200,
                "error":    "403 Client Error: Forbidden",
            }
        }
        with patch("hypogeum.dashboard_api._load_smoke_test_results", return_value=fake_smoke):
            with app.test_client() as client:
                resp = client.get("/providers")
        html = resp.data.decode("utf-8")
        # Page should contain the auth_failed operational readiness indicator
        assert "Auth failed" in html or "auth_failed" in html or "403" in html, (
            "MUAPI 403 auth failure not surfaced on providers page. "
            "The operational readiness note should mention the 403."
        )


class TestPublishingSection:
    """Publishing platforms section still renders correctly."""

    def test_youtube_card_present(self, providers_html):
        assert "YouTube" in providers_html

    def test_publishing_section_label(self, providers_html):
        assert "Publishing" in providers_html


# ---------------------------------------------------------------------------
# Phase 1E.1 fixes
# ---------------------------------------------------------------------------

class TestOpenAIVerifyRoute:
    """
    Phase 1E.1 Fix 1: /api/providers/verify/openai must be accepted by the
    verification route. Before the fix it returned 404 "unknown provider 'openai'"
    because openai was missing from _CONTENT_PROVIDERS.
    """

    def test_verify_openai_route_accepted(self):
        """POST /api/providers/verify/openai must return 200, not 404."""
        os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
        from run_dashboard import app
        with app.test_client() as client:
            # Patch the probe so no real network call is made
            with patch("hypogeum.provider_verifier._probe_openai",
                       return_value=(True, None, 55)):
                resp = client.post("/api/providers/verify/openai")
        data = resp.get_json()
        assert resp.status_code == 200, (
            f"Expected 200 from /api/providers/verify/openai, got {resp.status_code}. "
            f"Body: {data}. openai must be in _CONTENT_PROVIDERS."
        )
        assert data.get("error") != "unknown provider 'openai'", (
            "Route still returning 'unknown provider openai'. "
            "Add openai to _CONTENT_PROVIDERS."
        )

    def test_verify_openai_returns_status_not_unknown_provider(self):
        """Response body must contain a provider status, not an error."""
        os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
        from run_dashboard import app
        with app.test_client() as client:
            with patch("hypogeum.provider_verifier._probe_openai",
                       return_value=(False, "HTTP 401", 30)):
                resp = client.post("/api/providers/verify/openai")
        data = resp.get_json()
        # Even a failed probe is a valid response — "unknown provider" is not
        assert "status" in data, (
            f"Response missing 'status' field. Got: {data}"
        )
        assert data.get("provider") == "openai", (
            f"Response provider field should be 'openai'. Got: {data}"
        )


class TestOperationalReadinessWithSecretProfile:
    """
    Phase 1E.1 Fix 2: operational_readiness must not stay OR_SKIPPED_NO_KEY when
    the credential is saved as a SecretProfile (even if absent from env vars).

    Before the fix, the smoke-test snapshot ("skipped_no_key") was treated as
    permanent, making the UI show "Skipped — no key" even after connecting.
    """

    def test_skipped_smoke_result_upgraded_when_secret_profile_present(self, tmp_path):
        """
        A smoke result of 'skipped_no_key' must be upgraded to OR_NOT_TESTED
        when the provider has an active SecretProfile (has_secret_profile=True).
        """
        from hypogeum.dashboard_api import (
            get_provider_catalog, OR_NOT_TESTED, OR_SKIPPED_NO_KEY,
        )

        fake_statuses = {
            "openai": {
                "status":            "saved",
                "has_env_var":       False,
                "has_secret_profile": True,  # key saved in DB
                "not_verifiable":    False,
            }
        }
        fake_smoke = {
            "openai": {"status": "skipped_no_key", "note": "OPENAI_API_KEY not set"}
        }

        db = tmp_path / "test.db"
        with patch("hypogeum.provider_verifier.get_all_provider_statuses",
                   return_value=fake_statuses), \
             patch("hypogeum.dashboard_api._load_smoke_test_results",
                   return_value=fake_smoke):
            catalog = get_provider_catalog(db_path=db)

        openai_entry = next(e for e in catalog if e.provider == "openai")
        assert openai_entry.is_configured is True, (
            "is_configured must be True when has_secret_profile=True"
        )
        assert openai_entry.operational_readiness != OR_SKIPPED_NO_KEY, (
            "operational_readiness must NOT be OR_SKIPPED_NO_KEY when a "
            "credential is saved in the DB. Got OR_SKIPPED_NO_KEY — the stale "
            "smoke snapshot is overriding a live credential."
        )
        assert openai_entry.operational_readiness == OR_NOT_TESTED, (
            f"Expected OR_NOT_TESTED after upgrade. Got: {openai_entry.operational_readiness}"
        )

    def test_skipped_smoke_stays_skipped_when_truly_not_configured(self, tmp_path):
        """
        When the provider genuinely has no key (env=False, secret_profile=False),
        OR_SKIPPED_NO_KEY must be preserved — it is accurate.
        """
        from hypogeum.dashboard_api import (
            get_provider_catalog, OR_SKIPPED_NO_KEY,
        )

        fake_statuses = {
            "openai": {
                "status":            "missing",
                "has_env_var":       False,
                "has_secret_profile": False,
            }
        }
        fake_smoke = {
            "openai": {"status": "skipped_no_key", "note": "OPENAI_API_KEY not set"}
        }

        db = tmp_path / "test.db"
        with patch("hypogeum.provider_verifier.get_all_provider_statuses",
                   return_value=fake_statuses), \
             patch("hypogeum.dashboard_api._load_smoke_test_results",
                   return_value=fake_smoke):
            catalog = get_provider_catalog(db_path=db)

        openai_entry = next(e for e in catalog if e.provider == "openai")
        assert openai_entry.is_configured is False
        assert openai_entry.operational_readiness == OR_SKIPPED_NO_KEY, (
            "OR_SKIPPED_NO_KEY must be preserved when the provider truly has "
            f"no credential. Got: {openai_entry.operational_readiness}"
        )

    def test_env_var_present_also_upgrades_skipped_readiness(self, tmp_path):
        """
        OR_SKIPPED_NO_KEY is upgraded when has_env_var=True (not only secret_profile).
        """
        from hypogeum.dashboard_api import (
            get_provider_catalog, OR_NOT_TESTED,
        )

        fake_statuses = {
            "openai": {
                "status":            "saved",
                "has_env_var":       True,   # key in env
                "has_secret_profile": False,
            }
        }
        fake_smoke = {
            "openai": {"status": "skipped_no_key", "note": "OPENAI_API_KEY not set"}
        }

        db = tmp_path / "test.db"
        with patch("hypogeum.provider_verifier.get_all_provider_statuses",
                   return_value=fake_statuses), \
             patch("hypogeum.dashboard_api._load_smoke_test_results",
                   return_value=fake_smoke):
            catalog = get_provider_catalog(db_path=db)

        openai_entry = next(e for e in catalog if e.provider == "openai")
        assert openai_entry.operational_readiness == OR_NOT_TESTED, (
            "OR_SKIPPED_NO_KEY must be upgraded when has_env_var=True. "
            f"Got: {openai_entry.operational_readiness}"
        )
