"""
tests/test_vertical_master.py — the single vertical-delivery contract
(2026-09-04). One MP4 uploaded verbatim to TikTok / IG Reels / FB Reels /
YouTube Shorts.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from hypogeum import vertical_master as vm


def _ffmpeg():
    return shutil.which("ffmpeg") or "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"


class TestSpecShape:
    def test_geometry(self):
        assert (vm.WIDTH, vm.HEIGHT) == (1080, 1920)
        assert vm.ASPECT_RATIO == "9:16"
        assert vm.FRAME_RATE == 30

    def test_duration_under_a_minute(self):
        assert vm.MAX_DURATION_SECONDS <= 59.0

    def test_audio_is_48k_stereo_in_band(self):
        assert vm.AUDIO_SAMPLE_RATE == 48000
        assert vm.AUDIO_CHANNELS == 2
        assert vm.AUDIO_BITRATE.endswith("k")
        assert 192 <= int(vm.AUDIO_BITRATE[:-1]) <= 320

    def test_safe_box_matches_chatgpt_research(self):
        # X 80..900, Y 250..1420
        assert vm.SAFE_BOX_PX == {"x_min": 80, "x_max": 900, "y_min": 250, "y_max": 1420}

    def test_ffmpeg_args_carry_rec709_and_high_profile(self):
        v = " ".join(vm.ffmpeg_video_args())
        assert "-profile:v high" in v
        assert "bt709" in v
        a = " ".join(vm.ffmpeg_audio_args())
        assert "-ar 48000" in a and "-ac 2" in a


class TestConformanceFindings:
    def _base(self):
        return {
            "width": 1080, "height": 1920, "codec_name": "h264", "profile": "High",
            "pix_fmt": "yuv420p", "r_frame_rate": "30/1",
            "sample_rate": "48000", "channels": 2, "duration_s": "50.0",
            "color_primaries": "bt709", "color_transfer": "bt709", "color_space": "bt709",
        }

    def test_clean_probe_has_no_findings(self):
        assert vm.conformance_findings(self._base()) == []

    def test_flags_96k_audio(self):
        p = self._base(); p["sample_rate"] = "96000"
        assert any("sample rate" in f for f in vm.conformance_findings(p))

    def test_flags_untagged_colour(self):
        p = self._base(); p["color_primaries"] = "unknown"
        assert any("Rec.709" in f for f in vm.conformance_findings(p))

    def test_flags_wrong_resolution(self):
        p = self._base(); p["width"] = 720
        assert any("resolution" in f for f in vm.conformance_findings(p))

    def test_flags_overlong(self):
        p = self._base(); p["duration_s"] = "75"
        assert any("duration" in f for f in vm.conformance_findings(p))

    def test_flags_non_high_profile(self):
        p = self._base(); p["profile"] = "Main"
        assert any("profile" in f for f in vm.conformance_findings(p))


@pytest.mark.skipif(not (shutil.which("ffmpeg") or Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg").exists()),
                    reason="ffmpeg not available")
class TestConformPassEndToEnd:
    def test_conforms_a_96k_untagged_file(self, tmp_path):
        from hypogeum.agents.video_renderer import conform_to_vertical_master, probe_media
        src = tmp_path / "in.mp4"
        # 2s 1080x1920 30fps clip with 96kHz audio, no colour tags.
        subprocess.run([
            _ffmpeg(), "-y",
            "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=96000:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "96000",
            "-shortest", str(src),
        ], check=True, capture_output=True)
        assert any("sample rate" in f for f in vm.conformance_findings(probe_media(src, _ffmpeg())))

        ok, remaining = conform_to_vertical_master(src, ffmpeg_binary=_ffmpeg(), episode_id="t")
        assert ok is True and remaining == []
        after = probe_media(src, _ffmpeg())
        assert after["sample_rate"] in ("48000", 48000)
        assert (after["color_primaries"] or "").lower() == "bt709"
