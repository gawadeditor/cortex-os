"""
tests/test_provider_model_director.py — Phase F: Provider & Model Director tests

Coverage:
    TestDataContracts                — all 6 dataclasses instantiate correctly
    TestBenchmarkProviders           — empty DB, populated, ranked DESC, success_rate
    TestBenchmarkModels              — empty DB, populated, breadth score, tier
    TestProviderSwapRecommendations  — <2 providers, gap>=0.15, failure_rate trigger, urgency
    TestModelSwapRecommendations     — premium triggers, non-premium ignored, stages
    TestGenerateMigrationPlans       — plan_type, steps, risk levels, rollback
    TestFleetCostEfficiency          — unknown/optimal/suboptimal/critical states
    TestComputeTotalSavings          — empty, single, multiple
    TestRunProviderModelDirector     — full report, top_provider/top_model, all fields
    TestNonBlocking                  — bad DB returns safe defaults
    TestNoNetworkCalls               — socket blocked
    TestNoProductionSideEffects      — no ep_000 references, read-only
"""

from __future__ import annotations

import socket
import uuid
from pathlib import Path

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.provider_model_director import (
    # Dataclasses
    MigrationPlan,
    ModelBenchmark,
    ModelSwapRecommendation,
    ProviderBenchmark,
    ProviderModelDirectorReport,
    ProviderSwapRecommendation,
    # Constants
    FAILURE_RATE_CRITICAL,
    MODEL_ALTERNATIVES,
    MODEL_COST_CEILING,
    MODEL_COST_CEILING_USD,
    MODEL_TIERS,
    MODEL_WEIGHT_BREADTH,
    MODEL_WEIGHT_COST,
    MODEL_WEIGHT_RELIABILITY,
    METADATA_VERSION,
    PROVIDER_COST_CEILING_USD,
    PROVIDER_SPEED_CEILING_MS,
    PROVIDER_WEIGHT_COST,
    PROVIDER_WEIGHT_RELIABILITY,
    PROVIDER_WEIGHT_SPEED,
    SWAP_COMPOSITE_THRESHOLD,
    # Functions
    _compute_fleet_cost_efficiency,
    _compute_total_savings,
    benchmark_models,
    benchmark_providers,
    find_model_swap_recommendations,
    find_provider_swap_recommendations,
    generate_migration_plans,
    run_provider_model_director,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "pmd_test.db"
    initialize_schema(p)
    return p


def _insert_run(conn, run_id="r1", episode_id="ep1"):
    conn.execute(
        "INSERT OR IGNORE INTO runs (run_id, episode_id, started_at, status) "
        "VALUES (?,?,?,?)",
        (run_id, episode_id, "2026-01-01T00:00:00", "complete"),
    )


def _insert_asset(conn, provider, status="succeeded", cost_usd=0.05, duration_ms=2000,
                  asset_type="video_clip", run_id="r1", episode_id="ep1"):
    conn.execute(
        """INSERT INTO asset_versions
           (version_id, asset_slot_id, episode_id, run_id, asset_type, provider,
            status, cost_usd, duration_ms, attempted_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), str(uuid.uuid4()), episode_id, run_id, asset_type, provider,
         status, cost_usd, duration_ms, "2026-01-01T00:00:00"),
    )


def _insert_stage(conn, run_id, episode_id, stage_name, status,
                  skipped=0, duration_ms=1000, cost_usd=0.01, model_slug=None):
    conn.execute(
        """INSERT INTO stage_executions
           (stage_exec_id, run_id, episode_id, stage_name, status,
            skipped, duration_ms, cost_usd, model_slug, started_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), run_id, episode_id, stage_name, status,
         skipped, duration_ms, cost_usd, model_slug, "2026-01-01T00:00:00"),
    )


def _insert_cost(conn, run_id, episode_id, model, cost_usd=0.05):
    conn.execute(
        """INSERT INTO costs
           (cost_id, run_id, episode_id, provider, call_type, model, cost_usd, called_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), run_id, episode_id, "openrouter", "research_synthesis",
         model, cost_usd, "2026-01-01T00:00:00"),
    )


@pytest.fixture()
def populated_db(db_path):
    """
    Two providers: muapi (good) and runway (bad).
    Two models: claude-sonnet-4.6 (cheap) and claude-opus-4.6 (premium).
    """
    conn = get_connection(db_path)
    _insert_run(conn, "r1", "ep1")
    _insert_run(conn, "r2", "ep2")
    _insert_run(conn, "r3", "ep3")
    _insert_run(conn, "r4", "ep4")

    # muapi: 3 success, 1 fail
    for i in range(3):
        _insert_asset(conn, "muapi", "succeeded", 0.10, 3000, run_id="r1")
    _insert_asset(conn, "muapi", "failed", 0.00, 0, run_id="r2")

    # runway: 2 success, 3 fail (high failure rate > 0.20)
    for i in range(2):
        _insert_asset(conn, "runway", "succeeded", 0.50, 5000, run_id="r3")
    for i in range(3):
        _insert_asset(conn, "runway", "failed", 0.00, 0, run_id="r4")

    # claude-sonnet costs — cheap
    for i in range(3):
        _insert_cost(conn, "r1", "ep1", "anthropic/claude-sonnet-4.6", 0.01)
    _insert_stage(conn, "r1", "ep1", "research", "completed", 0, 1000, 0.01,
                  "anthropic/claude-sonnet-4.6")
    _insert_stage(conn, "r1", "ep1", "narration", "completed", 0, 1000, 0.01,
                  "anthropic/claude-sonnet-4.6")

    # claude-opus costs — premium, expensive
    for i in range(3):
        _insert_cost(conn, "r2", "ep2", "anthropic/claude-opus-4.6", 0.15)
    _insert_stage(conn, "r2", "ep2", "research", "completed", 0, 1000, 0.15,
                  "anthropic/claude-opus-4.6")

    conn.commit()
    conn.close()
    return db_path


# ===========================================================================
# TestDataContracts
# ===========================================================================

class TestDataContracts:

    def test_provider_benchmark_fields(self):
        pb = ProviderBenchmark(
            provider="muapi",
            total_attempts=10,
            success_count=9,
            failure_count=1,
            success_rate=0.9,
            avg_cost_usd=0.05,
            avg_duration_ms=2000.0,
            cost_efficiency_score=0.95,
            reliability_score=0.9,
            speed_score=0.967,
            composite_score=0.92,
            rank=1,
        )
        assert pb.provider == "muapi"
        assert pb.rank == 1
        assert pb.composite_score == 0.92

    def test_model_benchmark_fields(self):
        mb = ModelBenchmark(
            model_id="anthropic/claude-sonnet-4.6",
            usage_count=5,
            total_cost_usd=0.25,
            avg_cost_per_call=0.05,
            stage_assignments=["research", "narration"],
            breadth_score=0.5,
            cost_score=0.9,
            reliability_score=0.85,
            composite_score=0.78,
            tier="standard",
            rank=1,
        )
        assert mb.model_id == "anthropic/claude-sonnet-4.6"
        assert mb.tier == "standard"
        assert len(mb.stage_assignments) == 2

    def test_provider_swap_recommendation_fields(self):
        psr = ProviderSwapRecommendation(
            current_provider="runway",
            recommended_provider="muapi",
            asset_types_affected=["video_clip"],
            reason="muapi is better",
            current_composite_score=0.4,
            recommended_composite_score=0.8,
            expected_reliability_improvement=20.0,
            expected_cost_reduction_pct=30.0,
            urgency="urgent",
            migration_steps=["step1", "step2"],
        )
        assert psr.urgency == "urgent"
        assert psr.current_provider == "runway"
        assert len(psr.migration_steps) == 2

    def test_model_swap_recommendation_fields(self):
        msr = ModelSwapRecommendation(
            stage_name="research",
            current_model="anthropic/claude-opus-4.6",
            recommended_model="anthropic/claude-sonnet-4.6",
            reason="too expensive",
            current_tier="premium",
            recommended_tier="standard",
            expected_cost_reduction_pct=35.0,
            urgency="optional",
            migration_steps=["step1"],
        )
        assert msr.stage_name == "research"
        assert msr.current_tier == "premium"
        assert msr.expected_cost_reduction_pct == 35.0

    def test_migration_plan_fields(self):
        mp = MigrationPlan(
            plan_id="provider_migration_01",
            title="Migrate runway → muapi",
            plan_type="provider_swap",
            description="Replace runway with muapi.",
            steps=["step1", "step2", "step3"],
            estimated_cost_reduction_pct=30.0,
            risk_level="medium",
            rollback_strategy="Revert to runway.",
        )
        assert mp.plan_id == "provider_migration_01"
        assert mp.plan_type == "provider_swap"
        assert len(mp.steps) == 3

    def test_report_fields(self):
        report = ProviderModelDirectorReport(
            analyzed_at="2026-01-01T00:00:00+00:00",
            provider_benchmarks=[],
            model_benchmarks=[],
            provider_swap_recommendations=[],
            model_swap_recommendations=[],
            migration_plans=[],
            top_provider=None,
            top_model=None,
            fleet_cost_efficiency="unknown",
            total_estimated_savings_pct=0.0,
        )
        assert report.metadata_version == METADATA_VERSION
        assert report.fleet_cost_efficiency == "unknown"

    def test_report_metadata_version_default(self):
        report = ProviderModelDirectorReport(
            analyzed_at="2026-01-01T00:00:00+00:00",
            provider_benchmarks=[],
            model_benchmarks=[],
            provider_swap_recommendations=[],
            model_swap_recommendations=[],
            migration_plans=[],
            top_provider="muapi",
            top_model="anthropic/claude-sonnet-4.6",
            fleet_cost_efficiency="optimal",
            total_estimated_savings_pct=10.0,
        )
        assert report.metadata_version == "v1"


# ===========================================================================
# TestBenchmarkProviders
# ===========================================================================

class TestBenchmarkProviders:

    def test_empty_db_returns_empty_list(self, db_path):
        result = benchmark_providers(db_path)
        assert result == []

    def test_returns_list_of_provider_benchmarks(self, populated_db):
        result = benchmark_providers(populated_db)
        assert all(isinstance(b, ProviderBenchmark) for b in result)

    def test_two_providers_returned(self, populated_db):
        result = benchmark_providers(populated_db)
        assert len(result) == 2

    def test_sorted_by_composite_desc(self, populated_db):
        result = benchmark_providers(populated_db)
        scores = [b.composite_score for b in result]
        assert scores == sorted(scores, reverse=True)

    def test_rank_assigned_starting_at_1(self, populated_db):
        result = benchmark_providers(populated_db)
        ranks = [b.rank for b in result]
        assert ranks[0] == 1
        assert ranks[1] == 2

    def test_muapi_better_than_runway(self, populated_db):
        result = benchmark_providers(populated_db)
        providers = [b.provider for b in result]
        assert providers[0] == "muapi"

    def test_success_rate_calculation(self, populated_db):
        result = benchmark_providers(populated_db)
        muapi = next(b for b in result if b.provider == "muapi")
        # 3 success / 4 total = 0.75
        assert muapi.success_rate == pytest.approx(0.75, abs=0.01)

    def test_failure_count_populated(self, populated_db):
        result = benchmark_providers(populated_db)
        runway = next(b for b in result if b.provider == "runway")
        assert runway.failure_count == 3

    def test_composite_score_uses_weights(self, populated_db):
        result = benchmark_providers(populated_db)
        for b in result:
            # Manual check: composite = reliability*0.50 + cost*0.30 + speed*0.20
            expected = round(
                b.reliability_score * PROVIDER_WEIGHT_RELIABILITY
                + b.cost_efficiency_score * PROVIDER_WEIGHT_COST
                + b.speed_score * PROVIDER_WEIGHT_SPEED,
                4,
            )
            assert b.composite_score == pytest.approx(expected, abs=0.0001)

    def test_cost_efficiency_score_range(self, populated_db):
        result = benchmark_providers(populated_db)
        for b in result:
            assert 0.0 <= b.cost_efficiency_score <= 1.0

    def test_speed_score_range(self, populated_db):
        result = benchmark_providers(populated_db)
        for b in result:
            assert 0.0 <= b.speed_score <= 1.0

    def test_bad_db_returns_empty_list(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        result = benchmark_providers(bad)
        assert result == []


# ===========================================================================
# TestBenchmarkModels
# ===========================================================================

class TestBenchmarkModels:

    def test_empty_db_returns_empty_list(self, db_path):
        result = benchmark_models(db_path)
        assert result == []

    def test_returns_list_of_model_benchmarks(self, populated_db):
        result = benchmark_models(populated_db)
        assert all(isinstance(b, ModelBenchmark) for b in result)

    def test_two_models_returned(self, populated_db):
        result = benchmark_models(populated_db)
        assert len(result) == 2

    def test_sorted_by_composite_desc(self, populated_db):
        result = benchmark_models(populated_db)
        scores = [b.composite_score for b in result]
        assert scores == sorted(scores, reverse=True)

    def test_rank_assigned_starting_at_1(self, populated_db):
        result = benchmark_models(populated_db)
        assert result[0].rank == 1

    def test_tier_classification_standard(self, populated_db):
        result = benchmark_models(populated_db)
        sonnet = next(b for b in result if "sonnet" in b.model_id)
        assert sonnet.tier == "standard"

    def test_tier_classification_premium(self, populated_db):
        result = benchmark_models(populated_db)
        opus = next(b for b in result if "opus" in b.model_id)
        assert opus.tier == "premium"

    def test_avg_cost_per_call_calculated(self, populated_db):
        result = benchmark_models(populated_db)
        sonnet = next(b for b in result if "sonnet" in b.model_id)
        # 3 calls at 0.01 each → avg = 0.01
        assert sonnet.avg_cost_per_call == pytest.approx(0.01, abs=0.001)

    def test_stage_assignments_populated(self, populated_db):
        result = benchmark_models(populated_db)
        sonnet = next(b for b in result if "sonnet" in b.model_id)
        # sonnet used in research and narration stages
        assert len(sonnet.stage_assignments) >= 1

    def test_breadth_score_normalised_to_1(self, populated_db):
        result = benchmark_models(populated_db)
        for b in result:
            assert 0.0 <= b.breadth_score <= 1.0

    def test_cost_score_range(self, populated_db):
        result = benchmark_models(populated_db)
        for b in result:
            assert 0.0 <= b.cost_score <= 1.0

    def test_unknown_tier_for_unrecognised_model(self, db_path):
        conn = get_connection(db_path)
        _insert_run(conn)
        _insert_cost(conn, "r1", "ep1", "some/unknown-model-xyz", 0.02)
        conn.commit()
        conn.close()
        result = benchmark_models(db_path)
        m = result[0]
        assert m.tier == "unknown"

    def test_bad_db_returns_empty_list(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        result = benchmark_models(bad)
        assert result == []

    def test_model_not_in_stage_executions_gets_empty_stages(self, db_path):
        conn = get_connection(db_path)
        _insert_run(conn)
        _insert_cost(conn, "r1", "ep1", "anthropic/claude-sonnet-4.6", 0.01)
        conn.commit()
        conn.close()
        result = benchmark_models(db_path)
        assert result[0].stage_assignments == []


# ===========================================================================
# TestProviderSwapRecommendations
# ===========================================================================

class TestProviderSwapRecommendations:

    def test_empty_list_when_fewer_than_two_providers(self):
        pb = ProviderBenchmark(
            provider="muapi", total_attempts=5, success_count=5, failure_count=0,
            success_rate=1.0, avg_cost_usd=0.05, avg_duration_ms=2000.0,
            cost_efficiency_score=0.95, reliability_score=1.0, speed_score=0.97,
            composite_score=0.98, rank=1,
        )
        result = find_provider_swap_recommendations([pb])
        assert result == []

    def test_empty_list_when_no_benchmarks(self):
        result = find_provider_swap_recommendations([])
        assert result == []

    def test_no_swap_when_providers_are_close(self):
        good = ProviderBenchmark(
            provider="muapi", total_attempts=10, success_count=9, failure_count=1,
            success_rate=0.9, avg_cost_usd=0.05, avg_duration_ms=2000.0,
            cost_efficiency_score=0.95, reliability_score=0.9, speed_score=0.97,
            composite_score=0.92, rank=1,
        )
        similar = ProviderBenchmark(
            provider="runway", total_attempts=10, success_count=8, failure_count=2,
            success_rate=0.8, avg_cost_usd=0.06, avg_duration_ms=2100.0,
            cost_efficiency_score=0.94, reliability_score=0.8, speed_score=0.965,
            # composite gap < 0.15 and success_rate >= 0.80
            composite_score=0.87, rank=2,
        )
        result = find_provider_swap_recommendations([good, similar])
        assert result == []

    def test_swap_recommended_when_composite_gap_exceeds_threshold(self):
        good = ProviderBenchmark(
            provider="muapi", total_attempts=10, success_count=10, failure_count=0,
            success_rate=1.0, avg_cost_usd=0.05, avg_duration_ms=1000.0,
            cost_efficiency_score=0.95, reliability_score=1.0, speed_score=0.983,
            composite_score=0.98, rank=1,
        )
        bad = ProviderBenchmark(
            provider="runway", total_attempts=10, success_count=7, failure_count=3,
            success_rate=0.7, avg_cost_usd=0.50, avg_duration_ms=10000.0,
            cost_efficiency_score=0.50, reliability_score=0.7, speed_score=0.833,
            composite_score=0.65, rank=2,   # gap = 0.33 > 0.15
        )
        result = find_provider_swap_recommendations([good, bad])
        assert len(result) >= 1
        rec = result[0]
        assert rec.current_provider == "runway"
        assert rec.recommended_provider == "muapi"

    def test_urgent_urgency_when_failure_rate_critical(self):
        good = ProviderBenchmark(
            provider="muapi", total_attempts=10, success_count=10, failure_count=0,
            success_rate=1.0, avg_cost_usd=0.05, avg_duration_ms=1000.0,
            cost_efficiency_score=0.95, reliability_score=1.0, speed_score=0.983,
            composite_score=0.98, rank=1,
        )
        # success_rate=0.7 means failure_rate=0.30 > FAILURE_RATE_CRITICAL(0.20)
        bad = ProviderBenchmark(
            provider="runway", total_attempts=10, success_count=7, failure_count=3,
            success_rate=0.7, avg_cost_usd=0.50, avg_duration_ms=10000.0,
            cost_efficiency_score=0.50, reliability_score=0.7, speed_score=0.833,
            composite_score=0.65, rank=2,
        )
        result = find_provider_swap_recommendations([good, bad])
        assert any(r.urgency == "urgent" for r in result)

    def test_recommended_urgency_for_composite_gap_only(self):
        good = ProviderBenchmark(
            provider="muapi", total_attempts=10, success_count=10, failure_count=0,
            success_rate=1.0, avg_cost_usd=0.05, avg_duration_ms=1000.0,
            cost_efficiency_score=0.95, reliability_score=1.0, speed_score=0.983,
            composite_score=0.95, rank=1,
        )
        # failure_rate = 0.15 < critical; composite gap = 0.25 >= threshold
        ok_ish = ProviderBenchmark(
            provider="runway", total_attempts=20, success_count=17, failure_count=3,
            success_rate=0.85, avg_cost_usd=0.60, avg_duration_ms=5000.0,
            cost_efficiency_score=0.40, reliability_score=0.85, speed_score=0.917,
            composite_score=0.70, rank=2,
        )
        result = find_provider_swap_recommendations([good, ok_ish])
        assert any(r.urgency == "recommended" for r in result)

    def test_migration_steps_are_six(self):
        good = ProviderBenchmark(
            provider="muapi", total_attempts=10, success_count=10, failure_count=0,
            success_rate=1.0, avg_cost_usd=0.05, avg_duration_ms=1000.0,
            cost_efficiency_score=0.95, reliability_score=1.0, speed_score=0.983,
            composite_score=0.98, rank=1,
        )
        bad = ProviderBenchmark(
            provider="runway", total_attempts=10, success_count=6, failure_count=4,
            success_rate=0.6, avg_cost_usd=0.50, avg_duration_ms=10000.0,
            cost_efficiency_score=0.50, reliability_score=0.6, speed_score=0.833,
            composite_score=0.63, rank=2,
        )
        result = find_provider_swap_recommendations([good, bad])
        assert len(result[0].migration_steps) == 6

    def test_asset_types_affected_populated(self):
        good = ProviderBenchmark(
            provider="muapi", total_attempts=10, success_count=10, failure_count=0,
            success_rate=1.0, avg_cost_usd=0.05, avg_duration_ms=1000.0,
            cost_efficiency_score=0.95, reliability_score=1.0, speed_score=0.983,
            composite_score=0.98, rank=1,
        )
        bad = ProviderBenchmark(
            provider="runway", total_attempts=10, success_count=6, failure_count=4,
            success_rate=0.6, avg_cost_usd=0.50, avg_duration_ms=10000.0,
            cost_efficiency_score=0.50, reliability_score=0.6, speed_score=0.833,
            composite_score=0.63, rank=2,
        )
        result = find_provider_swap_recommendations([good, bad])
        assert len(result[0].asset_types_affected) >= 1

    def test_returns_list_of_provider_swap_recommendations(self):
        good = ProviderBenchmark(
            provider="muapi", total_attempts=10, success_count=10, failure_count=0,
            success_rate=1.0, avg_cost_usd=0.05, avg_duration_ms=1000.0,
            cost_efficiency_score=0.95, reliability_score=1.0, speed_score=0.983,
            composite_score=0.98, rank=1,
        )
        bad = ProviderBenchmark(
            provider="runway", total_attempts=10, success_count=6, failure_count=4,
            success_rate=0.6, avg_cost_usd=0.50, avg_duration_ms=10000.0,
            cost_efficiency_score=0.50, reliability_score=0.6, speed_score=0.833,
            composite_score=0.63, rank=2,
        )
        result = find_provider_swap_recommendations([good, bad])
        assert all(isinstance(r, ProviderSwapRecommendation) for r in result)


# ===========================================================================
# TestModelSwapRecommendations
# ===========================================================================

class TestModelSwapRecommendations:

    def _make_model_benchmark(self, model_id, avg_cost=0.05, stage_assignments=None,
                               tier=None, composite=0.7):
        t = tier or MODEL_TIERS.get(model_id, "unknown")
        return ModelBenchmark(
            model_id=model_id,
            usage_count=5,
            total_cost_usd=avg_cost * 5,
            avg_cost_per_call=avg_cost,
            stage_assignments=stage_assignments or ["research"],
            breadth_score=0.5,
            cost_score=max(0.0, 1 - avg_cost / MODEL_COST_CEILING_USD),
            reliability_score=0.85,
            composite_score=composite,
            tier=t,
            rank=1,
        )

    def test_empty_list_when_no_benchmarks(self):
        result = find_model_swap_recommendations([])
        assert result == []

    def test_premium_model_triggers_recommendation(self):
        mb = self._make_model_benchmark("anthropic/claude-opus-4.6", avg_cost=0.15)
        result = find_model_swap_recommendations([mb])
        assert len(result) >= 1

    def test_recommended_model_is_alternative(self):
        mb = self._make_model_benchmark("anthropic/claude-opus-4.6", avg_cost=0.15)
        result = find_model_swap_recommendations([mb])
        assert all(r.recommended_model == "anthropic/claude-sonnet-4.6" for r in result)

    def test_standard_model_not_in_alternatives_ignored(self):
        mb = self._make_model_benchmark("anthropic/claude-sonnet-4.6", avg_cost=0.01)
        result = find_model_swap_recommendations([mb])
        assert result == []

    def test_economy_model_not_in_alternatives_ignored(self):
        mb = self._make_model_benchmark("anthropic/claude-haiku-4.5", avg_cost=0.001)
        result = find_model_swap_recommendations([mb])
        assert result == []

    def test_unknown_model_not_in_alternatives_ignored(self):
        mb = self._make_model_benchmark("some/unknown-model", avg_cost=0.05)
        result = find_model_swap_recommendations([mb])
        assert result == []

    def test_current_tier_is_premium(self):
        mb = self._make_model_benchmark("anthropic/claude-opus-4.6", avg_cost=0.15)
        result = find_model_swap_recommendations([mb])
        assert all(r.current_tier == "premium" for r in result)

    def test_recommended_tier_is_standard(self):
        mb = self._make_model_benchmark("anthropic/claude-opus-4.6", avg_cost=0.15)
        result = find_model_swap_recommendations([mb])
        assert all(r.recommended_tier == "standard" for r in result)

    def test_urgent_when_cost_exceeds_triple_ceiling(self):
        # MODEL_COST_CEILING = 0.10; triple = 0.30
        mb = self._make_model_benchmark("anthropic/claude-opus-4.6", avg_cost=0.35)
        result = find_model_swap_recommendations([mb])
        assert any(r.urgency == "urgent" for r in result)

    def test_optional_when_only_tier_triggers(self):
        # avg_cost below MODEL_COST_CEILING but model is premium tier
        mb = self._make_model_benchmark("anthropic/claude-opus-4.6", avg_cost=0.05)
        result = find_model_swap_recommendations([mb])
        assert any(r.urgency == "optional" for r in result)

    def test_migration_steps_are_six(self):
        mb = self._make_model_benchmark("anthropic/claude-opus-4.6", avg_cost=0.15,
                                        stage_assignments=["research"])
        result = find_model_swap_recommendations([mb])
        assert all(len(r.migration_steps) == 6 for r in result)

    def test_stage_name_in_recommendation(self):
        mb = self._make_model_benchmark("anthropic/claude-opus-4.6", avg_cost=0.15,
                                        stage_assignments=["narration"])
        result = find_model_swap_recommendations([mb])
        stage_names = [r.stage_name for r in result]
        assert "narration" in stage_names

    def test_multiple_stages_produce_one_recommendation_each(self):
        mb = self._make_model_benchmark(
            "anthropic/claude-opus-4.6", avg_cost=0.15,
            stage_assignments=["research", "narration", "thumbnail"],
        )
        result = find_model_swap_recommendations([mb])
        assert len(result) == 3

    def test_expected_cost_reduction_pct_positive(self):
        mb = self._make_model_benchmark("anthropic/claude-opus-4.6", avg_cost=0.15)
        result = find_model_swap_recommendations([mb])
        assert all(r.expected_cost_reduction_pct > 0 for r in result)

    def test_returns_list_of_model_swap_recommendations(self):
        mb = self._make_model_benchmark("anthropic/claude-opus-4.6", avg_cost=0.15)
        result = find_model_swap_recommendations([mb])
        assert all(isinstance(r, ModelSwapRecommendation) for r in result)

    def test_gpt_premium_model_triggers_recommendation(self):
        mb = self._make_model_benchmark("openai/gpt-5.5-pro", avg_cost=0.20)
        result = find_model_swap_recommendations([mb])
        assert len(result) >= 1
        assert all(r.current_model == "openai/gpt-5.5-pro" for r in result)


# ===========================================================================
# TestGenerateMigrationPlans
# ===========================================================================

class TestGenerateMigrationPlans:

    def _provider_swap(self, urgency="recommended", cost_reduction=20.0):
        return ProviderSwapRecommendation(
            current_provider="runway",
            recommended_provider="muapi",
            asset_types_affected=["video_clip"],
            reason="muapi is better",
            current_composite_score=0.5,
            recommended_composite_score=0.9,
            expected_reliability_improvement=20.0,
            expected_cost_reduction_pct=cost_reduction,
            urgency=urgency,
            migration_steps=[f"{i}. step" for i in range(1, 7)],
        )

    def _model_swap(self, urgency="optional"):
        return ModelSwapRecommendation(
            stage_name="research",
            current_model="anthropic/claude-opus-4.6",
            recommended_model="anthropic/claude-sonnet-4.6",
            reason="premium too costly",
            current_tier="premium",
            recommended_tier="standard",
            expected_cost_reduction_pct=35.0,
            urgency=urgency,
            migration_steps=[f"{i}. step" for i in range(1, 7)],
        )

    def test_empty_inputs_returns_empty_list(self):
        result = generate_migration_plans([], [])
        assert result == []

    def test_provider_swap_generates_provider_plan(self):
        result = generate_migration_plans([self._provider_swap()], [])
        assert len(result) == 1
        assert result[0].plan_type == "provider_swap"

    def test_model_swap_generates_model_plan(self):
        result = generate_migration_plans([], [self._model_swap()])
        assert len(result) == 1
        assert result[0].plan_type == "model_swap"

    def test_plan_id_format_provider(self):
        result = generate_migration_plans([self._provider_swap()], [])
        assert result[0].plan_id == "provider_migration_01"

    def test_plan_id_format_model(self):
        result = generate_migration_plans([], [self._model_swap()])
        assert result[0].plan_id == "model_migration_01"

    def test_plan_title_contains_providers(self):
        result = generate_migration_plans([self._provider_swap()], [])
        assert "runway" in result[0].title
        assert "muapi" in result[0].title

    def test_steps_inherited_from_swap(self):
        result = generate_migration_plans([self._provider_swap()], [])
        assert len(result[0].steps) == 6

    def test_risk_level_high_when_urgent(self):
        result = generate_migration_plans([self._provider_swap(urgency="urgent")], [])
        assert result[0].risk_level == "high"

    def test_risk_level_low_for_optional_model_swap(self):
        result = generate_migration_plans([], [self._model_swap(urgency="optional")])
        assert result[0].risk_level == "low"

    def test_risk_level_medium_for_recommended(self):
        result = generate_migration_plans([], [self._model_swap(urgency="recommended")])
        assert result[0].risk_level == "medium"

    def test_rollback_strategy_not_empty(self):
        result = generate_migration_plans([self._provider_swap()], [self._model_swap()])
        for plan in result:
            assert plan.rollback_strategy
            assert len(plan.rollback_strategy) > 10

    def test_estimated_cost_reduction_inherited(self):
        result = generate_migration_plans([self._provider_swap(cost_reduction=30.0)], [])
        assert result[0].estimated_cost_reduction_pct == 30.0

    def test_multiple_swaps_produce_multiple_plans(self):
        result = generate_migration_plans(
            [self._provider_swap(), self._provider_swap()],
            [self._model_swap()],
        )
        assert len(result) == 3

    def test_returns_list_of_migration_plans(self):
        result = generate_migration_plans([self._provider_swap()], [self._model_swap()])
        assert all(isinstance(p, MigrationPlan) for p in result)

    def test_description_mentions_providers(self):
        result = generate_migration_plans([self._provider_swap()], [])
        assert "runway" in result[0].description or "muapi" in result[0].description


# ===========================================================================
# TestFleetCostEfficiency
# ===========================================================================

class TestFleetCostEfficiency:

    def _make_provider(self, provider, success_rate, composite):
        return ProviderBenchmark(
            provider=provider, total_attempts=10,
            success_count=int(success_rate * 10),
            failure_count=10 - int(success_rate * 10),
            success_rate=success_rate,
            avg_cost_usd=0.05, avg_duration_ms=2000.0,
            cost_efficiency_score=0.95, reliability_score=success_rate,
            speed_score=0.97, composite_score=composite, rank=1,
        )

    def test_unknown_when_no_benchmarks(self):
        result = _compute_fleet_cost_efficiency([], [], [])
        assert result == "unknown"

    def test_optimal_when_no_swaps_and_healthy_providers(self):
        p = self._make_provider("muapi", 0.95, 0.92)
        result = _compute_fleet_cost_efficiency([p], [], [])
        assert result == "optimal"

    def test_suboptimal_when_provider_swaps_exist(self):
        p = self._make_provider("muapi", 0.95, 0.92)
        fake_swap = object()  # truthy — just needs to be non-empty
        result = _compute_fleet_cost_efficiency([p], [fake_swap], [])
        assert result == "suboptimal"

    def test_suboptimal_when_model_swaps_exist(self):
        p = self._make_provider("muapi", 0.95, 0.92)
        fake_swap = object()
        result = _compute_fleet_cost_efficiency([p], [], [fake_swap])
        assert result == "suboptimal"

    def test_critical_when_any_provider_has_high_failure_rate(self):
        # success_rate=0.7 → failure_rate=0.30 > FAILURE_RATE_CRITICAL(0.20)
        p = self._make_provider("runway", 0.7, 0.65)
        result = _compute_fleet_cost_efficiency([p], [], [])
        assert result == "critical"

    def test_critical_takes_priority_over_suboptimal(self):
        p = self._make_provider("runway", 0.7, 0.65)
        fake_swap = object()
        result = _compute_fleet_cost_efficiency([p], [fake_swap], [fake_swap])
        assert result == "critical"

    def test_exactly_at_failure_rate_boundary_is_ok(self):
        # success_rate=0.80 means failure_rate=0.20 which equals critical threshold
        # < not <= so exactly 0.20 is NOT critical
        p = self._make_provider("muapi", 0.80, 0.82)
        result = _compute_fleet_cost_efficiency([p], [], [])
        assert result == "optimal"


# ===========================================================================
# TestComputeTotalSavings
# ===========================================================================

class TestComputeTotalSavings:

    def _provider_swap(self, pct):
        s = ProviderSwapRecommendation(
            current_provider="runway", recommended_provider="muapi",
            asset_types_affected=[], reason="", current_composite_score=0.5,
            recommended_composite_score=0.9, expected_reliability_improvement=0.0,
            expected_cost_reduction_pct=pct, urgency="recommended", migration_steps=[],
        )
        return s

    def _model_swap(self, pct):
        return ModelSwapRecommendation(
            stage_name="research", current_model="anthropic/claude-opus-4.6",
            recommended_model="anthropic/claude-sonnet-4.6", reason="",
            current_tier="premium", recommended_tier="standard",
            expected_cost_reduction_pct=pct, urgency="optional", migration_steps=[],
        )

    def test_empty_inputs_returns_zero(self):
        result = _compute_total_savings([], [])
        assert result == 0.0

    def test_single_provider_swap(self):
        result = _compute_total_savings([self._provider_swap(30.0)], [])
        assert result == pytest.approx(30.0)

    def test_single_model_swap(self):
        result = _compute_total_savings([], [self._model_swap(35.0)])
        assert result == pytest.approx(35.0)

    def test_takes_minimum_of_all_pcts(self):
        # Conservative estimate is the minimum
        result = _compute_total_savings(
            [self._provider_swap(30.0)],
            [self._model_swap(35.0)],
        )
        assert result == pytest.approx(30.0)

    def test_minimum_when_multiple_swaps(self):
        result = _compute_total_savings(
            [self._provider_swap(20.0), self._provider_swap(40.0)],
            [self._model_swap(35.0)],
        )
        assert result == pytest.approx(20.0)


# ===========================================================================
# TestRunProviderModelDirector
# ===========================================================================

class TestRunProviderModelDirector:

    def test_returns_provider_model_director_report(self, db_path):
        result = run_provider_model_director(db_path)
        assert isinstance(result, ProviderModelDirectorReport)

    def test_empty_db_returns_unknown_efficiency(self, db_path):
        result = run_provider_model_director(db_path)
        assert result.fleet_cost_efficiency == "unknown"

    def test_analyzed_at_is_iso_string(self, db_path):
        result = run_provider_model_director(db_path)
        assert "T" in result.analyzed_at
        assert result.analyzed_at.endswith("+00:00")

    def test_top_provider_is_none_on_empty_db(self, db_path):
        result = run_provider_model_director(db_path)
        assert result.top_provider is None

    def test_top_model_is_none_on_empty_db(self, db_path):
        result = run_provider_model_director(db_path)
        assert result.top_model is None

    def test_populated_db_sets_top_provider(self, populated_db):
        result = run_provider_model_director(populated_db)
        assert result.top_provider == "muapi"

    def test_populated_db_sets_top_model(self, populated_db):
        result = run_provider_model_director(populated_db)
        assert result.top_model is not None

    def test_provider_benchmarks_populated(self, populated_db):
        result = run_provider_model_director(populated_db)
        assert len(result.provider_benchmarks) == 2

    def test_model_benchmarks_populated(self, populated_db):
        result = run_provider_model_director(populated_db)
        assert len(result.model_benchmarks) == 2

    def test_critical_efficiency_when_runway_fails_heavily(self, populated_db):
        # runway has 3/5 failures = 60% failure rate — critical
        result = run_provider_model_director(populated_db)
        assert result.fleet_cost_efficiency == "critical"

    def test_provider_swap_recommended_due_to_runway_failures(self, populated_db):
        result = run_provider_model_director(populated_db)
        assert len(result.provider_swap_recommendations) >= 1

    def test_model_swap_recommended_for_opus(self, populated_db):
        result = run_provider_model_director(populated_db)
        assert len(result.model_swap_recommendations) >= 1

    def test_migration_plans_generated(self, populated_db):
        result = run_provider_model_director(populated_db)
        assert len(result.migration_plans) >= 1

    def test_total_savings_pct_is_float(self, populated_db):
        result = run_provider_model_director(populated_db)
        assert isinstance(result.total_estimated_savings_pct, float)

    def test_metadata_version_is_v1(self, populated_db):
        result = run_provider_model_director(populated_db)
        assert result.metadata_version == "v1"

    def test_bad_db_returns_report_with_empty_lists(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        result = run_provider_model_director(bad)
        assert isinstance(result, ProviderModelDirectorReport)
        assert result.provider_benchmarks == []
        assert result.model_benchmarks == []
        assert result.fleet_cost_efficiency == "unknown"


# ===========================================================================
# TestNonBlocking
# ===========================================================================

class TestNonBlocking:

    def test_benchmark_providers_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        result = benchmark_providers(bad)
        assert result == []

    def test_benchmark_models_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        result = benchmark_models(bad)
        assert result == []

    def test_run_director_bad_db_does_not_raise(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        # Must not raise any exception
        result = run_provider_model_director(bad)
        assert isinstance(result, ProviderModelDirectorReport)

    def test_find_provider_swaps_empty_list_does_not_raise(self):
        result = find_provider_swap_recommendations([])
        assert result == []

    def test_find_model_swaps_empty_list_does_not_raise(self):
        result = find_model_swap_recommendations([])
        assert result == []

    def test_generate_migration_plans_empty_does_not_raise(self):
        result = generate_migration_plans([], [])
        assert result == []


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:

    @pytest.fixture(autouse=True)
    def block_network(self, monkeypatch):
        def no_socket(*args, **kwargs):
            raise OSError("Network blocked in tests")
        monkeypatch.setattr(socket, "socket", no_socket)

    def test_benchmark_providers_no_network(self, populated_db):
        result = benchmark_providers(populated_db)
        assert isinstance(result, list)

    def test_benchmark_models_no_network(self, populated_db):
        result = benchmark_models(populated_db)
        assert isinstance(result, list)

    def test_run_director_no_network(self, populated_db):
        result = run_provider_model_director(populated_db)
        assert isinstance(result, ProviderModelDirectorReport)

    def test_find_provider_swaps_no_network(self):
        result = find_provider_swap_recommendations([])
        assert result == []

    def test_find_model_swaps_no_network(self):
        result = find_model_swap_recommendations([])
        assert result == []

    def test_generate_plans_no_network(self):
        result = generate_migration_plans([], [])
        assert result == []


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:

    def test_benchmark_providers_does_not_write_to_db(self, populated_db):
        conn = get_connection(populated_db)
        count_before = conn.execute(
            "SELECT COUNT(*) FROM asset_versions"
        ).fetchone()[0]
        conn.close()

        benchmark_providers(populated_db)

        conn = get_connection(populated_db)
        count_after = conn.execute(
            "SELECT COUNT(*) FROM asset_versions"
        ).fetchone()[0]
        conn.close()
        assert count_before == count_after

    def test_benchmark_models_does_not_write_to_db(self, populated_db):
        conn = get_connection(populated_db)
        count_before = conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0]
        conn.close()

        benchmark_models(populated_db)

        conn = get_connection(populated_db)
        count_after = conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0]
        conn.close()
        assert count_before == count_after

    def test_run_director_does_not_write_to_db(self, populated_db):
        conn = get_connection(populated_db)
        asset_before = conn.execute("SELECT COUNT(*) FROM asset_versions").fetchone()[0]
        cost_before = conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0]
        conn.close()

        run_provider_model_director(populated_db)

        conn = get_connection(populated_db)
        asset_after = conn.execute("SELECT COUNT(*) FROM asset_versions").fetchone()[0]
        cost_after = conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0]
        conn.close()
        assert asset_before == asset_after
        assert cost_before == cost_after

    def test_no_ep_000_references(self, populated_db):
        """No production episode IDs must appear in test data or calls."""
        conn = get_connection(populated_db)
        rows = conn.execute(
            "SELECT episode_id FROM asset_versions WHERE episode_id='ep_000'"
        ).fetchall()
        conn.close()
        assert rows == []

    def test_no_ep_smoke_references(self, populated_db):
        conn = get_connection(populated_db)
        rows = conn.execute(
            "SELECT episode_id FROM asset_versions WHERE episode_id LIKE 'ep_smoke%'"
        ).fetchall()
        conn.close()
        assert rows == []
