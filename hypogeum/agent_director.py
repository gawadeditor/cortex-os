"""
hypogeum/agent_director.py — Phase E: Agent Director

Evaluates every pipeline agent (stage) on four dimensions:
quality, cost, speed, and reliability. Produces retirement candidates,
upgrade recommendations, and a ranked fleet health report.

Architecture:
    - Reads from stage_executions (v_stage_performance view + raw table).
    - No LLM calls. No network. Deterministic. Non-blocking.
    - All scores are floats in [0.0, 1.0]. Higher is always better.

Scoring dimensions:
    quality_score     = success_rate (successes / non-skipped executions)
    cost_score        = 1 - min(1, avg_cost_usd / COST_CEILING)
    speed_score       = 1 - min(1, avg_duration_ms / SPEED_CEILING_MS)
    reliability_score = quality_score, penalised when sample_size < 3

    composite_score   = quality*0.40 + cost*0.25 + speed*0.20 + reliability*0.15

Recommendation thresholds (composite_score):
    >= 0.80  → "healthy"
    >= 0.60  → "monitor"
    >= 0.40  → "optimize"
    <  0.40  → "retire"

Retirement criteria:
    composite < 0.40 AND non-skipped executions >= MIN_SAMPLE_FOR_RETIREMENT

Public API:
    score_agent(stage_name, total, successes, failures, skips,
                avg_duration_ms, avg_cost_usd)    -> AgentScore
    analyze_agents(db_path=None)                  -> list[AgentScore]
    find_retirement_candidates(scores, min_sample) -> list[RetirementCandidate]
    find_upgrade_recommendations(scores)           -> list[UpgradeRecommendation]
    run_agent_director(db_path=None)               -> AgentDirectorReport
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

# Normalisation ceilings — scores go to 0.0 at these values
COST_CEILING_USD = 1.00        # $1.00/stage → cost_score = 0.0
SPEED_CEILING_MS = 60_000.0    # 60 seconds  → speed_score = 0.0

# Composite weights (must sum to 1.0)
WEIGHT_QUALITY     = 0.40
WEIGHT_COST        = 0.25
WEIGHT_SPEED       = 0.20
WEIGHT_RELIABILITY = 0.15

# Recommendation thresholds
THRESHOLD_HEALTHY  = 0.80
THRESHOLD_MONITOR  = 0.60
THRESHOLD_OPTIMIZE = 0.40

# Retirement requires this many non-skipped executions for statistical confidence
MIN_SAMPLE_FOR_RETIREMENT = 5

# Upgrade recommendation triggers
UPGRADE_QUALITY_THRESHOLD     = 0.70   # quality_score below this → quality upgrade
UPGRADE_COST_THRESHOLD        = 0.70   # cost_score below this → cost optimization
UPGRADE_SPEED_THRESHOLD       = 0.70   # speed_score below this → speed optimization
UPGRADE_RELIABILITY_THRESHOLD = 0.70   # reliability_score below this → reliability fix


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _round4(v: float) -> float:
    return round(v, 4)


# ===========================================================================
# Data Contracts
# ===========================================================================

@dataclass
class AgentScore:
    """
    Four-dimensional performance score for one pipeline agent.

    All scores are in [0.0, 1.0]; higher is always better.
    composite_score is the weighted average used for ranking.
    recommendation is derived from composite_score thresholds.
    """
    stage_name:        str
    quality_score:     float    # success_rate (non-skip)
    cost_score:        float    # 1 - normalised avg cost
    speed_score:       float    # 1 - normalised avg duration
    reliability_score: float    # quality adjusted for sample confidence
    composite_score:   float    # weighted average of all four
    sample_size:       int      # total executions (including skips)
    non_skip_count:    int      # executions used for quality/reliability
    recommendation:    str      # "healthy"|"monitor"|"optimize"|"retire"


@dataclass
class RetirementCandidate:
    """An agent recommended for retirement or replacement."""
    stage_name:             str
    composite_score:        float
    reason:                 str
    evidence:               dict    # supporting stats
    replacement_suggestion: str
    urgency:                str     # "urgent"|"recommended"|"optional"


@dataclass
class UpgradeRecommendation:
    """An agent with one dimension that needs improvement."""
    stage_name:           str
    dimension:            str    # "quality"|"cost"|"speed"|"reliability"
    current_score:        float
    target_score:         float
    suggested_action:     str
    expected_improvement: str


@dataclass
class AgentDirectorReport:
    """
    Complete fleet evaluation from the Agent Director.

    overall_fleet_health:
        "healthy"  — all agents composite >= 0.80
        "warning"  — any agent in "monitor" or "optimize" range
        "degraded" — any retirement candidate exists
        "unknown"  — no stage execution data
    """
    analyzed_at:             str
    total_agents_evaluated:  int
    agent_scores:            list
    retirement_candidates:   list
    upgrade_recommendations: list
    healthy_agents:          list     # stage names, composite >= 0.80
    agents_needing_attention: list    # stage names sorted by composite ASC
    cost_efficiency_ranking: list     # stage names sorted by cost_score DESC
    speed_ranking:           list     # stage names sorted by speed_score DESC
    overall_fleet_health:    str
    metadata_version:        str = METADATA_VERSION


# ===========================================================================
# Scoring
# ===========================================================================

def score_agent(
    stage_name:      str,
    total:           int,
    successes:       int,
    failures:        int,
    skips:           int,
    avg_duration_ms: Optional[float],
    avg_cost_usd:    Optional[float],
) -> AgentScore:
    """
    Compute the four-dimensional score for one agent.

    Args:
        stage_name:      Stage identifier.
        total:           Total executions (including skips).
        successes:       Executions with status='completed'.
        failures:        Executions with status='failed'.
        skips:           Executions with skipped=1.
        avg_duration_ms: Mean duration in milliseconds (None if unknown).
        avg_cost_usd:    Mean cost in USD (None if unknown).

    Returns:
        AgentScore.
    """
    non_skip = total - skips
    non_skip = max(non_skip, 0)

    # Quality
    if non_skip == 0:
        quality_score = 0.5      # no actionable data → neutral
    else:
        quality_score = _round4(successes / non_skip)

    # Reliability — same as quality but penalised for low sample size
    if non_skip == 0:
        reliability_score = 0.5
    elif non_skip < 3:
        reliability_score = _round4(quality_score * 0.7)   # low-confidence penalty
    else:
        reliability_score = quality_score

    # Cost (higher score = cheaper)
    raw_cost = avg_cost_usd or 0.0
    cost_score = _round4(max(0.0, 1.0 - raw_cost / COST_CEILING_USD))

    # Speed (higher score = faster)
    raw_dur = avg_duration_ms or 0.0
    speed_score = _round4(max(0.0, 1.0 - raw_dur / SPEED_CEILING_MS))

    # Composite
    composite = _round4(
        quality_score     * WEIGHT_QUALITY
        + cost_score      * WEIGHT_COST
        + speed_score     * WEIGHT_SPEED
        + reliability_score * WEIGHT_RELIABILITY
    )

    # Recommendation
    if composite >= THRESHOLD_HEALTHY:
        recommendation = "healthy"
    elif composite >= THRESHOLD_MONITOR:
        recommendation = "monitor"
    elif composite >= THRESHOLD_OPTIMIZE:
        recommendation = "optimize"
    else:
        recommendation = "retire"

    return AgentScore(
        stage_name        = stage_name,
        quality_score     = quality_score,
        cost_score        = cost_score,
        speed_score       = speed_score,
        reliability_score = reliability_score,
        composite_score   = composite,
        sample_size       = total,
        non_skip_count    = non_skip,
        recommendation    = recommendation,
    )


# ===========================================================================
# Agent Analysis
# ===========================================================================

def analyze_agents(db_path: Optional[Path] = None) -> list:
    """
    Score all pipeline agents from stage_executions data.

    Uses the v_stage_performance view for aggregates and the
    stage_executions table for per-stage cost averages.

    Returns a list of AgentScore objects sorted by composite_score DESC
    (best agents first).

    Non-blocking: returns [] on any error.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute("""
                SELECT
                    sp.stage_name,
                    sp.total_executions,
                    sp.successes,
                    sp.failures,
                    sp.skips,
                    sp.avg_duration_ms,
                    sp.avg_cost_usd
                FROM v_stage_performance sp
                ORDER BY sp.stage_name
            """).fetchall()
        finally:
            conn.close()

        scores = [
            score_agent(
                stage_name      = r["stage_name"],
                total           = r["total_executions"] or 0,
                successes       = r["successes"]        or 0,
                failures        = r["failures"]         or 0,
                skips           = r["skips"]            or 0,
                avg_duration_ms = r["avg_duration_ms"],
                avg_cost_usd    = r["avg_cost_usd"],
            )
            for r in rows
        ]

        # Sort: best composite first
        return sorted(scores, key=lambda s: -s.composite_score)
    except Exception:
        return []


# ===========================================================================
# Retirement Candidates
# ===========================================================================

def find_retirement_candidates(
    scores:     list,
    min_sample: int = MIN_SAMPLE_FOR_RETIREMENT,
) -> list:
    """
    Identify agents whose composite score is below the retirement threshold
    AND have enough executions for statistical confidence.

    Args:
        scores:     List of AgentScore objects from analyze_agents().
        min_sample: Minimum non-skip executions required for retirement
                    recommendation.

    Returns:
        List of RetirementCandidate sorted by composite_score ASC
        (worst first).

    Non-blocking: returns [] on any error.
    """
    try:
        candidates: list[RetirementCandidate] = []

        for s in scores:
            if s.composite_score >= THRESHOLD_OPTIMIZE:
                continue
            if s.non_skip_count < min_sample:
                continue   # insufficient data

            # Determine urgency
            if s.composite_score < 0.20:
                urgency = "urgent"
            elif s.composite_score < 0.30:
                urgency = "recommended"
            else:
                urgency = "optional"

            # Build replacement suggestion based on weakest dimension
            dims = {
                "quality":     s.quality_score,
                "cost":        s.cost_score,
                "speed":       s.speed_score,
                "reliability": s.reliability_score,
            }
            weakest = min(dims, key=dims.get)
            suggestions = {
                "quality":     "Replace with a higher-accuracy model or refine the prompt.",
                "cost":        "Switch to a cheaper model or reduce output token budget.",
                "speed":       "Switch to a faster model variant or reduce context size.",
                "reliability": "Add retry logic, fallback model, or simplify the task.",
            }

            candidates.append(RetirementCandidate(
                stage_name             = s.stage_name,
                composite_score        = s.composite_score,
                reason                 = (
                    f"Composite score {s.composite_score:.2f} is below "
                    f"retirement threshold {THRESHOLD_OPTIMIZE:.2f}. "
                    f"Weakest dimension: {weakest} ({dims[weakest]:.2f})."
                ),
                evidence               = {
                    "quality_score":     s.quality_score,
                    "cost_score":        s.cost_score,
                    "speed_score":       s.speed_score,
                    "reliability_score": s.reliability_score,
                    "composite_score":   s.composite_score,
                    "non_skip_count":    s.non_skip_count,
                },
                replacement_suggestion = suggestions[weakest],
                urgency                = urgency,
            ))

        return sorted(candidates, key=lambda c: c.composite_score)
    except Exception:
        return []


# ===========================================================================
# Upgrade Recommendations
# ===========================================================================

def find_upgrade_recommendations(scores: list) -> list:
    """
    Find agents with one or more dimensions below the upgrade threshold.

    An agent may produce multiple recommendations (one per weak dimension).
    Retirement candidates are excluded — they get RetirementCandidate
    records instead.

    Returns:
        List of UpgradeRecommendation, sorted by (stage_name, dimension).

    Non-blocking: returns [] on any error.
    """
    try:
        recommendations: list[UpgradeRecommendation] = []

        for s in scores:
            # Skip retirement candidates
            if s.recommendation == "retire":
                continue

            # Quality
            if s.quality_score < UPGRADE_QUALITY_THRESHOLD and s.non_skip_count > 0:
                recommendations.append(UpgradeRecommendation(
                    stage_name           = s.stage_name,
                    dimension            = "quality",
                    current_score        = s.quality_score,
                    target_score         = UPGRADE_QUALITY_THRESHOLD,
                    suggested_action     = (
                        "Review failure logs for this stage. Consider upgrading "
                        "the model, refining the prompt, or adding input validation."
                    ),
                    expected_improvement = (
                        f"Raising quality from {s.quality_score:.0%} to "
                        f"{UPGRADE_QUALITY_THRESHOLD:.0%} improves the "
                        f"composite score by ~{(UPGRADE_QUALITY_THRESHOLD - s.quality_score) * WEIGHT_QUALITY:.2f}."
                    ),
                ))

            # Cost
            if s.cost_score < UPGRADE_COST_THRESHOLD:
                approx_cost = (1.0 - s.cost_score) * COST_CEILING_USD
                recommendations.append(UpgradeRecommendation(
                    stage_name           = s.stage_name,
                    dimension            = "cost",
                    current_score        = s.cost_score,
                    target_score         = UPGRADE_COST_THRESHOLD,
                    suggested_action     = (
                        f"Average cost ~${approx_cost:.3f}/run. Switch to a "
                        "cheaper model variant or reduce prompt size to lower cost."
                    ),
                    expected_improvement = (
                        f"Reducing cost score gap from {s.cost_score:.2f} to "
                        f"{UPGRADE_COST_THRESHOLD:.2f} saves ~"
                        f"${(UPGRADE_COST_THRESHOLD - s.cost_score) * COST_CEILING_USD:.3f}/run."
                    ),
                ))

            # Speed
            if s.speed_score < UPGRADE_SPEED_THRESHOLD:
                approx_ms = (1.0 - s.speed_score) * SPEED_CEILING_MS
                recommendations.append(UpgradeRecommendation(
                    stage_name           = s.stage_name,
                    dimension            = "speed",
                    current_score        = s.speed_score,
                    target_score         = UPGRADE_SPEED_THRESHOLD,
                    suggested_action     = (
                        f"Average duration ~{approx_ms/1000:.1f}s. Switch to a "
                        "faster model, reduce context, or split into smaller stages."
                    ),
                    expected_improvement = (
                        f"Reducing duration from {approx_ms/1000:.1f}s to "
                        f"{(1 - UPGRADE_SPEED_THRESHOLD) * SPEED_CEILING_MS / 1000:.1f}s "
                        "reduces end-to-end run time."
                    ),
                ))

            # Reliability
            if s.reliability_score < UPGRADE_RELIABILITY_THRESHOLD and s.non_skip_count > 0:
                recommendations.append(UpgradeRecommendation(
                    stage_name           = s.stage_name,
                    dimension            = "reliability",
                    current_score        = s.reliability_score,
                    target_score         = UPGRADE_RELIABILITY_THRESHOLD,
                    suggested_action     = (
                        "Add retry logic with exponential backoff, a fallback "
                        "model, or input/output validation to increase consistency."
                    ),
                    expected_improvement = (
                        f"Raising reliability from {s.reliability_score:.0%} to "
                        f"{UPGRADE_RELIABILITY_THRESHOLD:.0%} reduces unpredictable "
                        "run outcomes."
                    ),
                ))

        return sorted(recommendations, key=lambda r: (r.stage_name, r.dimension))
    except Exception:
        return []


# ===========================================================================
# Fleet Health
# ===========================================================================

def _compute_fleet_health(scores: list) -> str:
    if not scores:
        return "unknown"
    if any(s.recommendation == "retire" for s in scores):
        return "degraded"
    if any(s.recommendation in {"monitor", "optimize"} for s in scores):
        return "warning"
    return "healthy"


# ===========================================================================
# Main Entry Point
# ===========================================================================

def run_agent_director(db_path: Optional[Path] = None) -> AgentDirectorReport:
    """
    Run the Agent Director and return a complete fleet evaluation report.

    Scores all pipeline stages, identifies retirement candidates and upgrade
    opportunities, and produces ranked lists for operator review.

    Non-blocking: all sections degrade gracefully on error.

    Args:
        db_path: Override SQLite path.

    Returns:
        AgentDirectorReport.
    """
    scores   = analyze_agents(db_path)
    retiring = find_retirement_candidates(scores)
    upgrades = find_upgrade_recommendations(scores)
    health   = _compute_fleet_health(scores)

    healthy   = [s.stage_name for s in scores if s.recommendation == "healthy"]
    attention = [s.stage_name for s in sorted(scores, key=lambda s: s.composite_score)
                 if s.recommendation != "healthy"]
    cost_rank  = [s.stage_name for s in sorted(scores, key=lambda s: -s.cost_score)]
    speed_rank = [s.stage_name for s in sorted(scores, key=lambda s: -s.speed_score)]

    return AgentDirectorReport(
        analyzed_at              = _now_iso(),
        total_agents_evaluated   = len(scores),
        agent_scores             = scores,
        retirement_candidates    = retiring,
        upgrade_recommendations  = upgrades,
        healthy_agents           = healthy,
        agents_needing_attention = attention,
        cost_efficiency_ranking  = cost_rank,
        speed_ranking            = speed_rank,
        overall_fleet_health     = health,
    )
