"""
hypogeum/publishing/submission_ledger.py — Durable write-ahead publishing
submission boundary (ADR-013 Decision 7, item 2).

Sole writer of publication_intents / publishing_attempts / publishing_receipts
(hypogeum/schema.sql). Provider-neutral: nothing here references any
specific provider by name.

Three durable identities (see schema.sql's own docstring for the full
contract):

    publication_intent  -- one deliberate publish/republish operation.
    publishing_attempt  -- one external submission attempt within an intent.
    publishing_receipt  -- the acknowledged external result for an attempt.

Public API:
    guard_and_prepare_attempt(...) -> AttemptGuard
        The sole entry point production code calls before making an
        external publish call. Inspects durable state and either commits a
        new 'prepared' attempt (action='proceed' -- durably visible before
        any external call is made) or returns 'blocked'/'use_existing'
        without ever making one.

    record_success(...)             -> None  (writes receipt, closes intent)
    record_definitive_failure(...)  -> None
    record_ambiguous(...)           -> None

    open_new_publication_intent(...) -> str
        Explicit, deliberate republish -- callers (not this module) decide
        when a new intent is warranted; this function never runs implicitly.

State machine (equivalent vocabulary to the task's spec):
    prepared -> submitting -> acknowledged -> succeeded
                            -> failed_definitive
                            -> ambiguous -> reconciliation_required -> succeeded
                                                                    -> failed_definitive
    prepared/submitting -> cancelled
Invalid transitions raise InvalidTransitionError -- fail closed, never
silently coerced.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema

_MAX_ERROR_MESSAGE_LEN = 2000

# Redact anything that looks like a bearer token, API key, or Authorization
# header value before it is ever persisted -- defense in depth on top of
# "no credential ownership inside the contract" (ADR-013 Decision 3).
_SECRET_PATTERNS = [
    # Consumes the entire header value (e.g. "Authorization: Bearer <token>"),
    # not just the first whitespace-delimited token -- a scheme + token
    # pair must be redacted together or the token survives untouched.
    re.compile(r"(?i)(authorization\s*:\s*).+?(?=,|;|$)"),
    re.compile(r"(?i)(bearer\s+)\S+"),
    re.compile(r"(?i)([a-z0-9_\-]*(?:api[_-]?key|access[_-]?token|secret)[a-z0-9_\-]*\s*[:=]\s*)\S+"),
]


def _sanitize_error_message(message: Optional[str]) -> Optional[str]:
    """Truncate and redact any credential-shaped substrings. Never raises."""
    if not message:
        return message
    text = str(message)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text[:_MAX_ERROR_MESSAGE_LEN]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SubmissionLedgerError(RuntimeError):
    """Base class for durable-boundary failures. Always fail closed."""


class InvalidTransitionError(SubmissionLedgerError):
    """Raised when an attempt state transition is not in the validated set."""


class AttemptNotFoundError(SubmissionLedgerError):
    pass


class BlindRetryRefusedError(SubmissionLedgerError):
    """
    Raised (never silently swallowed) when a caller tries to record an
    outcome for an attempt whose state does not permit it -- e.g. finalizing
    an attempt twice, or resubmitting one already ambiguous.
    """


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

_ALLOWED_TRANSITIONS: dict = {
    "prepared": frozenset({"submitting", "cancelled", "failed_definitive", "ambiguous"}),
    "submitting": frozenset({"acknowledged", "failed_definitive", "ambiguous", "cancelled"}),
    "acknowledged": frozenset({"succeeded", "failed_definitive", "ambiguous"}),
    "ambiguous": frozenset({"reconciliation_required", "succeeded", "failed_definitive"}),
    "reconciliation_required": frozenset({"succeeded", "failed_definitive", "ambiguous"}),
    "succeeded": frozenset(),
    "failed_definitive": frozenset(),
    "cancelled": frozenset(),
}

# States from which a NEW external call is never permitted -- a retry that
# finds an attempt in one of these must fail closed rather than resubmit.
_TERMINAL_SUCCESS = frozenset({"succeeded"})
_ACKNOWLEDGED_NOT_YET_SUCCEEDED = frozenset({"acknowledged"})
# The ONLY states from which a brand-new attempt may be created. Everything
# else -- prepared/submitting (something is currently in flight, or crashed
# without reaching a definitive outcome), acknowledged, ambiguous,
# reconciliation_required, succeeded -- blocks a new external call. This is
# the sole mechanism preventing two concurrent callers from both crossing
# the external-call boundary for the same intent.
_PERMITS_NEW_ATTEMPT = frozenset({"failed_definitive", "cancelled"})


def _validate_transition(current: str, new: str) -> None:
    allowed = _ALLOWED_TRANSITIONS.get(current)
    if allowed is None or new not in allowed:
        raise InvalidTransitionError(f"invalid attempt state transition: {current!r} -> {new!r}")


# ---------------------------------------------------------------------------
# Row -> dict helpers
# ---------------------------------------------------------------------------

def _row(conn: sqlite3.Connection, table: str, id_col: str, id_val: str) -> Optional[dict]:
    row = conn.execute(f"SELECT * FROM {table} WHERE {id_col} = ?", (id_val,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Publication intent
# ---------------------------------------------------------------------------

def open_new_publication_intent(
    db_path: Optional[Path],
    *,
    channel_id: Optional[str],
    episode_id: str,
    deliverable_id: str,
    platform: str,
) -> str:
    """
    Explicitly open a NEW publication intent for (episode_id, platform,
    deliverable_id), closing any existing open intent for that same triple
    as 'cancelled' first (there is at most one open intent per triple --
    enforced by a DB-level partial unique index, not just here).

    This is the deliberate-republish entry point (e.g. re-publishing
    ep_f3619022 after its prior external publication was deleted). It is
    NEVER called automatically by guard_and_prepare_attempt() or by any
    retry path -- only an explicit caller decision mints a new intent.
    """
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now_iso()
            conn.execute(
                """
                UPDATE publication_intents
                SET status = 'cancelled', updated_at = ?
                WHERE episode_id = ? AND platform = ? AND deliverable_id = ? AND status = 'open'
                """,
                (now, episode_id, platform, deliverable_id),
            )
            intent_id = _new_id("intent")
            conn.execute(
                """
                INSERT INTO publication_intents
                    (publication_intent_id, channel_id, episode_id, deliverable_id, platform,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (intent_id, channel_id, episode_id, deliverable_id, platform, now, now),
            )
            conn.commit()
            return intent_id
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def _get_or_create_open_intent(conn: sqlite3.Connection, *, channel_id, episode_id, deliverable_id, platform, now) -> dict:
    """
    Must run inside an already-open transaction on `conn`.

    Returns the MOST RECENT intent for this (episode_id, platform,
    deliverable_id) triple regardless of its status -- not just an 'open'
    one. A prior 'succeeded' intent must be returned as-is (never masked by
    silently minting a new one), so a plain retry after success is
    recognized by guard_and_prepare_attempt() as "already succeeded"
    rather than accidentally opening a fresh intent. Only when NO intent
    has ever existed for this triple is a new 'open' one created here.
    """
    existing = conn.execute(
        """
        SELECT * FROM publication_intents
        WHERE episode_id = ? AND platform = ? AND deliverable_id = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (episode_id, platform, deliverable_id),
    ).fetchone()
    if existing is not None:
        return dict(existing)

    intent_id = _new_id("intent")
    try:
        conn.execute(
            """
            INSERT INTO publication_intents
                (publication_intent_id, channel_id, episode_id, deliverable_id, platform,
                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (intent_id, channel_id, episode_id, deliverable_id, platform, now, now),
        )
    except sqlite3.IntegrityError:
        # Lost a race under BEGIN IMMEDIATE serialization (should not
        # normally happen -- the transaction holds the write lock -- but
        # fail safe by re-reading rather than assuming).
        existing = conn.execute(
            """
            SELECT * FROM publication_intents
            WHERE episode_id = ? AND platform = ? AND deliverable_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (episode_id, platform, deliverable_id),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        raise
    return _row(conn, "publication_intents", "publication_intent_id", intent_id)


# ---------------------------------------------------------------------------
# Guard + prepare — the sole pre-external-call entry point
# ---------------------------------------------------------------------------

@dataclass
class AttemptGuard:
    action: str  # 'proceed' | 'use_existing' | 'blocked'
    attempt_id: Optional[str] = None
    publication_intent_id: Optional[str] = None
    existing_receipt: Optional[dict] = None
    existing_attempt_state: Optional[str] = None
    reason: Optional[str] = None  # populated for 'blocked' and 'use_existing'


def guard_and_prepare_attempt(
    db_path: Optional[Path],
    *,
    channel_id: Optional[str],
    episode_id: str,
    deliverable_id: str,
    platform: str,
    provider_id: str,
) -> AttemptGuard:
    """
    Single atomic transaction: resolve/open the current publication intent,
    inspect its most recent attempt, and either:
      - commit a new 'prepared' attempt (action='proceed') -- durably
        visible before the caller makes any external call; or
      - report an existing terminal-success result (action='use_existing');
      - or refuse to permit a new external call (action='blocked') because
        the latest attempt is ambiguous/reconciliation_required.

    No attempt is ever left committed without either being returned as
    'proceed' (caller is responsible for finalizing it) or already
    representing a genuinely prior outcome.
    """
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now_iso()
            intent = _get_or_create_open_intent(
                conn, channel_id=channel_id, episode_id=episode_id,
                deliverable_id=deliverable_id, platform=platform, now=now,
            )
            intent_id = intent["publication_intent_id"]

            latest = conn.execute(
                """
                SELECT * FROM publishing_attempts
                WHERE publication_intent_id = ?
                ORDER BY attempt_number DESC LIMIT 1
                """,
                (intent_id,),
            ).fetchone()
            latest = dict(latest) if latest else None

            if latest is not None and latest["state"] in _TERMINAL_SUCCESS:
                receipt = _row(conn, "publishing_receipts", "attempt_id", latest["attempt_id"])
                conn.rollback()
                return AttemptGuard(
                    action="use_existing", attempt_id=latest["attempt_id"],
                    publication_intent_id=intent_id, existing_receipt=receipt,
                    existing_attempt_state=latest["state"],
                    reason="publication intent already succeeded",
                )

            if latest is not None and latest["state"] in _ACKNOWLEDGED_NOT_YET_SUCCEEDED:
                # We know the provider acknowledged this -- never blindly
                # resubmit; the caller must reconcile (finalize the receipt
                # write) rather than re-call the provider.
                conn.rollback()
                return AttemptGuard(
                    action="blocked", attempt_id=latest["attempt_id"],
                    publication_intent_id=intent_id,
                    existing_attempt_state=latest["state"],
                    reason="prior attempt was acknowledged by the provider but not yet "
                           "confirmed succeeded locally -- reconcile before retrying",
                )

            if latest is not None and latest["state"] not in _PERMITS_NEW_ATTEMPT:
                # Covers ambiguous/reconciliation_required (needs explicit
                # reconciliation) AND prepared/submitting (another caller is
                # already inside this exact external-call boundary right
                # now, or crashed mid-flight without reaching a definitive
                # outcome -- in both cases the safe, fail-closed answer is
                # the same: never let a second caller cross the boundary
                # concurrently, and never blindly resubmit after an
                # unexplained gap). This is the sole mechanism that keeps
                # two concurrent claims for the same intent from both
                # reaching 'proceed'.
                conn.rollback()
                return AttemptGuard(
                    action="blocked", attempt_id=latest["attempt_id"],
                    publication_intent_id=intent_id,
                    existing_attempt_state=latest["state"],
                    reason=f"prior attempt is {latest['state']} -- reconciliation required "
                           f"(or another caller is currently attempting it) before any new "
                           f"external call",
                )

            attempt_number = (latest["attempt_number"] + 1) if latest else 1
            attempt_id = _new_id("attempt")
            conn.execute(
                """
                INSERT INTO publishing_attempts
                    (attempt_id, publication_intent_id, channel_id, episode_id, deliverable_id,
                     platform, provider_id, provider_request_key, attempt_number, state,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                """,
                (attempt_id, intent_id, channel_id, episode_id, deliverable_id, platform,
                 provider_id, _new_id("req"), attempt_number, now, now),
            )
            # Advance straight to 'submitting', in the SAME transaction, so
            # the durable state any concurrent/subsequent guard call sees is
            # never a bare 'prepared' row that a second caller could also
            # treat as "nothing in flight yet" -- 'prepared' -> 'submitting'
            # is itself a validated transition (_ALLOWED_TRANSITIONS), just
            # applied before this function ever returns.
            _transition_attempt(conn, attempt_id, "submitting", now=now)
            conn.commit()
            return AttemptGuard(action="proceed", attempt_id=attempt_id, publication_intent_id=intent_id)
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Finalization
# ---------------------------------------------------------------------------

def _transition_attempt(
    conn: sqlite3.Connection, attempt_id: str, new_state: str, *,
    last_error_category: Optional[str] = None, last_error_message: Optional[str] = None, now: str,
) -> dict:
    row = _row(conn, "publishing_attempts", "attempt_id", attempt_id)
    if row is None:
        raise AttemptNotFoundError(f"unknown attempt_id: {attempt_id!r}")
    _validate_transition(row["state"], new_state)
    conn.execute(
        """
        UPDATE publishing_attempts
        SET state = ?, last_error_category = COALESCE(?, last_error_category),
            last_error_message = COALESCE(?, last_error_message), updated_at = ?
        WHERE attempt_id = ?
        """,
        (new_state, last_error_category, _sanitize_error_message(last_error_message), now, attempt_id),
    )
    return _row(conn, "publishing_attempts", "attempt_id", attempt_id)


def record_success(
    db_path: Optional[Path],
    attempt_id: str,
    *,
    provider_publication_id: Optional[str] = None,
    native_platform_content_id: Optional[str] = None,
    provider_status: Optional[str] = None,
    raw_receipt_metadata: Optional[dict] = None,
) -> dict:
    """
    Record the acknowledged external result and close the attempt+intent as
    succeeded, in one transaction. If this raises (e.g. a DB error) after
    the external call already succeeded, the attempt remains at whatever
    state it was transitioned to just before this call (acknowledged) --
    durably recorded, not silently lost -- so a subsequent guard call
    reports 'blocked' (reconcile) rather than permitting a blind resubmit.
    """
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now_iso()
            attempt = _row(conn, "publishing_attempts", "attempt_id", attempt_id)
            if attempt is None:
                raise AttemptNotFoundError(f"unknown attempt_id: {attempt_id!r}")

            if attempt["state"] not in ("prepared", "submitting", "acknowledged"):
                raise BlindRetryRefusedError(
                    f"cannot record success for attempt {attempt_id!r} in state "
                    f"{attempt['state']!r}"
                )

            if attempt["state"] == "prepared":
                attempt = _transition_attempt(conn, attempt_id, "submitting", now=now)
            if attempt["state"] == "submitting":
                attempt = _transition_attempt(conn, attempt_id, "acknowledged", now=now)

            import json as _json
            conn.execute(
                """
                INSERT INTO publishing_receipts
                    (receipt_id, attempt_id, provider_id, platform, provider_publication_id,
                     native_platform_content_id, provider_status, acknowledged_at,
                     raw_receipt_metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (_new_id("receipt"), attempt_id, attempt["provider_id"], attempt["platform"],
                 provider_publication_id, native_platform_content_id, provider_status, now,
                 _json.dumps(raw_receipt_metadata) if raw_receipt_metadata else None),
            )
            _transition_attempt(conn, attempt_id, "succeeded", now=now)
            conn.execute(
                "UPDATE publication_intents SET status = 'succeeded', updated_at = ? "
                "WHERE publication_intent_id = ?",
                (now, attempt["publication_intent_id"]),
            )
            conn.commit()
            return _row(conn, "publishing_attempts", "attempt_id", attempt_id)
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def record_definitive_failure(
    db_path: Optional[Path], attempt_id: str, *, error_category: str, error_message: str,
) -> dict:
    """Confirmed provider rejection. A later attempt under the same intent
    is left possible (per Decision 7 item 2's state machine) -- policy for
    whether/when to retry lives with the caller, not this module."""
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now_iso()
            attempt = _transition_attempt(
                conn, attempt_id, "failed_definitive",
                last_error_category=error_category, last_error_message=error_message, now=now,
            )
            conn.commit()
            return attempt
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def record_ambiguous(
    db_path: Optional[Path], attempt_id: str, *, error_category: str, error_message: str,
) -> dict:
    """Timeout / connection loss / interrupted response / unknown outcome.
    Leaves the attempt (and its intent) in a state that blocks blind
    retry -- guard_and_prepare_attempt() will report 'blocked' until this
    is explicitly reconciled."""
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _now_iso()
            attempt = _transition_attempt(
                conn, attempt_id, "ambiguous",
                last_error_category=error_category, last_error_message=error_message, now=now,
            )
            conn.commit()
            return attempt
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
