"""
tests/test_production_plan.py — Unit tests for hypogeum/production_plan.py

Covers:
  TestCompileProductionPlan  — deliverable derivation, asset enumeration,
                                required vs contingency roles, cost totals.
  TestContentHash             — deterministic, input-order-independent,
                                excludes compiled_at.
  TestValidatePlanCoverage    — per-beat/per-deliverable pass/fail, never
                                aggregate-duration-only.
  TestPersistence              — write-once, immutable, idempotent on an
                                identical replay.
  TestContingencyNeverExceedsPlannedShots — the planning-level half of the
                                duplicate-prompt invariant: a Production
                                Plan can never declare more contingency
                                assets for a beat than the Cinematic
                                Coverage Plan actually supplied distinct
                                shots for.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.production_plan import (
    MAIN_DELIVERABLE_ID,
    ROLE_CONTINGENCY,
    ROLE_REQUIRED,
    ProductionPlanImmutabilityError,
    compile_production_plan,
    load_production_plan,
    normalize_prompt,
    persist_production_plan,
    prompt_fingerprint,
    validate_plan_coverage,
)


def _make_config(**overrides) -> SimpleNamespace:
    base = dict(
        MUAPI_DEFAULT_TIER=2,
        MUAPI_T1_MODEL="veo3-text-to-video",
        MUAPI_T2_MODEL="veo3.1-lite-text-to-video",
        MUAPI_T3_MODEL="veo3.1-lite-text-to-video",
        COVERAGE_CONSERVATIVE_DURATION_FACTOR=0.95,
        ENHANCEMENT_UNIT_PRICE_USD=0.09,
        COVERAGE_FILL_MAX_SLOT_RETRIES=1,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _shot(beat_id: str, order: int, **overrides) -> dict:
    shot = {
        "shot_id": f"{beat_id}__shot{order}", "parent_beat_id": beat_id, "shot_order": order,
        "primary_prompt": f"A distinct prompt for {beat_id} shot {order}.",
        "camera_angle": "eye_level", "shot_scale": "medium", "camera_height": "eye_level",
        "camera_direction": "frontal", "camera_movement": "slow_push_in",
        "composition": "rule_of_thirds_left", "background_depth": "shallow",
        "screen_direction": "neutral", "motion_energy": "low",
    }
    shot.update(overrides)
    return shot


def _make_script_package() -> dict:
    return {
        "sections": [{
            "section_id": "s1",
            "narration_beats": [{"beat_id": "b_001", "text": "one"}, {"beat_id": "b_002", "text": "two"}],
        }],
        "shorts_scripts": [{
            "short_id": "short_001",
            "narration_beats": [{"beat_id": "b_s01", "text": "three"}],
        }],
    }


def _make_narration_package() -> dict:
    return {"narration_segments": [
        {"beat_reference": "b_001", "duration_seconds": 8.0},
        {"beat_reference": "b_002", "duration_seconds": 30.0},
        {"beat_reference": "b_s01", "duration_seconds": 8.0},
    ]}


def _make_cinematic_plan(*, b_002_shots: int = 3) -> dict:
    return {
        "episode_id": "ep_test",
        "beat_plans": [
            {"beat_id": "b_001", "shots": [_shot("b_001", 1)]},
            {"beat_id": "b_002", "shots": [_shot("b_002", i) for i in range(1, b_002_shots + 1)]},
            {"beat_id": "b_s01", "shots": [_shot("b_s01", 1)]},
        ],
    }


def _compile(**overrides) -> "ProductionPlan":  # noqa: F821
    cfg = _make_config()
    return compile_production_plan(
        script_package=_make_script_package(),
        narration_package=_make_narration_package(),
        cinematic_coverage_plan=_make_cinematic_plan(),
        channel_id="hypogeum", episode_id="ep_test", config=cfg,
        **overrides,
    )


# ---------------------------------------------------------------------------
# TestCompileProductionPlan
# ---------------------------------------------------------------------------

class TestCompileProductionPlan:

    def test_deliverable_shape_is_one_main_plus_shorts_from_real_script_package(self):
        plan = _compile()
        assert plan.deliverable_ids == [MAIN_DELIVERABLE_ID, "short_001"]

    def test_one_required_asset_per_beat(self):
        plan = _compile()
        required = [a for a in plan.assets if a.role == ROLE_REQUIRED]
        assert {a.beat_id for a in required} == {"b_001", "b_002", "b_s01"}
        assert len(required) == 3

    def test_contingency_assets_only_for_beats_with_extra_planned_shots(self):
        plan = _compile()
        contingency = [a for a in plan.assets if a.role == ROLE_CONTINGENCY]
        assert {a.beat_id for a in contingency} == {"b_002"}
        assert len(contingency) == 2  # b_002 has 3 shots -> 1 required + 2 contingency

    def test_contingency_asset_ids_match_existing_extraN_convention(self):
        """Generic Factory Core interoperability: contingency asset_ids must
        match the naming convention hypogeum.muapi.submit_scene()'s resume
        safety and hypogeum.agents.clip_repair_agent already use
        ({beat_id}__extraN), so a declared contingency asset that a real
        provider call later fulfills is discoverable by the same file-exists
        reuse check every other stage already relies on."""
        plan = _compile()
        contingency_ids = sorted(a.asset_id for a in plan.assets if a.role == ROLE_CONTINGENCY)
        assert contingency_ids == ["b_002__extra1", "b_002__extra2"]

    def test_required_asset_id_equals_beat_id(self):
        plan = _compile()
        required = {a.beat_id: a.asset_id for a in plan.assets if a.role == ROLE_REQUIRED}
        assert required == {"b_001": "b_001", "b_002": "b_002", "b_s01": "b_s01"}

    def test_short_beats_get_portrait_aspect_ratio(self):
        plan = _compile()
        by_beat = {a.beat_id: a.aspect_ratio for a in plan.assets if a.role == ROLE_REQUIRED}
        assert by_beat["b_s01"] == "9:16"
        assert by_beat["b_001"] == "16:9"

    def test_every_asset_has_a_distinct_prompt_fingerprint_within_a_beat(self):
        plan = _compile()
        by_beat: dict = {}
        for a in plan.assets:
            by_beat.setdefault(a.beat_id, []).append(a.prompt_fingerprint)
        for beat_id, fps in by_beat.items():
            assert len(set(fps)) == len(fps), f"duplicate prompt fingerprint within beat {beat_id}"

    def test_normal_cost_counts_only_required_assets_plus_thumbnails(self):
        plan = _compile()
        required_cost = sum(a.unit_price_usd + a.enhancement_price_usd for a in plan.assets if a.role == ROLE_REQUIRED)
        thumbnail_cost = plan.thumbnail_count * plan.thumbnail_unit_price_usd
        assert plan.normal_expected_cost_usd == pytest.approx(required_cost + thumbnail_cost, abs=0.0001)

    def test_maximum_bounded_cost_counts_every_declared_asset(self):
        plan = _compile()
        total = sum(a.unit_price_usd + a.enhancement_price_usd for a in plan.assets)
        thumbnail_cost = plan.thumbnail_count * plan.thumbnail_unit_price_usd
        assert plan.maximum_bounded_cost_usd == pytest.approx(total + thumbnail_cost, abs=0.0001)

    def test_maximum_bounded_cost_strictly_exceeds_normal_when_contingency_exists(self):
        plan = _compile()
        assert plan.maximum_bounded_cost_usd > plan.normal_expected_cost_usd

    def test_thumbnail_counted_once_per_deliverable_not_per_beat(self):
        plan = _compile()
        assert plan.thumbnail_count == len(plan.deliverable_ids) == 2

    def test_conservative_duration_is_strictly_below_requested_duration(self):
        plan = _compile()
        for a in plan.assets:
            assert a.conservative_usable_duration_seconds < a.requested_duration_seconds


# ---------------------------------------------------------------------------
# TestContingencyNeverExceedsPlannedShots — planning-level duplicate-prompt
# prevention: a Production Plan literally cannot declare a contingency
# asset for a beat whose Cinematic Coverage Plan never supplied a distinct
# extra shot for it.
# ---------------------------------------------------------------------------

class TestContingencyNeverExceedsPlannedShots:

    def test_single_shot_beat_declares_zero_contingency_assets(self):
        """The exact ep_af078f34 shape: a beat whose plan has only one
        shot must never get a contingency asset declared for it -- there
        is no second distinct prompt to declare one from."""
        cfg = _make_config()
        plan = compile_production_plan(
            script_package=_make_script_package(),
            narration_package=_make_narration_package(),
            cinematic_coverage_plan=_make_cinematic_plan(b_002_shots=1),
            channel_id="hypogeum", episode_id="ep_test", config=cfg,
        )
        contingency = [a for a in plan.assets if a.role == ROLE_CONTINGENCY]
        assert contingency == []

    def test_contingency_count_exactly_matches_extra_planned_shots(self):
        for n_shots in (1, 2, 4, 6):
            cfg = _make_config()
            plan = compile_production_plan(
                script_package=_make_script_package(),
                narration_package=_make_narration_package(),
                cinematic_coverage_plan=_make_cinematic_plan(b_002_shots=n_shots),
                channel_id="hypogeum", episode_id="ep_test", config=cfg,
            )
            contingency = [a for a in plan.assets if a.beat_id == "b_002" and a.role == ROLE_CONTINGENCY]
            assert len(contingency) == n_shots - 1


# ---------------------------------------------------------------------------
# TestContentHash
# ---------------------------------------------------------------------------

class TestContentHash:

    def test_identical_inputs_produce_identical_hash(self):
        plan_a = _compile()
        plan_b = _compile()
        assert plan_a.content_hash == plan_b.content_hash

    def test_different_prompt_changes_hash(self):
        cfg = _make_config()
        cinematic_plan = _make_cinematic_plan()
        cinematic_plan["beat_plans"][0]["shots"][0]["primary_prompt"] = "a totally different prompt"
        plan_a = _compile()
        plan_b = compile_production_plan(
            script_package=_make_script_package(), narration_package=_make_narration_package(),
            cinematic_coverage_plan=cinematic_plan, channel_id="hypogeum", episode_id="ep_test", config=cfg,
        )
        assert plan_a.content_hash != plan_b.content_hash

    def test_hash_excludes_compiled_at_timestamp(self):
        plan_a = _compile()
        import time
        time.sleep(0.01)
        plan_b = _compile()
        assert plan_a.compiled_at != plan_b.compiled_at
        assert plan_a.content_hash == plan_b.content_hash


# ---------------------------------------------------------------------------
# TestValidatePlanCoverage
# ---------------------------------------------------------------------------

class TestValidatePlanCoverage:

    def test_sufficient_beats_all_pass(self):
        cfg = _make_config()
        plan = compile_production_plan(
            script_package=_make_script_package(), narration_package=_make_narration_package(),
            cinematic_coverage_plan=_make_cinematic_plan(b_002_shots=5),  # enough shots to cover 30s
            channel_id="hypogeum", episode_id="ep_test", config=cfg,
        )
        result = validate_plan_coverage(plan, _make_narration_package())
        assert result.sufficient
        assert result.failing_beats == []

    def test_insufficient_beat_reported_even_with_all_contingency_counted(self):
        """The exact ep_af078f34 defect, caught before Finance/payment: a
        long beat whose plan only ever supplies 1 shot cannot be closed by
        contingency that does not exist."""
        cfg = _make_config()
        plan = compile_production_plan(
            script_package=_make_script_package(), narration_package=_make_narration_package(),
            cinematic_coverage_plan=_make_cinematic_plan(b_002_shots=1),
            channel_id="hypogeum", episode_id="ep_test", config=cfg,
        )
        result = validate_plan_coverage(plan, _make_narration_package())
        assert not result.sufficient
        assert [b["beat_id"] for b in result.failing_beats] == ["b_002"]

    def test_evaluation_is_per_beat_never_aggregate_episode_duration(self):
        """One badly-short beat must fail even though the episode's TOTAL
        available duration across all beats is generously sufficient --
        proof the check is per-beat/per-deliverable, never a single
        episode-wide sum (the exact mistake that would have hidden
        ep_af078f34's defect)."""
        cfg = _make_config()
        # b_001 and b_s01 each get a huge surplus of shots; b_002 (the long
        # beat) still only gets 1 -- aggregate duration is enormous, but
        # b_002 alone must still fail.
        cinematic_plan = {
            "episode_id": "ep_test",
            "beat_plans": [
                {"beat_id": "b_001", "shots": [_shot("b_001", i) for i in range(1, 9)]},
                {"beat_id": "b_002", "shots": [_shot("b_002", 1)]},
                {"beat_id": "b_s01", "shots": [_shot("b_s01", i) for i in range(1, 9)]},
            ],
        }
        plan = compile_production_plan(
            script_package=_make_script_package(), narration_package=_make_narration_package(),
            cinematic_coverage_plan=cinematic_plan,
            channel_id="hypogeum", episode_id="ep_test", config=cfg,
        )
        result = validate_plan_coverage(plan, _make_narration_package())
        assert not result.sufficient
        assert [b["beat_id"] for b in result.failing_beats] == ["b_002"]

    def test_reports_both_deliverables_independently(self):
        cfg = _make_config()
        plan = compile_production_plan(
            script_package=_make_script_package(), narration_package=_make_narration_package(),
            cinematic_coverage_plan=_make_cinematic_plan(b_002_shots=1),
            channel_id="hypogeum", episode_id="ep_test", config=cfg,
        )
        result = validate_plan_coverage(plan, _make_narration_package())
        deliverables = {r["deliverable_id"] for r in result.beat_reports}
        assert deliverables == {MAIN_DELIVERABLE_ID, "short_001"}


# ---------------------------------------------------------------------------
# TestPersistence
# ---------------------------------------------------------------------------

class TestPersistence:

    def test_writes_plan_to_disk(self, tmp_path):
        plan = _compile()
        path = persist_production_plan(plan, tmp_path)
        assert path.exists()
        on_disk = json.loads(path.read_text())
        assert on_disk["content_hash"] == plan.content_hash

    def test_identical_replay_is_a_noop(self, tmp_path):
        plan = _compile()
        path_a = persist_production_plan(plan, tmp_path)
        mtime_a = path_a.stat().st_mtime_ns
        path_b = persist_production_plan(plan, tmp_path)
        assert path_a == path_b
        assert path_b.stat().st_mtime_ns == mtime_a  # never rewritten

    def test_different_plan_for_same_episode_is_refused(self, tmp_path):
        plan_a = _compile()
        persist_production_plan(plan_a, tmp_path)

        cfg = _make_config()
        cinematic_plan = _make_cinematic_plan()
        cinematic_plan["beat_plans"][0]["shots"][0]["primary_prompt"] = "a different prompt entirely"
        plan_b = compile_production_plan(
            script_package=_make_script_package(), narration_package=_make_narration_package(),
            cinematic_coverage_plan=cinematic_plan, channel_id="hypogeum", episode_id="ep_test", config=cfg,
        )
        with pytest.raises(ProductionPlanImmutabilityError):
            persist_production_plan(plan_b, tmp_path)

    def test_load_returns_none_when_absent(self, tmp_path):
        assert load_production_plan("ep_missing", tmp_path) is None

    def test_load_round_trips_the_persisted_plan(self, tmp_path):
        plan = _compile()
        persist_production_plan(plan, tmp_path)
        loaded = load_production_plan("ep_test", tmp_path)
        assert loaded.content_hash == plan.content_hash
        assert len(loaded.assets) == len(plan.assets)


# ---------------------------------------------------------------------------
# TestPromptNormalization
# ---------------------------------------------------------------------------

class TestPromptNormalization:

    def test_whitespace_and_case_normalized(self):
        assert normalize_prompt("  Hello   World  ") == "hello world"

    def test_empty_prompt_normalizes_to_empty(self):
        assert normalize_prompt("") == ""
        assert normalize_prompt(None) == ""

    def test_fingerprint_is_stable_for_equivalent_prompts(self):
        assert prompt_fingerprint("Hello World") == prompt_fingerprint("  hello   world  ")

    def test_fingerprint_differs_for_different_prompts(self):
        assert prompt_fingerprint("a scene") != prompt_fingerprint("a different scene")
