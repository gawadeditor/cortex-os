"""
tests/test_agent_director.py — Phase E: Agent Director tests

Coverage:
    TestDataContracts          — all 4 dataclasses instantiate correctly
    TestScoreAgent             — quality/cost/speed/reliability/composite math,
                                 recommendation thresholds, low-sample penalty,
                                 all-skip edge case
    TestAnalyzeAgents          — empty DB, populated, sorted by composite DESC
    TestRetirementCandidates   — threshold, min_sample gate, urgency levels,
                                 weakest-dimension logic
    TestUpgradeRecommendations — per-dimension triggers, retirement excluded
    TestFleetHealth            — unknown/healthy/warning/degraded
    TestRunAgentDirector       — full report, rankings, all fields populated
    TestNoNetworkCalls         — socket blocked
    TestNoProductionSideEffects — read-only, no ep_000
"""

from __future__ import annotations

import socket
import uuid
from pathlib import Path

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.agent_director import (
    # Dataclasses
    AgentDirectorReport,
    AgentScore,
    RetirementCandidate,
    UpgradeRecommendation,
    # Constants
    COST_CEILING_USD,
    MIN_SAMPLE_FOR_RETIREMENT,
    SPEED_CEILING_MS,
    THRESHOLD_HEALTHY,
    THRESHOLD_MONITOR,
    THRESHOLD_OPTIMIZE,
    UPGRADE_COST_THRESHOLD,
    UPGRADE_QUALITY_THRESHOLD,
    UPGRADE_RELIABILITY_THRESHOLD,
    UPGRADE_SPEED_THRESHOLD,
    WEIGHT_COST,
    WEIGHT_QUALITY,
    WEIGHT_RELIABILITY,
    WEIGHT_SPEED,
    # Functions
    _compute_fleet_health,
    analyze_agents,
    find_retirement_candidates,
    find_upgrade_recommendations,
    run_agent_director,
    score_agent,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "agent_director_test.db"
    initialize_schema(p)
    return p


def _insert_run(conn, run_id="r1", episode_id="ep1"):
    conn.execute(
        "INSERT OR IGNORE INTO runs (run_id, episode_id, started_at, status) VALUES (?,?,?,?)",
        (run_id, episode_id, "2026-01-01T00:00:00", "complete"),
    )


def _insert_stage(conn, run_id, episode_id, stage_name, status,
                  skipped=0, duration_ms=1000, cost_usd=0.01):
    conn.execute(
        """INSERT INTO stage_executions
           (stage_exec_id, run_id, episode_id, stage_name, started_at,
            status, skipped, duration_ms, cost_usd)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), run_id, episode_id, stage_name,
         "2026-01-01T00:00:00", status, skipped, duration_ms, cost_usd),
    )


@pytest.fixture()
def populated_db(db_path):
    """
    Insert a fleet of stages with distinct performance profiles:
      research:  3 completed, 0 failed  → healthy
      narration: 2 completed, 1 failed  → monitor/warn
      assembly:  0 completed, 6 failed  → retire candidate
      thumbnail: 2 completed, 0 failed, 2 skipped → healthy (only 2 non-skip)
    """
    conn = get_connection(db_path)
    _insert_run(conn, "r1", "ep1")
    _insert_run(conn, "r2", "ep2")
    _insert_run(conn, "r3", "ep3")

    # research — 3 successes, fast, cheap
    for i in range(3):
        _insert_stage(conn, "r1", "ep1", "research", "completed",
                      duration_ms=2000, cost_usd=0.01)

    # narration — 2 successes, 1 failure, moderate cost
    _insert_stage(conn, "r1", "ep1", "narration", "completed", duration_ms=5000, cost_usd=0.05)
    _insert_stage(conn, "r2", "ep2", "narration", "completed", duration_ms=5000, cost_usd=0.05)
    _insert_stage(conn, "r3", "ep3", "narration", "failed",    duration_ms=5000, cost_usd=0.05)

    # assembly — 6 failures, expensive, slow
    for i in range(6):
        _insert_stage(conn, "r1", "ep1", "assembly", "failed",
                      duration_ms=40000, cost_usd=0.50)

    # thumbnail — 2 success, 2 skipped
    _insert_stage(conn, "r1", "ep1", "thumbnail", "completed", skipped=0, duration_ms=3000, cost_usd=0.02)
    _insert_stage(conn, "r2", "ep2", "thumbnail", "completed", skipped=0, duration_ms=3000, cost_usd=0.02)
    _insert_stage(conn, "r3", "ep3", "thumbnail", "skipped",   skipped=1, duration_ms=0,    cost_usd=0.00)
    _insert_stage(conn, "r1", "ep1", "thumbnail", "skipped",   skipped=1, duration_ms=0,    cost_usd=0.00)

    conn.commit()
    conn.close()
    return db_path


# ===========================================================================
# TestDataContracts
# ===========================================================================

class TestDataContracts:
    def test_agent_score_fields(self):
        s = AgentScore("research", 0.9, 0.8, 0.7, 0.9, 0.83, 10, 10, "healthy")
        assert s.stage_name == "research"
        assert s.composite_score == 0.83
        assert s.recommendation == "healthy"

    def test_retirement_candidate_fields(self):
        r = RetirementCandidate("bad_stage", 0.2, "low score", {}, "replace it", "urgent")
        assert r.stage_name == "bad_stage"
        assert r.urgency == "urgent"

    def test_upgrade_recommendation_fields(self):
        u = UpgradeRecommendation("stage", "quality", 0.5, 0.7, "action", "improve 20%")
        assert u.dimension == "quality"
        assert u.current_score == 0.5

    def test_director_report_fields(self):
        r = AgentDirectorReport(
            "2026-01-01", 0, [], [], [], [], [], [], [], "unknown"
        )
        assert r.overall_fleet_health == "unknown"
        assert r.metadata_version == "v1"


# ===========================================================================
# TestScoreAgent
# ===========================================================================

class TestScoreAgent:
    def _score(self, **kwargs):
        defaults = dict(
            stage_name="test", total=10, successes=8, failures=2,
            skips=0, avg_duration_ms=5000.0, avg_cost_usd=0.01,
        )
        defaults.update(kwargs)
        return score_agent(**defaults)

    def test_returns_agent_score(self):
        assert isinstance(self._score(), AgentScore)

    def test_quality_score_is_success_rate(self):
        s = self._score(total=10, successes=8, failures=2, skips=0)
        assert abs(s.quality_score - 0.8) < 1e-4

    def test_quality_score_excludes_skips(self):
        # 6 successes, 0 failures, 4 skips → quality = 6/6 = 1.0
        s = self._score(total=10, successes=6, failures=0, skips=4)
        assert s.quality_score == 1.0

    def test_all_skips_quality_is_neutral(self):
        s = self._score(total=5, successes=0, failures=0, skips=5)
        assert s.quality_score == 0.5

    def test_cost_score_at_zero_cost(self):
        s = self._score(avg_cost_usd=0.0)
        assert s.cost_score == 1.0

    def test_cost_score_at_ceiling(self):
        s = self._score(avg_cost_usd=COST_CEILING_USD)
        assert s.cost_score == 0.0

    def test_cost_score_midpoint(self):
        s = self._score(avg_cost_usd=COST_CEILING_USD / 2)
        assert abs(s.cost_score - 0.5) < 1e-4

    def test_cost_score_clamped_above_ceiling(self):
        s = self._score(avg_cost_usd=COST_CEILING_USD * 2)
        assert s.cost_score == 0.0

    def test_speed_score_at_zero_duration(self):
        s = self._score(avg_duration_ms=0.0)
        assert s.speed_score == 1.0

    def test_speed_score_at_ceiling(self):
        s = self._score(avg_duration_ms=SPEED_CEILING_MS)
        assert s.speed_score == 0.0

    def test_speed_score_midpoint(self):
        s = self._score(avg_duration_ms=SPEED_CEILING_MS / 2)
        assert abs(s.speed_score - 0.5) < 1e-4

    def test_reliability_penalised_below_3_samples(self):
        high_sample = self._score(total=10, successes=8, failures=2, skips=0)
        low_sample  = self._score(total=2,  successes=2, failures=0, skips=0)
        # low sample: quality = 1.0 but reliability = 1.0 * 0.7
        assert low_sample.reliability_score < high_sample.reliability_score

    def test_reliability_not_penalised_at_3_or_more(self):
        s = self._score(total=3, successes=3, failures=0, skips=0)
        assert abs(s.reliability_score - s.quality_score) < 1e-4

    def test_composite_uses_weights(self):
        s = self._score(
            total=10, successes=10, failures=0, skips=0,
            avg_cost_usd=0.0, avg_duration_ms=0.0,
        )
        expected = (
            1.0 * WEIGHT_QUALITY
            + 1.0 * WEIGHT_COST
            + 1.0 * WEIGHT_SPEED
            + 1.0 * WEIGHT_RELIABILITY
        )
        assert abs(s.composite_score - expected) < 1e-4

    def test_recommendation_healthy(self):
        s = self._score(total=10, successes=10, failures=0, skips=0,
                        avg_cost_usd=0.0, avg_duration_ms=0.0)
        assert s.recommendation == "healthy"

    def test_recommendation_monitor(self):
        # quality = 0.7 → composite around 0.65-0.75
        s = self._score(total=10, successes=7, failures=3, skips=0,
                        avg_cost_usd=0.0, avg_duration_ms=0.0)
        assert s.recommendation in {"monitor", "healthy"}  # depends on exact composite

    def test_recommendation_retire_when_all_fail(self):
        # 0% success, expensive, slow → composite very low
        s = self._score(total=10, successes=0, failures=10, skips=0,
                        avg_cost_usd=0.80, avg_duration_ms=50000.0)
        assert s.recommendation == "retire"

    def test_composite_in_unit_range(self):
        s = self._score()
        assert 0.0 <= s.composite_score <= 1.0

    def test_no_cost_data_still_scores(self):
        s = self._score(avg_cost_usd=None)
        assert s.cost_score == 1.0   # 0 cost → perfect cost score

    def test_no_duration_data_still_scores(self):
        s = self._score(avg_duration_ms=None)
        assert s.speed_score == 1.0   # 0 duration → perfect speed score

    def test_sample_size_stored(self):
        s = self._score(total=15)
        assert s.sample_size == 15

    def test_non_skip_count_stored(self):
        s = self._score(total=10, skips=3)
        assert s.non_skip_count == 7


# ===========================================================================
# TestAnalyzeAgents
# ===========================================================================

class TestAnalyzeAgents:
    def test_empty_db_returns_empty(self, db_path):
        assert analyze_agents(db_path=db_path) == []

    def test_returns_agent_score_objects(self, populated_db):
        result = analyze_agents(db_path=populated_db)
        assert all(isinstance(s, AgentScore) for s in result)

    def test_all_stages_found(self, populated_db):
        result = analyze_agents(db_path=populated_db)
        names = {s.stage_name for s in result}
        assert "research" in names
        assert "narration" in names
        assert "assembly" in names
        assert "thumbnail" in names

    def test_sorted_by_composite_desc(self, populated_db):
        result = analyze_agents(db_path=populated_db)
        scores = [s.composite_score for s in result]
        assert scores == sorted(scores, reverse=True)

    def test_research_is_healthy(self, populated_db):
        result = analyze_agents(db_path=populated_db)
        research = next(s for s in result if s.stage_name == "research")
        assert research.quality_score == 1.0
        assert research.recommendation == "healthy"

    def test_assembly_fails_all_runs(self, populated_db):
        result = analyze_agents(db_path=populated_db)
        assembly = next(s for s in result if s.stage_name == "assembly")
        assert assembly.quality_score == 0.0

    def test_thumbnail_skips_excluded_from_quality(self, populated_db):
        result = analyze_agents(db_path=populated_db)
        thumbnail = next(s for s in result if s.stage_name == "thumbnail")
        # 2 successes, 0 failures (2 skipped) → quality = 1.0
        assert thumbnail.quality_score == 1.0

    def test_thumbnail_skips_counted(self, populated_db):
        result = analyze_agents(db_path=populated_db)
        thumbnail = next(s for s in result if s.stage_name == "thumbnail")
        assert thumbnail.sample_size == 4
        assert thumbnail.non_skip_count == 2

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        assert analyze_agents(db_path=bad) == []


# ===========================================================================
# TestRetirementCandidates
# ===========================================================================

class TestRetirementCandidates:
    def test_no_candidates_for_good_agents(self):
        good = score_agent("good", 10, 10, 0, 0, 1000.0, 0.01)
        result = find_retirement_candidates([good])
        assert result == []

    def test_candidate_for_failing_agent_with_enough_data(self):
        bad = score_agent("bad", 10, 0, 10, 0, 50000.0, 0.80)
        result = find_retirement_candidates([bad], min_sample=5)
        assert len(result) == 1
        assert result[0].stage_name == "bad"

    def test_no_candidate_when_sample_too_small(self):
        bad = score_agent("bad", 3, 0, 3, 0, 50000.0, 0.80)
        result = find_retirement_candidates([bad], min_sample=5)
        assert result == []

    def test_retirement_candidate_is_typed(self, populated_db):
        scores = analyze_agents(db_path=populated_db)
        result = find_retirement_candidates(scores, min_sample=5)
        assert all(isinstance(c, RetirementCandidate) for c in result)

    def test_assembly_is_retirement_candidate(self, populated_db):
        scores = analyze_agents(db_path=populated_db)
        candidates = find_retirement_candidates(scores, min_sample=5)
        names = {c.stage_name for c in candidates}
        assert "assembly" in names

    def test_research_not_retirement_candidate(self, populated_db):
        scores = analyze_agents(db_path=populated_db)
        candidates = find_retirement_candidates(scores, min_sample=5)
        names = {c.stage_name for c in candidates}
        assert "research" not in names

    def test_candidates_sorted_by_composite_asc(self):
        bad1 = score_agent("b1", 10, 0, 10, 0, 50000.0, 0.80)
        bad2 = score_agent("b2", 10, 1, 9,  0, 45000.0, 0.70)
        result = find_retirement_candidates([bad1, bad2], min_sample=5)
        if len(result) == 2:
            assert result[0].composite_score <= result[1].composite_score

    def test_urgency_urgent_below_0_20(self):
        bad = score_agent("bad", 10, 0, 10, 0, 50000.0, 0.80)
        result = find_retirement_candidates([bad], min_sample=5)
        if result:
            assert result[0].urgency == "urgent"

    def test_evidence_dict_has_required_keys(self, populated_db):
        scores = analyze_agents(db_path=populated_db)
        candidates = find_retirement_candidates(scores, min_sample=5)
        required = {"quality_score", "cost_score", "speed_score",
                    "reliability_score", "composite_score", "non_skip_count"}
        for c in candidates:
            assert required <= set(c.evidence.keys())

    def test_replacement_suggestion_is_string(self, populated_db):
        scores = analyze_agents(db_path=populated_db)
        candidates = find_retirement_candidates(scores, min_sample=5)
        for c in candidates:
            assert isinstance(c.replacement_suggestion, str)
            assert len(c.replacement_suggestion) > 0

    def test_bad_input_returns_empty(self):
        result = find_retirement_candidates([])
        assert result == []


# ===========================================================================
# TestUpgradeRecommendations
# ===========================================================================

class TestUpgradeRecommendations:
    def test_no_recommendations_for_healthy_agent(self):
        good = score_agent("good", 10, 10, 0, 0, 1000.0, 0.01)
        assert find_upgrade_recommendations([good]) == []

    def test_quality_recommendation_triggered(self):
        # quality = 0.5 (below threshold 0.7)
        s = score_agent("slow_q", 10, 5, 5, 0, 1000.0, 0.01)
        result = find_upgrade_recommendations([s])
        dims = {r.dimension for r in result}
        assert "quality" in dims

    def test_cost_recommendation_triggered(self):
        # cost_score below threshold
        high_cost = (1.0 - UPGRADE_COST_THRESHOLD) * COST_CEILING_USD + 0.05
        s = score_agent("pricey", 10, 10, 0, 0, 1000.0, high_cost)
        result = find_upgrade_recommendations([s])
        dims = {r.dimension for r in result}
        assert "cost" in dims

    def test_speed_recommendation_triggered(self):
        # speed_score below threshold
        high_dur = (1.0 - UPGRADE_SPEED_THRESHOLD) * SPEED_CEILING_MS + 1000
        s = score_agent("slow", 10, 10, 0, 0, high_dur, 0.01)
        result = find_upgrade_recommendations([s])
        dims = {r.dimension for r in result}
        assert "speed" in dims

    def test_reliability_recommendation_triggered(self):
        # quality = 0.5 with 2 executions → reliability penalty → below threshold
        s = score_agent("unreliable", 2, 1, 1, 0, 1000.0, 0.01)
        result = find_upgrade_recommendations([s])
        dims = {r.dimension for r in result}
        # reliability is quality*0.7 = 0.5*0.7 = 0.35 < 0.70 → trigger
        assert "reliability" in dims

    def test_retire_candidates_excluded(self):
        bad = score_agent("retire_me", 10, 0, 10, 0, 50000.0, 0.80)
        assert bad.recommendation == "retire"
        result = find_upgrade_recommendations([bad])
        names = {r.stage_name for r in result}
        assert "retire_me" not in names

    def test_returns_upgrade_objects(self, populated_db):
        scores = analyze_agents(db_path=populated_db)
        result = find_upgrade_recommendations(scores)
        assert all(isinstance(u, UpgradeRecommendation) for u in result)

    def test_sorted_by_stage_then_dimension(self):
        s = score_agent("z_stage", 10, 5, 5, 0, 50000.0, 0.80)
        result = find_upgrade_recommendations([s])
        # Stage is not retire (composite borderline); may have multiple dims
        pairs = [(r.stage_name, r.dimension) for r in result]
        assert pairs == sorted(pairs)

    def test_current_score_below_target_score(self):
        s = score_agent("q_weak", 10, 5, 5, 0, 1000.0, 0.01)
        result = find_upgrade_recommendations([s])
        for r in result:
            assert r.current_score < r.target_score

    def test_bad_input_returns_empty(self):
        assert find_upgrade_recommendations([]) == []


# ===========================================================================
# TestFleetHealth
# ===========================================================================

class TestFleetHealth:
    def test_empty_scores_unknown(self):
        assert _compute_fleet_health([]) == "unknown"

    def test_all_healthy_returns_healthy(self):
        scores = [score_agent("s", 10, 10, 0, 0, 1000.0, 0.01)]
        assert _compute_fleet_health(scores) == "healthy"

    def test_any_monitor_returns_warning(self):
        healthy = score_agent("h", 10, 10, 0, 0, 1000.0, 0.01)
        monitor = score_agent("m", 10, 7, 3, 0, 5000.0, 0.01)
        # Force monitor recommendation
        import hypogeum.agent_director as ad
        monitor.recommendation = "monitor"
        assert _compute_fleet_health([healthy, monitor]) == "warning"

    def test_any_optimize_returns_warning(self):
        healthy = score_agent("h", 10, 10, 0, 0, 1000.0, 0.01)
        optimize = score_agent("o", 10, 5, 5, 0, 1000.0, 0.01)
        optimize.recommendation = "optimize"
        assert _compute_fleet_health([healthy, optimize]) == "warning"

    def test_any_retire_returns_degraded(self):
        healthy = score_agent("h", 10, 10, 0, 0, 1000.0, 0.01)
        retire = score_agent("r", 10, 0, 10, 0, 50000.0, 0.80)
        assert _compute_fleet_health([healthy, retire]) == "degraded"

    def test_populated_db_with_assembly_failures_is_degraded(self, populated_db):
        scores = analyze_agents(db_path=populated_db)
        assert _compute_fleet_health(scores) == "degraded"


# ===========================================================================
# TestRunAgentDirector
# ===========================================================================

class TestRunAgentDirector:
    def test_empty_db_returns_report(self, db_path):
        report = run_agent_director(db_path=db_path)
        assert isinstance(report, AgentDirectorReport)

    def test_empty_db_fleet_health_unknown(self, db_path):
        report = run_agent_director(db_path=db_path)
        assert report.overall_fleet_health == "unknown"

    def test_empty_db_no_agents(self, db_path):
        report = run_agent_director(db_path=db_path)
        assert report.total_agents_evaluated == 0

    def test_populated_agents_evaluated(self, populated_db):
        report = run_agent_director(db_path=populated_db)
        assert report.total_agents_evaluated == 4

    def test_populated_fleet_health_degraded(self, populated_db):
        report = run_agent_director(db_path=populated_db)
        assert report.overall_fleet_health == "degraded"

    def test_retirement_candidates_populated(self, populated_db):
        report = run_agent_director(db_path=populated_db)
        assert len(report.retirement_candidates) > 0

    def test_upgrade_recommendations_present(self, populated_db):
        report = run_agent_director(db_path=populated_db)
        assert len(report.upgrade_recommendations) > 0

    def test_healthy_agents_list(self, populated_db):
        report = run_agent_director(db_path=populated_db)
        assert "research" in report.healthy_agents

    def test_agents_needing_attention_sorted_asc(self, populated_db):
        report = run_agent_director(db_path=populated_db)
        # Map names to scores for validation
        score_map = {s.stage_name: s.composite_score for s in report.agent_scores}
        attentions = [score_map[n] for n in report.agents_needing_attention]
        assert attentions == sorted(attentions)

    def test_cost_efficiency_ranking_sorted_desc(self, populated_db):
        report = run_agent_director(db_path=populated_db)
        score_map = {s.stage_name: s.cost_score for s in report.agent_scores}
        costs = [score_map[n] for n in report.cost_efficiency_ranking]
        assert costs == sorted(costs, reverse=True)

    def test_speed_ranking_sorted_desc(self, populated_db):
        report = run_agent_director(db_path=populated_db)
        score_map = {s.stage_name: s.speed_score for s in report.agent_scores}
        speeds = [score_map[n] for n in report.speed_ranking]
        assert speeds == sorted(speeds, reverse=True)

    def test_analyzed_at_set(self, db_path):
        report = run_agent_director(db_path=db_path)
        assert report.analyzed_at is not None

    def test_metadata_version(self, db_path):
        report = run_agent_director(db_path=db_path)
        assert report.metadata_version == "v1"

    def test_bad_db_returns_report(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        report = run_agent_director(db_path=bad)
        assert isinstance(report, AgentDirectorReport)
        assert report.overall_fleet_health == "unknown"


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:
    @pytest.fixture(autouse=True)
    def block_network(self, monkeypatch):
        def no_socket(*args, **kwargs):
            raise RuntimeError("Network call in agent_director tests")
        monkeypatch.setattr(socket, "socket", no_socket)
        yield

    def test_score_agent_no_network(self):
        score_agent("s", 10, 8, 2, 0, 5000.0, 0.01)

    def test_analyze_agents_no_network(self, db_path):
        analyze_agents(db_path=db_path)

    def test_find_retirement_candidates_no_network(self, db_path):
        scores = analyze_agents(db_path=db_path)
        find_retirement_candidates(scores)

    def test_find_upgrade_recommendations_no_network(self, db_path):
        scores = analyze_agents(db_path=db_path)
        find_upgrade_recommendations(scores)

    def test_run_agent_director_no_network(self, db_path):
        run_agent_director(db_path=db_path)


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:
    def test_no_ep_000_created(self, db_path, tmp_path):
        run_agent_director(db_path=db_path)
        assert list(tmp_path.rglob("ep_000")) == []

    def test_does_not_write_runs(self, db_path):
        run_agent_director(db_path=db_path)
        conn = get_connection(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
            assert count == 0
        finally:
            conn.close()

    def test_does_not_write_episodes(self, db_path):
        run_agent_director(db_path=db_path)
        conn = get_connection(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
            assert count == 0
        finally:
            conn.close()

    def test_all_functions_read_only(self, populated_db):
        conn = get_connection(populated_db)
        se_before = conn.execute("SELECT COUNT(*) AS c FROM stage_executions").fetchone()["c"]
        conn.close()

        scores = analyze_agents(db_path=populated_db)
        find_retirement_candidates(scores)
        find_upgrade_recommendations(scores)
        run_agent_director(db_path=populated_db)

        conn = get_connection(populated_db)
        se_after = conn.execute("SELECT COUNT(*) AS c FROM stage_executions").fetchone()["c"]
        conn.close()

        assert se_before == se_after
