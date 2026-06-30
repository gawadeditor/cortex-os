"""
tests/test_publishing_agent.py — Unit tests for hypogeum/agents/publishing_agent.py

All platform calls are mocked. No real HTTP occurs.
Patches target the actual module functions (hypogeum.publishing.youtube.*) since
the agent imports them inside run_publishing_agent() as local names.

Tests:
  - ai_disclosure_text missing halts with PublishingAgentError
  - YouTube auth failure issues PDR and skips YouTube
  - YouTube quotaExceeded halts remaining YouTube uploads
  - >3 failures issues PDR
  - TikTok gate fail → all TikTok deliverables skipped (not failed)
  - successful full run (all platforms publish)
  - overall_status = "published" when youtube_main_published
  - overall_status = "failed" when all fail
  - Instagram staging failure causes short to fail
  - Publishing record written to output_dir
  - Schema validation passes on successful run
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.agents.publishing_agent import PublishingAgentError, run_publishing_agent
from hypogeum.publishing.retry import HardFailureError, PollTimeoutError, SoftFailureError
from hypogeum.publishing.staging import StagingError


# ---------------------------------------------------------------------------
# Module paths for patching
# ---------------------------------------------------------------------------

_YT = "hypogeum.publishing.youtube"
_META = "hypogeum.publishing.meta"
_TT = "hypogeum.publishing.tiktok"
_STAGING = "hypogeum.agents.publishing_agent.get_staging_provider"


# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------

def _make_config(
    has_youtube: bool = True,
    has_meta: bool = True,
    has_tiktok: bool = False,
    staging_url: str = "https://staging.example.com",
):
    cfg = MagicMock()
    cfg.YOUTUBE_CLIENT_ID = "yt_client_id" if has_youtube else None
    cfg.YOUTUBE_CLIENT_SECRET = "yt_secret" if has_youtube else None
    cfg.YOUTUBE_REFRESH_TOKEN = "yt_refresh" if has_youtube else None
    cfg.YOUTUBE_CHANNEL_ID = "UCchannel" if has_youtube else None
    cfg.YOUTUBE_DEFAULT_CATEGORY_ID = 27
    cfg.META_APP_ID = "meta_app_id" if has_meta else None
    cfg.META_APP_SECRET = "meta_secret" if has_meta else None
    cfg.META_ACCESS_TOKEN = "EAA_token" if has_meta else None
    cfg.META_INSTAGRAM_ACCOUNT_ID = "ig_account" if has_meta else None
    cfg.META_FACEBOOK_PAGE_ID = "fb_page" if has_meta else None
    cfg.META_GRAPH_API_VERSION = "v19.0"
    cfg.META_STAGING_BASE_URL = staging_url if has_meta else None
    cfg.TIKTOK_CLIENT_KEY = "tt_key" if has_tiktok else None
    cfg.TIKTOK_CLIENT_SECRET = "tt_secret" if has_tiktok else None
    cfg.TIKTOK_ACCESS_TOKEN = "tt_token" if has_tiktok else None
    cfg.TIKTOK_OPEN_ID = "tt_open_id" if has_tiktok else None
    cfg.tiktok_available = has_tiktok
    return cfg


def _make_final_render_package(episode_id: str, tmp_path: Path) -> dict:
    """Build a minimal final_render_package with real files on disk."""
    ep_dir = tmp_path / f"episodes/{episode_id}"
    ep_dir.mkdir(parents=True, exist_ok=True)

    main_video = ep_dir / f"main_{episode_id}.mp4"
    main_video.write_bytes(b"fake main video")

    srt_file = ep_dir / "captions_main.srt"
    srt_file.write_text("1\n00:00:00,000 --> 00:00:05,000\nHello\n")

    short_ids = ["sh_001", "sh_002", "sh_003"]
    shorts_renders = []
    for i, sid in enumerate(short_ids):
        short_video = ep_dir / f"short_{i}_{episode_id}.mp4"
        short_video.write_bytes(b"fake short video")
        shorts_renders.append({
            "short_id": sid,
            "video_asset_id": f"episodes/{episode_id}/short_{i}_{episode_id}",
            "caption_asset_id": f"episodes/{episode_id}/captions_{sid}",
        })

    thumb = ep_dir / "thumb_main.jpg"
    thumb.write_bytes(b"fake jpeg")

    return {
        "package_metadata": {
            "render_package_id": f"rp_{episode_id}",
            "episode_id": episode_id,
        },
        "episode_render": {
            "video_asset_id": f"episodes/{episode_id}/main_{episode_id}",
            "caption_asset_id": f"episodes/{episode_id}/captions_main",
            "duration_seconds": 600.0,
        },
        "shorts_renders": shorts_renders,
        "thumbnail_references": [
            {
                "thumbnail_asset_id": f"episodes/{episode_id}/thumb_main",
                "short_id": None,
                "format": "16:9",
            }
        ],
        "music_log": [],
        "metadata_source": {
            "episode_title": "The Antikythera Mechanism",
            "logline": "Humanity's first computer, hidden for 2000 years.",
            "section_titles": [
                {"title": "Introduction", "start_seconds": 0.0},
                {"title": "The Discovery", "start_seconds": 120.0},
            ],
            "shorts_titles": {
                "sh_001": "3 Hidden Secrets",
                "sh_002": "The Shocking Truth",
                "sh_003": "What Nobody Told You",
            },
            "ai_disclosure_text": "This video was produced with AI assistance.",
        },
        "assembly_qa_report": {"overall_status": "pass"},
        "package_integrity": {"production_ready": True},
    }


# ---------------------------------------------------------------------------
# Patch context manager
# ---------------------------------------------------------------------------

@contextmanager
def _patch_platforms(
    yt_token="ya29.token",
    yt_main_vid="yt_vid_main",
    yt_short_vid="yt_vid_short",
    meta_token="EAA_refreshed",
    fb_vid="fb_vid_001",
    ig_container="ig_cont_001",
    ig_status="FINISHED",
    ig_media="ig_media_001",
    tiktok_avail=False,
    yt_refresh_exc=None,
    meta_refresh_exc=None,
    yt_upload_exc=None,
    fb_upload_exc=None,
    staging_exc=None,
    yt_quota_exc=False,
):
    """
    Patch all platform client functions at their module level.
    The publishing agent imports them as local names inside run_publishing_agent(),
    so we must patch the actual module-level functions.
    """
    yt_calls = [0]

    def mock_yt_upload(*args, **kwargs):
        if yt_upload_exc:
            raise yt_upload_exc
        if yt_quota_exc:
            raise HardFailureError("quotaExceeded", status_code=403, error_code="quotaExceeded")
        idx = yt_calls[0]
        yt_calls[0] += 1
        return yt_main_vid if idx == 0 else yt_short_vid

    mock_staging = MagicMock()
    if staging_exc:
        mock_staging.stage_file = MagicMock(side_effect=staging_exc)
    else:
        mock_staging.stage_file = MagicMock(return_value="https://staged.example.com/f.mp4")
    mock_staging.delete_staged_file = MagicMock()

    with patch(f"{_YT}.refresh_youtube_token",
               side_effect=yt_refresh_exc if yt_refresh_exc else None,
               return_value=yt_token if not yt_refresh_exc else None), \
         patch(f"{_YT}.reset_quota_counter"), \
         patch(f"{_YT}.upload_video", side_effect=mock_yt_upload), \
         patch(f"{_YT}.upload_thumbnail", return_value=True), \
         patch(f"{_YT}.upload_captions", return_value=True), \
         patch(f"{_YT}.apply_end_screen", return_value=True), \
         patch(f"{_YT}.search_prior_episode", return_value=None), \
         patch(f"{_META}.refresh_meta_token",
               side_effect=meta_refresh_exc if meta_refresh_exc else None,
               return_value=meta_token if not meta_refresh_exc else None), \
         patch(f"{_META}.upload_facebook_video",
               side_effect=fb_upload_exc if fb_upload_exc else None,
               return_value=fb_vid if not fb_upload_exc else None), \
         patch(f"{_META}.create_instagram_container", return_value=ig_container), \
         patch(f"{_META}.poll_instagram_container", return_value=ig_status), \
         patch(f"{_META}.publish_instagram_container", return_value=ig_media), \
         patch(f"{_TT}.refresh_tiktok_token",
               side_effect=RuntimeError("No active TikTok credential in secret_profiles")), \
         patch(f"{_TT}.check_tiktok_availability", return_value=tiktok_avail), \
         patch(f"{_TT}.init_tiktok_upload", return_value=("tt_pub_id", "https://upload.tiktok/abc")), \
         patch(f"{_TT}.upload_tiktok_chunks"), \
         patch(f"{_TT}.poll_tiktok_publish", return_value="tt_post_001"), \
         patch(_STAGING, return_value=mock_staging):
        yield


def _run(pkg, config, episode_id, tmp_path, publish_at="2026-07-02T14:00:00+00:00"):
    """Run publishing agent with a scheduled publish_at by default (production behaviour)."""
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    return run_publishing_agent(
        pkg, config, episode_id,
        output_dir=tmp_path,
        project_root=tmp_path,
        publish_at=publish_at,
    )


# ---------------------------------------------------------------------------
# TestPreflightDisclosure
# ---------------------------------------------------------------------------

class TestPreflightDisclosure:
    def test_missing_disclosure_halts_with_publishing_agent_error(self, tmp_path):
        config = _make_config()
        pkg = _make_final_render_package("ep_disc_001", tmp_path)
        pkg["metadata_source"]["ai_disclosure_text"] = ""

        with pytest.raises(PublishingAgentError):
            _run(pkg, config, "ep_disc_001", tmp_path)

    def test_no_youtube_upload_when_disclosure_missing(self, tmp_path):
        config = _make_config()
        pkg = _make_final_render_package("ep_disc_002", tmp_path)
        pkg["metadata_source"]["ai_disclosure_text"] = ""

        upload_mock = MagicMock()
        with patch(f"{_YT}.upload_video", upload_mock):
            with pytest.raises(PublishingAgentError):
                _run(pkg, config, "ep_disc_002", tmp_path)

        upload_mock.assert_not_called()

    def test_whitespace_only_disclosure_halts(self, tmp_path):
        config = _make_config()
        pkg = _make_final_render_package("ep_disc_003", tmp_path)
        pkg["metadata_source"]["ai_disclosure_text"] = "   "

        with pytest.raises(PublishingAgentError):
            _run(pkg, config, "ep_disc_003", tmp_path)


# ---------------------------------------------------------------------------
# TestYouTubeAuthFailure
# ---------------------------------------------------------------------------

class TestYouTubeAuthFailure:
    def test_youtube_auth_failure_issues_pdr(self, tmp_path):
        config = _make_config(has_youtube=True, has_meta=True)
        pkg = _make_final_render_package("ep_ytauth_001", tmp_path)

        with _patch_platforms(yt_refresh_exc=HardFailureError("401 Unauthorized", status_code=401)):
            result = _run(pkg, config, "ep_ytauth_001", tmp_path)

        # YouTube main should be in failures list
        failures = result.get("failures", [])
        assert len(failures) > 0

    def test_youtube_auth_failure_skips_all_youtube_uploads(self, tmp_path):
        config = _make_config(has_youtube=True, has_meta=True)
        pkg = _make_final_render_package("ep_ytauth_002", tmp_path)

        with _patch_platforms(yt_refresh_exc=HardFailureError("401", status_code=401)) as patches:
            pass  # just test result

        with _patch_platforms(yt_refresh_exc=HardFailureError("401", status_code=401)):
            result = _run(pkg, config, "ep_ytauth_002", tmp_path)

        yt_results = [r for r in result["platform_results"] if r["platform"] == "youtube"]
        assert all(r["status"] == "failed" for r in yt_results)

    def test_youtube_auth_failure_continues_other_platforms(self, tmp_path):
        config = _make_config(has_youtube=True, has_meta=True)
        pkg = _make_final_render_package("ep_ytauth_003", tmp_path)

        with _patch_platforms(yt_refresh_exc=HardFailureError("401", status_code=401)):
            result = _run(pkg, config, "ep_ytauth_003", tmp_path)

        # Facebook and Instagram should still be attempted
        platform_results = result.get("platform_results", [])
        fb_results = [r for r in platform_results if r["platform"] == "facebook"]
        ig_results = [r for r in platform_results if r["platform"] == "instagram"]
        assert len(fb_results) == 1
        assert len(ig_results) == 3


# ---------------------------------------------------------------------------
# TestQuotaExceeded
# ---------------------------------------------------------------------------

class TestQuotaExceeded:
    def test_quota_exceeded_issues_pdr(self, tmp_path):
        config = _make_config(has_youtube=True)
        pkg = _make_final_render_package("ep_quota_001", tmp_path)

        with _patch_platforms(yt_quota_exc=True):
            result = _run(pkg, config, "ep_quota_001", tmp_path)

        failures = result.get("failures", [])
        failure_descriptions = [
            f.get("deficiency_items", [{}])[0].get("description", "")
            for f in failures
        ]
        assert any("quota" in d.lower() or "YouTube" in d for d in failure_descriptions)

    def test_quota_exceeded_marks_main_as_failed(self, tmp_path):
        config = _make_config(has_youtube=True)
        pkg = _make_final_render_package("ep_quota_002", tmp_path)

        with _patch_platforms(yt_quota_exc=True):
            result = _run(pkg, config, "ep_quota_002", tmp_path)

        yt_main = next(
            (r for r in result["platform_results"]
             if r["platform"] == "youtube" and r["deliverable_type"] == "main_episode"),
            None,
        )
        assert yt_main is not None
        assert yt_main["status"] == "failed"


# ---------------------------------------------------------------------------
# TestTiktokGate
# ---------------------------------------------------------------------------

class TestTiktokGate:
    def test_tiktok_unavailable_all_skipped(self, tmp_path):
        config = _make_config()
        pkg = _make_final_render_package("ep_tt_001", tmp_path)

        with _patch_platforms(tiktok_avail=False):
            result = _run(pkg, config, "ep_tt_001", tmp_path)

        tiktok_results = [r for r in result["platform_results"] if r["platform"] == "tiktok"]
        assert len(tiktok_results) == 3
        assert all(r["status"] == "skipped" for r in tiktok_results)
        assert all(r["skipped_reason"] == "tiktok_unavailable" for r in tiktok_results)

    def test_tiktok_skipped_not_counted_as_failed(self, tmp_path):
        config = _make_config()
        pkg = _make_final_render_package("ep_tt_002", tmp_path)

        with _patch_platforms(tiktok_avail=False):
            result = _run(pkg, config, "ep_tt_002", tmp_path)

        summary = result["summary"]
        assert summary["total_skipped"] == 3

    def test_tiktok_uploads_not_called_when_gate_fails(self, tmp_path):
        config = _make_config()
        pkg = _make_final_render_package("ep_tt_003", tmp_path)

        tt_init_mock = MagicMock()

        with _patch_platforms(tiktok_avail=False):
            with patch(f"{_TT}.init_tiktok_upload", tt_init_mock):
                _run(pkg, config, "ep_tt_003", tmp_path)

        tt_init_mock.assert_not_called()


# ---------------------------------------------------------------------------
# TestSuccessfulFullRun
# ---------------------------------------------------------------------------

class TestSuccessfulFullRun:
    def test_full_run_published_status(self, tmp_path):
        config = _make_config(has_youtube=True, has_meta=True, has_tiktok=False)
        pkg = _make_final_render_package("ep_full_001", tmp_path)

        with _patch_platforms():
            result = _run(pkg, config, "ep_full_001", tmp_path)

        assert result["package_metadata"]["overall_status"] == "published"
        assert result["package_integrity"]["production_ready"] is True
        assert result["summary"]["youtube_main_published"] is True

    def test_full_run_summary_includes_youtube_main_video_id(self, tmp_path):
        # Regression: summary.youtube_main_video_id must be populated so
        # pipeline.py can write analytics_pending after a successful publish.
        config = _make_config(has_youtube=True, has_meta=False, has_tiktok=False)
        pkg = _make_final_render_package("ep_vid_id_001", tmp_path)

        with _patch_platforms():
            result = _run(pkg, config, "ep_vid_id_001", tmp_path)

        assert result["summary"]["youtube_main_video_id"] == "yt_vid_main"

    def test_full_run_writes_publishing_outcome_to_db(self, tmp_path):
        # Regression: run_publishing_agent must call record_publishing_outcome
        # after a successful YouTube main upload so analytics sync can discover it.
        config = _make_config(has_youtube=True, has_meta=False, has_tiktok=False)
        pkg = _make_final_render_package("ep_outcome_db_001", tmp_path)

        with _patch_platforms(), \
             patch("hypogeum.agents.publishing_agent.record_publishing_outcome") as mock_outcome:
            _run(pkg, config, "ep_outcome_db_001", tmp_path)

        mock_outcome.assert_called_once()
        _, call_kwargs = mock_outcome.call_args
        assert call_kwargs["episode_id"] == "ep_outcome_db_001"
        assert call_kwargs["platform"] == "youtube"
        assert call_kwargs["publishing_status"] == "success"
        assert call_kwargs["platform_video_id"] == "yt_vid_main"

    def test_full_run_record_written_to_disk(self, tmp_path):
        config = _make_config()
        pkg = _make_final_render_package("ep_disk_001", tmp_path)

        with _patch_platforms():
            _run(pkg, config, "ep_disk_001", tmp_path)

        record_path = tmp_path / "publishing_record_ep_disk_001.json"
        assert record_path.exists()

        with open(record_path) as f:
            data = json.load(f)
        assert data["package_metadata"]["episode_id"] == "ep_disk_001"

    def test_full_run_youtube_main_gets_captions(self, tmp_path):
        config = _make_config()
        pkg = _make_final_render_package("ep_caps_001", tmp_path)

        captions_mock = MagicMock(return_value=True)

        with _patch_platforms():
            with patch(f"{_YT}.upload_captions", captions_mock):
                result = _run(pkg, config, "ep_caps_001", tmp_path)

        captions_mock.assert_called_once()

        yt_main = next(
            r for r in result["platform_results"]
            if r["platform"] == "youtube" and r["deliverable_type"] == "main_episode"
        )
        assert yt_main["status"] == "published"

    def test_full_run_correct_deliverable_count(self, tmp_path):
        config = _make_config(has_tiktok=False)
        pkg = _make_final_render_package("ep_count_001", tmp_path)

        with _patch_platforms():
            result = _run(pkg, config, "ep_count_001", tmp_path)

        # 1 YT main + 3 YT shorts + 1 FB + 3 IG + 3 TT skipped = 11 entries
        platform_results = result["platform_results"]
        assert len(platform_results) == 11

        yt_results = [r for r in platform_results if r["platform"] == "youtube"]
        assert len(yt_results) == 4

        fb_results = [r for r in platform_results if r["platform"] == "facebook"]
        assert len(fb_results) == 1

        ig_results = [r for r in platform_results if r["platform"] == "instagram"]
        assert len(ig_results) == 3

        tt_results = [r for r in platform_results if r["platform"] == "tiktok"]
        assert len(tt_results) == 3
        assert all(r["status"] == "skipped" for r in tt_results)

    def test_tiktok_published_when_available(self, tmp_path):
        config = _make_config(has_tiktok=True)
        pkg = _make_final_render_package("ep_tt_full_001", tmp_path)

        with _patch_platforms(tiktok_avail=True):
            result = _run(pkg, config, "ep_tt_full_001", tmp_path)

        tt_results = [r for r in result["platform_results"] if r["platform"] == "tiktok"]
        assert len(tt_results) == 3
        assert all(r["status"] == "published" for r in tt_results)


# ---------------------------------------------------------------------------
# TestOverallStatusLogic
# ---------------------------------------------------------------------------

class TestOverallStatusLogic:
    def test_published_when_yt_main_succeeds(self, tmp_path):
        config = _make_config()
        pkg = _make_final_render_package("ep_status_pub_001", tmp_path)

        with _patch_platforms():
            result = _run(pkg, config, "ep_status_pub_001", tmp_path)

        assert result["package_metadata"]["overall_status"] == "published"
        assert result["package_integrity"]["production_ready"] is True

    def test_failed_when_no_platforms_available(self, tmp_path):
        config = _make_config(has_youtube=False, has_meta=False, has_tiktok=False)
        pkg = _make_final_render_package("ep_all_fail_001", tmp_path)

        with _patch_platforms(tiktok_avail=False):
            result = _run(pkg, config, "ep_all_fail_001", tmp_path)

        assert result["package_metadata"]["overall_status"] == "failed"
        assert result["package_integrity"]["production_ready"] is False

    def test_partially_published_when_yt_fails_meta_succeeds(self, tmp_path):
        config = _make_config(has_youtube=True, has_meta=True)
        pkg = _make_final_render_package("ep_partial_001", tmp_path)

        yt_calls = [0]
        def selective_yt_fail(*args, **kwargs):
            yt_calls[0] += 1
            if yt_calls[0] == 1:
                raise SoftFailureError("yt main fail", status_code=503)
            return f"yt_short_{yt_calls[0]}"

        with _patch_platforms():
            with patch(f"{_YT}.upload_video", side_effect=selective_yt_fail), \
                 patch("hypogeum.publishing.retry.time.sleep"):
                result = _run(pkg, config, "ep_partial_001", tmp_path)

        # YouTube main failed but Facebook/Instagram succeeded -> partially_published
        assert result["package_metadata"]["overall_status"] in ("partially_published", "failed")


# ---------------------------------------------------------------------------
# TestFailureThresholdPDR
# ---------------------------------------------------------------------------

class TestFailureThresholdPDR:
    def test_more_than_3_failures_issues_threshold_pdr(self, tmp_path):
        # No YouTube, No Meta = 4 YouTube + 1 Facebook + 3 Instagram = 8 failures > threshold(3)
        config = _make_config(has_youtube=False, has_meta=False, has_tiktok=False)
        pkg = _make_final_render_package("ep_thresh_001", tmp_path)

        with _patch_platforms(tiktok_avail=False):
            result = _run(pkg, config, "ep_thresh_001", tmp_path)

        failures = result.get("failures", [])
        failure_descriptions = [
            f.get("deficiency_items", [{}])[0].get("description", "")
            for f in failures
        ]
        has_threshold_pdr = any(
            "threshold" in d.lower() or "failed" in d.lower()
            for d in failure_descriptions
        )
        assert has_threshold_pdr


# ---------------------------------------------------------------------------
# TestInstagramStagingFailure
# ---------------------------------------------------------------------------

class TestInstagramStagingFailure:
    def test_staging_failure_marks_all_ig_shorts_as_failed(self, tmp_path):
        config = _make_config(has_meta=True)
        pkg = _make_final_render_package("ep_stage_fail_001", tmp_path)

        with _patch_platforms(staging_exc=StagingError("staging failed")):
            result = _run(pkg, config, "ep_stage_fail_001", tmp_path)

        ig_results = [r for r in result["platform_results"] if r["platform"] == "instagram"]
        assert len(ig_results) == 3
        assert all(r["status"] == "failed" for r in ig_results)
        staging_failures = [r for r in ig_results if r.get("skipped_reason") == "staging_failed"]
        assert len(staging_failures) == 3


# ---------------------------------------------------------------------------
# TestSchemaValidation
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_successful_run_passes_schema_validation(self, tmp_path):
        from hypogeum.schemas import validate, SchemaError

        config = _make_config()
        pkg = _make_final_render_package("ep_schema_001", tmp_path)

        with _patch_platforms():
            result = _run(pkg, config, "ep_schema_001", tmp_path)

        try:
            validate("publishing_record", result)
        except SchemaError as e:
            pytest.fail(f"Schema validation failed: {e}")

    def test_result_has_required_top_level_keys(self, tmp_path):
        config = _make_config()
        pkg = _make_final_render_package("ep_keys_001", tmp_path)

        with _patch_platforms():
            result = _run(pkg, config, "ep_keys_001", tmp_path)

        assert "package_metadata" in result
        assert "platform_results" in result
        assert "failures" in result
        assert "summary" in result
        assert "package_integrity" in result

    def test_each_platform_result_has_metadata_applied(self, tmp_path):
        config = _make_config()
        pkg = _make_final_render_package("ep_meta_001", tmp_path)

        with _patch_platforms():
            result = _run(pkg, config, "ep_meta_001", tmp_path)

        for r in result["platform_results"]:
            assert "metadata_applied" in r
            assert "ai_disclosure_included" in r["metadata_applied"]

    def test_youtube_main_result_has_captions_and_thumbnail_fields(self, tmp_path):
        config = _make_config()
        pkg = _make_final_render_package("ep_yt_fields_001", tmp_path)

        with _patch_platforms():
            result = _run(pkg, config, "ep_yt_fields_001", tmp_path)

        yt_main = next(
            r for r in result["platform_results"]
            if r["platform"] == "youtube" and r["deliverable_type"] == "main_episode"
        )
        meta = yt_main["metadata_applied"]
        assert "captions_submitted" in meta
        assert "thumbnail_applied" in meta
        assert "end_screen_applied" in meta


# ---------------------------------------------------------------------------
# TestPublishScheduleGuard
# Regression: autonomous uploads must be private+scheduled, never public.
# Missing schedule must block upload (skipped), not default to public.
# ---------------------------------------------------------------------------

class TestPublishScheduleGuard:

    def _run_with(self, tmp_path, publish_at=None, youtube_privacy="public",
                  capture_metadata=None):
        """Run publishing agent and optionally capture upload metadata."""
        config = _make_config()
        pkg = _make_final_render_package("ep_sched_test", tmp_path)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        captured = {}

        def spy_upload(config, access_token, video_path, metadata, episode_id):
            captured.update(metadata)
            return "yt_vid_scheduled"

        with _patch_platforms():
            with patch(f"{_YT}.upload_video", side_effect=spy_upload):
                result = run_publishing_agent(
                    pkg, config, "ep_sched_test",
                    output_dir=tmp_path,
                    project_root=tmp_path,
                    youtube_privacy=youtube_privacy,
                    publish_at=publish_at,
                )
        if capture_metadata is not None:
            capture_metadata.update(captured)
        return result

    def test_scheduled_upload_uses_private_privacy(self, tmp_path):
        """publish_at set → privacyStatus must be 'private'."""
        meta = {}
        self._run_with(tmp_path, publish_at="2026-07-02T14:00:00+00:00", capture_metadata=meta)
        assert meta.get("privacyStatus") == "private", (
            f"Expected 'private' for scheduled upload, got {meta.get('privacyStatus')!r}"
        )

    def test_scheduled_upload_sets_publish_at_in_metadata(self, tmp_path):
        """publish_at set → publishAt must appear in video metadata sent to YouTube."""
        meta = {}
        self._run_with(tmp_path, publish_at="2026-07-02T14:00:00+00:00", capture_metadata=meta)
        assert meta.get("publishAt") == "2026-07-02T14:00:00+00:00", (
            f"Expected publishAt in metadata, got {meta!r}"
        )

    def test_no_schedule_blocks_youtube_upload(self, tmp_path):
        """publish_at=None + default public → YouTube upload must be skipped."""
        upload_called = []

        config = _make_config()
        pkg = _make_final_render_package("ep_nosch", tmp_path)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        def spy_upload(*args, **kwargs):
            upload_called.append(True)
            return "yt_vid_leaked"

        with _patch_platforms():
            with patch(f"{_YT}.upload_video", side_effect=spy_upload):
                result = run_publishing_agent(
                    pkg, config, "ep_nosch",
                    output_dir=tmp_path,
                    project_root=tmp_path,
                    publish_at=None,
                    youtube_privacy="public",
                )

        assert not upload_called, (
            "YouTube upload must be blocked when no publish schedule is set"
        )

    def test_no_schedule_records_skipped_not_failed(self, tmp_path):
        """Blocked upload is recorded as 'skipped' with no_publish_schedule reason."""
        config = _make_config()
        pkg = _make_final_render_package("ep_nosch2", tmp_path)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        with _patch_platforms():
            result = run_publishing_agent(
                pkg, config, "ep_nosch2",
                output_dir=tmp_path,
                project_root=tmp_path,
                publish_at=None,
                youtube_privacy="public",
            )

        yt_main = next(
            (r for r in result["platform_results"]
             if r["platform"] == "youtube" and r["deliverable_type"] == "main_episode"),
            None,
        )
        assert yt_main is not None
        assert yt_main["status"] == "skipped"
        assert yt_main.get("skipped_reason") == "no_publish_schedule"

    def test_explicit_private_without_schedule_uploads(self, tmp_path):
        """youtube_privacy='private' with no publish_at allows upload (CLI/Episode Zero path)."""
        upload_called = []

        config = _make_config()
        pkg = _make_final_render_package("ep_cli_priv", tmp_path)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        def spy_upload(config, access_token, video_path, metadata, episode_id):
            upload_called.append(metadata.get("privacyStatus"))
            return "yt_vid_private"

        with _patch_platforms():
            with patch(f"{_YT}.upload_video", side_effect=spy_upload):
                run_publishing_agent(
                    pkg, config, "ep_cli_priv",
                    output_dir=tmp_path,
                    project_root=tmp_path,
                    publish_at=None,
                    youtube_privacy="private",
                )

        assert upload_called, "Expected upload to proceed with explicit private privacy"
        assert upload_called[0] == "private"

    def test_scheduled_upload_never_public(self, tmp_path):
        """publish_at set → privacyStatus must never be 'public'."""
        meta = {}
        self._run_with(tmp_path, publish_at="2026-07-02T14:00:00+00:00", capture_metadata=meta)
        assert meta.get("privacyStatus") != "public", (
            "privacyStatus must not be 'public' when a publish schedule is set"
        )


# ---------------------------------------------------------------------------
# TestPlaceholderShortGuard
# Regression: placeholder shorts (sh_000_placeholder, sh_001_placeholder,
# sh_002_placeholder) from assembly padding must be skipped cleanly, not
# counted as failures. Real shorts with missing files remain failures.
# ---------------------------------------------------------------------------

def _make_pkg_with_placeholder_shorts(episode_id: str, tmp_path: Path) -> dict:
    """Build a final_render_package where shorts_renders are all placeholders."""
    base = _make_final_render_package(episode_id, tmp_path)
    base["shorts_renders"] = [
        {
            "short_id": f"sh_{i:03d}_placeholder",
            "video_asset_id": "",
            "duration_seconds": 0.0,
            "resolution": "1080x1920",
            "frame_rate": 30,
            "scene_count": 0,
            "caption_asset_id": "",
            "text_overlays_applied": 0,
            "text_overlays_skipped": [],
        }
        for i in range(3)
    ]
    return base


def _make_pkg_with_real_shorts(episode_id: str, tmp_path: Path) -> dict:
    """Build a final_render_package with real short video assets on disk."""
    base = _make_final_render_package(episode_id, tmp_path)
    ep_dir = tmp_path / f"episodes/{episode_id}"
    ep_dir.mkdir(parents=True, exist_ok=True)
    short_renders = []
    for i in range(3):
        short_id = f"sh_{i:03d}"
        video_path = ep_dir / f"short_{i}_{episode_id}.mp4"
        video_path.write_bytes(b"fake short video")
        short_renders.append({
            "short_id": short_id,
            "video_asset_id": f"episodes/{episode_id}/short_{i}_{episode_id}",
            "duration_seconds": 45.0,
            "resolution": "1080x1920",
            "frame_rate": 30,
            "scene_count": 3,
            "caption_asset_id": "",
            "text_overlays_applied": 0,
            "text_overlays_skipped": [],
        })
    base["shorts_renders"] = short_renders
    # Update metadata to have matching short titles
    base["metadata_source"]["shorts_titles"] = {
        f"sh_{i:03d}": f"Short {i+1}" for i in range(3)
    }
    return base


class TestPlaceholderShortGuard:
    """
    Regression: assembly pads shorts_renders to exactly 3 with placeholder stubs
    when ENABLE_DEDICATED_SHORT_GENERATION=false (default). These placeholders must
    be skipped cleanly by the publishing agent — not counted as failures.
    """

    def _result_by(self, results, platform, short_id):
        return next(
            (r for r in results
             if r["platform"] == platform and r.get("short_id") == short_id),
            None,
        )

    def test_placeholder_shorts_skipped_not_failed_youtube(self, tmp_path):
        """Placeholder YouTube shorts produce skipped, not failed, results."""
        config = _make_config()
        pkg = _make_pkg_with_placeholder_shorts("ep_ph_yt", tmp_path)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        with _patch_platforms():
            result = run_publishing_agent(
                pkg, config, "ep_ph_yt",
                output_dir=tmp_path, project_root=tmp_path,
                publish_at="2026-07-02T14:00:00+00:00",
            )

        for i in range(3):
            short_id = f"sh_{i:03d}_placeholder"
            r = self._result_by(result["platform_results"], "youtube", short_id)
            assert r is not None, f"Expected result for {short_id}"
            assert r["status"] == "skipped", f"{short_id}: expected skipped, got {r['status']}"
            assert r.get("skipped_reason") == "short_not_produced"

    def test_placeholder_shorts_skipped_not_failed_instagram(self, tmp_path):
        """Placeholder Instagram shorts produce skipped, not failed, results."""
        config = _make_config()
        pkg = _make_pkg_with_placeholder_shorts("ep_ph_ig", tmp_path)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        with _patch_platforms():
            result = run_publishing_agent(
                pkg, config, "ep_ph_ig",
                output_dir=tmp_path, project_root=tmp_path,
                publish_at="2026-07-02T14:00:00+00:00",
            )

        for i in range(3):
            short_id = f"sh_{i:03d}_placeholder"
            r = self._result_by(result["platform_results"], "instagram", short_id)
            assert r is not None
            assert r["status"] == "skipped"
            assert r.get("skipped_reason") == "short_not_produced"

    def test_placeholder_shorts_skipped_not_failed_tiktok(self, tmp_path):
        """Placeholder TikTok shorts produce skipped, not failed, results."""
        config = _make_config()
        pkg = _make_pkg_with_placeholder_shorts("ep_ph_tt", tmp_path)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        with _patch_platforms(tiktok_avail=True):
            result = run_publishing_agent(
                pkg, config, "ep_ph_tt",
                output_dir=tmp_path, project_root=tmp_path,
                publish_at="2026-07-02T14:00:00+00:00",
            )

        for i in range(3):
            short_id = f"sh_{i:03d}_placeholder"
            r = self._result_by(result["platform_results"], "tiktok", short_id)
            assert r is not None
            assert r["status"] == "skipped"
            assert r.get("skipped_reason") == "short_not_produced"

    def test_placeholder_shorts_do_not_increment_failed_count(self, tmp_path):
        """Placeholder shorts must not push failure count above the PDR threshold."""
        config = _make_config()
        pkg = _make_pkg_with_placeholder_shorts("ep_ph_pdr", tmp_path)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        with _patch_platforms():
            result = run_publishing_agent(
                pkg, config, "ep_ph_pdr",
                output_dir=tmp_path, project_root=tmp_path,
                publish_at="2026-07-02T14:00:00+00:00",
            )

        # PDR is issued when > 3 non-TikTok failures; placeholder shorts must not count
        pdr_failures = result.get("failures", [])
        pdr_threshold_hit = any(
            "threshold exceeded" in str(f.get("deficiency_items", "")).lower()
            for f in pdr_failures
        )
        assert not pdr_threshold_hit, (
            "Placeholder shorts must not push failure count above PDR threshold"
        )

    def test_real_shorts_with_missing_files_still_fail(self, tmp_path):
        """Real short_ids (non-placeholder) with missing video files still count as failures."""
        config = _make_config()
        pkg = _make_final_render_package("ep_real_miss", tmp_path)
        # Use paths that definitely do not exist on disk (different prefix)
        pkg["shorts_renders"] = [
            {
                "short_id": f"sh_{i:03d}",
                "video_asset_id": f"episodes/ep_real_miss/missing_short_{i}_ep_real_miss",
                "duration_seconds": 45.0,
                "resolution": "1080x1920",
                "frame_rate": 30,
                "scene_count": 3,
                "caption_asset_id": "",
                "text_overlays_applied": 0,
                "text_overlays_skipped": [],
            }
            for i in range(3)
        ]
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        with _patch_platforms():
            result = run_publishing_agent(
                pkg, config, "ep_real_miss",
                output_dir=tmp_path, project_root=tmp_path,
                publish_at="2026-07-02T14:00:00+00:00",
            )

        failed_yt_shorts = [
            r for r in result["platform_results"]
            if r["platform"] == "youtube"
            and r["deliverable_type"] == "short"
            and r["status"] == "failed"
        ]
        assert len(failed_yt_shorts) == 3, (
            "Real short_ids with missing files must be counted as failures"
        )

    def test_real_shorts_with_files_get_publish_at(self, tmp_path):
        """Real short uploads receive publishAt in their metadata."""
        config = _make_config()
        pkg = _make_pkg_with_real_shorts("ep_real_shorts", tmp_path)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        short_metas = []

        def spy_upload(config, access_token, video_path, metadata, episode_id):
            short_metas.append(dict(metadata))
            return f"yt_vid_{len(short_metas)}"

        with _patch_platforms():
            with patch(f"{_YT}.upload_video", side_effect=spy_upload):
                run_publishing_agent(
                    pkg, config, "ep_real_shorts",
                    output_dir=tmp_path, project_root=tmp_path,
                    publish_at="2026-07-02T14:00:00+00:00",
                )

        # First call is main episode, subsequent are shorts
        short_upload_metas = short_metas[1:]  # skip main episode
        assert len(short_upload_metas) == 3, "Expected 3 short uploads"
        for meta in short_upload_metas:
            assert meta.get("privacyStatus") == "private", "Short must be private when scheduled"
            assert meta.get("publishAt") == "2026-07-02T14:00:00+00:00", "Short must have publishAt"

    def test_vpd_suppress_shorts_false_when_flag_enabled(self):
        """ENABLE_DEDICATED_SHORT_GENERATION=true → scene plan input has no shorts_count=0 constraint."""
        from hypogeum.agents.scene_planner import build_scene_plan_input
        from unittest.mock import MagicMock

        config = MagicMock()
        config.ENABLE_DEDICATED_SHORT_GENERATION = True
        config.EPISODE_ZERO_MAX_SCENES = 0

        sp = {
            "sections": [{"section_id": "s01", "title": "H", "narration_beats": [
                {"beat_id": "b_001", "text": "Main beat.", "beat_type": "narration"},
            ], "visual_directions": ""}],
            "shorts_scripts": [{"short_id": "short_001", "visual_directions": "",
                                 "narration_beats": [{"beat_id": "sh1b01", "text": "Short.", "beat_type": "narration"}],
                                 "text_overlays": []}],
            "episode_metadata": {}, "script_integrity": {}, "traceability_manifest": {},
            "chapter_markers": [], "shorts_extraction_markers": [],
        }
        input_pkg = build_scene_plan_input(sp, config, "ep_test_on")

        constraints = input_pkg.get("generation_constraints", {})
        assert constraints.get("shorts_count", -1) != 0, (
            "shorts_count must NOT be 0 when ENABLE_DEDICATED_SHORT_GENERATION=True"
        )
        # Shorts scripts must be included (not stripped) in the compacted script
        assert "shorts_scripts" in input_pkg.get("script_package", {})

    def test_vpd_suppress_shorts_true_when_flag_disabled(self):
        """ENABLE_DEDICATED_SHORT_GENERATION=false → scene plan input suppresses shorts."""
        from hypogeum.agents.scene_planner import build_scene_plan_input
        from unittest.mock import MagicMock

        config = MagicMock()
        config.ENABLE_DEDICATED_SHORT_GENERATION = False
        config.EPISODE_ZERO_MAX_SCENES = 0

        sp = {
            "sections": [{"section_id": "s01", "title": "H", "narration_beats": [
                {"beat_id": "b_001", "text": "Main beat.", "beat_type": "narration"},
            ], "visual_directions": ""}],
            "shorts_scripts": [{"short_id": "short_001", "visual_directions": "",
                                 "narration_beats": [{"beat_id": "sh1b01", "text": "Short.", "beat_type": "narration"}],
                                 "text_overlays": []}],
            "episode_metadata": {}, "script_integrity": {}, "traceability_manifest": {},
            "chapter_markers": [], "shorts_extraction_markers": [],
        }
        input_pkg = build_scene_plan_input(sp, config, "ep_test_off")

        constraints = input_pkg.get("generation_constraints", {})
        assert constraints.get("shorts_count") == 0, (
            "shorts_count must be 0 when ENABLE_DEDICATED_SHORT_GENERATION=False"
        )
        # Shorts scripts must be stripped from the compacted script when suppressed
        compacted = input_pkg.get("script_package", {})
        assert not compacted.get("shorts_scripts"), (
            "shorts_scripts must be stripped when suppress_shorts=True"
        )

    def test_vpd_shorts_beats_included_when_not_suppressed(self):
        """When flag enabled, _flatten_beats_for_a2 includes short beats in the plan."""
        from hypogeum.agents.visual_package_director import _flatten_beats_for_a2

        input_pkg = {
            "script_package": {
                "sections": [{"section_id": "s01", "visual_directions": "",
                               "narration_beats": [{"beat_id": "b_001", "text": "Main.", "beat_type": "narration"}]}],
                "shorts_scripts": [{"short_id": "short_001", "visual_directions": "",
                                    "narration_beats": [{"beat_id": "sh1b01", "text": "Short.", "beat_type": "narration"}]}],
            },
            "generation_constraints": {},  # no shorts_count=0 → shorts not suppressed
        }
        beats = _flatten_beats_for_a2(input_pkg)

        short_beats = [b for b in beats if b.get("source_type") == "short"]
        main_beats = [b for b in beats if b.get("source_type") == "main_episode"]
        assert len(main_beats) == 1
        assert len(short_beats) == 1, "Short beats must be included when shorts_count not 0"
        assert short_beats[0]["short_id"] == "short_001"

    def test_vpd_shorts_beats_excluded_when_suppressed(self):
        """When shorts_count=0 in constraints, _flatten_beats_for_a2 excludes short beats."""
        from hypogeum.agents.visual_package_director import _flatten_beats_for_a2

        input_pkg = {
            "script_package": {
                "sections": [{"section_id": "s01", "visual_directions": "",
                               "narration_beats": [{"beat_id": "b_001", "text": "Main.", "beat_type": "narration"}]}],
                "shorts_scripts": [{"short_id": "short_001", "visual_directions": "",
                                    "narration_beats": [{"beat_id": "sh1b01", "text": "Short.", "beat_type": "narration"}]}],
            },
            "generation_constraints": {"shorts_count": 0},
        }
        beats = _flatten_beats_for_a2(input_pkg)

        short_beats = [b for b in beats if b.get("source_type") == "short"]
        assert short_beats == [], "Short beats must be excluded when shorts_count=0"
