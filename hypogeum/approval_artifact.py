"""
hypogeum/approval_artifact.py — Cryptographically verified approval
artifact (Factory Core).

An ApprovalArtifact is the only thing hypogeum.authorization_authority
accepts as proof a human approved a RunAuthorizationGrant, an
authorization proposal, or an enforcement cutover. It is signed
offline (Ed25519) by a private key that never runs inside the
pipeline/worker process -- production runtime code holds only the
PUBLIC key (see hypogeum.config.Config.AUTHORIZATION_APPROVAL_PUBLIC_KEY),
sufficient to verify a signature but structurally incapable of forging
one. Free-text-looking fields such as approver_identity exist only as
signed CONTENT, never as unsigned proof by themselves: an artifact
with any field altered after signing fails verification, because the
signature no longer matches the recomputed canonical bytes.

sign_approval_artifact() is called exclusively by the offline approval
scripts (scripts/approve_run_grant.py, scripts/approve_authorization_
proposal.py) -- no production runtime module imports it. Production
code (hypogeum.authorization_authority) calls only
verify_approval_artifact().

Zero domain content: works identically for any channel_id/scope_id/
workflow/provider-model-operation binding set.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

__all__ = [
    "APPROVAL_TYPES",
    "ApprovalArtifact",
    "GrantBindingApproval",
    "ApprovalArtifactError",
    "sign_approval_artifact",
    "verify_approval_artifact",
    "compute_approval_subject_checksum",
]

APPROVAL_TYPES = frozenset({"grant", "authorization_proposal", "enforcement_cutover"})


class ApprovalArtifactError(Exception):
    """Raised when an artifact cannot even be constructed (malformed
    approval_type, etc.) -- distinct from a failed verification, which
    verify_approval_artifact() reports as False, never as an
    exception, so callers cannot accidentally treat a raised-and-
    caught verification failure as anything other than a denial."""


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
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


@dataclass(frozen=True)
class GrantBindingApproval:
    """One (provider, model, provider_operation) triple an approval
    covers -- mirrors hypogeum.adapter_registry's own keying."""

    provider: str
    model: str
    provider_operation: str


@dataclass(frozen=True)
class ApprovalArtifact:
    """Immutable, integrity-protected proof of human approval. Every
    field except `signature` is covered by the signature -- there is
    no unsigned field verification ever relies on for its own sake."""

    approval_id: str
    approval_version: int
    approval_type: str  # "grant" | "authorization_proposal" | "enforcement_cutover"
    subject_checksum: str  # exact checksum of the grant/proposal payload this artifact approves
    channel_id: str
    scope_id: str  # episode_id or execution_id, per approval_type
    workflow_id: str
    workflow_version: str
    workflow_checksum: str
    channel_package_version: str
    channel_package_checksum: str
    allowed_bindings: Sequence[GrantBindingApproval]
    currency: str
    max_cost_per_call: Decimal
    max_total_spend: Decimal
    max_calls: int
    retry_authorized: bool
    fallback_authorized: bool
    issued_at: str
    expires_at: str
    approver_identity: str
    nonce: str
    signature: bytes

    def _identity_payload(self) -> dict:
        payload = {f.name: getattr(self, f.name) for f in fields(self)}
        payload.pop("signature", None)
        return payload

    def canonical_bytes(self) -> bytes:
        """The exact bytes the signature is computed over -- every
        field except `signature` itself."""
        return _canonical_bytes(self._identity_payload())


def sign_approval_artifact(
    unsigned_fields: Mapping[str, Any], *, private_key: Ed25519PrivateKey,
) -> ApprovalArtifact:
    """The only place a signature is ever produced. Called exclusively
    by the offline approval scripts -- never imported by production
    runtime code, since production code never holds an Ed25519 private
    key at all."""
    if unsigned_fields.get("approval_type") not in APPROVAL_TYPES:
        raise ApprovalArtifactError(
            f"approval_type must be one of {sorted(APPROVAL_TYPES)}, got {unsigned_fields.get('approval_type')!r}"
        )
    draft = ApprovalArtifact(signature=b"", **{k: v for k, v in unsigned_fields.items() if k != "signature"})
    signature = private_key.sign(draft.canonical_bytes())
    return dataclasses.replace(draft, signature=signature)


def verify_approval_artifact(artifact: ApprovalArtifact, *, public_key: Ed25519PublicKey) -> bool:
    """Recomputes the canonical bytes of every field except `signature`
    and verifies the Ed25519 signature against them using only the
    PUBLIC key. Returns False (never raises) for any malformed,
    tampered, or wrong-keypair artifact -- hypogeum.authorization_
    authority treats False as a fail-closed denial, never as
    "unverifiable but maybe okay"."""
    try:
        public_key.verify(artifact.signature, artifact.canonical_bytes())
        return True
    except InvalidSignature:
        return False
    except (TypeError, ValueError):
        # A malformed signature (wrong length/type) raises before the
        # cryptographic check even runs -- still a verification
        # failure, never a crash a caller has to catch separately.
        return False


def compute_approval_subject_checksum(payload: Mapping[str, Any]) -> str:
    """sha256 of the canonical bytes of a grant/proposal payload --
    what an ApprovalArtifact's subject_checksum must exactly equal for
    the artifact to apply to that specific payload and no other."""
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()
