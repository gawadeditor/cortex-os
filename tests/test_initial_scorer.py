"""
tests/test_initial_scorer.py — Tests for hypogeum/initial_scorer.py

Required coverage from the implementation spec:
  1.  compute_score returns float in [0.0, 1.0]
  2.  deterministic repeated calls
  3.  higher mystery_hook quality increases score
  4.  stronger differentiation_claim increases score
  5.  existing canonical_subject applies novelty penalty
  6.  top_performer_keywords boost score
  7.  penalty_keywords reduce score
  8.  analytics delta capped at ±ANALYTICS_DELTA_CAP
  9.  missing candidate fields do not crash
  10. scorer makes no LLM/API calls

Additional coverage:
  - _score_mystery_hook tiers
  - _score_differentiation tiers
  - _analytics_delta positive cap
  - _analytics_delta negative cap
  - no_analytics context → no delta
  - malformed signal_ctx → no crash
  - clamp: score never exceeds 1.0 or falls below 0.0
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from hypogeum.initial_scorer import (
    compute_score,
    ANALYTICS_DELTA_CAP,
    _score_mystery_hook,
    _score_differentiation,
    _analytics_delta,
    _HOOK_EXCELLENT,
    _HOOK_STRONG,
    _HOOK_DECENT,
    _HOOK_WEAK,
    _DIFF_STRONG,
    _DIFF_DECENT,
    _DIFF_GENERIC,
    _NOVELTY_PENALTY,
    _BASE_SCORE,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _candidate(**overrides) -> dict:
    base = {
        "topic_text":             "Lost City of Helike",
        "canonical_subject":      "lost_city_of_helike",
        "angle":                  "The Poseidon legend connection",
        "mystery_hook":           "Why did an entire Greek city vanish beneath the waves in a single night?",
        "differentiation_claim":  "First video to use 2001 underwater sonar data from Katsonopoulou and Soter",
        "visual_signal":          "Underwater ruins at golden hour",
        "evidence_signal":        "2001 sonar survey peer-reviewed paper",
    }
    base.update(overrides)
    return base


def _signal_ctx(
    *,
    top_performer=None,
    high_retention=None,
    penalty=None,
) -> dict:
    return {
        "has_real_analytics":     True,
        "top_performer_keywords": set(top_performer or []),
        "high_retention_keywords": set(high_retention or []),
        "penalty_keywords":       set(penalty or []),
        "avg_ctr":                0.06,
        "avg_view_pct":           0.62,
        "avg_watch_time_s":       380.0,
    }


# ---------------------------------------------------------------------------
# 1. Return type and range
# ---------------------------------------------------------------------------

class TestReturnTypeAndRange:

    def test_returns_float(self):
        assert isinstance(compute_score(_candidate()), float)

    def test_output_ge_zero(self):
        assert compute_score(_candidate()) >= 0.0

    def test_output_le_one(self):
        assert compute_score(_candidate()) <= 1.0

    def test_output_in_range_no_signal(self):
        score = compute_score(_candidate())
        assert 0.0 <= score <= 1.0

    def test_output_in_range_with_signal(self):
        ctx = _signal_ctx(top_performer=["ancient", "lost"])
        score = compute_score(_candidate(), signal_ctx=ctx)
        assert 0.0 <= score <= 1.0

    def test_rounded_to_4_decimal_places(self):
        score = compute_score(_candidate())
        assert score == round(score, 4)


# ---------------------------------------------------------------------------
# 2. Determinism
# ---------------------------------------------------------------------------

class TestDeterminism:

    def test_same_inputs_same_output(self):
        c = _candidate()
        ctx = _signal_ctx(top_performer=["ancient"])
        existing = {"other_topic"}
        scores = [compute_score(c, ctx, existing) for _ in range(10)]
        assert len(set(scores)) == 1

    def test_no_signal_ctx_deterministic(self):
        c = _candidate()
        assert compute_score(c) == compute_score(c)

    def test_with_existing_topics_deterministic(self):
        c = _candidate()
        existing = {"lost_city_of_helike"}
        assert compute_score(c, existing_topics=existing) == \
               compute_score(c, existing_topics=existing)


# ---------------------------------------------------------------------------
# 3. mystery_hook quality increases score
# ---------------------------------------------------------------------------

class TestMysteryHookQuality:

    def test_no_hook_lower_than_excellent_hook(self):
        no_hook = compute_score(_candidate(mystery_hook=""))
        excellent = compute_score(_candidate(
            mystery_hook="Why did an entire Greek city vanish beneath the waves in a single night?"
        ))
        assert excellent > no_hook

    def test_excellent_hook_higher_than_decent_hook(self):
        excellent = compute_score(_candidate(
            mystery_hook="Why did an entire ancient city disappear without warning?"
        ))
        decent = compute_score(_candidate(mystery_hook="A strange disappearance"))
        assert excellent > decent

    def test_hook_tier_excellent(self):
        hook = "Why did an entire civilization vanish beneath the waves overnight?"
        assert _score_mystery_hook(hook) == _HOOK_EXCELLENT

    def test_hook_tier_strong_question_no_curiosity_word(self):
        # 40+ chars, ends with ?, but no curiosity word
        hook = "Could this be the greatest architectural achievement in the region?"
        assert _score_mystery_hook(hook) == _HOOK_STRONG

    def test_hook_tier_strong_curiosity_no_question(self):
        # 40+ chars, no ?, but contains curiosity word
        hook = "The secret behind the disappearance of an entire empire"
        assert _score_mystery_hook(hook) == _HOOK_STRONG

    def test_hook_tier_decent(self):
        hook = "A city beneath the sea"  # 22 chars, no ? or curiosity
        assert _score_mystery_hook(hook) == _HOOK_DECENT

    def test_hook_tier_weak(self):
        hook = "Short hook"  # 10 chars
        assert _score_mystery_hook(hook) == _HOOK_WEAK

    def test_empty_hook_zero(self):
        assert _score_mystery_hook("") == 0.0

    def test_whitespace_only_hook_zero(self):
        assert _score_mystery_hook("   ") == 0.0


# ---------------------------------------------------------------------------
# 4. differentiation_claim specificity increases score
# ---------------------------------------------------------------------------

class TestDifferentiationClaimSpecificity:

    def test_strong_claim_higher_than_no_claim(self):
        no_claim = compute_score(_candidate(differentiation_claim=""))
        strong = compute_score(_candidate(
            differentiation_claim="First documentary to use 2001 underwater sonar data from peer-reviewed research"
        ))
        assert strong > no_claim

    def test_specific_claim_higher_than_generic(self):
        generic = compute_score(_candidate(
            differentiation_claim="This topic has never been covered before"
        ))
        strong = compute_score(_candidate(
            differentiation_claim="First and only production using the 2001 sonar dataset from Katsonopoulou"
        ))
        assert strong > generic

    def test_diff_tier_strong(self):
        claim = "First documentary to use classified underwater sonar data from the 2001 expedition"
        assert _score_differentiation(claim) == _DIFF_STRONG

    def test_diff_tier_decent_short_with_marker(self):
        claim = "Only 3 sources exist on this topic"  # has digit + "Only"
        assert _score_differentiation(claim) == _DIFF_DECENT

    def test_diff_tier_generic_no_marker(self):
        claim = "This approach is different from existing videos"
        assert _score_differentiation(claim) == _DIFF_GENERIC

    def test_empty_claim_zero(self):
        assert _score_differentiation("") == 0.0

    def test_specificity_marker_number(self):
        claim = "Based on 47 primary sources excavated between 1988 and 2001"
        assert _score_differentiation(claim) == _DIFF_STRONG

    def test_specificity_marker_first(self):
        claim = "First production to combine acoustic mapping with archival diving logs"
        assert _score_differentiation(claim) == _DIFF_STRONG


# ---------------------------------------------------------------------------
# 5. Novelty penalty
# ---------------------------------------------------------------------------

class TestNoveltyPenalty:

    def test_existing_canonical_subject_reduces_score(self):
        canonical = "lost_city_of_helike"
        without_existing = compute_score(_candidate())
        with_existing = compute_score(
            _candidate(), existing_topics={canonical}
        )
        assert with_existing < without_existing

    def test_novelty_penalty_magnitude(self):
        c = _candidate()
        canonical = c["canonical_subject"]
        base = compute_score(c)
        penalized = compute_score(c, existing_topics={canonical})
        assert abs(base - penalized - _NOVELTY_PENALTY) < 0.001

    def test_different_canonical_no_penalty(self):
        c = _candidate()
        score_no_existing = compute_score(c)
        score_other_existing = compute_score(c, existing_topics={"some_other_topic"})
        assert score_no_existing == score_other_existing

    def test_empty_existing_topics_no_penalty(self):
        c = _candidate()
        assert compute_score(c, existing_topics=set()) == compute_score(c)

    def test_none_existing_topics_no_penalty(self):
        c = _candidate()
        assert compute_score(c, existing_topics=None) == compute_score(c)

    def test_penalized_score_still_in_range(self):
        c = _candidate(mystery_hook="Short")
        score = compute_score(c, existing_topics={c["canonical_subject"]})
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# 6. top_performer_keywords boost
# ---------------------------------------------------------------------------

class TestTopPerformerBoost:

    def test_matching_keyword_raises_score(self):
        ctx_no_match = _signal_ctx(top_performer=["unrelated"])
        ctx_match = _signal_ctx(top_performer=["helike"])  # in topic_text
        score_no = compute_score(_candidate(), signal_ctx=ctx_no_match)
        score_yes = compute_score(_candidate(), signal_ctx=ctx_match)
        assert score_yes > score_no

    def test_no_keyword_match_no_delta(self):
        base = compute_score(_candidate())
        ctx = _signal_ctx(top_performer=["zzznomatch"])
        with_ctx = compute_score(_candidate(), signal_ctx=ctx)
        assert with_ctx == base

    def test_has_real_analytics_false_no_boost(self):
        ctx = {
            "has_real_analytics": False,
            "top_performer_keywords": {"helike", "lost", "ancient"},
            "high_retention_keywords": set(),
            "penalty_keywords": set(),
        }
        base = compute_score(_candidate())
        with_ctx = compute_score(_candidate(), signal_ctx=ctx)
        assert with_ctx == base


# ---------------------------------------------------------------------------
# 7. penalty_keywords reduce score
# ---------------------------------------------------------------------------

class TestPenaltyKeywords:

    def test_penalty_keyword_match_reduces_score(self):
        ctx_no_penalty = _signal_ctx(penalty=["zzznomatch"])
        ctx_penalty = _signal_ctx(penalty=["helike"])  # in topic_text
        score_no = compute_score(_candidate(), signal_ctx=ctx_no_penalty)
        score_yes = compute_score(_candidate(), signal_ctx=ctx_penalty)
        assert score_yes < score_no

    def test_penalty_reduces_below_base(self):
        c = _candidate(
            mystery_hook="",
            differentiation_claim="",
        )
        ctx = _signal_ctx(penalty=["helike"])
        score = compute_score(c, signal_ctx=ctx)
        # base=0.50, no hook/diff bonus, penalty_delta capped at -0.20 → 0.30
        assert score < _BASE_SCORE

    def test_penalty_score_still_non_negative(self):
        c = _candidate(mystery_hook="", differentiation_claim="")
        ctx = _signal_ctx(penalty=["helike"])
        score = compute_score(c, signal_ctx=ctx, existing_topics={c["canonical_subject"]})
        assert score >= 0.0


# ---------------------------------------------------------------------------
# 8. Analytics delta capped at ±ANALYTICS_DELTA_CAP
# ---------------------------------------------------------------------------

class TestAnalyticsDeltaCap:

    def test_positive_cap_applied(self):
        """
        Both top_performer and high_retention overlap → uncapped = 0.22,
        capped to ANALYTICS_DELTA_CAP (0.20).
        """
        ctx = _signal_ctx(
            top_performer=["helike"],       # matches topic_text
            high_retention=["poseidon"],    # matches angle
        )
        delta = _analytics_delta(_candidate(), ctx)
        assert delta == ANALYTICS_DELTA_CAP
        assert delta == 0.20

    def test_negative_cap_applied(self):
        """
        Penalty keyword overlap → uncapped = -0.25, capped to -ANALYTICS_DELTA_CAP.
        """
        ctx = _signal_ctx(penalty=["helike"])
        delta = _analytics_delta(_candidate(), ctx)
        assert delta == -ANALYTICS_DELTA_CAP
        assert delta == -0.20

    def test_single_boost_no_cap(self):
        ctx = _signal_ctx(top_performer=["helike"])
        delta = _analytics_delta(_candidate(), ctx)
        assert 0.0 < delta < ANALYTICS_DELTA_CAP

    def test_no_analytics_returns_zero_delta(self):
        ctx = {"has_real_analytics": False, "top_performer_keywords": {"helike"}}
        assert _analytics_delta(_candidate(), ctx) == 0.0

    def test_none_signal_ctx_returns_zero_delta(self):
        assert _analytics_delta(_candidate(), {}) == 0.0

    def test_compute_score_capped_above_one(self):
        """Even with all boosts, score is ≤ 1.0."""
        ctx = _signal_ctx(top_performer=["helike"], high_retention=["poseidon"])
        score = compute_score(_candidate(), signal_ctx=ctx)
        assert score <= 1.0

    def test_compute_score_capped_below_zero(self):
        """Even with all penalties + novelty, score is ≥ 0.0."""
        c = _candidate(mystery_hook="", differentiation_claim="")
        ctx = _signal_ctx(penalty=["helike"])
        score = compute_score(c, signal_ctx=ctx, existing_topics={c["canonical_subject"]})
        assert score >= 0.0


# ---------------------------------------------------------------------------
# 9. Missing candidate fields do not crash
# ---------------------------------------------------------------------------

class TestMissingFields:

    def test_empty_candidate(self):
        score = compute_score({})
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    def test_none_values_in_candidate(self):
        c = {
            "topic_text": None,
            "canonical_subject": None,
            "mystery_hook": None,
            "differentiation_claim": None,
        }
        score = compute_score(c)
        assert isinstance(score, float)

    def test_missing_mystery_hook(self):
        c = _candidate()
        del c["mystery_hook"]
        score = compute_score(c)
        assert isinstance(score, float)

    def test_missing_differentiation_claim(self):
        c = _candidate()
        del c["differentiation_claim"]
        score = compute_score(c)
        assert isinstance(score, float)

    def test_missing_canonical_subject(self):
        c = _candidate()
        del c["canonical_subject"]
        score = compute_score(c, existing_topics={"lost_city_of_helike"})
        assert isinstance(score, float)

    def test_malformed_signal_ctx_no_crash(self):
        score = compute_score(_candidate(), signal_ctx={"broken": True})
        assert isinstance(score, float)

    def test_non_dict_candidate_returns_zero(self):
        # Exception in compute_score → returns 0.0
        score = compute_score("not a dict")  # type: ignore[arg-type]
        assert score == 0.0

    def test_integer_field_values_no_crash(self):
        c = _candidate(mystery_hook=42, differentiation_claim=99)  # type: ignore[arg-type]
        score = compute_score(c)
        assert isinstance(score, float)


# ---------------------------------------------------------------------------
# 10. Scorer makes no LLM/API calls
# ---------------------------------------------------------------------------

class TestNoNetworkCalls:

    def test_compute_score_requires_no_network(self, monkeypatch):
        """Block all socket connections; compute_score must still succeed."""
        def _no_network(*args, **kwargs):
            raise AssertionError("initial_scorer must not make network calls")
        monkeypatch.setattr(socket, "socket", _no_network)
        score = compute_score(_candidate())
        assert isinstance(score, float)

    def test_no_forbidden_imports_in_source(self):
        import inspect
        import hypogeum.initial_scorer as mod
        source = inspect.getsource(mod)
        forbidden = ("requests", "httpx", "aiohttp", "openai", "anthropic",
                     "urllib.request", "http.client")
        for lib in forbidden:
            assert lib not in source, \
                f"initial_scorer must not import network library '{lib}'"

    def test_compute_score_with_no_credentials(self):
        """No env vars, no credentials — must still return a valid score."""
        import os
        env_backup = {k: v for k, v in os.environ.items()
                      if any(k.startswith(p) for p in
                             ("OPENROUTER", "ANTHROPIC", "PERPLEXITY", "ELEVENLABS"))}
        try:
            for k in env_backup:
                os.environ.pop(k, None)
            score = compute_score(_candidate())
            assert isinstance(score, float)
        finally:
            os.environ.update(env_backup)
