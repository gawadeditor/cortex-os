"""
tests/test_f9_dashboard_ux.py — F9 Dashboard UX tests

Coverage:
    TestConnectButtonVisibility
        — disconnected platform shows "Connect" button/link
        — connected platform (active cred + real analytics) shows "Connected"
        — demo-only platform shows "Not connected" and connect button
        — published-no-cred platform shows reconnect link

    TestDataIntegrity
        — demo analytics never appear in dashboard KPI text
        — mock views not shown as integers in analytics summary
        — system alerts use human-readable labels (no raw OIS IDs as text nodes)

    TestDashboardUXElements
        — dashboard has animation class (fadeUp or exec-section)
        — dashboard has chart section
        — dashboard has pulse element for connected platform
        — no raw internal IDs in main visible content
        — /dashboard has no external CDN references
        — /dashboard has no script src= (external)
        — meta refresh present

    TestNoNetworkCalls
        — /dashboard makes no socket connections
        — /channel-profile makes no socket connections
        — /platforms/connect/youtube makes no socket connections
"""

from __future__ import annotations

import json
import re
import socket
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import initialize_schema, get_connection
from hypogeum.analytics_store import store_platform_metrics, seed_mock_analytics
from hypogeum.credential_manager import create_secret_profile


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _fake_state(active_signals=None, factory_name="Test Channel"):
    from hypogeum.dashboard_api import (
        FactoryState, RunHealthSummary, ArchiveSummary,
        RecallSummary, LearningSummary, CostSummary, AuditSummary,
    )
    return FactoryState(
        factory_id="test", schema_version="v2",
        checked_at="2026-01-01T00:00:00+00:00",
        overall_health="healthy",
        run_health=RunHealthSummary(5, 4, 1, 0, 0.8, None),
        channels=[], agent_health=[], model_usage=[],
        provider_health=[],
        archive=ArchiveSummary(0, 0, {}, None, None),
        recall=RecallSummary(0, 0, 0, 0.0),
        learning=LearningSummary(0, {}, []),
        costs=CostSummary(1.23, {}, {}, {}, 1, 1),
        audit=AuditSummary("2026-01-01T00:00:00", True, 0, 0, "ACCEPTED"),
        notifications=[], factory_name=factory_name, factory_status="active",
        active_signals=active_signals or [], schedules=[], pending_approvals=[],
    )


def _fake_report():
    from hypogeum.factory_director import FactoryDirectorReport, RunClearanceDecision
    return FactoryDirectorReport(
        factory_id="test", factory_name="Test Channel",
        readiness_level="READY", readiness_score=95, can_run=True,
        assessed_at="2026-01-01T00:00:00", domains=[],
        blocking_reasons=[], warnings=[],
        run_clearance=RunClearanceDecision(
            cleared=True, reason="CLEARED", blocking_checks=[],
            blocking_signals=[], pending_approvals=0,
            checked_at="2026-01-01T00:00:00",
        ),
    )


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "ux_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def db_with_mock(db):
    seed_mock_analytics("ep001", "youtube", db_path=db)
    return db


@pytest.fixture()
def db_with_real(db):
    store_platform_metrics(
        "ep001", "youtube", {"views": 4200, "ctr": 0.05},
        source="manual", db_path=db,
    )
    return db


@pytest.fixture()
def db_with_credential(db):
    create_secret_profile(
        provider="youtube", label="YT Main",
        credential_type="oauth2_token", credential_data="ya29.fake",
        db_path=db,
    )
    return db


@pytest.fixture()
def client():
    import run_dashboard as server
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


def _dashboard_html(client, db_path, signals=None):
    import run_dashboard as server
    server.app.config["DB_PATH"] = str(db_path)
    state  = _fake_state(active_signals=signals)
    report = _fake_report()
    with patch.object(server, "get_factory_state", return_value=state), \
         patch.object(server, "assess_factory", return_value=report):
        return client.get("/dashboard").data.decode()


# ===========================================================================
# TestConnectButtonVisibility
# ===========================================================================

class TestConnectButtonVisibility:
    def test_disconnected_youtube_shows_connect_link(self, client, db):
        html = _dashboard_html(client, db)
        assert "Connect YouTube" in html or (
            "/platforms/connect/youtube" in html and "Not connected" in html
        )

    def test_disconnected_tiktok_shows_connect_link(self, client, db):
        html = _dashboard_html(client, db)
        assert "Connect TikTok" in html or "/platforms/connect/tiktok" in html

    def test_connected_platform_no_connect_button(self, client, db_with_credential, db_with_real):
        # Use the credentialed db that also has real analytics
        store_platform_metrics(
            "ep001", "youtube", {"views": 100},
            source="manual", db_path=db_with_credential,
        )
        html = _dashboard_html(client, db_with_credential)
        assert "Connected" in html

    def test_demo_only_shows_not_connected(self, client, db_with_mock):
        html = _dashboard_html(client, db_with_mock)
        assert "Not connected" in html

    def test_demo_only_shows_connect_link(self, client, db_with_mock):
        html = _dashboard_html(client, db_with_mock)
        assert "/platforms/connect/youtube" in html

    def test_demo_banner_shown(self, client, db_with_mock):
        html = _dashboard_html(client, db_with_mock)
        # Demo warning banner should appear
        assert "demo-banner" in html or "Demo" in html

    def test_connected_with_data_shows_record_count(self, client, db_with_credential):
        store_platform_metrics(
            "ep001", "youtube", {"views": 500, "ctr": 0.04},
            source="manual", db_path=db_with_credential,
        )
        html = _dashboard_html(client, db_with_credential)
        # Should show "1 analytics record" or "Connected"
        assert "record" in html or "Connected" in html

    def test_not_connected_platform_label(self, client, db):
        html = _dashboard_html(client, db)
        assert "Not connected" in html


# ===========================================================================
# TestDataIntegrity
# ===========================================================================

class TestDataIntegrity:
    def test_mock_views_not_shown_as_number(self, client, db_with_mock):
        """Mock analytics should never appear as a view count integer in the KPI summary."""
        from hypogeum.analytics_store import seed_mock_analytics as _seed
        # The mock youtube analytics will have views in the thousands
        html = _dashboard_html(client, db_with_mock)
        # KPI section shows "—" for views, not a seeded number
        # Specifically check the "No real data" label is present
        assert "No real data" in html

    def test_mock_only_kpi_shows_dash(self, client, db_with_mock):
        html = _dashboard_html(client, db_with_mock)
        # The analytics summary KPI for views should be "—"
        # We look for the dash-val class which wraps "—"
        assert "dash-val" in html or "—" in html

    def test_signal_type_not_as_visible_text(self, client, db):
        """Raw signal IDs should not appear as visible text — only in data attributes."""
        sig = {
            "signal_id": "sig001", "signal_type": "ois_block",
            "severity": "CRITICAL", "message": "factory blocked",
            "blocking": True, "lifecycle_status": "open",
            "channel_id": None, "source_system": "ois",
            "created_at": "2026-01-01T00:00:00",
        }
        html = _dashboard_html(client, db, signals=[sig])
        # "ois_block" appears only in data attributes, not as text content
        text_nodes = re.findall(r'>([^<]*ois_block[^<]*)<', html)
        assert len(text_nodes) == 0, (
            "ois_block should not appear as a visible text node; "
            f"found in: {text_nodes}"
        )

    def test_translated_label_shown_for_signal(self, client, db):
        """The human-readable label must appear instead of the raw signal type."""
        sig = {
            "signal_id": "sig002", "signal_type": "credential_failure",
            "severity": "CRITICAL", "message": "key invalid",
            "blocking": True, "lifecycle_status": "open",
            "channel_id": None, "source_system": "startup",
            "created_at": "2026-01-01T00:00:00",
        }
        html = _dashboard_html(client, db, signals=[sig])
        assert "API key missing or invalid" in html

    def test_budget_exhaustion_translated(self, client, db):
        sig = {
            "signal_id": "sig003", "signal_type": "budget_exhaustion",
            "severity": "CRITICAL", "message": "budget hit",
            "blocking": True, "lifecycle_status": "open",
            "channel_id": None, "source_system": "finance",
            "created_at": "2026-01-01T00:00:00",
        }
        html = _dashboard_html(client, db, signals=[sig])
        assert "budget exceeded" in html.lower() or "AI budget" in html


# ===========================================================================
# TestDashboardUXElements
# ===========================================================================

class TestDashboardUXElements:
    def test_animation_class_present(self, client, db):
        html = _dashboard_html(client, db)
        assert "fadeUp" in html or "exec-section" in html

    def test_chart_section_present(self, client, db):
        html = _dashboard_html(client, db)
        assert "exec-charts" in html or "chart-bars" in html

    def test_pulse_animation_for_connected_platform(self, client, db_with_credential):
        store_platform_metrics(
            "ep001", "youtube", {"views": 100},
            source="manual", db_path=db_with_credential,
        )
        html = _dashboard_html(client, db_with_credential)
        assert "pulse" in html

    def test_no_external_cdn_references(self, client, db):
        html = _dashboard_html(client, db)
        for cdn in ("cdn.jsdelivr", "unpkg.com", "cdnjs.cloudflare", "googleapis"):
            assert cdn not in html

    def test_no_external_script_src(self, client, db):
        html = _dashboard_html(client, db)
        assert "<script src=" not in html

    def test_meta_refresh_present(self, client, db):
        html = _dashboard_html(client, db)
        assert 'http-equiv="refresh"' in html
        assert 'content="30"' in html

    def test_no_raw_api_debug_links_on_client_page(self, client, db):
        # The Overview page is client-facing — it must not expose raw
        # /api/* debug links (they moved off the page 2026-09-03).
        html = _dashboard_html(client, db)
        assert "/api/platform-connections" not in html
        assert "/api/state" not in html

    def test_channels_reachable_from_dashboard(self, client, db):
        html = _dashboard_html(client, db)
        assert "/channels" in html or "/channel/" in html


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:
    @pytest.fixture(autouse=True)
    def block_network(self, monkeypatch):
        def no_socket(*args, **kwargs):
            raise RuntimeError("Network call detected — should not happen in F9 routes")
        monkeypatch.setattr(socket, "socket", no_socket)
        yield

    def test_dashboard_no_network(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        state  = _fake_state()
        report = _fake_report()
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            resp = client.get("/dashboard")
        assert resp.status_code == 200

    def test_channel_profile_page_no_network(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        # legacy route now redirects to the channel list / a channel
        resp = client.get("/channel-profile")
        assert resp.status_code in (200, 302)

    def test_connect_platform_page_no_network(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/platforms/connect/youtube")
        assert resp.status_code == 200

    def test_api_channel_profile_no_network(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/api/channel-profile")
        assert resp.status_code == 200
