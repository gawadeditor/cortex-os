"""
tests/test_video_dna_validation_suite.py — Video DNA Validation Suite v1 tests (2026-07-14)

Covers hypogeum.video_dna_validation_suite and hypogeum.video_dna_validator.
Zero provider calls anywhere in this file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hypogeum.video_dna_validation_suite import get_all_scenes, get_scene, get_categories, SUITE_VERSION
from hypogeum.video_dna_validator import (
    build_submission_prompt, validate_scene_structural, run_structural_validation,
)

_REQUIRED_FIELDS = (
    "validation_id", "category", "objective", "what_is_tested",
    "success_criteria", "failure_patterns", "canonical_prompt",
    "expected_visual_behavior",
)

_REQUIRED_CATEGORIES = (
    "discovery_through_movement", "discovery_through_light", "discovery_through_dust",
    "discovery_through_shadow", "object_reveal", "human_interaction_with_artifact",
    "slow_environmental_motion", "camera_push_in", "camera_orbit", "vertical_reveal",
    "scale_reveal", "interior_ancient_architecture", "exterior_archaeological_ruins",
    "underground_chamber", "mechanism_detail", "ancient_writing", "material_realism",
    "transition_consistency",
)

_EPISODE_SPECIFIC_TERMS = ("antikythera", "ep_003", "ep003", "hypogeum chronicles")


class TestSuiteSize:
    def test_scene_count_in_range(self):
        scenes = get_all_scenes()
        assert 12 <= len(scenes) <= 20

    def test_suite_version_is_v1(self):
        assert SUITE_VERSION == "v1"


class TestCategoryCoverage:
    def test_every_required_category_present(self):
        categories = set(get_categories())
        for required in _REQUIRED_CATEGORIES:
            assert required in categories, f"missing required category: {required}"

    def test_no_duplicate_validation_ids(self):
        ids = [s["validation_id"] for s in get_all_scenes()]
        assert len(ids) == len(set(ids))

    def test_get_scene_by_id_works(self):
        scene = get_scene("camera_push_in")
        assert scene["category"] == "camera_push_in"


class TestSceneStructure:
    @pytest.mark.parametrize("scene", get_all_scenes(), ids=lambda s: s["validation_id"])
    def test_every_scene_has_all_required_fields(self, scene):
        for field in _REQUIRED_FIELDS:
            assert field in scene and scene[field], f"{scene['validation_id']} missing {field}"

    @pytest.mark.parametrize("scene", get_all_scenes(), ids=lambda s: s["validation_id"])
    def test_success_criteria_and_failure_patterns_are_nonempty_lists(self, scene):
        assert isinstance(scene["success_criteria"], list) and len(scene["success_criteria"]) >= 2
        assert isinstance(scene["failure_patterns"], list) and len(scene["failure_patterns"]) >= 2


class TestSceneTopicAgnostic:
    @pytest.mark.parametrize("scene", get_all_scenes(), ids=lambda s: s["validation_id"])
    def test_no_episode_specific_wording(self, scene):
        lowered = scene["canonical_prompt"].lower()
        for term in _EPISODE_SPECIFIC_TERMS:
            assert term not in lowered, f"{scene['validation_id']} contains episode-specific term {term!r}"

    @pytest.mark.parametrize("scene", get_all_scenes(), ids=lambda s: s["validation_id"])
    def test_no_dialogue_narration_subtitle_logo_requested(self, scene):
        lowered = scene["canonical_prompt"].lower()
        for bad in ("with dialogue", "narrated by", "subtitle reads", "logo appears",
                    "displays a logo", "character says"):
            assert bad not in lowered

    @pytest.mark.parametrize("scene", get_all_scenes(), ids=lambda s: s["validation_id"])
    def test_scene_explicitly_disclaims_forbidden_elements(self, scene):
        lowered = scene["canonical_prompt"].lower()
        assert any(term in lowered for term in ("no dialogue", "no text", "no modern"))

    def test_all_scenes_test_different_categories(self):
        # "Each scene must test a different cinematic behavior rather than a
        # different topic" — categories must be unique across the suite.
        categories = get_categories()
        assert len(categories) == len(set(categories))


class TestNoProviderCallsPossible:
    def test_suite_module_makes_no_provider_calls(self):
        source = Path("hypogeum/video_dna_validation_suite.py").read_text()
        assert "import muapi" not in source
        assert "from hypogeum.muapi import" not in source
        assert "run_agent(" not in source

    def test_validator_module_does_not_call_submit_scene(self):
        # AST-based check, not a raw substring scan — the module's own
        # docstring legitimately mentions submit_scene() as documentation
        # of the assembly order it mirrors; that is not a call.
        import ast
        tree = ast.parse(Path("hypogeum/video_dna_validator.py").read_text())
        called_names = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "submit_scene" not in called_names
        assert "_generate_thumbnail_with_model" not in called_names


class TestValidatorAssemblyOrderMatchesProduction:
    def test_build_submission_prompt_matches_submit_scene_order(self):
        # 2026-07-14 Prompt Writer architecture revision: structured
        # section assembly via hypogeum.video_prompt_writer, matching
        # hypogeum.muapi.submit_scene()'s own call exactly.
        scene = get_scene("camera_push_in")
        prompt = build_submission_prompt(scene)
        assert prompt.startswith(f"SCENE: {scene['canonical_prompt']}")
        assert (
            prompt.find("VISUAL PRIORITIES:") < prompt.find("VIDEO DNA:")
            < prompt.find("BRAND DNA:") < prompt.find("FORBIDDEN:")
        )
        for label in ("SCENE:", "VISUAL PRIORITIES:", "VIDEO DNA:", "BRAND DNA:", "FORBIDDEN:"):
            assert prompt.count(label) == 1

    def test_validate_scene_structural_returns_hash_and_length(self):
        scene = get_scene("camera_push_in")
        result = validate_scene_structural(scene)
        assert result["passed"] is True
        assert result["prompt_length"] > 0
        assert len(result["prompt_sha256"]) == 64


class TestSuiteValidatesAgainstCurrentVideoDna:
    def test_full_suite_passes_against_video_dna_v1(self):
        result = run_structural_validation()
        assert result["passed"] is True
        assert result["scene_count"] == len(get_all_scenes())
        assert result["failed_validation_ids"] == []
        assert result["video_dna_version"] == "v1"


class TestVersionComparisonCapability:
    def test_different_video_dna_directive_produces_different_hashes(self):
        # Proves the validator supports comparing Video DNA versions: the
        # SAME suite, run against two different directive functions,
        # produces different prompt hashes while every canonical_prompt
        # stays byte-identical.
        def _fake_v2_directive():
            return " — HYPOGEUM VIDEO DNA v2: a deliberately different directive for comparison testing."

        result_v1 = run_structural_validation(video_dna_version="v1")
        result_v2 = run_structural_validation(
            video_dna_directive_fn=_fake_v2_directive, video_dna_version="v2",
        )

        assert result_v1["video_dna_version"] == "v1"
        assert result_v2["video_dna_version"] == "v2"

        hashes_v1 = {r["validation_id"]: r["prompt_sha256"] for r in result_v1["results"]}
        hashes_v2 = {r["validation_id"]: r["prompt_sha256"] for r in result_v2["results"]}
        for vid in hashes_v1:
            assert hashes_v1[vid] != hashes_v2[vid]

    def test_canonical_prompts_unchanged_across_versions(self):
        # The suite itself never changes between Video DNA versions — only
        # the directive appended to it does.
        def _fake_v2_directive():
            return " — HYPOGEUM VIDEO DNA v2: different."

        scene = get_scene("camera_push_in")
        prompt_v1 = build_submission_prompt(scene)
        prompt_v2 = build_submission_prompt(scene, video_dna_directive_fn=_fake_v2_directive)
        expected_prefix = f"SCENE: {scene['canonical_prompt']}"
        assert prompt_v1.startswith(expected_prefix)
        assert prompt_v2.startswith(expected_prefix)

    def test_custom_brand_directive_also_swappable(self):
        def _fake_brand():
            return " — FAKE BRAND DIRECTIVE."

        scene = get_scene("object_reveal")
        prompt = build_submission_prompt(scene, brand_directive_fn=_fake_brand)
        assert "FAKE BRAND DIRECTIVE" in prompt
        assert prompt.startswith(f"SCENE: {scene['canonical_prompt']}")


class TestPromptLengthSafeForProvider:
    @pytest.mark.parametrize("scene", get_all_scenes(), ids=lambda s: s["validation_id"])
    def test_every_scene_prompt_under_safe_ceiling(self, scene):
        prompt = build_submission_prompt(scene)
        assert len(prompt) < 4500
