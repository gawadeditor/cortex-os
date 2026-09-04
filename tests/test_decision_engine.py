"""
tests/test_decision_engine.py — F3: Real Decision Engine v0 tests

Coverage:
    TestEngineStatus         — not_ready/partial/ready from real inputs
    TestLaunchReadiness      — blocked vs ready vs queued topic
    TestFailureReview        — 2+ failures threshold
    TestCostReview           — threshold logic, insufficient when no data
    TestPublishingGap        — completed episodes with no publishing outcome
    TestAudienceReview       — mock-only → insufficient; real snapshots → decision
    TestDiscoveryReview      — empty queue, active queue, all-consumed
    TestDecisionCardShape    — every card has all required fields
    TestInsufficientData     — shape of insufficient_data entries
    TestGetDecisionState     — full assembly, empty DB safe, ordering
    TestNoExternalAPICalls   — socket blocked throughout
    TestNoWrites             — reads never write
"""

from __future__ import annotations

import socket
import uuid
from pathlib import Path

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.decision_engine import (
    get_decision_state,
    _rule_launch_readiness,
    _rule_failure_review,
    _rule_cost_review,
    _rule_publishing_gap,
    _rule_audience_review,
    _rule_discovery_review,
    COST_THRESHOLD_USD_PER_EPISODE,
    MIN_FAILURES_FOR_REVIEW,
    MIN_AUDIENCE_SNAPSHOTS,
)
from hypogeum.decision_inputs.store import record_publishing_outcome


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path) -> Path:
    p = tmp_path / "de_test.db"
    initialize_schema(p)
    return p


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    _orig = socket.socket
    def _no(*a, **kw):
        raise RuntimeError("Network forbidden in decision_engine tests")
    monkeypatch.setattr(socket, "socket", _no)
    yield
    monkeypatch.setattr(socket, "socket", _orig)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_cmd(can_launch=True, blockers=None, failures=None, next_topic=None):
    return {
        "episode_readiness": {
            "can_launch":           can_launch,
            "blockers":             blockers or [],
            "next_topic":           next_topic or {},
            "pending_approval_count": 0,
        },
        "run_state": {
            "recent_failures": failures or [],
            "active_runs": [], "queued_runs": [],
            "last_completed": None, "blockers": [],
        },
    }


def _make_di(
    discovery_queued=0, discovery_total=0, discovery_consumed=0,
    completed_episodes=0,
    costs_has=False, costs_avg=None, costs_by_provider=None,
    pub_total=0,
    audience_real=0, audience_mock=0,
    has_real=False, ready=False,
):
    return {
        "discovery": {
            "has_data":              discovery_total > 0,
            "total_topics":          discovery_total,
            "channels_with_topics":  1 if discovery_total > 0 else 0,
            "by_status": {
                "queued":    discovery_queued,
                "consumed":  discovery_consumed,
                "claimed":   0, "skipped": 0, "cancelled": 0,
            },
            "recent_consumed": [],
        },
        "episodes": {
            "has_data":           completed_episodes > 0,
            "total_episodes":     completed_episodes,
            "completed_episodes": completed_episodes,
            "failed_episodes":    0,
            "by_status":          {},
            "recent_completed":   [],
            "avg_run_duration_s": None,
            "total_produced_assets": 0,
        },
        "costs": {
            "has_data":              costs_has,
            "total_episodes_costed": 1 if costs_has else 0,
            "total_cost_usd":        (costs_avg or 0.0) if costs_has else 0.0,
            "avg_cost_per_episode":  costs_avg,
            "by_provider":           costs_by_provider or {},
            "by_model":              {},
            "recent_episodes":       [],
            "total_tokens":          0.0,
        },
        "publishing": {
            "has_data":       pub_total > 0,
            "total_outcomes": pub_total,
            "successful":     pub_total,
            "by_status":      {},
            "by_platform":    {},
            "recent_outcomes": [],
        },
        "audience": {
            "has_real_data":  audience_real > 0,
            "total_snapshots": audience_real + audience_mock,
            "real_snapshots": audience_real,
            "mock_snapshots": audience_mock,
            "by_platform":    {},
            "recent_real":    [],
            "mock_warning":   (
                "All snapshots are mock/demo data." if audience_real == 0 and audience_mock > 0
                else None
            ),
        },
        "readiness": {
            "has_real_inputs":           has_real,
            "ready_for_decision_engine": ready,
            "missing_inputs":            [],
        },
    }


# ---------------------------------------------------------------------------
# TestEngineStatus
# ---------------------------------------------------------------------------

class TestEngineStatus:
    def test_not_ready_on_empty_db(self, db):
        s = get_decision_state(db_path=db)
        assert s["engine_status"] == "not_ready"

    def test_partial_when_some_inputs(self):
        di = _make_di(discovery_total=1, discovery_queued=1, has_real=True, ready=False)
        from hypogeum.decision_engine import _compute_engine_status
        assert _compute_engine_status(di) == "partial"

    def test_ready_when_all_minimum_inputs(self):
        di = _make_di(
            discovery_total=1, discovery_consumed=1,
            completed_episodes=1,
            costs_has=True, costs_avg=0.5,
            has_real=True, ready=True,
        )
        from hypogeum.decision_engine import _compute_engine_status
        assert _compute_engine_status(di) == "ready"

    def test_summary_not_empty(self, db):
        s = get_decision_state(db_path=db)
        assert isinstance(s["summary"], str)
        assert len(s["summary"]) > 0


# ---------------------------------------------------------------------------
# TestLaunchReadiness
# ---------------------------------------------------------------------------

class TestLaunchReadiness:
    def test_blocked_produces_critical_decision(self):
        cmd = _make_cmd(can_launch=False, blockers=["acceptance audit failed"])
        di  = _make_di()
        decs, insuff = _rule_launch_readiness(cmd, di)
        assert len(decs) >= 1
        assert decs[0]["priority"] == 1
        assert decs[0]["type"] == "launch_readiness"
        assert "acceptance audit failed" in decs[0]["evidence"]

    def test_can_launch_with_queue_produces_ready_decision(self):
        cmd = _make_cmd(can_launch=True)
        di  = _make_di(discovery_queued=2, discovery_total=2)
        decs, insuff = _rule_launch_readiness(cmd, di)
        assert len(decs) == 1
        assert decs[0]["type"] == "launch_readiness"
        assert decs[0]["priority"] == 1
        assert decs[0]["confidence"] == 1.0

    def test_can_launch_no_queue_produces_no_launch_readiness_decision(self):
        cmd = _make_cmd(can_launch=True)
        di  = _make_di(discovery_queued=0, discovery_total=0)
        decs, _ = _rule_launch_readiness(cmd, di)
        # No queued topic → no launch_readiness decision (discovery_review handles empty queue)
        assert len(decs) == 0

    def test_blocked_no_explicit_reason_still_produces_decision(self):
        cmd = _make_cmd(can_launch=False, blockers=[])
        di  = _make_di()
        decs, _ = _rule_launch_readiness(cmd, di)
        assert len(decs) == 1
        assert decs[0]["priority"] == 1

    def test_blocked_decision_links_to_preflight(self):
        cmd = _make_cmd(can_launch=False, blockers=["audit failed"])
        di  = _make_di()
        decs, _ = _rule_launch_readiness(cmd, di)
        assert decs[0]["investigate_url"] == "/preflight"

    def test_ready_decision_links_to_queue(self):
        cmd = _make_cmd(can_launch=True)
        di  = _make_di(discovery_queued=1, discovery_total=1)
        decs, _ = _rule_launch_readiness(cmd, di)
        assert decs[0]["investigate_url"] == "/queue"


# ---------------------------------------------------------------------------
# TestFailureReview
# ---------------------------------------------------------------------------

class TestFailureReview:
    def _failures(self, n):
        return [{"run_id": f"r{i}", "episode_id": f"ep_{i:02d}",
                 "error_message": f"error {i}"} for i in range(n)]

    def test_below_threshold_no_decision(self):
        cmd = _make_cmd(failures=self._failures(MIN_FAILURES_FOR_REVIEW - 1))
        decs, _ = _rule_failure_review(cmd)
        assert decs == []

    def test_at_threshold_produces_decision(self):
        cmd = _make_cmd(failures=self._failures(MIN_FAILURES_FOR_REVIEW))
        decs, _ = _rule_failure_review(cmd)
        assert len(decs) == 1
        assert decs[0]["type"] == "failure_review"

    def test_above_threshold_produces_decision(self):
        cmd = _make_cmd(failures=self._failures(5))
        decs, _ = _rule_failure_review(cmd)
        assert len(decs) == 1
        assert decs[0]["priority"] == 2

    def test_decision_links_to_failures(self):
        cmd = _make_cmd(failures=self._failures(3))
        decs, _ = _rule_failure_review(cmd)
        assert decs[0]["investigate_url"] == "/failures"

    def test_confidence_is_1_for_failure_review(self):
        cmd = _make_cmd(failures=self._failures(2))
        decs, _ = _rule_failure_review(cmd)
        assert decs[0]["confidence"] == 1.0


# ---------------------------------------------------------------------------
# TestCostReview
# ---------------------------------------------------------------------------

class TestCostReview:
    def test_no_cost_data_returns_insufficient(self):
        di = _make_di(costs_has=False)
        decs, insuff = _rule_cost_review(di)
        assert decs == []
        assert len(insuff) == 1
        assert insuff[0]["type"] == "cost_review"

    def test_below_threshold_no_decision(self):
        di = _make_di(costs_has=True, costs_avg=COST_THRESHOLD_USD_PER_EPISODE - 0.01)
        decs, insuff = _rule_cost_review(di)
        assert decs == []
        assert insuff == []

    def test_above_threshold_produces_decision(self):
        di = _make_di(costs_has=True, costs_avg=COST_THRESHOLD_USD_PER_EPISODE + 0.50,
                      costs_by_provider={"openrouter": 1.20})
        decs, _ = _rule_cost_review(di)
        assert len(decs) == 1
        assert decs[0]["type"] == "cost_review"
        assert decs[0]["priority"] == 3

    def test_cost_evidence_contains_threshold(self):
        avg = COST_THRESHOLD_USD_PER_EPISODE + 0.10
        di = _make_di(costs_has=True, costs_avg=avg)
        decs, _ = _rule_cost_review(di)
        evidence_str = " ".join(decs[0]["evidence"])
        assert "threshold" in evidence_str.lower()

    def test_confidence_is_0_8(self):
        di = _make_di(costs_has=True, costs_avg=COST_THRESHOLD_USD_PER_EPISODE + 2.0)
        decs, _ = _rule_cost_review(di)
        assert decs[0]["confidence"] == 0.8

    def test_none_avg_returns_insufficient(self):
        di = _make_di(costs_has=True, costs_avg=None)
        decs, insuff = _rule_cost_review(di)
        assert decs == []
        assert len(insuff) == 1


# ---------------------------------------------------------------------------
# TestPublishingGap
# ---------------------------------------------------------------------------

class TestPublishingGap:
    def test_no_completed_no_decision(self):
        di = _make_di(completed_episodes=0, pub_total=0)
        decs, _ = _rule_publishing_gap(di)
        assert decs == []

    def test_completed_with_publishing_no_decision(self):
        di = _make_di(completed_episodes=2, pub_total=1)
        decs, _ = _rule_publishing_gap(di)
        assert decs == []

    def test_completed_no_publishing_produces_decision(self):
        di = _make_di(completed_episodes=1, pub_total=0)
        decs, _ = _rule_publishing_gap(di)
        assert len(decs) == 1
        assert decs[0]["type"] == "publishing_gap"
        assert decs[0]["priority"] == 3

    def test_decision_confidence_is_0_9(self):
        di = _make_di(completed_episodes=1, pub_total=0)
        decs, _ = _rule_publishing_gap(di)
        assert decs[0]["confidence"] == 0.9

    def test_evidence_mentions_completed_count(self):
        di = _make_di(completed_episodes=3, pub_total=0)
        decs, _ = _rule_publishing_gap(di)
        evidence_str = " ".join(decs[0]["evidence"])
        assert "3" in evidence_str


# ---------------------------------------------------------------------------
# TestAudienceReview
# ---------------------------------------------------------------------------

class TestAudienceReview:
    def test_no_data_returns_insufficient(self):
        di = _make_di(audience_real=0, audience_mock=0)
        decs, insuff = _rule_audience_review(di)
        assert decs == []
        assert len(insuff) == 1

    def test_mock_only_returns_insufficient(self):
        di = _make_di(audience_real=0, audience_mock=3)
        decs, insuff = _rule_audience_review(di)
        assert decs == []
        assert len(insuff) == 1
        assert "mock" in insuff[0]["reason"].lower()

    def test_mock_data_does_not_create_audience_recommendation(self):
        di = _make_di(audience_real=0, audience_mock=10)
        decs, insuff = _rule_audience_review(di)
        assert len(decs) == 0
        assert len(insuff) == 1

    def test_fewer_than_min_real_snapshots_low_confidence(self):
        di = _make_di(audience_real=MIN_AUDIENCE_SNAPSHOTS - 1, audience_mock=0)
        decs, _ = _rule_audience_review(di)
        assert len(decs) == 1
        assert decs[0]["confidence"] == 0.3

    def test_exactly_min_real_snapshots_higher_confidence(self):
        di = _make_di(audience_real=MIN_AUDIENCE_SNAPSHOTS, audience_mock=0)
        decs, _ = _rule_audience_review(di)
        assert len(decs) == 1
        assert decs[0]["confidence"] == 0.7

    def test_3_plus_real_snapshots_produces_review_decision(self):
        di = _make_di(audience_real=5, audience_mock=0)
        decs, insuff = _rule_audience_review(di)
        assert len(decs) == 1
        assert insuff == []
        assert decs[0]["type"] == "audience_review"

    def test_no_content_direction_recommendation_in_evidence(self):
        di = _make_di(audience_real=5, audience_mock=0)
        decs, _ = _rule_audience_review(di)
        full_text = " ".join(decs[0]["evidence"] + [decs[0]["recommendation"]])
        for phrase in ("produce more", "avoid", "increase topic",
                       "best performing topic"):
            assert phrase.lower() not in full_text.lower()

    def test_mixed_real_and_mock_uses_real_count(self):
        di = _make_di(audience_real=3, audience_mock=10)
        decs, insuff = _rule_audience_review(di)
        # Real count is 3 which meets threshold — should get decision, not insufficient
        assert len(decs) == 1
        assert insuff == []


# ---------------------------------------------------------------------------
# TestDiscoveryReview
# ---------------------------------------------------------------------------

class TestDiscoveryReview:
    def test_no_topics_produces_add_topics_decision(self):
        cmd = _make_cmd()
        di  = _make_di(discovery_total=0, discovery_queued=0)
        decs, _ = _rule_discovery_review(cmd, di)
        assert len(decs) == 1
        assert "No topics" in decs[0]["title"] or "empty" in decs[0]["title"].lower()

    def test_queued_topics_produces_queue_ready_decision(self):
        cmd = _make_cmd()
        di  = _make_di(discovery_total=3, discovery_queued=2)
        decs, _ = _rule_discovery_review(cmd, di)
        assert len(decs) == 1
        assert "2" in decs[0]["title"] or "queued" in decs[0]["title"].lower()
        assert decs[0]["investigate_url"] == "/queue"

    def test_all_consumed_produces_add_more_decision(self):
        cmd = _make_cmd()
        di  = _make_di(discovery_total=3, discovery_queued=0, discovery_consumed=3)
        decs, _ = _rule_discovery_review(cmd, di)
        assert len(decs) == 1
        assert "consumed" in decs[0]["title"].lower() or "empty" in decs[0]["title"].lower()


# ---------------------------------------------------------------------------
# TestDecisionCardShape
# ---------------------------------------------------------------------------

class TestDecisionCardShape:
    _REQUIRED_KEYS = (
        "decision_id", "type", "priority", "title",
        "recommendation", "evidence", "expected_impact",
        "confidence", "status", "action_label",
        "reject_label", "investigate_url",
    )

    def _all_decisions(self, db):
        s = get_decision_state(db_path=db)
        return s["decisions"]

    def test_all_cards_have_required_keys(self, db):
        conn = get_connection(db)
        # Seed some data to get decisions
        conn.execute("""
            INSERT INTO episode_topics
              (topic_id, channel_id, topic_text, status, priority, created_at, updated_at)
            VALUES (?, 'ch1', 'test topic', 'queued', 0,
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """, (str(uuid.uuid4()),))
        conn.commit(); conn.close()

        for d in self._all_decisions(db):
            for key in self._REQUIRED_KEYS:
                assert key in d, f"decision missing key: {key}"

    def test_status_is_proposed(self, db):
        conn = get_connection(db)
        conn.execute("""
            INSERT INTO episode_topics
              (topic_id, channel_id, topic_text, status, priority, created_at, updated_at)
            VALUES (?, 'ch1', 'test', 'queued', 0,
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """, (str(uuid.uuid4()),))
        conn.commit(); conn.close()
        for d in self._all_decisions(db):
            assert d["status"] == "proposed"

    def test_confidence_between_0_and_1(self, db):
        for d in self._all_decisions(db):
            assert 0.0 <= d["confidence"] <= 1.0

    def test_priority_is_int_1_to_4(self, db):
        for d in self._all_decisions(db):
            assert d["priority"] in (1, 2, 3, 4)

    def test_reject_label_is_reject(self, db):
        for d in self._all_decisions(db):
            assert d["reject_label"] == "Reject"


# ---------------------------------------------------------------------------
# TestInsufficientData
# ---------------------------------------------------------------------------

class TestInsufficientData:
    def test_all_sections_insufficient_on_empty_db(self, db):
        s = get_decision_state(db_path=db)
        types = {i["type"] for i in s["insufficient_data"]}
        # cost_review and audience_review always produce insufficient on empty DB
        assert "cost_review"    in types
        assert "audience_review" in types

    def test_insufficient_entry_has_type_and_reason(self, db):
        s = get_decision_state(db_path=db)
        for i in s["insufficient_data"]:
            assert "type"   in i
            assert "reason" in i
            assert len(i["reason"]) > 0


# ---------------------------------------------------------------------------
# TestGetDecisionState
# ---------------------------------------------------------------------------

class TestGetDecisionState:
    def test_returns_dict_with_all_top_level_keys(self, db):
        s = get_decision_state(db_path=db)
        for key in ("checked_at", "engine_status", "summary",
                    "decisions", "insufficient_data"):
            assert key in s

    def test_decisions_sorted_by_priority(self, db):
        conn = get_connection(db)
        # Seed failures + queued topic to get multiple decision types
        conn.execute("""
            INSERT INTO episode_topics
              (topic_id, channel_id, topic_text, status, priority, created_at, updated_at)
            VALUES (?, 'ch1', 'topic', 'queued', 0,
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """, (str(uuid.uuid4()),))
        for i in range(MIN_FAILURES_FOR_REVIEW):
            conn.execute("""
                INSERT INTO runs (run_id, episode_id, started_at, status)
                VALUES (?, ?, '2026-01-01T10:00:00Z', 'failed')
            """, (f"run_f{i}", f"ep_f{i:02d}"))
        conn.commit(); conn.close()

        s = get_decision_state(db_path=db)
        priorities = [d["priority"] for d in s["decisions"]]
        assert priorities == sorted(priorities)

    def test_safe_on_missing_db(self, tmp_path):
        s = get_decision_state(db_path=tmp_path / "missing.db")
        assert isinstance(s, dict)
        assert s["engine_status"] in ("not_ready", "partial", "ready")

    def test_decisions_is_list(self, db):
        s = get_decision_state(db_path=db)
        assert isinstance(s["decisions"], list)

    def test_insufficient_data_is_list(self, db):
        s = get_decision_state(db_path=db)
        assert isinstance(s["insufficient_data"], list)

    def test_checked_at_is_iso_string(self, db):
        s = get_decision_state(db_path=db)
        assert "T" in s["checked_at"]

    def test_command_blocker_produces_priority_1(self, db):
        conn = get_connection(db)
        import json
        conn.execute("""
            INSERT INTO signal_records
              (signal_id, episode_id, run_id, ingested_at, data_quality,
               signal_type, severity, blocking, lifecycle_status, full_record, source_system)
            VALUES (?, 'ep_000', 'run-x', '2026-06-01T10:00:00Z', 'no_data',
                    'credential_failure', 'CRITICAL', 1, 'open', ?, 'startup')
        """, (str(uuid.uuid4()), json.dumps({"message": "OpenRouter unavailable"})))
        conn.commit(); conn.close()

        s = get_decision_state(db_path=db)
        priority_1 = [d for d in s["decisions"] if d["priority"] == 1]
        assert len(priority_1) >= 1


# ---------------------------------------------------------------------------
# TestNoExternalAPICalls
# ---------------------------------------------------------------------------

class TestNoExternalAPICalls:
    def test_full_state_no_network(self, db):
        s = get_decision_state(db_path=db)
        assert s is not None


# ---------------------------------------------------------------------------
# TestNoWrites
# ---------------------------------------------------------------------------

class TestNoWrites:
    def test_does_not_write_to_episodes(self, db):
        conn = get_connection(db)
        before = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()
        get_decision_state(db_path=db)
        conn = get_connection(db)
        after = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()
        assert before == after

    def test_does_not_write_to_runs(self, db):
        conn = get_connection(db)
        before = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        conn.close()
        get_decision_state(db_path=db)
        conn = get_connection(db)
        after = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        conn.close()
        assert before == after

    def test_does_not_write_to_learning_actions(self, db):
        conn = get_connection(db)
        before = conn.execute("SELECT COUNT(*) AS c FROM learning_actions").fetchone()["c"]
        conn.close()
        get_decision_state(db_path=db)
        conn = get_connection(db)
        after = conn.execute("SELECT COUNT(*) AS c FROM learning_actions").fetchone()["c"]
        conn.close()
        assert before == after
