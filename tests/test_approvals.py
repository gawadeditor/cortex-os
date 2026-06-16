"""
tests/test_approvals.py — P3-04: Approval System tests

Coverage:
    TestCreateApprovalRequest  — DB record creation, field values, OIS signal
    TestApprove                — status update, RLG transition, OIS signal
    TestReject                 — status update, RLG transition, OIS signal
    TestGetApproval            — fetch by approval_id, None for missing
    TestListPendingApprovals   — filtered list, cross-run filtering
    TestIsApproved             — True/False based on DB state
    TestExpireStaleApprovals   — age-based expiry
    TestPermissions            — viewer cannot approve; operator can
    TestPipelineApprovalGate   — pipeline returns status="awaiting_review"
    TestApproveRunCLI          — CLI approve_run.py basic smoke

All tests use isolated tmp_path SQLite DBs. No production DB accessed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.approvals import (
    VALID_APPROVAL_TYPES,
    ApprovalAlreadyDecided,
    ApprovalNotFoundError,
    ApprovalRecord,
    PermissionDeniedError,
    approve,
    create_approval_request,
    expire_stale_approvals,
    get_approval,
    is_approved,
    list_pending_approvals,
    reject,
)
from hypogeum.auth import create_user
from hypogeum.db import get_connection, initialize_schema
from hypogeum.run_registry import register_run, get_run_state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "approval_test.db"
    initialize_schema(p)
    return p


def _make_config():
    cfg = MagicMock()
    cfg.EP_MODEL = "anthropic/claude-sonnet-4.6"
    cfg.RD_MODEL = "anthropic/claude-opus-4.6"
    cfg.TD_PLANNING_MODEL = "openai/gpt-5.5-pro"
    cfg.BGA_PASS1_MODEL = "anthropic/claude-sonnet-4.6"
    cfg.BGA_PASS2_MODEL = "openai/gpt-5.5-pro"
    return cfg


def _seed_run(db_path: Path, episode_id: str = "ep_001") -> str:
    """Register a real run and return run_id."""
    return register_run(
        episode_id, _make_config(),
        db_path=db_path,
        runs_dir=db_path.parent / "runs",
    )


def _seed_approval(db_path: Path, run_id: str, episode_id: str = "ep_001",
                   approval_type: str = "human_review") -> str:
    return create_approval_request(
        run_id=run_id,
        episode_id=episode_id,
        approval_type=approval_type,
        requested_by="system:pipeline",
        db_path=db_path,
    )


# ===========================================================================
# TestCreateApprovalRequest
# ===========================================================================

class TestCreateApprovalRequest:
    def test_returns_approval_id_string(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        assert isinstance(aid, str)
        assert len(aid) == 36  # UUID4 format

    def test_creates_pending_record(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        record = get_approval(aid, db_path=db_path)
        assert record is not None
        assert record.status == "pending"

    def test_fields_stored_correctly(self, db_path):
        run_id = _seed_run(db_path)
        aid = create_approval_request(
            run_id=run_id,
            episode_id="ep_001",
            approval_type="human_review",
            requested_by="system:pipeline",
            evidence={"key": "val"},
            db_path=db_path,
        )
        r = get_approval(aid, db_path=db_path)
        assert r.run_id == run_id
        assert r.episode_id == "ep_001"
        assert r.approval_type == "human_review"
        assert r.requested_by == "system:pipeline"
        assert r.evidence == {"key": "val"}
        assert r.decided_by is None
        assert r.decided_at is None

    def test_all_valid_types_accepted(self, db_path):
        run_id = _seed_run(db_path)
        for i, atype in enumerate(sorted(VALID_APPROVAL_TYPES)):
            aid = create_approval_request(
                run_id=run_id, episode_id=f"ep_{i:02d}",
                approval_type=atype, requested_by="system:pipeline",
                db_path=db_path,
            )
            assert get_approval(aid, db_path=db_path).approval_type == atype

    def test_invalid_type_raises_value_error(self, db_path):
        run_id = _seed_run(db_path)
        with pytest.raises(ValueError, match="Invalid approval_type"):
            create_approval_request(
                run_id=run_id, episode_id="ep_x",
                approval_type="invalid_type",
                requested_by="system:pipeline",
                db_path=db_path,
            )

    def test_notification_signal_emitted(self, db_path):
        run_id = _seed_run(db_path)
        with patch("hypogeum.approvals._emit_signal_nonblocking") as mock_emit:
            _seed_approval(db_path, run_id)
        mock_emit.assert_called_once()
        assert mock_emit.call_args.kwargs["signal_type"] == "approval_pending"

    def test_different_calls_produce_different_ids(self, db_path):
        run_id = _seed_run(db_path)
        a1 = _seed_approval(db_path, run_id)
        a2 = _seed_approval(db_path, run_id)
        assert a1 != a2


# ===========================================================================
# TestApprove
# ===========================================================================

class TestApprove:
    def test_approve_sets_status_approved(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        approve(aid, "system:operator", db_path=db_path)
        record = get_approval(aid, db_path=db_path)
        assert record.status == "approved"

    def test_approve_records_decided_by(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        approve(aid, "system:operator", notes="looks good", db_path=db_path)
        record = get_approval(aid, db_path=db_path)
        assert record.decided_by == "system:operator"
        assert record.decided_at is not None
        assert record.notes == "looks good"

    def test_approve_emits_approval_granted_signal(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        with patch("hypogeum.approvals._emit_signal_nonblocking") as mock_emit:
            approve(aid, "system:operator", db_path=db_path)
        signal_types = [c.kwargs["signal_type"] for c in mock_emit.call_args_list]
        assert "approval_granted" in signal_types

    def test_approve_transitions_run_to_awaiting_publish_approval(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        # run is in 'running' → need to transition to awaiting_review first
        from hypogeum.run_registry import transition_run_state
        transition_run_state(run_id, "awaiting_review",
                             "system:pipeline", "test", db_path=db_path)
        approve(aid, "system:operator", db_path=db_path)
        assert get_run_state(run_id, db_path=db_path) == "awaiting_publish_approval"

    def test_approve_returns_true(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        assert approve(aid, "system:operator", db_path=db_path) is True

    def test_approve_raises_not_found_for_unknown_id(self, db_path):
        with pytest.raises(ApprovalNotFoundError):
            approve("ghost-approval-id", "system:operator", db_path=db_path)

    def test_approve_raises_already_decided_on_second_call(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        approve(aid, "system:operator", db_path=db_path)
        with pytest.raises(ApprovalAlreadyDecided):
            approve(aid, "system:operator", db_path=db_path)


# ===========================================================================
# TestReject
# ===========================================================================

class TestReject:
    def test_reject_sets_status_rejected(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        reject(aid, "system:operator", notes="not ready", db_path=db_path)
        record = get_approval(aid, db_path=db_path)
        assert record.status == "rejected"

    def test_reject_records_decided_by(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        reject(aid, "system:operator", notes="needs revision", db_path=db_path)
        record = get_approval(aid, db_path=db_path)
        assert record.decided_by == "system:operator"
        assert record.notes == "needs revision"

    def test_reject_emits_approval_rejected_signal(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        with patch("hypogeum.approvals._emit_signal_nonblocking") as mock_emit:
            reject(aid, "system:operator", db_path=db_path)
        signal_types = [c.kwargs["signal_type"] for c in mock_emit.call_args_list]
        assert "approval_rejected" in signal_types

    def test_reject_transitions_run_to_failed(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        reject(aid, "system:operator", db_path=db_path)
        assert get_run_state(run_id, db_path=db_path) == "failed"

    def test_reject_raises_already_decided_if_not_pending(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        approve(aid, "system:operator", db_path=db_path)
        with pytest.raises(ApprovalAlreadyDecided):
            reject(aid, "system:operator", db_path=db_path)


# ===========================================================================
# TestGetApproval
# ===========================================================================

class TestGetApproval:
    def test_returns_approval_record(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        record = get_approval(aid, db_path=db_path)
        assert isinstance(record, ApprovalRecord)
        assert record.approval_id == aid

    def test_returns_none_for_missing(self, db_path):
        assert get_approval("ghost", db_path=db_path) is None


# ===========================================================================
# TestListPendingApprovals
# ===========================================================================

class TestListPendingApprovals:
    def test_returns_only_pending(self, db_path):
        run_id = _seed_run(db_path)
        aid_pending = _seed_approval(db_path, run_id)
        aid_approved = _seed_approval(db_path, run_id)
        approve(aid_approved, "system:operator", db_path=db_path)

        pending = list_pending_approvals(db_path=db_path)
        ids = {r.approval_id for r in pending}
        assert aid_pending in ids
        assert aid_approved not in ids

    def test_filter_by_run_id(self, db_path):
        run_a = _seed_run(db_path, "ep_a")
        run_b = _seed_run(db_path, "ep_b")
        aid_a = _seed_approval(db_path, run_a, "ep_a")
        _seed_approval(db_path, run_b, "ep_b")

        pending_a = list_pending_approvals(run_id=run_a, db_path=db_path)
        assert len(pending_a) == 1
        assert pending_a[0].approval_id == aid_a

    def test_empty_when_none_pending(self, db_path):
        assert list_pending_approvals(db_path=db_path) == []

    def test_test_is_approved_returns_false_for_pending(self, db_path):
        run_id = _seed_run(db_path)
        _seed_approval(db_path, run_id)
        assert is_approved(run_id, "human_review", db_path=db_path) is False


# ===========================================================================
# TestIsApproved
# ===========================================================================

class TestIsApproved:
    def test_false_for_pending(self, db_path):
        run_id = _seed_run(db_path)
        _seed_approval(db_path, run_id)
        assert is_approved(run_id, "human_review", db_path=db_path) is False

    def test_true_after_approve(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        approve(aid, "system:operator", db_path=db_path)
        assert is_approved(run_id, "human_review", db_path=db_path) is True

    def test_false_after_reject(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        reject(aid, "system:operator", db_path=db_path)
        assert is_approved(run_id, "human_review", db_path=db_path) is False

    def test_false_for_different_type(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id, approval_type="human_review")
        approve(aid, "system:operator", db_path=db_path)
        assert is_approved(run_id, "publish_approval", db_path=db_path) is False


# ===========================================================================
# TestExpireStaleApprovals
# ===========================================================================

class TestExpireStaleApprovals:
    def test_expire_stale_approvals_marks_old_pending_as_expired(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        # Back-date the requested_at to 100 hours ago
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        conn = get_connection(db_path=db_path)
        try:
            conn.execute(
                "UPDATE approvals SET requested_at = ? WHERE approval_id = ?",
                (old_ts, aid),
            )
            conn.commit()
        finally:
            conn.close()

        count = expire_stale_approvals(older_than_hours=72, db_path=db_path)
        assert count == 1
        record = get_approval(aid, db_path=db_path)
        assert record.status == "expired"

    def test_recent_pending_not_expired(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        count = expire_stale_approvals(older_than_hours=72, db_path=db_path)
        assert count == 0
        assert get_approval(aid, db_path=db_path).status == "pending"

    def test_already_decided_not_affected(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        approve(aid, "system:operator", db_path=db_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        conn = get_connection(db_path=db_path)
        try:
            conn.execute(
                "UPDATE approvals SET requested_at = ? WHERE approval_id = ?",
                (old_ts, aid),
            )
            conn.commit()
        finally:
            conn.close()
        count = expire_stale_approvals(older_than_hours=72, db_path=db_path)
        assert count == 0  # already approved, not pending
        assert get_approval(aid, db_path=db_path).status == "approved"


# ===========================================================================
# TestPermissions
# ===========================================================================

class TestPermissions:
    def test_system_actor_bypasses_auth(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        # system: prefix bypasses DB auth check
        result = approve(aid, "system:pipeline", db_path=db_path)
        assert result is True

    def test_operator_user_can_approve(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        user = create_user(
            email="operator@example.com", name="Op User",
            role="operator", db_path=db_path,
        )
        result = approve(aid, user.user_id, db_path=db_path)
        assert result is True

    def test_admin_user_can_approve(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        user = create_user(
            email="admin@example.com", name="Admin User",
            role="admin", db_path=db_path,
        )
        result = approve(aid, user.user_id, db_path=db_path)
        assert result is True

    def test_viewer_cannot_approve(self, db_path):
        """Spec: test_approve_requires_valid_user_role — viewer cannot approve."""
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        user = create_user(
            email="viewer@example.com", name="Viewer User",
            role="viewer", db_path=db_path,
        )
        with pytest.raises(PermissionDeniedError):
            approve(aid, user.user_id, db_path=db_path)

    def test_editor_cannot_approve(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        user = create_user(
            email="editor@example.com", name="Editor User",
            role="editor", db_path=db_path,
        )
        with pytest.raises(PermissionDeniedError):
            approve(aid, user.user_id, db_path=db_path)

    def test_viewer_cannot_reject(self, db_path):
        run_id = _seed_run(db_path)
        aid = _seed_approval(db_path, run_id)
        user = create_user(
            email="viewer2@example.com", name="Viewer 2",
            role="viewer", db_path=db_path,
        )
        with pytest.raises(PermissionDeniedError):
            reject(aid, user.user_id, db_path=db_path)

    def test_permission_denied_error_is_runtime_error(self):
        assert issubclass(PermissionDeniedError, RuntimeError)

    def test_approval_not_found_error_is_runtime_error(self):
        assert issubclass(ApprovalNotFoundError, RuntimeError)

    def test_approval_already_decided_is_runtime_error(self):
        assert issubclass(ApprovalAlreadyDecided, RuntimeError)


# ===========================================================================
# TestPipelineApprovalGate
# ===========================================================================

class TestPipelineApprovalGate:
    """
    Verify that run_pipeline() returns status="awaiting_review" when the
    compliance agent sets human_review_required=True on its report.
    """

    def _minimal_compliance_with_review(self) -> dict:
        return {
            "verdict": "APPROVED",
            "failures": [],
            "human_review_required": True,
        }

    def test_pipeline_returns_awaiting_review_status(self, tmp_path):
        from hypogeum.pipeline import run_pipeline
        from tests.test_pipeline import (
            MINIMAL_FINAL_RENDER_PACKAGE,
            MINIMAL_FINANCE_AUTHORIZATION,
            MINIMAL_NARRATION_PACKAGE,
            MINIMAL_NARRATIVE_PACKAGE,
            MINIMAL_RESEARCH_PACKAGE,
            MINIMAL_SCRIPT_PACKAGE,
            MINIMAL_SCORECARD_PASS,
            MINIMAL_THUMBNAIL_PACKAGE,
            MINIMAL_TOPIC_BRIEF,
            MINIMAL_VISUAL_PACKAGE,
            make_config,
            make_startup_report,
        )

        cfg = make_config()
        qa_report     = {"verdict": "APPROVED", "failures": []}
        archive_rec   = {"verdict": "archived", "archived": True}

        with patch("hypogeum.pipeline.run_startup_checks",
                   return_value=make_startup_report()), \
             patch("hypogeum.pipeline._run_executive_producer",
                   return_value=(MINIMAL_SCORECARD_PASS, MINIMAL_TOPIC_BRIEF)), \
             patch("hypogeum.pipeline._run_research_director",
                   return_value=(MINIMAL_RESEARCH_PACKAGE, None)), \
             patch("hypogeum.pipeline._run_narrative_architect",
                   return_value=MINIMAL_NARRATIVE_PACKAGE), \
             patch("hypogeum.pipeline._run_script_engine",
                   return_value=MINIMAL_SCRIPT_PACKAGE), \
             patch("hypogeum.pipeline._run_budget_authorization",
                   return_value=MINIMAL_FINANCE_AUTHORIZATION), \
             patch("hypogeum.pipeline._run_visual_package_director",
                   return_value=MINIMAL_VISUAL_PACKAGE), \
             patch("hypogeum.pipeline._run_thumbnail_director",
                   return_value=MINIMAL_THUMBNAIL_PACKAGE), \
             patch("hypogeum.pipeline._run_audio_agent",
                   return_value=MINIMAL_NARRATION_PACKAGE), \
             patch("hypogeum.pipeline._run_assembly_agent",
                   return_value=MINIMAL_FINAL_RENDER_PACKAGE), \
             patch("hypogeum.pipeline._run_qa_agent",
                   return_value=qa_report), \
             patch("hypogeum.pipeline._run_compliance_agent",
                   return_value=self._minimal_compliance_with_review()), \
             patch("hypogeum.approvals.create_approval_request",
                   return_value="appr_test_001"), \
             patch("hypogeum.pipeline.transition_run_state"):
            result = run_pipeline(
                topic_input="Test topic",
                episode_id="ep_review_test",
                config=cfg,
                channel_id=None,
            )

        assert result.status == "awaiting_review"

    def test_pipeline_returns_awaiting_review_has_run_id(self, tmp_path):
        from hypogeum.pipeline import run_pipeline
        from tests.test_pipeline import (
            MINIMAL_FINAL_RENDER_PACKAGE, MINIMAL_FINANCE_AUTHORIZATION,
            MINIMAL_NARRATION_PACKAGE, MINIMAL_NARRATIVE_PACKAGE,
            MINIMAL_RESEARCH_PACKAGE, MINIMAL_SCRIPT_PACKAGE,
            MINIMAL_SCORECARD_PASS, MINIMAL_THUMBNAIL_PACKAGE,
            MINIMAL_TOPIC_BRIEF, MINIMAL_VISUAL_PACKAGE,
            make_config, make_startup_report,
        )
        cfg = make_config()
        qa_report = {"verdict": "APPROVED", "failures": []}

        with patch("hypogeum.pipeline.run_startup_checks",
                   return_value=make_startup_report()), \
             patch("hypogeum.pipeline._run_executive_producer",
                   return_value=(MINIMAL_SCORECARD_PASS, MINIMAL_TOPIC_BRIEF)), \
             patch("hypogeum.pipeline._run_research_director",
                   return_value=(MINIMAL_RESEARCH_PACKAGE, None)), \
             patch("hypogeum.pipeline._run_narrative_architect",
                   return_value=MINIMAL_NARRATIVE_PACKAGE), \
             patch("hypogeum.pipeline._run_script_engine",
                   return_value=MINIMAL_SCRIPT_PACKAGE), \
             patch("hypogeum.pipeline._run_budget_authorization",
                   return_value=MINIMAL_FINANCE_AUTHORIZATION), \
             patch("hypogeum.pipeline._run_visual_package_director",
                   return_value=MINIMAL_VISUAL_PACKAGE), \
             patch("hypogeum.pipeline._run_thumbnail_director",
                   return_value=MINIMAL_THUMBNAIL_PACKAGE), \
             patch("hypogeum.pipeline._run_audio_agent",
                   return_value=MINIMAL_NARRATION_PACKAGE), \
             patch("hypogeum.pipeline._run_assembly_agent",
                   return_value=MINIMAL_FINAL_RENDER_PACKAGE), \
             patch("hypogeum.pipeline._run_qa_agent",
                   return_value=qa_report), \
             patch("hypogeum.pipeline._run_compliance_agent",
                   return_value=self._minimal_compliance_with_review()), \
             patch("hypogeum.approvals.create_approval_request",
                   return_value="appr_test_002"), \
             patch("hypogeum.pipeline.transition_run_state"):
            result = run_pipeline(
                topic_input="Test", episode_id="ep_review_2",
                config=cfg, channel_id=None,
            )

        # run_id is set by the mocked register_run in conftest → "test-run-id-isolated"
        assert result.run_id is not None

    def test_pipeline_normal_flow_unaffected_without_human_review(self, tmp_path):
        """When human_review_required is absent, pipeline still completes normally."""
        from hypogeum.pipeline import run_pipeline
        from tests.test_pipeline import (
            MINIMAL_FINAL_RENDER_PACKAGE, MINIMAL_FINANCE_AUTHORIZATION,
            MINIMAL_FINANCE_RECORD, MINIMAL_NARRATION_PACKAGE,
            MINIMAL_NARRATIVE_PACKAGE, MINIMAL_PUBLISHING_RECORD,
            MINIMAL_RESEARCH_PACKAGE, MINIMAL_SCRIPT_PACKAGE,
            MINIMAL_SCORECARD_PASS, MINIMAL_THUMBNAIL_PACKAGE,
            MINIMAL_TOPIC_BRIEF, MINIMAL_VISUAL_PACKAGE,
            make_config, make_startup_report,
        )
        cfg = make_config()
        qa_report       = {"verdict": "APPROVED", "failures": []}
        compliance_ok   = {"verdict": "APPROVED", "failures": []}
        archive_rec     = {"verdict": "archived",  "archived": True}
        asset_arc       = {"verdict": "archived",  "catalog_summary": {"total_assets_indexed": 0}}

        with patch("hypogeum.pipeline.run_startup_checks",
                   return_value=make_startup_report()), \
             patch("hypogeum.pipeline._run_executive_producer",
                   return_value=(MINIMAL_SCORECARD_PASS, MINIMAL_TOPIC_BRIEF)), \
             patch("hypogeum.pipeline._run_research_director",
                   return_value=(MINIMAL_RESEARCH_PACKAGE, None)), \
             patch("hypogeum.pipeline._run_narrative_architect",
                   return_value=MINIMAL_NARRATIVE_PACKAGE), \
             patch("hypogeum.pipeline._run_script_engine",
                   return_value=MINIMAL_SCRIPT_PACKAGE), \
             patch("hypogeum.pipeline._run_budget_authorization",
                   return_value=MINIMAL_FINANCE_AUTHORIZATION), \
             patch("hypogeum.pipeline._run_visual_package_director",
                   return_value=MINIMAL_VISUAL_PACKAGE), \
             patch("hypogeum.pipeline._run_thumbnail_director",
                   return_value=MINIMAL_THUMBNAIL_PACKAGE), \
             patch("hypogeum.pipeline._run_audio_agent",
                   return_value=MINIMAL_NARRATION_PACKAGE), \
             patch("hypogeum.pipeline._run_assembly_agent",
                   return_value=MINIMAL_FINAL_RENDER_PACKAGE), \
             patch("hypogeum.pipeline._run_qa_agent",
                   return_value=qa_report), \
             patch("hypogeum.pipeline._run_compliance_agent",
                   return_value=compliance_ok), \
             patch("hypogeum.pipeline._run_publishing_agent",
                   return_value=MINIMAL_PUBLISHING_RECORD), \
             patch("hypogeum.pipeline._run_cost_ledger",
                   return_value=MINIMAL_FINANCE_RECORD), \
             patch("hypogeum.pipeline._run_archive_agent",
                   return_value=archive_rec), \
             patch("hypogeum.pipeline._run_asset_archive",
                   return_value=asset_arc), \
             patch("hypogeum.pipeline.write_production_metadata",
                   return_value={}), \
             patch("hypogeum.pipeline.run_post_run_intelligence"):
            result = run_pipeline(
                topic_input="Test", episode_id="ep_normal",
                config=cfg, channel_id=None,
            )

        assert result.status == "completed"


# ===========================================================================
# TestApproveRunCLI
# ===========================================================================

class TestApproveRunCLI:
    def test_cli_approve(self, db_path):
        import scripts.approve_run as ar_mod
        # Patch in the scripts module namespace (local reference after import)
        with patch.object(ar_mod, "approve", return_value=True) as mock_approve:
            rc = ar_mod.main([
                "--approval-id", "appr-001",
                "--user-id", "op_user_1",
                "--notes", "CLI test",
            ])
        assert rc == 0
        mock_approve.assert_called_once_with("appr-001", "op_user_1", notes="CLI test")

    def test_cli_reject(self, db_path):
        import scripts.approve_run as ar_mod
        with patch.object(ar_mod, "reject", return_value=True) as mock_reject:
            rc = ar_mod.main([
                "--approval-id", "appr-002",
                "--user-id", "op_user_1",
                "--reject",
            ])
        assert rc == 0
        mock_reject.assert_called_once_with("appr-002", "op_user_1", notes=None)

    def test_cli_permission_denied_exits_1(self):
        import scripts.approve_run as ar_mod
        with patch.object(ar_mod, "approve",
                          side_effect=PermissionDeniedError("viewer_1")):
            rc = ar_mod.main(["--approval-id", "some-id", "--user-id", "viewer_1"])
        assert rc == 1

    def test_cli_list_pending_empty(self):
        import scripts.approve_run as ar_mod
        with patch.object(ar_mod, "list_pending_approvals", return_value=[]):
            rc = ar_mod.main(["--list-pending"])
        assert rc == 0
