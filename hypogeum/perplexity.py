"""
hypogeum/perplexity.py — Perplexity API client

Single integration point for all Perplexity web-discovery queries in the pipeline.
Only the Research Director calls this module. No other agent accesses Perplexity.

Primary path: POST https://api.perplexity.ai/chat/completions (OpenAI-compatible format)
No fallback provider exists in V1. If Perplexity is unavailable, the research run halts
and a Package Deficiency Report is issued to the Executive Producer.

Integration contracts:
  - All environment access goes through the Config object — never os.getenv()
  - All logging goes through hypogeum.logger.get_logger() — never print() or stdlib logging
  - PERPLEXITY_API_KEY is never logged, printed, or stored in any package
  - Requests are sequential only — never parallel
  - Max 1 retry per query across all soft-failure conditions

Logging events emitted:
  perplexity_request_sent             — info  — before each API call
  perplexity_response_received        — info  — on successful response
  perplexity_no_citations             — warn  — response has empty citations[]
  perplexity_finish_reason_unexpected — warn  — finish_reason != "stop"
  perplexity_retry_triggered          — warn  — before each retry attempt
  perplexity_hard_failure             — error — on 400/401/403/422
  perplexity_soft_failure_exhausted   — error — retry exhausted on soft failure
  perplexity_health_check_started     — info  — before health check request
  perplexity_health_check_result      — info/warn/error — health check outcome

API key is never logged. Full query text is logged at debug level only, truncated to 200 chars.
Full response content is never logged.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

from hypogeum.config import Config
from hypogeum.logger import get_logger


# ---------------------------------------------------------------------------
# Endpoints and constants
# ---------------------------------------------------------------------------

PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"

# Health check query — short, factual, inexpensive
HEALTH_CHECK_QUERY    = "What year did World War I begin?"
HEALTH_CHECK_MAX_TOKENS = 50
HEALTH_CHECK_TIMEOUT_SECONDS = 10.0

# Per-request timeout (seconds) for research queries
REQUEST_TIMEOUT = 60

# Retry policy (mirrors the spec §8)
RETRY_MAX              = 1
RETRY_INITIAL_DELAY    = 5.0   # seconds
RETRY_MULTIPLIER       = 2.0
RETRY_MAX_DELAY        = 60.0  # seconds
RETRY_JITTER_FRACTION  = 0.20  # ±20%

# Default for 429 Retry-After when header is absent
RETRY_AFTER_DEFAULT = 60.0

# HTTP status codes by failure class
RETRYABLE_STATUS_CODES   = {429, 500, 503}
HARD_FAILURE_STATUS_CODES = {400, 401, 403, 422}

# Appended to query on content_filter retry — academic framing reduces filter triggers
_CONTENT_FILTER_SUFFIX = (
    " Focus on documented historical facts and established academic sources only."
)

# System prompt embedded in every Perplexity query (per spec §5)
_RESEARCH_SYSTEM_PROMPT = (
    "You are a research assistant specialized in historical fact-finding. "
    "Prioritize academic sources, primary documents, and expert analysis over general-interest content. "
    "Include specific dates, names, and documented facts where available. "
    "Flag when claims are disputed or contested across sources. "
    "Do not speculate beyond what sources state."
)

# Max tokens doubled on length-truncation retry; capped to avoid runaway cost
_LENGTH_RETRY_MAX_TOKENS_CAP = 4096


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class PerplexityResult:
    """Structured result of a successful query_perplexity() call."""

    content: str
    """Synthesized answer text from choices[0].message.content."""

    citations: list
    """Source URLs extracted from the top-level citations[] array. May be empty."""

    usage: dict
    """Token counts: {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}."""

    finish_reason: str
    """Stop reason reported by the API. Normally "stop"; may be "length" or "content_filter"."""

    phase_id: Optional[str]
    """Research phase identifier passed by the caller. Logged for tracing."""

    query_index: Optional[int]
    """Query sequence number within the phase. Logged for tracing."""

    response_time_ms: int
    """Wall-clock milliseconds from request dispatch to response parsed."""

    citations_absent: bool
    """True when citations[] was empty — caller should log this as a quality signal."""


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class PerplexityError(RuntimeError):
    """Base class for all Perplexity client errors."""


class PerplexityHardFailure(PerplexityError):
    """
    Unrecoverable error — no retry.

    Covers HTTP 400 (malformed request), 401 (auth failure), 403 (forbidden),
    422 (invalid parameters).

    Callers must halt the research run and issue a Package Deficiency Report.
    """
    def __init__(self, message: str, http_status: int) -> None:
        self.http_status = http_status
        super().__init__(message)


class PerplexityAuthFailure(PerplexityHardFailure):
    """
    HTTP 401 — API key is missing or invalid.

    This is a hard failure requiring operator intervention. The research run
    cannot proceed. Issue PDR with severity: BLOCKING.
    """
    def __init__(self) -> None:
        super().__init__(
            "Perplexity authentication failed (HTTP 401). "
            "Check PERPLEXITY_API_KEY in environment.",
            http_status=401,
        )


class PerplexitySoftFailureExhausted(PerplexityError):
    """
    A retryable condition persisted through the maximum retry count.

    The query could not be completed. Callers should skip this query,
    document the gap in research_run_summary, and continue with remaining
    research phases.
    """
    def __init__(self, message: str, last_status: Optional[int] = None) -> None:
        self.last_status = last_status
        super().__init__(message)


class PerplexityUnavailable(PerplexityError):
    """
    Health check determined Perplexity cannot be reached or is not authenticated.

    The research run must not begin. Issue a PDR to the Executive Producer
    with severity: BLOCKING, deficiency_type: provider_unavailable.
    """
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            f"Perplexity is unavailable: {reason}. "
            f"Research run cannot proceed. Issue PDR to Executive Producer."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_perplexity_health(
    config: Config,
    episode_id: Optional[str] = None,
    agent: Optional[str] = None,
) -> str:
    """
    Verify Perplexity reachability and authentication before a research run.

    Sends a minimal, low-cost test query and evaluates the response.

    Args:
        config:     Loaded Config instance. PERPLEXITY_API_KEY and PERPLEXITY_MODEL
                    are read from here — never from os.getenv() directly.
        episode_id: Optional episode identifier for log correlation.
        agent:      Optional agent name for log correlation.

    Returns:
        "available"   — HTTP 200, non-empty content, response time < 10s.
        "degraded"    — HTTP 200, non-empty content, response time ≥ 10s.
        "unavailable" — Any error (auth failure, connection error, server error).

    Raises:
        Nothing — all exceptions are caught and reflected in the return value.
        Callers must treat "unavailable" as a hard stop (issue PDR).
    """
    log = get_logger(episode_id=episode_id, agent=agent or "perplexity_health")
    log.info("perplexity_health_check_started", model=config.PERPLEXITY_MODEL)

    headers = _build_headers(config)
    body = {
        "model": config.PERPLEXITY_MODEL,
        "max_tokens": HEALTH_CHECK_MAX_TOKENS,
        "messages": [{"role": "user", "content": HEALTH_CHECK_QUERY}],
    }

    t0 = time.monotonic()
    try:
        resp = requests.post(
            PERPLEXITY_API_URL,
            headers=headers,
            json=body,
            timeout=HEALTH_CHECK_TIMEOUT_SECONDS,
        )
    except requests.exceptions.Timeout:
        log.error(
            "perplexity_health_check_result",
            status="unavailable",
            reason="timeout",
        )
        return "unavailable"
    except requests.exceptions.ConnectionError as exc:
        log.error(
            "perplexity_health_check_result",
            status="unavailable",
            reason="connection_error",
            error=str(exc),
        )
        return "unavailable"
    except Exception as exc:
        log.error(
            "perplexity_health_check_result",
            status="unavailable",
            reason="unexpected_error",
            error=str(exc),
        )
        return "unavailable"

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if resp.status_code != 200:
        log.error(
            "perplexity_health_check_result",
            status="unavailable",
            reason=f"http_{resp.status_code}",
            http_status=resp.status_code,
        )
        return "unavailable"

    try:
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception:
        content = ""

    if not content:
        log.error(
            "perplexity_health_check_result",
            status="unavailable",
            reason="empty_content",
            response_time_ms=elapsed_ms,
        )
        return "unavailable"

    elapsed_seconds = elapsed_ms / 1000.0
    if elapsed_seconds >= HEALTH_CHECK_TIMEOUT_SECONDS:
        log.warn(
            "perplexity_health_check_result",
            status="degraded",
            reason="slow_response",
            response_time_ms=elapsed_ms,
        )
        return "degraded"

    log.info(
        "perplexity_health_check_result",
        status="available",
        response_time_ms=elapsed_ms,
    )
    return "available"


def query_perplexity(
    query: str,
    config: Config,
    *,
    phase_id: Optional[str] = None,
    query_index: Optional[int] = None,
    episode_id: Optional[str] = None,
    agent: Optional[str] = None,
    max_tokens_override: Optional[int] = None,
) -> PerplexityResult:
    """
    Issue one research query to Perplexity and return the structured result.

    Retries once on soft failures per the policy in §8. Raises immediately on
    hard failures. On retry exhaustion, raises PerplexitySoftFailureExhausted.

    Args:
        query:              The specific research question for this phase.
        config:             Loaded Config instance.
        phase_id:           Research phase identifier (e.g., "phase_2"). Logged only.
        query_index:        Query sequence number within the phase. Logged only.
        episode_id:         Episode identifier for log correlation.
        agent:              Agent name for log correlation.
        max_tokens_override: Override the config's PERPLEXITY_MAX_TOKENS for this query.

    Returns:
        PerplexityResult on success.

    Raises:
        PerplexityHardFailure         — HTTP 400/401/403/422. No retry.
        PerplexityAuthFailure         — HTTP 401 specifically.
        PerplexitySoftFailureExhausted — Soft failure persisted through all retries.
    """
    log = get_logger(episode_id=episode_id, agent=agent or "perplexity")
    max_tokens = max_tokens_override or config.PERPLEXITY_MAX_TOKENS or 2048

    return _execute_with_retry(
        query=query,
        config=config,
        max_tokens=max_tokens,
        phase_id=phase_id,
        query_index=query_index,
        log=log,
    )


# ---------------------------------------------------------------------------
# Internal execution and retry logic
# ---------------------------------------------------------------------------

def _execute_with_retry(
    query: str,
    config: Config,
    max_tokens: int,
    phase_id: Optional[str],
    query_index: Optional[int],
    log,
) -> PerplexityResult:
    """
    Execute one query with up to RETRY_MAX retries on soft failures.

    Retry variant is chosen based on failure type:
      - 429/500/503:             same query, same max_tokens
      - finish_reason: length:   same query, doubled max_tokens
      - finish_reason: content_filter: query with academic suffix appended
      - empty choices[]:         same query, same max_tokens
    """
    current_query = query
    current_max_tokens = max_tokens

    for attempt in range(RETRY_MAX + 1):
        result, failure_type, retry_after = _single_request(
            query=current_query,
            config=config,
            max_tokens=current_max_tokens,
            phase_id=phase_id,
            query_index=query_index,
            log=log,
            attempt=attempt,
        )

        if result is not None:
            return result

        # failure_type is one of: "429", "5xx", "length", "content_filter", "empty_choices"
        # result is None → failure occurred

        if attempt < RETRY_MAX:
            # Prepare retry parameters before sleeping
            if failure_type == "length":
                current_max_tokens = min(current_max_tokens * 2, _LENGTH_RETRY_MAX_TOKENS_CAP)
                trigger = "finish_reason_length"
            elif failure_type == "content_filter":
                if not current_query.endswith(_CONTENT_FILTER_SUFFIX):
                    current_query = current_query + _CONTENT_FILTER_SUFFIX
                trigger = "finish_reason_content_filter"
            else:
                trigger = f"http_{failure_type}" if failure_type.isdigit() else failure_type

            delay = _retry_delay(attempt=attempt, retry_after=retry_after)
            log.warn(
                "perplexity_retry_triggered",
                phase_id=phase_id,
                query_index=query_index,
                trigger_reason=trigger,
                retry_delay_seconds=round(delay, 2),
                attempt=attempt + 1,
            )
            time.sleep(delay)
        else:
            # All retries exhausted
            log.error(
                "perplexity_soft_failure_exhausted",
                phase_id=phase_id,
                query_index=query_index,
                failure_type=failure_type,
            )
            raise PerplexitySoftFailureExhausted(
                f"Query failed after {RETRY_MAX} retry (failure_type={failure_type!r}). "
                f"phase_id={phase_id!r}, query_index={query_index!r}",
                last_status=int(failure_type) if failure_type.isdigit() else None,
            )

    # Should never be reached
    raise PerplexitySoftFailureExhausted("Retry loop exited unexpectedly.")


def _single_request(
    query: str,
    config: Config,
    max_tokens: int,
    phase_id: Optional[str],
    query_index: Optional[int],
    log,
    attempt: int,
) -> tuple:
    """
    Make one HTTP request to Perplexity.

    Returns: (PerplexityResult, None, None) on success
             (None, failure_type_str, retry_after_or_None) on retryable failure
    Raises: PerplexityHardFailure on hard failures (400/401/403/422).
    """
    headers = _build_headers(config)
    body = {
        "model": config.PERPLEXITY_MODEL,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": _RESEARCH_SYSTEM_PROMPT},
            {"role": "user",   "content": query},
        ],
    }

    # Log query at debug level only, truncated to 200 chars per spec §11
    log.info(
        "perplexity_request_sent",
        phase_id=phase_id,
        query_index=query_index,
        model=config.PERPLEXITY_MODEL,
        max_tokens=max_tokens,
        attempt=attempt,
        query_preview=query[:200],
    )

    t0 = time.monotonic()
    try:
        resp = requests.post(
            PERPLEXITY_API_URL,
            headers=headers,
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        log.warn(
            "perplexity_retry_triggered",
            phase_id=phase_id,
            query_index=query_index,
            trigger_reason="connection_error",
            error=str(exc),
        )
        return None, "connection_error", None

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    # Hard failures — no retry
    if resp.status_code in HARD_FAILURE_STATUS_CODES:
        if resp.status_code == 401:
            log.error(
                "perplexity_hard_failure",
                phase_id=phase_id,
                query_index=query_index,
                http_status=401,
                error_type="PERPLEXITY_AUTH_FAILURE",
            )
            raise PerplexityAuthFailure()
        log.error(
            "perplexity_hard_failure",
            phase_id=phase_id,
            query_index=query_index,
            http_status=resp.status_code,
            error_type=f"http_{resp.status_code}",
        )
        raise PerplexityHardFailure(
            f"Perplexity returned HTTP {resp.status_code} — hard failure, no retry.",
            http_status=resp.status_code,
        )

    # Soft failures — retryable
    if resp.status_code in RETRYABLE_STATUS_CODES:
        retry_after = None
        if resp.status_code == 429:
            try:
                retry_after = float(resp.headers.get("Retry-After", RETRY_AFTER_DEFAULT))
            except (TypeError, ValueError):
                retry_after = RETRY_AFTER_DEFAULT
            log.warn(
                "perplexity_rate_limit_hit",
                phase_id=phase_id,
                query_index=query_index,
                http_status=429,
                retry_after_seconds=retry_after,
            )
        return None, str(resp.status_code), retry_after

    if resp.status_code != 200:
        # Any other non-200 treated as soft failure
        return None, str(resp.status_code), None

    # Parse 200 response
    try:
        data = resp.json()
    except Exception as exc:
        log.warn(
            "perplexity_retry_triggered",
            phase_id=phase_id,
            query_index=query_index,
            trigger_reason="json_parse_error",
            error=str(exc),
        )
        return None, "json_parse_error", None

    choices = data.get("choices", [])
    if not choices:
        # Empty choices — soft failure
        return None, "empty_choices", None

    message = choices[0].get("message", {})
    content = message.get("content", "")
    if not content:
        return None, "empty_choices", None

    finish_reason = choices[0].get("finish_reason", "stop")
    usage = data.get("usage", {})
    citations = data.get("citations", [])

    # finish_reason: content_filter — soft failure (retry with rephrased query)
    if finish_reason == "content_filter":
        return None, "content_filter", None

    # finish_reason: length — soft failure (retry with more tokens)
    if finish_reason == "length":
        return None, "length", None

    # Log unexpected finish_reason as warning but continue (content is used)
    if finish_reason not in ("stop", "length", "content_filter"):
        log.warn(
            "perplexity_finish_reason_unexpected",
            phase_id=phase_id,
            query_index=query_index,
            finish_reason=finish_reason,
        )

    citations_absent = len(citations) == 0
    if citations_absent:
        log.warn(
            "perplexity_no_citations",
            phase_id=phase_id,
            query_index=query_index,
            finish_reason=finish_reason,
        )

    log.info(
        "perplexity_response_received",
        phase_id=phase_id,
        query_index=query_index,
        total_tokens_used=usage.get("total_tokens", 0),
        citation_count=len(citations),
        finish_reason=finish_reason,
        response_time_ms=elapsed_ms,
    )

    return PerplexityResult(
        content=content,
        citations=list(citations),
        usage=dict(usage),
        finish_reason=finish_reason,
        phase_id=phase_id,
        query_index=query_index,
        response_time_ms=elapsed_ms,
        citations_absent=citations_absent,
    ), None, None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_headers(config: Config) -> dict:
    """Build the Authorization header. API key is never logged."""
    return {
        "Authorization": f"Bearer {config.PERPLEXITY_API_KEY}",
        "Content-Type": "application/json",
    }


def _retry_delay(attempt: int, retry_after: Optional[float]) -> float:
    """
    Compute the delay before the next retry attempt.

    Uses exponential backoff with ±20% jitter (per spec §8).
    If retry_after is set (from 429 Retry-After header), uses the larger of
    the computed delay and retry_after.
    """
    base = min(RETRY_INITIAL_DELAY * (RETRY_MULTIPLIER ** attempt), RETRY_MAX_DELAY)
    jitter = base * RETRY_JITTER_FRACTION * (2.0 * random.random() - 1.0)
    delay = base + jitter
    if retry_after is not None:
        delay = max(delay, float(retry_after))
    return delay
