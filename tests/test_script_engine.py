"""
tests/test_script_engine.py — Unit tests for hypogeum/agents/script_engine.py

All run_agent() calls are mocked. No real Claude calls. No real filesystem writes.

Test coverage:
  TestSuccessfulScript         — run_agent returns script_package; all fields propagate
  TestCTAGeneration            — exactly 2 CTAs; subscribe + comment types; no prohibited language
  TestChapterGeneration        — chapter_markers per section; publishing-ready titles; unique
  TestShortsMarkers            — exactly 3 extraction markers; opening/closing beat refs match
  TestShortsScripts            — exactly 3 shorts_scripts; narration_beats + text_overlays
  TestScriptPackageSchema      — valid package passes schema; invalid packages caught
  TestClaudeIntegration        — run_agent called with Sonnet 4.6, correct kwargs
  TestInputPackage             — input_package content: task, narrative_package, output_dir
  TestExceptionPropagation     — ResponseParseError, SchemaError, RuntimeError propagate
  TestLogging                  — se_script_started, se_script_completed logged
  TestSystemPrompt             — prompt file exists, non-empty, loaded, CTA / chapter content
  TestPipelineIntegration      — stub delegates; success → VPD stub (NotImplementedError)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agent import ResponseParseError
from hypogeum.agents.script_engine import (
    PINNED_HUMANIZER_SHA,
    SYSTEM_PROMPT,
    _AGENT_NAME,
    _CLAUDE_SONNET,
    _HUMANIZER_SKILL_PATH,
    _PROMPT_FILE,
    _SE_MAX_TOKENS,
    run_script_engine,
)
from hypogeum.config import Config
from hypogeum.pipeline import _run_script_engine as pipeline_se_stub
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

EPISODE_ID = "ep_test_001"


def make_config() -> Config:
    from unittest.mock import patch as _patch
    original = {k: os.environ.get(k) for k in _PIPELINE_ENV_VARS}
    for k in _PIPELINE_ENV_VARS:
        os.environ.pop(k, None)
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    # Patch _load_dotenv so the real .env file does not re-populate cleared vars
    with _patch("hypogeum.config._load_dotenv"):
        try:
            return Config()
        finally:
            for k in _PIPELINE_ENV_VARS:
                os.environ.pop(k, None)
            for k, v in original.items():
                if v is not None:
                    os.environ[k] = v


cfg = make_config()


def _make_overlay(overlay_id: str, beat_ref: str, overlay_type: str = "opening_hook") -> dict:
    return {
        "overlay_id": overlay_id,
        "overlay_type": overlay_type,
        "text": "Ancient gears turn",
        "beat_reference": beat_ref,
        "display_duration_seconds": 3.0,
    }


def _make_short_script(short_id: str, beat_id: str, overlay_id: str) -> dict:
    return {
        "short_id": short_id,
        "narration_beats": [
            {"beat_id": beat_id, "text": "A short narration beat.", "beat_type": "narration"},
        ],
        "text_overlays": [_make_overlay(overlay_id, beat_id)],
    }


def _make_script_package() -> dict:
    return {
        "episode_metadata": {
            "topic": "The Antikythera Mechanism",
            "archetype": "default_six_section",
            "episode_id": "ep_001",
            "script_model": "script_engine_v1",
        },
        "sections": [
            {
                "section_id": "s_001",
                "title": "Hook",
                "narration_beats": [
                    {"beat_id": "b_001", "text": "Opening hook narration.", "beat_type": "hook"},
                ],
            },
            {
                "section_id": "s_002",
                "title": "Context",
                "narration_beats": [
                    {"beat_id": "b_002", "text": "Context narration.", "beat_type": "narration"},
                    {
                        "beat_id": "b_003",
                        "text": "If you want to follow investigations like this one, subscribing keeps you in the loop.",
                        "beat_type": "cta",
                    },
                ],
            },
            {
                "section_id": "s_003",
                "title": "Rising Mystery",
                "narration_beats": [
                    {"beat_id": "b_004", "text": "Mystery deepens.", "beat_type": "narration"},
                ],
            },
            {
                "section_id": "s_004",
                "title": "Escalation",
                "narration_beats": [
                    {"beat_id": "b_005", "text": "Escalation narration.", "beat_type": "narration"},
                    {
                        "beat_id": "b_006",
                        "text": "What do you think happened in that final decade? The comments are the place to put it.",
                        "beat_type": "cta",
                    },
                ],
            },
            {
                "section_id": "s_005",
                "title": "Revelation",
                "narration_beats": [
                    {"beat_id": "b_007", "text": "The reveal lands here.", "beat_type": "reveal"},
                ],
            },
            {
                "section_id": "s_006",
                "title": "Resolution",
                "narration_beats": [
                    {"beat_id": "b_008", "text": "Resolution narration.", "beat_type": "resolution"},
                ],
            },
        ],
        "shorts_scripts": [
            _make_short_script("short_001", "b_s01", "ov_001"),
            _make_short_script("short_002", "b_s02", "ov_002"),
            _make_short_script("short_003", "b_s03", "ov_003"),
        ],
        "chapter_markers": [
            {"chapter_id": "ch_001", "section_id": "s_001", "title": "The Object Nobody Recognised", "opening_beat_id": "b_001"},
            {"chapter_id": "ch_002", "section_id": "s_002", "title": "2000 Years in the Dark",      "opening_beat_id": "b_002"},
            {"chapter_id": "ch_003", "section_id": "s_003", "title": "What The Gears Reveal",       "opening_beat_id": "b_004"},
            {"chapter_id": "ch_004", "section_id": "s_004", "title": "The Evidence Compounds",      "opening_beat_id": "b_005"},
            {"chapter_id": "ch_005", "section_id": "s_005", "title": "The Answer Surfaces",         "opening_beat_id": "b_007"},
            {"chapter_id": "ch_006", "section_id": "s_006", "title": "What Remains Unknown",        "opening_beat_id": "b_008"},
        ],
        "shorts_extraction_markers": [
            {"marker_id": "sm_001", "short_id": "short_001", "opening_beat_id": "b_s01", "closing_beat_id": "b_s01"},
            {"marker_id": "sm_002", "short_id": "short_002", "opening_beat_id": "b_s02", "closing_beat_id": "b_s02"},
            {"marker_id": "sm_003", "short_id": "short_003", "opening_beat_id": "b_s03", "closing_beat_id": "b_s03"},
        ],
        "traceability_manifest": {
            "sections": [
                {"section_id": "s_001", "narrative_purpose": "Open the question.", "claim_ids_used": ["clm_001"], "beat_count": 1},
            ]
        },
        "script_integrity": {
            "cta_count": 2,
            "chapter_count": 6,
            "shorts_count": 3,
            "mystery_first_respected": True,
            "all_reveals_placed": True,
            "aggressive_cta_language": False,
            "notes": [],
        },
    }


def _make_narrative_package() -> dict:
    return {
        "episode_metadata": {
            "topic": "The Antikythera Mechanism",
            "archetype": "default_six_section",
            "central_question": "Who built the Antikythera Mechanism?",
            "central_answer": "Likely Hipparchus or his school.",
            "episode_id": "ep_001",
            "narrative_model": "narrative_architect_v1",
        },
        "episode_structure": {
            "archetype": "default_six_section",
            "sections": [
                {"section_id": "s_001", "title": "Hook",          "narrative_purpose": "Open the question.", "entity_count": 1, "key_claims": ["clm_001"], "open_loops_opened": [], "open_loops_closed": [], "viewer_tension_level": "high"},
                {"section_id": "s_002", "title": "Context",       "narrative_purpose": "Establish setting.", "entity_count": 3, "key_claims": ["clm_001"], "open_loops_opened": [], "open_loops_closed": [], "viewer_tension_level": "medium"},
                {"section_id": "s_003", "title": "Rising Mystery","narrative_purpose": "First strangeness.","entity_count": 2, "key_claims": ["clm_001"], "open_loops_opened": [], "open_loops_closed": [], "viewer_tension_level": "medium"},
                {"section_id": "s_004", "title": "Escalation",    "narrative_purpose": "Deepen mystery.",   "entity_count": 3, "key_claims": ["clm_001"], "open_loops_opened": [], "open_loops_closed": [], "viewer_tension_level": "high"},
                {"section_id": "s_005", "title": "Revelation",    "narrative_purpose": "Land the reveal.",  "entity_count": 2, "key_claims": ["clm_001"], "open_loops_opened": [], "open_loops_closed": [], "viewer_tension_level": "high"},
                {"section_id": "s_006", "title": "Resolution",    "narrative_purpose": "Elevate mystery.",  "entity_count": 1, "key_claims": ["clm_001"], "open_loops_opened": [], "open_loops_closed": [], "viewer_tension_level": "low"},
            ],
        },
        "narrative_arc": {
            "setup": "Establish the wreck and the object's strangeness.",
            "escalation": "Each research line deepens the impossibility.",
            "revelation": "The mechanism maps the entire known cosmos.",
            "resolution": "We still don't know how they knew.",
        },
        "reveal_architecture": {
            "reveal_map": [{"reveal_id": "rev_001", "section_id": "s_005", "beat_position": 0, "claim_id": "clm_001", "reveal_type": "central", "impact_justification": "Answers the central question.", "adjacent_reveal_exception": None}],
            "viewer_question_state": [{"after_section_id": "s_001", "question": "What is this object?"}],
            "open_question_tracker": [],
        },
        "shorts_selection": [
            {"short_id": "short_001", "title": "The Gears", "source_claim_id": "clm_001", "visual_anchor_signal": "Bronze gears", "spoiler_policy": "no_central_answer", "narrative_arc": "Discovery to wonder.", "text_overlay_plan": {"opening_hook": {"beat_position": 0, "narrative_intent": "Create curiosity."}, "key_information": [{"beat_position": 1, "narrative_intent": "State key fact."}], "payoff": {"beat_position": 2, "narrative_intent": "Land wonder."}}},
            {"short_id": "short_002", "title": "The Wreck", "source_claim_id": "clm_001", "visual_anchor_signal": "Shipwreck site", "spoiler_policy": "no_central_answer", "narrative_arc": "From discovery to mystery.", "text_overlay_plan": {"opening_hook": {"beat_position": 0, "narrative_intent": "Hook on the wreck."}, "key_information": [], "payoff": {"beat_position": 1, "narrative_intent": "Elevate mystery."}}},
            {"short_id": "short_003", "title": "The Date",  "source_claim_id": "clm_001", "visual_anchor_signal": "Timeline graphic", "spoiler_policy": "no_central_answer", "narrative_arc": "From date to implications.", "text_overlay_plan": {"opening_hook": {"beat_position": 0, "narrative_intent": "Shock with the date."}, "key_information": [], "payoff": {"beat_position": 1, "narrative_intent": "Why it matters."}}},
        ],
        "narrative_signals": [],
        "differentiation_record": {"ep_hypothesis": "The mechanism's social role.", "authority_tier": "refinement", "na_angle": "Focus on who used it.", "adjustment_rationale": "Research supports this.", "replacement_rationale": None},
        "narrative_integrity_log": [],
        "traceability": {"total_claims_used": 1, "total_reveals": 1, "total_open_questions": 0, "research_coverage_warning": False, "claim_ids_used": ["clm_001"]},
    }


NP = _make_narrative_package()


def _run_se(narrative_package=None, run_agent_return=None, output_dir=None):
    """Run run_script_engine with mocked run_agent."""
    if narrative_package is None:
        narrative_package = NP
    if run_agent_return is None:
        run_agent_return = _make_script_package()
    with patch("hypogeum.agents.script_engine.run_agent", return_value=run_agent_return):
        return run_script_engine(narrative_package, cfg, EPISODE_ID, output_dir)


# ---------------------------------------------------------------------------
# TestSuccessfulScript
# ---------------------------------------------------------------------------

class TestSuccessfulScript:
    def test_returns_script_package_dict(self):
        result = _run_se()
        assert isinstance(result, dict)

    def test_episode_metadata_present(self):
        result = _run_se()
        assert "episode_metadata" in result

    def test_sections_present(self):
        result = _run_se()
        assert "sections" in result

    def test_shorts_scripts_present(self):
        result = _run_se()
        assert "shorts_scripts" in result

    def test_chapter_markers_present(self):
        result = _run_se()
        assert "chapter_markers" in result

    def test_shorts_extraction_markers_present(self):
        result = _run_se()
        assert "shorts_extraction_markers" in result

    def test_script_integrity_present(self):
        result = _run_se()
        assert "script_integrity" in result

    def test_traceability_manifest_present(self):
        result = _run_se()
        assert "traceability_manifest" in result

    def test_run_agent_return_passed_through(self):
        sp = _make_script_package()
        result = _run_se(run_agent_return=sp)
        assert result is sp


# ---------------------------------------------------------------------------
# TestCTAGeneration
# ---------------------------------------------------------------------------

class TestCTAGeneration:
    def test_script_integrity_reports_two_ctas(self):
        result = _run_se()
        assert result["script_integrity"]["cta_count"] == 2

    def test_cta_beats_exist_in_sections(self):
        result = _run_se()
        cta_beats = [
            beat
            for section in result["sections"]
            for beat in section["narration_beats"]
            if beat.get("beat_type") == "cta"
        ]
        assert len(cta_beats) == 2

    def test_subscribe_cta_present(self):
        result = _run_se()
        cta_texts = [
            beat["text"]
            for section in result["sections"]
            for beat in section["narration_beats"]
            if beat.get("beat_type") == "cta"
        ]
        # At least one CTA references subscribing
        assert any("subscrib" in t.lower() for t in cta_texts)

    def test_comment_cta_present(self):
        result = _run_se()
        cta_texts = [
            beat["text"]
            for section in result["sections"]
            for beat in section["narration_beats"]
            if beat.get("beat_type") == "cta"
        ]
        # At least one CTA references comments
        assert any("comment" in t.lower() for t in cta_texts)

    def test_no_aggressive_cta_language(self):
        result = _run_se()
        assert result["script_integrity"]["aggressive_cta_language"] is False

    def test_aggressive_cta_language_flag_is_false(self):
        """Schema field exists and is explicitly False — not just falsy."""
        sp = _make_script_package()
        sp["script_integrity"]["aggressive_cta_language"] = False
        result = _run_se(run_agent_return=sp)
        assert result["script_integrity"]["aggressive_cta_language"] is False

    def test_shorts_contain_no_cta_beats(self):
        result = _run_se()
        for short in result["shorts_scripts"]:
            for beat in short["narration_beats"]:
                assert beat.get("beat_type") != "cta"


# ---------------------------------------------------------------------------
# TestChapterGeneration
# ---------------------------------------------------------------------------

class TestChapterGeneration:
    def test_chapter_markers_is_list(self):
        result = _run_se()
        assert isinstance(result["chapter_markers"], list)

    def test_chapter_count_matches_sections(self):
        result = _run_se()
        assert len(result["chapter_markers"]) == len(result["sections"])

    def test_each_chapter_has_chapter_id(self):
        result = _run_se()
        for ch in result["chapter_markers"]:
            assert "chapter_id" in ch
            assert isinstance(ch["chapter_id"], str)

    def test_each_chapter_has_section_id(self):
        result = _run_se()
        for ch in result["chapter_markers"]:
            assert "section_id" in ch

    def test_each_chapter_has_title(self):
        result = _run_se()
        for ch in result["chapter_markers"]:
            assert "title" in ch
            assert len(ch["title"]) > 0

    def test_each_chapter_has_opening_beat_id(self):
        result = _run_se()
        for ch in result["chapter_markers"]:
            assert "opening_beat_id" in ch

    def test_chapter_titles_are_unique(self):
        result = _run_se()
        titles = [ch["title"] for ch in result["chapter_markers"]]
        assert len(titles) == len(set(titles))

    def test_chapter_section_ids_match_sections(self):
        result = _run_se()
        section_ids = {s["section_id"] for s in result["sections"]}
        for ch in result["chapter_markers"]:
            assert ch["section_id"] in section_ids

    def test_script_integrity_chapter_count(self):
        result = _run_se()
        assert result["script_integrity"]["chapter_count"] == len(result["chapter_markers"])


# ---------------------------------------------------------------------------
# TestShortsMarkers
# ---------------------------------------------------------------------------

class TestShortsMarkers:
    def test_exactly_three_extraction_markers(self):
        result = _run_se()
        assert len(result["shorts_extraction_markers"]) == 3

    def test_each_marker_has_marker_id(self):
        result = _run_se()
        for m in result["shorts_extraction_markers"]:
            assert "marker_id" in m

    def test_each_marker_has_short_id(self):
        result = _run_se()
        for m in result["shorts_extraction_markers"]:
            assert "short_id" in m

    def test_each_marker_has_opening_beat_id(self):
        result = _run_se()
        for m in result["shorts_extraction_markers"]:
            assert "opening_beat_id" in m

    def test_each_marker_has_closing_beat_id(self):
        result = _run_se()
        for m in result["shorts_extraction_markers"]:
            assert "closing_beat_id" in m

    def test_marker_short_ids_match_shorts_scripts(self):
        result = _run_se()
        short_ids = {s["short_id"] for s in result["shorts_scripts"]}
        for m in result["shorts_extraction_markers"]:
            assert m["short_id"] in short_ids

    def test_marker_ids_are_unique(self):
        result = _run_se()
        ids = [m["marker_id"] for m in result["shorts_extraction_markers"]]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# TestShortsScripts
# ---------------------------------------------------------------------------

class TestShortsScripts:
    def test_exactly_three_shorts_scripts(self):
        result = _run_se()
        assert len(result["shorts_scripts"]) == 3

    def test_each_short_has_short_id(self):
        result = _run_se()
        for short in result["shorts_scripts"]:
            assert "short_id" in short

    def test_each_short_has_narration_beats(self):
        result = _run_se()
        for short in result["shorts_scripts"]:
            assert "narration_beats" in short
            assert len(short["narration_beats"]) >= 1

    def test_each_short_has_text_overlays(self):
        result = _run_se()
        for short in result["shorts_scripts"]:
            assert "text_overlays" in short

    def test_text_overlay_fields(self):
        result = _run_se()
        for short in result["shorts_scripts"]:
            for ov in short["text_overlays"]:
                assert "overlay_id" in ov
                assert "overlay_type" in ov
                assert "text" in ov
                assert "beat_reference" in ov
                assert "display_duration_seconds" in ov

    def test_script_integrity_shorts_count(self):
        result = _run_se()
        assert result["script_integrity"]["shorts_count"] == 3

    def test_short_ids_are_unique(self):
        result = _run_se()
        ids = [s["short_id"] for s in result["shorts_scripts"]]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# TestScriptPackageSchema
# ---------------------------------------------------------------------------

class TestScriptPackageSchema:
    def test_valid_package_passes_schema(self):
        pkg = _make_script_package()
        validate("script_package", pkg)  # must not raise

    def test_missing_episode_metadata_fails_schema(self):
        pkg = _make_script_package()
        del pkg["episode_metadata"]
        with pytest.raises(SchemaError):
            validate("script_package", pkg)

    def test_missing_sections_fails_schema(self):
        pkg = _make_script_package()
        del pkg["sections"]
        with pytest.raises(SchemaError):
            validate("script_package", pkg)

    def test_wrong_shorts_count_fails_schema(self):
        pkg = _make_script_package()
        pkg["shorts_scripts"] = pkg["shorts_scripts"][:2]  # only 2
        with pytest.raises(SchemaError):
            validate("script_package", pkg)

    def test_missing_narration_beats_fails_schema(self):
        pkg = _make_script_package()
        pkg["sections"][0]["narration_beats"] = []
        with pytest.raises(SchemaError):
            validate("script_package", pkg)

    def test_missing_overlay_id_fails_schema(self):
        pkg = _make_script_package()
        del pkg["shorts_scripts"][0]["text_overlays"][0]["overlay_id"]
        with pytest.raises(SchemaError):
            validate("script_package", pkg)

    def test_missing_traceability_fails_schema(self):
        pkg = _make_script_package()
        del pkg["traceability_manifest"]
        with pytest.raises(SchemaError):
            validate("script_package", pkg)

    def test_missing_script_integrity_fails_schema(self):
        pkg = _make_script_package()
        del pkg["script_integrity"]
        with pytest.raises(SchemaError):
            validate("script_package", pkg)


# ---------------------------------------------------------------------------
# TestClaudeIntegration
# ---------------------------------------------------------------------------

class TestClaudeIntegration:
    def _captured_call_kwargs(self, narrative_package=None):
        if narrative_package is None:
            narrative_package = NP
        with patch("hypogeum.agents.script_engine.run_agent",
                   return_value=_make_script_package()) as mock_ra:
            run_script_engine(narrative_package, cfg, EPISODE_ID)
        return mock_ra.call_args.kwargs

    def test_model_slug_is_sonnet(self):
        kw = self._captured_call_kwargs()
        assert kw["model_slug"] == _CLAUDE_SONNET

    def test_model_slug_contains_sonnet(self):
        assert "sonnet" in _CLAUDE_SONNET.lower()

    def test_output_schema_is_script_package(self):
        kw = self._captured_call_kwargs()
        assert kw["output_schema"] == "script_package"

    def test_max_tokens_is_16384(self):
        kw = self._captured_call_kwargs()
        assert kw["max_tokens"] == 16_384

    def test_agent_name_is_script_engine(self):
        kw = self._captured_call_kwargs()
        assert kw["agent_name"] == "script_engine"

    def test_episode_id_passed_through(self):
        kw = self._captured_call_kwargs()
        assert kw["episode_id"] == EPISODE_ID

    def test_config_passed_through(self):
        kw = self._captured_call_kwargs()
        assert kw["config"] is cfg

    def test_system_prompt_is_non_empty_string(self):
        kw = self._captured_call_kwargs()
        assert isinstance(kw["system_prompt"], str)
        assert len(kw["system_prompt"]) > 0

    def test_max_tokens_constant(self):
        assert _SE_MAX_TOKENS == 16_384

    def test_model_slug_constant(self):
        assert _CLAUDE_SONNET == "anthropic/claude-sonnet-4.6"


# ---------------------------------------------------------------------------
# TestInputPackage
# ---------------------------------------------------------------------------

class TestInputPackage:
    def _captured_input(self, narrative_package=None):
        if narrative_package is None:
            narrative_package = NP
        with patch("hypogeum.agents.script_engine.run_agent",
                   return_value=_make_script_package()) as mock_ra:
            run_script_engine(narrative_package, cfg, EPISODE_ID)
        return mock_ra.call_args.kwargs["input_package"]

    def test_input_package_is_dict(self):
        assert isinstance(self._captured_input(), dict)

    def test_task_field_is_write_script(self):
        inp = self._captured_input()
        assert inp["task"] == "write_script"

    def test_narrative_package_in_input(self):
        inp = self._captured_input()
        assert "narrative_package" in inp

    def test_narrative_package_passed_as_is(self):
        np_pkg = _make_narrative_package()
        inp = self._captured_input(narrative_package=np_pkg)
        assert inp["narrative_package"] is np_pkg

    def test_output_dir_none_propagated(self):
        with patch("hypogeum.agents.script_engine.run_agent",
                   return_value=_make_script_package()) as mock_ra:
            run_script_engine(NP, cfg, EPISODE_ID, output_dir=None)
        assert mock_ra.call_args.kwargs["output_dir"] is None

    def test_output_dir_path_propagated(self):
        p = Path("/tmp/test_output")
        with patch("hypogeum.agents.script_engine.run_agent",
                   return_value=_make_script_package()) as mock_ra:
            run_script_engine(NP, cfg, EPISODE_ID, output_dir=p)
        assert mock_ra.call_args.kwargs["output_dir"] == p


# ---------------------------------------------------------------------------
# TestExceptionPropagation
# ---------------------------------------------------------------------------

class TestExceptionPropagation:
    def test_response_parse_error_propagates(self):
        err = ResponseParseError("script_engine", "invalid_json")
        with patch("hypogeum.agents.script_engine.run_agent", side_effect=err):
            with pytest.raises(ResponseParseError):
                run_script_engine(NP, cfg, EPISODE_ID)

    def test_schema_error_propagates(self):
        err = SchemaError("script_package", "sections", "must have at least 1")
        with patch("hypogeum.agents.script_engine.run_agent", side_effect=err):
            with pytest.raises(SchemaError):
                run_script_engine(NP, cfg, EPISODE_ID)

    def test_runtime_error_propagates(self):
        with patch("hypogeum.agents.script_engine.run_agent", side_effect=RuntimeError("fail")):
            with pytest.raises(RuntimeError):
                run_script_engine(NP, cfg, EPISODE_ID)

    def test_run_agent_called_once_even_on_error(self):
        with patch("hypogeum.agents.script_engine.run_agent",
                   side_effect=RuntimeError("fail")) as mock_ra:
            with pytest.raises(RuntimeError):
                run_script_engine(NP, cfg, EPISODE_ID)
        mock_ra.assert_called_once()


# ---------------------------------------------------------------------------
# TestLogging
# ---------------------------------------------------------------------------

class TestLogging:
    def _run_and_capture_logs(self, narrative_package=None):
        if narrative_package is None:
            narrative_package = NP
        events = []

        class _FakeLogger:
            def info(self, event, **kw):
                events.append({"level": "info", "event": event, **kw})
            def warn(self, event, **kw):
                events.append({"level": "warn", "event": event, **kw})
            def error(self, event, **kw):
                events.append({"level": "error", "event": event, **kw})

        with patch("hypogeum.agents.script_engine.run_agent",
                   return_value=_make_script_package()), \
             patch("hypogeum.agents.script_engine.get_logger", return_value=_FakeLogger()):
            run_script_engine(narrative_package, cfg, EPISODE_ID)
        return events

    def test_se_script_started_logged(self):
        events = self._run_and_capture_logs()
        names = [e["event"] for e in events]
        assert "se_script_started" in names

    def test_se_script_started_is_info(self):
        events = self._run_and_capture_logs()
        ev = next(e for e in events if e["event"] == "se_script_started")
        assert ev["level"] == "info"

    def test_se_script_completed_logged(self):
        events = self._run_and_capture_logs()
        names = [e["event"] for e in events]
        assert "se_script_completed" in names

    def test_se_script_completed_is_info(self):
        events = self._run_and_capture_logs()
        ev = next(e for e in events if e["event"] == "se_script_completed")
        assert ev["level"] == "info"

    def test_cta_count_in_completed_log(self):
        events = self._run_and_capture_logs()
        ev = next(e for e in events if e["event"] == "se_script_completed")
        assert "cta_count" in ev

    def test_sections_count_in_started_log(self):
        events = self._run_and_capture_logs()
        ev = next(e for e in events if e["event"] == "se_script_started")
        assert "sections_count" in ev


# ---------------------------------------------------------------------------
# TestSystemPrompt
# ---------------------------------------------------------------------------

class TestSystemPrompt:
    def test_prompt_file_exists(self):
        assert _PROMPT_FILE.exists()

    def test_prompt_is_regular_file(self):
        assert _PROMPT_FILE.is_file()

    def test_system_prompt_is_non_empty_string(self):
        assert isinstance(SYSTEM_PROMPT, str)
        assert len(SYSTEM_PROMPT) > 0

    def test_system_prompt_contains_base_prompt_file(self):
        from hypogeum.prompt_loader import load_prompt
        assert load_prompt(_PROMPT_FILE) in SYSTEM_PROMPT

    def test_system_prompt_composed_via_compose_system_prompt(self):
        from hypogeum.prompt_loader import compose_system_prompt
        assert SYSTEM_PROMPT == compose_system_prompt(_PROMPT_FILE, _HUMANIZER_SKILL_PATH)

    def test_pinned_humanizer_skill_path_exists(self):
        assert _HUMANIZER_SKILL_PATH.exists()
        assert PINNED_HUMANIZER_SHA in str(_HUMANIZER_SKILL_PATH)

    def test_pinned_humanizer_skill_loaded_verbatim(self):
        """The composed system prompt must contain the pinned SKILL.md's
        exact on-disk bytes — no paraphrase, no summarization, no rewrite."""
        on_disk = _HUMANIZER_SKILL_PATH.read_text(encoding="utf-8")
        assert on_disk in SYSTEM_PROMPT

    def test_skill_not_duplicated_or_rewritten_in_base_prompt(self):
        """A representative, distinctive upstream heading must not be
        duplicated inside script_engine.txt's own authored content — it
        must come from the composed skill file only."""
        from hypogeum.prompt_loader import load_prompt
        base_only = load_prompt(_PROMPT_FILE)
        skill_text = _HUMANIZER_SKILL_PATH.read_text(encoding="utf-8")
        assert "### 24. Excessive Hedging" not in base_only
        assert "### 24. Excessive Hedging" in skill_text

    def test_base_prompt_bridges_to_humanizer_skill(self):
        """script_engine.txt's own content must reference the composed
        skill and instruct single-pass application, not a rewrite step."""
        from hypogeum.prompt_loader import load_prompt
        base_only = load_prompt(_PROMPT_FILE)
        assert "Humanizer" in base_only
        assert "AskUserQuestion" in base_only or "clarifying question" in base_only.lower()

    def test_prompt_mentions_cta_strategy(self):
        assert "CTA" in SYSTEM_PROMPT or "cta" in SYSTEM_PROMPT.lower()

    def test_prompt_mentions_subscribe(self):
        assert "subscrib" in SYSTEM_PROMPT.lower()

    def test_prompt_mentions_chapter(self):
        assert "chapter" in SYSTEM_PROMPT.lower()

    def test_prompt_mentions_shorts_extraction(self):
        assert "shorts_extraction" in SYSTEM_PROMPT.lower() or "extraction" in SYSTEM_PROMPT.lower()

    def test_prompt_mentions_mystery_first(self):
        assert "mystery" in SYSTEM_PROMPT.lower()

    def test_system_prompt_passed_to_run_agent(self):
        with patch("hypogeum.agents.script_engine.run_agent",
                   return_value=_make_script_package()) as mock_ra:
            run_script_engine(NP, cfg, EPISODE_ID)
        assert mock_ra.call_args.kwargs["system_prompt"] == SYSTEM_PROMPT

    def test_agent_name_constant(self):
        assert _AGENT_NAME == "script_engine"


# ---------------------------------------------------------------------------
# TestPipelineIntegration
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
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

    def _make_research_package(self):
        return {
            "research_run_metadata": {}, "research_strategy": {},
            "claims": [{"claim_id": "c1", "text": "Fact.", "confidence": 0.9}],
            "contradictions": [], "open_questions": [], "source_registry": [],
            "competitive_landscape": {}, "research_run_summary": {}, "package_integrity": {},
        }

    def test_stub_delegates_to_run_script_engine(self, tmp_path):
        sp_pkg = _make_script_package()
        with patch("hypogeum.pipeline.run_script_engine",
                   return_value=sp_pkg) as mock_se:
            result = pipeline_se_stub({}, cfg, EPISODE_ID, tmp_path)
        mock_se.assert_called_once()
        assert result is sp_pkg

    def test_stub_passes_narrative_package(self, tmp_path):
        np_pkg = _make_narrative_package()
        with patch("hypogeum.pipeline.run_script_engine",
                   return_value=_make_script_package()) as mock_se:
            pipeline_se_stub(np_pkg, cfg, EPISODE_ID, tmp_path)
        assert mock_se.call_args.kwargs["narrative_package"] is np_pkg

    def test_stub_passes_episode_id(self, tmp_path):
        with patch("hypogeum.pipeline.run_script_engine",
                   return_value=_make_script_package()) as mock_se:
            pipeline_se_stub({}, cfg, EPISODE_ID, tmp_path)
        assert mock_se.call_args.kwargs["episode_id"] == EPISODE_ID

    def test_success_continues_to_vpd_stub(self):
        """Successful SE → pipeline proceeds through all remaining agents to completion."""
        from hypogeum.pipeline import run_pipeline
        sc, brief = self._ep_output()

        with patch("hypogeum.pipeline.run_executive_producer", return_value=(sc, brief)), \
             patch("hypogeum.pipeline.validate_narration_completeness"), \
             patch("hypogeum.pipeline.run_research_director",
                   return_value=(self._make_research_package(), None)), \
             patch("hypogeum.pipeline.run_narrative_architect",
                   return_value=_make_narrative_package()), \
             patch("hypogeum.pipeline.run_script_engine",
                   return_value=_make_script_package()), \
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
        err = SchemaError("script_package", "sections", "must have at least 1")
        with patch("hypogeum.pipeline.run_script_engine", side_effect=err):
            with pytest.raises(SchemaError) as exc_info:
                pipeline_se_stub({}, cfg, EPISODE_ID, tmp_path)
        assert exc_info.value is err

    def test_stub_passes_output_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            with patch("hypogeum.pipeline.run_script_engine",
                       return_value=_make_script_package()) as mock_se:
                pipeline_se_stub(
                    narrative_package=NP, config=cfg,
                    episode_id=EPISODE_ID, output_dir=p,
                )
            assert mock_se.call_args.kwargs["output_dir"] == p


# ---------------------------------------------------------------------------
# Short-native viral-shape gate (2026-09-04)
# ---------------------------------------------------------------------------

from hypogeum.agents.script_engine import (  # noqa: E402
    ScriptViralityError, _DATE_OPENER_RE, _short_native_viral_findings,
)


_GOOD_STINGER = "And two thousand years later nobody can fully explain how it happened."


def _sn_pkg(first_line, *, paid=True, stinger=True, spine="why?", last_line=_GOOD_STINGER):
    return {
        "sections": [{"section_id": "s1", "title": "x",
                      "narration_beats": [{"beat_id": "b1", "text": first_line, "beat_type": "hook"},
                                          {"beat_id": "bt", "text": "And then the small worry melted away.", "beat_type": "reveal"},
                                          {"beat_id": "b2", "text": last_line, "beat_type": "resolution"}]}],
        "shorts_scripts": [], "chapter_markers": [], "shorts_extraction_markers": [],
        "traceability_manifest": {},
        "script_integrity": {"cta_count": 0, "short_native": {
            "hook_opener_type": "contradiction", "spine_question": spine,
            "spine_paid_off": paid, "has_stinger": stinger,
            "story_problem": "the room felt too big and dark", "story_turn": "a firefly showed the way"}},
        "episode_metadata": {},
    }


class TestDateOpenerRegex:
    @pytest.mark.parametrize("line", [
        "In 2006, archaeologists opened the tomb.",
        "2006: the year everything changed.",
        "By 1974 the site was buried.",
        "On March 3, 1912 the ship sailed.",
        "Circa 1500 BC, the temple stood.",
    ])
    def test_flags_date_openers(self, line):
        assert _DATE_OPENER_RE.match(line)

    @pytest.mark.parametrize("line", [
        "A sealed tomb with everything for a burial — except a body.",
        "You have been told the Colosseum held 70,000 victims.",
        "Sixty winches lifted cages through the arena floor.",
    ])
    def test_passes_strong_hooks(self, line):
        assert not _DATE_OPENER_RE.match(line)


class TestShortNativeViralFindings:
    def test_clean_investigative_script_has_no_findings(self):
        pkg = _sn_pkg("A sealed tomb with no body inside.")
        assert _short_native_viral_findings(pkg, investigative=True) == []

    def test_date_opener_flagged_for_investigative(self):
        pkg = _sn_pkg("In 2006, archaeologists opened the tomb.")
        out = _short_native_viral_findings(pkg, investigative=True)
        assert any("date/year" in p for p in out)

    def test_date_opener_ignored_for_gentle(self):
        pkg = _sn_pkg("In 2006, archaeologists opened the tomb.")
        assert _short_native_viral_findings(pkg, investigative=False) == []

    def test_unpaid_spine_flagged(self):
        pkg = _sn_pkg("A sealed tomb with no body.", paid=False)
        assert any("spine_paid_off" in p for p in _short_native_viral_findings(pkg, investigative=True))

    def test_missing_stinger_flagged(self):
        pkg = _sn_pkg("A sealed tomb with no body.", stinger=False)
        assert any("has_stinger" in p for p in _short_native_viral_findings(pkg, investigative=True))

    def test_missing_short_native_object_flagged(self):
        pkg = _sn_pkg("A sealed tomb with no body.")
        del pkg["script_integrity"]["short_native"]
        assert _short_native_viral_findings(pkg, investigative=True)


class TestShortNativeGateInRunScriptEngine:
    def _cfg_short_native(self):
        c = make_config()
        c.SHORT_NATIVE_MODE = True
        return c

    def test_retry_then_pass(self, monkeypatch):
        monkeypatch.setattr("hypogeum.agents.script_engine.channel_brief_for",
                            lambda cid: {"tone": "investigative, evidence-based"})
        monkeypatch.setattr("hypogeum.agents.script_engine._enforce_short_native_budget",
                            lambda *a, **k: None)
        bad = _sn_pkg("In 2006, they opened it.", paid=False)
        good = _sn_pkg("A sealed tomb, no body inside.")
        calls = iter([bad, good])
        monkeypatch.setattr("hypogeum.agents.script_engine.run_agent",
                            lambda **kw: next(calls))
        out = run_script_engine(NP, self._cfg_short_native(), EPISODE_ID, None)
        assert _short_native_viral_findings(out, investigative=True) == []

    def test_retry_then_fail_raises(self, monkeypatch):
        monkeypatch.setattr("hypogeum.agents.script_engine.channel_brief_for",
                            lambda cid: {"tone": "investigative, evidence-based"})
        monkeypatch.setattr("hypogeum.agents.script_engine._enforce_short_native_budget",
                            lambda *a, **k: None)
        bad = _sn_pkg("In 2006, they opened it.", paid=False)
        monkeypatch.setattr("hypogeum.agents.script_engine.run_agent", lambda **kw: bad)
        with pytest.raises(ScriptViralityError):
            run_script_engine(NP, self._cfg_short_native(), EPISODE_ID, None)


class TestShortNativeBeatBudgetEnforcement:
    """_enforce_short_native_budget must hard-cap a beat even when its single
    leading sentence alone exceeds max_beat_words — the miss that forced
    Assembly's coverage-fill path on ~1s deficits (2026-09: twinkle b_013)."""

    def _cfg(self):
        return make_config()

    def test_single_over_budget_sentence_is_hard_cut(self):
        from hypogeum.agents.script_engine import (
            _enforce_short_native_budget, _short_native_beat_budget,
        )
        from hypogeum.logger import get_logger

        cfg = self._cfg()
        _bs, max_words, _wps = _short_native_beat_budget(cfg, "twinkle_mini_tales")
        long_sentence = " ".join(["word"] * (max_words + 6)) + "."
        pkg = {"sections": [{"section_id": "s", "title": "t", "narration_beats": [
            {"beat_id": "b1", "text": long_sentence},
        ]}]}
        _enforce_short_native_budget(pkg, cfg, get_logger(agent="t"),
                                     channel_id="twinkle_mini_tales")
        out = pkg["sections"][0]["narration_beats"][0]["text"]
        assert len(out.split()) <= max_words

    def test_multi_sentence_beat_keeps_leading_whole_sentences(self):
        from hypogeum.agents.script_engine import (
            _enforce_short_native_budget, _short_native_beat_budget,
        )
        from hypogeum.logger import get_logger

        cfg = self._cfg()
        _bs, max_words, _wps = _short_native_beat_budget(cfg, "hypogeum")
        pkg = {"sections": [{"section_id": "s", "title": "t", "narration_beats": [
            {"beat_id": "b1", "text": "Short first line. " + " ".join(["x"] * (max_words + 10)) + "."},
        ]}]}
        _enforce_short_native_budget(pkg, cfg, get_logger(agent="t"), channel_id="hypogeum")
        out = pkg["sections"][0]["narration_beats"][0]["text"]
        assert len(out.split()) <= max_words
        assert out.startswith("Short first line")
