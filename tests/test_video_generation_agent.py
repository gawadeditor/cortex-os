"""
tests/test_video_generation_agent.py — Unit tests for video_generation_agent.py

Coverage:
  TestBuildAssetIndex       — {scene_id: asset_dict} index construction
  TestGenerateSceneVideo    — single-scene submit_scene call + asset_manifest write-back
  TestGenerateAllSceneVideos — full per-scene generation loop
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.muapi import MUAPIAuthFailure, MUAPIQuotaExhausted, MUAPIError, MUAPIBudgetExhausted
from hypogeum.agents.video_generation_agent import (
    build_asset_index,
    generate_scene_video,
    generate_all_scene_videos,
)

# Re-use config helper from VPD tests
import hypogeum.config as _cfg_module

_ALL_ENV_VARS = [
    "OPENROUTER_API_KEY", "OPENROUTER_SONNET_MODEL_ID", "OPENROUTER_OPUS_MODEL_ID",
    "OPENROUTER_APP_NAME", "OPENROUTER_APP_URL", "OPENROUTER_FALLBACK_MODEL",
    "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "PERPLEXITY_MODEL", "PERPLEXITY_MAX_TOKENS",
    "MUAPI_API_KEY", "MUAPI_BASE_URL", "MUAPI_T1_MODEL", "MUAPI_T2_MODEL", "MUAPI_T3_MODEL",
    "MUAPI_I2V_MODEL", "MUAPI_IMAGE_MODEL", "MUAPI_DEFAULT_TIER", "MUAPI_TIMEOUT_SECONDS",
    "MUAPI_POLL_INTERVAL_SECONDS", "MUAPI_MAX_POLL_ATTEMPTS", "MUAPI_WEBHOOK_URL",
    "MUAPI_CREDIT_WARNING_THRESHOLD",
    "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID",
    "ELEVENLABS_STABILITY", "ELEVENLABS_SIMILARITY_BOOST", "ELEVENLABS_MUSIC_MODEL_ID",
    "ELEVENLABS_MUSIC_DEFAULT_DURATION", "OPENAI_API_KEY", "WHISPER_MODEL",
    "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
    "YOUTUBE_CHANNEL_ID", "YOUTUBE_DEFAULT_CATEGORY_ID",
    "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_OPEN_ID",
    "META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN", "META_INSTAGRAM_ACCOUNT_ID",
    "META_FACEBOOK_PAGE_ID", "META_GRAPH_API_VERSION", "META_STAGING_BASE_URL",
]


def make_config():
    from hypogeum.config import Config
    original = {k: os.environ.get(k) for k in _ALL_ENV_VARS}
    for k in _ALL_ENV_VARS:
        os.environ.pop(k, None)
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    os.environ["MUAPI_API_KEY"]      = "muapi-test-key"
    os.environ["MUAPI_BASE_URL"]     = "https://api.muapi.ai/api/v1"
    os.environ["MUAPI_T1_MODEL"]     = "veo3-text-to-video"
    os.environ["MUAPI_T2_MODEL"]     = "runway-text-to-video"
    os.environ["MUAPI_T3_MODEL"]     = "veo3-fast-text-to-video"
    os.environ["MUAPI_I2V_MODEL"]    = "veo3-image-to-video"
    os.environ["MUAPI_IMAGE_MODEL"]  = "flux-2-dev"
    os.environ["MUAPI_POLL_INTERVAL_SECONDS"] = "0"
    os.environ["MUAPI_MAX_POLL_ATTEMPTS"]     = "3"
    try:
        return Config()
    finally:
        for k in _ALL_ENV_VARS:
            os.environ.pop(k, None)
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scene(
    scene_id: str = "s01",
    scene_index: int = 0,
    source_type: str = "main_episode",
    short_id: Optional[str] = None,
) -> dict:
    return {
        "scene_id": scene_id,
        "scene_index": scene_index,
        "source_type": source_type,
        "short_id": short_id,
        "entity_appearances": [],
        "production_spec": {
            "aspect_ratio": "16:9",
            "duration_seconds": 8.0,
            "provider_routing": {
                "primary_tier": 2,
                "primary_prompt": "Ancient throne room.",
                "fallback_tier": 3,
                "fallback_prompt": "Egyptian palace.",
            },
        },
    }


def _make_asset(
    scene_id: str = "s01",
    episode_id: str = "ep_001",
    generation_status: str = "planned",
) -> dict:
    return {
        "asset_id": f"episodes/{episode_id}/video/sc_{scene_id}",
        "asset_type": "generated_video",
        "generation_status": generation_status,
        "used_in_scenes": [scene_id],
    }


def _make_submit_result(
    scene_id: str = "s01",
    episode_id: str = "ep_001",
    generation_status: str = "generated",
) -> dict:
    return {
        "scene_id": scene_id,
        "asset_id": f"episodes/{episode_id}/video/sc_{scene_id}",
        "generation_status": generation_status,
        "provider": "muapi",
        "tier_used": 2,
        "model_used": "runway-text-to-video",
        "prompt_used": "Ancient throne room.",
        "fallback_used": False,
        "failure_reason": None,
    }


def _make_visual_package(
    episode_id: str = "ep_001",
    scenes: Optional[list] = None,
    assets: Optional[list] = None,
) -> dict:
    if scenes is None:
        scenes = [_make_scene()]
    if assets is None:
        assets = [_make_asset(s["scene_id"], episode_id) for s in scenes]
    return {
        "scenes": scenes,
        "asset_manifest": {"assets": assets},
        "provider_routing_manifest": {},
    }


_PATCH_SUBMIT = "hypogeum.agents.video_generation_agent.submit_scene"


# ---------------------------------------------------------------------------
# TestBuildAssetIndex
# ---------------------------------------------------------------------------

class TestBuildAssetIndex:

    def test_indexed_by_scene_id(self):
        vp = _make_visual_package(scenes=[_make_scene("s01")])
        index = build_asset_index(vp)
        assert "s01" in index

    def test_empty_assets_gives_empty_index(self):
        vp = _make_visual_package(assets=[])
        index = build_asset_index(vp)
        assert index == {}

    def test_multiple_scenes_all_indexed(self):
        scenes = [_make_scene("s01"), _make_scene("s02"), _make_scene("s03")]
        vp = _make_visual_package(scenes=scenes)
        index = build_asset_index(vp)
        assert set(index.keys()) == {"s01", "s02", "s03"}

    def test_returns_asset_dict_object(self):
        vp = _make_visual_package(scenes=[_make_scene("s01")])
        index = build_asset_index(vp)
        assert index["s01"]["used_in_scenes"] == ["s01"]

    def test_asset_without_used_in_scenes_not_indexed(self):
        vp = _make_visual_package(assets=[
            {"asset_id": "ep/video/sc_s01", "generation_status": "planned"}
        ])
        index = build_asset_index(vp)
        assert index == {}

    def test_missing_asset_manifest_gives_empty_index(self):
        vp = {"scenes": []}
        index = build_asset_index(vp)
        assert index == {}

    def test_returns_dict_type(self):
        assert isinstance(build_asset_index({}), dict)


# ---------------------------------------------------------------------------
# TestGenerateSceneVideo
# ---------------------------------------------------------------------------

class TestGenerateSceneVideo:

    def _invoke(self, scene, asset_index, asset_manifest_assets,
                submit_result, tmp_path, entity_hosted_urls=None):
        log = MagicMock()
        cfg = make_config()
        with patch(_PATCH_SUBMIT, return_value=submit_result):
            generate_scene_video(
                scene, asset_index, asset_manifest_assets,
                entity_hosted_urls or {}, cfg, "ep_001", tmp_path, log,
            )
        return log

    # --- submit_scene called once ---

    def test_submit_scene_called_once(self, tmp_path):
        scene = _make_scene("s01")
        submit_result = _make_submit_result("s01")
        with patch(_PATCH_SUBMIT, return_value=submit_result) as mock_sub:
            generate_scene_video(
                scene, {}, [], {}, make_config(), "ep_001", tmp_path, MagicMock(),
            )
        mock_sub.assert_called_once()

    # --- existing asset update path ---

    def test_updates_existing_asset_generation_status(self, tmp_path):
        scene = _make_scene("s01")
        existing = _make_asset("s01")
        asset_index = {"s01": existing}
        asset_manifest_assets = [existing]
        self._invoke(scene, asset_index, asset_manifest_assets,
                     _make_submit_result("s01", generation_status="generated"), tmp_path)
        assert existing["generation_status"] == "generated"

    def test_updates_existing_asset_asset_id(self, tmp_path):
        scene = _make_scene("s01")
        existing = {"asset_id": "", "generation_status": "planned", "used_in_scenes": ["s01"]}
        asset_index = {"s01": existing}
        asset_manifest_assets = [existing]
        self._invoke(scene, asset_index, asset_manifest_assets,
                     _make_submit_result("s01"), tmp_path)
        assert existing["asset_id"] == "episodes/ep_001/video/sc_s01"

    def test_update_path_does_not_append(self, tmp_path):
        scene = _make_scene("s01")
        existing = _make_asset("s01")
        asset_manifest_assets = [existing]
        asset_index = {"s01": existing}
        self._invoke(scene, asset_index, asset_manifest_assets,
                     _make_submit_result("s01"), tmp_path)
        assert len(asset_manifest_assets) == 1

    # --- new asset append path ---

    def test_appends_new_entry_when_not_in_index(self, tmp_path):
        scene = _make_scene("s01")
        asset_manifest_assets = []
        asset_index = {}
        self._invoke(scene, asset_index, asset_manifest_assets,
                     _make_submit_result("s01", generation_status="generated"), tmp_path)
        assert len(asset_manifest_assets) == 1

    def test_appended_entry_has_scene_id(self, tmp_path):
        scene = _make_scene("s01")
        asset_manifest_assets = []
        self._invoke(scene, {}, asset_manifest_assets,
                     _make_submit_result("s01"), tmp_path)
        assert asset_manifest_assets[0]["scene_id"] == "s01"

    def test_appended_entry_has_generation_status(self, tmp_path):
        scene = _make_scene("s01")
        asset_manifest_assets = []
        self._invoke(scene, {}, asset_manifest_assets,
                     _make_submit_result("s01", generation_status="generated"), tmp_path)
        assert asset_manifest_assets[0]["generation_status"] == "generated"

    def test_appended_entry_has_asset_id(self, tmp_path):
        scene = _make_scene("s01")
        asset_manifest_assets = []
        self._invoke(scene, {}, asset_manifest_assets,
                     _make_submit_result("s01"), tmp_path)
        assert asset_manifest_assets[0]["asset_id"] == "episodes/ep_001/video/sc_s01"

    def test_appended_entry_has_short_id(self, tmp_path):
        scene = _make_scene("sh_01_s01", source_type="short", short_id="sh_01")
        asset_manifest_assets = []
        self._invoke(scene, {}, asset_manifest_assets,
                     _make_submit_result("sh_01_s01"), tmp_path)
        assert asset_manifest_assets[0]["short_id"] == "sh_01"

    def test_appended_entry_has_source_type(self, tmp_path):
        scene = _make_scene("sh_01_s01", source_type="short", short_id="sh_01")
        asset_manifest_assets = []
        self._invoke(scene, {}, asset_manifest_assets,
                     _make_submit_result("sh_01_s01"), tmp_path)
        assert asset_manifest_assets[0]["source_type"] == "short"

    def test_appended_entry_has_used_in_scenes(self, tmp_path):
        scene = _make_scene("s01")
        asset_manifest_assets = []
        self._invoke(scene, {}, asset_manifest_assets,
                     _make_submit_result("s01"), tmp_path)
        assert asset_manifest_assets[0]["used_in_scenes"] == ["s01"]

    def test_appended_entry_has_provider(self, tmp_path):
        scene = _make_scene("s01")
        asset_manifest_assets = []
        self._invoke(scene, {}, asset_manifest_assets,
                     _make_submit_result("s01"), tmp_path)
        assert asset_manifest_assets[0]["provider"] == "muapi"

    def test_appended_entry_has_model(self, tmp_path):
        scene = _make_scene("s01")
        asset_manifest_assets = []
        self._invoke(scene, {}, asset_manifest_assets,
                     _make_submit_result("s01"), tmp_path)
        assert asset_manifest_assets[0]["model"] == "runway-text-to-video"

    # --- generation_failed handling ---

    def test_generation_failed_does_not_raise(self, tmp_path):
        scene = _make_scene("s01")
        self._invoke(scene, {}, [],
                     _make_submit_result("s01", generation_status="generation_failed"), tmp_path)

    def test_generation_failed_status_written_to_index(self, tmp_path):
        scene = _make_scene("s01")
        existing = _make_asset("s01")
        asset_index = {"s01": existing}
        self._invoke(scene, asset_index, [existing],
                     _make_submit_result("s01", generation_status="generation_failed"), tmp_path)
        assert existing["generation_status"] == "generation_failed"

    def test_generation_failed_appended_with_failed_status(self, tmp_path):
        scene = _make_scene("s01")
        asset_manifest_assets = []
        self._invoke(scene, {}, asset_manifest_assets,
                     _make_submit_result("s01", generation_status="generation_failed"), tmp_path)
        assert asset_manifest_assets[0]["generation_status"] == "generation_failed"

    # --- halt conditions propagate ---

    def test_auth_failure_propagates(self, tmp_path):
        scene = _make_scene("s01")
        with patch(_PATCH_SUBMIT, side_effect=MUAPIAuthFailure()):
            with pytest.raises(MUAPIAuthFailure):
                generate_scene_video(
                    scene, {}, [], {}, make_config(), "ep_001", tmp_path, MagicMock(),
                )

    def test_quota_exhausted_propagates(self, tmp_path):
        scene = _make_scene("s01")
        with patch(_PATCH_SUBMIT, side_effect=MUAPIQuotaExhausted()):
            with pytest.raises(MUAPIQuotaExhausted):
                generate_scene_video(
                    scene, {}, [], {}, make_config(), "ep_001", tmp_path, MagicMock(),
                )

    def test_budget_exhausted_propagates(self, tmp_path):
        scene = _make_scene("s01")
        with patch(_PATCH_SUBMIT, side_effect=MUAPIBudgetExhausted("budget")):
            with pytest.raises(MUAPIBudgetExhausted):
                generate_scene_video(
                    scene, {}, [], {}, make_config(), "ep_001", tmp_path, MagicMock(),
                )

    # --- logging ---

    def test_generation_start_logged(self, tmp_path):
        log = self._invoke(
            _make_scene("s01"), {}, [], _make_submit_result("s01"), tmp_path,
        )
        event_names = [c[0][0] for c in log.info.call_args_list]
        assert "vpd_scene_generation_start" in event_names

    def test_generated_logged_on_success(self, tmp_path):
        log = self._invoke(
            _make_scene("s01"), {}, [], _make_submit_result("s01", generation_status="generated"), tmp_path,
        )
        event_names = [c[0][0] for c in log.info.call_args_list]
        assert "vpd_scene_generated" in event_names

    def test_failed_logged_on_failure(self, tmp_path):
        log = self._invoke(
            _make_scene("s01"), {}, [], _make_submit_result("s01", generation_status="generation_failed"), tmp_path,
        )
        warn_event_names = [c[0][0] for c in log.warn.call_args_list]
        assert "vpd_scene_failed" in warn_event_names


# ---------------------------------------------------------------------------
# TestGenerateAllSceneVideos
# ---------------------------------------------------------------------------

class TestGenerateAllSceneVideos:

    def _invoke(self, visual_package, scenes_sorted, submit_side_effect,
                tmp_path, entity_hosted_urls=None):
        log = MagicMock()
        cfg = make_config()
        with patch(_PATCH_SUBMIT, side_effect=submit_side_effect) as mock_sub:
            generate_all_scene_videos(
                visual_package, scenes_sorted,
                entity_hosted_urls or {}, cfg, "ep_001", tmp_path, log,
            )
        return log, mock_sub

    # --- submit_scene call count ---

    def test_submit_called_once_per_scene(self, tmp_path):
        scenes = [_make_scene(f"s0{i}", i) for i in range(3)]
        vp = _make_visual_package(scenes=scenes)
        results = [_make_submit_result(f"s0{i}") for i in range(3)]
        _, mock_sub = self._invoke(vp, scenes, iter(results), tmp_path)
        assert mock_sub.call_count == 3

    def test_empty_scenes_no_submit_call(self, tmp_path):
        vp = _make_visual_package(scenes=[], assets=[])
        _, mock_sub = self._invoke(vp, [], iter([]), tmp_path)
        mock_sub.assert_not_called()

    def test_single_scene_submit_called_once(self, tmp_path):
        scenes = [_make_scene("s01")]
        vp = _make_visual_package(scenes=scenes)
        _, mock_sub = self._invoke(vp, scenes, [_make_submit_result("s01")], tmp_path)
        mock_sub.assert_called_once()

    # --- asset_manifest updated ---

    def test_asset_manifest_updated_for_each_scene(self, tmp_path):
        scenes = [_make_scene("s01"), _make_scene("s02", 1)]
        vp = _make_visual_package(scenes=scenes)
        results = [
            _make_submit_result("s01", generation_status="generated"),
            _make_submit_result("s02", generation_status="generated"),
        ]
        self._invoke(vp, scenes, iter(results), tmp_path)
        statuses = {
            a["used_in_scenes"][0]: a["generation_status"]
            for a in vp["asset_manifest"]["assets"]
        }
        assert statuses["s01"] == "generated"
        assert statuses["s02"] == "generated"

    def test_failed_scene_does_not_abort_loop(self, tmp_path):
        scenes = [_make_scene("s01"), _make_scene("s02", 1)]
        vp = _make_visual_package(scenes=scenes)
        results = [
            _make_submit_result("s01", generation_status="generation_failed"),
            _make_submit_result("s02", generation_status="generated"),
        ]
        _, mock_sub = self._invoke(vp, scenes, iter(results), tmp_path)
        assert mock_sub.call_count == 2

    # --- halt conditions ---

    def test_auth_failure_propagates(self, tmp_path):
        scenes = [_make_scene("s01")]
        vp = _make_visual_package(scenes=scenes)
        with patch(_PATCH_SUBMIT, side_effect=MUAPIAuthFailure()):
            with pytest.raises(MUAPIAuthFailure):
                generate_all_scene_videos(
                    vp, scenes, {}, make_config(), "ep_001", tmp_path, MagicMock(),
                )

    def test_quota_exhausted_propagates(self, tmp_path):
        scenes = [_make_scene("s01")]
        vp = _make_visual_package(scenes=scenes)
        with patch(_PATCH_SUBMIT, side_effect=MUAPIQuotaExhausted()):
            with pytest.raises(MUAPIQuotaExhausted):
                generate_all_scene_videos(
                    vp, scenes, {}, make_config(), "ep_001", tmp_path, MagicMock(),
                )

    def test_auth_failure_on_second_scene_stops_loop(self, tmp_path):
        scenes = [_make_scene("s01"), _make_scene("s02", 1)]
        vp = _make_visual_package(scenes=scenes)
        side_effects = [_make_submit_result("s01"), MUAPIAuthFailure()]
        with patch(_PATCH_SUBMIT, side_effect=side_effects) as mock_sub:
            with pytest.raises(MUAPIAuthFailure):
                generate_all_scene_videos(
                    vp, scenes, {}, make_config(), "ep_001", tmp_path, MagicMock(),
                )
        # First scene submitted, second raised — loop stops
        assert mock_sub.call_count == 2

    # --- new asset appended when not pre-populated ---

    def test_new_assets_appended_when_manifest_empty(self, tmp_path):
        scenes = [_make_scene("s01")]
        vp = _make_visual_package(scenes=scenes, assets=[])
        self._invoke(vp, scenes, [_make_submit_result("s01")], tmp_path)
        assert len(vp["asset_manifest"]["assets"]) == 1
        assert vp["asset_manifest"]["assets"][0]["scene_id"] == "s01"


# ---------------------------------------------------------------------------
# TestReuseCountingFix (Patch 2 — reuse = success, not failure)
# ---------------------------------------------------------------------------

class TestReuseCountingFix:
    """Verify that generation_status='reused' is treated as success, not failure."""

    def _invoke(self, scene, asset_index, asset_manifest_assets, submit_result, tmp_path):
        log = MagicMock()
        cfg = make_config()
        with patch(_PATCH_SUBMIT, return_value=submit_result):
            generate_scene_video(
                scene, asset_index, asset_manifest_assets, {}, cfg, "ep_001", tmp_path, log,
            )
        return log

    def test_reused_logs_vpd_scene_generated_not_failed(self, tmp_path):
        scene = _make_scene("s01")
        log = self._invoke(scene, {}, [], _make_submit_result("s01", generation_status="reused"), tmp_path)
        info_events = [c[0][0] for c in log.info.call_args_list]
        warn_events = [c[0][0] for c in log.warn.call_args_list]
        assert "vpd_scene_generated" in info_events
        assert "vpd_scene_failed" not in warn_events

    def test_reused_log_event_has_reused_true(self, tmp_path):
        scene = _make_scene("s01")
        log = self._invoke(scene, {}, [], _make_submit_result("s01", generation_status="reused"), tmp_path)
        generated_calls = [c for c in log.info.call_args_list if c[0][0] == "vpd_scene_generated"]
        assert len(generated_calls) == 1
        assert generated_calls[0][1].get("reused") is True

    def test_generated_log_event_has_reused_false(self, tmp_path):
        scene = _make_scene("s01")
        log = self._invoke(scene, {}, [], _make_submit_result("s01", generation_status="generated"), tmp_path)
        generated_calls = [c for c in log.info.call_args_list if c[0][0] == "vpd_scene_generated"]
        assert len(generated_calls) == 1
        assert generated_calls[0][1].get("reused") is False

    def test_reused_status_written_to_asset_index(self, tmp_path):
        scene = _make_scene("s01")
        existing = _make_asset("s01")
        asset_index = {"s01": existing}
        self._invoke(scene, asset_index, [existing],
                     _make_submit_result("s01", generation_status="reused"), tmp_path)
        assert existing["generation_status"] == "reused"

    def test_reused_appended_with_reused_status(self, tmp_path):
        scene = _make_scene("s01")
        asset_manifest_assets = []
        self._invoke(scene, {}, asset_manifest_assets,
                     _make_submit_result("s01", generation_status="reused"), tmp_path)
        assert asset_manifest_assets[0]["generation_status"] == "reused"
