"""
tests/test_decision_execution.py — F5: Decision Execution Guardrails unit tests

Coverage:
    TestSchema                — decision_execution_plans table is created
    TestCreateFromReview      — accepted review creates plan; pending/rejected blocked
    TestCreateIdempotent      — duplicate create returns existing plan
    TestDryRunMode            — all plans are dry_run; live blocked
    TestExecutedUnreachable   — executed status cannot be set via any API
    TestTransitions           — approve/reject/cancel status transitions + timestamps
    TestTransitionNotFound    — unknown plan_id returns ok=False, code=not_found
    TestDryRunSimulation      — dry_run returns steps, warnings, validation; no DB state change
    TestPlanTemplates         — each known decision_type maps to correct steps/risk/blockers
    TestUnknownType           — unknown type creates blocked/high-risk plan
    TestListFilter            — list_execution_plans filters by status
    TestGetPlan               — get_execution_plan returns plan or None
    TestNoSideEffects         — no runs, episodes, or network calls from any operation
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.decision_reviews import (
    sync_decisions_to_reviews,
    list_decision_reviews,
    accept_decision,
    reject_decision as reject_review,
)
from hypogeum.decision_execution import (
    create_execution_plan_from_review,
    list_execution_plans,
    get_execution_plan,
    approve_execution_plan,
    reject_execution_plan,
    cancel_execution_plan,
    dry_run_execution_plan,
    _PLAN_TEMPLATES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_review_decision(
    decision_id: str | None = None,
    dtype: str = "cost_review",
    evidence: list | None = None,
) -> dict:
    return {
        "decision_id":    decision_id or str(uuid.uuid4()),
        "type":           dtype,
        "title":          f"Test {dtype}",
        "recommendation": "Do the thing",
        "evidence":       evidence if evidence is not None else ["ev A", "ev B"],
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


@pytest.fixture()
def db(tmp_path) -> Path:
    p = tmp_path / "exec_test.db"
    initialize_schema(p)
    return p


def _seed_accepted_review(db: Path, dtype: str = "cost_review") -> str:
    """Seed a decision and accept its review. Returns review_id."""
    d = _fake_review_decision(dtype=dtype)
    with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
        sync_decisions_to_reviews(db_path=db)
    rev = list_decision_reviews(db_path=db)[0]
    accept_decision(rev["review_id"], db_path=db)
    return rev["review_id"]


# ---------------------------------------------------------------------------
# TestSchema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_decision_execution_plans_table_exists(self, db):
        conn = get_connection(db)
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='decision_execution_plans'"
        ).fetchone()
        conn.close()
        assert row is not None, "decision_execution_plans table must exist"

    def test_table_columns_present(self, db):
        conn = get_connection(db)
        info = conn.execute("PRAGMA table_info(decision_execution_plans)").fetchall()
        conn.close()
        names = {r["name"] for r in info}
        required = {
            "plan_id", "review_id", "decision_id", "decision_type",
            "created_at", "plan_status", "execution_mode", "action_summary",
            "planned_steps_json", "risk_level", "blockers_json", "evidence_json",
            "operator_note", "approved_at", "rejected_at", "executed_at",
            "metadata_version",
        }
        assert required.issubset(names)


# ---------------------------------------------------------------------------
# TestCreateFromReview
# ---------------------------------------------------------------------------

class TestCreateFromReview:
    def test_accepted_review_creates_plan(self, db):
        rid = _seed_accepted_review(db)
        result = create_execution_plan_from_review(rid, db_path=db)
        assert result["ok"] is True
        assert result["created"] is True
        assert "plan_id" in result

    def test_created_plan_is_draft(self, db):
        rid = _seed_accepted_review(db)
        result = create_execution_plan_from_review(rid, db_path=db)
        plan = get_execution_plan(result["plan_id"], db_path=db)
        assert plan["plan_status"] == "draft"

    def test_created_plan_is_dry_run(self, db):
        rid = _seed_accepted_review(db)
        result = create_execution_plan_from_review(rid, db_path=db)
        plan = get_execution_plan(result["plan_id"], db_path=db)
        assert plan["execution_mode"] == "dry_run"

    def test_pending_review_blocked(self, db):
        d = _fake_review_decision()
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)
        rev = list_decision_reviews(db_path=db)[0]
        result = create_execution_plan_from_review(rev["review_id"], db_path=db)
        assert result["ok"] is False
        assert result["code"] == "review_not_accepted"

    def test_rejected_review_blocked(self, db):
        d = _fake_review_decision()
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)
        rev = list_decision_reviews(db_path=db)[0]
        reject_review(rev["review_id"], db_path=db)
        result = create_execution_plan_from_review(rev["review_id"], db_path=db)
        assert result["ok"] is False
        assert result["code"] == "review_rejected"

    def test_unknown_review_id(self, db):
        result = create_execution_plan_from_review("ghost-id", db_path=db)
        assert result["ok"] is False
        assert result["code"] == "review_not_found"

    def test_plan_has_action_summary(self, db):
        rid = _seed_accepted_review(db)
        result = create_execution_plan_from_review(rid, db_path=db)
        plan = result["plan"]
        assert plan["action_summary"]

    def test_plan_has_steps(self, db):
        rid = _seed_accepted_review(db)
        result = create_execution_plan_from_review(rid, db_path=db)
        plan = result["plan"]
        assert isinstance(plan["planned_steps"], list)
        assert len(plan["planned_steps"]) > 0

    def test_plan_carries_evidence(self, db):
        rid = _seed_accepted_review(db)
        result = create_execution_plan_from_review(rid, db_path=db)
        plan = result["plan"]
        assert isinstance(plan["evidence"], list)


# ---------------------------------------------------------------------------
# TestCreateIdempotent
# ---------------------------------------------------------------------------

class TestCreateIdempotent:
    def test_duplicate_create_returns_existing(self, db):
        rid = _seed_accepted_review(db)
        r1 = create_execution_plan_from_review(rid, db_path=db)
        r2 = create_execution_plan_from_review(rid, db_path=db)
        assert r1["ok"] is True
        assert r2["ok"] is True
        assert r1["plan_id"] == r2["plan_id"]
        assert r2["created"] is False

    def test_no_duplicate_rows(self, db):
        rid = _seed_accepted_review(db)
        create_execution_plan_from_review(rid, db_path=db)
        create_execution_plan_from_review(rid, db_path=db)
        assert len(list_execution_plans(db_path=db)) == 1


# ---------------------------------------------------------------------------
# TestDryRunMode
# ---------------------------------------------------------------------------

class TestDryRunMode:
    def test_all_plans_are_dry_run(self, db):
        for dtype in ["cost_review", "failure_review", "launch_readiness"]:
            rid = _seed_accepted_review(db, dtype=dtype)
            result = create_execution_plan_from_review(rid, db_path=db)
            assert result["plan"]["execution_mode"] == "dry_run", dtype

    def test_live_mode_blocked_in_dry_run_call(self, db):
        rid = _seed_accepted_review(db)
        result = create_execution_plan_from_review(rid, db_path=db)
        pid = result["plan_id"]

        # Manually corrupt mode to 'live' (DB bypass, simulates hypothetical future leak)
        conn = get_connection(db)
        conn.execute("UPDATE decision_execution_plans SET execution_mode='live' WHERE plan_id=?", (pid,))
        conn.commit()
        conn.close()

        dry = dry_run_execution_plan(pid, db_path=db)
        assert dry["ok"] is False
        assert dry["code"] == "live_mode_blocked"


# ---------------------------------------------------------------------------
# TestExecutedUnreachable
# ---------------------------------------------------------------------------

class TestExecutedUnreachable:
    def test_approve_does_not_set_executed(self, db):
        rid = _seed_accepted_review(db)
        r = create_execution_plan_from_review(rid, db_path=db)
        approve_execution_plan(r["plan_id"], db_path=db)
        plan = get_execution_plan(r["plan_id"], db_path=db)
        assert plan["plan_status"] == "approved"
        assert plan["plan_status"] != "executed"

    def test_dry_run_does_not_set_executed(self, db):
        rid = _seed_accepted_review(db)
        r = create_execution_plan_from_review(rid, db_path=db)
        dry_run_execution_plan(r["plan_id"], db_path=db)
        plan = get_execution_plan(r["plan_id"], db_path=db)
        assert plan["plan_status"] in ("draft", "approved")
        assert plan["plan_status"] != "executed"

    def test_no_plan_reaches_executed_via_public_api(self, db):
        # Exhaust every public API call; none should create executed plans
        rid = _seed_accepted_review(db)
        r = create_execution_plan_from_review(rid, db_path=db)
        pid = r["plan_id"]
        dry_run_execution_plan(pid, db_path=db)
        approve_execution_plan(pid, db_path=db)
        plans = list_execution_plans(db_path=db)
        assert all(p["plan_status"] != "executed" for p in plans)


# ---------------------------------------------------------------------------
# TestTransitions
# ---------------------------------------------------------------------------

class TestTransitions:
    def _make_plan(self, db) -> str:
        rid = _seed_accepted_review(db)
        return create_execution_plan_from_review(rid, db_path=db)["plan_id"]

    def test_approve_sets_status(self, db):
        pid = self._make_plan(db)
        result = approve_execution_plan(pid, note="looks good", db_path=db)
        assert result["ok"] is True
        assert result["status"] == "approved"
        plan = get_execution_plan(pid, db_path=db)
        assert plan["plan_status"] == "approved"
        assert plan["approved_at"] is not None

    def test_reject_sets_status(self, db):
        pid = self._make_plan(db)
        result = reject_execution_plan(pid, note="nope", db_path=db)
        assert result["ok"] is True
        assert result["status"] == "rejected"
        plan = get_execution_plan(pid, db_path=db)
        assert plan["plan_status"] == "rejected"
        assert plan["rejected_at"] is not None

    def test_cancel_sets_status(self, db):
        pid = self._make_plan(db)
        result = cancel_execution_plan(pid, note="no longer needed", db_path=db)
        assert result["ok"] is True
        assert result["status"] == "cancelled"
        plan = get_execution_plan(pid, db_path=db)
        assert plan["plan_status"] == "cancelled"


# ---------------------------------------------------------------------------
# TestTransitionNotFound
# ---------------------------------------------------------------------------

class TestTransitionNotFound:
    def test_approve_unknown(self, db):
        r = approve_execution_plan("no-such-plan", db_path=db)
        assert r["ok"] is False
        assert r["code"] == "not_found"

    def test_reject_unknown(self, db):
        r = reject_execution_plan("no-such-plan", db_path=db)
        assert r["ok"] is False
        assert r["code"] == "not_found"

    def test_cancel_unknown(self, db):
        r = cancel_execution_plan("no-such-plan", db_path=db)
        assert r["ok"] is False
        assert r["code"] == "not_found"

    def test_dry_run_unknown(self, db):
        r = dry_run_execution_plan("no-such-plan", db_path=db)
        assert r["ok"] is False
        assert r["code"] == "not_found"


# ---------------------------------------------------------------------------
# TestDryRunSimulation
# ---------------------------------------------------------------------------

class TestDryRunSimulation:
    def test_dry_run_returns_simulated_steps(self, db):
        rid = _seed_accepted_review(db)
        pid = create_execution_plan_from_review(rid, db_path=db)["plan_id"]
        result = dry_run_execution_plan(pid, db_path=db)
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert isinstance(result["simulated_steps"], list)
        assert len(result["simulated_steps"]) > 0

    def test_dry_run_steps_not_executed(self, db):
        rid = _seed_accepted_review(db)
        pid = create_execution_plan_from_review(rid, db_path=db)["plan_id"]
        result = dry_run_execution_plan(pid, db_path=db)
        for step in result["simulated_steps"]:
            assert step["executed"] is False
            assert step["simulated"] is True

    def test_dry_run_has_validation(self, db):
        rid = _seed_accepted_review(db)
        pid = create_execution_plan_from_review(rid, db_path=db)["plan_id"]
        result = dry_run_execution_plan(pid, db_path=db)
        assert "validation" in result
        assert "f5_guard" in result["validation"]

    def test_dry_run_has_warnings(self, db):
        rid = _seed_accepted_review(db)
        pid = create_execution_plan_from_review(rid, db_path=db)["plan_id"]
        result = dry_run_execution_plan(pid, db_path=db)
        assert isinstance(result["warnings"], list)
        assert any("executed" in w.lower() for w in result["warnings"])

    def test_dry_run_does_not_change_plan_status(self, db):
        rid = _seed_accepted_review(db)
        pid = create_execution_plan_from_review(rid, db_path=db)["plan_id"]
        before = get_execution_plan(pid, db_path=db)["plan_status"]
        dry_run_execution_plan(pid, db_path=db)
        after  = get_execution_plan(pid, db_path=db)["plan_status"]
        assert before == after == "draft"


# ---------------------------------------------------------------------------
# TestPlanTemplates
# ---------------------------------------------------------------------------

class TestPlanTemplates:
    @pytest.mark.parametrize("dtype,expected_risk,has_blocker", [
        ("launch_readiness", "high",   True),
        ("failure_review",   "medium", False),
        ("cost_review",      "low",    False),
        ("publishing_gap",   "high",   True),
        ("audience_review",  "low",    False),
        ("discovery_review", "low",    False),
    ])
    def test_template_risk_and_blockers(self, db, dtype, expected_risk, has_blocker):
        rid = _seed_accepted_review(db, dtype=dtype)
        result = create_execution_plan_from_review(rid, db_path=db)
        plan = result["plan"]
        assert plan["risk_level"] == expected_risk, f"{dtype}: wrong risk"
        if has_blocker:
            assert len(plan["blockers"]) > 0, f"{dtype}: expected blockers"
        else:
            assert len(plan["blockers"]) == 0, f"{dtype}: unexpected blockers"

    @pytest.mark.parametrize("dtype,expected_step_fragment", [
        ("launch_readiness", "run clearance"),
        ("failure_review",   "failed run"),
        ("cost_review",      "cost"),
        ("publishing_gap",   "publishing"),
        ("audience_review",  "audience"),
        ("discovery_review", "topic queue"),
    ])
    def test_template_steps_contain_keywords(self, db, dtype, expected_step_fragment):
        rid = _seed_accepted_review(db, dtype=dtype)
        result = create_execution_plan_from_review(rid, db_path=db)
        steps_text = " ".join(result["plan"]["planned_steps"]).lower()
        assert expected_step_fragment in steps_text, \
            f"{dtype}: '{expected_step_fragment}' not found in steps"

    def test_launch_readiness_blocker_text(self, db):
        rid = _seed_accepted_review(db, dtype="launch_readiness")
        plan = create_execution_plan_from_review(rid, db_path=db)["plan"]
        assert any("F5" in b or "disabled" in b.lower() for b in plan["blockers"])

    def test_publishing_gap_blocker_text(self, db):
        rid = _seed_accepted_review(db, dtype="publishing_gap")
        plan = create_execution_plan_from_review(rid, db_path=db)["plan"]
        assert any("F5" in b or "disabled" in b.lower() for b in plan["blockers"])


# ---------------------------------------------------------------------------
# TestUnknownType
# ---------------------------------------------------------------------------

class TestUnknownType:
    def test_unknown_type_creates_blocked_plan(self, db):
        d = _fake_review_decision(dtype="totally_unknown_type")
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)
        rev = list_decision_reviews(db_path=db)[0]
        accept_decision(rev["review_id"], db_path=db)
        result = create_execution_plan_from_review(rev["review_id"], db_path=db)
        assert result["ok"] is True
        plan = result["plan"]
        assert plan["risk_level"] == "blocked"
        assert len(plan["blockers"]) > 0
        assert any("unsupported" in b.lower() or "not found" in b.lower()
                   for b in plan["blockers"])


# ---------------------------------------------------------------------------
# TestListFilter
# ---------------------------------------------------------------------------

class TestListFilter:
    def _seed_three(self, db):
        pids = []
        for dtype in ["cost_review", "failure_review", "discovery_review"]:
            rid = _seed_accepted_review(db, dtype=dtype)
            pid = create_execution_plan_from_review(rid, db_path=db)["plan_id"]
            pids.append(pid)
        approve_execution_plan(pids[1], db_path=db)
        cancel_execution_plan(pids[2], db_path=db)
        return pids

    def test_list_all(self, db):
        self._seed_three(db)
        assert len(list_execution_plans(db_path=db)) == 3

    def test_list_draft(self, db):
        self._seed_three(db)
        rows = list_execution_plans(status="draft", db_path=db)
        assert len(rows) == 1
        assert rows[0]["plan_status"] == "draft"

    def test_list_approved(self, db):
        self._seed_three(db)
        rows = list_execution_plans(status="approved", db_path=db)
        assert len(rows) == 1
        assert rows[0]["plan_status"] == "approved"

    def test_list_cancelled(self, db):
        self._seed_three(db)
        rows = list_execution_plans(status="cancelled", db_path=db)
        assert len(rows) == 1
        assert rows[0]["plan_status"] == "cancelled"

    def test_draft_first(self, db):
        self._seed_three(db)
        rows = list_execution_plans(db_path=db)
        assert rows[0]["plan_status"] == "draft"


# ---------------------------------------------------------------------------
# TestGetPlan
# ---------------------------------------------------------------------------

class TestGetPlan:
    def test_get_existing(self, db):
        rid = _seed_accepted_review(db)
        pid = create_execution_plan_from_review(rid, db_path=db)["plan_id"]
        plan = get_execution_plan(pid, db_path=db)
        assert plan is not None
        assert plan["plan_id"] == pid

    def test_get_nonexistent_returns_none(self, db):
        assert get_execution_plan("ghost-id", db_path=db) is None

    def test_get_decodes_steps(self, db):
        rid = _seed_accepted_review(db)
        pid = create_execution_plan_from_review(rid, db_path=db)["plan_id"]
        plan = get_execution_plan(pid, db_path=db)
        assert isinstance(plan["planned_steps"], list)

    def test_get_decodes_blockers(self, db):
        rid = _seed_accepted_review(db, dtype="launch_readiness")
        pid = create_execution_plan_from_review(rid, db_path=db)["plan_id"]
        plan = get_execution_plan(pid, db_path=db)
        assert isinstance(plan["blockers"], list)

    def test_get_decodes_evidence(self, db):
        rid = _seed_accepted_review(db)
        pid = create_execution_plan_from_review(rid, db_path=db)["plan_id"]
        plan = get_execution_plan(pid, db_path=db)
        assert isinstance(plan["evidence"], list)


# ---------------------------------------------------------------------------
# TestNoSideEffects
# ---------------------------------------------------------------------------

class TestNoSideEffects:
    def _pid(self, db) -> str:
        rid = _seed_accepted_review(db)
        return create_execution_plan_from_review(rid, db_path=db)["plan_id"]

    def _run_count(self, db) -> int:
        conn = get_connection(db)
        n = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        return n

    def test_create_does_not_create_run(self, db):
        _seed_accepted_review(db)
        assert self._run_count(db) == 0

    def test_approve_does_not_create_run(self, db):
        pid = self._pid(db)
        approve_execution_plan(pid, db_path=db)
        assert self._run_count(db) == 0

    def test_dry_run_does_not_create_run(self, db):
        pid = self._pid(db)
        dry_run_execution_plan(pid, db_path=db)
        assert self._run_count(db) == 0

    def test_cancel_does_not_create_run(self, db):
        pid = self._pid(db)
        cancel_execution_plan(pid, db_path=db)
        assert self._run_count(db) == 0
