"""
tests/test_asset_archive_wiring.py — Phase 3A: Asset Archive Wiring test suite

Coverage:
    TestRunAssetArchiveRunId     — run_id embedded in JSONL and returned record
    TestRunAssetArchiveDB        — assets written to operational DB with correct run_id
    TestRunAssetArchiveFields    — required fields present in catalog records
    TestRunAssetArchiveIdempotent — re-run does not duplicate DB rows
    TestRunAssetArchiveNoRunId   — silent no-op for DB when run_id omitted
    TestPipelineIntegration      — asset_archive_record present in PipelineResult
    TestSchemaValidation         — asset_archive_record schema validates correctly
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.agents.asset_archive_agent import run_asset_archive
from hypogeum.db import get_connection, initialize_schema, insert_run
from hypogeum.identifiers import METADATA_VERSION


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    """Returns (db_path, catalog_dir, output_dir) with initialized schema + one run row."""
    db_path = tmp_path / "hypogeum.db"
    initialize_schema(db_path)
    conn = get_connection(db_path)
    insert_run(conn, "run-001", "ep_000", "2026-06-12T10:00:00Z")
    conn.commit()
    conn.close()
    catalog_dir = tmp_path / "catalogs"
    output_dir = tmp_path / "episodes" / "ep_000"
    return db_path, catalog_dir, output_dir


def _make_packages():
    """Minimal package fixtures with one asset of each type."""
    visual_package = {
        "asset_manifest": {
            "assets": [
                {"asset_id": "clip_asset_001", "scene_id": "scene_1",
                 "source_type": "main_episode", "derivation_source": "muapi"},
            ]
        },
        "visual_continuity_record": {"locked_entities": []},
    }
    narration_package = {
        "stitched_tracks": [
            {"audio_asset_id": "narr_asset_001", "track_id": "t1", "source_type": "elevenlabs"},
        ],
        "captions": [],
    }
    thumbnail_package = {
        "main_thumbnail": {"asset_id": "thumb_asset_001"},
        "short_thumbnails": [],
    }
    final_render_package = {
        "episode_render": {"video_asset_id": "render_asset_001"},
        "shorts_renders": [],
        "thumbnail_references": [],
        "music_log": [
            {
                "log_id": "music_log_001",
                "asset_id": "music_asset_001",
                "source": "ai_generated",
                "category": "background",
                "section_key": "intro",
            }
        ],
    }
    return visual_package, narration_package, thumbnail_package, final_render_package


# ---------------------------------------------------------------------------
# TestRunAssetArchiveRunId
# ---------------------------------------------------------------------------

class TestRunAssetArchiveRunId:
    def test_returned_record_contains_run_id(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        record = run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        assert record["run_id"] == "run-001"

    def test_returned_record_has_metadata_version(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        record = run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        assert record["metadata_version"] == METADATA_VERSION

    def test_asset_catalog_records_have_run_id(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        asset_catalog = catalog_dir / "asset_catalog.jsonl"
        assert asset_catalog.exists()
        records = [json.loads(line) for line in asset_catalog.read_text().splitlines() if line.strip()]
        assert len(records) > 0
        for r in records:
            assert r.get("run_id") == "run-001", f"Missing run_id in record: {r}"

    def test_catalog_records_have_metadata_version(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        records = [
            json.loads(line)
            for line in (catalog_dir / "asset_catalog.jsonl").read_text().splitlines()
            if line.strip()
        ]
        for r in records:
            assert r.get("metadata_version") == METADATA_VERSION


# ---------------------------------------------------------------------------
# TestRunAssetArchiveDB
# ---------------------------------------------------------------------------

class TestRunAssetArchiveDB:
    def test_assets_written_to_db(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        conn = get_connection(db_path)
        rows = conn.execute("SELECT * FROM assets WHERE run_id='run-001'").fetchall()
        conn.close()
        assert len(rows) > 0

    def test_db_records_have_correct_run_id(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        conn = get_connection(db_path)
        rows = conn.execute("SELECT run_id FROM assets").fetchall()
        conn.close()
        for row in rows:
            assert row["run_id"] == "run-001"

    def test_db_records_have_episode_id(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        conn = get_connection(db_path)
        rows = conn.execute("SELECT episode_id FROM assets").fetchall()
        conn.close()
        for row in rows:
            assert row["episode_id"] == "ep_000"

    def test_db_records_have_asset_type(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        conn = get_connection(db_path)
        rows = conn.execute("SELECT asset_type FROM assets WHERE run_id='run-001'").fetchall()
        conn.close()
        asset_types = {row["asset_type"] for row in rows}
        # Should have at least clip, thumbnail, narration, music types
        assert len(asset_types) > 0
        for at in asset_types:
            assert at in ("clip", "video_clip", "thumbnail", "narration", "music", "caption", "")

    def test_clip_written_to_db(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT asset_id FROM assets WHERE asset_id='clip_asset_001'"
        ).fetchone()
        conn.close()
        assert row is not None

    def test_thumbnail_written_to_db(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT asset_id FROM assets WHERE asset_id='thumb_asset_001'"
        ).fetchone()
        conn.close()
        assert row is not None

    def test_narration_written_to_db(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT asset_id FROM assets WHERE asset_id='narr_asset_001'"
        ).fetchone()
        conn.close()
        assert row is not None


# ---------------------------------------------------------------------------
# TestRunAssetArchiveFields
# ---------------------------------------------------------------------------

class TestRunAssetArchiveFields:
    def test_catalog_records_have_episode_id(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        records = [
            json.loads(line)
            for line in (catalog_dir / "asset_catalog.jsonl").read_text().splitlines()
            if line.strip()
        ]
        for r in records:
            assert r.get("source_episode_id") == "ep_000"

    def test_catalog_records_have_provider(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        records = [
            json.loads(line)
            for line in (catalog_dir / "asset_catalog.jsonl").read_text().splitlines()
            if line.strip()
        ]
        for r in records:
            assert "provider" in r

    def test_catalog_records_have_indexed_at(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        records = [
            json.loads(line)
            for line in (catalog_dir / "asset_catalog.jsonl").read_text().splitlines()
            if line.strip()
        ]
        for r in records:
            assert "indexed_at" in r

    def test_returned_record_has_required_fields(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        record = run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        for field in ("asset_archive_id", "episode_id", "run_id", "cataloged_at",
                      "verdict", "cataloged", "checks", "failures", "warnings",
                      "catalog_summary", "metadata_version"):
            assert field in record, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# TestRunAssetArchiveIdempotent
# ---------------------------------------------------------------------------

class TestRunAssetArchiveIdempotent:
    def test_second_run_does_not_duplicate_db_rows(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        conn = get_connection(db_path)
        count = conn.execute("SELECT COUNT(*) FROM assets WHERE run_id='run-001'").fetchone()[0]
        conn.close()
        # Same assets — second run should not add more (INSERT OR IGNORE)
        conn2 = get_connection(db_path)
        count2 = conn2.execute("SELECT COUNT(DISTINCT asset_id) FROM assets WHERE run_id='run-001'").fetchone()[0]
        conn2.close()
        assert count == count2

    def test_second_run_verdict_is_warning_or_cataloged(self, store):
        """Second run with same assets produces WARNING (idempotent re-run) or CATALOGED."""
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        record2 = run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        assert record2["verdict"] in ("WARNING", "CATALOGED")


# ---------------------------------------------------------------------------
# TestRunAssetArchiveNoRunId
# ---------------------------------------------------------------------------

class TestRunAssetArchiveNoRunId:
    def test_no_run_id_still_writes_catalogs(self, tmp_path):
        """Without run_id, JSONL catalogs are still written (DB write is skipped)."""
        vp, np, tp, frp = _make_packages()
        catalog_dir = tmp_path / "catalogs"
        output_dir = tmp_path / "episodes" / "ep_000"
        record = run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
        )
        assert (catalog_dir / "asset_catalog.jsonl").exists()
        assert record["cataloged"] is True

    def test_no_run_id_record_run_id_is_empty(self, tmp_path):
        vp, np, tp, frp = _make_packages()
        catalog_dir = tmp_path / "catalogs"
        output_dir = tmp_path / "episodes" / "ep_000"
        record = run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
        )
        assert record["run_id"] == ""


# ---------------------------------------------------------------------------
# TestPipelineIntegration
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    def test_pipeline_result_has_asset_archive_record_field(self):
        """PipelineResult dataclass must have asset_archive_record field."""
        import dataclasses
        from hypogeum.pipeline import PipelineResult
        field_names = {f.name for f in dataclasses.fields(PipelineResult)}
        assert "asset_archive_record" in field_names

    def test_asset_archive_record_default_is_none(self):
        import dataclasses
        from hypogeum.pipeline import PipelineResult
        fields = {f.name: f for f in dataclasses.fields(PipelineResult)}
        assert fields["asset_archive_record"].default is None

    def test_pipeline_imports_run_asset_archive(self):
        import inspect
        from hypogeum import pipeline
        src = inspect.getsource(pipeline)
        assert "run_asset_archive" in src

    def test_pipeline_passes_run_id_to_asset_archive(self):
        import inspect
        from hypogeum import pipeline
        src = inspect.getsource(pipeline)
        # _run_asset_archive must accept run_id
        assert "run_id=run_id" in src or "run_id=" in src


# ---------------------------------------------------------------------------
# TestSchemaValidation
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_schema_registered(self):
        from hypogeum.schemas import _VALIDATORS
        assert "asset_archive_record" in _VALIDATORS

    def test_valid_record_passes(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages()
        record = run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        from hypogeum.schemas import validate
        # Should not raise
        validate("asset_archive_record", record)

    def test_missing_episode_id_fails(self):
        from hypogeum.schemas import validate, SchemaError
        bad = {
            "asset_archive_id": "aa_abc",
            "cataloged_at": "2026-01-01T00:00:00Z",
            "verdict": "CATALOGED",
            "cataloged": True,
            "checks": [],
            "failures": [],
            "warnings": [],
            "catalog_summary": {
                "clips_indexed": 0, "thumbnails_indexed": 0,
                "music_tracks_indexed": 0, "entities_indexed": 0,
                "total_assets_indexed": 0,
            },
        }
        with pytest.raises(SchemaError):
            validate("asset_archive_record", bad)
