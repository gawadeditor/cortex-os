"""
tests/test_decision_engine_routes.py — F3: Decision Engine route tests

Coverage:
    TestAPIDecisions         — GET /api/decisions JSON shape
    TestDecisionsPage        — GET /decisions HTML 200 + sections
    TestCommandIntegration   — /command shows next decision card
    TestNoExternalCalls      — network blocked throughout
    TestNoSideEffects        — routes are read-only
"""

from __future__ import annotations

import json
import socket
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_dashboard as server

from hypogeum.db import get_connection, initialize_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path) -> Path:
    p = tmp_path / "de_routes_test.db"
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
# TestAPIDecisions
# ---------------------------------------------------------------------------

class TestAPIDecisions:
    def test_returns_200(self, client):
        assert client.get("/api/decisions").status_code == 200

    def test_content_type_json(self, client):
        resp = client.get("/api/decisions")
        assert "application/json" in resp.content_type

    def test_has_required_keys(self, client):
        data = json.loads(client.get("/api/decisions").data)
        for key in ("checked_at", "engine_status", "summary",
                    "decisions", "insufficient_data"):
            assert key in data

    def test_engine_status_is_known_value(self, client):
        data = json.loads(client.get("/api/decisions").data)
        assert data["engine_status"] in ("not_ready", "partial", "ready")

    def test_empty_db_returns_not_ready(self, client):
        data = json.loads(client.get("/api/decisions").data)
        assert data["engine_status"] == "not_ready"

    def test_decisions_is_list(self, client):
        data = json.loads(client.get("/api/decisions").data)
        assert isinstance(data["decisions"], list)

    def test_insufficient_data_is_list(self, client):
        data = json.loads(client.get("/api/decisions").data)
        assert isinstance(data["insufficient_data"], list)

    def test_empty_db_has_insufficient_entries(self, client):
        data = json.loads(client.get("/api/decisions").data)
        assert len(data["insufficient_data"]) > 0

    def test_decision_cards_have_all_required_fields(self, client, db):
        # Seed a queued topic to generate a decision
        conn = get_connection(db)
        conn.execute("""
            INSERT INTO episode_topics
              (topic_id, channel_id, topic_text, status, priority, created_at, updated_at)
            VALUES (?, 'ch1', 'test topic', 'queued', 0,
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """, (str(uuid.uuid4()),))
        conn.commit(); conn.close()
        data = json.loads(client.get("/api/decisions").data)
        for d in data["decisions"]:
            for key in ("decision_id", "type", "priority", "title",
                        "recommendation", "evidence", "expected_impact",
                        "confidence", "status", "action_label",
                        "reject_label", "investigate_url"):
                assert key in d

    def test_no_mock_analytics_at_top_level(self, client):
        data = json.loads(client.get("/api/decisions").data)
        for key in ("mock_analytics", "synthetic_data", "fake_recommendations"):
            assert key not in data

    def test_insufficient_data_entries_have_type_and_reason(self, client):
        data = json.loads(client.get("/api/decisions").data)
        for i in data["insufficient_data"]:
            assert "type"   in i
            assert "reason" in i


# ---------------------------------------------------------------------------
# TestDecisionsPage
# ---------------------------------------------------------------------------

class TestDecisionsPage:
    def test_returns_200(self, client):
        assert client.get("/decisions").status_code == 200

    def test_returns_html(self, client):
        html = client.get("/decisions").data.decode()
        assert "<!doctype html>" in html.lower() or "<html" in html.lower()

    def test_page_has_decisions_title(self, client):
        assert b"Decisions" in client.get("/decisions").data

    def test_page_has_engine_status_indicator(self, client):
        html = client.get("/decisions").data.decode()
        assert "not ready" in html.lower() or "ready" in html.lower() or "partial" in html.lower()

    def test_page_has_insufficient_data_section_when_empty(self, client):
        html = client.get("/decisions").data.decode()
        assert "Insufficient data" in html or "insufficient" in html.lower()

    def test_page_shows_review_only_notice(self, client):
        html = client.get("/decisions").data.decode()
        assert "review" in html.lower()

    def test_page_links_to_decision_inputs(self, client):
        html = client.get("/decisions").data.decode()
        assert "/decision-inputs" in html

    def test_page_links_to_api_endpoint(self, client):
        html = client.get("/decisions").data.decode()
        assert "/api/decisions" in html

    def test_page_no_fake_recommendations(self, client):
        html = client.get("/decisions").data.decode().lower()
        for phrase in ("synthetic_insight", "mock_recommendation",
                       "auto_generated", "produce more ancient"):
            assert phrase not in html

    def test_page_shows_decision_when_topics_queued(self, client, db):
        conn = get_connection(db)
        conn.execute("""
            INSERT INTO episode_topics
              (topic_id, channel_id, topic_text, status, priority, created_at, updated_at)
            VALUES (?, 'ch1', 'My test topic', 'queued', 0,
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """, (str(uuid.uuid4()),))
        conn.commit(); conn.close()
        html = client.get("/decisions").data.decode()
        assert "queued" in html.lower() or "launch" in html.lower()

    def test_page_shows_failure_decision_when_failures_exist(self, client, db):
        conn = get_connection(db)
        for i in range(2):
            conn.execute("""
                INSERT INTO runs (run_id, episode_id, started_at, status, error_message)
                VALUES (?, ?, '2026-01-01T10:00:00Z', 'failed', 'error msg')
            """, (f"run_f{i}", f"ep_f{i:02d}"))
        conn.commit(); conn.close()
        html = client.get("/decisions").data.decode()
        assert "failure" in html.lower() or "failed" in html.lower()


# ---------------------------------------------------------------------------
# TestCommandIntegration
# ---------------------------------------------------------------------------

class TestCommandIntegration:
    def test_command_page_includes_next_decision_section(self, client):
        resp = client.get("/command")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Next decision" in html

    def test_command_shows_engine_not_ready_on_empty_db(self, client):
        html = client.get("/command").data.decode()
        assert "not ready" in html.lower() or "not_ready" in html.lower()

    def test_command_shows_decision_when_topics_queued(self, client, db):
        conn = get_connection(db)
        conn.execute("""
            INSERT INTO episode_topics
              (topic_id, channel_id, topic_text, status, priority, created_at, updated_at)
            VALUES (?, 'ch1', 'test topic', 'queued', 0,
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """, (str(uuid.uuid4()),))
        conn.commit(); conn.close()
        html = client.get("/command").data.decode()
        # Should show the decision title or the engine state
        assert "decision" in html.lower()

    def test_command_does_not_show_fake_recommendations(self, client):
        html = client.get("/command").data.decode().lower()
        for phrase in ("synthetic_insight", "mock_recommendation",
                       "produce more ancient"):
            assert phrase not in html

    def test_command_has_link_to_decisions_page(self, client):
        html = client.get("/command").data.decode()
        assert "/decisions" in html


# ---------------------------------------------------------------------------
# TestNoExternalCalls
# ---------------------------------------------------------------------------

class TestNoExternalCalls:
    def test_api_no_network(self, client):
        assert client.get("/api/decisions").status_code == 200

    def test_page_no_network(self, client):
        assert client.get("/decisions").status_code == 200

    def test_command_no_network(self, client):
        assert client.get("/command").status_code == 200


# ---------------------------------------------------------------------------
# TestNoSideEffects
# ---------------------------------------------------------------------------

class TestNoSideEffects:
    def test_api_does_not_write_episodes(self, client, db):
        conn = get_connection(db)
        before = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()
        client.get("/api/decisions")
        conn = get_connection(db)
        after = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()
        assert before == after

    def test_api_does_not_write_runs(self, client, db):
        conn = get_connection(db)
        before = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        conn.close()
        client.get("/api/decisions")
        conn = get_connection(db)
        after = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        conn.close()
        assert before == after

    def test_api_does_not_publish(self, client, db):
        conn = get_connection(db)
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM publishing_outcomes"
        ).fetchone()["c"]
        conn.close()
        client.get("/api/decisions")
        conn = get_connection(db)
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM publishing_outcomes"
        ).fetchone()["c"]
        conn.close()
        assert before == after
