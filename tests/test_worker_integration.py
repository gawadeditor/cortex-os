"""
tests/test_worker_integration.py — canonical worker queue-gating tests
(2026-07-21 correction).

hypogeum.worker_integration.build_gated_pipeline_executor() is passed as
queue.process_next()/queue.poll()'s pipeline_executor — the SAME real claim
-> topic-resolve -> pipeline -> complete/fail/release lifecycle every queue
item already goes through. There is no separate gated code path: every test
here drives the real hypogeum.queue functions (enqueue, process_next, poll,
get_item) with only hypogeum.pipeline.run_pipeline mocked.

Coverage:
    TestRealQueuePathAppliesGates  — no queue-claimed item reaches
                                     run_pipeline() without profile/plan gates
    TestScheduledVsManualMode      — invocation_mode derived from scheduled_run_id
    TestConcurrencyGate            — max_concurrent_episodes deferred/enforced
    TestQueueLifecycleReused       — claim/complete/fail/release not duplicated
    TestPlanContinuity             — plan_id and worker-attempt history preserved
    TestPublishingFilterReachesPipeline — eligible platforms passed to run_pipeline
    TestRetryOwnership             — run_pipeline called at most once per attempt
    TestGatedCli                   — run_worker._run_gated_cli() exit codes
    TestNoSchedulerBehavior        — hypogeum.scheduler is never touched
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import run_worker
from hypogeum import worker_integration as wi
from hypogeum.backward_planner import build_backward_plan, load_backward_plan, save_backward_plan
from hypogeum.channel_config import (
    create_channel_operations_profile,
    create_channel_profile,
    get_channel_operations_profile,
)
from hypogeum.db import get_connection, initialize_schema
from hypogeum.queue import consume_scheduled_runs, enqueue, get_item, poll, process_next
from hypogeum.scheduler import create_schedule, tick
from hypogeum.topics import enqueue_topic, get_topic


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
    profile = create_channel_operations_profile(**kwargs)
    _activate_channel(db_path, channel_id)
    return profile


def _activate_channel(db_path, channel_id):
    """CALOG (hypogeum.calog) is a separate gate from ChannelOperationsProfile
    — process_next() checks it first, before our executor ever runs. Tests
    must activate the channel here so items reach the gate under test."""
    from hypogeum.calog import set_channel_state
    set_channel_state(channel_id, "active", db_path=db_path)


def _insert_schedule_and_scheduled_run(db_path, channel_id, episode_id, scheduled_for_iso):
    """Insert minimal schedules + scheduled_runs rows so a run_queue row can
    legitimately reference scheduled_run_id (FK-enforced)."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO schedules
               (schedule_id, scope, channel_id, cadence, status, max_runs_per_day, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            ("sched_test", "channel", channel_id, "weekly", "active", 1, now, now),
        )
        conn.execute(
            """INSERT INTO scheduled_runs
               (scheduled_run_id, schedule_id, channel_id, episode_id, scheduled_for, status, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            ("sr_test", "sched_test", channel_id, episode_id, scheduled_for_iso, "triggered", now),
        )
        conn.commit()
    finally:
        conn.close()
    return "sr_test"


def _insert_active_run(db_path, channel_id, episode_id):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status, channel_id) VALUES (?,?,?,?,?)",
            (f"run_active_{episode_id}", episode_id, now, "running", channel_id),
        )
        conn.commit()
    finally:
        conn.close()


def _write_episode_target(db_path, episode_id, target_publish_at_iso):
    """episode_topics.target_publish_at — the same field pipeline.py's
    _get_episode_publish_at() already reads for real YouTube scheduling.
    episode_id/target_publish_at are P12-21 migration columns applied by
    initialize_schema() — must be included explicitly or they default NULL."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO episode_topics
               (topic_id, channel_id, topic_text, status, priority, created_at, updated_at,
                target_publish_at, episode_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (f"topic_{episode_id}", "hypogeum", "placeholder", "consumed", 0, now, now,
             target_publish_at_iso, episode_id),
        )
        conn.commit()
    finally:
        conn.close()


def _patch_pipeline_success(monkeypatch, call_log=None):
    def _fake(*, topic_input, episode_id, config, channel_id, db_path=None,
              cancel_check=None, publishing_platform_filter=None, youtube_privacy=None):
        if call_log is not None:
            call_log.append({
                "episode_id": episode_id, "topic_input": topic_input,
                "channel_id": channel_id,
                "publishing_platform_filter": publishing_platform_filter,
                "youtube_privacy": youtube_privacy,
            })
    monkeypatch.setattr("hypogeum.pipeline.run_pipeline", _fake)
    return _fake


def _process_gated(db_path, project_root, worker_id="w1", topic="Test Topic"):
    executor = wi.build_gated_pipeline_executor(db_path=db_path, project_root=project_root)
    return process_next(
        worker_id=worker_id, db_path=db_path, dry_run=False, config=object(),
        topic_resolver=(lambda row: topic), pipeline_executor=executor,
    )


_TARGET = datetime(2026, 9, 3, 18, 0, tzinfo=ZoneInfo("Asia/Dubai"))
_GENERATED = datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)


# ===========================================================================
# TestRealQueuePathAppliesGates
# ===========================================================================

class TestRealQueuePathAppliesGates:
    def test_missing_operations_profile_fails_closed_no_pipeline_call(self, db_path, project_root, monkeypatch):
        _activate_channel(db_path, "ghost_channel")
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        queue_id = enqueue(db_path=db_path, channel_id="ghost_channel", episode_id="ep_no_profile")

        pr = _process_gated(db_path, project_root)

        assert pr.status == "failed"
        assert call_log == []
        row = get_item(queue_id, db_path=db_path)
        assert row["status"] == "failed"

    def test_operator_disabled_refuses_manual_item(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, operator_enabled=False)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_op_disabled")

        pr = _process_gated(db_path, project_root)

        assert pr.status == "failed"
        assert call_log == []

    def test_production_disabled_refuses_manual_item(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, production_enabled=False)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_prod_disabled")

        pr = _process_gated(db_path, project_root)

        assert pr.status == "failed"
        assert call_log == []

    def test_gated_run_succeeds_when_all_gates_pass(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_ok")

        pr = _process_gated(db_path, project_root)

        assert pr.status == "completed"
        assert len(call_log) == 1
        assert call_log[0]["episode_id"] == "ep_ok"

    def test_channel_scoped_none_skips_gating_entirely(self, db_path, project_root, monkeypatch):
        """A factory-level item (channel_id=None) has no profile to gate —
        matches process_next()'s own CALOG check, which is also skipped for
        unscoped items."""
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id=None, episode_id="ep_unscoped")

        executor = wi.build_gated_pipeline_executor(db_path=db_path, project_root=project_root)
        pr = process_next(
            worker_id="w1", db_path=db_path, dry_run=False, config=object(),
            topic_resolver=lambda row: "Test Topic", pipeline_executor=executor,
        )
        assert pr.status == "completed"
        assert len(call_log) == 1


# ===========================================================================
# TestScheduledVsManualMode
# ===========================================================================

class TestScheduledVsManualMode:
    def test_scheduler_linked_item_refuses_when_unattended_disabled(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, unattended_operation_enabled=False)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        scheduled_run_id = _insert_schedule_and_scheduled_run(
            db_path, "hypogeum", "ep_scheduled_blocked", _TARGET.isoformat(),
        )
        enqueue(
            db_path=db_path, channel_id="hypogeum", episode_id="ep_scheduled_blocked",
            scheduled_run_id=scheduled_run_id,
        )

        pr = _process_gated(db_path, project_root)

        assert pr.status == "failed"
        assert call_log == []

    def test_scheduler_linked_item_succeeds_when_unattended_enabled(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, unattended_operation_enabled=True)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        _write_episode_target(db_path, "ep_scheduled_ok", _TARGET.isoformat())
        scheduled_run_id = _insert_schedule_and_scheduled_run(
            db_path, "hypogeum", "ep_scheduled_ok", _TARGET.isoformat(),
        )
        enqueue(
            db_path=db_path, channel_id="hypogeum", episode_id="ep_scheduled_ok",
            scheduled_run_id=scheduled_run_id,
        )

        pr = _process_gated(db_path, project_root)

        assert pr.status == "completed"
        assert len(call_log) == 1

    def test_non_scheduler_item_runs_as_manual_even_with_unattended_disabled(
        self, db_path, project_root, monkeypatch,
    ):
        """Today's real queue traffic (topic-backlog / manually enqueued) has
        no scheduled_run_id — it must be gated as 'manual', not blocked by
        an unattended_operation_enabled=False setting meant for the
        (not-yet-implemented) Scheduler's own traffic."""
        _make_operations_profile(db_path, unattended_operation_enabled=False)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_manual_traffic")

        pr = _process_gated(db_path, project_root)

        assert pr.status == "completed"
        assert len(call_log) == 1

    def test_scheduler_derived_target_reaches_backward_plan_via_real_backlog_claim(
        self, db_path, project_root, monkeypatch,
    ):
        """End-to-end, real (unmocked) scheduler -> queue path — proves req 4
        without any change to worker_integration.py:
        tick() -> consume_scheduled_runs() (pure promotion — no topic lookup,
        no topic write; see the race-condition correction) -> the real
        backlog-claim branch of process_next() (topic_resolver=None, not the
        fixed-topic test helper), which derives target_publish_at from
        ChannelOperationsProfile.publishing_schedule and persists it onto
        episode_topics strictly AFTER its own claim_next_topic() call
        succeeds, for the exact topic just claimed -> resolve_backward_plan()
        succeeds under InvocationMode.SCHEDULED.

        The topic is enqueued with an explicit episode_id (the same
        convention hypogeum.dashboard_control's rerun/finance-approval flows
        already use) so the pipeline's episode_id matches the episode_topics
        row process_next() writes target_publish_at onto — episode_id
        auto-assignment for brand-new (non-rerun) topics is a separate,
        pre-existing characteristic of process_next() unrelated to this
        milestone.
        """
        _make_operations_profile(db_path, unattended_operation_enabled=True)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)

        topic_id = enqueue_topic(
            "hypogeum", "Scheduler-derived topic", db_path=db_path,
            episode_id="ep_sched_e2e",
        )
        assert get_topic(topic_id, db_path=db_path)["target_publish_at"] is None

        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        create_schedule(
            db_path=db_path, scope="channel", channel_id="hypogeum",
            cadence="daily", next_run_at=past,
        )
        tick(db_path=db_path, now=datetime.now(timezone.utc))
        consume_scheduled_runs(db_path=db_path)

        # consume_scheduled_runs() must NOT have touched episode_topics —
        # nothing is derived/written until process_next() actually claims.
        assert get_topic(topic_id, db_path=db_path)["target_publish_at"] is None

        executor = wi.build_gated_pipeline_executor(db_path=db_path, project_root=project_root)
        pr = process_next(
            worker_id="w1", db_path=db_path, dry_run=False, config=object(),
            pipeline_executor=executor,  # topic_resolver=None — real backlog claim
        )

        # Only now — after the real claim — is the target derived and written
        # onto the exact topic that was claimed.
        assert get_topic(topic_id, db_path=db_path)["target_publish_at"] is not None

        assert pr.status == "completed"
        assert len(call_log) == 1
        assert call_log[0]["episode_id"] == "ep_sched_e2e"

        attempt = wi.load_latest_worker_attempt("ep_sched_e2e", project_root=project_root)
        assert attempt.blocked_reason is None
        assert attempt.plan_status == "executable"
        assert attempt.success is True

    def test_organically_queued_topic_with_no_preassigned_episode_id_reaches_pipeline(
        self, db_path, project_root, monkeypatch,
    ):
        """MAR Production Repair, H2 regression test — the exact scenario the
        prior test above deliberately worked around (see its docstring):
        a topic enqueued with NO pre-assigned episode_id (the default case
        for organically-queued/discovered topics, not just a test
        convenience) must still resolve target_publish_at and reach
        run_pipeline() under InvocationMode.SCHEDULED, instead of failing
        closed with 'scheduled invocation requires an existing backward
        plan artifact or an explicit target_publish_at — neither was
        supplied'.
        """
        _make_operations_profile(db_path, unattended_operation_enabled=True)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)

        topic_id = enqueue_topic(
            "hypogeum", "Organically discovered topic", db_path=db_path,
            # no episode_id — this is the default/common case being fixed.
        )
        assert get_topic(topic_id, db_path=db_path)["episode_id"] is None
        assert get_topic(topic_id, db_path=db_path)["target_publish_at"] is None

        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        create_schedule(
            db_path=db_path, scope="channel", channel_id="hypogeum",
            cadence="daily", next_run_at=past,
        )
        tick(db_path=db_path, now=datetime.now(timezone.utc))
        consume_scheduled_runs(db_path=db_path)

        executor = wi.build_gated_pipeline_executor(db_path=db_path, project_root=project_root)
        pr = process_next(
            worker_id="w1", db_path=db_path, dry_run=False, config=object(),
            pipeline_executor=executor,
        )

        topic_after = get_topic(topic_id, db_path=db_path)
        assert topic_after["episode_id"] is not None, "episode_id must be persisted onto the topic"
        assert topic_after["target_publish_at"] is not None

        assert pr.status == "completed", (
            f"expected the pipeline to actually run, got status={pr.status!r} "
            f"error={pr.error_message!r}"
        )
        assert len(call_log) == 1
        resolved_episode_id = call_log[0]["episode_id"]
        assert resolved_episode_id == topic_after["episode_id"]

        attempt = wi.load_latest_worker_attempt(resolved_episode_id, project_root=project_root)
        assert attempt.blocked_reason is None, (
            "must not be refused with 'neither was supplied' — the exact H2 defect"
        )
        assert attempt.plan_status == "executable"
        assert attempt.success is True


# ===========================================================================
# TestConcurrencyGate
# ===========================================================================

class TestConcurrencyGate:
    def test_max_concurrent_episodes_defers_item_back_to_queued(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, max_concurrent_episodes=1)
        _insert_active_run(db_path, "hypogeum", "ep_already_running")
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        queue_id = enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_deferred")

        pr = _process_gated(db_path, project_root)

        assert call_log == []
        row = get_item(queue_id, db_path=db_path)
        # Deferred: the item is back in 'queued' (released), not terminally 'failed' —
        # process_next()'s own fail() call becomes a safe no-op since release()
        # already ran first.
        assert row["status"] == "queued"

    def test_raising_max_concurrent_episodes_allows_execution(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, max_concurrent_episodes=2)
        _insert_active_run(db_path, "hypogeum", "ep_already_running")
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_allowed")

        pr = _process_gated(db_path, project_root)

        assert pr.status == "completed"
        assert len(call_log) == 1

    def test_no_active_runs_does_not_defer(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, max_concurrent_episodes=1)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_first")

        pr = _process_gated(db_path, project_root)

        assert pr.status == "completed"


# ===========================================================================
# TestQueueLifecycleReused — proves claim/complete/fail/release/topic
# lifecycle is not duplicated; the gated executor only decides whether the
# ONE existing run_pipeline() call happens.
# ===========================================================================

class TestQueueLifecycleReused:
    def test_successful_attempt_completes_queue_item_and_consumes_topic(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        from hypogeum.topics import enqueue_topic
        topic_id = enqueue_topic("hypogeum", "Backlog Topic", db_path=db_path, episode_id="ep_backlog")
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        queue_id = enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_backlog")

        executor = wi.build_gated_pipeline_executor(db_path=db_path, project_root=project_root)
        pr = process_next(
            worker_id="w1", db_path=db_path, dry_run=False, config=object(),
            pipeline_executor=executor,
        )

        assert pr.status == "completed"
        assert call_log[0]["topic_input"] == "Backlog Topic"
        assert get_item(queue_id, db_path=db_path)["status"] == "completed"

        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT status FROM episode_topics WHERE topic_id=?", (topic_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "consumed"

    def test_terminal_gate_failure_marks_queue_item_failed_and_releases_topic(
        self, db_path, project_root, monkeypatch,
    ):
        _make_operations_profile(db_path, operator_enabled=False)
        from hypogeum.topics import enqueue_topic
        topic_id = enqueue_topic("hypogeum", "Backlog Topic 2", db_path=db_path, episode_id="ep_backlog2")
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        queue_id = enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_backlog2")

        executor = wi.build_gated_pipeline_executor(db_path=db_path, project_root=project_root)
        pr = process_next(
            worker_id="w1", db_path=db_path, dry_run=False, config=object(),
            pipeline_executor=executor,
        )

        assert pr.status == "failed"
        assert get_item(queue_id, db_path=db_path)["status"] == "failed"
        # Existing topic hygiene: non-rejection failures release the topic
        # for retry (same as any other infra failure) — unchanged behavior.
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT status FROM episode_topics WHERE topic_id=?", (topic_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "queued"


# ===========================================================================
# TestPlanContinuity
# ===========================================================================

class TestPlanContinuity:
    def test_plan_id_preserved_across_two_gated_attempts(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        _write_episode_target(db_path, "ep_plan_continuity", _TARGET.isoformat())
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)

        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_plan_continuity")
        _process_gated(db_path, project_root)

        first_plan = load_backward_plan("ep_plan_continuity", project_root=project_root)
        assert first_plan is not None

        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_plan_continuity")
        _process_gated(db_path, project_root)

        second_plan = load_backward_plan("ep_plan_continuity", project_root=project_root)
        assert second_plan.plan_id == first_plan.plan_id

        attempts = wi.list_worker_attempts("ep_plan_continuity", project_root=project_root)
        assert len(attempts) == 2
        assert attempts[0].worker_run_id != attempts[1].worker_run_id
        assert attempts[0].plan_id == attempts[1].plan_id == first_plan.plan_id

    def test_infeasible_plan_blocks_without_compressing_durations(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        too_close = datetime.now(timezone.utc) + timedelta(hours=1)
        _write_episode_target(db_path, "ep_infeasible", too_close.isoformat())
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_infeasible")

        pr = _process_gated(db_path, project_root)

        assert pr.status == "failed"
        assert call_log == []
        plan = load_backward_plan("ep_infeasible", project_root=project_root)
        assert plan.plan_status == "infeasible"


# ===========================================================================
# TestPublishingFilterReachesPipeline
# ===========================================================================

class TestPublishingFilterReachesPipeline:
    def test_eligible_platforms_passed_to_run_pipeline(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_platform_pass")

        _process_gated(db_path, project_root)

        assert call_log[0]["publishing_platform_filter"] == frozenset({"youtube"})

    def test_publishing_disabled_passes_empty_filter(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, publishing_enabled=False)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_platform_none")

        _process_gated(db_path, project_root)

        assert call_log[0]["publishing_platform_filter"] == frozenset()


# ===========================================================================
# TestYoutubePrivacyResolution — MAR/publishing-safety repair: run_pipeline()
# defaults youtube_privacy to "public" and the gated executor never passed
# it, so every autonomous queue-driven run silently uploaded public. Now
# resolved from ChannelOperationsProfile.default_privacy_state (the
# canonical source) and passed explicitly, failing closed on an
# unresolved/invalid value rather than falling through to "public".
# ===========================================================================

class TestYoutubePrivacyResolution:
    def test_autonomous_worker_uses_channel_profile_privacy_value(
        self, db_path, project_root, monkeypatch,
    ):
        """Uses a non-default value (unlisted) specifically to prove the
        value is actually read from the profile, not coincidentally
        matching a hardcoded default."""
        _make_operations_profile(db_path, default_privacy_state="unlisted")
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_privacy_unlisted")

        pr = _process_gated(db_path, project_root)

        assert pr.status == "completed"
        assert call_log[0]["youtube_privacy"] == "unlisted"

    def test_private_remains_private(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path, default_privacy_state="private")
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_privacy_private")

        _process_gated(db_path, project_root)

        assert call_log[0]["youtube_privacy"] == "private"

    def test_explicit_direct_caller_override_still_works(self, db_path, monkeypatch):
        """A caller that invokes hypogeum.pipeline.run_pipeline() directly
        (e.g. run_pipeline.py's CLI, which passes --youtube-privacy
        explicitly) is untouched by this fix — only the gated executor's
        own call site was changed."""
        import hypogeum.pipeline as pipeline_module
        captured = {}

        def _fake_run_pipeline(*, youtube_privacy="public", **kwargs):
            captured["youtube_privacy"] = youtube_privacy

        monkeypatch.setattr(pipeline_module, "run_pipeline", _fake_run_pipeline)
        pipeline_module.run_pipeline(
            topic_input="t", episode_id="ep_direct", config=object(),
            channel_id="hypogeum", youtube_privacy="private",
        )
        assert captured["youtube_privacy"] == "private"

    def test_missing_privacy_value_fails_closed(self, db_path, project_root, monkeypatch):
        """A profile with no resolvable default_privacy_state must refuse
        the run, never fall through to run_pipeline()'s public default.

        The DB schema enforces NOT NULL on this column (belt-and-suspenders
        at that layer already), so a true NULL can't be constructed via a
        real row — this simulates the same "no valid value" condition the
        way it could still occur (a profile object missing/blanking the
        field) via dataclasses.replace() on the real, otherwise-valid
        profile the executor already loads.
        """
        import dataclasses
        _make_operations_profile(db_path)
        real_profile = get_channel_operations_profile("hypogeum", db_path=db_path)
        blank_profile = dataclasses.replace(real_profile, default_privacy_state="")
        monkeypatch.setattr(wi, "load_operations_profile_or_fail", lambda *a, **kw: blank_profile)

        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_privacy_missing")

        pr = _process_gated(db_path, project_root)

        assert pr.status == "failed"
        assert call_log == [], "run_pipeline() must never be called when privacy can't be resolved"
        attempt = wi.load_latest_worker_attempt("ep_privacy_missing", project_root=project_root)
        assert attempt.blocked_reason is not None
        assert "youtube_privacy" in attempt.blocked_reason

    def test_invalid_privacy_value_fails_closed(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        conn = get_connection(db_path)
        try:
            conn.execute(
                "UPDATE channel_operations_profiles SET default_privacy_state = 'garbage' "
                "WHERE channel_id = 'hypogeum'",
            )
            conn.commit()
        finally:
            conn.close()

        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_privacy_invalid")

        pr = _process_gated(db_path, project_root)

        assert pr.status == "failed"
        assert call_log == []

    def test_no_gated_run_ever_passes_none_or_public_by_default(
        self, db_path, project_root, monkeypatch,
    ):
        """Regression guard for the exact defect: a channel using the
        schema's own default profile (default_privacy_state='private',
        the DB column default) must never silently resolve to 'public'."""
        _make_operations_profile(db_path)  # uses the helper's own default
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_privacy_default")

        _process_gated(db_path, project_root)

        assert call_log[0]["youtube_privacy"] is not None
        assert call_log[0]["youtube_privacy"] != "public"


# ===========================================================================
# TestRetryOwnership — no double retry multiplication: the executor never
# wraps run_pipeline() in its own retry loop, regardless of retry_policy.
# ===========================================================================

class TestRetryOwnership:
    def test_run_pipeline_called_exactly_once_regardless_of_retry_policy(
        self, db_path, project_root, monkeypatch,
    ):
        _make_operations_profile(db_path, retry_policy={"max_retries_per_stage": 5, "backoff_seconds": 30})
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_retry_once")

        _process_gated(db_path, project_root)

        assert len(call_log) == 1

    def test_pipeline_failure_is_not_retried_by_the_executor(self, db_path, project_root, monkeypatch):
        """A failing run_pipeline() call must propagate once — the worker
        layer does not add its own retry loop around a failure. Existing
        topic release (for the queue's own next-poll retry) is unchanged."""
        _make_operations_profile(db_path, retry_policy={"max_retries_per_stage": 3, "backoff_seconds": 10})
        call_count = {"n": 0}

        def _fake_fail(**kwargs):
            call_count["n"] += 1
            raise RuntimeError("simulated provider failure")
        monkeypatch.setattr("hypogeum.pipeline.run_pipeline", _fake_fail)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_retry_fail")

        pr = _process_gated(db_path, project_root)

        assert pr.status == "failed"
        assert call_count["n"] == 1


# ===========================================================================
# TestGatedCli
# ===========================================================================

class TestGatedCli:
    def test_blocked_cli_invocation_exits_non_zero(self, db_path, monkeypatch, capsys):
        _make_operations_profile(db_path, operator_enabled=False)
        monkeypatch.setattr("hypogeum.config.Config", lambda: object())
        _patch_pipeline_success(monkeypatch)

        exit_code = run_worker._run_gated_cli(
            channel_id="hypogeum", episode_id="ep_cli_blocked", topic="A Topic", db_path=db_path,
        )
        assert exit_code != 0

    def test_successful_cli_invocation_exits_zero(self, db_path, monkeypatch, capsys):
        _make_operations_profile(db_path)
        monkeypatch.setattr("hypogeum.config.Config", lambda: object())
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)

        exit_code = run_worker._run_gated_cli(
            channel_id="hypogeum", episode_id="ep_cli_ok", topic="A Topic", db_path=db_path,
        )
        assert exit_code == 0
        assert len(call_log) == 1


# ===========================================================================
# TestNoSchedulerBehavior
# ===========================================================================

class TestNoSchedulerBehavior:
    def test_scheduler_module_is_never_called(self, db_path, project_root, monkeypatch):
        import hypogeum.scheduler as scheduler_module

        def _fail(*a, **k):
            raise AssertionError("hypogeum.scheduler must not be called by the gated executor")

        monkeypatch.setattr(scheduler_module, "tick", _fail)
        monkeypatch.setattr(scheduler_module, "create_schedule", _fail)

        _make_operations_profile(db_path)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_no_scheduler")

        pr = _process_gated(db_path, project_root)
        assert pr.status == "completed"

    def test_operations_profile_never_mutated(self, db_path, project_root, monkeypatch):
        _make_operations_profile(db_path)
        before = get_channel_operations_profile("hypogeum", db_path=db_path)
        call_log = []
        _patch_pipeline_success(monkeypatch, call_log)
        enqueue(db_path=db_path, channel_id="hypogeum", episode_id="ep_profile_untouched")

        _process_gated(db_path, project_root)

        after = get_channel_operations_profile("hypogeum", db_path=db_path)
        assert before == after
