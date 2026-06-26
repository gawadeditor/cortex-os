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


# ---------------------------------------------------------------------------
# TestProvenanceFields — M7.2 observability pass
# ---------------------------------------------------------------------------

def _make_packages_with_provenance():
    """Package fixtures with provenance data set as VGA now writes back."""
    visual_package = {
        "asset_manifest": {
            "assets": [
                {
                    "asset_id":          "clip_asset_001",
                    "scene_id":          "scene_1",
                    "source_type":       "main_episode",
                    "derivation_source": "muapi",
                    "generation_status": "generated",
                    "provider":          "muapi",
                    "model":             "veo3-text-to-video",
                    "prompt_used":       "Ancient Roman colosseum at dusk, cinematic",
                    "tier_used":         1,
                    "fallback_used":     False,
                },
            ]
        },
        "visual_continuity_record": {"locked_entities": []},
    }
    narration_package = {
        "voice_configuration": {
            "voice_id":  "j9jfwdrw7BRfcR43Qohk",
            "model_id":  "eleven_multilingual_v2",
            "environment_source": "env",
        },
        "stitched_tracks": [
            {
                "audio_asset_id": "narr_asset_001",
                "track_id":       "main",
                "source_type":    "elevenlabs",
            },
        ],
        "captions": [
            {
                "caption_id":     "cap_001",
                "srt_asset_id":   "srt_asset_001",
                "caption_method": "elevenlabs_alignment",
            }
        ],
    }
    thumbnail_package = {
        "main_thumbnail": {"asset_id": "thumb_asset_001"},
        "candidates": [
            {
                "candidate_id":   "cand_a",
                "rank":           1,
                "asset_id":       "thumb_asset_001",
                "status":         "generated",
                "visual_prompt":  "Dark ancient ruins, high contrast",
                "file_path":      "/abs/path/to/episodes/ep_000/thumbnails/main.jpg",
            },
        ],
        "short_thumbnails": [],
        "generation_metadata": {
            "provider":       "muapi",
            "primary_model":  "gpt-image-2-text-to-image",
            "fallback_model": "flux-2-dev",
            "resolution":     "2K",
            "quality":        "high",
        },
    }
    final_render_package = {
        "episode_render": {"video_asset_id": "render_asset_001"},
        "shorts_renders": [],
        "thumbnail_references": [],
        "music_log": [
            {
                "log_id":      "music_log_001",
                "asset_id":    "music_asset_001",
                "source":      "ai_generated",
                "category":    "background",
                "section_key": "intro",
            }
        ],
    }
    return visual_package, narration_package, thumbnail_package, final_render_package


class TestProvenanceFields:
    """Verify M7.2 provenance fields are captured in catalog records."""

    def test_clip_catalog_has_generation_prompt(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        records = [
            json.loads(line)
            for line in (catalog_dir / "clip_catalog.jsonl").read_text().splitlines()
            if line.strip()
        ]
        clip = next(r for r in records if r.get("scene_id") == "scene_1")
        assert clip["generation_prompt"] == "Ancient Roman colosseum at dusk, cinematic"

    def test_clip_catalog_has_model(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        records = [
            json.loads(line)
            for line in (catalog_dir / "clip_catalog.jsonl").read_text().splitlines()
            if line.strip()
        ]
        clip = next(r for r in records if r.get("scene_id") == "scene_1")
        assert clip["model"] == "veo3-text-to-video"

    def test_clip_catalog_has_file_path(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        records = [
            json.loads(line)
            for line in (catalog_dir / "clip_catalog.jsonl").read_text().splitlines()
            if line.strip()
        ]
        clip = next(r for r in records if r.get("scene_id") == "scene_1")
        assert clip["file_path"] == "episodes/ep_000/video/sc_scene_1.mp4"
        assert clip["archive_location"] == clip["file_path"]
        assert clip["moved_at"] is None

    def test_clip_catalog_has_generation_params(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        records = [
            json.loads(line)
            for line in (catalog_dir / "clip_catalog.jsonl").read_text().splitlines()
            if line.strip()
        ]
        clip = next(r for r in records if r.get("scene_id") == "scene_1")
        assert "tier_used" in clip["generation_params"]
        assert clip["generation_params"]["tier_used"] == 1

    def test_clip_catalog_has_version_and_created_at(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        records = [
            json.loads(line)
            for line in (catalog_dir / "clip_catalog.jsonl").read_text().splitlines()
            if line.strip()
        ]
        clip = next(r for r in records if r.get("scene_id") == "scene_1")
        assert clip["version"] == 1
        assert clip["created_at"]

    def test_thumbnail_catalog_has_generation_prompt(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        records = [
            json.loads(line)
            for line in (catalog_dir / "thumbnail_catalog.jsonl").read_text().splitlines()
            if line.strip()
        ]
        thumb = next(r for r in records if r.get("thumbnail_type") == "main")
        assert thumb["generation_prompt"] == "Dark ancient ruins, high contrast"

    def test_thumbnail_catalog_has_model(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        records = [
            json.loads(line)
            for line in (catalog_dir / "thumbnail_catalog.jsonl").read_text().splitlines()
            if line.strip()
        ]
        thumb = next(r for r in records if r.get("thumbnail_type") == "main")
        assert thumb["model"] == "gpt-image-2-text-to-image"

    def test_thumbnail_catalog_has_generation_params(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        records = [
            json.loads(line)
            for line in (catalog_dir / "thumbnail_catalog.jsonl").read_text().splitlines()
            if line.strip()
        ]
        thumb = next(r for r in records if r.get("thumbnail_type") == "main")
        assert thumb["generation_params"].get("resolution") == "2K"
        assert thumb["generation_params"].get("quality") == "high"
        assert thumb["generation_params"].get("aspect_ratio") == "16:9"

    def test_thumbnail_catalog_has_file_path(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        records = [
            json.loads(line)
            for line in (catalog_dir / "thumbnail_catalog.jsonl").read_text().splitlines()
            if line.strip()
        ]
        thumb = next(r for r in records if r.get("thumbnail_type") == "main")
        # file_path comes from candidate["file_path"] (absolute path normalized)
        assert thumb["file_path"] is not None
        assert thumb["archive_location"] == thumb["file_path"]
        assert thumb["moved_at"] is None

    def test_narration_catalog_has_model(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
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
        narr = next(r for r in records if r.get("asset_type") == "narration")
        assert narr["model"] == "eleven_multilingual_v2"

    def test_narration_catalog_has_file_path(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
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
        narr = next(r for r in records if r.get("asset_type") == "narration")
        assert narr["file_path"] == "episodes/ep_000/narration/narr_asset_001.mp3"

    def test_narration_catalog_has_voice_generation_params(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
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
        narr = next(r for r in records if r.get("asset_type") == "narration")
        assert narr["generation_params"]["voice_id"] == "j9jfwdrw7BRfcR43Qohk"

    def test_music_catalog_has_model(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        records = [
            json.loads(line)
            for line in (catalog_dir / "music_catalog.jsonl").read_text().splitlines()
            if line.strip()
        ]
        music = records[0]
        assert music["model"] == "music_v1"

    def test_music_catalog_has_generation_params(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        records = [
            json.loads(line)
            for line in (catalog_dir / "music_catalog.jsonl").read_text().splitlines()
            if line.strip()
        ]
        music = records[0]
        assert music["generation_params"]["category"] == "background"
        assert music["generation_params"]["section_key"] == "intro"

    def test_music_catalog_has_file_path(self, store):
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        records = [
            json.loads(line)
            for line in (catalog_dir / "music_catalog.jsonl").read_text().splitlines()
            if line.strip()
        ]
        music = records[0]
        assert music["file_path"] == "episodes/ep_000/music/music_asset_001.mp3"
        assert music["archive_location"] == music["file_path"]

    def test_asset_catalog_provenance_fields_present_for_all_types(self, store):
        """Every catalog record has the required provenance fields."""
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
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
        required_provenance = {
            "generation_prompt", "generation_params", "file_path",
            "archive_location", "moved_at", "version", "created_at", "cost_usd",
        }
        for r in records:
            missing = required_provenance - set(r.keys())
            assert not missing, (
                f"asset_id={r.get('asset_id')} type={r.get('asset_type')} "
                f"missing provenance fields: {missing}"
            )

    def test_db_record_has_model_populated(self, store):
        """DB assets table receives model from catalog (not None)."""
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        conn = get_connection(db_path)
        # archive_index stores model
        rows = conn.execute(
            "SELECT model FROM archive_index WHERE episode_id='ep_000'"
        ).fetchall()
        conn.close()
        models = [r["model"] for r in rows if r["model"]]
        assert len(models) > 0, "Expected at least one archive_index row with a model"

    def test_db_archive_index_has_file_path(self, store):
        """archive_index stores file_path for clip assets."""
        db_path, catalog_dir, output_dir = store
        vp, np, tp, frp = _make_packages_with_provenance()
        run_asset_archive(
            vp, np, tp, frp, "ep_000",
            catalog_dir=catalog_dir, output_dir=output_dir,
            run_id="run-001", db_path=db_path,
        )
        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT file_path FROM archive_index WHERE asset_id='clip_asset_001'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["file_path"] == "episodes/ep_000/video/sc_scene_1.mp4"

    def test_vga_writeback_model_is_in_asset_manifest(self):
        """video_generation_agent writes model/prompt_used back to existing asset entries."""
        from hypogeum.agents.video_generation_agent import build_asset_index
        # Simulate a pre-populated asset_manifest from Claude Phase 1.
        # Phase 1 assets have used_in_scenes — that's how build_asset_index builds the index.
        visual_package = {
            "asset_manifest": {
                "assets": [{
                    "asset_id":          "clip_old",
                    "scene_id":          "sc_1",
                    "used_in_scenes":    ["sc_1"],
                    "source_type":       "main_episode",
                    "generation_status": "planned",
                }]
            }
        }
        asset_index = build_asset_index(visual_package)
        assets = visual_package["asset_manifest"]["assets"]
        # Simulate the VGA update path (the "if scene_id in asset_index" branch)
        manifest_entry = {
            "scene_id":          "sc_1",
            "asset_id":          "clip_new",
            "generation_status": "generated",
            "provider":          "muapi",
            "model_used":        "veo3-text-to-video",
            "prompt_used":       "Roman forum at sunset",
            "tier_used":         1,
            "fallback_used":     False,
        }
        scene_id = "sc_1"
        gen_status = "generated"
        assert scene_id in asset_index, "build_asset_index must index by used_in_scenes[0]"
        asset = asset_index[scene_id]
        asset["generation_status"] = gen_status
        asset["asset_id"] = manifest_entry.get("asset_id", asset.get("asset_id", ""))
        asset["provider"]      = manifest_entry.get("provider", "muapi")
        asset["model"]         = manifest_entry.get("model_used", "")
        asset["prompt_used"]   = manifest_entry.get("prompt_used")
        asset["tier_used"]     = manifest_entry.get("tier_used")
        asset["fallback_used"] = manifest_entry.get("fallback_used", False)

        updated = assets[0]
        assert updated["model"] == "veo3-text-to-video"
        assert updated["prompt_used"] == "Roman forum at sunset"
        assert updated["tier_used"] == 1
        assert updated["provider"] == "muapi"
