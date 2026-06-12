"""
tests/test_media_specs_qa.py — Unit tests for Media Specs QA Agent (Agent 36)

Tests the core decision logic by mocking ffprobe output. Real ffprobe is
called only for the file-not-found and zero-byte paths, which are pure
filesystem checks that don't require a valid media file.

Coverage:
  - Wrong resolution (1280×720) → publish_ready=False, resolution failure listed
  - Correct resolution (1920×1080) → publish_ready=True
  - Shorts portrait (1080×1920) → publish_ready=True with "short" file_type
  - Wrong Shorts resolution → publish_ready=False
  - Missing file → file_valid=False
  - Zero-byte file → file_valid=False
  - ffprobe crash → file_valid=False
  - Audio file inspection (sample rate match / mismatch)
  - Image file inspection (min dimension check)
  - FPS tolerance: 29.97 passes as 30fps
  - FPS out of tolerance: 24fps fails against 30fps expected
  - Schema validation: output always passes validate("media_specs_report", ...)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.agents.media_specs_qa import (
    _check_specs,
    _extract_specs,
    run_media_specs_qa,
)
from hypogeum.schemas import SchemaError, validate


# ---------------------------------------------------------------------------
# Helpers — build fake ffprobe JSON payloads
# ---------------------------------------------------------------------------

def _make_video_probe(width: int, height: int, fps_str: str = "30/1",
                      video_codec: str = "h264", audio_codec: str = "aac",
                      sample_rate: int = 44100, duration: float = 30.0,
                      file_size: int = 5_000_000) -> dict:
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": video_codec,
                "width": width,
                "height": height,
                "r_frame_rate": fps_str,
                "duration": str(duration),
            },
            {
                "codec_type": "audio",
                "codec_name": audio_codec,
                "sample_rate": str(sample_rate),
                "channels": 2,
                "duration": str(duration),
            },
        ],
        "format": {
            "duration": str(duration),
            "size": str(file_size),
        },
    }


def _make_audio_probe(sample_rate: int = 44100, channels: int = 2,
                      duration: float = 120.0, codec: str = "aac") -> dict:
    return {
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": codec,
                "sample_rate": str(sample_rate),
                "channels": channels,
                "duration": str(duration),
            },
        ],
        "format": {
            "duration": str(duration),
            "size": "2000000",
        },
    }


def _make_image_probe(width: int, height: int) -> dict:
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "png",
                "width": width,
                "height": height,
            },
        ],
        "format": {
            "size": "500000",
        },
    }


def _mock_ffprobe(probe_dict: dict):
    """Return a context manager that patches _run_ffprobe to return probe_dict."""
    return patch(
        "hypogeum.agents.media_specs_qa._run_ffprobe",
        return_value=probe_dict,
    )


# ---------------------------------------------------------------------------
# Resolution tests — main video (1920×1080)
# ---------------------------------------------------------------------------

class TestVideoResolution:
    def test_wrong_resolution_1280x720_fails(self, tmp_path):
        """1280×720 clip must set publish_ready=False and list a resolution failure."""
        fake_file = tmp_path / "sc_01.mp4"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_video_probe(width=1280, height=720)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="video",
                episode_id="ep_test",
            )

        assert report["publish_ready"] is False
        assert report["file_valid"] is True
        assert any("resolution" in f for f in report["failures"]), \
            f"Expected resolution failure in {report['failures']}"

    def test_correct_resolution_1920x1080_passes(self, tmp_path):
        """1920×1080 clip must set publish_ready=True with no failures."""
        fake_file = tmp_path / "sc_02.mp4"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_video_probe(width=1920, height=1080)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="video",
                episode_id="ep_test",
            )

        assert report["publish_ready"] is True
        assert report["file_valid"] is True
        assert report["failures"] == []

    def test_wrong_width_only_fails(self, tmp_path):
        """Wrong width alone is sufficient to fail."""
        fake_file = tmp_path / "sc_03.mp4"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_video_probe(width=1280, height=1080)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="video",
                episode_id="ep_test",
            )

        assert report["publish_ready"] is False
        assert any("resolution" in f for f in report["failures"])

    def test_wrong_height_only_fails(self, tmp_path):
        """Wrong height alone is sufficient to fail."""
        fake_file = tmp_path / "sc_04.mp4"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_video_probe(width=1920, height=720)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="video",
                episode_id="ep_test",
            )

        assert report["publish_ready"] is False

    def test_custom_expected_spec_overrides_default(self, tmp_path):
        """Caller-supplied expected_spec overrides the built-in default."""
        fake_file = tmp_path / "sc_05.mp4"
        fake_file.write_bytes(b"\x00" * 100)

        # File is actually 1280×720 — but we're testing against a custom spec
        probe = _make_video_probe(width=1280, height=720)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="video",
                episode_id="ep_test",
                expected_spec={"width": 1280, "height": 720},
            )

        assert report["publish_ready"] is True
        assert report["failures"] == []


# ---------------------------------------------------------------------------
# Shorts (9:16 portrait)
# ---------------------------------------------------------------------------

class TestShortsResolution:
    def test_correct_shorts_portrait_passes(self, tmp_path):
        """1080×1920 short clip must pass with file_type='short'."""
        fake_file = tmp_path / "short_01.mp4"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_video_probe(width=1080, height=1920)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="short",
                episode_id="ep_test",
            )

        assert report["publish_ready"] is True
        assert report["failures"] == []

    def test_wrong_shorts_resolution_fails(self, tmp_path):
        """1920×1080 clip submitted as 'short' must fail (wrong orientation)."""
        fake_file = tmp_path / "short_02.mp4"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_video_probe(width=1920, height=1080)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="short",
                episode_id="ep_test",
            )

        assert report["publish_ready"] is False


# ---------------------------------------------------------------------------
# FPS tolerance
# ---------------------------------------------------------------------------

class TestFPS:
    def test_29_97_passes_as_30fps(self, tmp_path):
        """29.97 fps (30000/1001) must be within tolerance of 30fps spec."""
        fake_file = tmp_path / "sc_fps.mp4"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_video_probe(width=1920, height=1080, fps_str="30000/1001")
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="video",
                episode_id="ep_test",
            )

        assert report["publish_ready"] is True, \
            f"29.97fps should pass 30fps spec; failures: {report['failures']}"

    def test_24fps_fails_against_30fps_spec(self, tmp_path):
        """24fps is outside tolerance and must fail."""
        fake_file = tmp_path / "sc_24fps.mp4"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_video_probe(width=1920, height=1080, fps_str="24/1")
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="video",
                episode_id="ep_test",
            )

        assert report["publish_ready"] is False
        assert any("fps" in f for f in report["failures"])


# ---------------------------------------------------------------------------
# File-level failures
# ---------------------------------------------------------------------------

class TestFileValidity:
    def test_missing_file_is_not_valid(self, tmp_path):
        """Non-existent file must set file_valid=False and publish_ready=False."""
        missing = tmp_path / "nonexistent.mp4"

        report = run_media_specs_qa(
            file_path=missing,
            file_type="video",
            episode_id="ep_test",
        )

        assert report["file_valid"] is False
        assert report["publish_ready"] is False
        assert any("not found" in f for f in report["failures"])

    def test_zero_byte_file_is_not_valid(self, tmp_path):
        """Zero-byte file must set file_valid=False."""
        empty = tmp_path / "empty.mp4"
        empty.write_bytes(b"")

        report = run_media_specs_qa(
            file_path=empty,
            file_type="video",
            episode_id="ep_test",
        )

        assert report["file_valid"] is False
        assert report["publish_ready"] is False
        assert any("zero bytes" in f for f in report["failures"])

    def test_ffprobe_crash_is_not_valid(self, tmp_path):
        """ffprobe RuntimeError must set file_valid=False."""
        fake_file = tmp_path / "corrupt.mp4"
        fake_file.write_bytes(b"\xFF" * 50)

        with patch(
            "hypogeum.agents.media_specs_qa._run_ffprobe",
            side_effect=RuntimeError("ffprobe exited 1: Invalid data"),
        ):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="video",
                episode_id="ep_test",
            )

        assert report["file_valid"] is False
        assert report["publish_ready"] is False
        assert any("ffprobe failed" in f for f in report["failures"])


# ---------------------------------------------------------------------------
# Audio inspection
# ---------------------------------------------------------------------------

class TestAudioInspection:
    def test_audio_correct_sample_rate_passes(self, tmp_path):
        """44100 Hz audio passes against default audio spec."""
        fake_file = tmp_path / "narration.mp3"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_audio_probe(sample_rate=44100)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="audio",
                episode_id="ep_test",
            )

        assert report["publish_ready"] is True
        assert report["failures"] == []

    def test_audio_wrong_sample_rate_fails(self, tmp_path):
        """22050 Hz audio fails against 44100 Hz default spec."""
        fake_file = tmp_path / "narration_bad.mp3"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_audio_probe(sample_rate=22050)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="audio",
                episode_id="ep_test",
            )

        assert report["publish_ready"] is False
        assert any("sample_rate" in f for f in report["failures"])

    def test_audio_observed_sample_rate_recorded(self, tmp_path):
        """specs_observed must include audio_sample_rate."""
        fake_file = tmp_path / "music.mp3"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_audio_probe(sample_rate=44100)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="audio",
                episode_id="ep_test",
            )

        assert report["specs_observed"]["audio_sample_rate"] == 44100


# ---------------------------------------------------------------------------
# Image inspection
# ---------------------------------------------------------------------------

class TestImageInspection:
    def test_image_meets_minimum_dimensions_passes(self, tmp_path):
        """1920×1080 thumbnail passes default image spec."""
        fake_file = tmp_path / "thumb.png"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_image_probe(width=1920, height=1080)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="image",
                episode_id="ep_test",
            )

        assert report["publish_ready"] is True
        assert report["failures"] == []

    def test_image_below_minimum_width_fails(self, tmp_path):
        """Image narrower than 1920px fails."""
        fake_file = tmp_path / "thumb_small.png"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_image_probe(width=1280, height=720)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="image",
                episode_id="ep_test",
            )

        assert report["publish_ready"] is False
        assert any("width" in f for f in report["failures"])

    def test_image_larger_than_minimum_passes(self, tmp_path):
        """4K thumbnail (3840×2160) must pass ≥1920×1080 minimum."""
        fake_file = tmp_path / "thumb_4k.png"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_image_probe(width=3840, height=2160)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="image",
                episode_id="ep_test",
            )

        assert report["publish_ready"] is True


# ---------------------------------------------------------------------------
# Render type (same rules as video)
# ---------------------------------------------------------------------------

class TestRenderInspection:
    def test_render_correct_resolution_passes(self, tmp_path):
        fake_file = tmp_path / "main_ep_test.mp4"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_video_probe(width=1920, height=1080)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="render",
                episode_id="ep_test",
            )

        assert report["publish_ready"] is True

    def test_render_wrong_resolution_fails(self, tmp_path):
        fake_file = tmp_path / "main_ep_test_bad.mp4"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_video_probe(width=1280, height=720)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="render",
                episode_id="ep_test",
            )

        assert report["publish_ready"] is False


# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------

class TestSchemaContract:
    def test_output_always_validates_against_schema_pass(self, tmp_path):
        """Every report (pass or fail) must satisfy the media_specs_report schema."""
        fake_file = tmp_path / "sc_schema.mp4"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_video_probe(width=1920, height=1080)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="video",
                episode_id="ep_schema_test",
            )

        # Should not raise
        validate("media_specs_report", report)

    def test_output_always_validates_against_schema_fail(self, tmp_path):
        """Even a failing report must satisfy the media_specs_report schema."""
        fake_file = tmp_path / "sc_schema_fail.mp4"
        fake_file.write_bytes(b"\x00" * 100)

        probe = _make_video_probe(width=1280, height=720)
        with _mock_ffprobe(probe):
            report = run_media_specs_qa(
                file_path=fake_file,
                file_type="video",
                episode_id="ep_schema_test",
            )

        validate("media_specs_report", report)

    def test_output_always_validates_missing_file(self, tmp_path):
        """Missing-file report must satisfy the media_specs_report schema."""
        missing = tmp_path / "gone.mp4"
        report = run_media_specs_qa(
            file_path=missing,
            file_type="video",
            episode_id="ep_schema_test",
        )
        validate("media_specs_report", report)

    def test_schema_registered(self):
        """media_specs_report must be in the schema registry."""
        from hypogeum.schemas import SCHEMA_NAMES
        assert "media_specs_report" in SCHEMA_NAMES

    def test_invalid_file_type_raises_value_error(self, tmp_path):
        """Unknown file_type must raise ValueError before any file I/O."""
        fake_file = tmp_path / "sc.mp4"
        fake_file.write_bytes(b"\x00" * 100)
        with pytest.raises(ValueError, match="Unknown file_type"):
            run_media_specs_qa(
                file_path=fake_file,
                file_type="hologram",
                episode_id="ep_test",
            )


# ---------------------------------------------------------------------------
# _extract_specs unit tests (pure function)
# ---------------------------------------------------------------------------

class TestExtractSpecs:
    def test_aspect_ratio_16_9(self):
        probe = _make_video_probe(width=1920, height=1080)
        result = _extract_specs(probe)
        assert result["aspect_ratio"] == "16:9"

    def test_aspect_ratio_9_16(self):
        probe = _make_video_probe(width=1080, height=1920)
        result = _extract_specs(probe)
        assert result["aspect_ratio"] == "9:16"

    def test_fps_parsed_from_fraction(self):
        probe = _make_video_probe(width=1920, height=1080, fps_str="30000/1001")
        result = _extract_specs(probe)
        assert result["fps"] is not None
        assert abs(result["fps"] - 29.97) < 0.01

    def test_no_audio_stream(self):
        probe = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "r_frame_rate": "30/1",
                    "duration": "10.0",
                }
            ],
            "format": {"duration": "10.0", "size": "1000000"},
        }
        result = _extract_specs(probe)
        assert result["audio_codec"] is None
        assert result["audio_sample_rate"] is None

    def test_no_video_stream(self):
        probe = _make_audio_probe()
        result = _extract_specs(probe)
        assert result["width"] is None
        assert result["height"] is None
        assert result["video_codec"] is None


# ---------------------------------------------------------------------------
# _check_specs unit tests (pure function)
# ---------------------------------------------------------------------------

class TestCheckSpecs:
    def test_exact_match_no_failures(self):
        observed = {
            "width": 1920, "height": 1080, "fps": 30.0,
            "video_codec": "h264", "audio_codec": "aac",
            "audio_sample_rate": 44100, "duration_seconds": 30.0,
        }
        expected = {"width": 1920, "height": 1080, "fps": 30.0,
                    "video_codec": "h264", "audio_codec": "aac",
                    "audio_sample_rate": 44100}
        failures, warnings = _check_specs("video", observed, expected)
        assert failures == []

    def test_wrong_resolution_produces_failure(self):
        observed = {
            "width": 1280, "height": 720, "fps": 30.0,
            "duration_seconds": 10.0,
        }
        expected = {"width": 1920, "height": 1080}
        failures, _ = _check_specs("video", observed, expected)
        assert len(failures) > 0
        assert any("resolution" in f for f in failures)

    def test_fps_tolerance_passes(self):
        observed = {
            "width": 1920, "height": 1080, "fps": 29.97,
            "duration_seconds": 10.0,
        }
        expected = {"width": 1920, "height": 1080, "fps": 30.0}
        failures, _ = _check_specs("video", observed, expected)
        assert not any("fps" in f for f in failures)

    def test_zero_duration_is_failure(self):
        observed = {
            "width": 1920, "height": 1080, "fps": 30.0,
            "duration_seconds": 0.0,
        }
        expected = {"width": 1920, "height": 1080, "fps": 30.0}
        failures, _ = _check_specs("video", observed, expected)
        assert any("duration" in f for f in failures)
