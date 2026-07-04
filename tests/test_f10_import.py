"""
tests/test_f10_import.py — F10: Manual Analytics and Publishing Import Pipeline

Coverage:
    TestManualAnalyticsImport       — POST /api/analytics/import/manual inserts source='manual'
    TestCSVAnalyticsImport          — POST /api/analytics/import/csv inserts source='csv'
    TestDashboardKPISourceFiltering — dashboard KPI ignores source='mock', includes manual+csv
    TestPublishingImport            — POST /api/published/import/manual creates publishing_outcomes row
    TestLatestEpisodeFromPublishing — dashboard shows episode from publishing_outcomes
    TestInvalidCSV                  — invalid CSV fails safely (no crash, returns error)
    TestNoExternalCalls             — no socket connections made by any F10 route
    TestPublishedImportPage         — GET /published/import renders without errors
    TestPlatformConnectRedirect     — platform connect save redirects to /dashboard
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import initialize_schema, get_connection
from hypogeum.analytics_store import (
    seed_mock_analytics,
    store_platform_metrics,
    list_analytics,
)
from hypogeum.platform_connections import detect_real_vs_mock_analytics
from hypogeum.dashboard_executive import get_executive_overview


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "f10_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def flask_client(db_path):
    import run_dashboard as _mod
    _mod.app.config["TESTING"] = True
    _mod.app.config["DB_PATH"] = str(db_path)
    with _mod.app.test_client() as client:
        yield client


def _fake_state():
    from hypogeum.dashboard_api import (
        FactoryState, RunHealthSummary, ArchiveSummary,
        RecallSummary, LearningSummary, CostSummary, AuditSummary,
    )
    return FactoryState(
        factory_id="test", schema_version="v2",
        checked_at="2026-01-01T00:00:00+00:00",
        overall_health="healthy",
        run_health=RunHealthSummary(0, 0, 0, 0, 1.0, None),
        channels=[], agent_health=[], model_usage=[], provider_health=[],
        archive=ArchiveSummary(0, 0, {}, None, None),
        recall=RecallSummary(0, 0, 0, 0.0),
        learning=LearningSummary(0, {}, []),
        costs=CostSummary(0.0, {}, {}, {}, 0, 0),
        audit=AuditSummary("2026-01-01T00:00:00", True, 0, 0, "ACCEPTED"),
        notifications=[], factory_name="Test", factory_status="active",
        active_signals=[], schedules=[], pending_approvals=[],
    )


def _fake_report():
    from hypogeum.factory_director import FactoryDirectorReport, RunClearanceDecision
    return FactoryDirectorReport(
        factory_id="test", factory_name="Test",
        readiness_level="READY", readiness_score=100, can_run=True,
        assessed_at="2026-01-01T00:00:00", domains=[],
        blocking_reasons=[], warnings=[],
        run_clearance=RunClearanceDecision(
            cleared=True, reason="ok", blocking_checks=[],
            blocking_signals=[], pending_approvals=0,
            checked_at="2026-01-01T00:00:00",
        ),
    )


def _dashboard_client(db_path):
    """Flask client with patched factory state for /dashboard rendering."""
    import run_dashboard as _mod
    _mod.app.config["TESTING"] = True
    _mod.app.config["DB_PATH"] = str(db_path)
    return _mod.app.test_client()


# ===========================================================================
# TestManualAnalyticsImport
# ===========================================================================

class TestManualAnalyticsImport:
    def test_inserts_source_manual(self, flask_client, db_path):
        payload = {
            "episode_id": "ep_manual_test",
            "platform":   "youtube",
            "views":      15000,
            "impressions": 120000,
            "ctr":        0.048,
            "avg_view_pct": 0.52,
            "watch_time": 4680000,
            "likes":      450,
            "comments":   38,
            "shares":     72,
            "platform_video_id": "dQw4w9WgXcQ",
        }
        resp = flask_client.post(
            "/api/analytics/import/manual",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True

        rows = list_analytics(db_path=db_path)
        manual_rows = [r for r in rows if r["episode_id"] == "ep_manual_test"]
        assert len(manual_rows) >= 1
        assert manual_rows[0]["source"] == "manual"

    def test_watch_time_maps_to_total_watch_time_s(self, flask_client, db_path):
        payload = {
            "episode_id": "ep_wt_test",
            "platform":   "youtube",
            "views":      5000,
            "watch_time": 1000000,
        }
        resp = flask_client.post(
            "/api/analytics/import/manual",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 200
        rows = list_analytics(db_path=db_path)
        row = next((r for r in rows if r["episode_id"] == "ep_wt_test"), None)
        assert row is not None
        assert row["total_watch_time_s"] == 1000000.0
        assert row["source"] == "manual"

    def test_missing_episode_id_returns_400(self, flask_client):
        resp = flask_client.post(
            "/api/analytics/import/manual",
            data=json.dumps({"platform": "youtube", "views": 100}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_invalid_platform_returns_400(self, flask_client):
        resp = flask_client.post(
            "/api/analytics/import/manual",
            data=json.dumps({"episode_id": "ep_x", "platform": "notaplatform", "views": 100}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_source_never_mock(self, flask_client, db_path):
        payload = {"episode_id": "ep_src_check", "platform": "tiktok", "views": 999}
        flask_client.post(
            "/api/analytics/import/manual",
            data=json.dumps(payload),
            content_type="application/json",
        )
        rows = list_analytics(db_path=db_path)
        row = next((r for r in rows if r["episode_id"] == "ep_src_check"), None)
        assert row is not None
        assert row["source"] != "mock"


# ===========================================================================
# TestCSVAnalyticsImport
# ===========================================================================

class TestCSVAnalyticsImport:
    _VALID_CSV = (
        "Views,Impressions,Impressions click-through rate (%),Average view percentage (%)\n"
        "12500,95000,4.2,51.3\n"
    )

    def test_inserts_source_csv(self, flask_client, db_path):
        payload = {
            "episode_id": "ep_csv_test",
            "platform":   "youtube",
            "csv_text":   self._VALID_CSV,
        }
        resp = flask_client.post(
            "/api/analytics/import/csv",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body.get("imported_count", 0) >= 1

        rows = list_analytics(db_path=db_path)
        csv_rows = [r for r in rows if r["episode_id"] == "ep_csv_test"]
        assert len(csv_rows) >= 1
        assert all(r["source"] == "csv" for r in csv_rows)

    def test_source_never_mock(self, flask_client, db_path):
        payload = {
            "episode_id": "ep_csv_src",
            "platform":   "youtube",
            "csv_text":   "Views\n5000\n",
        }
        flask_client.post(
            "/api/analytics/import/csv",
            data=json.dumps(payload),
            content_type="application/json",
        )
        rows = list_analytics(db_path=db_path)
        row = next((r for r in rows if r["episode_id"] == "ep_csv_src"), None)
        assert row is not None
        assert row["source"] == "csv"

    def test_invalid_csv_fails_safely(self, flask_client):
        payload = {
            "episode_id": "ep_bad_csv",
            "platform":   "youtube",
            "csv_text":   "not,a,valid,csv,export\nno,recognizable,columns,here,x\n",
        }
        resp = flask_client.post(
            "/api/analytics/import/csv",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code in (200, 400)
        body = resp.get_json()
        assert "ok" in body

    def test_empty_csv_returns_400(self, flask_client):
        payload = {"episode_id": "ep_empty", "platform": "youtube", "csv_text": ""}
        resp = flask_client.post(
            "/api/analytics/import/csv",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_missing_episode_id_returns_400(self, flask_client):
        resp = flask_client.post(
            "/api/analytics/import/csv",
            data=json.dumps({"platform": "youtube", "csv_text": "Views\n100\n"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_invalid_platform_returns_400(self, flask_client):
        resp = flask_client.post(
            "/api/analytics/import/csv",
            data=json.dumps({"episode_id": "ep_x", "platform": "snapchat",
                             "csv_text": "Views\n100\n"}),
            content_type="application/json",
        )
        assert resp.status_code == 400


# ===========================================================================
# TestDashboardKPISourceFiltering
# ===========================================================================

class TestDashboardKPISourceFiltering:
    def test_mock_excluded_from_real_data_detection(self, db_path):
        seed_mock_analytics("ep_kpi_test", "youtube", db_path=db_path)
        detection = detect_real_vs_mock_analytics(db_path=db_path)
        assert detection["by_platform"]["youtube"]["data_state"] == "mock_only"
        assert detection["has_any_real"] is False

    def test_manual_source_counts_as_real(self, db_path):
        store_platform_metrics(
            "ep_real_test", "youtube",
            {"views": 5000, "ctr": 0.04},
            source="manual",
            db_path=db_path,
        )
        detection = detect_real_vs_mock_analytics(db_path=db_path)
        assert detection["has_any_real"] is True
        assert detection["by_platform"]["youtube"]["data_state"] == "real"

    def test_csv_source_counts_as_real(self, db_path):
        store_platform_metrics(
            "ep_csv_real", "tiktok",
            {"views": 3000},
            source="csv",
            db_path=db_path,
        )
        detection = detect_real_vs_mock_analytics(db_path=db_path)
        assert detection["has_any_real"] is True
        assert detection["by_platform"]["tiktok"]["data_state"] == "real"

    def test_mock_and_manual_coexist(self, db_path):
        seed_mock_analytics("ep_coexist", "youtube", db_path=db_path)
        store_platform_metrics(
            "ep_real_coexist", "youtube",
            {"views": 7000},
            source="manual",
            db_path=db_path,
        )
        detection = detect_real_vs_mock_analytics(db_path=db_path)
        assert detection["has_any_real"] is True
        assert detection["has_any_mock"] is True

    def test_executive_analytics_excludes_mock(self, db_path):
        seed_mock_analytics("ep_exec_mock", "youtube", db_path=db_path)
        overview = get_executive_overview(db_path=db_path)
        assert overview["analytics"]["views"] is None

    def test_executive_analytics_includes_manual(self, db_path):
        store_platform_metrics(
            "ep_exec_manual", "youtube",
            {"views": 9999},
            source="manual",
            db_path=db_path,
        )
        # Insert into episodes table so the episode appears
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO episodes (episode_id, title, status, created_at)"
                " VALUES (?,?,?,?)",
                ["ep_exec_manual", "Manual Test", "complete", "2026-06-17T00:00:00"],
            )
            conn.commit()
        finally:
            conn.close()
        overview = get_executive_overview(db_path=db_path)
        assert overview["analytics"]["views"] == 9999

    def test_dashboard_kpi_api_includes_manual_not_mock(self, flask_client, db_path):
        seed_mock_analytics("ep_kpi_mock", "youtube", db_path=db_path)
        store_platform_metrics(
            "ep_kpi_real", "youtube",
            {"views": 12345},
            source="manual",
            db_path=db_path,
        )
        resp = flask_client.get("/api/platform-connections")
        assert resp.status_code == 200
        data = resp.get_json()
        analytics_detail = data["analytics_integrity"]
        assert analytics_detail["has_any_real"] is True
        assert analytics_detail["has_any_mock"] is True


# ===========================================================================
# TestPublishingImport
# ===========================================================================

class TestPublishingImport:
    def test_creates_publishing_outcomes_row(self, flask_client, db_path):
        payload = {
            "episode_id":       "ep_pub_test",
            "platform":         "youtube",
            "publishing_status": "success",
            "platform_video_id": "abc123",
            "published_at":     "2026-06-17T10:00:00",
            "url":              "https://youtube.com/watch?v=abc123",
        }
        resp = flask_client.post(
            "/api/published/import/manual",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert "outcome_id" in body

        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM publishing_outcomes WHERE episode_id=? AND platform=?",
                ["ep_pub_test", "youtube"],
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["platform_video_id"] == "abc123"
        assert row["publishing_status"] == "success"
        assert row["url"] == "https://youtube.com/watch?v=abc123"

    def test_upserts_on_same_episode_platform(self, flask_client, db_path):
        base_payload = {
            "episode_id": "ep_upsert",
            "platform":   "tiktok",
            "publishing_status": "pending",
        }
        flask_client.post(
            "/api/published/import/manual",
            data=json.dumps(base_payload),
            content_type="application/json",
        )
        updated_payload = {**base_payload, "publishing_status": "success",
                           "platform_video_id": "tiktok_vid_1"}
        flask_client.post(
            "/api/published/import/manual",
            data=json.dumps(updated_payload),
            content_type="application/json",
        )
        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM publishing_outcomes WHERE episode_id='ep_upsert' AND platform='tiktok'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0]["publishing_status"] == "success"

    def test_missing_episode_id_returns_400(self, flask_client):
        resp = flask_client.post(
            "/api/published/import/manual",
            data=json.dumps({"platform": "youtube", "publishing_status": "success"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_invalid_platform_returns_400(self, flask_client):
        resp = flask_client.post(
            "/api/published/import/manual",
            data=json.dumps({"episode_id": "ep_x", "platform": "twitch",
                             "publishing_status": "success"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_invalid_status_returns_400(self, flask_client):
        resp = flask_client.post(
            "/api/published/import/manual",
            data=json.dumps({"episode_id": "ep_x", "platform": "youtube",
                             "publishing_status": "published"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_url_column_stored(self, flask_client, db_path):
        payload = {
            "episode_id": "ep_url_test",
            "platform":   "instagram",
            "publishing_status": "success",
            "url":        "https://instagram.com/reel/abc",
        }
        flask_client.post(
            "/api/published/import/manual",
            data=json.dumps(payload),
            content_type="application/json",
        )
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT url FROM publishing_outcomes WHERE episode_id='ep_url_test'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["url"] == "https://instagram.com/reel/abc"


# ===========================================================================
# TestLatestEpisodeFromPublishingOutcomes
# ===========================================================================

class TestLatestEpisodeFromPublishingOutcomes:
    def test_episode_from_publishing_appears_on_dashboard(self, db_path):
        conn = get_connection(db_path)
        import uuid
        from datetime import datetime, timezone
        outcome_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        try:
            conn.execute(
                """INSERT INTO publishing_outcomes
                   (outcome_id, episode_id, platform, publishing_status,
                    platform_video_id, url, published_at, recorded_at, metadata_version)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (outcome_id, "ep_pub_only", "youtube", "success",
                 "vid_001", "https://youtube.com/watch?v=vid_001",
                 now, now, "v1"),
            )
            conn.commit()
        finally:
            conn.close()

        overview = get_executive_overview(db_path=db_path)
        ep_ids = [ep.get("title", "") for ep in overview["recent_episodes"]]
        assert any("ep_pub_only" in (ep.get("title") or "") or
                   ep.get("platform_video_id") == "vid_001"
                   for ep in overview["recent_episodes"])

    def test_platform_video_id_in_recent_episodes(self, db_path):
        conn = get_connection(db_path)
        import uuid
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO episodes (episode_id, title, status, created_at)
                   VALUES (?,?,?,?)""",
                ["ep_with_vid", "My Episode", "complete", now],
            )
            conn.execute(
                """INSERT OR IGNORE INTO publishing_outcomes
                   (outcome_id, episode_id, platform, publishing_status,
                    platform_video_id, url, published_at, recorded_at, metadata_version)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), "ep_with_vid", "youtube", "success",
                 "yt_video_id_1", "https://youtube.com/watch?v=yt_video_id_1",
                 now, now, "v1"),
            )
            conn.commit()
        finally:
            conn.close()

        overview = get_executive_overview(db_path=db_path)
        ep = next((e for e in overview["recent_episodes"]
                   if e.get("episode_id") == "ep_with_vid"), None)
        assert ep is not None
        assert ep.get("platform_video_id") == "yt_video_id_1"
        assert ep.get("url") == "https://youtube.com/watch?v=yt_video_id_1"

    def test_dashboard_html_shows_platform_video_id_column(self, db_path):
        import run_dashboard as _mod
        _mod.app.config["TESTING"] = True
        _mod.app.config["DB_PATH"] = str(db_path)
        # "Platform ID" column header only renders when recent_episodes is non-empty
        fake_overview = {
            "recent_episodes": [{
                "episode_id": "ep_fake", "title": "Fake Episode",
                "published_at": "2026-01-01T00:00:00", "status": "complete",
                "views": 100, "ctr": None, "retention": None,
                "platform_video_id": "vid_fake", "url": None,
            }],
            "production": {"current_episode": None, "display_title": None,
                           "current_stage": None, "progress_pct": 0, "queue_size": 0},
            "analytics": {"subscribers": None, "views": None, "watch_time_h": None,
                          "revenue_usd": None, "growth_pct": None},
            "platform_state": None, "channel_profile": None,
            "views_chart": [], "demo_warning": None,
            "factory_health": {"status_label": "OK", "active_count": 0,
                               "queued_count": 0, "failed_count": 0},
            "needs_attention": [], "primary_cta": {}, "failed_run_id": None,
        }
        with _mod.app.test_client() as client:
            with patch("run_dashboard.get_factory_state", return_value=_fake_state()):
                with patch("run_dashboard.assess_factory", return_value=_fake_report()):
                    with patch("run_dashboard.get_executive_overview",
                               return_value=fake_overview):
                        resp = client.get("/dashboard")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Platform ID" in html


# ===========================================================================
# TestPublishedImportPage
# ===========================================================================

class TestPublishedImportPage:
    def test_get_page_renders(self, flask_client):
        resp = flask_client.get("/published/import")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Import Published Video" in html
        assert "publishing_status" in html
        assert "platform_video_id" in html

    def test_form_submit_creates_row(self, flask_client, db_path):
        resp = flask_client.post(
            "/published/import",
            data={
                "episode_id":       "ep_form_pub",
                "platform":         "youtube",
                "publishing_status": "success",
                "platform_video_id": "form_vid_001",
                "published_at":     "",
                "url":              "",
                "run_id":           "",
                "failure_reason":   "",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM publishing_outcomes WHERE episode_id='ep_form_pub'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["platform_video_id"] == "form_vid_001"


# ===========================================================================
# TestNoExternalCalls
# ===========================================================================

class TestNoExternalCalls:
    def _make_client(self, db_path):
        import run_dashboard as _mod
        _mod.app.config["TESTING"] = True
        _mod.app.config["DB_PATH"] = str(db_path)
        return _mod.app.test_client()

    def test_analytics_import_manual_no_network(self, db_path):
        real_socket = socket.socket

        def assert_no_connect(*args, **kwargs):
            raise AssertionError("socket.connect called during test")

        client = self._make_client(db_path)
        with patch.object(socket.socket, "connect", assert_no_connect):
            resp = client.post(
                "/api/analytics/import/manual",
                data=json.dumps({"episode_id": "ep_net_test", "platform": "youtube",
                                 "views": 100}),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_analytics_import_csv_no_network(self, db_path):
        client = self._make_client(db_path)
        with patch.object(socket.socket, "connect",
                          side_effect=AssertionError("socket.connect called")):
            resp = client.post(
                "/api/analytics/import/csv",
                data=json.dumps({"episode_id": "ep_csv_net", "platform": "youtube",
                                 "csv_text": "Views\n500\n"}),
                content_type="application/json",
            )
        assert resp.status_code in (200, 400)

    def test_published_import_no_network(self, db_path):
        client = self._make_client(db_path)
        with patch.object(socket.socket, "connect",
                          side_effect=AssertionError("socket.connect called")):
            resp = client.post(
                "/api/published/import/manual",
                data=json.dumps({"episode_id": "ep_pub_net", "platform": "youtube",
                                 "publishing_status": "success"}),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_published_import_page_no_network(self, db_path):
        client = self._make_client(db_path)
        with patch.object(socket.socket, "connect",
                          side_effect=AssertionError("socket.connect called")):
            resp = client.get("/published/import")
        assert resp.status_code == 200


# ===========================================================================
# TestPlatformConnectRedirect
# ===========================================================================

class TestPlatformConnectRedirect:
    def test_platform_connect_save_redirects_to_dashboard(self, flask_client):
        resp = flask_client.post(
            "/platforms/connect/youtube/save",
            data={"credential": "ya29.fake_token_for_test", "label": "test"},
            follow_redirects=False,
        )
        assert resp.status_code in (301, 302)
        location = resp.headers.get("Location", "")
        assert "/dashboard" in location
