"""
tests/test_final_render_reviewer.py — Unit tests for Final Render Reviewer (Agent 37)

Tests the sole authority for production_ready determination. No real files,
ffmpeg, or subprocess calls — the reviewer operates on dicts only.

Coverage:
  TestProductionReadyRules      — when production_ready is True vs False
  TestOverallVerdict            — APPROVED / CONDITIONAL / REJECTED mapping
  TestMediaSpecsIntegration     — publish_ready failures propagate correctly
  TestAudioQAIntegration        — audio_qa_verdict fail/warn/pass propagation
  TestCaptionStatus             — caption_status flag check
  TestMusicCoverage             — sections_without_music → CONDITIONAL
  TestDurationCoverage          — duration_coverage_gap threshold (5s)
  TestHumanReviewReasons        — unique reasons, no duplicates
  TestSchemaContract            — output always validates; verdict_id, manifest_id present
  TestRenderOutputManifestSchema — render_output_manifest schema validation
  TestAssemblyAgentEmitsManifest — assembly_agent writes manifest to disk
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.final_render_reviewer import run_final_render_reviewer, _add_check
from hypogeum.agents.video_renderer import _build_render_output_manifest
from hypogeum.schemas import validate, SchemaError, SCHEMA_NAMES


# ---------------------------------------------------------------------------
# Helpers — canonical manifests
# ---------------------------------------------------------------------------

def _make_manifest(
    render_status: str = "success",
    main_exists: bool = True,
    main_size: int = 18_000_000,
    shorts: list | None = None,
    clip_rate: float = 1.0,
    missing_clips: list | None = None,
    sections_without_music: list | None = None,
    ffmpeg_errors: list | None = None,
    coverage_gap: float = 0.0,
    episode_id: str = "ep_test",
) -> dict:
    if shorts is None:
        shorts = [
            {"short_id": "sh_001", "file_path": "/ep/short_0.mp4",
             "file_exists": True, "file_size_bytes": 5_000_000},
            {"short_id": "sh_002", "file_path": "/ep/short_1.mp4",
             "file_exists": True, "file_size_bytes": 5_000_000},
            {"short_id": "sh_003", "file_path": "/ep/short_2.mp4",
             "file_exists": True, "file_size_bytes": 5_000_000},
        ]
    return {
        "manifest_id": f"rom_{episode_id}",
        "episode_id": episode_id,
        "generated_at": "2026-06-07T00:00:00+00:00",
        "render_status": render_status,
        "main_render": {
            "file_path": f"/ep/main_{episode_id}.mp4",
            "file_exists": main_exists,
            "file_size_bytes": main_size if main_exists else 0,
        },
        "shorts_renders": shorts,
        "clip_availability_rate": clip_rate,
        "missing_clips": missing_clips or [],
        "sections_without_music": sections_without_music or [],
        "ffmpeg_errors": ffmpeg_errors or [],
        "duration_coverage_gap_seconds": coverage_gap,
        "total_narration_seconds": 115.0,
        "covered_narration_seconds": 115.0 - coverage_gap,
    }


def _make_media_specs_report(
    file_path: str = "/ep/main_ep_test.mp4",
    file_type: str = "video",
    file_valid: bool = True,
    publish_ready: bool = True,
    failures: list | None = None,
) -> dict:
    return {
        "report_id": "r_001",
        "episode_id": "ep_test",
        "file_path": file_path,
        "file_type": file_type,
        "file_valid": file_valid,
        "publish_ready": publish_ready,
        "specs_observed": {"width": 1920, "height": 1080} if publish_ready else {"width": 1280, "height": 720},
        "specs_expected": {"width": 1920, "height": 1080},
        "failures": failures or ([] if publish_ready else ["resolution: expected 1920×1080, got 1280×720"]),
        "warnings": [],
        "checked_at": "2026-06-07T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# TestProductionReadyRules
# ---------------------------------------------------------------------------

class TestProductionReadyRules:

    def test_all_ok_is_production_ready(self):
        """Perfect manifest → production_ready=True."""
        manifest = _make_manifest()
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert verdict["production_ready"] is True

    def test_render_status_failed_blocks_production_ready(self):
        """render_status=failed → production_ready=False."""
        manifest = _make_manifest(render_status="failed", main_exists=False)
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert verdict["production_ready"] is False

    def test_main_file_missing_blocks_production_ready(self):
        """main_render.file_exists=False → production_ready=False."""
        manifest = _make_manifest(main_exists=False)
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert verdict["production_ready"] is False

    def test_main_file_zero_bytes_blocks_production_ready(self):
        """main_render.file_size_bytes=0 → production_ready=False."""
        manifest = _make_manifest(main_exists=True, main_size=0)
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert verdict["production_ready"] is False

    def test_missing_short_blocks_production_ready(self):
        """A short with file_exists=False → production_ready=False."""
        shorts = [
            {"short_id": "sh_001", "file_path": "/ep/s0.mp4",
             "file_exists": True, "file_size_bytes": 5_000_000},
            {"short_id": "sh_002", "file_path": "/ep/s1.mp4",
             "file_exists": False, "file_size_bytes": 0},
        ]
        manifest = _make_manifest(shorts=shorts)
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert verdict["production_ready"] is False
        assert any("sh_002" in f for f in verdict["failures"])

    def test_partial_render_does_not_block_if_files_exist(self):
        """render_status=partial with all files present → production_ready=True (CONDITIONAL)."""
        manifest = _make_manifest(
            render_status="partial",
            clip_rate=0.9,
            missing_clips=["sc_05"],
        )
        verdict = run_final_render_reviewer(manifest, "ep_test")
        # partial render alone doesn't block if files exist
        assert verdict["production_ready"] is True
        assert verdict["overall_verdict"] == "CONDITIONAL"

    def test_clip_availability_below_60_blocks_production_ready(self):
        """clip_availability_rate < 0.60 → production_ready=False."""
        manifest = _make_manifest(
            render_status="partial",
            clip_rate=0.55,
            missing_clips=["sc_01", "sc_02", "sc_03"],
        )
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert verdict["production_ready"] is False

    def test_clip_availability_between_60_and_100_is_warning_not_failure(self):
        """clip_availability_rate 0.60–0.99 → production_ready=True (CONDITIONAL, not blocked)."""
        manifest = _make_manifest(
            render_status="partial",
            clip_rate=0.75,
            missing_clips=["sc_01", "sc_02", "sc_03"],
        )
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert verdict["production_ready"] is True
        assert verdict["overall_verdict"] == "CONDITIONAL"
        assert any("clip_availability" in w for w in verdict["warnings"])

    def test_duration_gap_over_5s_blocks_production_ready(self):
        """duration_coverage_gap_seconds > 5.0 → production_ready=False."""
        manifest = _make_manifest(coverage_gap=6.0)
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert verdict["production_ready"] is False
        assert any("duration_coverage" in f for f in verdict["failures"])

    def test_duration_gap_under_5s_does_not_block(self):
        """duration_coverage_gap_seconds ≤ 5.0 → production_ready not blocked by gap."""
        manifest = _make_manifest(coverage_gap=3.0)
        verdict = run_final_render_reviewer(manifest, "ep_test")
        # Gap alone (≤5s) is a warning, not a failure
        assert any("duration_coverage" in w for w in verdict["warnings"])
        # production_ready still True (other checks pass)
        assert verdict["production_ready"] is True

    def test_no_shorts_configured_passes(self):
        """Empty shorts_renders list → production_ready not blocked."""
        manifest = _make_manifest(shorts=[])
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert verdict["production_ready"] is True


# ---------------------------------------------------------------------------
# TestOverallVerdict
# ---------------------------------------------------------------------------

class TestOverallVerdict:

    def test_approved_when_no_failures_no_warnings_except_media_specs(self):
        """APPROVED requires production_ready=True and no warnings besides media_specs."""
        # Provide media_specs_reports so we don't get 'spec not verified' warning
        manifest = _make_manifest()
        reports = [_make_media_specs_report(publish_ready=True)]
        verdict = run_final_render_reviewer(manifest, "ep_test",
                                            media_specs_reports=reports,
                                            audio_qa_verdict={"overall_status": "pass"})
        # Note: audio QA 'pass' removes that warning too
        assert verdict["overall_verdict"] == "APPROVED"
        assert verdict["production_ready"] is True

    def test_conditional_when_production_ready_but_has_warnings(self):
        """CONDITIONAL = production_ready=True but at least one warning."""
        manifest = _make_manifest(sections_without_music=["main_episode"])
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert verdict["overall_verdict"] == "CONDITIONAL"
        assert verdict["production_ready"] is True

    def test_rejected_when_production_not_ready(self):
        """REJECTED = production_ready=False."""
        manifest = _make_manifest(main_exists=False)
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert verdict["overall_verdict"] == "REJECTED"
        assert verdict["production_ready"] is False

    def test_rejected_has_failures_list(self):
        """REJECTED verdict always has at least one item in failures."""
        manifest = _make_manifest(render_status="failed", main_exists=False)
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert len(verdict["failures"]) > 0


# ---------------------------------------------------------------------------
# TestMediaSpecsIntegration
# ---------------------------------------------------------------------------

class TestMediaSpecsIntegration:

    def test_publish_ready_false_blocks_production_ready(self):
        """A media_specs_report with publish_ready=False → production_ready=False."""
        manifest = _make_manifest()
        report = _make_media_specs_report(publish_ready=False)
        verdict = run_final_render_reviewer(manifest, "ep_test",
                                            media_specs_reports=[report])
        assert verdict["production_ready"] is False
        assert any("media_spec" in f for f in verdict["failures"])

    def test_1280x720_spec_failure_propagates(self):
        """Resolution failure from Agent 36 shows in verdict failures."""
        manifest = _make_manifest()
        report = _make_media_specs_report(
            publish_ready=False,
            failures=["resolution: expected 1920×1080, got 1280×720"],
        )
        verdict = run_final_render_reviewer(manifest, "ep_test",
                                            media_specs_reports=[report])
        assert verdict["production_ready"] is False
        assert any("1920" in f or "1280" in f for f in verdict["failures"])

    def test_all_specs_pass_is_pass_check(self):
        """All publish_ready=True → c_media_specs check is pass."""
        manifest = _make_manifest()
        reports = [
            _make_media_specs_report(file_path="/ep/main.mp4", publish_ready=True),
            _make_media_specs_report(file_path="/ep/short_0.mp4",
                                     file_type="short", publish_ready=True),
        ]
        verdict = run_final_render_reviewer(manifest, "ep_test",
                                            media_specs_reports=reports)
        c_media = next(c for c in verdict["checks"] if c["check_id"] == "c_media_specs")
        assert c_media["status"] == "pass"

    def test_invalid_file_is_warning_not_failure(self):
        """file_valid=False (ffprobe crashed) → warning, not blocking failure."""
        manifest = _make_manifest()
        report = _make_media_specs_report(file_valid=False, publish_ready=False,
                                          failures=["ffprobe failed: exit 1"])
        verdict = run_final_render_reviewer(manifest, "ep_test",
                                            media_specs_reports=[report])
        # invalid file → warning (can't inspect), not blocking
        c_media = next(c for c in verdict["checks"] if c["check_id"] == "c_media_specs")
        assert c_media["status"] == "warn"
        # production_ready not blocked by this alone
        assert verdict["production_ready"] is True

    def test_no_media_specs_reports_is_warning(self):
        """media_specs_reports=None → c_media_specs is warn, not fail."""
        manifest = _make_manifest()
        verdict = run_final_render_reviewer(manifest, "ep_test",
                                            media_specs_reports=None)
        c_media = next(c for c in verdict["checks"] if c["check_id"] == "c_media_specs")
        assert c_media["status"] == "warn"
        assert verdict["production_ready"] is True  # not blocked


# ---------------------------------------------------------------------------
# TestAudioQAIntegration
# ---------------------------------------------------------------------------

class TestAudioQAIntegration:

    def test_audio_qa_fail_blocks_production_ready(self):
        """audio_qa_verdict with overall_status=fail → production_ready=False."""
        manifest = _make_manifest()
        verdict = run_final_render_reviewer(
            manifest, "ep_test",
            audio_qa_verdict={"overall_status": "fail"},
        )
        assert verdict["production_ready"] is False
        assert any("audio_qa" in f for f in verdict["failures"])

    def test_audio_qa_warn_is_not_blocking(self):
        """audio_qa_verdict with overall_status=warn → warning only, not blocking."""
        manifest = _make_manifest()
        verdict = run_final_render_reviewer(
            manifest, "ep_test",
            audio_qa_verdict={"overall_status": "warn"},
        )
        assert verdict["production_ready"] is True
        assert any("audio_qa" in w for w in verdict["warnings"])

    def test_audio_qa_pass_produces_pass_check(self):
        """audio_qa_verdict with overall_status=pass → c_audio_qa check is pass."""
        manifest = _make_manifest()
        verdict = run_final_render_reviewer(
            manifest, "ep_test",
            audio_qa_verdict={"overall_status": "pass"},
        )
        c_audio = next(c for c in verdict["checks"] if c["check_id"] == "c_audio_qa")
        assert c_audio["status"] == "pass"

    def test_no_audio_qa_verdict_is_warning(self):
        """audio_qa_verdict=None → c_audio_qa is warn, not blocking."""
        manifest = _make_manifest()
        verdict = run_final_render_reviewer(manifest, "ep_test",
                                            audio_qa_verdict=None)
        c_audio = next(c for c in verdict["checks"] if c["check_id"] == "c_audio_qa")
        assert c_audio["status"] == "warn"
        assert verdict["production_ready"] is True


# ---------------------------------------------------------------------------
# TestCaptionStatus
# ---------------------------------------------------------------------------

class TestCaptionStatus:

    def test_captions_present_passes(self):
        manifest = _make_manifest()
        verdict = run_final_render_reviewer(
            manifest, "ep_test",
            caption_status={"main_captions_present": True},
        )
        c_cap = next(c for c in verdict["checks"] if c["check_id"] == "c_captions")
        assert c_cap["status"] == "pass"

    def test_captions_absent_is_warning(self):
        """main_captions_present=False → warning, not blocking."""
        manifest = _make_manifest()
        verdict = run_final_render_reviewer(
            manifest, "ep_test",
            caption_status={"main_captions_present": False},
        )
        c_cap = next(c for c in verdict["checks"] if c["check_id"] == "c_captions")
        assert c_cap["status"] == "warn"
        assert verdict["production_ready"] is True  # not blocking

    def test_no_caption_status_skips_check(self):
        """caption_status=None → no c_captions check in output."""
        manifest = _make_manifest()
        verdict = run_final_render_reviewer(manifest, "ep_test", caption_status=None)
        check_ids = [c["check_id"] for c in verdict["checks"]]
        assert "c_captions" not in check_ids


# ---------------------------------------------------------------------------
# TestMusicCoverage
# ---------------------------------------------------------------------------

class TestMusicCoverage:

    def test_sections_without_music_is_conditional(self):
        """sections_without_music present → CONDITIONAL (not REJECTED)."""
        manifest = _make_manifest(sections_without_music=["main_episode"])
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert verdict["production_ready"] is True
        assert verdict["overall_verdict"] == "CONDITIONAL"
        assert any("music_coverage" in w for w in verdict["warnings"])

    def test_no_sections_without_music_is_pass(self):
        manifest = _make_manifest(sections_without_music=[])
        verdict = run_final_render_reviewer(manifest, "ep_test")
        c_music = next(c for c in verdict["checks"] if c["check_id"] == "c_music_coverage")
        assert c_music["status"] == "pass"


# ---------------------------------------------------------------------------
# TestDurationCoverage
# ---------------------------------------------------------------------------

class TestDurationCoverage:

    def test_gap_exactly_5s_is_warning_not_failure(self):
        """Gap of exactly 5.0s is at the threshold — warn (not > 5.0 so not fail, but > 0 so warn)."""
        manifest = _make_manifest(coverage_gap=5.0)
        verdict = run_final_render_reviewer(manifest, "ep_test")
        # 5.0 is not > 5.0 → doesn't fail; but 5.0 > 0.0 → warn
        c_dur = next(c for c in verdict["checks"] if c["check_id"] == "c_duration_coverage")
        assert c_dur["status"] == "warn"
        assert verdict["production_ready"] is True  # warn doesn't block

    def test_gap_5_001s_fails(self):
        """Gap just over 5.0s triggers failure."""
        manifest = _make_manifest(coverage_gap=5.001)
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert verdict["production_ready"] is False

    def test_zero_gap_passes(self):
        manifest = _make_manifest(coverage_gap=0.0)
        verdict = run_final_render_reviewer(manifest, "ep_test")
        c_dur = next(c for c in verdict["checks"] if c["check_id"] == "c_duration_coverage")
        assert c_dur["status"] == "pass"


# ---------------------------------------------------------------------------
# TestHumanReviewReasons
# ---------------------------------------------------------------------------

class TestHumanReviewReasons:

    def test_no_issues_no_human_review(self):
        manifest = _make_manifest()
        reports = [_make_media_specs_report(publish_ready=True)]
        verdict = run_final_render_reviewer(manifest, "ep_test",
                                            media_specs_reports=reports,
                                            audio_qa_verdict={"overall_status": "pass"})
        assert verdict["human_review_recommended"] is False
        assert verdict["human_review_reasons"] == []

    def test_reasons_are_unique(self):
        """Duplicate human_review reasons are deduplicated."""
        # Both sections_without_music and missing clips trigger review
        manifest = _make_manifest(
            render_status="partial",
            clip_rate=0.9,
            missing_clips=["sc_01"],
            sections_without_music=["main_episode"],
        )
        verdict = run_final_render_reviewer(manifest, "ep_test")
        reasons = verdict["human_review_reasons"]
        assert len(reasons) == len(set(reasons)), "Duplicate reasons found"

    def test_partial_render_triggers_human_review(self):
        manifest = _make_manifest(render_status="partial")
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert verdict["human_review_recommended"] is True
        assert "partial_render" in verdict["human_review_reasons"]

    def test_clips_missing_triggers_human_review(self):
        manifest = _make_manifest(clip_rate=0.9, missing_clips=["sc_01"])
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert "clips_missing" in verdict["human_review_reasons"]


# ---------------------------------------------------------------------------
# TestSchemaContract
# ---------------------------------------------------------------------------

class TestSchemaContract:

    def test_verdict_validates_against_schema_approved(self):
        manifest = _make_manifest()
        reports = [_make_media_specs_report(publish_ready=True)]
        verdict = run_final_render_reviewer(manifest, "ep_test",
                                            media_specs_reports=reports,
                                            audio_qa_verdict={"overall_status": "pass"})
        validate("render_review_verdict", verdict)

    def test_verdict_validates_against_schema_rejected(self):
        manifest = _make_manifest(main_exists=False)
        verdict = run_final_render_reviewer(manifest, "ep_test")
        validate("render_review_verdict", verdict)

    def test_verdict_has_verdict_id(self):
        manifest = _make_manifest()
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert verdict["verdict_id"].startswith("rrv_ep_test_")

    def test_verdict_has_manifest_id(self):
        manifest = _make_manifest()
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert verdict["manifest_id"] == "rom_ep_test"

    def test_verdict_has_reviewed_at(self):
        manifest = _make_manifest()
        verdict = run_final_render_reviewer(manifest, "ep_test")
        assert "reviewed_at" in verdict
        assert verdict["reviewed_at"]

    def test_schemas_registered(self):
        assert "render_review_verdict" in SCHEMA_NAMES
        assert "render_output_manifest" in SCHEMA_NAMES

    def test_checks_list_contains_expected_check_ids(self):
        """All mandatory checks must appear in the output."""
        manifest = _make_manifest()
        verdict = run_final_render_reviewer(manifest, "ep_test")
        check_ids = {c["check_id"] for c in verdict["checks"]}
        for required in (
            "c_render_status", "c_main_render_exists", "c_shorts_render_exists",
            "c_clip_availability", "c_duration_coverage", "c_music_coverage",
            "c_media_specs", "c_audio_qa",
        ):
            assert required in check_ids, f"Missing check: {required}"


# ---------------------------------------------------------------------------
# TestRenderOutputManifestSchema
# ---------------------------------------------------------------------------

class TestRenderOutputManifestSchema:

    def test_valid_manifest_validates(self):
        manifest = _make_manifest()
        validate("render_output_manifest", manifest)

    def test_manifest_missing_render_status_fails(self):
        manifest = _make_manifest()
        del manifest["render_status"]
        with pytest.raises(SchemaError):
            validate("render_output_manifest", manifest)

    def test_manifest_invalid_render_status_fails(self):
        manifest = _make_manifest()
        manifest["render_status"] = "unknown_status"
        with pytest.raises(SchemaError):
            validate("render_output_manifest", manifest)

    def test_manifest_missing_main_render_fails(self):
        manifest = _make_manifest()
        del manifest["main_render"]
        with pytest.raises(SchemaError):
            validate("render_output_manifest", manifest)

    def test_manifest_shorts_render_missing_file_exists_fails(self):
        manifest = _make_manifest()
        del manifest["shorts_renders"][0]["file_exists"]
        with pytest.raises(SchemaError):
            validate("render_output_manifest", manifest)


# ---------------------------------------------------------------------------
# TestAssemblyAgentEmitsManifest
# ---------------------------------------------------------------------------

class TestAssemblyAgentEmitsManifest:
    """Verify assembly_agent writes render_output_manifest to disk."""

    def _run_assembly_with_full_setup(self, tmp_path, episode_id: str):
        """
        Run assembly_agent end-to-end with files and mocks matching the
        pattern used in TestProductionReadyEvaluation in test_assembly_agent.py.
        """
        import subprocess
        from unittest.mock import MagicMock
        from hypogeum.agents.assembly_agent import run_assembly_agent
        from tests.test_assembly_agent import (
            _make_visual_package, _make_narration_package, _make_thumbnail_package,
            _make_config, _setup_clips_on_disk, _setup_narration_on_disk,
            _make_music_track_result, _mock_ffmpeg_success,
        )

        cfg = _make_config()
        visual = _make_visual_package(episode_id=episode_id)
        narration = _make_narration_package(episode_id=episode_id)
        thumbnails = _make_thumbnail_package(episode_id=episode_id)

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, episode_id)
        _setup_narration_on_disk(narration, assets_root)

        with patch("hypogeum.agents.music_agent.generate_music_track",
                   return_value=_make_music_track_result(tmp_path=tmp_path)):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id=episode_id,
                    output_dir=tmp_path / episode_id,
                )

    def test_manifest_written_to_disk(self, tmp_path):
        """After run_assembly_agent, render_output_manifest_<id>.json must exist."""
        ep = "ep_mtest"
        self._run_assembly_with_full_setup(tmp_path, ep)

        manifest_path = tmp_path / ep / f"render_output_manifest_{ep}.json"
        assert manifest_path.exists(), "render_output_manifest file not written by assembly_agent"
        with open(manifest_path) as f:
            manifest = json.load(f)
        assert manifest["episode_id"] == ep
        assert "render_status" in manifest
        assert "main_render" in manifest
        validate("render_output_manifest", manifest)

    def test_manifest_has_correct_episode_id(self, tmp_path):
        """manifest_id and episode_id in manifest match the episode_id arg."""
        ep = "ep_idcheck"
        self._run_assembly_with_full_setup(tmp_path, ep)

        manifest_path = tmp_path / ep / f"render_output_manifest_{ep}.json"
        manifest = json.loads(manifest_path.read_text())
        assert manifest["episode_id"] == ep
        assert ep in manifest["manifest_id"]


# ---------------------------------------------------------------------------
# TestBuildRenderOutputManifestHelper
# ---------------------------------------------------------------------------

class TestBuildRenderOutputManifestHelper:
    """Unit tests for _build_render_output_manifest helper."""

    def test_main_exists_true_when_file_present(self, tmp_path):
        main = tmp_path / "main.mp4"
        main.write_bytes(b"\x00" * 1000)
        manifest = _build_render_output_manifest(
            episode_id="ep_t",
            render_status="success",
            main_output_path=main,
            short_output_paths=[],
            clip_availability_rate=1.0,
            missing_clips=[],
            sections_without_music=[],
            ffmpeg_errors=[],
            total_narration_dur=30.0,
            covered_dur=30.0,
        )
        assert manifest["main_render"]["file_exists"] is True
        assert manifest["main_render"]["file_size_bytes"] == 1000

    def test_main_exists_false_when_file_absent(self, tmp_path):
        main = tmp_path / "missing.mp4"
        manifest = _build_render_output_manifest(
            episode_id="ep_t",
            render_status="failed",
            main_output_path=main,
            short_output_paths=[],
            clip_availability_rate=0.0,
            missing_clips=[],
            sections_without_music=[],
            ffmpeg_errors=[],
            total_narration_dur=30.0,
            covered_dur=0.0,
        )
        assert manifest["main_render"]["file_exists"] is False
        assert manifest["main_render"]["file_size_bytes"] == 0

    def test_coverage_gap_computed_correctly(self, tmp_path):
        main = tmp_path / "main.mp4"
        main.write_bytes(b"\x00" * 100)
        manifest = _build_render_output_manifest(
            episode_id="ep_t",
            render_status="success",
            main_output_path=main,
            short_output_paths=[],
            clip_availability_rate=1.0,
            missing_clips=[],
            sections_without_music=[],
            ffmpeg_errors=[],
            total_narration_dur=115.0,
            covered_dur=110.0,
        )
        assert manifest["duration_coverage_gap_seconds"] == 5.0

    def test_shorts_file_exists_flags(self, tmp_path):
        present = tmp_path / "short_0.mp4"
        present.write_bytes(b"\x00" * 500)
        absent = tmp_path / "short_1.mp4"
        main = tmp_path / "main.mp4"
        main.write_bytes(b"\x00" * 100)

        manifest = _build_render_output_manifest(
            episode_id="ep_t",
            render_status="partial",
            main_output_path=main,
            short_output_paths=[("sh_001", present), ("sh_002", absent)],
            clip_availability_rate=1.0,
            missing_clips=[],
            sections_without_music=[],
            ffmpeg_errors=[],
            total_narration_dur=30.0,
            covered_dur=30.0,
        )
        assert manifest["shorts_renders"][0]["file_exists"] is True
        assert manifest["shorts_renders"][1]["file_exists"] is False

    def test_manifest_schema_validates(self, tmp_path):
        main = tmp_path / "main.mp4"
        main.write_bytes(b"\x00" * 100)
        manifest = _build_render_output_manifest(
            episode_id="ep_t",
            render_status="success",
            main_output_path=main,
            short_output_paths=[],
            clip_availability_rate=1.0,
            missing_clips=[],
            sections_without_music=[],
            ffmpeg_errors=[],
            total_narration_dur=30.0,
            covered_dur=30.0,
        )
        validate("render_output_manifest", manifest)
