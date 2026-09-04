"""
tests/test_topic_ideator.py — Unit tests for hypogeum/discovery/topic_ideator.py

Tests:
  LLMSuccess          — run_topic_ideator returns LLM candidates when run_agent succeeds
  NumericStripping     — numeric fields in mock LLM output are stripped before validation
  CanonicalSubject     — canonical_subject is Python-generated from topic_text only
  InitialComposite     — initial_composite is assigned by initial_scorer.compute_score
  CoveredFiltering     — opportunities whose canonical_subject is in existing_topics are dropped
  FallbackOnFailure    — LLM failure produces fallback seeds in six-field shape
  FallbackShape        — fallback opportunities have all six required string fields
  V1Regression         — v1 _score_seed and run_discovery_cycle are unaffected

All tests mock run_agent and DB; no network calls, no file I/O beyond tmp_path.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.discovery.topic_ideator import (
    run_topic_ideator,
    _seeds_to_opportunities,
    _enrich_and_filter,
    _STRING_FIELDS,
)
from hypogeum.topic_discovery import _make_canonical_subject


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SIX_FIELDS = (
    "topic_text", "angle", "mystery_hook",
    "differentiation_claim", "visual_signal", "evidence_signal",
)


def _make_config():
    cfg = SimpleNamespace()
    cfg.OPENROUTER_API_KEY = "test-key"
    cfg.OPENROUTER_SONNET_MODEL_ID = "anthropic/claude-sonnet-4.6"
    return cfg


def _make_opp(**overrides) -> dict:
    base = {
        "topic_text":            "Lost City of Helike",
        "angle":                 "The Poseidon legend connection",
        "mystery_hook":          "Why did an entire Greek city vanish beneath the waves in a single night?",
        "differentiation_claim": "First video to use 2001 underwater sonar data from Katsonopoulou and Soter",
        "visual_signal":         "Underwater ruins at golden hour",
        "evidence_signal":       "2001 sonar survey peer-reviewed paper",
    }
    base.update(overrides)
    return base


def _make_batch(channel_id: str = "ch_test", opportunities=None) -> dict:
    """Minimal valid opportunity_batch from a mocked LLM response."""
    if opportunities is None:
        opportunities = [_make_opp()]
    return {
        "batch_id":      str(uuid.uuid4()),
        "channel_id":    channel_id,
        "engine":        "v2",
        "opportunities": opportunities,
    }


def _empty_signal_ctx() -> dict:
    return {
        "has_real_analytics":     False,
        "top_performer_keywords": set(),
        "high_retention_keywords": set(),
        "penalty_keywords":       set(),
        "avg_ctr":                None,
        "avg_view_pct":           None,
        "avg_watch_time_s":       None,
    }


# ---------------------------------------------------------------------------
# 1. LLM success path
# ---------------------------------------------------------------------------

class TestLLMSuccess:

    def test_returns_ok_true_on_success(self, tmp_path):
        batch = _make_batch()
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection") as mock_conn:
            mock_conn.return_value.__enter__ = lambda s: s
            mock_conn.return_value.close = MagicMock()
            result = run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)
        assert result["ok"] is True

    def test_source_is_llm_on_success(self, tmp_path):
        batch = _make_batch()
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)
        assert result["source"] == "llm"

    def test_returns_channel_id(self, tmp_path):
        batch = _make_batch(channel_id="ch_xyz")
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator("ch_xyz", _make_config(), output_dir=tmp_path)
        assert result["channel_id"] == "ch_xyz"

    def test_opportunities_list_non_empty(self, tmp_path):
        batch = _make_batch()
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)
        assert len(result["opportunities"]) >= 1

    def test_multiple_opportunities_returned(self, tmp_path):
        batch = _make_batch(opportunities=[
            _make_opp(topic_text="Topic One", mystery_hook="Why did Topic One vanish from history?"),
            _make_opp(topic_text="Topic Two", mystery_hook="How did Topic Two shape the ancient world?"),
            _make_opp(topic_text="Topic Three", mystery_hook="What secret did Topic Three hide?"),
        ])
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)
        assert len(result["opportunities"]) == 3

    def test_batch_id_present(self, tmp_path):
        batch = _make_batch()
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)
        assert "batch_id" in result
        assert result["batch_id"]

    def test_channel_topic_learning_injected_into_llm_input(self, tmp_path):
        batch = _make_batch()
        seen = {}

        def _capture(**kw):
            seen["pkg"] = kw["input_package"]
            return batch

        with patch("hypogeum.discovery.topic_ideator.run_agent", side_effect=_capture), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"), \
             patch("hypogeum.channel_learning.directive_texts",
                   return_value=["Lean away from cryptid angles — weak engagement"]):
            run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)
        assert seen["pkg"]["channel_topic_learning"] == [
            "Lean away from cryptid angles — weak engagement"]


# ---------------------------------------------------------------------------
# 2. Numeric field stripping
# ---------------------------------------------------------------------------

class TestNumericStripping:

    def test_initial_composite_from_llm_is_stripped(self, tmp_path):
        """LLM-returned initial_composite must be stripped; scorer assigns the real one."""
        opp_with_score = {**_make_opp(), "initial_composite": 0.99, "score": 100}
        batch = _make_batch(opportunities=[opp_with_score])
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)
        opp = result["opportunities"][0]
        # The LLM-injected value 0.99 must never survive to the output
        assert opp["initial_composite"] != 0.99

    def test_rank_from_llm_is_stripped(self, tmp_path):
        """rank field (numeric scoring) must not appear in enriched opportunity."""
        opp_with_rank = {**_make_opp(), "rank": 1}
        batch = _make_batch(opportunities=[opp_with_rank])
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)
        assert "rank" not in result["opportunities"][0]

    def test_scorer_initial_composite_is_float_in_range(self, tmp_path):
        """Even after stripping, opportunities must carry a valid scorer-assigned composite."""
        batch = _make_batch(opportunities=[_make_opp(), _make_opp(
            topic_text="Another Ancient Mystery",
            mystery_hook="What caused the collapse of an entire civilization overnight?",
        )])
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)
        for opp in result["opportunities"]:
            assert isinstance(opp["initial_composite"], float)
            assert 0.0 <= opp["initial_composite"] <= 1.0


# ---------------------------------------------------------------------------
# 3. canonical_subject injection
# ---------------------------------------------------------------------------

class TestCanonicalSubject:

    def test_canonical_subject_present_in_output(self, tmp_path):
        batch = _make_batch()
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)
        assert "canonical_subject" in result["opportunities"][0]

    def test_canonical_subject_derived_from_topic_text(self, tmp_path):
        """canonical_subject must equal _make_canonical_subject(topic_text)."""
        topic = "The Lost City of Helike"
        batch = _make_batch(opportunities=[_make_opp(topic_text=topic)])
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)
        opp = result["opportunities"][0]
        assert opp["canonical_subject"] == _make_canonical_subject(topic)

    def test_canonical_subject_not_from_llm_batch(self, tmp_path):
        """If LLM sneaks canonical_subject into the batch, it must be overwritten by Python."""
        opp_with_canonical = {**_make_opp(), "canonical_subject": "llm_injected_value"}
        batch = _make_batch(opportunities=[opp_with_canonical])
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)
        opp = result["opportunities"][0]
        expected = _make_canonical_subject(_make_opp()["topic_text"])
        assert opp["canonical_subject"] == expected
        assert opp["canonical_subject"] != "llm_injected_value"

    def test_canonical_subject_angle_independent(self):
        """Same topic_text with different angles must produce the same canonical_subject."""
        topic = "The Architecture of Ancient Rome"
        opp_a = _make_opp(topic_text=topic, angle="Political angle")
        opp_b = _make_opp(topic_text=topic, angle="Engineering angle")
        ctx = _empty_signal_ctx()
        enriched_a = _enrich_and_filter([opp_a], ctx, None)
        enriched_b = _enrich_and_filter([opp_b], ctx, None)
        assert enriched_a[0]["canonical_subject"] == enriched_b[0]["canonical_subject"]


# ---------------------------------------------------------------------------
# 4. initial_composite from scorer
# ---------------------------------------------------------------------------

class TestInitialComposite:

    def test_initial_composite_assigned_by_scorer(self, tmp_path):
        """initial_composite must be computed by compute_score, not taken from LLM."""
        from hypogeum.initial_scorer import compute_score, _BASE_SCORE
        opp = _make_opp()
        batch = _make_batch(opportunities=[opp])
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)
        enriched = result["opportunities"][0]
        # Scorer must produce >= BASE_SCORE for a well-formed candidate
        assert enriched["initial_composite"] >= _BASE_SCORE

    def test_longer_mystery_hook_increases_score(self, tmp_path):
        """A developed mystery_hook should produce a higher score than a stub."""
        opp_good = _make_opp(
            topic_text="Good Topic With Long Hook",
            mystery_hook="Why did an entire ancient civilization disappear without trace?"
        )
        opp_stub = _make_opp(
            topic_text="Another Topic Same Length",
            mystery_hook="Short"
        )
        batch = _make_batch(opportunities=[opp_good, opp_stub])
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)
        good_score = next(
            o["initial_composite"] for o in result["opportunities"]
            if "Good Topic" in o["topic_text"]
        )
        stub_score = next(
            o["initial_composite"] for o in result["opportunities"]
            if "Another Topic" in o["topic_text"]
        )
        assert good_score > stub_score

    def test_signal_boost_increases_score(self, tmp_path):
        """top_performer_keywords matching topic_text must increase initial_composite."""
        opp = _make_opp(topic_text="Helike Ancient Greek Mystery")
        batch = _make_batch(opportunities=[opp])
        ctx_no_boost = _empty_signal_ctx()
        ctx_boosted = {
            **_empty_signal_ctx(),
            "has_real_analytics": True,
            "top_performer_keywords": {"helike", "ancient", "greek"},
        }
        no_boost_opps = _enrich_and_filter([opp], ctx_no_boost, None)
        boosted_opps = _enrich_and_filter([opp], ctx_boosted, None)
        assert boosted_opps[0]["initial_composite"] > no_boost_opps[0]["initial_composite"]


# ---------------------------------------------------------------------------
# 5. Covered-topic filtering
# ---------------------------------------------------------------------------

class TestCoveredFiltering:

    def test_covered_canonical_subject_removed(self, tmp_path):
        topic = "The Lost City of Helike"
        canonical = _make_canonical_subject(topic)
        opp = _make_opp(topic_text=topic)
        batch = _make_batch(opportunities=[opp])
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator(
                "ch_test", _make_config(),
                output_dir=tmp_path,
                existing_topics={canonical},
            )
        assert all(
            o["canonical_subject"] != canonical
            for o in result["opportunities"]
        )

    def test_uncovered_passes_filter(self, tmp_path):
        opp = _make_opp(topic_text="Uncovered New Topic Here")
        batch = _make_batch(opportunities=[opp])
        covered = {"some_entirely_different_canonical"}
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator(
                "ch_test", _make_config(),
                output_dir=tmp_path,
                existing_topics=covered,
            )
        assert len(result["opportunities"]) == 1

    def test_none_existing_topics_no_filter(self, tmp_path):
        batch = _make_batch(opportunities=[_make_opp(), _make_opp(
            topic_text="Second Distinct Topic Here",
        )])
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator(
                "ch_test", _make_config(),
                output_dir=tmp_path,
                existing_topics=None,
            )
        assert len(result["opportunities"]) == 2

    def test_all_covered_returns_empty_list(self, tmp_path):
        topic = "Completely Covered Topic Already"
        canonical = _make_canonical_subject(topic)
        batch = _make_batch(opportunities=[_make_opp(topic_text=topic)])
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator(
                "ch_test", _make_config(),
                output_dir=tmp_path,
                existing_topics={canonical},
            )
        assert result["opportunities"] == []


# ---------------------------------------------------------------------------
# 6. Fallback on LLM failure
# ---------------------------------------------------------------------------

class TestFallbackOnFailure:

    def _run_with_failure(self, tmp_path, exc=RuntimeError("LLM call failed")):
        with patch("hypogeum.discovery.topic_ideator.run_agent", side_effect=exc), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            return run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)

    def test_fallback_source_on_agent_error(self, tmp_path):
        result = self._run_with_failure(tmp_path)
        assert result["source"] == "fallback"

    def test_ok_true_on_fallback(self, tmp_path):
        """Fallback must succeed — never raise."""
        result = self._run_with_failure(tmp_path)
        assert result["ok"] is True

    def test_fallback_returns_non_empty_opportunities(self, tmp_path):
        result = self._run_with_failure(tmp_path)
        assert len(result["opportunities"]) > 0

    def test_schema_error_triggers_fallback(self, tmp_path):
        from hypogeum.schemas import SchemaError
        exc = SchemaError("opportunity_batch", "opportunities", "missing required field")
        result = self._run_with_failure(tmp_path, exc=exc)
        assert result["source"] == "fallback"

    def test_connection_error_triggers_fallback(self, tmp_path):
        result = self._run_with_failure(tmp_path, exc=ConnectionError("network down"))
        assert result["source"] == "fallback"


# ---------------------------------------------------------------------------
# 7. Fallback opportunity shape
# ---------------------------------------------------------------------------

class TestFallbackShape:

    def _fallback_opps(self):
        return _seeds_to_opportunities("ch_test")

    def test_fallback_has_six_string_fields(self):
        for opp in self._fallback_opps():
            for field in _SIX_FIELDS:
                assert field in opp, f"Missing field {field!r} in fallback opportunity"
                assert isinstance(opp[field], str), f"{field!r} must be a string"
                assert opp[field], f"{field!r} must be non-empty"

    def test_fallback_no_numeric_fields(self):
        for opp in self._fallback_opps():
            assert "initial_composite" not in opp
            assert "score" not in opp
            assert "canonical_subject" not in opp

    def test_enrich_and_filter_adds_canonical_and_composite(self):
        opps = self._fallback_opps()
        enriched = _enrich_and_filter(opps, _empty_signal_ctx(), None)
        assert len(enriched) == len(opps)
        for opp in enriched:
            assert "canonical_subject" in opp
            assert "initial_composite" in opp
            assert isinstance(opp["initial_composite"], float)

    def test_fallback_composite_in_valid_range(self, tmp_path):
        with patch("hypogeum.discovery.topic_ideator.run_agent",
                   side_effect=RuntimeError("down")), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)
        for opp in result["opportunities"]:
            assert 0.0 <= opp["initial_composite"] <= 1.0


# ---------------------------------------------------------------------------
# 8. generation_source propagation
# ---------------------------------------------------------------------------

class TestGenerationSource:

    def _run_llm(self, tmp_path):
        batch = _make_batch()
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            return run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)

    def _run_fallback(self, tmp_path):
        with patch("hypogeum.discovery.topic_ideator.run_agent",
                   side_effect=RuntimeError("LLM down")), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            return run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)

    def test_llm_path_sets_llm_ideation(self, tmp_path):
        result = self._run_llm(tmp_path)
        assert result["source"] == "llm"
        for opp in result["opportunities"]:
            assert opp.get("generation_source") == "llm_ideation", (
                f"Expected llm_ideation, got {opp.get('generation_source')!r}"
            )

    def test_fallback_path_sets_fallback_seed(self, tmp_path):
        result = self._run_fallback(tmp_path)
        assert result["source"] == "fallback"
        for opp in result["opportunities"]:
            assert opp.get("generation_source") == "fallback_seed", (
                f"Expected fallback_seed, got {opp.get('generation_source')!r}"
            )

    def test_generation_source_present_on_all_opportunities(self, tmp_path):
        batch = _make_batch(opportunities=[
            _make_opp(topic_text="Topic One", mystery_hook="Why did Topic One vanish?"),
            _make_opp(topic_text="Topic Two", mystery_hook="What happened to Topic Two?"),
        ])
        with patch("hypogeum.discovery.topic_ideator.run_agent", return_value=batch), \
             patch("hypogeum.discovery.topic_ideator._build_signal_context",
                   return_value=_empty_signal_ctx()), \
             patch("hypogeum.discovery.topic_ideator.get_connection"):
            result = run_topic_ideator("ch_test", _make_config(), output_dir=tmp_path)
        assert len(result["opportunities"]) == 2
        for opp in result["opportunities"]:
            assert "generation_source" in opp

    def test_generation_source_valid_values_only(self, tmp_path):
        """generation_source must be one of the two defined values."""
        valid = {"llm_ideation", "fallback_seed"}
        for runner in (self._run_llm, self._run_fallback):
            result = runner(tmp_path)
            for opp in result["opportunities"]:
                assert opp.get("generation_source") in valid


# ---------------------------------------------------------------------------
# 9. V1 regression — v1 scorer and cycle unaffected
# ---------------------------------------------------------------------------

class TestV1Regression:

    def test_score_seed_uses_constants_not_env_vars(self):
        """_score_seed must use named constants, not os.getenv — v1 is frozen."""
        from hypogeum.topic_discovery import _score_seed, _SIGNAL_BOOST_TOP_PERFORMER
        seed = {
            "topic": "The hidden history of ancient underground cities",
            "source": "heuristic",
            "ctr_rationale": "", "retention_rationale": "",
            "watch_time_rationale": "", "risk": "",
        }
        ctx = {
            "has_real_analytics": True,
            "top_performer_keywords": {"hidden", "ancient", "underground"},
            "high_retention_keywords": set(),
            "penalty_keywords": set(),
            "avg_ctr": 0.05, "avg_view_pct": 0.60, "avg_watch_time_s": 300.0,
        }
        base = _score_seed(seed, set(), None)
        boosted = _score_seed(seed, set(), ctx)
        assert boosted == round(base + _SIGNAL_BOOST_TOP_PERFORMER, 4)

    def test_run_discovery_cycle_v1_still_produces_candidates(self, tmp_path):
        from hypogeum.db import initialize_schema
        from hypogeum.topic_discovery import run_discovery_cycle
        import os
        db_file = tmp_path / "test.db"
        initialize_schema(db_file)
        old = os.environ.get("DISCOVERY_ENGINE")
        try:
            os.environ.pop("DISCOVERY_ENGINE", None)
            result = run_discovery_cycle("ch_v1_regression", db_path=db_file)
        finally:
            if old is None:
                os.environ.pop("DISCOVERY_ENGINE", None)
            else:
                os.environ["DISCOVERY_ENGINE"] = old
        assert result["ok"] is True
        assert result["candidate_count"] == 7
        assert result["selected_candidate_id"] is not None

    def test_v1_scores_unchanged(self):
        """v1 heuristic base scores must not drift."""
        from hypogeum.topic_discovery import _score_seed
        expected = {
            "The hidden history of ancient underground cities": 0.82,
            "Lost civilisations: what archaeology refuses to explain": 0.78,
            "The architecture of power: buildings that shaped empires": 0.71,
            "Ancient trade routes that built the modern world": 0.67,
            "The real story behind the world's most mysterious ruins": 0.74,
            "How ancient engineers solved problems we still can't explain": 0.69,
            "Secret societies and the empires they shaped": 0.64,
        }
        for topic, expected_score in expected.items():
            seed = {"topic": topic, "source": "heuristic",
                    "ctr_rationale": "", "retention_rationale": "",
                    "watch_time_rationale": "", "risk": ""}
            assert _score_seed(seed, set(), None) == expected_score, (
                f"v1 score drifted for {topic!r}"
            )
