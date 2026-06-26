"""
tests/test_source_verifier.py — Unit tests for hypogeum/discovery/source_verifier.py

Tests:
  ScoreQualification   — threshold gate, delta gate, below-both is skipped, no top-N
  Persistence          — verified candidate written to source_verifications table
  Caching              — cache hit skips Perplexity; returns verification_id
  PerplexityFailure    — exception → failed status, no crash, other candidates proceed
  OutputShape          — all required fields present, correct types and values
  V1Regression         — v1 scorer and run_discovery_cycle unaffected

All DB tests use real in-memory SQLite via initialize_schema.
Perplexity calls are always mocked — no network.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.discovery.source_verifier import (
    run_source_verifier,
    _is_qualified,
    _source_score,
)
from hypogeum.topic_discovery import _make_canonical_subject


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(threshold=0.65, delta=0.10):
    cfg = SimpleNamespace()
    cfg.DISCOVERY_VERIFY_THRESHOLD = threshold
    cfg.DISCOVERY_VERIFY_DELTA = delta
    cfg.PERPLEXITY_API_KEY = "test-key"
    cfg.PERPLEXITY_MODEL = "sonar-pro"
    cfg.PERPLEXITY_MAX_TOKENS = 512
    return cfg


def _candidate(topic_text="Lost City of Helike", initial_composite=0.75, **kw):
    return {
        "topic_text":            topic_text,
        "angle":                 "The underwater mystery",
        "mystery_hook":          "Why did an entire city vanish beneath the waves?",
        "differentiation_claim": "First video using 2001 sonar data from Katsonopoulou",
        "visual_signal":         "Underwater ruins at golden hour",
        "evidence_signal":       "2001 sonar survey",
        "canonical_subject":     _make_canonical_subject(topic_text),
        "initial_composite":     initial_composite,
        **kw,
    }


def _mock_plex(citations=None):
    """Return a mock PerplexityResult with the given citation URLs."""
    result = MagicMock()
    result.citations = citations if citations is not None else [
        "https://example.com/source1",
        "https://example.com/source2",
    ]
    return result


def _init_db(tmp_path: Path):
    """Initialize a real SQLite DB using the full schema."""
    from hypogeum.db import initialize_schema, get_connection
    db = tmp_path / "test.db"
    initialize_schema(db)
    return db


# ---------------------------------------------------------------------------
# 1. Score Qualification
# ---------------------------------------------------------------------------

class TestScoreQualification:

    def test_above_threshold_is_qualified(self):
        c = _candidate(initial_composite=0.70)
        assert _is_qualified(c, best_score=0.70, threshold=0.65, delta=0.10)

    def test_equal_to_threshold_is_qualified(self):
        c = _candidate(initial_composite=0.65)
        assert _is_qualified(c, best_score=0.65, threshold=0.65, delta=0.10)

    def test_below_threshold_within_delta_is_qualified(self):
        # best=0.80, delta=0.10 → floor=0.70; candidate=0.72 → qualifies
        c = _candidate(initial_composite=0.72)
        assert _is_qualified(c, best_score=0.80, threshold=0.65, delta=0.10)

    def test_below_threshold_and_outside_delta_is_not_qualified(self):
        # best=0.80, delta=0.10 → floor=0.70; candidate=0.55 < 0.65 threshold
        # and 0.55 < 0.70 delta-floor → not qualified
        c = _candidate(initial_composite=0.55)
        assert not _is_qualified(c, best_score=0.80, threshold=0.65, delta=0.10)

    def test_skipped_candidate_has_correct_fields(self, tmp_path):
        # best=0.80, threshold=0.65, delta=0.10 → floor=0.70
        # candidate at 0.30 is below both gates → skipped
        db = _init_db(tmp_path)
        high = _candidate(topic_text="High Scoring Topic", initial_composite=0.80)
        low  = _candidate(topic_text="Low Scoring Topic",  initial_composite=0.30)
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(["https://src.com"])) as mock_px:
            results = run_source_verifier([high, low], _cfg(), db_path=db)
        low_result = next(r for r in results if r["topic_text"] == "Low Scoring Topic")
        assert low_result["verification_status"] == "skipped"
        assert low_result["source_count"] == 0
        assert low_result["source_score"] == 0.0
        assert low_result["source_urls"] == []
        assert mock_px.call_count == 1  # only the high-scoring one called Perplexity

    def test_no_fixed_top_n_all_qualify_when_above_threshold(self, tmp_path):
        """When 8 candidates are all above threshold, all 8 must be verified."""
        db = _init_db(tmp_path)
        candidates = [
            _candidate(topic_text=f"High Score Topic {i}", initial_composite=0.70 + i * 0.01)
            for i in range(8)
        ]
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(["https://source.com/ref"])):
            results = run_source_verifier(candidates, _cfg(), db_path=db)
        verified = [r for r in results if r["verification_status"] == "verified"]
        assert len(verified) == 8

    def test_only_above_threshold_qualified_rest_skipped(self, tmp_path):
        db = _init_db(tmp_path)
        candidates = [
            _candidate(topic_text="High Topic", initial_composite=0.80),
            _candidate(topic_text="Low Topic One", initial_composite=0.30),
            _candidate(topic_text="Low Topic Two", initial_composite=0.20),
        ]
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(["https://source.com/ref"])):
            results = run_source_verifier(candidates, _cfg(), db_path=db)
        statuses = {r["topic_text"]: r["verification_status"] for r in results}
        assert statuses["High Topic"] == "verified"
        assert statuses["Low Topic One"] == "skipped"
        assert statuses["Low Topic Two"] == "skipped"

    def test_delta_gate_verifies_below_threshold_candidate(self, tmp_path):
        """Candidate below threshold but within delta of best must NOT be skipped."""
        db = _init_db(tmp_path)
        candidates = [
            _candidate(topic_text="Best Topic", initial_composite=0.80),
            _candidate(topic_text="Delta Topic", initial_composite=0.74),  # 0.80 - 0.10 = 0.70, so 0.74 qualifies
        ]
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(["https://source.com/ref"])):
            results = run_source_verifier(candidates, _cfg(threshold=0.65, delta=0.10), db_path=db)
        statuses = {r["topic_text"]: r["verification_status"] for r in results}
        assert statuses["Delta Topic"] != "skipped"


# ---------------------------------------------------------------------------
# 2. Persistence
# ---------------------------------------------------------------------------

class TestPersistence:

    def test_verified_candidate_written_to_db(self, tmp_path):
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.80)
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(["https://ref.com/paper"])):
            run_source_verifier([c], _cfg(), db_path=db)

        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT verified, source_url FROM source_verifications WHERE canonical_subject = ?",
                (c["canonical_subject"],),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "verified candidate must be persisted"
        assert row[0] == 1
        stored_urls = json.loads(row[1])
        assert "https://ref.com/paper" in stored_urls

    def test_verification_id_present_in_result(self, tmp_path):
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.80)
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(["https://ref.com/paper"])):
            results = run_source_verifier([c], _cfg(), db_path=db)
        assert "verification_id" in results[0]
        assert results[0]["verification_id"]

    def test_cycle_id_stored_in_db(self, tmp_path):
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.80)
        cycle = "cycle_abc"
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(["https://ref.com"])):
            run_source_verifier([c], _cfg(), cycle_id=cycle, db_path=db)

        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT cycle_id FROM source_verifications WHERE canonical_subject = ?",
                (c["canonical_subject"],),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == cycle

    def test_zero_citations_not_persisted(self, tmp_path):
        """0-citation result must not be written to DB so the next cycle retries."""
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.80)
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(citations=[])):
            results = run_source_verifier([c], _cfg(), db_path=db)

        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM source_verifications WHERE canonical_subject = ?",
                (c["canonical_subject"],),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0
        assert results[0]["verification_status"] == "failed"


# ---------------------------------------------------------------------------
# 3. Caching
# ---------------------------------------------------------------------------

class TestCaching:

    def test_cache_hit_skips_perplexity(self, tmp_path):
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.80)

        # First run — populates cache
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(["https://cache.com/src"])) as mock_px:
            run_source_verifier([c], _cfg(), db_path=db)
        assert mock_px.call_count == 1

        # Second run — must use cache, NOT call Perplexity again
        with patch("hypogeum.discovery.source_verifier.query_perplexity") as mock_px2:
            results = run_source_verifier([c], _cfg(), db_path=db)
        mock_px2.assert_not_called()
        assert results[0]["verification_status"] == "verified"

    def test_cache_hit_returns_verification_id(self, tmp_path):
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.80)
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(["https://cache.com/src"])):
            first = run_source_verifier([c], _cfg(), db_path=db)

        with patch("hypogeum.discovery.source_verifier.query_perplexity"):
            second = run_source_verifier([c], _cfg(), db_path=db)

        assert "verification_id" in second[0]
        # Same DB row → same verification_id
        assert second[0]["verification_id"] == first[0]["verification_id"]

    def test_cache_hit_preserves_source_urls(self, tmp_path):
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.80)
        urls = ["https://doi.org/paper1", "https://doi.org/paper2"]
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(urls)):
            run_source_verifier([c], _cfg(), db_path=db)

        with patch("hypogeum.discovery.source_verifier.query_perplexity"):
            results = run_source_verifier([c], _cfg(), db_path=db)

        assert results[0]["source_urls"] == urls
        assert results[0]["source_count"] == 2

    def test_different_canonical_subjects_not_shared(self, tmp_path):
        """Cache must not bleed between different canonical_subject values."""
        db = _init_db(tmp_path)
        c1 = _candidate(topic_text="Topic Alpha Here", initial_composite=0.80)
        c2 = _candidate(topic_text="Topic Beta Here",  initial_composite=0.80)

        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(["https://alpha.com"])) as mock_px:
            run_source_verifier([c1], _cfg(), db_path=db)
        assert mock_px.call_count == 1

        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(["https://beta.com"])) as mock_px2:
            run_source_verifier([c2], _cfg(), db_path=db)
        assert mock_px2.call_count == 1  # c2 must be a cache miss


# ---------------------------------------------------------------------------
# 4. Perplexity failure
# ---------------------------------------------------------------------------

class TestPerplexityFailure:

    def test_exception_returns_failed_status(self, tmp_path):
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.80)
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   side_effect=RuntimeError("API down")):
            results = run_source_verifier([c], _cfg(), db_path=db)
        assert results[0]["verification_status"] == "failed"

    def test_exception_does_not_crash(self, tmp_path):
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.80)
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   side_effect=Exception("hard failure")):
            results = run_source_verifier([c], _cfg(), db_path=db)
        assert len(results) == 1
        assert results[0]["verification_status"] == "failed"

    def test_one_failure_does_not_block_others(self, tmp_path):
        """An exception on candidate A must not prevent candidate B from being verified."""
        db = _init_db(tmp_path)
        c_fail = _candidate(topic_text="Topic That Fails", initial_composite=0.80)
        c_ok   = _candidate(topic_text="Topic That Succeeds", initial_composite=0.80)

        call_count = [0]
        def _flaky(query, cfg, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("first call fails")
            return _mock_plex(["https://good-source.com"])

        with patch("hypogeum.discovery.source_verifier.query_perplexity", side_effect=_flaky):
            results = run_source_verifier([c_fail, c_ok], _cfg(), db_path=db)

        statuses = {r["topic_text"]: r["verification_status"] for r in results}
        assert statuses["Topic That Fails"] == "failed"
        assert statuses["Topic That Succeeds"] == "verified"

    def test_failed_candidate_not_persisted(self, tmp_path):
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.80)
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   side_effect=RuntimeError("down")):
            run_source_verifier([c], _cfg(), db_path=db)

        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM source_verifications WHERE canonical_subject = ?",
                (c["canonical_subject"],),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_network_error_triggers_failed(self, tmp_path):
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.80)
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   side_effect=ConnectionError("timeout")):
            results = run_source_verifier([c], _cfg(), db_path=db)
        assert results[0]["verification_status"] == "failed"


# ---------------------------------------------------------------------------
# 5. Output shape
# ---------------------------------------------------------------------------

class TestOutputShape:

    def test_all_required_fields_on_verified(self, tmp_path):
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.80)
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(["https://src.com/1", "https://src.com/2"])):
            results = run_source_verifier([c], _cfg(), db_path=db)
        r = results[0]
        for field in ("verification_status", "source_count", "source_score", "source_urls"):
            assert field in r, f"missing field: {field}"
        assert "verification_id" in r

    def test_all_required_fields_on_failed(self, tmp_path):
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.80)
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   side_effect=RuntimeError("down")):
            results = run_source_verifier([c], _cfg(), db_path=db)
        r = results[0]
        for field in ("verification_status", "source_count", "source_score", "source_urls"):
            assert field in r, f"missing field: {field}"

    def test_all_required_fields_on_skipped(self, tmp_path):
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.10)
        with patch("hypogeum.discovery.source_verifier.query_perplexity"):
            results = run_source_verifier([c], _cfg(), db_path=db)
        r = results[0]
        for field in ("verification_status", "source_count", "source_score", "source_urls"):
            assert field in r, f"missing field: {field}"

    def test_source_score_range(self, tmp_path):
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.80)
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(["https://s.com/1", "https://s.com/2"])):
            results = run_source_verifier([c], _cfg(), db_path=db)
        assert 0.0 <= results[0]["source_score"] <= 1.0

    def test_source_score_formula(self):
        assert _source_score(0) == 0.0
        assert _source_score(1) == round(1 / 3, 4)
        assert _source_score(3) == 1.0
        assert _source_score(10) == 1.0

    def test_original_candidate_fields_preserved(self, tmp_path):
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.80)
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(["https://src.com"])):
            results = run_source_verifier([c], _cfg(), db_path=db)
        r = results[0]
        assert r["topic_text"] == c["topic_text"]
        assert r["initial_composite"] == c["initial_composite"]
        assert r["canonical_subject"] == c["canonical_subject"]

    def test_empty_candidates_returns_empty(self, tmp_path):
        db = _init_db(tmp_path)
        with patch("hypogeum.discovery.source_verifier.query_perplexity"):
            results = run_source_verifier([], _cfg(), db_path=db)
        assert results == []

    def test_verification_status_is_one_of_three_values(self, tmp_path):
        db = _init_db(tmp_path)
        candidates = [
            _candidate(topic_text="Verified Topic",  initial_composite=0.80),
            _candidate(topic_text="Skipped Topic",   initial_composite=0.10),
        ]
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(["https://src.com"])):
            results = run_source_verifier(candidates, _cfg(), db_path=db)
        for r in results:
            assert r["verification_status"] in ("verified", "failed", "skipped")


# ---------------------------------------------------------------------------
# 6. V1 regression
# ---------------------------------------------------------------------------

class TestV1Regression:

    def test_v1_score_seed_unaffected(self):
        from hypogeum.topic_discovery import _score_seed, _SIGNAL_BOOST_TOP_PERFORMER
        seed = {
            "topic": "The hidden history of ancient underground cities",
            "source": "heuristic",
            "ctr_rationale": "", "retention_rationale": "",
            "watch_time_rationale": "", "risk": "",
        }
        assert _score_seed(seed, set(), None) == 0.82

    def test_v1_run_discovery_cycle_produces_candidates(self, tmp_path):
        import os
        from hypogeum.db import initialize_schema
        from hypogeum.topic_discovery import run_discovery_cycle
        db = tmp_path / "v1.db"
        initialize_schema(db)
        old = os.environ.get("DISCOVERY_ENGINE")
        try:
            os.environ.pop("DISCOVERY_ENGINE", None)
            result = run_discovery_cycle("ch_v1", db_path=db)
        finally:
            if old is None:
                os.environ.pop("DISCOVERY_ENGINE", None)
            else:
                os.environ["DISCOVERY_ENGINE"] = old
        assert result["ok"] is True
        assert result["candidate_count"] == 7

    def test_source_verifier_import_does_not_touch_v1(self):
        """Importing source_verifier must not modify any v1 module state."""
        from hypogeum.topic_discovery import _score_seed
        from hypogeum.discovery import source_verifier  # noqa: F401
        seed = {
            "topic": "Secret societies and the empires they shaped",
            "source": "heuristic",
            "ctr_rationale": "", "retention_rationale": "",
            "watch_time_rationale": "", "risk": "",
        }
        assert _score_seed(seed, set(), None) == 0.64


# ---------------------------------------------------------------------------
# 7. Query template extraction (Phase 2A follow-up)
# ---------------------------------------------------------------------------

class TestQueryTemplate:

    def test_source_verifier_query_template_loaded_from_file(self):
        from hypogeum.discovery.source_verifier import _VERIFY_QUERY_TEMPLATE
        assert isinstance(_VERIFY_QUERY_TEMPLATE, str)
        assert len(_VERIFY_QUERY_TEMPLATE) > 0
        assert "{topic_text}" in _VERIFY_QUERY_TEMPLATE

    def test_source_verifier_query_template_body_excludes_headers(self):
        from hypogeum.discovery.source_verifier import _VERIFY_QUERY_TEMPLATE
        for line in _VERIFY_QUERY_TEMPLATE.splitlines():
            assert not line.startswith("# PROMPT_VERSION:"), "PROMPT_VERSION header leaked into body"
            assert not line.startswith("# OWNER_AGENT:"), "OWNER_AGENT header leaked into body"
            assert not line.startswith("# MODEL_TARGET:"), "MODEL_TARGET header leaked into body"
            assert not line.startswith("# PURPOSE:"), "PURPOSE header leaked into body"

    def test_prompt_registry_includes_source_verifier_query(self):
        from hypogeum.prompt_registry import list_prompts
        names = [r.name for r in list_prompts()]
        assert "source_verifier_query" in names

    def test_source_verifier_no_network_calls(self, tmp_path):
        db = _init_db(tmp_path)
        c = _candidate(initial_composite=0.80)
        with patch("hypogeum.discovery.source_verifier.query_perplexity",
                   return_value=_mock_plex(["https://src.com/paper"])) as mock_px:
            results = run_source_verifier([c], _cfg(), db_path=db)
        assert mock_px.call_count == 1, "only one Perplexity call expected per qualified candidate"
        assert results[0]["verification_status"] == "verified"
