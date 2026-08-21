"""
hypogeum/publishing/media_staging.py — Generic, provider-neutral durable
media-staging contract and write-ahead boundary.

Answers the blocker identified in
docs/operations/audits/ZERNIO_OPERATIONAL_ENABLEMENT_READINESS_2026-08-21.md
§4/§7: no working public-media-URL path exists for any platform. This
module defines what "staged media" means generically (Factory Core level)
and the durable boundary around staging attempts; it never references any
specific provider's endpoint, request field, response field, or
credential shape. hypogeum.publishing.zernio_media_staging is the sole
Zernio-specific implementation of the SourceMedia -> StagedMediaResult
contract this module defines.

Mirrors hypogeum.publishing.submission_ledger's write-ahead pattern
exactly (ADR-013 Decision 7 item 2): a durable `media_staging_attempts`
row is committed BEFORE any external call, so a caller retrying after a
crash/timeout never blindly re-uploads. Same state-machine philosophy:
ambiguous/reconciliation_required outcomes block blind retry;
failed_definitive and expired are the only states that permit a fresh
attempt; a confirmed, non-expired 'uploaded' result is reused rather than
re-staged.

Public API:
    compute_sha256(path)                       -> str
    SourceMedia                                  -- local-file input dataclass
    StagedMediaResult                             -- staged-output dataclass
    MediaStagingProvider (Protocol)                -- .provider_id, .stage(source, db_path=None)
    staged_result_to_media_reference(result, ...)   -> hypogeum.publishing.provider.MediaReference

    guard_and_prepare_staging_attempt(...)  -> StagingAttemptGuard
        The sole pre-external-call entry point. Same three actions as
        submission_ledger.guard_and_prepare_attempt(): 'proceed' (durably
        committed, caller must finalize), 'use_existing' (confirmed,
        non-expired prior result), 'blocked' (ambiguous prior attempt --
        no blind resubmit).

    mark_staging_upload_ready(...)     -> dict   (presigning -> upload_ready)
    mark_staging_uploading(...)        -> dict   (upload_ready -> uploading)
    record_staging_uploaded(...)       -> dict   (-> uploaded; writes public_url)
    record_staging_definitive_failure(...) -> dict
    record_staging_ambiguous(...)      -> dict

No credential material of any kind is ever accepted, stored, or
referenced by this module. The presigned upload URL itself is never
persisted -- only the resulting durable public_url, and only by a
provider-specific caller that has independently confirmed (per its own
verified contract) that the field is the intended, stable media
reference.
"""

from __future__ import annotations

import hashlib
import json as _json
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing.provider import MediaReference

_MAX_ERROR_MESSAGE_LEN = 2000

# Same defense-in-depth policy as submission_ledger.py/provider_repository.py.
_SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*:\s*).+?(?=,|;|$)"),
    re.compile(r"(?i)(bearer\s+)\S+"),
    re.compile(r"(?i)([a-z0-9_\-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret)[a-z0-9_\-]*\s*[:=]\s*)\S+"),
]


def _sanitize_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    out = str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(r"\1[REDACTED]", out)
    return out[:_MAX_ERROR_MESSAGE_LEN]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def compute_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Stream the file in chunks -- never loads a multi-hundred-MB video
    fully into memory just to hash it."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Generic contract dataclasses (Factory Core level -- no provider-specific
# field names anywhere below)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceMedia:
    """One local file to be staged to a public HTTPS URL."""
    local_path: Path
    sha256: str
    size_bytes: int
    mime_type: str
    media_type: str  # 'image' | 'video' | 'gif' | 'document' -- matches MediaReference
    filename: str
    channel_id: Optional[str] = None
    episode_id: str = ""
    deliverable_id: str = ""
    platform_scope: Optional[str] = None
    thumbnail_url: Optional[str] = None
    alt_text: Optional[str] = None


@dataclass(frozen=True)
class StagedMediaResult:
    """Generic staged-media output -- no provider-specific field names."""
    media_staging_provider_id: str
    public_url: str
    source_sha256: str
    size_bytes: int
    mime_type: str
    created_at: str
    status: str  # 'uploaded' | 'reused'
    remote_object_id: Optional[str] = None
    expires_at: Optional[str] = None
    provider_metadata: dict = field(default_factory=dict)


class MediaStagingProvider(Protocol):
    provider_id: str

    def stage(self, source: SourceMedia, *, db_path: Optional[Path] = None) -> StagedMediaResult:
        ...


def staged_result_to_media_reference(
    result: StagedMediaResult,
    *,
    media_type: str,
    thumbnail_url: Optional[str] = None,
    title: Optional[str] = None,
    alt_text: Optional[str] = None,
) -> MediaReference:
    """
    Convert a generic staged-media result into the existing generic
    hypogeum.publishing.provider.MediaReference a publishing provider
    consumes -- the two contracts are deliberately kept separate (staging
    identity/lifecycle vs. publish-time media reference) even though this
    conversion is a thin field mapping today.
    """
    return MediaReference(
        url=result.public_url, media_type=media_type,
        thumbnail_url=thumbnail_url, title=title, alt_text=alt_text,
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class MediaStagingError(RuntimeError):
    """Base class for durable-staging-boundary failures. Always fail closed."""


class InvalidStagingTransitionError(MediaStagingError):
    pass


class StagingAttemptNotFoundError(MediaStagingError):
    pass


class BlindStagingRetryRefusedError(MediaStagingError):
    """Raised when a caller tries to record an outcome for a staging
    attempt whose state does not permit it."""


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

_ALLOWED_TRANSITIONS: dict = {
    "prepared": frozenset({"presigning", "failed_definitive", "failed_ambiguous"}),
    "presigning": frozenset({"upload_ready", "failed_definitive", "failed_ambiguous"}),
    "upload_ready": frozenset({"uploading", "failed_definitive", "failed_ambiguous"}),
    "uploading": frozenset({"uploaded", "failed_definitive", "failed_ambiguous"}),
    "uploaded": frozenset({"expired"}),
    "failed_definitive": frozenset(),
    "failed_ambiguous": frozenset({"reconciliation_required"}),
    "reconciliation_required": frozenset({"uploaded", "failed_definitive", "failed_ambiguous"}),
    "expired": frozenset(),
}

_TERMINAL_SUCCESS = frozenset({"uploaded"})
# The only states from which a brand-new staging attempt may be created --
# everything else (in-flight, ambiguous, reconciliation_required, a still-
# valid uploaded result) blocks a new external call. Same mechanism as
# submission_ledger._PERMITS_NEW_ATTEMPT.
_PERMITS_NEW_ATTEMPT = frozenset({"failed_definitive", "expired"})


def _validate_transition(current: str, new: str) -> None:
    allowed = _ALLOWED_TRANSITIONS.get(current)
    if allowed is None or new not in allowed:
        raise InvalidStagingTransitionError(f"invalid staging state transition: {current!r} -> {new!r}")


def _row(conn: sqlite3.Connection, staging_attempt_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM media_staging_attempts WHERE staging_attempt_id = ?", (staging_attempt_id,),
    ).fetchone()
    return dict(row) if row else None


def _is_expired(row: dict, *, now: datetime) -> bool:
    if not row.get("expires_at"):
        return False
    try:
        expires_at = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


# ---------------------------------------------------------------------------
# Guard + prepare -- the sole pre-external-call entry point
# ---------------------------------------------------------------------------

@dataclass
class StagingAttemptGuard:
    action: str  # 'proceed' | 'use_existing' | 'blocked'
    staging_attempt_id: Optional[str] = None
    existing_result: Optional[StagedMediaResult] = None
    existing_state: Optional[str] = None
    reason: Optional[str] = None


def _to_staged_result(row: dict, *, status: str) -> StagedMediaResult:
    metadata = {}
    if row.get("metadata_json"):
        try:
            metadata = _json.loads(row["metadata_json"])
        except ValueError:
            metadata = {}
    return StagedMediaResult(
        media_staging_provider_id=row["provider_id"],
        public_url=row["public_url"],
        source_sha256=row["source_sha256"],
        size_bytes=row["source_size_bytes"],
        mime_type=row["source_mime_type"],
        created_at=row["created_at"],
        status=status,
        remote_object_id=row.get("remote_object_id"),
        expires_at=row.get("expires_at"),
        provider_metadata=metadata,
    )


def guard_and_prepare_staging_attempt(
    db_path: Optional[Path],
    *,
    provider_id: str,
    channel_id: Optional[str],
    episode_id: str,
    deliverable_id: str,
    source_sha256: str,
    source_size_bytes: int,
    source_mime_type: str,
) -> StagingAttemptGuard:
    """
    Single atomic transaction: inspect the most recent staging attempt for
    (provider_id, channel_id, episode_id, deliverable_id, source_sha256)
    and either:
      - report a confirmed, non-expired prior result (action='use_existing'
        -- no new presign/upload call, ever);
      - lazily recognize an expired 'uploaded' result and permit a fresh
        attempt (never reuses an expired URL);
      - refuse a new external call (action='blocked') because the latest
        attempt is ambiguous/reconciliation_required/already in flight;
      - or commit a new 'prepared' attempt, already advanced to
        'presigning' in the SAME transaction (action='proceed') -- durably
        visible before any external call, and closing the same
        concurrent-claim race window submission_ledger's guard closes.
    """
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            now_dt = datetime.now(timezone.utc)
            now = now_dt.isoformat()
            latest = conn.execute(
                """
                SELECT * FROM media_staging_attempts
                WHERE provider_id = ? AND channel_id IS ? AND episode_id = ?
                  AND deliverable_id = ? AND source_sha256 = ?
                ORDER BY attempt_number DESC LIMIT 1
                """,
                (provider_id, channel_id, episode_id, deliverable_id, source_sha256),
            ).fetchone()
            latest = dict(latest) if latest else None

            if latest is not None and latest["state"] in _TERMINAL_SUCCESS:
                if _is_expired(latest, now=now_dt):
                    latest = _transition(conn, latest["staging_attempt_id"], "expired", now=now)
                else:
                    conn.rollback()
                    return StagingAttemptGuard(
                        action="use_existing", staging_attempt_id=latest["staging_attempt_id"],
                        existing_result=_to_staged_result(latest, status="reused"),
                        existing_state=latest["state"], reason="confirmed non-expired staged result exists",
                    )

            if latest is not None and latest["state"] not in _PERMITS_NEW_ATTEMPT:
                conn.rollback()
                return StagingAttemptGuard(
                    action="blocked", staging_attempt_id=latest["staging_attempt_id"],
                    existing_state=latest["state"],
                    reason=f"prior staging attempt is {latest['state']} -- reconciliation required "
                           f"(or another caller is currently staging it) before any new external call",
                )

            attempt_number = (latest["attempt_number"] + 1) if latest else 1
            staging_attempt_id = _new_id("stage")
            conn.execute(
                """
                INSERT INTO media_staging_attempts
                    (staging_attempt_id, provider_id, channel_id, episode_id, deliverable_id,
                     source_sha256, source_size_bytes, source_mime_type, attempt_number, state,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                """,
                (staging_attempt_id, provider_id, channel_id, episode_id, deliverable_id,
                 source_sha256, source_size_bytes, source_mime_type, attempt_number, now, now),
            )
            _transition(conn, staging_attempt_id, "presigning", now=now)
            conn.commit()
            return StagingAttemptGuard(action="proceed", staging_attempt_id=staging_attempt_id)
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

def _transition(
    conn: sqlite3.Connection, staging_attempt_id: str, new_state: str, *,
    last_error_category: Optional[str] = None, last_error_message: Optional[str] = None, now: str,
) -> dict:
    row = _row(conn, staging_attempt_id)
    if row is None:
        raise StagingAttemptNotFoundError(f"unknown staging_attempt_id: {staging_attempt_id!r}")
    _validate_transition(row["state"], new_state)
    conn.execute(
        """
        UPDATE media_staging_attempts
        SET state = ?, last_error_category = COALESCE(?, last_error_category),
            last_error_message = COALESCE(?, last_error_message), updated_at = ?
        WHERE staging_attempt_id = ?
        """,
        (new_state, last_error_category, _sanitize_text(last_error_message), now, staging_attempt_id),
    )
    return _row(conn, staging_attempt_id)


def _run_transition(db_path, staging_attempt_id, new_state, **kwargs) -> dict:
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now_iso()
            result = _transition(conn, staging_attempt_id, new_state, now=now, **kwargs)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def mark_staging_upload_ready(db_path: Optional[Path], staging_attempt_id: str) -> dict:
    """presigning -> upload_ready. Called after a successful, validated
    presign response -- BEFORE the upload transport call."""
    return _run_transition(db_path, staging_attempt_id, "upload_ready")


def mark_staging_uploading(db_path: Optional[Path], staging_attempt_id: str) -> dict:
    """upload_ready -> uploading. Called immediately before the upload
    transport call -- the durable pre-call commit for the SECOND external
    boundary, mirroring guard_and_prepare_staging_attempt()'s commit for
    the first."""
    return _run_transition(db_path, staging_attempt_id, "uploading")


def record_staging_uploaded(
    db_path: Optional[Path], staging_attempt_id: str, *,
    public_url: str, remote_object_id: Optional[str] = None,
    expires_at: Optional[str] = None, provider_metadata: Optional[dict] = None,
) -> dict:
    """
    Record confirmed upload completion. Storing public_url here is the
    ONLY place this module ever persists a URL -- callers must only reach
    this function once their own provider-specific contract has confirmed
    public_url is the intended, stable media reference (never the
    short-lived presigned upload URL itself).
    """
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now_iso()
            attempt = _row(conn, staging_attempt_id)
            if attempt is None:
                raise StagingAttemptNotFoundError(f"unknown staging_attempt_id: {staging_attempt_id!r}")
            if attempt["state"] not in ("upload_ready", "uploading", "reconciliation_required"):
                raise BlindStagingRetryRefusedError(
                    f"cannot record uploaded for staging attempt {staging_attempt_id!r} in state "
                    f"{attempt['state']!r}"
                )
            if attempt["state"] == "upload_ready":
                attempt = _transition(conn, staging_attempt_id, "uploading", now=now)

            metadata_json = _json.dumps(_sanitize_metadata(provider_metadata)) if provider_metadata else None
            conn.execute(
                """
                UPDATE media_staging_attempts
                SET public_url = ?, remote_object_id = ?, expires_at = ?, metadata_json = ?
                WHERE staging_attempt_id = ?
                """,
                (public_url, remote_object_id, expires_at, metadata_json, staging_attempt_id),
            )
            _transition(conn, staging_attempt_id, "uploaded", now=now)
            conn.commit()
            return _row(conn, staging_attempt_id)
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def record_staging_definitive_failure(
    db_path: Optional[Path], staging_attempt_id: str, *, error_category: str, error_message: str,
) -> dict:
    return _run_transition(
        db_path, staging_attempt_id, "failed_definitive",
        last_error_category=error_category, last_error_message=error_message,
    )


def record_staging_ambiguous(
    db_path: Optional[Path], staging_attempt_id: str, *, error_category: str, error_message: str,
) -> dict:
    return _run_transition(
        db_path, staging_attempt_id, "failed_ambiguous",
        last_error_category=error_category, last_error_message=error_message,
    )


# ---------------------------------------------------------------------------
# Metadata sanitization -- same policy as provider_repository.py
# ---------------------------------------------------------------------------

_CREDENTIAL_KEY_PATTERN = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|cookie|"
    r"authorization|auth[_-]?header|upload[_-]?url|signed[_-]?url)"
)


def _sanitize_metadata(metadata: Optional[dict]) -> Optional[dict]:
    if metadata is None:
        return None

    def _walk(value):
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if _CREDENTIAL_KEY_PATTERN.search(str(k)):
                    out[k] = "[REDACTED]"
                else:
                    out[k] = _walk(v)
            return out
        if isinstance(value, list):
            return [_walk(v) for v in value]
        if isinstance(value, str):
            return _sanitize_text(value)
        return value

    return _walk(metadata)
