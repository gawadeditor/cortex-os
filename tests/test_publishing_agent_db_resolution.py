"""
tests/test_publishing_agent_db_resolution.py — Regression tests for Track B1

Verifies that publishing_agent.run_publishing_agent() applies SecretProfile-first
token resolution for TikTok and Meta before using env / config values.

All tests use mocked list_secret_profiles and mocked platform clients.
No real HTTP calls. No API credits. No file writes beyond tmp_path.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.publishing_agent import PublishingAgentError, run_publishing_agent
from hypogeum.publishing.retry import HardFailureError


# ---------------------------------------------------------------------------
# Path constants for patching
# ---------------------------------------------------------------------------

_AGENT       = "hypogeum.agents.publishing_agent"
_LSP_TT      = f"{_AGENT}.list_secret_profiles"   # patched via import inside fn
_LSP_META    = f"{_AGENT}.list_secret_profiles"
_YT          = "hypogeum.publishing.youtube"
_META_MOD    = "hypogeum.publishing.meta"
_TT_MOD      = "hypogeum.publishing.tiktok"
_STAGING     = f"{_AGENT}.get_staging_provider"
_CRED_MGR    = "hypogeum.credential_manager.list_secret_profiles"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_secret_profile(provider: str, credential_data: str, status: str = "active"):
    """Build a minimal object that looks like a SecretProfile row."""
    p = SimpleNamespace()
    p.secret_profile_id = f"sp_{provider}"
    p.provider          = provider
    p.credential_data   = credential_data
    p.status            = status
    p.label             = f"{provider}-token"
    return p


def _make_config(
    tiktok_token: str = "env_tt_token",
    tiktok_open_id: str = "env_open_id",
    meta_token: str = "env_meta_token",
):
    cfg = MagicMock()
    # YouTube (required for agent startup; set to absent so it skips fast)
    cfg.YOUTUBE_CLIENT_ID     = None
    cfg.YOUTUBE_CLIENT_SECRET = None
    cfg.YOUTUBE_REFRESH_TOKEN = None
    cfg.YOUTUBE_CHANNEL_ID    = "UC_test"
    cfg.YOUTUBE_DEFAULT_CATEGORY_ID = 27
    # Meta
    cfg.META_APP_ID              = "meta_app_id"
    cfg.META_APP_SECRET          = "meta_secret"
    cfg.META_ACCESS_TOKEN        = meta_token
    cfg.META_INSTAGRAM_ACCOUNT_ID = "ig_account"
    cfg.META_FACEBOOK_PAGE_ID    = "fb_page"
    cfg.META_GRAPH_API_VERSION   = "v19.0"
    cfg.META_STAGING_BASE_URL    = None
    # TikTok
    cfg.TIKTOK_CLIENT_KEY    = "tt_key"
    cfg.TIKTOK_CLIENT_SECRET = "tt_secret"
    cfg.TIKTOK_ACCESS_TOKEN  = tiktok_token
    cfg.TIKTOK_OPEN_ID       = tiktok_open_id
    # Misc
    cfg.THUMBNAIL_IMAGE_MODEL = "test-model"
    cfg.ASSETS_ROOT           = None
    return cfg


def _make_final_render_package(tmp_path: Path) -> dict:
    """Minimal final_render_package that passes validation."""
    vid = tmp_path / "main.mp4"
    vid.write_bytes(b"\x00" * 8)
    cap = tmp_path / "main.srt"
    cap.write_text("1\n00:00:00,000 --> 00:00:01,000\nTest\n")
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 10 + b"\xff\xd9")
    return {
        "final_render_package_id": "frp_test",
        "episode_id":              "ep_test",
        "video_asset_id":          str(vid),
        "caption_asset_id":        str(cap),
        "thumbnail_asset_id":      str(thumb),
        "ep_render":               {"video_asset_id": str(vid), "caption_asset_id": str(cap)},
        "shorts_renders":          [],
        "metadata_source": {
            "episode_id":        "ep_test",
            "title":             "Test Episode",
            "description":       "A test episode.",
            "tags":              ["test"],
            "ai_disclosure_text": "AI-assisted production.",
            "hook_statement":    "Hook.",
        },
    }


@contextmanager
def _patched_agent(
    tmp_path:        Path,
    cfg:             MagicMock,
    tt_sp_profiles:  list = None,
    meta_sp_profiles: list = None,
    meta_refresh_exc = None,
    tt_available:    bool = False,
):
    """
    Context manager that patches all external dependencies of run_publishing_agent
    and yields the captured config after DB resolution runs.

    We capture the config passed to refresh_meta_token / check_tiktok_availability
    to inspect what values were applied.
    """
    captured = {}
    tt_profs   = tt_sp_profiles   or []
    meta_profs = meta_sp_profiles or []

    def _fake_lsp(provider, db_path=None):
        if provider == "tiktok":
            return tt_profs
        if provider in ("facebook", "instagram"):
            return [p for p in meta_profs if p.provider == provider]
        if provider == "youtube":
            return []
        return []

    def _fake_meta_refresh(config, episode_id=None):
        # Snapshot the token value NOW — the agent mutates config.META_ACCESS_TOKEN
        # after this call returns, so storing the object reference is unreliable.
        captured["meta_access_token_at_call"] = config.META_ACCESS_TOKEN
        captured["meta_app_id_at_call"]       = config.META_APP_ID
        captured["meta_config"]               = config
        if meta_refresh_exc:
            raise meta_refresh_exc
        return "refreshed_" + (config.META_ACCESS_TOKEN or "")

    def _fake_tt_check(config, episode_id=None):
        captured["tt_config"] = config
        return tt_available

    def _fake_tt_refresh(db_path=None):
        # Simulate "no SecretProfile exists" — non-blocking; lets the token
        # already set by DB resolution flow through unchanged to publish.
        raise RuntimeError("No active TikTok credential in secret_profiles")

    with patch(f"{_CRED_MGR}", side_effect=_fake_lsp), \
         patch(f"{_YT}.refresh_youtube_token",   side_effect=HardFailureError("no yt", 401)), \
         patch(f"{_YT}.reset_quota_counter"), \
         patch(f"{_META_MOD}.refresh_meta_token", side_effect=_fake_meta_refresh), \
         patch(f"{_META_MOD}.upload_facebook_video"), \
         patch(f"{_META_MOD}.create_instagram_container", return_value="ig_c"), \
         patch(f"{_META_MOD}.poll_instagram_container",    return_value="FINISHED"), \
         patch(f"{_META_MOD}.publish_instagram_container", return_value="ig_m"), \
         patch(f"{_TT_MOD}.refresh_tiktok_token",         side_effect=_fake_tt_refresh), \
         patch(f"{_TT_MOD}.check_tiktok_availability",   side_effect=_fake_tt_check), \
         patch(f"{_TT_MOD}.init_tiktok_upload",          return_value=("pub_id", "https://upload.tt/")), \
         patch(f"{_TT_MOD}.upload_tiktok_chunks"), \
         patch(f"{_TT_MOD}.poll_tiktok_publish",         return_value="tt_post_1"), \
         patch(f"{_STAGING}",                            return_value=None), \
         patch(
             "hypogeum.agents.publishing_agent.enforce_production_output_gate",
             return_value=MagicMock(passed=True, checks=[]),
         ):
        # Production Output Gate (2026-08-02): this file tests credential/DB
        # resolution logic against in-memory config, not real on-disk
        # artifacts the gate could inspect -- bypassed here as a passing
        # no-op, same as test_publishing_agent.py's _patch_platforms().
        yield captured


# ---------------------------------------------------------------------------
# TikTok: DB token overrides env token
# ---------------------------------------------------------------------------

class TestTikTokDbResolution:

    def test_raw_token_from_db_overrides_env(self, tmp_path):
        """A raw access token in secret_profiles overrides config.TIKTOK_ACCESS_TOKEN."""
        cfg = _make_config(tiktok_token="env_tt_token")
        pkg = _make_final_render_package(tmp_path)

        sp = _make_secret_profile("tiktok", "db_tt_raw_token")

        with _patched_agent(tmp_path, cfg, tt_sp_profiles=[sp]) as captured:
            try:
                run_publishing_agent(pkg, cfg, "ep_test", tmp_path)
            except Exception:
                pass

        assert "tt_config" in captured, "check_tiktok_availability was never called"
        assert captured["tt_config"].TIKTOK_ACCESS_TOKEN == "db_tt_raw_token", (
            f"Expected DB token 'db_tt_raw_token', got {captured['tt_config'].TIKTOK_ACCESS_TOKEN!r}"
        )

    def test_env_token_unchanged_when_no_db_profile(self, tmp_path):
        """When no active TikTok SecretProfile exists, env token is kept."""
        cfg = _make_config(tiktok_token="env_tt_token")
        pkg = _make_final_render_package(tmp_path)

        with _patched_agent(tmp_path, cfg, tt_sp_profiles=[]) as captured:
            try:
                run_publishing_agent(pkg, cfg, "ep_test", tmp_path)
            except Exception:
                pass

        assert "tt_config" in captured
        assert captured["tt_config"].TIKTOK_ACCESS_TOKEN == "env_tt_token", (
            "Env token should be unchanged when no DB profile exists"
        )

    def test_json_blob_access_token_applied(self, tmp_path):
        """JSON blob with 'access_token' key overrides TIKTOK_ACCESS_TOKEN."""
        cfg = _make_config(tiktok_token="env_tt_token", tiktok_open_id="env_open_id")
        pkg = _make_final_render_package(tmp_path)

        blob = json.dumps({"access_token": "json_tt_token", "open_id": "json_open_id"})
        sp   = _make_secret_profile("tiktok", blob)

        with _patched_agent(tmp_path, cfg, tt_sp_profiles=[sp]) as captured:
            try:
                run_publishing_agent(pkg, cfg, "ep_test", tmp_path)
            except Exception:
                pass

        tt_cfg = captured["tt_config"]
        assert tt_cfg.TIKTOK_ACCESS_TOKEN == "json_tt_token", (
            f"Expected 'json_tt_token', got {tt_cfg.TIKTOK_ACCESS_TOKEN!r}"
        )

    def test_json_blob_open_id_applied(self, tmp_path):
        """JSON blob with 'open_id' key overrides TIKTOK_OPEN_ID."""
        cfg = _make_config(tiktok_token="env_tt_token", tiktok_open_id="env_open_id")
        pkg = _make_final_render_package(tmp_path)

        blob = json.dumps({"access_token": "json_tt_token", "open_id": "json_open_id"})
        sp   = _make_secret_profile("tiktok", blob)

        with _patched_agent(tmp_path, cfg, tt_sp_profiles=[sp]) as captured:
            try:
                run_publishing_agent(pkg, cfg, "ep_test", tmp_path)
            except Exception:
                pass

        assert captured["tt_config"].TIKTOK_OPEN_ID == "json_open_id", (
            f"Expected 'json_open_id', got {captured['tt_config'].TIKTOK_OPEN_ID!r}"
        )

    def test_json_blob_without_open_id_keeps_env_open_id(self, tmp_path):
        """JSON blob with no 'open_id' key leaves TIKTOK_OPEN_ID unchanged."""
        cfg = _make_config(tiktok_token="env_tt_token", tiktok_open_id="env_open_id")
        pkg = _make_final_render_package(tmp_path)

        blob = json.dumps({"access_token": "json_tt_token"})
        sp   = _make_secret_profile("tiktok", blob)

        with _patched_agent(tmp_path, cfg, tt_sp_profiles=[sp]) as captured:
            try:
                run_publishing_agent(pkg, cfg, "ep_test", tmp_path)
            except Exception:
                pass

        assert captured["tt_config"].TIKTOK_OPEN_ID == "env_open_id", (
            "open_id should remain as env value when JSON blob has no 'open_id' key"
        )

    def test_inactive_profile_ignored(self, tmp_path):
        """An inactive SecretProfile must not override the env token."""
        cfg = _make_config(tiktok_token="env_tt_token")
        pkg = _make_final_render_package(tmp_path)

        sp = _make_secret_profile("tiktok", "inactive_db_token", status="inactive")

        with _patched_agent(tmp_path, cfg, tt_sp_profiles=[sp]) as captured:
            try:
                run_publishing_agent(pkg, cfg, "ep_test", tmp_path)
            except Exception:
                pass

        assert captured["tt_config"].TIKTOK_ACCESS_TOKEN == "env_tt_token", (
            "Inactive SecretProfile must not override env token"
        )


# ---------------------------------------------------------------------------
# Meta: DB token overrides env token
# ---------------------------------------------------------------------------

class TestMetaDbResolution:

    def test_raw_facebook_token_from_db_overrides_env(self, tmp_path):
        """A raw token in provider='facebook' SecretProfile overrides META_ACCESS_TOKEN."""
        cfg = _make_config(meta_token="env_meta_token")
        pkg = _make_final_render_package(tmp_path)

        sp = _make_secret_profile("facebook", "db_fb_raw_token")

        with _patched_agent(tmp_path, cfg, meta_sp_profiles=[sp]) as captured:
            try:
                run_publishing_agent(pkg, cfg, "ep_test", tmp_path)
            except Exception:
                pass

        assert "meta_access_token_at_call" in captured, "refresh_meta_token was never called"
        assert captured["meta_access_token_at_call"] == "db_fb_raw_token", (
            f"Expected 'db_fb_raw_token', got {captured['meta_access_token_at_call']!r}"
        )

    def test_instagram_profile_used_when_no_facebook(self, tmp_path):
        """Falls back to provider='instagram' when no facebook SecretProfile exists."""
        cfg = _make_config(meta_token="env_meta_token")
        pkg = _make_final_render_package(tmp_path)

        sp = _make_secret_profile("instagram", "db_ig_token")

        with _patched_agent(tmp_path, cfg, meta_sp_profiles=[sp]) as captured:
            try:
                run_publishing_agent(pkg, cfg, "ep_test", tmp_path)
            except Exception:
                pass

        assert captured["meta_access_token_at_call"] == "db_ig_token", (
            f"Expected fallback 'db_ig_token', got {captured['meta_access_token_at_call']!r}"
        )

    def test_facebook_preferred_over_instagram(self, tmp_path):
        """When both facebook and instagram profiles exist, facebook wins."""
        cfg = _make_config(meta_token="env_meta_token")
        pkg = _make_final_render_package(tmp_path)

        fb_sp = _make_secret_profile("facebook",  "db_fb_token")
        ig_sp = _make_secret_profile("instagram", "db_ig_token")

        with _patched_agent(tmp_path, cfg, meta_sp_profiles=[fb_sp, ig_sp]) as captured:
            try:
                run_publishing_agent(pkg, cfg, "ep_test", tmp_path)
            except Exception:
                pass

        assert captured["meta_access_token_at_call"] == "db_fb_token", (
            "Facebook profile must take priority over Instagram profile"
        )

    def test_json_blob_access_token_applied(self, tmp_path):
        """JSON blob with 'access_token' key overrides META_ACCESS_TOKEN."""
        cfg = _make_config(meta_token="env_meta_token")
        pkg = _make_final_render_package(tmp_path)

        blob = json.dumps({"access_token": "json_fb_token"})
        sp   = _make_secret_profile("facebook", blob)

        with _patched_agent(tmp_path, cfg, meta_sp_profiles=[sp]) as captured:
            try:
                run_publishing_agent(pkg, cfg, "ep_test", tmp_path)
            except Exception:
                pass

        assert captured["meta_access_token_at_call"] == "json_fb_token", (
            f"Expected 'json_fb_token', got {captured['meta_access_token_at_call']!r}"
        )

    def test_env_token_unchanged_when_no_db_profile(self, tmp_path):
        """When no active Meta SecretProfile exists, env token is kept unchanged."""
        cfg = _make_config(meta_token="env_meta_token")
        pkg = _make_final_render_package(tmp_path)

        with _patched_agent(tmp_path, cfg, meta_sp_profiles=[]) as captured:
            try:
                run_publishing_agent(pkg, cfg, "ep_test", tmp_path)
            except Exception:
                pass

        assert captured["meta_access_token_at_call"] == "env_meta_token", (
            "Env token should be unchanged when no DB profile exists"
        )

    def test_meta_app_id_never_overridden_by_db(self, tmp_path):
        """DB resolution must never change META_APP_ID from its env value."""
        cfg = _make_config(meta_token="env_meta_token")
        original_app_id = cfg.META_APP_ID
        pkg = _make_final_render_package(tmp_path)

        sp = _make_secret_profile("facebook", "db_fb_token")

        with _patched_agent(tmp_path, cfg, meta_sp_profiles=[sp]) as captured:
            try:
                run_publishing_agent(pkg, cfg, "ep_test", tmp_path)
            except Exception:
                pass

        assert captured["meta_app_id_at_call"] == original_app_id, (
            "META_APP_ID must never be modified by DB resolution"
        )

    def test_inactive_meta_profile_ignored(self, tmp_path):
        """An inactive Meta SecretProfile must not override the env token."""
        cfg = _make_config(meta_token="env_meta_token")
        pkg = _make_final_render_package(tmp_path)

        sp = _make_secret_profile("facebook", "db_inactive_token", status="inactive")

        with _patched_agent(tmp_path, cfg, meta_sp_profiles=[sp]) as captured:
            try:
                run_publishing_agent(pkg, cfg, "ep_test", tmp_path)
            except Exception:
                pass

        assert captured["meta_access_token_at_call"] == "env_meta_token", (
            "Inactive SecretProfile must not override env token"
        )
