"""
tests/test_agent_auditor.py — Agent Performance Auditor tests

Coverage:
    TestAuditStage              — success rate, skip exclusion, empty stage, cost aggregation
    TestAuditGates              — pass/block/warning rates, APPROVED_WITH_FLAGS, empty
    TestAuditAssetGeneration    — retries, first-attempt success, cost/success, most_common_error
    TestGetFailureSummary       — grouping, ordering, limit, empty
    TestRunHealthSummary        — completion rate, empty DB zero-fill
    TestAuditAgents             — full report structure, generated_at present
    TestEmptyDB                 — all functions tolerate empty tables
    TestNoNetworkCalls          — no sockets opened
    TestNoProductionSideEffects — no writes to ep_000, no external calls
"""

from __future__ import annotations

import socket
import uuid
from pathlib import Path

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.agent_auditor import (
    AgentAuditReport,
    AssetAuditResult,
    FailureRecord,
    GateAuditResult,
    RunHealthSummary,
    StageAuditResult,
    audit_agents,
    audit_asset_generation,
    audit_gates,
    audit_stage,
    get_failure_summary,
    run_health_summary,
    _safe_rate,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUN_A = "run-audit-aaa"
_RUN_B = "run-audit-bbb"
_EP    = "ep_audit_test"
_TS    = "2026-06-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    """Isolated in-memory-like SQLite DB initialised with v1+v2 schema."""
    p = tmp_path / "audit_test.db"
    initialize_schema(p)
    conn = get_connection(p)
    try:
        # Runs
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status) VALUES (?,?,?,?)",
            (_RUN_A, _EP, _TS, "complete"),
        )
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status) VALUES (?,?,?,?)",
            (_RUN_B, _EP, _TS, "failed"),
        )
        conn.commit()
    finally:
        conn.close()
    return p


def _insert_stage(conn, run_id, stage_name, status, skipped=0,
                  duration_ms=None, cost_usd=None, episode_id=_EP):
    conn.execute(
        """INSERT INTO stage_executions
           (stage_exec_id, run_id, episode_id, stage_name, started_at,
            status, skipped, duration_ms, cost_usd)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), run_id, episode_id, stage_name, _TS,
         status, skipped, duration_ms, cost_usd),
    )


def _insert_gate(conn, run_id, gate_name, verdict, episode_id=_EP):
    conn.execute(
        """INSERT INTO gate_verdicts
           (verdict_id, run_id, episode_id, gate_name, verdict, evaluated_at)
           VALUES (?,?,?,?,?,?)""",
        (str(uuid.uuid4()), run_id, episode_id, gate_name, verdict, _TS),
    )


def _insert_av(conn, run_id, asset_type, provider, status,
               attempt_number=1, cost_usd=None, error_type=None,
               episode_id=_EP):
    conn.execute(
        """INSERT INTO asset_versions
           (version_id, asset_slot_id, run_id, episode_id, asset_type,
            provider, attempted_at, status, attempt_number, cost_usd, error_type)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), str(uuid.uuid4()), run_id, episode_id,
         asset_type, provider, _TS, status, attempt_number, cost_usd, error_type),
    )


# ===========================================================================
# TestSafeRate
# ===========================================================================

class TestSafeRate:
    def test_normal(self):
        assert _safe_rate(3, 4) == pytest.approx(0.75)

    def test_zero_denom(self):
        assert _safe_rate(0, 0) == 0.0

    def test_numerator_zero(self):
        assert _safe_rate(0, 5) == 0.0

    def test_full(self):
        assert _safe_rate(7, 7) == pytest.approx(1.0)


# ===========================================================================
# TestAuditStage
# ===========================================================================

class TestAuditStage:
    def test_unknown_stage_returns_none(self, db_path):
        assert audit_stage("nonexistent_stage", db_path=db_path) is None

    def test_basic_success_rate(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_stage(conn, _RUN_A, "vpd", "completed", duration_ms=500, cost_usd=0.10)
            _insert_stage(conn, _RUN_A, "vpd", "completed", duration_ms=400, cost_usd=0.10)
            _insert_stage(conn, _RUN_A, "vpd", "failed",   duration_ms=200)
            conn.commit()
        finally:
            conn.close()
        result = audit_stage("vpd", db_path=db_path)
        assert isinstance(result, StageAuditResult)
        assert result.stage_name == "vpd"
        assert result.total_executions == 3
        assert result.successes == 2
        assert result.failures == 1
        assert result.skips == 0
        assert result.success_rate == pytest.approx(2 / 3)

    def test_skips_excluded_from_success_denominator(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_stage(conn, _RUN_A, "sds", "completed")
            _insert_stage(conn, _RUN_A, "sds", "completed")
            _insert_stage(conn, _RUN_A, "sds", "skipped", skipped=1)
            conn.commit()
        finally:
            conn.close()
        result = audit_stage("sds", db_path=db_path)
        # denominator = 3 - 1 skip = 2; 2/2 = 1.0
        assert result.success_rate == pytest.approx(1.0)
        assert result.total_executions == 3
        assert result.skips == 1

    def test_all_skipped_success_rate_zero(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_stage(conn, _RUN_A, "all_skip", "skipped", skipped=1)
            _insert_stage(conn, _RUN_A, "all_skip", "skipped", skipped=1)
            conn.commit()
        finally:
            conn.close()
        result = audit_stage("all_skip", db_path=db_path)
        assert result.success_rate == 0.0

    def test_cost_aggregation(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_stage(conn, _RUN_A, "narr", "completed", cost_usd=0.05)
            _insert_stage(conn, _RUN_A, "narr", "completed", cost_usd=0.15)
            conn.commit()
        finally:
            conn.close()
        result = audit_stage("narr", db_path=db_path)
        assert result.total_cost_usd == pytest.approx(0.20)

    def test_avg_duration_only_completed(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_stage(conn, _RUN_A, "tig", "completed", duration_ms=1000)
            _insert_stage(conn, _RUN_A, "tig", "completed", duration_ms=2000)
            _insert_stage(conn, _RUN_A, "tig", "failed",    duration_ms=500)
            conn.commit()
        finally:
            conn.close()
        result = audit_stage("tig", db_path=db_path)
        # AVG only on completed rows → (1000+2000)/2 = 1500
        assert result.avg_duration_ms == pytest.approx(1500.0)

    def test_episode_filter(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_stage(conn, _RUN_A, "filt", "completed", episode_id="ep_other")
            _insert_stage(conn, _RUN_A, "filt", "failed",    episode_id=_EP)
            conn.commit()
        finally:
            conn.close()
        result = audit_stage("filt", db_path=db_path, episode_id=_EP)
        assert result.total_executions == 1
        assert result.failures == 1

    def test_returns_none_on_bad_db(self, tmp_path):
        bad_path = tmp_path / "does_not_exist.db"
        assert audit_stage("anything", db_path=bad_path) is None


# ===========================================================================
# TestAuditGates
# ===========================================================================

class TestAuditGates:
    def test_empty_returns_empty_list(self, db_path):
        assert audit_gates(db_path=db_path) == []

    def test_approved_counts_as_pass(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_gate(conn, _RUN_A, "bga_p1", "APPROVED")
            _insert_gate(conn, _RUN_A, "bga_p1", "APPROVED")
            conn.commit()
        finally:
            conn.close()
        results = audit_gates(db_path=db_path)
        assert len(results) == 1
        g = results[0]
        assert g.gate_name == "bga_p1"
        assert g.passes == 2
        assert g.pass_rate == pytest.approx(1.0)

    def test_approved_with_flags_counts_as_pass(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_gate(conn, _RUN_A, "g1", "APPROVED_WITH_FLAGS")
            _insert_gate(conn, _RUN_A, "g1", "REJECTED")
            conn.commit()
        finally:
            conn.close()
        g = audit_gates(db_path=db_path)[0]
        assert g.passes == 1
        assert g.blocks == 1
        assert g.pass_rate == pytest.approx(0.5)
        assert g.block_rate == pytest.approx(0.5)

    def test_blocked_counts_as_block(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_gate(conn, _RUN_A, "qa", "BLOCKED")
            conn.commit()
        finally:
            conn.close()
        g = audit_gates(db_path=db_path)[0]
        assert g.blocks == 1
        assert g.block_rate == pytest.approx(1.0)

    def test_warning_counted_separately(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_gate(conn, _RUN_A, "compliance", "WARNING")
            _insert_gate(conn, _RUN_A, "compliance", "APPROVED")
            conn.commit()
        finally:
            conn.close()
        g = audit_gates(db_path=db_path)[0]
        assert g.warnings == 1
        assert g.passes == 1

    def test_multiple_gates_sorted(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_gate(conn, _RUN_A, "z_gate", "APPROVED")
            _insert_gate(conn, _RUN_A, "a_gate", "APPROVED")
            conn.commit()
        finally:
            conn.close()
        names = [g.gate_name for g in audit_gates(db_path=db_path)]
        assert names == ["a_gate", "z_gate"]

    def test_episode_filter(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_gate(conn, _RUN_A, "gf", "APPROVED", episode_id="ep_other")
            _insert_gate(conn, _RUN_A, "gf", "REJECTED", episode_id=_EP)
            conn.commit()
        finally:
            conn.close()
        results = audit_gates(db_path=db_path, episode_id=_EP)
        assert len(results) == 1
        assert results[0].total_evaluations == 1
        assert results[0].blocks == 1

    def test_returns_empty_on_bad_db(self, tmp_path):
        assert audit_gates(db_path=tmp_path / "nope.db") == []


# ===========================================================================
# TestAuditAssetGeneration
# ===========================================================================

class TestAuditAssetGeneration:
    def test_empty_returns_empty_list(self, db_path):
        assert audit_asset_generation(db_path=db_path) == []

    def test_basic_success_rate(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "succeeded", attempt_number=1, cost_usd=0.15)
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed",    attempt_number=1)
            conn.commit()
        finally:
            conn.close()
        results = audit_asset_generation(db_path=db_path)
        assert len(results) == 1
        r = results[0]
        assert r.asset_type == "video_clip"
        assert r.provider == "muapi"
        assert r.total_attempts == 2
        assert r.successes == 1
        assert r.failures == 1
        assert r.success_rate == pytest.approx(0.5)

    def test_retry_rate(self, db_path):
        conn = get_connection(db_path)
        try:
            # 1 first attempt, 2 retries
            _insert_av(conn, _RUN_A, "thumbnail", "dalle", "succeeded", attempt_number=1)
            _insert_av(conn, _RUN_A, "thumbnail", "dalle", "succeeded", attempt_number=2)
            _insert_av(conn, _RUN_A, "thumbnail", "dalle", "failed",    attempt_number=3)
            conn.commit()
        finally:
            conn.close()
        r = audit_asset_generation(db_path=db_path)[0]
        assert r.retries == 2
        assert r.retry_rate == pytest.approx(2 / 3)

    def test_first_attempt_success(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "narration", "elevenlabs", "succeeded", attempt_number=1)
            _insert_av(conn, _RUN_A, "narration", "elevenlabs", "succeeded", attempt_number=1)
            _insert_av(conn, _RUN_A, "narration", "elevenlabs", "failed",    attempt_number=1)
            conn.commit()
        finally:
            conn.close()
        r = audit_asset_generation(db_path=db_path)[0]
        assert r.first_attempt_successes == 2

    def test_avg_cost_per_success(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "thumbnail", "dalle", "succeeded", cost_usd=0.04)
            _insert_av(conn, _RUN_A, "thumbnail", "dalle", "succeeded", cost_usd=0.08)
            _insert_av(conn, _RUN_A, "thumbnail", "dalle", "failed",    cost_usd=0.04)
            conn.commit()
        finally:
            conn.close()
        r = audit_asset_generation(db_path=db_path)[0]
        # Only success costs: 0.04 + 0.08 = 0.12 / 2 = 0.06
        assert r.avg_cost_per_success == pytest.approx(0.06)

    def test_avg_cost_none_when_no_successes(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed")
            conn.commit()
        finally:
            conn.close()
        r = audit_asset_generation(db_path=db_path)[0]
        assert r.avg_cost_per_success is None

    def test_most_common_error_populated(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed", error_type="MUAPIQuotaExhausted")
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed", error_type="MUAPIQuotaExhausted")
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed", error_type="Timeout")
            conn.commit()
        finally:
            conn.close()
        r = audit_asset_generation(db_path=db_path)[0]
        assert r.most_common_error == "MUAPIQuotaExhausted"

    def test_most_common_error_none_when_no_failures(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "narration", "elevenlabs", "succeeded")
            conn.commit()
        finally:
            conn.close()
        r = audit_asset_generation(db_path=db_path)[0]
        assert r.most_common_error is None

    def test_asset_type_filter(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "succeeded")
            _insert_av(conn, _RUN_A, "thumbnail",  "dalle", "failed")
            conn.commit()
        finally:
            conn.close()
        results = audit_asset_generation(asset_type="thumbnail", db_path=db_path)
        assert len(results) == 1
        assert results[0].asset_type == "thumbnail"

    def test_episode_filter(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "succeeded", episode_id="ep_other")
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed",    episode_id=_EP)
            conn.commit()
        finally:
            conn.close()
        results = audit_asset_generation(db_path=db_path, episode_id=_EP)
        assert len(results) == 1
        assert results[0].total_attempts == 1
        assert results[0].failures == 1

    def test_multiple_providers_separate_rows(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi",  "succeeded")
            _insert_av(conn, _RUN_A, "video_clip", "runway", "succeeded")
            conn.commit()
        finally:
            conn.close()
        results = audit_asset_generation(db_path=db_path)
        providers = {r.provider for r in results}
        assert providers == {"muapi", "runway"}

    def test_returns_empty_on_bad_db(self, tmp_path):
        assert audit_asset_generation(db_path=tmp_path / "nope.db") == []


# ===========================================================================
# TestGetFailureSummary
# ===========================================================================

class TestGetFailureSummary:
    def test_empty_returns_empty_list(self, db_path):
        assert get_failure_summary(db_path=db_path) == []

    def test_sorted_by_count_desc(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed", error_type="Timeout")
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed", error_type="QuotaError")
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed", error_type="QuotaError")
            conn.commit()
        finally:
            conn.close()
        results = get_failure_summary(db_path=db_path)
        assert results[0].error_type == "QuotaError"
        assert results[0].count == 2
        assert results[1].error_type == "Timeout"
        assert results[1].count == 1

    def test_groups_by_error_type_and_provider(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "thumbnail", "dalle", "failed", error_type="RateLimited")
            _insert_av(conn, _RUN_A, "thumbnail", "muapi", "failed", error_type="RateLimited")
            conn.commit()
        finally:
            conn.close()
        results = get_failure_summary(db_path=db_path)
        # Different providers → separate rows even with same error_type
        assert len(results) == 2
        providers = {r.provider for r in results}
        assert providers == {"dalle", "muapi"}

    def test_excludes_null_error_type(self, db_path):
        conn = get_connection(db_path)
        try:
            # failed but no error_type (should be excluded)
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed", error_type=None)
            conn.commit()
        finally:
            conn.close()
        assert get_failure_summary(db_path=db_path) == []

    def test_limit_respected(self, db_path):
        conn = get_connection(db_path)
        try:
            for i in range(5):
                _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed",
                           error_type=f"Error{i}")
            conn.commit()
        finally:
            conn.close()
        results = get_failure_summary(db_path=db_path, limit=3)
        assert len(results) == 3

    def test_episode_filter(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed",
                       error_type="E1", episode_id="ep_other")
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed",
                       error_type="E2", episode_id=_EP)
            conn.commit()
        finally:
            conn.close()
        results = get_failure_summary(db_path=db_path, episode_id=_EP)
        assert len(results) == 1
        assert results[0].error_type == "E2"

    def test_result_fields_populated(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "narration", "elevenlabs", "failed",
                       error_type="AuthError")
            conn.commit()
        finally:
            conn.close()
        r = get_failure_summary(db_path=db_path)[0]
        assert isinstance(r, FailureRecord)
        assert r.error_type == "AuthError"
        assert r.asset_type == "narration"
        assert r.provider == "elevenlabs"
        assert r.count == 1

    def test_returns_empty_on_bad_db(self, tmp_path):
        assert get_failure_summary(db_path=tmp_path / "nope.db") == []


# ===========================================================================
# TestRunHealthSummary
# ===========================================================================

class TestRunHealthSummary:
    def test_basic_counts(self, db_path):
        # db_path fixture already has 1 complete + 1 failed run
        result = run_health_summary(db_path=db_path)
        assert isinstance(result, RunHealthSummary)
        assert result.total_runs == 2
        assert result.completed == 1
        assert result.failed == 1
        assert result.in_progress == 0

    def test_completion_rate(self, db_path):
        result = run_health_summary(db_path=db_path)
        assert result.completion_rate == pytest.approx(0.5)

    def test_in_progress_count(self, db_path):
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT INTO runs (run_id, episode_id, started_at, status) VALUES (?,?,?,?)",
                ("run-running", _EP, _TS, "running"),
            )
            conn.commit()
        finally:
            conn.close()
        result = run_health_summary(db_path=db_path)
        assert result.in_progress == 1

    def test_empty_db_returns_zero_filled(self, tmp_path):
        p = tmp_path / "empty.db"
        initialize_schema(p)
        result = run_health_summary(db_path=p)
        assert result.total_runs == 0
        assert result.completion_rate == 0.0
        assert result.avg_duration_ms is None
        assert result.total_cost_usd == 0.0

    def test_bad_db_returns_zero_filled(self, tmp_path):
        result = run_health_summary(db_path=tmp_path / "nope.db")
        assert result.total_runs == 0
        assert result.completion_rate == 0.0

    def test_episode_filter(self, db_path):
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT INTO runs (run_id, episode_id, started_at, status) VALUES (?,?,?,?)",
                ("run-other", "ep_other", _TS, "complete"),
            )
            conn.commit()
        finally:
            conn.close()
        result = run_health_summary(db_path=db_path, episode_id=_EP)
        # Only the 2 runs for _EP
        assert result.total_runs == 2

    def test_total_cost_usd_aggregated(self, db_path):
        # Patch total_cost_usd via direct UPDATE (v2 column migration added it)
        conn = get_connection(db_path)
        try:
            conn.execute(
                "UPDATE runs SET total_cost_usd=? WHERE run_id=?",
                (1.50, _RUN_A),
            )
            conn.execute(
                "UPDATE runs SET total_cost_usd=? WHERE run_id=?",
                (0.50, _RUN_B),
            )
            conn.commit()
        finally:
            conn.close()
        result = run_health_summary(db_path=db_path)
        assert result.total_cost_usd == pytest.approx(2.0)


# ===========================================================================
# TestAuditAgents
# ===========================================================================

class TestAuditAgents:
    def test_returns_agent_audit_report(self, db_path):
        result = audit_agents(db_path=db_path)
        assert isinstance(result, AgentAuditReport)

    def test_generated_at_present(self, db_path):
        result = audit_agents(db_path=db_path)
        # Must be a non-empty ISO timestamp string
        assert isinstance(result.generated_at, str)
        assert len(result.generated_at) > 10
        assert "T" in result.generated_at

    def test_run_count_populated(self, db_path):
        result = audit_agents(db_path=db_path)
        assert result.run_count == 2

    def test_stages_from_stage_executions(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_stage(conn, _RUN_A, "vpd", "completed")
            _insert_stage(conn, _RUN_A, "tig", "failed")
            conn.commit()
        finally:
            conn.close()
        result = audit_agents(db_path=db_path)
        stage_names = {s.stage_name for s in result.stages}
        assert "vpd" in stage_names
        assert "tig" in stage_names

    def test_gates_populated(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_gate(conn, _RUN_A, "bga_p1", "APPROVED")
            conn.commit()
        finally:
            conn.close()
        result = audit_agents(db_path=db_path)
        assert any(g.gate_name == "bga_p1" for g in result.gates)

    def test_assets_populated(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "succeeded")
            conn.commit()
        finally:
            conn.close()
        result = audit_agents(db_path=db_path)
        assert any(a.asset_type == "video_clip" for a in result.assets)

    def test_top_failures_populated(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed",
                       error_type="QuotaError")
            conn.commit()
        finally:
            conn.close()
        result = audit_agents(db_path=db_path)
        assert any(f.error_type == "QuotaError" for f in result.top_failures)

    def test_run_health_included(self, db_path):
        result = audit_agents(db_path=db_path)
        assert result.run_health is not None
        assert isinstance(result.run_health, RunHealthSummary)

    def test_empty_db_returns_empty_report(self, tmp_path):
        p = tmp_path / "empty.db"
        initialize_schema(p)
        result = audit_agents(db_path=p)
        assert isinstance(result, AgentAuditReport)
        assert result.stages == []
        assert result.gates == []
        assert result.assets == []
        assert result.top_failures == []

    def test_bad_db_does_not_raise(self, tmp_path):
        result = audit_agents(db_path=tmp_path / "nope.db")
        assert isinstance(result, AgentAuditReport)


# ===========================================================================
# TestEmptyDB
# ===========================================================================

class TestEmptyDB:
    @pytest.fixture()
    def empty_db(self, tmp_path):
        p = tmp_path / "empty.db"
        initialize_schema(p)
        return p

    def test_audit_stage_empty(self, empty_db):
        assert audit_stage("anything", db_path=empty_db) is None

    def test_audit_gates_empty(self, empty_db):
        assert audit_gates(db_path=empty_db) == []

    def test_audit_asset_generation_empty(self, empty_db):
        assert audit_asset_generation(db_path=empty_db) == []

    def test_get_failure_summary_empty(self, empty_db):
        assert get_failure_summary(db_path=empty_db) == []

    def test_run_health_summary_empty(self, empty_db):
        h = run_health_summary(db_path=empty_db)
        assert h.total_runs == 0
        assert h.completion_rate == 0.0

    def test_audit_agents_empty(self, empty_db):
        r = audit_agents(db_path=empty_db)
        assert r.run_count == 0
        assert r.stages == []


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:
    """Ensure agent_auditor never opens network connections."""

    def _block_socket(self, monkeypatch):
        original = socket.socket

        def no_socket(*args, **kwargs):
            raise AssertionError("agent_auditor opened a socket — no network calls allowed")

        monkeypatch.setattr(socket, "socket", no_socket)

    def test_audit_stage_no_network(self, db_path, monkeypatch):
        self._block_socket(monkeypatch)
        audit_stage("vpd", db_path=db_path)

    def test_audit_gates_no_network(self, db_path, monkeypatch):
        self._block_socket(monkeypatch)
        audit_gates(db_path=db_path)

    def test_audit_asset_generation_no_network(self, db_path, monkeypatch):
        self._block_socket(monkeypatch)
        audit_asset_generation(db_path=db_path)

    def test_get_failure_summary_no_network(self, db_path, monkeypatch):
        self._block_socket(monkeypatch)
        get_failure_summary(db_path=db_path)

    def test_run_health_summary_no_network(self, db_path, monkeypatch):
        self._block_socket(monkeypatch)
        run_health_summary(db_path=db_path)

    def test_audit_agents_no_network(self, db_path, monkeypatch):
        self._block_socket(monkeypatch)
        audit_agents(db_path=db_path)


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:
    """Ensure agent_auditor makes no writes and has no production side effects."""

    def test_audit_stage_is_read_only(self, db_path):
        conn = get_connection(db_path)
        try:
            row_count_before = conn.execute(
                "SELECT COUNT(*) FROM stage_executions"
            ).fetchone()[0]
        finally:
            conn.close()
        audit_stage("vpd", db_path=db_path)
        conn = get_connection(db_path)
        try:
            row_count_after = conn.execute(
                "SELECT COUNT(*) FROM stage_executions"
            ).fetchone()[0]
        finally:
            conn.close()
        assert row_count_before == row_count_after

    def test_audit_gates_is_read_only(self, db_path):
        conn = get_connection(db_path)
        try:
            before = conn.execute("SELECT COUNT(*) FROM gate_verdicts").fetchone()[0]
        finally:
            conn.close()
        audit_gates(db_path=db_path)
        conn = get_connection(db_path)
        try:
            after = conn.execute("SELECT COUNT(*) FROM gate_verdicts").fetchone()[0]
        finally:
            conn.close()
        assert before == after

    def test_audit_asset_generation_is_read_only(self, db_path):
        conn = get_connection(db_path)
        try:
            before = conn.execute("SELECT COUNT(*) FROM asset_versions").fetchone()[0]
        finally:
            conn.close()
        audit_asset_generation(db_path=db_path)
        conn = get_connection(db_path)
        try:
            after = conn.execute("SELECT COUNT(*) FROM asset_versions").fetchone()[0]
        finally:
            conn.close()
        assert before == after

    def test_no_ep_000_touched(self, db_path):
        """No function should create or touch ep_000 data."""
        audit_agents(db_path=db_path)
        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE episode_id='ep_000'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert rows == 0

    def test_no_writes_to_runs(self, db_path):
        run_ids_before = set()
        conn = get_connection(db_path)
        try:
            for r in conn.execute("SELECT run_id FROM runs").fetchall():
                run_ids_before.add(r[0])
        finally:
            conn.close()
        audit_agents(db_path=db_path)
        conn = get_connection(db_path)
        try:
            run_ids_after = set(
                r[0] for r in conn.execute("SELECT run_id FROM runs").fetchall()
            )
        finally:
            conn.close()
        assert run_ids_before == run_ids_after
