"""
tests/test_isolation.py — Test isolation regression suite.

Proves that the Factory OS runtime modules (OIS, queue, scheduler, runtime)
and the content pipeline never read or write the production database
(.hypogeum-assets/hypogeum.db) during test execution.

Every test here uses tmp_path (an isolated per-test directory). No test
touches the live asset root. No provider calls are made.

Coverage:
  TestOISIsolation      — OIS uses test DB in test context
  TestQueueIsolation    — queue uses test DB in test context
  TestSchedulerIsolation — scheduler uses test DB in test context
  TestRuntimeIsolation  — smoke_run uses test DB in test context
  TestPipelineOISIsolation — run_pipeline() does not reach live OIS DB
  TestRunRegistryIsolation — run_pipeline() does not write live run_registry DB
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.db import get_connection, initialize_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LIVE_DB = Path(".hypogeum-assets/hypogeum.db")


def _live_db_exists() -> bool:
    return _LIVE_DB.exists()


def _live_signal_count() -> int:
    """Return count of open blocking signals in the live DB (0 if DB absent)."""
    if not _live_db_exists():
        return 0
    conn = get_connection(_LIVE_DB)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM signal_records WHERE blocking=1 AND lifecycle_status='open'"
        ).fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


def _live_run_count() -> int:
    """Return count of rows in the live runs table."""
    if not _live_db_exists():
        return 0
    conn = get_connection(_LIVE_DB)
    try:
        return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


def _live_queue_count() -> int:
    """Return count of rows in the live run_queue table."""
    if not _live_db_exists():
        return 0
    conn = get_connection(_LIVE_DB)
    try:
        return conn.execute("SELECT COUNT(*) FROM run_queue").fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# TestOISIsolation
# ---------------------------------------------------------------------------

class TestOISIsolation:
    """OIS functions are safe with an isolated test DB."""

    def test_is_factory_blocked_returns_false_on_clean_db(self, tmp_path):
        """Fresh test DB has no signals → is_factory_blocked returns False."""
        from hypogeum.ois import is_factory_blocked
        db = tmp_path / "ois.db"
        initialize_schema(db)
        assert is_factory_blocked(db_path=db) is False

    def test_is_channel_blocked_returns_false_on_clean_db(self, tmp_path):
        """Fresh test DB has no signals → is_channel_blocked returns False."""
        from hypogeum.ois import is_channel_blocked
        db = tmp_path / "ois.db"
        initialize_schema(db)
        assert is_channel_blocked("ch_001", db_path=db) is False

    def test_emit_signal_writes_to_test_db_not_live(self, tmp_path):
        """emit_signal with db_path writes to test DB only."""
        from hypogeum.ois import emit_signal, is_factory_blocked
        db = tmp_path / "ois.db"
        initialize_schema(db)

        live_before = _live_signal_count()

        emit_signal(
            signal_type="credential_failure",
            severity="CRITICAL",
            message="test isolation signal",
            channel_id=None,
            source_system="test",
            blocking=True,
            db_path=db,
        )

        # Signal must be in test DB
        assert is_factory_blocked(db_path=db) is True
        # Live DB must be unchanged
        assert _live_signal_count() == live_before

    def test_blocking_signal_in_test_db_does_not_affect_live(self, tmp_path):
        """Active blocking signal in test DB has no effect on live DB state."""
        from hypogeum.ois import emit_signal, is_factory_blocked
        db = tmp_path / "ois.db"
        initialize_schema(db)

        emit_signal(
            signal_type="budget_exhaustion",
            severity="CRITICAL",
            message="test budget signal",
            channel_id=None,
            source_system="test",
            blocking=True,
            db_path=db,
        )

        assert is_factory_blocked(db_path=db) is True
        # Cross-check: calling with a different test DB returns False
        db2 = tmp_path / "ois2.db"
        initialize_schema(db2)
        assert is_factory_blocked(db_path=db2) is False


# ---------------------------------------------------------------------------
# TestQueueIsolation
# ---------------------------------------------------------------------------

class TestQueueIsolation:
    """Queue operations are safe with an isolated test DB."""

    def test_enqueue_writes_to_test_db_not_live(self, tmp_path):
        """enqueue() with db_path writes to test DB only."""
        from hypogeum.queue import enqueue, get_queue_stats
        db = tmp_path / "queue.db"
        initialize_schema(db)

        live_before = _live_queue_count()

        qid = enqueue(db_path=db, channel_id=None)
        assert qid is not None

        stats = get_queue_stats(db_path=db)
        assert stats["queued"] == 1
        assert _live_queue_count() == live_before

    def test_poll_uses_test_db_not_live(self, tmp_path):
        """poll() with db_path processes only the test DB queue."""
        from hypogeum.queue import enqueue, poll
        db = tmp_path / "queue.db"
        initialize_schema(db)

        enqueue(db_path=db, channel_id=None)
        live_before = _live_queue_count()

        result = poll(
            worker_id=str(uuid.uuid4()),
            db_path=db,
            dry_run=True,
            sleep_interval=0.0,
        )

        assert result.completed == 1
        assert _live_queue_count() == live_before

    def test_queue_isolation_between_two_test_dbs(self, tmp_path):
        """Items enqueued in one test DB do not appear in another."""
        from hypogeum.queue import enqueue, get_queue_stats
        db_a = tmp_path / "a.db"
        db_b = tmp_path / "b.db"
        initialize_schema(db_a)
        initialize_schema(db_b)

        enqueue(db_path=db_a, channel_id=None)
        enqueue(db_path=db_a, channel_id=None)

        assert get_queue_stats(db_path=db_a)["queued"] == 2
        assert get_queue_stats(db_path=db_b)["queued"] == 0


# ---------------------------------------------------------------------------
# TestSchedulerIsolation
# ---------------------------------------------------------------------------

class TestSchedulerIsolation:
    """Scheduler operations are safe with an isolated test DB."""

    def test_create_schedule_writes_to_test_db_not_live(self, tmp_path):
        """create_schedule() with db_path writes to test DB only."""
        from datetime import datetime, timedelta, timezone
        from hypogeum.scheduler import create_schedule, tick
        db = tmp_path / "sched.db"
        initialize_schema(db)

        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        sid = create_schedule(db_path=db, cadence="manual", next_run_at=past)
        assert sid is not None

        conn = get_connection(db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_tick_writes_to_test_db_not_live(self, tmp_path):
        """tick() creates scheduled_runs only in the test DB."""
        from datetime import datetime, timedelta, timezone
        from hypogeum.scheduler import create_schedule, tick
        db = tmp_path / "sched.db"
        initialize_schema(db)

        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        create_schedule(db_path=db, cadence="manual", next_run_at=past)
        sr_ids = tick(db_path=db, now=datetime.now(timezone.utc))

        assert len(sr_ids) == 1

        conn = get_connection(db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM scheduled_runs").fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_scheduler_isolation_between_two_test_dbs(self, tmp_path):
        """Schedules created in one test DB don't appear in another."""
        from datetime import datetime, timedelta, timezone
        from hypogeum.scheduler import create_schedule, tick
        db_a = tmp_path / "a.db"
        db_b = tmp_path / "b.db"
        initialize_schema(db_a)
        initialize_schema(db_b)

        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        create_schedule(db_path=db_a, cadence="manual", next_run_at=past)
        sr_ids_a = tick(db_path=db_a, now=datetime.now(timezone.utc))
        sr_ids_b = tick(db_path=db_b, now=datetime.now(timezone.utc))

        assert len(sr_ids_a) == 1
        assert len(sr_ids_b) == 0


# ---------------------------------------------------------------------------
# TestRuntimeIsolation
# ---------------------------------------------------------------------------

class TestRuntimeIsolation:
    """smoke_run() is safe with an isolated test DB."""

    def test_smoke_run_writes_to_test_db_not_live(self, tmp_path):
        """smoke_run() with db_path creates rows only in the test DB."""
        from hypogeum.runtime import smoke_run
        db = tmp_path / "runtime.db"
        initialize_schema(db)

        live_queue_before = _live_queue_count()
        result = smoke_run(db_path=db)

        assert result.success is True
        # Completed queue row must be in test DB
        conn = get_connection(db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM run_queue WHERE status='completed'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count >= 1
        # Live DB queue must be unchanged
        assert _live_queue_count() == live_queue_before

    def test_smoke_run_does_not_write_live_schedules(self, tmp_path):
        """smoke_run() creates schedules only in the test DB."""
        from hypogeum.runtime import smoke_run
        db = tmp_path / "runtime.db"
        initialize_schema(db)

        if _live_db_exists():
            live_conn = get_connection(_LIVE_DB)
            try:
                live_before = live_conn.execute(
                    "SELECT COUNT(*) FROM schedules"
                ).fetchone()[0]
            finally:
                live_conn.close()
        else:
            live_before = 0

        smoke_run(db_path=db)

        if _live_db_exists():
            live_conn = get_connection(_LIVE_DB)
            try:
                live_after = live_conn.execute(
                    "SELECT COUNT(*) FROM schedules"
                ).fetchone()[0]
            finally:
                live_conn.close()
            assert live_after == live_before


# ---------------------------------------------------------------------------
# TestPipelineOISIsolation
# ---------------------------------------------------------------------------

class TestPipelineOISIsolation:
    """
    run_pipeline() must not read the live OIS DB.

    The conftest._isolate_pipeline_ois fixture patches is_factory_blocked and
    is_channel_blocked in the pipeline namespace for every test. These tests
    verify that the patch is in place and effective.
    """

    def _make_config(self):
        import os
        from unittest.mock import patch as p
        from hypogeum.config import Config
        env = {"OPENROUTER_API_KEY": "or-key", "ELEVENLABS_VOICE_ID": "v-id"}
        with p.dict(os.environ, env, clear=True):
            return Config()

    def test_pipeline_not_blocked_by_live_ois_signals(self):
        """
        run_pipeline() proceeds even when the live DB would have blocking OIS
        signals.

        This test used to emit a real blocking signal into the live DB
        (db_path=_LIVE_DB) to prove the conftest patch protects run_pipeline()
        from it, with best-effort (non-guaranteed) cleanup afterwards. That
        was itself an isolation violation of the exact kind this file exists
        to catch: an explicit db_path pointing at the production file bypassed
        the old emit_signal guard (which only checked "was db_path omitted"),
        and the cleanup being a bare try/except rather than a finally meant
        any failure left the signal permanently stranded in production — which
        is exactly what happened (11 leaked credential_failure signals,
        2026-08-10 through 2026-08-17, cleaned up as a separate incident).

        conftest.py's _isolate_ois_emit_signal fixture is now path-aware (see
        that fixture's docstring) and no-ops ANY call whose effective db_path
        resolves to the production file, explicit or not — so emit_signal can
        no longer reach the live DB from this or any other test. This test now
        only needs to prove run_pipeline() itself does not consult live OIS
        state (via the conftest _isolate_pipeline_ois patch), which is the
        actual thing under test.
        """
        from hypogeum.pipeline import run_pipeline
        from hypogeum.startup import StartupReport

        cfg = self._make_config()
        startup_report = StartupReport(
            overall_status="healthy",
            openrouter_reachable=True,
            models_verified=True,
            fallback_active=False,
            fallback_configured=False,
            checked_at="2026-01-01T00:00:00Z",
        )

        fake_ep_result = (
            {"decision": "PASS", "overall_score": 8.0},
            {"episode_title": "Test", "logline": "Test logline"},
        )

        with patch("hypogeum.pipeline.run_startup_checks", return_value=startup_report):
            with patch("hypogeum.pipeline._run_executive_producer", return_value=fake_ep_result):
                with patch("hypogeum.pipeline._run_research_director",
                           return_value=({"source_registry": []}, None)):
                    with patch("hypogeum.pipeline._run_narrative_architect",
                               return_value={"narrative_arc": "mystery"}):
                        with patch("hypogeum.pipeline._run_script_engine",
                                   return_value={"sections": [], "title": "Test"}):
                            with patch("hypogeum.pipeline._run_budget_authorization",
                                       return_value={"verdict": "AUTHORIZED",
                                                     "estimated_spend_usd": 1.0}):
                              with patch("hypogeum.pipeline._run_thumbnail_strategy",
                                         return_value={}):
                                with patch("hypogeum.pipeline._run_visual_package_director",
                                           return_value={"scenes": [],
                                                         "episode_metadata": {},
                                                         "package_metadata": {}}):
                                    with patch("hypogeum.pipeline._run_thumbnail_director",
                                               return_value={"main_thumbnail": {},
                                                             "thumbnail_package_id": "tp"}):
                                        with patch("hypogeum.pipeline._run_audio_agent",
                                                   return_value={"stitched_tracks": [],
                                                                  "narration_segments": [],
                                                                  "package_metadata": {}}):
                                            with patch("hypogeum.pipeline._run_assembly_agent",
                                                       return_value={"episode_render": {},
                                                                      "shorts_renders": [],
                                                                      "thumbnail_references": [],
                                                                      "music_log": [],
                                                                      "metadata_source": {
                                                                          "ai_disclosure_text": "AI",
                                                                          "section_titles": [],
                                                                      },
                                                                      "assembly_qa_report": {},
                                                                      "package_integrity": {
                                                                          "production_ready": True,
                                                                      },
                                                                      "package_metadata": {}}):
                                                with patch("hypogeum.pipeline._run_asset_archive",
                                                           return_value={}):
                                                    with patch("hypogeum.pipeline._run_qa_agent",
                                                               return_value={"verdict": "APPROVED",
                                                                              "failures": []}):
                                                        with patch("hypogeum.pipeline._run_compliance_agent",
                                                                   return_value={"verdict": "APPROVED",
                                                                                  "failures": []}):
                                                            with patch("hypogeum.pipeline._run_publishing_agent",
                                                                       return_value={
                                                                           "summary": {},
                                                                           "published_at": "",
                                                                       }):
                                                                with patch("hypogeum.pipeline._run_archive_agent",
                                                                           return_value={}):
                                                                    with patch("hypogeum.pipeline._run_cost_ledger",
                                                                               return_value={}):
                                                                        with patch("hypogeum.pipeline.write_production_metadata",
                                                                                   return_value={}):
                                                                            with patch("hypogeum.pipeline.run_post_run_intelligence"):
                                                                                with patch("hypogeum.pipeline.write_analytics_pending",
                                                                                           return_value={}):
                                                                                    # Must not raise OISBlockedError
                                                                                    result = run_pipeline(
                                                                                        topic_input="Test",
                                                                                        episode_id="ep_isolation_test",
                                                                                        config=cfg,
                                                                                        channel_id=None,
                                                                                    )
                                                                                    assert result is not None

    def test_conftest_ois_patch_is_active(self):
        """The conftest autouse fixture must patch is_factory_blocked in pipeline namespace."""
        import hypogeum.pipeline as pl
        # The conftest patches hypogeum.pipeline.is_factory_blocked to return False.
        # We verify the patched function returns False even when live DB might be blocked.
        result = pl.is_factory_blocked()
        assert result is False

    def test_conftest_ois_channel_patch_is_active(self):
        """The conftest autouse fixture must patch is_channel_blocked in pipeline namespace."""
        import hypogeum.pipeline as pl
        result = pl.is_channel_blocked("any-channel-id")
        assert result is False

    def test_emit_signal_with_explicit_production_db_path_does_not_write(self):
        """
        Regression test for the 2026-08-17 OIS contamination incident.

        Root cause: conftest.py's _isolate_ois_emit_signal guard used to check
        only "was db_path omitted" — a call that explicitly passed the real
        production path (db_path=_LIVE_DB, exactly what
        test_pipeline_not_blocked_by_live_ois_signals used to do) sailed
        straight through and wrote a real row. This test proves the guard now
        also catches an *explicit* db_path that resolves to the production
        file, using the exact same call shape as the leak.
        """
        from hypogeum.ois import emit_signal

        before = _live_signal_count() if _live_db_exists() else 0

        signal_id = emit_signal(
            signal_type="credential_failure",
            severity="CRITICAL",
            message="pipeline isolation test signal",
            channel_id=None,
            source_system="test_isolation",
            blocking=True,
            db_path=_LIVE_DB,
        )

        # A fake UUID is still returned (callers get a signal_id either way),
        # but no row may have been written to the live DB.
        assert signal_id is not None
        after = _live_signal_count() if _live_db_exists() else 0
        assert after == before, (
            "emit_signal() wrote to the production DB despite an explicit "
            "db_path pointing at it -- the isolation guard regressed"
        )

        if _live_db_exists():
            conn = get_connection(_LIVE_DB)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM signal_records WHERE signal_id = ?",
                    (signal_id,),
                ).fetchone()
                assert row[0] == 0, "the guarded signal_id must not exist in the production DB"
            finally:
                conn.close()

    def test_emit_signal_with_tmp_db_path_still_writes(self, tmp_path):
        """
        The guard must not be so broad it blocks legitimate isolated writes --
        an explicit db_path that does NOT resolve to the production file must
        still pass through to a real write (mirrors test_ois.py's existing
        coverage; kept here so the two guard behaviours are visible side by
        side in the same file that documents the incident).
        """
        from hypogeum.db import initialize_schema
        from hypogeum.ois import emit_signal

        db_path = tmp_path / "isolated.db"
        initialize_schema(db_path)

        signal_id = emit_signal(
            signal_type="credential_failure",
            severity="CRITICAL",
            message="tmp db regression check",
            channel_id=None,
            source_system="test_isolation",
            blocking=True,
            db_path=db_path,
        )

        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM signal_records WHERE signal_id = ?",
                (signal_id,),
            ).fetchone()
            assert row[0] == 1, "an explicit non-production db_path must still receive the write"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestRunRegistryIsolation
# ---------------------------------------------------------------------------

class TestRunRegistryIsolation:
    """run_pipeline() must not write run rows to the production database."""

    def test_conftest_run_registry_patch_is_active(self):
        """The conftest autouse fixture must stub register_run in pipeline namespace."""
        import hypogeum.pipeline as pl
        # The conftest patches register_run to return a fixed test run ID.
        # Call it directly to verify the patch is active.
        result = pl.register_run(
            episode_id="ep_test",
            config=MagicMock(),
        )
        assert result == "test-run-id-isolated"

    def test_pipeline_does_not_write_run_rows_to_live_db(self):
        """Calling run_pipeline() with mocked agents must not create rows in live runs table."""
        from hypogeum.pipeline import run_pipeline, PipelineRejectedError
        from hypogeum.startup import StartupReport
        import os
        from unittest.mock import patch as p

        live_runs_before = _live_run_count()

        cfg_env = {"OPENROUTER_API_KEY": "or-key", "ELEVENLABS_VOICE_ID": "v-id"}
        with p.dict(os.environ, cfg_env, clear=True):
            from hypogeum.config import Config
            cfg = Config()

        startup_report = StartupReport(
            overall_status="healthy",
            openrouter_reachable=True,
            models_verified=True,
            fallback_active=False,
            fallback_configured=False,
            checked_at="2026-01-01T00:00:00Z",
        )
        rejection_scorecard = {"decision": "REJECT", "overall_score": 2.0}
        fake_ep_result = (rejection_scorecard, {})

        with patch("hypogeum.pipeline.run_startup_checks", return_value=startup_report):
            with patch("hypogeum.pipeline._run_executive_producer", return_value=fake_ep_result):
                try:
                    run_pipeline(
                        topic_input="Test",
                        episode_id="ep_regtest_isolated",
                        config=cfg,
                        channel_id=None,
                    )
                except PipelineRejectedError:
                    pass  # Expected — topic rejected

        # Live runs table must be unchanged
        assert _live_run_count() == live_runs_before
