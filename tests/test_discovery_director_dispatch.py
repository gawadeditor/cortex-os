"""
tests/test_discovery_director_dispatch.py — Discovery Director dispatch layer (phases 1–4)

Tests:
  TestDispatch             — run_discovery_cycle() routes to v1 or v2 based on env var
  TestNormalizeTopicText   — normalize_topic_text() article-stripping contract
  TestCanonicalSubject     — _make_canonical_subject() slugification + normalization
  TestOpportunityBatchParser   — parse_opportunity_batch() strips unknown fields
  TestOpportunityBatch     — opportunity_batch schema validator (post-parse)
"""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

from hypogeum.topic_discovery import (
    run_discovery_cycle,
    normalize_topic_text,
    _make_canonical_subject,
)
from hypogeum.schemas import validate, parse_opportunity_batch, SchemaError


# ---------------------------------------------------------------------------
# TestDispatch
# ---------------------------------------------------------------------------

class TestDispatch:

    @pytest.fixture
    def db(self, tmp_path):
        """Isolated DB for run_discovery_cycle()'s own upsert_discovery_state()
        writes — which fire before dispatch, regardless of the mocked engine.
        Without this the write falls through to the production default and the
        conftest tripwire blocks it."""
        from hypogeum.db import initialize_schema
        p = tmp_path / "discovery.db"
        initialize_schema(p)
        return p

    def test_v1_default_calls_v1_impl(self, db):
        """No DISCOVERY_ENGINE set → dispatches to v1 heuristic."""
        called = []

        def fake_v1(channel_id, db_path=None):
            called.append("v1")
            return {"ok": True, "cycle_id": "c1", "channel_id": channel_id,
                    "candidate_count": 0, "selected_candidate_id": None, "candidates": []}

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISCOVERY_ENGINE", None)
            with patch("hypogeum.topic_discovery._run_discovery_cycle_v1", side_effect=fake_v1):
                result = run_discovery_cycle("ch_test", db_path=db)

        assert called == ["v1"]
        assert result["ok"] is True

    def test_v1_explicit_calls_v1_impl(self, db):
        """DISCOVERY_ENGINE=v1 → dispatches to v1 heuristic."""
        called = []

        def fake_v1(channel_id, db_path=None):
            called.append("v1")
            return {"ok": True, "cycle_id": "c1", "channel_id": channel_id,
                    "candidate_count": 0, "selected_candidate_id": None, "candidates": []}

        with patch.dict(os.environ, {"DISCOVERY_ENGINE": "v1"}):
            with patch("hypogeum.topic_discovery._run_discovery_cycle_v1", side_effect=fake_v1):
                result = run_discovery_cycle("ch_test", db_path=db)

        assert called == ["v1"]
        assert result["ok"] is True

    def test_v2_dispatches_to_director(self, db):
        """DISCOVERY_ENGINE=v2 → calls discovery_director.run(); v1 is never called."""
        v1_called = []

        def fake_v1(channel_id, db_path=None):
            v1_called.append("v1")
            return {}

        def fake_director(channel_id, config=None, db_path=None, triggered_by="worker_refill"):
            return {
                "ok": True, "engine_used": "v2", "cycle_id": "cyc_test",
                "channel_id": channel_id, "candidate_count": 7,
                "verified_count": 3, "selected_opportunity_id": None,
                "topic_id_enqueued": None, "route_status": "pending_approval",
                "opportunities": [],
            }

        with patch.dict(os.environ, {"DISCOVERY_ENGINE": "v2"}):
            with patch("hypogeum.topic_discovery._run_discovery_cycle_v1", side_effect=fake_v1):
                with patch("hypogeum.agents.discovery_director.run", side_effect=fake_director):
                    result = run_discovery_cycle("ch_test", db_path=db)

        assert v1_called == [], "v1 must not be called when DISCOVERY_ENGINE=v2"
        assert result["ok"] is True
        assert result["engine_used"] == "v2"

    def test_v2_does_not_call_v1(self, db):
        """Confirm v2 director isolation — v1 impl is entirely bypassed."""
        def fake_director(channel_id, config=None, db_path=None, triggered_by="worker_refill"):
            return {"ok": True, "engine_used": "v2", "cycle_id": "c", "channel_id": channel_id,
                    "candidate_count": 0, "verified_count": 0, "selected_opportunity_id": None,
                    "topic_id_enqueued": None, "route_status": "no_candidate", "opportunities": []}

        with patch.dict(os.environ, {"DISCOVERY_ENGINE": "v2"}):
            with patch("hypogeum.topic_discovery._run_discovery_cycle_v1") as mock_v1:
                with patch("hypogeum.agents.discovery_director.run", side_effect=fake_director):
                    run_discovery_cycle("ch_any", db_path=db)
        mock_v1.assert_not_called()

    def test_unknown_engine_falls_back_to_v1(self, db):
        """Any unrecognised DISCOVERY_ENGINE value falls back to v1."""
        called = []

        def fake_v1(channel_id, db_path=None):
            called.append("v1")
            return {"ok": True, "cycle_id": "c1", "channel_id": channel_id,
                    "candidate_count": 0, "selected_candidate_id": None, "candidates": []}

        with patch.dict(os.environ, {"DISCOVERY_ENGINE": "v99"}):
            with patch("hypogeum.topic_discovery._run_discovery_cycle_v1", side_effect=fake_v1):
                result = run_discovery_cycle("ch_test", db_path=db)

        assert called == ["v1"]


# ---------------------------------------------------------------------------
# TestNormalizeTopicText
# ---------------------------------------------------------------------------

class TestNormalizeTopicText:
    """
    normalize_topic_text() applies the full normalization pipeline:
      trim → lowercase → strip leading article → remove punctuation → collapse whitespace
    Output is always lowercase with single spaces and no punctuation.
    """

    def test_strips_leading_the(self):
        assert normalize_topic_text("The Lost City of Helike") == "lost city of helike"

    def test_strips_leading_the_already_lowercase(self):
        assert normalize_topic_text("the lost city of helike") == "lost city of helike"

    def test_strips_leading_a(self):
        assert normalize_topic_text("A Hidden History of Rome") == "hidden history of rome"

    def test_strips_leading_an(self):
        assert normalize_topic_text("An Ancient Mystery Solved") == "ancient mystery solved"

    def test_strips_mixed_case_article(self):
        assert normalize_topic_text("THE Forgotten Empire") == "forgotten empire"

    def test_no_article_unchanged(self):
        # No article to strip; text still lowercased.
        assert normalize_topic_text("Lost City of Helike") == "lost city of helike"

    def test_article_in_middle_not_stripped(self):
        # Only leading article is stripped.
        assert normalize_topic_text("Secrets of the Ancient World") == "secrets of the ancient world"

    def test_strips_leading_whitespace(self):
        assert normalize_topic_text("  The Hidden Temple  ") == "hidden temple"

    def test_empty_string(self):
        assert normalize_topic_text("") == ""

    def test_only_article(self):
        # "The" with no following whitespace+word: article regex requires \s+, no match.
        assert normalize_topic_text("The") == "the"

    def test_article_followed_by_nothing(self):
        assert normalize_topic_text("The ") == "the"

    def test_punctuation_removed(self):
        assert normalize_topic_text("Lost City of Helike?") == "lost city of helike"

    def test_trailing_exclamation_removed(self):
        assert normalize_topic_text("Lost City of Helike!") == "lost city of helike"

    def test_colon_removed(self):
        assert normalize_topic_text("Egypt: A Hidden History") == "egypt a hidden history"

    def test_apostrophe_removed(self):
        assert normalize_topic_text("Egypt's Lost Temples") == "egypts lost temples"

    def test_all_design_doc_variants_converge(self):
        """The five variants from the approved design doc must all normalize identically."""
        expected = "lost city of helike"
        variants = [
            "Lost City of Helike",
            "The Lost City of Helike",
            "Lost City Of Helike",
            "Lost City of Helike?",
            "Lost City of Helike!",
        ]
        for v in variants:
            assert normalize_topic_text(v) == expected, f"Failed for: {v!r}"

    def test_whitespace_collapsed(self):
        assert normalize_topic_text("Lost   City  of   Helike") == "lost city of helike"


# ---------------------------------------------------------------------------
# TestCanonicalSubject
# ---------------------------------------------------------------------------

class TestCanonicalSubject:

    def test_basic_normalization(self):
        result = _make_canonical_subject("Lost City of Helike")
        assert result == "lost_city_of_helike"

    def test_with_article_matches_without(self):
        """Core contract: 'The X' and 'X' produce the same canonical_subject."""
        assert _make_canonical_subject("The Lost City of Helike") == \
               _make_canonical_subject("Lost City of Helike")

    def test_a_article_matches_without(self):
        assert _make_canonical_subject("A Hidden History of Rome") == \
               _make_canonical_subject("Hidden History of Rome")

    def test_an_article_matches_without(self):
        assert _make_canonical_subject("An Ancient Mystery Solved") == \
               _make_canonical_subject("Ancient Mystery Solved")

    def test_lowercase(self):
        assert _make_canonical_subject("ANCIENT ROME") == "ancient_rome"

    def test_punctuation_removed(self):
        assert _make_canonical_subject("Egypt's Pyramids: A Mystery!") == \
               "egypts_pyramids_a_mystery"

    def test_whitespace_collapsed_to_underscore(self):
        assert _make_canonical_subject("  deep   sea   ruins  ") == "deep_sea_ruins"

    def test_same_text_same_output(self):
        text = "The Hidden Temples of Southeast Asia"
        assert _make_canonical_subject(text) == _make_canonical_subject(text)

    def test_same_topic_different_angles_same_canonical_subject(self):
        """Core contract: angle is irrelevant to canonical_subject."""
        topic = "Lost City of Helike"
        canonical = _make_canonical_subject(topic)
        assert "poseidon" not in canonical
        assert "trade" not in canonical
        assert "earthquake" not in canonical
        assert canonical == "lost_city_of_helike"

    def test_max_200_chars(self):
        long_text = "ancient " * 50  # 400 chars
        result = _make_canonical_subject(long_text)
        assert len(result) <= 200

    def test_empty_string(self):
        assert _make_canonical_subject("") == ""

    def test_only_punctuation(self):
        assert _make_canonical_subject("!!!---???") == ""

    def test_numbers_preserved(self):
        # Article "The" is stripped; number and remaining words are kept
        assert _make_canonical_subject("The 7 Wonders of the Ancient World") == \
               "7_wonders_of_the_ancient_world"

    def test_unicode_not_in_output(self):
        result = _make_canonical_subject("Hérodote's Histories")
        assert all(c.isascii() for c in result)

    def test_deterministic_across_calls(self):
        text = "Pompeii: Life Before the Eruption"
        results = [_make_canonical_subject(text) for _ in range(5)]
        assert len(set(results)) == 1

    def test_all_design_doc_variants_converge(self):
        """All five variants from the approved design doc must produce the same slug."""
        variants = [
            "Lost City of Helike",
            "The Lost City of Helike",
            "Lost City Of Helike",
            "Lost City of Helike?",
            "Lost City of Helike!",
        ]
        slugs = {_make_canonical_subject(v) for v in variants}
        assert len(slugs) == 1, f"Expected 1 unique slug, got: {slugs}"
        assert slugs.pop() == "lost_city_of_helike"


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

def _valid_opp(**overrides):
    base = {
        "topic_text": "Lost City of Helike",
        "angle": "The Poseidon legend connection",
        "mystery_hook": "Why did an entire city vanish overnight?",
        "differentiation_claim": "First documentary to use underwater sonar data",
        "visual_signal": "Underwater ruins at golden hour",
        "evidence_signal": "2001 sonar survey by Katsonopoulou and Soter",
    }
    base.update(overrides)
    return base


def _valid_batch(**overrides):
    base = {
        "batch_id": "batch_001",
        "channel_id": "ch_test",
        "engine": "v2",
        "opportunities": [_valid_opp()],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# TestOpportunityBatchParser
# ---------------------------------------------------------------------------

class TestOpportunityBatchParser:

    def test_valid_batch_unchanged(self):
        """A clean batch with only approved fields passes through unmodified."""
        batch = _valid_batch()
        result = parse_opportunity_batch(batch)
        assert result["opportunities"][0] == _valid_opp()

    def test_numeric_field_stripped(self):
        """initial_composite (or any numeric) is stripped, not rejected."""
        opp = {**_valid_opp(), "initial_composite": 0.85}
        batch = _valid_batch(opportunities=[opp])
        result = parse_opportunity_batch(batch)
        assert "initial_composite" not in result["opportunities"][0]

    def test_integer_field_stripped(self):
        opp = {**_valid_opp(), "rank": 1}
        batch = _valid_batch(opportunities=[opp])
        result = parse_opportunity_batch(batch)
        assert "rank" not in result["opportunities"][0]

    def test_unknown_string_field_stripped(self):
        opp = {**_valid_opp(), "extra_note": "something extra"}
        batch = _valid_batch(opportunities=[opp])
        result = parse_opportunity_batch(batch)
        assert "extra_note" not in result["opportunities"][0]

    def test_required_fields_preserved(self):
        """All six approved fields survive stripping."""
        opp = {**_valid_opp(), "initial_composite": 0.9, "rank": 2}
        batch = _valid_batch(opportunities=[opp])
        result = parse_opportunity_batch(batch)
        cleaned = result["opportunities"][0]
        for field in ("topic_text", "angle", "mystery_hook",
                      "differentiation_claim", "visual_signal", "evidence_signal"):
            assert field in cleaned

    def test_candidate_accepted_after_strip(self):
        """After stripping, the batch passes schema validation."""
        opp = {**_valid_opp(), "initial_composite": 0.85, "rank": 1}
        batch = _valid_batch(opportunities=[opp])
        cleaned = parse_opportunity_batch(batch)
        validate("opportunity_batch", cleaned)  # must not raise

    def test_numeric_scoring_field_logs_distinct_message(self, caplog):
        """Known scoring fields (initial_composite, score, rank, …) log 'numeric scoring field stripped'."""
        opp = {**_valid_opp(), "initial_composite": 0.9}
        batch = _valid_batch(opportunities=[opp])
        with caplog.at_level(logging.WARNING, logger="hypogeum.schemas"):
            parse_opportunity_batch(batch)
        messages = [r.message for r in caplog.records]
        assert any("numeric scoring field stripped" in m and "initial_composite" in m
                   for m in messages)

    def test_unknown_field_logs_distinct_message(self, caplog):
        """Unknown non-scoring fields log 'unknown field stripped'."""
        opp = {**_valid_opp(), "extra_metadata": "some note"}
        batch = _valid_batch(opportunities=[opp])
        with caplog.at_level(logging.WARNING, logger="hypogeum.schemas"):
            parse_opportunity_batch(batch)
        messages = [r.message for r in caplog.records]
        assert any("unknown field stripped" in m and "extra_metadata" in m
                   for m in messages)

    def test_rank_classified_as_scoring_field(self, caplog):
        opp = {**_valid_opp(), "rank": 2}
        batch = _valid_batch(opportunities=[opp])
        with caplog.at_level(logging.WARNING, logger="hypogeum.schemas"):
            parse_opportunity_batch(batch)
        messages = [r.message for r in caplog.records]
        assert any("numeric scoring field stripped" in m and "rank" in m
                   for m in messages)

    def test_score_classified_as_scoring_field(self, caplog):
        opp = {**_valid_opp(), "score": 0.7}
        batch = _valid_batch(opportunities=[opp])
        with caplog.at_level(logging.WARNING, logger="hypogeum.schemas"):
            parse_opportunity_batch(batch)
        messages = [r.message for r in caplog.records]
        assert any("numeric scoring field stripped" in m for m in messages)

    def test_multiple_opportunities_all_cleaned(self):
        opp1 = {**_valid_opp(), "score": 0.7}
        opp2 = {**_valid_opp(topic_text="Ancient Rome"), "rank": 2}
        batch = _valid_batch(opportunities=[opp1, opp2])
        result = parse_opportunity_batch(batch)
        assert "score" not in result["opportunities"][0]
        assert "rank" not in result["opportunities"][1]

    def test_non_dict_opportunity_passed_through(self):
        """Non-dict items are passed through; the validator will reject them."""
        batch = _valid_batch(opportunities=["not a dict"])
        result = parse_opportunity_batch(batch)
        assert result["opportunities"][0] == "not a dict"

    def test_top_level_batch_fields_preserved(self):
        batch = _valid_batch()
        result = parse_opportunity_batch(batch)
        assert result["batch_id"] == "batch_001"
        assert result["channel_id"] == "ch_test"
        assert result["engine"] == "v2"

    def test_does_not_mutate_input(self):
        """parse_opportunity_batch returns a new dict; input is not modified."""
        opp = {**_valid_opp(), "initial_composite": 0.9}
        batch = _valid_batch(opportunities=[opp])
        original_keys = set(batch["opportunities"][0].keys())
        parse_opportunity_batch(batch)
        assert set(batch["opportunities"][0].keys()) == original_keys


# ---------------------------------------------------------------------------
# TestOpportunityBatch (schema validator — expects pre-parsed input)
# ---------------------------------------------------------------------------

class TestOpportunityBatch:

    def test_valid_batch_passes(self):
        validate("opportunity_batch", _valid_batch())

    def test_missing_batch_id_raises(self):
        batch = _valid_batch()
        del batch["batch_id"]
        with pytest.raises(SchemaError) as exc_info:
            validate("opportunity_batch", batch)
        assert "batch_id" in exc_info.value.path

    def test_missing_channel_id_raises(self):
        batch = _valid_batch()
        del batch["channel_id"]
        with pytest.raises(SchemaError):
            validate("opportunity_batch", batch)

    def test_empty_opportunities_raises(self):
        batch = _valid_batch(opportunities=[])
        with pytest.raises(SchemaError) as exc_info:
            validate("opportunity_batch", batch)
        assert "opportunities" in exc_info.value.path

    def test_missing_required_opportunity_field_raises(self):
        opp = {k: v for k, v in _valid_opp().items() if k != "mystery_hook"}
        batch = _valid_batch(opportunities=[opp])
        with pytest.raises(SchemaError) as exc_info:
            validate("opportunity_batch", batch)
        assert "mystery_hook" in exc_info.value.path

    def test_all_six_fields_required(self):
        """Each of the six fields, when removed, causes a SchemaError."""
        for field in ("topic_text", "angle", "mystery_hook",
                      "differentiation_claim", "visual_signal", "evidence_signal"):
            opp = {k: v for k, v in _valid_opp().items() if k != field}
            batch = _valid_batch(opportunities=[opp])
            with pytest.raises(SchemaError):
                validate("opportunity_batch", batch)

    def test_opportunity_not_dict_raises(self):
        batch = _valid_batch(opportunities=["not a dict"])
        with pytest.raises(SchemaError) as exc_info:
            validate("opportunity_batch", batch)
        assert "opportunities[0]" in exc_info.value.path

    def test_numeric_field_does_not_raise_after_parse(self):
        """Validator is lenient; numeric fields only matter at parse time."""
        opp = {**_valid_opp(), "initial_composite": 0.85}
        batch = _valid_batch(opportunities=[opp])
        cleaned = parse_opportunity_batch(batch)
        validate("opportunity_batch", cleaned)  # must not raise
