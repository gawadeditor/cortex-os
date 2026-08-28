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
import re
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
    "GrantScopeMismatchError",
    "GrantPlanHashMismatchError",
    "compute_grant_checksum",
    "register_run_grant",
    "register_channel_operating_grant",
    "resolve_active_operating_grant",
    "OperatingGrantNotYetValidError",
    "CHANNEL_OPERATING_EPISODE_SENTINEL",
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


class GrantScopeMismatchError(AuthorizationAuthorityError):
    """The authorization target's (envelope's or proposal's) declared
    channel_id/episode_id/workflow identity does not exactly equal the
    grant's own -- or the target is missing that scope evidence
    entirely. Raised by _validate_grant_scope_binding(), the ONE shared
    check both issue_child_authorization() and
    activate_authorization_proposal() call, before any ledger
    reservation or authorization state transition. A grant registered
    for one channel/episode/workflow can never authorize a target
    belonging to a different one, even if every provider/model/
    operation/currency/cost binding happens to match."""


class GrantPlanHashMismatchError(AuthorizationAuthorityError):
    """A plan-hash binding mismatch on a plan-bound child authorization:
    the grant carries no production_plan_content_hash, its hash differs
    from the caller-supplied verified Production Plan hash, or the
    envelope's dependency_checksums.plan differs from that verified
    hash. Raised by issue_child_authorization() ONLY when the caller
    opts in via expected_production_plan_content_hash (today: the paid
    MUAPI video boundary) -- before any ledger reservation or state
    transition. Domains that do not opt in are unaffected."""


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
    production_plan_content_hash: str,
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

    production_plan_content_hash is REQUIRED (Phase 2 plan-bound
    authorization): the 64-hex-char content_hash of the exact verified
    persisted Production Plan this grant authorizes. It is part of the
    signed grant checksum -- the offline approver's signature therefore
    covers the plan identity -- and every child authorization minted
    from this grant must carry a matching envelope plan checksum. There
    is no unbound-grant registration path any more.
    """
    if not isinstance(production_plan_content_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", production_plan_content_hash
    ):
        raise AuthorizationAuthorityError(
            f"production_plan_content_hash must be a 64-hex-char Production Plan "
            f"content_hash, got {production_plan_content_hash!r} -- refusing to "
            "register an unbound grant"
        )
    grant_checksum = compute_grant_checksum(
        channel_id=channel_id, episode_id=episode_id, workflow_id=workflow_id,
        workflow_version=workflow_version, workflow_checksum=workflow_checksum,
        production_plan_content_hash=production_plan_content_hash,
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
                workflow_version=workflow_version, workflow_checksum=workflow_checksum,
                production_plan_content_hash=production_plan_content_hash, bindings=bindings,
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
    production_plan_content_hash: Optional[str],
    bindings: Sequence[store.GrantBinding], max_total_spend_all_bindings, currency, expires_at,
    grant_kind: str = "run",
    channel_package_version: Optional[str] = None, channel_package_checksum: Optional[str] = None,
    allowed_provider_domains: Optional[Sequence[str]] = None, cadence_snapshot: Optional[str] = None,
    weekly_exposure_ceiling_usd=None, valid_from: Optional[str] = None,
    operating_period_ref: Optional[str] = None,
) -> str:
    """production_plan_content_hash participates in the checksum payload
    ONLY when non-None -- historical-compatibility contract: grants
    registered before Phase 2 plan binding existed have stored
    grant_checksums / artifact subject_checksums computed over payloads
    WITHOUT this key. Omitting the key for None makes the recomputed
    expected checksum for such a legacy row byte-identical to its
    original, so resolve_active_grant() keeps resolving it exactly as it
    did before the plan-binding change (no global invalidation, no
    forced re-registration). Registration (register_run_grant) always
    supplies a validated non-None hash, so every NEW grant's signature
    covers its exact Production Plan identity."""
    from hypogeum.approval_artifact import compute_approval_subject_checksum
    payload = {
        "channel_id": channel_id, "episode_id": episode_id, "workflow_id": workflow_id,
        "workflow_version": workflow_version, "workflow_checksum": workflow_checksum,
        "bindings": [b.to_dict() for b in bindings],
        "max_total_spend_all_bindings": None if max_total_spend_all_bindings is None else str(max_total_spend_all_bindings),
        "currency": currency, "expires_at": expires_at,
    }
    if production_plan_content_hash is not None:
        payload["production_plan_content_hash"] = production_plan_content_hash
    # Channel Operating Grant (ADR-016): the channel-package / cadence /
    # provider-domain / weekly-ceiling identity is part of the signed
    # payload ONLY for grant_kind='channel_operating'. Omitting the keys
    # for the default 'run' kind keeps every historical grant's recomputed
    # checksum byte-identical to its stored one.
    if grant_kind == "channel_operating":
        payload["grant_kind"] = "channel_operating"
        payload["channel_package_version"] = channel_package_version
        payload["channel_package_checksum"] = channel_package_checksum
        payload["allowed_provider_domains"] = (
            sorted(allowed_provider_domains) if allowed_provider_domains is not None else None
        )
        payload["cadence_snapshot"] = cadence_snapshot
        payload["weekly_exposure_ceiling_usd"] = (
            None if weekly_exposure_ceiling_usd is None else str(weekly_exposure_ceiling_usd)
        )
        payload["valid_from"] = valid_from
        payload["operating_period_ref"] = operating_period_ref
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
            production_plan_content_hash=getattr(grant, "production_plan_content_hash", None),
            bindings=grant.bindings, max_total_spend_all_bindings=grant.max_total_spend_all_bindings,
            currency=grant.currency, expires_at=grant.expires_at,
        )
        if artifact.subject_checksum != expected_checksum or grant.grant_checksum != expected_checksum:
            return None
        return grant
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Channel Operating Grant -- ADR-016
#
# One offline Ed25519 signature authorizes many autonomous episodes for
# exactly one channel over a bounded operating period. It creates NO
# budget capacity: the weekly ledger, the PRE_PLAN reservation, the
# atomic plan upgrade, and the plan-bound reservation remain mandatory
# and authoritative. The Operating Grant is a channel/workflow/domain/
# ceiling gate that the runtime resolves (public key only) and from which
# it mints narrowly scoped per-envelope child authorizations in-process.
# The runtime holds no signing key and cannot broaden or re-sign it.
# ---------------------------------------------------------------------------

CHANNEL_OPERATING_EPISODE_SENTINEL = "*"


class OperatingGrantNotYetValidError(AuthorizationAuthorityError):
    """`at` precedes the operating grant's valid_from."""


def register_channel_operating_grant(
    *,
    grant_id: str,
    grant_version: int,
    channel_id: str,
    workflow_id: str,
    workflow_version: str,
    workflow_checksum: str,
    channel_package_version: str,
    channel_package_checksum: str,
    allowed_provider_domains: Sequence[str],
    cadence_snapshot: str,
    weekly_exposure_ceiling_usd: Decimal,
    bindings: Sequence[store.GrantBinding],
    currency: str,
    valid_from: str,
    expires_at: str,
    operating_period_ref: str,
    approval_artifact: ApprovalArtifact,
    db_path: Optional[Path] = None,
) -> store.RunAuthorizationGrant:
    """Register a Channel Operating Grant -- ONLY if approval_artifact
    verifies, was signed with approval_type="channel_operating_grant",
    and its subject_checksum matches this exact grant payload. Same
    offline-signing / public-key-verification model as
    register_run_grant(): no in-process caller can construct a valid
    artifact.

    weekly_exposure_ceiling_usd is a CEILING, not capacity. It records the
    operator-configured weekly budget limit that is effective for this
    grant's operating period (an operator-controlled, versioned Channel
    policy value -- NOT an architectural constant). It is stored and
    signed; the authoritative weekly gate stays the weekly-budget ledger.
    Runtime admission rejects the grant if this value no longer equals the
    operator's currently-configured weekly limit (the operator must
    re-sign the grant when the policy changes).
    """
    if currency != "USD":
        raise AuthorizationAuthorityError("Channel Operating Grant currency must be USD")
    ceiling = Decimal(str(weekly_exposure_ceiling_usd))
    grant_checksum = compute_grant_checksum(
        channel_id=channel_id, episode_id=CHANNEL_OPERATING_EPISODE_SENTINEL, workflow_id=workflow_id,
        workflow_version=workflow_version, workflow_checksum=workflow_checksum,
        production_plan_content_hash=None,
        bindings=bindings, max_total_spend_all_bindings=ceiling, currency=currency,
        expires_at=expires_at,
        grant_kind="channel_operating",
        channel_package_version=channel_package_version,
        channel_package_checksum=channel_package_checksum,
        allowed_provider_domains=list(allowed_provider_domains),
        cadence_snapshot=cadence_snapshot,
        weekly_exposure_ceiling_usd=ceiling,
        valid_from=valid_from,
        operating_period_ref=operating_period_ref,
    )
    _require_verified(
        approval_artifact, approval_type="channel_operating_grant", subject_checksum=grant_checksum,
    )

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
                channel_id=channel_id, episode_id=CHANNEL_OPERATING_EPISODE_SENTINEL, workflow_id=workflow_id,
                workflow_version=workflow_version, workflow_checksum=workflow_checksum,
                production_plan_content_hash="", bindings=bindings,
                max_total_spend_all_bindings=ceiling, currency=currency, expires_at=expires_at,
                approval_id=approval_artifact.approval_id, approval_version=approval_artifact.approval_version,
                grant_kind="channel_operating",
                channel_package_version=channel_package_version,
                channel_package_checksum=channel_package_checksum,
                allowed_provider_domains=list(allowed_provider_domains),
                cadence_snapshot=cadence_snapshot,
                weekly_exposure_ceiling_usd=ceiling,
                valid_from=valid_from,
                operating_period_ref=operating_period_ref,
            )
            conn.commit()
            return grant
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def _recompute_operating_grant_checksum(grant: "store.RunAuthorizationGrant") -> str:
    return compute_grant_checksum(
        channel_id=grant.channel_id, episode_id=grant.episode_id, workflow_id=grant.workflow_id,
        workflow_version=grant.workflow_version, workflow_checksum=grant.workflow_checksum,
        production_plan_content_hash=None,
        bindings=grant.bindings, max_total_spend_all_bindings=grant.max_total_spend_all_bindings,
        currency=grant.currency, expires_at=grant.expires_at,
        grant_kind="channel_operating",
        channel_package_version=grant.channel_package_version,
        channel_package_checksum=grant.channel_package_checksum,
        allowed_provider_domains=grant.allowed_provider_domains,
        cadence_snapshot=grant.cadence_snapshot,
        weekly_exposure_ceiling_usd=grant.weekly_exposure_ceiling_usd,
        valid_from=grant.valid_from,
        operating_period_ref=grant.operating_period_ref,
    )


def resolve_active_operating_grant(
    channel_id: str, *, at: str, db_path: Optional[Path] = None,
) -> Optional[store.RunAuthorizationGrant]:
    """Read-only. Returns the one active, in-window, signature-verified
    Channel Operating Grant for channel_id, or None (never raises) if
    there is none, it is revoked, expired, not yet valid, or its stored
    ApprovalArtifact no longer verifies against the configured public
    key. A grant row inserted by any means other than
    register_channel_operating_grant()'s verified path fails this check
    every time it is read."""
    conn = get_connection(db_path)
    try:
        grant = store.find_active_operating_grant_for_channel(conn, channel_id)
        if grant is None:
            return None
        if grant.valid_from and at < grant.valid_from:
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
        expected_checksum = _recompute_operating_grant_checksum(grant)
        if artifact.subject_checksum != expected_checksum or grant.grant_checksum != expected_checksum:
            return None
        if artifact.approval_type != "channel_operating_grant":
            return None
        return grant
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# issue_child_authorization() -- Correction 3/4
# ---------------------------------------------------------------------------

def _resolve_and_verify_active_grant(
    conn: sqlite3.Connection, grant_id: str, grant_version: int, at: str,
) -> "store.RunAuthorizationGrant":
    """The shared lookup+status+expiry+artifact-verification block every
    grant-consuming issuance path (issue_child_authorization() and
    activate_authorization_proposal()) requires identically. Fails
    closed (GrantNotFoundError/GrantRevokedError/GrantExpiredError/
    GrantUnverifiableError) before any ledger or AuthorizationRecord
    mutation -- a missing, revoked, expired, or unverifiable grant
    never reaches capacity-checking or reservation, let alone
    activation."""
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
    return grant


def _validate_grant_scope_binding(
    grant: "store.RunAuthorizationGrant", *,
    channel_id: Optional[str], episode_id: Optional[str], workflow_version: Optional[str],
    workflow_id: Optional[str] = None, workflow_checksum: Optional[str] = None,
    target_description: str,
) -> None:
    """The ONE shared fail-closed channel/episode/run(workflow)-scope
    check both issue_child_authorization() and
    activate_authorization_proposal() call -- never duplicated into two
    divergent implementations. Requires EXACT equality between the
    grant's own channel_id/episode_id/workflow_version and the
    authorization target's (an envelope's or a proposal's) declared
    scope, before any ledger reservation or authorization state
    transition. Missing target scope evidence (None or empty) fails
    closed exactly like a genuine mismatch -- it is never treated as
    "not applicable, skip the check."

    workflow_id/workflow_checksum are compared ONLY when the caller
    actually supplies a non-None value for them -- i.e. only when the
    target's own data model genuinely exposes that identity (an
    AuthorizationProposal carries workflow_id/workflow_checksum; a bare
    GenerationExecutionEnvelope does not carry workflow_id at all, by
    design of that schema, not by omission) -- "wherever both sides
    expose that identity." workflow_version IS always present on both
    an envelope and a proposal, and is therefore always required and
    compared, exactly like channel_id/episode_id.

    Never mutates the grant or the target to make them match (there is
    no write in this function at all); never a bypass or fallback --
    every branch below either passes silently or raises
    GrantScopeMismatchError.
    """
    if not channel_id:
        raise GrantScopeMismatchError(f"{target_description} is missing channel_id -- cannot verify grant scope")
    if channel_id != grant.channel_id:
        raise GrantScopeMismatchError(
            f"{target_description}'s channel_id {channel_id!r} does not match grant "
            f"{grant.grant_id!r}@{grant.grant_version}'s channel_id {grant.channel_id!r}"
        )

    if not episode_id:
        raise GrantScopeMismatchError(f"{target_description} is missing episode_id -- cannot verify grant scope")
    # A Channel Operating Grant (ADR-016) is channel-scoped, not
    # episode-scoped: episode_id is the '*' sentinel and authorizes any
    # episode of this channel. The target must still DECLARE its own
    # episode identity (checked above) -- the operating grant just does
    # not pin it. A per-episode 'run' grant still requires exact equality.
    if getattr(grant, "grant_kind", "run") == "channel_operating":
        if grant.episode_id != CHANNEL_OPERATING_EPISODE_SENTINEL:
            raise GrantScopeMismatchError(
                f"channel_operating grant {grant.grant_id!r}@{grant.grant_version} has a "
                f"non-sentinel episode_id {grant.episode_id!r}"
            )
    elif episode_id != grant.episode_id:
        raise GrantScopeMismatchError(
            f"{target_description}'s episode_id {episode_id!r} does not match grant "
            f"{grant.grant_id!r}@{grant.grant_version}'s episode_id {grant.episode_id!r}"
        )

    if not workflow_version:
        raise GrantScopeMismatchError(f"{target_description} is missing workflow_version -- cannot verify grant scope")
    if workflow_version != grant.workflow_version:
        raise GrantScopeMismatchError(
            f"{target_description}'s workflow_version {workflow_version!r} does not match grant "
            f"{grant.grant_id!r}@{grant.grant_version}'s workflow_version {grant.workflow_version!r}"
        )

    if workflow_id is not None and workflow_id != grant.workflow_id:
        raise GrantScopeMismatchError(
            f"{target_description}'s workflow_id {workflow_id!r} does not match grant "
            f"{grant.grant_id!r}@{grant.grant_version}'s workflow_id {grant.workflow_id!r}"
        )
    if workflow_checksum is not None and workflow_checksum != grant.workflow_checksum:
        raise GrantScopeMismatchError(
            f"{target_description}'s workflow_checksum {workflow_checksum!r} does not match grant "
            f"{grant.grant_id!r}@{grant.grant_version}'s workflow_checksum {grant.workflow_checksum!r}"
        )


def _validate_envelope_grant_scope(
    grant: "store.RunAuthorizationGrant", envelope: GenerationExecutionEnvelope, *, target_description: str,
) -> None:
    """Thin adapter: extracts an envelope's own scope fields and calls
    the one shared _validate_grant_scope_binding(). envelope.
    episode_level_budget_ref is the envelope-side field carrying the
    same episode identity as RunAuthorizationGrant.episode_id /
    AuthorizationProposal.episode_id -- every real compiler (tools.
    migrate_legacy_manifest_to_envelopes, scripts.propose_current_
    episode_authorization) sources both from the exact same episode_id
    value. workflow_id/workflow_checksum are never passed here -- the
    envelope schema does not carry them at all."""
    _validate_grant_scope_binding(
        grant, channel_id=envelope.channel_id, episode_id=envelope.episode_level_budget_ref,
        workflow_version=envelope.workflow_version, target_description=target_description,
    )


def _validate_expected_plan_hash(
    grant: "store.RunAuthorizationGrant", envelope: GenerationExecutionEnvelope,
    expected_production_plan_content_hash: str,
) -> None:
    """Phase 2 plan-bound authorization for callers that DECLARE a
    verified Production Plan identity (today: the paid MUAPI video
    boundary, which passes the exact hash of the integrity-verified
    persisted plan it just loaded). Enforces three-way equality BEFORE
    any ledger reservation or state transition:

        grant.production_plan_content_hash ==
        expected_production_plan_content_hash ==
        envelope.dependency_checksums.plan

    A legacy NULL-hash grant fails here (denied for plan-bound
    consumption) while remaining fully usable for domains that do not
    opt in -- this function runs ONLY when the caller supplies an
    expected hash. No mutation, no fallback: pass silently or raise
    GrantPlanHashMismatchError."""
    grant_plan_hash = getattr(grant, "production_plan_content_hash", None)
    if not grant_plan_hash:
        raise GrantPlanHashMismatchError(
            f"grant {grant.grant_id!r}@{grant.grant_version} carries no "
            "production_plan_content_hash -- grants registered before plan binding "
            "are denied for plan-bound authorization; re-register the grant against "
            "the verified persisted plan"
        )
    if grant_plan_hash != expected_production_plan_content_hash:
        raise GrantPlanHashMismatchError(
            f"grant {grant.grant_id!r}@{grant.grant_version} is bound to content_hash "
            f"{grant_plan_hash!r} != caller-supplied verified plan hash "
            f"{expected_production_plan_content_hash!r} -- this grant authorizes a "
            "different Production Plan"
        )
    envelope_plan = envelope.dependency_checksums.plan
    if envelope_plan != expected_production_plan_content_hash:
        raise GrantPlanHashMismatchError(
            f"envelope {envelope.envelope_id!r} plan checksum {envelope_plan!r} != "
            f"caller-supplied verified plan hash "
            f"{expected_production_plan_content_hash!r}"
        )


def _validate_envelope_against_grant_binding(
    grant: "store.RunAuthorizationGrant", envelope: GenerationExecutionEnvelope,
) -> "store.GrantBinding":
    """The shared per-envelope binding-match + limits check. Fails
    closed (NoMatchingGrantBindingError/EnvelopeExceedsGrantLimitsError)
    -- a mismatched provider/model/operation, an over-ceiling cost, a
    currency mismatch, or an unauthorized retry/fallback request is
    never silently accepted."""
    binding = grant.find_binding(envelope.provider, envelope.model, envelope.provider_operation)
    if binding is None:
        raise NoMatchingGrantBindingError(
            f"grant {grant.grant_id!r}@{grant.grant_version} has no binding for "
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
    return binding


def _reserve_grant_ledger_capacity(
    conn: sqlite3.Connection, grant: "store.RunAuthorizationGrant", grant_version: int,
    binding: "store.GrantBinding", envelope: GenerationExecutionEnvelope, at: str,
) -> "store.LedgerReservation":
    """The shared cumulative-capacity check (binding-level calls/spend,
    then grant-wide spend) plus the atomic ledger insert itself. Reads
    hypogeum.authorization_store.sum_active_ledger_consumption() (and,
    for a grant-wide ceiling, every OTHER binding's own consumption too)
    fresh from the SAME open transaction/connection this call runs
    inside -- so sequential calls within one batch (e.g.
    activate_authorization_proposal()'s own loop, still inside its one
    BEGIN IMMEDIATE) each see every reservation already inserted earlier
    in that same batch, which is exactly what prevents the batch itself
    from oversubscribing the grant. Concurrent callers on DIFFERENT
    connections are serialized by BEGIN IMMEDIATE's own write-lock
    semantics -- see tests/test_grant_binding_hardening.py::
    TestConcurrentActivationCannotOversubscribe."""
    grant_id = grant.grant_id
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

    reservation_id = f"{grant_id}:{grant_version}:{binding.binding_key()}:{envelope.idempotency_key}"
    return store.insert_ledger_reservation(
        conn, reservation_id=reservation_id, grant_id=grant_id, grant_version=grant_version,
        provider=envelope.provider, model=envelope.model, provider_operation=envelope.provider_operation,
        idempotency_key=envelope.idempotency_key, envelope_id=envelope.envelope_id,
        authorization_id=envelope.authorization_id, reserved_amount=envelope.max_cost_per_call, at=at,
    )


def issue_child_authorization(
    envelope: GenerationExecutionEnvelope, grant_id: str, grant_version: int, *,
    at: str, db_path: Optional[Path] = None,
    expected_production_plan_content_hash: Optional[str] = None,
) -> AuthorizationRecord:
    """
    See plan §5.5 for the full algorithm. Uses envelope.idempotency_key
    (already a required envelope field) as the ledger's idempotency
    key -- no separate, potentially-conflicting parameter is accepted.

    Before any ledger reservation, requires the envelope's own
    channel_id/episode_level_budget_ref/workflow_version to exactly
    equal the grant's own channel_id/episode_id/workflow_version (see
    _validate_envelope_grant_scope() / _validate_grant_scope_binding()
    -- the one shared check activate_authorization_proposal() also
    uses). A grant registered for a different channel, episode, or
    workflow fails closed (GrantScopeMismatchError) even if every
    provider/model/operation/currency/cost binding matches.

    expected_production_plan_content_hash -- Phase 2 plan-bound
    authorization, OPT-IN per caller. Only a caller that has IN HAND
    the exact hash of an integrity-verified persisted Production Plan
    (today: hypogeum.muapi._submit_scene_via_gate(), the paid video
    boundary) supplies it; when supplied,
    _validate_expected_plan_hash() requires grant hash == supplied
    verified hash == envelope dependency_checksums.plan before any
    ledger activity. When NOT supplied (every other gated domain),
    pre-Phase-2 behavior is preserved exactly -- including full
    compatibility with historical NULL-hash grants.
    """
    conn = get_connection(db_path)
    try:
        store.ensure_tables(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            grant = _resolve_and_verify_active_grant(conn, grant_id, grant_version, at)
            _validate_envelope_grant_scope(
                grant, envelope, target_description=f"envelope {envelope.envelope_id!r}",
            )
            # Phase 2 plan-bound authorization -- OPT-IN, video-only: the
            # paid MUAPI video caller supplies the verified persisted
            # Production Plan hash it loaded; the authority independently
            # re-checks grant == expected and envelope.plan == expected
            # here. Callers that do not supply an expected hash (every
            # other gated domain) get exactly their pre-Phase-2 behavior.
            # Either way this runs pre-ledger, pre-state-transition: a
            # denial leaves zero consumption rows and an untouched record.
            if expected_production_plan_content_hash is not None:
                if getattr(grant, "grant_kind", "run") == "channel_operating":
                    # A Channel Operating Grant carries no plan hash (it
                    # precedes every episode's plan). The plan binding is
                    # still enforced: the envelope's own
                    # dependency_checksums.plan MUST equal the caller's
                    # freshly-loaded canonical Production Plan hash, and
                    # hypogeum.capability_boundary independently re-checks
                    # the envelope binding against the canonical plan and
                    # the plan-bound weekly reservation before any I/O.
                    envelope_plan = envelope.dependency_checksums.plan
                    if envelope_plan != expected_production_plan_content_hash:
                        raise GrantPlanHashMismatchError(
                            f"envelope {envelope.envelope_id!r} plan checksum {envelope_plan!r} != "
                            f"caller-supplied verified canonical plan hash "
                            f"{expected_production_plan_content_hash!r}"
                        )
                else:
                    _validate_expected_plan_hash(
                        grant, envelope, expected_production_plan_content_hash,
                    )
            binding = _validate_envelope_against_grant_binding(grant, envelope)
            reservation = _reserve_grant_ledger_capacity(conn, grant, grant_version, binding, envelope, at)

            # AuthorizationRecord.authorization_id must be exactly
            # envelope.authorization_id -- an envelope carries its own
            # authorization identity from compile time
            # (hypogeum.submission_gate._validate_authorization()
            # requires the two to match exactly), never a value this
            # module invents independently.
            authorization_id = envelope.authorization_id

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
    proposal_checksum: str, *, approval_artifact: ApprovalArtifact, grant_id: str, grant_version: int,
    at: str, db_path: Optional[Path] = None,
) -> "list[AuthorizationRecord]":
    """
    Generic, atomic, idempotent bulk activation of an already-compiled,
    already-proposed batch (hypogeum.authorization_store.
    AuthorizationProposal, produced by scripts.propose_current_episode_
    authorization) from AWAITING_EXPLICIT_AUTHORIZATION to AUTHORIZED,
    given one verified ApprovalArtifact bound to the proposal's own
    checksum, structurally bound to a valid, verified RunAuthorizationGrant.

    grant_id/grant_version are REQUIRED, never optional or defaulted --
    every proposal-activated paid authorization must be bound to a
    grant (closes the real, documented gap this function previously
    had: see git history for the prior version's own docstring, and
    tests/test_grant_not_consumed_by_proposal_activation.py, which now
    additionally proves the OLD, ungapped shape can no longer be
    constructed). Uses the exact same shared, already-tested grant
    resolution/validation/reservation helpers issue_child_authorization()
    uses (_resolve_and_verify_active_grant(),
    _validate_grant_scope_binding() / _validate_envelope_grant_scope(),
    _validate_envelope_against_grant_binding(),
    _reserve_grant_ledger_capacity()) -- a missing, revoked, expired, or
    unverifiable grant; a channel/episode/workflow scope mismatch
    (checked BOTH against the proposal's own declared scope AND
    independently against every named envelope's own scope, so a
    mixed-channel/mixed-episode/mixed-run batch fails closed even when
    the proposal's own metadata happens to match); a missing/mismatched
    provider/model/operation/currency/cost binding; or exhausted
    binding/grant-wide capacity all fail closed, before mutating
    anything, with zero adapter calls (this function never touches
    submission_gate at all).

    The missing counterpart to issue_authorization_from_explicit_
    approval(): that function only ever constructs a BRAND NEW record
    when none exists yet at its authorization_id, and silently returns
    an EXISTING record unchanged otherwise -- structurally unable to
    activate a batch that has already reached AWAITING_EXPLICIT_
    AUTHORIZATION. This function's target state transition
    (AWAITING_EXPLICIT_AUTHORIZATION -> AUTHORIZED) and its required
    evidence are unchanged from authorization_lifecycle.py's own
    existing, already-defined edge -- no new lifecycle state or
    transition is introduced. The AUTHORIZED-transition evidence now
    ALSO carries parent_grant_id/parent_grant_version/
    parent_grant_checksum/grant_reservation_id, exactly matching
    issue_child_authorization()'s own evidence shape -- this is what
    makes hypogeum.submission_gate._grant_binding_evidence() (and the
    gate's existing, unchanged Step 8.5 boundary revalidation) apply to
    every record this function now produces, with zero changes to
    submission_gate.py itself.

    Fails closed, before mutating anything, if: no AuthorizationProposal
    exists with this exact proposal_checksum; any one of the proposal's
    own named authorization_ids is missing; or any one is in a state
    other than AWAITING_EXPLICIT_AUTHORIZATION or (for idempotent
    replay only) already AUTHORIZED under this exact approval_id AND
    this exact grant_id/grant_version. Validates the ENTIRE named set
    (grant resolution, every envelope's binding match, and every
    envelope's ledger capacity, checked cumulatively across the whole
    batch within this one open transaction) before transitioning ANY of
    them -- all-or-nothing, one BEGIN IMMEDIATE transaction.

    Idempotent replay: if every named authorization_id is already
    AUTHORIZED with approval_artifact_id == this artifact's own
    approval_id AND parent_grant_id/parent_grant_version == the ones
    supplied now, returns the existing records unchanged -- no
    re-transition, no duplicate approval_artifacts row, no duplicate
    ledger reservation. A record already AUTHORIZED under a DIFFERENT
    approval_id OR a DIFFERENT grant fails closed (never silently
    re-approves under a different signature or grant).
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

            # Grant resolved and verified BEFORE any per-record work --
            # a missing/revoked/expired/unverifiable grant blocks the
            # entire batch with zero mutation, exactly like a missing
            # proposal or a missing authorization_id already did.
            grant = _resolve_and_verify_active_grant(conn, grant_id, grant_version, at)

            # Proposal-level scope check, BEFORE reading a single named
            # record -- catches "this grant belongs to a different
            # channel/episode/workflow entirely" with zero mutation and
            # zero per-record reads. Checked against the proposal's own
            # declared channel_id/episode_id/workflow_id/workflow_
            # version/workflow_checksum -- the fullest scope identity
            # available on either side of this function (a bare
            # envelope never carries workflow_id).
            _validate_grant_scope_binding(
                grant, channel_id=proposal.channel_id, episode_id=proposal.episode_id,
                workflow_version=proposal.workflow_version, workflow_id=proposal.workflow_id,
                workflow_checksum=proposal.workflow_checksum,
                target_description=f"proposal {proposal.proposal_id!r}",
            )

            records: dict = {}
            envelopes: dict = {}
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
                    if (
                        approved_evidence.get("parent_grant_id") != grant_id
                        or approved_evidence.get("parent_grant_version") != grant_version
                    ):
                        raise store.ProposalReconciliationConflictError(
                            f"authorization {authorization_id!r} is already AUTHORIZED under a DIFFERENT grant "
                            f"({approved_evidence.get('parent_grant_id')!r}@{approved_evidence.get('parent_grant_version')!r}), "
                            f"cannot re-activate under {grant_id!r}@{grant_version!r}"
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
                envelopes[authorization_id] = store.get_execution_envelope(conn, record.envelope_id, record.envelope_version)

            if already_authorized_under_this_approval:
                conn.commit()
                return [records[aid] for aid in proposal.authorization_ids]

            # Validate every envelope's own channel/episode/workflow
            # scope AND grant binding BEFORE reserving anything -- a
            # single mismatched envelope anywhere in the batch blocks
            # the whole batch, never a partial activation. This is a
            # SEPARATE check from the proposal-level one above: even if
            # the proposal's own declared metadata matches the grant, an
            # individual envelope within it could still diverge (a
            # mixed-channel/mixed-episode/mixed-run batch) -- every
            # envelope is checked independently, never inferred safe
            # from the proposal's own scope alone.
            for aid, env in envelopes.items():
                _validate_envelope_grant_scope(grant, env, target_description=f"envelope {env.envelope_id!r} ({aid!r})")
            bindings = {
                aid: _validate_envelope_against_grant_binding(grant, env) for aid, env in envelopes.items()
            }

            # Reserve ledger capacity for every envelope, sequentially,
            # inside this same open transaction -- each reservation is
            # immediately visible to sum_active_ledger_consumption() for
            # every subsequent envelope in this same batch (Correction:
            # this is what prevents the batch itself from oversubscribing
            # the grant; a genuinely concurrent activation on a DIFFERENT
            # connection is serialized by BEGIN IMMEDIATE's write lock).
            reservations = {
                aid: _reserve_grant_ledger_capacity(conn, grant, grant_version, bindings[aid], env, at)
                for aid, env in envelopes.items()
            }

            store.persist_approval_artifact(conn, approval_artifact, consumed_by_authorization_id=proposal.proposal_id)

            activated = []
            for authorization_id in proposal.authorization_ids:
                record = records[authorization_id]
                if record.current_state != AuthorizationState.AUTHORIZED:
                    reservation = reservations[authorization_id]
                    record = record.transition(
                        AuthorizationState.AUTHORIZED,
                        evidence={
                            "authorized_by": approval_artifact.approver_identity,
                            "authorization_method": "explicit_human_approval_proposal_batch",
                            "approval_artifact_id": approval_artifact.approval_id,
                            "approval_subject_checksum": approval_artifact.subject_checksum,
                            "parent_grant_id": grant_id,
                            "parent_grant_version": grant_version,
                            "parent_grant_checksum": grant.grant_checksum,
                            "grant_reservation_id": reservation.reservation_id,
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
