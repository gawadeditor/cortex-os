"""
tests/test_backward_planner.py — Deterministic Backward Production Planner tests

Coverage:
    TestValidCalculation      — timezone-aware, exact stage order, no overlap,
                                 exact target-time preservation, retry/buffer math
    TestPlatformAwareness     — blocked platforms excluded, enabled-only duration
    TestRefusals              — publishing/operator/production disabled behavior
    TestInputValidation       — invalid timezone/timestamp, missing/non-positive duration
    TestFeasibility           — infeasible target, earliest feasible publish time
    TestConcurrency           — max_concurrent_episodes represented
    TestDeterminism           — identical input/output, no randomness
    TestPersistence           — JSON serialization round-trip
    TestProfileUnchanged      — ChannelOperationsProfile never mutated
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from hypogeum.backward_planner import (
    DEFAULT_STAGE_DURATIONS_SECONDS,
    PIPELINE_STAGE_ORDER,
    BackwardPlan,
    build_backward_plan,
    load_backward_plan,
    save_backward_plan,
)
from hypogeum.channel_config import ChannelOperationsProfile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_profile(**overrides) -> ChannelOperationsProfile:
    base = dict(
        channel_id="hypogeum",
        channel_name="The Hypogeum",
        timezone="Asia/Dubai",
        operator_enabled=True,
        production_cadence={"videos_per_week": 1},
        publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
        enabled_platforms={"youtube": True, "facebook": False, "tiktok": False, "instagram": False},
        blocked_platform_reasons={
            "facebook": "Meta app permission/configuration",
            "tiktok": "upload-init endpoint HTTP 404",
            "instagram": "public staging URL not configured",
        },
        discovery_enabled=True,
        production_enabled=True,
        publishing_enabled=True,
        analytics_sync_enabled=True,
        unattended_operation_enabled=False,
        max_concurrent_episodes=1,
        retry_policy={"max_retries_per_stage": 1, "backoff_seconds": 60},
        failure_halt_policy={
            "halt_on_production_failure": True,
            "halt_on_validation_failure": True,
            "halt_on_enabled_platform_publish_failure": True,
            "blocked_or_disabled_platforms_do_not_halt": True,
        },
        cost_ceiling_usd_per_episode=None,
        default_privacy_state="private",
        created_at="2026-07-20T00:00:00+00:00",
        updated_at="2026-07-20T00:00:00+00:00",
    )
    base.update(overrides)
    return ChannelOperationsProfile(**base)


_TARGET = datetime(2026, 9, 3, 18, 0, tzinfo=ZoneInfo("Asia/Dubai"))  # a Thursday, far enough out
_GENERATED = datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)


# ===========================================================================
# TestValidCalculation
# ===========================================================================

class TestValidCalculation:
    def test_valid_timezone_aware_calculation_succeeds(self):
        plan = build_backward_plan("hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED)
        assert plan.plan_status == "executable"
        assert plan.timezone == "Asia/Dubai"

    def test_exact_stage_order_matches_pipeline_stage_order(self):
        plan = build_backward_plan("hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED)
        assert [s.stage_name for s in plan.stages] == list(PIPELINE_STAGE_ORDER)
        for i, s in enumerate(plan.stages):
            assert s.sequence_index == i

    def test_depends_on_chains_sequentially(self):
        plan = build_backward_plan("hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED)
        assert plan.stages[0].depends_on is None
        for i in range(1, len(plan.stages)):
            assert plan.stages[i].depends_on == plan.stages[i - 1].stage_name

    def test_no_accidental_stage_overlap(self):
        """Each stage's finish must equal the next stage's start exactly — no gap, no overlap."""
        plan = build_backward_plan("hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED)
        for i in range(len(plan.stages) - 1):
            assert plan.stages[i].planned_finish_at == plan.stages[i + 1].planned_start_at

    def test_all_stages_critical_in_v1(self):
        plan = build_backward_plan("hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED)
        assert all(s.critical for s in plan.stages)

    def test_target_publish_time_preserved_exactly(self):
        """The last stage's finish + the global safety buffer must reconstruct target_publish_at exactly."""
        plan = build_backward_plan(
            "hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED, safety_buffer_seconds=3600,
        )
        last_finish = datetime.fromisoformat(plan.stages[-1].planned_finish_at)
        assert last_finish + timedelta(seconds=3600) == _TARGET.astimezone(ZoneInfo("Asia/Dubai"))
        assert plan.target_publish_at == _TARGET.astimezone(ZoneInfo("Asia/Dubai")).isoformat()

    def test_retry_allowance_derived_from_retry_policy(self):
        profile = _make_profile(retry_policy={"max_retries_per_stage": 2, "backoff_seconds": 30})
        plan = build_backward_plan("hypogeum", _TARGET, profile, generated_at=_GENERATED)
        research_stage = next(s for s in plan.stages if s.stage_name == "research_director")
        expected_retry = 2 * (DEFAULT_STAGE_DURATIONS_SECONDS["research_director"] + 30)
        assert research_stage.retry_allowance_seconds == expected_retry

    def test_zero_retries_means_zero_retry_allowance(self):
        profile = _make_profile(retry_policy={"max_retries_per_stage": 0, "backoff_seconds": 60})
        plan = build_backward_plan("hypogeum", _TARGET, profile, generated_at=_GENERATED)
        assert all(s.retry_allowance_seconds == 0 for s in plan.stages)

    def test_safety_buffers_included_per_stage(self):
        plan = build_backward_plan("hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED)
        script_stage = next(s for s in plan.stages if s.stage_name == "script_engine")
        expected_buffer = round(DEFAULT_STAGE_DURATIONS_SECONDS["script_engine"] * 0.10)
        assert script_stage.safety_buffer_seconds == expected_buffer


# ===========================================================================
# TestPlatformAwareness
# ===========================================================================

class TestPlatformAwareness:
    def test_youtube_only_publishing_duration(self):
        plan = build_backward_plan("hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED)
        pub_stage = next(s for s in plan.stages if s.stage_name == "publishing_agent")
        from hypogeum.backward_planner import PUBLISHING_PREP_DURATION_SECONDS, PER_PLATFORM_UPLOAD_DURATION_SECONDS
        expected = PUBLISHING_PREP_DURATION_SECONDS + PER_PLATFORM_UPLOAD_DURATION_SECONDS["youtube"]
        assert pub_stage.estimated_duration_seconds == expected

    def test_blocked_disabled_platforms_add_zero_duration(self):
        """Facebook/TikTok/Instagram are disabled+blocked — must not add any time."""
        all_enabled_profile = _make_profile(
            enabled_platforms={"youtube": True, "facebook": False, "tiktok": False, "instagram": False},
        )
        youtube_only_plan = build_backward_plan("hypogeum", _TARGET, all_enabled_profile, generated_at=_GENERATED)

        # If facebook/tiktok/instagram were (incorrectly) enabled with no blocked reasons,
        # duration would be strictly larger. Confirm disabled+blocked contributes nothing
        # by comparing against a profile with every platform truly enabled.
        every_enabled_profile = _make_profile(
            enabled_platforms={"youtube": True, "facebook": True, "tiktok": True, "instagram": True},
            blocked_platform_reasons={},
        )
        every_enabled_plan = build_backward_plan("hypogeum", _TARGET, every_enabled_profile, generated_at=_GENERATED)

        yt_only_pub = next(s for s in youtube_only_plan.stages if s.stage_name == "publishing_agent")
        every_pub = next(s for s in every_enabled_plan.stages if s.stage_name == "publishing_agent")
        assert yt_only_pub.estimated_duration_seconds < every_pub.estimated_duration_seconds

    def test_blocked_platforms_never_block_the_plan(self):
        plan = build_backward_plan("hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED)
        assert plan.plan_status == "executable"


# ===========================================================================
# TestRefusals
# ===========================================================================

class TestRefusals:
    def test_publishing_disabled_omits_publishing_stage_but_keeps_production_complete(self):
        profile = _make_profile(publishing_enabled=False)
        plan = build_backward_plan("hypogeum", _TARGET, profile, generated_at=_GENERATED)
        assert plan.plan_status == "executable"
        assert "publishing_agent" not in [s.stage_name for s in plan.stages]
        assert plan.production_complete_at is not None
        assert plan.production_complete_at == plan.stages[-1].planned_finish_at

    def test_operator_disabled_refuses_executable_plan(self):
        profile = _make_profile(operator_enabled=False)
        plan = build_backward_plan("hypogeum", _TARGET, profile, generated_at=_GENERATED)
        assert plan.plan_status == "refused"
        assert plan.stages == []
        assert "operator_enabled" in plan.refusal_reason

    def test_production_disabled_refuses_executable_plan(self):
        profile = _make_profile(production_enabled=False)
        plan = build_backward_plan("hypogeum", _TARGET, profile, generated_at=_GENERATED)
        assert plan.plan_status == "refused"
        assert plan.stages == []
        assert "production_enabled" in plan.refusal_reason

    def test_unattended_disabled_does_not_refuse_planning(self):
        profile = _make_profile(unattended_operation_enabled=False)
        plan = build_backward_plan("hypogeum", _TARGET, profile, generated_at=_GENERATED)
        assert plan.plan_status == "executable"


# ===========================================================================
# TestInputValidation
# ===========================================================================

class TestInputValidation:
    def test_naive_target_publish_at_raises(self):
        naive_target = datetime(2026, 9, 3, 18, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            build_backward_plan("hypogeum", naive_target, _make_profile(), generated_at=_GENERATED)

    def test_naive_generated_at_raises(self):
        naive_generated = datetime(2026, 7, 21, 9, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            build_backward_plan("hypogeum", _TARGET, _make_profile(), generated_at=naive_generated)

    def test_invalid_timezone_raises(self):
        profile = _make_profile(timezone="Mars/Olympus_Mons")
        with pytest.raises(ValueError, match="invalid timezone"):
            build_backward_plan("hypogeum", _TARGET, profile, generated_at=_GENERATED)

    def test_missing_stage_duration_raises(self):
        overrides = {"research_director": None}
        with pytest.raises(ValueError, match="research_director"):
            build_backward_plan(
                "hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED,
                stage_durations=overrides,
            )

    def test_non_positive_stage_duration_raises(self):
        overrides = {"script_engine": 0}
        with pytest.raises(ValueError, match="script_engine"):
            build_backward_plan(
                "hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED,
                stage_durations=overrides,
            )

    def test_negative_stage_duration_raises(self):
        overrides = {"assembly_agent": -100}
        with pytest.raises(ValueError, match="assembly_agent"):
            build_backward_plan(
                "hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED,
                stage_durations=overrides,
            )


# ===========================================================================
# TestFeasibility
# ===========================================================================

class TestFeasibility:
    def test_infeasible_target_returns_explicit_result(self):
        """target_publish_at only 1 hour after generated_at — nowhere near enough lead time."""
        too_close_target = _GENERATED.astimezone(ZoneInfo("Asia/Dubai")) + timedelta(hours=1)
        plan = build_backward_plan("hypogeum", too_close_target, _make_profile(), generated_at=_GENERATED)

        assert plan.plan_status == "infeasible"
        assert plan.required_lead_time_seconds is not None
        assert plan.available_lead_time_seconds is not None
        assert plan.required_lead_time_seconds > plan.available_lead_time_seconds
        assert plan.blocking_stages == [s.stage_name for s in plan.stages]

    def test_durations_never_silently_compressed_when_infeasible(self):
        too_close_target = _GENERATED.astimezone(ZoneInfo("Asia/Dubai")) + timedelta(hours=1)
        plan = build_backward_plan("hypogeum", too_close_target, _make_profile(), generated_at=_GENERATED)
        # Stage durations must be identical to the feasible-case defaults — no shrinking.
        for s in plan.stages:
            if s.stage_name != "publishing_agent":
                assert s.estimated_duration_seconds == DEFAULT_STAGE_DURATIONS_SECONDS[s.stage_name]

    def test_earliest_feasible_publish_time_is_generated_at_plus_required_lead(self):
        too_close_target = _GENERATED.astimezone(ZoneInfo("Asia/Dubai")) + timedelta(hours=1)
        plan = build_backward_plan("hypogeum", too_close_target, _make_profile(), generated_at=_GENERATED)

        earliest = datetime.fromisoformat(plan.earliest_feasible_publish_at)
        generated_local = _GENERATED.astimezone(ZoneInfo("Asia/Dubai"))
        expected = generated_local + timedelta(seconds=plan.required_lead_time_seconds)
        assert earliest == expected

    def test_ample_lead_time_is_feasible(self):
        plan = build_backward_plan("hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED)
        assert plan.plan_status == "executable"
        assert plan.available_lead_time_seconds >= plan.required_lead_time_seconds


# ===========================================================================
# TestConcurrency
# ===========================================================================

class TestConcurrency:
    def test_max_concurrent_episodes_represented_in_plan(self):
        profile = _make_profile(max_concurrent_episodes=1)
        plan = build_backward_plan("hypogeum", _TARGET, profile, generated_at=_GENERATED)
        assert plan.max_concurrent_episodes == 1

    def test_different_max_concurrent_episodes_value_propagates(self):
        profile = _make_profile(max_concurrent_episodes=3)
        plan = build_backward_plan("hypogeum", _TARGET, profile, generated_at=_GENERATED)
        assert plan.max_concurrent_episodes == 3


# ===========================================================================
# TestDeterminism
# ===========================================================================

class TestDeterminism:
    def test_identical_inputs_produce_identical_output(self):
        plan1 = build_backward_plan("hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED)
        plan2 = build_backward_plan("hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED)
        assert plan1.to_dict() == plan2.to_dict()

    def test_identical_inputs_produce_identical_plan_id(self):
        plan1 = build_backward_plan("hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED)
        plan2 = build_backward_plan("hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED)
        assert plan1.plan_id == plan2.plan_id

    def test_different_generated_at_produces_different_plan_id(self):
        plan1 = build_backward_plan("hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED)
        plan2 = build_backward_plan(
            "hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED + timedelta(seconds=1),
        )
        assert plan1.plan_id != plan2.plan_id


# ===========================================================================
# TestPersistence
# ===========================================================================

class TestPersistence:
    def test_json_serialization_round_trip(self, tmp_path):
        plan = build_backward_plan("hypogeum", _TARGET, _make_profile(), generated_at=_GENERATED)
        path = save_backward_plan(plan, "ep_test_backward_plan", project_root=tmp_path)

        assert path.exists()
        raw = json.loads(path.read_text())
        assert raw["plan_id"] == plan.plan_id
        assert len(raw["stages"]) == len(plan.stages)

        loaded = load_backward_plan("ep_test_backward_plan", project_root=tmp_path)
        assert loaded is not None
        assert loaded.to_dict() == plan.to_dict()

    def test_load_missing_plan_returns_none(self, tmp_path):
        assert load_backward_plan("does_not_exist", project_root=tmp_path) is None

    def test_infeasible_plan_round_trips_too(self, tmp_path):
        too_close_target = _GENERATED.astimezone(ZoneInfo("Asia/Dubai")) + timedelta(hours=1)
        plan = build_backward_plan("hypogeum", too_close_target, _make_profile(), generated_at=_GENERATED)
        save_backward_plan(plan, "ep_test_infeasible", project_root=tmp_path)
        loaded = load_backward_plan("ep_test_infeasible", project_root=tmp_path)
        assert loaded.plan_status == "infeasible"
        assert loaded.to_dict() == plan.to_dict()


# ===========================================================================
# TestProfileUnchanged
# ===========================================================================

class TestProfileUnchanged:
    def test_operations_profile_is_never_mutated(self):
        profile = _make_profile()
        before = ChannelOperationsProfile(**{**profile.__dict__})
        build_backward_plan("hypogeum", _TARGET, profile, generated_at=_GENERATED)
        assert profile == before
