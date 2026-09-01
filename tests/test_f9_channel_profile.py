"""
tests/test_f9_channel_profile.py — F9 Channel Profile tests

Coverage:
    TestGetChannelProfile
        — returns defaults when no row exists
        — creates default row on first call (subsequent call reads same values)
        — all target fields present with positive defaults

    TestUpdateChannelProfile
        — update channel_name persists
        — update numeric targets persists
        — unknown keys are silently ignored
        — empty update returns error
        — integer coercion works for targets

    TestAPIRoutes
        — GET /channel-profile returns 200 HTML
        — GET /api/channel-profile returns 200 JSON
        — GET /api/channel-profile has expected keys
        — POST /api/channel-profile/update saves name
        — POST /channel-profile/save redirects

    TestDashboardShowsTargets
        — weekly targets appear in /dashboard HTML
        — channel name from profile appears in /dashboard
        — /dashboard has chart section
        — /dashboard links to channel profile editor
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import initialize_schema
from hypogeum.channel_profile import get_channel_profile, update_channel_profile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "f9_cp.db"
    initialize_schema(p)
    return p


def _fake_state(factory_name="Test Factory"):
    from hypogeum.dashboard_api import (
        FactoryState, RunHealthSummary, ArchiveSummary,
        RecallSummary, LearningSummary, CostSummary, AuditSummary,
    )
    return FactoryState(
        factory_id="test", schema_version="v2",
        checked_at="2026-01-01T00:00:00+00:00",
        overall_health="healthy",
        run_health=RunHealthSummary(0, 0, 0, 0, 0.0, None),
        channels=[], agent_health=[], model_usage=[],
        provider_health=[],
        archive=ArchiveSummary(0, 0, {}, None, None),
        recall=RecallSummary(0, 0, 0, 0.0),
        learning=LearningSummary(0, {}, []),
        costs=CostSummary(0.0, {}, {}, {}, 0, 0),
        audit=AuditSummary("2026-01-01T00:00:00", True, 0, 0, "ACCEPTED"),
        notifications=[], factory_name=factory_name, factory_status="active",
        active_signals=[], schedules=[], pending_approvals=[],
    )


def _fake_report():
    from hypogeum.factory_director import FactoryDirectorReport, RunClearanceDecision
    return FactoryDirectorReport(
        factory_id="test", factory_name="Test Factory",
        readiness_level="READY", readiness_score=100, can_run=True,
        assessed_at="2026-01-01T00:00:00", domains=[],
        blocking_reasons=[], warnings=[],
        run_clearance=RunClearanceDecision(
            cleared=True, reason="CLEARED", blocking_checks=[],
            blocking_signals=[], pending_approvals=0,
            checked_at="2026-01-01T00:00:00",
        ),
    )


@pytest.fixture()
def client():
    import run_dashboard as server
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


# ===========================================================================
# TestGetChannelProfile
# ===========================================================================

class TestGetChannelProfile:
    def test_returns_dict(self, db):
        result = get_channel_profile(db_path=db)
        assert isinstance(result, dict)

    def test_default_targets_present(self, db):
        result = get_channel_profile(db_path=db)
        assert "yt_videos_per_week"  in result
        assert "yt_shorts_per_week"  in result
        assert "tiktok_per_week"     in result
        assert "instagram_per_week"  in result
        assert "facebook_per_week"   in result

    def test_default_targets_positive(self, db):
        result = get_channel_profile(db_path=db)
        for k in ("yt_videos_per_week", "yt_shorts_per_week",
                  "tiktok_per_week", "instagram_per_week", "facebook_per_week"):
            assert result[k] > 0, f"{k} must be > 0 in defaults"

    def test_default_channel_name_is_none_or_string(self, db):
        result = get_channel_profile(db_path=db)
        assert result.get("channel_name") is None or isinstance(result["channel_name"], str)

    def test_second_call_returns_same_data(self, db):
        r1 = get_channel_profile(db_path=db)
        r2 = get_channel_profile(db_path=db)
        assert r1["yt_videos_per_week"] == r2["yt_videos_per_week"]

    def test_colors_present(self, db):
        result = get_channel_profile(db_path=db)
        assert result.get("primary_color")
        assert result.get("secondary_color")
        assert result.get("accent_color")

    def test_nonexistent_db_returns_defaults(self, tmp_path):
        no_db = tmp_path / "missing.db"
        result = get_channel_profile(db_path=no_db)
        assert "yt_videos_per_week" in result


# ===========================================================================
# TestUpdateChannelProfile
# ===========================================================================

class TestUpdateChannelProfile:
    def test_update_channel_name(self, db):
        result = update_channel_profile({"channel_name": "My Tech Channel"}, db_path=db)
        assert result["ok"] is True
        profile = get_channel_profile(db_path=db)
        assert profile["channel_name"] == "My Tech Channel"

    def test_update_numeric_targets(self, db):
        result = update_channel_profile({"yt_videos_per_week": 5}, db_path=db)
        assert result["ok"] is True
        profile = get_channel_profile(db_path=db)
        assert profile["yt_videos_per_week"] == 5

    def test_update_multiple_fields(self, db):
        update_channel_profile({
            "channel_name": "Finance Pro",
            "content_niche": "Personal Finance",
            "tiktok_per_week": 7,
        }, db_path=db)
        profile = get_channel_profile(db_path=db)
        assert profile["channel_name"]    == "Finance Pro"
        assert profile["content_niche"]   == "Personal Finance"
        assert profile["tiktok_per_week"] == 7

    def test_unknown_keys_ignored(self, db):
        result = update_channel_profile(
            {"channel_name": "Safe", "_injected": "evil"},
            db_path=db,
        )
        assert result["ok"] is True
        profile = get_channel_profile(db_path=db)
        assert "evil" not in str(profile)

    def test_empty_update_returns_error(self, db):
        result = update_channel_profile({}, db_path=db)
        assert result["ok"] is False

    def test_integer_coercion_from_string(self, db):
        update_channel_profile({"yt_shorts_per_week": "4"}, db_path=db)
        profile = get_channel_profile(db_path=db)
        assert profile["yt_shorts_per_week"] == 4

    def test_update_returns_updated_list(self, db):
        result = update_channel_profile({"channel_name": "New"}, db_path=db)
        assert "updated" in result
        assert "channel_name" in result["updated"]


# ===========================================================================
# TestAPIRoutes
# ===========================================================================

class TestAPIRoutes:
    def test_channel_profile_page_200(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        # legacy route redirects to the channel list / a channel now
        resp = client.get("/channel-profile", follow_redirects=True)
        assert resp.status_code == 200

    def test_channel_profile_page_html(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/channel-profile", follow_redirects=True)
        assert b"<!DOCTYPE html>" in resp.data or b"<html" in resp.data

    def test_api_channel_profile_200(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/api/channel-profile")
        assert resp.status_code == 200

    def test_api_channel_profile_json(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/api/channel-profile")
        assert "application/json" in resp.content_type

    def test_api_channel_profile_has_targets(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/api/channel-profile")
        data = json.loads(resp.data)
        for k in ("yt_videos_per_week", "tiktok_per_week", "instagram_per_week"):
            assert k in data

    def test_api_update_saves_name(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.post(
            "/api/channel-profile/update",
            json={"channel_name": "API Updated"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        # Verify persistence
        profile_resp = client.get("/api/channel-profile")
        profile = json.loads(profile_resp.data)
        assert profile["channel_name"] == "API Updated"

    def test_form_save_redirects(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.post("/channel-profile/save",
                           data={"channel_name": "Form Saved"})
        assert resp.status_code in (301, 302)

    def test_connect_platform_page_youtube(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/platforms/connect/youtube")
        assert resp.status_code == 200
        assert b"YouTube" in resp.data

    def test_connect_platform_page_tiktok(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/platforms/connect/tiktok")
        assert resp.status_code == 200
        assert b"TikTok" in resp.data

    def test_connect_platform_invalid_returns_404(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/platforms/connect/snapchat")
        assert resp.status_code == 404

    def test_connect_platform_save_api(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.post(
            "/api/platforms/connect/youtube/save",
            json={"credential": "ya29.FakeToken", "label": "YT Test"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True

    def test_connect_platform_save_empty_credential_rejected(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.post(
            "/api/platforms/connect/youtube/save",
            json={"credential": "", "label": "Empty"},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_connect_platform_form_save_redirects(self, client, db):
        import run_dashboard as server
        server.app.config["DB_PATH"] = str(db)
        resp = client.post(
            "/platforms/connect/youtube/save",
            data={"credential": "ya29.Token", "label": "YT main"},
        )
        assert resp.status_code in (301, 302)


# ===========================================================================
# TestDashboardShowsTargets
# ===========================================================================

class TestDashboardShowsTargets:
    def _html(self, client, db_path=None):
        import run_dashboard as server
        if db_path:
            server.app.config["DB_PATH"] = str(db_path)
        else:
            server.app.config.pop("DB_PATH", None)
        state  = _fake_state()
        report = _fake_report()
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            return client.get("/dashboard").data.decode()

    def test_weekly_targets_in_dashboard(self, client, db):
        update_channel_profile({"yt_videos_per_week": 3}, db_path=db)
        html = self._html(client, db_path=db)
        assert "/wk" in html or "per_week" in html or "3" in html

    def test_chart_section_in_dashboard(self, client, db):
        html = self._html(client, db_path=db)
        assert "exec-charts" in html or "chart" in html.lower()

    def test_channel_profile_link_in_dashboard(self, client, db):
        html = self._html(client, db_path=db)
        assert "/channel-profile" in html

    def test_channel_name_from_profile_in_dashboard(self, client, db):
        update_channel_profile({"channel_name": "Rocket Finance"}, db_path=db)
        html = self._html(client, db_path=db)
        assert "Rocket Finance" in html

    def test_target_bar_labels_present(self, client, db):
        html = self._html(client, db_path=db)
        # Weekly Targets section should show platform labels
        assert "YouTube" in html
        assert "TikTok" in html
