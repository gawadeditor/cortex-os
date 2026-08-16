"""
tests/test_phase_1j.py — Phase 1J: Official provider activation framework tests

Coverage:
    TestProviderCatalog          — all new providers in catalog with correct access_mode
    TestProviderVerifierMapping  — all new providers in env_var map + probes
    TestConnectForms             — all new provider connect forms accessible
    TestSecretProfileResolution  — Secret Profile used before env var for all providers
    TestVideoReadinessCheck      — video readiness check: credentials probed, no generation
    TestImageComparisonFreepik   — freepik added to image comparison
    TestRouteVideoCompare        — POST /api/providers/compare/video
    TestRouteVideoResults        — GET /api/providers/compare/video/results
    TestDashboardRendering       — /providers page has both comparison sections
    TestComparisonTypeIsolation  — image and video results stored/retrieved separately
    TestNoNetworkCalls           — no sockets opened
    TestNoProductionSideEffects  — no writes to production DB
"""

from __future__ import annotations

import json
import socket
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_dashboard as server
from hypogeum.db import (
    get_connection,
    initialize_schema,
    insert_provider_comparison_run,
    complete_provider_comparison_run,
    get_latest_provider_comparison,
)
from hypogeum.provider_comparison import (
    COMPARISON_TYPE_IMAGE,
    COMPARISON_TYPE_VIDEO,
    DEFAULT_IMAGE_PROVIDERS,
    DEFAULT_VIDEO_PROVIDERS,
    ComparisonRun,
    COMPARISON_PROMPT,
    VIDEO_PROBE_PROMPT,
    get_comparison_result,
    run_video_readiness_check,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "1j_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def client(db):
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db)
    with server.app.test_client() as c:
        yield c


def _seed_run(db, comparison_type=COMPARISON_TYPE_IMAGE, results=None):
    rid = str(uuid.uuid4())
    run_at = "2026-06-21T10:00:00+00:00"
    done_at = "2026-06-21T10:01:00+00:00"
    conn = get_connection(db)
    try:
        insert_provider_comparison_run(conn, rid, run_at, "test prompt", comparison_type)
        conn.commit()
        complete_provider_comparison_run(conn, rid, results or [], done_at)
        conn.commit()
    finally:
        conn.close()
    return rid


# ===========================================================================
# TestProviderCatalog
# ===========================================================================

class TestProviderCatalog:

    def test_runway_official_is_direct(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        cat = {e.provider: e for e in get_provider_catalog(db_path=db)}
        assert cat["runway_official"].access_mode == "direct"

    def test_kling_official_is_direct(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        cat = {e.provider: e for e in get_provider_catalog(db_path=db)}
        assert cat["kling_official"].access_mode == "direct"

    def test_veo_official_is_direct(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        cat = {e.provider: e for e in get_provider_catalog(db_path=db)}
        assert cat["veo_official"].access_mode == "direct"

    def test_no_direct_placeholders_remain(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        placeholders = [e for e in get_provider_catalog(db_path=db)
                        if e.access_mode == "direct_placeholder"]
        assert placeholders == [], f"Unexpected placeholders: {[e.provider for e in placeholders]}"

    def test_runway_official_credential_key(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        cat = {e.provider: e for e in get_provider_catalog(db_path=db)}
        assert cat["runway_official"].credential_key_name == "RUNWAY_API_KEY"

    def test_kling_official_credential_key(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        cat = {e.provider: e for e in get_provider_catalog(db_path=db)}
        assert cat["kling_official"].credential_key_name == "KLING_API_KEY"

    def test_veo_official_credential_key(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        cat = {e.provider: e for e in get_provider_catalog(db_path=db)}
        assert cat["veo_official"].credential_key_name == "GOOGLE_API_KEY"

    def test_veo_official_display_name_mentions_google(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        cat = {e.provider: e for e in get_provider_catalog(db_path=db)}
        assert "Google" in cat["veo_official"].display_name or "Veo" in cat["veo_official"].display_name

    def test_facebook_in_catalog(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        cat = {e.provider: e for e in get_provider_catalog(db_path=db)}
        assert "facebook" in cat
        assert "publishing" in cat["facebook"].capabilities

    def test_instagram_in_catalog(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        cat = {e.provider: e for e in get_provider_catalog(db_path=db)}
        assert "instagram" in cat
        assert "publishing" in cat["instagram"].capabilities

    def test_tiktok_in_catalog(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        cat = {e.provider: e for e in get_provider_catalog(db_path=db)}
        assert "tiktok" in cat
        assert "publishing" in cat["tiktok"].capabilities

    def test_facebook_credential_key_is_meta_token(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        cat = {e.provider: e for e in get_provider_catalog(db_path=db)}
        assert cat["facebook"].credential_key_name == "META_ACCESS_TOKEN"

    def test_tiktok_credential_key_is_tiktok_token(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        cat = {e.provider: e for e in get_provider_catalog(db_path=db)}
        assert cat["tiktok"].credential_key_name == "TIKTOK_ACCESS_TOKEN"

    def test_freepik_still_in_catalog(self, db):
        from hypogeum.dashboard_api import get_provider_catalog
        cat = {e.provider: e for e in get_provider_catalog(db_path=db)}
        assert "freepik" in cat
        assert cat["freepik"].access_mode == "direct"


# ===========================================================================
# TestProviderVerifierMapping
# ===========================================================================

class TestProviderVerifierMapping:

    def test_runway_official_in_env_var_map(self):
        from hypogeum.provider_verifier import _PROVIDER_ENV_VAR
        assert _PROVIDER_ENV_VAR["runway_official"] == "RUNWAY_API_KEY"

    def test_kling_official_in_env_var_map(self):
        from hypogeum.provider_verifier import _PROVIDER_ENV_VAR
        assert _PROVIDER_ENV_VAR["kling_official"] == "KLING_API_KEY"

    def test_veo_official_in_env_var_map(self):
        from hypogeum.provider_verifier import _PROVIDER_ENV_VAR
        assert _PROVIDER_ENV_VAR["veo_official"] == "GOOGLE_API_KEY"

    def test_facebook_in_env_var_map(self):
        from hypogeum.provider_verifier import _PROVIDER_ENV_VAR
        assert _PROVIDER_ENV_VAR["facebook"] == "META_ACCESS_TOKEN"

    def test_instagram_in_env_var_map(self):
        from hypogeum.provider_verifier import _PROVIDER_ENV_VAR
        assert _PROVIDER_ENV_VAR["instagram"] == "META_ACCESS_TOKEN"

    def test_tiktok_in_env_var_map(self):
        from hypogeum.provider_verifier import _PROVIDER_ENV_VAR
        assert _PROVIDER_ENV_VAR["tiktok"] == "TIKTOK_ACCESS_TOKEN"

    def test_freepik_has_probe(self):
        from hypogeum.provider_verifier import _PROBES
        assert "freepik" in _PROBES
        # Must not be None
        assert _PROBES["freepik"] is not None

    def test_freepik_not_in_not_verifiable(self):
        from hypogeum.provider_verifier import _NOT_VERIFIABLE
        assert "freepik" not in _NOT_VERIFIABLE

    def test_runway_official_has_probe(self):
        from hypogeum.provider_verifier import _PROBES
        assert "runway_official" in _PROBES

    def test_kling_official_has_probe(self):
        from hypogeum.provider_verifier import _PROBES
        assert "kling_official" in _PROBES

    def test_veo_official_has_probe(self):
        from hypogeum.provider_verifier import _PROBES
        assert "veo_official" in _PROBES

    def test_facebook_has_probe(self):
        from hypogeum.provider_verifier import _PROBES
        assert "facebook" in _PROBES

    def test_instagram_has_probe(self):
        from hypogeum.provider_verifier import _PROBES
        assert "instagram" in _PROBES

    def test_tiktok_has_probe(self):
        from hypogeum.provider_verifier import _PROBES
        assert "tiktok" in _PROBES

    def test_verify_runway_official_missing_when_no_key(self, db, monkeypatch):
        from hypogeum.provider_verifier import verify_provider, STATUS_MISSING
        monkeypatch.delenv("RUNWAY_API_KEY", raising=False)
        result = verify_provider("runway_official", db_path=db)
        assert result["status"] == STATUS_MISSING

    def test_verify_kling_official_verified_on_probe_success(self, db, monkeypatch):
        from hypogeum.provider_verifier import verify_provider, STATUS_VERIFIED
        monkeypatch.setenv("KLING_API_KEY", "kid:ksecret")
        with patch("hypogeum.dormant_providers.kling_official.probe_credential",
                   return_value=(True, None, 200)):
            result = verify_provider("kling_official", db_path=db)
        assert result["status"] == STATUS_VERIFIED

    def test_verify_veo_official_verified_on_probe_success(self, db, monkeypatch):
        from hypogeum.provider_verifier import verify_provider, STATUS_VERIFIED
        monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")
        with patch("hypogeum.dormant_providers.veo_official.probe_credential",
                   return_value=(True, None, 150)):
            result = verify_provider("veo_official", db_path=db)
        assert result["status"] == STATUS_VERIFIED

    def test_verify_facebook_uses_meta_token(self, db, monkeypatch):
        from hypogeum.provider_verifier import verify_provider, _PROBES
        monkeypatch.setenv("META_ACCESS_TOKEN", "EAA-test")
        original = _PROBES["facebook"]
        try:
            _PROBES["facebook"] = lambda k: (True, None, 80)
            result = verify_provider("facebook", db_path=db)
        finally:
            _PROBES["facebook"] = original
        assert result["status"] == "verified"

    def test_verify_instagram_same_probe_as_facebook(self, db, monkeypatch):
        from hypogeum.provider_verifier import verify_provider, _PROBES
        monkeypatch.setenv("META_ACCESS_TOKEN", "EAA-test")
        original = _PROBES["instagram"]
        try:
            _PROBES["instagram"] = lambda k: (True, None, 80)
            result = verify_provider("instagram", db_path=db)
        finally:
            _PROBES["instagram"] = original
        assert result["status"] == "verified"


# ===========================================================================
# TestConnectForms
# ===========================================================================

class TestConnectForms:

    def test_runway_official_in_valid_providers(self):
        assert "runway_official" in server._VALID_CONTENT_PROVIDERS

    def test_kling_official_in_valid_providers(self):
        assert "kling_official" in server._VALID_CONTENT_PROVIDERS

    def test_veo_official_in_valid_providers(self):
        assert "veo_official" in server._VALID_CONTENT_PROVIDERS

    def test_facebook_in_valid_providers(self):
        assert "facebook" in server._VALID_CONTENT_PROVIDERS

    def test_instagram_in_valid_providers(self):
        assert "instagram" in server._VALID_CONTENT_PROVIDERS

    def test_tiktok_in_valid_providers(self):
        assert "tiktok" in server._VALID_CONTENT_PROVIDERS

    def test_runway_setup_env_var(self):
        assert server._CONTENT_PROVIDER_SETUP["runway_official"]["env_var"] == "RUNWAY_API_KEY"

    def test_kling_setup_env_var(self):
        assert server._CONTENT_PROVIDER_SETUP["kling_official"]["env_var"] == "KLING_API_KEY"

    def test_veo_setup_env_var(self):
        assert server._CONTENT_PROVIDER_SETUP["veo_official"]["env_var"] == "GOOGLE_API_KEY"

    def test_facebook_setup_env_var(self):
        assert server._CONTENT_PROVIDER_SETUP["facebook"]["env_var"] == "META_ACCESS_TOKEN"

    def test_instagram_setup_env_var(self):
        assert server._CONTENT_PROVIDER_SETUP["instagram"]["env_var"] == "META_ACCESS_TOKEN"

    def test_tiktok_setup_env_var(self):
        assert server._CONTENT_PROVIDER_SETUP["tiktok"]["env_var"] == "TIKTOK_ACCESS_TOKEN"

    def test_runway_connect_page_renders(self, client):
        resp = client.get("/providers/content/connect/runway_official")
        assert resp.status_code == 200
        assert b"Runway" in resp.data

    def test_kling_connect_page_renders(self, client):
        resp = client.get("/providers/content/connect/kling_official")
        assert resp.status_code == 200
        assert b"Kling" in resp.data

    def test_veo_connect_page_renders(self, client):
        resp = client.get("/providers/content/connect/veo_official")
        assert resp.status_code == 200
        assert b"Google" in resp.data or b"Veo" in resp.data

    def test_facebook_connect_page_renders(self, client):
        resp = client.get("/providers/content/connect/facebook")
        assert resp.status_code == 200
        assert b"Facebook" in resp.data or b"Meta" in resp.data

    def test_instagram_connect_page_renders(self, client):
        resp = client.get("/providers/content/connect/instagram")
        assert resp.status_code == 200
        assert b"Instagram" in resp.data or b"Meta" in resp.data

    def test_tiktok_connect_page_renders(self, client):
        resp = client.get("/providers/content/connect/tiktok")
        assert resp.status_code == 200
        assert b"TikTok" in resp.data

    def test_unknown_provider_returns_404(self, client):
        resp = client.get("/providers/content/connect/fakeprovider_xyz")
        assert resp.status_code == 404

    def test_kling_instructions_mention_colon_format(self, client):
        resp = client.get("/providers/content/connect/kling_official")
        assert b":" in resp.data   # key_id:key_secret format
        assert b"KLING_API_KEY" in resp.data

    def test_verify_route_works_for_runway(self, client):
        with patch("run_dashboard.verify_provider",
                   return_value={"status": "missing", "latency_ms": None,
                                 "error": "no key", "source": "env",
                                 "checked_at": "t", "provider": "runway_official"}):
            resp = client.post("/api/providers/verify/runway_official")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_verify_route_works_for_facebook(self, client):
        with patch("run_dashboard.verify_provider",
                   return_value={"status": "missing", "latency_ms": None,
                                 "error": "no key", "source": "env",
                                 "checked_at": "t", "provider": "facebook"}):
            resp = client.post("/api/providers/verify/facebook")
        assert resp.status_code == 200


# ===========================================================================
# TestSecretProfileResolution
# ===========================================================================

class TestSecretProfileResolution:

    def test_runway_secret_profile_beats_env(self, db, monkeypatch):
        from hypogeum.credential_manager import create_secret_profile
        from hypogeum.provider_comparison import _resolve_credential
        monkeypatch.setenv("RUNWAY_API_KEY", "env-key")
        create_secret_profile(
            provider="runway_official", label="runway-sp",
            credential_type="api_key", credential_data="sp-key",
            db_path=db,
        )
        cred, source = _resolve_credential("runway_official", db)
        assert cred == "sp-key"
        assert source == "secret_profile"

    def test_kling_env_fallback_when_no_profile(self, db, monkeypatch):
        from hypogeum.provider_comparison import _resolve_credential
        monkeypatch.setenv("KLING_API_KEY", "kling-env-key")
        cred, source = _resolve_credential("kling_official", db)
        assert cred == "kling-env-key"
        assert source == "env"

    def test_missing_returns_none(self, db, monkeypatch):
        from hypogeum.provider_comparison import _resolve_credential
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        cred, source = _resolve_credential("veo_official", db)
        assert cred is None

    def test_facebook_secret_profile_beats_env(self, db, monkeypatch):
        from hypogeum.credential_manager import create_secret_profile
        from hypogeum.provider_comparison import _resolve_credential
        monkeypatch.setenv("META_ACCESS_TOKEN", "env-meta-token")
        create_secret_profile(
            provider="facebook", label="fb-sp",
            credential_type="api_key", credential_data="sp-meta-token",
            db_path=db,
        )
        cred, source = _resolve_credential("facebook", db)
        assert cred == "sp-meta-token"
        assert source == "secret_profile"


# ===========================================================================
# TestVideoReadinessCheck
# ===========================================================================

class TestVideoReadinessCheck:

    def _mock_probe(self, success=True, latency=100):
        return patch("hypogeum.provider_verifier._PROBES",
                     {p: (lambda k, s=success, l=latency: (s, None if s else "failed", l))
                      for p in DEFAULT_VIDEO_PROVIDERS})

    def test_returns_comparison_run(self, db, monkeypatch):
        monkeypatch.setenv("BYTEPLUS_ARK_API_KEY", "ark-test")
        monkeypatch.setenv("RUNWAY_API_KEY",       "rw-test")
        monkeypatch.setenv("KLING_API_KEY",        "kid:ksec")
        monkeypatch.setenv("GOOGLE_API_KEY",       "AIza-test")
        monkeypatch.setenv("MUAPI_API_KEY",        "mu-test")
        with patch("hypogeum.provider_comparison._PROBES",
                   {p: (lambda k: (True, None, 100)) for p in DEFAULT_VIDEO_PROVIDERS},
                   create=True):
            with patch("hypogeum.provider_verifier._PROBES",
                       {p: (lambda k: (True, None, 100)) for p in DEFAULT_VIDEO_PROVIDERS}):
                run = run_video_readiness_check(db_path=db)
        assert isinstance(run, ComparisonRun)
        assert run.status == "completed"

    def test_comparison_type_is_video(self, db, monkeypatch):
        monkeypatch.setenv("BYTEPLUS_ARK_API_KEY", "ark-test")
        monkeypatch.setenv("MUAPI_API_KEY", "mu-test")
        with patch("hypogeum.provider_verifier._PROBES",
                   {p: (lambda k: (True, None, 50)) for p in DEFAULT_VIDEO_PROVIDERS}):
            run = run_video_readiness_check(db_path=db,
                                             providers=["seedance_official", "muapi"])
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT comparison_type FROM provider_comparison_runs WHERE run_id=?",
                (run.run_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["comparison_type"] == COMPARISON_TYPE_VIDEO

    def test_skipped_when_no_credential(self, db, monkeypatch):
        monkeypatch.delenv("RUNWAY_API_KEY", raising=False)
        run = run_video_readiness_check(db_path=db, providers=["runway_official"])
        assert run.results[0]["status"] == "skipped_no_key"

    def test_ready_when_probe_succeeds(self, db, monkeypatch):
        monkeypatch.setenv("RUNWAY_API_KEY", "rw-test")
        with patch("hypogeum.provider_verifier._PROBES",
                   {"runway_official": lambda k: (True, None, 120)}):
            run = run_video_readiness_check(db_path=db, providers=["runway_official"])
        assert run.results[0]["status"] == "ready"
        assert run.results[0]["latency_ms"] == 120

    def test_auth_failure_when_probe_fails(self, db, monkeypatch):
        monkeypatch.setenv("RUNWAY_API_KEY", "bad-key")
        with patch("hypogeum.provider_verifier._PROBES",
                   {"runway_official": lambda k: (False, "HTTP 401", 50)}):
            run = run_video_readiness_check(db_path=db, providers=["runway_official"])
        assert run.results[0]["status"] == "auth_failure"
        assert "401" in (run.results[0]["error"] or "")

    def test_output_path_is_null_for_all_providers(self, db, monkeypatch):
        monkeypatch.setenv("MUAPI_API_KEY", "mu-test")
        with patch("hypogeum.provider_verifier._PROBES",
                   {"muapi": lambda k: (True, None, 80)}):
            run = run_video_readiness_check(db_path=db, providers=["muapi"])
        assert run.results[0]["output_path"] is None

    def test_no_video_file_created(self, db, monkeypatch, tmp_path):
        monkeypatch.setenv("BYTEPLUS_ARK_API_KEY", "ark-test")
        with patch("hypogeum.provider_verifier._PROBES",
                   {"seedance_official": lambda k: (True, None, 100)}):
            run = run_video_readiness_check(db_path=db, providers=["seedance_official"])
        # No files should be created under provider_comparisons/{run_id}/
        out = Path(".hypogeum-assets") / "provider_comparisons" / run.run_id
        assert not out.exists() or list(out.rglob("*.mp4")) == []

    def test_results_persisted_in_db(self, db, monkeypatch):
        monkeypatch.setenv("MUAPI_API_KEY", "mu-test")
        with patch("hypogeum.provider_verifier._PROBES",
                   {"muapi": lambda k: (True, None, 80)}):
            run = run_video_readiness_check(db_path=db, providers=["muapi"])
        stored = get_comparison_result(db, comparison_type=COMPARISON_TYPE_VIDEO)
        assert stored is not None
        assert stored["run_id"] == run.run_id


# ===========================================================================
# TestImageComparisonFreepik
# ===========================================================================

class TestImageComparisonFreepik:

    def test_freepik_in_default_image_providers(self):
        assert "freepik" in DEFAULT_IMAGE_PROVIDERS

    def test_freepik_env_var_mapped(self):
        from hypogeum.provider_comparison import _PROVIDER_ENV_VAR
        assert _PROVIDER_ENV_VAR["freepik"] == "FREEPIK_API_KEY"

    def test_freepik_has_estimated_cost(self):
        from hypogeum.provider_comparison import _PROVIDER_EST_COST
        assert _PROVIDER_EST_COST.get("freepik") is not None

    def test_freepik_skipped_when_no_key(self, db, monkeypatch):
        from hypogeum.provider_comparison import run_provider_comparison
        from unittest.mock import patch
        monkeypatch.delenv("FREEPIK_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        _FAKE = b"JFIF" + b"\x00" * 2048

        def _fake_openrouter(deliverable_id, image_prompt, aspect_ratio, config, local_path, log):
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(_FAKE)
            return "openai/gpt-5-image-mini"

        with patch("hypogeum.provider_comparison._generate_thumbnail_openai",
                   side_effect=Exception("no key")):
            with patch("hypogeum.provider_comparison._generate_thumbnail_openrouter",
                       side_effect=_fake_openrouter):
                with patch("hypogeum.provider_comparison._attempt_thumbnail_generation",
                           side_effect=Exception("no key")):
                    with patch("hypogeum.provider_comparison._normalize_thumbnail"):
                        with patch("hypogeum.provider_comparison._apply_wordmark_overlay"):
                            run = run_provider_comparison(db_path=db,
                                                          providers=["openrouter", "freepik"])
        statuses = {r["provider"]: r["status"] for r in run.results}
        assert statuses["freepik"] == "skipped_no_key"
        assert statuses["openrouter"] == "success"


# ===========================================================================
# TestRouteVideoCompare
# ===========================================================================

class TestRouteVideoCompare:

    def _mock_video_run(self, results=None):
        fake = ComparisonRun(
            run_id="vid-run-1", run_at="2026-06-21T10:00:00+00:00",
            prompt=VIDEO_PROBE_PROMPT,
            results=results or [{"provider": "muapi", "status": "ready",
                                  "model": "veo3", "latency_ms": 80,
                                  "estimated_cost_usd": 0.09,
                                  "output_path": None, "error": None,
                                  "credential_source": "env"}],
            status="completed", completed_at="2026-06-21T10:00:05+00:00",
        )
        return patch("run_dashboard.run_video_readiness_check", return_value=fake)

    def test_returns_200(self, client):
        with self._mock_video_run():
            resp = client.post("/api/providers/compare/video",
                               content_type="application/json", data=json.dumps({}))
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_comparison_type_is_video(self, client):
        with self._mock_video_run():
            resp = client.post("/api/providers/compare/video",
                               content_type="application/json", data=json.dumps({}))
        assert resp.get_json()["comparison_type"] == COMPARISON_TYPE_VIDEO

    def test_results_in_response(self, client):
        with self._mock_video_run():
            resp = client.post("/api/providers/compare/video",
                               content_type="application/json", data=json.dumps({}))
        results = resp.get_json()["results"]
        assert len(results) >= 1
        assert results[0]["provider"] == "muapi"

    def test_custom_providers_forwarded(self, client):
        captured = {}
        def _fake(db_path=None, providers=None):
            captured["providers"] = providers
            return ComparisonRun(run_id="x", run_at="t", prompt="p",
                                 results=[], status="completed", completed_at="t")
        with patch("run_dashboard.run_video_readiness_check", side_effect=_fake):
            client.post("/api/providers/compare/video",
                        content_type="application/json",
                        data=json.dumps({"providers": ["muapi", "seedance_official"]}))
        assert captured["providers"] == ["muapi", "seedance_official"]

    def test_invalid_providers_filtered(self, client):
        captured = {}
        def _fake(db_path=None, providers=None):
            captured["providers"] = providers
            return ComparisonRun(run_id="x", run_at="t", prompt="p",
                                 results=[], status="completed", completed_at="t")
        with patch("run_dashboard.run_video_readiness_check", side_effect=_fake):
            client.post("/api/providers/compare/video",
                        content_type="application/json",
                        data=json.dumps({"providers": ["muapi", "not_real"]}))
        assert "not_real" not in captured.get("providers", [])

    def test_all_invalid_returns_400(self, client):
        resp = client.post("/api/providers/compare/video",
                           content_type="application/json",
                           data=json.dumps({"providers": ["totally_fake"]}))
        assert resp.status_code == 400

    def test_error_returns_500(self, client):
        with patch("run_dashboard.run_video_readiness_check",
                   side_effect=RuntimeError("probe failure")):
            resp = client.post("/api/providers/compare/video",
                               content_type="application/json", data=json.dumps({}))
        assert resp.status_code == 500
        assert resp.get_json()["ok"] is False


# ===========================================================================
# TestRouteVideoResults
# ===========================================================================

class TestRouteVideoResults:

    def test_returns_none_when_no_run(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/api/providers/compare/video/results")
        assert resp.status_code == 200
        assert resp.get_json()["run"] is None

    def test_returns_latest_video_run(self, client):
        fake = {"run_id": "vid-1", "comparison_type": "video",
                "results": [{"provider": "muapi", "status": "ready"}]}
        with patch("run_dashboard.get_comparison_result", return_value=fake):
            resp = client.get("/api/providers/compare/video/results")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["run"]["run_id"] == "vid-1"


# ===========================================================================
# TestComparisonTypeIsolation
# ===========================================================================

class TestComparisonTypeIsolation:

    def test_image_and_video_stored_separately(self, db):
        # Store one image run and one video run
        rid_img = str(uuid.uuid4())
        rid_vid = str(uuid.uuid4())
        conn = get_connection(db)
        try:
            insert_provider_comparison_run(conn, rid_img, "2026-06-21T10:00:00+00:00",
                                           "image prompt", COMPARISON_TYPE_IMAGE)
            conn.commit()
            complete_provider_comparison_run(conn, rid_img,
                                             [{"provider": "openai", "status": "success"}],
                                             "2026-06-21T10:01:00+00:00")
            conn.commit()
            insert_provider_comparison_run(conn, rid_vid, "2026-06-21T10:02:00+00:00",
                                           "video prompt", COMPARISON_TYPE_VIDEO)
            conn.commit()
            complete_provider_comparison_run(conn, rid_vid,
                                             [{"provider": "muapi", "status": "ready"}],
                                             "2026-06-21T10:02:10+00:00")
            conn.commit()
            latest_img = get_latest_provider_comparison(conn, COMPARISON_TYPE_IMAGE)
            latest_vid = get_latest_provider_comparison(conn, COMPARISON_TYPE_VIDEO)
        finally:
            conn.close()

        assert latest_img["run_id"] == rid_img
        assert latest_vid["run_id"] == rid_vid
        assert latest_img["results"][0]["provider"] == "openai"
        assert latest_vid["results"][0]["provider"] == "muapi"

    def test_get_comparison_result_type_filter(self, db):
        _seed_run(db, COMPARISON_TYPE_IMAGE,
                  [{"provider": "openai", "status": "success"}])
        _seed_run(db, COMPARISON_TYPE_VIDEO,
                  [{"provider": "muapi", "status": "ready"}])
        img_result = get_comparison_result(db, COMPARISON_TYPE_IMAGE)
        vid_result = get_comparison_result(db, COMPARISON_TYPE_VIDEO)
        assert img_result["results"][0]["provider"] == "openai"
        assert vid_result["results"][0]["provider"] == "muapi"

    def test_image_comparison_not_affected_by_video_runs(self, db):
        # Only a video run — image should return None
        _seed_run(db, COMPARISON_TYPE_VIDEO, [{"provider": "muapi"}])
        result = get_comparison_result(db, COMPARISON_TYPE_IMAGE)
        assert result is None


# ===========================================================================
# TestDashboardRendering
# ===========================================================================

class TestDashboardRendering:

    def test_providers_page_has_image_comparison_button(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert b"Run Image Comparison" in resp.data

    def test_providers_page_has_video_readiness_button(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert b"Run Video Readiness" in resp.data or b"Video Readiness" in resp.data

    def test_providers_page_has_video_grid(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert b"vid-grid" in resp.data

    def test_providers_page_shows_no_video_run_message(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert b"No readiness check" in resp.data

    def test_providers_page_runway_official_visible(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert b"Runway" in resp.data

    def test_providers_page_kling_official_visible(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert b"Kling" in resp.data

    def test_providers_page_veo_google_visible(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert b"Veo" in resp.data

    def test_providers_page_no_placeholder_badges(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        # "Official API — not yet implemented" badge must not appear for promoted providers
        body = resp.data.decode()
        placeholder_count = body.count("Official API — not yet implemented")
        assert placeholder_count == 0


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

    def test_providers_page_no_network(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert resp.status_code == 200

    def test_db_comparison_type_no_network(self, db):
        conn = get_connection(db)
        try:
            insert_provider_comparison_run(conn, str(uuid.uuid4()),
                                           "2026-06-21T00:00:00+00:00",
                                           "test", COMPARISON_TYPE_VIDEO)
            conn.commit()
        finally:
            conn.close()

    def test_kling_jwt_generation_no_network(self):
        from hypogeum.dormant_providers.kling_official import generate_jwt
        token = generate_jwt("kid-test", "ksec-test")
        assert "." in token
        assert len(token.split(".")) == 3


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:

    def test_video_check_writes_only_to_tmp_db(self, db, monkeypatch):
        monkeypatch.setenv("MUAPI_API_KEY", "mu-test")
        prod_db = Path(".hypogeum-assets") / "hypogeum.db"
        mtime_before = prod_db.stat().st_mtime if prod_db.exists() else None
        with patch("hypogeum.provider_verifier._PROBES",
                   {"muapi": lambda k: (True, None, 50)}):
            run_video_readiness_check(db_path=db, providers=["muapi"])
        mtime_after = prod_db.stat().st_mtime if prod_db.exists() else None
        assert mtime_before == mtime_after

    def test_no_episode_rows_written(self, db, monkeypatch):
        monkeypatch.setenv("MUAPI_API_KEY", "mu-test")
        with patch("hypogeum.provider_verifier._PROBES",
                   {"muapi": lambda k: (True, None, 50)}):
            run_video_readiness_check(db_path=db, providers=["muapi"])
        conn = get_connection(db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        finally:
            conn.close()
        assert count == 0
