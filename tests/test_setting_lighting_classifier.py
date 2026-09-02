"""
tests/test_setting_lighting_classifier.py — Phase B2.3: upstream groundwork
for setting_type / lighting_selection.

Covers hypogeum.agents.setting_lighting_classifier's deterministic
assignment logic, the visual_package schema's new optional-field
validation, wiring into visual_package_director.py, and Objective 6/8's
ownership-boundary and legacy-compatibility checks. Zero LLM/provider/
network calls anywhere in this file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hypogeum.agents.setting_lighting_classifier import (
    classify_setting_type, classify_lighting_selection,
    assign_setting_and_lighting, SETTING_TYPE_VALUES, LIGHTING_SELECTION_VALUES,
)
from hypogeum.schemas import SchemaError, validate


# ---------------------------------------------------------------------------
# Allowed taxonomy values (Objective 2 / Deliverable 2)
# ---------------------------------------------------------------------------

class TestTaxonomies:
    def test_setting_type_matches_frozen_contract_plus_fallback(self):
        # The 6 values already named in B1_3_ARCHITECTURE_CONTRACT.md's
        # Historical Authenticity DNA section, plus one fallback addition.
        assert set(SETTING_TYPE_VALUES) == {
            "underwater", "interior", "exterior", "artifact_macro",
            "figure", "underground", "unspecified",
        }

    def test_lighting_selection_matches_frozen_contract_plus_fallback(self):
        # The 6 values already named in B1_3_ARCHITECTURE_CONTRACT.md's
        # Lighting DNA section (= vpd_scene_prompts.txt's own existing
        # LIGHTING CONSTRAINT categories), plus one fallback addition.
        assert set(LIGHTING_SELECTION_VALUES) == {
            "warm", "harsh", "atmospheric", "dramatic", "underground",
            "underwater", "unspecified",
        }

    def test_no_duplicated_meanings_within_each_taxonomy(self):
        assert len(SETTING_TYPE_VALUES) == len(set(SETTING_TYPE_VALUES))
        assert len(LIGHTING_SELECTION_VALUES) == len(set(LIGHTING_SELECTION_VALUES))

    def test_no_cinematic_prose_in_enum_values(self):
        # Every value is a single lowercase snake_case token, not a phrase.
        for value in SETTING_TYPE_VALUES + LIGHTING_SELECTION_VALUES:
            assert " " not in value
            assert value == value.lower()


# ---------------------------------------------------------------------------
# Deterministic assignment (Objective 4)
# ---------------------------------------------------------------------------

class TestClassifySettingType:
    def test_section_cut_yields_underground(self):
        scene = {"presentation_plan": {"presentation_method": "section_cut"}}
        assert classify_setting_type(scene) == "underground"

    def test_vertical_reveal_yields_underground(self):
        scene = {"presentation_plan": {"presentation_method": "vertical_reveal"}}
        assert classify_setting_type(scene) == "underground"

    def test_other_presentation_method_falls_back(self):
        scene = {"presentation_plan": {"presentation_method": "slow_reveal"}}
        assert classify_setting_type(scene) == "unspecified"

    def test_architectural_cutaway_falls_back_not_assumed_underground(self):
        # architectural_cutaway is ALSO used for non-underground
        # hidden_structure scenes per the Visual Language Engine's mapping
        # table — not a reliable underground signal on its own, so it must
        # NOT be classified as underground.
        scene = {"presentation_plan": {"presentation_method": "architectural_cutaway"}}
        assert classify_setting_type(scene) == "unspecified"

    def test_missing_presentation_plan_falls_back(self):
        assert classify_setting_type({}) == "unspecified"

    def test_deterministic_same_input_same_output(self):
        scene = {"presentation_plan": {"presentation_method": "section_cut"}}
        assert classify_setting_type(scene) == classify_setting_type(scene)


class TestClassifyLightingSelection:
    def test_underground_setting_yields_underground_lighting(self):
        scene = {"presentation_plan": {"presentation_method": "section_cut"}}
        assert classify_lighting_selection(scene) == "underground"

    def test_non_underground_setting_falls_back(self):
        scene = {"presentation_plan": {"presentation_method": "slow_reveal"}}
        assert classify_lighting_selection(scene) == "unspecified"

    def test_explicit_setting_type_argument_honored(self):
        # Passing setting_type directly skips re-derivation.
        assert classify_lighting_selection({}, setting_type="underground") == "underground"
        assert classify_lighting_selection({}, setting_type="exterior") == "unspecified"

    def test_missing_scene_data_falls_back(self):
        assert classify_lighting_selection({}) == "unspecified"


class TestAssignSettingAndLighting:
    def test_writes_both_fields_on_every_scene(self):
        scenes = [
            {"scene_id": "s01", "presentation_plan": {"presentation_method": "section_cut"}},
            {"scene_id": "s02", "presentation_plan": {"presentation_method": "push_in"}},
        ]
        assign_setting_and_lighting(scenes)
        assert scenes[0]["setting_type"] == "underground"
        assert scenes[0]["lighting_selection"] == "underground"
        assert scenes[1]["setting_type"] == "unspecified"
        assert scenes[1]["lighting_selection"] == "unspecified"

    def test_idempotent_across_repeated_calls(self):
        scenes = [{"scene_id": "s01", "presentation_plan": {"presentation_method": "section_cut"}}]
        assign_setting_and_lighting(scenes)
        first = dict(scenes[0])
        assign_setting_and_lighting(scenes)
        assert scenes[0] == first

    def test_empty_scene_list_no_error(self):
        assign_setting_and_lighting([])  # must not raise

    def test_scene_missing_presentation_plan_key_no_error(self):
        scenes = [{"scene_id": "s01"}]
        assign_setting_and_lighting(scenes)
        assert scenes[0]["setting_type"] == "unspecified"
        assert scenes[0]["lighting_selection"] == "unspecified"


# ---------------------------------------------------------------------------
# Schema shape, valid/invalid values, legacy records (Objectives 3, 6, 7)
# ---------------------------------------------------------------------------

def _make_visual_package(scenes: list) -> dict:
    return {
        "package_metadata": {},
        "episode_metadata": {},
        "visual_continuity_record": {"locked_entities": []},
        "scenes": scenes,
        "asset_manifest": {},
        "provider_routing_manifest": {},
        "visual_integrity_log": {},
        "thumbnail_handoff_brief": {
            "central_curiosity_gap_statement": "x",
            "candidates": [{"candidate_id": "c1"}, {"candidate_id": "c2"}],
            "short_candidates": [],
        },
        "package_integrity": {"production_ready": True},
    }


def _make_scene(**overrides) -> dict:
    scene = {"scene_id": "s01", "source_type": "main_episode", "derivation_source": "x"}
    scene.update(overrides)
    return scene


class TestSchemaValidValues:
    def test_valid_setting_type_and_lighting_selection_pass(self):
        pkg = _make_visual_package([
            _make_scene(setting_type="underground", lighting_selection="underground"),
        ])
        validate("visual_package", pkg)  # must not raise

    def test_unspecified_fallback_value_passes(self):
        pkg = _make_visual_package([
            _make_scene(setting_type="unspecified", lighting_selection="unspecified"),
        ])
        validate("visual_package", pkg)  # must not raise

    def test_every_frozen_setting_type_value_passes(self):
        for value in SETTING_TYPE_VALUES:
            pkg = _make_visual_package([_make_scene(setting_type=value)])
            validate("visual_package", pkg)

    def test_every_frozen_lighting_selection_value_passes(self):
        for value in LIGHTING_SELECTION_VALUES:
            pkg = _make_visual_package([_make_scene(lighting_selection=value)])
            validate("visual_package", pkg)


class TestSchemaInvalidValuesRejected:
    def test_invalid_setting_type_rejected(self):
        pkg = _make_visual_package([_make_scene(setting_type="not_a_real_value")])
        with pytest.raises(SchemaError):
            validate("visual_package", pkg)

    def test_invalid_lighting_selection_rejected(self):
        pkg = _make_visual_package([_make_scene(lighting_selection="fluorescent_studio")])
        with pytest.raises(SchemaError):
            validate("visual_package", pkg)

    def test_provider_specific_value_rejected(self):
        pkg = _make_visual_package([_make_scene(setting_type="veo_scene")])
        with pytest.raises(SchemaError):
            validate("visual_package", pkg)


class TestLegacyRecordsCompatibility:
    def test_scene_without_either_field_still_loads(self):
        pkg = _make_visual_package([_make_scene()])  # no setting_type/lighting_selection at all
        validate("visual_package", pkg)  # must not raise — both fields optional

    def test_scene_with_explicit_null_values_still_loads(self):
        pkg = _make_visual_package([_make_scene(setting_type=None, lighting_selection=None)])
        validate("visual_package", pkg)  # must not raise — null treated as absent

    def test_mixed_legacy_and_new_scenes_in_same_package(self):
        pkg = _make_visual_package([
            _make_scene(scene_id="s01"),  # legacy, no fields
            _make_scene(scene_id="s02", setting_type="interior", lighting_selection="warm"),
        ])
        validate("visual_package", pkg)  # must not raise


# ---------------------------------------------------------------------------
# Round-trip serialization (Objective 7, check 8)
# ---------------------------------------------------------------------------

def test_round_trip_json_serialization():
    import json
    scenes = [{"scene_id": "s01", "presentation_plan": {"presentation_method": "section_cut"}}]
    assign_setting_and_lighting(scenes)
    dumped = json.dumps(scenes)
    reloaded = json.loads(dumped)
    assert reloaded[0]["setting_type"] == "underground"
    assert reloaded[0]["lighting_selection"] == "underground"


# ---------------------------------------------------------------------------
# One writer only; no second writer; Stage B does not mutate (Objective 6, 8)
# ---------------------------------------------------------------------------

class TestSingleWriter:
    def test_only_assign_setting_and_lighting_writes_these_keys_in_vpd(self):
        source = Path("hypogeum/agents/visual_package_director.py").read_text()
        assert source.count('["setting_type"] =') == 0  # never written by direct assignment here
        assert source.count("assign_setting_and_lighting(") == 1  # exactly one call site

    def test_stage_b_prompt_file_not_updated_to_reference_new_fields(self):
        # Deliberate: this checkpoint propagates DATA only. No new MANDATORY
        # compliance section was added to Stage B's base prompt — that would
        # be "activating new DNA behavior," explicitly out of scope.
        source = Path("hypogeum/prompts/vpd_scene_prompts.txt").read_text()
        assert "setting_type" not in source
        assert "lighting_selection" not in source
        assert "SETTING TYPE COMPLIANCE" not in source.upper()
        assert "LIGHTING SELECTION COMPLIANCE" not in source.upper()

    def test_vpd_scene_prompts_batch_output_schema_excludes_these_fields(self):
        # Stage B is not expected to read, write, or return them.
        import inspect
        from hypogeum import schemas
        body = inspect.getsource(schemas._validate_vpd_scene_prompts_batch)
        assert "setting_type" not in body
        assert "lighting_selection" not in body


# ---------------------------------------------------------------------------
# Objective 6/8 — no ownership leakage into other DNA/provider layers
# ---------------------------------------------------------------------------

class TestNoOwnershipLeakage:
    @pytest.fixture
    def classifier_source(self):
        return Path("hypogeum/agents/setting_lighting_classifier.py").read_text().lower()

    def test_no_camera_vocabulary(self, classifier_source):
        for term in ("slow_push_in", "camera_movement", "camera_angle", "dolly", "orbital_arc"):
            assert term not in classifier_source

    def test_no_motion_vocabulary(self, classifier_source):
        for term in ("subject behavior", "heat shimmer", "speed_ramp"):
            assert term not in classifier_source

    def test_no_composition_vocabulary(self, classifier_source):
        for term in ("rule_of_thirds", "negative_space_dominant", "diagonal_leading_line"):
            assert term not in classifier_source

    def test_no_brand_or_color_content(self, classifier_source):
        import re
        assert re.search(r"#[0-9a-f]{6}\b", classifier_source) is None  # no hex color codes
        for term in ("aged_gold", "near_black", "wordmark", "typography"):
            assert term not in classifier_source

    def test_no_provider_vocabulary(self, classifier_source):
        for term in ("veo", "seedance", "muapi", "byteplus"):
            assert term not in classifier_source

    def test_lighting_selection_taxonomy_has_no_composition_camera_or_brand_terms(self):
        for value in LIGHTING_SELECTION_VALUES:
            assert value not in ("rule_of_thirds_left", "slow_push_in", "gold", "aged_gold")

    def test_setting_type_taxonomy_has_no_lighting_or_provider_terms(self):
        for value in SETTING_TYPE_VALUES:
            assert value not in ("warm", "harsh", "atmospheric", "veo", "seedance")

    def test_no_provider_calls_possible(self):
        source = Path("hypogeum/agents/setting_lighting_classifier.py").read_text()
        assert "import muapi" not in source
        assert "from hypogeum.muapi import" not in source
        assert "run_agent(" not in source
        assert "requests" not in source
        assert "urllib" not in source
        assert "random" not in source  # no random selection permitted


def test_vpd_scene_prompts_batch_schema_still_valid_after_b2_3():
    """
    Regression check mirroring the established B1.2A/B2.1/B2.2 pattern:
    this checkpoint touches visual_package's schema and
    visual_package_director.py's internal payload construction, not the
    Stage B OUTPUT schema — confirmed by actually validating a
    representative sample.
    """
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
