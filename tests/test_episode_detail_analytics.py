"""
tests/test_episode_detail_analytics.py — Episode Detail Analytics tab (Step 5)

Coverage:
    TestAnalyticsDataLayer   — _build_analytics_data: totals, mock exclusion,
                               platform breakdown, staleness, missing detection
    TestAnalyticsRoute       — ?tab=analytics 200 + active tab class
    TestTabIsolation         — other tabs unaffected; analytics absent elsewhere
    TestOverviewSection      — KPI values in HTML; empty-state when no real data
    TestPlatformBreakdown    — all four platforms present; per-metric rendering
    TestPublishingPerformance — published_at, URL, video ID in table
    TestAnalyticsHealth      — real/mock counts, missing, stale warnings
    TestNoSideEffects        — no network calls; no mock leakage into KPIs
"""

from __future__ import annotations

import socket
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import get_connection, initialize_schema
from hypogeum.episode_detail import get_episode_detail, _build_analytics_data


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "anl_tab_test.db"
    initialize_schema(p)
    return p


def _ep(conn, episode_id="ep_a01"):
    conn.execute(
        """INSERT INTO episodes (episode_id, title, topic, status, created_at, metadata_version)
           VALUES (?, 'Analytics Test', 'A topic', 'complete', '2026-06-09T10:00:00', 'v1')""",
        [episode_id],
    )
    conn.commit()


def _run(conn, episode_id="ep_a01"):
    rid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO runs (run_id, episode_id, started_at, status, metadata_version)
           VALUES (?, ?, '2026-06-09T10:00:00', 'complete', 'v1')""",
        [rid, episode_id],
    )
    conn.execute("UPDATE episodes SET run_id = ? WHERE episode_id = ?",
                 [rid, episode_id])
    conn.commit()
    return rid


def _analytics(conn, episode_id="ep_a01", platform="youtube",
               ctr=0.06, views=6000, impressions=100000,
               avg_view_pct=0.60, avg_watch_time_s=300.0,
               total_watch_time_s=1800000.0,
               likes=400, comments=30, shares=60, saves=None,
               source="manual",
               snapshot_date=None):
    if snapshot_date is None:
        snapshot_date = "2026-06-16"
    rid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO platform_analytics
           (record_id, episode_id, platform, snapshot_date,
            ctr, views, impressions, avg_view_pct, avg_watch_time_s,
            total_watch_time_s, likes, comments, shares, saves,
            source, imported_at, metadata_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-06-16T10:00:00', 'v1')""",
        [rid, episode_id, platform, snapshot_date,
         ctr, views, impressions, avg_view_pct, avg_watch_time_s,
         total_watch_time_s, likes, comments, shares, saves, source],
    )
    conn.commit()
    return rid


def _outcome(conn, episode_id="ep_a01", platform="youtube",
             status="success", video_id="vid123",
             url="https://youtu.be/vid123",
             published_at="2026-06-14T14:00:00"):
    oid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO publishing_outcomes
           (outcome_id, episode_id, platform, publishing_status,
            platform_video_id, url, published_at, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, '2026-06-14T14:00:00')""",
        [oid, episode_id, platform, status, video_id, url, published_at],
    )
    conn.commit()
    return oid


@pytest.fixture()
def client(db_path):
    import run_dashboard as server
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db_path)
    with server.app.test_client() as c:
        yield c, db_path


def _anl_html(client_pair, episode_id="ep_a01"):
    c, _ = client_pair
    resp = c.get(f"/episode/{episode_id}?tab=analytics")
    assert resp.status_code == 200
    return resp.data.decode()


# ===========================================================================
# TestAnalyticsDataLayer
# ===========================================================================

class TestAnalyticsDataLayer:

    def test_empty_overview_when_no_real_data(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        assert d["analytics"]["overview"]["has_real_data"] is False
        assert d["analytics"]["overview"]["total_views"] is None

    def test_real_totals_computed(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, views=5000, impressions=80000, source="manual")
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        ov = d["analytics"]["overview"]
        assert ov["has_real_data"] is True
        assert ov["total_views"] == 5000
        assert ov["total_impressions"] == 80000

    def test_mock_analytics_excluded_from_totals(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, views=99999, source="mock")
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        ov = d["analytics"]["overview"]
        assert ov["has_real_data"] is False
        assert ov["total_views"] is None

    def test_mock_count_tracked_in_health(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="mock")
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        health = d["analytics"]["health"]
        assert health["mock_row_count"] == 1
        assert health["has_mock"] is True
        assert health["real_row_count"] == 0

    def test_real_row_count_in_health(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="manual", snapshot_date="2026-06-15")
        _analytics(conn, source="api",    snapshot_date="2026-06-16")
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        assert d["analytics"]["health"]["real_row_count"] == 2

    def test_ctr_as_percentage(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.062, source="manual")
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        yt = next(p for p in d["analytics"]["platforms"] if p["platform"] == "youtube")
        assert yt["ctr_pct"] == pytest.approx(6.2, rel=0.01)

    def test_retention_as_percentage(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, avg_view_pct=0.58, source="manual")
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        yt = next(p for p in d["analytics"]["platforms"] if p["platform"] == "youtube")
        assert yt["avg_retention_pct"] == pytest.approx(58.0, rel=0.01)

    def test_watch_time_in_hours(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, total_watch_time_s=7200.0, source="manual")
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        yt = next(p for p in d["analytics"]["platforms"] if p["platform"] == "youtube")
        assert yt["total_watch_time_h"] == pytest.approx(2.0, rel=0.01)

    def test_all_four_platforms_present(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        platforms = [p["platform"] for p in d["analytics"]["platforms"]]
        for p in ("youtube", "tiktok", "instagram", "facebook"):
            assert p in platforms

    def test_platform_ordering(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        platforms = [p["platform"] for p in d["analytics"]["platforms"]]
        assert platforms == ["youtube", "tiktok", "instagram", "facebook"]

    def test_platform_with_no_data_has_data_false(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        tiktok = next(p for p in d["analytics"]["platforms"] if p["platform"] == "tiktok")
        assert tiktok["has_data"] is False

    def test_latest_snapshot_used_not_all_summed(self, db_path):
        """When two snapshots exist, use latest; don't double-count."""
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, views=3000, snapshot_date="2026-06-10", source="manual")
        _analytics(conn, views=6000, snapshot_date="2026-06-16", source="manual")
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        yt = next(p for p in d["analytics"]["platforms"] if p["platform"] == "youtube")
        assert yt["views"] == 6000  # latest, not 9000 (sum)

    def test_missing_platforms_list_when_no_data(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        health = d["analytics"]["health"]
        # All 4 platforms missing (no analytics imported)
        assert len(health["missing_platforms"]) == 4
        assert "youtube" in health["missing_platforms"]

    def test_published_platform_not_missing_when_has_data(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, platform="youtube", source="manual")
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        health = d["analytics"]["health"]
        assert "youtube" not in health["missing_platforms"]
        # Other 3 still missing
        assert len(health["missing_platforms"]) == 3

    def test_stale_platform_flagged(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        stale_date = (date.today() - timedelta(days=10)).isoformat()
        _analytics(conn, platform="youtube", source="manual", snapshot_date=stale_date)
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        health = d["analytics"]["health"]
        assert "youtube" in health["stale_platforms"]

    def test_recent_snapshot_not_stale(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        fresh_date = (date.today() - timedelta(days=2)).isoformat()
        _analytics(conn, platform="youtube", source="manual", snapshot_date=fresh_date)
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        assert "youtube" not in d["analytics"]["health"]["stale_platforms"]

    def test_published_flag_from_publishing_outcomes(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _outcome(conn, platform="youtube", status="success")
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        yt = next(p for p in d["analytics"]["platforms"] if p["platform"] == "youtube")
        assert yt["published"] is True
        assert yt["published_at"] is not None
        assert yt["url"] is not None

    def test_unpublished_platform_has_published_false(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        tiktok = next(p for p in d["analytics"]["platforms"] if p["platform"] == "tiktok")
        assert tiktok["published"] is False
        assert tiktok["published_at"] is None

    def test_days_since_publish_computed(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        pub_date = (date.today() - timedelta(days=5)).isoformat()
        _outcome(conn, platform="youtube", published_at=pub_date + "T12:00:00")
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        yt = next(p for p in d["analytics"]["platforms"] if p["platform"] == "youtube")
        assert yt["days_since_publish"] == 5

    def test_first_and_latest_snapshot_tracked(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="manual", snapshot_date="2026-06-10")
        _analytics(conn, source="manual", snapshot_date="2026-06-16")
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        yt = next(p for p in d["analytics"]["platforms"] if p["platform"] == "youtube")
        assert yt["first_snapshot"]  == "2026-06-10"
        assert yt["latest_snapshot"] == "2026-06-16"

    def test_overview_aggregates_across_platforms(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, platform="youtube", views=4000, likes=200, source="manual")
        _analytics(conn, platform="tiktok",  views=8000, likes=500, source="csv")
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        ov = d["analytics"]["overview"]
        assert ov["total_views"]    == 12000
        assert ov["total_likes"]    == 700

    def test_avg_ctr_is_average_of_platform_ctrs(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, platform="youtube", ctr=0.06, source="manual")
        _analytics(conn, platform="tiktok",  ctr=0.04, source="manual")
        conn.close()
        d = get_episode_detail("ep_a01", db_path=db_path)
        ov = d["analytics"]["overview"]
        assert ov["avg_ctr"] == pytest.approx(5.0, rel=0.01)  # avg of 6% and 4%


# ===========================================================================
# TestAnalyticsRoute
# ===========================================================================

class TestAnalyticsRoute:

    def test_analytics_tab_returns_200(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_a01?tab=analytics")
        assert resp.status_code == 200

    def test_analytics_tab_is_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_a01?tab=analytics")
        assert b"<!DOCTYPE html>" in resp.data or b"<!doctype html>" in resp.data.lower()

    def test_analytics_tab_active_class(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _anl_html((c, db_path))
        assert '?tab=analytics" class="tab-btn active"' in html

    def test_all_prior_tabs_still_work(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        for tab in ("production", "publishing", "thumbnails", "titles"):
            resp = c.get(f"/episode/ep_a01?tab={tab}")
            assert resp.status_code == 200, f"tab={tab} returned {resp.status_code}"


# ===========================================================================
# TestTabIsolation
# ===========================================================================

class TestTabIsolation:

    def test_anl_kpi_grid_absent_on_production_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_a01?tab=production")
        assert 'class="anl-kpi-grid"' not in resp.data.decode()

    def test_title_list_absent_on_analytics_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _anl_html((c, db_path))
        assert 'class="title-list"' not in html

    def test_tn_grid_absent_on_analytics_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _anl_html((c, db_path))
        assert 'class="tn-grid"' not in html

    def test_pub_grid_absent_on_analytics_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _anl_html((c, db_path))
        assert 'class="pub-grid"' not in html


# ===========================================================================
# TestOverviewSection
# ===========================================================================

class TestOverviewSection:

    def test_empty_state_shown_when_no_real_data(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _anl_html((c, db_path))
        assert "No real analytics imported yet" in html

    def test_views_shown_when_real_data_exists(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, views=12345, source="manual")
        conn.close()
        html = _anl_html((c, db_path))
        assert "12,345" in html

    def test_ctr_percentage_shown_in_kpi(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.055, source="manual")
        conn.close()
        html = _anl_html((c, db_path))
        assert "5.5" in html
        assert "CTR" in html

    def test_mock_only_shows_empty_state(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, views=99999, source="mock")
        conn.close()
        html = _anl_html((c, db_path))
        assert "No real analytics imported yet" in html
        assert "99,999" not in html


# ===========================================================================
# TestPlatformBreakdown
# ===========================================================================

class TestPlatformBreakdown:

    def test_all_four_platform_names_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _anl_html((c, db_path))
        for name in ("YouTube", "TikTok", "Instagram", "Facebook"):
            assert name in html

    def test_platform_with_data_shows_views(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, platform="tiktok", views=22000, source="csv")
        conn.close()
        html = _anl_html((c, db_path))
        assert "22,000" in html

    def test_platform_without_data_shows_no_data_message(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, platform="youtube", source="manual")
        conn.close()
        html = _anl_html((c, db_path))
        assert "No analytics imported for this platform" in html

    def test_published_badge_on_published_platform(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _outcome(conn, platform="youtube", status="success")
        conn.close()
        html = _anl_html((c, db_path))
        assert "Published" in html

    def test_stale_warning_shown_in_platform_card(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        stale = (date.today() - timedelta(days=10)).isoformat()
        _analytics(conn, source="manual", snapshot_date=stale)
        conn.close()
        html = _anl_html((c, db_path))
        assert "Stale" in html


# ===========================================================================
# TestPublishingPerformance
# ===========================================================================

class TestPublishingPerformance:

    def test_publishing_performance_section_shown_when_published(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _outcome(conn, platform="youtube", status="success",
                 published_at="2026-06-14T14:00:00",
                 url="https://youtu.be/vid123", video_id="vid123")
        conn.close()
        html = _anl_html((c, db_path))
        assert "Publishing Performance" in html
        assert "2026-06-14" in html

    def test_url_link_shown_when_available(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _outcome(conn, url="https://youtu.be/myVid")
        conn.close()
        html = _anl_html((c, db_path))
        assert "https://youtu.be/myVid" in html
        assert "Open" in html

    def test_video_id_shown_in_table(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _outcome(conn, video_id="AbCdEfGhIjK")
        conn.close()
        html = _anl_html((c, db_path))
        assert "AbCdEfGhIjK" in html

    def test_no_publishing_performance_section_when_not_published(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _anl_html((c, db_path))
        assert "Publishing Performance" not in html


# ===========================================================================
# TestAnalyticsHealth
# ===========================================================================

class TestAnalyticsHealth:

    def test_health_section_present(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _anl_html((c, db_path))
        assert "Analytics Health" in html

    def test_real_row_count_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="manual")
        conn.close()
        html = _anl_html((c, db_path))
        assert "Real analytics rows" in html

    def test_mock_warning_shown_when_mock_data_exists(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="mock")
        conn.close()
        html = _anl_html((c, db_path))
        assert "Mock/demo data exists but is excluded from all KPIs" in html

    def test_no_mock_warning_when_no_mock_data(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="manual")
        conn.close()
        html = _anl_html((c, db_path))
        assert "Mock/demo data exists but is excluded from all KPIs" not in html

    def test_missing_platforms_listed(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        # Only YouTube has analytics; others should appear as missing
        _analytics(conn, platform="youtube", source="manual")
        conn.close()
        html = _anl_html((c, db_path))
        assert "Missing analytics" in html


# ===========================================================================
# TestNoSideEffects
# ===========================================================================

class TestNoSideEffects:

    def test_no_network_calls_on_analytics_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()

        def deny_connect(self, *args, **kwargs):
            raise AssertionError("Network call made during analytics tab render")

        with patch.object(socket.socket, "connect", deny_connect):
            resp = c.get("/episode/ep_a01?tab=analytics")
        assert resp.status_code == 200

    def test_mock_views_not_in_kpi_text(self, client):
        """Mock analytics must never surface as KPI values."""
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, views=77777, source="mock")
        conn.close()
        html = _anl_html((c, db_path))
        assert "77,777" not in html
        assert "77777"  not in html
