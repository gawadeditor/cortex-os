"""
hypogeum/openrouter.py — OpenRouter API client

Single integration point for all Claude model calls in the pipeline.
All 9 agents call Claude through this module — never via requests directly.

Primary path:  OpenRouter chat completions (OpenAI-compatible format)
Fallback path: Anthropic Messages API direct (triggered by 401 or retry exhaustion)

External dependency: requests

Logging events emitted:
  openrouter_request_sent       — info  — before each API call
  openrouter_response_received  — info  — on successful response
  openrouter_retry_triggered    — warn  — before each retry attempt
  openrouter_hard_failure       — error — on 400/402/403/content_filter
  openrouter_fallback_activated — warn  — when switching to Anthropic direct
  openrouter_truncated_response — warn  — when finish_reason == "length"

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
    Anthropic direct fallback also failed — pipeline cannot proceed.
    No further retry or fallback is attempted.
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
# Public API
# ---------------------------------------------------------------------------

def call_claude(
    model_slug: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    config: Config,
    episode_id: Optional[str] = None,
    agent: Optional[str] = None,
) -> CallResult:
    """
    Call Claude via OpenRouter (primary) or Anthropic direct (fallback).

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

    Returns:
        CallResult on success.

    Raises:
        HardFailure        — 400, 403, malformed response (no retry/fallback)
        CreditsExhausted   — 402 (no retry/fallback; human must top up)
        ContentFiltered    — finish_reason == content_filter
        TruncatedResponse  — finish_reason == length
        EmptyResponse      — empty content field despite HTTP 200
        FallbackFailed     — OpenRouter failed and Anthropic direct also failed
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

    # If the health check already marked OpenRouter unavailable for this run,
    # skip straight to Anthropic direct.
    if _use_anthropic_for_run:
        return _call_anthropic_direct(
            model_slug=model_slug,
            messages=messages,
            max_tokens=max_tokens,
            config=config,
            log=log,
            start_ms=start_ms,
        )

    # --- OpenRouter path with up to RETRY_MAX retries ---
    attempt = 0
    while True:
        try:
            return _call_openrouter(
                model_slug=model_slug,
                messages=messages,
                max_tokens=max_tokens,
                config=config,
                log=log,
                start_ms=start_ms,
            )

        except _AuthError:
            log.warn(
                "openrouter_fallback_activated",
                reason="auth_failure_401",
                fallback_target="anthropic_direct",
            )
            _use_anthropic_for_run = True
            return _call_anthropic_direct(
                model_slug=model_slug,
                messages=messages,
                max_tokens=max_tokens,
                config=config,
                log=log,
                start_ms=start_ms,
            )

        except (_RetryableError, requests.Timeout) as exc:
            if attempt >= RETRY_MAX:
                # Retry budget exhausted — switch to Anthropic direct
                log.warn(
                    "openrouter_fallback_activated",
                    reason="retry_exhausted",
                    fallback_target="anthropic_direct",
                )
                _use_anthropic_for_run = True
                return _call_anthropic_direct(
                    model_slug=model_slug,
                    messages=messages,
                    max_tokens=max_tokens,
                    config=config,
                    log=log,
                    start_ms=start_ms,
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
    messages: list[dict[str, str]],
    max_tokens: int,
    config: Config,
    log: Logger,
    start_ms: int,
) -> CallResult:
    headers = _build_headers(config)
    body = {
        "model": model_slug,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
    }

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
        # Raise retryable so call_claude() retries once then falls back to Anthropic.
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
    messages: list[dict[str, str]],
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
    system_content = ""
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
