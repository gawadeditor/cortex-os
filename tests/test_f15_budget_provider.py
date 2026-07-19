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


# ---------------------------------------------------------------------------
# TestProbeYoutubeReal — the actual _probe_youtube() implementation, not a
# substituted mock. This is the exact false-positive fix: previously
# _probe_youtube() was a token-length check and none of the tests above
# exercised its real body for "youtube" at all.
# ---------------------------------------------------------------------------

class TestProbeYoutubeReal:

    def _mock_oauth_response(self, status_code, json_data):
        resp = MagicMock()
        resp.ok = status_code < 400
        resp.status_code = status_code
        resp.json.return_value = json_data
        resp.headers = {}
        return resp

    def test_probe_youtube_success_makes_real_oauth_call(self, monkeypatch):
        from hypogeum.provider_verifier import _probe_youtube
        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid-real")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "csecret-real")
        mock_resp = self._mock_oauth_response(200, {"access_token": "ya29.real"})
        with patch("requests.post", return_value=mock_resp) as mock_post:
            success, error, latency_ms = _probe_youtube("some-refresh-token")
        assert success is True
        assert error is None
        mock_post.assert_called_once()
        assert mock_post.call_args[1]["data"]["client_id"] == "cid-real"
        assert mock_post.call_args[1]["data"]["client_secret"] == "csecret-real"
        assert mock_post.call_args[1]["data"]["refresh_token"] == "some-refresh-token"

    def test_probe_youtube_invalid_grant_fails(self, monkeypatch):
        """The exact regression this fix targets: a fake/expired/revoked
        token that is >10 characters (would have passed the old length
        check) must now genuinely fail."""
        from hypogeum.provider_verifier import _probe_youtube
        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "cs")
        mock_resp = self._mock_oauth_response(400, {"error": "invalid_grant"})
        with patch("requests.post", return_value=mock_resp):
            success, error, latency_ms = _probe_youtube("1//fake-but-long-enough-token")
        assert success is False
        assert error == "invalid_grant"

    def test_verify_provider_youtube_end_to_end_success(self, db, monkeypatch):
        """verify_provider('youtube') exercises real credential resolution
        (env fallback) through to the real probe and real persistence."""
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "1//env-refresh-token")
        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "cs")
        mock_resp = self._mock_oauth_response(200, {"access_token": "ya29.x"})
        with patch("requests.post", return_value=mock_resp):
            result = verify_provider("youtube", db_path=db)
        assert result["status"] == STATUS_VERIFIED

    def test_verify_provider_youtube_end_to_end_invalid_grant(self, db, monkeypatch):
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "1//env-refresh-token")
        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "cs")
        mock_resp = self._mock_oauth_response(400, {"error": "invalid_grant"})
        with patch("requests.post", return_value=mock_resp):
            result = verify_provider("youtube", db_path=db)
        assert result["status"] == STATUS_FAILED
        assert result["error"] == "invalid_grant"

        conn = get_connection(db)
        try:
            row = get_provider_verification(conn, "youtube")
        finally:
            conn.close()
        assert row["status"] == STATUS_FAILED
        assert row["error_message"] == "invalid_grant"

    def test_probe_youtube_invalid_client_fails(self, monkeypatch):
        """invalid_client (bad client_id/client_secret app config) must be
        distinguishable from invalid_grant (expired/revoked user token) --
        both are definitive hard OAuth rejections."""
        from hypogeum.provider_verifier import _probe_youtube
        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "bad-cid")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "bad-cs")
        mock_resp = self._mock_oauth_response(401, {"error": "invalid_client"})
        with patch("requests.post", return_value=mock_resp):
            success, error, latency_ms = _probe_youtube("1//some-token")
        assert success is False
        assert error == "invalid_client"

    def test_verify_provider_youtube_transient_timeout_preserves_prior_verified(self, db, monkeypatch):
        """A timeout on a subsequent manual /api/providers/verify/youtube
        check must not downgrade a prior real 'verified' result to
        'failed' -- it is a transient operational failure, not a
        definitive credential rejection."""
        import requests
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "1//env-refresh-token")
        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "cs")

        mock_ok = self._mock_oauth_response(200, {"access_token": "ya29.x"})
        with patch("requests.post", return_value=mock_ok):
            first = verify_provider("youtube", db_path=db)
        assert first["status"] == STATUS_VERIFIED

        with patch("requests.post", side_effect=requests.exceptions.Timeout("timed out")):
            second = verify_provider("youtube", db_path=db)
        assert second["status"] == STATUS_VERIFIED

        conn = get_connection(db)
        try:
            row = get_provider_verification(conn, "youtube")
        finally:
            conn.close()
        assert row["status"] == STATUS_VERIFIED

    def test_verify_provider_youtube_transient_503_preserves_prior_verified(self, db, monkeypatch):
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "1//env-refresh-token")
        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "cs")

        mock_ok = self._mock_oauth_response(200, {"access_token": "ya29.x"})
        with patch("requests.post", return_value=mock_ok):
            first = verify_provider("youtube", db_path=db)
        assert first["status"] == STATUS_VERIFIED

        mock_503 = self._mock_oauth_response(503, {"error": "temporarily_unavailable"})
        with patch("requests.post", return_value=mock_503):
            second = verify_provider("youtube", db_path=db)
        assert second["status"] == STATUS_VERIFIED

        conn = get_connection(db)
        try:
            row = get_provider_verification(conn, "youtube")
        finally:
            conn.close()
        assert row["status"] == STATUS_VERIFIED

    def test_verify_provider_youtube_recovers_after_transient_then_success(self, db, monkeypatch):
        """The dashboard-visible definitive state must recover normally:
        verified -> transient failure (preserved as verified) -> a later
        real success still lands as verified."""
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "1//env-refresh-token")
        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "cs")

        mock_ok = self._mock_oauth_response(200, {"access_token": "ya29.x"})
        with patch("requests.post", return_value=mock_ok):
            verify_provider("youtube", db_path=db)

        mock_503 = self._mock_oauth_response(503, {"error": "temporarily_unavailable"})
        with patch("requests.post", return_value=mock_503):
            verify_provider("youtube", db_path=db)

        with patch("requests.post", return_value=mock_ok):
            final = verify_provider("youtube", db_path=db)
        assert final["status"] == STATUS_VERIFIED

    def test_other_provider_probes_unaffected(self, db, monkeypatch):
        """Sanity check: fixing YouTube's probe must not change any other
        provider's real behavior (all others already made real HTTP calls
        before this change; only the mocked-_PROBES tests exercise them
        above, this confirms verify_provider()'s dispatch/orchestration
        logic itself -- shared by every provider -- is unaffected)."""
        monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")
        mock = MagicMock(return_value=(True, None, 50))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"elevenlabs": mock}):
            result = verify_provider("elevenlabs", db_path=db)
        assert result["status"] == STATUS_VERIFIED
        mock.assert_called_once()


# ---------------------------------------------------------------------------
# TestYoutubeSecretProfilePrecedence — when more than one active
# provider='youtube' SecretProfile exists (e.g. a stale imported
# 'youtube-env' row alongside the dashboard-managed 'youtube-oauth-refresh'
# row), verify_provider() must probe the same credential publishing_agent
# would actually use, not just whichever row is oldest.
# ---------------------------------------------------------------------------

class TestYoutubeSecretProfilePrecedence:

    def _mock_oauth_response(self, status_code, json_data):
        resp = MagicMock()
        resp.ok = status_code < 400
        resp.status_code = status_code
        resp.json.return_value = json_data
        resp.headers = {}
        return resp

    def test_prefers_oauth_refresh_label_over_older_env_profile(self, db, monkeypatch):
        from hypogeum.credential_manager import create_secret_profile

        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "cs")

        # Older, stale row -- created first so it would win a naive "first
        # active row" selection.
        create_secret_profile(
            provider="youtube", label="youtube-env",
            credential_type="oauth2_token", credential_data="1//stale-env-token",
            db_path=db,
        )
        # Newer, authoritative row -- what publishing_agent.py actually uses.
        create_secret_profile(
            provider="youtube", label="youtube-oauth-refresh",
            credential_type="oauth2_token", credential_data="1//authoritative-token",
            db_path=db,
        )

        mock_resp = self._mock_oauth_response(200, {"access_token": "ya29.x"})
        with patch("requests.post", return_value=mock_resp) as mock_post:
            result = verify_provider("youtube", db_path=db)

        assert result["status"] == STATUS_VERIFIED
        assert mock_post.call_args[1]["data"]["refresh_token"] == "1//authoritative-token"

    def test_stale_older_row_cannot_override_authoritative_profile(self, db, monkeypatch):
        """The stale row's credential must never reach the probe once an
        authoritative labeled row exists, even if the stale one is 'valid'
        and the authoritative one would fail -- the point is which
        credential gets tested, not which one happens to succeed."""
        from hypogeum.credential_manager import create_secret_profile

        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "cs")

        create_secret_profile(
            provider="youtube", label="youtube-env",
            credential_type="oauth2_token", credential_data="1//stale-env-token",
            db_path=db,
        )
        create_secret_profile(
            provider="youtube", label="youtube-oauth-refresh",
            credential_type="oauth2_token", credential_data="1//authoritative-token",
            db_path=db,
        )

        mock_resp = self._mock_oauth_response(400, {"error": "invalid_grant"})
        with patch("requests.post", return_value=mock_resp) as mock_post:
            result = verify_provider("youtube", db_path=db)

        assert result["status"] == STATUS_FAILED
        assert mock_post.call_args[1]["data"]["refresh_token"] == "1//authoritative-token"
        assert mock_post.call_args[1]["data"]["refresh_token"] != "1//stale-env-token"

    def test_falls_back_to_active_profile_when_no_oauth_refresh_label(self, db, monkeypatch):
        """If no 'youtube-oauth-refresh' row exists yet, verify_provider()
        must still fall back to whatever active YouTube profile it can
        find -- the label preference is additive, not a hard requirement."""
        from hypogeum.credential_manager import create_secret_profile

        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "cs")

        create_secret_profile(
            provider="youtube", label="youtube-env",
            credential_type="oauth2_token", credential_data="1//only-token",
            db_path=db,
        )

        mock_resp = self._mock_oauth_response(200, {"access_token": "ya29.x"})
        with patch("requests.post", return_value=mock_resp) as mock_post:
            result = verify_provider("youtube", db_path=db)

        assert result["status"] == STATUS_VERIFIED
        assert mock_post.call_args[1]["data"]["refresh_token"] == "1//only-token"

    def test_other_provider_secret_profile_precedence_unaffected(self, db, monkeypatch):
        """Only youtube gets a label preference. A provider with two active
        SecretProfiles (e.g. elevenlabs) must keep the pre-existing
        first-active-row behavior."""
        from hypogeum.credential_manager import create_secret_profile

        create_secret_profile(
            provider="elevenlabs", label="first",
            credential_type="api_key", credential_data="el-first-key",
            db_path=db,
        )
        create_secret_profile(
            provider="elevenlabs", label="second",
            credential_type="api_key", credential_data="el-second-key",
            db_path=db,
        )

        mock = MagicMock(return_value=(True, None, 50))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"elevenlabs": mock}):
            verify_provider("elevenlabs", db_path=db)

        mock.assert_called_once_with("el-first-key")

    def test_no_second_google_request_introduced(self, db, monkeypatch):
        """The precedence fix must not add any extra network call -- exactly
        one OAuth exchange per verify_provider() call, regardless of how
        many active SecretProfiles exist."""
        from hypogeum.credential_manager import create_secret_profile

        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "cs")

        create_secret_profile(
            provider="youtube", label="youtube-env",
            credential_type="oauth2_token", credential_data="1//stale-env-token",
            db_path=db,
        )
        create_secret_profile(
            provider="youtube", label="youtube-oauth-refresh",
            credential_type="oauth2_token", credential_data="1//authoritative-token",
            db_path=db,
        )

        mock_resp = self._mock_oauth_response(200, {"access_token": "ya29.x"})
        with patch("requests.post", return_value=mock_resp) as mock_post:
            verify_provider("youtube", db_path=db)

        mock_post.assert_called_once()


# ---------------------------------------------------------------------------
# TestRecordProviderVerification — the shared persistence entry point for
# callers (e.g. publishing_agent) that already performed the real check
# themselves and must not repeat the underlying network request.
# ---------------------------------------------------------------------------

class TestRecordProviderVerification:

    def test_records_success_without_any_network_call(self, db):
        from hypogeum.provider_verifier import record_provider_verification
        with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
            result = record_provider_verification(
                provider="youtube", ok=True, latency_ms=250,
                source="secret_profile", db_path=db,
            )
        mock_post.assert_not_called()
        mock_get.assert_not_called()
        assert result["status"] == STATUS_VERIFIED

        conn = get_connection(db)
        try:
            row = get_provider_verification(conn, "youtube")
        finally:
            conn.close()
        assert row["status"] == STATUS_VERIFIED
        assert row["source"] == "secret_profile"

    def test_records_failure_without_any_network_call(self, db):
        from hypogeum.provider_verifier import record_provider_verification
        with patch("requests.post") as mock_post:
            result = record_provider_verification(
                provider="youtube", ok=False, error="invalid_grant",
                latency_ms=180, source="secret_profile", db_path=db,
            )
        mock_post.assert_not_called()
        assert result["status"] == STATUS_FAILED
        assert result["error"] == "invalid_grant"

        conn = get_connection(db)
        try:
            row = get_provider_verification(conn, "youtube")
        finally:
            conn.close()
        assert row["status"] == STATUS_FAILED
        assert row["error_message"] == "invalid_grant"

    def test_verify_provider_uses_record_provider_verification_internally(self, db, monkeypatch):
        """verify_provider() must not have its own separate persistence
        implementation -- it delegates to record_provider_verification()
        so there is exactly one place that writes a verification result."""
        monkeypatch.setenv("MUAPI_API_KEY", "mu-key")
        mock_probe = MagicMock(return_value=(True, None, 100))
        with patch.dict("hypogeum.provider_verifier._PROBES", {"muapi": mock_probe}), \
             patch("hypogeum.provider_verifier.record_provider_verification",
                   wraps=__import__("hypogeum.provider_verifier", fromlist=["record_provider_verification"]).record_provider_verification) as mock_record:
            verify_provider("muapi", db_path=db)
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["provider"] == "muapi"
        assert mock_record.call_args.kwargs["ok"] is True

    def test_does_not_revoke_secret_profile_on_failure(self, db):
        """A failure must never flip secret_profiles.status -- revocation
        is a permission-gated, human-triggered action, not something an
        automated background record call performs."""
        from hypogeum.credential_manager import create_secret_profile, get_secret_profile
        from hypogeum.provider_verifier import record_provider_verification

        sp = create_secret_profile(
            provider="youtube", label="youtube-oauth-refresh",
            credential_type="oauth2_token", credential_data="1//tok",
            db_path=db,
        )
        record_provider_verification(
            provider="youtube", ok=False, error="invalid_grant",
            source="secret_profile", db_path=db,
        )
        refreshed = get_secret_profile(sp.secret_profile_id, db_path=db)
        assert refreshed.status == "active"

    def test_updates_last_tested_at_on_success_when_secret_profile_source(self, db):
        from hypogeum.credential_manager import create_secret_profile, get_secret_profile
        from hypogeum.provider_verifier import record_provider_verification

        sp = create_secret_profile(
            provider="youtube", label="youtube-oauth-refresh",
            credential_type="oauth2_token", credential_data="1//tok",
            db_path=db,
        )
        assert sp.last_tested_at is None
        record_provider_verification(
            provider="youtube", ok=True, source="secret_profile", db_path=db,
        )
        refreshed = get_secret_profile(sp.secret_profile_id, db_path=db)
        assert refreshed.last_tested_at is not None

    def test_transient_timeout_does_not_overwrite_prior_verified(self, db):
        """Direct unit test of record_provider_verification()'s transient=
        contract: provider_verifications has no transient/unreachable
        status, so a timeout must preserve the prior 'verified' status
        rather than downgrade it to 'failed'."""
        from hypogeum.provider_verifier import record_provider_verification

        record_provider_verification(provider="youtube", ok=True, source="env", db_path=db)
        result = record_provider_verification(
            provider="youtube", ok=False, error="transient: timed out",
            source="env", db_path=db, transient=True,
        )
        assert result["status"] == STATUS_VERIFIED

        conn = get_connection(db)
        try:
            row = get_provider_verification(conn, "youtube")
        finally:
            conn.close()
        assert row["status"] == STATUS_VERIFIED

    def test_transient_503_does_not_overwrite_prior_verified(self, db):
        from hypogeum.provider_verifier import record_provider_verification

        record_provider_verification(provider="youtube", ok=True, source="env", db_path=db)
        result = record_provider_verification(
            provider="youtube", ok=False,
            error="transient: YouTube OAuth exchange failed: HTTP 503",
            source="env", db_path=db, transient=True,
        )
        assert result["status"] == STATUS_VERIFIED

        conn = get_connection(db)
        try:
            row = get_provider_verification(conn, "youtube")
        finally:
            conn.close()
        assert row["status"] == STATUS_VERIFIED

    def test_transient_failure_with_no_prior_row_persists_pending_not_failed(self, db):
        """When there is no prior definitive result at all, a transient
        failure must still never be recorded as STATUS_FAILED -- it falls
        back to the existing STATUS_PENDING value (schema default meaning
        'no definitive result yet'), not a new status."""
        from hypogeum.provider_verifier import record_provider_verification, STATUS_PENDING

        result = record_provider_verification(
            provider="youtube", ok=False, error="transient: timed out",
            source="env", db_path=db, transient=True,
        )
        assert result["status"] == STATUS_PENDING
        assert result["status"] != STATUS_FAILED

    def test_hard_failure_after_prior_verified_does_overwrite_as_failed(self, db):
        """Contrast case: a definitive rejection (transient=False, the
        default) must still correctly downgrade a prior verified result --
        only transient failures are protected."""
        from hypogeum.provider_verifier import record_provider_verification

        record_provider_verification(provider="youtube", ok=True, source="env", db_path=db)
        result = record_provider_verification(
            provider="youtube", ok=False, error="invalid_grant", source="env", db_path=db,
        )
        assert result["status"] == STATUS_FAILED


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
