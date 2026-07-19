"""
hypogeum/credential_manager.py — Credential / Secret Profile Manager

Stores provider credentials in the `secret_profiles` DB table.
V1 note: credentials stored as plaintext; acceptable for local self-hosted
single-machine deployment. Encryption is a V2 SaaS concern.

Public API:
    create_secret_profile(provider, label, credential_type, credential_data,
                          scope="factory", channel_id=None, expires_at=None,
                          secret_profile_id=None, user_id=None,
                          db_path=None) -> SecretProfile

    get_secret_profile(secret_profile_id, db_path=None) -> Optional[SecretProfile]
    list_secret_profiles(provider=None, scope=None, db_path=None) -> list[SecretProfile]

    update_secret_profile(secret_profile_id, credential_data=None, expires_at=None,
                          label=None, user_id=None, db_path=None) -> bool

    revoke_secret_profile(secret_profile_id, user_id=None, db_path=None) -> bool
    delete_secret_profile(secret_profile_id, user_id=None, db_path=None) -> bool

    resolve_credential(secret_profile_id, db_path=None) -> str
      # Returns credential_data. Updates last_used_at.
      # Raises CredentialRevokedError if status != 'active'.

    test_secret_profile(secret_profile_id, db_path=None) -> CredentialTestResult
      # dataclass: success: bool, latency_ms: int, error: Optional[str]
      # Fires a provider-specific minimal HTTP probe to validate the credential.
      # Updates last_tested_at on success.
      # Supported providers: openrouter, perplexity, muapi, elevenlabs, youtube

Authorization:
    create_secret_profile(), update_secret_profile(), revoke_secret_profile(),
    delete_secret_profile() each accept an optional user_id. When provided,
    the caller must hold at least admin role (enforced via check_authorization).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

VALID_CREDENTIAL_TYPES = ("api_key", "oauth2_token", "webhook_url", "username_password")
VALID_SCOPES = ("factory", "channel-specific")
VALID_STATUSES = ("active", "expired", "revoked", "testing")

# Proxy admin-level action used for authorization checks.
# override_model requires admin or above — the correct threshold for credential management.
_CREDENTIAL_AUTH_ACTION = "override_model"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CredentialRevokedError(Exception):
    """Raised by resolve_credential() when the profile is not active."""
    def __init__(self, secret_profile_id: str, status: str) -> None:
        self.secret_profile_id = secret_profile_id
        self.status = status
        super().__init__(
            f"Credential {secret_profile_id!r} cannot be resolved (status={status!r}). "
            f"Only 'active' profiles may be resolved."
        )


class PermissionDeniedError(Exception):
    """Raised when the calling user lacks the required role."""
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        super().__init__(
            f"User {user_id!r} does not have the required role (admin or owner) "
            f"to manage credential profiles."
        )


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------

@dataclass
class SecretProfile:
    secret_profile_id: str
    provider:          str
    label:             str
    credential_type:   str
    credential_data:   str
    status:            str
    scope:             str
    channel_id:        Optional[str]
    expires_at:        Optional[str]
    last_tested_at:    Optional[str]
    last_used_at:      Optional[str]
    created_at:        str
    updated_at:        str


@dataclass
class CredentialTestResult:
    success:    bool
    latency_ms: int
    error:      Optional[str]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _require_admin_or_owner(user_id: Optional[str], db_path: Optional[Path]) -> None:
    """Raise PermissionDeniedError if user_id is provided but not admin/owner."""
    if user_id is None:
        return
    from hypogeum.auth import check_authorization
    if not check_authorization(user_id, _CREDENTIAL_AUTH_ACTION, db_path=db_path):
        raise PermissionDeniedError(user_id)


def _row_to_profile(row) -> SecretProfile:
    return SecretProfile(
        secret_profile_id=row["secret_profile_id"],
        provider=row["provider"],
        label=row["label"],
        credential_type=row["credential_type"],
        credential_data=row["credential_data"],
        status=row["status"],
        scope=row["scope"],
        channel_id=row["channel_id"],
        expires_at=row["expires_at"],
        last_tested_at=row["last_tested_at"],
        last_used_at=row["last_used_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_secret_profile(
    provider: str,
    label: str,
    credential_type: str,
    credential_data: str,
    scope: str = "factory",
    channel_id: Optional[str] = None,
    expires_at: Optional[str] = None,
    secret_profile_id: Optional[str] = None,
    user_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> SecretProfile:
    """
    Create a new secret profile. Returns the created SecretProfile.

    Raises ValueError for invalid credential_type or scope.
    Raises PermissionDeniedError if user_id is provided but not admin/owner.
    """
    if credential_type not in VALID_CREDENTIAL_TYPES:
        raise ValueError(
            f"Invalid credential_type {credential_type!r}. "
            f"Must be one of: {', '.join(VALID_CREDENTIAL_TYPES)}"
        )
    if scope not in VALID_SCOPES:
        raise ValueError(
            f"Invalid scope {scope!r}. Must be one of: {', '.join(VALID_SCOPES)}"
        )
    _require_admin_or_owner(user_id, db_path)

    pid = secret_profile_id or str(uuid.uuid4())
    now = _now_iso()
    conn = get_connection(_resolve_db_path(db_path))
    try:
        conn.execute(
            """
            INSERT INTO secret_profiles (
                secret_profile_id, provider, label, credential_type,
                credential_data, status, scope, channel_id, expires_at,
                last_tested_at, last_used_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (pid, provider, label, credential_type, credential_data,
             scope, channel_id, expires_at, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return SecretProfile(
        secret_profile_id=pid,
        provider=provider,
        label=label,
        credential_type=credential_type,
        credential_data=credential_data,
        status="active",
        scope=scope,
        channel_id=channel_id,
        expires_at=expires_at,
        last_tested_at=None,
        last_used_at=None,
        created_at=now,
        updated_at=now,
    )


def get_secret_profile(
    secret_profile_id: str,
    db_path: Optional[Path] = None,
) -> Optional[SecretProfile]:
    """Return SecretProfile for secret_profile_id, or None if not found."""
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            row = conn.execute(
                "SELECT * FROM secret_profiles WHERE secret_profile_id = ?",
                (secret_profile_id,),
            ).fetchone()
        finally:
            conn.close()
        return _row_to_profile(row) if row is not None else None
    except Exception:
        return None


def list_secret_profiles(
    provider: Optional[str] = None,
    scope: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list:
    """
    Return all secret profiles, optionally filtered by provider and/or scope.
    Returns [] on any error.
    """
    try:
        clauses = []
        params: list = []
        if provider is not None:
            clauses.append("provider = ?")
            params.append(provider)
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM secret_profiles {where} ORDER BY created_at"
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [_row_to_profile(r) for r in rows]
    except Exception:
        return []


def update_secret_profile(
    secret_profile_id: str,
    credential_data: Optional[str] = None,
    expires_at: Optional[str] = None,
    label: Optional[str] = None,
    user_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """
    Update mutable fields on a secret profile. Returns True on success.
    Only non-None arguments are written; passing all None is a no-op.
    Raises PermissionDeniedError if user_id is provided but not admin/owner.
    """
    _require_admin_or_owner(user_id, db_path)

    updates: dict = {}
    if credential_data is not None:
        updates["credential_data"] = credential_data
    if expires_at is not None:
        updates["expires_at"] = expires_at
    if label is not None:
        updates["label"] = label
    if not updates:
        return True

    updates["updated_at"] = _now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [secret_profile_id]

    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            conn.execute(
                f"UPDATE secret_profiles SET {set_clause} WHERE secret_profile_id = ?",
                values,
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def revoke_secret_profile(
    secret_profile_id: str,
    user_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """
    Set secret profile status to 'revoked'. Returns True on success.
    Raises PermissionDeniedError if user_id is provided but not admin/owner.
    """
    _require_admin_or_owner(user_id, db_path)
    now = _now_iso()
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            conn.execute(
                "UPDATE secret_profiles SET status = 'revoked', updated_at = ? "
                "WHERE secret_profile_id = ?",
                (now, secret_profile_id),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def delete_secret_profile(
    secret_profile_id: str,
    user_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """
    Permanently delete a secret profile row. Returns True on success.
    Prefer revoke_secret_profile() for audit trail preservation.
    Raises PermissionDeniedError if user_id is provided but not admin/owner.
    """
    _require_admin_or_owner(user_id, db_path)
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            conn.execute(
                "DELETE FROM secret_profiles WHERE secret_profile_id = ?",
                (secret_profile_id,),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------

def resolve_credential(
    secret_profile_id: str,
    db_path: Optional[Path] = None,
) -> str:
    """
    Return the credential_data for a secret profile and update last_used_at.

    Raises CredentialRevokedError if the profile status is not 'active'.
    Raises KeyError if the profile does not exist.
    """
    profile = get_secret_profile(secret_profile_id, db_path=db_path)
    if profile is None:
        raise KeyError(f"Secret profile {secret_profile_id!r} not found.")
    if profile.status != "active":
        raise CredentialRevokedError(secret_profile_id, profile.status)

    now = _now_iso()
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            conn.execute(
                "UPDATE secret_profiles SET last_used_at = ? WHERE secret_profile_id = ?",
                (now, secret_profile_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass

    return profile.credential_data


# ---------------------------------------------------------------------------
# Credential testing
# ---------------------------------------------------------------------------

def test_secret_profile(
    secret_profile_id: str,
    db_path: Optional[Path] = None,
) -> CredentialTestResult:
    """
    Probe the credential with a minimal provider-specific HTTP call.

    Updates last_tested_at on the profile when the probe succeeds.
    Supported providers: openrouter, perplexity, muapi, elevenlabs, youtube.
    Unknown providers return success=False with a descriptive error.
    """
    profile = get_secret_profile(secret_profile_id, db_path=db_path)
    if profile is None:
        return CredentialTestResult(success=False, latency_ms=0,
                          error=f"Profile {secret_profile_id!r} not found")
    if profile.status != "active":
        return CredentialTestResult(success=False, latency_ms=0,
                          error=f"Profile status is {profile.status!r} (not active)")

    tester = _PROVIDER_TESTERS.get(profile.provider)
    if tester is None:
        return CredentialTestResult(
            success=False, latency_ms=0,
            error=f"No test implementation for provider {profile.provider!r}",
        )

    result = tester(profile.credential_data)

    if result.success:
        now = _now_iso()
        try:
            conn = get_connection(_resolve_db_path(db_path))
            try:
                conn.execute(
                    "UPDATE secret_profiles SET last_tested_at = ?, updated_at = ? "
                    "WHERE secret_profile_id = ?",
                    (now, now, secret_profile_id),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Provider-specific test probes
# ---------------------------------------------------------------------------

def _test_openrouter(credential_data: str) -> CredentialTestResult:
    import requests
    t0 = time.monotonic()
    try:
        resp = requests.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {credential_data}"},
            timeout=10,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code == 200:
            return CredentialTestResult(success=True, latency_ms=latency_ms, error=None)
        return CredentialTestResult(success=False, latency_ms=latency_ms,
                          error=f"HTTP {resp.status_code}")
    except Exception as exc:
        return CredentialTestResult(success=False,
                          latency_ms=int((time.monotonic() - t0) * 1000),
                          error=str(exc))


def _test_perplexity(credential_data: str) -> CredentialTestResult:
    import requests
    t0 = time.monotonic()
    try:
        resp = requests.get(
            "https://api.perplexity.ai/",
            headers={"Authorization": f"Bearer {credential_data}"},
            timeout=10,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        # Perplexity returns 200 or 401/403 on key validity
        if resp.status_code in (200, 404):
            return CredentialTestResult(success=True, latency_ms=latency_ms, error=None)
        return CredentialTestResult(success=False, latency_ms=latency_ms,
                          error=f"HTTP {resp.status_code}")
    except Exception as exc:
        return CredentialTestResult(success=False,
                          latency_ms=int((time.monotonic() - t0) * 1000),
                          error=str(exc))


def _test_muapi(credential_data: str) -> CredentialTestResult:
    import requests
    t0 = time.monotonic()
    try:
        resp = requests.get(
            "https://api.muapi.ai/api/v1/user/balance",
            headers={"Authorization": f"Bearer {credential_data}"},
            timeout=10,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code in (200, 201):
            return CredentialTestResult(success=True, latency_ms=latency_ms, error=None)
        return CredentialTestResult(success=False, latency_ms=latency_ms,
                          error=f"HTTP {resp.status_code}")
    except Exception as exc:
        return CredentialTestResult(success=False,
                          latency_ms=int((time.monotonic() - t0) * 1000),
                          error=str(exc))


def _test_elevenlabs(credential_data: str) -> CredentialTestResult:
    import requests
    t0 = time.monotonic()
    try:
        resp = requests.get(
            "https://api.elevenlabs.io/v1/user",
            headers={"xi-api-key": credential_data},
            timeout=10,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code == 200:
            return CredentialTestResult(success=True, latency_ms=latency_ms, error=None)
        return CredentialTestResult(success=False, latency_ms=latency_ms,
                          error=f"HTTP {resp.status_code}")
    except Exception as exc:
        return CredentialTestResult(success=False,
                          latency_ms=int((time.monotonic() - t0) * 1000),
                          error=str(exc))


def _test_youtube(credential_data: str) -> CredentialTestResult:
    """
    Validate a YouTube OAuth refresh token via the real Google OAuth2 token
    exchange — the same shared primitive real publishing uses
    (hypogeum.publishing.youtube.exchange_refresh_token()) and
    hypogeum.provider_verifier._probe_youtube() calls, so the credential
    test path and provider verification path can never disagree about
    what "valid" means.
    """
    import os
    from hypogeum.publishing.youtube import exchange_refresh_token
    from hypogeum.publishing.retry import HardFailureError, SoftFailureError

    t0 = time.monotonic()
    client_id = os.environ.get("YOUTUBE_CLIENT_ID", "")
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "")

    try:
        exchange_refresh_token(refresh_token=credential_data, client_id=client_id, client_secret=client_secret)
        return CredentialTestResult(success=True, latency_ms=int((time.monotonic() - t0) * 1000), error=None)
    except HardFailureError as exc:
        detail = exc.error_code or (f"http_{exc.status_code}" if exc.status_code else "unexpected_response")
        return CredentialTestResult(success=False, latency_ms=int((time.monotonic() - t0) * 1000), error=detail)
    except SoftFailureError as exc:
        return CredentialTestResult(success=False, latency_ms=int((time.monotonic() - t0) * 1000),
                          error=f"transient: {exc}")


_PROVIDER_TESTERS = {
    "openrouter": _test_openrouter,
    "perplexity": _test_perplexity,
    "muapi":      _test_muapi,
    "elevenlabs": _test_elevenlabs,
    "youtube":    _test_youtube,
}
