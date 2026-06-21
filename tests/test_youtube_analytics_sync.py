"""
tests/test_youtube_analytics_sync.py — YouTube Analytics API sync tests

Coverage:
  TestFetchVideoAnalytics
    test_normalises_api_response_to_schema_fields
    test_returns_empty_dict_when_no_rows
    test_avg_view_percentage_divided_by_100
    test_estimated_minutes_converted_to_seconds
    test_video_id_included_in_metrics
    test_raises_value_error_on_api_error
    test_raises_runtime_error_on_network_failure

  TestSyncYouTubeAnalytics
    test_stores_api_sourced_metrics_in_db
    test_skips_episodes_with_no_video_id
    test_returns_error_when_token_refresh_fails
    test_cleans_video_id_with_timestamp_suffix
    test_records_source_as_api_not_manual
    test_empty_metrics_response_counted_as_skipped

  TestGetLastYouTubeSync
    test_returns_none_when_no_api_records
    test_returns_most_recent_imported_at

  TestSyncRoute
    test_sync_route_returns_400_when_not_connected
    test_sync_route_calls_sync_function
    test_sync_route_returns_result

  TestImportPageYouTubeState
    test_import_page_shows_sync_button_when_connected
    test_import_page_shows_connect_link_when_not_connected
    test_import_page_still_shows_manual_import_when_connected

No real network calls. No real API keys.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.db import initialize_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def client(db):
    import run_dashboard as server
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db)
    with server.app.test_client() as c:
        yield c


def _fake_analytics_response(video_id="abc123", views=5000,
                               avg_dur=240.0, avg_pct=45.0,
                               minutes=20000, likes=120,
                               comments=15, shares=8,
                               subs_gained=22):
    """Build a minimal YouTube Analytics API v2 response."""
    return {
        "columnHeaders": [
            {"name": "video"},
            {"name": "views"},
            {"name": "estimatedMinutesWatched"},
            {"name": "averageViewDuration"},
            {"name": "averageViewPercentage"},
            {"name": "likes"},
            {"name": "comments"},
            {"name": "shares"},
            {"name": "subscribersGained"},
        ],
        "rows": [[
            video_id,
            views,
            minutes,
            avg_dur,
            avg_pct,
            likes,
            comments,
            shares,
            subs_gained,
        ]],
    }


def _mock_requests_get(response_data, status_code=200):
    resp = MagicMock()
    resp.ok = (status_code < 400)
    resp.status_code = status_code
    resp.json.return_value = response_data
    resp.text = json.dumps(response_data)
    return resp


def _make_config_with_youtube(**overrides):
    """Return a MagicMock Config with YouTube credentials set."""
    cfg = MagicMock()
    cfg.YOUTUBE_CLIENT_ID = overrides.get("YOUTUBE_CLIENT_ID", "client-id")
    cfg.YOUTUBE_CLIENT_SECRET = overrides.get("YOUTUBE_CLIENT_SECRET", "client-secret")
    cfg.YOUTUBE_REFRESH_TOKEN = overrides.get("YOUTUBE_REFRESH_TOKEN", "refresh-token")
    cfg.YOUTUBE_CHANNEL_ID = overrides.get("YOUTUBE_CHANNEL_ID", "UC123")
    return cfg


# ===========================================================================
# TestFetchVideoAnalytics
# ===========================================================================

class TestFetchVideoAnalytics:

    def test_normalises_api_response_to_schema_fields(self):
        from hypogeum.youtube_analytics import fetch_video_analytics
        api_data = _fake_analytics_response(video_id="abc123", views=5000)
        mock_resp = _mock_requests_get(api_data)

        with patch("requests.get", return_value=mock_resp):
            metrics = fetch_video_analytics("token", "abc123", "2026-01-01", "2026-01-28")

        assert metrics["views"] == 5000
        assert "avg_watch_time_s" in metrics
        assert "total_watch_time_s" in metrics
        assert "avg_view_pct" in metrics
        assert metrics["platform_video_id"] == "abc123"

    def test_returns_empty_dict_when_no_rows(self):
        from hypogeum.youtube_analytics import fetch_video_analytics
        api_data = {"columnHeaders": [], "rows": []}
        mock_resp = _mock_requests_get(api_data)

        with patch("requests.get", return_value=mock_resp):
            metrics = fetch_video_analytics("token", "abc123", "2026-01-01", "2026-01-28")

        assert metrics == {}

    def test_avg_view_percentage_divided_by_100(self):
        from hypogeum.youtube_analytics import fetch_video_analytics
        api_data = _fake_analytics_response(avg_pct=52.0)
        mock_resp = _mock_requests_get(api_data)

        with patch("requests.get", return_value=mock_resp):
            metrics = fetch_video_analytics("token", "v1", "2026-01-01", "2026-01-28")

        # API returns 0–100; schema stores 0–1
        assert metrics["avg_view_pct"] == pytest.approx(0.52, abs=1e-5)

    def test_estimated_minutes_converted_to_seconds(self):
        from hypogeum.youtube_analytics import fetch_video_analytics
        # 100 minutes watched → 6000 seconds
        api_data = _fake_analytics_response(minutes=100)
        mock_resp = _mock_requests_get(api_data)

        with patch("requests.get", return_value=mock_resp):
            metrics = fetch_video_analytics("token", "v1", "2026-01-01", "2026-01-28")

        assert metrics["total_watch_time_s"] == pytest.approx(6000.0, abs=1.0)

    def test_video_id_included_in_metrics(self):
        from hypogeum.youtube_analytics import fetch_video_analytics
        api_data = _fake_analytics_response(video_id="xyz789")
        mock_resp = _mock_requests_get(api_data)

        with patch("requests.get", return_value=mock_resp):
            metrics = fetch_video_analytics("token", "xyz789", "2026-01-01", "2026-01-28")

        assert metrics["platform_video_id"] == "xyz789"

    def test_raises_value_error_on_api_error(self):
        from hypogeum.youtube_analytics import fetch_video_analytics
        mock_resp = _mock_requests_get(
            {"error": {"message": "insufficient permissions"}}, status_code=403
        )

        with patch("requests.get", return_value=mock_resp):
            with pytest.raises(ValueError, match="403"):
                fetch_video_analytics("bad-token", "v1", "2026-01-01", "2026-01-28")

    def test_raises_runtime_error_on_network_failure(self):
        import requests as _req
        from hypogeum.youtube_analytics import fetch_video_analytics

        with patch("requests.get", side_effect=_req.ConnectionError("timeout")):
            with pytest.raises(RuntimeError, match="network error"):
                fetch_video_analytics("token", "v1", "2026-01-01", "2026-01-28")


# ===========================================================================
# TestSyncYouTubeAnalytics
# ===========================================================================

class TestSyncYouTubeAnalytics:

    def _insert_publishing_outcome(self, db, episode_id, video_id):
        """Insert a successful YouTube publishing outcome."""
        from hypogeum.db import get_connection
        conn = get_connection(db)
        conn.execute(
            """INSERT OR REPLACE INTO publishing_outcomes
               (outcome_id, episode_id, platform, publishing_status,
                platform_video_id, recorded_at, metadata_version)
               VALUES (?,?,?,?,?,?,?)""",
            (f"o-{episode_id}", episode_id, "youtube", "success",
             video_id, datetime.now(timezone.utc).isoformat(), "v1"),
        )
        conn.commit()
        conn.close()

    def test_stores_api_sourced_metrics_in_db(self, db):
        from hypogeum.youtube_analytics import sync_youtube_analytics
        from hypogeum.analytics_store import list_analytics
        cfg = _make_config_with_youtube()
        self._insert_publishing_outcome(db, "ep_001", "vid001")

        api_data = _fake_analytics_response(video_id="vid001", views=8000)

        with patch("hypogeum.youtube_analytics.refresh_youtube_token",
                   return_value="access-token"), \
             patch("requests.get", return_value=_mock_requests_get(api_data)):
            result = sync_youtube_analytics(cfg, db_path=db)

        assert result["ok"] is True
        assert result["synced_count"] == 1
        assert "ep_001" in result["episodes"]

        rows = list_analytics(platform="youtube", db_path=db)
        assert len(rows) >= 1
        assert rows[0]["views"] == 8000
        assert rows[0]["source"] == "api"

    def test_skips_episodes_with_no_video_id(self, db):
        from hypogeum.youtube_analytics import sync_youtube_analytics
        from hypogeum.db import get_connection
        cfg = _make_config_with_youtube()

        # Insert outcome with no video ID
        conn = get_connection(db)
        conn.execute(
            """INSERT OR REPLACE INTO publishing_outcomes
               (outcome_id, episode_id, platform, publishing_status,
                platform_video_id, recorded_at, metadata_version)
               VALUES (?,?,?,?,?,?,?)""",
            ("o-ep-novidid", "ep_novidid", "youtube", "success",
             "", datetime.now(timezone.utc).isoformat(), "v1"),
        )
        conn.commit()
        conn.close()

        with patch("hypogeum.youtube_analytics.refresh_youtube_token",
                   return_value="access-token"):
            result = sync_youtube_analytics(cfg, db_path=db)

        assert result["ok"] is True
        assert result["synced_count"] == 0
        assert result["skipped_count"] == 1

    def test_returns_error_when_token_refresh_fails(self, db):
        from hypogeum.youtube_analytics import sync_youtube_analytics
        from hypogeum.publishing.retry import HardFailureError
        cfg = _make_config_with_youtube()

        with patch("hypogeum.youtube_analytics.refresh_youtube_token",
                   side_effect=HardFailureError("bad credentials", status_code=401)):
            result = sync_youtube_analytics(cfg, db_path=db)

        assert result["ok"] is False
        assert "Token refresh failed" in result["errors"][0]
        assert result["synced_count"] == 0

    def test_cleans_video_id_with_timestamp_suffix(self, db):
        from hypogeum.youtube_analytics import sync_youtube_analytics
        cfg = _make_config_with_youtube()
        # Insert a video ID with the &t=400s timestamp suffix we saw in real data
        self._insert_publishing_outcome(db, "ep_ts", "WVbs9rMuQvA&t=400s")

        api_data = _fake_analytics_response(video_id="WVbs9rMuQvA", views=1200)
        captured_params = {}

        original_get = __import__("requests").get

        def _mock_get(url, headers=None, params=None, **kw):
            captured_params.update(params or {})
            return _mock_requests_get(api_data)

        with patch("hypogeum.youtube_analytics.refresh_youtube_token",
                   return_value="access-token"), \
             patch("requests.get", side_effect=_mock_get):
            result = sync_youtube_analytics(cfg, db_path=db)

        # The filters param must use the clean video ID, not the one with &t=
        assert "WVbs9rMuQvA&t=400s" not in captured_params.get("filters", "")
        assert "WVbs9rMuQvA" in captured_params.get("filters", "")
        assert result["synced_count"] == 1

    def test_records_source_as_api_not_manual(self, db):
        from hypogeum.youtube_analytics import sync_youtube_analytics
        from hypogeum.analytics_store import list_analytics
        cfg = _make_config_with_youtube()
        self._insert_publishing_outcome(db, "ep_src", "srcvid")

        with patch("hypogeum.youtube_analytics.refresh_youtube_token",
                   return_value="access-token"), \
             patch("requests.get",
                   return_value=_mock_requests_get(
                       _fake_analytics_response(video_id="srcvid"))):
            sync_youtube_analytics(cfg, db_path=db)

        rows = list_analytics(platform="youtube", db_path=db)
        assert any(r["source"] == "api" for r in rows), (
            "Synced analytics must be stored with source='api', not 'manual'. "
            "This prevents it from being treated as demo/mock data."
        )

    def test_empty_metrics_response_counted_as_skipped(self, db):
        from hypogeum.youtube_analytics import sync_youtube_analytics
        cfg = _make_config_with_youtube()
        self._insert_publishing_outcome(db, "ep_new", "newvid")

        empty_response = {"columnHeaders": [], "rows": []}

        with patch("hypogeum.youtube_analytics.refresh_youtube_token",
                   return_value="access-token"), \
             patch("requests.get", return_value=_mock_requests_get(empty_response)):
            result = sync_youtube_analytics(cfg, db_path=db)

        assert result["synced_count"] == 0
        assert result["skipped_count"] == 1
        assert result["ok"] is True  # not an error — video just has no data yet


# ===========================================================================
# TestGetLastYouTubeSync
# ===========================================================================

class TestGetLastYouTubeSync:

    def test_returns_none_when_no_api_records(self, db):
        from hypogeum.youtube_analytics import get_last_youtube_sync
        result = get_last_youtube_sync(db_path=db)
        assert result is None

    def test_returns_most_recent_imported_at(self, db):
        from hypogeum.youtube_analytics import get_last_youtube_sync
        from hypogeum.analytics_store import store_platform_metrics

        store_platform_metrics(
            episode_id="ep_sync_test",
            platform="youtube",
            metrics={"views": 100},
            source="api",
            snapshot_date="2026-06-21",
            db_path=db,
        )

        result = get_last_youtube_sync(db_path=db)
        assert result is not None
        assert "2026" in result


# ===========================================================================
# TestSyncRoute
# ===========================================================================

class TestSyncRoute:

    def test_sync_route_returns_400_when_not_connected(self, client):
        with patch("hypogeum.config.Config") as MockCfg:
            inst = MockCfg.return_value
            inst.YOUTUBE_REFRESH_TOKEN = None
            inst.YOUTUBE_CLIENT_ID = "id"
            inst.YOUTUBE_CLIENT_SECRET = "secret"
            resp = client.post(
                "/api/analytics/sync/youtube",
                json={},
                content_type="application/json",
            )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert "not connected" in data["error"].lower() or "YOUTUBE_REFRESH_TOKEN" in data["error"]

    def test_sync_route_calls_sync_function(self, client):
        fake_result = {
            "ok": True, "synced_count": 2, "skipped_count": 0,
            "errors": [], "synced_at": "2026-06-21T18:00:00+00:00",
            "episodes": ["ep_001", "ep_002"],
        }
        with patch("hypogeum.config.Config") as MockCfg, \
             patch("hypogeum.youtube_analytics.sync_youtube_analytics",
                   return_value=fake_result) as mock_sync:
            inst = MockCfg.return_value
            inst.YOUTUBE_REFRESH_TOKEN = "tok"
            inst.YOUTUBE_CLIENT_ID = "cid"
            inst.YOUTUBE_CLIENT_SECRET = "csec"
            resp = client.post(
                "/api/analytics/sync/youtube",
                json={"window_days": 14},
                content_type="application/json",
            )

        assert resp.status_code == 200
        mock_sync.assert_called_once()
        call_kw = mock_sync.call_args[1]
        assert call_kw.get("window_days") == 14

    def test_sync_route_returns_result(self, client):
        fake_result = {
            "ok": True, "synced_count": 1, "skipped_count": 0,
            "errors": [], "synced_at": "2026-06-21T18:00:00+00:00",
            "episodes": ["ep_001"],
        }
        with patch("hypogeum.config.Config") as MockCfg, \
             patch("hypogeum.youtube_analytics.sync_youtube_analytics",
                   return_value=fake_result):
            inst = MockCfg.return_value
            inst.YOUTUBE_REFRESH_TOKEN = "tok"
            inst.YOUTUBE_CLIENT_ID = "cid"
            inst.YOUTUBE_CLIENT_SECRET = "csec"
            resp = client.post(
                "/api/analytics/sync/youtube",
                json={},
                content_type="application/json",
            )

        data = resp.get_json()
        assert data["ok"] is True
        assert data["synced_count"] == 1
        assert "ep_001" in data["episodes"]


# ===========================================================================
# TestImportPageYouTubeState
# ===========================================================================

class TestImportPageYouTubeState:

    def test_import_page_shows_sync_button_when_connected(self, client):
        with patch("hypogeum.config.Config") as MockCfg, \
             patch("hypogeum.youtube_analytics.get_last_youtube_sync", return_value=None):
            inst = MockCfg.return_value
            inst.YOUTUBE_REFRESH_TOKEN = "tok"
            inst.YOUTUBE_CLIENT_ID = "cid"
            inst.YOUTUBE_CLIENT_SECRET = "csec"
            resp = client.get("/analytics/import")
        html = resp.data.decode()
        assert "Sync YouTube Analytics" in html, (
            "Sync button must appear on /analytics/import when YouTube is connected."
        )
        assert "syncYouTube" in html, (
            "syncYouTube JS function must be present when YouTube is connected."
        )

    def test_import_page_shows_connect_link_when_not_connected(self, client):
        with patch("hypogeum.config.Config") as MockCfg, \
             patch("hypogeum.youtube_analytics.get_last_youtube_sync", return_value=None):
            inst = MockCfg.return_value
            inst.YOUTUBE_REFRESH_TOKEN = None
            inst.YOUTUBE_CLIENT_ID = "cid"
            inst.YOUTUBE_CLIENT_SECRET = "csec"
            resp = client.get("/analytics/import")
        html = resp.data.decode()
        assert "/platforms/connect/youtube" in html, (
            "Connect link must appear when YouTube is not connected."
        )

    def test_import_page_still_shows_manual_import_when_connected(self, client):
        """Manual import must always be visible — even when YouTube sync is available."""
        with patch("hypogeum.config.Config") as MockCfg, \
             patch("hypogeum.youtube_analytics.get_last_youtube_sync", return_value=None):
            inst = MockCfg.return_value
            inst.YOUTUBE_REFRESH_TOKEN = "tok"
            inst.YOUTUBE_CLIENT_ID = "cid"
            inst.YOUTUBE_CLIENT_SECRET = "csec"
            resp = client.get("/analytics/import")
        html = resp.data.decode()
        assert "Manual Import" in html, (
            "Manual Import section must remain visible even when YouTube is connected."
        )
        assert 'action="/analytics/import"' in html, (
            "Manual import form must still be present when YouTube is connected."
        )
