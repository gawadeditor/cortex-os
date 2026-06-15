"""
hypogeum/provider_model_director.py — Phase F: Provider & Model Director

A CTO-style analytical agent that benchmarks all providers and models in
the factory, detects better-performing alternatives, and generates concrete
migration plans for the operator.

Architecture:
    - Reads asset_versions (provider perf), costs (model cost),
      stage_executions (model assignments). No writes. No network.
    - All scoring is deterministic and testable.
    - Migration plans are self-contained step-by-step instructions.

Scoring:
    Provider composite = reliability*0.50 + cost_efficiency*0.30 + speed*0.20
    Model composite    = cost_efficiency*0.50 + usage_breadth*0.30 + reliability*0.20

Provider swap triggers:
    - Composite gap >= SWAP_THRESHOLD (0.15) between best and any active provider.
    - OR failure_rate > FAILURE_RATE_CRITICAL (0.20) for an active provider.

Model swap triggers:
    - avg_cost_per_call > MODEL_COST_CEILING (0.10 USD).
    - Stage uses premium-tier model but standard-tier alternative exists.

Public API:
    benchmark_providers(db_path=None)        -> list[ProviderBenchmark]
    benchmark_models(db_path=None)           -> list[ModelBenchmark]
    find_provider_swap_recommendations(benchmarks)
                                             -> list[ProviderSwapRecommendation]
    find_model_swap_recommendations(model_benchmarks)
                                             -> list[ModelSwapRecommendation]
    generate_migration_plans(provider_swaps, model_swaps)
                                             -> list[MigrationPlan]
    run_provider_model_director(db_path=None) -> ProviderModelDirectorReport
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
METADATA_VERSION = "v1"

# Provider scoring
PROVIDER_COST_CEILING_USD  = 1.00    # per asset attempt
PROVIDER_SPEED_CEILING_MS  = 60_000.0
PROVIDER_WEIGHT_RELIABILITY  = 0.50
PROVIDER_WEIGHT_COST         = 0.30
PROVIDER_WEIGHT_SPEED        = 0.20

# Model scoring
MODEL_COST_CEILING_USD     = 0.50    # per LLM call
MODEL_WEIGHT_COST          = 0.50
MODEL_WEIGHT_BREADTH       = 0.30    # how many distinct stages use this model
MODEL_WEIGHT_RELIABILITY   = 0.20

# Swap recommendation thresholds
SWAP_COMPOSITE_THRESHOLD  = 0.15    # min composite gap to recommend a provider swap
FAILURE_RATE_CRITICAL     = 0.20    # failure rate that always triggers a swap recommendation
MODEL_COST_CEILING        = 0.10    # avg cost/call above which we recommend downgrade

# Known model tiers (for swap suggestions)
MODEL_TIERS: dict[str, str] = {
    "anthropic/claude-opus-4.6":    "premium",
    "anthropic/claude-sonnet-4.6":  "standard",
    "anthropic/claude-haiku-4.5":   "economy",
    "openai/gpt-5.5-pro":           "premium",
    "openai/gpt-4.5-mini":          "economy",
    "openai/gpt-4o":                "standard",
}

# Suggested cheaper alternatives for premium models
MODEL_ALTERNATIVES: dict[str, str] = {
    "anthropic/claude-opus-4.6":    "anthropic/claude-sonnet-4.6",
    "openai/gpt-5.5-pro":           "anthropic/claude-sonnet-4.6",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _round4(v: float) -> float:
    return round(v, 4)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _safe_rate(num: int, denom: int) -> float:
    return _round4(num / denom) if denom > 0 else 0.0


def _cost_score(avg_cost: float, ceiling: float) -> float:
    return _round4(_clamp(1.0 - avg_cost / ceiling))


def _speed_score(avg_ms: float, ceiling: float) -> float:
    return _round4(_clamp(1.0 - avg_ms / ceiling))


# ===========================================================================
# Data Contracts
# ===========================================================================

@dataclass
class ProviderBenchmark:
    """Performance benchmark for one asset provider."""
    provider:             str
    total_attempts:       int
    success_count:        int
    failure_count:        int
    success_rate:         float
    avg_cost_usd:         float
    avg_duration_ms:      float
    cost_efficiency_score: float   # 0–1; higher = cheaper
    reliability_score:    float    # 0–1; = success_rate
    speed_score:          float    # 0–1; higher = faster
    composite_score:      float    # weighted average
    rank:                 int      # 1 = best


@dataclass
class ModelBenchmark:
    """Usage and cost benchmark for one model slug."""
    model_id:          str
    usage_count:       int
    total_cost_usd:    float
    avg_cost_per_call: float
    stage_assignments: list      # distinct stages that use this model
    breadth_score:     float     # normalised count of distinct stages
    cost_score:        float     # 0–1; higher = cheaper
    reliability_score: float     # 0–1; from stage success rates (0.5 if unknown)
    composite_score:   float
    tier:              str       # "premium"|"standard"|"economy"|"unknown"
    rank:              int       # 1 = best


@dataclass
class ProviderSwapRecommendation:
    """Recommendation to move an asset type from one provider to another."""
    current_provider:                   str
    recommended_provider:               str
    asset_types_affected:               list   # which asset types to migrate
    reason:                             str
    current_composite_score:            float
    recommended_composite_score:        float
    expected_reliability_improvement:   float  # percentage points
    expected_cost_reduction_pct:        float  # %
    urgency:                            str    # "urgent"|"recommended"|"optional"
    migration_steps:                    list   # list[str]


@dataclass
class ModelSwapRecommendation:
    """Recommendation to replace one model with a cheaper/better alternative."""
    stage_name:              str
    current_model:           str
    recommended_model:       str
    reason:                  str
    current_tier:            str
    recommended_tier:        str
    expected_cost_reduction_pct: float
    urgency:                 str    # "urgent"|"recommended"|"optional"
    migration_steps:         list   # list[str]


@dataclass
class MigrationPlan:
    """
    Step-by-step migration plan for a provider or model swap.

    Each plan is self-contained: an operator can execute it without
    reading any other documentation.
    """
    plan_id:                     str
    title:                       str
    plan_type:                   str   # "provider_swap"|"model_swap"
    description:                 str
    steps:                       list  # list[str], ordered
    estimated_cost_reduction_pct: float
    risk_level:                  str   # "low"|"medium"|"high"
    rollback_strategy:           str


@dataclass
class ProviderModelDirectorReport:
    """
    Complete CTO-level analysis from the Provider & Model Director.

    fleet_cost_efficiency:
        "optimal"    — all providers/models within normal operating range
        "suboptimal" — cost savings of 15%+ are available
        "critical"   — a provider is failing at > 20% rate
        "unknown"    — insufficient data
    """
    analyzed_at:                  str
    provider_benchmarks:          list
    model_benchmarks:             list
    provider_swap_recommendations: list
    model_swap_recommendations:   list
    migration_plans:              list
    top_provider:                 Optional[str]
    top_model:                    Optional[str]
    fleet_cost_efficiency:        str
    total_estimated_savings_pct:  float
    metadata_version:             str = METADATA_VERSION


# ===========================================================================
# Provider Benchmarking
# ===========================================================================

def benchmark_providers(db_path: Optional[Path] = None) -> list:
    """
    Benchmark all providers from asset_versions data.

    Returns a list of ProviderBenchmark sorted by composite_score DESC
    (rank 1 = best). Non-blocking: returns [] on error.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute("""
                SELECT
                    provider,
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded,
                    SUM(CASE WHEN status='failed'    THEN 1 ELSE 0 END) AS failed,
                    AVG(COALESCE(cost_usd, 0))    AS avg_cost,
                    AVG(COALESCE(duration_ms, 0)) AS avg_dur
                FROM asset_versions
                GROUP BY provider
                ORDER BY provider
            """).fetchall()
        finally:
            conn.close()

        benchmarks: list[ProviderBenchmark] = []
        for r in rows:
            total  = r["total"]     or 0
            succ   = r["succeeded"] or 0
            fail   = r["failed"]    or 0
            cost   = r["avg_cost"]  or 0.0
            dur    = r["avg_dur"]   or 0.0
            sr     = _safe_rate(succ, total)
            cs     = _cost_score(cost, PROVIDER_COST_CEILING_USD)
            ss     = _speed_score(dur, PROVIDER_SPEED_CEILING_MS)
            comp   = _round4(
                sr * PROVIDER_WEIGHT_RELIABILITY
                + cs * PROVIDER_WEIGHT_COST
                + ss * PROVIDER_WEIGHT_SPEED
            )
            benchmarks.append(ProviderBenchmark(
                provider              = r["provider"],
                total_attempts        = total,
                success_count         = succ,
                failure_count         = fail,
                success_rate          = sr,
                avg_cost_usd          = _round4(cost),
                avg_duration_ms       = _round4(dur),
                cost_efficiency_score = cs,
                reliability_score     = sr,
                speed_score           = ss,
                composite_score       = comp,
                rank                  = 0,   # set after sort
            ))

        benchmarks.sort(key=lambda b: -b.composite_score)
        for i, b in enumerate(benchmarks, start=1):
            b.rank = i

        return benchmarks
    except Exception:
        return []


# ===========================================================================
# Model Benchmarking
# ===========================================================================

def benchmark_models(db_path: Optional[Path] = None) -> list:
    """
    Benchmark all models from costs and stage_executions data.

    Returns a list of ModelBenchmark sorted by composite_score DESC
    (rank 1 = best). Non-blocking: returns [] on error.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            cost_rows = conn.execute("""
                SELECT model, COUNT(*) AS cnt, SUM(cost_usd) AS total_cost
                FROM costs
                WHERE model IS NOT NULL
                GROUP BY model
            """).fetchall()

            stage_rows = conn.execute("""
                SELECT model_slug,
                       GROUP_CONCAT(DISTINCT stage_name) AS stages,
                       COUNT(DISTINCT stage_name) AS stage_count,
                       AVG(CASE WHEN status='completed' THEN 1.0 ELSE 0.0 END) AS success_rate
                FROM stage_executions
                WHERE model_slug IS NOT NULL AND skipped = 0
                GROUP BY model_slug
            """).fetchall()
        finally:
            conn.close()

        # Build stage map
        stage_map: dict[str, dict] = {}
        for r in stage_rows:
            slug = r["model_slug"]
            stages_raw = r["stages"] or ""
            stages = [s for s in stages_raw.split(",") if s]
            stage_map[slug] = {
                "stages":        stages,
                "stage_count":   r["stage_count"] or 0,
                "success_rate":  r["success_rate"] or 0.5,
            }

        # Find max stage count for breadth normalisation
        max_stages = max((v["stage_count"] for v in stage_map.values()), default=1)
        max_stages = max(max_stages, 1)

        benchmarks: list[ModelBenchmark] = []
        for r in cost_rows:
            model  = r["model"]
            cnt    = r["cnt"]         or 0
            total  = r["total_cost"]  or 0.0
            avg    = _round4(total / cnt) if cnt > 0 else 0.0
            cs     = _cost_score(avg, MODEL_COST_CEILING_USD)
            sd     = stage_map.get(model, {})
            stages = sd.get("stages", [])
            sc     = sd.get("stage_count", 0)
            sr     = sd.get("success_rate", 0.5)
            bs     = _round4(sc / max_stages)   # breadth score
            comp   = _round4(
                cs * MODEL_WEIGHT_COST
                + bs * MODEL_WEIGHT_BREADTH
                + sr * MODEL_WEIGHT_RELIABILITY
            )
            tier = MODEL_TIERS.get(model, "unknown")
            benchmarks.append(ModelBenchmark(
                model_id          = model,
                usage_count       = cnt,
                total_cost_usd    = _round4(total),
                avg_cost_per_call = avg,
                stage_assignments = stages,
                breadth_score     = bs,
                cost_score        = cs,
                reliability_score = _round4(sr),
                composite_score   = comp,
                tier              = tier,
                rank              = 0,
            ))

        benchmarks.sort(key=lambda b: -b.composite_score)
        for i, b in enumerate(benchmarks, start=1):
            b.rank = i

        return benchmarks
    except Exception:
        return []


# ===========================================================================
# Provider Swap Recommendations
# ===========================================================================

def find_provider_swap_recommendations(benchmarks: list) -> list:
    """
    Compare providers and recommend swaps where a significantly better
    alternative is available.

    Triggers:
        1. Any provider has failure_rate > FAILURE_RATE_CRITICAL
        2. Composite gap between best and any other provider >= SWAP_THRESHOLD

    Non-blocking: returns [] on error or with insufficient data.
    """
    try:
        if len(benchmarks) < 2:
            return []

        best = benchmarks[0]   # rank 1 (sorted by composite DESC)
        recommendations: list[ProviderSwapRecommendation] = []

        seen: set[str] = set()
        for b in benchmarks[1:]:
            gap            = _round4(best.composite_score - b.composite_score)
            failure_critical = b.success_rate < (1 - FAILURE_RATE_CRITICAL)
            composite_gap    = gap >= SWAP_COMPOSITE_THRESHOLD

            if not (failure_critical or composite_gap):
                continue
            if b.provider in seen:
                continue
            seen.add(b.provider)

            # Estimate savings
            if best.avg_cost_usd > 0 and b.avg_cost_usd > 0:
                cost_reduction = _round4(
                    (b.avg_cost_usd - best.avg_cost_usd) / b.avg_cost_usd * 100
                )
                cost_reduction = max(0.0, cost_reduction)
            else:
                cost_reduction = 0.0

            reliability_improvement = _round4(
                (best.success_rate - b.success_rate) * 100
            )

            if failure_critical:
                urgency = "urgent"
                reason  = (
                    f"Provider '{b.provider}' has a critical failure rate "
                    f"of {(1 - b.success_rate):.0%} "
                    f"({b.failure_count}/{b.total_attempts} attempts failed). "
                    f"'{best.provider}' achieves {best.success_rate:.0%} success rate."
                )
            else:
                urgency = "recommended"
                reason  = (
                    f"'{best.provider}' outperforms '{b.provider}' by "
                    f"{gap:.2f} composite points "
                    f"(reliability: {best.success_rate:.0%} vs {b.success_rate:.0%}, "
                    f"cost: ${best.avg_cost_usd:.4f} vs ${b.avg_cost_usd:.4f}/attempt)."
                )

            steps = [
                f"1. Identify all asset_versions rows using provider='{b.provider}'.",
                f"2. Update model_profile.image_provider or .video_provider to "
                f"'{best.provider}' in the relevant channel profiles.",
                f"3. Run one dry-run episode to verify '{best.provider}' integration.",
                f"4. Compare asset quality and cost for the first production run.",
                f"5. Monitor failure rates for 3 episodes before fully decommissioning "
                f"'{b.provider}'.",
                f"6. Rollback: revert model_profile fields to '{b.provider}' if "
                f"quality degrades.",
            ]

            recommendations.append(ProviderSwapRecommendation(
                current_provider                 = b.provider,
                recommended_provider             = best.provider,
                asset_types_affected             = ["video_clip", "image"],
                reason                           = reason,
                current_composite_score          = b.composite_score,
                recommended_composite_score      = best.composite_score,
                expected_reliability_improvement = reliability_improvement,
                expected_cost_reduction_pct      = cost_reduction,
                urgency                          = urgency,
                migration_steps                  = steps,
            ))

        return sorted(recommendations, key=lambda r: r.current_composite_score)
    except Exception:
        return []


# ===========================================================================
# Model Swap Recommendations
# ===========================================================================

def find_model_swap_recommendations(model_benchmarks: list) -> list:
    """
    Identify models that should be replaced by cheaper or more reliable
    alternatives.

    Triggers:
        1. avg_cost_per_call > MODEL_COST_CEILING
        2. Model is in premium tier AND a standard alternative is known

    Non-blocking: returns [] on error or insufficient data.
    """
    try:
        recommendations: list[ModelSwapRecommendation] = []

        for mb in model_benchmarks:
            alternative = MODEL_ALTERNATIVES.get(mb.model_id)
            if alternative is None:
                continue   # no known alternative

            current_tier = MODEL_TIERS.get(mb.model_id, "unknown")
            alt_tier     = MODEL_TIERS.get(alternative, "unknown")

            # Trigger conditions
            cost_trigger = mb.avg_cost_per_call > MODEL_COST_CEILING
            tier_trigger = current_tier == "premium"

            if not (cost_trigger or tier_trigger):
                continue

            # Estimate savings (rough: standard ~40% cheaper than premium)
            cost_reduction = 35.0 if current_tier == "premium" else 15.0

            if mb.avg_cost_per_call > MODEL_COST_CEILING * 3:
                urgency = "urgent"
            elif cost_trigger:
                urgency = "recommended"
            else:
                urgency = "optional"

            reason_parts = []
            if tier_trigger:
                reason_parts.append(
                    f"Model '{mb.model_id}' is premium-tier. "
                    f"'{alternative}' provides similar capability at lower cost."
                )
            if cost_trigger:
                reason_parts.append(
                    f"Avg cost ${mb.avg_cost_per_call:.4f}/call exceeds "
                    f"ceiling ${MODEL_COST_CEILING:.2f}."
                )

            # Stage names this affects
            stages = mb.stage_assignments if mb.stage_assignments else ["(unknown stages)"]
            stage_str = ", ".join(stages[:3])
            if len(stages) > 3:
                stage_str += f" + {len(stages) - 3} more"

            for stage in (stages if stages else ["all"]):
                steps = [
                    f"1. Update model_profile.{stage} from '{mb.model_id}' "
                    f"to '{alternative}'.",
                    f"2. Run acceptance audit to verify no blocker is introduced.",
                    f"3. Execute one dry-run episode targeting this stage.",
                    f"4. Compare output quality for the stage against baseline.",
                    f"5. Monitor cost for 3 episodes to confirm reduction.",
                    f"6. Rollback: revert model_profile to '{mb.model_id}' "
                    f"if quality drops measurably.",
                ]
                recommendations.append(ModelSwapRecommendation(
                    stage_name              = stage,
                    current_model           = mb.model_id,
                    recommended_model       = alternative,
                    reason                  = " ".join(reason_parts),
                    current_tier            = current_tier,
                    recommended_tier        = alt_tier,
                    expected_cost_reduction_pct = cost_reduction,
                    urgency                 = urgency,
                    migration_steps         = steps,
                ))

        return sorted(recommendations,
                      key=lambda r: (-{"urgent": 2, "recommended": 1, "optional": 0}
                                       .get(r.urgency, 0),
                                     r.stage_name))
    except Exception:
        return []


# ===========================================================================
# Migration Plan Generator
# ===========================================================================

def generate_migration_plans(
    provider_swaps: list,
    model_swaps:    list,
) -> list:
    """
    Convert swap recommendations into self-contained migration plans.

    Each plan consolidates one swap recommendation into a named plan
    with risk assessment and rollback strategy.

    Non-blocking: returns [] on error.
    """
    try:
        plans: list[MigrationPlan] = []

        for i, swap in enumerate(provider_swaps, start=1):
            risk = "medium"
            if swap.urgency == "urgent":
                risk = "high"
            elif swap.expected_cost_reduction_pct > 20:
                risk = "medium"
            else:
                risk = "low"

            plans.append(MigrationPlan(
                plan_id    = f"provider_migration_{i:02d}",
                title      = (f"Migrate '{swap.current_provider}' → "
                              f"'{swap.recommended_provider}'"),
                plan_type  = "provider_swap",
                description = (
                    f"Replace provider '{swap.current_provider}' with "
                    f"'{swap.recommended_provider}' for "
                    f"{', '.join(swap.asset_types_affected)}. "
                    f"Expected reliability improvement: "
                    f"+{swap.expected_reliability_improvement:.1f}pp. "
                    f"Expected cost reduction: "
                    f"{swap.expected_cost_reduction_pct:.1f}%."
                ),
                steps                        = swap.migration_steps,
                estimated_cost_reduction_pct = swap.expected_cost_reduction_pct,
                risk_level                   = risk,
                rollback_strategy            = (
                    f"Revert model_profile image_provider / video_provider to "
                    f"'{swap.current_provider}'. All asset_versions history is "
                    "preserved — no data loss on rollback."
                ),
            ))

        for i, swap in enumerate(model_swaps, start=1):
            risk = "low" if swap.urgency == "optional" else "medium"
            plans.append(MigrationPlan(
                plan_id    = f"model_migration_{i:02d}",
                title      = (f"Replace '{swap.current_model}' → "
                              f"'{swap.recommended_model}' ({swap.stage_name})"),
                plan_type  = "model_swap",
                description = (
                    f"Replace {swap.current_tier}-tier '{swap.current_model}' "
                    f"with {swap.recommended_tier}-tier '{swap.recommended_model}' "
                    f"in stage '{swap.stage_name}'. "
                    f"Expected cost reduction: "
                    f"{swap.expected_cost_reduction_pct:.0f}%."
                ),
                steps                        = swap.migration_steps,
                estimated_cost_reduction_pct = swap.expected_cost_reduction_pct,
                risk_level                   = risk,
                rollback_strategy            = (
                    f"Revert model_profile to '{swap.current_model}' in the "
                    "channel config. Run acceptance audit after reverting."
                ),
            ))

        return plans
    except Exception:
        return []


# ===========================================================================
# Fleet Cost Efficiency
# ===========================================================================

def _compute_fleet_cost_efficiency(
    provider_benchmarks: list,
    provider_swaps:      list,
    model_swaps:         list,
) -> str:
    if not provider_benchmarks:
        return "unknown"
    if any(b.success_rate < (1 - FAILURE_RATE_CRITICAL) for b in provider_benchmarks):
        return "critical"
    if provider_swaps or model_swaps:
        return "suboptimal"
    return "optimal"


def _compute_total_savings(provider_swaps: list, model_swaps: list) -> float:
    """Rough estimate: average of all expected savings percentages."""
    all_pcts = (
        [s.expected_cost_reduction_pct for s in provider_swaps]
        + [s.expected_cost_reduction_pct for s in model_swaps]
    )
    if not all_pcts:
        return 0.0
    # Conservative: take the minimum to avoid overselling
    return _round4(min(all_pcts)) if all_pcts else 0.0


# ===========================================================================
# Main Entry Point
# ===========================================================================

def run_provider_model_director(
    db_path: Optional[Path] = None,
) -> ProviderModelDirectorReport:
    """
    Run the Provider & Model Director and return a complete CTO-level report.

    Benchmarks all providers and models, identifies swap opportunities,
    and generates self-contained migration plans.

    Non-blocking: all sections degrade gracefully on error.

    Args:
        db_path: Override SQLite path.

    Returns:
        ProviderModelDirectorReport.
    """
    provider_benchmarks = benchmark_providers(db_path)
    model_benchmarks    = benchmark_models(db_path)

    provider_swaps = find_provider_swap_recommendations(provider_benchmarks)
    model_swaps    = find_model_swap_recommendations(model_benchmarks)
    plans          = generate_migration_plans(provider_swaps, model_swaps)

    top_provider = provider_benchmarks[0].provider if provider_benchmarks else None
    top_model    = model_benchmarks[0].model_id    if model_benchmarks    else None

    fleet_efficiency = _compute_fleet_cost_efficiency(
        provider_benchmarks, provider_swaps, model_swaps
    )
    total_savings = _compute_total_savings(provider_swaps, model_swaps)

    return ProviderModelDirectorReport(
        analyzed_at                   = _now_iso(),
        provider_benchmarks           = provider_benchmarks,
        model_benchmarks              = model_benchmarks,
        provider_swap_recommendations = provider_swaps,
        model_swap_recommendations    = model_swaps,
        migration_plans               = plans,
        top_provider                  = top_provider,
        top_model                     = top_model,
        fleet_cost_efficiency         = fleet_efficiency,
        total_estimated_savings_pct   = total_savings,
    )
