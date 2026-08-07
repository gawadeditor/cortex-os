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
    compute_shot_budget,
    estimate_beat_duration_seconds,
    run_cinematic_director,
    shot_diversity_key,
    validate_coverage_plan_diversity,
)
from hypogeum.schemas import validate


def _make_config(**overrides) -> SimpleNamespace:
    base = dict(
        CINEMATIC_SECONDS_PER_SHOT=10.0,
        CINEMATIC_MAX_SHOTS_PER_BEAT=6,
        CINEMATIC_ESTIMATED_WORDS_PER_MINUTE=145.0,
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
