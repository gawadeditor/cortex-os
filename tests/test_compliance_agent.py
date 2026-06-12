"""
tests/test_compliance_agent.py — Rights & Compliance Agent (R48) test suite

Coverage:
  TestComplianceAgentApproved    — APPROVED when all inputs are clean and complete
  TestComplianceAgentWarnings    — WARNING for each non-blocking condition
  TestComplianceAgentRejected    — REJECTED for each blocking failure type
  TestComplianceAgentChecks      — all 6 check IDs present; status values correct
  TestComplianceAgentOutputFile  — report written to disk at expected path
  TestComplianceAgentSchema      — schema validation passes; schema is registered
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hypogeum.agents.compliance_agent import run_compliance_agent
from hypogeum.schemas import validate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _good_final_render_package() -> dict:
    """Minimal valid final_render_package satisfying all compliance checks."""
    return {
        "metadata_source": {
            "episode_title": "The Antikythera Mechanism",
            "ai_disclosure_text": "This video was created with AI-generated content.",
            "section_titles": ["Intro", "Discovery", "Outro"],
        },
        "music_log": [],
        "package_integrity": {"production_ready": True},
        "assembly_qa_report": {"overall_status": "pass"},
        "episode_render": {
            "video_asset_id": "vid_abc",
            "caption_asset_id": "cap_abc",
        },
        "shorts_renders": [],
        "thumbnail_references": [{"thumbnail_asset_id": "th_abc", "format": "jpeg"}],
        "package_metadata": {"render_package_id": "rp_001", "episode_id": "ep_test"},
    }


def _good_source_registry() -> list:
    return [
        {"source_id": "s1", "url": "https://example.com/source1", "title": "Source One"},
        {"source_id": "s2", "url": "https://example.com/source2", "title": "Source Two"},
    ]


def _good_music_track_package() -> dict:
    return {
        "package_id": "mtp_test",
        "episode_id": "ep_test",
        "sourced_at": "2026-06-07T00:00:00+00:00",
        "sourcing_status": "complete",
        "tracks": {},
        "music_log": [],
        "sections_without_music": [],
    }


# ---------------------------------------------------------------------------
# TestComplianceAgentApproved
# ---------------------------------------------------------------------------

class TestComplianceAgentApproved:
    def test_approved_verdict_all_inputs_present(self, tmp_path):
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=_good_music_track_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "APPROVED"

    def test_approved_sets_compliant_true(self, tmp_path):
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=_good_music_track_package(),
            output_dir=tmp_path,
        )
        assert report["compliant"] is True

    def test_approved_has_no_failures(self, tmp_path):
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=_good_music_track_package(),
            output_dir=tmp_path,
        )
        assert report["failures"] == []

    def test_approved_has_no_warnings(self, tmp_path):
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=_good_music_track_package(),
            output_dir=tmp_path,
        )
        assert report["warnings"] == []

    def test_approved_with_channel_library_music(self, tmp_path):
        """Channel library music in music_log has no AI attribution requirement."""
        pkg = _good_final_render_package()
        pkg["music_log"] = [{"log_id": "ml_1", "source": "channel_library"}]
        report = run_compliance_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=_good_music_track_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "APPROVED"

    def test_approved_with_ai_generated_music_when_disclosure_mentions_ai(self, tmp_path):
        """AI-generated music is fine when disclosure text mentions AI."""
        pkg = _good_final_render_package()
        pkg["music_log"] = [{"log_id": "ml_1", "source": "ai_generated"}]
        # disclosure already says "AI-generated"
        report = run_compliance_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=_good_music_track_package(),
            output_dir=tmp_path,
        )
        assert report["compliant"] is True


# ---------------------------------------------------------------------------
# TestComplianceAgentWarnings
# ---------------------------------------------------------------------------

class TestComplianceAgentWarnings:
    def test_warning_when_no_source_registry(self, tmp_path):
        """Absent source_registry → warning, not failure."""
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            source_registry=None,
            music_track_package=_good_music_track_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "WARNING"
        assert report["compliant"] is True
        assert any("source_attribution" in w for w in report["warnings"])

    def test_warning_when_empty_source_registry(self, tmp_path):
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            source_registry=[],
            music_track_package=_good_music_track_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "WARNING"
        assert report["compliant"] is True

    def test_warning_when_no_music_track_package(self, tmp_path):
        """Absent music_track_package → warning (license not verified)."""
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=None,
            output_dir=tmp_path,
        )
        assert report["verdict"] == "WARNING"
        assert report["compliant"] is True
        assert any("music_license" in w for w in report["warnings"])

    def test_warning_when_music_sourcing_partial(self, tmp_path):
        mtp = _good_music_track_package()
        mtp["sourcing_status"] = "partial"
        mtp["sections_without_music"] = ["intro"]
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=mtp,
            output_dir=tmp_path,
        )
        assert report["verdict"] == "WARNING"
        assert any("music_license" in w for w in report["warnings"])

    def test_warning_when_music_sourcing_failed(self, tmp_path):
        mtp = _good_music_track_package()
        mtp["sourcing_status"] = "failed"
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=mtp,
            output_dir=tmp_path,
        )
        assert report["verdict"] == "WARNING"
        assert any("music_license" in w for w in report["warnings"])

    def test_warning_when_disclosure_lacks_ai_keyword(self, tmp_path):
        """Disclosure present but no AI keywords → warning on content check."""
        pkg = _good_final_render_package()
        pkg["metadata_source"]["ai_disclosure_text"] = (
            "This video uses third-party content. Please see description."
        )
        report = run_compliance_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=_good_music_track_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "WARNING"
        assert any("ai_disclosure_content" in w for w in report["warnings"])

    def test_warning_when_ai_music_but_disclosure_lacks_keywords(self, tmp_path):
        """AI-generated music in music_log with weak disclosure → c_music_ai_tracks warn."""
        pkg = _good_final_render_package()
        pkg["music_log"] = [{"log_id": "ml_1", "source": "ai_generated"}]
        pkg["metadata_source"]["ai_disclosure_text"] = "This video uses third-party content."
        report = run_compliance_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=_good_music_track_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "WARNING"
        assert any("music_ai_tracks" in w for w in report["warnings"])


# ---------------------------------------------------------------------------
# TestComplianceAgentRejected
# ---------------------------------------------------------------------------

class TestComplianceAgentRejected:
    def test_rejected_when_no_ai_disclosure(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["metadata_source"]["ai_disclosure_text"] = ""
        report = run_compliance_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=_good_music_track_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "REJECTED"
        assert report["compliant"] is False
        assert any("ai_disclosure_present" in f for f in report["failures"])

    def test_rejected_when_ai_disclosure_missing_from_metadata_source(self, tmp_path):
        pkg = _good_final_render_package()
        del pkg["metadata_source"]["ai_disclosure_text"]
        report = run_compliance_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert report["verdict"] == "REJECTED"
        assert report["compliant"] is False

    def test_rejected_when_no_episode_title(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["metadata_source"]["episode_title"] = ""
        report = run_compliance_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=_good_music_track_package(),
            output_dir=tmp_path,
        )
        assert report["verdict"] == "REJECTED"
        assert any("platform_metadata" in f for f in report["failures"])

    def test_rejected_when_episode_title_missing_from_metadata_source(self, tmp_path):
        pkg = _good_final_render_package()
        del pkg["metadata_source"]["episode_title"]
        report = run_compliance_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert report["verdict"] == "REJECTED"
        assert report["compliant"] is False

    def test_rejected_sets_compliant_false(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["metadata_source"]["ai_disclosure_text"] = ""
        pkg["metadata_source"]["episode_title"] = ""
        report = run_compliance_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert report["compliant"] is False
        assert len(report["failures"]) >= 2

    def test_rejected_when_both_disclosure_and_title_missing(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["metadata_source"]["ai_disclosure_text"] = ""
        pkg["metadata_source"]["episode_title"] = ""
        report = run_compliance_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert report["verdict"] == "REJECTED"
        failure_ids = " ".join(report["failures"])
        assert "ai_disclosure_present" in failure_ids
        assert "platform_metadata" in failure_ids


# ---------------------------------------------------------------------------
# TestComplianceAgentChecks
# ---------------------------------------------------------------------------

class TestComplianceAgentChecks:
    def test_all_seven_checks_present(self, tmp_path):
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=_good_music_track_package(),
            output_dir=tmp_path,
        )
        check_ids = {c["check_id"] for c in report["checks"]}
        expected = {
            "c_ai_disclosure_present",
            "c_ai_disclosure_content",
            "c_source_attribution",
            "c_music_license",
            "c_music_ai_tracks",
            "c_platform_metadata",
            "c_banned_terms",
        }
        assert expected == check_ids

    def test_each_check_has_required_fields(self, tmp_path):
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        for check in report["checks"]:
            assert "check_id" in check
            assert "status" in check
            assert "detail" in check
            assert check["status"] in ("pass", "warn", "fail")

    def test_all_checks_pass_when_inputs_clean(self, tmp_path):
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=_good_music_track_package(),
            output_dir=tmp_path,
        )
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        for check_id, status in statuses.items():
            assert status == "pass", f"{check_id}: expected pass, got {status!r}"

    def test_c_ai_disclosure_present_fails_on_empty(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["metadata_source"]["ai_disclosure_text"] = ""
        report = run_compliance_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_ai_disclosure_present"] == "fail"

    def test_c_source_attribution_warns_when_absent(self, tmp_path):
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            source_registry=None,
            music_track_package=_good_music_track_package(),
            output_dir=tmp_path,
        )
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_source_attribution"] == "warn"

    def test_c_music_license_warns_when_absent(self, tmp_path):
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=None,
            output_dir=tmp_path,
        )
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_music_license"] == "warn"

    def test_c_platform_metadata_fails_on_empty_title(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["metadata_source"]["episode_title"] = ""
        report = run_compliance_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_platform_metadata"] == "fail"

    def test_c_music_ai_tracks_warns_when_no_keywords(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["music_log"] = [{"log_id": "ml_1", "source": "ai_generated"}]
        pkg["metadata_source"]["ai_disclosure_text"] = "Watch responsibly."
        report = run_compliance_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_music_ai_tracks"] == "warn"


# ---------------------------------------------------------------------------
# TestComplianceAgentOutputFile
# ---------------------------------------------------------------------------

class TestComplianceAgentOutputFile:
    def test_report_written_to_disk(self, tmp_path):
        run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_001",
            output_dir=tmp_path,
        )
        out = tmp_path / "compliance_report_ep_001.json"
        assert out.exists(), "compliance_report_ep_001.json not found"

    def test_written_file_is_valid_json(self, tmp_path):
        run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_001",
            output_dir=tmp_path,
        )
        data = json.loads((tmp_path / "compliance_report_ep_001.json").read_text())
        assert isinstance(data, dict)

    def test_written_file_matches_returned_report(self, tmp_path):
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_001",
            output_dir=tmp_path,
        )
        data = json.loads((tmp_path / "compliance_report_ep_001.json").read_text())
        assert data["report_id"] == report["report_id"]
        assert data["verdict"] == report["verdict"]

    def test_episode_id_in_filename_and_report(self, tmp_path):
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_special",
            output_dir=tmp_path,
        )
        assert (tmp_path / "compliance_report_ep_special.json").exists()
        assert report["episode_id"] == "ep_special"

    def test_report_id_unique_per_call(self, tmp_path):
        r1 = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_001",
            output_dir=tmp_path / "r1",
        )
        r2 = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_001",
            output_dir=tmp_path / "r2",
        )
        assert r1["report_id"] != r2["report_id"]


# ---------------------------------------------------------------------------
# TestComplianceAgentSchema
# ---------------------------------------------------------------------------

class TestComplianceAgentSchema:
    def test_schema_validates_approved_report(self, tmp_path):
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            source_registry=_good_source_registry(),
            music_track_package=_good_music_track_package(),
            output_dir=tmp_path,
        )
        validate("compliance_report", report)  # must not raise

    def test_schema_validates_warning_report(self, tmp_path):
        report = run_compliance_agent(
            final_render_package=_good_final_render_package(),
            episode_id="ep_test",
            source_registry=None,
            output_dir=tmp_path,
        )
        validate("compliance_report", report)

    def test_schema_validates_rejected_report(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["metadata_source"]["ai_disclosure_text"] = ""
        report = run_compliance_agent(
            final_render_package=pkg,
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        validate("compliance_report", report)

    def test_schema_is_registered(self):
        from hypogeum.schemas import SCHEMA_NAMES
        assert "compliance_report" in SCHEMA_NAMES

    def test_schema_count_updated(self):
        from hypogeum.schemas import SCHEMA_NAMES
        assert len(SCHEMA_NAMES) == 25
