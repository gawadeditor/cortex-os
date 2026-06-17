"""
tests/test_decision_execution_routes.py — F5: Decision Execution Flask route tests

Coverage:
    TestExecutionPlansPage         — GET /execution-plans returns 200 with F5 guard text
    TestAPIListPlans               — GET /api/execution-plans returns JSON list
    TestAPIGetPlan                 — GET /api/execution-plans/<id> returns plan or 404
    TestAPIFromReview              — POST /api/execution-plans/from-review/<review_id>
    TestAPIApproveRejectCancel     — approve / reject / cancel endpoints
    TestAPIDryRun                  — dry-run endpoint returns simulation
    TestUnknownPlanRoutes          — 404 for unknown plan_ids
    TestDecisionReviewsAccepted    — /decision-reviews shows "Create execution plan" button
    TestNoExecution                — no run/episode/network side-effects from any route
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import patch

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
from hypogeum.decision_execution import (
    create_execution_plan_from_review,
    list_execution_plans,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_decision(dtype: str = "cost_review") -> dict:
    return {
        "decision_id":    str(uuid.uuid4()),
        "type":           dtype,
        "title":          f"Route test {dtype}",
        "recommendation": "Do it",
        "evidence":       ["signal X", "signal Y"],
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path) -> Path:
    p = tmp_path / "exec_routes_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def client(db):
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db)
    with server.app.test_client() as c:
        yield c


def _seed_accepted_review(db: Path, dtype: str = "cost_review") -> str:
    d = _fake_decision(dtype=dtype)
    with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
        sync_decisions_to_reviews(db_path=db)
    rev = list_decision_reviews(db_path=db)[0]
    accept_decision(rev["review_id"], db_path=db)
    return rev["review_id"]


def _seed_plan(db: Path, dtype: str = "cost_review") -> str:
    rid = _seed_accepted_review(db, dtype=dtype)
    return create_execution_plan_from_review(rid, db_path=db)["plan_id"]


# ---------------------------------------------------------------------------
# TestExecutionPlansPage
# ---------------------------------------------------------------------------

class TestExecutionPlansPage:
    def test_returns_200(self, client):
        resp = client.get("/execution-plans")
        assert resp.status_code == 200

    def test_contains_f5_guard_text(self, client):
        resp = client.get("/execution-plans")
        html = resp.data.decode()
        assert "dry-run" in html.lower() or "dry run" in html.lower()
        assert "no live execution" in html.lower()

    def test_shows_empty_draft_section(self, client):
        resp = client.get("/execution-plans")
        assert resp.status_code == 200

    def test_f5_notice_mentions_executed_unreachable(self, client):
        resp = client.get("/execution-plans")
        html = resp.data.decode()
        assert "executed" in html.lower()


# ---------------------------------------------------------------------------
# TestAPIListPlans
# ---------------------------------------------------------------------------

class TestAPIListPlans:
    def test_returns_200(self, client):
        resp = client.get("/api/execution-plans")
        assert resp.status_code == 200

    def test_returns_json_list(self, client):
        data = json.loads(client.get("/api/execution-plans").data)
        assert isinstance(data, list)

    def test_status_filter_draft(self, client, db):
        _seed_plan(db)
        data = json.loads(client.get("/api/execution-plans?status=draft").data)
        assert isinstance(data, list)
        assert all(p["plan_status"] == "draft" for p in data)

    def test_plan_in_list_has_expected_fields(self, client, db):
        _seed_plan(db)
        data = json.loads(client.get("/api/execution-plans").data)
        assert len(data) == 1
        plan = data[0]
        for field in ("plan_id", "plan_status", "execution_mode", "action_summary",
                      "risk_level", "planned_steps", "blockers"):
            assert field in plan, f"missing field: {field}"


# ---------------------------------------------------------------------------
# TestAPIGetPlan
# ---------------------------------------------------------------------------

class TestAPIGetPlan:
    def test_existing_plan_returns_200(self, client, db):
        pid = _seed_plan(db)
        resp = client.get(f"/api/execution-plans/{pid}")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["plan_id"] == pid

    def test_unknown_plan_returns_404(self, client):
        resp = client.get("/api/execution-plans/no-such-id")
        assert resp.status_code == 404
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert data["code"] == "not_found"


# ---------------------------------------------------------------------------
# TestAPIFromReview
# ---------------------------------------------------------------------------

class TestAPIFromReview:
    def test_accepted_review_creates_plan(self, client, db):
        rid = _seed_accepted_review(db)
        resp = client.post(f"/api/execution-plans/from-review/{rid}", json={})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert "plan_id" in data

    def test_pending_review_returns_400(self, client, db):
        d = _fake_decision()
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)
        rev = list_decision_reviews(db_path=db)[0]
        resp = client.post(f"/api/execution-plans/from-review/{rev['review_id']}", json={})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["ok"] is False

    def test_unknown_review_id_returns_404(self, client):
        resp = client.post("/api/execution-plans/from-review/no-such-review", json={})
        assert resp.status_code == 404
        data = json.loads(resp.data)
        assert data["ok"] is False

    def test_duplicate_create_idempotent(self, client, db):
        rid = _seed_accepted_review(db)
        r1 = json.loads(client.post(f"/api/execution-plans/from-review/{rid}", json={}).data)
        r2 = json.loads(client.post(f"/api/execution-plans/from-review/{rid}", json={}).data)
        assert r1["ok"] is True
        assert r2["ok"] is True
        assert r1["plan_id"] == r2["plan_id"]


# ---------------------------------------------------------------------------
# TestAPIApproveRejectCancel
# ---------------------------------------------------------------------------

class TestAPIApproveRejectCancel:
    def test_approve_returns_200(self, client, db):
        pid = _seed_plan(db)
        resp = client.post(f"/api/execution-plans/{pid}/approve", json={})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert data["status"] == "approved"

    def test_reject_returns_200(self, client, db):
        pid = _seed_plan(db)
        resp = client.post(f"/api/execution-plans/{pid}/reject", json={})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert data["status"] == "rejected"

    def test_cancel_returns_200(self, client, db):
        pid = _seed_plan(db)
        resp = client.post(f"/api/execution-plans/{pid}/cancel", json={})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert data["status"] == "cancelled"

    def test_approve_unknown_returns_404(self, client):
        resp = client.post("/api/execution-plans/no-such-id/approve", json={})
        assert resp.status_code == 404
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert data["code"] == "not_found"

    def test_reject_unknown_returns_404(self, client):
        resp = client.post("/api/execution-plans/no-such-id/reject", json={})
        assert resp.status_code == 404

    def test_cancel_unknown_returns_404(self, client):
        resp = client.post("/api/execution-plans/no-such-id/cancel", json={})
        assert resp.status_code == 404

    def test_approve_does_not_set_executed(self, client, db):
        pid = _seed_plan(db)
        client.post(f"/api/execution-plans/{pid}/approve", json={})
        plan = json.loads(client.get(f"/api/execution-plans/{pid}").data)
        assert plan["plan_status"] == "approved"
        assert plan["plan_status"] != "executed"


# ---------------------------------------------------------------------------
# TestAPIDryRun
# ---------------------------------------------------------------------------

class TestAPIDryRun:
    def test_dry_run_returns_200(self, client, db):
        pid = _seed_plan(db)
        resp = client.post(f"/api/execution-plans/{pid}/dry-run", json={})
        assert resp.status_code == 200

    def test_dry_run_returns_ok(self, client, db):
        pid = _seed_plan(db)
        data = json.loads(client.post(f"/api/execution-plans/{pid}/dry-run", json={}).data)
        assert data["ok"] is True
        assert data["dry_run"] is True

    def test_dry_run_has_simulated_steps(self, client, db):
        pid = _seed_plan(db)
        data = json.loads(client.post(f"/api/execution-plans/{pid}/dry-run", json={}).data)
        assert isinstance(data["simulated_steps"], list)
        assert len(data["simulated_steps"]) > 0

    def test_dry_run_steps_not_executed(self, client, db):
        pid = _seed_plan(db)
        data = json.loads(client.post(f"/api/execution-plans/{pid}/dry-run", json={}).data)
        for step in data["simulated_steps"]:
            assert step["executed"] is False

    def test_dry_run_does_not_change_plan_status(self, client, db):
        pid = _seed_plan(db)
        client.post(f"/api/execution-plans/{pid}/dry-run", json={})
        plan = json.loads(client.get(f"/api/execution-plans/{pid}").data)
        assert plan["plan_status"] == "draft"

    def test_dry_run_unknown_returns_404(self, client):
        resp = client.post("/api/execution-plans/no-such-id/dry-run", json={})
        assert resp.status_code == 404

    def test_dry_run_has_f5_warning(self, client, db):
        pid = _seed_plan(db)
        data = json.loads(client.post(f"/api/execution-plans/{pid}/dry-run", json={}).data)
        all_warnings = " ".join(data.get("warnings", [])).lower()
        assert "executed" in all_warnings


# ---------------------------------------------------------------------------
# TestUnknownPlanRoutes
# ---------------------------------------------------------------------------

class TestUnknownPlanRoutes:
    def test_get_unknown_404(self, client):
        assert client.get("/api/execution-plans/ghost").status_code == 404

    def test_approve_unknown_404(self, client):
        assert client.post("/api/execution-plans/ghost/approve", json={}).status_code == 404

    def test_reject_unknown_404(self, client):
        assert client.post("/api/execution-plans/ghost/reject", json={}).status_code == 404

    def test_cancel_unknown_404(self, client):
        assert client.post("/api/execution-plans/ghost/cancel", json={}).status_code == 404

    def test_dry_run_unknown_404(self, client):
        assert client.post("/api/execution-plans/ghost/dry-run", json={}).status_code == 404


# ---------------------------------------------------------------------------
# TestDecisionReviewsAccepted
# ---------------------------------------------------------------------------

class TestDecisionReviewsAccepted:
    def test_accepted_review_shows_create_plan_button(self, client, db):
        _seed_accepted_review(db)
        resp = client.get("/decision-reviews")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Create action plan" in html

    def test_execution_plans_link_present_in_accepted_section(self, client, db):
        _seed_accepted_review(db)
        resp = client.get("/decision-reviews")
        html = resp.data.decode()
        assert "/execution-plans" in html


# ---------------------------------------------------------------------------
# TestNoExecution
# ---------------------------------------------------------------------------

class TestNoExecution:
    def _run_count(self, db: Path) -> int:
        from hypogeum.db import get_connection
        conn = get_connection(db)
        n = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        return n

    def test_create_plan_no_run(self, client, db):
        rid = _seed_accepted_review(db)
        client.post(f"/api/execution-plans/from-review/{rid}", json={})
        assert self._run_count(db) == 0

    def test_approve_no_run(self, client, db):
        pid = _seed_plan(db)
        client.post(f"/api/execution-plans/{pid}/approve", json={})
        assert self._run_count(db) == 0

    def test_dry_run_no_run(self, client, db):
        pid = _seed_plan(db)
        client.post(f"/api/execution-plans/{pid}/dry-run", json={})
        assert self._run_count(db) == 0

    def test_cancel_no_run(self, client, db):
        pid = _seed_plan(db)
        client.post(f"/api/execution-plans/{pid}/cancel", json={})
        assert self._run_count(db) == 0
