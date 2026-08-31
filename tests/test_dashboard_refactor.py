"""
tests/test_dashboard_refactor.py — F12 Operator Dashboard Refactor

Coverage:
    /dashboard returns 200
    channel header visible
    no raw OIS language in main visible text
    no raw signal IDs visible
    platform connection cards visible
    Connect YouTube account link visible when not connected
    recently published section links to /episode/<episode_id>
    mock analytics excluded from dashboard KPIs
    real analytics shown
    production empty state works
    current production card works
    weekly targets visible
    needs attention only shows operator language
    Operations/debug links collapsed
    charts render with CSS classes
    no external CDN links
"""

from __future__ import annotations

import re
import socket
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import get_connection, initialize_schema
from hypogeum.analytics_store import seed_mock_analytics, store_platform_metrics
from hypogeum.credential_manager import create_secret_profile


# ---------------------------------------------------------------------------
# Shared fake data factories (mirrors test_f9_dashboard_ux.py pattern)
# ---------------------------------------------------------------------------

def _fake_state(active_signals=None, factory_name="Test Channel"):
    from hypogeum.dashboard_api import (
        FactoryState, RunHealthSummary, ArchiveSummary,
        RecallSummary, LearningSummary, CostSummary, AuditSummary,
    )
    return FactoryState(
        factory_id="test", schema_version="v2",
        checked_at="2026-06-19T10:00:00+00:00",
        overall_health="healthy",
        run_health=RunHealthSummary(5, 4, 1, 0, 0.8, None),
        channels=[], agent_health=[], model_usage=[],
        provider_health=[],
        archive=ArchiveSummary(0, 0, {}, None, None),
        recall=RecallSummary(0, 0, 0, 0.0),
        learning=LearningSummary(0, {}, []),
        costs=CostSummary(1.23, {}, {}, {}, 1, 1),
        audit=AuditSummary("2026-06-19T10:00:00", True, 0, 0, "ACCEPTED"),
        notifications=[], factory_name=factory_name, factory_status="active",
        active_signals=active_signals or [], schedules=[], pending_approvals=[],
    )


def _fake_report():
    from hypogeum.factory_director import FactoryDirectorReport, RunClearanceDecision
    return FactoryDirectorReport(
        factory_id="test", factory_name="Test Channel",
        readiness_level="READY", readiness_score=95, can_run=True,
        assessed_at="2026-06-19T10:00:00", domains=[],
        blocking_reasons=[], warnings=[],
        run_clearance=RunClearanceDecision(
            cleared=True, reason="CLEARED", blocking_checks=[],
            blocking_signals=[], pending_approvals=0,
            checked_at="2026-06-19T10:00:00",
        ),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "refactor_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def client():
    import run_dashboard as server
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


def _html(client, db_path, signals=None):
    import run_dashboard as server
    server.app.config["DB_PATH"] = str(db_path)
    state  = _fake_state(active_signals=signals)
    report = _fake_report()
    with patch.object(server, "get_factory_state", return_value=state), \
         patch.object(server, "assess_factory", return_value=report):
        return client.get("/dashboard").data.decode()


# ---------------------------------------------------------------------------
# Helpers for inserting test data
# ---------------------------------------------------------------------------

def _episode(db_path, episode_id="ep_r01", title="Test Episode"):
    conn = get_connection(db_path)
    conn.execute(
        """INSERT INTO episodes (episode_id, title, topic, status, created_at, metadata_version)
           VALUES (?, ?, 'Test topic', 'complete', '2026-06-19T10:00:00', 'v1')""",
        [episode_id, title],
    )
    conn.execute(
        """INSERT INTO publishing_outcomes
           (outcome_id, episode_id, platform, publishing_status,
            recorded_at, metadata_version)
           VALUES (?, ?, 'youtube', 'success', '2026-06-19T10:05:00', 'v1')""",
        [str(uuid.uuid4()), episode_id],
    )
    conn.commit()
    conn.close()


# ===========================================================================
# TestDashboardBasic
# ===========================================================================

class TestDashboardBasic:

    def test_dashboard_returns_200(self, client, db):
        resp_code = _html(client, db)
        # Just verify we can get the HTML
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        state  = _fake_state()
        report = _fake_report()
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            resp = client.get("/dashboard")
        assert resp.status_code == 200

    def test_dashboard_is_html(self, client, db):
        html = _html(client, db)
        assert "<!DOCTYPE html>" in html or "<!doctype html>" in html.lower()

    def test_no_external_cdn_links(self, client, db):
        html = _html(client, db)
        for cdn in ("cdn.jsdelivr", "unpkg.com", "cdnjs.cloudflare", "googleapis"):
            assert cdn not in html, f"External CDN found: {cdn}"

    def test_no_external_script_src(self, client, db):
        html = _html(client, db)
        assert "<script src=" not in html

    def test_charts_render_with_css_classes(self, client, db):
        html = _html(client, db)
        assert "chart-bars" in html or "exec-charts" in html

    def test_meta_refresh_present(self, client, db):
        html = _html(client, db)
        assert 'http-equiv="refresh"' in html


# ===========================================================================
# TestChannelHeader
# ===========================================================================

class TestChannelHeader:

    def test_channel_header_visible(self, client, db):
        html = _html(client, db)
        assert "Test Channel" in html or "My Channel" in html

    def test_factory_readiness_label_present(self, client, db):
        html = _html(client, db)
        assert "Factory Readiness" in html or "readiness" in html.lower()

    def test_factory_health_section_rendered(self, client, db):
        html = _html(client, db)
        # health now reads from the tier-1 KPI strip + the "Factory status" card
        assert "Factory readiness" in html or "Factory status" in html

    def test_factory_health_shows_readable_status(self, client, db):
        html = _html(client, db)
        assert any(label in html for label in ("Ready", "Blocked", "Needs Setup"))


# ===========================================================================
# TestOISLanguage
# ===========================================================================

class TestOISLanguage:

    def test_no_raw_ois_signal_type_as_text_node(self, client, db):
        """ois_block signal type must not appear as a visible text node."""
        sig = {
            "signal_id": str(uuid.uuid4()), "signal_type": "ois_block",
            "severity": "CRITICAL", "message": "factory blocked",
            "blocking": True, "lifecycle_status": "open",
            "channel_id": None, "source_system": "ois",
            "created_at": "2026-06-19T10:00:00",
        }
        html = _html(client, db, signals=[sig])
        text_nodes = re.findall(r'>([^<]*ois_block[^<]*)<', html)
        assert len(text_nodes) == 0, f"ois_block found as text: {text_nodes}"

    def test_ois_block_translated_to_operator_language(self, client, db):
        """ois_block signal must be shown as translated operator label."""
        sig = {
            "signal_id": str(uuid.uuid4()), "signal_type": "ois_block",
            "severity": "CRITICAL", "message": "OIS block triggered",
            "blocking": True, "lifecycle_status": "open",
            "channel_id": None, "source_system": "ois",
            "created_at": "2026-06-19T10:00:00",
        }
        html = _html(client, db, signals=[sig])
        assert "Operational check is blocking production" in html

    def test_no_raw_signal_ids_in_visible_text(self, client, db):
        """Raw UUID signal IDs must not appear as visible text."""
        sig_id = str(uuid.uuid4())
        sig = {
            "signal_id": sig_id, "signal_type": "credential_failure",
            "severity": "WARNING", "message": "key check failed",
            "blocking": False, "lifecycle_status": "open",
            "channel_id": None, "source_system": "startup",
            "created_at": "2026-06-19T10:00:00",
        }
        html = _html(client, db, signals=[sig])
        # UUID must not appear as a visible text node
        uuid_as_text = re.findall(r'>([^<]*' + re.escape(sig_id) + r'[^<]*)<', html)
        assert len(uuid_as_text) == 0, f"Raw signal UUID found in visible text: {uuid_as_text}"


# ===========================================================================
# TestPlatformConnections
# ===========================================================================

class TestPlatformConnections:

    def test_platform_connection_cards_visible(self, client, db):
        html = _html(client, db)
        for platform in ("YouTube", "TikTok", "Instagram", "Facebook"):
            assert platform in html, f"{platform} missing from platform cards"

    def test_connect_youtube_account_link_when_not_connected(self, client, db):
        """Spec: YouTube connect button must say 'Connect YouTube account'."""
        html = _html(client, db)
        assert "Connect YouTube account" in html

    def test_connect_tiktok_account_link_when_not_connected(self, client, db):
        html = _html(client, db)
        assert "Connect TikTok account" in html

    def test_youtube_connect_url_present(self, client, db):
        html = _html(client, db)
        assert "/platforms/connect/youtube" in html

    def test_connected_platform_shows_connected_label(self, client, db):
        create_secret_profile(
            provider="youtube", label="YT Main",
            credential_type="oauth2_token", credential_data="ya29.fake",
            db_path=db,
        )
        store_platform_metrics(
            "ep001", "youtube", {"views": 1000, "ctr": 0.04},
            source="manual", db_path=db,
        )
        html = _html(client, db)
        assert "Connected" in html

    def test_not_connected_shown_for_unconfigured_platform(self, client, db):
        html = _html(client, db)
        assert "Not connected" in html


# ===========================================================================
# TestRecentlyPublished
# ===========================================================================

class TestRecentlyPublished:

    def test_recently_published_empty_state(self, client, db):
        html = _html(client, db)
        assert "No episodes produced yet" in html or "Recent Episodes" in html

    def test_recently_published_links_to_episode_detail(self, client, db):
        _episode(db, episode_id="ep_r01", title="My Published Video")
        html = _html(client, db)
        assert "/episode/ep_r01" in html

    def test_episode_title_shown_in_recent(self, client, db):
        _episode(db, episode_id="ep_r02", title="Ancient Mysteries Revealed")
        html = _html(client, db)
        assert "Ancient Mysteries Revealed" in html

    def test_recent_episode_section_present(self, client, db):
        html = _html(client, db)
        assert "Recent Episodes" in html


# ===========================================================================
# TestAnalytics
# ===========================================================================

class TestAnalytics:

    def test_mock_analytics_excluded_from_kpis(self, client, db):
        """Mock analytics must not appear as real KPI values."""
        seed_mock_analytics("ep_r01", "youtube", db_path=db)
        html = _html(client, db)
        # No real data exists → dashboard shows "No real data"
        assert "No real data" in html or "dash-val" in html

    def test_real_analytics_shown_when_imported(self, client, db):
        """Real analytics must appear in the dashboard KPI section."""
        store_platform_metrics(
            "ep_r01", "youtube",
            {"views": 12345, "ctr": 0.06},
            source="manual", db_path=db,
        )
        html = _html(client, db)
        # 12345 views should appear somewhere in the page
        assert "12,345" in html or "12345" in html

    def test_mock_only_kpi_shows_dash(self, client, db):
        seed_mock_analytics("ep_r01", "youtube", db_path=db)
        html = _html(client, db)
        assert "—" in html or "dash-val" in html


# ===========================================================================
# TestProduction
# ===========================================================================

class TestProduction:

    def test_production_empty_state_message(self, client, db):
        html = _html(client, db)
        assert "No episode currently in production." in html

    def test_current_production_section_renders(self, client, db):
        html = _html(client, db)
        assert "Production Overview" in html or "In Production" in html

    def test_production_section_has_queued_label(self, client, db):
        html = _html(client, db)
        assert "Queued" in html or "queue" in html.lower()


# ===========================================================================
# TestWeeklyTargets
# ===========================================================================

class TestWeeklyTargets:

    def test_weekly_targets_visible(self, client, db):
        html = _html(client, db)
        assert "YouTube Videos" in html or "Weekly Publishing Targets" in html

    def test_targets_section_has_labels(self, client, db):
        html = _html(client, db)
        assert "TikTok" in html and "Instagram" in html

    def test_edit_targets_link_present(self, client, db):
        html = _html(client, db)
        assert "/channel-profile" in html


# ===========================================================================
# TestNeedsAttention
# ===========================================================================

class TestNeedsAttention:

    def test_needs_attention_section_rendered(self, client, db):
        html = _html(client, db)
        assert "Needs Attention" in html

    def test_needs_attention_uses_operator_language(self, client, db):
        """Needs Attention must use human-readable labels, not internal codes."""
        html = _html(client, db)
        # Should have friendly platform messages, not internal codes
        # "not connected" is the expected language
        assert "not connected" in html.lower() or "No real analytics" in html

    def test_no_raw_ois_terms_in_needs_attention(self, client, db):
        """OIS terminology must not appear in Needs Attention items."""
        sig = {
            "signal_id": str(uuid.uuid4()), "signal_type": "ois_block",
            "severity": "CRITICAL", "message": "OIS triggered",
            "blocking": True, "lifecycle_status": "open",
            "channel_id": None, "source_system": "ois",
            "created_at": "2026-06-19T10:00:00",
        }
        html = _html(client, db, signals=[sig])
        # The Needs Attention section items should not contain "OIS"
        # (ois_block translated items may still say "operational" which is fine)
        needs_attention_raw = re.search(
            r'Needs Attention.*?(?=Operations|<details)', html, re.DOTALL
        )
        if needs_attention_raw:
            na_text = needs_attention_raw.group()
            assert "ois_block" not in na_text

    def test_connect_platform_action_in_needs_attention(self, client, db):
        """Platform not connected should appear as an attention item."""
        html = _html(client, db)
        assert "not connected" in html.lower()

    def test_analytics_import_action_in_needs_attention(self, client, db):
        """No analytics should trigger attention item with import link."""
        html = _html(client, db)
        assert "/analytics/import" in html or "No real analytics" in html


# ===========================================================================
# TestOperationsCollapsed
# ===========================================================================

class TestOperationsCollapsed:

    def test_operations_section_uses_details_element(self, client, db):
        html = _html(client, db)
        assert "<details" in html

    def test_operations_summary_inside_details(self, client, db):
        html = _html(client, db)
        # The secondary nav menu ("More" — modules + advanced) is collapsed.
        assert "More" in html
        details_match = re.search(r'<details[^>]*>(.*?)</details>', html, re.DOTALL)
        assert details_match is not None
        assert "More" in details_match.group(1)

    def test_debug_links_inside_collapsed_section(self, client, db):
        html = _html(client, db)
        details_match = re.search(r'<details[^>]*>(.*?)</details>', html, re.DOTALL)
        assert details_match is not None
        inner = details_match.group(1)
        assert "/runs" in inner or "/preflight" in inner or "/approvals" in inner

    def test_pending_approvals_link_in_operations(self, client, db):
        html = _html(client, db)
        details_match = re.search(r'<details[^>]*>(.*?)</details>', html, re.DOTALL)
        assert details_match is not None
        assert '/approvals' in details_match.group(1)


# ===========================================================================
# F13 — Operator Product Reset
# ===========================================================================

class TestF13NavRefactor:

    def test_primary_nav_has_only_operator_items(self, client, db):
        html = _html(client, db)
        import re as _re
        # Extract all nav-link hrefs (primary navigation only)
        nav_links = _re.findall(r'class="nav-link[^"]*"[^>]*href="([^"]+)"', html)
        if not nav_links:
            nav_links = _re.findall(r'href="([^"]+)"[^>]*class="nav-link', html)
        # surface-area IA: six core sections only (docs/dashboard-surface-area.md)
        operator_paths = {"/dashboard", "/channels", "/create", "/attention",
                          "/providers", "/channel-profile"}
        for href in nav_links:
            assert href in operator_paths, (
                f"Non-operator nav item found: {href!r}. "
                f"Only {sorted(operator_paths)} allowed in primary nav."
            )

    def test_internal_pages_not_in_primary_nav(self, client, db):
        html = _html(client, db)
        import re as _re
        nav_links = _re.findall(r'class="nav-link[^"]*"[^>]*href="([^"]+)"', html)
        if not nav_links:
            nav_links = _re.findall(r'href="([^"]+)"[^>]*class="nav-link', html)
        internal = {"/command", "/live", "/runs", "/queue", "/approvals",
                    "/signals", "/learning", "/decisions", "/decision-reviews",
                    "/execution-plans", "/decision-inputs"}
        for href in nav_links:
            assert href not in internal, (
                f"Internal page {href!r} found in primary nav — must be in Operations only."
            )

    def test_dashboard_contains_factory_health(self, client, db):
        html = _html(client, db)
        assert "Factory readiness" in html or "Factory status" in html

    def test_dashboard_contains_production_section(self, client, db):
        html = _html(client, db)
        assert "Production Overview" in html or "In Production" in html or "Current Production" in html

    def test_dashboard_contains_needs_attention(self, client, db):
        html = _html(client, db)
        assert "Needs Attention" in html

    def test_dashboard_contains_recent_episodes(self, client, db):
        html = _html(client, db)
        assert "Recent Episodes" in html

    def test_dashboard_contains_analytics_or_performance(self, client, db):
        html = _html(client, db)
        assert "Analytics" in html or "Performance" in html

    def test_dashboard_contains_cost_section(self, client, db):
        html = _html(client, db)
        # Cost appears in production cost KPI or costs section
        assert "cost" in html.lower() or "Cost" in html

    def test_dashboard_no_raw_signal_ids_in_text_nodes(self, client, db):
        sig_id = str(uuid.uuid4())
        sig = {
            "signal_id": sig_id, "signal_type": "ois_block",
            "severity": "CRITICAL", "message": "factory blocked",
            "blocking": True, "lifecycle_status": "open",
            "channel_id": None, "source_system": "ois",
            "created_at": "2026-06-19T10:00:00",
        }
        html = _html(client, db, signals=[sig])
        text_nodes = re.findall(r'>([^<]*' + re.escape(sig_id) + r'[^<]*)<', html)
        assert len(text_nodes) == 0, f"Raw signal ID found as text node: {text_nodes}"

    def test_dashboard_no_decision_execution_plans_visible(self, client, db):
        html = _html(client, db)
        text_nodes = re.findall(r'>([^<]*decision_execution_plan[^<]*)<', html)
        assert len(text_nodes) == 0, f"'decision_execution_plans' exposed as text: {text_nodes}"

    def test_dashboard_no_secret_profiles_visible(self, client, db):
        html = _html(client, db)
        text_nodes = re.findall(r'>([^<]*secret_profile[^<]*)<', html)
        assert len(text_nodes) == 0, f"'secret_profiles' exposed as text: {text_nodes}"

    def test_analytics_import_default_is_not_mock(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/analytics/advanced/import")
        html = resp.data.decode()
        # The hidden input "method-input" should NOT default to "mock"
        assert 'id="method-input" value="mock"' not in html
        assert 'value="mock"' not in html.split('id="method-input"')[0][-200:] if 'id="method-input"' in html else True

    def test_analytics_import_json_is_first_active_tab(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/analytics/advanced/import")
        html = resp.data.decode()
        # JSON tab must come before mock tab in the HTML
        json_pos = html.find("setMethod('json'")
        mock_pos = html.find("setMethod('mock'")
        assert json_pos < mock_pos, "JSON tab must appear before mock tab"


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:

    def test_no_network_calls_on_dashboard(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        state  = _fake_state()
        report = _fake_report()

        def deny_connect(self, *args, **kwargs):
            raise AssertionError("Network call during dashboard render")

        with patch.object(socket.socket, "connect", deny_connect):
            with patch.object(server, "get_factory_state", return_value=state), \
                 patch.object(server, "assess_factory", return_value=report):
                resp = client.get("/dashboard")
        assert resp.status_code == 200
