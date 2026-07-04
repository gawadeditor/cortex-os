"""
tests/test_muapi.py — Unit tests for hypogeum/muapi.py

All HTTP calls are mocked. No real network calls. No real API keys required.
Use MUAPI_TEST_MODE=true (or rely on mocks) — no real API credits consumed.

Test coverage:
  TestSubmitSceneSuccess          — happy path: submit → poll (processing × N → completed) → download
  TestSubmitSceneFallbackTrigger  — primary fails → fallback succeeds
  TestSubmitSceneBothAttemptsFail — primary + fallback both fail → generation_failed manifest
  TestSubmitSceneTimeout          — poll_attempts > max → timeout → fallback triggered
  TestSubmitSceneJobFailed        — job status "failed" → fallback triggered
  TestSubmitSceneJobCancelled     — job status "cancelled" → fallback triggered
  TestSubmitSceneHardFailure400   — HTTP 400 → attempt fails, fallback tried
  TestSubmitSceneHardFailure403   — HTTP 403 → attempt fails, fallback tried
  TestSubmitSceneHardFailure422   — HTTP 422 → attempt fails, fallback tried
  TestSubmitSceneAuthFailure      — HTTP 401 → MUAPIAuthFailure raised (halts all generation)
  TestSubmitSceneQuotaExhausted   — HTTP 402 → MUAPIQuotaExhausted raised
  TestSubmitScene429Retry         — 429 with Retry-After → correct delay, retry once
  TestSubmitScene5xxRetry         — 500/503 → retry once with backoff
  TestSubmitSceneDownloadFailed   — job completes but download fails → fallback
  TestSubmitSceneDownloadRetry    — first download fails, retry succeeds → generated
  TestSubmitSceneValidationFail   — downloaded file too small → fallback
  TestSubmitSceneMissingApiKey    — MUAPI_API_KEY absent → raises before submission
  TestSubmitSceneShortScene       — short_id set → correct path sh_{short_id}/sc_{scene_id}.mp4
  TestSubmitSceneI2VRouting       — reference_image_url set → I2V model used, images_list sent
  TestSubmitSceneRunwayDuration   — runway model → duration rounded to 5 or 8
  TestSubmitSceneVeo3NoDuration   — veo3 model → no duration field in payload
  TestHealthCheck                 — available, degraded (slow response), unavailable paths
  TestHealthCheckAuthFailure      — health check returns 401 → unavailable
  TestHealthCheckNoRequestId      — submission succeeds but no request_id → unavailable
  TestGenerateReferenceImage      — full Path A flow: generate → poll → download → upload
  TestGenerateReferenceImageFails — poll failure → MUAPIError raised
  TestUploadFile                  — upload succeeds, hosted_url returned
  TestUploadFileAuthFailure       — upload gets 401 → MUAPIAuthFailure
  TestInternalHelpers             — _tier_to_model, _build_job_payload, _compute_asset_id,
                                     _scene_file_path, _mask_url, _retry_delay
  TestExceptionHierarchy          — class inheritance
  TestLogging                     — correct event names at correct levels
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch
from io import BytesIO

import pytest
import requests
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.config import Config
from hypogeum.muapi import (
    _RETRY_AFTER_DEFAULT,
    _RETRY_INITIAL_DELAY,
    _RETRY_JITTER_FRACTION,
    _RETRY_MAX_HTTP,
    _VEO3_MODELS,
    _RUNWAY_MODELS,
    _IMAGE_MODELS,
    _ASSET_CLASS_REFERENCE,
    _ASSET_CLASS_THUMBNAIL,
    AssetClassError,
    MUAPIAuthFailure,
    MUAPIBudgetExhausted,
    MUAPIError,
    MUAPIQuotaExhausted,
    MUAPIUnavailable,
    ReferenceImageResult,
    ThumbnailResult,
    _apply_wordmark_overlay,
    _build_job_payload,
    _compute_asset_id,
    _mask_url,
    _normalize_thumbnail,
    _retry_delay,
    _scene_file_path,
    _tier_to_model,
    assert_asset_class,
    check_muapi_health,
    generate_reference_image,
    generate_thumbnail,
    submit_scene,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALL_ENV_VARS = [
    "OPENROUTER_API_KEY", "OPENROUTER_SONNET_MODEL_ID", "OPENROUTER_OPUS_MODEL_ID",
    "OPENROUTER_APP_NAME", "OPENROUTER_APP_URL", "OPENROUTER_FALLBACK_MODEL",
    "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "PERPLEXITY_MODEL", "PERPLEXITY_MAX_TOKENS",
    "MUAPI_API_KEY", "MUAPI_BASE_URL", "MUAPI_T1_MODEL", "MUAPI_T2_MODEL", "MUAPI_T3_MODEL",
    "MUAPI_I2V_MODEL", "MUAPI_IMAGE_MODEL", "MUAPI_DEFAULT_TIER", "MUAPI_TIMEOUT_SECONDS",
    "MUAPI_POLL_INTERVAL_SECONDS", "MUAPI_MAX_POLL_ATTEMPTS", "MUAPI_WEBHOOK_URL",
    "MUAPI_CREDIT_WARNING_THRESHOLD", "MUAPI_RUNWAY_ONLY_MODE",
    "MUAPI_EPISODE_BUDGET_CREDITS", "MUAPI_SCENE_BUDGET_CREDITS",
    "MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE", "EPISODE_ZERO_MAX_SCENES",
    "MUAPI_CHEAP_MODE", "MUAPI_DRY_RUN", "MUAPI_ALLOW_PAID_FALLBACKS",
    "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID",
    "ELEVENLABS_STABILITY", "ELEVENLABS_SIMILARITY_BOOST", "ELEVENLABS_MUSIC_MODEL_ID",
    "ELEVENLABS_MUSIC_DEFAULT_DURATION", "OPENAI_API_KEY", "WHISPER_MODEL",
    "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
    "YOUTUBE_CHANNEL_ID", "YOUTUBE_DEFAULT_CATEGORY_ID",
    "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_OPEN_ID",
    "META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN", "META_INSTAGRAM_ACCOUNT_ID",
    "META_FACEBOOK_PAGE_ID", "META_GRAPH_API_VERSION", "META_STAGING_BASE_URL",
    # Thumbnail provider chain — must be cleared before Config() so dotenv doesn't
    # repopulate them with production values, then re-set to muapi for MUAPI-path tests.
    "THUMBNAIL_IMAGE_PROVIDER", "THUMBNAIL_FALLBACK_PROVIDER",
    "THUMBNAIL_FALLBACK2_PROVIDER", "THUMBNAIL_FALLBACK_MODEL",
    "THUMBNAIL_IMAGE_MODEL",
]


def make_config(
    muapi_key: str = "muapi-test-key",
    t1_model: str = "veo3-text-to-video",
    t2_model: str = "runway-text-to-video",
    t3_model: str = "veo3-fast-text-to-video",
    i2v_model: str = "veo3-image-to-video",
    image_model: str = "flux-2-dev",
    poll_interval: int = 0,   # 0 for tests (no real sleep)
    max_poll_attempts: int = 3,
) -> Config:
    """Build a minimal Config for muapi tests."""
    original = {k: os.environ.get(k) for k in _ALL_ENV_VARS}
    for k in _ALL_ENV_VARS:
        os.environ.pop(k, None)
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    if muapi_key:
        os.environ["MUAPI_API_KEY"] = muapi_key
    os.environ["MUAPI_BASE_URL"] = "https://api.muapi.ai/api/v1"
    os.environ["MUAPI_T1_MODEL"] = t1_model
    os.environ["MUAPI_T2_MODEL"] = t2_model
    os.environ["MUAPI_T3_MODEL"] = t3_model
    os.environ["MUAPI_I2V_MODEL"] = i2v_model
    os.environ["MUAPI_IMAGE_MODEL"] = image_model
    os.environ["MUAPI_POLL_INTERVAL_SECONDS"] = str(poll_interval)
    os.environ["MUAPI_MAX_POLL_ATTEMPTS"] = str(max_poll_attempts)
    # Explicitly enable paid fallbacks in tests — default changed to False in M5.6
    os.environ["MUAPI_ALLOW_PAID_FALLBACKS"] = "true"
    # Pin thumbnail provider to muapi so existing MUAPI-path tests are unaffected
    # by the production default change (openrouter → openai → muapi).
    os.environ["THUMBNAIL_IMAGE_PROVIDER"] = "muapi"
    os.environ["THUMBNAIL_FALLBACK_PROVIDER"] = "muapi"
    os.environ["THUMBNAIL_FALLBACK2_PROVIDER"] = "muapi"
    os.environ["THUMBNAIL_FALLBACK_MODEL"] = "flux-2-dev"
    # Pin THUMBNAIL_IMAGE_MODEL to match image_model so MUAPI-path tests hit the
    # expected endpoint. Default changed to gpt-image-2-text-to-image in production.
    os.environ["THUMBNAIL_IMAGE_MODEL"] = image_model
    try:
        return Config()
    finally:
        for k in _ALL_ENV_VARS:
            os.environ.pop(k, None)
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v


def make_scene_spec(
    scene_id: str = "s01b01",
    aspect_ratio: str = "16:9",
    duration_seconds: float = 8.0,
    primary_prompt: str = "Ancient Greek ruins at dawn, wide establishing shot.",
    primary_tier: int = 2,
    fallback_prompt: str = "Ancient ruins, atmospheric, wide shot.",
    fallback_tier: int = 3,
    reference_image_url: str = None,
    short_id: str = None,
) -> dict:
    return {
        "scene_id": scene_id,
        "visual_spec": {
            "aspect_ratio": aspect_ratio,
            "duration_seconds": duration_seconds,
        },
        "production_spec": {
            "primary_prompt": primary_prompt,
            "primary_tier": primary_tier,
            "fallback_prompt": fallback_prompt,
            "fallback_tier": fallback_tier,
            "reference_image_url": reference_image_url,
        },
        "short_id": short_id,
    }


def _mock_submit_ok(request_id: str = "req_abc123") -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"request_id": request_id}
    return m


def _mock_poll_processing() -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"status": "processing"}
    return m


def _mock_poll_completed(video_url: str = "https://cdn.example.com/video.mp4") -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {
        "status": "completed",
        "outputs": [video_url],
    }
    return m


def _mock_poll_failed() -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"status": "failed", "error": "generation_error"}
    return m


def _mock_poll_cancelled() -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"status": "cancelled"}
    return m


def _mock_download_ok(content: bytes = b"\x00" * 50_000) -> MagicMock:
    """Simulate a successful video download with minimal content."""
    m = MagicMock()
    m.status_code = 200
    m.iter_content.return_value = [content]
    return m


def _mock_http_error(status_code: int, headers: dict = None) -> MagicMock:
    m = MagicMock()
    m.status_code = status_code
    m.headers = headers or {}
    m.json.return_value = {"error": f"HTTP {status_code}"}
    return m


# ---------------------------------------------------------------------------
# TestSubmitSceneSuccess
# ---------------------------------------------------------------------------

class TestSubmitSceneSuccess:
    """Happy path: submit → poll (processing × 2 → completed) → download → generated."""

    def test_returns_generated_manifest(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")) as mock_dl, \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok("req_001")
            mock_get.side_effect = [
                _mock_poll_processing(),
                _mock_poll_processing(),
                _mock_poll_completed("https://cdn.example.com/v.mp4"),
            ]

            result = submit_scene(
                spec, config, "ep_001", tmp_path, episode_id_for_asset="ep_001"
            )

        assert result["generation_status"] == "generated"
        assert result["scene_id"] == "s01b01"
        assert result["asset_id"] == "episodes/ep_001/video/sc_s01b01"
        assert result["provider"] == "muapi"
        assert result["tier_used"] == 2
        assert result["fallback_used"] is False
        assert result["failure_reason"] is None

    def test_correct_model_used_for_tier(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(primary_tier=1)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_completed()

            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["model_used"] == "veo3-text-to-video"
        # Verify endpoint used
        call_url = mock_post.call_args[0][0]
        assert "veo3-text-to-video" in call_url

    def test_correct_model_used_for_tier3(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(primary_tier=3)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_completed()

            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["model_used"] == "veo3-fast-text-to-video"

    def test_x_api_key_header_used(self, tmp_path):
        config = make_config(muapi_key="my-real-key")
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_completed()
            submit_scene(spec, config, "ep_001", tmp_path)

        headers = mock_post.call_args[1]["headers"]
        assert "x-api-key" in headers
        assert headers["x-api-key"] == "my-real-key"
        assert "Authorization" not in headers

    def test_prompt_used_is_recorded(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(primary_prompt="A pharaoh's tomb entrance, torchlight.")

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_completed()
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["prompt_used"] == "A pharaoh's tomb entrance, torchlight."

    def test_poll_url_uses_request_id(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok("req_xyz789")
            mock_get.return_value = _mock_poll_completed()
            submit_scene(spec, config, "ep_001", tmp_path)

        poll_url = mock_get.call_args[0][0]
        assert "predictions/req_xyz789/result" in poll_url


# ---------------------------------------------------------------------------
# TestSubmitSceneFallbackTrigger
# ---------------------------------------------------------------------------

class TestSubmitSceneFallbackTrigger:

    def test_fallback_used_when_primary_fails_job_failed(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        submit_responses = [_mock_submit_ok("req_001"), _mock_submit_ok("req_002")]
        get_responses = [_mock_poll_failed(), _mock_poll_completed()]

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.side_effect = submit_responses
            mock_get.side_effect = get_responses
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["generation_status"] == "generated"
        assert result["fallback_used"] is True
        assert result["prompt_used"] == spec["production_spec"]["fallback_prompt"]
        assert result["tier_used"] == spec["production_spec"]["fallback_tier"]

    def test_fallback_used_when_primary_submission_fails_with_400(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.side_effect = [_mock_http_error(400), _mock_submit_ok()]
            mock_get.return_value = _mock_poll_completed()
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["generation_status"] == "generated"
        assert result["fallback_used"] is True


# ---------------------------------------------------------------------------
# TestSubmitSceneBothAttemptsFail
# ---------------------------------------------------------------------------

class TestSubmitSceneBothAttemptsFail:

    def test_generation_failed_manifest_returned(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"):

            mock_post.side_effect = [_mock_submit_ok("req_1"), _mock_submit_ok("req_2")]
            mock_get.side_effect = [_mock_poll_failed(), _mock_poll_failed()]
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["generation_status"] == "generation_failed"
        assert result["asset_id"] is None
        assert result["fallback_used"] is True
        assert result["failure_reason"] is not None
        assert "attempt1" in result["failure_reason"]
        assert "attempt2" in result["failure_reason"]

    def test_pipeline_continues_after_scene_failure(self, tmp_path):
        """submit_scene does not raise on both-fail — it returns a manifest."""
        config = make_config()
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"):

            mock_post.side_effect = [_mock_submit_ok(), _mock_submit_ok()]
            mock_get.side_effect = [_mock_poll_failed(), _mock_poll_failed()]
            # Should not raise
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["generation_status"] == "generation_failed"


# ---------------------------------------------------------------------------
# TestSubmitSceneTimeout
# ---------------------------------------------------------------------------

class TestSubmitSceneTimeout:

    def test_timeout_triggers_fallback(self, tmp_path):
        config = make_config(max_poll_attempts=2)
        spec = make_scene_spec()

        # First attempt: polls indefinitely → timeout. Second attempt: succeeds.
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.side_effect = [_mock_submit_ok("req_1"), _mock_submit_ok("req_2")]
            # First attempt: all polls return "processing" → timeout after max_poll_attempts
            # Second attempt: completed
            mock_get.side_effect = [
                _mock_poll_processing(),
                _mock_poll_processing(),
                _mock_poll_completed(),
            ]
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["generation_status"] == "generated"
        assert result["fallback_used"] is True

    def test_timeout_both_attempts_returns_generation_failed(self, tmp_path):
        config = make_config(max_poll_attempts=2)
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"):

            mock_post.side_effect = [_mock_submit_ok("req_1"), _mock_submit_ok("req_2")]
            mock_get.return_value = _mock_poll_processing()  # always processing
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["generation_status"] == "generation_failed"
        assert "timeout" in result["failure_reason"]


# ---------------------------------------------------------------------------
# TestSubmitSceneJobFailed / Cancelled
# ---------------------------------------------------------------------------

class TestSubmitSceneJobFailed:

    def test_job_failed_status_triggers_fallback(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.side_effect = [_mock_submit_ok(), _mock_submit_ok()]
            mock_get.side_effect = [_mock_poll_failed(), _mock_poll_completed()]
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["generation_status"] == "generated"
        assert result["fallback_used"] is True

    def test_job_cancelled_triggers_fallback(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.side_effect = [_mock_submit_ok(), _mock_submit_ok()]
            mock_get.side_effect = [_mock_poll_cancelled(), _mock_poll_completed()]
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["generation_status"] == "generated"
        assert result["fallback_used"] is True


# ---------------------------------------------------------------------------
# TestSubmitSceneHardFailures (400/403/422 — attempt-level, not halt)
# ---------------------------------------------------------------------------

class TestSubmitSceneHardFailures:

    @pytest.mark.parametrize("status_code", [400, 403, 422])
    def test_hard_failure_tries_fallback(self, status_code, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.side_effect = [_mock_http_error(status_code), _mock_submit_ok()]
            mock_get.return_value = _mock_poll_completed()
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["generation_status"] == "generated"
        assert result["fallback_used"] is True

    @pytest.mark.parametrize("status_code", [400, 403, 422])
    def test_hard_failure_both_attempts_returns_failed(self, status_code, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.time.sleep"):

            mock_post.return_value = _mock_http_error(status_code)
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["generation_status"] == "generation_failed"


# ---------------------------------------------------------------------------
# TestSubmitSceneAuthFailure (401 — halts all generation)
# ---------------------------------------------------------------------------

class TestSubmitSceneAuthFailure:

    def test_401_raises_muapi_auth_failure(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.time.sleep"):

            mock_post.return_value = _mock_http_error(401)
            with pytest.raises(MUAPIAuthFailure):
                submit_scene(spec, config, "ep_001", tmp_path)

    def test_401_does_not_try_fallback(self, tmp_path):
        """On 401, the second submit call must not happen."""
        config = make_config()
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.time.sleep"):

            mock_post.return_value = _mock_http_error(401)
            with pytest.raises(MUAPIAuthFailure):
                submit_scene(spec, config, "ep_001", tmp_path)

        # Only one POST call should have been made
        assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# TestSubmitSceneQuotaExhausted (402)
# ---------------------------------------------------------------------------

class TestSubmitSceneQuotaExhausted:

    def test_402_raises_muapi_quota_exhausted(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.time.sleep"):

            mock_post.return_value = _mock_http_error(402)
            with pytest.raises(MUAPIQuotaExhausted):
                submit_scene(spec, config, "ep_001", tmp_path)


# ---------------------------------------------------------------------------
# TestSubmitScene429Retry
# ---------------------------------------------------------------------------

class TestSubmitScene429Retry:

    def test_429_retries_after_retry_after_header(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        retry_after = MagicMock()
        retry_after.status_code = 429
        retry_after.headers = {"Retry-After": "30"}
        retry_after.json.return_value = {"error": "rate_limited"}

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep") as mock_sleep, \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.side_effect = [retry_after, _mock_submit_ok()]
            mock_get.return_value = _mock_poll_completed()
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["generation_status"] == "generated"
        assert result["fallback_used"] is False
        # A sleep call should have occurred for the 429 backoff
        assert mock_sleep.called
        # The sleep duration should be at least the Retry-After value
        sleep_duration = mock_sleep.call_args_list[0][0][0]
        assert sleep_duration >= 30.0

    def test_429_without_retry_after_uses_default(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        retry_after = MagicMock()
        retry_after.status_code = 429
        retry_after.headers = {}  # No Retry-After
        retry_after.json.return_value = {}

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep") as mock_sleep, \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.side_effect = [retry_after, _mock_submit_ok()]
            mock_get.return_value = _mock_poll_completed()
            submit_scene(spec, config, "ep_001", tmp_path)

        # Sleep duration should be >= RETRY_AFTER_DEFAULT (60s)
        sleep_duration = mock_sleep.call_args_list[0][0][0]
        assert sleep_duration >= _RETRY_AFTER_DEFAULT

    def test_429_exhausted_triggers_fallback(self, tmp_path):
        """429 on both HTTP tries → attempt fails → fallback triggered."""
        config = make_config()
        spec = make_scene_spec()

        rate_limit_resp = MagicMock()
        rate_limit_resp.status_code = 429
        rate_limit_resp.headers = {"Retry-After": "1"}
        rate_limit_resp.json.return_value = {}

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            # Both HTTP tries for Attempt 1 return 429; Attempt 2 succeeds
            mock_post.side_effect = [
                rate_limit_resp, rate_limit_resp,  # Attempt 1 (2 tries)
                _mock_submit_ok(),                   # Attempt 2
            ]
            mock_get.return_value = _mock_poll_completed()
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["generation_status"] == "generated"
        assert result["fallback_used"] is True


# ---------------------------------------------------------------------------
# TestSubmitScene5xxRetry
# ---------------------------------------------------------------------------

class TestSubmitScene5xxRetry:

    @pytest.mark.parametrize("status_code", [500, 503])
    def test_5xx_retries_once(self, status_code, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.side_effect = [_mock_http_error(status_code), _mock_submit_ok()]
            mock_get.return_value = _mock_poll_completed()
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["generation_status"] == "generated"
        assert result["fallback_used"] is False
        assert mock_post.call_count == 2  # initial + 1 retry


# ---------------------------------------------------------------------------
# TestSubmitSceneDownloadFailed
# ---------------------------------------------------------------------------

class TestSubmitSceneDownloadFailed:

    def test_download_failure_triggers_fallback(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file") as mock_dl, \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.side_effect = [_mock_submit_ok("req_1"), _mock_submit_ok("req_2")]
            mock_get.side_effect = [_mock_poll_completed(), _mock_poll_completed()]
            # Both download calls for Attempt 1 fail; Attempt 2 download succeeds
            mock_dl.side_effect = [
                (False, "http_404"),  # Attempt 1 first try
                (False, "http_404"),  # Attempt 1 retry
                (True, ""),           # Attempt 2 first try
            ]
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["generation_status"] == "generated"
        assert result["fallback_used"] is True

    def test_download_retry_succeeds(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file") as mock_dl, \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_completed()
            mock_dl.side_effect = [
                (False, "http_500"),  # first download fails
                (True, ""),           # retry succeeds
            ]
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["generation_status"] == "generated"
        assert result["fallback_used"] is False


# ---------------------------------------------------------------------------
# TestSubmitSceneValidationFail
# ---------------------------------------------------------------------------

class TestSubmitSceneValidationFail:

    def test_small_file_triggers_fallback(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file") as mock_dl:

            mock_post.side_effect = [_mock_submit_ok("req_1"), _mock_submit_ok("req_2")]
            mock_get.side_effect = [_mock_poll_completed(), _mock_poll_completed()]

            call_count = {"n": 0}

            def dl_side_effect(url, dest_path, log):
                call_count["n"] += 1
                # Write tiny file for Attempt 1; succeed for Attempt 2 (fallback)
                if call_count["n"] <= 1:
                    dest_path.write_bytes(b"\x00" * 100)  # too small
                    return True, ""
                dest_path.write_bytes(b"\x00" * 60_000)
                return True, ""

            mock_dl.side_effect = dl_side_effect
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["generation_status"] == "generated"
        assert result["fallback_used"] is True


# ---------------------------------------------------------------------------
# TestSubmitSceneMissingApiKey
# ---------------------------------------------------------------------------

class TestSubmitSceneMissingApiKey:

    def test_none_api_key_raises_on_submission(self, tmp_path):
        """x-api-key: None will be sent — MUAPI returns 401 → MUAPIAuthFailure."""
        config = make_config(muapi_key="")
        spec = make_scene_spec()

        # Without a key, MUAPI would return 401, which we simulate
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.time.sleep"):

            mock_post.return_value = _mock_http_error(401)
            with pytest.raises(MUAPIAuthFailure):
                submit_scene(spec, config, "ep_001", tmp_path)


# ---------------------------------------------------------------------------
# TestSubmitSceneShortScene
# ---------------------------------------------------------------------------

class TestSubmitSceneShortScene:

    def test_short_scene_uses_sh_prefix_path(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s02b01", short_id="sh_01", aspect_ratio="9:16")

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_completed()
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["asset_id"] == "episodes/ep_001/video/sh_sh_01/sc_s02b01"

    def test_short_scene_aspect_ratio_sent_correctly(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(short_id="sh_02", aspect_ratio="9:16")

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_completed()
            submit_scene(spec, config, "ep_001", tmp_path)

        payload = mock_post.call_args[1]["json"]
        assert payload["aspect_ratio"] == "9:16"


# ---------------------------------------------------------------------------
# TestSubmitSceneI2VRouting
# ---------------------------------------------------------------------------

class TestSubmitSceneI2VRouting:

    def test_reference_image_url_routes_to_i2v_model(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(reference_image_url="https://cdn.muapi.ai/ref_pharaoh.jpg")

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_completed()
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["model_used"] == "veo3-image-to-video"
        endpoint = mock_post.call_args[0][0]
        assert "veo3-image-to-video" in endpoint

    def test_reference_image_url_sent_in_images_list(self, tmp_path):
        config = make_config()
        ref_url = "https://cdn.muapi.ai/ref_pharaoh.jpg"
        spec = make_scene_spec(reference_image_url=ref_url)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_completed()
            submit_scene(spec, config, "ep_001", tmp_path)

        payload = mock_post.call_args[1]["json"]
        assert payload.get("images_list") == [ref_url]
        assert "reference_image" not in payload  # spec's wrong param name NOT used

    def test_no_reference_image_url_uses_t2v_model(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(reference_image_url=None, primary_tier=2)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_completed()
            result = submit_scene(spec, config, "ep_001", tmp_path)

        assert result["model_used"] == "runway-text-to-video"
        payload = mock_post.call_args[1]["json"]
        assert "images_list" not in payload


# ---------------------------------------------------------------------------
# TestSubmitSceneRunwayDuration
# ---------------------------------------------------------------------------

class TestSubmitSceneRunwayDuration:

    @pytest.mark.parametrize("requested,expected_duration", [
        (3.0, 5),
        (5.0, 5),
        (6.0, 5),
        (6.1, 8),
        (8.0, 8),
        (15.0, 8),
    ])
    def test_runway_duration_rounding(self, requested, expected_duration, tmp_path):
        config = make_config(t2_model="runway-text-to-video")
        spec = make_scene_spec(duration_seconds=requested, primary_tier=2)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_completed()
            submit_scene(spec, config, "ep_001", tmp_path)

        payload = mock_post.call_args[1]["json"]
        assert payload["duration"] == expected_duration


# ---------------------------------------------------------------------------
# TestSubmitSceneVeo3NoDuration
# ---------------------------------------------------------------------------

class TestSubmitSceneVeo3NoDuration:

    def test_veo3_t1_has_no_duration_field(self, tmp_path):
        config = make_config(t1_model="veo3-text-to-video")
        spec = make_scene_spec(primary_tier=1, duration_seconds=8.0)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_completed()
            submit_scene(spec, config, "ep_001", tmp_path)

        payload = mock_post.call_args[1]["json"]
        assert "duration" not in payload

    def test_veo3_t3_has_no_duration_field(self, tmp_path):
        config = make_config(t3_model="veo3-fast-text-to-video")
        spec = make_scene_spec(primary_tier=3, duration_seconds=8.0)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_completed()
            submit_scene(spec, config, "ep_001", tmp_path)

        payload = mock_post.call_args[1]["json"]
        assert "duration" not in payload


# ---------------------------------------------------------------------------
# TestHealthCheck
# ---------------------------------------------------------------------------

class TestHealthCheck:

    def test_available_on_fast_response_with_request_id(self):
        config = make_config()

        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"request_id": "hc_001"}

        with patch("hypogeum.muapi.requests.post", return_value=m), \
             patch("hypogeum.muapi.time.monotonic", side_effect=[0.0, 2.0]):
            result = check_muapi_health(config)

        assert result == "available"

    def test_degraded_on_slow_response(self):
        config = make_config()

        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"request_id": "hc_002"}

        with patch("hypogeum.muapi.requests.post", return_value=m), \
             patch("hypogeum.muapi.time.monotonic", side_effect=[0.0, 12.0]):
            result = check_muapi_health(config)

        assert result == "degraded"

    @pytest.mark.parametrize("status_code", [401, 402, 403, 503])
    def test_unavailable_on_error_status(self, status_code):
        config = make_config()

        with patch("hypogeum.muapi.requests.post", return_value=_mock_http_error(status_code)):
            result = check_muapi_health(config)

        assert result == "unavailable"

    def test_unavailable_on_connection_error(self):
        config = make_config()

        with patch("hypogeum.muapi.requests.post",
                   side_effect=requests.exceptions.ConnectionError("refused")):
            result = check_muapi_health(config)

        assert result == "unavailable"

    def test_unavailable_on_timeout(self):
        config = make_config()

        with patch("hypogeum.muapi.requests.post",
                   side_effect=requests.exceptions.Timeout("timed out")):
            result = check_muapi_health(config)

        assert result == "unavailable"

    def test_unavailable_when_no_request_id(self):
        config = make_config()

        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"status": "queued"}  # no request_id

        with patch("hypogeum.muapi.requests.post", return_value=m), \
             patch("hypogeum.muapi.time.monotonic", side_effect=[0.0, 1.0]):
            result = check_muapi_health(config)

        assert result == "unavailable"

    def test_t3_model_endpoint_used(self):
        config = make_config(t3_model="veo3-fast-text-to-video")

        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"request_id": "hc_003"}

        with patch("hypogeum.muapi.requests.post", return_value=m) as mock_post, \
             patch("hypogeum.muapi.time.monotonic", side_effect=[0.0, 1.0]):
            check_muapi_health(config)

        endpoint = mock_post.call_args[0][0]
        assert "veo3-fast-text-to-video" in endpoint


# ---------------------------------------------------------------------------
# TestGenerateReferenceImage
# ---------------------------------------------------------------------------

class TestGenerateReferenceImage:

    def test_full_path_a_flow(self, tmp_path):
        """Generate image → poll → download → upload → return result."""
        config = make_config()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi._download_file") as mock_dl, \
             patch("hypogeum.muapi._upload_file", return_value="https://cdn.muapi.ai/ref_p.jpg"), \
             patch("hypogeum.muapi.time.sleep"):

            # Submission
            mock_post.return_value = _mock_submit_ok("img_req_001")
            # Poll → completed with images array
            poll_result = MagicMock()
            poll_result.status_code = 200
            poll_result.json.return_value = {
                "status": "completed",
                "outputs": ["https://cdn.muapi.ai/generated_pharaoh.jpg"],
            }
            mock_get.return_value = poll_result

            # Download succeeds, writes a real file
            def dl_side_effect(url, dest_path, log):
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 5000)  # JPEG-like
                return True, ""

            mock_dl.side_effect = dl_side_effect

            result = generate_reference_image(
                entity_id="pharaoh_amenhotep",
                visual_spec_text="A tall ancient Egyptian pharaoh, golden headdress.",
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert isinstance(result, ReferenceImageResult)
        assert result.asset_id == "episodes/ep_001/video/ref_pharaoh_amenhotep"
        assert result.hosted_url == "https://cdn.muapi.ai/ref_p.jpg"

    def test_image_model_endpoint_used(self, tmp_path):
        config = make_config(image_model="flux-2-dev")

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi._download_file") as mock_dl, \
             patch("hypogeum.muapi._upload_file", return_value="https://hosted.url/img.jpg"), \
             patch("hypogeum.muapi.time.sleep"):

            mock_post.return_value = _mock_submit_ok("img_req_002")
            poll_m = MagicMock()
            poll_m.status_code = 200
            poll_m.json.return_value = {
                "status": "completed",
                "outputs": ["https://cdn.muapi.ai/img.jpg"],
            }
            mock_get.return_value = poll_m

            def dl_side_effect(url, dest_path, log):
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_bytes(b"\x00" * 5000)
                return True, ""

            mock_dl.side_effect = dl_side_effect

            generate_reference_image("entity_1", "A person.", config, "ep_001", tmp_path)

        endpoint = mock_post.call_args[0][0]
        assert "flux-2-dev" in endpoint

    def test_poll_failure_raises_muapi_error(self, tmp_path):
        config = make_config()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_failed()

            with pytest.raises(MUAPIError):
                generate_reference_image("e1", "desc", config, "ep_001", tmp_path)


# ---------------------------------------------------------------------------
# TestUploadFile
# ---------------------------------------------------------------------------

class TestUploadFile:
    """Tests for _upload_file via generate_reference_image integration."""

    def test_upload_returns_url_from_response(self, tmp_path):
        from hypogeum.muapi import _upload_file
        config = make_config()

        local_file = tmp_path / "test.jpg"
        local_file.write_bytes(b"\xff\xd8\xff" + b"\x00" * 5000)

        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"url": "https://cdn.muapi.ai/uploads/test.jpg"}

        with patch("hypogeum.muapi.requests.post", return_value=m):
            url = _upload_file(local_file, config, MagicMock())

        assert url == "https://cdn.muapi.ai/uploads/test.jpg"

    def test_upload_401_raises_auth_failure(self, tmp_path):
        from hypogeum.muapi import _upload_file
        config = make_config()

        local_file = tmp_path / "test.jpg"
        local_file.write_bytes(b"\x00" * 5000)

        with patch("hypogeum.muapi.requests.post", return_value=_mock_http_error(401)):
            with pytest.raises(MUAPIAuthFailure):
                _upload_file(local_file, config, MagicMock())

    def test_upload_500_raises_muapi_error(self, tmp_path):
        from hypogeum.muapi import _upload_file
        config = make_config()

        local_file = tmp_path / "test.jpg"
        local_file.write_bytes(b"\x00" * 5000)

        with patch("hypogeum.muapi.requests.post", return_value=_mock_http_error(500)):
            with pytest.raises(MUAPIError):
                _upload_file(local_file, config, MagicMock())


# ---------------------------------------------------------------------------
# TestInternalHelpers
# ---------------------------------------------------------------------------

class TestInternalHelpers:

    # --- _tier_to_model ---

    def test_tier1_maps_to_t1_model(self):
        config = make_config()
        assert _tier_to_model(1, None, config) == "veo3-text-to-video"

    def test_tier2_maps_to_t2_model(self):
        config = make_config()
        assert _tier_to_model(2, None, config) == "runway-text-to-video"

    def test_tier3_maps_to_t3_model(self):
        config = make_config()
        assert _tier_to_model(3, None, config) == "veo3-fast-text-to-video"

    def test_reference_image_url_overrides_tier_to_i2v(self):
        config = make_config()
        assert _tier_to_model(1, "https://url.com/img.jpg", config) == "veo3-image-to-video"
        assert _tier_to_model(2, "https://url.com/img.jpg", config) == "veo3-image-to-video"
        assert _tier_to_model(3, "https://url.com/img.jpg", config) == "veo3-image-to-video"

    def test_tier_defaults_to_t2_for_unknown_tier(self):
        config = make_config()
        assert _tier_to_model(99, None, config) == "runway-text-to-video"

    # --- _build_job_payload ---

    def test_veo3_payload_has_no_duration(self):
        payload = _build_job_payload(
            "prompt", "veo3-text-to-video", "16:9", 8.0, None
        )
        assert "duration" not in payload
        assert payload["prompt"] == "prompt"
        assert payload["aspect_ratio"] == "16:9"

    def test_runway_payload_has_duration_5(self):
        payload = _build_job_payload(
            "prompt", "runway-text-to-video", "16:9", 5.0, None
        )
        assert payload["duration"] == 5

    def test_runway_payload_has_duration_8(self):
        payload = _build_job_payload(
            "prompt", "runway-text-to-video", "16:9", 8.0, None
        )
        assert payload["duration"] == 8

    def test_veo3_fast_payload_sends_aspect_ratio(self):
        # veo3-fast-text-to-video is in _VEO3_MODELS — aspect_ratio is sent in the payload.
        # Note: the provider may generate square output regardless (observed 2026-06-09
        # during ep_smoke_01), but the payload is correct. No client-side fix applies.
        payload = _build_job_payload(
            "prompt", "veo3-fast-text-to-video", "16:9", 8.0, None
        )
        assert payload["aspect_ratio"] == "16:9"
        assert "duration" not in payload

    def test_veo3_i2v_payload_has_images_list(self):
        payload = _build_job_payload(
            "prompt", "veo3-image-to-video", "16:9", 8.0, "https://img.url/ref.jpg"
        )
        assert payload["images_list"] == ["https://img.url/ref.jpg"]
        assert "duration" not in payload

    def test_veo3_i2v_payload_no_reference_image_field(self):
        """Ensure spec's wrong param name is NOT used."""
        payload = _build_job_payload(
            "prompt", "veo3-image-to-video", "16:9", 8.0, "https://img.url/ref.jpg"
        )
        assert "reference_image" not in payload

    # Runway I2V payload tests (bug fix 2026-06-06)
    # Runway I2V uses "image_url" (single string).
    # Confirmed via live 422: {"loc":["body","image_url"],"msg":"Field required"}
    # Veo3 I2V uses "images_list" (array) — different field names per provider.

    def test_runway_i2v_payload_has_image_url(self):
        """runway-image-to-video with reference_image_url must include image_url (not images_list)."""
        ref = "https://cdn.muapi.ai/outputs/abc123.jpg"
        payload = _build_job_payload(
            "prompt", "runway-image-to-video", "16:9", 5.0, ref
        )
        assert payload["image_url"] == ref
        assert "images_list" not in payload, "Runway I2V uses image_url, not images_list"

    def test_runway_i2v_payload_has_duration(self):
        """runway-image-to-video must still include duration (Runway enum requirement)."""
        payload = _build_job_payload(
            "prompt", "runway-image-to-video", "16:9", 5.0,
            "https://cdn.muapi.ai/outputs/abc123.jpg"
        )
        assert payload["duration"] == 5

    def test_runway_i2v_payload_no_image_url_when_no_ref_url(self):
        """runway-image-to-video with no reference_image_url must NOT include image_url."""
        payload = _build_job_payload(
            "prompt", "runway-image-to-video", "16:9", 5.0, None
        )
        assert "image_url" not in payload
        assert "images_list" not in payload

    def test_runway_t2v_payload_no_image_fields(self):
        """runway-text-to-video must never include image_url or images_list."""
        payload = _build_job_payload(
            "prompt", "runway-text-to-video", "16:9", 5.0, None
        )
        assert "image_url" not in payload
        assert "images_list" not in payload

    def test_runway_i2v_image_url_is_string_not_array(self):
        """image_url for Runway I2V must be a plain string (not a list)."""
        ref = "https://cdn.muapi.ai/outputs/ref.jpg"
        payload = _build_job_payload(
            "prompt", "runway-image-to-video", "16:9", 8.0, ref
        )
        assert isinstance(payload["image_url"], str)

    def test_veo3_t2v_no_image_fields_unchanged(self):
        """Existing Veo3 T2V behaviour is unchanged: no image fields when no ref url."""
        payload = _build_job_payload(
            "prompt", "veo3-text-to-video", "16:9", 8.0, None
        )
        assert "images_list" not in payload
        assert "image_url" not in payload

    def test_veo3_i2v_images_list_unchanged(self):
        """Existing Veo3 I2V behaviour is unchanged: images_list (array) set when ref url present."""
        ref = "https://img.url/ref.jpg"
        payload = _build_job_payload(
            "prompt", "veo3-image-to-video", "16:9", 8.0, ref
        )
        assert payload["images_list"] == [ref]
        assert "image_url" not in payload, "Veo3 uses images_list, not image_url"
        assert "duration" not in payload

    # --- _compute_asset_id ---

    def test_main_episode_asset_id(self):
        assert _compute_asset_id("ep_001", "s01b01", None) == "episodes/ep_001/video/sc_s01b01"

    def test_short_asset_id(self):
        result = _compute_asset_id("ep_001", "s02b01", "sh_01")
        assert result == "episodes/ep_001/video/sh_sh_01/sc_s02b01"

    # --- _scene_file_path ---

    def test_main_scene_path(self, tmp_path):
        path = _scene_file_path(tmp_path, "s01b01", None)
        assert path == tmp_path / "video" / "sc_s01b01.mp4"

    def test_short_scene_path(self, tmp_path):
        path = _scene_file_path(tmp_path, "s02b01", "sh_01")
        assert path == tmp_path / "video" / "sh_sh_01" / "sc_s02b01.mp4"

    # --- _mask_url ---

    def test_masks_query_params(self):
        url = "https://cdn.example.com/video.mp4?X-Amz-Signature=abc123&Expires=999"
        masked = _mask_url(url)
        assert "X-Amz-Signature" not in masked
        assert masked.endswith("?[masked]")

    def test_no_query_params_unchanged(self):
        url = "https://cdn.example.com/video.mp4"
        assert _mask_url(url) == url

    # --- _retry_delay ---

    def test_retry_delay_grows_exponentially(self):
        d0 = _retry_delay(0, None)
        d1 = _retry_delay(1, None)
        # d1 should be approximately 2× d0 (before jitter)
        assert d1 > d0

    def test_retry_delay_respects_retry_after(self):
        # retry_after=120 should dominate
        delay = _retry_delay(0, 120.0)
        assert delay >= 120.0

    def test_retry_delay_capped_at_max(self):
        from hypogeum.muapi import _RETRY_MAX_DELAY
        delay = _retry_delay(100, None)  # very large attempt number
        # With jitter ±25%, max is _RETRY_MAX_DELAY * 1.25
        assert delay <= _RETRY_MAX_DELAY * 1.26

    def test_retry_delay_has_jitter(self):
        """Two calls at the same attempt should not always return identical values."""
        results = {_retry_delay(0, None) for _ in range(20)}
        assert len(results) > 1


# ---------------------------------------------------------------------------
# TestExceptionHierarchy
# ---------------------------------------------------------------------------

class TestExceptionHierarchy:

    def test_auth_failure_is_muapi_error(self):
        assert issubclass(MUAPIAuthFailure, MUAPIError)

    def test_quota_exhausted_is_muapi_error(self):
        assert issubclass(MUAPIQuotaExhausted, MUAPIError)

    def test_unavailable_is_muapi_error(self):
        assert issubclass(MUAPIUnavailable, MUAPIError)

    def test_muapi_error_is_runtime_error(self):
        assert issubclass(MUAPIError, RuntimeError)

    def test_auth_failure_message_contains_401(self):
        exc = MUAPIAuthFailure()
        assert "401" in str(exc)

    def test_quota_exhausted_message_contains_402(self):
        exc = MUAPIQuotaExhausted()
        assert "402" in str(exc)


# ---------------------------------------------------------------------------
# TestLogging
# ---------------------------------------------------------------------------

class TestLogging:

    def test_api_key_not_in_submit_payload(self, tmp_path):
        """API key must not appear in the request JSON body."""
        config = make_config(muapi_key="SECRET-KEY-DO-NOT-LOG")
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_completed()
            submit_scene(spec, config, "ep_001", tmp_path)

        payload = mock_post.call_args[1]["json"]
        payload_str = str(payload)
        assert "SECRET-KEY-DO-NOT-LOG" not in payload_str

    def test_api_key_in_header_not_json_body(self, tmp_path):
        config = make_config(muapi_key="my-test-key")
        spec = make_scene_spec()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_completed()
            submit_scene(spec, config, "ep_001", tmp_path)

        headers = mock_post.call_args[1]["headers"]
        json_body = mock_post.call_args[1]["json"]
        assert headers["x-api-key"] == "my-test-key"
        assert "my-test-key" not in str(json_body)

    def test_reference_image_result_dataclass_fields(self):
        r = ReferenceImageResult(
            asset_id="episodes/ep_001/video/ref_entity_1",
            hosted_url="https://cdn.muapi.ai/ref.jpg",
        )
        assert r.asset_id == "episodes/ep_001/video/ref_entity_1"
        assert r.hosted_url == "https://cdn.muapi.ai/ref.jpg"

    def test_thumbnail_result_dataclass_fields(self, tmp_path):
        local = tmp_path / "thumb.jpg"
        r = ThumbnailResult(
            asset_id="episodes/ep_001/thumbnails/main",
            local_path=local,
        )
        assert r.asset_id == "episodes/ep_001/thumbnails/main"
        assert r.local_path == local


# ---------------------------------------------------------------------------
# TestGenerateThumbnail (M2.5)
# ---------------------------------------------------------------------------

def _mock_poll_image_completed(image_url: str = "https://cdn.example.com/thumb.jpg") -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {
        "status": "completed",
        "outputs": [image_url],
    }
    return m


class TestGenerateThumbnail:

    def _dl_writes_file(self, tmp_path, deliverable_id: str = "main"):
        """Returns a _download_file side_effect that writes a real JPEG file."""
        def _write(url, dest_path, log):
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            img = Image.new("RGB", (1024, 1024), color=(80, 80, 80))
            img.save(dest_path, format="JPEG", quality=85)
            return True, ""
        return _write

    def test_returns_thumbnail_result(self, tmp_path):
        config = make_config()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file",
                   side_effect=self._dl_writes_file(tmp_path, "main")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_image_completed()

            result = generate_thumbnail(
                deliverable_id="main",
                image_prompt="Pharaoh on throne, golden light",
                aspect_ratio="16:9",
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert isinstance(result, ThumbnailResult)
        assert result.asset_id == "episodes/ep_001/thumbnails/main"

    def test_asset_id_follows_deliverable_id(self, tmp_path):
        config = make_config()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file",
                   side_effect=self._dl_writes_file(tmp_path, "sh_02")):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_image_completed()

            result = generate_thumbnail(
                deliverable_id="sh_02",
                image_prompt="Hieroglyphs close-up",
                aspect_ratio="9:16",
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert result.asset_id == "episodes/ep_001/thumbnails/sh_02"

    def test_aspect_ratio_in_payload(self, tmp_path):
        config = make_config()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file",
                   side_effect=self._dl_writes_file(tmp_path)):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_image_completed()

            generate_thumbnail(
                deliverable_id="main",
                image_prompt="Pharaoh on throne",
                aspect_ratio="16:9",
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        payload = mock_post.call_args[1]["json"]
        assert payload["aspect_ratio"] == "16:9"

    def test_image_model_endpoint_used(self, tmp_path):
        config = make_config()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file",
                   side_effect=self._dl_writes_file(tmp_path)):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_image_completed()

            generate_thumbnail(
                deliverable_id="main",
                image_prompt="Pharaoh on throne",
                aspect_ratio="16:9",
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        # Must post to the image model endpoint, not a video model
        post_url = mock_post.call_args[0][0]
        assert "flux-2-dev" in post_url

    def test_auth_failure_propagates(self, tmp_path):
        config = make_config()

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.time.sleep"):
            mock_post.return_value.status_code = 401

            with pytest.raises(MUAPIAuthFailure):
                generate_thumbnail(
                    deliverable_id="main",
                    image_prompt="Pharaoh on throne",
                    aspect_ratio="16:9",
                    config=config,
                    episode_id="ep_001",
                    output_dir=tmp_path,
                )

    def test_raises_on_missing_images_in_result(self, tmp_path):
        config = make_config()

        def empty_images_poll(*args, **kwargs):
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = {"status": "completed", "outputs": []}
            return m

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"):

            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = empty_images_poll()

            with pytest.raises(MUAPIError, match="no outputs"):
                generate_thumbnail(
                    deliverable_id="main",
                    image_prompt="Pharaoh on throne",
                    aspect_ratio="16:9",
                    config=config,
                    episode_id="ep_001",
                    output_dir=tmp_path,
                )


# ---------------------------------------------------------------------------
# TestPoll400ResponseBodyLogged (Patch 1 — observability)
# ---------------------------------------------------------------------------

class TestPoll400ResponseBodyLogged:
    """Verify that HTTP 400 poll responses capture response_body in the log event."""

    def _run_with_poll_400(self, tmp_path, response_text: str):
        """Submit a scene, mock all polls to return 400 with given response_text,
        return the mock log so callers can inspect what was logged."""
        from hypogeum.muapi import _poll_until_complete
        from unittest.mock import MagicMock

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = response_text

        mock_log = MagicMock()
        config = make_config()
        config.MUAPI_MAX_POLL_ATTEMPTS = 5
        config.MUAPI_POLL_INTERVAL_SECONDS = 0

        with patch("hypogeum.muapi.requests.get", return_value=mock_resp), \
             patch("hypogeum.muapi.time.sleep"):
            _poll_until_complete(
                request_id="req_test_400",
                config=config,
                scene_id="s02",
                log=mock_log,
            )
        return mock_log

    def test_response_body_field_in_warn_log(self, tmp_path):
        log = self._run_with_poll_400(tmp_path, '{"error":"content_policy_violation"}')
        warn_calls = log.warn.call_args_list
        # Find the muapi_poll_unexpected_status calls
        status_calls = [c for c in warn_calls if c[0][0] == "muapi_poll_unexpected_status"]
        assert len(status_calls) > 0, "muapi_poll_unexpected_status was not logged"
        # All 400 warn events must carry response_body
        for call_ in status_calls:
            kwargs = call_[1]
            assert "response_body" in kwargs, f"response_body missing from warn event: {kwargs}"

    def test_response_body_content_captured(self, tmp_path):
        body = '{"error":"job_generation_failed","code":4001}'
        log = self._run_with_poll_400(tmp_path, body)
        warn_calls = log.warn.call_args_list
        status_calls = [c for c in warn_calls if c[0][0] == "muapi_poll_unexpected_status"]
        assert len(status_calls) > 0
        assert status_calls[0][1]["response_body"] == body

    def test_response_body_truncated_at_500_chars(self, tmp_path):
        long_body = "x" * 1000
        log = self._run_with_poll_400(tmp_path, long_body)
        warn_calls = log.warn.call_args_list
        status_calls = [c for c in warn_calls if c[0][0] == "muapi_poll_unexpected_status"]
        assert len(status_calls) > 0
        assert len(status_calls[0][1]["response_body"]) <= 500


# ---------------------------------------------------------------------------
# TestNormalizeThumbnail — _normalize_thumbnail() unit tests
# ---------------------------------------------------------------------------

def _write_jpeg(path: Path, width: int, height: int) -> None:
    """Write a minimal valid JPEG at the given dimensions to path."""
    img = Image.new("RGB", (width, height), color=(128, 64, 32))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="JPEG", quality=85)


class TestNormalizeThumbnail:
    """Unit tests for _normalize_thumbnail(). No network calls required."""

    def test_1024x1024_16_9_becomes_1280x720(self, tmp_path):
        """Square source + 16:9 → file saved as 1280×720."""
        p = tmp_path / "thumb.jpg"
        _write_jpeg(p, 1024, 1024)
        _normalize_thumbnail(p, "16:9")
        img = Image.open(p)
        assert img.size == (1280, 720)

    def test_already_1280x720_16_9_unchanged(self, tmp_path):
        """Already 1280×720 + 16:9 → no-op (no re-encode, size stays exact)."""
        p = tmp_path / "thumb.jpg"
        _write_jpeg(p, 1280, 720)
        mtime_before = p.stat().st_mtime
        _normalize_thumbnail(p, "16:9")
        img = Image.open(p)
        assert img.size == (1280, 720)
        # File should not have been rewritten (mtime unchanged on no-op)
        assert p.stat().st_mtime == mtime_before

    def test_1024x1024_9_16_becomes_720x1280(self, tmp_path):
        """Square source + 9:16 → file saved as 720×1280."""
        p = tmp_path / "thumb.jpg"
        _write_jpeg(p, 1024, 1024)
        _normalize_thumbnail(p, "9:16")
        img = Image.open(p)
        assert img.size == (720, 1280)

    def test_unknown_aspect_ratio_leaves_file_unchanged(self, tmp_path):
        """Unknown aspect ratio → function is a no-op, file untouched."""
        p = tmp_path / "thumb.jpg"
        _write_jpeg(p, 1024, 1024)
        mtime_before = p.stat().st_mtime
        _normalize_thumbnail(p, "4:3")
        assert p.stat().st_mtime == mtime_before
        img = Image.open(p)
        assert img.size == (1024, 1024)

    def test_empty_aspect_ratio_leaves_file_unchanged(self, tmp_path):
        """Empty string aspect ratio → no-op."""
        p = tmp_path / "thumb.jpg"
        _write_jpeg(p, 800, 600)
        mtime_before = p.stat().st_mtime
        _normalize_thumbnail(p, "")
        assert p.stat().st_mtime == mtime_before

    def test_non_square_source_16_9_fills_without_letterbox(self, tmp_path):
        """Non-square source (wider than target ratio) → result is exactly 1280×720."""
        p = tmp_path / "thumb.jpg"
        _write_jpeg(p, 1920, 1080)
        _normalize_thumbnail(p, "16:9")
        img = Image.open(p)
        assert img.size == (1280, 720)

    def test_tall_source_9_16_fills_without_pillarbox(self, tmp_path):
        """Tall source (already portrait) → result is exactly 720×1280."""
        p = tmp_path / "thumb.jpg"
        _write_jpeg(p, 720, 1440)
        _normalize_thumbnail(p, "9:16")
        img = Image.open(p)
        assert img.size == (720, 1280)

    def test_output_is_valid_jpeg(self, tmp_path):
        """Output file after normalization is a valid JPEG (can be opened by PIL)."""
        p = tmp_path / "thumb.jpg"
        _write_jpeg(p, 1024, 1024)
        _normalize_thumbnail(p, "16:9")
        img = Image.open(p)
        assert img.format == "JPEG"


# ---------------------------------------------------------------------------
# TestGenerateThumbnailNormalization — integration: generate_thumbnail calls
# _normalize_thumbnail with correct aspect_ratio
# ---------------------------------------------------------------------------

class TestGenerateThumbnailNormalization:
    """Verify generate_thumbnail() normalizes the downloaded file to the correct size."""

    def _make_real_jpeg_downloader(self, tmp_path, width: int, height: int, deliverable_id: str):
        """Side-effect for _download_file that writes a real JPEG at given dimensions."""
        def _write(url, dest_path, log):
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            img = Image.new("RGB", (width, height), color=(100, 150, 200))
            img.save(dest_path, format="JPEG", quality=85)
            return True, ""
        return _write

    def test_1024x1024_download_normalized_to_1280x720_for_16_9(self, tmp_path):
        """generate_thumbnail with aspect_ratio='16:9' saves a 1280×720 file."""
        config = make_config()
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file",
                   side_effect=self._make_real_jpeg_downloader(tmp_path, 1024, 1024, "main")):
            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_image_completed()
            result = generate_thumbnail(
                deliverable_id="main",
                image_prompt="Egyptian tomb",
                aspect_ratio="16:9",
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )
        img = Image.open(result.local_path)
        assert img.size == (1280, 720), f"Expected 1280×720, got {img.size}"

    def test_1024x1024_download_normalized_to_720x1280_for_9_16(self, tmp_path):
        """generate_thumbnail with aspect_ratio='9:16' saves a 720×1280 file."""
        config = make_config()
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file",
                   side_effect=self._make_real_jpeg_downloader(tmp_path, 1024, 1024, "sh_01")):
            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_image_completed()
            result = generate_thumbnail(
                deliverable_id="sh_01",
                image_prompt="Egyptian short thumbnail",
                aspect_ratio="9:16",
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )
        img = Image.open(result.local_path)
        assert img.size == (720, 1280), f"Expected 720×1280, got {img.size}"

    def test_already_correct_size_not_re_encoded(self, tmp_path):
        """1280×720 source with aspect_ratio='16:9' → file is unchanged (no re-encode)."""
        config = make_config()
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file",
                   side_effect=self._make_real_jpeg_downloader(tmp_path, 1280, 720, "main")):
            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_image_completed()
            result = generate_thumbnail(
                deliverable_id="main",
                image_prompt="Already correct size",
                aspect_ratio="16:9",
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )
        img = Image.open(result.local_path)
        assert img.size == (1280, 720)

    def test_asset_id_unchanged_after_normalization(self, tmp_path):
        """Normalization does not alter asset_id or local_path."""
        config = make_config()
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file",
                   side_effect=self._make_real_jpeg_downloader(tmp_path, 1024, 1024, "main")):
            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_image_completed()
            result = generate_thumbnail(
                deliverable_id="main",
                image_prompt="Pyramid at sunset",
                aspect_ratio="16:9",
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )
        assert result.asset_id == "episodes/ep_001/thumbnails/main"
        assert result.local_path.name == "main.jpg"


# ---------------------------------------------------------------------------
# TestAssetClassSeparation (Task B)
# ---------------------------------------------------------------------------

class TestAssetClassSeparation:
    """Asset-class sentinel fields and assert_asset_class validation."""

    def test_thumbnail_result_has_correct_asset_class(self, tmp_path):
        r = ThumbnailResult(asset_id="episodes/ep_001/thumbnails/main", local_path=tmp_path / "main.jpg")
        assert r.asset_class == "thumbnail_image"
        assert r.asset_class == _ASSET_CLASS_THUMBNAIL

    def test_reference_image_result_has_correct_asset_class(self):
        r = ReferenceImageResult(asset_id="episodes/ep_001/video/ref_x", hosted_url="https://host/img.jpg")
        assert r.asset_class == "reference_image"
        assert r.asset_class == _ASSET_CLASS_REFERENCE

    def test_assert_asset_class_passes_on_correct_class(self, tmp_path):
        r = ThumbnailResult(asset_id="t", local_path=tmp_path / "t.jpg")
        assert_asset_class(r, _ASSET_CLASS_THUMBNAIL)  # no exception

    def test_assert_asset_class_fails_on_wrong_class(self, tmp_path):
        r = ThumbnailResult(asset_id="t", local_path=tmp_path / "t.jpg")
        with pytest.raises(AssetClassError, match="thumbnail_image"):
            assert_asset_class(r, _ASSET_CLASS_REFERENCE)

    def test_assert_asset_class_fails_for_reference_used_as_thumbnail(self):
        r = ReferenceImageResult(asset_id="ref_x", hosted_url="https://h/img.jpg")
        with pytest.raises(AssetClassError, match="reference_image"):
            assert_asset_class(r, _ASSET_CLASS_THUMBNAIL)

    def test_assert_asset_class_fails_when_field_missing(self):
        """Objects without asset_class field always fail the check."""
        class _NoClass:
            asset_id = "x"
        with pytest.raises(AssetClassError):
            assert_asset_class(_NoClass(), _ASSET_CLASS_THUMBNAIL)

    def test_asset_class_error_is_value_error(self):
        assert issubclass(AssetClassError, ValueError)

    def test_asset_class_constants_are_strings(self):
        assert isinstance(_ASSET_CLASS_REFERENCE, str)
        assert isinstance(_ASSET_CLASS_THUMBNAIL, str)
        assert _ASSET_CLASS_REFERENCE != _ASSET_CLASS_THUMBNAIL

    def test_generate_thumbnail_result_has_thumbnail_class(self, tmp_path):
        """generate_thumbnail() returns ThumbnailResult with asset_class=thumbnail_image."""
        config = make_config()
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file",
                   side_effect=lambda url, dest, log: (
                       dest.parent.mkdir(parents=True, exist_ok=True) or
                       Image.new("RGB", (1280, 720), color=(50, 50, 50)).save(str(dest), "JPEG") or
                       (True, "")
                   )):
            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_image_completed()
            result = generate_thumbnail(
                deliverable_id="main",
                image_prompt="A pyramid",
                aspect_ratio="16:9",
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )
        assert result.asset_class == "thumbnail_image"

    def test_generate_reference_image_result_has_reference_class(self, tmp_path):
        """generate_reference_image() returns ReferenceImageResult with asset_class=reference_image."""
        config = make_config()
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi._download_file") as mock_dl, \
             patch("hypogeum.muapi._upload_file", return_value="https://cdn.muapi.ai/ref.jpg"), \
             patch("hypogeum.muapi.time.sleep"):
            mock_post.return_value = _mock_submit_ok("img_req_cls")
            poll_m = MagicMock()
            poll_m.status_code = 200
            poll_m.json.return_value = {"status": "completed", "outputs": ["https://cdn.muapi.ai/ref.jpg"]}
            mock_get.return_value = poll_m
            def dl_side(url, dest, log):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"\xff\xd8\xff" + b"\x00" * 5000)
                return True, ""
            mock_dl.side_effect = dl_side
            result = generate_reference_image(
                entity_id="pharaoh_test",
                visual_spec_text="A pharaoh.",
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )
        assert result.asset_class == "reference_image"


# ---------------------------------------------------------------------------
# TestReferenceImageAspectRatio (Task C)
# ---------------------------------------------------------------------------

class TestReferenceImageAspectRatio:
    """Reference image aspect ratio enforcement (Task C)."""

    def _make_dl_side_effect(self):
        def dl_side(url, dest, log):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"\xff\xd8\xff" + b"\x00" * 5000)
            return True, ""
        return dl_side

    def _run_generate(self, tmp_path, video_aspect_ratio):
        config = make_config()
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi._download_file") as mock_dl, \
             patch("hypogeum.muapi._upload_file", return_value="https://cdn.muapi.ai/ref.jpg"), \
             patch("hypogeum.muapi.time.sleep"):
            mock_post.return_value = _mock_submit_ok("img_req_ar")
            poll_m = MagicMock()
            poll_m.status_code = 200
            poll_m.json.return_value = {"status": "completed", "outputs": ["https://cdn.muapi.ai/r.jpg"]}
            mock_get.return_value = poll_m
            mock_dl.side_effect = self._make_dl_side_effect()
            return generate_reference_image(
                entity_id="entity_ar",
                visual_spec_text="Test entity.",
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
                video_aspect_ratio=video_aspect_ratio,
            ), mock_post

    def test_16_9_video_uses_16_9_aspect_ratio(self, tmp_path):
        """Main episode (16:9) → payload aspect_ratio='16:9'."""
        _, mock_post = self._run_generate(tmp_path, "16:9")
        payload = mock_post.call_args[1]["json"]
        assert payload["aspect_ratio"] == "16:9"

    def test_9_16_video_uses_9_16_aspect_ratio(self, tmp_path):
        """Shorts (9:16) → payload aspect_ratio='9:16'."""
        _, mock_post = self._run_generate(tmp_path, "9:16")
        payload = mock_post.call_args[1]["json"]
        assert payload["aspect_ratio"] == "9:16"

    def test_default_is_16_9(self, tmp_path):
        """Default video_aspect_ratio is 16:9."""
        config = make_config()
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi._download_file") as mock_dl, \
             patch("hypogeum.muapi._upload_file", return_value="https://cdn.muapi.ai/ref.jpg"), \
             patch("hypogeum.muapi.time.sleep"):
            mock_post.return_value = _mock_submit_ok("img_req_def")
            poll_m = MagicMock()
            poll_m.status_code = 200
            poll_m.json.return_value = {"status": "completed", "outputs": ["https://cdn.muapi.ai/r.jpg"]}
            mock_get.return_value = poll_m
            mock_dl.side_effect = self._make_dl_side_effect()
            generate_reference_image(
                entity_id="entity_def",
                visual_spec_text="Test.",
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
                # no video_aspect_ratio — relies on default
            )
        payload = mock_post.call_args[1]["json"]
        assert payload["aspect_ratio"] == "16:9"

    def test_unsupported_aspect_ratio_raises_asset_class_error(self, tmp_path):
        """Unsupported aspect ratio raises AssetClassError before any network call."""
        config = make_config()
        with pytest.raises(AssetClassError, match="Unsupported"):
            generate_reference_image(
                entity_id="entity_bad",
                visual_spec_text="Test.",
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
                video_aspect_ratio="1:1",
            )

    def test_unsupported_aspect_ratio_no_network_calls(self, tmp_path):
        """AssetClassError fires before POST — zero network calls."""
        config = make_config()
        with patch("hypogeum.muapi.requests.post") as mock_post:
            with pytest.raises(AssetClassError):
                generate_reference_image(
                    entity_id="entity_bad2",
                    visual_spec_text="Test.",
                    config=config,
                    episode_id="ep_001",
                    output_dir=tmp_path,
                    video_aspect_ratio="4:3",
                )
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# TestWordmarkOverlay — _apply_wordmark_overlay() unit tests
# ---------------------------------------------------------------------------

def _make_jpeg(path: Path, width: int = 1280, height: int = 720) -> None:
    """Write a solid-colour JPEG at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), color=(30, 20, 10)).save(path, format="JPEG", quality=95)


class TestWordmarkOverlay:
    """Unit tests for _apply_wordmark_overlay()."""

    def test_wordmark_overlay_modifies_image(self, tmp_path):
        """Calling _apply_wordmark_overlay changes pixel data (wordmark is rendered)."""
        p = tmp_path / "thumbnails" / "main.jpg"
        _make_jpeg(p)
        before = Image.open(p).tobytes()
        _apply_wordmark_overlay(p)
        after = Image.open(p).tobytes()
        assert before != after, "Expected wordmark to modify at least one pixel"

    def test_top_hook_placement_pushes_wordmark_lower(self, tmp_path):
        """With hook_placement='top', the first wordmark line starts below 20% of image height."""
        p = tmp_path / "thumbnails" / "main.jpg"
        _make_jpeg(p, width=1280, height=720)

        # Capture the y-coordinate of the first non-background pixel column
        # by comparing the 'top' result against a blank image row by row.
        baseline_img = Image.new("RGB", (1280, 720), color=(30, 20, 10))

        _apply_wordmark_overlay(p, hook_placement="top")
        result = Image.open(p)
        result_arr = result.load()
        baseline_arr = baseline_img.load()

        # Find the topmost row that differs from the solid background
        first_modified_row = None
        for y in range(720):
            for x in range(1280):
                if result_arr[x, y] != baseline_arr[x, y]:
                    first_modified_row = y
                    break
            if first_modified_row is not None:
                break

        assert first_modified_row is not None, "Wordmark produced no pixels"
        # Hook band is 20% (144px on 720p). start_y for 'top' is 22% (≈158px).
        # Allow ±5px for shadow offset below start_y.
        assert first_modified_row >= round(720 * 0.20) - 5, (
            f"Wordmark first pixel at row {first_modified_row}, expected ≥ row {round(720 * 0.20) - 5}"
        )

    def test_post_process_passes_hook_placement_to_wordmark(self, tmp_path):
        """generate_thumbnail with text_overlay placement='top' passes hook_placement to wordmark."""
        config = make_config()

        def _write_jpeg(url, dest_path, log):
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (1280, 720), color=(30, 20, 10)).save(dest_path, format="JPEG", quality=95)
            return True, ""

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", side_effect=_write_jpeg), \
             patch("hypogeum.muapi._apply_wordmark_overlay") as mock_wm:
            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_image_completed()
            generate_thumbnail(
                deliverable_id="main",
                image_prompt="Test",
                aspect_ratio="16:9",
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
                text_overlay={"text": "RISE OF THE PHARAOHS", "placement": "top"},
            )
        mock_wm.assert_called_once()
        _, kwargs = mock_wm.call_args
        assert kwargs.get("hook_placement") == "top", (
            f"Expected hook_placement='top', got {kwargs}"
        )


# ---------------------------------------------------------------------------
# TestGenerateThumbnailOpenAI — regression for gpt-image-1 parameter support
# ---------------------------------------------------------------------------

class TestGenerateThumbnailOpenAI:
    """
    Regression tests for _generate_thumbnail_openai.

    gpt-image-1 does NOT support response_format (DALL-E 2/3 only).
    Sending it causes: "Unknown parameter: response_format".
    The function must omit response_format and parse data[0].b64_json
    from the default response shape.
    """

    def _make_openai_response(self, b64_data: str = ""):
        """Minimal OpenAI gpt-image-1 response body with a valid-sized image."""
        import json, base64
        if not b64_data:
            # 2 KB of zero bytes — satisfies _MIN_IMAGE_BYTES=1000
            b64_data = base64.b64encode(b"\x00" * 2000).decode("utf-8")
        body = json.dumps({"data": [{"b64_json": b64_data}]}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.read.return_value = body
        return mock_resp

    def test_response_format_not_in_payload(self, tmp_path):
        """response_format must NOT be sent — gpt-image-1 rejects it."""
        from hypogeum.muapi import _generate_thumbnail_openai

        config = MagicMock()
        config.OPENAI_API_KEY = "sk-test"
        config.OPENAI_IMAGE_MODEL = "gpt-image-1"
        config.OPENAI_IMAGE_BASE_URL = "https://api.openai.com/v1"
        config.THUMBNAIL_IMAGE_QUALITY = "high"

        captured_payload = {}

        def _fake_urlopen(req, timeout=None):
            import json as _json
            captured_payload.update(_json.loads(req.data.decode("utf-8")))
            return self._make_openai_response()

        local_path = tmp_path / "out.jpg"
        log = MagicMock()

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            _generate_thumbnail_openai(
                deliverable_id="cmp",
                image_prompt="test prompt",
                aspect_ratio="16:9",
                config=config,
                local_path=local_path,
                log=log,
            )

        assert "response_format" not in captured_payload, (
            "response_format must NOT be sent to gpt-image-1 — it is a DALL-E 2/3 "
            "parameter and causes 'Unknown parameter: response_format' on gpt-image-1."
        )

    def test_correct_parameters_sent(self, tmp_path):
        """Verify the payload contains the parameters gpt-image-1 does accept."""
        from hypogeum.muapi import _generate_thumbnail_openai

        config = MagicMock()
        config.OPENAI_API_KEY = "sk-test"
        config.OPENAI_IMAGE_MODEL = "gpt-image-1"
        config.OPENAI_IMAGE_BASE_URL = "https://api.openai.com/v1"
        config.THUMBNAIL_IMAGE_QUALITY = "high"

        captured_payload = {}

        def _fake_urlopen(req, timeout=None):
            import json as _json
            captured_payload.update(_json.loads(req.data.decode("utf-8")))
            return self._make_openai_response()

        local_path = tmp_path / "out.jpg"

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            _generate_thumbnail_openai(
                deliverable_id="cmp",
                image_prompt="test prompt",
                aspect_ratio="16:9",
                config=MagicMock(
                    OPENAI_API_KEY="sk-test",
                    OPENAI_IMAGE_MODEL="gpt-image-1",
                    OPENAI_IMAGE_BASE_URL="https://api.openai.com/v1",
                    THUMBNAIL_IMAGE_QUALITY="high",
                ),
                local_path=local_path,
                log=MagicMock(),
            )

        assert captured_payload.get("model") == "gpt-image-1"
        assert captured_payload.get("size") == "1536x1024"   # 16:9 → 1536x1024
        assert captured_payload.get("quality") == "high"
        assert captured_payload.get("n") == 1

    def test_b64_json_decoded_and_written(self, tmp_path):
        """Image bytes from data[0].b64_json are written to local_path."""
        import base64
        from hypogeum.muapi import _generate_thumbnail_openai

        raw_bytes = b"\xff\xd8\xff" + b"\x00" * 1500   # fake JPEG header + padding (>_MIN_IMAGE_BYTES)
        b64 = base64.b64encode(raw_bytes).decode("utf-8")

        def _fake_urlopen(req, timeout=None):
            return self._make_openai_response(b64_data=b64)

        local_path = tmp_path / "out.jpg"

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            _generate_thumbnail_openai(
                deliverable_id="cmp",
                image_prompt="test prompt",
                aspect_ratio="16:9",
                config=MagicMock(
                    OPENAI_API_KEY="sk-test",
                    OPENAI_IMAGE_MODEL="gpt-image-1",
                    OPENAI_IMAGE_BASE_URL="https://api.openai.com/v1",
                    THUMBNAIL_IMAGE_QUALITY="high",
                ),
                local_path=local_path,
                log=MagicMock(),
            )

        assert local_path.exists(), "Image file must be written to local_path"
        assert local_path.read_bytes() == raw_bytes

    def test_401_raises_muapi_auth_failure(self, tmp_path):
        """401 from OpenAI must raise MUAPIAuthFailure."""
        import urllib.error
        from hypogeum.muapi import _generate_thumbnail_openai

        fp = BytesIO(b"Unauthorized")
        exc = urllib.error.HTTPError("", 401, "Unauthorized", {}, fp)

        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(MUAPIAuthFailure):
                _generate_thumbnail_openai(
                    deliverable_id="cmp",
                    image_prompt="test",
                    aspect_ratio="16:9",
                    config=MagicMock(
                        OPENAI_API_KEY="sk-bad",
                        OPENAI_IMAGE_MODEL="gpt-image-1",
                        OPENAI_IMAGE_BASE_URL="https://api.openai.com/v1",
                        THUMBNAIL_IMAGE_QUALITY="high",
                    ),
                    local_path=tmp_path / "out.jpg",
                    log=MagicMock(),
                )


# ---------------------------------------------------------------------------
# TestThumbnailProviderChain — provider priority: openrouter → openai → muapi
# ---------------------------------------------------------------------------

class TestThumbnailProviderChain:
    """
    Tests for the 3-tier thumbnail provider fallback chain.

    Default priority: openrouter (primary) → openai (fallback #1) → muapi (fallback #2).
    Each provider is tried in order; failure falls through to the next.
    MUAPIBudgetExhausted halts the chain immediately (never falls through).
    """

    def _make_config(self, primary="openrouter", fallback1="openai", fallback2="muapi"):
        cfg = MagicMock()
        cfg.THUMBNAIL_IMAGE_PROVIDER = primary
        cfg.THUMBNAIL_FALLBACK_PROVIDER = fallback1
        cfg.THUMBNAIL_FALLBACK2_PROVIDER = fallback2
        cfg.THUMBNAIL_FALLBACK_MODEL = "flux-2-dev"
        cfg.THUMBNAIL_IMAGE_MODEL = "gpt-image-2-text-to-image"
        cfg.OPENROUTER_API_KEY = "or-key"
        cfg.OPENAI_API_KEY = "sk-key"
        cfg.MUAPI_API_KEY = "muapi-key"
        cfg.OPENROUTER_IMAGE_MODEL = "openai/gpt-5-image-mini"
        cfg.OPENAI_IMAGE_MODEL = "gpt-image-1"
        cfg.OPENAI_IMAGE_BASE_URL = "https://api.openai.com/v1"
        cfg.THUMBNAIL_IMAGE_QUALITY = "high"
        cfg.OPENROUTER_APP_URL = None
        cfg.OPENROUTER_APP_NAME = "hypogeum-brain"
        cfg.THUMBNAIL_IMAGE_RESOLUTION = "2K"
        cfg.MUAPI_DRY_RUN = False
        return cfg

    def test_openrouter_is_called_first_by_default(self, tmp_path):
        """With default config, openrouter must be attempted before openai and muapi."""
        from hypogeum.muapi import generate_thumbnail

        call_order = []

        def _fake_openrouter(*a, **kw):
            call_order.append("openrouter")
            return "openai/gpt-5-image-mini"

        local_path = tmp_path / "thumbnails" / "main.jpg"
        local_path.parent.mkdir(parents=True)
        local_path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 2000)

        with patch("hypogeum.muapi._generate_thumbnail_openrouter", side_effect=_fake_openrouter), \
             patch("hypogeum.muapi._generate_thumbnail_openai") as mock_openai, \
             patch("hypogeum.muapi._attempt_thumbnail_generation") as mock_muapi, \
             patch("hypogeum.muapi._normalize_thumbnail"), \
             patch("hypogeum.muapi._apply_wordmark_overlay"):
            result = generate_thumbnail(
                deliverable_id="main",
                image_prompt="test",
                aspect_ratio="16:9",
                config=self._make_config(),
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert call_order == ["openrouter"], "openrouter must be called first"
        mock_openai.assert_not_called(), "openai must not be called when openrouter succeeds"
        mock_muapi.assert_not_called(), "muapi must not be called when openrouter succeeds"
        assert result.provider_used == "openrouter"
        assert result.used_fallback is False

    def test_openai_called_when_openrouter_fails(self, tmp_path):
        """When openrouter raises, openai must be attempted next."""
        from hypogeum.muapi import generate_thumbnail

        call_order = []

        def _fake_openrouter(*a, **kw):
            call_order.append("openrouter")
            raise MUAPIError("openrouter unavailable")

        def _fake_openai(*a, **kw):
            call_order.append("openai")
            return "gpt-image-1"

        local_path = tmp_path / "thumbnails" / "main.jpg"
        local_path.parent.mkdir(parents=True)
        local_path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 2000)

        with patch("hypogeum.muapi._generate_thumbnail_openrouter", side_effect=_fake_openrouter), \
             patch("hypogeum.muapi._generate_thumbnail_openai", side_effect=_fake_openai), \
             patch("hypogeum.muapi._attempt_thumbnail_generation") as mock_muapi, \
             patch("hypogeum.muapi._normalize_thumbnail"), \
             patch("hypogeum.muapi._apply_wordmark_overlay"):
            result = generate_thumbnail(
                deliverable_id="main",
                image_prompt="test",
                aspect_ratio="16:9",
                config=self._make_config(),
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert call_order == ["openrouter", "openai"], (
            f"Expected [openrouter, openai], got {call_order}"
        )
        mock_muapi.assert_not_called(), "muapi must not be called when openai succeeds"
        assert result.provider_used == "openai"
        assert result.used_fallback is True

    def test_muapi_called_when_openrouter_and_openai_fail(self, tmp_path):
        """When openrouter and openai both fail, muapi must be attempted last."""
        from hypogeum.muapi import generate_thumbnail

        call_order = []

        def _fake_openrouter(*a, **kw):
            call_order.append("openrouter")
            raise MUAPIError("openrouter unavailable")

        def _fake_openai(*a, **kw):
            call_order.append("openai")
            raise MUAPIError("openai unavailable")

        def _fake_muapi(*a, **kw):
            call_order.append("muapi")
            return ("gpt-image-2-text-to-image", False)

        local_path = tmp_path / "thumbnails" / "main.jpg"
        local_path.parent.mkdir(parents=True)
        local_path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 2000)

        with patch("hypogeum.muapi._generate_thumbnail_openrouter", side_effect=_fake_openrouter), \
             patch("hypogeum.muapi._generate_thumbnail_openai", side_effect=_fake_openai), \
             patch("hypogeum.muapi._attempt_thumbnail_generation", side_effect=_fake_muapi), \
             patch("hypogeum.muapi._normalize_thumbnail"), \
             patch("hypogeum.muapi._apply_wordmark_overlay"):
            result = generate_thumbnail(
                deliverable_id="main",
                image_prompt="test",
                aspect_ratio="16:9",
                config=self._make_config(),
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert call_order == ["openrouter", "openai", "muapi"], (
            f"Expected full chain [openrouter, openai, muapi], got {call_order}"
        )
        assert result.provider_used == "muapi"
        assert result.used_fallback is True

    def test_budget_exhausted_halts_chain_immediately(self, tmp_path):
        """MUAPIBudgetExhausted must never fall through to the next provider."""
        from hypogeum.muapi import generate_thumbnail

        def _fake_openrouter(*a, **kw):
            raise MUAPIBudgetExhausted("over budget")

        with patch("hypogeum.muapi._generate_thumbnail_openrouter", side_effect=_fake_openrouter), \
             patch("hypogeum.muapi._generate_thumbnail_openai") as mock_openai, \
             patch("hypogeum.muapi._attempt_thumbnail_generation") as mock_muapi:
            with pytest.raises(MUAPIBudgetExhausted):
                generate_thumbnail(
                    deliverable_id="main",
                    image_prompt="test",
                    aspect_ratio="16:9",
                    config=self._make_config(),
                    episode_id="ep_001",
                    output_dir=tmp_path,
                )

        mock_openai.assert_not_called(), "openai must NOT be tried after MUAPIBudgetExhausted"
        mock_muapi.assert_not_called(), "muapi must NOT be tried after MUAPIBudgetExhausted"

    def test_all_providers_fail_raises_muapi_error(self, tmp_path):
        """When all three providers fail, the last provider's exception is re-raised."""
        from hypogeum.muapi import generate_thumbnail

        with patch("hypogeum.muapi._generate_thumbnail_openrouter",
                   side_effect=MUAPIError("or fail")), \
             patch("hypogeum.muapi._generate_thumbnail_openai",
                   side_effect=MUAPIError("oai fail")), \
             patch("hypogeum.muapi._attempt_thumbnail_generation",
                   side_effect=MUAPIError("muapi fail")):
            with pytest.raises(MUAPIError, match="muapi fail"):
                generate_thumbnail(
                    deliverable_id="main",
                    image_prompt="test",
                    aspect_ratio="16:9",
                    config=self._make_config(),
                    episode_id="ep_001",
                    output_dir=tmp_path,
                )

    def test_chain_deduplicates_repeated_provider(self, tmp_path):
        """If primary and fallback1 are the same provider, it is only tried once."""
        from hypogeum.muapi import generate_thumbnail

        call_count = [0]

        def _fake_openrouter(*a, **kw):
            call_count[0] += 1
            raise MUAPIError("fail")

        # Set primary=openrouter, fallback1=openrouter (duplicate), fallback2=muapi
        cfg = self._make_config(primary="openrouter", fallback1="openrouter", fallback2="muapi")

        def _fake_muapi(*a, **kw):
            return ("gpt-image-2-text-to-image", False)

        local_path = tmp_path / "thumbnails" / "main.jpg"
        local_path.parent.mkdir(parents=True)
        local_path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 2000)

        with patch("hypogeum.muapi._generate_thumbnail_openrouter", side_effect=_fake_openrouter), \
             patch("hypogeum.muapi._attempt_thumbnail_generation", side_effect=_fake_muapi), \
             patch("hypogeum.muapi._normalize_thumbnail"), \
             patch("hypogeum.muapi._apply_wordmark_overlay"):
            result = generate_thumbnail(
                deliverable_id="main",
                image_prompt="test",
                aspect_ratio="16:9",
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert call_count[0] == 1, (
            f"Duplicate provider openrouter must only be called once, got {call_count[0]}"
        )
        assert result.provider_used == "muapi"
