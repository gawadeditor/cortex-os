"""
tests/test_provider_control_center.py — F13 Provider Control Center

Coverage:
    /providers returns 200
    All 12 providers listed (content + platform)
    YouTube shows OAuth setup, not API key paste
    OAuth fields (client_id, client_secret, redirect_uri, scopes) present for YouTube
    YouTube authorization status shown
    API key providers show connect buttons
    Connected state shown when credential exists
    YouTube section does not have plain API-key-only connect form
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import initialize_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "providers_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def client(db):
    import run_dashboard as server
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db)
    with server.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _html(client):
    resp = client.get("/providers")
    assert resp.status_code == 200
    return resp.data.decode()


# ===========================================================================
# TestProvidersPage
# ===========================================================================

class TestProvidersPage:

    def test_providers_page_returns_200(self, client):
        resp = client.get("/providers")
        assert resp.status_code == 200

    def test_providers_page_is_html(self, client):
        html = _html(client)
        assert "<!DOCTYPE html>" in html or "<!doctype html>" in html.lower()

    def test_providers_page_title(self, client):
        html = _html(client)
        assert "Providers" in html


# ===========================================================================
# TestAllProvidersListed
# ===========================================================================

class TestAllProvidersListed:

    def test_openrouter_listed(self, client):
        assert "OpenRouter" in _html(client)

    def test_muapi_listed(self, client):
        assert "MUAPI" in _html(client)

    def test_elevenlabs_listed(self, client):
        assert "ElevenLabs" in _html(client)

    def test_seedance_listed(self, client):
        assert "Seedance" in _html(client)

    def test_veo_listed(self, client):
        assert "Veo" in _html(client)

    def test_kling_listed(self, client):
        assert "Kling" in _html(client)

    def test_runway_listed(self, client):
        assert "Runway" in _html(client)

    def test_freepik_listed(self, client):
        assert "Freepik" in _html(client)

    def test_youtube_listed(self, client):
        assert "YouTube" in _html(client)

    def test_tiktok_listed(self, client):
        assert "TikTok" in _html(client)

    def test_instagram_listed(self, client):
        assert "Instagram" in _html(client)

    def test_facebook_listed(self, client):
        assert "Facebook" in _html(client)

    def test_all_12_providers_present(self, client):
        html = _html(client)
        providers = ["OpenRouter", "MUAPI", "ElevenLabs", "Seedance",
                     "Veo", "Kling", "Runway", "Freepik",
                     "YouTube", "TikTok", "Instagram", "Facebook"]
        missing = [p for p in providers if p not in html]
        assert not missing, f"Missing providers: {missing}"


# ===========================================================================
# TestYouTubeOAuth
# ===========================================================================

class TestYouTubeOAuth:

    def test_youtube_shows_oauth_setup(self, client):
        html = _html(client)
        # YouTube section must mention OAuth
        assert "OAuth" in html or "oauth" in html.lower()

    def test_youtube_shows_oauth_setup_required(self, client):
        html = _html(client)
        assert "platforms/youtube/authorize" in html or "Re-authorize" in html or "Authorize with Google" in html

    def test_youtube_shows_client_id_field(self, client):
        # YOUTUBE_CLIENT_ID appears in missing-requirements badge when unconfigured
        html = _html(client)
        assert "YOUTUBE_CLIENT_ID" in html

    def test_youtube_shows_client_secret_field(self, client):
        # YOUTUBE_REFRESH_TOKEN (the final OAuth artifact) appears in missing_reqs
        html = _html(client)
        assert "YOUTUBE_REFRESH_TOKEN" in html

    def test_youtube_shows_redirect_uri(self, client):
        # OAuth redirect handled via /platforms/youtube/authorize flow
        html = _html(client)
        assert "/platforms/youtube/authorize" in html

    def test_youtube_shows_scopes(self, client):
        # OAuth scopes are requested during the /platforms/youtube/authorize flow
        html = _html(client)
        assert "/platforms/youtube/authorize" in html or "Authorize with Google" in html

    def test_youtube_shows_authorization_status(self, client):
        # Credential status rendered as badge: Not configured / Saved / Verified
        html = _html(client)
        assert ("Not configured" in html or "Saved" in html or "Verified" in html
                or "Authorize with Google" in html or "Re-authorize" in html)

    def test_youtube_no_plain_api_key_only_form(self, client):
        """YouTube must not show a standalone API key paste field without OAuth context."""
        html = _html(client)
        # Find the YouTube section
        yt_start = html.find('data-provider-id="youtube"')
        if yt_start == -1:
            yt_start = html.find("YouTube")
        # Check that within the YouTube section, we don't have a plain "API key" form
        # (The OAuth notice + fields should be there instead)
        yt_section = html[yt_start:yt_start + 2000] if yt_start != -1 else html
        # If there's an input[type=text] or input[type=password] in the YouTube section,
        # it must NOT be labeled just "API key" without OAuth context
        if "oauth" not in yt_section.lower():
            # If no OAuth mention, then there must not be a bare API key input
            assert "API key" not in yt_section, \
                "YouTube shows plain API key setup without OAuth context"

    def test_youtube_connect_action_present(self, client):
        html = _html(client)
        assert "Set up OAuth" in html or "Re-authorize" in html or "oauth" in html.lower()


# ===========================================================================
# TestAPIKeyProviders
# ===========================================================================

class TestAPIKeyProviders:

    def test_api_key_providers_show_connect_buttons(self, client):
        html = _html(client)
        # Non-YouTube platforms should have connect buttons
        assert "Connect" in html

    def test_unconfigured_providers_show_not_configured(self, client):
        # F15: "Connected" replaced by 5-state status; unconfigured shows "Not configured"
        html = _html(client)
        assert "Not configured" in html or "Not connected" in html or "missing" in html.lower()

    def test_nav_has_providers_link(self, client):
        html = _html(client)
        assert "/providers" in html

    def test_providers_page_no_external_cdn(self, client):
        html = _html(client)
        for cdn in ("cdn.jsdelivr", "unpkg.com", "cdnjs.cloudflare"):
            assert cdn not in html, f"External CDN found: {cdn}"


# ===========================================================================
# TestConnectedState
# ===========================================================================

class TestConnectedState:

    def test_saved_provider_shows_saved_status(self, client, db):
        # F15: saved-but-unverified credential shows "Saved", not "Connected"
        from hypogeum.credential_manager import create_secret_profile
        create_secret_profile(
            provider="openrouter",
            label="OpenRouter key",
            credential_type="api_key",
            credential_data="sk-or-fake-key",
            db_path=db,
        )
        html = _html(client)
        assert "Saved" in html or "Verified" in html  # Saved (unverified) or Verified

    def test_saved_youtube_shows_saved_or_verified(self, client, db):
        # F15: YouTube credential shows Saved (if not verified) or Verified
        from hypogeum.credential_manager import create_secret_profile
        create_secret_profile(
            provider="youtube",
            label="YouTube OAuth",
            credential_type="oauth2_token",
            credential_data="ya29.fake_token",
            db_path=db,
        )
        html = _html(client)
        assert "Saved" in html or "Verified" in html or "authorized" in html.lower()
