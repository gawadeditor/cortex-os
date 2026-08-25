"""
hypogeum/budget_scopes.py — Exact-Decimal budget scopes and circuit
breakers (Factory Core)

Implements Commit 3 / Part 2 and Part 3 of docs/architecture/
AUTHORIZED_GENERATION_ENVELOPE_IMPLEMENTATION_BLUEPRINT.md, per
docs/architecture/AUTHORIZED_GENERATION_ENVELOPE.md §Phase 7.

Thin Decimal-typed wrapper around hypogeum.db's budget_reservations and
circuit_breakers tables -- this module owns no persistence of its own and
never invents a second database; every atomic guarantee (no double
reservation, monotonic breaker trip) comes from hypogeum.db's
BEGIN-IMMEDIATE / conditional-UPDATE primitives, reused here, not
reimplemented.

No network access, no provider knowledge, no episode-file I/O.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Mapping, Optional, Sequence

import hypogeum.db as db

__all__ = [
    "BudgetScopeKind",
    "ScopeRequest",
    "BudgetReservation",
    "MultiScopeReservation",
    "BreakerEvidence",
    "BudgetError",
    "BudgetScopeMissingError",
    "BudgetCurrencyMismatchError",
    "BudgetCeilingExceededError",
    "OverchargeViolation",
    "reserve_all_scopes",
    "release_reservation",
    "release_all",
    "commit_reservation",
    "record_breaker",
    "is_tripped",
    "get_breaker",
    "reset_breaker",
]


class BudgetScopeKind(str, Enum):
    CALL = "call"
    RUN = "run"
    EPISODE = "episode"
    ORGANIZATION = "organization"
    RETRY = "retry"
    FALLBACK = "fallback"
    # Phase 3 (2026-08-25): per-channel weekly paid-video ceiling. scope_ref
    # is "<channel_id>:<ISO week key>" (e.g. "ch_x:2026-W35"); the window
    # itself is ISO Monday 00:00 -> next Monday 00:00 in the channel's own
    # configured timezone, resolved by hypogeum.weekly_budget and snapshotted
    # onto the reservation row as immutable window-evidence columns.
    CHANNEL_WEEKLY = "channel_weekly"


class BudgetError(Exception):
    """Base class for every budget failure in this module."""


class BudgetScopeMissingError(BudgetError):
    """A scope the envelope requires (e.g. run_level_budget_ref) was not
    supplied with a ceiling -- Part 2 requirement 3: missing required
    budget scope must deny submission."""

    def __init__(self, scope_kind: "BudgetScopeKind"):
        self.scope_kind = scope_kind
        super().__init__(f"no ceiling configured for required budget scope {scope_kind.value!r}")


class BudgetCurrencyMismatchError(BudgetError):
    def __init__(self, scope_kind: "BudgetScopeKind", requested: str, ceiling_currency: str):
        self.scope_kind = scope_kind
        self.requested = requested
        self.ceiling_currency = ceiling_currency
        super().__init__(
            f"{scope_kind.value}: requested currency {requested!r} != scope ceiling currency {ceiling_currency!r}"
        )


class BudgetCeilingExceededError(BudgetError):
    def __init__(self, scope_kind: "BudgetScopeKind", scope_ref: str):
        self.scope_kind = scope_kind
        self.scope_ref = scope_ref
        super().__init__(f"{scope_kind.value}:{scope_ref} ceiling would be exceeded by this reservation")


class OverchargeViolation(BudgetError):
    """Permanent: a provider-acknowledged charge exceeded the envelope's
    authorized max_cost_per_call. Part 2 requirement 10 -- this must trip
    a permanent circuit breaker and require reconciliation, never be
    silently absorbed."""

    def __init__(self, reservation_id: str, authorized: Decimal, charged: Decimal):
        self.reservation_id = reservation_id
        self.authorized = authorized
        self.charged = charged
        super().__init__(f"reservation {reservation_id}: charged {charged} exceeds authorized ceiling {authorized}")


@dataclass(frozen=True)
class ScopeRequest:
    """One scope this attempt must reserve against, before the gate
    evaluates the next one. `ceiling` is the scope's own maximum -- for
    CALL this is the envelope's max_cost_per_call; for RUN/EPISODE/
    ORGANIZATION it is whatever the caller's policy configured for that
    scope_ref (never read from the envelope itself, since a scope's
    remaining balance must never be a value the envelope carries -- see
    architecture §7.1).

    evidence -- optional immutable window attribution written onto the
    reservation row inside the same atomic insert (Phase 3: CHANNEL_WEEKLY
    carries channel_id / timezone snapshot / UTC window start+end /
    ceiling snapshot; every other scope kind leaves it None). Never a
    second write -- an accepted reservation can never exist without its
    evidence."""

    scope_kind: BudgetScopeKind
    scope_ref: str
    ceiling: Decimal
    currency: str
    evidence: Optional[Mapping] = None


@dataclass(frozen=True)
class BudgetReservation:
    reservation_id: str
    scope_kind: BudgetScopeKind
    scope_ref: str
    idempotency_key: str
    reserved_amount: Decimal
    currency: str
    status: str
    committed_amount: Optional[Decimal]
    # COMPENSATING-RELEASE OWNERSHIP (Phase 3 correction, 2026-08-25):
    # True only when the call that returned this reservation CREATED the
    # row inside its own transaction. A replayed row (identical
    # scope_kind/scope_ref/idempotency_key already existed) carries False
    # and belongs to whichever earlier attempt created it -- a replaying
    # attempt must never release it.
    created_by_this_call: bool = True


@dataclass(frozen=True)
class MultiScopeReservation:
    """The result of reserve_all_scopes(): every scope's reservation, all
    made or all denied together (see reserve_all_scopes' docstring for
    the compensating-release-on-partial-failure guarantee)."""

    idempotency_key: str
    reservations: Sequence[BudgetReservation]
    # True iff THIS call inserted every reservation in `reservations`.
    # False means at least one was an idempotent replay of an earlier
    # attempt's row (mixed create/replay is possible across scopes).
    all_created_by_this_call: bool = True

    def reservation_ids(self) -> Sequence[str]:
        return tuple(r.reservation_id for r in self.reservations)

    def created_by_this_call(self) -> bool:
        return self.all_created_by_this_call


@dataclass(frozen=True)
class BreakerEvidence:
    breaker_id: str
    breaker_type: str
    scope_kind: str
    scope_ref: str
    observed_value: str
    configured_limit: str
    tripped: bool
    triggering_attempt_id: Optional[str]
    tripped_at: Optional[str]


def _row_to_reservation(row: dict, *, created_by_this_call: bool = True) -> BudgetReservation:
    return BudgetReservation(
        reservation_id=row["reservation_id"],
        scope_kind=BudgetScopeKind(row["scope_kind"]),
        scope_ref=row["scope_ref"],
        idempotency_key=row["idempotency_key"],
        reserved_amount=Decimal(row["reserved_amount"]),
        currency=row["currency"],
        status=row["status"],
        committed_amount=Decimal(row["committed_amount"]) if row["committed_amount"] is not None else None,
        created_by_this_call=created_by_this_call,
    )


def _row_to_breaker(row: dict) -> BreakerEvidence:
    return BreakerEvidence(
        breaker_id=row["breaker_id"], breaker_type=row["breaker_type"],
        scope_kind=row["scope_kind"], scope_ref=row["scope_ref"],
        observed_value=row["observed_value"], configured_limit=row["configured_limit"],
        tripped=bool(row["tripped"]), triggering_attempt_id=row["triggering_attempt_id"],
        tripped_at=row["tripped_at"],
    )


def reserve_all_scopes(
    conn,
    scopes: Sequence[ScopeRequest],
    *,
    idempotency_key: str,
    amount: Decimal,
    currency: str,
    at: str,
    reservation_id_prefix: str,
) -> MultiScopeReservation:
    """
    Reserves `amount` against every scope in `scopes`, in order. If any
    scope denies (currency mismatch or ceiling exceeded), every scope
    already reserved earlier in this same call is compensating-released
    before raising -- so a caller never observes a partial multi-scope
    reservation (Part 2 requirement 2's "every applicable scope must
    pass before submission" holds as a single atomic-from-the-caller's-
    view operation, even though the underlying primitive is one
    transaction per scope).

    Each scope's own reservation is itself fully atomic (BEGIN IMMEDIATE
    inside hypogeum.db.reserve_budget()) against concurrent reservations
    for that scope specifically (Part 2 requirement 12).

    Raises BudgetCurrencyMismatchError or BudgetCeilingExceededError on
    the first scope that denies. Never raises BudgetScopeMissingError
    itself -- an empty `scopes` sequence is the caller's responsibility
    to avoid (see submission_gate.py, which raises
    BudgetScopeMissingError before ever calling this function when a
    required scope has no configured ceiling).
    """
    made: list = []
    created_flags: list = []
    try:
        for scope in scopes:
            if currency != scope.currency:
                raise BudgetCurrencyMismatchError(scope.scope_kind, currency, scope.currency)
            reservation_id = f"{reservation_id_prefix}:{scope.scope_kind.value}"
            accepted, row, inserted_now = db.reserve_budget(
                conn, reservation_id, scope.scope_kind.value, scope.scope_ref, idempotency_key,
                str(amount), currency, str(scope.ceiling), created_at=at,
                evidence=scope.evidence,
            )
            if not accepted:
                raise BudgetCeilingExceededError(scope.scope_kind, scope.scope_ref)
            made.append(_row_to_reservation(row, created_by_this_call=inserted_now))
            created_flags.append(inserted_now)
    except BudgetError:
        # Compensating release touches ONLY rows this call created. A
        # replayed row (inserted_now=False) belongs to an earlier attempt;
        # releasing it here would destroy another attempt's live or
        # unknown-held reservation (Phase 3 ownership correction).
        for reservation, inserted in zip(made, created_flags):
            if inserted:
                db.release_budget_reservation(conn, reservation.reservation_id, at=at)
        raise
    return MultiScopeReservation(
        idempotency_key=idempotency_key,
        reservations=tuple(made),
        all_created_by_this_call=all(created_flags) if created_flags else True,
    )


def release_reservation(conn, reservation_id: str, *, at: str) -> bool:
    return db.release_budget_reservation(conn, reservation_id, at=at)


def release_all(conn, multi: MultiScopeReservation, *, at: str) -> None:
    for reservation in multi.reservations:
        db.release_budget_reservation(conn, reservation.reservation_id, at=at)


def commit_reservation(
    conn, reservation_id: str, *, authorized_ceiling: Decimal, charged_amount: Decimal, at: str,
) -> str:
    """
    Commits actual provider-charged spend to a reservation -- only ever
    called from durable provider acknowledgement evidence (Part 2
    requirement 9). If charged_amount exceeds authorized_ceiling, raises
    OverchargeViolation (Part 2 requirement 10) -- the commit itself
    still proceeds (the charge is real and must be recorded, never
    hidden), but the caller (hypogeum.submission_gate) is required to
    treat the raised exception as grounds for a permanent circuit-breaker
    trip.

    Returns the same status vocabulary as hypogeum.db.commit_budget_
    reservation(): "committed", "already_committed_same",
    "already_committed_conflict", or "not_reserved".
    """
    status = db.commit_budget_reservation(conn, reservation_id, str(charged_amount), at=at)
    if charged_amount > authorized_ceiling:
        raise OverchargeViolation(reservation_id, authorized_ceiling, charged_amount)
    return status


def record_breaker(
    conn,
    breaker_id: str,
    *,
    breaker_type: str,
    scope_kind: str,
    scope_ref: str,
    observed_value,
    configured_limit,
    tripped: bool,
    triggering_attempt_id: Optional[str],
    evidence: Mapping,
    at: str,
) -> BreakerEvidence:
    """Records breaker evidence -- observed_value/configured_limit may be
    any value convertible to str (Decimal, int, ...). See
    hypogeum.db.record_breaker_evidence() for the monotonic-tripped
    guarantee this delegates to."""
    row = db.record_breaker_evidence(
        conn, breaker_id, breaker_type=breaker_type, scope_kind=scope_kind, scope_ref=scope_ref,
        observed_value=str(observed_value), configured_limit=str(configured_limit),
        tripped=tripped, triggering_attempt_id=triggering_attempt_id,
        evidence_json=json.dumps(dict(evidence), sort_keys=True), at=at,
    )
    return _row_to_breaker(row)


def is_tripped(conn, breaker_id: str) -> bool:
    return db.is_breaker_tripped(conn, breaker_id)


def get_breaker(conn, breaker_id: str) -> Optional[BreakerEvidence]:
    row = db.get_breaker(conn, breaker_id)
    return _row_to_breaker(row) if row else None


def reset_breaker(conn, breaker_id: str, *, reset_by: str, at: str) -> bool:
    """The one explicit, documented reset path (Part 3 requirement 4) --
    never called automatically by anything in this module."""
    return db.reset_breaker(conn, breaker_id, reset_by=reset_by, at=at)
