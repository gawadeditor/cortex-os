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

import pytest

from hypogeum.video_prompt_writer import (
    CoveragePlanSyncError,
    compile_shot_prompt,
    render_coverage_plan_shot_prompts,
    sync_coverage_plan_shots_from_standalone_plan,
)


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


def _make_standalone_plan(beat_id: str, shots: list) -> dict:
    return {
        "episode_id": "ep_test",
        "beat_plans": [{
            "beat_id": beat_id,
            "narration_duration_seconds": 46.858,
            "progression_pattern": "establish_develop_reveal",
            "shots": shots,
        }],
    }


def _b047_shot_fixtures() -> list:
    """Field values mirroring the real b_047 shot1/shot2/shot3 specs from
    ep_f3619022's Coverage Plan -- used to prove the three shots compile to
    materially distinct prompts, exactly as they must in production."""
    return [
        _make_shot(1, beat_id="b_047", shot_purpose="action_beat",
                   narrative_function="Introduces the tunnel passage as a narrowing corridor.",
                   subject="erdstall tunnel narrowing toward schlupf constriction",
                   action="camera creeps slowly forward through earthen passage",
                   shot_scale="medium_wide", camera_angle="eye_level",
                   camera_movement="slow_push_in", composition="diagonal_leading_line",
                   background_depth="deep_layered", screen_direction="away_from_camera",
                   lighting_direction="single torch glow beyond constriction"),
        _make_shot(2, beat_id="b_047", shot_purpose="transition_beat",
                   narrative_function="Delivers the climactic threshold by halting the camera at the schlupf.",
                   subject="schlupf constriction point as sacred threshold",
                   action="camera holds locked at the narrowing",
                   shot_scale="medium_close_up", camera_angle="eye_level",
                   camera_movement="locked_off_hold", composition="centered_symmetry",
                   background_depth="shallow", screen_direction="neutral",
                   lighting_direction="torch glow from beyond the constriction"),
        _make_shot(3, beat_id="b_047", shot_purpose="atmosphere_detail",
                   narrative_function="Delivers the payoff scale -- the void itself.",
                   subject="the darkest point beyond the schlupf constriction",
                   action="static hold, torch rim-light flickering",
                   shot_scale="extreme_close_up", camera_angle="low_angle",
                   camera_movement="controlled_slow_push_in", composition="negative_space_dominant",
                   background_depth="deep", screen_direction="neutral",
                   lighting_direction="torch rim-light on the aperture edge"),
    ]


class TestCompileShotPrompt:
    """compile_shot_prompt() -- the single-shot compiler API, tested in
    isolation from the merge/sync boundary below."""

    def test_missing_required_field_raises_explicit_local_error(self):
        shot = _make_shot(1)
        del shot["shot_scale"]
        scene = _make_scene(shots=[])
        with pytest.raises(CoveragePlanSyncError, match="shot_scale"):
            compile_shot_prompt(scene, shot)

    def test_identical_input_produces_identical_output(self):
        scene = _make_scene(shots=[])
        shot_a = _make_shot(1, camera_angle="low_angle")
        shot_b = _make_shot(1, camera_angle="low_angle")
        result_a = compile_shot_prompt(scene, shot_a)
        result_b = compile_shot_prompt(scene, shot_b)
        assert result_a["primary_prompt"] == result_b["primary_prompt"]
        assert result_a["prompt_checksum"] == result_b["prompt_checksum"]

    def test_shot_scale_change_appears_in_rendered_prompt(self):
        scene = _make_scene(shots=[])
        wide = compile_shot_prompt(scene, _make_shot(1, shot_scale="wide_establishing"))
        close = compile_shot_prompt(scene, _make_shot(1, shot_scale="extreme_close_up"))
        assert "wide_establishing" in wide["primary_prompt"]
        assert "extreme_close_up" in close["primary_prompt"]
        assert wide["primary_prompt"] != close["primary_prompt"]

    def test_camera_angle_movement_composition_changes_appear_in_prompt(self):
        scene = _make_scene(shots=[])
        a = compile_shot_prompt(scene, _make_shot(
            1, camera_angle="low_angle", camera_movement="lateral_dolly_left",
            composition="negative_space_dominant"))
        b = compile_shot_prompt(scene, _make_shot(
            1, camera_angle="high_angle", camera_movement="handheld_drift",
            composition="layered_depth"))
        for field, val in [("camera_angle", "low_angle"), ("camera_movement", "lateral_dolly_left"),
                            ("composition", "negative_space_dominant")]:
            assert val in a["primary_prompt"], field
        for field, val in [("camera_angle", "high_angle"), ("camera_movement", "handheld_drift"),
                            ("composition", "layered_depth")]:
            assert val in b["primary_prompt"], field

    def test_channel_context_comes_from_scene_not_hardcoded(self):
        """The base prompt (channel/episode identity) is read from the
        scene's own production_spec.provider_routing -- runtime context --
        never a constant baked into this module."""
        scene_a = _make_scene(base_primary="CHANNEL_A_IDENTITY_TEXT", shots=[])
        scene_b = _make_scene(base_primary="CHANNEL_B_IDENTITY_TEXT", shots=[])
        shot = _make_shot(1)
        result_a = compile_shot_prompt(scene_a, shot)
        result_b = compile_shot_prompt(scene_b, shot)
        assert result_a["primary_prompt"].startswith("CHANNEL_A_IDENTITY_TEXT")
        assert result_b["primary_prompt"].startswith("CHANNEL_B_IDENTITY_TEXT")
        assert result_a["primary_prompt"] != result_b["primary_prompt"]

    def test_returns_structured_evidence_of_compiled_fields(self):
        scene = _make_scene(shots=[])
        result = compile_shot_prompt(scene, _make_shot(1))
        assert result["shot_id"] == "b_029__shot1"
        assert "shot_scale" in result["compiled_fields"]
        assert "camera_angle" in result["compiled_fields"]
        assert result["prompt_checksum"]

    def test_b047_shot1_shot2_shot3_render_materially_distinct(self):
        scene = _make_scene(scene_id="sh_short_003_s05", shots=[])
        shot1, shot2, shot3 = _b047_shot_fixtures()
        p1 = compile_shot_prompt(scene, shot1)["primary_prompt"]
        p2 = compile_shot_prompt(scene, shot2)["primary_prompt"]
        p3 = compile_shot_prompt(scene, shot3)["primary_prompt"]
        assert len({p1, p2, p3}) == 3
        assert "medium_wide" in p1 and "medium_close_up" in p2 and "extreme_close_up" in p3
        assert "locked_off_hold" in p2 and "locked_off_hold" not in p1 and "locked_off_hold" not in p3
        assert "centered_symmetry" in p2
        assert "sacred threshold" in p2


class TestSyncCoveragePlanShotsFromStandalonePlan:
    """sync_coverage_plan_shots_from_standalone_plan() -- the standalone
    plan document -> embedded per-scene copy bridge that clip_repair_agent
    actually consumes."""

    def _visual_package(self, scene_id="sh_short_003_s05", beat_id="b_047",
                         base_primary="A schlupf constriction, torchlit erdstall tunnel."):
        # Pre-existing shot1 must match the standalone plan's own shot1
        # fixture exactly -- a real sync never finds a legitimate shot1/
        # shot1 conflict, since both sides describe the same already-
        # decided shot. The conflict-detection tests below use their own,
        # deliberately different fixtures.
        preexisting_shot1 = dict(_b047_shot_fixtures()[0])
        scene_stub = {"production_spec": {"provider_routing": {
            "primary_prompt": base_primary, "fallback_prompt": base_primary,
        }}}
        # Pre-render shot1's prompt the same way a real prior VPD/sync pass
        # would have -- so this fixture is a realistic "already synced"
        # state, not an arbitrary sentinel a deterministic re-render could
        # never reproduce.
        rendered = compile_shot_prompt(scene_stub, preexisting_shot1)
        preexisting_shot1["primary_prompt"] = rendered["primary_prompt"]
        preexisting_shot1["fallback_prompt"] = rendered["fallback_prompt"]
        return {
            "scenes": [{
                "scene_id": scene_id,
                "narration_reference": beat_id,
                "production_spec": {"provider_routing": {
                    "primary_prompt": base_primary, "fallback_prompt": base_primary,
                }},
                "cinematic_coverage_plan": {
                    "beat_id": beat_id, "narration_duration_seconds": 11.807,
                    "progression_pattern": "establish_develop_reveal",
                    "shots": [preexisting_shot1],
                },
            }],
        }

    def test_merges_only_new_shots_and_renders_their_prompts(self):
        vp = self._visual_package()
        shot1, shot2, shot3 = _b047_shot_fixtures()
        standalone = _make_standalone_plan("b_047", [shot1, shot2, shot3])

        evidence = sync_coverage_plan_shots_from_standalone_plan(vp, standalone)

        embedded_ids = {s["shot_id"] for s in vp["scenes"][0]["cinematic_coverage_plan"]["shots"]}
        assert embedded_ids == {"b_047__shot1", "b_047__shot2", "b_047__shot3"}
        # shot1 pre-existed (from the visual_package fixture) -- only 2/3 were merged
        assert set(evidence["shots_merged"]) == {"b_047__shot2", "b_047__shot3"}
        assert evidence["scenes_touched"] == ["sh_short_003_s05"]
        assert set(evidence["shots_rendered"]) == {"b_047__shot2", "b_047__shot3"}

    def test_merged_shots_get_distinct_rendered_prompts(self):
        vp = self._visual_package()
        shot1, shot2, shot3 = _b047_shot_fixtures()
        standalone = _make_standalone_plan("b_047", [shot1, shot2, shot3])
        sync_coverage_plan_shots_from_standalone_plan(vp, standalone)

        by_id = {s["shot_id"]: s for s in vp["scenes"][0]["cinematic_coverage_plan"]["shots"]}
        prompts = {sid: s["primary_prompt"] for sid, s in by_id.items()}
        assert len(set(prompts.values())) == 3
        assert "medium_close_up" in prompts["b_047__shot2"]
        assert "locked_off_hold" in prompts["b_047__shot2"]
        assert "centered_symmetry" in prompts["b_047__shot2"]
        assert "extreme_close_up" in prompts["b_047__shot3"]

    def test_existing_shot_not_collapsed_onto_shared_base_prompt(self):
        """Requirement: existing scene-level prompts are not copied
        unchanged across planned shots -- after a sync, shot1's prompt
        still reflects shot1's own fields (re-rendering is idempotent, so
        it reproduces the same text a real pre-existing prompt already
        had) and is never made identical to shot2's or shot3's."""
        vp = self._visual_package()
        pre_sync_shot1_prompt = vp["scenes"][0]["cinematic_coverage_plan"]["shots"][0]["primary_prompt"]
        shot1, shot2, shot3 = _b047_shot_fixtures()
        standalone = _make_standalone_plan("b_047", [shot1, shot2, shot3])

        sync_coverage_plan_shots_from_standalone_plan(vp, standalone)

        by_id = {s["shot_id"]: s for s in vp["scenes"][0]["cinematic_coverage_plan"]["shots"]}
        # deterministic re-render of an unchanged shot reproduces the same text
        assert by_id["b_047__shot1"]["primary_prompt"] == pre_sync_shot1_prompt
        assert by_id["b_047__shot2"]["primary_prompt"] != by_id["b_047__shot1"]["primary_prompt"]
        assert by_id["b_047__shot3"]["primary_prompt"] != by_id["b_047__shot1"]["primary_prompt"]
        assert by_id["b_047__shot2"]["primary_prompt"] != by_id["b_047__shot3"]["primary_prompt"]

    def test_missing_scene_for_beat_raises_explicit_error(self):
        vp = {"scenes": []}
        standalone = _make_standalone_plan("b_999", [_make_shot(1, beat_id="b_999")])
        with pytest.raises(CoveragePlanSyncError, match="b_999"):
            sync_coverage_plan_shots_from_standalone_plan(vp, standalone)

    def test_incomplete_incoming_shot_raises_before_any_merge(self):
        vp = self._visual_package()
        bad_shot = _make_shot(2, beat_id="b_047")
        del bad_shot["camera_angle"]
        standalone = _make_standalone_plan("b_047", [bad_shot])
        with pytest.raises(CoveragePlanSyncError, match="camera_angle"):
            sync_coverage_plan_shots_from_standalone_plan(vp, standalone)
        # nothing partially merged
        assert len(vp["scenes"][0]["cinematic_coverage_plan"]["shots"]) == 1

    def test_contradictory_existing_shot_raises_explicit_error(self):
        vp = self._visual_package()
        contradictory_shot1 = _make_shot(1, beat_id="b_047", shot_scale="extreme_close_up")
        standalone = _make_standalone_plan("b_047", [contradictory_shot1])
        with pytest.raises(CoveragePlanSyncError, match="shot_scale"):
            sync_coverage_plan_shots_from_standalone_plan(vp, standalone)

    def test_beat_ids_scope_restricts_sync_to_authorized_beats_only(self):
        """Proves an authorization-manifest-scoped run never touches an
        unauthorized beat's shots."""
        vp = {"scenes": [
            self._visual_package()["scenes"][0],
            {"scene_id": "s_other", "narration_reference": "b_999",
             "production_spec": {"provider_routing": {"primary_prompt": "X", "fallback_prompt": "X"}},
             "cinematic_coverage_plan": {"beat_id": "b_999", "narration_duration_seconds": 8.0,
                                          "progression_pattern": "p", "shots": [_make_shot(1, beat_id="b_999")]}},
        ]}
        shot1, shot2, shot3 = _b047_shot_fixtures()
        standalone = {
            "episode_id": "ep_test",
            "beat_plans": [
                {"beat_id": "b_047", "narration_duration_seconds": 11.807,
                 "progression_pattern": "p", "shots": [shot1, shot2, shot3]},
                {"beat_id": "b_999", "narration_duration_seconds": 8.0,
                 "progression_pattern": "p", "shots": [_make_shot(1, beat_id="b_999"),
                                                         _make_shot(2, beat_id="b_999")]},
            ],
        }

        evidence = sync_coverage_plan_shots_from_standalone_plan(
            vp, standalone, beat_ids={"b_047"}
        )

        assert evidence["scenes_touched"] == ["sh_short_003_s05"]
        other_scene = next(s for s in vp["scenes"] if s["scene_id"] == "s_other")
        assert len(other_scene["cinematic_coverage_plan"]["shots"]) == 1  # untouched
