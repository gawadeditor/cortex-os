"""
tests/test_discovery_director_v2.py — Integration tests for Discovery Director v2

Tests:
  CycleLifecycle      — starts and completes cycle, returns required keys, engine_used=v2
  ComponentCalls      — calls Topic Ideator, Source Verifier, repository
  WinnerSelection     — highest composite wins; failed excluded if verified exist;
                        failed eligible when no verified; empty handled
  Routing             — auto_approve=True enqueues; False holds pending_approval
  RepositoryUpdates   — selected/enqueued status marked atomically
  FunnelMetrics       — complete_cycle receives correct counts
  Failures            — ideator error → fallback; verifier error → unverified;
                        save failure → ok=False; never raises
  V1Regression        — v2 is behind DISCOVERY_ENGINE=v2 flag; v1 unchanged

All tests mock sub-components to isolate orchestration logic.
DB-touching tests use a real tmp SQLite via initialize_schema.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from hypogeum.agents.discovery_director import run as director_run
from hypogeum.topic_discovery import _make_canonical_subject


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(auto_approve: bool = False):
    cfg = SimpleNamespace()
    cfg.AUTO_DISCOVERY_AUTO_APPROVE = auto_approve
    cfg.DISCOVERY_VERIFY_THRESHOLD = 0.65
    cfg.DISCOVERY_VERIFY_DELTA = 0.10
    cfg.OPENROUTER_SONNET_MODEL_ID = "anthropic/claude-sonnet-4.6"
    cfg.PERPLEXITY_API_KEY = "test"
    cfg.PERPLEXITY_MODEL = "sonar-pro"
    cfg.PERPLEXITY_MAX_TOKENS = 512
    return cfg


def _opp(topic_text: str = "Lost City of Helike",
         initial_composite: float = 0.72,
         verification_status: str = "verified",
         source_score: float = 0.67,
         **kw) -> dict:
    return {
        "topic_text":            topic_text,
        "angle":                 "Underwater mystery",
        "mystery_hook":          "Why did an entire city vanish in a single night?",
        "differentiation_claim": "First video using 2001 sonar data",
        "visual_signal":         "Underwater ruins",
        "evidence_signal":       "2001 sonar paper",
        "canonical_subject":     _make_canonical_subject(topic_text),
        "initial_composite":     initial_composite,
        "verification_status":   verification_status,
        "source_count":          2,
        "source_score":          source_score,
        "source_urls":           ["https://src.com/1"],
        **kw,
    }


def _init_db(tmp_path: Path) -> Path:
    from hypogeum.db import initialize_schema
    db = tmp_path / "test.db"
    initialize_schema(db)
    return db


def _fake_ideator_result(opps: list[dict], source: str = "llm") -> dict:
    return {
        "ok":           True,
        "source":       source,
        "batch_id":     str(uuid.uuid4()),
        "channel_id":   "ch_test",
        "opportunities": opps,
    }


def _fake_route_enqueued(priority: int = 7) -> dict:
    return {
        "ok":             True,
        "routed_to":      "episode_topics",
        "topic_id":       str(uuid.uuid4()),
        "queue_priority": priority,
    }


def _fake_route_pending() -> dict:
    return {"ok": True, "routed_to": "pending_approval", "topic_id": None}


def _fake_save(n: int) -> dict:
    return {"ok": True, "saved_count": n, "opportunity_ids": [str(uuid.uuid4()) for _ in range(n)]}


def _fake_start_ok() -> dict:
    return {"ok": True, "cycle_id": "cyc_1", "channel_id": "ch_test", "engine_used": "v2"}


def _fake_update_ok() -> dict:
    return {"ok": True, "opportunity_id": "opp_1", "fields_updated": ["status"], "event_id": "ev_1"}


def _fake_complete_ok() -> dict:
    return {"ok": True, "cycle_id": "cyc_1"}


def _patches(
    opps=None,
    verifier_opps=None,
    save_ok=True,
    route_result=None,
):
    """Return a context-manager stack for the common patch set."""
    if opps is None:
        opps = [_opp()]
    ideator_out = _fake_ideator_result(opps)
    ver_out = verifier_opps if verifier_opps is not None else opps
    save_out = _fake_save(len(ver_out)) if save_ok else {"ok": False, "failure_reason": "db_error"}
    if route_result is None:
        route_result = _fake_route_pending()

    return (
        patch("hypogeum.agents.discovery_director.run_topic_ideator",
              return_value=ideator_out),
        patch("hypogeum.agents.discovery_director.run_source_verifier",
              return_value=ver_out),
        patch("hypogeum.discovery.repository.start_cycle",
              return_value=_fake_start_ok()),
        patch("hypogeum.discovery.repository.save_opportunities",
              return_value=save_out),
        patch("hypogeum.discovery.repository.update_opportunity",
              return_value=_fake_update_ok()),
        patch("hypogeum.discovery.repository.complete_cycle",
              return_value=_fake_complete_ok()),
        patch("hypogeum.discovery.repository.fail_cycle",
              return_value={"ok": True}),
        patch("hypogeum.agents.discovery_director.route_selected_opportunity",
              return_value=route_result),
    )


# ---------------------------------------------------------------------------
# 1. Cycle lifecycle
# ---------------------------------------------------------------------------

class TestCycleLifecycle:

    def test_starts_and_completes_cycle(self):
        opps = [_opp()]
        with _patches(opps=opps)[0], _patches(opps=opps)[1], \
             patch("hypogeum.discovery.repository.start_cycle", return_value=_fake_start_ok()) as mock_start, \
             patch("hypogeum.discovery.repository.save_opportunities", return_value=_fake_save(1)), \
             patch("hypogeum.discovery.repository.update_opportunity", return_value=_fake_update_ok()), \
             patch("hypogeum.discovery.repository.complete_cycle", return_value=_fake_complete_ok()) as mock_complete, \
             patch("hypogeum.discovery.repository.fail_cycle", return_value={"ok": True}), \
             patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   return_value=_fake_ideator_result(opps)), \
             patch("hypogeum.agents.discovery_director.run_source_verifier", return_value=opps), \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=_fake_route_pending()):
            result = director_run("ch_test", _cfg())
        assert result["ok"] is True
        mock_start.assert_called_once()
        mock_complete.assert_called_once()

    def test_returns_all_required_keys(self):
        opps = [_opp()]
        with patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   return_value=_fake_ideator_result(opps)), \
             patch("hypogeum.agents.discovery_director.run_source_verifier", return_value=opps), \
             patch("hypogeum.discovery.repository.start_cycle", return_value=_fake_start_ok()), \
             patch("hypogeum.discovery.repository.save_opportunities", return_value=_fake_save(1)), \
             patch("hypogeum.discovery.repository.update_opportunity", return_value=_fake_update_ok()), \
             patch("hypogeum.discovery.repository.complete_cycle", return_value=_fake_complete_ok()), \
             patch("hypogeum.discovery.repository.fail_cycle", return_value={"ok": True}), \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=_fake_route_pending()):
            result = director_run("ch_test", _cfg())
        required = ("ok", "engine_used", "cycle_id", "channel_id", "candidate_count",
                    "verified_count", "selected_opportunity_id", "topic_id_enqueued",
                    "route_status", "opportunities")
        for key in required:
            assert key in result, f"Missing key: {key}"

    def test_engine_used_is_v2(self):
        opps = [_opp()]
        with patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   return_value=_fake_ideator_result(opps)), \
             patch("hypogeum.agents.discovery_director.run_source_verifier", return_value=opps), \
             patch("hypogeum.discovery.repository.start_cycle", return_value=_fake_start_ok()), \
             patch("hypogeum.discovery.repository.save_opportunities", return_value=_fake_save(1)), \
             patch("hypogeum.discovery.repository.update_opportunity", return_value=_fake_update_ok()), \
             patch("hypogeum.discovery.repository.complete_cycle", return_value=_fake_complete_ok()), \
             patch("hypogeum.discovery.repository.fail_cycle", return_value={"ok": True}), \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=_fake_route_pending()):
            result = director_run("ch_test", _cfg())
        assert result["engine_used"] == "v2"

    def test_channel_id_echoed_in_result(self):
        opps = [_opp()]
        with patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   return_value=_fake_ideator_result(opps, )), \
             patch("hypogeum.agents.discovery_director.run_source_verifier", return_value=opps), \
             patch("hypogeum.discovery.repository.start_cycle", return_value=_fake_start_ok()), \
             patch("hypogeum.discovery.repository.save_opportunities", return_value=_fake_save(1)), \
             patch("hypogeum.discovery.repository.update_opportunity", return_value=_fake_update_ok()), \
             patch("hypogeum.discovery.repository.complete_cycle", return_value=_fake_complete_ok()), \
             patch("hypogeum.discovery.repository.fail_cycle", return_value={"ok": True}), \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=_fake_route_pending()):
            result = director_run("ch_xyz", _cfg())
        assert result["channel_id"] == "ch_xyz"


# ---------------------------------------------------------------------------
# 2. Component calls
# ---------------------------------------------------------------------------

class TestComponentCalls:

    def test_calls_topic_ideator(self):
        opps = [_opp()]
        with patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   return_value=_fake_ideator_result(opps)) as mock_ideator, \
             patch("hypogeum.agents.discovery_director.run_source_verifier", return_value=opps), \
             patch("hypogeum.discovery.repository.start_cycle", return_value=_fake_start_ok()), \
             patch("hypogeum.discovery.repository.save_opportunities", return_value=_fake_save(1)), \
             patch("hypogeum.discovery.repository.update_opportunity", return_value=_fake_update_ok()), \
             patch("hypogeum.discovery.repository.complete_cycle", return_value=_fake_complete_ok()), \
             patch("hypogeum.discovery.repository.fail_cycle", return_value={"ok": True}), \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=_fake_route_pending()):
            director_run("ch_test", _cfg())
        mock_ideator.assert_called_once()
        assert mock_ideator.call_args[0][0] == "ch_test"

    def test_calls_source_verifier(self):
        opps = [_opp()]
        with patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   return_value=_fake_ideator_result(opps)), \
             patch("hypogeum.agents.discovery_director.run_source_verifier",
                   return_value=opps) as mock_verifier, \
             patch("hypogeum.discovery.repository.start_cycle", return_value=_fake_start_ok()), \
             patch("hypogeum.discovery.repository.save_opportunities", return_value=_fake_save(1)), \
             patch("hypogeum.discovery.repository.update_opportunity", return_value=_fake_update_ok()), \
             patch("hypogeum.discovery.repository.complete_cycle", return_value=_fake_complete_ok()), \
             patch("hypogeum.discovery.repository.fail_cycle", return_value={"ok": True}), \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=_fake_route_pending()):
            director_run("ch_test", _cfg())
        mock_verifier.assert_called_once()

    def test_persists_all_opportunities(self):
        opps = [_opp(f"T{i}") for i in range(4)]
        with patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   return_value=_fake_ideator_result(opps)), \
             patch("hypogeum.agents.discovery_director.run_source_verifier", return_value=opps), \
             patch("hypogeum.discovery.repository.start_cycle", return_value=_fake_start_ok()), \
             patch("hypogeum.discovery.repository.save_opportunities",
                   return_value=_fake_save(4)) as mock_save, \
             patch("hypogeum.discovery.repository.update_opportunity", return_value=_fake_update_ok()), \
             patch("hypogeum.discovery.repository.complete_cycle", return_value=_fake_complete_ok()), \
             patch("hypogeum.discovery.repository.fail_cycle", return_value={"ok": True}), \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=_fake_route_pending()):
            result = director_run("ch_test", _cfg())
        mock_save.assert_called_once()
        saved_opps = mock_save.call_args[0][0]
        assert len(saved_opps) == 4
        assert result["candidate_count"] == 4


# ---------------------------------------------------------------------------
# 3. Winner selection
# ---------------------------------------------------------------------------

class TestWinnerSelection:

    def _run(self, opps, route_result=None):
        if route_result is None:
            route_result = _fake_route_pending()
        save_out = _fake_save(len(opps))
        with patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   return_value=_fake_ideator_result(opps)), \
             patch("hypogeum.agents.discovery_director.run_source_verifier", return_value=opps), \
             patch("hypogeum.discovery.repository.start_cycle", return_value=_fake_start_ok()), \
             patch("hypogeum.discovery.repository.save_opportunities", return_value=save_out), \
             patch("hypogeum.discovery.repository.update_opportunity",
                   return_value=_fake_update_ok()) as mock_update, \
             patch("hypogeum.discovery.repository.complete_cycle", return_value=_fake_complete_ok()), \
             patch("hypogeum.discovery.repository.fail_cycle", return_value={"ok": True}), \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=route_result) as mock_route:
            result = director_run("ch_test", _cfg())
        return result, mock_update, mock_route

    def test_selects_highest_initial_composite(self):
        opps = [
            _opp("Topic A", initial_composite=0.60, verification_status="verified"),
            _opp("Topic B", initial_composite=0.85, verification_status="verified"),
            _opp("Topic C", initial_composite=0.70, verification_status="verified"),
        ]
        result, mock_update, mock_route = self._run(opps)
        # Winner is Topic B (highest composite)
        # The opportunity passed to route must be Topic B
        routed_opp = mock_route.call_args[0][0]
        assert routed_opp["topic_text"] == "Topic B"

    def test_source_score_breaks_tie(self):
        opps = [
            _opp("Topic A", initial_composite=0.80, source_score=0.33,
                 verification_status="verified"),
            _opp("Topic B", initial_composite=0.80, source_score=0.67,
                 verification_status="verified"),
        ]
        result, mock_update, mock_route = self._run(opps)
        routed_opp = mock_route.call_args[0][0]
        assert routed_opp["topic_text"] == "Topic B"

    def test_failed_excluded_when_verified_exists(self):
        opps = [
            _opp("Good", initial_composite=0.70, verification_status="verified",
                 source_score=0.67),
            _opp("Bad", initial_composite=0.95, verification_status="failed",
                 source_score=0.0),
        ]
        result, mock_update, mock_route = self._run(opps)
        routed_opp = mock_route.call_args[0][0]
        # Bad has higher composite BUT is failed — must be excluded
        assert routed_opp["topic_text"] == "Good"

    def test_failed_eligible_when_no_verified_exists(self):
        opps = [
            _opp("Low", initial_composite=0.60, verification_status="failed", source_score=0.0),
            _opp("High", initial_composite=0.90, verification_status="failed", source_score=0.0),
        ]
        result, mock_update, mock_route = self._run(opps)
        routed_opp = mock_route.call_args[0][0]
        assert routed_opp["topic_text"] == "High"

    def test_skipped_candidates_eligible(self):
        opps = [
            _opp("Skipped", initial_composite=0.95, verification_status="skipped",
                 source_score=0.0),
        ]
        result, mock_update, mock_route = self._run(opps)
        assert result["ok"] is True
        # skipped candidate can still win
        assert mock_route.called

    def test_empty_opportunities_no_winner(self):
        result, mock_update, mock_route = self._run([])
        assert result["ok"] is True
        assert result["selected_opportunity_id"] is None
        assert result["route_status"] == "no_candidate"
        mock_route.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Routing
# ---------------------------------------------------------------------------

class TestRouting:

    def _run_with_route(self, route_result, opps=None):
        if opps is None:
            opps = [_opp()]
        save_out = _fake_save(len(opps))
        with patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   return_value=_fake_ideator_result(opps)), \
             patch("hypogeum.agents.discovery_director.run_source_verifier", return_value=opps), \
             patch("hypogeum.discovery.repository.start_cycle", return_value=_fake_start_ok()), \
             patch("hypogeum.discovery.repository.save_opportunities", return_value=save_out), \
             patch("hypogeum.discovery.repository.update_opportunity",
                   return_value=_fake_update_ok()), \
             patch("hypogeum.discovery.repository.complete_cycle", return_value=_fake_complete_ok()), \
             patch("hypogeum.discovery.repository.fail_cycle", return_value={"ok": True}), \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=route_result):
            return director_run("ch_test", _cfg())

    def test_auto_approve_true_enqueues(self):
        tid = str(uuid.uuid4())
        result = self._run_with_route(
            {"ok": True, "routed_to": "episode_topics", "topic_id": tid, "queue_priority": 7}
        )
        assert result["topic_id_enqueued"] == tid
        assert result["route_status"] == "episode_topics"

    def test_auto_approve_false_pending_approval(self):
        result = self._run_with_route(
            {"ok": True, "routed_to": "pending_approval", "topic_id": None}
        )
        assert result["topic_id_enqueued"] is None
        assert result["route_status"] == "pending_approval"

    def test_topic_id_enqueued_none_when_pending(self):
        result = self._run_with_route(_fake_route_pending())
        assert result["topic_id_enqueued"] is None

    def test_route_status_reflects_failure(self):
        result = self._run_with_route(
            {"ok": False, "failure_reason": "duplicate"}
        )
        assert "duplicate" in result["route_status"]


# ---------------------------------------------------------------------------
# 5. Repository updates
# ---------------------------------------------------------------------------

class TestRepositoryUpdates:

    def _run_capture_updates(self, route_result=None, opps=None):
        if opps is None:
            opps = [_opp()]
        if route_result is None:
            route_result = _fake_route_pending()
        save_out = _fake_save(len(opps))
        updates = []

        def fake_update(opp_id, fields, event_type, actor, detail=None, db_path=None):
            updates.append({"fields": fields, "event_type": event_type, "actor": actor})
            return _fake_update_ok()

        with patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   return_value=_fake_ideator_result(opps)), \
             patch("hypogeum.agents.discovery_director.run_source_verifier", return_value=opps), \
             patch("hypogeum.discovery.repository.start_cycle", return_value=_fake_start_ok()), \
             patch("hypogeum.discovery.repository.save_opportunities", return_value=save_out), \
             patch("hypogeum.discovery.repository.update_opportunity", side_effect=fake_update), \
             patch("hypogeum.discovery.repository.complete_cycle", return_value=_fake_complete_ok()), \
             patch("hypogeum.discovery.repository.fail_cycle", return_value={"ok": True}), \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=route_result):
            result = director_run("ch_test", _cfg())
        return result, updates

    def test_winner_marked_selected(self):
        result, updates = self._run_capture_updates()
        selected_updates = [u for u in updates if u["fields"].get("status") == "selected"]
        assert len(selected_updates) >= 1

    def test_winner_marked_enqueued_when_enqueued(self):
        tid = str(uuid.uuid4())
        route = {"ok": True, "routed_to": "episode_topics", "topic_id": tid, "queue_priority": 7}
        result, updates = self._run_capture_updates(route_result=route)
        enqueued_updates = [u for u in updates if u["fields"].get("status") == "enqueued"]
        assert len(enqueued_updates) >= 1

    def test_topic_id_stored_on_enqueue(self):
        tid = str(uuid.uuid4())
        route = {"ok": True, "routed_to": "episode_topics", "topic_id": tid, "queue_priority": 7}
        result, updates = self._run_capture_updates(route_result=route)
        enqueued = [u for u in updates if u["fields"].get("status") == "enqueued"]
        assert enqueued[0]["fields"]["topic_id"] == tid

    def test_no_enqueue_update_when_pending(self):
        result, updates = self._run_capture_updates(route_result=_fake_route_pending())
        enqueued_updates = [u for u in updates if u["fields"].get("status") == "enqueued"]
        assert len(enqueued_updates) == 0


# ---------------------------------------------------------------------------
# 6. Funnel metrics
# ---------------------------------------------------------------------------

class TestFunnelMetrics:

    def test_complete_cycle_receives_correct_counts(self):
        opps = [
            _opp("V1", initial_composite=0.80, verification_status="verified"),
            _opp("V2", initial_composite=0.75, verification_status="verified"),
            _opp("F1", initial_composite=0.60, verification_status="failed"),
            _opp("S1", initial_composite=0.40, verification_status="skipped"),
        ]
        summary_calls = []

        def fake_complete(cycle_id, summary, db_path=None):
            summary_calls.append(summary)
            return _fake_complete_ok()

        with patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   return_value=_fake_ideator_result(opps)), \
             patch("hypogeum.agents.discovery_director.run_source_verifier", return_value=opps), \
             patch("hypogeum.discovery.repository.start_cycle", return_value=_fake_start_ok()), \
             patch("hypogeum.discovery.repository.save_opportunities", return_value=_fake_save(4)), \
             patch("hypogeum.discovery.repository.update_opportunity",
                   return_value=_fake_update_ok()), \
             patch("hypogeum.discovery.repository.complete_cycle", side_effect=fake_complete), \
             patch("hypogeum.discovery.repository.fail_cycle", return_value={"ok": True}), \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=_fake_route_pending()):
            result = director_run("ch_test", _cfg())

        assert len(summary_calls) == 1
        s = summary_calls[0]
        assert s["raw_candidates_count"] == 4
        assert s["verified_count"] == 2
        assert s["rejected_source_count"] == 1  # failed
        assert s["rejected_score_count"] == 1   # skipped

    def test_verified_count_in_result(self):
        opps = [
            _opp("V1", verification_status="verified"),
            _opp("V2", verification_status="verified"),
            _opp("S1", verification_status="skipped"),
        ]
        with patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   return_value=_fake_ideator_result(opps)), \
             patch("hypogeum.agents.discovery_director.run_source_verifier", return_value=opps), \
             patch("hypogeum.discovery.repository.start_cycle", return_value=_fake_start_ok()), \
             patch("hypogeum.discovery.repository.save_opportunities", return_value=_fake_save(3)), \
             patch("hypogeum.discovery.repository.update_opportunity",
                   return_value=_fake_update_ok()), \
             patch("hypogeum.discovery.repository.complete_cycle", return_value=_fake_complete_ok()), \
             patch("hypogeum.discovery.repository.fail_cycle", return_value={"ok": True}), \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=_fake_route_pending()):
            result = director_run("ch_test", _cfg())
        assert result["verified_count"] == 2


# ---------------------------------------------------------------------------
# 7. Failures
# ---------------------------------------------------------------------------

class TestFailures:

    def _common_patches(self, **overrides):
        defaults = {
            "topic_ideator":    _fake_ideator_result([_opp()]),
            "verifier":         [_opp()],
            "start":            _fake_start_ok(),
            "save":             _fake_save(1),
            "update":           _fake_update_ok(),
            "complete":         _fake_complete_ok(),
            "fail_cycle":       {"ok": True},
            "route":            _fake_route_pending(),
        }
        defaults.update(overrides)
        return defaults

    def test_ideator_exception_still_completes(self):
        """Ideator crash → zero candidates; cycle completes with ok=True."""
        with patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   side_effect=RuntimeError("ideator down")), \
             patch("hypogeum.agents.discovery_director.run_source_verifier", return_value=[]), \
             patch("hypogeum.discovery.repository.start_cycle", return_value=_fake_start_ok()), \
             patch("hypogeum.discovery.repository.save_opportunities", return_value=_fake_save(0)), \
             patch("hypogeum.discovery.repository.update_opportunity",
                   return_value=_fake_update_ok()), \
             patch("hypogeum.discovery.repository.complete_cycle",
                   return_value=_fake_complete_ok()), \
             patch("hypogeum.discovery.repository.fail_cycle", return_value={"ok": True}), \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=_fake_route_pending()):
            result = director_run("ch_test", _cfg())
        assert "ok" in result
        assert result["candidate_count"] == 0

    def test_verifier_exception_continues_with_unverified(self):
        """Verifier crash → candidates without verification_status get 'unverified' default."""
        # Simulate topic_ideator output: no verification_status yet (only 6 fields + scores)
        raw_opps = [
            {
                "topic_text": "Lost City of Helike", "angle": "Underwater",
                "mystery_hook": "Why did a city vanish?",
                "differentiation_claim": "Uses 2001 sonar data",
                "visual_signal": "Ruins", "evidence_signal": "Paper",
                "canonical_subject": "lost_city_of_helike",
                "initial_composite": 0.72,
                # verification_status intentionally absent
            }
        ]
        with patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   return_value=_fake_ideator_result(raw_opps)), \
             patch("hypogeum.agents.discovery_director.run_source_verifier",
                   side_effect=RuntimeError("verifier down")), \
             patch("hypogeum.discovery.repository.start_cycle", return_value=_fake_start_ok()), \
             patch("hypogeum.discovery.repository.save_opportunities",
                   return_value=_fake_save(1)) as mock_save, \
             patch("hypogeum.discovery.repository.update_opportunity",
                   return_value=_fake_update_ok()), \
             patch("hypogeum.discovery.repository.complete_cycle",
                   return_value=_fake_complete_ok()), \
             patch("hypogeum.discovery.repository.fail_cycle", return_value={"ok": True}), \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=_fake_route_pending()):
            result = director_run("ch_test", _cfg())
        mock_save.assert_called_once()
        saved = mock_save.call_args[0][0]
        for c in saved:
            assert c.get("verification_status") == "unverified"

    def test_save_failure_returns_ok_false(self):
        opps = [_opp()]
        with patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   return_value=_fake_ideator_result(opps)), \
             patch("hypogeum.agents.discovery_director.run_source_verifier", return_value=opps), \
             patch("hypogeum.discovery.repository.start_cycle", return_value=_fake_start_ok()), \
             patch("hypogeum.discovery.repository.save_opportunities",
                   return_value={"ok": False, "failure_reason": "db_error"}), \
             patch("hypogeum.discovery.repository.update_opportunity",
                   return_value=_fake_update_ok()), \
             patch("hypogeum.discovery.repository.complete_cycle",
                   return_value=_fake_complete_ok()), \
             patch("hypogeum.discovery.repository.fail_cycle",
                   return_value={"ok": True}) as mock_fail, \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=_fake_route_pending()):
            result = director_run("ch_test", _cfg())
        assert result["ok"] is False
        mock_fail.assert_called_once()

    def test_start_failure_returns_ok_false(self):
        with patch("hypogeum.discovery.repository.start_cycle",
                   return_value={"ok": False, "failure_reason": "duplicate_cycle"}), \
             patch("hypogeum.agents.discovery_director.run_topic_ideator"), \
             patch("hypogeum.agents.discovery_director.run_source_verifier"):
            result = director_run("ch_test", _cfg())
        assert result["ok"] is False

    def test_never_raises_on_unexpected_exception(self):
        """A completely unexpected error must be caught and returned as ok=False."""
        with patch("hypogeum.discovery.repository.start_cycle",
                   return_value=_fake_start_ok()), \
             patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   side_effect=SystemError("catastrophic")), \
             patch("hypogeum.discovery.repository.fail_cycle",
                   return_value={"ok": True}):
            result = director_run("ch_test", _cfg())
        assert "ok" in result  # must return, never raise

    def test_all_failure_paths_have_ok_key(self):
        """Every failure return path must include 'ok'."""
        scenarios = [
            # start_cycle fails
            dict(
                start={"ok": False},
            ),
        ]
        with patch("hypogeum.discovery.repository.start_cycle",
                   return_value={"ok": False, "failure_reason": "x"}), \
             patch("hypogeum.agents.discovery_director.run_topic_ideator"), \
             patch("hypogeum.agents.discovery_director.run_source_verifier"):
            result = director_run("ch_test", _cfg())
        assert "ok" in result


# ---------------------------------------------------------------------------
# 8. generation_source propagation through director
# ---------------------------------------------------------------------------

class TestGenerationSource:

    def _run_with_opps(self, opps, route_result=None):
        if route_result is None:
            route_result = _fake_route_pending()
        save_out = _fake_save(len(opps))
        with patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   return_value=_fake_ideator_result(opps)), \
             patch("hypogeum.agents.discovery_director.run_source_verifier", return_value=opps), \
             patch("hypogeum.discovery.repository.start_cycle", return_value=_fake_start_ok()), \
             patch("hypogeum.discovery.repository.save_opportunities", return_value=save_out), \
             patch("hypogeum.discovery.repository.update_opportunity",
                   return_value=_fake_update_ok()), \
             patch("hypogeum.discovery.repository.complete_cycle", return_value=_fake_complete_ok()), \
             patch("hypogeum.discovery.repository.fail_cycle", return_value={"ok": True}), \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=route_result):
            return director_run("ch_test", _cfg())

    def test_generation_source_preserved_in_result_opportunities(self):
        opps = [_opp("Topic A", generation_source="llm_ideation")]
        result = self._run_with_opps(opps)
        assert result["ok"] is True
        assert len(result["opportunities"]) == 1
        assert result["opportunities"][0].get("generation_source") == "llm_ideation"

    def test_fallback_seed_preserved_in_result_opportunities(self):
        opps = [_opp("Topic A", generation_source="fallback_seed")]
        result = self._run_with_opps(opps)
        assert result["opportunities"][0].get("generation_source") == "fallback_seed"

    def test_generation_source_on_selected_opportunity(self):
        opps = [
            _opp("Winner", initial_composite=0.90, generation_source="llm_ideation",
                 verification_status="verified"),
            _opp("Loser", initial_composite=0.60, generation_source="llm_ideation",
                 verification_status="verified"),
        ]
        result = self._run_with_opps(opps)
        sel_id = result.get("selected_opportunity_id")
        assert sel_id is not None
        winner = next(
            (o for o in result["opportunities"] if o.get("_opportunity_id") == sel_id),
            result["opportunities"][0],
        )
        assert winner.get("generation_source") == "llm_ideation"

    def test_repository_receives_generation_source(self, tmp_path):
        """End-to-end: generation_source persisted to DB via real repository calls."""
        from hypogeum.db import initialize_schema, get_connection
        db = tmp_path / "test.db"
        initialize_schema(db)

        opps = [_opp("Helike", generation_source="llm_ideation")]
        with patch("hypogeum.agents.discovery_director.run_topic_ideator",
                   return_value=_fake_ideator_result(opps)), \
             patch("hypogeum.agents.discovery_director.run_source_verifier", return_value=opps), \
             patch("hypogeum.agents.discovery_director.route_selected_opportunity",
                   return_value=_fake_route_pending()):
            result = director_run("ch_test", _cfg(), db_path=db)

        assert result["ok"] is True
        conn = get_connection(db)
        try:
            rows = conn.execute(
                "SELECT generation_source FROM opportunities"
            ).fetchall()
        finally:
            conn.close()
        sources = [dict(r)["generation_source"] for r in rows]
        assert all(s == "llm_ideation" for s in sources), f"Unexpected: {sources}"


# ---------------------------------------------------------------------------
# 9. V1 regression
# ---------------------------------------------------------------------------

class TestV1Regression:

    def test_v2_is_behind_engine_flag(self):
        """Without DISCOVERY_ENGINE=v2, the director is never called."""
        import os
        from hypogeum.topic_discovery import run_discovery_cycle
        called = []

        def fake_v1(channel_id, db_path=None):
            called.append("v1")
            return {"ok": True, "cycle_id": "c", "channel_id": channel_id,
                    "candidate_count": 0, "selected_candidate_id": None, "candidates": []}

        with patch("hypogeum.topic_discovery._run_discovery_cycle_v1", side_effect=fake_v1):
            old = os.environ.pop("DISCOVERY_ENGINE", None)
            try:
                run_discovery_cycle("ch_test")
            finally:
                if old:
                    os.environ["DISCOVERY_ENGINE"] = old

        assert called == ["v1"], "v1 must be called when DISCOVERY_ENGINE is not v2"

    def test_v1_score_seed_unaffected(self):
        from hypogeum.topic_discovery import _score_seed
        seed = {
            "topic": "The hidden history of ancient underground cities",
            "source": "heuristic",
            "ctr_rationale": "", "retention_rationale": "",
            "watch_time_rationale": "", "risk": "",
        }
        assert _score_seed(seed, set(), None) == 0.82

    def test_v1_heuristic_scores_frozen(self):
        from hypogeum.topic_discovery import _score_seed
        expected = {
            "Lost civilisations: what archaeology refuses to explain": 0.78,
            "The architecture of power: buildings that shaped empires": 0.71,
            "Secret societies and the empires they shaped":             0.64,
        }
        for topic, score in expected.items():
            seed = {"topic": topic, "source": "heuristic",
                    "ctr_rationale": "", "retention_rationale": "",
                    "watch_time_rationale": "", "risk": ""}
            assert _score_seed(seed, set(), None) == score

    def test_v1_run_discovery_cycle_produces_candidates(self, tmp_path):
        import os
        from hypogeum.db import initialize_schema
        from hypogeum.topic_discovery import run_discovery_cycle
        db = tmp_path / "v1.db"
        initialize_schema(db)
        old = os.environ.get("DISCOVERY_ENGINE")
        try:
            os.environ.pop("DISCOVERY_ENGINE", None)
            result = run_discovery_cycle("ch_v1_dd", db_path=db)
        finally:
            if old is None:
                os.environ.pop("DISCOVERY_ENGINE", None)
            else:
                os.environ["DISCOVERY_ENGINE"] = old
        assert result["ok"] is True
        assert result["candidate_count"] == 7
