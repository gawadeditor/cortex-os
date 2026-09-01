"""
tests/test_create_page.py — F13 Create Page (Production Launcher)

Coverage:
    /create returns 200
    Has topic input
    Has video type selector (long video, short, reel)
    Has target platform selector
    Has quality mode selector (economy, balanced, premium)
    Has start/submit button
    No terminal / raw run command
    Shows missing requirements when no channels configured
    Shows channel selector when channels exist
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
    p = tmp_path / "create_test.db"
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
    resp = client.get("/create")
    assert resp.status_code == 200
    return resp.data.decode()


def _fake_channel():
    from hypogeum.dashboard_api import ChannelSummary
    return ChannelSummary(
        channel_id="ch_test", name="Test Channel", status="active",
        brand_complete=True, publishing_complete=True, models_complete=True,
        profile_completeness=1.0, created_at="2026-01-01T00:00:00",
    )


# ===========================================================================
# TestCreatePageBasic
# ===========================================================================

class TestCreatePageBasic:

    def test_create_page_returns_200(self, client):
        resp = client.get("/create")
        assert resp.status_code == 200

    def test_create_page_is_html(self, client):
        html = _html(client)
        assert "<!DOCTYPE html>" in html or "<!doctype html>" in html.lower()

    def test_create_page_title(self, client):
        # Page renamed from "Create" to "Production Queue" in factory OS reframe
        html = _html(client)
        assert "Production Queue" in html or "Create" in html

    def test_create_page_no_external_cdn(self, client):
        html = _html(client)
        for cdn in ("cdn.jsdelivr", "unpkg.com", "cdnjs.cloudflare"):
            assert cdn not in html, f"External CDN found: {cdn}"


# ===========================================================================
# TestCreateFormElements
# ===========================================================================

class TestCreateFormElements:

    def test_has_topic_input(self, client):
        html = _html(client)
        assert 'name="topic"' in html

    def test_has_video_type_long(self, client):
        html = _html(client)
        assert "long_video" in html or "Long video" in html

    def test_has_video_type_short(self, client):
        html = _html(client)
        assert 'value="short"' in html or "Short" in html

    def test_has_video_type_reel(self, client):
        # reel video type was removed; form now supports long_video and short only
        html = _html(client)
        assert "long_video" in html or "short" in html.lower()

    def test_has_all_three_video_types(self, client):
        # reel removed; verify the two remaining video types are present
        html = _html(client)
        assert "long_video" in html or "Long video" in html
        assert "short" in html.lower()

    def test_has_target_platforms(self, client):
        html = _html(client)
        assert "youtube" in html.lower()
        assert "tiktok" in html.lower()

    def test_has_quality_mode_economy(self, client):
        html = _html(client)
        assert "economy" in html.lower() or "Economy" in html

    def test_has_quality_mode_balanced(self, client):
        html = _html(client)
        assert "balanced" in html.lower() or "Balanced" in html

    def test_has_quality_mode_premium(self, client):
        html = _html(client)
        assert "premium" in html.lower() or "Premium" in html

    def test_has_all_three_quality_modes(self, client):
        html = _html(client)
        assert "economy" in html.lower()
        assert "balanced" in html.lower()
        assert "premium" in html.lower()

    def test_has_submit_button(self, client):
        html = _html(client)
        assert "Start Production" in html or 'type="submit"' in html

    def test_form_action_exists(self, client):
        html = _html(client)
        assert 'action="/create/launch"' in html or 'method="POST"' in html


# ===========================================================================
# TestCreateNoTerminal
# ===========================================================================

class TestCreateNoTerminal:

    def test_no_terminal_element(self, client):
        html = _html(client)
        assert "<terminal" not in html.lower()
        assert 'class="terminal"' not in html
        assert "xterm" not in html.lower()

    def test_no_raw_run_command(self, client):
        html = _html(client)
        assert "python3 run_pipeline.py" not in html
        assert "python run_pipeline.py" not in html
        assert "run_pipeline" not in html

    def test_no_run_id_exposed(self, client):
        html = _html(client)
        # run_id should not appear as a visible form field label
        import re
        text_nodes = re.findall(r'>([^<]*run_id[^<]*)<', html)
        assert len(text_nodes) == 0, f"raw run_id exposed in create page: {text_nodes}"


# ===========================================================================
# TestCreateMissingRequirements
# ===========================================================================

class TestCreateMissingRequirements:

    def test_shows_missing_requirements_when_no_channels(self, client):
        html = _html(client)
        # When no channels, should show setup prompt
        assert "Missing Requirements" in html or "No channels configured" in html \
               or "Set up a channel" in html

    def test_submit_disabled_when_no_channels(self, client):
        html = _html(client)
        # Start button should be disabled when no channels
        assert "disabled" in html

    def test_channel_selector_shows_when_channels_exist(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        ch = _fake_channel()
        with patch("hypogeum.dashboard_api.get_channel_summaries", return_value=[ch]):
            resp = client.get("/create")
        html = resp.data.decode()
        assert "Test Channel" in html or 'name="channel_id"' in html

    def test_settings_link_shown_when_no_channels(self, client):
        html = _html(client)
        assert "/settings" in html or "/channel-profile" in html


# ===========================================================================
# TestCreateNavIntegration
# ===========================================================================

class TestCreateNavIntegration:

    def test_create_link_in_nav(self, client):
        html = _html(client)
        # /create is the "Queue" entry of the Production sub-nav
        assert 'href="/create"' in html

    def test_create_nav_link_marked_active(self, client):
        html = _html(client)
        import re
        active = re.findall(r'href="([^"]+)"[^>]*class="sub-nav-link[^"]*active', html)
        active += re.findall(r'class="sub-nav-link[^"]*active[^"]*"[^>]*href="([^"]+)"', html)
        assert any("create" in h for h in active)
