"""
tests/test_worker_integration.py — run_worker.py <-> ChannelOperationsProfile
<-> BackwardPlan integration tests (2026-07-21).

run_gated_episode() (run_worker.py) is a queue-agnostic, single-episode
entry point that gates hypogeum.pipeline.run_pipeline() with a
ChannelOperationsProfile and a BackwardPlan. hypogeum.pipeline.run_pipeline
is always mocked here — this suite tests the gating/wiring, not the
pipeline itself (which has its own extensive test coverage).

Coverage:
    TestInvocationModeGate     — unknown mode, operator/production/unattended gates
    TestOperationsProfileGate  — missing profile fails closed
    TestBackwardPlanGate       — load/build/mismatch/corruption/infeasible
    TestResumeAndCheckpoint    — plan_id preserved, checkpoint_status, stale rows
    TestPlatformFiltering      — eligible/excluded platforms, publishing disabled
    TestTopicSelectionSynthetic — synthetic stage treated as satisfied evidence
    TestStateTruth             — never reports success on a blocked/failed path
    TestPersistenceFailsClosed — worker-run metadata write failures propagate
    TestNoSchedulerBehavior    — scheduler.py / queue.py never touched
    TestProfileAndPlanUntouched — ChannelOperationsProfile / plan artifact immutability
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import run_worker
from hypogeum import worker_integration as wi
from hypogeum.backward_planner import load_backward_plan, save_backward_plan
from hypogeum.channel_config import (
    create_channel_operations_profile,
    create_channel_profile,
    get_channel_operations_profile,
)
from hypogeum.db import get_connection, initialize_schema


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "worker_integration_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def project_root(tmp_path):
    root = tmp_path / "project_root"
    root.mkdir()
    return root


def _make_operations_profile(db_path, channel_id="hypogeum", **overrides):
    create_channel_profile(channel_id=channel_id, name="The Hypogeum", db_path=db_path)
    kwargs = dict(
        channel_id=channel_id,
        channel_name="The Hypogeum",
        timezone="Asia/Dubai",
        operator_enabled=True,
        production_cadence={"videos_per_week": 1},
        publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
        enabled_platforms={"youtube": True, "facebook": False, "tiktok": False, "instagram": False},
        blocked_platform_reasons={
            "facebook": "Meta app permission/configuration",
            "tiktok": "upload-init endpoint HTTP 404",
            "instagram": "public staging URL not configured",
        },
        discovery_enabled=True,
        production_enabled=True,
        publishing_enabled=True,
        analytics_sync_enabled=True,
        unattended_operation_enabled=False,
        max_concurrent_episodes=1,
        retry_policy={"max_retries_per_stage": 1, "backoff_seconds": 60},
        failure_halt_policy={
            "halt_on_production_failure": True,
            "halt_on_validation_failure": True,
            "halt_on_enabled_platform_publish_failure": True,
            "blocked_or_disabled_platforms_do_not_halt": True,
        },
        db_path=db_path,
    )
    kwargs.update(overrides)
    return create_channel_operations_profile(**kwargs)


def _ensure_run_row(db_path, episode_id, run_id="run_x"):
    conn = get_connection(db_path)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO runs (run_id, episode_id, started_at, status)
               VALUES (?,?,?,?)""",
            (run_id, episode_id, datetime.now(timezone.utc).isoformat(), "running"),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_stage_row(db_path, episode_id, stage_name, started_at, completed_at=None, status="completed"):
    _ensure_run_row(db_path, episode_id)
    conn = get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO stage_executions
               (stage_exec_id, run_id, episode_id, stage_name, started_at,
                completed_at, status, skipped)
               VALUES (?,?,?,?,?,?,?,0)""",
            (f"se_{stage_name}_{started_at}", "run_x", episode_id, stage_name,
             started_at, completed_at, status),
        )
        conn.commit()
    finally:
        conn.close()


_TARGET = datetime(2026, 9, 3, 18, 0, tzinfo=ZoneInfo("Asia/Dubai"))
_GENERATED = datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)


def _fake_pipeline_success(db_path=None, episode_id=None, **_):
    """Records a couple of plausible stage_executions rows, mirroring what a
    real run_pipeline() call would leave behind, without calling any agent."""
    now = datetime.now(timezone.utc).isoformat()
    if db_path is not None and episode_id is not None:
        _insert_stage_row(db_path, episode_id, "startup_checks", now, now, "completed")
        _insert_stage_row(db_path, episode_id, "executive_producer", now, now, "completed")


def _patch_pipeline_success(monkeypatch, call_log=None):
    def _fake(*, topic_input, episode_id, config, channel_id, db_path, youtube_privacy=None):
        if call_log is not None:
            call_log.append((episode_id, topic_input, channel_id))
        _fake_pipeline_success(db_path=db_path, episode_id=episode_id)
    monkeypatch.setattr("hypogeum.pipeline.run_pipeline", _fake)


def _patch_pipeline_raises(monkeypatch, exc):
    def _fake(*args, **kwargs):
        raise exc
    monkeypatch.setattr("hypogeum.pipeline.run_pipeline", _fake)


# ===========================================================================
# TestInvocationModeGate
# ===========================================================================

class TestInvocationModeGate:
    def test_unknown_invocation_mode_fails_closed(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)

        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_bad_mode", invocation_mode="nightly",
            config=object(), db_path=db_path, topic_input="A Topic",
            project_root=project_root,
        )
        assert result.success is False
        assert "unknown invocation_mode" in result.blocked_reason
        assert call_log == []

    def test_operator_disabled_refuses_manual(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, operator_enabled=False)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_op_manual", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert result.success is False
        assert "operator_enabled" in result.blocked_reason
        assert call_log == []

    def test_operator_disabled_refuses_scheduled(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, operator_enabled=False, unattended_operation_enabled=True)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_op_sched", invocation_mode="scheduled",
            config=object(), db_path=db_path, topic_input="A Topic",
            target_publish_at=_TARGET, generated_at=_GENERATED, project_root=project_root,
        )
        assert result.success is False
        assert "operator_enabled" in result.blocked_reason
        assert call_log == []

    def test_production_disabled_refuses_manual(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, production_enabled=False)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_prod_manual", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert result.success is False
        assert "production_enabled" in result.blocked_reason
        assert call_log == []

    def test_production_disabled_refuses_scheduled(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, production_enabled=False, unattended_operation_enabled=True)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_prod_sched", invocation_mode="scheduled",
            config=object(), db_path=db_path, topic_input="A Topic",
            target_publish_at=_TARGET, generated_at=_GENERATED, project_root=project_root,
        )
        assert result.success is False
        assert "production_enabled" in result.blocked_reason
        assert call_log == []

    def test_manual_mode_works_with_unattended_disabled(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, unattended_operation_enabled=False)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_manual_ok", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert result.success is True
        assert result.blocked_reason is None
        assert call_log == [("ep_manual_ok", "A Topic", "hypogeum")]

    def test_scheduled_mode_refuses_when_unattended_disabled(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, unattended_operation_enabled=False)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_sched_blocked", invocation_mode="scheduled",
            config=object(), db_path=db_path, topic_input="A Topic",
            target_publish_at=_TARGET, generated_at=_GENERATED, project_root=project_root,
        )
        assert result.success is False
        assert "unattended_operation_enabled" in result.blocked_reason
        assert call_log == []

    def test_scheduled_mode_works_when_unattended_enabled(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, unattended_operation_enabled=True)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_sched_ok", invocation_mode="scheduled",
            config=object(), db_path=db_path, topic_input="A Topic",
            target_publish_at=_TARGET, generated_at=_GENERATED, project_root=project_root,
        )
        assert result.success is True
        assert call_log == [("ep_sched_ok", "A Topic", "hypogeum")]


# ===========================================================================
# TestOperationsProfileGate
# ===========================================================================

class TestOperationsProfileGate:
    def test_missing_operations_profile_fails_closed(self, db_path, project_root, monkeypatch):
        # No create_channel_operations_profile() call at all — not even a channel_profiles row.
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="ghost_channel", episode_id="ep_no_profile", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert result.success is False
        assert "no ChannelOperationsProfile exists" in result.blocked_reason
        assert call_log == []


# ===========================================================================
# TestBackwardPlanGate
# ===========================================================================

class TestBackwardPlanGate:
    def test_target_publish_at_builds_and_saves_one_deterministic_plan(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_plan_build", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic",
            target_publish_at=_TARGET, generated_at=_GENERATED, project_root=project_root,
        )
        assert result.success is True
        assert result.plan_id is not None
        assert result.plan_status == "executable"

        saved = load_backward_plan("ep_plan_build", project_root=project_root)
        assert saved is not None
        assert saved.plan_id == result.plan_id

    def test_existing_valid_plan_artifact_loads(self, db_path, project_root, monkeypatch):
        profile = _make_operations_profile(db_path)
        from hypogeum.backward_planner import build_backward_plan
        plan = build_backward_plan("hypogeum", _TARGET, profile, generated_at=_GENERATED)
        save_backward_plan(plan, "ep_existing_plan", project_root=project_root)

        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_existing_plan", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert result.success is True
        assert result.plan_id == plan.plan_id

    def test_mismatched_plan_channel_id_is_rejected(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, channel_id="hypogeum")
        other_profile = _make_operations_profile(db_path, channel_id="other_channel")
        from hypogeum.backward_planner import build_backward_plan
        plan = build_backward_plan("other_channel", _TARGET, other_profile, generated_at=_GENERATED)
        save_backward_plan(plan, "ep_mismatch", project_root=project_root)

        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_mismatch", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert result.success is False
        assert "does not match" in result.blocked_reason
        assert call_log == []

    def test_corrupted_plan_artifact_is_rejected(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        episode_dir = project_root / ".hypogeum-assets" / "episodes" / "ep_corrupt"
        episode_dir.mkdir(parents=True)
        (episode_dir / "backward_plan_ep_corrupt.json").write_text("{not valid json", encoding="utf-8")

        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_corrupt", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert result.success is False
        assert "corrupted" in result.blocked_reason
        assert call_log == []

    def test_infeasible_plan_prevents_stage_execution(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        too_close_target = _GENERATED.astimezone(ZoneInfo("Asia/Dubai")) + timedelta(hours=1)

        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_infeasible", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic",
            target_publish_at=too_close_target, generated_at=_GENERATED, project_root=project_root,
        )
        assert result.success is False
        assert result.plan_status == "infeasible"
        assert "infeasible" in result.blocked_reason
        assert result.required_lead_time_seconds is not None
        assert result.available_lead_time_seconds is not None
        assert call_log == []

    def test_manual_mode_with_no_plan_and_no_target_is_allowed(self, db_path, project_root, monkeypatch):
        """The pre-existing unscheduled supervised flow — manual mode may run
        with no plan enforced at all when neither an artifact nor a target
        is supplied."""
        _make_operations_profile(db_path)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_no_plan", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert result.success is True
        assert result.plan_id is None
        assert result.plan_status is None
        assert result.schedule_status == "not_applicable"

    def test_scheduled_mode_with_no_plan_and_no_target_fails_closed(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, unattended_operation_enabled=True)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_sched_no_plan", invocation_mode="scheduled",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert result.success is False
        assert "requires an existing backward plan artifact" in result.blocked_reason
        assert call_log == []


# ===========================================================================
# TestResumeAndCheckpoint
# ===========================================================================

class TestResumeAndCheckpoint:
    def test_resume_reuses_the_same_plan_id(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)

        first = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_resume", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic",
            target_publish_at=_TARGET, generated_at=_GENERATED, project_root=project_root,
        )
        assert first.checkpoint_status == "fresh"

        second = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_resume", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert second.plan_id == first.plan_id
        assert second.checkpoint_status == "resumed"

    def test_completed_stages_are_not_rerun(self, db_path, project_root, monkeypatch):
        """Confirms our wrapper never clears/resets stage_executions between
        invocations of the same episode_id — actual stage-skip logic lives
        in pipeline.py and is exercised by that module's own tests."""
        _make_operations_profile(db_path)
        now = datetime.now(timezone.utc).isoformat()
        _insert_stage_row(db_path, "ep_no_rerun", "startup_checks", now, now, "completed")

        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_no_rerun", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert result.checkpoint_status == "resumed"
        assert "startup_checks" in result.completed_stages

    def test_stale_running_checkpoint_does_not_block_a_new_attempt(self, db_path, project_root, monkeypatch):
        """A stale 'running' row (simulating a prior crash mid-stage) must
        not cause our wrapper to refuse or error — orphan/stale-run recovery
        is orphan_runs.py's responsibility, untouched by this integration."""
        _make_operations_profile(db_path)
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        _insert_stage_row(db_path, "ep_stale", "assembly_agent", stale_time, None, "running")

        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_stale", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert result.success is True
        assert result.checkpoint_status == "resumed"

    def test_backward_plan_artifact_unchanged_on_resume(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)

        run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_plan_stable", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic",
            target_publish_at=_TARGET, generated_at=_GENERATED, project_root=project_root,
        )
        plan_path = project_root / ".hypogeum-assets" / "episodes" / "ep_plan_stable" / "backward_plan_ep_plan_stable.json"
        before = plan_path.read_text(encoding="utf-8")

        run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_plan_stable", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        after = plan_path.read_text(encoding="utf-8")
        assert before == after


# ===========================================================================
# TestPlatformFiltering
# ===========================================================================

class TestPlatformFiltering:
    def test_enabled_unblocked_platforms_are_eligible(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_platforms", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert result.eligible_platforms == ["youtube"]

    def test_blocked_platforms_are_excluded_with_reasons(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_blocked_platforms", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert result.excluded_platforms["facebook"] == "Meta app permission/configuration"
        assert result.excluded_platforms["tiktok"] == "upload-init endpoint HTTP 404"
        assert result.excluded_platforms["instagram"] == "public staging URL not configured"

    def test_disabled_but_unblocked_platform_is_excluded_as_not_enabled(self, db_path, project_root, monkeypatch):
        _make_operations_profile(
            db_path,
            enabled_platforms={"youtube": True, "facebook": False, "tiktok": False, "instagram": False},
            blocked_platform_reasons={},
        )
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_disabled_platform", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert result.eligible_platforms == ["youtube"]
        assert result.excluded_platforms["facebook"] == "not enabled for this channel"

    def test_publishing_disabled_skips_publishing_agent(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, publishing_enabled=False)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_pub_disabled", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic",
            target_publish_at=_TARGET, generated_at=_GENERATED, project_root=project_root,
        )
        assert result.success is True
        assert result.eligible_platforms == []
        plan = load_backward_plan("ep_pub_disabled", project_root=project_root)
        assert "publishing_agent" not in [s.stage_name for s in plan.stages]


# ===========================================================================
# TestTopicSelectionSynthetic
# ===========================================================================

class TestTopicSelectionSynthetic:
    def test_topic_selection_marked_completed_via_explicit_topic_input(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_topic_explicit", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert "topic_selection" in result.completed_stages

    def test_topic_selection_marked_completed_via_backlog_resolution(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        from hypogeum.topics import enqueue_topic
        enqueue_topic("hypogeum", "Backlog Topic", db_path=db_path, episode_id="ep_topic_backlog")

        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_topic_backlog", invocation_mode="manual",
            config=object(), db_path=db_path, project_root=project_root,
        )
        assert result.success is True
        assert "topic_selection" in result.completed_stages
        assert call_log[0][1] == "Backlog Topic"

    def test_no_queued_topic_blocks_without_inventing_one(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_no_topic", invocation_mode="manual",
            config=object(), db_path=db_path, project_root=project_root,
        )
        assert result.success is False
        assert "no queued topic available" in result.blocked_reason
        assert call_log == []


# ===========================================================================
# TestStateTruth
# ===========================================================================

class TestStateTruth:
    def test_pipeline_exception_is_reported_as_failure_not_success(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        _patch_pipeline_raises(monkeypatch, RuntimeError("provider exploded"))
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_pipeline_fails", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert result.success is False
        assert "pipeline execution failed" in result.blocked_reason

    def test_deadline_lateness_recorded_without_compressing_plan(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        from hypogeum.backward_planner import build_backward_plan
        profile = get_channel_operations_profile("hypogeum", db_path=db_path)
        plan = build_backward_plan("hypogeum", _TARGET, profile, generated_at=_GENERATED)
        save_backward_plan(plan, "ep_late", project_root=project_root)
        original_durations = {s.stage_name: s.total_planned_seconds for s in plan.stages}

        first_stage = plan.stages[0]
        planned_finish = datetime.fromisoformat(first_stage.planned_finish_at)
        late_finish = (planned_finish + timedelta(hours=5)).isoformat()
        _insert_stage_row(
            db_path, "ep_late", first_stage.stage_name,
            first_stage.planned_start_at, late_finish, "completed",
        )

        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_late", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        risk = {r["stage_name"]: r for r in result.stage_schedule_risk}
        assert risk[first_stage.stage_name]["deadline_status"] == "late"
        assert result.schedule_status == "late"

        # The plan itself must be untouched — no compression of durations.
        reloaded = load_backward_plan("ep_late", project_root=project_root)
        for s in reloaded.stages:
            assert s.total_planned_seconds == original_durations[s.stage_name]


# ===========================================================================
# TestPersistenceFailsClosed
# ===========================================================================

class TestPersistenceFailsClosed:
    def test_worker_run_metadata_persistence_failure_propagates(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)

        def _boom(*args, **kwargs):
            raise wi.WorkerPersistenceError("simulated disk failure")
        monkeypatch.setattr("hypogeum.worker_integration.save_worker_run_result", _boom)

        with pytest.raises(wi.WorkerPersistenceError):
            run_worker.run_gated_episode(
                channel_id="hypogeum", episode_id="ep_persist_fail", invocation_mode="manual",
                config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
            )
        # The pipeline itself must never have been reached before the first
        # (pre-execution) persistence checkpoint fails.
        assert call_log == []

    def test_post_execution_persistence_failure_propagates_not_swallowed_as_success(
        self, db_path, project_root, monkeypatch,
    ):
        _make_operations_profile(db_path)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)

        call_count = {"n": 0}
        real_save = wi.save_worker_run_result

        def _fail_on_second_call(result, project_root=None):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise wi.WorkerPersistenceError("simulated post-run disk failure")
            return real_save(result, project_root=project_root)
        monkeypatch.setattr("hypogeum.worker_integration.save_worker_run_result", _fail_on_second_call)

        with pytest.raises(wi.WorkerPersistenceError):
            run_worker.run_gated_episode(
                channel_id="hypogeum", episode_id="ep_persist_fail_late", invocation_mode="manual",
                config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
            )
        # Pipeline DID run (this is the point of the test — success must not
        # be reported to any caller when the final metadata write fails).
        assert call_log == [("ep_persist_fail_late", "A Topic", "hypogeum")]


# ===========================================================================
# TestNoSchedulerBehavior
# ===========================================================================

class TestNoSchedulerBehavior:
    def test_scheduler_and_queue_are_never_touched(self, db_path, project_root, monkeypatch):
        import hypogeum.scheduler as scheduler_module
        import hypogeum.queue as queue_module

        def _fail(*a, **k):
            raise AssertionError("scheduler/queue must not be called by run_gated_episode")

        monkeypatch.setattr(scheduler_module, "tick", _fail)
        monkeypatch.setattr(scheduler_module, "create_schedule", _fail)
        monkeypatch.setattr(queue_module, "claim_next", _fail)
        monkeypatch.setattr(queue_module, "process_next", _fail)
        monkeypatch.setattr(queue_module, "poll", _fail)

        _make_operations_profile(db_path)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        result = run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_no_scheduler", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic", project_root=project_root,
        )
        assert result.success is True


# ===========================================================================
# TestProfileAndPlanUntouched
# ===========================================================================

class TestProfileAndPlanUntouched:
    def test_operations_profile_is_never_mutated(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        before = get_channel_operations_profile("hypogeum", db_path=db_path)

        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        run_worker.run_gated_episode(
            channel_id="hypogeum", episode_id="ep_profile_untouched", invocation_mode="manual",
            config=object(), db_path=db_path, topic_input="A Topic",
            target_publish_at=_TARGET, generated_at=_GENERATED, project_root=project_root,
        )

        after = get_channel_operations_profile("hypogeum", db_path=db_path)
        assert before == after
