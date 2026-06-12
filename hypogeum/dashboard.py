"""
hypogeum/dashboard.py — Factory Dashboard

Read-only aggregator that produces a structured snapshot of factory
health. Combines data from all sub-systems into one DashboardSnapshot.

Sub-systems queried:
    run_health_summary()        — from agent_auditor
    audit_agents()              — stage + gate + asset metrics
    evolve()                    — active recommendations
    list_channels()             — registered channels
    get_supported_platforms()   — available platform connectors

Architecture:
    All functions are read-only and non-blocking. Any exception in a
    sub-query degrades gracefully — the snapshot is returned with whatever
    data is available.

Public API:
    snapshot(db_path=None, episode_id=None) -> DashboardSnapshot
    format_text(snapshot) -> str   # terminal-friendly text summary
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.agent_auditor import (
    AgentAuditReport,
    RunHealthSummary,
    audit_agents,
    run_health_summary,
)
from hypogeum.channel_manager import Channel, list_channels
from hypogeum.evolution_engine import EvolutionReport, evolve
from hypogeum.publishing.connectors import get_supported_platforms


# ---------------------------------------------------------------------------
# Default path
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


# ---------------------------------------------------------------------------
# Snapshot dataclass
# ---------------------------------------------------------------------------

@dataclass
class DashboardSnapshot:
    """
    Point-in-time factory health snapshot.

    All sub-reports are included even if partially populated. The
    snapshot_status field reflects overall factory health:
        "healthy"   — completion_rate ≥ 0.80 and no high-priority recs
        "warning"   — completion_rate < 0.80 or provider/gate recs exist
        "degraded"  — completion_rate < 0.50
        "unknown"   — no run data
    """
    generated_at:        str
    episode_id:          Optional[str]
    snapshot_status:     str                      # "healthy"|"warning"|"degraded"|"unknown"
    run_health:          Optional[RunHealthSummary]
    audit:               Optional[AgentAuditReport]
    evolution:           Optional[EvolutionReport]
    channels:            list[Channel]            = field(default_factory=list)
    supported_platforms: list[str]                = field(default_factory=list)
    alerts:              list[str]                = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_status(
    health: Optional[RunHealthSummary],
    evolution: Optional[EvolutionReport],
) -> str:
    if health is None or health.total_runs == 0:
        return "unknown"
    rate = health.completion_rate
    if rate < 0.50:
        return "degraded"
    has_recs = (
        evolution is not None
        and (evolution.provider_recs or evolution.gate_recs)
    )
    if rate < 0.80 or has_recs:
        return "warning"
    return "healthy"


def _build_alerts(
    health: Optional[RunHealthSummary],
    evolution: Optional[EvolutionReport],
    audit: Optional[AgentAuditReport],
) -> list[str]:
    alerts: list[str] = []

    if health and health.total_runs > 0 and health.completion_rate < 0.50:
        alerts.append(
            f"CRITICAL: run completion rate {health.completion_rate:.0%} "
            f"({health.failed} failed / {health.total_runs} total)"
        )

    if evolution:
        for rec in evolution.provider_recs:
            if rec.action == "swap":
                alerts.append(
                    f"Provider swap recommended: {rec.current_provider} "
                    f"for {rec.asset_type} ({rec.failure_rate:.0%} failure rate)"
                )
        for rec in evolution.gate_recs:
            if rec.action == "investigate":
                alerts.append(
                    f"Gate '{rec.gate_name}' blocked {rec.block_rate:.0%} "
                    f"of evaluations — investigate"
                )

    if audit:
        for stage in audit.stages:
            if stage.total_executions > 2 and stage.success_rate < 0.50:
                alerts.append(
                    f"Stage '{stage.stage_name}' success rate "
                    f"{stage.success_rate:.0%} — check logs"
                )

    return alerts


# ---------------------------------------------------------------------------
# Public API — snapshot
# ---------------------------------------------------------------------------

def snapshot(
    db_path:    Optional[Path] = None,
    episode_id: Optional[str]  = None,
) -> DashboardSnapshot:
    """
    Generate a point-in-time factory health snapshot.

    Queries all sub-systems non-blocking. Partial results are returned if
    any sub-query fails. The snapshot_status field summarises overall health.

    Args:
        db_path:    Override SQLite path.
        episode_id: Restrict pipeline metrics to one episode if provided.

    Returns:
        DashboardSnapshot.
    """
    health:    Optional[RunHealthSummary]  = None
    audit_rep: Optional[AgentAuditReport] = None
    evo_rep:   Optional[EvolutionReport]  = None
    channels:  list[Channel]              = []

    try:
        health = run_health_summary(db_path=db_path, episode_id=episode_id)
    except Exception:
        pass

    try:
        audit_rep = audit_agents(db_path=db_path, episode_id=episode_id)
    except Exception:
        pass

    try:
        evo_rep = evolve(db_path=db_path, episode_id=episode_id)
    except Exception:
        pass

    try:
        channels = list_channels(db_path=db_path)
    except Exception:
        pass

    status = _compute_status(health, evo_rep)
    alerts = _build_alerts(health, evo_rep, audit_rep)

    return DashboardSnapshot(
        generated_at        = _now_iso(),
        episode_id          = episode_id,
        snapshot_status     = status,
        run_health          = health,
        audit               = audit_rep,
        evolution           = evo_rep,
        channels            = channels,
        supported_platforms = get_supported_platforms(),
        alerts              = alerts,
    )


# ---------------------------------------------------------------------------
# Public API — format_text
# ---------------------------------------------------------------------------

def format_text(snap: DashboardSnapshot) -> str:
    """
    Render a DashboardSnapshot as a terminal-friendly text summary.

    No external calls. Pure string formatting.

    Args:
        snap: DashboardSnapshot to format.

    Returns:
        Multi-line string suitable for print() or logging.
    """
    lines: list[str] = []
    sep = "─" * 60

    lines.append(sep)
    lines.append("  Hypogeum Factory Dashboard")
    lines.append(f"  {snap.generated_at}")
    if snap.episode_id:
        lines.append(f"  Episode: {snap.episode_id}")
    lines.append(f"  Status: {snap.snapshot_status.upper()}")
    lines.append(sep)

    # Alerts
    if snap.alerts:
        lines.append("ALERTS:")
        for alert in snap.alerts:
            lines.append(f"  ⚠  {alert}")
        lines.append("")

    # Run health
    if snap.run_health:
        h = snap.run_health
        lines.append("Run Health:")
        lines.append(f"  Total runs    : {h.total_runs}")
        lines.append(f"  Completed     : {h.completed}")
        lines.append(f"  Failed        : {h.failed}")
        lines.append(f"  In progress   : {h.in_progress}")
        lines.append(f"  Completion    : {h.completion_rate:.0%}")
        if h.total_cost_usd:
            lines.append(f"  Total cost    : ${h.total_cost_usd:.3f}")
        lines.append("")

    # Stages
    if snap.audit and snap.audit.stages:
        lines.append("Stage Performance:")
        for s in snap.audit.stages:
            lines.append(
                f"  {s.stage_name:<30} "
                f"ok={s.successes}/{s.total_executions - s.skips} "
                f"({s.success_rate:.0%})"
            )
        lines.append("")

    # Gate pass rates
    if snap.audit and snap.audit.gates:
        lines.append("Gate Pass Rates:")
        for g in snap.audit.gates:
            lines.append(
                f"  {g.gate_name:<30} "
                f"pass={g.passes}/{g.total_evaluations} "
                f"({g.pass_rate:.0%})"
            )
        lines.append("")

    # Evolution recommendations
    if snap.evolution:
        provider_actions = [r for r in snap.evolution.provider_recs
                            if r.action in ("swap", "monitor")]
        gate_actions = snap.evolution.gate_recs
        if provider_actions or gate_actions:
            lines.append("Recommendations:")
            for rec in provider_actions:
                lines.append(f"  [provider] {rec.action}: {rec.rationale}")
            for rec in gate_actions:
                lines.append(f"  [gate]     {rec.action}: {rec.rationale}")
            lines.append("")

    # Channels
    if snap.channels:
        lines.append(f"Channels ({len(snap.channels)}):")
        for ch in snap.channels:
            lines.append(f"  {ch.channel_id:<20} platform={ch.platform}")
        lines.append("")

    # Supported platforms
    if snap.supported_platforms:
        lines.append("Platforms: " + ", ".join(snap.supported_platforms))
        lines.append("")

    lines.append(sep)
    return "\n".join(lines)
