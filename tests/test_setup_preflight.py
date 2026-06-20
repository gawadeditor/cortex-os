"""
tests/test_setup_preflight.py — Factory Setup / Production Readiness Preflight

Coverage:
    Missing env var appears as blocker in get_setup_preflight_status
    All required env vars present → ready (no env/ffmpeg/ois blockers from those checks)
    Budget ceiling missing → blocker
    Budget ceiling below low estimate → blocker
    Budget ceiling above high estimate → info, no blocker
    OIS blocking signal → appears as blocker
    GET /setup returns 200
    GET /api/setup/preflight returns JSON with ready, blockers, env_vars
    GET /create disabled (button attr) when setup blockers exist
    GET /providers/content/connect/seedance returns 200 (not 404)
    GET /providers/content/connect/freepik returns 200 (not 404)
    GET /providers/content/connect/openrouter returns 200 (not 404)
    GET /providers/content/connect/unknown_xyz returns 404
    POST /providers/content/connect/elevenlabs/save saves credential
    GET /providers — seedance/freepik links do NOT point to /platforms/connect/
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import initialize_schema
from hypogeum.setup_preflight import (
    get_setup_preflight_status,
    _REQUIRED_ENV_VARS,
    _ESTIMATED_COST_LOW,
    _ESTIMATED_COST_HIGH,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "setup_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def client(db):
    import run_dashboard as server
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db)
    with server.app.test_client() as c:
        yield c


@pytest.fixture()
def all_env(monkeypatch):
    """Set all required env vars to non-empty values."""
    for var in _REQUIRED_ENV_VARS:
        monkeypatch.setenv(var, f"fake-{var}-value")
    return None


# ---------------------------------------------------------------------------
# Unit tests — get_setup_preflight_status
# ---------------------------------------------------------------------------

class TestSetupPreflightUnit:

    def test_missing_single_env_var_is_blocker(self, db, monkeypatch):
        for var in _REQUIRED_ENV_VARS:
            monkeypatch.setenv(var, f"fake-{var}")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "")

        result = get_setup_preflight_status(db_path=db)

        blocker_names = [b["name"] for b in result["blockers"]]
        assert "OPENROUTER_API_KEY" in blocker_names
        assert result["env_vars"]["OPENROUTER_API_KEY"] is False

    def test_all_env_vars_present_not_env_blocker(self, db, all_env):
        result = get_setup_preflight_status(db_path=db)
        env_blockers = [b for b in result["blockers"] if b["name"] in _REQUIRED_ENV_VARS]
        assert env_blockers == [], f"Unexpected env blockers: {env_blockers}"

    def test_env_vars_dict_contains_all_required(self, db, all_env):
        result = get_setup_preflight_status(db_path=db)
        for var in _REQUIRED_ENV_VARS:
            assert var in result["env_vars"]

    def test_budget_ceiling_missing_is_blocker(self, db, monkeypatch):
        monkeypatch.setenv("FINANCE_BUDGET_CEILING_USD", "0")
        result = get_setup_preflight_status(db_path=db)
        blocker_names = [b["name"] for b in result["blockers"]]
        assert "budget_ceiling" in blocker_names

    def test_budget_ceiling_below_low_estimate_is_blocker(self, db, monkeypatch):
        below = str(_ESTIMATED_COST_LOW - 5.0)
        monkeypatch.setenv("FINANCE_BUDGET_CEILING_USD", below)
        result = get_setup_preflight_status(db_path=db)
        blocker_names = [b["name"] for b in result["blockers"]]
        assert "cost_vs_ceiling" in blocker_names

    def test_budget_ceiling_above_high_estimate_no_blocker(self, db, monkeypatch):
        above = str(_ESTIMATED_COST_HIGH + 10.0)
        monkeypatch.setenv("FINANCE_BUDGET_CEILING_USD", above)
        result = get_setup_preflight_status(db_path=db)
        blocker_names = [b["name"] for b in result["blockers"]]
        assert "cost_vs_ceiling" not in blocker_names
        assert "budget_ceiling" not in blocker_names

    def test_budget_ceiling_value_in_result(self, db, monkeypatch):
        monkeypatch.setenv("FINANCE_BUDGET_CEILING_USD", "40.0")
        result = get_setup_preflight_status(db_path=db)
        assert result["budget_ceiling_usd"] == pytest.approx(40.0)

    def test_ois_blocking_signal_appears_as_blocker(self, db, monkeypatch):
        from hypogeum.ois import emit_signal
        emit_signal(
            signal_type="budget_exhaustion",
            severity="CRITICAL",
            message="Test blocking signal",
            channel_id=None,
            source_system="test",
            blocking=True,
            db_path=db,
        )
        result = get_setup_preflight_status(db_path=db)
        blocker_names = [b["name"] for b in result["blockers"]]
        assert any("ois:" in n for n in blocker_names), (
            f"Expected OIS blocker, got: {blocker_names}"
        )

    def test_ois_blocking_signal_in_active_signals(self, db, monkeypatch):
        from hypogeum.ois import emit_signal
        emit_signal(
            signal_type="emergency_halt",
            severity="CRITICAL",
            message="Halt for test",
            channel_id=None,
            source_system="test",
            blocking=True,
            db_path=db,
        )
        result = get_setup_preflight_status(db_path=db)
        types = [s["signal_type"] for s in result["active_signals"]]
        assert "emergency_halt" in types

    def test_no_ois_signals_empty_active_signals(self, db):
        result = get_setup_preflight_status(db_path=db)
        assert result["active_signals"] == []

    def test_estimated_cost_range_present(self, db):
        result = get_setup_preflight_status(db_path=db)
        assert result["estimated_cost_low"] > 0
        assert result["estimated_cost_high"] >= result["estimated_cost_low"]

    def test_checked_at_present(self, db):
        result = get_setup_preflight_status(db_path=db)
        assert result["checked_at"]

    def test_checks_list_present(self, db):
        result = get_setup_preflight_status(db_path=db)
        assert isinstance(result["checks"], list)
        assert len(result["checks"]) > 0

    def test_result_never_raises(self, db):
        result = get_setup_preflight_status(db_path=db)
        assert "ready" in result


# ---------------------------------------------------------------------------
# Route tests — /setup and /api/setup/preflight
# ---------------------------------------------------------------------------

class TestSetupPage:

    def test_setup_page_returns_200(self, client):
        resp = client.get("/setup")
        assert resp.status_code == 200

    def test_setup_page_is_html(self, client):
        resp = client.get("/setup")
        html = resp.data.decode()
        assert "<!DOCTYPE html>" in html or "<!doctype html>" in html.lower()

    def test_setup_page_shows_factory_setup_title(self, client):
        resp = client.get("/setup")
        assert b"Factory Setup" in resp.data or b"Setup" in resp.data

    def test_api_setup_preflight_returns_200(self, client):
        resp = client.get("/api/setup/preflight")
        assert resp.status_code == 200

    def test_api_setup_preflight_returns_json(self, client):
        resp = client.get("/api/setup/preflight")
        data = resp.get_json()
        assert data is not None
        assert "ready" in data
        assert "blockers" in data
        assert "env_vars" in data

    def test_setup_page_shows_blockers_when_env_missing(self, client, monkeypatch):
        for var in _REQUIRED_ENV_VARS:
            monkeypatch.setenv(var, "")
        resp = client.get("/setup")
        html = resp.data.decode()
        assert "BLOCKER" in html or "blocker" in html.lower() or any(
            v in html for v in _REQUIRED_ENV_VARS
        )

    def test_setup_page_shows_budget_ceiling_section(self, client):
        resp = client.get("/setup")
        html = resp.data.decode()
        assert "FINANCE_BUDGET_CEILING_USD" in html or "Budget" in html

    def test_setup_page_shows_ois_section(self, client):
        resp = client.get("/setup")
        html = resp.data.decode()
        assert "OIS" in html or "Blocking" in html or "signal" in html.lower()


# ---------------------------------------------------------------------------
# Route tests — /create disabled when blockers
# ---------------------------------------------------------------------------

class TestCreatePagePreflight:

    def test_create_page_returns_200(self, client):
        resp = client.get("/create")
        assert resp.status_code == 200

    def test_create_page_button_disabled_when_env_missing(self, client, monkeypatch):
        for var in _REQUIRED_ENV_VARS:
            monkeypatch.setenv(var, "")
        resp = client.get("/create")
        html = resp.data.decode()
        # The button must have disabled attribute
        assert 'disabled' in html

    def test_create_page_shows_setup_link_when_blocked(self, client, monkeypatch):
        for var in _REQUIRED_ENV_VARS:
            monkeypatch.setenv(var, "")
        resp = client.get("/create")
        html = resp.data.decode()
        assert "/setup" in html

    def test_create_page_shows_blocker_name_when_env_missing(self, client, monkeypatch):
        for var in _REQUIRED_ENV_VARS:
            monkeypatch.setenv(var, "")
        resp = client.get("/create")
        html = resp.data.decode()
        # At least one required var name should be visible
        assert any(var in html for var in _REQUIRED_ENV_VARS)


# ---------------------------------------------------------------------------
# Route tests — content provider connect (no more 404)
# ---------------------------------------------------------------------------

class TestContentProviderConnect:

    def test_seedance_connect_returns_200(self, client):
        resp = client.get("/providers/content/connect/seedance")
        assert resp.status_code == 200, f"seedance connect returned {resp.status_code}"

    def test_freepik_connect_returns_200(self, client):
        resp = client.get("/providers/content/connect/freepik")
        assert resp.status_code == 200, f"freepik connect returned {resp.status_code}"

    def test_openrouter_connect_returns_200(self, client):
        resp = client.get("/providers/content/connect/openrouter")
        assert resp.status_code == 200

    def test_elevenlabs_connect_returns_200(self, client):
        resp = client.get("/providers/content/connect/elevenlabs")
        assert resp.status_code == 200

    def test_muapi_connect_returns_200(self, client):
        resp = client.get("/providers/content/connect/muapi")
        assert resp.status_code == 200

    def test_runway_connect_returns_200(self, client):
        resp = client.get("/providers/content/connect/runway")
        assert resp.status_code == 200

    def test_kling_connect_returns_200(self, client):
        resp = client.get("/providers/content/connect/kling")
        assert resp.status_code == 200

    def test_veo_connect_returns_200(self, client):
        resp = client.get("/providers/content/connect/veo")
        assert resp.status_code == 200

    def test_unknown_provider_returns_404(self, client):
        resp = client.get("/providers/content/connect/unknown_xyz_provider")
        assert resp.status_code == 404

    def test_save_elevenlabs_stores_credential(self, client, db):
        resp = client.post(
            "/providers/content/connect/elevenlabs/save",
            data={"credential": "el_fake_key_123", "label": "ElevenLabs test"},
        )
        # Should redirect on success (302)
        assert resp.status_code in (200, 302)

        from hypogeum.credential_manager import list_secret_profiles
        profiles = list_secret_profiles(provider="elevenlabs", db_path=db)
        active = [p for p in profiles if p.status == "active"]
        assert len(active) == 1
        assert active[0].label == "ElevenLabs test"

    def test_save_seedance_stores_credential(self, client, db):
        resp = client.post(
            "/providers/content/connect/seedance/save",
            data={"credential": "mu-fake-seedance-key", "label": ""},
        )
        assert resp.status_code in (200, 302)

        from hypogeum.credential_manager import list_secret_profiles
        profiles = list_secret_profiles(provider="seedance", db_path=db)
        active = [p for p in profiles if p.status == "active"]
        assert len(active) == 1

    def test_save_unknown_provider_returns_404(self, client):
        resp = client.post(
            "/providers/content/connect/totally_unknown/save",
            data={"credential": "key123"},
        )
        assert resp.status_code == 404

    def test_connect_page_shows_provider_label(self, client):
        resp = client.get("/providers/content/connect/seedance")
        html = resp.data.decode()
        assert "Seedance" in html

    def test_connect_page_shows_env_var_hint(self, client):
        resp = client.get("/providers/content/connect/openrouter")
        html = resp.data.decode()
        assert "OPENROUTER_API_KEY" in html

    def test_connect_page_has_save_form(self, client):
        resp = client.get("/providers/content/connect/freepik")
        html = resp.data.decode()
        assert 'method="POST"' in html or "method='POST'" in html
        assert "credential" in html


# ---------------------------------------------------------------------------
# Route tests — /providers no longer links content providers to /platforms/connect/
# ---------------------------------------------------------------------------

class TestProvidersPageRouting:

    def test_seedance_link_not_platforms_connect(self, client):
        resp = client.get("/providers")
        html = resp.data.decode()
        # Find the Seedance section and check its link
        idx = html.find("Seedance")
        assert idx != -1, "Seedance not found in providers page"
        section = html[idx:idx + 500]
        assert "/platforms/connect/seedance" not in section, (
            "Seedance still links to /platforms/connect/ which 404s"
        )

    def test_freepik_link_not_platforms_connect(self, client):
        resp = client.get("/providers")
        html = resp.data.decode()
        idx = html.find("Freepik")
        assert idx != -1, "Freepik not found in providers page"
        section = html[idx:idx + 500]
        assert "/platforms/connect/freepik" not in section, (
            "Freepik still links to /platforms/connect/ which 404s"
        )

    def test_seedance_link_points_to_content_connect(self, client):
        resp = client.get("/providers")
        html = resp.data.decode()
        assert "/providers/content/connect/seedance" in html

    def test_freepik_link_points_to_content_connect(self, client):
        resp = client.get("/providers")
        html = resp.data.decode()
        assert "/providers/content/connect/freepik" in html

    def test_openrouter_link_points_to_content_connect(self, client):
        resp = client.get("/providers")
        html = resp.data.decode()
        assert "/providers/content/connect/openrouter" in html
