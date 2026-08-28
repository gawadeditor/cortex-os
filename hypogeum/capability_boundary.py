"""
hypogeum/capability_boundary.py — the single authoritative verifier +
durable single-use consumer for EVERY AdapterInvocationCapability
consumer (Factory Core, provider-boundary layer, 2026-08-28).

Every real provider adapter (`hypogeum/providers/*_adapter.py`) calls
`verify_and_consume_capability()` at the very top of its `invoke()`,
before any provider I/O. This module -- not the individual adapters --
owns:

  * capability checksum INTEGRITY (a tampered field with a stale
    checksum is rejected);
  * capability AUTHENTICITY against authoritative persisted state -- a
    fully fabricated `AdapterInvocationCapability` whose checksum the
    caller honestly recomputed with `compute_capability_checksum()` is
    still rejected, because no matching durable issuance backs it:
      - `hypogeum.authorization_store.get_execution_envelope()` must
        return an integrity-verified envelope for
        (envelope_id, envelope_version), and every identity family on
        the capability must equal it;
      - `get_authorization_record()` must return an AUTHORIZED child
        authorization bound to that exact envelope, derived from a
        signed run grant (parent_grant_id + grant_reservation_id) or an
        explicit human approval;
      - the `WriteAheadReceiptStore` trail for
        (execution_id, idempotency_key) must already carry the gate's
        own ABOUT_TO_SUBMIT (seq 0) + SUBMISSION_ATTEMPT_STARTED
        boundary records, matching the capability;
      - the CURRENT lease table (`db.validate_lease`) -- not a value
        copied into a receipt -- must show (lease_owner_id,
        lease_fencing_token) still holding the envelope lease;
      - capability `issued_at`/`expires_at` must place "now" (an
        injected clock) inside the capability TTL window;
      - when the persisted envelope binds a plan-weekly reservation
        (Closure 0), the full reservation identity + state is verified;
  * DURABLE, ATOMIC, RESTART- AND CONCURRENCY-SAFE single use -- before
    returning, this module appends its own consumption-claim event to
    the durable receipt trail via `WriteAheadReceiptStore.append_event`
    (os.link create-if-absent). A second call -- concurrent, or from a
    brand-new adapter instance after a process restart -- finds either
    that claim (or a later ACCEPTED/REJECTED) already durable and fails
    closed. The adapters' in-memory `_consumed_capability_ids` set is a
    same-process cache ONLY, passed in as `consumed_cache`, never the
    source of truth.

The returned `VerifiedInvocationContext` is what an adapter's
`_invoke_real()` receives. It cannot be manufactured from ordinary
caller input: `assert_live()` re-reads the durable receipt trail and
requires that the context's `claim_nonce` is the current HEAD of that
trail -- a hand-constructed context carries a nonce that is not durably
recorded and is rejected before provider I/O.

This module performs no provider I/O, no polling, and no network access.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from hypogeum.providers.fake_provider_adapter import (
    AdapterInvocationCapability,
    CapabilityAlreadyConsumedError,
    InvalidCapabilityError,
    compute_capability_checksum,
)

__all__ = [
    "CAPABILITY_TTL_SECONDS",
    "CLOCK_SKEW_SECONDS",
    "CapabilityAuthenticityError",
    "CapabilityExpiredError",
    "VerifiedInvocationContext",
    "verify_and_consume_capability",
    "default_expiry_for",
    "now_utc",
]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Redirected in tests exactly like hypogeum.db._DEFAULT_DB_PATH. Used only
# as the last-resort receipt-trail root when a caller (a direct
# adapter.invoke() with no submission_gate receipt_store) supplies none.
_DEFAULT_ASSETS_ROOT = _PROJECT_ROOT / ".hypogeum-assets"

# A capability is short-lived. submission_gate.submit() stamps
# issued_at = at and expires_at = at + CAPABILITY_TTL_SECONDS unless the
# caller supplied an explicit capability_expires_at. Deliberately much
# shorter than the 30-minute envelope lease: a capability authorizes ONE
# imminent provider call, not a long-lived hold.
CAPABILITY_TTL_SECONDS = 900
CLOCK_SKEW_SECONDS = 120

# Receipt-trail phase tags for this module's two durable atomic claims:
#  - the CAPABILITY consumption claim, written by
#    verify_and_consume_capability() (one per verified capability);
#  - the provider-I/O ENTRY claim, written by
#    VerifiedInvocationContext.claim_io_entry() at the deepest controlled
#    boundary immediately before paid provider I/O (one per attempt, ever).
# Both are os.link create-if-absent appends; their presence makes replay
# (concurrent, sequential, reconstructed-context, or post-restart) fail
# closed.
_CONSUMPTION_PHASE = "capability_boundary_consumption_claim"
_IO_ENTRY_PHASE = "capability_boundary_provider_io_entry_claim"

# classify_recovery_state() values that mean "this attempt already
# reached (or may have reached) the provider once" -> the capability is
# spent, never re-executable.
_SPENT_RECOVERY_STATE_VALUES = frozenset({
    "acknowledged_provider_submission",
    "provider_output_downloaded",
    "unknown_acceptance_requires_reconciliation",
    "terminally_failed_before_provider_acceptance",
    "completed_terminal_authorization",
})
_LIVE_PRE_BOUNDARY_STATE_VALUE = "submitting_possible_acceptance_no_acknowledgement"


class CapabilityAuthenticityError(InvalidCapabilityError):
    """The capability could not be matched to an authoritative, durable
    submission-gate issuance -- a missing or identity-mismatched
    execution envelope, child authorization, signed-grant derivation,
    write-ahead receipt trail, current lease, plan/reservation binding,
    or recovery state. A recomputed capability_checksum does not help;
    authenticity is proven against persisted state, never the checksum.
    Fails closed before any provider network I/O."""


class CapabilityExpiredError(InvalidCapabilityError):
    """"Now" (an injected authoritative clock) is outside the
    capability's [issued_at, expires_at) TTL window -- either issued in
    the future beyond allowed clock skew, or already expired. An expired
    unconsumed capability is never replayed or date-adjusted; a newly
    authorized attempt is required. Expiry alone releases no financial
    exposure and never makes an ambiguous attempt retryable."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> datetime:
    s = (value or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def default_expiry_for(issued_at_iso: str, *, ttl_seconds: int = CAPABILITY_TTL_SECONDS) -> str:
    """submission_gate.submit() default: expires_at strictly later than
    issued_at."""
    return (_parse_iso(issued_at_iso) + timedelta(seconds=max(1, ttl_seconds))).isoformat()


def _assert_authoritative_receipt_path(assets_root, execution_id: str, idempotency_key: str) -> None:
    """The receipts attempt directory is derived SOLELY from the
    deployment-owned assets root + persisted execution_id + idempotency_key
    (never a caller value). This inspects the derived path COMPONENT BY
    COMPONENT rather than blindly trusting resolve(): if any component
    beneath the assets root -- episodes/, {execution_id}/, receipts/,
    {idempotency_key}/ -- is a symlink, or if the real attempt directory
    would fall outside the configured assets root, it fails closed. A
    symlink to a POPULATED foreign directory holding a byte-identical
    valid receipt trail is rejected exactly like a symlink to an empty
    one. The normal, non-symlink canonical receipt path is untouched."""
    from hypogeum.provider_receipt import AmbiguousReceiptsRootError, resolve_episode_receipts_root

    root = Path(assets_root)
    try:
        receipts_root = resolve_episode_receipts_root(root, execution_id)
    except AmbiguousReceiptsRootError as exc:
        raise CapabilityAuthenticityError(
            f"cannot resolve the authoritative receipts location for execution_id={execution_id!r}: {exc}"
        ) from exc
    attempt_dir = receipts_root / idempotency_key
    try:
        rel_parts = attempt_dir.relative_to(root).parts
    except ValueError as exc:
        raise CapabilityAuthenticityError(
            f"authoritative receipts path {attempt_dir} is not under the configured assets root {root}"
        ) from exc

    cur = root
    for part in rel_parts:
        cur = cur / part
        if cur.is_symlink():
            raise CapabilityAuthenticityError(
                f"authoritative receipt path component {cur} is a symlink -- refusing a substituted "
                f"receipt trail (populated or empty)"
            )

    base = root.resolve(strict=False)
    real = attempt_dir.resolve(strict=False)
    if real != base and base not in real.parents:
        raise CapabilityAuthenticityError(
            f"authoritative receipt directory {real} escapes the configured assets root {base}"
        )


@dataclass(frozen=True)
class VerifiedInvocationContext:
    """Handed to an adapter's `_invoke_real()`. Carries the verified
    envelope + decoded payload, and re-asserts liveness against durable
    state so a hand-constructed instance cannot be used to reach provider
    I/O. `_assets_root` is the module's own authoritative receipts root
    snapshot (never caller input)."""

    capability: AdapterInvocationCapability
    envelope: Any                       # hypogeum.execution_envelope.GenerationExecutionEnvelope
    operation_payload: dict
    execution_id: str
    idempotency_key: str
    claim_nonce: str
    _assets_root: Path
    _db_path: Optional[Path]
    _clock: Callable[[], datetime]

    def _authoritative_events(self):
        from hypogeum.provider_receipt import WriteAheadReceiptStore, _classify

        _assert_authoritative_receipt_path(self._assets_root, self.execution_id, self.idempotency_key)
        store = WriteAheadReceiptStore(self._assets_root)
        events = store.read_events(self.execution_id, self.idempotency_key)
        return store, events, _classify(events)

    def assert_live(self) -> None:
        """Re-verify against authoritative durable state that THIS context
        was produced by verify_and_consume_capability() for an attempt
        that has not since gone terminal or expired. A context built by a
        caller (bypassing verify_and_consume_capability) carries a
        `claim_nonce` that is not durably recorded -> raises."""
        _store, events, state = self._authoritative_events()
        if not any(
            e.evidence.get("phase") == _CONSUMPTION_PHASE
            and e.evidence.get("invocation_nonce") == self.claim_nonce
            for e in events
        ):
            raise CapabilityAuthenticityError(
                f"verified context for capability {self.capability.capability_id!r} is stale or "
                f"fabricated: no durable consumption claim in the authoritative receipt trail "
                f"matches this context"
            )
        if state.value in _SPENT_RECOVERY_STATE_VALUES:
            raise CapabilityAlreadyConsumedError(self.capability.capability_id)
        now = self._clock()
        if now >= _parse_iso(self.capability.expires_at):
            raise CapabilityExpiredError(
                f"capability {self.capability.capability_id!r} expired at "
                f"{self.capability.expires_at!r} (now {now.isoformat()!r})"
            )

    def claim_io_entry(self) -> str:
        """The deepest durable atomic gate immediately before paid
        provider I/O. Exactly one call per attempt EVER succeeds:

          - a second sequential call finds the durable I/O-entry claim
            already present -> CapabilityAlreadyConsumedError;
          - two concurrent calls race on os.link create-if-absent ->
            exactly one wins, the loser raises;
          - a reconstructed/replayed context fails assert_live() first
            (its consumption nonce is not durable), or finds the claim;
          - a fresh adapter after a process restart cannot re-verify the
            capability at all (its consumption claim is already durable);
          - a crash AFTER this claim leaves the attempt spent: the
            capability can never be re-verified, and this claim can never
            be re-made -- never silently retryable.

        Never releases or reduces financial exposure. Returns the
        I/O-entry nonce for evidence/logging."""
        from hypogeum.provider_receipt import (
            DuplicateEventError, EventType, ProviderReceiptEvent, WriteAheadReceiptStore,
        )

        self.assert_live()
        store, events, _state = self._authoritative_events()
        if any(e.evidence.get("phase") == _IO_ENTRY_PHASE for e in events):
            raise CapabilityAlreadyConsumedError(
                f"{self.capability.capability_id}: provider-I/O entry already claimed for this attempt"
            )
        env = self.envelope
        io_nonce = uuid.uuid4().hex
        ev = ProviderReceiptEvent(
            envelope_id=env.envelope_id, envelope_version=env.envelope_version,
            authorization_id=env.authorization_id, authorization_version=env.authorization_version,
            attempt_id=self.capability.attempt_id, idempotency_key=self.idempotency_key,
            event_type=EventType.SUBMISSION_ATTEMPT_STARTED, event_seq=len(events),
            at=self._clock().isoformat(),
            provider=env.provider, model=env.model, provider_operation=env.provider_operation,
            provider_bound_content_sha256=env.operation_payload_sha256,
            max_cost_per_call=env.max_cost_per_call, currency=env.currency,
            evidence={
                "phase": _IO_ENTRY_PHASE, "consumer": "capability_boundary",
                "capability_id": self.capability.capability_id, "invocation_nonce": io_nonce,
                "parent_consumption_nonce": self.claim_nonce,
            },
        )
        try:
            store.append_event(ev, execution_id=self.execution_id)
        except (DuplicateEventError, FileExistsError, FileNotFoundError, OSError) as exc:
            # Any failure to durably write the atomic claim -- a lost
            # os.link race, a concurrent tmp-file collision, or any other
            # filesystem error -- is CONSERVATIVE: the caller must NOT
            # proceed to provider I/O.
            raise CapabilityAlreadyConsumedError(
                f"{self.capability.capability_id}: could not durably claim provider-I/O entry ({exc})"
            ) from exc
        return io_nonce


def verify_and_consume_capability(
    capability: AdapterInvocationCapability,
    *,
    expected_schema_version: Optional[str] = None,
    consumed_cache: Optional[set] = None,
    db_path: Optional[Path] = None,
    clock: Optional[Callable[[], datetime]] = None,
    clock_skew_seconds: int = CLOCK_SKEW_SECONDS,
) -> VerifiedInvocationContext:
    """The one authoritative verifier + durable single-use consumer.
    Raises (all before any provider I/O):

        InvalidCapabilityError        -- not an AdapterInvocationCapability,
                                         checksum mismatch, or wrong schema.
        CapabilityAuthenticityError   -- no matching durable issuance.
        CapabilityExpiredError        -- outside the TTL window.
        CapabilityAlreadyConsumedError-- already used (cache, durable trail,
                                         recovery state, or a lost
                                         concurrent os.link race).
    """
    import hypogeum.authorization_store as authorization_store
    import hypogeum.db as db
    from hypogeum.authorization_lifecycle import AuthorizationState
    from hypogeum.provider_receipt import (
        DuplicateEventError,
        EventType,
        ProviderReceiptEvent,
        WriteAheadReceiptStore,
    )

    clk = clock or now_utc

    if not isinstance(capability, AdapterInvocationCapability):
        raise InvalidCapabilityError(
            f"expected AdapterInvocationCapability, got {type(capability)!r}"
        )

    # ---- 1) checksum INTEGRITY (necessary, never sufficient) ----
    recomputed = compute_capability_checksum(capability)
    if recomputed != capability.capability_checksum:
        raise InvalidCapabilityError(
            f"capability {capability.capability_id!r} checksum mismatch: "
            f"stored {capability.capability_checksum!r} != recomputed {recomputed!r}"
        )
    if expected_schema_version is not None and \
            capability.operation_payload_schema_version != expected_schema_version:
        raise InvalidCapabilityError(
            f"adapter accepts {expected_schema_version!r} payloads, got "
            f"{capability.operation_payload_schema_version!r}"
        )
    if consumed_cache is not None and capability.capability_id in consumed_cache:
        raise CapabilityAlreadyConsumedError(capability.capability_id)

    try:
        operation_payload = json.loads(capability.operation_payload_bytes)
        if not isinstance(operation_payload, dict):
            raise ValueError("operation payload is not a JSON object")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCapabilityError(f"operation_payload_bytes is not a valid JSON object: {exc}") from exc

    def _fail(reason: str) -> CapabilityAuthenticityError:
        return CapabilityAuthenticityError(
            f"capability {capability.capability_id!r} is not backed by an authoritative "
            f"submission-gate issuance: {reason}"
        )

    conn = db.get_connection(db_path)
    try:
        # ---- 2) durable, integrity-verified execution envelope ----
        try:
            env = authorization_store.get_execution_envelope(
                conn, capability.envelope_id, capability.envelope_version,
            )
        except Exception as exc:  # noqa: BLE001 -- any store failure = not authentic
            raise _fail(
                f"no durably-stored execution envelope for "
                f"envelope_id={capability.envelope_id!r}@{capability.envelope_version} ({exc})"
            ) from exc

        for cap_val, env_val, name in (
            (capability.provider, env.provider, "provider"),
            (capability.model, env.model, "model"),
            (capability.provider_operation, env.provider_operation, "provider_operation"),
            (capability.operation_payload_sha256, env.operation_payload_sha256, "operation_payload_sha256"),
            (capability.operation_payload_schema_version, env.operation_payload_schema_version, "operation_payload_schema_version"),
            (capability.currency, env.currency, "currency"),
            (capability.idempotency_key, env.idempotency_key, "idempotency_key"),
            (capability.attempt_id, env.authorization_id, "attempt_id/authorization_id"),
            (str(capability.max_cost_per_call), str(env.max_cost_per_call), "max_cost_per_call"),
        ):
            if cap_val != env_val:
                raise _fail(f"{name} {cap_val!r} != persisted envelope {env_val!r}")
        if not (env.channel_id and str(env.channel_id).strip()):
            raise _fail("persisted envelope carries no channel_id")
        if not (env.execution_id and str(env.execution_id).strip()):
            raise _fail("persisted envelope carries no execution_id")
        execution_id = env.execution_id

        # ---- 3) AUTHORIZED child authorization, signed-grant / explicit-approval derived ----
        auth = authorization_store.get_authorization_record(conn, env.authorization_id)
        if auth is None:
            raise _fail(f"no child authorization record for authorization_id={env.authorization_id!r}")
        if auth.envelope_id != env.envelope_id or auth.envelope_version != env.envelope_version:
            raise _fail("child authorization does not reference this exact envelope id/version")
        if auth.authorization_version != env.authorization_version:
            raise _fail("child authorization_version does not match the envelope")
        if auth.current_state != AuthorizationState.AUTHORIZED:
            raise _fail(f"child authorization state is {auth.current_state.value!r}, not 'authorized'")
        authorized_evidence = next(
            (e.evidence for e in auth.history if e.to_state == AuthorizationState.AUTHORIZED), {},
        )
        grant_derived = bool(
            authorized_evidence.get("parent_grant_id") and authorized_evidence.get("grant_reservation_id")
        )
        explicit_approval = bool(
            authorized_evidence.get("approval_id") or authorized_evidence.get("approver_identity")
            or authorized_evidence.get("authorization_method")
        )
        if not (grant_derived or explicit_approval):
            raise _fail("child authorization carries no signed-grant or explicit-approval derivation evidence")

        # ---- 3b) ADR-016 provider-domain re-check (defense in depth) ----
        # If the parent grant is a Channel Operating Grant, independently
        # re-verify -- against the freshly loaded signed grant -- that this
        # call's canonical provider domain is in allowed_provider_domains.
        # issue_child_authorization() already enforced this before minting
        # the AuthorizationRecord; this is a second, pre-I/O check.
        _pg_id = authorized_evidence.get("parent_grant_id")
        _pg_ver = authorized_evidence.get("parent_grant_version")
        if _pg_id is not None and _pg_ver is not None:
            _pg = authorization_store.get_grant(conn, _pg_id, int(_pg_ver))
            if _pg is not None and getattr(_pg, "grant_kind", "run") == "channel_operating":
                from hypogeum.provider_domains import (  # noqa: PLC0415
                    UnknownProviderOperationError, resolve_provider_domain,
                )
                _signed = set(getattr(_pg, "allowed_provider_domains", None) or [])
                if not _signed:
                    raise _fail("operating grant has no signed allowed_provider_domains")
                try:
                    _dom = resolve_provider_domain(env.provider, env.provider_operation)
                except UnknownProviderOperationError as _exc:
                    raise _fail(str(_exc)) from _exc
                if _dom not in _signed:
                    raise _fail(
                        f"call ({env.provider!r}, {env.provider_operation!r}) -> domain {_dom!r} "
                        f"not in signed allowed_provider_domains {sorted(_signed)!r}"
                    )

        # ---- 4) durable write-ahead receipt trail (AUTHORITATIVE LOCATION) ----
        # The receipt trail is resolved SOLELY from this module's own
        # _DEFAULT_ASSETS_ROOT (a fixed deployment constant, redirected in
        # tests exactly like hypogeum.db._DEFAULT_DB_PATH) plus the
        # persisted envelope's own execution_id + idempotency_key. No
        # caller supplies or selects it. _assert_authoritative_receipt_path
        # additionally refuses ANY symlink in the derived path (execution /
        # episode / receipts / attempt) and any escape outside the
        # configured assets root -- so a symlink to a POPULATED foreign
        # directory holding a byte-identical valid trail is rejected
        # exactly like a symlink to an empty one, and a copied trail at an
        # ordinary noncanonical path is simply never consulted.
        assets_root = Path(_DEFAULT_ASSETS_ROOT)
        _assert_authoritative_receipt_path(assets_root, execution_id, capability.idempotency_key)
        store = WriteAheadReceiptStore(assets_root)

        events = store.read_events(execution_id, capability.idempotency_key)
        if not events:
            raise _fail("no write-ahead receipt trail exists for this attempt (never entered the gate)")
        e0 = events[0]
        if e0.event_type != EventType.ABOUT_TO_SUBMIT or e0.event_seq != 0:
            raise _fail("receipt trail does not begin with the gate's ABOUT_TO_SUBMIT write-ahead record")
        for got, want, name in (
            (e0.envelope_id, capability.envelope_id, "receipt.envelope_id"),
            (e0.attempt_id, capability.attempt_id, "receipt.attempt_id"),
            (e0.idempotency_key, capability.idempotency_key, "receipt.idempotency_key"),
            (e0.provider, capability.provider, "receipt.provider"),
            (e0.model, capability.model, "receipt.model"),
            (e0.provider_operation, capability.provider_operation, "receipt.provider_operation"),
            (e0.provider_bound_content_sha256, capability.operation_payload_sha256, "receipt.payload_sha256"),
        ):
            if got != want:
                raise _fail(f"{name} {got!r} != capability {want!r}")

        boundary = next(
            (e for e in events if e.event_type == EventType.SUBMISSION_ATTEMPT_STARTED
             and e.evidence.get("phase") != _CONSUMPTION_PHASE),
            None,
        )
        if boundary is None:
            raise _fail("the gate's SUBMISSION_ATTEMPT_STARTED boundary-entry record is absent")
        if boundary.evidence.get("lease_owner_id") != capability.lease_owner_id or \
                boundary.evidence.get("lease_fencing_token") != capability.lease_fencing_token:
            raise _fail("capability lease owner / fencing token do not match the gate's boundary record")

        trail_reservation_ids = set(e0.evidence.get("reservation_ids") or [])
        if set(capability.budget_reservation_ids) != trail_reservation_ids:
            raise _fail("capability budget_reservation_ids do not match the gate's ABOUT_TO_SUBMIT record")
        if capability.issued_at != e0.at:
            raise _fail("capability issued_at does not match the gate's ABOUT_TO_SUBMIT timestamp")

        # ---- 5) capability TTL against an injected clock ----
        now = clk()
        issued = _parse_iso(capability.issued_at)
        expires = _parse_iso(capability.expires_at)
        if expires <= issued:
            raise _fail("capability expires_at is not strictly later than issued_at")
        if now < issued - timedelta(seconds=max(0, clock_skew_seconds)):
            raise CapabilityExpiredError(
                f"capability {capability.capability_id!r} is issued in the future "
                f"(issued_at {capability.issued_at!r}, now {now.isoformat()!r})"
            )
        if now >= expires:
            raise CapabilityExpiredError(
                f"capability {capability.capability_id!r} expired at {capability.expires_at!r} "
                f"(now {now.isoformat()!r})"
            )

        # ---- 6) CURRENT lease/fencing (authoritative table, not receipt copy) ----
        if not db.validate_lease(
            conn, env.envelope_id, capability.lease_owner_id, capability.lease_fencing_token,
            at=now.isoformat(),
        ):
            raise _fail(
                f"no CURRENT lease held by owner={capability.lease_owner_id!r} "
                f"fencing_token={capability.lease_fencing_token!r} on envelope {env.envelope_id!r}"
            )

        # ---- 7) MANDATORY post-plan plan + CHANNEL_WEEKLY binding ----
        # Every paid POST_PLAN capability invocation must be bound to the
        # authoritative canonical Production Plan AND the one
        # CHANNEL_WEEKLY reservation governing that channel/week. PRE_PLAN
        # work (episode scoring, research, narrative/script, first
        # narration, first plan compilation) is explicitly outside
        # Closure 0 -- handled by the next bounded pre-production closure.
        _verify_post_plan_weekly_binding(conn, env=env, capability=capability, now=now, fail=_fail)

        # ---- 8) recovery state + DURABLE ATOMIC single-use claim ----
        state = store.classify_recovery_state(execution_id, capability.idempotency_key)
        if state.value in _SPENT_RECOVERY_STATE_VALUES:
            raise CapabilityAlreadyConsumedError(capability.capability_id)
        if state.value != _LIVE_PRE_BOUNDARY_STATE_VALUE:
            raise _fail(
                f"recovery state {state.value!r} is not the live pre-boundary state the gate leaves "
                f"immediately before invoking the adapter"
            )
        if any(e.evidence.get("phase") == _CONSUMPTION_PHASE for e in events):
            raise CapabilityAlreadyConsumedError(capability.capability_id)

        claim_nonce = uuid.uuid4().hex
        claim = ProviderReceiptEvent(
            envelope_id=env.envelope_id, envelope_version=env.envelope_version,
            authorization_id=env.authorization_id, authorization_version=env.authorization_version,
            attempt_id=capability.attempt_id, idempotency_key=capability.idempotency_key,
            event_type=EventType.SUBMISSION_ATTEMPT_STARTED, event_seq=len(events),
            at=now.isoformat(),
            provider=env.provider, model=env.model, provider_operation=env.provider_operation,
            provider_bound_content_sha256=env.operation_payload_sha256,
            max_cost_per_call=env.max_cost_per_call, currency=env.currency,
            evidence={
                "phase": _CONSUMPTION_PHASE, "consumer": "capability_boundary",
                "capability_id": capability.capability_id, "invocation_nonce": claim_nonce,
            },
        )
        try:
            store.append_event(claim, execution_id=execution_id)
        except (DuplicateEventError, FileExistsError, FileNotFoundError, OSError) as exc:
            # Conservative: any failure to durably write the atomic
            # consumption claim (lost os.link race, concurrent tmp-file
            # collision, filesystem error) -> the capability is treated as
            # already consumed; the caller must NOT proceed.
            raise CapabilityAlreadyConsumedError(capability.capability_id) from exc

        if consumed_cache is not None:
            consumed_cache.add(capability.capability_id)

        return VerifiedInvocationContext(
            capability=capability, envelope=env, operation_payload=operation_payload,
            execution_id=execution_id, idempotency_key=capability.idempotency_key,
            claim_nonce=claim_nonce, _assets_root=assets_root, _db_path=db_path, _clock=clk,
        )
    finally:
        conn.close()


def _verify_post_plan_weekly_binding(conn, *, env, capability, now, fail):
    """MANDATORY for every paid POST_PLAN capability invocation.

    Resolves the authoritative canonical Production Plan for this episode
    (canonical episode directory only -- the accepted sole plan
    authority), then proves the invocation is bound to the ONE
    CHANNEL_WEEKLY reservation the accepted e20bdcf weekly-budget design
    establishes for (channel, episode, verified plan content_hash) in the
    CURRENT channel-week: it must exist, be for this channel, this
    ISO-week window, this currency, this exact plan-maximum amount, and
    be in a state the weekly-budget design authorizes ('reserved' or
    'committed'). Released, expired, foreign-channel, foreign-week,
    wrong-plan, wrong-amount, missing, or substituted reservations fail
    closed. This function only READS the ledger -- it never inserts a
    reservation (no second reservation during recursive adapter
    execution).

    PRE_PLAN work (no canonical plan yet) is explicitly outside Closure 0
    and returns without a weekly requirement.
    """
    import hypogeum.weekly_budget as weekly_budget
    from hypogeum.plan_reservation import plan_weekly_idempotency_key
    from hypogeum.production_plan import (
        PlanLifecycle, canonical_episode_dir, classify_plan_lifecycle, load_production_plan,
    )

    lifecycle = classify_plan_lifecycle(env.execution_id, None)
    if lifecycle is PlanLifecycle.PRE_PLAN:
        # PRE_PLAN paid work (episode scoring / research / narrative /
        # script / first narration) is bound to the ONE pre-production
        # CHANNEL_WEEKLY hold, not to a plan. Verify that binding
        # (defense in depth for the grant-derived gate paths).
        _verify_pre_plan_reservation_binding(conn, env=env, capability=capability, now=now, fail=fail)
        return

    plan = load_production_plan(env.execution_id, canonical_episode_dir(env.execution_id))
    if plan is None:
        raise fail(
            "a canonical Production Plan file exists for this episode but is not "
            "integrity-verifiable -- refusing paid post-plan provider work"
        )
    if getattr(plan, "channel_id", None) not in (None, env.channel_id):
        raise fail("canonical Production Plan names a different channel than the envelope")

    # ---- EXACT canonical-plan identity binding through the protected chain ----
    # The persisted execution envelope (integrity-verified by
    # get_execution_envelope: stored-blob checksum + recomputed
    # envelope_id) carries the exact Production Plan content_hash this
    # capability was ISSUED for -- either in audit_metadata (upscale /
    # image / thumbnail / narration / music: an explicit protected binding
    # added at issuance) or, for the video path, in the identity-protected
    # dependency_checksums.plan (which the video gate additionally
    # cross-checks against the signed parent grant via
    # expected_production_plan_content_hash). A previously issued
    # capability/envelope for Plan A does NOT become valid merely because
    # Plan B is now canonical and a valid Plan B reservation exists.
    _md = dict(getattr(env, "audit_metadata", None) or {})
    _dep = getattr(env, "dependency_checksums", None)
    if _dep is None:
        _dep_plan = None
    elif isinstance(_dep, Mapping):
        _dep_plan = _dep.get("plan")
    else:
        _dep_plan = getattr(_dep, "plan", None)
    bound_plan_hash = _md.get("production_plan_content_hash") or _dep_plan
    if not bound_plan_hash:
        raise fail(
            "the persisted execution envelope carries no exact Production Plan content_hash "
            "binding (neither audit_metadata.production_plan_content_hash nor "
            "dependency_checksums.plan) -- refusing paid post-plan provider work"
        )
    if bound_plan_hash != plan.content_hash:
        raise fail(
            f"the execution envelope / authorization chain is bound to Production Plan "
            f"{str(bound_plan_hash)[:12]!r} but the current canonical Production Plan is "
            f"{plan.content_hash[:12]!r} -- stale, superseded, or substituted plan binding"
        )

    try:
        plan_max = Decimal(str(plan.maximum_bounded_cost_usd))
    except (ValueError, ArithmeticError, TypeError) as exc:
        raise fail(f"verified Production Plan maximum_bounded_cost_usd is unreadable: {exc}") from exc
    if not plan_max.is_finite() or plan_max <= 0:
        raise fail(f"verified Production Plan maximum_bounded_cost_usd {plan_max} cannot authorize spend")

    # Authoritative current channel-week identity (channel's own timezone,
    # Monday 00:00 boundaries) -- computed from the injected clock, never
    # trusted from the row.
    try:
        tz_name, _ceiling = weekly_budget.resolve_channel_weekly_policy(conn, env.channel_id)
        window = weekly_budget.compute_weekly_window(now, tz_name)
        expected_scope_ref = weekly_budget.week_scope_ref(env.channel_id, window)
    except weekly_budget.WeeklyBudgetError as exc:
        raise fail(f"cannot resolve the channel's authoritative weekly window: {exc}") from exc

    # The weekly reservation MUST be the one derived for that same exact
    # plan identity (bound_plan_hash == plan.content_hash, just proven).
    idem = plan_weekly_idempotency_key(env.channel_id, env.execution_id, bound_plan_hash)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(budget_reservations)")}
    row = conn.execute(
        "SELECT * FROM budget_reservations "
        "WHERE scope_kind = 'channel_weekly' AND idempotency_key = ? AND scope_ref = ?",
        (idem, expected_scope_ref),
    ).fetchone()
    if row is None:
        raise fail(
            f"no CHANNEL_WEEKLY reservation for the canonical plan in the current channel-week "
            f"(idempotency_key={idem!r}, scope_ref={expected_scope_ref!r}) -- paid post-plan work "
            f"is not bound to the plan's weekly budget"
        )
    if "channel_id" in cols and row["channel_id"] not in (None, env.channel_id):
        raise fail(f"weekly reservation channel_id {row['channel_id']!r} != envelope {env.channel_id!r}")
    if str(row["currency"] or "") not in ("", capability.currency, env.currency):
        raise fail(f"weekly reservation currency {row['currency']!r} != {capability.currency!r}")
    if row["status"] not in ("reserved", "committed"):
        raise fail(
            f"weekly reservation status is {row['status']!r} -- only 'reserved'/'committed' are "
            f"authorized by the weekly-budget design (released/expired reservations are terminal)"
        )
    try:
        reserved_amount = Decimal(str(row["reserved_amount"]))
    except (ValueError, ArithmeticError) as exc:
        raise fail(f"weekly reservation reserved_amount is unreadable: {exc}") from exc
    if reserved_amount != plan_max:
        raise fail(
            f"weekly reservation amount {reserved_amount} != verified plan maximum_bounded_cost_usd {plan_max}"
        )


def _verify_pre_plan_reservation_binding(conn, *, env, capability, now, fail):
    """MANDATORY for a paid PRE_PLAN capability invocation reached through
    the (grant-derived) gate. The invocation must be bound to the ONE
    pre-production CHANNEL_WEEKLY hold for (channel, episode) whose window
    contains 'now': it must exist, be for this channel, this currency, and
    be in a state the weekly-budget design authorises ('reserved'). A
    released, foreign-channel, wrong-currency, or missing hold fails
    closed. This function only READS the ledger.
    """
    import hypogeum.weekly_budget as weekly_budget
    from hypogeum.preplan_reservation import preplan_idempotency_key

    if not (env.channel_id and str(env.channel_id).strip()):
        raise fail("PRE_PLAN paid invocation with no channel_id")

    try:
        tz_name, _ceiling = weekly_budget.resolve_channel_weekly_policy(conn, env.channel_id)
    except weekly_budget.WeeklyBudgetError as exc:
        raise fail(f"cannot resolve the channel's weekly policy: {exc}") from exc

    idem = preplan_idempotency_key(env.channel_id, env.execution_id)
    rows = conn.execute(
        "SELECT * FROM budget_reservations "
        "WHERE scope_kind = 'channel_weekly' AND idempotency_key = ?",
        (idem,),
    ).fetchall()
    if not rows:
        raise fail(
            f"no pre-production CHANNEL_WEEKLY hold (idempotency_key={idem!r}) -- paid PRE_PLAN "
            f"work is not bound to the weekly ledger"
        )
    now_iso = now.isoformat()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(budget_reservations)")}
    live = None
    for r in rows:
        start = r["weekly_window_start_utc"] or ""
        end = r["weekly_window_end_utc"] or ""
        if start <= now_iso < end:
            live = r
            break
    if live is None:
        raise fail(
            "the pre-production hold's window does not contain now -- a stale/rolled-over hold "
            "cannot authorise a current paid PRE_PLAN call"
        )
    if live["status"] != "reserved":
        raise fail(
            f"pre-production hold status is {live['status']!r} -- only 'reserved' authorises "
            f"paid PRE_PLAN work (released holds are terminal)"
        )
    if "channel_id" in cols and live["channel_id"] not in (None, env.channel_id):
        raise fail(f"pre-production hold channel_id {live['channel_id']!r} != envelope {env.channel_id!r}")
    if str(live["currency"] or "") not in ("", capability.currency, env.currency):
        raise fail(f"pre-production hold currency {live['currency']!r} != {capability.currency!r}")
