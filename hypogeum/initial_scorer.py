"""
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

def compute_score(
    candidate: dict,
    signal_ctx: Optional[dict] = None,
    existing_topics: Optional[set] = None,
) -> float:
    """
    Compute the initial_composite score for a v2 candidate opportunity.

    This is the sole source of initial_composite. Call this after
    parse_opportunity_batch() has stripped unknown fields and injected
    canonical_subject.

    Args:
        candidate:       Dict produced by Topic Ideator + canonical_subject
                         injection. Expected keys: topic_text, canonical_subject,
                         angle, mystery_hook, differentiation_claim,
                         visual_signal, evidence_signal.
                         Missing keys degrade gracefully (return partial score).
        signal_ctx:      Output of _build_signal_context(). None or
                         has_real_analytics=False → analytics delta is 0.0.
        existing_topics: Set of canonical_subject strings already enqueued or
                         recently consumed. Presence → novelty penalty applied.

    Returns:
        float in [0.0, 1.0], rounded to 4 decimal places.
        Returns 0.0 (not raises) on any unexpected exception.
    """
    try:
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
