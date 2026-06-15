"""
tests/test_auth.py — P1-03: User / Role System tests

Coverage:
    TestUserCRUD              — create, get, list, disable, invalid role
    TestApiKeys               — create, authenticate, revoke, hash storage
    TestCheckAuthorization    — role hierarchy against PERMISSION_TABLE actions
    TestDashboardIsAuthorized — dashboard_api.is_authorized() wiring
    TestNoNetworkCalls        — no sockets opened during auth operations
    TestNoProductionSideEffects — no episode or run records written
"""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from hypogeum.auth import (
    VALID_ROLES,
    UserRecord,
    authenticate_api_key,
    check_authorization,
    create_api_key,
    create_user,
    disable_user,
    get_user,
    list_users,
    revoke_api_key,
)
from hypogeum.db import get_connection, initialize_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "auth_test.db"
    initialize_schema(p)
    return p


def _make_user(db_path, role="operator", email=None, name="Test User"):
    suffix = role
    return create_user(
        email=email or f"{suffix}@test.local",
        name=name,
        role=role,
        db_path=db_path,
    )


# ===========================================================================
# TestUserCRUD
# ===========================================================================

class TestUserCRUD:

    def test_create_returns_user_record(self, db_path):
        u = _make_user(db_path, role="operator")
        assert isinstance(u, UserRecord)
        assert u.role == "operator"
        assert u.status == "active"

    def test_create_persists_to_db(self, db_path):
        u = _make_user(db_path, role="admin")
        fetched = get_user(u.user_id, db_path=db_path)
        assert fetched is not None
        assert fetched.email == u.email

    def test_create_with_custom_user_id(self, db_path):
        u = create_user("custom@test.local", "Custom", "viewer",
                        user_id="custom-uid-001", db_path=db_path)
        assert u.user_id == "custom-uid-001"
        assert get_user("custom-uid-001", db_path=db_path) is not None

    def test_list_empty(self, db_path):
        assert list_users(db_path=db_path) == []

    def test_list_returns_all(self, db_path):
        _make_user(db_path, role="owner", email="a@test.local")
        _make_user(db_path, role="viewer", email="b@test.local")
        users = list_users(db_path=db_path)
        assert len(users) == 2

    def test_list_ordered_by_created_at(self, db_path):
        u1 = _make_user(db_path, role="admin", email="first@test.local")
        u2 = _make_user(db_path, role="editor", email="second@test.local")
        users = list_users(db_path=db_path)
        assert users[0].user_id == u1.user_id
        assert users[1].user_id == u2.user_id

    def test_disable_user_sets_status_disabled(self, db_path):
        u = _make_user(db_path, role="operator")
        disable_user(u.user_id, db_path=db_path)
        fetched = get_user(u.user_id, db_path=db_path)
        assert fetched.status == "disabled"

    def test_disable_user_returns_true(self, db_path):
        u = _make_user(db_path, role="editor")
        assert disable_user(u.user_id, db_path=db_path) is True

    def test_get_nonexistent_returns_none(self, db_path):
        assert get_user("no_such_user", db_path=db_path) is None

    def test_invalid_role_raises(self, db_path):
        with pytest.raises(ValueError, match="Invalid role"):
            create_user("x@test.local", "X", "superuser", db_path=db_path)

    def test_all_six_roles_accepted(self, db_path):
        for i, role in enumerate(VALID_ROLES):
            create_user(f"role{i}@test.local", f"User{i}", role, db_path=db_path)
        users = list_users(db_path=db_path)
        assert len(users) == len(VALID_ROLES)

    def test_created_at_is_iso_timestamp(self, db_path):
        u = _make_user(db_path, role="owner")
        assert "T" in u.created_at

    def test_duplicate_email_raises(self, db_path):
        import sqlite3
        create_user("dup@test.local", "A", "viewer", db_path=db_path)
        with pytest.raises(sqlite3.IntegrityError):
            create_user("dup@test.local", "B", "editor", db_path=db_path)


# ===========================================================================
# TestApiKeys
# ===========================================================================

class TestApiKeys:

    def test_create_returns_key_id_and_raw_key(self, db_path):
        u = _make_user(db_path, role="operator")
        key_id, raw_key = create_api_key(u.user_id, "test-key", db_path=db_path)
        assert isinstance(key_id, str) and len(key_id) > 0
        assert isinstance(raw_key, str) and raw_key.startswith("hbk_")

    def test_api_key_hash_not_stored_as_plaintext(self, db_path):
        u = _make_user(db_path, role="admin")
        _, raw_key = create_api_key(u.user_id, "my-key", db_path=db_path)
        conn = get_connection(db_path)
        try:
            rows = conn.execute("SELECT key_hash FROM api_keys").fetchall()
        finally:
            conn.close()
        stored_hashes = [r["key_hash"] for r in rows]
        assert raw_key not in stored_hashes, "raw_key must not be stored in DB"

    def test_authenticate_with_raw_key_returns_user(self, db_path):
        u = _make_user(db_path, role="owner")
        _, raw_key = create_api_key(u.user_id, "auth-key", db_path=db_path)
        result = authenticate_api_key(raw_key, db_path=db_path)
        assert result is not None
        assert result.user_id == u.user_id

    def test_wrong_key_returns_none(self, db_path):
        u = _make_user(db_path, role="operator")
        create_api_key(u.user_id, "real-key", db_path=db_path)
        result = authenticate_api_key("hbk_wrongkey", db_path=db_path)
        assert result is None

    def test_revoked_key_returns_none(self, db_path):
        u = _make_user(db_path, role="operator")
        key_id, raw_key = create_api_key(u.user_id, "revoke-me", db_path=db_path)
        revoke_api_key(key_id, db_path=db_path)
        result = authenticate_api_key(raw_key, db_path=db_path)
        assert result is None

    def test_disabled_user_cannot_authenticate(self, db_path):
        u = _make_user(db_path, role="operator")
        _, raw_key = create_api_key(u.user_id, "disabled-key", db_path=db_path)
        disable_user(u.user_id, db_path=db_path)
        result = authenticate_api_key(raw_key, db_path=db_path)
        assert result is None

    def test_authenticate_updates_last_used_at(self, db_path):
        u = _make_user(db_path, role="viewer")
        _, raw_key = create_api_key(u.user_id, "used-key", db_path=db_path)
        conn = get_connection(db_path)
        try:
            before = conn.execute(
                "SELECT last_used_at FROM api_keys"
            ).fetchone()["last_used_at"]
        finally:
            conn.close()
        assert before is None
        authenticate_api_key(raw_key, db_path=db_path)
        conn = get_connection(db_path)
        try:
            after = conn.execute(
                "SELECT last_used_at FROM api_keys"
            ).fetchone()["last_used_at"]
        finally:
            conn.close()
        assert after is not None

    def test_two_keys_for_same_user(self, db_path):
        u = _make_user(db_path, role="admin")
        kid1, rk1 = create_api_key(u.user_id, "key-1", db_path=db_path)
        kid2, rk2 = create_api_key(u.user_id, "key-2", db_path=db_path)
        assert kid1 != kid2
        assert rk1 != rk2
        assert authenticate_api_key(rk1, db_path=db_path) is not None
        assert authenticate_api_key(rk2, db_path=db_path) is not None

    def test_revoke_one_key_leaves_other_active(self, db_path):
        u = _make_user(db_path, role="admin")
        kid1, rk1 = create_api_key(u.user_id, "key-1", db_path=db_path)
        _, rk2 = create_api_key(u.user_id, "key-2", db_path=db_path)
        revoke_api_key(kid1, db_path=db_path)
        assert authenticate_api_key(rk1, db_path=db_path) is None
        assert authenticate_api_key(rk2, db_path=db_path) is not None


# ===========================================================================
# TestCheckAuthorization
# ===========================================================================

class TestCheckAuthorization:

    def test_owner_can_approve_run(self, db_path):
        u = _make_user(db_path, role="owner", email="owner@test.local")
        assert check_authorization(u.user_id, "approve_run", db_path=db_path) is True

    def test_admin_can_approve_run(self, db_path):
        u = _make_user(db_path, role="admin", email="admin@test.local")
        assert check_authorization(u.user_id, "approve_run", db_path=db_path) is True

    def test_operator_can_approve_run(self, db_path):
        u = _make_user(db_path, role="operator", email="op@test.local")
        assert check_authorization(u.user_id, "approve_run", db_path=db_path) is True

    def test_editor_cannot_approve_run(self, db_path):
        u = _make_user(db_path, role="editor", email="ed@test.local")
        assert check_authorization(u.user_id, "approve_run", db_path=db_path) is False

    def test_viewer_cannot_approve_run(self, db_path):
        u = _make_user(db_path, role="viewer", email="viewer@test.local")
        assert check_authorization(u.user_id, "approve_run", db_path=db_path) is False

    def test_client_cannot_approve_run(self, db_path):
        u = _make_user(db_path, role="client", email="client@test.local")
        assert check_authorization(u.user_id, "approve_run", db_path=db_path) is False

    def test_operator_can_pause_channel(self, db_path):
        u = _make_user(db_path, role="operator", email="op2@test.local")
        assert check_authorization(u.user_id, "pause_channel", db_path=db_path) is True

    def test_operator_can_resume_channel(self, db_path):
        u = _make_user(db_path, role="operator", email="op3@test.local")
        assert check_authorization(u.user_id, "resume_channel", db_path=db_path) is True

    def test_operator_cannot_clone_factory(self, db_path):
        u = _make_user(db_path, role="operator", email="op4@test.local")
        assert check_authorization(u.user_id, "clone_factory", db_path=db_path) is False

    def test_admin_can_clone_factory(self, db_path):
        u = _make_user(db_path, role="admin", email="admin2@test.local")
        assert check_authorization(u.user_id, "clone_factory", db_path=db_path) is True

    def test_owner_can_emergency_stop(self, db_path):
        u = _make_user(db_path, role="owner", email="owner2@test.local")
        assert check_authorization(u.user_id, "emergency_stop", db_path=db_path) is True

    def test_admin_cannot_emergency_stop(self, db_path):
        u = _make_user(db_path, role="admin", email="admin3@test.local")
        assert check_authorization(u.user_id, "emergency_stop", db_path=db_path) is False

    def test_owner_can_deactivate_channel(self, db_path):
        u = _make_user(db_path, role="owner", email="owner3@test.local")
        assert check_authorization(u.user_id, "deactivate_channel", db_path=db_path) is True

    def test_disabled_user_cannot_authorize(self, db_path):
        u = _make_user(db_path, role="owner", email="owner4@test.local")
        disable_user(u.user_id, db_path=db_path)
        assert check_authorization(u.user_id, "approve_run", db_path=db_path) is False

    def test_nonexistent_user_returns_false(self, db_path):
        assert check_authorization("ghost-user-id", "approve_run", db_path=db_path) is False

    def test_unknown_action_returns_false(self, db_path):
        u = _make_user(db_path, role="owner", email="owner5@test.local")
        assert check_authorization(u.user_id, "totally_unknown_action", db_path=db_path) is False

    def test_all_operator_level_actions_allowed_for_operator(self, db_path):
        u = _make_user(db_path, role="operator", email="op5@test.local")
        operator_actions = [
            "approve_run", "pause_channel", "resume_channel",
            "approve_evolution", "import_package",
        ]
        for action in operator_actions:
            assert check_authorization(u.user_id, action, db_path=db_path) is True, (
                f"operator should be able to {action}"
            )


# ===========================================================================
# TestDashboardIsAuthorized
# ===========================================================================

class TestDashboardIsAuthorized:
    """dashboard_api.is_authorized() wires to auth.check_authorization()."""

    def test_is_authorized_owner_approve_run(self, db_path):
        from hypogeum.dashboard_api import is_authorized
        u = _make_user(db_path, role="owner", email="ow@dash.local")
        assert is_authorized(u.user_id, "approve_run", db_path=db_path) is True

    def test_is_authorized_viewer_approve_run(self, db_path):
        from hypogeum.dashboard_api import is_authorized
        u = _make_user(db_path, role="viewer", email="vi@dash.local")
        assert is_authorized(u.user_id, "approve_run", db_path=db_path) is False

    def test_is_authorized_unknown_user_returns_false(self, db_path):
        from hypogeum.dashboard_api import is_authorized
        assert is_authorized("ghost", "approve_run", db_path=db_path) is False

    def test_is_authorized_does_not_raise_on_bad_input(self, db_path):
        from hypogeum.dashboard_api import is_authorized
        result = is_authorized(None, None, db_path=db_path)
        assert result is False

    def test_existing_get_permission_still_works(self):
        from hypogeum.dashboard_api import get_permission
        p = get_permission("approve_run")
        assert p is not None
        assert p.action == "approve_run"

    def test_existing_list_permissions_still_works(self):
        from hypogeum.dashboard_api import list_permissions
        perms = list_permissions()
        assert len(perms) == 10


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:

    def test_user_crud_opens_no_sockets(self, db_path, monkeypatch):
        def no_socket(*a, **k):
            raise AssertionError("auth opened a socket")
        monkeypatch.setattr(socket, "socket", no_socket)
        u = _make_user(db_path, role="operator")
        get_user(u.user_id, db_path=db_path)
        list_users(db_path=db_path)

    def test_check_authorization_opens_no_sockets(self, db_path, monkeypatch):
        u = _make_user(db_path, role="owner", email="owner_sock@test.local")
        def no_socket(*a, **k):
            raise AssertionError("auth opened a socket")
        monkeypatch.setattr(socket, "socket", no_socket)
        check_authorization(u.user_id, "approve_run", db_path=db_path)


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:

    def test_user_creation_does_not_write_runs(self, db_path):
        _make_user(db_path, role="operator")
        conn = get_connection(db_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            conn.close()
        assert n == 0

    def test_api_key_creation_does_not_write_episodes(self, db_path):
        u = _make_user(db_path, role="admin")
        create_api_key(u.user_id, "label", db_path=db_path)
        conn = get_connection(db_path)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "episodes" in tables:
                n = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
                assert n == 0
        finally:
            conn.close()
