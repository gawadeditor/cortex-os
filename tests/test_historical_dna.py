"""
tests/test_historical_dna.py — Phase B2.7: Historical Authenticity DNA
extraction.

Covers hypogeum.historical_dna's public API (per
docs/validation/prompt_writer_architecture/B1_3_ARCHITECTURE_CONTRACT.md),
proof that its content matches what video_dna.py/cinematic_prompt_
architecture.md used to carry, its default-on wiring (via the call-site
"unspecified" fallback, since setting_type is a required parameter) into
hypogeum.video_prompt_writer.assemble_video_scene_prompt(), and
responsibility-leakage checks mirroring test_lighting_dna.py's pattern.
Zero provider calls anywhere in this file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hypogeum.historical_dna import (
    get_historical_dna_rules, describe_historical_constraints,
    get_historical_forbidden_items,
)


# ---------------------------------------------------------------------------
# Public API matches the frozen contract
# ---------------------------------------------------------------------------

class TestHistoricalDnaPublicApi:
    def test_get_historical_dna_rules_returns_dict(self):
        assert isinstance(get_historical_dna_rules(), dict)

    def test_describe_historical_constraints_signature(self):
        import inspect
        sig = inspect.signature(describe_historical_constraints)
        params = list(sig.parameters.values())
        assert params[0].name == "setting_type"
        assert params[0].default is inspect.Parameter.empty  # required, no default
        assert sig.parameters["historical_period"].default is None

    def test_get_historical_forbidden_items_signature(self):
        import inspect
        sig = inspect.signature(get_historical_forbidden_items)
        params = list(sig.parameters.values())
        assert params[0].name == "setting_type"
        assert params[0].default is inspect.Parameter.empty  # required, no default

    def test_get_historical_forbidden_items_returns_list(self):
        items = get_historical_forbidden_items("underwater")
        assert isinstance(items, list)
        assert all(isinstance(i, str) for i in items)


# ---------------------------------------------------------------------------
# Deterministic, no mutable global state exposed
# ---------------------------------------------------------------------------

class TestHistoricalDnaDeterminism:
    def test_rules_call_is_deterministic(self):
        assert get_historical_dna_rules() == get_historical_dna_rules()

    def test_directive_call_is_deterministic(self):
        a = describe_historical_constraints(setting_type="underwater")
        b = describe_historical_constraints(setting_type="underwater")
        assert a == b

    def test_forbidden_items_call_is_deterministic(self):
        assert get_historical_forbidden_items("interior") == get_historical_forbidden_items("interior")

    def test_mutating_returned_rules_does_not_affect_module_state(self):
        rules = get_historical_dna_rules()
        rules["plausibility_rule"] = "MUTATED"
        rules["anachronism_library"]["underwater"].append("MUTATED")
        fresh = get_historical_dna_rules()
        assert fresh["plausibility_rule"] != "MUTATED"
        assert "MUTATED" not in fresh["anachronism_library"]["underwater"]

    def test_mutating_returned_forbidden_items_does_not_affect_module_state(self):
        items = get_historical_forbidden_items("figure")
        items.append("MUTATED")
        assert "MUTATED" not in get_historical_forbidden_items("figure")


# ---------------------------------------------------------------------------
# Content matches what video_dna.py / cinematic_prompt_architecture.md used
# to carry
# ---------------------------------------------------------------------------

class TestExtractedContentMatchesOriginal:
    def test_plausibility_rule_unchanged_from_pre_extraction_wording(self):
        assert get_historical_dna_rules()["plausibility_rule"] == (
            "artifacts, architecture, and environments are physically "
            "plausible for their stated historical period"
        )

    def test_material_pattern_is_generalized_pattern_not_worked_example(self):
        pattern = get_historical_dna_rules()["material_pattern"]
        # Must describe the four-observation STRUCTURE, never Antikythera's
        # own worked example content (canvas shirt, hemp rope, limestone
        # cliff, Aegean water — none of that belongs in a topic-agnostic
        # Factory component).
        for topic_term in ("canvas", "hemp rope", "limestone", "aegean", "diver"):
            assert topic_term not in pattern.lower()
        assert "primary surface" in pattern
        assert "precision detail" in pattern

    def test_positive_directive_contains_no_negative_language(self):
        for directive in (
            describe_historical_constraints(setting_type="unspecified"),
            describe_historical_constraints(setting_type="underwater", historical_period="Bronze Age"),
        ):
            lowered = directive.lower()
            for negative_word in (" never ", " no ", " avoid", " forbidden", " don't", " do not "):
                assert negative_word not in lowered, f"found negative language: {negative_word!r} in {directive!r}"


# ---------------------------------------------------------------------------
# setting_type-scoped anachronism library
# ---------------------------------------------------------------------------

class TestAnachronismLibrary:
    def test_six_categories_plus_unspecified(self):
        library = get_historical_dna_rules()["anachronism_library"]
        assert set(library) == {
            "underwater", "interior", "artifact_macro", "figure",
            "exterior", "underground", "unspecified",
        }

    def test_directly_evidenced_categories_nonempty(self):
        # Named in cinematic_prompt_architecture.md Layer 9's own generic
        # guidance line + worked examples.
        for setting_type in ("underwater", "interior", "artifact_macro", "figure"):
            items = get_historical_forbidden_items(setting_type)
            assert len(items) > 0, f"{setting_type} should have evidenced anachronism items"

    def test_extrapolated_categories_still_nonempty(self):
        # exterior/underground have weaker direct textual support but are
        # still populated, not left empty — see module docstring.
        for setting_type in ("exterior", "underground"):
            items = get_historical_forbidden_items(setting_type)
            assert len(items) > 0

    def test_unspecified_returns_empty_list_not_a_guess(self):
        assert get_historical_forbidden_items("unspecified") == []

    def test_unrecognised_setting_type_degrades_safely_to_empty_list(self):
        assert get_historical_forbidden_items("not_a_real_setting_type") == []

    def test_museum_lighting_scoped_to_artifact_macro_only(self):
        assert "museum lighting" in get_historical_forbidden_items("artifact_macro")
        assert "museum lighting" not in get_historical_forbidden_items("underwater")
        assert "museum lighting" not in get_historical_forbidden_items("figure")

    def test_modern_watercraft_scoped_to_underwater_only(self):
        assert "modern watercraft" in get_historical_forbidden_items("underwater")
        assert "modern watercraft" not in get_historical_forbidden_items("interior")

    def test_no_duplicated_items_within_one_category(self):
        library = get_historical_dna_rules()["anachronism_library"]
        for setting_type, items in library.items():
            assert len(items) == len(set(items)), f"{setting_type} has duplicate items"


# ---------------------------------------------------------------------------
# setting_type does not alter the positive directive text (only scopes
# forbidden items, per the frozen contract)
# ---------------------------------------------------------------------------

class TestSettingTypeDoesNotAlterPositiveDirective:
    def test_directive_identical_across_setting_types(self):
        directive_a = describe_historical_constraints(setting_type="underwater")
        directive_b = describe_historical_constraints(setting_type="interior")
        directive_c = describe_historical_constraints(setting_type="unspecified")
        assert directive_a == directive_b == directive_c

    def test_historical_period_rendered_when_present(self):
        directive = describe_historical_constraints(
            setting_type="unspecified", historical_period="Bronze Age",
        )
        assert "Bronze Age" in directive

    def test_historical_period_absent_by_default(self):
        directive = describe_historical_constraints(setting_type="unspecified")
        assert "Historical period" not in directive


# ---------------------------------------------------------------------------
# Responsibility leakage checks
# ---------------------------------------------------------------------------

class TestHistoricalDnaDoesNotLeakOtherDnaResponsibilities:
    @pytest.fixture
    def full_text(self):
        rules = get_historical_dna_rules()
        directive = describe_historical_constraints(
            setting_type="underwater", historical_period="Bronze Age",
        )
        all_forbidden = []
        for setting_type in rules["anachronism_library"]:
            all_forbidden += get_historical_forbidden_items(setting_type)
        return (str(rules) + directive + str(all_forbidden)).lower()

    def test_no_camera_vocabulary(self, full_text):
        for term in ("slow_push_in", "static_with_atmosphere", "camera_movement", "whip_pan"):
            assert term not in full_text, f"leaked camera vocabulary: {term!r}"

    def test_no_motion_vocabulary(self, full_text):
        for term in ("motion comes from physical action", "speed_ramp", "snap_cut"):
            assert term not in full_text, f"leaked motion vocabulary: {term!r}"

    def test_no_lighting_vocabulary(self, full_text):
        # Explicit non-responsibility: "does NOT own lighting vocabulary
        # even for period light sources" — cross-referenced only, never
        # restated.
        for term in ("torchlight", "falloff", "amber directional", "oil lamp"):
            assert term not in full_text, f"leaked lighting vocabulary: {term!r}"

    def test_no_composition_vocabulary(self, full_text):
        for term in ("rule_of_thirds", "negative_space_dominant", "diagonal_leading_line"):
            assert term not in full_text, f"leaked composition vocabulary: {term!r}"

    def test_no_brand_content(self, full_text):
        import re
        assert re.search(r"#[0-9a-f]{6}\b", full_text) is None
        for term in ("aged_gold", "near_black", "wordmark"):
            assert term not in full_text

    def test_no_provider_specific_terminology(self, full_text):
        for term in ("veo", "seedance", "muapi", "byteplus"):
            assert term not in full_text, f"leaked provider terminology: {term!r}"

    def test_no_topic_or_episode_names(self, full_text):
        for term in ("antikythera", "ep_", "episode "):
            assert term not in full_text

    def test_no_provider_calls_possible(self):
        source = Path("hypogeum/historical_dna.py").read_text()
        assert "import muapi" not in source
        assert "from hypogeum.muapi import" not in source
        assert "run_agent(" not in source
        assert "requests" not in source
        assert "urllib" not in source

    def test_no_agent_or_episode_metadata_imports(self):
        # Frozen contract: "must never read episode_metadata directly or
        # import anything from hypogeum/agents/." Checks actual import
        # statements only — the module's own docstring legitimately
        # *mentions* hypogeum.agents.scene_planner as a discipline
        # reference, which is not an import and must not fail this check.
        # Parse actual import statements via ast, not substring matching —
        # the module's own docstring legitimately mentions
        # hypogeum.agents.scene_planner as a discipline reference, which
        # must not fail a naive substring check.
        import ast
        source = Path("hypogeum/historical_dna.py").read_text()
        tree = ast.parse(source)
        imported_modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported_modules.append(node.module or "")
        assert not any(m.startswith("hypogeum.agents") for m in imported_modules)


# ---------------------------------------------------------------------------
# Wiring into video_prompt_writer.assemble_video_scene_prompt() — default ON
# via a call-site "unspecified" fallback (setting_type has no default in
# the module's own frozen signature)
# ---------------------------------------------------------------------------

class TestHistoricalDnaWiring:
    def test_historical_section_present_by_default(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        assert "HISTORICAL DNA:" in prompt

    def test_historical_section_content_matches_module(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        historical_section = prompt[prompt.find("HISTORICAL DNA:"):prompt.find("BRAND DNA:")]
        assert "physically plausible" in historical_section

    def test_historical_section_ordered_after_lighting_before_brand(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        assert (
            prompt.find("LIGHTING DNA:") < prompt.find("HISTORICAL DNA:") < prompt.find("BRAND DNA:")
        )

    def test_default_call_contributes_no_setting_specific_forbidden_items(self):
        # Default fallback is setting_type="unspecified" -> empty list,
        # per the module's own honest-degradation design. Checked against
        # items exclusive to Historical DNA's own anachronism library —
        # "museum lighting" is deliberately excluded from this check since
        # it is ALSO a legitimate, independent Lighting DNA forbidden item
        # (a lighting-quality concern, not an anachronistic-context
        # concern) and renders regardless of Historical DNA's setting_type;
        # forbidden_dna's dedup correctly collapses the two into one
        # occurrence when both would otherwise contribute it — see
        # test_museum_lighting_scoped_to_artifact_macro_only for the
        # Historical-DNA-only proof that it's still setting-scoped there.
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt("A stone corridor.")
        forbidden_section = prompt[prompt.find("FORBIDDEN:"):]
        for item in ("modern watercraft", "synthetic wetsuit material", "contemporary excavation equipment"):
            assert item not in forbidden_section

    def test_override_callable_replaces_default_directive(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt(
            "A stone corridor.",
            historical_directive_fn=lambda: "OVERRIDE HISTORICAL TEXT.",
        )
        historical_section = prompt[prompt.find("HISTORICAL DNA:"):prompt.find("BRAND DNA:")]
        assert "OVERRIDE HISTORICAL TEXT." in historical_section
        assert "physically plausible" not in historical_section

    def test_override_callable_can_supply_real_setting_type(self):
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt
        prompt = assemble_video_scene_prompt(
            "A stone corridor.",
            historical_forbidden_fn=lambda: get_historical_forbidden_items("artifact_macro"),
        )
        forbidden_section = prompt[prompt.find("FORBIDDEN:"):]
        assert "museum lighting" in forbidden_section

    def test_submit_scene_call_shape_unaffected(self):
        import hypogeum.muapi as muapi
        source = Path(muapi.__file__).read_text()
        assert (
            'video_prompt_writer.assemble_video_scene_prompt(prod_spec["primary_prompt"])'
            in source
        )


def test_no_duplicate_historical_forbidden_items_when_combined_with_composition():
    from hypogeum.video_prompt_writer import assemble_video_scene_prompt
    prompt = assemble_video_scene_prompt(
        "A stone corridor.",
        historical_forbidden_fn=lambda: get_historical_forbidden_items("artifact_macro"),
        composition_forbidden_fn=lambda: ["museum lighting", "disconnected_scale_shot"],
    )
    forbidden_section = prompt[prompt.find("FORBIDDEN:"):]
    assert forbidden_section.count("museum lighting") == 1
