"""
hypogeum/discovery/topic_ideator.py — Topic Ideator v2 (Discovery Director)

Generates scored topic opportunity candidates for a channel using an LLM.
Called by the Discovery Director when DISCOVERY_ENGINE=v2.

Pipeline (per call):
  1. Build signal context from DB (real analytics when available).
  2. Call run_agent() with schema "opportunity_batch" — LLM returns six
     pure-string fields per opportunity; no numeric fields.
  3. parse_opportunity_batch() strips any numeric drift from LLM output.
  4. For each opportunity: inject canonical_subject in Python from topic_text.
  5. For each opportunity: call initial_scorer.compute_score() for initial_composite.
  6. Filter out opportunities whose canonical_subject is already covered.
  7. On any LLM failure: fall back to converting _FALLBACK_SEEDS into the
     same six-field shape so the caller always gets usable candidates.

Public API:
    run_topic_ideator(channel_id, config, db_path=None, output_dir=None,
                      existing_topics=None) -> dict

    Return shape:
        {
          "ok": bool,
          "source": "llm" | "fallback",
          "batch_id": str,
          "channel_id": str,
          "opportunities": [
              {
                "topic_text": str,
                "angle": str,
                "mystery_hook": str,
                "differentiation_claim": str,
                "visual_signal": str,
                "evidence_signal": str,
                "canonical_subject": str,    # Python-injected
                "initial_composite": float,  # scorer-assigned
              },
              ...
          ],
        }
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from hypogeum.agent import run_agent
from hypogeum.config import Config
from hypogeum.db import get_connection
from hypogeum.prompt_loader import load_prompt
from hypogeum.initial_scorer import compute_score
from hypogeum.schemas import parse_opportunity_batch
from hypogeum.topic_discovery import (
    _FALLBACK_SEEDS,
    _build_signal_context,
    _make_canonical_subject,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "topic_ideator"
_MAX_TOKENS = 4_096
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILE = _PROMPTS_DIR / "topic_ideator.txt"

_STRING_FIELDS = (
    "topic_text", "angle", "mystery_hook",
    "differentiation_claim", "visual_signal", "evidence_signal",
)


# ---------------------------------------------------------------------------
# System prompt — loaded once at import time
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    return load_prompt(_PROMPT_FILE)


SYSTEM_PROMPT: str = _load_system_prompt()


# ---------------------------------------------------------------------------
# Fallback: convert v1 seeds → six-field opportunity shape
# ---------------------------------------------------------------------------

def _seeds_to_opportunities(channel_id: str) -> list[dict]:
    """Convert _FALLBACK_SEEDS into the six-field opportunity shape."""
    opps = []
    for seed in _FALLBACK_SEEDS:
        opps.append({
            "topic_text":            seed["topic"],
            "angle":                 "Historical documentary perspective",
            "mystery_hook":          seed["ctr_rationale"],
            "differentiation_claim": seed["retention_rationale"],
            "visual_signal":         "Archival and cinematic reconstructions",
            "evidence_signal":       seed["risk"],
        })
    return opps


# ---------------------------------------------------------------------------
# Post-processing: inject canonical_subject + score, filter covered
# ---------------------------------------------------------------------------

def _enrich_and_filter(
    opportunities: list[dict],
    signal_ctx: dict,
    existing_topics: Optional[set[str]],
    generation_source: str = "llm_ideation",
) -> list[dict]:
    """
    For each opportunity:
      - inject canonical_subject from topic_text (Python-owned, not LLM)
      - inject generation_source so callers can distinguish real LLM vs fallback
      - call compute_score() to attach initial_composite
      - drop opportunities whose canonical_subject is in existing_topics
    """
    covered = existing_topics or set()
    result = []
    for opp in opportunities:
        topic_text = opp.get("topic_text") or ""
        canonical = _make_canonical_subject(topic_text)
        if canonical and canonical in covered:
            continue
        score = compute_score(opp, signal_ctx=signal_ctx, existing_topics=covered)
        result.append({
            **opp,
            "canonical_subject":  canonical,
            "initial_composite":  score,
            "generation_source":  generation_source,
        })
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_topic_ideator(
    channel_id: str,
    config: Config,
    db_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    existing_topics: Optional[set[str]] = None,
) -> dict:
    """
    Run the Topic Ideator for a channel and return scored opportunities.

    Args:
        channel_id:      Target channel identifier.
        config:          Loaded Config instance (provides API keys, model slugs).
        db_path:         Override DB path (for tests). None = default.
        output_dir:      Override output directory for run_agent() artifacts.
        existing_topics: Set of canonical_subject strings already queued or
                         recently consumed. Used for novelty filtering.

    Returns:
        {ok, source, batch_id, channel_id, opportunities}
        Each opportunity has the six LLM string fields plus canonical_subject
        and initial_composite.
    """
    batch_id = str(uuid.uuid4())

    # ── 1. Signal context from DB ──────────────────────────────────────────
    conn = get_connection(db_path)
    try:
        signal_ctx = _build_signal_context(channel_id, conn)
    finally:
        conn.close()

    # ── 2. Build LLM input ─────────────────────────────────────────────────
    input_package = {
        "channel_id": channel_id,
        "batch_id":   batch_id,
        "signal_context": {
            "has_real_analytics":     signal_ctx.get("has_real_analytics", False),
            "avg_ctr":                signal_ctx.get("avg_ctr"),
            "avg_view_pct":           signal_ctx.get("avg_view_pct"),
            "top_performer_keywords": sorted(signal_ctx.get("top_performer_keywords", set())),
            "high_retention_keywords": sorted(signal_ctx.get("high_retention_keywords", set())),
            "penalty_keywords":       sorted(signal_ctx.get("penalty_keywords", set())),
        },
    }

    # ── 3. LLM call ────────────────────────────────────────────────────────
    try:
        raw_batch = run_agent(
            agent_name=_AGENT_NAME,
            model_slug=getattr(config, "OPENROUTER_SONNET_MODEL_ID",
                               "anthropic/claude-sonnet-4.6"),
            system_prompt=SYSTEM_PROMPT,
            input_package=input_package,
            output_schema="opportunity_batch",
            episode_id=batch_id,
            max_tokens=_MAX_TOKENS,
            config=config,
            output_dir=output_dir,
        )
        source = "llm"

        # ── 4. Strip numeric drift, validate ──────────────────────────────
        parsed = parse_opportunity_batch(raw_batch)
        raw_opps = parsed.get("opportunities", [])

    except Exception:
        # Any LLM or parse failure → fall back to heuristic seeds
        source = "fallback"
        raw_opps = _seeds_to_opportunities(channel_id)

    # ── 5. Enrich: inject canonical_subject + initial_composite, filter ────
    gen_source = "llm_ideation" if source == "llm" else "fallback_seed"
    opportunities = _enrich_and_filter(raw_opps, signal_ctx, existing_topics,
                                       generation_source=gen_source)

    return {
        "ok":            True,
        "source":        source,
        "batch_id":      batch_id,
        "channel_id":    channel_id,
        "opportunities": opportunities,
    }
