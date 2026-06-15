"""
tests/test_db.py — Operational database test suite

Coverage:
    TestSchemaFile         — schema.sql exists and is readable
    TestGetConnection      — FK enforcement, WAL mode, row factory
    TestInitializeSchema   — all tables created, idempotency
    TestInsertRun          — insert + status transitions
    TestInsertEpisode      — insert + replace semantics
    TestInsertAsset        — insert + FK enforcement
    TestInsertCost         — insert + FK enforcement
    TestInsertThumbnailCandidate — insert + selected flag
    TestInsertTitle        — insert + selected flag
    TestForeignKeyEnforcement — FK violations raise IntegrityError
    TestDefaultPath        — default DB path is under .hypogeum-assets/
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hypogeum.db import (
    _DEFAULT_DB_PATH,
    _SCHEMA_PATH,
    get_connection,
    initialize_schema,
    insert_asset,
    insert_cost,
    insert_episode,
    insert_run,
    insert_thumbnail_candidate,
    insert_title,
    update_run_complete,
    update_run_failed,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """Initialized in-memory-equivalent DB in tmp_path. Returns open conn."""
    db_path = tmp_path / "test_hypogeum.db"
    initialize_schema(db_path)
    conn = get_connection(db_path)
    yield conn
    conn.close()


@pytest.fixture()
def db_with_run(db):
    """DB with one 'running' run already inserted."""
    insert_run(db, "run-001", "ep_000", "2026-06-12T10:00:00Z")
    db.commit()
    return db


_STARTED = "2026-06-12T10:00:00Z"
_COMPLETED = "2026-06-12T12:00:00Z"


# ---------------------------------------------------------------------------
# TestSchemaFile
# ---------------------------------------------------------------------------

class TestSchemaFile:
    def test_schema_sql_exists(self):
        assert _SCHEMA_PATH.exists(), f"schema.sql not found at {_SCHEMA_PATH}"

    def test_schema_sql_is_readable(self):
        content = _SCHEMA_PATH.read_text(encoding="utf-8")
        assert len(content) > 0

    def test_schema_sql_contains_all_tables(self):
        content = _SCHEMA_PATH.read_text(encoding="utf-8")
        for table in ("runs", "episodes", "assets", "costs",
                      "thumbnail_candidates", "titles"):
            assert f"CREATE TABLE IF NOT EXISTS {table}" in content


# ---------------------------------------------------------------------------
# TestGetConnection
# ---------------------------------------------------------------------------

class TestGetConnection:
    def test_returns_connection(self, tmp_path):
        conn = get_connection(tmp_path / "test.db")
        assert isinstance(conn, sqlite3.Connection)
        conn.close()

    def test_foreign_keys_enabled(self, tmp_path):
        conn = get_connection(tmp_path / "test.db")
        row = conn.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1
        conn.close()

    def test_wal_mode(self, tmp_path):
        conn = get_connection(tmp_path / "test.db")
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"
        conn.close()

    def test_row_factory_is_row(self, tmp_path):
        conn = get_connection(tmp_path / "test.db")
        assert conn.row_factory == sqlite3.Row
        conn.close()

    def test_creates_parent_directories(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c" / "test.db"
        conn = get_connection(nested)
        assert nested.parent.exists()
        conn.close()


# ---------------------------------------------------------------------------
# TestInitializeSchema
# ---------------------------------------------------------------------------

class TestInitializeSchema:
    def test_creates_all_tables(self, tmp_path):
        db_path = tmp_path / "hypogeum.db"
        initialize_schema(db_path)
        conn = get_connection(db_path)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r["name"] for r in rows}
        for table in ("runs", "episodes", "assets", "costs",
                      "thumbnail_candidates", "titles"):
            assert table in table_names, f"Table '{table}' missing"
        conn.close()

    def test_is_idempotent(self, tmp_path):
        db_path = tmp_path / "hypogeum.db"
        initialize_schema(db_path)
        initialize_schema(db_path)  # must not raise
        initialize_schema(db_path)

    def test_existing_data_preserved_on_reinit(self, tmp_path):
        db_path = tmp_path / "hypogeum.db"
        initialize_schema(db_path)
        conn = get_connection(db_path)
        insert_run(conn, "run-keep", "ep_000", _STARTED)
        conn.commit()
        conn.close()

        initialize_schema(db_path)  # reinit must not drop data

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT run_id FROM runs WHERE run_id='run-keep'"
        ).fetchone()
        assert row is not None
        conn.close()

    def test_creates_database_file(self, tmp_path):
        db_path = tmp_path / "new_dir" / "hypogeum.db"
        initialize_schema(db_path)
        assert db_path.exists()


# ---------------------------------------------------------------------------
# TestInsertRun
# ---------------------------------------------------------------------------

class TestInsertRun:
    def test_insert_creates_row(self, db):
        insert_run(db, "run-001", "ep_000", _STARTED)
        db.commit()
        row = db.execute("SELECT * FROM runs WHERE run_id='run-001'").fetchone()
        assert row is not None

    def test_initial_status_is_running(self, db):
        insert_run(db, "run-001", "ep_000", _STARTED)
        db.commit()
        row = db.execute("SELECT status FROM runs WHERE run_id='run-001'").fetchone()
        assert row["status"] == "running"

    def test_episode_id_stored(self, db):
        insert_run(db, "run-001", "ep_000", _STARTED)
        db.commit()
        row = db.execute("SELECT episode_id FROM runs WHERE run_id='run-001'").fetchone()
        assert row["episode_id"] == "ep_000"

    def test_model_assignments_stored_as_json(self, db):
        assignments = {"ep": "claude-sonnet-4.6", "rd": "claude-opus-4.6"}
        insert_run(db, "run-001", "ep_000", _STARTED, model_assignments=assignments)
        db.commit()
        row = db.execute("SELECT model_assignments FROM runs WHERE run_id='run-001'").fetchone()
        parsed = json.loads(row["model_assignments"])
        assert parsed == assignments

    def test_metadata_version_stored(self, db):
        insert_run(db, "run-001", "ep_000", _STARTED)
        db.commit()
        row = db.execute("SELECT metadata_version FROM runs WHERE run_id='run-001'").fetchone()
        assert row["metadata_version"] == "v1"

    def test_update_run_complete(self, db_with_run):
        update_run_complete(db_with_run, "run-001", _COMPLETED, exit_code=0)
        db_with_run.commit()
        row = db_with_run.execute(
            "SELECT status, exit_code, completed_at FROM runs WHERE run_id='run-001'"
        ).fetchone()
        assert row["status"] == "complete"
        assert row["exit_code"] == 0
        assert row["completed_at"] == _COMPLETED

    def test_update_run_failed(self, db_with_run):
        update_run_failed(db_with_run, "run-001", _COMPLETED, "Something broke")
        db_with_run.commit()
        row = db_with_run.execute(
            "SELECT status, exit_code, error_message FROM runs WHERE run_id='run-001'"
        ).fetchone()
        assert row["status"] == "failed"
        assert row["exit_code"] == 1
        assert row["error_message"] == "Something broke"

    def test_error_message_truncated_at_4096(self, db_with_run):
        long_error = "x" * 5000
        update_run_failed(db_with_run, "run-001", _COMPLETED, long_error)
        db_with_run.commit()
        row = db_with_run.execute(
            "SELECT error_message FROM runs WHERE run_id='run-001'"
        ).fetchone()
        assert len(row["error_message"]) == 4096

    def test_duplicate_run_id_raises(self, db_with_run):
        with pytest.raises(sqlite3.IntegrityError):
            insert_run(db_with_run, "run-001", "ep_001", _STARTED)
            db_with_run.commit()


# ---------------------------------------------------------------------------
# TestInsertEpisode
# ---------------------------------------------------------------------------

class TestInsertEpisode:
    def test_insert_creates_row(self, db_with_run):
        insert_episode(db_with_run, "ep_000", _STARTED, run_id="run-001")
        db_with_run.commit()
        row = db_with_run.execute(
            "SELECT * FROM episodes WHERE episode_id='ep_000'"
        ).fetchone()
        assert row is not None

    def test_title_and_topic_stored(self, db_with_run):
        insert_episode(
            db_with_run, "ep_000", _STARTED,
            title="The Computer That Vanished",
            topic="Antikythera Mechanism",
            run_id="run-001",
        )
        db_with_run.commit()
        row = db_with_run.execute(
            "SELECT title, topic FROM episodes WHERE episode_id='ep_000'"
        ).fetchone()
        assert row["title"] == "The Computer That Vanished"
        assert row["topic"] == "Antikythera Mechanism"

    def test_replace_updates_existing(self, db_with_run):
        insert_episode(db_with_run, "ep_000", _STARTED, run_id="run-001")
        db_with_run.commit()
        insert_episode(
            db_with_run, "ep_000", _STARTED,
            title="Updated Title", run_id="run-001",
        )
        db_with_run.commit()
        rows = db_with_run.execute(
            "SELECT title FROM episodes WHERE episode_id='ep_000'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["title"] == "Updated Title"

    def test_default_status_is_in_progress(self, db_with_run):
        insert_episode(db_with_run, "ep_000", _STARTED)
        db_with_run.commit()
        row = db_with_run.execute(
            "SELECT status FROM episodes WHERE episode_id='ep_000'"
        ).fetchone()
        assert row["status"] == "in_progress"


# ---------------------------------------------------------------------------
# TestInsertAsset
# ---------------------------------------------------------------------------

class TestInsertAsset:
    def test_insert_creates_row(self, db_with_run):
        insert_asset(
            db_with_run, "asset-abc123", "run-001", "ep_000",
            "video_clip", "muapi", _STARTED,
        )
        db_with_run.commit()
        row = db_with_run.execute(
            "SELECT * FROM assets WHERE asset_id='asset-abc123'"
        ).fetchone()
        assert row is not None

    def test_all_fields_stored(self, db_with_run):
        insert_asset(
            db_with_run, "asset-abc123", "run-001", "ep_000",
            "thumbnail", "muapi", _STARTED,
            prompt_hash="abc123hash",
            file_path="episodes/ep_000/thumbnails/main.jpg",
        )
        db_with_run.commit()
        row = db_with_run.execute(
            "SELECT * FROM assets WHERE asset_id='asset-abc123'"
        ).fetchone()
        assert row["asset_type"] == "thumbnail"
        assert row["provider"] == "muapi"
        assert row["prompt_hash"] == "abc123hash"
        assert row["file_path"] == "episodes/ep_000/thumbnails/main.jpg"


# ---------------------------------------------------------------------------
# TestInsertCost
# ---------------------------------------------------------------------------

class TestInsertCost:
    def test_insert_creates_row(self, db_with_run):
        insert_cost(
            db_with_run, "cost-001", "run-001", "ep_000",
            "openrouter", "research_synthesis", 0.1446, _STARTED,
            model="anthropic/claude-opus-4.6",
            tokens_or_credits=4821.0,
        )
        db_with_run.commit()
        row = db_with_run.execute(
            "SELECT * FROM costs WHERE cost_id='cost-001'"
        ).fetchone()
        assert row is not None

    def test_cost_usd_stored_correctly(self, db_with_run):
        insert_cost(
            db_with_run, "cost-001", "run-001", "ep_000",
            "muapi", "scene_generation", 0.15, _STARTED,
        )
        db_with_run.commit()
        row = db_with_run.execute(
            "SELECT cost_usd FROM costs WHERE cost_id='cost-001'"
        ).fetchone()
        assert abs(row["cost_usd"] - 0.15) < 1e-9

    def test_episode_cost_aggregate(self, db_with_run):
        for i, cost in enumerate([0.10, 0.20, 0.30]):
            insert_cost(
                db_with_run, f"cost-{i:03d}", "run-001", "ep_000",
                "openrouter", "planning", cost, _STARTED,
            )
        db_with_run.commit()
        row = db_with_run.execute(
            "SELECT SUM(cost_usd) as total FROM costs WHERE episode_id='ep_000'"
        ).fetchone()
        assert abs(row["total"] - 0.60) < 1e-9


# ---------------------------------------------------------------------------
# TestInsertThumbnailCandidate
# ---------------------------------------------------------------------------

class TestInsertThumbnailCandidate:
    def test_insert_creates_row(self, db_with_run):
        insert_thumbnail_candidate(
            db_with_run, "tc-abc", "run-001", "ep_000", rank=1,
        )
        db_with_run.commit()
        row = db_with_run.execute(
            "SELECT * FROM thumbnail_candidates WHERE candidate_id='tc-abc'"
        ).fetchone()
        assert row is not None

    def test_selected_stored_as_integer(self, db_with_run):
        insert_thumbnail_candidate(
            db_with_run, "tc-001", "run-001", "ep_000", rank=1, selected=True,
        )
        insert_thumbnail_candidate(
            db_with_run, "tc-002", "run-001", "ep_000", rank=2, selected=False,
        )
        db_with_run.commit()
        r1 = db_with_run.execute(
            "SELECT selected FROM thumbnail_candidates WHERE candidate_id='tc-001'"
        ).fetchone()
        r2 = db_with_run.execute(
            "SELECT selected FROM thumbnail_candidates WHERE candidate_id='tc-002'"
        ).fetchone()
        assert r1["selected"] == 1
        assert r2["selected"] == 0

    def test_all_fields_stored(self, db_with_run):
        insert_thumbnail_candidate(
            db_with_run, "tc-001", "run-001", "ep_000", rank=1,
            text_overlay="THE COMPUTER THAT VANISHED",
            placement="top",
            visual_prompt="Corroded bronze gears",
        )
        db_with_run.commit()
        row = db_with_run.execute(
            "SELECT * FROM thumbnail_candidates WHERE candidate_id='tc-001'"
        ).fetchone()
        assert row["text_overlay"] == "THE COMPUTER THAT VANISHED"
        assert row["placement"] == "top"
        assert row["visual_prompt"] == "Corroded bronze gears"
        assert row["rank"] == 1


# ---------------------------------------------------------------------------
# TestInsertTitle
# ---------------------------------------------------------------------------

class TestInsertTitle:
    def test_insert_creates_row(self, db_with_run):
        insert_title(
            db_with_run, "title-001", "run-001", "ep_000",
            "The Computer That Vanished", _STARTED,
        )
        db_with_run.commit()
        row = db_with_run.execute(
            "SELECT * FROM titles WHERE title_id='title-001'"
        ).fetchone()
        assert row is not None

    def test_title_text_stored(self, db_with_run):
        insert_title(
            db_with_run, "title-001", "run-001", "ep_000",
            "The Computer That Vanished", _STARTED, selected=True,
        )
        db_with_run.commit()
        row = db_with_run.execute(
            "SELECT title_text, selected FROM titles WHERE title_id='title-001'"
        ).fetchone()
        assert row["title_text"] == "The Computer That Vanished"
        assert row["selected"] == 1


# ---------------------------------------------------------------------------
# TestForeignKeyEnforcement
# ---------------------------------------------------------------------------

class TestForeignKeyEnforcement:
    def test_asset_fk_violation_raises(self, db):
        """Inserting an asset referencing a non-existent run_id must fail."""
        with pytest.raises(sqlite3.IntegrityError):
            insert_asset(
                db, "asset-orphan", "run-nonexistent", "ep_000",
                "video_clip", "muapi", _STARTED,
            )
            db.commit()

    def test_cost_fk_violation_raises(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            insert_cost(
                db, "cost-orphan", "run-nonexistent", "ep_000",
                "openrouter", "synthesis", 0.10, _STARTED,
            )
            db.commit()

    def test_thumbnail_candidate_fk_violation_raises(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            insert_thumbnail_candidate(
                db, "tc-orphan", "run-nonexistent", "ep_000", rank=1,
            )
            db.commit()

    def test_title_fk_violation_raises(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            insert_title(
                db, "title-orphan", "run-nonexistent", "ep_000",
                "Some Title", _STARTED,
            )
            db.commit()

    def test_valid_fk_does_not_raise(self, db_with_run):
        """Positive case: FK to existing run_id must succeed."""
        insert_asset(
            db_with_run, "asset-valid", "run-001", "ep_000",
            "thumbnail", "muapi", _STARTED,
        )
        db_with_run.commit()


# ---------------------------------------------------------------------------
# TestDefaultPath
# ---------------------------------------------------------------------------

class TestDefaultPath:
    def test_default_db_path_under_hypogeum_assets(self):
        assert ".hypogeum-assets" in str(_DEFAULT_DB_PATH)
        assert _DEFAULT_DB_PATH.name == "hypogeum.db"

    def test_schema_path_in_hypogeum_package(self):
        assert _SCHEMA_PATH.name == "schema.sql"
        assert _SCHEMA_PATH.parent.name == "hypogeum"
