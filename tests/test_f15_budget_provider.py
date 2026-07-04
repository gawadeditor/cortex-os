"""
tests/test_f15_budget_provider.py — F15 Budget + Provider Verification

Coverage:
    Budget ceiling:
        - ceiling can be updated via POST /api/setup/budget-ceiling
        - updated ceiling is written to .env file
        - updated ceiling reflected in os.environ immediately
        - /create disabled when ceiling below estimated cost
        - invalid ceiling values rejected (negative, > 9999)
        - api/setup/preflight returns budget_ceiling_usd in JSON

    Provider verification:
        - verify_provider returns verified on successful probe (mocked)
        - verify_provider returns failed on probe failure (mocked)
        - verify_provider returns missing when no credential
        - verify_provider returns not_verifiable for freepik
        - seedance uses MUAPI probe (env var MUAPI_API_KEY)
        - result persisted in provider_verifications DB table
        - get_all_provider_statuses includes saved status for env-var creds
        - provider page shows Saved before test, Verified after
        - provider page no longer shows Connected unless verified
        - POST /api/providers/verify/openrouter returns JSON with status
        - POST /api/providers/verify/unknown_xyz returns 404
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import initialize_schema, get_connection, get_provider_verification
from hypogeum.setup_preflight import _ESTIMATED_COST_LOW, _ESTIMATED_COST_HIGH, _REQUIRED_ENV_VARS
from hypogeum.provider_verifier import (
    verify_provider,
    get_all_provider_statuses,
    STATUS_VERIFIED,
    STATUS_FAILED,
    STATUS_MISSING,
    STATUS_NOT_VERIFIABLE,
    STATUS_SAVED,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "f15_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def env_file(tmp_path):
    """A temporary .env file."""
    f = tmp_path / ".env"
    f.write_text("FINANCE_BUDGET_CEILING_USD=15.00\n", encoding="utf-8")
    return f


@pytest.fixture()
def client(db, tmp_path):
    import run_dashboard as server
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db)
    # Point the budget-ceiling writer at the tmp env file
    server._ENV_PATH = tmp_path / ".env"
    (tmp_path / ".env").write_text("FINANCE_BUDGET_CEILING_USD=15.00\n")
    with server.app.test_client() as c:
        yield c


@pytest.fixture()
def all_env(monkeypatch):
    for var in _REQUIRED_ENV_VARS:
        monkeypatch.setenv(var, f"fake-{var}-value")


# ---------------------------------------------------------------------------
# Budget Ceiling — Unit
# ---------------------------------------------------------------------------

class TestBudgetCeilingUpdate:

    def test_update_writes_os_environ(self, monkeypatch, tmp_path):
        import run_dashboard as server
        orig = server._ENV_PATH
        server._ENV_PATH = tmp_path / ".env"
        (tmp_path / ".env").write_text("FINANCE_BUDGET_CEILING_USD=15.00\n")
        try:
            result = server._update_budget_ceiling(35.0)
            assert result["ok"] is True
            assert os.environ.get("FINANCE_BUDGET_CEILING_USD") == "35.00"
        finally:
            server._ENV_PATH = orig

    def test_update_writes_env_file(self, tmp_path):
        import run_dashboard as server
        orig = server._ENV_PATH
        env_f = tmp_path / ".env"
        env_f.write_text("# comment\nFINANCE_BUDGET_CEILING_USD=15.00\n")
        server._ENV_PATH = env_f
        try:
            server._update_budget_ceiling(40.0)
            content = env_f.read_text()
            assert "FINANCE_BUDGET_CEILING_USD=40.00" in content
            assert "# comment" in content  # preserves comments
        finally:
            server._ENV_PATH = orig

    def test_update_appends_when_key_missing(self, tmp_path):
        import run_dashboard as server
        orig = server._ENV_PATH
        env_f = tmp_path / ".env"
        env_f.write_text("OPENROUTER_API_KEY=sk-test\n")
        server._ENV_PATH = env_f
        try:
            server._update_budget_ceiling(30.0)
            content = env_f.read_text()
            assert "FINANCE_BUDGET_CEILING_USD=30.00" in content
        finally:
            server._ENV_PATH = orig

    def test_negative_ceiling_rejected(self, tmp_path):
        import run_dashboard as server
        orig = server._ENV_PATH
        server._ENV_PATH = tmp_path / ".env"
        try:
            result = server._update_budget_ceiling(-5.0)
            assert result["ok"] is False
        finally:
            server._ENV_PATH = orig

    def test_excessively_large_ceiling_rejected(self, tmp_path):
        import run_dashboard as server
        orig = server._ENV_PATH
        server._ENV_PATH = tmp_path / ".env"
        try:
            result = server._update_budget_ceiling(99999.0)
            assert result["ok"] is False
        finally:
            server._ENV_PATH = orig


class TestBudgetCeilingRoute:

    def test_post_budget_ceiling_updates_value(self, client, monkeypatch):
        resp = client.post(
            "/api/setup/budget-ceiling",
            json={"ceiling": 35.0},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["new_ceiling"] == pytest.approx(35.0)

    def test_post_invalid_ceiling_returns_400(self, client):
        resp = client.post(
            "/api/setup/budget-ceiling",
            json={"ceiling": -10.0},
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False

    def test_post_non_numeric_ceiling_returns_400(self, client):
        resp = client.post(
            "/api/setup/budget-ceiling",
            json={"ceiling": "bad"},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_api_preflight_has_budget_ceiling(self, client):
        resp = client.get("/api/setup/preflight")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "budget_ceiling_usd" in data


class TestCreatePageBudgetGating:

    def test_create_disabled_when_ceiling_too_low(self, client, monkeypatch, all_env):
        monkeypatch.setenv("FINANCE_BUDGET_CEILING_USD", str(_ESTIMATED_COST_LOW - 3.0))
        resp = client.get("/create")
        assert resp.status_code == 200
        html = resp.data.decode()
        # button should be disabled
        assert "disabled" in html

    def test_create_shows_setup_link_when_budget_too_low(self, client, monkeypatch, all_env):
        monkeypatch.setenv("FINANCE_BUDGET_CEILING_USD", "1.0")
        resp = client.get("/create")
        html = resp.data.decode()
        assert "/setup" in html

    def test_setup_page_shows_budget_ceiling_section(self, client):
        resp = client.get("/setup")
        html = resp.data.decode()
        assert "FINANCE_BUDGET_CEILING_USD" in html or "Budget" in html

    def test_setup_page_shows_update_form(self, client):
        resp = client.get("/setup")
        html = resp.data.decode()
        assert "ceiling-input" in html or "update" in html.lower()


# ---------------------------------------------------------------------------
# Provider Verification — Unit
# ---------------------------------------------------------------------------

class TestVerifyProvider:

    def _mock_probe(self, provider, success, error=None, latency=100):
        """Return a context manager that patches _PROBES[provider] with a mock."""
        mock = MagicMock(return_value=(success, error, latency))
        return patch.dict("hypogeum.provider_verifier._PROBES", {provider: mock}), mock

    def test_verify_openrouter_success(self, db, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
        mock = MagicMock(return_value=(True, None, 120))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"openrouter": mock}):
            result = verify_provider("openrouter", db_path=db)
        assert result["status"] == STATUS_VERIFIED
        assert result["error"] is None

    def test_verify_openrouter_failure(self, db, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-bad-key")
        mock = MagicMock(return_value=(False, "HTTP 401", 90))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"openrouter": mock}):
            result = verify_provider("openrouter", db_path=db)
        assert result["status"] == STATUS_FAILED
        assert "401" in result["error"]

    def test_verify_elevenlabs_success(self, db, monkeypatch):
        monkeypatch.setenv("ELEVENLABS_API_KEY", "el_fake_key")
        mock = MagicMock(return_value=(True, None, 200))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"elevenlabs": mock}):
            result = verify_provider("elevenlabs", db_path=db)
        assert result["status"] == STATUS_VERIFIED

    def test_verify_elevenlabs_failure(self, db, monkeypatch):
        monkeypatch.setenv("ELEVENLABS_API_KEY", "el_bad")
        mock = MagicMock(return_value=(False, "HTTP 401", 100))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"elevenlabs": mock}):
            result = verify_provider("elevenlabs", db_path=db)
        assert result["status"] == STATUS_FAILED

    def test_verify_muapi_success(self, db, monkeypatch):
        monkeypatch.setenv("MUAPI_API_KEY", "mu-test-key")
        mock = MagicMock(return_value=(True, None, 150))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"muapi": mock}):
            result = verify_provider("muapi", db_path=db)
        assert result["status"] == STATUS_VERIFIED

    def test_verify_seedance_uses_muapi_probe(self, db, monkeypatch):
        """Seedance shares the MUAPI_API_KEY and uses the MUAPI probe."""
        monkeypatch.setenv("MUAPI_API_KEY", "mu-shared-key")
        mock = MagicMock(return_value=(True, None, 180))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"seedance": mock}):
            result = verify_provider("seedance", db_path=db)
        mock.assert_called_once()
        assert result["status"] == STATUS_VERIFIED

    def test_verify_freepik_not_verifiable(self, db, monkeypatch):
        # freepik now has a real probe (Phase 1J). A bad key returns STATUS_FAILED.
        monkeypatch.setenv("FREEPIK_API_KEY", "fpk-some-key")
        fail_probe = MagicMock(return_value=(False, "HTTP 401 — invalid API key", 50))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"freepik": fail_probe}):
            result = verify_provider("freepik", db_path=db)
        assert result["status"] == STATUS_FAILED

    def test_verify_missing_credential_returns_missing(self, db, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "")
        result = verify_provider("openrouter", db_path=db)
        assert result["status"] == STATUS_MISSING

    def test_verify_result_persisted_in_db(self, db, monkeypatch):
        monkeypatch.setenv("MUAPI_API_KEY", "mu-key-123")
        mock = MagicMock(return_value=(True, None, 100))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"muapi": mock}):
            verify_provider("muapi", db_path=db)

        conn = get_connection(db)
        try:
            row = get_provider_verification(conn, "muapi")
        finally:
            conn.close()

        assert row is not None
        assert row["status"] == STATUS_VERIFIED

    def test_verify_failure_persisted_in_db(self, db, monkeypatch):
        monkeypatch.setenv("ELEVENLABS_API_KEY", "el-bad")
        mock = MagicMock(return_value=(False, "HTTP 403", 50))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"elevenlabs": mock}):
            verify_provider("elevenlabs", db_path=db)

        conn = get_connection(db)
        try:
            row = get_provider_verification(conn, "elevenlabs")
        finally:
            conn.close()

        assert row is not None
        assert row["status"] == STATUS_FAILED
        assert row["error_message"] == "HTTP 403"

    def test_verify_result_has_checked_at(self, db, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-key")
        mock = MagicMock(return_value=(True, None, 100))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"openrouter": mock}):
            result = verify_provider("openrouter", db_path=db)
        assert result.get("checked_at")


class TestGetAllProviderStatuses:

    def test_returns_dict_with_known_providers(self, db, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-key")
        statuses = get_all_provider_statuses(db_path=db)
        assert isinstance(statuses, dict)
        assert "openrouter" in statuses

    def test_env_var_set_shows_saved_before_test(self, db, monkeypatch):
        monkeypatch.setenv("MUAPI_API_KEY", "mu-key")
        statuses = get_all_provider_statuses(db_path=db)
        assert statuses["muapi"]["status"] == STATUS_SAVED

    def test_no_credential_shows_missing(self, db, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "")
        statuses = get_all_provider_statuses(db_path=db)
        if "openrouter" in statuses:
            assert statuses["openrouter"]["status"] == STATUS_MISSING

    def test_after_verify_shows_verified(self, db, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-key")
        mock = MagicMock(return_value=(True, None, 80))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"openrouter": mock}):
            verify_provider("openrouter", db_path=db)
        statuses = get_all_provider_statuses(db_path=db)
        assert statuses["openrouter"]["status"] == STATUS_VERIFIED

    def test_freepik_has_not_verifiable_after_verify(self, db, monkeypatch):
        # freepik now has a real probe (Phase 1J); not_verifiable=False after failed verify.
        monkeypatch.setenv("FREEPIK_API_KEY", "fpk-key")
        fail_probe = MagicMock(return_value=(False, "HTTP 401 — invalid API key", 50))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"freepik": fail_probe}):
            verify_provider("freepik", db_path=db)
        statuses = get_all_provider_statuses(db_path=db)
        assert statuses.get("freepik", {}).get("status") == STATUS_FAILED
        assert statuses.get("freepik", {}).get("not_verifiable") is False


# ---------------------------------------------------------------------------
# Provider Verification — Routes
# ---------------------------------------------------------------------------

class TestProviderVerifyRoute:

    def test_verify_openrouter_route_returns_200(self, client, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        mock = MagicMock(return_value=(True, None, 100))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"openrouter": mock}):
            resp = client.post("/api/providers/verify/openrouter")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["status"] == STATUS_VERIFIED

    def test_verify_unknown_provider_returns_404(self, client):
        resp = client.post("/api/providers/verify/totally_unknown_xyz")
        assert resp.status_code == 404

    def test_verify_missing_credential_returns_missing_status(self, client, monkeypatch):
        # Empty env var + fresh DB (no SecretProfiles) = missing
        monkeypatch.setenv("OPENROUTER_API_KEY", "")
        resp = client.post("/api/providers/verify/openrouter")
        data = resp.get_json()
        assert data["status"] == STATUS_MISSING

    def test_verify_freepik_returns_not_verifiable(self, client, monkeypatch):
        # freepik now has a real probe (Phase 1J); bad key → STATUS_FAILED.
        monkeypatch.setenv("FREEPIK_API_KEY", "fpk-key")
        fail_probe = MagicMock(return_value=(False, "HTTP 401 — invalid API key", 50))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"freepik": fail_probe}):
            resp = client.post("/api/providers/verify/freepik")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == STATUS_FAILED

    def test_verify_seedance_route_exists(self, client, monkeypatch):
        monkeypatch.setenv("MUAPI_API_KEY", "mu-key")
        mock = MagicMock(return_value=(True, None, 90))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"seedance": mock}):
            resp = client.post("/api/providers/verify/seedance")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == STATUS_VERIFIED


# ---------------------------------------------------------------------------
# Providers Page — status badge rendering
# ---------------------------------------------------------------------------

class TestProvidersPageStatusBadges:

    def _html(self, client):
        resp = client.get("/providers")
        assert resp.status_code == 200
        return resp.data.decode()

    def test_page_does_not_show_connected_for_unverified(self, client, monkeypatch):
        # Ensure env vars present but no verification run
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-key")
        html = self._html(client)
        # The word "Connected" (capital C) should NOT appear since we replaced the badge
        assert "✓ Connected" not in html, (
            "'Connected' badge still showing — should be 'Verified' or status-based"
        )

    def test_page_shows_saved_badge_before_verify(self, client, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-key")
        html = self._html(client)
        # "Saved" badge should appear when credential present but not verified
        assert "Saved" in html

    def test_page_shows_not_configured_when_no_credential(self, client, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "")
        html = self._html(client)
        assert "Not configured" in html

    def test_page_shows_verified_badge_after_verify(self, client, db, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-key")
        mock = MagicMock(return_value=(True, None, 100))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"openrouter": mock}):
            client.post("/api/providers/verify/openrouter")
        html = self._html(client)
        assert "Verified" in html

    def test_page_shows_failed_badge_after_bad_verify(self, client, monkeypatch):
        monkeypatch.setenv("ELEVENLABS_API_KEY", "el-bad")
        mock = MagicMock(return_value=(False, "HTTP 401", 50))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"elevenlabs": mock}):
            client.post("/api/providers/verify/elevenlabs")
        html = self._html(client)
        assert "Failed" in html

    def test_page_has_test_buttons(self, client):
        html = self._html(client)
        assert "testProvider" in html or "btn-test" in html

    def test_page_shows_freepik_not_verifiable(self, client, monkeypatch):
        monkeypatch.setenv("FREEPIK_API_KEY", "fpk-key")
        # Run verify to store status
        client.post("/api/providers/verify/freepik")
        html = self._html(client)
        # freepik should show "not verifiable" style badge
        assert "not verifiable" in html.lower() or "no probe" in html.lower()
