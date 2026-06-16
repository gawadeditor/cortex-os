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
        r = flask_client.get("/analytics")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "Analytics" in text

    def test_analytics_page_shows_platforms(self, flask_client):
        r = flask_client.get("/analytics")
        text = r.get_data(as_text=True)
        for p in ("youtube", "tiktok", "instagram", "facebook"):
            assert p in text.lower()

    def test_analytics_page_empty_state_no_crash(self, flask_client_bare):
        r = flask_client_bare.get("/analytics")
        assert r.status_code == 200

    def test_analytics_platform_youtube_200(self, flask_client):
        r = flask_client.get("/analytics/platform/youtube")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "youtube" in text.lower()

    def test_analytics_platform_tiktok_200(self, flask_client):
        r = flask_client.get("/analytics/platform/tiktok")
        assert r.status_code == 200

    def test_analytics_platform_instagram_200(self, flask_client):
        r = flask_client.get("/analytics/platform/instagram")
        assert r.status_code == 200

    def test_analytics_platform_facebook_200(self, flask_client):
        r = flask_client.get("/analytics/platform/facebook")
        assert r.status_code == 200

    def test_analytics_platform_unknown_400(self, flask_client):
        r = flask_client.get("/analytics/platform/snapchat")
        assert r.status_code == 404

    def test_analytics_episode_200(self, flask_client):
        r = flask_client.get("/analytics/episode/ep_alpha")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "ep_alpha" in text

    def test_analytics_episode_no_data_404(self, flask_client):
        r = flask_client.get("/analytics/episode/no-such-episode")
        assert r.status_code == 404

    def test_analytics_import_page_200(self, flask_client):
        r = flask_client.get("/analytics/import")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "Import" in text
        assert "mock" in text.lower() or "Generate" in text

    def test_analytics_import_shows_platform_select(self, flask_client):
        r = flask_client.get("/analytics/import")
        text = r.get_data(as_text=True)
        assert "youtube" in text.lower()

    def test_learning_page_200(self, flask_client):
        r = flask_client.get("/learning")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "Intelligence" in text

    def test_learning_page_shows_actions(self, flask_client):
        r = flask_client.get("/learning")
        text = r.get_data(as_text=True)
        # Should show pending actions or empty state
        assert "action" in text.lower() or "No pending" in text

    def test_learning_page_shows_platform_coverage(self, flask_client):
        r = flask_client.get("/learning")
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
        assert "/analytics" in text
        assert "/learning"  in text

    def test_dashboard_has_analytics_nav(self, flask_client):
        r = flask_client.get("/")
        self._check_nav(r.get_data(as_text=True))

    def test_analytics_page_has_analytics_nav(self, flask_client):
        r = flask_client.get("/analytics")
        self._check_nav(r.get_data(as_text=True))

    def test_learning_page_has_analytics_nav(self, flask_client):
        r = flask_client.get("/learning")
        self._check_nav(r.get_data(as_text=True))

    def test_runs_page_has_analytics_nav(self, flask_client):
        r = flask_client.get("/runs")
        self._check_nav(r.get_data(as_text=True))


# ===========================================================================
# TestNoCredentialsRequired
# ===========================================================================

class TestNoCredentialsRequired:
    """All analytics routes must work with zero env vars / credentials."""

    def test_analytics_overview_no_credentials(self, flask_client_bare):
        r = flask_client_bare.get("/analytics")
        assert r.status_code == 200

    def test_platform_pages_no_credentials(self, flask_client_bare):
        for p in ("youtube", "tiktok", "instagram", "facebook"):
            r = flask_client_bare.get(f"/analytics/platform/{p}")
            assert r.status_code == 200, f"Platform {p} failed"

    def test_import_page_no_credentials(self, flask_client_bare):
        r = flask_client_bare.get("/analytics/import")
        assert r.status_code == 200

    def test_learning_page_no_credentials(self, flask_client_bare):
        r = flask_client_bare.get("/learning")
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
