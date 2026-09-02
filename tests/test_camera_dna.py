"""
tests/test_camera_dna.py — Phase B2.5: Camera DNA extraction.

Covers hypogeum.camera_dna's public API (per
docs/validation/prompt_writer_architecture/B1_3_ARCHITECTURE_CONTRACT.md),
proof that it matches what hypogeum.video_dna used to produce for
camera_language byte-for-byte, its default-on wiring into
hypogeum.video_prompt_writer.assemble_video_scene_prompt(), and
responsibility-leakage checks mirroring test_motion_dna.py's pattern. Zero
provider calls anywhere in this file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hypogeum.camera_dna import (
    get_camera_dna_rules, describe_camera_constraints, get_camera_forbidden_items,
    is_recognised_camera_movement,
)


# ---------------------------------------------------------------------------
# Public API matches the frozen contract
# ---------------------------------------------------------------------------

class TestCameraDnaPublicApi:
    def test_get_camera_dna_rules_returns_dict(self):
        assert isinstance(get_camera_dna_rules(), dict)

    def test_describe_camera_constraints_signature(self):
        import inspect
        sig = inspect.signature(describe_camera_constraints)
        for name in ("camera_movement", "camera_angle", "shot_scale"):
            assert sig.parameters[name].default is None

    def test_get_camera_forbidden_items_returns_list(self):
        items = get_camera_forbidden_items()
        assert isinstance(items, list)
        assert all(isinstance(i, str) for i in items)

    def test_is_recognised_camera_movement_returns_bool(self):
        assert isinstance(is_recognised_camera_movement("slow_push_in"), bool)


# ---------------------------------------------------------------------------
# Deterministic, no mutable global state exposed
# ---------------------------------------------------------------------------

class TestCameraDnaDeterminism:
    def test_rules_call_is_deterministic(self):
        assert get_camera_dna_rules() == get_camera_dna_rules()

    def test_directive_call_is_deterministic(self):
        assert describe_camera_constraints() == describe_camera_constraints()

    def test_mutating_returned_rules_does_not_affect_module_state(self):
        rules = get_camera_dna_rules()
        rules["camera_language"]["positive_rule"] = "MUTATED"
        rules["camera_language"]["allowed_movements"].append("MUTATED")
        rules["forbidden_camera_styles"].append("MUTATED")
        fresh = get_camera_dna_rules()
        assert fresh["camera_language"]["positive_rule"] != "MUTATED"
        assert "MUTATED" not in fresh["camera_language"]["allowed_movements"]
        assert "MUTATED" not in fresh["forbidden_camera_styles"]

    def test_mutating_returned_forbidden_items_does_not_affect_module_state(self):
        items = get_camera_forbidden_items()
        items.append("MUTATED")
        assert "MUTATED" not in get_camera_forbidden_items()


# ---------------------------------------------------------------------------
# Content matches what video_dna.py used to produce, byte-for-byte
# ---------------------------------------------------------------------------

class TestExtractedContentMatchesOriginal:
    def test_allowed_movements_unchanged(self):
        assert get_camera_dna_rules()["camera_language"]["allowed_movements"] == [
            "slow_pan", "slow_push_in", "slow_pull_out", "static_with_atmosphere",
            "slow_aerial_drift", "slow_tracking_along_surface", "slow_tilt_reveal",
        ]

    def test_positive_rule_unchanged(self):
        assert (
            get_camera_dna_rules()["camera_language"]["positive_rule"]
            == "slow, deliberate camera; one coherent perspective per shot"
        )

    def test_static_framing_policy_unchanged(self):
        # This is the decision already made in B1_2A_STATIC_CAMERA_POLICY_MEMO.md
        # — this checkpoint relocates it, never re-derives it.
        assert get_camera_dna_rules()["camera_language"]["static_framing_policy"] == (
            "static framing is permitted only when the frame contains meaningful "
            "subject, environmental, atmospheric, or lighting motion; static "
            "framing is intentional, rare, and narratively justified"
        )

    def test_forbidden_items_unchanged_from_pre_extraction_set(self):
        assert set(get_camera_forbidden_items()) == {
            "whip_pan", "handheld_shake", "zoom_burst",
            "completely frozen frame with no subject, environmental, atmospheric, or lighting motion",
        }

    def test_positive_directive_contains_no_negative_language(self):
        directive = describe_camera_constraints()
        lowered = directive.lower()
        for negative_word in (" never ", " no ", " avoid", " forbidden", " don't", " do not "):
            assert negative_word not in lowered, f"found negative language: {negative_word!r}"


# ---------------------------------------------------------------------------
# Optional per-scene assignment rendering (consume, never redefine)
# ---------------------------------------------------------------------------

class TestCameraDnaAssignmentRendering:
    def test_no_assignment_arguments_omits_assignment_lines(self):
        directive = describe_camera_constraints()
        assert "assignment" not in directive.lower()

    def test_shot_scale_rendered_when_supplied(self):
        directive = describe_camera_constraints(shot_scale="wide_establishing")
        assert "wide_establishing" in directive

    def test_camera_movement_rendered_when_supplied(self):
        directive = describe_camera_constraints(camera_movement="slow_push_in")
        assert "slow_push_in" in directive

    def test_camera_angle_rendered_when_supplied(self):
        directive = describe_camera_constraints(camera_angle="eye_level")
        assert "eye_level" in directive

    def test_does_not_validate_or_reject_unrecognised_values(self):
        # Consumes already-decided upstream facts; does not gate them.
        directive = describe_camera_constraints(camera_movement="not_a_real_movement")
        assert "not_a_real_movement" in directive


class TestIsRecognisedCameraMovement:
    def test_known_movement_is_true(self):
        assert is_recognised_camera_movement("slow_push_in") is True

    def test_unknown_movement_is_false(self):
        assert is_recognised_camera_movement("teleport_cut") is False

    def test_not_itself_a_validator(self):
        # Membership check only — never raises, never gates.
        assert is_recognised_camera_movement("") is False


# ---------------------------------------------------------------------------
# Responsibility leakage checks
# ---------------------------------------------------------------------------

class TestCameraDnaDoesNotLeakOtherDnaResponsibilities:
    @pytest.fixture
    def full_text(self):
        return (str(get_camera_dna_rules()) + describe_camera_constraints()
                + str(get_camera_forbidden_items())).lower()

    def test_no_motion_vocabulary(self, full_text):
        for term in ("motion comes from physical action", "speed_ramp",
                     "jitter_or_vibration", "snap_cut", "flash_cut"):
            assert term not in full_text, f"leaked motion vocabulary: {term!r}"

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
        source = Path("hypogeum/camera_dna.py").read_text()
        assert "import muapi" not in source
        assert "from hypogeum.muapi import" not in source
        assert "run_agent(" not in source
        assert "requests" not in source
        assert "urllib" not in source


# ---------------------------------------------------------------------------
# Wiring into video_prompt_writer.assemble_video_scene_prompt() — default ON
# ---------------------------------------------------------------------------

class TestCameraDnaWiring:
    def test_camera_section_present_by_default(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        assert "CAMERA DNA:" in prompt

    def test_camera_section_content_matches_module(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        camera_section = prompt[prompt.find("CAMERA DNA:"):]
        assert "slow, deliberate camera" in camera_section
        assert "static framing is permitted only when" in camera_section

    def test_camera_section_ordered_after_video_dna_before_motion_dna(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        assert (
            prompt.find("VIDEO DNA:") < prompt.find("CAMERA DNA:") < prompt.find("MOTION DNA:")
        )

    def test_camera_forbidden_items_present_in_default_call(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        forbidden_section = prompt[prompt.find("FORBIDDEN:"):]
        for item in ("whip_pan", "handheld_shake", "zoom_burst"):
            assert item in forbidden_section
        assert "frozen frame" in forbidden_section

    def test_override_callable_replaces_default(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt(
            "A stone corridor.",
            camera_directive_fn=lambda: "OVERRIDE CAMERA TEXT.",
        )
        camera_section = prompt[prompt.find("CAMERA DNA:"):prompt.find("MOTION DNA:")]
        assert "OVERRIDE CAMERA TEXT." in camera_section
        assert "slow, deliberate camera" not in camera_section

    def test_submit_scene_call_shape_unaffected(self):
        import hypogeum.muapi as muapi
        source = Path(muapi.__file__).read_text()
        assert (
            'video_prompt_writer.assemble_video_scene_prompt(prod_spec["primary_prompt"])'
            in source
        )


def test_no_duplicate_camera_forbidden_items_when_combined_with_composition():
    from hypogeum.video_prompt_writer import assemble_video_scene_prompt
    prompt = assemble_video_scene_prompt(
        "A stone corridor.",
        composition_forbidden_fn=lambda: ["whip_pan", "disconnected_scale_shot"],
    )
    forbidden_section = prompt[prompt.find("FORBIDDEN:"):]
    assert forbidden_section.count("whip_pan") == 1
