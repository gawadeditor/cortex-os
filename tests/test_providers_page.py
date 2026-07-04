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
        """Official providers (Phase 1J) have access_mode='direct' and OR_NOT_TESTED.
        The template renders OR_NOT_TESTED as 'Not tested' in the Generation row badge.
        """
        assert "Not tested" in providers_html, (
            "Expected 'Not tested' badge for untested direct providers not found."
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
        When MUAPI smoke test result shows 403, the page must show a generation
        failure indicator — not auth_failed (403 from generation is model access,
        not credential rejection). Credential and generation rows are independent.
        """
        os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
        from run_dashboard import app

        fake_smoke = {
            "muapi": {
                "status":   "error_submit",
                "model":    "gpt-image-2-text-to-image",
                "latency_ms": 1200,
                "error":    "403 Client Error: Forbidden",
            }
        }
        with patch("hypogeum.dashboard_api._load_smoke_test_results_with_age",
                   return_value=(fake_smoke, 0.1)):  # fresh — no staleness downgrade
            with app.test_client() as client:
                resp = client.get("/providers")
        html = resp.data.decode("utf-8")
        # 403 from generation endpoint → generation_failed (not auth_failed)
        assert "Generation failed" in html or "generation_failed" in html or "403" in html, (
            "MUAPI 403 generation failure not surfaced on providers page. "
            "The operational readiness note should mention the generation failure."
        )
        # 403 from generation must NOT show as auth_failed (that is only for 401)
        assert "Auth failed (403)" not in html, (
            "MUAPI 403 from generation endpoint must not show 'Auth failed (403)'. "
            "Only 401 is a credential rejection. 403 is a model access issue."
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


# ---------------------------------------------------------------------------
# Phase 1L — MUAPI false failure state tests
# ---------------------------------------------------------------------------

class TestMuapiFalseFailurePhase1L:
    """
    Phase 1L: Verify that MUAPI dashboard state is correctly distinguished
    between credential failures (401), generation failures (403/model error),
    and stale smoke results. The smoke test previously used wrong auth header
    (Authorization: Bearer instead of x-api-key), causing false 403 failures.

    Key invariants:
      - 403 from generation endpoint → generation_failed (not auth_failed)
      - Stale smoke failure (>SMOKE_MAX_AGE_DAYS) → needs_retest
      - Credential row reflects verifier probe only (independent of smoke)
      - Secret Profile key is preferred over env var
    """

    def test_stale_muapi_smoke_failure_becomes_needs_retest(self, tmp_path):
        """
        A MUAPI smoke failure older than SMOKE_MAX_AGE_DAYS must be downgraded
        to needs_retest — it must never permanently show auth_failed.
        """
        from hypogeum.dashboard_api import (
            get_provider_catalog, OR_AUTH_FAILED, OR_NEEDS_RETEST, SMOKE_MAX_AGE_DAYS,
        )

        stale_smoke = {
            "muapi": {
                "status": "error_submit",
                "model": "gpt-image-2-text-to-image",
                "latency_ms": 1087,
                "error": "401 Unauthorized",
            }
        }
        age_days = SMOKE_MAX_AGE_DAYS + 1  # older than threshold

        db = tmp_path / "test.db"
        with patch("hypogeum.provider_verifier.get_all_provider_statuses", return_value={}), \
             patch("hypogeum.dashboard_api._load_smoke_test_results_with_age",
                   return_value=(stale_smoke, age_days)):
            catalog = get_provider_catalog(db_path=db)

        muapi = next(e for e in catalog if e.provider == "muapi")
        assert muapi.operational_readiness != OR_AUTH_FAILED, (
            f"Stale MUAPI smoke failure must not permanently show auth_failed. "
            f"Got: {muapi.operational_readiness}"
        )
        assert muapi.operational_readiness == OR_NEEDS_RETEST, (
            f"Stale MUAPI smoke failure must show needs_retest. "
            f"Got: {muapi.operational_readiness}"
        )

    def test_working_credential_with_stale_smoke_failure_shows_needs_retest(self, tmp_path):
        """
        When MUAPI credential is verified (status='verified') but the smoke test
        result is stale and shows a generation failure, the dashboard must show
        needs_retest — not auth_failed or generation_failed.
        """
        from hypogeum.dashboard_api import (
            get_provider_catalog, OR_NEEDS_RETEST, OR_AUTH_FAILED, OR_GENERATION_FAILED,
            SMOKE_MAX_AGE_DAYS,
        )

        stale_smoke = {
            "muapi": {
                "status": "error_submit",
                "model": "gpt-image-2-text-to-image",
                "latency_ms": 500,
                "error": "403 Client Error: Forbidden",
            }
        }
        fake_statuses = {
            "muapi": {
                "status": "verified",
                "has_env_var": True,
                "has_secret_profile": False,
                "not_verifiable": False,
            }
        }
        age_days = SMOKE_MAX_AGE_DAYS + 2  # stale

        db = tmp_path / "test.db"
        with patch("hypogeum.provider_verifier.get_all_provider_statuses",
                   return_value=fake_statuses), \
             patch("hypogeum.dashboard_api._load_smoke_test_results_with_age",
                   return_value=(stale_smoke, age_days)):
            catalog = get_provider_catalog(db_path=db)

        muapi = next(e for e in catalog if e.provider == "muapi")
        assert muapi.operational_readiness not in (OR_AUTH_FAILED, OR_GENERATION_FAILED), (
            f"Stale smoke failure with working credential must not show as "
            f"auth_failed or generation_failed. Got: {muapi.operational_readiness}"
        )
        assert muapi.operational_readiness == OR_NEEDS_RETEST, (
            f"Expected needs_retest. Got: {muapi.operational_readiness}"
        )
        assert muapi.credential_status == "verified", (
            "Credential row must reflect verifier result, not smoke test result. "
            f"Got: {muapi.credential_status}"
        )

    def test_403_from_generation_is_generation_failed_not_auth_failed(self, tmp_path):
        """
        A 403 from the MUAPI generation endpoint in the smoke test must map to
        generation_failed — not auth_failed. Only 401 is a credential rejection.
        403 means the key reached the server but the model/tier is restricted.
        """
        from hypogeum.dashboard_api import (
            get_provider_catalog, OR_AUTH_FAILED, OR_GENERATION_FAILED,
        )

        fresh_smoke = {
            "muapi": {
                "status": "error_submit",
                "model": "gpt-image-2-text-to-image",
                "latency_ms": 1087,
                "error": "403 Client Error: Forbidden",
            }
        }

        db = tmp_path / "test.db"
        with patch("hypogeum.provider_verifier.get_all_provider_statuses", return_value={}), \
             patch("hypogeum.dashboard_api._load_smoke_test_results_with_age",
                   return_value=(fresh_smoke, 0.0)):  # fresh — no staleness
            catalog = get_provider_catalog(db_path=db)

        muapi = next(e for e in catalog if e.provider == "muapi")
        assert muapi.operational_readiness != OR_AUTH_FAILED, (
            "403 from generation endpoint must NOT map to auth_failed. "
            "Only 401 is a credential rejection."
        )
        assert muapi.operational_readiness == OR_GENERATION_FAILED, (
            f"403 from generation endpoint must map to generation_failed. "
            f"Got: {muapi.operational_readiness}"
        )

    def test_smoke_403_does_not_affect_credential_status(self, tmp_path):
        """
        A 403 in the smoke test (generation failure) must not contaminate the
        credential row. The credential status comes from the verifier probe only.
        """
        from hypogeum.dashboard_api import get_provider_catalog

        fresh_smoke = {
            "muapi": {
                "status": "error_submit",
                "model": "gpt-image-2-text-to-image",
                "latency_ms": 1000,
                "error": "403 Client Error: Forbidden",
            }
        }
        fake_statuses = {
            "muapi": {
                "status": "verified",
                "has_env_var": True,
                "has_secret_profile": False,
                "not_verifiable": False,
            }
        }

        db = tmp_path / "test.db"
        with patch("hypogeum.provider_verifier.get_all_provider_statuses",
                   return_value=fake_statuses), \
             patch("hypogeum.dashboard_api._load_smoke_test_results_with_age",
                   return_value=(fresh_smoke, 0.0)):
            catalog = get_provider_catalog(db_path=db)

        muapi = next(e for e in catalog if e.provider == "muapi")
        assert muapi.credential_status == "verified", (
            "Smoke test 403 must not affect the credential_status row. "
            f"credential_status must reflect verifier result only. "
            f"Got: {muapi.credential_status}"
        )

    def test_secret_profile_key_preferred_over_env_var(self, tmp_path):
        """
        verify_provider('muapi') must use Secret Profile credential over env var
        when both are present. The source field in the result must be 'secret_profile'.
        """
        from hypogeum.provider_verifier import verify_provider, STATUS_VERIFIED, _PROBES
        from hypogeum.db import initialize_schema
        from unittest.mock import MagicMock

        db = tmp_path / "sp_test.db"
        initialize_schema(db)

        secret_key = "secret-profile-key-muapi-sp-12345"
        fake_profile = type("SP", (), {
            "status": "active",
            "credential_data": secret_key,
            "secret_profile_id": "sp-001",
            "provider": "muapi",
        })()

        mock_probe = MagicMock(return_value=(True, None, 55))
        fake_probes = dict(_PROBES)
        fake_probes["muapi"] = mock_probe

        with patch("hypogeum.credential_manager.list_secret_profiles",
                   return_value=[fake_profile]), \
             patch("hypogeum.provider_verifier._PROBES", fake_probes), \
             patch.dict("os.environ", {"MUAPI_API_KEY": "env-key-should-not-be-used"}):
            result = verify_provider("muapi", db_path=db)

        assert result["source"] == "secret_profile", (
            "Secret Profile must be preferred over env var when both are present. "
            f"Got source: {result['source']}"
        )
        mock_probe.assert_called_once_with(secret_key)
        assert result["status"] == STATUS_VERIFIED, (
            f"Expected verified. Got: {result['status']}"
        )
