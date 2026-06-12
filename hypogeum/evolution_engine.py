"""
hypogeum/evolution_engine.py — Factory Evolution Engine

Reads learning signals produced by past pipeline runs and emits structured
configuration recommendations for future runs. No network calls. No writes
to production state — all writes go to the local learning_records and
evolution_adjustments tables only.

Architecture:
    All analysis functions are non-blocking: any exception returns an
    empty/default structure rather than propagating.

    Data sources (read):
        learning_records  — per-episode signals (performance, cost, quality)
        asset_versions    — generation retry / failure rates
        stage_executions  — stage timing and failure rates
        gate_verdicts     — gate block rates

    Data sinks (write):
        evolution_adjustments — one row per recommended config change per run
                                (table created lazily, NOT in schema.sql)

Signal types consumed (from learning_records.signal_type):
    "performance"       — YouTube/engagement performance
    "gate_outcome"      — brand/QA gate pass/block
    "cost_efficiency"   — cost per episode vs. budget
    "quality_verdict"   — asset quality scores
    "topic_score"       — topic resonance signal
    "thumbnail_ctr"     — thumbnail click-through rate

Adjustment categories emitted:
    "provider_swap"     — switch provider for an asset type
    "retry_budget"      — increase/decrease retry allowance
    "cost_ceiling"      — raise or lower budget ceiling
    "gate_threshold"    — relax/tighten gate severity
    "model_upgrade"     — upgrade/downgrade model slug
    "topic_weight"      — adjust topic preference weights

Public API:
    analyze_episode(
        episode_id, db_path=None,
    ) -> EpisodeEvolutionReport

    get_provider_recommendations(
        db_path=None, episode_id=None, min_failure_rate=0.20,
    ) -> list[ProviderRecommendation]

    get_cost_recommendations(
        db_path=None, episode_id=None, budget_ceiling_usd=None,
    ) -> list[CostRecommendation]

    get_gate_recommendations(
        db_path=None, episode_id=None, max_block_rate=0.30,
    ) -> list[GateRecommendation]

    record_adjustment(
        adjustment_type, asset_type, current_value, recommended_value,
        rationale, run_id, episode_id=None, db_path=None,
    ) -> Optional[str]   # returns adjustment_id or None on error

    evolve(
        db_path=None, episode_id=None, run_id=None,
        budget_ceiling_usd=None,
    ) -> EvolutionReport
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection


# ---------------------------------------------------------------------------
# Default path
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

# Thresholds
_DEFAULT_MIN_FAILURE_RATE  = 0.20   # flag provider if failure rate ≥ 20 %
_DEFAULT_MAX_BLOCK_RATE    = 0.30   # flag gate if block rate ≥ 30 %
_MIN_SAMPLE_SIZE           = 3      # ignore signals with < 3 data points


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ProviderRecommendation:
    """Recommend switching or retaining a provider for an asset type."""
    asset_type:      str
    current_provider: str
    failure_rate:    float
    total_attempts:  int
    action:          str            # "swap" | "monitor" | "ok"
    rationale:       str
    suggested_provider: Optional[str] = None


@dataclass
class CostRecommendation:
    """Recommend a budget adjustment based on observed cost patterns."""
    scope:           str            # "episode" | "asset_type" | "provider"
    scope_value:     str            # e.g. "ep_001" or "video_clip"
    observed_cost:   float
    budget_ceiling:  Optional[float]
    action:          str            # "raise_ceiling" | "lower_ceiling" | "ok"
    rationale:       str


@dataclass
class GateRecommendation:
    """Recommend adjusting gate thresholds based on block rates."""
    gate_name:   str
    block_rate:  float
    total_evals: int
    action:      str                # "relax" | "investigate" | "ok"
    rationale:   str


@dataclass
class LearningSignalSummary:
    """Aggregated learning signal for one episode."""
    episode_id:          str
    signal_type:         str
    avg_value:           Optional[float]
    sample_count:        int
    latest_label:        Optional[str]
    latest_recommendation: Optional[str]


@dataclass
class EpisodeEvolutionReport:
    """Per-episode evolution analysis."""
    episode_id:          str
    generated_at:        str
    signals:             list[LearningSignalSummary] = field(default_factory=list)
    provider_recs:       list[ProviderRecommendation] = field(default_factory=list)
    cost_recs:           list[CostRecommendation]     = field(default_factory=list)
    gate_recs:           list[GateRecommendation]     = field(default_factory=list)


@dataclass
class EvolutionReport:
    """
    Cross-episode factory evolution report.

    Produced by evolve(). Aggregates all recommendation types for the
    factory operator to review before the next run.
    """
    generated_at:    str
    run_id:          Optional[str]
    episode_count:   int
    provider_recs:   list[ProviderRecommendation] = field(default_factory=list)
    cost_recs:       list[CostRecommendation]     = field(default_factory=list)
    gate_recs:       list[GateRecommendation]     = field(default_factory=list)
    adjustments_recorded: int = 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _safe_rate(num: int, denom: int) -> float:
    return num / denom if denom > 0 else 0.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_adjustments_table(conn) -> None:
    """Create evolution_adjustments table if not present (lazy creation)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evolution_adjustments (
            adjustment_id       TEXT    NOT NULL PRIMARY KEY,
            run_id              TEXT,
            episode_id          TEXT,
            adjustment_type     TEXT    NOT NULL,
            asset_type          TEXT,
            current_value       TEXT,
            recommended_value   TEXT,
            rationale           TEXT,
            applied             INTEGER NOT NULL DEFAULT 0,
            created_at          TEXT    NOT NULL,
            metadata_version    TEXT    NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Public API — get_provider_recommendations
# ---------------------------------------------------------------------------

def get_provider_recommendations(
    db_path:          Optional[Path] = None,
    episode_id:       Optional[str]  = None,
    min_failure_rate: float          = _DEFAULT_MIN_FAILURE_RATE,
) -> list[ProviderRecommendation]:
    """
    Analyse asset_versions to identify underperforming providers.

    Any (asset_type, provider) combination with a failure rate ≥
    min_failure_rate AND at least _MIN_SAMPLE_SIZE attempts is flagged.

    Non-blocking: returns [] on any exception.

    Args:
        db_path:          Override SQLite path.
        episode_id:       Restrict to one episode if provided.
        min_failure_rate: Failure rate threshold (0–1). Default 0.20.

    Returns:
        List of ProviderRecommendation sorted by failure_rate descending.
    """
    try:
        clauses = ["1=1"]
        params: list = []
        if episode_id:
            clauses.append("episode_id = ?")
            params.append(episode_id)

        where = " AND ".join(clauses)
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute(
                f"""
                SELECT
                    asset_type,
                    provider,
                    COUNT(*)                                                    AS total,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END)           AS failures,
                    SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END)        AS successes
                FROM asset_versions
                WHERE {where}
                GROUP BY asset_type, provider
                ORDER BY asset_type, provider
                """,
                params,
            ).fetchall()
        finally:
            conn.close()

        results: list[ProviderRecommendation] = []
        for row in rows:
            total    = row["total"] or 0
            failures = row["failures"] or 0
            if total < _MIN_SAMPLE_SIZE:
                continue
            rate = _safe_rate(failures, total)
            if rate >= min_failure_rate:
                action = "swap" if rate >= 0.50 else "monitor"
                rationale = (
                    f"{row['provider']} failed {failures}/{total} attempts "
                    f"({rate:.0%}) for {row['asset_type']}"
                )
                results.append(ProviderRecommendation(
                    asset_type       = row["asset_type"],
                    current_provider = row["provider"],
                    failure_rate     = rate,
                    total_attempts   = total,
                    action           = action,
                    rationale        = rationale,
                ))

        results.sort(key=lambda r: r.failure_rate, reverse=True)
        return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public API — get_cost_recommendations
# ---------------------------------------------------------------------------

def get_cost_recommendations(
    db_path:            Optional[Path]  = None,
    episode_id:         Optional[str]   = None,
    budget_ceiling_usd: Optional[float] = None,
) -> list[CostRecommendation]:
    """
    Analyse asset_versions cost data to identify budget pressure.

    If budget_ceiling_usd is provided, episodes exceeding it are flagged
    with a "raise_ceiling" recommendation. Episodes well under budget
    (< 60 %) receive a "lower_ceiling" suggestion.

    Non-blocking: returns [] on any exception.

    Args:
        db_path:            Override SQLite path.
        episode_id:         Restrict to one episode if provided.
        budget_ceiling_usd: Budget cap per episode for comparison.

    Returns:
        List of CostRecommendation.
    """
    try:
        clauses = ["status='succeeded'"]
        params: list = []
        if episode_id:
            clauses.append("episode_id = ?")
            params.append(episode_id)

        where = " AND ".join(clauses)
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute(
                f"""
                SELECT
                    episode_id,
                    COALESCE(SUM(cost_usd), 0) AS total_cost
                FROM asset_versions
                WHERE {where}
                GROUP BY episode_id
                ORDER BY episode_id
                """,
                params,
            ).fetchall()
        finally:
            conn.close()

        results: list[CostRecommendation] = []
        for row in rows:
            ep_cost  = float(row["total_cost"] or 0)
            ceiling  = budget_ceiling_usd

            if ceiling is not None and ep_cost > ceiling:
                action    = "raise_ceiling"
                rationale = (
                    f"Episode {row['episode_id']} cost ${ep_cost:.3f} "
                    f"exceeds ceiling ${ceiling:.3f}"
                )
            elif ceiling is not None and ceiling > 0 and ep_cost < ceiling * 0.60:
                action    = "lower_ceiling"
                rationale = (
                    f"Episode {row['episode_id']} cost ${ep_cost:.3f} "
                    f"is <60% of ceiling ${ceiling:.3f} — ceiling can be reduced"
                )
            else:
                action    = "ok"
                rationale = f"Episode {row['episode_id']} cost ${ep_cost:.3f} within budget"

            results.append(CostRecommendation(
                scope           = "episode",
                scope_value     = row["episode_id"],
                observed_cost   = ep_cost,
                budget_ceiling  = ceiling,
                action          = action,
                rationale       = rationale,
            ))

        return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public API — get_gate_recommendations
# ---------------------------------------------------------------------------

def get_gate_recommendations(
    db_path:       Optional[Path] = None,
    episode_id:    Optional[str]  = None,
    max_block_rate: float         = _DEFAULT_MAX_BLOCK_RATE,
) -> list[GateRecommendation]:
    """
    Analyse gate_verdicts block rates and flag problematic gates.

    Gates with block_rate ≥ max_block_rate AND at least _MIN_SAMPLE_SIZE
    evaluations are flagged for investigation or threshold relaxation.

    Non-blocking: returns [] on any exception.

    Args:
        db_path:        Override SQLite path.
        episode_id:     Restrict to one episode if provided.
        max_block_rate: Block rate threshold (0–1). Default 0.30.

    Returns:
        List of GateRecommendation sorted by block_rate descending.
    """
    try:
        clauses = ["1=1"]
        params: list = []
        if episode_id:
            clauses.append("episode_id = ?")
            params.append(episode_id)

        where = " AND ".join(clauses)
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute(
                f"""
                SELECT
                    gate_name,
                    COUNT(*) AS total,
                    SUM(CASE WHEN verdict IN ('REJECTED','BLOCKED')
                             THEN 1 ELSE 0 END) AS blocks
                FROM gate_verdicts
                WHERE {where}
                GROUP BY gate_name
                ORDER BY gate_name
                """,
                params,
            ).fetchall()
        finally:
            conn.close()

        results: list[GateRecommendation] = []
        for row in rows:
            total  = row["total"]  or 0
            blocks = row["blocks"] or 0
            if total < _MIN_SAMPLE_SIZE:
                continue
            rate = _safe_rate(blocks, total)
            if rate >= max_block_rate:
                action = "investigate" if rate >= 0.50 else "relax"
                rationale = (
                    f"Gate '{row['gate_name']}' blocked {blocks}/{total} "
                    f"evaluations ({rate:.0%})"
                )
                results.append(GateRecommendation(
                    gate_name   = row["gate_name"],
                    block_rate  = rate,
                    total_evals = total,
                    action      = action,
                    rationale   = rationale,
                ))

        results.sort(key=lambda r: r.block_rate, reverse=True)
        return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public API — analyze_episode
# ---------------------------------------------------------------------------

def analyze_episode(
    episode_id: str,
    db_path:    Optional[Path] = None,
) -> EpisodeEvolutionReport:
    """
    Produce a full evolution report for one episode.

    Combines learning signal summaries with provider, cost, and gate
    recommendations scoped to the given episode.

    Non-blocking: returns a partial report on any exception.

    Args:
        episode_id: Episode to analyse.
        db_path:    Override SQLite path.

    Returns:
        EpisodeEvolutionReport.
    """
    signals: list[LearningSignalSummary] = []
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute(
                """
                SELECT
                    signal_type,
                    AVG(signal_value)   AS avg_val,
                    COUNT(*)            AS cnt,
                    MAX(signal_label)   AS latest_label,
                    MAX(recommendation) AS latest_rec
                FROM learning_records
                WHERE episode_id = ?
                GROUP BY signal_type
                ORDER BY signal_type
                """,
                [episode_id],
            ).fetchall()
        finally:
            conn.close()

        for row in rows:
            signals.append(LearningSignalSummary(
                episode_id             = episode_id,
                signal_type            = row["signal_type"],
                avg_value              = row["avg_val"],
                sample_count           = row["cnt"] or 0,
                latest_label           = row["latest_label"],
                latest_recommendation  = row["latest_rec"],
            ))
    except Exception:
        pass

    return EpisodeEvolutionReport(
        episode_id   = episode_id,
        generated_at = _now_iso(),
        signals      = signals,
        provider_recs = get_provider_recommendations(db_path=db_path, episode_id=episode_id),
        cost_recs    = get_cost_recommendations(db_path=db_path, episode_id=episode_id),
        gate_recs    = get_gate_recommendations(db_path=db_path, episode_id=episode_id),
    )


# ---------------------------------------------------------------------------
# Public API — record_adjustment
# ---------------------------------------------------------------------------

def record_adjustment(
    adjustment_type:   str,
    asset_type:        Optional[str],
    current_value:     str,
    recommended_value: str,
    rationale:         str,
    run_id:            Optional[str],
    episode_id:        Optional[str]  = None,
    db_path:           Optional[Path] = None,
) -> Optional[str]:
    """
    Persist a recommended configuration adjustment to evolution_adjustments.

    Non-blocking: returns None on any exception.

    Args:
        adjustment_type:   One of the adjustment category strings.
        asset_type:        Asset type this adjustment applies to (may be None).
        current_value:     Current config value as a string.
        recommended_value: Suggested new value as a string.
        rationale:         Human-readable justification.
        run_id:            Run that triggered this recommendation.
        episode_id:        Episode context (optional).
        db_path:           Override SQLite path.

    Returns:
        adjustment_id (str) or None on failure.
    """
    try:
        adjustment_id = str(uuid.uuid4())
        conn = get_connection(_resolve_db_path(db_path))
        try:
            _ensure_adjustments_table(conn)
            conn.execute(
                """
                INSERT INTO evolution_adjustments
                    (adjustment_id, run_id, episode_id, adjustment_type,
                     asset_type, current_value, recommended_value,
                     rationale, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (adjustment_id, run_id, episode_id, adjustment_type,
                 asset_type, current_value, recommended_value,
                 rationale, _now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
        return adjustment_id
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API — evolve (full report + optional persistence)
# ---------------------------------------------------------------------------

def evolve(
    db_path:            Optional[Path]  = None,
    episode_id:         Optional[str]   = None,
    run_id:             Optional[str]   = None,
    budget_ceiling_usd: Optional[float] = None,
    record:             bool            = False,
) -> EvolutionReport:
    """
    Generate a cross-episode factory evolution report and optionally
    persist the top recommendations as evolution_adjustments rows.

    Non-blocking: partial results returned on any sub-query failure.

    Args:
        db_path:            Override SQLite path.
        episode_id:         Restrict analysis to one episode if provided.
        run_id:             Associated run ID used when recording adjustments.
        budget_ceiling_usd: Budget cap for cost recommendations.
        record:             If True, persist recommendations to
                            evolution_adjustments table.

    Returns:
        EvolutionReport with all recommendation lists populated.
    """
    provider_recs = get_provider_recommendations(
        db_path=db_path, episode_id=episode_id,
    )
    cost_recs = get_cost_recommendations(
        db_path=db_path, episode_id=episode_id,
        budget_ceiling_usd=budget_ceiling_usd,
    )
    gate_recs = get_gate_recommendations(
        db_path=db_path, episode_id=episode_id,
    )

    # Count distinct episodes analysed
    episode_count = 0
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            if episode_id:
                episode_count = 1
            else:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT episode_id) AS cnt FROM runs"
                ).fetchone()
                episode_count = row["cnt"] if row else 0
        finally:
            conn.close()
    except Exception:
        pass

    adjustments_recorded = 0
    if record and run_id:
        for rec in provider_recs:
            if rec.action in ("swap", "monitor"):
                aid = record_adjustment(
                    adjustment_type   = "provider_swap",
                    asset_type        = rec.asset_type,
                    current_value     = rec.current_provider,
                    recommended_value = rec.suggested_provider or "review",
                    rationale         = rec.rationale,
                    run_id            = run_id,
                    episode_id        = episode_id,
                    db_path           = db_path,
                )
                if aid:
                    adjustments_recorded += 1

        for rec in cost_recs:
            if rec.action != "ok":
                aid = record_adjustment(
                    adjustment_type   = "cost_ceiling",
                    asset_type        = None,
                    current_value     = str(rec.budget_ceiling),
                    recommended_value = str(rec.observed_cost),
                    rationale         = rec.rationale,
                    run_id            = run_id,
                    episode_id        = episode_id,
                    db_path           = db_path,
                )
                if aid:
                    adjustments_recorded += 1

        for rec in gate_recs:
            if rec.action in ("investigate", "relax"):
                aid = record_adjustment(
                    adjustment_type   = "gate_threshold",
                    asset_type        = None,
                    current_value     = rec.gate_name,
                    recommended_value = rec.action,
                    rationale         = rec.rationale,
                    run_id            = run_id,
                    episode_id        = episode_id,
                    db_path           = db_path,
                )
                if aid:
                    adjustments_recorded += 1

    return EvolutionReport(
        generated_at         = _now_iso(),
        run_id               = run_id,
        episode_count        = episode_count,
        provider_recs        = provider_recs,
        cost_recs            = cost_recs,
        gate_recs            = gate_recs,
        adjustments_recorded = adjustments_recorded,
    )
