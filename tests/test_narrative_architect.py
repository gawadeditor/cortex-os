"""
tests/test_narrative_architect.py — Unit tests for hypogeum/agents/narrative_architect.py

All run_agent() calls are mocked. No real Claude calls. No real filesystem writes.

Test coverage:
  TestSuccessfulNarrative      — run_agent returns narrative_package; all fields propagate
  TestWeakResearchHandling     — thin/empty research_package still processed; no halt
  TestChapterGeneration        — episode_structure fields: archetype, sections, narrative_arc
  TestRevealStructure          — reveal_architecture, narrative_signals, traceability
  TestShortsOpportunities      — exactly 3 shorts_selection; text_overlay_plan fields
  TestNarrativePackageSchema   — valid package passes schema; invalid packages caught
  TestClaudeIntegration        — run_agent called with Opus 4.6, correct kwargs
  TestInputPackage             — input_package content: task, research_package, output_dir
  TestExceptionPropagation     — ResponseParseError, SchemaError, RuntimeError propagate
  TestLogging                  — na_narrative_started, na_narrative_completed logged
  TestSystemPrompt             — prompt file exists, non-empty, loaded, content checks
  TestPipelineIntegration      — stub delegates; success → SE stub (NotImplementedError)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agent import ResponseParseError
from hypogeum.agents.narrative_architect import (
    SYSTEM_PROMPT,
    _AGENT_NAME,
    _CLAUDE_OPUS,
    _NA_MAX_TOKENS,
    _PROMPT_FILE,
    run_narrative_architect,
)
from hypogeum.config import Config
from hypogeum.pipeline import _run_narrative_architect as pipeline_na_stub
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


def _make_short(short_id: str = "short_001") -> dict:
    return {
        "short_id": short_id,
        "title": f"Short {short_id}",
        "source_claim_id": "clm_001",
        "visual_anchor_signal": "The bronze gears",
        "spoiler_policy": "no_central_answer",
        "narrative_arc": "From discovery to wonder.",
        "text_overlay_plan": {
            "opening_hook": {"beat_position": 0, "narrative_intent": "Open the mystery."},
            "key_information": [{"beat_position": 2, "narrative_intent": "Key fact here."}],
            "payoff": {"beat_position": 5, "narrative_intent": "Land the wonder."},
        },
    }


def _make_narrative_package(archetype: str = "default_six_section") -> dict:
    return {
        "episode_metadata": {
            "topic": "The Antikythera Mechanism",
            "archetype": archetype,
            "archetype_rationale": "Rich mystery with strong visual anchors.",
            "central_question": "Who built the Antikythera Mechanism?",
            "central_answer": "Likely Hipparchus or his school, based on astronomical calibration.",
            "episode_id": "ep_001",
            "narrative_model": "narrative_architect_v1",
        },
        "episode_structure": {
            "archetype": archetype,
            "sections": [
                {
                    "section_id": "s_001",
                    "title": "Hook",
                    "narrative_purpose": "Open the central question.",
                    "entity_count": 1,
                    "key_claims": ["clm_001"],
                    "open_loops_opened": ["What is this object?"],
                    "open_loops_closed": [],
                    "viewer_tension_level": "high",
                },
                {
                    "section_id": "s_002",
                    "title": "Context",
                    "narrative_purpose": "Establish historical setting.",
                    "entity_count": 3,
                    "key_claims": ["clm_001"],
                    "open_loops_opened": [],
                    "open_loops_closed": [],
                    "viewer_tension_level": "medium",
                },
            ],
        },
        "narrative_arc": {
            "setup": "Establish the Antikythera wreck discovery and the object's strangeness.",
            "escalation": "Each line of research deepens the impossibility.",
            "revelation": "The mechanism maps the entire known cosmos — built by masters.",
            "resolution": "We still don't know how they knew what they knew.",
        },
        "reveal_architecture": {
            "reveal_map": [
                {
                    "reveal_id": "rev_001",
                    "section_id": "s_002",
                    "beat_position": 3,
                    "claim_id": "clm_001",
                    "reveal_type": "central",
                    "impact_justification": "Answers the central question posed in Hook.",
                    "adjacent_reveal_exception": None,
                }
            ],
            "viewer_question_state": [
                {"after_section_id": "s_001", "question": "What is this object?"},
                {"after_section_id": "s_002", "question": "How was this possible?"},
            ],
            "open_question_tracker": [
                {
                    "question_id": "oq_001",
                    "text": "Who specifically built it?",
                    "opened_in_section": "s_001",
                    "resolved_in_section": None,
                }
            ],
        },
        "shorts_selection": [
            _make_short("short_001"),
            _make_short("short_002"),
            _make_short("short_003"),
        ],
        "narrative_signals": [
            {
                "signal_id": "ns_001",
                "signal_type": "shock_fact",
                "section_id": "s_001",
                "source_claim_id": "clm_001",
                "derivation_rationale": "Fact defies expectation about ancient technology.",
                "placement_beat": 1,
            }
        ],
        "differentiation_record": {
            "ep_hypothesis": "No channel explored the astronomical predictions angle.",
            "authority_tier": "refinement",
            "na_angle": "Focus on the predictive accuracy as evidence of lost knowledge.",
            "adjustment_rationale": "Research strongly supports the astronomical angle.",
            "replacement_rationale": None,
        },
        "narrative_integrity_log": [],
        "traceability": {
            "total_claims_used": 1,
            "total_reveals": 1,
            "total_open_questions": 1,
            "research_coverage_warning": False,
            "claim_ids_used": ["clm_001"],
        },
    }


def _make_research_package(
    claims=None,
    quality="high",
    production_ready=True,
) -> dict:
    if claims is None:
        claims = [
            {
                "claim_id": "clm_001",
                "text": "The Antikythera Mechanism dates to around 150–100 BCE.",
                "confidence": 0.95,
                "sources": ["src_001"],
                "narrative_potential": "Establishes historical grounding.",
                "narrative_signal_type": "shock_fact",
            }
        ]
    return {
        "research_run_metadata": {
            "episode_id": "ep_001",
            "topic": "The Antikythera Mechanism: Ancient Computer",
            "phases_completed": 4,
            "phases_failed": 0,
            "total_sources": 8,
            "research_model": "research_director_v1",
            "synthesised_by": "research_director",
        },
        "research_strategy": {
            "phases": [],
            "differentiation_hypothesis_tested": "yes",
            "coverage_assessment": "Good.",
        },
        "claims": claims,
        "contradictions": [],
        "open_questions": [],
        "source_registry": [{"source_id": "src_001", "url": "https://example.com", "phase_id": "core_facts"}],
        "competitive_landscape": {
            "coverage_density": "medium",
            "dominant_angle": "mechanical complexity",
            "differentiation_gap": "astronomical predictions",
            "hypothesis_supported": "yes",
            "notes": "",
        },
        "research_run_summary": {
            "overall_quality": quality,
            "claim_count": len(claims),
            "high_confidence_claim_count": len(claims),
            "contradiction_count": 0,
            "open_question_count": 0,
            "narrative_signal_distribution": {
                k: 0 for k in ("shock_fact", "turning_point", "human_stakes", "irony",
                               "mystery_deepening", "reveal", "visual_anchor",
                               "foreshadowing", "false_assumption")
            },
            "quality_warnings": [],
        },
        "package_integrity": {
            "production_ready": production_ready,
            "integrity_notes": "",
        },
    }


EPISODE_ID = "ep_001"
RP = _make_research_package()


def _run_na(research_package=None, run_agent_return=None, output_dir=None):
    """Helper: run run_narrative_architect with mocked run_agent."""
    if research_package is None:
        research_package = _make_research_package()
    if run_agent_return is None:
        run_agent_return = _make_narrative_package()
    cfg = make_config()
    with patch("hypogeum.agents.narrative_architect.run_agent",
               return_value=run_agent_return):
        return run_narrative_architect(research_package, cfg, EPISODE_ID, output_dir)


# ---------------------------------------------------------------------------
# TestSuccessfulNarrative
# ---------------------------------------------------------------------------

class TestSuccessfulNarrative:
    """run_agent returns narrative_package; result passed through unchanged."""

    def test_returns_dict(self):
        result = _run_na()
        assert isinstance(result, dict)

    def test_returns_run_agent_output(self):
        np_pkg = _make_narrative_package()
        result = _run_na(run_agent_return=np_pkg)
        assert result is np_pkg

    def test_run_agent_called_exactly_once(self):
        cfg = make_config()
        with patch("hypogeum.agents.narrative_architect.run_agent",
                   return_value=_make_narrative_package()) as mock_ra:
            run_narrative_architect(RP, cfg, EPISODE_ID)
        assert mock_ra.call_count == 1

    def test_returns_non_empty_dict(self):
        result = _run_na()
        assert len(result) > 0

    def test_episode_structure_present(self):
        result = _run_na()
        assert "episode_structure" in result

    def test_shorts_selection_present(self):
        result = _run_na()
        assert "shorts_selection" in result


# ---------------------------------------------------------------------------
# TestWeakResearchHandling
# ---------------------------------------------------------------------------

class TestWeakResearchHandling:
    """Thin/empty research_package does not halt NA — Claude handles it."""

    def test_empty_claims_list_still_calls_run_agent(self):
        rp = _make_research_package(claims=[])
        cfg = make_config()
        with patch("hypogeum.agents.narrative_architect.run_agent",
                   return_value=_make_narrative_package()) as mock_ra:
            run_narrative_architect(rp, cfg, EPISODE_ID)
        assert mock_ra.call_count == 1

    def test_low_quality_research_still_processed(self):
        rp = _make_research_package(quality="low")
        result = _run_na(research_package=rp)
        assert isinstance(result, dict)

    def test_production_not_ready_still_processed(self):
        rp = _make_research_package(production_ready=False)
        result = _run_na(research_package=rp)
        assert isinstance(result, dict)

    def test_minimal_research_package_accepted(self):
        rp = {
            "research_run_metadata": {}, "research_strategy": {},
            "claims": [], "contradictions": [], "open_questions": [],
            "source_registry": [], "competitive_landscape": {},
            "research_run_summary": {}, "package_integrity": {},
        }
        result = _run_na(research_package=rp)
        assert isinstance(result, dict)

    def test_claims_count_logged_on_start(self):
        rp = _make_research_package(claims=[
            {"claim_id": "c1", "text": "Fact.", "confidence": 0.9}
        ])
        cfg = make_config()
        logged = []

        class _CapLog:
            def info(self, e, **kw): logged.append((e, kw))
            def warn(self, e, **kw): pass
            def error(self, e, **kw): pass

        with patch("hypogeum.agents.narrative_architect.get_logger", return_value=_CapLog()), \
             patch("hypogeum.agents.narrative_architect.run_agent",
                   return_value=_make_narrative_package()):
            run_narrative_architect(rp, cfg, EPISODE_ID)
        start_events = [kw for e, kw in logged if e == "na_narrative_started"]
        assert start_events and start_events[0]["claims_count"] == 1


# ---------------------------------------------------------------------------
# TestChapterGeneration
# ---------------------------------------------------------------------------

class TestChapterGeneration:
    """episode_structure, narrative_arc, and archetype fields."""

    def test_episode_structure_is_dict(self):
        result = _run_na()
        assert isinstance(result["episode_structure"], dict)

    def test_archetype_is_string(self):
        result = _run_na()
        assert isinstance(result["episode_structure"]["archetype"], str)

    def test_valid_archetype_values(self):
        for arch in ("default_six_section", "discovery_investigation", "wrong_answer"):
            np_pkg = _make_narrative_package(archetype=arch)
            result = _run_na(run_agent_return=np_pkg)
            assert result["episode_structure"]["archetype"] == arch

    def test_sections_is_list(self):
        result = _run_na()
        assert isinstance(result["episode_structure"]["sections"], list)

    def test_sections_non_empty(self):
        result = _run_na()
        assert len(result["episode_structure"]["sections"]) >= 1

    def test_narrative_arc_has_four_keys(self):
        result = _run_na()
        arc = result["narrative_arc"]
        for key in ("setup", "escalation", "revelation", "resolution"):
            assert key in arc

    def test_narrative_arc_values_are_non_empty(self):
        result = _run_na()
        arc = result["narrative_arc"]
        for key in ("setup", "escalation", "revelation", "resolution"):
            assert arc[key]

    def test_episode_metadata_present(self):
        result = _run_na()
        assert isinstance(result["episode_metadata"], dict)


# ---------------------------------------------------------------------------
# TestRevealStructure
# ---------------------------------------------------------------------------

class TestRevealStructure:
    """reveal_architecture, narrative_signals, traceability."""

    def test_reveal_architecture_is_dict(self):
        result = _run_na()
        assert isinstance(result["reveal_architecture"], dict)

    def test_narrative_signals_is_list(self):
        result = _run_na()
        assert isinstance(result["narrative_signals"], list)

    def test_narrative_integrity_log_is_list(self):
        result = _run_na()
        assert isinstance(result["narrative_integrity_log"], list)

    def test_traceability_is_dict(self):
        result = _run_na()
        assert isinstance(result["traceability"], dict)

    def test_differentiation_record_is_dict(self):
        result = _run_na()
        assert isinstance(result["differentiation_record"], dict)

    def test_signal_has_source_claim_id(self):
        np_pkg = _make_narrative_package()
        result = _run_na(run_agent_return=np_pkg)
        if result["narrative_signals"]:
            assert "source_claim_id" in result["narrative_signals"][0]

    def test_signal_has_derivation_rationale(self):
        np_pkg = _make_narrative_package()
        result = _run_na(run_agent_return=np_pkg)
        if result["narrative_signals"]:
            assert "derivation_rationale" in result["narrative_signals"][0]


# ---------------------------------------------------------------------------
# TestShortsOpportunities
# ---------------------------------------------------------------------------

class TestShortsOpportunities:
    """Exactly 3 shorts_selection entries with required fields."""

    def test_exactly_three_shorts(self):
        result = _run_na()
        assert len(result["shorts_selection"]) == 3

    def test_each_short_has_short_id(self):
        result = _run_na()
        for short in result["shorts_selection"]:
            assert "short_id" in short and isinstance(short["short_id"], str)

    def test_each_short_has_text_overlay_plan(self):
        result = _run_na()
        for short in result["shorts_selection"]:
            assert "text_overlay_plan" in short
            assert isinstance(short["text_overlay_plan"], dict)

    def test_text_overlay_plan_has_opening_hook(self):
        result = _run_na()
        for short in result["shorts_selection"]:
            assert "opening_hook" in short["text_overlay_plan"]

    def test_text_overlay_plan_has_payoff(self):
        result = _run_na()
        for short in result["shorts_selection"]:
            assert "payoff" in short["text_overlay_plan"]

    def test_text_overlay_plan_has_key_information(self):
        result = _run_na()
        for short in result["shorts_selection"]:
            assert "key_information" in short["text_overlay_plan"]

    def test_short_ids_are_unique(self):
        result = _run_na()
        ids = [s["short_id"] for s in result["shorts_selection"]]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# TestNarrativePackageSchema
# ---------------------------------------------------------------------------

class TestNarrativePackageSchema:
    """Schema validation via schemas.py."""

    def test_valid_package_passes_schema(self):
        pkg = _make_narrative_package()
        validate("narrative_package", pkg)  # must not raise

    def test_wrong_shorts_count_fails_schema(self):
        pkg = _make_narrative_package()
        pkg["shorts_selection"] = pkg["shorts_selection"][:2]  # only 2
        with pytest.raises(SchemaError):
            validate("narrative_package", pkg)

    def test_missing_narrative_arc_fails_schema(self):
        pkg = _make_narrative_package()
        del pkg["narrative_arc"]
        with pytest.raises(SchemaError):
            validate("narrative_package", pkg)

    def test_missing_short_id_fails_schema(self):
        pkg = _make_narrative_package()
        del pkg["shorts_selection"][0]["short_id"]
        with pytest.raises(SchemaError):
            validate("narrative_package", pkg)

    def test_missing_text_overlay_plan_fails_schema(self):
        pkg = _make_narrative_package()
        del pkg["shorts_selection"][0]["text_overlay_plan"]
        with pytest.raises(SchemaError):
            validate("narrative_package", pkg)

    def test_missing_episode_structure_fails_schema(self):
        pkg = _make_narrative_package()
        del pkg["episode_structure"]
        with pytest.raises(SchemaError):
            validate("narrative_package", pkg)


# ---------------------------------------------------------------------------
# TestClaudeIntegration
# ---------------------------------------------------------------------------

class TestClaudeIntegration:
    """run_agent called with Opus 4.6 and all required kwargs."""

    def _capture(self):
        cfg = make_config()
        captured = []

        def cap(**kwargs):
            captured.append(kwargs)
            return _make_narrative_package()

        with patch("hypogeum.agents.narrative_architect.run_agent", side_effect=cap):
            run_narrative_architect(RP, cfg, EPISODE_ID)
        return captured[0], cfg

    def test_model_slug_is_opus(self):
        kwargs, _ = self._capture()
        assert kwargs["model_slug"] == "anthropic/claude-opus-4.6"

    def test_output_schema_is_narrative_package(self):
        kwargs, _ = self._capture()
        assert kwargs["output_schema"] == "narrative_package"

    def test_max_tokens_is_16384(self):
        kwargs, _ = self._capture()
        assert kwargs["max_tokens"] == 16384

    def test_agent_name_is_narrative_architect(self):
        kwargs, _ = self._capture()
        assert kwargs["agent_name"] == "narrative_architect"

    def test_episode_id_passed_through(self):
        kwargs, _ = self._capture()
        assert kwargs["episode_id"] == EPISODE_ID

    def test_config_passed_through(self):
        cfg = make_config()
        captured = []

        def cap(**kwargs):
            captured.append(kwargs)
            return _make_narrative_package()

        with patch("hypogeum.agents.narrative_architect.run_agent", side_effect=cap):
            run_narrative_architect(RP, cfg, EPISODE_ID)
        assert captured[0]["config"] is cfg

    def test_system_prompt_is_non_empty_string(self):
        kwargs, _ = self._capture()
        assert isinstance(kwargs["system_prompt"], str) and len(kwargs["system_prompt"]) > 0

    def test_model_slug_constant(self):
        assert _CLAUDE_OPUS == "anthropic/claude-opus-4.6"

    def test_max_tokens_constant(self):
        assert _NA_MAX_TOKENS == 16384


# ---------------------------------------------------------------------------
# TestInputPackage
# ---------------------------------------------------------------------------

class TestInputPackage:
    """input_package content: task field and research_package."""

    def _capture_input(self, research_package=None):
        if research_package is None:
            research_package = _make_research_package()
        cfg = make_config()
        captured = []

        def cap(**kwargs):
            captured.append(kwargs.get("input_package", {}))
            return _make_narrative_package()

        with patch("hypogeum.agents.narrative_architect.run_agent", side_effect=cap):
            run_narrative_architect(research_package, cfg, EPISODE_ID)
        return captured[0], research_package

    def test_input_package_is_dict(self):
        pkg, _ = self._capture_input()
        assert isinstance(pkg, dict)

    def test_task_field_is_build_narrative(self):
        pkg, _ = self._capture_input()
        assert pkg["task"] == "build_narrative"

    def test_research_package_in_input(self):
        pkg, rp = self._capture_input()
        assert "research_package" in pkg

    def test_research_package_passed_as_is(self):
        rp = _make_research_package()
        pkg, _ = self._capture_input(research_package=rp)
        assert pkg["research_package"] is rp

    def test_operator_notes_absent_when_not_given(self):
        pkg, _ = self._capture_input()
        assert "operator_notes" not in pkg

    def test_operator_notes_included_when_given(self):
        cfg = make_config()
        captured = []

        def cap(**kwargs):
            captured.append(kwargs.get("input_package", {}))
            return _make_narrative_package()

        with patch("hypogeum.agents.narrative_architect.run_agent", side_effect=cap):
            run_narrative_architect(
                _make_research_package(), cfg, EPISODE_ID,
                operator_notes="Fern should get MORE lost, not less.",
            )
        assert "Fern should get MORE lost, not less." in captured[0]["operator_notes"]

    def test_empty_operator_notes_is_not_included(self):
        cfg = make_config()
        captured = []

        def cap(**kwargs):
            captured.append(kwargs.get("input_package", {}))
            return _make_narrative_package()

        with patch("hypogeum.agents.narrative_architect.run_agent", side_effect=cap):
            run_narrative_architect(_make_research_package(), cfg, EPISODE_ID, operator_notes="")
        assert "operator_notes" not in captured[0]

    def test_output_dir_none_propagated(self):
        cfg = make_config()
        captured = []

        def cap(**kwargs):
            captured.append(kwargs)
            return _make_narrative_package()

        with patch("hypogeum.agents.narrative_architect.run_agent", side_effect=cap):
            run_narrative_architect(RP, cfg, EPISODE_ID, output_dir=None)
        assert captured[0]["output_dir"] is None

    def test_output_dir_path_propagated(self):
        cfg = make_config()
        captured = []

        def cap(**kwargs):
            captured.append(kwargs)
            return _make_narrative_package()

        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            with patch("hypogeum.agents.narrative_architect.run_agent", side_effect=cap):
                run_narrative_architect(RP, cfg, EPISODE_ID, output_dir=p)
        assert captured[0]["output_dir"] == p


# ---------------------------------------------------------------------------
# TestExceptionPropagation
# ---------------------------------------------------------------------------

class TestExceptionPropagation:
    """Exceptions from run_agent propagate unchanged."""

    def test_response_parse_error_propagates(self):
        cfg = make_config()
        err = ResponseParseError("narrative_architect", "invalid_json")
        with patch("hypogeum.agents.narrative_architect.run_agent", side_effect=err):
            with pytest.raises(ResponseParseError) as exc_info:
                run_narrative_architect(RP, cfg, EPISODE_ID)
        assert exc_info.value is err

    def test_schema_error_propagates(self):
        cfg = make_config()
        err = SchemaError("narrative_package", "shorts_selection", "must have exactly 3 items")
        with patch("hypogeum.agents.narrative_architect.run_agent", side_effect=err):
            with pytest.raises(SchemaError) as exc_info:
                run_narrative_architect(RP, cfg, EPISODE_ID)
        assert exc_info.value is err

    def test_runtime_error_propagates(self):
        cfg = make_config()
        err = RuntimeError("OpenRouter hard failure")
        with patch("hypogeum.agents.narrative_architect.run_agent", side_effect=err):
            with pytest.raises(RuntimeError) as exc_info:
                run_narrative_architect(RP, cfg, EPISODE_ID)
        assert exc_info.value is err

    def test_run_agent_called_once_even_on_error(self):
        cfg = make_config()
        with patch("hypogeum.agents.narrative_architect.run_agent",
                   side_effect=RuntimeError("fail")) as mock_ra:
            with pytest.raises(RuntimeError):
                run_narrative_architect(RP, cfg, EPISODE_ID)
        assert mock_ra.call_count == 1


# ---------------------------------------------------------------------------
# TestLogging
# ---------------------------------------------------------------------------

class TestLogging:
    """na_narrative_started and na_narrative_completed emitted at info level."""

    def _collect_events(self, research_package=None):
        if research_package is None:
            research_package = _make_research_package()
        cfg = make_config()
        events = []

        class _CapLog:
            def info(self, e, **kw):  events.append(("info", e, kw))
            def warn(self, e, **kw):  events.append(("warn", e, kw))
            def error(self, e, **kw): events.append(("error", e, kw))
            def debug(self, e, **kw): events.append(("debug", e, kw))

        with patch("hypogeum.agents.narrative_architect.get_logger", return_value=_CapLog()), \
             patch("hypogeum.agents.narrative_architect.run_agent",
                   return_value=_make_narrative_package()):
            run_narrative_architect(research_package, cfg, EPISODE_ID)
        return events

    def test_na_narrative_started_logged(self):
        events = self._collect_events()
        assert any(e == "na_narrative_started" for _, e, _ in events)

    def test_na_narrative_started_is_info(self):
        events = self._collect_events()
        assert any(lvl == "info" and e == "na_narrative_started" for lvl, e, _ in events)

    def test_na_narrative_completed_logged(self):
        events = self._collect_events()
        assert any(e == "na_narrative_completed" for _, e, _ in events)

    def test_na_narrative_completed_is_info(self):
        events = self._collect_events()
        assert any(lvl == "info" and e == "na_narrative_completed" for lvl, e, _ in events)

    def test_archetype_in_completed_log(self):
        events = self._collect_events()
        completed = [(lvl, e, kw) for lvl, e, kw in events if e == "na_narrative_completed"]
        assert completed and "archetype" in completed[0][2]

    def test_claims_count_in_started_log(self):
        rp = _make_research_package(claims=[
            {"claim_id": "c1", "text": "A.", "confidence": 0.9}
        ])
        events = self._collect_events(research_package=rp)
        started = [(lvl, e, kw) for lvl, e, kw in events if e == "na_narrative_started"]
        assert started and started[0][2]["claims_count"] == 1


# ---------------------------------------------------------------------------
# TestSystemPrompt
# ---------------------------------------------------------------------------

class TestSystemPrompt:
    """System prompt file and module-level constant."""

    def test_prompt_file_exists(self):
        assert _PROMPT_FILE.exists(), f"Prompt file not found: {_PROMPT_FILE}"

    def test_prompt_is_regular_file(self):
        assert _PROMPT_FILE.is_file()

    def test_system_prompt_is_non_empty_string(self):
        assert isinstance(SYSTEM_PROMPT, str) and len(SYSTEM_PROMPT.strip()) > 0

    def test_system_prompt_matches_file(self):
        from hypogeum.prompt_loader import load_prompt
        assert SYSTEM_PROMPT == load_prompt(_PROMPT_FILE)

    def test_prompt_mentions_three_archetypes(self):
        assert "default_six_section" in SYSTEM_PROMPT
        assert "discovery_investigation" in SYSTEM_PROMPT
        assert "wrong_answer" in SYSTEM_PROMPT

    def test_prompt_mentions_text_overlay_plan(self):
        assert "text_overlay_plan" in SYSTEM_PROMPT

    def test_prompt_mentions_shorts_selection(self):
        assert "shorts_selection" in SYSTEM_PROMPT

    def test_prompt_mentions_reveal_architecture(self):
        assert "reveal_architecture" in SYSTEM_PROMPT

    def test_system_prompt_passed_to_run_agent(self):
        cfg = make_config()
        captured = []

        def cap(**kwargs):
            captured.append(kwargs.get("system_prompt"))
            return _make_narrative_package()

        with patch("hypogeum.agents.narrative_architect.run_agent", side_effect=cap):
            run_narrative_architect(RP, cfg, EPISODE_ID)
        assert captured[0] == SYSTEM_PROMPT

    def test_agent_name_constant(self):
        assert _AGENT_NAME == "narrative_architect"


# ---------------------------------------------------------------------------
# TestPipelineIntegration
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    """Pipeline stub delegates to run_narrative_architect."""

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

    def _ep_output(self):
        sc = {
            "scorecard_id": "sc_001", "topic_id": "t_001",
            "dimensions": {k: 8.0 for k in (
                "audience_demand", "curiosity_potential", "mystery_potential",
                "visual_potential", "differentiation_opportunity", "shorts_potential",
            )},
            "overall_score": 8.0, "decision": "PASS",
            "issued_at": "2026-06-01T00:00:00Z", "issued_by": "executive_producer",
        }
        brief = {
            "topic_id": "t_001", "episode_title": "Test", "topic_category": "ancient_mystery",
            "logline": "A mystery.", "audience_demand_signal": "High.",
            "curiosity_hook": "Why?", "mystery_anchor": "Unknown.",
            "differentiation_hypothesis": "New angle.",
            "research_depth_flags": {"differentiation_opportunity": "deep"},
            "shorts_directions": ["Dir 1", "Dir 2"],
            "issued_at": "2026-06-01T00:00:00Z", "issued_by": "executive_producer",
        }
        return sc, brief

    def test_stub_delegates_to_run_narrative_architect(self, tmp_path):
        cfg = make_config()
        np_pkg = _make_narrative_package()
        with patch("hypogeum.pipeline.run_narrative_architect",
                   return_value=np_pkg) as mock_na:
            result = pipeline_na_stub({}, cfg, EPISODE_ID, tmp_path)
        mock_na.assert_called_once()
        assert result is np_pkg

    def test_stub_passes_research_package(self, tmp_path):
        cfg = make_config()
        rp = _make_research_package()
        with patch("hypogeum.pipeline.run_narrative_architect",
                   return_value=_make_narrative_package()) as mock_na:
            pipeline_na_stub(rp, cfg, EPISODE_ID, tmp_path)
        assert mock_na.call_args.kwargs["research_package"] is rp

    def test_stub_passes_episode_id(self, tmp_path):
        cfg = make_config()
        with patch("hypogeum.pipeline.run_narrative_architect",
                   return_value=_make_narrative_package()) as mock_na:
            pipeline_na_stub({}, cfg, EPISODE_ID, tmp_path)
        assert mock_na.call_args.kwargs["episode_id"] == EPISODE_ID

    def test_success_continues_to_vpd_stub(self):
        """Successful NA → pipeline proceeds past SE through all remaining agents to completion."""
        from hypogeum.pipeline import run_pipeline
        cfg = make_config()
        sc, brief = self._ep_output()

        with patch("hypogeum.pipeline.run_executive_producer", return_value=(sc, brief)), \
             patch("hypogeum.pipeline.validate_narration_completeness"), \
             patch("hypogeum.pipeline.run_research_director",
                   return_value=(_make_research_package(), None)), \
             patch("hypogeum.pipeline.run_narrative_architect",
                   return_value=_make_narrative_package()), \
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
            result = run_pipeline("Test Topic", EPISODE_ID, cfg, channel_id=None)
        assert result.status == "completed"

    def test_exception_propagates_through_pipeline(self, tmp_path):
        cfg = make_config()
        err = SchemaError("narrative_package", "shorts_selection", "must have exactly 3")
        with patch("hypogeum.pipeline.run_narrative_architect", side_effect=err):
            with pytest.raises(SchemaError) as exc_info:
                pipeline_na_stub({}, cfg, EPISODE_ID, tmp_path)
        assert exc_info.value is err

    def test_stub_passes_output_dir(self):
        cfg = make_config()
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            with patch("hypogeum.pipeline.run_narrative_architect",
                       return_value=_make_narrative_package()) as mock_na:
                pipeline_na_stub({}, cfg, EPISODE_ID, p)
            assert mock_na.call_args.kwargs["output_dir"] == p
