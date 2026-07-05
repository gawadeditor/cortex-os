"""
tests/test_archive_agent.py — Archive Agent (R49) test suite

Coverage:
  TestArchiveAgentArchived       — ARCHIVED verdict when all outcomes are clean
  TestArchiveAgentWarnings       — WARNING verdict for each non-blocking condition
  TestArchiveAgentFailed         — FAILED verdict when publishing completely failed
  TestArchiveAgentChecks         — all 5 check IDs present; correct status values
  TestArchiveAgentEpisodeSummary — episode_summary fields populated correctly
  TestArchiveAgentOutputFile     — record written to disk at expected path
  TestArchiveAgentSchema         — validate() passes; schema is registered
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hypogeum.agents.archive_agent import run_archive_agent
from hypogeum.schemas import validate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _good_final_render_package() -> dict:
    return {
        "metadata_source": {
            "episode_title": "The Antikythera Mechanism",
            "ai_disclosure_text": "This video was created with AI-generated content.",
            "section_titles": ["Intro", "Discovery", "Outro"],
        },
        "episode_render": {
            "video_asset_id": "vid_abc123",
            "caption_asset_id": "cap_abc123",
        },
        "music_log": [],
        "package_integrity": {"production_ready": True},
        "assembly_qa_report": {"overall_status": "pass"},
        "shorts_renders": [],
        "thumbnail_references": [{"thumbnail_asset_id": "th_abc", "format": "jpeg"}],
        "package_metadata": {"render_package_id": "rp_001", "episode_id": "ep_test"},
    }


def _good_publishing_record() -> dict:
    return {
        "package_metadata": {
            "publishing_record_id": "pr_001",
            "episode_id": "ep_test",
            "overall_status": "published",
        },
        "platform_results": [
            {
                "result_id": "res_yt",
                "platform": "youtube",
                "status": "published",
                "metadata_applied": {"ai_disclosure_included": True},
            }
        ],
        "failures": [],
        "summary": {
            "youtube_main_published": True,
            "total_deliverables_attempted": 4,
            "total_published": 4,
            "total_failed": 0,
            "total_skipped": 0,
        },
        "package_integrity": {"production_ready": True},
    }


def _failed_publishing_record() -> dict:
    rec = _good_publishing_record()
    rec["package_metadata"]["overall_status"] = "failed"
    rec["summary"]["youtube_main_published"] = False
    rec["summary"]["total_published"] = 0
    rec["summary"]["total_failed"] = 4
    return rec


def _partial_publishing_record() -> dict:
    rec = _good_publishing_record()
    rec["package_metadata"]["overall_status"] = "partially_published"
    rec["summary"]["total_published"] = 2
    rec["summary"]["total_failed"] = 2
    return rec


# ---------------------------------------------------------------------------
# TestArchiveAgentArchived
# ---------------------------------------------------------------------------

class TestArchiveAgentArchived:
    def test_archived_verdict_all_clean(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["verdict"] == "ARCHIVED"

    def test_archived_sets_archived_true(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["archived"] is True

    def test_archived_has_no_failures(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["failures"] == []

    def test_archived_has_no_warnings(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["warnings"] == []

    def test_archive_status_is_archived(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["archive_status"] == "archived"


# ---------------------------------------------------------------------------
# TestArchiveAgentWarnings
# ---------------------------------------------------------------------------

class TestArchiveAgentWarnings:
    def test_warning_when_youtube_not_published(self, tmp_path):
        pub = _good_publishing_record()
        pub["summary"]["youtube_main_published"] = False
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=pub,
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["verdict"] == "WARNING"
        assert record["archived"] is True
        assert any("youtube_main_published" in w for w in record["warnings"])

    def test_warning_when_no_video_asset(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["episode_render"]["video_asset_id"] = ""
        record = run_archive_agent(
            final_render_package=pkg,
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["verdict"] == "WARNING"
        assert any("video_asset_referenced" in w for w in record["warnings"])

    def test_warning_when_no_episode_title(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["metadata_source"]["episode_title"] = ""
        record = run_archive_agent(
            final_render_package=pkg,
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["verdict"] == "WARNING"
        assert any("episode_metadata" in w for w in record["warnings"])

    def test_warning_when_zero_deliverables_published(self, tmp_path):
        pub = _good_publishing_record()
        pub["package_metadata"]["overall_status"] = "partially_published"
        pub["summary"]["total_published"] = 0
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=pub,
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["archived"] is True
        assert any("deliverables_count" in w for w in record["warnings"])

    def test_warning_when_partially_published(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_partial_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["verdict"] == "WARNING"
        assert record["archived"] is True
        assert any("publishing_completed" in w for w in record["warnings"])

    def test_archive_status_is_warning_on_warning(self, tmp_path):
        pub = _good_publishing_record()
        pub["summary"]["youtube_main_published"] = False
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=pub,
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["archive_status"] == "warning"


# ---------------------------------------------------------------------------
# TestArchiveAgentFailed
# ---------------------------------------------------------------------------

class TestArchiveAgentFailed:
    def test_failed_verdict_when_publish_failed(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_failed_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["verdict"] == "FAILED"

    def test_failed_sets_archived_false(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_failed_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["archived"] is False

    def test_failed_has_publishing_completed_failure(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_failed_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert any("publishing_completed" in f for f in record["failures"])

    def test_archive_status_is_failed(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_failed_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["archive_status"] == "failed"


# ---------------------------------------------------------------------------
# TestArchiveAgentChecks
# ---------------------------------------------------------------------------

class TestArchiveAgentChecks:
    def test_all_five_checks_present(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        check_ids = {c["check_id"] for c in record["checks"]}
        expected = {
            "c_publishing_completed",
            "c_youtube_main_published",
            "c_video_asset_referenced",
            "c_episode_metadata",
            "c_deliverables_count",
        }
        assert expected == check_ids

    def test_each_check_has_required_fields(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        for check in record["checks"]:
            assert "check_id" in check
            assert "status" in check
            assert "detail" in check
            assert check["status"] in ("pass", "warn", "fail")

    def test_all_checks_pass_when_clean(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        statuses = {c["check_id"]: c["status"] for c in record["checks"]}
        for check_id, status in statuses.items():
            assert status == "pass", f"{check_id}: expected pass, got {status!r}"

    def test_c_publishing_completed_fails_on_failed_status(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_failed_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        statuses = {c["check_id"]: c["status"] for c in record["checks"]}
        assert statuses["c_publishing_completed"] == "fail"

    def test_c_publishing_completed_warns_on_partial(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_partial_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        statuses = {c["check_id"]: c["status"] for c in record["checks"]}
        assert statuses["c_publishing_completed"] == "warn"

    def test_c_youtube_warns_when_not_published(self, tmp_path):
        pub = _good_publishing_record()
        pub["summary"]["youtube_main_published"] = False
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=pub,
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        statuses = {c["check_id"]: c["status"] for c in record["checks"]}
        assert statuses["c_youtube_main_published"] == "warn"

    def test_c_video_asset_warns_when_empty(self, tmp_path):
        pkg = _good_final_render_package()
        pkg["episode_render"]["video_asset_id"] = ""
        record = run_archive_agent(
            final_render_package=pkg,
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        statuses = {c["check_id"]: c["status"] for c in record["checks"]}
        assert statuses["c_video_asset_referenced"] == "warn"


# ---------------------------------------------------------------------------
# TestArchiveAgentEpisodeSummary
# ---------------------------------------------------------------------------

class TestArchiveAgentEpisodeSummary:
    def test_episode_summary_present(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert "episode_summary" in record

    def test_episode_summary_has_title(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["episode_summary"]["episode_title"] == "The Antikythera Mechanism"

    def test_episode_summary_has_youtube_flag(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["episode_summary"]["youtube_main_published"] is True

    def test_episode_summary_has_total_published(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["episode_summary"]["total_published"] == 4

    def test_episode_summary_has_video_asset_id(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["episode_summary"]["video_asset_id"] == "vid_abc123"

    def test_episode_summary_publishing_status(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        assert record["episode_summary"]["publishing_overall_status"] == "published"


# ---------------------------------------------------------------------------
# TestArchiveAgentOutputFile
# ---------------------------------------------------------------------------

class TestArchiveAgentOutputFile:
    def test_record_written_to_disk(self, tmp_path):
        run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_001",
            output_dir=tmp_path,
        )
        out = tmp_path / "archive_record_ep_001.json"
        assert out.exists(), "archive_record_ep_001.json not found"

    def test_written_file_is_valid_json(self, tmp_path):
        run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_001",
            output_dir=tmp_path,
        )
        data = json.loads((tmp_path / "archive_record_ep_001.json").read_text())
        assert isinstance(data, dict)

    def test_written_file_matches_returned_record(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_001",
            output_dir=tmp_path,
        )
        data = json.loads((tmp_path / "archive_record_ep_001.json").read_text())
        assert data["archive_id"] == record["archive_id"]
        assert data["verdict"] == record["verdict"]

    def test_episode_id_in_filename_and_record(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_special",
            output_dir=tmp_path,
        )
        assert (tmp_path / "archive_record_ep_special.json").exists()
        assert record["episode_id"] == "ep_special"

    def test_archive_id_unique_per_call(self, tmp_path):
        r1 = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_001",
            output_dir=tmp_path / "r1",
        )
        r2 = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_001",
            output_dir=tmp_path / "r2",
        )
        assert r1["archive_id"] != r2["archive_id"]


# ---------------------------------------------------------------------------
# TestArchiveAgentSchema
# ---------------------------------------------------------------------------

class TestArchiveAgentSchema:
    def test_schema_validates_archived_record(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_good_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        validate("archive_record", record)  # must not raise

    def test_schema_validates_warning_record(self, tmp_path):
        pub = _good_publishing_record()
        pub["summary"]["youtube_main_published"] = False
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=pub,
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        validate("archive_record", record)

    def test_schema_validates_failed_record(self, tmp_path):
        record = run_archive_agent(
            final_render_package=_good_final_render_package(),
            publishing_record=_failed_publishing_record(),
            episode_id="ep_test",
            output_dir=tmp_path,
        )
        validate("archive_record", record)

    def test_schema_is_registered(self):
        from hypogeum.schemas import SCHEMA_NAMES
        assert "archive_record" in SCHEMA_NAMES

    def test_schema_count_updated(self):
        from hypogeum.schemas import SCHEMA_NAMES
        assert len(SCHEMA_NAMES) == 35  # +4 for Visual Intelligence V1–V4 schemas
