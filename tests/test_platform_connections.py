"""
tests/test_platform_connections.py — F8 Platform Connection & Data Integrity Tests

Coverage:
    TestDetectRealVsMock
        — mock analytics are counted and flagged correctly
        — real analytics (source='manual'/'csv'/'api') are counted separately
        — mixed real+mock: real counts correctly, mock counts separately
        — empty DB returns safe defaults

    TestGetPlatformConnectionState
        — no credentials + no analytics → all platforms "not_connected"
        — active youtube credential → "connected_no_data"
        — active credential + real analytics → "connected"
        — mock analytics only (no cred) → "demo_only"
        — published platform with no cred → "published_no_cred"

    TestExecutiveKPIsExcludeMock
        — get_executive_overview with only mock analytics → views/watch_time None
        — get_executive_overview with real analytics → views populated
        — mixed real+mock → only real values in KPIs

    TestAPIRoute
        — /api/platform-connections returns 200 JSON
        — response has "connections" and "analytics_integrity" keys
        — each platform in "connections.platforms"
        — demo_warning present when mock-only data exists

    TestDashboardShowsDisconnectedState
        — /dashboard HTML shows "Not connected" when no credentials
        — /dashboard HTML shows demo banner when mock analytics exist
        — /dashboard HTML does NOT show mock view counts as real KPIs
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import get_connection, initialize_schema
from hypogeum.analytics_store import store_platform_metrics, seed_mock_analytics
from hypogeum.platform_connections import (
    detect_real_vs_mock_analytics,
    get_platform_connection_state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """Fresh initialised DB."""
    p = tmp_path / "f8_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def db_with_mock(db):
    """DB containing ONLY mock analytics for youtube."""
    seed_mock_analytics("ep001", "youtube", db_path=db)
    seed_mock_analytics("ep001", "tiktok",  db_path=db)
    return db


@pytest.fixture()
def db_with_real(db):
    """DB containing ONLY real (manual) analytics for youtube."""
    store_platform_metrics(
        episode_id="ep001", platform="youtube",
        metrics={"views": 1200, "ctr": 0.045, "avg_view_pct": 0.52},
        source="manual", db_path=db,
    )
    return db


@pytest.fixture()
def db_with_mixed(db):
    """DB with mock youtube + real csv youtube + real manual tiktok."""
    seed_mock_analytics("ep001", "youtube", db_path=db)
    store_platform_metrics(
        episode_id="ep002", platform="youtube",
        metrics={"views": 5000, "ctr": 0.06},
        source="csv", db_path=db,
    )
    store_platform_metrics(
        episode_id="ep001", platform="tiktok",
        metrics={"views": 800},
        source="manual", db_path=db,
    )
    return db


@pytest.fixture()
def db_with_credential(db):
    """DB with an active youtube credential."""
    conn = get_connection(db)
    conn.execute(
        """INSERT INTO secret_profiles
           (secret_profile_id, provider, label, credential_type,
            credential_data, status, scope, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("sp001", "youtube", "YT OAuth", "oauth2_token",
         "ya29.fakeoauthtoken", "active", "factory",
         "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def db_with_expired_credential(db):
    """DB with an expired youtube credential."""
    conn = get_connection(db)
    conn.execute(
        """INSERT INTO secret_profiles
           (secret_profile_id, provider, label, credential_type,
            credential_data, status, scope, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("sp002", "youtube", "YT OAuth", "oauth2_token",
         "ya29.expiredtoken", "expired", "factory",
         "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def db_with_published(db):
    """DB with a publishing_outcome for youtube (credential removed)."""
    conn = get_connection(db)
    conn.execute(
        """INSERT INTO episodes (episode_id, created_at)
           VALUES ('ep001', '2026-01-01T00:00:00')"""
    )
    conn.execute(
        """INSERT INTO publishing_outcomes
           (outcome_id, episode_id, run_id, platform, publishing_status,
            platform_video_id, published_at, recorded_at, metadata_version)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("out001", "ep001", None, "youtube", "success",
         "yt_abc123", "2026-01-01T12:00:00", "2026-01-01T12:01:00", "v1"),
    )
    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# Flask client for /dashboard and /api/platform-connections tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def flask_app():
    import run_dashboard as server
    server.app.config["TESTING"] = True
    return server


@pytest.fixture()
def client(flask_app):
    with flask_app.app.test_client() as c:
        yield c


def _fake_state(**kwargs):
    from hypogeum.dashboard_api import (
        FactoryState, RunHealthSummary, ArchiveSummary,
        RecallSummary, LearningSummary, CostSummary, AuditSummary,
    )
    defaults = dict(
        factory_id="test", schema_version="v2",
        checked_at="2026-01-01T00:00:00+00:00",
        overall_health="healthy",
        run_health=RunHealthSummary(
            total_runs=0, complete_count=0, failed_count=0,
            running_count=0, success_rate=0.0, last_run_at=None,
        ),
        channels=[], agent_health=[], model_usage=[],
        provider_health=[],
        archive=ArchiveSummary(0, 0, {}, None, None),
        recall=RecallSummary(0, 0, 0, 0.0),
        learning=LearningSummary(0, {}, []),
        costs=CostSummary(0.0, {}, {}, {}, 0, 0),
        audit=AuditSummary("2026-01-01T00:00:00", True, 0, 0, "ACCEPTED"),
        notifications=[], factory_name="Test Factory", factory_status="active",
        active_signals=[], schedules=[], pending_approvals=[],
    )
    defaults.update(kwargs)
    return FactoryState(**defaults)


def _fake_report():
    from hypogeum.factory_director import FactoryDirectorReport, RunClearanceDecision
    return FactoryDirectorReport(
        factory_id="test", factory_name="Test Factory",
        readiness_level="READY", readiness_score=100,
        can_run=True, assessed_at="2026-01-01T00:00:00",
        domains=[], blocking_reasons=[], warnings=[],
        run_clearance=RunClearanceDecision(
            cleared=True, reason="CLEARED", blocking_checks=[],
            blocking_signals=[], pending_approvals=0,
            checked_at="2026-01-01T00:00:00",
        ),
    )


# ===========================================================================
# TestDetectRealVsMock
# ===========================================================================

class TestDetectRealVsMock:
    def test_empty_db_returns_zeros(self, db):
        result = detect_real_vs_mock_analytics(db_path=db)
        assert result["totals"]["real_count"] == 0
        assert result["totals"]["mock_count"] == 0
        assert result["has_any_real"] is False
        assert result["has_any_mock"] is False
        assert result["demo_warning"] is None

    def test_mock_only_flagged_as_no_real(self, db_with_mock):
        result = detect_real_vs_mock_analytics(db_path=db_with_mock)
        assert result["has_any_real"] is False
        assert result["has_any_mock"] is True
        assert result["totals"]["mock_count"] > 0
        assert result["totals"]["real_count"] == 0

    def test_mock_only_generates_demo_warning(self, db_with_mock):
        result = detect_real_vs_mock_analytics(db_path=db_with_mock)
        assert result["demo_warning"] is not None
        assert "demo" in result["demo_warning"].lower() or "mock" in result["demo_warning"].lower()

    def test_real_data_counted_correctly(self, db_with_real):
        result = detect_real_vs_mock_analytics(db_path=db_with_real)
        assert result["has_any_real"] is True
        assert result["has_any_mock"] is False
        assert result["totals"]["real_count"] >= 1
        assert result["totals"]["mock_count"] == 0

    def test_real_data_no_demo_warning(self, db_with_real):
        result = detect_real_vs_mock_analytics(db_path=db_with_real)
        assert result["demo_warning"] is None

    def test_mixed_real_and_mock_counts_separately(self, db_with_mixed):
        result = detect_real_vs_mock_analytics(db_path=db_with_mixed)
        assert result["has_any_real"] is True
        assert result["has_any_mock"] is True
        assert result["totals"]["real_count"] >= 2   # youtube csv + tiktok manual
        assert result["totals"]["mock_count"] >= 1   # youtube mock

    def test_per_platform_youtube_mock_only(self, db_with_mock):
        result = detect_real_vs_mock_analytics(db_path=db_with_mock)
        yt = result["by_platform"]["youtube"]
        assert yt["data_state"] == "mock_only"
        assert yt["has_mock"] is True
        assert yt["has_real"] is False

    def test_per_platform_youtube_real(self, db_with_real):
        result = detect_real_vs_mock_analytics(db_path=db_with_real)
        yt = result["by_platform"]["youtube"]
        assert yt["data_state"] == "real"
        assert yt["has_real"] is True
        assert yt["real_count"] >= 1

    def test_per_platform_unconnected_platform_state_is_none(self, db_with_real):
        result = detect_real_vs_mock_analytics(db_path=db_with_real)
        # tiktok has no data → "none"
        assert result["by_platform"]["tiktok"]["data_state"] == "none"
        assert result["by_platform"]["tiktok"]["real_count"] == 0
        assert result["by_platform"]["tiktok"]["mock_count"] == 0

    def test_nonexistent_db_returns_safe_defaults(self, tmp_path):
        no_db = tmp_path / "missing.db"
        result = detect_real_vs_mock_analytics(db_path=no_db)
        assert result["has_any_real"] is False
        assert result["has_any_mock"] is False


# ===========================================================================
# TestGetPlatformConnectionState
# ===========================================================================

class TestGetPlatformConnectionState:
    def test_empty_db_all_not_connected(self, db):
        result = get_platform_connection_state(db_path=db)
        for p in ("youtube", "tiktok", "instagram", "facebook"):
            assert result["platforms"][p]["connection_state"] == "not_connected"

    def test_empty_db_any_connected_false(self, db):
        result = get_platform_connection_state(db_path=db)
        assert result["any_connected"] is False

    def test_active_credential_no_data_is_connected_no_data(self, db_with_credential):
        result = get_platform_connection_state(db_path=db_with_credential)
        yt = result["platforms"]["youtube"]
        assert yt["connection_state"] == "connected_no_data"
        assert yt["has_credential"] is True

    def test_active_credential_any_connected_true(self, db_with_credential):
        result = get_platform_connection_state(db_path=db_with_credential)
        assert result["any_connected"] is True

    def test_credential_plus_real_analytics_is_connected(self, db_with_credential, db):
        # Add real analytics to the credentialed DB
        store_platform_metrics(
            "ep001", "youtube", {"views": 999},
            source="manual", db_path=db_with_credential,
        )
        result = get_platform_connection_state(db_path=db_with_credential)
        yt = result["platforms"]["youtube"]
        assert yt["connection_state"] == "connected"
        assert yt["real_count"] >= 1

    def test_zernio_account_is_connection_source_of_truth(self, db):
        # A Zernio provider_account wins over any direct secret_profile:
        # no direct YouTube cred here, but an active Zernio youtube account.
        from hypogeum.publishing.provider_repository import upsert_provider_account
        upsert_provider_account(
            db, provider_id="zernio", platform="youtube",
            external_account_id="acc_yt", display_label="The Hypogeum",
            status="active", needs_reconnection=False,
            metadata={"username": "thehypogeum", "followers_count": 3,
                      "has_analytics_access": True, "synced_at": "2026-08-31T00:00:00+00:00"},
        )
        result = get_platform_connection_state(db_path=db)
        yt = result["platforms"]["youtube"]
        assert yt["connection_via"] == "zernio"
        assert yt["has_credential"] is True
        assert yt["account"]["username"] == "thehypogeum"
        assert yt["account"]["followers_count"] == 3
        assert result["any_connected"] is True

    def test_zernio_needs_reconnection_maps_to_published_no_cred(self, db):
        from hypogeum.publishing.provider_repository import upsert_provider_account
        upsert_provider_account(
            db, provider_id="zernio", platform="tiktok",
            external_account_id="acc_tk", status="needs_reconnection",
            needs_reconnection=True, metadata={"username": "thehypogeum1"},
        )
        result = get_platform_connection_state(db_path=db)
        tk = result["platforms"]["tiktok"]
        assert tk["connection_via"] == "zernio"
        assert tk["has_credential"] is False
        assert tk["connection_state"] == "published_no_cred"
        assert tk["connection_label"] == "Reconnect on Zernio"

    def test_no_zernio_row_falls_back_to_direct_credential(self, db_with_credential):
        # db_with_credential has a direct youtube secret_profile, no Zernio rows.
        result = get_platform_connection_state(db_path=db_with_credential)
        yt = result["platforms"]["youtube"]
        assert yt["connection_via"] == "direct"
        assert yt["has_credential"] is True

    def test_mock_only_no_credential_is_demo_only(self, db_with_mock):
        result = get_platform_connection_state(db_path=db_with_mock)
        yt = result["platforms"]["youtube"]
        assert yt["connection_state"] == "demo_only"

    def test_demo_only_has_credential_false(self, db_with_mock):
        result = get_platform_connection_state(db_path=db_with_mock)
        yt = result["platforms"]["youtube"]
        assert yt["has_credential"] is False

    def test_published_no_cred_state(self, db_with_published):
        result = get_platform_connection_state(db_path=db_with_published)
        yt = result["platforms"]["youtube"]
        assert yt["connection_state"] == "published_no_cred"
        assert yt["has_published"] is True

    def test_not_connected_label_text(self, db):
        result = get_platform_connection_state(db_path=db)
        yt = result["platforms"]["youtube"]
        assert "not" in yt["connection_label"].lower() or "connect" in yt["connection_label"].lower()

    def test_all_four_platforms_present(self, db):
        result = get_platform_connection_state(db_path=db)
        for p in ("youtube", "tiktok", "instagram", "facebook"):
            assert p in result["platforms"]

    def test_platform_display_name(self, db):
        result = get_platform_connection_state(db_path=db)
        assert result["platforms"]["youtube"]["name"] == "YouTube"
        assert result["platforms"]["tiktok"]["name"] == "TikTok"
        assert result["platforms"]["instagram"]["name"] == "Instagram"
        assert result["platforms"]["facebook"]["name"] == "Facebook"

    def test_nonexistent_db_returns_not_connected(self, tmp_path):
        no_db = tmp_path / "missing.db"
        result = get_platform_connection_state(db_path=no_db)
        for p in ("youtube", "tiktok", "instagram", "facebook"):
            assert result["platforms"][p]["connection_state"] == "not_connected"


# ===========================================================================
# TestExecutiveKPIsExcludeMock
# ===========================================================================

class TestExecutiveKPIsExcludeMock:
    def test_mock_only_views_is_none(self, db_with_mock):
        from hypogeum.dashboard_executive import get_executive_overview
        result = get_executive_overview(db_path=db_with_mock)
        assert result["analytics"]["views"] is None, (
            "views must be None when only mock analytics exist"
        )

    def test_mock_only_watch_time_is_none(self, db_with_mock):
        from hypogeum.dashboard_executive import get_executive_overview
        result = get_executive_overview(db_path=db_with_mock)
        assert result["analytics"]["watch_time_h"] is None

    def test_real_analytics_views_populated(self, db_with_real):
        from hypogeum.dashboard_executive import get_executive_overview
        result = get_executive_overview(db_path=db_with_real)
        assert result["analytics"]["views"] == 1200

    def test_mixed_only_real_values_in_kpis(self, db_with_mixed):
        """With 1 mock + 1 real youtube row, views must equal real row only (5000)."""
        from hypogeum.dashboard_executive import get_executive_overview
        result = get_executive_overview(db_path=db_with_mixed)
        # youtube: 1 mock (seeded) + 1 real csv (views=5000)
        # real_views = 5000, NOT 5000 + seeded_mock_views
        assert result["analytics"]["views"] == 5000

    def test_demo_warning_set_when_mock_only(self, db_with_mock):
        from hypogeum.dashboard_executive import get_executive_overview
        result = get_executive_overview(db_path=db_with_mock)
        assert result["demo_warning"] is not None

    def test_demo_warning_none_when_real_only(self, db_with_real):
        from hypogeum.dashboard_executive import get_executive_overview
        result = get_executive_overview(db_path=db_with_real)
        assert result["demo_warning"] is None


# ===========================================================================
# TestAPIRoute
# ===========================================================================

class TestAPIRoute:
    def test_platform_connections_route_200(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/api/platform-connections")
        assert resp.status_code == 200

    def test_platform_connections_content_type_json(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/api/platform-connections")
        assert "application/json" in resp.content_type

    def test_platform_connections_has_connections_key(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/api/platform-connections")
        data = json.loads(resp.data)
        assert "connections" in data

    def test_platform_connections_has_analytics_integrity_key(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/api/platform-connections")
        data = json.loads(resp.data)
        assert "analytics_integrity" in data

    def test_platform_connections_all_platforms_present(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/api/platform-connections")
        data = json.loads(resp.data)
        platforms = data["connections"]["platforms"]
        for p in ("youtube", "tiktok", "instagram", "facebook"):
            assert p in platforms

    def test_platform_connections_demo_warning_when_mock(self, client, db_with_mock):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db_with_mock)
        resp = client.get("/api/platform-connections")
        data = json.loads(resp.data)
        assert data["connections"]["demo_warning"] is not None

    def test_platform_connections_no_demo_warning_when_real(self, client, db_with_real):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db_with_real)
        resp = client.get("/api/platform-connections")
        data = json.loads(resp.data)
        assert data["connections"]["demo_warning"] is None

    def test_platform_connections_surface_is_discoverable_from_dashboard(self, client):
        # The dashboard no longer footer-links /api/platform-connections
        # directly; the connections surface moved to the /providers tab
        # (Provider Control Center). The API endpoint itself is unchanged
        # (covered by the other tests in this class).
        state  = _fake_state()
        report = _fake_report()
        import run_dashboard as server
        server.app.config.pop("DB_PATH", None)
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            resp = client.get("/dashboard")
        assert b"/providers" in resp.data or b"/api/platform-connections" in resp.data


# ===========================================================================
# TestDashboardShowsDisconnectedState
# ===========================================================================

class TestDashboardShowsDisconnectedState:
    def _get_dashboard(self, client, db_path=None):
        import run_dashboard as server
        state  = _fake_state()
        report = _fake_report()
        if db_path:
            server.app.config["DB_PATH"] = str(db_path)
        else:
            server.app.config.pop("DB_PATH", None)
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            return client.get("/dashboard").data.decode()

    def test_not_connected_text_present_when_no_credentials(self, client, db):
        html = self._get_dashboard(client, db_path=db)
        assert "Not connected" in html

    def test_demo_banner_shown_when_mock_analytics_exist(self, client, db_with_mock):
        html = self._get_dashboard(client, db_path=db_with_mock)
        assert "Demo" in html or "demo" in html or "mock" in html.lower()

    def test_no_real_data_shows_dash_for_views(self, client, db_with_mock):
        html = self._get_dashboard(client, db_path=db_with_mock)
        # The analytics summary must NOT show a large integer for views;
        # it must show "—" (dash) or "No real data"
        assert "No real data" in html or "—" in html

    def test_demo_only_label_shown_for_mock_platform(self, client, db_with_mock):
        html = self._get_dashboard(client, db_path=db_with_mock)
        assert "Demo data only" in html

    def test_connected_label_shown_when_real_analytics(self, client, db_with_credential):
        # Seed real analytics too
        store_platform_metrics(
            "ep001", "youtube", {"views": 42},
            source="manual", db_path=db_with_credential,
        )
        html = self._get_dashboard(client, db_path=db_with_credential)
        assert "Connected" in html

    def test_dashboard_no_script_src_still_holds(self, client, db):
        html = self._get_dashboard(client, db_path=db)
        assert "<script src=" not in html

    def test_dashboard_still_has_meta_refresh(self, client, db):
        html = self._get_dashboard(client, db_path=db)
        assert 'http-equiv="refresh"' in html
        assert 'content="30"' in html
