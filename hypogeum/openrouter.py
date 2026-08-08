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

import random
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

from hypogeum.config import Config
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
        # Transient provider glitch — not a permanent hard failure.
        # Raise retryable so call_claude() retries once, then falls back to
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
