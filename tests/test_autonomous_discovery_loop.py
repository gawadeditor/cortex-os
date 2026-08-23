"""
tests/test_autonomous_discovery_loop.py — M5.1 Autonomous Discovery Loop

Tests:
  SignalContext          — _build_signal_context() reads both tables correctly
  ScoreSignalBoost       — signal boosts and penalties applied to _score_seed()
  ScoreNoAnalyticsFallback — score identical to pre-M5.1 heuristic when no data
  QueueRefillTrigger     — _check_and_refill_queue() fires at correct thresholds
  MultiChannelRefill     — refill iterates all active channels independently
  AutoApprove            — auto_approve=True enqueues; False leaves pending
  DuplicateTopic         — duplicate topic handled without crash
  WorkerIntegration      — run_iteration() calls discovery when enabled

All tests use in-memory SQLite. No network calls. No file I/O beyond tmp_path.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.topic_discovery import (
    _build_signal_context,
    _score_seed,
    _tokenise,
    run_discovery_cycle,
    approve_candidate,
)


# ---------------------------------------------------------------------------
# In-memory DB helpers
# ---------------------------------------------------------------------------

_SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS episodes (
    episode_id TEXT NOT NULL PRIMARY KEY,
    title TEXT, topic TEXT,
    status TEXT NOT NULL DEFAULT 'in_progress',
    run_id TEXT,
    created_at TEXT NOT NULL,
    metadata_version TEXT NOT NULL DEFAULT 'v1'
);
CREATE TABLE IF NOT EXISTS channel_episodes (
    assignment_id TEXT NOT NULL PRIMARY KEY,
    channel_id    TEXT NOT NULL,
    episode_id    TEXT NOT NULL,
    assigned_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS platform_analytics (
    record_id TEXT NOT NULL PRIMARY KEY,
    episode_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    window_days INTEGER NOT NULL DEFAULT 7,
    views INTEGER, impressions INTEGER,
    ctr REAL, avg_view_pct REAL,
    avg_watch_time_s REAL, total_watch_time_s REAL,
    likes INTEGER, comments INTEGER, shares INTEGER, saves INTEGER,
    platform_video_id TEXT, raw_json TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    imported_at TEXT NOT NULL,
    metadata_version TEXT NOT NULL DEFAULT 'v1',
    UNIQUE(episode_id, platform, snapshot_date)
);
CREATE TABLE IF NOT EXISTS analytics_insights (
    insight_id TEXT NOT NULL PRIMARY KEY,
    episode_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    created_at TEXT NOT NULL,
    insight_type TEXT NOT NULL,
    affected_dim TEXT NOT NULL,
    signal_strength REAL NOT NULL,
    threshold_used TEXT,
    detail TEXT,
    metadata_version TEXT NOT NULL DEFAULT 'v1'
);
CREATE TABLE IF NOT EXISTS episode_topics (
    topic_id TEXT NOT NULL PRIMARY KEY,
    channel_id TEXT NOT NULL,
    topic_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    claimed_at TEXT,
    consumed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topic_candidates (
    candidate_id TEXT NOT NULL PRIMARY KEY,
    channel_id TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'heuristic',
    score REAL NOT NULL DEFAULT 0.0,
    ctr_rationale TEXT,
    retention_rationale TEXT,
    watch_time_rationale TEXT,
    risk TEXT,
    selected INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'youtube',
    identity_json TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
"""


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_TABLES)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_cleared_factory(db_path) -> None:
    """Seed the minimal active factory + credential rows
    hypogeum.factory_director.check_run_clearance() requires to return
    cleared=True. Without this, run_iteration()'s clearance gate (wired
    2026-07-18) skips poll on a bare test schema, since a schema with no
    factory row / no active credentials is a genuinely un-cleared state,
    not a test artifact to special-case around."""
    from hypogeum.db import get_connection
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO factories (factory_id, factory_name, factory_slug, "
            "status, default_language, default_timezone, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("hypogeum", "Test Factory", "hypogeum", "active", "en", "UTC",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO secret_profiles "
            "(secret_profile_id, provider, label, credential_type, "
            "credential_data, status, scope, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "openrouter", "openrouter-env", "api_key",
             "dummy", "active", "factory",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _add_episode(conn, episode_id: str, channel_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO episodes (episode_id, created_at) VALUES (?,?)",
        (episode_id, _now()),
    )
    conn.execute(
        "INSERT OR IGNORE INTO channel_episodes (assignment_id, channel_id, episode_id, assigned_at) VALUES (?,?,?,?)",
        (str(uuid.uuid4()), channel_id, episode_id, _now()),
    )
    conn.commit()


def _add_analytics(conn, episode_id: str, *, ctr=0.05, avg_view_pct=0.60,
                   avg_watch_time_s=300.0, source="manual") -> None:
    conn.execute(
        """INSERT OR IGNORE INTO platform_analytics
           (record_id, episode_id, platform, snapshot_date, ctr,
            avg_view_pct, avg_watch_time_s, source, imported_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), episode_id, "youtube", "2026-06-01",
         ctr, avg_view_pct, avg_watch_time_s, source, _now()),
    )
    conn.commit()


def _add_insight(conn, episode_id: str, insight_type: str, detail: str) -> None:
    conn.execute(
        """INSERT INTO analytics_insights
           (insight_id, episode_id, platform, created_at, insight_type,
            affected_dim, signal_strength, detail)
           VALUES (?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), episode_id, "youtube", _now(),
         insight_type, "topic", 0.8, detail),
    )
    conn.commit()


def _add_channel(conn, channel_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO channels (channel_id, name, created_at) VALUES (?,?,?)",
        (channel_id, channel_id, _now()),
    )
    conn.commit()


def _add_queued_topic(conn, channel_id: str, topic_text: str) -> str:
    tid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO episode_topics
           (topic_id, channel_id, topic_text, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?)""",
        (tid, channel_id, topic_text, "queued", _now(), _now()),
    )
    conn.commit()
    return tid


# ---------------------------------------------------------------------------
# Helper: no-close wrapper so multi-channel tests share one in-memory conn
# ---------------------------------------------------------------------------

class _NoClose:
    """Wraps a sqlite3.Connection; makes close() a no-op so the same in-memory
    DB can be re-used across multiple get_connection() calls in one test."""
    def __init__(self, conn: sqlite3.Connection):
        self._c = conn
    def execute(self, *a, **kw):
        return self._c.execute(*a, **kw)
    def executemany(self, *a, **kw):
        return self._c.executemany(*a, **kw)
    def commit(self):
        return self._c.commit()
    def close(self):
        pass  # intentional no-op


@contextmanager
def _use_db(conn: sqlite3.Connection):
    """Patch hypogeum.db.get_connection to return a pre-built in-memory connection."""
    with patch("hypogeum.db.get_connection", return_value=conn):
        yield conn


# ---------------------------------------------------------------------------
# SignalContext tests
# ---------------------------------------------------------------------------

class TestSignalContext:

    def test_empty_db_returns_no_analytics(self):
        conn = _make_db()
        ctx = _build_signal_context("ch_test", conn)
        assert ctx["has_real_analytics"] is False
        assert ctx["top_performer_keywords"] == set()
        assert ctx["penalty_keywords"] == set()
        assert ctx["avg_ctr"] is None

    def test_mock_analytics_ignored(self):
        """platform_analytics rows with source='mock' must not count as real data."""
        conn = _make_db()
        _add_episode(conn, "ep1", "ch_test")
        _add_analytics(conn, "ep1", source="mock")
        ctx = _build_signal_context("ch_test", conn)
        assert ctx["has_real_analytics"] is False

    def test_real_analytics_sets_kpis(self):
        conn = _make_db()
        _add_episode(conn, "ep1", "ch_test")
        _add_analytics(conn, "ep1", ctr=0.07, avg_view_pct=0.65, avg_watch_time_s=320.0)
        ctx = _build_signal_context("ch_test", conn)
        assert ctx["has_real_analytics"] is True
        assert abs(ctx["avg_ctr"] - 0.07) < 1e-6
        assert abs(ctx["avg_view_pct"] - 0.65) < 1e-6
        assert abs(ctx["avg_watch_time_s"] - 320.0) < 1e-1

    def test_top_performer_keywords_extracted(self):
        conn = _make_db()
        _add_episode(conn, "ep1", "ch_test")
        _add_insight(conn, "ep1", "top_performer",
                     "Ancient civilisations drive strongest audience retention")
        ctx = _build_signal_context("ch_test", conn)
        assert ctx["has_real_analytics"] is True
        assert "ancient" in ctx["top_performer_keywords"]
        assert "civilisations" in ctx["top_performer_keywords"]
        assert "drive" in ctx["top_performer_keywords"]

    def test_penalty_keywords_extracted(self):
        conn = _make_db()
        _add_episode(conn, "ep1", "ch_test")
        _add_insight(conn, "ep1", "hook_failure",
                     "Society trade routes lost audience in first 30s")
        ctx = _build_signal_context("ch_test", conn)
        assert ctx["has_real_analytics"] is True
        assert "society" in ctx["penalty_keywords"]
        assert "trade" in ctx["penalty_keywords"]

    def test_high_retention_keywords_extracted(self):
        conn = _make_db()
        _add_episode(conn, "ep1", "ch_test")
        _add_insight(conn, "ep1", "high_retention",
                     "Underground architecture maintains 70% completion")
        ctx = _build_signal_context("ch_test", conn)
        assert ctx["has_real_analytics"] is True
        assert "underground" in ctx["high_retention_keywords"]
        assert "architecture" in ctx["high_retention_keywords"]

    def test_only_channel_episodes_included(self):
        """Signal context must be scoped to the channel — not bleed across channels."""
        conn = _make_db()
        _add_episode(conn, "ep_other", "ch_other")
        _add_insight(conn, "ep_other", "top_performer", "Secrets ruins history ancient")
        ctx = _build_signal_context("ch_test", conn)
        # ch_test has no episodes — context must have no data
        assert ctx["has_real_analytics"] is False

    def test_stopwords_excluded_from_keywords(self):
        conn = _make_db()
        _add_episode(conn, "ep1", "ch_test")
        _add_insight(conn, "ep1", "top_performer",
                     "The ancient ruins of the empire were explored")
        ctx = _build_signal_context("ch_test", conn)
        # "the", "of", "were" are stopwords and must not appear
        assert "the" not in ctx["top_performer_keywords"]
        assert "of" not in ctx["top_performer_keywords"]
        assert "were" not in ctx["top_performer_keywords"]
        # Content words must be present
        assert "ancient" in ctx["top_performer_keywords"]
        assert "ruins" in ctx["top_performer_keywords"]

    def test_analytics_exception_returns_empty_context(self):
        """DB errors must never crash discovery — return empty context."""
        conn = _make_db()
        conn.close()  # deliberately break the connection
        ctx = _build_signal_context("ch_test", conn)
        assert ctx["has_real_analytics"] is False


# ---------------------------------------------------------------------------
# Score boost / penalty tests
# ---------------------------------------------------------------------------

class TestScoreSignalBoost:

    _SEED_ANCIENT_UNDERGROUND = {
        "topic": "The hidden history of ancient underground cities",
        "source": "heuristic",
        "ctr_rationale": "", "retention_rationale": "",
        "watch_time_rationale": "", "risk": "",
    }
    _SEED_TRADE_ROUTES = {
        "topic": "Ancient trade routes that built the modern world",
        "source": "heuristic",
        "ctr_rationale": "", "retention_rationale": "",
        "watch_time_rationale": "", "risk": "",
    }

    def _ctx(self, top=None, high=None, penalty=None):
        return {
            "has_real_analytics": True,
            "top_performer_keywords": set(top or []),
            "high_retention_keywords": set(high or []),
            "penalty_keywords": set(penalty or []),
            "avg_ctr": 0.05,
            "avg_view_pct": 0.60,
            "avg_watch_time_s": 300.0,
        }

    def test_boost_top_performer_overlap(self):
        base = _score_seed(self._SEED_ANCIENT_UNDERGROUND, set(), None)
        boosted = _score_seed(self._SEED_ANCIENT_UNDERGROUND, set(),
                              self._ctx(top=["ancient", "underground", "hidden"]))
        assert boosted > base, f"Expected boost: base={base}, boosted={boosted}"

    def test_boost_high_retention_overlap(self):
        base = _score_seed(self._SEED_ANCIENT_UNDERGROUND, set(), None)
        boosted = _score_seed(self._SEED_ANCIENT_UNDERGROUND, set(),
                              self._ctx(high=["ancient", "underground"]))
        assert boosted > base

    def test_penalty_low_retention_overlap(self):
        base = _score_seed(self._SEED_TRADE_ROUTES, set(), None)
        penalised = _score_seed(self._SEED_TRADE_ROUTES, set(),
                                self._ctx(penalty=["ancient", "trade", "routes"]))
        assert penalised < base, f"Expected penalty: base={base}, penalised={penalised}"

    def test_boost_then_novelty_penalty(self):
        """Signal boost applied before novelty penalty — novelty multiplies last."""
        existing = {"ancient trade routes that built the modern world"}
        base = _score_seed(self._SEED_TRADE_ROUTES, set(), None)
        base_with_novelty = _score_seed(self._SEED_TRADE_ROUTES, existing, None)
        boosted_with_novelty = _score_seed(
            self._SEED_TRADE_ROUTES, existing,
            self._ctx(top=["ancient", "trade", "routes"])
        )
        # Boost + novelty must still be higher than base + novelty (no boost)
        assert boosted_with_novelty > base_with_novelty
        # But both must be below the base score without novelty
        assert base_with_novelty < base

    def test_score_clamped_above_1(self):
        """Even with boost, score must never exceed 1.0."""
        # Set up a seed already at 0.82; boost it — must stay ≤ 1.0
        ctx = self._ctx(top=["hidden", "ancient", "underground", "history", "cities"])
        score = _score_seed(self._SEED_ANCIENT_UNDERGROUND, set(), ctx)
        assert score <= 1.0

    def test_score_clamped_below_0(self):
        """Score must never go negative."""
        ctx = self._ctx(penalty=["hidden", "history", "ancient", "underground",
                                  "cities", "secret", "ruins", "trade", "lost"])
        # Apply penalty to seed with base 0.64 (secrets topic)
        seed = {
            "topic": "Secret societies and the empires they shaped",
            "source": "heuristic",
            "ctr_rationale": "", "retention_rationale": "",
            "watch_time_rationale": "", "risk": "",
        }
        score = _score_seed(seed, set(), ctx)
        assert score >= 0.0

    def test_no_analytics_identical_to_heuristic(self):
        """With signal_ctx=None, scores must exactly match pre-M5.1 values."""
        expected = {
            "The hidden history of ancient underground cities": 0.82,
            "Lost civilisations: what archaeology refuses to explain": 0.78,
            "The architecture of power: buildings that shaped empires": 0.71,
            "Ancient trade routes that built the modern world": 0.67,
            "The real story behind the world's most mysterious ruins": 0.74,
            "How ancient engineers solved problems we still can't explain": 0.69,
            "Secret societies and the empires they shaped": 0.64,
        }
        for topic_text, expected_score in expected.items():
            seed = {"topic": topic_text, "source": "heuristic",
                    "ctr_rationale": "", "retention_rationale": "",
                    "watch_time_rationale": "", "risk": ""}
            got = _score_seed(seed, set(), None)
            assert got == expected_score, (
                f"Heuristic score mismatch for {topic_text!r}: "
                f"expected {expected_score}, got {got}"
            )

    def test_has_real_analytics_false_uses_heuristic(self):
        """signal_ctx with has_real_analytics=False must produce same score as None."""
        empty_ctx = {
            "has_real_analytics": False,
            "top_performer_keywords": {"ancient", "ruins"},
            "high_retention_keywords": set(),
            "penalty_keywords": set(),
            "avg_ctr": None, "avg_view_pct": None, "avg_watch_time_s": None,
        }
        seed = {
            "topic": "The hidden history of ancient underground cities",
            "source": "heuristic",
            "ctr_rationale": "", "retention_rationale": "",
            "watch_time_rationale": "", "risk": "",
        }
        score_none = _score_seed(seed, set(), None)
        score_empty = _score_seed(seed, set(), empty_ctx)
        assert score_none == score_empty, (
            f"Empty analytics context must produce heuristic score: "
            f"none={score_none}, empty_ctx={score_empty}"
        )


# ---------------------------------------------------------------------------
# Queue refill trigger tests
# ---------------------------------------------------------------------------

class TestQueueRefillTrigger:
    """Tests for _check_and_refill_queue() directly."""

    def _fake_channel(self, channel_id: str):
        ch = SimpleNamespace()
        ch.channel_id = channel_id
        return ch

    def _run_refill(self, channels, queued_topics, threshold=2, auto_approve=False,
                    db_path=None):
        """
        Run _check_and_refill_queue() with mocked list_channels and DB.

        `queued_topics` is a dict: {channel_id: count_of_queued_topics}.
        Returns (result_dict, list_of_run_discovery_cycle_calls).
        """
        from run_worker import _check_and_refill_queue

        discovery_calls = []

        def _fake_discovery(channel_id, db_path=None):
            discovery_calls.append(channel_id)
            return {
                "ok": True,
                "cycle_id": f"cycle_{channel_id}",
                "channel_id": channel_id,
                "candidate_count": 7,
                "selected_candidate_id": f"cand_{channel_id}",
                "candidates": [],
            }

        def _fake_approve(candidate_id, db_path=None):
            return {
                "ok": True,
                "candidate_id": candidate_id,
                "topic_id": f"tid_{candidate_id}",
                "topic_text": f"Topic for {candidate_id}",
                "channel_id": "ch",
            }

        def _fake_list_channels(status=None, db_path=None):
            return channels

        # Build an in-memory DB and populate queue counts
        real_conn = _make_db()
        for ch_id, count in queued_topics.items():
            _add_channel(real_conn, ch_id)
            for i in range(count):
                _add_queued_topic(real_conn, ch_id, f"Topic {i} for {ch_id}")

        def _fake_get_conn(db_path=None):
            return _NoClose(real_conn)

        # 2026-08-23: _check_and_refill_queue()'s per-channel operator_enabled
        # gate now fails CLOSED on a missing/unreadable ChannelOperationsProfile
        # (autonomy-safety correction -- discovery is a paid, mutating action).
        # This helper's channels are SimpleNamespace stand-ins with no real
        # channel_operations_profiles row, so every channel_id must resolve
        # to an explicitly-enabled fake profile here to keep exercising the
        # threshold/multi-channel/auto-approve logic these tests actually
        # target, rather than being blocked earlier by the (now correct)
        # missing-profile gate.
        fake_profile = SimpleNamespace(operator_enabled=True, deliverable_bundle_definitions=[])

        def _fake_get_ops_profile(channel_id, db_path=None):
            return fake_profile

        with patch("hypogeum.channel_config.list_channel_profiles", side_effect=_fake_list_channels), \
             patch("hypogeum.channel_config.get_channel_operations_profile", side_effect=_fake_get_ops_profile), \
             patch("hypogeum.topic_discovery.run_discovery_cycle", side_effect=_fake_discovery), \
             patch("hypogeum.topic_discovery.approve_candidate", side_effect=_fake_approve), \
             patch("hypogeum.db.get_connection", side_effect=_fake_get_conn):
            result = _check_and_refill_queue(
                db_path=db_path or Path("/fake"),
                threshold=threshold,
                auto_approve=auto_approve,
            )

        return result, discovery_calls

    def test_queue_below_threshold_triggers_discovery(self):
        ch = self._fake_channel("ch_main")
        result, calls = self._run_refill([ch], {"ch_main": 0}, threshold=2)
        assert "ch_main" in calls, "Discovery must fire when queue is empty"

    def test_queue_at_threshold_no_discovery(self):
        ch = self._fake_channel("ch_main")
        result, calls = self._run_refill([ch], {"ch_main": 2}, threshold=2)
        assert "ch_main" not in calls, "Discovery must not fire when depth == threshold"

    def test_queue_above_threshold_no_discovery(self):
        ch = self._fake_channel("ch_main")
        result, calls = self._run_refill([ch], {"ch_main": 5}, threshold=2)
        assert calls == []

    def test_queue_depth_1_below_threshold_2_triggers(self):
        ch = self._fake_channel("ch_main")
        result, calls = self._run_refill([ch], {"ch_main": 1}, threshold=2)
        assert "ch_main" in calls

    def test_result_channels_refilled_count(self):
        ch = self._fake_channel("ch_main")
        result, _ = self._run_refill([ch], {"ch_main": 0}, threshold=2)
        assert result["channels_refilled"] == 1
        assert result["channels_checked"] == 1

    def test_no_channels_returns_empty_result(self):
        result, calls = self._run_refill([], {}, threshold=2)
        assert result["channels_checked"] == 0
        assert result["channels_refilled"] == 0
        assert calls == []


# ---------------------------------------------------------------------------
# Multi-channel refill tests
# ---------------------------------------------------------------------------

class TestMultiChannelRefill:

    def _fake_channel(self, channel_id: str):
        ch = SimpleNamespace()
        ch.channel_id = channel_id
        return ch

    def _run_multi(self, channel_queue_map: dict, threshold=2, auto_approve=False):
        """channel_queue_map = {channel_id: queued_count}"""
        from run_worker import _check_and_refill_queue

        discovery_calls = []

        def _fake_discovery(channel_id, db_path=None):
            discovery_calls.append(channel_id)
            return {
                "ok": True,
                "cycle_id": f"cycle_{channel_id}",
                "channel_id": channel_id,
                "candidate_count": 7,
                "selected_candidate_id": f"cand_{channel_id}",
                "candidates": [],
            }

        def _fake_approve(candidate_id, db_path=None):
            return {
                "ok": True,
                "candidate_id": candidate_id,
                "topic_id": str(uuid.uuid4()),
                "topic_text": f"Topic for {candidate_id}",
                "channel_id": "ch",
            }

        channels = [self._fake_channel(cid) for cid in channel_queue_map]

        real_conn = _make_db()
        for ch_id, count in channel_queue_map.items():
            _add_channel(real_conn, ch_id)
            for i in range(count):
                _add_queued_topic(real_conn, ch_id, f"Topic {i} for {ch_id}")

        def _fake_get_conn(db_path=None):
            return _NoClose(real_conn)

        # 2026-08-23: see TestQueueRefillTrigger._run_refill()'s own comment
        # -- the per-channel operator_enabled gate now fails closed on a
        # missing profile, so every fake channel here needs an explicitly-
        # enabled one.
        fake_profile = SimpleNamespace(operator_enabled=True, deliverable_bundle_definitions=[])

        def _fake_get_ops_profile(channel_id, db_path=None):
            return fake_profile

        with patch("hypogeum.channel_config.list_channel_profiles", return_value=channels), \
             patch("hypogeum.channel_config.get_channel_operations_profile", side_effect=_fake_get_ops_profile), \
             patch("hypogeum.topic_discovery.run_discovery_cycle", side_effect=_fake_discovery), \
             patch("hypogeum.topic_discovery.approve_candidate", side_effect=_fake_approve), \
             patch("hypogeum.db.get_connection", side_effect=_fake_get_conn):
            result = _check_and_refill_queue(
                db_path=Path("/fake"),
                threshold=threshold,
                auto_approve=auto_approve,
            )

        return result, discovery_calls

    def test_two_channels_both_below_threshold(self):
        result, calls = self._run_multi({"ch_a": 0, "ch_b": 1}, threshold=2)
        assert "ch_a" in calls
        assert "ch_b" in calls
        assert result["channels_refilled"] == 2
        assert result["channels_checked"] == 2

    def test_one_below_one_above(self):
        result, calls = self._run_multi({"ch_a": 0, "ch_b": 5}, threshold=2)
        assert "ch_a" in calls
        assert "ch_b" not in calls
        assert result["channels_refilled"] == 1
        assert result["channels_checked"] == 2

    def test_all_above_threshold_no_discovery(self):
        result, calls = self._run_multi({"ch_a": 3, "ch_b": 4, "ch_c": 10}, threshold=2)
        assert calls == []
        assert result["channels_refilled"] == 0

    def test_three_channels_each_triggers_independently(self):
        result, calls = self._run_multi({"ch_a": 0, "ch_b": 0, "ch_c": 0}, threshold=2)
        assert set(calls) == {"ch_a", "ch_b", "ch_c"}
        assert result["channels_refilled"] == 3


# ---------------------------------------------------------------------------
# Auto-approve tests
# ---------------------------------------------------------------------------

class TestAutoApprove:

    def _fake_channel(self, channel_id="ch_main"):
        ch = SimpleNamespace()
        ch.channel_id = channel_id
        return ch

    def _run_with_approve(self, auto_approve: bool, discovery_ok=True,
                          selected_id="cand_001", approve_return=None):
        from run_worker import _check_and_refill_queue

        approve_calls = []
        approve_ret = approve_return or {
            "ok": True,
            "candidate_id": selected_id,
            "topic_id": "tid_001",
            "topic_text": "Approved topic text",
            "channel_id": "ch_main",
        }

        def _fake_discovery(channel_id, db_path=None):
            return {
                "ok": discovery_ok,
                "cycle_id": "cycle_001",
                "channel_id": channel_id,
                "candidate_count": 7,
                "selected_candidate_id": selected_id if discovery_ok else None,
                "candidates": [],
            }

        def _fake_approve(candidate_id, db_path=None):
            approve_calls.append(candidate_id)
            return approve_ret

        real_conn = _make_db()
        _add_channel(real_conn, "ch_main")
        # Queue is empty → triggers refill

        def _fake_get_conn(db_path=None):
            return _NoClose(real_conn)

        # 2026-08-23: see TestQueueRefillTrigger._run_refill()'s own comment.
        fake_profile = SimpleNamespace(operator_enabled=True, deliverable_bundle_definitions=[])

        def _fake_get_ops_profile(channel_id, db_path=None):
            return fake_profile

        with patch("hypogeum.channel_config.list_channel_profiles", return_value=[self._fake_channel()]), \
             patch("hypogeum.channel_config.get_channel_operations_profile", side_effect=_fake_get_ops_profile), \
             patch("hypogeum.topic_discovery.run_discovery_cycle", side_effect=_fake_discovery), \
             patch("hypogeum.topic_discovery.approve_candidate", side_effect=_fake_approve), \
             patch("hypogeum.db.get_connection", side_effect=_fake_get_conn):
            result = _check_and_refill_queue(
                db_path=Path("/fake"),
                threshold=2,
                auto_approve=auto_approve,
            )

        return result, approve_calls

    def test_auto_approve_true_calls_approve_candidate(self):
        result, calls = self._run_with_approve(auto_approve=True)
        assert calls == ["cand_001"], (
            f"approve_candidate must be called once; got calls={calls}"
        )

    def test_auto_approve_true_enqueues_topic(self):
        result, calls = self._run_with_approve(auto_approve=True)
        assert result["topics_enqueued"] == ["Approved topic text"]

    def test_auto_approve_false_does_not_call_approve(self):
        result, calls = self._run_with_approve(auto_approve=False)
        assert calls == [], "approve_candidate must NOT be called when auto_approve=False"
        assert result["topics_enqueued"] == []

    def test_auto_approve_true_no_selected_candidate_skips_approve(self):
        """If discovery produces no selected candidate, approve must not be called."""
        result, calls = self._run_with_approve(auto_approve=True, selected_id=None)
        assert calls == []

    def test_auto_approve_duplicate_code_not_an_error(self):
        """A 'duplicate' approve response must be handled gracefully."""
        dup_return = {"ok": False, "code": "duplicate", "error": "already queued"}
        result, calls = self._run_with_approve(
            auto_approve=True, approve_return=dup_return
        )
        # Duplicate is not fatal — function should complete without raising
        assert result["channels_refilled"] == 1


# ---------------------------------------------------------------------------
# Duplicate topic handling
# ---------------------------------------------------------------------------

class TestDuplicateTopic:

    def test_duplicate_topic_handled_in_approve_candidate(self, tmp_path):
        """
        approve_candidate() calls enqueue_topic() which raises DuplicateTopicError.
        The function must catch this and return ok=True with topic_id='duplicate'
        or an existing queued topic_id.
        """
        db_file = tmp_path / "test.db"

        # Bootstrap schema using real get_connection
        from hypogeum.db import get_connection, initialize_schema
        from hypogeum.channel_config import _ensure_tables as _ensure_channel_tables
        initialize_schema(db_file)
        # channel_profiles is created by channel_config._ensure_tables, not schema.sql
        _cp_conn = get_connection(db_file)
        try:
            _ensure_channel_tables(_cp_conn)
        finally:
            _cp_conn.close()

        real_conn = get_connection(db_file)
        try:
            # channel_profiles must exist for validate_channel=True to pass
            real_conn.execute(
                """INSERT OR IGNORE INTO channel_profiles
                   (channel_id, name, created_at)
                   VALUES ('ch1', 'Test Channel', ?)""",
                (_now(),),
            )
            # Insert an existing queued topic (this is the "duplicate")
            real_conn.execute(
                """INSERT INTO episode_topics
                   (topic_id, channel_id, topic_text, status, created_at, updated_at)
                   VALUES ('tid_existing', 'ch1', 'The hidden history of ancient underground cities',
                           'queued', ?, ?)""",
                (_now(), _now()),
            )
            # Insert a selected candidate with the same topic text
            real_conn.execute(
                """INSERT INTO topic_candidates
                   (candidate_id, channel_id, cycle_id, topic, source, score,
                    selected, status, created_at, updated_at)
                   VALUES ('cand_dup', 'ch1', 'cycle_001',
                           'The hidden history of ancient underground cities',
                           'heuristic', 0.82, 1, 'pending', ?, ?)""",
                (_now(), _now()),
            )
            real_conn.commit()
        finally:
            real_conn.close()

        result = approve_candidate("cand_dup", db_path=db_file)
        # The function must succeed (ok=True) even though the topic was a duplicate
        assert result["ok"] is True
        assert result["candidate_id"] == "cand_dup"


# ---------------------------------------------------------------------------
# Full discovery cycle integration (using real DB)
# ---------------------------------------------------------------------------

_CHANNEL_EPISODES_DDL = """
CREATE TABLE IF NOT EXISTS channel_episodes (
    assignment_id TEXT NOT NULL PRIMARY KEY,
    channel_id    TEXT NOT NULL,
    episode_id    TEXT NOT NULL,
    assigned_at   TEXT NOT NULL
);
"""


def _ensure_channel_episodes(db_path: Path) -> None:
    """Create channel_episodes table if not present (schema.sql doesn't include it)."""
    import sqlite3 as _sq3
    conn = _sq3.connect(str(db_path))
    conn.executescript(_CHANNEL_EPISODES_DDL)
    conn.commit()
    conn.close()


class TestDiscoveryCycleIntegration:

    def test_run_discovery_cycle_produces_candidates(self, tmp_path):
        """run_discovery_cycle() must write 7 candidates and mark selected=1 for top."""
        from hypogeum.db import initialize_schema
        db_file = tmp_path / "test.db"
        initialize_schema(db_file)

        result = run_discovery_cycle("ch_test", db_path=db_file)

        assert result["ok"] is True
        assert result["candidate_count"] == 7
        assert result["selected_candidate_id"] is not None

        # Verify DB state
        from hypogeum.db import get_connection
        conn = get_connection(db_file)
        try:
            rows = conn.execute(
                "SELECT selected, status FROM topic_candidates WHERE channel_id='ch_test'"
            ).fetchall()
        finally:
            conn.close()

        selected_count = sum(1 for r in rows if r[0] == 1)
        assert selected_count == 1, f"Exactly one candidate must be selected; got {selected_count}"

    def test_run_discovery_cycle_with_analytics_uses_signal_context(self, tmp_path):
        """With real analytics, discovery cycle must use signal-adjusted scores."""
        from hypogeum.db import initialize_schema, get_connection
        db_file = tmp_path / "test.db"
        initialize_schema(db_file)
        _ensure_channel_episodes(db_file)

        # Seed real analytics data
        conn = get_connection(db_file)
        try:
            conn.execute(
                "INSERT INTO episodes (episode_id, created_at) VALUES (?,?)",
                ("ep1", _now()),
            )
            # Link episode to channel via channel_episodes
            conn.execute(
                """INSERT OR IGNORE INTO channel_episodes
                   (assignment_id, channel_id, episode_id, assigned_at)
                   VALUES (?,?,?,?)""",
                (str(uuid.uuid4()), "ch_analytics", "ep1", _now()),
            )
            # Real (non-mock) analytics row
            conn.execute(
                """INSERT INTO platform_analytics
                   (record_id, episode_id, platform, snapshot_date, ctr,
                    avg_view_pct, avg_watch_time_s, source, imported_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), "ep1", "youtube", "2026-06-01",
                 0.08, 0.72, 350.0, "api", _now()),
            )
            # Top-performer insight containing words from a seed topic
            conn.execute(
                """INSERT INTO analytics_insights
                   (insight_id, episode_id, platform, created_at, insight_type,
                    affected_dim, signal_strength, detail)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), "ep1", "youtube", _now(),
                 "top_performer", "topic", 0.9,
                 "Hidden underground ancient civilisations performed best"),
            )
            conn.commit()
        finally:
            conn.close()

        result = run_discovery_cycle("ch_analytics", db_path=db_file)
        assert result["ok"] is True

        # The top candidate must be one that overlaps "hidden", "underground", "ancient"
        top = next(c for c in result["candidates"] if c["selected"] == 1)
        assert any(word in top["topic"].lower()
                   for word in ("ancient", "underground", "hidden", "civilisation")), (
            f"Top candidate should overlap top-performer keywords; got: {top['topic']!r}"
        )

    def test_no_analytics_scores_match_heuristic(self, tmp_path):
        """With no analytics data, scores must exactly match the pre-M5.1 heuristic."""
        from hypogeum.db import initialize_schema
        db_file = tmp_path / "test.db"
        initialize_schema(db_file)

        result = run_discovery_cycle("ch_empty", db_path=db_file)
        assert result["ok"] is True

        expected_scores = {
            "The hidden history of ancient underground cities": 0.82,
            "Lost civilisations: what archaeology refuses to explain": 0.78,
            "The architecture of power: buildings that shaped empires": 0.71,
            "Ancient trade routes that built the modern world": 0.67,
            "The real story behind the world's most mysterious ruins": 0.74,
            "How ancient engineers solved problems we still can't explain": 0.69,
            "Secret societies and the empires they shaped": 0.64,
        }
        for candidate in result["candidates"]:
            expected = expected_scores.get(candidate["topic"])
            if expected is not None:
                assert candidate["score"] == expected, (
                    f"Heuristic score mismatch: {candidate['topic']!r}: "
                    f"expected {expected}, got {candidate['score']}"
                )


# ---------------------------------------------------------------------------
# Worker integration — run_iteration() with AUTO_DISCOVERY_ENABLED
# ---------------------------------------------------------------------------

class TestWorkerIntegration:

    def _make_config(self, enabled=True, threshold=2, auto_approve=False):
        cfg = SimpleNamespace()
        cfg.AUTO_DISCOVERY_ENABLED = enabled
        cfg.AUTO_DISCOVERY_THRESHOLD = threshold
        cfg.AUTO_DISCOVERY_AUTO_APPROVE = auto_approve
        return cfg

    def test_run_iteration_calls_discovery_when_enabled(self, tmp_path):
        from run_worker import run_iteration

        refill_calls = []

        def _fake_refill(db_path, threshold, auto_approve):
            refill_calls.append({"threshold": threshold, "auto_approve": auto_approve})
            return {"channels_checked": 1, "channels_refilled": 0,
                    "cycles_run": [], "topics_enqueued": []}

        def _fake_tick(**kw):     return []
        def _fake_consume(**kw):  return []

        poll_result = SimpleNamespace()
        poll_result.results = []
        poll_result.stop_reason = "empty"

        def _fake_poll(**kw):     return poll_result

        with patch("run_worker._check_and_refill_queue", side_effect=_fake_refill), \
             patch("hypogeum.scheduler.tick",                   side_effect=_fake_tick), \
             patch("hypogeum.queue.consume_scheduled_runs",     side_effect=_fake_consume), \
             patch("hypogeum.queue.poll",                       side_effect=_fake_poll):
            result = run_iteration(
                worker_id="w1",
                db_path=tmp_path / "test.db",
                dry_run=False,
                channel_id=None,
                config=self._make_config(enabled=True),
            )

        assert refill_calls, "Discovery must be called when AUTO_DISCOVERY_ENABLED=True"
        assert refill_calls[0]["threshold"] == 2
        assert refill_calls[0]["auto_approve"] is False

    def test_run_iteration_skips_discovery_when_disabled(self, tmp_path):
        from run_worker import run_iteration

        refill_calls = []

        def _fake_refill(**kw):
            refill_calls.append(True)
            return {}

        def _fake_tick(**kw):     return []
        def _fake_consume(**kw):  return []
        poll_result = SimpleNamespace(results=[], stop_reason="empty")
        def _fake_poll(**kw):     return poll_result

        with patch("run_worker._check_and_refill_queue", side_effect=_fake_refill), \
             patch("hypogeum.scheduler.tick",                   side_effect=_fake_tick), \
             patch("hypogeum.queue.consume_scheduled_runs",     side_effect=_fake_consume), \
             patch("hypogeum.queue.poll",                       side_effect=_fake_poll):
            run_iteration(
                worker_id="w1",
                db_path=tmp_path / "test.db",
                dry_run=False,
                channel_id=None,
                config=self._make_config(enabled=False),
            )

        assert refill_calls == [], "Discovery must NOT be called when AUTO_DISCOVERY_ENABLED=False"

    def test_run_iteration_result_includes_discovery_key(self, tmp_path):
        from run_worker import run_iteration

        def _fake_refill(**kw):
            return {"channels_checked": 1, "channels_refilled": 1,
                    "cycles_run": ["cyc1"], "topics_enqueued": ["topic"]}

        def _fake_tick(**kw):     return []
        def _fake_consume(**kw):  return []
        poll_result = SimpleNamespace(results=[], stop_reason="empty")
        def _fake_poll(**kw):     return poll_result

        with patch("run_worker._check_and_refill_queue", side_effect=_fake_refill), \
             patch("hypogeum.scheduler.tick",                   side_effect=_fake_tick), \
             patch("hypogeum.queue.consume_scheduled_runs",     side_effect=_fake_consume), \
             patch("hypogeum.queue.poll",                       side_effect=_fake_poll):
            result = run_iteration(
                worker_id="w1",
                db_path=tmp_path / "test.db",
                dry_run=False,
                channel_id=None,
                config=self._make_config(enabled=True),
            )

        assert "discovery" in result
        assert result["discovery"]["channels_refilled"] == 1

    def test_discovery_error_does_not_crash_worker(self, tmp_path):
        """An exception in _check_and_refill_queue must not propagate to run_iteration."""
        from hypogeum.db import initialize_schema
        from run_worker import run_iteration

        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        _seed_cleared_factory(db_path)

        def _fake_refill(**kw):
            raise RuntimeError("DB exploded")

        def _fake_tick(**kw):     return []
        def _fake_consume(**kw):  return []
        poll_result = SimpleNamespace(results=[], stop_reason="empty")
        def _fake_poll(**kw):     return poll_result

        with patch("run_worker._check_and_refill_queue", side_effect=_fake_refill), \
             patch("hypogeum.scheduler.tick",                   side_effect=_fake_tick), \
             patch("hypogeum.queue.consume_scheduled_runs",     side_effect=_fake_consume), \
             patch("hypogeum.queue.poll",                       side_effect=_fake_poll):
            # Must not raise
            result = run_iteration(
                worker_id="w1",
                db_path=db_path,
                dry_run=False,
                channel_id=None,
                config=self._make_config(enabled=True),
            )

        # Worker continues normally despite discovery error
        assert result["poll_status"] == "empty"


# ---------------------------------------------------------------------------
# Discovery scoring config — all 11 weights live in Config, not in source
# ---------------------------------------------------------------------------

_SCORING_VARS = [
    "OPENROUTER_API_KEY",
    "DISCOVERY_BASE_SCORE",
    "DISCOVERY_HOOK_LONG_WEIGHT",
    "DISCOVERY_HOOK_SHORT_WEIGHT",
    "DISCOVERY_HOOK_PRESENT_WEIGHT",
    "DISCOVERY_DIFF_LONG_WEIGHT",
    "DISCOVERY_DIFF_SHORT_WEIGHT",
    "DISCOVERY_NOVELTY_PENALTY",
    "DISCOVERY_SIGNAL_BOOST_TOP_PERFORMER",
    "DISCOVERY_SIGNAL_BOOST_HIGH_RETENTION",
    "DISCOVERY_SIGNAL_PENALTY_KEYWORD",
    "DISCOVERY_ANALYTICS_DELTA_CAP",
]


def _make_isolated_config(overrides: dict | None = None):
    """Instantiate Config in a clean env, applying any overrides."""
    import os
    from hypogeum.config import Config
    saved = {k: os.environ.get(k) for k in _SCORING_VARS}
    for k in _SCORING_VARS:
        os.environ.pop(k, None)
    os.environ["OPENROUTER_API_KEY"] = "test-key"
    if overrides:
        os.environ.update(overrides)
    try:
        cfg = Config()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return cfg


class TestDiscoveryScoringConfig:
    """Verify that all 11 v2 scoring weights are in Config with correct defaults."""

    _ALL_KEYS = [
        "DISCOVERY_BASE_SCORE",
        "DISCOVERY_HOOK_LONG_WEIGHT",
        "DISCOVERY_HOOK_SHORT_WEIGHT",
        "DISCOVERY_HOOK_PRESENT_WEIGHT",
        "DISCOVERY_DIFF_LONG_WEIGHT",
        "DISCOVERY_DIFF_SHORT_WEIGHT",
        "DISCOVERY_NOVELTY_PENALTY",
        "DISCOVERY_SIGNAL_BOOST_TOP_PERFORMER",
        "DISCOVERY_SIGNAL_BOOST_HIGH_RETENTION",
        "DISCOVERY_SIGNAL_PENALTY_KEYWORD",
        "DISCOVERY_ANALYTICS_DELTA_CAP",
    ]

    def test_config_has_all_11_scoring_keys(self):
        cfg = _make_isolated_config()
        for key in self._ALL_KEYS:
            assert hasattr(cfg, key), f"Config missing attribute {key}"

    def test_config_default_base_score(self):
        cfg = _make_isolated_config()
        assert cfg.DISCOVERY_BASE_SCORE == pytest.approx(0.50)

    def test_config_default_hook_long_weight(self):
        cfg = _make_isolated_config()
        assert cfg.DISCOVERY_HOOK_LONG_WEIGHT == pytest.approx(0.10)

    def test_config_default_hook_short_weight(self):
        cfg = _make_isolated_config()
        assert cfg.DISCOVERY_HOOK_SHORT_WEIGHT == pytest.approx(0.05)

    def test_config_default_hook_present_weight(self):
        cfg = _make_isolated_config()
        assert cfg.DISCOVERY_HOOK_PRESENT_WEIGHT == pytest.approx(0.02)

    def test_config_default_diff_long_weight(self):
        cfg = _make_isolated_config()
        assert cfg.DISCOVERY_DIFF_LONG_WEIGHT == pytest.approx(0.10)

    def test_config_default_diff_short_weight(self):
        cfg = _make_isolated_config()
        assert cfg.DISCOVERY_DIFF_SHORT_WEIGHT == pytest.approx(0.05)

    def test_config_default_novelty_penalty(self):
        cfg = _make_isolated_config()
        assert cfg.DISCOVERY_NOVELTY_PENALTY == pytest.approx(0.20)

    def test_config_default_signal_boost_top_performer(self):
        cfg = _make_isolated_config()
        assert cfg.DISCOVERY_SIGNAL_BOOST_TOP_PERFORMER == pytest.approx(0.12)

    def test_config_default_signal_boost_high_retention(self):
        cfg = _make_isolated_config()
        assert cfg.DISCOVERY_SIGNAL_BOOST_HIGH_RETENTION == pytest.approx(0.10)

    def test_config_default_signal_penalty_keyword(self):
        cfg = _make_isolated_config()
        assert cfg.DISCOVERY_SIGNAL_PENALTY_KEYWORD == pytest.approx(0.25)

    def test_config_default_analytics_delta_cap(self):
        cfg = _make_isolated_config()
        assert cfg.DISCOVERY_ANALYTICS_DELTA_CAP == pytest.approx(0.20)

    def test_env_var_overrides_initial_scorer_boost(self):
        """DISCOVERY_SIGNAL_BOOST_TOP_PERFORMER env var changes initial_scorer output."""
        import os
        from hypogeum.initial_scorer import compute_score, _analytics_delta
        candidate = {
            "topic_text": "Lost City of Helike",
            "canonical_subject": "lost_city_of_helike",
            "angle": "The mystery angle",
            "mystery_hook": "Why did an entire Greek city vanish beneath the waves?",
            "differentiation_claim": "First video to use 2001 sonar data",
        }
        ctx = {
            "has_real_analytics": True,
            "top_performer_keywords": {"helike", "lost"},
            "high_retention_keywords": set(),
            "penalty_keywords": set(),
        }
        old = os.environ.get("DISCOVERY_SIGNAL_BOOST_TOP_PERFORMER")
        try:
            os.environ["DISCOVERY_SIGNAL_BOOST_TOP_PERFORMER"] = "0.19"
            delta_high = _analytics_delta(candidate, ctx)
            os.environ["DISCOVERY_SIGNAL_BOOST_TOP_PERFORMER"] = "0.01"
            delta_low = _analytics_delta(candidate, ctx)
        finally:
            if old is None:
                os.environ.pop("DISCOVERY_SIGNAL_BOOST_TOP_PERFORMER", None)
            else:
                os.environ["DISCOVERY_SIGNAL_BOOST_TOP_PERFORMER"] = old
        assert delta_high > delta_low

    def test_env_var_overrides_initial_scorer_novelty(self):
        """DISCOVERY_NOVELTY_PENALTY env var changes compute_score output."""
        import os
        from hypogeum.initial_scorer import compute_score
        candidate = {
            "topic_text": "Lost City of Helike",
            "canonical_subject": "lost_city_of_helike",
            "mystery_hook": "Short",
            "differentiation_claim": "",
        }
        existing = {"lost_city_of_helike"}
        old = os.environ.get("DISCOVERY_NOVELTY_PENALTY")
        try:
            os.environ["DISCOVERY_NOVELTY_PENALTY"] = "0.05"
            score_light = compute_score(candidate, existing_topics=existing)
            os.environ["DISCOVERY_NOVELTY_PENALTY"] = "0.40"
            score_heavy = compute_score(candidate, existing_topics=existing)
        finally:
            if old is None:
                os.environ.pop("DISCOVERY_NOVELTY_PENALTY", None)
            else:
                os.environ["DISCOVERY_NOVELTY_PENALTY"] = old
        assert score_light > score_heavy
