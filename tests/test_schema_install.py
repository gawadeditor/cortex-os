"""
tests/test_schema_install.py — Schema installation and Discovery Director table verification

Tests:
  TestSchemaInstall        — initialize_schema() completes on a fresh DB without error
  TestDiscoveryTables      — all four Discovery Director tables exist post-install
  TestOpportunitiesIndexes — required indexes on opportunities are present
  TestDiscoveryCyclesSchema — discovery_cycles has all required funnel columns
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hypogeum.db import initialize_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def _indexes(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    return {r[0] for r in rows}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}  # r[1] is the column name


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# TestSchemaInstall
# ---------------------------------------------------------------------------

class TestSchemaInstall:

    def test_initialize_schema_succeeds_on_fresh_db(self, tmp_path):
        db = tmp_path / "fresh.db"
        initialize_schema(db)  # must not raise

    def test_initialize_schema_is_idempotent(self, tmp_path):
        db = tmp_path / "idempotent.db"
        initialize_schema(db)
        initialize_schema(db)  # second call must not raise or duplicate data

    def test_core_tables_exist(self, tmp_path):
        db = tmp_path / "core.db"
        initialize_schema(db)
        tables = _tables(_open(db))
        core = {"episodes", "runs", "episode_topics", "channel_profile_settings"}
        assert core.issubset(tables), f"Missing core tables: {core - tables}"


# ---------------------------------------------------------------------------
# TestDiscoveryTables
# ---------------------------------------------------------------------------

class TestDiscoveryTables:

    _DISCOVERY_TABLES = {
        "opportunities",
        "opportunity_events",
        "discovery_cycles",
        "source_verifications",
    }

    def test_all_four_discovery_tables_exist(self, tmp_path):
        db = tmp_path / "disc.db"
        initialize_schema(db)
        tables = _tables(_open(db))
        missing = self._DISCOVERY_TABLES - tables
        assert not missing, f"Missing discovery tables: {missing}"

    def test_opportunities_table_exists(self, tmp_path):
        db = tmp_path / "disc.db"
        initialize_schema(db)
        assert "opportunities" in _tables(_open(db))

    def test_opportunity_events_table_exists(self, tmp_path):
        db = tmp_path / "disc.db"
        initialize_schema(db)
        assert "opportunity_events" in _tables(_open(db))

    def test_discovery_cycles_table_exists(self, tmp_path):
        db = tmp_path / "disc.db"
        initialize_schema(db)
        assert "discovery_cycles" in _tables(_open(db))

    def test_source_verifications_table_exists(self, tmp_path):
        db = tmp_path / "disc.db"
        initialize_schema(db)
        assert "source_verifications" in _tables(_open(db))


# ---------------------------------------------------------------------------
# TestOpportunitiesSchema
# ---------------------------------------------------------------------------

class TestOpportunitiesSchema:

    def test_required_indexes_exist(self, tmp_path):
        db = tmp_path / "opp.db"
        initialize_schema(db)
        idx = _indexes(_open(db))
        assert "idx_opp_subject"  in idx, "idx_opp_subject missing"
        assert "idx_opp_status"   in idx, "idx_opp_status missing"
        assert "idx_opp_priority" in idx, "idx_opp_priority missing"

    def test_lifecycle_columns_present(self, tmp_path):
        db = tmp_path / "opp.db"
        initialize_schema(db)
        cols = _columns(_open(db), "opportunities")
        required = {
            "opportunity_id", "canonical_subject", "topic_text",
            "verification_status", "source_count", "source_score",
            "initial_composite", "final_composite",
            "priority_score", "priority_tier",
            "status", "rejection_reason",
            "topic_id", "routed_to", "selected_at", "enqueued_at",
        }
        missing = required - cols
        assert not missing, f"opportunities missing columns: {missing}"

    def test_opportunity_id_is_primary_key(self, tmp_path):
        db = tmp_path / "opp.db"
        initialize_schema(db)
        conn = _open(db)
        rows = conn.execute("PRAGMA table_info(opportunities)").fetchall()
        pk_cols = [r[1] for r in rows if r[5] == 1]  # r[5] is pk flag
        assert pk_cols == ["opportunity_id"], f"Unexpected PK: {pk_cols}"

    def test_canonical_subject_is_not_primary_key(self, tmp_path):
        db = tmp_path / "opp.db"
        initialize_schema(db)
        conn = _open(db)
        rows = conn.execute("PRAGMA table_info(opportunities)").fetchall()
        pk_cols = {r[1] for r in rows if r[5] == 1}
        assert "canonical_subject" not in pk_cols


# ---------------------------------------------------------------------------
# TestDiscoveryCyclesSchema
# ---------------------------------------------------------------------------

class TestDiscoveryCyclesSchema:

    def test_required_indexes_exist(self, tmp_path):
        db = tmp_path / "cycles.db"
        initialize_schema(db)
        idx = _indexes(_open(db))
        assert "idx_disccycle_channel" in idx

    def test_funnel_columns_present(self, tmp_path):
        db = tmp_path / "cycles.db"
        initialize_schema(db)
        cols = _columns(_open(db), "discovery_cycles")
        required = {
            "cycle_id", "channel_id", "engine_used",
            "raw_candidates_count", "viable_after_feasibility",
            "verify_eligible_count", "verified_count",
            "rejected_source_count", "rejected_score_count",
            "selected_opportunity_id", "selected_canonical_subject",
            "topic_id_enqueued",
            "model_used", "prompt_version",
            "status", "created_at", "completed_at",
        }
        missing = required - cols
        assert not missing, f"discovery_cycles missing columns: {missing}"

    def test_opportunity_events_references_opportunity_id(self, tmp_path):
        db = tmp_path / "events.db"
        initialize_schema(db)
        cols = _columns(_open(db), "opportunity_events")
        assert "opportunity_id" in cols, "opportunity_events must reference opportunity_id"
        assert "canonical_subject" not in cols, \
            "opportunity_events must NOT reference canonical_subject directly"
