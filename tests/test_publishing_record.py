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
# TestExpectedDeliverablesProductionReady
#
# expected_deliverables is an opt-in run-contract for production_ready: a
# list of {platform, deliverable_type, count, status, visibility[,
# skipped_reason_prefix]} specs. Omitted entirely (the default), behavior is
# unchanged from before this parameter existed — that's what "legacy records
# remain valid" verifies. This replaces the earlier platform-name-only
# expected_platforms mechanism, which could not express that a successful
# Instagram container-only result is intentionally status="skipped".
# ---------------------------------------------------------------------------

def _smoke_contract(num_shorts: int = 3) -> list[dict]:
    """The exact approved smoke-test contract for one episode."""
    return [
        {"platform": "youtube", "deliverable_type": "main_episode", "count": 1,
         "status": "published", "visibility": "private"},
        {"platform": "youtube", "deliverable_type": "short", "count": num_shorts,
         "status": "published", "visibility": "private"},
        {"platform": "facebook", "deliverable_type": "main_episode", "count": 1,
         "status": "published", "visibility": "unpublished"},
        {"platform": "tiktok", "deliverable_type": "short", "count": 1,
         "status": "published", "visibility": "self_only"},
        {"platform": "instagram", "deliverable_type": "short", "count": 1,
         "status": "skipped", "visibility": "container_only",
         "skipped_reason_prefix": "instagram_container_validated"},
    ]


class TestExpectedDeliverablesProductionReady:
    def test_full_smoke_contract_all_succeed_is_ready(self, tmp_path):
        """Includes the Instagram container-only success: status=skipped,
        visibility=container_only, skipped_reason starting with the prefix."""
        record = PublishingRecord(
            "ep_scope_001", output_dir=tmp_path, expected_deliverables=_smoke_contract(num_shorts=3),
        )
        record.initialize()
        record.append_result(make_result_entry(
            platform="youtube", deliverable_type="main_episode", status="published",
            platform_video_id="yt_main", visibility="private",
        ))
        for i in range(1, 4):
            record.append_result(make_result_entry(
                platform="youtube", deliverable_type="short", status="published",
                short_id=f"short_{i:03d}", platform_video_id=f"yt_short_{i}", visibility="private",
            ))
        record.append_result(make_result_entry(
            platform="facebook", deliverable_type="main_episode", status="published",
            platform_video_id="fb_vid", visibility="unpublished",
        ))
        record.append_result(make_result_entry(
            platform="tiktok", deliverable_type="short", status="published",
            short_id="short_001", platform_video_id="tt_post", visibility="self_only",
        ))
        record.append_result(make_result_entry(
            platform="instagram", deliverable_type="short", status="skipped",
            short_id="short_001", visibility="container_only",
            skipped_reason="instagram_container_validated: creation_id=abc, terminal_status=FINISHED",
        ))
        sealed = record.seal()

        assert sealed["package_integrity"]["production_ready"] is True

    def test_expected_facebook_fails_is_not_ready(self, tmp_path):
        record = PublishingRecord(
            "ep_scope_002", output_dir=tmp_path,
            expected_deliverables=[
                {"platform": "youtube", "deliverable_type": "main_episode", "count": 1,
                 "status": "published", "visibility": "private"},
                {"platform": "facebook", "deliverable_type": "main_episode", "count": 1,
                 "status": "published", "visibility": "unpublished"},
            ],
        )
        record.initialize()
        record.append_result(make_result_entry(
            platform="youtube", deliverable_type="main_episode", status="published",
            platform_video_id="yt_main", visibility="private",
        ))
        record.append_result(_failed_result("facebook", "main_episode"))
        sealed = record.seal()

        assert sealed["package_integrity"]["production_ready"] is False

    def test_expected_deliverable_never_attempted_is_not_ready(self, tmp_path):
        """TikTok/Instagram pending — the run halted before reaching them."""
        record = PublishingRecord(
            "ep_scope_003", output_dir=tmp_path, expected_deliverables=_smoke_contract(num_shorts=3),
        )
        record.initialize()
        record.append_result(make_result_entry(
            platform="youtube", deliverable_type="main_episode", status="published",
            platform_video_id="yt_main", visibility="private",
        ))
        for i in range(1, 4):
            record.append_result(make_result_entry(
                platform="youtube", deliverable_type="short", status="published",
                short_id=f"short_{i:03d}", platform_video_id=f"yt_short_{i}", visibility="private",
            ))
        record.append_result(_failed_result("facebook", "main_episode"))
        # tiktok/instagram never attempted — no result entries appended at all.
        sealed = record.seal()

        assert sealed["package_integrity"]["production_ready"] is False

    def test_wrong_visibility_does_not_satisfy_the_deliverable(self, tmp_path):
        """A published Facebook video that is NOT unpublished doesn't count —
        the contract requires visibility, not just status."""
        record = PublishingRecord(
            "ep_scope_004b", output_dir=tmp_path,
            expected_deliverables=[
                {"platform": "facebook", "deliverable_type": "main_episode", "count": 1,
                 "status": "published", "visibility": "unpublished"},
            ],
        )
        record.initialize()
        record.append_result(make_result_entry(
            platform="facebook", deliverable_type="main_episode", status="published",
            platform_video_id="fb_vid", visibility="published",  # wrong — went public
        ))
        sealed = record.seal()

        assert sealed["package_integrity"]["production_ready"] is False

    def test_disabled_non_required_platform_does_not_block_readiness(self, tmp_path):
        """A deliverable class outside expected_deliverables never affects
        readiness, whether it's absent, skipped, or failed."""
        record = PublishingRecord(
            "ep_scope_004", output_dir=tmp_path,
            expected_deliverables=[
                {"platform": "youtube", "deliverable_type": "main_episode", "count": 1,
                 "status": "published", "visibility": "private"},
            ],
        )
        record.initialize()
        record.append_result(make_result_entry(
            platform="youtube", deliverable_type="main_episode", status="published",
            platform_video_id="yt_main", visibility="private",
        ))
        record.append_result(_failed_result("facebook", "main_episode"))
        record.append_result(_skipped_result("tiktok"))
        sealed = record.seal()

        assert sealed["package_integrity"]["production_ready"] is True

    def test_legacy_records_without_expected_deliverables_unchanged(self, tmp_path):
        """No expected_deliverables passed at all — original youtube-main-only
        semantics, exactly as before this parameter existed."""
        record = PublishingRecord("ep_scope_005", output_dir=tmp_path)
        record.initialize()
        record.append_result(_published_result("youtube", "main_episode"))
        record.append_result(_failed_result("facebook", "main_episode"))
        sealed = record.seal()

        assert sealed["package_integrity"]["production_ready"] is True
        assert sealed["package_metadata"]["overall_status"] == "published"

    def test_contract_is_persisted_for_post_restart_audit(self, tmp_path):
        """The contract must be readable from the sealed JSON itself, not
        only held in the live Python object, so a later process (or a human
        reading the file) can audit why production_ready came out as it did."""
        contract = _smoke_contract(num_shorts=1)
        record = PublishingRecord("ep_scope_006", output_dir=tmp_path, expected_deliverables=contract)
        record.initialize()
        sealed = record.seal()

        assert sealed["package_metadata"]["expected_deliverables"] == contract

        # Re-read from disk — a fresh process would only ever see this.
        on_disk = json.loads((tmp_path / "publishing_record_ep_scope_006.json").read_text())
        assert on_disk["package_metadata"]["expected_deliverables"] == contract


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

    def test_end_screen_status_defaults_to_none_when_omitted(self):
        """Platforms with no End Screen concept (non-YouTube deliverables,
        or callers that never touch this field) get None, distinct from
        the YouTube-specific states."""
        entry = make_result_entry(
            platform="facebook", deliverable_type="main_episode", status="published",
        )
        meta = entry["metadata_applied"]
        assert meta["end_screen_status"] is None
        assert meta["end_screen_applied"] is None

    def test_end_screen_status_applied_derives_true(self):
        entry = make_result_entry(
            platform="youtube", deliverable_type="main_episode", status="published",
            end_screen_status="applied",
        )
        meta = entry["metadata_applied"]
        assert meta["end_screen_status"] == "applied"
        assert meta["end_screen_applied"] is True

    def test_end_screen_status_unsupported_derives_false_not_null(self):
        """The ep_f3619022 defect signature: this must be False, not None,
        once a status is explicitly recorded."""
        entry = make_result_entry(
            platform="youtube", deliverable_type="main_episode", status="published",
            end_screen_status="unsupported",
        )
        meta = entry["metadata_applied"]
        assert meta["end_screen_status"] == "unsupported"
        assert meta["end_screen_applied"] is False

    def test_end_screen_status_not_attempted_derives_false_not_null(self):
        entry = make_result_entry(
            platform="youtube", deliverable_type="main_episode", status="published",
            end_screen_status="not_attempted",
        )
        meta = entry["metadata_applied"]
        assert meta["end_screen_status"] == "not_attempted"
        assert meta["end_screen_applied"] is False

    def test_end_screen_status_failed_derives_false_not_null(self):
        entry = make_result_entry(
            platform="youtube", deliverable_type="main_episode", status="published",
            end_screen_status="failed",
        )
        meta = entry["metadata_applied"]
        assert meta["end_screen_status"] == "failed"
        assert meta["end_screen_applied"] is False

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

    def test_visibility_defaults_to_none(self):
        """Existing production callers never pass visibility -- must not break."""
        entry = make_result_entry(platform="youtube", deliverable_type="main_episode", status="published")
        assert entry["visibility"] is None

    def test_facebook_unpublished_representation(self):
        entry = make_result_entry(
            platform="facebook",
            deliverable_type="main_episode",
            status="published",
            platform_video_id="fb_vid_dark",
            visibility="unpublished",
        )
        assert entry["status"] == "published"
        assert entry["visibility"] == "unpublished"
        assert entry["platform_video_id"] == "fb_vid_dark"

    def test_tiktok_self_only_representation(self):
        entry = make_result_entry(
            platform="tiktok",
            deliverable_type="short",
            status="published",
            short_id="short_001",
            platform_video_id="tiktok_post_123",
            visibility="self_only",
        )
        assert entry["status"] == "published"
        assert entry["visibility"] == "self_only"

    def test_youtube_private_representation(self):
        entry = make_result_entry(
            platform="youtube",
            deliverable_type="main_episode",
            status="published",
            platform_video_id="yt_vid_priv",
            visibility="private",
        )
        assert entry["status"] == "published"
        assert entry["visibility"] == "private"

    def test_instagram_container_validated_representation(self):
        entry = make_result_entry(
            platform="instagram",
            deliverable_type="short",
            status="skipped",
            short_id="short_001",
            skipped_reason="instagram_container_validated: creation_id=abc, terminal_status=FINISHED",
            visibility="container_only",
        )
        assert entry["status"] == "skipped"
        assert entry["visibility"] == "container_only"
        assert entry["platform_video_id"] is None
        assert "container_validated" in entry["skipped_reason"]

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
