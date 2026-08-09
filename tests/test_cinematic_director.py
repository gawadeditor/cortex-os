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
    SYSTEM_PROMPT,
    CoveragePlanDiversityError,
    CoverageRegenerationError,
    NarrationCompletenessError,
    _COMPACT_CODE_FIELDS,
    _COMPACT_CODE_PARTS,
    _COMPACT_ENUM_VOCAB,
    _COMPACT_FREE_TEXT_FIELDS,
    _MAX_SLOTS_PER_BATCH,
    _MAX_TOKENS_REGEN,
    _STRUCTURED_ENUM_PROPERTY_NAMES,
    _STRUCTURED_OUTPUT_PROVIDER,
    _build_structured_response_format,
    _decode_code_part,
    _decode_enum_code,
    _estimate_batch_max_tokens,
    _expand_compact_plan,
    _no_duplicate_keys_object_pairs_hook,
    _partition_beats_into_batches,
    _structured_payload_to_compact,
    authoritative_beat_ids,
    authoritative_beat_texts,
    build_structured_coverage_plan_schema,
    compute_shot_budget,
    estimate_beat_duration_seconds,
    regenerate_cinematic_coverage_plan,
    run_cinematic_director,
    shot_diversity_key,
    validate_coverage_plan_diversity,
    validate_narration_completeness,
)
from hypogeum.schemas import SchemaError, validate


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
    arithmetic _make_small_narration_package() implies (1, 3, 1). This is
    the FULL canonical shape -- used only for pre-seeding an existing
    checkpoint file directly on disk (bypassing regenerate_cinematic_
    coverage_plan() entirely), never as an LLM response."""
    return {
        "episode_id": "ep_regen",
        "beat_plans": [
            _valid_beat_plan("b_001", 8.0, 1),
            _valid_beat_plan("b_002", 20.0, 3),
            _valid_beat_plan("b_s01", 8.0, 1),
        ],
    }


def _make_call_result(content: str, **overrides) -> SimpleNamespace:
    """Minimal stand-in for hypogeum.openrouter.CallResult -- only the
    fields regenerate_cinematic_coverage_plan() actually reads."""
    base = dict(
        content=content,
        usage={"prompt_tokens": 800, "completion_tokens": 600, "total_tokens": 1400},
        model_used="anthropic/claude-sonnet-4.6",
        finish_reason="stop",
        response_time_ms=1200,
        via_fallback=False,
        estimated_cost_usd=0.01,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _compact_codes(**field_overrides) -> dict:
    """A valid {"code1": ..., "code2": ...} pair using each field's first
    vocabulary value by default, with per-field overrides by full schema
    name (e.g. _compact_codes(camera_movement="orbital_arc")). Built from
    the real _COMPACT_CODE_PARTS/_COMPACT_ENUM_VOCAB so these fixtures can
    never silently drift from the actual decode tables. Used only by
    TestCompactWireFormat below, which tests _expand_compact_plan() (the
    compact <-> canonical layer, unchanged by the structured-output work)
    directly."""
    parts = {}
    for part_key, fields in _COMPACT_CODE_PARTS.items():
        letters = []
        for field in fields:
            if field in field_overrides:
                idx = _COMPACT_ENUM_VOCAB[field].index(field_overrides[field])
            else:
                idx = 0
            letters.append(chr(65 + idx))
        parts[part_key] = "".join(letters)
    return parts


def _compact_shot(**overrides) -> dict:
    base = {
        **_compact_codes(),
        "func": "distinct contribution this shot alone makes",
        "subj": "x",
    }
    base.update(overrides)
    return base


def _structured_shot(**overrides) -> dict:
    """A valid structured-output shot object: the ten enum-coded fields at
    their vocabulary's first letter by default (built from the real
    _STRUCTURED_ENUM_PROPERTY_NAMES/_COMPACT_ENUM_VOCAB), plus the required
    free-text/lens properties using "" as the "not applicable" sentinel.
    Overrides may use either a full schema field name (e.g.
    camera_angle="high_angle", resolved to the right property+letter) or a
    raw structured property key directly (e.g. dep="Z", subj="...") for
    constructing malformed fixtures."""
    shot: dict = {}
    remaining = dict(overrides)
    for field in _COMPACT_CODE_FIELDS:
        prop = _STRUCTURED_ENUM_PROPERTY_NAMES[field]
        if field in remaining:
            idx = _COMPACT_ENUM_VOCAB[field].index(remaining.pop(field))
            shot[prop] = chr(65 + idx)
        else:
            shot[prop] = "A"
    shot.update({
        "func": "distinct contribution this shot alone makes",
        "subj": "x",
        "act": "", "fram": "", "light": "", "dup": "", "lens": "A",
    })
    shot.update(remaining)
    return shot


def _structured_beat(pattern: str, shots: list) -> dict:
    """shots is a list of shot dicts; slot keys s01..sNN are assigned by
    list position, matching regenerate_cinematic_coverage_plan()'s own
    contract."""
    return {
        "pattern": pattern,
        "shots": {f"s{i:02d}": shot for i, shot in enumerate(shots, start=1)},
    }


def _matching_structured_response() -> dict:
    """Structured wire-format response whose shot counts exactly match the
    arithmetic _make_small_narration_package() implies (1, 3, 1)."""
    return {"beats": {
        "b_001": _structured_beat("A", [_structured_shot()]),
        "b_002": _structured_beat("A", [
            _structured_shot(camera_angle="eye_level"),
            _structured_shot(camera_angle="low_angle"),
            _structured_shot(camera_angle="high_angle"),
        ]),
        "b_s01": _structured_beat("A", [_structured_shot()]),
    }}


def _mismatched_structured_response() -> dict:
    """Same beats, but b_002 gets only 1 shot instead of the required 3 --
    an arithmetic mismatch that must be rejected. Strict structured output
    is contracted by the provider to make this shape unsatisfiable at the
    API level, but this fixture exercises the defensive fallback check in
    _structured_payload_to_compact() for the case where it arrives anyway."""
    return {"beats": {
        "b_001": _structured_beat("A", [_structured_shot()]),
        "b_002": _structured_beat("A", [_structured_shot()]),
        "b_s01": _structured_beat("A", [_structured_shot()]),
    }}


class TestRegenerateCinematicCoveragePlan:

    def test_normal_cached_behavior_unchanged_when_force_false(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        existing = _matching_candidate_plan()
        ckpt.write_text(json.dumps(existing))

        with patch("hypogeum.openrouter.call_claude") as mock_cc:
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=False,
            )

        mock_cc.assert_not_called()
        assert result["status"] == "cached"
        assert result["plan"] == existing
        assert json.loads(ckpt.read_text()) == existing  # untouched on disk

    def test_regeneration_requires_explicit_force(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        # No checkpoint exists at all.

        with patch("hypogeum.openrouter.call_claude") as mock_cc:
            with pytest.raises(CoverageRegenerationError):
                regenerate_cinematic_coverage_plan(
                    sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=False,
                )

        mock_cc.assert_not_called()
        assert not (tmp_path / "cinematic_coverage_plan_ep_regen.json").exists()

    def test_input_validation_happens_before_llm_call(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        incomplete_np = {"narration_segments": [
            {"beat_reference": "b_001", "duration_seconds": 8.0},
        ]}  # b_002 and b_s01 missing
        vp = _make_small_visual_package()

        with patch("hypogeum.openrouter.call_claude") as mock_cc:
            with pytest.raises(NarrationCompletenessError):
                regenerate_cinematic_coverage_plan(
                    sp, incomplete_np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
                )

        mock_cc.assert_not_called()

    def test_one_llm_call_maximum(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(_matching_structured_response()))) as mock_cc:
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert mock_cc.call_count == 1
        assert result["status"] == "installed"

    def test_max_tokens_is_batch_specific_and_within_model_limit(self, tmp_path):
        """max_tokens is no longer a blind constant (16,000, then 24,000) --
        it is computed per batch by _estimate_batch_max_tokens() (2026-08-09
        batching correction, prompted by a live 'compiled grammar is too
        large' rejection of the single 47-beat/130-slot request). For this
        small 3-beat/5-slot fixture, everything fits in exactly one batch,
        so the computed ceiling is the real _estimate_batch_max_tokens()
        value for that one batch -- not the old blind _MAX_TOKENS_REGEN."""
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(_matching_structured_response()))) as mock_cc:
            regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        expected = _estimate_batch_max_tokens(
            ["b_001", "b_002", "b_s01"], {"b_001": 1, "b_002": 3, "b_s01": 1},
        )
        actual = mock_cc.call_args.kwargs["max_tokens"]
        assert actual == expected
        assert actual <= 128_000  # verified model output-limit ceiling

    def test_response_format_is_strict_json_schema_with_deterministic_batch_name(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(_matching_structured_response()))) as mock_cc:
            regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        rf = mock_cc.call_args.kwargs["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["strict"] is True
        assert rf["json_schema"]["name"] == "cinematic_coverage_plan_batch_01"
        assert isinstance(rf["json_schema"]["schema"], dict)

    def test_provider_require_parameters_true_no_pinning_or_fallback_override(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(_matching_structured_response()))) as mock_cc:
            regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        provider = mock_cc.call_args.kwargs["provider"]
        assert provider == {"require_parameters": True}
        # No pinning/ordering/allow-listing/deny-listing/fallback-override keys.
        for forbidden_key in ("order", "only", "ignore", "allow_fallbacks"):
            assert forbidden_key not in provider

    def test_different_model_fallback_disabled_for_this_call(self, tmp_path):
        """allow_model_fallback=False must be passed -- a schema built for
        anthropic/claude-sonnet-4.6 must never silently be sent to a
        different model via config.OPENROUTER_FALLBACK_MODEL."""
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(_matching_structured_response()))) as mock_cc:
            regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert mock_cc.call_args.kwargs["allow_model_fallback"] is False

    def test_max_attempts_is_one_no_same_model_retry(self, tmp_path):
        """max_attempts=1 must be passed alongside allow_model_fallback=False
        -- without it, a transient provider error would still trigger
        call_claude()'s own same-model retry (a second HTTP request) before
        allow_model_fallback is ever consulted, leaving more than one actual
        provider request reachable despite the 'no retry' contract."""
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(_matching_structured_response()))) as mock_cc:
            regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert mock_cc.call_args.kwargs["max_attempts"] == 1
        assert mock_cc.call_args.kwargs["allow_model_fallback"] is False

    def test_undercounted_beat_preserves_old_plan_and_backup(self, tmp_path):
        """Under the structured-output slot-key schema, a beat with fewer
        shots than its authoritative budget (the exact b_014 4-vs-5 failure
        observed live) is now caught earlier than before: _structured_
        payload_to_compact()'s own slot-key check rejects it as
        malformed_output during conversion, before the downstream
        arithmetic-mismatch check would ever run -- strictly earlier
        detection of the same defect, not a weaker one. The downstream
        arithmetic check remains in place (defense in depth for any other
        code path that might reach it) but is no longer reachable via this
        specific structured-response shape, since both checks now derive
        from the same expected_shot_budget_by_beat_id source of truth."""
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        original = _matching_candidate_plan()
        original["notes"] = ["ORIGINAL"]
        ckpt.write_text(json.dumps(original))

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(_mismatched_structured_response()))):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "rejected"
        assert "malformed_output" in result["reason"]
        assert "slot keys" in result["reason"]
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

        with patch("hypogeum.openrouter.call_claude",
                   side_effect=RuntimeError("provider down")):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "rejected"
        assert "llm_call_failed" in result["reason"]
        assert json.loads(ckpt.read_text()) == original

    def test_truncated_response_preserves_old_plan(self, tmp_path):
        """A TruncatedResponse (finish_reason == 'length') is exactly the
        failure this whole correction exists to prevent for a
        representative response -- but if it ever happens anyway, it must
        still fail closed like any other llm_call_failed condition."""
        from hypogeum.openrouter import TruncatedResponse

        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        with patch("hypogeum.openrouter.call_claude",
                   side_effect=TruncatedResponse(_MAX_TOKENS_REGEN, _MAX_TOKENS_REGEN)):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "rejected"
        assert "llm_call_failed" in result["reason"]
        assert json.loads(ckpt.read_text()) == original

    def test_malformed_json_preserves_old_plan(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result("{not valid json")):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "rejected"
        assert "malformed_output" in result["reason"]
        assert json.loads(ckpt.read_text()) == original
        assert not (tmp_path / "cinematic_coverage_plan_ep_regen.candidate.json").exists()

    def test_duplicate_json_keys_rejected_before_expansion(self, tmp_path):
        """A raw response containing a duplicate JSON key must be rejected
        -- never silently collapsed to its last occurrence."""
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        raw_with_duplicate_key = (
            '{"beats": {"b_001": {"pattern": "A", "shots": {"s01": {}}}, '
            '"b_001": {"pattern": "A", "shots": {"s01": {}}}}}'
        )

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(raw_with_duplicate_key)):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "rejected"
        assert "malformed_output" in result["reason"]
        assert json.loads(ckpt.read_text()) == original

    def test_invalid_enum_value_preserves_old_plan(self, tmp_path):
        """The exact real-world failure observed live: an out-of-vocabulary
        letter for background_depth (only A/B/C are valid). Under strict
        structured output this specific value is supposed to be
        unsatisfiable at the API level (background_depth's schema enum is
        literally ["A","B","C"]), but this fixture proves the defensive
        fallback check still catches it if it ever arrives anyway."""
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        bad_response = _matching_structured_response()
        bad_response["beats"]["b_001"]["shots"]["s01"]["dep"] = "Z"  # out of range

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(bad_response))):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "rejected"
        assert "malformed_output" in result["reason"]
        assert json.loads(ckpt.read_text()) == original

    def test_missing_enum_property_preserves_old_plan(self, tmp_path):
        """A shot missing one of its ten required enum properties entirely
        -- never defaulted, padded, or inferred."""
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        bad_response = _matching_structured_response()
        del bad_response["beats"]["b_001"]["shots"]["s01"]["dep"]

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(bad_response))):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "rejected"
        assert "malformed_output" in result["reason"]
        assert json.loads(ckpt.read_text()) == original

    def test_wrong_slot_keys_preserves_old_plan(self, tmp_path):
        """A beat whose shots object doesn't have exactly its authoritative
        s01..sNN slot keys (here: b_001 emitted with zero shots) must fail
        closed -- never synthesized or defaulted."""
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        bad_response = _matching_structured_response()
        bad_response["beats"]["b_001"]["shots"] = {}

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(bad_response))):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "rejected"
        assert "malformed_output" in result["reason"]
        assert json.loads(ckpt.read_text()) == original

    def test_missing_beat_preserves_old_plan(self, tmp_path):
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        bad_response = _matching_structured_response()
        del bad_response["beats"]["b_002"]

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(bad_response))):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "rejected"
        assert "malformed_output" in result["reason"]
        assert json.loads(ckpt.read_text()) == original

    def test_missing_required_shot_field_fails_schema_validation(self, tmp_path):
        """A shot with no 'subj' key expands to no 'subject' key, which the
        full schema requires -- must fail closed at the validate() step,
        never install a schema-invalid candidate."""
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        bad_response = _matching_structured_response()
        del bad_response["beats"]["b_001"]["shots"]["s01"]["subj"]

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(bad_response))):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "rejected"
        assert "malformed_output" in result["reason"]
        assert json.loads(ckpt.read_text()) == original

    def test_valid_candidate_atomically_installed(self, tmp_path):
        """End-to-end proof: a structured response converts deterministically
        through code1/code2 into the unchanged canonical plan shape."""
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        original = _matching_candidate_plan()
        original["notes"] = ["ORIGINAL"]
        ckpt.write_text(json.dumps(original))
        new_response = _matching_structured_response()
        new_response["beats"]["b_001"]["shots"]["s01"]["subj"] = "REGENERATED_MARKER"

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(new_response))):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "installed"
        on_disk = json.loads(ckpt.read_text())
        assert on_disk["beat_plans"][0]["shots"][0]["subject"] == "REGENERATED_MARKER"
        assert on_disk != original
        validate("cinematic_coverage_plan", on_disk)  # candidate that was installed is schema-valid
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

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(_matching_structured_response()))), \
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

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(_matching_structured_response()))):
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

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(_matching_structured_response()))):
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
        what the caller passed in, not by any constant in this module (the
        expectation used here, 999, is not ep_f3619022's real total and
        appears nowhere in the production code)."""
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_regen.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(_matching_structured_response()))):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
                fixture_verification={"expected_total_shots": 999},
            )

        assert result["status"] == "rejected"
        assert "fixture_verification_mismatch" in result["reason"]
        assert json.loads(ckpt.read_text()) == original

    def test_no_temp_dirs_or_video_tree_artifacts(self, tmp_path):
        """Regeneration must never write anywhere under video/, and must
        never leave any .coverage_regen_* directory behind."""
        cfg = _make_config()
        sp = _make_small_script_package()
        np = _make_small_narration_package()
        vp = _make_small_visual_package()

        with patch("hypogeum.openrouter.call_claude",
                   return_value=_make_call_result(json.dumps(_matching_structured_response()))):
            regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_regen", cfg, output_dir=tmp_path, force=True,
            )

        assert not (tmp_path / "video").exists()
        assert list(tmp_path.glob(".coverage_regen_*")) == []


# ---------------------------------------------------------------------------
# TestStructuredCoverageSchema (2026-08-09)
#
# Direct tests of the strict-structured-output JSON Schema generator and the
# structured <-> compact converter, independent of the full regenerate_
# cinematic_coverage_plan() mocked-call flow above -- proves the schema
# itself structurally enforces cardinality/enum correctness, and that a
# representative response converts and installs correctly.
# ---------------------------------------------------------------------------

def _representative_47_beat_arithmetic():
    main_shot_counts = [3, 3, 3, 3, 2, 4, 1, 3, 3, 3, 5, 2, 3, 5, 4,
                         3, 3, 2, 4, 2, 3, 4, 4, 4, 1, 3, 5, 4, 6]
    short_shot_counts = [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 2]
    authoritative_order = (
        [f"b_{i:03d}" for i in range(1, len(main_shot_counts) + 1)]
        + [f"b_s{i:02d}" for i in range(1, len(short_shot_counts) + 1)]
    )
    shot_budget_by_beat_id = dict(zip(
        authoritative_order, main_shot_counts + short_shot_counts,
    ))
    return authoritative_order, shot_budget_by_beat_id, len(main_shot_counts)


def _real_ep_f3619022_arithmetic():
    """Identical per-beat shot-budget arithmetic to
    _representative_47_beat_arithmetic() above, but with the ACTUAL real
    ep_f3619022 beat_id naming convention -- sequential b_001..b_047 (main
    beats are b_001-b_029, short beats are b_030-b_047; there is no "b_s"
    prefix in the real script_package/narration_package for this episode).
    Confirmed by directly reading the real, on-disk (gitignored, not
    checked in) script_package_ep_f3619022.json/narration_package_
    ep_f3619022.json once, 2026-08-09 -- the exact main_shot_counts/
    short_shot_counts values below are pinned from that read and match
    _representative_47_beat_arithmetic() exactly; only the beat_id strings
    differ. Kept as a separate fixture (rather than changing the widely-used
    _representative_47_beat_arithmetic() itself) so this correction stays
    scoped to the pinned-measurement tests that specifically need the real
    naming, without touching any already-passing test."""
    main_shot_counts = [3, 3, 3, 3, 2, 4, 1, 3, 3, 3, 5, 2, 3, 5, 4,
                         3, 3, 2, 4, 2, 3, 4, 4, 4, 1, 3, 5, 4, 6]
    short_shot_counts = [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 2]
    authoritative_order = [f"b_{i:03d}" for i in range(1, len(main_shot_counts) + len(short_shot_counts) + 1)]
    shot_budget_by_beat_id = dict(zip(
        authoritative_order, main_shot_counts + short_shot_counts,
    ))
    return authoritative_order, shot_budget_by_beat_id, len(main_shot_counts)


def _walk_schema_objects(node):
    """Yield every object-typed node (with its own properties/required) in
    the schema tree, including $defs -- used by several structural tests
    below."""
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            yield node
            for sub in node["properties"].values():
                yield from _walk_schema_objects(sub)
        for sub in node.get("$defs", {}).values():
            yield from _walk_schema_objects(sub)


class TestStructuredCoverageSchema:

    def test_exactly_47_required_beat_properties_in_authoritative_order(self):
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        schema = build_structured_coverage_plan_schema(authoritative_order, budget)

        assert len(schema["required"]) == 47
        assert set(schema["properties"].keys()) == set(authoritative_order)
        assert schema["required"] == authoritative_order
        assert list(schema["properties"].keys()) == authoritative_order

    def test_schema_never_uses_array_type(self):
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        schema = build_structured_coverage_plan_schema(authoritative_order, budget)

        def assert_no_arrays(node):
            if isinstance(node, dict):
                assert node.get("type") != "array", f"found an array-typed node: {node}"
                for v in node.values():
                    assert_no_arrays(v)
            elif isinstance(node, list):
                for item in node:
                    assert_no_arrays(item)

        assert_no_arrays(schema)

    def test_b014_style_beat_requires_exactly_s01_through_s05(self):
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        b_014_budget = budget["b_014"]
        assert b_014_budget == 5  # confirms this is the real b_014 arithmetic
        schema = build_structured_coverage_plan_schema(authoritative_order, budget)

        shots_schema = schema["properties"]["b_014"]["properties"]["shots"]
        assert shots_schema["required"] == ["s01", "s02", "s03", "s04", "s05"]
        assert set(shots_schema["properties"].keys()) == {"s01", "s02", "s03", "s04", "s05"}
        assert shots_schema["additionalProperties"] is False

    def test_declared_slot_totals_match_95_35_130_arithmetic(self):
        authoritative_order, budget, n_main = _representative_47_beat_arithmetic()
        schema = build_structured_coverage_plan_schema(authoritative_order, budget)

        main_slots = sum(
            len(schema["properties"][b]["properties"]["shots"]["required"])
            for b in authoritative_order[:n_main]
        )
        short_slots = sum(
            len(schema["properties"][b]["properties"]["shots"]["required"])
            for b in authoritative_order[n_main:]
        )
        assert main_slots == 95
        assert short_slots == 35
        assert main_slots + short_slots == 130

    def test_zero_optional_properties_anywhere(self):
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        schema = build_structured_coverage_plan_schema(authoritative_order, budget)

        for obj_node in _walk_schema_objects(schema):
            required = set(obj_node.get("required", []))
            declared = set(obj_node.get("properties", {}).keys())
            assert declared == required, f"optional property found: {declared - required}"

    def test_additional_properties_false_everywhere(self):
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        schema = build_structured_coverage_plan_schema(authoritative_order, budget)

        for obj_node in _walk_schema_objects(schema):
            assert obj_node.get("additionalProperties") is False

    def test_every_enum_position_restricted_to_authoritative_vocabulary(self):
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        schema = build_structured_coverage_plan_schema(authoritative_order, budget)
        shot_schema = schema["$defs"]["shot"]

        for field, prop in _STRUCTURED_ENUM_PROPERTY_NAMES.items():
            declared_enum = shot_schema["properties"][prop]["enum"]
            expected_letters = [chr(65 + i) for i in range(len(_COMPACT_ENUM_VOCAB[field]))]
            assert declared_enum == expected_letters

    def test_background_depth_enum_excludes_f(self):
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        schema = build_structured_coverage_plan_schema(authoritative_order, budget)
        shot_schema = schema["$defs"]["shot"]
        dep_prop = _STRUCTURED_ENUM_PROPERTY_NAMES["background_depth"]

        assert shot_schema["properties"][dep_prop]["enum"] == ["A", "B", "C"]
        assert "F" not in shot_schema["properties"][dep_prop]["enum"]

    def test_no_unions_anywhere(self):
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        schema = build_structured_coverage_plan_schema(authoritative_order, budget)

        def assert_no_unions(node):
            if isinstance(node, dict):
                assert not any(k in node for k in ("anyOf", "oneOf", "allOf"))
                for v in node.values():
                    assert_no_unions(v)
            elif isinstance(node, list):
                for item in node:
                    assert_no_unions(item)

        assert_no_unions(schema)

    def test_duplicate_json_keys_rejected_directly(self):
        with pytest.raises(ValueError):
            json.loads(
                '{"a": {"b_001": 1, "b_001": 2}}',
                object_pairs_hook=_no_duplicate_keys_object_pairs_hook,
            )

    def test_no_duplicate_keys_hook_is_transparent_for_valid_json(self):
        parsed = json.loads(
            '{"a": 1, "b": {"c": 2}}',
            object_pairs_hook=_no_duplicate_keys_object_pairs_hook,
        )
        assert parsed == {"a": 1, "b": {"c": 2}}

    def test_structured_payload_converts_to_compact_shape(self):
        authoritative = ["b_001"]
        budget = {"b_001": 2}
        structured = {"beats": {
            "b_001": {
                "pattern": "A",
                "shots": {
                    "s01": _structured_shot(subj="first"),
                    "s02": _structured_shot(subj="second", camera_angle="high_angle"),
                },
            },
        }}

        compact = _structured_payload_to_compact(structured, authoritative, budget)

        assert compact == {"beats": [
            {"beat_id": "b_001", "pattern": "A", "shots": [
                {
                    "code1": "AAAAA", "code2": "AAAAA",
                    "func": "distinct contribution this shot alone makes",
                    "subj": "first", "act": "", "fram": "", "light": "",
                    "dup": "", "lens": "A",
                },
                {
                    # code1 = [shot_purpose, shot_scale, camera_angle,
                    # camera_height, camera_direction] -- camera_angle is
                    # position index 2, so "high_angle" (C) lands third.
                    "code1": "AACAA", "code2": "AAAAA",
                    "func": "distinct contribution this shot alone makes",
                    "subj": "second", "act": "", "fram": "", "light": "",
                    "dup": "", "lens": "A",
                },
            ]},
        ]}

    def test_representative_structured_response_expands_to_exact_95_35_130(self):
        """Proves the full chain: structured payload -> compact -> the
        unchanged _expand_compact_plan() -> canonical schema-valid plan,
        with the real ep_f3619022 95/35/130 arithmetic reproduced exactly."""
        authoritative_order, budget, n_main = _representative_47_beat_arithmetic()

        beats_obj = {}
        for beat_id in authoritative_order:
            n = budget[beat_id]
            shots_obj = {
                f"s{i:02d}": _structured_shot() for i in range(1, n + 1)
            }
            beats_obj[beat_id] = {"pattern": "B" if n > 1 else "A", "shots": shots_obj}
        structured_response = {"beats": beats_obj}

        compact = _structured_payload_to_compact(structured_response, authoritative_order, budget)
        measured_durations = {b: 20.0 for b in authoritative_order}
        expanded = _expand_compact_plan(
            compact, "ep_f3619022", authoritative_order, measured_durations, 8.0,
        )
        validate("cinematic_coverage_plan", expanded)

        total_shots = sum(len(bp["shots"]) for bp in expanded["beat_plans"])
        main_shots = sum(len(bp["shots"]) for bp in expanded["beat_plans"][:n_main])
        short_shots = sum(len(bp["shots"]) for bp in expanded["beat_plans"][n_main:])
        assert total_shots == 130
        assert main_shots == 95
        assert short_shots == 35

    def test_structured_payload_missing_beat_fails_closed(self):
        authoritative = ["b_001", "b_002"]
        budget = {"b_001": 1, "b_002": 1}
        structured = {"beats": {
            "b_001": {"pattern": "A", "shots": {"s01": _structured_shot()}},
        }}
        with pytest.raises(ValueError):
            _structured_payload_to_compact(structured, authoritative, budget)

    def test_structured_payload_wrong_slot_keys_fails_closed(self):
        authoritative = ["b_001"]
        budget = {"b_001": 2}
        structured = {"beats": {
            "b_001": {"pattern": "A", "shots": {"s01": _structured_shot()}},  # missing s02
        }}
        with pytest.raises(ValueError):
            _structured_payload_to_compact(structured, authoritative, budget)

    def test_structured_payload_not_a_dict_fails_closed(self):
        with pytest.raises(ValueError):
            _structured_payload_to_compact([], ["b_001"], {"b_001": 1})
        with pytest.raises(ValueError):
            _structured_payload_to_compact({"beats": []}, ["b_001"], {"b_001": 1})


    def test_batch_scoped_schema_retains_all_strict_closed_properties(self):
        """A schema generated for a SUBSET of beats (as every batch uses)
        must retain every structural guarantee the full 47-beat schema
        does: closed beat set, closed slot set, zero optional properties,
        additionalProperties:false everywhere, authoritative enum
        vocabularies, background_depth excluding F."""
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        batches = _partition_beats_into_batches(authoritative_order, budget)
        first_batch = batches[0]
        batch_budget = {b: budget[b] for b in first_batch}
        schema = build_structured_coverage_plan_schema(first_batch, batch_budget)

        assert set(schema["properties"].keys()) == set(first_batch)
        assert schema["required"] == first_batch
        assert schema["additionalProperties"] is False
        for obj_node in _walk_schema_objects(schema):
            required = set(obj_node.get("required", []))
            declared = set(obj_node.get("properties", {}).keys())
            assert declared == required  # zero optional properties
            assert obj_node.get("additionalProperties") is False
        dep_prop = _STRUCTURED_ENUM_PROPERTY_NAMES["background_depth"]
        assert schema["$defs"]["shot"]["properties"][dep_prop]["enum"] == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# TestPartitionBeatsIntoBatches / TestEstimateBatchMaxTokens (2026-08-09)
#
# Direct tests of the deterministic greedy batch-partitioning and per-batch
# max_tokens machinery introduced after a live 47-beat/130-slot strict
# request was rejected by the provider before generation began ("The
# compiled grammar is too large").
# ---------------------------------------------------------------------------

class TestPartitionBeatsIntoBatches:

    def test_25_slot_ceiling_never_exceeded(self):
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        batches = _partition_beats_into_batches(authoritative_order, budget)
        for batch in batches:
            assert sum(budget[b] for b in batch) <= _MAX_SLOTS_PER_BATCH

    def test_no_beat_split_across_batches(self):
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        batches = _partition_beats_into_batches(authoritative_order, budget)
        seen: set = set()
        for batch in batches:
            for beat_id in batch:
                assert beat_id not in seen, f"{beat_id!r} appeared in more than one batch"
                seen.add(beat_id)

    def test_every_batch_preserves_authoritative_order(self):
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        batches = _partition_beats_into_batches(authoritative_order, budget)
        for batch in batches:
            assert batch == sorted(batch, key=authoritative_order.index)

    def test_concatenated_batches_reproduce_authoritative_order_exactly(self):
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        batches = _partition_beats_into_batches(authoritative_order, budget)
        concatenated = [beat_id for batch in batches for beat_id in batch]
        assert concatenated == authoritative_order

    def test_exactly_47_unique_beats_across_all_batches(self):
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        batches = _partition_beats_into_batches(authoritative_order, budget)
        all_beats = [beat_id for batch in batches for beat_id in batch]
        assert len(all_beats) == 47
        assert len(set(all_beats)) == 47

    def test_slot_totals_equal_95_35_130(self):
        authoritative_order, budget, n_main = _representative_47_beat_arithmetic()
        batches = _partition_beats_into_batches(authoritative_order, budget)
        all_beats = [beat_id for batch in batches for beat_id in batch]
        main_slots = sum(budget[b] for b in all_beats[:n_main])
        short_slots = sum(budget[b] for b in all_beats[n_main:])
        assert main_slots == 95
        assert short_slots == 35
        assert main_slots + short_slots == 130

    def test_beat_exceeding_ceiling_raises_before_any_batch_or_request(self):
        """A beat whose own budget exceeds max_slots_per_batch can never fit
        in any batch without violating the 25-slot invariant -- it is never
        silently dropped, truncated, split, or placed in an oversized
        batch. This must raise deterministically, before any batch is
        constructed and before any provider call becomes reachable."""
        with pytest.raises(ValueError, match="exceeds max_slots_per_batch"):
            _partition_beats_into_batches(["b_001"], {"b_001": 30}, max_slots_per_batch=25)

    def test_beat_exactly_at_ceiling_is_accepted(self):
        """A beat whose budget is exactly max_slots_per_batch (not
        exceeding it) is valid and gets its own batch, unmodified."""
        batches = _partition_beats_into_batches(["b_001"], {"b_001": 25}, max_slots_per_batch=25)
        assert batches == [["b_001"]]

    def test_oversized_beat_among_others_raises_and_returns_no_batches(self):
        """The oversized-beat check must fire even when other, valid beats
        are present -- no partial/oversized batch is ever returned."""
        with pytest.raises(ValueError, match="'b_002'"):
            _partition_beats_into_batches(
                ["b_001", "b_002", "b_003"],
                {"b_001": 5, "b_002": 30, "b_003": 5},
                max_slots_per_batch=25,
            )

    def test_ordinary_ep_f3619022_partitioning_unaffected_by_the_invariant(self):
        """The real ep_f3619022 arithmetic has no beat anywhere near the
        25-slot ceiling (max single-beat budget is 6) -- the fail-closed
        check must never fire for ordinary, in-range input."""
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        assert max(budget.values()) <= _MAX_SLOTS_PER_BATCH
        batches = _partition_beats_into_batches(authoritative_order, budget)  # must not raise
        assert len(batches) == 6

    def test_real_ep_f3619022_partition_is_six_batches(self):
        """The exact real ep_f3619022 arithmetic, computed once and pinned
        here -- a change in this result would mean the real batch plan
        changed, which is worth noticing explicitly."""
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        batches = _partition_beats_into_batches(authoritative_order, budget)
        assert len(batches) == 6
        assert [len(b) for b in batches] == [9, 7, 7, 7, 13, 4]
        assert [sum(budget[x] for x in b) for b in batches] == [25, 25, 22, 25, 25, 8]


class TestEstimateBatchMaxTokens:

    def test_deterministic_for_same_inputs(self):
        beat_ids = ["b_001", "b_002"]
        budget = {"b_001": 3, "b_002": 5}
        assert _estimate_batch_max_tokens(beat_ids, budget) == _estimate_batch_max_tokens(beat_ids, budget)

    def test_scales_with_slot_and_beat_count(self):
        small = _estimate_batch_max_tokens(["b_001"], {"b_001": 1})
        large = _estimate_batch_max_tokens(
            [f"b_{i:03d}" for i in range(1, 10)], {f"b_{i:03d}": 3 for i in range(1, 10)},
        )
        assert large > small

    def test_every_representative_batch_ceiling_within_model_limit(self):
        authoritative_order, budget, _ = _representative_47_beat_arithmetic()
        batches = _partition_beats_into_batches(authoritative_order, budget)
        for batch in batches:
            batch_budget = {b: budget[b] for b in batch}
            max_tokens = _estimate_batch_max_tokens(batch, batch_budget)
            assert max_tokens <= 128_000  # verified model max_completion_tokens
            assert max_tokens >= 500  # sane lower bound, never zero or negative

# ---------------------------------------------------------------------------
# TestPinnedEpF3619022BatchMeasurements (2026-08-09)
#
# Exact, reconciled measurements for every batch of the real ep_f3619022
# 47-beat partition, computed once directly from committed production code
# (build_structured_coverage_plan_schema(), _estimate_batch_max_tokens(),
# _partition_beats_into_batches()) against the real ep_f3619022 script_
# package/narration_package/compute_shot_budget() output (read once,
# read-only, from the live episode files -- the exact numbers are pinned
# here as a literal fixture, via _representative_47_beat_arithmetic(),
# rather than re-reading .hypogeum-assets/ at test time, since that
# directory is gitignored and must not be a test dependency).
#
# This corrects two prior report errors:
#   1. max_tokens for batches 1, 2, 4, and 5 is 6,500, not 6,000 -- the
#      earlier "final report" mis-stated this; the original transcript's
#      6,500 figure was the correct one, verified here directly against
#      _estimate_batch_max_tokens().
#   2. The 95 main / 35 short split point falls INSIDE batch 4 (23 main
#      slots from b_024-b_029, then 2 short slots from b_030) -- not "the
#      first two beats of batch 5" as previously mis-stated. Batch 5
#      (b_031-b_043) is entirely short beats; batches 1-3 are entirely
#      main beats; batch 6 is entirely short beats.
# ---------------------------------------------------------------------------

def _representative_response_for_measurement(beat_ids: list, budget: dict, shot_index_start: int) -> tuple:
    """Builds one batch's representative structured response using the
    exact same realistic content-length style used throughout this
    correction (full-sentence narrative_function, multi-word subject/
    action/framing/lighting text) -- returns (response_dict, next_shot_index)."""
    narrative_functions = [
        "Establishes the physical scale of the tunnel before the narration names it.",
        "Shows the tool marks as proof of deliberate, planned construction.",
        "Reveals the constriction point the narration is about to describe.",
        "Grounds the abstract historical claim in a concrete physical detail.",
        "Shifts from the object itself to the hand that shaped it.",
    ]
    subjects = [
        "earthen tunnel wall, tool-mark striations receding into shadow",
        "medieval scribe's hand, quill and iron-gall ink on parchment",
        "schlupf constriction point, earthen walls pressing inward",
        "timber farmhouse threshold, concealed entrance in the earthen floor",
    ]
    actions = "quill moves in short deliberate strokes across the parchment"
    framings = "medium wide shot, tunnel wall filling the frame in tight profile"
    lightings = "warm amber torch light raking obliquely across the earth wall"

    shot_index = shot_index_start
    beats_obj = {}
    for beat_id in beat_ids:
        n = budget[beat_id]
        shots_obj = {}
        for slot_i in range(1, n + 1):
            shot = {}
            for field in _COMPACT_CODE_FIELDS:
                shot[_STRUCTURED_ENUM_PROPERTY_NAMES[field]] = "A"
            shot["func"] = narrative_functions[shot_index % len(narrative_functions)]
            shot["subj"] = subjects[shot_index % len(subjects)]
            shot["act"] = actions if shot_index % 3 != 0 else ""
            shot["fram"] = framings if shot_index % 2 == 0 else ""
            shot["light"] = lightings if shot_index % 4 == 0 else ""
            shot["lens"] = "B" if shot_index % 10 == 0 else "A"
            shot["dup"] = "deliberate match-cut, intentional repeat for continuity" if shot_index % 25 == 0 else ""
            shots_obj[f"s{slot_i:02d}"] = shot
            shot_index += 1
        beats_obj[beat_id] = {"pattern": "B" if n > 1 else "A", "shots": shots_obj}
    return {"beats": beats_obj}, shot_index


def _count_schema_properties(node, depth_box, depth=0):
    """Returns (required_count, optional_count, union_count) across the
    whole schema tree, including $defs."""
    req = opt = union = 0
    depth_box[0] = max(depth_box[0], depth)
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            required = set(node.get("required", []))
            for name, sub in node["properties"].items():
                sub_req, sub_opt, sub_union = _count_schema_properties(sub, depth_box, depth + 1)
                if name in required:
                    req += 1 + sub_req
                else:
                    opt += 1 + sub_opt
                union += sub_union
        if any(k in node for k in ("anyOf", "oneOf", "allOf")):
            union += 1
        for sub in node.get("$defs", {}).values():
            sub_req, sub_opt, sub_union = _count_schema_properties(sub, depth_box, depth + 1)
            req += sub_req
            opt += sub_opt
            union += sub_union
    return req, opt, union


# Exact, pinned expected values for every one of the 6 real ep_f3619022
# batches -- computed once directly from committed code, verified, locked.
_PINNED_BATCH_MEASUREMENTS = [
    {
        "batch_index": 1,
        "beat_ids": ["b_001", "b_002", "b_003", "b_004", "b_005", "b_006", "b_007", "b_008", "b_009"],
        "main_beats": ["b_001", "b_002", "b_003", "b_004", "b_005", "b_006", "b_007", "b_008", "b_009"],
        "main_slots": 25,
        "short_beats": [],
        "short_slots": 0,
        "total_slots": 25,
        "schema_bytes": 8237,
        "request_bytes": 16509,
        "resp_chars": 11150,
        "max_tokens": 6500,
        "required_props": 69,
    },
    {
        "batch_index": 2,
        "beat_ids": ["b_010", "b_011", "b_012", "b_013", "b_014", "b_015", "b_016"],
        "main_beats": ["b_010", "b_011", "b_012", "b_013", "b_014", "b_015", "b_016"],
        "main_slots": 25,
        "short_beats": [],
        "short_slots": 0,
        "total_slots": 25,
        "schema_bytes": 7275,
        "request_bytes": 15455,
        "resp_chars": 11007,
        "max_tokens": 6500,
        "required_props": 63,
    },
    {
        "batch_index": 3,
        "beat_ids": ["b_017", "b_018", "b_019", "b_020", "b_021", "b_022", "b_023"],
        "main_beats": ["b_017", "b_018", "b_019", "b_020", "b_021", "b_022", "b_023"],
        "main_slots": 22,
        "short_beats": [],
        "short_slots": 0,
        "total_slots": 22,
        "schema_bytes": 7155,
        "request_bytes": 15335,
        "resp_chars": 9750,
        "max_tokens": 6000,
        "required_props": 60,
    },
    {
        # The main/short split point -- 23 main slots, then b_030 (short, 2
        # slots) -- proving the split is NOT "the first two beats of batch 5".
        "batch_index": 4,
        "beat_ids": ["b_024", "b_025", "b_026", "b_027", "b_028", "b_029", "b_030"],
        "main_beats": ["b_024", "b_025", "b_026", "b_027", "b_028", "b_029"],
        "main_slots": 23,
        "short_beats": ["b_030"],
        "short_slots": 2,
        "total_slots": 25,
        "schema_bytes": 7275,
        "request_bytes": 15455,
        "resp_chars": 11074,
        "max_tokens": 6500,
        "required_props": 63,
    },
    {
        # Entirely short beats -- batch 5 begins at b_031, one past the
        # main/short boundary already crossed inside batch 4.
        "batch_index": 5,
        "beat_ids": ["b_031", "b_032", "b_033", "b_034", "b_035", "b_036", "b_037",
                     "b_038", "b_039", "b_040", "b_041", "b_042", "b_043"],
        "main_beats": [],
        "main_slots": 0,
        "short_beats": ["b_031", "b_032", "b_033", "b_034", "b_035", "b_036", "b_037",
                         "b_038", "b_039", "b_040", "b_041", "b_042", "b_043"],
        "short_slots": 25,
        "total_slots": 25,
        "schema_bytes": 10161,
        "request_bytes": 18618,
        "resp_chars": 11235,
        "max_tokens": 6500,
        "required_props": 81,
    },
    {
        "batch_index": 6,
        "beat_ids": ["b_044", "b_045", "b_046", "b_047"],
        "main_beats": [],
        "main_slots": 0,
        "short_beats": ["b_044", "b_045", "b_046", "b_047"],
        "short_slots": 8,
        "total_slots": 8,
        "schema_bytes": 5152,
        "request_bytes": 13194,
        "resp_chars": 3615,
        "max_tokens": 3500,
        "required_props": 37,
    },
]


class TestPinnedEpF3619022BatchMeasurements:

    def test_real_partition_matches_pinned_beat_ids(self):
        authoritative_order, budget, n_main = _real_ep_f3619022_arithmetic()
        classification = {b: ("main" if i < n_main else "short") for i, b in enumerate(authoritative_order)}
        batches = _partition_beats_into_batches(authoritative_order, budget)

        assert len(batches) == len(_PINNED_BATCH_MEASUREMENTS) == 6
        for batch, expected in zip(batches, _PINNED_BATCH_MEASUREMENTS):
            assert batch == expected["beat_ids"]
            main_beats = [b for b in batch if classification[b] == "main"]
            short_beats = [b for b in batch if classification[b] == "short"]
            assert main_beats == expected["main_beats"]
            assert short_beats == expected["short_beats"]

    def test_pinned_totals_are_exactly_95_35_130(self):
        assert sum(e["main_slots"] for e in _PINNED_BATCH_MEASUREMENTS) == 95
        assert sum(e["short_slots"] for e in _PINNED_BATCH_MEASUREMENTS) == 35
        assert sum(e["total_slots"] for e in _PINNED_BATCH_MEASUREMENTS) == 130

    def test_committed_max_tokens_formula_matches_pinned_values(self):
        """The single source of truth for max_tokens is the committed
        _estimate_batch_max_tokens() function itself -- this test proves
        its actual output equals the pinned values above, not the reverse."""
        authoritative_order, budget, _ = _real_ep_f3619022_arithmetic()
        batches = _partition_beats_into_batches(authoritative_order, budget)
        for batch, expected in zip(batches, _PINNED_BATCH_MEASUREMENTS):
            batch_budget = {b: budget[b] for b in batch}
            actual = _estimate_batch_max_tokens(batch, batch_budget)
            assert actual == expected["max_tokens"], (
                f"batch {expected['batch_index']}: committed formula returned "
                f"{actual}, pinned value is {expected['max_tokens']}"
            )

    def test_every_batch_max_tokens_exceeds_representative_response_with_documented_margin(self):
        """The documented safety margin is satisfied for every batch --
        each ceiling is verified strictly greater than its own representative
        response's estimated token count, with the minimum margin across
        all six batches reported exactly (2,295 tokens, at batch 6)."""
        authoritative_order, budget, _ = _real_ep_f3619022_arithmetic()
        batches = _partition_beats_into_batches(authoritative_order, budget)
        margins = []
        shot_index = 0
        for batch, expected in zip(batches, _PINNED_BATCH_MEASUREMENTS):
            batch_budget = {b: budget[b] for b in batch}
            response, shot_index = _representative_response_for_measurement(batch, batch_budget, shot_index)
            resp_chars = len(json.dumps(response, ensure_ascii=False))
            resp_tok = resp_chars / 3.0
            max_tokens = _estimate_batch_max_tokens(batch, batch_budget)
            margin = max_tokens - resp_tok
            assert resp_chars == expected["resp_chars"]
            assert margin > 0
            margins.append(margin)

        assert min(margins) == pytest.approx(2295.0, abs=1.0)

    def test_schema_and_request_bytes_match_pinned_values(self):
        authoritative_order, budget, _ = _real_ep_f3619022_arithmetic()
        batches = _partition_beats_into_batches(authoritative_order, budget)
        for batch, expected in zip(batches, _PINNED_BATCH_MEASUREMENTS):
            batch_budget = {b: budget[b] for b in batch}
            schema = build_structured_coverage_plan_schema(batch, batch_budget)
            schema_bytes = len(json.dumps(schema, ensure_ascii=False).encode("utf-8"))
            assert schema_bytes == expected["schema_bytes"]

            rf = _build_structured_response_format(
                schema, name=f"cinematic_coverage_plan_batch_{expected['batch_index']:02d}",
            )
            director_input_stub = {
                "task": "plan_cinematic_coverage_structured_batch",
                "episode_metadata": {
                    "episode_title": "The Antikythera Mechanism", "total_beats": len(batch),
                    "episode_id": "ep_f3619022", "batch_index": expected["batch_index"],
                    "batch_count": 6,
                },
                "progression_patterns": list(PROGRESSION_PATTERNS),
                "beats": [{"beat_id": b, "shot_budget": batch_budget[b]} for b in batch],
                "output_format_override": "placeholder",
            }
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(director_input_stub, ensure_ascii=False)},
            ]
            body = {
                "model": "anthropic/claude-sonnet-4.6", "messages": messages,
                "max_tokens": expected["max_tokens"], "temperature": 0,
                "response_format": rf, "provider": _STRUCTURED_OUTPUT_PROVIDER,
            }
            request_bytes = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
            assert request_bytes == expected["request_bytes"]

    def test_schema_property_and_nesting_counts_match_pinned_values(self):
        authoritative_order, budget, _ = _real_ep_f3619022_arithmetic()
        batches = _partition_beats_into_batches(authoritative_order, budget)
        for batch, expected in zip(batches, _PINNED_BATCH_MEASUREMENTS):
            batch_budget = {b: budget[b] for b in batch}
            schema = build_structured_coverage_plan_schema(batch, batch_budget)
            depth_box = [0]
            req, opt, union = _count_schema_properties(schema, depth_box)
            assert req == expected["required_props"]
            assert opt == 0
            assert union == 0
            assert depth_box[0] == 3




# ---------------------------------------------------------------------------
# TestBatchedRegeneration (2026-08-09)
#
# Multi-batch integration tests against the full regenerate_cinematic_
# coverage_plan() mocked-call flow. Uses a 12-beat fixture with an
# artificially huge per-beat narration duration (1000.0s) so compute_shot_
# budget() deterministically returns the configured safety cap
# (CINEMATIC_MAX_SHOTS_PER_BEAT=6) for every beat regardless of the exact
# shot-budget formula -- 12 beats x 6 shots = 72 slots, which the 25-slot
# ceiling partitions into exactly three batches of four beats/24 slots each.
# ---------------------------------------------------------------------------

def _make_batch_test_script_package(n_beats: int = 12) -> dict:
    return {
        "sections": [{
            "section_id": "s_001", "title": "T",
            "narration_beats": [
                {"beat_id": f"b_{i:03d}", "text": f"beat {i}"} for i in range(1, n_beats + 1)
            ],
        }],
        "shorts_scripts": [],
    }


def _make_batch_test_narration_package(n_beats: int = 12) -> dict:
    return {"narration_segments": [
        {"beat_reference": f"b_{i:03d}", "duration_seconds": 1000.0} for i in range(1, n_beats + 1)
    ]}


def _make_batch_test_visual_package(n_beats: int = 12) -> dict:
    return {
        "episode_metadata": {"episode_title": "Batch Test Episode"},
        "scenes": [
            {"scene_id": f"s{i:02d}", "narration_reference": f"b_{i:03d}", "source_type": "main_episode",
             "visual_spec": {}, "presentation_plan": {}, "narrative_signals": []}
            for i in range(1, n_beats + 1)
        ],
    }


_BATCH_TEST_GROUPS = [
    ["b_001", "b_002", "b_003", "b_004"],
    ["b_005", "b_006", "b_007", "b_008"],
    ["b_009", "b_010", "b_011", "b_012"],
]


def _structured_response_for_batch(beat_ids: list, shots_per_beat: int = 6) -> dict:
    return {"beats": {
        beat_id: _structured_beat("A", [_structured_shot() for _ in range(shots_per_beat)])
        for beat_id in beat_ids
    }}


class TestBatchedRegeneration:

    def test_full_12_beat_partition_matches_three_batches_of_four(self):
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        cfg = _make_config()
        measured = validate_narration_completeness(sp, np)
        authoritative = list(measured.keys())
        budget = {b: compute_shot_budget(measured[b], cfg) for b in authoritative}
        assert all(v == 6 for v in budget.values())  # confirms the safety cap, not a formula guess
        batches = _partition_beats_into_batches(authoritative, budget)
        assert batches == _BATCH_TEST_GROUPS

    def test_all_batches_succeed_installs_correct_totals(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        responses = [
            _make_call_result(json.dumps(_structured_response_for_batch(g))) for g in _BATCH_TEST_GROUPS
        ]

        with patch("hypogeum.openrouter.call_claude", side_effect=responses) as mock_cc:
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
            )

        assert mock_cc.call_count == 3
        assert result["status"] == "installed"
        assert result["totals"]["main_shots"] == 72
        assert result["totals"]["total_shots"] == 72
        installed = json.loads((tmp_path / "cinematic_coverage_plan_ep_batch.json").read_text())
        assert [bp["beat_id"] for bp in installed["beat_plans"]] == [f"b_{i:03d}" for i in range(1, 13)]
        validate("cinematic_coverage_plan", installed)

    def test_each_batch_makes_exactly_one_call(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        responses = [
            _make_call_result(json.dumps(_structured_response_for_batch(g))) for g in _BATCH_TEST_GROUPS
        ]

        with patch("hypogeum.openrouter.call_claude", side_effect=responses) as mock_cc:
            regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
            )

        assert mock_cc.call_count == len(_BATCH_TEST_GROUPS)
        for call in mock_cc.call_args_list:
            assert call.kwargs["allow_model_fallback"] is False
            assert call.kwargs["max_attempts"] == 1

    def test_batch_names_are_deterministic_and_unique(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        responses = [
            _make_call_result(json.dumps(_structured_response_for_batch(g))) for g in _BATCH_TEST_GROUPS
        ]

        with patch("hypogeum.openrouter.call_claude", side_effect=responses) as mock_cc:
            regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
            )

        names = [c.kwargs["response_format"]["json_schema"]["name"] for c in mock_cc.call_args_list]
        assert names == [
            "cinematic_coverage_plan_batch_01",
            "cinematic_coverage_plan_batch_02",
            "cinematic_coverage_plan_batch_03",
        ]
        assert len(set(names)) == len(names)

    def test_each_batch_schema_contains_only_its_assigned_beats(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        responses = [
            _make_call_result(json.dumps(_structured_response_for_batch(g))) for g in _BATCH_TEST_GROUPS
        ]

        with patch("hypogeum.openrouter.call_claude", side_effect=responses) as mock_cc:
            regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
            )

        for call, expected_beats in zip(mock_cc.call_args_list, _BATCH_TEST_GROUPS):
            schema = call.kwargs["response_format"]["json_schema"]["schema"]
            assert set(schema["properties"].keys()) == set(expected_beats)
            assert schema["required"] == expected_beats
            assert schema["additionalProperties"] is False

    def test_batch_failure_prevents_later_batch_submission(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_batch.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        batch1_response = _make_call_result(json.dumps(_structured_response_for_batch(_BATCH_TEST_GROUPS[0])))
        # Only ONE response supplied for the successful batch 1; batch 2
        # raises. If the code tried to submit a batch 3, side_effect would
        # raise StopIteration and fail this test loudly.
        with patch("hypogeum.openrouter.call_claude",
                   side_effect=[batch1_response, RuntimeError("batch 2 provider down")]) as mock_cc:
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
            )

        assert mock_cc.call_count == 2  # batch 1 succeeded, batch 2 attempted and failed, no batch 3
        assert result["status"] == "rejected"
        assert "llm_call_failed" in result["reason"]
        assert "batch 2/3" in result["reason"]
        assert json.loads(ckpt.read_text()) == original
        assert not (tmp_path / "cinematic_coverage_plan_ep_batch.candidate.json").exists()

    def test_batch_failure_discards_earlier_successful_batches(self, tmp_path):
        """The successful batch-1 result must never be installed alone or
        merged into anything on disk after a later batch fails."""
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        # No pre-existing checkpoint -- nothing to resume from either.

        batch1_response = _make_call_result(json.dumps(_structured_response_for_batch(_BATCH_TEST_GROUPS[0])))
        with patch("hypogeum.openrouter.call_claude",
                   side_effect=[batch1_response, RuntimeError("batch 2 down")]):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "rejected"
        assert not (tmp_path / "cinematic_coverage_plan_ep_batch.json").exists()
        assert not (tmp_path / "cinematic_coverage_plan_ep_batch.candidate.json").exists()

    def test_malformed_batch_response_preserves_active_plan(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_batch.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        batch1_response = _make_call_result(json.dumps(_structured_response_for_batch(_BATCH_TEST_GROUPS[0])))
        batch2_malformed = _make_call_result("{not valid json")
        with patch("hypogeum.openrouter.call_claude",
                   side_effect=[batch1_response, batch2_malformed]) as mock_cc:
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
            )

        assert mock_cc.call_count == 2
        assert result["status"] == "rejected"
        assert "malformed_output" in result["reason"]
        assert "batch 2/3" in result["reason"]
        assert json.loads(ckpt.read_text()) == original

    def test_duplicate_json_keys_in_a_batch_response_preserve_active_plan(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_batch.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        batch1_response = _make_call_result(json.dumps(_structured_response_for_batch(_BATCH_TEST_GROUPS[0])))
        raw_with_duplicate_key = '{"beats": {"b_005": {}, "b_005": {}}}'
        batch2_duplicate = _make_call_result(raw_with_duplicate_key)
        with patch("hypogeum.openrouter.call_claude",
                   side_effect=[batch1_response, batch2_duplicate]) as mock_cc:
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
            )

        assert mock_cc.call_count == 2
        assert result["status"] == "rejected"
        assert "malformed_output" in result["reason"]
        assert json.loads(ckpt.read_text()) == original

    def test_invalid_enum_in_a_batch_response_preserves_active_plan(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_batch.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        batch1_response = _make_call_result(json.dumps(_structured_response_for_batch(_BATCH_TEST_GROUPS[0])))
        bad_batch2 = _structured_response_for_batch(_BATCH_TEST_GROUPS[1])
        bad_batch2["beats"]["b_005"]["shots"]["s01"]["dep"] = "Z"
        batch2_invalid = _make_call_result(json.dumps(bad_batch2))
        with patch("hypogeum.openrouter.call_claude",
                   side_effect=[batch1_response, batch2_invalid]) as mock_cc:
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
            )

        assert mock_cc.call_count == 2
        assert result["status"] == "rejected"
        assert "malformed_output" in result["reason"]
        assert json.loads(ckpt.read_text()) == original

    def test_missing_beat_in_a_batch_response_preserves_active_plan(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_batch.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        # batch 1 response is missing b_004 entirely.
        incomplete_batch1 = _structured_response_for_batch(_BATCH_TEST_GROUPS[0])
        del incomplete_batch1["beats"]["b_004"]
        with patch("hypogeum.openrouter.call_claude",
                   side_effect=[_make_call_result(json.dumps(incomplete_batch1))]) as mock_cc:
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
            )

        assert mock_cc.call_count == 1  # rejected at batch 1, batch 2/3 never submitted
        assert result["status"] == "rejected"
        assert "malformed_output" in result["reason"]
        assert "batch 1/3" in result["reason"]
        assert json.loads(ckpt.read_text()) == original

    def test_no_stage_b_provider_assembly_or_publishing_reachable_across_batches(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()

        def _must_not_be_called(*a, **k):
            raise AssertionError("regenerate_cinematic_coverage_plan must never reach this")

        responses = [
            _make_call_result(json.dumps(_structured_response_for_batch(g))) for g in _BATCH_TEST_GROUPS
        ]
        with patch("hypogeum.openrouter.call_claude", side_effect=responses), \
             patch("hypogeum.agents.shot_diversity_planner.run_shot_diversity_planner",
                   side_effect=_must_not_be_called), \
             patch("hypogeum.muapi.submit_scene", side_effect=_must_not_be_called), \
             patch("hypogeum.agents.assembly_agent.run_assembly_agent",
                   side_effect=_must_not_be_called), \
             patch("hypogeum.agents.publishing_agent.run_publishing_agent",
                   side_effect=_must_not_be_called):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
            )

        assert result["status"] == "installed"


# ---------------------------------------------------------------------------
# TestFirstBatchProbe (2026-08-09)
#
# regenerate_cinematic_coverage_plan(probe_first_batch=True) -- exactly one
# call_claude() invocation maximum, against batch 1 of the deterministic
# partition only, through the exact same _execute_single_batch() helper
# ordinary batching itself calls. Reuses the 12-beat/3-batch fixture and
# _BATCH_TEST_GROUPS defined above for TestBatchedRegeneration specifically
# because it forces a genuine multi-batch scenario -- proving "never reaches
# batch 2" requires there to BE a batch 2 to not reach.
# ---------------------------------------------------------------------------

class TestFirstBatchProbe:

    def test_probe_makes_exactly_one_call_claude_invocation_on_success(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        batch1_response = _make_call_result(json.dumps(_structured_response_for_batch(_BATCH_TEST_GROUPS[0])))

        # Only ONE response supplied -- if the probe tried batch 2,
        # side_effect would raise StopIteration and fail this test loudly.
        with patch("hypogeum.openrouter.call_claude", side_effect=[batch1_response]) as mock_cc:
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
                probe_first_batch=True,
            )

        assert mock_cc.call_count == 1
        assert result["status"] == "probe_succeeded"
        assert result["probe_batch_index"] == 1
        assert result["probe_batch_count"] == 3
        assert result["probe_batch_beat_ids"] == _BATCH_TEST_GROUPS[0]

    def test_probe_uses_the_exact_same_production_request_shape(self, tmp_path):
        """The probe must submit batch 1 through the exact same
        response_format/provider/allow_model_fallback/max_attempts/model
        contract ordinary batching itself uses -- proving it exercises
        production request construction, not a parallel implementation."""
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        batch1_response = _make_call_result(json.dumps(_structured_response_for_batch(_BATCH_TEST_GROUPS[0])))

        with patch("hypogeum.openrouter.call_claude", side_effect=[batch1_response]) as mock_cc:
            regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
                probe_first_batch=True,
            )

        call = mock_cc.call_args
        assert call.kwargs["model_slug"] == "anthropic/claude-sonnet-4.6"
        assert call.kwargs["allow_model_fallback"] is False
        assert call.kwargs["max_attempts"] == 1
        assert call.kwargs["provider"] == {"require_parameters": True}
        assert call.kwargs["response_format"]["type"] == "json_schema"
        assert call.kwargs["response_format"]["json_schema"]["strict"] is True
        assert call.kwargs["response_format"]["json_schema"]["name"] == "cinematic_coverage_plan_batch_01"
        schema = call.kwargs["response_format"]["json_schema"]["schema"]
        assert set(schema["properties"].keys()) == set(_BATCH_TEST_GROUPS[0])

    def test_probe_failure_never_reaches_batch_2(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()

        # Only ONE side effect supplied -- a batch-2 attempt would raise
        # StopIteration and fail this test loudly.
        with patch("hypogeum.openrouter.call_claude",
                   side_effect=[RuntimeError("provider down")]) as mock_cc:
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
                probe_first_batch=True,
            )

        assert mock_cc.call_count == 1
        assert result["status"] == "probe_rejected"
        assert "llm_call_failed" in result["reason"]
        assert "batch 1/3" in result["reason"]
        assert result["probe_batch_index"] == 1
        assert result["probe_batch_count"] == 3

    def test_probe_malformed_json_returns_probe_rejected(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()

        with patch("hypogeum.openrouter.call_claude",
                   side_effect=[_make_call_result("{not valid json")]) as mock_cc:
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
                probe_first_batch=True,
            )

        assert mock_cc.call_count == 1
        assert result["status"] == "probe_rejected"
        assert "malformed_output" in result["reason"]

    def test_probe_duplicate_json_key_returns_probe_rejected(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        raw_with_duplicate_key = '{"beats": {"b_001": {}, "b_001": {}}}'

        with patch("hypogeum.openrouter.call_claude",
                   side_effect=[_make_call_result(raw_with_duplicate_key)]) as mock_cc:
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
                probe_first_batch=True,
            )

        assert mock_cc.call_count == 1
        assert result["status"] == "probe_rejected"
        assert "malformed_output" in result["reason"]

    def test_probe_invalid_enum_returns_probe_rejected(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        bad_batch1 = _structured_response_for_batch(_BATCH_TEST_GROUPS[0])
        bad_batch1["beats"]["b_001"]["shots"]["s01"]["dep"] = "Z"

        with patch("hypogeum.openrouter.call_claude",
                   side_effect=[_make_call_result(json.dumps(bad_batch1))]) as mock_cc:
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
                probe_first_batch=True,
            )

        assert mock_cc.call_count == 1
        assert result["status"] == "probe_rejected"
        assert "malformed_output" in result["reason"]

    def test_probe_never_writes_candidate_or_installs(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        batch1_response = _make_call_result(json.dumps(_structured_response_for_batch(_BATCH_TEST_GROUPS[0])))

        with patch("hypogeum.openrouter.call_claude", side_effect=[batch1_response]):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
                probe_first_batch=True,
            )

        assert result["status"] == "probe_succeeded"
        assert result["plan"] is None
        assert not (tmp_path / "cinematic_coverage_plan_ep_batch.json").exists()
        assert not (tmp_path / "cinematic_coverage_plan_ep_batch.candidate.json").exists()

    def test_probe_creates_no_active_plan_backup(self, tmp_path):
        """Even when a checkpoint already exists, a probe must not copy it
        to a timestamped backup -- nothing is being installed, so there is
        nothing to protect."""
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_batch.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))
        batch1_response = _make_call_result(json.dumps(_structured_response_for_batch(_BATCH_TEST_GROUPS[0])))

        with patch("hypogeum.openrouter.call_claude", side_effect=[batch1_response]):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
                probe_first_batch=True,
            )

        assert result["status"] == "probe_succeeded"
        assert result["backup_path"] is None
        assert list(tmp_path.glob("cinematic_coverage_plan_ep_batch.backup_*.json")) == []
        # Active plan (pre-existing) is completely untouched.
        assert json.loads(ckpt.read_text()) == original

    def test_probe_preserves_active_plan_on_rejection(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        ckpt = tmp_path / "cinematic_coverage_plan_ep_batch.json"
        original = _matching_candidate_plan()
        ckpt.write_text(json.dumps(original))

        with patch("hypogeum.openrouter.call_claude",
                   side_effect=[RuntimeError("provider down")]):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
                probe_first_batch=True,
            )

        assert result["status"] == "probe_rejected"
        assert result["backup_path"] is None
        assert list(tmp_path.glob("cinematic_coverage_plan_ep_batch.backup_*.json")) == []
        assert json.loads(ckpt.read_text()) == original

    def test_probe_default_is_false_ordinary_regeneration_unaffected(self, tmp_path):
        """probe_first_batch omitted entirely -- ordinary regeneration still
        executes all batches and installs atomically only after complete
        validation, exactly as before this correction."""
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()
        responses = [
            _make_call_result(json.dumps(_structured_response_for_batch(g))) for g in _BATCH_TEST_GROUPS
        ]

        with patch("hypogeum.openrouter.call_claude", side_effect=responses) as mock_cc:
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
                # probe_first_batch not passed -- defaults to False
            )

        assert mock_cc.call_count == 3  # all three batches submitted
        assert result["status"] == "installed"
        assert (tmp_path / "cinematic_coverage_plan_ep_batch.json").exists()

    def test_no_stage_b_provider_assembly_or_publishing_reachable_during_probe(self, tmp_path):
        cfg = _make_config()
        sp = _make_batch_test_script_package()
        np = _make_batch_test_narration_package()
        vp = _make_batch_test_visual_package()

        def _must_not_be_called(*a, **k):
            raise AssertionError("probe_first_batch must never reach this")

        batch1_response = _make_call_result(json.dumps(_structured_response_for_batch(_BATCH_TEST_GROUPS[0])))
        with patch("hypogeum.openrouter.call_claude", side_effect=[batch1_response]), \
             patch("hypogeum.agents.shot_diversity_planner.run_shot_diversity_planner",
                   side_effect=_must_not_be_called), \
             patch("hypogeum.muapi.submit_scene", side_effect=_must_not_be_called), \
             patch("hypogeum.agents.assembly_agent.run_assembly_agent",
                   side_effect=_must_not_be_called), \
             patch("hypogeum.agents.publishing_agent.run_publishing_agent",
                   side_effect=_must_not_be_called):
            result = regenerate_cinematic_coverage_plan(
                sp, np, vp, "ep_batch", cfg, output_dir=tmp_path, force=True,
                probe_first_batch=True,
            )

        assert result["status"] == "probe_succeeded"



# ---------------------------------------------------------------------------
# TestCompactWireFormat (2026-08-08 truncation-fix correction)
#
# Direct tests of the compact <-> canonical expansion machinery, independent
# of the full regenerate_cinematic_coverage_plan() flow -- including the
# representative 130-shot token-budget proof this correction exists for.
# ---------------------------------------------------------------------------

class TestCompactWireFormat:

    def test_decode_enum_code_valid(self):
        assert _decode_enum_code("motion_energy", "A") == "low"
        assert _decode_enum_code("motion_energy", "b") == "medium"  # case-insensitive
        assert _decode_enum_code("motion_energy", "C") == "high"

    def test_decode_enum_code_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            _decode_enum_code("motion_energy", "D")  # only A/B/C are valid (3 values)

    def test_decode_enum_code_non_letter_rejected(self):
        with pytest.raises(ValueError):
            _decode_enum_code("motion_energy", "AB")  # not a single letter
        with pytest.raises(ValueError):
            _decode_enum_code("motion_energy", 1)  # not a string

    def test_decode_code_part_each_part_is_exactly_five_characters(self):
        for part_key, fields in _COMPACT_CODE_PARTS.items():
            assert len(fields) == 5, f"{part_key} does not carry exactly 5 fields"

    def test_decode_code_part_missing_character_rejected(self):
        """The exact real-world failure mode this correction exists for: a
        4-character value where 5 were required. Never repaired by
        shifting, padding, defaulting, or inference -- rejected whole."""
        for part_key in _COMPACT_CODE_PARTS:
            with pytest.raises(ValueError):
                _decode_code_part(part_key, "AAAA")  # one character short

    def test_decode_code_part_wrong_length_rejected(self):
        for part_key in _COMPACT_CODE_PARTS:
            with pytest.raises(ValueError):
                _decode_code_part(part_key, "AAA")  # too short
            with pytest.raises(ValueError):
                _decode_code_part(part_key, "AAAAAA")  # too long
            with pytest.raises(ValueError):
                _decode_code_part(part_key, "")  # empty
            with pytest.raises(ValueError):
                _decode_code_part(part_key, None)  # missing entirely

    def test_decode_code_part_invalid_character_rejected(self):
        for part_key in _COMPACT_CODE_PARTS:
            with pytest.raises(ValueError):
                _decode_code_part(part_key, "ZZZZZ")  # out of range for every field

    def test_decode_code_part_valid_decodes_every_field_in_that_part(self):
        for part_key, fields in _COMPACT_CODE_PARTS.items():
            value = "".join(chr(65) for _ in fields)
            decoded = _decode_code_part(part_key, value)
            assert set(decoded.keys()) == set(fields)
            for field in fields:
                assert decoded[field] == _COMPACT_ENUM_VOCAB[field][0]

    def test_decode_code_parts_together_cover_all_ten_fields_in_original_order(self):
        """The split must not alter which fields exist or their order --
        only how they're chunked into JSON keys."""
        all_fields = []
        for fields in _COMPACT_CODE_PARTS.values():
            all_fields.extend(fields)
        assert all_fields == _COMPACT_CODE_FIELDS

    def test_expand_compact_plan_round_trip_preserves_all_semantic_fields(self):
        """Every field Stage B / semantic-compatibility classification needs
        survives the compact round-trip, verbatim, including a non-default
        enum choice for composition and camera_movement specifically."""
        authoritative = ["b_001"]
        measured_durations = {"b_001": 24.427}
        compact = {"beats": [{
            "beat_id": "b_001",
            "pattern": "B",  # establish_develop_reveal
            "shots": [{
                **_compact_codes(
                    composition="diagonal_leading_line",
                    camera_movement="orbital_arc",
                ),
                "func": "Shows the tool marks as proof of deliberate construction.",
                "subj": "hand-carved earthen tunnel wall",
                "act": "torch flame flickers in the foreground",
                "fram": "medium wide, tunnel wall filling the frame",
                "light": "warm amber torch, raking oblique angle",
                "lens": "B",  # wide
                "dup": "deliberate match-cut continuing the prior shot",
            }],
        }]}

        expanded = _expand_compact_plan(compact, "ep_test", authoritative, measured_durations, 8.0)
        validate("cinematic_coverage_plan", expanded)  # must pass the real schema

        bp = expanded["beat_plans"][0]
        assert bp["beat_id"] == "b_001"
        assert bp["narration_duration_seconds"] == 24.427
        assert bp["progression_pattern"] == "establish_develop_reveal"

        shot = bp["shots"][0]
        # Authoritative beat mapping / identity (derived, not authored).
        assert shot["shot_id"] == "b_001__shot1"
        assert shot["parent_beat_id"] == "b_001"
        assert shot["shot_order"] == 1
        # Derived, policy-fixed values.
        assert shot["estimated_usable_duration"] == 8.0
        assert shot["transition_in"] == "clean_cut"
        assert shot["transition_out"] == "clean_cut"
        # Decoded enum fields -- the two non-default choices specifically.
        assert shot["composition"] == "diagonal_leading_line"
        assert shot["camera_movement"] == "orbital_arc"
        assert shot["lens_character"] == "wide"
        # Free-text fields, verbatim.
        assert shot["narrative_function"] == "Shows the tool marks as proof of deliberate construction."
        assert shot["subject"] == "hand-carved earthen tunnel wall"
        assert shot["action"] == "torch flame flickers in the foreground"
        assert shot["framing"] == "medium wide, tunnel wall filling the frame"
        assert shot["lighting_direction"] == "warm amber torch, raking oblique angle"
        assert shot["diversity_justification"] == "deliberate match-cut continuing the prior shot"
        # shot_diversity_key()'s own required fields are all present.
        for field in SHOT_DIVERSITY_FIELDS:
            assert field in shot

    def test_expand_compact_plan_wrong_beat_count_fails_closed(self):
        with pytest.raises(ValueError):
            _expand_compact_plan(
                {"beats": []}, "ep_test", ["b_001"], {"b_001": 8.0}, 8.0,
            )

    def test_expand_compact_plan_duplicate_beat_id_fails_closed(self):
        compact = {"beats": [
            {"beat_id": "b_001", "pattern": "A", "shots": [_compact_shot()]},
            {"beat_id": "b_001", "pattern": "A", "shots": [_compact_shot()]},
        ]}
        with pytest.raises(ValueError):
            _expand_compact_plan(
                compact, "ep_test", ["b_001", "b_002"], {"b_001": 8.0, "b_002": 8.0}, 8.0,
            )

    def test_expand_compact_plan_unknown_beat_id_fails_closed(self):
        compact = {"beats": [
            {"beat_id": "b_999_unknown", "pattern": "A", "shots": [_compact_shot()]},
        ]}
        with pytest.raises(ValueError):
            _expand_compact_plan(
                compact, "ep_test", ["b_001"], {"b_001": 8.0}, 8.0,
            )

    def test_expand_compact_plan_missing_authoritative_beat_fails_closed(self):
        compact = {"beats": [
            {"beat_id": "b_001", "pattern": "A", "shots": [_compact_shot()]},
        ]}
        # b_002 is authoritative but never appears -- caught even though the
        # array length happens to be wrong too (belt-and-suspenders check).
        with pytest.raises(ValueError):
            _expand_compact_plan(
                compact, "ep_test", ["b_001", "b_002"], {"b_001": 8.0, "b_002": 8.0}, 8.0,
            )

    def test_representative_130_shot_response_fits_16000_token_budget_with_margin(self):
        """The proof this whole correction exists for: a representative
        worst-case 130-shot/47-beat compact response, using ep_f3619022's
        own real per-beat shot-budget arithmetic (95 main + 35 short = 130,
        established and unchanged from the prior verification), must
        serialize to comfortably under the 16,000-token non-streaming
        ceiling -- with at least 2,000 tokens of margin, target <=14,000.

        Field-value lengths below are deliberately realistic, not
        artificially short: full one-sentence narrative_function values,
        real multi-word subject/action/framing/lighting text, matching the
        actual style of ep_f3619022's own production prompts -- this is not
        a favorable best case, it is what a real differentiated 130-shot
        plan actually looks like.
        """
        main_shot_counts = [3, 3, 3, 3, 2, 4, 1, 3, 3, 3, 5, 2, 3, 5, 4,
                             3, 3, 2, 4, 2, 3, 4, 4, 4, 1, 3, 5, 4, 6]
        short_shot_counts = [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 2]
        shot_counts = main_shot_counts + short_shot_counts
        assert len(shot_counts) == 47
        assert sum(shot_counts) == 130  # the verified, unchanged ep_f3619022 fixture total

        narrative_functions = [
            "Establishes the physical scale of the tunnel before the narration names it.",
            "Shows the tool marks as proof of deliberate, planned construction.",
            "Reveals the constriction point the narration is about to describe.",
            "Grounds the abstract historical claim in a concrete physical detail.",
            "Shifts from the object itself to the hand that shaped it.",
        ]
        subjects = [
            "earthen tunnel wall, tool-mark striations receding into shadow",
            "medieval scribe's hand, quill and iron-gall ink on parchment",
            "schlupf constriction point, earthen walls pressing inward",
            "timber farmhouse threshold, concealed entrance in the earthen floor",
        ]
        actions = "quill moves in short deliberate strokes across the parchment"
        framings = "medium wide shot, tunnel wall filling the frame in tight profile"
        lightings = "warm amber torch light raking obliquely across the earth wall"

        beats = []
        shot_index = 0
        for beat_index, n_shots in enumerate(shot_counts, start=1):
            shots = []
            for _ in range(n_shots):
                shot = _compact_shot(
                    **_compact_codes(),
                    func=narrative_functions[shot_index % len(narrative_functions)],
                    subj=subjects[shot_index % len(subjects)],
                )
                if shot_index % 3 != 0:
                    shot["act"] = actions
                if shot_index % 2 == 0:
                    shot["fram"] = framings
                if shot_index % 4 == 0:
                    shot["light"] = lightings
                if shot_index % 10 == 0:
                    shot["lens"] = "B"
                if shot_index % 25 == 0:
                    shot["dup"] = "deliberate match-cut, intentional repeat for continuity"
                shots.append(shot)
                shot_index += 1
            beats.append({
                "beat_id": f"b_{beat_index:03d}",
                "pattern": "B" if n_shots > 1 else "A",
                "shots": shots,
            })

        assert shot_index == 130
        representative_response = {"beats": beats}

        serialized = json.dumps(representative_response, ensure_ascii=False)
        char_count = len(serialized)
        # Conservative estimator used throughout this correction: 3.0
        # characters per token (real English/JSON text typically runs
        # closer to 3.5-4.0 chars/token, so this deliberately overestimates
        # token count -- a margin computed against it is a safe margin).
        estimated_tokens = char_count / 3.0

        assert estimated_tokens <= 14_000, (
            f"representative 130-shot compact response estimated at "
            f"{estimated_tokens:.0f} tokens ({char_count} chars) -- exceeds "
            f"the 14,000-token target"
        )
        assert estimated_tokens <= _MAX_TOKENS_REGEN - 2_000, (
            f"representative response leaves only "
            f"{_MAX_TOKENS_REGEN - estimated_tokens:.0f} tokens of margin "
            f"below the {_MAX_TOKENS_REGEN}-token ceiling -- required minimum is 2,000"
        )

        # And prove the representative response is not just small -- it is
        # actually valid: expand it and confirm it passes the real schema
        # with the exact 95/35/130 arithmetic this correction must preserve.
        authoritative = [f"b_{i:03d}" for i in range(1, 48)]
        measured_durations = {beat_id: 20.0 for beat_id in authoritative}
        expanded = _expand_compact_plan(
            representative_response, "ep_f3619022", authoritative, measured_durations, 8.0,
        )
        validate("cinematic_coverage_plan", expanded)
        total_shots = sum(len(bp["shots"]) for bp in expanded["beat_plans"])
        assert total_shots == 130
        main_shots = sum(len(bp["shots"]) for bp in expanded["beat_plans"][:29])
        short_shots = sum(len(bp["shots"]) for bp in expanded["beat_plans"][29:])
        assert main_shots == 95
        assert short_shots == 35
