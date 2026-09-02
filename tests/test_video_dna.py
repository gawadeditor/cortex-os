"""
tests/test_video_dna.py — Hypogeum Video DNA v1 tests (2026-07-14, revised
for the Prompt Writer architecture revision — see
hypogeum/video_dna.py, hypogeum/brand/visual_identity.py,
hypogeum/video_prompt_writer.py module docstrings)

Covers hypogeum.video_dna and its wiring into
hypogeum.muapi.submit_scene() via hypogeum.video_prompt_writer.
Zero provider calls anywhere in this file.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hypogeum.video_dna import (
    get_video_dna_rules, describe_video_dna_constraints,
    get_video_dna_forbidden_items, get_priority_hierarchy,
)

# "materials" and "color_hierarchy" were retired as distinct Video DNA
# dimensions in the 2026-07-14 Prompt Writer architecture revision — both
# are now exclusively owned by hypogeum.brand.visual_identity's adaptive
# directive (material character / color philosophy), per the "each concept
# has exactly one owner" rule. fine_detail_preservation is new.
#
# movement_and_pacing was extracted to hypogeum.motion_dna in Phase B2.4,
# camera_language was extracted to hypogeum.camera_dna in Phase B2.5,
# lighting was extracted to hypogeum.lighting_dna in Phase B2.6, and
# archaeological_realism was extracted to hypogeum.historical_dna in Phase
# B2.7 (all deleted here, not copied — see TestMovementAndPacingExtracted,
# TestCameraLanguageExtracted, TestLightingExtracted, and
# TestArchaeologicalRealismExtracted below for the absence proofs the
# frozen contract requires).
_REQUIRED_DIMENSIONS = (
    "fine_detail_preservation", "continuity", "subject_hierarchy",
    "forbidden_visual_styles", "scene_transitions",
)


class TestVideoDnaRulesStructure:
    @pytest.mark.parametrize("dimension", _REQUIRED_DIMENSIONS)
    def test_every_required_dimension_present(self, dimension):
        rules = get_video_dna_rules()
        assert dimension in rules

    def test_forbidden_visual_styles_includes_presenter_modes(self):
        items = get_video_dna_forbidden_items()
        for item in ("talking_head", "interview_framing", "visible_lip_sync",
                     "direct_address_to_camera"):
            assert item in items

    def test_no_provider_calls_possible(self):
        source = Path("hypogeum/video_dna.py").read_text()
        assert "import muapi" not in source
        assert "from hypogeum.muapi import" not in source
        assert "run_agent(" not in source


class TestMovementAndPacingExtracted:
    """
    Phase B2.4: movement_and_pacing and its four forbidden items were
    deleted from video_dna.py, not copied — hypogeum.motion_dna is now
    their sole canonical owner. This class is the absence proof the frozen
    Architecture Contract's Extraction Plan (Checkpoint 4) requires.
    """

    def test_movement_and_pacing_absent_from_rules_dict(self):
        rules = get_video_dna_rules()
        assert "movement_and_pacing" not in rules

    def test_motion_forbidden_items_absent_from_video_dna(self):
        items = get_video_dna_forbidden_items()
        for item in ("speed_ramp", "jitter_or_vibration", "snap_cut", "flash_cut"):
            assert item not in items

    def test_positive_directive_no_longer_mentions_motion_rule(self):
        directive = describe_video_dna_constraints()
        assert "motion comes from physical action" not in directive


class TestCameraLanguageExtracted:
    """
    Phase B2.5: camera_language and its camera-scoped forbidden items were
    deleted from video_dna.py, not copied — hypogeum.camera_dna is now
    their sole canonical owner. This class is the absence proof the frozen
    Architecture Contract's Extraction Plan (Checkpoint 3) requires.
    """

    def test_camera_language_absent_from_rules_dict(self):
        rules = get_video_dna_rules()
        assert "camera_language" not in rules

    def test_camera_forbidden_items_absent_from_video_dna(self):
        items = get_video_dna_forbidden_items()
        for item in ("whip_pan", "handheld_shake", "zoom_burst"):
            assert item not in items
        assert not any("frozen frame" in item for item in items)

    def test_presenter_forbidden_items_still_present(self):
        # Unaffected by this checkpoint — only camera-scoped items were
        # extracted.
        items = get_video_dna_forbidden_items()
        for item in ("talking_head", "interview_framing"):
            assert item in items

    def test_positive_directive_no_longer_mentions_camera_or_static_policy(self):
        directive = describe_video_dna_constraints()
        assert "slow, deliberate camera" not in directive
        assert "static framing is permitted only when" not in directive


class TestLightingExtracted:
    """
    Phase B2.6: the one-line `lighting` dimension was deleted from
    video_dna.py, not copied — hypogeum.lighting_dna is now its sole
    canonical owner. This class is the absence proof the frozen
    Architecture Contract's Extraction Plan (Checkpoint 5) requires.
    """

    def test_lighting_absent_from_rules_dict(self):
        rules = get_video_dna_rules()
        assert "lighting" not in rules

    def test_positive_directive_no_longer_mentions_lighting_rule(self):
        directive = describe_video_dna_constraints()
        assert "single directional light source" not in directive

    def test_priority_hierarchy_lighting_tier_unaffected(self):
        # _PRIORITY_HIERARCHY's 3: "lighting" is a distinct concept (a
        # priority-tier label) from the lighting rule content extracted
        # above — must survive this checkpoint unchanged.
        hierarchy = get_priority_hierarchy()
        assert hierarchy[3] == "lighting"


class TestArchaeologicalRealismExtracted:
    """
    Phase B2.7: the one-line `archaeological_realism` dimension was deleted
    from video_dna.py, not copied — hypogeum.historical_dna is now its sole
    canonical owner. This class is the absence proof the frozen
    Architecture Contract's Extraction Plan (Checkpoint 6) requires.
    """

    def test_archaeological_realism_absent_from_rules_dict(self):
        rules = get_video_dna_rules()
        assert "archaeological_realism" not in rules

    def test_positive_directive_no_longer_mentions_plausibility_rule(self):
        directive = describe_video_dna_constraints()
        assert "physically plausible for their stated historical period" not in directive

    def test_general_anachronism_forbidden_item_still_present(self):
        # video_dna.py's own global anachronism ban is a distinct concept
        # from historical_dna's setting-type-scoped anachronism library —
        # unaffected by this checkpoint.
        items = get_video_dna_forbidden_items()
        assert "anachronistic objects or technology in frame" in items


class TestVideoDnaPositiveNegativeSeparation:
    def test_positive_directive_contains_no_negative_language(self):
        # 2026-07-14: every prohibition moved out of the positive directive
        # into get_video_dna_forbidden_items() — the directive text itself
        # must read as entirely positive instruction.
        directive = describe_video_dna_constraints()
        lowered = directive.lower()
        for negative_word in (" never ", " no ", " avoid", " forbidden", " don't", " do not "):
            assert negative_word not in lowered, f"found negative language: {negative_word!r}"

    def test_forbidden_items_returns_a_list(self):
        # Threshold deliberately loose (not the exact count). As of Phase
        # B2.8's umbrella-scope audit, this list is video_dna.py's stable,
        # terminal content — Camera/Motion/Lighting/Historical DNA (Phases
        # B2.4-B2.7) already extracted everything that belonged to them;
        # no further shrinkage is expected as a matter of course. The loose
        # bound is kept anyway as a general non-empty sanity check, not
        # because the count is still expected to change.
        items = get_video_dna_forbidden_items()
        assert isinstance(items, list)
        assert len(items) >= 5

    def test_fine_detail_rule_not_duplicated_in_positive_directive(self):
        # fine_detail_preservation is rendered exactly once, by
        # hypogeum.video_prompt_writer's Visual Priorities section — NOT
        # also restated inside describe_video_dna_constraints()'s own text,
        # which would recreate the exact cross-section duplication this
        # revision eliminated.
        directive = describe_video_dna_constraints()
        assert "micro-texture" not in directive
        assert "readable carvings" not in directive


class TestVideoDnaPriorityHierarchy:
    def test_priority_hierarchy_has_four_tiers(self):
        hierarchy = get_priority_hierarchy()
        assert hierarchy[1] == "primary subject"
        assert hierarchy[2] == "environmental detail"
        assert hierarchy[3] == "lighting"
        assert hierarchy[4] == "atmosphere"

    def test_priority_hierarchy_is_universal_not_scene_specific(self):
        # Same call, same result, no arguments — proves it's not
        # topic/scene-dependent.
        import inspect
        from hypogeum import video_dna
        sig = inspect.signature(video_dna.get_priority_hierarchy)
        assert len(sig.parameters) == 0


class TestVideoDnaTopicAgnostic:
    def test_directive_names_no_specific_topic(self):
        directive = describe_video_dna_constraints()
        lowered = directive.lower()
        for forbidden in ("antikythera", "ep_003", "ep003", "mechanism", "doorway"):
            assert forbidden not in lowered

    def test_no_scene_specific_staging_advice(self):
        # "artifacts revealed progressively" was removed from
        # archaeological_realism — that was scene-specific staging advice,
        # not a universal rule (a static object-reveal shot has nothing to
        # reveal "progressively").
        directive = describe_video_dna_constraints()
        assert "revealed progressively" not in directive

    def test_no_per_topic_override_mechanism_exists(self):
        # Unlike Thumbnail DNA (which varies by emotional_trigger), Video DNA
        # v1 is intentionally a single fixed ruleset — no trigger parameter.
        import inspect
        from hypogeum import video_dna
        sig = inspect.signature(video_dna.describe_video_dna_constraints)
        assert len(sig.parameters) == 0


class TestVideoDnaDirectiveIsCompact:
    def test_directive_under_provider_safe_ceiling(self):
        # Learned directly from the Thumbnail DNA v5 -> v5.1 lesson — kept
        # compact from the outset rather than waiting for a real rejection.
        directive = describe_video_dna_constraints()
        assert len(directive) < 1500

    def test_each_rule_stated_once(self):
        directive = describe_video_dna_constraints()
        assert directive.count("talking_head") == 0  # moved to forbidden items entirely
        forbidden = get_video_dna_forbidden_items()
        assert forbidden.count("talking_head") == 1


class TestVideoDnaWiredIntoSubmitScene:
    def test_submit_scene_uses_structured_section_assembly(self, monkeypatch):
        import hypogeum.muapi as muapi

        captured_prompts = []

        def _fake_attempt_generation(prompt, **kwargs):
            captured_prompts.append(prompt)
            return ({"scene_id": "sc_01", "asset_id": "a1", "generation_status": "generated",
                      "provider": "muapi", "tier_used": 2, "model_used": "veo3.1-lite-text-to-video",
                      "prompt_used": prompt, "fallback_used": False, "failure_reason": None}, None)

        monkeypatch.setattr(muapi, "_attempt_generation", _fake_attempt_generation)

        scene_spec = {
            "scene_id": "sc_01",
            "visual_spec": {"aspect_ratio": "16:9", "duration_seconds": 8.0},
            "production_spec": {
                "primary_prompt": "A stone corridor lit by a single shaft of daylight.",
                "fallback_prompt": "A stone corridor, alternate framing.",
                "primary_tier": 2,
                "fallback_tier": 2,
            },
        }

        class _FakeConfig:
            MUAPI_CHEAP_MODE = False
            MUAPI_RUNWAY_ONLY_MODE = False
            MUAPI_DEFAULT_TIER = 2
            MUAPI_ALLOW_PAID_FALLBACKS = False

        output_dir = Path("/tmp/hypogeum-test-video-dna-submit-scene")
        output_dir.mkdir(parents=True, exist_ok=True)

        muapi.submit_scene(
            scene_spec=scene_spec, episode_id="ep_test", output_dir=output_dir,
            config=_FakeConfig(), budget_guard=None, agent="test", episode_id_for_asset=None,
        )

        assert captured_prompts, "expected _attempt_generation to be called"
        prompt = captured_prompts[0]
        assert prompt.startswith("SCENE: A stone corridor lit by a single shaft of daylight.")
        # Phase B2.4/B2.5/B2.6/B2.7: MOTION, CAMERA, LIGHTING, and
        # HISTORICAL DNA are now default-on sections, extracted out of
        # VIDEO DNA — present in every real submitted prompt, not optional.
        for label in (
            "SCENE:", "VISUAL PRIORITIES:", "VIDEO DNA:", "CAMERA DNA:",
            "MOTION DNA:", "LIGHTING DNA:", "HISTORICAL DNA:", "BRAND DNA:",
            "FORBIDDEN:",
        ):
            assert prompt.count(label) == 1
        # Fixed section order.
        assert (
            prompt.find("SCENE:") < prompt.find("VISUAL PRIORITIES:")
            < prompt.find("VIDEO DNA:") < prompt.find("CAMERA DNA:")
            < prompt.find("MOTION DNA:") < prompt.find("LIGHTING DNA:")
            < prompt.find("HISTORICAL DNA:") < prompt.find("BRAND DNA:")
            < prompt.find("FORBIDDEN:")
        )
        # Content preserved: the motion/camera/lighting/historical rules
        # and their forbidden items are still present in the prompt, just
        # under their own section labels instead of folded into VIDEO
        # DNA — see the B2.4/B2.5/B2.6/B2.7 reports for why this is
        # content-preserving but not literally byte-identical to
        # pre-extraction output.
        camera_section = prompt[prompt.find("CAMERA DNA:"):prompt.find("MOTION DNA:")]
        assert "slow, deliberate camera" in camera_section
        assert "static framing is permitted only when" in camera_section
        motion_section = prompt[prompt.find("MOTION DNA:"):prompt.find("LIGHTING DNA:")]
        assert "motion comes from physical action" in motion_section
        lighting_section = prompt[prompt.find("LIGHTING DNA:"):prompt.find("HISTORICAL DNA:")]
        assert "single directional light source" in lighting_section
        historical_section = prompt[prompt.find("HISTORICAL DNA:"):prompt.find("BRAND DNA:")]
        assert "physically plausible" in historical_section
        forbidden_section = prompt[prompt.find("FORBIDDEN:"):]
        for item in (
            "speed_ramp", "jitter_or_vibration", "snap_cut", "flash_cut",
            "whip_pan", "handheld_shake", "zoom_burst",
            "fluorescent", "bleached noon white",
        ):
            assert item in forbidden_section
        # Historical DNA's default fallback (setting_type="unspecified")
        # correctly contributes no setting-specific forbidden items — see
        # hypogeum/historical_dna.py's docstring for why this is honest
        # degradation, not a bug. Checked against an item exclusive to
        # Historical DNA's own library — "museum lighting" is deliberately
        # NOT used here since it is also an independent Lighting DNA
        # forbidden item (a lighting-quality concern) and renders
        # regardless of Historical DNA's setting_type; see
        # tests/test_historical_dna.py for the full explanation.
        assert "modern watercraft" not in forbidden_section

    def test_fallback_prompt_also_receives_structured_assembly(self, monkeypatch):
        import hypogeum.muapi as muapi

        captured = []

        def _fake_attempt_generation(prompt, is_fallback=False, **kwargs):
            if not is_fallback:
                return None, "job_failed"
            captured.append(prompt)
            return ({"scene_id": "sc_01", "asset_id": "a1", "generation_status": "generated",
                      "provider": "muapi", "tier_used": 2, "model_used": "veo3.1-lite-text-to-video",
                      "prompt_used": prompt, "fallback_used": True, "failure_reason": None}, None)

        monkeypatch.setattr(muapi, "_attempt_generation", _fake_attempt_generation)

        scene_spec = {
            "scene_id": "sc_01",
            "visual_spec": {"aspect_ratio": "16:9", "duration_seconds": 8.0},
            "production_spec": {
                "primary_prompt": "Primary scene text.",
                "fallback_prompt": "Fallback scene text.",
                "primary_tier": 2,
                "fallback_tier": 2,
            },
        }

        class _FakeConfig:
            MUAPI_CHEAP_MODE = False
            MUAPI_RUNWAY_ONLY_MODE = False
            MUAPI_DEFAULT_TIER = 2
            MUAPI_ALLOW_PAID_FALLBACKS = True

        output_dir = Path("/tmp/hypogeum-test-video-dna-fallback")
        output_dir.mkdir(parents=True, exist_ok=True)

        muapi.submit_scene(
            scene_spec=scene_spec, episode_id="ep_test", output_dir=output_dir,
            config=_FakeConfig(), budget_guard=None, agent="test", episode_id_for_asset=None,
        )

        assert captured
        assert captured[0].startswith("SCENE: Fallback scene text.")
        assert "VIDEO DNA:" in captured[0]

    def test_default_video_model_is_veo3_1_lite(self):
        from hypogeum.config import Config
        import os
        original = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        try:
            with patch("hypogeum.config._load_dotenv"):
                config = Config()
        finally:
            if original is None:
                os.environ.pop("OPENROUTER_API_KEY", None)
            else:
                os.environ["OPENROUTER_API_KEY"] = original
        assert config.MUAPI_T2_MODEL == "veo3.1-lite-text-to-video"
        assert config.MUAPI_T3_MODEL == "veo3.1-lite-text-to-video"

    def test_veo3_1_lite_registered_with_correct_cost(self):
        import hypogeum.muapi as muapi
        assert "veo3.1-lite-text-to-video" in muapi._VEO3_1_MODELS
        assert muapi.MODEL_CREDIT_COSTS["veo3.1-lite-text-to-video"] == 0.39
