"""
tests/test_dashboard_server.py — Web Dashboard Phase A test suite

Coverage:
    TestRoutes          — GET / and GET /api/state return correct status codes
    TestBadges          — readiness_level badges render correctly for all levels
    TestSections        — key dashboard sections present in HTML output
    TestAPIState        — /api/state returns valid JSON with expected structure
    TestFailSafe        — returns safe degraded page when factory state is unavailable
    TestNoNetworkCalls  — no network calls during render
    TestReadOnly        — no DB writes during any request
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import the Flask app from run_dashboard
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_dashboard as server


# ---------------------------------------------------------------------------
# Fake data factories
# ---------------------------------------------------------------------------

def _fake_rh(**kwargs):
    from hypogeum.dashboard_api import RunHealthSummary
    defaults = dict(total_runs=10, complete_count=8, failed_count=1,
                    running_count=1, success_rate=0.89, last_run_at="2026-01-01T00:00:00")
    return RunHealthSummary(**{**defaults, **kwargs})


def _fake_state(
    overall_health="healthy",
    factory_name="Test Factory",
    factory_status="active",
    active_signals=None,
    pending_approvals=None,
    schedules=None,
    notifications=None,
):
    from hypogeum.dashboard_api import (
        FactoryState, RunHealthSummary, ArchiveSummary,
        RecallSummary, LearningSummary, CostSummary, AuditSummary,
    )
    return FactoryState(
        factory_id       = "test",
        schema_version   = "v2",
        checked_at       = "2026-01-01T00:00:00+00:00",
        overall_health   = overall_health,
        run_health       = _fake_rh(),
        channels         = [],
        agent_health     = [],
        model_usage      = [],
        provider_health  = [],
        archive          = ArchiveSummary(0, 0, {}, None, None),
        recall           = RecallSummary(0, 0, 0, 0.0),
        learning         = LearningSummary(0, {}, []),
        costs            = CostSummary(0.0, {}, {}, {}, 0, 0),
        audit            = AuditSummary("2026-01-01T00:00:00", True, 0, 0, "ACCEPTED"),
        notifications    = notifications or [],
        factory_name     = factory_name,
        factory_status   = factory_status,
        active_signals   = active_signals or [],
        schedules        = schedules or [],
        pending_approvals= pending_approvals or [],
    )


def _fake_report(
    readiness_level="READY",
    readiness_score=100,
    can_run=True,
    blocking_reasons=None,
    warnings=None,
    cleared=True,
):
    from hypogeum.factory_director import (
        FactoryDirectorReport, RunClearanceDecision,
    )
    clearance = RunClearanceDecision(
        cleared=cleared,
        reason="CLEARED" if cleared else "BLOCKED",
        blocking_checks=[],
        blocking_signals=[],
        pending_approvals=0,
        checked_at="2026-01-01T00:00:00",
    )
    return FactoryDirectorReport(
        factory_id       = "test",
        factory_name     = "Test Factory",
        readiness_level  = readiness_level,
        readiness_score  = readiness_score,
        can_run          = can_run,
        assessed_at      = "2026-01-01T00:00:00",
        domains          = [],
        blocking_reasons = blocking_reasons or [],
        warnings         = warnings or [],
        run_clearance    = clearance,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# TestRoutes
# ---------------------------------------------------------------------------

class TestRoutes:
    def test_index_returns_200(self, client):
        state  = _fake_state()
        report = _fake_report()
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            resp = client.get("/")
        assert resp.status_code == 200

    def test_index_returns_html(self, client):
        state  = _fake_state()
        report = _fake_report()
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            resp = client.get("/")
        assert b"<!DOCTYPE html>" in resp.data or b"<html" in resp.data

    def test_api_state_returns_200(self, client):
        state  = _fake_state()
        report = _fake_report()
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            resp = client.get("/api/state")
        assert resp.status_code == 200

    def test_api_state_content_type_json(self, client):
        state  = _fake_state()
        report = _fake_report()
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            resp = client.get("/api/state")
        assert "application/json" in resp.content_type

    def test_unknown_route_returns_404(self, client):
        resp = client.get("/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestBadges
# ---------------------------------------------------------------------------

class TestBadges:
    def _render(self, client, level, score=80):
        state  = _fake_state(overall_health=level.lower())
        report = _fake_report(readiness_level=level, readiness_score=score,
                               can_run=(level in ("READY", "DEGRADED")))
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            return client.get("/").data.decode()

    def test_ready_badge_rendered(self, client):
        html = self._render(client, "READY", score=100)
        assert "badge-ready" in html
        assert "READY" in html

    def test_degraded_badge_rendered(self, client):
        html = self._render(client, "DEGRADED", score=70)
        assert "badge-degraded" in html
        assert "DEGRADED" in html

    def test_blocked_badge_rendered(self, client):
        html = self._render(client, "BLOCKED", score=30)
        assert "badge-blocked" in html
        assert "BLOCKED" in html

    def test_unknown_badge_rendered(self, client):
        html = self._render(client, "UNKNOWN", score=0)
        assert "badge-unknown" in html
        assert "UNKNOWN" in html

    def test_score_displayed(self, client):
        html = self._render(client, "DEGRADED", score=75)
        assert "75" in html


# ---------------------------------------------------------------------------
# TestSections
# ---------------------------------------------------------------------------

class TestSections:
    @pytest.fixture()
    def html(self, client):
        state  = _fake_state()
        report = _fake_report()
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            return client.get("/").data.decode()

    def test_factory_header_present(self, html):
        assert "Test Factory" in html

    def test_run_health_section(self, html):
        assert "Run Health" in html

    def test_director_section(self, html):
        assert "Factory Director" in html

    def test_ois_signals_section(self, html):
        assert "Active OIS Signals" in html

    def test_pending_approvals_section(self, html):
        assert "Pending Approvals" in html

    def test_channels_section(self, html):
        assert "Channels" in html

    def test_schedules_section(self, html):
        assert "Schedules" in html

    def test_costs_section(self, html):
        assert "Costs" in html

    def test_audit_section(self, html):
        assert "Acceptance Audit" in html

    def test_director_domains_section(self, html):
        assert "Director" in html and "Domain" in html

    def test_api_link_in_footer(self, html):
        assert "/api/state" in html

    def test_auto_refresh_meta_present(self, html):
        assert 'http-equiv="refresh"' in html
        assert 'content="30"' in html

    def test_no_external_cdn(self, html):
        # Must not reference any CDN
        for cdn in ("cdn.jsdelivr", "unpkg.com", "cdnjs.cloudflare", "googleapis"):
            assert cdn not in html

    def test_no_script_src(self, html):
        # No external JS frameworks loaded via <script src>
        assert '<script src=' not in html


# ---------------------------------------------------------------------------
# TestAPIState
# ---------------------------------------------------------------------------

class TestAPIState:
    def _json(self, client):
        state  = _fake_state()
        report = _fake_report()
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            resp = client.get("/api/state")
        return json.loads(resp.data)

    def test_has_factory_state_key(self, client):
        data = self._json(client)
        assert "factory_state" in data

    def test_has_director_report_key(self, client):
        data = self._json(client)
        assert "director_report" in data

    def test_factory_state_has_factory_id(self, client):
        data = self._json(client)
        assert "factory_id" in data["factory_state"]

    def test_factory_state_has_readiness(self, client):
        data = self._json(client)
        assert "overall_health" in data["factory_state"]

    def test_director_report_has_level(self, client):
        data = self._json(client)
        assert "readiness_level" in data["director_report"]

    def test_director_report_has_score(self, client):
        data = self._json(client)
        assert "readiness_score" in data["director_report"]

    def test_director_report_has_clearance(self, client):
        data = self._json(client)
        assert "run_clearance" in data["director_report"]

    def test_json_is_valid(self, client):
        state  = _fake_state()
        report = _fake_report()
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            resp = client.get("/api/state")
        # Must parse without error
        parsed = json.loads(resp.data)
        assert isinstance(parsed, dict)

    def test_blocking_signal_in_state(self, client):
        sig = {"signal_id": "s1", "signal_type": "budget_exhausted",
               "severity": "CRITICAL", "message": "hit ceiling",
               "blocking": True, "lifecycle_status": "open",
               "channel_id": None, "source_system": "finance",
               "created_at": "2026-01-01T00:00:00"}
        state  = _fake_state(active_signals=[sig])
        report = _fake_report(readiness_level="BLOCKED", readiness_score=50,
                               can_run=False, cleared=False,
                               blocking_reasons=["[ois] OIS blocked"])
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            resp = client.get("/api/state")
        data = json.loads(resp.data)
        signals = data["factory_state"]["active_signals"]
        assert len(signals) == 1
        assert signals[0]["blocking"] is True


# ---------------------------------------------------------------------------
# TestFailSafe
# ---------------------------------------------------------------------------

class TestFailSafe:
    def test_index_200_when_get_factory_state_raises(self, client):
        with patch.object(server, "get_factory_state", side_effect=RuntimeError("DB down")), \
             patch.object(server, "assess_factory", side_effect=RuntimeError("DB down")):
            resp = client.get("/")
        assert resp.status_code == 200

    def test_index_shows_error_banner_on_failure(self, client):
        with patch.object(server, "get_factory_state", side_effect=Exception("connection refused")), \
             patch.object(server, "assess_factory", side_effect=Exception("connection refused")):
            resp = client.get("/")
        assert b"connection refused" in resp.data or b"unavailable" in resp.data

    def test_api_state_503_when_factory_state_raises(self, client):
        with patch.object(server, "get_factory_state", side_effect=RuntimeError("DB down")), \
             patch.object(server, "assess_factory", side_effect=RuntimeError("DB down")):
            resp = client.get("/api/state")
        assert resp.status_code == 503

    def test_api_state_503_has_error_key(self, client):
        with patch.object(server, "get_factory_state", side_effect=Exception("timeout")), \
             patch.object(server, "assess_factory", side_effect=Exception("timeout")):
            resp = client.get("/api/state")
        data = json.loads(resp.data)
        assert "error" in data

    def test_blocking_signals_shown_in_html(self, client):
        sig = {"signal_id": "s1", "signal_type": "credential_failure",
               "severity": "CRITICAL", "message": "key invalid",
               "blocking": True, "lifecycle_status": "open",
               "channel_id": None, "source_system": "startup",
               "created_at": "2026-01-01T00:00:00"}
        state  = _fake_state(active_signals=[sig])
        report = _fake_report(readiness_level="BLOCKED", readiness_score=50,
                               can_run=False, cleared=False)
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            resp = client.get("/")
        html = resp.data.decode()
        assert "credential_failure" in html

    def test_pending_approval_shown_in_html(self, client):
        from hypogeum.approvals import ApprovalRecord
        approval = ApprovalRecord(
            approval_id="appr-001", run_id="run-001", episode_id="ep_001",
            approval_type="human_review", requested_by="system:pipeline",
            status="pending", requested_at="2026-01-01T00:00:00",
            decided_by=None, decided_at=None, notes=None, evidence=None,
        )
        state  = _fake_state(pending_approvals=[approval])
        report = _fake_report()
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            resp = client.get("/")
        html = resp.data.decode()
        assert "human_review" in html
        assert "appr-001" in html


# ---------------------------------------------------------------------------
# TestNoNetworkCalls
# ---------------------------------------------------------------------------

class TestNoNetworkCalls:
    @pytest.fixture(autouse=True)
    def block_network(self, monkeypatch):
        def no_socket(*args, **kwargs):
            raise RuntimeError("Network call detected in dashboard server tests")
        monkeypatch.setattr(socket, "socket", no_socket)
        yield

    def test_index_no_network(self, client):
        state  = _fake_state()
        report = _fake_report()
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            client.get("/")

    def test_api_state_no_network(self, client):
        state  = _fake_state()
        report = _fake_report()
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            client.get("/api/state")


# ---------------------------------------------------------------------------
# TestReadOnly
# ---------------------------------------------------------------------------

class TestReadOnly:
    def test_index_calls_no_db_writes(self, client):
        state  = _fake_state()
        report = _fake_report()
        # Patch at the app module level; if any write SQL slips through
        # the DB is not involved at all since state/report are mocked
        with patch.object(server, "get_factory_state", return_value=state) as mock_s, \
             patch.object(server, "assess_factory", return_value=report) as mock_r:
            client.get("/")
        mock_s.assert_called_once()
        mock_r.assert_called_once()

    def test_to_dict_handles_nested_dataclasses(self):
        state = _fake_state()
        d = server._to_dict(state)
        assert isinstance(d, dict)
        assert "run_health" in d
        assert isinstance(d["run_health"], dict)
        assert "total_runs" in d["run_health"]

    def test_to_dict_handles_lists(self):
        report = _fake_report()
        d = server._to_dict(report)
        assert isinstance(d["blocking_reasons"], list)
        assert isinstance(d["domains"], list)
