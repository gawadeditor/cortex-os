"""
tests/test_visual_intelligence.py — Tests for the Visual Intelligence architecture (V0–V6)

Tests cover:
  V0 — Visual Language Engine (pattern lookup, forbidden defaults)
  V1 — vpd_presentation_plan_batch schema validation
  V2 — vpd_visual_intent_qa_batch schema validation
  V3 — shot_diversity_plan schema validation
  V4 — editorial_qa_report schema validation
  V6 — Assembly Intelligence checks
"""

from __future__ import annotations

import pytest

from hypogeum.visual_language_engine import (
    FORBIDDEN_DEFAULTS,
    VISUAL_LANGUAGE_PATTERNS,
    get_presentation_method,
    get_method_detail,
    list_all_intents,
    list_all_methods,
)
from hypogeum.schemas import SchemaError, validate


# ---------------------------------------------------------------------------
# V0 — Visual Language Engine tests
# ---------------------------------------------------------------------------

class TestVisualLanguageEngine:
    """Tests for hypogeum/visual_language_engine.py"""

    def test_vle_pattern_lookup(self):
        """get_presentation_method('hidden_structure') returns architectural_cutaway."""
        methods = get_presentation_method("hidden_structure")
        assert isinstance(methods, list)
        assert "architectural_cutaway" in methods

    def test_vle_underground_lookup(self):
        """get_presentation_method('underground') returns section_cut and vertical_reveal."""
        methods = get_presentation_method("underground")
        assert "section_cut" in methods
        assert "vertical_reveal" in methods

    def test_vle_unknown_intent_returns_empty(self):
        """get_presentation_method with unknown intent returns empty list."""
        result = get_presentation_method("nonexistent_intent_xyz")
        assert result == []

    def test_vle_forbidden_defaults(self):
        """'split_screen_for_two_ideas' is in FORBIDDEN_DEFAULTS."""
        assert "split_screen_for_two_ideas" in FORBIDDEN_DEFAULTS

    def test_vle_forbidden_defaults_completeness(self):
        """FORBIDDEN_DEFAULTS contains all six defined forbidden defaults."""
        expected = {
            "split_screen_for_two_ideas",
            "side_by_side_composition",
            "independent_camera_moves_composited",
            "talking_head",
            "news_package_style",
            "static_camera",
        }
        assert expected.issubset(FORBIDDEN_DEFAULTS)

    def test_vle_method_detail_keys(self):
        """get_method_detail returns all required keys for a known method."""
        detail = get_method_detail("architectural_cutaway")
        assert "description" in detail
        assert "when_to_use" in detail
        assert "forbidden" in detail
        assert "camera_strategy" in detail
        assert "spatial_rule" in detail

    def test_vle_method_detail_forbidden_list(self):
        """architectural_cutaway forbidden list includes split_screen."""
        detail = get_method_detail("architectural_cutaway")
        assert "split_screen" in detail["forbidden"]

    def test_vle_method_detail_unknown_returns_empty(self):
        """get_method_detail returns empty dict for unknown key."""
        result = get_method_detail("nonexistent_method_xyz")
        assert result == {}

    def test_vle_list_all_intents(self):
        """list_all_intents returns a non-empty list."""
        intents = list_all_intents()
        assert len(intents) > 0
        assert "hidden_structure" in intents
        assert "underground" in intents

    def test_vle_list_all_methods(self):
        """list_all_methods returns all known methods."""
        methods = list_all_methods()
        assert "architectural_cutaway" in methods
        assert "slow_reveal" in methods
        assert "point_of_view" in methods

    def test_vle_pattern_dict_structure(self):
        """VISUAL_LANGUAGE_PATTERNS has the expected top-level keys."""
        assert "narrative_intent" in VISUAL_LANGUAGE_PATTERNS
        assert "presentation_methods" in VISUAL_LANGUAGE_PATTERNS
        assert "forbidden_defaults" in VISUAL_LANGUAGE_PATTERNS
        assert "spatial_relationship_templates" in VISUAL_LANGUAGE_PATTERNS


# ---------------------------------------------------------------------------
# V1 — vpd_presentation_plan_batch schema tests
# ---------------------------------------------------------------------------

def _make_presentation_plan(scene_id: str = "s01") -> dict:
    return {
        "scene_id": scene_id,
        "narrative_goal": "Reveal the hidden city beneath the arena floor",
        "visual_concept": "Arena floor and tunnels coexist in one integrated frame",
        "presentation_method": "architectural_cutaway",
        "camera_strategy": "Single continuous dolly pushes through the cutaway plane",
        "spatial_relationship": "Arena floor directly above hypogeum tunnels in one frame",
        "forbidden_techniques": ["split_screen", "side_by_side"],
    }


def _make_presentation_plan_batch(plans: list | None = None) -> dict:
    return {"presentation_plans": [_make_presentation_plan()] if plans is None else plans}


class TestPresentationPlanSchema:
    """Tests for vpd_presentation_plan_batch schema."""

    def test_presentation_plan_schema_valid(self):
        """A valid presentation_plan_batch passes validation."""
        validate("vpd_presentation_plan_batch", _make_presentation_plan_batch())

    def test_presentation_plan_schema_multiple_scenes(self):
        """Batch with multiple plans passes validation."""
        batch = _make_presentation_plan_batch([
            _make_presentation_plan("s01"),
            _make_presentation_plan("s02"),
            _make_presentation_plan("s03"),
        ])
        validate("vpd_presentation_plan_batch", batch)

    def test_presentation_plan_schema_missing_scene_id(self):
        """Missing scene_id raises SchemaError."""
        plan = _make_presentation_plan()
        del plan["scene_id"]
        batch = {"presentation_plans": [plan]}
        with pytest.raises(SchemaError) as exc_info:
            validate("vpd_presentation_plan_batch", batch)
        assert "scene_id" in str(exc_info.value)

    def test_presentation_plan_schema_missing_presentation_method(self):
        """Missing presentation_method raises SchemaError."""
        plan = _make_presentation_plan()
        del plan["presentation_method"]
        batch = {"presentation_plans": [plan]}
        with pytest.raises(SchemaError) as exc_info:
            validate("vpd_presentation_plan_batch", batch)
        assert "presentation_method" in str(exc_info.value)

    def test_presentation_plan_schema_forbidden_techniques_not_list(self):
        """forbidden_techniques must be a list."""
        plan = _make_presentation_plan()
        plan["forbidden_techniques"] = "split_screen"  # should be a list
        batch = {"presentation_plans": [plan]}
        with pytest.raises(SchemaError):
            validate("vpd_presentation_plan_batch", batch)

    def test_presentation_plan_schema_empty_batch_rejected(self):
        """Empty presentation_plans list raises SchemaError."""
        with pytest.raises(SchemaError):
            validate("vpd_presentation_plan_batch", {"presentation_plans": []})

    def test_presentation_plan_schema_missing_presentations_plans_key(self):
        """Missing presentation_plans key raises SchemaError."""
        with pytest.raises(SchemaError):
            validate("vpd_presentation_plan_batch", {})


# ---------------------------------------------------------------------------
# V2 — vpd_visual_intent_qa_batch schema tests
# ---------------------------------------------------------------------------

def _make_qa_result(scene_id: str = "s01", qa_pass: bool = True) -> dict:
    return {
        "scene_id": scene_id,
        "qa_pass": qa_pass,
        "rejection_reason": None if qa_pass else "Prompt describes two independent cameras. Spatial relationship absent.",
        "checks": {
            "narrative_goal_met": qa_pass,
            "presentation_method_satisfied": qa_pass,
            "spatial_relationship_present": qa_pass,
            "forbidden_techniques_absent": qa_pass,
        },
    }


def _make_qa_batch(results: list | None = None) -> dict:
    return {"qa_results": [_make_qa_result()] if results is None else results}


class TestVisualIntentQASchema:
    """Tests for vpd_visual_intent_qa_batch schema."""

    def test_visual_intent_qa_schema_valid(self):
        """A valid qa batch passes validation."""
        validate("vpd_visual_intent_qa_batch", _make_qa_batch())

    def test_visual_intent_qa_schema_fail_with_reason(self):
        """A failing result with rejection_reason passes validation."""
        batch = _make_qa_batch([_make_qa_result("s01", qa_pass=False)])
        validate("vpd_visual_intent_qa_batch", batch)

    def test_visual_intent_qa_schema_invalid_type(self):
        """qa_pass must be bool, not string."""
        result = _make_qa_result()
        result["qa_pass"] = "true"  # should be bool
        batch = {"qa_results": [result]}
        with pytest.raises(SchemaError) as exc_info:
            validate("vpd_visual_intent_qa_batch", batch)
        assert "qa_pass" in str(exc_info.value)

    def test_visual_intent_qa_schema_rejection_reason_wrong_type(self):
        """rejection_reason must be str or null."""
        result = _make_qa_result()
        result["rejection_reason"] = 42  # wrong type
        batch = {"qa_results": [result]}
        with pytest.raises(SchemaError) as exc_info:
            validate("vpd_visual_intent_qa_batch", batch)
        assert "rejection_reason" in str(exc_info.value)

    def test_visual_intent_qa_schema_missing_checks(self):
        """Missing checks dict raises SchemaError."""
        result = _make_qa_result()
        del result["checks"]
        batch = {"qa_results": [result]}
        with pytest.raises(SchemaError):
            validate("vpd_visual_intent_qa_batch", batch)

    def test_visual_intent_qa_schema_missing_check_key(self):
        """Missing a check key inside checks dict raises SchemaError."""
        result = _make_qa_result()
        del result["checks"]["spatial_relationship_present"]
        batch = {"qa_results": [result]}
        with pytest.raises(SchemaError):
            validate("vpd_visual_intent_qa_batch", batch)

    def test_visual_intent_qa_schema_empty_results_rejected(self):
        """Empty qa_results list raises SchemaError."""
        with pytest.raises(SchemaError):
            validate("vpd_visual_intent_qa_batch", {"qa_results": []})


# ---------------------------------------------------------------------------
# V3 — shot_diversity_plan schema tests
# ---------------------------------------------------------------------------

def _make_diversity_assignment(scene_id: str = "s01") -> dict:
    return {
        "scene_id": scene_id,
        "shot_scale": "wide_establishing",
        "camera_movement": "slow_push_in",
        "camera_angle": "eye_level",
        "composition": "rule_of_thirds_left",
        "color_palette": "deep amber torch glow",
        "environment": "Roman arena exterior",
        "subject": "stone archway",
        "visual_motif": "fire-lit bronze threshold",
        "reuse_allowed": False,
    }


def _make_diversity_plan(assignments: list | None = None) -> dict:
    return {
        "episode_id": "ep_test_001",
        "diversity_map": {
            "shot_scales": ["wide_establishing", "close_up"],
            "camera_movements": ["slow_push_in", "controlled_pull_back"],
            "environments": ["Roman arena exterior"],
            "compositions": ["rule_of_thirds_left"],
        },
        "scene_assignments": [_make_diversity_assignment()] if assignments is None else assignments,
        "violations": [],
        "diversity_score": 0.85,
    }


class TestShotDiversitySchema:
    """Tests for shot_diversity_plan schema."""

    def test_shot_diversity_schema_valid(self):
        """A valid diversity plan passes validation."""
        validate("shot_diversity_plan", _make_diversity_plan())

    def test_shot_diversity_schema_with_violations(self):
        """Diversity plan with violations passes validation."""
        plan = _make_diversity_plan()
        plan["violations"] = [{
            "violation_id": "viol_01",
            "violation_type": "consecutive_shot_scale",
            "scenes_affected": ["s01", "s02"],
            "description": "wide_establishing repeated consecutively",
        }]
        validate("shot_diversity_plan", plan)

    def test_shot_diversity_schema_missing_scene_id(self):
        """Missing scene_id in assignment raises SchemaError."""
        assignment = _make_diversity_assignment()
        del assignment["scene_id"]
        plan = _make_diversity_plan([assignment])
        with pytest.raises(SchemaError) as exc_info:
            validate("shot_diversity_plan", plan)
        assert "scene_id" in str(exc_info.value)

    def test_shot_diversity_schema_reuse_allowed_not_bool(self):
        """reuse_allowed must be bool."""
        assignment = _make_diversity_assignment()
        assignment["reuse_allowed"] = "no"  # wrong type
        plan = _make_diversity_plan([assignment])
        with pytest.raises(SchemaError):
            validate("shot_diversity_plan", plan)

    def test_shot_diversity_schema_missing_diversity_map_key(self):
        """Missing shot_scales in diversity_map raises SchemaError."""
        plan = _make_diversity_plan()
        del plan["diversity_map"]["shot_scales"]
        with pytest.raises(SchemaError):
            validate("shot_diversity_plan", plan)

    def test_shot_diversity_schema_empty_assignments_rejected(self):
        """Empty scene_assignments raises SchemaError."""
        plan = _make_diversity_plan([])
        with pytest.raises(SchemaError):
            validate("shot_diversity_plan", plan)

    def test_shot_diversity_score_must_be_numeric(self):
        """diversity_score must be numeric."""
        plan = _make_diversity_plan()
        plan["diversity_score"] = "high"  # wrong type
        with pytest.raises(SchemaError):
            validate("shot_diversity_plan", plan)


# ---------------------------------------------------------------------------
# V4 — editorial_qa_report schema tests
# ---------------------------------------------------------------------------

def _make_editorial_check(check_id: str = "check_01", status: str = "pass") -> dict:
    return {
        "check_id": check_id,
        "status": status,
        "detail": f"Check {check_id} result: {status}",
    }


def _make_editorial_qa_report(
    verdict: str = "APPROVED",
    failures: list | None = None,
    warnings: list | None = None,
) -> dict:
    approved = verdict != "REJECTED"
    return {
        "report_id": "editorial_qa_ep_test_abc12345",
        "episode_id": "ep_test_001",
        "evaluated_at": "2026-07-05T12:00:00+00:00",
        "overall_verdict": verdict,
        "approved": approved,
        "checks": [_make_editorial_check(f"check_{i:02d}") for i in range(1, 9)],
        "failures": failures or [],
        "warnings": warnings or [],
    }


class TestEditorialQASchema:
    """Tests for editorial_qa_report schema."""

    def test_editorial_qa_schema_valid(self):
        """A valid editorial QA report passes validation."""
        validate("editorial_qa_report", _make_editorial_qa_report())

    def test_editorial_qa_schema_warning_verdict(self):
        """WARNING verdict passes validation."""
        report = _make_editorial_qa_report(
            verdict="WARNING",
            warnings=["check_01: consecutive slow_reveal at s04–s05"],
        )
        validate("editorial_qa_report", report)

    def test_editorial_qa_schema_rejected_verdict(self):
        """REJECTED verdict with failures passes validation."""
        report = _make_editorial_qa_report(
            verdict="REJECTED",
            failures=["check_04: split_screen found in scene s03 prompt"],
        )
        validate("editorial_qa_report", report)

    def test_editorial_qa_verdict_enum(self):
        """overall_verdict must be APPROVED, WARNING, or REJECTED."""
        report = _make_editorial_qa_report()
        report["overall_verdict"] = "PASSED"  # not a valid value
        with pytest.raises(SchemaError) as exc_info:
            validate("editorial_qa_report", report)
        assert "overall_verdict" in str(exc_info.value)

    def test_editorial_qa_approved_wrong_type(self):
        """approved must be bool."""
        report = _make_editorial_qa_report()
        report["approved"] = "yes"  # wrong type
        with pytest.raises(SchemaError):
            validate("editorial_qa_report", report)

    def test_editorial_qa_missing_report_id(self):
        """Missing report_id raises SchemaError."""
        report = _make_editorial_qa_report()
        del report["report_id"]
        with pytest.raises(SchemaError):
            validate("editorial_qa_report", report)

    def test_editorial_qa_check_missing_status(self):
        """Check entry missing status raises SchemaError."""
        report = _make_editorial_qa_report()
        del report["checks"][0]["status"]
        with pytest.raises(SchemaError):
            validate("editorial_qa_report", report)

    def test_editorial_qa_check_invalid_status(self):
        """Check status must be pass/warn/fail."""
        report = _make_editorial_qa_report()
        report["checks"][0]["status"] = "ok"  # not valid
        with pytest.raises(SchemaError):
            validate("editorial_qa_report", report)


# ---------------------------------------------------------------------------
# V4 — Editorial QA deterministic checks
# ---------------------------------------------------------------------------

class TestEditorialQAAgent:
    """Tests for hypogeum/agents/editorial_qa.py deterministic logic."""

    def _make_config(self):
        """Return a minimal config-like object."""
        class _Config:
            pass
        return _Config()

    def _make_scene(
        self,
        scene_id: str,
        method: str = "slow_reveal",
        setting: str = "ancient marketplace",
        shot_scale: str = "wide_establishing",
        camera_movement: str = "slow_push_in",
        motif: str = "fire-lit stone",
        primary_prompt: str = "",
    ) -> dict:
        return {
            "scene_id": scene_id,
            "scene_index": int(scene_id[1:]) if scene_id[1:].isdigit() else 0,
            "visual_spec": {"setting": setting, "subject": "historical subject"},
            "presentation_plan": {
                "presentation_method": method,
                "spatial_relationship": "elements coexist in one frame",
                "forbidden_techniques": [],
            },
            "diversity_assignment": {
                "shot_scale": shot_scale,
                "camera_movement": camera_movement,
                "visual_motif": motif,
                "environment": setting,
            },
            "primary_prompt": primary_prompt,
        }

    def test_editorial_qa_clean_episode_approved(self, tmp_path):
        """A clean episode with varied attributes returns APPROVED or WARNING (not REJECTED)."""
        from hypogeum.agents.editorial_qa import run_editorial_qa

        scenes = [
            self._make_scene("s01", "slow_reveal", "arena exterior", "wide_establishing", "slow_push_in", "stone arch"),
            # underground setting must use an underground method to pass check_06
            self._make_scene("s02", "section_cut", "ancient marketplace", "close_up", "controlled_pull_back", "torch glow"),
            self._make_scene("s03", "architectural_cutaway", "amphitheatre", "medium_wide", "lateral_dolly_left", "amber cutaway"),
            self._make_scene("s04", "low_angle_hero", "forum exterior", "low_angle", "slow_tilt_up", "marble column"),
        ]
        vp = {"scenes": scenes, "episode_metadata": {"episode_title": "Test"}}
        report = run_editorial_qa(vp, "ep_test_001", self._make_config(), tmp_path)
        assert report["overall_verdict"] in ("APPROVED", "WARNING")

    def test_editorial_qa_split_screen_misuse_detected(self, tmp_path):
        """Split-screen in prompt without planning triggers FAIL."""
        from hypogeum.agents.editorial_qa import run_editorial_qa

        scenes = [
            self._make_scene(
                "s01",
                "slow_reveal",
                "ancient city",
                primary_prompt="Left side of the split frame shows the exterior arena while the right side reveals the tunnels below.",
            ),
        ]
        vp = {"scenes": scenes, "episode_metadata": {"episode_title": "Test"}}
        report = run_editorial_qa(vp, "ep_test_002", self._make_config(), tmp_path)
        assert report["overall_verdict"] == "REJECTED"
        assert any("check_04" in c["check_id"] and c["status"] == "fail"
                   for c in report["checks"])

    def test_editorial_qa_underground_wrong_method_detected(self, tmp_path):
        """Underground setting without underground method triggers FAIL."""
        from hypogeum.agents.editorial_qa import run_editorial_qa

        scenes = [
            self._make_scene(
                "s01",
                "slow_reveal",  # wrong for underground
                "deep underground hypogeum tunnels beneath the arena",
            ),
        ]
        vp = {"scenes": scenes, "episode_metadata": {"episode_title": "Test"}}
        report = run_editorial_qa(vp, "ep_test_003", self._make_config(), tmp_path)
        assert report["overall_verdict"] == "REJECTED"
        assert any("check_06" in c["check_id"] and c["status"] == "fail"
                   for c in report["checks"])

    def test_editorial_qa_underground_correct_method_passes(self, tmp_path):
        """Underground setting with section_cut passes check_06."""
        from hypogeum.agents.editorial_qa import run_editorial_qa

        scenes = [
            self._make_scene(
                "s01",
                "section_cut",  # correct for underground
                "deep underground hypogeum tunnels beneath the arena",
            ),
        ]
        vp = {"scenes": scenes, "episode_metadata": {"episode_title": "Test"}}
        report = run_editorial_qa(vp, "ep_test_004", self._make_config(), tmp_path)
        underground_check = next(
            c for c in report["checks"] if c["check_id"] == "check_06"
        )
        assert underground_check["status"] == "pass"

    def test_editorial_qa_resumes_from_checkpoint(self, tmp_path):
        """Second call reads checkpoint instead of re-evaluating."""
        from hypogeum.agents.editorial_qa import run_editorial_qa

        scenes = [self._make_scene("s01")]
        vp = {"scenes": scenes, "episode_metadata": {"episode_title": "Test"}}
        report1 = run_editorial_qa(vp, "ep_test_005", self._make_config(), tmp_path)
        # Modify the visual package — second call should return checkpoint result unchanged
        vp["scenes"] = []
        report2 = run_editorial_qa(vp, "ep_test_005", self._make_config(), tmp_path)
        assert report1["report_id"] == report2["report_id"]


# ---------------------------------------------------------------------------
# V6 — Assembly Intelligence tests
# ---------------------------------------------------------------------------

class TestAssemblyIntelligence:
    """Tests for hypogeum/agents/assembly_intelligence.py"""

    def _make_entry(self, clip_asset_id: str, scene_id: str = "s01") -> dict:
        return {
            "clip_asset_id": clip_asset_id,
            "scene_id": scene_id,
            "duration_seconds": 8.0,
        }

    def test_assembly_intelligence_no_issues(self, tmp_path):
        """Clean timeline with unique clips returns pass status."""
        from hypogeum.agents.assembly_intelligence import run_assembly_intelligence

        timeline = [
            self._make_entry("clip_001", "s01"),
            self._make_entry("clip_002", "s02"),
            self._make_entry("clip_003", "s03"),
        ]
        result = run_assembly_intelligence(
            timeline_entries=timeline,
            music_tracks={"main": {"section_key": "main", "asset_id": "music_001"}},
            output_dir=tmp_path,
            episode_id="ep_ai_test_001",
        )
        assert result["overall_status"] == "pass"
        assert result["blocking_count"] == 0

    def test_assembly_intelligence_blocking_clip_exhaustion(self, tmp_path):
        """Same clip repeated 3x returns BLOCKING clip_exhaustion issue."""
        from hypogeum.agents.assembly_intelligence import run_assembly_intelligence

        timeline = [
            self._make_entry("clip_001", "s01"),
            self._make_entry("clip_001", "s02"),
            self._make_entry("clip_001", "s03"),  # 3rd use → exhaustion
        ]
        result = run_assembly_intelligence(
            timeline_entries=timeline,
            music_tracks={},
            output_dir=tmp_path,
            episode_id="ep_ai_test_002",
        )
        assert result["overall_status"] == "fail"
        assert result["blocking_count"] >= 1
        issue_types = [i["issue_type"] for i in result["issues"]]
        assert "clip_exhaustion" in issue_types

    def test_assembly_intelligence_two_uses_allowed(self, tmp_path):
        """Same clip repeated 2x (at or below threshold) does NOT trigger clip_exhaustion."""
        from hypogeum.agents.assembly_intelligence import run_assembly_intelligence

        timeline = [
            self._make_entry("clip_001", "s01"),
            self._make_entry("clip_001", "s02"),  # 2nd use → OK (threshold is 2)
        ]
        result = run_assembly_intelligence(
            timeline_entries=timeline,
            music_tracks={},
            output_dir=tmp_path,
            episode_id="ep_ai_test_003",
        )
        clip_exhaustion_issues = [
            i for i in result["issues"] if i["issue_type"] == "clip_exhaustion"
        ]
        assert len(clip_exhaustion_issues) == 0

    def test_assembly_intelligence_tpad_warning(self, tmp_path):
        """Timeline entry with tpad=True generates tpad_freeze warning."""
        from hypogeum.agents.assembly_intelligence import run_assembly_intelligence

        timeline = [
            {
                "clip_asset_id": "clip_001",
                "scene_id": "s01",
                "duration_seconds": 8.0,
                "tpad": True,
            },
        ]
        result = run_assembly_intelligence(
            timeline_entries=timeline,
            music_tracks={"main": {"section_key": "main", "asset_id": "music_001"}},
            output_dir=tmp_path,
            episode_id="ep_ai_test_004",
        )
        assert result["warning_count"] >= 1
        issue_types = [i["issue_type"] for i in result["issues"]]
        assert "tpad_freeze" in issue_types

    def test_assembly_intelligence_visible_hold_blocking(self, tmp_path):
        """Three consecutive identical clips generate BLOCKING visible_hold."""
        from hypogeum.agents.assembly_intelligence import run_assembly_intelligence

        timeline = [
            self._make_entry("clip_001", "s01"),
            self._make_entry("clip_001", "s02"),
            self._make_entry("clip_001", "s03"),
        ]
        result = run_assembly_intelligence(
            timeline_entries=timeline,
            music_tracks={},
            output_dir=tmp_path,
            episode_id="ep_ai_test_005",
        )
        visible_hold_blocking = [
            i for i in result["issues"]
            if i["issue_type"] == "visible_hold" and i["severity"] == "BLOCKING"
        ]
        assert len(visible_hold_blocking) >= 1

    def test_assembly_intelligence_no_music_warning(self, tmp_path):
        """Empty music_tracks generates music_discontinuity warning."""
        from hypogeum.agents.assembly_intelligence import run_assembly_intelligence

        timeline = [self._make_entry("clip_001", "s01")]
        result = run_assembly_intelligence(
            timeline_entries=timeline,
            music_tracks={},
            output_dir=tmp_path,
            episode_id="ep_ai_test_006",
        )
        issue_types = [i["issue_type"] for i in result["issues"]]
        assert "music_discontinuity" in issue_types

    def test_assembly_intelligence_resumes_from_checkpoint(self, tmp_path):
        """Second call reads checkpoint — does not recompute."""
        from hypogeum.agents.assembly_intelligence import run_assembly_intelligence

        timeline = [self._make_entry("clip_001", "s01")]
        result1 = run_assembly_intelligence(
            timeline_entries=timeline,
            music_tracks={},
            output_dir=tmp_path,
            episode_id="ep_ai_test_007",
        )
        # Change the timeline — should still return cached result
        timeline2 = [
            self._make_entry("clip_001", "s01"),
            self._make_entry("clip_001", "s02"),
            self._make_entry("clip_001", "s03"),
        ]
        result2 = run_assembly_intelligence(
            timeline_entries=timeline2,
            music_tracks={},
            output_dir=tmp_path,
            episode_id="ep_ai_test_007",
        )
        assert result1["assembly_intelligence_id"] == result2["assembly_intelligence_id"]

    def test_assembly_intelligence_result_structure(self, tmp_path):
        """Result dict has all required keys."""
        from hypogeum.agents.assembly_intelligence import run_assembly_intelligence

        result = run_assembly_intelligence(
            timeline_entries=[self._make_entry("clip_001")],
            music_tracks={},
            output_dir=tmp_path,
            episode_id="ep_ai_test_008",
        )
        assert "assembly_intelligence_id" in result
        assert "episode_id" in result
        assert "evaluated_at" in result
        assert "issues" in result
        assert "overall_status" in result
        assert "blocking_count" in result
        assert "warning_count" in result
        assert result["overall_status"] in ("pass", "warn", "fail")
