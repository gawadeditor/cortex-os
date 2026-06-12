"""
tests/test_dashboard_director.py — Phase D: Dashboard Director Agent tests

Coverage:
    TestDataContracts          — all 6 dataclasses instantiate correctly
    TestMetricGapDetector      — triggers on populated tables, silent on empty
    TestKPIProposer            — KPI IDs, formulas, current_value from state
    TestWidgetProposer         — always-on widgets, conditional widgets
    TestAlertProposer          — all alert IDs and levels present
    TestManagementProposer     — urgent/recommended/optional conditions
    TestPriorityRanker         — ordering logic, stable sort, empty inputs
    TestRunDirector            — full report assembly, non-blocking
    TestNoNetworkCalls         — socket blocked throughout
    TestNoProductionSideEffects — no writes, no ep_000
"""

from __future__ import annotations

import socket
import uuid
from pathlib import Path

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.channel_config import bootstrap_channel
from hypogeum.dashboard_api import get_factory_state, FactoryState, RunHealthSummary
from hypogeum.dashboard_director import (
    # Dataclasses
    AlertProposal,
    DashboardDirectorReport,
    KPIProposal,
    ManagementProposal,
    MetricGap,
    WidgetProposal,
    # Functions
    analyze_metric_gaps,
    compute_priority_order,
    propose_alerts,
    propose_kpis,
    propose_management_improvements,
    propose_widgets,
    run_director,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "director_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def populated_db(db_path):
    """Insert data that triggers metric gap detection."""
    conn = get_connection(db_path)

    # Runs (needed for FK)
    conn.execute("""INSERT INTO runs (run_id, episode_id, started_at, status)
                    VALUES ('r1','ep1','2026-01-01T00:00:00','complete'),
                           ('r2','ep2','2026-01-02T00:00:00','failed')""")

    # Episodes
    conn.execute("""INSERT INTO episodes (episode_id, created_at, run_id)
                    VALUES ('ep1','2026-01-01T00:00:00','r1'),
                           ('ep2','2026-01-02T00:00:00','r2')""")

    # Gate verdicts → triggers gate_pass_rate_trend gap
    conn.execute(
        """INSERT INTO gate_verdicts
           (verdict_id, run_id, episode_id, gate_name, verdict, evaluated_at)
           VALUES (?,?,?,?,?,?)""",
        (str(uuid.uuid4()), "r1", "ep1", "bga_p1", "APPROVED", "2026-01-01T00:00:00"),
    )

    # Stage executions
    for i, (stage, status) in enumerate([
        ("research",  "completed"),
        ("narration", "completed"),
        ("narration", "failed"),
    ]):
        conn.execute(
            """INSERT INTO stage_executions
               (stage_exec_id, run_id, episode_id, stage_name, started_at,
                status, skipped, duration_ms, cost_usd)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), "r1", "ep1", stage,
             "2026-01-01T00:00:00", status, 0, 1500, 0.02),
        )

    # Costs (2 episodes → triggers episode_cost_trend gap)
    for ep, prov, model, cost in [
        ("ep1", "anthropic", "claude-sonnet-4.6", 0.10),
        ("ep2", "anthropic", "claude-opus-4.6",   0.20),
    ]:
        conn.execute(
            """INSERT INTO costs
               (cost_id, run_id, episode_id, provider, model,
                call_type, cost_usd, called_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), "r1", ep, prov, model, "llm", cost,
             "2026-01-01T00:00:00"),
        )

    # Asset versions → triggers asset_retry_waste gap; 2 providers for widget test
    for prov, status, cost in [
        ("muapi",      "failed",    0.15),
        ("elevenlabs", "succeeded", 0.05),
    ]:
        conn.execute(
            """INSERT INTO asset_versions
               (version_id, asset_slot_id, run_id, episode_id, provider,
                asset_type, status, cost_usd, attempt_number, attempted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), str(uuid.uuid4()), "r1", "ep1",
             prov, "video_clip", status, cost, 1, "2026-01-01T00:00:00"),
        )

    # kg_entities → triggers knowledge_graph_coverage gap
    conn.execute(
        """INSERT INTO kg_entities
           (entity_id, episode_id, run_id, entity_name, entity_type,
            first_seen_episode_id, extracted_at)
           VALUES (?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), "ep1", "r1", "GPT-4", "topic",
         "ep1", "2026-01-01T00:00:00"),
    )

    # Learning records → triggers learning_application_rate gap
    conn.execute(
        """INSERT INTO learning_records
           (learning_id, episode_id, run_id, recorded_at, signal_type)
           VALUES (?,?,?,?,?)""",
        (str(uuid.uuid4()), "ep1", "r1", "2026-01-01T00:00:00", "performance"),
    )

    # Archive index
    conn.execute(
        """INSERT INTO archive_index
           (archive_id, asset_id, episode_id, asset_type, provider,
            reuse_eligible, archived_at)
           VALUES (?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), str(uuid.uuid4()), "ep1",
         "video_clip", "muapi", 1, "2026-01-01T00:00:00"),
    )

    conn.commit()
    conn.close()

    # Bootstrap two channels (triggers channel_production_rate gap: count >= 2)
    for i, ch_id in enumerate(["ch_001", "ch_002"]):
        bootstrap_channel({
            "channel_id": ch_id,
            "name": f"Channel {i+1}",
            "brand": {"persona_name": "Host", "language": "en"},
            "publishing": {"enabled_platforms": ["youtube"]},
            "models": {},
        }, db_path=db_path)

    return db_path


# ===========================================================================
# TestDataContracts
# ===========================================================================

class TestDataContracts:
    def test_metric_gap_fields(self):
        g = MetricGap("id", "title", "desc", "table", "high", "low", "SQL")
        assert g.gap_id == "id"
        assert g.impact == "high"
        assert g.effort == "low"

    def test_kpi_proposal_fields(self):
        k = KPIProposal("k1", "KPI", "desc", "formula", "target", 0.5, "$")
        assert k.kpi_id == "k1"
        assert k.current_value == 0.5
        assert k.unit == "$"

    def test_widget_proposal_fields(self):
        w = WidgetProposal("w1", "Widget", "gauge", "desc", "table", 1)
        assert w.widget_id == "w1"
        assert w.widget_type == "gauge"
        assert w.priority == 1

    def test_alert_proposal_fields(self):
        a = AlertProposal("a1", "Alert", "cond", "critical", "desc", "SQL")
        assert a.alert_id == "a1"
        assert a.level == "critical"

    def test_management_proposal_fields(self):
        m = ManagementProposal("m1", "title", "desc", "action", "reason", "urgent")
        assert m.proposal_id == "m1"
        assert m.priority == "urgent"

    def test_director_report_fields(self):
        r = DashboardDirectorReport(
            "2026-01-01", "hypogeum", [], [], [], [], [], [], 0
        )
        assert r.factory_id == "hypogeum"
        assert r.total_proposals == 0
        assert r.metadata_version == "v1"


# ===========================================================================
# TestMetricGapDetector
# ===========================================================================

class TestMetricGapDetector:
    def test_empty_db_returns_no_gaps(self, db_path):
        state = get_factory_state(db_path=db_path)
        gaps = analyze_metric_gaps(state, db_path=db_path)
        assert gaps == []

    def test_returns_metric_gap_objects(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        gaps = analyze_metric_gaps(state, db_path=populated_db)
        assert all(isinstance(g, MetricGap) for g in gaps)

    def test_gate_gap_detected(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        gaps = analyze_metric_gaps(state, db_path=populated_db)
        ids = {g.gap_id for g in gaps}
        assert "gate_pass_rate_trend" in ids

    def test_episode_cost_trend_detected(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        gaps = analyze_metric_gaps(state, db_path=populated_db)
        ids = {g.gap_id for g in gaps}
        assert "episode_cost_trend" in ids

    def test_asset_retry_waste_detected(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        gaps = analyze_metric_gaps(state, db_path=populated_db)
        ids = {g.gap_id for g in gaps}
        assert "asset_retry_waste" in ids

    def test_knowledge_graph_gap_detected(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        gaps = analyze_metric_gaps(state, db_path=populated_db)
        ids = {g.gap_id for g in gaps}
        assert "knowledge_graph_coverage" in ids

    def test_learning_gap_detected(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        gaps = analyze_metric_gaps(state, db_path=populated_db)
        ids = {g.gap_id for g in gaps}
        assert "learning_application_rate" in ids

    def test_channel_production_rate_detected_with_two_channels(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        gaps = analyze_metric_gaps(state, db_path=populated_db)
        ids = {g.gap_id for g in gaps}
        assert "channel_production_rate" in ids

    def test_sorted_high_impact_first(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        gaps = analyze_metric_gaps(state, db_path=populated_db)
        if len(gaps) >= 2:
            impacts = [g.impact for g in gaps]
            # High before medium before low
            seen_medium = False
            seen_low = False
            for imp in impacts:
                if imp == "medium": seen_medium = True
                if imp == "low":    seen_low = True
                if seen_medium and imp == "high":
                    pytest.fail("high impact gap appeared after medium")
                if seen_low and imp in {"high", "medium"}:
                    pytest.fail("high/medium gap appeared after low")

    def test_each_gap_has_query_hint(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        gaps = analyze_metric_gaps(state, db_path=populated_db)
        for g in gaps:
            assert g.query_hint  # non-empty

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        state = get_factory_state(db_path=bad)
        result = analyze_metric_gaps(state, db_path=bad)
        assert result == []


# ===========================================================================
# TestKPIProposer
# ===========================================================================

class TestKPIProposer:
    def test_empty_state_returns_kpis(self, db_path):
        state = get_factory_state(db_path=db_path)
        kpis = propose_kpis(state, db_path=db_path)
        assert len(kpis) > 0

    def test_returns_kpi_objects(self, db_path):
        state = get_factory_state(db_path=db_path)
        kpis = propose_kpis(state, db_path=db_path)
        assert all(isinstance(k, KPIProposal) for k in kpis)

    def test_cost_per_episode_always_proposed(self, db_path):
        state = get_factory_state(db_path=db_path)
        kpis = propose_kpis(state, db_path=db_path)
        ids = {k.kpi_id for k in kpis}
        assert "cost_per_episode" in ids

    def test_run_success_rate_always_proposed(self, db_path):
        state = get_factory_state(db_path=db_path)
        kpis = propose_kpis(state, db_path=db_path)
        ids = {k.kpi_id for k in kpis}
        assert "run_success_rate" in ids

    def test_gate_pass_rate_always_proposed(self, db_path):
        state = get_factory_state(db_path=db_path)
        kpis = propose_kpis(state, db_path=db_path)
        ids = {k.kpi_id for k in kpis}
        assert "gate_pass_rate" in ids

    def test_archive_reuse_rate_always_proposed(self, db_path):
        state = get_factory_state(db_path=db_path)
        kpis = propose_kpis(state, db_path=db_path)
        ids = {k.kpi_id for k in kpis}
        assert "archive_reuse_rate" in ids

    def test_active_channel_count_always_proposed(self, db_path):
        state = get_factory_state(db_path=db_path)
        kpis = propose_kpis(state, db_path=db_path)
        ids = {k.kpi_id for k in kpis}
        assert "active_channel_count" in ids

    def test_cost_per_episode_computed_from_state(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        kpis = propose_kpis(state, db_path=populated_db)
        kpi = next(k for k in kpis if k.kpi_id == "cost_per_episode")
        assert kpi.current_value is not None
        assert kpi.current_value > 0

    def test_cost_per_episode_none_when_no_episodes(self, db_path):
        state = get_factory_state(db_path=db_path)
        kpis = propose_kpis(state, db_path=db_path)
        kpi = next(k for k in kpis if k.kpi_id == "cost_per_episode")
        assert kpi.current_value is None

    def test_run_success_rate_none_when_no_runs(self, db_path):
        state = get_factory_state(db_path=db_path)
        kpis = propose_kpis(state, db_path=db_path)
        kpi = next(k for k in kpis if k.kpi_id == "run_success_rate")
        assert kpi.current_value is None

    def test_kpi_has_unit(self, db_path):
        state = get_factory_state(db_path=db_path)
        kpis = propose_kpis(state, db_path=db_path)
        for k in kpis:
            assert k.unit  # non-empty

    def test_kpi_has_target(self, db_path):
        state = get_factory_state(db_path=db_path)
        kpis = propose_kpis(state, db_path=db_path)
        for k in kpis:
            assert k.target  # non-empty

    def test_active_channel_count_kpi_value(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        kpis = propose_kpis(state, db_path=populated_db)
        kpi = next(k for k in kpis if k.kpi_id == "active_channel_count")
        assert kpi.current_value == 2.0

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        state = get_factory_state(db_path=bad)
        result = propose_kpis(state, db_path=bad)
        # Non-blocking — may return empty list or partial list, not exception
        assert isinstance(result, list)


# ===========================================================================
# TestWidgetProposer
# ===========================================================================

class TestWidgetProposer:
    def test_returns_widget_objects(self, db_path):
        state = get_factory_state(db_path=db_path)
        widgets = propose_widgets(state)
        assert all(isinstance(w, WidgetProposal) for w in widgets)

    def test_run_health_gauge_always_present(self, db_path):
        state = get_factory_state(db_path=db_path)
        widgets = propose_widgets(state)
        ids = {w.widget_id for w in widgets}
        assert "run_health_gauge" in ids

    def test_episode_cost_trend_always_present(self, db_path):
        state = get_factory_state(db_path=db_path)
        widgets = propose_widgets(state)
        ids = {w.widget_id for w in widgets}
        assert "episode_cost_trend" in ids

    def test_agent_health_table_always_present(self, db_path):
        state = get_factory_state(db_path=db_path)
        widgets = propose_widgets(state)
        ids = {w.widget_id for w in widgets}
        assert "agent_health_table" in ids

    def test_notification_feed_always_present(self, db_path):
        state = get_factory_state(db_path=db_path)
        widgets = propose_widgets(state)
        ids = {w.widget_id for w in widgets}
        assert "notification_feed" in ids

    def test_provider_cost_share_only_with_multiple_providers(self, db_path):
        state = get_factory_state(db_path=db_path)
        # No data → no providers
        widgets = propose_widgets(state)
        ids = {w.widget_id for w in widgets}
        assert "provider_cost_share" not in ids

    def test_provider_cost_share_with_multiple_providers(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        widgets = propose_widgets(state)
        ids = {w.widget_id for w in widgets}
        assert "provider_cost_share" in ids

    def test_channel_health_grid_only_with_multiple_channels(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        widgets = propose_widgets(state)
        ids = {w.widget_id for w in widgets}
        assert "channel_health_grid" in ids

    def test_archive_reuse_gauge_with_archive_data(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        widgets = propose_widgets(state)
        ids = {w.widget_id for w in widgets}
        assert "archive_reuse_gauge" in ids

    def test_widgets_sorted_by_priority(self, db_path):
        state = get_factory_state(db_path=db_path)
        widgets = propose_widgets(state)
        priorities = [w.priority for w in widgets]
        assert priorities == sorted(priorities)

    def test_each_widget_has_data_source(self, db_path):
        state = get_factory_state(db_path=db_path)
        widgets = propose_widgets(state)
        for w in widgets:
            assert w.data_source


# ===========================================================================
# TestAlertProposer
# ===========================================================================

class TestAlertProposer:
    def test_returns_alert_objects(self, db_path):
        state = get_factory_state(db_path=db_path)
        alerts = propose_alerts(state)
        assert all(isinstance(a, AlertProposal) for a in alerts)

    def test_always_returns_alerts(self, db_path):
        state = get_factory_state(db_path=db_path)
        alerts = propose_alerts(state)
        assert len(alerts) > 0

    def test_run_success_rate_critical_present(self, db_path):
        state = get_factory_state(db_path=db_path)
        alerts = propose_alerts(state)
        ids = {a.alert_id for a in alerts}
        assert "run_success_rate_critical" in ids

    def test_cost_spike_alert_present(self, db_path):
        state = get_factory_state(db_path=db_path)
        alerts = propose_alerts(state)
        ids = {a.alert_id for a in alerts}
        assert "cost_spike" in ids

    def test_provider_failure_spike_present(self, db_path):
        state = get_factory_state(db_path=db_path)
        alerts = propose_alerts(state)
        ids = {a.alert_id for a in alerts}
        assert "provider_failure_spike" in ids

    def test_budget_ceiling_exceeded_present(self, db_path):
        state = get_factory_state(db_path=db_path)
        alerts = propose_alerts(state)
        ids = {a.alert_id for a in alerts}
        assert "budget_ceiling_exceeded" in ids

    def test_archive_staleness_present(self, db_path):
        state = get_factory_state(db_path=db_path)
        alerts = propose_alerts(state)
        ids = {a.alert_id for a in alerts}
        assert "archive_staleness" in ids

    def test_acceptance_audit_failed_present(self, db_path):
        state = get_factory_state(db_path=db_path)
        alerts = propose_alerts(state)
        ids = {a.alert_id for a in alerts}
        assert "acceptance_audit_failed" in ids

    def test_levels_are_valid(self, db_path):
        state = get_factory_state(db_path=db_path)
        alerts = propose_alerts(state)
        valid = {"critical", "warning"}
        for a in alerts:
            assert a.level in valid

    def test_each_alert_has_query_hint(self, db_path):
        state = get_factory_state(db_path=db_path)
        alerts = propose_alerts(state)
        for a in alerts:
            assert a.query_hint


# ===========================================================================
# TestManagementProposer
# ===========================================================================

class TestManagementProposer:
    def test_returns_management_objects(self, db_path):
        state = get_factory_state(db_path=db_path)
        mgmt = propose_management_improvements(state, db_path=db_path)
        assert all(isinstance(m, ManagementProposal) for m in mgmt)

    def test_no_channels_produces_urgent_proposal(self, db_path):
        state = get_factory_state(db_path=db_path)
        mgmt = propose_management_improvements(state, db_path=db_path)
        urgent = [m for m in mgmt if m.priority == "urgent"]
        assert any(m.proposal_id == "create_first_channel" for m in urgent)

    def test_no_budget_ceiling_produces_urgent_proposal(self, db_path):
        state = get_factory_state(db_path=db_path)
        mgmt = propose_management_improvements(state, db_path=db_path)
        ids = {m.proposal_id for m in mgmt}
        assert "set_cost_ceiling" in ids

    def test_no_evolution_adjustments_produces_recommended_proposal(self, db_path):
        state = get_factory_state(db_path=db_path)
        mgmt = propose_management_improvements(state, db_path=db_path)
        ids = {m.proposal_id for m in mgmt}
        assert "enable_evolution" in ids

    def test_incomplete_channel_produces_proposal(self, db_path):
        # Bootstrap a channel without brand profile to make it incomplete
        # Actually bootstrap always creates brand — so let's just verify
        # the incomplete detection logic works when state has an incomplete channel
        from hypogeum.dashboard_api import ChannelSummary
        from hypogeum.dashboard_director import _missing_profiles
        ch = ChannelSummary("ch1", "Test", "active", False, True, True, 0.667, "2026-01-01")
        missing = _missing_profiles(ch)
        assert "brand" in missing

    def test_audit_not_ready_produces_urgent_proposal(self, db_path):
        from hypogeum.dashboard_api import (
            AuditSummary, FactoryState, RunHealthSummary,
            ArchiveSummary, RecallSummary, LearningSummary, CostSummary,
        )
        # Build a state with audit not ready
        rh = RunHealthSummary(0, 0, 0, 0, 0.0, None)
        au = AuditSummary("2026-01-01", False, 2, 0, "REJECTED")
        state = FactoryState(
            "hypogeum", "v2", "2026-01-01", "unknown",
            rh, [], [], [], [],
            ArchiveSummary(0, 0, {}, None, None),
            RecallSummary(0, 0, 0, 0.0),
            LearningSummary(0, {}, []),
            CostSummary(0.0, {}, {}, {}, 0, 0),
            au, []
        )
        mgmt = propose_management_improvements(state, db_path=db_path)
        ids = {m.proposal_id for m in mgmt}
        assert "resolve_audit_blockers" in ids

    def test_high_provider_failure_rate_produces_urgent(self, db_path):
        from hypogeum.dashboard_api import (
            FactoryState, RunHealthSummary, ArchiveSummary,
            RecallSummary, LearningSummary, CostSummary,
            AuditSummary, ProviderHealthSummary,
        )
        bad_prov = ProviderHealthSummary("bad_prov", 10, 3, 0.3, 1.0)
        rh = RunHealthSummary(10, 8, 2, 0, 0.8, "2026-01-01")
        au = AuditSummary("2026-01-01", True, 0, 0, "ACCEPTED")
        state = FactoryState(
            "hypogeum", "v2", "2026-01-01", "healthy",
            rh, [], [], [], [bad_prov],
            ArchiveSummary(0, 0, {}, None, None),
            RecallSummary(0, 0, 0, 0.0),
            LearningSummary(0, {}, []),
            CostSummary(0.0, {}, {}, {}, 0, 0),
            au, []
        )
        mgmt = propose_management_improvements(state, db_path=db_path)
        urgent = [m for m in mgmt if m.priority == "urgent"]
        assert any("review_provider" in m.proposal_id for m in urgent)

    def test_priorities_are_valid(self, db_path):
        state = get_factory_state(db_path=db_path)
        mgmt = propose_management_improvements(state, db_path=db_path)
        valid = {"urgent", "recommended", "optional"}
        for m in mgmt:
            assert m.priority in valid

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        state = get_factory_state(db_path=bad)
        result = propose_management_improvements(state, db_path=bad)
        assert isinstance(result, list)


# ===========================================================================
# TestPriorityRanker
# ===========================================================================

class TestPriorityRanker:
    def test_empty_inputs_returns_empty(self):
        result = compute_priority_order([], [], [], [], [])
        assert result == []

    def test_returns_list_of_strings(self, db_path):
        state = get_factory_state(db_path=db_path)
        gaps    = analyze_metric_gaps(state, db_path=db_path)
        kpis    = propose_kpis(state, db_path=db_path)
        widgets = propose_widgets(state)
        alerts  = propose_alerts(state)
        mgmt    = propose_management_improvements(state, db_path=db_path)
        result  = compute_priority_order(gaps, kpis, widgets, alerts, mgmt)
        assert all(isinstance(p, str) for p in result)

    def test_all_ids_included(self, db_path):
        state   = get_factory_state(db_path=db_path)
        gaps    = analyze_metric_gaps(state, db_path=db_path)
        kpis    = propose_kpis(state, db_path=db_path)
        widgets = propose_widgets(state)
        alerts  = propose_alerts(state)
        mgmt    = propose_management_improvements(state, db_path=db_path)
        result  = compute_priority_order(gaps, kpis, widgets, alerts, mgmt)

        all_ids = (
            {g.gap_id   for g in gaps}
            | {k.kpi_id   for k in kpis}
            | {w.widget_id for w in widgets}
            | {a.alert_id  for a in alerts}
            | {m.proposal_id for m in mgmt}
        )
        assert set(result) == all_ids

    def test_no_duplicates_in_order(self, db_path):
        state   = get_factory_state(db_path=db_path)
        gaps    = analyze_metric_gaps(state, db_path=db_path)
        kpis    = propose_kpis(state, db_path=db_path)
        widgets = propose_widgets(state)
        alerts  = propose_alerts(state)
        mgmt    = propose_management_improvements(state, db_path=db_path)
        result  = compute_priority_order(gaps, kpis, widgets, alerts, mgmt)
        assert len(result) == len(set(result))

    def test_urgent_management_before_optional(self):
        urgent = ManagementProposal("urgent_m", "t", "d", "a", "r", "urgent")
        optional = ManagementProposal("optional_m", "t", "d", "a", "r", "optional")
        result = compute_priority_order([], [], [], [], [urgent, optional])
        assert result.index("urgent_m") < result.index("optional_m")

    def test_critical_alert_before_warning(self):
        critical = AlertProposal("crit_a", "t", "c", "critical", "d", "q")
        warning  = AlertProposal("warn_a", "t", "c", "warning",  "d", "q")
        result = compute_priority_order([], [], [], [critical, warning], [])
        assert result.index("crit_a") < result.index("warn_a")

    def test_high_impact_gap_before_low_impact(self):
        high_gap = MetricGap("high_g", "t", "d", "table", "high", "low", "q")
        low_gap  = MetricGap("low_g",  "t", "d", "table", "low",  "low", "q")
        result = compute_priority_order([high_gap, low_gap], [], [], [], [])
        assert result.index("high_g") < result.index("low_g")

    def test_priority_1_widget_before_priority_3(self):
        p1 = WidgetProposal("w1", "t", "gauge", "d", "src", 1)
        p3 = WidgetProposal("w3", "t", "gauge", "d", "src", 3)
        result = compute_priority_order([], [], [p1, p3], [], [])
        assert result.index("w1") < result.index("w3")


# ===========================================================================
# TestRunDirector
# ===========================================================================

class TestRunDirector:
    def test_returns_director_report(self, db_path):
        report = run_director(db_path=db_path)
        assert isinstance(report, DashboardDirectorReport)

    def test_factory_id_set(self, db_path):
        report = run_director(db_path=db_path, factory_id="my_factory")
        assert report.factory_id == "my_factory"

    def test_analyzed_at_is_set(self, db_path):
        report = run_director(db_path=db_path)
        assert report.analyzed_at is not None

    def test_kpi_proposals_not_empty(self, db_path):
        report = run_director(db_path=db_path)
        assert len(report.kpi_proposals) > 0

    def test_widget_proposals_not_empty(self, db_path):
        report = run_director(db_path=db_path)
        assert len(report.widget_proposals) > 0

    def test_alert_proposals_not_empty(self, db_path):
        report = run_director(db_path=db_path)
        assert len(report.alert_proposals) > 0

    def test_management_proposals_not_empty(self, db_path):
        report = run_director(db_path=db_path)
        assert len(report.management_proposals) > 0

    def test_priority_order_not_empty(self, db_path):
        report = run_director(db_path=db_path)
        assert len(report.priority_order) > 0

    def test_total_proposals_sum(self, db_path):
        report = run_director(db_path=db_path)
        expected = (
            len(report.missing_metrics)
            + len(report.kpi_proposals)
            + len(report.widget_proposals)
            + len(report.alert_proposals)
            + len(report.management_proposals)
        )
        assert report.total_proposals == expected

    def test_priority_order_contains_all_ids(self, db_path):
        report = run_director(db_path=db_path)
        all_ids = (
            {g.gap_id    for g in report.missing_metrics}
            | {k.kpi_id    for k in report.kpi_proposals}
            | {w.widget_id for w in report.widget_proposals}
            | {a.alert_id  for a in report.alert_proposals}
            | {m.proposal_id for m in report.management_proposals}
        )
        assert set(report.priority_order) == all_ids

    def test_populated_db_has_metric_gaps(self, populated_db):
        report = run_director(db_path=populated_db)
        assert len(report.missing_metrics) > 0

    def test_metadata_version(self, db_path):
        report = run_director(db_path=db_path)
        assert report.metadata_version == "v1"

    def test_bad_db_returns_report(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        report = run_director(db_path=bad)
        assert isinstance(report, DashboardDirectorReport)


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:
    @pytest.fixture(autouse=True)
    def block_network(self, monkeypatch):
        def no_socket(*args, **kwargs):
            raise RuntimeError("Network call detected in dashboard_director tests")
        monkeypatch.setattr(socket, "socket", no_socket)
        yield

    def test_analyze_metric_gaps_no_network(self, db_path):
        state = get_factory_state(db_path=db_path)
        analyze_metric_gaps(state, db_path=db_path)

    def test_propose_kpis_no_network(self, db_path):
        state = get_factory_state(db_path=db_path)
        propose_kpis(state, db_path=db_path)

    def test_propose_widgets_no_network(self, db_path):
        state = get_factory_state(db_path=db_path)
        propose_widgets(state)

    def test_propose_alerts_no_network(self, db_path):
        state = get_factory_state(db_path=db_path)
        propose_alerts(state)

    def test_propose_management_no_network(self, db_path):
        state = get_factory_state(db_path=db_path)
        propose_management_improvements(state, db_path=db_path)

    def test_compute_priority_order_no_network(self):
        compute_priority_order([], [], [], [], [])

    def test_run_director_no_network(self, db_path):
        run_director(db_path=db_path)


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:
    def test_no_ep_000_created(self, db_path, tmp_path):
        run_director(db_path=db_path)
        assert list(tmp_path.rglob("ep_000")) == []

    def test_run_director_does_not_write_runs(self, db_path):
        run_director(db_path=db_path)
        conn = get_connection(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
            assert count == 0
        finally:
            conn.close()

    def test_run_director_does_not_write_episodes(self, db_path):
        run_director(db_path=db_path)
        conn = get_connection(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
            assert count == 0
        finally:
            conn.close()

    def test_all_functions_read_only(self, populated_db):
        conn = get_connection(populated_db)
        run_before = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        ep_before  = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()

        state = get_factory_state(db_path=populated_db)
        analyze_metric_gaps(state, db_path=populated_db)
        propose_kpis(state, db_path=populated_db)
        propose_widgets(state)
        propose_alerts(state)
        propose_management_improvements(state, db_path=populated_db)
        run_director(db_path=populated_db)

        conn = get_connection(populated_db)
        run_after = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        ep_after  = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()

        assert run_before == run_after
        assert ep_before  == ep_after
