"""
tests/test_research_director.py — Unit tests for hypogeum/agents/research_director.py

All external calls (Perplexity, Claude) are mocked. No real network calls. No real
filesystem writes.

Test coverage:
  TestSuccessfulResearch       — all 4 queries succeed; Claude synthesises → (package, None)
  TestHealthCheckIntegration   — health check called with correct args; status propagates
  TestPerplexityUnavailable    — health "unavailable" → BLOCKING PDR, no queries
  TestPerplexityDegraded       — health "degraded" → warn, queries proceed
  TestHardFailureDuringQuery   — PerplexityHardFailure → BLOCKING PDR, halts immediately
  TestSoftFailureAccumulation  — 0-2 soft failures continue; ≥3 → PDR MAJOR
  TestPDRGeneration            — PDR fields, schema validity, routing_target
  TestCitationExtraction       — citations passed to Claude synthesis input
  TestSourceRegistry           — built from citations, deduplicated, included in input
  TestClaudeIntegration        — run_agent called with Opus 4.6, correct schema + args
  TestQueryStrategy            — 4 phases, phase_ids, max_tokens_override, sequential
  TestPipelineIntegration      — pipeline stub delegates; PDR → PackageDeficiencyError
  TestExceptionPropagation     — run_agent exceptions propagate unchanged
  TestLogging                  — key log events emitted
  TestSystemPrompt             — prompt file exists, non-empty, loaded correctly
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agent import ResponseParseError
from hypogeum.agents.research_director import (
    SYSTEM_PROMPT,
    _AGENT_NAME,
    _CLAUDE_OPUS,
    _PDR_SOFT_FAILURE_THRESHOLD,
    _PERPLEXITY_MAX_TOKENS,
    _PROMPT_FILE,
    _RD_MAX_TOKENS,
    _RESEARCH_PHASES,
    _build_pdr,
    _build_source_registry,
    run_research_director,
)
from hypogeum.config import Config
from hypogeum.perplexity import (
    PerplexityAuthFailure,
    PerplexityHardFailure,
    PerplexityResult,
    PerplexitySoftFailureExhausted,
)
from hypogeum.pipeline import (
    PackageDeficiencyError,
    _run_research_director as pipeline_rd_stub,
)
from hypogeum.schemas import SchemaError, validate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PIPELINE_ENV_VARS = [
    "OPENROUTER_API_KEY", "OPENROUTER_SONNET_MODEL_ID", "OPENROUTER_OPUS_MODEL_ID",
    "OPENROUTER_APP_NAME", "OPENROUTER_APP_URL", "OPENROUTER_FALLBACK_MODEL",
    "ANTHROPIC_API_KEY",
    "PERPLEXITY_API_KEY", "PERPLEXITY_MODEL", "PERPLEXITY_MAX_TOKENS",
    "MUAPI_API_KEY", "MUAPI_DEFAULT_TIER", "MUAPI_TIMEOUT_SECONDS", "MUAPI_WEBHOOK_URL",
    "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID",
    "ELEVENLABS_STABILITY", "ELEVENLABS_SIMILARITY_BOOST",
    "ELEVENLABS_MUSIC_MODEL_ID", "ELEVENLABS_MUSIC_DEFAULT_DURATION",
    "OPENAI_API_KEY", "WHISPER_MODEL",
    "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
    "YOUTUBE_CHANNEL_ID", "YOUTUBE_DEFAULT_CATEGORY_ID",
    "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_OPEN_ID",
    "META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN",
    "META_INSTAGRAM_ACCOUNT_ID", "META_FACEBOOK_PAGE_ID",
    "META_GRAPH_API_VERSION", "META_STAGING_BASE_URL",
]


def make_config() -> Config:
    original = {k: os.environ.get(k) for k in _PIPELINE_ENV_VARS}
    for k in _PIPELINE_ENV_VARS:
        os.environ.pop(k, None)
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    try:
        return Config()
    finally:
        for k in _PIPELINE_ENV_VARS:
            os.environ.pop(k, None)
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v


def _make_px_result(
    content: str = "The Antikythera Mechanism is an ancient Greek device.",
    citations: list = None,
    phase_id: str = "core_facts",
    response_time_ms: int = 500,
) -> PerplexityResult:
    """Build a valid PerplexityResult for testing."""
    return PerplexityResult(
        content=content,
        citations=citations if citations is not None else [
            "https://example.com/source1",
            "https://example.com/source2",
        ],
        usage={"prompt_tokens": 100, "completion_tokens": 80, "total_tokens": 180},
        finish_reason="stop",
        phase_id=phase_id,
        query_index=0,
        response_time_ms=response_time_ms,
        citations_absent=(citations == []),
    )


def _make_research_package() -> dict:
    """Return a minimal valid research_package dict."""
    return {
        "research_run_metadata": {
            "episode_id": "ep_001",
            "topic": "The Antikythera Mechanism",
            "phases_completed": 4,
            "phases_failed": 0,
            "total_sources": 8,
            "research_model": "research_director_v1",
            "synthesised_by": "research_director",
        },
        "research_strategy": {
            "phases": [
                {"phase_id": "core_facts", "description": "Core facts", "status": "completed", "sources_found": 2},
            ],
            "differentiation_hypothesis_tested": "yes",
            "coverage_assessment": "Good coverage of primary sources.",
        },
        "claims": [
            {
                "claim_id": "clm_001",
                "text": "The Antikythera Mechanism dates to around 150-100 BCE.",
                "confidence": 0.95,
                "sources": ["src_001"],
                "narrative_potential": "Establishes historical grounding.",
                "narrative_signal_type": "shock_fact",
            }
        ],
        "contradictions": [],
        "open_questions": [],
        "source_registry": [
            {
                "source_id": "src_001",
                "url": "https://example.com/source1",
                "inferred_type": "academic",
                "phase_id": "core_facts",
                "claim_ids": ["clm_001"],
            }
        ],
        "competitive_landscape": {
            "coverage_density": "medium",
            "dominant_angle": "mechanical complexity",
            "differentiation_gap": "astronomical predictions angle",
            "hypothesis_supported": "yes",
            "notes": "Several documentary-style videos exist.",
        },
        "research_run_summary": {
            "overall_quality": "high",
            "claim_count": 1,
            "high_confidence_claim_count": 1,
            "contradiction_count": 0,
            "open_question_count": 0,
            "narrative_signal_distribution": {
                "shock_fact": 1, "turning_point": 0, "human_stakes": 0,
                "irony": 0, "mystery_deepening": 0, "reveal": 0,
                "visual_anchor": 0, "foreshadowing": 0, "false_assumption": 0,
            },
            "quality_warnings": [],
        },
        "package_integrity": {
            "production_ready": True,
            "integrity_notes": "",
        },
    }


def _make_topic_brief() -> dict:
    return {
        "topic_id": "antikythera_mechanism",
        "episode_title": "The Antikythera Mechanism: Ancient Computer",
        "topic_category": "ancient_mystery",
        "logline": "A 2,000-year-old device that shouldn't exist.",
        "audience_demand_signal": "High search volume.",
        "curiosity_hook": "Who built it and how was the knowledge lost?",
        "mystery_anchor": "No known predecessor exists.",
        "differentiation_hypothesis": "No channel explored the astronomical predictions angle.",
        "research_depth_flags": {"differentiation_opportunity": "deep"},
        "shorts_directions": ["The missing manual theory", "The Roman shipwreck discovery"],
        "issued_at": "2026-06-01T10:00:00Z",
        "issued_by": "executive_producer",
    }


EPISODE_ID = "ep_001"
BRIEF = _make_topic_brief()

# Soft failure error reused across tests
_SOFT_ERR = PerplexitySoftFailureExhausted("Retry exhausted after 1 attempt", last_status=500)
# Hard failure error reused across tests
_HARD_ERR = PerplexityHardFailure("Perplexity returned HTTP 401", http_status=401)


def _success_side_effect():
    """Return 4 successful PerplexityResults for the 4 phases."""
    return [
        _make_px_result(phase_id=p["phase_id"]) for p in _RESEARCH_PHASES
    ]


def _run_rd(
    brief=None,
    health="available",
    px_side_effect=None,
    run_agent_return=None,
    output_dir=None,
):
    """
    Helper: run run_research_director with standard mocks.
    px_side_effect: list of returns/raises for query_perplexity, one per call.
    run_agent_return: dict returned by run_agent (defaults to _make_research_package()).
    """
    if brief is None:
        brief = _make_topic_brief()
    if px_side_effect is None:
        px_side_effect = _success_side_effect()
    if run_agent_return is None:
        run_agent_return = _make_research_package()
    cfg = make_config()

    with patch("hypogeum.agents.research_director.check_perplexity_health",
               return_value=health), \
         patch("hypogeum.agents.research_director.query_perplexity",
               side_effect=px_side_effect), \
         patch("hypogeum.agents.research_director.run_agent",
               return_value=run_agent_return):
        return run_research_director(brief, cfg, EPISODE_ID, output_dir)


# ---------------------------------------------------------------------------
# TestSuccessfulResearch
# ---------------------------------------------------------------------------

class TestSuccessfulResearch:
    """All 4 queries succeed; Claude synthesises → (research_package, None)."""

    def test_returns_tuple(self):
        result = _run_rd()
        assert isinstance(result, tuple) and len(result) == 2

    def test_returns_research_package(self):
        pkg = _make_research_package()
        result_pkg, _ = _run_rd(run_agent_return=pkg)
        assert result_pkg is pkg

    def test_pdr_is_none_on_success(self):
        _, pdr = _run_rd()
        assert pdr is None

    def test_four_perplexity_calls_made(self):
        cfg = make_config()
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()) as mock_px, \
             patch("hypogeum.agents.research_director.run_agent",
                   return_value=_make_research_package()):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        assert mock_px.call_count == 4

    def test_run_agent_called_once(self):
        cfg = make_config()
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()), \
             patch("hypogeum.agents.research_director.run_agent",
                   return_value=_make_research_package()) as mock_ra:
            run_research_director(BRIEF, cfg, EPISODE_ID)
        assert mock_ra.call_count == 1

    def test_run_agent_not_called_when_unavailable(self):
        cfg = make_config()
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="unavailable"), \
             patch("hypogeum.agents.research_director.run_agent") as mock_ra:
            run_research_director(BRIEF, cfg, EPISODE_ID)
        mock_ra.assert_not_called()

    def test_topic_brief_in_run_agent_input(self):
        cfg = make_config()
        captured = []

        def capture_input(**kwargs):
            captured.append(kwargs.get("input_package", {}))
            return _make_research_package()

        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()), \
             patch("hypogeum.agents.research_director.run_agent",
                   side_effect=capture_input):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        assert captured[0].get("topic_brief") == BRIEF

    def test_perplexity_results_in_run_agent_input(self):
        cfg = make_config()
        captured = []

        def capture_input(**kwargs):
            captured.append(kwargs.get("input_package", {}))
            return _make_research_package()

        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()), \
             patch("hypogeum.agents.research_director.run_agent",
                   side_effect=capture_input):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        assert "perplexity_results" in captured[0]
        assert len(captured[0]["perplexity_results"]) == 4


# ---------------------------------------------------------------------------
# TestHealthCheckIntegration
# ---------------------------------------------------------------------------

class TestHealthCheckIntegration:
    """Health check called with correct args; status propagates correctly."""

    def test_health_check_called_once(self):
        cfg = make_config()
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available") as mock_hc, \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()), \
             patch("hypogeum.agents.research_director.run_agent",
                   return_value=_make_research_package()):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        mock_hc.assert_called_once()

    def test_health_check_receives_config(self):
        cfg = make_config()
        captured = []
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   side_effect=lambda c, **kw: captured.append(c) or "available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()), \
             patch("hypogeum.agents.research_director.run_agent",
                   return_value=_make_research_package()):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        assert captured[0] is cfg

    def test_health_check_receives_episode_id(self):
        cfg = make_config()
        captured = []
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   side_effect=lambda c, **kw: captured.append(kw) or "available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()), \
             patch("hypogeum.agents.research_director.run_agent",
                   return_value=_make_research_package()):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        assert captured[0].get("episode_id") == EPISODE_ID

    def test_unavailable_halts_before_queries(self):
        cfg = make_config()
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="unavailable"), \
             patch("hypogeum.agents.research_director.query_perplexity") as mock_px:
            run_research_director(BRIEF, cfg, EPISODE_ID)
        mock_px.assert_not_called()

    def test_available_allows_queries(self):
        cfg = make_config()
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()) as mock_px, \
             patch("hypogeum.agents.research_director.run_agent",
                   return_value=_make_research_package()):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        assert mock_px.call_count == 4


# ---------------------------------------------------------------------------
# TestPerplexityUnavailable
# ---------------------------------------------------------------------------

class TestPerplexityUnavailable:
    """health "unavailable" → BLOCKING PDR, no queries run."""

    def test_returns_empty_package_and_pdr(self):
        pkg, pdr = _run_rd(health="unavailable")
        assert isinstance(pkg, dict)
        assert pdr is not None

    def test_empty_package_on_unavailable(self):
        pkg, _ = _run_rd(health="unavailable")
        assert pkg == {}

    def test_pdr_severity_is_blocking(self):
        _, pdr = _run_rd(health="unavailable")
        assert pdr["severity"] == "BLOCKING"

    def test_pdr_issuing_agent_is_research_director(self):
        _, pdr = _run_rd(health="unavailable")
        assert pdr["issuing_agent"] == "research_director"

    def test_pdr_routing_target_is_executive_producer(self):
        _, pdr = _run_rd(health="unavailable")
        assert pdr["routing_target"] == "executive_producer"

    def test_no_query_perplexity_calls(self):
        cfg = make_config()
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="unavailable"), \
             patch("hypogeum.agents.research_director.query_perplexity") as mock_px:
            run_research_director(BRIEF, cfg, EPISODE_ID)
        mock_px.assert_not_called()

    def test_pdr_target_package_is_research_package(self):
        _, pdr = _run_rd(health="unavailable")
        assert pdr["target_package"] == "research_package"


# ---------------------------------------------------------------------------
# TestPerplexityDegraded
# ---------------------------------------------------------------------------

class TestPerplexityDegraded:
    """health "degraded" → warn logged, queries still proceed."""

    def test_queries_proceed_on_degraded(self):
        cfg = make_config()
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="degraded"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()) as mock_px, \
             patch("hypogeum.agents.research_director.run_agent",
                   return_value=_make_research_package()):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        assert mock_px.call_count == 4

    def test_synthesis_proceeds_on_degraded(self):
        cfg = make_config()
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="degraded"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()), \
             patch("hypogeum.agents.research_director.run_agent",
                   return_value=_make_research_package()) as mock_ra:
            run_research_director(BRIEF, cfg, EPISODE_ID)
        assert mock_ra.call_count == 1

    def test_pdr_is_none_on_degraded_success(self):
        _, pdr = _run_rd(health="degraded")
        assert pdr is None

    def test_returns_research_package_on_degraded(self):
        pkg = _make_research_package()
        result_pkg, _ = _run_rd(health="degraded", run_agent_return=pkg)
        assert result_pkg is pkg


# ---------------------------------------------------------------------------
# TestHardFailureDuringQuery
# ---------------------------------------------------------------------------

class TestHardFailureDuringQuery:
    """PerplexityHardFailure during a query → BLOCKING PDR, no further queries."""

    def test_hard_failure_returns_pdr(self):
        # First query raises hard failure
        _, pdr = _run_rd(px_side_effect=[_HARD_ERR])
        assert pdr is not None

    def test_hard_failure_pdr_severity_blocking(self):
        _, pdr = _run_rd(px_side_effect=[_HARD_ERR])
        assert pdr["severity"] == "BLOCKING"

    def test_hard_failure_no_further_queries(self):
        cfg = make_config()
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=[_HARD_ERR]) as mock_px, \
             patch("hypogeum.agents.research_director.run_agent"):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        assert mock_px.call_count == 1

    def test_hard_failure_run_agent_not_called(self):
        cfg = make_config()
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=[_HARD_ERR]), \
             patch("hypogeum.agents.research_director.run_agent") as mock_ra:
            run_research_director(BRIEF, cfg, EPISODE_ID)
        mock_ra.assert_not_called()

    def test_auth_failure_also_triggers_blocking_pdr(self):
        """PerplexityAuthFailure is a PerplexityHardFailure subclass."""
        auth_err = PerplexityAuthFailure()
        _, pdr = _run_rd(px_side_effect=[auth_err])
        assert pdr["severity"] == "BLOCKING"

    def test_hard_failure_on_second_query_halts(self):
        """Hard failure on query 2 → only 2 query_perplexity calls."""
        cfg = make_config()
        success = _make_px_result(phase_id="core_facts")
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=[success, _HARD_ERR]) as mock_px, \
             patch("hypogeum.agents.research_director.run_agent"):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        assert mock_px.call_count == 2

    def test_hard_failure_pdr_routing_target(self):
        _, pdr = _run_rd(px_side_effect=[_HARD_ERR])
        assert pdr["routing_target"] == "executive_producer"


# ---------------------------------------------------------------------------
# TestSoftFailureAccumulation
# ---------------------------------------------------------------------------

class TestSoftFailureAccumulation:
    """Soft-failure accumulation logic: <3 → continue; ≥3 → PDR MAJOR."""

    def test_one_soft_failure_continues(self):
        """1 failure → pdr is None, synthesis proceeds."""
        side = [_SOFT_ERR] + [_make_px_result(phase_id=p["phase_id"])
                               for p in _RESEARCH_PHASES[1:]]
        _, pdr = _run_rd(px_side_effect=side)
        assert pdr is None

    def test_two_soft_failures_continues(self):
        """2 failures → pdr is None."""
        side = [_SOFT_ERR, _SOFT_ERR] + [_make_px_result(phase_id=p["phase_id"])
                                          for p in _RESEARCH_PHASES[2:]]
        _, pdr = _run_rd(px_side_effect=side)
        assert pdr is None

    def test_three_soft_failures_issues_pdr(self):
        """3 failures → PDR MAJOR."""
        side = [_SOFT_ERR, _SOFT_ERR, _SOFT_ERR,
                _make_px_result(phase_id=_RESEARCH_PHASES[3]["phase_id"])]
        _, pdr = _run_rd(px_side_effect=side)
        assert pdr is not None
        assert pdr["severity"] == "MAJOR"

    def test_four_soft_failures_issues_pdr(self):
        """4 failures (all) → PDR MAJOR (not BLOCKING — no hard failure)."""
        side = [_SOFT_ERR] * 4
        _, pdr = _run_rd(px_side_effect=side)
        assert pdr is not None

    def test_three_failures_run_agent_not_called(self):
        """PDR threshold reached → synthesis skipped."""
        side = [_SOFT_ERR, _SOFT_ERR, _SOFT_ERR,
                _make_px_result(phase_id=_RESEARCH_PHASES[3]["phase_id"])]
        cfg = make_config()
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=side), \
             patch("hypogeum.agents.research_director.run_agent") as mock_ra:
            run_research_director(BRIEF, cfg, EPISODE_ID)
        mock_ra.assert_not_called()

    def test_three_failures_pdr_has_deficiency_items(self):
        side = [_SOFT_ERR, _SOFT_ERR, _SOFT_ERR,
                _make_px_result(phase_id=_RESEARCH_PHASES[3]["phase_id"])]
        _, pdr = _run_rd(px_side_effect=side)
        assert len(pdr["deficiency_items"]) == 3

    def test_failure_threshold_constant(self):
        assert _PDR_SOFT_FAILURE_THRESHOLD == 3


# ---------------------------------------------------------------------------
# TestPDRGeneration
# ---------------------------------------------------------------------------

class TestPDRGeneration:
    """PDR structure, field values, and schema validity."""

    def test_pdr_has_report_id(self):
        _, pdr = _run_rd(health="unavailable")
        assert isinstance(pdr["report_id"], str) and pdr["report_id"]

    def test_pdr_has_issuing_agent(self):
        _, pdr = _run_rd(health="unavailable")
        assert pdr["issuing_agent"] == "research_director"

    def test_pdr_has_target_package(self):
        _, pdr = _run_rd(health="unavailable")
        assert pdr["target_package"] == "research_package"

    def test_pdr_has_issued_at(self):
        _, pdr = _run_rd(health="unavailable")
        assert isinstance(pdr["issued_at"], str) and "T" in pdr["issued_at"]

    def test_pdr_has_deficiency_items_list(self):
        _, pdr = _run_rd(health="unavailable")
        assert isinstance(pdr["deficiency_items"], list)
        assert len(pdr["deficiency_items"]) >= 1

    def test_pdr_deficiency_items_have_item_id(self):
        _, pdr = _run_rd(health="unavailable")
        for item in pdr["deficiency_items"]:
            assert "item_id" in item and isinstance(item["item_id"], str)

    def test_pdr_deficiency_items_have_description(self):
        _, pdr = _run_rd(health="unavailable")
        for item in pdr["deficiency_items"]:
            assert "description" in item and isinstance(item["description"], str)

    def test_pdr_validates_against_schema(self):
        _, pdr = _run_rd(health="unavailable")
        validate("package_deficiency_report", pdr)  # must not raise

    def test_build_pdr_helper_validates_internally(self):
        """_build_pdr raises SchemaError if items list is empty."""
        with pytest.raises(SchemaError):
            _build_pdr("ep_001", "MAJOR", items=[])  # deficiency_items min_length=1

    def test_pdr_routing_target(self):
        _, pdr = _run_rd(health="unavailable")
        assert pdr["routing_target"] == "executive_producer"


# ---------------------------------------------------------------------------
# TestCitationExtraction
# ---------------------------------------------------------------------------

class TestCitationExtraction:
    """Citations from PerplexityResult are extracted and passed to Claude."""

    def test_citations_in_perplexity_results_sent_to_claude(self):
        cfg = make_config()
        citations = ["https://academic.example.com/paper1", "https://news.example.com/2"]
        results = [_make_px_result(citations=citations, phase_id=p["phase_id"])
                   for p in _RESEARCH_PHASES]
        captured = []

        def capture_input(**kwargs):
            captured.append(kwargs.get("input_package", {}))
            return _make_research_package()

        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=results), \
             patch("hypogeum.agents.research_director.run_agent",
                   side_effect=capture_input):
            run_research_director(BRIEF, cfg, EPISODE_ID)

        all_citations = [
            c for r in captured[0]["perplexity_results"]
            for c in r.get("citations", [])
        ]
        assert "https://academic.example.com/paper1" in all_citations

    def test_empty_citations_handled_gracefully(self):
        """Empty citation list doesn't crash the run."""
        results = [_make_px_result(citations=[], phase_id=p["phase_id"])
                   for p in _RESEARCH_PHASES]
        pkg, pdr = _run_rd(px_side_effect=results)
        assert pdr is None

    def test_citations_absent_flag_passed_to_claude(self):
        cfg = make_config()
        result_with_no_cites = _make_px_result(citations=[], phase_id="core_facts")
        results = [result_with_no_cites] + [
            _make_px_result(phase_id=p["phase_id"]) for p in _RESEARCH_PHASES[1:]
        ]
        captured = []

        def capture_input(**kwargs):
            captured.append(kwargs.get("input_package", {}))
            return _make_research_package()

        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=results), \
             patch("hypogeum.agents.research_director.run_agent",
                   side_effect=capture_input):
            run_research_director(BRIEF, cfg, EPISODE_ID)

        first_result = captured[0]["perplexity_results"][0]
        assert first_result["citations_absent"] is True

    def test_citations_from_all_phases_included(self):
        cfg = make_config()
        results = [
            _make_px_result(citations=[f"https://example.com/phase{i}"],
                            phase_id=p["phase_id"])
            for i, p in enumerate(_RESEARCH_PHASES)
        ]
        captured = []

        def capture_input(**kwargs):
            captured.append(kwargs.get("input_package", {}))
            return _make_research_package()

        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=results), \
             patch("hypogeum.agents.research_director.run_agent",
                   side_effect=capture_input):
            run_research_director(BRIEF, cfg, EPISODE_ID)

        phase_ids_in_results = {r["phase_id"] for r in captured[0]["perplexity_results"]}
        assert phase_ids_in_results == {p["phase_id"] for p in _RESEARCH_PHASES}

    def test_content_from_perplexity_included_in_input(self):
        cfg = make_config()
        unique_content = "Unique Antikythera content from Perplexity XYZ123"
        results = [_make_px_result(content=unique_content, phase_id=p["phase_id"])
                   for p in _RESEARCH_PHASES]
        captured = []

        def capture_input(**kwargs):
            captured.append(kwargs.get("input_package", {}))
            return _make_research_package()

        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=results), \
             patch("hypogeum.agents.research_director.run_agent",
                   side_effect=capture_input):
            run_research_director(BRIEF, cfg, EPISODE_ID)

        assert any(r["content"] == unique_content
                   for r in captured[0]["perplexity_results"])


# ---------------------------------------------------------------------------
# TestSourceRegistry
# ---------------------------------------------------------------------------

class TestSourceRegistry:
    """Source registry built from citations, deduplicated, passed to Claude."""

    def test_source_registry_in_input(self):
        cfg = make_config()
        captured = []

        def capture_input(**kwargs):
            captured.append(kwargs.get("input_package", {}))
            return _make_research_package()

        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()), \
             patch("hypogeum.agents.research_director.run_agent",
                   side_effect=capture_input):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        assert "source_registry" in captured[0]

    def test_source_registry_entries_have_source_id(self):
        registry = _build_source_registry([
            {"phase_id": "core_facts",
             "citations": ["https://a.com", "https://b.com"]}
        ])
        for entry in registry:
            assert "source_id" in entry and entry["source_id"].startswith("src_")

    def test_source_registry_deduplicates_urls(self):
        registry = _build_source_registry([
            {"phase_id": "core_facts", "citations": ["https://dup.com"]},
            {"phase_id": "mystery_depth", "citations": ["https://dup.com"]},
        ])
        urls = [e["url"] for e in registry]
        assert urls.count("https://dup.com") == 1

    def test_source_registry_entries_have_phase_id(self):
        registry = _build_source_registry([
            {"phase_id": "academic_context", "citations": ["https://x.com"]},
        ])
        assert registry[0]["phase_id"] == "academic_context"

    def test_source_registry_empty_on_no_citations(self):
        registry = _build_source_registry([
            {"phase_id": "core_facts", "citations": []},
        ])
        assert registry == []

    def test_source_registry_preserves_order_of_first_appearance(self):
        registry = _build_source_registry([
            {"phase_id": "core_facts", "citations": ["https://first.com"]},
            {"phase_id": "mystery_depth", "citations": ["https://second.com", "https://first.com"]},
        ])
        assert registry[0]["url"] == "https://first.com"
        assert registry[1]["url"] == "https://second.com"


# ---------------------------------------------------------------------------
# TestClaudeIntegration
# ---------------------------------------------------------------------------

class TestClaudeIntegration:
    """run_agent called with Opus 4.6, correct schema, all required args."""

    def _capture_run_agent(self):
        cfg = make_config()
        captured = []

        def capture(**kwargs):
            captured.append(kwargs)
            return _make_research_package()

        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()), \
             patch("hypogeum.agents.research_director.run_agent",
                   side_effect=capture):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        return captured[0], cfg

    def test_model_slug_from_config(self):
        kwargs, cfg = self._capture_run_agent()
        assert kwargs["model_slug"] == cfg.RD_MODEL

    def test_model_slug_default_is_opus(self):
        kwargs, _ = self._capture_run_agent()
        assert kwargs["model_slug"] == "anthropic/claude-opus-4.6"

    def test_output_schema_is_research_package(self):
        kwargs, _ = self._capture_run_agent()
        assert kwargs["output_schema"] == "research_package"

    def test_max_tokens_is_16384(self):
        kwargs, _ = self._capture_run_agent()
        assert kwargs["max_tokens"] == 16384

    def test_agent_name_is_research_director(self):
        kwargs, _ = self._capture_run_agent()
        assert kwargs["agent_name"] == "research_director"

    def test_input_package_has_task_field(self):
        kwargs, _ = self._capture_run_agent()
        assert kwargs["input_package"]["task"] == "synthesize_research"

    def test_episode_id_passed_to_run_agent(self):
        kwargs, _ = self._capture_run_agent()
        assert kwargs["episode_id"] == EPISODE_ID

    def test_system_prompt_non_empty(self):
        kwargs, _ = self._capture_run_agent()
        assert isinstance(kwargs["system_prompt"], str) and len(kwargs["system_prompt"]) > 0


# ---------------------------------------------------------------------------
# TestQueryStrategy
# ---------------------------------------------------------------------------

class TestQueryStrategy:
    """4 phases executed sequentially with correct arguments."""

    def _capture_px_calls(self):
        cfg = make_config()
        calls_made = []

        def capture(query, config, **kwargs):
            calls_made.append({"query": query, **kwargs})
            return _make_px_result(phase_id=kwargs.get("phase_id", ""))

        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=capture), \
             patch("hypogeum.agents.research_director.run_agent",
                   return_value=_make_research_package()):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        return calls_made

    def test_exactly_four_phases_queried(self):
        calls = self._capture_px_calls()
        assert len(calls) == 4

    def test_phase_ids_are_correct(self):
        calls = self._capture_px_calls()
        phase_ids = [c["phase_id"] for c in calls]
        expected = [p["phase_id"] for p in _RESEARCH_PHASES]
        assert phase_ids == expected

    def test_query_includes_topic(self):
        calls = self._capture_px_calls()
        topic = BRIEF.get("episode_title")
        for c in calls:
            assert topic in c["query"]

    def test_max_tokens_override_set(self):
        calls = self._capture_px_calls()
        for c in calls:
            assert c["max_tokens_override"] == _PERPLEXITY_MAX_TOKENS

    def test_episode_id_passed_to_queries(self):
        calls = self._capture_px_calls()
        for c in calls:
            assert c["episode_id"] == EPISODE_ID

    def test_query_index_sequential(self):
        calls = self._capture_px_calls()
        indices = [c["query_index"] for c in calls]
        assert indices == list(range(4))

    def test_perplexity_max_tokens_constant(self):
        assert _PERPLEXITY_MAX_TOKENS == 2048


# ---------------------------------------------------------------------------
# TestPipelineIntegration
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    """Pipeline stub delegates; PDR → PackageDeficiencyError."""

    def _make_startup_report(self):
        from hypogeum.startup import StartupReport
        return StartupReport(
            overall_status="healthy",
            openrouter_reachable=True,
            models_verified=True,
            fallback_active=False,
            fallback_configured=False,
            checked_at="2026-06-01T10:00:00Z",
        )

    def _make_minimal_ep_output(self):
        scorecard = {
            "scorecard_id": "sc_001", "topic_id": "t_001",
            "dimensions": {k: 8.0 for k in (
                "audience_demand", "curiosity_potential", "mystery_potential",
                "visual_potential", "differentiation_opportunity", "shorts_potential",
            )},
            "overall_score": 8.0, "decision": "PASS",
            "issued_at": "2026-06-01T00:00:00Z", "issued_by": "executive_producer",
        }
        brief = {
            "topic_id": "t_001", "episode_title": "Test Episode",
            "topic_category": "ancient_mystery", "logline": "A mystery.",
            "audience_demand_signal": "High demand.", "curiosity_hook": "Why?",
            "mystery_anchor": "Unknown origin.", "differentiation_hypothesis": "New angle.",
            "research_depth_flags": {"differentiation_opportunity": "deep"},
            "shorts_directions": ["Direction 1", "Direction 2"],
            "issued_at": "2026-06-01T00:00:00Z", "issued_by": "executive_producer",
        }
        return scorecard, brief

    def test_pipeline_stub_delegates_to_run_research_director(self, tmp_path):
        cfg = make_config()
        rp = _make_research_package()
        with patch("hypogeum.pipeline.run_research_director",
                   return_value=(rp, None)) as mock_rd:
            result = pipeline_rd_stub(
                topic_brief=BRIEF, config=cfg,
                episode_id=EPISODE_ID, output_dir=tmp_path,
            )
        mock_rd.assert_called_once()
        assert result == (rp, None)

    def test_pipeline_stub_passes_topic_brief(self, tmp_path):
        cfg = make_config()
        with patch("hypogeum.pipeline.run_research_director",
                   return_value=(_make_research_package(), None)) as mock_rd:
            pipeline_rd_stub(
                topic_brief=BRIEF, config=cfg,
                episode_id=EPISODE_ID, output_dir=tmp_path,
            )
        assert mock_rd.call_args.kwargs["topic_brief"] is BRIEF

    def test_pipeline_stub_passes_episode_id(self, tmp_path):
        cfg = make_config()
        with patch("hypogeum.pipeline.run_research_director",
                   return_value=(_make_research_package(), None)) as mock_rd:
            pipeline_rd_stub(
                topic_brief=BRIEF, config=cfg,
                episode_id=EPISODE_ID, output_dir=tmp_path,
            )
        assert mock_rd.call_args.kwargs["episode_id"] == EPISODE_ID

    def test_pdr_returned_raises_package_deficiency_error(self, tmp_path):
        from hypogeum.pipeline import run_pipeline
        cfg = make_config()
        pdr = _build_pdr("ep_001", "MAJOR",
                         [{"item_id": "i_001", "description": "Test deficiency"}])
        sc, brief = self._make_minimal_ep_output()

        with patch("hypogeum.pipeline.run_executive_producer",
                   return_value=(sc, brief)), \
             patch("hypogeum.pipeline.run_research_director",
                   return_value=({}, pdr)), \
             patch("hypogeum.pipeline.run_startup_checks",
                   return_value=self._make_startup_report()):
            with pytest.raises(PackageDeficiencyError) as exc_info:
                run_pipeline("Test", EPISODE_ID, cfg, output_dir=tmp_path)
        assert exc_info.value.episode_id == EPISODE_ID

    def test_pdr_attached_to_exception(self, tmp_path):
        from hypogeum.pipeline import run_pipeline
        cfg = make_config()
        pdr = _build_pdr("ep_001", "MAJOR",
                         [{"item_id": "i_001", "description": "Test deficiency"}])
        sc, brief = self._make_minimal_ep_output()

        with patch("hypogeum.pipeline.run_executive_producer",
                   return_value=(sc, brief)), \
             patch("hypogeum.pipeline.run_research_director",
                   return_value=({}, pdr)), \
             patch("hypogeum.pipeline.run_startup_checks",
                   return_value=self._make_startup_report()):
            with pytest.raises(PackageDeficiencyError) as exc_info:
                run_pipeline("Test", EPISODE_ID, cfg, output_dir=tmp_path)
        assert exc_info.value.pdr is pdr

    def test_success_continues_to_next_agent(self):
        """Successful RD → pipeline proceeds past NA, SE through all remaining agents to completion."""
        from hypogeum.pipeline import run_pipeline
        cfg = make_config()
        sc, brief = self._make_minimal_ep_output()

        with patch("hypogeum.pipeline.run_executive_producer",
                   return_value=(sc, brief)), \
             patch("hypogeum.pipeline.validate_narration_completeness"), \
             patch("hypogeum.pipeline.run_research_director",
                   return_value=(_make_research_package(), None)), \
             patch("hypogeum.pipeline.run_narrative_architect",
                   return_value={}), \
             patch("hypogeum.pipeline.run_script_engine",
                   return_value={}), \
             patch("hypogeum.pipeline.run_budget_authorization",
                   return_value={"verdict": "AUTHORIZED", "authorized": True,
                                 "checks": [], "failures": [], "warnings": []}), \
             patch("hypogeum.pipeline.run_thumbnail_strategy",
                   return_value={}), \
             patch("hypogeum.pipeline.run_visual_package_director",
                   return_value={}), \
             patch("hypogeum.pipeline.build_thumbnail_handoff_brief",
                   return_value={}), \
             patch("hypogeum.pipeline.run_thumbnail_director",
                   return_value={}), \
             patch("hypogeum.pipeline.run_audio_agent",
                   return_value={}), \
             patch("hypogeum.pipeline.run_assembly_agent",
                   return_value={}), \
             patch("hypogeum.pipeline.run_qa_agent",
                   return_value={"verdict": "APPROVED", "approved": True,
                                 "checks": [], "failures": [], "warnings": []}), \
             patch("hypogeum.pipeline.run_compliance_agent",
                   return_value={"verdict": "APPROVED", "compliant": True,
                                 "checks": [], "failures": [], "warnings": []}), \
             patch("hypogeum.pipeline.run_publishing_agent",
                   return_value={}), \
             patch("hypogeum.pipeline.run_cost_ledger",
                   return_value={"verdict": "RECORDED", "authorized": True,
                                 "checks": [], "failures": [], "warnings": []}), \
             patch("hypogeum.pipeline.run_startup_checks",
                   return_value=self._make_startup_report()):
            result = run_pipeline("Test", EPISODE_ID, cfg, channel_id=None)
        assert result.status == "completed"

    def test_stub_passes_output_dir(self):
        cfg = make_config()
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            with patch("hypogeum.pipeline.run_research_director",
                       return_value=(_make_research_package(), None)) as mock_rd:
                pipeline_rd_stub(
                    topic_brief=BRIEF, config=cfg,
                    episode_id=EPISODE_ID, output_dir=p,
                )
            assert mock_rd.call_args.kwargs["output_dir"] == p


# ---------------------------------------------------------------------------
# TestExceptionPropagation
# ---------------------------------------------------------------------------

class TestExceptionPropagation:
    """Exceptions from run_agent propagate unchanged."""

    def test_response_parse_error_propagates(self):
        cfg = make_config()
        err = ResponseParseError("research_director", "invalid_json")
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()), \
             patch("hypogeum.agents.research_director.run_agent", side_effect=err):
            with pytest.raises(ResponseParseError) as exc_info:
                run_research_director(BRIEF, cfg, EPISODE_ID)
        assert exc_info.value is err

    def test_schema_error_propagates(self):
        cfg = make_config()
        err = SchemaError("research_package", "claims", "missing required field")
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()), \
             patch("hypogeum.agents.research_director.run_agent", side_effect=err):
            with pytest.raises(SchemaError) as exc_info:
                run_research_director(BRIEF, cfg, EPISODE_ID)
        assert exc_info.value is err

    def test_unexpected_exception_from_run_agent_propagates(self):
        cfg = make_config()
        err = RuntimeError("OpenRouter unreachable")
        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()), \
             patch("hypogeum.agents.research_director.run_agent", side_effect=err):
            with pytest.raises(RuntimeError) as exc_info:
                run_research_director(BRIEF, cfg, EPISODE_ID)
        assert exc_info.value is err


# ---------------------------------------------------------------------------
# TestLogging
# ---------------------------------------------------------------------------

class TestLogging:
    """Key log events emitted at correct levels."""

    def _collect_events(self, health="available", px_side=None):
        """Run RD and collect (level, event) pairs from the structured logger."""
        if px_side is None:
            px_side = _success_side_effect()
        cfg = make_config()
        events = []

        class _CapLog:
            def info(self, event, **kw):   events.append(("info", event))
            def warn(self, event, **kw):   events.append(("warn", event))
            def error(self, event, **kw):  events.append(("error", event))
            def debug(self, event, **kw):  events.append(("debug", event))

        cap = _CapLog()
        with patch("hypogeum.agents.research_director.get_logger", return_value=cap), \
             patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value=health), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=px_side), \
             patch("hypogeum.agents.research_director.run_agent",
                   return_value=_make_research_package()):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        return events

    def test_health_check_started_logged(self):
        events = self._collect_events()
        assert ("info", "rd_health_check_started") in events

    def test_perplexity_unavailable_logged_as_error(self):
        cfg = make_config()
        events = []

        class _CapLog:
            def info(self, e, **k): events.append(("info", e))
            def warn(self, e, **k): events.append(("warn", e))
            def error(self, e, **k): events.append(("error", e))
            def debug(self, e, **k): events.append(("debug", e))

        with patch("hypogeum.agents.research_director.get_logger", return_value=_CapLog()), \
             patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="unavailable"):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        assert ("error", "rd_perplexity_unavailable") in events

    def test_pdr_issued_logged_as_error_on_unavailable(self):
        cfg = make_config()
        events = []

        class _CapLog:
            def info(self, e, **k): events.append(("info", e))
            def warn(self, e, **k): events.append(("warn", e))
            def error(self, e, **k): events.append(("error", e))
            def debug(self, e, **k): events.append(("debug", e))

        with patch("hypogeum.agents.research_director.get_logger", return_value=_CapLog()), \
             patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="unavailable"):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        assert ("error", "rd_pdr_issued") in events

    def test_synthesis_started_logged(self):
        events = self._collect_events()
        assert ("info", "rd_synthesis_started") in events

    def test_degraded_logged_as_warn(self):
        cfg = make_config()
        events = []

        class _CapLog:
            def info(self, e, **k): events.append(("info", e))
            def warn(self, e, **k): events.append(("warn", e))
            def error(self, e, **k): events.append(("error", e))
            def debug(self, e, **k): events.append(("debug", e))

        with patch("hypogeum.agents.research_director.get_logger", return_value=_CapLog()), \
             patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="degraded"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()), \
             patch("hypogeum.agents.research_director.run_agent",
                   return_value=_make_research_package()):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        assert ("warn", "rd_perplexity_degraded") in events


# ---------------------------------------------------------------------------
# TestSystemPrompt
# ---------------------------------------------------------------------------

class TestSystemPrompt:
    """System prompt file and module-level constant."""

    def test_prompt_file_exists(self):
        assert _PROMPT_FILE.exists(), f"Prompt file not found: {_PROMPT_FILE}"

    def test_prompt_file_is_regular_file(self):
        assert _PROMPT_FILE.is_file()

    def test_system_prompt_is_non_empty_string(self):
        assert isinstance(SYSTEM_PROMPT, str) and len(SYSTEM_PROMPT.strip()) > 0

    def test_system_prompt_matches_file_contents(self):
        from hypogeum.prompt_loader import load_prompt
        assert SYSTEM_PROMPT == load_prompt(_PROMPT_FILE)

    def test_prompt_contains_narrative_signal_taxonomy(self):
        assert "shock_fact" in SYSTEM_PROMPT
        assert "mystery_deepening" in SYSTEM_PROMPT

    def test_prompt_contains_synthesis_task(self):
        assert "synthesize_research" in SYSTEM_PROMPT

    def test_system_prompt_passed_to_run_agent(self):
        cfg = make_config()
        captured = []

        def capture(**kwargs):
            captured.append(kwargs.get("system_prompt"))
            return _make_research_package()

        with patch("hypogeum.agents.research_director.check_perplexity_health",
                   return_value="available"), \
             patch("hypogeum.agents.research_director.query_perplexity",
                   side_effect=_success_side_effect()), \
             patch("hypogeum.agents.research_director.run_agent",
                   side_effect=capture):
            run_research_director(BRIEF, cfg, EPISODE_ID)
        assert captured[0] == SYSTEM_PROMPT


class TestChannelRegisterPhases:
    """The research phases adapt to the channel's register (channel separation)."""

    def test_default_is_investigative(self):
        from hypogeum.agents.research_director import _research_phases_for, _RESEARCH_PHASES
        assert _research_phases_for(None) is _RESEARCH_PHASES
        assert _research_phases_for({"tone": "investigative", "genre": "documentary history"}) is _RESEARCH_PHASES

    def test_gentle_channel_gets_gentle_phases(self):
        from hypogeum.agents.research_director import _research_phases_for, _GENTLE_RESEARCH_PHASES
        got = _research_phases_for({"tone": "gentle soothing", "genre": "bedtime stories for toddlers"})
        assert got is _GENTLE_RESEARCH_PHASES
        # stable phase_ids, adapted keywords
        assert [p["phase_id"] for p in got] == [p["phase_id"] for p in _RESEARCH_PHASES]
        joined = " ".join(p["query_template"] for p in got)
        assert "peer reviewed" not in joined
        assert "archaeological" not in joined
