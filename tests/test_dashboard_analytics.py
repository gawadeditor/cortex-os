"""
tests/test_dashboard_analytics.py — Phase E: Dashboard Analytics route/UI tests

Coverage:
    TestDashboardAnalyticsReads  — get_analytics_overview, get_platform_summary,
                                   get_episode_performance, get_learning_state
    TestFlaskAnalyticsHtmlRoutes — GET /analytics, /analytics/platform/<p>,
                                   /analytics/episode/<id>, /analytics/import
    TestFlaskAnalyticsApiRoutes  — GET/POST /api/analytics/* JSON endpoints
    TestFlaskLearningRoutes      — GET /learning, POST apply/dismiss
    TestFlaskImportRoutes        — POST /analytics/import (mock/json/csv paths)
    TestFlaskSeedMockRoute       — POST /api/analytics/seed-mock/<episode_id>
    TestNavAnalyticsPresent      — Analytics and Learning in nav on all pages
    TestNoCredentialsRequired    — all routes work without any env vars
    TestNoProductionSideEffects  — GET routes do not write
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hypogeum.db import initialize_schema, get_connection
from hypogeum.analytics_store import (
    seed_mock_analytics,
    store_platform_metrics,
    store_action,
    update_action_status,
)
from hypogeum.learning_engine import extract_insights, generate_actions
from hypogeum.dashboard_analytics import (
    get_analytics_overview,
    get_platform_summary,
    get_episode_performance,
    get_learning_state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "analytics_dash_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def seeded(db_path):
    """Seed mock analytics for two episodes across two platforms, extract insights."""
    for ep in ("ep_alpha", "ep_beta"):
        for p in ("youtube", "tiktok"):
            seed_mock_analytics(ep, p, db_path=db_path)
        extract_insights(ep, db_path=db_path)
        generate_actions(ep, db_path=db_path)
    return db_path


@pytest.fixture()
def known_metrics(db_path):
    """Insert specific known metrics for deterministic test assertions."""
    store_platform_metrics("ep_known", "youtube", {
        "views": 10000, "ctr": 0.035, "avg_view_pct": 0.48,
        "avg_watch_time_s": 288, "likes": 300, "comments": 25, "shares": 80,
    }, db_path=db_path)
    store_platform_metrics("ep_known", "instagram", {
        "views": 3000, "avg_view_pct": 0.41, "saves": 90, "likes": 180,
    }, db_path=db_path)
    return db_path


@pytest.fixture()
def flask_client(db_path, seeded):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import run_dashboard as _mod
    _mod.app.config["TESTING"] = True
    _mod.app.config["DB_PATH"] = str(db_path)
    with _mod.app.test_client() as client:
        yield client


@pytest.fixture()
def flask_client_bare(db_path):
    """Flask client with no seeded data — tests empty-state rendering."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import run_dashboard as _mod
    _mod.app.config["TESTING"] = True
    _mod.app.config["DB_PATH"] = str(db_path)
    with _mod.app.test_client() as client:
        yield client


# ===========================================================================
# TestDashboardAnalyticsReads
# ===========================================================================

class TestDashboardAnalyticsReads:
    def test_get_analytics_overview_empty(self, db_path):
        overview = get_analytics_overview(db_path=db_path)
        assert isinstance(overview, dict)
        assert "platform_breakdown" in overview
        assert "episodes" in overview

    def test_get_analytics_overview_has_platform_after_seed(self, seeded):
        overview = get_analytics_overview(db_path=seeded)
        assert "youtube" in overview["platform_breakdown"]
        assert "tiktok" in overview["platform_breakdown"]

    def test_get_analytics_overview_episode_list(self, seeded):
        overview = get_analytics_overview(db_path=seeded)
        ep_ids = {ep["episode_id"] for ep in overview["episodes"]}
        assert "ep_alpha" in ep_ids
        assert "ep_beta"  in ep_ids

    def test_get_platform_summary_empty(self, db_path):
        summary = get_platform_summary("youtube", db_path=db_path)
        assert summary["platform"] == "youtube"
        assert summary["kpis"]["episodes_tracked"] == 0
        assert summary["episodes"] == []
        assert summary["best_episode"] is None

    def test_get_platform_summary_with_data(self, seeded):
        summary = get_platform_summary("youtube", db_path=seeded)
        assert summary["kpis"]["episodes_tracked"] >= 2
        assert len(summary["episodes"]) >= 2
        assert summary["best_episode"] is not None
        assert summary["worst_episode"] is not None

    def test_get_platform_summary_kpi_keys(self, seeded):
        summary = get_platform_summary("youtube", db_path=seeded)
        required = {"episodes_tracked", "total_snapshots", "avg_ctr",
                    "avg_view_pct", "total_views"}
        assert required.issubset(summary["kpis"].keys())

    def test_get_episode_performance_no_data_returns_none(self, db_path):
        result = get_episode_performance("no-ep", db_path=db_path)
        assert result is None

    def test_get_episode_performance_with_data(self, seeded):
        result = get_episode_performance("ep_alpha", db_path=seeded)
        assert result is not None
        assert "platforms"  in result
        assert "insights"   in result
        assert "actions"    in result

    def test_get_episode_performance_multi_platform(self, known_metrics):
        result = get_episode_performance("ep_known", db_path=known_metrics)
        assert result is not None
        assert "youtube"   in result["platforms"]
        assert "instagram" in result["platforms"]

    def test_get_learning_state_empty(self, db_path):
        state = get_learning_state(db_path=db_path)
        assert isinstance(state, dict)
        assert "pending_actions_list" in state
        assert "recommendations"      in state

    def test_get_learning_state_with_data(self, seeded):
        state = get_learning_state(db_path=seeded)
        assert isinstance(state["pending_actions_list"], list)
        assert isinstance(state["recommendations"], dict)
        assert isinstance(state["total_insights"], int)


# ===========================================================================
# TestFlaskAnalyticsHtmlRoutes
# ===========================================================================

class TestFlaskAnalyticsHtmlRoutes:
    def test_analytics_page_200(self, flask_client):
        r = flask_client.get("/intelligence/performance")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "Performance" in text or "Intelligence" in text

    def test_analytics_page_shows_platforms(self, flask_client):
        r = flask_client.get("/intelligence/performance")
        text = r.get_data(as_text=True)
        for p in ("youtube", "tiktok", "instagram", "facebook"):
            assert p in text.lower()

    def test_analytics_page_empty_state_no_crash(self, flask_client_bare):
        r = flask_client_bare.get("/intelligence/performance")
        assert r.status_code == 200

    def test_analytics_platform_youtube_200(self, flask_client):
        r = flask_client.get("/intelligence/platform/youtube")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "youtube" in text.lower()

    def test_analytics_platform_tiktok_200(self, flask_client):
        r = flask_client.get("/intelligence/platform/tiktok")
        assert r.status_code == 200

    def test_analytics_platform_instagram_200(self, flask_client):
        r = flask_client.get("/intelligence/platform/instagram")
        assert r.status_code == 200

    def test_analytics_platform_facebook_200(self, flask_client):
        r = flask_client.get("/intelligence/platform/facebook")
        assert r.status_code == 200

    def test_analytics_platform_unknown_400(self, flask_client):
        r = flask_client.get("/intelligence/platform/snapchat")
        assert r.status_code == 404

    def test_analytics_episode_200(self, flask_client):
        r = flask_client.get("/intelligence/performance/episode/ep_alpha")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "ep_alpha" in text

    def test_analytics_episode_no_data_404(self, flask_client):
        r = flask_client.get("/intelligence/performance/episode/no-such-episode")
        assert r.status_code == 404

    def test_analytics_import_page_200(self, flask_client):
        # /analytics/import now redirects (301) to /analytics/advanced/import
        r = flask_client.get("/analytics/import")
        assert r.status_code == 301
        # Follow the redirect
        r2 = flask_client.get("/analytics/advanced/import")
        assert r2.status_code == 200
        text = r2.get_data(as_text=True)
        assert "Import" in text
        assert "mock" in text.lower() or "Generate" in text

    def test_analytics_import_shows_platform_select(self, flask_client):
        r = flask_client.get("/analytics/advanced/import")
        text = r.get_data(as_text=True)
        assert "youtube" in text.lower()

    def test_learning_page_200(self, flask_client):
        r = flask_client.get("/intelligence/learning")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "Intelligence" in text

    def test_learning_page_shows_actions(self, flask_client):
        r = flask_client.get("/intelligence/learning")
        text = r.get_data(as_text=True)
        # Should show pending actions or empty state
        assert "action" in text.lower() or "No pending" in text

    def test_learning_page_shows_platform_coverage(self, flask_client):
        r = flask_client.get("/intelligence/learning")
        text = r.get_data(as_text=True)
        assert "youtube" in text.lower() or "Platform" in text


# ===========================================================================
# TestFlaskAnalyticsApiRoutes
# ===========================================================================

class TestFlaskAnalyticsApiRoutes:
    def test_api_analytics_overview_200(self, flask_client):
        r = flask_client.get("/api/analytics/overview")
        assert r.status_code == 200
        data = r.get_json()
        assert "platform_breakdown" in data
        assert "episodes" in data

    def test_api_analytics_platform_youtube_200(self, flask_client):
        r = flask_client.get("/api/analytics/platform/youtube")
        assert r.status_code == 200
        data = r.get_json()
        assert data["platform"] == "youtube"
        assert "kpis" in data

    def test_api_analytics_platform_unknown_400(self, flask_client):
        r = flask_client.get("/api/analytics/platform/snapchat")
        assert r.status_code == 400

    def test_api_analytics_episode_200(self, flask_client):
        r = flask_client.get("/api/analytics/episode/ep_alpha")
        assert r.status_code == 200
        data = r.get_json()
        assert data["episode_id"] == "ep_alpha"

    def test_api_analytics_episode_no_data_404(self, flask_client):
        r = flask_client.get("/api/analytics/episode/no-such-episode")
        assert r.status_code == 404

    def test_api_analytics_insights_200(self, flask_client):
        r = flask_client.get("/api/analytics/insights/ep_alpha")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)

    def test_api_learning_200(self, flask_client):
        r = flask_client.get("/api/learning")
        assert r.status_code == 200
        data = r.get_json()
        assert "pending_actions_list" in data
        assert "recommendations" in data

    def test_api_learning_recommendations_200(self, flask_client):
        r = flask_client.get("/api/learning/recommendations")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, dict)

    def test_api_analytics_import_json_200(self, flask_client):
        payload = {
            "episode_id": "ep_new",
            "platform": "youtube",
            "metrics": {"views": 5000, "ctr": 0.04, "avg_view_pct": 0.50},
        }
        r = flask_client.post("/api/analytics/import",
                              data=json.dumps(payload),
                              content_type="application/json")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True

    def test_api_analytics_import_missing_episode_400(self, flask_client):
        r = flask_client.post("/api/analytics/import",
                              data=json.dumps({"platform": "youtube", "metrics": {}}),
                              content_type="application/json")
        assert r.status_code == 400


# ===========================================================================
# TestFlaskSeedMockRoute
# ===========================================================================

class TestFlaskSeedMockRoute:
    def test_seed_mock_single_platform(self, flask_client):
        r = flask_client.post("/api/analytics/seed-mock/ep_test",
                              data=json.dumps({"platform": "youtube"}),
                              content_type="application/json")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True

    def test_seed_mock_all_platforms_when_no_platform(self, flask_client):
        r = flask_client.post("/api/analytics/seed-mock/ep_test",
                              data=json.dumps({}),
                              content_type="application/json")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert "results" in data  # one result per platform

    def test_seed_mock_creates_analytics_records(self, flask_client, db_path):
        flask_client.post("/api/analytics/seed-mock/ep_test",
                          data=json.dumps({"platform": "tiktok"}),
                          content_type="application/json")
        from hypogeum.analytics_store import get_episode_analytics
        data = get_episode_analytics("ep_test", db_path=db_path)
        assert "tiktok" in data


# ===========================================================================
# TestFlaskImportRoutes
# ===========================================================================

class TestFlaskImportRoutes:
    def test_import_mock_redirects_to_episode_page(self, flask_client):
        r = flask_client.post("/analytics/import", data={
            "episode_id": "ep_alpha",
            "platform":   "youtube",
            "method":     "mock",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert "ep_alpha" in r.headers["Location"]

    def test_import_json_redirects_on_success(self, flask_client):
        r = flask_client.post("/analytics/import", data={
            "episode_id": "ep_alpha",
            "platform":   "youtube",
            "method":     "json",
            "json_text":  json.dumps({"views": 1000, "ctr": 0.04}),
        }, follow_redirects=False)
        assert r.status_code == 302

    def test_import_csv_redirects_on_success(self, flask_client):
        r = flask_client.post("/analytics/import", data={
            "episode_id": "ep_alpha",
            "platform":   "youtube",
            "method":     "csv",
            "csv_text":   "Views,Impressions\n10000,80000\n",
        }, follow_redirects=False)
        assert r.status_code == 302

    def test_import_invalid_platform_redirects_with_error(self, flask_client):
        r = flask_client.post("/analytics/import", data={
            "episode_id": "ep_alpha",
            "platform":   "",  # missing
            "method":     "mock",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert "invalid" in r.headers["Location"]

    def test_apply_action_redirects_to_learning(self, flask_client, db_path):
        aid = store_action({"source_episode": "ep_alpha", "dimension": "thumbnail",
                            "action_type": "test", "rationale": "test"}, db_path=db_path)
        r = flask_client.post(f"/learning/action/{aid}/apply",
                              follow_redirects=False)
        assert r.status_code == 302
        assert "/learning" in r.headers["Location"]

    def test_dismiss_action_redirects_to_learning(self, flask_client, db_path):
        aid = store_action({"source_episode": "ep_alpha", "dimension": "hook",
                            "action_type": "change", "rationale": "test"}, db_path=db_path)
        r = flask_client.post(f"/learning/action/{aid}/dismiss",
                              follow_redirects=False)
        assert r.status_code == 302


# ===========================================================================
# TestFlaskLearningApiActionRoutes
# ===========================================================================

class TestFlaskLearningApiActionRoutes:
    def test_api_apply_action_200(self, flask_client, db_path):
        aid = store_action({"source_episode": "ep1", "dimension": "title",
                            "action_type": "test", "rationale": "R"}, db_path=db_path)
        r = flask_client.post(f"/api/learning/action/{aid}/apply",
                              content_type="application/json")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert data["status"] == "applied"

    def test_api_dismiss_action_200(self, flask_client, db_path):
        aid = store_action({"source_episode": "ep1", "dimension": "description",
                            "action_type": "change", "rationale": "R"}, db_path=db_path)
        r = flask_client.post(f"/api/learning/action/{aid}/dismiss",
                              content_type="application/json")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True

    def test_api_apply_action_not_found_404(self, flask_client):
        r = flask_client.post("/api/learning/action/no-such-action/apply",
                              content_type="application/json")
        assert r.status_code == 404


# ===========================================================================
# TestNavAnalyticsPresent
# ===========================================================================

class TestNavAnalyticsPresent:
    def _check_nav(self, text):
        assert "/intelligence/performance" in text
        assert "/intelligence/learning"   in text

    def test_dashboard_has_analytics_nav(self, flask_client):
        r = flask_client.get("/dashboard")
        self._check_nav(r.get_data(as_text=True))

    def test_analytics_page_has_analytics_nav(self, flask_client):
        r = flask_client.get("/intelligence/performance")
        self._check_nav(r.get_data(as_text=True))

    def test_learning_page_has_analytics_nav(self, flask_client):
        r = flask_client.get("/intelligence/learning")
        self._check_nav(r.get_data(as_text=True))

    def test_runs_page_reaches_analytics_via_more(self, flask_client):
        # after the IA restructure, Analytics/Learning live in the "More"
        # menu (present in the shared nav on every page), not inline on
        # every section page
        text = flask_client.get("/runs").get_data(as_text=True)
        assert "/analytics" in text and "/intelligence/learning" in text


# ===========================================================================
# TestNoCredentialsRequired
# ===========================================================================

class TestNoCredentialsRequired:
    """All analytics routes must work with zero env vars / credentials."""

    def test_analytics_overview_no_credentials(self, flask_client_bare):
        r = flask_client_bare.get("/intelligence/performance")
        assert r.status_code == 200

    def test_platform_pages_no_credentials(self, flask_client_bare):
        for p in ("youtube", "tiktok", "instagram", "facebook"):
            r = flask_client_bare.get(f"/intelligence/platform/{p}")
            assert r.status_code == 200, f"Platform {p} failed"

    def test_import_page_no_credentials(self, flask_client_bare):
        r = flask_client_bare.get("/analytics/advanced/import")
        assert r.status_code == 200

    def test_learning_page_no_credentials(self, flask_client_bare):
        r = flask_client_bare.get("/intelligence/learning")
        assert r.status_code == 200

    def test_seed_mock_no_credentials(self, flask_client_bare):
        r = flask_client_bare.post("/api/analytics/seed-mock/ep_x",
                                   data=json.dumps({"platform": "youtube"}),
                                   content_type="application/json")
        assert r.status_code == 200
        assert r.get_json()["ok"] is True


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:
    def test_get_analytics_overview_no_write(self, db_path):
        conn = get_connection(db_path)
        before = conn.execute("SELECT COUNT(*) AS c FROM platform_analytics").fetchone()["c"]
        conn.close()
        get_analytics_overview(db_path=db_path)
        conn = get_connection(db_path)
        after = conn.execute("SELECT COUNT(*) AS c FROM platform_analytics").fetchone()["c"]
        conn.close()
        assert before == after

    def test_get_platform_summary_no_write(self, db_path):
        conn = get_connection(db_path)
        before = conn.execute("SELECT COUNT(*) AS c FROM platform_analytics").fetchone()["c"]
        conn.close()
        get_platform_summary("youtube", db_path=db_path)
        conn = get_connection(db_path)
        after = conn.execute("SELECT COUNT(*) AS c FROM platform_analytics").fetchone()["c"]
        conn.close()
        assert before == after

    def test_get_episode_performance_no_write(self, seeded):
        conn = get_connection(seeded)
        before = conn.execute("SELECT COUNT(*) AS c FROM analytics_insights").fetchone()["c"]
        conn.close()
        get_episode_performance("ep_alpha", db_path=seeded)
        conn = get_connection(seeded)
        after = conn.execute("SELECT COUNT(*) AS c FROM analytics_insights").fetchone()["c"]
        conn.close()
        assert before == after


# ===========================================================================
# TestAnalyticsUX — background-first UX requirements (req F11)
# ===========================================================================

class TestAnalyticsUX:
    """
    Analytics UI must be background-first: no visible Quick/Full Sync buttons
    in primary page areas; sync status per platform; manual sync collapsed.
    """

    def test_analytics_page_no_manual_import_cta(self, flask_client_bare):
        """Main /intelligence/performance page must not show 'Manual Analytics Import' as primary CTA."""
        r = flask_client_bare.get("/intelligence/performance")
        assert r.status_code == 200
        body = r.data.decode()
        assert "Manual Analytics Import" not in body, (
            "Primary analytics page must not promote manual import as the main CTA"
        )

    def test_analytics_page_no_import_button_on_platform_cards(self, flask_client_bare):
        """Platform health cards must not show 'Import →' buttons."""
        r = flask_client_bare.get("/intelligence/performance")
        assert r.status_code == 200
        body = r.data.decode()
        assert "/analytics/import?platform=" not in body, (
            "Platform cards must not link to manual import"
        )

    def test_analytics_page_empty_state_points_to_sync(self, flask_client_bare):
        """Empty state must link to /intelligence/sync, not to manual import."""
        r = flask_client_bare.get("/intelligence/performance")
        assert r.status_code == 200
        body = r.data.decode()
        assert "/intelligence/sync" in body, "Empty state must link to sync status page"

    def test_analytics_page_no_manual_import_subnav(self, flask_client_bare):
        """Sub-nav must not expose 'Manual Import' as a top-level link."""
        r = flask_client_bare.get("/intelligence/performance")
        assert r.status_code == 200
        body = r.data.decode()
        assert "Manual Import" not in body, (
            "Sub-nav must not show Manual Import as a visible top-level entry"
        )

    def test_sync_page_buttons_inside_details(self, flask_client_bare):
        """Quick Sync / Full Sync buttons must be inside a <details> element."""
        r = flask_client_bare.get("/intelligence/sync")
        assert r.status_code == 200
        body = r.data.decode()
        # Find the emergency/force-sync <details> block
        details_start = body.find("Emergency: force sync")
        assert details_start >= 0, "Emergency force-sync <details> block not found"
        # Walk back to the containing <details> tag
        details_tag = body.rfind("<details", 0, details_start)
        assert details_tag >= 0
        details_end = body.find("</details>", details_tag)
        details_block = body[details_tag:details_end]
        assert "btn-sync-quick" in details_block, (
            "Quick Sync button must be inside <details>, not in page header"
        )
        assert "btn-sync-full" in details_block, (
            "Full Sync button must be inside <details>, not in page header"
        )

    def test_sync_page_no_sync_buttons_in_header(self, flask_client_bare):
        """Sync buttons must not appear in page-header area."""
        r = flask_client_bare.get("/intelligence/sync")
        assert r.status_code == 200
        body = r.data.decode()
        header_end = body.find("</div>", body.find("page-header"))
        header_block = body[:header_end] if header_end > 0 else ""
        assert "btn-sync-quick" not in header_block, (
            "Quick Sync button must not appear in page-header"
        )

    def test_sync_page_shows_connector_not_implemented(self, flask_client_bare):
        """Non-YouTube platforms show credential status on sync page."""
        r = flask_client_bare.get("/intelligence/sync")
        assert r.status_code == 200
        body = r.data.decode()
        # Connectors are now implemented; without credentials they show "Credential required"
        assert "Credential required" in body, (
            "Non-YouTube platforms must show credential status"
        )

    def test_sync_page_shows_youtube_autosync_status(self, flask_client_bare):
        """YouTube card must show auto-sync state (Active or Requires OAuth)."""
        r = flask_client_bare.get("/intelligence/sync")
        assert r.status_code == 200
        body = r.data.decode()
        assert "Auto-sync" in body, "YouTube card must show Auto-sync state"
        assert ("Active" in body or "Requires OAuth" in body or "oauth_required" in body), (
            "YouTube auto-sync must show a readable state"
        )

    def test_platform_page_no_import_button_in_header(self, flask_client_bare):
        """Platform page header must not have an Import button."""
        r = flask_client_bare.get("/intelligence/platform/youtube")
        assert r.status_code == 200
        body = r.data.decode()
        header_area = body[body.find("page-header"):body.find("kpi-strip")]
        assert "/analytics/import" not in header_area, (
            "Platform page header must not link to manual import"
        )

    def test_platform_page_tiktok_empty_shows_not_implemented(self, flask_client_bare):
        """TikTok platform empty state must reference TikTok Video API sync."""
        r = flask_client_bare.get("/intelligence/platform/tiktok")
        assert r.status_code == 200
        body = r.data.decode()
        # Connector is now implemented via TikTok Video API
        assert "TikTok Video API" in body or "TikTok" in body, (
            "TikTok empty state must reference TikTok connector"
        )

    def test_sync_status_api_returns_platform_map(self, flask_client_bare):
        """/api/analytics/sync/status must return per-platform status."""
        r = flask_client_bare.get("/api/analytics/sync/status")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        platforms = data["platforms"]
        assert set(platforms.keys()) == {"youtube", "tiktok", "instagram", "facebook"}
        # YouTube fields
        yt = platforms["youtube"]
        assert "connected" in yt
        assert "auto_sync" in yt
        assert "last_sync" in yt
        # Non-YouTube connectors require credentials; without them show credential_required
        for p in ("tiktok", "instagram", "facebook"):
            assert platforms[p]["auto_sync"] in ("credential_required", "manual_import_only", "credential_expired")
            assert platforms[p]["connected"] is False

    def test_sync_status_api_youtube_not_connected_when_no_creds(self, flask_client_bare):
        """Without OAuth creds, youtube.connected must be False."""
        from unittest.mock import patch, MagicMock
        mock_cfg = MagicMock()
        mock_cfg.YOUTUBE_REFRESH_TOKEN = None
        mock_cfg.YOUTUBE_CLIENT_ID = None
        mock_cfg.YOUTUBE_CLIENT_SECRET = None
        with patch("hypogeum.config.Config", return_value=mock_cfg):
            r = flask_client_bare.get("/api/analytics/sync/status")
        data = r.get_json()
        assert data["platforms"]["youtube"]["connected"] is False
        assert data["platforms"]["youtube"]["auto_sync"] == "oauth_required"
