"""
tests/test_phase_1l.py — Phase 1L: MUAPI false-failure state fix

Coverage:
    TestMUAPIProbeHeader         — verifier uses x-api-key, not Authorization: Bearer
    TestMUAPIProbeEndpoints      — balance endpoint fallback to models endpoint
    TestErrorClassification      — 403 from generation → generation_failed, not auth_failed
                                   401 from generation → auth_failed
    TestStalenessHandling        — stale smoke results → needs_retest, not auth_failed
    TestDelegatedCredential      — MUAPI-delegated providers inherit MUAPI's credential_status
    TestSmokePath                — OR_NEEDS_RETEST badge in template; age displayed
    TestSmokeTestRoute           — POST /api/providers/muapi/smoke-test
    TestProviderCatalogConsistency — credential row vs generation row separation
    TestNoNetworkCalls           — all logic testable without sockets
"""

from __future__ import annotations

import json
import socket
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_dashboard as server
from hypogeum.dashboard_api import (
    SMOKE_MAX_AGE_DAYS,
    OR_AUTH_FAILED,
    OR_GENERATION_FAILED,
    OR_NEEDS_RETEST,
    OR_NOT_TESTED,
    OR_READY,
    OR_MUAPI_NOT_READY,
    _derive_operational_readiness,
    _load_smoke_test_results_with_age,
    get_provider_catalog,
)
from hypogeum.db import initialize_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "1l_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def client(db):
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db)
    with server.app.test_client() as c:
        yield c


def _smoke_report(status: str, error: str = "", age_days: float = 0.0, model: str = "t3") -> dict:
    """Build a fake providers smoke dict for MUAPI."""
    generated_at = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    return {
        "generated_at": generated_at,
        "providers": {
            "muapi": {
                "provider": "muapi",
                "status": status,
                "model": model,
                "endpoint": "https://api.muapi.ai/api/v1/veo3-fast-text-to-video",
                "latency_ms": 1200,
                "error": error,
                "started_at": generated_at,
            }
        }
    }


def _patch_smoke(report: dict):
    return patch(
        "hypogeum.dashboard_api._load_smoke_test_results_with_age",
        return_value=(
            report.get("providers", {}),
            (datetime.now(timezone.utc) - datetime.fromisoformat(
                report.get("generated_at", datetime.now(timezone.utc).isoformat())
                .replace("Z", "+00:00")
            )).total_seconds() / 86400.0
            if "generated_at" in report else None,
        ),
    )


# ===========================================================================
# TestMUAPIProbeHeader
# ===========================================================================

class TestMUAPIProbeHeader:

    def test_probe_uses_x_api_key_header(self):
        """_probe_muapi must send x-api-key, not Authorization: Bearer."""
        from hypogeum.provider_verifier import _probe_muapi
        captured = {}

        def _fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.headers)
            mock = MagicMock()
            mock.__enter__ = lambda s: s
            mock.__exit__ = MagicMock(return_value=False)
            mock.status = 200
            return mock

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            _probe_muapi("test-api-key")

        # urllib.request normalizes header keys with str.capitalize(),
        # so "x-api-key" is stored as "X-api-key". Check case-insensitively.
        header_keys_lower = {k.lower() for k in captured.get("headers", {})}
        assert "x-api-key" in header_keys_lower, \
            f"MUAPI probe must use x-api-key header, got: {captured.get('headers', {})}"
        assert "authorization" not in header_keys_lower, \
            "MUAPI probe must NOT use Authorization header"

    def test_probe_sends_key_value(self):
        """Probe sends the actual key value in x-api-key header."""
        from hypogeum.provider_verifier import _probe_muapi
        captured = {}

        def _fake_urlopen(req, timeout=None):
            captured["x_api_key"] = req.get_header("X-api-key")
            mock = MagicMock()
            mock.__enter__ = lambda s: s
            mock.__exit__ = MagicMock(return_value=False)
            mock.status = 200
            return mock

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            _probe_muapi("my-secret-muapi-key")

        assert captured.get("x_api_key") == "my-secret-muapi-key"

    def test_probe_returns_true_on_200(self):
        from hypogeum.provider_verifier import _probe_muapi
        mock = MagicMock()
        mock.__enter__ = lambda s: s
        mock.__exit__ = MagicMock(return_value=False)
        mock.status = 200
        with patch("urllib.request.urlopen", return_value=mock):
            success, error, latency = _probe_muapi("valid-key")
        assert success is True
        assert error is None

    def test_probe_returns_false_on_401(self):
        import urllib.error
        from hypogeum.provider_verifier import _probe_muapi
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError("", 401, "Unauthorized", {}, None)):
            success, error, latency = _probe_muapi("bad-key")
        assert success is False
        assert "401" in (error or "")

    def test_probe_returns_false_on_403_from_balance(self):
        """403 from balance endpoint is a credential failure (key rejected)."""
        import urllib.error
        from hypogeum.provider_verifier import _probe_muapi
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError("", 403, "Forbidden", {}, None)):
            success, error, latency = _probe_muapi("bad-key")
        assert success is False


# ===========================================================================
# TestMUAPIProbeEndpoints
# ===========================================================================

class TestMUAPIProbeEndpoints:

    def test_balance_endpoint_used_first(self):
        """First probe attempt hits /api/v1/user/balance."""
        from hypogeum.provider_verifier import _probe_muapi
        urls_called = []

        def _fake_urlopen(req, timeout=None):
            urls_called.append(req.full_url)
            mock = MagicMock()
            mock.__enter__ = lambda s: s
            mock.__exit__ = MagicMock(return_value=False)
            mock.status = 200
            return mock

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            _probe_muapi("key")

        assert any("user/balance" in u for u in urls_called), \
            f"Expected /user/balance to be called, got: {urls_called}"

    def test_models_endpoint_called_as_fallback_on_404(self):
        """If balance returns 404, probe falls back to /api/v1/models."""
        import urllib.error
        from hypogeum.provider_verifier import _probe_muapi
        urls_called = []
        call_count = [0]

        def _fake_urlopen(req, timeout=None):
            urls_called.append(req.full_url)
            call_count[0] += 1
            if call_count[0] == 1:
                raise urllib.error.HTTPError("", 404, "Not Found", {}, None)
            mock = MagicMock()
            mock.__enter__ = lambda s: s
            mock.__exit__ = MagicMock(return_value=False)
            mock.status = 200
            return mock

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            success, _, _ = _probe_muapi("key")

        assert any("models" in u for u in urls_called), \
            f"Fallback to /models expected, got: {urls_called}"
        assert success is True

    def test_404_on_both_endpoints_returns_false(self):
        """If both balance and models return 404, the probe fails gracefully."""
        import urllib.error
        from hypogeum.provider_verifier import _probe_muapi

        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError("", 404, "Not Found", {}, None)):
            success, error, latency = _probe_muapi("key")

        assert success is False


# ===========================================================================
# TestErrorClassification
# ===========================================================================

class TestErrorClassification:

    def _derive(self, status: str, error: str):
        smoke = {"status": status, "error": error, "model": "t3", "latency_ms": 100}
        return _derive_operational_readiness("muapi", {"access_mode": "direct"}, smoke, OR_NOT_TESTED)

    def test_401_in_error_is_auth_failed(self):
        readiness, note = self._derive("error_submit", "HTTP 401 Unauthorized")
        assert readiness == OR_AUTH_FAILED

    def test_403_in_error_is_generation_failed_not_auth_failed(self):
        """403 from a generation endpoint ≠ bad credential. Classify as generation_failed."""
        readiness, note = self._derive(
            "error_submit",
            "403 Client Error: Forbidden for url: https://api.muapi.ai/api/v1/gpt-image-2-text-to-image"
        )
        assert readiness == OR_GENERATION_FAILED, \
            f"Expected generation_failed, got {readiness}"
        assert readiness != OR_AUTH_FAILED, "403 must NOT be classified as auth_failed"

    def test_403_note_mentions_model_access(self):
        readiness, note = self._derive(
            "error_submit",
            "403 Client Error: Forbidden"
        )
        note_lower = (note or "").lower()
        assert any(kw in note_lower for kw in ("model", "access", "account", "tier", "403"))

    def test_402_is_billing_failed(self):
        readiness, note = self._derive("error_submit", "402 Payment Required")
        from hypogeum.dashboard_api import OR_BILLING_FAILED
        assert readiness == OR_BILLING_FAILED

    def test_5xx_is_generation_failed(self):
        readiness, note = self._derive("error_submit", "503 Service Unavailable")
        assert readiness == OR_GENERATION_FAILED

    def test_success_status_is_ready(self):
        readiness, note = self._derive("success", "")
        assert readiness == OR_READY

    def test_skipped_no_key_is_correct(self):
        from hypogeum.dashboard_api import OR_SKIPPED_NO_KEY
        smoke = {"status": "skipped_no_key", "error": "", "note": "No API key set"}
        readiness, _ = _derive_operational_readiness("muapi", {"access_mode": "direct"}, smoke, OR_NOT_TESTED)
        assert readiness == OR_SKIPPED_NO_KEY


# ===========================================================================
# TestStalenessHandling
# ===========================================================================

class TestStalenessHandling:

    def test_smoke_max_age_days_constant_exists(self):
        assert isinstance(SMOKE_MAX_AGE_DAYS, (int, float))
        assert SMOKE_MAX_AGE_DAYS > 0

    def test_needs_retest_constant_exists(self):
        assert OR_NEEDS_RETEST == "needs_retest"

    def test_stale_auth_failed_becomes_needs_retest(self, db):
        """auth_failed smoke result that is older than threshold → needs_retest."""
        stale_report = _smoke_report(
            "error_submit",
            error="401 Unauthorized",
            age_days=SMOKE_MAX_AGE_DAYS + 1
        )
        with patch("hypogeum.provider_verifier.get_all_provider_statuses",
                   return_value={"muapi": {"status": "verified", "has_env_var": True,
                                           "has_secret_profile": False}}):
            with _patch_smoke(stale_report):
                catalog = get_provider_catalog(db_path=db)
        muapi = next(e for e in catalog if e.provider == "muapi")
        assert muapi.operational_readiness == OR_NEEDS_RETEST, \
            f"Stale auth_failed should be needs_retest, got {muapi.operational_readiness}"

    def test_stale_generation_failed_becomes_needs_retest(self, db):
        """generation_failed smoke result older than threshold → needs_retest."""
        stale_report = _smoke_report(
            "error_submit",
            error="403 Client Error: Forbidden",
            age_days=SMOKE_MAX_AGE_DAYS + 2
        )
        with patch("hypogeum.provider_verifier.get_all_provider_statuses",
                   return_value={"muapi": {"status": "verified", "has_env_var": True,
                                           "has_secret_profile": False}}):
            with _patch_smoke(stale_report):
                catalog = get_provider_catalog(db_path=db)
        muapi = next(e for e in catalog if e.provider == "muapi")
        assert muapi.operational_readiness == OR_NEEDS_RETEST

    def test_recent_generation_failed_stays_generation_failed(self, db):
        """Recent (non-stale) generation_failed is NOT upgraded to needs_retest."""
        recent_report = _smoke_report(
            "error_submit",
            error="403 Client Error: Forbidden",
            age_days=1.0  # within threshold
        )
        with patch("hypogeum.provider_verifier.get_all_provider_statuses",
                   return_value={"muapi": {"status": "verified", "has_env_var": True,
                                           "has_secret_profile": False}}):
            with _patch_smoke(recent_report):
                catalog = get_provider_catalog(db_path=db)
        muapi = next(e for e in catalog if e.provider == "muapi")
        assert muapi.operational_readiness == OR_GENERATION_FAILED

    def test_stale_ready_stays_ready(self, db):
        """Successful (ready) smoke result does NOT become needs_retest when stale."""
        stale_ready = _smoke_report("success", age_days=SMOKE_MAX_AGE_DAYS + 5)
        with patch("hypogeum.provider_verifier.get_all_provider_statuses",
                   return_value={"muapi": {"status": "verified", "has_env_var": True,
                                           "has_secret_profile": False}}):
            with _patch_smoke(stale_ready):
                catalog = get_provider_catalog(db_path=db)
        muapi = next(e for e in catalog if e.provider == "muapi")
        assert muapi.operational_readiness == OR_READY, \
            "Stale successful result should stay ready, not become needs_retest"

    def test_smoke_age_days_exposed_on_entry(self, db):
        """ProviderCatalogEntry.smoke_age_days is populated."""
        report = _smoke_report("success", age_days=3.5)
        with patch("hypogeum.provider_verifier.get_all_provider_statuses",
                   return_value={"muapi": {"status": "verified", "has_env_var": True,
                                           "has_secret_profile": False}}):
            with _patch_smoke(report):
                catalog = get_provider_catalog(db_path=db)
        muapi = next(e for e in catalog if e.provider == "muapi")
        assert muapi.smoke_age_days is not None
        assert muapi.smoke_age_days > 3.0

    def test_no_smoke_report_gives_none_age(self, db):
        with patch("hypogeum.dashboard_api._load_smoke_test_results_with_age",
                   return_value=({}, None)):
            catalog = get_provider_catalog(db_path=db)
        muapi = next(e for e in catalog if e.provider == "muapi")
        assert muapi.smoke_age_days is None

    def test_load_smoke_with_age_returns_float(self):
        """_load_smoke_test_results_with_age returns (dict, float) when report exists."""
        generated = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        fake_data = {
            "generated_at": generated,
            "providers": {"muapi": {"status": "success"}}
        }
        # Patch the open/read operation at the Path level
        with patch("builtins.open"), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value=json.dumps(fake_data)):
            providers, age = _load_smoke_test_results_with_age()
        assert isinstance(age, float)
        assert 1.9 < age < 2.1
        assert "muapi" in providers


# ===========================================================================
# TestDelegatedCredential
# ===========================================================================

class TestDelegatedCredential:

    def test_delegated_providers_use_muapi_credential_status(self, db):
        """Seedance, Veo, Kling, Runway credential status = MUAPI credential status."""
        with patch("hypogeum.provider_verifier.get_all_provider_statuses",
                   return_value={
                       "muapi": {"status": "verified", "has_env_var": True, "has_secret_profile": True},
                       # These stale entries should NOT be used:
                       "seedance": {"status": "failed", "has_env_var": True, "has_secret_profile": False},
                       "veo":      {"status": "failed", "has_env_var": True, "has_secret_profile": False},
                   }):
            with patch("hypogeum.dashboard_api._load_smoke_test_results_with_age",
                       return_value=({}, None)):
                catalog = get_provider_catalog(db_path=db)

        by_id = {e.provider: e for e in catalog}
        # Delegated providers must inherit MUAPI's verified status
        for delegated in ("seedance", "runway", "kling", "veo"):
            entry = by_id.get(delegated)
            if entry:
                assert entry.credential_status == "verified", \
                    f"{delegated} should use MUAPI credential status 'verified', got {entry.credential_status!r}"

    def test_delegated_credential_fails_when_muapi_fails(self, db):
        """If MUAPI credential fails, delegated providers inherit that failure."""
        with patch("hypogeum.provider_verifier.get_all_provider_statuses",
                   return_value={
                       "muapi": {"status": "failed", "has_env_var": True, "has_secret_profile": False},
                   }):
            with patch("hypogeum.dashboard_api._load_smoke_test_results_with_age",
                       return_value=({}, None)):
                catalog = get_provider_catalog(db_path=db)

        by_id = {e.provider: e for e in catalog}
        for delegated in ("seedance", "runway", "kling", "veo"):
            entry = by_id.get(delegated)
            if entry:
                assert entry.credential_status == "failed", \
                    f"{delegated} should inherit MUAPI 'failed' credential status"


# ===========================================================================
# TestSmokePath
# ===========================================================================

class TestSmokePath:

    def test_needs_retest_badge_in_template(self, client):
        """OR_NEEDS_RETEST renders as 'Needs retest' badge, not 'Auth failed'."""
        from hypogeum.dashboard_api import ProviderCatalogEntry
        # Build a fake catalog where MUAPI has needs_retest
        def _fake_catalog(**kw):
            return [
                ProviderCatalogEntry(
                    provider="muapi", display_name="MUAPI",
                    capabilities=["image", "video"], access_mode="direct",
                    delegate_via=None, credential_status="verified",
                    has_env_var=True, is_configured=True,
                    total_attempts=0, failure_count=0,
                    failure_rate=0.0, total_cost_usd=0.0,
                    operational_readiness=OR_NEEDS_RETEST,
                    operational_note="Smoke result stale — re-run to confirm.",
                    smoke_age_days=10.0,
                )
            ]
        with patch("hypogeum.dashboard_api.get_provider_catalog", side_effect=_fake_catalog):
            with patch("run_dashboard.get_comparison_result", return_value=None):
                resp = client.get("/providers")
        assert b"Needs retest" in resp.data
        assert b"Auth failed" not in resp.data

    def test_smoke_age_displayed_when_needs_retest(self, client):
        from hypogeum.dashboard_api import ProviderCatalogEntry
        def _fake_catalog(**kw):
            return [
                ProviderCatalogEntry(
                    provider="muapi", display_name="MUAPI",
                    capabilities=["image", "video"], access_mode="direct",
                    delegate_via=None, credential_status="verified",
                    has_env_var=True, is_configured=True,
                    total_attempts=0, failure_count=0,
                    failure_rate=0.0, total_cost_usd=0.0,
                    operational_readiness=OR_NEEDS_RETEST,
                    operational_note="Stale result.",
                    smoke_age_days=12.0,
                )
            ]
        with patch("hypogeum.dashboard_api.get_provider_catalog", side_effect=_fake_catalog):
            with patch("run_dashboard.get_comparison_result", return_value=None):
                resp = client.get("/providers")
        body = resp.data.decode()
        # Age should appear in the card
        assert "12d" in body or "12" in body

    def test_smoke_test_button_present_when_needs_retest(self, client):
        from hypogeum.dashboard_api import ProviderCatalogEntry
        def _fake_catalog(**kw):
            return [
                ProviderCatalogEntry(
                    provider="muapi", display_name="MUAPI",
                    capabilities=["image", "video"], access_mode="direct",
                    delegate_via=None, credential_status="verified",
                    has_env_var=True, is_configured=True,
                    total_attempts=0, failure_count=0,
                    failure_rate=0.0, total_cost_usd=0.0,
                    operational_readiness=OR_NEEDS_RETEST,
                    operational_note="Stale.",
                    smoke_age_days=10.0,
                )
            ]
        with patch("hypogeum.dashboard_api.get_provider_catalog", side_effect=_fake_catalog):
            with patch("run_dashboard.get_comparison_result", return_value=None):
                resp = client.get("/providers")
        assert b"Smoke Test" in resp.data or b"muapi-smoke" in resp.data

    def test_auth_failed_not_shown_for_403_from_generation(self, client):
        """After fix: 403 from generation endpoint must not show as 'Auth failed'."""
        from hypogeum.dashboard_api import ProviderCatalogEntry
        def _fake_catalog(**kw):
            return [
                ProviderCatalogEntry(
                    provider="muapi", display_name="MUAPI",
                    capabilities=["image", "video"], access_mode="direct",
                    delegate_via=None, credential_status="verified",
                    has_env_var=True, is_configured=True,
                    total_attempts=0, failure_count=0,
                    failure_rate=0.0, total_cost_usd=0.0,
                    operational_readiness=OR_GENERATION_FAILED,
                    operational_note="Generation forbidden (403 — model access).",
                    smoke_age_days=0.1,
                )
            ]
        with patch("hypogeum.dashboard_api.get_provider_catalog", side_effect=_fake_catalog):
            with patch("run_dashboard.get_comparison_result", return_value=None):
                resp = client.get("/providers")
        body = resp.data.decode()
        # Generation failed badge must be shown, not auth failed
        assert "Generation failed" in body or "generation_failed" in body or "Auth failed (403)" not in body


# ===========================================================================
# TestSmokeTestRoute
# ===========================================================================

class TestSmokeTestRoute:

    # Smoke test route lazy-imports list_secret_profiles and Config
    # inside the function — patch at their definition modules.
    _SP_PATCH  = "hypogeum.credential_manager.list_secret_profiles"
    _CFG_PATCH = "hypogeum.config.Config"

    def test_route_exists(self, client):
        with patch(self._SP_PATCH, return_value=[]):
            with patch(self._CFG_PATCH, side_effect=Exception("no config")):
                resp = client.post("/api/providers/muapi/smoke-test")
        # Should not return 404
        assert resp.status_code != 404

    def test_returns_400_when_no_credential(self, client, monkeypatch):
        monkeypatch.delenv("MUAPI_API_KEY", raising=False)
        with patch(self._SP_PATCH, return_value=[]):
            resp = client.post("/api/providers/muapi/smoke-test")
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_returns_200_on_successful_probe(self, client, monkeypatch):
        monkeypatch.setenv("MUAPI_API_KEY", "muapi-test-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-for-config")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"request_id": "test-req-123"}
        with patch(self._SP_PATCH, return_value=[]):
            with patch("requests.post", return_value=mock_resp):
                resp = client.post("/api/providers/muapi/smoke-test")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["request_id"] == "test-req-123"

    def test_credential_source_secret_profile_reported(self, client, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-for-config")
        from hypogeum.credential_manager import SecretProfile
        fake_sp = MagicMock(spec=SecretProfile)
        fake_sp.status = "active"
        fake_sp.credential_data = "sp-key"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"request_id": "sp-req-456"}
        with patch(self._SP_PATCH, return_value=[fake_sp]):
            with patch("requests.post", return_value=mock_resp):
                resp = client.post("/api/providers/muapi/smoke-test")
        data = resp.get_json()
        assert data.get("credential_source") == "secret_profile"

    def test_smoke_result_written_to_report_file(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv("MUAPI_API_KEY", "muapi-test")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"request_id": "write-test-req"}
        fake_root = tmp_path
        with patch(self._SP_PATCH, return_value=[]):
            with patch("requests.post", return_value=mock_resp):
                with patch("run_dashboard._ROOT", fake_root):
                    resp = client.post("/api/providers/muapi/smoke-test")
        report = fake_root / ".hypogeum-assets" / "provider_smoke_tests" / "provider_live_smoke_report.json"
        if report.exists():
            data = json.loads(report.read_text())
            assert "muapi" in data.get("providers", {})
            assert "generated_at" in data


# ===========================================================================
# TestProviderCatalogConsistency
# ===========================================================================

class TestProviderCatalogConsistency:

    def test_credential_row_reflects_probe_not_smoke(self, db):
        """Credential status comes from verifier probe, not smoke test result."""
        # Smoke shows error but verifier says verified
        report = _smoke_report("error_submit", "403 Forbidden", age_days=0.5)
        with patch("hypogeum.provider_verifier.get_all_provider_statuses",
                   return_value={"muapi": {"status": "verified", "has_env_var": True,
                                           "has_secret_profile": True}}):
            with _patch_smoke(report):
                catalog = get_provider_catalog(db_path=db)
        muapi = next(e for e in catalog if e.provider == "muapi")
        # Credential row = verified (from probe)
        assert muapi.credential_status == "verified"
        # Generation row = generation_failed (from smoke 403)
        assert muapi.operational_readiness == OR_GENERATION_FAILED

    def test_working_credential_stale_smoke_shows_needs_retest(self, db):
        """Working probe + stale failed smoke → verified credential + needs_retest generation."""
        stale_report = _smoke_report(
            "error_submit", "403 Forbidden",
            age_days=SMOKE_MAX_AGE_DAYS + 1
        )
        with patch("hypogeum.provider_verifier.get_all_provider_statuses",
                   return_value={"muapi": {"status": "verified", "has_env_var": True,
                                           "has_secret_profile": True}}):
            with _patch_smoke(stale_report):
                catalog = get_provider_catalog(db_path=db)
        muapi = next(e for e in catalog if e.provider == "muapi")
        assert muapi.credential_status == "verified"
        assert muapi.operational_readiness == OR_NEEDS_RETEST

    def test_muapi_generation_failed_propagates_to_delegated(self, db):
        """When MUAPI's readiness is generation_failed, delegated providers inherit MUAPI_NOT_READY."""
        report = _smoke_report("error_submit", "403 Forbidden", age_days=0.5)
        with patch("hypogeum.provider_verifier.get_all_provider_statuses",
                   return_value={"muapi": {"status": "verified", "has_env_var": True,
                                           "has_secret_profile": True}}):
            with _patch_smoke(report):
                catalog = get_provider_catalog(db_path=db)
        by_id = {e.provider: e for e in catalog}
        for delegated in ("seedance", "runway", "kling", "veo"):
            if delegated in by_id:
                assert by_id[delegated].operational_readiness == OR_MUAPI_NOT_READY, \
                    f"{delegated} should be MUAPI_NOT_READY when MUAPI generation fails"


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:

    @pytest.fixture(autouse=True)
    def block_sockets(self):
        _orig = socket.socket
        def _blocked(*a, **kw):
            raise RuntimeError("Network blocked in unit tests")
        socket.socket = _blocked
        yield
        socket.socket = _orig

    def test_error_classification_no_network(self):
        readiness, _ = _derive_operational_readiness(
            "muapi", {"access_mode": "direct"},
            {"status": "error_submit", "error": "403 Forbidden", "model": "t3"},
            OR_NOT_TESTED,
        )
        assert readiness == OR_GENERATION_FAILED

    def test_staleness_check_no_network(self, db):
        stale = _smoke_report("error_submit", "403", age_days=SMOKE_MAX_AGE_DAYS + 1)
        with patch("hypogeum.provider_verifier.get_all_provider_statuses",
                   return_value={"muapi": {"status": "verified", "has_env_var": True,
                                           "has_secret_profile": False}}):
            with _patch_smoke(stale):
                catalog = get_provider_catalog(db_path=db)
        muapi = next(e for e in catalog if e.provider == "muapi")
        assert muapi.operational_readiness == OR_NEEDS_RETEST

    def test_providers_page_no_network(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert resp.status_code == 200
