"""
tests/test_lighting_dna.py — Phase B2.6: Lighting DNA extraction.

Covers hypogeum.lighting_dna's public API (per
docs/validation/prompt_writer_architecture/B1_3_ARCHITECTURE_CONTRACT.md),
proof that its content matches what video_dna.py/cinematic_prompt_
architecture.md/vpd_scene_prompts.txt used to carry, its default-on wiring
into hypogeum.video_prompt_writer.assemble_video_scene_prompt(), and
responsibility-leakage checks mirroring test_camera_dna.py's pattern. Zero
provider calls anywhere in this file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hypogeum.lighting_dna import (
    get_lighting_dna_rules, describe_lighting_constraints,
    get_lighting_forbidden_items, get_lamp_off_frame_tag,
)


# ---------------------------------------------------------------------------
# Public API matches the frozen contract
# ---------------------------------------------------------------------------

class TestLightingDnaPublicApi:
    def test_get_lighting_dna_rules_returns_dict(self):
        assert isinstance(get_lighting_dna_rules(), dict)

    def test_describe_lighting_constraints_signature(self):
        import inspect
        sig = inspect.signature(describe_lighting_constraints)
        assert sig.parameters["lighting_selection"].default is None
        assert sig.parameters["has_practical_source"].default is False

    def test_get_lighting_forbidden_items_returns_list(self):
        items = get_lighting_forbidden_items()
        assert isinstance(items, list)
        assert all(isinstance(i, str) for i in items)

    def test_get_lamp_off_frame_tag_signature(self):
        import inspect
        sig = inspect.signature(get_lamp_off_frame_tag)
        assert len(sig.parameters) == 1


# ---------------------------------------------------------------------------
# Deterministic, no mutable global state exposed
# ---------------------------------------------------------------------------

class TestLightingDnaDeterminism:
    def test_rules_call_is_deterministic(self):
        assert get_lighting_dna_rules() == get_lighting_dna_rules()

    def test_directive_call_is_deterministic(self):
        assert describe_lighting_constraints() == describe_lighting_constraints()

    def test_mutating_returned_rules_does_not_affect_module_state(self):
        rules = get_lighting_dna_rules()
        rules["base_rule"]["positive_rule"] = "MUTATED"
        rules["temperature_categories"]["warm"] = "MUTATED"
        rules["forbidden_lighting_styles"].append("MUTATED")
        fresh = get_lighting_dna_rules()
        assert fresh["base_rule"]["positive_rule"] != "MUTATED"
        assert fresh["temperature_categories"]["warm"] != "MUTATED"
        assert "MUTATED" not in fresh["forbidden_lighting_styles"]

    def test_mutating_returned_forbidden_items_does_not_affect_module_state(self):
        items = get_lighting_forbidden_items()
        items.append("MUTATED")
        assert "MUTATED" not in get_lighting_forbidden_items()


# ---------------------------------------------------------------------------
# get_lamp_off_frame_tag — deterministic, code-computed replacement for
# LLM-memory-dependent instruction
# ---------------------------------------------------------------------------

class TestLampOffFrameTag:
    def test_returns_tag_when_practical_source_present(self):
        assert get_lamp_off_frame_tag(True) == "[lamp source off-frame]"

    def test_returns_none_when_no_practical_source(self):
        assert get_lamp_off_frame_tag(False) is None

    def test_tag_rendered_in_directive_when_practical_source_true(self):
        directive = describe_lighting_constraints(has_practical_source=True)
        assert "[lamp source off-frame]" in directive

    def test_tag_absent_from_directive_by_default(self):
        directive = describe_lighting_constraints()
        assert "[lamp source off-frame]" not in directive


# ---------------------------------------------------------------------------
# Taxonomy matches the frozen contract exactly (already established by
# Phase B2.3's setting_lighting_classifier — cross-checked here, not
# re-derived)
# ---------------------------------------------------------------------------

class TestLightingTaxonomy:
    def test_six_categories_match_frozen_contract(self):
        categories = set(get_lighting_dna_rules()["temperature_categories"])
        assert categories == {"warm", "harsh", "atmospheric", "dramatic", "underground", "underwater"}

    def test_matches_setting_lighting_classifier_taxonomy(self):
        # Phase B2.3 built LIGHTING_SELECTION_VALUES from the same source
        # (vpd_scene_prompts.txt's LIGHTING CONSTRAINT categories) — this
        # confirms the two never drifted apart.
        from hypogeum.agents.setting_lighting_classifier import LIGHTING_SELECTION_VALUES
        categories = set(get_lighting_dna_rules()["temperature_categories"])
        assert categories == set(LIGHTING_SELECTION_VALUES) - {"unspecified"}

    def test_unspecified_or_unrecognised_selection_degrades_to_full_vocabulary(self):
        directive_none = describe_lighting_constraints(lighting_selection=None)
        directive_unspecified = describe_lighting_constraints(lighting_selection="unspecified")
        directive_unknown = describe_lighting_constraints(lighting_selection="not_a_real_category")
        for directive in (directive_none, directive_unspecified, directive_unknown):
            assert "warm" in directive.lower()
            assert "harsh" in directive.lower()
            assert "underwater" in directive.lower()

    def test_recognised_selection_filters_to_one_category(self):
        directive = describe_lighting_constraints(lighting_selection="underground")
        assert "torch illumination" in directive
        # Full-vocabulary categories not selected should not appear.
        assert "direct overhead sun" not in directive


# ---------------------------------------------------------------------------
# Positive/negative separation and forbidden-item content
# ---------------------------------------------------------------------------

class TestLightingDnaPositiveNegativeSeparation:
    def test_positive_directive_contains_no_negative_language(self):
        for directive in (
            describe_lighting_constraints(),
            describe_lighting_constraints(lighting_selection="warm"),
            describe_lighting_constraints(has_practical_source=True),
        ):
            lowered = directive.lower()
            for negative_word in (" never ", " no ", " avoid", " forbidden", " don't", " do not "):
                assert negative_word not in lowered, f"found negative language: {negative_word!r} in {directive!r}"

    def test_forbidden_items_include_fluorescent_and_museum_and_cool_blue_and_bleached_noon(self):
        items = get_lighting_forbidden_items()
        for item in ("fluorescent", "museum lighting", "cool blue-white", "bleached noon white"):
            assert item in items


# ---------------------------------------------------------------------------
# Responsibility leakage checks
# ---------------------------------------------------------------------------

class TestLightingDnaDoesNotLeakOtherDnaResponsibilities:
    @pytest.fixture
    def full_text(self):
        return (str(get_lighting_dna_rules())
                + describe_lighting_constraints(lighting_selection="warm", has_practical_source=True)
                + str(get_lighting_forbidden_items())).lower()

    def test_no_camera_vocabulary(self, full_text):
        for term in ("slow_push_in", "static_with_atmosphere", "camera_movement",
                     "whip_pan", "handheld_shake"):
            assert term not in full_text, f"leaked camera vocabulary: {term!r}"

    def test_no_motion_vocabulary(self, full_text):
        for term in ("motion comes from physical action", "speed_ramp", "snap_cut"):
            assert term not in full_text, f"leaked motion vocabulary: {term!r}"

    def test_no_composition_vocabulary(self, full_text):
        for term in ("rule_of_thirds", "negative_space_dominant", "diagonal_leading_line"):
            assert term not in full_text, f"leaked composition vocabulary: {term!r}"

    def test_no_brand_material_or_color_philosophy_content(self, full_text):
        # "lighting character" (color-temperature philosophy) is explicitly
        # Brand DNA's, per the frozen contract's boundary — must not appear
        # here as owned content.
        import re
        assert re.search(r"#[0-9a-f]{6}\b", full_text) is None
        for term in ("aged_gold", "near_black", "wordmark", "lighting character"):
            assert term not in full_text

    def test_no_provider_specific_terminology(self, full_text):
        for term in ("veo", "seedance", "muapi", "byteplus"):
            assert term not in full_text, f"leaked provider terminology: {term!r}"

    def test_no_topic_or_episode_names(self, full_text):
        for term in ("antikythera", "ep_", "episode "):
            assert term not in full_text

    def test_no_provider_calls_possible(self):
        source = Path("hypogeum/lighting_dna.py").read_text()
        assert "import muapi" not in source
        assert "from hypogeum.muapi import" not in source
        assert "run_agent(" not in source
        assert "requests" not in source
        assert "urllib" not in source


# ---------------------------------------------------------------------------
# Wiring into video_prompt_writer.assemble_video_scene_prompt() — default ON
# ---------------------------------------------------------------------------

class TestLightingDnaWiring:
    def test_lighting_section_present_by_default(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        assert "LIGHTING DNA:" in prompt

    def test_lighting_section_content_matches_module(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        lighting_section = prompt[prompt.find("LIGHTING DNA:"):prompt.find("BRAND DNA:")]
        assert "single directional light source" in lighting_section

    def test_lighting_section_ordered_after_motion_before_brand(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        assert (
            prompt.find("MOTION DNA:") < prompt.find("LIGHTING DNA:") < prompt.find("BRAND DNA:")
        )

    def test_lighting_forbidden_items_present_in_default_call(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        forbidden_section = prompt[prompt.find("FORBIDDEN:"):]
        for item in ("fluorescent", "bleached noon white"):
            assert item in forbidden_section

    def test_override_callable_replaces_default(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt(
            "A stone corridor.",
            lighting_directive_fn=lambda: "OVERRIDE LIGHTING TEXT.",
        )
        lighting_section = prompt[prompt.find("LIGHTING DNA:"):prompt.find("BRAND DNA:")]
        assert "OVERRIDE LIGHTING TEXT." in lighting_section
        assert "single directional light source" not in lighting_section

    def test_override_callable_can_supply_lighting_selection(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt(
            "A stone corridor.",
            lighting_directive_fn=lambda: describe_lighting_constraints(lighting_selection="underground"),
        )
        lighting_section = prompt[prompt.find("LIGHTING DNA:"):prompt.find("BRAND DNA:")]
        assert "torch illumination" in lighting_section

    def test_submit_scene_call_shape_unaffected(self):
        import hypogeum.muapi as muapi
        source = Path(muapi.__file__).read_text()
        assert "video_prompt_writer.assemble_video_scene_prompt(" in source
        assert 'prod_spec["primary_prompt"], channel_id=channel_id)' in source


def test_no_duplicate_lighting_forbidden_items_when_combined_with_composition():
    from hypogeum.video_prompt_writer import assemble_video_scene_prompt
    prompt = assemble_video_scene_prompt(
        "A stone corridor.",
        composition_forbidden_fn=lambda: ["fluorescent", "disconnected_scale_shot"],
    )
    forbidden_section = prompt[prompt.find("FORBIDDEN:"):]
    assert forbidden_section.count("fluorescent") == 1
