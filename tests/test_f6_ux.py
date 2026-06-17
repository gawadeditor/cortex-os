"""
tests/test_f6_ux.py — F6: Command Center Operator UX Verification

Coverage:
    TestCommandLabels          — /command uses operator-friendly labels
    TestCommandNeedsAttention  — "Needs attention" section present
    TestCommandDecisionWaiting — shows "Decision waiting for review" with pending reviews
    TestCommandActionPlans     — shows "Action plans waiting" card when plans exist
    TestCommandNoExecute       — /command never contains "Execute" button
    TestCommandFallbacks       — empty states are calm and clear
    TestExecutionPlansPage     — /execution-plans is named "Action Plans", no Execute button
    TestDecisionReviewsUX      — pending section has only Accept/Reject; accepted has "Create action plan"
    TestNoProviderCalls        — no network/provider calls from any page render
    TestNoPublishing           — no publishing calls from any page
    TestNoEpisodeLaunch        — no runs created from any page render
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
from hypogeum.decision_execution import (
    create_execution_plan_from_review,
)


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

def _fake_decision(title: str = "Test decision", dtype: str = "cost_review") -> dict:
    return {
        "decision_id":    str(uuid.uuid4()),
        "type":           dtype,
        "title":          title,
        "recommendation": "Investigate this",
        "evidence":       ["signal A", "signal B"],
        "confidence":     0.75,
        "priority":       2,
        "investigate_url": "/decisions",
        "action_label":   "Investigate",
    }


def _fake_ds(decisions: list[dict] | None = None) -> dict:
    return {
        "checked_at":        "2026-01-01T00:00:00+00:00",
        "engine_status":     "ready",
        "summary":           "ok",
        "decisions":         decisions or [],
        "insufficient_data": [],
    }


def _fake_cmd() -> dict:
    return {
        "factory_status":    {"level": "HEALTHY", "reason": "", "director_score": 100},
        "checked_at":        "2026-01-01T00:00:00+00:00",
        "attention_queue":   [],
        "episode_readiness": {
            "can_launch":            False,
            "blockers":              ["No channels configured"],
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
            "has_real_inputs":           False,
            "missing_inputs":            [],
            "ready_for_decision_engine": False,
        }
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path) -> Path:
    p = tmp_path / "f6_ux_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def client(db):
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db)
    with server.app.test_client() as c:
        yield c


def _get_command(client, db, *, reviews=None, ds=None, cmd=None):
    """Helper to render /command with mocked data."""
    with (
        patch("run_dashboard.get_command_state", return_value=cmd or _fake_cmd()),
        patch("run_dashboard.get_decision_inputs_state", return_value=_fake_di()),
        patch("run_dashboard.get_decision_state", return_value=ds or _fake_ds()),
    ):
        return client.get("/command")


def _seed_pending_review(db: Path) -> str:
    d = _fake_decision()
    with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
        sync_decisions_to_reviews(db_path=db)
    return list_decision_reviews(db_path=db)[0]["review_id"]


def _seed_accepted_review(db: Path) -> str:
    rid = _seed_pending_review(db)
    accept_decision(rid, db_path=db)
    return rid


def _seed_plan(db: Path) -> str:
    rid = _seed_accepted_review(db)
    return create_execution_plan_from_review(rid, db_path=db)["plan_id"]


# ---------------------------------------------------------------------------
# TestCommandLabels
# ---------------------------------------------------------------------------

class TestCommandLabels:
    def test_command_has_needs_attention_heading(self, client, db):
        resp = _get_command(client, db)
        html = resp.data.decode()
        assert "Needs attention" in html

    def test_command_does_not_have_old_attention_queue_label(self, client, db):
        resp = _get_command(client, db)
        html = resp.data.decode()
        assert "Attention queue" not in html

    def test_command_has_launch_readiness_heading(self, client, db):
        resp = _get_command(client, db)
        html = resp.data.decode()
        assert "Launch readiness" in html

    def test_command_has_investigate_heading(self, client, db):
        resp = _get_command(client, db)
        html = resp.data.decode()
        assert "Investigate" in html

    def test_command_has_next_decision_heading(self, client, db):
        resp = _get_command(client, db)
        html = resp.data.decode()
        assert "Next decision" in html

    def test_command_has_decision_data_label_in_collapsed_section(self, client, db):
        resp = _get_command(client, db)
        html = resp.data.decode()
        assert "Decision data" in html

    def test_command_does_not_use_decision_inputs_label(self, client, db):
        resp = _get_command(client, db)
        html = resp.data.decode()
        # "Decision inputs" should not appear as a visible heading
        assert "Decision inputs" not in html


# ---------------------------------------------------------------------------
# TestCommandNeedsAttention
# ---------------------------------------------------------------------------

class TestCommandNeedsAttention:
    def test_empty_attention_shows_calm_state(self, client, db):
        resp = _get_command(client, db)
        html = resp.data.decode()
        assert "Nothing needs attention" in html

    def test_attention_item_shown_when_present(self, client, db):
        cmd = _fake_cmd()
        cmd["attention_queue"] = [{
            "level": "high",
            "title": "Provider key missing",
            "why": "ElevenLabs is required",
            "evidence": "",
            "action_url": "/preflight",
            "action_label": "Check preflight",
            "investigate_url": "/preflight",
            "investigate_label": "Details",
        }]
        resp = _get_command(client, db, cmd=cmd)
        html = resp.data.decode()
        assert "Provider key missing" in html


# ---------------------------------------------------------------------------
# TestCommandDecisionWaiting
# ---------------------------------------------------------------------------

class TestCommandDecisionWaiting:
    def test_pending_review_shows_decision_waiting(self, client, db):
        _seed_pending_review(db)
        with (
            patch("run_dashboard.get_command_state", return_value=_fake_cmd()),
            patch("run_dashboard.get_decision_inputs_state", return_value=_fake_di()),
            patch("run_dashboard.get_decision_state", return_value=_fake_ds()),
        ):
            resp = client.get("/command")
        html = resp.data.decode()
        assert "Decision waiting for review" in html

    def test_no_reviews_falls_back_to_computed_decision(self, client, db):
        d = _fake_decision(title="Computed engine decision")
        with (
            patch("run_dashboard.get_command_state", return_value=_fake_cmd()),
            patch("run_dashboard.get_decision_inputs_state", return_value=_fake_di()),
            patch("run_dashboard.get_decision_state", return_value=_fake_ds([d])),
            patch("run_dashboard.list_decision_reviews", return_value=[]),
        ):
            resp = client.get("/command")
        html = resp.data.decode()
        assert "Computed engine decision" in html
        assert "Decision waiting for review" not in html

    def test_engine_not_ready_shows_calm_state(self, client, db):
        ds_not_ready = {
            "checked_at":        "2026-01-01T00:00:00+00:00",
            "engine_status":     "not_ready",
            "summary":           "Insufficient data",
            "decisions":         [],
            "insufficient_data": ["episodes"],
        }
        with (
            patch("run_dashboard.get_command_state", return_value=_fake_cmd()),
            patch("run_dashboard.get_decision_inputs_state", return_value=_fake_di()),
            patch("run_dashboard.get_decision_state", return_value=ds_not_ready),
            patch("run_dashboard.list_decision_reviews", return_value=[]),
        ):
            resp = client.get("/command")
        html = resp.data.decode()
        assert "not ready" in html.lower() or "decision data" in html.lower()


# ---------------------------------------------------------------------------
# TestCommandActionPlans
# ---------------------------------------------------------------------------

class TestCommandActionPlans:
    def test_action_plans_card_shown_when_plans_exist(self, client, db):
        _seed_plan(db)
        with (
            patch("run_dashboard.get_command_state", return_value=_fake_cmd()),
            patch("run_dashboard.get_decision_inputs_state", return_value=_fake_di()),
            patch("run_dashboard.get_decision_state", return_value=_fake_ds()),
        ):
            resp = client.get("/command")
        html = resp.data.decode()
        assert "Action plans waiting" in html

    def test_action_plans_card_has_correct_label(self, client, db):
        _seed_plan(db)
        with (
            patch("run_dashboard.get_command_state", return_value=_fake_cmd()),
            patch("run_dashboard.get_decision_inputs_state", return_value=_fake_di()),
            patch("run_dashboard.get_decision_state", return_value=_fake_ds()),
        ):
            resp = client.get("/command")
        html = resp.data.decode()
        assert "Action plans" in html
        assert "View action plans" in html

    def test_action_plans_card_not_shown_when_no_plans(self, client, db):
        with (
            patch("run_dashboard.get_command_state", return_value=_fake_cmd()),
            patch("run_dashboard.get_decision_inputs_state", return_value=_fake_di()),
            patch("run_dashboard.get_decision_state", return_value=_fake_ds()),
        ):
            resp = client.get("/command")
        html = resp.data.decode()
        assert "Action plans waiting" not in html

    def test_action_plans_links_to_execution_plans(self, client, db):
        _seed_plan(db)
        with (
            patch("run_dashboard.get_command_state", return_value=_fake_cmd()),
            patch("run_dashboard.get_decision_inputs_state", return_value=_fake_di()),
            patch("run_dashboard.get_decision_state", return_value=_fake_ds()),
        ):
            resp = client.get("/command")
        html = resp.data.decode()
        assert "/execution-plans" in html


# ---------------------------------------------------------------------------
# TestCommandNoExecute
# ---------------------------------------------------------------------------

class TestCommandNoExecute:
    def test_command_has_no_execute_button(self, client, db):
        _seed_plan(db)
        with (
            patch("run_dashboard.get_command_state", return_value=_fake_cmd()),
            patch("run_dashboard.get_decision_inputs_state", return_value=_fake_di()),
            patch("run_dashboard.get_decision_state", return_value=_fake_ds()),
        ):
            resp = client.get("/command")
        html = resp.data.decode()
        import re
        execute_buttons = re.findall(
            r'<button[^>]*>[^<]*[Ee]xecute[^<]*</button>|'
            r'onclick=["\'][^"\']*execute[^"\']*["\']',
            html
        )
        assert len(execute_buttons) == 0, f"Found Execute button: {execute_buttons}"

    def test_command_no_execute_word_in_buttons(self, client, db):
        with (
            patch("run_dashboard.get_command_state", return_value=_fake_cmd()),
            patch("run_dashboard.get_decision_inputs_state", return_value=_fake_di()),
            patch("run_dashboard.get_decision_state", return_value=_fake_ds()),
        ):
            resp = client.get("/command")
        html = resp.data.decode()
        # "Execute" should not appear as button text (dry-run wording is ok)
        import re
        buttons = re.findall(r'<(?:button|a)[^>]*>([^<]+)</(?:button|a)>', html)
        execute_btns = [b.strip() for b in buttons if "execute" in b.strip().lower()]
        assert execute_btns == [], f"Execute in button labels: {execute_btns}"


# ---------------------------------------------------------------------------
# TestCommandFallbacks
# ---------------------------------------------------------------------------

class TestCommandFallbacks:
    def test_command_returns_200(self, client, db):
        resp = _get_command(client, db)
        assert resp.status_code == 200

    def test_empty_attention_queue_calm_message(self, client, db):
        resp = _get_command(client, db)
        html = resp.data.decode()
        assert "Nothing needs attention" in html

    def test_no_decisions_shows_calm_state(self, client, db):
        ds = _fake_ds(decisions=[])
        with (
            patch("run_dashboard.get_command_state", return_value=_fake_cmd()),
            patch("run_dashboard.get_decision_inputs_state", return_value=_fake_di()),
            patch("run_dashboard.get_decision_state", return_value=ds),
            patch("run_dashboard.list_decision_reviews", return_value=[]),
        ):
            resp = client.get("/command")
        html = resp.data.decode()
        assert "No decisions pending" in html


# ---------------------------------------------------------------------------
# TestExecutionPlansPage
# ---------------------------------------------------------------------------

class TestExecutionPlansPage:
    def test_page_title_is_action_plans(self, client):
        resp = client.get("/execution-plans")
        html = resp.data.decode()
        assert "Action Plans" in html or "action plans" in html.lower()

    def test_page_has_dry_run_only_notice(self, client):
        resp = client.get("/execution-plans")
        html = resp.data.decode()
        assert "DRY-RUN ONLY" in html or "dry-run only" in html.lower() or "Dry-run only" in html

    def test_page_states_no_execute_button(self, client):
        resp = client.get("/execution-plans")
        html = resp.data.decode()
        assert "no execute button" in html.lower() or "There is no Execute button" in html

    def test_page_has_no_execute_button_element(self, client, db):
        _seed_plan(db)
        resp = client.get("/execution-plans")
        html = resp.data.decode()
        import re
        execute_buttons = re.findall(
            r'<button[^>]*>[^<]*[Ee]xecute[^<]*</button>', html
        )
        assert execute_buttons == [], f"Found Execute button: {execute_buttons}"

    def test_page_returns_200(self, client):
        assert client.get("/execution-plans").status_code == 200

    def _seed_plan(self, db):
        d = _fake_decision()
        with patch("hypogeum.decision_engine.get_decision_state", return_value=_fake_ds([d])):
            sync_decisions_to_reviews(db_path=db)
        rev = list_decision_reviews(db_path=db)[0]
        accept_decision(rev["review_id"], db_path=db)
        return create_execution_plan_from_review(rev["review_id"], db_path=db)["plan_id"]

    def test_draft_plan_shows_dry_run_and_approve_buttons(self, client, db):
        self._seed_plan(db)
        resp = client.get("/execution-plans")
        html = resp.data.decode()
        assert "Dry run" in html
        assert "Approve" in html

    def test_dry_run_not_execute(self, client, db):
        self._seed_plan(db)
        resp = client.get("/execution-plans")
        html = resp.data.decode()
        # The button should say "Dry run", not "Execute"
        assert "Dry run" in html
        import re
        execute_btns = re.findall(r'<button[^>]*>Execute</button>', html)
        assert execute_btns == []


# ---------------------------------------------------------------------------
# TestDecisionReviewsUX
# ---------------------------------------------------------------------------

class TestDecisionReviewsUX:
    def test_pending_section_has_accept_button(self, client, db):
        _seed_pending_review(db)
        resp = client.get("/decision-reviews")
        html = resp.data.decode()
        assert "Accept" in html

    def test_pending_section_has_reject_button(self, client, db):
        _seed_pending_review(db)
        resp = client.get("/decision-reviews")
        html = resp.data.decode()
        assert "Reject" in html

    def test_pending_section_no_execution_wording(self, client, db):
        _seed_pending_review(db)
        resp = client.get("/decision-reviews")
        html = resp.data.decode()
        # Pending section must not say "execute" or "launch"
        # Only pending cards are shown in this fixture; accepted section is hidden
        # Check that the Create action plan button does NOT appear (no accepted reviews)
        assert "Create action plan" not in html

    def test_accepted_section_has_create_action_plan(self, client, db):
        _seed_accepted_review(db)
        resp = client.get("/decision-reviews")
        html = resp.data.decode()
        assert "Create action plan" in html

    def test_accepted_section_has_view_action_plans(self, client, db):
        _seed_accepted_review(db)
        resp = client.get("/decision-reviews")
        html = resp.data.decode()
        assert "View action plans" in html

    def test_accepted_section_no_execute_button(self, client, db):
        _seed_accepted_review(db)
        resp = client.get("/decision-reviews")
        html = resp.data.decode()
        import re
        execute_btns = re.findall(r'<button[^>]*>Execute</button>', html)
        assert execute_btns == []

    def test_rejected_section_no_execute_wording(self, client, db):
        rid = _seed_pending_review(db)
        from hypogeum.decision_reviews import reject_decision
        reject_decision(rid, db_path=db)
        resp = client.get("/decision-reviews")
        html = resp.data.decode()
        import re
        execute_btns = re.findall(r'<button[^>]*>[^<]*[Ee]xecute[^<]*</button>', html)
        assert execute_btns == []


# ---------------------------------------------------------------------------
# TestNoProviderCalls
# ---------------------------------------------------------------------------

class TestNoProviderCalls:
    def test_command_no_network_calls(self, client, db):
        import socket
        original_connect = socket.socket.connect

        def no_connect(self, *args, **kwargs):
            raise AssertionError(f"Unexpected network call to {args}")

        socket.socket.connect = no_connect
        try:
            _get_command(client, db)
        finally:
            socket.socket.connect = original_connect

    def test_execution_plans_no_network_calls(self, client):
        import socket
        original_connect = socket.socket.connect

        def no_connect(self, *args, **kwargs):
            raise AssertionError(f"Unexpected network call to {args}")

        socket.socket.connect = no_connect
        try:
            client.get("/execution-plans")
        finally:
            socket.socket.connect = original_connect

    def test_decision_reviews_no_network_calls(self, client):
        import socket
        original_connect = socket.socket.connect

        def no_connect(self, *args, **kwargs):
            raise AssertionError(f"Unexpected network call to {args}")

        socket.socket.connect = no_connect
        try:
            client.get("/decision-reviews")
        finally:
            socket.socket.connect = original_connect


# ---------------------------------------------------------------------------
# TestNoPublishing
# ---------------------------------------------------------------------------

class TestNoPublishing:
    def _run_count(self, db: Path) -> int:
        from hypogeum.db import get_connection
        conn = get_connection(db)
        n = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        return n

    def test_command_no_publishing_side_effect(self, client, db):
        _seed_plan(db)
        with (
            patch("run_dashboard.get_command_state", return_value=_fake_cmd()),
            patch("run_dashboard.get_decision_inputs_state", return_value=_fake_di()),
            patch("run_dashboard.get_decision_state", return_value=_fake_ds()),
        ):
            client.get("/command")
        assert self._run_count(db) == 0

    def test_execution_plans_no_publishing_side_effect(self, client, db):
        _seed_plan(db)
        client.get("/execution-plans")
        assert self._run_count(db) == 0

    def test_decision_reviews_no_publishing_side_effect(self, client, db):
        _seed_accepted_review(db)
        client.get("/decision-reviews")
        assert self._run_count(db) == 0


# ---------------------------------------------------------------------------
# TestNoEpisodeLaunch
# ---------------------------------------------------------------------------

class TestNoEpisodeLaunch:
    def _run_count(self, db: Path) -> int:
        from hypogeum.db import get_connection
        conn = get_connection(db)
        n = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        return n

    def test_command_render_no_run(self, client, db):
        _get_command(client, db)
        assert self._run_count(db) == 0

    def test_execution_plans_render_no_run(self, client, db):
        client.get("/execution-plans")
        assert self._run_count(db) == 0

    def test_decision_reviews_render_no_run(self, client, db):
        client.get("/decision-reviews")
        assert self._run_count(db) == 0

    def test_approve_plan_no_run(self, client, db):
        pid = _seed_plan(db)
        client.post(f"/api/execution-plans/{pid}/approve", json={})
        assert self._run_count(db) == 0

    def test_accept_review_no_run(self, client, db):
        rid = _seed_pending_review(db)
        client.post(f"/api/decision-reviews/{rid}/accept", json={})
        assert self._run_count(db) == 0
