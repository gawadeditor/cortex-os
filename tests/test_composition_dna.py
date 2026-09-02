"""
tests/test_composition_dna.py — Phase B2.1: Composition DNA extraction.

Covers hypogeum.composition_dna's public API (per
docs/validation/prompt_writer_architecture/B1_3_ARCHITECTURE_CONTRACT.md),
its wiring into hypogeum.video_prompt_writer.assemble_video_scene_prompt(),
and Objective 4's responsibility-leakage checks. Zero provider calls anywhere
in this file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hypogeum.composition_dna import (
    get_composition_dna_rules, describe_composition_constraints,
    get_composition_forbidden_items,
)


# ---------------------------------------------------------------------------
# Public API matches the frozen contract
# ---------------------------------------------------------------------------

class TestCompositionDnaPublicApi:
    def test_get_composition_dna_rules_returns_dict(self):
        rules = get_composition_dna_rules()
        assert isinstance(rules, dict)

    def test_describe_composition_constraints_requires_aspect_ratio(self):
        import inspect
        sig = inspect.signature(describe_composition_constraints)
        params = list(sig.parameters.values())
        assert params[0].name == "aspect_ratio"
        assert params[0].default is inspect.Parameter.empty  # required, no default
        assert sig.parameters["spatial_relationship"].default is None
        assert sig.parameters["composition"].default is None

    def test_get_composition_forbidden_items_returns_list(self):
        items = get_composition_forbidden_items()
        assert isinstance(items, list)
        assert all(isinstance(i, str) for i in items)


# ---------------------------------------------------------------------------
# Deterministic, no mutable global state exposed
# ---------------------------------------------------------------------------

class TestCompositionDnaDeterminism:
    def test_rules_call_is_deterministic(self):
        assert get_composition_dna_rules() == get_composition_dna_rules()

    def test_directive_call_is_deterministic(self):
        a = describe_composition_constraints(aspect_ratio="16:9")
        b = describe_composition_constraints(aspect_ratio="16:9")
        assert a == b

    def test_forbidden_items_call_is_deterministic(self):
        assert get_composition_forbidden_items() == get_composition_forbidden_items()

    def test_mutating_returned_rules_does_not_affect_module_state(self):
        rules = get_composition_dna_rules()
        rules["spatial_pattern"]["background"] = "MUTATED"
        rules["composition_vocabulary"].append("MUTATED")
        fresh = get_composition_dna_rules()
        assert fresh["spatial_pattern"]["background"] != "MUTATED"
        assert "MUTATED" not in fresh["composition_vocabulary"]

    def test_mutating_returned_forbidden_items_does_not_affect_module_state(self):
        items = get_composition_forbidden_items()
        items.append("MUTATED")
        fresh = get_composition_forbidden_items()
        assert "MUTATED" not in fresh


# ---------------------------------------------------------------------------
# Positive composition rules present
# ---------------------------------------------------------------------------

class TestCompositionDnaPositiveRules:
    def test_background_subject_negative_space_present(self):
        directive = describe_composition_constraints(aspect_ratio="16:9")
        lowered = directive.lower()
        assert "background" in lowered
        assert "rule-of-thirds" in lowered
        assert "negative space" in lowered

    def test_aspect_ratio_framing_rendered_for_known_ratios(self):
        wide = describe_composition_constraints(aspect_ratio="16:9")
        vertical = describe_composition_constraints(aspect_ratio="9:16")
        assert "widescreen" in wide.lower()
        assert "vertical" in vertical.lower()

    def test_unknown_aspect_ratio_degrades_safely(self):
        # Must not raise; framing sentence simply omitted.
        directive = describe_composition_constraints(aspect_ratio="4:3")
        assert isinstance(directive, str)
        assert len(directive) > 0

    def test_spatial_relationship_passed_through_verbatim(self):
        directive = describe_composition_constraints(
            aspect_ratio="16:9",
            spatial_relationship="The arena floor exists directly above the hypogeum tunnels.",
        )
        assert "The arena floor exists directly above the hypogeum tunnels." in directive

    def test_composition_assignment_rendered_when_present(self):
        directive = describe_composition_constraints(
            aspect_ratio="16:9", composition="rule_of_thirds_left",
        )
        assert "rule_of_thirds_left" in directive

    def test_composition_assignment_absent_by_default(self):
        directive = describe_composition_constraints(aspect_ratio="16:9")
        assert "Composition assignment" not in directive

    def test_positive_directive_contains_no_negative_language(self):
        # Matches hypogeum.video_dna's established, tested discipline.
        directive = describe_composition_constraints(aspect_ratio="16:9")
        lowered = directive.lower()
        for negative_word in (" never ", " no ", " avoid", " forbidden", " don't", " do not "):
            assert negative_word not in lowered, f"found negative language: {negative_word!r}"

    def test_forbidden_items_generalized_from_contract_example(self):
        items = get_composition_forbidden_items()
        assert "disconnected_scale_shot" in items
        assert "abstract_size_text" in items


# ---------------------------------------------------------------------------
# Objective 4 — responsibility leakage checks
# ---------------------------------------------------------------------------

class TestCompositionDnaDoesNotLeakOtherDnaResponsibilities:
    """
    Composition DNA must not define or translate camera movement vocabulary,
    provider-specific terminology, negative-bracket prompt syntax, lighting
    vocabulary, historical-era rules, brand palette/typography, or
    shot-diversity assignment/enforcement logic.
    """

    @pytest.fixture
    def full_text(self):
        rules = get_composition_dna_rules()
        directive = describe_composition_constraints(
            aspect_ratio="16:9", spatial_relationship="x", composition="y",
        )
        forbidden = get_composition_forbidden_items()
        return (str(rules) + directive + str(forbidden)).lower()

    def test_no_camera_movement_vocabulary(self, full_text):
        for term in (
            "slow_push_in", "controlled slow push in", "dolly", "orbital_arc",
            "locked_off_hold", "handheld_drift", "camera_movement", "camera_angle",
            "static framing", "static_with_atmosphere",
        ):
            assert term not in full_text, f"leaked camera vocabulary: {term!r}"

    def test_no_provider_specific_terminology(self, full_text):
        for term in ("veo", "seedance", "muapi", "byteplus"):
            assert term not in full_text, f"leaked provider terminology: {term!r}"

    def test_no_negative_bracket_syntax(self, full_text):
        for term in ("[no ", "[without", "[lamp source off-frame]"):
            assert term not in full_text, f"leaked negative-bracket syntax: {term!r}"

    def test_no_lighting_vocabulary(self, full_text):
        for term in (
            "torchlight", "falloff", "amber", "oil lamp", "golden hour",
            "lighting character", "color temperature",
        ):
            assert term not in full_text, f"leaked lighting vocabulary: {term!r}"

    def test_no_historical_era_rules(self, full_text):
        for term in ("archaeological", "anachronis", "historical period", "period-accurate"):
            assert term not in full_text, f"leaked historical-authenticity content: {term!r}"

    def test_no_brand_palette_or_typography(self, full_text):
        for term in ("#", "near_black", "aged_gold", "antique_gold", "typography", "wordmark"):
            assert term not in full_text, f"leaked brand content: {term!r}"

    def test_no_shot_diversity_enforcement_logic(self, full_text):
        for term in ("max_consecutive", "reuse_allowed", "consecutive repeated"):
            assert term not in full_text, f"leaked shot-diversity enforcement logic: {term!r}"

    def test_no_provider_calls_possible(self):
        source = Path("hypogeum/composition_dna.py").read_text()
        assert "import muapi" not in source
        assert "from hypogeum.muapi import" not in source
        assert "run_agent(" not in source
        assert "requests" not in source
        assert "urllib" not in source


# ---------------------------------------------------------------------------
# Existing composition prose preserved (Objective 5, check 9) — annotations
# were added alongside the original content, not in place of it.
# ---------------------------------------------------------------------------

class TestExistingCompositionProsePreserved:
    def test_cinematic_architecture_layer_5_preserved(self):
        path = Path("hypogeum/skills/visual_package_director/cinematic_prompt_architecture.md")
        text = path.read_text(encoding="utf-8")
        assert "## Layer 5 — Spatial Composition and Negative Space" in text
        assert "rule-of-thirds language: upper-left quarter, lower-right third" in text
        assert "Negative space is not emptiness" in text
        assert "Hypogeum Policy Note" in text  # annotation present too

    def test_vle_spatial_relationship_templates_preserved(self):
        path = Path("hypogeum/skills/compiled/visual_language_engine.compact.md")
        text = path.read_text(encoding="utf-8")
        assert "## Spatial Relationship Templates" in text
        for key in ("above_below", "inside_outside", "before_after", "micro_macro"):
            assert key in text
        assert "Hypogeum Policy Note" in text

    def test_shot_diversity_composition_options_preserved(self):
        path = Path("hypogeum/prompts/shot_diversity_planner.txt")
        text = path.read_text(encoding="utf-8")
        assert "COMPOSITION OPTIONS" in text
        for option in (
            "rule_of_thirds_left", "rule_of_thirds_right", "centered_symmetry",
            "diagonal_leading_line", "foreground_frame", "layered_depth",
            "negative_space_dominant", "edge_of_frame_subject",
        ):
            assert option in text
        assert "hypogeum/composition_dna.py" in text


# ---------------------------------------------------------------------------
# Wiring into video_prompt_writer.assemble_video_scene_prompt()
# ---------------------------------------------------------------------------

class TestCompositionDnaWiring:
    def test_composition_section_absent_by_default(self):
        # Backward compatibility: hypogeum.muapi.submit_scene()'s existing,
        # zero-extra-argument call must be unaffected.
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        assert "COMPOSITION:" not in prompt

    def test_composition_section_present_when_explicitly_supplied(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt(
            "A stone corridor.",
            composition_directive_fn=lambda: describe_composition_constraints(aspect_ratio="16:9"),
        )
        assert "COMPOSITION:" in prompt

    def test_composition_section_ordered_after_visual_priorities_before_video_dna(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt(
            "A stone corridor.",
            composition_directive_fn=lambda: describe_composition_constraints(aspect_ratio="16:9"),
        )
        assert (
            prompt.find("VISUAL PRIORITIES:")
            < prompt.find("COMPOSITION:")
            < prompt.find("VIDEO DNA:")
        )

    def test_composition_forbidden_items_merged_and_deduplicated(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt(
            "A stone corridor.",
            composition_forbidden_fn=lambda: ["disconnected_scale_shot", "talking_head"],
        )
        forbidden_section = prompt[prompt.find("FORBIDDEN:"):]
        assert forbidden_section.count("disconnected_scale_shot") == 1
        assert "talking_head" in forbidden_section  # from Video DNA, not duplicated

    def test_submit_scene_call_shape_unaffected(self, monkeypatch):
        # hypogeum.muapi.submit_scene() must continue calling
        # assemble_video_scene_prompt() with exactly one positional
        # argument — proves this slice did not touch muapi.py's call site.
        import hypogeum.muapi as muapi
        source = Path(muapi.__file__).read_text()
        assert (
            'video_prompt_writer.assemble_video_scene_prompt(prod_spec["primary_prompt"])'
            in source
        )
        assert (
            'video_prompt_writer.assemble_video_scene_prompt(prod_spec["fallback_prompt"])'
            in source
        )
