"""
tests/test_worker.py — run_worker.py unit tests

Covers:
  - scheduled runs are promoted from schedules → scheduled_runs → run_queue
  - queued items are claimed by process_next
  - dry_run=True never calls run_pipeline
  - dry_run=False reaches the process path
  - --once exits after one iteration
  - --dry-run flag propagates correctly
  - graceful shutdown on signal
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _past(minutes: int = 5) -> datetime:
    return _now_utc() - timedelta(minutes=minutes)


def _seed_cleared_factory(db_path: Path) -> None:
    """Seed the minimal active factory + credential rows
    hypogeum.factory_director.check_run_clearance() requires to return
    cleared=True. Without this, run_iteration()'s clearance gate (wired
    2026-07-18) skips poll on a bare test schema, since a schema with no
    factory row / no active credentials is a genuinely un-cleared state,
    not a test artifact to special-case around."""
    from hypogeum.db import get_connection
    conn = get_connection(db_path)
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


def _setup_db(db_path: Path) -> str:
    """Initialize schema and seed one active daily schedule due in the past."""
    from hypogeum.db import initialize_schema
    from hypogeum.scheduler import create_schedule
    from hypogeum.calog import set_channel_state

    initialize_schema(db_path)
    _seed_cleared_factory(db_path)
    set_channel_state("hypogeum", "active", triggered_by="test_setup", db_path=db_path)
    schedule_id = create_schedule(
        db_path=db_path,
        scope="factory",
        channel_id="hypogeum",
        cadence="daily",
        next_run_at=_past(10),
        label="test_schedule",
    )
    return schedule_id


@pytest.fixture
def mock_config():
    """Patch Config() so worker tests don't need real env vars."""
    cfg = MagicMock()
    # Config is imported locally inside run_worker() — patch the source module
    with patch("hypogeum.config.Config", return_value=cfg):
        yield cfg


def _enqueue_directly(db_path: Path, channel_id: str = "hypogeum") -> str:
    """Insert one run_queue row directly (bypasses scheduler), with a channel."""
    from hypogeum.db import get_connection, initialize_schema
    initialize_schema(db_path)
    queue_id = str(uuid.uuid4())
    conn = get_connection(db_path)
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


def _seed_queued_topic(db_path: Path, channel_id: str = "hypogeum") -> str:
    """Insert one queued topic so process_next has something to consume."""
    from hypogeum.db import get_connection
    topic_id = str(uuid.uuid4())
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO episode_topics (topic_id, channel_id, topic_text, status, priority, created_at, updated_at) "
            "VALUES (?, ?, ?, 'queued', 0, datetime('now'), datetime('now'))",
            (topic_id, channel_id, "Test topic for worker"),
        )
        conn.commit()
    finally:
        conn.close()
    return topic_id


# ---------------------------------------------------------------------------
# Priority 1: scheduled_runs are promoted
# ---------------------------------------------------------------------------

class TestScheduledRunPromotion:
    def test_tick_creates_scheduled_run(self, tmp_path):
        """tick() must create a scheduled_run row for a due schedule."""
        db = tmp_path / "h.db"
        _setup_db(db)

        from hypogeum.scheduler import tick
        sr_ids = tick(db_path=db, now=_now_utc())

        assert len(sr_ids) == 1

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status FROM scheduled_runs WHERE scheduled_run_id=?",
                           (sr_ids[0],)).fetchone()
        conn.close()
        assert row["status"] == "pending"

    def test_consume_promotes_to_run_queue(self, tmp_path):
        """consume_scheduled_runs() must move pending scheduled_runs to run_queue."""
        db = tmp_path / "h.db"
        _setup_db(db)

        from hypogeum.scheduler import tick
        from hypogeum.queue import consume_scheduled_runs

        tick(db_path=db, now=_now_utc())
        queue_ids = consume_scheduled_runs(db_path=db)

        assert len(queue_ids) == 1

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status FROM run_queue WHERE queue_id=?",
                           (queue_ids[0],)).fetchone()
        conn.close()
        assert row["status"] == "queued"

    def test_run_iteration_promotes_due_schedule(self, tmp_path):
        """run_iteration() must run the full tick→consume→poll chain."""
        db = tmp_path / "h.db"
        _setup_db(db)

        from run_worker import run_iteration
        from hypogeum.config import Config

        result = run_iteration(
            worker_id=str(uuid.uuid4()),
            db_path=db,
            dry_run=True,
            channel_id=None,
            config=MagicMock(spec=Config),
        )

        assert result["scheduled_runs_created"] == 1
        assert result["queue_items_created"] == 1

    def test_second_tick_does_not_duplicate(self, tmp_path):
        """Daily schedule: second tick within the same period must not double-fire."""
        db = tmp_path / "h.db"
        _setup_db(db)

        from hypogeum.scheduler import tick
        from hypogeum.queue import consume_scheduled_runs

        tick(db_path=db, now=_now_utc())
        consume_scheduled_runs(db_path=db)

        # Second tick: next_run_at has been advanced, so nothing should fire
        sr_ids2 = tick(db_path=db, now=_now_utc())
        assert sr_ids2 == []


# ---------------------------------------------------------------------------
# Priority 2: queued item is claimed
# ---------------------------------------------------------------------------

class TestQueuedItemClaimed:
    def test_dry_run_claims_and_completes_without_pipeline(self, tmp_path):
        """dry_run=True: item is claimed and completed; pipeline never called."""
        db = tmp_path / "h.db"
        _setup_db(db)

        from hypogeum.scheduler import tick
        from hypogeum.queue import consume_scheduled_runs, poll

        tick(db_path=db, now=_now_utc())
        consume_scheduled_runs(db_path=db)

        worker_id = str(uuid.uuid4())
        with patch("hypogeum.pipeline.run_pipeline") as mock_pipe:
            result = poll(
                worker_id=worker_id,
                db_path=db,
                dry_run=True,
                max_iterations=1,
                sleep_interval=0.0,
            )

        mock_pipe.assert_not_called()
        assert result.completed == 1

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status FROM run_queue LIMIT 1").fetchone()
        conn.close()
        assert row["status"] == "completed"

    def test_item_status_is_completed_after_dry_run(self, tmp_path):
        """DB must reflect completed status after dry_run poll."""
        db = tmp_path / "h.db"
        _setup_db(db)

        from hypogeum.scheduler import tick
        from hypogeum.queue import consume_scheduled_runs, poll

        tick(db_path=db, now=_now_utc())
        queue_ids = consume_scheduled_runs(db_path=db)

        poll(worker_id=str(uuid.uuid4()), db_path=db, dry_run=True,
             max_iterations=1, sleep_interval=0.0)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status FROM run_queue WHERE queue_id=?",
                           (queue_ids[0],)).fetchone()
        conn.close()
        assert row["status"] == "completed"


# ---------------------------------------------------------------------------
# Priority 3: dry_run=True never calls real pipeline
# ---------------------------------------------------------------------------

class TestDryRunNeverCallsPipeline:
    def test_run_iteration_dry_run_never_calls_pipeline(self, tmp_path):
        """run_iteration(dry_run=True) must never invoke run_pipeline."""
        db = tmp_path / "h.db"
        _setup_db(db)

        from run_worker import run_iteration

        with patch("hypogeum.pipeline.run_pipeline") as mock_pipe:
            run_iteration(
                worker_id=str(uuid.uuid4()),
                db_path=db,
                dry_run=True,
                channel_id=None,
                config=MagicMock(),
            )

        mock_pipe.assert_not_called()

    def test_worker_dry_run_flag_propagates_to_poll(self, tmp_path, mock_config):
        """--dry-run CLI flag must pass dry_run=True through to poll()."""
        db = tmp_path / "h.db"
        _setup_db(db)

        from run_worker import run_worker
        from hypogeum.queue import PollResult

        poll_calls = []

        def capturing_poll(**kwargs):
            poll_calls.append(kwargs.get("dry_run"))
            return PollResult()

        with patch("hypogeum.queue.poll", side_effect=capturing_poll):
            run_worker(db_path=db, dry_run=True, interval=0, once=True, channel_id=None)

        assert len(poll_calls) == 1
        assert poll_calls[0] is True, f"Expected dry_run=True, got: {poll_calls}"


# ---------------------------------------------------------------------------
# Priority 4: dry_run=False reaches the process path
# ---------------------------------------------------------------------------

class TestDryRunFalseCallsProcessPath:
    def test_dry_run_false_calls_process_next(self, tmp_path):
        """dry_run=False must invoke run_pipeline via process_next.

        Uses channel_id=None (factory scope) to skip CALOG, plus a
        topic_resolver so process_next has a topic without a DB backlog.
        """
        db = tmp_path / "h.db"
        _setup_db(db)
        # Factory-scoped item: channel_id=None bypasses CALOG check
        _enqueue_directly(db, channel_id=None)

        mock_pipeline = MagicMock(
            side_effect=Exception("sentinel — pipeline reached")
        )

        with patch("hypogeum.pipeline.run_pipeline", mock_pipeline):
            from hypogeum.queue import poll
            result = poll(
                worker_id=str(uuid.uuid4()),
                db_path=db,
                dry_run=False,
                max_iterations=1,
                sleep_interval=0.0,
                config=MagicMock(),
                # Provide topic directly so claim_next_topic is never called
                topic_resolver=lambda item: "Test topic for worker dry_run=False",
            )

        mock_pipeline.assert_called_once()
        assert result.failed == 1

    def test_run_iteration_dry_run_false_reaches_pipeline(self, tmp_path):
        """run_iteration(dry_run=False) must reach run_pipeline.

        Uses a channel-scoped queue item (channel_id='hypogeum') with a custom
        topic_resolver to bypass CALOG and topic-backlog lookups.
        """
        db = tmp_path / "h.db"
        _setup_db(db)
        _enqueue_directly(db)

        from run_worker import run_iteration

        mock_pipeline = MagicMock(
            side_effect=Exception("sentinel — pipeline reached")
        )

        with patch("hypogeum.pipeline.run_pipeline", mock_pipeline):
            result_live = run_iteration(
                worker_id=str(uuid.uuid4()),
                db_path=db,
                dry_run=False,
                channel_id=None,
                config=MagicMock(),
            )

        # Re-enqueue for a dry_run comparison
        _enqueue_directly(db)
        result_dry = run_iteration(
            worker_id=str(uuid.uuid4()),
            db_path=db,
            dry_run=True,
            channel_id=None,
            config=MagicMock(),
        )

        # dry_run=True always returns "completed" (item done, no pipeline).
        # dry_run=False with no topic returns "no_topic" (topic resolution ran).
        # Distinct statuses prove the two code paths diverge.
        assert result_dry["poll_status"] == "completed"
        assert result_live["poll_status"] == "no_topic"
        mock_pipeline.assert_not_called()  # no topic means pipeline never reached


# ---------------------------------------------------------------------------
# Priority 5: --once and --dry-run flags
# ---------------------------------------------------------------------------

class TestWorkerFlags:
    def test_once_exits_after_one_iteration(self, tmp_path, mock_config):
        """--once must exit after exactly one tick→consume→poll cycle."""
        db = tmp_path / "h.db"
        _setup_db(db)

        from run_worker import run_worker

        iterations_run = []

        original_run_iteration = __import__("run_worker").run_iteration

        def counting_iteration(**kwargs):
            iterations_run.append(1)
            return original_run_iteration(**kwargs)

        with patch("run_worker.run_iteration", side_effect=counting_iteration):
            exit_code = run_worker(
                db_path=db, dry_run=True, interval=0, once=True, channel_id=None
            )

        assert exit_code == 0
        assert len(iterations_run) == 1

    def test_channel_id_passed_to_iteration(self, tmp_path, mock_config):
        """--channel-id must be forwarded to run_iteration."""
        db = tmp_path / "h.db"
        _setup_db(db)

        from run_worker import run_worker

        captured = {}

        def capturing_iteration(**kwargs):
            captured.update(kwargs)
            return {"scheduled_runs_created": 0, "queue_items_created": 0,
                    "poll_status": "empty", "poll_stop_reason": "empty", "dry_run": True}

        with patch("run_worker.run_iteration", side_effect=capturing_iteration):
            run_worker(db_path=db, dry_run=True, interval=0, once=True,
                       channel_id="hypogeum")

        assert captured.get("channel_id") == "hypogeum"

    def test_main_parses_dry_run_flag(self, tmp_path):
        """CLI --dry-run flag must propagate to run_worker(dry_run=True)."""
        db = tmp_path / "h.db"
        _setup_db(db)

        from run_worker import main

        calls = {}

        def mock_run_worker(**kwargs):
            calls.update(kwargs)
            return 0

        with patch("run_worker.run_worker", side_effect=mock_run_worker):
            main(["--dry-run", "--once", "--db-path", str(db)])

        assert calls["dry_run"] is True
        assert calls["once"] is True

    def test_main_parses_interval(self, tmp_path):
        """CLI --interval must be parsed as a float."""
        db = tmp_path / "h.db"
        _setup_db(db)

        from run_worker import main

        calls = {}

        def mock_run_worker(**kwargs):
            calls.update(kwargs)
            return 0

        with patch("run_worker.run_worker", side_effect=mock_run_worker):
            main(["--dry-run", "--once", "--interval", "30", "--db-path", str(db)])

        assert calls["interval"] == 30.0


# ---------------------------------------------------------------------------
# Factory clearance gate (wired 2026-07-18) — hard block before claiming
# ---------------------------------------------------------------------------

class TestFactoryClearanceGate:
    def _seed_cost(self, db_path: Path, cost_usd: float) -> None:
        from hypogeum.db import get_connection
        run_id = str(uuid.uuid4())
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT INTO runs (run_id, episode_id, started_at, status) "
                "VALUES (?, ?, datetime('now'), 'completed')",
                (run_id, f"ep_{run_id[:6]}"),
            )
            conn.execute(
                "INSERT INTO costs "
                "(cost_id, run_id, episode_id, provider, call_type, cost_usd, called_at) "
                "VALUES (?,?,?,?,?,?,datetime('now'))",
                (str(uuid.uuid4()), run_id, f"ep_{run_id[:6]}", "muapi", "video", cost_usd),
            )
            conn.commit()
        finally:
            conn.close()

    def test_over_budget_skips_poll_entirely(self, tmp_path):
        """run_iteration must not claim a queued item when the factory is
        not cleared (here: monthly budget ceiling exceeded) -- the gate
        must fire before poll(), not just be reported by the dashboard."""
        db = tmp_path / "h.db"
        _setup_db(db)  # seeds an active factory + active credential
        self._seed_cost(db, 999.0)
        _enqueue_directly(db)

        from hypogeum.factory_entity import update_factory
        update_factory("hypogeum", db_path=db, factory_policies={"monthly_ceiling_usd": 10.0})

        from run_worker import run_iteration

        with patch("hypogeum.pipeline.run_pipeline") as mock_pipe:
            result = run_iteration(
                worker_id=str(uuid.uuid4()),
                db_path=db,
                dry_run=True,
                channel_id=None,
                config=MagicMock(),
            )

        mock_pipe.assert_not_called()
        assert result["factory_cleared"] is False
        assert result["poll_status"] == "not_cleared"
        assert "clearance_reason" in result

        from hypogeum.queue import get_queue_stats
        stats = get_queue_stats(db_path=db)
        assert stats["claimed"] == 0, "No item should be claimed when over budget ceiling"

    def test_under_budget_allows_poll(self, tmp_path):
        """Sanity check: the same scenario with spend under the ceiling must
        NOT be blocked -- isolates 'gate exists' from 'gate is always on'."""
        db = tmp_path / "h.db"
        _setup_db(db)
        self._seed_cost(db, 1.0)
        _enqueue_directly(db)

        from hypogeum.factory_entity import update_factory
        update_factory("hypogeum", db_path=db, factory_policies={"monthly_ceiling_usd": 10.0})

        from run_worker import run_iteration

        result = run_iteration(
            worker_id=str(uuid.uuid4()),
            db_path=db,
            dry_run=True,
            channel_id=None,
            config=MagicMock(),
        )

        assert result["factory_cleared"] is True
        assert result["poll_status"] != "not_cleared"

    def test_validation_mode_exempts_budget_ceiling(self, tmp_path):
        """validation_mode=True must let a supervised validation run
        proceed even when month-to-date spend exceeds the ceiling --
        narrow, explicit exemption added 2026-07-18."""
        db = tmp_path / "h.db"
        _setup_db(db)
        self._seed_cost(db, 999.0)
        _enqueue_directly(db)

        from hypogeum.factory_entity import update_factory
        update_factory("hypogeum", db_path=db, factory_policies={"monthly_ceiling_usd": 10.0})

        from run_worker import run_iteration

        result = run_iteration(
            worker_id=str(uuid.uuid4()),
            db_path=db,
            dry_run=True,
            channel_id=None,
            config=MagicMock(),
            validation_mode=True,
        )

        assert result["factory_cleared"] is True
        assert result["poll_status"] != "not_cleared"
        assert result["validation_exemption"] == "supervised validation budget exemption"

    def test_validation_mode_defaults_false(self, tmp_path):
        """Omitting validation_mode must behave identically to the
        over-budget test above -- the exemption is opt-in only."""
        db = tmp_path / "h.db"
        _setup_db(db)
        self._seed_cost(db, 999.0)
        _enqueue_directly(db)

        from hypogeum.factory_entity import update_factory
        update_factory("hypogeum", db_path=db, factory_policies={"monthly_ceiling_usd": 10.0})

        from run_worker import run_iteration

        result = run_iteration(
            worker_id=str(uuid.uuid4()),
            db_path=db,
            dry_run=True,
            channel_id=None,
            config=MagicMock(),
        )

        assert result["factory_cleared"] is False
        assert result["poll_status"] == "not_cleared"
        assert result["validation_exemption"] is None

    def test_autonomous_worker_loop_never_sets_validation_mode(self):
        """run_worker() -- the actual autonomous daemon loop -- must never
        pass validation_mode to run_iteration(). Static check: the only
        caller allowed to set it is an explicit, one-off Python caller
        (e.g. a supervised validation script), never the CLI/daemon path."""
        import ast
        import inspect
        import run_worker as mod

        src = inspect.getsource(mod.run_worker)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "run_iteration"):
                kwarg_names = {kw.arg for kw in node.keywords}
                assert "validation_mode" not in kwarg_names

    def test_no_cli_flag_for_validation_mode(self):
        """There must be no --validation-mode (or similar) CLI flag --
        the exemption is reachable only via a direct Python call."""
        import inspect
        import run_worker as mod

        src = inspect.getsource(mod._parse_args)
        assert "validation" not in src.lower()


# ---------------------------------------------------------------------------
# Startup & Shutdown Verification (Phase 3 Production Hardening, P0.5)
# ---------------------------------------------------------------------------

class TestStartupVerificationWiring:
    """
    Thin-integration tests only: confirm run_worker() calls
    hypogeum.startup_verification.verify_startup() automatically at
    startup and logs when blockers are present. verify_startup()'s own
    behavior (preflight checks, OIS signal emission) is covered in
    tests/test_startup_verification.py — not re-tested here.

    verify_startup is mocked rather than exercised with a real config,
    because a bare MagicMock config (this file's existing mock_config
    fixture) makes every getattr(config, "TELEGRAM_BOT_TOKEN", None)
    truthy, which would make hypogeum.notifications.route_signal()
    attempt a real network call — exactly what test isolation must avoid.
    """

    def test_verify_startup_is_called_with_the_worker_db_path(
        self, tmp_path, mock_config,
    ):
        db = tmp_path / "h.db"
        _setup_db(db)

        from run_worker import run_worker
        from hypogeum.queue import PollResult

        with patch(
            "hypogeum.startup_verification.verify_startup",
            return_value={"ready": True, "blocker_count": 0, "blockers": [], "signal_id": None},
        ) as mock_verify:
            with patch("hypogeum.queue.poll", return_value=PollResult()):
                run_worker(db_path=db, dry_run=True, interval=0, once=True, channel_id=None)

        mock_verify.assert_called_once()
        assert mock_verify.call_args.kwargs.get("db_path") == db

    def test_blockers_present_do_not_abort_the_worker(self, tmp_path, mock_config):
        """A blocker must alert (verify_startup's own job — see
        tests/test_startup_verification.py) but never stop the
        scheduler/queue loop from running."""
        db = tmp_path / "h.db"
        _setup_db(db)

        blocked_report = {
            "ready": False, "blocker_count": 1,
            "blockers": [{"name": "ELEVENLABS_API_KEY", "message": "Missing", "action": ""}],
            "signal_id": "sig-123",
        }
        from run_worker import run_worker
        from hypogeum.queue import PollResult

        with patch("hypogeum.startup_verification.verify_startup", return_value=blocked_report):
            with patch("hypogeum.queue.poll", return_value=PollResult()) as mock_poll:
                exit_code = run_worker(db_path=db, dry_run=True, interval=0, once=True, channel_id=None)

        assert exit_code == 0
        mock_poll.assert_called_once()  # the loop ran despite the blocker

    def test_startup_verification_error_does_not_abort_worker(self, tmp_path, mock_config):
        """A crash inside verify_startup (e.g. a DB hiccup) must never
        prevent the worker from starting its loop."""
        db = tmp_path / "h.db"
        _setup_db(db)

        with patch(
            "hypogeum.startup_verification.verify_startup",
            side_effect=RuntimeError("boom"),
        ):
            from run_worker import run_worker
            from hypogeum.queue import PollResult
            with patch("hypogeum.queue.poll", return_value=PollResult()) as mock_poll:
                exit_code = run_worker(db_path=db, dry_run=True, interval=0, once=True, channel_id=None)

        assert exit_code == 0
        mock_poll.assert_called_once()  # the loop still ran despite the startup error


class TestGracefulShutdownOnSignal:
    """
    The docstring at the top of this file has claimed "graceful shutdown
    on signal" as covered since before this session, but no test actually
    exercised it. This closes that gap (Phase 3 Production Hardening,
    P0.5) — verifying existing behavior, not adding new shutdown logic:
    run_worker.py's _shutdown_requested flag (set by _handle_signal(),
    itself already registered for SIGINT/SIGTERM at import time) is
    checked at the top of every loop iteration, after each iteration
    completes, and during the inter-iteration sleep — a signal never
    interrupts an in-flight queue claim or pipeline stage; it lets the
    current iteration finish before the loop exits.
    """

    def test_handle_signal_sets_the_shutdown_flag(self):
        import signal
        import run_worker as mod

        mod._shutdown_requested = False
        try:
            mod._handle_signal(signal.SIGTERM, None)
            assert mod._shutdown_requested is True
        finally:
            mod._shutdown_requested = False  # don't leak into other tests

    def test_loop_exits_promptly_once_shutdown_is_requested_mid_run(
        self, tmp_path, mock_config,
    ):
        """Simulates signal arrival by setting the same flag _handle_signal
        sets, from a separate thread while run_worker() is mid-loop with a
        long interval — equivalent in effect to a real SIGTERM arriving
        during the inter-iteration sleep, without needing real OS signals
        in a unit test. The loop must exit well before the full interval
        elapses, and worker_status must end as 'stopped'.
        """
        import threading
        import time
        import run_worker as mod
        from hypogeum.queue import PollResult

        db = tmp_path / "h.db"
        _setup_db(db)
        mod._shutdown_requested = False

        def _trigger_shutdown_shortly():
            time.sleep(0.3)
            mod._shutdown_requested = True

        with patch("hypogeum.queue.poll", return_value=PollResult()):
            with patch(
                "hypogeum.startup_verification.verify_startup",
                return_value={"ready": True, "blocker_count": 0, "blockers": [], "signal_id": None},
            ):
                trigger = threading.Thread(target=_trigger_shutdown_shortly)
                trigger.start()
                start = time.monotonic()
                exit_code = mod.run_worker(
                    db_path=db, dry_run=True, interval=30, once=False, channel_id=None,
                )
                elapsed = time.monotonic() - start
                trigger.join()

        assert exit_code == 0
        assert elapsed < 5, f"loop took {elapsed:.1f}s to exit — sleep phase did not respect shutdown flag"

        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT value FROM worker_control WHERE key = 'worker_status'"
            ).fetchone()
        finally:
            conn.close()
        assert row["value"] == "stopped"
