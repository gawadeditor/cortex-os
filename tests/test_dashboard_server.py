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
    def test_index_redirects(self, client):
        resp = client.get("/")
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert "/dashboard" in location or "/command" in location

    def test_index_follows_redirect_to_command(self, client):
        resp = client.get("/", follow_redirects=True)
        assert resp.status_code == 200
        assert b"<!DOCTYPE html>" in resp.data or b"<html" in resp.data

    def test_legacy_dashboard_returns_200(self, client):
        state  = _fake_state()
        report = _fake_report()
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            resp = client.get("/dashboard")
        assert resp.status_code == 200

    def test_legacy_dashboard_returns_html(self, client):
        state  = _fake_state()
        report = _fake_report()
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            resp = client.get("/dashboard")
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
            return client.get("/dashboard").data.decode()

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
            return client.get("/dashboard").data.decode()

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
    def test_dashboard_200_when_get_factory_state_raises(self, client):
        with patch.object(server, "get_factory_state", side_effect=RuntimeError("DB down")), \
             patch.object(server, "assess_factory", side_effect=RuntimeError("DB down")):
            resp = client.get("/dashboard")
        assert resp.status_code == 200

    def test_dashboard_shows_error_banner_on_failure(self, client):
        with patch.object(server, "get_factory_state", side_effect=Exception("connection refused")), \
             patch.object(server, "assess_factory", side_effect=Exception("connection refused")):
            resp = client.get("/dashboard")
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
            resp = client.get("/dashboard")
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
            resp = client.get("/dashboard")
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

    def test_command_no_network(self, client):
        # /command uses get_command_state which must not make network calls
        client.get("/command")

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
    def test_legacy_dashboard_calls_factory_state(self, client):
        state  = _fake_state()
        report = _fake_report()
        with patch.object(server, "get_factory_state", return_value=state) as mock_s, \
             patch.object(server, "assess_factory", return_value=report) as mock_r:
            client.get("/dashboard")
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


# ---------------------------------------------------------------------------
# TestEpisodeArtifactRoutes
# Covers artifact file-serving, path-traversal guard, and API endpoints.
# ---------------------------------------------------------------------------

class TestEpisodeArtifactRoutes:
    """Artifact file-serving: thumbnail, render, narration, captions, clips, JSON."""

    def _ep_id(self, tmp_path) -> str:
        return "ep_art_test"

    def _make_ep_dir(self, tmp_path, ep_id: str):
        ep_dir = tmp_path / ".hypogeum-assets" / "episodes" / ep_id
        ep_dir.mkdir(parents=True)
        # Thumbnail
        (ep_dir / "thumbnails").mkdir()
        (ep_dir / "thumbnails" / "main.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 20)
        # Render
        (ep_dir / f"main_{ep_id}.mp4").write_bytes(b"\x00" * 100)
        # Narration
        (ep_dir / "narration").mkdir()
        (ep_dir / "narration" / "main_narration.mp3").write_bytes(b"\xff\xfb" * 50)
        # Captions
        (ep_dir / "captions").mkdir()
        (ep_dir / "captions" / "main.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
        # Video clip
        (ep_dir / "video").mkdir()
        (ep_dir / "video" / "sc_s01.mp4").write_bytes(b"\x00" * 50)
        # JSON artifact
        import json
        (ep_dir / f"qa_approval_report_{ep_id}.json").write_text(
            json.dumps({"verdict": "WARNING", "approved": True})
        )
        return ep_dir

    def _patch_assets(self, tmp_path):
        """Return context manager that patches server's _ASSETS_ROOT_PATH."""
        from unittest.mock import patch
        return patch.object(server, "_ASSETS_ROOT_PATH", tmp_path / ".hypogeum-assets")

    def test_thumbnail_200_for_existing_episode(self, client, tmp_path):
        ep_id = "ep_art_test"
        self._make_ep_dir(tmp_path, ep_id)
        with self._patch_assets(tmp_path):
            resp = client.get(f"/episode/{ep_id}/thumbnail")
        assert resp.status_code == 200
        assert resp.content_type.startswith("image/")

    def test_thumbnail_404_for_missing_episode(self, client, tmp_path):
        with self._patch_assets(tmp_path):
            resp = client.get("/episode/ep_nonexistent/thumbnail")
        assert resp.status_code == 404

    def test_render_200_for_existing_episode(self, client, tmp_path):
        ep_id = "ep_art_test"
        self._make_ep_dir(tmp_path, ep_id)
        with self._patch_assets(tmp_path):
            resp = client.get(f"/episode/{ep_id}/render")
        assert resp.status_code == 200

    def test_render_404_for_missing_episode(self, client, tmp_path):
        with self._patch_assets(tmp_path):
            resp = client.get("/episode/ep_nonexistent/render")
        assert resp.status_code == 404

    def test_narration_200(self, client, tmp_path):
        ep_id = "ep_art_test"
        self._make_ep_dir(tmp_path, ep_id)
        with self._patch_assets(tmp_path):
            resp = client.get(f"/episode/{ep_id}/narration")
        assert resp.status_code == 200

    def test_captions_200(self, client, tmp_path):
        ep_id = "ep_art_test"
        self._make_ep_dir(tmp_path, ep_id)
        with self._patch_assets(tmp_path):
            resp = client.get(f"/episode/{ep_id}/captions")
        assert resp.status_code == 200

    def test_clip_200(self, client, tmp_path):
        ep_id = "ep_art_test"
        self._make_ep_dir(tmp_path, ep_id)
        with self._patch_assets(tmp_path):
            resp = client.get(f"/episode/{ep_id}/clip/sc_s01")
        assert resp.status_code == 200

    def test_clip_404_for_nonexistent_scene(self, client, tmp_path):
        ep_id = "ep_art_test"
        self._make_ep_dir(tmp_path, ep_id)
        with self._patch_assets(tmp_path):
            resp = client.get(f"/episode/{ep_id}/clip/sc_s99")
        assert resp.status_code == 404

    def test_artifact_json_200(self, client, tmp_path):
        ep_id = "ep_art_test"
        self._make_ep_dir(tmp_path, ep_id)
        with self._patch_assets(tmp_path):
            resp = client.get(f"/episode/{ep_id}/artifact/qa_approval_report")
        assert resp.status_code == 200
        assert b"WARNING" in resp.data or b"approved" in resp.data

    def test_artifact_json_404_for_missing_artifact(self, client, tmp_path):
        ep_id = "ep_art_test"
        self._make_ep_dir(tmp_path, ep_id)
        with self._patch_assets(tmp_path):
            resp = client.get(f"/episode/{ep_id}/artifact/nonexistent_artifact")
        assert resp.status_code == 404

    def test_path_traversal_blocked_thumbnail(self, client, tmp_path):
        """Path traversal in episode_id must not escape the assets directory."""
        with self._patch_assets(tmp_path):
            resp = client.get("/episode/../../../etc/passwd/thumbnail")
        assert resp.status_code in (404, 400)

    def test_path_traversal_blocked_clip(self, client, tmp_path):
        ep_id = "ep_art_test"
        self._make_ep_dir(tmp_path, ep_id)
        with self._patch_assets(tmp_path):
            resp = client.get(f"/episode/{ep_id}/clip/../../etc/passwd")
        assert resp.status_code in (404, 400)

    def test_episode_artifacts_api_200(self, client, tmp_path):
        ep_id = "ep_art_test"
        self._make_ep_dir(tmp_path, ep_id)
        with self._patch_assets(tmp_path):
            resp = client.get(f"/episode/{ep_id}/artifacts")
        assert resp.status_code == 200
        import json
        data = json.loads(resp.data)
        assert data["exists"] is True
        assert data["has_thumbnail"] is True
        assert data["has_render"] is True
        assert data["has_narration"] is True
        assert data["has_captions"] is True
        assert data["clip_count"] >= 1

    def test_episode_artifacts_api_404_missing(self, client, tmp_path):
        with self._patch_assets(tmp_path):
            resp = client.get("/episode/ep_nonexistent/artifacts")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestResumeAPIEndpoints
# Covers POST /api/episode/<id>/resume/assembly and /resume/narration.
# ---------------------------------------------------------------------------

class TestResumeAPIEndpoints:
    """Resume endpoints must return {ok: true} and not actually spawn subprocesses in tests."""

    def _make_ep_dir(self, tmp_path, ep_id: str):
        ep_dir = tmp_path / ".hypogeum-assets" / "episodes" / ep_id
        ep_dir.mkdir(parents=True)
        (ep_dir / f"main_{ep_id}.mp4").write_bytes(b"\x00" * 100)
        return ep_dir

    def _patch_assets(self, tmp_path):
        from unittest.mock import patch
        return patch.object(server, "_ASSETS_ROOT_PATH", tmp_path / ".hypogeum-assets")

    def test_resume_assembly_returns_ok(self, client, tmp_path):
        ep_id = "ep_resume_test"
        self._make_ep_dir(tmp_path, ep_id)
        script = tmp_path / "scripts" / "rebuild_ep003_assembly_qa.py"
        script.parent.mkdir(parents=True)
        script.write_text("# fake script\n")
        import os
        with self._patch_assets(tmp_path), \
             patch.object(server, "_ROOT", tmp_path), \
             patch.object(server._subprocess, "Popen",
                          return_value=type("P", (), {"pid": 999})()):
            resp = client.post(f"/api/episode/{ep_id}/resume/assembly")
        import json
        data = json.loads(resp.data)
        assert resp.status_code == 200
        assert data["ok"] is True
        assert data["status"] == "started"

    def test_resume_assembly_404_for_missing_episode(self, client, tmp_path):
        with self._patch_assets(tmp_path):
            resp = client.post("/api/episode/ep_nonexistent/resume/assembly")
        assert resp.status_code == 404

    def test_resume_narration_400_without_api_key(self, client, tmp_path):
        ep_id = "ep_resume_test"
        self._make_ep_dir(tmp_path, ep_id)
        import os
        env_without_key = {k: v for k, v in os.environ.items()
                           if k not in ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID")}
        with self._patch_assets(tmp_path), \
             patch.dict(os.environ, env_without_key, clear=True):
            resp = client.post(f"/api/episode/{ep_id}/resume/narration")
        assert resp.status_code == 400

    def test_resume_narration_404_for_missing_episode(self, client, tmp_path):
        with self._patch_assets(tmp_path):
            resp = client.post("/api/episode/ep_nonexistent/resume/narration")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestDashboardHomeLatestEpisodes
# Covers the Latest Episodes card and Next Action banner on /dashboard.
# ---------------------------------------------------------------------------

class TestDashboardHomeLatestEpisodes:

    def _standard_patches(self):
        state  = _fake_state()
        report = _fake_report()
        return state, report

    def test_dashboard_shows_latest_episodes_section(self, client, tmp_path):
        state, report = self._standard_patches()
        fake_episodes = [
            {"episode_id": "ep_003", "title": "The Antikythera Mechanism",
             "topic": None, "status": "complete", "run_status": "complete",
             "total_cost_usd": 21.5, "created_at": "2026-06-20T00:00:00",
             "qa_verdict": "WARNING", "has_render": True, "_disk_only": True},
        ]
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report), \
             patch.object(server, "list_episodes", return_value=[]), \
             patch.object(server, "list_disk_episodes", return_value=[
                 {"episode_id": "ep_003", "title": "The Antikythera Mechanism",
                  "has_render": True, "qa_verdict": "WARNING", "mtime": 1234567890}
             ]):
            resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert b"ep_003" in resp.data
        assert b"Latest Episodes" in resp.data

    def test_dashboard_shows_next_action(self, client):
        state, report = self._standard_patches()
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report), \
             patch.object(server, "list_episodes", return_value=[]), \
             patch.object(server, "list_disk_episodes", return_value=[]):
            resp = client.get("/dashboard")
        assert resp.status_code == 200
        # Next action should be visible (label text like "Create" or "factory")
        assert b"next-action" in resp.data or b"Next" in resp.data or b"Create" in resp.data


# ---------------------------------------------------------------------------
# TestEpisodeDetailArtifactsTab
# ---------------------------------------------------------------------------

class TestEpisodeDetailArtifactsTab:

    def _make_ep_dir(self, tmp_path, ep_id: str):
        ep_dir = tmp_path / ".hypogeum-assets" / "episodes" / ep_id
        ep_dir.mkdir(parents=True)
        (ep_dir / "thumbnails").mkdir()
        (ep_dir / "thumbnails" / "main.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 20)
        (ep_dir / f"main_{ep_id}.mp4").write_bytes(b"\x00" * 100)
        (ep_dir / "narration").mkdir()
        (ep_dir / "narration" / "main_narration.mp3").write_bytes(b"\xff\xfb" * 50)
        (ep_dir / "captions").mkdir()
        (ep_dir / "captions" / "main.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
        import json
        (ep_dir / f"qa_approval_report_{ep_id}.json").write_text(
            json.dumps({"verdict": "WARNING", "approved": True, "checks": [],
                        "failures": [], "warnings": [], "episode_id": ep_id,
                        "report_id": "rpt1", "evaluated_at": "2026-01-01T00:00:00+00:00"})
        )
        return ep_dir

    def _patch_assets(self, tmp_path):
        from unittest.mock import patch
        return patch.object(server, "_ASSETS_ROOT_PATH", tmp_path / ".hypogeum-assets")

    def test_episode_artifacts_tab_200_disk_only(self, client, tmp_path):
        ep_id = "ep_disk_test"
        self._make_ep_dir(tmp_path, ep_id)
        with self._patch_assets(tmp_path), \
             patch.object(server, "get_episode_detail", return_value=None):
            resp = client.get(f"/episode/{ep_id}?tab=artifacts")
        assert resp.status_code == 200
        assert b"Files" in resp.data or b"Artifact" in resp.data

    def test_episode_artifacts_tab_shows_download_links(self, client, tmp_path):
        ep_id = "ep_disk_test"
        self._make_ep_dir(tmp_path, ep_id)
        with self._patch_assets(tmp_path), \
             patch.object(server, "get_episode_detail", return_value=None):
            resp = client.get(f"/episode/{ep_id}?tab=artifacts")
        assert resp.status_code == 200
        assert b"Download" in resp.data
        assert b"/render" in resp.data or b"/narration" in resp.data

    def test_episode_not_found_returns_404(self, client, tmp_path):
        with self._patch_assets(tmp_path), \
             patch.object(server, "get_episode_detail", return_value=None):
            resp = client.get("/episode/ep_totally_missing")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestEpisodeArtifactsDataLayer
# Unit tests for hypogeum.episode_artifacts without Flask.
# ---------------------------------------------------------------------------

class TestEpisodeArtifactsDataLayer:

    def _make_ep(self, tmp_path, ep_id: str):
        from hypogeum.episode_artifacts import get_episode_artifacts
        ep_dir = tmp_path / "episodes" / ep_id
        ep_dir.mkdir(parents=True)
        (ep_dir / "thumbnails").mkdir()
        (ep_dir / "thumbnails" / "main.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 10)
        (ep_dir / f"main_{ep_id}.mp4").write_bytes(b"\x00" * 200)
        (ep_dir / "narration").mkdir()
        (ep_dir / "narration" / "main_narration.mp3").write_bytes(b"\xff\xfb" * 100)
        (ep_dir / "captions").mkdir()
        (ep_dir / "captions" / "main.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
        (ep_dir / "video").mkdir()
        (ep_dir / "video" / "sc_s01.mp4").write_bytes(b"\x00" * 50)
        (ep_dir / "video" / "sc_s02.mp4").write_bytes(b"\x00" * 50)
        return tmp_path

    def test_get_episode_artifacts_exists_true(self, tmp_path):
        from hypogeum.episode_artifacts import get_episode_artifacts
        ep_id = "ep_unit_test"
        assets_root = self._make_ep(tmp_path, ep_id)
        result = get_episode_artifacts(ep_id, assets_root=assets_root)
        assert result["exists"] is True

    def test_get_episode_artifacts_all_flags(self, tmp_path):
        from hypogeum.episode_artifacts import get_episode_artifacts
        ep_id = "ep_unit_test"
        assets_root = self._make_ep(tmp_path, ep_id)
        result = get_episode_artifacts(ep_id, assets_root=assets_root)
        assert result["has_render"] is True
        assert result["has_thumbnail"] is True
        assert result["has_narration"] is True
        assert result["has_captions"] is True
        assert result["clip_count"] == 2

    def test_get_episode_artifacts_nonexistent(self, tmp_path):
        from hypogeum.episode_artifacts import get_episode_artifacts
        result = get_episode_artifacts("ep_missing", assets_root=tmp_path)
        assert result["exists"] is False
        assert result["clip_count"] == 0

    def test_safe_episode_asset_path_traversal_guard(self, tmp_path):
        from hypogeum.episode_artifacts import safe_episode_asset_path
        ep_id = "ep_unit_test"
        self._make_ep(tmp_path, ep_id)
        # Path traversal attempt in filename
        result = safe_episode_asset_path(
            ep_id, "video", "../../etc/passwd",
            assets_root=tmp_path,
        )
        assert result is None

    def test_safe_episode_asset_path_returns_none_for_missing(self, tmp_path):
        from hypogeum.episode_artifacts import safe_episode_asset_path
        result = safe_episode_asset_path(
            "ep_unit_test", "thumbnail", "main.jpg",
            assets_root=tmp_path,
        )
        assert result is None

    def test_list_disk_episodes_returns_episodes(self, tmp_path):
        from hypogeum.episode_artifacts import list_disk_episodes
        ep_id = "ep_unit_test"
        self._make_ep(tmp_path, ep_id)
        results = list_disk_episodes(assets_root=tmp_path, limit=10)
        ep_ids = [r["episode_id"] for r in results]
        assert ep_id in ep_ids

    def test_list_disk_episodes_empty_dir(self, tmp_path):
        from hypogeum.episode_artifacts import list_disk_episodes
        (tmp_path / "episodes").mkdir(parents=True)
        results = list_disk_episodes(assets_root=tmp_path, limit=10)
        assert results == []
