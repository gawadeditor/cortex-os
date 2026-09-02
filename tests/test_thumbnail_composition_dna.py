"""
tests/test_thumbnail_composition_dna.py — Thumbnail DNA v1 tests

Covers the permanent, topic-agnostic composition rules promoted from the
Antikythera V3 validation round (2026-07-13):
  - hypogeum.agents.thumbnail_composition_planner: DNA default fields,
    describe_dna_constraints()
  - hypogeum.agents.thumbnail_plan_validator: validate_thumbnail_dna()
  - The Antikythera V4 plan/prompt, produced through the canonical system
    with zero special-case code

Zero provider calls anywhere in this file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hypogeum.agents.thumbnail_composition_planner import (
    plan_composition,
    describe_dna_constraints,
    _TRIGGER_MAP,
    _DEFAULTS,
)
from hypogeum.agents.thumbnail_typography_planner import plan_typography
from hypogeum.agents.thumbnail_plan_validator import (
    validate_thumbnail_dna,
    validate_all_candidates_thumbnail_dna,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Canonical single source
# ---------------------------------------------------------------------------

class TestCanonicalDNASource:
    def test_dna_defaults_defined_once_in_composition_planner(self):
        for field in (
            "headline_zone_width_pct", "headline_visual_weight_ceiling_pct",
            "focal_zone_buffer", "highlight_containment",
            "hero_object_detail_density", "mobile_readability_target",
            "exception_reason",
        ):
            assert field in _DEFAULTS

    @pytest.mark.parametrize("trigger", list(_TRIGGER_MAP.keys()))
    def test_every_trigger_inherits_dna_defaults(self, trigger):
        plan = plan_composition(trigger)
        for field, default in _DEFAULTS.items():
            assert field in plan

    def test_unknown_trigger_falls_back_and_still_carries_dna(self):
        plan = plan_composition("not_a_real_trigger")
        assert plan["focal_zone_buffer"] == "required"
        assert plan["mobile_readability_target"] == "320x180"

    def test_no_provider_calls_possible_from_planner_or_validator(self):
        # Check for actual import/call statements, not any mention of the
        # word "muapi" — a docstring citation (e.g. referencing
        # hypogeum.muapi.generate_thumbnail's docstring as design-target
        # evidence) is documentation, not a provider call.
        for relative_path in (
            "hypogeum/agents/thumbnail_composition_planner.py",
            "hypogeum/agents/thumbnail_plan_validator.py",
        ):
            source = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
            assert "import muapi" not in source
            assert "from hypogeum.muapi import" not in source
            assert "requests." not in source
            assert "run_agent(" not in source


# ---------------------------------------------------------------------------
# Rule 1 — headline visual-weight ceiling
# ---------------------------------------------------------------------------

class TestHeadlineVisualWeightCeiling:
    def test_default_headline_zone_width_and_ceiling(self):
        # "betrayal" carries no per-trigger override — tests the topic-agnostic
        # DNA v1 default. "impossibility" has its own DNA v2 override (tested
        # separately below), tightened during the V4 regression correction.
        plan = plan_composition("betrayal")
        assert plan["headline_zone_width_pct"] == [30, 38]
        assert plan["headline_visual_weight_ceiling_pct"] == 40

    def test_impossibility_headline_zone_width_override(self):
        # DNA v2 (2026-07-13, V4 regression correction): "impossibility"
        # overrides the generic [30, 38] default to a tighter [30, 34].
        plan = plan_composition("impossibility")
        assert plan["headline_zone_width_pct"] == [30, 34]
        assert plan["headline_emphasis_max_width_pct"] == 34

    def test_no_fixed_antikythera_specific_reduction_encoded(self):
        # The general principle (a ceiling + default range) must be encoded,
        # not a one-off "reduce by 8-10%" instruction copied from a single
        # generation.
        source = (
            _REPO_ROOT / "hypogeum" / "agents" / "thumbnail_composition_planner.py"
        ).read_text()
        assert "8-10%" not in source
        assert "8%" not in source and "10% reduction" not in source

    def test_headline_at_or_above_ceiling_warns_without_exception(self):
        plan = plan_composition("impossibility")
        plan["headline_zone_width_pct"] = [35, 42]
        result = validate_thumbnail_dna({"candidate_id": "c1", "composition_plan": plan})
        assert result["passed"] is True  # warning only, not blocking
        assert any("visual-weight ceiling" in w for w in result["warnings"])

    def test_exception_reason_present_suppresses_the_warning(self):
        plan = plan_composition("impossibility")
        plan["headline_zone_width_pct"] = [35, 42]
        plan["exception_reason"] = "concept is explicitly typography-led per approved concept brief"
        result = validate_thumbnail_dna({"candidate_id": "c1", "composition_plan": plan})
        assert result["warnings"] == []


# ---------------------------------------------------------------------------
# Rule 2 — focal-zone breathing room / eye path
# ---------------------------------------------------------------------------

class TestFocalZoneBreathingRoom:
    def test_focal_zone_buffer_required_by_default(self):
        plan = plan_composition("betrayal")
        assert plan["focal_zone_buffer"] == "required"

    def test_missing_buffer_is_blocking(self):
        plan = plan_composition("betrayal")
        plan["focal_zone_buffer"] = "not_set"
        result = validate_thumbnail_dna({"candidate_id": "c1", "composition_plan": plan})
        assert result["passed"] is False
        assert any("focal_zone_buffer" in e for e in result["errors"])

    def test_eye_path_required(self):
        plan = plan_composition("betrayal")
        assert plan["eye_path"], "every plan must declare a non-empty eye_path"

    def test_missing_eye_path_is_blocking(self):
        plan = plan_composition("betrayal")
        plan["eye_path"] = []
        result = validate_thumbnail_dna({"candidate_id": "c1", "composition_plan": plan})
        assert result["passed"] is False
        assert any("eye_path" in e for e in result["errors"])

    def test_text_zone_overlapping_focal_zone_is_blocking(self):
        plan = plan_composition("betrayal")
        plan["text_zone"] = plan["focal_zone"]
        result = validate_thumbnail_dna({"candidate_id": "c1", "composition_plan": plan})
        assert result["passed"] is False
        assert any("overlaps focal_zone" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# Rule 4 — focal dominance budget (primary/secondary/tertiary)
# ---------------------------------------------------------------------------

class TestFocalDominanceBudget:
    def test_eye_path_doubles_as_priority_order_no_duplicate_field(self):
        # "visual_priority_order" was explicitly NOT added as a separate
        # field — eye_path already encodes it. Confirm no duplicate field
        # was introduced.
        plan = plan_composition("impossibility")
        assert "visual_priority_order" not in plan

    def test_primary_secondary_tertiary_are_distinct_positions(self):
        plan = plan_composition("betrayal")
        eye_path = plan["eye_path"]
        assert len(eye_path) >= 3
        primary, secondary = eye_path[0], eye_path[1]
        tertiary_plus = eye_path[2:]
        assert primary != secondary
        assert primary not in tertiary_plus or len(set(eye_path)) == len(eye_path)

    def test_brightest_source_role_derived_from_secondary_read(self):
        plan = plan_composition("betrayal")
        assert plan["brightest_source_role"] == plan["eye_path"][1]

    def test_hero_detail_density_competing_with_critical_readability_warns(self):
        plan = plan_composition("impossibility")
        plan["hero_object_detail_density"] = "high"
        assert plan["subject_readability_at_320px"] == "critical"
        result = validate_thumbnail_dna({"candidate_id": "c1", "composition_plan": plan})
        assert result["passed"] is True
        assert any("detail_density" in w or "detail density" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# Rule 3 — highlight containment
# ---------------------------------------------------------------------------

class TestHighlightContainment:
    def test_highlight_containment_required_by_default(self):
        plan = plan_composition("impossibility")
        assert plan["highlight_containment"] == "required"

    def test_non_required_containment_warns(self):
        plan = plan_composition("impossibility")
        plan["highlight_containment"] = "loose"
        result = validate_thumbnail_dna({"candidate_id": "c1", "composition_plan": plan})
        assert result["passed"] is True
        assert any("containment" in w for w in result["warnings"])

    def test_uncontrolled_blown_highlight_in_prompt_is_blocking(self):
        plan = plan_composition("impossibility")
        candidate = {
            "candidate_id": "c1",
            "composition_plan": plan,
            "image_prompt": "the doorway is blown out, blinding light fills the whole frame",
        }
        result = validate_thumbnail_dna(candidate)
        assert result["passed"] is False
        assert any("blown highlight" in e for e in result["errors"])

    def test_blown_highlight_with_containment_language_passes(self):
        plan = plan_composition("impossibility")
        candidate = {
            "candidate_id": "c1",
            "composition_plan": plan,
            "image_prompt": "a blinding light, but it stays contained and does not spread across the frame",
        }
        result = validate_thumbnail_dna(candidate)
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# Rule 5 — mobile-size validation
# ---------------------------------------------------------------------------

class TestMobileReadabilityTarget:
    def test_mobile_readability_target_default(self):
        plan = plan_composition("impossibility")
        assert plan["mobile_readability_target"] == "320x180"

    def test_missing_mobile_target_is_blocking(self):
        plan = plan_composition("impossibility")
        plan["mobile_readability_target"] = None
        result = validate_thumbnail_dna({"candidate_id": "c1", "composition_plan": plan})
        assert result["passed"] is False
        assert any("mobile_readability_target" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# describe_dna_constraints() — topic-agnostic prompt phrase builder
# ---------------------------------------------------------------------------

class TestDescribeDnaConstraints:
    def test_phrase_contains_no_topic_specific_language(self):
        plan = plan_composition("impossibility")
        phrase = describe_dna_constraints(plan)
        lowered = phrase.lower()
        for forbidden in ("antikythera", "mechanism", "gear", "doorway", "ep_003"):
            assert forbidden not in lowered

    def test_phrase_reflects_headline_width_from_plan(self):
        plan = plan_composition("impossibility")
        plan["headline_zone_width_pct"] = [25, 33]
        phrase = describe_dna_constraints(plan)
        assert "25-33%" in phrase

    def test_build_layer_prompts_injects_dna_phrase_into_base_and_hero_not_fx(self):
        from hypogeum.agents.thumbnail_compositor import build_layer_prompts

        prompts = build_layer_prompts(
            {"suggested_hook_text": "IT SHOULDNT EXIST", "visual_thesis": "x",
             "emotional_trigger": "impossibility"},
            plan_composition("impossibility"),
        )
        assert "THUMBNAIL COMPOSITION DNA" in prompts["base_scene"]
        assert "THUMBNAIL COMPOSITION DNA" in prompts["hero_subject"]
        assert "THUMBNAIL COMPOSITION DNA" not in prompts["fx"]


# ---------------------------------------------------------------------------
# Antikythera V4 plan — canonical system, zero special-case code
# ---------------------------------------------------------------------------

class TestAntikytheraV4Plan:
    def test_v4_plan_uses_the_pre_existing_impossibility_trigger(self):
        # "impossibility" is the same emotional_trigger this concept already
        # used in thumbnail_compositor.py's env_descs/subject_descs dicts —
        # not a new trigger added for V4.
        assert "impossibility" in _TRIGGER_MAP

    def test_v4_plan_passes_dna_validation_with_no_special_case_code(self):
        comp_plan = plan_composition("impossibility")
        typo_plan = plan_typography("IT SHOULDN'T EXIST", "impossibility", comp_plan)
        candidate = {
            "candidate_id": "ep003_v4",
            "composition_plan": comp_plan,
            "typography_plan": typo_plan,
            "image_prompt": "",
        }
        result = validate_thumbnail_dna(candidate)
        assert result == {"passed": True, "errors": [], "warnings": []}

    def test_v4_plan_headline_width_tighter_than_v3_hand_authored_range(self):
        # V3 used a hand-picked 36-40%; the canonical DNA default is 30-38% —
        # proof the DNA-driven plan is *slightly* more controlled, not just
        # a copy of V3's number.
        comp_plan = plan_composition("impossibility")
        lo, hi = comp_plan["headline_zone_width_pct"]
        assert hi < 40

    def test_no_antikythera_specific_branch_in_composition_planner(self):
        # A provenance mention in the module docstring/comments (explaining
        # where the DNA rules were promoted from) is expected and fine — what
        # must never exist is an actual conditional/data branch keyed on
        # Antikythera or this specific episode.
        import hypogeum.agents.thumbnail_composition_planner as mod

        assert "antikythera" not in _TRIGGER_MAP
        assert "ep_003" not in _TRIGGER_MAP
        for value in _DEFAULTS.values():
            assert "antikythera" not in str(value).lower()
        for trigger_config in _TRIGGER_MAP.values():
            assert "antikythera" not in str(trigger_config).lower()
            assert "ep_003" not in str(trigger_config).lower()

    def test_no_antikythera_specific_branch_in_plan_validator(self):
        source = (
            _REPO_ROOT / "hypogeum" / "agents" / "thumbnail_plan_validator.py"
        ).read_text().lower()
        for forbidden in ("antikythera", "ep_003", "ep003"):
            assert forbidden not in source

    def test_v4_blueprint_document_exists_and_references_canonical_system(self):
        path = (
            _REPO_ROOT / "docs" / "validation" / "thumbnail_image_generation_validation"
            / "ANTIKYTHERA_COMPOSITION_BLUEPRINT_V4.md"
        )
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "describe_dna_constraints" in text
        assert "IT SHOULDN'T\nEXIST" in text
        assert "No provider call has been made" in text

    def test_v3_blueprint_preserved_not_deleted(self):
        path = (
            _REPO_ROOT / "docs" / "validation" / "thumbnail_image_generation_validation"
            / "ANTIKYTHERA_COMPOSITION_BLUEPRINT_V3.md"
        )
        assert path.exists()


# ---------------------------------------------------------------------------
# validate_all_candidates_thumbnail_dna — batch wrapper
# ---------------------------------------------------------------------------

class TestValidateAllCandidatesThumbnailDna:
    def test_batch_wrapper_aggregates_failures_and_warnings(self):
        good_plan = plan_composition("impossibility")
        bad_plan = plan_composition("impossibility")
        bad_plan["eye_path"] = []
        candidates = [
            {"candidate_id": "good", "composition_plan": good_plan},
            {"candidate_id": "bad", "composition_plan": bad_plan},
        ]
        result = validate_all_candidates_thumbnail_dna(candidates)
        assert result["passed"] is False
        assert len(result["failures"]) == 1
        assert result["failures"][0]["candidate_id"] == "bad"

    def test_skips_silently_when_composition_plan_absent(self):
        result = validate_all_candidates_thumbnail_dna([{"candidate_id": "no_plan"}])
        assert result["passed"] is True
        assert result["failures"] == []


# ---------------------------------------------------------------------------
# Thumbnail DNA v2 (2026-07-13) — V4 regression correction
# ---------------------------------------------------------------------------

class TestDnaV2HeroObjectAndFigureFields:
    def test_hero_object_max_frame_share_default_is_topic_agnostic(self):
        # "betrayal" carries no override — generic default applies.
        plan = plan_composition("betrayal")
        assert plan["hero_object_max_frame_share_pct"] == [25, 35]
        assert plan["hero_object_concealment"] == "partially_concealed"
        assert plan["patina_prominence_max"] == "subtle"

    def test_impossibility_hero_object_override(self):
        plan = plan_composition("impossibility")
        assert plan["hero_object_max_frame_share_pct"] == [28, 32]
        assert plan["hero_object_detail_density"] == "reduced"
        assert plan["hero_object_concealment"] == "partially_concealed"

    def test_focal_priority_derived_from_eye_path_no_duplicate_source(self):
        plan = plan_composition("impossibility")
        assert plan["focal_priority"] == {
            "primary": "hook_text", "secondary": "human_figure", "tertiary": "focal_object",
        }
        # Must be derived from eye_path, not an independently-settable field.
        assert plan["focal_priority"]["primary"] == plan["eye_path"][0]

    def test_impossibility_eye_path_now_includes_human_figure(self):
        # Root-cause fix: the DNA directive's own prose already referenced
        # "the human figure" while eye_path had no such element for this
        # trigger — a pre-existing inconsistency. General refinement, not
        # Antikythera-only (see module docstring rationale).
        plan = plan_composition("impossibility")
        assert "human_figure" in plan["eye_path"]
        assert plan["brightest_source_role"] == "human_figure"

    def test_human_figure_gesture_active_when_figure_present(self):
        plan = plan_composition("impossibility")
        assert plan["human_figure_gesture"] == "active"
        assert plan["human_figure_readable_at_mobile"] == "required"

    def test_human_figure_fields_not_applicable_when_absent(self):
        # "loss" trigger's eye_path has no human_figure element.
        plan = plan_composition("loss")
        assert "human_figure" not in plan["eye_path"]
        assert plan["human_figure_gesture"] == "none"
        assert plan["human_figure_readable_at_mobile"] == "not_applicable"

    def test_headline_emphasis_max_width_derived_not_duplicated(self):
        plan = plan_composition("impossibility")
        assert plan["headline_emphasis_max_width_pct"] == plan["headline_zone_width_pct"][1]

    def test_focal_scale_consistent_with_hero_object_max_frame_share(self):
        # focal_scale (categorical) and hero_object_max_frame_share_pct
        # (numeric) must not contradict each other for "impossibility":
        # "medium" = 25-40% of right frame, matching [28, 32].
        plan = plan_composition("impossibility")
        assert plan["focal_scale"] == "medium"
        lo, hi = plan["hero_object_max_frame_share_pct"]
        assert 25 <= lo and hi <= 40

    def test_describe_dna_constraints_states_hero_ceiling_and_patina_cap(self):
        plan = plan_composition("impossibility")
        directive = describe_dna_constraints(plan)
        assert "28-32%" in directive
        assert "mostly in shadow" in directive
        assert "subtle" in directive
        assert "primary read = hook_text" in directive
        assert "visibly active" in directive

    def test_describe_dna_constraints_stays_topic_agnostic(self):
        plan = plan_composition("impossibility")
        directive = describe_dna_constraints(plan)
        lowered = directive.lower()
        for forbidden in ("antikythera", "mechanism", "gear", "doorway"):
            assert forbidden not in lowered

    def test_no_provider_calls_possible(self):
        # Check for actual import/call statements — a docstring citation of
        # hypogeum.muapi.generate_thumbnail's own docstring (design-target
        # evidence for the V5.1 prompt-length correction) is documentation,
        # not a provider call.
        source = Path(
            "hypogeum/agents/thumbnail_composition_planner.py"
        ).read_text()
        assert "import muapi" not in source
        assert "from hypogeum.muapi import" not in source
        assert "run_agent(" not in source


class TestAntikytheraV5Blueprint:
    def _blueprint_text(self):
        path = (
            _REPO_ROOT / "docs" / "validation" / "thumbnail_image_generation_validation"
            / "ANTIKYTHERA_COMPOSITION_BLUEPRINT_V5.md"
        )
        assert path.exists()
        return path.read_text(encoding="utf-8")

    def test_v5_blueprint_exists_and_v3_v4_preserved(self):
        self._blueprint_text()
        base = _REPO_ROOT / "docs" / "validation" / "thumbnail_image_generation_validation"
        assert (base / "ANTIKYTHERA_COMPOSITION_BLUEPRINT_V3.md").exists()
        assert (base / "ANTIKYTHERA_COMPOSITION_BLUEPRINT_V4.md").exists()

    def test_v5_blueprint_numbers_match_live_composition_plan(self):
        # Guards against drift: the blueprint's stated numbers must match
        # what plan_composition("impossibility") actually produces right now.
        text = self._blueprint_text()
        plan = plan_composition("impossibility")
        lo, hi = plan["headline_zone_width_pct"]
        hero_lo, hero_hi = plan["hero_object_max_frame_share_pct"]
        assert f"{lo}-{hi}%" in text
        assert f"{hero_lo}-{hero_hi}%" in text
        assert str(plan["headline_emphasis_max_width_pct"]) in text

    def test_v5_blueprint_includes_live_dna_directive_verbatim(self):
        text = self._blueprint_text()
        plan = plan_composition("impossibility")
        live_directive = describe_dna_constraints(plan)
        assert live_directive in text

    def test_v5_blueprint_contains_exact_headline_text(self):
        text = self._blueprint_text()
        assert "IT SHOULDN'T\nEXIST" in text

    def test_v5_blueprint_declares_secondary_and_tertiary_read_labels(self):
        text = self._blueprint_text()
        assert "SECONDARY READ" in text
        assert "TERTIARY READ" in text


# ---------------------------------------------------------------------------
# V5.1 — provider-safe compact prompt (2026-07-13)
# ---------------------------------------------------------------------------

class TestCompactDnaDirective:
    def test_full_validation_rules_still_exist_canonically(self):
        # describe_dna_constraints() (verbose) is unchanged and still the
        # source validate_thumbnail_dna() reasons about via composition_plan
        # fields directly — compaction only affects prompt text rendering.
        from hypogeum.agents.thumbnail_composition_planner import describe_dna_constraints
        plan = plan_composition("impossibility")
        full = describe_dna_constraints(plan)
        assert "28-32%" in full
        assert "30-34%" in full
        result = validate_thumbnail_dna({"composition_plan": plan})
        assert result == {"passed": True, "errors": [], "warnings": []}

    def test_compact_directive_preserves_every_structured_constraint(self):
        from hypogeum.agents.thumbnail_composition_planner import describe_dna_constraints_compact
        plan = plan_composition("impossibility")
        compact = describe_dna_constraints_compact(plan)
        # headline width + emphasis cap
        assert "30-34%" in compact and "34%" in compact
        # mechanism max frame share
        assert "28-32%" in compact
        # hierarchy
        assert "hook_text" in compact and "human_figure" in compact and "focal_object" in compact
        # concealment + detail
        assert "concealed" in compact
        # patina cap
        assert "subtle" in compact
        # mobile readability
        assert "320x180" in compact
        # highlight containment
        assert "localized" in compact
        # no-incidental-text rule
        assert "watermarks" in compact

    def test_compact_directive_shorter_than_full(self):
        from hypogeum.agents.thumbnail_composition_planner import (
            describe_dna_constraints, describe_dna_constraints_compact,
        )
        plan = plan_composition("impossibility")
        assert len(describe_dna_constraints_compact(plan)) < len(describe_dna_constraints(plan))

    def test_compact_directive_states_each_rule_once(self):
        from hypogeum.agents.thumbnail_composition_planner import describe_dna_constraints_compact
        plan = plan_composition("impossibility")
        compact = describe_dna_constraints_compact(plan)
        assert compact.count("320x180") == 1
        assert compact.count("30-34%") == 1
        assert compact.count("28-32%") == 1

    def test_no_hardcoded_dna_hex_or_percentages_independent_of_plan(self):
        # describe_dna_constraints_compact() must read from composition_plan,
        # not hardcode Antikythera-tuned numbers as literals.
        plan2 = plan_composition("betrayal")
        from hypogeum.agents.thumbnail_composition_planner import describe_dna_constraints_compact
        compact2 = describe_dna_constraints_compact(plan2)
        assert "30-38%" in compact2  # betrayal uses the generic DNA v1 default, not impossibility's override
        assert "28-32%" not in compact2  # impossibility-specific override must not leak


class TestCompactBrandDirective:
    def test_compact_brand_directive_preserves_ratios_and_forbidden_items(self):
        from hypogeum.brand import visual_identity as vi
        compact = vi.build_visual_identity_directive_compact("thumbnail")
        assert "60-70%" in compact and "20-30%" in compact and "5-10%" in compact
        for item in ("teal", "turquoise verdigris", "polished gold", "fluorescent", "saturated"):
            assert item in compact
        assert "fantasy-art finish" in compact and "museum-display finish" in compact and "glossy modern product-render" in compact

    def test_compact_brand_directive_shorter_than_full(self):
        from hypogeum.brand import visual_identity as vi
        full = vi.build_visual_identity_directive("thumbnail")
        compact = vi.build_visual_identity_directive_compact("thumbnail")
        assert len(compact) < len(full)

    def test_compact_brand_directive_reads_same_palette_source(self):
        from hypogeum.brand import visual_identity as vi
        palette = vi.get_palette()
        compact = vi.build_visual_identity_directive_compact("thumbnail")
        assert palette["near_black"]["hex"] in compact
        assert palette["aged_gold"]["hex"] in compact

    def test_adaptive_and_light_tiers_unaffected_by_compaction(self):
        from hypogeum.brand import visual_identity as vi
        for mode in ("video_scene", "reference_image"):
            assert vi.build_visual_identity_directive_compact(mode) == vi.build_visual_identity_directive(mode)


class TestV51ProviderSafePrompt:
    def _blueprint_text(self):
        path = (
            _REPO_ROOT / "docs" / "validation" / "thumbnail_image_generation_validation"
            / "ANTIKYTHERA_COMPOSITION_BLUEPRINT_V5_1.md"
        )
        assert path.exists()
        return path.read_text(encoding="utf-8")

    def test_v5_1_blueprint_exists_and_prior_versions_preserved(self):
        self._blueprint_text()
        base = _REPO_ROOT / "docs" / "validation" / "thumbnail_image_generation_validation"
        for name in ("ANTIKYTHERA_COMPOSITION_BLUEPRINT_V3.md", "ANTIKYTHERA_COMPOSITION_BLUEPRINT_V4.md",
                     "ANTIKYTHERA_COMPOSITION_BLUEPRINT_V5.md"):
            assert (base / name).exists()

    def test_final_v5_1_prompt_under_safe_ceiling(self, tmp_path):
        from unittest.mock import patch
        import hypogeum.agents.thumbnail_compositor as tc

        plan = plan_composition("impossibility")
        typo = plan_typography("IT SHOULDN'T EXIST", "impossibility", plan)
        candidate = {
            "image_prompt": (
                "Premium archaeological investigative documentary YouTube thumbnail, 16:9. "
                "Left zone: two-line carved-stone headline (see typography). Center-left, "
                "off-axis (not centered): a glowing ancient stone doorway lights worn steps; "
                "a human figure caught mid-step, one hand actively reaching toward the light "
                "as if just discovering it, backlit, faceless. Right foreground, partially "
                "cropped by the frame edge: a corroded bronze Antikythera-like mechanism, a "
                "few large gear forms and one rim-lit dial, dark aged bronze. One coherent "
                "camera perspective, one physically continuous underground chamber, one warm "
                "directional light source anchored at the doorway. Restrained, investigative "
                "mood — mystery and discovery, not spectacle."
            ),
            "typography_plan": typo,
            "composition_plan": plan,
        }
        captured = {}

        def fake_generate(model, deliverable_id, image_prompt, aspect_ratio, config, local_path, budget_guard, log):
            captured["prompt"] = image_prompt
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(b"x")

        class FakeConfig:
            THUMBNAIL_IMAGE_MODEL = "gpt-image-2-text-to-image"

        class FakeLog:
            def info(self, *a, **k): pass
            def error(self, *a, **k): pass

        with patch.object(tc, "_generate_thumbnail_with_model", side_effect=fake_generate), \
             patch.object(tc, "_normalize_thumbnail"):
            tc.generate_unified_integrated_thumbnail(
                candidate, config=FakeConfig(), episode_id="ep_003", output_dir=tmp_path,
                deliverable_id="v51", log=FakeLog(), apply_wordmark=False,
            )
        assert len(captured["prompt"]) < 4500
        assert captured["prompt"].startswith("Premium archaeological investigative documentary")
        assert captured["prompt"].count("INTEGRATED TYPOGRAPHY") == 1
        assert captured["prompt"].count("THUMBNAIL COMPOSITION DNA") == 1
        assert captured["prompt"].count("HYPOGEUM BRAND") == 1

    def test_topic_prompt_remains_exact_first_component(self):
        text = self._blueprint_text()
        idx_full = text.find("Full assembled prompt")
        idx_topic_start = text.find("Premium archaeological investigative documentary")
        assert idx_topic_start != -1
        assert idx_full == -1 or idx_topic_start < text.find("```", idx_full)

    def test_no_provider_calls_in_this_test_file(self):
        # Every test in this class mocks _generate_thumbnail_with_model
        # directly (see test_final_v5_1_prompt_under_safe_ceiling above) —
        # no real HTTP client is imported anywhere in this module. (Checking
        # the module's own top-level import list, not a raw substring scan,
        # since a raw scan for this exact phrase would self-match the
        # assertion string itself.)
        import ast
        tree = ast.parse(Path(__file__).read_text())
        imported_names = {
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert "requests" not in imported_names
