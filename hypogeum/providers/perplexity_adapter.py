"""
hypogeum/providers/perplexity_adapter.py — Real Perplexity research-query
adapter (research.query domain).

Implements the exact same protocol
hypogeum.providers.fake_provider_adapter.FakeProviderAdapter defines,
mirroring hypogeum.providers.openrouter_text_adapter.OpenrouterTextAdapter's
own structure exactly. Wraps hypogeum.perplexity's existing, unchanged
_single_request() call rather than re-implementing its HTTP logic.

Scope: only a SINGLE _single_request() dispatch is gated here -- one
capability, one HTTP request. hypogeum.perplexity._execute_with_retry()'s
own retry loop (same query with adjusted max_tokens/query text, up to
RETRY_MAX extra attempts) stays entirely with the caller
(hypogeum.perplexity._query_perplexity_via_gate() below), each attempt
issuing its own child authorization with its own idempotency_key --
the same plan §5.5 "Retries and fallbacks" rule already applied to the
text/LLM domain (Commit H).

Unlike the OpenRouter domain, _single_request() already returns a
(result, failure_type, retry_after) TUPLE for every retryable
condition -- it only ever RAISES for the two genuinely unrecoverable
cases, PerplexityAuthFailure and PerplexityHardFailure. This adapter
mirrors that shape: a retryable outcome (tuple with result=None)
converts to outcome="rejected" evidence carrying failure_type/
retry_after (never raised); the two hard-failure exceptions are caught
and converted to outcome="rejected" evidence carrying the exact
exception class, exactly like the text/LLM adapter's own exception-
reconstruction discipline -- never left to propagate into
hypogeum.submission_gate.submit()'s generic exception handler, which
would collapse everything into an undifferentiated unknown_acceptance.

_single_request() already calls hypogeum.finance.record_cost()
internally on success (unlike the OpenRouter/video/voice/music
domains, where cost recording lives one layer up) -- this adapter does
not duplicate that call; it reuses the exact same code path unchanged.

operation_payload_schema_version "research.query/1.0" -- the exact
JSON shape operation_payload_bytes must decode to:
{
  "query": str, "max_tokens": int, "phase_id": str | null,
  "query_index": int | null, "attempt": int
}
"""

from __future__ import annotations

import json
from decimal import Decimal

from hypogeum.providers.fake_provider_adapter import (
    AdapterInvocationCapability,
    AdapterResult,
    CapabilityAlreadyConsumedError,
    InvalidCapabilityError,
    RawPromptRejectedError,
    compute_capability_checksum,
)

__all__ = [
    "SCHEMA_VERSION",
    "PerplexityAdapter",
    "encode_research_payload",
    "decode_research_payload",
]

SCHEMA_VERSION = "research.query/1.0"

_REQUIRED_PAYLOAD_FIELDS = ("query", "max_tokens")


def encode_research_payload(
    *, query: str, max_tokens: int, phase_id: "str | None" = None,
    query_index: "int | None" = None, attempt: int = 0,
) -> bytes:
    payload = {
        "query": query, "max_tokens": max_tokens, "phase_id": phase_id,
        "query_index": query_index, "attempt": attempt,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_research_payload(payload_bytes: bytes) -> dict:
    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidCapabilityError(f"operation_payload_bytes is not valid JSON: {exc}") from exc
    missing = [f for f in _REQUIRED_PAYLOAD_FIELDS if f not in payload]
    if missing:
        raise InvalidCapabilityError(f"research payload missing required field(s): {missing}")
    return payload


def _rejected(exception_class: str, message: str, *, retryable: bool, **extra) -> AdapterResult:
    evidence = {"exception_class": exception_class, "message": message, "retryable": retryable}
    evidence.update(extra)
    return AdapterResult(
        outcome="rejected", provider_job_id=None, provider_request_id=None,
        charged_amount=None, response_checksum=None, evidence=evidence,
    )


class PerplexityAdapter:
    """The one sanctioned real adapter for provider="perplexity",
    provider_operation="research.query". Mirrors
    hypogeum.providers.openrouter_text_adapter.OpenrouterTextAdapter's
    own single-use / checksum-verification discipline exactly."""

    def __init__(self):
        self.invocation_count = 0
        self._consumed_capability_ids: set = set()
        self.received_capabilities: list = []

    def invoke(self, capability: AdapterInvocationCapability, *, mode: "str | None" = None) -> AdapterResult:
        if isinstance(capability, str):
            raise RawPromptRejectedError(
                "PerplexityAdapter.invoke() refuses a raw string prompt -- "
                "an AdapterInvocationCapability minted by the submission gate is required"
            )
        if not isinstance(capability, AdapterInvocationCapability):
            raise InvalidCapabilityError(f"expected AdapterInvocationCapability, got {type(capability)!r}")

        recomputed = compute_capability_checksum(capability)
        if recomputed != capability.capability_checksum:
            raise InvalidCapabilityError(
                f"capability {capability.capability_id!r} checksum mismatch: "
                f"stored {capability.capability_checksum!r} != recomputed {recomputed!r}"
            )
        if capability.capability_id in self._consumed_capability_ids:
            raise CapabilityAlreadyConsumedError(capability.capability_id)
        if capability.operation_payload_schema_version != SCHEMA_VERSION:
            raise InvalidCapabilityError(
                f"PerplexityAdapter only accepts {SCHEMA_VERSION!r} payloads, "
                f"got {capability.operation_payload_schema_version!r}"
            )

        self.invocation_count += 1
        self._consumed_capability_ids.add(capability.capability_id)
        self.received_capabilities.append(capability)

        return self._invoke_real(capability)

    def _invoke_real(self, capability: AdapterInvocationCapability) -> AdapterResult:
        import hashlib

        from hypogeum.config import Config
        from hypogeum.logger import get_logger
        from hypogeum.perplexity import (
            PerplexityAuthFailure, PerplexityHardFailure, _PERPLEXITY_USD_PER_TOKEN, _single_request,
        )

        payload = decode_research_payload(capability.operation_payload_bytes)
        config = Config()
        log = get_logger("perplexity_adapter")

        try:
            result, failure_type, retry_after = _single_request(
                query=payload["query"], config=config, max_tokens=payload["max_tokens"],
                phase_id=payload.get("phase_id"), query_index=payload.get("query_index"),
                log=log, attempt=payload.get("attempt", 0),
            )
        except PerplexityAuthFailure as exc:
            return _rejected("PerplexityAuthFailure", str(exc), retryable=False, http_status=exc.http_status)
        except PerplexityHardFailure as exc:
            return _rejected("PerplexityHardFailure", str(exc), retryable=False, http_status=exc.http_status)

        if result is None:
            return _rejected(
                "soft_failure", f"failure_type={failure_type}", retryable=True,
                failure_type=failure_type, retry_after=retry_after,
            )

        request_id = f"px_{capability.capability_id}"
        response_evidence = {"finish_reason": result.finish_reason, "citation_count": len(result.citations)}
        response_checksum = hashlib.sha256(json.dumps(response_evidence, sort_keys=True).encode()).hexdigest()

        # Real usage-based cost, mirroring _single_request()'s own
        # formula exactly -- never exceeds the authorized ceiling.
        total_tokens = result.usage.get("total_tokens", 0)
        computed_cost = Decimal(str(round(total_tokens * _PERPLEXITY_USD_PER_TOKEN, 6)))
        charged_amount = min(computed_cost, capability.max_cost_per_call)

        return AdapterResult(
            outcome="accepted", provider_job_id=request_id, provider_request_id=request_id,
            charged_amount=charged_amount, response_checksum=response_checksum,
            evidence={
                "content": result.content, "citations": result.citations, "usage": result.usage,
                "finish_reason": result.finish_reason, "response_time_ms": result.response_time_ms,
                "citations_absent": result.citations_absent,
            },
        )
