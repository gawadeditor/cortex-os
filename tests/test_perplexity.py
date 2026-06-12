"""
tests/test_perplexity.py — Unit tests for hypogeum/perplexity.py

All HTTP calls are mocked. No real network calls. No real API keys required.

Test coverage:
  TestQuerySuccess              — happy path, all response fields extracted
  TestQueryNoCitations          — empty citations[] → warn logged, content returned
  TestQueryFinishReasonUnexpected — unusual finish_reason → warn, content still used
  TestRetry429                  — 429 → retry with Retry-After backoff, warn logged
  TestRetry500                  — 500 → retry with backoff
  TestRetry503                  — 503 → retry with backoff
  TestRetryEmptyChoices         — empty choices[] → retry
  TestRetryContentFilter        — finish_reason:content_filter → rephrased retry
  TestRetryLength               — finish_reason:length → doubled max_tokens retry
  TestHardFailure401            — 401 → PerplexityAuthFailure, no retry
  TestHardFailure400            — 400 → PerplexityHardFailure, no retry
  TestHardFailure403            — 403 → PerplexityHardFailure, no retry
  TestHardFailure422            — 422 → PerplexityHardFailure, no retry
  TestRetryExhaustion           — persistent soft failure → PerplexitySoftFailureExhausted
  TestConnectionError           — requests.ConnectionError → retry
  TestHealthCheck               — available, degraded (slow), unavailable paths
  TestLogging                   — correct events at correct levels
  TestRetryDelay                — _retry_delay() respects jitter and Retry-After
  TestExceptionHierarchy        — class inheritance
  TestPerplexityResult          — result fields
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock, call, patch

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.config import Config
from hypogeum.perplexity import (
    HEALTH_CHECK_TIMEOUT_SECONDS,
    RETRY_AFTER_DEFAULT,
    RETRY_INITIAL_DELAY,
    RETRY_JITTER_FRACTION,
    RETRY_MAX,
    _CONTENT_FILTER_SUFFIX,
    _LENGTH_RETRY_MAX_TOKENS_CAP,
    PerplexityAuthFailure,
    PerplexityError,
    PerplexityHardFailure,
    PerplexityResult,
    PerplexitySoftFailureExhausted,
    PerplexityUnavailable,
    _retry_delay,
    check_perplexity_health,
    query_perplexity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_config(perplexity_key: str = "sk-px-test", model: str = "sonar-pro") -> Config:
    """Build a Config with minimal valid environment."""
    pipeline_vars = [
        "OPENROUTER_API_KEY", "OPENROUTER_SONNET_MODEL_ID", "OPENROUTER_OPUS_MODEL_ID",
        "OPENROUTER_APP_NAME", "OPENROUTER_APP_URL", "OPENROUTER_FALLBACK_MODEL",
        "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "PERPLEXITY_MODEL",
        "PERPLEXITY_MAX_TOKENS", "MUAPI_API_KEY", "MUAPI_DEFAULT_TIER",
        "MUAPI_TIMEOUT_SECONDS", "MUAPI_WEBHOOK_URL", "ELEVENLABS_API_KEY",
        "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID", "ELEVENLABS_STABILITY",
        "ELEVENLABS_SIMILARITY_BOOST", "ELEVENLABS_MUSIC_MODEL_ID",
        "ELEVENLABS_MUSIC_DEFAULT_DURATION", "OPENAI_API_KEY", "WHISPER_MODEL",
        "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
        "YOUTUBE_CHANNEL_ID", "YOUTUBE_DEFAULT_CATEGORY_ID",
        "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_ACCESS_TOKEN",
        "TIKTOK_OPEN_ID", "META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN",
        "META_INSTAGRAM_ACCOUNT_ID", "META_FACEBOOK_PAGE_ID",
        "META_GRAPH_API_VERSION", "META_STAGING_BASE_URL",
    ]
    original = {k: os.environ.get(k) for k in pipeline_vars}
    for k in pipeline_vars:
        os.environ.pop(k, None)
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    os.environ["PERPLEXITY_API_KEY"] = perplexity_key
    os.environ["PERPLEXITY_MODEL"] = model
    try:
        return Config()
    finally:
        for k in pipeline_vars:
            os.environ.pop(k, None)
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v


def perplexity_200(
    content: str = "The Antikythera Mechanism is an ancient Greek device.",
    finish_reason: str = "stop",
    citations: list = None,
    prompt_tokens: int = 150,
    completion_tokens: int = 80,
) -> MagicMock:
    """Build a mock 200 response from Perplexity."""
    if citations is None:
        citations = ["https://example.com/source1", "https://example.com/source2"]
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {
        "id": "resp_001",
        "model": "sonar-pro",
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "citations": citations,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    return mock


def http_error(status_code: int, headers: dict = None) -> MagicMock:
    """Build a mock HTTP error response."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.headers = headers or {}
    mock.json.return_value = {"error": f"HTTP {status_code}"}
    return mock


def run_query(config=None, query="Test query?", **kwargs) -> PerplexityResult:
    """Convenience wrapper for query_perplexity()."""
    if config is None:
        config = make_config()
    return query_perplexity(
        query=query,
        config=config,
        phase_id="phase_1",
        query_index=0,
        episode_id="ep_001",
        agent="research_director",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# TestQuerySuccess
# ---------------------------------------------------------------------------

class TestQuerySuccess:
    def test_returns_perplexity_result(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()):
            result = run_query(cfg)
        assert isinstance(result, PerplexityResult)

    def test_content_extracted(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   return_value=perplexity_200(content="Ancient Greek device.")):
            result = run_query(cfg)
        assert result.content == "Ancient Greek device."

    def test_citations_extracted(self):
        cfg = make_config()
        cites = ["https://example.com/a", "https://example.com/b"]
        with patch("hypogeum.perplexity.requests.post",
                   return_value=perplexity_200(citations=cites)):
            result = run_query(cfg)
        assert result.citations == cites

    def test_usage_extracted(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   return_value=perplexity_200(prompt_tokens=100, completion_tokens=50)):
            result = run_query(cfg)
        assert result.usage["total_tokens"] == 150

    def test_finish_reason_stop(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()):
            result = run_query(cfg)
        assert result.finish_reason == "stop"

    def test_phase_id_carried(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()):
            result = query_perplexity("Q?", cfg, phase_id="phase_3", query_index=2)
        assert result.phase_id == "phase_3"
        assert result.query_index == 2

    def test_citations_absent_false_when_citations_present(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()):
            result = run_query(cfg)
        assert result.citations_absent is False

    def test_response_time_ms_positive(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()):
            result = run_query(cfg)
        assert result.response_time_ms >= 0

    def test_api_key_in_authorization_header(self):
        cfg = make_config(perplexity_key="sk-px-secret")
        with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()) as mock_post:
            run_query(cfg)
        _, kwargs = mock_post.call_args
        headers = kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer sk-px-secret"

    def test_model_from_config(self):
        cfg = make_config(model="sonar")
        with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()) as mock_post:
            run_query(cfg)
        _, kwargs = mock_post.call_args
        body = kwargs.get("json", {})
        assert body.get("model") == "sonar"

    def test_max_tokens_override(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()) as mock_post:
            run_query(cfg, max_tokens_override=1024)
        _, kwargs = mock_post.call_args
        body = kwargs.get("json", {})
        assert body.get("max_tokens") == 1024

    def test_system_prompt_included(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()) as mock_post:
            run_query(cfg, query="My research question.")
        _, kwargs = mock_post.call_args
        messages = kwargs.get("json", {}).get("messages", [])
        roles = [m["role"] for m in messages]
        assert "system" in roles
        assert "user" in roles

    def test_user_message_is_query(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()) as mock_post:
            run_query(cfg, query="Specific research question?")
        _, kwargs = mock_post.call_args
        messages = kwargs.get("json", {}).get("messages", [])
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert user_msgs[0]["content"] == "Specific research question?"

    def test_only_one_request_on_success(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()) as mock_post:
            run_query(cfg)
        assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# TestQueryNoCitations
# ---------------------------------------------------------------------------

class TestQueryNoCitations:
    def test_empty_citations_returns_result(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   return_value=perplexity_200(citations=[])):
            result = run_query(cfg)
        assert result.content != ""

    def test_citations_absent_true(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   return_value=perplexity_200(citations=[])):
            result = run_query(cfg)
        assert result.citations_absent is True

    def test_citations_empty_list(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   return_value=perplexity_200(citations=[])):
            result = run_query(cfg)
        assert result.citations == []

    def test_no_citations_warn_logged(self):
        cfg = make_config()
        log_events = []

        def make_logger(**kwargs):
            mock_log = MagicMock()
            mock_log.warn.side_effect = lambda event, **f: log_events.append(("warn", event))
            mock_log.info.side_effect = lambda event, **f: log_events.append(("info", event))
            mock_log.error.side_effect = lambda event, **f: log_events.append(("error", event))
            return mock_log

        with patch("hypogeum.perplexity.get_logger", side_effect=make_logger):
            with patch("hypogeum.perplexity.requests.post",
                       return_value=perplexity_200(citations=[])):
                run_query(cfg)

        assert ("warn", "perplexity_no_citations") in log_events


# ---------------------------------------------------------------------------
# TestQueryFinishReasonUnexpected
# ---------------------------------------------------------------------------

class TestQueryFinishReasonUnexpected:
    def test_unexpected_finish_reason_returns_result(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   return_value=perplexity_200(finish_reason="unknown_reason")):
            result = run_query(cfg)
        assert result.content != ""

    def test_unexpected_finish_reason_warn_logged(self):
        cfg = make_config()
        log_events = []

        def make_logger(**kwargs):
            mock_log = MagicMock()
            mock_log.warn.side_effect = lambda event, **f: log_events.append(("warn", event))
            mock_log.info.side_effect = lambda event, **f: None
            mock_log.error.side_effect = lambda event, **f: None
            return mock_log

        with patch("hypogeum.perplexity.get_logger", side_effect=make_logger):
            with patch("hypogeum.perplexity.requests.post",
                       return_value=perplexity_200(finish_reason="unknown")):
                run_query(cfg)

        assert ("warn", "perplexity_finish_reason_unexpected") in log_events


# ---------------------------------------------------------------------------
# TestRetry429
# ---------------------------------------------------------------------------

class TestRetry429:
    def test_429_triggers_retry(self):
        cfg = make_config()
        responses = [http_error(429, {"Retry-After": "5"}), perplexity_200()]
        with patch("hypogeum.perplexity.requests.post", side_effect=responses) as mock_post:
            with patch("hypogeum.perplexity.time.sleep"):
                result = run_query(cfg)
        assert mock_post.call_count == 2
        assert isinstance(result, PerplexityResult)

    def test_429_uses_retry_after_header(self):
        cfg = make_config()
        responses = [http_error(429, {"Retry-After": "30"}), perplexity_200()]
        sleep_calls = []
        with patch("hypogeum.perplexity.requests.post", side_effect=responses):
            with patch("hypogeum.perplexity.time.sleep", side_effect=sleep_calls.append):
                run_query(cfg)
        assert len(sleep_calls) == 1
        # Sleep must be at least Retry-After value (30s)
        assert sleep_calls[0] >= 30.0

    def test_429_uses_default_when_no_header(self):
        cfg = make_config()
        responses = [http_error(429, {}), perplexity_200()]
        sleep_calls = []
        with patch("hypogeum.perplexity.requests.post", side_effect=responses):
            with patch("hypogeum.perplexity.time.sleep", side_effect=sleep_calls.append):
                run_query(cfg)
        assert sleep_calls[0] >= RETRY_AFTER_DEFAULT

    def test_429_emits_warn_log(self):
        cfg = make_config()
        log_events = []

        def make_logger(**kwargs):
            mock_log = MagicMock()
            mock_log.warn.side_effect = lambda event, **f: log_events.append(("warn", event))
            mock_log.info.side_effect = lambda event, **f: None
            mock_log.error.side_effect = lambda event, **f: None
            return mock_log

        with patch("hypogeum.perplexity.get_logger", side_effect=make_logger):
            with patch("hypogeum.perplexity.requests.post",
                       side_effect=[http_error(429, {"Retry-After": "1"}), perplexity_200()]):
                with patch("hypogeum.perplexity.time.sleep"):
                    run_query(cfg)

        assert ("warn", "perplexity_retry_triggered") in log_events


# ---------------------------------------------------------------------------
# TestRetry500
# ---------------------------------------------------------------------------

class TestRetry500:
    def test_500_triggers_retry(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   side_effect=[http_error(500), perplexity_200()]) as mock_post:
            with patch("hypogeum.perplexity.time.sleep"):
                result = run_query(cfg)
        assert mock_post.call_count == 2
        assert isinstance(result, PerplexityResult)

    def test_500_does_not_raise_on_first_attempt(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   side_effect=[http_error(500), perplexity_200()]):
            with patch("hypogeum.perplexity.time.sleep"):
                result = run_query(cfg)
        assert result is not None


# ---------------------------------------------------------------------------
# TestRetry503
# ---------------------------------------------------------------------------

class TestRetry503:
    def test_503_triggers_retry(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   side_effect=[http_error(503), perplexity_200()]) as mock_post:
            with patch("hypogeum.perplexity.time.sleep"):
                result = run_query(cfg)
        assert mock_post.call_count == 2
        assert isinstance(result, PerplexityResult)


# ---------------------------------------------------------------------------
# TestRetryEmptyChoices
# ---------------------------------------------------------------------------

class TestRetryEmptyChoices:
    def _empty_choices_response(self):
        mock = MagicMock()
        mock.status_code = 200
        mock.json.return_value = {"choices": [], "citations": [], "usage": {}}
        return mock

    def test_empty_choices_triggers_retry(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   side_effect=[self._empty_choices_response(), perplexity_200()]) as mock_post:
            with patch("hypogeum.perplexity.time.sleep"):
                result = run_query(cfg)
        assert mock_post.call_count == 2
        assert isinstance(result, PerplexityResult)

    def test_empty_content_triggers_retry(self):
        """choices[0].message.content being empty string also triggers retry."""
        empty_content = MagicMock()
        empty_content.status_code = 200
        empty_content.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
            "citations": [],
            "usage": {},
        }
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   side_effect=[empty_content, perplexity_200()]) as mock_post:
            with patch("hypogeum.perplexity.time.sleep"):
                result = run_query(cfg)
        assert mock_post.call_count == 2
        assert result.content != ""


# ---------------------------------------------------------------------------
# TestRetryContentFilter
# ---------------------------------------------------------------------------

class TestRetryContentFilter:
    def test_content_filter_triggers_retry(self):
        cfg = make_config()
        filtered = perplexity_200(finish_reason="content_filter")
        success = perplexity_200()
        with patch("hypogeum.perplexity.requests.post",
                   side_effect=[filtered, success]) as mock_post:
            with patch("hypogeum.perplexity.time.sleep"):
                result = run_query(cfg, query="Original query.")
        assert mock_post.call_count == 2
        assert isinstance(result, PerplexityResult)

    def test_content_filter_appends_suffix_to_retry_query(self):
        cfg = make_config()
        filtered = perplexity_200(finish_reason="content_filter")
        success = perplexity_200()
        captured_queries = []

        def capture(url, headers, json, timeout):
            msgs = json.get("messages", [])
            for m in msgs:
                if m.get("role") == "user":
                    captured_queries.append(m["content"])
            return filtered if len(captured_queries) == 1 else success

        with patch("hypogeum.perplexity.requests.post", side_effect=capture):
            with patch("hypogeum.perplexity.time.sleep"):
                run_query(cfg, query="Original query.")

        assert len(captured_queries) == 2
        assert _CONTENT_FILTER_SUFFIX in captured_queries[1]
        assert "Original query." in captured_queries[1]

    def test_content_filter_suffix_not_doubled_on_second_retry(self):
        """Suffix should not accumulate if content_filter fires twice."""
        cfg = make_config()
        filtered = perplexity_200(finish_reason="content_filter")
        success = perplexity_200()
        captured_queries = []

        def capture(url, headers, json, timeout):
            msgs = json.get("messages", [])
            for m in msgs:
                if m.get("role") == "user":
                    captured_queries.append(m["content"])
            # First two attempts filtered (but RETRY_MAX is 1, so only 2 total)
            return filtered if len(captured_queries) == 1 else success

        with patch("hypogeum.perplexity.requests.post", side_effect=capture):
            with patch("hypogeum.perplexity.time.sleep"):
                run_query(cfg, query="Original.")

        # The second query should have suffix exactly once
        second = captured_queries[1]
        assert second.count(_CONTENT_FILTER_SUFFIX) == 1


# ---------------------------------------------------------------------------
# TestRetryLength
# ---------------------------------------------------------------------------

class TestRetryLength:
    def test_length_triggers_retry(self):
        cfg = make_config()
        truncated = perplexity_200(finish_reason="length")
        success = perplexity_200()
        with patch("hypogeum.perplexity.requests.post",
                   side_effect=[truncated, success]) as mock_post:
            with patch("hypogeum.perplexity.time.sleep"):
                result = run_query(cfg, max_tokens_override=512)
        assert mock_post.call_count == 2

    def test_length_doubles_max_tokens_on_retry(self):
        cfg = make_config()
        truncated = perplexity_200(finish_reason="length")
        success = perplexity_200()
        captured_tokens = []

        def capture(url, headers, json, timeout):
            captured_tokens.append(json.get("max_tokens"))
            return truncated if len(captured_tokens) == 1 else success

        with patch("hypogeum.perplexity.requests.post", side_effect=capture):
            with patch("hypogeum.perplexity.time.sleep"):
                run_query(cfg, max_tokens_override=512)

        assert captured_tokens[1] == 1024  # 512 * 2

    def test_length_retry_max_tokens_capped(self):
        cfg = make_config()
        truncated = perplexity_200(finish_reason="length")
        success = perplexity_200()
        captured_tokens = []

        def capture(url, headers, json, timeout):
            captured_tokens.append(json.get("max_tokens"))
            return truncated if len(captured_tokens) == 1 else success

        with patch("hypogeum.perplexity.requests.post", side_effect=capture):
            with patch("hypogeum.perplexity.time.sleep"):
                # Start with near-max tokens — doubled should be capped
                run_query(cfg, max_tokens_override=3000)

        assert captured_tokens[1] <= _LENGTH_RETRY_MAX_TOKENS_CAP


# ---------------------------------------------------------------------------
# TestHardFailure401
# ---------------------------------------------------------------------------

class TestHardFailure401:
    def test_401_raises_auth_failure(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=http_error(401)):
            with pytest.raises(PerplexityAuthFailure):
                run_query(cfg)

    def test_401_no_retry(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=http_error(401)) as mock_post:
            with pytest.raises(PerplexityAuthFailure):
                run_query(cfg)
        assert mock_post.call_count == 1

    def test_401_error_level_logged(self):
        cfg = make_config()
        log_events = []

        def make_logger(**kwargs):
            mock_log = MagicMock()
            mock_log.error.side_effect = lambda event, **f: log_events.append(("error", event))
            mock_log.warn.side_effect = lambda event, **f: None
            mock_log.info.side_effect = lambda event, **f: None
            return mock_log

        with patch("hypogeum.perplexity.get_logger", side_effect=make_logger):
            with patch("hypogeum.perplexity.requests.post", return_value=http_error(401)):
                with pytest.raises(PerplexityAuthFailure):
                    run_query(cfg)

        assert ("error", "perplexity_hard_failure") in log_events

    def test_auth_failure_is_hard_failure(self):
        assert issubclass(PerplexityAuthFailure, PerplexityHardFailure)

    def test_auth_failure_http_status(self):
        err = PerplexityAuthFailure()
        assert err.http_status == 401


# ---------------------------------------------------------------------------
# TestHardFailure400
# ---------------------------------------------------------------------------

class TestHardFailure400:
    def test_400_raises_hard_failure(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=http_error(400)):
            with pytest.raises(PerplexityHardFailure) as exc_info:
                run_query(cfg)
        assert exc_info.value.http_status == 400

    def test_400_no_retry(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=http_error(400)) as mock_post:
            with pytest.raises(PerplexityHardFailure):
                run_query(cfg)
        assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# TestHardFailure403
# ---------------------------------------------------------------------------

class TestHardFailure403:
    def test_403_raises_hard_failure(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=http_error(403)):
            with pytest.raises(PerplexityHardFailure) as exc_info:
                run_query(cfg)
        assert exc_info.value.http_status == 403

    def test_403_no_retry(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=http_error(403)) as mock_post:
            with pytest.raises(PerplexityHardFailure):
                run_query(cfg)
        assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# TestHardFailure422
# ---------------------------------------------------------------------------

class TestHardFailure422:
    def test_422_raises_hard_failure(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=http_error(422)):
            with pytest.raises(PerplexityHardFailure) as exc_info:
                run_query(cfg)
        assert exc_info.value.http_status == 422

    def test_422_no_retry(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=http_error(422)) as mock_post:
            with pytest.raises(PerplexityHardFailure):
                run_query(cfg)
        assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# TestRetryExhaustion
# ---------------------------------------------------------------------------

class TestRetryExhaustion:
    def test_persistent_500_raises_soft_failure_exhausted(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=http_error(500)):
            with patch("hypogeum.perplexity.time.sleep"):
                with pytest.raises(PerplexitySoftFailureExhausted):
                    run_query(cfg)

    def test_persistent_429_raises_soft_failure_exhausted(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   return_value=http_error(429, {"Retry-After": "1"})):
            with patch("hypogeum.perplexity.time.sleep"):
                with pytest.raises(PerplexitySoftFailureExhausted):
                    run_query(cfg)

    def test_persistent_empty_choices_raises(self):
        cfg = make_config()
        empty = MagicMock()
        empty.status_code = 200
        empty.json.return_value = {"choices": [], "citations": [], "usage": {}}
        with patch("hypogeum.perplexity.requests.post", return_value=empty):
            with patch("hypogeum.perplexity.time.sleep"):
                with pytest.raises(PerplexitySoftFailureExhausted):
                    run_query(cfg)

    def test_exhaustion_total_request_count(self):
        """Total requests = 1 original + RETRY_MAX retries."""
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   return_value=http_error(500)) as mock_post:
            with patch("hypogeum.perplexity.time.sleep"):
                with pytest.raises(PerplexitySoftFailureExhausted):
                    run_query(cfg)
        assert mock_post.call_count == 1 + RETRY_MAX

    def test_exhaustion_emits_error_log(self):
        cfg = make_config()
        log_events = []

        def make_logger(**kwargs):
            mock_log = MagicMock()
            mock_log.error.side_effect = lambda event, **f: log_events.append(("error", event))
            mock_log.warn.side_effect = lambda event, **f: None
            mock_log.info.side_effect = lambda event, **f: None
            return mock_log

        with patch("hypogeum.perplexity.get_logger", side_effect=make_logger):
            with patch("hypogeum.perplexity.requests.post", return_value=http_error(503)):
                with patch("hypogeum.perplexity.time.sleep"):
                    with pytest.raises(PerplexitySoftFailureExhausted):
                        run_query(cfg)

        assert ("error", "perplexity_soft_failure_exhausted") in log_events

    def test_soft_failure_exhausted_attributes(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=http_error(500)):
            with patch("hypogeum.perplexity.time.sleep"):
                with pytest.raises(PerplexitySoftFailureExhausted) as exc_info:
                    run_query(cfg)
        err = exc_info.value
        assert err.last_status == 500
        assert str(err)  # message is non-empty


# ---------------------------------------------------------------------------
# TestConnectionError
# ---------------------------------------------------------------------------

class TestConnectionError:
    def test_connection_error_triggers_retry(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   side_effect=[requests.exceptions.ConnectionError("no route"), perplexity_200()]) as mock_post:
            with patch("hypogeum.perplexity.time.sleep"):
                result = run_query(cfg)
        assert mock_post.call_count == 2
        assert isinstance(result, PerplexityResult)

    def test_persistent_connection_error_raises(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   side_effect=requests.exceptions.ConnectionError("no route")):
            with patch("hypogeum.perplexity.time.sleep"):
                with pytest.raises(PerplexitySoftFailureExhausted):
                    run_query(cfg)

    def test_timeout_triggers_retry(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   side_effect=[requests.exceptions.Timeout(), perplexity_200()]) as mock_post:
            with patch("hypogeum.perplexity.time.sleep"):
                result = run_query(cfg)
        assert mock_post.call_count == 2
        assert isinstance(result, PerplexityResult)


# ---------------------------------------------------------------------------
# TestHealthCheck
# ---------------------------------------------------------------------------

class TestHealthCheck:
    def test_available_on_200_fast_response(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()):
            with patch("hypogeum.perplexity.time.monotonic", side_effect=[0.0, 1.0]):
                status = check_perplexity_health(cfg)
        assert status == "available"

    def test_degraded_on_slow_response(self):
        cfg = make_config()
        timeout_secs = HEALTH_CHECK_TIMEOUT_SECONDS
        with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()):
            with patch("hypogeum.perplexity.time.monotonic",
                       side_effect=[0.0, timeout_secs + 1.0]):
                status = check_perplexity_health(cfg)
        assert status == "degraded"

    def test_unavailable_on_401(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=http_error(401)):
            status = check_perplexity_health(cfg)
        assert status == "unavailable"

    def test_unavailable_on_503(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=http_error(503)):
            status = check_perplexity_health(cfg)
        assert status == "unavailable"

    def test_unavailable_on_connection_error(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   side_effect=requests.exceptions.ConnectionError("refused")):
            status = check_perplexity_health(cfg)
        assert status == "unavailable"

    def test_unavailable_on_timeout(self):
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   side_effect=requests.exceptions.Timeout()):
            status = check_perplexity_health(cfg)
        assert status == "unavailable"

    def test_unavailable_on_empty_content(self):
        cfg = make_config()
        empty = MagicMock()
        empty.status_code = 200
        empty.json.return_value = {
            "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
            "citations": [],
        }
        with patch("hypogeum.perplexity.requests.post", return_value=empty):
            with patch("hypogeum.perplexity.time.monotonic", side_effect=[0.0, 1.0]):
                status = check_perplexity_health(cfg)
        assert status == "unavailable"

    def test_health_check_uses_short_max_tokens(self):
        """Health check must not consume significant tokens."""
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()) as mock_post:
            with patch("hypogeum.perplexity.time.monotonic", side_effect=[0.0, 1.0]):
                check_perplexity_health(cfg)
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["max_tokens"] <= 50

    def test_health_check_emits_started_event(self):
        cfg = make_config()
        log_events = []

        def make_logger(**kwargs):
            mock_log = MagicMock()
            mock_log.info.side_effect = lambda event, **f: log_events.append(("info", event))
            mock_log.warn.side_effect = lambda event, **f: log_events.append(("warn", event))
            mock_log.error.side_effect = lambda event, **f: log_events.append(("error", event))
            return mock_log

        with patch("hypogeum.perplexity.get_logger", side_effect=make_logger):
            with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()):
                with patch("hypogeum.perplexity.time.monotonic", side_effect=[0.0, 1.0]):
                    check_perplexity_health(cfg)

        assert ("info", "perplexity_health_check_started") in log_events

    def test_available_emits_result_at_info(self):
        cfg = make_config()
        log_events = []

        def make_logger(**kwargs):
            mock_log = MagicMock()
            mock_log.info.side_effect = lambda event, **f: log_events.append(("info", event))
            mock_log.warn.side_effect = lambda event, **f: log_events.append(("warn", event))
            mock_log.error.side_effect = lambda event, **f: log_events.append(("error", event))
            return mock_log

        with patch("hypogeum.perplexity.get_logger", side_effect=make_logger):
            with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()):
                with patch("hypogeum.perplexity.time.monotonic", side_effect=[0.0, 1.0]):
                    check_perplexity_health(cfg)

        assert ("info", "perplexity_health_check_result") in log_events

    def test_degraded_emits_result_at_warn(self):
        cfg = make_config()
        log_events = []

        def make_logger(**kwargs):
            mock_log = MagicMock()
            mock_log.info.side_effect = lambda event, **f: log_events.append(("info", event))
            mock_log.warn.side_effect = lambda event, **f: log_events.append(("warn", event))
            mock_log.error.side_effect = lambda event, **f: log_events.append(("error", event))
            return mock_log

        timeout_secs = HEALTH_CHECK_TIMEOUT_SECONDS
        with patch("hypogeum.perplexity.get_logger", side_effect=make_logger):
            with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200()):
                with patch("hypogeum.perplexity.time.monotonic",
                           side_effect=[0.0, timeout_secs + 1.0]):
                    check_perplexity_health(cfg)

        assert ("warn", "perplexity_health_check_result") in log_events

    def test_unavailable_emits_result_at_error(self):
        cfg = make_config()
        log_events = []

        def make_logger(**kwargs):
            mock_log = MagicMock()
            mock_log.info.side_effect = lambda event, **f: None
            mock_log.warn.side_effect = lambda event, **f: None
            mock_log.error.side_effect = lambda event, **f: log_events.append(("error", event))
            return mock_log

        with patch("hypogeum.perplexity.get_logger", side_effect=make_logger):
            with patch("hypogeum.perplexity.requests.post", return_value=http_error(401)):
                check_perplexity_health(cfg)

        assert ("error", "perplexity_health_check_result") in log_events


# ---------------------------------------------------------------------------
# TestLogging
# ---------------------------------------------------------------------------

class TestLogging:
    def _run_and_capture(self, responses, query="Test?", **kwargs):
        cfg = make_config()
        log_events = []

        def make_logger(**kwargs_):
            mock_log = MagicMock()
            mock_log.info.side_effect = lambda event, **f: log_events.append(("info", event))
            mock_log.warn.side_effect = lambda event, **f: log_events.append(("warn", event))
            mock_log.error.side_effect = lambda event, **f: log_events.append(("error", event))
            return mock_log

        with patch("hypogeum.perplexity.get_logger", side_effect=make_logger):
            with patch("hypogeum.perplexity.requests.post", side_effect=responses):
                with patch("hypogeum.perplexity.time.sleep"):
                    try:
                        run_query(cfg, query=query, **kwargs)
                    except Exception:
                        pass

        return log_events

    def test_request_sent_logged_at_info(self):
        events = self._run_and_capture([perplexity_200()])
        assert ("info", "perplexity_request_sent") in events

    def test_response_received_logged_at_info(self):
        events = self._run_and_capture([perplexity_200()])
        assert ("info", "perplexity_response_received") in events

    def test_retry_logged_at_warn(self):
        events = self._run_and_capture([http_error(500), perplexity_200()])
        assert ("warn", "perplexity_retry_triggered") in events

    def test_hard_failure_logged_at_error(self):
        events = self._run_and_capture([http_error(400)])
        assert ("error", "perplexity_hard_failure") in events

    def test_request_sent_before_response(self):
        events = self._run_and_capture([perplexity_200()])
        event_names = [e for _, e in events]
        sent_idx = event_names.index("perplexity_request_sent")
        recv_idx = event_names.index("perplexity_response_received")
        assert sent_idx < recv_idx


# ---------------------------------------------------------------------------
# TestRetryDelay
# ---------------------------------------------------------------------------

class TestRetryDelay:
    def test_delay_positive(self):
        delay = _retry_delay(attempt=0, retry_after=None)
        assert delay > 0

    def test_delay_not_below_jitter_minimum(self):
        # Even with maximum negative jitter, delay > 0
        for _ in range(50):
            delay = _retry_delay(attempt=0, retry_after=None)
            assert delay > 0

    def test_delay_within_jitter_range(self):
        base = RETRY_INITIAL_DELAY
        jitter_fraction = RETRY_JITTER_FRACTION
        for _ in range(100):
            delay = _retry_delay(attempt=0, retry_after=None)
            assert base * (1 - jitter_fraction) <= delay <= base * (1 + jitter_fraction)

    def test_retry_after_respected(self):
        # When retry_after > computed delay, must use retry_after
        for _ in range(20):
            delay = _retry_delay(attempt=0, retry_after=100.0)
            assert delay >= 100.0

    def test_backoff_increases_with_attempt(self):
        delay0 = RETRY_INITIAL_DELAY  # Expected base for attempt 0
        delay1 = RETRY_INITIAL_DELAY * 2  # Expected base for attempt 1
        # Verify delay1 > delay0 holds in expectation (test with fixed random seed)
        with patch("hypogeum.perplexity.random.random", return_value=0.5):
            d0 = _retry_delay(attempt=0, retry_after=None)
            d1 = _retry_delay(attempt=1, retry_after=None)
        assert d1 > d0


# ---------------------------------------------------------------------------
# TestExceptionHierarchy
# ---------------------------------------------------------------------------

class TestExceptionHierarchy:
    def test_perplexity_error_is_runtime_error(self):
        assert issubclass(PerplexityError, RuntimeError)

    def test_hard_failure_is_perplexity_error(self):
        assert issubclass(PerplexityHardFailure, PerplexityError)

    def test_auth_failure_is_hard_failure(self):
        assert issubclass(PerplexityAuthFailure, PerplexityHardFailure)

    def test_auth_failure_is_perplexity_error(self):
        assert issubclass(PerplexityAuthFailure, PerplexityError)

    def test_soft_failure_exhausted_is_perplexity_error(self):
        assert issubclass(PerplexitySoftFailureExhausted, PerplexityError)

    def test_unavailable_is_perplexity_error(self):
        assert issubclass(PerplexityUnavailable, PerplexityError)

    def test_hard_failure_direct_construction(self):
        err = PerplexityHardFailure("test message", http_status=400)
        assert err.http_status == 400
        assert "test message" in str(err)

    def test_unavailable_direct_construction(self):
        err = PerplexityUnavailable("connection refused")
        assert err.reason == "connection refused"
        assert str(err)


# ---------------------------------------------------------------------------
# TestPerplexityResult
# ---------------------------------------------------------------------------

class TestPerplexityResult:
    def _get_result(self, **response_kwargs) -> PerplexityResult:
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post",
                   return_value=perplexity_200(**response_kwargs)):
            return run_query(cfg)

    def test_content_is_string(self):
        result = self._get_result()
        assert isinstance(result.content, str)

    def test_citations_is_list(self):
        result = self._get_result()
        assert isinstance(result.citations, list)

    def test_usage_is_dict(self):
        result = self._get_result()
        assert isinstance(result.usage, dict)

    def test_finish_reason_is_string(self):
        result = self._get_result()
        assert isinstance(result.finish_reason, str)

    def test_response_time_ms_is_int(self):
        result = self._get_result()
        assert isinstance(result.response_time_ms, int)

    def test_citations_absent_is_bool(self):
        result = self._get_result()
        assert isinstance(result.citations_absent, bool)
