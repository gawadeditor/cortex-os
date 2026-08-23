"""
Focused unit coverage for hypogeum.agents.canonical_metadata_agent's
_default_llm_call() -- the real production LLM-call wrapper used when no
llm_call is injected. Every other test of this module (see
tests/test_zernio_channel_release_trigger.py,
tests/test_cross_platform_publishing.py) exercises
generate_canonical_platform_metadata_for_deliverable() with an injected
fake llm_call, which never touches _default_llm_call() itself.
"""
import json

import pytest

from hypogeum.agents.canonical_metadata_agent import _default_llm_call
from hypogeum.config import Config
from hypogeum.openrouter import CallResult


def _result(content: str) -> CallResult:
    return CallResult(
        content=content, usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        model_used="anthropic/claude-sonnet-4.6", finish_reason="stop",
        response_time_ms=1, via_fallback=False, estimated_cost_usd=0.0,
    )


class TestDefaultLlmCallJsonRetry:
    """
    Real bug found live against ep_f3619022's short_003: the model
    occasionally emits a literal, unescaped double-quote character inside
    a JSON string value (most often when quoting source material), which
    breaks json.loads() with no recovery. Reproduced consistently across
    3 independent live calls for that exact deliverable before the fix.
    """

    def test_valid_json_on_first_attempt_makes_exactly_one_call(self, monkeypatch):
        calls = []

        def fake_call_claude(**kwargs):
            calls.append(kwargs)
            return _result('{"platforms": {"youtube": {"description": "d"}}}')

        monkeypatch.setattr("hypogeum.openrouter.call_claude", fake_call_claude)
        result = _default_llm_call("system", {"x": 1}, config=Config(), episode_id="ep_1", channel_id="c")
        assert result == {"platforms": {"youtube": {"description": "d"}}}
        assert len(calls) == 1

    def test_malformed_json_retries_and_recovers(self, monkeypatch):
        # First attempt returns an unescaped literal quote inside a string
        # value (invalid JSON); second attempt returns valid JSON.
        responses = [
            '{"platforms": {"youtube": {"description": "he said "hi" to me"}}}',
            '{"platforms": {"youtube": {"description": "he said \\"hi\\" to me"}}}',
        ]
        calls = []

        def fake_call_claude(**kwargs):
            calls.append(kwargs)
            return _result(responses[len(calls) - 1])

        monkeypatch.setattr("hypogeum.openrouter.call_claude", fake_call_claude)
        result = _default_llm_call("system", {"x": 1}, config=Config(), episode_id="ep_1", channel_id="c")
        assert result["platforms"]["youtube"]["description"] == 'he said "hi" to me'
        assert len(calls) == 2
        # The retry message must reach the model so it knows to correct itself.
        retry_messages = calls[1]["messages"]
        assert any("valid JSON" in m["content"] for m in retry_messages if m["role"] == "user")

    def test_persistently_malformed_json_raises_after_bounded_retries(self, monkeypatch):
        calls = []

        def fake_call_claude(**kwargs):
            calls.append(kwargs)
            return _result('{"platforms": {"youtube": {"description": "always "broken""}}}')

        monkeypatch.setattr("hypogeum.openrouter.call_claude", fake_call_claude)
        with pytest.raises(json.JSONDecodeError):
            _default_llm_call("system", {"x": 1}, config=Config(), episode_id="ep_1", channel_id="c")
        # Bounded, not infinite.
        assert 1 < len(calls) <= 5

    def test_code_fence_wrapped_json_still_parses(self, monkeypatch):
        def fake_call_claude(**kwargs):
            return _result('```json\n{"platforms": {"youtube": {"description": "d"}}}\n```')

        monkeypatch.setattr("hypogeum.openrouter.call_claude", fake_call_claude)
        result = _default_llm_call("system", {"x": 1}, config=Config(), episode_id="ep_1", channel_id="c")
        assert result == {"platforms": {"youtube": {"description": "d"}}}
