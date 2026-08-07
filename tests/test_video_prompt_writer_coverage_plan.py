"""
tests/test_video_prompt_writer_coverage_plan.py — Unit tests for
hypogeum.video_prompt_writer.render_coverage_plan_shot_prompts() (2026-08-07)

Architecture (see hypogeum/agents/clip_repair_agent.py's ownership note and
hypogeum/agents/cinematic_director.py's module docstring addendum):
    Cinematic Director   — decides the ordered planned shots (shot IDENTITY:
                            camera_angle, shot_scale, composition, etc.)
    Video Prompt Writer   — renders each planned shot's fields into a fully
                            rendered prompt (THIS module, THIS function)
    clip_repair_agent     — only ever SELECTS one already-rendered prompt

This file covers rendering in isolation from both Cinematic Director's
planning (see tests/test_cinematic_director.py for shot_diversity_key() /
validate_coverage_plan_diversity()) and clip_repair_agent's selection (see
tests/test_clip_repair_agent.py's TestCoverageFillShotDiversity). Zero
provider/LLM calls anywhere in this file — rendering is deterministic.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.video_prompt_writer import render_coverage_plan_shot_prompts


def _make_scene(
    scene_id: str = "s29",
    base_primary: str = "A sealed medieval tunnel, candlelight.",
    base_fallback: str = "A sealed medieval tunnel.",
    shots: list | None = None,
) -> dict:
    return {
        "scene_id": scene_id,
        "production_spec": {
            "provider_routing": {
                "primary_prompt": base_primary,
                "fallback_prompt": base_fallback,
            },
        },
        "cinematic_coverage_plan": {
            "beat_id": "b_029",
            "narration_duration_seconds": 46.858,
            "progression_pattern": "establish_develop_reveal",
            "shots": shots if shots is not None else [],
        },
    }


def _make_shot(shot_order: int, beat_id: str = "b_029", **overrides) -> dict:
    shot = {
        "shot_id": f"{beat_id}__shot{shot_order}",
        "parent_beat_id": beat_id,
        "shot_order": shot_order,
        "shot_purpose": "establish_location",
        "narrative_function": "establishes the tunnel's scale",
        "estimated_usable_duration": 8.0,
        "subject": "the sealed passage",
        "action": "",
        "framing": "",
        "shot_scale": "medium",
        "camera_angle": "eye_level",
        "camera_height": "eye_level",
        "camera_direction": "frontal",
        "camera_movement": "slow_push_in",
        "lens_character": "standard",
        "composition": "rule_of_thirds_left",
        "foreground_usage": "none",
        "background_depth": "medium",
        "lighting_direction": "",
        "screen_direction": "neutral",
        "eye_line": "neutral",
        "motion_energy": "low",
        "transition_in": "clean_cut",
        "transition_out": "clean_cut",
        "diversity_justification": None,
    }
    shot.update(overrides)
    return shot


class TestRenderCoveragePlanShotPrompts:

    def test_no_plan_is_a_noop(self):
        scene = {"scene_id": "s01", "production_spec": {"provider_routing": {
            "primary_prompt": "BASE", "fallback_prompt": "BASE_FALLBACK",
        }}}
        render_coverage_plan_shot_prompts(scene)  # must not raise
        assert "cinematic_coverage_plan" not in scene

    def test_empty_shots_list_is_a_noop(self):
        scene = _make_scene(shots=[])
        render_coverage_plan_shot_prompts(scene)
        assert scene["cinematic_coverage_plan"]["shots"] == []

    def test_every_shot_gets_primary_and_fallback_prompt(self):
        shots = [_make_shot(1), _make_shot(2, camera_angle="low_angle")]
        scene = _make_scene(shots=shots)
        render_coverage_plan_shot_prompts(scene)
        for shot in scene["cinematic_coverage_plan"]["shots"]:
            assert shot["primary_prompt"]
            assert shot["fallback_prompt"]

    def test_rendered_prompts_are_materially_distinct_per_shot(self):
        """The real s29 case: shots differing only in camera_angle/
        shot_scale/composition/camera_movement/narrative_function must
        render to different text -- real field values differ, not
        synonym-shuffled wording."""
        shots = [
            _make_shot(1, camera_angle="eye_level", shot_scale="wide_establishing",
                       composition="rule_of_thirds_left", camera_movement="slow_push_in",
                       narrative_function="establishes the tunnel's scale"),
            _make_shot(2, camera_angle="low_angle", shot_scale="medium",
                       composition="centered_symmetry", camera_movement="lateral_dolly_left",
                       narrative_function="reveals the ceiling's construction"),
            _make_shot(3, camera_angle="high_angle", shot_scale="close_up",
                       composition="negative_space_dominant", camera_movement="overhead_descent",
                       narrative_function="shows tool marks on the wall"),
            _make_shot(4, camera_angle="dutch_tilt", shot_scale="medium_close_up",
                       composition="diagonal_leading_line", camera_movement="handheld_drift",
                       narrative_function="conveys disorientation in the passage"),
            _make_shot(5, camera_angle="worms_eye", shot_scale="extreme_close_up",
                       composition="layered_depth", camera_movement="vertical_ascent",
                       narrative_function="finds the passage's terminus"),
        ]
        scene = _make_scene(shots=shots)
        render_coverage_plan_shot_prompts(scene)

        rendered = scene["cinematic_coverage_plan"]["shots"]
        primary_prompts = [s["primary_prompt"] for s in rendered]
        assert len(set(primary_prompts)) == 5, "every shot must render to distinct text"

        for shot, prompt in zip(shots, primary_prompts):
            assert shot["camera_angle"] in prompt
            assert shot["shot_scale"] in prompt
            assert shot["composition"] in prompt
            assert shot["camera_movement"] in prompt
            assert shot["narrative_function"] in prompt

    def test_base_prompt_continuity_is_preserved(self):
        """Appending (never replacing) the base prompt is what preserves
        subject/setting/period continuity across every shot in a beat."""
        shots = [_make_shot(1), _make_shot(2, camera_angle="low_angle")]
        scene = _make_scene(
            base_primary="A sealed medieval tunnel, candlelight.",
            base_fallback="A sealed medieval tunnel.",
            shots=shots,
        )
        render_coverage_plan_shot_prompts(scene)

        for shot in scene["cinematic_coverage_plan"]["shots"]:
            assert shot["primary_prompt"].startswith("A sealed medieval tunnel, candlelight.")
            assert shot["fallback_prompt"].startswith("A sealed medieval tunnel.")

    def test_empty_base_prompt_falls_back_to_directive_only(self):
        shots = [_make_shot(1, camera_angle="low_angle")]
        scene = _make_scene(base_primary="", base_fallback="", shots=shots)
        render_coverage_plan_shot_prompts(scene)

        shot = scene["cinematic_coverage_plan"]["shots"][0]
        assert shot["primary_prompt"]  # non-empty
        assert not shot["primary_prompt"].startswith(" ")

    def test_does_not_mutate_scene_base_prompt(self):
        shots = [_make_shot(1)]
        scene = _make_scene(base_primary="BASE_TEXT", base_fallback="BASE_FALLBACK", shots=shots)
        render_coverage_plan_shot_prompts(scene)

        routing = scene["production_spec"]["provider_routing"]
        assert routing["primary_prompt"] == "BASE_TEXT"
        assert routing["fallback_prompt"] == "BASE_FALLBACK"

    def test_idempotent_across_repeated_calls(self):
        """Deterministic and safe to call unconditionally on every VPD run
        (resumed or not) -- re-rendering must reproduce identical text."""
        shots = [_make_shot(1, camera_angle="low_angle")]
        scene = _make_scene(shots=shots)
        render_coverage_plan_shot_prompts(scene)
        first_pass = scene["cinematic_coverage_plan"]["shots"][0]["primary_prompt"]

        render_coverage_plan_shot_prompts(scene)
        second_pass = scene["cinematic_coverage_plan"]["shots"][0]["primary_prompt"]

        assert first_pass == second_pass
