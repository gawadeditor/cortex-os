"""
tests/test_motion_dna.py — Phase B2.4: Motion DNA extraction.

Covers hypogeum.motion_dna's public API (per
docs/validation/prompt_writer_architecture/B1_3_ARCHITECTURE_CONTRACT.md),
proof that it matches what hypogeum.video_dna used to produce for
movement_and_pacing byte-for-byte, its default-on wiring into
hypogeum.video_prompt_writer.assemble_video_scene_prompt(), and Objective-
style responsibility-leakage checks mirroring test_composition_dna.py's
pattern. Zero provider calls anywhere in this file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hypogeum.motion_dna import (
    get_motion_dna_rules, describe_motion_constraints, get_motion_forbidden_items,
)


# ---------------------------------------------------------------------------
# Public API matches the frozen contract
# ---------------------------------------------------------------------------

class TestMotionDnaPublicApi:
    def test_get_motion_dna_rules_returns_dict(self):
        rules = get_motion_dna_rules()
        assert isinstance(rules, dict)

    def test_describe_motion_constraints_is_zero_arg(self):
        import inspect
        sig = inspect.signature(describe_motion_constraints)
        assert len(sig.parameters) == 0

    def test_get_motion_forbidden_items_returns_list(self):
        items = get_motion_forbidden_items()
        assert isinstance(items, list)
        assert all(isinstance(i, str) for i in items)


# ---------------------------------------------------------------------------
# Deterministic, no mutable global state exposed (matches Composition DNA's
# established defensive-copy convention)
# ---------------------------------------------------------------------------

class TestMotionDnaDeterminism:
    def test_rules_call_is_deterministic(self):
        assert get_motion_dna_rules() == get_motion_dna_rules()

    def test_directive_call_is_deterministic(self):
        assert describe_motion_constraints() == describe_motion_constraints()

    def test_mutating_returned_rules_does_not_affect_module_state(self):
        rules = get_motion_dna_rules()
        rules["movement_and_pacing"]["positive_rule"] = "MUTATED"
        rules["forbidden_motion_styles"].append("MUTATED")
        fresh = get_motion_dna_rules()
        assert fresh["movement_and_pacing"]["positive_rule"] != "MUTATED"
        assert "MUTATED" not in fresh["forbidden_motion_styles"]

    def test_mutating_returned_forbidden_items_does_not_affect_module_state(self):
        items = get_motion_forbidden_items()
        items.append("MUTATED")
        assert "MUTATED" not in get_motion_forbidden_items()


# ---------------------------------------------------------------------------
# Content matches what video_dna.py used to produce, byte-for-byte
# (the Architecture Contract's own validation bar for this extraction)
# ---------------------------------------------------------------------------

class TestExtractedContentMatchesOriginal:
    def test_positive_rule_text_unchanged_from_pre_extraction_wording(self):
        assert (
            get_motion_dna_rules()["movement_and_pacing"]["positive_rule"]
            == "motion comes from physical action, environment, or the camera itself"
        )

    def test_forbidden_items_unchanged_from_pre_extraction_set(self):
        assert set(get_motion_forbidden_items()) == {
            "speed_ramp", "jitter_or_vibration", "snap_cut", "flash_cut",
        }

    def test_positive_directive_contains_no_negative_language(self):
        # Matches hypogeum.video_dna's established, tested discipline.
        directive = describe_motion_constraints()
        lowered = directive.lower()
        for negative_word in (" never ", " no ", " avoid", " forbidden", " don't", " do not "):
            assert negative_word not in lowered, f"found negative language: {negative_word!r}"


# ---------------------------------------------------------------------------
# Responsibility leakage checks
# ---------------------------------------------------------------------------

class TestMotionDnaDoesNotLeakOtherDnaResponsibilities:
    @pytest.fixture
    def full_text(self):
        return (str(get_motion_dna_rules()) + describe_motion_constraints()
                + str(get_motion_forbidden_items())).lower()

    def test_no_camera_verb_vocabulary(self, full_text):
        for term in ("slow_push_in", "controlled slow push in", "dolly", "static_with_atmosphere",
                     "camera_movement", "camera_angle", "whip_pan", "handheld_shake", "zoom_burst"):
            assert term not in full_text, f"leaked camera vocabulary: {term!r}"

    def test_no_lighting_vocabulary(self, full_text):
        for term in ("torchlight", "falloff", "amber", "oil lamp", "golden hour"):
            assert term not in full_text, f"leaked lighting vocabulary: {term!r}"

    def test_no_composition_vocabulary(self, full_text):
        for term in ("rule_of_thirds", "negative_space_dominant", "diagonal_leading_line"):
            assert term not in full_text, f"leaked composition vocabulary: {term!r}"

    def test_no_provider_specific_terminology(self, full_text):
        for term in ("veo", "seedance", "muapi", "byteplus"):
            assert term not in full_text, f"leaked provider terminology: {term!r}"

    def test_no_brand_content(self, full_text):
        import re
        assert re.search(r"#[0-9a-f]{6}\b", full_text) is None
        for term in ("aged_gold", "near_black", "wordmark"):
            assert term not in full_text

    def test_no_topic_or_episode_names(self, full_text):
        for term in ("antikythera", "ep_", "episode "):
            assert term not in full_text

    def test_no_provider_calls_possible(self):
        source = Path("hypogeum/motion_dna.py").read_text()
        assert "import muapi" not in source
        assert "from hypogeum.muapi import" not in source
        assert "run_agent(" not in source
        assert "requests" not in source
        assert "urllib" not in source


# ---------------------------------------------------------------------------
# Wiring into video_prompt_writer.assemble_video_scene_prompt() — default ON
# ---------------------------------------------------------------------------

class TestMotionDnaWiring:
    def test_motion_section_present_by_default(self):
        # Unlike Composition DNA, Motion DNA is default-on — proves the
        # extracted content still reaches production's zero-argument call.
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        assert "MOTION DNA:" in prompt

    def test_motion_section_content_matches_module(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        motion_section = prompt[prompt.find("MOTION DNA:"):]
        assert "motion comes from physical action" in motion_section

    def test_motion_section_ordered_after_video_dna_before_brand_dna(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        assert (
            prompt.find("VIDEO DNA:") < prompt.find("MOTION DNA:") < prompt.find("BRAND DNA:")
        )

    def test_motion_forbidden_items_present_in_default_call(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        forbidden_section = prompt[prompt.find("FORBIDDEN:"):]
        for item in ("speed_ramp", "jitter_or_vibration", "snap_cut", "flash_cut"):
            assert item in forbidden_section

    def test_override_callable_replaces_default(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt(
            "A stone corridor.",
            motion_directive_fn=lambda: "OVERRIDE MOTION TEXT.",
        )
        motion_section = prompt[prompt.find("MOTION DNA:"):]
        assert "OVERRIDE MOTION TEXT." in motion_section
        assert "motion comes from physical action" not in motion_section

    def test_submit_scene_call_shape_unaffected(self):
        # hypogeum.muapi.submit_scene() must continue calling
        # assemble_video_scene_prompt() with exactly one positional
        # argument — this checkpoint did not touch muapi.py's call site;
        # default-on wiring means the new content reaches it without any
        # change to the call itself.
        import hypogeum.muapi as muapi
        source = Path(muapi.__file__).read_text()
        assert (
            'video_prompt_writer.assemble_video_scene_prompt(prod_spec["primary_prompt"])'
            in source
        )


def test_no_duplicate_motion_forbidden_items_when_combined_with_composition():
    from hypogeum.video_prompt_writer import assemble_video_scene_prompt
    prompt = assemble_video_scene_prompt(
        "A stone corridor.",
        composition_forbidden_fn=lambda: ["speed_ramp", "disconnected_scale_shot"],
    )
    forbidden_section = prompt[prompt.find("FORBIDDEN:"):]
    assert forbidden_section.count("speed_ramp") == 1
