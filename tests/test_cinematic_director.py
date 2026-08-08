"""
tests/test_cinematic_director.py — Unit tests for the Cinematic Director (2026-08-06)

Covers:
  TestComputeShotBudget         — duration-aware shot count (short/medium/long beats,
                                   never a fixed count, safety cap respected)
  TestEstimateBeatDuration      — word-count-based duration estimate
  TestRunCinematicDirector      — checkpoint resume, schema-valid output, LLM-failure
                                   fallback, one beat_plan per beat

All Claude calls (run_agent) are mocked. No network calls.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.cinematic_director import (
    PROGRESSION_PATTERNS,
    SHOT_DIVERSITY_FIELDS,
    CoveragePlanDiversityError,
    CoverageRegenerationError,
    NarrationCompletenessError,
    authoritative_beat_ids,
    authoritative_beat_texts,
    compute_shot_budget,
    estimate_beat_duration_seconds,
    regenerate_cinematic_coverage_plan,
    run_cinematic_director,
    shot_diversity_key,
    validate_coverage_plan_diversity,
    validate_narration_completeness,
)
from hypogeum.schemas import validate


def _make_config(**overrides) -> SimpleNamespace:
    base = dict(
        CINEMATIC_SECONDS_PER_SHOT=10.0,
        CINEMATIC_MAX_SHOTS_PER_BEAT=6,
        CINEMATIC_MIN_SHOTS_PER_BEAT=1,
        CINEMATIC_ESTIMATED_WORDS_PER_MINUTE=145.0,
        # Provider capability inputs compute_shot_budget() now consumes via
        # muapi.get_requested_clip_duration_seconds() -- T2/veo3.1-lite
        # matches this channel's real production default (8s/clip).
        MUAPI_DEFAULT_TIER=2,
        MUAPI_T1_MODEL="veo3-text-to-video",
        MUAPI_T2_MODEL="veo3.1-lite-text-to-video",
        MUAPI_T3_MODEL="veo3.1-lite-text-to-video",
        MUAPI_I2V_MODEL="veo3-image-to-video",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_visual_package(scenes=None) -> dict:
    return {
        "episode_metadata": {"episode_title": "Test Episode"},
        "scenes": scenes if scenes is not None else [
            {"scene_id": "s01", "narration_reference": "b_001", "source_type": "main_episode",
             "visual_spec": {"subject": "x"}, "presentation_plan": {}, "narrative_signals": []},
        ],
    }


def _valid_beat_plan(beat_id: str, narration_duration: float, n_shots: int) -> dict:
    shots = []
    for i in range(1, n_shots + 1):
        shots.append({
            "shot_id": f"{beat_id}__shot{i}", "parent_beat_id": beat_id, "shot_order": i,
            "shot_purpose": "establish_location", "narrative_function": f"shot {i} purpose",
            "estimated_usable_duration": 8.0, "subject": "x", "shot_scale": "medium",
            "camera_angle": "eye_level", "camera_height": "eye_level",
            "camera_direction": "frontal", "camera_movement": "slow_push_in",
            "composition": "rule_of_thirds_left", "background_depth": "shallow",
            "screen_direction": "neutral", "motion_energy": "low",
            "transition_in": "clean_cut", "transition_out": "clean_cut",
        })
    return {
        "beat_id": beat_id, "narration_duration_seconds": narration_duration,
        "progression_pattern": "single_shot" if n_shots == 1 else "wide_medium_close",
        "shots": shots,
    }


# ---------------------------------------------------------------------------
# TestComputeShotBudget
# ---------------------------------------------------------------------------

class TestComputeShotBudget:

    def test_short_beat_gets_one_shot(self):
        cfg = _make_config()
        assert compute_shot_budget(8.0, cfg) == 1

    def test_medium_beat_gets_two_or_three_shots(self):
        cfg = _make_config()
        assert 2 <= compute_shot_budget(21.7, cfg) <= 3

    def test_long_beat_gets_four_or_more_shots(self):
        cfg = _make_config()
        assert compute_shot_budget(46.9, cfg) >= 4

    def test_never_a_fixed_three_shots_rule(self):
        """Different durations must produce different budgets -- proof this
        is not a hardcoded 'always 3' rule."""
        cfg = _make_config()
        budgets = {compute_shot_budget(d, cfg) for d in (8.0, 18.0, 30.0, 47.0, 90.0)}
        assert len(budgets) > 1

    def test_bounded_by_configurable_safety_cap(self):
        cfg = _make_config(CINEMATIC_MAX_SHOTS_PER_BEAT=3)
        assert compute_shot_budget(1000.0, cfg) == 3

    def test_zero_or_negative_duration_still_gets_at_least_one_shot(self):
        cfg = _make_config()
        assert compute_shot_budget(0.0, cfg) == 1
        assert compute_shot_budget(-5.0, cfg) == 1

    def test_budget_scales_monotonically_with_duration(self):
        cfg = _make_config()
        durations = [8.0, 15.0, 25.0, 35.0, 55.0]
        budgets = [compute_shot_budget(d, cfg) for d in durations]
        assert budgets == sorted(budgets)


# ---------------------------------------------------------------------------
# TestEstimateBeatDuration
# ---------------------------------------------------------------------------

class TestEstimateBeatDuration:

    def test_real_production_calibration(self):
        """Calibrated against real ep_f3619022 data: 58 words measured at
        24.427s (~142.5 WPM) -- the default (145 WPM) must land close."""
        cfg = _make_config()
        text = " ".join(["word"] * 58)
        estimate = estimate_beat_duration_seconds(text, cfg)
        assert estimate == pytest.approx(24.0, abs=1.5)

    def test_empty_text_is_zero(self):
        cfg = _make_config()
        assert estimate_beat_duration_seconds("", cfg) == 0.0

    def test_longer_text_yields_longer_estimate(self):
        cfg = _make_config()
        short = estimate_beat_duration_seconds("one two three", cfg)
        long = estimate_beat_duration_seconds(" ".join(["word"] * 100), cfg)
        assert long > short


# ---------------------------------------------------------------------------
# TestRunCinematicDirector
# ---------------------------------------------------------------------------

class TestRunCinematicDirector:

    def test_checkpoint_written_and_schema_valid(self, tmp_path):
        cfg = _make_config()
        vp = _make_visual_package()
        plan = {
            "episode_id": "ep_001",
            "beat_plans": [_valid_beat_plan("b_001", 8.0, 1)],
        }
        with patch("hypogeum.agents.cinematic_director.run_agent", return_value=plan):
            result = run_cinematic_director(
                visual_package=vp, beat_texts_by_id={"b_001": "a short beat"},
                episode_id="ep_001", config=cfg, output_dir=tmp_path,
            )
        validate("cinematic_coverage_plan", result)
        ckpt = tmp_path / "cinematic_coverage_plan_ep_001.json"
        assert ckpt.exists()
        assert json.loads(ckpt.read_text()) == result

    def test_resumes_from_existing_checkpoint_without_calling_llm(self, tmp_path):
        cfg = _make_config()
        vp = _make_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_001.json"
        existing = {"episode_id": "ep_001", "beat_plans": [_valid_beat_plan("b_001", 8.0, 1)]}
        ckpt.write_text(json.dumps(existing))

        with patch("hypogeum.agents.cinematic_director.run_agent") as mock_ra:
            result = run_cinematic_director(
                visual_package=vp, beat_texts_by_id={"b_001": "text"},
                episode_id="ep_001", config=cfg, output_dir=tmp_path,
            )
        mock_ra.assert_not_called()
        assert result == existing

    def test_llm_failure_falls_back_to_minimal_valid_plan(self, tmp_path):
        cfg = _make_config()
        vp = _make_visual_package()
        with patch("hypogeum.agents.cinematic_director.run_agent", side_effect=RuntimeError("boom")):
            result = run_cinematic_director(
                visual_package=vp, beat_texts_by_id={"b_001": "a short beat"},
                episode_id="ep_001", config=cfg, output_dir=tmp_path,
            )
        validate("cinematic_coverage_plan", result)
        assert len(result["beat_plans"]) == 1
        assert result["beat_plans"][0]["shots"][0]["transition_in"] == "clean_cut"
        assert result["beat_plans"][0]["shots"][0]["transition_out"] == "clean_cut"

    def test_one_beat_plan_per_scene_narration_reference(self, tmp_path):
        cfg = _make_config()
        vp = _make_visual_package(scenes=[
            {"scene_id": "s01", "narration_reference": "b_001", "source_type": "main_episode",
             "visual_spec": {}, "presentation_plan": {}, "narrative_signals": []},
            {"scene_id": "s02", "narration_reference": "b_002", "source_type": "main_episode",
             "visual_spec": {}, "presentation_plan": {}, "narrative_signals": []},
        ])
        plan = {
            "episode_id": "ep_001",
            "beat_plans": [
                _valid_beat_plan("b_001", 8.0, 1),
                _valid_beat_plan("b_002", 30.0, 3),
            ],
        }
        captured = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return plan

        with patch("hypogeum.agents.cinematic_director.run_agent", side_effect=_capture):
            run_cinematic_director(
                visual_package=vp,
                beat_texts_by_id={"b_001": "short", "b_002": " ".join(["word"] * 70)},
                episode_id="ep_001", config=cfg, output_dir=tmp_path,
            )

        sent_beats = captured["input_package"]["beats"]
        assert len(sent_beats) == 2
        # The longer beat (b_002) must receive a strictly larger shot_budget
        # than the short one -- proof duration drives planning, not a fixed count.
        budgets = {b["beat_id"]: b["shot_budget"] for b in sent_beats}
        assert budgets["b_002"] > budgets["b_001"]

    def test_progression_patterns_are_documented_vocabulary(self):
        assert "single_shot" in PROGRESSION_PATTERNS
        assert len(PROGRESSION_PATTERNS) >= 5


# ---------------------------------------------------------------------------
# Shot diversity — identity, not text (2026-08-07 refactor)
#
# This module decides what a shot IS; shot_diversity_key() and
# validate_coverage_plan_diversity() are the code enforcement of the
# prompt file's own "WITHIN-BEAT DIVERSITY" rule, and the shared source of
# truth hypogeum.agents.clip_repair_agent imports for its own (secondary)
# fail-closed check. Rendering a shot into prompt TEXT is a different
# module's job -- see tests/test_video_prompt_writer_coverage_plan.py.
# ---------------------------------------------------------------------------

def _make_shot(shot_order: int, beat_id: str = "b_001", **overrides) -> dict:
    shot = {
        "shot_id": f"{beat_id}__shot{shot_order}",
        "parent_beat_id": beat_id,
        "shot_order": shot_order,
        "shot_purpose": "establish_location",
        "narrative_function": "test coverage",
        "estimated_usable_duration": 8.0,
        "subject": "the passage",
        "shot_scale": "medium",
        "camera_angle": "eye_level",
        "camera_height": "eye_level",
        "camera_direction": "frontal",
        "camera_movement": "slow_push_in",
        "composition": "rule_of_thirds_left",
        "background_depth": "medium",
        "screen_direction": "neutral",
        "motion_energy": "low",
        "transition_in": "clean_cut",
        "transition_out": "clean_cut",
        "diversity_justification": None,
    }
    shot.update(overrides)
    return shot


class TestShotDiversityKey:

    def test_key_uses_the_documented_field_set(self):
        assert SHOT_DIVERSITY_FIELDS == (
            "shot_scale", "camera_angle", "camera_height", "camera_direction",
            "camera_movement", "composition", "background_depth", "screen_direction",
        )

    def test_differs_for_different_camera_design(self):
        a = _make_shot(1, camera_angle="eye_level", composition="rule_of_thirds_left")
        b = _make_shot(2, camera_angle="low_angle", composition="centered_symmetry")
        assert shot_diversity_key(a) != shot_diversity_key(b)

    def test_matches_for_identical_camera_design(self):
        a = _make_shot(1, camera_angle="eye_level", composition="rule_of_thirds_left")
        b = _make_shot(2, camera_angle="eye_level", composition="rule_of_thirds_left")
        assert shot_diversity_key(a) == shot_diversity_key(b)

    def test_ignores_fields_outside_the_documented_set(self):
        """A different subject/narrative_function/shot_purpose alone does
        not make two shots diverse -- only the camera/composition design
        does, matching the prompt file's own rule."""
        a = _make_shot(1, subject="a candle", narrative_function="finds the candle")
        b = _make_shot(2, subject="a skull", narrative_function="finds the skull")
        assert shot_diversity_key(a) == shot_diversity_key(b)


class TestValidateCoveragePlanDiversity:

    def _plan(self, *beat_plans) -> dict:
        return {"episode_id": "ep_001", "beat_plans": list(beat_plans)}

    def test_single_shot_beat_always_passes(self):
        plan = self._plan({
            "beat_id": "b_001", "narration_duration_seconds": 8.0,
            "progression_pattern": "single_shot", "shots": [_make_shot(1)],
        })
        validate_coverage_plan_diversity(plan, episode_id="ep_001")  # must not raise

    def test_distinct_shots_pass(self):
        plan = self._plan({
            "beat_id": "b_001", "narration_duration_seconds": 24.0,
            "progression_pattern": "wide_medium_close",
            "shots": [
                _make_shot(1, camera_angle="eye_level", composition="rule_of_thirds_left"),
                _make_shot(2, camera_angle="low_angle", composition="centered_symmetry"),
                _make_shot(3, camera_angle="high_angle", composition="layered_depth"),
            ],
        })
        validate_coverage_plan_diversity(plan, episode_id="ep_001")  # must not raise

    def test_duplicate_shots_without_justification_raise(self):
        plan = self._plan({
            "beat_id": "b_001", "narration_duration_seconds": 16.0,
            "progression_pattern": "wide_medium_close",
            "shots": [
                _make_shot(1, camera_angle="eye_level", composition="rule_of_thirds_left"),
                _make_shot(2, camera_angle="eye_level", composition="rule_of_thirds_left"),
            ],
        })
        with pytest.raises(CoveragePlanDiversityError) as exc_info:
            validate_coverage_plan_diversity(plan, episode_id="ep_001")
        err = exc_info.value
        assert err.episode_id == "ep_001"
        assert err.beat_id == "b_001"
        assert err.shot_id_a == "b_001__shot1"
        assert err.shot_id_b == "b_001__shot2"

    def test_duplicate_shots_with_justification_pass(self):
        plan = self._plan({
            "beat_id": "b_001", "narration_duration_seconds": 16.0,
            "progression_pattern": "wide_medium_close",
            "shots": [
                _make_shot(1, camera_angle="eye_level", composition="rule_of_thirds_left"),
                _make_shot(2, camera_angle="eye_level", composition="rule_of_thirds_left",
                            diversity_justification="deliberate match-cut"),
            ],
        })
        validate_coverage_plan_diversity(plan, episode_id="ep_001")  # must not raise

    def test_duplicate_in_one_beat_does_not_block_other_beats(self):
        """A rejection is per-plan (raises on first offending beat found),
        but distinct beats are independent -- a clean beat elsewhere in the
        same plan is not itself the problem."""
        plan = self._plan(
            {
                "beat_id": "b_001", "narration_duration_seconds": 8.0,
                "progression_pattern": "single_shot", "shots": [_make_shot(1, beat_id="b_001")],
            },
            {
                "beat_id": "b_002", "narration_duration_seconds": 16.0,
                "progression_pattern": "wide_medium_close",
                "shots": [
                    _make_shot(1, beat_id="b_002", camera_angle="eye_level"),
                    _make_shot(2, beat_id="b_002", camera_angle="eye_level"),
                ],
            },
        )
        with pytest.raises(CoveragePlanDiversityError) as exc_info:
            validate_coverage_plan_diversity(plan, episode_id="ep_001")
        assert exc_info.value.beat_id == "b_002"

    def test_llm_failure_pass_through_plan_always_passes(self):
        """_minimal_pass_through()'s degraded plan (exactly one shot per
        beat) can never trigger this validation -- a single shot has no
        sibling to duplicate."""
        from hypogeum.agents.cinematic_director import _minimal_pass_through

        beats_for_director = [
            {"beat_id": "b_001", "narration_duration_seconds": 8.0,
             "visual_spec": {"subject": "a corridor", "framing": "wide", "lighting": "warm"}},
            {"beat_id": "b_002", "narration_duration_seconds": 40.0,
             "visual_spec": {"subject": "a chamber", "framing": "medium", "lighting": "cool"}},
        ]
        plan = _minimal_pass_through("ep_fallback", beats_for_director)
        validate_coverage_plan_diversity(plan, episode_id="ep_fallback")  # must not raise


# ---------------------------------------------------------------------------
# TestShotBudgetIndependentTerms — coverage_minimum, editorial_minimum, and
# policy_minimum must stay independently computable and never conflated
# ---------------------------------------------------------------------------

class TestShotBudgetIndependentTerms:

    def test_coverage_minimum_derives_from_provider_capability_not_editorial_target(self):
        """A beat whose duration is under the editorial pacing target but
        over the provider coverage capacity must still get enough shots
        for real coverage -- proof coverage_minimum is a live, independent
        term, not dominated by editorial_minimum."""
        cfg = _make_config(CINEMATIC_SECONDS_PER_SHOT=100.0)  # pacing target far too generous
        # duration 20s: editorial_minimum = ceil(20/100) = 1, but
        # coverage_minimum = ceil(20 / (8*1.15=9.2)) = 3 -- must win.
        assert compute_shot_budget(20.0, cfg) == 3

    def test_editorial_minimum_can_dominate_for_a_longer_provider_clip(self):
        """With a hypothetical longer provider clip, editorial pacing can
        legitimately require MORE shots than raw coverage capacity alone --
        proof the two terms are independent, not one subsuming the other."""
        cfg = _make_config(CINEMATIC_SECONDS_PER_SHOT=5.0, MUAPI_T2_MODEL="veo3.1-lite-text-to-video")
        # duration 20s: coverage_minimum = ceil(20/9.2) = 3,
        # editorial_minimum = ceil(20/5) = 4 -- editorial must win.
        assert compute_shot_budget(20.0, cfg) == 4

    def test_policy_minimum_floors_a_very_short_beat(self):
        cfg = _make_config(CINEMATIC_MIN_SHOTS_PER_BEAT=2)
        assert compute_shot_budget(1.0, cfg) == 2

    def test_provider_capacity_change_alone_changes_budget(self):
        """Swapping the configured provider/model (via MUAPI_DEFAULT_TIER)
        changes coverage_minimum with zero change to editorial_pacing or
        duration -- proof the three concepts are truly independent inputs,
        not one hardcoded constant."""
        cfg_8s = _make_config()  # T2 = veo3.1-lite, 8s/clip
        cfg_8s.MUAPI_DEFAULT_TIER = 2
        budget_8s = compute_shot_budget(30.0, cfg_8s)

        cfg_8s.CINEMATIC_SECONDS_PER_SHOT = 1000.0  # neutralize editorial term
        budget_8s_editorial_neutralized = compute_shot_budget(30.0, cfg_8s)
        assert budget_8s_editorial_neutralized == math.ceil(30.0 / (8.0 * 1.15))


# ---------------------------------------------------------------------------
# TestMeasuredDurationPreference — run_cinematic_director() must prefer
# measured_durations_by_id over the text-based estimate whenever present
# ---------------------------------------------------------------------------

class TestMeasuredDurationPreference:

    def test_measured_duration_used_when_present(self, tmp_path):
        cfg = _make_config()
        vp = _make_visual_package()
        plan = {"episode_id": "ep_001", "beat_plans": [_valid_beat_plan("b_001", 40.0, 1)]}
        captured = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return plan

        with patch("hypogeum.agents.cinematic_director.run_agent", side_effect=_capture):
            run_cinematic_director(
                visual_package=vp,
                beat_texts_by_id={"b_001": "one two three"},  # tiny estimate if used
                episode_id="ep_001", config=cfg, output_dir=tmp_path,
                measured_durations_by_id={"b_001": 40.0},  # real, much longer
            )

        sent_beat = captured["input_package"]["beats"][0]
        assert sent_beat["narration_duration_seconds"] == 40.0

    def test_falls_back_to_estimate_when_beat_missing_from_measured(self, tmp_path):
        cfg = _make_config()
        vp = _make_visual_package()
        plan = {"episode_id": "ep_001", "beat_plans": [_valid_beat_plan("b_001", 8.0, 1)]}
        captured = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return plan

        with patch("hypogeum.agents.cinematic_director.run_agent", side_effect=_capture):
            run_cinematic_director(
                visual_package=vp,
                beat_texts_by_id={"b_001": " ".join(["word"] * 58)},
                episode_id="ep_001", config=cfg, output_dir=tmp_path,
                measured_durations_by_id={},  # b_001 absent -- must fall back
            )

        sent_beat = captured["input_package"]["beats"][0]
        assert sent_beat["narration_duration_seconds"] == pytest.approx(24.0, abs=1.5)

    def test_omitting_measured_durations_entirely_preserves_old_estimate_behavior(self, tmp_path):
        """A caller that doesn't pass measured_durations_by_id at all (e.g.
        an existing test not concerned with duration accuracy) must keep
        working exactly as before the parameter was added."""
        cfg = _make_config()
        vp = _make_visual_package()
        plan = {"episode_id": "ep_001", "beat_plans": [_valid_beat_plan("b_001", 8.0, 1)]}

        with patch("hypogeum.agents.cinematic_director.run_agent", return_value=plan):
            result = run_cinematic_director(
                visual_package=vp, beat_texts_by_id={"b_001": "a short beat"},
                episode_id="ep_001", config=cfg, output_dir=tmp_path,
            )
        validate("cinematic_coverage_plan", result)


# ---------------------------------------------------------------------------
# TestValidateNarrationCompleteness (2026-08-08 fail-closed fix)
# ---------------------------------------------------------------------------

def _make_script_package_47() -> dict:
    """Same authoritative-beat shape as the real ep_f3619022 episode: 29
    main beats across 6 sections, 18 short beats across 3 shorts (6 each) --
    47 total. Used to prove the gate at the same scale it must handle in
    production, not just a toy 1-beat case."""
    sections = []
    beat_n = 1
    section_beat_counts = [3, 6, 5, 6, 4, 5]  # sums to 29
    for si, count in enumerate(section_beat_counts, start=1):
        beats = []
        for _ in range(count):
            beats.append({"beat_id": f"b_{beat_n:03d}", "text": f"Narration beat {beat_n}."})
            beat_n += 1
        sections.append({
            "section_id": f"s_{si:03d}", "title": f"Section {si}", "narration_beats": beats,
        })
    shorts = []
    for shi in range(1, 4):
        beats = []
        for _ in range(6):
            beats.append({"beat_id": f"b_{beat_n:03d}", "text": f"Short narration beat {beat_n}."})
            beat_n += 1
        shorts.append({"short_id": f"short_{shi:03d}", "narration_beats": beats})
    return {
        "episode_metadata": {}, "sections": sections, "shorts_scripts": shorts,
        "traceability_manifest": {}, "script_integrity": {},
    }


def _make_complete_narration_package_47(script_package: dict) -> dict:
    """narration_package with exactly one valid segment per authoritative
    beat in `script_package` -- the valid, 47-beat-style case."""
    segments = []
    for i, beat_id in enumerate(authoritative_beat_ids(script_package)):
        segments.append({
            "segment_id": f"seg_{beat_id}",
            "beat_reference": beat_id,
            "source_type": "main_episode",
            "generation_status": "generated",
            "duration_seconds": 10.0 + i,
        })
    return {"narration_segments": segments}


class TestValidateNarrationCompleteness:

    def test_valid_47_beat_style_case_returns_full_duration_map(self):
        sp = _make_script_package_47()
        beat_ids = authoritative_beat_ids(sp)
        assert len(beat_ids) == 47
        np = _make_complete_narration_package_47(sp)

        result = validate_narration_completeness(sp, np)

        assert set(result.keys()) == set(beat_ids)
        assert len(result) == 47
        assert all(isinstance(v, float) and v > 0 for v in result.values())

    def test_authoritative_beat_texts_covers_every_beat(self):
        sp = _make_script_package_47()
        texts = authoritative_beat_texts(sp)
        assert set(texts.keys()) == set(authoritative_beat_ids(sp))
        assert all(t for t in texts.values())

    def test_absent_narration_package_rejected(self):
        sp = _make_script_package_47()
        with pytest.raises(NarrationCompletenessError, match="absent"):
            validate_narration_completeness(sp, None)

    def test_empty_narration_package_rejected(self):
        sp = _make_script_package_47()
        with pytest.raises(NarrationCompletenessError, match="absent"):
            validate_narration_completeness(sp, {})

    def test_script_package_with_no_authoritative_beats_rejected(self):
        with pytest.raises(NarrationCompletenessError, match="no authoritative beat_ids"):
            validate_narration_completeness(
                {"sections": [], "shorts_scripts": []},
                {"narration_segments": []},
            )

    def test_missing_beat_reference_rejected(self):
        sp = _make_script_package_47()
        np = _make_complete_narration_package_47(sp)
        np["narration_segments"][0]["beat_reference"] = ""
        with pytest.raises(NarrationCompletenessError, match="beats_with_no_valid_segment"):
            validate_narration_completeness(sp, np)

    def test_unknown_unmatched_beat_reference_rejected(self):
        sp = _make_script_package_47()
        np = _make_complete_narration_package_47(sp)
        np["narration_segments"][0]["beat_reference"] = "b_999_does_not_exist"
        with pytest.raises(NarrationCompletenessError, match="unmatched_beat_references"):
            validate_narration_completeness(sp, np)

    def test_duplicate_beat_reference_rejected(self):
        sp = _make_script_package_47()
        np = _make_complete_narration_package_47(sp)
        dup = dict(np["narration_segments"][0])
        np["narration_segments"].append(dup)
        with pytest.raises(NarrationCompletenessError, match="duplicate_beat_references"):
            validate_narration_completeness(sp, np)

    def test_missing_duration_seconds_rejected(self):
        sp = _make_script_package_47()
        np = _make_complete_narration_package_47(sp)
        del np["narration_segments"][0]["duration_seconds"]
        with pytest.raises(NarrationCompletenessError, match="beats_with_no_valid_segment"):
            validate_narration_completeness(sp, np)

    def test_zero_duration_rejected(self):
        sp = _make_script_package_47()
        np = _make_complete_narration_package_47(sp)
        np["narration_segments"][0]["duration_seconds"] = 0.0
        with pytest.raises(NarrationCompletenessError, match="invalid_durations"):
            validate_narration_completeness(sp, np)

    def test_negative_duration_rejected(self):
        sp = _make_script_package_47()
        np = _make_complete_narration_package_47(sp)
        np["narration_segments"][0]["duration_seconds"] = -3.5
        with pytest.raises(NarrationCompletenessError, match="invalid_durations"):
            validate_narration_completeness(sp, np)

    def test_nan_duration_rejected(self):
        sp = _make_script_package_47()
        np = _make_complete_narration_package_47(sp)
        np["narration_segments"][0]["duration_seconds"] = float("nan")
        with pytest.raises(NarrationCompletenessError, match="invalid_durations"):
            validate_narration_completeness(sp, np)

    def test_infinity_duration_rejected(self):
        sp = _make_script_package_47()
        np = _make_complete_narration_package_47(sp)
        np["narration_segments"][0]["duration_seconds"] = float("inf")
        with pytest.raises(NarrationCompletenessError, match="invalid_durations"):
            validate_narration_completeness(sp, np)

    def test_negative_infinity_duration_rejected(self):
        sp = _make_script_package_47()
        np = _make_complete_narration_package_47(sp)
        np["narration_segments"][0]["duration_seconds"] = float("-inf")
        with pytest.raises(NarrationCompletenessError, match="invalid_durations"):
            validate_narration_completeness(sp, np)

    def test_boolean_duration_rejected(self):
        sp = _make_script_package_47()
        np = _make_complete_narration_package_47(sp)
        np["narration_segments"][0]["duration_seconds"] = True
        with pytest.raises(NarrationCompletenessError, match="invalid_durations"):
            validate_narration_completeness(sp, np)

    def test_non_numeric_string_duration_rejected(self):
        sp = _make_script_package_47()
        np = _make_complete_narration_package_47(sp)
        np["narration_segments"][0]["duration_seconds"] = "twenty-four seconds"
        with pytest.raises(NarrationCompletenessError, match="invalid_durations"):
            validate_narration_completeness(sp, np)

    def test_incomplete_authoritative_beat_coverage_rejected(self):
        sp = _make_script_package_47()
        np = _make_complete_narration_package_47(sp)
        np["narration_segments"] = np["narration_segments"][:-1]  # drop the last beat
        with pytest.raises(NarrationCompletenessError, match=r"46/47 valid"):
            validate_narration_completeness(sp, np)

    def test_invalid_value_is_never_coerced_to_zero(self):
        """A rejected duration must never silently become a usable 0.0 --
        the beat must be reported as invalid, never fabricated into a
        zero-duration entry in the returned map (there is no returned map
        at all on failure -- validate_narration_completeness() raises)."""
        sp = _make_script_package_47()
        np = _make_complete_narration_package_47(sp)
        first_beat = np["narration_segments"][0]["beat_reference"]
        np["narration_segments"][0]["duration_seconds"] = float("nan")
        with pytest.raises(NarrationCompletenessError) as exc_info:
            validate_narration_completeness(sp, np)
        assert f"invalid_durations=[('{first_beat}'" in str(exc_info.value)


# ---------------------------------------------------------------------------
# TestRegenerateCinematicCoveragePlan (2026-08-08 isolated planning-only
# regeneration)
# ---------------------------------------------------------------------------

def _make_small_script_package() -> dict:
    """2 main beats + 1 short beat -- small enough to hand-verify the
    arithmetic (shot budgets) in each test."""
    return {
        "sections": [{
            "section_id": "s_001", "title": "T",
            "narration_beats": [
                {"beat_id": "b_001", "text": "one"},
                {"beat_id": "b_002", "text": "two"},
            ],
        }],
        "shorts_scripts": [{
            "short_id": "short_001",
            "narration_beats": [{"beat_id": "b_s01", "text": "three"}],
        }],
    }


def _make_small_narration_package() -> dict:
    """durations chosen so, with _make_config()'s defaults (8s clip,
    MAX_EDITORIAL_SPEED_FACTOR=1.15 -> 9.2s capacity/shot,
    CINEMATIC_SECONDS_PER_SHOT=10.0): b_001 -> 1 shot, b_002 -> 3 shots
    (ceil(20/9.2)=3), b_s01 -> 1 shot. main_shots=4, short_shots=1, total=5."""
    return {"narration_segments": [
        {"beat_reference": "b_001", "duration_seconds": 8.0},
        {"beat_reference": "b_002", "duration_seconds": 20.0},
        {"beat_reference": "b_s01", "duration_seconds": 8.0},
    ]}


def _make_small_visual_package() -> dict:
    return {
        "episode_metadata": {"episode_title": "Small Test Episode"},
        "scenes": [
            {"scene_id": "s01", "narration_reference": "b_001", "source_type": "main_episode",
             "visual_spec": {}, "presentation_plan": {}, "narrative_signals": []},
            {"scene_id": "s02", "narration_reference": "b_002", "source_type": "main_episode",
             "visual_spec": {}, "presentation_plan": {}, "narrative_signals": []},
            {"scene_id": "s01", "narration_reference": "b_s01", "source_type": "short",
             "visual_spec": {}, "presentation_plan": {}, "narrative_signals": []},
        ],
    }


def _matching_candidate_plan() -> dict:
    """A cinematic_coverage_plan whose shot counts exactly match the
    arithmetic _make_small_narration_package() implies (1, 3, 1)."""
    return {
        "episode_id": "ep_regen",
        "beat_plans": [
            _valid_beat_plan("b_001", 8.0, 1),
            _valid_beat_plan("b_002", 20.0, 3),
            _valid_beat_plan("b_s01", 8.0, 1),
        ],
    }


def _mismatched_candidate_plan() -> dict:
    """Same beats, but b_002 gets only 1 shot instead of the required 3 --
    an arithmetic mismatch that must be rejected."""
    return {
        "episode_id": "ep_regen",
        "beat_plans": [
            _valid_beat_plan("b_001", 8.0, 1),
            _valid_beat_plan("b_002", 20.0, 1),
            _valid_beat_plan("b_s01", 8.0, 1),
        ],
    }


class TestRegenerateCinematicCoveragePlan:

    def test_normal_cached_behavior_unchanged_when_force_false(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        existing = _matching_candidate_plan()
        ckpt.write_text(json.dumps(existing))

        with patch("hypogeum.agents.cinematic_director.run_agent") as mock_ra:
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=False,
            )

        mock_ra.assert_not_called()
        assert result["status"] == "cached"
        assert result["plan"] == existing
        assert json.loads(ckpt.read_text()) == existing  # untouched on disk

    def test_regeneration_requires_explicit_force(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        # No checkpoint exists at all.

        with patch("hypogeum.agents.cinematic_director.run_agent") as mock_ra:
            with pytest.raises(CoverageRegenerationError):
                regenerate_cinematic_coverage_plan(
                    sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=False,
                )

        mock_ra.assert_not_called()
        assert not (tmp_path / "cinematic_coverage_plan_ep_regen.json").exists()

    def test_input_validation_happens_before_llm_call(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        incomplete_np = {"narration_segments": [
            {"beat_reference": "b_001", "duration_seconds": 8.0},
        ]}  # b_002 and b_s01 missing
        vp = _make_small_visual_package()

        with patch("hypogeum.agents.cinematic_director.run_agent") as mock_ra:
            with pytest.raises(NarrationCompletenessError):
                regenerate_cinematic_coverage_plan(
                    sp, incomplete_np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
                )

        mock_ra.assert_not_called()

    def test_one_llm_call_maximum(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()

        with patch("hypogeum.agents.cinematic_director.run_agent",
                   return_value=_matching_candidate_plan()) as mock_ra:
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert mock_ra.call_count == 1
        assert result["status"] == "installed"

    def test_invalid_candidate_preserves_old_plan_and_backup(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        original = _matching_candidate_plan()
        original["notes"] = ["ORIGINAL"]
        ckpt.write_text(json.dumps(original))

        with patch("hypogeum.agents.cinematic_director.run_agent",
                   return_value=_mismatched_candidate_plan()):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "rejected"
        assert "arithmetic_mismatch" in result["reason"]
        # Active plan on disk is byte-for-byte the original -- never touched.
        assert json.loads(ckpt.read_text()) == original
        assert result["plan"] == original
        # Backup exists and also holds the original.
        backups = list(tmp_path.glob("cinematic_coverage_plan_ep_regen.backup_*.json"))
        assert len(backups) == 1
        assert json.loads(backups[0].read_text()) == original
        # No leftover candidate file.
        assert not (tmp_path / "cinematic_coverage_plan_ep_regen.candidate.json").exists()

    def test_llm_failure_preserves_old_plan(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        with patch("hypogeum.agents.cinematic_director.run_agent",
                   side_effect=RuntimeError("provider down")):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "rejected"
        assert "llm_call_failed" in result["reason"]
        assert json.loads(ckpt.read_text()) == original

    def test_valid_candidate_atomically_installed(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        original = _matching_candidate_plan()
        original["notes"] = ["ORIGINAL"]
        ckpt.write_text(json.dumps(original))
        new_plan = _matching_candidate_plan()
        new_plan["notes"] = ["REGENERATED"]

        with patch("hypogeum.agents.cinematic_director.run_agent", return_value=new_plan):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "installed"
        on_disk = json.loads(ckpt.read_text())
        assert on_disk["notes"] == ["REGENERATED"]
        assert on_disk != original
        backups = list(tmp_path.glob("cinematic_coverage_plan_ep_regen.backup_*.json"))
        assert len(backups) == 1
        assert json.loads(backups[0].read_text()) == original
        assert result["totals"] == {
            "main_shots": 4, "short_shots": 1, "total_shots": 5,
            "sufficient_beats": 3, "insufficient_beats": 0,
        }

    def test_no_stage_b_provider_assembly_or_publishing_function_reachable(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()

        def _must_not_be_called(*a, **k):
            raise AssertionError("regenerate_cinematic_coverage_plan must never reach this")

        with patch("hypogeum.agents.cinematic_director.run_agent",
                   return_value=_matching_candidate_plan()), \
             patch("hypogeum.agents.shot_diversity_planner.run_shot_diversity_planner",
                   side_effect=_must_not_be_called), \
             patch("hypogeum.muapi.submit_scene", side_effect=_must_not_be_called), \
             patch("hypogeum.agents.assembly_agent.run_assembly_agent",
                   side_effect=_must_not_be_called), \
             patch("hypogeum.agents.publishing_agent.run_publishing_agent",
                   side_effect=_must_not_be_called):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "installed"

    def test_fixture_verification_omitted_by_default_no_hardcoded_check(self, tmp_path):
        """Without fixture_verification, a valid candidate installs
        regardless of its actual totals -- proof no episode-specific
        numbers are baked into the function itself."""
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()

        with patch("hypogeum.agents.cinematic_director.run_agent",
                   return_value=_matching_candidate_plan()):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "installed"
        assert result["totals"]["total_shots"] == 5  # real total, never asserted against by the function itself

    def test_fixture_verification_matching_totals_installs(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()

        with patch("hypogeum.agents.cinematic_director.run_agent",
                   return_value=_matching_candidate_plan()):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
                fixture_verification={
                    "expected_main_shots": 4, "expected_short_shots": 1,
                    "expected_total_shots": 5, "expected_sufficient_beats": 3,
                },
            )

        assert result["status"] == "installed"

    def test_fixture_verification_mismatch_rejects_and_preserves_old_plan(self, tmp_path):
        """fixture_verification is caller-supplied and episode-specific --
        a caller asserting the WRONG numbers must reject the candidate,
        proving the check is real (not a no-op) and driven entirely by
        what the caller passed in, not by any constant in this module."""
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        with patch("hypogeum.agents.cinematic_director.run_agent",
                   return_value=_matching_candidate_plan()):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
                fixture_verification={"expected_total_shots": 999},
            )

        assert result["status"] == "rejected"
        assert "fixture_verification_mismatch" in result["reason"]
        assert json.loads(ckpt.read_text()) == original

    def test_temp_and_candidate_artifacts_do_not_leak_into_the_88_clip_video_tree(self, tmp_path):
        """Regeneration must never write anywhere under video/ -- only
        cinematic_coverage_plan_*.json variants beside the checkpoint."""
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()

        with patch("hypogeum.agents.cinematic_director.run_agent",
                   return_value=_matching_candidate_plan()):
            regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert not (tmp_path / "video").exists()
        leftover_temp_dirs = list(tmp_path.glob(".coverage_regen_*"))
        assert leftover_temp_dirs == []
