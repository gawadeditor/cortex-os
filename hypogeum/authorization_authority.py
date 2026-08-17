"""
hypogeum/authorization_authority.py — The only minter of
AuthorizationRecords and RunAuthorizationGrants (Factory Core).

Three trust boundaries, three actors: a domain caller COMPILES a
GenerationExecutionEnvelope (unchanged, existing logic); this module
AUTHORIZES it, either from a durable, pre-approved bounded grant (for
ordinary autonomous production calls) or from a one-off explicit human
approval; hypogeum.submission_gate.submit() SUBMITS an already-
AUTHORIZED envelope. No domain caller, workflow stage, worker,
scheduler, retry path, adapter, or the submission gate itself may call
anything in this module that mints or activates authorization --
submission_gate.py imports only hypogeum.authorization_store's narrow
read/revalidate/settle functions, never this module.

Every function here that accepts an ApprovalArtifact verifies it
(Ed25519 signature, against the trusted public key loaded internally
via _load_trusted_public_key() -- never a caller-suppliable parameter,
so no caller can substitute a different "trusted" key) before treating
it as proof of anything. A fabricated, tampered, replayed, or
wrong-keypair artifact is denied at the first check, before any grant
or authorization is touched.
"""

from __future__ import annotations

import base64
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Optional, Sequence

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import hypogeum.authorization_store as store
from hypogeum.approval_artifact import ApprovalArtifact, verify_approval_artifact
from hypogeum.authorization_lifecycle import AuthorizationRecord, AuthorizationState
from hypogeum.db import get_connection
from hypogeum.execution_envelope import GenerationExecutionEnvelope

__all__ = [
    "AuthorizationAuthorityError",
    "TrustAnchorNotConfiguredError",
    "InvalidApprovalSignatureError",
    "ApprovalSubjectMismatchError",
    "ApprovalTypeMismatchError",
    "GrantNotFoundError",
    "GrantUnverifiableError",
    "GrantRevokedError",
    "GrantExpiredError",
    "NoMatchingGrantBindingError",
    "EnvelopeExceedsGrantLimitsError",
    "GrantCapacityExhaustedError",
    "compute_grant_checksum",
    "register_run_grant",
    "resolve_active_grant",
    "issue_child_authorization",
    "issue_authorization_from_explicit_approval",
    "activate_authorization_proposal",
    "get_authorization",
    "revoke_grant",
]


class AuthorizationAuthorityError(Exception):
    """Base class for every failure in this module."""


class TrustAnchorNotConfiguredError(AuthorizationAuthorityError):
    """AUTHORIZATION_APPROVAL_PUBLIC_KEY is not configured -- there is
    no trust anchor to verify any ApprovalArtifact against, so every
    approval-dependent operation fails closed rather than silently
    treating an unconfigured trust anchor as "verification not
    required"."""


class InvalidApprovalSignatureError(AuthorizationAuthorityError):
    pass


class ApprovalSubjectMismatchError(AuthorizationAuthorityError):
    """The artifact's subject_checksum does not match the exact
    payload it is being applied to."""


class ApprovalTypeMismatchError(AuthorizationAuthorityError):
    pass


class GrantNotFoundError(AuthorizationAuthorityError):
    pass


class GrantUnverifiableError(AuthorizationAuthorityError):
    """The grant's stored ApprovalArtifact no longer verifies -- either
    it is missing, or its signature no longer checks out against the
    trust anchor. A grant row that reaches this state (e.g. via a
    hand-crafted direct SQL INSERT bypassing register_run_grant()) is
    treated as unusable, never as "assume it's fine."""


class GrantRevokedError(AuthorizationAuthorityError):
    pass


class GrantExpiredError(AuthorizationAuthorityError):
    pass


class NoMatchingGrantBindingError(AuthorizationAuthorityError):
    pass


class EnvelopeExceedsGrantLimitsError(AuthorizationAuthorityError):
    pass


class GrantCapacityExhaustedError(AuthorizationAuthorityError):
    pass


def _load_trusted_public_key() -> Ed25519PublicKey:
    """The ONE place the trust anchor is resolved. Not a function
    parameter anywhere on this module's public API -- a caller cannot
    substitute a different "trusted" key by passing one in, since there
    is no parameter to pass it through. Tests get isolation by
    monkeypatching this function itself (`monkeypatch.setattr(
    authorization_authority, "_load_trusted_public_key", lambda: test_key)`),
    never by threading a parameter through production call sites."""
    from hypogeum.config import Config

    encoded = Config().AUTHORIZATION_APPROVAL_PUBLIC_KEY
    if not encoded:
        raise TrustAnchorNotConfiguredError(
            "AUTHORIZATION_APPROVAL_PUBLIC_KEY is not configured -- cannot verify any ApprovalArtifact"
        )
    raw = base64.b64decode(encoded)
    return Ed25519PublicKey.from_public_bytes(raw)


def _require_verified(artifact: ApprovalArtifact, *, approval_type: str, subject_checksum: str) -> None:
    public_key = _load_trusted_public_key()
    if not verify_approval_artifact(artifact, public_key=public_key):
        raise InvalidApprovalSignatureError(
            f"ApprovalArtifact {artifact.approval_id!r}@{artifact.approval_version} does not verify "
            "against the configured trust anchor"
        )
    if artifact.approval_type != approval_type:
        raise ApprovalTypeMismatchError(
            f"expected approval_type {approval_type!r}, artifact carries {artifact.approval_type!r}"
        )
    if artifact.subject_checksum != subject_checksum:
        raise ApprovalSubjectMismatchError(
            f"artifact subject_checksum {artifact.subject_checksum!r} != expected {subject_checksum!r} -- "
            "this artifact was signed for a different payload"
        )


# ---------------------------------------------------------------------------
# register_run_grant() -- Correction 2
# ---------------------------------------------------------------------------

def register_run_grant(
    *,
    grant_id: str,
    grant_version: int,
    channel_id: str,
    episode_id: str,
    workflow_id: str,
    workflow_version: str,
    workflow_checksum: str,
    bindings: Sequence[store.GrantBinding],
    max_total_spend_all_bindings: Optional[Decimal],
    currency: str,
    expires_at: str,
    approval_artifact: ApprovalArtifact,
    db_path: Optional[Path] = None,
) -> store.RunAuthorizationGrant:
    """
    Registers a grant -- but ONLY if approval_artifact verifies, was
    signed with approval_type="grant", and its subject_checksum matches
    this exact grant payload. No caller can construct a valid artifact
    without the offline private key (hypogeum.approval_artifact never
    exposes signing to anything but the offline scripts), so calling
    this function with a self-constructed or tampered artifact always
    fails before any grant row is written.
    """
    grant_checksum = compute_grant_checksum(
        channel_id=channel_id, episode_id=episode_id, workflow_id=workflow_id,
        workflow_version=workflow_version, workflow_checksum=workflow_checksum,
        bindings=bindings, max_total_spend_all_bindings=max_total_spend_all_bindings, currency=currency,
        expires_at=expires_at,
    )
    _require_verified(approval_artifact, approval_type="grant", subject_checksum=grant_checksum)

    conn = get_connection(db_path)
    try:
        store.ensure_tables(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            store.persist_approval_artifact(
                conn, approval_artifact,
                consumed_by_grant_id=grant_id, consumed_by_grant_version=grant_version,
            )
            grant = store.insert_grant(
                conn, grant_id=grant_id, grant_version=grant_version, grant_checksum=grant_checksum,
                channel_id=channel_id, episode_id=episode_id, workflow_id=workflow_id,
                workflow_version=workflow_version, workflow_checksum=workflow_checksum, bindings=bindings,
                max_total_spend_all_bindings=max_total_spend_all_bindings, currency=currency,
                expires_at=expires_at, approval_id=approval_artifact.approval_id,
                approval_version=approval_artifact.approval_version,
            )
            conn.commit()
            return grant
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def compute_grant_checksum(
    *, channel_id, episode_id, workflow_id, workflow_version, workflow_checksum,
    bindings: Sequence[store.GrantBinding], max_total_spend_all_bindings, currency, expires_at,
) -> str:
    from hypogeum.approval_artifact import compute_approval_subject_checksum
    payload = {
        "channel_id": channel_id, "episode_id": episode_id, "workflow_id": workflow_id,
        "workflow_version": workflow_version, "workflow_checksum": workflow_checksum,
        "bindings": [b.to_dict() for b in bindings],
        "max_total_spend_all_bindings": None if max_total_spend_all_bindings is None else str(max_total_spend_all_bindings),
        "currency": currency, "expires_at": expires_at,
    }
    return compute_approval_subject_checksum(payload)


# ---------------------------------------------------------------------------
# resolve_active_grant() -- read-time re-verification
# ---------------------------------------------------------------------------

def resolve_active_grant(
    channel_id: str, episode_id: str, *, at: str, db_path: Optional[Path] = None,
) -> Optional[store.RunAuthorizationGrant]:
    """Read-only. Returns None (never raises) if no grant exists for
    this scope, or if the candidate grant is revoked, expired, or its
    stored ApprovalArtifact no longer verifies -- a grant row inserted
    by any means other than register_run_grant()'s verified path (e.g.
    a hand-crafted direct SQL INSERT) has no matching verified artifact
    and is therefore treated as unusable here, every time it is read."""
    conn = get_connection(db_path)
    try:
        grant = store.find_active_grant_for_scope(conn, channel_id, episode_id)
        if grant is None:
            return None
        if at >= grant.expires_at:
            return None
        artifact = store.get_approval_artifact_for_grant(conn, grant.grant_id, grant.grant_version)
        if artifact is None:
            return None
        try:
            public_key = _load_trusted_public_key()
        except TrustAnchorNotConfiguredError:
            return None
        if not verify_approval_artifact(artifact, public_key=public_key):
            return None
        expected_checksum = compute_grant_checksum(
            channel_id=grant.channel_id, episode_id=grant.episode_id, workflow_id=grant.workflow_id,
            workflow_version=grant.workflow_version, workflow_checksum=grant.workflow_checksum,
            bindings=grant.bindings, max_total_spend_all_bindings=grant.max_total_spend_all_bindings,
            currency=grant.currency, expires_at=grant.expires_at,
        )
        if artifact.subject_checksum != expected_checksum or grant.grant_checksum != expected_checksum:
            return None
        return grant
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# issue_child_authorization() -- Correction 3/4
# ---------------------------------------------------------------------------

def issue_child_authorization(
    envelope: GenerationExecutionEnvelope, grant_id: str, grant_version: int, *,
    at: str, db_path: Optional[Path] = None,
) -> AuthorizationRecord:
    """
    See plan §5.5 for the full algorithm. Uses envelope.idempotency_key
    (already a required envelope field) as the ledger's idempotency
    key -- no separate, potentially-conflicting parameter is accepted.
    """
    conn = get_connection(db_path)
    try:
        store.ensure_tables(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            grant = store.get_grant(conn, grant_id, grant_version)
            if grant is None:
                raise GrantNotFoundError(f"grant {grant_id!r}@{grant_version} not found")
            if grant.status != "active":
                raise GrantRevokedError(f"grant {grant_id!r}@{grant_version} is {grant.status!r}, not active")
            if at >= grant.expires_at:
                raise GrantExpiredError(f"grant {grant_id!r}@{grant_version} expired at {grant.expires_at!r}")

            artifact = store.get_approval_artifact_for_grant(conn, grant_id, grant_version)
            public_key = _load_trusted_public_key()
            if artifact is None or not verify_approval_artifact(artifact, public_key=public_key):
                raise GrantUnverifiableError(f"grant {grant_id!r}@{grant_version} has no verifiable approval artifact")

            binding = grant.find_binding(envelope.provider, envelope.model, envelope.provider_operation)
            if binding is None:
                raise NoMatchingGrantBindingError(
                    f"grant {grant_id!r}@{grant_version} has no binding for "
                    f"({envelope.provider!r}, {envelope.model!r}, {envelope.provider_operation!r})"
                )

            if envelope.max_cost_per_call > binding.max_cost_per_call:
                raise EnvelopeExceedsGrantLimitsError(
                    f"envelope max_cost_per_call {envelope.max_cost_per_call} > binding max {binding.max_cost_per_call}"
                )
            if envelope.currency != binding.currency:
                raise EnvelopeExceedsGrantLimitsError(
                    f"envelope currency {envelope.currency!r} != binding currency {binding.currency!r}"
                )
            if envelope.retry_authorized and not binding.retry_authorized:
                raise EnvelopeExceedsGrantLimitsError("envelope requests retry_authorized but binding does not permit it")
            if envelope.fallback_authorized and not binding.fallback_authorized:
                raise EnvelopeExceedsGrantLimitsError("envelope requests fallback_authorized but binding does not permit it")

            calls, spend = store.sum_active_ledger_consumption(
                conn, grant_id, envelope.provider, envelope.model, envelope.provider_operation,
            )
            if calls + 1 > binding.max_calls:
                raise GrantCapacityExhaustedError(
                    f"binding call capacity exhausted: {calls} already active, max_calls={binding.max_calls}"
                )
            if spend + envelope.max_cost_per_call > binding.max_total_spend:
                raise GrantCapacityExhaustedError(
                    f"binding spend capacity exhausted: {spend} already active + {envelope.max_cost_per_call} "
                    f"would exceed max_total_spend={binding.max_total_spend}"
                )
            if grant.max_total_spend_all_bindings is not None:
                grant_total = _sum_grant_wide_consumption(conn, grant)
                if grant_total + envelope.max_cost_per_call > grant.max_total_spend_all_bindings:
                    raise GrantCapacityExhaustedError(
                        f"grant-wide spend capacity exhausted: {grant_total} already active + "
                        f"{envelope.max_cost_per_call} would exceed max_total_spend_all_bindings="
                        f"{grant.max_total_spend_all_bindings}"
                    )

            # AuthorizationRecord.authorization_id must be exactly
            # envelope.authorization_id -- an envelope carries its own
            # authorization identity from compile time
            # (hypogeum.submission_gate._validate_authorization()
            # requires the two to match exactly), never a value this
            # module invents independently.
            authorization_id = envelope.authorization_id
            reservation_id = f"{grant_id}:{grant_version}:{binding.binding_key()}:{envelope.idempotency_key}"

            reservation = store.insert_ledger_reservation(
                conn, reservation_id=reservation_id, grant_id=grant_id, grant_version=grant_version,
                provider=envelope.provider, model=envelope.model, provider_operation=envelope.provider_operation,
                idempotency_key=envelope.idempotency_key, envelope_id=envelope.envelope_id,
                authorization_id=authorization_id, reserved_amount=envelope.max_cost_per_call, at=at,
            )

            existing_record = store.get_authorization_record(conn, authorization_id)
            if existing_record is not None:
                conn.commit()
                return existing_record

            record = AuthorizationRecord.create(
                authorization_id=authorization_id, authorization_version=envelope.authorization_version,
                envelope_id=envelope.envelope_id, envelope_version=envelope.envelope_version,
            )
            record = record.transition(
                AuthorizationState.COMPILED,
                evidence={"compiler_identity": envelope.compiler_identity, "compiler_version": envelope.compiler_version},
                at=at,
            )
            record = record.transition(
                AuthorizationState.AWAITING_AUTHORIZATION,
                evidence={"preflight_result": "authority_bounded_grant_checks_passed"},
                at=at,
            )
            record = record.transition(
                AuthorizationState.AUTHORIZED,
                evidence={
                    "authorized_by": f"run_authorization_grant:{grant_id}",
                    "authorization_method": "bounded_run_grant_autonomous",
                    "parent_grant_id": grant_id,
                    "parent_grant_version": grant_version,
                    "parent_grant_checksum": grant.grant_checksum,
                    "grant_reservation_id": reservation.reservation_id,
                },
                at=at,
            )
            store.persist_authorization_record(conn, record)
            conn.commit()
            return record
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def _sum_grant_wide_consumption(conn: sqlite3.Connection, grant: store.RunAuthorizationGrant) -> Decimal:
    total = Decimal("0")
    for b in grant.bindings:
        _count, spend = store.sum_active_ledger_consumption(conn, grant.grant_id, b.provider, b.model, b.provider_operation)
        total += spend
    return total


# ---------------------------------------------------------------------------
# issue_authorization_from_explicit_approval() -- one-off human approval
# ---------------------------------------------------------------------------

def issue_authorization_from_explicit_approval(
    envelope: GenerationExecutionEnvelope, *, approval_artifact: ApprovalArtifact,
    at: str, db_path: Optional[Path] = None,
) -> AuthorizationRecord:
    """
    The one-off path (Canary-class flows; Commit M's future cutover).
    No grant, no ledger -- requires a verified ApprovalArtifact with
    approval_type="authorization_proposal" whose subject_checksum
    matches the envelope's own envelope_id exactly (binding the
    approval to this one exact envelope, never "anything with this
    shape"). No free-text approval_evidence is accepted anywhere in
    this path.
    """
    _require_verified(
        approval_artifact, approval_type="authorization_proposal", subject_checksum=envelope.envelope_id,
    )

    authorization_id = envelope.authorization_id

    conn = get_connection(db_path)
    try:
        store.ensure_tables(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            store.persist_approval_artifact(conn, approval_artifact, consumed_by_authorization_id=authorization_id)

            existing_record = store.get_authorization_record(conn, authorization_id)
            if existing_record is not None:
                conn.commit()
                return existing_record

            record = AuthorizationRecord.create(
                authorization_id=authorization_id, authorization_version=envelope.authorization_version,
                envelope_id=envelope.envelope_id, envelope_version=envelope.envelope_version,
            )
            record = record.transition(
                AuthorizationState.COMPILED,
                evidence={"compiler_identity": envelope.compiler_identity, "compiler_version": envelope.compiler_version},
                at=at,
            )
            record = record.transition(
                AuthorizationState.AWAITING_AUTHORIZATION,
                evidence={"preflight_result": "authority_explicit_approval_checks_passed"},
                at=at,
            )
            record = record.transition(
                AuthorizationState.AUTHORIZED,
                evidence={
                    "authorized_by": approval_artifact.approver_identity,
                    "authorization_method": "explicit_human_approval",
                    "approval_id": approval_artifact.approval_id,
                    "approval_version": approval_artifact.approval_version,
                },
                at=at,
            )
            store.persist_authorization_record(conn, record)
            conn.commit()
            return record
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def activate_authorization_proposal(
    proposal_checksum: str, *, approval_artifact: ApprovalArtifact, at: str, db_path: Optional[Path] = None,
) -> "list[AuthorizationRecord]":
    """
    Generic, atomic, idempotent bulk activation of an already-compiled,
    already-proposed batch (hypogeum.authorization_store.
    AuthorizationProposal, produced by scripts.propose_current_episode_
    authorization) from AWAITING_EXPLICIT_AUTHORIZATION to AUTHORIZED,
    given one verified ApprovalArtifact bound to the proposal's own
    checksum.

    The missing counterpart to issue_authorization_from_explicit_
    approval(): that function only ever constructs a BRAND NEW record
    when none exists yet at its authorization_id, and silently returns
    an EXISTING record unchanged otherwise -- structurally unable to
    activate a batch that has already reached AWAITING_EXPLICIT_
    AUTHORIZATION. This function's target state transition
    (AWAITING_EXPLICIT_AUTHORIZATION -> AUTHORIZED) and its required
    evidence (authorized_by, authorization_method, approval_artifact_id,
    approval_subject_checksum) are unchanged from authorization_
    lifecycle.py's own existing, already-defined edge -- no new
    lifecycle state or transition is introduced.

    Fails closed, before mutating anything, if: no AuthorizationProposal
    exists with this exact proposal_checksum; any one of the proposal's
    own named authorization_ids is missing; or any one is in a state
    other than AWAITING_EXPLICIT_AUTHORIZATION or (for idempotent
    replay only) already AUTHORIZED under this exact approval_id.
    Validates the ENTIRE named set before transitioning ANY of them --
    all-or-nothing, one BEGIN IMMEDIATE transaction.

    Idempotent replay: if every named authorization_id is already
    AUTHORIZED with approval_artifact_id == this artifact's own
    approval_id, returns the existing records unchanged -- no
    re-transition, no duplicate approval_artifacts row. A record
    already AUTHORIZED under a DIFFERENT approval_id fails closed
    (never silently re-approves under a different signature).

    No grant, no ledger -- deliberately, structurally distinct from
    issue_child_authorization(envelope, grant_id, grant_version, ...),
    the OTHER path that reaches AUTHORIZED. This function's own
    AUTHORIZED-transition evidence never carries parent_grant_id/
    parent_grant_version/parent_grant_checksum/grant_reservation_id --
    only authorized_by/authorization_method/approval_artifact_id/
    approval_subject_checksum. A RunAuthorizationGrant registered
    separately (e.g. via register_run_grant()) alongside a batch this
    function activates is therefore NEVER consulted, consumed, or
    revalidated by hypogeum.submission_gate.submit() for any record
    this function produces -- see that module's
    _grant_binding_evidence() docstring for the real incident
    (ep_f3619022 v4 replacement batch, 2026-08-17) that surfaced this,
    and tests/test_grant_not_consumed_by_proposal_activation.py for a
    structural proof. This is a known, documented gap, not a defect
    introduced here -- closing it (e.g. an optional grant-binding
    parameter on this function) is future, separately-authorized work.
    """
    _require_verified(approval_artifact, approval_type="authorization_proposal", subject_checksum=proposal_checksum)

    conn = get_connection(db_path)
    try:
        store.ensure_tables(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            proposal = store.get_authorization_proposal_by_checksum(conn, proposal_checksum)
            if proposal is None:
                raise store.AuthorizationRecordNotFoundError(
                    f"no AuthorizationProposal found with proposal_checksum={proposal_checksum!r}"
                )

            records: dict = {}
            already_authorized_under_this_approval = True
            for authorization_id in proposal.authorization_ids:
                record = store.get_authorization_record(conn, authorization_id)
                if record is None:
                    raise store.AuthorizationRecordNotFoundError(authorization_id)
                if record.current_state == AuthorizationState.AUTHORIZED:
                    approved_evidence = next(
                        (h.evidence for h in record.history if h.to_state == AuthorizationState.AUTHORIZED), {},
                    )
                    if approved_evidence.get("approval_artifact_id") != approval_artifact.approval_id:
                        raise store.ProposalReconciliationConflictError(
                            f"authorization {authorization_id!r} is already AUTHORIZED under a DIFFERENT "
                            f"approval_artifact_id ({approved_evidence.get('approval_artifact_id')!r}), cannot "
                            f"re-approve under {approval_artifact.approval_id!r}"
                        )
                    records[authorization_id] = record
                    continue
                already_authorized_under_this_approval = False
                if record.current_state != AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION:
                    raise store.ProposalReconciliationConflictError(
                        f"authorization {authorization_id!r} is at {record.current_state.value!r}, "
                        "not 'awaiting_explicit_authorization' -- not eligible for bulk activation"
                    )
                records[authorization_id] = record

            if already_authorized_under_this_approval:
                conn.commit()
                return [records[aid] for aid in proposal.authorization_ids]

            store.persist_approval_artifact(conn, approval_artifact, consumed_by_authorization_id=proposal.proposal_id)

            activated = []
            for authorization_id in proposal.authorization_ids:
                record = records[authorization_id]
                if record.current_state != AuthorizationState.AUTHORIZED:
                    record = record.transition(
                        AuthorizationState.AUTHORIZED,
                        evidence={
                            "authorized_by": approval_artifact.approver_identity,
                            "authorization_method": "explicit_human_approval_proposal_batch",
                            "approval_artifact_id": approval_artifact.approval_id,
                            "approval_subject_checksum": approval_artifact.subject_checksum,
                        },
                        at=at,
                    )
                    store.persist_authorization_record(conn, record)
                activated.append(record)

            conn.commit()
            return activated
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reads / revocation
# ---------------------------------------------------------------------------

def get_authorization(authorization_id: str, *, db_path: Optional[Path] = None) -> Optional[AuthorizationRecord]:
    conn = get_connection(db_path)
    try:
        return store.get_authorization_record(conn, authorization_id)
    finally:
        conn.close()


def revoke_grant(
    grant_id: str, grant_version: int, *, revoked_by: str, reason: str, at: str,
    db_path: Optional[Path] = None,
) -> bool:
    conn = get_connection(db_path)
    try:
        result = store.revoke_grant_row(conn, grant_id, grant_version, revoked_by=revoked_by, reason=reason, at=at)
        conn.commit()
        return result
    finally:
        conn.close()
