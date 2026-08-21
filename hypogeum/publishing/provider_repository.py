"""
hypogeum/publishing/provider_repository.py — Provider/account/profile/content
operational mappings (ADR-013 Decision 7, item 3).

Sole writer of provider_profiles / provider_accounts / channel_provider_bindings
/ provider_content_mappings (hypogeum/schema.sql). Provider-neutral: nothing
here references any specific provider by name.

Ownership boundary (ADR-013 Decision 3), enforced by construction:
    - Credential Manager (hypogeum.credential_manager, secret_profiles) owns
      credential material and credential metadata ONLY. This module never
      reads or writes secret_profiles.
    - Channel Package (hypogeum.channel_config.PublishingProfile) owns
      logical configuration: enabled platforms, provider preference by
      platform, provider profile/account alias by platform. This module
      only records what a preference RESOLVED TO operationally (external
      IDs, connection status, content mappings) -- never the preference
      itself.
    - This module (the operational database) owns external provider
      profile/account IDs, native platform account IDs, connection/
      reconnection status, provider/native content IDs, and their
      relationships to Cortex channels, publication_intents,
      publishing_attempts, and publishing_receipts.
    - Factory Core (this module included) contains no Hypogeum-specific
      handles, IDs, platform usernames, or any specific-provider branching.

Public API:
    upsert_provider_profile(...)          -> dict   (idempotent by provider_id+external_profile_id)
    get_provider_profile(...)             -> Optional[dict]  (read-only, by internal PK)
    upsert_provider_account(...)          -> dict   (idempotent by provider_id+platform+external_account_id)
    get_provider_account(...)             -> Optional[dict]  (read-only, by internal PK)
    bind_channel_provider(...)            -> dict   (fails closed on a conflicting active binding)
    resolve_active_binding(...)           -> Optional[dict]
    list_bindings(...)                    -> list[dict]
    persist_content_mapping(...)          -> dict
    resolve_content_mapping(...)          -> list[dict]
    mark_connection_status(...)           -> dict
    disable_binding(...)                  -> dict   (never deletes -- history preserved)

Importing this module, or resolving a provider elsewhere, performs no DB
write -- every write here happens inside an explicitly-called function.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema

_MAX_METADATA_STRING_LEN = 2000

# Same defense-in-depth policy as hypogeum.publishing.submission_ledger:
# redact anything credential-shaped before it is ever persisted, whether it
# arrives as a metadata dict value or a dict key name.
_SECRET_VALUE_PATTERNS = [
    re.compile(r"(?i)(authorization\s*:\s*).+?(?=,|;|$)"),
    re.compile(r"(?i)(bearer\s+)\S+"),
    re.compile(r"(?i)([a-z0-9_\-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret)[a-z0-9_\-]*\s*[:=]\s*)\S+"),
]
_CREDENTIAL_KEY_PATTERN = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|cookie|"
    r"authorization|auth[_-]?header)"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _sanitize_value(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _CREDENTIAL_KEY_PATTERN.search(str(k)):
                out[k] = "[REDACTED]"
            else:
                out[k] = _sanitize_value(v)
        return out
    if isinstance(value, list):
        return [_sanitize_value(v) for v in value]
    if isinstance(value, str):
        text = value
        for pattern in _SECRET_VALUE_PATTERNS:
            text = pattern.sub(r"\1[REDACTED]", text)
        return text[:_MAX_METADATA_STRING_LEN]
    return value


def _sanitize_metadata_json(metadata: Optional[dict]) -> Optional[str]:
    """Sanitize and serialize a metadata dict. Never raises. None stays None."""
    if metadata is None:
        return None
    try:
        return json.dumps(_sanitize_value(metadata))
    except Exception:
        return json.dumps({"_unserializable_metadata": True})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ProviderRepositoryError(RuntimeError):
    """Base class for provider-repository failures. Always fail closed."""


class DuplicateActiveBindingError(ProviderRepositoryError):
    """
    Raised when a caller attempts to bind (channel_id, capability, platform,
    logical_alias) to different provider resources while an active binding
    for that exact key already exists. No automatic ordered-fallback is
    implemented in this slice -- the caller must explicitly disable the
    existing binding first if it should be replaced.
    """


class ProviderAccountNotFoundError(ProviderRepositoryError):
    pass


class BindingNotFoundError(ProviderRepositoryError):
    pass


class AmbiguousContentMappingLookupError(ProviderRepositoryError):
    pass


# ---------------------------------------------------------------------------
# Row -> dict helper
# ---------------------------------------------------------------------------

def _row(conn: sqlite3.Connection, table: str, id_col: str, id_val: str) -> Optional[dict]:
    row = conn.execute(f"SELECT * FROM {table} WHERE {id_col} = ?", (id_val,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# provider_profiles
# ---------------------------------------------------------------------------

def upsert_provider_profile(
    db_path: Optional[Path],
    *,
    provider_id: str,
    external_profile_id: str,
    display_label: Optional[str] = None,
    status: str = "active",
    metadata: Optional[dict] = None,
) -> dict:
    """
    Idempotent upsert keyed on UNIQUE(provider_id, external_profile_id).
    A second call with the same (provider_id, external_profile_id) updates
    the existing row in place (display_label/status/metadata/updated_at/
    last_verified_at) rather than creating a duplicate profile.
    """
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now_iso()
            existing = conn.execute(
                "SELECT * FROM provider_profiles WHERE provider_id = ? AND external_profile_id = ?",
                (provider_id, external_profile_id),
            ).fetchone()
            metadata_json = _sanitize_metadata_json(metadata)
            if existing is not None:
                profile_id = existing["provider_profile_id"]
                conn.execute(
                    """
                    UPDATE provider_profiles
                    SET display_label = COALESCE(?, display_label),
                        status = ?,
                        metadata_json = COALESCE(?, metadata_json),
                        updated_at = ?,
                        last_verified_at = ?
                    WHERE provider_profile_id = ?
                    """,
                    (display_label, status, metadata_json, now, now, profile_id),
                )
            else:
                profile_id = _new_id("provprofile")
                conn.execute(
                    """
                    INSERT INTO provider_profiles
                        (provider_profile_id, provider_id, external_profile_id, display_label,
                         status, metadata_json, created_at, updated_at, last_verified_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (profile_id, provider_id, external_profile_id, display_label,
                     status, metadata_json, now, now, now),
                )
            conn.commit()
            return _row(conn, "provider_profiles", "provider_profile_id", profile_id)
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def get_provider_profile(db_path: Optional[Path], *, provider_profile_id: str) -> Optional[dict]:
    """Read-only lookup by internal Cortex PK. Returns None if unknown."""
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        return _row(conn, "provider_profiles", "provider_profile_id", provider_profile_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# provider_accounts
# ---------------------------------------------------------------------------

def upsert_provider_account(
    db_path: Optional[Path],
    *,
    provider_id: str,
    platform: str,
    external_account_id: str,
    provider_profile_id: Optional[str] = None,
    native_platform_account_id: Optional[str] = None,
    display_label: Optional[str] = None,
    status: str = "active",
    needs_reconnection: bool = False,
    metadata: Optional[dict] = None,
) -> dict:
    """
    Idempotent upsert keyed on UNIQUE(provider_id, platform, external_account_id).
    One provider_profile_id may own several provider_accounts (one per
    platform, or more) -- provider stays separate from platform throughout.
    """
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now_iso()
            existing = conn.execute(
                """
                SELECT * FROM provider_accounts
                WHERE provider_id = ? AND platform = ? AND external_account_id = ?
                """,
                (provider_id, platform, external_account_id),
            ).fetchone()
            metadata_json = _sanitize_metadata_json(metadata)
            if existing is not None:
                account_id = existing["provider_account_id"]
                conn.execute(
                    """
                    UPDATE provider_accounts
                    SET provider_profile_id = COALESCE(?, provider_profile_id),
                        native_platform_account_id = COALESCE(?, native_platform_account_id),
                        display_label = COALESCE(?, display_label),
                        status = ?,
                        needs_reconnection = ?,
                        metadata_json = COALESCE(?, metadata_json),
                        updated_at = ?,
                        last_verified_at = ?
                    WHERE provider_account_id = ?
                    """,
                    (provider_profile_id, native_platform_account_id, display_label,
                     status, int(needs_reconnection), metadata_json, now, now, account_id),
                )
            else:
                account_id = _new_id("provaccount")
                conn.execute(
                    """
                    INSERT INTO provider_accounts
                        (provider_account_id, provider_profile_id, provider_id, platform,
                         external_account_id, native_platform_account_id, display_label,
                         status, needs_reconnection, metadata_json, created_at, updated_at,
                         last_verified_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (account_id, provider_profile_id, provider_id, platform,
                     external_account_id, native_platform_account_id, display_label,
                     status, int(needs_reconnection), metadata_json, now, now, now),
                )
            conn.commit()
            return _row(conn, "provider_accounts", "provider_account_id", account_id)
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def get_provider_account(db_path: Optional[Path], *, provider_account_id: str) -> Optional[dict]:
    """Read-only lookup by internal Cortex PK. Returns None if unknown."""
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        return _row(conn, "provider_accounts", "provider_account_id", provider_account_id)
    finally:
        conn.close()


def mark_connection_status(
    db_path: Optional[Path],
    *,
    provider_account_id: str,
    status: Optional[str] = None,
    needs_reconnection: Optional[bool] = None,
) -> dict:
    """Update connection/reconnection status for an existing provider_account
    without touching any other field. Fails closed if the account is unknown."""
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = _row(conn, "provider_accounts", "provider_account_id", provider_account_id)
            if existing is None:
                raise ProviderAccountNotFoundError(
                    f"unknown provider_account_id: {provider_account_id!r}"
                )
            now = _now_iso()
            conn.execute(
                """
                UPDATE provider_accounts
                SET status = COALESCE(?, status),
                    needs_reconnection = COALESCE(?, needs_reconnection),
                    updated_at = ?,
                    last_verified_at = ?
                WHERE provider_account_id = ?
                """,
                (status, int(needs_reconnection) if needs_reconnection is not None else None,
                 now, now, provider_account_id),
            )
            conn.commit()
            return _row(conn, "provider_accounts", "provider_account_id", provider_account_id)
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# channel_provider_bindings
# ---------------------------------------------------------------------------

def bind_channel_provider(
    db_path: Optional[Path],
    *,
    channel_id: str,
    capability: str,
    platform: str,
    provider_id: str,
    provider_profile_id: Optional[str] = None,
    provider_account_id: Optional[str] = None,
    logical_alias: str = "default",
) -> dict:
    """
    Bind one Cortex channel to provider resources for one capability
    ('publishing'|'analytics') and one destination platform.

    At most one ENABLED binding may exist for
    (channel_id, capability, platform, logical_alias). A second call with
    the identical key and identical provider/profile/account is an
    idempotent no-op (touches updated_at only). A second call with the
    same key but DIFFERENT provider/profile/account raises
    DuplicateActiveBindingError -- no automatic fallback is invented here;
    the caller must explicitly disable_binding() the existing one first.
    """
    if capability not in ("publishing", "analytics"):
        raise ValueError(f"capability must be 'publishing' or 'analytics', got {capability!r}")

    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now_iso()
            existing = conn.execute(
                """
                SELECT * FROM channel_provider_bindings
                WHERE channel_id = ? AND capability = ? AND platform = ? AND logical_alias = ?
                  AND enabled = 1
                """,
                (channel_id, capability, platform, logical_alias),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["provider_id"] == provider_id
                    and existing["provider_profile_id"] == provider_profile_id
                    and existing["provider_account_id"] == provider_account_id
                )
                if not same:
                    conn.rollback()
                    raise DuplicateActiveBindingError(
                        f"an active binding already exists for channel_id={channel_id!r} "
                        f"capability={capability!r} platform={platform!r} "
                        f"logical_alias={logical_alias!r} (provider_id={existing['provider_id']!r}); "
                        f"disable it first before binding a different provider/profile/account"
                    )
                conn.execute(
                    "UPDATE channel_provider_bindings SET updated_at = ? WHERE binding_id = ?",
                    (now, existing["binding_id"]),
                )
                conn.commit()
                return _row(conn, "channel_provider_bindings", "binding_id", existing["binding_id"])

            binding_id = _new_id("binding")
            conn.execute(
                """
                INSERT INTO channel_provider_bindings
                    (binding_id, channel_id, capability, platform, provider_id,
                     provider_profile_id, provider_account_id, logical_alias, enabled,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (binding_id, channel_id, capability, platform, provider_id,
                 provider_profile_id, provider_account_id, logical_alias, now, now),
            )
            conn.commit()
            return _row(conn, "channel_provider_bindings", "binding_id", binding_id)
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def resolve_active_binding(
    db_path: Optional[Path],
    *,
    channel_id: str,
    capability: str,
    platform: str,
    logical_alias: str = "default",
) -> Optional[dict]:
    """Resolve the single enabled binding for this key, or None."""
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """
            SELECT * FROM channel_provider_bindings
            WHERE channel_id = ? AND capability = ? AND platform = ? AND logical_alias = ?
              AND enabled = 1
            """,
            (channel_id, capability, platform, logical_alias),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_bindings(
    db_path: Optional[Path],
    *,
    channel_id: str,
    capability: Optional[str] = None,
    platform: Optional[str] = None,
) -> list:
    """List bindings (enabled and disabled -- full history) for a channel,
    optionally narrowed by capability and/or platform."""
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        clauses = ["channel_id = ?"]
        params: list = [channel_id]
        if capability is not None:
            clauses.append("capability = ?")
            params.append(capability)
        if platform is not None:
            clauses.append("platform = ?")
            params.append(platform)
        rows = conn.execute(
            f"SELECT * FROM channel_provider_bindings WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def disable_binding(db_path: Optional[Path], *, binding_id: str) -> dict:
    """Disable a binding without deleting it -- history is preserved and
    remains queryable via list_bindings(). Fails closed if unknown."""
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = _row(conn, "channel_provider_bindings", "binding_id", binding_id)
            if existing is None:
                raise BindingNotFoundError(f"unknown binding_id: {binding_id!r}")
            now = _now_iso()
            conn.execute(
                "UPDATE channel_provider_bindings SET enabled = 0, updated_at = ? WHERE binding_id = ?",
                (now, binding_id),
            )
            conn.commit()
            return _row(conn, "channel_provider_bindings", "binding_id", binding_id)
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# provider_content_mappings
# ---------------------------------------------------------------------------

def _insert_content_mapping_in_txn(
    conn: sqlite3.Connection,
    *,
    provider_id: str,
    platform: str,
    channel_id: Optional[str],
    episode_id: str,
    deliverable_id: str,
    provider_profile_id: Optional[str] = None,
    provider_account_id: Optional[str] = None,
    provider_content_id: Optional[str] = None,
    native_platform_content_id: Optional[str] = None,
    publication_intent_id: Optional[str] = None,
    attempt_id: Optional[str] = None,
    receipt_id: Optional[str] = None,
    status: str = "confirmed",
    now: str,
) -> dict:
    """
    Must run inside an already-open transaction on `conn`. This is the
    in-transaction primitive used by both persist_content_mapping() (its
    own standalone transaction) and
    hypogeum.publishing.submission_ledger.record_success() (wired directly
    into the receipt-persistence boundary, same transaction as the receipt
    insert -- see that function's own docstring for why: only a confirmed
    receipt may ever produce a mapping row).
    """
    mapping_id = _new_id("contentmap")
    conn.execute(
        """
        INSERT INTO provider_content_mappings
            (mapping_id, provider_id, platform, provider_profile_id, provider_account_id,
             provider_content_id, native_platform_content_id, channel_id, episode_id,
             deliverable_id, publication_intent_id, attempt_id, receipt_id, status,
             created_at, updated_at, last_verified_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (mapping_id, provider_id, platform, provider_profile_id, provider_account_id,
         provider_content_id, native_platform_content_id, channel_id, episode_id,
         deliverable_id, publication_intent_id, attempt_id, receipt_id, status,
         now, now, now),
    )
    return _row(conn, "provider_content_mappings", "mapping_id", mapping_id)


def persist_content_mapping(
    db_path: Optional[Path],
    *,
    provider_id: str,
    platform: str,
    channel_id: Optional[str],
    episode_id: str,
    deliverable_id: str,
    provider_profile_id: Optional[str] = None,
    provider_account_id: Optional[str] = None,
    provider_content_id: Optional[str] = None,
    native_platform_content_id: Optional[str] = None,
    publication_intent_id: Optional[str] = None,
    attempt_id: Optional[str] = None,
    receipt_id: Optional[str] = None,
    status: str = "confirmed",
) -> dict:
    """Standalone entry point (own transaction). Production code should
    prefer letting hypogeum.publishing.submission_ledger.record_success()
    create the mapping automatically at the receipt boundary; this function
    exists for direct/administrative use and tests."""
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now_iso()
            result = _insert_content_mapping_in_txn(
                conn,
                provider_id=provider_id, platform=platform, channel_id=channel_id,
                episode_id=episode_id, deliverable_id=deliverable_id,
                provider_profile_id=provider_profile_id, provider_account_id=provider_account_id,
                provider_content_id=provider_content_id,
                native_platform_content_id=native_platform_content_id,
                publication_intent_id=publication_intent_id, attempt_id=attempt_id,
                receipt_id=receipt_id, status=status, now=now,
            )
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def resolve_content_mapping(
    db_path: Optional[Path],
    *,
    publication_intent_id: Optional[str] = None,
    attempt_id: Optional[str] = None,
    receipt_id: Optional[str] = None,
) -> list:
    """
    Resolve provider_content_mappings by exactly one of publication_intent_id,
    attempt_id, or receipt_id. Returns a list (a publication_intent_id can
    have produced several mappings across retries/platforms; attempt_id and
    receipt_id each resolve to at most one row in practice, still returned
    as a list for a uniform contract).
    """
    given = [k for k, v in (
        ("publication_intent_id", publication_intent_id),
        ("attempt_id", attempt_id),
        ("receipt_id", receipt_id),
    ) if v is not None]
    if len(given) != 1:
        raise AmbiguousContentMappingLookupError(
            "resolve_content_mapping() requires exactly one of publication_intent_id, "
            f"attempt_id, receipt_id -- got: {given}"
        )
    col = given[0]
    value = {"publication_intent_id": publication_intent_id,
             "attempt_id": attempt_id, "receipt_id": receipt_id}[col]

    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM provider_content_mappings WHERE {col} = ? ORDER BY created_at",
            (value,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
