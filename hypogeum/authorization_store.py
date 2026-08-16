"""
hypogeum/authorization_store.py — Durable persistence for the
Authorization Authority (Factory Core).

Pure persistence + a small set of narrow, read-and-settle-only
functions -- no issuance policy lives here (that is
hypogeum.authorization_authority's job, mirroring the
hypogeum.workflow_registry / hypogeum.workflow_executor storage/policy
split). Four tables:

  authorization_records            -- durable AuthorizationRecord + history
  run_authorization_grants         -- durable RunAuthorizationGrant
  run_authorization_grant_consumption -- the reservation ledger (state lifecycle)
  approval_artifacts                -- durable, replay-defended ApprovalArtifact

hypogeum/submission_gate.py imports ONLY revalidate_grant_reservation()
from this module -- a read-and-settle-only function that can transition
an *already-issued* reservation from 'reserved' to 'boundary_entered'
(or deny), but can never create a grant, a reservation, or an
AUTHORIZED AuthorizationRecord. It never imports
hypogeum.authorization_authority.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import fields
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from hypogeum.approval_artifact import ApprovalArtifact, GrantBindingApproval
from hypogeum.authorization_lifecycle import AuthorizationRecord, AuthorizationState, TransitionEvent
from hypogeum.db import get_connection

__all__ = [
    "RESERVATION_STATES",
    "NON_CHARGE_STATES",
    "GrantBinding",
    "RunAuthorizationGrant",
    "LedgerReservation",
    "AuthorizationStoreError",
    "ApprovalReplayError",
    "GrantAlreadyExistsWithDifferentContentError",
    "LedgerReservationConflictError",
    "GrantRevalidationError",
    "GrantNotFoundError",
    "GrantRevokedError",
    "GrantExpiredError",
    "GrantChecksumMismatchError",
    "ReservationNotFoundError",
    "ReservationStateMismatchError",
    "ensure_tables",
    "artifact_to_json",
    "artifact_from_json",
    "persist_approval_artifact",
    "get_approval_artifact",
    "get_approval_artifact_for_grant",
    "insert_grant",
    "get_grant",
    "find_active_grant_for_scope",
    "revoke_grant_row",
    "persist_authorization_record",
    "get_authorization_record",
    "insert_ledger_reservation",
    "get_ledger_reservation",
    "sum_active_ledger_consumption",
    "transition_ledger_state",
    "revalidate_grant_reservation",
]

# ---------------------------------------------------------------------------
# Reservation lifecycle (Correction 4)
# ---------------------------------------------------------------------------

RESERVATION_STATES = frozenset({
    "reserved", "boundary_entered", "committed", "released",
    "possible_unknown_acceptance", "expired_abandoned", "reconciled",
})

# States (and the one reconciled-resolution) that give capacity back to
# the grant -- everything else still counts against the ceiling, per
# submission_gate.py's own existing rule for unknown-acceptance budget
# reservations: "must count as a possible call and possible spend at
# the authorized ceiling until reconciled."
NON_CHARGE_STATES = frozenset({"released"})
_NON_CHARGE_RECONCILIATION_RESOLUTION = "confirmed_not_submitted"


class AuthorizationStoreError(Exception):
    """Base class for every failure in this module."""


class ApprovalReplayError(AuthorizationStoreError):
    """An ApprovalArtifact's nonce has already been consumed."""


class GrantAlreadyExistsWithDifferentContentError(AuthorizationStoreError):
    """(grant_id, grant_version) is already registered with a different
    grant_checksum -- a material change requires a new grant_version,
    never a silent overwrite."""


class LedgerReservationConflictError(AuthorizationStoreError):
    """The same (grant_id, binding, idempotency_key) was already used
    for a *different* envelope_id -- the ambiguous case from
    requirement 7."""


class GrantRevalidationError(AuthorizationStoreError):
    """Base class for revalidate_grant_reservation() failures -- every
    subclass means submission_gate.submit() must deny with zero
    provider calls."""


class GrantNotFoundError(GrantRevalidationError):
    pass


class GrantRevokedError(GrantRevalidationError):
    pass


class GrantExpiredError(GrantRevalidationError):
    pass


class GrantChecksumMismatchError(GrantRevalidationError):
    """The grant's live checksum no longer matches parent_grant_checksum
    -- superseded or altered since the child authorization was issued."""


class ReservationNotFoundError(GrantRevalidationError):
    pass


class ReservationStateMismatchError(GrantRevalidationError):
    """The reservation is not in 'reserved' state -- already
    boundary-entered by a concurrent path, already released, or
    otherwise inconsistent."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decimal_to_str(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)


def _str_to_decimal(value: Optional[str]) -> Optional[Decimal]:
    return None if value is None else Decimal(value)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS approval_artifacts (
            approval_id                TEXT NOT NULL,
            approval_version           INTEGER NOT NULL,
            approval_type              TEXT NOT NULL,
            subject_checksum           TEXT NOT NULL,
            nonce                      TEXT NOT NULL,
            artifact_json              TEXT NOT NULL,
            consumed_by_grant_id       TEXT,
            consumed_by_grant_version  INTEGER,
            consumed_by_authorization_id TEXT,
            recorded_at                TEXT NOT NULL,
            PRIMARY KEY (approval_id, approval_version),
            UNIQUE (nonce)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_authorization_grants (
            grant_id                     TEXT NOT NULL,
            grant_version                INTEGER NOT NULL,
            grant_checksum                TEXT NOT NULL,
            channel_id                     TEXT NOT NULL,
            episode_id                      TEXT NOT NULL,
            workflow_id                      TEXT NOT NULL,
            workflow_version                  TEXT NOT NULL,
            workflow_checksum                  TEXT NOT NULL,
            bindings_json                        TEXT NOT NULL,
            max_total_spend_all_bindings           TEXT,
            currency                                 TEXT NOT NULL,
            expires_at                                TEXT NOT NULL,
            approval_id                                 TEXT NOT NULL,
            approval_version                              INTEGER NOT NULL,
            status                                          TEXT NOT NULL DEFAULT 'active'
                                                                 CHECK(status IN ('active','revoked')),
            registered_at                                     TEXT NOT NULL,
            revoked_at                                          TEXT,
            revoked_by                                            TEXT,
            revoked_reason                                          TEXT,
            PRIMARY KEY (grant_id, grant_version)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_authorization_grant_consumption (
            reservation_id       TEXT NOT NULL PRIMARY KEY,
            grant_id               TEXT NOT NULL,
            grant_version            INTEGER NOT NULL,
            provider                  TEXT NOT NULL,
            model                       TEXT NOT NULL,
            provider_operation           TEXT NOT NULL,
            idempotency_key                TEXT NOT NULL,
            envelope_id                      TEXT NOT NULL,
            authorization_id                   TEXT NOT NULL,
            reserved_amount                      TEXT NOT NULL,
            charged_amount                         TEXT,
            state                                    TEXT NOT NULL,
            reconciliation_resolution                  TEXT,
            created_at                                   TEXT NOT NULL,
            updated_at                                     TEXT NOT NULL,
            UNIQUE(grant_id, provider, model, provider_operation, idempotency_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS authorization_records (
            authorization_id              TEXT NOT NULL PRIMARY KEY,
            authorization_version         INTEGER NOT NULL,
            envelope_id                     TEXT NOT NULL,
            envelope_version                 INTEGER NOT NULL,
            current_state                      TEXT NOT NULL,
            history_json                         TEXT NOT NULL,
            superseded_authorization_id            TEXT,
            updated_at                                TEXT NOT NULL
        )
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# ApprovalArtifact persistence
# ---------------------------------------------------------------------------

def artifact_to_json(artifact: ApprovalArtifact) -> str:
    payload = {f.name: getattr(artifact, f.name) for f in fields(artifact)}
    payload["allowed_bindings"] = [
        {"provider": b.provider, "model": b.model, "provider_operation": b.provider_operation}
        for b in artifact.allowed_bindings
    ]
    payload["max_cost_per_call"] = str(artifact.max_cost_per_call)
    payload["max_total_spend"] = str(artifact.max_total_spend)
    payload["signature"] = base64.b64encode(artifact.signature).decode("ascii")
    return json.dumps(payload, sort_keys=True)


def artifact_from_json(raw: str) -> ApprovalArtifact:
    payload = json.loads(raw)
    payload["allowed_bindings"] = tuple(
        GrantBindingApproval(**b) for b in payload["allowed_bindings"]
    )
    payload["max_cost_per_call"] = Decimal(payload["max_cost_per_call"])
    payload["max_total_spend"] = Decimal(payload["max_total_spend"])
    payload["signature"] = base64.b64decode(payload["signature"])
    return ApprovalArtifact(**payload)


def persist_approval_artifact(
    conn: sqlite3.Connection, artifact: ApprovalArtifact, *,
    consumed_by_grant_id: Optional[str] = None,
    consumed_by_grant_version: Optional[int] = None,
    consumed_by_authorization_id: Optional[str] = None,
) -> None:
    """Atomically records a verified artifact. UNIQUE(nonce) is the
    replay defense -- a second attempt to persist an artifact whose
    nonce has already been consumed raises ApprovalReplayError even if
    the signature is (still) valid."""
    ensure_tables(conn)
    try:
        conn.execute(
            """
            INSERT INTO approval_artifacts
                (approval_id, approval_version, approval_type, subject_checksum, nonce,
                 artifact_json, consumed_by_grant_id, consumed_by_grant_version,
                 consumed_by_authorization_id, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact.approval_id, artifact.approval_version, artifact.approval_type,
                artifact.subject_checksum, artifact.nonce, artifact_to_json(artifact),
                consumed_by_grant_id, consumed_by_grant_version, consumed_by_authorization_id,
                _now_iso(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ApprovalReplayError(
            f"approval artifact nonce {artifact.nonce!r} (or approval_id/version) already consumed"
        ) from exc


def get_approval_artifact(
    conn: sqlite3.Connection, approval_id: str, approval_version: int,
) -> Optional[ApprovalArtifact]:
    ensure_tables(conn)
    row = conn.execute(
        "SELECT artifact_json FROM approval_artifacts WHERE approval_id = ? AND approval_version = ?",
        (approval_id, approval_version),
    ).fetchone()
    return artifact_from_json(row["artifact_json"]) if row else None


def get_approval_artifact_for_grant(
    conn: sqlite3.Connection, grant_id: str, grant_version: int,
) -> Optional[ApprovalArtifact]:
    ensure_tables(conn)
    row = conn.execute(
        "SELECT artifact_json FROM approval_artifacts WHERE consumed_by_grant_id = ? AND consumed_by_grant_version = ?",
        (grant_id, grant_version),
    ).fetchone()
    return artifact_from_json(row["artifact_json"]) if row else None


# ---------------------------------------------------------------------------
# RunAuthorizationGrant persistence
# ---------------------------------------------------------------------------

class GrantBinding:
    """One (provider, model, provider_operation) limit set within a
    grant. Plain attribute-holder (not a dataclass with validation) --
    hypogeum.authorization_authority owns validation."""

    __slots__ = (
        "provider", "model", "provider_operation", "currency", "max_cost_per_call",
        "max_calls", "max_total_spend", "retry_authorized", "retry_max_attempts",
        "fallback_authorized", "fallback_provider", "fallback_model",
    )

    def __init__(
        self, *, provider, model, provider_operation, currency, max_cost_per_call,
        max_calls, max_total_spend, retry_authorized=False, retry_max_attempts=None,
        fallback_authorized=False, fallback_provider=None, fallback_model=None,
    ):
        self.provider = provider
        self.model = model
        self.provider_operation = provider_operation
        self.currency = currency
        self.max_cost_per_call = max_cost_per_call
        self.max_calls = max_calls
        self.max_total_spend = max_total_spend
        self.retry_authorized = retry_authorized
        self.retry_max_attempts = retry_max_attempts
        self.fallback_authorized = fallback_authorized
        self.fallback_provider = fallback_provider
        self.fallback_model = fallback_model

    def binding_key(self) -> str:
        return f"{self.provider}:{self.model}:{self.provider_operation}"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider, "model": self.model, "provider_operation": self.provider_operation,
            "currency": self.currency, "max_cost_per_call": str(self.max_cost_per_call),
            "max_calls": self.max_calls, "max_total_spend": str(self.max_total_spend),
            "retry_authorized": self.retry_authorized, "retry_max_attempts": self.retry_max_attempts,
            "fallback_authorized": self.fallback_authorized, "fallback_provider": self.fallback_provider,
            "fallback_model": self.fallback_model,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GrantBinding":
        return cls(
            provider=d["provider"], model=d["model"], provider_operation=d["provider_operation"],
            currency=d["currency"], max_cost_per_call=Decimal(d["max_cost_per_call"]),
            max_calls=d["max_calls"], max_total_spend=Decimal(d["max_total_spend"]),
            retry_authorized=d.get("retry_authorized", False), retry_max_attempts=d.get("retry_max_attempts"),
            fallback_authorized=d.get("fallback_authorized", False),
            fallback_provider=d.get("fallback_provider"), fallback_model=d.get("fallback_model"),
        )


class RunAuthorizationGrant:
    """Durable record of one registered, verified grant. Constructed
    only by insert_grant()/get_grant() -- never hand-built by a
    caller wanting to bypass verification."""

    __slots__ = (
        "grant_id", "grant_version", "grant_checksum", "channel_id", "episode_id",
        "workflow_id", "workflow_version", "workflow_checksum", "bindings",
        "max_total_spend_all_bindings", "currency", "expires_at", "approval_id",
        "approval_version", "status", "registered_at", "revoked_at", "revoked_by", "revoked_reason",
    )

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    def find_binding(self, provider: str, model: str, provider_operation: str) -> Optional[GrantBinding]:
        for b in self.bindings:
            if b.provider == provider and b.model == model and b.provider_operation == provider_operation:
                return b
        return None


def insert_grant(
    conn: sqlite3.Connection, *, grant_id: str, grant_version: int, grant_checksum: str,
    channel_id: str, episode_id: str, workflow_id: str, workflow_version: str, workflow_checksum: str,
    bindings: Sequence[GrantBinding], max_total_spend_all_bindings: Optional[Decimal], currency: str,
    expires_at: str, approval_id: str, approval_version: int,
) -> RunAuthorizationGrant:
    """Atomic content-addressed registration -- idempotent replay on
    identical (grant_id, grant_version, grant_checksum); raises
    GrantAlreadyExistsWithDifferentContentError on a checksum mismatch
    for the same key, mirroring workflow_registry.register_workflow()'s
    exact discipline."""
    ensure_tables(conn)
    registered_at = _now_iso()
    bindings_json = json.dumps([b.to_dict() for b in bindings], sort_keys=True)
    try:
        conn.execute(
            """
            INSERT INTO run_authorization_grants
                (grant_id, grant_version, grant_checksum, channel_id, episode_id, workflow_id,
                 workflow_version, workflow_checksum, bindings_json, max_total_spend_all_bindings,
                 currency, expires_at, approval_id, approval_version, status, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                grant_id, grant_version, grant_checksum, channel_id, episode_id, workflow_id,
                workflow_version, workflow_checksum, bindings_json,
                _decimal_to_str(max_total_spend_all_bindings), currency, expires_at,
                approval_id, approval_version, registered_at,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        existing = conn.execute(
            "SELECT * FROM run_authorization_grants WHERE grant_id = ? AND grant_version = ?",
            (grant_id, grant_version),
        ).fetchone()
        if existing is None:
            raise
        if existing["grant_checksum"] != grant_checksum:
            raise GrantAlreadyExistsWithDifferentContentError(
                f"grant {grant_id!r}@{grant_version} already registered with checksum "
                f"{existing['grant_checksum']!r}, attempted {grant_checksum!r}"
            )
        return _row_to_grant(existing)

    row = conn.execute(
        "SELECT * FROM run_authorization_grants WHERE grant_id = ? AND grant_version = ?",
        (grant_id, grant_version),
    ).fetchone()
    return _row_to_grant(row)


def _row_to_grant(row: sqlite3.Row) -> RunAuthorizationGrant:
    bindings = [GrantBinding.from_dict(b) for b in json.loads(row["bindings_json"])]
    return RunAuthorizationGrant(
        grant_id=row["grant_id"], grant_version=row["grant_version"], grant_checksum=row["grant_checksum"],
        channel_id=row["channel_id"], episode_id=row["episode_id"], workflow_id=row["workflow_id"],
        workflow_version=row["workflow_version"], workflow_checksum=row["workflow_checksum"],
        bindings=bindings, max_total_spend_all_bindings=_str_to_decimal(row["max_total_spend_all_bindings"]),
        currency=row["currency"], expires_at=row["expires_at"], approval_id=row["approval_id"],
        approval_version=row["approval_version"], status=row["status"], registered_at=row["registered_at"],
        revoked_at=row["revoked_at"], revoked_by=row["revoked_by"], revoked_reason=row["revoked_reason"],
    )


def find_active_grant_for_scope(
    conn: sqlite3.Connection, channel_id: str, episode_id: str,
) -> Optional[RunAuthorizationGrant]:
    """Most recently registered 'active'-status grant for (channel_id,
    episode_id). Does NOT itself check expiry or re-verify the stored
    approval artifact -- callers (hypogeum.authorization_authority.
    resolve_active_grant()) must do both before treating the result as
    usable."""
    ensure_tables(conn)
    row = conn.execute(
        """
        SELECT * FROM run_authorization_grants
        WHERE channel_id = ? AND episode_id = ? AND status = 'active'
        ORDER BY registered_at DESC LIMIT 1
        """,
        (channel_id, episode_id),
    ).fetchone()
    return _row_to_grant(row) if row else None


def get_grant(
    conn: sqlite3.Connection, grant_id: str, grant_version: Optional[int] = None,
) -> Optional[RunAuthorizationGrant]:
    ensure_tables(conn)
    if grant_version is not None:
        row = conn.execute(
            "SELECT * FROM run_authorization_grants WHERE grant_id = ? AND grant_version = ?",
            (grant_id, grant_version),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM run_authorization_grants WHERE grant_id = ? ORDER BY grant_version DESC LIMIT 1",
            (grant_id,),
        ).fetchone()
    return _row_to_grant(row) if row else None


def revoke_grant_row(
    conn: sqlite3.Connection, grant_id: str, grant_version: int, *, revoked_by: str, reason: str, at: str,
) -> bool:
    """Atomic conditional UPDATE + rowcount check -- mirrors
    hypogeum.db.release_lease(). Returns False (never raises) if the
    grant was not active (already revoked, or never existed)."""
    ensure_tables(conn)
    cursor = conn.execute(
        """
        UPDATE run_authorization_grants
        SET status = 'revoked', revoked_at = ?, revoked_by = ?, revoked_reason = ?
        WHERE grant_id = ? AND grant_version = ? AND status = 'active'
        """,
        (at, revoked_by, reason, grant_id, grant_version),
    )
    conn.commit()
    return cursor.rowcount == 1


# ---------------------------------------------------------------------------
# AuthorizationRecord persistence
# ---------------------------------------------------------------------------

def _record_to_json(record: AuthorizationRecord) -> tuple:
    history_json = json.dumps(
        [
            {"from_state": e.from_state.value, "to_state": e.to_state.value, "at": e.at, "evidence": dict(e.evidence)}
            for e in record.history
        ],
        sort_keys=True,
    )
    return record.current_state.value, history_json


def persist_authorization_record(conn: sqlite3.Connection, record: AuthorizationRecord) -> None:
    """Upsert keyed by authorization_id alone -- AuthorizationRecord's
    own transitions mutate current_state/history functionally but keep
    the same authorization_id/authorization_version (a NEW
    authorization_version/authorization_id only appears via
    create_superseding_authorization(), which is persisted as a
    second, separate row)."""
    ensure_tables(conn)
    current_state, history_json = _record_to_json(record)
    conn.execute(
        """
        INSERT INTO authorization_records
            (authorization_id, authorization_version, envelope_id, envelope_version,
             current_state, history_json, superseded_authorization_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(authorization_id) DO UPDATE SET
            current_state = excluded.current_state,
            history_json = excluded.history_json,
            updated_at = excluded.updated_at
        """,
        (
            record.authorization_id, record.authorization_version, record.envelope_id,
            record.envelope_version, current_state, history_json,
            record.superseded_authorization_id, _now_iso(),
        ),
    )


def get_authorization_record(conn: sqlite3.Connection, authorization_id: str) -> Optional[AuthorizationRecord]:
    ensure_tables(conn)
    row = conn.execute(
        "SELECT * FROM authorization_records WHERE authorization_id = ?", (authorization_id,),
    ).fetchone()
    if row is None:
        return None
    history = tuple(
        TransitionEvent(
            from_state=AuthorizationState(e["from_state"]), to_state=AuthorizationState(e["to_state"]),
            at=e["at"], evidence=e["evidence"],
        )
        for e in json.loads(row["history_json"])
    )
    return AuthorizationRecord(
        authorization_id=row["authorization_id"], authorization_version=row["authorization_version"],
        envelope_id=row["envelope_id"], envelope_version=row["envelope_version"],
        current_state=AuthorizationState(row["current_state"]), history=history,
        superseded_authorization_id=row["superseded_authorization_id"],
    )


# ---------------------------------------------------------------------------
# Grant-consumption ledger (reservation lifecycle -- Correction 4)
# ---------------------------------------------------------------------------

class LedgerReservation:
    __slots__ = (
        "reservation_id", "grant_id", "grant_version", "provider", "model", "provider_operation",
        "idempotency_key", "envelope_id", "authorization_id", "reserved_amount", "charged_amount",
        "state", "reconciliation_resolution", "created_at", "updated_at",
    )

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))


def _row_to_reservation(row: sqlite3.Row) -> LedgerReservation:
    return LedgerReservation(
        reservation_id=row["reservation_id"], grant_id=row["grant_id"], grant_version=row["grant_version"],
        provider=row["provider"], model=row["model"], provider_operation=row["provider_operation"],
        idempotency_key=row["idempotency_key"], envelope_id=row["envelope_id"],
        authorization_id=row["authorization_id"], reserved_amount=_str_to_decimal(row["reserved_amount"]),
        charged_amount=_str_to_decimal(row["charged_amount"]), state=row["state"],
        reconciliation_resolution=row["reconciliation_resolution"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def sum_active_ledger_consumption(
    conn: sqlite3.Connection, grant_id: str, provider: str, model: str, provider_operation: str,
) -> "tuple[int, Decimal]":
    """Returns (call_count, spend_total) summed over every ledger row
    for this (grant_id, binding) NOT in a confirmed-non-charge state --
    i.e. every row except 'released', and 'reconciled' rows whose
    resolution is 'confirmed_not_submitted'. Never trusts SQLite SUM()
    on floats -- sums Decimal in Python, mirroring
    hypogeum.db.reserve_budget()."""
    ensure_tables(conn)
    rows = conn.execute(
        """
        SELECT state, reconciliation_resolution, reserved_amount, charged_amount
        FROM run_authorization_grant_consumption
        WHERE grant_id = ? AND provider = ? AND model = ? AND provider_operation = ?
        """,
        (grant_id, provider, model, provider_operation),
    ).fetchall()
    count = 0
    total = Decimal("0")
    for r in rows:
        if r["state"] in NON_CHARGE_STATES:
            continue
        if r["state"] == "reconciled" and r["reconciliation_resolution"] == _NON_CHARGE_RECONCILIATION_RESOLUTION:
            continue
        count += 1
        amount = r["charged_amount"] if r["charged_amount"] is not None else r["reserved_amount"]
        total += Decimal(amount)
    return count, total


def insert_ledger_reservation(
    conn: sqlite3.Connection, *, reservation_id: str, grant_id: str, grant_version: int,
    provider: str, model: str, provider_operation: str, idempotency_key: str,
    envelope_id: str, authorization_id: str, reserved_amount: Decimal, at: str,
) -> LedgerReservation:
    """Atomic INSERT, state='reserved'. UNIQUE(grant_id, provider,
    model, provider_operation, idempotency_key) gives idempotent replay
    (same envelope_id -> return existing row) or a conflict error
    (different envelope_id -> the 'ambiguous' case from requirement 7)."""
    ensure_tables(conn)
    try:
        conn.execute(
            """
            INSERT INTO run_authorization_grant_consumption
                (reservation_id, grant_id, grant_version, provider, model, provider_operation,
                 idempotency_key, envelope_id, authorization_id, reserved_amount, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
            """,
            (
                reservation_id, grant_id, grant_version, provider, model, provider_operation,
                idempotency_key, envelope_id, authorization_id, str(reserved_amount), at, at,
            ),
        )
    except sqlite3.IntegrityError:
        existing = conn.execute(
            """
            SELECT * FROM run_authorization_grant_consumption
            WHERE grant_id = ? AND provider = ? AND model = ? AND provider_operation = ? AND idempotency_key = ?
            """,
            (grant_id, provider, model, provider_operation, idempotency_key),
        ).fetchone()
        if existing is None:
            raise
        if existing["envelope_id"] != envelope_id:
            raise LedgerReservationConflictError(
                f"idempotency_key {idempotency_key!r} already used for envelope_id {existing['envelope_id']!r}, "
                f"attempted with a different envelope_id {envelope_id!r}"
            )
        return _row_to_reservation(existing)

    row = conn.execute(
        "SELECT * FROM run_authorization_grant_consumption WHERE reservation_id = ?", (reservation_id,),
    ).fetchone()
    return _row_to_reservation(row)


def get_ledger_reservation(conn: sqlite3.Connection, reservation_id: str) -> Optional[LedgerReservation]:
    ensure_tables(conn)
    row = conn.execute(
        "SELECT * FROM run_authorization_grant_consumption WHERE reservation_id = ?", (reservation_id,),
    ).fetchone()
    return _row_to_reservation(row) if row else None


def transition_ledger_state(
    conn: sqlite3.Connection, reservation_id: str, *, from_state: str, to_state: str, at: str,
    charged_amount: Optional[Decimal] = None, reconciliation_resolution: Optional[str] = None,
) -> bool:
    """Conditional UPDATE ... WHERE state = <from_state> + rowcount
    check -- mirrors hypogeum.db.release_lease()/commit_budget_
    reservation(). Returns False (never raises) if the row was not in
    from_state -- a duplicate or racing settlement attempt simply
    no-ops rather than double-transitioning."""
    ensure_tables(conn)
    if to_state not in RESERVATION_STATES:
        raise AuthorizationStoreError(f"unknown reservation state {to_state!r}")
    cursor = conn.execute(
        """
        UPDATE run_authorization_grant_consumption
        SET state = ?, charged_amount = COALESCE(?, charged_amount),
            reconciliation_resolution = COALESCE(?, reconciliation_resolution), updated_at = ?
        WHERE reservation_id = ? AND state = ?
        """,
        (
            to_state, _decimal_to_str(charged_amount), reconciliation_resolution, at,
            reservation_id, from_state,
        ),
    )
    return cursor.rowcount == 1


# ---------------------------------------------------------------------------
# The one function hypogeum/submission_gate.py imports (Correction 3)
# ---------------------------------------------------------------------------

def revalidate_grant_reservation(
    conn: sqlite3.Connection, *, parent_grant_id: str, parent_grant_version: int,
    parent_grant_checksum: str, grant_reservation_id: str, at: str,
) -> None:
    """The ONLY grant-related function hypogeum.submission_gate.submit()
    ever calls. Read-only except for settling the one, already-existing
    reservation row it names -- it can never create a grant, a
    reservation, or an AUTHORIZED AuthorizationRecord, so calling it
    does not violate "the gate must not mint or broaden authorization."

    Raises a GrantRevalidationError subclass (never returns False --
    every failure here must abort submit() before the provider
    boundary) if: the grant no longer exists; is revoked; is expired as
    of `at`; its live checksum no longer matches parent_grant_checksum
    (superseded/altered since child-authorization issuance); or the
    named reservation is not currently in state 'reserved' (already
    boundary-entered by a concurrent path, already released, or
    otherwise inconsistent). On success, transitions the reservation
    'reserved' -> 'boundary_entered'.
    """
    ensure_tables(conn)
    grant = get_grant(conn, parent_grant_id, parent_grant_version)
    if grant is None:
        raise GrantNotFoundError(f"grant {parent_grant_id!r}@{parent_grant_version} not found")
    if grant.status != "active":
        raise GrantRevokedError(f"grant {parent_grant_id!r}@{parent_grant_version} is {grant.status!r}, not active")
    if at >= grant.expires_at:
        raise GrantExpiredError(f"grant {parent_grant_id!r}@{parent_grant_version} expired at {grant.expires_at!r}")
    if grant.grant_checksum != parent_grant_checksum:
        raise GrantChecksumMismatchError(
            f"grant {parent_grant_id!r}@{parent_grant_version} live checksum {grant.grant_checksum!r} "
            f"!= authorization-time checksum {parent_grant_checksum!r}"
        )

    reservation = get_ledger_reservation(conn, grant_reservation_id)
    if reservation is None:
        raise ReservationNotFoundError(f"reservation {grant_reservation_id!r} not found")
    if reservation.state != "reserved":
        raise ReservationStateMismatchError(
            f"reservation {grant_reservation_id!r} is in state {reservation.state!r}, not 'reserved'"
        )

    settled = transition_ledger_state(
        conn, grant_reservation_id, from_state="reserved", to_state="boundary_entered", at=at,
    )
    if not settled:
        # Lost a race against a concurrent boundary-entry attempt for
        # the exact same reservation -- fail closed rather than proceed
        # as if this call had won.
        raise ReservationStateMismatchError(
            f"reservation {grant_reservation_id!r} was concurrently settled by another attempt"
        )
