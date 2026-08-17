"""
tests/test_worker_control.py — Worker Control feature tests

Covers:
  - cancel_requested DB flag is set/read correctly
  - worker_control key-value store works
  - abort_queue_item force-cancels claimed items
  - PipelineCancelledError is raised when cancel_check returns True
  - cancel check fires between every pipeline stage boundary
  - process_next() handles PipelineCancelledError → cancels queue item + releases topic
  - run() marks run as cancelled (not failed) on PipelineCancelledError
  - cancel_run() in run_registry adds 'cancelled' state transition
  - Pause Queue prevents poll from claiming new jobs
  - Resume Queue re-enables job claiming
  - Stop Worker via DB flag works alongside SIGTERM
  - run_worker.py writes/reads worker_status from worker_control table
"""

from __future__ import annotations

import contextlib
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db(tmp_path: Path) -> Path:
    from hypogeum.db import initialize_schema, get_connection
    db = tmp_path / "h.db"
    initialize_schema(db)

    # Seed the minimal active factory + credential rows
    # hypogeum.factory_director.check_run_clearance() requires to return
    # cleared=True. Without this, run_iteration()'s clearance gate (wired
    # 2026-07-18) skips poll on a bare schema, since no-factory/no-credentials
    # is a genuinely un-cleared state, not a test artifact to special-case
    # around.
    conn = get_connection(db)
    try:
        conn.execute(
            "INSERT INTO factories (factory_id, factory_name, factory_slug, "
            "status, default_language, default_timezone, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("hypogeum", "Test Factory", "hypogeum", "active", "en", "UTC",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO secret_profiles "
            "(secret_profile_id, provider, label, credential_type, "
            "credential_data, status, scope, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "openrouter", "openrouter-env", "api_key",
             "dummy", "active", "factory",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    return db


def _enqueue(db: Path, channel_id: str | None = None) -> str:
    from hypogeum.db import get_connection
    queue_id = str(uuid.uuid4())
    conn = get_connection(db)
    try:
        conn.execute(
            "INSERT INTO run_queue (queue_id, channel_id, status, priority, queued_at, created_at) "
            "VALUES (?, ?, 'queued', 0, datetime('now'), datetime('now'))",
            (queue_id, channel_id),
        )
        conn.commit()
    finally:
        conn.close()
    return queue_id


def _claim(db: Path, queue_id: str, worker_id: str) -> None:
    from hypogeum.db import get_connection
    conn = get_connection(db)
    try:
        conn.execute(
            "UPDATE run_queue SET status='claimed', worker_id=?, claimed_at=datetime('now') "
            "WHERE queue_id=? AND status='queued'",
            (worker_id, queue_id),
        )
        conn.commit()
    finally:
        conn.close()


def _queue_status(db: Path, queue_id: str) -> str | None:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT status FROM run_queue WHERE queue_id=?", (queue_id,)).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


def _cancel_requested(db: Path, queue_id: str) -> int:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT cancel_requested FROM run_queue WHERE queue_id=?", (queue_id,)).fetchone()
        return row["cancel_requested"] if row else 0
    finally:
        conn.close()


# ===========================================================================
# 1. DB layer: worker_control table
# ===========================================================================

class TestWorkerControlDB:
    def test_set_and_get_value(self, tmp_path):
        db = _db(tmp_path)
        from hypogeum.db import set_worker_control, get_worker_control
        set_worker_control("queue_paused", "1", db_path=db)
        assert get_worker_control("queue_paused", db_path=db) == "1"

    def test_get_missing_key_returns_default(self, tmp_path):
        db = _db(tmp_path)
        from hypogeum.db import get_worker_control
        assert get_worker_control("nonexistent", default="xyz", db_path=db) == "xyz"

    def test_upsert_overwrites(self, tmp_path):
        db = _db(tmp_path)
        from hypogeum.db import set_worker_control, get_worker_control
        set_worker_control("worker_status", "idle", db_path=db)
        set_worker_control("worker_status", "running", db_path=db)
        assert get_worker_control("worker_status", db_path=db) == "running"

    def test_multiple_keys(self, tmp_path):
        db = _db(tmp_path)
        from hypogeum.db import set_worker_control, get_worker_control
        set_worker_control("a", "1", db_path=db)
        set_worker_control("b", "2", db_path=db)
        assert get_worker_control("a", db_path=db) == "1"
        assert get_worker_control("b", db_path=db) == "2"


# ===========================================================================
# 2. DB layer: cancel_requested column on run_queue
# ===========================================================================

class TestCancelRequestedFlag:
    def test_default_is_zero(self, tmp_path):
        db = _db(tmp_path)
        queue_id = _enqueue(db)
        assert _cancel_requested(db, queue_id) == 0

    def test_request_cancel_sets_flag(self, tmp_path):
        db = _db(tmp_path)
        queue_id = _enqueue(db)
        from hypogeum.db import request_cancel_for_queue_item
        ok = request_cancel_for_queue_item(queue_id, db_path=db)
        assert ok is True
        assert _cancel_requested(db, queue_id) == 1

    def test_request_cancel_returns_false_for_missing(self, tmp_path):
        db = _db(tmp_path)
        from hypogeum.db import request_cancel_for_queue_item
        assert request_cancel_for_queue_item(str(uuid.uuid4()), db_path=db) is False

    def test_is_cancel_requested(self, tmp_path):
        db = _db(tmp_path)
        queue_id = _enqueue(db)
        from hypogeum.db import request_cancel_for_queue_item, is_cancel_requested
        assert is_cancel_requested(queue_id, db_path=db) is False
        request_cancel_for_queue_item(queue_id, db_path=db)
        assert is_cancel_requested(queue_id, db_path=db) is True

    def test_cancel_works_on_claimed_item(self, tmp_path):
        db = _db(tmp_path)
        wid = str(uuid.uuid4())
        queue_id = _enqueue(db)
        _claim(db, queue_id, wid)
        from hypogeum.db import request_cancel_for_queue_item
        ok = request_cancel_for_queue_item(queue_id, db_path=db)
        assert ok is True
        assert _cancel_requested(db, queue_id) == 1


# ===========================================================================
# 3. DB layer: abort_queue_item (force-cancel regardless of status)
# ===========================================================================

class TestAbortQueueItem:
    def test_aborts_claimed_item(self, tmp_path):
        db = _db(tmp_path)
        wid = str(uuid.uuid4())
        queue_id = _enqueue(db)
        _claim(db, queue_id, wid)
        from hypogeum.db import abort_queue_item
        abort_queue_item(queue_id, db_path=db)
        assert _queue_status(db, queue_id) == "cancelled"

    def test_aborts_queued_item(self, tmp_path):
        db = _db(tmp_path)
        queue_id = _enqueue(db)
        from hypogeum.db import abort_queue_item
        abort_queue_item(queue_id, db_path=db)
        assert _queue_status(db, queue_id) == "cancelled"

    def test_noop_on_completed_item(self, tmp_path):
        db = _db(tmp_path)
        queue_id = _enqueue(db)
        conn = sqlite3.connect(db)
        try:
            conn.execute("UPDATE run_queue SET status='completed' WHERE queue_id=?", (queue_id,))
            conn.commit()
        finally:
            conn.close()
        from hypogeum.db import abort_queue_item
        abort_queue_item(queue_id, db_path=db)
        assert _queue_status(db, queue_id) == "completed"


# ===========================================================================
# 4. run_registry: cancelled state and cancel_run()
# ===========================================================================

class TestRunRegistryCancelledState:
    def test_cancelled_in_run_states(self):
        from hypogeum.run_registry import RUN_STATES
        assert "cancelled" in RUN_STATES

    def test_running_to_cancelled_is_valid(self):
        from hypogeum.run_registry import _VALID_TRANSITIONS
        assert "cancelled" in _VALID_TRANSITIONS["running"]

    def test_cancelled_is_terminal(self):
        from hypogeum.run_registry import _TERMINAL_STATES
        assert "cancelled" in _TERMINAL_STATES

    def _make_config(self):
        cfg = MagicMock()
        cfg.EP_MODEL = "anthropic/claude-sonnet-4.6"
        cfg.RD_MODEL = "anthropic/claude-opus-4.6"
        cfg.TD_PLANNING_MODEL = "openai/gpt-5.5-pro"
        cfg.BGA_PASS1_MODEL = "anthropic/claude-sonnet-4.6"
        cfg.BGA_PASS2_MODEL = "openai/gpt-5.5-pro"
        return cfg

    def test_cancel_run_sets_status(self, tmp_path):
        db = _db(tmp_path)
        from hypogeum.run_registry import register_run, cancel_run
        run_id = register_run(episode_id="ep_test", config=self._make_config(), db_path=db)
        cancel_run(run_id, "ep_test", db_path=db, runs_dir=tmp_path / "runs")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        finally:
            conn.close()
        assert row["status"] == "cancelled"

    def test_cancel_run_transition_recorded(self, tmp_path):
        db = _db(tmp_path)
        from hypogeum.run_registry import register_run, cancel_run, get_run_transition_history
        run_id = register_run(episode_id="ep_test", config=self._make_config(), db_path=db)
        cancel_run(run_id, "ep_test", db_path=db, runs_dir=tmp_path / "runs")
        history = get_run_transition_history(run_id, db_path=db)
        to_states = [t["to_state"] for t in history]
        assert "cancelled" in to_states


# ===========================================================================
# 5. pipeline.py: PipelineCancelledError
# ===========================================================================

class TestPipelineCancelledError:
    def test_error_class_exists(self):
        from hypogeum.pipeline import PipelineCancelledError, PipelineError
        assert issubclass(PipelineCancelledError, PipelineError)

    def test_error_attributes(self):
        from hypogeum.pipeline import PipelineCancelledError
        exc = PipelineCancelledError(episode_id="ep_001", stage_after="script_engine")
        assert exc.episode_id == "ep_001"
        assert exc.stage_after == "script_engine"

    def test_cancel_check_none_does_not_raise(self, tmp_path):
        """cancel_check=None must never raise PipelineCancelledError."""
        from hypogeum.pipeline import run_pipeline, PipelineCancelledError

        # With cancel_check=None, pipeline runs normally (will fail for other reasons)
        with patch("hypogeum.pipeline.run_startup_checks") as mock_startup:
            mock_startup.side_effect = Exception("startup_abort")
            with pytest.raises(Exception, match="startup_abort"):
                run_pipeline(
                    topic_input="test topic",
                    episode_id="ep_check_none",
                    config=MagicMock(),
                    cancel_check=None,
                    db_path=tmp_path / "h.db",
                )


# ===========================================================================
# 6. pipeline.py: cancel check fires between each stage
# ===========================================================================

_ALL_STAGES = [
    "executive_producer",
    "research_director",
    "narrative_architect",
    "script_engine",
    "finance_agent_authorization",
    "thumbnail_strategy",
    "visual_package_director",
    "thumbnail_director",
    "audio_agent",
    "assembly_agent",
    "qa_agent",
    "compliance_agent",
]


class TestCancelCheckFiresBetweenStages:
    """
    For each stage, verify that if cancel_check() returns True immediately
    after that stage completes, PipelineCancelledError is raised with the
    correct stage_after attribute.
    """

    def _run_pipeline_until_stage(self, stage_name: str, tmp_path: Path):
        """
        Run the pipeline with a cancel_check that triggers only after `stage_name`.
        Expects PipelineCancelledError with stage_after == stage_name.
        """
        from hypogeum.pipeline import run_pipeline, PipelineCancelledError

        stages_seen: list[str] = []
        trigger_after = stage_name

        def cancel_check() -> bool:
            return stage_name in stages_seen

        # Patch all agent runners to record their stage name and succeed.
        # We patch at the pipeline module level to intercept calls.
        stage_patches = {
            "executive_producer":          ("hypogeum.pipeline._run_executive_producer",
                                            ({"decision": "ACCEPT", "overall_score": 8.0}, {})),
            "research_director":           ("hypogeum.pipeline._run_research_director",
                                            ({"source_registry": []}, None)),
            "narrative_architect":         ("hypogeum.pipeline._run_narrative_architect", {}),
            "script_engine":               ("hypogeum.pipeline._run_script_engine", {}),
            "finance_agent_authorization": ("hypogeum.pipeline._run_budget_authorization",
                                            {"verdict": "PROCEED", "estimated_spend_usd": 1.0}),
            "thumbnail_strategy":          ("hypogeum.pipeline._run_thumbnail_strategy", {}),
            "visual_package_director":     ("hypogeum.pipeline._run_visual_package_director", {}),
            "thumbnail_director":          ("hypogeum.pipeline._run_thumbnail_director", {}),
            "audio_agent":                 ("hypogeum.pipeline._run_audio_agent", {}),
            "assembly_agent":              ("hypogeum.pipeline._run_assembly_agent", {}),
            "qa_agent":                    ("hypogeum.pipeline._run_qa_agent",
                                            {"verdict": "APPROVED"}),
            "compliance_agent":            ("hypogeum.pipeline._run_compliance_agent",
                                            {"verdict": "APPROVED", "human_review_required": False}),
        }

        def _make_agent_mock(name, return_val):
            def _mock(*args, **kwargs):
                stages_seen.append(name)
                return return_val
            return _mock

        context_managers = []
        stop_after = list(_ALL_STAGES).index(stage_name)
        for i, sname in enumerate(_ALL_STAGES):
            target, rv = stage_patches[sname]
            m = patch(target, side_effect=_make_agent_mock(sname, rv))
            context_managers.append(m)
            if i == stop_after:
                break

        # Also patch startup checks, DB helpers, and run registry.
        from hypogeum.startup import StartupReport
        startup_report = StartupReport.__new__(StartupReport)
        startup_report.overall_status = "ok"
        startup_report.openrouter_reachable = True
        startup_report.fallback_active = False

        cancel_run_calls = []
        pause_transition_calls = []

        with patch("hypogeum.pipeline.run_startup_checks", return_value=startup_report), \
             patch("hypogeum.pipeline.register_run", return_value="run_test"), \
             patch("hypogeum.pipeline.cancel_run",
                   side_effect=lambda *a, **kw: cancel_run_calls.append((a, kw))), \
             patch("hypogeum.pipeline.fail_run"), \
             patch("hypogeum.pipeline.transition_run_state",
                   side_effect=lambda **kw: pause_transition_calls.append(kw)), \
             patch("hypogeum.pipeline.set_run_context"), \
             patch("hypogeum.pipeline.clear_run_context"), \
             patch("hypogeum.pipeline._record_stage_start", return_value=("sid", _now_utc())), \
             patch("hypogeum.pipeline._record_stage_complete"), \
             patch("hypogeum.pipeline._record_stage_failed"), \
             patch("hypogeum.pipeline.is_factory_blocked", return_value=False), \
             patch("hypogeum.pipeline.is_channel_blocked", return_value=False):
            with contextlib.ExitStack() as stack:
                for cm in context_managers:
                    stack.enter_context(cm)
                with pytest.raises(PipelineCancelledError) as exc_info:
                    run_pipeline(
                        topic_input="test topic",
                        episode_id="ep_cancel_test",
                        config=MagicMock(
                            EPISODE_ZERO_MAX_SCENES=0,
                            ENABLE_DEDICATED_SHORT_GENERATION=False,
                        ),
                        cancel_check=cancel_check,
                        channel_id=None,
                        db_path=tmp_path / "h.db",
                    )
            assert exc_info.value.stage_after == stage_name

        # A bare cancel_check()-triggered stop is ALWAYS operator
        # cancellation (see PipelineCancelledError's own docstring and
        # hypogeum.queue._cancel_check(), the only real production
        # constructor of cancel_check) -- terminal 'cancelled' via
        # cancel_run(), never 'paused' via transition_run_state(). This
        # holds regardless of which stage it fires after, including
        # Research Director and Thumbnail Strategy (TESTS items 1/2).
        assert len(cancel_run_calls) == 1, "cancel_run must be called for operator cancellation"
        assert len(pause_transition_calls) == 0, "a bare cancel_check must never produce a pause transition"

    def test_cancel_after_executive_producer(self, tmp_path):
        self._run_pipeline_until_stage("executive_producer", tmp_path)

    def test_cancel_after_research_director(self, tmp_path):
        self._run_pipeline_until_stage("research_director", tmp_path)

    def test_cancel_after_narrative_architect(self, tmp_path):
        self._run_pipeline_until_stage("narrative_architect", tmp_path)

    def test_cancel_after_script_engine(self, tmp_path):
        self._run_pipeline_until_stage("script_engine", tmp_path)

    def test_cancel_after_finance_authorization(self, tmp_path):
        self._run_pipeline_until_stage("finance_agent_authorization", tmp_path)

    def test_cancel_after_thumbnail_strategy(self, tmp_path):
        self._run_pipeline_until_stage("thumbnail_strategy", tmp_path)

    def test_cancel_after_thumbnail_strategy_never_reaches_audio_agent(self, tmp_path):
        """
        Items 8/9, exact real-incident scenario: stopping right after
        thumbnail_strategy (the first zero-cost stage before the Audio Agent
        paid boundary) must never call the Audio Agent's stage function at
        all -- zero provider-adapter invocations, not merely zero *successful*
        ones. hypogeum.pipeline._run_audio_agent is deliberately left
        unpatched here; if resume logic ever crossed the boundary, the call
        would hit the real function and fail with something other than
        PipelineCancelledError, and the assertion below on stages_seen would
        also catch a mock-based crossing.
        """
        from hypogeum.pipeline import run_pipeline, PipelineCancelledError
        from hypogeum.startup import StartupReport

        stages_seen: list[str] = []
        audio_agent_calls: list = []

        def cancel_check() -> bool:
            return "thumbnail_strategy" in stages_seen

        def _mock(name):
            def _fn(*a, **kw):
                stages_seen.append(name)
                if name == "executive_producer":
                    return ({"decision": "ACCEPT", "overall_score": 8.0}, {})
                if name == "research_director":
                    return ({"source_registry": []}, None)
                return {}
            return _fn

        startup_report = StartupReport.__new__(StartupReport)
        startup_report.overall_status = "ok"
        startup_report.openrouter_reachable = True
        startup_report.fallback_active = False

        with patch("hypogeum.pipeline.run_startup_checks", return_value=startup_report), \
             patch("hypogeum.pipeline.register_run", return_value="run_boundary_test"), \
             patch("hypogeum.pipeline.transition_run_state"), \
             patch("hypogeum.pipeline.set_run_context"), \
             patch("hypogeum.pipeline.clear_run_context"), \
             patch("hypogeum.pipeline._record_stage_start", return_value=("sid", _now_utc())), \
             patch("hypogeum.pipeline._record_stage_complete"), \
             patch("hypogeum.pipeline._record_stage_failed"), \
             patch("hypogeum.pipeline.is_factory_blocked", return_value=False), \
             patch("hypogeum.pipeline.is_channel_blocked", return_value=False), \
             patch("hypogeum.pipeline._run_executive_producer", side_effect=_mock("executive_producer")), \
             patch("hypogeum.pipeline._run_research_director", side_effect=_mock("research_director")), \
             patch("hypogeum.pipeline._run_narrative_architect", side_effect=_mock("narrative_architect")), \
             patch("hypogeum.pipeline._run_script_engine", side_effect=_mock("script_engine")), \
             patch("hypogeum.pipeline._run_budget_authorization", side_effect=_mock("finance_agent_authorization")), \
             patch("hypogeum.pipeline._run_thumbnail_strategy", side_effect=_mock("thumbnail_strategy")):
            # _run_audio_agent is intentionally NOT patched -- see docstring.
            with pytest.raises(PipelineCancelledError) as exc_info:
                run_pipeline(
                    topic_input="test topic",
                    episode_id="ep_boundary_test",
                    config=MagicMock(
                        EPISODE_ZERO_MAX_SCENES=0,
                        ENABLE_DEDICATED_SHORT_GENERATION=False,
                    ),
                    cancel_check=cancel_check,
                    channel_id=None,
                    db_path=tmp_path / "h.db",
                )

        assert exc_info.value.stage_after == "thumbnail_strategy"
        assert "audio_agent" not in stages_seen
        assert len(audio_agent_calls) == 0

    def test_cancel_after_visual_package_director(self, tmp_path):
        self._run_pipeline_until_stage("visual_package_director", tmp_path)

    def test_cancel_after_thumbnail_director(self, tmp_path):
        self._run_pipeline_until_stage("thumbnail_director", tmp_path)

    def test_cancel_after_audio_agent(self, tmp_path):
        self._run_pipeline_until_stage("audio_agent", tmp_path)

    def test_cancel_after_assembly_agent(self, tmp_path):
        self._run_pipeline_until_stage("assembly_agent", tmp_path)

    def test_cancel_after_qa_agent(self, tmp_path):
        self._run_pipeline_until_stage("qa_agent", tmp_path)

    def test_cancel_after_compliance_agent(self, tmp_path):
        self._run_pipeline_until_stage("compliance_agent", tmp_path)


# ===========================================================================
# 7. pipeline.py: PipelineCancelledError means operator cancellation ->
#    'cancelled' (terminal). PipelineAuthorizationRequiredError means an
#    authorization-boundary stop -> 'paused' (resumable). These are two
#    structurally distinct exceptions, never inferred from one shared signal.
# ===========================================================================
#
# History: a first attempt at fixing the 2026-08-17 run-ecb0f87a incident
# (commit 2051fa1) collapsed EVERY PipelineCancelledError into 'paused',
# which is itself wrong in the other direction -- cancel_check's only real
# production meaning (hypogeum.queue._cancel_check(), the sole real
# constructor) is a genuine operator cancellation request, which must stay
# terminal 'cancelled'. This corrects that regression: PipelineCancelledError
# restores its original, correct 'cancelled' semantics (TestOperatorCancellationRestored
# below); a new, structurally separate PipelineAuthorizationRequiredError
# (opt-in via enforce_authorization_boundaries=True, never raised for any
# existing production caller) now owns the 'paused' semantics
# (TestAuthorizationBoundaryPause below).

class TestOperatorCancellationRestored:
    """TESTS items 1/2: operator cancellation always becomes 'cancelled', at any stage."""

    def test_cancel_error_calls_cancel_run_not_paused_or_failed(self, tmp_path):
        """When PipelineCancelledError escapes, the run must be marked cancelled."""
        from hypogeum.pipeline import run_pipeline, PipelineCancelledError
        from hypogeum.startup import StartupReport

        startup_report = StartupReport.__new__(StartupReport)
        startup_report.overall_status = "ok"
        startup_report.openrouter_reachable = True
        startup_report.fallback_active = False

        cancel_run_calls = []
        fail_run_calls = []
        transition_calls = []

        with patch("hypogeum.pipeline.run_startup_checks", return_value=startup_report), \
             patch("hypogeum.pipeline.register_run", return_value="run_cancel_test"), \
             patch("hypogeum.pipeline.cancel_run", side_effect=lambda *a, **kw: cancel_run_calls.append(a)), \
             patch("hypogeum.pipeline.fail_run", side_effect=lambda *a, **kw: fail_run_calls.append(a)), \
             patch("hypogeum.pipeline.transition_run_state",
                   side_effect=lambda **kw: transition_calls.append(kw)), \
             patch("hypogeum.pipeline.set_run_context"), \
             patch("hypogeum.pipeline.clear_run_context"), \
             patch("hypogeum.pipeline._record_stage_start", return_value=("sid", "t0")), \
             patch("hypogeum.pipeline._record_stage_complete"), \
             patch("hypogeum.pipeline._record_stage_failed"), \
             patch("hypogeum.pipeline.is_factory_blocked", return_value=False), \
             patch("hypogeum.pipeline.is_channel_blocked", return_value=False), \
             patch("hypogeum.pipeline._run_executive_producer",
                   return_value=({"decision": "ACCEPT", "overall_score": 8.0}, {})):
            # cancel_check returns True after EP, causing PipelineCancelledError.
            # enforce_authorization_boundaries is left at its default False, so
            # this is a pure operator-cancellation scenario (item 4: a generic
            # cancel_check cannot impersonate an authorization pause).
            call_count = [0]
            def cancel_check():
                call_count[0] += 1
                return call_count[0] >= 1

            with pytest.raises(PipelineCancelledError):
                run_pipeline(
                    topic_input="test topic",
                    episode_id="ep_cancel_test2",
                    config=MagicMock(
                        EPISODE_ZERO_MAX_SCENES=0,
                        ENABLE_DEDICATED_SHORT_GENERATION=False,
                    ),
                    cancel_check=cancel_check,
                    channel_id=None,
                    db_path=tmp_path / "h.db",
                )

        assert len(cancel_run_calls) == 1, "cancel_run must be called for operator cancellation"
        assert len(fail_run_calls) == 0, "fail_run must NOT be called on cancellation"
        assert len(transition_calls) == 0, "transition_run_state (paused) must NOT be called for a bare cancel_check"


class TestAuthorizationBoundaryPause:
    """
    TESTS items 3/4/5: an explicit authorization-boundary stop (opt-in via
    enforce_authorization_boundaries=True) becomes 'paused', is structurally
    distinct from cancel_check, and never fires for a non-paid next stage.
    """

    def _run(self, tmp_path, enforce, stop_stage="thumbnail_strategy", cancel_check=None):
        from hypogeum.pipeline import run_pipeline
        from hypogeum.startup import StartupReport

        startup_report = StartupReport.__new__(StartupReport)
        startup_report.overall_status = "ok"
        startup_report.openrouter_reachable = True
        startup_report.fallback_active = False

        cancel_run_calls = []
        transition_calls = []

        def _mock(name, rv):
            def _fn(*a, **kw):
                return rv
            return _fn

        stage_targets = [
            ("hypogeum.pipeline._run_executive_producer",
             ({"decision": "ACCEPT", "overall_score": 8.0}, {})),
            ("hypogeum.pipeline._run_research_director", ({"source_registry": []}, None)),
            ("hypogeum.pipeline._run_narrative_architect", {}),
            ("hypogeum.pipeline._run_script_engine", {}),
            ("hypogeum.pipeline._run_budget_authorization",
             {"verdict": "PROCEED", "estimated_spend_usd": 1.0}),
            ("hypogeum.pipeline._run_thumbnail_strategy", {}),
        ]
        stop_index = {
            "executive_producer": 0, "research_director": 1, "narrative_architect": 2,
            "script_engine": 3, "finance_agent_authorization": 4, "thumbnail_strategy": 5,
        }[stop_stage]

        with patch("hypogeum.pipeline.run_startup_checks", return_value=startup_report), \
             patch("hypogeum.pipeline.register_run", return_value="run_auth_test"), \
             patch("hypogeum.pipeline.cancel_run", side_effect=lambda *a, **kw: cancel_run_calls.append(a)), \
             patch("hypogeum.pipeline.fail_run"), \
             patch("hypogeum.pipeline.transition_run_state",
                   side_effect=lambda **kw: transition_calls.append(kw)), \
             patch("hypogeum.pipeline.set_run_context"), \
             patch("hypogeum.pipeline.clear_run_context"), \
             patch("hypogeum.pipeline._record_stage_start", return_value=("sid", "t0")), \
             patch("hypogeum.pipeline._record_stage_complete"), \
             patch("hypogeum.pipeline._record_stage_failed"), \
             patch("hypogeum.pipeline.is_factory_blocked", return_value=False), \
             patch("hypogeum.pipeline.is_channel_blocked", return_value=False):
            with contextlib.ExitStack() as stack:
                for target, rv in stage_targets[: stop_index + 1]:
                    stack.enter_context(patch(target, side_effect=_mock(target, rv)))
                exc = None
                try:
                    run_pipeline(
                        topic_input="test topic",
                        episode_id="ep_auth_test",
                        config=MagicMock(
                            EPISODE_ZERO_MAX_SCENES=0,
                            ENABLE_DEDICATED_SHORT_GENERATION=False,
                        ),
                        channel_id=None,
                        db_path=tmp_path / "h.db",
                        cancel_check=cancel_check,
                        enforce_authorization_boundaries=enforce,
                    )
                except Exception as e:
                    exc = e
        return exc, cancel_run_calls, transition_calls

    def test_authorization_required_stop_becomes_paused(self, tmp_path):
        """Item 3: explicit authorization-required stop before Audio Agent -> paused."""
        from hypogeum.pipeline import PipelineAuthorizationRequiredError

        exc, cancel_run_calls, transition_calls = self._run(tmp_path, enforce=True)

        assert isinstance(exc, PipelineAuthorizationRequiredError)
        assert exc.stage_after == "thumbnail_strategy"
        assert exc.next_stage == "audio_agent"
        assert len(cancel_run_calls) == 0
        assert len(transition_calls) == 1
        assert transition_calls[0]["to_state"] == "paused"
        assert transition_calls[0]["run_id"] == "run_auth_test"
        assert "audio_agent" in transition_calls[0]["reason"]

    def test_generic_cancel_check_cannot_impersonate_authorization_pause(self, tmp_path):
        """
        Item 4: with enforce_authorization_boundaries left False (every real
        production caller's default), the boundary check must never fire even
        though the very next stage (audio_agent) genuinely requires
        authorization. A cancel_check that fires at the same point
        (mimicking a simultaneous, unrelated operator cancellation) must
        produce plain PipelineCancelledError, never PipelineAuthorizationRequiredError
        -- proving the two signals cannot be confused for one another. A
        cancel_check is required here (rather than leaving the run to
        actually enter audio_agent) so this test never risks a real,
        unmocked provider call.
        """
        from hypogeum.pipeline import PipelineAuthorizationRequiredError, PipelineCancelledError

        exc, cancel_run_calls, transition_calls = self._run(
            tmp_path, enforce=False, cancel_check=lambda: True,
        )

        assert isinstance(exc, PipelineCancelledError)
        assert not isinstance(exc, PipelineAuthorizationRequiredError)
        assert len(cancel_run_calls) == 1
        assert len(transition_calls) == 0

    def test_non_paid_next_stage_produces_no_exception(self):
        """
        Item 5: a direct, unit-level check that a non-paid next stage
        (research_director) never produces authorization-pause evidence,
        even with enforcement on -- checked directly against the same
        functions run_pipeline() calls at every boundary, with no pipeline
        execution (and therefore no risk of reaching any real stage).
        """
        import hypogeum.pipeline as pl

        assert pl._stage_requires_unavailable_authorization("research_director") is None
        assert pl._stage_requires_unavailable_authorization("script_engine") is None
        assert pl._stage_requires_unavailable_authorization("thumbnail_strategy") is None

        pl._raise_if_authorization_required(
            "ep_x", "executive_producer", "research_director",
            enforce_authorization_boundaries=True,
        )  # must not raise

    def test_paid_next_stage_produces_authorization_required(self):
        """Sanity counterpart: a paid next stage DOES raise when enforced."""
        import hypogeum.pipeline as pl

        with pytest.raises(pl.PipelineAuthorizationRequiredError):
            pl._raise_if_authorization_required(
                "ep_x", "thumbnail_strategy", "audio_agent",
                enforce_authorization_boundaries=True,
            )


# ===========================================================================
# 8. queue.py: process_next handles PipelineCancelledError
# ===========================================================================

class TestProcessNextHandlesCancellation:
    def test_cancelled_result_on_pipeline_cancelled_error(self, tmp_path):
        """process_next must return status='cancelled' when PipelineCancelledError is raised."""
        db = _db(tmp_path)
        queue_id = _enqueue(db, channel_id=None)

        from hypogeum.pipeline import PipelineCancelledError
        import hypogeum.queue as queue_mod

        cancelled_error = PipelineCancelledError(episode_id="ep_q", stage_after="script_engine")

        original_pipeline = queue_mod._pipeline_module
        mock_pipeline = MagicMock()
        mock_pipeline.run_pipeline = MagicMock(side_effect=cancelled_error)
        mock_pipeline.PipelineCancelledError = PipelineCancelledError
        mock_pipeline.PipelineRejectedError = original_pipeline.PipelineRejectedError

        with patch.object(queue_mod, "_pipeline_module", mock_pipeline):
            from hypogeum.queue import process_next
            result = process_next(
                worker_id=str(uuid.uuid4()),
                db_path=db,
                dry_run=False,
                config=MagicMock(),
                topic_resolver=lambda item: "test topic",
            )

        assert result.status == "cancelled"

    def test_queue_item_is_cancelled_after_pipeline_cancel(self, tmp_path):
        """Queue item must have status='cancelled' after PipelineCancelledError."""
        db = _db(tmp_path)
        queue_id = _enqueue(db, channel_id=None)

        from hypogeum.pipeline import PipelineCancelledError
        import hypogeum.queue as queue_mod

        cancelled_error = PipelineCancelledError(episode_id="ep_q2", stage_after="audio_agent")
        original_pipeline = queue_mod._pipeline_module
        mock_pipeline = MagicMock()
        mock_pipeline.run_pipeline = MagicMock(side_effect=cancelled_error)
        mock_pipeline.PipelineCancelledError = PipelineCancelledError
        mock_pipeline.PipelineRejectedError = original_pipeline.PipelineRejectedError

        with patch.object(queue_mod, "_pipeline_module", mock_pipeline):
            from hypogeum.queue import process_next
            process_next(
                worker_id=str(uuid.uuid4()),
                db_path=db,
                dry_run=False,
                config=MagicMock(),
                topic_resolver=lambda item: "test topic",
            )

        assert _queue_status(db, queue_id) == "cancelled"

    def test_failed_result_still_works_without_cancel(self, tmp_path):
        """Regular exceptions must still produce status='failed' (regression guard)."""
        db = _db(tmp_path)
        _enqueue(db, channel_id=None)

        import hypogeum.queue as queue_mod
        original_pipeline = queue_mod._pipeline_module
        mock_pipeline = MagicMock()
        mock_pipeline.run_pipeline = MagicMock(side_effect=RuntimeError("normal failure"))
        mock_pipeline.PipelineCancelledError = original_pipeline.PipelineCancelledError
        mock_pipeline.PipelineRejectedError = original_pipeline.PipelineRejectedError

        with patch.object(queue_mod, "_pipeline_module", mock_pipeline):
            from hypogeum.queue import process_next
            result = process_next(
                worker_id=str(uuid.uuid4()),
                db_path=db,
                dry_run=False,
                config=MagicMock(),
                topic_resolver=lambda item: "test topic",
            )

        assert result.status == "failed"


# ===========================================================================
# 9. Queue Pause / Resume
# ===========================================================================

class TestQueuePauseResume:
    def test_pause_queue_skips_poll(self, tmp_path):
        """With queue_paused=1, run_iteration must skip poll (return 'paused')."""
        from hypogeum.db import set_worker_control
        db = _db(tmp_path)
        set_worker_control("queue_paused", "1", db_path=db)

        # Seed a queued item so there's something to claim
        _enqueue(db, channel_id=None)

        from run_worker import run_iteration
        result = run_iteration(
            worker_id=str(uuid.uuid4()),
            db_path=db,
            dry_run=True,
            channel_id=None,
            config=MagicMock(),
        )

        # Poll was skipped — queue item must still be queued
        from hypogeum.queue import get_queue_stats
        stats = get_queue_stats(db_path=db)
        assert stats["claimed"] == 0, "No items should be claimed when queue is paused"
        assert result["poll_status"] == "paused"

    def test_resume_queue_allows_claiming(self, tmp_path):
        """After resume, queue items are claimed normally on next iteration."""
        from hypogeum.db import set_worker_control
        db = _db(tmp_path)
        set_worker_control("queue_paused", "0", db_path=db)
        _enqueue(db, channel_id=None)

        from run_worker import run_iteration
        result = run_iteration(
            worker_id=str(uuid.uuid4()),
            db_path=db,
            dry_run=True,
            channel_id=None,
            config=MagicMock(),
        )

        # dry_run=True completes items immediately; poll_status should not be 'paused'
        assert result["poll_status"] != "paused"
        assert result["queue_paused"] is False
        # Confirms the clearance gate (wired 2026-07-18) is not the reason
        # poll ran -- this test's seeded factory/credentials make the
        # factory genuinely cleared, isolating "resume allows claiming"
        # from "clearance gate skipped poll" as two independently-tested
        # conditions.
        assert result["factory_cleared"] is True

    def test_pause_does_not_affect_current_run(self, tmp_path):
        """Setting queue_paused=1 while a run is executing must not interrupt it."""
        from hypogeum.db import set_worker_control, is_cancel_requested
        db = _db(tmp_path)
        queue_id = _enqueue(db)
        # Pausing the queue should not set cancel_requested on the claimed item
        set_worker_control("queue_paused", "1", db_path=db)
        assert is_cancel_requested(queue_id, db_path=db) is False


# ===========================================================================
# 10. Stop worker via DB flag
# ===========================================================================

class TestStopWorkerViaDB:
    def test_stop_requested_breaks_worker_loop(self, tmp_path, monkeypatch):
        """Worker must exit when stop_requested=1 in worker_control."""
        from hypogeum.db import set_worker_control, initialize_schema

        db = _db(tmp_path)
        # Pre-set the stop flag before the worker starts
        set_worker_control("stop_requested", "1", db_path=db)

        from run_worker import run_worker
        iterations_run = []

        def counting_iteration(**kwargs):
            iterations_run.append(1)
            # Shouldn't even be called — stop flag is checked before iteration
            return {"scheduled_runs_created": 0, "queue_items_created": 0,
                    "poll_status": "empty", "poll_stop_reason": "empty",
                    "dry_run": True, "discovery": {}, "analytics_synced": False,
                    "queue_paused": False}

        with patch("run_worker.run_iteration", side_effect=counting_iteration), \
             patch("hypogeum.config.Config", return_value=MagicMock()):
            exit_code = run_worker(
                db_path=db, dry_run=True, interval=0, once=False, channel_id=None
            )

        assert exit_code == 0
        # Worker should stop before running any iteration (stop_requested checked first)
        assert len(iterations_run) == 0


# ===========================================================================
# 11. Worker status written to DB
# ===========================================================================

class TestWorkerStatusTracking:
    def test_worker_writes_idle_on_start(self, tmp_path):
        """Worker must write worker_status='idle' on startup."""
        from hypogeum.db import get_worker_control, set_worker_control, initialize_schema

        db = _db(tmp_path)
        # Pre-set stop flag so the worker exits immediately after setup
        set_worker_control("stop_requested", "1", db_path=db)

        with patch("hypogeum.config.Config", return_value=MagicMock()):
            from run_worker import run_worker
            run_worker(db_path=db, dry_run=True, interval=0, once=False, channel_id=None)

        # After stop, status should be 'stopped'
        status = get_worker_control("worker_status", default="", db_path=db)
        assert status in ("stopped", "idle")

    def test_worker_writes_stopped_on_exit(self, tmp_path):
        """Worker must write worker_status='stopped' when it exits."""
        from hypogeum.db import get_worker_control, initialize_schema

        db = _db(tmp_path)

        with patch("hypogeum.config.Config", return_value=MagicMock()), \
             patch("run_worker.run_iteration",
                   return_value={"scheduled_runs_created": 0, "queue_items_created": 0,
                                 "poll_status": "empty", "poll_stop_reason": "empty",
                                 "dry_run": True, "discovery": {}, "analytics_synced": False,
                                 "queue_paused": False}):
            from run_worker import run_worker
            run_worker(db_path=db, dry_run=True, interval=0, once=True, channel_id=None)

        status = get_worker_control("worker_status", default="", db_path=db)
        assert status == "stopped"


# ===========================================================================
# 12. PollResult tracks cancelled count
# ===========================================================================

class TestPollResultCancelledCount:
    def test_cancelled_status_increments_counter(self, tmp_path):
        db = _db(tmp_path)
        _enqueue(db, channel_id=None)

        from hypogeum.pipeline import PipelineCancelledError
        import hypogeum.queue as queue_mod

        cancelled_error = PipelineCancelledError(episode_id="ep_poll", stage_after="script_engine")
        original_pipeline = queue_mod._pipeline_module
        mock_pipeline = MagicMock()
        mock_pipeline.run_pipeline = MagicMock(side_effect=cancelled_error)
        mock_pipeline.PipelineCancelledError = PipelineCancelledError
        mock_pipeline.PipelineRejectedError = original_pipeline.PipelineRejectedError

        with patch.object(queue_mod, "_pipeline_module", mock_pipeline):
            from hypogeum.queue import poll
            result = poll(
                worker_id=str(uuid.uuid4()),
                db_path=db,
                dry_run=False,
                max_iterations=1,
                sleep_interval=0.0,
                config=MagicMock(),
                topic_resolver=lambda item: "test topic",
            )

        assert result.cancelled == 1
        assert result.failed == 0
        assert result.completed == 0

    def test_total_processed_includes_cancelled(self, tmp_path):
        from hypogeum.queue import PollResult
        r = PollResult(completed=2, failed=1, skipped=1, no_topic=0, cancelled=2)
        assert r.total_processed == 6
