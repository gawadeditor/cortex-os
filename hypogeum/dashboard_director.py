"""
hypogeum/dashboard_director.py — Phase D: Dashboard Director Agent

A rule-based analytical agent that inspects FactoryState and the live DB
to discover observability gaps, propose new KPIs, widgets, alerts, and
management improvements. All analysis is deterministic and testable.

Architecture:
    - Reads FactoryState (pre-computed) and/or issues direct DB queries.
    - No LLM calls. No network. Non-blocking. Pure analysis.
    - Every function returns typed dataclasses or safe defaults on error.

Sub-components:
    MetricGap Detector    — finds data that exists but isn't surfaced
    KPI Proposer          — proposes new key performance indicators
    Widget Proposer       — proposes dashboard widget types
    Alert Proposer        — proposes new alert conditions
    Management Proposer   — proposes operational improvements
    Priority Ranker       — orders all proposals by impact/effort ratio

Public API:
    analyze_metric_gaps(state, db_path=None)        -> list[MetricGap]
    propose_kpis(state, db_path=None)               -> list[KPIProposal]
    propose_widgets(state)                          -> list[WidgetProposal]
    propose_alerts(state)                           -> list[AlertProposal]
    propose_management_improvements(state, db_path) -> list[ManagementProposal]
    compute_priority_order(metric_gaps, kpis, widgets, alerts, mgmt)
                                                    -> list[str]
    run_director(db_path=None, factory_id="hypogeum")
                                                    -> DashboardDirectorReport
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection
from hypogeum.dashboard_api import (
    FactoryState,
    get_factory_state,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
METADATA_VERSION = "v1"

_IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2}
_EFFORT_ORDER = {"low": 0, "medium": 1, "high": 2}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_row_count(conn, table: str) -> int:
    """Return count of rows in table, 0 if table missing."""
    try:
        return conn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()["cnt"]
    except Exception:
        return 0


def _table_exists(conn, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            [table],
        ).fetchone()
        return row is not None
    except Exception:
        return False


# ===========================================================================
# Data Contracts
# ===========================================================================

@dataclass
class MetricGap:
    """
    Observability gap: data that exists in the DB but isn't being surfaced
    in the current dashboard state.
    """
    gap_id:      str    # stable snake_case identifier
    title:       str
    description: str
    data_source: str    # table(s) that hold the raw data
    impact:      str    # "high" | "medium" | "low"
    effort:      str    # "low" | "medium" | "high" (to implement)
    query_hint:  str    # pseudo-SQL showing how to compute it


@dataclass
class KPIProposal:
    """A new Key Performance Indicator to track."""
    kpi_id:        str
    title:         str
    description:   str
    formula:       str          # human-readable formula
    target:        str          # what "good" looks like
    current_value: Optional[float]  # computed if data available, else None
    unit:          str          # "$", "%", "episodes", "ms", etc.


@dataclass
class WidgetProposal:
    """A new dashboard widget to display."""
    widget_id:   str
    title:       str
    widget_type: str    # "line_chart"|"bar_chart"|"pie_chart"|"grid"|"gauge"|"table"
    description: str
    data_source: str
    priority:    int    # lower = higher priority


@dataclass
class AlertProposal:
    """A new alert condition to configure."""
    alert_id:    str
    title:       str
    condition:   str    # human-readable trigger condition
    level:       str    # "critical" | "warning"
    description: str
    query_hint:  str


@dataclass
class ManagementProposal:
    """An operational improvement for the factory operator."""
    proposal_id: str
    title:       str
    description: str
    action:      str    # what to do
    reason:      str    # why it matters
    priority:    str    # "urgent" | "recommended" | "optional"


@dataclass
class DashboardDirectorReport:
    """
    Complete analysis from the Dashboard Director Agent.

    priority_order contains all proposal IDs sorted by impact/effort ratio
    (high impact, low effort first). Use this list to decide what to
    implement next on the dashboard.
    """
    analyzed_at:         str
    factory_id:          str
    missing_metrics:     list
    kpi_proposals:       list
    widget_proposals:    list
    alert_proposals:     list
    management_proposals: list
    priority_order:      list    # list[str] — proposal IDs, highest priority first
    total_proposals:     int
    metadata_version:    str = METADATA_VERSION


# ===========================================================================
# MetricGap Detector
# ===========================================================================

# Catalogue of all possible metric gaps
_ALL_GAPS = [
    MetricGap(
        gap_id="gate_pass_rate_trend",
        title="Gate Pass Rate Trends",
        description="gate_verdicts rows exist but week-over-week pass rate "
                    "trends are not surfaced. Operators cannot see whether "
                    "brand compliance is improving or degrading over time.",
        data_source="gate_verdicts, v_gate_pass_rates",
        impact="high",
        effort="low",
        query_hint=(
            "SELECT gate_name, strftime('%Y-W%W', evaluated_at) AS week, "
            "SUM(CASE WHEN verdict IN ('APPROVED','APPROVED_WITH_FLAGS') "
            "THEN 1 ELSE 0 END)*1.0/COUNT(*) AS pass_rate "
            "FROM gate_verdicts GROUP BY gate_name, week ORDER BY week"
        ),
    ),
    MetricGap(
        gap_id="budget_utilization",
        title="Budget Utilization Rate",
        description="budget_authorizations table is not yet exposed. "
                    "Operators cannot see what fraction of the budget "
                    "ceiling is being used per episode.",
        data_source="budget_authorizations",
        impact="high",
        effort="low",
        query_hint=(
            "SELECT episode_id, estimated_cost_usd / budget_ceiling_usd "
            "AS utilization_rate FROM budget_authorizations"
        ),
    ),
    MetricGap(
        gap_id="episode_cost_trend",
        title="Episode Cost Trend",
        description="Costs exist per episode but no time-series trend is "
                    "surfaced. Without this, cost creep is invisible.",
        data_source="costs, v_episode_costs",
        impact="high",
        effort="low",
        query_hint=(
            "SELECT episode_id, SUM(cost_usd) AS total, "
            "strftime('%Y-%m-%d', called_at) AS date "
            "FROM costs GROUP BY episode_id ORDER BY date"
        ),
    ),
    MetricGap(
        gap_id="asset_retry_waste",
        title="Asset Retry Waste",
        description="asset_versions tracks every attempt including failures "
                    "but total wasted cost (failed attempts) is not surfaced.",
        data_source="asset_versions, v_asset_retry_rates",
        impact="medium",
        effort="low",
        query_hint=(
            "SELECT provider, asset_type, "
            "SUM(CASE WHEN status='failed' THEN COALESCE(cost_usd,0) END) "
            "AS wasted_usd FROM asset_versions GROUP BY provider, asset_type"
        ),
    ),
    MetricGap(
        gap_id="knowledge_graph_coverage",
        title="Knowledge Graph Entity Coverage",
        description="kg_entities rows exist but cross-episode entity "
                    "coverage (how many topics have been covered) is "
                    "not tracked as a metric.",
        data_source="kg_entities, kg_seeds",
        impact="medium",
        effort="medium",
        query_hint=(
            "SELECT entity_type, COUNT(DISTINCT entity_name) AS unique_entities, "
            "COUNT(DISTINCT episode_id) AS episodes_covered "
            "FROM kg_entities GROUP BY entity_type"
        ),
    ),
    MetricGap(
        gap_id="learning_application_rate",
        title="Learning Application Rate",
        description="learning_records.applied_to_run column tracks whether "
                    "each learning signal was actually used in a future run, "
                    "but this application rate is never surfaced.",
        data_source="learning_records",
        impact="medium",
        effort="low",
        query_hint=(
            "SELECT signal_type, "
            "SUM(CASE WHEN applied_to_run IS NOT NULL THEN 1 ELSE 0 END) "
            "* 1.0 / COUNT(*) AS application_rate "
            "FROM learning_records GROUP BY signal_type"
        ),
    ),
    MetricGap(
        gap_id="channel_production_rate",
        title="Per-Channel Episode Production Rate",
        description="Multiple channels may be configured but episode "
                    "production rate per channel over time is not tracked.",
        data_source="channel_profiles, channel_episodes, episodes",
        impact="medium",
        effort="medium",
        query_hint=(
            "SELECT channel_id, strftime('%Y-%m', e.created_at) AS month, "
            "COUNT(*) AS episodes_produced "
            "FROM channel_episodes ce JOIN episodes e USING(episode_id) "
            "GROUP BY channel_id, month ORDER BY month"
        ),
    ),
    MetricGap(
        gap_id="thumbnail_ctr_correlation",
        title="Thumbnail CTR Correlation",
        description="thumbnail_candidates and signal_windows both exist but "
                    "the correlation between thumbnail text/placement and "
                    "observed CTR is never computed.",
        data_source="thumbnail_candidates, signal_windows",
        impact="low",
        effort="high",
        query_hint=(
            "SELECT tc.text_overlay, tc.placement, AVG(sw.ctr) AS avg_ctr "
            "FROM thumbnail_candidates tc "
            "JOIN signal_windows sw USING(episode_id) "
            "WHERE tc.selected=1 GROUP BY tc.text_overlay, tc.placement"
        ),
    ),
    MetricGap(
        gap_id="render_timeline_density",
        title="Render Timeline Scene Density",
        description="render_timelines tracks scene-to-clip mapping but "
                    "average scenes per episode and clip duration variance "
                    "are not measured.",
        data_source="render_timelines",
        impact="low",
        effort="low",
        query_hint=(
            "SELECT episode_id, COUNT(*) AS scene_count, "
            "AVG(duration_ms) AS avg_clip_ms "
            "FROM render_timelines GROUP BY episode_id"
        ),
    ),
]

_GAP_TRIGGERS: dict = {
    # gap_id -> (table_to_check, min_rows_to_fire)
    "gate_pass_rate_trend":    ("gate_verdicts",         1),
    "budget_utilization":      ("budget_authorizations", 1),
    "episode_cost_trend":      ("costs",                 2),
    "asset_retry_waste":       ("asset_versions",        1),
    "knowledge_graph_coverage":("kg_entities",           1),
    "learning_application_rate":("learning_records",     1),
    "channel_production_rate": ("channel_profiles",      2),
    "thumbnail_ctr_correlation":("thumbnail_candidates", 1),
    "render_timeline_density": ("render_timelines",      1),
}


def analyze_metric_gaps(
    state:   FactoryState,
    db_path: Optional[Path] = None,
) -> list:
    """
    Detect observability gaps: data that exists in the DB but isn't surfaced.

    Each gap fires only when the underlying table has enough rows to make
    the metric meaningful. Returns a list of MetricGap sorted by impact.

    Non-blocking: returns [] on any error.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            gaps = []
            gap_map = {g.gap_id: g for g in _ALL_GAPS}
            for gap_id, (table, min_rows) in _GAP_TRIGGERS.items():
                count = _table_row_count(conn, table)
                if count >= min_rows:
                    gap = gap_map.get(gap_id)
                    if gap:
                        gaps.append(gap)
        finally:
            conn.close()

        # Sort: high impact first, then low effort
        return sorted(
            gaps,
            key=lambda g: (_IMPACT_ORDER.get(g.impact, 9),
                           _EFFORT_ORDER.get(g.effort, 9)),
        )
    except Exception:
        return []


# ===========================================================================
# KPI Proposer
# ===========================================================================

def propose_kpis(
    state:   FactoryState,
    db_path: Optional[Path] = None,
) -> list:
    """
    Propose KPIs based on available data in the factory state.

    Computes current_value where data is already available in the state.
    Non-blocking: returns [] on error.
    """
    try:
        proposals: list[KPIProposal] = []

        # --- Cost per episode ---
        if state.costs.episode_count > 0:
            cost_per_ep = round(state.costs.total_usd / state.costs.episode_count, 4)
        else:
            cost_per_ep = None

        proposals.append(KPIProposal(
            kpi_id        = "cost_per_episode",
            title         = "Cost per Episode",
            description   = "Average total production cost per episode, "
                            "covering all providers and models.",
            formula       = "total_costs_usd / episode_count",
            target        = "< $5.00 per episode",
            current_value = cost_per_ep,
            unit          = "$",
        ))

        # --- Run success rate ---
        proposals.append(KPIProposal(
            kpi_id        = "run_success_rate",
            title         = "Run Success Rate",
            description   = "Fraction of pipeline runs that complete "
                            "without error.",
            formula       = "complete_runs / (complete_runs + failed_runs)",
            target        = ">= 80%",
            current_value = state.run_health.success_rate if state.run_health.total_runs > 0 else None,
            unit          = "%",
        ))

        # --- Gate pass rate (aggregate) ---
        if state.agent_health:
            gate_pass_rates = [
                a.success_rate for a in state.agent_health
                if "gate" in a.stage_name.lower() or "bga" in a.stage_name.lower()
            ]
            gate_rate = (
                round(sum(gate_pass_rates) / len(gate_pass_rates), 4)
                if gate_pass_rates else None
            )
        else:
            gate_rate = None

        proposals.append(KPIProposal(
            kpi_id        = "gate_pass_rate",
            title         = "Brand Gate Pass Rate",
            description   = "Fraction of gate evaluations that pass "
                            "(APPROVED or APPROVED_WITH_FLAGS).",
            formula       = "approved_verdicts / total_gate_evaluations",
            target        = ">= 90%",
            current_value = gate_rate,
            unit          = "%",
        ))

        # --- Archive reuse rate ---
        proposals.append(KPIProposal(
            kpi_id        = "archive_reuse_rate",
            title         = "Archive Reuse Rate",
            description   = "Fraction of asset requests served from the "
                            "archive rather than generated fresh.",
            formula       = "reuse_decisions / total_decisions",
            target        = ">= 20% (saves cost at scale)",
            current_value = state.recall.reuse_rate if state.recall.total_decisions > 0 else None,
            unit          = "%",
        ))

        # --- Average stage duration ---
        durations = [
            a.avg_duration_ms for a in state.agent_health
            if a.avg_duration_ms is not None
        ]
        avg_dur = round(sum(durations) / len(durations), 1) if durations else None

        proposals.append(KPIProposal(
            kpi_id        = "avg_stage_duration_ms",
            title         = "Average Stage Duration",
            description   = "Mean execution time per pipeline stage. "
                            "Rising duration indicates model latency or "
                            "prompt bloat.",
            formula       = "AVG(duration_ms) across all stage_executions",
            target        = "< 30,000 ms per stage",
            current_value = avg_dur,
            unit          = "ms",
        ))

        # --- Provider failure rate (worst) ---
        if state.provider_health:
            worst_rate = max(p.failure_rate for p in state.provider_health)
        else:
            worst_rate = None

        proposals.append(KPIProposal(
            kpi_id        = "worst_provider_failure_rate",
            title         = "Worst Provider Failure Rate",
            description   = "Highest single-provider asset generation "
                            "failure rate. Triggers provider swap recommendation.",
            formula       = "MAX(failed_attempts / total_attempts) across providers",
            target        = "< 10%",
            current_value = worst_rate,
            unit          = "%",
        ))

        # --- Channel count ---
        proposals.append(KPIProposal(
            kpi_id        = "active_channel_count",
            title         = "Active Channel Count",
            description   = "Number of channels in 'active' status. "
                            "Measures factory utilization and scale.",
            formula       = "COUNT(*) FROM channel_profiles WHERE status='active'",
            target        = ">= 1 (factory is operational)",
            current_value = float(sum(1 for c in state.channels if c.status == "active")),
            unit          = "channels",
        ))

        return proposals
    except Exception:
        return []


# ===========================================================================
# Widget Proposer
# ===========================================================================

def propose_widgets(state: FactoryState) -> list:
    """
    Propose dashboard widgets based on available data.

    Widgets are always proposed regardless of data volume (they show
    "no data yet" states gracefully). Priority 1 = must-have.

    Non-blocking: returns [] on error.
    """
    try:
        widgets: list[WidgetProposal] = []

        # Always propose
        widgets.append(WidgetProposal(
            widget_id   = "run_health_gauge",
            title       = "Run Health Gauge",
            widget_type = "gauge",
            description = "Real-time run success rate gauge with healthy/"
                          "warning/degraded colour bands.",
            data_source = "runs",
            priority    = 1,
        ))

        widgets.append(WidgetProposal(
            widget_id   = "episode_cost_trend",
            title       = "Episode Cost Trend",
            widget_type = "line_chart",
            description = "Total cost per episode over time. "
                          "Shows cost creep and optimization impact.",
            data_source = "costs, v_episode_costs",
            priority    = 1,
        ))

        widgets.append(WidgetProposal(
            widget_id   = "agent_health_table",
            title       = "Agent Health Table",
            widget_type = "table",
            description = "Per-stage success rate, average duration, and "
                          "average cost. Sortable by any column.",
            data_source = "stage_executions, v_stage_performance",
            priority    = 1,
        ))

        # Conditional on provider data
        if len(state.provider_health) > 1:
            widgets.append(WidgetProposal(
                widget_id   = "provider_cost_share",
                title       = "Provider Cost Distribution",
                widget_type = "pie_chart",
                description = "Cost breakdown by provider. Shows which "
                              "provider drives the most spend.",
                data_source = "costs",
                priority    = 2,
            ))

        # Conditional on gate data
        if any("gate" in a.stage_name.lower() or "bga" in a.stage_name.lower()
               for a in state.agent_health):
            widgets.append(WidgetProposal(
                widget_id   = "gate_pass_rate_heatmap",
                title       = "Gate Pass Rate Heatmap",
                widget_type = "bar_chart",
                description = "Gate pass rates by gate name and week. "
                              "Shows brand compliance trends.",
                data_source = "gate_verdicts, v_gate_pass_rates",
                priority    = 2,
            ))

        # Conditional on channels
        if len(state.channels) > 1:
            widgets.append(WidgetProposal(
                widget_id   = "channel_health_grid",
                title       = "Channel Health Grid",
                widget_type = "grid",
                description = "Grid of all channels showing status, profile "
                              "completeness, and run health.",
                data_source = "channel_profiles, runs",
                priority    = 2,
            ))

        # Conditional on archive data
        if state.archive.total_archived > 0:
            widgets.append(WidgetProposal(
                widget_id   = "archive_reuse_gauge",
                title       = "Archive Reuse Rate",
                widget_type = "gauge",
                description = "Fraction of asset requests fulfilled from "
                              "archive. Higher = better cost efficiency.",
                data_source = "reuse_decisions",
                priority    = 3,
            ))

        # Conditional on learning data
        if state.learning.total_records > 0:
            widgets.append(WidgetProposal(
                widget_id   = "learning_signal_chart",
                title       = "Learning Signals by Type",
                widget_type = "bar_chart",
                description = "Count of learning signals by type over time. "
                              "Shows how fast the factory is learning.",
                data_source = "learning_records",
                priority    = 3,
            ))

        # Notifications summary always
        widgets.append(WidgetProposal(
            widget_id   = "notification_feed",
            title       = "Live Notification Feed",
            widget_type = "table",
            description = "Real-time feed of critical/warning/info "
                          "notifications from factory state analysis.",
            data_source = "dashboard_api.get_notifications()",
            priority    = 1,
        ))

        return sorted(widgets, key=lambda w: (w.priority, w.widget_id))
    except Exception:
        return []


# ===========================================================================
# Alert Proposer
# ===========================================================================

def propose_alerts(state: FactoryState) -> list:
    """
    Propose alert conditions based on available data.

    Non-blocking: returns [] on error.
    """
    try:
        alerts: list[AlertProposal] = []

        alerts.append(AlertProposal(
            alert_id    = "run_success_rate_critical",
            title       = "Run Success Rate Critical",
            condition   = "run success_rate < 50% over last 10 runs",
            level       = "critical",
            description = "Pipeline is critically unstable. Requires "
                          "immediate investigation.",
            query_hint  = (
                "SELECT SUM(CASE WHEN status='complete' THEN 1.0 ELSE 0 END) "
                "/ COUNT(*) AS rate FROM "
                "(SELECT status FROM runs ORDER BY started_at DESC LIMIT 10)"
            ),
        ))

        alerts.append(AlertProposal(
            alert_id    = "run_success_rate_warning",
            title       = "Run Success Rate Warning",
            condition   = "run success_rate < 80% over last 10 runs",
            level       = "warning",
            description = "Pipeline success rate is below target. "
                          "Review recent failures.",
            query_hint  = (
                "SELECT SUM(CASE WHEN status='complete' THEN 1.0 ELSE 0 END) "
                "/ COUNT(*) AS rate FROM "
                "(SELECT status FROM runs ORDER BY started_at DESC LIMIT 10)"
            ),
        ))

        alerts.append(AlertProposal(
            alert_id    = "cost_spike",
            title       = "Episode Cost Spike",
            condition   = "episode cost > 2× the rolling 5-episode average",
            level       = "warning",
            description = "Unexpected cost spike detected. Check for prompt "
                          "bloat, retry storms, or provider pricing changes.",
            query_hint  = (
                "SELECT episode_id, SUM(cost_usd) AS episode_cost FROM costs "
                "GROUP BY episode_id HAVING episode_cost > "
                "2 * (SELECT AVG(total_cost_usd) FROM v_episode_costs "
                "ORDER BY last_cost_at DESC LIMIT 5)"
            ),
        ))

        alerts.append(AlertProposal(
            alert_id    = "provider_failure_spike",
            title       = "Provider Failure Spike",
            condition   = "any provider failure_rate > 30% in last 24 hours",
            level       = "critical",
            description = "A provider is failing at high rates. Consider "
                          "switching to backup provider.",
            query_hint  = (
                "SELECT provider, "
                "SUM(CASE WHEN status='failed' THEN 1.0 ELSE 0 END)/COUNT(*) "
                "AS fail_rate FROM asset_versions "
                "WHERE attempted_at > datetime('now','-1 day') "
                "GROUP BY provider HAVING fail_rate > 0.3"
            ),
        ))

        alerts.append(AlertProposal(
            alert_id    = "budget_ceiling_exceeded",
            title       = "Budget Ceiling Blocked",
            condition   = "any budget_authorizations verdict = 'BLOCKED'",
            level       = "critical",
            description = "A run was blocked by the Finance Agent. "
                          "Review cost ceiling or episode scope.",
            query_hint  = (
                "SELECT run_id, episode_id, estimated_cost_usd, "
                "budget_ceiling_usd FROM budget_authorizations "
                "WHERE verdict='BLOCKED' ORDER BY evaluated_at DESC LIMIT 5"
            ),
        ))

        alerts.append(AlertProposal(
            alert_id    = "archive_staleness",
            title       = "Archive Not Growing",
            condition   = "no new archive_index entries in 7+ days",
            level       = "warning",
            description = "Archive has stopped growing. Either no runs have "
                          "completed or the archive stage is failing.",
            query_hint  = (
                "SELECT MAX(archived_at) AS last_archived FROM archive_index"
            ),
        ))

        alerts.append(AlertProposal(
            alert_id    = "acceptance_audit_failed",
            title       = "Acceptance Audit Failure",
            condition   = "acceptance audit ready=False (any blocker present)",
            level       = "critical",
            description = "Factory is not cleared for any run. "
                          "Resolve all audit blockers before proceeding.",
            query_hint  = "run_acceptance_audit() → ready=False",
        ))

        return alerts
    except Exception:
        return []


# ===========================================================================
# Management Proposer
# ===========================================================================

def propose_management_improvements(
    state:   FactoryState,
    db_path: Optional[Path] = None,
) -> list:
    """
    Propose operational improvements based on factory state analysis.

    Non-blocking: returns [] on error.
    """
    try:
        proposals: list[ManagementProposal] = []

        # --- No channels ---
        if not state.channels:
            proposals.append(ManagementProposal(
                proposal_id = "create_first_channel",
                title       = "Create First Channel",
                description = "No channels are configured. The factory "
                              "cannot produce any content without at least "
                              "one channel profile.",
                action      = "Call create_channel() or clone_factory() "
                              "with a complete config dict.",
                reason      = "A channel profile is the minimum requirement "
                              "for any factory run.",
                priority    = "urgent",
            ))

        # --- Incomplete channel profiles ---
        incomplete = [c for c in state.channels if c.profile_completeness < 1.0]
        for ch in incomplete:
            proposals.append(ManagementProposal(
                proposal_id = f"complete_profile_{ch.channel_id}",
                title       = f"Complete Profile: {ch.name}",
                description = f"Channel '{ch.name}' is missing: "
                              + _missing_profiles(ch),
                action      = "Call update_brand_profile() / "
                              "update_publishing_profile() / "
                              "update_model_profile() to fill the gaps.",
                reason      = "Incomplete profiles cause pipeline stages to "
                              "use defaults, reducing content quality.",
                priority    = "recommended",
            ))

        # --- No budget ceiling set ---
        if _budget_ceiling_missing(db_path):
            proposals.append(ManagementProposal(
                proposal_id = "set_cost_ceiling",
                title       = "Set Budget Ceiling",
                description = "No budget_authorizations records found. "
                              "The Finance Agent has never been invoked, "
                              "meaning runs have no cost guardrail.",
                action      = "Configure BUDGET_CEILING_USD in channel "
                              "config and enable the Finance Agent stage.",
                reason      = "Without a budget ceiling, a runaway retry "
                              "loop could incur unbounded costs.",
                priority    = "urgent",
            ))

        # --- No evolution adjustments recorded ---
        if _evolution_adjustments_empty(db_path):
            proposals.append(ManagementProposal(
                proposal_id = "enable_evolution",
                title       = "Enable Evolution Engine",
                description = "No evolution_adjustments have been recorded. "
                              "The factory is not learning from its runs.",
                action      = "After a run completes, call "
                              "evolve(record=True) to capture learning.",
                reason      = "Factory evolution drives continuous quality "
                              "and cost improvement. Without it the factory "
                              "does not improve over time.",
                priority    = "recommended",
            ))

        # --- High provider failure rate ---
        for p in state.provider_health:
            if p.failure_rate > 0.20 and p.total_attempts >= 5:
                proposals.append(ManagementProposal(
                    proposal_id = f"review_provider_{p.provider}",
                    title       = f"Review Provider: {p.provider}",
                    description = f"Provider '{p.provider}' has a "
                                  f"{p.failure_rate:.0%} failure rate "
                                  f"over {p.total_attempts} attempts.",
                    action      = "Check provider API status, update "
                                  "credentials, or switch to backup provider "
                                  "in the model profile.",
                    reason      = "High failure rates cause retry storms "
                                  "that waste budget and slow production.",
                    priority    = "urgent",
                ))

        # --- Low reuse rate with sizeable archive ---
        if (state.archive.total_archived >= 10
                and state.recall.total_decisions >= 5
                and state.recall.reuse_rate < 0.10):
            proposals.append(ManagementProposal(
                proposal_id = "tune_reuse_threshold",
                title       = "Tune Archive Reuse Threshold",
                description = f"Archive has {state.archive.total_archived} "
                              f"assets but reuse rate is only "
                              f"{state.recall.reuse_rate:.0%}.",
                action      = "Lower the reuse threshold in should_reuse() "
                              "(currently 0.6) or improve semantic tagging "
                              "to increase recall hit rate.",
                reason      = "Low reuse rate means archive investment is "
                              "not saving costs.",
                priority    = "recommended",
            ))

        # --- Missing acceptance audit readiness ---
        if not state.audit.ready:
            proposals.append(ManagementProposal(
                proposal_id = "resolve_audit_blockers",
                title       = "Resolve Audit Blockers",
                description = f"Acceptance audit is not READY "
                              f"({state.audit.blocker_count} blockers, "
                              f"{state.audit.warning_count} warnings). "
                              f"Detail: {state.audit.detail}",
                action      = "Run python3 run_acceptance_audit.py to see "
                              "which checks are failing and fix them.",
                reason      = "Factory cannot be run until all blockers "
                              "are resolved.",
                priority    = "urgent",
            ))

        return proposals
    except Exception:
        return []


def _missing_profiles(ch) -> str:
    missing = []
    if not ch.brand_complete:      missing.append("brand")
    if not ch.publishing_complete: missing.append("publishing")
    if not ch.models_complete:     missing.append("models")
    return ", ".join(missing) if missing else "nothing (complete)"


def _budget_ceiling_missing(db_path: Optional[Path]) -> bool:
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            count = _table_row_count(conn, "budget_authorizations")
        finally:
            conn.close()
        return count == 0
    except Exception:
        return False


def _evolution_adjustments_empty(db_path: Optional[Path]) -> bool:
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            count = _table_row_count(conn, "evolution_adjustments")
        finally:
            conn.close()
        return count == 0
    except Exception:
        return False


# ===========================================================================
# Priority Ranker
# ===========================================================================

def compute_priority_order(
    metric_gaps:          list,
    kpis:                 list,
    widgets:              list,
    alerts:               list,
    management_proposals: list,
) -> list:
    """
    Return a list of proposal IDs ordered by impact/effort ratio.

    Scoring:
        impact: high=3, medium=2, low=1
        effort: low=3, medium=2, high=1 (inverted — less effort = higher score)
        mgmt priority: urgent=3, recommended=2, optional=1
        alert level:   critical=2, warning=1
        widget priority: inverted (1=3, 2=2, 3=1)

    Non-blocking: returns [] on error.
    """
    try:
        scored: list[tuple[float, str]] = []

        impact_score  = {"high": 3, "medium": 2, "low": 1}
        effort_score  = {"low": 3, "medium": 2, "high": 1}   # inverted
        mgmt_score    = {"urgent": 3, "recommended": 2, "optional": 1}
        alert_score   = {"critical": 2, "warning": 1}
        widget_score  = {1: 3, 2: 2, 3: 1}

        for g in metric_gaps:
            s = impact_score.get(g.impact, 1) * effort_score.get(g.effort, 1)
            scored.append((s, g.gap_id))

        for k in kpis:
            scored.append((2.0, k.kpi_id))   # medium priority by default

        for w in widgets:
            s = widget_score.get(w.priority, 1)
            scored.append((float(s), w.widget_id))

        for a in alerts:
            s = alert_score.get(a.level, 1)
            scored.append((float(s), a.alert_id))

        for m in management_proposals:
            s = mgmt_score.get(m.priority, 1)
            scored.append((float(s), m.proposal_id))

        # Sort descending by score, then alphabetically for stability
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [pid for _, pid in scored]
    except Exception:
        return []


# ===========================================================================
# Main entry point
# ===========================================================================

def run_director(
    db_path:    Optional[Path] = None,
    factory_id: str            = "hypogeum",
) -> DashboardDirectorReport:
    """
    Run the Dashboard Director Agent and return a complete report.

    Loads FactoryState from the DB, then runs all five analysis sub-components
    and computes a priority-ordered list of actionable proposals.

    Non-blocking: all sections degrade gracefully on error.

    Args:
        db_path:    Override SQLite path.
        factory_id: Stable factory identifier (cosmetic).

    Returns:
        DashboardDirectorReport.
    """
    state = get_factory_state(db_path=db_path, factory_id=factory_id)

    gaps    = analyze_metric_gaps(state, db_path=db_path)
    kpis    = propose_kpis(state, db_path=db_path)
    widgets = propose_widgets(state)
    alerts  = propose_alerts(state)
    mgmt    = propose_management_improvements(state, db_path=db_path)

    priority = compute_priority_order(gaps, kpis, widgets, alerts, mgmt)

    total = len(gaps) + len(kpis) + len(widgets) + len(alerts) + len(mgmt)

    return DashboardDirectorReport(
        analyzed_at          = state.checked_at,
        factory_id           = factory_id,
        missing_metrics      = gaps,
        kpi_proposals        = kpis,
        widget_proposals     = widgets,
        alert_proposals      = alerts,
        management_proposals = mgmt,
        priority_order       = priority,
        total_proposals      = total,
    )
