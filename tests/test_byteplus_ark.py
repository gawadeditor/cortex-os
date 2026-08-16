"""
tests/test_byteplus_ark.py — Phase 1H: BytePlus ModelArk / Seedance Official tests

Coverage:
    TestBytePlusArkClient    — probe_credential, submit_video_task, get_task_result,
                               poll_until_complete, extract_video_url
    TestBytePlusArkExceptions — error hierarchy, auth failure, quota exhausted
    TestProviderVerifier     — seedance_official in env var map and probes table
    TestProviderCatalog      — seedance_official is 'direct', not 'direct_placeholder'
    TestConfig               — BYTEPLUS_ARK_* keys present and default-correct
    TestDashboardRoute       — POST /api/providers/verify/seedance_official
    TestConnectForm          — seedance_official in connect form, correct env_var
    TestNoNetworkCalls       — no sockets opened during unit tests
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_dashboard as server
from hypogeum.dormant_providers.byteplus_ark import (
    BytePlusArkAuthFailure,
    BytePlusArkError,
    BytePlusArkQuotaExhausted,
    _DEFAULT_BASE_URL,
    _VALID_DURATIONS,
    _round_duration,
    _resolution_for_aspect,
    extract_video_url,
    probe_credential,
    submit_video_task,
    get_task_result,
    poll_until_complete,
)
from hypogeum.db import initialize_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "ark_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def client(db):
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db)
    with server.app.test_client() as c:
        yield c


def _make_config(api_key="ark-test-key", base_url=None, model=None):
    cfg = MagicMock()
    cfg.BYTEPLUS_ARK_API_KEY  = api_key
    cfg.BYTEPLUS_ARK_BASE_URL = base_url or _DEFAULT_BASE_URL
    cfg.BYTEPLUS_ARK_MODEL    = model or "seedance-1-lite"
    return cfg


def _mock_urlopen(status: int, body: dict):
    """Return a context-manager-compatible mock for urllib.request.urlopen."""
    import io
    import http.client

    class _FakeResp:
        def __init__(self):
            self.status = status
            self._data = json.dumps(body).encode()
        def read(self):
            return self._data
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    return _FakeResp()


def _mock_http_error(code: int, body: bytes = b"error"):
    exc = urllib.error.HTTPError(url="", code=code, msg="", hdrs=None, fp=BytesIO(body))
    return exc


# ===========================================================================
# TestBytePlusArkClient
# ===========================================================================

class TestBytePlusArkClient:

    # ── probe_credential ──────────────────────────────────────────────────

    def test_probe_returns_true_on_200(self):
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(200, {"data": []})):
            success, error, latency_ms = probe_credential("ark-test-key")
        assert success is True
        assert error  is None
        assert latency_ms >= 0

    def test_probe_returns_false_on_401(self):
        with patch("urllib.request.urlopen", side_effect=_mock_http_error(401)):
            success, error, latency_ms = probe_credential("bad-key")
        assert success is False
        assert "401" in (error or "")

    def test_probe_returns_false_on_403(self):
        with patch("urllib.request.urlopen", side_effect=_mock_http_error(403)):
            success, error, latency_ms = probe_credential("bad-key")
        assert success is False

    def test_probe_returns_false_on_connection_error(self):
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            success, error, latency_ms = probe_credential("ark-test-key")
        assert success is False
        assert error is not None

    def test_probe_does_not_raise(self):
        with patch("urllib.request.urlopen", side_effect=RuntimeError("unexpected")):
            success, error, latency_ms = probe_credential("ark-test-key")
        assert success is False
        assert error is not None

    # ── submit_video_task ────────────────────────────────────────────────

    def test_submit_returns_task_id(self):
        response_body = {"id": "task-abc-123", "status": "submitted"}
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(200, response_body)):
            cfg = _make_config()
            task_id = submit_video_task("ancient temple", cfg, "ep_001")
        assert task_id == "task-abc-123"

    def test_submit_accepts_aspect_ratio(self):
        captured_payload = {}

        def _capture(req, timeout=None):
            captured_payload["data"] = json.loads(req.data.decode())
            return _mock_urlopen(200, {"id": "t-1"})

        cfg = _make_config()
        with patch("urllib.request.urlopen", side_effect=_capture):
            submit_video_task("prompt", cfg, "ep_001", aspect_ratio="9:16")
        params = captured_payload["data"]["parameters"]
        assert params["resolution"] == "720x1280"

    def test_submit_rounds_duration_to_8_for_large(self):
        captured_payload = {}

        def _capture(req, timeout=None):
            captured_payload["data"] = json.loads(req.data.decode())
            return _mock_urlopen(200, {"id": "t-1"})

        cfg = _make_config()
        with patch("urllib.request.urlopen", side_effect=_capture):
            submit_video_task("prompt", cfg, "ep_001", duration_seconds=10.0)
        assert captured_payload["data"]["parameters"]["duration"] == 8

    def test_submit_rounds_duration_to_5_for_small(self):
        captured_payload = {}

        def _capture(req, timeout=None):
            captured_payload["data"] = json.loads(req.data.decode())
            return _mock_urlopen(200, {"id": "t-1"})

        cfg = _make_config()
        with patch("urllib.request.urlopen", side_effect=_capture):
            submit_video_task("prompt", cfg, "ep_001", duration_seconds=3.0)
        assert captured_payload["data"]["parameters"]["duration"] == 5

    def test_submit_includes_reference_image_when_provided(self):
        captured_payload = {}

        def _capture(req, timeout=None):
            captured_payload["data"] = json.loads(req.data.decode())
            return _mock_urlopen(200, {"id": "t-1"})

        cfg = _make_config()
        with patch("urllib.request.urlopen", side_effect=_capture):
            submit_video_task("prompt", cfg, "ep_001",
                              reference_image_url="https://example.com/ref.jpg")
        content = captured_payload["data"]["content"]
        types = [c["type"] for c in content]
        assert "image_url" in types

    def test_submit_raises_auth_failure_on_401(self):
        with patch("urllib.request.urlopen", side_effect=_mock_http_error(401)):
            cfg = _make_config()
            with pytest.raises(BytePlusArkAuthFailure):
                submit_video_task("prompt", cfg, "ep_001")

    def test_submit_raises_quota_exhausted_on_402(self):
        with patch("urllib.request.urlopen", side_effect=_mock_http_error(402)):
            cfg = _make_config()
            with pytest.raises(BytePlusArkQuotaExhausted):
                submit_video_task("prompt", cfg, "ep_001")

    def test_submit_raises_when_no_task_id_in_response(self):
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(200, {"status": "submitted"})):
            cfg = _make_config()
            with pytest.raises(BytePlusArkError, match="task_id"):
                submit_video_task("prompt", cfg, "ep_001")

    def test_submit_raises_auth_failure_when_no_key(self):
        cfg = _make_config(api_key="")
        with pytest.raises(BytePlusArkAuthFailure):
            submit_video_task("prompt", cfg, "ep_001")

    def test_submit_sends_correct_model(self):
        captured_payload = {}

        def _capture(req, timeout=None):
            captured_payload["data"] = json.loads(req.data.decode())
            return _mock_urlopen(200, {"id": "t-1"})

        cfg = _make_config(model="seedance-1-pro")
        with patch("urllib.request.urlopen", side_effect=_capture):
            submit_video_task("prompt", cfg, "ep_001")
        assert captured_payload["data"]["model"] == "seedance-1-pro"

    # ── get_task_result ───────────────────────────────────────────────────

    def test_get_task_result_returns_dict(self):
        body = {"id": "t-1", "status": "processing"}
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(200, body)):
            cfg = _make_config()
            result = get_task_result("t-1", cfg, "ep_001")
        assert result["status"] == "processing"

    def test_get_task_result_raises_auth_failure_on_401(self):
        with patch("urllib.request.urlopen", side_effect=_mock_http_error(401)):
            cfg = _make_config()
            with pytest.raises(BytePlusArkAuthFailure):
                get_task_result("t-1", cfg, "ep_001")

    # ── poll_until_complete ────────────────────────────────────────────────

    def test_poll_returns_result_on_success(self):
        succeeded = {"id": "t-1", "status": "succeeded",
                     "content": [{"type": "video_url", "video_url": {"url": "https://cdn.example.com/v.mp4"}}]}
        call_count = {"n": 0}

        def _get(req, timeout=None):
            call_count["n"] += 1
            if call_count["n"] < 3:
                return _mock_urlopen(200, {"id": "t-1", "status": "processing"})
            return _mock_urlopen(200, succeeded)

        cfg = _make_config()
        with patch("urllib.request.urlopen", side_effect=_get):
            with patch("time.sleep"):
                result = poll_until_complete("t-1", cfg, "ep_001")
        assert result["status"] == "succeeded"

    def test_poll_raises_on_failed_task(self):
        failed = {"id": "t-1", "status": "failed", "error": "content policy"}
        cfg = _make_config()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(200, failed)):
            with patch("time.sleep"):
                with pytest.raises(BytePlusArkError, match="failed"):
                    poll_until_complete("t-1", cfg, "ep_001")

    def test_poll_raises_on_timeout(self):
        processing = {"id": "t-1", "status": "processing"}
        cfg = _make_config()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(200, processing)):
            with patch("time.sleep"):
                with patch("time.monotonic", side_effect=[0.0, 0.0, 9999.0, 9999.0]):
                    with pytest.raises(BytePlusArkError, match="did not complete"):
                        poll_until_complete("t-1", cfg, "ep_001", timeout_seconds=1.0)

    # ── extract_video_url ─────────────────────────────────────────────────

    def test_extract_from_video_url_type(self):
        result = {
            "status": "succeeded",
            "content": [{"type": "video_url", "video_url": {"url": "https://cdn.example.com/v.mp4"}}],
        }
        url = extract_video_url(result)
        assert url == "https://cdn.example.com/v.mp4"

    def test_extract_from_direct_url_field(self):
        result = {
            "status": "succeeded",
            "content": [{"url": "https://cdn.example.com/v2.mp4"}],
        }
        url = extract_video_url(result)
        assert url == "https://cdn.example.com/v2.mp4"

    def test_extract_raises_when_no_url(self):
        result = {"status": "succeeded", "content": []}
        with pytest.raises(BytePlusArkError, match="no video URL"):
            extract_video_url(result)


# ===========================================================================
# TestBytePlusArkExceptions
# ===========================================================================

class TestBytePlusArkExceptions:

    def test_auth_failure_is_byteplus_ark_error(self):
        assert issubclass(BytePlusArkAuthFailure, BytePlusArkError)

    def test_quota_exhausted_is_byteplus_ark_error(self):
        assert issubclass(BytePlusArkQuotaExhausted, BytePlusArkError)

    def test_auth_failure_message_mentions_key(self):
        exc = BytePlusArkAuthFailure()
        assert "BYTEPLUS_ARK_API_KEY" in str(exc)

    def test_quota_exhausted_message_mentions_credits(self):
        exc = BytePlusArkQuotaExhausted()
        assert "credits" in str(exc).lower() or "quota" in str(exc).lower()


# ===========================================================================
# TestRoundDuration + TestResolution (helper functions)
# ===========================================================================

class TestHelpers:

    def test_round_duration_5_for_small(self):
        assert _round_duration(3.0) == 5
        assert _round_duration(5.0) == 5
        assert _round_duration(6.0) == 5

    def test_round_duration_8_for_large(self):
        assert _round_duration(6.1) == 8
        assert _round_duration(8.0) == 8
        assert _round_duration(15.0) == 8

    def test_resolution_16x9(self):
        assert _resolution_for_aspect("16:9") == "1280x720"

    def test_resolution_9x16(self):
        assert _resolution_for_aspect("9:16") == "720x1280"

    def test_resolution_1x1(self):
        assert _resolution_for_aspect("1:1") == "960x960"

    def test_resolution_unknown_defaults_to_16x9(self):
        assert _resolution_for_aspect("4:3") == "1280x720"

    def test_valid_durations_are_5_and_8(self):
        assert 5 in _VALID_DURATIONS
        assert 8 in _VALID_DURATIONS


# ===========================================================================
# TestProviderVerifier
# ===========================================================================

class TestProviderVerifier:

    def test_seedance_official_in_env_var_map(self):
        from hypogeum.provider_verifier import _PROVIDER_ENV_VAR
        assert "seedance_official" in _PROVIDER_ENV_VAR
        assert _PROVIDER_ENV_VAR["seedance_official"] == "BYTEPLUS_ARK_API_KEY"

    def test_seedance_official_in_probes(self):
        from hypogeum.provider_verifier import _PROBES
        assert "seedance_official" in _PROBES

    def test_seedance_official_not_in_not_verifiable(self):
        from hypogeum.provider_verifier import _NOT_VERIFIABLE
        assert "seedance_official" not in _NOT_VERIFIABLE

    def test_verify_seedance_official_returns_missing_when_no_key(self, db, monkeypatch):
        from hypogeum.provider_verifier import verify_provider, STATUS_MISSING
        monkeypatch.delenv("BYTEPLUS_ARK_API_KEY", raising=False)
        result = verify_provider("seedance_official", db_path=db)
        assert result["status"] == STATUS_MISSING

    def test_verify_seedance_official_verified_on_200(self, db, monkeypatch):
        from hypogeum.provider_verifier import verify_provider, STATUS_VERIFIED
        monkeypatch.setenv("BYTEPLUS_ARK_API_KEY", "ark-test-key")
        with patch("hypogeum.dormant_providers.byteplus_ark.probe_credential",
                   return_value=(True, None, 120)):
            result = verify_provider("seedance_official", db_path=db)
        assert result["status"] == STATUS_VERIFIED
        assert result["latency_ms"] == 120

    def test_verify_seedance_official_failed_on_probe_failure(self, db, monkeypatch):
        from hypogeum.provider_verifier import verify_provider, STATUS_FAILED
        monkeypatch.setenv("BYTEPLUS_ARK_API_KEY", "ark-bad-key")
        with patch("hypogeum.dormant_providers.byteplus_ark.probe_credential",
                   return_value=(False, "HTTP 401 — invalid API key", 50)):
            result = verify_provider("seedance_official", db_path=db)
        assert result["status"] == STATUS_FAILED
        assert result["error"] is not None

    def test_verify_persists_to_db(self, db, monkeypatch):
        from hypogeum.provider_verifier import verify_provider
        from hypogeum.db import get_connection, get_provider_verification
        monkeypatch.setenv("BYTEPLUS_ARK_API_KEY", "ark-test-key")
        with patch("hypogeum.dormant_providers.byteplus_ark.probe_credential",
                   return_value=(True, None, 80)):
            verify_provider("seedance_official", db_path=db)
        conn = get_connection(db)
        try:
            row = get_provider_verification(conn, "seedance_official")
        finally:
            conn.close()
        assert row is not None
        assert row["status"] == "verified"

    def test_secret_profile_used_if_present(self, db, monkeypatch):
        from hypogeum.provider_verifier import verify_provider, STATUS_VERIFIED
        from hypogeum.credential_manager import create_secret_profile
        monkeypatch.delenv("BYTEPLUS_ARK_API_KEY", raising=False)
        create_secret_profile(
            provider="seedance_official",
            label="byteplus-sp",
            credential_type="api_key",
            credential_data="ark-from-profile",
            db_path=db,
        )
        with patch("hypogeum.dormant_providers.byteplus_ark.probe_credential",
                   return_value=(True, None, 95)):
            result = verify_provider("seedance_official", db_path=db)
        assert result["status"] == STATUS_VERIFIED
        assert result["source"] == "secret_profile"


# ===========================================================================
# TestProviderCatalog
# ===========================================================================

class TestProviderCatalog:

    def test_seedance_official_access_mode_is_direct(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        catalog = get_provider_catalog(db_path=db)
        entry = next((e for e in catalog if e.provider == "seedance_official"), None)
        assert entry is not None
        assert entry.access_mode == "direct"

    def test_seedance_official_display_name_mentions_byteplus(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        catalog = get_provider_catalog(db_path=db)
        entry = next((e for e in catalog if e.provider == "seedance_official"), None)
        assert entry is not None
        assert "BytePlus" in entry.display_name or "Seedance" in entry.display_name

    def test_seedance_official_credential_key_is_byteplus(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        catalog = get_provider_catalog(db_path=db)
        entry = next((e for e in catalog if e.provider == "seedance_official"), None)
        assert entry is not None
        assert entry.credential_key_name == "BYTEPLUS_ARK_API_KEY"

    def test_seedance_official_capability_is_video(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        catalog = get_provider_catalog(db_path=db)
        entry = next((e for e in catalog if e.provider == "seedance_official"), None)
        assert entry is not None
        assert "video" in entry.capabilities

    def test_seedance_official_not_a_placeholder(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        catalog = get_provider_catalog(db_path=db)
        entry = next((e for e in catalog if e.provider == "seedance_official"), None)
        assert entry is not None
        assert entry.access_mode != "direct_placeholder"


# ===========================================================================
# TestConfig
# ===========================================================================

class TestConfig:

    def test_byteplus_ark_api_key_is_optional(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        monkeypatch.delenv("BYTEPLUS_ARK_API_KEY", raising=False)
        from hypogeum.config import Config
        cfg = Config()
        assert cfg.BYTEPLUS_ARK_API_KEY is None

    def test_byteplus_ark_api_key_reads_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        monkeypatch.setenv("BYTEPLUS_ARK_API_KEY", "ark-live-key")
        from hypogeum.config import Config
        cfg = Config()
        assert cfg.BYTEPLUS_ARK_API_KEY == "ark-live-key"

    def test_byteplus_ark_base_url_has_correct_default(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        from hypogeum.config import Config
        cfg = Config()
        assert "bytepluses.com" in cfg.BYTEPLUS_ARK_BASE_URL
        assert "api/v3" in cfg.BYTEPLUS_ARK_BASE_URL

    def test_byteplus_ark_model_default_is_seedance_lite(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        from hypogeum.config import Config
        cfg = Config()
        assert "seedance" in cfg.BYTEPLUS_ARK_MODEL.lower()

    def test_byteplus_ark_base_url_can_be_overridden(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        monkeypatch.setenv("BYTEPLUS_ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
        from hypogeum.config import Config
        cfg = Config()
        assert "cn-beijing" in cfg.BYTEPLUS_ARK_BASE_URL


# ===========================================================================
# TestDashboardRoute
# ===========================================================================

class TestDashboardRoute:

    def test_verify_seedance_official_route_exists(self, client):
        with patch("run_dashboard.verify_provider",
                   return_value={"status": "verified", "latency_ms": 100,
                                 "error": None, "source": "env",
                                 "checked_at": "2026-06-21T10:00:00+00:00",
                                 "provider": "seedance_official"}):
            resp = client.post("/api/providers/verify/seedance_official")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_verify_seedance_official_missing_returns_correct_status(self, client):
        with patch("run_dashboard.verify_provider",
                   return_value={"status": "missing", "latency_ms": None,
                                 "error": "No credential", "source": "env",
                                 "checked_at": "t", "provider": "seedance_official"}):
            resp = client.post("/api/providers/verify/seedance_official")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_providers_page_renders_with_seedance_official(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert resp.status_code == 200
        # seedance_official is in video_provider_pairs section
        assert b"Seedance" in resp.data or b"BytePlus" in resp.data


# ===========================================================================
# TestConnectForm
# ===========================================================================

class TestConnectForm:

    def test_seedance_official_in_content_providers(self):
        from run_dashboard import _CONTENT_PROVIDERS
        ids = [p["id"] for p in _CONTENT_PROVIDERS]
        assert "seedance_official" in ids

    def test_seedance_official_in_content_provider_setup(self):
        from run_dashboard import _CONTENT_PROVIDER_SETUP
        assert "seedance_official" in _CONTENT_PROVIDER_SETUP

    def test_seedance_official_env_var_is_byteplus_key(self):
        from run_dashboard import _CONTENT_PROVIDER_SETUP
        setup = _CONTENT_PROVIDER_SETUP["seedance_official"]
        assert setup["env_var"] == "BYTEPLUS_ARK_API_KEY"

    def test_seedance_official_is_in_valid_providers(self):
        from run_dashboard import _VALID_CONTENT_PROVIDERS
        assert "seedance_official" in _VALID_CONTENT_PROVIDERS

    def test_connect_page_renders(self, client):
        resp = client.get("/providers/content/connect/seedance_official")
        assert resp.status_code == 200
        assert b"BytePlus" in resp.data or b"ARK" in resp.data or b"Seedance" in resp.data


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:
    """No sockets opened during unit tests — probes must be mocked."""

    @pytest.fixture(autouse=True)
    def block_sockets(self):
        _orig = socket.socket

        def _blocked(*args, **kwargs):
            raise RuntimeError("Network access blocked in unit tests")

        socket.socket = _blocked
        yield
        socket.socket = _orig

    def test_probe_credential_does_not_open_socket_when_patched(self):
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(200, {})):
            probe_credential("ark-test-key")

    def test_config_keys_loaded_without_network(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        from hypogeum.config import Config
        cfg = Config()
        assert cfg.BYTEPLUS_ARK_MODEL is not None

    def test_providers_page_no_network(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert resp.status_code == 200
