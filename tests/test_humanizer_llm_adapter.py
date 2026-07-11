"""
tests/test_humanizer_llm_adapter.py — Unit tests for the thin Humanizer LLM
runtime adapter (hypogeum/agents/humanizer_llm_adapter.py).

Scope: this module only. All hypogeum.openrouter.call_claude() calls are
mocked — no real network/API access. No hypogeum.finance DB writes occur
because record_cost() is a no-op outside an active run context.

Coverage:
    TestPinnedSkillIntegrity  — the pinned SKILL.md is loaded verbatim, byte-identical
    TestSystemPromptComposition — wrapper + skill composed via compose_system_prompt()
    TestStructuredOutputParsing — {"humanized_text": ...} parsing, fences, malformed shapes
    TestProviderFailure       — every OpenRouterError subclass maps to HumanizerLLMAdapterError
    TestCostTracking          — record_cost() is invoked on a successful call
    TestIsolation             — no direct OpenAI/Anthropic SDK, no httpx/requests/subprocess
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.humanizer_llm_adapter import (
    HUMANIZER_LLM_MODEL_ID,
    HumanizerLLMAdapterError,
    HumanizerLLMResult,
    _PINNED_SKILL_PATH,
    _WRAPPER_PROMPT_PATH,
    call_humanizer_llm,
)
from hypogeum.config import Config
from hypogeum.humanization_fingerprint import PINNED_HUMANIZER_SHA
from hypogeum.openrouter import (
    CallResult,
    ContentFiltered,
    CreditsExhausted,
    EmptyResponse,
    FallbackFailed,
    HardFailure,
    TruncatedResponse,
)


def make_config() -> Config:
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


def call_result_for(content: str, **overrides) -> CallResult:
    defaults = dict(
        content=content,
        usage={"prompt_tokens": 500, "completion_tokens": 40, "total_tokens": 540},
        model_used=HUMANIZER_LLM_MODEL_ID,
        finish_reason="stop",
        response_time_ms=850,
        via_fallback=False,
        estimated_cost_usd=0.002,
    )
    defaults.update(overrides)
    return CallResult(**defaults)


class TestPinnedSkillIntegrity:
    def test_pinned_skill_path_matches_manifest_sha(self):
        assert PINNED_HUMANIZER_SHA in str(_PINNED_SKILL_PATH)
        assert _PINNED_SKILL_PATH.name == "SKILL.md"
        assert _PINNED_SKILL_PATH.exists()

    def test_skill_content_loaded_verbatim_byte_identical(self):
        """
        The composed system prompt must contain the pinned SKILL.md's exact
        on-disk bytes, unmodified — no paraphrase, no summarization, no
        rewrite of upstream content.
        """
        on_disk = _PINNED_SKILL_PATH.read_text(encoding="utf-8")
        with patch(
            "hypogeum.agents.humanizer_llm_adapter.call_claude",
            return_value=call_result_for('{"humanized_text": "ok"}'),
        ):
            cfg = make_config()
            call_humanizer_llm(
                beat_id="b_001", original_text="Some narration.",
                episode_id="ep_001", config=cfg,
            )
        import hypogeum.agents.humanizer_llm_adapter as mod
        composed = mod._system_prompt()
        assert on_disk in composed

    def test_no_duplicate_or_rewritten_humanizer_content_in_wrapper_prompt(self):
        """
        The Hypogeum-authored wrapper instructions file must not itself
        contain a copy of the upstream skill's own numbered pattern list —
        it must reference/compose with the pinned file, never re-author its
        content.
        """
        wrapper_text = _WRAPPER_PROMPT_PATH.read_text(encoding="utf-8")
        skill_text = _PINNED_SKILL_PATH.read_text(encoding="utf-8")
        # A representative, distinctive upstream section heading must not be
        # duplicated verbatim inside the wrapper's own instructions.
        assert "### 24. Excessive Hedging" not in wrapper_text
        assert "### 24. Excessive Hedging" in skill_text


class TestSystemPromptComposition:
    def test_wrapper_forbids_ask_user_question(self):
        wrapper_text = _WRAPPER_PROMPT_PATH.read_text(encoding="utf-8")
        assert "AskUserQuestion" in wrapper_text
        assert "may not ask" in wrapper_text or "No clarifying questions" in wrapper_text

    def test_wrapper_requires_structured_json_output(self):
        wrapper_text = _WRAPPER_PROMPT_PATH.read_text(encoding="utf-8")
        assert "humanized_text" in wrapper_text
        assert "JSON" in wrapper_text

    def test_missing_pinned_skill_raises_adapter_error(self, monkeypatch):
        import hypogeum.agents.humanizer_llm_adapter as mod

        monkeypatch.setattr(mod, "_PINNED_SKILL_PATH", Path("/nonexistent/SKILL.md"))
        monkeypatch.setattr(mod, "_SYSTEM_PROMPT", None)
        with pytest.raises(HumanizerLLMAdapterError):
            mod._load_system_prompt()
        monkeypatch.setattr(mod, "_SYSTEM_PROMPT", None)  # do not leak cache to other tests


class TestStructuredOutputParsing:
    def test_valid_json_response_parsed(self):
        cfg = make_config()
        with patch(
            "hypogeum.agents.humanizer_llm_adapter.call_claude",
            return_value=call_result_for('{"humanized_text": "A rewritten sentence."}'),
        ):
            result = call_humanizer_llm(
                beat_id="b_001", original_text="A sentence.", episode_id="ep_001", config=cfg,
            )
        assert isinstance(result, HumanizerLLMResult)
        assert result.humanized_text == "A rewritten sentence."
        assert result.model_id == HUMANIZER_LLM_MODEL_ID
        assert result.provider == "openrouter"
        assert result.total_tokens == 540

    def test_markdown_fenced_json_response_parsed(self):
        cfg = make_config()
        fenced = '```json\n{"humanized_text": "Fenced rewrite."}\n```'
        with patch(
            "hypogeum.agents.humanizer_llm_adapter.call_claude",
            return_value=call_result_for(fenced),
        ):
            result = call_humanizer_llm(
                beat_id="b_001", original_text="A sentence.", episode_id="ep_001", config=cfg,
            )
        assert result.humanized_text == "Fenced rewrite."

    def test_non_json_response_raises_adapter_error(self):
        cfg = make_config()
        with patch(
            "hypogeum.agents.humanizer_llm_adapter.call_claude",
            return_value=call_result_for("This is not JSON at all."),
        ):
            with pytest.raises(HumanizerLLMAdapterError):
                call_humanizer_llm(
                    beat_id="b_001", original_text="A sentence.", episode_id="ep_001", config=cfg,
                )

    def test_json_array_response_raises_adapter_error(self):
        cfg = make_config()
        with patch(
            "hypogeum.agents.humanizer_llm_adapter.call_claude",
            return_value=call_result_for('["not", "a", "dict"]'),
        ):
            with pytest.raises(HumanizerLLMAdapterError):
                call_humanizer_llm(
                    beat_id="b_001", original_text="A sentence.", episode_id="ep_001", config=cfg,
                )

    def test_missing_humanized_text_key_raises_adapter_error(self):
        cfg = make_config()
        with patch(
            "hypogeum.agents.humanizer_llm_adapter.call_claude",
            return_value=call_result_for('{"wrong_key": "value"}'),
        ):
            with pytest.raises(HumanizerLLMAdapterError):
                call_humanizer_llm(
                    beat_id="b_001", original_text="A sentence.", episode_id="ep_001", config=cfg,
                )

    def test_empty_humanized_text_raises_adapter_error(self):
        cfg = make_config()
        with patch(
            "hypogeum.agents.humanizer_llm_adapter.call_claude",
            return_value=call_result_for('{"humanized_text": "   "}'),
        ):
            with pytest.raises(HumanizerLLMAdapterError):
                call_humanizer_llm(
                    beat_id="b_001", original_text="A sentence.", episode_id="ep_001", config=cfg,
                )


class TestProviderFailure:
    @pytest.mark.parametrize(
        "exc",
        [
            HardFailure("bad request", http_status=400),
            CreditsExhausted("no credits", http_status=402),
            ContentFiltered("blocked", http_status=None),
            EmptyResponse("empty", http_status=None),
            TruncatedResponse(completion_tokens=2048, max_tokens_set=2048),
            FallbackFailed("both failed"),
        ],
    )
    def test_openrouter_error_maps_to_adapter_error(self, exc):
        cfg = make_config()
        with patch("hypogeum.agents.humanizer_llm_adapter.call_claude", side_effect=exc):
            with pytest.raises(HumanizerLLMAdapterError):
                call_humanizer_llm(
                    beat_id="b_001", original_text="A sentence.", episode_id="ep_001", config=cfg,
                )


class TestCostTracking:
    def test_record_cost_called_on_success(self):
        cfg = make_config()
        with patch(
            "hypogeum.agents.humanizer_llm_adapter.call_claude",
            return_value=call_result_for('{"humanized_text": "ok"}'),
        ), patch("hypogeum.agents.humanizer_llm_adapter.record_cost") as mock_cost:
            call_humanizer_llm(
                beat_id="b_001", original_text="A sentence.", episode_id="ep_001", config=cfg,
            )
        mock_cost.assert_called_once()
        _, kwargs = mock_cost.call_args
        assert kwargs["provider"] == "openrouter"
        assert kwargs["call_type"] == "humanization_stage_llm"

    def test_record_cost_not_called_on_provider_failure(self):
        cfg = make_config()
        with patch(
            "hypogeum.agents.humanizer_llm_adapter.call_claude",
            side_effect=HardFailure("bad request"),
        ), patch("hypogeum.agents.humanizer_llm_adapter.record_cost") as mock_cost:
            with pytest.raises(HumanizerLLMAdapterError):
                call_humanizer_llm(
                    beat_id="b_001", original_text="A sentence.", episode_id="ep_001", config=cfg,
                )
        mock_cost.assert_not_called()


class TestIsolation:
    def test_module_has_no_direct_provider_sdk_or_low_level_call_surface(self):
        import inspect

        import hypogeum.agents.humanizer_llm_adapter as mod
        source = inspect.getsource(mod)
        for forbidden in ("OpenAI(", "anthropic.Anthropic(", "requests.", "httpx.", "subprocess."):
            assert forbidden not in source, f"found forbidden call surface: {forbidden}"

    def test_uses_call_claude_not_run_agent(self):
        import ast
        import inspect

        import hypogeum.agents.humanizer_llm_adapter as mod
        tree = ast.parse(inspect.getsource(mod))
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert "call_claude" in imported_names
        assert "run_agent" not in imported_names
