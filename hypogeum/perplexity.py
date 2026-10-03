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
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import requests

from hypogeum.config import Config
from hypogeum.execution_envelope import GenerationExecutionEnvelope
from hypogeum.logger import get_logger
import hypogeum.finance as _finance
from hypogeum.prompt_loader import load_prompt

# Cost estimate for Perplexity sonar-pro (USD per token, combined input+output).
# sonar-pro pricing: $1/1M input tokens + $5/1M output tokens ≈ $3/1M blended.
_PERPLEXITY_USD_PER_TOKEN = 3e-6


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

# System prompt loaded from hypogeum/prompts/perplexity_research.txt
_PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "perplexity_research.txt"
_RESEARCH_SYSTEM_PROMPT = load_prompt(_PROMPT_FILE)

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
    channel_id: Optional[str] = None,
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

    # The health probe is a real (minimal) paid Perplexity completion --
    # a PRE_PLAN reachable provider call. Establish/replay the ONE
    # pre-production CHANNEL_WEEKLY hold first; fail closed -> "unavailable"
    # (a hard stop for the caller), zero network.
    if channel_id and episode_id:
        from hypogeum.preplan_reservation import ensure_preproduction_reservation  # noqa: PLC0415
        try:
            ensure_preproduction_reservation(
                channel_id=channel_id, episode_id=episode_id, config=config, log=log,
            )
        except Exception as exc:  # noqa: BLE001 -- fail closed, zero network
            log.error("perplexity_health_check_result", status="unavailable",
                      reason="pre_plan_reservation_denied", error=str(exc))
            return "unavailable"

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


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ASSETS_ROOT = _PROJECT_ROOT / ".hypogeum-assets"

_HYPOGEUM_WORKFLOW_ID = "hypogeum_main_production"
_HYPOGEUM_WORKFLOW_VERSION = "1.0.0"
_RESEARCH_PROVIDER = "perplexity"
_RESEARCH_PROVIDER_OPERATION = "research.query"
_RESEARCH_COMPILER_IDENTITY = "hypogeum.perplexity.query_perplexity"
_RESEARCH_ENVELOPE_SCHEMA_VERSION = "1.0"

# Conservative per-token rate used to pre-estimate a max_cost_per_call
# ceiling BEFORE the real usage is known -- see
# hypogeum.openrouter._estimate_max_cost_ceiling()'s identical rationale.
# _PERPLEXITY_USD_PER_TOKEN already blends input+output, so a flat
# per-token ceiling covering the full max_tokens is already conservative.
_RESEARCH_CEILING_USD_PER_TOKEN = _PERPLEXITY_USD_PER_TOKEN


def _canonical_json_bytes(payload: dict) -> bytes:
    import json
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _single_request_via_gate(
    *, channel_id: str, grant, query: str, max_tokens: int, phase_id: Optional[str],
    query_index: Optional[int], config: Config, log, episode_id: str, attempt: int, attempt_label: str,
) -> "tuple":
    """
    The grant-derived gate path for ONE Perplexity dispatch -- mirrors
    hypogeum.openrouter._call_openrouter_via_gate()'s structure exactly
    (see that function's own docstring for the full rationale). Never
    reached unless a real, verified grant already resolves for
    (channel_id, episode_id). Called once per attempt by
    _query_perplexity_via_gate() below -- each attempt is its own
    envelope/authorization/gate submission with its own idempotency_key,
    per the plan's §5.5 "Retries and fallbacks" rule.

    Returns the exact same (PerplexityResult, failure_type, retry_after)
    tuple shape _single_request() returns, so _query_perplexity_via_gate()'s
    retry-variant logic (length doubling, content_filter suffix, etc.) is
    unaffected by which path actually ran. A genuine gate-level denial
    (grant/authorization/lease failure, or a truly unclassified
    unknown_acceptance) raises PerplexitySoftFailureExhausted directly --
    there is no lower-level "gate_denied" tuple shape to return, so this
    is surfaced the same way retry exhaustion already is.
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
    from hypogeum.providers.perplexity_adapter import (
        SCHEMA_VERSION as _RESEARCH_SCHEMA_VERSION, PerplexityAdapter, encode_research_payload,
    )
    from hypogeum.provider_receipt import WriteAheadReceiptStore

    at = datetime.now(timezone.utc).isoformat()

    package_identity = channel_config.compute_channel_package_identity(channel_id)
    if not package_identity.get("available"):
        raise PerplexitySoftFailureExhausted(f"channel_package_identity_unavailable for {attempt_label!r}")
    workflow_binding = workflow_executor.resolve_workflow_binding(
        channel_id, _HYPOGEUM_WORKFLOW_ID, _HYPOGEUM_WORKFLOW_VERSION,
    )

    plan_checksum = hashlib.sha256(_canonical_json_bytes({"phase_id": phase_id, "attempt_label": attempt_label})).hexdigest()
    structured_spec_checksum = hashlib.sha256(_canonical_json_bytes({"max_tokens": max_tokens, "query_index": query_index})).hexdigest()

    operation_payload_bytes = encode_research_payload(
        query=query, max_tokens=max_tokens, phase_id=phase_id, query_index=query_index, attempt=attempt,
    )
    operation_payload_content = operation_payload_bytes.decode("utf-8")
    operation_payload_sha256 = hashlib.sha256(operation_payload_bytes).hexdigest()

    expected_output_identity = f"episodes/{episode_id}/research/{attempt_label}"
    deterministic_output_path = f".hypogeum-assets/episodes/{episode_id}/research/{attempt_label}.json"
    max_cost = Decimal(str(round(max_tokens * _RESEARCH_CEILING_USD_PER_TOKEN, 6)))
    authorization_id = f"{episode_id}:research:{attempt_label}"

    try:
        envelope = GenerationExecutionEnvelope.create(
            envelope_schema_version=_RESEARCH_ENVELOPE_SCHEMA_VERSION, envelope_version=1, channel_id=channel_id,
            channel_package_version=package_identity["version"], channel_package_checksum=package_identity["checksum"],
            execution_id=episode_id, workflow_version=workflow_binding["workflow_version"],
            plan_version="live", plan_checksum=plan_checksum, planned_shot_id=attempt_label,
            structured_spec_checksum=structured_spec_checksum, compiler_identity=_RESEARCH_COMPILER_IDENTITY,
            compiler_version="1", operation_payload_content=operation_payload_content,
            operation_payload_sha256=operation_payload_sha256, operation_payload_schema_version=_RESEARCH_SCHEMA_VERSION,
            content_provenance={
                "compiler": _RESEARCH_COMPILER_IDENTITY, "compiler_version": "1",
                "source_work_item_id": attempt_label, "rendered_at": at,
            },
            provider=_RESEARCH_PROVIDER, model=config.PERPLEXITY_MODEL, provider_operation=_RESEARCH_PROVIDER_OPERATION,
            deterministic_output_path=deterministic_output_path, expected_output_identity=expected_output_identity,
            authorization_id=authorization_id, authorization_version=1, max_cost_per_call=max_cost,
            run_level_budget_ref=episode_id, episode_level_budget_ref=episode_id, currency="USD",
            retry_authorized=False, fallback_authorized=False, idempotency_key=authorization_id,
            created_at=at, expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
            dependency_checksums={
                "channel_package": package_identity["checksum"], "plan": plan_checksum,
                "structured_spec": structured_spec_checksum, "compiler": hashlib.sha256(_RESEARCH_COMPILER_IDENTITY.encode()).hexdigest(),
            },
        )
        authorization = authority.issue_child_authorization(envelope, grant.grant_id, grant.grant_version, at=at)
    except Exception as exc:  # noqa: BLE001 -- any compile/authorize failure here is a clean non-network denial
        raise PerplexitySoftFailureExhausted(f"Research gate authorization failed for {attempt_label!r}: {exc}") from exc

    conn = db.get_connection()
    _held_lease = None
    try:
        authorization_store.persist_execution_envelope(conn, envelope)

        adapter_registry.register_adapter(_RESEARCH_PROVIDER, config.PERPLEXITY_MODEL, _RESEARCH_PROVIDER_OPERATION, "1.0", PerplexityAdapter)
        lease_owner_id = f"perplexity_adapter:{attempt_label}"
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        lease = db.acquire_lease(conn, envelope.envelope_id, lease_owner_id, acquired_at=at, expires_at=lease_expires_at)
        if lease is None:
            raise PerplexitySoftFailureExhausted(f"Research gate lease unavailable for {attempt_label!r}")
        _held_lease = (envelope.envelope_id, lease_owner_id, lease["fencing_token"])

        receipt_store = WriteAheadReceiptStore(_ASSETS_ROOT / "episodes" / episode_id)
        live_sources = submission_gate.LiveSources(
            channel_package_checksum=package_identity["checksum"], plan_checksum=plan_checksum,
            structured_spec_checksum=structured_spec_checksum, compiler_version="1",
        )
        scopes = [
            bs.ScopeRequest(bs.BudgetScopeKind.CALL, f"{episode_id}:{attempt_label}", max_cost, "USD"),
            bs.ScopeRequest(bs.BudgetScopeKind.EPISODE, episode_id, grant.max_total_spend_all_bindings or max_cost, "USD"),
        ]
        result = submission_gate.submit(
            envelope, live_sources=live_sources, authorization=authorization, lease_conn=conn,
            lease_owner_id=lease_owner_id, lease_fencing_token=lease["fencing_token"], receipt_store=receipt_store,
            execution_id=episode_id, attempt_id=authorization_id, budget_scopes=scopes,
            required_provider=_RESEARCH_PROVIDER, required_model=config.PERPLEXITY_MODEL, at=at,
            expected_output_identity=expected_output_identity, expected_deterministic_output_path=deterministic_output_path,
            output_already_exists=False,
        )
    finally:
        # Release in finally (as muapi's gate paths do since 2026-09-03): an
        # unreleased lease makes every retry of the same call within 30 min
        # fail "lease unavailable" -- on 2026-10-03 one OpenRouter 402 turned
        # into three wasted runs this way and tripped the spend breaker.
        if _held_lease is not None:
            try:
                db.release_lease(
                    conn, _held_lease[0], _held_lease[1], _held_lease[2],
                    released_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception:  # noqa: BLE001
                pass
        conn.close()

    if not result.approved:
        evidence = result.adapter_result.evidence if result.adapter_result else {}
        exc_class = evidence.get("exception_class")
        message = evidence.get("message", result.reason)
        if exc_class == "PerplexityAuthFailure":
            raise PerplexityAuthFailure()
        if exc_class == "PerplexityHardFailure":
            raise PerplexityHardFailure(message, http_status=evidence.get("http_status"))
        if exc_class == "soft_failure":
            return None, evidence.get("failure_type", "gate_denied"), evidence.get("retry_after")
        raise PerplexitySoftFailureExhausted(f"Research gate denied for {attempt_label!r}: [{result.denied_at_check}] {result.reason}")

    evidence = result.adapter_result.evidence
    return PerplexityResult(
        content=evidence["content"], citations=evidence["citations"], usage=evidence["usage"],
        finish_reason=evidence["finish_reason"], phase_id=phase_id, query_index=query_index,
        response_time_ms=evidence["response_time_ms"], citations_absent=evidence["citations_absent"],
    ), None, None


def _query_perplexity_via_gate(
    *, channel_id: str, grant, query: str, config: Config, max_tokens: int,
    phase_id: Optional[str], query_index: Optional[int], log, episode_id: str,
) -> PerplexityResult:
    """
    The grant-derived gate path for query_perplexity() -- mirrors
    _execute_with_retry()'s own retry loop exactly (same RETRY_MAX
    budget, same retry-variant selection logic), except each individual
    Perplexity dispatch is its own gated attempt via
    _single_request_via_gate() instead of a direct _single_request()
    call. Never reached unless a real, verified grant already resolves
    for (channel_id, episode_id).
    """
    current_query = query
    current_max_tokens = max_tokens

    for attempt in range(RETRY_MAX + 1):
        result, failure_type, retry_after = _single_request_via_gate(
            channel_id=channel_id, grant=grant, query=current_query, max_tokens=current_max_tokens,
            phase_id=phase_id, query_index=query_index, config=config, log=log, episode_id=episode_id,
            attempt=attempt, attempt_label=f"{phase_id or 'research'}_{query_index if query_index is not None else 0}_{attempt}",
        )

        if result is not None:
            return result

        if attempt < RETRY_MAX:
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
                "perplexity_retry_triggered", phase_id=phase_id, query_index=query_index,
                trigger_reason=trigger, retry_delay_seconds=round(delay, 2), attempt=attempt + 1, via_gate=True,
            )
            time.sleep(delay)
        else:
            log.error(
                "perplexity_soft_failure_exhausted", phase_id=phase_id, query_index=query_index,
                failure_type=failure_type, via_gate=True,
            )
            raise PerplexitySoftFailureExhausted(
                f"Query failed via the gate path after {RETRY_MAX} retry (failure_type={failure_type!r}). "
                f"phase_id={phase_id!r}, query_index={query_index!r}",
                last_status=int(failure_type) if failure_type.isdigit() else None,
            )

    raise PerplexitySoftFailureExhausted("Gate-path retry loop exited unexpectedly.")


def query_perplexity(
    query: str,
    config: Config,
    *,
    phase_id: Optional[str] = None,
    query_index: Optional[int] = None,
    episode_id: Optional[str] = None,
    agent: Optional[str] = None,
    max_tokens_override: Optional[int] = None,
    channel_id: Optional[str] = None,
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
        channel_id:         Optional (added for the Authorization Authority gate
                            path, plan §5.5/§6 Commit I -- mirrors hypogeum.muapi.
                            submit_scene()'s own channel_id parameter exactly).
                            When supplied AND a real, verified RunAuthorizationGrant
                            already resolves for (channel_id, episode_id), the call
                            routes through hypogeum.authorization_authority +
                            hypogeum.submission_gate instead of the legacy path
                            below. Every existing caller omits channel_id and is
                            completely unaffected; even a caller that supplies it
                            falls straight through to the unchanged legacy path
                            whenever no real grant exists.

    Returns:
        PerplexityResult on success.

    Raises:
        PerplexityHardFailure         — HTTP 400/401/403/422. No retry.
        PerplexityAuthFailure         — HTTP 401 specifically.
        PerplexitySoftFailureExhausted — Soft failure persisted through all retries.
    """
    log = get_logger(episode_id=episode_id, agent=agent or "perplexity")
    max_tokens = max_tokens_override or config.PERPLEXITY_MAX_TOKENS or 2048

    # Pre-production (PRE_PLAN) reservation boundary (2026-08-28): research
    # runs before the first authoritative Production Plan. Establish/replay
    # the ONE pre-production CHANNEL_WEEKLY hold before this paid Perplexity
    # call reaches the network; no-op once a canonical plan exists; fail
    # closed on any weekly denial -> zero network.
    if channel_id and episode_id:
        from hypogeum.preplan_reservation import ensure_preproduction_reservation  # noqa: PLC0415
        try:
            ensure_preproduction_reservation(
                channel_id=channel_id, episode_id=episode_id, config=config, log=log,
            )
        except Exception as exc:  # noqa: BLE001 -- fail closed, no retry, no network
            raise PerplexityHardFailure(f"pre_plan_reservation_denied: {exc}", 0) from exc

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
        # ADR-016: an admitted autonomous execution has no paid legacy path.
        _admission = None
        if episode_id:
            try:
                from hypogeum.runtime_admission import get_admission_binding  # noqa: PLC0415
                _admission = get_admission_binding(channel_id, episode_id)
            except Exception:  # noqa: BLE001
                _admission = None
        if _admission is not None:
            grant = _authority.resolve_active_operating_grant(channel_id, at=_now_iso)
            if grant is None:
                raise PerplexityHardFailure(
                    "admitted_autonomous_execution_without_active_operating_grant -- "
                    "fail closed, no paid legacy path", 0,
                )
            return _query_perplexity_via_gate(
                channel_id=channel_id, grant=grant, query=query, config=config, max_tokens=max_tokens,
                phase_id=phase_id, query_index=query_index, log=log, episode_id=_eid,
            )
        grant = _authority.resolve_active_grant(channel_id, _eid, at=_now_iso)
        if grant is not None:
            return _query_perplexity_via_gate(
                channel_id=channel_id, grant=grant, query=query, config=config, max_tokens=max_tokens,
                phase_id=phase_id, query_index=query_index, log=log, episode_id=_eid,
            )

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

    total_tokens = usage.get("total_tokens", 0)
    log.info(
        "perplexity_response_received",
        phase_id=phase_id,
        query_index=query_index,
        total_tokens_used=total_tokens,
        citation_count=len(citations),
        finish_reason=finish_reason,
        response_time_ms=elapsed_ms,
    )
    _finance.record_cost(
        provider="perplexity",
        model=config.PERPLEXITY_MODEL,
        call_type="web_search",
        cost_usd=round(total_tokens * _PERPLEXITY_USD_PER_TOKEN, 6),
        tokens_or_credits=float(total_tokens),
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
