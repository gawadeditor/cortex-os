"""
hypogeum/auth.py — User authentication and authorization

Public API:
    create_user(email, name, role, user_id=None, db_path=None) -> UserRecord
    get_user(user_id, db_path=None) -> Optional[UserRecord]
    list_users(db_path=None) -> list[UserRecord]
    disable_user(user_id, db_path=None) -> bool

    create_api_key(user_id, label, db_path=None) -> tuple[str, str]
      # returns (key_id, raw_key); raw_key shown once, never stored
    revoke_api_key(key_id, db_path=None) -> bool
    authenticate_api_key(raw_key, db_path=None) -> Optional[UserRecord]
      # hashes raw_key (SHA-256), looks up key_hash, returns user if active

    check_authorization(user_id, action, db_path=None) -> bool
      # looks up user role; validates against PERMISSION_TABLE[action].requires_level
      # role hierarchy: owner > admin > operator > editor > viewer > client

Role hierarchy (lower level number = more authority):
    owner=0 > admin=1 > operator=2 > editor=3 > viewer=4 > client=5

requires_level mapping to minimum role:
    "operator"  → level 2  (owner, admin, operator may perform)
    "admin"     → level 1  (owner, admin may perform)
    "emergency" → level 0  (owner only)
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

VALID_ROLES = ("owner", "admin", "operator", "editor", "viewer", "client")

_ROLE_LEVEL: dict[str, int] = {
    "owner":    0,
    "admin":    1,
    "operator": 2,
    "editor":   3,
    "viewer":   4,
    "client":   5,
}

_REQUIRED_LEVEL: dict[str, int] = {
    "operator":  _ROLE_LEVEL["operator"],   # 2 — owner, admin, operator
    "admin":     _ROLE_LEVEL["admin"],      # 1 — owner, admin
    "emergency": _ROLE_LEVEL["owner"],      # 0 — owner only
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------

@dataclass
class UserRecord:
    user_id:    str
    email:      str
    name:       str
    role:       str
    status:     str
    created_at: str
    updated_at: str


@dataclass
class ApiKeyRecord:
    key_id:       str
    user_id:      str
    key_hash:     str
    label:        str
    status:       str
    last_used_at: Optional[str]
    expires_at:   Optional[str]
    created_at:   str


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------

def create_user(
    email: str,
    name: str,
    role: str,
    user_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> UserRecord:
    """
    Create a new user. Raises ValueError for invalid role.
    Raises sqlite3.IntegrityError if email is not unique.
    """
    if role not in VALID_ROLES:
        raise ValueError(
            f"Invalid role {role!r}. Must be one of: {', '.join(VALID_ROLES)}"
        )
    uid = user_id or str(uuid.uuid4())
    now = _now_iso()
    conn = get_connection(_resolve_db_path(db_path))
    try:
        conn.execute(
            """
            INSERT INTO users (user_id, email, name, role, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'active', ?, ?)
            """,
            (uid, email, name, role, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return UserRecord(
        user_id=uid, email=email, name=name, role=role,
        status="active", created_at=now, updated_at=now,
    )


def get_user(user_id: str, db_path: Optional[Path] = None) -> Optional[UserRecord]:
    """Return UserRecord for user_id, or None if not found."""
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return UserRecord(
            user_id=row["user_id"], email=row["email"], name=row["name"],
            role=row["role"], status=row["status"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )
    except Exception:
        return None


def list_users(db_path: Optional[Path] = None) -> list:
    """Return all users ordered by creation time."""
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()
        return [
            UserRecord(
                user_id=r["user_id"], email=r["email"], name=r["name"],
                role=r["role"], status=r["status"],
                created_at=r["created_at"], updated_at=r["updated_at"],
            )
            for r in rows
        ]
    except Exception:
        return []


def disable_user(user_id: str, db_path: Optional[Path] = None) -> bool:
    """Set user status to 'disabled'. Returns True on success."""
    now = _now_iso()
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            conn.execute(
                "UPDATE users SET status = 'disabled', updated_at = ? WHERE user_id = ?",
                (now, user_id),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# API key management
# ---------------------------------------------------------------------------

def create_api_key(
    user_id: str,
    label: str,
    db_path: Optional[Path] = None,
) -> tuple:
    """
    Create an API key for user_id. Returns (key_id, raw_key).

    The raw_key is shown once and never stored; only its SHA-256 hash
    is persisted in the api_keys table.
    """
    key_id = str(uuid.uuid4())
    raw_key = f"hbk_{secrets.token_hex(32)}"
    key_hash = _hash_key(raw_key)
    now = _now_iso()
    conn = get_connection(_resolve_db_path(db_path))
    try:
        conn.execute(
            """
            INSERT INTO api_keys (key_id, user_id, key_hash, label, status, created_at)
            VALUES (?, ?, ?, ?, 'active', ?)
            """,
            (key_id, user_id, key_hash, label, now),
        )
        conn.commit()
    finally:
        conn.close()
    return key_id, raw_key


def revoke_api_key(key_id: str, db_path: Optional[Path] = None) -> bool:
    """Set key status to 'revoked'. Returns True on success."""
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            conn.execute(
                "UPDATE api_keys SET status = 'revoked' WHERE key_id = ?",
                (key_id,),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def authenticate_api_key(
    raw_key: str,
    db_path: Optional[Path] = None,
) -> Optional[UserRecord]:
    """
    Authenticate raw_key. Returns owning UserRecord if the key is active
    and the user is active. Returns None otherwise.

    Updates api_keys.last_used_at on successful lookup.
    """
    key_hash = _hash_key(raw_key)
    path = _resolve_db_path(db_path)
    try:
        conn = get_connection(path)
        try:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ? AND status = 'active'",
                (key_hash,),
            ).fetchone()
            if row is None:
                return None
            user_id = row["user_id"]
            try:
                conn.execute(
                    "UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?",
                    (_now_iso(), key_hash),
                )
                conn.commit()
            except Exception:
                pass
        finally:
            conn.close()
        user = get_user(user_id, db_path=db_path)
        if user is None or user.status != "active":
            return None
        return user
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

def check_authorization(
    user_id: str,
    action: str,
    db_path: Optional[Path] = None,
) -> bool:
    """
    Return True if user_id is authorized to perform action.

    Looks up the user's role and validates it against
    PERMISSION_TABLE[action].requires_level using the role hierarchy:
        owner > admin > operator > editor > viewer > client

    Returns False if:
    - user does not exist or is disabled
    - action is not defined in PERMISSION_TABLE
    - role or requires_level is unrecognized
    """
    from hypogeum.dashboard_api import PERMISSION_TABLE

    perm = PERMISSION_TABLE.get(action)
    if perm is None:
        return False

    user = get_user(user_id, db_path=db_path)
    if user is None or user.status != "active":
        return False

    required_level = _REQUIRED_LEVEL.get(perm.requires_level)
    if required_level is None:
        return False

    user_level = _ROLE_LEVEL.get(user.role)
    if user_level is None:
        return False

    return user_level <= required_level
