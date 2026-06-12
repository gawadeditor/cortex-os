"""
tests/test_qa_gate_v1.py — Phase 4C: QA Gate v1 new checks

Coverage:
    TestRunIdField              — run_id propagated into report and qa_report file
    TestQAReportFile            — qa_report_{episode_id}.json written alongside qa_approval_report
    TestVideoDimensions         — c_video_dimensions check (FAIL on wrong, PASS on correct, WARN on absent)
    TestThumbnailDimensions     — c_thumbnail_dimensions check (FAIL on wrong, PASS on correct, WARN on absent)
    TestDescriptionPresent      — c_description_present check (FAIL if empty/absent)
    TestChapterMarkers          — c_chapter_markers check (WARN if absent)
    TestCaptionsUpgraded        — c_captions_asset is now FAIL (not WARN)
    TestThumbnailPresentUpgrade — c_thumbnail_present is now FAIL (not WARN) when asset_id empty
    TestMetadataVersion         — metadata_version field in report
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hypogeum.agents.qa_agent import run_qa_agent
from hypogeum.identifiers import METADATA_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_frp(**overrides) -> dict:
    """Minimal final_render_package with all good values."""
    pkg = {
        "package_integrity": {"production_ready": True},
        "assembly_qa_report": {"overall_status": "pass"},
        "episode_render": {
            "video_asset_id": "vid_001",
            "caption_asset_id": "cap_001",
        },
        "metadata_source": {
            "episode_title": "The Lost City",
            "ai_disclosure_text": "Made with AI.",
            "description": "A documentary about the lost city.",
            "section_titles": ["Intro", "Exploration", "Conclusion"],
        },
        "timeline_qa_report": {
            "overall_status": "pass",
            "checks": [],
            "warnings": [],
            "failures": [],
        },
    }
    pkg.update(overrides)
    return pkg


def _good_thumb() -> dict:
    return {
        "thumbnail_package_id": "tp_001",
        "episode_id": "ep_000",
        "main_thumbnail": {"asset_id": "thumb_001", "width": 1280, "height": 720},
        "short_thumbnails": [],
        "package_integrity": {"production_ready": True},
        "thumbnail_qa_report": {
            "overall_status": "pass",
            "checks": [],
            "warnings": [],
            "failures": [],
        },
    }


def _good_msr(width: int = 1920, height: int = 1080) -> list:
    return [
        {
            "report_id": "msr_001",
            "episode_id": "ep_000",
            "file_path": "/path/episode.mp4",
            "file_type": "video",
            "file_valid": True,
            "publish_ready": True,
            "specs_observed": {"width": width, "height": height},
            "specs_expected": {"width": 1920, "height": 1080},
            "failures": [],
            "warnings": [],
            "checked_at": "2026-06-12T00:00:00+00:00",
        }
    ]


def _run(**kwargs) -> dict:
    defaults = dict(
        final_render_package=_base_frp(),
        episode_id="ep_000",
        thumbnail_package=_good_thumb(),
        media_specs_reports=_good_msr(),
    )
    defaults.update(kwargs)
    return run_qa_agent(**defaults)


# ---------------------------------------------------------------------------
# TestRunIdField
# ---------------------------------------------------------------------------

class TestRunIdField:
    def test_run_id_in_report(self, tmp_path):
        report = _run(run_id="run-abc-123", output_dir=tmp_path)
        assert report["run_id"] == "run-abc-123"

    def test_run_id_empty_when_not_provided(self, tmp_path):
        report = _run(output_dir=tmp_path)
        assert report["run_id"] == ""

    def test_run_id_in_qa_report_file(self, tmp_path):
        _run(run_id="run-xyz", output_dir=tmp_path)
        content = json.loads((tmp_path / "qa_report_ep_000.json").read_text())
        assert content["run_id"] == "run-xyz"


# ---------------------------------------------------------------------------
# TestQAReportFile
# ---------------------------------------------------------------------------

class TestQAReportFile:
    def test_qa_report_file_written(self, tmp_path):
        _run(output_dir=tmp_path)
        assert (tmp_path / "qa_report_ep_000.json").exists()

    def test_qa_report_file_parseable(self, tmp_path):
        _run(output_dir=tmp_path)
        content = json.loads((tmp_path / "qa_report_ep_000.json").read_text())
        assert content["episode_id"] == "ep_000"

    def test_qa_report_and_approval_report_both_written(self, tmp_path):
        _run(output_dir=tmp_path)
        assert (tmp_path / "qa_approval_report_ep_000.json").exists()
        assert (tmp_path / "qa_report_ep_000.json").exists()

    def test_qa_report_verdict_matches_approval_report(self, tmp_path):
        _run(output_dir=tmp_path)
        approval = json.loads((tmp_path / "qa_approval_report_ep_000.json").read_text())
        qa = json.loads((tmp_path / "qa_report_ep_000.json").read_text())
        assert approval["verdict"] == qa["verdict"]


# ---------------------------------------------------------------------------
# TestVideoDimensions
# ---------------------------------------------------------------------------

class TestVideoDimensions:
    def test_pass_on_correct_dimensions(self, tmp_path):
        report = _run(media_specs_reports=_good_msr(1920, 1080), output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_video_dimensions"] == "pass"

    def test_fail_on_wrong_dimensions(self, tmp_path):
        report = _run(media_specs_reports=_good_msr(1280, 720), output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_video_dimensions"] == "fail"

    def test_rejected_on_wrong_video_dimensions(self, tmp_path):
        report = _run(media_specs_reports=_good_msr(1280, 720), output_dir=tmp_path)
        assert report["verdict"] == "REJECTED"

    def test_warn_when_no_media_specs(self, tmp_path):
        report = _run(media_specs_reports=None, output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_video_dimensions"] == "warn"

    def test_warn_when_media_specs_has_no_video_report(self, tmp_path):
        audio_report = {
            "report_id": "msr_002",
            "episode_id": "ep_000",
            "file_path": "/path/audio.mp3",
            "file_type": "audio",
            "file_valid": True,
            "publish_ready": True,
            "specs_observed": {"bitrate": 128000},
            "specs_expected": {},
            "failures": [],
            "warnings": [],
            "checked_at": "2026-06-12T00:00:00+00:00",
        }
        report = _run(media_specs_reports=[audio_report], output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_video_dimensions"] == "warn"


# ---------------------------------------------------------------------------
# TestThumbnailDimensions
# ---------------------------------------------------------------------------

class TestThumbnailDimensions:
    def test_pass_on_correct_dimensions(self, tmp_path):
        report = _run(thumbnail_package=_good_thumb(), output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_thumbnail_dimensions"] == "pass"

    def test_fail_on_wrong_thumbnail_dimensions(self, tmp_path):
        thumb = _good_thumb()
        thumb["main_thumbnail"]["width"] = 1920
        thumb["main_thumbnail"]["height"] = 1080
        report = _run(thumbnail_package=thumb, output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_thumbnail_dimensions"] == "fail"

    def test_rejected_on_wrong_thumbnail_dimensions(self, tmp_path):
        thumb = _good_thumb()
        thumb["main_thumbnail"]["width"] = 1920
        thumb["main_thumbnail"]["height"] = 1080
        report = _run(thumbnail_package=thumb, output_dir=tmp_path)
        assert report["verdict"] == "REJECTED"

    def test_warn_when_no_thumbnail_package(self, tmp_path):
        report = _run(thumbnail_package=None, output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_thumbnail_dimensions"] == "warn"

    def test_warn_when_dimensions_not_in_main_thumbnail(self, tmp_path):
        thumb = _good_thumb()
        del thumb["main_thumbnail"]["width"]
        del thumb["main_thumbnail"]["height"]
        report = _run(thumbnail_package=thumb, output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_thumbnail_dimensions"] == "warn"


# ---------------------------------------------------------------------------
# TestDescriptionPresent
# ---------------------------------------------------------------------------

class TestDescriptionPresent:
    def test_pass_when_description_present(self, tmp_path):
        report = _run(output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_description_present"] == "pass"

    def test_fail_when_description_empty(self, tmp_path):
        frp = _base_frp()
        frp["metadata_source"]["description"] = ""
        report = _run(final_render_package=frp, output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_description_present"] == "fail"

    def test_rejected_when_description_absent(self, tmp_path):
        frp = _base_frp()
        del frp["metadata_source"]["description"]
        report = _run(final_render_package=frp, output_dir=tmp_path)
        assert report["verdict"] == "REJECTED"
        assert any("description" in f for f in report["failures"])

    def test_fail_when_description_key_missing(self, tmp_path):
        frp = _base_frp()
        frp["metadata_source"].pop("description", None)
        report = _run(final_render_package=frp, output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_description_present"] == "fail"


# ---------------------------------------------------------------------------
# TestChapterMarkers
# ---------------------------------------------------------------------------

class TestChapterMarkers:
    def test_pass_when_section_titles_present(self, tmp_path):
        report = _run(output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_chapter_markers"] == "pass"

    def test_warn_when_section_titles_empty(self, tmp_path):
        frp = _base_frp()
        frp["metadata_source"]["section_titles"] = []
        report = _run(final_render_package=frp, output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_chapter_markers"] == "warn"

    def test_warn_does_not_cause_rejection(self, tmp_path):
        frp = _base_frp()
        frp["metadata_source"]["section_titles"] = []
        report = _run(
            final_render_package=frp,
            render_review_verdict={
                "verdict_id": "rrv_001",
                "episode_id": "ep_000",
                "manifest_id": "mfst_001",
                "reviewed_at": "2026-06-12T00:00:00+00:00",
                "production_ready": True,
                "overall_verdict": "APPROVED",
                "checks": [],
                "failures": [],
                "warnings": [],
                "human_review_recommended": False,
                "human_review_reasons": [],
            },
            audio_qa_verdict={
                "verdict_id": "aqv_001",
                "episode_id": "ep_000",
                "evaluated_at": "2026-06-12T00:00:00+00:00",
                "overall_status": "pass",
                "production_ready": True,
                "total_beats": 5,
                "beats_generated": 5,
                "beats_failed": 0,
                "failure_rate": 0.0,
                "total_characters_billed": 200,
                "checks": [],
            },
            output_dir=tmp_path,
        )
        # Should be WARNING (chapter warning) not REJECTED
        assert report["approved"] is True

    def test_warn_when_section_titles_missing(self, tmp_path):
        frp = _base_frp()
        frp["metadata_source"].pop("section_titles", None)
        report = _run(final_render_package=frp, output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_chapter_markers"] == "warn"


# ---------------------------------------------------------------------------
# TestCaptionsUpgraded
# ---------------------------------------------------------------------------

class TestCaptionsUpgraded:
    def test_captions_empty_causes_fail(self, tmp_path):
        frp = _base_frp()
        frp["episode_render"]["caption_asset_id"] = ""
        report = _run(final_render_package=frp, output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_captions_asset"] == "fail"

    def test_captions_empty_causes_rejected(self, tmp_path):
        frp = _base_frp()
        frp["episode_render"]["caption_asset_id"] = ""
        report = _run(final_render_package=frp, output_dir=tmp_path)
        assert report["verdict"] == "REJECTED"

    def test_captions_present_still_passes(self, tmp_path):
        report = _run(output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_captions_asset"] == "pass"


# ---------------------------------------------------------------------------
# TestThumbnailPresentUpgrade
# ---------------------------------------------------------------------------

class TestThumbnailPresentUpgrade:
    def test_empty_asset_id_causes_fail(self, tmp_path):
        thumb = _good_thumb()
        thumb["main_thumbnail"]["asset_id"] = ""
        report = _run(thumbnail_package=thumb, output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_thumbnail_present"] == "fail"

    def test_empty_asset_id_causes_rejected(self, tmp_path):
        thumb = _good_thumb()
        thumb["main_thumbnail"]["asset_id"] = ""
        report = _run(thumbnail_package=thumb, output_dir=tmp_path)
        assert report["verdict"] == "REJECTED"

    def test_none_thumbnail_still_produces_warn(self, tmp_path):
        """When no thumbnail_package is provided at all, still WARN (not FAIL)."""
        report = _run(thumbnail_package=None, output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_thumbnail_present"] == "warn"


# ---------------------------------------------------------------------------
# TestMetadataVersion
# ---------------------------------------------------------------------------

class TestMetadataVersion:
    def test_metadata_version_in_report(self, tmp_path):
        report = _run(output_dir=tmp_path)
        assert report["metadata_version"] == METADATA_VERSION

    def test_metadata_version_in_qa_report_file(self, tmp_path):
        _run(output_dir=tmp_path)
        content = json.loads((tmp_path / "qa_report_ep_000.json").read_text())
        assert content["metadata_version"] == METADATA_VERSION
