"""
tests/test_agent.py — Unit tests for hypogeum/agent.py

All Claude calls are mocked. File writes use a temporary directory injected via
run_agent(output_dir=...). No real network calls. No fixed filesystem paths.

Test coverage:
  TestSuccessPath          — happy-path run_agent() end-to-end
  TestOutputFile           — file path, name, content, directory creation
  TestInputConstruction    — messages[] format, serialization of input_package
  TestCallClaudeIntegration — model_slug, max_tokens, config, episode_id, agent passed correctly
  TestResponseParsing      — non-JSON, JSON non-dict, various malformed payloads
  TestSchemaValidation     — invalid package triggers SchemaError; valid passes
  TestErrorPropagation     — openrouter exceptions propagate unchanged; file not written on error
  TestLogging              — correct log events at correct levels
  TestResponseParseError   — exception attributes and message
  TestAgentErrorHierarchy  — ResponseParseError is-a AgentError
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agent import (
    AgentError,
    ResponseParseError,
    _resolve_output_path,
    _write_package,
    run_agent,
)
from hypogeum.config import Config
from hypogeum.openrouter import (
    CallResult,
    ContentFiltered,
    CreditsExhausted,
    FallbackFailed,
    HardFailure,
    TruncatedResponse,
)
from hypogeum.schemas import SchemaError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SONNET = "anthropic/claude-sonnet-4.6"

MINIMAL_TOPIC_BRIEF = {
    "topic_id": "topic_001",
    "episode_title": "The Antikythera Mechanism",
    "topic_category": "Ancient Civilizations",
    "logline": "A 2,000-year-old computer found on a shipwreck.",
    "audience_demand_signal": "High search volume.",
    "curiosity_hook": "How did ancients build this?",
    "mystery_anchor": "What happened to the civilization?",
    "differentiation_hypothesis": "Focus on social implications.",
    "research_depth_flags": {"differentiation_opportunity": "mandatory_deep"},
    "shorts_directions": ["The mechanism function", "The shipwreck discovery"],
    "issued_at": "2026-06-01T10:00:00Z",
    "issued_by": "executive_producer",
}

MINIMAL_INPUT = {"topic": "The Antikythera Mechanism"}


def make_config() -> Config:
    """Build a Config with minimal valid environment."""
    pipeline_vars = [
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
    original = {k: os.environ.get(k) for k in pipeline_vars}
    for k in pipeline_vars:
        os.environ.pop(k, None)
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
    try:
        return Config()
    finally:
        for k in pipeline_vars:
            os.environ.pop(k, None)
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v


def call_result_for(content: Any) -> CallResult:
    """Build a CallResult wrapping the given content."""
    content_str = json.dumps(content) if not isinstance(content, str) else content
    return CallResult(
        content=content_str,
        usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        model_used=SONNET,
        finish_reason="stop",
        response_time_ms=1200,
        via_fallback=False,
        estimated_cost_usd=0.001,
    )


def run_with_mock(
    package: Any,
    output_dir: Path,
    *,
    schema: str = "topic_brief",
    agent: str = "executive_producer",
    episode_id: str = "ep_001",
    max_tokens: int = 4096,
) -> dict:
    """
    Convenience wrapper: mock call_claude to return package, call run_agent, return result.
    """
    cfg = make_config()
    with patch("hypogeum.agent.call_claude", return_value=call_result_for(package)):
        return run_agent(
            agent_name=agent,
            model_slug=SONNET,
            system_prompt="You are the Executive Producer.",
            input_package=MINIMAL_INPUT,
            output_schema=schema,
            episode_id=episode_id,
            max_tokens=max_tokens,
            config=cfg,
            output_dir=output_dir,
        )


# ---------------------------------------------------------------------------
# TestAgentErrorHierarchy
# ---------------------------------------------------------------------------

class TestAgentErrorHierarchy:
    def test_response_parse_error_is_agent_error(self):
        assert issubclass(ResponseParseError, AgentError)

    def test_agent_error_is_runtime_error(self):
        assert issubclass(AgentError, RuntimeError)

    def test_response_parse_error_has_agent_name(self):
        err = ResponseParseError("test_agent", "invalid_json")
        assert err.agent_name == "test_agent"

    def test_response_parse_error_has_reason(self):
        err = ResponseParseError("test_agent", "not a dict")
        assert err.reason == "not a dict"

    def test_response_parse_error_message_includes_agent(self):
        err = ResponseParseError("research_director", "invalid_json")
        assert "research_director" in str(err)

    def test_response_parse_error_with_preview(self):
        err = ResponseParseError("test_agent", "invalid_json", content_preview="abc")
        assert "abc" in str(err)

    def test_response_parse_error_without_preview(self):
        err = ResponseParseError("test_agent", "invalid_json")
        assert str(err)  # just verify it doesn't crash


# ---------------------------------------------------------------------------
# TestSuccessPath
# ---------------------------------------------------------------------------

class TestSuccessPath:
    def test_returns_package_dict(self, tmp_path):
        result = run_with_mock(MINIMAL_TOPIC_BRIEF, tmp_path)
        assert isinstance(result, dict)

    def test_returns_correct_content(self, tmp_path):
        result = run_with_mock(MINIMAL_TOPIC_BRIEF, tmp_path)
        assert result["topic_id"] == "topic_001"
        assert result["episode_title"] == "The Antikythera Mechanism"

    def test_does_not_raise_on_valid_package(self, tmp_path):
        run_with_mock(MINIMAL_TOPIC_BRIEF, tmp_path)  # must not raise

    def test_schema_validation_called_with_correct_name(self, tmp_path):
        with patch("hypogeum.agent.call_claude", return_value=call_result_for(MINIMAL_TOPIC_BRIEF)):
            with patch("hypogeum.agent.validate") as mock_validate:
                cfg = make_config()
                run_agent(
                    agent_name="executive_producer",
                    model_slug=SONNET,
                    system_prompt="System prompt.",
                    input_package=MINIMAL_INPUT,
                    output_schema="topic_brief",
                    episode_id="ep_001",
                    max_tokens=4096,
                    config=cfg,
                    output_dir=tmp_path,
                )
            mock_validate.assert_called_once_with("topic_brief", MINIMAL_TOPIC_BRIEF)


# ---------------------------------------------------------------------------
# TestOutputFile
# ---------------------------------------------------------------------------

class TestOutputFile:
    def test_file_is_written(self, tmp_path):
        run_with_mock(MINIMAL_TOPIC_BRIEF, tmp_path)
        files = list(tmp_path.iterdir())
        assert len(files) == 1

    def test_file_name_includes_schema_and_episode_id(self, tmp_path):
        run_with_mock(MINIMAL_TOPIC_BRIEF, tmp_path, schema="topic_brief", episode_id="ep_001")
        assert (tmp_path / "topic_brief_ep_001.json").exists()

    def test_file_name_reflects_schema(self, tmp_path):
        """Different schemas produce different filenames."""
        # Use a valid research_package fixture
        from tests.test_schemas import _research_package
        run_with_mock(_research_package(), tmp_path, schema="research_package")
        assert (tmp_path / "research_package_ep_001.json").exists()

    def test_file_content_is_valid_json(self, tmp_path):
        run_with_mock(MINIMAL_TOPIC_BRIEF, tmp_path)
        out_file = tmp_path / "topic_brief_ep_001.json"
        content = json.loads(out_file.read_text(encoding="utf-8"))
        assert isinstance(content, dict)

    def test_file_content_matches_returned_package(self, tmp_path):
        result = run_with_mock(MINIMAL_TOPIC_BRIEF, tmp_path)
        out_file = tmp_path / "topic_brief_ep_001.json"
        on_disk = json.loads(out_file.read_text(encoding="utf-8"))
        assert on_disk == result

    def test_file_content_is_pretty_printed(self, tmp_path):
        run_with_mock(MINIMAL_TOPIC_BRIEF, tmp_path)
        out_file = tmp_path / "topic_brief_ep_001.json"
        raw = out_file.read_text(encoding="utf-8")
        assert "\n" in raw  # pretty-printed, not single line

    def test_file_is_utf8(self, tmp_path):
        package = copy.deepcopy(MINIMAL_TOPIC_BRIEF)
        package["logline"] = "Découverte sous-marine extraordinaire."
        run_with_mock(package, tmp_path)
        out_file = tmp_path / "topic_brief_ep_001.json"
        content = out_file.read_bytes().decode("utf-8")
        assert "Découverte" in content

    def test_output_directory_created_if_missing(self, tmp_path):
        nested = tmp_path / "deep" / "nested" / "dir"
        assert not nested.exists()
        run_with_mock(MINIMAL_TOPIC_BRIEF, nested)
        assert nested.exists()
        assert (nested / "topic_brief_ep_001.json").exists()

    def test_default_output_path_structure(self):
        """_resolve_output_path() without override uses standard path."""
        path = _resolve_output_path("ep_test", "script_package", None)
        assert "episodes" in str(path)
        assert "ep_test" in str(path)
        assert path.name == "script_package_ep_test.json"

    def test_override_output_path(self, tmp_path):
        path = _resolve_output_path("ep_test", "script_package", tmp_path)
        assert path.parent == tmp_path
        assert path.name == "script_package_ep_test.json"


# ---------------------------------------------------------------------------
# TestInputConstruction
# ---------------------------------------------------------------------------

class TestInputConstruction:
    def test_system_prompt_in_system_role(self, tmp_path):
        cfg = make_config()
        with patch("hypogeum.agent.call_claude", return_value=call_result_for(MINIMAL_TOPIC_BRIEF)) as mock_cc:
            run_agent(
                agent_name="ep",
                model_slug=SONNET,
                system_prompt="Custom system prompt here.",
                input_package=MINIMAL_INPUT,
                output_schema="topic_brief",
                episode_id="ep_001",
                max_tokens=4096,
                config=cfg,
                output_dir=tmp_path,
            )
        messages = mock_cc.call_args.kwargs["messages"]
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert system_msgs[0]["content"] == "Custom system prompt here."

    def test_input_package_serialized_as_user_message(self, tmp_path):
        cfg = make_config()
        input_pkg = {"key": "value", "number": 42}
        with patch("hypogeum.agent.call_claude", return_value=call_result_for(MINIMAL_TOPIC_BRIEF)) as mock_cc:
            run_agent(
                agent_name="ep",
                model_slug=SONNET,
                system_prompt="Prompt.",
                input_package=input_pkg,
                output_schema="topic_brief",
                episode_id="ep_001",
                max_tokens=4096,
                config=cfg,
                output_dir=tmp_path,
            )
        messages = mock_cc.call_args.kwargs["messages"]
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1
        parsed_input = json.loads(user_msgs[0]["content"])
        assert parsed_input == input_pkg

    def test_messages_has_exactly_two_entries(self, tmp_path):
        cfg = make_config()
        with patch("hypogeum.agent.call_claude", return_value=call_result_for(MINIMAL_TOPIC_BRIEF)) as mock_cc:
            run_agent(
                agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                input_package=MINIMAL_INPUT, output_schema="topic_brief",
                episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
            )
        messages = mock_cc.call_args.kwargs["messages"]
        assert len(messages) == 2

    def test_input_unicode_survives_serialization(self, tmp_path):
        cfg = make_config()
        input_pkg = {"topic": "メカニズム"}
        with patch("hypogeum.agent.call_claude", return_value=call_result_for(MINIMAL_TOPIC_BRIEF)) as mock_cc:
            run_agent(
                agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                input_package=input_pkg, output_schema="topic_brief",
                episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
            )
        messages = mock_cc.call_args.kwargs["messages"]
        user_content = messages[1]["content"]
        parsed = json.loads(user_content)
        assert parsed["topic"] == "メカニズム"


# ---------------------------------------------------------------------------
# TestCallClaudeIntegration
# ---------------------------------------------------------------------------

class TestCallClaudeIntegration:
    def _run(self, tmp_path, **overrides) -> MagicMock:
        cfg = make_config()
        kwargs = dict(
            agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
            input_package=MINIMAL_INPUT, output_schema="topic_brief",
            episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
        )
        kwargs.update(overrides)
        with patch("hypogeum.agent.call_claude", return_value=call_result_for(MINIMAL_TOPIC_BRIEF)) as mock_cc:
            run_agent(**kwargs)
        return mock_cc

    def test_model_slug_forwarded(self, tmp_path):
        mock_cc = self._run(tmp_path, model_slug="anthropic/claude-opus-4.6")
        assert mock_cc.call_args.kwargs["model_slug"] == "anthropic/claude-opus-4.6"

    def test_max_tokens_forwarded(self, tmp_path):
        mock_cc = self._run(tmp_path, max_tokens=8192)
        assert mock_cc.call_args.kwargs["max_tokens"] == 8192

    def test_config_forwarded(self, tmp_path):
        cfg = make_config()
        with patch("hypogeum.agent.call_claude", return_value=call_result_for(MINIMAL_TOPIC_BRIEF)) as mock_cc:
            run_agent(
                agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                input_package=MINIMAL_INPUT, output_schema="topic_brief",
                episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
            )
        assert mock_cc.call_args.kwargs["config"] is cfg

    def test_episode_id_forwarded(self, tmp_path):
        mock_cc = self._run(tmp_path, episode_id="ep_xyz")
        assert mock_cc.call_args.kwargs["episode_id"] == "ep_xyz"

    def test_agent_name_forwarded(self, tmp_path):
        mock_cc = self._run(tmp_path, agent_name="narrative_architect")
        assert mock_cc.call_args.kwargs["agent"] == "narrative_architect"

    def test_call_claude_called_exactly_once(self, tmp_path):
        mock_cc = self._run(tmp_path)
        assert mock_cc.call_count == 1


# ---------------------------------------------------------------------------
# TestResponseParsing
# ---------------------------------------------------------------------------

class TestResponseParsing:
    def test_invalid_json_raises_response_parse_error(self, tmp_path):
        cfg = make_config()
        bad_result = call_result_for("this is not json")
        bad_result = CallResult(
            content="not json {{",
            usage={}, model_used=SONNET, finish_reason="stop",
            response_time_ms=100, via_fallback=False, estimated_cost_usd=0.0,
        )
        with patch("hypogeum.agent.call_claude", return_value=bad_result):
            with pytest.raises(ResponseParseError) as exc_info:
                run_agent(
                    agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                    input_package=MINIMAL_INPUT, output_schema="topic_brief",
                    episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
                )
        assert exc_info.value.agent_name == "ep"

    def test_json_array_raises_response_parse_error(self, tmp_path):
        cfg = make_config()
        array_result = CallResult(
            content=json.dumps(["this", "is", "a", "list"]),
            usage={}, model_used=SONNET, finish_reason="stop",
            response_time_ms=100, via_fallback=False, estimated_cost_usd=0.0,
        )
        with patch("hypogeum.agent.call_claude", return_value=array_result):
            with pytest.raises(ResponseParseError) as exc_info:
                run_agent(
                    agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                    input_package=MINIMAL_INPUT, output_schema="topic_brief",
                    episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
                )
        assert "list" in str(exc_info.value)

    def test_json_null_raises_response_parse_error(self, tmp_path):
        cfg = make_config()
        null_result = CallResult(
            content="null",
            usage={}, model_used=SONNET, finish_reason="stop",
            response_time_ms=100, via_fallback=False, estimated_cost_usd=0.0,
        )
        with patch("hypogeum.agent.call_claude", return_value=null_result):
            with pytest.raises(ResponseParseError):
                run_agent(
                    agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                    input_package=MINIMAL_INPUT, output_schema="topic_brief",
                    episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
                )

    def test_json_string_raises_response_parse_error(self, tmp_path):
        cfg = make_config()
        str_result = CallResult(
            content=json.dumps("just a string"),
            usage={}, model_used=SONNET, finish_reason="stop",
            response_time_ms=100, via_fallback=False, estimated_cost_usd=0.0,
        )
        with patch("hypogeum.agent.call_claude", return_value=str_result):
            with pytest.raises(ResponseParseError):
                run_agent(
                    agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                    input_package=MINIMAL_INPUT, output_schema="topic_brief",
                    episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
                )

    def test_parse_error_has_cause_on_invalid_json(self, tmp_path):
        cfg = make_config()
        bad_result = CallResult(
            content="{{bad json",
            usage={}, model_used=SONNET, finish_reason="stop",
            response_time_ms=100, via_fallback=False, estimated_cost_usd=0.0,
        )
        with patch("hypogeum.agent.call_claude", return_value=bad_result):
            with pytest.raises(ResponseParseError) as exc_info:
                run_agent(
                    agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                    input_package=MINIMAL_INPUT, output_schema="topic_brief",
                    episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
                )
        assert exc_info.value.__cause__ is not None

    def test_json_wrapped_in_markdown_fence_is_parsed(self, tmp_path):
        """Claude occasionally wraps JSON in ```json ... ``` — must parse correctly."""
        cfg = make_config()
        inner = json.dumps(MINIMAL_TOPIC_BRIEF)
        fenced_result = CallResult(
            content=f"```json\n{inner}\n```",
            usage={"prompt_tokens": 10, "completion_tokens": 20},
            model_used=SONNET, finish_reason="stop",
            response_time_ms=100, via_fallback=False, estimated_cost_usd=0.001,
        )
        with patch("hypogeum.agent.call_claude", return_value=fenced_result):
            pkg = run_agent(
                agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                input_package=MINIMAL_INPUT, output_schema="topic_brief",
                episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
            )
        assert pkg["topic_id"] == MINIMAL_TOPIC_BRIEF["topic_id"]

    def test_json_wrapped_in_bare_fence_is_parsed(self, tmp_path):
        """Claude occasionally uses bare ``` fences without language tag."""
        cfg = make_config()
        inner = json.dumps(MINIMAL_TOPIC_BRIEF)
        fenced_result = CallResult(
            content=f"```\n{inner}\n```",
            usage={"prompt_tokens": 10, "completion_tokens": 20},
            model_used=SONNET, finish_reason="stop",
            response_time_ms=100, via_fallback=False, estimated_cost_usd=0.001,
        )
        with patch("hypogeum.agent.call_claude", return_value=fenced_result):
            pkg = run_agent(
                agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                input_package=MINIMAL_INPUT, output_schema="topic_brief",
                episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
            )
        assert pkg["topic_id"] == MINIMAL_TOPIC_BRIEF["topic_id"]

    def test_raw_json_unaffected_by_fence_stripping(self, tmp_path):
        """Raw JSON without fences must still parse correctly after stripping."""
        cfg = make_config()
        raw_result = CallResult(
            content=json.dumps(MINIMAL_TOPIC_BRIEF),
            usage={"prompt_tokens": 10, "completion_tokens": 20},
            model_used=SONNET, finish_reason="stop",
            response_time_ms=100, via_fallback=False, estimated_cost_usd=0.001,
        )
        with patch("hypogeum.agent.call_claude", return_value=raw_result):
            pkg = run_agent(
                agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                input_package=MINIMAL_INPUT, output_schema="topic_brief",
                episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
            )
        assert pkg["topic_id"] == MINIMAL_TOPIC_BRIEF["topic_id"]


# ---------------------------------------------------------------------------
# TestSchemaValidation
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_schema_error_raised_on_invalid_package(self, tmp_path):
        cfg = make_config()
        # topic_brief without required fields
        bad_package = {"topic_id": "x"}  # missing most fields
        result = CallResult(
            content=json.dumps(bad_package),
            usage={}, model_used=SONNET, finish_reason="stop",
            response_time_ms=100, via_fallback=False, estimated_cost_usd=0.0,
        )
        with patch("hypogeum.agent.call_claude", return_value=result):
            with pytest.raises(SchemaError) as exc_info:
                run_agent(
                    agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                    input_package=MINIMAL_INPUT, output_schema="topic_brief",
                    episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
                )
        assert exc_info.value.schema_name == "topic_brief"

    def test_schema_error_carries_path_and_reason(self, tmp_path):
        cfg = make_config()
        bad_package = {"topic_id": "x"}
        result = CallResult(
            content=json.dumps(bad_package),
            usage={}, model_used=SONNET, finish_reason="stop",
            response_time_ms=100, via_fallback=False, estimated_cost_usd=0.0,
        )
        with patch("hypogeum.agent.call_claude", return_value=result):
            with pytest.raises(SchemaError) as exc_info:
                run_agent(
                    agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                    input_package=MINIMAL_INPUT, output_schema="topic_brief",
                    episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
                )
        assert exc_info.value.path
        assert exc_info.value.reason

    def test_valid_package_does_not_raise(self, tmp_path):
        cfg = make_config()
        result = CallResult(
            content=json.dumps(MINIMAL_TOPIC_BRIEF),
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            model_used=SONNET, finish_reason="stop",
            response_time_ms=100, via_fallback=False, estimated_cost_usd=0.001,
        )
        with patch("hypogeum.agent.call_claude", return_value=result):
            run_agent(
                agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                input_package=MINIMAL_INPUT, output_schema="topic_brief",
                episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
            )  # must not raise

    def test_unknown_schema_raises_value_error(self, tmp_path):
        cfg = make_config()
        result = CallResult(
            content=json.dumps({"key": "value"}),
            usage={}, model_used=SONNET, finish_reason="stop",
            response_time_ms=100, via_fallback=False, estimated_cost_usd=0.0,
        )
        with patch("hypogeum.agent.call_claude", return_value=result):
            with pytest.raises(ValueError):
                run_agent(
                    agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                    input_package=MINIMAL_INPUT, output_schema="nonexistent_schema",
                    episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
                )


# ---------------------------------------------------------------------------
# TestErrorPropagation
# ---------------------------------------------------------------------------

class TestErrorPropagation:
    def _run_with_error(self, tmp_path, exc):
        cfg = make_config()
        with patch("hypogeum.agent.call_claude", side_effect=exc):
            run_agent(
                agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                input_package=MINIMAL_INPUT, output_schema="topic_brief",
                episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
            )

    def test_hard_failure_propagates(self, tmp_path):
        with pytest.raises(HardFailure):
            self._run_with_error(tmp_path, HardFailure("Bad request", http_status=400))

    def test_credits_exhausted_propagates(self, tmp_path):
        with pytest.raises(CreditsExhausted):
            self._run_with_error(tmp_path, CreditsExhausted("No credits", http_status=402))

    def test_truncated_response_propagates(self, tmp_path):
        with pytest.raises(TruncatedResponse):
            self._run_with_error(tmp_path, TruncatedResponse(4096, 4096))

    def test_content_filtered_propagates(self, tmp_path):
        with pytest.raises(ContentFiltered):
            self._run_with_error(tmp_path, ContentFiltered("Filtered", http_status=200))

    def test_fallback_failed_propagates(self, tmp_path):
        with pytest.raises(FallbackFailed):
            self._run_with_error(tmp_path, FallbackFailed("Both failed"))

    def test_file_not_written_on_parse_error(self, tmp_path):
        cfg = make_config()
        bad_result = CallResult(
            content="not json",
            usage={}, model_used=SONNET, finish_reason="stop",
            response_time_ms=100, via_fallback=False, estimated_cost_usd=0.0,
        )
        with patch("hypogeum.agent.call_claude", return_value=bad_result):
            with pytest.raises(ResponseParseError):
                run_agent(
                    agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                    input_package=MINIMAL_INPUT, output_schema="topic_brief",
                    episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
                )
        # No file should exist
        assert list(tmp_path.iterdir()) == []

    def test_file_not_written_on_schema_error(self, tmp_path):
        cfg = make_config()
        bad_pkg = CallResult(
            content=json.dumps({"topic_id": "only_this"}),
            usage={}, model_used=SONNET, finish_reason="stop",
            response_time_ms=100, via_fallback=False, estimated_cost_usd=0.0,
        )
        with patch("hypogeum.agent.call_claude", return_value=bad_pkg):
            with pytest.raises(SchemaError):
                run_agent(
                    agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                    input_package=MINIMAL_INPUT, output_schema="topic_brief",
                    episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
                )
        assert list(tmp_path.iterdir()) == []

    def test_file_not_written_on_call_claude_error(self, tmp_path):
        cfg = make_config()
        with patch("hypogeum.agent.call_claude", side_effect=HardFailure("Bad", 400)):
            with pytest.raises(HardFailure):
                run_agent(
                    agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                    input_package=MINIMAL_INPUT, output_schema="topic_brief",
                    episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
                )
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# TestLogging
# ---------------------------------------------------------------------------

class TestLogging:
    def _run_capturing_log(self, tmp_path, package=None, raise_exc=None):
        cfg = make_config()
        if raise_exc:
            mock_result = raise_exc
        else:
            content = json.dumps(package or MINIMAL_TOPIC_BRIEF)
            mock_result = CallResult(
                content=content,
                usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                model_used=SONNET, finish_reason="stop",
                response_time_ms=100, via_fallback=False, estimated_cost_usd=0.001,
            )

        with patch("hypogeum.agent.call_claude",
                   return_value=mock_result if not raise_exc else None,
                   side_effect=raise_exc if raise_exc else None):
            with patch("hypogeum.agent.get_logger") as mock_get_logger:
                mock_log = MagicMock()
                mock_get_logger.return_value = mock_log
                try:
                    run_agent(
                        agent_name="test_agent", model_slug=SONNET, system_prompt="Prompt.",
                        input_package=MINIMAL_INPUT, output_schema="topic_brief",
                        episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
                    )
                except Exception:
                    pass
        return mock_log

    def test_agent_run_started_logged_at_info(self, tmp_path):
        mock_log = self._run_capturing_log(tmp_path)
        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "agent_run_started" in info_events

    def test_agent_run_completed_logged_at_info(self, tmp_path):
        mock_log = self._run_capturing_log(tmp_path)
        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "agent_run_completed" in info_events

    def test_agent_package_written_logged_at_info(self, tmp_path):
        mock_log = self._run_capturing_log(tmp_path)
        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "agent_package_written" in info_events

    def test_parse_failure_logged_at_error(self, tmp_path):
        cfg = make_config()
        bad_result = CallResult(
            content="not json",
            usage={}, model_used=SONNET, finish_reason="stop",
            response_time_ms=100, via_fallback=False, estimated_cost_usd=0.0,
        )
        with patch("hypogeum.agent.call_claude", return_value=bad_result):
            with patch("hypogeum.agent.get_logger") as mock_get_logger:
                mock_log = MagicMock()
                mock_get_logger.return_value = mock_log
                with pytest.raises(ResponseParseError):
                    run_agent(
                        agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                        input_package=MINIMAL_INPUT, output_schema="topic_brief",
                        episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
                    )
        error_events = [c.args[0] for c in mock_log.error.call_args_list]
        assert "agent_response_parse_failed" in error_events

    def test_schema_failure_logged_at_error(self, tmp_path):
        cfg = make_config()
        bad_pkg = CallResult(
            content=json.dumps({"topic_id": "x"}),
            usage={}, model_used=SONNET, finish_reason="stop",
            response_time_ms=100, via_fallback=False, estimated_cost_usd=0.0,
        )
        with patch("hypogeum.agent.call_claude", return_value=bad_pkg):
            with patch("hypogeum.agent.get_logger") as mock_get_logger:
                mock_log = MagicMock()
                mock_get_logger.return_value = mock_log
                with pytest.raises(SchemaError):
                    run_agent(
                        agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                        input_package=MINIMAL_INPUT, output_schema="topic_brief",
                        episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
                    )
        error_events = [c.args[0] for c in mock_log.error.call_args_list]
        assert "agent_schema_validation_failed" in error_events

    def test_logger_bound_to_correct_episode_and_agent(self, tmp_path):
        cfg = make_config()
        with patch("hypogeum.agent.call_claude", return_value=call_result_for(MINIMAL_TOPIC_BRIEF)):
            with patch("hypogeum.agent.get_logger") as mock_get_logger:
                mock_log = MagicMock()
                mock_get_logger.return_value = mock_log
                run_agent(
                    agent_name="narrative_architect", model_slug=SONNET,
                    system_prompt="Prompt.", input_package=MINIMAL_INPUT,
                    output_schema="topic_brief", episode_id="ep_antikythera",
                    max_tokens=4096, config=cfg, output_dir=tmp_path,
                )
        mock_get_logger.assert_called_once_with(
            episode_id="ep_antikythera",
            agent="narrative_architect",
        )

    def test_run_started_includes_schema_and_model(self, tmp_path):
        cfg = make_config()
        with patch("hypogeum.agent.call_claude", return_value=call_result_for(MINIMAL_TOPIC_BRIEF)):
            with patch("hypogeum.agent.get_logger") as mock_get_logger:
                mock_log = MagicMock()
                mock_get_logger.return_value = mock_log
                run_agent(
                    agent_name="ep", model_slug="anthropic/claude-opus-4.6",
                    system_prompt="Prompt.", input_package=MINIMAL_INPUT,
                    output_schema="topic_brief", episode_id="ep_001",
                    max_tokens=4096, config=cfg, output_dir=tmp_path,
                )
        started_call = next(
            c for c in mock_log.info.call_args_list if c.args[0] == "agent_run_started"
        )
        assert started_call.kwargs.get("output_schema") == "topic_brief"
        assert started_call.kwargs.get("model_slug") == "anthropic/claude-opus-4.6"

    def test_run_completed_includes_token_counts(self, tmp_path):
        cfg = make_config()
        with patch("hypogeum.agent.call_claude", return_value=call_result_for(MINIMAL_TOPIC_BRIEF)):
            with patch("hypogeum.agent.get_logger") as mock_get_logger:
                mock_log = MagicMock()
                mock_get_logger.return_value = mock_log
                run_agent(
                    agent_name="ep", model_slug=SONNET, system_prompt="Prompt.",
                    input_package=MINIMAL_INPUT, output_schema="topic_brief",
                    episode_id="ep_001", max_tokens=4096, config=cfg, output_dir=tmp_path,
                )
        completed_call = next(
            c for c in mock_log.info.call_args_list if c.args[0] == "agent_run_completed"
        )
        assert "prompt_tokens" in completed_call.kwargs
        assert "completion_tokens" in completed_call.kwargs
        assert "estimated_cost_usd" in completed_call.kwargs


# ---------------------------------------------------------------------------
# TestWritePackageHelper
# ---------------------------------------------------------------------------

class TestWritePackageHelper:
    """Direct tests for the internal _write_package helper."""

    def test_writes_file(self, tmp_path):
        log = MagicMock()
        _write_package({"key": "value"}, tmp_path / "out.json", "test", log)
        assert (tmp_path / "out.json").exists()

    def test_content_is_valid_json(self, tmp_path):
        log = MagicMock()
        _write_package({"key": "value", "n": 42}, tmp_path / "out.json", "test", log)
        data = json.loads((tmp_path / "out.json").read_text())
        assert data["key"] == "value"
        assert data["n"] == 42

    def test_creates_parent_directories(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c" / "out.json"
        log = MagicMock()
        _write_package({"x": 1}, nested, "test", log)
        assert nested.exists()

    def test_logs_agent_package_written(self, tmp_path):
        log = MagicMock()
        _write_package({"x": 1}, tmp_path / "out.json", "my_schema", log)
        log.info.assert_called_once()
        assert log.info.call_args.args[0] == "agent_package_written"

    def test_log_includes_size_bytes(self, tmp_path):
        log = MagicMock()
        _write_package({"x": 1}, tmp_path / "out.json", "my_schema", log)
        assert log.info.call_args.kwargs.get("size_bytes", 0) > 0
