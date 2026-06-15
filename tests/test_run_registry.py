"""
tests/test_run_registry.py — Run Registry + RLG State Machine test suite

Coverage:
    TestRegisterRun      — run_id format, DB row, JSON file, index entry
    TestCompleteRun      — DB update, JSON update, index append
    TestFailRun          — DB update, JSON update, index append
    TestIndexAppend      — append-only semantics, backfill compatibility
    TestRunIdPropagation — run_id returned and usable as FK in DB
    TestPipelineIntegration — PipelineResult.run_id populated
    TestStateMachine     — P3-03: state transitions, validation, history
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.run_registry import (
    InvalidRunTransitionError,
    RUN_STATES,
    complete_run,
    fail_run,
    get_run_state,
    get_run_transition_history,
    pause_run,
    register_run,
    resume_run,
    transition_run_state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_config():
    """Minimal Config-like object with model assignment fields."""
    cfg = MagicMock()
    cfg.EP_MODEL = "anthropic/claude-sonnet-4.6"
    cfg.RD_MODEL = "anthropic/claude-opus-4.6"
    cfg.TD_PLANNING_MODEL = "openai/gpt-5.5-pro"
    cfg.BGA_PASS1_MODEL = "anthropic/claude-sonnet-4.6"
    cfg.BGA_PASS2_MODEL = "openai/gpt-5.5-pro"
    return cfg


@pytest.fixture()
def store(tmp_path):
    """Returns (db_path, runs_dir) tuple using tmp_path."""
    db_path = tmp_path / "hypogeum.db"
    runs_dir = tmp_path / "runs"
    initialize_schema(db_path)
    return db_path, runs_dir


# ---------------------------------------------------------------------------
# TestRegisterRun
# ---------------------------------------------------------------------------

class TestRegisterRun:
    def test_returns_uuid4_string(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        parsed = uuid.UUID(run_id)
        assert parsed.version == 4

    def test_db_row_created(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        conn.close()
        assert row is not None

    def test_db_row_status_is_running(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        conn.close()
        assert row["status"] == "running"

    def test_db_row_episode_id_stored(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT episode_id FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        conn.close()
        assert row["episode_id"] == "ep_000"

    def test_json_file_created(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        json_path = runs_dir / f"{run_id}.json"
        assert json_path.exists()

    def test_json_file_contains_run_id(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        record = json.loads((runs_dir / f"{run_id}.json").read_text())
        assert record["run_id"] == run_id

    def test_json_file_status_is_running(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        record = json.loads((runs_dir / f"{run_id}.json").read_text())
        assert record["status"] == "running"

    def test_json_file_contains_model_assignments(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        record = json.loads((runs_dir / f"{run_id}.json").read_text())
        assert "model_assignments" in record
        assert record["model_assignments"]["rd"] == "anthropic/claude-opus-4.6"

    def test_json_file_metadata_version(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        record = json.loads((runs_dir / f"{run_id}.json").read_text())
        assert record["metadata_version"] == "v1"

    def test_index_entry_created(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        index_path = runs_dir / "index.jsonl"
        assert index_path.exists()
        lines = index_path.read_text().strip().splitlines()
        assert len(lines) >= 1
        entry = json.loads(lines[0])
        assert entry["run_id"] == run_id
        assert entry["status"] == "running"

    def test_different_calls_produce_different_run_ids(self, store):
        db_path, runs_dir = store
        cfg = _make_config()
        id1 = register_run("ep_000", cfg, db_path=db_path, runs_dir=runs_dir)
        id2 = register_run("ep_000", cfg, db_path=db_path, runs_dir=runs_dir)
        assert id1 != id2

    def test_initializes_schema_if_db_absent(self, tmp_path):
        db_path = tmp_path / "new" / "hypogeum.db"
        runs_dir = tmp_path / "runs"
        # Must not raise even without prior initialize_schema()
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        assert run_id


# ---------------------------------------------------------------------------
# TestCompleteRun
# ---------------------------------------------------------------------------

class TestCompleteRun:
    def test_db_status_updated_to_completed(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        complete_run(run_id, "ep_000", db_path=db_path, runs_dir=runs_dir)
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        conn.close()
        assert row["status"] == "completed"

    def test_db_completed_at_set(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        complete_run(run_id, "ep_000", db_path=db_path, runs_dir=runs_dir)
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT completed_at FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        conn.close()
        assert row["completed_at"] is not None

    def test_json_status_updated(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        complete_run(run_id, "ep_000", db_path=db_path, runs_dir=runs_dir)
        record = json.loads((runs_dir / f"{run_id}.json").read_text())
        assert record["status"] == "completed"

    def test_index_appended_with_completed(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        complete_run(run_id, "ep_000", db_path=db_path, runs_dir=runs_dir)
        lines = (runs_dir / "index.jsonl").read_text().strip().splitlines()
        statuses = [json.loads(l)["status"] for l in lines]
        assert "completed" in statuses


# ---------------------------------------------------------------------------
# TestFailRun
# ---------------------------------------------------------------------------

class TestFailRun:
    def test_db_status_updated_to_failed(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        fail_run(run_id, "ep_000", "Something broke", db_path=db_path, runs_dir=runs_dir)
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status, error_message FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        conn.close()
        assert row["status"] == "failed"
        assert "Something broke" in row["error_message"]

    def test_json_status_updated_to_failed(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        fail_run(run_id, "ep_000", "Error occurred", db_path=db_path, runs_dir=runs_dir)
        record = json.loads((runs_dir / f"{run_id}.json").read_text())
        assert record["status"] == "failed"
        assert "Error occurred" in record["error_message"]

    def test_index_appended_with_failed(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        fail_run(run_id, "ep_000", "Crash", db_path=db_path, runs_dir=runs_dir)
        lines = (runs_dir / "index.jsonl").read_text().strip().splitlines()
        statuses = [json.loads(l)["status"] for l in lines]
        assert "failed" in statuses

    def test_error_truncated_in_json(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        long_error = "e" * 5000
        fail_run(run_id, "ep_000", long_error, db_path=db_path, runs_dir=runs_dir)
        record = json.loads((runs_dir / f"{run_id}.json").read_text())
        assert len(record["error_message"]) == 4096


# ---------------------------------------------------------------------------
# TestIndexAppend
# ---------------------------------------------------------------------------

class TestIndexAppend:
    def test_index_is_append_only(self, store):
        db_path, runs_dir = store
        cfg = _make_config()
        for ep in ("ep_000", "ep_001", "ep_002"):
            run_id = register_run(ep, cfg, db_path=db_path, runs_dir=runs_dir)
            complete_run(run_id, ep, db_path=db_path, runs_dir=runs_dir)

        lines = (runs_dir / "index.jsonl").read_text().strip().splitlines()
        # 3 register + 3 complete = 6 entries
        assert len(lines) == 6

    def test_index_entries_are_valid_json(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        complete_run(run_id, "ep_000", db_path=db_path, runs_dir=runs_dir)
        lines = (runs_dir / "index.jsonl").read_text().strip().splitlines()
        for line in lines:
            entry = json.loads(line)
            assert "run_id" in entry
            assert "episode_id" in entry
            assert "status" in entry
            assert "recorded_at" in entry

    def test_index_backfill_compatible(self, store):
        """Each line is a self-contained JSON object — safe to replay in order."""
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        fail_run(run_id, "ep_000", "Crash", db_path=db_path, runs_dir=runs_dir)
        lines = (runs_dir / "index.jsonl").read_text().strip().splitlines()
        entries = [json.loads(l) for l in lines]
        assert entries[0]["status"] == "running"
        assert entries[1]["status"] == "failed"
        # Both reference the same run_id
        assert entries[0]["run_id"] == entries[1]["run_id"]


# ---------------------------------------------------------------------------
# TestRunIdPropagation
# ---------------------------------------------------------------------------

class TestRunIdPropagation:
    def test_run_id_usable_as_fk(self, store):
        """run_id returned by register_run must be a valid FK for assets table."""
        from hypogeum.db import insert_asset
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)

        conn = get_connection(db_path)
        insert_asset(
            conn, "asset-test-001", run_id, "ep_000",
            "thumbnail", "muapi", "2026-06-12T10:00:00Z",
        )
        conn.commit()
        row = conn.execute(
            "SELECT asset_id FROM assets WHERE run_id=?", (run_id,)
        ).fetchone()
        conn.close()
        assert row["asset_id"] == "asset-test-001"

    def test_run_id_in_json_matches_db(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        record = json.loads((runs_dir / f"{run_id}.json").read_text())

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT run_id FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        conn.close()

        assert record["run_id"] == row["run_id"] == run_id


# ---------------------------------------------------------------------------
# TestPipelineIntegration
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    def test_pipeline_result_has_run_id_field(self):
        """PipelineResult dataclass must have run_id field."""
        from hypogeum.pipeline import PipelineResult
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(PipelineResult)}
        assert "run_id" in field_names

    def test_run_id_field_default_is_none(self):
        """run_id must be optional (default None) for backward compatibility."""
        from hypogeum.pipeline import PipelineResult
        import dataclasses
        fields = {f.name: f for f in dataclasses.fields(PipelineResult)}
        assert fields["run_id"].default is None


# ---------------------------------------------------------------------------
# TestStateMachine — P3-03: Run Lifecycle Governance
# ---------------------------------------------------------------------------

class TestStateMachine:
    """
    All tests use isolated tmp_path DBs. No production DB is accessed.
    The store fixture provides (db_path, runs_dir).
    """

    # ── helpers ────────────────────────────────────────────────────────────

    def _register(self, store) -> tuple[str, Path, Path]:
        db_path, runs_dir = store
        run_id = register_run(
            "ep_sm_001", _make_config(),
            db_path=db_path, runs_dir=runs_dir,
        )
        return run_id, db_path, runs_dir

    # ── state constants ────────────────────────────────────────────────────

    def test_run_states_set_is_correct(self):
        expected = {
            "queued", "running", "paused",
            "awaiting_review", "awaiting_publish_approval",
            "publishing", "completed", "failed", "blocked",
        }
        assert RUN_STATES == expected

    def test_invalid_run_transition_error_is_runtime_error(self):
        assert issubclass(InvalidRunTransitionError, RuntimeError)

    def test_invalid_transition_error_attributes(self, store):
        run_id, db_path, runs_dir = self._register(store)
        try:
            transition_run_state(run_id, "queued", "system:pipeline", "test",
                                 db_path=db_path)
        except InvalidRunTransitionError as exc:
            assert exc.run_id == run_id
            assert exc.from_state == "running"
            assert exc.to_state == "queued"
        else:
            pytest.fail("Expected InvalidRunTransitionError")

    # ── transition record writing ──────────────────────────────────────────

    def test_register_run_creates_initial_transition(self, store):
        """register_run records None → running in run_state_transitions."""
        run_id, db_path, _ = self._register(store)
        history = get_run_transition_history(run_id, db_path=db_path)
        assert len(history) == 1
        t = history[0]
        assert t["from_state"] is None
        assert t["to_state"] == "running"
        assert t["triggered_by"] == "system:pipeline"

    def test_valid_transition_recorded(self, store):
        """queued → running → completed produces 3 rows in run_state_transitions."""
        db_path, runs_dir = store
        # Seed a run directly in queued state to test the queued → running path
        import uuid as _uuid
        from hypogeum.db import insert_run, insert_run_state_transition
        run_id = str(_uuid.uuid4())
        conn = get_connection(db_path)
        try:
            insert_run(conn, run_id=run_id, episode_id="ep_sm_q",
                       started_at=_make_config().EP_MODEL or "2026-01-01T00:00:00Z",
                       model_assignments={})
            conn.execute("UPDATE runs SET status = 'queued' WHERE run_id = ?", (run_id,))
            insert_run_state_transition(
                conn, transition_id=str(_uuid.uuid4()), run_id=run_id,
                from_state=None, to_state="queued",
                triggered_by="system:pipeline", trigger_source="test",
                transitioned_at="2026-01-01T00:00:00+00:00",
            )
            conn.commit()
        finally:
            conn.close()

        # queued → running
        transition_run_state(run_id, "running", "system:pipeline", "test",
                             db_path=db_path)
        # running → completed
        transition_run_state(run_id, "completed", "system:pipeline", "test",
                             db_path=db_path)

        history = get_run_transition_history(run_id, db_path=db_path)
        assert len(history) == 3
        states = [(r["from_state"], r["to_state"]) for r in history]
        assert states[0] == (None, "queued")
        assert states[1] == ("queued", "running")
        assert states[2] == ("running", "completed")

    def test_invalid_transition_raises(self, store):
        """completed → running raises InvalidRunTransitionError."""
        run_id, db_path, runs_dir = self._register(store)
        complete_run(run_id, "ep_sm_001", db_path=db_path, runs_dir=runs_dir)
        with pytest.raises(InvalidRunTransitionError):
            transition_run_state(run_id, "running", "operator", "test",
                                 db_path=db_path)

    def test_transition_record_is_immutable(self, store):
        """The API exposes no update path for run_state_transitions rows."""
        import hypogeum.db as db_module
        import hypogeum.run_registry as rr_module
        assert not hasattr(db_module, "update_run_state_transition"), (
            "update_run_state_transition must not exist — rows are immutable"
        )
        assert not hasattr(rr_module, "update_run_state_transition"), (
            "No update path should exist in run_registry either"
        )

    # ── get_run_state ──────────────────────────────────────────────────────

    def test_get_run_state_returns_running_after_register(self, store):
        run_id, db_path, _ = self._register(store)
        assert get_run_state(run_id, db_path=db_path) == "running"

    def test_get_run_state_returns_none_for_unknown_run(self, store):
        db_path, _ = store
        assert get_run_state("ghost-run-id", db_path=db_path) is None

    # ── running → completed ────────────────────────────────────────────────

    def test_running_to_completed_via_complete_run(self, store):
        run_id, db_path, runs_dir = self._register(store)
        complete_run(run_id, "ep_sm_001", db_path=db_path, runs_dir=runs_dir)
        assert get_run_state(run_id, db_path=db_path) == "completed"

    def test_running_to_completed_records_transition(self, store):
        run_id, db_path, runs_dir = self._register(store)
        complete_run(run_id, "ep_sm_001", db_path=db_path, runs_dir=runs_dir)
        history = get_run_transition_history(run_id, db_path=db_path)
        completed = [t for t in history if t["to_state"] == "completed"]
        assert len(completed) == 1
        assert completed[0]["from_state"] == "running"

    # ── running → failed ───────────────────────────────────────────────────

    def test_running_to_failed_via_fail_run(self, store):
        run_id, db_path, runs_dir = self._register(store)
        fail_run(run_id, "ep_sm_001", "boom", db_path=db_path, runs_dir=runs_dir)
        assert get_run_state(run_id, db_path=db_path) == "failed"

    def test_running_to_failed_records_transition(self, store):
        run_id, db_path, runs_dir = self._register(store)
        fail_run(run_id, "ep_sm_001", "boom", db_path=db_path, runs_dir=runs_dir)
        history = get_run_transition_history(run_id, db_path=db_path)
        failed = [t for t in history if t["to_state"] == "failed"]
        assert len(failed) == 1
        assert failed[0]["from_state"] == "running"

    # ── terminal states ────────────────────────────────────────────────────

    def test_completed_is_terminal(self, store):
        """No transition out of completed is allowed."""
        run_id, db_path, runs_dir = self._register(store)
        complete_run(run_id, "ep_sm_001", db_path=db_path, runs_dir=runs_dir)
        for target in ("running", "failed", "paused", "blocked", "queued"):
            with pytest.raises(InvalidRunTransitionError):
                transition_run_state(run_id, target, "system:pipeline", "test",
                                     db_path=db_path)

    def test_failed_is_terminal(self, store):
        """No transition out of failed is allowed."""
        run_id, db_path, runs_dir = self._register(store)
        fail_run(run_id, "ep_sm_001", "err", db_path=db_path, runs_dir=runs_dir)
        for target in ("running", "completed", "paused", "blocked"):
            with pytest.raises(InvalidRunTransitionError):
                transition_run_state(run_id, target, "system:pipeline", "test",
                                     db_path=db_path)

    # ── pause / resume ─────────────────────────────────────────────────────

    def test_pause_run_transitions_to_paused(self, store):
        run_id, db_path, _ = self._register(store)
        pause_run(run_id, "operator_user_001", "investigating", db_path=db_path)
        assert get_run_state(run_id, db_path=db_path) == "paused"

    def test_pause_sets_paused_at(self, store):
        run_id, db_path, _ = self._register(store)
        pause_run(run_id, "operator_user_001", "investigating", db_path=db_path)
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT paused_at FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        conn.close()
        assert row["paused_at"] is not None

    def test_resume_run_transitions_to_running(self, store):
        run_id, db_path, _ = self._register(store)
        pause_run(run_id, "operator_user_001", "investigating", db_path=db_path)
        resume_run(run_id, "operator_user_001", db_path=db_path)
        assert get_run_state(run_id, db_path=db_path) == "running"

    def test_pause_then_resume_history(self, store):
        run_id, db_path, _ = self._register(store)
        pause_run(run_id, "op_1", "check", db_path=db_path)
        resume_run(run_id, "op_1", db_path=db_path)
        history = get_run_transition_history(run_id, db_path=db_path)
        states = [t["to_state"] for t in history]
        assert "paused" in states
        assert states[-1] == "running"

    # ── approval-related states (exist, no P3-04 required) ─────────────────

    def test_awaiting_review_is_valid_state(self):
        assert "awaiting_review" in RUN_STATES

    def test_awaiting_publish_approval_is_valid_state(self):
        assert "awaiting_publish_approval" in RUN_STATES

    def test_running_can_transition_to_awaiting_review(self, store):
        run_id, db_path, _ = self._register(store)
        transition_run_state(
            run_id, "awaiting_review",
            "system:pipeline", "pipeline",
            db_path=db_path,
        )
        assert get_run_state(run_id, db_path=db_path) == "awaiting_review"

    def test_running_can_transition_to_awaiting_publish_approval(self, store):
        run_id, db_path, _ = self._register(store)
        transition_run_state(
            run_id, "awaiting_publish_approval",
            "system:pipeline", "pipeline",
            db_path=db_path,
        )
        assert get_run_state(run_id, db_path=db_path) == "awaiting_publish_approval"

    def test_awaiting_review_advances_to_awaiting_publish_approval(self, store):
        run_id, db_path, _ = self._register(store)
        transition_run_state(run_id, "awaiting_review",
                             "system:pipeline", "pipeline", db_path=db_path)
        transition_run_state(run_id, "awaiting_publish_approval",
                             "system:approvals", "approval_system", db_path=db_path)
        assert get_run_state(run_id, db_path=db_path) == "awaiting_publish_approval"

    # ── OIS signal emission ────────────────────────────────────────────────

    def test_transition_emits_ois_signal_on_completed(self, store):
        """complete_run emits run_completed signal via OIS."""
        run_id, db_path, runs_dir = self._register(store)
        with patch("hypogeum.run_registry._emit_ois_signal_nonblocking") as mock_emit:
            complete_run(run_id, "ep_sm_001", db_path=db_path, runs_dir=runs_dir)
        mock_emit.assert_called_once()
        args = mock_emit.call_args
        assert args.kwargs["to_state"] == "completed"

    def test_transition_emits_ois_signal_on_failed(self, store):
        """fail_run emits run_failed signal via OIS."""
        run_id, db_path, runs_dir = self._register(store)
        with patch("hypogeum.run_registry._emit_ois_signal_nonblocking") as mock_emit:
            fail_run(run_id, "ep_sm_001", "error", db_path=db_path, runs_dir=runs_dir)
        mock_emit.assert_called_once()
        args = mock_emit.call_args
        assert args.kwargs["to_state"] == "failed"

    # ── backward compat ────────────────────────────────────────────────────

    def test_existing_register_run_still_works(self, store):
        """register_run() backwards-compat: returns a run_id, DB row exists."""
        db_path, runs_dir = store
        run_id = register_run(
            "ep_compat", _make_config(),
            db_path=db_path, runs_dir=runs_dir,
        )
        assert run_id
        assert get_run_state(run_id, db_path=db_path) == "running"

    def test_existing_complete_run_still_works(self, store):
        run_id, db_path, runs_dir = self._register(store)
        complete_run(run_id, "ep_sm_001", db_path=db_path, runs_dir=runs_dir)
        # Should not raise; run should be completed
        assert get_run_state(run_id, db_path=db_path) == "completed"

    def test_existing_fail_run_still_works(self, store):
        run_id, db_path, runs_dir = self._register(store)
        fail_run(run_id, "ep_sm_001", "test error", db_path=db_path, runs_dir=runs_dir)
        assert get_run_state(run_id, db_path=db_path) == "failed"

    # ── no production DB access ────────────────────────────────────────────

    def test_no_production_db_access(self, store, tmp_path):
        """All state machine operations use the provided db_path."""
        run_id, db_path, _ = self._register(store)
        # Verify the run exists only in the test DB, not the default path
        prod_path = Path(__file__).resolve().parent.parent / ".hypogeum-assets" / "hypogeum.db"
        if prod_path.exists():
            prod_conn = get_connection(prod_path)
            row = prod_conn.execute(
                "SELECT run_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            prod_conn.close()
            assert row is None, "run_id must not appear in production DB"
