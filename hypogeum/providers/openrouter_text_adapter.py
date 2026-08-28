"""
hypogeum/providers/openrouter_text_adapter.py — Real OpenRouter
text-completion adapter (text.completion domain).

Implements the exact same protocol
hypogeum.providers.fake_provider_adapter.FakeProviderAdapter defines,
mirroring hypogeum.providers.muapi_video_adapter.MuapiVideoAdapter's
own structure exactly. Wraps hypogeum.openrouter's existing, unchanged
_call_openrouter() call rather than re-implementing its HTTP/retry-
signal logic.

Scope: only a SINGLE _call_openrouter() dispatch is gated here -- one
capability, one HTTP request. call_claude()'s own retry loop (same
model, up to RETRY_MAX extra attempts) and its one-shot different-
model fallback stay entirely with the caller
(hypogeum.openrouter._call_claude_via_gate()/
_try_openrouter_model_fallback_via_gate()), each attempt issuing its
own child authorization with its own idempotency_key -- exactly the
plan's §5.5 "Retries and fallbacks" rule: "A retry/fallback attempt is
a separate issue_child_authorization() call... a real second call with
real potential spend, never free."

Unlike every other domain's adapter, EVERY exception _call_openrouter()
can raise (_AuthError, _RetryableError, requests.RequestException,
CreditsExhausted, ContentFiltered, EmptyResponse, HardFailure,
TruncatedResponse) is caught here and converted to a structured
outcome="rejected" AdapterResult carrying the exact exception class
name plus its reconstructible fields in evidence -- never left to
propagate into hypogeum.submission_gate.submit()'s generic exception
handler, which would collapse all of them into an undifferentiated
unknown_acceptance and lose the retry-vs-hard-failure distinction the
outer gate orchestrator needs to reproduce call_claude()'s existing
policy exactly. Only a genuinely unanticipated exception (a real bug,
not a modeled OpenRouter failure mode) propagates uncaught, which IS
correctly classified as unknown_acceptance -- consistent with every
other adapter's disclosed choice for truly unexpected failures.

operation_payload_schema_version "text.completion/1.0" -- the exact
JSON shape operation_payload_bytes must decode to:
{
  "messages": [{"role": str, "content": str}, ...], "model_slug": str,
  "max_tokens": int, "response_format": dict | null, "provider": dict | null
}
"""

from __future__ import annotations

import json
from decimal import Decimal

import hypogeum.capability_boundary as capability_boundary
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
    "OpenrouterTextAdapter",
    "encode_text_payload",
    "decode_text_payload",
]

SCHEMA_VERSION = "text.completion/1.0"

_REQUIRED_PAYLOAD_FIELDS = ("messages", "model_slug", "max_tokens")


def encode_text_payload(
    *, messages: "list[dict]", model_slug: str, max_tokens: int,
    response_format: "dict | None" = None, provider: "dict | None" = None,
) -> bytes:
    payload = {
        "messages": messages, "model_slug": model_slug, "max_tokens": max_tokens,
        "response_format": response_format, "provider": provider,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_text_payload(payload_bytes: bytes) -> dict:
    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidCapabilityError(f"operation_payload_bytes is not valid JSON: {exc}") from exc
    missing = [f for f in _REQUIRED_PAYLOAD_FIELDS if f not in payload]
    if missing:
        raise InvalidCapabilityError(f"text payload missing required field(s): {missing}")
    return payload


def _rejected(exception_class: str, message: str, *, retryable: bool, **extra) -> AdapterResult:
    evidence = {"exception_class": exception_class, "message": message, "retryable": retryable}
    evidence.update(extra)
    return AdapterResult(
        outcome="rejected", provider_job_id=None, provider_request_id=None,
        charged_amount=None, response_checksum=None, evidence=evidence,
    )


class OpenrouterTextAdapter:
    """The one sanctioned real adapter for provider="openrouter",
    provider_operation="text.completion". Mirrors
    hypogeum.providers.muapi_video_adapter.MuapiVideoAdapter's own
    single-use / checksum-verification discipline exactly."""

    def __init__(self):
        self.invocation_count = 0
        self._consumed_capability_ids: set = set()
        self.received_capabilities: list = []

    def invoke(
        self, capability: AdapterInvocationCapability, *, mode: "str | None" = None,
        on_provider_acknowledged: "object | None" = None,
    ) -> AdapterResult:
        # on_provider_acknowledged: accepted for protocol uniformity with
        # every other registered adapter (hypogeum.submission_gate.submit()
        # passes it to every adapter's invoke() uniformly) -- this domain
        # has no intermediate submit-then-poll acknowledgement phase, so it
        # is intentionally never called here.
        if isinstance(capability, str):
            raise RawPromptRejectedError(
                "OpenrouterTextAdapter.invoke() refuses a raw string prompt -- "
                "an AdapterInvocationCapability minted by the submission gate is required"
            )
        if not isinstance(capability, AdapterInvocationCapability):
            raise InvalidCapabilityError(f"expected AdapterInvocationCapability, got {type(capability)!r}")

        ctx = capability_boundary.verify_and_consume_capability(
            capability, expected_schema_version=SCHEMA_VERSION,
            consumed_cache=self._consumed_capability_ids,
        )

        self.invocation_count += 1
        self.received_capabilities.append(capability)

        return self._invoke_real(ctx)

    def _invoke_real(self, ctx: "capability_boundary.VerifiedInvocationContext") -> AdapterResult:
        ctx.assert_live()
        ctx.claim_io_entry()  # durable atomic provider-I/O entry gate
        capability = ctx.capability
        import hashlib
        import time

        import requests

        from hypogeum.config import Config
        from hypogeum.logger import get_logger
        from hypogeum.openrouter import (
            ContentFiltered, CreditsExhausted, EmptyResponse, HardFailure, TruncatedResponse,
            _AuthError, _RetryableError, _call_openrouter,
        )

        payload = decode_text_payload(capability.operation_payload_bytes)
        config = Config()
        log = get_logger("openrouter_text_adapter")
        start_ms = int(time.monotonic() * 1000)

        try:
            result = _call_openrouter(
                model_slug=payload["model_slug"], messages=payload["messages"], max_tokens=payload["max_tokens"],
                config=config, log=log, start_ms=start_ms,
                response_format=payload.get("response_format"), provider=payload.get("provider"),
            )
        except _AuthError as exc:
            return _rejected("_AuthError", str(exc), retryable=False)
        except _RetryableError as exc:
            return _rejected(
                "_RetryableError", str(exc), retryable=True,
                status_code=exc.status_code, retry_after=exc.retry_after,
            )
        except requests.exceptions.RequestException as exc:
            return _rejected("RequestException", str(exc), retryable=True)
        except CreditsExhausted as exc:
            return _rejected("CreditsExhausted", str(exc), retryable=False, http_status=exc.http_status)
        except ContentFiltered as exc:
            return _rejected("ContentFiltered", str(exc), retryable=False, http_status=exc.http_status)
        except EmptyResponse as exc:
            return _rejected("EmptyResponse", str(exc), retryable=False, http_status=exc.http_status)
        except TruncatedResponse as exc:
            return _rejected(
                "TruncatedResponse", str(exc), retryable=False,
                completion_tokens=exc.completion_tokens, max_tokens_set=exc.max_tokens_set,
            )
        except HardFailure as exc:
            return _rejected("HardFailure", str(exc), retryable=False, http_status=exc.http_status)

        request_id = f"or_{capability.capability_id}"
        response_evidence = {"model_used": result.model_used, "finish_reason": result.finish_reason}
        response_checksum = hashlib.sha256(json.dumps(response_evidence, sort_keys=True).encode()).hexdigest()

        # Real usage-based cost, mirroring the legacy path's own
        # _estimate_cost() formula exactly -- never exceeds the
        # authorized ceiling.
        computed_cost = Decimal(str(round(result.estimated_cost_usd, 6)))
        charged_amount = min(computed_cost, capability.max_cost_per_call)

        return AdapterResult(
            outcome="accepted", provider_job_id=request_id, provider_request_id=request_id,
            charged_amount=charged_amount, response_checksum=response_checksum,
            evidence={
                "content": result.content, "usage": result.usage, "model_used": result.model_used,
                "finish_reason": result.finish_reason, "response_time_ms": result.response_time_ms,
                "estimated_cost_usd": result.estimated_cost_usd,
            },
        )
