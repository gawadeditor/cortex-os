"""
tests/test_decision_inputs.py — F2: Real Decision Inputs Layer tests

Coverage:
    TestDiscoveryFacts       — reads from episode_topics; empty / populated
    TestEpisodeFacts         — reads from episodes + runs; empty / completed / failed
    TestCostFacts            — reads from costs; empty / populated; aggregation
    TestPublishingFacts      — reads from publishing_outcomes; empty / populated
    TestAudienceFacts        — real vs mock separation; has_real_data logic
    TestPublishingStore      — record_publishing_outcome() write / idempotent upsert
    TestDecisionInputsState  — full assembly; readiness logic; empty DB safe
    TestNoExternalAPICalls   — socket blocked; no network during any read
    TestNoWrites             — reads never write to existing tables
"""

from __future__ import annotations

import json
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.decision_inputs.discovery  import get_discovery_facts
from hypogeum.decision_inputs.episodes   import get_episode_facts
from hypogeum.decision_inputs.costs      import get_cost_facts
from hypogeum.decision_inputs.publishing import get_publishing_facts
from hypogeum.decision_inputs.audience   import get_audience_facts
from hypogeum.decision_inputs.store      import record_publishing_outcome
from hypogeum.decision_inputs.state      import get_decision_inputs_state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path) -> Path:
    p = tmp_path / "di_test.db"
    initialize_schema(p)
    return p


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    _orig = socket.socket
    def _no(*a, **kw):
        raise RuntimeError("Network forbidden in decision_inputs tests")
    monkeypatch.setattr(socket, "socket", _no)
    yield
    monkeypatch.setattr(socket, "socket", _orig)


# ---------------------------------------------------------------------------
# TestDiscoveryFacts
# ---------------------------------------------------------------------------

class TestDiscoveryFacts:
    def test_empty_db_returns_safe_state(self, db):
        d = get_discovery_facts(db_path=db)
        assert isinstance(d, dict)
        assert d["total_topics"] == 0
        assert d["has_data"] is False

    def test_by_status_always_present(self, db):
        d = get_discovery_facts(db_path=db)
        for status in ("queued", "claimed", "consumed", "skipped", "cancelled"):
            assert status in d["by_status"]

    def test_counts_queued_topics(self, db):
        conn = get_connection(db)
        for i in range(3):
            conn.execute("""
                INSERT INTO episode_topics
                  (topic_id, channel_id, topic_text, status, priority, created_at, updated_at)
                VALUES (?, 'ch1', ?, 'queued', 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """, (str(uuid.uuid4()), f"Topic {i}"))
        conn.commit(); conn.close()
        d = get_discovery_facts(db_path=db)
        assert d["total_topics"] == 3
        assert d["by_status"]["queued"] == 3
        assert d["has_data"] is True

    def test_counts_consumed_topics(self, db):
        conn = get_connection(db)
        conn.execute("""
            INSERT INTO episode_topics
              (topic_id, channel_id, topic_text, status, priority, created_at, updated_at, consumed_at)
            VALUES (?, 'ch1', 'Consumed topic', 'consumed', 0,
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')
        """, (str(uuid.uuid4()),))
        conn.commit(); conn.close()
        d = get_discovery_facts(db_path=db)
        assert d["by_status"]["consumed"] == 1
        assert len(d["recent_consumed"]) == 1

    def test_channels_with_topics(self, db):
        conn = get_connection(db)
        for ch in ("ch_a", "ch_b"):
            conn.execute("""
                INSERT INTO episode_topics
                  (topic_id, channel_id, topic_text, status, priority, created_at, updated_at)
                VALUES (?, ?, 'topic', 'queued', 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """, (str(uuid.uuid4()), ch))
        conn.commit(); conn.close()
        d = get_discovery_facts(db_path=db)
        assert d["channels_with_topics"] == 2

    def test_safe_on_missing_db(self, tmp_path):
        d = get_discovery_facts(db_path=tmp_path / "missing.db")
        assert d["total_topics"] == 0


# ---------------------------------------------------------------------------
# TestEpisodeFacts
# ---------------------------------------------------------------------------

class TestEpisodeFacts:
    def _insert_run(self, conn, run_id, ep_id, status,
                    started="2026-01-01T10:00:00Z", completed=None):
        conn.execute("""
            INSERT INTO runs (run_id, episode_id, started_at, completed_at, status)
            VALUES (?, ?, ?, ?, ?)
        """, (run_id, ep_id, started, completed, status))
        conn.execute("""
            INSERT INTO episodes (episode_id, created_at, status, run_id)
            VALUES (?, ?, ?, ?)
        """, (ep_id, started, status, run_id))

    def test_empty_db_safe(self, db):
        e = get_episode_facts(db_path=db)
        assert e["total_episodes"] == 0
        assert e["has_data"] is False

    def test_completed_episode_counted(self, db):
        conn = get_connection(db)
        self._insert_run(conn, "r1", "ep_1", "completed",
                         completed="2026-01-01T11:00:00Z")
        conn.commit(); conn.close()
        e = get_episode_facts(db_path=db)
        assert e["completed_episodes"] == 1
        assert e["has_data"] is True

    def test_failed_episode_counted(self, db):
        conn = get_connection(db)
        self._insert_run(conn, "r2", "ep_2", "failed")
        conn.commit(); conn.close()
        e = get_episode_facts(db_path=db)
        assert e["failed_episodes"] == 1

    def test_recent_completed_in_result(self, db):
        conn = get_connection(db)
        self._insert_run(conn, "r3", "ep_3", "completed",
                         completed="2026-01-01T12:00:00Z")
        conn.commit(); conn.close()
        e = get_episode_facts(db_path=db)
        assert len(e["recent_completed"]) == 1

    def test_safe_on_missing_db(self, tmp_path):
        e = get_episode_facts(db_path=tmp_path / "missing.db")
        assert e["total_episodes"] == 0


# ---------------------------------------------------------------------------
# TestCostFacts
# ---------------------------------------------------------------------------

class TestCostFacts:
    def _insert_cost(self, conn, run_id, ep_id, provider, cost,
                     model=None, tokens=None):
        conn.execute("""
            INSERT INTO runs (run_id, episode_id, started_at, status)
            VALUES (?, ?, '2026-01-01T10:00:00Z', 'running')
        """, (run_id, ep_id))
        conn.execute("""
            INSERT INTO costs (cost_id, run_id, episode_id, provider, model,
                               call_type, cost_usd, tokens_or_credits, called_at)
            VALUES (?, ?, ?, ?, ?, 'llm', ?, ?, '2026-01-01T10:01:00Z')
        """, (str(uuid.uuid4()), run_id, ep_id, provider, model, cost, tokens))

    def test_empty_db_safe(self, db):
        c = get_cost_facts(db_path=db)
        assert c["total_cost_usd"] == 0.0
        assert c["has_data"] is False

    def test_total_cost_aggregated(self, db):
        conn = get_connection(db)
        self._insert_cost(conn, "r1", "ep_1", "openrouter", 0.05, "claude")
        self._insert_cost(conn, "r2", "ep_2", "muapi", 0.10)
        conn.commit(); conn.close()
        c = get_cost_facts(db_path=db)
        assert abs(c["total_cost_usd"] - 0.15) < 1e-6
        assert c["has_data"] is True

    def test_by_provider_breakdown(self, db):
        conn = get_connection(db)
        self._insert_cost(conn, "r3", "ep_3", "openrouter", 0.03)
        self._insert_cost(conn, "r4", "ep_4", "elevenlabs", 0.07)
        conn.commit(); conn.close()
        c = get_cost_facts(db_path=db)
        assert "openrouter" in c["by_provider"]
        assert "elevenlabs" in c["by_provider"]

    def test_avg_cost_per_episode(self, db):
        conn = get_connection(db)
        self._insert_cost(conn, "r5", "ep_5", "openrouter", 0.10)
        self._insert_cost(conn, "r6", "ep_6", "openrouter", 0.20)
        conn.commit(); conn.close()
        c = get_cost_facts(db_path=db)
        assert c["avg_cost_per_episode"] is not None
        assert abs(c["avg_cost_per_episode"] - 0.15) < 1e-6

    def test_safe_on_missing_db(self, tmp_path):
        c = get_cost_facts(db_path=tmp_path / "missing.db")
        assert c["total_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# TestPublishingFacts
# ---------------------------------------------------------------------------

class TestPublishingFacts:
    def test_empty_db_safe(self, db):
        p = get_publishing_facts(db_path=db)
        assert p["total_outcomes"] == 0
        assert p["has_data"] is False

    def test_all_statuses_present(self, db):
        p = get_publishing_facts(db_path=db)
        for st in ("success", "failed", "pending", "skipped", "unknown"):
            assert st in p["by_status"]

    def test_records_outcome(self, db):
        record_publishing_outcome("ep_001", "youtube", "success",
                                   platform_video_id="yt-abc123", db_path=db)
        p = get_publishing_facts(db_path=db)
        assert p["total_outcomes"] == 1
        assert p["successful"] == 1
        assert p["has_data"] is True

    def test_by_platform_counted(self, db):
        record_publishing_outcome("ep_002", "youtube",  "success", db_path=db)
        record_publishing_outcome("ep_003", "tiktok",   "failed",  db_path=db)
        p = get_publishing_facts(db_path=db)
        assert p["by_platform"]["youtube"] == 1
        assert p["by_platform"]["tiktok"]  == 1

    def test_safe_on_missing_db(self, tmp_path):
        p = get_publishing_facts(db_path=tmp_path / "missing.db")
        assert p["total_outcomes"] == 0


# ---------------------------------------------------------------------------
# TestAudienceFacts
# ---------------------------------------------------------------------------

class TestAudienceFacts:
    def _insert_pa(self, conn, ep_id, platform, source, views=1000):
        conn.execute("""
            INSERT OR REPLACE INTO platform_analytics
              (record_id, episode_id, platform, snapshot_date, window_days,
               views, source, imported_at)
            VALUES (?, ?, ?, '2026-01-01', 7, ?, ?, '2026-01-01T00:00:00Z')
        """, (str(uuid.uuid4()), ep_id, platform, views, source))

    def test_empty_db_safe(self, db):
        a = get_audience_facts(db_path=db)
        assert a["total_snapshots"] == 0
        assert a["has_real_data"] is False
        assert a["mock_warning"] is None

    def test_real_snapshot_counted(self, db):
        conn = get_connection(db)
        self._insert_pa(conn, "ep_1", "youtube", "manual")
        conn.commit(); conn.close()
        a = get_audience_facts(db_path=db)
        assert a["real_snapshots"] == 1
        assert a["mock_snapshots"] == 0
        assert a["has_real_data"] is True

    def test_mock_snapshot_excluded_from_has_real_data(self, db):
        conn = get_connection(db)
        self._insert_pa(conn, "ep_2", "youtube", "mock")
        conn.commit(); conn.close()
        a = get_audience_facts(db_path=db)
        assert a["mock_snapshots"] == 1
        assert a["real_snapshots"] == 0
        assert a["has_real_data"] is False

    def test_mock_warning_set_when_mock_only(self, db):
        conn = get_connection(db)
        self._insert_pa(conn, "ep_3", "youtube", "mock")
        conn.commit(); conn.close()
        a = get_audience_facts(db_path=db)
        assert a["mock_warning"] is not None
        assert "mock" in a["mock_warning"].lower()

    def test_mixed_real_and_mock(self, db):
        conn = get_connection(db)
        self._insert_pa(conn, "ep_4", "youtube", "manual")
        self._insert_pa(conn, "ep_5", "youtube", "mock")
        conn.commit(); conn.close()
        a = get_audience_facts(db_path=db)
        assert a["real_snapshots"] == 1
        assert a["mock_snapshots"] == 1
        assert a["has_real_data"] is True
        assert a["mock_warning"] is not None

    def test_real_snapshots_in_recent_real(self, db):
        conn = get_connection(db)
        self._insert_pa(conn, "ep_6", "youtube", "csv")
        self._insert_pa(conn, "ep_7", "youtube", "mock")
        conn.commit(); conn.close()
        a = get_audience_facts(db_path=db)
        assert len(a["recent_real"]) == 1
        assert a["recent_real"][0]["episode_id"] == "ep_6"

    def test_safe_on_missing_db(self, tmp_path):
        a = get_audience_facts(db_path=tmp_path / "missing.db")
        assert a["total_snapshots"] == 0


# ---------------------------------------------------------------------------
# TestPublishingStore
# ---------------------------------------------------------------------------

class TestPublishingStore:
    def test_record_returns_outcome_id(self, db):
        oid = record_publishing_outcome("ep_001", "youtube", "success", db_path=db)
        assert isinstance(oid, str)
        assert len(oid) == 36  # UUID4

    def test_record_writes_to_db(self, db):
        record_publishing_outcome("ep_002", "tiktok", "failed",
                                   failure_reason="Upload timeout", db_path=db)
        conn = get_connection(db)
        row = conn.execute(
            "SELECT * FROM publishing_outcomes WHERE episode_id='ep_002'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["publishing_status"] == "failed"
        assert row["failure_reason"] == "Upload timeout"

    def test_upsert_updates_existing_row(self, db):
        record_publishing_outcome("ep_003", "youtube", "pending", db_path=db)
        oid_first = record_publishing_outcome("ep_003", "youtube", "success",
                                               platform_video_id="yt-xyz", db_path=db)
        conn = get_connection(db)
        rows = conn.execute(
            "SELECT * FROM publishing_outcomes WHERE episode_id='ep_003'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1  # only one row per episode/platform
        assert rows[0]["publishing_status"] == "success"
        assert rows[0]["platform_video_id"] == "yt-xyz"

    def test_invalid_status_raises(self, db):
        with pytest.raises(ValueError):
            record_publishing_outcome("ep_004", "youtube", "invalid_status", db_path=db)

    def test_valid_statuses_accepted(self, db):
        for i, st in enumerate(("success", "failed", "pending", "skipped", "unknown")):
            record_publishing_outcome(f"ep_{i:02d}", "youtube", st, db_path=db)

    def test_no_external_calls(self, db):
        # socket is already blocked by autouse fixture — this just verifies no error
        record_publishing_outcome("ep_net", "youtube", "success", db_path=db)


# ---------------------------------------------------------------------------
# TestDecisionInputsState
# ---------------------------------------------------------------------------

class TestDecisionInputsState:
    def test_empty_db_returns_all_keys(self, db):
        s = get_decision_inputs_state(db_path=db)
        for key in ("checked_at", "discovery", "episodes", "publishing",
                    "costs", "audience", "readiness"):
            assert key in s

    def test_readiness_not_ready_on_empty_db(self, db):
        s = get_decision_inputs_state(db_path=db)
        r = s["readiness"]
        assert r["ready_for_decision_engine"] is False
        assert r["has_real_inputs"] is False

    def test_missing_inputs_lists_all_on_empty(self, db):
        s = get_decision_inputs_state(db_path=db)
        missing = s["readiness"]["missing_inputs"]
        for section in ("discovery", "episodes", "costs", "publishing", "audience"):
            assert section in missing

    def test_has_real_inputs_true_when_topics_exist(self, db):
        conn = get_connection(db)
        conn.execute("""
            INSERT INTO episode_topics
              (topic_id, channel_id, topic_text, status, priority, created_at, updated_at)
            VALUES (?, 'ch1', 'test topic', 'queued', 0,
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """, (str(uuid.uuid4()),))
        conn.commit(); conn.close()
        s = get_decision_inputs_state(db_path=db)
        assert s["readiness"]["has_real_inputs"] is True
        assert "discovery" not in s["readiness"]["missing_inputs"]

    def test_ready_for_decision_engine_requires_discovery_episodes_costs(self, db):
        conn = get_connection(db)
        # Add discovery
        conn.execute("""
            INSERT INTO episode_topics
              (topic_id, channel_id, topic_text, status, priority, created_at, updated_at)
            VALUES (?, 'ch1', 'topic', 'consumed', 0,
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """, (str(uuid.uuid4()),))
        # Add completed episode
        conn.execute("""
            INSERT INTO runs (run_id, episode_id, started_at, completed_at, status)
            VALUES ('r1', 'ep_1', '2026-01-01T10:00:00Z', '2026-01-01T11:00:00Z', 'completed')
        """)
        conn.execute("""
            INSERT INTO episodes (episode_id, created_at, status, run_id)
            VALUES ('ep_1', '2026-01-01T10:00:00Z', 'completed', 'r1')
        """)
        # Add cost
        conn.execute("""
            INSERT INTO costs (cost_id, run_id, episode_id, provider, call_type, cost_usd, called_at)
            VALUES (?, 'r1', 'ep_1', 'openrouter', 'llm', 0.05, '2026-01-01T10:01:00Z')
        """, (str(uuid.uuid4()),))
        conn.commit(); conn.close()

        s = get_decision_inputs_state(db_path=db)
        r = s["readiness"]
        assert r["ready_for_decision_engine"] is True

    def test_safe_on_missing_db(self, tmp_path):
        s = get_decision_inputs_state(db_path=tmp_path / "missing.db")
        assert isinstance(s, dict)
        assert s["readiness"]["ready_for_decision_engine"] is False

    def test_checked_at_is_iso_string(self, db):
        s = get_decision_inputs_state(db_path=db)
        assert "T" in s["checked_at"]


# ---------------------------------------------------------------------------
# TestNoExternalAPICalls
# ---------------------------------------------------------------------------

class TestNoExternalAPICalls:
    def test_full_state_no_network(self, db):
        s = get_decision_inputs_state(db_path=db)
        assert s is not None

    def test_store_no_network(self, db):
        record_publishing_outcome("ep_net", "youtube", "success", db_path=db)


# ---------------------------------------------------------------------------
# TestNoWrites
# ---------------------------------------------------------------------------

class TestNoWrites:
    def test_reads_do_not_write_to_episodes(self, db):
        conn = get_connection(db)
        before = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()
        get_episode_facts(db_path=db)
        conn = get_connection(db)
        after = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()
        assert before == after

    def test_reads_do_not_write_to_costs(self, db):
        conn = get_connection(db)
        before = conn.execute("SELECT COUNT(*) AS c FROM costs").fetchone()["c"]
        conn.close()
        get_cost_facts(db_path=db)
        conn = get_connection(db)
        after = conn.execute("SELECT COUNT(*) AS c FROM costs").fetchone()["c"]
        conn.close()
        assert before == after

    def test_reads_do_not_write_to_platform_analytics(self, db):
        conn = get_connection(db)
        before = conn.execute("SELECT COUNT(*) AS c FROM platform_analytics").fetchone()["c"]
        conn.close()
        get_audience_facts(db_path=db)
        conn = get_connection(db)
        after = conn.execute("SELECT COUNT(*) AS c FROM platform_analytics").fetchone()["c"]
        conn.close()
        assert before == after
