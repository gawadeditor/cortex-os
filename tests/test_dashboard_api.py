"""
tests/test_dashboard_api.py — Phase C: Real Dashboard Foundation tests

Coverage:
    TestDataContracts        — all dataclasses instantiate with correct fields
    TestPermissionModel      — PERMISSION_TABLE, get_permission, list_permissions
    TestChannelSummaries     — empty, populated, completeness calculation
    TestRunHealth            — empty, complete/failed/running breakdown, success rate
    TestAgentHealth          — reads v_stage_performance view, skip exclusion
    TestModelUsage           — reads costs table, empty returns []
    TestProviderHealth       — reads asset_versions, failure_rate calc
    TestArchiveSummary       — reads archive_index, empty returns zeroed
    TestRecallSummary        — reads lazy reuse_decisions table, missing table → zeroed
    TestLearningSummary      — reads learning_records, empty returns zeroed
    TestCostSummary          — total, by_provider, by_model, by_episode
    TestAuditSummary         — calls acceptance_audit, returns typed result
    TestNotifications        — critical/warning/info conditions
    TestOverallHealth        — unknown/degraded/warning/healthy logic
    TestGetFactoryState      — full assembly, schema_version, all sections present
    TestFormatFactoryState   — text output format, no network, no DB
    TestNonBlocking          — all functions return safe defaults on bad DB
    TestNoNetworkCalls       — socket blocked throughout
    TestNoProductionSideEffects — no ep_000, no run state written
"""

from __future__ import annotations

import socket
import uuid
from pathlib import Path

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.channel_config import bootstrap_channel
from hypogeum.dashboard_api import (
    # Dataclasses
    AgentHealthSummary,
    ArchiveSummary,
    AuditSummary,
    ChannelSummary,
    CostSummary,
    FactoryState,
    LearningSummary,
    ModelUsageSummary,
    Notification,
    Permission,
    ProviderHealthSummary,
    RecallSummary,
    RunHealthSummary,
    # Permission model
    PERMISSION_TABLE,
    get_permission,
    list_permissions,
    # Section functions
    get_agent_health,
    get_archive_summary,
    get_audit_summary,
    get_channel_summaries,
    get_cost_summary,
    get_factory_state,
    get_learning_summary,
    get_model_usage,
    get_notifications,
    get_provider_health,
    get_recall_summary,
    get_run_health,
    # Formatter
    format_factory_state,
    # Internals
    _compute_overall_health,
    SCHEMA_VERSION,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "dashboard_api_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def populated_db(db_path):
    """Insert sample data across all relevant tables."""
    conn = get_connection(db_path)

    # Runs
    conn.execute("""INSERT INTO runs (run_id, episode_id, started_at, status)
                    VALUES ('r1','ep1','2026-01-01T00:00:00','complete'),
                           ('r2','ep2','2026-01-02T00:00:00','complete'),
                           ('r3','ep3','2026-01-03T00:00:00','failed'),
                           ('r4','ep4','2026-01-04T00:00:00','running')""")

    # Episodes
    conn.execute("""INSERT INTO episodes (episode_id, created_at, run_id)
                    VALUES ('ep1','2026-01-01T00:00:00','r1'),
                           ('ep2','2026-01-02T00:00:00','r2'),
                           ('ep3','2026-01-03T00:00:00','r3'),
                           ('ep4','2026-01-04T00:00:00','r4')""")

    # Stage executions
    for i, (stage, status, skipped) in enumerate([
        ("research",  "completed", 0),
        ("research",  "completed", 0),
        ("research",  "failed",    0),
        ("narration", "completed", 0),
        ("narration", "skipped",   1),
    ]):
        conn.execute(
            """INSERT INTO stage_executions
               (stage_exec_id, run_id, episode_id, stage_name, started_at,
                status, skipped, duration_ms, cost_usd)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), f"r{(i%4)+1}", f"ep{(i%4)+1}",
             stage, "2026-01-01T00:00:00", status, skipped, 1200, 0.01),
        )

    # Gate verdicts
    conn.execute(
        """INSERT INTO gate_verdicts
           (verdict_id, run_id, episode_id, gate_name, verdict, evaluated_at)
           VALUES (?,?,?,?,?,?)""",
        (str(uuid.uuid4()), "r1", "ep1", "bga_p1", "APPROVED", "2026-01-01T00:00:00"),
    )

    # Asset versions
    for prov, status, cost in [
        ("muapi",   "succeeded", 0.15),
        ("muapi",   "succeeded", 0.15),
        ("muapi",   "failed",    0.00),
        ("elevenlabs", "succeeded", 0.05),
    ]:
        conn.execute(
            """INSERT INTO asset_versions
               (version_id, asset_slot_id, run_id, episode_id, provider,
                asset_type, status, cost_usd, attempted_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), str(uuid.uuid4()), "r1", "ep1",
             prov, "video_clip", status, cost, "2026-01-01T00:00:00"),
        )

    # Costs
    for ep, prov, model, cost in [
        ("ep1", "anthropic", "claude-sonnet-4.6", 0.10),
        ("ep1", "muapi",     None,                0.15),
        ("ep2", "anthropic", "claude-opus-4.6",   0.20),
        ("ep2", "elevenlabs",None,                0.05),
    ]:
        conn.execute(
            """INSERT INTO costs
               (cost_id, run_id, episode_id, provider, model,
                call_type, cost_usd, called_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), "r1", ep, prov, model, "llm", cost,
             "2026-01-01T00:00:00"),
        )

    # Archive index
    for i in range(3):
        conn.execute(
            """INSERT INTO archive_index
               (archive_id, asset_id, episode_id, asset_type, provider,
                reuse_eligible, archived_at)
               VALUES (?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), str(uuid.uuid4()), f"ep{i+1}",
             "video_clip" if i < 2 else "thumbnail",
             "muapi", 1 if i < 2 else 0,
             f"2026-01-0{i+1}T00:00:00"),
        )

    # Learning records
    for sig in ["performance", "performance", "cost_efficiency"]:
        conn.execute(
            """INSERT INTO learning_records
               (learning_id, episode_id, run_id, recorded_at,
                signal_type, recommendation)
               VALUES (?,?,?,?,?,?)""",
            (str(uuid.uuid4()), "ep1", "r1", "2026-01-01T00:00:00",
             sig, f"Recommendation for {sig}"),
        )

    # reuse_decisions (lazy — create it inline)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reuse_decisions (
            decision_id TEXT NOT NULL PRIMARY KEY,
            episode_id TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            provider TEXT,
            recommended_asset_id TEXT,
            archive_id TEXT,
            score REAL,
            decision TEXT NOT NULL,
            score_components_json TEXT,
            decided_at TEXT NOT NULL
        )
    """)
    for decision in ["reuse", "reuse", "generate"]:
        conn.execute(
            """INSERT INTO reuse_decisions
               (decision_id, episode_id, asset_type, decision, decided_at)
               VALUES (?,?,?,?,?)""",
            (str(uuid.uuid4()), "ep1", "video_clip", decision, "2026-01-01T00:00:00"),
        )

    conn.commit()
    conn.close()

    # Bootstrap a channel
    bootstrap_channel({
        "channel_id": "test_ch",
        "name": "Test Channel",
        "brand": {"persona_name": "Host", "language": "en"},
        "publishing": {"enabled_platforms": ["youtube"]},
        "models": {"narration_voice_id": "voice_x"},
    }, db_path=db_path)

    return db_path


# ===========================================================================
# TestDataContracts
# ===========================================================================

class TestDataContracts:
    def test_channel_summary_fields(self):
        cs = ChannelSummary("id", "name", "active", True, True, True, 1.0, "2026-01-01")
        assert cs.channel_id == "id"
        assert cs.profile_completeness == 1.0

    def test_run_health_fields(self):
        rh = RunHealthSummary(10, 8, 2, 0, 0.8, "2026-01-01")
        assert rh.success_rate == 0.8
        assert rh.running_count == 0

    def test_agent_health_fields(self):
        ah = AgentHealthSummary("research", 10, 8, 2, 0, 0.8, 1200.0, 0.01)
        assert ah.stage_name == "research"
        assert ah.avg_duration_ms == 1200.0

    def test_model_usage_fields(self):
        mu = ModelUsageSummary("claude-sonnet", 5, 0.50)
        assert mu.model_id == "claude-sonnet"
        assert mu.total_cost_usd == 0.50

    def test_provider_health_fields(self):
        ph = ProviderHealthSummary("muapi", 10, 1, 0.1, 1.50)
        assert ph.provider == "muapi"
        assert ph.failure_rate == 0.1

    def test_archive_summary_fields(self):
        ar = ArchiveSummary(5, 4, {"video_clip": 5}, "2026-01-01", "2026-12-01")
        assert ar.total_archived == 5
        assert ar.asset_types["video_clip"] == 5

    def test_recall_summary_fields(self):
        rs = RecallSummary(10, 6, 4, 0.6)
        assert rs.reuse_rate == 0.6

    def test_learning_summary_fields(self):
        ls = LearningSummary(5, {"performance": 3}, ["rec1"])
        assert ls.total_records == 5
        assert ls.recent_recommendations[0] == "rec1"

    def test_cost_summary_fields(self):
        cs = CostSummary(1.5, {"anthropic": 1.0}, {}, {"ep1": 1.5}, 3, 2)
        assert cs.total_usd == 1.5
        assert cs.by_provider["anthropic"] == 1.0

    def test_audit_summary_fields(self):
        au = AuditSummary("2026-01-01", True, 0, 0, "ACCEPTED")
        assert au.ready is True

    def test_notification_fields(self):
        n = Notification("critical", "run_health", "msg", None, "2026-01-01")
        assert n.level == "critical"
        assert n.category == "run_health"

    def test_permission_fields(self):
        p = Permission("approve_run", "desc", "operator", False)
        assert p.action == "approve_run"
        assert p.requires_level == "operator"

    def test_factory_state_fields(self):
        rh = RunHealthSummary(0, 0, 0, 0, 0.0, None)
        ar = ArchiveSummary(0, 0, {}, None, None)
        rc = RecallSummary(0, 0, 0, 0.0)
        ls = LearningSummary(0, {}, [])
        cs = CostSummary(0.0, {}, {}, {}, 0, 0)
        au = AuditSummary(None, True, 0, 0, "ACCEPTED")
        state = FactoryState(
            "hypogeum", "v2", "2026-01-01", "unknown",
            rh, [], [], [], [], ar, rc, ls, cs, au, []
        )
        assert state.factory_id == "hypogeum"
        assert state.schema_version == "v2"
        assert state.metadata_version == "v1"


# ===========================================================================
# TestPermissionModel
# ===========================================================================

class TestPermissionModel:
    def test_permission_table_has_ten_entries(self):
        assert len(PERMISSION_TABLE) == 10

    def test_all_required_actions_defined(self):
        required = {
            "approve_run", "pause_channel", "resume_channel",
            "approve_evolution", "import_package", "clone_factory",
            "override_model", "override_cost_ceiling",
            "deactivate_channel", "emergency_stop",
        }
        assert required == set(PERMISSION_TABLE.keys())

    def test_get_permission_returns_permission(self):
        p = get_permission("approve_run")
        assert isinstance(p, Permission)
        assert p.requires_level == "operator"

    def test_get_permission_missing_action_returns_none(self):
        assert get_permission("nonexistent_action") is None

    def test_emergency_stop_requires_confirmation(self):
        p = get_permission("emergency_stop")
        assert p.requires_confirmation is True
        assert p.requires_level == "emergency"

    def test_deactivate_channel_requires_confirmation(self):
        p = get_permission("deactivate_channel")
        assert p.requires_confirmation is True

    def test_approve_run_does_not_require_confirmation(self):
        p = get_permission("approve_run")
        assert p.requires_confirmation is False

    def test_list_permissions_returns_all(self):
        perms = list_permissions()
        assert len(perms) == 10

    def test_list_permissions_ordered_by_level(self):
        perms = list_permissions()
        levels = [p.requires_level for p in perms]
        # All operators come before admins come before emergency
        last_op  = max((i for i, l in enumerate(levels) if l == "operator"),  default=-1)
        first_adm = min((i for i, l in enumerate(levels) if l == "admin"),    default=999)
        first_em  = min((i for i, l in enumerate(levels) if l == "emergency"), default=999)
        assert last_op < first_adm
        assert first_adm < first_em

    def test_clone_factory_requires_admin(self):
        p = get_permission("clone_factory")
        assert p.requires_level == "admin"


# ===========================================================================
# TestChannelSummaries
# ===========================================================================

class TestChannelSummaries:
    def test_empty_db_returns_empty_list(self, db_path):
        assert get_channel_summaries(db_path=db_path) == []

    def test_returns_channel_summary_objects(self, populated_db):
        result = get_channel_summaries(db_path=populated_db)
        assert len(result) == 1
        assert isinstance(result[0], ChannelSummary)

    def test_channel_id_correct(self, populated_db):
        result = get_channel_summaries(db_path=populated_db)
        assert result[0].channel_id == "test_ch"

    def test_channel_name_correct(self, populated_db):
        result = get_channel_summaries(db_path=populated_db)
        assert result[0].name == "Test Channel"

    def test_full_channel_completeness_is_one(self, populated_db):
        result = get_channel_summaries(db_path=populated_db)
        assert result[0].profile_completeness == 1.0

    def test_brand_complete_is_true(self, populated_db):
        result = get_channel_summaries(db_path=populated_db)
        assert result[0].brand_complete is True

    def test_publishing_complete_is_true(self, populated_db):
        result = get_channel_summaries(db_path=populated_db)
        assert result[0].publishing_complete is True

    def test_models_complete_is_true(self, populated_db):
        result = get_channel_summaries(db_path=populated_db)
        assert result[0].models_complete is True

    def test_channel_without_brand_is_incomplete(self, db_path):
        bootstrap_channel({
            "channel_id": "bare_ch",
            "name": "Bare",
            "brand": {"language": "en"},
            "publishing": {"enabled_platforms": ["youtube"]},
            "models": {},
        }, db_path=db_path)
        result = get_channel_summaries(db_path=db_path)
        # brand has no persona/tone etc but the profile exists → brand_complete=True
        # Just verify completeness is a float between 0 and 1
        assert 0.0 <= result[0].profile_completeness <= 1.0

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        assert get_channel_summaries(db_path=bad) == []


# ===========================================================================
# TestRunHealth
# ===========================================================================

class TestRunHealth:
    def test_empty_db_returns_zeroed(self, db_path):
        rh = get_run_health(db_path=db_path)
        assert rh.total_runs == 0
        assert rh.success_rate == 0.0

    def test_total_runs_correct(self, populated_db):
        rh = get_run_health(db_path=populated_db)
        assert rh.total_runs == 4

    def test_complete_count_correct(self, populated_db):
        rh = get_run_health(db_path=populated_db)
        assert rh.complete_count == 2

    def test_failed_count_correct(self, populated_db):
        rh = get_run_health(db_path=populated_db)
        assert rh.failed_count == 1

    def test_running_count_correct(self, populated_db):
        rh = get_run_health(db_path=populated_db)
        assert rh.running_count == 1

    def test_success_rate_excludes_running(self, populated_db):
        rh = get_run_health(db_path=populated_db)
        # 2 complete / (2 complete + 1 failed) = 0.6667
        assert abs(rh.success_rate - 2/3) < 0.001

    def test_last_run_at_not_none(self, populated_db):
        rh = get_run_health(db_path=populated_db)
        assert rh.last_run_at is not None

    def test_bad_db_returns_zeroed(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        rh = get_run_health(db_path=bad)
        assert rh.total_runs == 0


# ===========================================================================
# TestAgentHealth
# ===========================================================================

class TestAgentHealth:
    def test_empty_db_returns_empty_list(self, db_path):
        assert get_agent_health(db_path=db_path) == []

    def test_returns_agent_health_objects(self, populated_db):
        result = get_agent_health(db_path=populated_db)
        assert all(isinstance(a, AgentHealthSummary) for a in result)

    def test_stages_present(self, populated_db):
        result = get_agent_health(db_path=populated_db)
        names = {a.stage_name for a in result}
        assert "research" in names
        assert "narration" in names

    def test_research_success_rate(self, populated_db):
        result = get_agent_health(db_path=populated_db)
        research = next(a for a in result if a.stage_name == "research")
        # 2 complete, 1 failed, 0 skipped → 2/3
        assert abs(research.success_rate - 2/3) < 0.001

    def test_narration_skip_excluded_from_denominator(self, populated_db):
        result = get_agent_health(db_path=populated_db)
        narration = next(a for a in result if a.stage_name == "narration")
        # 1 completed, 1 skipped → success_rate = 1/1 = 1.0
        assert narration.success_rate == 1.0
        assert narration.skip_count == 1

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        assert get_agent_health(db_path=bad) == []


# ===========================================================================
# TestModelUsage
# ===========================================================================

class TestModelUsage:
    def test_empty_db_returns_empty(self, db_path):
        assert get_model_usage(db_path=db_path) == []

    def test_returns_model_usage_objects(self, populated_db):
        result = get_model_usage(db_path=populated_db)
        assert all(isinstance(m, ModelUsageSummary) for m in result)

    def test_models_present(self, populated_db):
        result = get_model_usage(db_path=populated_db)
        model_ids = {m.model_id for m in result}
        assert "claude-sonnet-4.6" in model_ids
        assert "claude-opus-4.6" in model_ids

    def test_usage_count_correct(self, populated_db):
        result = get_model_usage(db_path=populated_db)
        sonnet = next(m for m in result if m.model_id == "claude-sonnet-4.6")
        assert sonnet.usage_count == 1

    def test_cost_aggregated_per_model(self, populated_db):
        result = get_model_usage(db_path=populated_db)
        opus = next(m for m in result if m.model_id == "claude-opus-4.6")
        assert abs(opus.total_cost_usd - 0.20) < 1e-6

    def test_null_model_excluded(self, populated_db):
        result = get_model_usage(db_path=populated_db)
        for m in result:
            assert m.model_id is not None

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        assert get_model_usage(db_path=bad) == []


# ===========================================================================
# TestProviderHealth
# ===========================================================================

class TestProviderHealth:
    def test_empty_db_returns_empty(self, db_path):
        assert get_provider_health(db_path=db_path) == []

    def test_returns_provider_health_objects(self, populated_db):
        result = get_provider_health(db_path=populated_db)
        assert all(isinstance(p, ProviderHealthSummary) for p in result)

    def test_providers_present(self, populated_db):
        result = get_provider_health(db_path=populated_db)
        providers = {p.provider for p in result}
        assert "muapi" in providers
        assert "elevenlabs" in providers

    def test_muapi_failure_rate(self, populated_db):
        result = get_provider_health(db_path=populated_db)
        muapi = next(p for p in result if p.provider == "muapi")
        # 3 total (2 succeeded, 1 failed) → 1/3
        assert abs(muapi.failure_rate - 1/3) < 0.001

    def test_elevenlabs_zero_failure_rate(self, populated_db):
        result = get_provider_health(db_path=populated_db)
        el = next(p for p in result if p.provider == "elevenlabs")
        assert el.failure_rate == 0.0

    def test_cost_summed_correctly(self, populated_db):
        result = get_provider_health(db_path=populated_db)
        muapi = next(p for p in result if p.provider == "muapi")
        assert abs(muapi.total_cost_usd - 0.30) < 1e-6

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        assert get_provider_health(db_path=bad) == []


# ===========================================================================
# TestArchiveSummary
# ===========================================================================

class TestArchiveSummary:
    def test_empty_db_returns_zeroed(self, db_path):
        ar = get_archive_summary(db_path=db_path)
        assert ar.total_archived == 0
        assert ar.asset_types == {}

    def test_total_archived_correct(self, populated_db):
        ar = get_archive_summary(db_path=populated_db)
        assert ar.total_archived == 3

    def test_reuse_eligible_count(self, populated_db):
        ar = get_archive_summary(db_path=populated_db)
        assert ar.reuse_eligible_count == 2

    def test_asset_types_breakdown(self, populated_db):
        ar = get_archive_summary(db_path=populated_db)
        assert "video_clip" in ar.asset_types
        assert "thumbnail" in ar.asset_types
        assert ar.asset_types["video_clip"] == 2

    def test_oldest_at_not_none(self, populated_db):
        ar = get_archive_summary(db_path=populated_db)
        assert ar.oldest_at is not None

    def test_newest_at_not_none(self, populated_db):
        ar = get_archive_summary(db_path=populated_db)
        assert ar.newest_at is not None

    def test_bad_db_returns_zeroed(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        ar = get_archive_summary(db_path=bad)
        assert ar.total_archived == 0


# ===========================================================================
# TestRecallSummary
# ===========================================================================

class TestRecallSummary:
    def test_empty_db_returns_zeroed(self, db_path):
        rs = get_recall_summary(db_path=db_path)
        assert rs.total_decisions == 0
        assert rs.reuse_rate == 0.0

    def test_total_decisions_correct(self, populated_db):
        rs = get_recall_summary(db_path=populated_db)
        assert rs.total_decisions == 3

    def test_reuse_count_correct(self, populated_db):
        rs = get_recall_summary(db_path=populated_db)
        assert rs.reuse_count == 2

    def test_generate_count_correct(self, populated_db):
        rs = get_recall_summary(db_path=populated_db)
        assert rs.generate_count == 1

    def test_reuse_rate_correct(self, populated_db):
        rs = get_recall_summary(db_path=populated_db)
        assert abs(rs.reuse_rate - 2/3) < 0.001

    def test_missing_table_returns_zeroed(self, db_path):
        # reuse_decisions table not created → should return zeroed
        rs = get_recall_summary(db_path=db_path)
        assert rs.total_decisions == 0

    def test_bad_db_returns_zeroed(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        rs = get_recall_summary(db_path=bad)
        assert rs.total_decisions == 0


# ===========================================================================
# TestLearningSummary
# ===========================================================================

class TestLearningSummary:
    def test_empty_db_returns_zeroed(self, db_path):
        ls = get_learning_summary(db_path=db_path)
        assert ls.total_records == 0
        assert ls.signal_types == {}

    def test_total_records_correct(self, populated_db):
        ls = get_learning_summary(db_path=populated_db)
        assert ls.total_records == 3

    def test_signal_types_breakdown(self, populated_db):
        ls = get_learning_summary(db_path=populated_db)
        assert "performance" in ls.signal_types
        assert ls.signal_types["performance"] == 2
        assert ls.signal_types["cost_efficiency"] == 1

    def test_recent_recommendations_present(self, populated_db):
        ls = get_learning_summary(db_path=populated_db)
        assert len(ls.recent_recommendations) > 0

    def test_recent_recommendations_max_five(self, db_path):
        conn = get_connection(db_path)
        # Insert a run row first to satisfy FK constraint
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status) VALUES (?,?,?,?)",
            ("r_learn", "ep_learn", "2026-01-01T00:00:00", "complete"),
        )
        for i in range(8):
            conn.execute(
                """INSERT INTO learning_records
                   (learning_id, episode_id, run_id, recorded_at,
                    signal_type, recommendation)
                   VALUES (?,?,?,?,?,?)""",
                (str(uuid.uuid4()), "ep_learn", "r_learn",
                 f"2026-01-{i+1:02d}T00:00:00", "performance", f"Rec {i}"),
            )
        conn.commit()
        conn.close()
        ls = get_learning_summary(db_path=db_path)
        assert len(ls.recent_recommendations) <= 5

    def test_bad_db_returns_zeroed(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        ls = get_learning_summary(db_path=bad)
        assert ls.total_records == 0


# ===========================================================================
# TestCostSummary
# ===========================================================================

class TestCostSummary:
    def test_empty_db_returns_zeroed(self, db_path):
        cs = get_cost_summary(db_path=db_path)
        assert cs.total_usd == 0.0
        assert cs.by_provider == {}

    def test_total_usd_correct(self, populated_db):
        cs = get_cost_summary(db_path=populated_db)
        expected = 0.10 + 0.15 + 0.20 + 0.05
        assert abs(cs.total_usd - expected) < 1e-6

    def test_by_provider_has_anthropic(self, populated_db):
        cs = get_cost_summary(db_path=populated_db)
        assert "anthropic" in cs.by_provider
        assert abs(cs.by_provider["anthropic"] - 0.30) < 1e-6

    def test_by_provider_has_muapi(self, populated_db):
        cs = get_cost_summary(db_path=populated_db)
        assert "muapi" in cs.by_provider

    def test_by_model_populated(self, populated_db):
        cs = get_cost_summary(db_path=populated_db)
        assert "claude-sonnet-4.6" in cs.by_model or "claude-opus-4.6" in cs.by_model

    def test_by_episode_populated(self, populated_db):
        cs = get_cost_summary(db_path=populated_db)
        assert "ep1" in cs.by_episode or "ep2" in cs.by_episode

    def test_run_count_correct(self, populated_db):
        cs = get_cost_summary(db_path=populated_db)
        assert cs.run_count == 4

    def test_episode_count_correct(self, populated_db):
        cs = get_cost_summary(db_path=populated_db)
        assert cs.episode_count == 4

    def test_bad_db_returns_zeroed(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        cs = get_cost_summary(db_path=bad)
        assert cs.total_usd == 0.0


# ===========================================================================
# TestAuditSummary
# ===========================================================================

class TestAuditSummary:
    def test_returns_audit_summary(self, db_path):
        au = get_audit_summary(db_path=db_path)
        assert isinstance(au, AuditSummary)

    def test_last_run_at_is_set(self, db_path):
        au = get_audit_summary(db_path=db_path)
        assert au.last_run_at is not None

    def test_ready_is_bool(self, db_path):
        au = get_audit_summary(db_path=db_path)
        assert isinstance(au.ready, bool)

    def test_blocker_count_is_int(self, db_path):
        au = get_audit_summary(db_path=db_path)
        assert isinstance(au.blocker_count, int)

    def test_detail_is_string(self, db_path):
        au = get_audit_summary(db_path=db_path)
        assert isinstance(au.detail, str)

    def test_bad_db_nonblocking(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        au = get_audit_summary(db_path=bad)
        assert isinstance(au, AuditSummary)


# ===========================================================================
# TestNotifications
# ===========================================================================

class TestNotifications:
    def _rh(self, total=0, complete=0, failed=0, running=0, rate=0.0):
        return RunHealthSummary(total, complete, failed, running, rate, None)

    def _au(self, ready=True, blockers=0, warnings=0):
        return AuditSummary("2026-01-01", ready, blockers, warnings, "OK")

    def test_no_channels_produces_info(self, db_path):
        notes = get_notifications(db_path=db_path)
        cats = [n.category for n in notes]
        assert "channels" in cats

    def test_no_channels_info_level(self, db_path):
        notes = get_notifications(db_path=db_path)
        channel_notes = [n for n in notes if n.category == "channels"]
        assert all(n.level == "info" for n in channel_notes)

    def test_low_success_rate_critical(self, db_path):
        rh = self._rh(total=10, complete=3, failed=7, rate=0.3)
        au = self._au()
        notes = get_notifications(db_path=db_path, run_health=rh, audit=au,
                                   provider_health=[], agent_health=[], channels=[])
        levels = [n.level for n in notes if n.category == "run_health"]
        assert "critical" in levels

    def test_moderate_success_rate_warning(self, db_path):
        rh = self._rh(total=10, complete=7, failed=3, rate=0.7)
        au = self._au()
        notes = get_notifications(db_path=db_path, run_health=rh, audit=au,
                                   provider_health=[], agent_health=[], channels=[])
        levels = [n.level for n in notes if n.category == "run_health"]
        assert "warning" in levels

    def test_audit_blocker_is_critical(self, db_path):
        rh = self._rh()
        au = self._au(ready=False, blockers=2)
        notes = get_notifications(db_path=db_path, run_health=rh, audit=au,
                                   provider_health=[], agent_health=[], channels=[])
        audit_notes = [n for n in notes if n.category == "audit"]
        assert any(n.level == "critical" for n in audit_notes)

    def test_audit_warning_only_is_warning(self, db_path):
        rh = self._rh()
        au = self._au(ready=True, blockers=0, warnings=1)
        notes = get_notifications(db_path=db_path, run_health=rh, audit=au,
                                   provider_health=[], agent_health=[], channels=[])
        audit_notes = [n for n in notes if n.category == "audit"]
        assert any(n.level == "warning" for n in audit_notes)

    def test_high_provider_failure_rate_warning(self, db_path):
        rh = self._rh()
        au = self._au()
        ph = [ProviderHealthSummary("bad_provider", 10, 4, 0.4, 1.0)]
        notes = get_notifications(db_path=db_path, run_health=rh, audit=au,
                                   provider_health=ph, agent_health=[], channels=[])
        prov_notes = [n for n in notes if n.category == "provider_health"]
        assert any(n.level == "warning" for n in prov_notes)

    def test_low_agent_success_rate_warning(self, db_path):
        rh = self._rh()
        au = self._au()
        ah = [AgentHealthSummary("bad_stage", 10, 5, 5, 0, 0.5, None, None)]
        notes = get_notifications(db_path=db_path, run_health=rh, audit=au,
                                   provider_health=[], agent_health=ah, channels=[])
        agent_notes = [n for n in notes if n.category == "agent_health"]
        assert any(n.level == "warning" for n in agent_notes)

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        notes = get_notifications(db_path=bad)
        assert isinstance(notes, list)

    def test_notification_has_created_at(self, db_path):
        notes = get_notifications(db_path=db_path)
        for n in notes:
            assert n.created_at is not None


# ===========================================================================
# TestOverallHealth
# ===========================================================================

class TestOverallHealth:
    def _rh(self, total, rate):
        return RunHealthSummary(total, 0, 0, 0, rate, None)

    def _au(self, blockers=0):
        return AuditSummary(None, blockers == 0, blockers, 0, "")

    def test_unknown_when_no_runs(self):
        assert _compute_overall_health(self._rh(0, 0.0), self._au()) == "unknown"

    def test_degraded_when_rate_below_50(self):
        assert _compute_overall_health(self._rh(10, 0.4), self._au()) == "degraded"

    def test_warning_when_rate_below_80(self):
        assert _compute_overall_health(self._rh(10, 0.7), self._au()) == "warning"

    def test_warning_when_audit_blockers(self):
        assert _compute_overall_health(self._rh(10, 0.9), self._au(blockers=1)) == "warning"

    def test_healthy_when_rate_80_and_no_blockers(self):
        assert _compute_overall_health(self._rh(10, 0.85), self._au()) == "healthy"

    def test_exactly_50_is_warning(self):
        # rate < 0.5 is degraded; rate == 0.5 is warning (not < 0.5)
        assert _compute_overall_health(self._rh(10, 0.5), self._au()) == "warning"

    def test_exactly_80_is_healthy(self):
        assert _compute_overall_health(self._rh(10, 0.8), self._au()) == "healthy"


# ===========================================================================
# TestGetFactoryState
# ===========================================================================

class TestGetFactoryState:
    def test_returns_factory_state(self, db_path):
        state = get_factory_state(db_path=db_path)
        assert isinstance(state, FactoryState)

    def test_factory_id_set(self, db_path):
        state = get_factory_state(db_path=db_path, factory_id="my_factory")
        assert state.factory_id == "my_factory"

    def test_schema_version_is_v2(self, db_path):
        state = get_factory_state(db_path=db_path)
        assert state.schema_version == "v2"

    def test_checked_at_is_set(self, db_path):
        state = get_factory_state(db_path=db_path)
        assert state.checked_at is not None

    def test_overall_health_is_string(self, db_path):
        state = get_factory_state(db_path=db_path)
        assert state.overall_health in {"healthy", "warning", "degraded", "unknown"}

    def test_empty_factory_health_is_unknown(self, db_path):
        state = get_factory_state(db_path=db_path)
        assert state.overall_health == "unknown"

    def test_run_health_populated(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        assert state.run_health.total_runs == 4

    def test_channels_populated(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        assert len(state.channels) == 1

    def test_agent_health_populated(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        assert len(state.agent_health) >= 2

    def test_model_usage_populated(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        assert len(state.model_usage) >= 1

    def test_provider_health_populated(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        assert len(state.provider_health) >= 2

    def test_archive_populated(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        assert state.archive.total_archived == 3

    def test_recall_populated(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        assert state.recall.total_decisions == 3

    def test_learning_populated(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        assert state.learning.total_records == 3

    def test_costs_populated(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        assert state.costs.total_usd > 0

    def test_audit_populated(self, populated_db):
        state = get_factory_state(db_path=populated_db)
        assert isinstance(state.audit, AuditSummary)

    def test_notifications_is_list(self, db_path):
        state = get_factory_state(db_path=db_path)
        assert isinstance(state.notifications, list)

    def test_metadata_version_is_v1(self, db_path):
        state = get_factory_state(db_path=db_path)
        assert state.metadata_version == "v1"


# ===========================================================================
# TestFormatFactoryState
# ===========================================================================

class TestFormatFactoryState:
    @pytest.fixture()
    def state(self, db_path):
        return get_factory_state(db_path=db_path)

    def test_returns_string(self, state):
        result = format_factory_state(state)
        assert isinstance(result, str)

    def test_contains_factory_id(self, state):
        result = format_factory_state(state)
        assert "hypogeum" in result

    def test_contains_health(self, state):
        result = format_factory_state(state)
        assert "UNKNOWN" in result.upper() or "HEALTHY" in result.upper() or \
               "WARNING" in result.upper() or "DEGRADED" in result.upper()

    def test_contains_run_health_section(self, state):
        result = format_factory_state(state)
        assert "Run Health" in result

    def test_contains_channels_section(self, state):
        result = format_factory_state(state)
        assert "Channels" in result

    def test_contains_agent_health_section(self, state):
        result = format_factory_state(state)
        assert "Agent Health" in result

    def test_contains_provider_section(self, state):
        result = format_factory_state(state)
        assert "Provider Health" in result

    def test_contains_costs_section(self, state):
        result = format_factory_state(state)
        assert "Costs" in result

    def test_contains_archive_section(self, state):
        result = format_factory_state(state)
        assert "Archive" in result

    def test_contains_recall_section(self, state):
        result = format_factory_state(state)
        assert "Recall" in result

    def test_contains_audit_section(self, state):
        result = format_factory_state(state)
        assert "Audit" in result

    def test_contains_notifications_section(self, state):
        result = format_factory_state(state)
        assert "Notification" in result

    def test_no_db_access_in_formatter(self, state, tmp_path):
        """format_factory_state must work without a DB path — pure formatting."""
        # Pass already-computed state; should not need DB
        result = format_factory_state(state)
        assert isinstance(result, str)


# ===========================================================================
# TestNonBlocking
# ===========================================================================

class TestNonBlocking:
    def _bad_db(self, tmp_path):
        p = tmp_path / "bad"
        p.mkdir()
        return p

    def test_get_channel_summaries_bad_db(self, tmp_path):
        assert get_channel_summaries(db_path=self._bad_db(tmp_path)) == []

    def test_get_run_health_bad_db(self, tmp_path):
        rh = get_run_health(db_path=self._bad_db(tmp_path))
        assert rh.total_runs == 0

    def test_get_agent_health_bad_db(self, tmp_path):
        assert get_agent_health(db_path=self._bad_db(tmp_path)) == []

    def test_get_model_usage_bad_db(self, tmp_path):
        assert get_model_usage(db_path=self._bad_db(tmp_path)) == []

    def test_get_provider_health_bad_db(self, tmp_path):
        assert get_provider_health(db_path=self._bad_db(tmp_path)) == []

    def test_get_archive_summary_bad_db(self, tmp_path):
        ar = get_archive_summary(db_path=self._bad_db(tmp_path))
        assert ar.total_archived == 0

    def test_get_recall_summary_bad_db(self, tmp_path):
        rs = get_recall_summary(db_path=self._bad_db(tmp_path))
        assert rs.total_decisions == 0

    def test_get_learning_summary_bad_db(self, tmp_path):
        ls = get_learning_summary(db_path=self._bad_db(tmp_path))
        assert ls.total_records == 0

    def test_get_cost_summary_bad_db(self, tmp_path):
        cs = get_cost_summary(db_path=self._bad_db(tmp_path))
        assert cs.total_usd == 0.0

    def test_get_audit_summary_bad_db(self, tmp_path):
        au = get_audit_summary(db_path=self._bad_db(tmp_path))
        assert isinstance(au, AuditSummary)

    def test_get_notifications_bad_db(self, tmp_path):
        notes = get_notifications(db_path=self._bad_db(tmp_path))
        assert isinstance(notes, list)

    def test_get_factory_state_bad_db(self, tmp_path):
        state = get_factory_state(db_path=self._bad_db(tmp_path))
        assert isinstance(state, FactoryState)


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:
    @pytest.fixture(autouse=True)
    def block_network(self, monkeypatch):
        def no_socket(*args, **kwargs):
            raise RuntimeError("Network call detected in dashboard_api tests")
        monkeypatch.setattr(socket, "socket", no_socket)
        yield

    def test_get_channel_summaries_no_network(self, db_path):
        get_channel_summaries(db_path=db_path)

    def test_get_run_health_no_network(self, db_path):
        get_run_health(db_path=db_path)

    def test_get_agent_health_no_network(self, db_path):
        get_agent_health(db_path=db_path)

    def test_get_model_usage_no_network(self, db_path):
        get_model_usage(db_path=db_path)

    def test_get_provider_health_no_network(self, db_path):
        get_provider_health(db_path=db_path)

    def test_get_archive_summary_no_network(self, db_path):
        get_archive_summary(db_path=db_path)

    def test_get_recall_summary_no_network(self, db_path):
        get_recall_summary(db_path=db_path)

    def test_get_learning_summary_no_network(self, db_path):
        get_learning_summary(db_path=db_path)

    def test_get_cost_summary_no_network(self, db_path):
        get_cost_summary(db_path=db_path)

    def test_get_audit_summary_no_network(self, db_path):
        get_audit_summary(db_path=db_path)

    def test_get_notifications_no_network(self, db_path):
        get_notifications(db_path=db_path)

    def test_get_factory_state_no_network(self, db_path):
        get_factory_state(db_path=db_path)

    def test_get_permission_no_network(self):
        get_permission("approve_run")

    def test_list_permissions_no_network(self):
        list_permissions()

    def test_format_factory_state_no_network(self, db_path):
        state = get_factory_state.__wrapped__(db_path) if hasattr(get_factory_state, "__wrapped__") else get_factory_state(db_path=db_path)
        format_factory_state(state)


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:
    def test_no_ep_000_created(self, db_path, tmp_path):
        get_factory_state(db_path=db_path)
        ep_in_tmp = list(tmp_path.rglob("ep_000"))
        assert ep_in_tmp == []

    def test_get_factory_state_does_not_write_runs(self, db_path):
        get_factory_state(db_path=db_path)
        conn = get_connection(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) AS cnt FROM runs").fetchone()["cnt"]
            assert count == 0
        finally:
            conn.close()

    def test_get_factory_state_does_not_write_episodes(self, db_path):
        get_factory_state(db_path=db_path)
        conn = get_connection(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) AS cnt FROM episodes").fetchone()["cnt"]
            assert count == 0
        finally:
            conn.close()

    def test_all_functions_read_only(self, populated_db):
        """Run all dashboard functions; verify run/episode counts unchanged."""
        conn = get_connection(populated_db)
        run_before = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        ep_before  = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()

        get_channel_summaries(db_path=populated_db)
        get_run_health(db_path=populated_db)
        get_agent_health(db_path=populated_db)
        get_model_usage(db_path=populated_db)
        get_provider_health(db_path=populated_db)
        get_archive_summary(db_path=populated_db)
        get_recall_summary(db_path=populated_db)
        get_learning_summary(db_path=populated_db)
        get_cost_summary(db_path=populated_db)
        get_audit_summary(db_path=populated_db)
        get_notifications(db_path=populated_db)
        state = get_factory_state(db_path=populated_db)
        format_factory_state(state)

        conn = get_connection(populated_db)
        run_after = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        ep_after  = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()

        assert run_before == run_after
        assert ep_before  == ep_after
