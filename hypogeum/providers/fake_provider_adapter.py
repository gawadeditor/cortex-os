"""
hypogeum/providers/fake_provider_adapter.py — The one sanctioned
provider-adapter test double (Factory Core)

Implements Commit 3 / Part 4 and Part 5 of docs/architecture/
AUTHORIZED_GENERATION_ENVELOPE_IMPLEMENTATION_BLUEPRINT.md.

Structurally incapable of network access: this module imports nothing
network-capable (no requests/httpx/socket/urllib.request), so it is not
merely "unused" in tests -- it could not reach a network even if a test
tried to make it. This is the ONLY provider adapter this commit
implements or wires anywhere; hypogeum/submission_gate.py is the only
caller.

An adapter never receives a raw prompt, a bare envelope, or arbitrary
caller input -- it receives exactly one thing: an
AdapterInvocationCapability, a short-lived, integrity-checksummed,
single-use token that hypogeum.submission_gate mints only after every
gate check has already passed. See AdapterInvocationCapability's
docstring for the exact binding (Part 5 requirement 2).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional, Sequence

__all__ = [
    "AdapterInvocationCapability",
    "AdapterResult",
    "FakeProviderAdapter",
    "FakeProviderAdapterError",
    "RawPromptRejectedError",
    "InvalidCapabilityError",
    "CapabilityAlreadyConsumedError",
    "compute_capability_checksum",
]


class FakeProviderAdapterError(Exception):
    """Base class for every fake-adapter failure."""


class RawPromptRejectedError(FakeProviderAdapterError):
    """Raised when invoke() is given anything other than an
    AdapterInvocationCapability -- a raw string prompt, a bare envelope,
    or any other unvalidated input. There is no code path in this module
    that can turn such input into an executable request (Part 5
    requirement 6)."""


class InvalidCapabilityError(FakeProviderAdapterError):
    """Raised when a capability's own checksum does not match its
    recomputed identity -- tampered, malformed, or hand-constructed
    outside the gate."""


class CapabilityAlreadyConsumedError(FakeProviderAdapterError):
    """Raised when a capability_id has already been used for one
    invocation -- a capability is single-use (Part 5 requirement 4)."""

    def __init__(self, capability_id: str):
        self.capability_id = capability_id
        super().__init__(f"capability {capability_id!r} has already been consumed")


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if is_dataclass(value):
        return {f.name: _to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


@dataclass(frozen=True)
class AdapterInvocationCapability:
    """
    The short-lived, gate-issued, integrity-protected token an adapter
    accepts (Part 5 requirement 2). Bound to exact:
      - envelope_id / envelope_version
      - attempt_id / idempotency_key
      - lease_owner_id / lease_fencing_token
      - provider / model / provider_operation
      - operation_payload_bytes / operation_payload_sha256 / operation_payload_schema_version
      - max_cost_per_call / currency
      - budget_reservation_ids
      - issued_at / expires_at

    Immutable (frozen dataclass) and integrity-protected: capability_checksum
    is a sha256 of every other field's canonical JSON, computed by
    compute_capability_checksum() and verified by FakeProviderAdapter.
    invoke() before anything else happens. A capability object with a
    checksum that doesn't match its own other fields (hand-constructed,
    or a genuine one with a field swapped) is rejected -- this is what
    makes "cannot be applied to a different envelope or attempt" hold:
    changing envelope_id without recomputing capability_checksum makes
    the checksum invalid.
    """

    capability_id: str
    envelope_id: str
    envelope_version: int
    attempt_id: str
    idempotency_key: str
    lease_owner_id: str
    lease_fencing_token: int
    provider: str
    model: str
    provider_operation: str
    operation_payload_bytes: bytes
    operation_payload_sha256: str
    operation_payload_schema_version: str
    max_cost_per_call: Decimal
    currency: str
    budget_reservation_ids: Sequence[str]
    issued_at: str
    expires_at: str
    capability_checksum: str

    def to_dict(self) -> dict:
        return _to_jsonable({f.name: getattr(self, f.name) for f in fields(self)})


def compute_capability_checksum(capability: AdapterInvocationCapability) -> str:
    """sha256 of every field except capability_checksum itself,
    canonical JSON (sorted keys, compact separators) -- deterministic
    regardless of construction order."""
    payload = capability.to_dict()
    payload.pop("capability_checksum", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class AdapterResult:
    """What invoke() returns. outcome is one of "accepted", "rejected",
    or "unknown_acceptance" -- never a bare success/failure boolean,
    since "unknown" is a first-class, distinct outcome this whole
    architecture exists to never silently collapse into either
    "succeeded" or "failed"."""

    outcome: str
    provider_job_id: Optional[str]
    provider_request_id: Optional[str]
    charged_amount: Optional[Decimal]
    response_checksum: Optional[str]
    evidence: Mapping[str, Any]


class FakeProviderAdapter:
    """
    The one sanctioned test-only provider adapter. Performs no network
    access whatsoever -- there is no code path here that could reach a
    provider even if a test tried to force it.

    invocation_count and received_capabilities exist purely for test
    inspection (Part 4: "record invocation count and received immutable
    request identity").

    Deterministic by design: the same capability_id can only be invoked
    once (CapabilityAlreadyConsumedError on a second attempt), but the
    *content* of a result (e.g. provider_job_id) is derived from
    idempotency_key, not capability_id -- so two different capabilities
    minted for the same idempotency_key (e.g. a gate re-issuing a
    capability after a receipt-append retry, never a second real
    provider call) deterministically produce the same acknowledgement
    content, which is exactly what "support deterministic duplicate
    acknowledgement for idempotency tests" requires.
    """

    def __init__(self):
        self.invocation_count = 0
        self._consumed_capability_ids: set = set()
        self.received_capabilities: list = []

    def invoke(
        self, capability: AdapterInvocationCapability, *, mode: str = "success",
        on_provider_acknowledged: "Callable[..., None] | None" = None,
    ) -> AdapterResult:
        """
        mode is a deterministic test-control knob, never something a
        real provider integration would expose:
          "success"                  -> outcome="accepted", a provider_job_id
          "unknown_acceptance"       -> outcome="unknown_acceptance", no job id
          "rejected"                 -> outcome="rejected", no job id
          "overcharge"               -> outcome="accepted", charged_amount > capability.max_cost_per_call
          "duplicate_acknowledgement"-> outcome="accepted", identical content to a
                                         prior "success" invocation for the same
                                         idempotency_key (same derived job id)
          "post_submit_timeout"      -> calls on_provider_acknowledged() with a
                                         REAL job id (simulating a successful
                                         submit), THEN raises -- simulating a
                                         crash/timeout during a later poll/wait,
                                         AFTER the provider already returned an
                                         ID. Exists specifically to test that
                                         durable persistence happens before the
                                         later failure, never only after it.

        on_provider_acknowledged: optional callable, invoked with
        keyword args provider_job_id/provider_request_id the moment
        this adapter has a real provider-side identifier -- mirrors
        hypogeum.providers.muapi_video_adapter.MuapiVideoAdapter's own
        real call to it, immediately after a successful submit and
        strictly before any subsequent poll/wait. Never called for
        "unknown_acceptance" (simulates never even getting an ID) or
        "rejected" (simulates outright submission failure) -- only for
        modes that reach a real acknowledgement first.

        Raises RawPromptRejectedError if capability is not an
        AdapterInvocationCapability at all (e.g. a raw string), and
        InvalidCapabilityError if it is one but its checksum doesn't
        match its own other fields, or if capability_id was already
        consumed.
        """
        if isinstance(capability, str):
            raise RawPromptRejectedError(
                "FakeProviderAdapter.invoke() refuses a raw string prompt -- "
                "an AdapterInvocationCapability minted by the submission gate is required"
            )
        if not isinstance(capability, AdapterInvocationCapability):
            raise InvalidCapabilityError(f"expected AdapterInvocationCapability, got {type(capability)!r}")

        recomputed = compute_capability_checksum(capability)
        if recomputed != capability.capability_checksum:
            raise InvalidCapabilityError(
                f"capability {capability.capability_id!r} checksum mismatch: "
                f"stored {capability.capability_checksum!r} != recomputed {recomputed!r} "
                f"(tampered, hand-constructed, or applied to a different envelope/attempt)"
            )
        if capability.capability_id in self._consumed_capability_ids:
            raise CapabilityAlreadyConsumedError(capability.capability_id)

        self.invocation_count += 1
        self._consumed_capability_ids.add(capability.capability_id)
        self.received_capabilities.append(capability)

        job_id = f"fake_job_{capability.idempotency_key}"
        request_id = f"fake_req_{capability.capability_id}"
        response_payload = {"capability_id": capability.capability_id, "mode": mode}
        response_checksum = hashlib.sha256(
            json.dumps(response_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

        if mode in ("success", "duplicate_acknowledgement"):
            if on_provider_acknowledged is not None:
                on_provider_acknowledged(provider_job_id=job_id, provider_request_id=request_id)
            return AdapterResult(
                outcome="accepted", provider_job_id=job_id, provider_request_id=request_id,
                charged_amount=capability.max_cost_per_call, response_checksum=response_checksum,
                evidence={"mode": mode},
            )
        if mode == "unknown_acceptance":
            return AdapterResult(
                outcome="unknown_acceptance", provider_job_id=None, provider_request_id=request_id,
                charged_amount=None, response_checksum=None, evidence={"mode": mode, "reason": "simulated timeout"},
            )
        if mode == "rejected":
            return AdapterResult(
                outcome="rejected", provider_job_id=None, provider_request_id=request_id,
                charged_amount=None, response_checksum=response_checksum,
                evidence={"mode": mode, "reason": "simulated provider rejection"},
            )
        if mode == "overcharge":
            if on_provider_acknowledged is not None:
                on_provider_acknowledged(provider_job_id=job_id, provider_request_id=request_id)
            return AdapterResult(
                outcome="accepted", provider_job_id=job_id, provider_request_id=request_id,
                charged_amount=capability.max_cost_per_call + Decimal("0.10"),
                response_checksum=response_checksum, evidence={"mode": mode},
            )
        if mode == "post_submit_timeout":
            if on_provider_acknowledged is not None:
                on_provider_acknowledged(provider_job_id=job_id, provider_request_id=request_id)
            raise TimeoutError(
                f"simulated post-submit timeout for idempotency_key={capability.idempotency_key!r} "
                f"AFTER acknowledgement (job_id={job_id!r}) -- the provider was contacted and returned "
                "an ID, but polling/waiting for a terminal result never completed"
            )
        raise FakeProviderAdapterError(f"unknown mode {mode!r}")
