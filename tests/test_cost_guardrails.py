"""
tests/test_cost_guardrails.py — M5.6 Production Cost Guardrails

Tests for:
  - MUAPIBudgetGuard (episode budget, scene budget, max paid attempts)
  - MUAPIBudgetExhausted exception
  - Resume safety in submit_scene() (reuse valid existing files)
  - Cheap mode (force T3, skip fallback)
  - Dry-run mode (no real API calls)
  - Paid fallback control (MUAPI_ALLOW_PAID_FALLBACKS=false)
  - HTTP 400 early abort in _poll_until_complete()
  - Episode Zero scene cap (EPISODE_ZERO_MAX_SCENES)
  - Config bool parsing for new env vars
  - Smoke Test Mode config defaults and override logic (TestSmokeTestMode)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.config import Config
from hypogeum.muapi import (
    MODEL_CREDIT_COSTS,
    MUAPIBudgetExhausted,
    MUAPIBudgetGuard,
    MUAPIError,
    _DRY_RUN_REQUEST_ID_PREFIX,
    _MIN_VIDEO_BYTES,
    _POLL_400_ABORT_THRESHOLD,
    _attempt_generation,
    _poll_until_complete,
    _submit_http,
    submit_scene,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALL_ENV_VARS = [
    "OPENROUTER_API_KEY", "OPENROUTER_SONNET_MODEL_ID", "OPENROUTER_OPUS_MODEL_ID",
    "OPENROUTER_APP_NAME", "MUAPI_API_KEY", "MUAPI_BASE_URL", "MUAPI_T1_MODEL",
    "MUAPI_T2_MODEL", "MUAPI_T3_MODEL", "MUAPI_I2V_MODEL", "MUAPI_IMAGE_MODEL",
    "MUAPI_DEFAULT_TIER", "MUAPI_TIMEOUT_SECONDS", "MUAPI_POLL_INTERVAL_SECONDS",
    "MUAPI_MAX_POLL_ATTEMPTS", "MUAPI_WEBHOOK_URL", "MUAPI_CREDIT_WARNING_THRESHOLD",
    "MUAPI_EPISODE_BUDGET_CREDITS", "MUAPI_SCENE_BUDGET_CREDITS",
    "MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE", "EPISODE_ZERO_MAX_SCENES",
    "MUAPI_CHEAP_MODE", "MUAPI_RUNWAY_ONLY_MODE", "MUAPI_DRY_RUN", "MUAPI_ALLOW_PAID_FALLBACKS",
    "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID",
    "ELEVENLABS_STABILITY", "ELEVENLABS_SIMILARITY_BOOST", "ELEVENLABS_MUSIC_MODEL_ID",
    "ELEVENLABS_MUSIC_DEFAULT_DURATION", "OPENAI_API_KEY", "WHISPER_MODEL",
    "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
    "YOUTUBE_CHANNEL_ID", "YOUTUBE_DEFAULT_CATEGORY_ID", "TIKTOK_CLIENT_KEY",
    "TIKTOK_CLIENT_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_OPEN_ID",
    "META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN", "META_INSTAGRAM_ACCOUNT_ID",
    "META_FACEBOOK_PAGE_ID", "META_GRAPH_API_VERSION", "META_STAGING_BASE_URL",
]


def make_config(
    muapi_key: str = "test-key",
    poll_interval: int = 0,
    max_poll_attempts: int = 5,
    episode_budget_credits: float = 0.0,
    scene_budget_credits: float = 0.0,
    max_paid_attempts_per_scene: int = 0,
    episode_zero_max_scenes: int = 0,
    cheap_mode: bool = False,
    runway_only_mode: bool = False,
    dry_run: bool = False,
    allow_paid_fallbacks: bool = True,
) -> Config:
    original = {k: os.environ.get(k) for k in _ALL_ENV_VARS}
    for k in _ALL_ENV_VARS:
        os.environ.pop(k, None)
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    os.environ["MUAPI_API_KEY"] = muapi_key
    os.environ["MUAPI_BASE_URL"] = "https://api.muapi.ai/api/v1"
    os.environ["MUAPI_T1_MODEL"] = "veo3-text-to-video"
    os.environ["MUAPI_T2_MODEL"] = "runway-text-to-video"
    os.environ["MUAPI_T3_MODEL"] = "veo3-fast-text-to-video"
    os.environ["MUAPI_I2V_MODEL"] = "veo3-image-to-video"
    os.environ["MUAPI_IMAGE_MODEL"] = "flux-2-dev"
    os.environ["MUAPI_POLL_INTERVAL_SECONDS"] = str(poll_interval)
    os.environ["MUAPI_MAX_POLL_ATTEMPTS"] = str(max_poll_attempts)
    if episode_budget_credits:
        os.environ["MUAPI_EPISODE_BUDGET_CREDITS"] = str(episode_budget_credits)
    if scene_budget_credits:
        os.environ["MUAPI_SCENE_BUDGET_CREDITS"] = str(scene_budget_credits)
    if max_paid_attempts_per_scene:
        os.environ["MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE"] = str(max_paid_attempts_per_scene)
    if episode_zero_max_scenes:
        os.environ["EPISODE_ZERO_MAX_SCENES"] = str(episode_zero_max_scenes)
    if cheap_mode:
        os.environ["MUAPI_CHEAP_MODE"] = "true"
    if runway_only_mode:
        os.environ["MUAPI_RUNWAY_ONLY_MODE"] = "true"
    if dry_run:
        os.environ["MUAPI_DRY_RUN"] = "true"
    # Always set explicitly — default changed to False in M5.6
    os.environ["MUAPI_ALLOW_PAID_FALLBACKS"] = "true" if allow_paid_fallbacks else "false"
    try:
        return Config()
    finally:
        for k in _ALL_ENV_VARS:
            os.environ.pop(k, None)
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v


def make_scene_spec(
    scene_id: str = "s01",
    primary_tier: int = 1,
    fallback_tier: int = 3,
    reference_image_url: str = None,
    short_id: str = None,
) -> dict:
    return {
        "scene_id": scene_id,
        "short_id": short_id,
        "visual_spec": {"aspect_ratio": "16:9", "duration_seconds": 8.0},
        "production_spec": {
            "primary_prompt": "Ancient ruins at dawn.",
            "primary_tier": primary_tier,
            "fallback_prompt": "Ancient ruins.",
            "fallback_tier": fallback_tier,
            "reference_image_url": reference_image_url,
        },
    }


def make_log() -> MagicMock:
    log = MagicMock()
    log.info = MagicMock()
    log.warn = MagicMock()
    log.error = MagicMock()
    log.debug = MagicMock()
    return log


def _write_valid_mp4(path: Path) -> None:
    """Write a minimal valid-sized fake mp4 file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x00\x00\x18ftyp" + b"\x00" * (_MIN_VIDEO_BYTES + 100))


# ---------------------------------------------------------------------------
# TestMUAPIBudgetGuard
# ---------------------------------------------------------------------------

class TestMUAPIBudgetGuard:
    def _make_guard(
        self,
        episode_budget: float = 0.0,
        scene_budget: float = 0.0,
        max_attempts: int = 0,
    ) -> MUAPIBudgetGuard:
        return MUAPIBudgetGuard(
            episode_budget_credits=episode_budget,
            scene_budget_credits=scene_budget,
            max_paid_attempts=max_attempts,
            log=make_log(),
        )

    def test_unlimited_budget_allows_any_model(self):
        guard = self._make_guard()
        guard.check_and_record_attempt("s01", "veo3-text-to-video")  # should not raise

    def test_episode_budget_blocks_when_exceeded(self):
        guard = self._make_guard(episode_budget=2.0)
        guard.check_and_record_attempt("s01", "veo3-fast-text-to-video")  # 0.6 used
        with pytest.raises(MUAPIBudgetExhausted, match="episode budget"):
            # next call would bring total to 0.6 + 2.5 = 3.1 > 2.0
            guard.check_and_record_attempt("s02", "veo3-text-to-video")

    def test_episode_budget_allows_within_limit(self):
        guard = self._make_guard(episode_budget=10.0)
        guard.check_and_record_attempt("s01", "veo3-text-to-video")  # 2.5
        guard.check_and_record_attempt("s02", "veo3-text-to-video")  # 5.0
        guard.check_and_record_attempt("s03", "veo3-fast-text-to-video")  # 5.6

    def test_scene_budget_blocks_second_attempt(self):
        guard = self._make_guard(scene_budget=2.0)
        guard.check_and_record_attempt("s01", "veo3-fast-text-to-video")  # 0.6 spent on s01
        # next s01 attempt: 0.6 + 2.5 = 3.1 > 2.0
        with pytest.raises(MUAPIBudgetExhausted, match="scene budget"):
            guard.check_and_record_attempt("s01", "veo3-text-to-video")

    def test_scene_budget_does_not_block_different_scenes(self):
        guard = self._make_guard(scene_budget=3.0)
        guard.check_and_record_attempt("s01", "veo3-text-to-video")  # 2.5 on s01
        guard.check_and_record_attempt("s02", "veo3-text-to-video")  # 2.5 on s02 — separate

    def test_max_paid_attempts_blocks_second_attempt(self):
        guard = self._make_guard(max_attempts=1)
        guard.check_and_record_attempt("s01", "veo3-fast-text-to-video")
        with pytest.raises(MUAPIBudgetExhausted, match="already used 1 paid attempt"):
            guard.check_and_record_attempt("s01", "veo3-fast-text-to-video")

    def test_max_paid_attempts_zero_is_unlimited(self):
        guard = self._make_guard(max_attempts=0)
        for _ in range(5):
            guard.check_and_record_attempt("s01", "veo3-fast-text-to-video")

    def test_max_paid_attempts_per_scene_not_episode(self):
        guard = self._make_guard(max_attempts=1)
        guard.check_and_record_attempt("s01", "veo3-fast-text-to-video")
        guard.check_and_record_attempt("s02", "veo3-fast-text-to-video")  # different scene — ok

    def test_estimated_spend_tracks_correctly(self):
        guard = self._make_guard()
        guard.check_and_record_attempt("s01", "veo3-text-to-video")      # 2.5
        guard.check_and_record_attempt("s02", "veo3-fast-text-to-video") # 0.6
        assert abs(guard.estimated_spend - 3.1) < 0.01

    def test_actual_spend_tracks_after_download(self):
        guard = self._make_guard()
        guard.check_and_record_attempt("s01", "veo3-text-to-video")
        guard.record_actual_spend(2.5)
        assert guard.actual_spend == 2.5

    def test_actual_spend_accumulates(self):
        guard = self._make_guard()
        guard.record_actual_spend(2.5)
        guard.record_actual_spend(0.6)
        assert abs(guard.actual_spend - 3.1) < 0.01

    def test_unknown_model_uses_conservative_cost(self):
        guard = self._make_guard(episode_budget=2.0)
        # Unknown model → _DEFAULT_UNKNOWN_MODEL_COST = 2.5
        with pytest.raises(MUAPIBudgetExhausted):
            guard.check_and_record_attempt("s01", "unknown-future-model")

    def test_budget_exhausted_is_muapi_error(self):
        exc = MUAPIBudgetExhausted("test reason")
        assert isinstance(exc, MUAPIError)

    def test_budget_exhausted_message_contains_reason(self):
        exc = MUAPIBudgetExhausted("scene s01 exceeded budget")
        assert "scene s01 exceeded budget" in str(exc)

    def test_model_credit_costs_table_has_veo3(self):
        assert "veo3-text-to-video" in MODEL_CREDIT_COSTS
        assert MODEL_CREDIT_COSTS["veo3-text-to-video"] == 2.5

    def test_model_credit_costs_table_has_veo3_fast(self):
        assert "veo3-fast-text-to-video" in MODEL_CREDIT_COSTS
        assert MODEL_CREDIT_COSTS["veo3-fast-text-to-video"] == 0.6


# ---------------------------------------------------------------------------
# TestResumeSafety
# ---------------------------------------------------------------------------

class TestResumeSafety:
    def test_valid_existing_file_returns_reused_status(self, tmp_path):
        config = make_config()
        scene_spec = make_scene_spec(scene_id="s01")
        mp4_path = tmp_path / "video" / "sc_s01.mp4"
        _write_valid_mp4(mp4_path)

        result = submit_scene(scene_spec, config, "ep_000", tmp_path)

        assert result["generation_status"] == "reused"
        assert result["model_used"] == "cached"
        assert result["scene_id"] == "s01"
        assert result["asset_id"] is not None

    def test_reused_scene_makes_no_http_calls(self, tmp_path):
        config = make_config()
        scene_spec = make_scene_spec(scene_id="s01")
        mp4_path = tmp_path / "video" / "sc_s01.mp4"
        _write_valid_mp4(mp4_path)

        with patch("hypogeum.muapi.requests.post") as mock_post:
            submit_scene(scene_spec, config, "ep_000", tmp_path)
            mock_post.assert_not_called()

    def test_zero_byte_file_is_not_reused(self, tmp_path):
        config = make_config(dry_run=True)
        scene_spec = make_scene_spec(scene_id="s01")
        mp4_path = tmp_path / "video" / "sc_s01.mp4"
        mp4_path.parent.mkdir(parents=True, exist_ok=True)
        mp4_path.write_bytes(b"")  # zero bytes

        result = submit_scene(scene_spec, config, "ep_000", tmp_path)
        # Should not reuse — should proceed to generation (dry-run returns dry_run status)
        assert result["generation_status"] != "reused"

    def test_small_file_below_min_bytes_is_not_reused(self, tmp_path):
        config = make_config(dry_run=True)
        scene_spec = make_scene_spec(scene_id="s01")
        mp4_path = tmp_path / "video" / "sc_s01.mp4"
        mp4_path.parent.mkdir(parents=True, exist_ok=True)
        mp4_path.write_bytes(b"\x00" * (_MIN_VIDEO_BYTES - 1))  # just under threshold

        result = submit_scene(scene_spec, config, "ep_000", tmp_path)
        assert result["generation_status"] != "reused"

    def test_reused_fallback_used_is_false(self, tmp_path):
        config = make_config()
        scene_spec = make_scene_spec(scene_id="s01")
        mp4_path = tmp_path / "video" / "sc_s01.mp4"
        _write_valid_mp4(mp4_path)

        result = submit_scene(scene_spec, config, "ep_000", tmp_path)
        assert result["fallback_used"] is False

    def test_short_scene_valid_file_returns_reused(self, tmp_path):
        config = make_config()
        scene_spec = make_scene_spec(scene_id="sh_01_s01", short_id="sh_01")
        mp4_path = tmp_path / "video" / "sh_sh_01" / "sc_sh_01_s01.mp4"
        _write_valid_mp4(mp4_path)

        result = submit_scene(scene_spec, config, "ep_000", tmp_path)
        assert result["generation_status"] == "reused"


# ---------------------------------------------------------------------------
# TestDryRunMode
# ---------------------------------------------------------------------------

class TestDryRunMode:
    def test_dry_run_submit_returns_fake_request_id(self, tmp_path):
        config = make_config(dry_run=True)

        with patch("hypogeum.muapi.requests.post") as mock_post:
            request_id, failure = _submit_http(
                endpoint="https://api.muapi.ai/api/v1/veo3-fast-text-to-video",
                payload={"prompt": "test", "aspect_ratio": "16:9"},
                config=config,
                scene_id="s01",
                log=make_log(),
            )
            mock_post.assert_not_called()

        assert failure is None
        assert request_id is not None
        assert request_id.startswith(_DRY_RUN_REQUEST_ID_PREFIX)

    def _mock_ffmpeg_writes_file(self):
        """subprocess.run side_effect that writes a real (fake-content) mp4
        file, mirroring hypogeum.muapi._generate_dry_run_placeholder_clip()'s
        real ffmpeg invocation without actually shelling out in unit tests."""
        def _side_effect(cmd, capture_output, text, timeout):
            out_path = Path(cmd[-1])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"FAKEMP4" * 2000)
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result
        return _side_effect

    def test_dry_run_submit_scene_returns_generated_status(self, tmp_path):
        """Changed 2026-07-19: dry-run now writes a real placeholder clip and
        reports "generated" (not a special "dry_run" status) so Assembly/QA
        validate the exact same contract as a real run — see
        hypogeum.muapi._generate_dry_run_placeholder_clip()'s docstring."""
        config = make_config(dry_run=True)
        scene_spec = make_scene_spec(scene_id="s01")

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.subprocess.run", side_effect=self._mock_ffmpeg_writes_file()):
            result = submit_scene(scene_spec, config, "ep_000", tmp_path)
            mock_post.assert_not_called()

        assert result["generation_status"] == "generated"
        assert result["scene_id"] == "s01"

    def test_dry_run_writes_a_real_placeholder_mp4_file(self, tmp_path):
        """Changed 2026-07-19: dry-run must write a real, structurally-valid
        placeholder clip file so Assembly's clip_availability_rate reflects
        100% (not 0%) and QA validates the same contract as a real run."""
        config = make_config(dry_run=True)
        scene_spec = make_scene_spec(scene_id="s01")

        with patch("hypogeum.muapi.requests.post"), \
             patch("hypogeum.muapi.subprocess.run", side_effect=self._mock_ffmpeg_writes_file()):
            submit_scene(scene_spec, config, "ep_000", tmp_path)

        mp4 = tmp_path / "video" / "sc_s01.mp4"
        assert mp4.exists()
        assert mp4.stat().st_size > 0

    def test_dry_run_does_not_consume_budget(self, tmp_path):
        config = make_config(dry_run=True, episode_budget_credits=0.1)
        guard = MUAPIBudgetGuard(
            episode_budget_credits=0.1,
            scene_budget_credits=0.0,
            max_paid_attempts=0,
            log=make_log(),
        )
        scene_spec = make_scene_spec(scene_id="s01")

        # Budget guard is not called in dry-run because _submit_http returns fake ID
        # before budget_guard.check_and_record_attempt fires in _attempt_generation.
        # However, in our implementation, budget guard IS checked before submission.
        # In dry-run, we want to allow the run even if budget would be exceeded.
        # This test verifies that submit_scene completes without raising.
        with patch("hypogeum.muapi.requests.post"), \
             patch("hypogeum.muapi.subprocess.run", side_effect=self._mock_ffmpeg_writes_file()):
            result = submit_scene(scene_spec, config, "ep_000", tmp_path,
                                  budget_guard=guard)
        assert result["generation_status"] == "generated"

    def test_dry_run_multiple_scenes_succeed(self, tmp_path):
        config = make_config(dry_run=True)
        for i in range(5):
            scene_spec = make_scene_spec(scene_id=f"s{i:02d}")
            with patch("hypogeum.muapi.requests.post"), \
                 patch("hypogeum.muapi.subprocess.run", side_effect=self._mock_ffmpeg_writes_file()):
                result = submit_scene(scene_spec, config, "ep_000", tmp_path)
            assert result["generation_status"] == "generated"


# ---------------------------------------------------------------------------
# TestCheapMode
# ---------------------------------------------------------------------------

class TestCheapMode:
    def test_cheap_mode_forces_t3_model(self, tmp_path):
        config = make_config(cheap_mode=True)
        scene_spec = make_scene_spec(scene_id="s01", primary_tier=1)

        submitted_endpoints = []

        def fake_post(url, **kwargs):
            submitted_endpoints.append(url)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"request_id": "req-123"}
            return resp

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "status": "completed",
                "outputs": ["https://example.com/video.mp4"],
            }
            return resp

        def fake_download(url, dest_path, log):
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(b"\x00\x00\x00\x18ftyp" + b"\x00" * (_MIN_VIDEO_BYTES + 100))
            return True, ""

        with patch("hypogeum.muapi.requests.post", side_effect=fake_post), \
             patch("hypogeum.muapi.requests.get", side_effect=fake_get), \
             patch("hypogeum.muapi._download_file", side_effect=fake_download):
            result = submit_scene(scene_spec, config, "ep_000", tmp_path)

        # Should have used T3 model endpoint
        assert any("veo3-fast-text-to-video" in ep for ep in submitted_endpoints)
        assert all("veo3-text-to-video" not in ep or "fast" in ep for ep in submitted_endpoints)

    def test_cheap_mode_skips_paid_fallback(self, tmp_path):
        config = make_config(cheap_mode=True)
        scene_spec = make_scene_spec(scene_id="s01", primary_tier=1, fallback_tier=3)

        call_count = [0]

        def fake_post(url, **kwargs):
            call_count[0] += 1
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"request_id": "req-123"}
            return resp

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"status": "failed"}
            return resp

        with patch("hypogeum.muapi.requests.post", side_effect=fake_post), \
             patch("hypogeum.muapi.requests.get", side_effect=fake_get):
            result = submit_scene(scene_spec, config, "ep_000", tmp_path)

        # Only 1 submission — cheap mode disables fallback
        assert call_count[0] == 1
        assert result["generation_status"] == "generation_failed"
        assert "fallback_disabled" in (result.get("failure_reason") or "")

    def test_cheap_mode_skips_reference_image_routing(self, tmp_path):
        config = make_config(cheap_mode=True)
        # Scene with reference image URL — cheap mode should ignore it
        scene_spec = make_scene_spec(
            scene_id="s01",
            primary_tier=1,
            reference_image_url="https://example.com/ref.jpg",
        )

        submitted_endpoints = []

        def fake_post(url, **kwargs):
            submitted_endpoints.append(url)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"request_id": "req-123"}
            return resp

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"status": "failed"}
            return resp

        with patch("hypogeum.muapi.requests.post", side_effect=fake_post), \
             patch("hypogeum.muapi.requests.get", side_effect=fake_get):
            submit_scene(scene_spec, config, "ep_000", tmp_path)

        # Should NOT use I2V model — cheap mode sets reference_image_url=None
        assert all("image-to-video" not in ep for ep in submitted_endpoints)


# ---------------------------------------------------------------------------
# TestRunwayOnlyMode
# ---------------------------------------------------------------------------

class TestRunwayOnlyMode:
    def test_runway_only_forces_t2_model(self, tmp_path):
        config = make_config(runway_only_mode=True)
        # Scene planned with T1 — should be overridden to T2
        scene_spec = make_scene_spec(scene_id="s01", primary_tier=1)

        submitted_endpoints = []

        def fake_post(url, **kwargs):
            submitted_endpoints.append(url)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"request_id": "req-123"}
            return resp

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "status": "completed",
                "outputs": ["https://example.com/video.mp4"],
            }
            return resp

        def fake_download(url, dest_path, log):
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(b"\x00\x00\x00\x18ftyp" + b"\x00" * (_MIN_VIDEO_BYTES + 100))
            return True, ""

        with patch("hypogeum.muapi.requests.post", side_effect=fake_post), \
             patch("hypogeum.muapi.requests.get", side_effect=fake_get), \
             patch("hypogeum.muapi._download_file", side_effect=fake_download):
            result = submit_scene(scene_spec, config, "ep_000", tmp_path)

        assert any("runway-text-to-video" in ep for ep in submitted_endpoints)
        assert all("veo3-text-to-video" not in ep for ep in submitted_endpoints)
        assert result["tier_used"] == 2

    def test_runway_only_overrides_t3_scene(self, tmp_path):
        config = make_config(runway_only_mode=True)
        scene_spec = make_scene_spec(scene_id="s01", primary_tier=3)

        submitted_endpoints = []

        def fake_post(url, **kwargs):
            submitted_endpoints.append(url)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"request_id": "req-123"}
            return resp

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "status": "completed",
                "outputs": ["https://example.com/video.mp4"],
            }
            return resp

        def fake_download(url, dest_path, log):
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(b"\x00\x00\x00\x18ftyp" + b"\x00" * (_MIN_VIDEO_BYTES + 100))
            return True, ""

        with patch("hypogeum.muapi.requests.post", side_effect=fake_post), \
             patch("hypogeum.muapi.requests.get", side_effect=fake_get), \
             patch("hypogeum.muapi._download_file", side_effect=fake_download):
            result = submit_scene(scene_spec, config, "ep_000", tmp_path)

        assert any("runway-text-to-video" in ep for ep in submitted_endpoints)
        assert result["tier_used"] == 2

    def test_runway_only_keeps_reference_image_url(self, tmp_path):
        """Unlike cheap_mode, runway_only keeps reference_image_url for I2V conditioning."""
        config = make_config(runway_only_mode=True)
        scene_spec = make_scene_spec(
            scene_id="s01",
            primary_tier=1,
            reference_image_url="https://example.com/ref.jpg",
        )

        submitted_endpoints = []

        def fake_post(url, **kwargs):
            submitted_endpoints.append(url)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"request_id": "req-123"}
            return resp

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"status": "failed"}
            return resp

        with patch("hypogeum.muapi.requests.post", side_effect=fake_post), \
             patch("hypogeum.muapi.requests.get", side_effect=fake_get):
            submit_scene(scene_spec, config, "ep_000", tmp_path)

        # Should use I2V endpoint (reference_image_url was NOT cleared)
        assert any("image-to-video" in ep for ep in submitted_endpoints)

    def test_runway_only_default_false(self):
        config = make_config()
        assert config.MUAPI_RUNWAY_ONLY_MODE is False

    def test_runway_only_true_via_env(self):
        config = make_config(runway_only_mode=True)
        assert config.MUAPI_RUNWAY_ONLY_MODE is True


# ---------------------------------------------------------------------------
# TestPaidFallbackControl
# ---------------------------------------------------------------------------

class TestPaidFallbackControl:
    def test_fallback_disabled_stops_after_attempt_1(self, tmp_path):
        config = make_config(allow_paid_fallbacks=False)
        scene_spec = make_scene_spec(scene_id="s01", primary_tier=1, fallback_tier=3)

        call_count = [0]

        def fake_post(url, **kwargs):
            call_count[0] += 1
            resp = MagicMock()
            resp.status_code = 400  # T1 hard fail
            return resp

        with patch("hypogeum.muapi.requests.post", side_effect=fake_post):
            result = submit_scene(scene_spec, config, "ep_000", tmp_path)

        assert call_count[0] == 1  # only 1 submission, no fallback
        assert result["generation_status"] == "generation_failed"
        assert result["fallback_used"] is False

    def test_fallback_disabled_result_contains_fallback_disabled_reason(self, tmp_path):
        config = make_config(allow_paid_fallbacks=False)
        scene_spec = make_scene_spec(scene_id="s01")

        def fake_post(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 400
            return resp

        with patch("hypogeum.muapi.requests.post", side_effect=fake_post):
            result = submit_scene(scene_spec, config, "ep_000", tmp_path)

        assert "fallback_disabled" in (result.get("failure_reason") or "")

    def test_fallback_enabled_allows_second_attempt(self, tmp_path):
        config = make_config(allow_paid_fallbacks=True)
        scene_spec = make_scene_spec(scene_id="s01", primary_tier=1, fallback_tier=3)

        call_count = [0]

        def fake_post(url, **kwargs):
            call_count[0] += 1
            resp = MagicMock()
            resp.status_code = 400 if call_count[0] == 1 else 200
            if call_count[0] == 2:
                resp.json.return_value = {"request_id": "req-fallback"}
            return resp

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"status": "failed"}
            return resp

        with patch("hypogeum.muapi.requests.post", side_effect=fake_post), \
             patch("hypogeum.muapi.requests.get", side_effect=fake_get):
            result = submit_scene(scene_spec, config, "ep_000", tmp_path)

        assert call_count[0] == 2  # two submissions (T1 failed, T3 tried)


# ---------------------------------------------------------------------------
# TestHttp400EarlyAbort
# ---------------------------------------------------------------------------

class TestHttp400EarlyAbort:
    def test_three_consecutive_400s_aborts_early(self):
        config = make_config(max_poll_attempts=20)
        log = make_log()

        call_count = [0]

        def fake_get(url, **kwargs):
            call_count[0] += 1
            resp = MagicMock()
            resp.status_code = 400
            return resp

        with patch("hypogeum.muapi.requests.get", side_effect=fake_get), \
             patch("hypogeum.muapi.time.sleep"):
            result, reason = _poll_until_complete("req-abc", config, "s01", log)

        assert result is None
        assert reason == "http_400_persistent"
        # Should have aborted at exactly _POLL_400_ABORT_THRESHOLD polls
        assert call_count[0] == _POLL_400_ABORT_THRESHOLD

    def test_two_400s_then_success_does_not_abort(self):
        config = make_config(max_poll_attempts=10)
        log = make_log()

        call_count = [0]

        def fake_get(url, **kwargs):
            call_count[0] += 1
            resp = MagicMock()
            if call_count[0] <= 2:
                resp.status_code = 400
            else:
                resp.status_code = 200
                resp.json.return_value = {
                    "status": "completed",
                    "outputs": ["https://example.com/video.mp4"],
                }
            return resp

        with patch("hypogeum.muapi.requests.get", side_effect=fake_get), \
             patch("hypogeum.muapi.time.sleep"):
            result, reason = _poll_until_complete("req-abc", config, "s01", log)

        assert result is not None
        assert reason == "ok"

    def test_400_counter_resets_on_200(self):
        config = make_config(max_poll_attempts=10)
        log = make_log()

        calls = [0]
        sequence = [400, 400, 200, 400, 400, 200, 200]

        def fake_get(url, **kwargs):
            resp = MagicMock()
            idx = calls[0]
            calls[0] += 1
            status = sequence[idx] if idx < len(sequence) else 200
            resp.status_code = status
            if status == 200:
                resp.json.return_value = {
                    "status": "completed",
                    "outputs": ["https://example.com/video.mp4"],
                }
            return resp

        with patch("hypogeum.muapi.requests.get", side_effect=fake_get), \
             patch("hypogeum.muapi.time.sleep"):
            result, reason = _poll_until_complete("req-abc", config, "s01", log)

        # Reset after 200 at index 2, so the 400s at index 3-4 don't form a trio
        assert result is not None
        assert reason == "ok"

    def test_abort_threshold_value_is_three(self):
        assert _POLL_400_ABORT_THRESHOLD == 3


# ---------------------------------------------------------------------------
# TestBudgetGuardIntegrationWithSubmitScene
# ---------------------------------------------------------------------------

class TestBudgetGuardIntegrationWithSubmitScene:
    def test_budget_guard_blocks_submission_when_exceeded(self, tmp_path):
        config = make_config()
        scene_spec = make_scene_spec(scene_id="s01")
        guard = MUAPIBudgetGuard(
            episode_budget_credits=0.1,  # too low for any model
            scene_budget_credits=0.0,
            max_paid_attempts=0,
            log=make_log(),
        )

        with patch("hypogeum.muapi.requests.post") as mock_post:
            with pytest.raises(MUAPIBudgetExhausted):
                submit_scene(scene_spec, config, "ep_000", tmp_path, budget_guard=guard)
            mock_post.assert_not_called()

    def test_budget_guard_blocks_fallback_after_first_spend(self, tmp_path):
        config = make_config(allow_paid_fallbacks=True)
        scene_spec = make_scene_spec(scene_id="s01", primary_tier=1, fallback_tier=3)
        # Budget: 2.5 credits. T1 = 2.5, so after T1 attempt, T3 fallback (0.6) would exceed.
        guard = MUAPIBudgetGuard(
            episode_budget_credits=2.5,
            scene_budget_credits=0.0,
            max_paid_attempts=0,
            log=make_log(),
        )

        call_count = [0]

        def fake_post(url, **kwargs):
            call_count[0] += 1
            resp = MagicMock()
            resp.status_code = 400  # T1 fails
            return resp

        with patch("hypogeum.muapi.requests.post", side_effect=fake_post):
            with pytest.raises(MUAPIBudgetExhausted):
                submit_scene(scene_spec, config, "ep_000", tmp_path, budget_guard=guard)

        assert call_count[0] == 1  # Only T1 submitted before budget was consumed

    def test_max_paid_attempts_1_per_scene_blocks_fallback(self, tmp_path):
        config = make_config(allow_paid_fallbacks=True)
        scene_spec = make_scene_spec(scene_id="s01", primary_tier=1, fallback_tier=3)
        guard = MUAPIBudgetGuard(
            episode_budget_credits=0.0,
            scene_budget_credits=0.0,
            max_paid_attempts=1,
            log=make_log(),
        )

        call_count = [0]

        def fake_post(url, **kwargs):
            call_count[0] += 1
            resp = MagicMock()
            resp.status_code = 400  # T1 fails
            return resp

        with patch("hypogeum.muapi.requests.post", side_effect=fake_post):
            with pytest.raises(MUAPIBudgetExhausted):
                submit_scene(scene_spec, config, "ep_000", tmp_path, budget_guard=guard)

        assert call_count[0] == 1


# ---------------------------------------------------------------------------
# TestConfigBoolParsing
# ---------------------------------------------------------------------------

class TestConfigBoolParsing:
    def _make_config_with_flags(self, **env_overrides):
        original = {k: os.environ.get(k) for k in _ALL_ENV_VARS}
        for k in _ALL_ENV_VARS:
            os.environ.pop(k, None)
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        os.environ["MUAPI_API_KEY"] = "test"
        os.environ["MUAPI_BASE_URL"] = "https://api.muapi.ai/api/v1"
        for k, v in env_overrides.items():
            os.environ[k] = v
        # Patch _load_dotenv so the real .env file does not re-populate cleared vars
        with patch("hypogeum.config._load_dotenv"):
            try:
                return Config()
            finally:
                for k in _ALL_ENV_VARS:
                    os.environ.pop(k, None)
                for k, v in original.items():
                    if v is not None:
                        os.environ[k] = v

    def test_muapi_dry_run_default_false(self):
        config = self._make_config_with_flags()
        assert config.MUAPI_DRY_RUN is False

    def test_muapi_dry_run_true(self):
        config = self._make_config_with_flags(MUAPI_DRY_RUN="true")
        assert config.MUAPI_DRY_RUN is True

    def test_muapi_dry_run_1(self):
        config = self._make_config_with_flags(MUAPI_DRY_RUN="1")
        assert config.MUAPI_DRY_RUN is True

    def test_muapi_cheap_mode_default_false(self):
        config = self._make_config_with_flags()
        assert config.MUAPI_CHEAP_MODE is False

    def test_muapi_cheap_mode_true(self):
        config = self._make_config_with_flags(MUAPI_CHEAP_MODE="true")
        assert config.MUAPI_CHEAP_MODE is True

    def test_muapi_allow_paid_fallbacks_default_false(self):
        # M5.6 — default changed from True to False for safety
        config = self._make_config_with_flags()
        assert config.MUAPI_ALLOW_PAID_FALLBACKS is False

    def test_muapi_allow_paid_fallbacks_false(self):
        config = self._make_config_with_flags(MUAPI_ALLOW_PAID_FALLBACKS="false")
        assert config.MUAPI_ALLOW_PAID_FALLBACKS is False

    def test_muapi_allow_paid_fallbacks_zero(self):
        config = self._make_config_with_flags(MUAPI_ALLOW_PAID_FALLBACKS="0")
        assert config.MUAPI_ALLOW_PAID_FALLBACKS is False

    def test_muapi_episode_budget_credits_default_zero(self):
        config = self._make_config_with_flags()
        assert config.MUAPI_EPISODE_BUDGET_CREDITS == 0.0

    def test_muapi_episode_budget_credits_set(self):
        config = self._make_config_with_flags(MUAPI_EPISODE_BUDGET_CREDITS="10.5")
        assert config.MUAPI_EPISODE_BUDGET_CREDITS == 10.5

    def test_muapi_max_paid_attempts_default_zero(self):
        config = self._make_config_with_flags()
        assert config.MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE == 0

    def test_muapi_max_paid_attempts_set(self):
        config = self._make_config_with_flags(MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE="2")
        assert config.MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE == 2

    def test_episode_zero_max_scenes_default_zero(self):
        config = self._make_config_with_flags()
        assert config.EPISODE_ZERO_MAX_SCENES == 0

    def test_episode_zero_max_scenes_set(self):
        config = self._make_config_with_flags(EPISODE_ZERO_MAX_SCENES="6")
        assert config.EPISODE_ZERO_MAX_SCENES == 6


# ---------------------------------------------------------------------------
# TestEpisodeZeroSceneCap (VPD integration)
# ---------------------------------------------------------------------------

class TestEpisodeZeroSceneCap:
    def _make_vpd_package(self, n_scenes: int) -> dict:
        return {
            "episode_id": "ep_000",
            "visual_continuity_record": {"locked_entities": []},
            "scenes": [
                {
                    "scene_id": f"s{i:02d}",
                    "scene_index": i,
                    "source_type": "main_episode",
                    "short_id": None,
                    "entity_appearances": [],
                    "visual_spec": {"aspect_ratio": "16:9", "duration_seconds": 8.0},
                    "production_spec": {
                        "primary_prompt": f"Scene {i} prompt.",
                        "primary_tier": 3,
                        "fallback_prompt": f"Scene {i} fallback.",
                        "fallback_tier": 3,
                        "provider_routing": {},
                    },
                }
                for i in range(1, n_scenes + 1)
            ],
            "asset_manifest": {"assets": []},
            "provider_routing_manifest": {},
        }

    def test_scene_cap_truncates_scene_list(self, tmp_path):
        from hypogeum.agents.visual_package_director import _execute_muapi_phase

        config = make_config(
            dry_run=True,
            episode_zero_max_scenes=3,
        )
        package = self._make_vpd_package(10)
        log = make_log()

        generated_scene_ids = []

        def fake_submit(scene_spec, cfg, ep_id, out_dir, **kwargs):
            generated_scene_ids.append(scene_spec["scene_id"])
            return {
                "scene_id": scene_spec["scene_id"],
                "asset_id": f"episodes/ep_000/video/sc_{scene_spec['scene_id']}",
                "generation_status": "dry_run",
                "provider": "muapi",
                "tier_used": 3,
                "model_used": "veo3-fast-text-to-video",
                "prompt_used": "test",
                "fallback_used": False,
                "failure_reason": None,
            }

        with patch("hypogeum.agents.video_generation_agent.submit_scene", side_effect=fake_submit):
            _execute_muapi_phase(package, config, "ep_000", tmp_path, log)

        assert len(generated_scene_ids) == 3
        assert generated_scene_ids == ["s01", "s02", "s03"]

    def test_scene_cap_zero_does_not_truncate(self, tmp_path):
        from hypogeum.agents.visual_package_director import _execute_muapi_phase

        config = make_config(dry_run=True, episode_zero_max_scenes=0)
        package = self._make_vpd_package(5)
        log = make_log()

        generated_scene_ids = []

        def fake_submit(scene_spec, cfg, ep_id, out_dir, **kwargs):
            generated_scene_ids.append(scene_spec["scene_id"])
            return {
                "scene_id": scene_spec["scene_id"],
                "asset_id": f"episodes/ep_000/video/sc_{scene_spec['scene_id']}",
                "generation_status": "dry_run",
                "provider": "muapi",
                "tier_used": 3,
                "model_used": "veo3-fast-text-to-video",
                "prompt_used": "test",
                "fallback_used": False,
                "failure_reason": None,
            }

        with patch("hypogeum.agents.video_generation_agent.submit_scene", side_effect=fake_submit):
            _execute_muapi_phase(package, config, "ep_000", tmp_path, log)

        assert len(generated_scene_ids) == 5

    def test_scene_cap_greater_than_total_does_not_truncate(self, tmp_path):
        from hypogeum.agents.visual_package_director import _execute_muapi_phase

        config = make_config(dry_run=True, episode_zero_max_scenes=100)
        package = self._make_vpd_package(3)
        log = make_log()

        generated_scene_ids = []

        def fake_submit(scene_spec, cfg, ep_id, out_dir, **kwargs):
            generated_scene_ids.append(scene_spec["scene_id"])
            return {
                "scene_id": scene_spec["scene_id"],
                "asset_id": f"episodes/ep_000/video/sc_{scene_spec['scene_id']}",
                "generation_status": "dry_run",
                "provider": "muapi", "tier_used": 3,
                "model_used": "veo3-fast-text-to-video",
                "prompt_used": "test", "fallback_used": False, "failure_reason": None,
            }

        with patch("hypogeum.agents.video_generation_agent.submit_scene", side_effect=fake_submit):
            _execute_muapi_phase(package, config, "ep_000", tmp_path, log)

        assert len(generated_scene_ids) == 3


# ---------------------------------------------------------------------------
# TestBudgetExhaustedPropagation
# ---------------------------------------------------------------------------

class TestBudgetExhaustedPropagation:
    def test_budget_exhausted_propagates_from_execute_muapi_phase(self, tmp_path):
        from hypogeum.agents.visual_package_director import _execute_muapi_phase

        config = make_config()
        package = {
            "episode_id": "ep_000",
            "visual_continuity_record": {"locked_entities": []},
            "scenes": [
                {
                    "scene_id": "s01",
                    "scene_index": 1,
                    "source_type": "main_episode",
                    "short_id": None,
                    "entity_appearances": [],
                    "visual_spec": {"aspect_ratio": "16:9", "duration_seconds": 8.0},
                    "production_spec": {
                        "primary_prompt": "Test.",
                        "primary_tier": 1,
                        "fallback_prompt": "Fallback.",
                        "fallback_tier": 3,
                        "provider_routing": {},
                    },
                }
            ],
            "asset_manifest": {"assets": []},
            "provider_routing_manifest": {},
        }
        log = make_log()

        def budget_exhausted_submit(scene_spec, cfg, ep_id, out_dir, **kwargs):
            raise MUAPIBudgetExhausted("test limit reached")

        with patch(
            "hypogeum.agents.video_generation_agent.submit_scene",
            side_effect=budget_exhausted_submit,
        ):
            with pytest.raises(MUAPIBudgetExhausted):
                _execute_muapi_phase(package, config, "ep_000", tmp_path, log)


# ---------------------------------------------------------------------------
# TestSmokeTestMode — Config defaults and override logic
# ---------------------------------------------------------------------------

def _make_smoke_config(**overrides) -> Config:
    """
    Build a Config with SMOKE_TEST_MODE=true and optional env overrides.
    Clears all env vars first so defaults are deterministic.
    """
    env = {k: os.environ.get(k) for k in _ALL_ENV_VARS}
    for k in _ALL_ENV_VARS:
        os.environ.pop(k, None)
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    os.environ["SMOKE_TEST_MODE"] = "true"
    for k, v in overrides.items():
        os.environ[k] = str(v)
    try:
        return Config()
    finally:
        for k in _ALL_ENV_VARS + ["SMOKE_TEST_MODE", "SMOKE_TEST_MAX_NARRATION_BEATS"]:
            os.environ.pop(k, None)
        for k, v in env.items():
            if v is not None:
                os.environ[k] = v


class TestSmokeTestMode:
    def test_smoke_mode_off_by_default(self):
        cfg = make_config()
        assert cfg.SMOKE_TEST_MODE is False

    def test_smoke_max_narration_beats_zero_by_default(self):
        cfg = make_config()
        assert cfg.SMOKE_TEST_MAX_NARRATION_BEATS == 0

    def test_smoke_mode_sets_episode_zero_max_scenes_to_2(self):
        cfg = _make_smoke_config()
        assert cfg.EPISODE_ZERO_MAX_SCENES == 2

    def test_smoke_mode_does_not_override_explicit_episode_zero_max_scenes(self):
        cfg = _make_smoke_config(EPISODE_ZERO_MAX_SCENES=5)
        assert cfg.EPISODE_ZERO_MAX_SCENES == 5

    def test_smoke_mode_enables_runway_only_mode(self):
        cfg = _make_smoke_config()
        assert cfg.MUAPI_RUNWAY_ONLY_MODE is True

    def test_smoke_mode_does_not_override_explicit_runway_only_false(self):
        # When MUAPI_CHEAP_MODE is set, MUAPI_RUNWAY_ONLY_MODE stays False
        cfg = _make_smoke_config(MUAPI_CHEAP_MODE="true")
        assert cfg.MUAPI_RUNWAY_ONLY_MODE is False

    def test_smoke_mode_does_not_override_explicit_runway_only_true(self):
        cfg = _make_smoke_config(MUAPI_RUNWAY_ONLY_MODE="true")
        assert cfg.MUAPI_RUNWAY_ONLY_MODE is True

    def test_smoke_mode_false_does_not_set_runway_only(self):
        env = {k: os.environ.get(k) for k in _ALL_ENV_VARS}
        for k in _ALL_ENV_VARS:
            os.environ.pop(k, None)
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        os.environ["SMOKE_TEST_MODE"] = "false"
        try:
            cfg = Config()
            assert cfg.MUAPI_RUNWAY_ONLY_MODE is False
            assert cfg.EPISODE_ZERO_MAX_SCENES == 0
        finally:
            for k in _ALL_ENV_VARS + ["SMOKE_TEST_MODE"]:
                os.environ.pop(k, None)
            for k, v in env.items():
                if v is not None:
                    os.environ[k] = v

    def test_smoke_max_narration_beats_parsed(self):
        env = {k: os.environ.get(k) for k in _ALL_ENV_VARS}
        for k in _ALL_ENV_VARS:
            os.environ.pop(k, None)
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        os.environ["SMOKE_TEST_MAX_NARRATION_BEATS"] = "3"
        try:
            cfg = Config()
            assert cfg.SMOKE_TEST_MAX_NARRATION_BEATS == 3
        finally:
            for k in _ALL_ENV_VARS + ["SMOKE_TEST_MAX_NARRATION_BEATS"]:
                os.environ.pop(k, None)
            for k, v in env.items():
                if v is not None:
                    os.environ[k] = v
