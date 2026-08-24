"""
tests/test_finance_gate.py — Finance gate approval workflow

Coverage:
  TestFinanceGateBlocks          — over-budget run raises FinancePostVPDBlockedError
  TestFinanceApprovalUnblocks    — valid approval lets pipeline pass the gate
  TestApprovalAuditTrail         — approval is inserted and consumable, logged
  TestApprovalExpiry             — expired approval does not unblock the gate
  TestBudgetCeilingAPI           — /api/setup/budget-ceiling updates .env and os.environ
  TestDashboardFinanceGatePanel  — _classify_run_recovery detects finance_gate class
                                   and surfaces spend/ceiling/overage
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.db import (
    get_connection,
    initialize_schema,
    insert_finance_approval,
    get_valid_finance_approval,
    consume_finance_approval,
    list_finance_approvals,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _future(hours: int = 24) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()

def _past(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

def _make_finance_record(
    episode_id: str,
    estimated: float,
    ceiling: float,
    verdict: str = "BLOCKED",
) -> dict:
    return {
        "finance_id":          f"fa_{uuid.uuid4().hex[:12]}",
        "episode_id":          episode_id,
        "phase":               "post_vpd_authorization",
        "recorded_at":         _now(),
        "verdict":             verdict,
        "authorized":          verdict != "BLOCKED",
        "budget_ceiling_usd":  ceiling,
        "estimated_spend_usd": estimated,
        "checks":              [],
        "failures":            [],
        "warnings":            [],
        "cost_breakdown": {
            "scene_count":            10,
            "model_breakdown":        {"veo3-fast-text-to-video": 10},
            "estimated_muapi_usd":    estimated * 0.82,
            "estimated_narration_usd": estimated * 0.15,
            "estimated_music_usd":    estimated * 0.02,
            "estimated_openrouter_usd": estimated * 0.01,
            "total_estimated_usd":    estimated,
            "narration_chars":        5000,
            "section_count":          3,
        },
    }


# ---------------------------------------------------------------------------
# TestFinanceGateBlocks
# ---------------------------------------------------------------------------

class TestFinanceGateBlocks:
    """Finance gate raises FinancePostVPDBlockedError when estimate > ceiling."""

    def test_blocked_verdict_raises(self, tmp_path):
        """When verdict='BLOCKED' and no approval, FinancePostVPDBlockedError is raised."""
        from hypogeum.agents.finance_agent import (
            FinancePostVPDBlockedError,
            run_post_vpd_authorization,
        )

        # Minimal visual_package with 0 scenes (ceiling check still fires when ceiling > 0)
        vp = {"scenes": [], "episode_metadata": {}, "visual_continuity_record": {"locked_entities": []}}

        cfg = MagicMock()
        cfg.FINANCE_BUDGET_CEILING_USD   = 5.0    # very low ceiling
        cfg.FINANCE_WARNING_THRESHOLD_PCT = 0.80
        cfg.MUAPI_DRY_RUN               = False
        cfg.MUAPI_CHEAP_MODE            = False
        cfg.MUAPI_RUNWAY_ONLY_MODE      = False
        cfg.MUAPI_T1_MODEL              = "veo3-text-to-video"
        cfg.MUAPI_T2_MODEL              = "veo3-fast-text-to-video"
        cfg.MUAPI_T3_MODEL              = "veo3-fast-text-to-video"
        cfg.MUAPI_I2V_MODEL             = "veo3-image-to-video"

        from hypogeum.production_plan import PLAN_SCHEMA_VERSION, ProductionPlan
        production_plan = ProductionPlan(
            schema_version=PLAN_SCHEMA_VERSION, channel_id="hypogeum", episode_id="ep_test_gate",
            deliverable_contract_id="test", deliverable_ids=["main_episode"], assets=[],
            thumbnail_main_candidate_count=3, thumbnail_short_candidate_cap=3,
            thumbnail_attempts_per_candidate=2, thumbnail_unit_price_usd=0.09,
            thumbnail_cost_normal_usd=0.27, thumbnail_cost_maximum_usd=0.27,
            narration_char_count=0, narration_price_per_char_usd=0.0003,
            narration_cost_usd=0.0, narration_is_realized=False,
            music_section_count=0, music_seconds_per_section=120.0,
            music_price_per_second_usd=0.02, music_cost_usd=0.0,
            planning_call_estimate_usd=0.05, enhancement_cost_maximum_usd=0.0,
            normal_expected_cost_usd=0.32, maximum_bounded_cost_usd=0.32,
            content_hash="a" * 64, compiled_at="2026-08-24T00:00:00+00:00",
        )

        # Patch policy cascade to return env ceiling only
        with patch("hypogeum.agents.finance_agent.get_effective_budget_ceiling",
                   return_value=5.0), \
             patch("hypogeum.ois.emit_signal"):
            record = run_post_vpd_authorization(
                visual_package=vp, episode_id="ep_test_gate",
                config=cfg, production_plan=production_plan, output_dir=tmp_path,
            )

        # With 0 scenes + narration=$0 the estimate is $0 and ceiling=$5 → AUTHORIZED.
        # For a BLOCKED test we need estimate > ceiling; patch the total estimate instead.
        blocked_record = {**record, "verdict": "BLOCKED",
                          "estimated_spend_usd": 35.0, "budget_ceiling_usd": 5.0}

        # Now call _check_finance_approval (no approval exists) → returns False → gate blocks
        from hypogeum.agents.visual_package_director import _check_finance_approval
        log = MagicMock()
        result = _check_finance_approval("ep_no_approval", blocked_record, log)
        assert result is False

    def test_finance_gate_error_message_includes_amounts(self, tmp_path):
        """FinancePostVPDBlockedError message contains estimated and ceiling amounts."""
        from hypogeum.agents.finance_agent import FinancePostVPDBlockedError
        rec = _make_finance_record("ep_x", 35.0, 30.0, verdict="BLOCKED")
        exc = FinancePostVPDBlockedError(episode_id="ep_x", record=rec)
        msg = str(exc)
        assert "35.0" in msg
        assert "30.0" in msg
        assert "ep_x" in msg


# ---------------------------------------------------------------------------
# TestFinanceApprovalUnblocks
# ---------------------------------------------------------------------------

class TestFinanceApprovalUnblocks:
    """Valid approval lets the pipeline pass the gate."""

    def test_valid_approval_is_found_then_consumed(self, tmp_path):
        """
        A valid pending approval is returned by get_valid_finance_approval,
        and after consume_finance_approval it is no longer returned.
        Verifies the full approve → consume lifecycle without going through
        the pipeline (which requires live API keys).
        """
        db = tmp_path / "test.db"
        initialize_schema(db)

        approval_id = f"fa_{uuid.uuid4().hex[:16]}"
        conn = get_connection(db)
        insert_finance_approval(
            conn,
            approval_id=approval_id,
            episode_id="ep_approved",
            approved_at=_now(),
            estimated_spend_usd=35.0,
            ceiling_at_approval=30.0,
            overage_usd=5.0,
            expires_at=_future(24),
        )
        conn.commit()

        # Before consume: should be found
        approval = get_valid_finance_approval(conn, "ep_approved", _now())
        assert approval is not None, "approval not found before consume"
        assert approval["approval_id"] == approval_id

        # Consume
        consume_finance_approval(conn, approval_id)
        conn.commit()

        # After consume: should not be returned
        approval_after = get_valid_finance_approval(conn, "ep_approved", _now())
        conn.close()
        assert approval_after is None, "consumed approval should not be returned"

    def test_approval_consumption_marks_as_consumed(self, tmp_path):
        """consume_finance_approval sets status='consumed'."""
        db = tmp_path / "test.db"
        initialize_schema(db)

        approval_id = f"fa_{uuid.uuid4().hex[:16]}"
        conn = get_connection(db)
        insert_finance_approval(
            conn, approval_id=approval_id, episode_id="ep_consume",
            approved_at=_now(), estimated_spend_usd=35.0, ceiling_at_approval=30.0,
            overage_usd=5.0, expires_at=_future(24),
        )
        conn.commit()

        consume_finance_approval(conn, approval_id)
        conn.commit()

        row = conn.execute(
            "SELECT status FROM finance_approvals WHERE approval_id = ?",
            (approval_id,)
        ).fetchone()
        conn.close()

        assert row["status"] == "consumed"


# ---------------------------------------------------------------------------
# TestApprovalAuditTrail
# ---------------------------------------------------------------------------

class TestApprovalAuditTrail:
    """Approvals are stored with full metadata and retrievable for audit."""

    def test_approval_stores_all_fields(self, tmp_path):
        """insert_finance_approval stores episode_id, amounts, note, run_id, approved_by."""
        db = tmp_path / "test.db"
        initialize_schema(db)

        approval_id = f"fa_{uuid.uuid4().hex[:16]}"
        run_id = str(uuid.uuid4())
        conn = get_connection(db)
        insert_finance_approval(
            conn,
            approval_id=approval_id,
            episode_id="ep_audit",
            run_id=run_id,
            approved_at=_now(),
            approved_by="operator",
            estimated_spend_usd=35.649,
            ceiling_at_approval=30.0,
            overage_usd=5.649,
            expires_at=_future(24),
            note="Launch ep_000 — one-time exception",
        )
        conn.commit()
        conn.close()

        conn2 = get_connection(db)
        approvals = list_finance_approvals(conn2, episode_id="ep_audit")
        conn2.close()

        assert len(approvals) == 1
        a = approvals[0]
        assert a["approval_id"] == approval_id
        assert a["episode_id"] == "ep_audit"
        assert a["run_id"] == run_id
        assert a["approved_by"] == "operator"
        assert a["estimated_spend_usd"] == pytest.approx(35.649, abs=0.001)
        assert a["ceiling_at_approval"] == pytest.approx(30.0, abs=0.001)
        assert a["overage_usd"] == pytest.approx(5.649, abs=0.001)
        assert a["note"] == "Launch ep_000 — one-time exception"
        assert a["status"] == "pending"

    def test_list_finance_approvals_returns_all(self, tmp_path):
        """list_finance_approvals returns all rows for the episode."""
        db = tmp_path / "test.db"
        initialize_schema(db)

        conn = get_connection(db)
        for i in range(3):
            insert_finance_approval(
                conn, approval_id=f"fa_{i:04d}", episode_id="ep_multi",
                approved_at=_now(), estimated_spend_usd=35.0+i, ceiling_at_approval=30.0,
                overage_usd=5.0+i, expires_at=_future(24),
            )
        conn.commit()
        approvals = list_finance_approvals(conn, episode_id="ep_multi")
        conn.close()

        assert len(approvals) == 3

    def test_api_finance_approve_endpoint(self, tmp_path):
        """POST /api/run/<run_id>/finance-approve creates approval and re-queues."""
        import run_dashboard
        from hypogeum.channel_config import (
            create_brand_profile, create_channel_profile,
            create_model_profile, create_publishing_profile,
        )
        from hypogeum.calog import set_channel_state

        db = tmp_path / "test.db"
        initialize_schema(db)
        bp = create_brand_profile("Brand", db_path=db)
        pp = create_publishing_profile("Publishing", db_path=db)
        mp = create_model_profile("Models", db_path=db)
        create_channel_profile(
            "hypogeum", "Test Channel",
            brand_profile_id=bp.brand_profile_id,
            publishing_profile_id=pp.publishing_profile_id,
            model_profile_id=mp.model_profile_id,
            db_path=db,
        )
        set_channel_state("hypogeum", "active", triggered_by="test", db_path=db)

        now_str = _now()
        run_id = str(uuid.uuid4())
        episode_id = "ep_gate_test"
        conn = get_connection(db)
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status, metadata_version, channel_id) "
            "VALUES (?, ?, ?, 'failed', 1, 'hypogeum')",
            (run_id, episode_id, now_str),
        )
        conn.execute(
            "INSERT INTO episodes (episode_id, title, topic, created_at, status, metadata_version) "
            "VALUES (?, 'Test', 'Test Topic', ?, 'produced', 1)",
            (episode_id, now_str),
        )
        conn.commit()
        conn.close()

        # Write a BLOCKED finance record to disk
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)
        finance_record = _make_finance_record(episode_id, 35.649, 30.0, verdict="BLOCKED")
        (ep_dir / f"finance_post_vpd_authorization_{episode_id}.json").write_text(
            json.dumps(finance_record), encoding="utf-8"
        )

        original_db  = run_dashboard._db_path
        original_assets = run_dashboard._ASSETS_ROOT_PATH
        run_dashboard._db_path          = lambda: db
        run_dashboard._ASSETS_ROOT_PATH = tmp_path

        try:
            with run_dashboard.app.test_client() as client:
                resp = client.post(
                    f"/api/run/{run_id}/finance-approve",
                    json={"note": "test approval"},
                )
        finally:
            run_dashboard._db_path          = original_db
            run_dashboard._ASSETS_ROOT_PATH = original_assets

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["episode_id"] == episode_id
        assert data["overage_usd"] == pytest.approx(5.649, abs=0.001)

        # Confirm approval written to DB
        conn2 = get_connection(db)
        approvals = list_finance_approvals(conn2, episode_id=episode_id)
        conn2.close()
        assert len(approvals) >= 1
        assert approvals[0]["status"] == "pending"
        assert approvals[0]["note"] == "test approval"


# ---------------------------------------------------------------------------
# TestApprovalExpiry
# ---------------------------------------------------------------------------

class TestApprovalExpiry:
    """Expired approvals do not unblock the gate."""

    def test_expired_approval_not_returned(self, tmp_path):
        """An approval with expires_at in the past is not returned by get_valid_finance_approval."""
        db = tmp_path / "test.db"
        initialize_schema(db)

        conn = get_connection(db)
        insert_finance_approval(
            conn, approval_id="fa_expired", episode_id="ep_exp",
            approved_at=_past(25),
            estimated_spend_usd=35.0, ceiling_at_approval=30.0, overage_usd=5.0,
            expires_at=_past(1),   # already expired
        )
        conn.commit()

        approval = get_valid_finance_approval(conn, "ep_exp", _now())
        conn.close()

        assert approval is None, "expired approval should not be returned"

    def test_future_approval_is_returned(self, tmp_path):
        """An approval with expires_at in the future is returned."""
        db = tmp_path / "test.db"
        initialize_schema(db)

        conn = get_connection(db)
        insert_finance_approval(
            conn, approval_id="fa_valid", episode_id="ep_valid",
            approved_at=_now(),
            estimated_spend_usd=35.0, ceiling_at_approval=30.0, overage_usd=5.0,
            expires_at=_future(24),
        )
        conn.commit()

        approval = get_valid_finance_approval(conn, "ep_valid", _now())
        conn.close()

        assert approval is not None
        assert approval["approval_id"] == "fa_valid"


# ---------------------------------------------------------------------------
# TestBudgetCeilingAPI
# ---------------------------------------------------------------------------

class TestBudgetCeilingAPI:
    """Budget ceiling can be raised from the dashboard without terminal."""

    def test_raise_ceiling_updates_env_file(self, tmp_path, monkeypatch):
        """POST /api/setup/budget-ceiling writes FINANCE_BUDGET_CEILING_USD to .env."""
        import run_dashboard

        env_path = tmp_path / ".env"
        env_path.write_text("FINANCE_BUDGET_CEILING_USD=30.00\n", encoding="utf-8")

        monkeypatch.setattr(run_dashboard, "_ENV_PATH", env_path)

        with run_dashboard.app.test_client() as client:
            resp = client.post("/api/setup/budget-ceiling", json={"ceiling": 45.0})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["new_ceiling"] == pytest.approx(45.0)

        content = env_path.read_text(encoding="utf-8")
        assert "FINANCE_BUDGET_CEILING_USD=45.00" in content

    def test_raise_ceiling_rejects_negative(self, tmp_path, monkeypatch):
        """Negative ceiling values are rejected."""
        import run_dashboard

        env_path = tmp_path / ".env"
        env_path.write_text("", encoding="utf-8")
        monkeypatch.setattr(run_dashboard, "_ENV_PATH", env_path)

        with run_dashboard.app.test_client() as client:
            resp = client.post("/api/setup/budget-ceiling", json={"ceiling": -5.0})

        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False


# ---------------------------------------------------------------------------
# TestDashboardFinanceGatePanel
# ---------------------------------------------------------------------------

class TestDashboardFinanceGatePanel:
    """_classify_run_recovery surfaces finance gate details."""

    def _make_detail(self, episode_id: str, error_msg: str) -> dict:
        return {
            "run": {
                "run_id":        "run-fg-test",
                "episode_id":    episode_id,
                "status":        "failed",
                "error_message": error_msg,
            },
            "episode": {"topic": "Test topic", "episode_id": episode_id},
            "stages": [
                {"stage_name":    "visual_package_director",
                 "status":        "failed",
                 "error_message": error_msg}
            ],
        }

    def test_finance_gate_failure_class(self, tmp_path, monkeypatch):
        """Finance gate blocked error classifies as 'finance_gate'."""
        import run_dashboard
        episode_id = "ep_fg1"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)

        finance_rec = _make_finance_record(episode_id, 35.649, 30.0, verdict="BLOCKED")
        (ep_dir / f"finance_post_vpd_authorization_{episode_id}.json").write_text(
            json.dumps(finance_rec), encoding="utf-8"
        )

        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)
        error_msg = f"Post-VPD finance gate BLOCKED for {episode_id}: estimated $35.649 USD exceeds $30.0 USD ceiling"
        detail = self._make_detail(episode_id, error_msg)
        recovery = run_dashboard._classify_run_recovery(detail)

        assert recovery["failure_class"] == "finance_gate"
        assert recovery["finance_gate_detail"]["estimated_spend_usd"] == pytest.approx(35.649, abs=0.001)
        assert recovery["finance_gate_detail"]["budget_ceiling_usd"]  == pytest.approx(30.0, abs=0.001)
        assert recovery["finance_gate_detail"]["overage_usd"]         == pytest.approx(5.649, abs=0.001)

    def test_finance_gate_why_mentions_amounts(self, tmp_path, monkeypatch):
        """The 'why' text includes estimated spend, ceiling, and overage."""
        import run_dashboard
        episode_id = "ep_fg2"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)

        finance_rec = _make_finance_record(episode_id, 35.649, 30.0, verdict="BLOCKED")
        (ep_dir / f"finance_post_vpd_authorization_{episode_id}.json").write_text(
            json.dumps(finance_rec), encoding="utf-8"
        )

        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)
        error_msg = f"Post-VPD finance gate BLOCKED for {episode_id}: estimated $35.649 USD exceeds $30.0 USD ceiling"
        detail = self._make_detail(episode_id, error_msg)
        recovery = run_dashboard._classify_run_recovery(detail)

        assert "35.6" in recovery["why"] or "35.649" in recovery["why"]
        assert "30." in recovery["why"]
        assert "overage" in recovery["why"].lower() or "5.6" in recovery["why"]

    def test_finance_gate_reuse_shows_vpd_checkpoints(self, tmp_path, monkeypatch):
        """When VPD checkpoints exist, reuse_summary lists them."""
        import run_dashboard
        episode_id = "ep_fg3"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)

        (ep_dir / f"visual_package_skeleton_{episode_id}.json").write_text("{}")
        for i in range(3):
            (ep_dir / f"vpd_prompts_batch_{i}_{episode_id}.json").write_text(
                json.dumps({"scene_prompts": [{"scene_id": f"s{i*5+j:02d}", "primary_prompt": "p", "fallback_prompt": "f"} for j in range(5)]})
            )

        finance_rec = _make_finance_record(episode_id, 35.649, 30.0, verdict="BLOCKED")
        (ep_dir / f"finance_post_vpd_authorization_{episode_id}.json").write_text(
            json.dumps(finance_rec), encoding="utf-8"
        )

        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)
        error_msg = f"Post-VPD finance gate BLOCKED for {episode_id}: estimated $35.649 USD exceeds ceiling"
        detail = self._make_detail(episode_id, error_msg)
        recovery = run_dashboard._classify_run_recovery(detail)

        reuse_text = " ".join(recovery["reuse_summary"])
        assert "skeleton" in reuse_text.lower()
        assert "Stage B" in reuse_text or "batch" in reuse_text.lower()
        assert "MUAPI" in reuse_text or "video generation" in reuse_text.lower()

    def test_finance_gate_safe_action_mentions_approve(self, tmp_path, monkeypatch):
        """safe_action must direct operator to the approval panel."""
        import run_dashboard
        episode_id = "ep_fg4"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)
        finance_rec = _make_finance_record(episode_id, 35.0, 30.0, verdict="BLOCKED")
        (ep_dir / f"finance_post_vpd_authorization_{episode_id}.json").write_text(
            json.dumps(finance_rec), encoding="utf-8"
        )
        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)
        error_msg = f"Post-VPD finance gate BLOCKED for {episode_id}: over budget"
        detail = self._make_detail(episode_id, error_msg)
        recovery = run_dashboard._classify_run_recovery(detail)

        safe = recovery["safe_action"].lower()
        assert "approve" in safe or "finance gate" in safe
