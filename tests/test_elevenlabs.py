"""
tests/test_elevenlabs.py — Unit tests for hypogeum/elevenlabs.py

All HTTP calls are mocked. No real network calls. No real API keys required.
No real ElevenLabs or OpenAI credits consumed.

Test coverage:
  TestGenerateNarrationSuccess     — happy path: TTS call, audio written, asset_id, request_id, alignment_source="tts", character_cost
  TestNullAlignmentPath            — both null → forced_alignment called; all null → whisper; alignment_source="none" when all fail
  TestForcedAlignmentFallback      — returns word timings, word timings structure, error handling
  TestWhisperFallback              — returns word timings, calls OpenAI endpoint, raises when OPENAI_API_KEY missing
  TestAuthFailure                  — 401 raises ElevenLabsAuthFailure
  TestRateLimitRetry               — 429 retried once, Retry-After header respected, second 429 raises ElevenLabsRateLimitError
  TestQuotaExhaustion              — body keyword detection raises ElevenLabsQuotaExhausted; 429 does NOT raise QuotaExhausted
  TestBuildSrt                     — empty segments, basic SRT format, index numbering, sequential offsets, explicit offsets, HH:MM:SS,mmm format
  TestCharsToWords                 — basic word grouping, space excluded, empty alignment
  TestFetchModelCostMultiplier     — returns multiplier from API, returns 1.0 on API failure, caching
  TestInternalHelpers              — _parse_char_alignment, _format_srt_timestamp, _output_format_extension, _retry_delay, _detect_quota_exhaustion
  TestExceptionHierarchy           — class inheritance
"""

from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, call, patch

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.config import Config
from hypogeum.elevenlabs import (
    # Public API
    generate_narration_segment,
    forced_alignment,
    whisper_fallback,
    build_srt,
    fetch_model_cost_multiplier,
    # Dataclasses
    CharAlignment,
    WordTiming,
    NarrationSegmentResult,
    # Exceptions
    ElevenLabsError,
    ElevenLabsAuthFailure,
    ElevenLabsQuotaExhausted,
    ElevenLabsRateLimitError,
    # Internal helpers (exposed for testing)
    _parse_char_alignment,
    _chars_to_words,
    _format_srt_timestamp,
    _output_format_extension,
    _retry_delay,
    _detect_quota_exhaustion,
    _model_cost_cache,
    _RETRY_MAX,
    _QUOTA_KEYWORDS,
    _SRT_MAX_WORDS_PER_LINE,
)


# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------

_ALL_ENV_VARS = [
    "OPENROUTER_API_KEY",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
    "ELEVENLABS_MODEL_ID",
    "ELEVENLABS_OUTPUT_FORMAT",
    "ELEVENLABS_STABILITY",
    "ELEVENLABS_SIMILARITY_BOOST",
    "ELEVENLABS_TIMEOUT_SECONDS",
    "OPENAI_API_KEY",
    "WHISPER_MODEL",
]

_FAKE_AUDIO_B64 = base64.b64encode(b"\xff\xfb\x90\x00" * 100).decode()  # minimal valid bytes


def _make_config(
    api_key: str = "test-xi-key",
    voice_id: str = "voice-abc",
    openai_key: str = "sk-openai-test",
) -> Config:
    """Return a Config loaded from an isolated env (no .env file side-effects)."""
    env = {
        "OPENROUTER_API_KEY": "test-or-key",
        "ELEVENLABS_API_KEY": api_key,
        "ELEVENLABS_VOICE_ID": voice_id,
        "ELEVENLABS_TIMEOUT_SECONDS": "10",
    }
    if openai_key:
        env["OPENAI_API_KEY"] = openai_key
    with patch.dict(os.environ, env, clear=True):
        return Config()


def _minimal_alignment_payload() -> dict:
    """TTS response with alignment data for 'hello world'."""
    return {
        "audio_base64": _FAKE_AUDIO_B64,
        "normalized_alignment": {
            "characters": list("hello world"),
            "character_start_times_seconds": [i * 0.1 for i in range(11)],
            "character_end_times_seconds":   [i * 0.1 + 0.1 for i in range(11)],
        },
        "alignment": None,
    }


def _null_alignment_payload() -> dict:
    """TTS response with both alignment fields null."""
    return {
        "audio_base64": _FAKE_AUDIO_B64,
        "normalized_alignment": None,
        "alignment": None,
    }


def _mock_tts_response(payload: dict, status: int = 200, headers: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = payload
    resp.headers = {"request-id": "req-123", **(headers or {})}
    resp.text = ""
    return resp


def _make_word_timings(words: list[str]) -> List[WordTiming]:
    timings = []
    for i, w in enumerate(words):
        timings.append(WordTiming(word=w, start_seconds=float(i), end_seconds=float(i) + 0.8))
    return timings


def _make_segment(
    beat_id: str = "b_001",
    word_timings: list | None = None,
    duration: float = 3.0,
    alignment_source: str = "tts",
) -> NarrationSegmentResult:
    return NarrationSegmentResult(
        beat_id=beat_id,
        asset_id=f"episodes/ep_001/narration/{beat_id}",
        local_path=Path(f"/fake/{beat_id}.mp3"),
        duration_seconds=duration,
        request_id=f"req_{beat_id}",
        character_cost=20,
        word_timings=word_timings if word_timings is not None else _make_word_timings(["hello", "world"]),
        alignment_source=alignment_source,
    )


# ---------------------------------------------------------------------------
# TestGenerateNarrationSuccess
# ---------------------------------------------------------------------------

class TestGenerateNarrationSuccess:
    """Happy path: TTS call succeeds, audio written to disk, result fields correct."""

    def _run(self, tmp_path: Path) -> NarrationSegmentResult:
        cfg = _make_config()
        with patch("hypogeum.elevenlabs.requests.post") as mock_post:
            mock_post.return_value = _mock_tts_response(_minimal_alignment_payload())
            return generate_narration_segment(
                beat_id="b_001",
                text="hello world",
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

    def test_returns_narration_segment_result(self, tmp_path):
        result = self._run(tmp_path)
        assert isinstance(result, NarrationSegmentResult)

    def test_audio_file_written_to_disk(self, tmp_path):
        self._run(tmp_path)
        assert (tmp_path / "narration" / "b_001.mp3").exists()

    def test_asset_id_contains_episode_and_beat(self, tmp_path):
        result = self._run(tmp_path)
        assert "ep_001" in result.asset_id
        assert "b_001" in result.asset_id

    def test_asset_id_format(self, tmp_path):
        result = self._run(tmp_path)
        assert result.asset_id == "episodes/ep_001/narration/b_001"

    def test_request_id_captured_from_header(self, tmp_path):
        result = self._run(tmp_path)
        assert result.request_id == "req-123"

    def test_alignment_source_is_tts(self, tmp_path):
        result = self._run(tmp_path)
        assert result.alignment_source == "tts"

    def test_character_cost_equals_text_length(self, tmp_path):
        result = self._run(tmp_path)
        assert result.character_cost == len("hello world")

    def test_word_timings_populated(self, tmp_path):
        result = self._run(tmp_path)
        assert len(result.word_timings) == 2
        assert result.word_timings[0].word == "hello"
        assert result.word_timings[1].word == "world"

    def test_previous_request_ids_capped_at_3(self, tmp_path):
        cfg = _make_config()
        captured_body = {}
        def capture_post(url, json=None, **kw):
            captured_body.update(json or {})
            return _mock_tts_response(_minimal_alignment_payload())
        with patch("hypogeum.elevenlabs.requests.post", side_effect=capture_post):
            generate_narration_segment(
                beat_id="b_001",
                text="hello world",
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
                previous_request_ids=["r1", "r2", "r3", "r4"],
            )
        assert captured_body["previous_request_ids"] == ["r1", "r2", "r3"]

    def test_request_id_fallback_when_header_missing(self, tmp_path):
        cfg = _make_config()
        resp = _mock_tts_response(_minimal_alignment_payload())
        resp.headers = {}  # no request-id header
        with patch("hypogeum.elevenlabs.requests.post", return_value=resp):
            result = generate_narration_segment(
                beat_id="b_fallback",
                text="hi",
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )
        assert result.request_id == "el_b_fallback"

    def test_missing_voice_id_raises(self, tmp_path):
        cfg = _make_config(voice_id="")
        with patch("hypogeum.elevenlabs.requests.post"):
            with pytest.raises(ElevenLabsError, match="ELEVENLABS_VOICE_ID"):
                generate_narration_segment(
                    beat_id="b_001", text="hello", config=cfg,
                    episode_id="ep_001", output_dir=tmp_path,
                )

    def test_missing_audio_base64_raises(self, tmp_path):
        cfg = _make_config()
        payload = {"audio_base64": "", "normalized_alignment": None, "alignment": None}
        with patch("hypogeum.elevenlabs.requests.post", return_value=_mock_tts_response(payload)):
            with pytest.raises(ElevenLabsError, match="audio_base64"):
                generate_narration_segment(
                    beat_id="b_001", text="hello", config=cfg,
                    episode_id="ep_001", output_dir=tmp_path,
                )


# ---------------------------------------------------------------------------
# TestNullAlignmentPath
# ---------------------------------------------------------------------------

class TestNullAlignmentPath:
    """Both alignment fields null → cascade to forced_alignment then whisper."""

    def test_null_alignment_calls_forced_alignment(self, tmp_path):
        cfg = _make_config()
        forced_words = _make_word_timings(["hello", "world"])
        with patch("hypogeum.elevenlabs.requests.post",
                   return_value=_mock_tts_response(_null_alignment_payload())):
            with patch("hypogeum.elevenlabs.forced_alignment",
                       return_value=forced_words) as mock_fa:
                result = generate_narration_segment(
                    beat_id="b_001", text="hello world", config=cfg,
                    episode_id="ep_001", output_dir=tmp_path,
                )
        mock_fa.assert_called_once()
        assert result.alignment_source == "forced_alignment"

    def test_forced_alignment_fail_calls_whisper(self, tmp_path):
        cfg = _make_config()
        whisper_words = _make_word_timings(["hello"])
        with patch("hypogeum.elevenlabs.requests.post",
                   return_value=_mock_tts_response(_null_alignment_payload())):
            with patch("hypogeum.elevenlabs.forced_alignment",
                       side_effect=ElevenLabsError("fa failed")):
                with patch("hypogeum.elevenlabs.whisper_fallback",
                           return_value=whisper_words) as mock_wh:
                    result = generate_narration_segment(
                        beat_id="b_001", text="hello world", config=cfg,
                        episode_id="ep_001", output_dir=tmp_path,
                    )
        mock_wh.assert_called_once()
        assert result.alignment_source == "whisper"

    def test_all_fallbacks_fail_gives_none_source(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.elevenlabs.requests.post",
                   return_value=_mock_tts_response(_null_alignment_payload())):
            with patch("hypogeum.elevenlabs.forced_alignment",
                       side_effect=ElevenLabsError("fa failed")):
                with patch("hypogeum.elevenlabs.whisper_fallback",
                           side_effect=ElevenLabsError("whisper failed")):
                    result = generate_narration_segment(
                        beat_id="b_001", text="hello world", config=cfg,
                        episode_id="ep_001", output_dir=tmp_path,
                    )
        assert result.alignment_source == "none"
        assert result.word_timings == []

    def test_auth_failure_propagates_through_cascade(self, tmp_path):
        """ElevenLabsAuthFailure inside forced_alignment must not be swallowed."""
        cfg = _make_config()
        with patch("hypogeum.elevenlabs.requests.post",
                   return_value=_mock_tts_response(_null_alignment_payload())):
            with patch("hypogeum.elevenlabs.forced_alignment",
                       side_effect=ElevenLabsAuthFailure()):
                with pytest.raises(ElevenLabsAuthFailure):
                    generate_narration_segment(
                        beat_id="b_001", text="hello", config=cfg,
                        episode_id="ep_001", output_dir=tmp_path,
                    )

    def test_quota_exhausted_propagates_through_cascade(self, tmp_path):
        """ElevenLabsQuotaExhausted inside forced_alignment must not be swallowed."""
        cfg = _make_config()
        with patch("hypogeum.elevenlabs.requests.post",
                   return_value=_mock_tts_response(_null_alignment_payload())):
            with patch("hypogeum.elevenlabs.forced_alignment",
                       side_effect=ElevenLabsQuotaExhausted()):
                with pytest.raises(ElevenLabsQuotaExhausted):
                    generate_narration_segment(
                        beat_id="b_001", text="hello", config=cfg,
                        episode_id="ep_001", output_dir=tmp_path,
                    )


# ---------------------------------------------------------------------------
# TestForcedAlignmentFallback
# ---------------------------------------------------------------------------

class TestForcedAlignmentFallback:
    """Tests for the forced_alignment() public function."""

    def _fa_response(self) -> MagicMock:
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "words": [
                {"text": "hello", "start": 0.1, "end": 0.5, "loss": 0.01},
                {"text": "world", "start": 0.6, "end": 1.0, "loss": 0.01},
            ]
        }
        resp.headers = {}
        resp.text = ""
        return resp

    def test_returns_word_timings(self, tmp_path):
        cfg = _make_config()
        audio_file = tmp_path / "b_001.mp3"
        audio_file.write_bytes(b"\xff" * 100)
        with patch("hypogeum.elevenlabs.requests.post", return_value=self._fa_response()):
            result = forced_alignment(
                beat_id="b_001",
                audio_path=audio_file,
                transcript="hello world",
                config=cfg,
                episode_id="ep_001",
            )
        assert len(result) == 2

    def test_word_timing_fields(self, tmp_path):
        cfg = _make_config()
        audio_file = tmp_path / "b_001.mp3"
        audio_file.write_bytes(b"\xff" * 100)
        with patch("hypogeum.elevenlabs.requests.post", return_value=self._fa_response()):
            result = forced_alignment(
                beat_id="b_001",
                audio_path=audio_file,
                transcript="hello world",
                config=cfg,
                episode_id="ep_001",
            )
        assert result[0].word == "hello"
        assert result[0].start_seconds == pytest.approx(0.1)
        assert result[0].end_seconds == pytest.approx(0.5)

    def test_401_raises_auth_failure(self, tmp_path):
        cfg = _make_config()
        audio_file = tmp_path / "b_001.mp3"
        audio_file.write_bytes(b"\xff" * 100)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 401
        resp.text = "Unauthorized"
        resp.headers = {}
        with patch("hypogeum.elevenlabs.requests.post", return_value=resp):
            with pytest.raises(ElevenLabsAuthFailure):
                forced_alignment(
                    beat_id="b_001",
                    audio_path=audio_file,
                    transcript="hello",
                    config=cfg,
                    episode_id="ep_001",
                )

    def test_empty_words_list_returns_empty(self, tmp_path):
        cfg = _make_config()
        audio_file = tmp_path / "b_001.mp3"
        audio_file.write_bytes(b"\xff" * 100)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.json.return_value = {"words": []}
        resp.headers = {}
        resp.text = ""
        with patch("hypogeum.elevenlabs.requests.post", return_value=resp):
            result = forced_alignment(
                beat_id="b_001",
                audio_path=audio_file,
                transcript="hello",
                config=cfg,
                episode_id="ep_001",
            )
        assert result == []

    def test_whitespace_only_words_excluded(self, tmp_path):
        cfg = _make_config()
        audio_file = tmp_path / "b_001.mp3"
        audio_file.write_bytes(b"\xff" * 100)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "words": [
                {"text": "hello", "start": 0.0, "end": 0.3},
                {"text": "   ", "start": 0.3, "end": 0.4},
                {"text": "world", "start": 0.4, "end": 0.7},
            ]
        }
        resp.headers = {}
        resp.text = ""
        with patch("hypogeum.elevenlabs.requests.post", return_value=resp):
            result = forced_alignment(
                beat_id="b_001",
                audio_path=audio_file,
                transcript="hello world",
                config=cfg,
                episode_id="ep_001",
            )
        words = [w.word for w in result]
        assert "   " not in words
        assert len(result) == 2


# ---------------------------------------------------------------------------
# TestWhisperFallback
# ---------------------------------------------------------------------------

class TestWhisperFallback:
    """Tests for the whisper_fallback() public function."""

    def _whisper_response(self) -> MagicMock:
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.json.return_value = {
            "text": "hello world",
            "words": [
                {"word": "hello", "start": 0.0, "end": 0.4},
                {"word": "world", "start": 0.5, "end": 0.9},
            ]
        }
        resp.text = ""
        return resp

    def test_returns_word_timings(self, tmp_path):
        cfg = _make_config()
        audio_file = tmp_path / "b_001.mp3"
        audio_file.write_bytes(b"\xff" * 100)
        with patch("hypogeum.elevenlabs.requests.post", return_value=self._whisper_response()):
            result = whisper_fallback(
                beat_id="b_001",
                audio_path=audio_file,
                config=cfg,
                episode_id="ep_001",
            )
        assert len(result) == 2
        assert result[0].word == "hello"

    def test_calls_openai_transcriptions_endpoint(self, tmp_path):
        cfg = _make_config()
        audio_file = tmp_path / "b_001.mp3"
        audio_file.write_bytes(b"\xff" * 100)
        with patch("hypogeum.elevenlabs.requests.post", return_value=self._whisper_response()) as mock_post:
            whisper_fallback(
                beat_id="b_001",
                audio_path=audio_file,
                config=cfg,
                episode_id="ep_001",
            )
        url = mock_post.call_args[0][0]
        assert "audio/transcriptions" in url
        assert "openai.com" in url

    def test_raises_when_openai_key_missing(self, tmp_path):
        cfg = _make_config(openai_key="")
        audio_file = tmp_path / "b_001.mp3"
        audio_file.write_bytes(b"\xff" * 100)
        with pytest.raises(ElevenLabsError, match="OPENAI_API_KEY"):
            whisper_fallback(
                beat_id="b_001",
                audio_path=audio_file,
                config=cfg,
                episode_id="ep_001",
            )

    def test_401_raises_elevenlabs_error(self, tmp_path):
        cfg = _make_config()
        audio_file = tmp_path / "b_001.mp3"
        audio_file.write_bytes(b"\xff" * 100)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 401
        resp.text = "Unauthorized"
        with patch("hypogeum.elevenlabs.requests.post", return_value=resp):
            with pytest.raises(ElevenLabsError, match="401"):
                whisper_fallback(
                    beat_id="b_001",
                    audio_path=audio_file,
                    config=cfg,
                    episode_id="ep_001",
                )

    def test_non_200_raises_elevenlabs_error(self, tmp_path):
        cfg = _make_config()
        audio_file = tmp_path / "b_001.mp3"
        audio_file.write_bytes(b"\xff" * 100)
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 500
        resp.text = "Internal Server Error"
        with patch("hypogeum.elevenlabs.requests.post", return_value=resp):
            with pytest.raises(ElevenLabsError, match="500"):
                whisper_fallback(
                    beat_id="b_001",
                    audio_path=audio_file,
                    config=cfg,
                    episode_id="ep_001",
                )


# ---------------------------------------------------------------------------
# TestAuthFailure
# ---------------------------------------------------------------------------

class TestAuthFailure:
    """HTTP 401 on TTS endpoint raises ElevenLabsAuthFailure."""

    def test_401_raises_auth_failure(self, tmp_path):
        cfg = _make_config()
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 401
        resp.text = "Unauthorized"
        resp.headers = {}
        with patch("hypogeum.elevenlabs.requests.post", return_value=resp):
            with pytest.raises(ElevenLabsAuthFailure):
                generate_narration_segment(
                    beat_id="b_001", text="hello", config=cfg,
                    episode_id="ep_001", output_dir=tmp_path,
                )

    def test_missing_api_key_raises_auth_failure(self, tmp_path):
        cfg = _make_config(api_key="")
        with pytest.raises(ElevenLabsAuthFailure):
            generate_narration_segment(
                beat_id="b_001", text="hello", config=cfg,
                episode_id="ep_001", output_dir=tmp_path,
            )


# ---------------------------------------------------------------------------
# TestRateLimitRetry
# ---------------------------------------------------------------------------

class TestRateLimitRetry:
    """429 is retried once; second 429 raises ElevenLabsRateLimitError."""

    def test_429_is_retried_once(self, tmp_path):
        cfg = _make_config()
        resp_429 = MagicMock(spec=requests.Response)
        resp_429.status_code = 429
        resp_429.headers = {}
        resp_429.text = "rate limit"
        resp_ok = _mock_tts_response(_minimal_alignment_payload())
        call_count = {"n": 0}
        def side_effect(*a, **kw):
            call_count["n"] += 1
            return resp_429 if call_count["n"] == 1 else resp_ok
        with patch("hypogeum.elevenlabs.requests.post", side_effect=side_effect):
            with patch("hypogeum.elevenlabs.time.sleep"):
                result = generate_narration_segment(
                    beat_id="b_001", text="hello world", config=cfg,
                    episode_id="ep_001", output_dir=tmp_path,
                )
        assert call_count["n"] == 2
        assert isinstance(result, NarrationSegmentResult)

    def test_retry_after_header_respected(self, tmp_path):
        cfg = _make_config()
        resp_429 = MagicMock(spec=requests.Response)
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "42"}
        resp_429.text = "rate limit"
        resp_ok = _mock_tts_response(_minimal_alignment_payload())
        responses = [resp_429, resp_ok]
        sleep_calls = []
        with patch("hypogeum.elevenlabs.requests.post", side_effect=iter(responses)):
            with patch("hypogeum.elevenlabs.time.sleep", side_effect=sleep_calls.append):
                generate_narration_segment(
                    beat_id="b_001", text="hello", config=cfg,
                    episode_id="ep_001", output_dir=tmp_path,
                )
        assert sleep_calls, "time.sleep should have been called"
        assert sleep_calls[0] == pytest.approx(42.0)

    def test_second_429_raises_rate_limit_error(self, tmp_path):
        cfg = _make_config()
        resp_429 = MagicMock(spec=requests.Response)
        resp_429.status_code = 429
        resp_429.headers = {}
        resp_429.text = "rate limit"
        with patch("hypogeum.elevenlabs.requests.post", return_value=resp_429):
            with patch("hypogeum.elevenlabs.time.sleep"):
                with pytest.raises(ElevenLabsRateLimitError):
                    generate_narration_segment(
                        beat_id="b_001", text="hello", config=cfg,
                        episode_id="ep_001", output_dir=tmp_path,
                    )


# ---------------------------------------------------------------------------
# TestQuotaExhaustion
# ---------------------------------------------------------------------------

class TestQuotaExhaustion:
    """Quota keywords in body → ElevenLabsQuotaExhausted; 429 alone does not."""

    def test_quota_keyword_in_body_raises_quota_exhausted(self, tmp_path):
        cfg = _make_config()
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 422
        resp.text = "Your account has insufficient credits to complete this request."
        resp.headers = {}
        with patch("hypogeum.elevenlabs.requests.post", return_value=resp):
            with pytest.raises(ElevenLabsQuotaExhausted):
                generate_narration_segment(
                    beat_id="b_001", text="hello", config=cfg,
                    episode_id="ep_001", output_dir=tmp_path,
                )

    def test_429_alone_does_not_raise_quota_exhausted(self, tmp_path):
        cfg = _make_config()
        resp_429 = MagicMock(spec=requests.Response)
        resp_429.status_code = 429
        resp_429.headers = {}
        resp_429.text = "rate limit exceeded"
        # Both attempts 429 → should raise RateLimitError, not QuotaExhausted
        with patch("hypogeum.elevenlabs.requests.post", return_value=resp_429):
            with patch("hypogeum.elevenlabs.time.sleep"):
                with pytest.raises(ElevenLabsRateLimitError):
                    generate_narration_segment(
                        beat_id="b_001", text="hello", config=cfg,
                        episode_id="ep_001", output_dir=tmp_path,
                    )

    def test_quota_keyword_credits_raises(self, tmp_path):
        cfg = _make_config()
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 400
        resp.text = "You have run out of credits."
        resp.headers = {}
        with patch("hypogeum.elevenlabs.requests.post", return_value=resp):
            with pytest.raises(ElevenLabsQuotaExhausted):
                generate_narration_segment(
                    beat_id="b_001", text="hello", config=cfg,
                    episode_id="ep_001", output_dir=tmp_path,
                )

    def test_quota_keyword_limit_exceeded_raises(self, tmp_path):
        cfg = _make_config()
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 400
        resp.text = "Character limit exceeded for this month."
        resp.headers = {}
        with patch("hypogeum.elevenlabs.requests.post", return_value=resp):
            with pytest.raises(ElevenLabsQuotaExhausted):
                generate_narration_segment(
                    beat_id="b_001", text="hello", config=cfg,
                    episode_id="ep_001", output_dir=tmp_path,
                )


# ---------------------------------------------------------------------------
# TestBuildSrt
# ---------------------------------------------------------------------------

class TestBuildSrt:
    """Tests for build_srt()."""

    def test_empty_segments_returns_empty_string(self):
        assert build_srt([]) == ""

    def test_segments_with_no_word_timings_returns_empty(self):
        seg = _make_segment(word_timings=[])
        assert build_srt([seg]) == ""

    def test_basic_srt_contains_index(self):
        seg = _make_segment(word_timings=_make_word_timings(["hello", "world"]))
        srt = build_srt([seg])
        assert "1\n" in srt

    def test_basic_srt_contains_arrow(self):
        seg = _make_segment(word_timings=_make_word_timings(["hello", "world"]))
        srt = build_srt([seg])
        assert "-->" in srt

    def test_basic_srt_contains_text(self):
        seg = _make_segment(word_timings=_make_word_timings(["hello", "world"]))
        srt = build_srt([seg])
        assert "hello" in srt
        assert "world" in srt

    def test_sequential_offsets_from_durations(self):
        seg1 = _make_segment("b_001", _make_word_timings(["one"]), duration=2.0)
        seg2 = _make_segment("b_002", _make_word_timings(["two"]), duration=3.0)
        srt = build_srt([seg1, seg2])
        # seg2 offset should be 2.0 (seg1.duration) — word "two" starts at 0.0 + 2.0 = 2.0
        lines = srt.strip().split("\n")
        # Find the timestamp line for "two" block
        found_offset = False
        for line in lines:
            if "-->" in line:
                start_str = line.split("-->")[0].strip()
                # start should be >= 2000ms (offset=2.0s)
                h, rest = start_str.split(":", 1)
                m, rest2 = rest.split(":", 1)
                s, ms = rest2.split(",")
                total_ms = (int(h) * 3600 + int(m) * 60 + int(s)) * 1000 + int(ms)
                if total_ms >= 2000:
                    found_offset = True
        assert found_offset

    def test_explicit_offsets_override_sequential(self):
        seg = _make_segment(word_timings=_make_word_timings(["hello"]))
        srt = build_srt([seg], time_offsets_seconds=[10.0])
        # word starts at 0.0 + offset 10.0 = 10.0s
        assert "00:00:10" in srt

    def test_srt_timestamp_format_hh_mm_ss_mmm(self):
        seg = _make_segment(word_timings=[WordTiming("hello", 3661.5, 3662.0)])
        srt = build_srt([seg])
        # 3661.5s = 1h 1m 1s 500ms → 01:01:01,500
        assert "01:01:01,500" in srt

    def test_multiple_blocks_have_sequential_index(self):
        # 8 words → 2 blocks (max 7 per block)
        words = _make_word_timings(["a", "b", "c", "d", "e", "f", "g", "h"])
        seg = _make_segment(word_timings=words)
        srt = build_srt([seg])
        assert "1\n" in srt
        assert "2\n" in srt

    def test_blank_line_between_blocks(self):
        words = _make_word_timings(["a", "b", "c", "d", "e", "f", "g", "h"])
        seg = _make_segment(word_timings=words)
        srt = build_srt([seg])
        # SRT blocks are separated by blank lines
        assert "\n\n" in srt


# ---------------------------------------------------------------------------
# TestCharsToWords
# ---------------------------------------------------------------------------

class TestCharsToWords:
    """Tests for _chars_to_words()."""

    def _make_alignment(self, text: str) -> CharAlignment:
        chars = list(text)
        starts = [i * 0.1 for i in range(len(chars))]
        ends = [i * 0.1 + 0.1 for i in range(len(chars))]
        return CharAlignment(
            characters=chars,
            character_start_times_seconds=starts,
            character_end_times_seconds=ends,
        )

    def test_basic_two_word_grouping(self):
        result = _chars_to_words(self._make_alignment("hi there"))
        words = [w.word for w in result]
        assert words == ["hi", "there"]

    def test_space_not_included_in_word(self):
        result = _chars_to_words(self._make_alignment("a b"))
        for wt in result:
            assert " " not in wt.word

    def test_word_count(self):
        result = _chars_to_words(self._make_alignment("one two three"))
        assert len(result) == 3

    def test_start_and_end_times_correct(self):
        result = _chars_to_words(self._make_alignment("ab cd"))
        # "ab": chars 0,1 → start=0.0, end=0.2
        assert result[0].word == "ab"
        assert result[0].start_seconds == pytest.approx(0.0)
        assert result[0].end_seconds == pytest.approx(0.2)

    def test_empty_alignment_returns_empty(self):
        alignment = CharAlignment(
            characters=[],
            character_start_times_seconds=[],
            character_end_times_seconds=[],
        )
        result = _chars_to_words(alignment)
        assert result == []

    def test_single_word(self):
        result = _chars_to_words(self._make_alignment("word"))
        assert len(result) == 1
        assert result[0].word == "word"

    def test_trailing_space_handled(self):
        result = _chars_to_words(self._make_alignment("hi "))
        assert len(result) == 1
        assert result[0].word == "hi"


# ---------------------------------------------------------------------------
# TestFetchModelCostMultiplier
# ---------------------------------------------------------------------------

class TestFetchModelCostMultiplier:
    """Tests for fetch_model_cost_multiplier()."""

    def setup_method(self):
        # Clear module-level cache before each test
        _model_cost_cache.clear()

    def _mock_models_response(self, model_id: str, multiplier: float) -> MagicMock:
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.json.return_value = [
            {
                "model_id": model_id,
                "model_rates": {"character_cost_multiplier": multiplier},
            }
        ]
        return resp

    def test_returns_multiplier_from_api(self):
        cfg = _make_config()
        with patch("hypogeum.elevenlabs.requests.get",
                   return_value=self._mock_models_response("eleven_multilingual_v2", 0.5)):
            result = fetch_model_cost_multiplier("eleven_multilingual_v2", cfg, "ep_001")
        assert result == pytest.approx(0.5)

    def test_returns_1_on_api_failure(self):
        cfg = _make_config()
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 500
        resp.text = "error"
        with patch("hypogeum.elevenlabs.requests.get", return_value=resp):
            result = fetch_model_cost_multiplier("eleven_multilingual_v2", cfg, "ep_001")
        assert result == pytest.approx(1.0)

    def test_returns_1_on_exception(self):
        cfg = _make_config()
        with patch("hypogeum.elevenlabs.requests.get", side_effect=Exception("network error")):
            result = fetch_model_cost_multiplier("eleven_multilingual_v2", cfg, "ep_001")
        assert result == pytest.approx(1.0)

    def test_result_is_cached(self):
        cfg = _make_config()
        with patch("hypogeum.elevenlabs.requests.get",
                   return_value=self._mock_models_response("eleven_multilingual_v2", 0.5)) as mock_get:
            fetch_model_cost_multiplier("eleven_multilingual_v2", cfg, "ep_001")
            fetch_model_cost_multiplier("eleven_multilingual_v2", cfg, "ep_001")
        # Should only call the API once
        assert mock_get.call_count == 1

    def test_model_not_found_returns_1(self):
        cfg = _make_config()
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.json.return_value = [
            {"model_id": "some_other_model", "model_rates": {"character_cost_multiplier": 0.3}}
        ]
        with patch("hypogeum.elevenlabs.requests.get", return_value=resp):
            result = fetch_model_cost_multiplier("eleven_multilingual_v2", cfg, "ep_001")
        assert result == pytest.approx(1.0)

    def test_returns_1_when_api_key_missing(self):
        cfg = _make_config(api_key="")
        with patch("hypogeum.elevenlabs.requests.get") as mock_get:
            result = fetch_model_cost_multiplier("eleven_multilingual_v2", cfg, "ep_001")
        mock_get.assert_not_called()
        assert result == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# TestInternalHelpers
# ---------------------------------------------------------------------------

class TestInternalHelpers:
    """Tests for internal helper functions."""

    # _parse_char_alignment
    def test_parse_char_alignment_valid(self):
        raw = {
            "characters": ["h", "i"],
            "character_start_times_seconds": [0.0, 0.1],
            "character_end_times_seconds": [0.1, 0.2],
        }
        result = _parse_char_alignment(raw)
        assert result is not None
        assert result.characters == ["h", "i"]

    def test_parse_char_alignment_none_input(self):
        assert _parse_char_alignment(None) is None

    def test_parse_char_alignment_empty_dict(self):
        assert _parse_char_alignment({}) is None

    def test_parse_char_alignment_mismatched_lengths(self):
        raw = {
            "characters": ["h", "i"],
            "character_start_times_seconds": [0.0],
            "character_end_times_seconds": [0.1, 0.2],
        }
        assert _parse_char_alignment(raw) is None

    def test_parse_char_alignment_empty_characters(self):
        raw = {
            "characters": [],
            "character_start_times_seconds": [],
            "character_end_times_seconds": [],
        }
        assert _parse_char_alignment(raw) is None

    # _format_srt_timestamp
    def test_format_srt_zero(self):
        assert _format_srt_timestamp(0.0) == "00:00:00,000"

    def test_format_srt_one_second(self):
        assert _format_srt_timestamp(1.0) == "00:00:01,000"

    def test_format_srt_one_minute(self):
        assert _format_srt_timestamp(60.0) == "00:01:00,000"

    def test_format_srt_with_milliseconds(self):
        assert _format_srt_timestamp(1.5) == "00:00:01,500"

    def test_format_srt_one_hour(self):
        assert _format_srt_timestamp(3600.0) == "01:00:00,000"

    # _output_format_extension
    def test_mp3_format_extension(self):
        assert _output_format_extension("mp3_44100_128") == ".mp3"

    def test_wav_format_extension(self):
        assert _output_format_extension("wav_44100_128") == ".wav"

    def test_unknown_format_defaults_to_mp3(self):
        assert _output_format_extension("unknown_format") == ".mp3"

    # _retry_delay
    def test_retry_after_header_clamps_to_max(self):
        delay = _retry_delay(0, retry_after=9999.0)
        from hypogeum.elevenlabs import _RETRY_MAX_DELAY
        assert delay <= _RETRY_MAX_DELAY

    def test_retry_delay_without_retry_after(self):
        delay = _retry_delay(0, retry_after=None)
        assert delay >= 0

    # _detect_quota_exhaustion
    def test_detects_quota_keyword(self):
        resp = MagicMock(spec=requests.Response)
        resp.text = "Your quota has been exceeded."
        assert _detect_quota_exhaustion(resp) is True

    def test_detects_credits_keyword(self):
        resp = MagicMock(spec=requests.Response)
        resp.text = "Insufficient credits to process request."
        assert _detect_quota_exhaustion(resp) is True

    def test_does_not_falsely_detect(self):
        resp = MagicMock(spec=requests.Response)
        resp.text = "Internal server error."
        assert _detect_quota_exhaustion(resp) is False

    def test_handles_exception_in_text_access(self):
        resp = MagicMock(spec=requests.Response)
        type(resp).text = property(lambda self: (_ for _ in ()).throw(Exception("broken")))
        assert _detect_quota_exhaustion(resp) is False


# ---------------------------------------------------------------------------
# TestExceptionHierarchy
# ---------------------------------------------------------------------------

class TestExceptionHierarchy:
    def test_elevenlabs_auth_failure_is_elevenlabs_error(self):
        assert issubclass(ElevenLabsAuthFailure, ElevenLabsError)

    def test_elevenlabs_quota_exhausted_is_elevenlabs_error(self):
        assert issubclass(ElevenLabsQuotaExhausted, ElevenLabsError)

    def test_elevenlabs_rate_limit_is_elevenlabs_error(self):
        assert issubclass(ElevenLabsRateLimitError, ElevenLabsError)

    def test_elevenlabs_error_is_runtime_error(self):
        assert issubclass(ElevenLabsError, RuntimeError)

    def test_auth_failure_no_arg_init(self):
        exc = ElevenLabsAuthFailure()
        assert "401" in str(exc)

    def test_quota_exhausted_no_arg_init(self):
        exc = ElevenLabsQuotaExhausted()
        assert "quota" in str(exc).lower() or "exhausted" in str(exc).lower()

    def test_quota_exhausted_with_detail(self):
        exc = ElevenLabsQuotaExhausted("ran out at beat 5")
        assert "ran out at beat 5" in str(exc)

    def test_rate_limit_no_arg_init(self):
        exc = ElevenLabsRateLimitError()
        assert "429" in str(exc)
