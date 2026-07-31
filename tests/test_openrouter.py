"""
tests/test_openrouter.py — Unit tests for hypogeum/openrouter.py

All OpenRouter and Anthropic HTTP calls are mocked — no real network requests.
Each test class focuses on one concern.

Test coverage:
  TestSuccessfulPath         — happy-path call_claude() via OpenRouter
  TestRetryBehavior          — 429/5xx triggers retry; exhaustion triggers fallback
  TestNetworkExceptionRetry  — ReadTimeout/ConnectionError trigger retry+fallback; non-network errors don't
  TestAuthFailureFallback    — 401 triggers immediate Anthropic direct fallback
  TestHardFailures           — 400/402/403/content_filter halt without retry
  TestTruncatedResponse      — finish_reason: length raises TruncatedResponse
  TestEmptyResponse          — empty content raises EmptyResponse
  TestMalformedResponse      — missing/bad keys in response body raise HardFailure
  TestTokenAccounting        — usage dict is correctly extracted and surfaced
  TestCostEstimation         — estimated_cost_usd matches known token counts
  TestAnthropicFallback      — Anthropic direct request format and slug translation
  TestFallbackNoKey          — FallbackFailed when ANTHROPIC_API_KEY missing
  TestHealthCheck            — check_health() routes and logs correctly
  TestRunState               — reset_run_state() clears the fallback flag
  TestLogging                — log events emitted at correct levels
  TestRetryDelay             — _retry_delay() bounds and jitter
  TestSlugTranslation        — _translate_slug_to_anthropic_id()

Run with:  python3 -m pytest tests/test_openrouter.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hypogeum.openrouter as openrouter_mod
from hypogeum.config import Config
from hypogeum.openrouter import (
    ANTHROPIC_MESSAGES_URL,
    OPENROUTER_CHAT_URL,
    OPENROUTER_MODELS_URL,
    CallResult,
    ContentFiltered,
    CreditsExhausted,
    EmptyResponse,
    FallbackFailed,
    HardFailure,
    TruncatedResponse,
    _apply_system_prompt_caching,
    _cache_control_supported,
    _estimate_cost,
    _retry_delay,
    _translate_slug_to_anthropic_id,
    call_claude,
    check_health,
    reset_run_state,
)


# ---------------------------------------------------------------------------
# Test fixtures and helpers
# ---------------------------------------------------------------------------

SONNET = "anthropic/claude-sonnet-4.6"
OPUS   = "anthropic/claude-opus-4.6"
# Real default model_slug used by config.TD_PLANNING_MODEL (Thumbnail Prompt
# Writer) — a live, non-Anthropic example already present in this codebase,
# not a hypothetical, for verifying unsupported-model behavior.
NON_ANTHROPIC_MODEL = "openai/gpt-5.5-pro"

MINIMAL_MESSAGES = [
    {"role": "system", "content": "You are a helpful pipeline agent."},
    {"role": "user",   "content": "Respond with: OK"},
]


def tmp_log() -> Path:
    """Return a path to a fresh temporary log file."""
    fd, path = tempfile.mkstemp(suffix=".log", prefix="openrouter_test_")
    os.close(fd)
    os.unlink(path)
    return Path(path)


def read_log_lines(path: Path) -> list[dict]:
    """Parse all JSONL lines from a log file."""
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def make_config(extra: Optional[dict] = None) -> Config:
    """
    Build a Config with minimal valid environment.
    Patches os.environ so Config.__init__ sees only the supplied vars.
    """
    env = {
        "OPENROUTER_API_KEY": "sk-or-test",
        "ANTHROPIC_API_KEY": "sk-ant-test",
    }
    if extra:
        env.update(extra)

    # Config reads from os.environ; patch it cleanly
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
    os.environ.update(env)
    try:
        return Config()
    finally:
        for k in pipeline_vars:
            os.environ.pop(k, None)
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v


def openrouter_200(content: str = "Test response.", finish_reason: str = "stop") -> MagicMock:
    """Build a mock requests.Response for a successful OpenRouter call."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "id": "chatcmpl-test",
        "model": SONNET,
        "choices": [{
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 10,
            "total_tokens": 60,
        },
    }
    return resp


def anthropic_200(content: str = "Test response.", stop_reason: str = "end_turn") -> MagicMock:
    """Build a mock requests.Response for Anthropic direct."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "id": "msg_test",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": content}],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 50, "output_tokens": 10},
    }
    return resp


def http_error(status_code: int, headers: Optional[dict] = None) -> MagicMock:
    """Build a mock error response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = f"HTTP {status_code} error"
    resp.headers = headers or {}
    return resp


# ---------------------------------------------------------------------------
# Auto-reset run state before every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_state():
    reset_run_state()
    yield
    reset_run_state()


# ---------------------------------------------------------------------------
# TestSuccessfulPath
# ---------------------------------------------------------------------------

class TestSuccessfulPath:
    def test_returns_call_result(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()) as mock_post:
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert isinstance(result, CallResult)

    def test_content_extracted(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200("hello world")):
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.content == "hello world"

    def test_finish_reason_stop(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()):
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.finish_reason == "stop"

    def test_via_fallback_is_false(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()):
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.via_fallback is False

    def test_response_time_ms_is_positive(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()):
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.response_time_ms >= 0

    def test_model_used_from_response(self):
        cfg = make_config()
        resp = openrouter_200()
        resp.json.return_value["model"] = "anthropic/claude-sonnet-4.6-routed"
        with patch("hypogeum.openrouter.requests.post", return_value=resp):
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.model_used == "anthropic/claude-sonnet-4.6-routed"

    def test_temperature_zero_in_request(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        body = mock_post.call_args.kwargs["json"]
        assert body["temperature"] == 0

    def test_authorization_header_set(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-or-test"

    def test_optional_referer_header_when_set(self):
        cfg = make_config({"OPENROUTER_APP_URL": "https://example.com"})
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        headers = mock_post.call_args.kwargs["headers"]
        assert headers.get("HTTP-Referer") == "https://example.com"

    def test_no_referer_header_when_absent(self):
        cfg = make_config()  # no OPENROUTER_APP_URL
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        headers = mock_post.call_args.kwargs["headers"]
        assert "HTTP-Referer" not in headers

    def test_x_title_header_set(self):
        cfg = make_config()  # default OPENROUTER_APP_NAME = "hypogeum-brain"
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        headers = mock_post.call_args.kwargs["headers"]
        assert headers.get("X-Title") == "hypogeum-brain"

    def test_model_slug_in_request_body(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()) as mock_post:
            call_claude(OPUS, MINIMAL_MESSAGES, 1024, cfg)
        body = mock_post.call_args.kwargs["json"]
        assert body["model"] == OPUS

    def test_max_tokens_in_request_body(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 2048, cfg)
        body = mock_post.call_args.kwargs["json"]
        assert body["max_tokens"] == 2048

    def test_posts_to_correct_url(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert mock_post.call_args.args[0] == OPENROUTER_CHAT_URL


# ---------------------------------------------------------------------------
# TestTokenAccounting
# ---------------------------------------------------------------------------

class TestTokenAccounting:
    def test_usage_dict_extracted(self):
        cfg = make_config()
        resp = openrouter_200()
        resp.json.return_value["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
        }
        with patch("hypogeum.openrouter.requests.post", return_value=resp):
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.usage["prompt_tokens"] == 100
        assert result.usage["completion_tokens"] == 200
        assert result.usage["total_tokens"] == 300

    def test_usage_dict_has_all_keys(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()):
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert "prompt_tokens" in result.usage
        assert "completion_tokens" in result.usage
        assert "total_tokens" in result.usage

    def test_usage_missing_gracefully_defaults_to_zero(self):
        cfg = make_config()
        resp = openrouter_200()
        resp.json.return_value["usage"] = {}
        with patch("hypogeum.openrouter.requests.post", return_value=resp):
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.usage["prompt_tokens"] == 0
        assert result.usage["completion_tokens"] == 0


# ---------------------------------------------------------------------------
# TestCostEstimation
# ---------------------------------------------------------------------------

class TestCostEstimation:
    def test_sonnet_cost_known_tokens(self):
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
        cost = _estimate_cost(SONNET, usage)
        # 1M prompt @ $3/M + 1M completion @ $15/M = $18
        assert abs(cost - 18.0) < 0.001

    def test_opus_cost_known_tokens(self):
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
        cost = _estimate_cost(OPUS, usage)
        # 1M prompt @ $15/M + 1M completion @ $75/M = $90
        assert abs(cost - 90.0) < 0.001

    def test_unknown_model_returns_zero(self):
        usage = {"prompt_tokens": 1000, "completion_tokens": 500}
        assert _estimate_cost("unknown/model-xyz", usage) == 0.0

    def test_cost_in_result(self):
        cfg = make_config()
        resp = openrouter_200()
        resp.json.return_value["usage"] = {
            "prompt_tokens": 50,
            "completion_tokens": 10,
            "total_tokens": 60,
        }
        with patch("hypogeum.openrouter.requests.post", return_value=resp):
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        expected = 50 * 3e-6 + 10 * 15e-6
        assert abs(result.estimated_cost_usd - expected) < 1e-9


# ---------------------------------------------------------------------------
# TestRetryBehavior
# ---------------------------------------------------------------------------

class TestRetryBehavior:
    def test_429_then_200_succeeds(self):
        cfg = make_config()
        responses = [http_error(429), openrouter_200("ok")]
        with patch("hypogeum.openrouter.requests.post", side_effect=responses):
            with patch("hypogeum.openrouter.time.sleep"):  # don't actually wait
                result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.content == "ok"

    def test_500_then_200_succeeds(self):
        cfg = make_config()
        responses = [http_error(500), openrouter_200("recovered")]
        with patch("hypogeum.openrouter.requests.post", side_effect=responses):
            with patch("hypogeum.openrouter.time.sleep"):
                result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.content == "recovered"

    def test_exactly_one_retry_attempted(self):
        """With RETRY_MAX=1, a 429 triggers one retry — total 2 POST calls."""
        cfg = make_config()
        responses = [http_error(429), openrouter_200()]
        with patch("hypogeum.openrouter.requests.post", side_effect=responses) as mock_post:
            with patch("hypogeum.openrouter.time.sleep"):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert mock_post.call_count == 2

    def test_retry_exhaustion_calls_openrouter_fallback(self):
        """500 twice (initial + 1 retry) → fallback to OPENROUTER_FALLBACK_MODEL, not Anthropic direct."""
        cfg = make_config()
        with patch.object(openrouter_mod, "_call_openrouter", side_effect=openrouter_mod._RetryableError(500)) as mock_or:
            with patch.object(openrouter_mod, "_try_openrouter_model_fallback", return_value=CallResult(
                content="fallback", usage={}, model_used=SONNET,
                finish_reason="stop", response_time_ms=0,
                via_fallback=False, estimated_cost_usd=0.0,
            )) as mock_fb:
                with patch("hypogeum.openrouter.time.sleep"):
                    result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.content == "fallback"
        assert result.via_fallback is False
        assert mock_fb.call_count == 1

    def test_retry_exhaustion_does_not_set_anthropic_flag(self):
        """Exhaustion must not flip the run into Anthropic-direct-for-all-calls mode anymore."""
        cfg = make_config()
        with patch.object(openrouter_mod, "_call_openrouter", side_effect=openrouter_mod._RetryableError(500)):
            with patch.object(openrouter_mod, "_try_openrouter_model_fallback", return_value=CallResult(
                content="ok", usage={}, model_used=SONNET,
                finish_reason="stop", response_time_ms=0,
                via_fallback=False, estimated_cost_usd=0.0,
            )):
                with patch("hypogeum.openrouter.time.sleep"):
                    call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert openrouter_mod._use_anthropic_for_run is False

    def test_retry_after_header_respected(self):
        """429 with Retry-After: 10 should delay at least 10 seconds."""
        cfg = make_config()
        err_resp = http_error(429, headers={"Retry-After": "10"})
        responses = [err_resp, openrouter_200()]
        delays_recorded = []

        def capture_sleep(secs):
            delays_recorded.append(secs)

        with patch("hypogeum.openrouter.requests.post", side_effect=responses):
            with patch("hypogeum.openrouter.time.sleep", side_effect=capture_sleep):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

        assert len(delays_recorded) == 1
        assert delays_recorded[0] >= 10.0  # floored at Retry-After value


# ---------------------------------------------------------------------------
# TestNetworkExceptionRetry
#
# Regression coverage for the exception-classification gap found in the
# 2026-07-31 production run: a mid-response read timeout surfaced as
# requests.exceptions.ConnectionError (wrapping the urllib3 read-timeout),
# not requests.exceptions.Timeout, and slipped past the retry/fallback
# wrapper entirely because the except clause only listed
# (_RetryableError, requests.Timeout). Fixed by widening the catch to
# requests.exceptions.RequestException, the common base for both.
# ---------------------------------------------------------------------------

class TestNetworkExceptionRetry:
    def test_read_timeout_triggers_retry(self):
        """A plain requests.exceptions.ReadTimeout is retried, not fatal."""
        cfg = make_config()
        import requests as req_lib
        responses = [
            req_lib.exceptions.ReadTimeout("Read timed out. (read timeout=120)"),
            openrouter_200("recovered"),
        ]
        with patch("hypogeum.openrouter.requests.post", side_effect=responses) as mock_post:
            with patch("hypogeum.openrouter.time.sleep"):
                result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.content == "recovered"
        assert mock_post.call_count == 2

    def test_connection_error_wrapping_read_timeout_triggers_retry(self):
        """
        requests.exceptions.ConnectionError with a urllib3-style read-timeout
        message (the exact failure mode observed in production) is retried,
        not left to escape as a fatal, un-retried episode failure.
        """
        cfg = make_config()
        import requests as req_lib
        responses = [
            req_lib.exceptions.ConnectionError(
                "HTTPSConnectionPool(host='openrouter.ai', port=443): Read timed out."
            ),
            openrouter_200("recovered"),
        ]
        with patch("hypogeum.openrouter.requests.post", side_effect=responses) as mock_post:
            with patch("hypogeum.openrouter.time.sleep"):
                result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.content == "recovered"
        assert mock_post.call_count == 2

    def test_connection_error_exhaustion_falls_back_to_openrouter_fallback_model(self):
        """Two consecutive ConnectionErrors (retry budget exhausted) fall back to OPENROUTER_FALLBACK_MODEL — the same behavior _RetryableError already gets. Never Anthropic direct (see TestOpenRouterModelFallback)."""
        cfg = make_config()
        import requests as req_lib
        with patch.object(
            openrouter_mod, "_call_openrouter",
            side_effect=req_lib.exceptions.ConnectionError(
                "HTTPSConnectionPool(host='openrouter.ai', port=443): Read timed out."
            ),
        ):
            with patch.object(openrouter_mod, "_try_openrouter_model_fallback", return_value=CallResult(
                content="fallback", usage={}, model_used=SONNET,
                finish_reason="stop", response_time_ms=0,
                via_fallback=False, estimated_cost_usd=0.0,
            )) as mock_fb:
                with patch("hypogeum.openrouter.time.sleep"):
                    result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.content == "fallback"
        assert result.via_fallback is False
        assert mock_fb.call_count == 1

    def test_non_network_error_not_retried(self):
        """
        A non-network exception (e.g. a bug surfacing as ValueError) must
        propagate immediately — proves the widened except clause is scoped
        to requests.exceptions.RequestException and does not turn into a
        catch-all that retries or falls back on unrelated failures.
        """
        cfg = make_config()
        with patch.object(openrouter_mod, "_call_openrouter", side_effect=ValueError("not a network error")):
            with patch.object(openrouter_mod, "_call_anthropic_direct") as mock_ant:
                with pytest.raises(ValueError):
                    call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert mock_ant.call_count == 0

    def test_hard_failure_still_not_retried(self):
        """HTTP 400 (HardFailure) is unaffected by the widened network-exception catch — still exactly one POST, no retry."""
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=http_error(400)) as mock_post:
            with pytest.raises(HardFailure):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# TestOpenRouterModelFallback
#
# The fallback target was moved from Anthropic direct to
# config.OPENROUTER_FALLBACK_MODEL — one attempt, same OpenRouter endpoint
# and credential, different model slug (docs/provider-model-director.md
# §8.3). Anthropic direct must never be reached automatically.
# ---------------------------------------------------------------------------

FALLBACK_MODEL = "openai/gpt-5.5-pro"  # live, non-Anthropic slug already used elsewhere in this repo


class TestOpenRouterModelFallback:
    def test_retry_exhaustion_activates_configured_fallback_model(self):
        """Primary model exhausts its retry budget; OPENROUTER_FALLBACK_MODEL is attempted through OpenRouter."""
        cfg = make_config({"OPENROUTER_FALLBACK_MODEL": FALLBACK_MODEL})
        responses = [http_error(500), http_error(500), openrouter_200("from fallback model")]
        with patch("hypogeum.openrouter.requests.post", side_effect=responses) as mock_post:
            with patch("hypogeum.openrouter.time.sleep"):
                result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.content == "from fallback model"
        assert mock_post.call_count == 3  # primary + 1 retry + 1 fallback-model attempt
        # Fallback call must hit the same OpenRouter endpoint with the fallback slug
        last_call = mock_post.call_args_list[-1]
        assert last_call.args[0] == OPENROUTER_CHAT_URL
        assert last_call.kwargs["json"]["model"] == FALLBACK_MODEL
        assert last_call.kwargs["headers"]["Authorization"] == "Bearer sk-or-test"

    def test_fallback_model_success_returns_normal_result(self):
        """A successful fallback-model call returns a normal CallResult — still OpenRouter, not Anthropic."""
        cfg = make_config({"OPENROUTER_FALLBACK_MODEL": FALLBACK_MODEL})
        responses = [http_error(500), http_error(500), openrouter_200("recovered")]
        with patch("hypogeum.openrouter.requests.post", side_effect=responses):
            with patch("hypogeum.openrouter.time.sleep"):
                result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.content == "recovered"
        assert result.via_fallback is False

    def test_fallback_model_failure_raises_retryable_fallback_failed(self):
        """If the fallback model also fails: FallbackFailed, not TERMINAL — retry-eligible at the episode/queue level."""
        cfg = make_config({"OPENROUTER_FALLBACK_MODEL": FALLBACK_MODEL})
        responses = [http_error(500), http_error(500), http_error(500)]
        with patch("hypogeum.openrouter.requests.post", side_effect=responses) as mock_post:
            with patch("hypogeum.openrouter.time.sleep"):
                with pytest.raises(FallbackFailed) as exc_info:
                    call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert mock_post.call_count == 3  # primary + 1 retry + 1 fallback-model attempt, then stop
        from hypogeum.failure_policy import FailurePolicy
        assert getattr(exc_info.value, "failure_policy", FailurePolicy.TRANSIENT) == FailurePolicy.TRANSIENT

    def test_missing_fallback_config_fails_safely_without_anthropic(self):
        """No OPENROUTER_FALLBACK_MODEL configured: fail safely after primary exhaustion, no third call, no Anthropic."""
        cfg = make_config()  # OPENROUTER_FALLBACK_MODEL intentionally absent
        responses = [http_error(500), http_error(500)]
        with patch("hypogeum.openrouter.requests.post", side_effect=responses) as mock_post:
            with patch("hypogeum.openrouter.time.sleep"):
                with pytest.raises(FallbackFailed):
                    call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert mock_post.call_count == 2  # primary + 1 retry only — no fallback attempt possible
        assert ANTHROPIC_MESSAGES_URL not in str(mock_post.call_args_list)

    def test_auth_failure_does_not_attempt_fallback_model(self):
        """401 must not trigger a second call with a different model through the same invalid credential."""
        cfg = make_config({"OPENROUTER_FALLBACK_MODEL": FALLBACK_MODEL})
        with patch("hypogeum.openrouter.requests.post", return_value=http_error(401)) as mock_post:
            with pytest.raises(FallbackFailed):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert mock_post.call_count == 1  # no fallback-model attempt, no Anthropic

    def test_anthropic_direct_not_called_by_default_on_retry_exhaustion(self):
        cfg = make_config({"OPENROUTER_FALLBACK_MODEL": FALLBACK_MODEL})
        responses = [http_error(500), http_error(500), openrouter_200()]
        with patch("hypogeum.openrouter.requests.post", side_effect=responses) as mock_post:
            with patch("hypogeum.openrouter.time.sleep"):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        for c in mock_post.call_args_list:
            assert ANTHROPIC_MESSAGES_URL not in str(c)
        assert openrouter_mod._use_anthropic_for_run is False

    def test_anthropic_direct_not_called_by_default_on_401(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=http_error(401)) as mock_post:
            with pytest.raises(FallbackFailed):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert ANTHROPIC_MESSAGES_URL not in str(mock_post.call_args_list)
        assert openrouter_mod._use_anthropic_for_run is False


# ---------------------------------------------------------------------------
# TestAuthFailureFallback
# ---------------------------------------------------------------------------

class TestAuthFailureFallback:
    def test_401_raises_fallback_failed_not_anthropic(self):
        """401 no longer activates Anthropic direct — it fails safely instead."""
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=http_error(401)):
            with pytest.raises(FallbackFailed):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

    def test_401_makes_exactly_one_call(self):
        """401 triggers no retry and no fallback-model attempt — exactly one POST."""
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=http_error(401)) as mock_post:
            with pytest.raises(FallbackFailed):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert mock_post.call_count == 1

    def test_401_does_not_set_run_fallback_flag(self):
        """401 must not flip the run into Anthropic-direct-for-all-calls mode anymore."""
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=http_error(401)):
            with pytest.raises(FallbackFailed):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert openrouter_mod._use_anthropic_for_run is False

    def test_health_check_flag_still_routes_to_anthropic_direct(self):
        """
        _use_anthropic_for_run is set only by check_health() now (unchanged,
        out of scope for this fix) — not by call_claude()'s own 401/retry
        handling anymore. If something else sets it, the top-of-function
        shortcut must still honor it.
        """
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200()) as mock_post:
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.via_fallback is True
        # Should have called Anthropic URL, not OpenRouter
        assert ANTHROPIC_MESSAGES_URL in str(mock_post.call_args)


# ---------------------------------------------------------------------------
# TestHardFailures
# ---------------------------------------------------------------------------

class TestHardFailures:
    def test_400_raises_hard_failure(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=http_error(400)):
            with pytest.raises(HardFailure) as exc_info:
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert exc_info.value.http_status == 400

    def test_400_no_retry(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=http_error(400)) as mock_post:
            with pytest.raises(HardFailure):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert mock_post.call_count == 1

    def test_402_raises_credits_exhausted(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=http_error(402)):
            with pytest.raises(CreditsExhausted):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

    def test_402_no_retry(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=http_error(402)) as mock_post:
            with pytest.raises(CreditsExhausted):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert mock_post.call_count == 1

    def test_403_raises_hard_failure(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=http_error(403)):
            with pytest.raises(HardFailure) as exc_info:
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert exc_info.value.http_status == 403

    def test_content_filter_raises(self):
        cfg = make_config()
        resp = openrouter_200(finish_reason="content_filter")
        with patch("hypogeum.openrouter.requests.post", return_value=resp):
            with pytest.raises(ContentFiltered):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

    def test_content_filter_is_hard_failure_subclass(self):
        assert issubclass(ContentFiltered, HardFailure)

    def test_credits_exhausted_is_hard_failure_subclass(self):
        assert issubclass(CreditsExhausted, HardFailure)


# ---------------------------------------------------------------------------
# TestTruncatedResponse
# ---------------------------------------------------------------------------

class TestTruncatedResponse:
    def test_length_finish_raises_truncated(self):
        cfg = make_config()
        resp = openrouter_200(finish_reason="length")
        with patch("hypogeum.openrouter.requests.post", return_value=resp):
            with pytest.raises(TruncatedResponse) as exc_info:
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        err = exc_info.value
        assert err.completion_tokens == 10  # from openrouter_200 fixture
        assert err.max_tokens_set == 512

    def test_truncated_response_message(self):
        cfg = make_config()
        resp = openrouter_200(finish_reason="length")
        with patch("hypogeum.openrouter.requests.post", return_value=resp):
            with pytest.raises(TruncatedResponse) as exc_info:
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert "max_tokens" in str(exc_info.value)

    def test_truncated_response_is_not_hard_failure(self):
        assert not issubclass(TruncatedResponse, HardFailure)


# ---------------------------------------------------------------------------
# TestEmptyResponse
# ---------------------------------------------------------------------------

class TestEmptyResponse:
    def test_empty_content_raises(self):
        cfg = make_config()
        resp = openrouter_200(content="")
        with patch("hypogeum.openrouter.requests.post", return_value=resp):
            with pytest.raises(EmptyResponse):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

    def test_none_content_raises(self):
        cfg = make_config()
        resp = openrouter_200()
        resp.json.return_value["choices"][0]["message"]["content"] = None
        with patch("hypogeum.openrouter.requests.post", return_value=resp):
            with pytest.raises(EmptyResponse):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)


# ---------------------------------------------------------------------------
# TestMalformedResponse
# ---------------------------------------------------------------------------

class TestMalformedResponse:
    def test_missing_choices_raises_hard_failure(self):
        """
        Malformed choices is classified _RetryableError (transient glitch) —
        it retries once, then hits the fallback path. With no
        OPENROUTER_FALLBACK_MODEL configured, that now surfaces as
        FallbackFailed rather than the old auto-Anthropic HardFailure-shaped
        parse error.
        """
        cfg = make_config()
        resp = openrouter_200()
        resp.json.return_value = {"id": "x", "model": SONNET}  # no "choices"
        with patch("hypogeum.openrouter.requests.post", return_value=resp):
            with pytest.raises(FallbackFailed):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

    def test_empty_choices_list_raises(self):
        cfg = make_config()
        resp = openrouter_200()
        resp.json.return_value["choices"] = []
        with patch("hypogeum.openrouter.requests.post", return_value=resp):
            with pytest.raises(FallbackFailed):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

    def test_missing_message_key_raises(self):
        cfg = make_config()
        resp = openrouter_200()
        resp.json.return_value["choices"] = [{"finish_reason": "stop"}]  # no "message"
        with patch("hypogeum.openrouter.requests.post", return_value=resp):
            with pytest.raises((HardFailure, EmptyResponse)):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)


# ---------------------------------------------------------------------------
# TestAnthropicFallback
# ---------------------------------------------------------------------------

class TestAnthropicFallback:
    """
    _call_anthropic_direct() itself is unchanged — these tests exercise its
    request-building logic directly. What changed is the *trigger*: 401 and
    retry-exhaustion no longer reach it automatically (see
    TestAuthFailureFallback / TestOpenRouterModelFallback), so these tests
    invoke it the same way check_health() would — via _use_anthropic_for_run
    — matching TestFallbackNoKey's existing pattern. Single call now, not
    [401, anthropic_200()], since nothing precedes it anymore.
    """

    def test_fallback_uses_anthropic_url(self):
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert mock_post.call_args.args[0] == ANTHROPIC_MESSAGES_URL

    def test_fallback_translates_slug_sonnet(self):
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        body = mock_post.call_args.kwargs["json"]
        assert body["model"] == "claude-sonnet-4-6"

    def test_fallback_translates_slug_opus(self):
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200()) as mock_post:
            call_claude(OPUS, MINIMAL_MESSAGES, 512, cfg)
        body = mock_post.call_args.kwargs["json"]
        assert body["model"] == "claude-opus-4-6"

    def test_fallback_extracts_system_message(self):
        """
        System message must be hoisted to top-level 'system' field. For an
        Anthropic model slug (SONNET), the caching transform converts it to a
        single-block cache_control array — the underlying text is unchanged,
        only the envelope shape changes (see TestSystemPromptCaching).
        """
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        body = mock_post.call_args.kwargs["json"]
        assert body["system"] == [
            {
                "type": "text",
                "text": "You are a helpful pipeline agent.",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def test_fallback_user_messages_only_in_messages(self):
        """After system extraction, messages[] should contain only non-system turns."""
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        body = mock_post.call_args.kwargs["json"]
        for m in body["messages"]:
            assert m["role"] != "system"

    def test_fallback_uses_x_api_key_header(self):
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["x-api-key"] == "sk-ant-test"

    def test_fallback_sets_anthropic_version_header(self):
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["anthropic-version"] == "2023-06-01"

    def test_fallback_normalises_end_turn_to_stop(self):
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200(stop_reason="end_turn")):
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.finish_reason == "stop"

    def test_fallback_content_extracted(self):
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200("fallback content")):
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.content == "fallback content"

    def test_fallback_via_fallback_true(self):
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200()):
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.via_fallback is True

    def test_fallback_token_accounting(self):
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        ant_resp = anthropic_200()
        ant_resp.json.return_value["usage"] = {"input_tokens": 80, "output_tokens": 20}
        with patch("hypogeum.openrouter.requests.post", return_value=ant_resp):
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.usage["prompt_tokens"] == 80
        assert result.usage["completion_tokens"] == 20
        assert result.usage["total_tokens"] == 100

    def test_fallback_truncation_raises(self):
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        ant_resp = anthropic_200(stop_reason="max_tokens")
        with patch("hypogeum.openrouter.requests.post", return_value=ant_resp):
            with pytest.raises(TruncatedResponse):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)


# ---------------------------------------------------------------------------
# TestFallbackNoKey
# ---------------------------------------------------------------------------

class TestFallbackNoKey:
    def test_raises_fallback_failed_without_key(self):
        """
        _call_anthropic_direct() itself is unchanged and still guards on a
        missing key — but 401/retry-exhaustion no longer reach it
        automatically (see TestAuthFailureFallback / TestOpenRouterModelFallback),
        so this exercises it the same way its sibling tests below do: via the
        _use_anthropic_for_run flag, as check_health() would set it.
        """
        cfg = make_config()
        cfg.ANTHROPIC_API_KEY = None  # Remove the key
        openrouter_mod._use_anthropic_for_run = True
        with pytest.raises(FallbackFailed) as exc_info:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert "ANTHROPIC_API_KEY" in str(exc_info.value)

    def test_fallback_failed_on_network_error(self):
        cfg = make_config()
        import requests as req_lib
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", side_effect=req_lib.ConnectionError("refused")):
            with pytest.raises(FallbackFailed):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

    def test_fallback_failed_on_non_200(self):
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=http_error(503)):
            with pytest.raises(FallbackFailed):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)


# ---------------------------------------------------------------------------
# TestHealthCheck
# ---------------------------------------------------------------------------

class TestHealthCheck:
    def _models_response(self, model_ids: list[str]) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": [{"id": mid} for mid in model_ids]}
        return resp

    def test_returns_true_when_both_models_present(self):
        cfg = make_config()
        both_present = self._models_response([SONNET, OPUS, "other/model"])
        with patch("hypogeum.openrouter.requests.get", return_value=both_present):
            assert check_health(cfg) is True

    def test_returns_false_when_model_missing(self):
        cfg = make_config()
        only_sonnet = self._models_response([SONNET])
        with patch("hypogeum.openrouter.requests.get", return_value=only_sonnet):
            assert check_health(cfg) is False

    def test_returns_false_and_sets_flag_on_non_200(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.get", return_value=http_error(503)):
            result = check_health(cfg)
        assert result is False
        assert openrouter_mod._use_anthropic_for_run is True

    def test_returns_false_and_sets_flag_on_connection_error(self):
        cfg = make_config()
        import requests as req_lib
        with patch("hypogeum.openrouter.requests.get", side_effect=req_lib.ConnectionError("timeout")):
            result = check_health(cfg)
        assert result is False
        assert openrouter_mod._use_anthropic_for_run is True

    def test_does_not_set_flag_on_missing_model(self):
        """Degraded (model missing) ≠ unavailable — flag should NOT be set."""
        cfg = make_config()
        only_sonnet = self._models_response([SONNET])
        with patch("hypogeum.openrouter.requests.get", return_value=only_sonnet):
            check_health(cfg)
        assert openrouter_mod._use_anthropic_for_run is False

    def test_uses_correct_url(self):
        cfg = make_config()
        both = self._models_response([SONNET, OPUS])
        with patch("hypogeum.openrouter.requests.get", return_value=both) as mock_get:
            check_health(cfg)
        assert mock_get.call_args.args[0] == OPENROUTER_MODELS_URL

    def test_uses_authorization_header(self):
        cfg = make_config()
        both = self._models_response([SONNET, OPUS])
        with patch("hypogeum.openrouter.requests.get", return_value=both) as mock_get:
            check_health(cfg)
        headers = mock_get.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-or-test"

    def test_run_routes_to_fallback_after_failed_health_check(self):
        """After health check fails, call_claude should use Anthropic direct."""
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.get", return_value=http_error(503)):
            check_health(cfg)
        # Now _use_anthropic_for_run is True — next call should go to Anthropic
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200()) as mock_post:
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.via_fallback is True
        call_url = mock_post.call_args.args[0]
        assert call_url == ANTHROPIC_MESSAGES_URL


# ---------------------------------------------------------------------------
# TestRunState
# ---------------------------------------------------------------------------

class TestRunState:
    def test_reset_clears_flag(self):
        openrouter_mod._use_anthropic_for_run = True
        reset_run_state()
        assert openrouter_mod._use_anthropic_for_run is False

    def test_flag_starts_false(self):
        reset_run_state()
        assert openrouter_mod._use_anthropic_for_run is False

    def test_flag_not_set_after_401(self):
        """401 fails safely now — it must not flip the run into Anthropic-direct-for-all-calls mode."""
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=http_error(401)):
            with pytest.raises(FallbackFailed):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert openrouter_mod._use_anthropic_for_run is False

    def test_reset_clears_flag_set_by_health_check(self):
        openrouter_mod._use_anthropic_for_run = True
        reset_run_state()
        assert openrouter_mod._use_anthropic_for_run is False


# ---------------------------------------------------------------------------
# TestLogging
# ---------------------------------------------------------------------------

class TestLogging:
    def test_request_sent_logged_at_info(self):
        cfg = make_config()
        log_path = tmp_log()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()):
            with patch("hypogeum.openrouter.get_logger") as mock_get_logger:
                mock_log = MagicMock()
                mock_get_logger.return_value = mock_log
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

        info_events = [
            c.args[0] for c in mock_log.info.call_args_list
        ]
        assert "openrouter_request_sent" in info_events

    def test_response_received_logged_at_info(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()):
            with patch("hypogeum.openrouter.get_logger") as mock_get_logger:
                mock_log = MagicMock()
                mock_get_logger.return_value = mock_log
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "openrouter_response_received" in info_events

    def test_retry_logged_at_warn(self):
        cfg = make_config()
        responses = [http_error(429), openrouter_200()]
        with patch("hypogeum.openrouter.requests.post", side_effect=responses):
            with patch("hypogeum.openrouter.time.sleep"):
                with patch("hypogeum.openrouter.get_logger") as mock_get_logger:
                    mock_log = MagicMock()
                    mock_get_logger.return_value = mock_log
                    call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

        warn_events = [c.args[0] for c in mock_log.warn.call_args_list]
        assert "openrouter_retry_triggered" in warn_events

    def test_hard_failure_logged_at_error_or_warn(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=http_error(402)):
            with patch("hypogeum.openrouter.get_logger") as mock_get_logger:
                mock_log = MagicMock()
                mock_get_logger.return_value = mock_log
                with pytest.raises(CreditsExhausted):
                    call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

        error_events = [c.args[0] for c in mock_log.error.call_args_list]
        assert "openrouter_hard_failure" in error_events

    def test_fallback_activation_logged_at_warn(self):
        """openrouter_model_fallback_activated is logged on retry exhaustion when OPENROUTER_FALLBACK_MODEL is set."""
        cfg = make_config({"OPENROUTER_FALLBACK_MODEL": "openai/gpt-5.5-pro"})
        responses = [http_error(500), http_error(500), openrouter_200()]
        with patch("hypogeum.openrouter.requests.post", side_effect=responses):
            with patch("hypogeum.openrouter.time.sleep"):
                with patch("hypogeum.openrouter.get_logger") as mock_get_logger:
                    mock_log = MagicMock()
                    mock_get_logger.return_value = mock_log
                    call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

        warn_events = [c.args[0] for c in mock_log.warn.call_args_list]
        assert "openrouter_model_fallback_activated" in warn_events

    def test_truncated_response_logged_at_warn(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200(finish_reason="length")):
            with patch("hypogeum.openrouter.get_logger") as mock_get_logger:
                mock_log = MagicMock()
                mock_get_logger.return_value = mock_log
                with pytest.raises(TruncatedResponse):
                    call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

        warn_events = [c.args[0] for c in mock_log.warn.call_args_list]
        assert "openrouter_truncated_response" in warn_events

    def test_episode_id_and_agent_passed_to_logger(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()):
            with patch("hypogeum.openrouter.get_logger") as mock_get_logger:
                mock_log = MagicMock()
                mock_get_logger.return_value = mock_log
                call_claude(
                    SONNET, MINIMAL_MESSAGES, 512, cfg,
                    episode_id="ep_test_001",
                    agent="test_agent",
                )
        mock_get_logger.assert_called_once_with(
            episode_id="ep_test_001",
            agent="test_agent",
        )


# ---------------------------------------------------------------------------
# TestRetryDelay
# ---------------------------------------------------------------------------

class TestRetryDelay:
    def test_attempt_0_base_is_5s(self):
        # With ±15% jitter, delay should be between 4.25s and 5.75s
        for _ in range(50):
            d = _retry_delay(0, None)
            assert 4.25 <= d <= 5.75, f"Out of range: {d}"

    def test_attempt_1_base_is_10s(self):
        for _ in range(50):
            d = _retry_delay(1, None)
            assert 8.5 <= d <= 11.5, f"Out of range: {d}"

    def test_retry_after_floors_delay(self):
        # retry_after=60 should override normal backoff
        d = _retry_delay(0, retry_after=60)
        assert d >= 60.0

    def test_max_delay_capped_at_30s(self):
        # Large attempt number should not exceed 30s * 1.15
        for _ in range(20):
            d = _retry_delay(10, None)
            assert d <= 30.0 * 1.15 + 0.001, f"Exceeded cap: {d}"


# ---------------------------------------------------------------------------
# TestSlugTranslation
# ---------------------------------------------------------------------------

class TestSlugTranslation:
    def test_sonnet_slug(self):
        assert _translate_slug_to_anthropic_id("anthropic/claude-sonnet-4.6") == "claude-sonnet-4-6"

    def test_opus_slug(self):
        assert _translate_slug_to_anthropic_id("anthropic/claude-opus-4.6") == "claude-opus-4-6"

    def test_no_prefix_passes_through(self):
        assert _translate_slug_to_anthropic_id("claude-sonnet-4-6") == "claude-sonnet-4-6"

    def test_dots_replaced_with_dashes(self):
        result = _translate_slug_to_anthropic_id("anthropic/claude-3.5-sonnet")
        assert "." not in result

    def test_only_first_slash_stripped(self):
        # Multi-slash slug — strip only up to first slash
        result = _translate_slug_to_anthropic_id("org/model/variant")
        assert result == "model/variant".replace(".", "-")


# ---------------------------------------------------------------------------
# TestSystemPromptCaching
#
# Verifies the shared prompt-caching implementation added to openrouter.py:
# Anthropic ephemeral cache_control applied to the system message's content,
# for Anthropic-slug models only, applied centrally (not per-agent), with no
# change to system/user prompt text, headers, other body fields, or call count.
# ---------------------------------------------------------------------------

class TestSystemPromptCaching:

    # -- unit tests on the pure helper -----------------------------------

    def test_cache_control_supported_for_anthropic_slug(self):
        assert _cache_control_supported(SONNET) is True
        assert _cache_control_supported(OPUS) is True

    def test_cache_control_not_supported_for_non_anthropic_slug(self):
        assert _cache_control_supported(NON_ANTHROPIC_MODEL) is False

    def test_apply_caching_converts_system_content_to_block_array(self):
        result = _apply_system_prompt_caching(MINIMAL_MESSAGES, SONNET)
        system_msg = next(m for m in result if m["role"] == "system")
        assert system_msg["content"] == [
            {
                "type": "text",
                "text": "You are a helpful pipeline agent.",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def test_apply_caching_leaves_user_message_untouched(self):
        result = _apply_system_prompt_caching(MINIMAL_MESSAGES, SONNET)
        user_msg = next(m for m in result if m["role"] == "user")
        assert user_msg == {"role": "user", "content": "Respond with: OK"}

    def test_apply_caching_unsupported_model_returns_unchanged_shape(self):
        result = _apply_system_prompt_caching(MINIMAL_MESSAGES, NON_ANTHROPIC_MODEL)
        assert result == MINIMAL_MESSAGES

    def test_apply_caching_does_not_mutate_input_list(self):
        """The original messages list/dicts must never be mutated in place —
        agent.py callers and shared test fixtures like MINIMAL_MESSAGES must
        be unaffected by a caching call for one model_slug."""
        original = [dict(m) for m in MINIMAL_MESSAGES]
        _apply_system_prompt_caching(MINIMAL_MESSAGES, SONNET)
        assert MINIMAL_MESSAGES == original

    # -- 1. Supported Anthropic/OpenRouter model receives cache metadata ---

    def test_supported_model_receives_cache_control_via_openrouter_path(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        body = mock_post.call_args.kwargs["json"]
        system_msg = next(m for m in body["messages"] if m["role"] == "system")
        assert system_msg["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_supported_model_receives_cache_control_via_anthropic_fallback(self):
        """_call_anthropic_direct() unchanged; triggered via the health-check
        flag now, not a 401 (see TestAnthropicFallback docstring)."""
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        body = mock_post.call_args.kwargs["json"]
        assert body["system"][0]["cache_control"] == {"type": "ephemeral"}

    # -- 2. Exact system prompt text remains byte-identical ----------------

    def test_system_prompt_text_byte_identical_via_openrouter(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        body = mock_post.call_args.kwargs["json"]
        system_msg = next(m for m in body["messages"] if m["role"] == "system")
        assert system_msg["content"][0]["text"] == "You are a helpful pipeline agent."

    def test_system_prompt_text_byte_identical_via_anthropic_fallback(self):
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        body = mock_post.call_args.kwargs["json"]
        assert body["system"][0]["text"] == "You are a helpful pipeline agent."

    # -- 3. User prompt remains unchanged -----------------------------------

    def test_user_prompt_unchanged_via_openrouter(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        body = mock_post.call_args.kwargs["json"]
        user_msg = next(m for m in body["messages"] if m["role"] == "user")
        assert user_msg["content"] == "Respond with: OK"

    def test_user_prompt_unchanged_via_anthropic_fallback(self):
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        body = mock_post.call_args.kwargs["json"]
        assert body["messages"] == [{"role": "user", "content": "Respond with: OK"}]

    # -- 4. Unsupported/non-Anthropic models retain the previous request shape

    def test_unsupported_model_keeps_plain_string_system_via_openrouter(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()) as mock_post:
            call_claude(NON_ANTHROPIC_MODEL, MINIMAL_MESSAGES, 512, cfg)
        body = mock_post.call_args.kwargs["json"]
        system_msg = next(m for m in body["messages"] if m["role"] == "system")
        assert system_msg["content"] == "You are a helpful pipeline agent."

    def test_unsupported_model_keeps_plain_string_system_via_anthropic_fallback(self):
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200()) as mock_post:
            call_claude(NON_ANTHROPIC_MODEL, MINIMAL_MESSAGES, 512, cfg)
        body = mock_post.call_args.kwargs["json"]
        assert body["system"] == "You are a helpful pipeline agent."

    # -- 5. Existing schema/body fields remain unchanged ---------------------

    def test_max_tokens_and_temperature_unaffected_by_caching(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 777, cfg)
        body = mock_post.call_args.kwargs["json"]
        assert body["max_tokens"] == 777
        assert body["temperature"] == 0

    def test_model_field_unaffected_by_caching(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        body = mock_post.call_args.kwargs["json"]
        assert body["model"] == SONNET

    # -- 6. Existing headers and authentication remain unchanged ------------

    def test_openrouter_auth_header_unaffected_by_caching(self):
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-or-test"

    def test_anthropic_fallback_headers_unaffected_by_caching(self):
        cfg = make_config()
        openrouter_mod._use_anthropic_for_run = True
        with patch("hypogeum.openrouter.requests.post", return_value=anthropic_200()) as mock_post:
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["x-api-key"] == "sk-ant-test"
        assert headers["anthropic-version"] == "2023-06-01"

    # -- 7. Retry calls use the same caching behavior ------------------------

    def test_retry_then_success_both_carry_cache_control(self):
        """A 429-then-200 retry must send the identical cached system content
        on both the failed and the succeeded attempt."""
        cfg = make_config()
        responses = [http_error(429), openrouter_200("ok")]
        with patch("hypogeum.openrouter.requests.post", side_effect=responses) as mock_post:
            with patch("hypogeum.openrouter.time.sleep"):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert mock_post.call_count == 2
        for call_obj in mock_post.call_args_list:
            body = call_obj.kwargs["json"]
            system_msg = next(m for m in body["messages"] if m["role"] == "system")
            assert system_msg["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_retry_exhaustion_fallback_carries_cache_control(self):
        """
        Retry-exhausted requests that fall back to OPENROUTER_FALLBACK_MODEL
        (still via OpenRouter) must carry the correct cache_control
        treatment for the FALLBACK model's own slug — re-applied fresh,
        not just reusing the primary model's cached_messages.
        """
        cfg = make_config({"OPENROUTER_FALLBACK_MODEL": "anthropic/claude-haiku-4.5"})
        responses = [http_error(500), http_error(500), openrouter_200()]
        with patch("hypogeum.openrouter.requests.post", side_effect=responses) as mock_post:
            with patch("hypogeum.openrouter.time.sleep"):
                result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        assert result.via_fallback is False  # still OpenRouter, not Anthropic direct
        body = mock_post.call_args_list[-1].kwargs["json"]
        system_msg = next(m for m in body["messages"] if m["role"] == "system")
        assert system_msg["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_retry_exhaustion_fallback_non_anthropic_model_no_cache_control(self):
        """A non-Anthropic OPENROUTER_FALLBACK_MODEL must not receive the
        Anthropic-specific cache_control block."""
        cfg = make_config({"OPENROUTER_FALLBACK_MODEL": "openai/gpt-5.5-pro"})
        responses = [http_error(500), http_error(500), openrouter_200()]
        with patch("hypogeum.openrouter.requests.post", side_effect=responses) as mock_post:
            with patch("hypogeum.openrouter.time.sleep"):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)
        body = mock_post.call_args_list[-1].kwargs["json"]
        system_msg = next(m for m in body["messages"] if m["role"] == "system")
        assert isinstance(system_msg["content"], str)  # unchanged plain string, no cache_control block

    # -- 8. No agent-level caching code is introduced ------------------------

    def test_no_agent_file_references_cache_control(self):
        """Caching must be applied centrally in openrouter.py only — no owning
        agent file may implement its own cache_control logic."""
        import inspect
        from hypogeum.agents import (
            script_engine,
            thumbnail_prompt_writer,
            visual_package_director,
            shot_diversity_planner,
        )
        for module in (script_engine, thumbnail_prompt_writer, visual_package_director, shot_diversity_planner):
            src = inspect.getsource(module)
            assert "cache_control" not in src, f"{module.__name__} must not reference cache_control"

    def test_no_agent_runner_references_cache_control(self):
        import inspect
        from hypogeum import agent as agent_mod
        assert "cache_control" not in inspect.getsource(agent_mod)

    # -- 9. No new run_agent() call is introduced ----------------------------

    def test_run_agent_call_count_in_vpd_unchanged(self):
        """Regression guard matching the count already established by the
        Seedance2/Veo Prompt Director attachment tasks — must remain 6."""
        import ast
        import inspect
        from hypogeum.agents import visual_package_director as vpd

        src = inspect.getsource(vpd)
        tree = ast.parse(src)
        call_count = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "run_agent":
                call_count += 1
        assert call_count == 6
