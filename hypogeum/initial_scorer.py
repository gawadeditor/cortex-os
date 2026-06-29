"""
hypogeum/initial_scorer.py — Initial scoring for Discovery Director v2

SCORING STATUS (2026-06-29): RUBRIC-BASED, NOT ANALYTICS-BACKED
  Scores currently derive from two sources:
    1. LLM-assigned score_breakdown (primary, when v2 prompt returns it):
       8 dimensions scored 1–10 by the Topic Ideator. Scores reflect the LLM's
       assessment of each candidate against a documented rubric. They produce
       genuine cross-candidate differentiation (typical spread: 0.45–0.85).
    2. Structural length signals (fallback): text character length of mystery_hook
       and differentiation_claim. Produces 0.70 for all well-formed LLM outputs
       (no differentiation) — this path fires only when score_breakdown is absent.

  WHAT THIS IS NOT: scores do not yet use real YouTube/TikTok/Instagram/Facebook
  channel performance data. The analytics_delta component (±0.20 cap) is present
  but returns 0.0 until the platform connectors complete and populate
  platform_analytics with source='api' rows.

TODO (backlog):
  Replace rubric-only discovery scoring with analytics-backed discovery scoring
  using actual YouTube/TikTok/Instagram/Facebook performance signals once
  connectors are complete. Specifically:
    - Use click-through rates of past episodes to calibrate click_potential weights
    - Use audience retention curves to calibrate retention_potential weights
    - Use watch time per topic cluster to calibrate curiosity_gap weights
    - Replace LLM self-assessment with delta from channel-specific KPI baseline
  This will make discovery scoring a genuine predictor of production ROI rather
  than a structural heuristic.

hypogeum/initial_scorer.py — Deterministic initial scoring for Discovery Director v2

Computes initial_composite for a candidate opportunity produced by the
Topic Ideator. Pure Python — no LLM calls, no DB reads, no network I/O.

This module is the sole source of initial_composite values. The Topic Ideator
is prohibited from returning any numeric field; all scores are assigned here
after parsing.

Design principle: structural signals only.
The scorer measures observable structure (field presence, text length) and
real analytics data (keyword overlap from signal_ctx). It does not encode
editorial vocabulary, curiosity-word lists, specificity markers, or
quality heuristics. Those judgments belong to the learning layer.

Public API:
    compute_score(candidate, signal_ctx=None, existing_topics=None) -> float

Internal helpers (also importable for direct unit testing):
    _score_mystery_hook(hook) -> float
    _score_differentiation(claim) -> float
    _analytics_delta(candidate, signal_ctx) -> float

Scoring formula:
    score = _BASE_SCORE (0.50)
          + mystery_hook length signal    [0.0 – 0.10]
          + differentiation length signal [0.0 – 0.10]
          – novelty penalty              [0.0 or 0.20]
          + analytics delta              [capped ± ANALYTICS_DELTA_CAP]

Output: clamped to [0.0, 1.0], rounded to 4 decimal places.
On any unexpected error, returns 0.0 without propagating the exception.

Signal context contract (same shape as _build_signal_context() in topic_discovery.py):
    {
      "has_real_analytics": bool,
      "top_performer_keywords": set[str],
      "high_retention_keywords": set[str],
      "penalty_keywords": set[str],
      ...
    }
"""

from __future__ import annotations

import os
import re
from typing import Optional


# ---------------------------------------------------------------------------
# Scoring constants — canonical defaults consumed by config.py
# ---------------------------------------------------------------------------
# Operators tune these via env vars (DISCOVERY_*) without touching source.
# config.py documents all 11 keys and their defaults.

_BASE_SCORE = 0.50

# mystery_hook structural length tiers (presence and elaborateness only —
# no vocabulary, no question-mark detection, no editorial judgment)
_HOOK_LONG    = 0.10   # ≥ 40 chars — developed ideation
_HOOK_SHORT   = 0.05   # ≥ 15 chars — present, limited development
_HOOK_PRESENT = 0.02   # non-empty, < 15 chars — stub

# Backward-compatible aliases for external importers and tests.
# The structural scorer collapsed EXCELLENT/STRONG into one LONG tier.
_HOOK_EXCELLENT = _HOOK_LONG
_HOOK_STRONG    = _HOOK_LONG
_HOOK_DECENT    = _HOOK_SHORT
_HOOK_WEAK      = _HOOK_PRESENT

# differentiation_claim structural length tiers
_DIFF_LONG  = 0.10   # ≥ 50 chars — elaborated claim
_DIFF_SHORT = 0.05   # non-empty, < 50 chars — minimal claim

# Backward-compatible aliases — specificity-marker tiers collapsed into length tiers.
_DIFF_STRONG  = _DIFF_LONG
_DIFF_DECENT  = _DIFF_SHORT
_DIFF_GENERIC = _DIFF_SHORT

# novelty penalty — applied when canonical_subject is already in existing_topics
_NOVELTY_PENALTY = 0.20

# analytics signal deltas (pre-cap; top + high_retention = 0.22 > cap so the
# positive cap is reachable in tests; penalty = 0.25 > cap for the negative cap)
_SIGNAL_BOOST_TOP_PERFORMER  = 0.12
_SIGNAL_BOOST_HIGH_RETENTION = 0.10
_SIGNAL_PENALTY_KEYWORD      = 0.25

# Maximum total analytics contribution in either direction
ANALYTICS_DELTA_CAP = 0.20


# ---------------------------------------------------------------------------
# Internal tokenisation helper
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "to", "and", "or", "but",
    "that", "this", "with", "for", "by", "from", "as", "is", "was", "are",
    "were", "be", "been", "being", "have", "has", "had", "do", "does",
    "did", "will", "would", "could", "should", "may", "might", "what",
    "how", "why", "when", "where", "which", "who", "we", "they", "their",
    "our", "its", "it", "he", "she", "you", "your", "my",
})

_WORD_RE = re.compile(r"[a-z']+")


def _tokenise(text: str) -> set[str]:
    """Lowercase word set excluding stopwords and tokens ≤ 2 chars.

    Matches the tokenization contract used by _build_signal_context() in
    topic_discovery.py so keyword overlap comparisons are consistent.
    """
    words = _WORD_RE.findall(text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


# ---------------------------------------------------------------------------
# Component scorers — structural signals only, no vocabulary judgment
# ---------------------------------------------------------------------------

def _score_mystery_hook(hook: str) -> float:
    """
    Score mystery_hook on character length as a proxy for ideation development.

    Longer hooks indicate the Topic Ideator elaborated the curiosity question;
    shorter hooks indicate minimal development. No word lists, no punctuation
    detection, no editorial quality judgment.

    Returns a value in [0.0, _HOOK_LONG] (env-var overridable).
    """
    if not hook or not hook.strip():
        return 0.0
    hook_long    = float(os.getenv("DISCOVERY_HOOK_LONG_WEIGHT",    str(_HOOK_LONG)))
    hook_short   = float(os.getenv("DISCOVERY_HOOK_SHORT_WEIGHT",   str(_HOOK_SHORT)))
    hook_present = float(os.getenv("DISCOVERY_HOOK_PRESENT_WEIGHT", str(_HOOK_PRESENT)))
    length = len(hook.strip())
    if length >= 40:
        return hook_long
    if length >= 15:
        return hook_short
    return hook_present


def _score_differentiation(claim: str) -> float:
    """
    Score differentiation_claim on character length as a proxy for claim elaboration.

    A longer claim indicates the Topic Ideator expanded on the differentiation;
    a short claim is minimal. No regex, no vocabulary, no specificity markers.

    Returns a value in [0.0, _DIFF_LONG] (env-var overridable).
    """
    if not claim or not claim.strip():
        return 0.0
    diff_long  = float(os.getenv("DISCOVERY_DIFF_LONG_WEIGHT",  str(_DIFF_LONG)))
    diff_short = float(os.getenv("DISCOVERY_DIFF_SHORT_WEIGHT", str(_DIFF_SHORT)))
    if len(claim.strip()) >= 50:
        return diff_long
    return diff_short


def _analytics_delta(candidate: dict, signal_ctx: dict) -> float:
    """
    Compute analytics signal delta for a candidate, capped at ±ANALYTICS_DELTA_CAP.

    Tokenizes topic_text + angle + mystery_hook and measures keyword set
    overlap against the signal context. Requires signal_ctx["has_real_analytics"]
    == True; returns 0.0 otherwise.

    Cap rationale: analytics signals should adjust ranking order, not dominate
    the score. A ±0.20 cap ensures structural signals remain a meaningful input.
    """
    if not signal_ctx or not signal_ctx.get("has_real_analytics"):
        return 0.0

    words = _tokenise(" ".join(filter(None, [
        str(candidate.get("topic_text") or ""),
        str(candidate.get("angle") or ""),
        str(candidate.get("mystery_hook") or ""),
    ])))

    boost_top  = float(os.getenv("DISCOVERY_SIGNAL_BOOST_TOP_PERFORMER",  str(_SIGNAL_BOOST_TOP_PERFORMER)))
    boost_high = float(os.getenv("DISCOVERY_SIGNAL_BOOST_HIGH_RETENTION", str(_SIGNAL_BOOST_HIGH_RETENTION)))
    penalty    = float(os.getenv("DISCOVERY_SIGNAL_PENALTY_KEYWORD",       str(_SIGNAL_PENALTY_KEYWORD)))
    cap        = float(os.getenv("DISCOVERY_ANALYTICS_DELTA_CAP",          str(ANALYTICS_DELTA_CAP)))

    delta = 0.0
    if words & signal_ctx.get("top_performer_keywords", set()):
        delta += boost_top
    if words & signal_ctx.get("high_retention_keywords", set()):
        delta += boost_high
    if words & signal_ctx.get("penalty_keywords", set()):
        delta -= penalty

    return max(-cap, min(cap, delta))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_SCORE_BREAKDOWN_WEIGHTS: dict[str, float] = {
    "click_potential":      0.20,
    "curiosity_gap":        0.20,
    "evidence_strength":    0.15,
    "visual_potential":     0.10,
    "retention_potential":  0.15,
    "production_feasibility": 0.10,
    "channel_fit":          0.05,
    "novelty":              0.05,
}


def _composite_from_breakdown(breakdown: dict) -> Optional[float]:
    """
    Compute a weighted composite in [0.0, 1.0] from an LLM-assigned score_breakdown.

    Each subscore is 1–10; the weight table sums to 1.0.
    Returns None if the breakdown dict is empty or malformed.
    """
    if not breakdown or not isinstance(breakdown, dict):
        return None
    total_weight = 0.0
    weighted_sum = 0.0
    for key, weight in _SCORE_BREAKDOWN_WEIGHTS.items():
        val = breakdown.get(key)
        if val is not None:
            weighted_sum += (float(val) / 10.0) * weight
            total_weight += weight
    if total_weight <= 0:
        return None
    # Normalize in case only a subset of keys is present
    return round(weighted_sum / total_weight, 4)


def compute_score(
    candidate: dict,
    signal_ctx: Optional[dict] = None,
    existing_topics: Optional[set] = None,
) -> float:
    """
    Compute the initial_composite score for a v2 candidate opportunity.

    Primary path (v2 prompt): when candidate contains a score_breakdown dict with
    8 LLM-assigned subscores (1–10), compute a weighted composite as the base score.
    This produces genuine cross-candidate differentiation since the LLM evaluates
    each dimension independently per topic.

    Fallback path: when score_breakdown is absent or malformed, fall back to
    structural length signals (mystery_hook + differentiation_claim character count).
    This path produces 0.70 for well-formed LLM output — a known limitation that
    motivates keeping the v2 prompt in sync.

    In both paths, novelty penalty and analytics delta are applied on top.

    Args:
        candidate:       Dict from parse_opportunity_batch() + _enrich_and_filter().
                         May include score_breakdown if the v2 prompt returned it.
        signal_ctx:      Output of _build_signal_context(). None or
                         has_real_analytics=False → analytics delta is 0.0.
        existing_topics: Set of canonical_subject strings already queued or consumed.
                         Presence triggers novelty penalty.

    Returns:
        float in [0.0, 1.0], rounded to 4 decimal places.
        Returns 0.0 (not raises) on any unexpected exception.
    """
    try:
        breakdown = candidate.get("score_breakdown")
        composite = _composite_from_breakdown(breakdown)

        if composite is not None:
            # Primary path: use LLM-assigned breakdown as base score
            score = composite
        else:
            # Fallback: structural length signals only
            score = float(os.getenv("DISCOVERY_BASE_SCORE", str(_BASE_SCORE)))
            score += _score_mystery_hook(str(candidate.get("mystery_hook") or ""))
            score += _score_differentiation(str(candidate.get("differentiation_claim") or ""))

        canonical = str(candidate.get("canonical_subject") or "")
        if existing_topics and canonical and canonical in existing_topics:
            score -= float(os.getenv("DISCOVERY_NOVELTY_PENALTY", str(_NOVELTY_PENALTY)))

        if signal_ctx:
            score += _analytics_delta(candidate, signal_ctx)

        return round(max(0.0, min(1.0, score)), 4)

    except Exception:
        return 0.0
