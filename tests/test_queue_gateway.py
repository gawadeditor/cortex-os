"""
tests/test_queue_gateway.py — Unit tests for hypogeum/discovery/queue_gateway.py

Tests:
  PendingApproval    — auto_approve=False routes to pending_approval, no DB write
  AutoApprove        — auto_approve=True writes episode_topics
  PriorityDerivation — priority_score, final_composite, initial_composite fallbacks,
                       clamping to 0–10, missing score defaults to 5
  ErrorHandling      — duplicate, channel_not_found, unexpected exception
  V1Regression       — v1 discovery unaffected

All DB tests use real SQLite via initialize_schema + _ensure_tables.
No network calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hypogeum.discovery.queue_gateway import (
    route_selected_opportunity,
    _derive_priority,
)
from hypogeum.topic_discovery import _make_canonical_subject


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(auto_approve: bool = False):
    cfg = SimpleNamespace()
    cfg.AUTO_DISCOVERY_AUTO_APPROVE = auto_approve
    return cfg


def _opp(channel_id="ch_test", topic_text="Lost City of Helike", **kw):
    base = {
        "channel_id":        channel_id,
        "topic_text":        topic_text,
        "canonical_subject": _make_canonical_subject(topic_text),
        "initial_composite": 0.72,
    }
    base.update(kw)
    return base


def _now():
    return datetime.now(timezone.utc).isoformat()


def _init_db_with_channel(tmp_path: Path, channel_id: str = "ch_test") -> Path:
    """Initialize schema and insert a channel_profiles row for validate_channel."""
    from hypogeum.db import initialize_schema, get_connection
    from hypogeum.channel_config import _ensure_tables as _ensure_channel_tables

    db = tmp_path / "test.db"
    initialize_schema(db)

    conn = get_connection(db)
    try:
        _ensure_channel_tables(conn)
        conn.execute(
            "INSERT OR IGNORE INTO channel_profiles (channel_id, name, created_at) VALUES (?,?,?)",
            (channel_id, f"Channel {channel_id}", _now()),
        )
        conn.commit()
    finally:
        conn.close()

    return db


# ---------------------------------------------------------------------------
# 1. Pending approval (auto_approve=False)
# ---------------------------------------------------------------------------

class TestPendingApproval:

    def test_returns_ok_true(self):
        result = route_selected_opportunity(_opp(), _cfg(auto_approve=False))
        assert result["ok"] is True

    def test_routed_to_pending_approval(self):
        result = route_selected_opportunity(_opp(), _cfg(auto_approve=False))
        assert result["routed_to"] == "pending_approval"

    def test_topic_id_is_none(self):
        result = route_selected_opportunity(_opp(), _cfg(auto_approve=False))
        assert result["topic_id"] is None

    def test_episode_topics_not_written(self, tmp_path):
        db = _init_db_with_channel(tmp_path)
        route_selected_opportunity(_opp(), _cfg(auto_approve=False), db_path=db)

        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM episode_topics").fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_no_db_access_needed_when_not_auto_approving(self):
        """No DB path needed when auto_approve=False — function must return immediately."""
        result = route_selected_opportunity(_opp(), _cfg(auto_approve=False), db_path=None)
        assert result["routed_to"] == "pending_approval"

    def test_queue_priority_not_in_result(self):
        result = route_selected_opportunity(_opp(), _cfg(auto_approve=False))
        assert "queue_priority" not in result


# ---------------------------------------------------------------------------
# 2. Auto approve (auto_approve=True)
# ---------------------------------------------------------------------------

class TestAutoApprove:

    def test_returns_ok_true(self, tmp_path):
        db = _init_db_with_channel(tmp_path)
        result = route_selected_opportunity(_opp(), _cfg(auto_approve=True), db_path=db)
        assert result["ok"] is True

    def test_routed_to_episode_topics(self, tmp_path):
        db = _init_db_with_channel(tmp_path)
        result = route_selected_opportunity(_opp(), _cfg(auto_approve=True), db_path=db)
        assert result["routed_to"] == "episode_topics"

    def test_topic_id_is_string(self, tmp_path):
        db = _init_db_with_channel(tmp_path)
        result = route_selected_opportunity(_opp(), _cfg(auto_approve=True), db_path=db)
        assert isinstance(result["topic_id"], str)
        assert result["topic_id"]

    def test_row_written_to_episode_topics(self, tmp_path):
        db = _init_db_with_channel(tmp_path)
        opp = _opp(topic_text="The Hidden Tunnels of Ancient Rome")
        route_selected_opportunity(opp, _cfg(auto_approve=True), db_path=db)

        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT topic_text, channel_id, status FROM episode_topics"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "The Hidden Tunnels of Ancient Rome"
        assert row[1] == "ch_test"
        assert row[2] == "queued"

    def test_queue_priority_in_result(self, tmp_path):
        db = _init_db_with_channel(tmp_path)
        result = route_selected_opportunity(_opp(), _cfg(auto_approve=True), db_path=db)
        assert "queue_priority" in result
        assert isinstance(result["queue_priority"], int)

    def test_queue_priority_clamped_0_10(self, tmp_path):
        db = _init_db_with_channel(tmp_path)
        result = route_selected_opportunity(
            _opp(initial_composite=0.72),
            _cfg(auto_approve=True), db_path=db,
        )
        assert 0 <= result["queue_priority"] <= 10


# ---------------------------------------------------------------------------
# 3. Priority derivation
# ---------------------------------------------------------------------------

class TestPriorityDerivation:

    # _derive_priority unit tests (no DB needed)

    def test_priority_score_used_first(self):
        opp = _opp(priority_score=0.80, final_composite=0.50, initial_composite=0.30)
        assert _derive_priority(opp) == round(0.80 * 10)

    def test_final_composite_fallback(self):
        opp = _opp(final_composite=0.60, initial_composite=0.30)
        # priority_score absent
        assert _derive_priority(opp) == round(0.60 * 10)

    def test_initial_composite_fallback(self):
        opp = _opp(initial_composite=0.50)
        assert _derive_priority(opp) == round(0.50 * 10)

    def test_missing_all_scores_defaults_to_5(self):
        opp = {"channel_id": "ch", "topic_text": "Topic"}
        assert _derive_priority(opp) == 5

    def test_none_score_falls_through_to_next(self):
        opp = _opp(priority_score=None, final_composite=0.40, initial_composite=0.20)
        assert _derive_priority(opp) == round(0.40 * 10)

    def test_score_1_0_maps_to_10(self):
        assert _derive_priority(_opp(initial_composite=1.0)) == 10

    def test_score_0_0_maps_to_0(self):
        assert _derive_priority(_opp(initial_composite=0.0)) == 0

    def test_score_above_1_clamped_to_10(self):
        assert _derive_priority(_opp(initial_composite=1.5)) == 10

    def test_score_below_0_clamped_to_0(self):
        assert _derive_priority(_opp(initial_composite=-0.5)) == 0

    def test_bad_string_score_skipped(self):
        opp = _opp(priority_score="not-a-float", initial_composite=0.30)
        assert _derive_priority(opp) == round(0.30 * 10)

    def test_priority_score_written_to_db(self, tmp_path):
        """queue_priority in result must match the derived value."""
        db = _init_db_with_channel(tmp_path)
        opp = _opp(priority_score=0.70)
        result = route_selected_opportunity(opp, _cfg(auto_approve=True), db_path=db)
        assert result["queue_priority"] == round(0.70 * 10)

    def test_initial_composite_fallback_in_db(self, tmp_path):
        db = _init_db_with_channel(tmp_path)
        opp = _opp(initial_composite=0.50)
        result = route_selected_opportunity(opp, _cfg(auto_approve=True), db_path=db)
        assert result["queue_priority"] == round(0.50 * 10)

    def test_missing_score_medium_priority_in_db(self, tmp_path):
        db = _init_db_with_channel(tmp_path)
        opp = {"channel_id": "ch_test", "topic_text": "Mysterious Roman Tunnels"}
        result = route_selected_opportunity(opp, _cfg(auto_approve=True), db_path=db)
        assert result["queue_priority"] == 5


# ---------------------------------------------------------------------------
# 4. Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:

    def test_duplicate_returns_ok_false(self, tmp_path):
        db = _init_db_with_channel(tmp_path)
        opp = _opp(topic_text="Ancient Roman Aqueducts")
        # First enqueue
        route_selected_opportunity(opp, _cfg(auto_approve=True), db_path=db)
        # Second enqueue — same topic, same channel → duplicate
        result = route_selected_opportunity(opp, _cfg(auto_approve=True), db_path=db)
        assert result["ok"] is False
        assert result["failure_reason"] == "duplicate"

    def test_channel_not_found_returns_ok_false(self, tmp_path):
        db = _init_db_with_channel(tmp_path, channel_id="ch_other")
        # Use a channel_id that is NOT in channel_profiles
        opp = _opp(channel_id="ch_missing")
        result = route_selected_opportunity(opp, _cfg(auto_approve=True), db_path=db)
        assert result["ok"] is False
        assert result["failure_reason"] == "channel_not_found"

    def test_unexpected_exception_returns_ok_false(self):
        from hypogeum.topics import enqueue_topic
        with patch(
            "hypogeum.discovery.queue_gateway.enqueue_topic",
            side_effect=RuntimeError("unexpected DB corruption"),
        ):
            result = route_selected_opportunity(_opp(), _cfg(auto_approve=True))
        assert result["ok"] is False
        assert result["failure_reason"] == "route_failed"

    def test_unexpected_exception_does_not_raise(self):
        with patch(
            "hypogeum.discovery.queue_gateway.enqueue_topic",
            side_effect=Exception("anything"),
        ):
            result = route_selected_opportunity(_opp(), _cfg(auto_approve=True))
        assert "failure_reason" in result

    def test_duplicate_does_not_write_second_row(self, tmp_path):
        db = _init_db_with_channel(tmp_path)
        opp = _opp(topic_text="Helike Underwater City")
        route_selected_opportunity(opp, _cfg(auto_approve=True), db_path=db)
        route_selected_opportunity(opp, _cfg(auto_approve=True), db_path=db)

        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM episode_topics").fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_channel_not_found_does_not_raise(self, tmp_path):
        db = _init_db_with_channel(tmp_path, channel_id="ch_real")
        opp = _opp(channel_id="ch_ghost")
        result = route_selected_opportunity(opp, _cfg(auto_approve=True), db_path=db)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# 5. V1 regression
# ---------------------------------------------------------------------------

class TestV1Regression:

    def test_v1_score_seed_unaffected(self):
        from hypogeum.topic_discovery import _score_seed
        seed = {
            "topic": "The hidden history of ancient underground cities",
            "source": "heuristic",
            "ctr_rationale": "", "retention_rationale": "",
            "watch_time_rationale": "", "risk": "",
        }
        assert _score_seed(seed, set(), None) == 0.82

    def test_v1_run_discovery_cycle_produces_candidates(self, tmp_path):
        import os
        from hypogeum.db import initialize_schema
        from hypogeum.topic_discovery import run_discovery_cycle
        db = tmp_path / "v1.db"
        initialize_schema(db)
        old = os.environ.get("DISCOVERY_ENGINE")
        try:
            os.environ.pop("DISCOVERY_ENGINE", None)
            result = run_discovery_cycle("ch_v1_gw", db_path=db)
        finally:
            if old is None:
                os.environ.pop("DISCOVERY_ENGINE", None)
            else:
                os.environ["DISCOVERY_ENGINE"] = old
        assert result["ok"] is True
        assert result["candidate_count"] == 7

    def test_queue_gateway_import_does_not_affect_v1(self):
        from hypogeum.topic_discovery import _score_seed
        from hypogeum.discovery import queue_gateway  # noqa: F401
        seed = {
            "topic": "Secret societies and the empires they shaped",
            "source": "heuristic",
            "ctr_rationale": "", "retention_rationale": "",
            "watch_time_rationale": "", "risk": "",
        }
        assert _score_seed(seed, set(), None) == 0.64
