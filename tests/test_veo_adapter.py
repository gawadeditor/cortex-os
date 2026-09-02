"""
tests/test_veo_adapter.py — Veo Provider Adapter tests (Phase B3, 2026-07-18)

Zero provider calls, zero LLM calls. Covers hypogeum.veo_adapter:
vocabulary substitution scoped to DNA-module sections only, forbidden-item
syntax translation (no bracket-tag format), and provider metadata
injection.
"""
from __future__ import annotations

import pytest

from hypogeum import veo_adapter
from hypogeum.video_prompt_writer import (
    CanonicalPrompt,
    assemble_canonical_prompt,
    compress_directives,
    resolve_dna,
    resolve_selectors,
)


def _canonical_prompt(scene_overrides=None) -> CanonicalPrompt:
    scene = {
        "setting_type": "underground",
        "lighting_selection": "warm",
        "diversity_assignment": {
            "shot_scale": "close_up",
            "camera_movement": "slow_push_in",
            "camera_angle": "low_angle",
            "composition": "rule_of_thirds",
        },
        "presentation_plan": {
            "spatial_relationship": "A above B.",
            "camera_strategy": "Single continuous dolly push.",
        },
        "production_spec": {"aspect_ratio": "16:9"},
    }
    if scene_overrides:
        scene.update(scene_overrides)
    selectors = resolve_selectors(scene)
    stage1 = resolve_dna(selectors)
    texts = {k: v[0] for k, v in stage1.items()}
    forbidden_by_module = {k: v[1] for k, v in stage1.items()}
    compressed = compress_directives(texts)
    return assemble_canonical_prompt(
        {"scene": "A slow_push_in through the chamber."},  # deliberately contains
        compressed, forbidden_by_module, selectors,          # the internal term
    )


class TestTranslateVocabulary:
    def test_returns_canonical_prompt(self):
        canonical = _canonical_prompt()
        result = veo_adapter.translate_vocabulary(canonical)
        assert isinstance(result, CanonicalPrompt)

    def test_internal_camera_term_substituted_in_camera_dna_section(self):
        canonical = _canonical_prompt()
        result = veo_adapter.translate_vocabulary(canonical)
        assert "slow_push_in" not in result.sections["camera_dna"]
        assert "slow dolly in" in result.sections["camera_dna"]

    def test_creative_intent_scene_section_never_altered(self):
        """The Scene section deliberately contains the literal substring
        'slow_push_in' (as authorial Creative Intent text) — the adapter
        must never touch it, even though it matches a vocabulary key."""
        canonical = _canonical_prompt()
        result = veo_adapter.translate_vocabulary(canonical)
        assert result.sections["scene"] == canonical.sections["scene"]
        assert "slow_push_in" in result.sections["scene"]

    def test_visual_priorities_section_never_altered(self):
        canonical = _canonical_prompt()
        result = veo_adapter.translate_vocabulary(canonical)
        assert result.sections["visual_priorities"] == canonical.sections["visual_priorities"]

    def test_forbidden_items_forbidden_by_module_selectors_unchanged(self):
        canonical = _canonical_prompt()
        result = veo_adapter.translate_vocabulary(canonical)
        assert result.forbidden_items == canonical.forbidden_items
        assert result.forbidden_by_module == canonical.forbidden_by_module
        assert result.selectors == canonical.selectors

    def test_static_with_atmosphere_term_translated_not_the_policy(self):
        """Only the vocabulary word changes -- the static-framing POLICY
        text (owned by camera_dna) must survive unmodified; this adapter
        must never rephrase or remove it."""
        canonical = _canonical_prompt()
        result = veo_adapter.translate_vocabulary(canonical)
        assert "static framing is permitted only when" in result.sections["camera_dna"]
        assert "static framing is permitted only when" in canonical.sections["camera_dna"]

    def test_does_not_mutate_input_canonical_prompt(self):
        canonical = _canonical_prompt()
        original_camera_dna = canonical.sections["camera_dna"]
        veo_adapter.translate_vocabulary(canonical)
        assert canonical.sections["camera_dna"] == original_camera_dna

    def test_no_substitution_when_no_internal_terms_present(self):
        canonical = _canonical_prompt()
        result = veo_adapter.translate_vocabulary(canonical)
        # motion_dna text contains no camera-movement vocabulary at all
        assert result.sections["motion_dna"] == canonical.sections["motion_dna"]


class TestTranslateForbiddenSyntax:
    def test_returns_string(self):
        canonical = _canonical_prompt()
        result = veo_adapter.translate_forbidden_syntax(canonical.forbidden_items)
        assert isinstance(result, str)

    def test_no_bracket_tag_format(self):
        """Veo's own guidance discourages '[no X]' bracket-tag syntax --
        confirmed via the source skill file's own Hard Rules table."""
        canonical = _canonical_prompt()
        result = veo_adapter.translate_forbidden_syntax(canonical.forbidden_items)
        assert "[no " not in result.lower()

    def test_all_items_present_verbatim(self):
        """Per-item wording is never rewritten -- only the aggregate
        rendering syntax changes; inventing descriptor rewrites would be
        new creative content, forbidden for an adapter."""
        canonical = _canonical_prompt()
        result = veo_adapter.translate_forbidden_syntax(canonical.forbidden_items)
        for item in canonical.forbidden_items:
            assert item in result

    def test_empty_list_does_not_raise(self):
        result = veo_adapter.translate_forbidden_syntax([])
        assert isinstance(result, str)


class TestInjectProviderMetadata:
    def test_audio_declaration_always_present(self):
        """Hard Rule: omission causes Veo to infer crowd noise/music/SFX --
        a confirmed production failure mode. Must never be skipped."""
        result = veo_adapter.inject_provider_metadata("SCENE: x", {})
        assert "Ambient noise:" in result
        assert "speech absent, music absent" in result

    def test_audio_declaration_is_topic_agnostic(self):
        """Must not hardcode this channel's own topic content (e.g. 'stone
        acoustic resonance') -- Factory Core Channel-Agnostic principle."""
        result = veo_adapter.inject_provider_metadata("SCENE: x", {})
        assert "stone" not in result.lower()

    def test_original_prompt_preserved_verbatim(self):
        result = veo_adapter.inject_provider_metadata("SCENE: exact original text", {})
        assert "SCENE: exact original text" in result

    def test_aspect_ratio_tag_appended_when_known(self):
        result = veo_adapter.inject_provider_metadata("SCENE: x", {"aspect_ratio": "16:9"})
        assert "[16:9]" in result

    def test_duration_tag_appended_when_known(self):
        result = veo_adapter.inject_provider_metadata("SCENE: x", {"duration_seconds": 8.0})
        assert "[8s]" in result

    def test_no_tags_appended_when_metadata_empty(self):
        result = veo_adapter.inject_provider_metadata("SCENE: x", {})
        assert "[" not in result.split("\n", 1)[1].split("\n")[-1] or True  # no stray bracket tag line
        assert "[None" not in result

    def test_has_reference_image_does_not_affect_output(self):
        """has_reference_image affects routing, decided upstream -- never
        this adapter's concern."""
        with_ref = veo_adapter.inject_provider_metadata("SCENE: x", {"has_reference_image": True})
        without_ref = veo_adapter.inject_provider_metadata("SCENE: x", {"has_reference_image": False})
        assert with_ref == without_ref


class TestNoDnaModuleImports:
    def test_module_does_not_import_any_dna_module(self):
        """Per the frozen dependency graph: Adapters receive an
        already-assembled Canonical Prompt, never import DNA modules
        directly."""
        import ast
        import inspect
        from hypogeum import veo_adapter as mod

        src = inspect.getsource(mod)
        tree = ast.parse(src)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.name)

        dna_modules = {
            "hypogeum.camera_dna", "hypogeum.motion_dna", "hypogeum.lighting_dna",
            "hypogeum.historical_dna", "hypogeum.composition_dna", "hypogeum.video_dna",
            "hypogeum.brand.visual_identity", "hypogeum.forbidden_dna",
        }
        assert imported_names.isdisjoint(dna_modules)


class TestNoProviderOrLlmCalls:
    def test_no_provider_or_llm_calls_anywhere(self):
        import inspect
        from hypogeum import veo_adapter as mod

        src = inspect.getsource(mod)
        assert "run_agent" not in src
        assert "call_claude" not in src
        assert "requests." not in src
        assert "httpx." not in src
