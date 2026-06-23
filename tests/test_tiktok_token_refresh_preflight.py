"""
tests/test_tiktok_token_refresh_preflight.py — T1 regression tests

Verifies PRE-FLIGHT 5: TikTok token refresh in publishing_agent.

Tests:
  T1-1  refresh_tiktok_token() is called before TikTok publish
  T1-2  Refreshed access_token (not original) is used at publish time
  T1-3  Failed refresh (API rejection) blocks TikTok publish
  T1-4  Non-TikTok platforms (YouTube, Meta) are unaffected by TikTok refresh failure

All tests use mocked HTTP. No real API calls. No file writes beyond tmp_path.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from hypogeum.agents.publishing_agent import run_publishing_agent
from hypogeum.publishing.retry import HardFailureError

# ---------------------------------------------------------------------------
# Module paths for patching
# ---------------------------------------------------------------------------

_YT     = "hypogeum.publishing.youtube"
_META   = "hypogeum.publishing.meta"
_TT     = "hypogeum.publishing.tiktok"
_CRED   = "hypogeum.credential_manager.list_secret_profiles"
_STAGE  = "hypogeum.agents.publishing_agent.get_staging_provider"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(has_tiktok: bool = True) -> MagicMock:
    cfg = MagicMock()
    cfg.YOUTUBE_CLIENT_ID     = None
    cfg.YOUTUBE_CLIENT_SECRET = None
    cfg.YOUTUBE_REFRESH_TOKEN = None
    cfg.YOUTUBE_CHANNEL_ID    = "UC_test"
    cfg.YOUTUBE_DEFAULT_CATEGORY_ID = 27
    cfg.META_APP_ID           = None
    cfg.META_APP_SECRET       = None
    cfg.META_ACCESS_TOKEN     = None
    cfg.META_INSTAGRAM_ACCOUNT_ID = None
    cfg.META_FACEBOOK_PAGE_ID = None
    cfg.META_GRAPH_API_VERSION = "v19.0"
    cfg.META_STAGING_BASE_URL = None
    cfg.TIKTOK_CLIENT_KEY     = "tt_key"    if has_tiktok else None
    cfg.TIKTOK_CLIENT_SECRET  = "tt_secret" if has_tiktok else None
    cfg.TIKTOK_ACCESS_TOKEN   = "original_tt_token" if has_tiktok else None
    cfg.TIKTOK_OPEN_ID        = "tt_open_id" if has_tiktok else None
    cfg.THUMBNAIL_IMAGE_MODEL = "test-model"
    cfg.ASSETS_ROOT           = None
    return cfg


def _make_config_with_meta() -> MagicMock:
    """Config that enables Meta (YouTube still absent) for cross-platform test."""
    cfg = _make_config(has_tiktok=True)
    cfg.META_APP_ID           = "meta_app"
    cfg.META_APP_SECRET       = "meta_secret"
    cfg.META_ACCESS_TOKEN     = "meta_token"
    cfg.META_INSTAGRAM_ACCOUNT_ID = "ig_acct"
    cfg.META_FACEBOOK_PAGE_ID = "fb_page"
    return cfg


def _make_package(tmp_path: Path) -> dict:
    vid   = tmp_path / "ep.mp4";   vid.write_bytes(b"\x00" * 8)
    cap   = tmp_path / "ep.srt";   cap.write_text("1\n00:00:00,000 --> 00:00:01,000\nTest\n")
    thumb = tmp_path / "thumb.jpg"; thumb.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 10 + b"\xff\xd9")
    short_vid = tmp_path / "s1.mp4"; short_vid.write_bytes(b"\x00" * 8)
    short_thumb = tmp_path / "st1.jpg"; short_thumb.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 10 + b"\xff\xd9")
    return {
        "final_render_package_id": "frp_t1",
        "episode_id":              "ep_t1",
        "video_asset_id":          str(vid),
        "caption_asset_id":        str(cap),
        "thumbnail_asset_id":      str(thumb),
        "ep_render":               {"video_asset_id": str(vid), "caption_asset_id": str(cap)},
        "shorts_renders": [
            {
                "short_id":       "sh_01",
                "video_asset_id": str(short_vid),
                "thumbnail_asset_id": str(short_thumb),
            }
        ],
        "metadata_source": {
            "episode_id":        "ep_t1",
            "title":             "T1 Test Episode",
            "description":       "T1 test.",
            "tags":              ["test"],
            "ai_disclosure_text": "AI-assisted production.",
            "hook_statement":    "Hook.",
            "shorts": [
                {
                    "short_id":    "sh_01",
                    "title":       "Short 1",
                    "description": "Short desc",
                    "tags":        ["short"],
                }
            ],
        },
    }


def _run(pkg, cfg, tmp_path):
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    return run_publishing_agent(pkg, cfg, "ep_t1", output_dir=tmp_path, project_root=tmp_path)


@contextmanager
def _base_patches(refresh_side_effect=None, refresh_return="refreshed_tt_token",
                  tiktok_avail=True, meta_refresh_return="meta_refreshed"):
    """Patch all external calls. Yields dict for capturing call arguments."""
    captured = {}

    def _lsp(provider, db_path=None):
        return []  # no SecretProfiles — tests drive via config + refresh mock

    def _tt_refresh(db_path=None):
        if refresh_side_effect is not None:
            raise refresh_side_effect
        return refresh_return

    def _tt_check(config, episode_id=None):
        captured["tt_token_at_check"] = config.TIKTOK_ACCESS_TOKEN
        captured["tt_open_id_at_check"] = config.TIKTOK_OPEN_ID
        return tiktok_avail

    def _meta_refresh(config, episode_id=None):
        captured["meta_token_at_refresh"] = config.META_ACCESS_TOKEN
        return meta_refresh_return

    with patch(_CRED, side_effect=_lsp), \
         patch(f"{_YT}.refresh_youtube_token",
               side_effect=HardFailureError("no yt", 401)), \
         patch(f"{_YT}.reset_quota_counter"), \
         patch(f"{_META}.refresh_meta_token",    side_effect=_meta_refresh), \
         patch(f"{_META}.upload_facebook_video"), \
         patch(f"{_META}.create_instagram_container", return_value="ig_c"), \
         patch(f"{_META}.poll_instagram_container",   return_value="FINISHED"), \
         patch(f"{_META}.publish_instagram_container", return_value="ig_m"), \
         patch(f"{_TT}.refresh_tiktok_token",     side_effect=_tt_refresh) as mock_tt_refresh, \
         patch(f"{_TT}.check_tiktok_availability", side_effect=_tt_check), \
         patch(f"{_TT}.init_tiktok_upload",        return_value=("pub_id", "https://up.tt/")), \
         patch(f"{_TT}.upload_tiktok_chunks"), \
         patch(f"{_TT}.poll_tiktok_publish",       return_value="tt_post_1"), \
         patch(_STAGE,                             return_value=None):
        captured["mock_tt_refresh"] = mock_tt_refresh
        yield captured


# ---------------------------------------------------------------------------
# T1-1  refresh_tiktok_token() is called before TikTok publish
# ---------------------------------------------------------------------------

class TestRefreshCalledBeforePublish:

    def test_refresh_called_exactly_once_before_availability_check(self, tmp_path):
        """refresh_tiktok_token() must fire exactly once per publish run."""
        cfg = _make_config(has_tiktok=True)
        pkg = _make_package(tmp_path)

        with _base_patches(tiktok_avail=False) as cap:
            _run(pkg, cfg, tmp_path)

        cap["mock_tt_refresh"].assert_called_once()

    def test_refresh_receives_db_path(self, tmp_path):
        """refresh_tiktok_token() must be called with db_path keyword argument."""
        cfg = _make_config(has_tiktok=True)
        pkg = _make_package(tmp_path)

        with _base_patches(tiktok_avail=False) as cap:
            _run(pkg, cfg, tmp_path)

        args, kwargs = cap["mock_tt_refresh"].call_args
        assert "db_path" in kwargs, (
            "refresh_tiktok_token must be called with db_path= keyword"
        )
        assert kwargs["db_path"] is not None, (
            "db_path must not be None — agent should pass the assets DB path"
        )

    def test_no_tiktok_credentials_skips_refresh(self, tmp_path):
        """When TIKTOK_ACCESS_TOKEN is absent, refresh must not be attempted."""
        cfg = _make_config(has_tiktok=False)
        pkg = _make_package(tmp_path)

        with _base_patches() as cap:
            _run(pkg, cfg, tmp_path)

        cap["mock_tt_refresh"].assert_not_called()


# ---------------------------------------------------------------------------
# T1-2  Refreshed access_token is used at publish time
# ---------------------------------------------------------------------------

class TestRefreshedTokenUsedAtPublish:

    def test_refreshed_token_replaces_original_in_config(self, tmp_path):
        """
        After a successful refresh, config.TIKTOK_ACCESS_TOKEN must equal the
        value returned by refresh_tiktok_token(), not the original env token.
        """
        cfg = _make_config(has_tiktok=True)
        pkg = _make_package(tmp_path)
        assert cfg.TIKTOK_ACCESS_TOKEN == "original_tt_token"

        with _base_patches(refresh_return="refreshed_tt_token", tiktok_avail=True) as cap:
            _run(pkg, cfg, tmp_path)

        assert "tt_token_at_check" in cap, (
            "check_tiktok_availability was never called"
        )
        assert cap["tt_token_at_check"] == "refreshed_tt_token", (
            f"Expected refreshed token at publish time, "
            f"got {cap['tt_token_at_check']!r}"
        )

    def test_open_id_preserved_after_refresh(self, tmp_path):
        """
        TIKTOK_OPEN_ID must NOT be altered by the refresh step.
        refresh_tiktok_token() only touches access_token and refresh_token.
        """
        cfg = _make_config(has_tiktok=True)
        pkg = _make_package(tmp_path)

        with _base_patches(refresh_return="refreshed_tt_token", tiktok_avail=True) as cap:
            _run(pkg, cfg, tmp_path)

        assert cap.get("tt_open_id_at_check") == "tt_open_id", (
            f"TIKTOK_OPEN_ID was mutated by refresh; got {cap.get('tt_open_id_at_check')!r}"
        )


# ---------------------------------------------------------------------------
# T1-3  Failed refresh blocks TikTok publish
# ---------------------------------------------------------------------------

class TestFailedRefreshBlocksTikTok:

    def test_api_rejection_blocks_tiktok(self, tmp_path):
        """
        When refresh raises RuntimeError (not the 'no profile' variant),
        all TikTok deliverables must be skipped, not published.
        """
        cfg = _make_config(has_tiktok=True)
        pkg = _make_package(tmp_path)

        with _base_patches(
            refresh_side_effect=RuntimeError("HTTP 401 — token rejected by TikTok"),
            tiktok_avail=True,
        ) as cap:
            result = _run(pkg, cfg, tmp_path)

        # check_tiktok_availability must NOT have been called (refresh blocked first)
        assert "tt_token_at_check" not in cap, (
            "check_tiktok_availability was called despite refresh failure"
        )

        # All TikTok deliverables must appear as skipped
        tt_results = [r for r in result.get("platform_results", []) if r.get("platform") == "tiktok"]
        assert len(tt_results) == 1, f"Expected 1 TikTok result, got {len(tt_results)}"
        assert tt_results[0]["status"] == "skipped", (
            f"Expected TikTok status 'skipped', got {tt_results[0]['status']!r}"
        )

    def test_no_secret_profile_is_non_blocking(self, tmp_path):
        """
        When refresh raises 'No active TikTok credential in secret_profiles',
        the pre-flight must be treated as non-blocking and TikTok proceeds.
        """
        cfg = _make_config(has_tiktok=True)
        pkg = _make_package(tmp_path)

        with _base_patches(
            refresh_side_effect=RuntimeError("No active TikTok credential in secret_profiles"),
            tiktok_avail=True,
        ) as cap:
            result = _run(pkg, cfg, tmp_path)

        # check_tiktok_availability SHOULD have been called
        assert "tt_token_at_check" in cap, (
            "'No active TikTok credential' must be non-blocking; "
            "check_tiktok_availability was never called"
        )
        # Token should be the original (unchanged by failed refresh)
        assert cap["tt_token_at_check"] == "original_tt_token", (
            f"Token should be unchanged; got {cap['tt_token_at_check']!r}"
        )

    def test_init_upload_not_called_when_refresh_fails(self, tmp_path):
        """init_tiktok_upload must not be called when refresh fails."""
        cfg = _make_config(has_tiktok=True)
        pkg = _make_package(tmp_path)

        with _base_patches(
            refresh_side_effect=RuntimeError("HTTP 500 — server error"),
        ) as cap, \
             patch(f"{_TT}.init_tiktok_upload") as mock_init:
            _run(pkg, cfg, tmp_path)

        mock_init.assert_not_called()


# ---------------------------------------------------------------------------
# T1-4  Non-TikTok platforms unaffected by TikTok refresh failure
# ---------------------------------------------------------------------------

class TestNonTikTokPlatformsUnaffected:

    def test_meta_still_publishes_when_tiktok_refresh_fails(self, tmp_path):
        """
        A TikTok token refresh failure must not prevent Meta (Facebook/Instagram)
        from publishing. Other platforms continue independently.
        """
        cfg = _make_config_with_meta()
        pkg = _make_package(tmp_path)

        with _base_patches(
            refresh_side_effect=RuntimeError("HTTP 401 — token rejected by TikTok"),
        ) as cap:
            result = _run(pkg, cfg, tmp_path)

        # Meta refresh must have been called (Meta is unaffected)
        assert "meta_token_at_refresh" in cap, (
            "Meta token refresh was never called — TikTok failure leaked into Meta flow"
        )

        # TikTok must be skipped
        tt_results = [r for r in result.get("platform_results", []) if r.get("platform") == "tiktok"]
        assert all(r["status"] == "skipped" for r in tt_results), (
            "TikTok deliverables should be skipped when refresh fails"
        )

    def test_youtube_preflight_runs_regardless_of_tiktok_refresh(self, tmp_path):
        """
        YouTube pre-flight (token refresh) must run independently of TikTok refresh.
        A TikTok refresh failure must not short-circuit the YouTube pre-flight.
        """
        cfg = _make_config(has_tiktok=True)
        cfg.YOUTUBE_CLIENT_ID     = "yt_cid"
        cfg.YOUTUBE_CLIENT_SECRET = "yt_cs"
        cfg.YOUTUBE_REFRESH_TOKEN = "yt_rt"
        pkg = _make_package(tmp_path)

        yt_refresh_called = []

        def _yt_refresh(config):
            yt_refresh_called.append(True)
            raise HardFailureError("yt auth failed", 401)

        with _base_patches(
            refresh_side_effect=RuntimeError("HTTP 401 — token rejected by TikTok"),
        ), patch(f"{_YT}.refresh_youtube_token", side_effect=_yt_refresh):
            _run(pkg, cfg, tmp_path)

        assert yt_refresh_called, (
            "YouTube refresh was not called — TikTok failure blocked YouTube pre-flight"
        )
