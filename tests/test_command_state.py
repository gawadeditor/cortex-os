"""
tests/test_command_state.py — Phase F0: Real Operational State Layer tests

Coverage:
    TestGetCommandState      — full assembly on empty DB, shape + types
    TestFactoryStatus        — healthy/degraded/blocked/unknown level mapping
    TestProviderStatus       — missing/untested/configured/expired from DB + env vars
    TestRunState             — active, queued, last completed, failures, blockers
    TestEpisodeReadiness     — can_launch logic, blockers, queued topics
    TestAttentionQueue       — ordering, categories, empty state
    TestNonBlocking          — all sections return safe defaults on bad DB
    TestNoExternalAPICalls   — socket blocked throughout; no live calls made
"""

from __future__ import annotations

import os
import socket
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.command_state import (
    get_command_state,
    _get_factory_status,
    _get_provider_status,
    _get_run_state,
    _get_episode_readiness,
    _build_attention_queue,
    _resolve_db,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path) -> Path:
    p = tmp_path / "test_command.db"
    initialize_schema(p)
    return p


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Ensure no live network calls during any test in this module."""
    _orig = socket.socket

    def _no_network(*args, **kwargs):
        raise RuntimeError("Network access is not allowed in command_state tests")

    monkeypatch.setattr(socket, "socket", _no_network)
    yield
    monkeypatch.setattr(socket, "socket", _orig)


# ---------------------------------------------------------------------------
# TestGetCommandState
# ---------------------------------------------------------------------------

class TestGetCommandState:
    def test_returns_dict_with_all_keys(self, db):
        state = get_command_state(db_path=db)
        assert isinstance(state, dict)
        for key in ("checked_at", "factory_status", "providers",
                    "run_state", "episode_readiness", "attention_queue"):
            assert key in state, f"missing key: {key}"

    def test_checked_at_is_iso_string(self, db):
        state = get_command_state(db_path=db)
        assert isinstance(state["checked_at"], str)
        assert "T" in state["checked_at"]

    def test_providers_is_list(self, db):
        state = get_command_state(db_path=db)
        assert isinstance(state["providers"], list)

    def test_run_state_is_dict(self, db):
        state = get_command_state(db_path=db)
        rs = state["run_state"]
        assert isinstance(rs, dict)
        for key in ("active_runs", "queued_runs", "last_completed",
                    "recent_failures", "blockers"):
            assert key in rs

    def test_episode_readiness_is_dict(self, db):
        state = get_command_state(db_path=db)
        er = state["episode_readiness"]
        assert isinstance(er, dict)
        assert "can_launch" in er
        assert "blockers" in er

    def test_attention_queue_is_list(self, db):
        state = get_command_state(db_path=db)
        assert isinstance(state["attention_queue"], list)


# ---------------------------------------------------------------------------
# TestFactoryStatus
# ---------------------------------------------------------------------------

class TestFactoryStatus:
    def test_returns_dict_with_level(self, db):
        status = _get_factory_status(db)
        assert isinstance(status, dict)
        assert "level" in status
        assert status["level"] in ("HEALTHY", "DEGRADED", "BLOCKED", "UNKNOWN")

    def test_safe_on_bad_db(self, tmp_path):
        bad_db = tmp_path / "nonexistent.db"
        status = _get_factory_status(bad_db)
        assert status["level"] in ("HEALTHY", "DEGRADED", "BLOCKED", "UNKNOWN")

    def test_contains_score_and_reasons(self, db):
        status = _get_factory_status(db)
        assert "director_score" in status
        assert isinstance(status["director_score"], int)
        assert "blocking_reasons" in status
        assert isinstance(status["blocking_reasons"], list)


# ---------------------------------------------------------------------------
# TestProviderStatus
# ---------------------------------------------------------------------------

class TestProviderStatus:
    def test_returns_three_providers(self, db):
        providers = _get_provider_status(db)
        names = {p["name"] for p in providers}
        assert names == {"openrouter", "elevenlabs", "muapi"}

    def test_missing_when_no_profile_no_env(self, db, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        monkeypatch.delenv("MUAPI_API_KEY", raising=False)
        providers = _get_provider_status(db)
        openrouter = next(p for p in providers if p["name"] == "openrouter")
        assert openrouter["status"] == "missing"

    def test_untested_when_env_var_present_no_profile(self, db, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-12345")
        providers = _get_provider_status(db)
        openrouter = next(p for p in providers if p["name"] == "openrouter")
        assert openrouter["status"] == "untested"

    def test_untested_when_profile_active_never_tested(self, db, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        conn = get_connection(db)
        try:
            conn.execute("""
                INSERT INTO secret_profiles
                  (secret_profile_id, provider, label, credential_type,
                   credential_data, status, scope, channel_id, expires_at,
                   last_tested_at, last_used_at, created_at, updated_at)
                VALUES (?, 'openrouter', 'test-openrouter', 'api_key',
                        'sk-test', 'active', 'factory', NULL, NULL,
                        NULL, NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """, (str(uuid.uuid4()),))
            conn.commit()
        finally:
            conn.close()
        providers = _get_provider_status(db)
        openrouter = next(p for p in providers if p["name"] == "openrouter")
        assert openrouter["status"] == "untested"

    def test_configured_when_recently_tested(self, db, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        conn = get_connection(db)
        try:
            conn.execute("""
                INSERT INTO secret_profiles
                  (secret_profile_id, provider, label, credential_type,
                   credential_data, status, scope, channel_id, expires_at,
                   last_tested_at, last_used_at, created_at, updated_at)
                VALUES (?, 'openrouter', 'test-openrouter', 'api_key',
                        'sk-test', 'active', 'factory', NULL, NULL,
                        ?, NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """, (str(uuid.uuid4()), recent))
            conn.commit()
        finally:
            conn.close()
        providers = _get_provider_status(db)
        openrouter = next(p for p in providers if p["name"] == "openrouter")
        assert openrouter["status"] == "configured"

    def test_expired_profile(self, db, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        conn = get_connection(db)
        try:
            conn.execute("""
                INSERT INTO secret_profiles
                  (secret_profile_id, provider, label, credential_type,
                   credential_data, status, scope, channel_id, expires_at,
                   last_tested_at, last_used_at, created_at, updated_at)
                VALUES (?, 'openrouter', 'expired-key', 'api_key',
                        'sk-test', 'expired', 'factory', NULL, NULL,
                        NULL, NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """, (str(uuid.uuid4()),))
            conn.commit()
        finally:
            conn.close()
        providers = _get_provider_status(db)
        openrouter = next(p for p in providers if p["name"] == "openrouter")
        assert openrouter["status"] == "expired"

    def test_each_provider_has_required_keys(self, db):
        providers = _get_provider_status(db)
        for p in providers:
            for key in ("name", "label", "required", "status", "note"):
                assert key in p, f"provider {p.get('name')} missing key: {key}"

    def test_openrouter_is_required(self, db):
        providers = _get_provider_status(db)
        openrouter = next(p for p in providers if p["name"] == "openrouter")
        assert openrouter["required"] is True

    def test_elevenlabs_and_muapi_optional(self, db):
        providers = _get_provider_status(db)
        for name in ("elevenlabs", "muapi"):
            p = next(x for x in providers if x["name"] == name)
            assert p["required"] is False


# ---------------------------------------------------------------------------
# TestRunState
# ---------------------------------------------------------------------------

class TestRunState:
    def _insert_run(self, conn, run_id, episode_id, status,
                    started_at="2026-06-01T10:00:00Z",
                    completed_at=None, error_message=None):
        conn.execute("""
            INSERT INTO runs (run_id, episode_id, started_at, completed_at,
                              status, error_message, metadata_version)
            VALUES (?, ?, ?, ?, ?, ?, 'v1')
        """, (run_id, episode_id, started_at, completed_at, status, error_message))
        conn.commit()

    def test_empty_db_returns_safe_defaults(self, db):
        rs = _get_run_state(db)
        assert rs["active_runs"] == []
        assert rs["queued_runs"] == []
        assert rs["last_completed"] is None
        assert rs["recent_failures"] == []
        assert rs["blockers"] == []

    def test_active_runs_detected(self, db):
        conn = get_connection(db)
        self._insert_run(conn, "run-1", "ep_001", "running")
        conn.close()
        rs = _get_run_state(db)
        assert len(rs["active_runs"]) == 1
        assert rs["active_runs"][0]["episode_id"] == "ep_001"

    def test_queued_runs_detected(self, db):
        conn = get_connection(db)
        self._insert_run(conn, "run-2", "ep_002", "queued")
        self._insert_run(conn, "run-3", "ep_003", "paused")
        conn.close()
        rs = _get_run_state(db)
        assert len(rs["queued_runs"]) == 2

    def test_last_completed_found(self, db):
        conn = get_connection(db)
        self._insert_run(conn, "run-4", "ep_004", "completed",
                         completed_at="2026-06-01T11:00:00Z")
        conn.close()
        rs = _get_run_state(db)
        assert rs["last_completed"] is not None
        assert rs["last_completed"]["episode_id"] == "ep_004"

    def test_recent_failures_capped_at_5(self, db):
        conn = get_connection(db)
        for i in range(7):
            self._insert_run(conn, f"run-f{i}", f"ep_f{i:02d}", "failed",
                             error_message=f"error {i}")
        conn.close()
        rs = _get_run_state(db)
        assert len(rs["recent_failures"]) <= 5

    def test_run_state_safe_on_bad_db(self, tmp_path):
        bad = tmp_path / "missing.db"
        rs = _get_run_state(bad)
        assert rs["active_runs"] == []

    def test_ois_blockers_appear(self, db):
        import json
        conn = get_connection(db)
        conn.execute("""
            INSERT INTO signal_records
              (signal_id, episode_id, run_id, ingested_at, data_quality,
               signal_type, severity, blocking, lifecycle_status, full_record,
               source_system)
            VALUES (?, 'ep_000', 'run-x', '2026-06-01T10:00:00Z', 'no_data',
                    'credential_failure', 'CRITICAL', 1, 'open', ?, 'startup')
        """, (str(uuid.uuid4()), json.dumps({"message": "OpenRouter unavailable"})))
        conn.commit()
        conn.close()
        rs = _get_run_state(db)
        assert len(rs["blockers"]) == 1
        assert rs["blockers"][0]["signal_type"] == "credential_failure"


# ---------------------------------------------------------------------------
# TestEpisodeReadiness
# ---------------------------------------------------------------------------

class TestEpisodeReadiness:
    def test_returns_dict_with_can_launch(self, db):
        fs = {"level": "UNKNOWN", "blocking_reasons": [], "warnings": []}
        er = _get_episode_readiness(db, fs)
        assert isinstance(er, dict)
        assert "can_launch" in er
        assert isinstance(er["can_launch"], bool)

    def test_queued_topic_found(self, db):
        conn = get_connection(db)
        conn.execute("""
            INSERT INTO episode_topics
              (topic_id, channel_id, topic_text, status, priority, created_at, updated_at)
            VALUES (?, 'hypogeum', 'Lost Cities of the Ancient World', 'queued', 0,
                    '2026-06-01T10:00:00Z', '2026-06-01T10:00:00Z')
        """, (str(uuid.uuid4()),))
        conn.commit()
        conn.close()
        fs = {"level": "UNKNOWN", "blocking_reasons": [], "warnings": []}
        er = _get_episode_readiness(db, fs)
        assert er["next_topic"] != {}
        assert "topic_text" in er["next_topic"]

    def test_no_queued_topics_returns_empty(self, db):
        fs = {"level": "UNKNOWN", "blocking_reasons": [], "warnings": []}
        er = _get_episode_readiness(db, fs)
        assert er["next_topic"] == {}

    def test_blockers_list_is_list(self, db):
        fs = {"level": "UNKNOWN", "blocking_reasons": [], "warnings": []}
        er = _get_episode_readiness(db, fs)
        assert isinstance(er["blockers"], list)

    def test_safe_on_bad_db(self, tmp_path):
        bad = tmp_path / "missing.db"
        fs = {"level": "UNKNOWN", "blocking_reasons": [], "warnings": []}
        er = _get_episode_readiness(bad, fs)
        assert er["can_launch"] is False


# ---------------------------------------------------------------------------
# TestAttentionQueue
# ---------------------------------------------------------------------------

class TestAttentionQueue:
    def _make_providers(self, statuses: dict) -> list:
        return [
            {"name": name, "label": name.title(), "required": (name == "openrouter"),
             "status": st, "note": f"note for {name}"}
            for name, st in statuses.items()
        ]

    def test_empty_when_all_healthy(self):
        factory_status = {"level": "HEALTHY", "blocking_reasons": [], "warnings": []}
        providers = self._make_providers(
            {"openrouter": "configured", "elevenlabs": "configured", "muapi": "configured"}
        )
        run_state = {"blockers": [], "active_runs": [], "queued_runs": [],
                     "last_completed": None, "recent_failures": []}
        episode_readiness = {"can_launch": True, "blockers": [], "missing_inputs": [],
                             "required_approvals": [], "pending_approval_count": 0}
        queue = _build_attention_queue(factory_status, providers, run_state, episode_readiness)
        assert queue == []

    def test_blocking_signal_is_priority_1(self):
        factory_status = {"level": "BLOCKED", "blocking_reasons": [], "warnings": []}
        providers = self._make_providers({"openrouter": "configured"})
        run_state = {
            "blockers": [{"signal_type": "credential_failure", "message": "OpenRouter down"}],
            "active_runs": [], "queued_runs": [], "last_completed": None, "recent_failures": [],
        }
        episode_readiness = {"can_launch": False, "blockers": [], "missing_inputs": [],
                             "required_approvals": [], "pending_approval_count": 0}
        queue = _build_attention_queue(factory_status, providers, run_state, episode_readiness)
        critical = [i for i in queue if i["priority"] == 1]
        assert len(critical) >= 1
        assert any(i["category"] == "blocking_signal" for i in critical)

    def test_missing_required_credential_is_medium(self):
        factory_status = {"level": "HEALTHY", "blocking_reasons": [], "warnings": []}
        providers = self._make_providers(
            {"openrouter": "missing", "elevenlabs": "configured", "muapi": "configured"}
        )
        run_state = {"blockers": [], "active_runs": [], "queued_runs": [],
                     "last_completed": None, "recent_failures": []}
        episode_readiness = {"can_launch": True, "blockers": [], "missing_inputs": [],
                             "required_approvals": [], "pending_approval_count": 0}
        queue = _build_attention_queue(factory_status, providers, run_state, episode_readiness)
        missing_items = [i for i in queue if i["category"] == "missing_credential"]
        assert len(missing_items) >= 1
        assert any(i["priority"] == 3 for i in missing_items)

    def test_pending_approvals_is_priority_2(self):
        factory_status = {"level": "HEALTHY", "blocking_reasons": [], "warnings": []}
        providers = self._make_providers({"openrouter": "configured"})
        run_state = {"blockers": [], "active_runs": [], "queued_runs": [],
                     "last_completed": None, "recent_failures": []}
        episode_readiness = {"can_launch": True, "blockers": [], "missing_inputs": [],
                             "required_approvals": [], "pending_approval_count": 2}
        queue = _build_attention_queue(factory_status, providers, run_state, episode_readiness)
        approval_items = [i for i in queue if i["category"] == "pending_approval"]
        assert len(approval_items) == 1
        assert approval_items[0]["priority"] == 2

    def test_items_ordered_by_priority(self):
        factory_status = {"level": "BLOCKED", "blocking_reasons": ["Factory not active"], "warnings": []}
        providers = self._make_providers({"openrouter": "missing", "elevenlabs": "untested"})
        run_state = {
            "blockers": [{"signal_type": "run_failure", "message": "critical"}],
            "active_runs": [], "queued_runs": [], "last_completed": None, "recent_failures": [],
        }
        episode_readiness = {"can_launch": False, "blockers": ["audit failed"],
                             "missing_inputs": [], "required_approvals": [],
                             "pending_approval_count": 1}
        queue = _build_attention_queue(factory_status, providers, run_state, episode_readiness)
        priorities = [i["priority"] for i in queue]
        assert priorities == sorted(priorities)

    def test_each_item_has_decision_card_keys(self):
        """Every attention queue item must have all decision card fields."""
        factory_status = {"level": "BLOCKED", "blocking_reasons": ["test blocker"], "warnings": []}
        providers = self._make_providers({"openrouter": "missing"})
        run_state = {"blockers": [], "active_runs": [], "queued_runs": [],
                     "last_completed": None, "recent_failures": []}
        episode_readiness = {"can_launch": False, "blockers": ["missing something"],
                             "missing_inputs": [], "required_approvals": [],
                             "pending_approval_count": 0}
        queue = _build_attention_queue(factory_status, providers, run_state, episode_readiness)
        required_keys = (
            "priority", "level", "category", "title",
            "why", "evidence",
            "action_url", "action_label",
            "investigate_url", "investigate_label",
        )
        for item in queue:
            for key in required_keys:
                assert key in item, f"item missing key: {key}"

    def test_no_mock_analytics_fields(self):
        """Attention queue must never contain analytics-derived mock fields."""
        factory_status = {"level": "HEALTHY", "blocking_reasons": [], "warnings": []}
        providers = self._make_providers({"openrouter": "untested"})
        run_state = {"blockers": [], "active_runs": [], "queued_runs": [],
                     "last_completed": None, "recent_failures": []}
        episode_readiness = {"can_launch": True, "blockers": [], "missing_inputs": [],
                             "required_approvals": [], "pending_approval_count": 0}
        queue = _build_attention_queue(factory_status, providers, run_state, episode_readiness)
        forbidden_categories = {"mock_recommendation", "synthetic_insight", "analytics_tip"}
        for item in queue:
            assert item["category"] not in forbidden_categories
            for key in item:
                assert "mock" not in key.lower()
                assert "synthetic" not in key.lower()
                assert "analytics" not in key.lower()

    def test_allowed_categories_only(self):
        """All generated categories must be from the allowed set."""
        from hypogeum.command_state import _ALLOWED_CATEGORIES
        factory_status = {"level": "BLOCKED", "blocking_reasons": ["blocker"], "warnings": []}
        providers = self._make_providers(
            {"openrouter": "missing", "elevenlabs": "untested", "muapi": "expired"}
        )
        run_state = {
            "blockers": [{"signal_type": "credential_failure", "message": "down"}],
            "active_runs": [], "queued_runs": [], "last_completed": None,
            "recent_failures": [{"episode_id": f"ep_{i}", "run_id": f"r{i}", "error_message": "err"}
                                  for i in range(3)],
        }
        episode_readiness = {"can_launch": False, "blockers": ["audit failed"],
                             "missing_inputs": [], "required_approvals": [],
                             "pending_approval_count": 1}
        queue = _build_attention_queue(factory_status, providers, run_state, episode_readiness)
        for item in queue:
            assert item["category"] in _ALLOWED_CATEGORIES, \
                f"unexpected category: {item['category']}"

    def test_recent_failures_trigger_queue_item(self):
        """Two or more recent failures create a queue item."""
        factory_status = {"level": "HEALTHY", "blocking_reasons": [], "warnings": []}
        providers = self._make_providers({"openrouter": "configured"})
        run_state = {
            "blockers": [], "active_runs": [], "queued_runs": [], "last_completed": None,
            "recent_failures": [
                {"episode_id": "ep_001", "run_id": "r1", "error_message": "err 1"},
                {"episode_id": "ep_002", "run_id": "r2", "error_message": "err 2"},
            ],
        }
        episode_readiness = {"can_launch": True, "blockers": [], "missing_inputs": [],
                             "required_approvals": [], "pending_approval_count": 0}
        queue = _build_attention_queue(factory_status, providers, run_state, episode_readiness)
        failure_items = [i for i in queue if i["category"] == "recent_failures"]
        assert len(failure_items) == 1
        assert failure_items[0]["priority"] == 2

    def test_single_failure_does_not_trigger_queue_item(self):
        """One failure is noise, not a systemic issue — should not appear."""
        factory_status = {"level": "HEALTHY", "blocking_reasons": [], "warnings": []}
        providers = self._make_providers({"openrouter": "configured"})
        run_state = {
            "blockers": [], "active_runs": [], "queued_runs": [], "last_completed": None,
            "recent_failures": [{"episode_id": "ep_001", "run_id": "r1", "error_message": "err"}],
        }
        episode_readiness = {"can_launch": True, "blockers": [], "missing_inputs": [],
                             "required_approvals": [], "pending_approval_count": 0}
        queue = _build_attention_queue(factory_status, providers, run_state, episode_readiness)
        assert not any(i["category"] == "recent_failures" for i in queue)


# ---------------------------------------------------------------------------
# TestNonBlocking
# ---------------------------------------------------------------------------

class TestNonBlocking:
    def test_get_command_state_bad_db(self, tmp_path):
        bad = tmp_path / "does_not_exist.db"
        state = get_command_state(db_path=bad)
        assert isinstance(state, dict)
        assert "factory_status" in state

    def test_factory_status_bad_db(self, tmp_path):
        bad = tmp_path / "does_not_exist.db"
        status = _get_factory_status(bad)
        assert status["level"] in ("HEALTHY", "DEGRADED", "BLOCKED", "UNKNOWN")

    def test_provider_status_no_credential_table(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        db = tmp_path / "empty.db"
        providers = _get_provider_status(db)
        assert isinstance(providers, list)

    def test_run_state_missing_db(self, tmp_path):
        bad = tmp_path / "does_not_exist.db"
        rs = _get_run_state(bad)
        assert isinstance(rs, dict)
        assert rs["active_runs"] == []


# ---------------------------------------------------------------------------
# TestNoExternalAPICalls
# ---------------------------------------------------------------------------

class TestNoExternalAPICalls:
    def test_full_state_no_network(self, db):
        """get_command_state must complete without network I/O (socket blocked by autouse fixture)."""
        state = get_command_state(db_path=db)
        assert state is not None


# ---------------------------------------------------------------------------
# TestF1Routes — Phase F1: / redirect and command page assertions
# ---------------------------------------------------------------------------

class TestF1Routes:
    @pytest.fixture()
    def flask_client(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import run_dashboard as server
        server.app.config["TESTING"] = True
        with server.app.test_client() as c:
            yield c, server

    def test_root_redirects_to_dashboard(self, flask_client):
        client, _ = flask_client
        resp = client.get("/")
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert "/dashboard" in location or "/command" in location

    def test_root_follows_redirect_to_command_html(self, flask_client):
        client, _ = flask_client
        resp = client.get("/", follow_redirects=True)
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "<!DOCTYPE html>" in html or "<html" in html

    def test_command_route_returns_200(self, flask_client):
        client, _ = flask_client
        resp = client.get("/command")
        assert resp.status_code == 200

    def test_command_page_contains_attention_queue_section(self, flask_client):
        client, _ = flask_client
        resp = client.get("/command")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Needs attention" in html

    def test_command_page_contains_launch_readiness_section(self, flask_client):
        client, _ = flask_client
        resp = client.get("/command")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Launch readiness" in html

    def test_command_page_contains_active_production_section(self, flask_client):
        client, _ = flask_client
        resp = client.get("/command")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Active production" in html

    def test_command_page_contains_provider_section(self, flask_client):
        client, _ = flask_client
        resp = client.get("/command")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Provider connectivity" in html

    def test_command_page_contains_investigate_section(self, flask_client):
        client, _ = flask_client
        resp = client.get("/command")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Investigate" in html

    def test_command_page_no_mock_analytics(self, flask_client):
        """Command page must not include mock analytics fields or synthetic data."""
        client, _ = flask_client
        resp = client.get("/command")
        html = resp.data.decode().lower()
        for forbidden in ("mock_analytics", "synthetic_insight", "ctr =", "retention =", "mock data"):
            assert forbidden not in html, f"found forbidden content: {forbidden!r}"

    def test_api_command_state_returns_200(self, flask_client):
        client, _ = flask_client
        resp = client.get("/api/command-state")
        assert resp.status_code == 200

    def test_api_command_state_has_required_keys(self, flask_client):
        import json
        client, _ = flask_client
        resp = client.get("/api/command-state")
        data = json.loads(resp.data)
        for key in ("checked_at", "factory_status", "providers",
                    "run_state", "episode_readiness", "attention_queue"):
            assert key in data, f"missing key: {key}"

    def test_api_command_state_no_mock_analytics_keys(self, flask_client):
        """API response must not contain analytics-derived or mock fields at top level."""
        import json
        client, _ = flask_client
        resp = client.get("/api/command-state")
        data = json.loads(resp.data)
        forbidden_top_level = {"analytics", "recommendations", "insights",
                               "mock_recommendations", "synthetic_data"}
        for key in forbidden_top_level:
            assert key not in data, f"forbidden key found in response: {key!r}"

    def test_attention_queue_items_have_decision_card_shape(self, flask_client):
        """Every attention queue item from the API must have the decision card fields."""
        import json
        client, _ = flask_client
        resp = client.get("/api/command-state")
        data = json.loads(resp.data)
        queue = data.get("attention_queue", [])
        required = ("priority", "level", "category", "title",
                    "why", "evidence", "action_url", "action_label",
                    "investigate_url", "investigate_label")
        for item in queue:
            for key in required:
                assert key in item, f"queue item missing key: {key}"

    def test_legacy_dashboard_still_accessible(self, flask_client):
        """Legacy dashboard must remain accessible at /dashboard."""
        from unittest.mock import patch
        client, server = flask_client
        from hypogeum.dashboard_api import (
            FactoryState, RunHealthSummary, ArchiveSummary,
            RecallSummary, LearningSummary, CostSummary, AuditSummary,
        )
        state = FactoryState(
            factory_id="test", schema_version="v2",
            checked_at="2026-01-01T00:00:00+00:00", overall_health="healthy",
            run_health=RunHealthSummary(0, 0, 0, 0, 0.0, None),
            channels=[], agent_health=[], model_usage=[], provider_health=[],
            archive=ArchiveSummary(0, 0, {}, None, None),
            recall=RecallSummary(0, 0, 0, 0.0),
            learning=LearningSummary(0, {}, []),
            costs=CostSummary(0.0, {}, {}, {}, 0, 0),
            audit=AuditSummary(None, True, 0, 0, "ACCEPTED"),
            notifications=[],
        )
        from hypogeum.factory_director import FactoryDirectorReport, RunClearanceDecision
        clearance = RunClearanceDecision(
            cleared=True, reason="OK", blocking_checks=[],
            blocking_signals=[], pending_approvals=0,
            checked_at="2026-01-01T00:00:00Z",
        )
        report = FactoryDirectorReport(
            factory_id="test", factory_name="Test", readiness_level="READY",
            readiness_score=100, can_run=True, assessed_at="2026-01-01T00:00:00Z",
            domains=[], blocking_reasons=[], warnings=[], run_clearance=clearance,
        )
        with patch.object(server, "get_factory_state", return_value=state), \
             patch.object(server, "assess_factory", return_value=report):
            resp = client.get("/dashboard")
        assert resp.status_code == 200
