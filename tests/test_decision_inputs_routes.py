"""
tests/test_decision_inputs_routes.py — F2: Decision Inputs routes tests

Coverage:
    TestAPIDecisionInputs    — GET /api/decision-inputs JSON shape
    TestDecisionInputsPage   — GET /decision-inputs HTML 200
    TestCommandIntegration   — /command includes decision inputs status card
    TestNoExternalCalls      — network blocked throughout
    TestNoSideEffects        — routes are read-only (no episode launch, no publish)
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_dashboard as server

from hypogeum.db import initialize_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path) -> Path:
    p = tmp_path / "di_routes_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def client(db):
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db)
    with server.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    _orig = socket.socket
    def _no(*a, **kw):
        raise RuntimeError("Network forbidden in route tests")
    monkeypatch.setattr(socket, "socket", _no)
    yield
    monkeypatch.setattr(socket, "socket", _orig)


# ---------------------------------------------------------------------------
# TestAPIDecisionInputs
# ---------------------------------------------------------------------------

class TestAPIDecisionInputs:
    def test_returns_200(self, client):
        resp = client.get("/api/decision-inputs")
        assert resp.status_code == 200

    def test_content_type_json(self, client):
        resp = client.get("/api/decision-inputs")
        assert "application/json" in resp.content_type

    def test_has_required_top_level_keys(self, client):
        resp = client.get("/api/decision-inputs")
        data = json.loads(resp.data)
        for key in ("checked_at", "discovery", "episodes", "publishing",
                    "costs", "audience", "readiness"):
            assert key in data, f"missing key: {key}"

    def test_readiness_has_required_keys(self, client):
        resp = client.get("/api/decision-inputs")
        data = json.loads(resp.data)
        r = data["readiness"]
        assert "has_real_inputs"           in r
        assert "missing_inputs"            in r
        assert "ready_for_decision_engine" in r

    def test_no_mock_analytics_at_top_level(self, client):
        resp = client.get("/api/decision-inputs")
        data = json.loads(resp.data)
        forbidden = {"recommendations", "synthetic_data", "mock_analytics"}
        for key in forbidden:
            assert key not in data, f"forbidden key in response: {key!r}"

    def test_audience_has_real_vs_mock_separation(self, client):
        resp = client.get("/api/decision-inputs")
        data = json.loads(resp.data)
        aud = data["audience"]
        assert "real_snapshots"  in aud
        assert "mock_snapshots"  in aud
        assert "has_real_data"   in aud

    def test_discovery_has_required_fields(self, client):
        resp = client.get("/api/decision-inputs")
        data = json.loads(resp.data)
        disc = data["discovery"]
        assert "total_topics"   in disc
        assert "by_status"      in disc
        assert "has_data"       in disc

    def test_empty_db_returns_valid_response(self, client):
        resp = client.get("/api/decision-inputs")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["readiness"]["ready_for_decision_engine"] is False


# ---------------------------------------------------------------------------
# TestDecisionInputsPage
# ---------------------------------------------------------------------------

class TestDecisionInputsPage:
    def test_returns_200(self, client):
        resp = client.get("/decision-inputs")
        assert resp.status_code == 200

    def test_returns_html(self, client):
        resp = client.get("/decision-inputs")
        html = resp.data.decode()
        assert "<!doctype html>" in html.lower() or "<html" in html.lower()

    def test_page_has_decision_inputs_title(self, client):
        resp = client.get("/decision-inputs")
        assert b"Decision Inputs" in resp.data

    def test_page_has_readiness_section(self, client):
        resp = client.get("/decision-inputs")
        assert b"Readiness" in resp.data or b"readiness" in resp.data

    def test_page_has_discovery_section(self, client):
        resp = client.get("/decision-inputs")
        assert b"Discovery" in resp.data

    def test_page_has_audience_section(self, client):
        resp = client.get("/decision-inputs")
        assert b"Audience" in resp.data

    def test_page_has_costs_section(self, client):
        resp = client.get("/decision-inputs")
        assert b"Cost" in resp.data

    def test_page_has_publishing_section(self, client):
        resp = client.get("/decision-inputs")
        assert b"Publishing" in resp.data

    def test_page_shows_mock_warning_when_mock_only(self, client, db):
        from hypogeum.db import get_connection
        import uuid
        conn = get_connection(db)
        conn.execute("""
            INSERT OR REPLACE INTO platform_analytics
              (record_id, episode_id, platform, snapshot_date, window_days,
               views, source, imported_at)
            VALUES (?, 'ep_1', 'youtube', '2026-01-01', 7, 1000, 'mock',
                    '2026-01-01T00:00:00Z')
        """, (str(uuid.uuid4()),))
        conn.commit(); conn.close()
        resp = client.get("/decision-inputs")
        html = resp.data.decode()
        assert "mock" in html.lower()

    def test_no_mock_data_as_truth(self, client):
        resp = client.get("/decision-inputs")
        html = resp.data.decode().lower()
        # Should never present mock data as factual without labeling
        assert "mock_analytics" not in html
        assert "synthetic_insight" not in html

    def test_page_has_investigate_links(self, client):
        resp = client.get("/decision-inputs")
        html = resp.data.decode()
        assert "/command" in html
        assert "/analytics" in html


# ---------------------------------------------------------------------------
# TestCommandIntegration
# ---------------------------------------------------------------------------

class TestCommandIntegration:
    def test_command_page_includes_decision_inputs_status(self, client):
        resp = client.get("/command")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Decision inputs" in html

    def test_command_page_has_link_to_decision_inputs(self, client):
        resp = client.get("/command")
        html = resp.data.decode()
        assert "/decision-inputs" in html

    def test_command_page_no_fake_recommendations(self, client):
        resp = client.get("/command")
        html = resp.data.decode().lower()
        for forbidden in ("synthetic_insight", "mock_recommendation",
                          "auto_generated_recommendation"):
            assert forbidden not in html


# ---------------------------------------------------------------------------
# TestNoExternalCalls
# ---------------------------------------------------------------------------

class TestNoExternalCalls:
    def test_api_no_network(self, client):
        resp = client.get("/api/decision-inputs")
        assert resp.status_code == 200

    def test_page_no_network(self, client):
        resp = client.get("/decision-inputs")
        assert resp.status_code == 200

    def test_command_no_network(self, client):
        resp = client.get("/command")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# TestNoSideEffects
# ---------------------------------------------------------------------------

class TestNoSideEffects:
    def test_api_does_not_launch_episode(self, client, db):
        from hypogeum.db import get_connection
        conn = get_connection(db)
        before = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()
        client.get("/api/decision-inputs")
        conn = get_connection(db)
        after = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()
        assert before == after

    def test_api_does_not_write_publishing(self, client, db):
        from hypogeum.db import get_connection
        conn = get_connection(db)
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM publishing_outcomes"
        ).fetchone()["c"]
        conn.close()
        client.get("/api/decision-inputs")
        conn = get_connection(db)
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM publishing_outcomes"
        ).fetchone()["c"]
        conn.close()
        assert before == after

    def test_page_does_not_launch_episode(self, client, db):
        from hypogeum.db import get_connection
        conn = get_connection(db)
        before = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        conn.close()
        client.get("/decision-inputs")
        conn = get_connection(db)
        after = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        conn.close()
        assert before == after
