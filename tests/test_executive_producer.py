"""
tests/test_executive_producer.py — Unit tests for hypogeum/agents/executive_producer.py

All run_agent() calls are mocked. No real Claude calls. No real filesystem writes.
Pipeline integration tests mock run_executive_producer and test pipeline.py delegation.

Test coverage:
  TestPassPath              — PASS decision: two run_agent calls, both packages returned
  TestWarnPath              — WARN decision: two calls, both packages returned
  TestRejectPath            — REJECT decision: one call, empty brief, no second call
  TestFirstCallArguments    — all kwargs validated for the scorecard call
  TestSecondCallArguments   — all kwargs validated for the brief call (PASS path)
  TestOutputDirPropagation  — output_dir=None and real path propagated correctly
  TestExceptionPropagation  — exceptions from both calls propagate unchanged
  TestSystemPrompt          — prompt file exists, non-empty, loaded correctly
  TestPipelineIntegration   — _run_executive_producer stub delegates to run_executive_producer
  TestDecisionGating        — decision field controls whether second call is made
  TestAgentIdentity         — agent_name and model_slug correct for all calls
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agent import ResponseParseError
from hypogeum.agents.executive_producer import (
    SYSTEM_PROMPT,
    _AGENT_NAME,
    _MAX_TOKENS,
    _MODEL_SLUG,
    _PROMPT_FILE,
    run_executive_producer,
)
from hypogeum.config import Config
from hypogeum.pipeline import (
    PipelineRejectedError,
    _run_executive_producer as pipeline_ep_stub,
)
from hypogeum.schemas import SchemaError


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
    """Build a Config with minimal valid environment."""
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


def _make_scorecard(decision: str = "PASS", score: float = 8.0) -> dict:
    """Return a minimal valid validation_scorecard dict."""
    return {
        "scorecard_id": "sc_ep001_001",
        "topic_id": "antikythera_mechanism",
        "dimensions": {
            "audience_demand": score,
            "curiosity_potential": score,
            "mystery_potential": score,
            "visual_potential": score,
            "differentiation_opportunity": score,
            "shorts_potential": score,
        },
        "overall_score": score,
        "decision": decision,
        "reasoning": {
            "audience_demand": "High search volume.",
            "curiosity_potential": "Unresolved origin questions.",
            "mystery_potential": "No known predecessor.",
            "visual_potential": "Rich artifact imagery.",
            "differentiation_opportunity": "Novel angle available.",
            "shorts_potential": "Clear punchy moments.",
            "decision_summary": f"Topic {decision.lower()}ed.",
        },
        "issued_at": "2026-06-01T10:00:00Z",
        "issued_by": "executive_producer",
    }


def _make_brief() -> dict:
    """Return a minimal valid topic_brief dict."""
    return {
        "topic_id": "antikythera_mechanism",
        "episode_title": "The Antikythera Mechanism: Ancient Computer",
        "topic_category": "ancient_mystery",
        "logline": "A 2,000-year-old device that shouldn't exist.",
        "audience_demand_signal": "High search volume and viral content history.",
        "curiosity_hook": "Who built it and how was the knowledge lost?",
        "mystery_anchor": "No known predecessor or successor exists.",
        "differentiation_hypothesis": "No channel has explored the astronomical predictions.",
        "research_depth_flags": {"differentiation_opportunity": "deep"},
        "shorts_directions": [
            "The missing instruction manual theory",
            "The Roman shipwreck discovery",
        ],
        "scorecard_reference": "sc_ep001_001",
        "warn_concerns": "",
        "issued_at": "2026-06-01T10:00:00Z",
        "issued_by": "executive_producer",
    }


TOPIC = "The Antikythera Mechanism"
EPISODE_ID = "ep_001"


# ---------------------------------------------------------------------------
# TestPassPath
# ---------------------------------------------------------------------------

class TestPassPath:
    """PASS decision — two run_agent calls, both packages returned."""

    def test_returns_tuple_of_two_dicts(self):
        scorecard = _make_scorecard("PASS")
        brief = _make_brief()
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   side_effect=[scorecard, brief]) as mock_ra:
            result = run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_returns_scorecard_as_first_element(self):
        scorecard = _make_scorecard("PASS")
        brief = _make_brief()
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   side_effect=[scorecard, brief]):
            sc, _ = run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert sc is scorecard

    def test_returns_brief_as_second_element(self):
        scorecard = _make_scorecard("PASS")
        brief = _make_brief()
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   side_effect=[scorecard, brief]):
            _, br = run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert br is brief

    def test_makes_exactly_two_run_agent_calls(self):
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   side_effect=[_make_scorecard("PASS"), _make_brief()]) as mock_ra:
            run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert mock_ra.call_count == 2

    def test_brief_is_non_empty_dict(self):
        cfg = make_config()
        brief = _make_brief()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   side_effect=[_make_scorecard("PASS"), brief]):
            _, br = run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert isinstance(br, dict)
        assert len(br) > 0

    def test_score_at_pass_threshold(self):
        scorecard = _make_scorecard("PASS", score=7.0)
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   side_effect=[scorecard, _make_brief()]) as mock_ra:
            run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert mock_ra.call_count == 2


# ---------------------------------------------------------------------------
# TestWarnPath
# ---------------------------------------------------------------------------

class TestWarnPath:
    """WARN decision — two run_agent calls, both packages returned."""

    def test_returns_tuple_of_two_dicts(self):
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   side_effect=[_make_scorecard("WARN", 6.5), _make_brief()]):
            result = run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert isinstance(result, tuple) and len(result) == 2

    def test_returns_warn_scorecard(self):
        scorecard = _make_scorecard("WARN", 6.5)
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   side_effect=[scorecard, _make_brief()]):
            sc, _ = run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert sc["decision"] == "WARN"

    def test_returns_brief_on_warn(self):
        cfg = make_config()
        brief = _make_brief()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   side_effect=[_make_scorecard("WARN", 6.5), brief]):
            _, br = run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert br is brief

    def test_makes_exactly_two_calls_on_warn(self):
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   side_effect=[_make_scorecard("WARN", 6.5), _make_brief()]) as mock_ra:
            run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert mock_ra.call_count == 2

    def test_score_at_warn_lower_boundary(self):
        scorecard = _make_scorecard("WARN", 6.0)
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   side_effect=[scorecard, _make_brief()]) as mock_ra:
            run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert mock_ra.call_count == 2


# ---------------------------------------------------------------------------
# TestRejectPath
# ---------------------------------------------------------------------------

class TestRejectPath:
    """REJECT decision — one call, empty brief, no second call."""

    def test_returns_tuple(self):
        scorecard = _make_scorecard("REJECT", 5.0)
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   return_value=scorecard):
            result = run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert isinstance(result, tuple) and len(result) == 2

    def test_returns_scorecard_as_first_element(self):
        scorecard = _make_scorecard("REJECT", 5.0)
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   return_value=scorecard):
            sc, _ = run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert sc is scorecard

    def test_brief_is_empty_dict(self):
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   return_value=_make_scorecard("REJECT", 5.0)):
            _, br = run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert br == {}

    def test_brief_is_dict_not_none(self):
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   return_value=_make_scorecard("REJECT", 5.0)):
            _, br = run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert br is not None
        assert isinstance(br, dict)

    def test_only_one_run_agent_call(self):
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   return_value=_make_scorecard("REJECT", 5.0)) as mock_ra:
            run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert mock_ra.call_count == 1

    def test_score_just_below_warn_boundary(self):
        scorecard = _make_scorecard("REJECT", 5.9)
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   return_value=scorecard) as mock_ra:
            run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert mock_ra.call_count == 1


# ---------------------------------------------------------------------------
# TestFirstCallArguments
# ---------------------------------------------------------------------------

class TestFirstCallArguments:
    """Validate all kwargs sent to run_agent for the scorecard call."""

    def _capture_calls(self, decision: str = "PASS"):
        cfg = make_config()
        calls = []

        def capture(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return _make_scorecard(decision)
            return _make_brief()

        with patch("hypogeum.agents.executive_producer.run_agent", side_effect=capture):
            run_executive_producer(TOPIC, cfg, EPISODE_ID)
        return calls, cfg

    def test_agent_name_is_executive_producer(self):
        calls, _ = self._capture_calls()
        assert calls[0]["agent_name"] == "executive_producer"

    def test_model_slug_is_config_ep_model(self):
        calls, cfg = self._capture_calls()
        assert calls[0]["model_slug"] == cfg.EP_MODEL

    def test_input_package_task_field(self):
        calls, _ = self._capture_calls()
        assert calls[0]["input_package"]["task"] == "validate_topic"

    def test_input_package_topic_field(self):
        calls, _ = self._capture_calls()
        assert calls[0]["input_package"]["topic"] == TOPIC

    def test_output_schema_is_validation_scorecard(self):
        calls, _ = self._capture_calls()
        assert calls[0]["output_schema"] == "validation_scorecard"

    def test_max_tokens_is_4096(self):
        calls, _ = self._capture_calls()
        assert calls[0]["max_tokens"] == 4096

    def test_episode_id_passed_through(self):
        calls, _ = self._capture_calls()
        assert calls[0]["episode_id"] == EPISODE_ID

    def test_system_prompt_is_non_empty_string(self):
        calls, _ = self._capture_calls()
        sp = calls[0]["system_prompt"]
        assert isinstance(sp, str) and len(sp) > 0


# ---------------------------------------------------------------------------
# TestSecondCallArguments
# ---------------------------------------------------------------------------

class TestSecondCallArguments:
    """Validate all kwargs sent to run_agent for the topic_brief call (PASS path)."""

    def _capture_calls(self):
        cfg = make_config()
        scorecard = _make_scorecard("PASS")
        calls = []

        def capture(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return scorecard
            return _make_brief()

        with patch("hypogeum.agents.executive_producer.run_agent", side_effect=capture):
            run_executive_producer(TOPIC, cfg, EPISODE_ID)
        return calls, cfg

    def test_agent_name_is_executive_producer(self):
        calls, _ = self._capture_calls()
        assert calls[1]["agent_name"] == "executive_producer"

    def test_model_slug_is_config_ep_model(self):
        calls, cfg = self._capture_calls()
        assert calls[1]["model_slug"] == cfg.EP_MODEL

    def test_input_package_task_field(self):
        calls, _ = self._capture_calls()
        assert calls[1]["input_package"]["task"] == "create_topic_brief"

    def test_input_package_includes_topic(self):
        calls, _ = self._capture_calls()
        assert calls[1]["input_package"]["topic"] == TOPIC

    def test_input_package_includes_scorecard(self):
        calls, _ = self._capture_calls()
        assert calls[1]["input_package"]["scorecard"]["decision"] == "PASS"

    def test_output_schema_is_topic_brief(self):
        calls, _ = self._capture_calls()
        assert calls[1]["output_schema"] == "topic_brief"

    def test_max_tokens_is_4096(self):
        calls, _ = self._capture_calls()
        assert calls[1]["max_tokens"] == 4096

    def test_episode_id_passed_through(self):
        calls, _ = self._capture_calls()
        assert calls[1]["episode_id"] == EPISODE_ID


# ---------------------------------------------------------------------------
# TestOutputDirPropagation
# ---------------------------------------------------------------------------

class TestOutputDirPropagation:
    """output_dir is forwarded to both run_agent calls."""

    def test_none_propagated_to_scorecard_call(self):
        cfg = make_config()
        calls = []

        def capture(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return _make_scorecard("PASS")
            return _make_brief()

        with patch("hypogeum.agents.executive_producer.run_agent", side_effect=capture):
            run_executive_producer(TOPIC, cfg, EPISODE_ID, output_dir=None)
        assert calls[0]["output_dir"] is None

    def test_path_propagated_to_scorecard_call(self):
        cfg = make_config()
        calls = []

        def capture(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return _make_scorecard("PASS")
            return _make_brief()

        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            with patch("hypogeum.agents.executive_producer.run_agent", side_effect=capture):
                run_executive_producer(TOPIC, cfg, EPISODE_ID, output_dir=p)
        assert calls[0]["output_dir"] == p

    def test_path_propagated_to_brief_call(self):
        cfg = make_config()
        calls = []

        def capture(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return _make_scorecard("PASS")
            return _make_brief()

        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            with patch("hypogeum.agents.executive_producer.run_agent", side_effect=capture):
                run_executive_producer(TOPIC, cfg, EPISODE_ID, output_dir=p)
        assert calls[1]["output_dir"] == p

    def test_none_propagated_to_brief_call(self):
        cfg = make_config()
        calls = []

        def capture(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return _make_scorecard("PASS")
            return _make_brief()

        with patch("hypogeum.agents.executive_producer.run_agent", side_effect=capture):
            run_executive_producer(TOPIC, cfg, EPISODE_ID, output_dir=None)
        assert calls[1]["output_dir"] is None


# ---------------------------------------------------------------------------
# TestExceptionPropagation
# ---------------------------------------------------------------------------

class TestExceptionPropagation:
    """Exceptions from run_agent propagate unchanged."""

    def test_response_parse_error_from_call_1_propagates(self):
        cfg = make_config()
        err = ResponseParseError("executive_producer", "invalid_json")
        with patch("hypogeum.agents.executive_producer.run_agent", side_effect=err):
            with pytest.raises(ResponseParseError) as exc_info:
                run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert exc_info.value is err

    def test_schema_error_from_call_1_propagates(self):
        cfg = make_config()
        err = SchemaError("validation_scorecard", "decision", "missing required field")
        with patch("hypogeum.agents.executive_producer.run_agent", side_effect=err):
            with pytest.raises(SchemaError) as exc_info:
                run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert exc_info.value is err

    def test_response_parse_error_from_call_2_propagates(self):
        cfg = make_config()
        err = ResponseParseError("executive_producer", "not_a_dict")

        def raise_on_second(**kwargs):
            if kwargs.get("output_schema") == "topic_brief":
                raise err
            return _make_scorecard("PASS")

        with patch("hypogeum.agents.executive_producer.run_agent",
                   side_effect=raise_on_second):
            with pytest.raises(ResponseParseError) as exc_info:
                run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert exc_info.value is err

    def test_schema_error_from_call_2_propagates(self):
        cfg = make_config()
        err = SchemaError("topic_brief", "logline", "missing required field")

        def raise_on_second(**kwargs):
            if kwargs.get("output_schema") == "topic_brief":
                raise err
            return _make_scorecard("PASS")

        with patch("hypogeum.agents.executive_producer.run_agent",
                   side_effect=raise_on_second):
            with pytest.raises(SchemaError) as exc_info:
                run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert exc_info.value is err

    def test_call_1_error_prevents_second_call(self):
        cfg = make_config()
        err = ResponseParseError("executive_producer", "invalid_json")
        with patch("hypogeum.agents.executive_producer.run_agent",
                   side_effect=err) as mock_ra:
            with pytest.raises(ResponseParseError):
                run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert mock_ra.call_count == 1

    def test_runtime_error_from_call_1_propagates(self):
        cfg = make_config()
        err = RuntimeError("openrouter hard failure")
        with patch("hypogeum.agents.executive_producer.run_agent", side_effect=err):
            with pytest.raises(RuntimeError) as exc_info:
                run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert exc_info.value is err


# ---------------------------------------------------------------------------
# TestSystemPrompt
# ---------------------------------------------------------------------------

class TestSystemPrompt:
    """System prompt file and module-level constant."""

    def test_prompt_file_exists(self):
        assert _PROMPT_FILE.exists(), f"Prompt file not found: {_PROMPT_FILE}"

    def test_prompt_file_is_regular_file(self):
        assert _PROMPT_FILE.is_file()

    def test_system_prompt_is_string(self):
        assert isinstance(SYSTEM_PROMPT, str)

    def test_system_prompt_is_non_empty(self):
        assert len(SYSTEM_PROMPT.strip()) > 0

    def test_system_prompt_matches_file_contents(self):
        file_content = _PROMPT_FILE.read_text(encoding="utf-8")
        assert SYSTEM_PROMPT == file_content

    def test_system_prompt_passed_to_first_call(self):
        cfg = make_config()
        captured = []

        def capture(**kwargs):
            captured.append(kwargs.get("system_prompt"))
            if len(captured) == 1:
                return _make_scorecard("PASS")
            return _make_brief()

        with patch("hypogeum.agents.executive_producer.run_agent", side_effect=capture):
            run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert captured[0] == SYSTEM_PROMPT

    def test_system_prompt_passed_to_second_call(self):
        cfg = make_config()
        captured = []

        def capture(**kwargs):
            captured.append(kwargs.get("system_prompt"))
            if len(captured) == 1:
                return _make_scorecard("PASS")
            return _make_brief()

        with patch("hypogeum.agents.executive_producer.run_agent", side_effect=capture):
            run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert captured[1] == SYSTEM_PROMPT

    def test_prompt_contains_mission_section(self):
        assert "MISSION" in SYSTEM_PROMPT

    def test_prompt_contains_output_instructions(self):
        assert "validate_topic" in SYSTEM_PROMPT
        assert "create_topic_brief" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# TestDecisionGating
# ---------------------------------------------------------------------------

class TestDecisionGating:
    """decision field controls whether the second run_agent call is made."""

    def test_pass_triggers_second_call(self):
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   side_effect=[_make_scorecard("PASS"), _make_brief()]) as mock_ra:
            run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert mock_ra.call_count == 2

    def test_warn_triggers_second_call(self):
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   side_effect=[_make_scorecard("WARN", 6.5), _make_brief()]) as mock_ra:
            run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert mock_ra.call_count == 2

    def test_reject_suppresses_second_call(self):
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   return_value=_make_scorecard("REJECT", 5.0)) as mock_ra:
            run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert mock_ra.call_count == 1

    def test_second_call_uses_scorecard_from_first_call(self):
        cfg = make_config()
        scorecard = _make_scorecard("PASS")
        captured_input = []

        def capture(**kwargs):
            captured_input.append(kwargs.get("input_package", {}))
            if len(captured_input) == 1:
                return scorecard
            return _make_brief()

        with patch("hypogeum.agents.executive_producer.run_agent", side_effect=capture):
            run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert captured_input[1].get("scorecard") is scorecard

    def test_reject_returns_empty_dict_not_none(self):
        cfg = make_config()
        with patch("hypogeum.agents.executive_producer.run_agent",
                   return_value=_make_scorecard("REJECT", 5.0)):
            _, br = run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert br is not None
        assert br == {}


# ---------------------------------------------------------------------------
# TestAgentIdentity
# ---------------------------------------------------------------------------

class TestAgentIdentity:
    """agent_name and model_slug constants are correct for all calls."""

    def test_module_agent_name_constant(self):
        assert _AGENT_NAME == "executive_producer"

    def test_module_model_slug_constant(self):
        assert _MODEL_SLUG == "anthropic/claude-sonnet-4.6"

    def test_module_max_tokens_constant(self):
        assert _MAX_TOKENS == 4096

    def test_agent_name_in_both_calls(self):
        cfg = make_config()
        captured_names = []

        def capture(**kwargs):
            captured_names.append(kwargs.get("agent_name"))
            if len(captured_names) == 1:
                return _make_scorecard("PASS")
            return _make_brief()

        with patch("hypogeum.agents.executive_producer.run_agent", side_effect=capture):
            run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert captured_names[0] == "executive_producer"
        assert captured_names[1] == "executive_producer"

    def test_model_slug_in_both_calls(self):
        cfg = make_config()
        captured_slugs = []

        def capture(**kwargs):
            captured_slugs.append(kwargs.get("model_slug"))
            if len(captured_slugs) == 1:
                return _make_scorecard("PASS")
            return _make_brief()

        with patch("hypogeum.agents.executive_producer.run_agent", side_effect=capture):
            run_executive_producer(TOPIC, cfg, EPISODE_ID)
        assert captured_slugs[0] == cfg.EP_MODEL
        assert captured_slugs[1] == cfg.EP_MODEL


# ---------------------------------------------------------------------------
# TestPipelineIntegration
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    """Pipeline stub delegates to run_executive_producer and honours governance."""

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

    def test_pipeline_stub_delegates_to_run_executive_producer(self):
        cfg = make_config()
        scorecard = _make_scorecard("PASS")
        brief = _make_brief()
        with patch("hypogeum.pipeline.run_executive_producer",
                   return_value=(scorecard, brief)) as mock_ep:
            result = pipeline_ep_stub(
                topic_input=TOPIC, config=cfg,
                episode_id=EPISODE_ID, output_dir=None,
            )
        mock_ep.assert_called_once()
        assert result == (scorecard, brief)

    def test_pipeline_stub_passes_topic_input(self):
        cfg = make_config()
        with patch("hypogeum.pipeline.run_executive_producer",
                   return_value=(_make_scorecard("PASS"), _make_brief())) as mock_ep:
            pipeline_ep_stub(
                topic_input=TOPIC, config=cfg,
                episode_id=EPISODE_ID, output_dir=None,
            )
        call_kwargs = mock_ep.call_args.kwargs
        assert call_kwargs["topic_input"] == TOPIC

    def test_pipeline_stub_passes_config(self):
        cfg = make_config()
        with patch("hypogeum.pipeline.run_executive_producer",
                   return_value=(_make_scorecard("PASS"), _make_brief())) as mock_ep:
            pipeline_ep_stub(
                topic_input=TOPIC, config=cfg,
                episode_id=EPISODE_ID, output_dir=None,
            )
        call_kwargs = mock_ep.call_args.kwargs
        assert call_kwargs["config"] is cfg

    def test_pipeline_stub_passes_episode_id(self):
        cfg = make_config()
        with patch("hypogeum.pipeline.run_executive_producer",
                   return_value=(_make_scorecard("PASS"), _make_brief())) as mock_ep:
            pipeline_ep_stub(
                topic_input=TOPIC, config=cfg,
                episode_id=EPISODE_ID, output_dir=None,
            )
        call_kwargs = mock_ep.call_args.kwargs
        assert call_kwargs["episode_id"] == EPISODE_ID

    def test_pipeline_reject_raises_pipeline_rejected_error(self):
        from hypogeum.pipeline import run_pipeline
        cfg = make_config()
        scorecard = _make_scorecard("REJECT", 5.0)

        def fake_ep(**kwargs):
            return scorecard, {}

        with patch("hypogeum.pipeline.run_executive_producer", side_effect=fake_ep):
            with patch("hypogeum.pipeline.run_startup_checks") as mock_startup:
                from hypogeum.startup import StartupReport
                mock_startup.return_value = self._make_startup_report()
                with pytest.raises(PipelineRejectedError) as exc_info:
                    run_pipeline(TOPIC, EPISODE_ID, cfg)
        assert exc_info.value.episode_id == EPISODE_ID

    def test_pipeline_reject_attaches_scorecard(self):
        from hypogeum.pipeline import run_pipeline
        cfg = make_config()
        scorecard = _make_scorecard("REJECT", 5.0)

        def fake_ep(**kwargs):
            return scorecard, {}

        with patch("hypogeum.pipeline.run_executive_producer", side_effect=fake_ep):
            with patch("hypogeum.pipeline.run_startup_checks") as mock_startup:
                from hypogeum.startup import StartupReport
                mock_startup.return_value = self._make_startup_report()
                with pytest.raises(PipelineRejectedError) as exc_info:
                    run_pipeline(TOPIC, EPISODE_ID, cfg)
        assert exc_info.value.scorecard is scorecard

    def test_pipeline_pass_continues_to_next_agent(self):
        """PASS decision — pipeline proceeds past EP through all 9 agents to completion."""
        from hypogeum.pipeline import run_pipeline
        cfg = make_config()
        _minimal_rp = {
            "research_run_metadata": {}, "research_strategy": {}, "claims": [
                {"claim_id": "c1", "text": "A fact.", "confidence": 0.9}
            ],
            "contradictions": [], "open_questions": [], "source_registry": [],
            "competitive_landscape": {}, "research_run_summary": {}, "package_integrity": {},
        }

        with patch("hypogeum.pipeline.run_executive_producer",
                   return_value=(_make_scorecard("PASS"), _make_brief())), \
             patch("hypogeum.pipeline.run_research_director",
                   return_value=(_minimal_rp, None)), \
             patch("hypogeum.pipeline.run_narrative_architect",
                   return_value={}), \
             patch("hypogeum.pipeline.run_script_engine",
                   return_value={}), \
             patch("hypogeum.pipeline.run_budget_authorization",
                   return_value={"verdict": "AUTHORIZED", "authorized": True,
                                 "checks": [], "failures": [], "warnings": []}), \
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
            result = run_pipeline(TOPIC, EPISODE_ID, cfg)
        assert result.status == "completed"

    def test_pipeline_warn_continues_to_next_agent(self):
        """WARN decision — pipeline proceeds past EP through all 9 agents to completion."""
        from hypogeum.pipeline import run_pipeline
        cfg = make_config()
        _minimal_rp = {
            "research_run_metadata": {}, "research_strategy": {}, "claims": [
                {"claim_id": "c1", "text": "A fact.", "confidence": 0.9}
            ],
            "contradictions": [], "open_questions": [], "source_registry": [],
            "competitive_landscape": {}, "research_run_summary": {}, "package_integrity": {},
        }

        with patch("hypogeum.pipeline.run_executive_producer",
                   return_value=(_make_scorecard("WARN", 6.5), _make_brief())), \
             patch("hypogeum.pipeline.run_research_director",
                   return_value=(_minimal_rp, None)), \
             patch("hypogeum.pipeline.run_narrative_architect",
                   return_value={}), \
             patch("hypogeum.pipeline.run_script_engine",
                   return_value={}), \
             patch("hypogeum.pipeline.run_budget_authorization",
                   return_value={"verdict": "AUTHORIZED", "authorized": True,
                                 "checks": [], "failures": [], "warnings": []}), \
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
            result = run_pipeline(TOPIC, EPISODE_ID, cfg)
        assert result.status == "completed"

    def test_pipeline_stub_passes_output_dir(self):
        cfg = make_config()
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            with patch("hypogeum.pipeline.run_executive_producer",
                       return_value=(_make_scorecard("PASS"), _make_brief())) as mock_ep:
                pipeline_ep_stub(
                    topic_input=TOPIC, config=cfg,
                    episode_id=EPISODE_ID, output_dir=p,
                )
            call_kwargs = mock_ep.call_args.kwargs
            assert call_kwargs["output_dir"] == p
