"""
tests/test_scene_planner.py — Unit tests for scene_planner.py

Coverage:
  TestCompactBeat             — word-truncation, ellipsis, field preservation
  TestCompactScriptPackage    — structure preservation, beat compaction, strip_shorts flag
  TestBuildScenePlanInput     — input_package construction, episode-zero mode, shorts suppression
  TestOrderAndFilterScenes    — sort order, shorts filtering, scene cap
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.config import Config
from hypogeum.agents.scene_planner import (
    compact_beat,
    compact_script_package,
    build_scene_plan_input,
    order_and_filter_scenes,
    PROVIDER_CAPABILITIES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def make_config(
    *,
    episode_zero_max_scenes: int = 0,
    enable_shorts: bool = False,
) -> Config:
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
    if episode_zero_max_scenes > 0:
        os.environ["EPISODE_ZERO_MAX_SCENES"] = str(episode_zero_max_scenes)
    os.environ["ENABLE_DEDICATED_SHORT_GENERATION"] = "true" if enable_shorts else "false"
    try:
        return Config()
    finally:
        for k in _ALL_ENV_VARS:
            os.environ.pop(k, None)
        os.environ.pop("EPISODE_ZERO_MAX_SCENES", None)
        os.environ.pop("ENABLE_DEDICATED_SHORT_GENERATION", None)
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v


def _make_beat(text: str = "short text", beat_id: str = "b_001", beat_type: str = "narration") -> dict:
    return {"beat_id": beat_id, "beat_type": beat_type, "text": text}


def _make_script_package(
    episode_id: str = "ep_001",
    *,
    include_shorts: bool = False,
    beats_per_section: int = 1,
) -> dict:
    return {
        "episode_metadata": {"episode_id": episode_id, "topic": "Test Topic"},
        "sections": [
            {
                "section_id": "sec_001",
                "title": "Hook",
                "visual_directions": "Wide establishing shot.",
                "narration_beats": [
                    _make_beat(f"beat {i}", beat_id=f"b_{i:03d}")
                    for i in range(beats_per_section)
                ],
            }
        ],
        "shorts_scripts": [
            {
                "short_id": "sh_01",
                "title": "Short 1",
                "visual_directions": "Close-up.",
                "narration_beats": [_make_beat("Short hook text.", beat_id="b_s01")],
            }
        ] if include_shorts else [],
    }


def _make_scene(
    scene_id: str = "s01",
    scene_index: int = 0,
    source_type: str = "main_episode",
) -> dict:
    return {
        "scene_id": scene_id,
        "scene_index": scene_index,
        "source_type": source_type,
    }


# ---------------------------------------------------------------------------
# TestCompactBeat
# ---------------------------------------------------------------------------

class TestCompactBeat:

    def test_preserves_beat_id(self):
        result = compact_beat({"beat_id": "b_042", "beat_type": "hook", "text": "Hello world."})
        assert result["beat_id"] == "b_042"

    def test_preserves_beat_type(self):
        result = compact_beat({"beat_id": "b_001", "beat_type": "narration", "text": "Hello."})
        assert result["beat_type"] == "narration"

    def test_short_text_unchanged(self):
        text = "This is a short text."
        result = compact_beat({"beat_id": "b_001", "beat_type": "hook", "text": text})
        assert result["text"] == text

    def test_exactly_20_words_no_ellipsis(self):
        text = " ".join(f"word{i}" for i in range(20))
        result = compact_beat({"beat_id": "b_001", "beat_type": "hook", "text": text})
        assert "…" not in result["text"]
        assert len(result["text"].split()) == 20

    def test_21_words_truncated_with_ellipsis(self):
        text = " ".join(f"word{i}" for i in range(21))
        result = compact_beat({"beat_id": "b_001", "beat_type": "hook", "text": text})
        assert result["text"].endswith("…")
        # 20 words + ellipsis appended directly
        parts = result["text"][:-1].split()  # strip "…" then split
        assert len(parts) == 20

    def test_100_words_truncated_to_20(self):
        text = " ".join(["word"] * 100)
        result = compact_beat({"beat_id": "b_001", "beat_type": "hook", "text": text})
        assert result["text"].endswith("…")
        assert len(result["text"][:-1].split()) == 20

    def test_missing_text_gives_empty_string(self):
        result = compact_beat({"beat_id": "b_001", "beat_type": "hook"})
        assert result["text"] == ""

    def test_missing_beat_id_gives_empty_string(self):
        result = compact_beat({"beat_type": "hook", "text": "Hello."})
        assert result["beat_id"] == ""

    def test_missing_beat_type_gives_empty_string(self):
        result = compact_beat({"beat_id": "b_001", "text": "Hello."})
        assert result["beat_type"] == ""

    def test_returns_only_expected_keys(self):
        result = compact_beat({"beat_id": "b_001", "beat_type": "hook", "text": "Hello.",
                                "extra_field": "should_be_dropped"})
        assert set(result.keys()) == {"beat_id", "beat_type", "text"}


# ---------------------------------------------------------------------------
# TestCompactScriptPackage
# ---------------------------------------------------------------------------

class TestCompactScriptPackage:

    def test_preserves_episode_metadata(self):
        sp = _make_script_package()
        result = compact_script_package(sp)
        assert result["episode_metadata"] == sp["episode_metadata"]

    def test_preserves_section_count(self):
        sp = _make_script_package()
        result = compact_script_package(sp)
        assert len(result["sections"]) == len(sp["sections"])

    def test_preserves_section_ids(self):
        sp = _make_script_package()
        result = compact_script_package(sp)
        assert result["sections"][0]["section_id"] == "sec_001"

    def test_preserves_section_title(self):
        sp = _make_script_package()
        result = compact_script_package(sp)
        assert result["sections"][0]["title"] == "Hook"

    def test_preserves_visual_directions(self):
        sp = _make_script_package()
        result = compact_script_package(sp)
        assert result["sections"][0]["visual_directions"] == "Wide establishing shot."

    def test_beats_are_compacted(self):
        sp = _make_script_package(beats_per_section=1)
        sp["sections"][0]["narration_beats"] = [
            {"beat_id": "b_001", "beat_type": "narration",
             "text": " ".join(["word"] * 30)}
        ]
        result = compact_script_package(sp)
        beat = result["sections"][0]["narration_beats"][0]
        assert beat["text"].endswith("…")

    def test_strip_shorts_false_includes_shorts(self):
        sp = _make_script_package(include_shorts=True)
        result = compact_script_package(sp, strip_shorts=False)
        assert len(result["shorts_scripts"]) == 1

    def test_strip_shorts_true_gives_empty_list(self):
        sp = _make_script_package(include_shorts=True)
        result = compact_script_package(sp, strip_shorts=True)
        assert result["shorts_scripts"] == []

    def test_no_shorts_in_input_gives_empty_list(self):
        sp = _make_script_package(include_shorts=False)
        result = compact_script_package(sp, strip_shorts=False)
        assert result["shorts_scripts"] == []

    def test_shorts_beats_are_compacted(self):
        sp = _make_script_package(include_shorts=True)
        sp["shorts_scripts"][0]["narration_beats"] = [
            {"beat_id": "b_s01", "beat_type": "hook",
             "text": " ".join(["word"] * 25)}
        ]
        result = compact_script_package(sp, strip_shorts=False)
        beat = result["shorts_scripts"][0]["narration_beats"][0]
        assert beat["text"].endswith("…")

    def test_shorts_short_id_preserved(self):
        sp = _make_script_package(include_shorts=True)
        result = compact_script_package(sp, strip_shorts=False)
        assert result["shorts_scripts"][0]["short_id"] == "sh_01"

    def test_shorts_visual_directions_preserved(self):
        sp = _make_script_package(include_shorts=True)
        result = compact_script_package(sp, strip_shorts=False)
        assert result["shorts_scripts"][0]["visual_directions"] == "Close-up."

    def test_multiple_beats_per_section(self):
        sp = _make_script_package(beats_per_section=5)
        result = compact_script_package(sp)
        assert len(result["sections"][0]["narration_beats"]) == 5


# ---------------------------------------------------------------------------
# TestBuildScenePlanInput
# ---------------------------------------------------------------------------

class TestBuildScenePlanInput:

    def test_task_field_present(self):
        sp = _make_script_package()
        cfg = make_config()
        result = build_scene_plan_input(sp, cfg, "ep_001")
        assert result["task"] == "plan_visual_package"

    def test_provider_capabilities_included(self):
        sp = _make_script_package()
        cfg = make_config()
        result = build_scene_plan_input(sp, cfg, "ep_001")
        assert "provider_capabilities" in result
        assert result["provider_capabilities"] == PROVIDER_CAPABILITIES

    def test_script_package_compacted(self):
        sp = _make_script_package()
        sp["sections"][0]["narration_beats"] = [
            {"beat_id": "b_001", "beat_type": "narration",
             "text": " ".join(["word"] * 30)}
        ]
        cfg = make_config()
        result = build_scene_plan_input(sp, cfg, "ep_001")
        beat = result["script_package"]["sections"][0]["narration_beats"][0]
        assert beat["text"].endswith("…")

    # --- Normal episode (no episode zero, shorts enabled) ---

    def test_normal_episode_no_generation_constraints(self):
        sp = _make_script_package()
        cfg = make_config(enable_shorts=True)
        result = build_scene_plan_input(sp, cfg, "ep_001")
        assert "generation_constraints" not in result

    def test_normal_episode_shorts_included(self):
        sp = _make_script_package(include_shorts=True)
        cfg = make_config(enable_shorts=True)
        result = build_scene_plan_input(sp, cfg, "ep_001")
        # shorts not stripped — they remain in compact script_package
        assert len(result["script_package"]["shorts_scripts"]) == 1

    # --- Episode Zero: ep_000 ---

    def test_ep_000_is_episode_zero(self):
        sp = _make_script_package()
        cfg = make_config()
        result = build_scene_plan_input(sp, cfg, "ep_000")
        constraints = result.get("generation_constraints", {})
        assert constraints.get("shorts_count") == 0

    def test_ep_000_constraints_have_mode_validation(self):
        sp = _make_script_package()
        cfg = make_config()
        result = build_scene_plan_input(sp, cfg, "ep_000")
        assert result["generation_constraints"]["mode"] == "validation"

    def test_ep_000_constraints_have_max_main_scenes_default_5(self):
        sp = _make_script_package()
        cfg = make_config()  # EPISODE_ZERO_MAX_SCENES=0 → default 5
        result = build_scene_plan_input(sp, cfg, "ep_000")
        assert result["generation_constraints"]["max_main_scenes"] == 5

    def test_ep_000_shorts_stripped_from_script_package(self):
        sp = _make_script_package(include_shorts=True)
        cfg = make_config()
        result = build_scene_plan_input(sp, cfg, "ep_000")
        assert result["script_package"]["shorts_scripts"] == []

    # --- Episode Zero: EPISODE_ZERO_MAX_SCENES > 0 ---

    def test_episode_zero_max_scenes_triggers_constraints(self):
        sp = _make_script_package()
        cfg = make_config(episode_zero_max_scenes=3, enable_shorts=True)
        result = build_scene_plan_input(sp, cfg, "ep_001")
        constraints = result.get("generation_constraints", {})
        assert constraints.get("shorts_count") == 0
        assert constraints.get("max_main_scenes") == 3

    def test_episode_zero_max_scenes_used_in_constraint(self):
        sp = _make_script_package()
        cfg = make_config(episode_zero_max_scenes=7)
        result = build_scene_plan_input(sp, cfg, "ep_001")
        assert result["generation_constraints"]["max_main_scenes"] == 7

    def test_episode_zero_max_scenes_strips_shorts(self):
        sp = _make_script_package(include_shorts=True)
        cfg = make_config(episode_zero_max_scenes=5)
        result = build_scene_plan_input(sp, cfg, "ep_001")
        assert result["script_package"]["shorts_scripts"] == []

    # --- Dedicated shorts disabled ---

    def test_shorts_disabled_adds_constraints_shorts_count_zero(self):
        sp = _make_script_package()
        cfg = make_config(enable_shorts=False)
        result = build_scene_plan_input(sp, cfg, "ep_001")
        constraints = result.get("generation_constraints", {})
        assert constraints.get("shorts_count") == 0

    def test_shorts_disabled_no_mode_validation(self):
        sp = _make_script_package()
        cfg = make_config(enable_shorts=False)
        result = build_scene_plan_input(sp, cfg, "ep_001")
        assert "mode" not in result.get("generation_constraints", {})

    def test_shorts_disabled_strips_shorts_from_script_package(self):
        sp = _make_script_package(include_shorts=True)
        cfg = make_config(enable_shorts=False)
        result = build_scene_plan_input(sp, cfg, "ep_001")
        assert result["script_package"]["shorts_scripts"] == []

    def test_episode_zero_takes_priority_over_shorts_disabled(self):
        """ep_000 with flag=false still gets mode=validation (ep_zero wins)."""
        sp = _make_script_package()
        cfg = make_config(enable_shorts=False)
        result = build_scene_plan_input(sp, cfg, "ep_000")
        assert result["generation_constraints"]["mode"] == "validation"

    def test_episode_metadata_preserved_in_compact(self):
        sp = _make_script_package(episode_id="ep_042")
        cfg = make_config()
        result = build_scene_plan_input(sp, cfg, "ep_042")
        assert result["script_package"]["episode_metadata"]["episode_id"] == "ep_042"


# ---------------------------------------------------------------------------
# TestOrderAndFilterScenes
# ---------------------------------------------------------------------------

class TestOrderAndFilterScenes:

    def _make_log(self):
        return MagicMock()

    def test_empty_scenes_returns_empty(self):
        cfg = make_config()
        result = order_and_filter_scenes([], cfg, self._make_log())
        assert result == []

    def test_sorts_by_scene_index_ascending(self):
        scenes = [
            _make_scene("s03", 2),
            _make_scene("s01", 0),
            _make_scene("s02", 1),
        ]
        cfg = make_config(enable_shorts=True)
        result = order_and_filter_scenes(scenes, cfg, self._make_log())
        assert [s["scene_id"] for s in result] == ["s01", "s02", "s03"]

    def test_already_sorted_scenes_unchanged(self):
        scenes = [_make_scene(f"s0{i}", i) for i in range(4)]
        cfg = make_config(enable_shorts=True)
        result = order_and_filter_scenes(scenes, cfg, self._make_log())
        assert [s["scene_id"] for s in result] == ["s00", "s01", "s02", "s03"]

    def test_scenes_missing_index_default_to_zero(self):
        scenes = [
            {"scene_id": "s_no_index", "source_type": "main_episode"},
            _make_scene("s01", 1),
        ]
        cfg = make_config(enable_shorts=True)
        result = order_and_filter_scenes(scenes, cfg, self._make_log())
        # Both index 0 and 1 — s_no_index comes first or tied (stable sort)
        assert len(result) == 2

    # --- Shorts filtering ---

    def test_short_scenes_filtered_when_disabled(self):
        scenes = [
            _make_scene("s01", 0, "main_episode"),
            _make_scene("sh_01_s01", 1, "short"),
        ]
        cfg = make_config(enable_shorts=False)
        result = order_and_filter_scenes(scenes, cfg, self._make_log())
        scene_ids = [s["scene_id"] for s in result]
        assert "sh_01_s01" not in scene_ids
        assert "s01" in scene_ids

    def test_short_scenes_preserved_when_enabled(self):
        scenes = [
            _make_scene("s01", 0, "main_episode"),
            _make_scene("sh_01_s01", 1, "short"),
        ]
        cfg = make_config(enable_shorts=True)
        result = order_and_filter_scenes(scenes, cfg, self._make_log())
        scene_ids = [s["scene_id"] for s in result]
        assert "sh_01_s01" in scene_ids
        assert "s01" in scene_ids

    def test_all_main_scenes_preserved_when_shorts_disabled(self):
        scenes = [_make_scene(f"s0{i}", i, "main_episode") for i in range(5)]
        scenes.append(_make_scene("sh_01_s01", 5, "short"))
        cfg = make_config(enable_shorts=False)
        result = order_and_filter_scenes(scenes, cfg, self._make_log())
        assert len(result) == 5

    def test_warn_logged_when_short_scenes_blocked(self):
        scenes = [
            _make_scene("s01", 0, "main_episode"),
            _make_scene("sh_01_s01", 1, "short"),
        ]
        cfg = make_config(enable_shorts=False)
        log = self._make_log()
        order_and_filter_scenes(scenes, cfg, log)
        log.warn.assert_called_once()
        call_args = log.warn.call_args
        assert call_args[0][0] == "vpd_short_scenes_blocked"

    def test_no_warn_when_no_short_scenes(self):
        scenes = [_make_scene("s01", 0, "main_episode")]
        cfg = make_config(enable_shorts=False)
        log = self._make_log()
        order_and_filter_scenes(scenes, cfg, log)
        log.warn.assert_not_called()

    # --- Episode Zero scene cap ---

    def test_scene_cap_applied(self):
        scenes = [_make_scene(f"s0{i}", i) for i in range(10)]
        cfg = make_config(episode_zero_max_scenes=3, enable_shorts=True)
        result = order_and_filter_scenes(scenes, cfg, self._make_log())
        assert len(result) == 3

    def test_scene_cap_takes_first_n_by_index(self):
        scenes = [_make_scene(f"s0{i}", i) for i in range(10)]
        cfg = make_config(episode_zero_max_scenes=3, enable_shorts=True)
        result = order_and_filter_scenes(scenes, cfg, self._make_log())
        assert [s["scene_id"] for s in result] == ["s00", "s01", "s02"]

    def test_no_cap_when_episode_zero_max_scenes_zero(self):
        scenes = [_make_scene(f"s0{i}", i) for i in range(10)]
        cfg = make_config(episode_zero_max_scenes=0, enable_shorts=True)
        result = order_and_filter_scenes(scenes, cfg, self._make_log())
        assert len(result) == 10

    def test_cap_warn_logged(self):
        scenes = [_make_scene(f"s0{i}", i) for i in range(10)]
        cfg = make_config(episode_zero_max_scenes=3, enable_shorts=True)
        log = self._make_log()
        order_and_filter_scenes(scenes, cfg, log)
        log.warn.assert_called_once()
        call_args = log.warn.call_args
        assert call_args[0][0] == "vpd_scene_cap_applied"

    def test_cap_not_applied_when_under_limit(self):
        scenes = [_make_scene(f"s0{i}", i) for i in range(3)]
        cfg = make_config(episode_zero_max_scenes=5, enable_shorts=True)
        log = self._make_log()
        result = order_and_filter_scenes(scenes, cfg, log)
        assert len(result) == 3
        log.warn.assert_not_called()

    def test_shorts_filtered_before_cap_applied(self):
        """Cap applied after shorts are removed — cap is on the final deliverable."""
        main_scenes = [_make_scene(f"s0{i}", i, "main_episode") for i in range(5)]
        short_scenes = [_make_scene(f"sh_0{i}_s01", i + 10, "short") for i in range(5)]
        cfg = make_config(episode_zero_max_scenes=4, enable_shorts=False)
        result = order_and_filter_scenes(main_scenes + short_scenes, cfg, self._make_log())
        # Shorts filtered out → 5 main scenes → cap to 4
        assert len(result) == 4
        assert all(s["source_type"] == "main_episode" for s in result)

    # --- Return type ---

    def test_returns_list(self):
        cfg = make_config(enable_shorts=True)
        result = order_and_filter_scenes([], cfg, self._make_log())
        assert isinstance(result, list)

    def test_does_not_mutate_input(self):
        scenes = [_make_scene(f"s0{i}", i) for i in range(5)]
        original_ids = [s["scene_id"] for s in scenes]
        cfg = make_config(episode_zero_max_scenes=3, enable_shorts=True)
        order_and_filter_scenes(scenes, cfg, self._make_log())
        assert [s["scene_id"] for s in scenes] == original_ids
