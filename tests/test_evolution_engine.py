"""
tests/test_evolution_engine.py — Factory Evolution Engine tests

Coverage:
    TestGetProviderRecommendations  — failure rate threshold, sample size guard,
                                      episode filter, sort order, bad DB
    TestGetCostRecommendations      — over/under budget, no ceiling, episode filter
    TestGetGateRecommendations      — block rate threshold, sample size guard,
                                      investigate vs. relax, sort order
    TestAnalyzeEpisode              — signal summaries, per-episode recs
    TestRecordAdjustment            — writes row, returns UUID, bad DB returns None
    TestEvolve                      — full report, record=True persists rows,
                                      episode_count, no-write when record=False
    TestEmptyDB                     — all functions tolerate empty tables
    TestNoNetworkCalls              — no sockets opened
    TestNoProductionSideEffects     — no ep_000, no writes when record=False
"""

from __future__ import annotations

import socket
import uuid

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.evolution_engine import (
    CostRecommendation,
    EpisodeEvolutionReport,
    EvolutionReport,
    GateRecommendation,
    LearningSignalSummary,
    ProviderRecommendation,
    analyze_episode,
    evolve,
    get_cost_recommendations,
    get_gate_recommendations,
    get_provider_recommendations,
    record_adjustment,
    _safe_rate,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUN_A = "run-evo-aaa"
_RUN_B = "run-evo-bbb"
_EP    = "ep_evo_test"
_EP2   = "ep_evo_test2"
_TS    = "2026-06-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "evo_test.db"
    initialize_schema(p)
    conn = get_connection(p)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status) VALUES (?,?,?,?)",
            (_RUN_A, _EP, _TS, "complete"),
        )
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status) VALUES (?,?,?,?)",
            (_RUN_B, _EP2, _TS, "complete"),
        )
        conn.commit()
    finally:
        conn.close()
    return p


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


def _insert_gate(conn, run_id, gate_name, verdict, episode_id=_EP):
    conn.execute(
        """INSERT INTO gate_verdicts
           (verdict_id, run_id, episode_id, gate_name, verdict, evaluated_at)
           VALUES (?,?,?,?,?,?)""",
        (str(uuid.uuid4()), run_id, episode_id, gate_name, verdict, _TS),
    )


def _insert_learning(conn, run_id, episode_id, signal_type,
                     signal_value=None, signal_label=None, recommendation=None):
    conn.execute(
        """INSERT INTO learning_records
           (learning_id, episode_id, run_id, recorded_at, signal_type,
            signal_value, signal_label, recommendation)
           VALUES (?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), episode_id, run_id, _TS, signal_type,
         signal_value, signal_label, recommendation),
    )


# ===========================================================================
# TestSafeRate
# ===========================================================================

class TestSafeRate:
    def test_zero_denom(self):
        assert _safe_rate(0, 0) == 0.0

    def test_normal(self):
        assert _safe_rate(1, 4) == pytest.approx(0.25)

    def test_full(self):
        assert _safe_rate(5, 5) == pytest.approx(1.0)


# ===========================================================================
# TestGetProviderRecommendations
# ===========================================================================

class TestGetProviderRecommendations:
    def test_empty_returns_empty(self, db_path):
        assert get_provider_recommendations(db_path=db_path) == []

    def test_below_sample_size_ignored(self, db_path):
        """Combinations with < 3 attempts are not flagged."""
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed")
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed")
            conn.commit()
        finally:
            conn.close()
        # Only 2 attempts → below _MIN_SAMPLE_SIZE (3)
        assert get_provider_recommendations(db_path=db_path) == []

    def test_flagged_above_threshold(self, db_path):
        conn = get_connection(db_path)
        try:
            # 3 attempts, 2 failures → 66 % ≥ 20 % default threshold
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed")
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed")
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "succeeded")
            conn.commit()
        finally:
            conn.close()
        recs = get_provider_recommendations(db_path=db_path)
        assert len(recs) == 1
        r = recs[0]
        assert r.asset_type == "video_clip"
        assert r.current_provider == "muapi"
        assert r.failure_rate == pytest.approx(2 / 3)
        assert r.total_attempts == 3

    def test_action_swap_above_50_pct(self, db_path):
        conn = get_connection(db_path)
        try:
            for _ in range(3):
                _insert_av(conn, _RUN_A, "thumbnail", "dalle", "failed")
            conn.commit()
        finally:
            conn.close()
        recs = get_provider_recommendations(db_path=db_path)
        assert recs[0].action == "swap"

    def test_action_monitor_between_20_and_50_pct(self, db_path):
        conn = get_connection(db_path)
        try:
            # 1 failure out of 4 → 25 %
            _insert_av(conn, _RUN_A, "narration", "elevenlabs", "failed")
            _insert_av(conn, _RUN_A, "narration", "elevenlabs", "succeeded")
            _insert_av(conn, _RUN_A, "narration", "elevenlabs", "succeeded")
            _insert_av(conn, _RUN_A, "narration", "elevenlabs", "succeeded")
            conn.commit()
        finally:
            conn.close()
        recs = get_provider_recommendations(db_path=db_path)
        assert recs[0].action == "monitor"

    def test_ok_rate_not_returned(self, db_path):
        conn = get_connection(db_path)
        try:
            for _ in range(5):
                _insert_av(conn, _RUN_A, "narration", "elevenlabs", "succeeded")
            conn.commit()
        finally:
            conn.close()
        assert get_provider_recommendations(db_path=db_path) == []

    def test_sorted_by_failure_rate_desc(self, db_path):
        conn = get_connection(db_path)
        try:
            # muapi: 3/3 failed = 100 %
            for _ in range(3):
                _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed")
            # runway: 1/3 failed ≈ 33 %
            _insert_av(conn, _RUN_A, "video_clip", "runway", "failed")
            _insert_av(conn, _RUN_A, "video_clip", "runway", "succeeded")
            _insert_av(conn, _RUN_A, "video_clip", "runway", "succeeded")
            conn.commit()
        finally:
            conn.close()
        recs = get_provider_recommendations(db_path=db_path)
        assert recs[0].current_provider == "muapi"
        assert recs[1].current_provider == "runway"

    def test_episode_filter(self, db_path):
        conn = get_connection(db_path)
        try:
            # failures in ep_other — should not appear when filtering for _EP
            for _ in range(3):
                _insert_av(conn, _RUN_B, "video_clip", "muapi", "failed",
                           episode_id="ep_other")
            # 1 success in _EP
            for _ in range(5):
                _insert_av(conn, _RUN_A, "video_clip", "muapi", "succeeded",
                           episode_id=_EP)
            conn.commit()
        finally:
            conn.close()
        recs = get_provider_recommendations(db_path=db_path, episode_id=_EP)
        assert recs == []

    def test_custom_min_failure_rate(self, db_path):
        conn = get_connection(db_path)
        try:
            # 1/3 failures = 33 %
            _insert_av(conn, _RUN_A, "narration", "elevenlabs", "failed")
            _insert_av(conn, _RUN_A, "narration", "elevenlabs", "succeeded")
            _insert_av(conn, _RUN_A, "narration", "elevenlabs", "succeeded")
            conn.commit()
        finally:
            conn.close()
        # With 50 % threshold, this should NOT be flagged
        recs = get_provider_recommendations(db_path=db_path, min_failure_rate=0.50)
        assert recs == []
        # With 10 % threshold, it should be flagged
        recs = get_provider_recommendations(db_path=db_path, min_failure_rate=0.10)
        assert len(recs) == 1

    def test_returns_empty_on_bad_db(self, tmp_path):
        assert get_provider_recommendations(db_path=tmp_path / "nope.db") == []


# ===========================================================================
# TestGetCostRecommendations
# ===========================================================================

class TestGetCostRecommendations:
    def test_empty_returns_empty(self, db_path):
        assert get_cost_recommendations(db_path=db_path) == []

    def test_no_ceiling_action_ok(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "succeeded", cost_usd=0.50)
            conn.commit()
        finally:
            conn.close()
        recs = get_cost_recommendations(db_path=db_path, budget_ceiling_usd=None)
        assert len(recs) == 1
        assert recs[0].action == "ok"

    def test_over_budget_raises_ceiling(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "succeeded", cost_usd=2.00)
            conn.commit()
        finally:
            conn.close()
        recs = get_cost_recommendations(db_path=db_path, budget_ceiling_usd=1.00)
        assert len(recs) == 1
        assert recs[0].action == "raise_ceiling"

    def test_under_60_pct_lowers_ceiling(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "succeeded", cost_usd=0.30)
            conn.commit()
        finally:
            conn.close()
        # 0.30 < 0.60 * 1.00 = 0.60 → lower_ceiling
        recs = get_cost_recommendations(db_path=db_path, budget_ceiling_usd=1.00)
        assert len(recs) == 1
        assert recs[0].action == "lower_ceiling"

    def test_between_60_and_100_pct_ok(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "succeeded", cost_usd=0.75)
            conn.commit()
        finally:
            conn.close()
        recs = get_cost_recommendations(db_path=db_path, budget_ceiling_usd=1.00)
        assert recs[0].action == "ok"

    def test_failed_assets_excluded_from_cost(self, db_path):
        conn = get_connection(db_path)
        try:
            # Only failed attempts — zero succeeded cost
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed", cost_usd=1.50)
            conn.commit()
        finally:
            conn.close()
        recs = get_cost_recommendations(db_path=db_path, budget_ceiling_usd=1.00)
        # No succeeded rows → no episode cost row
        assert recs == []

    def test_episode_filter(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "succeeded",
                       cost_usd=5.00, episode_id=_EP)
            _insert_av(conn, _RUN_B, "video_clip", "muapi", "succeeded",
                       cost_usd=0.10, episode_id=_EP2)
            conn.commit()
        finally:
            conn.close()
        recs = get_cost_recommendations(db_path=db_path,
                                        episode_id=_EP, budget_ceiling_usd=1.00)
        assert len(recs) == 1
        assert recs[0].scope_value == _EP

    def test_fields_populated(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "succeeded", cost_usd=2.00)
            conn.commit()
        finally:
            conn.close()
        r = get_cost_recommendations(db_path=db_path, budget_ceiling_usd=1.00)[0]
        assert isinstance(r, CostRecommendation)
        assert r.scope == "episode"
        assert r.scope_value == _EP
        assert r.observed_cost == pytest.approx(2.00)
        assert r.budget_ceiling == pytest.approx(1.00)

    def test_returns_empty_on_bad_db(self, tmp_path):
        assert get_cost_recommendations(db_path=tmp_path / "nope.db") == []


# ===========================================================================
# TestGetGateRecommendations
# ===========================================================================

class TestGetGateRecommendations:
    def test_empty_returns_empty(self, db_path):
        assert get_gate_recommendations(db_path=db_path) == []

    def test_below_sample_size_ignored(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_gate(conn, _RUN_A, "bga_p1", "BLOCKED")
            _insert_gate(conn, _RUN_A, "bga_p1", "BLOCKED")
            conn.commit()
        finally:
            conn.close()
        assert get_gate_recommendations(db_path=db_path) == []

    def test_flagged_above_threshold(self, db_path):
        conn = get_connection(db_path)
        try:
            for _ in range(3):
                _insert_gate(conn, _RUN_A, "qa", "BLOCKED")
            conn.commit()
        finally:
            conn.close()
        recs = get_gate_recommendations(db_path=db_path)
        assert len(recs) == 1
        assert recs[0].gate_name == "qa"
        assert recs[0].block_rate == pytest.approx(1.0)

    def test_action_investigate_above_50_pct(self, db_path):
        conn = get_connection(db_path)
        try:
            for _ in range(3):
                _insert_gate(conn, _RUN_A, "brand", "BLOCKED")
            conn.commit()
        finally:
            conn.close()
        recs = get_gate_recommendations(db_path=db_path)
        assert recs[0].action == "investigate"

    def test_action_relax_between_30_and_50_pct(self, db_path):
        conn = get_connection(db_path)
        try:
            # 2 blocked, 4 approved → 33 %
            _insert_gate(conn, _RUN_A, "brand", "BLOCKED")
            _insert_gate(conn, _RUN_A, "brand", "BLOCKED")
            for _ in range(4):
                _insert_gate(conn, _RUN_A, "brand", "APPROVED")
            conn.commit()
        finally:
            conn.close()
        recs = get_gate_recommendations(db_path=db_path)
        assert recs[0].action == "relax"

    def test_ok_rate_not_returned(self, db_path):
        conn = get_connection(db_path)
        try:
            for _ in range(5):
                _insert_gate(conn, _RUN_A, "tig", "APPROVED")
            conn.commit()
        finally:
            conn.close()
        assert get_gate_recommendations(db_path=db_path) == []

    def test_sorted_by_block_rate_desc(self, db_path):
        conn = get_connection(db_path)
        try:
            # bga: 100 % block
            for _ in range(3):
                _insert_gate(conn, _RUN_A, "bga", "BLOCKED")
            # qa: 50 % block
            _insert_gate(conn, _RUN_A, "qa", "BLOCKED")
            _insert_gate(conn, _RUN_A, "qa", "BLOCKED")
            _insert_gate(conn, _RUN_A, "qa", "APPROVED")
            _insert_gate(conn, _RUN_A, "qa", "APPROVED")
            conn.commit()
        finally:
            conn.close()
        recs = get_gate_recommendations(db_path=db_path)
        assert recs[0].gate_name == "bga"
        assert recs[1].gate_name == "qa"

    def test_rejected_also_counts_as_block(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_gate(conn, _RUN_A, "compliance", "REJECTED")
            _insert_gate(conn, _RUN_A, "compliance", "REJECTED")
            _insert_gate(conn, _RUN_A, "compliance", "REJECTED")
            conn.commit()
        finally:
            conn.close()
        recs = get_gate_recommendations(db_path=db_path)
        assert recs[0].block_rate == pytest.approx(1.0)

    def test_episode_filter(self, db_path):
        conn = get_connection(db_path)
        try:
            for _ in range(3):
                _insert_gate(conn, _RUN_B, "qa", "BLOCKED", episode_id="ep_other")
            for _ in range(5):
                _insert_gate(conn, _RUN_A, "qa", "APPROVED", episode_id=_EP)
            conn.commit()
        finally:
            conn.close()
        recs = get_gate_recommendations(db_path=db_path, episode_id=_EP)
        assert recs == []

    def test_custom_max_block_rate(self, db_path):
        conn = get_connection(db_path)
        try:
            # 1 blocked, 2 approved → 33 %
            _insert_gate(conn, _RUN_A, "g1", "BLOCKED")
            _insert_gate(conn, _RUN_A, "g1", "APPROVED")
            _insert_gate(conn, _RUN_A, "g1", "APPROVED")
            conn.commit()
        finally:
            conn.close()
        # Default 30 % → flagged
        assert len(get_gate_recommendations(db_path=db_path)) == 1
        # Raise to 50 % → not flagged
        assert get_gate_recommendations(db_path=db_path, max_block_rate=0.50) == []

    def test_returns_empty_on_bad_db(self, tmp_path):
        assert get_gate_recommendations(db_path=tmp_path / "nope.db") == []


# ===========================================================================
# TestAnalyzeEpisode
# ===========================================================================

class TestAnalyzeEpisode:
    def test_returns_report_type(self, db_path):
        r = analyze_episode(_EP, db_path=db_path)
        assert isinstance(r, EpisodeEvolutionReport)

    def test_episode_id_set(self, db_path):
        r = analyze_episode(_EP, db_path=db_path)
        assert r.episode_id == _EP

    def test_generated_at_present(self, db_path):
        r = analyze_episode(_EP, db_path=db_path)
        assert "T" in r.generated_at

    def test_signals_populated_from_learning_records(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_learning(conn, _RUN_A, _EP, "performance",
                             signal_value=0.80, recommendation="keep_topic")
            _insert_learning(conn, _RUN_A, _EP, "performance", signal_value=0.60)
            _insert_learning(conn, _RUN_A, _EP, "cost_efficiency", signal_value=0.50)
            conn.commit()
        finally:
            conn.close()
        r = analyze_episode(_EP, db_path=db_path)
        signal_types = {s.signal_type for s in r.signals}
        assert "performance" in signal_types
        assert "cost_efficiency" in signal_types

    def test_signal_avg_value(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_learning(conn, _RUN_A, _EP, "thumbnail_ctr", signal_value=0.10)
            _insert_learning(conn, _RUN_A, _EP, "thumbnail_ctr", signal_value=0.30)
            conn.commit()
        finally:
            conn.close()
        r = analyze_episode(_EP, db_path=db_path)
        sig = next(s for s in r.signals if s.signal_type == "thumbnail_ctr")
        assert sig.avg_value == pytest.approx(0.20)
        assert sig.sample_count == 2

    def test_empty_episode_gives_empty_signals(self, db_path):
        r = analyze_episode("ep_no_signals", db_path=db_path)
        assert r.signals == []

    def test_provider_recs_scoped_to_episode(self, db_path):
        conn = get_connection(db_path)
        try:
            for _ in range(3):
                _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed",
                           episode_id=_EP)
            conn.commit()
        finally:
            conn.close()
        r = analyze_episode(_EP, db_path=db_path)
        assert any(rec.asset_type == "video_clip" for rec in r.provider_recs)

    def test_bad_db_returns_partial_report(self, tmp_path):
        r = analyze_episode(_EP, db_path=tmp_path / "nope.db")
        assert isinstance(r, EpisodeEvolutionReport)
        assert r.episode_id == _EP
        assert r.signals == []


# ===========================================================================
# TestRecordAdjustment
# ===========================================================================

class TestRecordAdjustment:
    def test_returns_uuid_string(self, db_path):
        aid = record_adjustment(
            adjustment_type="provider_swap",
            asset_type="video_clip",
            current_value="muapi",
            recommended_value="runway",
            rationale="high failure rate",
            run_id=_RUN_A,
            episode_id=_EP,
            db_path=db_path,
        )
        assert aid is not None
        assert len(aid) == 36   # UUID4 string length

    def test_row_persisted(self, db_path):
        aid = record_adjustment(
            adjustment_type="gate_threshold",
            asset_type=None,
            current_value="bga_p1",
            recommended_value="relax",
            rationale="test",
            run_id=_RUN_A,
            db_path=db_path,
        )
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM evolution_adjustments WHERE adjustment_id=?", [aid]
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["adjustment_type"] == "gate_threshold"
        assert row["current_value"] == "bga_p1"
        assert row["applied"] == 0  # default not applied

    def test_bad_db_returns_none(self, tmp_path):
        # Point at a directory — SQLite cannot open a directory as a DB
        bad_path = tmp_path / "not_a_db_dir"
        bad_path.mkdir()
        aid = record_adjustment(
            adjustment_type="provider_swap",
            asset_type="video_clip",
            current_value="muapi",
            recommended_value="runway",
            rationale="test",
            run_id="run-x",
            db_path=bad_path,
        )
        assert aid is None

    def test_multiple_records_independent(self, db_path):
        aid1 = record_adjustment("provider_swap", "video_clip", "muapi", "runway",
                                 "r1", _RUN_A, db_path=db_path)
        aid2 = record_adjustment("cost_ceiling", None, "1.00", "2.00",
                                 "r2", _RUN_A, db_path=db_path)
        assert aid1 != aid2

    def test_none_asset_type_allowed(self, db_path):
        aid = record_adjustment("cost_ceiling", None, "1.00", "2.00",
                                "rationale", _RUN_A, db_path=db_path)
        assert aid is not None


# ===========================================================================
# TestEvolve
# ===========================================================================

class TestEvolve:
    def test_returns_evolution_report(self, db_path):
        r = evolve(db_path=db_path)
        assert isinstance(r, EvolutionReport)

    def test_generated_at_present(self, db_path):
        r = evolve(db_path=db_path)
        assert "T" in r.generated_at

    def test_episode_count_nonzero(self, db_path):
        r = evolve(db_path=db_path)
        # 2 distinct episodes (_EP and _EP2) were inserted in fixture
        assert r.episode_count >= 2

    def test_empty_recs_when_no_issues(self, db_path):
        r = evolve(db_path=db_path)
        assert r.provider_recs == []
        assert r.gate_recs == []

    def test_provider_recs_present(self, db_path):
        conn = get_connection(db_path)
        try:
            for _ in range(3):
                _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed")
            conn.commit()
        finally:
            conn.close()
        r = evolve(db_path=db_path)
        assert len(r.provider_recs) > 0

    def test_gate_recs_present(self, db_path):
        conn = get_connection(db_path)
        try:
            for _ in range(3):
                _insert_gate(conn, _RUN_A, "bga_p1", "BLOCKED")
            conn.commit()
        finally:
            conn.close()
        r = evolve(db_path=db_path)
        assert len(r.gate_recs) > 0

    def test_cost_recs_over_budget(self, db_path):
        conn = get_connection(db_path)
        try:
            _insert_av(conn, _RUN_A, "video_clip", "muapi", "succeeded", cost_usd=3.00)
            conn.commit()
        finally:
            conn.close()
        r = evolve(db_path=db_path, budget_ceiling_usd=1.00)
        assert any(c.action == "raise_ceiling" for c in r.cost_recs)

    def test_adjustments_recorded_when_record_true(self, db_path):
        conn = get_connection(db_path)
        try:
            for _ in range(3):
                _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed")
            conn.commit()
        finally:
            conn.close()
        r = evolve(db_path=db_path, run_id=_RUN_A, record=True)
        assert r.adjustments_recorded > 0

    def test_no_adjustments_when_record_false(self, db_path):
        conn = get_connection(db_path)
        try:
            for _ in range(3):
                _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed")
            conn.commit()
        finally:
            conn.close()
        r = evolve(db_path=db_path, run_id=_RUN_A, record=False)
        assert r.adjustments_recorded == 0

    def test_no_adjustments_without_run_id(self, db_path):
        conn = get_connection(db_path)
        try:
            for _ in range(3):
                _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed")
            conn.commit()
        finally:
            conn.close()
        r = evolve(db_path=db_path, record=True)  # no run_id → no writes
        assert r.adjustments_recorded == 0

    def test_run_id_set_in_report(self, db_path):
        r = evolve(db_path=db_path, run_id=_RUN_A)
        assert r.run_id == _RUN_A

    def test_bad_db_does_not_raise(self, tmp_path):
        r = evolve(db_path=tmp_path / "nope.db")
        assert isinstance(r, EvolutionReport)


# ===========================================================================
# TestEmptyDB
# ===========================================================================

class TestEmptyDB:
    @pytest.fixture()
    def empty_db(self, tmp_path):
        p = tmp_path / "empty.db"
        initialize_schema(p)
        return p

    def test_provider_recs_empty(self, empty_db):
        assert get_provider_recommendations(db_path=empty_db) == []

    def test_cost_recs_empty(self, empty_db):
        assert get_cost_recommendations(db_path=empty_db) == []

    def test_gate_recs_empty(self, empty_db):
        assert get_gate_recommendations(db_path=empty_db) == []

    def test_analyze_episode_empty(self, empty_db):
        r = analyze_episode("ep_x", db_path=empty_db)
        assert r.signals == []
        assert r.provider_recs == []

    def test_evolve_empty(self, empty_db):
        r = evolve(db_path=empty_db)
        assert r.provider_recs == []
        assert r.gate_recs == []
        assert r.cost_recs == []


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:
    def _block_socket(self, monkeypatch):
        def no_socket(*args, **kwargs):
            raise AssertionError("evolution_engine opened a socket")
        monkeypatch.setattr(socket, "socket", no_socket)

    def test_provider_recs_no_network(self, db_path, monkeypatch):
        self._block_socket(monkeypatch)
        get_provider_recommendations(db_path=db_path)

    def test_cost_recs_no_network(self, db_path, monkeypatch):
        self._block_socket(monkeypatch)
        get_cost_recommendations(db_path=db_path)

    def test_gate_recs_no_network(self, db_path, monkeypatch):
        self._block_socket(monkeypatch)
        get_gate_recommendations(db_path=db_path)

    def test_analyze_episode_no_network(self, db_path, monkeypatch):
        self._block_socket(monkeypatch)
        analyze_episode(_EP, db_path=db_path)

    def test_evolve_no_network(self, db_path, monkeypatch):
        self._block_socket(monkeypatch)
        evolve(db_path=db_path)


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:
    def test_no_ep_000_created(self, db_path):
        evolve(db_path=db_path, run_id=_RUN_A, record=True)
        conn = get_connection(db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE episode_id='ep_000'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 0

    def test_asset_versions_not_written(self, db_path):
        conn = get_connection(db_path)
        try:
            before = conn.execute(
                "SELECT COUNT(*) FROM asset_versions"
            ).fetchone()[0]
        finally:
            conn.close()

        evolve(db_path=db_path, run_id=_RUN_A, record=True)

        conn = get_connection(db_path)
        try:
            after = conn.execute(
                "SELECT COUNT(*) FROM asset_versions"
            ).fetchone()[0]
        finally:
            conn.close()
        assert before == after

    def test_gate_verdicts_not_written(self, db_path):
        conn = get_connection(db_path)
        try:
            before = conn.execute(
                "SELECT COUNT(*) FROM gate_verdicts"
            ).fetchone()[0]
        finally:
            conn.close()

        evolve(db_path=db_path, run_id=_RUN_A, record=True)

        conn = get_connection(db_path)
        try:
            after = conn.execute(
                "SELECT COUNT(*) FROM gate_verdicts"
            ).fetchone()[0]
        finally:
            conn.close()
        assert before == after

    def test_record_false_writes_nothing(self, db_path):
        conn = get_connection(db_path)
        try:
            for _ in range(3):
                _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed")
            conn.commit()
        finally:
            conn.close()

        evolve(db_path=db_path, run_id=_RUN_A, record=False)

        conn = get_connection(db_path)
        try:
            # evolution_adjustments table may not exist at all if never written to
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
        finally:
            conn.close()
        # If table exists, it must be empty
        if "evolution_adjustments" in tables:
            conn = get_connection(db_path)
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM evolution_adjustments"
                ).fetchone()[0]
            finally:
                conn.close()
            assert n == 0
