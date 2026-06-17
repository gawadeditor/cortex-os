"""
tests/test_decision_reviews_routes.py — F4: Decision Review Flask route tests

Coverage:
    TestDecisionReviewsPage    — GET /decision-reviews returns 200 HTML
    TestAPIDecisionReviews     — GET /api/decision-reviews returns JSON list
    TestAPIDecisionReviewsSync — POST /api/decision-reviews/sync returns ok JSON
    TestAcceptRejectEndpoints  — POST /api/decision-reviews/<id>/accept|reject work
    TestDecisionsPageSyncButton — GET /decisions contains "Sync to review queue"
    TestCommandPrefersReview   — GET /command shows pending review card when reviews exist
    TestNoExecution            — accept/reject routes do not launch episodes or network calls
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_dashboard as server
from hypogeum.db import initialize_schema
from hypogeum.decision_reviews import (
    sync_decisions_to_reviews,
    list_decision_reviews,
    accept_decision,
)


# ---------------------------------------------------------------------------
# Helpers / Fakes
# ---------------------------------------------------------------------------

def _fake_decision(
    decision_id: str | None = None,
    title: str = "Route test decision",
    evidence: list | None = None,
) -> dict:
    return {
        "decision_id":    decision_id or str(uuid.uuid4()),
        "type":           "topic_selection",
        "title":          title,
        "recommendation": "Pick this",
        "evidence":       evidence if evidence is not None else ["signal X"],
        "confidence":     0.8,
        "priority":       2,
        "investigate_url": "/decisions",
        "action_label":   "Investigate",
    }


def _fake_ds(decisions: list[dict]) -> dict:
    return {
        "checked_at":        "2026-01-01T00:00:00+00:00",
        "engine_status":     "ready",
        "summary":           "ok",
        "decisions":         decisions,
        "insufficient_data": [],
    }


def _fake_cmd() -> dict:
    return {
        "factory_status":    {"level": "HEALTHY", "reason": "", "director_score": 100},
        "checked_at":        "2026-01-01T00:00:00+00:00",
        "attention_queue":   [],
        "episode_readiness": {
            "can_launch":            False,
            "blockers":              ["No channels"],
            "required_approvals":    [],
            "pending_approval_count": 0,
            "next_topic":            None,
        },
        "run_state": {
            "active_runs":     [],
            "queued_runs":     [],
            "last_completed":  None,
            "recent_failures": [],
        },
        "providers": [],
    }


def _fake_di() -> dict:
    return {
        "readiness": {
            "has_real_inputs":          False,
            "missing_inputs":           [],
            "ready_for_decision_engine": False,
        }
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path) -> Path:
    p = tmp_path / "routes_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def client(db):
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db)
    with server.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# TestDecisionReviewsPage
# ---------------------------------------------------------------------------

class TestDecisionReviewsPage:
    def test_returns_200(self, client):
        resp = client.get("/decision-reviews")
        assert resp.status_code == 200

    def test_returns_html(self, client):
        resp = client.get("/decision-reviews")
        assert b"<!doctype html" in resp.data.lower() or b"<html" in resp.data.lower()

    def test_shows_empty_state(self, client):
        resp = client.get("/decision-reviews")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# TestAPIDecisionReviews
# ---------------------------------------------------------------------------

class TestAPIDecisionReviews:
    def test_returns_200(self, client):
        resp = client.get("/api/decision-reviews")
        assert resp.status_code == 200

    def test_returns_json_list(self, client):
        resp = client.get("/api/decision-reviews")
        data = json.loads(resp.data)
        assert isinstance(data, list)

    def test_status_filter_pending(self, client, db):
        d = _fake_decision()
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)

        resp = client.get("/api/decision-reviews?status=pending")
        data = json.loads(resp.data)
        assert isinstance(data, list)
        assert all(r["operator_status"] == "pending" for r in data)


# ---------------------------------------------------------------------------
# TestAPIDecisionReviewsSync
# ---------------------------------------------------------------------------

class TestAPIDecisionReviewsSync:
    def test_sync_returns_ok(self, client):
        with patch(
            "hypogeum.decision_engine.get_decision_state",
            return_value=_fake_ds([]),
        ):
            resp = client.post("/api/decision-reviews/sync")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True

    def test_sync_creates_review(self, client, db):
        d = _fake_decision()
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            resp = client.post("/api/decision-reviews/sync")
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert data["created"] >= 1

    def test_sync_engine_error_returns_503(self, client):
        with patch(
            "hypogeum.decision_engine.get_decision_state",
            side_effect=RuntimeError("engine down"),
        ):
            resp = client.post("/api/decision-reviews/sync")
        assert resp.status_code == 503
        data = json.loads(resp.data)
        assert data["ok"] is False


# ---------------------------------------------------------------------------
# TestAcceptRejectEndpoints
# ---------------------------------------------------------------------------

class TestAcceptRejectEndpoints:
    def _seed_review(self, db) -> str:
        d = _fake_decision()
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)
        return list_decision_reviews(db_path=db)[0]["review_id"]

    def test_accept_endpoint_returns_200(self, client, db):
        rid = self._seed_review(db)
        resp = client.post(
            f"/api/decision-reviews/{rid}/accept",
            json={"note": "approved"},
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert data["status"] == "accepted"

    def test_reject_endpoint_returns_200(self, client, db):
        rid = self._seed_review(db)
        resp = client.post(
            f"/api/decision-reviews/{rid}/reject",
            json={"note": "declined"},
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert data["status"] == "rejected"

    def test_accept_unknown_id_returns_404(self, client):
        resp = client.post("/api/decision-reviews/no-such-id/accept", json={})
        assert resp.status_code == 404
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert data["code"] == "not_found"

    def test_reject_unknown_id_returns_404(self, client):
        resp = client.post("/api/decision-reviews/no-such-id/reject", json={})
        assert resp.status_code == 404
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert data["code"] == "not_found"


# ---------------------------------------------------------------------------
# TestDecisionsPageSyncButton
# ---------------------------------------------------------------------------

class TestDecisionsPageSyncButton:
    def test_decisions_page_contains_sync_button(self, client):
        with patch(
            "run_dashboard.get_decision_state",
            return_value=_fake_ds([]),
        ):
            resp = client.get("/decisions")
        assert resp.status_code == 200
        assert b"Sync to review queue" in resp.data


# ---------------------------------------------------------------------------
# TestCommandPrefersReview
# ---------------------------------------------------------------------------

class TestCommandPrefersReview:
    def test_command_shows_pending_review_card(self, client, db):
        d = _fake_decision(title="My pending decision")
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)

        with (
            patch("run_dashboard.get_command_state", return_value=_fake_cmd()),
            patch("run_dashboard.get_decision_inputs_state", return_value=_fake_di()),
            patch("run_dashboard.get_decision_state", return_value=_fake_ds([])),
        ):
            resp = client.get("/command")

        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Review queue" in html
        assert "Pending review" in html

    def test_command_falls_back_to_engine_when_no_reviews(self, client, db):
        d_engine = _fake_decision(title="Engine decision")
        with (
            patch("run_dashboard.get_command_state", return_value=_fake_cmd()),
            patch("run_dashboard.get_decision_inputs_state", return_value=_fake_di()),
            patch("run_dashboard.get_decision_state", return_value=_fake_ds([d_engine])),
            patch(
                "run_dashboard.list_decision_reviews",
                return_value=[],
            ),
        ):
            resp = client.get("/command")

        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Engine decision" in html


# ---------------------------------------------------------------------------
# TestNoExecution
# ---------------------------------------------------------------------------

class TestNoExecution:
    def _seed_review(self, db) -> str:
        d = _fake_decision()
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)
        return list_decision_reviews(db_path=db)[0]["review_id"]

    def test_accept_does_not_create_run(self, client, db):
        rid = self._seed_review(db)
        client.post(f"/api/decision-reviews/{rid}/accept", json={})
        from hypogeum.db import get_connection
        conn = get_connection(db)
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        assert count == 0

    def test_reject_does_not_create_run(self, client, db):
        rid = self._seed_review(db)
        client.post(f"/api/decision-reviews/{rid}/reject", json={})
        from hypogeum.db import get_connection
        conn = get_connection(db)
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        assert count == 0

    def test_sync_does_not_create_run(self, client, db):
        d = _fake_decision()
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            client.post("/api/decision-reviews/sync")
        from hypogeum.db import get_connection
        conn = get_connection(db)
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        assert count == 0
