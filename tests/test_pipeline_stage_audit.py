"""
tests/test_pipeline_stage_audit.py — P3-01 stage execution audit write verification.

Tests that stage_executions rows are written correctly to the DB, that a failed
stage is recorded with status='failed', and that audit write failures are
non-blocking (pipeline completes successfully even when the DB is unavailable).

Isolation: all tests pass db_path=<tmp_path> directly to run_pipeline().
No module-level patching of get_connection is used; the test DB is an isolated
SQLite file in pytest's tmp_path. No production DB is accessed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.pipeline import run_pipeline
from tests.test_pipeline import (
    MINIMAL_FINAL_RENDER_PACKAGE,
    MINIMAL_FINANCE_AUTHORIZATION,
    MINIMAL_FINANCE_RECORD,
    MINIMAL_NARRATION_PACKAGE,
    MINIMAL_NARRATIVE_PACKAGE,
    MINIMAL_PUBLISHING_RECORD,
    MINIMAL_RESEARCH_PACKAGE,
    MINIMAL_SCRIPT_PACKAGE,
    MINIMAL_SCORECARD_PASS,
    MINIMAL_THUMBNAIL_PACKAGE,
    MINIMAL_TOPIC_BRIEF,
    MINIMAL_VISUAL_PACKAGE,
    _all_stubs_context,
    make_config,
    make_startup_report,
)

# ---------------------------------------------------------------------------
# Expected stages — one per _record_stage_start call in run_pipeline()
# ---------------------------------------------------------------------------

_EXPECTED_STAGES = {
    "startup_checks",
    "executive_producer",
    "research_director",
    "narrative_architect",
    "script_engine",
    "finance_agent_authorization",
    "thumbnail_strategy",
    "visual_package_director",
    "source_enhancement",
    "thumbnail_director",
    "audio_agent",
    "assembly_agent",
    "asset_archive",
    "qa_agent",
    "compliance_agent",
    "publishing_agent",
    "archive_agent",
    "finance_agent_ledger",
}

# Matches _isolate_pipeline_run_registry in conftest
_FAKE_RUN_ID = "test-run-id-isolated"

# Minimal return values for agents NOT covered by _all_stubs_context
_MINIMAL_QA_REPORT         = {"verdict": "APPROVED", "failures": []}
_MINIMAL_COMPLIANCE_REPORT = {"verdict": "APPROVED", "failures": []}
_MINIMAL_ARCHIVE_RECORD    = {"verdict": "archived", "archived": True}
_MINIMAL_ASSET_ARCHIVE_RECORD = {
    "verdict": "archived",
    "catalog_summary": {"total_assets_indexed": 0},
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_audit_db(tmp_path: Path) -> Path:
    """
    Temp SQLite DB with schema initialized and a seed runs row.

    The seed row satisfies the stage_executions FK constraint so INSERT
    statements succeed when the pipeline's audit helpers write during a test
    run.  run_id matches the constant returned by _isolate_pipeline_run_registry
    in conftest.py.
    """
    db_path = tmp_path / "test_audit.db"
    initialize_schema(db_path=db_path)
    conn = get_connection(db_path=db_path)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status, metadata_version) "
            "VALUES (?, ?, ?, ?, ?)",
            (_FAKE_RUN_ID, "ep_audit", "2026-06-15T00:00:00+00:00", "running", "v2"),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _query_stage_rows(db_path: Path, run_id: str) -> list[dict]:
    """Return all stage_executions rows for run_id as plain dicts."""
    conn = get_connection(db_path=db_path)
    try:
        rows = conn.execute(
            "SELECT stage_name, status, error_message "
            "FROM stage_executions WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _run_isolated(db_path: Path, **pipeline_kwargs):
    """
    Run run_pipeline() with all stubs mocked, audit writes going to db_path.

    Uses channel_id=None to bypass the channel-profile DB lookup.
    Mocks QA/compliance/archive/asset_archive and post-run side-effects so
    the run completes cleanly.  db_path is passed directly to run_pipeline();
    no get_connection patching is required.
    """
    cfg = make_config()
    with patch("hypogeum.pipeline.run_startup_checks", return_value=make_startup_report()):
        with _all_stubs_context():
            with patch("hypogeum.pipeline._run_qa_agent",
                       return_value=_MINIMAL_QA_REPORT):
                with patch("hypogeum.pipeline._run_compliance_agent",
                           return_value=_MINIMAL_COMPLIANCE_REPORT):
                    with patch("hypogeum.pipeline._run_archive_agent",
                               return_value=_MINIMAL_ARCHIVE_RECORD):
                        with patch("hypogeum.pipeline._run_asset_archive",
                                   return_value=_MINIMAL_ASSET_ARCHIVE_RECORD):
                            with patch("hypogeum.pipeline.write_production_metadata",
                                       return_value={}):
                                with patch("hypogeum.pipeline.run_post_run_intelligence"):
                                    return run_pipeline(
                                        topic_input="Test topic",
                                        episode_id="ep_audit",
                                        config=cfg,
                                        channel_id=None,
                                        db_path=db_path,
                                        **pipeline_kwargs,
                                    )


# ---------------------------------------------------------------------------
# TestStageAuditOnSuccess
# ---------------------------------------------------------------------------

class TestStageAuditOnSuccess:
    def test_stage_rows_written_for_all_stages(self, isolated_audit_db):
        """All 18 expected stage_executions rows exist after a successful run."""
        _run_isolated(isolated_audit_db)
        rows = _query_stage_rows(isolated_audit_db, _FAKE_RUN_ID)
        found = {r["stage_name"] for r in rows}
        assert found == _EXPECTED_STAGES

    def test_all_stage_rows_have_completed_status(self, isolated_audit_db):
        """Every row has status='completed' after a successful run."""
        _run_isolated(isolated_audit_db)
        rows = _query_stage_rows(isolated_audit_db, _FAKE_RUN_ID)
        not_completed = [r for r in rows if r["status"] != "completed"]
        assert not_completed == [], f"Rows with non-completed status: {not_completed}"

    def test_stage_row_count_is_18(self, isolated_audit_db):
        """Row count equals 18 (17 pre-existing + Source Enhancement) — no
        duplicates, no missing stages."""
        _run_isolated(isolated_audit_db)
        rows = _query_stage_rows(isolated_audit_db, _FAKE_RUN_ID)
        assert len(rows) == 18


# ---------------------------------------------------------------------------
# TestStageAuditOnFailure
# ---------------------------------------------------------------------------

class TestStageAuditOnFailure:
    def _run_failing_at_rd(self, db_path: Path, error_text: str = "rd boom"):
        """Run with research_director raising RuntimeError."""
        cfg = make_config()
        with patch("hypogeum.pipeline.run_startup_checks", return_value=make_startup_report()):
            with patch("hypogeum.pipeline._run_executive_producer",
                       return_value=(MINIMAL_SCORECARD_PASS, MINIMAL_TOPIC_BRIEF)):
                with patch("hypogeum.pipeline._run_research_director",
                           side_effect=RuntimeError(error_text)):
                    with pytest.raises(RuntimeError, match=error_text):
                        run_pipeline(
                            topic_input="Test",
                            episode_id="ep_audit",
                            config=cfg,
                            channel_id=None,
                            db_path=db_path,
                        )

    def test_failed_stage_recorded_as_failed(self, isolated_audit_db):
        """research_director row has status='failed' when the stage raises."""
        self._run_failing_at_rd(isolated_audit_db)
        rows = _query_stage_rows(isolated_audit_db, _FAKE_RUN_ID)
        rd_rows = [r for r in rows if r["stage_name"] == "research_director"]
        assert len(rd_rows) == 1, f"Expected 1 research_director row, got: {rd_rows}"
        assert rd_rows[0]["status"] == "failed"

    def test_failed_stage_error_message_stored(self, isolated_audit_db):
        """error_message on the failed row contains the exception text."""
        self._run_failing_at_rd(isolated_audit_db, error_text="specific error text")
        rows = _query_stage_rows(isolated_audit_db, _FAKE_RUN_ID)
        rd_rows = [r for r in rows if r["stage_name"] == "research_director"]
        assert "specific error text" in (rd_rows[0]["error_message"] or "")

    def test_stages_before_failure_recorded_as_completed(self, isolated_audit_db):
        """Stages that completed before the failure keep status='completed'."""
        self._run_failing_at_rd(isolated_audit_db)
        rows = _query_stage_rows(isolated_audit_db, _FAKE_RUN_ID)
        by_stage = {r["stage_name"]: r["status"] for r in rows}
        assert by_stage.get("startup_checks") == "completed"
        assert by_stage.get("executive_producer") == "completed"


# ---------------------------------------------------------------------------
# TestAuditWriteFailureIsNonBlocking
# ---------------------------------------------------------------------------

class TestAuditWriteFailureIsNonBlocking:
    """
    The audit helpers must never crash the pipeline.

    In this class, get_connection is patched per-test (not globally) so we
    can drive failure into just the audit path while everything else runs
    normally.  This is a targeted, narrow patch — the opposite of the removed
    global autouse fixture.
    """

    def test_pipeline_completes_when_get_connection_raises(self):
        """Pipeline returns completed result even when every audit DB call throws."""
        cfg = make_config()
        with patch("hypogeum.pipeline.get_connection", side_effect=RuntimeError("db down")):
            with patch("hypogeum.pipeline.run_startup_checks",
                       return_value=make_startup_report()):
                with _all_stubs_context():
                    with patch("hypogeum.pipeline._run_qa_agent",
                               return_value=_MINIMAL_QA_REPORT):
                        with patch("hypogeum.pipeline._run_compliance_agent",
                                   return_value=_MINIMAL_COMPLIANCE_REPORT):
                            with patch("hypogeum.pipeline._run_archive_agent",
                                       return_value=_MINIMAL_ARCHIVE_RECORD):
                                with patch("hypogeum.pipeline._run_asset_archive",
                                           return_value=_MINIMAL_ASSET_ARCHIVE_RECORD):
                                    with patch("hypogeum.pipeline.write_production_metadata",
                                               return_value={}):
                                        with patch("hypogeum.pipeline.run_post_run_intelligence"):
                                            result = run_pipeline(
                                                topic_input="Test",
                                                episode_id="ep_001",
                                                config=cfg,
                                                channel_id=None,
                                            )
        assert result.status == "completed"

    def test_pipeline_result_populated_when_all_audit_writes_fail(self):
        """PipelineResult fields are fully populated even when audit DB is down."""
        cfg = make_config()
        with patch("hypogeum.pipeline.get_connection", side_effect=Exception("no db")):
            with patch("hypogeum.pipeline.run_startup_checks",
                       return_value=make_startup_report()):
                with _all_stubs_context():
                    with patch("hypogeum.pipeline._run_qa_agent",
                               return_value=_MINIMAL_QA_REPORT):
                        with patch("hypogeum.pipeline._run_compliance_agent",
                                   return_value=_MINIMAL_COMPLIANCE_REPORT):
                            with patch("hypogeum.pipeline._run_archive_agent",
                                       return_value=_MINIMAL_ARCHIVE_RECORD):
                                with patch("hypogeum.pipeline._run_asset_archive",
                                           return_value=_MINIMAL_ASSET_ARCHIVE_RECORD):
                                    with patch("hypogeum.pipeline.write_production_metadata",
                                               return_value={}):
                                        with patch("hypogeum.pipeline.run_post_run_intelligence"):
                                            result = run_pipeline(
                                                topic_input="Test",
                                                episode_id="ep_001",
                                                config=cfg,
                                                channel_id=None,
                                            )
        assert result.validation_scorecard is not None
        assert result.research_package is not None
        assert result.publishing_record is not None
