"""
hypogeum/openrouter.py — OpenRouter API client

Single integration point for all Claude model calls in the pipeline.
All 9 agents call Claude through this module — never via requests directly.

Primary path:  OpenRouter chat completions (OpenAI-compatible format)
Fallback path: config.OPENROUTER_FALLBACK_MODEL, one attempt, through the
               same OpenRouter endpoint and credential (triggered by primary-
               model retry exhaustion; see docs/provider-model-director.md
               §8.3). A 401 (invalid OPENROUTER_API_KEY) does not attempt the
               fallback model — a different model through the same invalid
               credential fails identically. Anthropic direct
               (_call_anthropic_direct) still exists but is never called
               automatically; it is unreachable dead code from call_claude()
               unless something else opts back into it deliberately.

External dependency: requests

Logging events emitted:
  openrouter_request_sent            — info  — before each API call
  openrouter_response_received       — info  — on successful response
  openrouter_retry_triggered         — warn  — before each retry attempt
  openrouter_hard_failure            — error — on 400/402/403/content_filter/401
  openrouter_model_fallback_activated — warn — attempting/resolving OPENROUTER_FALLBACK_MODEL
  openrouter_truncated_response      — warn  — when finish_reason == "length"

API keys are never logged. Full message content is never logged.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import requests

from hypogeum.config import Config
from hypogeum.execution_envelope import GenerationExecutionEnvelope
from hypogeum.logger import Logger, get_logger


# ---------------------------------------------------------------------------
# Endpoints and policy constants
# ---------------------------------------------------------------------------

OPENROUTER_CHAT_URL   = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION  = "2023-06-01"

RETRY_MAX              = 1       # Total extra attempts after the first failure
RETRY_INITIAL_DELAY    = 5.0    # seconds
RETRY_MULTIPLIER       = 2.0
RETRY_MAX_DELAY        = 30.0   # seconds — cap on exponential growth
RETRY_JITTER_FRACTION  = 0.15   # ±15% of computed delay
REQUEST_TIMEOUT        = 120    # seconds — generous for long Opus calls

RETRYABLE_STATUS_CODES = {429, 500, 502, 503}

# Prefix used by every Anthropic-hosted model slug this codebase passes to
# OpenRouter (e.g. "anthropic/claude-sonnet-4.6"). Only slugs with this
# prefix receive the Anthropic prompt-caching content-block format below —
# other providers routed through OpenRouter (e.g. "openai/gpt-5.5-pro", used
# by default for TD_PLANNING_MODEL) do not support this exact format and must
# keep the plain-string system message they already receive today.
_ANTHROPIC_SLUG_PREFIX = "anthropic/"

# Approximate per-token USD cost for estimation only (not billing).
# Prompt (input) and completion (output) rates per token.
_COST_TABLE: dict[str, dict[str, float]] = {
    "anthropic/claude-sonnet-4.6": {"prompt": 3e-6,  "completion": 15e-6},
    "anthropic/claude-opus-4.6":   {"prompt": 15e-6, "completion": 75e-6},
}


# ---------------------------------------------------------------------------
# Module-level run state
# ---------------------------------------------------------------------------

_use_anthropic_for_run: bool = False


def reset_run_state() -> None:
    """
    Reset module-level run state. Call once at the start of each pipeline run.

    Clears the _use_anthropic_for_run flag that is set when OpenRouter is
    unavailable at health-check time or after a 401 / retry exhaustion during
    a run. Without a reset between runs, a single failed run would lock all
    subsequent runs into Anthropic direct mode.
    """
    global _use_anthropic_for_run
    _use_anthropic_for_run = False


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CallResult:
    """Structured result of a successful call_claude() invocation."""

    content: str
    """Extracted assistant message text."""

    usage: dict[str, int]
    """Token counts: {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}."""

    model_used: str
    """Actual model slug/ID reported by the API (may differ from requested)."""

    finish_reason: str
    """Stop reason: "stop" | "end_turn" (normalised) or raw API value."""

    response_time_ms: int
    """Wall-clock milliseconds from request dispatch to response parsed."""

    via_fallback: bool
    """True when Anthropic direct was used instead of OpenRouter."""

    estimated_cost_usd: float
    """Rough USD cost estimate from _COST_TABLE — for monitoring only."""


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class OpenRouterError(RuntimeError):
    """Base class for all OpenRouter client errors."""


class HardFailure(OpenRouterError):
    """
    Unrecoverable error — no retry, no fallback to a secondary model.
    Callers must halt the current agent and issue a PDR.
    Covers: HTTP 400 (bad request), 403 (forbidden), malformed response.
    """
    def __init__(self, message: str, http_status: Optional[int] = None) -> None:
        self.http_status = http_status
        super().__init__(message)


class CreditsExhausted(HardFailure):
    """HTTP 402 — OpenRouter account balance is zero. Human intervention required."""


class ContentFiltered(HardFailure):
    """finish_reason == 'content_filter' — output blocked by content policy."""


class EmptyResponse(HardFailure):
    """choices[0].message.content is empty or absent after a nominally successful call."""


class TruncatedResponse(OpenRouterError):
    """
    finish_reason == 'length' — the response was cut off at max_tokens.
    Not a hard failure (the API call itself succeeded) but the output is
    unusable without restructuring the prompt or increasing max_tokens.
    """
    def __init__(self, completion_tokens: int, max_tokens_set: int) -> None:
        self.completion_tokens = completion_tokens
        self.max_tokens_set = max_tokens_set
        super().__init__(
            f"Response truncated: completion_tokens={completion_tokens} "
            f"reached max_tokens={max_tokens_set}. "
            "Increase max_tokens or split the request."
        )


class FallbackFailed(OpenRouterError):
    """
    Every OpenRouter-native option is exhausted: the primary model's retry
    budget ran out and either no OPENROUTER_FALLBACK_MODEL is configured or
    that fallback model also failed. No Anthropic-direct call is attempted
    (see docs/provider-model-director.md §8 — the target architecture keeps
    fallback within OpenRouter's own model routing, not a separate
    provider/credential). This is not marked FailurePolicy.TERMINAL, so
    hypogeum.queue's bounded auto-retry / recurring-schedule mechanisms
    pick the episode back up from its on-disk checkpoint later — the
    caller does not need to (and must not) retry it inline.
    """
    def __init__(
        self,
        message: str,
        original_error: Optional[Exception] = None,
    ) -> None:
        self.original_error = original_error
        super().__init__(message)


# Internal signals — never raised outside this module
class _RetryableError(Exception):
    """Soft failure; outer loop will retry or activate fallback."""
    def __init__(self, status_code: int, retry_after: Optional[int] = None) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"Retryable HTTP {status_code}")


class _AuthError(Exception):
    """HTTP 401 — triggers immediate switch to Anthropic direct fallback."""


# ---------------------------------------------------------------------------
# Provider error diagnostics (2026-08-09)
#
# OpenRouter sometimes reports a request failure via HTTP 200 with a JSON
# body shaped {"error": {...}} instead of a valid choices completion (e.g.
# an underlying provider rejection like Anthropic's "compiled grammar is
# too large" invalid_request_error, forwarded through OpenRouter's own
# error envelope rather than surfacing as a non-200 status). Before this
# correction, _parse_openrouter_response() caught this shape as a generic
# KeyError("choices"), logged only top_level_keys/has_error_field, and
# unconditionally raised _RetryableError(200) -- discarding the actual
# error.code/message/type and misclassifying every such response as
# retryable regardless of what it actually said.
#
# _extract_safe_provider_error() pulls out ONLY a fixed, safe field
# whitelist (code/message/type, each capped in length, plus the nested
# provider error's own type/message when OpenRouter's metadata.raw carries
# one) -- never the request body, headers, Authorization/API-key values,
# cookies, or prompt/schema content, none of which this function ever
# reads from `data` in the first place (they were never part of the
# response body to begin with). _classify_and_raise_provider_error() then
# raises the SAME existing exception types (_AuthError, CreditsExhausted,
# HardFailure, _RetryableError) that HTTP-status-code classification
# already uses, driven now by the extracted fields instead of a blanket
# assumption -- retry policy, max_attempts, model-fallback policy, and
# every other caller-facing behavior in call_claude() are unchanged; only
# what gets classified as retryable vs. hard-failed for THIS specific
# response shape becomes accurate instead of universal.
# ---------------------------------------------------------------------------

_MAX_SAFE_ERROR_MESSAGE_LENGTH = 500


def _extract_safe_provider_error(data: dict) -> dict:
    """
    Extract only safe diagnostic fields from an OpenRouter error body
    shaped {"error": {"code":..., "message":..., "type":..., "metadata":
    {"raw": "<json-encoded nested provider error>"}}}. Returns {} if `data`
    has no dict-shaped "error" field. Every string field is length-capped;
    nothing beyond code/message/type (top-level and, when present, the
    nested provider error's own type/message) is ever extracted -- request
    payload, headers, credentials, and prompt/schema content are never
    part of this response body and are never read here.
    """
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return {}

    safe: dict = {}
    code = error.get("code")
    if isinstance(code, (int, str)):
        safe["code"] = code
    message = error.get("message")
    if isinstance(message, str):
        safe["message"] = message[:_MAX_SAFE_ERROR_MESSAGE_LENGTH]
    error_type = error.get("type")
    if isinstance(error_type, str):
        safe["type"] = error_type

    metadata = error.get("metadata")
    if isinstance(metadata, dict):
        raw = metadata.get("raw")
        if isinstance(raw, str):
            try:
                nested = json.loads(raw)
            except (json.JSONDecodeError, ValueError, TypeError):
                nested = None
            nested_error = nested.get("error") if isinstance(nested, dict) else None
            if isinstance(nested_error, dict):
                nested_type = nested_error.get("type")
                nested_message = nested_error.get("message")
                if isinstance(nested_type, str):
                    safe["provider_error_type"] = nested_type
                if isinstance(nested_message, str):
                    safe["provider_error_message"] = nested_message[:_MAX_SAFE_ERROR_MESSAGE_LENGTH]

    return safe


# Error types (top-level OpenRouter or nested provider) that are never
# worth retrying -- a request-shape/parameter/grammar rejection will fail
# identically on every retry.
_HARD_FAILURE_ERROR_TYPES = {"invalid_request_error", "permission_error", "not_found_error"}
# Error types that mean the same transient-glitch handling as a retryable
# HTTP status code (429/500/502/503).
_RETRYABLE_ERROR_TYPES = {"rate_limit_error", "overloaded_error", "api_error"}


def _classify_and_raise_provider_error(data: dict, log: Logger) -> None:
    """
    Raises the appropriate EXISTING exception type (_AuthError,
    CreditsExhausted, HardFailure, or _RetryableError — the same types
    HTTP-status-code classification already uses) for an OpenRouter
    response shaped {"error": {...}} with no valid "choices". Always
    raises; never returns normally. Logs only the safe fields
    _extract_safe_provider_error() returns.

    Classification (a top-level OpenRouter error.code takes precedence
    over error.type where both would apply; the nested provider error's
    own type, when present, is preferred over OpenRouter's own type for
    the invalid_request_error/retryable-type checks, since it is the more
    specific signal):
      code 401                                   -> _AuthError
      code 402                                   -> CreditsExhausted
      code 400 or 403, or type invalid_request_error/permission_error/
        not_found_error                          -> HardFailure
      code in RETRYABLE_STATUS_CODES, or type
        rate_limit_error/overloaded_error/api_error -> _RetryableError
      anything else (unrecognized shape)         -> _RetryableError
        (preserves the prior default for genuinely unrecognized errors —
        this function only replaces a BLANKET retryable assumption with
        accurate classification for the shapes it recognizes, it does not
        newly hard-fail shapes nobody has seen before)
    """
    safe = _extract_safe_provider_error(data)
    code = safe.get("code")
    effective_type = safe.get("provider_error_type") or safe.get("type")
    message = safe.get("provider_error_message") or safe.get("message") or "OpenRouter returned an error response"

    log.error(
        "openrouter_provider_error",
        http_status=200,
        error_code=code,
        error_type=safe.get("type"),
        provider_error_type=safe.get("provider_error_type"),
        error_message=safe.get("message"),
        provider_error_message=safe.get("provider_error_message"),
    )

    if code == 401:
        raise _AuthError()

    if code == 402:
        raise CreditsExhausted(f"OpenRouter credits exhausted: {message}", http_status=402)

    if code in (400, 403) or effective_type in _HARD_FAILURE_ERROR_TYPES:
        raise HardFailure(
            f"OpenRouter/provider returned an invalid-request error: {message}",
            http_status=code if isinstance(code, int) else 200,
        )

    if code in RETRYABLE_STATUS_CODES or effective_type in _RETRYABLE_ERROR_TYPES:
        raise _RetryableError(status_code=code if isinstance(code, int) else 200)

    raise _RetryableError(status_code=200)


# ---------------------------------------------------------------------------
# Prompt caching — Anthropic ephemeral cache_control on the system message
# ---------------------------------------------------------------------------
#
# Verified against primary sources (platform.claude.com/docs/en/build-with-claude/
# prompt-caching and openrouter.ai/docs/features/prompt-caching):
#   - Anthropic's native Messages API and OpenRouter's Anthropic-model proxy both
#     require the system message's content to be an array of typed content
#     blocks — not a plain string — for cache_control to apply. The exact block
#     shape is identical for both: {"type": "text", "text": "...",
#     "cache_control": {"type": "ephemeral"}}.
#   - Caching is not automatic; it is opt-in per content block.
#   - No special headers are required (prompt caching is generally available,
#     not a beta feature, on Anthropic's API as of this verification).
#   - Below-minimum-length prompts are silently processed without caching by
#     Anthropic's backend — no error is raised — so this transform never needs
#     its own length check to stay safe.
#   - This format is Anthropic-specific. Other providers proxied through
#     OpenRouter are not known to support this exact block shape, so this
#     module only applies it when model_slug identifies an Anthropic model.

def _cache_control_supported(model_slug: str) -> bool:
    """True when model_slug is an Anthropic-hosted model that supports the
    verified cache_control content-block format. See module note above."""
    return model_slug.startswith(_ANTHROPIC_SLUG_PREFIX)


def _apply_system_prompt_caching(
    messages: list[dict[str, Any]], model_slug: str
) -> list[dict[str, Any]]:
    """
    Return a new messages list with the system-role message's content
    converted to a single-block content array carrying an ephemeral
    cache_control breakpoint, for models that support it (see module note
    above). The system prompt text itself is never altered — only its JSON
    envelope shape, and only for supported models. Non-system messages and
    unsupported models are returned unchanged. The input list is never
    mutated in place.
    """
    if not _cache_control_supported(model_slug):
        return list(messages)

    transformed: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            transformed.append({
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": m["content"],
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            })
        else:
            transformed.append(m)
    return transformed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ASSETS_ROOT = _PROJECT_ROOT / ".hypogeum-assets"

_HYPOGEUM_WORKFLOW_ID = "hypogeum_main_production"
_HYPOGEUM_WORKFLOW_VERSION = "1.0.0"
_TEXT_PROVIDER = "openrouter"
_TEXT_PROVIDER_OPERATION = "text.completion"
_TEXT_COMPILER_IDENTITY = "hypogeum.openrouter.call_claude"
_TEXT_ENVELOPE_SCHEMA_VERSION = "1.0"

# Conservative per-token rate used to pre-estimate a max_cost_per_call
# ceiling BEFORE the real usage is known (unlike _estimate_cost(), which
# runs AFTER a response and uses the model's own _COST_TABLE entry).
# Unknown/unlisted model_slugs get the most expensive rate on file
# (Opus-tier) rather than 0.0, so an unrecognized model gets a safe,
# generous ceiling instead of one that would reject any real spend.
_UNKNOWN_MODEL_CEILING_RATE = {"prompt": 15e-6, "completion": 75e-6}


def _canonical_json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _estimate_max_cost_ceiling(model_slug: str, messages: list, max_tokens: int) -> Decimal:
    rates = _COST_TABLE.get(model_slug, _UNKNOWN_MODEL_CEILING_RATE)
    prompt_chars = sum(len(m.get("content", "")) for m in messages)
    prompt_tokens_estimate = max(1, prompt_chars // 4)
    ceiling = prompt_tokens_estimate * rates["prompt"] + max_tokens * rates["completion"]
    return Decimal(str(round(ceiling, 6)))


def _call_openrouter_via_gate(
    *, channel_id: str, grant, model_slug: str, messages: list[dict[str, Any]], max_tokens: int,
    config: Config, log: Logger, episode_id: str, agent: Optional[str],
    response_format: Optional[dict], provider: Optional[dict], attempt_label: str,
) -> CallResult:
    """
    The grant-derived gate path for ONE OpenRouter dispatch -- mirrors
    hypogeum.elevenlabs._generate_narration_via_gate()'s structure
    (which itself mirrors hypogeum.muapi._submit_scene_via_gate()).
    Never reached unless a real, verified grant already resolves for
    (channel_id, episode_id). Called once per attempt (primary
    attempt N, or the one-shot different-model fallback) by
    _call_claude_via_gate()/_try_openrouter_model_fallback_via_gate()
    below -- each attempt is its own envelope/authorization/gate
    submission with its own idempotency_key, per the plan's §5.5
    "Retries and fallbacks" rule.

    On gate approval, reconstructs and returns a CallResult from the
    adapter's evidence. On denial, reconstructs and raises the EXACT
    same exception type hypogeum.providers.openrouter_text_adapter.
    OpenrouterTextAdapter classified internally (see that module's own
    docstring), so the caller's retry/fallback decision logic sees
    identical exception types to the legacy _call_openrouter() path.
    A genuine gate-level denial (grant/authorization/lease failure, or
    a truly unclassified unknown_acceptance) raises FallbackFailed --
    retry-eligible at the episode/queue level, matching call_claude()'s
    own contract for "every OpenRouter-native option is exhausted."
    """
    import hashlib
    from datetime import timedelta

    import hypogeum.adapter_registry as adapter_registry
    import hypogeum.authorization_authority as authority
    import hypogeum.authorization_store as authorization_store
    import hypogeum.budget_scopes as bs
    import hypogeum.channel_config as channel_config
    import hypogeum.db as db
    import hypogeum.submission_gate as submission_gate
    import hypogeum.workflow_executor as workflow_executor
    from hypogeum.providers.openrouter_text_adapter import (
        SCHEMA_VERSION as _TEXT_SCHEMA_VERSION, OpenrouterTextAdapter, encode_text_payload,
    )
    from hypogeum.provider_receipt import WriteAheadReceiptStore

    at = datetime.now(timezone.utc).isoformat()

    package_identity = channel_config.compute_channel_package_identity(channel_id)
    if not package_identity.get("available"):
        raise FallbackFailed(f"channel_package_identity_unavailable for {attempt_label!r}")
    workflow_binding = workflow_executor.resolve_workflow_binding(
        channel_id, _HYPOGEUM_WORKFLOW_ID, _HYPOGEUM_WORKFLOW_VERSION,
    )

    plan_checksum = hashlib.sha256(_canonical_json_bytes({"agent": agent, "attempt_label": attempt_label})).hexdigest()
    structured_spec_checksum = hashlib.sha256(_canonical_json_bytes({"model_slug": model_slug, "max_tokens": max_tokens})).hexdigest()

    operation_payload_bytes = encode_text_payload(
        messages=messages, model_slug=model_slug, max_tokens=max_tokens,
        response_format=response_format, provider=provider,
    )
    operation_payload_content = operation_payload_bytes.decode("utf-8")
    operation_payload_sha256 = hashlib.sha256(operation_payload_bytes).hexdigest()

    # attempt_label identifies the RETRY (agent + primary/fallback + attempt N).
    # An agent that makes several DISTINCT gated calls in one stage (e.g. the
    # Executive Producer: validation_scorecard, then topic_brief) would otherwise
    # reuse one attempt_label across all of them and collide on the ledger's
    # UNIQUE(grant, provider, model, operation, idempotency_key) -- the second
    # call is rejected as a mismatched-envelope replay of the first. Fold in a
    # payload discriminator so each distinct call has its own identity while a
    # genuine retry (identical payload, incremented attempt N) still gets a
    # fresh key via attempt_label, and a true replay (same payload, same N)
    # still collapses to the same idempotent key.
    call_label = f"{attempt_label}_{operation_payload_sha256[:12]}"

    expected_output_identity = f"episodes/{episode_id}/text/{call_label}"
    deterministic_output_path = f".hypogeum-assets/episodes/{episode_id}/text/{call_label}.json"
    max_cost = _estimate_max_cost_ceiling(model_slug, messages, max_tokens)
    authorization_id = f"{episode_id}:{agent or 'text'}:{call_label}"

    try:
        envelope = GenerationExecutionEnvelope.create(
            envelope_schema_version=_TEXT_ENVELOPE_SCHEMA_VERSION, envelope_version=1, channel_id=channel_id,
            channel_package_version=package_identity["version"], channel_package_checksum=package_identity["checksum"],
            execution_id=episode_id, workflow_version=workflow_binding["workflow_version"],
            plan_version="live", plan_checksum=plan_checksum, planned_shot_id=call_label,
            structured_spec_checksum=structured_spec_checksum, compiler_identity=_TEXT_COMPILER_IDENTITY,
            compiler_version="1", operation_payload_content=operation_payload_content,
            operation_payload_sha256=operation_payload_sha256, operation_payload_schema_version=_TEXT_SCHEMA_VERSION,
            content_provenance={
                "compiler": _TEXT_COMPILER_IDENTITY, "compiler_version": "1",
                "source_work_item_id": call_label, "rendered_at": at,
            },
            provider=_TEXT_PROVIDER, model=model_slug, provider_operation=_TEXT_PROVIDER_OPERATION,
            deterministic_output_path=deterministic_output_path, expected_output_identity=expected_output_identity,
            authorization_id=authorization_id, authorization_version=1, max_cost_per_call=max_cost,
            run_level_budget_ref=episode_id, episode_level_budget_ref=episode_id, currency="USD",
            retry_authorized=False, fallback_authorized=False, idempotency_key=authorization_id,
            created_at=at, expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
            dependency_checksums={
                "channel_package": package_identity["checksum"], "plan": plan_checksum,
                "structured_spec": structured_spec_checksum, "compiler": hashlib.sha256(_TEXT_COMPILER_IDENTITY.encode()).hexdigest(),
            },
        )
        authorization = authority.issue_child_authorization(envelope, grant.grant_id, grant.grant_version, at=at)
    except Exception as exc:  # noqa: BLE001 -- any compile/authorize failure here is a clean non-network denial
        raise FallbackFailed(f"Text gate authorization failed for {call_label!r}: {exc}") from exc

    conn = db.get_connection()
    try:
        authorization_store.persist_execution_envelope(conn, envelope)

        adapter_registry.register_adapter(_TEXT_PROVIDER, model_slug, _TEXT_PROVIDER_OPERATION, "1.0", OpenrouterTextAdapter)
        lease_owner_id = f"openrouter_text_adapter:{call_label}"
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        lease = db.acquire_lease(conn, envelope.envelope_id, lease_owner_id, acquired_at=at, expires_at=lease_expires_at)
        if lease is None:
            raise FallbackFailed(f"Text gate lease unavailable for {call_label!r}")

        receipt_store = WriteAheadReceiptStore(_ASSETS_ROOT / "episodes" / episode_id)
        live_sources = submission_gate.LiveSources(
            channel_package_checksum=package_identity["checksum"], plan_checksum=plan_checksum,
            structured_spec_checksum=structured_spec_checksum, compiler_version="1",
        )
        scopes = [
            bs.ScopeRequest(bs.BudgetScopeKind.CALL, call_label, max_cost, "USD"),
            bs.ScopeRequest(bs.BudgetScopeKind.EPISODE, episode_id, grant.max_total_spend_all_bindings or max_cost, "USD"),
        ]
        result = submission_gate.submit(
            envelope, live_sources=live_sources, authorization=authorization, lease_conn=conn,
            lease_owner_id=lease_owner_id, lease_fencing_token=lease["fencing_token"], receipt_store=receipt_store,
            execution_id=episode_id, attempt_id=authorization_id, budget_scopes=scopes,
            required_provider=_TEXT_PROVIDER, required_model=model_slug, at=at,
            expected_output_identity=expected_output_identity, expected_deterministic_output_path=deterministic_output_path,
            output_already_exists=False,
        )
    finally:
        conn.close()

    if not result.approved:
        evidence = result.adapter_result.evidence if result.adapter_result else {}
        exc_class = evidence.get("exception_class")
        message = evidence.get("message", result.reason)
        if exc_class == "_AuthError":
            raise _AuthError()
        if exc_class == "_RetryableError":
            raise _RetryableError(evidence.get("status_code"), evidence.get("retry_after"))
        if exc_class == "RequestException":
            raise requests.exceptions.RequestException(message)
        if exc_class == "CreditsExhausted":
            raise CreditsExhausted(message, http_status=evidence.get("http_status"))
        if exc_class == "ContentFiltered":
            raise ContentFiltered(message, http_status=evidence.get("http_status"))
        if exc_class == "EmptyResponse":
            raise EmptyResponse(message, http_status=evidence.get("http_status"))
        if exc_class == "TruncatedResponse":
            raise TruncatedResponse(evidence.get("completion_tokens", 0), evidence.get("max_tokens_set", max_tokens))
        if exc_class == "HardFailure":
            raise HardFailure(message, http_status=evidence.get("http_status"))
        raise FallbackFailed(f"Text gate denied for {call_label!r}: [{result.denied_at_check}] {result.reason}")

    evidence = result.adapter_result.evidence
    return CallResult(
        content=evidence["content"], usage=evidence["usage"], model_used=evidence["model_used"],
        finish_reason=evidence["finish_reason"], response_time_ms=evidence["response_time_ms"],
        via_fallback=False, estimated_cost_usd=evidence["estimated_cost_usd"],
    )


def _try_openrouter_model_fallback_via_gate(
    *, channel_id: str, grant, primary_model: str, messages: list[dict[str, Any]], max_tokens: int,
    config: Config, log: Logger, episode_id: str, agent: Optional[str],
    response_format: Optional[dict], provider: Optional[dict],
) -> CallResult:
    """Gate-path counterpart to _try_openrouter_model_fallback() -- one
    attempt at config.OPENROUTER_FALLBACK_MODEL, its own gated
    envelope/authorization/submission, never Anthropic direct."""
    fallback_model = config.OPENROUTER_FALLBACK_MODEL

    if not fallback_model:
        log.warn(
            "openrouter_model_fallback_activated", primary_model=primary_model, fallback_model=None,
            reason="retry_exhausted", success=False, via_gate=True,
        )
        raise FallbackFailed(
            f"OpenRouter primary model '{primary_model}' failed via the gate path and no "
            "OPENROUTER_FALLBACK_MODEL is configured."
        )

    fallback_messages = _apply_system_prompt_caching(messages, fallback_model)

    try:
        result = _call_openrouter_via_gate(
            channel_id=channel_id, grant=grant, model_slug=fallback_model, messages=fallback_messages,
            max_tokens=max_tokens, config=config, log=log, episode_id=episode_id, agent=agent,
            response_format=response_format, provider=provider, attempt_label=f"{agent or 'text'}_fallback",
        )
    except Exception as exc:
        log.warn(
            "openrouter_model_fallback_activated", primary_model=primary_model, fallback_model=fallback_model,
            reason="retry_exhausted", success=False, via_gate=True,
        )
        raise FallbackFailed(
            f"OpenRouter primary model '{primary_model}' failed via the gate path and fallback "
            f"model '{fallback_model}' also failed: {exc}",
            original_error=exc,
        ) from exc

    log.warn(
        "openrouter_model_fallback_activated", primary_model=primary_model, fallback_model=fallback_model,
        reason="retry_exhausted", success=True, via_gate=True,
    )
    return result


def _call_claude_via_gate(
    *, channel_id: str, grant, model_slug: str, messages: list[dict[str, str]], max_tokens: int,
    config: Config, episode_id: str, agent: Optional[str], response_format: Optional[dict],
    provider: Optional[dict], allow_model_fallback: bool, max_attempts: Optional[int],
) -> CallResult:
    """
    The grant-derived gate path for call_claude() -- mirrors that
    function's own retry+model-fallback loop exactly (same
    RETRY_MAX-derived budget, same allow_model_fallback/max_attempts
    semantics), except each individual OpenRouter dispatch (each retry
    of the primary model, and the one-shot different-model fallback)
    is its own gated attempt via _call_openrouter_via_gate()/
    _try_openrouter_model_fallback_via_gate() instead of a direct
    _call_openrouter() call. Never reached unless a real, verified
    grant already resolves for (channel_id, episode_id); the dormant
    Anthropic-direct fallback (_use_anthropic_for_run) is intentionally
    NOT gated here -- it is unreachable dead code today (see module
    docstring) and stays exactly as unreachable via this path as via
    the legacy one.
    """
    log = get_logger(episode_id=episode_id, agent=agent)
    cached_messages = _apply_system_prompt_caching(messages, model_slug)

    effective_retry_max = RETRY_MAX if max_attempts is None else max(0, max_attempts - 1)
    attempt = 0
    while True:
        try:
            return _call_openrouter_via_gate(
                channel_id=channel_id, grant=grant, model_slug=model_slug, messages=cached_messages,
                max_tokens=max_tokens, config=config, log=log, episode_id=episode_id, agent=agent,
                response_format=response_format, provider=provider,
                attempt_label=f"{agent or 'text'}_primary_{attempt}",
            )

        except _AuthError:
            log.warn(
                "openrouter_hard_failure", failure_type="auth_failure_401", http_status=401,
                action="no_fallback_same_credential", via_gate=True,
            )
            raise FallbackFailed(
                "OpenRouter authentication failed (HTTP 401) via the gate path -- the "
                "OPENROUTER_API_KEY credential itself is invalid. No fallback model was "
                "attempted (it would use the same invalid credential)."
            )

        except (_RetryableError, requests.exceptions.RequestException) as exc:
            if attempt >= effective_retry_max:
                if not allow_model_fallback:
                    log.warn(
                        "openrouter_model_fallback_activated", primary_model=model_slug, fallback_model=None,
                        reason="retry_exhausted", success=False, fallback_disabled_by_caller=True, via_gate=True,
                    )
                    raise FallbackFailed(
                        f"OpenRouter primary model '{model_slug}' failed via the gate path and retry "
                        "budget is exhausted. Different-model fallback is disabled for this call "
                        "(allow_model_fallback=False)."
                    )
                return _try_openrouter_model_fallback_via_gate(
                    channel_id=channel_id, grant=grant, primary_model=model_slug, messages=messages,
                    max_tokens=max_tokens, config=config, log=log, episode_id=episode_id, agent=agent,
                    response_format=response_format, provider=provider,
                )

            retry_after = getattr(exc, "retry_after", None)
            status_code = getattr(exc, "status_code", "timeout")
            delay = _retry_delay(attempt, retry_after)
            log.warn(
                "openrouter_retry_triggered", attempt_number=attempt + 1, trigger_status_code=status_code,
                delay_ms=int(delay * 1000), via_gate=True,
            )
            time.sleep(delay)
            attempt += 1


def call_claude(
    model_slug: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    config: Config,
    episode_id: Optional[str] = None,
    agent: Optional[str] = None,
    response_format: Optional[dict] = None,
    provider: Optional[dict] = None,
    allow_model_fallback: bool = True,
    max_attempts: Optional[int] = None,
    channel_id: Optional[str] = None,
) -> CallResult:
    """
    Call Claude via OpenRouter (primary), then config.OPENROUTER_FALLBACK_MODEL
    through OpenRouter (fallback, one attempt, on primary retry exhaustion,
    unless allow_model_fallback=False — see below). Anthropic direct is
    never called automatically — see module docstring.

    The caller is responsible for passing the correct model_slug for the
    assigned agent (see openrouter-integration.md §5.A). This function does
    not resolve agent-to-model assignments — it takes the slug directly.

    Args:
        model_slug:  OpenRouter slug, e.g. "anthropic/claude-sonnet-4.6"
        messages:    Conversation in OpenAI format — list of
                     {"role": "system"|"user"|"assistant", "content": "..."}
        max_tokens:  Maximum tokens to generate. Set per agent based on
                     expected output size.
        config:      Loaded Config instance — provides API keys, app metadata.
        episode_id:  Current episode identifier for log correlation.
        agent:       Calling agent name for log attribution.
        response_format: Optional OpenRouter/OpenAI-shaped response_format
                     dict (e.g. {"type": "json_schema", "json_schema": {...}})
                     forwarded verbatim into the request body. Omitted from
                     the body entirely when None (the default) — existing
                     callers that don't pass this get byte-for-byte the same
                     request body as before this parameter existed.
        provider:    Optional OpenRouter "provider" routing-preference dict
                     (e.g. {"require_parameters": True}) forwarded verbatim
                     into the request body. Omitted entirely when None.
        channel_id:  Optional (added for the Authorization Authority gate
                     path, plan §5.5/§6 Commit H -- mirrors hypogeum.muapi.
                     submit_scene()'s own channel_id parameter exactly).
                     When supplied AND a real, verified RunAuthorizationGrant
                     already resolves for (channel_id, episode_id), the call
                     routes through hypogeum.authorization_authority +
                     hypogeum.submission_gate instead of the legacy path
                     below. Every existing caller omits channel_id and is
                     completely unaffected; even a caller that supplies it
                     falls straight through to the unchanged legacy path
                     whenever no real grant exists.
        allow_model_fallback: When True (the default — unchanged behavior
                     for every existing caller), retry exhaustion on the
                     primary model attempts exactly one call to
                     config.OPENROUTER_FALLBACK_MODEL (a *different* model
                     slug) as described above. When False, retry exhaustion
                     raises FallbackFailed immediately instead — used by
                     callers that must never silently substitute a
                     different model (e.g. a request whose response_format
                     schema was generated specifically for model_slug).
                     This never disables OpenRouter's own provider-level
                     routing/failover across compatible endpoints of the
                     *same* model — that stays fully available via the
                     `provider` parameter above and is orthogonal to this
                     flag, which only gates the different-model fallback.
        max_attempts: When None (the default — unchanged behavior for every
                     existing caller), transient-error retry uses the
                     existing RETRY_MAX-derived budget (one initial attempt
                     plus up to RETRY_MAX retries against the SAME model,
                     each a separate requests.post call). When set to an
                     integer, caps the total number of requests.post calls
                     against the primary model at that many — max_attempts=1
                     means the first transient failure is treated as
                     immediately exhausted (zero retries, zero additional
                     HTTP calls) before whatever allow_model_fallback then
                     decides. OpenRouter's own provider-level routing among
                     compatible endpoints inside that one submitted request
                     is unaffected either way — this parameter only bounds
                     how many separate requests THIS function submits.

    Returns:
        CallResult on success.

    Raises:
        HardFailure        — 400, 403, malformed response (no retry/fallback)
        CreditsExhausted   — 402 (no retry/fallback; human must top up)
        ContentFiltered    — finish_reason == content_filter
        TruncatedResponse  — finish_reason == length
        EmptyResponse      — empty content field despite HTTP 200
        FallbackFailed     — 401 (same-credential fallback would be pointless),
                             or primary exhausted retries and either no
                             OPENROUTER_FALLBACK_MODEL is configured or it
                             also failed. Retry-eligible at the episode/queue
                             level (FailurePolicy.TRANSIENT by default) —
                             never falls through to Anthropic direct.
    """
    global _use_anthropic_for_run
    log = get_logger(episode_id=episode_id, agent=agent)

    # Pre-production (PRE_PLAN) reservation boundary (2026-08-28): episode
    # scoring, research synthesis, narrative/structure, and script
    # generation/critique/revision all run BEFORE the first authoritative
    # Production Plan exists. Before this paid OpenRouter call reaches the
    # network, establish/replay the ONE pre-production CHANNEL_WEEKLY hold
    # against the accepted weekly ledger. No-op once a canonical plan
    # exists (that is POST_PLAN work -- hypogeum.plan_reservation governs).
    # Fail closed on any weekly denial -> zero network.
    if channel_id and episode_id:
        from hypogeum.preplan_reservation import ensure_preproduction_reservation  # noqa: PLC0415
        try:
            ensure_preproduction_reservation(
                channel_id=channel_id, episode_id=episode_id, config=config, log=log,
            )
        except Exception as exc:  # noqa: BLE001 -- fail closed, no retry, no network
            raise HardFailure(f"pre_plan_reservation_denied: {exc}") from exc

    # Grant-derived gate path (Correction 3/5.5) -- mirrors hypogeum.
    # muapi.submit_scene()'s own branch exactly. Only reached when
    # channel_id is supplied AND a real, verified grant already
    # resolves for (channel_id, episode_id). Every existing caller
    # (none of which pass channel_id today) is completely unaffected.
    # Even a caller that DOES pass channel_id falls straight through
    # to the legacy path below whenever no real grant exists.
    if channel_id is not None:
        import hypogeum.authorization_authority as _authority
        _eid = episode_id or "unknown_episode"
        _now_iso = datetime.now(timezone.utc).isoformat()
        # ADR-016: an execution ADMITTED under a Channel Operating Grant
        # (persisted state -- hypogeum.runtime_admission, never a caller
        # flag) MUST route every paid PRE_PLAN call through the submission
        # gate. There is NO paid legacy-network fallback for it: if the
        # operating grant no longer resolves, fail closed here, before any
        # network I/O.
        _admission = None
        if episode_id:
            try:
                from hypogeum.runtime_admission import get_admission_binding  # noqa: PLC0415
                _admission = get_admission_binding(channel_id, episode_id)
            except Exception:  # noqa: BLE001 -- admission table absent => not admitted
                _admission = None
        if _admission is not None:
            grant = _authority.resolve_active_operating_grant(channel_id, at=_now_iso)
            if grant is None:
                raise HardFailure(
                    "admitted_autonomous_execution_without_active_operating_grant -- "
                    "fail closed, no paid legacy path"
                )
            return _call_claude_via_gate(
                channel_id=channel_id, grant=grant, model_slug=model_slug, messages=messages,
                max_tokens=max_tokens, config=config, episode_id=_eid, agent=agent,
                response_format=response_format, provider=provider,
                allow_model_fallback=allow_model_fallback, max_attempts=max_attempts,
            )
        grant = _authority.resolve_active_grant(channel_id, _eid, at=_now_iso)
        if grant is not None:
            return _call_claude_via_gate(
                channel_id=channel_id, grant=grant, model_slug=model_slug, messages=messages,
                max_tokens=max_tokens, config=config, episode_id=_eid, agent=agent,
                response_format=response_format, provider=provider,
                allow_model_fallback=allow_model_fallback, max_attempts=max_attempts,
            )

    # Estimate prompt size for pre-call log (actual count comes from response)
    prompt_chars = sum(len(m.get("content", "")) for m in messages)
    prompt_tokens_estimate = max(1, prompt_chars // 4)

    log.info(
        "openrouter_request_sent",
        model_slug=model_slug,
        prompt_tokens_estimate=prompt_tokens_estimate,
        via_fallback=_use_anthropic_for_run,
    )

    start_ms = _now_ms()

    # Apply the Anthropic prompt-caching content-block format to the system
    # message for supported models only (see module note above). Computed once
    # and reused for every dispatch below — including retries and fallback —
    # so caching behavior is identical across all of them. Unsupported models
    # get back the exact same messages list, unchanged.
    cached_messages = _apply_system_prompt_caching(messages, model_slug)

    # If the health check already marked OpenRouter unavailable for this run,
    # skip straight to Anthropic direct.
    if _use_anthropic_for_run:
        return _call_anthropic_direct(
            model_slug=model_slug,
            messages=cached_messages,
            max_tokens=max_tokens,
            config=config,
            log=log,
            start_ms=start_ms,
        )

    # --- OpenRouter path with up to RETRY_MAX retries (or, when the caller
    # passes max_attempts, a caller-scoped cap on total requests.post calls
    # against the primary model instead) ---
    effective_retry_max = RETRY_MAX if max_attempts is None else max(0, max_attempts - 1)
    attempt = 0
    while True:
        try:
            return _call_openrouter(
                model_slug=model_slug,
                messages=cached_messages,
                max_tokens=max_tokens,
                config=config,
                log=log,
                start_ms=start_ms,
                response_format=response_format,
                provider=provider,
            )

        except _AuthError:
            # 401 means the OPENROUTER_API_KEY credential itself is invalid —
            # every model slug shares that one credential, so trying a
            # different model through OpenRouter would fail identically.
            # Fail safely without attempting the OpenRouter fallback model
            # or Anthropic direct.
            log.warn(
                "openrouter_hard_failure",
                failure_type="auth_failure_401",
                http_status=401,
                action="no_fallback_same_credential",
            )
            raise FallbackFailed(
                "OpenRouter authentication failed (HTTP 401) — the "
                "OPENROUTER_API_KEY credential itself is invalid. No "
                "OpenRouter fallback model was attempted (it would use the "
                "same invalid credential); no Anthropic-direct fallback is "
                "attempted by default. Rotate OPENROUTER_API_KEY."
            )

        except (_RetryableError, requests.exceptions.RequestException) as exc:
            if attempt >= effective_retry_max:
                if not allow_model_fallback:
                    log.warn(
                        "openrouter_model_fallback_activated",
                        primary_model=model_slug,
                        fallback_model=None,
                        reason="retry_exhausted",
                        success=False,
                        fallback_disabled_by_caller=True,
                    )
                    raise FallbackFailed(
                        f"OpenRouter primary model '{model_slug}' failed and retry "
                        "budget is exhausted. Different-model fallback is disabled "
                        "for this call (allow_model_fallback=False) — "
                        "OpenRouter's own provider-level routing/failover across "
                        "compatible endpoints of this same model was already "
                        "available throughout the attempts above via the "
                        "`provider` parameter and is now also exhausted."
                    )
                # Retry budget on the primary model is exhausted — attempt
                # exactly one OpenRouter-native fallback (same endpoint,
                # same credential, different model slug) instead of
                # switching to Anthropic direct.
                return _try_openrouter_model_fallback(
                    primary_model=model_slug,
                    messages=messages,
                    max_tokens=max_tokens,
                    config=config,
                    log=log,
                    start_ms=start_ms,
                    reason="retry_exhausted",
                    response_format=response_format,
                    provider=provider,
                )

            retry_after = getattr(exc, "retry_after", None)
            status_code = getattr(exc, "status_code", "timeout")
            delay = _retry_delay(attempt, retry_after)
            log.warn(
                "openrouter_retry_triggered",
                attempt_number=attempt + 1,
                trigger_status_code=status_code,
                delay_ms=int(delay * 1000),
            )
            time.sleep(delay)
            attempt += 1


def _try_openrouter_model_fallback(
    primary_model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    config: Config,
    log: Logger,
    start_ms: int,
    reason: str,
    response_format: Optional[dict] = None,
    provider: Optional[dict] = None,
) -> CallResult:
    """
    On primary-model retry exhaustion: attempt exactly one call to
    config.OPENROUTER_FALLBACK_MODEL through the same OpenRouter endpoint
    and API key — never Anthropic direct (see docs/provider-model-director.md
    §8.3: fallback stays within OpenRouter's own model routing).

    messages is the original, un-cached message list — prompt caching is
    re-applied here for the fallback model's own slug, since cache_control
    is Anthropic-specific and the fallback model may not be an Anthropic
    model.

    Raises FallbackFailed (FailurePolicy.TRANSIENT by default — retry-
    eligible at the episode/queue level) if no fallback model is configured
    or the fallback call also fails. Never falls through to Anthropic
    direct.
    """
    fallback_model = config.OPENROUTER_FALLBACK_MODEL

    if not fallback_model:
        log.warn(
            "openrouter_model_fallback_activated",
            primary_model=primary_model,
            fallback_model=None,
            reason=reason,
            success=False,
        )
        raise FallbackFailed(
            f"OpenRouter primary model '{primary_model}' failed and no "
            "OPENROUTER_FALLBACK_MODEL is configured. No Anthropic-direct "
            "fallback is attempted by default — set OPENROUTER_FALLBACK_MODEL "
            "to an approved OpenRouter model slug to enable a fallback."
        )

    fallback_messages = _apply_system_prompt_caching(messages, fallback_model)

    try:
        result = _call_openrouter(
            model_slug=fallback_model,
            messages=fallback_messages,
            max_tokens=max_tokens,
            config=config,
            log=log,
            start_ms=start_ms,
            response_format=response_format,
            provider=provider,
        )
    except Exception as exc:
        log.warn(
            "openrouter_model_fallback_activated",
            primary_model=primary_model,
            fallback_model=fallback_model,
            reason=reason,
            success=False,
        )
        raise FallbackFailed(
            f"OpenRouter primary model '{primary_model}' failed and fallback "
            f"model '{fallback_model}' also failed: {exc}",
            original_error=exc,
        ) from exc

    log.warn(
        "openrouter_model_fallback_activated",
        primary_model=primary_model,
        fallback_model=fallback_model,
        reason=reason,
        success=True,
    )
    return result


def check_health(
    config: Config,
    episode_id: Optional[str] = None,
    agent: Optional[str] = None,
) -> bool:
    """
    Query GET /models to confirm OpenRouter is reachable and both target models
    are available. Sets _use_anthropic_for_run if OpenRouter is unavailable.

    Returns:
        True  — OpenRouter healthy, both Sonnet and Opus slugs present.
        False — degraded (models missing) or unavailable (non-200 / network error).
                When unavailable, all subsequent call_claude() calls in this
                run will route to Anthropic direct.

    Call once at pipeline startup, not before each agent call.
    """
    global _use_anthropic_for_run
    log = get_logger(episode_id=episode_id, agent=agent)

    headers = _build_headers(config)
    try:
        resp = requests.get(OPENROUTER_MODELS_URL, headers=headers, timeout=30)
    except requests.RequestException as exc:
        log.error(
            "openrouter_health_check_failed",
            failure_type="connection_error",
            error=str(exc),
            action="routing_all_calls_to_anthropic_direct",
        )
        _use_anthropic_for_run = True
        return False

    if resp.status_code != 200:
        log.error(
            "openrouter_health_check_failed",
            failure_type="non_200_response",
            http_status=resp.status_code,
            action="routing_all_calls_to_anthropic_direct",
        )
        _use_anthropic_for_run = True
        return False

    try:
        model_ids = {m["id"] for m in resp.json().get("data", [])}
    except Exception:
        model_ids = set()

    missing = [
        slug
        for slug in (config.OPENROUTER_SONNET_MODEL_ID, config.OPENROUTER_OPUS_MODEL_ID)
        if slug not in model_ids
    ]

    if missing:
        log.warn(
            "openrouter_health_check_degraded",
            missing_models=missing,
            action="proceeding_with_caution",
        )
        return False

    log.info("openrouter_health_check_passed", model_count=len(model_ids))
    return True


# ---------------------------------------------------------------------------
# Internal: OpenRouter call
# ---------------------------------------------------------------------------

def _call_openrouter(
    model_slug: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    config: Config,
    log: Logger,
    start_ms: int,
    response_format: Optional[dict] = None,
    provider: Optional[dict] = None,
) -> CallResult:
    headers = _build_headers(config)
    body = {
        "model": model_slug,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    # Both omitted entirely (not sent as null) when the caller doesn't pass
    # them — every existing caller gets byte-for-byte the same body as
    # before either parameter existed.
    if response_format is not None:
        body["response_format"] = response_format
    if provider is not None:
        body["provider"] = provider

    # Let requests.Timeout propagate — outer loop catches it
    resp = requests.post(
        OPENROUTER_CHAT_URL,
        headers=headers,
        json=body,
        timeout=REQUEST_TIMEOUT,
    )

    if resp.status_code == 401:
        raise _AuthError()

    if resp.status_code == 402:
        log.error(
            "openrouter_hard_failure",
            failure_type="credits_exhausted",
            http_status=402,
            action="halt_pipeline_top_up_account",
        )
        raise CreditsExhausted(
            "OpenRouter credits exhausted (HTTP 402). "
            "Top up account balance before re-running the pipeline.",
            http_status=402,
        )

    if resp.status_code in (400, 403):
        log.warn(
            "openrouter_hard_failure",
            failure_type=f"http_{resp.status_code}",
            http_status=resp.status_code,
            action="no_retry",
        )
        raise HardFailure(
            f"OpenRouter returned {resp.status_code}: {resp.text[:300]}",
            http_status=resp.status_code,
        )

    if resp.status_code in RETRYABLE_STATUS_CODES:
        raise _RetryableError(resp.status_code, _parse_retry_after(resp))

    if resp.status_code != 200:
        raise HardFailure(
            f"Unexpected OpenRouter status {resp.status_code}: {resp.text[:300]}",
            http_status=resp.status_code,
        )

    return _parse_openrouter_response(
        data=resp.json(),
        model_slug=model_slug,
        start_ms=start_ms,
        log=log,
        max_tokens=max_tokens,
    )


def _parse_openrouter_response(
    data: dict,
    model_slug: str,
    start_ms: int,
    log: Logger,
    max_tokens: int,
) -> CallResult:
    elapsed_ms = _now_ms() - start_ms

    try:
        choice = data["choices"][0]
        finish_reason: str = choice.get("finish_reason", "") or ""
        content: str = (choice.get("message", {}).get("content") or "")
        model_used: str = data.get("model", model_slug)
        usage_raw = data.get("usage", {})
        usage = {
            "prompt_tokens":     int(usage_raw.get("prompt_tokens", 0)),
            "completion_tokens": int(usage_raw.get("completion_tokens", 0)),
            "total_tokens":      int(usage_raw.get("total_tokens", 0)),
        }
    except (KeyError, IndexError, TypeError) as exc:
        # A dict-shaped top-level "error" field means the provider told us
        # exactly what went wrong -- classify and raise using those real
        # diagnostic fields (see _classify_and_raise_provider_error()
        # above) instead of collapsing everything into a blanket retry.
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            _classify_and_raise_provider_error(data, log)

        log.error(
            "openrouter_malformed_choices",
            error=str(exc),
            top_level_keys=repr(list(data.keys()))[:200],
            choices_present="choices" in data,
            choices_count=(
                len(data["choices"])
                if isinstance(data.get("choices"), list)
                else None
            ),
            has_error_field=bool(data.get("error")),
            action="retrying",
        )
        # No "error" field at all -- a genuinely unstructured malformed
        # response (not a provider error we can classify). Transient
        # provider glitch — not a permanent hard failure. Raise retryable
        # so call_claude() retries once, then falls back to
        # OPENROUTER_FALLBACK_MODEL if configured (see _try_openrouter_model_fallback()).
        raise _RetryableError(status_code=200)

    if finish_reason == "content_filter":
        log.error(
            "openrouter_hard_failure",
            failure_type="content_filter",
            http_status=200,
            action="investigate_system_prompt",
        )
        raise ContentFiltered(
            "Response blocked by OpenRouter content filter. "
            "Investigate system prompt — pipeline history/mystery content should not trigger filters.",
            http_status=200,
        )

    if finish_reason == "length":
        log.warn(
            "openrouter_truncated_response",
            completion_tokens=usage["completion_tokens"],
            max_tokens_set=max_tokens,
            action="do_not_pass_to_next_agent",
        )
        raise TruncatedResponse(usage["completion_tokens"], max_tokens)

    if not content:
        raise EmptyResponse(
            "OpenRouter returned HTTP 200 but message content is empty.",
            http_status=200,
        )

    log.info(
        "openrouter_response_received",
        model_slug=model_slug,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        total_tokens=usage["total_tokens"],
        finish_reason=finish_reason,
        response_time_ms=elapsed_ms,
    )

    return CallResult(
        content=content,
        usage=usage,
        model_used=model_used,
        finish_reason=finish_reason,
        response_time_ms=elapsed_ms,
        via_fallback=False,
        estimated_cost_usd=_estimate_cost(model_slug, usage),
    )


# ---------------------------------------------------------------------------
# Internal: Anthropic direct fallback
# ---------------------------------------------------------------------------

def _call_anthropic_direct(
    model_slug: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    config: Config,
    log: Logger,
    start_ms: int,
) -> CallResult:
    if not config.ANTHROPIC_API_KEY:
        raise FallbackFailed(
            "Anthropic direct fallback required but ANTHROPIC_API_KEY is not set. "
            "Populate ANTHROPIC_API_KEY in .env to enable the fallback path."
        )

    anthropic_model_id = _translate_slug_to_anthropic_id(model_slug)
    headers = {
        "x-api-key": config.ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "Content-Type": "application/json",
    }

    # Translate OpenAI-format messages → Anthropic Messages API format.
    # System message is a top-level field; user/assistant turns stay in messages[].
    # system_content may be a plain string (unsupported models — unchanged
    # behavior) or a cache_control content-block array (see
    # _apply_system_prompt_caching()); either shape is valid for body["system"].
    system_content: Any = ""
    user_messages: list[dict[str, str]] = []
    for m in messages:
        if m.get("role") == "system":
            system_content = m.get("content", "")
        else:
            user_messages.append({"role": m["role"], "content": m.get("content", "")})

    body: dict[str, Any] = {
        "model": anthropic_model_id,
        "max_tokens": max_tokens,
        "messages": user_messages,
    }
    if system_content:
        body["system"] = system_content

    try:
        resp = requests.post(
            ANTHROPIC_MESSAGES_URL,
            headers=headers,
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise FallbackFailed(
            f"Anthropic direct fallback network error: {exc}",
            original_error=exc,
        )

    if resp.status_code != 200:
        raise FallbackFailed(
            f"Anthropic direct fallback returned HTTP {resp.status_code}: {resp.text[:300]}"
        )

    try:
        data = resp.json()
        # Anthropic format: {"content": [{"type": "text", "text": "..."}], ...}
        content_blocks = data.get("content", [])
        content = "".join(
            block.get("text", "")
            for block in content_blocks
            if block.get("type") == "text"
        )
        # Anthropic uses "end_turn"; normalise to "stop" for consistency
        stop_reason = data.get("stop_reason", "")
        finish_reason = "stop" if stop_reason == "end_turn" else (stop_reason or "")
        model_used: str = data.get("model", anthropic_model_id)

        usage_raw = data.get("usage", {})
        input_tokens  = int(usage_raw.get("input_tokens", 0))
        output_tokens = int(usage_raw.get("output_tokens", 0))
        usage = {
            "prompt_tokens":     input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens":      input_tokens + output_tokens,
        }
    except Exception as exc:
        raise FallbackFailed(
            f"Failed to parse Anthropic direct response: {exc}",
            original_error=exc,
        )

    elapsed_ms = _now_ms() - start_ms

    # Anthropic uses "max_tokens" as its stop reason when truncated
    if stop_reason == "max_tokens":
        log.warn(
            "openrouter_truncated_response",
            completion_tokens=usage["completion_tokens"],
            max_tokens_set=max_tokens,
            action="do_not_pass_to_next_agent",
            via_fallback=True,
        )
        raise TruncatedResponse(usage["completion_tokens"], max_tokens)

    if not content:
        raise EmptyResponse(
            "Anthropic direct fallback returned HTTP 200 but content is empty.",
            http_status=200,
        )

    log.info(
        "openrouter_response_received",
        model_slug=model_slug,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        total_tokens=usage["total_tokens"],
        finish_reason=finish_reason,
        response_time_ms=elapsed_ms,
        via_fallback=True,
    )

    return CallResult(
        content=content,
        usage=usage,
        model_used=model_used,
        finish_reason=finish_reason,
        response_time_ms=elapsed_ms,
        via_fallback=True,
        estimated_cost_usd=_estimate_cost(model_slug, usage),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_headers(config: Config) -> dict[str, str]:
    headers: dict[str, str] = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    if config.OPENROUTER_APP_URL:
        headers["HTTP-Referer"] = config.OPENROUTER_APP_URL
    if config.OPENROUTER_APP_NAME:
        headers["X-Title"] = config.OPENROUTER_APP_NAME
    return headers


def _translate_slug_to_anthropic_id(slug: str) -> str:
    """
    Translate an OpenRouter slug to an Anthropic API model ID.

    Examples:
        "anthropic/claude-sonnet-4.6" → "claude-sonnet-4-6"
        "anthropic/claude-opus-4.6"   → "claude-opus-4-6"
    """
    if "/" in slug:
        slug = slug.split("/", 1)[1]
    return slug.replace(".", "-")


def _retry_delay(attempt: int, retry_after: Optional[int]) -> float:
    """
    Exponential backoff with ±15% jitter.

    attempt=0 → base 5s; attempt=1 → base 10s (capped at 30s).
    If Retry-After is present, the computed delay is floored at that value.
    """
    base = min(RETRY_INITIAL_DELAY * (RETRY_MULTIPLIER ** attempt), RETRY_MAX_DELAY)
    jitter = base * RETRY_JITTER_FRACTION * (2.0 * random.random() - 1.0)
    delay = base + jitter
    if retry_after is not None:
        delay = max(delay, float(retry_after))
    return delay


def _parse_retry_after(resp: requests.Response) -> Optional[int]:
    """Extract Retry-After header as integer seconds, or None if absent/unparseable."""
    value = resp.headers.get("Retry-After")
    if value is not None:
        try:
            return int(value)
        except ValueError:
            pass
    return None


def _estimate_cost(model_slug: str, usage: dict[str, int]) -> float:
    """Rough USD cost estimate from _COST_TABLE. Returns 0.0 for unknown slugs."""
    rates = _COST_TABLE.get(model_slug)
    if not rates:
        return 0.0
    return (
        usage.get("prompt_tokens", 0) * rates["prompt"]
        + usage.get("completion_tokens", 0) * rates["completion"]
    )


def _now_ms() -> int:
    """Monotonic clock in milliseconds."""
    return int(time.monotonic() * 1000)
