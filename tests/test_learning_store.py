"""
tests/test_learning_store.py — Phase 5C: Learning Input Store

Coverage:
    TestSchemaTablesExist       — kg_seeds and signal_records tables created
    TestInsertKgSeed            — insert_kg_seed writes and reads correctly
    TestInsertKgSeedIdempotent  — INSERT OR REPLACE updates existing row
    TestInsertKgSeedFullRecord  — full_record JSON round-trips correctly
    TestInsertSignalRecord      — insert_signal_record writes and reads correctly
    TestInsertSignalIdempotent  — INSERT OR REPLACE updates existing row
    TestInsertSignalFullRecord  — full_record JSON round-trips correctly
    TestSignalDataQuality       — data_quality stored correctly for all 3 values
    TestKgSeedCounts            — entity/relationship/source counts stored
    TestSchemaIdempotency       — initialize_schema safe to call twice
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hypogeum.db import (
    get_connection,
    initialize_schema,
    insert_kg_seed,
    insert_signal_record,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    initialize_schema(db_path=db_path)
    return get_connection(db_path=db_path)


def _kg_seed_row(conn: sqlite3.Connection, kg_seed_id: str):
    return conn.execute(
        "SELECT * FROM kg_seeds WHERE kg_seed_id = ?", (kg_seed_id,)
    ).fetchone()


def _signal_row(conn: sqlite3.Connection, signal_id: str):
    return conn.execute(
        "SELECT * FROM signal_records WHERE signal_id = ?", (signal_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# TestSchemaTablesExist
# ---------------------------------------------------------------------------

class TestSchemaTablesExist:
    def test_kg_seeds_table_exists(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            result = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='kg_seeds'"
            ).fetchone()
            assert result is not None
        finally:
            conn.close()

    def test_signal_records_table_exists(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            result = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='signal_records'"
            ).fetchone()
            assert result is not None
        finally:
            conn.close()

    def test_kg_seeds_has_required_columns(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(kg_seeds)")}
            for col in ("kg_seed_id", "episode_id", "run_id", "extracted_at",
                        "entity_count", "relationship_count", "source_count",
                        "full_record", "metadata_version"):
                assert col in cols, f"Missing column: {col}"
        finally:
            conn.close()

    def test_signal_records_has_required_columns(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(signal_records)")}
            for col in ("signal_id", "episode_id", "run_id", "ingested_at",
                        "youtube_video_id", "window_days", "data_quality",
                        "views", "watch_minutes", "full_record", "metadata_version"):
                assert col in cols, f"Missing column: {col}"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestInsertKgSeed
# ---------------------------------------------------------------------------

class TestInsertKgSeed:
    def test_insert_and_retrieve(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_kg_seed(conn, "kg001", "ep_000", "run-abc", "2026-06-12T10:00:00+00:00")
            conn.commit()
            row = _kg_seed_row(conn, "kg001")
            assert row is not None
            assert row["episode_id"] == "ep_000"
            assert row["run_id"] == "run-abc"
        finally:
            conn.close()

    def test_extracted_at_stored(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_kg_seed(conn, "kg001", "ep_000", "run-abc", "2026-06-12T10:00:00+00:00")
            conn.commit()
            row = _kg_seed_row(conn, "kg001")
            assert row["extracted_at"] == "2026-06-12T10:00:00+00:00"
        finally:
            conn.close()

    def test_metadata_version_stored(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_kg_seed(conn, "kg001", "ep_000", "run-abc", "2026-06-12T10:00:00+00:00")
            conn.commit()
            row = _kg_seed_row(conn, "kg001")
            assert row["metadata_version"] == "v1"
        finally:
            conn.close()

    def test_null_full_record_allowed(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_kg_seed(conn, "kg001", "ep_000", "run-abc", "2026-06-12T10:00:00+00:00")
            conn.commit()
            row = _kg_seed_row(conn, "kg001")
            assert row["full_record"] is None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestInsertKgSeedIdempotent
# ---------------------------------------------------------------------------

class TestInsertKgSeedIdempotent:
    def test_second_insert_replaces(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_kg_seed(conn, "kg001", "ep_000", "run-abc", "2026-06-12T10:00:00+00:00",
                           entity_count=3)
            conn.commit()
            insert_kg_seed(conn, "kg001", "ep_000", "run-abc", "2026-06-12T11:00:00+00:00",
                           entity_count=7)
            conn.commit()
            row = _kg_seed_row(conn, "kg001")
            assert row["entity_count"] == 7
        finally:
            conn.close()

    def test_only_one_row_after_replace(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_kg_seed(conn, "kg001", "ep_000", "run-abc", "2026-06-12T10:00:00+00:00")
            conn.commit()
            insert_kg_seed(conn, "kg001", "ep_000", "run-abc", "2026-06-12T11:00:00+00:00")
            conn.commit()
            count = conn.execute(
                "SELECT COUNT(*) FROM kg_seeds WHERE kg_seed_id = ?", ("kg001",)
            ).scalar() if hasattr(conn, "scalar") else \
                conn.execute("SELECT COUNT(*) FROM kg_seeds WHERE kg_seed_id = ?", ("kg001",)).fetchone()[0]
            assert count == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestInsertKgSeedFullRecord
# ---------------------------------------------------------------------------

class TestInsertKgSeedFullRecord:
    def test_full_record_serialized(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            sample = {"entities": [{"entity_id": "abc"}], "run_id": "run-abc"}
            insert_kg_seed(conn, "kg001", "ep_000", "run-abc", "2026-06-12T10:00:00+00:00",
                           full_record=sample)
            conn.commit()
            row = _kg_seed_row(conn, "kg001")
            parsed = json.loads(row["full_record"])
            assert parsed["run_id"] == "run-abc"
            assert len(parsed["entities"]) == 1
        finally:
            conn.close()

    def test_full_record_entities_preserved(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            sample = {"entities": [{"entity_id": "x1", "name": "Malta"}]}
            insert_kg_seed(conn, "kg001", "ep_000", "run-abc", "2026-06-12T10:00:00+00:00",
                           full_record=sample)
            conn.commit()
            row = _kg_seed_row(conn, "kg001")
            parsed = json.loads(row["full_record"])
            assert parsed["entities"][0]["name"] == "Malta"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestInsertSignalRecord
# ---------------------------------------------------------------------------

class TestInsertSignalRecord:
    def test_insert_and_retrieve(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_signal_record(conn, "sig001", "ep_000", "run-abc",
                                 "2026-06-12T10:00:00+00:00")
            conn.commit()
            row = _signal_row(conn, "sig001")
            assert row is not None
            assert row["episode_id"] == "ep_000"
        finally:
            conn.close()

    def test_youtube_video_id_stored(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_signal_record(conn, "sig001", "ep_000", "run-abc",
                                 "2026-06-12T10:00:00+00:00",
                                 youtube_video_id="abc123")
            conn.commit()
            row = _signal_row(conn, "sig001")
            assert row["youtube_video_id"] == "abc123"
        finally:
            conn.close()

    def test_window_days_stored(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_signal_record(conn, "sig001", "ep_000", "run-abc",
                                 "2026-06-12T10:00:00+00:00",
                                 window_days=14)
            conn.commit()
            row = _signal_row(conn, "sig001")
            assert row["window_days"] == 14
        finally:
            conn.close()

    def test_views_stored(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_signal_record(conn, "sig001", "ep_000", "run-abc",
                                 "2026-06-12T10:00:00+00:00",
                                 views=1500.0)
            conn.commit()
            row = _signal_row(conn, "sig001")
            assert row["views"] == 1500.0
        finally:
            conn.close()

    def test_watch_minutes_stored(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_signal_record(conn, "sig001", "ep_000", "run-abc",
                                 "2026-06-12T10:00:00+00:00",
                                 watch_minutes=3200.5)
            conn.commit()
            row = _signal_row(conn, "sig001")
            assert abs(row["watch_minutes"] - 3200.5) < 0.01
        finally:
            conn.close()

    def test_null_views_allowed(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_signal_record(conn, "sig001", "ep_000", "run-abc",
                                 "2026-06-12T10:00:00+00:00")
            conn.commit()
            row = _signal_row(conn, "sig001")
            assert row["views"] is None
        finally:
            conn.close()

    def test_metadata_version_stored(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_signal_record(conn, "sig001", "ep_000", "run-abc",
                                 "2026-06-12T10:00:00+00:00")
            conn.commit()
            row = _signal_row(conn, "sig001")
            assert row["metadata_version"] == "v1"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestInsertSignalIdempotent
# ---------------------------------------------------------------------------

class TestInsertSignalIdempotent:
    def test_second_insert_replaces(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_signal_record(conn, "sig001", "ep_000", "run-abc",
                                 "2026-06-12T10:00:00+00:00", views=100.0)
            conn.commit()
            insert_signal_record(conn, "sig001", "ep_000", "run-abc",
                                 "2026-06-12T11:00:00+00:00", views=999.0)
            conn.commit()
            row = _signal_row(conn, "sig001")
            assert row["views"] == 999.0
        finally:
            conn.close()

    def test_only_one_row_after_replace(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_signal_record(conn, "sig001", "ep_000", "run-abc",
                                 "2026-06-12T10:00:00+00:00")
            conn.commit()
            insert_signal_record(conn, "sig001", "ep_000", "run-abc",
                                 "2026-06-12T11:00:00+00:00")
            conn.commit()
            count = conn.execute(
                "SELECT COUNT(*) FROM signal_records WHERE signal_id = ?", ("sig001",)
            ).fetchone()[0]
            assert count == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestInsertSignalFullRecord
# ---------------------------------------------------------------------------

class TestInsertSignalFullRecord:
    def test_full_record_serialized(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            sample = {"signal_id": "sig001", "metrics": {"views": 1500}}
            insert_signal_record(conn, "sig001", "ep_000", "run-abc",
                                 "2026-06-12T10:00:00+00:00",
                                 full_record=sample)
            conn.commit()
            row = _signal_row(conn, "sig001")
            parsed = json.loads(row["full_record"])
            assert parsed["metrics"]["views"] == 1500
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestSignalDataQuality
# ---------------------------------------------------------------------------

class TestSignalDataQuality:
    @pytest.mark.parametrize("quality", ["captured", "error", "no_data"])
    def test_data_quality_stored(self, quality, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_signal_record(conn, "sig001", "ep_000", "run-abc",
                                 "2026-06-12T10:00:00+00:00",
                                 data_quality=quality)
            conn.commit()
            row = _signal_row(conn, "sig001")
            assert row["data_quality"] == quality
        finally:
            conn.close()

    def test_default_data_quality_is_no_data(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_signal_record(conn, "sig001", "ep_000", "run-abc",
                                 "2026-06-12T10:00:00+00:00")
            conn.commit()
            row = _signal_row(conn, "sig001")
            assert row["data_quality"] == "no_data"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestKgSeedCounts
# ---------------------------------------------------------------------------

class TestKgSeedCounts:
    def test_entity_count_stored(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_kg_seed(conn, "kg001", "ep_000", "run-abc",
                           "2026-06-12T10:00:00+00:00", entity_count=5)
            conn.commit()
            row = _kg_seed_row(conn, "kg001")
            assert row["entity_count"] == 5
        finally:
            conn.close()

    def test_relationship_count_stored(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_kg_seed(conn, "kg001", "ep_000", "run-abc",
                           "2026-06-12T10:00:00+00:00", relationship_count=8)
            conn.commit()
            row = _kg_seed_row(conn, "kg001")
            assert row["relationship_count"] == 8
        finally:
            conn.close()

    def test_source_count_stored(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_kg_seed(conn, "kg001", "ep_000", "run-abc",
                           "2026-06-12T10:00:00+00:00", source_count=3)
            conn.commit()
            row = _kg_seed_row(conn, "kg001")
            assert row["source_count"] == 3
        finally:
            conn.close()

    def test_default_counts_are_zero(self, tmp_path):
        conn = _conn(tmp_path)
        try:
            insert_kg_seed(conn, "kg001", "ep_000", "run-abc",
                           "2026-06-12T10:00:00+00:00")
            conn.commit()
            row = _kg_seed_row(conn, "kg001")
            assert row["entity_count"] == 0
            assert row["relationship_count"] == 0
            assert row["source_count"] == 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestSchemaIdempotency
# ---------------------------------------------------------------------------

class TestSchemaIdempotency:
    def test_initialize_schema_twice_safe(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialize_schema(db_path=db_path)
        initialize_schema(db_path=db_path)  # must not raise

    def test_data_preserved_after_reinit(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialize_schema(db_path=db_path)
        conn = get_connection(db_path=db_path)
        try:
            insert_kg_seed(conn, "kg001", "ep_000", "run-abc",
                           "2026-06-12T10:00:00+00:00", entity_count=4)
            conn.commit()
        finally:
            conn.close()

        # Re-initialize (should be no-op)
        initialize_schema(db_path=db_path)

        conn2 = get_connection(db_path=db_path)
        try:
            row = _kg_seed_row(conn2, "kg001")
            assert row["entity_count"] == 4
        finally:
            conn2.close()
