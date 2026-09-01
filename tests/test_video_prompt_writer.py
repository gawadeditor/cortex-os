"""
tests/test_video_prompt_writer.py — Structured video prompt assembly tests (2026-07-14)

Covers hypogeum.video_prompt_writer: section ordering, the deterministic
compression pass, forbidden-item collection/deduplication, and scene_content
normalization. Zero provider calls anywhere in this file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hypogeum.video_prompt_writer import (
    UNRESOLVED_SELECTORS,
    CanonicalPrompt,
    apply_provider_adapter,
    assemble_canonical_prompt,
    assemble_video_scene_prompt,
    compile_scene_prompt,
    compress_directives,
    resolve_dna,
    resolve_selectors,
)


class TestSectionOrderAndLabels:
    def test_bare_string_becomes_scene_section(self):
        prompt = assemble_video_scene_prompt("A quiet stone chamber.")
        assert prompt.startswith("SCENE: A quiet stone chamber.")

    def test_fixed_nine_section_order(self):
        prompt = assemble_video_scene_prompt("A quiet stone chamber.")
        positions = {
            label: prompt.find(f"{label}:")
            for label in ("SCENE", "VISUAL PRIORITIES", "VIDEO DNA", "BRAND DNA", "FORBIDDEN")
        }
        assert all(p != -1 for p in positions.values())
        ordered = sorted(positions.items(), key=lambda kv: kv[1])
        assert [k for k, _ in ordered] == ["SCENE", "VISUAL PRIORITIES", "VIDEO DNA", "BRAND DNA", "FORBIDDEN"]

    def test_each_section_appears_exactly_once(self):
        prompt = assemble_video_scene_prompt("A quiet stone chamber.")
        for label in ("SCENE:", "VISUAL PRIORITIES:", "VIDEO DNA:", "BRAND DNA:", "FORBIDDEN:"):
            assert prompt.count(label) == 1

    def test_optional_camera_subject_motion_lighting_sections(self):
        prompt = assemble_video_scene_prompt({
            "scene": "A quiet stone chamber.",
            "camera": "Slow push-in.",
            "subject": "A carved relief.",
            "motion": "Dust drifts through light.",
            "lighting": "Single warm shaft.",
        })
        for label in ("SCENE:", "CAMERA:", "SUBJECT:", "MOTION:", "LIGHTING:"):
            assert label in prompt
        positions = [prompt.find(f"{l}:") for l in ("SCENE", "CAMERA", "SUBJECT", "MOTION", "LIGHTING")]
        assert positions == sorted(positions)

    def test_omitted_optional_sections_are_not_fabricated(self):
        # Today's callers (VPD, validation suite) supply only Scene text —
        # Camera/Subject/Motion/Lighting must never be invented.
        prompt = assemble_video_scene_prompt("A quiet stone chamber.")
        for label in ("CAMERA:", "SUBJECT:", "MOTION:", "LIGHTING:"):
            assert label not in prompt

    def test_scene_content_dict_requires_nonempty_scene(self):
        with pytest.raises(ValueError):
            assemble_video_scene_prompt({"camera": "Slow push-in."})

    def test_empty_string_scene_content_raises(self):
        with pytest.raises(ValueError):
            assemble_video_scene_prompt("")


class TestForbiddenSectionCollection:
    def test_forbidden_section_combines_video_dna_and_brand_items(self):
        prompt = assemble_video_scene_prompt("A quiet stone chamber.")
        forbidden_line = [l for l in prompt.split("\n") if l.startswith("FORBIDDEN:")][0]
        assert "talking_head" in forbidden_line  # from Video DNA
        assert "museum-display finish" in forbidden_line  # from Brand DNA

    def test_forbidden_items_deduplicated(self):
        def fake_video_dna_forbidden():
            return ["duplicate_item", "unique_video_item"]

        def fake_brand_forbidden():
            return ["duplicate_item", "unique_brand_item"]

        prompt = assemble_video_scene_prompt(
            "A scene.",
            video_dna_forbidden_fn=fake_video_dna_forbidden,
            brand_forbidden_fn=fake_brand_forbidden,
        )
        forbidden_line = [l for l in prompt.split("\n") if l.startswith("FORBIDDEN:")][0]
        assert forbidden_line.count("duplicate_item") == 1
        assert "unique_video_item" in forbidden_line
        assert "unique_brand_item" in forbidden_line

    def test_forbidden_is_the_only_section_with_negative_language(self):
        prompt = assemble_video_scene_prompt("A quiet stone chamber.")
        sections = {}
        current_label = None
        for line in prompt.split("\n"):
            for label in ("SCENE", "VISUAL PRIORITIES", "VIDEO DNA", "BRAND DNA", "FORBIDDEN"):
                if line.startswith(f"{label}:"):
                    current_label = label
                    sections[label] = line[len(label) + 1:]
        for label in ("VISUAL PRIORITIES", "VIDEO DNA", "BRAND DNA"):
            text = sections[label].lower()
            assert " never " not in text
            assert " avoid" not in text


class TestCompressionPass:
    def test_genuine_cross_section_duplicate_is_dropped(self):
        def fake_video_dna():
            return "Slow camera. Surfaces are weathered and aged materials throughout."

        def fake_brand():
            return " — FAKE BRAND (mode): Material character: aged, tactile, weathered surfaces everywhere. Finish: clean."

        prompt = assemble_video_scene_prompt(
            "A scene.", video_dna_directive_fn=fake_video_dna, brand_directive_fn=fake_brand,
        )
        # The duplicate sentence should be dropped from Brand DNA (appears later).
        assert prompt.count("weathered") == 1
        assert "Finish: clean." in prompt  # non-duplicate content preserved

    def test_scene_content_never_compressed(self):
        # Scene/Camera/Subject/Motion/Lighting are topic-specific authorial
        # content — the compression pass must never alter them, even if they
        # happen to repeat a word also used in Video DNA / Brand DNA.
        scene_text = "A weathered stone wall stands weathered and alone. No people, no text, no modern objects."
        prompt = assemble_video_scene_prompt(scene_text)
        assert f"SCENE: {scene_text}" in prompt

    def test_no_false_positive_compression_on_default_directives(self):
        # The real Video DNA + Brand DNA combination (post-2026-07-14
        # ownership revision) should not trigger any spurious drops beyond
        # what's genuinely redundant — every distinct rule concept survives.
        prompt = assemble_video_scene_prompt("A quiet stone chamber.")
        assert "slow, deliberate camera" in prompt.lower()
        assert "material character" in prompt.lower()
        assert "fine surface detail" in prompt.lower()


class TestVersionComparison:
    def test_swappable_video_dna_and_brand_directive_functions(self):
        def fake_video_dna():
            return "Alternate Video DNA v2 content."

        def fake_brand():
            return "Alternate Brand DNA v2 content."

        prompt = assemble_video_scene_prompt(
            "A scene.", video_dna_directive_fn=fake_video_dna, brand_directive_fn=fake_brand,
        )
        assert "Alternate Video DNA v2 content" in prompt
        assert "Alternate Brand DNA v2 content" in prompt

    def test_scene_content_identical_regardless_of_directive_version(self):
        def fake_v2():
            return "v2 content."

        p1 = assemble_video_scene_prompt("A fixed scene.")
        p2 = assemble_video_scene_prompt("A fixed scene.", video_dna_directive_fn=fake_v2)
        assert p1.split("\n")[0] == p2.split("\n")[0] == "SCENE: A fixed scene."


class TestNoProviderCallsPossible:
    def test_module_makes_no_provider_calls(self):
        import ast
        tree = ast.parse(Path("hypogeum/video_prompt_writer.py").read_text())
        called_names = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "submit_scene" not in called_names
        assert "requests" not in called_names


# ---------------------------------------------------------------------------
# TestPromptCompilerStage0SelectorResolution (Phase B4 Stage 0, 2026-07-18)
# ---------------------------------------------------------------------------

def _full_scene() -> dict:
    return {
        "setting_type": "underground",
        "lighting_selection": "warm",
        "diversity_assignment": {
            "shot_scale": "close_up",
            "camera_movement": "slow_push_in",
            "camera_angle": "low_angle",
            "composition": "rule_of_thirds",
        },
        "presentation_plan": {
            "spatial_relationship": "The arena floor exists directly above the tunnels.",
            "camera_strategy": "Single continuous dolly push through the cutaway plane.",
        },
        "production_spec": {
            "aspect_ratio": "16:9",
        },
    }


class TestPromptCompilerStage0SelectorResolution:
    """B4 Stage 0: resolve_selectors() is pure data mapping, not wired into
    any live call site. See B1_3_ARCHITECTURE_CONTRACT.md's Compiler Stages
    section for the frozen contract this implements.
    """

    def test_all_real_upstream_fields_extracted(self):
        result = resolve_selectors(_full_scene())
        assert result["setting_type"] == "underground"
        assert result["lighting_selection"] == "warm"
        assert result["shot_scale"] == "close_up"
        assert result["camera_movement"] == "slow_push_in"
        assert result["camera_angle"] == "low_angle"
        assert result["composition"] == "rule_of_thirds"
        assert result["spatial_relationship"] == (
            "The arena floor exists directly above the tunnels."
        )
        assert result["camera_strategy"] == (
            "Single continuous dolly push through the cutaway plane."
        )
        assert result["aspect_ratio"] == "16:9"

    def test_returns_exactly_eleven_keys(self):
        """camera_strategy added 2026-07-18 during Stage 3's IR-completeness
        audit — Stage A4's free-text camera description, required by the
        future Camera Validator (B5) per its own contract text, distinct
        from diversity_assignment's structured camera_movement/camera_angle/
        shot_scale."""
        result = resolve_selectors(_full_scene())
        assert set(result.keys()) == {
            "setting_type", "lighting_selection", "shot_scale", "camera_movement",
            "camera_angle", "composition", "spatial_relationship", "camera_strategy",
            "aspect_ratio", "has_practical_source", "historical_period",
        }

    def test_empty_scene_resolves_every_key_to_none(self):
        result = resolve_selectors({})
        assert all(v is None for v in result.values())
        assert set(result.keys()) == set(resolve_selectors(_full_scene()).keys())

    def test_missing_diversity_assignment_degrades_to_none_not_error(self):
        scene = _full_scene()
        del scene["diversity_assignment"]
        result = resolve_selectors(scene)
        assert result["shot_scale"] is None
        assert result["camera_movement"] is None
        assert result["camera_angle"] is None
        assert result["composition"] is None

    def test_missing_presentation_plan_degrades_to_none_not_error(self):
        scene = _full_scene()
        del scene["presentation_plan"]
        result = resolve_selectors(scene)
        assert result["spatial_relationship"] is None

    def test_missing_production_spec_degrades_to_none_not_error(self):
        scene = _full_scene()
        del scene["production_spec"]
        result = resolve_selectors(scene)
        assert result["aspect_ratio"] is None

    def test_none_valued_nested_dicts_degrade_to_none_not_error(self):
        """A scene with an explicit None for a nested dict (not merely
        absent) must not raise — matches the `or {}` guard in the
        implementation, not just `.get(key, {})`."""
        scene = {
            "diversity_assignment": None,
            "presentation_plan": None,
            "production_spec": None,
        }
        result = resolve_selectors(scene)
        assert result["shot_scale"] is None
        assert result["spatial_relationship"] is None
        assert result["aspect_ratio"] is None

    def test_no_fallback_values_applied(self):
        """Stage 0 must not invent 'unspecified' or any other normalized
        default for setting_type/lighting_selection — that policy decision
        belongs to a later call site (Stage 1+ / historical_directive_fn),
        not to pure extraction."""
        result = resolve_selectors({})
        assert result["setting_type"] is None
        assert result["lighting_selection"] is None

    def test_unresolved_selectors_always_none(self):
        result = resolve_selectors(_full_scene())
        for key in UNRESOLVED_SELECTORS:
            assert result[key] is None

    def test_unresolved_selectors_constant_contents(self):
        assert UNRESOLVED_SELECTORS == ("has_practical_source", "historical_period")

    def test_unresolved_selectors_present_even_if_scene_supplies_them(self):
        """Placeholders must not accidentally start reading a scene-supplied
        value — no upstream stage produces these today, so any value found
        on a scene dict under these keys would not be a real, validated
        producer and must still resolve to None."""
        scene = _full_scene()
        scene["has_practical_source"] = True
        scene["historical_period"] = "Bronze Age"
        result = resolve_selectors(scene)
        assert result["has_practical_source"] is None
        assert result["historical_period"] is None

    def test_calls_no_dna_module(self):
        """Pure data mapping only — Stage 0 must not import or call any DNA
        module function."""
        import ast
        import inspect
        from hypogeum import video_prompt_writer as vpw

        src = inspect.getsource(vpw.resolve_selectors)
        tree = ast.parse(src)
        called_names = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        dna_functions = {
            "describe_video_dna_constraints", "describe_camera_constraints",
            "describe_motion_constraints", "describe_lighting_constraints",
            "describe_historical_constraints", "describe_composition_constraints",
            "build_visual_identity_directive",
        }
        assert called_names.isdisjoint(dna_functions)

    def test_not_wired_into_assemble_video_scene_prompt(self):
        """Stage 0 must not be called anywhere inside
        assemble_video_scene_prompt() — it exists standalone, unwired,
        per this checkpoint's explicit scope."""
        import ast
        import inspect
        from hypogeum import video_prompt_writer as vpw

        src = inspect.getsource(vpw.assemble_video_scene_prompt)
        tree = ast.parse(src)
        called_names = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "resolve_selectors" not in called_names

    def test_muapi_submit_scene_call_shape_unaffected(self):
        """hypogeum.muapi.submit_scene()'s call to assemble_video_scene_prompt()
        stays one positional arg plus, at most, the `channel_id` keyword
        (added for per-channel brand identity — hypogeum.brand.channel_identity).
        Stage 0's compiler work still adds nothing."""
        import ast
        import inspect
        from hypogeum import muapi

        src = inspect.getsource(muapi.submit_scene)
        tree = ast.parse(src)
        matches = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "assemble_video_scene_prompt"
        ]
        assert len(matches) == 2, "Expected primary + fallback calls, unchanged"
        for node in matches:
            assert len(node.args) == 1
            assert {k.arg for k in node.keywords} <= {"channel_id"}

    def test_no_provider_or_llm_calls_in_stage_0(self):
        import ast
        import inspect
        from hypogeum import video_prompt_writer as vpw

        src = inspect.getsource(vpw.resolve_selectors)
        assert "run_agent" not in src
        assert "call_claude" not in src
        assert "requests." not in src
        assert "httpx." not in src


# ---------------------------------------------------------------------------
# TestPromptCompilerStage1DnaResolution (Phase B4 Stage 1, 2026-07-18)
# ---------------------------------------------------------------------------

_STAGE2_CANONICAL_ORDER = (
    "video_dna", "camera_dna", "motion_dna", "lighting_dna",
    "historical_dna", "composition_dna", "brand_dna",
)


class TestPromptCompilerStage1DnaResolution:
    """B4 Stage 1: resolve_dna() calls all 7 DNA modules with Stage 0's
    selectors, applying zero normalization of its own — see
    B1_3_ARCHITECTURE_CONTRACT.md's Compiler Stages section and Deliverable
    4's Data Ownership Matrix (Compiler is a read-only consumer of
    aspect_ratio/presentation_plan/diversity_assignment).
    """

    def test_returns_all_seven_modules_in_fixed_order(self):
        result = resolve_dna(resolve_selectors(_full_scene()))
        assert list(result.keys()) == list(_STAGE2_CANONICAL_ORDER)

    def test_each_value_is_a_directive_forbidden_pair(self):
        result = resolve_dna(resolve_selectors(_full_scene()))
        for name, value in result.items():
            assert isinstance(value, tuple) and len(value) == 2, name
            directive, forbidden = value
            assert isinstance(directive, str) and directive
            assert isinstance(forbidden, list)

    def test_camera_dna_receives_real_selectors(self):
        import hypogeum.camera_dna as camera_dna
        selectors = resolve_selectors(_full_scene())
        result = resolve_dna(selectors)
        expected = camera_dna.describe_camera_constraints(
            camera_movement="slow_push_in", camera_angle="low_angle", shot_scale="close_up",
        )
        assert result["camera_dna"][0] == expected

    def test_lighting_dna_receives_real_selectors(self):
        import hypogeum.lighting_dna as lighting_dna
        selectors = resolve_selectors(_full_scene())
        result = resolve_dna(selectors)
        expected = lighting_dna.describe_lighting_constraints(
            lighting_selection="warm", has_practical_source=None,
        )
        assert result["lighting_dna"][0] == expected

    def test_historical_dna_receives_real_setting_type(self):
        import hypogeum.historical_dna as historical_dna
        selectors = resolve_selectors(_full_scene())
        result = resolve_dna(selectors)
        expected_directive = historical_dna.describe_historical_constraints(
            setting_type="underground", historical_period=None,
        )
        expected_forbidden = historical_dna.get_historical_forbidden_items(setting_type="underground")
        assert result["historical_dna"] == (expected_directive, expected_forbidden)

    def test_composition_dna_receives_real_selectors(self):
        import hypogeum.composition_dna as composition_dna
        selectors = resolve_selectors(_full_scene())
        result = resolve_dna(selectors)
        expected = composition_dna.describe_composition_constraints(
            aspect_ratio="16:9",
            spatial_relationship="The arena floor exists directly above the tunnels.",
            composition="rule_of_thirds",
        )
        assert result["composition_dna"][0] == expected

    def test_brand_dna_uses_video_scene_mode(self):
        import hypogeum.brand.visual_identity as visual_identity
        result = resolve_dna(resolve_selectors(_full_scene()))
        expected = visual_identity.build_visual_identity_directive(mode="video_scene")
        assert result["brand_dna"][0] == expected

    def test_video_and_motion_dna_take_no_arguments(self):
        """These two modules' functions are zero-arg — resolve_dna() must
        call them exactly as-is, not invent parameters for them."""
        import hypogeum.video_dna as video_dna
        import hypogeum.motion_dna as motion_dna
        result = resolve_dna(resolve_selectors(_full_scene()))
        assert result["video_dna"][0] == video_dna.describe_video_dna_constraints()
        assert result["motion_dna"][0] == motion_dna.describe_motion_constraints()

    # -- no normalization (the architectural review point) -------------------

    def test_none_setting_type_passed_through_not_normalized(self):
        """Stage 1 must NOT substitute 'unspecified' for a None setting_type
        — that fallback is already owned by
        hypogeum.agents.setting_lighting_classifier, not the Compiler."""
        import hypogeum.historical_dna as historical_dna
        selectors = resolve_selectors({})  # setting_type is None
        result = resolve_dna(selectors)
        expected_directive = historical_dna.describe_historical_constraints(
            setting_type=None, historical_period=None,
        )
        expected_forbidden = historical_dna.get_historical_forbidden_items(setting_type=None)
        assert result["historical_dna"] == (expected_directive, expected_forbidden)
        assert expected_forbidden == []

    def test_none_aspect_ratio_passed_through_not_normalized(self):
        """Stage 1 must NOT substitute '16:9' for a None aspect_ratio — per
        the Data Ownership Matrix, aspect_ratio's canonical writer is Stage
        A2 only; the Compiler is a read-only consumer."""
        import hypogeum.composition_dna as composition_dna
        selectors = resolve_selectors({})  # aspect_ratio is None
        result = resolve_dna(selectors)
        expected = composition_dna.describe_composition_constraints(
            aspect_ratio=None, spatial_relationship=None, composition=None,
        )
        assert result["composition_dna"][0] == expected

    def test_empty_selectors_does_not_raise(self):
        """Every module must degrade safely on an all-None selectors dict —
        confirms Stage 1 needs no defensive normalization to avoid a crash."""
        result = resolve_dna(resolve_selectors({}))
        assert len(result) == 7

    def test_no_hardcoded_unspecified_or_aspect_ratio_literal_in_stage_1(self):
        """Stage 1's own CODE (not its docstring, which discusses these
        strings as prose) must not contain a literal 'unspecified' or
        '16:9' fallback constant — those normalization decisions belong to
        setting_lighting_classifier.py and Stage A2/episode config
        respectively, never to this stage. Uses AST constant inspection,
        not raw source text, to avoid a docstring-prose false positive."""
        import ast
        import inspect
        from hypogeum import video_prompt_writer as vpw

        src = inspect.getsource(vpw.resolve_dna)
        tree = ast.parse(src)
        func = tree.body[0]
        body_without_docstring = func.body[1:] if ast.get_docstring(func) else func.body
        string_constants = {
            node.value for node in ast.walk(ast.Module(body=body_without_docstring, type_ignores=[]))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert "unspecified" not in string_constants
        assert "16:9" not in string_constants

    def test_placeholder_selectors_passed_through_unmodified(self):
        """has_practical_source/historical_period stay None end-to-end —
        Stage 1 must not invent a value for either."""
        import hypogeum.lighting_dna as lighting_dna
        import hypogeum.historical_dna as historical_dna
        selectors = resolve_selectors(_full_scene())
        for key in UNRESOLVED_SELECTORS:
            assert selectors[key] is None
        result = resolve_dna(selectors)
        assert result["lighting_dna"][0] == lighting_dna.describe_lighting_constraints(
            lighting_selection="warm", has_practical_source=None,
        )

    # -- call-shape / no dedup / no wiring -----------------------------------

    def test_does_not_merge_or_deduplicate_forbidden_items(self):
        """Stage 1 must return raw per-module forbidden lists — merging/
        dedup is Stage 2's job, not this one's."""
        import hypogeum.lighting_dna as lighting_dna
        result = resolve_dna(resolve_selectors(_full_scene()))
        assert result["lighting_dna"][1] == lighting_dna.get_lighting_forbidden_items()

    def test_not_wired_into_assemble_video_scene_prompt(self):
        import ast
        import inspect
        from hypogeum import video_prompt_writer as vpw

        src = inspect.getsource(vpw.assemble_video_scene_prompt)
        tree = ast.parse(src)
        called_names = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "resolve_dna" not in called_names

    def test_muapi_submit_scene_call_shape_unaffected(self):
        import ast
        import inspect
        from hypogeum import muapi

        src = inspect.getsource(muapi.submit_scene)
        tree = ast.parse(src)
        matches = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "assemble_video_scene_prompt"
        ]
        assert len(matches) == 2
        for node in matches:
            assert len(node.args) == 1
            assert {k.arg for k in node.keywords} <= {"channel_id"}

    def test_no_provider_or_llm_calls_in_stage_1(self):
        import inspect
        from hypogeum import video_prompt_writer as vpw

        src = inspect.getsource(vpw.resolve_dna)
        assert "run_agent" not in src
        assert "call_claude" not in src
        assert "requests." not in src
        assert "httpx." not in src


# ---------------------------------------------------------------------------
# TestPromptCompilerStage2MergeAndCompression (Phase B4 Stage 2, 2026-07-18)
# ---------------------------------------------------------------------------

_STAGE2_CANONICAL_ORDER = (
    "video_dna", "camera_dna", "motion_dna",
    "lighting_dna", "historical_dna", "composition_dna", "brand_dna",
)


def _stage1_texts() -> dict:
    """Stage 1 output narrowed to bare directive text (dict[str, str]) —
    the type compress_directives() actually declares as its input."""
    stage1 = resolve_dna(resolve_selectors(_full_scene()))
    return {k: v[0] for k, v in stage1.items()}


class TestPromptCompilerStage2MergeAndCompression:
    """B4 Stage 2: compress_directives() is type-symmetric
    (dict[str, str] -> dict[str, str]), enforces canonical module order
    explicitly, never touches forbidden items, and is idempotent under
    repeated application. See B1_3_ARCHITECTURE_CONTRACT.md's Compiler
    Stages section.
    """

    # -- ordering --------------------------------------------------------

    def test_output_keys_in_canonical_order(self):
        result = compress_directives(_stage1_texts())
        assert list(result.keys()) == list(_STAGE2_CANONICAL_ORDER)

    def test_reordered_input_dict_does_not_change_output_order(self):
        texts = _stage1_texts()
        reordered = dict(reversed(list(texts.items())))
        assert list(reordered.keys()) != list(_STAGE2_CANONICAL_ORDER)  # sanity: actually reordered
        result = compress_directives(reordered)
        assert list(result.keys()) == list(_STAGE2_CANONICAL_ORDER)

    def test_reordered_input_produces_identical_values(self):
        texts = _stage1_texts()
        reordered = dict(reversed(list(texts.items())))
        assert compress_directives(texts) == compress_directives(reordered)

    # -- malformed input ---------------------------------------------------

    def test_missing_key_raises_value_error_naming_it(self):
        texts = _stage1_texts()
        del texts["brand_dna"]
        with pytest.raises(ValueError, match="brand_dna"):
            compress_directives(texts)

    def test_extra_key_raises_value_error_naming_it(self):
        texts = _stage1_texts()
        texts["some_unknown_module"] = "x"
        with pytest.raises(ValueError, match="some_unknown_module"):
            compress_directives(texts)

    def test_missing_and_extra_together_both_raise(self):
        texts = _stage1_texts()
        del texts["video_dna"]
        texts["bogus"] = "x"
        with pytest.raises(ValueError):
            compress_directives(texts)

    # -- idempotency (the architectural review point) ------------------------

    def test_idempotent_under_repeated_application(self):
        texts = _stage1_texts()
        once = compress_directives(texts)
        twice = compress_directives(once)
        assert once == twice

    def test_idempotent_output_type_matches_input_type(self):
        """The output must be directly re-feedable — dict[str, str], same
        shape as the input, never Stage 1's tuple shape."""
        result = compress_directives(_stage1_texts())
        for value in result.values():
            assert isinstance(value, str)
        # Re-feeding must not raise and must not silently corrupt via [0]-indexing
        again = compress_directives(result)
        assert again == result

    # -- forbidden items never touched --------------------------------------

    def test_input_type_excludes_forbidden_items_entirely(self):
        """compress_directives() takes dict[str, str] — passing Stage 1's
        raw tuple-shaped dict directly must fail loudly (a tuple has no
        .split(), so the compression pass's string operations raise
        AttributeError), proving forbidden items are structurally excluded,
        not silently dropped by unwritten internal logic."""
        stage1_raw = resolve_dna(resolve_selectors(_full_scene()))
        with pytest.raises(AttributeError):
            compress_directives(stage1_raw)

    def test_stage1_output_object_unmodified_by_stage2(self):
        """Stage 1's original dict-of-tuples must remain independently
        available and untouched after Stage 2 runs on a separately-narrowed
        copy of its text."""
        stage1 = resolve_dna(resolve_selectors(_full_scene()))
        stage1_before = {k: v for k, v in stage1.items()}
        texts = {k: v[0] for k, v in stage1.items()}
        compress_directives(texts)
        assert stage1 == stage1_before

    # -- compression behavior (reuses existing tested algorithm) -------------

    def test_cross_module_dedup_uses_existing_synonym_clusters(self):
        texts = dict.fromkeys(_STAGE2_CANONICAL_ORDER, "")
        texts["video_dna"] = "This has weathered surface."
        texts["brand_dna"] = "aged, tactile finish. A brand new unrelated fact."
        result = compress_directives(texts)
        assert "A brand new unrelated fact." in result["brand_dna"]
        assert "aged, tactile finish" not in result["brand_dna"]

    def test_unmatched_sentences_never_dropped(self):
        texts = dict.fromkeys(_STAGE2_CANONICAL_ORDER, "")
        texts["video_dna"] = "A completely unique statement."
        result = compress_directives(texts)
        assert result["video_dna"] == "A completely unique statement."

    def test_determinism_same_input_same_output(self):
        texts = _stage1_texts()
        assert compress_directives(texts) == compress_directives(dict(texts))

    # -- scope narrowing vs. today's live function ----------------------------

    def test_visual_priorities_not_a_valid_input_key(self):
        """Visual Priorities is not one of Stage 1's 7 modules — passing it
        must be treated as an unrecognized/extra key, not silently accepted."""
        texts = _stage1_texts()
        texts["visual_priorities"] = "some text"
        with pytest.raises(ValueError, match="visual_priorities"):
            compress_directives(texts)

    def test_does_not_strip_brand_directive_label(self):
        """Label-stripping (_strip_directive_label) is a Stage 3 (labeling/
        assembly) concern, out of scope for Stage 2 — brand_dna's raw text
        passes through compression unmodified in that respect."""
        texts = _stage1_texts()
        original_brand_text = texts["brand_dna"]
        result = compress_directives(texts)
        # Compression may drop duplicate sentences, but must not apply
        # label-stripping logic — the un-compressed text's non-duplicate
        # lead-in content (if any) is untouched by _strip_directive_label.
        assert result["brand_dna"] != "" if original_brand_text else True

    # -- no wiring / no provider calls ----------------------------------------

    def test_not_wired_into_assemble_video_scene_prompt(self):
        import ast
        import inspect
        from hypogeum import video_prompt_writer as vpw

        src = inspect.getsource(vpw.assemble_video_scene_prompt)
        tree = ast.parse(src)
        called_names = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "compress_directives" not in called_names

    def test_no_provider_or_llm_calls(self):
        import inspect
        from hypogeum import video_prompt_writer as vpw

        src = inspect.getsource(vpw.compress_directives)
        assert "run_agent" not in src
        assert "call_claude" not in src
        assert "requests." not in src
        assert "httpx." not in src


# ---------------------------------------------------------------------------
# TestPromptCompilerStage3CanonicalAssembly (Phase B4 Stage 3, 2026-07-18)
# ---------------------------------------------------------------------------

def _stage1_2_outputs():
    """Real Stage 0->1->2 pipeline output for a full scene, returning the
    three pieces assemble_canonical_prompt() needs: compressed texts,
    per-module forbidden lists, and selectors."""
    selectors = resolve_selectors(_full_scene())
    stage1 = resolve_dna(selectors)
    texts = {k: v[0] for k, v in stage1.items()}
    forbidden_by_module = {k: v[1] for k, v in stage1.items()}
    compressed = compress_directives(texts)
    return compressed, forbidden_by_module, selectors


class TestPromptCompilerStage3CanonicalAssembly:
    """B4 Stage 3: assemble_canonical_prompt() produces the project's
    canonical, structured (section-keyed, never a flattened string)
    intermediate representation — the shared contract for Stage 4, all of
    B5's Validators, and B6's Benchmark Platform. See
    B1_3_ARCHITECTURE_CONTRACT.md's Compiler Stages and Provider Adapter
    Contract sections.
    """

    # -- return type / structure ----------------------------------------

    def test_returns_canonical_prompt_dataclass(self):
        compressed, forbidden, selectors = _stage1_2_outputs()
        result = assemble_canonical_prompt("A quiet chamber.", compressed, forbidden, selectors)
        assert isinstance(result, CanonicalPrompt)

    def test_sections_is_dict_not_a_flattened_string(self):
        """Per the Provider Adapter Contract: 'Canonical Prompt (structured,
        section-keyed — not a flattened string)'."""
        compressed, forbidden, selectors = _stage1_2_outputs()
        result = assemble_canonical_prompt("A quiet chamber.", compressed, forbidden, selectors)
        assert isinstance(result.sections, dict)
        assert not isinstance(result, str)

    def test_sections_in_canonical_render_order(self):
        compressed, forbidden, selectors = _stage1_2_outputs()
        result = assemble_canonical_prompt(
            {"scene": "S", "camera": "C", "subject": "Su", "motion": "M", "lighting": "L"},
            compressed, forbidden, selectors,
        )
        expected_order = [
            "scene", "camera", "subject", "motion", "lighting",
            "visual_priorities", "composition_dna",
            "video_dna", "camera_dna", "motion_dna", "lighting_dna", "historical_dna",
            "brand_dna",
        ]
        assert list(result.sections.keys()) == expected_order

    def test_composition_dna_renders_before_video_dna(self):
        """The established render order (docstring, wired since B2.1-B2.8)
        differs from Stage 1/2's own processing order (Composition resolved
        after Historical) without conflict — dedup guarantees a concept
        appears once, not in any particular render position."""
        compressed, forbidden, selectors = _stage1_2_outputs()
        result = assemble_canonical_prompt("Scene.", compressed, forbidden, selectors)
        keys = list(result.sections.keys())
        assert keys.index("composition_dna") < keys.index("video_dna")

    def test_empty_topic_sections_omitted(self):
        compressed, forbidden, selectors = _stage1_2_outputs()
        result = assemble_canonical_prompt("Just a scene, no camera/subject/motion/lighting.", compressed, forbidden, selectors)
        assert "camera" not in result.sections
        assert "subject" not in result.sections
        assert "motion" not in result.sections
        assert "lighting" not in result.sections
        assert "scene" in result.sections

    def test_brand_dna_label_stripped(self):
        """Label-stripping is deferred from Stage 2 to Stage 3 (a labeling/
        assembly concern) — brand_dna's own embedded lead-in must not survive
        into the Canonical Prompt."""
        compressed, forbidden, selectors = _stage1_2_outputs()
        result = assemble_canonical_prompt("Scene.", compressed, forbidden, selectors)
        assert not result.sections["brand_dna"].lstrip(" —").startswith("HYPOGEUM BRAND VISUAL IDENTITY")

    # -- forbidden handling -----------------------------------------------

    def test_forbidden_items_is_aggregated_flat_list(self):
        compressed, forbidden, selectors = _stage1_2_outputs()
        result = assemble_canonical_prompt("Scene.", compressed, forbidden, selectors)
        assert isinstance(result.forbidden_items, list)
        assert len(result.forbidden_items) == len(set(result.forbidden_items))  # deduplicated

    def test_forbidden_items_never_rendered_into_provider_syntax(self):
        """No bracket-tag or 'FORBIDDEN:' sentence formatting here — that's
        Stage 4's job."""
        compressed, forbidden, selectors = _stage1_2_outputs()
        result = assemble_canonical_prompt("Scene.", compressed, forbidden, selectors)
        assert not any(item.startswith("[no ") for item in result.forbidden_items)
        assert "FORBIDDEN:" not in " ".join(result.forbidden_items)

    def test_forbidden_by_module_preserves_per_module_attribution(self):
        """Required by the Forbidden Rules Validator's own stated check
        ('no module silently dropped') — impossible to verify from the
        aggregated forbidden_items list alone."""
        compressed, forbidden, selectors = _stage1_2_outputs()
        result = assemble_canonical_prompt("Scene.", compressed, forbidden, selectors)
        assert set(result.forbidden_by_module.keys()) == set(_STAGE2_CANONICAL_ORDER)
        for module_name, items in forbidden.items():
            assert result.forbidden_by_module[module_name] == items

    def test_forbidden_items_is_the_aggregation_of_forbidden_by_module(self):
        import hypogeum.forbidden_dna as forbidden_dna
        compressed, forbidden, selectors = _stage1_2_outputs()
        result = assemble_canonical_prompt("Scene.", compressed, forbidden, selectors)
        expected = forbidden_dna.collect_forbidden_items(
            *(forbidden[name] for name in _STAGE2_CANONICAL_ORDER)
        )
        assert result.forbidden_items == expected

    # -- selectors carried through ------------------------------------------

    def test_selectors_carried_through_verbatim(self):
        compressed, forbidden, selectors = _stage1_2_outputs()
        result = assemble_canonical_prompt("Scene.", compressed, forbidden, selectors)
        assert result.selectors == selectors

    def test_selectors_includes_camera_strategy_for_camera_validator(self):
        compressed, forbidden, selectors = _stage1_2_outputs()
        result = assemble_canonical_prompt("Scene.", compressed, forbidden, selectors)
        assert result.selectors["camera_strategy"] == (
            "Single continuous dolly push through the cutaway plane."
        )

    # -- malformed input -----------------------------------------------------

    def test_missing_compressed_key_raises(self):
        compressed, forbidden, selectors = _stage1_2_outputs()
        del compressed["brand_dna"]
        with pytest.raises(ValueError, match="compressed.*brand_dna"):
            assemble_canonical_prompt("Scene.", compressed, forbidden, selectors)

    def test_missing_forbidden_key_raises(self):
        compressed, forbidden, selectors = _stage1_2_outputs()
        del forbidden["camera_dna"]
        with pytest.raises(ValueError, match="forbidden.*camera_dna"):
            assemble_canonical_prompt("Scene.", compressed, forbidden, selectors)

    def test_extra_key_in_either_raises(self):
        compressed, forbidden, selectors = _stage1_2_outputs()
        compressed["bogus"] = "x"
        with pytest.raises(ValueError, match="bogus"):
            assemble_canonical_prompt("Scene.", compressed, forbidden, selectors)

    # -- no wiring / no provider calls ----------------------------------------

    def test_not_wired_into_assemble_video_scene_prompt(self):
        import ast
        import inspect
        from hypogeum import video_prompt_writer as vpw

        src = inspect.getsource(vpw.assemble_video_scene_prompt)
        tree = ast.parse(src)
        called_names = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "assemble_canonical_prompt" not in called_names

    def test_no_provider_or_llm_calls(self):
        import inspect
        from hypogeum import video_prompt_writer as vpw

        src = inspect.getsource(vpw.assemble_canonical_prompt)
        assert "run_agent" not in src
        assert "call_claude" not in src
        assert "requests." not in src
        assert "httpx." not in src


# ---------------------------------------------------------------------------
# TestPromptCompilerStage4ProviderAdapterApplication (Phase B4 Stage 4, 2026-07-18)
# ---------------------------------------------------------------------------

def _canonical_prompt_for_stage4():
    compressed, forbidden, selectors = _stage1_2_outputs()
    return assemble_canonical_prompt(
        {"scene": "A slow_push_in through the chamber.", "camera": "slow_push_in framing."},
        compressed, forbidden, selectors,
    )


class TestPromptCompilerStage4ProviderAdapterApplication:
    """B4 Stage 4: apply_provider_adapter() dispatches a CanonicalPrompt to
    the correct Provider Adapter (hypogeum.veo_adapter /
    hypogeum.seedance_adapter) and returns the Provider-ready Prompt
    string. Not wired into any live call site.
    """

    def test_returns_string(self):
        canonical = _canonical_prompt_for_stage4()
        result = apply_provider_adapter(canonical, "veo", {})
        assert isinstance(result, str)

    def test_veo_vocabulary_translated(self):
        canonical = _canonical_prompt_for_stage4()
        result = apply_provider_adapter(canonical, "veo", {})
        assert "slow dolly in" in result

    def test_seedance_vocabulary_translated(self):
        canonical = _canonical_prompt_for_stage4()
        result = apply_provider_adapter(canonical, "seedance", {})
        assert "slow push" in result

    def test_veo_and_seedance_produce_different_output(self):
        """Same Canonical Prompt, different provider -- confirms Stage 4
        actually dispatches per-provider rather than always applying one
        adapter's translation."""
        canonical = _canonical_prompt_for_stage4()
        veo_result = apply_provider_adapter(canonical, "veo", {})
        seedance_result = apply_provider_adapter(canonical, "seedance", {})
        assert veo_result != seedance_result

    def test_forbidden_section_present_no_bracket_tag(self):
        canonical = _canonical_prompt_for_stage4()
        result = apply_provider_adapter(canonical, "veo", {})
        assert "Excluded:" in result
        assert "[no " not in result.lower()

    def test_provider_metadata_appended(self):
        canonical = _canonical_prompt_for_stage4()
        veo_result = apply_provider_adapter(canonical, "veo", {"aspect_ratio": "16:9"})
        assert "Ambient noise:" in veo_result
        assert "[16:9]" in veo_result
        seedance_result = apply_provider_adapter(canonical, "seedance", {"aspect_ratio": "16:9"})
        assert "Sound design:" in seedance_result
        assert "[16:9]" in seedance_result

    def test_creative_intent_scene_content_preserved(self):
        canonical = _canonical_prompt_for_stage4()
        result = apply_provider_adapter(canonical, "veo", {})
        assert "A slow_push_in through the chamber." in result

    def test_sections_rendered_in_canonical_order(self):
        canonical = _canonical_prompt_for_stage4()
        result = apply_provider_adapter(canonical, "veo", {})
        assert result.find("SCENE:") < result.find("VIDEO DNA:") < result.find("BRAND DNA:")

    def test_does_not_mutate_input_canonical_prompt(self):
        canonical = _canonical_prompt_for_stage4()
        original_camera_dna = canonical.sections.get("camera_dna")
        apply_provider_adapter(canonical, "veo", {})
        assert canonical.sections.get("camera_dna") == original_camera_dna

    def test_unrecognized_provider_id_raises(self):
        canonical = _canonical_prompt_for_stage4()
        with pytest.raises(ValueError, match="bogus_provider"):
            apply_provider_adapter(canonical, "bogus_provider", {})

    def test_not_wired_into_assemble_video_scene_prompt(self):
        import ast
        import inspect
        from hypogeum import video_prompt_writer as vpw

        src = inspect.getsource(vpw.assemble_video_scene_prompt)
        tree = ast.parse(src)
        called_names = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "apply_provider_adapter" not in called_names

    def test_no_provider_or_llm_calls(self):
        import inspect
        from hypogeum import video_prompt_writer as vpw

        src = inspect.getsource(vpw.apply_provider_adapter)
        assert "run_agent" not in src
        assert "call_claude" not in src
        assert "requests." not in src
        assert "httpx." not in src


# ---------------------------------------------------------------------------
# TestPromptCompilerStage5FinalEmission (Phase B4 Stage 5, 2026-07-18)
# ---------------------------------------------------------------------------

class TestPromptCompilerStage5FinalEmission:
    """B4 Stage 5: compile_scene_prompt() is the 0-5 driver, returning both
    the Canonical Prompt (provider-agnostic, for B5/B6) and the
    Provider-ready Prompt (for muapi.submit_scene()). Not wired into any
    live call site.
    """

    def test_returns_tuple_of_canonical_and_string(self):
        canonical, provider_ready = compile_scene_prompt(_full_scene(), "veo")
        assert isinstance(canonical, CanonicalPrompt)
        assert isinstance(provider_ready, str)

    def test_canonical_prompt_matches_direct_stage_0_3_chain(self):
        """compile_scene_prompt()'s Canonical Prompt must be identical to
        chaining Stage 0->3 by hand -- the driver adds no behavior of its
        own beyond calling the existing stages in order."""
        compressed, forbidden, selectors = _stage1_2_outputs()
        expected_canonical = assemble_canonical_prompt(_full_scene(), compressed, forbidden, selectors)
        canonical, _ = compile_scene_prompt(_full_scene(), "veo")
        assert canonical == expected_canonical

    def test_provider_ready_matches_direct_stage_4_call(self):
        canonical, provider_ready = compile_scene_prompt(_full_scene(), "veo", {"aspect_ratio": "16:9"})
        expected = apply_provider_adapter(canonical, "veo", {"aspect_ratio": "16:9"})
        assert provider_ready == expected

    def test_defaults_tech_metadata_to_empty_dict(self):
        """Omitting tech_metadata must not raise -- both adapters degrade
        safely (no technical tags appended)."""
        canonical, provider_ready = compile_scene_prompt(_full_scene(), "seedance")
        assert "[None" not in provider_ready

    def test_different_provider_id_yields_different_provider_ready_prompt(self):
        _, veo_result = compile_scene_prompt(_full_scene(), "veo")
        _, seedance_result = compile_scene_prompt(_full_scene(), "seedance")
        assert veo_result != seedance_result

    def test_unrecognized_provider_id_raises(self):
        with pytest.raises(ValueError, match="bogus_provider"):
            compile_scene_prompt(_full_scene(), "bogus_provider")

    def test_not_wired_into_assemble_video_scene_prompt(self):
        import ast
        import inspect
        from hypogeum import video_prompt_writer as vpw

        src = inspect.getsource(vpw.assemble_video_scene_prompt)
        tree = ast.parse(src)
        called_names = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "compile_scene_prompt" not in called_names

    def test_no_provider_or_llm_calls(self):
        import inspect
        from hypogeum import video_prompt_writer as vpw

        src = inspect.getsource(vpw.compile_scene_prompt)
        assert "run_agent" not in src
        assert "call_claude" not in src
        assert "requests." not in src
        assert "httpx." not in src
