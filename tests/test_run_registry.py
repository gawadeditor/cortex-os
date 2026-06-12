"""
tests/test_run_registry.py — Run Registry test suite

Coverage:
    TestRegisterRun     — run_id format, DB row, JSON file, index entry
    TestCompleteRun     — DB update, JSON update, index append
    TestFailRun         — DB update, JSON update, index append
    TestIndexAppend     — append-only semantics, backfill compatibility
    TestRunIdPropagation — run_id returned and usable as FK in DB
    TestPipelineIntegration — PipelineResult.run_id populated
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.run_registry import complete_run, fail_run, register_run


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_config():
    """Minimal Config-like object with model assignment fields."""
    cfg = MagicMock()
    cfg.EP_MODEL = "anthropic/claude-fable-5"
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
    def test_db_status_updated_to_complete(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        complete_run(run_id, "ep_000", db_path=db_path, runs_dir=runs_dir)
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        conn.close()
        assert row["status"] == "complete"

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
        assert record["status"] == "complete"

    def test_index_appended_with_complete(self, store):
        db_path, runs_dir = store
        run_id = register_run("ep_000", _make_config(), db_path=db_path, runs_dir=runs_dir)
        complete_run(run_id, "ep_000", db_path=db_path, runs_dir=runs_dir)
        lines = (runs_dir / "index.jsonl").read_text().strip().splitlines()
        statuses = [json.loads(l)["status"] for l in lines]
        assert "complete" in statuses


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
