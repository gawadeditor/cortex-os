"""
hypogeum/agents/research_director.py — Research Director agent (M1.3)

The Research Director is the second real agent in the pipeline. It uses a
split-provider architecture: Perplexity for web discovery, Claude Opus for synthesis.

Execution sequence:
  1. Health-check Perplexity — if unavailable, issue BLOCKING PDR immediately
  2. Run four sequential research queries via query_perplexity():
       Phase core_facts         — timeline, verified events
       Phase mystery_depth      — unknowns, disputed evidence
       Phase academic_context   — expert sources, peer-reviewed research
       Phase competitive_landscape — existing YouTube/documentary coverage
  3. If PerplexityHardFailure on any query → issue BLOCKING PDR immediately
  4. If ≥ _PDR_SOFT_FAILURE_THRESHOLD soft failures → issue MAJOR PDR
  5. Build source_registry from deduplicated citation URLs
  6. Call run_agent() with Claude Opus — synthesise all findings → research_package

Model: Claude Opus 4.6 (frozen — T0.1)
Max tokens: 8,192 (synthesis call)
Perplexity: 2,048 tokens per query

Integration contracts (must not be violated):
  - All Perplexity calls go through hypogeum.perplexity — never requests directly
  - All Claude calls go through run_agent() — never call_claude() directly
  - All PDRs validated against package_deficiency_report schema before return
  - All environment access via Config — never os.getenv()
  - All logging via get_logger() — never print() or stdlib logging

Logging events emitted:
  rd_health_check_started      — info  — before Perplexity health check
  rd_health_check_completed    — info  — health check result
  rd_perplexity_unavailable    — error — health check returned "unavailable"
  rd_perplexity_degraded       — warn  — health check returned "degraded"
  rd_query_started             — info  — before each Perplexity query
  rd_query_completed           — info  — after each successful Perplexity query
  rd_query_hard_failure        — error — PerplexityHardFailure during query
  rd_query_soft_failure        — warn  — PerplexitySoftFailureExhausted during query
  rd_partial_query_failures    — warn  — some queries failed but threshold not reached
  rd_pdr_issued                — error — PDR built and returned
  rd_synthesis_started         — info  — before Claude run_agent() call
  rd_synthesis_completed       — info  — after research_package returned
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.agent import run_agent
from hypogeum.config import Config
from hypogeum.logger import get_logger
from hypogeum.prompt_loader import load_prompt
from hypogeum.perplexity import (
    PerplexityHardFailure,
    PerplexitySoftFailureExhausted,
    check_perplexity_health,
    query_perplexity,
)
from hypogeum.schemas import validate


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CLAUDE_OPUS   = "anthropic/claude-opus-4.6"
_RD_MAX_TOKENS = 16_384
_AGENT_NAME    = "research_director"

# Perplexity tokens per discovery query
_PERPLEXITY_MAX_TOKENS = 2_048

# Minimum soft-failure count that triggers a PDR
# 0-2 failures → warn and continue; 3-4 failures → PDR MAJOR
_PDR_SOFT_FAILURE_THRESHOLD = 3

_PROMPTS_DIR  = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILE  = _PROMPTS_DIR / "research_director.txt"


# ---------------------------------------------------------------------------
# Research phases (frozen — all four run sequentially)
# ---------------------------------------------------------------------------

_RESEARCH_PHASES = [
    {
        "phase_id": "core_facts",
        "query_template": "{topic} history timeline key facts primary sources verified documented",
        "description": "Core historical facts and timeline",
    },
    {
        "phase_id": "mystery_depth",
        "query_template": "{topic} mysteries unsolved questions disputed evidence scholarly debate open questions",
        "description": "Mysteries, unknowns, and disputed claims",
    },
    {
        "phase_id": "academic_context",
        "query_template": "{topic} academic research archaeological study expert analysis peer reviewed publications",
        "description": "Academic and expert sources",
    },
    {
        "phase_id": "competitive_landscape",
        "query_template": "{topic} YouTube documentary video popular science coverage existing content",
        "description": "Competitive content landscape",
    },
]


# ---------------------------------------------------------------------------
# System prompt — loaded once at import time
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    return load_prompt(_PROMPT_FILE)


SYSTEM_PROMPT: str = _load_system_prompt()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_research_director(
    topic_brief: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path] = None,
    channel_id: Optional[str] = None,
) -> tuple[dict, Optional[dict]]:
    """
    Execute the Research Director agent end-to-end.

    Args:
        topic_brief:  Validated topic_brief dict from the Executive Producer.
        config:       Loaded Config instance. Passed through to all sub-calls.
        episode_id:   Episode identifier for log correlation and file naming.
        output_dir:   Optional output directory override (passed to run_agent()).
        channel_id:   Optional -- forwarded unchanged to query_perplexity()'s own
                      channel_id parameter (plan §5.5/§6 Commit I). Every existing
                      caller omits it and is completely unaffected.

    Returns:
        (research_package, None)   — successful synthesis; research_package is
                                     schema-validated by run_agent().
        ({}, pdr)                  — Perplexity unavailable, hard failure, or
                                     too many soft failures; pdr is a validated
                                     package_deficiency_report dict.

    Raises:
        ResponseParseError — Claude returned non-dict JSON.
        SchemaError        — Claude output failed research_package schema.
        Any exception from run_agent() / openrouter that is not caught here.
    """
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    topic = (
        topic_brief.get("episode_title")
        or topic_brief.get("topic_id")
        or "unknown_topic"
    )

    # -----------------------------------------------------------------------
    # Phase 1: Perplexity health check
    # -----------------------------------------------------------------------
    log.info("rd_health_check_started", topic=topic)
    health = check_perplexity_health(
        config, episode_id=episode_id, agent=_AGENT_NAME, channel_id=channel_id
    )
    log.info("rd_health_check_completed", status=health)

    if health == "unavailable":
        log.error("rd_perplexity_unavailable", health=health)
        pdr = _build_pdr(
            episode_id=episode_id,
            severity="BLOCKING",
            items=[
                {
                    "item_id": "pdr_item_001",
                    "description": (
                        "Perplexity is unavailable. The research run cannot proceed. "
                        "Verify PERPLEXITY_API_KEY and service availability, then re-run."
                    ),
                }
            ],
            reason="provider_unavailable",
        )
        log.error("rd_pdr_issued", severity="BLOCKING", reason="provider_unavailable")
        return {}, pdr

    if health == "degraded":
        log.warn("rd_perplexity_degraded", health=health)

    # -----------------------------------------------------------------------
    # Phase 2: Sequential research queries
    # -----------------------------------------------------------------------
    raw_results: list[dict] = []
    query_failures: list[dict] = []

    for i, phase in enumerate(_RESEARCH_PHASES):
        query = phase["query_template"].format(topic=topic)
        log.info("rd_query_started", phase_id=phase["phase_id"], query_index=i)

        try:
            result = query_perplexity(
                query,
                config,
                phase_id=phase["phase_id"],
                query_index=i,
                episode_id=episode_id,
                agent=_AGENT_NAME,
                max_tokens_override=_PERPLEXITY_MAX_TOKENS,
                channel_id=channel_id,
            )
            raw_results.append(
                {
                    "phase_id": phase["phase_id"],
                    "description": phase["description"],
                    "query": query,
                    "content": result.content,
                    "citations": result.citations,
                    "response_time_ms": result.response_time_ms,
                    "finish_reason": result.finish_reason,
                    "usage": result.usage,
                    "citations_absent": result.citations_absent,
                }
            )
            log.info(
                "rd_query_completed",
                phase_id=phase["phase_id"],
                query_index=i,
                citations_count=len(result.citations),
                response_time_ms=result.response_time_ms,
            )

        except PerplexityHardFailure as exc:
            log.error(
                "rd_query_hard_failure",
                phase_id=phase["phase_id"],
                query_index=i,
                http_status=exc.http_status,
                error=str(exc),
            )
            pdr = _build_pdr(
                episode_id=episode_id,
                severity="BLOCKING",
                items=[
                    {
                        "item_id": "pdr_item_001",
                        "description": (
                            f"Perplexity hard failure during phase "
                            f"'{phase['phase_id']}' (HTTP {exc.http_status}): {exc}. "
                            f"Verify API key and request configuration."
                        ),
                    }
                ],
                reason=f"hard_failure_{phase['phase_id']}",
            )
            log.error(
                "rd_pdr_issued",
                severity="BLOCKING",
                reason=f"hard_failure_{phase['phase_id']}",
            )
            return {}, pdr

        except PerplexitySoftFailureExhausted as exc:
            log.warn(
                "rd_query_soft_failure",
                phase_id=phase["phase_id"],
                query_index=i,
                last_status=exc.last_status,
                error=str(exc),
            )
            query_failures.append(
                {
                    "phase_id": phase["phase_id"],
                    "query_index": i,
                    "reason": str(exc),
                    "last_status": exc.last_status,
                }
            )

    # -----------------------------------------------------------------------
    # Phase 3: Check soft-failure threshold
    # -----------------------------------------------------------------------
    n_failed = len(query_failures)
    n_total  = len(_RESEARCH_PHASES)

    if n_failed >= _PDR_SOFT_FAILURE_THRESHOLD:
        severity = "BLOCKING" if n_failed == n_total else "MAJOR"
        items = [
            {
                "item_id": f"pdr_item_{idx + 1:03d}",
                "description": (
                    f"Query failure in phase '{f['phase_id']}': {f['reason']}"
                ),
            }
            for idx, f in enumerate(query_failures)
        ]
        pdr = _build_pdr(
            episode_id=episode_id,
            severity=severity,
            items=items,
            reason="insufficient_sources",
        )
        log.error(
            "rd_pdr_issued",
            severity=severity,
            reason="insufficient_sources",
            failed_queries=n_failed,
            total_queries=n_total,
        )
        return {}, pdr

    if n_failed > 0:
        log.warn(
            "rd_partial_query_failures",
            failed=n_failed,
            total=n_total,
        )

    # -----------------------------------------------------------------------
    # Phase 4: Build source registry from deduplicated citation URLs
    # -----------------------------------------------------------------------
    source_registry = _build_source_registry(raw_results)

    # -----------------------------------------------------------------------
    # Phase 5: Claude Opus synthesis
    # -----------------------------------------------------------------------
    log.info(
        "rd_synthesis_started",
        results_count=len(raw_results),
        source_count=len(source_registry),
    )

    research_package = run_agent(
        agent_name=_AGENT_NAME,
        model_slug=getattr(config, "RD_MODEL", _CLAUDE_OPUS),
        system_prompt=SYSTEM_PROMPT,
        input_package={
            "task": "synthesize_research",
            "topic_brief": topic_brief,
            "perplexity_results": raw_results,
            "query_failures": query_failures,
            "source_registry": source_registry,
        },
        output_schema="research_package",
        episode_id=episode_id,
        max_tokens=_RD_MAX_TOKENS,
        config=config,
        output_dir=output_dir,
        channel_id=channel_id,
    )

    log.info(
        "rd_synthesis_completed",
        claims_count=len(research_package.get("claims", [])),
        sources_count=len(research_package.get("source_registry", [])),
    )

    return research_package, None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_source_registry(raw_results: list[dict]) -> list[dict]:
    """
    Build a deduplicated source registry from Perplexity citation URLs.

    Each unique URL becomes one entry. Order is determined by first appearance
    (phase order, then citation order within the phase). Duplicate URLs are
    silently dropped — only the first occurrence is kept.
    """
    seen: set[str] = set()
    registry: list[dict] = []
    idx = 1
    for result in raw_results:
        for url in result.get("citations", []):
            if url and url not in seen:
                seen.add(url)
                registry.append(
                    {
                        "source_id": f"src_{idx:03d}",
                        "url": url,
                        "phase_id": result["phase_id"],
                    }
                )
                idx += 1
    return registry


def _build_pdr(
    episode_id: str,
    severity: str,
    items: list[dict],
    reason: str = "",
) -> dict:
    """
    Construct and schema-validate a Package Deficiency Report.

    The PDR is built programmatically (not via Claude) because it represents
    a structured error condition detected before or during research.

    Raises SchemaError if the constructed PDR fails schema validation —
    which indicates a bug in the caller, not a runtime failure.
    """
    pdr = {
        "report_id": f"pdr_{episode_id}_{reason}" if reason else f"pdr_{episode_id}",
        "issuing_agent": "research_director",
        "target_package": "research_package",
        "issued_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "severity": severity,
        "deficiency_items": items,
        "routing_target": "executive_producer",
    }
    validate("package_deficiency_report", pdr)
    return pdr
