"""
tests/test_factory_budget_route.py — Dashboard route tests for the
factory monthly budget ceiling control (Phase 3 Production Hardening).

Covers GET/POST /api/factory/budget-ceiling:
    - GET returns the current DB-backed status shape
    - POST validation (missing reason, invalid number)
    - POST success: DB updated, response reports previous/new/source/
      clearance correctly
    - An audit record is created for a successful POST
    - Route behaves consistently with the dashboard's existing (lack of)
      auth model — documented, not silently assumed

Uses server.app.config["DB_PATH"] override per CLAUDE.md's Test
Isolation convention. Never touches the production database.

Run with:  python3 -m pytest tests/test_factory_budget_route.py -v
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

import run_dashboard as server
from hypogeum.db import get_connection, initialize_schema


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "budget_route_test.db"
    initialize_schema(p)
    return p


def _seed_factory(db_path: Path, factory_id: str = "hypogeum") -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO factories (factory_id, factory_name, factory_slug, "
            "status, default_language, default_timezone, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (factory_id, "Hypogeum", factory_id, "active", "en", "UTC",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_credentials(db_path: Path) -> None:
    conn = get_connection(db_path)
    try:
        for prov in ("openrouter", "muapi", "elevenlabs"):
            conn.execute(
                "INSERT INTO secret_profiles "
                "(secret_profile_id, provider, label, credential_type, "
                "credential_data, status, scope, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), prov, f"{prov}-env", "api_key",
                 "dummy", "active", "factory",
                 "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def client(db_path):
    server.app.config["TESTING"] = True
    original = server.app.config.get("DB_PATH")
    server.app.config["DB_PATH"] = str(db_path)
    _seed_factory(db_path)
    _seed_credentials(db_path)
    with server.app.test_client() as c:
        yield c
    server.app.config["DB_PATH"] = original


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------

class TestGetBudgetCeiling:
    def test_get_returns_expected_shape(self, client):
        resp = client.get("/api/factory/budget-ceiling")
        assert resp.status_code == 200
        data = resp.get_json()
        for key in (
            "factory_id", "month_to_date_spend_usd", "effective_ceiling_usd",
            "ceiling_source", "headroom_usd", "cleared", "blocking_reason",
        ):
            assert key in data

    def test_get_reflects_factory_policy_when_set(self, client, db_path):
        from hypogeum.factory_entity import update_factory
        update_factory(
            "hypogeum", db_path=db_path,
            factory_policies={"monthly_ceiling_usd": 150.0},
        )
        resp = client.get("/api/factory/budget-ceiling")
        data = resp.get_json()
        assert data["ceiling_source"] == "factory_policy"
        assert data["effective_ceiling_usd"] == 150.0


# ---------------------------------------------------------------------------
# POST — validation
# ---------------------------------------------------------------------------

class TestPostValidation:
    def test_missing_reason_rejected(self, client):
        resp = client.post(
            "/api/factory/budget-ceiling",
            json={"monthly_ceiling_usd": 50},
        )
        assert resp.status_code == 400
        assert "reason" in resp.get_json()["error"].lower()

    def test_blank_reason_rejected(self, client):
        resp = client.post(
            "/api/factory/budget-ceiling",
            json={"monthly_ceiling_usd": 50, "reason": "   "},
        )
        assert resp.status_code == 400

    def test_non_numeric_ceiling_rejected(self, client):
        resp = client.post(
            "/api/factory/budget-ceiling",
            json={"monthly_ceiling_usd": "a lot", "reason": "test"},
        )
        assert resp.status_code == 400

    def test_negative_ceiling_rejected(self, client):
        resp = client.post(
            "/api/factory/budget-ceiling",
            json={"monthly_ceiling_usd": -5, "reason": "test"},
        )
        assert resp.status_code == 400

    def test_missing_body_rejected(self, client):
        resp = client.post("/api/factory/budget-ceiling", json={})
        assert resp.status_code == 400

    def test_no_db_write_occurs_on_validation_failure(self, client, db_path):
        client.post(
            "/api/factory/budget-ceiling",
            json={"monthly_ceiling_usd": -5, "reason": "test"},
        )
        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM audit_records").fetchone()[0]
        finally:
            conn.close()
        assert count == 0


# ---------------------------------------------------------------------------
# POST — success
# ---------------------------------------------------------------------------

class TestPostSuccess:
    def test_updates_db_backed_policy(self, client, db_path):
        resp = client.post(
            "/api/factory/budget-ceiling",
            json={"monthly_ceiling_usd": 200, "reason": "burn-in test increase"},
        )
        assert resp.status_code == 200

        from hypogeum.factory_entity import get_factory_policies
        policies = get_factory_policies("hypogeum", db_path=db_path)
        assert policies["monthly_ceiling_usd"] == 200

    def test_response_reports_previous_and_new(self, client, db_path):
        from hypogeum.factory_entity import update_factory
        update_factory(
            "hypogeum", db_path=db_path,
            factory_policies={"monthly_ceiling_usd": 100.0},
        )
        resp = client.post(
            "/api/factory/budget-ceiling",
            json={"monthly_ceiling_usd": 250, "reason": "raising for a run"},
        )
        data = resp.get_json()
        assert data["previous_ceiling_usd"] == 100.0
        assert data["new_ceiling_usd"] == 250.0
        assert data["ceiling_source"] == "factory_policy"

    def test_response_reflects_clearance_state(self, client, db_path):
        from hypogeum.factory_entity import update_factory
        # Seed a run + cost so month_to_date_spend > 0 and can meaningfully clear/block.
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT INTO runs (run_id, episode_id, started_at, status, completed_at) "
                "VALUES ('r1','ep1','2026-07-01T00:00:00','completed','2026-07-01T00:10:00')"
            )
            conn.execute(
                "INSERT INTO costs (cost_id, run_id, episode_id, provider, call_type, "
                "cost_usd, called_at) VALUES ('c1','r1','ep1','muapi','video',40.0,datetime('now'))"
            )
            conn.commit()
        finally:
            conn.close()

        resp = client.post(
            "/api/factory/budget-ceiling",
            json={"monthly_ceiling_usd": 10, "reason": "deliberately tight ceiling"},
        )
        data = resp.get_json()
        assert data["cleared"] is False
        assert data["blocking_reason"] is not None

    def test_operator_identity_recorded_when_available(self, client, db_path):
        client.post(
            "/api/factory/budget-ceiling",
            json={"monthly_ceiling_usd": 60, "reason": "test", "operator": "alice"},
        )
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT triggered_by FROM audit_records WHERE entity_id='hypogeum'"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "alice"

    def test_defaults_to_operator_when_identity_not_supplied(self, client, db_path):
        client.post(
            "/api/factory/budget-ceiling",
            json={"monthly_ceiling_usd": 60, "reason": "test"},
        )
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT triggered_by FROM audit_records WHERE entity_id='hypogeum'"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "operator"

    def test_zero_restores_env_fallback(self, client, db_path):
        from hypogeum.factory_entity import update_factory
        update_factory(
            "hypogeum", db_path=db_path,
            factory_policies={"monthly_ceiling_usd": 100.0},
        )
        resp = client.post(
            "/api/factory/budget-ceiling",
            json={"monthly_ceiling_usd": 0, "reason": "reverting to env fallback"},
        )
        data = resp.get_json()
        assert data["ceiling_source"] in ("env_fallback", "none")


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

class TestAuditRecordCreation:
    def test_successful_change_creates_audit_record(self, client, db_path):
        client.post(
            "/api/factory/budget-ceiling",
            json={"monthly_ceiling_usd": 80, "reason": "audit trail check"},
        )
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM audit_records WHERE entity_id='hypogeum'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["entity_type"] == "factory"
        assert row["action"] == "factory_policy_updated"
        assert row["reason"] == "audit trail check"
        assert row["new_value"] == "80.0"


# ---------------------------------------------------------------------------
# Authorization posture (documented, not silently assumed)
# ---------------------------------------------------------------------------

class TestAuthorizationPosture:
    def test_route_matches_dashboards_existing_auth_model(self, client):
        """
        This dashboard has no login/session system for ANY route today
        (confirmed by inspection of run_dashboard.py). This route
        intentionally matches that existing posture rather than
        introducing a one-off auth barrier inconsistent with every other
        route — building real authentication is out of this feature's
        scope. This test documents that decision rather than assuming it.
        """
        resp = client.get("/api/factory/budget-ceiling")
        assert resp.status_code != 401
        assert resp.status_code != 403
