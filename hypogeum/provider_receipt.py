"""
hypogeum/provider_receipt.py — Immutable provider receipts and write-ahead
attempt evidence (Factory Core)

Implements Commit 2 of docs/architecture/AUTHORIZED_GENERATION_ENVELOPE_
IMPLEMENTATION_BLUEPRINT.md: the receipt model specified by docs/
architecture/AUTHORIZED_GENERATION_ENVELOPE.md §Phase 6, using the exact
persistence location the blueprint specifies -- file-backed JSON under
`{assets_root}/episodes/{execution_id}/receipts/{idempotency_key}/`, never
hypogeum.db directly (durability-critical write-ahead evidence must
survive independently of SQLite lock contention; see the blueprint's
"Persistence location" section). Runtime leases are the one piece of this
design that does live in hypogeum.db (schema.sql's runtime_leases table)
-- this module calls hypogeum.db.validate_lease() before any append that
needs lease protection, but never re-implements lease semantics itself.

This module implements no provider submission, no network access, and no
polling. It answers "what evidence already durably exists," never "go
find out what happened." The actual network boundary is a later commit's
responsibility (the mandatory submission gate, Commit 3, and the MUAPI
adapter conversion, Commit 4) -- this module only has to make it possible
for that later code to write ahead safely and to recover correctly if it
crashes.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, List, Mapping, Optional

__all__ = [
    "EventType",
    "RecoveryState",
    "ProviderReceiptEvent",
    "WriteAheadReceiptStore",
    "ReceiptValidationError",
    "DuplicateEventError",
    "MissingAcknowledgementError",
    "LeaseRequiredError",
]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# Evidence key names that must never appear in a receipt event -- Part 1
# requirement 15 ("No receipt object may contain provider credentials or
# secrets"). Deliberately generic (not tied to any one provider's naming),
# matched case-insensitively against evidence dict keys.
_FORBIDDEN_EVIDENCE_KEY_SUBSTRINGS = (
    "api_key", "apikey", "secret", "password", "credential", "token",
    "authorization_header", "signed_url", "private_key",
)


class ReceiptValidationError(Exception):
    """Raised when a receipt event is malformed, incomplete, contradictory,
    or otherwise fails closed at construction/append time. Never raised
    after a record has already been durably written -- once written, a
    record is immutable and this module never attempts to "fix" it."""

    def __init__(self, field_name: str, reason: str):
        self.field_name = field_name
        self.reason = reason
        super().__init__(f"[{field_name}] {reason}")


class DuplicateEventError(Exception):
    """Raised when an event identity (attempt + event_seq) already has a
    durably-written record whose content differs from what is being
    written now. An identical replay is not an error -- see
    WriteAheadReceiptStore's docstring for the idempotent-replay rule."""

    def __init__(self, path: Path, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


class MissingAcknowledgementError(Exception):
    """Raised when an event claims provider acknowledgement (ACCEPTED or
    SUBMITTED) without the provider evidence that would justify that
    claim. This is what makes it structurally impossible to write "yes,
    the provider accepted this" without actually having a provider_job_id
    (or provider_request_id) to show for it."""

    def __init__(self, event_type: "EventType", missing: str):
        self.event_type = event_type
        self.missing = missing
        super().__init__(f"{event_type.value} event claims acknowledgement but is missing evidence.{missing}")


class LeaseRequiredError(Exception):
    """Raised when an append is attempted without a currently-valid lease
    (wrong owner, wrong/stale fencing_token, expired, or never acquired)
    on the envelope this event protects."""

    def __init__(self, resource_id: str, owner_id: str, fencing_token: int):
        self.resource_id = resource_id
        self.owner_id = owner_id
        self.fencing_token = fencing_token
        super().__init__(
            f"no valid lease held by owner_id={owner_id!r} fencing_token={fencing_token!r} "
            f"on resource_id={resource_id!r}"
        )


class EventType(str, Enum):
    """The write-ahead sequence from architecture doc §6.2, plus two
    events that sequence doesn't name explicitly but §6.3's crash table
    requires to make recovery states distinguishable purely from durable
    evidence: SUBMISSION_ATTEMPT_STARTED (marks that the network boundary
    was actually approached, distinct from ABOUT_TO_SUBMIT's earlier
    write-ahead-only marker) and TERMINAL_FAILURE (a failure discovered
    before ever reaching the provider, e.g. a local budget check)."""

    ABOUT_TO_SUBMIT = "about_to_submit"
    SUBMISSION_ATTEMPT_STARTED = "submission_attempt_started"
    ACCEPTED = "accepted"
    UNKNOWN_ACCEPTANCE = "unknown_acceptance"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    DOWNLOADED = "downloaded"
    REGISTERED = "registered"
    TERMINAL_FAILURE = "terminal_failure"
    RECONCILED = "reconciled"


_ACKNOWLEDGEMENT_EVENT_TYPES = frozenset({EventType.ACCEPTED, EventType.SUBMITTED})
_RECONCILIATION_RESOLUTIONS = frozenset({"confirmed_accepted", "confirmed_not_submitted"})


class RecoveryState(str, Enum):
    """The eight persistence-level recovery classifications required by
    the parent task's Part 4. Reconstructed purely from durable event
    evidence -- see WriteAheadReceiptStore.classify_recovery_state()."""

    AUTHORIZED_NO_ATTEMPT = "authorized_no_attempt_prepared"
    ATTEMPT_PREPARED_NOT_ENTERED = "attempt_prepared_provider_boundary_not_entered"
    SUBMITTING_NO_ACKNOWLEDGEMENT = "submitting_possible_acceptance_no_acknowledgement"
    ACKNOWLEDGED_SUBMITTED = "acknowledged_provider_submission"
    DOWNLOADED = "provider_output_downloaded"
    TERMINALLY_FAILED_BEFORE_ACCEPTANCE = "terminally_failed_before_provider_acceptance"
    UNKNOWN_ACCEPTANCE_REQUIRES_RECONCILIATION = "unknown_acceptance_requires_reconciliation"
    COMPLETED_TERMINAL = "completed_terminal_authorization"


# Only these states may ever be treated as eligible to prepare/attempt a
# future submission -- Part 4 safety rule 1 ("Only states explicitly
# proven safe by durable evidence may be considered eligible for a future
# submission"). Every uncertain or already-attempted state is excluded.
ELIGIBLE_FOR_SUBMISSION_STATES = frozenset({RecoveryState.AUTHORIZED_NO_ATTEMPT})


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {f.name: _to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        _to_jsonable(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")


def _coerce_decimal(field_name: str, value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise ReceiptValidationError(field_name, "monetary amounts must be Decimal or str, never a binary float")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ReceiptValidationError(field_name, f"cannot be represented as an exact decimal: {value!r}") from exc


def _check_no_secrets(evidence: Mapping[str, Any]) -> None:
    for key in evidence:
        lowered = key.lower()
        for forbidden in _FORBIDDEN_EVIDENCE_KEY_SUBSTRINGS:
            if forbidden in lowered:
                raise ReceiptValidationError(
                    "evidence", f"evidence key {key!r} looks like a credential/secret and is never permitted in a receipt",
                )


@dataclass(frozen=True)
class ProviderReceiptEvent:
    """One immutable record binding an attempt to its exact authorized
    identity (Part 1 requirement 1). Never edited after construction;
    WriteAheadReceiptStore never rewrites a previously-written event."""

    envelope_id: str
    envelope_version: int
    authorization_id: str
    authorization_version: int
    attempt_id: str
    idempotency_key: str
    event_type: EventType
    event_seq: int
    at: str
    provider: str
    model: str
    provider_operation: str
    provider_bound_content_sha256: str
    max_cost_per_call: Decimal
    currency: str
    evidence: Mapping[str, Any]

    def __post_init__(self):
        _validate_event(self)

    def to_dict(self) -> dict:
        payload = {f.name: getattr(self, f.name) for f in fields(self)}
        return _to_jsonable(payload)

    def canonical_json_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProviderReceiptEvent":
        payload = dict(data)
        payload["event_type"] = EventType(payload["event_type"])
        payload["max_cost_per_call"] = _coerce_decimal("max_cost_per_call", payload["max_cost_per_call"])
        return cls(**payload)


def _validate_event(event: "ProviderReceiptEvent") -> None:
    if not isinstance(event.event_type, EventType):
        raise ReceiptValidationError("event_type", f"must be an EventType member, got {event.event_type!r}")
    for f in ("envelope_id", "authorization_id", "attempt_id", "idempotency_key",
              "provider", "model", "provider_operation", "at", "currency"):
        value = getattr(event, f)
        if not isinstance(value, str) or not value.strip():
            raise ReceiptValidationError(f, f"must be a non-empty string, got {value!r}")

    if not _SHA256_RE.match(event.provider_bound_content_sha256 or ""):
        raise ReceiptValidationError(
            "provider_bound_content_sha256",
            f"must be a 64-character lowercase hex sha256 digest, got {event.provider_bound_content_sha256!r}",
        )
    if not isinstance(event.max_cost_per_call, Decimal) or event.max_cost_per_call < 0:
        raise ReceiptValidationError("max_cost_per_call", f"must be a non-negative Decimal, got {event.max_cost_per_call!r}")
    if not _CURRENCY_RE.match(event.currency):
        raise ReceiptValidationError("currency", f"must be a 3-letter uppercase currency code, got {event.currency!r}")
    if not isinstance(event.envelope_version, int) or event.envelope_version < 1:
        raise ReceiptValidationError("envelope_version", f"must be a positive integer, got {event.envelope_version!r}")
    if not isinstance(event.authorization_version, int) or event.authorization_version < 1:
        raise ReceiptValidationError("authorization_version", f"must be a positive integer, got {event.authorization_version!r}")
    if not isinstance(event.event_seq, int) or event.event_seq < 0:
        raise ReceiptValidationError("event_seq", f"must be a non-negative integer, got {event.event_seq!r}")
    if not isinstance(event.evidence, Mapping):
        raise ReceiptValidationError("evidence", f"must be a mapping, got {type(event.evidence)!r}")

    _check_no_secrets(event.evidence)

    if event.event_type == EventType.ABOUT_TO_SUBMIT and event.event_seq != 0:
        raise ReceiptValidationError("event_seq", "the ABOUT_TO_SUBMIT event must be event_seq 0 (the first event of an attempt)")

    # Part 1 requirement 8/9: a receipt must not claim successful provider
    # submission without the acknowledgement evidence that justifies it,
    # and the absence of a response must never be silently read as
    # "not submitted" -- UNKNOWN_ACCEPTANCE is the explicit representation
    # of that uncertainty, never an inferred default.
    if event.event_type in _ACKNOWLEDGEMENT_EVENT_TYPES:
        if not (event.evidence.get("provider_job_id") or event.evidence.get("provider_request_id")):
            raise MissingAcknowledgementError(event.event_type, "provider_job_id or provider_request_id")

    if event.event_type == EventType.RECONCILED:
        resolution = event.evidence.get("resolution")
        if resolution not in _RECONCILIATION_RESOLUTIONS:
            raise ReceiptValidationError(
                "evidence.resolution",
                f"a RECONCILED event's evidence.resolution must be one of {sorted(_RECONCILIATION_RESOLUTIONS)}, got {resolution!r}",
            )


class WriteAheadReceiptStore:
    """
    File-backed, append-only, content-addressed evidence store.

    Layout (matches this repo's existing `.hypogeum-assets/episodes/
    {episode_id}/canary_receipts/` convention, generalized to the
    channel-agnostic `execution_id` terminology
    hypogeum.execution_envelope already uses):

        {assets_root}/episodes/{execution_id}/receipts/{idempotency_key}/
            0000_about_to_submit.json
            0001_submission_attempt_started.json
            0002_accepted.json
            ...

    Writes are atomic and durable: content is first written to a unique
    temp file in the same directory and fsync'd, then linked into its
    final name via os.link() -- which atomically fails with
    FileExistsError if that exact final name already exists, giving
    real filesystem-level "create if absent" semantics (a hard link
    either succeeds completely or not at all; there is no partially-
    linked state). This is the write-ahead transaction boundary Part 2
    requirement 1 describes: if this call raises, nothing was written,
    and the caller has zero durable evidence to treat as submission-
    eligible (Part 2 requirement 3).

    Idempotent replay (Part 1 requirement 14): if the target file already
    exists with byte-identical content to what is being written, the
    write is treated as a successful no-op rather than an error --
    exactly replaying an already-recorded event is safe. Any other
    pre-existing content at that path raises DuplicateEventError (Part 1
    requirement 13) -- this store never silently overwrites a mismatched
    duplicate (Part 2 requirement 8) and never deletes or rewrites
    historical evidence (Part 1 requirement 4, Part 2 requirement 6-7).
    """

    def __init__(self, assets_root: Path):
        self.assets_root = Path(assets_root)

    def _attempt_dir(self, execution_id: str, idempotency_key: str) -> Path:
        return self.assets_root / "episodes" / execution_id / "receipts" / idempotency_key

    def prepare_attempt(self, event: ProviderReceiptEvent, *, execution_id: str) -> ProviderReceiptEvent:
        """The one write-ahead call that may happen before any future
        network boundary is crossed. event must be event_seq=0,
        event_type=ABOUT_TO_SUBMIT -- enforced by _validate_event()
        already, re-asserted here defensively since this is the
        specific entry point Part 2's "atomic transaction" language
        refers to."""
        if event.event_type != EventType.ABOUT_TO_SUBMIT or event.event_seq != 0:
            raise ReceiptValidationError(
                "event_type", "prepare_attempt() requires event_type=ABOUT_TO_SUBMIT and event_seq=0",
            )
        attempt_dir = self._attempt_dir(execution_id, event.idempotency_key)
        path = attempt_dir / f"{event.event_seq:04d}_{event.event_type.value}.json"
        return self._write_exclusive_or_verify_replay(attempt_dir, path, event)

    def append_event(
        self,
        event: ProviderReceiptEvent,
        *,
        execution_id: str,
        lease_conn=None,
        lease_owner_id: Optional[str] = None,
        lease_fencing_token: Optional[int] = None,
    ) -> ProviderReceiptEvent:
        """
        Appends a subsequent lifecycle event to an already-prepared
        attempt. Refuses (ReceiptValidationError) if no prior
        ABOUT_TO_SUBMIT record exists for this idempotency_key -- an
        attempt must be prepared before anything can be appended to it.

        If lease_conn is given, validates (lease_owner_id,
        lease_fencing_token) against hypogeum.db.validate_lease() for
        resource_id=event.envelope_id before writing, raising
        LeaseRequiredError if the lease does not currently belong to
        that exact owner/token pair. This is what makes "a stale owner
        must not ... append protected attempt evidence" (architecture
        Part 3 requirement 5) hold structurally rather than by
        convention: the check happens inside the store itself, not left
        to every future caller to remember.
        """
        attempt_dir = self._attempt_dir(execution_id, event.idempotency_key)
        if not (attempt_dir / "0000_about_to_submit.json").exists():
            raise ReceiptValidationError(
                "idempotency_key",
                f"no prepared attempt found for idempotency_key={event.idempotency_key!r} -- "
                f"call prepare_attempt() first",
            )

        if lease_conn is not None:
            from hypogeum.db import validate_lease  # local import: db.py is optional for pure-store use
            if not validate_lease(lease_conn, event.envelope_id, lease_owner_id, lease_fencing_token, at=event.at):
                raise LeaseRequiredError(event.envelope_id, lease_owner_id, lease_fencing_token)

        path = attempt_dir / f"{event.event_seq:04d}_{event.event_type.value}.json"
        return self._write_exclusive_or_verify_replay(attempt_dir, path, event)

    def _write_exclusive_or_verify_replay(
        self, attempt_dir: Path, path: Path, event: ProviderReceiptEvent,
    ) -> ProviderReceiptEvent:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        payload_bytes = event.canonical_json_bytes()

        if path.exists():
            if path.read_bytes() == payload_bytes:
                return event  # exact replay of an already-recorded event -- idempotent
            raise DuplicateEventError(path, "already exists with different content")

        tmp_path = attempt_dir / f".tmp_{path.name}.{os.getpid()}"
        with open(tmp_path, "wb") as f:
            f.write(payload_bytes)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp_path, path)
        except FileExistsError:
            # lost a race with a concurrent writer between our exists()
            # check and this link() call -- resolve exactly like above.
            if path.read_bytes() == payload_bytes:
                return event
            raise DuplicateEventError(path, "already exists with different content (race)")
        finally:
            os.unlink(tmp_path)
        return event

    def read_events(self, execution_id: str, idempotency_key: str) -> List[ProviderReceiptEvent]:
        """Every durably-recorded event for this attempt, in event_seq
        order. Returns an empty list if no attempt has ever been
        prepared -- never raises for "not found"."""
        attempt_dir = self._attempt_dir(execution_id, idempotency_key)
        if not attempt_dir.exists():
            return []
        files = sorted(p for p in attempt_dir.glob("*.json") if not p.name.startswith(".tmp"))
        return [ProviderReceiptEvent.from_dict(json.loads(p.read_text())) for p in files]

    def classify_recovery_state(self, execution_id: str, idempotency_key: str) -> RecoveryState:
        """The persistence-level recovery classification (Part 4),
        reconstructed purely from durable evidence -- never from an
        in-memory flag, and never performing network access."""
        return _classify(self.read_events(execution_id, idempotency_key))


def _classify(events: List[ProviderReceiptEvent]) -> RecoveryState:
    if not events:
        return RecoveryState.AUTHORIZED_NO_ATTEMPT

    by_type = [e.event_type for e in events]

    if EventType.RECONCILED in by_type:
        last_reconciled = [e for e in events if e.event_type == EventType.RECONCILED][-1]
        resolution = last_reconciled.evidence.get("resolution")
        if resolution == "confirmed_accepted":
            return RecoveryState.ACKNOWLEDGED_SUBMITTED
        if resolution == "confirmed_not_submitted":
            return RecoveryState.TERMINALLY_FAILED_BEFORE_ACCEPTANCE
        return RecoveryState.UNKNOWN_ACCEPTANCE_REQUIRES_RECONCILIATION

    if EventType.REGISTERED in by_type or EventType.REJECTED in by_type:
        return RecoveryState.COMPLETED_TERMINAL
    if EventType.DOWNLOADED in by_type:
        return RecoveryState.DOWNLOADED
    if EventType.UNKNOWN_ACCEPTANCE in by_type:
        return RecoveryState.UNKNOWN_ACCEPTANCE_REQUIRES_RECONCILIATION
    if EventType.SUBMITTED in by_type or EventType.ACCEPTED in by_type:
        return RecoveryState.ACKNOWLEDGED_SUBMITTED
    if EventType.TERMINAL_FAILURE in by_type:
        return RecoveryState.TERMINALLY_FAILED_BEFORE_ACCEPTANCE
    if EventType.SUBMISSION_ATTEMPT_STARTED in by_type:
        return RecoveryState.SUBMITTING_NO_ACKNOWLEDGEMENT
    if EventType.ABOUT_TO_SUBMIT in by_type:
        return RecoveryState.ATTEMPT_PREPARED_NOT_ENTERED
    return RecoveryState.AUTHORIZED_NO_ATTEMPT
