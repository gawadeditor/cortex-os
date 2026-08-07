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
from contextlib import ExitStack, contextmanager
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
    cfg.YOUTUBE_DRY_RUN = False  # must be explicit False — MagicMock attributes are truthy
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
    tiktok_publish_mode="direct_post",
    tiktok_mode_exc=None,
    tiktok_inbox_publish_id="tt_inbox_pub_id",
    tiktok_inbox_status="SEND_TO_USER_INBOX",
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

    # ExitStack, not a single backslash-chained `with` — the platform patch
    # list has grown past CPython's static nested-block limit for one `with`
    # statement (hit when adding the TikTok Upload-to-Inbox patches).
    with ExitStack() as stack:
        stack.enter_context(patch(
            f"{_YT}.refresh_youtube_token",
            side_effect=yt_refresh_exc if yt_refresh_exc else None,
            return_value=yt_token if not yt_refresh_exc else None,
        ))
        stack.enter_context(patch(f"{_YT}.reset_quota_counter"))
        stack.enter_context(patch(f"{_YT}.upload_video", side_effect=mock_yt_upload))
        stack.enter_context(patch(f"{_YT}.upload_thumbnail", return_value=True))
        stack.enter_context(patch(f"{_YT}.upload_captions", return_value=True))
        stack.enter_context(patch(f"{_YT}.apply_end_screen", return_value="unsupported"))
        stack.enter_context(patch(
            f"{_YT}.search_best_end_screen_videos",
            return_value=["yt_prior_001", "yt_prior_002"],
        ))
        stack.enter_context(patch(
            f"{_META}.refresh_meta_token",
            side_effect=meta_refresh_exc if meta_refresh_exc else None,
            return_value=meta_token if not meta_refresh_exc else None,
        ))
        stack.enter_context(patch(
            f"{_META}.upload_facebook_video",
            side_effect=fb_upload_exc if fb_upload_exc else None,
            return_value=fb_vid if not fb_upload_exc else None,
        ))
        stack.enter_context(patch(f"{_META}.create_instagram_container", return_value=ig_container))
        stack.enter_context(patch(f"{_META}.poll_instagram_container", return_value=ig_status))
        stack.enter_context(patch(f"{_META}.publish_instagram_container", return_value=ig_media))
        stack.enter_context(patch(
            f"{_TT}.refresh_tiktok_token",
            side_effect=RuntimeError("No active TikTok credential in secret_profiles"),
        ))
        stack.enter_context(patch(f"{_TT}.check_tiktok_availability", return_value=tiktok_avail))
        stack.enter_context(patch(
            f"{_TT}.resolve_tiktok_publish_mode",
            side_effect=tiktok_mode_exc if tiktok_mode_exc else None,
            return_value=tiktok_publish_mode if not tiktok_mode_exc else None,
        ))
        stack.enter_context(patch(
            f"{_TT}.init_tiktok_upload",
            return_value=("tt_pub_id", "https://upload.tiktok/abc"),
        ))
        stack.enter_context(patch(
            f"{_TT}.init_tiktok_inbox_upload",
            return_value=(tiktok_inbox_publish_id, "https://upload.tiktok/inbox_abc"),
        ))
        stack.enter_context(patch(f"{_TT}.upload_tiktok_chunks"))
        stack.enter_context(patch(f"{_TT}.poll_tiktok_publish", return_value="tt_post_001"))
        stack.enter_context(patch(f"{_TT}.poll_tiktok_inbox_status", return_value=tiktok_inbox_status))
        stack.enter_context(patch(_STAGING, return_value=mock_staging))
        # Production Output Gate (2026-08-02): every test in this file exercises
        # publishing LOGIC (idempotency, multi-platform, scheduling, schema) against
        # in-memory final_render_package dicts, not real on-disk artifacts the gate
        # could inspect -- so it's bypassed here as a passing no-op. The gate's own
        # behavior (fail-closed on a real failure, pass-through on a real pass) is
        # covered separately in TestProductionOutputGateWiring, which does not use
        # this helper.
        stack.enter_context(patch(
            "hypogeum.agents.publishing_agent.enforce_production_output_gate",
            return_value=MagicMock(passed=True, checks=[]),
        ))
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
# TestYouTubeProviderVerificationRecording — publishing_agent must record
# its own already-known OAuth result into the shared provider-verification
# state (so /platforms and /attention become accurate) without making a
# second request to Google.
# ---------------------------------------------------------------------------

class TestYouTubeProviderVerificationRecording:

    def test_success_records_shared_provider_health(self, tmp_path):
        config = _make_config(has_youtube=True, has_meta=True)
        pkg = _make_final_render_package("ep_ytrecord_001", tmp_path)

        with _patch_platforms(), \
             patch("hypogeum.provider_verifier.record_provider_verification") as mock_record:
            _run(pkg, config, "ep_ytrecord_001", tmp_path)

        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["provider"] == "youtube"
        assert mock_record.call_args.kwargs["ok"] is True
        assert mock_record.call_args.kwargs["source"] == "env"

    def test_failure_records_shared_provider_failure_with_error_code(self, tmp_path):
        config = _make_config(has_youtube=True, has_meta=True)
        pkg = _make_final_render_package("ep_ytrecord_002", tmp_path)

        exc = HardFailureError(
            "YouTube OAuth exchange failed: invalid_grant", status_code=400, error_code="invalid_grant",
        )
        with _patch_platforms(yt_refresh_exc=exc), \
             patch("hypogeum.provider_verifier.record_provider_verification") as mock_record:
            _run(pkg, config, "ep_ytrecord_003", tmp_path)

        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["provider"] == "youtube"
        assert mock_record.call_args.kwargs["ok"] is False
        assert mock_record.call_args.kwargs["error"] == "invalid_grant"

    def test_failure_without_error_code_falls_back_to_message(self, tmp_path):
        """SoftFailureError (e.g. network/timeout) has no error_code attribute
        at all -- the recorded error must fall back to the exception message
        rather than crash or record nothing."""
        config = _make_config(has_youtube=True, has_meta=True)
        pkg = _make_final_render_package("ep_ytrecord_004", tmp_path)

        exc = SoftFailureError("YouTube OAuth exchange network error: timed out")
        with _patch_platforms(yt_refresh_exc=exc), \
             patch("hypogeum.provider_verifier.record_provider_verification") as mock_record:
            _run(pkg, config, "ep_ytrecord_004", tmp_path)

        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["ok"] is False
        assert "timed out" in mock_record.call_args.kwargs["error"]

    def test_does_not_make_a_second_oauth_request_via_verify_provider(self, tmp_path):
        """The whole point of record_provider_verification(): publishing
        must reuse the OAuth result it already obtained, never call
        verify_provider() (which would re-probe Google) to record it."""
        config = _make_config(has_youtube=True, has_meta=True)
        pkg = _make_final_render_package("ep_ytrecord_005", tmp_path)

        with _patch_platforms(), \
             patch("hypogeum.provider_verifier.verify_provider") as mock_verify, \
             patch("hypogeum.provider_verifier.record_provider_verification") as mock_record:
            _run(pkg, config, "ep_ytrecord_005", tmp_path)

        mock_verify.assert_not_called()
        mock_record.assert_called_once()

    def test_existing_pdr_and_failure_behavior_unchanged(self, tmp_path):
        """The provider-verification recording is additive -- the existing
        PDR / failures-list behavior from TestYouTubeAuthFailure must be
        byte-for-byte unchanged."""
        config = _make_config(has_youtube=True, has_meta=True)
        pkg = _make_final_render_package("ep_ytrecord_006", tmp_path)

        with _patch_platforms(yt_refresh_exc=HardFailureError("401 Unauthorized", status_code=401)):
            result = _run(pkg, config, "ep_ytrecord_006", tmp_path)

        assert len(result.get("failures", [])) > 0
        yt_results = [r for r in result["platform_results"] if r["platform"] == "youtube"]
        assert all(r["status"] == "failed" for r in yt_results)

    def test_hard_failure_records_transient_false(self, tmp_path):
        """A definitive OAuth rejection (HardFailureError) must be recorded
        as transient=False -- eligible to persist as STATUS_FAILED."""
        config = _make_config(has_youtube=True, has_meta=True)
        pkg = _make_final_render_package("ep_ytrecord_008", tmp_path)

        exc = HardFailureError(
            "invalid_grant", status_code=400, error_code="invalid_grant",
        )
        with _patch_platforms(yt_refresh_exc=exc), \
             patch("hypogeum.provider_verifier.record_provider_verification") as mock_record:
            _run(pkg, config, "ep_ytrecord_008", tmp_path)

        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["transient"] is False

    def test_soft_failure_records_transient_true(self, tmp_path):
        """A transient operational failure (SoftFailureError: timeout,
        network error, 429, upstream 5xx) must be recorded as
        transient=True -- must never overwrite a prior definitive
        verified/failed result with STATUS_FAILED."""
        config = _make_config(has_youtube=True, has_meta=True)
        pkg = _make_final_render_package("ep_ytrecord_009", tmp_path)

        exc = SoftFailureError("YouTube OAuth exchange network error: timed out")
        with _patch_platforms(yt_refresh_exc=exc), \
             patch("hypogeum.provider_verifier.record_provider_verification") as mock_record:
            _run(pkg, config, "ep_ytrecord_009", tmp_path)

        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["transient"] is True

    def test_transient_failure_still_produces_pdr_and_failure(self, tmp_path):
        """A transient failure must not be swallowed -- publishing still
        fails/skips YouTube and issues a PDR exactly as before. Only the
        shared provider-verification record's status persistence differs."""
        config = _make_config(has_youtube=True, has_meta=True)
        pkg = _make_final_render_package("ep_ytrecord_010", tmp_path)

        exc = SoftFailureError("YouTube OAuth exchange network error: timed out")
        with _patch_platforms(yt_refresh_exc=exc):
            result = _run(pkg, config, "ep_ytrecord_010", tmp_path)

        assert len(result.get("failures", [])) > 0
        yt_results = [r for r in result["platform_results"] if r["platform"] == "youtube"]
        assert all(r["status"] == "failed" for r in yt_results)

    def test_recording_failure_never_blocks_publishing(self, tmp_path):
        """record_provider_verification() itself raising must not break
        publishing -- it is visibility only."""
        config = _make_config(has_youtube=True, has_meta=True)
        pkg = _make_final_render_package("ep_ytrecord_007", tmp_path)

        with _patch_platforms(), \
             patch("hypogeum.provider_verifier.record_provider_verification",
                   side_effect=RuntimeError("db unavailable")):
            result = _run(pkg, config, "ep_ytrecord_007", tmp_path)

        # YouTube auth itself succeeded (no yt_refresh_exc) -- a broken
        # recording call must not turn that into a publishing failure.
        yt_results = [r for r in result["platform_results"] if r["platform"] == "youtube"]
        assert len(yt_results) > 0
        assert all(r["status"] != "failed" for r in yt_results)


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
        # (Multi-platform publishing idempotency, 2026-07-25: shorts/reels now
        # also call this for their own deliverable_id — assert on the main
        # episode's call specifically, not call count.)
        config = _make_config(has_youtube=True, has_meta=False, has_tiktok=False)
        pkg = _make_final_render_package("ep_outcome_db_001", tmp_path)

        with _patch_platforms(), \
             patch("hypogeum.agents.publishing_agent.record_publishing_outcome") as mock_outcome:
            _run(pkg, config, "ep_outcome_db_001", tmp_path)

        main_calls = [
            c for c in mock_outcome.call_args_list
            if c.kwargs.get("platform") == "youtube"
            and c.kwargs.get("deliverable_id", "main_episode") == "main_episode"
        ]
        assert len(main_calls) == 1
        call_kwargs = main_calls[0].kwargs
        assert call_kwargs["episode_id"] == "ep_outcome_db_001"
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

    def test_tiktok_retries_transient_failure_and_succeeds(self, tmp_path):
        """Retry parity with YouTube/Facebook (2026-07-26): a transient
        SoftFailureError from init_tiktok_upload() on the first attempt
        must be retried, not treated as an immediate failure."""
        from hypogeum.publishing.retry import SoftFailureError

        config = _make_config(has_tiktok=True)
        pkg = _make_final_render_package("ep_tt_retry_001", tmp_path)

        call_count = [0]

        def flaky_init(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise SoftFailureError("HTTP 503 — soft failure (retry eligible)", status_code=503)
            return ("tt_pub_after_retry", "https://upload.tiktok/retry")

        with _patch_platforms(tiktok_avail=True), \
             patch(f"{_TT}.init_tiktok_upload", side_effect=flaky_init), \
             patch("hypogeum.publishing.retry._VIDEO_UPLOAD_INITIAL_DELAY", 0.001):
            result = _run(pkg, config, "ep_tt_retry_001", tmp_path)

        # First short retried once and succeeded; the other two shorts each
        # call init once (fresh call_count continuation), so overall this
        # proves at least one retry occurred without the whole run failing.
        tt_results = [r for r in result["platform_results"] if r["platform"] == "tiktok"]
        assert any(r["status"] == "published" for r in tt_results)
        assert call_count[0] >= 4  # 1 failed + 1 retried-success + 2 more shorts


# ---------------------------------------------------------------------------
# TestTiktokUploadToInboxMode — capability-gated (2026-07-26): a credential
# with only video.upload (no video.publish) must never attempt Direct Post,
# and its real outcome (draft delivered to the creator's inbox) must be
# recorded as uploaded_to_inbox, never published.
# ---------------------------------------------------------------------------

class TestTiktokUploadToInboxMode:
    def test_uses_inbox_flow_and_records_uploaded_to_inbox(self, tmp_path):
        config = _make_config(has_tiktok=True)
        pkg = _make_final_render_package("ep_tt_inbox_001", tmp_path)

        with _patch_platforms(tiktok_avail=True, tiktok_publish_mode="upload_to_inbox") as _, \
             patch(f"{_TT}.init_tiktok_upload") as mock_direct_init:
            result = _run(pkg, config, "ep_tt_inbox_001", tmp_path)

        tt_results = [r for r in result["platform_results"] if r["platform"] == "tiktok"]
        assert len(tt_results) == 3
        assert all(r["status"] == "uploaded_to_inbox" for r in tt_results)
        assert all(r["skipped_reason"] == "awaiting_human_publish" for r in tt_results)

        # The whole point of capability gating: Direct Post must never be
        # attempted when video.publish is absent.
        mock_direct_init.assert_not_called()

    def test_uploaded_to_inbox_never_counts_as_published(self, tmp_path):
        """PublishingRecord's summary/readiness must not treat inbox
        delivery as a real publish — Section 3 of the platform's own
        interface contract: this is real progress, not full autonomy."""
        config = _make_config(has_tiktok=True)
        pkg = _make_final_render_package("ep_tt_inbox_002", tmp_path)

        with _patch_platforms(tiktok_avail=True, tiktok_publish_mode="upload_to_inbox"):
            result = _run(pkg, config, "ep_tt_inbox_002", tmp_path)

        assert result["summary"]["total_published"] == 0 or all(
            r["platform"] != "tiktok"
            for r in result["platform_results"]
            if r["status"] == "published"
        )

    def test_never_attempts_direct_post_when_mode_unresolvable(self, tmp_path):
        """If the credential's scope contains neither video.publish nor
        video.upload, resolve_tiktok_publish_mode() raises — TikTok must be
        treated as unavailable (skipped), never crash the whole run."""
        config = _make_config(has_tiktok=True)
        pkg = _make_final_render_package("ep_tt_inbox_003", tmp_path)

        with _patch_platforms(
            tiktok_avail=True,
            tiktok_mode_exc=RuntimeError("neither video.publish nor video.upload"),
        ) as _, patch(f"{_TT}.init_tiktok_upload") as mock_direct_init, \
             patch(f"{_TT}.init_tiktok_inbox_upload") as mock_inbox_init:
            result = _run(pkg, config, "ep_tt_inbox_003", tmp_path)

        tt_results = [r for r in result["platform_results"] if r["platform"] == "tiktok"]
        assert len(tt_results) == 3
        assert all(r["status"] == "skipped" for r in tt_results)
        mock_direct_init.assert_not_called()
        mock_inbox_init.assert_not_called()


# ---------------------------------------------------------------------------
# TestTiktokInboxIdempotencyGuard — a rerun after a crash between a
# successful inbox upload and the run being marked complete must not create
# a second, duplicate inbox draft.
# ---------------------------------------------------------------------------

class TestTiktokInboxIdempotencyGuard:
    def test_skips_reupload_when_already_uploaded_to_inbox(self, tmp_path):
        from hypogeum.decision_inputs.store import record_publishing_outcome

        config = _make_config(has_tiktok=True)
        pkg = _make_final_render_package("ep_tt_inbox_idem_001", tmp_path)
        record_publishing_outcome(
            "ep_tt_inbox_idem_001", "tiktok", "uploaded_to_inbox",
            platform_video_id="tt_prior_publish_id",
            deliverable_id="short_sh_001",
            db_path=tmp_path / "hypogeum.db",
        )

        with _patch_platforms(tiktok_avail=True, tiktok_publish_mode="upload_to_inbox") as _, \
             patch(f"{_TT}.init_tiktok_inbox_upload") as mock_inbox_init:
            result = _run(pkg, config, "ep_tt_inbox_idem_001", tmp_path)

        # sh_001's prior inbox upload is never redone; sh_002/sh_003 still
        # attempt normally (2 calls, not 3).
        assert mock_inbox_init.call_count == 2
        tt_sh001 = next(
            r for r in result["platform_results"]
            if r["platform"] == "tiktok" and r.get("short_id") == "sh_001"
        )
        assert tt_sh001["status"] == "uploaded_to_inbox"
        assert tt_sh001["platform_video_id"] == "tt_prior_publish_id"


# ---------------------------------------------------------------------------
# TestYouTubeIdempotencyGuard — a rerun after a crash between a successful
# upload and the run being marked complete must not re-upload the video.
# Guard reads publishing_outcomes (the same table record_publishing_outcome
# already writes to on success) before attempting the upload.
# ---------------------------------------------------------------------------

class TestYouTubeIdempotencyGuard:
    def test_skips_reupload_when_already_published(self, tmp_path):
        from hypogeum.decision_inputs.store import record_publishing_outcome

        config = _make_config(has_youtube=True, has_meta=False, has_tiktok=False)
        pkg = _make_final_render_package("ep_idem_001", tmp_path)
        record_publishing_outcome(
            "ep_idem_001", "youtube", "success",
            platform_video_id="yt_prior_video",
            published_at="2026-07-20T00:00:00+00:00",
            db_path=tmp_path / "hypogeum.db",
        )

        with _patch_platforms():
            with patch(f"{_YT}.upload_video") as mock_upload:
                mock_upload.return_value = "yt_short_generic"
                result = _run(pkg, config, "ep_idem_001", tmp_path)

        # Main episode is never re-uploaded (guard fires); the 3 shorts are
        # a separate deliverable the guard does not cover, so they still
        # upload normally — call_count reflects shorts only.
        assert mock_upload.call_count == 3
        assert all(
            "main_" not in str(call.kwargs.get("video_path", ""))
            for call in mock_upload.call_args_list
        )
        yt_main = next(
            r for r in result["platform_results"]
            if r["platform"] == "youtube" and r["deliverable_type"] == "main_episode"
        )
        assert yt_main["status"] == "published"
        assert yt_main["platform_video_id"] == "yt_prior_video"
        assert result["summary"]["youtube_main_published"] is True
        assert result["summary"]["youtube_main_video_id"] == "yt_prior_video"

    def test_does_not_rewrite_outcome_when_skipping(self, tmp_path):
        # Regression: skipping the already-published YouTube main must not
        # re-record its outcome. (Multi-platform publishing idempotency,
        # 2026-07-25: the 3 shorts are NOT pre-populated here, so they still
        # upload normally and correctly get their own record calls — assert
        # specifically that the main episode's deliverable is never re-recorded.)
        from hypogeum.decision_inputs.store import record_publishing_outcome

        config = _make_config(has_youtube=True, has_meta=False, has_tiktok=False)
        pkg = _make_final_render_package("ep_idem_002", tmp_path)
        record_publishing_outcome(
            "ep_idem_002", "youtube", "success",
            platform_video_id="yt_prior_video_2",
            db_path=tmp_path / "hypogeum.db",
        )

        with _patch_platforms(), \
             patch("hypogeum.agents.publishing_agent.record_publishing_outcome") as mock_outcome:
            _run(pkg, config, "ep_idem_002", tmp_path)

        main_calls = [
            c for c in mock_outcome.call_args_list
            if c.kwargs.get("platform") == "youtube"
            and c.kwargs.get("deliverable_id", "main_episode") == "main_episode"
        ]
        assert main_calls == []

    def test_failed_prior_outcome_does_not_block_reupload(self, tmp_path):
        from hypogeum.decision_inputs.store import record_publishing_outcome

        config = _make_config(has_youtube=True, has_meta=False, has_tiktok=False)
        pkg = _make_final_render_package("ep_idem_003", tmp_path)
        record_publishing_outcome(
            "ep_idem_003", "youtube", "failed",
            failure_reason="prior attempt failed",
            db_path=tmp_path / "hypogeum.db",
        )

        with _patch_platforms():
            with patch(f"{_YT}.upload_video") as mock_upload:
                mock_upload.return_value = "yt_new_upload"
                result = _run(pkg, config, "ep_idem_003", tmp_path)

        # Main + 3 shorts all upload normally — a prior *failed* outcome
        # must not be treated as "already published".
        assert mock_upload.call_count == 4
        yt_main = next(
            r for r in result["platform_results"]
            if r["platform"] == "youtube" and r["deliverable_type"] == "main_episode"
        )
        assert yt_main["platform_video_id"] == "yt_new_upload"

    def test_no_prior_outcome_uploads_normally(self, tmp_path):
        config = _make_config(has_youtube=True, has_meta=False, has_tiktok=False)
        pkg = _make_final_render_package("ep_idem_004", tmp_path)

        with _patch_platforms():
            with patch(f"{_YT}.upload_video") as mock_upload:
                mock_upload.return_value = "yt_first_upload"
                result = _run(pkg, config, "ep_idem_004", tmp_path)

        assert mock_upload.call_count == 4  # main + 3 shorts
        yt_main = next(
            r for r in result["platform_results"]
            if r["platform"] == "youtube" and r["deliverable_type"] == "main_episode"
        )
        assert yt_main["platform_video_id"] == "yt_first_upload"

    def test_dry_run_ignores_prior_outcome(self, tmp_path):
        from hypogeum.decision_inputs.store import record_publishing_outcome

        config = _make_config(has_youtube=True, has_meta=False, has_tiktok=False)
        config.YOUTUBE_DRY_RUN = True
        pkg = _make_final_render_package("ep_idem_005", tmp_path)
        record_publishing_outcome(
            "ep_idem_005", "youtube", "success",
            platform_video_id="yt_prior_video_5",
            db_path=tmp_path / "hypogeum.db",
        )

        with _patch_platforms():
            with patch(f"{_YT}.upload_video") as mock_upload:
                result = _run(pkg, config, "ep_idem_005", tmp_path)

        mock_upload.assert_not_called()  # dry-run never calls upload_video regardless
        yt_main = next(
            r for r in result["platform_results"]
            if r["platform"] == "youtube" and r["deliverable_type"] == "main_episode"
        )
        assert yt_main["platform_video_id"].startswith("dry_run_")


# ---------------------------------------------------------------------------
# TestMultiPlatformIdempotencyGuard — generalizes TestYouTubeIdempotencyGuard's
# guard to YouTube Shorts, Facebook, Instagram Reels, and TikTok Shorts
# (multi-platform publishing idempotency, 2026-07-25). Each platform reuses
# the same get_publishing_outcome()/record_publishing_outcome() pair, keyed
# by deliverable_id ('main_episode' or 'short_<short_id>') — not a
# platform-specific mechanism.
# ---------------------------------------------------------------------------

class TestMultiPlatformIdempotencyGuard:
    def _pre_record(self, episode_id, platform, deliverable_id, video_id, tmp_path):
        from hypogeum.decision_inputs.store import record_publishing_outcome
        record_publishing_outcome(
            episode_id, platform, "success",
            platform_video_id=video_id,
            deliverable_id=deliverable_id,
            db_path=tmp_path / "hypogeum.db",
        )

    def test_youtube_short_skipped_when_already_published(self, tmp_path):
        episode_id = "ep_mpi_yt_short"
        config = _make_config(has_youtube=True, has_meta=False, has_tiktok=False)
        pkg = _make_final_render_package(episode_id, tmp_path)
        self._pre_record(episode_id, "youtube", "short_sh_001", "yt_prior_short", tmp_path)

        with _patch_platforms():
            with patch(f"{_YT}.upload_video") as mock_upload:
                mock_upload.return_value = "yt_new_upload"
                result = _run(pkg, config, episode_id, tmp_path)

        # main + 2 remaining shorts upload; sh_001 is skipped.
        assert mock_upload.call_count == 3
        assert all(
            "sh_001" not in str(call.kwargs.get("video_path", ""))
            for call in mock_upload.call_args_list
        )
        sh1 = next(
            r for r in result["platform_results"]
            if r["platform"] == "youtube" and r.get("short_id") == "sh_001"
        )
        assert sh1["status"] == "published"
        assert sh1["platform_video_id"] == "yt_prior_short"

    def test_facebook_skipped_when_already_published(self, tmp_path):
        episode_id = "ep_mpi_fb"
        config = _make_config(has_youtube=False, has_meta=True, has_tiktok=False)
        pkg = _make_final_render_package(episode_id, tmp_path)
        self._pre_record(episode_id, "facebook", "main_episode", "fb_prior_video", tmp_path)

        with _patch_platforms():
            with patch(f"{_META}.upload_facebook_video") as mock_fb:
                result = _run(pkg, config, episode_id, tmp_path)

        mock_fb.assert_not_called()
        fb = next(r for r in result["platform_results"] if r["platform"] == "facebook")
        assert fb["status"] == "published"
        assert fb["platform_video_id"] == "fb_prior_video"

    def test_instagram_reel_skipped_when_already_published(self, tmp_path):
        episode_id = "ep_mpi_ig"
        config = _make_config(has_youtube=False, has_meta=True, has_tiktok=False)
        pkg = _make_final_render_package(episode_id, tmp_path)
        self._pre_record(episode_id, "instagram", "short_sh_001", "ig_prior_media", tmp_path)

        with _patch_platforms():
            with patch(f"{_META}.create_instagram_container") as mock_create:
                mock_create.return_value = "ig_cont_new"
                result = _run(pkg, config, episode_id, tmp_path)

        # Only the 2 remaining reels create a container — sh_001 is skipped.
        assert mock_create.call_count == 2
        ig1 = next(
            r for r in result["platform_results"]
            if r["platform"] == "instagram" and r.get("short_id") == "sh_001"
        )
        assert ig1["status"] == "published"
        assert ig1["platform_video_id"] == "ig_prior_media"

    def test_tiktok_short_skipped_when_already_published(self, tmp_path):
        episode_id = "ep_mpi_tt"
        config = _make_config(has_youtube=False, has_meta=False, has_tiktok=True)
        pkg = _make_final_render_package(episode_id, tmp_path)
        self._pre_record(episode_id, "tiktok", "short_sh_001", "tt_prior_post", tmp_path)

        with _patch_platforms(tiktok_avail=True):
            with patch(f"{_TT}.init_tiktok_upload") as mock_init:
                mock_init.return_value = ("tt_pub_new", "https://upload.tiktok/new")
                result = _run(pkg, config, episode_id, tmp_path)

        assert mock_init.call_count == 2
        tt1 = next(
            r for r in result["platform_results"]
            if r["platform"] == "tiktok" and r.get("short_id") == "sh_001"
        )
        assert tt1["status"] == "published"
        assert tt1["platform_video_id"] == "tt_prior_post"

    def test_partial_instagram_reels_resume_only_unfinished(self, tmp_path):
        """
        Requirement: partial multi-platform publishing resumes only the
        unfinished platforms/deliverables. 2 of 3 Instagram Reels already
        succeeded — only the 1 remaining reel should attempt a real upload.
        """
        episode_id = "ep_mpi_ig_partial"
        config = _make_config(has_youtube=False, has_meta=True, has_tiktok=False)
        pkg = _make_final_render_package(episode_id, tmp_path)
        self._pre_record(episode_id, "instagram", "short_sh_001", "ig_done_1", tmp_path)
        self._pre_record(episode_id, "instagram", "short_sh_002", "ig_done_2", tmp_path)

        with _patch_platforms():
            with patch(f"{_META}.create_instagram_container") as mock_create:
                mock_create.return_value = "ig_cont_new"
                result = _run(pkg, config, episode_id, tmp_path)

        assert mock_create.call_count == 1
        ig_results = {
            r["short_id"]: r for r in result["platform_results"]
            if r["platform"] == "instagram"
        }
        assert ig_results["sh_001"]["status"] == "published"
        assert ig_results["sh_001"]["platform_video_id"] == "ig_done_1"
        assert ig_results["sh_002"]["status"] == "published"
        assert ig_results["sh_002"]["platform_video_id"] == "ig_done_2"
        assert ig_results["sh_003"]["status"] == "published"  # freshly uploaded

    def test_youtube_main_and_shorts_all_skip_on_full_rerun(self, tmp_path):
        """
        Integration check (verification plan item 2): run twice for the
        same episode_id with everything mocked to succeed on the first
        call; the second call must not re-attempt any of the 4 YouTube
        deliverables (main + 3 shorts).
        """
        episode_id = "ep_mpi_full_rerun"
        config = _make_config(has_youtube=True, has_meta=False, has_tiktok=False)
        pkg = _make_final_render_package(episode_id, tmp_path)

        with _patch_platforms():
            first = _run(pkg, config, episode_id, tmp_path)
        assert first["package_metadata"]["overall_status"] == "published"

        with _patch_platforms():
            with patch(f"{_YT}.upload_video") as mock_upload:
                second = _run(pkg, config, episode_id, tmp_path)

        mock_upload.assert_not_called()
        yt_results = [r for r in second["platform_results"] if r["platform"] == "youtube"]
        assert len(yt_results) == 4
        assert all(r["status"] == "published" for r in yt_results)


# ---------------------------------------------------------------------------
# TestEnabledPlatformsFilter — run_worker.py <-> ChannelOperationsProfile
# platform gating (2026-07-21 correction). Proves the filter reaches real
# provider calls, not just result metadata.
# ---------------------------------------------------------------------------

class TestEnabledPlatformsFilter:
    def test_youtube_only_skips_other_platforms_with_no_provider_calls(self, tmp_path):
        config = _make_config(has_youtube=True, has_meta=True, has_tiktok=True)
        pkg = _make_final_render_package("ep_filter_yt_only", tmp_path)

        with _patch_platforms(tiktok_avail=True) as _, \
             patch(f"{_META}.refresh_meta_token") as meta_refresh_mock, \
             patch(f"{_META}.upload_facebook_video") as fb_upload_mock, \
             patch(f"{_META}.create_instagram_container") as ig_container_mock, \
             patch(f"{_TT}.refresh_tiktok_token") as tt_refresh_mock, \
             patch(f"{_TT}.check_tiktok_availability") as tt_avail_mock, \
             patch(f"{_TT}.init_tiktok_upload") as tt_init_mock:
            result = run_publishing_agent(
                pkg, config, "ep_filter_yt_only",
                output_dir=tmp_path, project_root=tmp_path,
                publish_at="2026-07-02T14:00:00+00:00",
                enabled_platforms=frozenset({"youtube"}),
            )

        # No provider call of any kind for excluded platforms — not even
        # OAuth/token refresh or availability checks.
        meta_refresh_mock.assert_not_called()
        fb_upload_mock.assert_not_called()
        ig_container_mock.assert_not_called()
        tt_refresh_mock.assert_not_called()
        tt_avail_mock.assert_not_called()
        tt_init_mock.assert_not_called()

        assert result["summary"]["youtube_main_published"] is True

        fb_results = [r for r in result["platform_results"] if r["platform"] == "facebook"]
        assert all(r["status"] == "skipped" and r["skipped_reason"] == "platform_disabled" for r in fb_results)

        ig_results = [r for r in result["platform_results"] if r["platform"] == "instagram"]
        assert all(r["status"] == "skipped" and r["skipped_reason"] == "platform_disabled" for r in ig_results)

        tt_results = [r for r in result["platform_results"] if r["platform"] == "tiktok"]
        assert all(r["status"] == "skipped" for r in tt_results)

    def test_empty_filter_blocks_every_platform_including_youtube(self, tmp_path):
        config = _make_config(has_youtube=True, has_meta=True, has_tiktok=True)
        pkg = _make_final_render_package("ep_filter_empty", tmp_path)

        with patch(f"{_YT}.refresh_youtube_token") as yt_refresh_mock, \
             patch(f"{_YT}.upload_video") as yt_upload_mock, \
             patch(f"{_META}.refresh_meta_token") as meta_refresh_mock, \
             patch(f"{_TT}.refresh_tiktok_token") as tt_refresh_mock, \
             patch(f"{_TT}.check_tiktok_availability") as tt_avail_mock, \
             patch(
                 "hypogeum.agents.publishing_agent.enforce_production_output_gate",
                 return_value=MagicMock(passed=True, checks=[]),
             ):
            result = run_publishing_agent(
                pkg, config, "ep_filter_empty",
                output_dir=tmp_path, project_root=tmp_path,
                publish_at="2026-07-02T14:00:00+00:00",
                enabled_platforms=frozenset(),
            )

        yt_refresh_mock.assert_not_called()
        yt_upload_mock.assert_not_called()
        meta_refresh_mock.assert_not_called()
        tt_refresh_mock.assert_not_called()
        tt_avail_mock.assert_not_called()

        assert result["summary"]["youtube_main_published"] is False
        assert all(
            r["status"] in ("skipped", "failed") for r in result["platform_results"]
        )

    def test_none_filter_preserves_legacy_attempt_all_behavior(self, tmp_path):
        config = _make_config(has_youtube=True, has_meta=True, has_tiktok=False)
        pkg = _make_final_render_package("ep_filter_none", tmp_path)

        with _patch_platforms():
            result = run_publishing_agent(
                pkg, config, "ep_filter_none",
                output_dir=tmp_path, project_root=tmp_path,
                publish_at="2026-07-02T14:00:00+00:00",
                enabled_platforms=None,
            )

        assert result["package_metadata"]["overall_status"] == "published"
        fb_results = [r for r in result["platform_results"] if r["platform"] == "facebook"]
        assert all(r["status"] == "published" for r in fb_results)


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
        """ENABLE_DEDICATED_SHORT_GENERATION=true → scene plan input includes shorts_scripts and source_type=short beats."""
        from hypogeum.agents.scene_planner import build_scene_plan_input
        from hypogeum.agents.visual_package_director import _flatten_beats_for_a2
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

        # 1. No shorts_count=0 constraint — VPD will plan short scenes
        constraints = input_pkg.get("generation_constraints", {})
        assert constraints.get("shorts_count", -1) != 0, (
            "shorts_count must NOT be 0 when ENABLE_DEDICATED_SHORT_GENERATION=True"
        )
        # 2. shorts_scripts are NOT stripped from the input package
        assert "shorts_scripts" in input_pkg.get("script_package", {}), (
            "shorts_scripts must be passed to VPD when flag is enabled"
        )
        # 3. _flatten_beats_for_a2 on this input produces source_type=short beats
        beats = _flatten_beats_for_a2(input_pkg)
        short_beats = [b for b in beats if b.get("source_type") == "short"]
        assert len(short_beats) == 1, (
            "Expected 1 short beat in flattened plan when shorts_scripts present and flag enabled"
        )
        assert short_beats[0]["short_id"] == "short_001"
        assert short_beats[0]["beat_id"] == "sh1b01"

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


# ---------------------------------------------------------------------------
# TestYouTubeEndScreen
# Tests for auto-selection of two prior videos for end screen slots.
# ---------------------------------------------------------------------------

class TestYouTubeEndScreen:
    """
    Tests for the YouTube end screen wiring:
      - search_best_end_screen_videos is called with count=2 and its
        results are passed through to apply_end_screen as video_ids=
      - apply_end_screen() itself makes no network call and always
        reports "unsupported" -- YouTube Data API v3 has no public
        endpoint for End Screens (see hypogeum.publishing.youtube.
        END_SCREEN_UNSUPPORTED_REASON); prior versions of this test class
        exercised a fabricated `/videos/setEndscreen` POST body that was
        never a real API call in production
    """

    def test_end_screen_uses_two_video_ids(self, tmp_path):
        """apply_end_screen is called with video_ids containing up to 2 items."""
        config = _make_config()
        pkg = _make_final_render_package("ep_es_two_001", tmp_path)
        captured_kwargs = {}

        def spy_apply_end_screen(**kwargs):
            captured_kwargs.update(kwargs)
            return "unsupported"

        with _patch_platforms():
            with patch(f"{_YT}.apply_end_screen", side_effect=spy_apply_end_screen):
                _run(pkg, config, "ep_es_two_001", tmp_path)

        assert "video_ids" in captured_kwargs, "apply_end_screen must receive video_ids="
        assert len(captured_kwargs["video_ids"]) == 2

    def test_apply_end_screen_makes_no_network_call(self):
        """The real apply_end_screen() must never hit the network -- there
        is no real endpoint for it to call. Any requests.post/get call
        here would prove a fabricated API path has resurfaced."""
        from hypogeum.publishing.youtube import apply_end_screen as _apply

        cfg = MagicMock()
        with patch("hypogeum.publishing.youtube.requests.post") as mock_post, \
             patch("hypogeum.publishing.youtube.requests.get") as mock_get:
            result = _apply(
                config=cfg,
                access_token="tok",
                video_id="vid_new",
                duration_seconds=300.0,
                video_ids=["vid_prior_1", "vid_prior_2"],
                episode_id="ep_es_no_network",
            )

        mock_post.assert_not_called()
        mock_get.assert_not_called()
        assert result == "unsupported"

    def test_apply_end_screen_always_returns_unsupported(self):
        """Regardless of arguments (two video_ids, one, none, legacy
        prior_video_id), the current integration always reports
        "unsupported" -- it never fabricates "applied" or "failed" from a
        call that was never made."""
        from hypogeum.publishing.youtube import apply_end_screen as _apply

        cfg = MagicMock()
        for kwargs in (
            {"video_ids": ["a", "b"]},
            {"video_ids": ["a"]},
            {"video_ids": []},
            {"prior_video_id": "legacy_vid"},
            {},
        ):
            result = _apply(
                config=cfg, access_token="tok", video_id="vid_new",
                duration_seconds=300.0, episode_id="ep_es_unsupported", **kwargs,
            )
            assert result == "unsupported"

    def test_search_best_end_screen_videos_returns_list(self, tmp_path):
        """search_best_end_screen_videos() returns up to count video IDs."""
        from hypogeum.publishing.youtube import search_best_end_screen_videos

        cfg = MagicMock()
        cfg.YOUTUBE_CHANNEL_ID = "UCtest"

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "items": [
                {"id": {"videoId": "vid_001"}},
                {"id": {"videoId": "vid_002"}},
                {"id": {"videoId": "vid_003"}},
            ]
        }

        with patch("hypogeum.publishing.youtube.requests.get", return_value=mock_resp):
            result = search_best_end_screen_videos(
                config=cfg,
                access_token="tok",
                episode_id="ep_es_search",
                count=2,
            )

        assert result == ["vid_001", "vid_002"]

    def test_search_best_end_screen_videos_returns_empty_on_network_error(self):
        """search_best_end_screen_videos() returns [] when HTTP fails."""
        from hypogeum.publishing.youtube import search_best_end_screen_videos
        import requests as _req

        cfg = MagicMock()
        cfg.YOUTUBE_CHANNEL_ID = "UCtest"

        with patch("hypogeum.publishing.youtube.requests.get",
                   side_effect=_req.RequestException("timeout")):
            result = search_best_end_screen_videos(
                config=cfg, access_token="tok", episode_id="ep_es_fail",
            )

        assert result == []


# ---------------------------------------------------------------------------
# TestEndScreenRegressionEpF3619022
#
# Regression coverage for the confirmed ep_f3619022 production defect: the
# published publishing_record showed end_screen_applied=null for the
# YouTube main episode -- the idempotency-guard "already published" branch
# (STEP 6) called make_result_entry() without any end_screen kwarg,
# silently defaulting to None, with no way to tell "never attempted" apart
# from "verified applied". Root cause was two-fold: (1) that omitted kwarg,
# and (2) apply_end_screen() itself invoked a fabricated YouTube endpoint
# (`/videos/setEndscreen`) that does not exist in the real YouTube Data
# API v3 -- so even a fresh, non-idempotent publish could never have
# legitimately produced "applied" either.
# ---------------------------------------------------------------------------

class TestEndScreenRegressionEpF3619022:

    def test_idempotency_guard_records_not_attempted_not_null(self, tmp_path):
        """The exact ep_f3619022 code path: YouTube main already published
        in a prior run. end_screen_status must be the explicit string
        "not_attempted", and the legacy end_screen_applied must be False
        -- never None/null."""
        from hypogeum.decision_inputs.store import record_publishing_outcome

        config = _make_config(has_youtube=True, has_meta=False, has_tiktok=False)
        pkg = _make_final_render_package("ep_f3619022_regress", tmp_path)
        record_publishing_outcome(
            "ep_f3619022_regress", "youtube", "success",
            platform_video_id="heCS91MkfdE",
            published_at="2026-08-01T10:13:17+00:00",
            db_path=tmp_path / "hypogeum.db",
        )

        with _patch_platforms():
            with patch(f"{_YT}.upload_video", return_value="unused"):
                result = _run(pkg, config, "ep_f3619022_regress", tmp_path)

        yt_main = next(
            r for r in result["platform_results"]
            if r["platform"] == "youtube" and r["deliverable_type"] == "main_episode"
        )
        meta = yt_main["metadata_applied"]
        assert meta["end_screen_status"] == "not_attempted"
        assert meta["end_screen_applied"] is False
        assert meta["end_screen_status"] is not None, (
            "ep_f3619022 defect: end_screen_status/end_screen_applied must never "
            "be silently null for a published YouTube main episode"
        )

    def test_fresh_publish_over_20s_records_unsupported_and_issues_pdr(self, tmp_path):
        """A genuinely fresh (non-idempotent) publish of a video long enough
        to carry an end screen must record end_screen_status="unsupported"
        (never "applied" -- there is no real API path to verify that) and
        must surface a non-blocking MINOR deficiency so an operator knows
        to add the End Screen manually in YouTube Studio."""
        config = _make_config(has_youtube=True, has_meta=False, has_tiktok=False)
        pkg = _make_final_render_package("ep_es_unsupported_fresh", tmp_path)
        assert pkg["episode_render"]["duration_seconds"] > 20

        with _patch_platforms():
            result = _run(pkg, config, "ep_es_unsupported_fresh", tmp_path)

        yt_main = next(
            r for r in result["platform_results"]
            if r["platform"] == "youtube" and r["deliverable_type"] == "main_episode"
        )
        meta = yt_main["metadata_applied"]
        assert meta["end_screen_status"] == "unsupported"
        assert meta["end_screen_applied"] is False

        failures = result.get("failures", [])
        descriptions = [
            f.get("deficiency_items", [{}])[0].get("description", "") for f in failures
        ]
        assert any("End Screen" in d and "YouTube Studio" in d for d in descriptions), (
            f"expected an operator-facing End Screen deficiency, got: {descriptions}"
        )
        end_screen_failure = next(
            f for f in failures
            if "End Screen" in f.get("deficiency_items", [{}])[0].get("description", "")
        )
        assert end_screen_failure["severity"] == "MINOR", (
            "End Screen unsupported is a platform limitation, not a publish "
            "failure -- must not be BLOCKING/MAJOR"
        )

    def test_short_video_records_not_attempted_without_pdr(self, tmp_path):
        """A video too short to carry a meaningful end screen (<=20s) must
        record "not_attempted" (not "unsupported") and must not issue an
        End Screen deficiency -- there was nothing to attempt."""
        config = _make_config(has_youtube=True, has_meta=False, has_tiktok=False)
        pkg = _make_final_render_package("ep_es_short_video", tmp_path)
        pkg["episode_render"]["duration_seconds"] = 15.0

        with _patch_platforms():
            result = _run(pkg, config, "ep_es_short_video", tmp_path)

        yt_main = next(
            r for r in result["platform_results"]
            if r["platform"] == "youtube" and r["deliverable_type"] == "main_episode"
        )
        assert yt_main["metadata_applied"]["end_screen_status"] == "not_attempted"

        failures = result.get("failures", [])
        assert not any(
            "End Screen" in f.get("deficiency_items", [{}])[0].get("description", "")
            for f in failures
        )

    def test_production_ready_unaffected_by_unsupported_end_screen(self, tmp_path):
        """End Screen being unsupported is a platform limitation, not a
        publish failure -- the episode must still be production_ready when
        everything else genuinely published."""
        config = _make_config(has_youtube=True, has_meta=False, has_tiktok=False)
        pkg = _make_final_render_package("ep_es_prod_ready", tmp_path)

        with _patch_platforms():
            result = _run(pkg, config, "ep_es_prod_ready", tmp_path)

        yt_main = next(
            r for r in result["platform_results"]
            if r["platform"] == "youtube" and r["deliverable_type"] == "main_episode"
        )
        assert yt_main["metadata_applied"]["end_screen_status"] == "unsupported"
        assert result["package_integrity"]["production_ready"] is True
