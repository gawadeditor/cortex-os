"""
tests/test_forbidden_dna.py — Phase B2.2: Forbidden DNA extraction.

Covers hypogeum.forbidden_dna's public API (per
docs/validation/prompt_writer_architecture/B1_3_ARCHITECTURE_CONTRACT.md),
its wiring into hypogeum.video_prompt_writer.assemble_video_scene_prompt(),
and Objective 4's provider-separation checks. Zero provider calls anywhere
in this file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hypogeum.forbidden_dna import collect_forbidden_items


# ---------------------------------------------------------------------------
# Public API matches the frozen contract
# ---------------------------------------------------------------------------

class TestForbiddenDnaPublicApi:
    def test_signature_is_variadic_sources_returning_list(self):
        import inspect
        sig = inspect.signature(collect_forbidden_items)
        params = list(sig.parameters.values())
        assert len(params) == 1
        assert params[0].kind == inspect.Parameter.VAR_POSITIONAL
        assert params[0].name == "sources"

    def test_no_content_of_its_own(self):
        # Forbidden DNA owns no rules — calling it with zero sources
        # returns an empty list, never a hardcoded default.
        assert collect_forbidden_items() == []


# ---------------------------------------------------------------------------
# Stable deterministic ordering + deduplication
# ---------------------------------------------------------------------------

class TestForbiddenDnaOrderingAndDeduplication:
    def test_order_preserved_first_occurrence_wins(self):
        result = collect_forbidden_items(["a", "b"], ["c", "a"], ["d"])
        assert result == ["a", "b", "c", "d"]

    def test_duplicates_within_one_source_collapsed(self):
        result = collect_forbidden_items(["a", "a", "b"])
        assert result == ["a", "b"]

    def test_duplicates_across_sources_collapsed(self):
        result = collect_forbidden_items(["talking_head"], ["talking_head", "fluorescent"])
        assert result == ["talking_head", "fluorescent"]

    def test_deterministic_across_repeated_calls(self):
        args = (["a", "b"], ["c"])
        assert collect_forbidden_items(*args) == collect_forbidden_items(*args)


# ---------------------------------------------------------------------------
# Defensive-copy behavior
# ---------------------------------------------------------------------------

class TestForbiddenDnaDefensiveCopy:
    def test_returned_list_is_not_a_reference_to_any_source(self):
        source = ["a", "b"]
        result = collect_forbidden_items(source)
        result.append("MUTATED")
        assert "MUTATED" not in source

    def test_mutating_source_after_call_does_not_affect_prior_result(self):
        source = ["a", "b"]
        result = collect_forbidden_items(source)
        source.append("c")
        assert result == ["a", "b"]

    def test_two_calls_return_independent_list_objects(self):
        source = ["a"]
        r1 = collect_forbidden_items(source)
        r2 = collect_forbidden_items(source)
        assert r1 is not r2
        r1.append("MUTATED")
        assert "MUTATED" not in r2


# ---------------------------------------------------------------------------
# Aggregation from real DNA modules
# ---------------------------------------------------------------------------

class TestForbiddenDnaAggregatesRealSources:
    def test_aggregates_video_dna_forbidden_items(self):
        from hypogeum.video_dna import get_video_dna_forbidden_items
        result = collect_forbidden_items(get_video_dna_forbidden_items())
        assert "talking_head" in result

    def test_aggregates_brand_dna_forbidden_items(self):
        from hypogeum.brand.visual_identity import get_adaptive_video_forbidden_items
        brand_items = get_adaptive_video_forbidden_items()
        result = collect_forbidden_items(brand_items)
        for item in brand_items:
            assert item in result

    def test_aggregates_composition_dna_forbidden_items(self):
        from hypogeum.composition_dna import get_composition_forbidden_items
        result = collect_forbidden_items(get_composition_forbidden_items())
        assert "disconnected_scale_shot" in result
        assert "abstract_size_text" in result

    def test_aggregates_all_three_together_deduplicated(self):
        from hypogeum.video_dna import get_video_dna_forbidden_items
        from hypogeum.brand.visual_identity import get_adaptive_video_forbidden_items
        from hypogeum.composition_dna import get_composition_forbidden_items

        video_items = get_video_dna_forbidden_items()
        brand_items = get_adaptive_video_forbidden_items()
        comp_items = get_composition_forbidden_items()
        result = collect_forbidden_items(video_items, comp_items, brand_items)

        assert len(result) == len(set(result))  # no duplicates survive
        for item in video_items + comp_items + brand_items:
            assert item in result


# ---------------------------------------------------------------------------
# Empty / missing optional sources degrade safely
# ---------------------------------------------------------------------------

class TestForbiddenDnaSafeDegradation:
    def test_empty_list_source_skipped(self):
        assert collect_forbidden_items(["a"], [], ["b"]) == ["a", "b"]

    def test_none_source_skipped_without_error(self):
        assert collect_forbidden_items(["a"], None, ["b"]) == ["a", "b"]

    def test_all_empty_or_none_returns_empty_list(self):
        assert collect_forbidden_items([], None, []) == []

    def test_zero_sources_returns_empty_list(self):
        assert collect_forbidden_items() == []


# ---------------------------------------------------------------------------
# A future DNA source is addable without redesigning the module
# ---------------------------------------------------------------------------

class TestForbiddenDnaExtensibility:
    def test_more_than_three_sources_supported(self):
        result = collect_forbidden_items(
            ["camera_x"], ["motion_y"], ["lighting_z"], ["historical_w"], ["brand_v"],
        )
        assert result == ["camera_x", "motion_y", "lighting_z", "historical_w", "brand_v"]

    def test_arbitrary_number_of_sources_via_unpacking(self):
        # Simulates a future Compiler passing N DNA modules' forbidden
        # lists without forbidden_dna.py needing any signature change.
        future_sources = [["a"], ["b"], ["c"], ["d"], ["e"], ["f"], ["a"]]
        result = collect_forbidden_items(*future_sources)
        assert result == ["a", "b", "c", "d", "e", "f"]


# ---------------------------------------------------------------------------
# Objective 4 — provider separation
# ---------------------------------------------------------------------------

class TestForbiddenDnaProviderSeparation:
    """
    Forbidden DNA returns canonical forbidden concepts only — it must never
    emit provider-specific negative syntax, choose a provider, or translate
    vocabulary. That remains deferred to B3 Provider Adapters.
    """

    def test_output_contains_no_bracket_negative_syntax(self):
        result = collect_forbidden_items(["talking_head", "[no vignette]"])
        # The function must not itself ADD bracket syntax — whatever comes
        # in comes out unchanged; it never wraps/rewrites items.
        assert result == ["talking_head", "[no vignette]"]  # passthrough, not synthesis

    def test_function_body_contains_no_bracket_tag_generation(self):
        import inspect
        body = inspect.getsource(collect_forbidden_items)
        assert "[no " not in body
        assert 'f"[' not in body

    def test_function_body_names_no_provider(self):
        import inspect
        body = inspect.getsource(collect_forbidden_items).lower()
        for term in ("veo", "seedance", "muapi", "byteplus"):
            assert term not in body, f"collect_forbidden_items() must not reference provider {term!r}"

    def test_function_body_has_no_string_formatting_or_rendering(self):
        # Confirms this module returns a bare list, never a joined/labeled
        # string — rendering stays hypogeum.video_prompt_writer's job today.
        import inspect
        body = inspect.getsource(collect_forbidden_items)
        assert '"; "' not in body
        assert "FORBIDDEN:" not in body
        assert ".join(" not in body

    def test_no_provider_calls_possible(self):
        source = Path("hypogeum/forbidden_dna.py").read_text()
        assert "import muapi" not in source
        assert "from hypogeum.muapi import" not in source
        assert "run_agent(" not in source
        assert "requests" not in source
        assert "urllib" not in source


# ---------------------------------------------------------------------------
# Existing video_prompt_writer output unchanged for the current call path
# ---------------------------------------------------------------------------

class TestVideoPromptWriterUsesForbiddenDna:
    def test_forbidden_dna_imported_and_used(self):
        # Substring, not an exact import-line match — the combined import
        # statement gained camera_dna/motion_dna in later checkpoints
        # (B2.4, B2.5), so this checks module usage, not exact line text.
        import hypogeum.video_prompt_writer as vpw
        assert vpw.forbidden_dna.__name__ == "hypogeum.forbidden_dna"
        source = Path("hypogeum/video_prompt_writer.py").read_text()
        assert "forbidden_dna.collect_forbidden_items(" in source
        # The old inline dedup loop must be gone, not duplicated alongside.
        assert "if item not in forbidden_items:" not in source

    def test_assemble_video_scene_prompt_forbidden_section_unchanged(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        assert "FORBIDDEN:" in prompt
        forbidden_section = prompt[prompt.find("FORBIDDEN:"):]
        assert "talking_head" in forbidden_section  # Video DNA content still present
        # Still exactly one FORBIDDEN section, still semicolon-joined.
        assert prompt.count("FORBIDDEN:") == 1
        assert "; " in forbidden_section

    def test_no_duplicate_items_in_rendered_forbidden_section(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt(
            "A stone corridor.",
            composition_forbidden_fn=lambda: ["talking_head", "disconnected_scale_shot"],
        )
        forbidden_section = prompt[prompt.find("FORBIDDEN:"):]
        assert forbidden_section.count("talking_head") == 1


def test_vpd_scene_prompts_batch_schema_still_valid_after_forbidden_dna_extraction():
    """
    Regression check mirroring the B1.2A/B2.1 pattern: this extraction
    touches only internal aggregation logic, not the vpd_scene_prompts_batch
    OUTPUT schema — confirmed by actually validating a representative sample.
    """
    from hypogeum.schemas import validate

    sample = {
        "scene_prompts": [
            {
                "scene_id": "s01",
                "primary_prompt": "A stone corridor, warm torchlight raking across the walls.",
                "fallback_prompt": "A dim stone corridor lit by a single torch.",
            }
        ]
    }
    validate("vpd_scene_prompts_batch", sample)  # must not raise
