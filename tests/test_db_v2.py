"""
tests/test_db_v2.py — Operational Data Model v2 tests

Coverage:
    TestV2SchemaStructure       — v1 tables preserved; all v2 tables exist; views exist
    TestV2ColumnMigrations      — new columns added to v1 tables; idempotent
    TestV2Idempotency           — initialize_schema called twice is safe
    TestStageExecutions         — insert, complete, fail, skip lifecycle
    TestGateVerdicts            — insert gate verdict; FK enforcement
    TestAssetVersions           — insert attempts with success/failure status
    TestBudgetAuthorizations    — insert authorization record
    TestShorts                  — insert short; update_short_complete
    TestRenderTimelines         — insert timeline entry
    TestSignalWindows           — insert analytics window
    TestArchiveIndex            — insert archive entry with nullable tags
    TestKgEntities              — insert entity; idempotent replace
    TestLearningRecords         — insert learning signal; FK enforcement
    TestV2ForeignKeys           — FK violations raise IntegrityError
    TestNoProductionSideEffects — no ep_000 run; no publish; no asset generation
    TestV2Views                 — all four views are queryable
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from hypogeum.db import (
    _DEFAULT_DB_PATH,
    _SCHEMA_PATH,
    _V2_COLUMN_MIGRATIONS,
    _apply_v2_column_migrations,
    get_connection,
    initialize_schema,
    # v1 helpers
    insert_episode,
    insert_run,
    # v2 helpers
    insert_archive_index_entry,
    insert_asset_version,
    insert_budget_authorization,
    insert_gate_verdict,
    insert_kg_entity,
    insert_learning_record,
    insert_render_timeline_entry,
    insert_short,
    insert_signal_window,
    insert_stage_execution,
    update_short_complete,
    update_stage_execution_complete,
    update_stage_execution_failed,
    update_stage_execution_skipped,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """Initialized DB in tmp_path. Returns open connection."""
    db_path = tmp_path / "test_v2.db"
    initialize_schema(db_path)
    conn = get_connection(db_path)
    yield conn
    conn.close()


@pytest.fixture()
def db_with_run(db):
    """DB with one 'running' run already inserted."""
    insert_run(db, "run-v2-001", "ep_test", "2026-06-12T10:00:00Z")
    db.commit()
    return db


_TS = "2026-06-12T10:00:00Z"
_TS2 = "2026-06-12T12:00:00Z"


def _uid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# TestV2SchemaStructure
# ---------------------------------------------------------------------------

class TestV2SchemaStructure:
    _V1_TABLES = ("runs", "episodes", "assets", "costs",
                  "thumbnail_candidates", "titles", "kg_seeds", "signal_records")

    _V2_TABLES = (
        "stage_executions", "gate_verdicts", "asset_versions",
        "budget_authorizations", "shorts", "render_timelines",
        "signal_windows", "archive_index", "kg_entities", "learning_records",
    )

    _V2_VIEWS = (
        "v_episode_costs", "v_stage_performance",
        "v_gate_pass_rates", "v_asset_retry_rates",
    )

    def _get_tables_and_views(self, conn):
        rows = conn.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
        tables = {r["name"] for r in rows if r["type"] == "table"}
        views  = {r["name"] for r in rows if r["type"] == "view"}
        return tables, views

    def test_v1_tables_still_exist(self, db):
        tables, _ = self._get_tables_and_views(db)
        for t in self._V1_TABLES:
            assert t in tables, f"v1 table '{t}' missing after v2 migration"

    def test_v2_tables_exist(self, db):
        tables, _ = self._get_tables_and_views(db)
        for t in self._V2_TABLES:
            assert t in tables, f"v2 table '{t}' not created"

    def test_v2_views_exist(self, db):
        _, views = self._get_tables_and_views(db)
        for v in self._V2_VIEWS:
            assert v in views, f"v2 view '{v}' not created"

    def test_schema_sql_contains_v2_marker(self):
        content = _SCHEMA_PATH.read_text(encoding="utf-8")
        assert "v2 TABLES" in content or "v2" in content.lower()

    def test_all_v2_tables_in_schema_sql(self):
        content = _SCHEMA_PATH.read_text(encoding="utf-8")
        for t in self._V2_TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {t}" in content, (
                f"v2 table '{t}' not in schema.sql"
            )

    def test_all_v2_views_in_schema_sql(self):
        content = _SCHEMA_PATH.read_text(encoding="utf-8")
        for v in self._V2_VIEWS:
            assert f"CREATE VIEW IF NOT EXISTS {v}" in content, (
                f"v2 view '{v}' not in schema.sql"
            )


# ---------------------------------------------------------------------------
# TestV2ColumnMigrations
# ---------------------------------------------------------------------------

class TestV2ColumnMigrations:
    def test_v2_column_migrations_dict_exists(self):
        assert isinstance(_V2_COLUMN_MIGRATIONS, dict)
        assert len(_V2_COLUMN_MIGRATIONS) > 0

    def test_runs_gets_new_columns(self, db):
        cols = {row[1] for row in db.execute("PRAGMA table_info(runs)").fetchall()}
        for col_name, _ in _V2_COLUMN_MIGRATIONS["runs"]:
            assert col_name in cols, f"runs.{col_name} missing after migration"

    def test_episodes_gets_new_columns(self, db):
        cols = {row[1] for row in db.execute("PRAGMA table_info(episodes)").fetchall()}
        for col_name, _ in _V2_COLUMN_MIGRATIONS["episodes"]:
            assert col_name in cols, f"episodes.{col_name} missing after migration"

    def test_assets_gets_new_columns(self, db):
        cols = {row[1] for row in db.execute("PRAGMA table_info(assets)").fetchall()}
        for col_name, _ in _V2_COLUMN_MIGRATIONS["assets"]:
            assert col_name in cols, f"assets.{col_name} missing after migration"

    def test_costs_gets_new_columns(self, db):
        cols = {row[1] for row in db.execute("PRAGMA table_info(costs)").fetchall()}
        for col_name, _ in _V2_COLUMN_MIGRATIONS["costs"]:
            assert col_name in cols, f"costs.{col_name} missing after migration"

    def test_apply_v2_column_migrations_idempotent(self, db):
        """Calling _apply_v2_column_migrations twice must not raise."""
        _apply_v2_column_migrations(db)
        _apply_v2_column_migrations(db)
        # If we got here without IntegrityError or OperationalError, it's idempotent.


# ---------------------------------------------------------------------------
# TestV2Idempotency
# ---------------------------------------------------------------------------

class TestV2Idempotency:
    def test_initialize_schema_twice_is_safe(self, tmp_path):
        db_path = tmp_path / "idem.db"
        initialize_schema(db_path)
        initialize_schema(db_path)  # Must not raise

    def test_initialize_schema_three_times_is_safe(self, tmp_path):
        db_path = tmp_path / "idem3.db"
        for _ in range(3):
            initialize_schema(db_path)


# ---------------------------------------------------------------------------
# TestStageExecutions
# ---------------------------------------------------------------------------

class TestStageExecutions:
    def test_insert_stage_execution(self, db_with_run):
        db = db_with_run
        sid = _uid()
        insert_stage_execution(db, sid, "run-v2-001", "ep_test",
                               "executive_producer", _TS)
        db.commit()
        row = db.execute(
            "SELECT * FROM stage_executions WHERE stage_exec_id=?", (sid,)
        ).fetchone()
        assert row is not None
        assert row["status"] == "running"
        assert row["stage_name"] == "executive_producer"

    def test_update_stage_execution_complete(self, db_with_run):
        db = db_with_run
        sid = _uid()
        insert_stage_execution(db, sid, "run-v2-001", "ep_test",
                               "research_director", _TS)
        update_stage_execution_complete(db, sid, _TS2, duration_ms=3500, cost_usd=0.42)
        db.commit()
        row = db.execute(
            "SELECT * FROM stage_executions WHERE stage_exec_id=?", (sid,)
        ).fetchone()
        assert row["status"] == "completed"
        assert row["duration_ms"] == 3500
        assert row["cost_usd"] == pytest.approx(0.42)

    def test_update_stage_execution_failed(self, db_with_run):
        db = db_with_run
        sid = _uid()
        insert_stage_execution(db, sid, "run-v2-001", "ep_test",
                               "visual_package_director", _TS)
        update_stage_execution_failed(db, sid, _TS2, "Connection timeout", duration_ms=1000)
        db.commit()
        row = db.execute(
            "SELECT * FROM stage_executions WHERE stage_exec_id=?", (sid,)
        ).fetchone()
        assert row["status"] == "failed"
        assert "timeout" in row["error_message"].lower()

    def test_update_stage_execution_skipped(self, db_with_run):
        db = db_with_run
        sid = _uid()
        insert_stage_execution(db, sid, "run-v2-001", "ep_test",
                               "script_engine", _TS)
        update_stage_execution_skipped(db, sid, _TS2)
        db.commit()
        row = db.execute(
            "SELECT * FROM stage_executions WHERE stage_exec_id=?", (sid,)
        ).fetchone()
        assert row["status"] == "skipped"
        assert row["skipped"] == 1

    def test_multiple_stages_per_run(self, db_with_run):
        db = db_with_run
        stages = ["executive_producer", "research_director", "narrative_architect"]
        for stage in stages:
            sid = _uid()
            insert_stage_execution(db, sid, "run-v2-001", "ep_test", stage, _TS)
        db.commit()
        count = db.execute(
            "SELECT COUNT(*) FROM stage_executions WHERE run_id='run-v2-001'"
        ).fetchone()[0]
        assert count == 3


# ---------------------------------------------------------------------------
# TestGateVerdicts
# ---------------------------------------------------------------------------

class TestGateVerdicts:
    def test_insert_gate_verdict_approved(self, db_with_run):
        db = db_with_run
        vid = _uid()
        insert_gate_verdict(
            db, vid, "run-v2-001", "ep_test",
            gate_name="bga_pass1", verdict="APPROVED", evaluated_at=_TS,
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM gate_verdicts WHERE verdict_id=?", (vid,)
        ).fetchone()
        assert row["verdict"] == "APPROVED"
        assert row["gate_name"] == "bga_pass1"

    def test_insert_gate_verdict_with_rule_ids(self, db_with_run):
        db = db_with_run
        vid = _uid()
        insert_gate_verdict(
            db, vid, "run-v2-001", "ep_test",
            gate_name="tig", verdict="APPROVED_WITH_FLAGS", evaluated_at=_TS,
            rule_ids_flagged=["TIG-W01", "TIG-W02"],
            flag_count=2, warning_count=2,
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM gate_verdicts WHERE verdict_id=?", (vid,)
        ).fetchone()
        assert row["flag_count"] == 2
        flagged = json.loads(row["rule_ids_flagged"])
        assert "TIG-W01" in flagged

    def test_insert_gate_verdict_blocked(self, db_with_run):
        db = db_with_run
        vid = _uid()
        insert_gate_verdict(
            db, vid, "run-v2-001", "ep_test",
            gate_name="bga_pass2", verdict="REJECTED", evaluated_at=_TS,
            rule_ids_flagged=["E-C05"], flag_count=1, blocking_count=1,
            full_report={"verdict": "REJECTED", "flags": ["E-C05"]},
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM gate_verdicts WHERE verdict_id=?", (vid,)
        ).fetchone()
        assert row["blocking_count"] == 1
        report = json.loads(row["full_report"])
        assert report["verdict"] == "REJECTED"

    def test_gate_verdict_fk_enforcement(self, db):
        """FK violation: run_id that doesn't exist must raise IntegrityError."""
        with pytest.raises(sqlite3.IntegrityError):
            insert_gate_verdict(
                db, _uid(), "nonexistent-run", "ep_test",
                gate_name="bga_pass1", verdict="APPROVED", evaluated_at=_TS,
            )
            db.commit()


# ---------------------------------------------------------------------------
# TestAssetVersions
# ---------------------------------------------------------------------------

class TestAssetVersions:
    def test_insert_asset_version_success(self, db_with_run):
        db = db_with_run
        insert_asset_version(
            db, version_id=_uid(), asset_slot_id="slot-001",
            run_id="run-v2-001", episode_id="ep_test",
            asset_type="thumbnail", provider="muapi",
            attempt_number=1, version_index=0,
            status="succeeded", attempted_at=_TS,
            asset_id="asset-abc123", cost_usd=0.08,
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM asset_versions WHERE asset_slot_id='slot-001'"
        ).fetchone()
        assert row["status"] == "succeeded"
        assert row["attempt_number"] == 1

    def test_insert_asset_version_failed(self, db_with_run):
        db = db_with_run
        insert_asset_version(
            db, version_id=_uid(), asset_slot_id="slot-002",
            run_id="run-v2-001", episode_id="ep_test",
            asset_type="video_clip", provider="muapi",
            attempt_number=1, version_index=0,
            status="failed", attempted_at=_TS,
            error_type="MUAPIQuotaExhausted",
            error_message="Quota exceeded for this billing period",
            cost_usd=0.0,
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM asset_versions WHERE asset_slot_id='slot-002'"
        ).fetchone()
        assert row["status"] == "failed"
        assert row["error_type"] == "MUAPIQuotaExhausted"

    def test_multiple_attempts_per_slot(self, db_with_run):
        db = db_with_run
        slot = "slot-retry"
        for i in range(3):
            insert_asset_version(
                db, version_id=_uid(), asset_slot_id=slot,
                run_id="run-v2-001", episode_id="ep_test",
                asset_type="thumbnail", provider="muapi",
                attempt_number=i + 1, version_index=i,
                status="failed" if i < 2 else "succeeded",
                attempted_at=_TS,
            )
        db.commit()
        rows = db.execute(
            "SELECT * FROM asset_versions WHERE asset_slot_id=? ORDER BY attempt_number",
            (slot,)
        ).fetchall()
        assert len(rows) == 3
        assert rows[2]["status"] == "succeeded"


# ---------------------------------------------------------------------------
# TestBudgetAuthorizations
# ---------------------------------------------------------------------------

class TestBudgetAuthorizations:
    def test_insert_budget_authorization_authorized(self, db_with_run):
        db = db_with_run
        insert_budget_authorization(
            db, auth_id=_uid(), run_id="run-v2-001", episode_id="ep_test",
            evaluated_at=_TS, verdict="AUTHORIZED",
            budget_ceiling_usd=5.0, estimated_cost_usd=2.3, headroom_usd=2.7,
            estimates_by_provider={"muapi": 1.5, "openrouter": 0.8},
            model_slug="openai/gpt-5.5-pro",
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM budget_authorizations WHERE run_id='run-v2-001'"
        ).fetchone()
        assert row["verdict"] == "AUTHORIZED"
        estimates = json.loads(row["estimates_by_provider"])
        assert estimates["muapi"] == 1.5

    def test_insert_budget_authorization_blocked(self, db_with_run):
        db = db_with_run
        insert_budget_authorization(
            db, auth_id=_uid(), run_id="run-v2-001", episode_id="ep_test",
            evaluated_at=_TS, verdict="BLOCKED",
            budget_ceiling_usd=1.0, estimated_cost_usd=8.5, headroom_usd=-7.5,
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM budget_authorizations WHERE verdict='BLOCKED'"
        ).fetchone()
        assert row is not None
        assert row["headroom_usd"] == pytest.approx(-7.5)


# ---------------------------------------------------------------------------
# TestShorts
# ---------------------------------------------------------------------------

class TestShorts:
    def test_insert_short(self, db_with_run):
        db = db_with_run
        insert_short(
            db, short_id="ep_test_short_1", run_id="run-v2-001",
            episode_id="ep_test", short_index=1, created_at=_TS,
            title="Short 1 — The Roman Sewers",
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM shorts WHERE short_id='ep_test_short_1'"
        ).fetchone()
        assert row["status"] == "in_progress"
        assert row["short_index"] == 1

    def test_update_short_complete(self, db_with_run):
        db = db_with_run
        insert_short(
            db, short_id="ep_test_short_2", run_id="run-v2-001",
            episode_id="ep_test", short_index=2, created_at=_TS,
        )
        db.commit()
        update_short_complete(
            db, "ep_test_short_2",
            video_asset_id="vid-001", thumbnail_asset_id="thumb-001",
            audio_asset_id="aud-001", duration_seconds=58,
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM shorts WHERE short_id='ep_test_short_2'"
        ).fetchone()
        assert row["status"] == "complete"
        assert row["duration_seconds"] == 58
        assert row["video_asset_id"] == "vid-001"

    def test_three_shorts_per_episode(self, db_with_run):
        db = db_with_run
        for i in range(1, 4):
            insert_short(
                db, short_id=f"ep_test_s{i}", run_id="run-v2-001",
                episode_id="ep_test", short_index=i, created_at=_TS,
            )
        db.commit()
        count = db.execute(
            "SELECT COUNT(*) FROM shorts WHERE episode_id='ep_test'"
        ).fetchone()[0]
        assert count == 3


# ---------------------------------------------------------------------------
# TestRenderTimelines
# ---------------------------------------------------------------------------

class TestRenderTimelines:
    def test_insert_render_timeline_entry(self, db_with_run):
        db = db_with_run
        insert_render_timeline_entry(
            db, timeline_entry_id=_uid(), run_id="run-v2-001",
            episode_id="ep_test", scene_index=0, clip_index=0,
            asset_id="clip-001", created_at=_TS,
            start_ms=0, end_ms=4800, duration_ms=4800,
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM render_timelines WHERE run_id='run-v2-001' AND scene_index=0"
        ).fetchone()
        assert row["asset_id"] == "clip-001"
        assert row["end_ms"] == 4800

    def test_padded_and_trimmed_flags(self, db_with_run):
        db = db_with_run
        insert_render_timeline_entry(
            db, timeline_entry_id=_uid(), run_id="run-v2-001",
            episode_id="ep_test", scene_index=1, clip_index=0,
            asset_id="clip-002", created_at=_TS,
            padded=True, trimmed=False,
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM render_timelines WHERE scene_index=1"
        ).fetchone()
        assert row["padded"] == 1
        assert row["trimmed"] == 0


# ---------------------------------------------------------------------------
# TestSignalWindows
# ---------------------------------------------------------------------------

class TestSignalWindows:
    def test_insert_signal_window(self, db):
        insert_signal_window(
            db, window_id=_uid(), episode_id="ep_test",
            youtube_video_id="dQw4w9WgXcQ",
            window_start_date="2026-06-05", window_end_date="2026-06-12",
            window_days=7, fetched_at=_TS,
            views=1240.0, watch_minutes=412.0, avg_view_duration_s=19.9,
            likes=87.0, comments=12.0, shares=4.0, subscribers_gained=3.0,
            ctr=0.054, impressions=22980.0, data_quality="captured",
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM signal_windows WHERE episode_id='ep_test'"
        ).fetchone()
        assert row["views"] == pytest.approx(1240.0)
        assert row["data_quality"] == "captured"
        assert row["ctr"] == pytest.approx(0.054)

    def test_multiple_windows_per_episode(self, db):
        ep = "ep_multi_window"
        for days in (7, 14, 28):
            insert_signal_window(
                db, window_id=_uid(), episode_id=ep,
                youtube_video_id="abc123", window_days=days,
                window_start_date="2026-06-01",
                window_end_date="2026-06-12",
                fetched_at=_TS,
            )
        db.commit()
        count = db.execute(
            "SELECT COUNT(*) FROM signal_windows WHERE episode_id=?", (ep,)
        ).fetchone()[0]
        assert count == 3


# ---------------------------------------------------------------------------
# TestArchiveIndex
# ---------------------------------------------------------------------------

class TestArchiveIndex:
    def test_insert_archive_index_entry_minimal(self, db):
        insert_archive_index_entry(
            db, archive_id=_uid(), asset_id="asset-xyz",
            episode_id="ep_test", asset_type="thumbnail",
            provider="muapi", archived_at=_TS,
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM archive_index WHERE asset_id='asset-xyz'"
        ).fetchone()
        assert row["reuse_eligible"] == 1
        assert row["quality_verdict"] == "unvalidated"
        assert row["semantic_tags_json"] is None

    def test_insert_archive_index_entry_with_tags(self, db):
        insert_archive_index_entry(
            db, archive_id=_uid(), asset_id="asset-tagged",
            episode_id="ep_test", asset_type="video_clip",
            provider="muapi", archived_at=_TS,
            source_stage="visual_package_director",
            semantic_tags_json=["roman_aqueduct", "underground"],
            visual_style_json={"lighting": "torchlight", "palette": "warm_earth"},
            quality_verdict="gate_passed", gate_name="tig",
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM archive_index WHERE asset_id='asset-tagged'"
        ).fetchone()
        tags = json.loads(row["semantic_tags_json"])
        assert "roman_aqueduct" in tags
        style = json.loads(row["visual_style_json"])
        assert style["lighting"] == "torchlight"
        assert row["quality_verdict"] == "gate_passed"

    def test_reuse_eligible_false(self, db):
        insert_archive_index_entry(
            db, archive_id=_uid(), asset_id="asset-noreuse",
            episode_id="ep_test", asset_type="thumbnail",
            provider="muapi", archived_at=_TS,
            reuse_eligible=False,
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM archive_index WHERE asset_id='asset-noreuse'"
        ).fetchone()
        assert row["reuse_eligible"] == 0


# ---------------------------------------------------------------------------
# TestKgEntities
# ---------------------------------------------------------------------------

class TestKgEntities:
    def test_insert_kg_entity(self, db):
        insert_kg_entity(
            db, entity_id="ent-001", episode_id="ep_test",
            run_id="run-v2-001", entity_name="The Antikythera Mechanism",
            entity_type="artefact", first_seen_episode_id="ep_test",
            extracted_at=_TS,
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM kg_entities WHERE entity_id='ent-001'"
        ).fetchone()
        assert row["entity_name"] == "The Antikythera Mechanism"
        assert row["entity_type"] == "artefact"

    def test_kg_entity_idempotent_replace(self, db):
        """INSERT OR REPLACE must not raise on duplicate entity_id."""
        insert_kg_entity(
            db, entity_id="ent-002", episode_id="ep_test",
            run_id="run-v2-001", entity_name="Roman Colosseum",
            entity_type="place", first_seen_episode_id="ep_test",
            extracted_at=_TS,
        )
        db.commit()
        # Insert again with same entity_id — should replace cleanly
        insert_kg_entity(
            db, entity_id="ent-002", episode_id="ep_test",
            run_id="run-v2-002", entity_name="Roman Colosseum",
            entity_type="place", first_seen_episode_id="ep_test",
            extracted_at=_TS2,
        )
        db.commit()
        count = db.execute(
            "SELECT COUNT(*) FROM kg_entities WHERE entity_id='ent-002'"
        ).fetchone()[0]
        assert count == 1

    def test_kg_entity_types(self, db):
        for etype in ("topic", "person", "place", "artefact", "source", "other"):
            insert_kg_entity(
                db, entity_id=f"ent-{etype}", episode_id="ep_test",
                run_id="run-v2-001", entity_name=f"Entity {etype}",
                entity_type=etype, first_seen_episode_id="ep_test",
                extracted_at=_TS,
            )
        db.commit()
        count = db.execute(
            "SELECT COUNT(*) FROM kg_entities WHERE episode_id='ep_test'"
        ).fetchone()[0]
        assert count == 6


# ---------------------------------------------------------------------------
# TestLearningRecords
# ---------------------------------------------------------------------------

class TestLearningRecords:
    def test_insert_learning_record(self, db_with_run):
        db = db_with_run
        insert_learning_record(
            db, learning_id=_uid(), episode_id="ep_test",
            run_id="run-v2-001", recorded_at=_TS,
            signal_type="performance", signal_value=1240.0,
            signal_label="above_average",
            context={"thumbnail_style": "warm_amber", "topic_type": "ancient_rome"},
            recommendation="Prefer warm_amber thumbnail style for Roman topics",
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM learning_records WHERE run_id='run-v2-001'"
        ).fetchone()
        assert row["signal_type"] == "performance"
        assert row["signal_value"] == pytest.approx(1240.0)
        ctx = json.loads(row["context"])
        assert ctx["thumbnail_style"] == "warm_amber"

    def test_learning_record_fk_enforcement(self, db):
        """FK violation on run_id must raise IntegrityError."""
        with pytest.raises(sqlite3.IntegrityError):
            insert_learning_record(
                db, learning_id=_uid(), episode_id="ep_test",
                run_id="nonexistent-run", recorded_at=_TS,
                signal_type="gate_outcome",
            )
            db.commit()

    def test_all_signal_types(self, db_with_run):
        db = db_with_run
        types = ("performance", "gate_outcome", "cost_efficiency",
                 "quality_verdict", "topic_score", "thumbnail_ctr")
        for stype in types:
            insert_learning_record(
                db, learning_id=_uid(), episode_id="ep_test",
                run_id="run-v2-001", recorded_at=_TS,
                signal_type=stype,
            )
        db.commit()
        count = db.execute(
            "SELECT COUNT(*) FROM learning_records WHERE run_id='run-v2-001'"
        ).fetchone()[0]
        assert count == len(types)


# ---------------------------------------------------------------------------
# TestV2ForeignKeys
# ---------------------------------------------------------------------------

class TestV2ForeignKeys:
    def test_stage_execution_fk_violation(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            insert_stage_execution(
                db, _uid(), "bad-run-id", "ep_test",
                "executive_producer", _TS,
            )
            db.commit()

    def test_asset_version_fk_violation(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            insert_asset_version(
                db, version_id=_uid(), asset_slot_id="slot-x",
                run_id="bad-run-id", episode_id="ep_test",
                asset_type="thumbnail", provider="muapi",
                attempt_number=1, version_index=0,
                status="attempted", attempted_at=_TS,
            )
            db.commit()

    def test_budget_authorization_fk_violation(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            insert_budget_authorization(
                db, auth_id=_uid(), run_id="bad-run-id",
                episode_id="ep_test", evaluated_at=_TS, verdict="AUTHORIZED",
            )
            db.commit()

    def test_shorts_fk_violation(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            insert_short(
                db, short_id="bad-short", run_id="bad-run-id",
                episode_id="ep_test", short_index=1, created_at=_TS,
            )
            db.commit()


# ---------------------------------------------------------------------------
# TestNoProductionSideEffects
# ---------------------------------------------------------------------------

class TestNoProductionSideEffects:
    """Ensure this test module does not trigger any production side effects."""

    def test_no_ep_000_run_in_db(self, db):
        """ep_000 must never be inserted by any test in this module."""
        count = db.execute(
            "SELECT COUNT(*) FROM runs WHERE episode_id='ep_000'"
        ).fetchone()[0]
        assert count == 0, "ep_000 run must not exist — no production run in tests"

    def test_no_published_episode(self, db):
        """No episode should have a published_at value set by tests."""
        count = db.execute(
            "SELECT COUNT(*) FROM episodes WHERE published_at IS NOT NULL"
        ).fetchone()[0]
        assert count == 0, "No episode must be marked published in v2 DB tests"

    def test_default_db_path_not_production(self, tmp_path):
        """Tests use tmp_path — the default production DB must not be touched."""
        prod_db = _DEFAULT_DB_PATH
        # We never initialize_schema() with the default path in this test module.
        # Just assert the default path is not in tmp_path.
        assert str(tmp_path) not in str(prod_db)

    def test_no_archive_index_entry_without_explicit_episode(self, db):
        """Archive index must only contain entries we explicitly inserted."""
        count = db.execute("SELECT COUNT(*) FROM archive_index").fetchone()[0]
        assert count == 0, "archive_index must be empty in a fresh test DB"


# ---------------------------------------------------------------------------
# TestV2Views
# ---------------------------------------------------------------------------

class TestV2Views:
    def test_v_episode_costs_queryable(self, db_with_run):
        db = db_with_run
        # View should be queryable even on empty data
        rows = db.execute("SELECT * FROM v_episode_costs").fetchall()
        assert isinstance(rows, list)

    def test_v_stage_performance_queryable(self, db_with_run):
        db = db_with_run
        rows = db.execute("SELECT * FROM v_stage_performance").fetchall()
        assert isinstance(rows, list)

    def test_v_gate_pass_rates_queryable(self, db_with_run):
        db = db_with_run
        rows = db.execute("SELECT * FROM v_gate_pass_rates").fetchall()
        assert isinstance(rows, list)

    def test_v_asset_retry_rates_queryable(self, db_with_run):
        db = db_with_run
        rows = db.execute("SELECT * FROM v_asset_retry_rates").fetchall()
        assert isinstance(rows, list)

    def test_v_gate_pass_rates_aggregates_correctly(self, db_with_run):
        db = db_with_run
        # Insert 2 APPROVED + 1 REJECTED for bga_pass1
        for verdict in ("APPROVED", "APPROVED", "REJECTED"):
            insert_gate_verdict(
                db, _uid(), "run-v2-001", "ep_test",
                gate_name="bga_pass1", verdict=verdict, evaluated_at=_TS,
            )
        db.commit()
        row = db.execute(
            "SELECT * FROM v_gate_pass_rates WHERE gate_name='bga_pass1'"
        ).fetchone()
        assert row["total_evaluations"] == 3
        assert row["passes"] == 2
        assert row["blocks"] == 1

    def test_v_stage_performance_aggregates_correctly(self, db_with_run):
        db = db_with_run
        # Insert 2 completed + 1 failed executions for one stage
        for status_fn, status in (
            (update_stage_execution_complete, "completed"),
            (update_stage_execution_complete, "completed"),
            (update_stage_execution_failed, "failed"),
        ):
            sid = _uid()
            insert_stage_execution(db, sid, "run-v2-001", "ep_test",
                                   "audio_agent", _TS)
            if status == "completed":
                update_stage_execution_complete(db, sid, _TS2, duration_ms=2000)
            else:
                update_stage_execution_failed(db, sid, _TS2, "error", duration_ms=500)
        db.commit()
        row = db.execute(
            "SELECT * FROM v_stage_performance WHERE stage_name='audio_agent'"
        ).fetchone()
        assert row["total_executions"] == 3
        assert row["successes"] == 2
        assert row["failures"] == 1


# ---------------------------------------------------------------------------
# TestPublishingOutcomesDeliverableMigration
# Multi-platform publishing idempotency (2026-07-25): publishing_outcomes
# gains deliverable_id, UNIQUE(episode_id, platform) -> UNIQUE(episode_id,
# platform, deliverable_id), via a table-rebuild migration
# (db.py::_migrate_publishing_outcomes_deliverable_id).
# ---------------------------------------------------------------------------

class TestPublishingOutcomesDeliverableMigration:
    def _make_old_shape_db(self, tmp_path) -> Path:
        """A pre-existing DB with publishing_outcomes in its old 2-column-
        unique shape, plus one real row — exactly what a production
        database looked like before this migration existed."""
        db_path = tmp_path / "old_shape.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE publishing_outcomes (
                outcome_id          TEXT NOT NULL PRIMARY KEY,
                episode_id          TEXT NOT NULL,
                run_id              TEXT,
                platform            TEXT NOT NULL,
                publishing_status   TEXT NOT NULL DEFAULT 'unknown',
                platform_video_id   TEXT,
                published_at        TEXT,
                url                 TEXT,
                failure_reason      TEXT,
                recorded_at         TEXT NOT NULL,
                metadata_version    TEXT NOT NULL DEFAULT 'v1',
                UNIQUE(episode_id, platform)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO publishing_outcomes
                (outcome_id, episode_id, run_id, platform, publishing_status,
                 platform_video_id, published_at, url, recorded_at, metadata_version)
            VALUES ('out1', 'ep_real_001', 'run_1', 'youtube', 'success',
                    'yt_vid_abc', '2026-07-01T00:00:00Z',
                    'https://youtube.com/watch?v=yt_vid_abc',
                    '2026-07-01T00:00:01Z', 'v1')
            """
        )
        conn.commit()
        conn.close()
        return db_path

    def test_migration_adds_deliverable_id_and_widens_constraint(self, tmp_path):
        db_path = self._make_old_shape_db(tmp_path)
        initialize_schema(db_path)

        conn = get_connection(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(publishing_outcomes)").fetchall()}
        assert "deliverable_id" in cols
        conn.close()

    def test_migration_backfills_existing_rows_as_main_episode(self, tmp_path):
        db_path = self._make_old_shape_db(tmp_path)
        initialize_schema(db_path)

        conn = get_connection(db_path)
        row = dict(conn.execute(
            "SELECT * FROM publishing_outcomes WHERE episode_id='ep_real_001'"
        ).fetchone())
        conn.close()

        assert row["deliverable_id"] == "main_episode"

    def test_migration_preserves_all_original_row_data(self, tmp_path):
        db_path = self._make_old_shape_db(tmp_path)
        initialize_schema(db_path)

        conn = get_connection(db_path)
        row = dict(conn.execute(
            "SELECT * FROM publishing_outcomes WHERE episode_id='ep_real_001'"
        ).fetchone())
        conn.close()

        assert row["outcome_id"] == "out1"
        assert row["run_id"] == "run_1"
        assert row["platform"] == "youtube"
        assert row["publishing_status"] == "success"
        assert row["platform_video_id"] == "yt_vid_abc"
        assert row["published_at"] == "2026-07-01T00:00:00Z"
        assert row["url"] == "https://youtube.com/watch?v=yt_vid_abc"
        assert row["recorded_at"] == "2026-07-01T00:00:01Z"
        assert row["metadata_version"] == "v1"

    def test_migration_is_idempotent(self, tmp_path):
        db_path = self._make_old_shape_db(tmp_path)
        initialize_schema(db_path)

        conn = get_connection(db_path)
        row_before = dict(conn.execute(
            "SELECT * FROM publishing_outcomes WHERE episode_id='ep_real_001'"
        ).fetchone())
        conn.close()

        initialize_schema(db_path)  # second call must be a no-op

        conn = get_connection(db_path)
        row_after = dict(conn.execute(
            "SELECT * FROM publishing_outcomes WHERE episode_id='ep_real_001'"
        ).fetchone())
        count = conn.execute("SELECT COUNT(*) FROM publishing_outcomes").fetchone()[0]
        conn.close()

        assert row_before == row_after
        assert count == 1

    def test_new_deliverable_id_enables_multiple_rows_per_platform(self, tmp_path):
        """
        The actual capability the old UNIQUE(episode_id, platform)
        constraint could not provide: a second deliverable (e.g. a Short)
        for the same episode+platform now coexists with the main episode.
        """
        db_path = self._make_old_shape_db(tmp_path)
        initialize_schema(db_path)

        conn = get_connection(db_path)
        conn.execute(
            """
            INSERT INTO publishing_outcomes
                (outcome_id, episode_id, run_id, platform, deliverable_id,
                 publishing_status, platform_video_id, recorded_at)
            VALUES ('out2', 'ep_real_001', 'run_1', 'youtube', 'short_sh_001',
                    'success', 'yt_vid_short1', '2026-07-02T00:00:00Z')
            """
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM publishing_outcomes "
            "WHERE episode_id='ep_real_001' AND platform='youtube'"
        ).fetchone()[0]
        conn.close()

        assert count == 2

    def test_fresh_database_gets_new_shape_directly(self, tmp_path):
        """A brand-new database never touches the migration path at all —
        schema.sql's own CREATE TABLE IF NOT EXISTS already has the new shape."""
        db_path = tmp_path / "fresh.db"
        initialize_schema(db_path)

        conn = get_connection(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(publishing_outcomes)").fetchall()}
        conn.close()

        assert "deliverable_id" in cols
