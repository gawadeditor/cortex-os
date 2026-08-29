"""
tests/test_qa_agent.py — QA Agent (R47) test suite

Coverage:
  TestQAAgentApproved        — APPROVED verdict when all inputs are clean and complete
  TestQAAgentWarnings        — WARNING verdict when optional inputs are absent (no failures)
  TestQAAgentRejected        — REJECTED verdict on each category of blocking failure
  TestQAAgentChecks          — check IDs present and status values correct
  TestQAAgentOutputFile      — report written to disk at expected path with correct content
  TestQAAgentSchema          — schema validation passes on all emitted reports
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from hypogeum.agents.qa_agent import run_qa_agent
from hypogeum.schemas import validate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _good_final_render_package() -> dict:
    """Minimal valid final_render_package that satisfies all QA checks."""
    return {
        "package_integrity": {"production_ready": True},
        "assembly_qa_report": {"overall_status": "pass"},
        "music_log": [
            {"log_id": "ml_main_episode_0", "section_key": "main_episode_0",
             "category": "Mystery", "source": "channel_library",
             "asset_id": "music/mystery/track_abc"},
        ],
        "episode_render": {
            "video_asset_id": "vid_abc123",
            "caption_asset_id": "cap_abc123",
        },
        "metadata_source": {
            "episode_title": "The Antikythera Mechanism",
            "ai_disclosure_text": "This video was made with AI assistance.",
            "description": "A deep dive into the ancient Antikythera mechanism.",
            "section_titles": ["Intro", "Discovery", "Outro"],
        },
        "timeline_qa_report": {
            "overall_status": "pass",
            "checks": [],
            "warnings": [],
            "failures": [],
        },
    }


def _good_media_specs_reports() -> list:
    """Media specs report with correct video dimensions (1920×1080)."""
    return [
        {
            "report_id": "msr_test_001",
            "episode_id": "ep_test",
            "file_path": "/path/to/episode.mp4",
            "file_type": "video",
            "file_valid": True,
            "publish_ready": True,
            "specs_observed": {"width": 1920, "height": 1080},
            "specs_expected": {"width": 1920, "height": 1080},
            "failures": [],
            "warnings": [],
            "checked_at": "2026-06-12T00:00:00+00:00",
        }
    ]


def _good_render_review_verdict() -> dict:
    return {
        "verdict_id": "rrv_test_abc12345",
        "episode_id": "ep_test",
        "manifest_id": "mfst_test",
        "reviewed_at": "2026-06-07T00:00:00+00:00",
        "production_ready": True,
        "overall_verdict": "APPROVED",
        "checks": [],
        "failures": [],
        "warnings": [],
        "human_review_recommended": False,
        "human_review_reasons": [],
    }


def _good_audio_qa_verdict() -> dict:
    return {
        "verdict_id": "aqv_test",
        "episode_id": "ep_test",
        "evaluated_at": "2026-06-07T00:00:00+00:00",
        "overall_status": "pass",
        "production_ready": True,
        "total_beats": 10,
        "beats_generated": 10,
        "beats_failed": 0,
        "failure_rate": 0.0,
        "total_characters_billed": 500,
        "checks": [],
    }


def _good_thumbnail_package() -> dict:
    return {
        "thumbnail_package_id": "tp_test",
        "episode_id": "ep_test",
        "main_thumbnail": {"asset_id": "thumb_abc123", "width": 1280, "height": 720},
        "short_thumbnails": [],
        "package_integrity": {"production_ready": True},
        "thumbnail_qa_report": {
            "overall_status": "pass",
            "checks": [],
            "warnings": [],
            "failures": [],
        },
    }


def _write_editorial_qa_approved(output_dir: Path, episode_id: str = "ep_test") -> None:
    """Write a passing editorial_qa report to output_dir for QA agent tests."""
    report = {
        "report_id": f"editorial_qa_{episode_id}_test",
        "episode_id": episode_id,
        "evaluated_at": "2026-07-05T12:00:00+00:00",
        "overall_verdict": "APPROVED",
        "approved": True,
        "checks": [
            {"check_id": f"check_{i:02d}", "status": "pass", "detail": "OK"}
            for i in range(1, 9)
        ],
        "failures": [],
        "warnings": [],
    }
    (output_dir / f"editorial_qa_{episode_id}.json").write_text(
        json.dumps(report), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# TestQAAgentApproved
# ---------------------------------------------------------------------------

class TestQAAgentApproved:
    def test_approved_verdict_all_inputs_present(self, tmp_path):
        _write_editorial_qa_approved(tmp_path)
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            render_review_verdict=_good_render_review_verdict(),
            audio_qa_verdict=_good_audio_qa_verdict(),
            media_specs_reports=_good_media_specs_reports(),
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "APPROVED"

    def test_approved_sets_approved_true(self, tmp_path):
        _write_editorial_qa_approved(tmp_path)
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            render_review_verdict=_good_render_review_verdict(),
            audio_qa_verdict=_good_audio_qa_verdict(),
            media_specs_reports=_good_media_specs_reports(),
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        assert report["approved"] is True

    def test_approved_has_no_failures(self, tmp_path):
        _write_editorial_qa_approved(tmp_path)
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            render_review_verdict=_good_render_review_verdict(),
            audio_qa_verdict=_good_audio_qa_verdict(),
            media_specs_reports=_good_media_specs_reports(),
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        assert report["failures"] == []

    def test_approved_has_no_warnings(self, tmp_path):
        _write_editorial_qa_approved(tmp_path)
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            render_review_verdict=_good_render_review_verdict(),
            audio_qa_verdict=_good_audio_qa_verdict(),
            media_specs_reports=_good_media_specs_reports(),
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        assert report["warnings"] == []


# ---------------------------------------------------------------------------
# TestQAAgentWarnings
# ---------------------------------------------------------------------------

class TestQAAgentWarnings:
    def test_warning_verdict_when_no_render_review(self, tmp_path):
        """Absent render_review_verdict → warning, not failure → WARNING verdict."""
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            render_review_verdict=None,
            audio_qa_verdict=_good_audio_qa_verdict(),
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "WARNING"
        assert report["approved"] is True

    def test_warning_verdict_when_no_audio_qa(self, tmp_path):
        """Absent audio_qa_verdict → warning, not failure → WARNING verdict."""
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            render_review_verdict=_good_render_review_verdict(),
            audio_qa_verdict=None,
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "WARNING"
        assert report["approved"] is True

    def test_warning_verdict_when_no_thumbnail(self, tmp_path):
        """Absent thumbnail_package → warning, not failure → WARNING verdict."""
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            render_review_verdict=_good_render_review_verdict(),
            audio_qa_verdict=_good_audio_qa_verdict(),
            thumbnail_package=None,
            output_dir=tmp_path,
        )
        assert report["verdict"] == "WARNING"
        assert report["approved"] is True

    def test_rejected_when_no_caption_asset(self, tmp_path):
        """Empty caption_asset_id → failure (blocking) — captions are required."""
        pkg = _good_final_render_package()
        pkg["episode_render"]["caption_asset_id"] = ""
        report = run_qa_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            render_review_verdict=_good_render_review_verdict(),
            audio_qa_verdict=_good_audio_qa_verdict(),
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "REJECTED"
        assert report["approved"] is False
        assert any("caption" in f for f in report["failures"])

    def test_warning_when_render_review_conditional(self, tmp_path):
        """render_review CONDITIONAL → warning, not failure."""
        rrv = _good_render_review_verdict()
        rrv["overall_verdict"] = "CONDITIONAL"
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            render_review_verdict=rrv,
            audio_qa_verdict=_good_audio_qa_verdict(),
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "WARNING"
        assert report["approved"] is True

    def test_warning_when_assembly_qa_warn(self, tmp_path):
        """assembly_qa_report.overall_status=warn → warning, not failure."""
        pkg = _good_final_render_package()
        pkg["assembly_qa_report"]["overall_status"] = "warn"
        report = run_qa_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            render_review_verdict=_good_render_review_verdict(),
            audio_qa_verdict=_good_audio_qa_verdict(),
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "WARNING"
        assert report["approved"] is True


# ---------------------------------------------------------------------------
# TestQAAgentRejected
# ---------------------------------------------------------------------------

class TestQAAgentRejected:
    def test_rejected_when_package_not_production_ready(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["package_integrity"]["production_ready"] = False
        report = run_qa_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert report["verdict"] == "REJECTED"
        assert report["approved"] is False

    def test_rejected_when_assembly_qa_fails(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["assembly_qa_report"]["overall_status"] = "fail"
        report = run_qa_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            render_review_verdict=_good_render_review_verdict(),
            audio_qa_verdict=_good_audio_qa_verdict(),
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "REJECTED"
        assert report["approved"] is False

    def test_rejected_when_no_video_asset(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["episode_render"]["video_asset_id"] = ""
        report = run_qa_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            render_review_verdict=_good_render_review_verdict(),
            audio_qa_verdict=_good_audio_qa_verdict(),
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "REJECTED"
        assert any("video_asset_id" in f for f in report["failures"])

    def test_rejected_when_no_episode_title(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["metadata_source"]["episode_title"] = ""
        report = run_qa_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            render_review_verdict=_good_render_review_verdict(),
            audio_qa_verdict=_good_audio_qa_verdict(),
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "REJECTED"
        assert any("episode_title" in f for f in report["failures"])

    def test_rejected_when_no_ai_disclosure(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["metadata_source"]["ai_disclosure_text"] = ""
        report = run_qa_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            render_review_verdict=_good_render_review_verdict(),
            audio_qa_verdict=_good_audio_qa_verdict(),
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "REJECTED"
        assert any("ai_disclosure" in f for f in report["failures"])

    def test_rejected_when_render_review_rejected(self, tmp_path):
        rrv = _good_render_review_verdict()
        rrv["overall_verdict"] = "REJECTED"
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            render_review_verdict=rrv,
            audio_qa_verdict=_good_audio_qa_verdict(),
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "REJECTED"
        assert any("render_review" in f for f in report["failures"])

    def test_rejected_when_audio_not_production_ready(self, tmp_path):
        aqv = _good_audio_qa_verdict()
        aqv["production_ready"] = False
        aqv["overall_status"] = "fail"
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            render_review_verdict=_good_render_review_verdict(),
            audio_qa_verdict=aqv,
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "REJECTED"
        assert any("audio_qa" in f for f in report["failures"])


# ---------------------------------------------------------------------------
# TestQAAgentChecks
# ---------------------------------------------------------------------------

class TestQAAgentChecks:
    def test_all_checks_present(self, tmp_path):
        """All 14 check IDs appear in the checks list."""
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            render_review_verdict=_good_render_review_verdict(),
            audio_qa_verdict=_good_audio_qa_verdict(),
            media_specs_reports=_good_media_specs_reports(),
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        check_ids = {c["check_id"] for c in report["checks"]}
        expected = {
            "c_render_package_production_ready",
            "c_assembly_qa_report",
            "c_music_present",
            "c_main_video_asset",
            "c_captions_asset",
            "c_metadata_complete",
            "c_render_review",
            "c_audio_qa",
            "c_thumbnail_present",
            "c_timeline_qa",
            "c_thumbnail_qa",
            "c_video_dimensions",
            "c_thumbnail_dimensions",
            "c_description_present",
            "c_chapter_markers",
            "c_editorial_qa",
        }
        assert expected == check_ids

    def test_each_check_has_required_fields(self, tmp_path):
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        for check in report["checks"]:
            assert "check_id" in check
            assert "status" in check
            assert "detail" in check
            assert check["status"] in ("pass", "warn", "fail")

    def test_check_status_pass_when_all_clean(self, tmp_path):
        _write_editorial_qa_approved(tmp_path)
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            render_review_verdict=_good_render_review_verdict(),
            audio_qa_verdict=_good_audio_qa_verdict(),
            media_specs_reports=_good_media_specs_reports(),
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        # All checks should pass when all inputs are clean
        for check_id, status in statuses.items():
            assert status == "pass", f"{check_id} expected pass, got {status!r}"

    def test_render_package_check_fail_when_not_ready(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["package_integrity"]["production_ready"] = False
        report = run_qa_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_render_package_production_ready"] == "fail"

    def test_music_present_check_fails_on_empty_music_log(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["music_log"] = []
        report = run_qa_agent(final_render_package=pkg, episode_id="ep_test",
                              output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_music_present"] == "fail"
        assert report["verdict"] != "APPROVED"

    def test_music_present_check_fails_when_only_failure_entries(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["music_log"] = [
            {"log_id": "ml_x_failed", "section_key": "s0", "category": "Mystery",
             "source": None, "asset_id": None, "fallback_trigger": "provider_failure"},
        ]
        report = run_qa_agent(final_render_package=pkg, episode_id="ep_test",
                              output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_music_present"] == "fail"


# ---------------------------------------------------------------------------
# TestQAAgentOutputFile
# ---------------------------------------------------------------------------

class TestQAAgentOutputFile:
    def test_report_written_to_disk(self, tmp_path):
        run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_001",
            output_dir=tmp_path,
        )
        out = tmp_path / "qa_approval_report_ep_001.json"
        assert out.exists(), "qa_approval_report_ep_001.json not found"

    def test_written_file_is_valid_json(self, tmp_path):
        run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_001",
            output_dir=tmp_path,
        )
        out = tmp_path / "qa_approval_report_ep_001.json"
        data = json.loads(out.read_text())
        assert isinstance(data, dict)

    def test_written_file_matches_returned_report(self, tmp_path):
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_001",
            output_dir=tmp_path,
        )
        out = tmp_path / "qa_approval_report_ep_001.json"
        data = json.loads(out.read_text())
        assert data["report_id"] == report["report_id"]
        assert data["verdict"] == report["verdict"]

    def test_episode_id_in_report(self, tmp_path):
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_special",
            output_dir=tmp_path,
        )
        assert report["episode_id"] == "ep_special"

    def test_report_id_is_unique_per_call(self, tmp_path):
        r1 = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_001",
            output_dir=tmp_path / "r1",
        )
        r2 = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_001",
            output_dir=tmp_path / "r2",
        )
        assert r1["report_id"] != r2["report_id"]


# ---------------------------------------------------------------------------
# TestQAAgentSchema
# ---------------------------------------------------------------------------

class TestQAAgentSchema:
    def test_schema_validates_approved_report(self, tmp_path):
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            render_review_verdict=_good_render_review_verdict(),
            audio_qa_verdict=_good_audio_qa_verdict(),
            thumbnail_package=_good_thumbnail_package(),
            output_dir=tmp_path,
        )
        validate("qa_approval_report", report)  # must not raise

    def test_schema_validates_warning_report(self, tmp_path):
        report = run_qa_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            render_review_verdict=None,
            output_dir=tmp_path,
        )
        validate("qa_approval_report", report)

    def test_schema_validates_rejected_report(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["package_integrity"]["production_ready"] = False
        report = run_qa_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        validate("qa_approval_report", report)

    def test_schema_is_registered(self):
        from hypogeum.schemas import SCHEMA_NAMES
        assert "qa_approval_report" in SCHEMA_NAMES
