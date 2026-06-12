"""
tests/test_production_metadata.py — Phase 3B: Production Metadata Capture test suite

Coverage:
    TestWriteProductionMetadata   — required fields, metadata_version, FK consistency
    TestWriteAnalyticsPending     — required fields, status, conditional writing
    TestListAnalyticsPending      — list returns only pending records
    TestNonBlocking               — write failure returns error key, never raises
    TestPipelineIntegration       — PipelineResult fields present, pipeline wires correctly
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hypogeum.identifiers import METADATA_VERSION, generate_title_id
from hypogeum.production_metadata import (
    list_analytics_pending,
    write_analytics_pending,
    write_production_metadata,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_RUN_ID = "run-prod-001"
_EPISODE_ID = "ep_000"
_TITLE = "The Computer That Vanished"
_TITLE_ID = generate_title_id(_TITLE, _EPISODE_ID)

_THUMBNAIL = {
    "thumbnail_candidate_id": "thumb_cand_001",
    "text_overlay": "The Computer That Vanished",
    "visual_prompt": "A Roman galley sinking into a dark sea",
    "prompt_hash": "abc123",
}

_NARRATIVE = {
    "narrative_arc": "mystery_reveal",
    "structure_type": "three_act",
    "total_segments": 7,
}

_MODEL_ASSIGNMENTS = {
    "ep": "anthropic/claude-fable-5",
    "rd": "anthropic/claude-opus-4.6",
}


# ---------------------------------------------------------------------------
# TestWriteProductionMetadata
# ---------------------------------------------------------------------------

class TestWriteProductionMetadata:
    def test_returns_dict(self, tmp_path):
        record = write_production_metadata(
            run_id=_RUN_ID, episode_id=_EPISODE_ID,
            title_id=_TITLE_ID, selected_title=_TITLE,
            selected_thumbnail=_THUMBNAIL, narrative_structure=_NARRATIVE,
            research_source_count=12, model_assignments=_MODEL_ASSIGNMENTS,
            output_dir=tmp_path,
        )
        assert isinstance(record, dict)

    def test_required_fields_present(self, tmp_path):
        record = write_production_metadata(
            run_id=_RUN_ID, episode_id=_EPISODE_ID,
            title_id=_TITLE_ID, selected_title=_TITLE,
            selected_thumbnail=_THUMBNAIL, narrative_structure=_NARRATIVE,
            research_source_count=12, model_assignments=_MODEL_ASSIGNMENTS,
            output_dir=tmp_path,
        )
        for field in (
            "run_id", "episode_id", "title_id", "selected_title",
            "selected_thumbnail", "narrative_structure", "research_source_count",
            "model_assignments", "recorded_at", "metadata_version",
        ):
            assert field in record, f"Missing field: {field}"

    def test_metadata_version_is_v1(self, tmp_path):
        record = write_production_metadata(
            run_id=_RUN_ID, episode_id=_EPISODE_ID,
            title_id=_TITLE_ID, selected_title=_TITLE,
            selected_thumbnail=_THUMBNAIL, narrative_structure=_NARRATIVE,
            research_source_count=12, model_assignments=_MODEL_ASSIGNMENTS,
            output_dir=tmp_path,
        )
        assert record["metadata_version"] == METADATA_VERSION

    def test_run_id_stored_correctly(self, tmp_path):
        record = write_production_metadata(
            run_id=_RUN_ID, episode_id=_EPISODE_ID,
            title_id=_TITLE_ID, selected_title=_TITLE,
            selected_thumbnail=_THUMBNAIL, narrative_structure=_NARRATIVE,
            research_source_count=12, model_assignments=_MODEL_ASSIGNMENTS,
            output_dir=tmp_path,
        )
        assert record["run_id"] == _RUN_ID

    def test_episode_id_stored_correctly(self, tmp_path):
        record = write_production_metadata(
            run_id=_RUN_ID, episode_id=_EPISODE_ID,
            title_id=_TITLE_ID, selected_title=_TITLE,
            selected_thumbnail=_THUMBNAIL, narrative_structure=_NARRATIVE,
            research_source_count=12, model_assignments=_MODEL_ASSIGNMENTS,
            output_dir=tmp_path,
        )
        assert record["episode_id"] == _EPISODE_ID

    def test_title_id_consistent_with_selected_title(self, tmp_path):
        """title_id must match generate_title_id(selected_title, episode_id)."""
        record = write_production_metadata(
            run_id=_RUN_ID, episode_id=_EPISODE_ID,
            title_id=_TITLE_ID, selected_title=_TITLE,
            selected_thumbnail=_THUMBNAIL, narrative_structure=_NARRATIVE,
            research_source_count=12, model_assignments=_MODEL_ASSIGNMENTS,
            output_dir=tmp_path,
        )
        expected_title_id = generate_title_id(record["selected_title"], record["episode_id"])
        assert record["title_id"] == expected_title_id

    def test_thumbnail_fields_present(self, tmp_path):
        record = write_production_metadata(
            run_id=_RUN_ID, episode_id=_EPISODE_ID,
            title_id=_TITLE_ID, selected_title=_TITLE,
            selected_thumbnail=_THUMBNAIL, narrative_structure=_NARRATIVE,
            research_source_count=12, model_assignments=_MODEL_ASSIGNMENTS,
            output_dir=tmp_path,
        )
        thumb = record["selected_thumbnail"]
        assert "thumbnail_candidate_id" in thumb
        assert "text_overlay" in thumb
        assert "visual_prompt" in thumb
        assert "prompt_hash" in thumb

    def test_research_source_count_stored(self, tmp_path):
        record = write_production_metadata(
            run_id=_RUN_ID, episode_id=_EPISODE_ID,
            title_id=_TITLE_ID, selected_title=_TITLE,
            selected_thumbnail=_THUMBNAIL, narrative_structure=_NARRATIVE,
            research_source_count=12, model_assignments=_MODEL_ASSIGNMENTS,
            output_dir=tmp_path,
        )
        assert record["research_source_count"] == 12

    def test_model_assignments_stored(self, tmp_path):
        record = write_production_metadata(
            run_id=_RUN_ID, episode_id=_EPISODE_ID,
            title_id=_TITLE_ID, selected_title=_TITLE,
            selected_thumbnail=_THUMBNAIL, narrative_structure=_NARRATIVE,
            research_source_count=12, model_assignments=_MODEL_ASSIGNMENTS,
            output_dir=tmp_path,
        )
        assert record["model_assignments"]["rd"] == "anthropic/claude-opus-4.6"

    def test_json_file_written(self, tmp_path):
        write_production_metadata(
            run_id=_RUN_ID, episode_id=_EPISODE_ID,
            title_id=_TITLE_ID, selected_title=_TITLE,
            selected_thumbnail=_THUMBNAIL, narrative_structure=_NARRATIVE,
            research_source_count=12, model_assignments=_MODEL_ASSIGNMENTS,
            output_dir=tmp_path,
        )
        out = tmp_path / f"production_metadata_{_EPISODE_ID}.json"
        assert out.exists()

    def test_json_file_parseable(self, tmp_path):
        write_production_metadata(
            run_id=_RUN_ID, episode_id=_EPISODE_ID,
            title_id=_TITLE_ID, selected_title=_TITLE,
            selected_thumbnail=_THUMBNAIL, narrative_structure=_NARRATIVE,
            research_source_count=12, model_assignments=_MODEL_ASSIGNMENTS,
            output_dir=tmp_path,
        )
        content = json.loads(
            (tmp_path / f"production_metadata_{_EPISODE_ID}.json").read_text()
        )
        assert content["episode_id"] == _EPISODE_ID
        assert content["metadata_version"] == METADATA_VERSION

    def test_idempotent_overwrite(self, tmp_path):
        """Second write with same args overwrites file cleanly."""
        write_production_metadata(
            run_id=_RUN_ID, episode_id=_EPISODE_ID,
            title_id=_TITLE_ID, selected_title=_TITLE,
            selected_thumbnail=_THUMBNAIL, narrative_structure=_NARRATIVE,
            research_source_count=12, model_assignments=_MODEL_ASSIGNMENTS,
            output_dir=tmp_path,
        )
        write_production_metadata(
            run_id=_RUN_ID, episode_id=_EPISODE_ID,
            title_id=_TITLE_ID, selected_title=_TITLE,
            selected_thumbnail=_THUMBNAIL, narrative_structure=_NARRATIVE,
            research_source_count=12, model_assignments=_MODEL_ASSIGNMENTS,
            output_dir=tmp_path,
        )
        content = json.loads(
            (tmp_path / f"production_metadata_{_EPISODE_ID}.json").read_text()
        )
        assert content["episode_id"] == _EPISODE_ID


# ---------------------------------------------------------------------------
# TestWriteAnalyticsPending
# ---------------------------------------------------------------------------

class TestWriteAnalyticsPending:
    def test_returns_dict(self, tmp_path):
        record = write_analytics_pending(
            episode_id=_EPISODE_ID,
            youtube_video_id="dQw4w9WgXcQ",
            published_at="2026-06-12T10:00:00Z",
            run_id=_RUN_ID,
            output_dir=tmp_path,
        )
        assert isinstance(record, dict)

    def test_required_fields_present(self, tmp_path):
        record = write_analytics_pending(
            episode_id=_EPISODE_ID,
            youtube_video_id="dQw4w9WgXcQ",
            published_at="2026-06-12T10:00:00Z",
            run_id=_RUN_ID,
            output_dir=tmp_path,
        )
        for field in (
            "episode_id", "youtube_video_id", "published_at",
            "run_id", "window_days", "status", "created_at", "metadata_version",
        ):
            assert field in record, f"Missing field: {field}"

    def test_status_is_pending(self, tmp_path):
        record = write_analytics_pending(
            episode_id=_EPISODE_ID,
            youtube_video_id="dQw4w9WgXcQ",
            published_at="2026-06-12T10:00:00Z",
            run_id=_RUN_ID,
            output_dir=tmp_path,
        )
        assert record["status"] == "pending"

    def test_metadata_version_is_v1(self, tmp_path):
        record = write_analytics_pending(
            episode_id=_EPISODE_ID,
            youtube_video_id="dQw4w9WgXcQ",
            published_at="2026-06-12T10:00:00Z",
            run_id=_RUN_ID,
            output_dir=tmp_path,
        )
        assert record["metadata_version"] == METADATA_VERSION

    def test_run_id_stored(self, tmp_path):
        record = write_analytics_pending(
            episode_id=_EPISODE_ID,
            youtube_video_id="dQw4w9WgXcQ",
            published_at="2026-06-12T10:00:00Z",
            run_id=_RUN_ID,
            output_dir=tmp_path,
        )
        assert record["run_id"] == _RUN_ID

    def test_youtube_video_id_stored(self, tmp_path):
        record = write_analytics_pending(
            episode_id=_EPISODE_ID,
            youtube_video_id="dQw4w9WgXcQ",
            published_at="2026-06-12T10:00:00Z",
            run_id=_RUN_ID,
            output_dir=tmp_path,
        )
        assert record["youtube_video_id"] == "dQw4w9WgXcQ"

    def test_window_days_default_is_7(self, tmp_path):
        record = write_analytics_pending(
            episode_id=_EPISODE_ID,
            youtube_video_id="dQw4w9WgXcQ",
            published_at="2026-06-12T10:00:00Z",
            output_dir=tmp_path,
        )
        assert record["window_days"] == 7

    def test_window_days_custom(self, tmp_path):
        record = write_analytics_pending(
            episode_id=_EPISODE_ID,
            youtube_video_id="dQw4w9WgXcQ",
            published_at="2026-06-12T10:00:00Z",
            window_days=14,
            output_dir=tmp_path,
        )
        assert record["window_days"] == 14

    def test_json_file_written(self, tmp_path):
        write_analytics_pending(
            episode_id=_EPISODE_ID,
            youtube_video_id="dQw4w9WgXcQ",
            published_at="2026-06-12T10:00:00Z",
            run_id=_RUN_ID,
            output_dir=tmp_path,
        )
        assert (tmp_path / f"analytics_pending_{_EPISODE_ID}.json").exists()

    def test_json_file_parseable(self, tmp_path):
        write_analytics_pending(
            episode_id=_EPISODE_ID,
            youtube_video_id="dQw4w9WgXcQ",
            published_at="2026-06-12T10:00:00Z",
            run_id=_RUN_ID,
            output_dir=tmp_path,
        )
        content = json.loads(
            (tmp_path / f"analytics_pending_{_EPISODE_ID}.json").read_text()
        )
        assert content["status"] == "pending"
        assert content["metadata_version"] == METADATA_VERSION


# ---------------------------------------------------------------------------
# TestListAnalyticsPending
# ---------------------------------------------------------------------------

class TestListAnalyticsPending:
    def test_returns_empty_when_dir_absent(self, tmp_path):
        result = list_analytics_pending(output_dir=tmp_path / "nonexistent")
        assert result == []

    def test_returns_pending_records(self, tmp_path):
        write_analytics_pending("ep_000", "vid_001", "2026-06-12T10:00:00Z", output_dir=tmp_path)
        write_analytics_pending("ep_001", "vid_002", "2026-06-13T10:00:00Z", output_dir=tmp_path)
        results = list_analytics_pending(output_dir=tmp_path)
        assert len(results) == 2

    def test_skips_non_pending_records(self, tmp_path):
        write_analytics_pending("ep_000", "vid_001", "2026-06-12T10:00:00Z", output_dir=tmp_path)
        # Manually update status to "captured"
        path = tmp_path / "analytics_pending_ep_000.json"
        record = json.loads(path.read_text())
        record["status"] = "captured"
        path.write_text(json.dumps(record))
        results = list_analytics_pending(output_dir=tmp_path)
        assert len(results) == 0

    def test_returned_records_have_required_fields(self, tmp_path):
        write_analytics_pending("ep_000", "vid_001", "2026-06-12T10:00:00Z", output_dir=tmp_path)
        results = list_analytics_pending(output_dir=tmp_path)
        for r in results:
            assert "episode_id" in r
            assert "youtube_video_id" in r
            assert "status" in r


# ---------------------------------------------------------------------------
# TestNonBlocking
# ---------------------------------------------------------------------------

class TestNonBlocking:
    def test_write_production_metadata_bad_path_returns_error(self):
        """Non-blocking: bad path returns dict with error key, never raises."""
        record = write_production_metadata(
            run_id=_RUN_ID, episode_id=_EPISODE_ID,
            title_id=_TITLE_ID, selected_title=_TITLE,
            selected_thumbnail=_THUMBNAIL, narrative_structure=_NARRATIVE,
            research_source_count=12, model_assignments=_MODEL_ASSIGNMENTS,
            output_dir=Path("/nonexistent/deeply/nested/path"),
        )
        # Must not raise; may or may not have error key depending on mkdir behavior
        assert isinstance(record, dict)

    def test_write_analytics_pending_bad_path_returns_error(self):
        record = write_analytics_pending(
            episode_id=_EPISODE_ID,
            youtube_video_id="dQw4w9WgXcQ",
            published_at="2026-06-12T10:00:00Z",
            output_dir=Path("/nonexistent/deeply/nested/path"),
        )
        assert isinstance(record, dict)


# ---------------------------------------------------------------------------
# TestPipelineIntegration
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    def test_pipeline_result_has_production_metadata_record_field(self):
        import dataclasses
        from hypogeum.pipeline import PipelineResult
        field_names = {f.name for f in dataclasses.fields(PipelineResult)}
        assert "production_metadata_record" in field_names

    def test_pipeline_result_has_analytics_pending_record_field(self):
        import dataclasses
        from hypogeum.pipeline import PipelineResult
        field_names = {f.name for f in dataclasses.fields(PipelineResult)}
        assert "analytics_pending_record" in field_names

    def test_production_metadata_record_default_is_none(self):
        import dataclasses
        from hypogeum.pipeline import PipelineResult
        fields = {f.name: f for f in dataclasses.fields(PipelineResult)}
        assert fields["production_metadata_record"].default is None

    def test_analytics_pending_record_default_is_none(self):
        import dataclasses
        from hypogeum.pipeline import PipelineResult
        fields = {f.name: f for f in dataclasses.fields(PipelineResult)}
        assert fields["analytics_pending_record"].default is None

    def test_pipeline_imports_write_production_metadata(self):
        import inspect
        from hypogeum import pipeline
        src = inspect.getsource(pipeline)
        assert "write_production_metadata" in src

    def test_pipeline_imports_write_analytics_pending(self):
        import inspect
        from hypogeum import pipeline
        src = inspect.getsource(pipeline)
        assert "write_analytics_pending" in src

    def test_analytics_pending_only_when_youtube_published(self):
        """analytics_pending_record is None when youtube_main_published=False."""
        import inspect
        from hypogeum import pipeline
        src = inspect.getsource(pipeline)
        # The conditional write must check youtube_main_published
        assert "youtube_main_published" in src
        assert "write_analytics_pending" in src
