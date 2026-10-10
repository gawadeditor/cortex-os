"""
hypogeum/submission_gate.py — Mandatory shared submission gate
(Factory Core)

Implements Commit 3 of docs/architecture/AUTHORIZED_GENERATION_ENVELOPE_
IMPLEMENTATION_BLUEPRINT.md: docs/architecture/AUTHORIZED_GENERATION_
ENVELOPE.md §Phase 5's verify_and_consume_envelope() concept, generalized
to the exact boundary sequence this commit's task defines.

`submit()` is the ONLY function in Factory Core authorized to invoke a
provider adapter (Part 5 requirement 1). Every other module that wants to
spend constructs a GenerationExecutionEnvelope and calls this function --
never a provider adapter directly, never with a raw prompt, and never by
selecting or supplying an adapter instance. This module mints a short-
lived, integrity-checksummed, single-use AdapterInvocationCapability only
after every check below has passed, and that capability is the only
thing an adapter's invoke() will accept (see
hypogeum.providers.fake_provider_adapter).

submit()'s public signature carries no adapter parameter at all. The
exact adapter class is resolved internally, immediately before minting a
capability, via hypogeum.adapter_registry.resolve_adapter_via_conn() --
keyed by the envelope's own (provider, model, provider_operation) -- and
instantiated only inside this function. No caller ever imports,
constructs, selects, or invokes a real adapter class, or receives a
capability. Only hypogeum.providers.fake_provider_adapter.
FakeProviderAdapter is registered as of this commit -- a structurally
network-incapable test double -- so no real provider is reachable yet;
later commits register real per-domain adapters through the same
registry with no change to this module's resolution logic.

Default-deny: every check function below returns None on success and
returns a GateDenial on failure. submit() runs them in the exact order
documented, stopping at the first denial, and the fake adapter is invoked
only after all pre-boundary checks (through step 9) have passed.

If `authorization`'s AUTHORIZED-transition evidence carries
parent_grant_id/parent_grant_version/parent_grant_checksum/
grant_reservation_id (i.e. it was issued by hypogeum.authorization_
authority.issue_child_authorization() from a bounded run grant, rather
than a one-off explicit approval), submit() calls exactly one function
from hypogeum.authorization_store -- revalidate_grant_reservation() --
immediately before the provider boundary (mirroring the existing
Step-8 lease revalidation), and later settles that same reservation's
state according to the adapter's outcome. This is the ONLY grant-
related code in this module: revalidate_grant_reservation() and the
settlement calls are read-and-settle-only on an *already-issued*
reservation -- they can never create a grant, a reservation, or an
AUTHORIZED AuthorizationRecord, so this module still never mints or
broadens authorization. This module never imports
hypogeum.authorization_authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

import hypogeum.adapter_registry as adapter_registry
import hypogeum.authorization_store as authorization_store
import hypogeum.budget_scopes as bs
import hypogeum.db as db
from hypogeum.authorization_lifecycle import AuthorizationRecord, AuthorizationState
from hypogeum.execution_envelope import GenerationExecutionEnvelope
from hypogeum.providers.fake_provider_adapter import (
    AdapterInvocationCapability,
    AdapterResult,
    compute_capability_checksum,
)
from hypogeum.provider_receipt import (
    ELIGIBLE_FOR_SUBMISSION_STATES,
    EventType,
    ProviderReceiptEvent,
    RecoveryState,
    WriteAheadReceiptStore,
)

__all__ = [
    "LiveSources",
    "RetryContext",
    "FallbackContext",
    "BreakerLimits",
    "GateDenial",
    "GateResult",
    "GATE_CHECK_ORDER",
    "submit",
    "CheckOutcome",
    "run_shadow_checks",
]

# Recovery states a retry attempt may safely resume from -- only a
# *confirmed* pre-acceptance failure (never uncertainty, never anything
# post-acceptance) is provably safe to retry.
_RETRY_SAFE_RECOVERY_STATES = frozenset({RecoveryState.TERMINALLY_FAILED_BEFORE_ACCEPTANCE})

GATE_CHECK_ORDER = (
    "01_envelope_schema_version_supported",
    "02_envelope_integrity",
    "03_authorization_active_and_unconsumed",
    "04_authorization_version_match",
    "05_idempotency_key_unconsumed",
    "06_channel_package_checksum_match",
    "07_plan_checksum_match",
    "08_structured_spec_checksum_match",
    "09_compiler_identity_version_match",
    "10_prompt_checksum_match",
    "11_prompt_provenance_valid",
    "12_per_shot_direction_authoritative",
    "13_no_scene_level_dominance_leak",
    "14_model_provider_operation_match",
    "15_execution_binding_match",
    "16_output_collision",
    "17_budget_scope_configuration",
    "18_retry_fallback_authorization",
    "19_lease_and_fencing",
    "20_audit_persistence",
    "21_workflow_binding_not_reconstructed",
    "22_stored_envelope_durable_integrity",
)


@dataclass(frozen=True)
class LiveSources:
    """The current, live values every dependency checksum on the envelope
    is compared against. Constructed by the caller from whatever
    channel-specific source is canonical (e.g. Hypogeum's Channel
    Package/plan files) -- this module never reads a file or a database
    to derive these itself, keeping it channel-agnostic (architecture
    §3.2)."""

    channel_package_checksum: str
    plan_checksum: str
    structured_spec_checksum: str
    compiler_version: str


@dataclass(frozen=True)
class RetryContext:
    prior_attempt_id: str
    retry_budget_ref: str
    new_attempt_id: str


@dataclass(frozen=True)
class FallbackContext:
    fallback_provider: str
    fallback_model: str
    fallback_budget_ref: str


@dataclass(frozen=True)
class BreakerLimits:
    """Configured limits this gate call must enforce. All optional --
    an unset limit means that breaker kind is not evaluated for this
    call (Part 3's "configured limits", plural and caller-defined, not a
    hardcoded Factory Core policy)."""

    scope_kind: str
    scope_ref: str
    max_provider_calls: Optional[int] = None
    max_spend: Optional[Decimal] = None
    max_consecutive_failures: Optional[int] = None
    max_unknown_acceptance: Optional[int] = None
    max_duplicate_conflicts: Optional[int] = None


@dataclass(frozen=True)
class GateDenial:
    check: str
    reason: str


@dataclass(frozen=True)
class GateResult:
    approved: bool
    denied_at_check: Optional[str]
    reason: Optional[str]
    adapter_invoked: bool
    adapter_result: Optional[AdapterResult]
    recovery_state: Optional[RecoveryState]
    budget_reservation_ids: Sequence[str] = field(default_factory=tuple)
    breaker_evidence: Sequence[Any] = field(default_factory=tuple)


def _denial(check: str, reason: str) -> GateResult:
    return GateResult(
        approved=False, denied_at_check=check, reason=reason,
        adapter_invoked=False, adapter_result=None, recovery_state=None,
    )


# ---------------------------------------------------------------------------
# Step 1: validate envelope and sources
# ---------------------------------------------------------------------------

def _validate_envelope_and_sources(
    envelope: GenerationExecutionEnvelope,
    live: LiveSources,
    required_model: Optional[str],
    required_provider: Optional[str],
    dominance_leak_validator,
) -> Optional[GateDenial]:
    from hypogeum.execution_envelope import (
        EnvelopeIntegrityError, EnvelopeSchemaError, SUPPORTED_SCHEMA_VERSIONS,
    )

    if envelope.envelope_schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        return GateDenial(GATE_CHECK_ORDER[0], f"unsupported envelope_schema_version {envelope.envelope_schema_version!r}")

    try:
        envelope.validate_integrity()
    except (EnvelopeIntegrityError, EnvelopeSchemaError) as exc:
        return GateDenial(GATE_CHECK_ORDER[1], f"envelope integrity check failed: {exc}")

    if envelope.dependency_checksums.channel_package != live.channel_package_checksum:
        return GateDenial(
            GATE_CHECK_ORDER[5],
            f"channel_package checksum stale: authorized {envelope.dependency_checksums.channel_package!r} "
            f"!= live {live.channel_package_checksum!r}",
        )
    if envelope.dependency_checksums.plan != live.plan_checksum:
        return GateDenial(
            GATE_CHECK_ORDER[6],
            f"plan checksum stale: authorized {envelope.dependency_checksums.plan!r} != live {live.plan_checksum!r}",
        )
    if envelope.dependency_checksums.structured_spec != live.structured_spec_checksum:
        return GateDenial(
            GATE_CHECK_ORDER[7],
            f"structured_spec checksum stale: authorized {envelope.dependency_checksums.structured_spec!r} "
            f"!= live {live.structured_spec_checksum!r}",
        )
    if envelope.compiler_version != live.compiler_version:
        return GateDenial(
            GATE_CHECK_ORDER[8],
            f"compiler_version stale: authorized {envelope.compiler_version!r} != live {live.compiler_version!r}",
        )

    import hashlib
    recomputed_payload_checksum = hashlib.sha256(envelope.operation_payload_content.encode("utf-8")).hexdigest()
    if recomputed_payload_checksum != envelope.operation_payload_sha256:
        return GateDenial(GATE_CHECK_ORDER[9], "operation_payload_sha256 does not match the exact operation_payload_content bytes")

    cp = envelope.content_provenance
    if not cp.compiler or not cp.source_work_item_id:
        return GateDenial(GATE_CHECK_ORDER[10], "prompt provenance is incomplete")

    if dominance_leak_validator is not None:
        leak_reason = dominance_leak_validator(envelope)
        if leak_reason:
            return GateDenial(GATE_CHECK_ORDER[12], f"scene-level prompt dominance leak: {leak_reason}")
    # check 12 (per-shot direction authoritative) is the positive half of
    # the same channel-specific validator hook -- Factory Core has no
    # generic way to know what "authoritative per-shot direction" looks
    # like for an arbitrary domain, so both checks 12 and 13 are the same
    # optional hook; a caller that cares (e.g. Hypogeum's video domain)
    # supplies dominance_leak_validator, one that doesn't (a domain with
    # no scene/shot split) passes None and both checks are a no-op.

    if required_provider is not None and envelope.provider != required_provider:
        return GateDenial(GATE_CHECK_ORDER[13], f"provider {envelope.provider!r} != required {required_provider!r}")
    if required_model is not None and envelope.model != required_model:
        return GateDenial(GATE_CHECK_ORDER[13], f"model {envelope.model!r} != required {required_model!r}")

    return None


# ---------------------------------------------------------------------------
# Step 1.6: durable envelope integrity (docs/architecture/PROTECTED_
# GENERATION_LIFECYCLE_RECONCILIATION.md Part 1/5)
# ---------------------------------------------------------------------------

def _check_stored_envelope_durable_integrity(lease_conn, envelope: GenerationExecutionEnvelope) -> Optional[GateDenial]:
    """
    Real submission requires a full, durably-stored, integrity-verified
    envelope -- not merely a caller-supplied in-memory object whose
    content no one can independently audit later. Reads back
    hypogeum.authorization_store.get_execution_envelope() for this exact
    (envelope_id, envelope_version); any failure there (missing --
    LegacyHashOnlyEnvelopeError; corrupted; identity-mismatched;
    unsupported schema version) denies here, before any lease/budget
    state is touched and long before the provider boundary.

    A legacy hash-only proposal (compiled before this storage mechanism
    existed, or never persisted at all) has no row here and is
    therefore permanently non-executable through this path -- exactly
    Part 1 requirement 7's "must never pass the submission gate."

    Read-only: this function only reads authorization_store; it never
    imports hypogeum.authorization_authority and never calls any
    minting/persisting function (get_execution_envelope() itself never
    writes). Deliberately called ONLY from submit(), never shared with
    run_shadow_checks() -- Observation-Only evaluates freshly-compiled
    envelopes that are not yet, and may never be, durably persisted;
    requiring durable storage there would break that flow's own purpose.
    """
    try:
        stored = authorization_store.get_execution_envelope(lease_conn, envelope.envelope_id, envelope.envelope_version)
    except authorization_store.EnvelopeStoreError as exc:
        return GateDenial(GATE_CHECK_ORDER[21], f"{type(exc).__name__}: {exc}")

    if stored.envelope_id != envelope.envelope_id or stored.operation_payload_sha256 != envelope.operation_payload_sha256:
        return GateDenial(
            GATE_CHECK_ORDER[21],
            "supplied envelope does not match the durably-stored envelope for this identity",
        )
    return None


# ---------------------------------------------------------------------------
# Step 1.5: reconstructed-binding block (plan §5 -- legacy migration policy)
# ---------------------------------------------------------------------------

def _check_workflow_binding_not_reconstructed(envelope: GenerationExecutionEnvelope) -> Optional[GateDenial]:
    """
    A workflow/channel-package binding reconstructed against the
    CURRENT registry for a PRE-registry authorization (e.g. a legacy
    manifest migrated via tools/migrate_legacy_manifest_to_envelopes.py)
    may only be used for diagnostics and Observation-Only -- it must
    never be accepted for real submission. tools/migrate_legacy_
    manifest_to_envelopes.py tags every envelope it constructs
    audit_metadata["workflow_binding_reconstructed"] = True; this check
    is the sole enforcement point for that tag.

    Deliberately called ONLY from submit() itself, never from
    _validate_envelope_and_sources() or any other function
    run_shadow_checks() shares with submit() -- Observation-Only must
    remain able to evaluate a reconstructed envelope's every other
    dimension (that is the whole point of the migration tool's dry-run
    report), so this one check is structurally absent from shadow
    evaluation's own check list rather than being conditionally
    skipped inside a shared function.
    """
    if envelope.audit_metadata.get("workflow_binding_reconstructed") is True:
        return GateDenial(
            GATE_CHECK_ORDER[20],
            "envelope.audit_metadata['workflow_binding_reconstructed'] is True -- "
            "a reconstructed legacy binding may only be used for diagnostics/Observation-Only, "
            "never for real submission",
        )
    return None


# ---------------------------------------------------------------------------
# Step 2: validate authorization
# ---------------------------------------------------------------------------

def _validate_authorization(
    envelope: GenerationExecutionEnvelope, authorization: AuthorizationRecord, at: str,
) -> Optional[GateDenial]:
    if authorization.envelope_id != envelope.envelope_id:
        return GateDenial(GATE_CHECK_ORDER[2], "authorization does not reference this envelope_id")
    if authorization.authorization_id != envelope.authorization_id:
        return GateDenial(GATE_CHECK_ORDER[2], "authorization_id does not match envelope.authorization_id")
    if authorization.current_state != AuthorizationState.AUTHORIZED:
        return GateDenial(
            GATE_CHECK_ORDER[2],
            f"authorization state is {authorization.current_state.value!r}, not 'authorized' "
            f"(terminal={authorization.is_terminal()})",
        )
    if authorization.authorization_version != envelope.authorization_version:
        return GateDenial(
            GATE_CHECK_ORDER[3],
            f"authorization_version {authorization.authorization_version} != "
            f"envelope.authorization_version {envelope.authorization_version}",
        )
    if authorization.envelope_version != envelope.envelope_version:
        return GateDenial(GATE_CHECK_ORDER[3], "authorization.envelope_version does not match envelope.envelope_version")

    expires_at = envelope.expiration_policy.expires_at
    if expires_at is not None and at >= expires_at:
        return GateDenial(GATE_CHECK_ORDER[2], f"authorization expired at {expires_at!r} (evaluated at {at!r})")

    return None


# ---------------------------------------------------------------------------
# Step 3 / Step 8: lease and fencing
# ---------------------------------------------------------------------------

def _validate_lease(conn, envelope: GenerationExecutionEnvelope, owner_id: str, fencing_token: int, at: str) -> Optional[GateDenial]:
    if not db.validate_lease(conn, envelope.envelope_id, owner_id, fencing_token, at=at):
        return GateDenial(GATE_CHECK_ORDER[18], f"no active lease held by owner={owner_id!r} fencing_token={fencing_token!r}")
    return None


# ---------------------------------------------------------------------------
# Step 4: recovery / idempotency
# ---------------------------------------------------------------------------

def _validate_recovery_state(
    receipt_store: WriteAheadReceiptStore, execution_id: str, envelope: GenerationExecutionEnvelope,
    retry_context: Optional[RetryContext],
) -> "tuple[Optional[GateDenial], RecoveryState]":
    state = receipt_store.classify_recovery_state(execution_id, envelope.idempotency_key)

    if retry_context is not None:
        if state not in _RETRY_SAFE_RECOVERY_STATES:
            return GateDenial(
                GATE_CHECK_ORDER[4],
                f"recovery state {state.value!r} is not proven safe for retry "
                f"(only {[s.value for s in _RETRY_SAFE_RECOVERY_STATES]} qualify)",
            ), state
        return None, state

    if state not in ELIGIBLE_FOR_SUBMISSION_STATES:
        return GateDenial(
            GATE_CHECK_ORDER[4],
            f"idempotency_key {envelope.idempotency_key!r} already has attempt evidence "
            f"(recovery state {state.value!r}) -- not eligible for a fresh submission",
        ), state
    return None, state


# ---------------------------------------------------------------------------
# Step 5 (config half) + Step 18: retry/fallback authorization flags
# ---------------------------------------------------------------------------

def _validate_retry_fallback_authorization(
    envelope: GenerationExecutionEnvelope,
    retry_context: Optional[RetryContext],
    fallback_context: Optional[FallbackContext],
) -> Optional[GateDenial]:
    if retry_context is not None:
        if not envelope.retry_authorized:
            return GateDenial(GATE_CHECK_ORDER[17], "retry attempted but envelope.retry_authorized is False")
        if envelope.retry_budget_ref != retry_context.retry_budget_ref:
            return GateDenial(GATE_CHECK_ORDER[17], "retry_context.retry_budget_ref does not match envelope.retry_budget_ref")
    if fallback_context is not None:
        if not envelope.fallback_authorized:
            return GateDenial(GATE_CHECK_ORDER[17], "fallback attempted but envelope.fallback_authorized is False")
        if (
            envelope.fallback_provider != fallback_context.fallback_provider
            or envelope.fallback_model != fallback_context.fallback_model
            or envelope.fallback_budget_ref != fallback_context.fallback_budget_ref
        ):
            return GateDenial(GATE_CHECK_ORDER[17], "fallback_context does not exactly match envelope's authorized fallback binding")
    return None


# ---------------------------------------------------------------------------
# Step 15/16: execution binding + output collision
# ---------------------------------------------------------------------------

def _validate_execution_binding(
    envelope: GenerationExecutionEnvelope,
    expected_duration_seconds: Optional[Decimal],
    expected_aspect_ratio: Optional[str],
    expected_seed: Optional[int],
    expected_output_identity: Optional[str],
    expected_deterministic_output_path: Optional[str],
    output_already_exists: bool,
) -> Optional[GateDenial]:
    if expected_duration_seconds is not None and envelope.duration_seconds != expected_duration_seconds:
        return GateDenial(GATE_CHECK_ORDER[14], "duration_seconds does not match authorization-time value")
    if expected_aspect_ratio is not None and envelope.aspect_ratio != expected_aspect_ratio:
        return GateDenial(GATE_CHECK_ORDER[14], "aspect_ratio does not match authorization-time value")
    if expected_seed is not None and envelope.seed != expected_seed:
        return GateDenial(GATE_CHECK_ORDER[14], "seed does not match authorization-time value")
    if expected_output_identity is not None and envelope.expected_output_identity != expected_output_identity:
        return GateDenial(GATE_CHECK_ORDER[14], "expected_output_identity does not match authorization-time value")
    if expected_deterministic_output_path is not None and envelope.deterministic_output_path != expected_deterministic_output_path:
        return GateDenial(GATE_CHECK_ORDER[14], "deterministic_output_path does not match authorization-time value")
    if output_already_exists:
        return GateDenial(GATE_CHECK_ORDER[15], f"output already exists at {envelope.deterministic_output_path!r}")
    return None


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------

def _breaker_id(kind: str, scope_kind: str, scope_ref: str) -> str:
    return f"{kind}:{scope_kind}:{scope_ref}"


_BREAKER_KINDS = ("provider_calls", "max_spend", "consecutive_failures", "unknown_acceptance", "duplicate_conflicts", "overcharge")


def _check_breakers_not_tripped(conn, limits: Optional[BreakerLimits]) -> Optional[GateDenial]:
    if limits is None:
        return None
    for kind in _BREAKER_KINDS:
        breaker_id = _breaker_id(kind, limits.scope_kind, limits.scope_ref)
        if bs.is_tripped(conn, breaker_id):
            return GateDenial(GATE_CHECK_ORDER[16], f"circuit breaker {breaker_id!r} is tripped")
    return None


def _record_breaker_observation(conn, kind: str, limits: BreakerLimits, observed, limit_value, *, tripped: bool, attempt_id: str, at: str, evidence: Mapping):
    breaker_id = _breaker_id(kind, limits.scope_kind, limits.scope_ref)
    bs.record_breaker(
        conn, breaker_id, breaker_type=kind, scope_kind=limits.scope_kind, scope_ref=limits.scope_ref,
        observed_value=observed, configured_limit=limit_value, tripped=tripped,
        triggering_attempt_id=attempt_id if tripped else None, evidence=evidence, at=at,
    )


def _increment_counter_breaker(conn, kind: str, limit_attr: str, limits: Optional[BreakerLimits], *, at: str, attempt_id: str, evidence_extra: Optional[dict] = None) -> None:
    """Reads the current integer observed_value for (kind, limits),
    increments by 1, writes it back, and trips the breaker if the new
    value meets/exceeds the configured limit (a None limit means this
    breaker kind is simply not enforced -- observed_value is still
    tracked for visibility, but tripped stays False)."""
    if limits is None:
        return
    breaker_id = _breaker_id(kind, limits.scope_kind, limits.scope_ref)
    existing = bs.get_breaker(conn, breaker_id)
    current = int(existing.observed_value) if existing and existing.observed_value not in (None, "None") else 0
    new_value = current + 1
    limit = getattr(limits, limit_attr, None)
    tripped = limit is not None and new_value >= limit
    _record_breaker_observation(
        conn, kind, limits, new_value, limit if limit is not None else "unset",
        tripped=tripped, attempt_id=attempt_id, at=at,
        evidence={**(evidence_extra or {}), "observed": new_value, "limit": limit},
    )


def _reset_counter_breaker_observed_value(conn, kind: str, limits: Optional[BreakerLimits], *, at: str) -> None:
    """Sets observed_value back to 0 on a success -- e.g. consecutive_
    failures resets on an accepted outcome. This is not a breaker
    "reset": tripped remains monotonic (see hypogeum.db.record_breaker_
    evidence's own docstring) -- only the raw counter moves."""
    if limits is None:
        return
    breaker_id = _breaker_id(kind, limits.scope_kind, limits.scope_ref)
    limit = getattr(limits, "max_consecutive_failures", None)
    _record_breaker_observation(
        conn, kind, limits, 0, limit if limit is not None else "unset",
        tripped=False, attempt_id=None, at=at, evidence={"observed": 0, "reason": "reset on accepted outcome"},
    )


# ---------------------------------------------------------------------------
# Grant-reservation evidence lookup + settlement (Correction 3/4) --
# every function here is read-and-settle-only on an *already-issued*
# reservation; none of them can create a grant, a reservation, or an
# AUTHORIZED AuthorizationRecord.
# ---------------------------------------------------------------------------

def _grant_binding_evidence(authorization: AuthorizationRecord) -> Optional[Mapping[str, Any]]:
    """Returns the parent_grant_id/parent_grant_version/
    parent_grant_checksum/grant_reservation_id evidence from this
    authorization's AUTHORIZED transition, or None if it was issued via
    the one-off explicit-approval path (issue_authorization_from_
    explicit_approval() -- Commit M's future cutover), which carries no
    grant evidence at all.

    Historical note (ep_f3619022, 2026-08-17): a RunAuthorizationGrant
    (grant_ep_f3619022_v4_replacement) was registered alongside a v4
    replacement-proposal batch, but the batch's 85 AuthorizationRecords
    were AUTHORIZED via hypogeum.authorization_authority.
    activate_authorization_proposal(), which at the time never bound a
    grant at all -- this function returned None for every one of those
    85 records, and the registered grant's own ledger ended up with
    ZERO rows for it. Documented and proven structurally in
    tests/test_grant_not_consumed_by_proposal_activation.py, then
    CLOSED (2026-08-17, same day): activate_authorization_proposal()
    now REQUIRES grant_id/grant_version, resolves and verifies the
    grant, validates every envelope's binding, reserves ledger capacity
    for every envelope (atomically, within one transaction, so the
    batch itself cannot oversubscribe), and stamps the exact same
    parent_grant_id/parent_grant_version/parent_grant_checksum/
    grant_reservation_id evidence issue_child_authorization() already
    stamps -- so this function (and this module's own unchanged Step
    8.5 boundary revalidation) now applies identically to records
    produced by EITHER authorization-issuance path. See
    tests/test_grant_binding_hardening.py for the closure's own
    coverage. The 65 real calls the ep_f3619022 v4 replacement batch
    already made were not, and are not, retroactively repaired -- this
    closes the gap for every future proposal activation only."""
    if not authorization.history:
        return None
    last_event = authorization.history[-1]
    if last_event.to_state != AuthorizationState.AUTHORIZED:
        return None
    evidence = last_event.evidence
    if "parent_grant_id" not in evidence:
        return None
    return evidence


def _settle_grant_reservation(
    lease_conn, grant_binding_evidence: Optional[Mapping[str, Any]], *, to_state: str, at: str,
    charged_amount: Optional[Decimal] = None,
) -> None:
    """No-op if this authorization carries no grant evidence. Otherwise
    transitions the already-issued reservation boundary_entered -> the
    given terminal-for-this-attempt state, mirroring the same three-way
    outcome branch this function's callers already use for receipts and
    budget_scopes -- never new control flow, just one more parallel
    update at the same three existing points."""
    if grant_binding_evidence is None:
        return
    authorization_store.transition_ledger_state(
        lease_conn, grant_binding_evidence["grant_reservation_id"],
        from_state="boundary_entered", to_state=to_state, at=at, charged_amount=charged_amount,
    )


# ---------------------------------------------------------------------------
# Adapter resolution -- the only place a real adapter class is ever
# located and loaded. No caller supplies one.
# ---------------------------------------------------------------------------

def _resolve_adapter_binding(lease_conn, envelope: GenerationExecutionEnvelope) -> "tuple[Optional[GateDenial], Optional[type]]":
    """
    Resolves the exact adapter class bound to this envelope's
    (provider, model, provider_operation) via the generic Adapter
    Registry (hypogeum.adapter_registry) -- the only way submit() ever
    discovers which class to instantiate. Reuses lease_conn (the same
    connection every other check in submit() already uses) rather than
    opening a separate connection, so production resolves against the
    real database and tests resolve against whatever isolated database
    their lease_conn already points to, with no separate adapter-
    registry parameter on submit()'s own public signature.

    Fails closed if no active registration exists, or if the resolved
    binding's recorded adapter_implementation_checksum no longer
    matches the live adapter class's current module-source checksum
    (implementation drift -- the adapter changed without a new
    registration).
    """
    try:
        binding = adapter_registry.resolve_adapter_via_conn(
            lease_conn, envelope.provider, envelope.model, envelope.provider_operation,
        )
    except adapter_registry.UnregisteredAdapterError as exc:
        return GateDenial(GATE_CHECK_ORDER[13], f"no adapter registered: {exc}"), None

    try:
        adapter_class = adapter_registry.load_adapter_class(binding.adapter_qualified_name)
    except Exception as exc:  # noqa: BLE001 -- an unimportable registered class is itself a fail-closed condition
        return GateDenial(
            GATE_CHECK_ORDER[13],
            f"registered adapter class {binding.adapter_qualified_name!r} could not be loaded: {exc}",
        ), None

    live_checksum = adapter_registry.compute_adapter_implementation_checksum(adapter_class)
    if live_checksum != binding.adapter_implementation_checksum:
        return GateDenial(
            GATE_CHECK_ORDER[13],
            f"adapter implementation drift: registered checksum {binding.adapter_implementation_checksum!r} "
            f"!= live checksum {live_checksum!r} for {binding.adapter_qualified_name!r} -- "
            "the adapter's implementation changed without a new registration",
        ), None

    return None, adapter_class


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def submit(
    envelope: GenerationExecutionEnvelope,
    *,
    live_sources: LiveSources,
    authorization: AuthorizationRecord,
    lease_conn,
    lease_owner_id: str,
    lease_fencing_token: int,
    receipt_store: WriteAheadReceiptStore,
    execution_id: str,
    attempt_id: str,
    budget_scopes: Sequence[bs.ScopeRequest],
    adapter_mode: str = "success",
    at: str,
    dominance_leak_validator=None,
    required_provider: Optional[str] = None,
    required_model: Optional[str] = None,
    expected_duration_seconds: Optional[Decimal] = None,
    expected_aspect_ratio: Optional[str] = None,
    expected_seed: Optional[int] = None,
    expected_output_identity: Optional[str] = None,
    expected_deterministic_output_path: Optional[str] = None,
    output_already_exists: bool = False,
    retry_context: Optional[RetryContext] = None,
    fallback_context: Optional[FallbackContext] = None,
    breaker_limits: Optional[BreakerLimits] = None,
    capability_ttl_seconds_note: str = "caller-supplied expires_at",
    capability_expires_at: Optional[str] = None,
) -> GateResult:
    """
    The one function in Factory Core authorized to invoke a provider
    adapter. Runs every check in GATE_CHECK_ORDER; on the first denial,
    returns a GateResult(approved=False, ...) with adapter_invoked=False
    and performs no further action -- in particular, nothing about a
    real adapter is touched at all until step 9 completes durably. No
    caller supplies an adapter: the exact class is resolved internally
    via hypogeum.adapter_registry, keyed by the envelope's own
    (provider, model, provider_operation) -- see
    _resolve_adapter_binding().

    `adapter_mode` is a test-control knob forwarded to whichever
    resolved adapter's invoke() accepts it (see
    FakeProviderAdapter.invoke() for the vocabulary: "success",
    "unknown_acceptance", "rejected", "overcharge",
    "duplicate_acknowledgement"). Only the fake adapter is registered as
    of this commit; later commits' real per-domain adapters are expected
    to ignore or ultimately retire this parameter.
    """
    # ---- Step 1: validate envelope and sources ----
    denial = _validate_envelope_and_sources(envelope, live_sources, required_model, required_provider, dominance_leak_validator)
    if denial:
        return _denial(denial.check, denial.reason)

    # ---- Step 1.5: reject a reconstructed legacy binding (plan §5) ----
    denial = _check_workflow_binding_not_reconstructed(envelope)
    if denial:
        return _denial(denial.check, denial.reason)

    # ---- Step 1.6: durable envelope integrity (Part 1/5) ----
    denial = _check_stored_envelope_durable_integrity(lease_conn, envelope)
    if denial:
        return _denial(denial.check, denial.reason)

    denial = _validate_execution_binding(
        envelope, expected_duration_seconds, expected_aspect_ratio, expected_seed,
        expected_output_identity, expected_deterministic_output_path, output_already_exists,
    )
    if denial:
        return _denial(denial.check, denial.reason)

    # ---- Resolve the adapter internally -- no caller-supplied adapter ----
    denial, adapter_class = _resolve_adapter_binding(lease_conn, envelope)
    if denial:
        return _denial(denial.check, denial.reason)

    # ---- Step 2: validate authorization ----
    denial = _validate_authorization(envelope, authorization, at)
    if denial:
        return _denial(denial.check, denial.reason)

    # ---- Step 3: validate lease (first pass) ----
    denial = _validate_lease(lease_conn, envelope, lease_owner_id, lease_fencing_token, at)
    if denial:
        return _denial(denial.check, denial.reason)

    # ---- Step 4: validate recovery / idempotency ----
    denial, recovery_state = _validate_recovery_state(receipt_store, execution_id, envelope, retry_context)
    if denial:
        return _denial(denial.check, denial.reason)

    # ---- Step 5: validate budgets (config) and breakers ----
    denial = _validate_retry_fallback_authorization(envelope, retry_context, fallback_context)
    if denial:
        return _denial(denial.check, denial.reason)
    if not budget_scopes:
        return _denial(GATE_CHECK_ORDER[16], "no budget scopes supplied -- at least the call scope is required")
    denial = _check_breakers_not_tripped(lease_conn, breaker_limits)
    if denial:
        return _denial(denial.check, denial.reason)

    # ---- Step 6: reserve applicable budget ----
    try:
        multi_reservation = bs.reserve_all_scopes(
            lease_conn, budget_scopes, idempotency_key=envelope.idempotency_key,
            amount=envelope.max_cost_per_call, currency=envelope.currency, at=at,
            reservation_id_prefix=envelope.idempotency_key,
        )
    except bs.BudgetError as exc:
        return _denial(GATE_CHECK_ORDER[16], f"budget reservation denied: {exc}")

    reservation_ids = multi_reservation.reservation_ids()

    def _release_reservations():
        bs.release_all(lease_conn, multi_reservation, at=at)

    # ---- Step 7: durably persist write-ahead attempt (check 20) ----
    about_to_submit = ProviderReceiptEvent(
        envelope_id=envelope.envelope_id, envelope_version=envelope.envelope_version,
        authorization_id=envelope.authorization_id, authorization_version=envelope.authorization_version,
        attempt_id=attempt_id, idempotency_key=envelope.idempotency_key,
        event_type=EventType.ABOUT_TO_SUBMIT, event_seq=0, at=at,
        provider=envelope.provider, model=envelope.model, provider_operation=envelope.provider_operation,
        provider_bound_content_sha256=envelope.operation_payload_sha256,
        max_cost_per_call=envelope.max_cost_per_call, currency=envelope.currency,
        evidence={"reservation_ids": list(reservation_ids)},
    )
    from hypogeum.provider_receipt import DuplicateEventError  # noqa: PLC0415
    try:
        receipt_store.prepare_attempt(about_to_submit, execution_id=execution_id)
    except DuplicateEventError as exc:
        _release_reservations()
        _increment_counter_breaker(lease_conn, "duplicate_conflicts", "max_duplicate_conflicts", breaker_limits, at=at, attempt_id=attempt_id, evidence_extra={"reason": str(exc)})
        return _denial(GATE_CHECK_ORDER[4], f"conflicting attempt identity for idempotency_key {envelope.idempotency_key!r}: {exc}")
    except Exception as exc:  # noqa: BLE001 -- any other persistence failure must deny, never proceed
        _release_reservations()
        return _denial(GATE_CHECK_ORDER[19], f"write-ahead persistence failed: {exc}")

    # ---- Step 8: revalidate lease/fencing immediately before the boundary ----
    denial = _validate_lease(lease_conn, envelope, lease_owner_id, lease_fencing_token, at)
    if denial:
        _release_reservations()
        return _denial(denial.check, f"lease invalid at pre-boundary revalidation: {denial.reason}")

    # ---- Step 8.5: revalidate the parent grant reservation, if this authorization was grant-derived ----
    # "Issuance read active before revocation" is not sufficient by itself
    # (architecture Correction 3) -- this is the actual enforcement point.
    # A one-off explicit-approval authorization (Commit M's future cutover
    # path) carries no grant evidence and skips this entirely.
    grant_binding_evidence = _grant_binding_evidence(authorization)
    if grant_binding_evidence is not None:
        try:
            authorization_store.revalidate_grant_reservation(
                lease_conn,
                parent_grant_id=grant_binding_evidence["parent_grant_id"],
                parent_grant_version=grant_binding_evidence["parent_grant_version"],
                parent_grant_checksum=grant_binding_evidence["parent_grant_checksum"],
                grant_reservation_id=grant_binding_evidence["grant_reservation_id"],
                at=at,
            )
        except authorization_store.GrantRevalidationError as exc:
            _release_reservations()
            return _denial(GATE_CHECK_ORDER[2], f"parent grant revalidation failed at pre-boundary: {exc}")

    # ---- Step 9: persist boundary-entry event immediately before invocation ----
    boundary_entry = ProviderReceiptEvent(
        envelope_id=envelope.envelope_id, envelope_version=envelope.envelope_version,
        authorization_id=envelope.authorization_id, authorization_version=envelope.authorization_version,
        attempt_id=attempt_id, idempotency_key=envelope.idempotency_key,
        event_type=EventType.SUBMISSION_ATTEMPT_STARTED, event_seq=1, at=at,
        provider=envelope.provider, model=envelope.model, provider_operation=envelope.provider_operation,
        provider_bound_content_sha256=envelope.operation_payload_sha256,
        max_cost_per_call=envelope.max_cost_per_call, currency=envelope.currency,
        evidence={"lease_owner_id": lease_owner_id, "lease_fencing_token": lease_fencing_token},
    )
    receipt_store.append_event(
        boundary_entry, execution_id=execution_id,
        lease_conn=lease_conn, lease_owner_id=lease_owner_id, lease_fencing_token=lease_fencing_token,
    )
    # Make the boundary-entry state durable AND release the SQLite write lock
    # before the (slow) provider call. revalidate_grant_reservation() above
    # leaves an uncommitted UPDATE (reserved -> boundary_entered) on
    # lease_conn; without this commit the connection holds the database-wide
    # write lock for the whole provider call (video generation + polling,
    # upscaling: minutes at a time, back to back for a whole run), so every
    # other writer (portal sign-in, dashboard syncs, operator alerts,
    # self-heal) gets "database is locked" for the entire production run.
    # It is also the correct durability order: a crash during provider I/O
    # must find the reservation already at boundary_entered, not rolled back
    # to reserved.
    lease_conn.commit()
    # Count this as an attempted provider-boundary entry *now*, before the
    # outcome is known -- Part 3 requirement 7: "Maximum-call limits must
    # count attempted provider-boundary entries, not only successful
    # outputs." A tripped max_provider_calls breaker here still lets this
    # already-in-flight attempt finish; it blocks the *next* submit() call
    # (checked at step 5 of that later call).
    _increment_counter_breaker(lease_conn, "provider_calls", "max_provider_calls", breaker_limits, at=at, attempt_id=attempt_id)

    # ---- Step 10: instantiate the resolved adapter and invoke it exactly once ----
    # Capability TTL: a capability authorizes ONE imminent provider call.
    # expires_at is stamped strictly later than issued_at (default
    # hypogeum.capability_boundary.CAPABILITY_TTL_SECONDS) unless the
    # caller supplied an explicit capability_expires_at. Every real
    # adapter re-verifies this window against an injected clock, plus the
    # full authoritative issuance, via hypogeum.capability_boundary.
    from hypogeum.capability_boundary import default_expiry_for  # noqa: PLC0415
    capability = _mint_capability(
        envelope, attempt_id, lease_owner_id, lease_fencing_token, reservation_ids, at,
        capability_expires_at or default_expiry_for(at),
    )
    try:
        adapter_instance = adapter_registry.get_or_create_adapter_instance(adapter_class)
        on_provider_acknowledged = _make_provider_acknowledgement_recorder(
            receipt_store, envelope, execution_id, attempt_id, at, lease_conn, lease_owner_id, lease_fencing_token,
        )
        adapter_result = adapter_instance.invoke(
            capability, mode=adapter_mode, on_provider_acknowledged=on_provider_acknowledged,
        )
        outcome = adapter_result.outcome
    except Exception as exc:  # noqa: BLE001 -- a crash *during* invocation is exactly "possible acceptance, no ack"
        adapter_result = None
        outcome = "unknown_acceptance"
        _persist_unknown_acceptance(receipt_store, envelope, execution_id, attempt_id, at, reason=f"adapter raised: {exc}")
        # Unknown acceptance is deliberately NOT released -- architecture:
        # "Unknown acceptance must count as a possible call and possible
        # spend at the authorized ceiling until reconciled." The
        # reservation stays 'reserved', still counted by
        # sum_reserved_and_committed(), until an explicit reconciliation
        # event resolves it (see hypogeum.provider_receipt.RECONCILED).
        _increment_counter_breaker(lease_conn, "unknown_acceptance", "max_unknown_acceptance", breaker_limits, at=at, attempt_id=attempt_id)
        _increment_counter_breaker(lease_conn, "consecutive_failures", "max_consecutive_failures", breaker_limits, at=at, attempt_id=attempt_id)
        _settle_grant_reservation(lease_conn, grant_binding_evidence, to_state="possible_unknown_acceptance", at=at)
        return GateResult(
            approved=False, denied_at_check=None, reason=f"adapter invocation raised: {exc}",
            adapter_invoked=True, adapter_result=None,
            recovery_state=receipt_store.classify_recovery_state(execution_id, envelope.idempotency_key),
            budget_reservation_ids=reservation_ids,
        )

    # ---- Step 11: persist acknowledgement / rejection / unknown-acceptance evidence ----
    if outcome == "unknown_acceptance":
        _persist_unknown_acceptance(receipt_store, envelope, execution_id, attempt_id, at, reason=str(adapter_result.evidence))
        # See the exception-path comment above: reservation intentionally
        # stays 'reserved', never released, while acceptance is unknown.
        _increment_counter_breaker(lease_conn, "unknown_acceptance", "max_unknown_acceptance", breaker_limits, at=at, attempt_id=attempt_id)
        _increment_counter_breaker(lease_conn, "consecutive_failures", "max_consecutive_failures", breaker_limits, at=at, attempt_id=attempt_id)
        _settle_grant_reservation(lease_conn, grant_binding_evidence, to_state="possible_unknown_acceptance", at=at)
        return GateResult(
            approved=False, denied_at_check=None, reason="provider acceptance unknown -- reconciliation required, never auto-retried",
            adapter_invoked=True, adapter_result=adapter_result,
            recovery_state=receipt_store.classify_recovery_state(execution_id, envelope.idempotency_key),
            budget_reservation_ids=reservation_ids,
        )

    if outcome == "rejected":
        receipt_store.append_event(
            ProviderReceiptEvent(
                envelope_id=envelope.envelope_id, envelope_version=envelope.envelope_version,
                authorization_id=envelope.authorization_id, authorization_version=envelope.authorization_version,
                attempt_id=attempt_id, idempotency_key=envelope.idempotency_key,
                event_type=EventType.REJECTED,
                event_seq=len(receipt_store.read_events(execution_id, envelope.idempotency_key)), at=at,
                provider=envelope.provider, model=envelope.model, provider_operation=envelope.provider_operation,
                provider_bound_content_sha256=envelope.operation_payload_sha256,
                max_cost_per_call=envelope.max_cost_per_call, currency=envelope.currency,
                evidence=dict(adapter_result.evidence),
            ),
            execution_id=execution_id, lease_conn=lease_conn, lease_owner_id=lease_owner_id, lease_fencing_token=lease_fencing_token,
        )
        # A confirmed rejection (unlike unknown acceptance) is a definite
        # non-charge outcome -- safe to release the reservation.
        _release_reservations()
        _increment_counter_breaker(lease_conn, "consecutive_failures", "max_consecutive_failures", breaker_limits, at=at, attempt_id=attempt_id)
        _settle_grant_reservation(lease_conn, grant_binding_evidence, to_state="released", at=at)
        return GateResult(
            approved=False, denied_at_check=None, reason="provider explicitly rejected",
            adapter_invoked=True, adapter_result=adapter_result,
            recovery_state=receipt_store.classify_recovery_state(execution_id, envelope.idempotency_key),
            budget_reservation_ids=reservation_ids,
        )

    # outcome == "accepted"
    receipt_store.append_event(
        ProviderReceiptEvent(
            envelope_id=envelope.envelope_id, envelope_version=envelope.envelope_version,
            authorization_id=envelope.authorization_id, authorization_version=envelope.authorization_version,
            attempt_id=attempt_id, idempotency_key=envelope.idempotency_key,
            event_type=EventType.ACCEPTED,
            event_seq=len(receipt_store.read_events(execution_id, envelope.idempotency_key)), at=at,
            provider=envelope.provider, model=envelope.model, provider_operation=envelope.provider_operation,
            provider_bound_content_sha256=envelope.operation_payload_sha256,
            max_cost_per_call=envelope.max_cost_per_call, currency=envelope.currency,
            evidence={"provider_job_id": adapter_result.provider_job_id, "provider_request_id": adapter_result.provider_request_id},
        ),
        execution_id=execution_id, lease_conn=lease_conn, lease_owner_id=lease_owner_id, lease_fencing_token=lease_fencing_token,
    )
    _reset_counter_breaker_observed_value(lease_conn, "consecutive_failures", breaker_limits, at=at)
    _settle_grant_reservation(
        lease_conn, grant_binding_evidence, to_state="committed", at=at, charged_amount=adapter_result.charged_amount,
    )

    # ---- Step 12: update budget/breaker evidence according to result ----
    overcharge_exc = None
    for reservation_id in reservation_ids:
        try:
            bs.commit_reservation(
                lease_conn, reservation_id, authorized_ceiling=envelope.max_cost_per_call,
                charged_amount=adapter_result.charged_amount, at=at,
            )
        except bs.OverchargeViolation as exc:
            overcharge_exc = exc

    breaker_evidence = []
    if breaker_limits is not None and breaker_limits.max_spend is not None:
        total_committed = sum(
            (Decimal(r["committed_amount"]) for rid in reservation_ids
             if (r := db.get_budget_reservation(lease_conn, rid)) and r["committed_amount"]),
            Decimal("0"),
        )
        tripped = total_committed > breaker_limits.max_spend
        _record_breaker_observation(
            lease_conn, "max_spend", breaker_limits, str(total_committed), str(breaker_limits.max_spend),
            tripped=tripped, attempt_id=attempt_id, at=at, evidence={"total_committed": str(total_committed)},
        )
        breaker_evidence.append(bs.get_breaker(lease_conn, _breaker_id("max_spend", breaker_limits.scope_kind, breaker_limits.scope_ref)))

    if overcharge_exc is not None and breaker_limits is not None:
        _record_breaker_observation(
            lease_conn, "overcharge", breaker_limits, str(overcharge_exc.charged), str(overcharge_exc.authorized),
            tripped=True, attempt_id=attempt_id, at=at,
            evidence={"authorized": str(overcharge_exc.authorized), "charged": str(overcharge_exc.charged)},
        )
        breaker_evidence.append(bs.get_breaker(lease_conn, _breaker_id("overcharge", breaker_limits.scope_kind, breaker_limits.scope_ref)))

    return GateResult(
        approved=True, denied_at_check=None, reason=None,
        adapter_invoked=True, adapter_result=adapter_result,
        recovery_state=receipt_store.classify_recovery_state(execution_id, envelope.idempotency_key),
        budget_reservation_ids=reservation_ids, breaker_evidence=tuple(breaker_evidence),
    )


def _make_provider_acknowledgement_recorder(
    receipt_store, envelope, execution_id, attempt_id, at, lease_conn, lease_owner_id, lease_fencing_token,
):
    """Builds the on_provider_acknowledged callback passed into
    adapter_instance.invoke() -- the ONE thing an adapter is given to
    durably record a real provider job/request ID the moment it has
    one, strictly before any subsequent poll/wait. Appends a
    PROVIDER_ACKNOWLEDGED receipt event synchronously at the current
    tail of the trail (immediately after SUBMISSION_ATTEMPT_STARTED and
    hypogeum.capability_boundary's own durable consumption-claim event --
    this event, when it happens at all, always happens at exactly this
    point in the sequence). Lease-protected exactly like the boundary-
    entry event it directly follows."""
    def _on_provider_acknowledged(*, provider_job_id=None, provider_request_id=None):
        receipt_store.append_event(
            ProviderReceiptEvent(
                envelope_id=envelope.envelope_id, envelope_version=envelope.envelope_version,
                authorization_id=envelope.authorization_id, authorization_version=envelope.authorization_version,
                attempt_id=attempt_id, idempotency_key=envelope.idempotency_key,
                event_type=EventType.PROVIDER_ACKNOWLEDGED,
                event_seq=len(receipt_store.read_events(execution_id, envelope.idempotency_key)), at=at,
                provider=envelope.provider, model=envelope.model, provider_operation=envelope.provider_operation,
                provider_bound_content_sha256=envelope.operation_payload_sha256,
                max_cost_per_call=envelope.max_cost_per_call, currency=envelope.currency,
                evidence={"provider_job_id": provider_job_id, "provider_request_id": provider_request_id},
            ),
            execution_id=execution_id,
            lease_conn=lease_conn, lease_owner_id=lease_owner_id, lease_fencing_token=lease_fencing_token,
        )
    return _on_provider_acknowledged


def _persist_unknown_acceptance(receipt_store, envelope, execution_id, attempt_id, at, *, reason: str):
    # Fold in the provider job/request ID from an already-durably-
    # written PROVIDER_ACKNOWLEDGED event, if one exists for this
    # attempt -- this is what makes "post-submit timeout retains
    # request_id in unknown-acceptance evidence" hold whether this
    # function is reached via a normal AdapterResult(outcome=
    # "unknown_acceptance") return OR via the caller's own except-block
    # after the adapter raised mid-invocation (in both cases, the
    # acknowledgement -- if any -- was already written before the
    # failure, never reconstructed after the fact).
    existing_events = receipt_store.read_events(execution_id, envelope.idempotency_key)
    ack_event = next(
        (e for e in reversed(existing_events) if e.event_type == EventType.PROVIDER_ACKNOWLEDGED), None,
    )
    evidence = {"detected_by": "submission_gate", "reason": reason}
    if ack_event is not None:
        evidence["provider_job_id"] = ack_event.evidence.get("provider_job_id")
        evidence["provider_request_id"] = ack_event.evidence.get("provider_request_id")

    receipt_store.append_event(
        ProviderReceiptEvent(
            envelope_id=envelope.envelope_id, envelope_version=envelope.envelope_version,
            authorization_id=envelope.authorization_id, authorization_version=envelope.authorization_version,
            attempt_id=attempt_id, idempotency_key=envelope.idempotency_key,
            event_type=EventType.UNKNOWN_ACCEPTANCE, event_seq=len(existing_events), at=at,
            provider=envelope.provider, model=envelope.model, provider_operation=envelope.provider_operation,
            provider_bound_content_sha256=envelope.operation_payload_sha256,
            max_cost_per_call=envelope.max_cost_per_call, currency=envelope.currency,
            evidence=evidence,
        ),
        execution_id=execution_id,
    )


def _mint_capability(
    envelope: GenerationExecutionEnvelope, attempt_id: str, lease_owner_id: str, lease_fencing_token: int,
    reservation_ids: Sequence[str], issued_at: str, expires_at: str,
) -> AdapterInvocationCapability:
    """The only place a capability is ever constructed. Never exposed as
    a standalone public helper a caller could invoke with arbitrary
    fields (Part 5 requirement 6) -- it is a private function called
    exactly once, at exactly this point in submit(), always from
    already-gate-validated envelope fields."""
    capability = AdapterInvocationCapability(
        capability_id=f"{envelope.idempotency_key}:{attempt_id}",
        envelope_id=envelope.envelope_id, envelope_version=envelope.envelope_version,
        attempt_id=attempt_id, idempotency_key=envelope.idempotency_key,
        lease_owner_id=lease_owner_id, lease_fencing_token=lease_fencing_token,
        provider=envelope.provider, model=envelope.model, provider_operation=envelope.provider_operation,
        operation_payload_bytes=envelope.operation_payload_content.encode("utf-8"),
        operation_payload_sha256=envelope.operation_payload_sha256,
        operation_payload_schema_version=envelope.operation_payload_schema_version,
        max_cost_per_call=envelope.max_cost_per_call, currency=envelope.currency,
        budget_reservation_ids=tuple(reservation_ids), issued_at=issued_at, expires_at=expires_at,
        capability_checksum="",
    )
    import dataclasses
    checksum = compute_capability_checksum(capability)
    return dataclasses.replace(capability, capability_checksum=checksum)


# ---------------------------------------------------------------------------
# Non-mutating shadow evaluation (Commit 4 / Part 1)
#
# run_shadow_checks() reuses the exact same private check functions
# submit() itself calls -- never a second, independently-maintained
# checklist -- but never proceeds past check evaluation: no budget
# reservation, no write-ahead persistence, no capability minting, no
# adapter call, no lease/breaker mutation. Missing evidence for a given
# check (e.g. no AuthorizationRecord supplied) is reported as
# "unverifiable" for that check, never silently skipped as "pass".
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CheckOutcome:
    check: str
    status: str  # "pass" | "fail" | "unverifiable"
    detail: Optional[str] = None


def _check_grant_capacity_shadow(
    lease_conn, envelope: GenerationExecutionEnvelope, authorization: Optional[AuthorizationRecord],
) -> CheckOutcome:
    """
    Read-only grant-capacity parity check for Observation-Only (plan
    §5.5 Correction 4's remaining-capacity computation, added for
    Commit M) -- never reserves anything, never writes a ledger row.
    Reports whether the grant that authorized (or would authorize)
    this envelope still has room, purely as diagnostic evidence in the
    shadow report. "unverifiable" whenever the evidence needed to even
    ask the question is absent -- there is no lower-severity "skip"
    outcome, matching every other check in this function.
    """
    if authorization is None:
        return CheckOutcome(
            "grant_capacity_remaining", "unverifiable",
            "no authorization supplied -- cannot tell whether this would be a grant-derived attempt",
        )
    grant_evidence = _grant_binding_evidence(authorization)
    if grant_evidence is None:
        # A complete, valid authorization that simply carries no grant
        # evidence (the one-off explicit-approval path, or -- true for
        # every real domain today -- no grant exists at all) is not a
        # capacity-check failure: there is no grant ceiling to exceed.
        # A genuine "pass", never "unverifiable" -- otherwise every
        # non-grant-derived shadow evaluation across every domain would
        # be blocked from ever reaching WOULD_ALLOW.
        return CheckOutcome("grant_capacity_remaining", "pass", "not a grant-derived attempt -- no grant ceiling applies")
    if lease_conn is None:
        return CheckOutcome("grant_capacity_remaining", "unverifiable", "no lease_conn supplied -- cannot read the grant ledger")

    grant = authorization_store.get_grant(lease_conn, grant_evidence["parent_grant_id"], grant_evidence["parent_grant_version"])
    if grant is None:
        return CheckOutcome("grant_capacity_remaining", "fail", "parent grant no longer exists")
    if grant.status != "active":
        return CheckOutcome("grant_capacity_remaining", "fail", f"parent grant status is {grant.status!r}, not 'active'")

    binding = grant.find_binding(envelope.provider, envelope.model, envelope.provider_operation)
    if binding is None:
        return CheckOutcome("grant_capacity_remaining", "fail", "no matching grant binding for this envelope's (provider, model, provider_operation)")

    call_count, spend_total = authorization_store.sum_active_ledger_consumption(
        lease_conn, grant.grant_id, envelope.provider, envelope.model, envelope.provider_operation,
    )
    if binding.max_calls is not None and call_count >= binding.max_calls:
        return CheckOutcome("grant_capacity_remaining", "fail", f"binding call capacity exhausted: {call_count}/{binding.max_calls}")
    if binding.max_total_spend is not None and spend_total + envelope.max_cost_per_call > binding.max_total_spend:
        return CheckOutcome(
            "grant_capacity_remaining", "fail",
            f"binding spend capacity would be exceeded: {spend_total} + {envelope.max_cost_per_call} > {binding.max_total_spend}",
        )

    max_calls_display = binding.max_calls if binding.max_calls is not None else "unlimited"
    max_spend_display = binding.max_total_spend if binding.max_total_spend is not None else "unlimited"
    return CheckOutcome(
        "grant_capacity_remaining", "pass",
        f"{call_count}/{max_calls_display} calls, {spend_total}/{max_spend_display} spend",
    )


def run_shadow_checks(
    envelope: Optional[GenerationExecutionEnvelope],
    *,
    live_sources: Optional[LiveSources] = None,
    required_provider: Optional[str] = None,
    required_model: Optional[str] = None,
    dominance_leak_validator=None,
    expected_duration_seconds: Optional[Decimal] = None,
    expected_aspect_ratio: Optional[str] = None,
    expected_seed: Optional[int] = None,
    expected_output_identity: Optional[str] = None,
    expected_deterministic_output_path: Optional[str] = None,
    output_already_exists: Optional[bool] = None,
    authorization: Optional[AuthorizationRecord] = None,
    lease_conn=None,
    lease_owner_id: Optional[str] = None,
    lease_fencing_token: Optional[int] = None,
    receipt_store=None,
    execution_id: Optional[str] = None,
    retry_context: Optional[RetryContext] = None,
    fallback_context: Optional[FallbackContext] = None,
    budget_scopes: Optional[Sequence[bs.ScopeRequest]] = None,
    at: str,
) -> Sequence[CheckOutcome]:
    """
    Runs GATE_CHECK_ORDER's checks against whatever evidence was
    supplied, in the same order submit() evaluates them, and returns one
    CheckOutcome per check -- "unverifiable" (never "pass") for any check
    whose required evidence was not supplied. Never mutates anything:
    no DB write, no file write, no adapter call.

    A caller (e.g. hypogeum.shadow_observation.evaluate_shadow()) is
    responsible for turning this list into a final verdict and for
    catching any exception this function itself might raise (a
    malformed input can still raise here -- the caller's job is to
    convert that into structured UNVERIFIABLE evidence, per Part 1
    requirement 8; this function does not swallow exceptions itself,
    so callers always see the real failure).
    """
    outcomes: list = []

    if envelope is None:
        outcomes.append(CheckOutcome(GATE_CHECK_ORDER[0], "unverifiable", "missing_generation_execution_envelope"))
        return tuple(outcomes)

    if live_sources is None:
        outcomes.append(CheckOutcome(GATE_CHECK_ORDER[5], "unverifiable", "no live_sources supplied -- cannot verify dependency freshness"))
    else:
        denial = _validate_envelope_and_sources(envelope, live_sources, required_model, required_provider, dominance_leak_validator)
        if denial:
            outcomes.append(CheckOutcome(denial.check, "fail", denial.reason))
        else:
            outcomes.append(CheckOutcome("01-16_envelope_sources_binding", "pass"))

    binding_denial = _validate_execution_binding(
        envelope, expected_duration_seconds, expected_aspect_ratio, expected_seed,
        expected_output_identity, expected_deterministic_output_path, bool(output_already_exists),
    )
    if binding_denial:
        outcomes.append(CheckOutcome(binding_denial.check, "fail", binding_denial.reason))
    else:
        outcomes.append(CheckOutcome(GATE_CHECK_ORDER[14], "pass"))

    if authorization is None:
        outcomes.append(CheckOutcome(GATE_CHECK_ORDER[2], "unverifiable", "no authorization record supplied"))
    else:
        denial = _validate_authorization(envelope, authorization, at)
        outcomes.append(CheckOutcome(denial.check, "fail", denial.reason) if denial else CheckOutcome(GATE_CHECK_ORDER[2], "pass"))

    if lease_conn is None or lease_owner_id is None or lease_fencing_token is None:
        outcomes.append(CheckOutcome(GATE_CHECK_ORDER[18], "unverifiable", "no lease evidence supplied"))
    else:
        denial = _validate_lease(lease_conn, envelope, lease_owner_id, lease_fencing_token, at)
        outcomes.append(CheckOutcome(denial.check, "fail", denial.reason) if denial else CheckOutcome(GATE_CHECK_ORDER[18], "pass"))

    if receipt_store is None or execution_id is None:
        outcomes.append(CheckOutcome(GATE_CHECK_ORDER[4], "unverifiable", "no receipt store/execution_id supplied -- cannot verify recovery/idempotency state"))
    else:
        denial, _state = _validate_recovery_state(receipt_store, execution_id, envelope, retry_context)
        outcomes.append(CheckOutcome(denial.check, "fail", denial.reason) if denial else CheckOutcome(GATE_CHECK_ORDER[4], "pass"))

    retry_fallback_denial = _validate_retry_fallback_authorization(envelope, retry_context, fallback_context)
    if retry_fallback_denial:
        outcomes.append(CheckOutcome(retry_fallback_denial.check, "fail", retry_fallback_denial.reason))
    else:
        outcomes.append(CheckOutcome(GATE_CHECK_ORDER[17], "pass"))

    if not budget_scopes:
        outcomes.append(CheckOutcome(GATE_CHECK_ORDER[16], "unverifiable", "no budget scopes supplied"))
    else:
        outcomes.append(CheckOutcome(GATE_CHECK_ORDER[16], "pass", "scope configuration present (no reservation attempted in shadow mode)"))

    outcomes.append(_check_grant_capacity_shadow(lease_conn, envelope, authorization))

    return tuple(outcomes)
