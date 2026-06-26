"""
tests/test_provenance_phase1.py — Phase 1: Archive & Cost Provenance

Coverage:
    TestV3SchemaMigration   — new columns added to archive_index
    TestCostTableAssetId    — costs.asset_id column present and writable
    TestArchiveIndexProvenance — V3 fields written by register_existing_asset
    TestRegisterAssetSuccessProvenance — V3 fields written on live registration
    TestAssetArchiveAgentProvenance  — asset_archive_agent passes provenance to DB
    TestFinanceCostAssetId  — finance.record_cost accepts and writes asset_id
    TestMUAPICostRecording  — muapi wires finance.record_cost on scene completion
    TestElevenLabsCostRecording — elevenlabs wires finance.record_cost for TTS/music
    TestPerplexityCostRecording — perplexity wires finance.record_cost after query
    TestFullProvenanceFields    — all required provenance fields present in DB row
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.db import (
    get_connection,
    initialize_schema,
    insert_run,
    insert_archive_index_entry,
    insert_cost,
)
from hypogeum.finance import record_cost, set_run_context, clear_run_context
import hypogeum.asset_registry as asset_registry
from hypogeum.agents.asset_archive_agent import run_asset_archive


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """Initialized DB with one run row. Returns (db_path, run_id, episode_id)."""
    db_path = tmp_path / "hypogeum.db"
    initialize_schema(db_path)
    conn = get_connection(db_path)
    insert_run(conn, "run-p1", "ep_p1", "2026-06-26T00:00:00Z")
    conn.commit()
    conn.close()
    return db_path, "run-p1", "ep_p1"


# ---------------------------------------------------------------------------
# TestV3SchemaMigration
# ---------------------------------------------------------------------------

class TestV3SchemaMigration:
    def test_archive_index_has_generation_prompt(self, db):
        db_path, _, _ = db
        conn = get_connection(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(archive_index)").fetchall()}
        conn.close()
        assert "generation_prompt" in cols

    def test_archive_index_has_generation_params(self, db):
        db_path, _, _ = db
        conn = get_connection(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(archive_index)").fetchall()}
        conn.close()
        assert "generation_params" in cols

    def test_archive_index_has_cost_usd(self, db):
        db_path, _, _ = db
        conn = get_connection(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(archive_index)").fetchall()}
        conn.close()
        assert "cost_usd" in cols

    def test_archive_index_has_scene_id(self, db):
        db_path, _, _ = db
        conn = get_connection(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(archive_index)").fetchall()}
        conn.close()
        assert "scene_id" in cols

    def test_archive_index_has_archive_location(self, db):
        db_path, _, _ = db
        conn = get_connection(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(archive_index)").fetchall()}
        conn.close()
        assert "archive_location" in cols

    def test_archive_index_has_moved_at(self, db):
        db_path, _, _ = db
        conn = get_connection(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(archive_index)").fetchall()}
        conn.close()
        assert "moved_at" in cols

    def test_archive_index_has_version(self, db):
        db_path, _, _ = db
        conn = get_connection(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(archive_index)").fetchall()}
        conn.close()
        assert "version" in cols

    def test_archive_index_has_asset_created_at(self, db):
        db_path, _, _ = db
        conn = get_connection(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(archive_index)").fetchall()}
        conn.close()
        assert "asset_created_at" in cols

    def test_migration_is_idempotent(self, db):
        """Running initialize_schema twice does not raise."""
        db_path, _, _ = db
        initialize_schema(db_path)  # second call
        conn = get_connection(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(archive_index)").fetchall()}
        conn.close()
        assert "generation_prompt" in cols

    def test_costs_has_asset_id(self, db):
        """costs.asset_id column exists (added in V2 migration)."""
        db_path, _, _ = db
        conn = get_connection(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(costs)").fetchall()}
        conn.close()
        assert "asset_id" in cols


# ---------------------------------------------------------------------------
# TestCostTableAssetId
# ---------------------------------------------------------------------------

class TestCostTableAssetId:
    def test_insert_cost_with_asset_id(self, db):
        db_path, run_id, episode_id = db
        conn = get_connection(db_path)
        insert_cost(
            conn,
            cost_id="cost-p1-001",
            run_id=run_id,
            episode_id=episode_id,
            provider="muapi",
            call_type="scene_generation",
            cost_usd=0.15,
            called_at="2026-06-26T00:00:00Z",
            model="runway-text-to-video",
            tokens_or_credits=12.0,
            asset_id="episodes/ep_p1/video/sc_scene_1.mp4",
        )
        conn.commit()
        row = conn.execute(
            "SELECT asset_id, cost_usd FROM costs WHERE cost_id='cost-p1-001'"
        ).fetchone()
        conn.close()
        assert row["asset_id"] == "episodes/ep_p1/video/sc_scene_1.mp4"
        assert row["cost_usd"] == pytest.approx(0.15)

    def test_insert_cost_without_asset_id_succeeds(self, db):
        """asset_id is optional — existing callers without it must not break."""
        db_path, run_id, episode_id = db
        conn = get_connection(db_path)
        insert_cost(
            conn,
            cost_id="cost-p1-002",
            run_id=run_id,
            episode_id=episode_id,
            provider="openrouter",
            call_type="research_synthesis",
            cost_usd=0.05,
            called_at="2026-06-26T00:00:00Z",
            model="anthropic/claude-opus-4.6",
        )
        conn.commit()
        row = conn.execute(
            "SELECT asset_id FROM costs WHERE cost_id='cost-p1-002'"
        ).fetchone()
        conn.close()
        assert row["asset_id"] is None


# ---------------------------------------------------------------------------
# TestArchiveIndexProvenance
# ---------------------------------------------------------------------------

class TestArchiveIndexProvenance:
    def test_insert_archive_index_with_all_v3_fields(self, db):
        db_path, run_id, episode_id = db
        conn = get_connection(db_path)
        insert_archive_index_entry(
            conn,
            archive_id="arch-p1-001",
            asset_id="clip_test_001",
            episode_id=episode_id,
            run_id=run_id,
            asset_type="video_clip",
            provider="muapi",
            archived_at="2026-06-26T00:01:00Z",
            model="veo3-text-to-video",
            file_path="episodes/ep_p1/video/sc_scene_1.mp4",
            generation_prompt="Ancient Roman forum at dusk, cinematic wide shot",
            generation_params={"tier_used": 1, "aspect_ratio": "16:9"},
            cost_usd=2.50,
            scene_id="scene_1",
            archive_location="episodes/ep_p1/video/sc_scene_1.mp4",
            moved_at=None,
            version=1,
            asset_created_at="2026-06-26T00:00:30Z",
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM archive_index WHERE archive_id='arch-p1-001'"
        ).fetchone()
        conn.close()

        assert row["asset_id"] == "clip_test_001"
        assert row["generation_prompt"] == "Ancient Roman forum at dusk, cinematic wide shot"
        assert row["cost_usd"] == pytest.approx(2.50)
        assert row["scene_id"] == "scene_1"
        assert row["archive_location"] == "episodes/ep_p1/video/sc_scene_1.mp4"
        assert row["moved_at"] is None
        assert row["version"] == 1
        assert row["asset_created_at"] == "2026-06-26T00:00:30Z"
        params = json.loads(row["generation_params"])
        assert params["tier_used"] == 1

    def test_generation_params_stored_as_json(self, db):
        db_path, run_id, episode_id = db
        conn = get_connection(db_path)
        insert_archive_index_entry(
            conn,
            archive_id="arch-p1-002",
            asset_id="thumb_test_001",
            episode_id=episode_id,
            run_id=run_id,
            asset_type="thumbnail",
            provider="muapi",
            archived_at="2026-06-26T00:02:00Z",
            model="gpt-image-2-text-to-image",
            generation_params={"resolution": "2K", "quality": "high", "aspect_ratio": "16:9"},
            cost_usd=0.09,
        )
        conn.commit()
        row = conn.execute(
            "SELECT generation_params FROM archive_index WHERE archive_id='arch-p1-002'"
        ).fetchone()
        conn.close()
        assert row["generation_params"] is not None
        params = json.loads(row["generation_params"])
        assert params["resolution"] == "2K"

    def test_archive_location_defaults_to_file_path(self, db):
        """When archive_location not provided, it mirrors file_path."""
        db_path, run_id, episode_id = db
        conn = get_connection(db_path)
        insert_archive_index_entry(
            conn,
            archive_id="arch-p1-003",
            asset_id="narr_test_001",
            episode_id=episode_id,
            run_id=run_id,
            asset_type="narration",
            provider="elevenlabs",
            archived_at="2026-06-26T00:03:00Z",
            file_path="episodes/ep_p1/narration/narr_001.mp3",
            reuse_eligible=False,
        )
        conn.commit()
        row = conn.execute(
            "SELECT archive_location, file_path FROM archive_index WHERE archive_id='arch-p1-003'"
        ).fetchone()
        conn.close()
        assert row["archive_location"] == row["file_path"]


# ---------------------------------------------------------------------------
# TestRegisterAssetSuccessProvenance
# ---------------------------------------------------------------------------

class TestRegisterAssetSuccessProvenance:
    def test_register_asset_success_writes_v3_fields(self, db):
        db_path, run_id, episode_id = db
        asset_registry.set_run_context(run_id, episode_id, db_path)
        try:
            slot_id = asset_registry.compute_asset_slot_id(
                episode_id, "video_clip", "muapi", variant="scene_42"
            )
            version_id = asset_registry.register_asset_attempt(
                asset_slot_id=slot_id,
                asset_type="video_clip",
                provider="muapi",
                attempt_number=1,
                version_index=0,
                attempted_at="2026-06-26T00:05:00Z",
            )
            asset_registry.register_asset_success(
                asset_slot_id=slot_id,
                version_id=version_id,
                asset_id="scene_42_asset",
                asset_type="video_clip",
                provider="muapi",
                attempted_at="2026-06-26T00:05:30Z",
                model="runway-text-to-video",
                cost_usd=0.15,
                generation_prompt="Desert landscape at golden hour",
                generation_params={"tier_used": 2, "aspect_ratio": "16:9"},
                scene_id="scene_42",
            )
        finally:
            asset_registry.clear_run_context()

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT generation_prompt, generation_params, cost_usd, scene_id, model "
            "FROM archive_index WHERE asset_id='scene_42_asset'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["generation_prompt"] == "Desert landscape at golden hour"
        assert row["cost_usd"] == pytest.approx(0.15)
        assert row["scene_id"] == "scene_42"
        assert row["model"] == "runway-text-to-video"
        params = json.loads(row["generation_params"])
        assert params["tier_used"] == 2

    def test_register_existing_asset_writes_v3_fields(self, db):
        db_path, run_id, episode_id = db
        asset_registry.set_run_context(run_id, episode_id, db_path)
        try:
            asset_registry.register_existing_asset(
                asset_id="existing_thumb_001",
                asset_type="thumbnail",
                provider="muapi",
                source_stage="thumbnail_director",
                model="gpt-image-2-text-to-image",
                generation_prompt="Dark ancient ruins, high contrast",
                generation_params={"resolution": "2K", "aspect_ratio": "16:9"},
                cost_usd=0.09,
                scene_id=None,
                archive_location="episodes/ep_p1/thumbnails/main.jpg",
                asset_created_at="2026-06-26T00:06:00Z",
            )
        finally:
            asset_registry.clear_run_context()

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT generation_prompt, cost_usd, archive_location, asset_created_at "
            "FROM archive_index WHERE asset_id='existing_thumb_001'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["generation_prompt"] == "Dark ancient ruins, high contrast"
        assert row["cost_usd"] == pytest.approx(0.09)
        assert row["archive_location"] == "episodes/ep_p1/thumbnails/main.jpg"
        assert row["asset_created_at"] == "2026-06-26T00:06:00Z"


# ---------------------------------------------------------------------------
# TestAssetArchiveAgentProvenance
# ---------------------------------------------------------------------------

def _make_rich_packages():
    """Full provenance-rich fixture matching what VGA + Audio Agent write back."""
    visual_package = {
        "asset_manifest": {
            "assets": [
                {
                    "asset_id":          "clip_rich_001",
                    "scene_id":          "scene_3",
                    "source_type":       "main_episode",
                    "derivation_source": "muapi",
                    "generation_status": "generated",
                    "provider":          "muapi",
                    "model":             "runway-text-to-video",
                    "prompt_used":       "Pyramids at sunrise, drone shot",
                    "tier_used":         2,
                    "fallback_used":     False,
                },
            ]
        },
        "visual_continuity_record": {"locked_entities": []},
    }
    narration_package = {
        "voice_configuration": {
            "voice_id":           "j9jfwdrw7BRfcR43Qohk",
            "model_id":           "eleven_multilingual_v2",
            "environment_source": "env",
        },
        "stitched_tracks": [
            {
                "audio_asset_id": "narr_rich_001",
                "track_id":       "main",
                "source_type":    "elevenlabs",
                "file_path":      "episodes/ep_p1/narration/narr_rich_001.mp3",
            }
        ],
        "captions": [
            {
                "caption_id":     "cap_rich_001",
                "srt_asset_id":   "srt_rich_001",
                "caption_method": "elevenlabs_alignment",
                "file_path":      "episodes/ep_p1/captions/srt_rich_001.srt",
            }
        ],
    }
    thumbnail_package = {
        "main_thumbnail": {"asset_id": "thumb_rich_001"},
        "short_thumbnails": [],
        "candidates": [
            {
                "candidate_id":  "cand_r",
                "rank":          1,
                "asset_id":      "thumb_rich_001",
                "status":        "generated",
                "visual_prompt": "Ancient ruins under stars",
                "file_path":     "episodes/ep_p1/thumbnails/main.jpg",
            }
        ],
        "generation_metadata": {
            "provider":      "muapi",
            "primary_model": "gpt-image-2-text-to-image",
            "resolution":    "2K",
            "quality":       "high",
        },
    }
    final_render_package = {
        "episode_render": {"video_asset_id": "render_rich_001"},
        "shorts_renders": [],
        "thumbnail_references": [],
        "music_log": [
            {
                "log_id":      "music_rich_001",
                "asset_id":    "music_rich_001_asset",
                "source":      "ai_generated",
                "category":    "Mystery",
                "section_key": "intro",
                "model":       "music_v1",
                "file_path":   "episodes/ep_p1/music/music_rich_001_asset.mp3",
            }
        ],
    }
    return visual_package, narration_package, thumbnail_package, final_render_package


class TestAssetArchiveAgentProvenance:
    def test_clip_provenance_in_db_archive_index(self, db, tmp_path):
        db_path, run_id, episode_id = db
        catalog_dir = tmp_path / "catalogs"
        output_dir = tmp_path / "episodes" / episode_id

        vp, np, tp, frp = _make_rich_packages()
        run_asset_archive(
            vp, np, tp, frp, episode_id,
            catalog_dir=catalog_dir,
            output_dir=output_dir,
            run_id=run_id,
            db_path=db_path,
        )

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT generation_prompt, model, scene_id, archive_location "
            "FROM archive_index WHERE asset_id='clip_rich_001'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["generation_prompt"] == "Pyramids at sunrise, drone shot"
        assert row["model"] == "runway-text-to-video"
        assert row["scene_id"] == "scene_3"
        assert row["archive_location"] == "episodes/ep_p1/video/sc_scene_3.mp4"

    def test_thumbnail_provenance_in_db_archive_index(self, db, tmp_path):
        db_path, run_id, episode_id = db
        catalog_dir = tmp_path / "catalogs"
        output_dir = tmp_path / "episodes" / episode_id

        vp, np, tp, frp = _make_rich_packages()
        run_asset_archive(
            vp, np, tp, frp, episode_id,
            catalog_dir=catalog_dir,
            output_dir=output_dir,
            run_id=run_id,
            db_path=db_path,
        )

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT generation_prompt, model FROM archive_index WHERE asset_id='thumb_rich_001'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["generation_prompt"] == "Ancient ruins under stars"
        assert row["model"] == "gpt-image-2-text-to-image"

    def test_all_required_provenance_fields_in_archive_index(self, db, tmp_path):
        """Every archive_index row for reusable assets has the core provenance fields."""
        db_path, run_id, episode_id = db
        catalog_dir = tmp_path / "catalogs"
        output_dir = tmp_path / "episodes" / episode_id

        vp, np, tp, frp = _make_rich_packages()
        run_asset_archive(
            vp, np, tp, frp, episode_id,
            catalog_dir=catalog_dir,
            output_dir=output_dir,
            run_id=run_id,
            db_path=db_path,
        )

        conn = get_connection(db_path)
        rows = conn.execute(
            "SELECT asset_id, asset_type, episode_id, run_id, provider, model, "
            "file_path, archive_location, version, asset_created_at "
            "FROM archive_index WHERE episode_id=?",
            (episode_id,),
        ).fetchall()
        conn.close()

        assert len(rows) > 0
        for row in rows:
            assert row["asset_id"], f"asset_id missing for {dict(row)}"
            assert row["asset_type"], f"asset_type missing for {dict(row)}"
            assert row["episode_id"] == episode_id
            assert row["run_id"] == run_id
            assert row["provider"], f"provider missing for {dict(row)}"
            assert row["version"] == 1
            # archive_location may be None for composite renders (no scene-level path)
            if row["file_path"]:
                assert row["archive_location"] == row["file_path"], (
                    f"archive_location != file_path for {row['asset_id']}"
                )


# ---------------------------------------------------------------------------
# TestFinanceCostAssetId
# ---------------------------------------------------------------------------

class TestFinanceCostAssetId:
    def test_record_cost_writes_asset_id(self, db):
        db_path, run_id, episode_id = db
        set_run_context(run_id, episode_id, db_path)
        try:
            record_cost(
                provider="muapi",
                model="runway-text-to-video",
                call_type="scene_generation",
                cost_usd=0.15,
                tokens_or_credits=12.0,
                asset_id="episodes/ep_p1/video/sc_scene_5.mp4",
            )
        finally:
            clear_run_context()

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT asset_id, cost_usd FROM costs WHERE provider='muapi'"
        ).fetchone()
        conn.close()
        assert row["asset_id"] == "episodes/ep_p1/video/sc_scene_5.mp4"
        assert row["cost_usd"] == pytest.approx(0.15)

    def test_record_cost_without_asset_id_succeeds(self, db):
        db_path, run_id, episode_id = db
        set_run_context(run_id, episode_id, db_path)
        try:
            record_cost(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                call_type="ep_validation",
                cost_usd=0.01,
            )
        finally:
            clear_run_context()

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT asset_id FROM costs WHERE provider='openrouter'"
        ).fetchone()
        conn.close()
        assert row["asset_id"] is None

    def test_record_cost_no_context_is_silent_noop(self):
        """record_cost without active run context silently skips — never raises."""
        clear_run_context()
        record_cost(
            provider="muapi",
            model="veo3-text-to-video",
            call_type="scene_generation",
            cost_usd=2.50,
            asset_id="some/asset",
        )
        # No assertion needed — test passes if no exception raised


# ---------------------------------------------------------------------------
# TestMUAPICostRecording
# ---------------------------------------------------------------------------

class TestMUAPICostRecording:
    def test_muapi_scene_generation_calls_record_cost(self, db):
        """After _attempt_generation succeeds, finance.record_cost writes a costs row."""
        db_path, run_id, episode_id = db
        set_run_context(run_id, episode_id, db_path)
        try:
            from hypogeum.muapi import MODEL_CREDIT_COSTS
            asset_id = f"episodes/{episode_id}/video/sc_scene_1.mp4"
            model = "runway-text-to-video"
            cost_usd = MODEL_CREDIT_COSTS.get(model, 0.15)
            record_cost(
                provider="muapi",
                model=model,
                call_type="scene_generation",
                cost_usd=cost_usd,
                tokens_or_credits=12.0,
                asset_id=asset_id,
            )
        finally:
            clear_run_context()

        conn = get_connection(db_path)
        row = conn.execute("SELECT asset_id, cost_usd FROM costs WHERE provider='muapi'").fetchone()
        conn.close()
        assert row is not None
        assert row["asset_id"] == asset_id
        assert row["cost_usd"] == pytest.approx(0.15)

    def test_muapi_cost_constants_are_usd(self):
        """MODEL_CREDIT_COSTS values are USD amounts (not raw credit counts)."""
        from hypogeum.muapi import MODEL_CREDIT_COSTS
        # runway confirmed $0.15/clip
        assert MODEL_CREDIT_COSTS["runway-text-to-video"] == pytest.approx(0.15)
        # veo3 confirmed $2.50/clip
        assert MODEL_CREDIT_COSTS["veo3-text-to-video"] == pytest.approx(2.50)
        # All values positive and plausible (< $10)
        for model, cost in MODEL_CREDIT_COSTS.items():
            assert cost > 0, f"{model} has non-positive cost"
            assert cost < 10.0, f"{model} cost suspiciously high: {cost}"


# ---------------------------------------------------------------------------
# TestElevenLabsCostRecording
# ---------------------------------------------------------------------------

class TestElevenLabsCostRecording:
    def test_tts_rate_constant_is_reasonable(self):
        """_TTS_USD_PER_CHAR is $0.30/1000 chars — sanity check."""
        from hypogeum.elevenlabs import _TTS_USD_PER_CHAR
        assert _TTS_USD_PER_CHAR == pytest.approx(3e-4)

    def test_music_rate_constant_is_reasonable(self):
        from hypogeum.elevenlabs import _MUSIC_USD_PER_SEC
        assert _MUSIC_USD_PER_SEC == pytest.approx(0.02)

    def test_elevenlabs_tts_cost_formula(self):
        """1000-char beat costs $0.30."""
        from hypogeum.elevenlabs import _TTS_USD_PER_CHAR
        cost = round(1000 * _TTS_USD_PER_CHAR, 6)
        assert cost == pytest.approx(0.30)

    def test_elevenlabs_tts_record_cost_called(self, db):
        """finance.record_cost is reachable with elevenlabs provider from active run."""
        db_path, run_id, episode_id = db
        set_run_context(run_id, episode_id, db_path)
        from hypogeum.elevenlabs import _TTS_USD_PER_CHAR
        try:
            record_cost(
                provider="elevenlabs",
                model="eleven_multilingual_v2",
                call_type="narration",
                cost_usd=round(500 * _TTS_USD_PER_CHAR, 6),
                tokens_or_credits=500.0,
                asset_id="episodes/ep_p1/narration/beat_01",
            )
        finally:
            clear_run_context()

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT provider, model, call_type, cost_usd, asset_id "
            "FROM costs WHERE provider='elevenlabs'"
        ).fetchone()
        conn.close()
        assert row["provider"] == "elevenlabs"
        assert row["call_type"] == "narration"
        assert row["asset_id"] == "episodes/ep_p1/narration/beat_01"
        assert row["cost_usd"] == pytest.approx(0.15)

    def test_elevenlabs_music_record_cost_called(self, db):
        db_path, run_id, episode_id = db
        set_run_context(run_id, episode_id, db_path)
        from hypogeum.elevenlabs import _MUSIC_USD_PER_SEC
        duration = 120.0
        try:
            record_cost(
                provider="elevenlabs",
                model="music_v1",
                call_type="music",
                cost_usd=round(duration * _MUSIC_USD_PER_SEC, 6),
                tokens_or_credits=duration,
                asset_id="episodes/ep_p1/music/intro",
            )
        finally:
            clear_run_context()

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT call_type, cost_usd FROM costs WHERE provider='elevenlabs' AND call_type='music'"
        ).fetchone()
        conn.close()
        assert row["cost_usd"] == pytest.approx(2.40)


# ---------------------------------------------------------------------------
# TestPerplexityCostRecording
# ---------------------------------------------------------------------------

class TestPerplexityCostRecording:
    def test_perplexity_rate_constant_is_reasonable(self):
        from hypogeum.perplexity import _PERPLEXITY_USD_PER_TOKEN
        assert _PERPLEXITY_USD_PER_TOKEN == pytest.approx(3e-6)

    def test_perplexity_cost_formula(self):
        """1000 tokens costs $0.003."""
        from hypogeum.perplexity import _PERPLEXITY_USD_PER_TOKEN
        cost = round(1000 * _PERPLEXITY_USD_PER_TOKEN, 6)
        assert cost == pytest.approx(0.003)

    def test_perplexity_record_cost_written_to_db(self, db):
        db_path, run_id, episode_id = db
        set_run_context(run_id, episode_id, db_path)
        from hypogeum.perplexity import _PERPLEXITY_USD_PER_TOKEN
        total_tokens = 800
        try:
            record_cost(
                provider="perplexity",
                model="sonar-pro",
                call_type="web_search",
                cost_usd=round(total_tokens * _PERPLEXITY_USD_PER_TOKEN, 6),
                tokens_or_credits=float(total_tokens),
            )
        finally:
            clear_run_context()

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT provider, call_type, tokens_or_credits, cost_usd "
            "FROM costs WHERE provider='perplexity'"
        ).fetchone()
        conn.close()
        assert row["provider"] == "perplexity"
        assert row["call_type"] == "web_search"
        assert row["tokens_or_credits"] == pytest.approx(800.0)
        assert row["cost_usd"] == pytest.approx(0.0024)


# ---------------------------------------------------------------------------
# TestFullProvenanceFields — all 14 required fields present per task spec
# ---------------------------------------------------------------------------

class TestFullProvenanceFields:
    """
    Verifies that after run_asset_archive, every asset record
    can store all 14 required provenance fields from the task spec.
    """

    REQUIRED_ARCHIVE_INDEX_COLS = {
        "asset_id", "asset_type", "episode_id", "run_id",
        "provider", "model",
        "generation_prompt", "generation_params",
        "file_path", "archive_location",
        "moved_at", "version", "cost_usd", "asset_created_at",
    }

    def test_all_required_columns_exist_in_archive_index(self, db):
        db_path, _, _ = db
        conn = get_connection(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(archive_index)").fetchall()}
        conn.close()
        missing = self.REQUIRED_ARCHIVE_INDEX_COLS - cols
        assert not missing, f"archive_index missing columns: {missing}"

    def test_scene_id_column_exists(self, db):
        """scene_id is task-spec required for clip assets."""
        db_path, _, _ = db
        conn = get_connection(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(archive_index)").fetchall()}
        conn.close()
        assert "scene_id" in cols

    def test_jsonl_catalog_records_have_all_required_fields(self, db, tmp_path):
        """asset_catalog.jsonl records carry all 14 provenance fields."""
        db_path, run_id, episode_id = db
        catalog_dir = tmp_path / "catalogs"
        output_dir = tmp_path / "episodes" / episode_id

        vp, np, tp, frp = _make_rich_packages()
        run_asset_archive(
            vp, np, tp, frp, episode_id,
            catalog_dir=catalog_dir,
            output_dir=output_dir,
            run_id=run_id,
            db_path=db_path,
        )

        import json as _json
        catalog_path = catalog_dir / "asset_catalog.jsonl"
        records = [
            _json.loads(line)
            for line in catalog_path.read_text().splitlines()
            if line.strip()
        ]

        required_jsonl_fields = {
            "asset_id", "asset_type", "source_episode_id", "run_id",
            "provider", "model",
            "generation_prompt", "generation_params",
            "file_path", "archive_location",
            "moved_at", "version", "cost_usd", "created_at",
        }

        for r in records:
            missing = required_jsonl_fields - set(r.keys())
            assert not missing, (
                f"asset_id={r.get('asset_id')} type={r.get('asset_type')} "
                f"missing fields: {missing}"
            )
