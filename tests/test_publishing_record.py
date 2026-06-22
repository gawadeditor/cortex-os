"""
tests/test_publishing_record.py — Unit tests for hypogeum/publishing/record.py

Tests:
  - initialize() creates record and writes to disk
  - append_result() appends entry and updates summary
  - update summary after each append
  - seal() computes overall_status and production_ready
  - overall_status logic (published / partially_published / failed)
  - incremental write after each operation
  - make_result_entry() factory
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hypogeum.publishing.record import PublishingRecord, make_result_entry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _published_result(platform="youtube", deliverable_type="main_episode", short_id=None):
    return make_result_entry(
        platform=platform,
        deliverable_type=deliverable_type,
        status="published",
        short_id=short_id,
        platform_video_id="vid_123",
        ai_disclosure_included=True,
    )


def _failed_result(platform="facebook", deliverable_type="main_episode", short_id=None):
    return make_result_entry(
        platform=platform,
        deliverable_type=deliverable_type,
        status="failed",
        short_id=short_id,
        ai_disclosure_included=True,
    )


def _skipped_result(platform="tiktok", deliverable_type="short", short_id="sh_001"):
    return make_result_entry(
        platform=platform,
        deliverable_type=deliverable_type,
        status="skipped",
        short_id=short_id,
        skipped_reason="tiktok_unavailable",
        ai_disclosure_included=False,
    )


# ---------------------------------------------------------------------------
# TestPublishingRecordInitialize
# ---------------------------------------------------------------------------

class TestPublishingRecordInitialize:
    def test_initialize_creates_record(self, tmp_path):
        record = PublishingRecord("ep_test_001", output_dir=tmp_path)
        record.initialize()

        data = record.to_dict()
        assert data["package_metadata"]["episode_id"] == "ep_test_001"
        assert data["package_metadata"]["overall_status"] == "in_progress"
        assert data["platform_results"] == []
        assert data["failures"] == []

    def test_initialize_writes_to_disk(self, tmp_path):
        record = PublishingRecord("ep_test_002", output_dir=tmp_path)
        record.initialize()

        write_path = tmp_path / "publishing_record_ep_test_002.json"
        assert write_path.exists()

        with open(write_path) as f:
            data = json.load(f)
        assert data["package_metadata"]["episode_id"] == "ep_test_002"

    def test_initialize_sets_publishing_record_id(self, tmp_path):
        record = PublishingRecord("ep_test_003", output_dir=tmp_path)
        record.initialize()

        data = record.to_dict()
        assert "publishing_record_id" in data["package_metadata"]
        assert data["package_metadata"]["publishing_record_id"].startswith("pub_ep_test_003_")

    def test_initialize_summary_all_zero(self, tmp_path):
        record = PublishingRecord("ep_test_004", output_dir=tmp_path)
        record.initialize()

        summary = record.to_dict()["summary"]
        assert summary["youtube_main_published"] is False
        assert summary["total_deliverables_attempted"] == 0
        assert summary["total_published"] == 0
        assert summary["total_failed"] == 0
        assert summary["total_skipped"] == 0


# ---------------------------------------------------------------------------
# TestPublishingRecordAppendResult
# ---------------------------------------------------------------------------

class TestPublishingRecordAppendResult:
    def test_append_adds_to_platform_results(self, tmp_path):
        record = PublishingRecord("ep_001", output_dir=tmp_path)
        record.initialize()
        record.append_result(_published_result("youtube", "main_episode"))

        data = record.to_dict()
        assert len(data["platform_results"]) == 1
        assert data["platform_results"][0]["platform"] == "youtube"
        assert data["platform_results"][0]["status"] == "published"

    def test_append_updates_summary_counts(self, tmp_path):
        record = PublishingRecord("ep_002", output_dir=tmp_path)
        record.initialize()
        record.append_result(_published_result("youtube", "main_episode"))
        record.append_result(_failed_result("facebook"))

        summary = record.to_dict()["summary"]
        assert summary["total_published"] == 1
        assert summary["total_failed"] == 1
        assert summary["total_deliverables_attempted"] == 2

    def test_append_counts_skipped_separately(self, tmp_path):
        record = PublishingRecord("ep_003", output_dir=tmp_path)
        record.initialize()
        record.append_result(_skipped_result())

        summary = record.to_dict()["summary"]
        assert summary["total_skipped"] == 1
        assert summary["total_deliverables_attempted"] == 0

    def test_append_writes_to_disk_immediately(self, tmp_path):
        record = PublishingRecord("ep_004", output_dir=tmp_path)
        record.initialize()
        record.append_result(_published_result())

        write_path = tmp_path / "publishing_record_ep_004.json"
        with open(write_path) as f:
            data = json.load(f)
        assert len(data["platform_results"]) == 1

    def test_append_sets_youtube_main_published(self, tmp_path):
        record = PublishingRecord("ep_005", output_dir=tmp_path)
        record.initialize()
        record.append_result(_published_result("youtube", "main_episode"))

        summary = record.to_dict()["summary"]
        assert summary["youtube_main_published"] is True

    def test_append_sets_youtube_main_video_id(self, tmp_path):
        # Regression: summary must include youtube_main_video_id so pipeline.py
        # can write analytics_pending after a successful YouTube publish.
        record = PublishingRecord("ep_005b", output_dir=tmp_path)
        record.initialize()
        record.append_result(_published_result("youtube", "main_episode"))

        summary = record.to_dict()["summary"]
        assert summary.get("youtube_main_video_id") == "vid_123"

    def test_youtube_main_video_id_none_when_not_published(self, tmp_path):
        record = PublishingRecord("ep_005c", output_dir=tmp_path)
        record.initialize()
        record.append_result(_failed_result("youtube", "main_episode"))

        summary = record.to_dict()["summary"]
        assert summary.get("youtube_main_video_id") is None

    def test_youtube_main_video_id_not_set_by_short(self, tmp_path):
        # A published short must not overwrite the main video ID slot.
        record = PublishingRecord("ep_005d", output_dir=tmp_path)
        record.initialize()
        record.append_result(_published_result("youtube", "short", "sh_001"))

        summary = record.to_dict()["summary"]
        assert summary.get("youtube_main_video_id") is None
        assert summary["youtube_main_published"] is False

    def test_append_multiple_results(self, tmp_path):
        record = PublishingRecord("ep_006", output_dir=tmp_path)
        record.initialize()

        record.append_result(_published_result("youtube", "main_episode"))
        record.append_result(_published_result("youtube", "short", "sh_001"))
        record.append_result(_failed_result("facebook"))
        record.append_result(_skipped_result())

        data = record.to_dict()
        assert len(data["platform_results"]) == 4
        assert data["summary"]["total_published"] == 2
        assert data["summary"]["total_failed"] == 1
        assert data["summary"]["total_skipped"] == 1

    def test_append_auto_generates_result_id_if_missing(self, tmp_path):
        record = PublishingRecord("ep_007", output_dir=tmp_path)
        record.initialize()
        # Entry without result_id
        entry = {
            "platform": "youtube",
            "deliverable_type": "main_episode",
            "status": "published",
            "metadata_applied": {"ai_disclosure_included": True},
        }
        record.append_result(entry)

        data = record.to_dict()
        assert data["platform_results"][0].get("result_id", "").startswith("res_")


# ---------------------------------------------------------------------------
# TestPublishingRecordSeal
# ---------------------------------------------------------------------------

class TestPublishingRecordSeal:
    def test_seal_sets_overall_status_published(self, tmp_path):
        record = PublishingRecord("ep_seal_001", output_dir=tmp_path)
        record.initialize()
        record.append_result(_published_result("youtube", "main_episode"))
        sealed = record.seal()

        assert sealed["package_metadata"]["overall_status"] == "published"
        assert sealed["package_integrity"]["production_ready"] is True

    def test_seal_sets_overall_status_partially_published(self, tmp_path):
        record = PublishingRecord("ep_seal_002", output_dir=tmp_path)
        record.initialize()
        record.append_result(_failed_result("youtube", "main_episode"))
        record.append_result(_published_result("facebook", "main_episode"))
        sealed = record.seal()

        assert sealed["package_metadata"]["overall_status"] == "partially_published"
        assert sealed["package_integrity"]["production_ready"] is False

    def test_seal_sets_overall_status_failed(self, tmp_path):
        record = PublishingRecord("ep_seal_003", output_dir=tmp_path)
        record.initialize()
        record.append_result(_failed_result("youtube", "main_episode"))
        record.append_result(_failed_result("facebook"))
        sealed = record.seal()

        assert sealed["package_metadata"]["overall_status"] == "failed"
        assert sealed["package_integrity"]["production_ready"] is False

    def test_seal_skipped_only_means_failed_status(self, tmp_path):
        record = PublishingRecord("ep_seal_004", output_dir=tmp_path)
        record.initialize()
        record.append_result(_skipped_result())
        sealed = record.seal()

        assert sealed["package_metadata"]["overall_status"] == "failed"

    def test_seal_sets_sealed_at(self, tmp_path):
        record = PublishingRecord("ep_seal_005", output_dir=tmp_path)
        record.initialize()
        record.append_result(_published_result("youtube", "main_episode"))
        sealed = record.seal()

        assert sealed["package_metadata"]["sealed_at"] is not None

    def test_seal_writes_to_disk(self, tmp_path):
        record = PublishingRecord("ep_seal_006", output_dir=tmp_path)
        record.initialize()
        record.append_result(_published_result("youtube", "main_episode"))
        record.seal()

        write_path = tmp_path / "publishing_record_ep_seal_006.json"
        with open(write_path) as f:
            data = json.load(f)
        assert data["package_metadata"]["overall_status"] == "published"

    def test_youtube_main_published_property(self, tmp_path):
        record = PublishingRecord("ep_prop_001", output_dir=tmp_path)
        record.initialize()
        assert record.youtube_main_published is False

        record.append_result(_published_result("youtube", "main_episode"))
        assert record.youtube_main_published is True


# ---------------------------------------------------------------------------
# TestMakeResultEntry
# ---------------------------------------------------------------------------

class TestMakeResultEntry:
    def test_basic_published_entry(self):
        entry = make_result_entry(
            platform="youtube",
            deliverable_type="main_episode",
            status="published",
        )
        assert entry["platform"] == "youtube"
        assert entry["deliverable_type"] == "main_episode"
        assert entry["status"] == "published"
        assert "result_id" in entry
        assert entry["result_id"].startswith("res_")

    def test_metadata_applied_fields(self):
        entry = make_result_entry(
            platform="youtube",
            deliverable_type="main_episode",
            status="published",
            title="Test Title",
            description_length_chars=500,
            tags_count=5,
            thumbnail_applied=True,
            captions_submitted=True,
            ai_disclosure_included=True,
            end_screen_applied=True,
        )
        meta = entry["metadata_applied"]
        assert meta["title"] == "Test Title"
        assert meta["description_length_chars"] == 500
        assert meta["tags_count"] == 5
        assert meta["thumbnail_applied"] is True
        assert meta["captions_submitted"] is True
        assert meta["ai_disclosure_included"] is True
        assert meta["end_screen_applied"] is True

    def test_failed_entry(self):
        entry = make_result_entry(
            platform="facebook",
            deliverable_type="main_episode",
            status="failed",
            ai_disclosure_included=False,
        )
        assert entry["status"] == "failed"
        assert entry["metadata_applied"]["ai_disclosure_included"] is False

    def test_skipped_entry(self):
        entry = make_result_entry(
            platform="tiktok",
            deliverable_type="short",
            status="skipped",
            short_id="sh_001",
            skipped_reason="tiktok_unavailable",
            ai_disclosure_included=False,
        )
        assert entry["status"] == "skipped"
        assert entry["short_id"] == "sh_001"
        assert entry["skipped_reason"] == "tiktok_unavailable"

    def test_unique_result_ids(self):
        entry1 = make_result_entry(platform="youtube", deliverable_type="main_episode", status="published")
        entry2 = make_result_entry(platform="youtube", deliverable_type="main_episode", status="published")
        assert entry1["result_id"] != entry2["result_id"]

    def test_incremental_write_after_each_append(self, tmp_path):
        """Verify disk state reflects each individual append."""
        record = PublishingRecord("ep_incr_001", output_dir=tmp_path)
        record.initialize()

        write_path = tmp_path / "publishing_record_ep_incr_001.json"

        record.append_result(_published_result("youtube", "main_episode"))
        with open(write_path) as f:
            data = json.load(f)
        assert len(data["platform_results"]) == 1

        record.append_result(_published_result("youtube", "short", "sh_001"))
        with open(write_path) as f:
            data = json.load(f)
        assert len(data["platform_results"]) == 2

        record.append_result(_failed_result("facebook"))
        with open(write_path) as f:
            data = json.load(f)
        assert len(data["platform_results"]) == 3
