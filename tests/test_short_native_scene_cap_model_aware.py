"""Short-native scene cap scales with the channel model's real clip length.

The default cap (SHORT_NATIVE_MAX_SCENES=8) is sized for 8s clips = a full
~45-60s short. A channel on a 5s-clip model (seedance, adopted via the
bake-off) needs proportionally MORE scenes to hold the whole story; with the
default 8 the ending beats get no video and the short is visibly cut off
(the ep_twinkle_sep08 symptom)."""

from __future__ import annotations

import math

from hypogeum.config import Config


def _cfg():
    c = Config()
    c.SHORT_NATIVE_MAX_SCENES = 8
    c.EPISODE_ZERO_MAX_SCENES = 0
    return c


def test_preset_without_clip_seconds_keeps_default_cap():
    c = _cfg()
    c.apply_short_native_preset()
    assert c.EPISODE_ZERO_MAX_SCENES == 8


def test_preset_scales_cap_up_for_short_clip_model():
    c = _cfg()
    c.apply_short_native_preset(clip_seconds=5.04)
    # ceil(8 * 8 / 5.04) == 13
    assert c.EPISODE_ZERO_MAX_SCENES == math.ceil(8 * 8 / 5.04) == 13


def test_preset_does_not_scale_for_8s_or_longer_clips():
    c = _cfg()
    c.apply_short_native_preset(clip_seconds=8.0)
    assert c.EPISODE_ZERO_MAX_SCENES == 8
    c2 = _cfg()
    c2.apply_short_native_preset(clip_seconds=10.0)
    assert c2.EPISODE_ZERO_MAX_SCENES == 8


def test_scene_planner_honours_the_scaled_cap():
    from hypogeum.agents.scene_planner import build_scene_plan_input

    c = _cfg()
    c.apply_short_native_preset(clip_seconds=5.04)  # -> EPISODE_ZERO_MAX_SCENES = 13
    c.ENABLE_DEDICATED_SHORT_GENERATION = False

    # a 10-beat script (what the model-aware beat sizing produces for seedance)
    sp = {"sections": [{"section_id": "s1", "narration_beats": [
        {"beat_id": f"b_{i:03d}", "text": "a few words here."} for i in range(1, 11)
    ]}], "shorts_scripts": []}

    got = build_scene_plan_input(sp, c, "ep_x", channel_id="twinkle")
    # all 10 beats get a scene — not silently re-capped at 8
    assert got["generation_constraints"]["max_main_scenes"] == 10


def test_scene_planner_still_honours_a_tight_validation_cap():
    from hypogeum.agents.scene_planner import build_scene_plan_input

    c = _cfg()
    c.SHORT_NATIVE_MODE = True
    c.EPISODE_ZERO_MAX_SCENES = 3          # genuine Episode-Zero validation
    c.ENABLE_DEDICATED_SHORT_GENERATION = False
    sp = {"sections": [{"section_id": "s1", "narration_beats": [
        {"beat_id": f"b_{i:03d}", "text": "x"} for i in range(1, 11)
    ]}], "shorts_scripts": []}
    got = build_scene_plan_input(sp, c, "ep_x", channel_id="twinkle")
    assert got["generation_constraints"]["max_main_scenes"] == 3
