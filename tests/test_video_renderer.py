"""
tests/test_video_renderer.py — Unit tests for hypogeum/agents/video_renderer.py

Tests cover the public API surface and critical internal helpers:
  TestRenderEpisode             — render_episode() end-to-end (manifest shape, statuses)
  TestBuildFfmpegConcatCommand  — _build_ffmpeg_concat_command() (tpad, -t, audio)
  TestRunFfmpeg                 — _run_ffmpeg() (success, failure, timeout)
  TestAudioMixingInternals      — audio mix constants and command structure
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.video_renderer import (
    FRAME_RATE,
    MAIN_RESOLUTION,
    SHORT_RESOLUTION,
    _MUSIC_AMPLITUDE_FACTOR,
    _build_ffmpeg_concat_command,
    _run_ffmpeg,
    render_episode,
)
from hypogeum.agents.timeline_builder import TimelineEntry, TimelinePlan
from hypogeum.elevenlabs import MusicTrackResult


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_entry(
    scene_index=0,
    scene_id="sc_001",
    source_type="main_episode",
    short_id=None,
    duration_seconds=4.5,
    clip_duration_seconds=8.0,
    clip_path=None,
    narration_path=None,
) -> TimelineEntry:
    return TimelineEntry(
        scene_index=scene_index,
        scene_id=scene_id,
        source_type=source_type,
        short_id=short_id,
        clip_path=clip_path,
        narration_path=narration_path,
        start_seconds=0.0,
        duration_seconds=duration_seconds,
        text_overlays=[],
        music_section=None,
        text_safe_zone=None,
        clip_duration_seconds=clip_duration_seconds,
    )


def _make_timeline_plan(
    main_entries=None,
    shorts_entries=None,
) -> TimelinePlan:
    main_timeline = main_entries or [_make_entry()]
    shorts_timelines = shorts_entries or {}
    short_ids = sorted(shorts_timelines.keys())
    return TimelinePlan(
        main_timeline=main_timeline,
        shorts_timelines=shorts_timelines,
        short_ids=short_ids,
    )


def _make_music_track(tmp_path: Path) -> MusicTrackResult:
    music_path = tmp_path / "music.mp3"
    music_path.write_bytes(b"\xff\xfb" * 100)
    return MusicTrackResult(
        track_id="tr1",
        category="Mystery",
        asset_id="channel/music/mystery/tr1",
        local_path=music_path,
        source="ai_generated",
        duration_seconds=120.0,
    )


def _mock_ffmpeg_success():
    def _side_effect(cmd, capture_output, text, timeout):
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"FAKEMP4" * 1000)
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        result.stdout = ""
        return result
    return _side_effect


def _mock_log():
    log = MagicMock()
    log.info = MagicMock()
    log.error = MagicMock()
    log.warn = MagicMock()
    return log


# ---------------------------------------------------------------------------
# TestRenderEpisode
# ---------------------------------------------------------------------------

class TestRenderEpisode:

    def _minimal_render(self, tmp_path, ffmpeg_side_effect=None):
        """Run render_episode() with mocked ffmpeg and return the manifest."""
        plan = _make_timeline_plan()
        episode_dir = tmp_path / "ep_001"
        episode_dir.mkdir()
        log = _mock_log()

        side_effect = ffmpeg_side_effect or _mock_ffmpeg_success()
        with patch("subprocess.run", side_effect=side_effect):
            return render_episode(
                timeline_plan=plan,
                main_narration_path=None,
                short_narration_paths={},
                music_tracks={},
                clip_availability_rate=1.0,
                missing_clips=[],
                sections_without_music=[],
                episode_dir=episode_dir,
                episode_id="ep_001",
                ffmpeg_timeout=60,
                log=log,
            )

    def test_returns_dict(self, tmp_path):
        """render_episode() returns a dict."""
        result = self._minimal_render(tmp_path)
        assert isinstance(result, dict)

    def test_manifest_has_render_status(self, tmp_path):
        """Manifest includes render_status field."""
        result = self._minimal_render(tmp_path)
        assert "render_status" in result

    def test_manifest_has_main_render(self, tmp_path):
        """Manifest includes main_render dict with file_exists."""
        result = self._minimal_render(tmp_path)
        assert "main_render" in result
        assert "file_exists" in result["main_render"]

    def test_main_render_file_exists_on_success(self, tmp_path):
        """When ffmpeg succeeds and creates the file, file_exists is True."""
        result = self._minimal_render(tmp_path)
        assert result["main_render"]["file_exists"] is True

    def test_render_status_failed_when_ffmpeg_fails(self, tmp_path):
        """render_status is 'failed' when main episode ffmpeg exits non-zero."""
        def _fail(cmd, capture_output, text, timeout):
            r = MagicMock()
            r.returncode = 1
            r.stderr = "error"
            r.stdout = ""
            return r

        result = self._minimal_render(tmp_path, ffmpeg_side_effect=_fail)
        assert result["render_status"] == "failed"

    def test_render_status_partial_when_clips_below_100pct(self, tmp_path):
        """When clip_availability_rate < 1.0 and renders succeed, status is partial."""
        plan = _make_timeline_plan()
        episode_dir = tmp_path / "ep_001"
        episode_dir.mkdir()
        log = _mock_log()

        with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
            result = render_episode(
                timeline_plan=plan,
                main_narration_path=None,
                short_narration_paths={},
                music_tracks={},
                clip_availability_rate=0.9,
                missing_clips=["sc_002"],
                sections_without_music=[],
                episode_dir=episode_dir,
                episode_id="ep_001",
                ffmpeg_timeout=60,
                log=log,
            )

        assert result["render_status"] == "partial"

    def test_shorts_renders_in_manifest(self, tmp_path):
        """Manifest includes shorts_renders list."""
        plan = _make_timeline_plan()
        episode_dir = tmp_path / "ep_001"
        episode_dir.mkdir()
        log = _mock_log()

        with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
            result = render_episode(
                timeline_plan=plan,
                main_narration_path=None,
                short_narration_paths={},
                music_tracks={},
                clip_availability_rate=1.0,
                missing_clips=[],
                sections_without_music=[],
                episode_dir=episode_dir,
                episode_id="ep_001",
                ffmpeg_timeout=60,
                log=log,
            )

        assert "shorts_renders" in result
        assert isinstance(result["shorts_renders"], list)

    def test_missing_clips_propagated_to_manifest(self, tmp_path):
        """missing_clips passed in appear in the manifest."""
        plan = _make_timeline_plan()
        episode_dir = tmp_path / "ep_001"
        episode_dir.mkdir()
        log = _mock_log()

        with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
            result = render_episode(
                timeline_plan=plan,
                main_narration_path=None,
                short_narration_paths={},
                music_tracks={},
                clip_availability_rate=0.8,
                missing_clips=["sc_99"],
                sections_without_music=[],
                episode_dir=episode_dir,
                episode_id="ep_001",
                ffmpeg_timeout=60,
                log=log,
            )

        assert "sc_99" in result["missing_clips"]

    def test_sections_without_music_propagated_to_manifest(self, tmp_path):
        """sections_without_music passed in appear in the manifest."""
        plan = _make_timeline_plan()
        episode_dir = tmp_path / "ep_001"
        episode_dir.mkdir()
        log = _mock_log()

        with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
            result = render_episode(
                timeline_plan=plan,
                main_narration_path=None,
                short_narration_paths={},
                music_tracks={},
                clip_availability_rate=1.0,
                missing_clips=[],
                sections_without_music=["main_episode"],
                episode_dir=episode_dir,
                episode_id="ep_001",
                ffmpeg_timeout=60,
                log=log,
            )

        assert "main_episode" in result["sections_without_music"]

    def test_manifest_includes_resolution_and_frame_rate(self, tmp_path):
        """Manifest includes main_resolution, short_resolution, frame_rate fields."""
        result = self._minimal_render(tmp_path)
        assert result["main_resolution"] == MAIN_RESOLUTION
        assert result["short_resolution"] == SHORT_RESOLUTION
        assert result["frame_rate"] == FRAME_RATE

    def test_manifest_includes_duration_coverage_fields(self, tmp_path):
        """Manifest includes total_narration_seconds, covered_narration_seconds, gap."""
        result = self._minimal_render(tmp_path)
        assert "total_narration_seconds" in result
        assert "covered_narration_seconds" in result
        assert "duration_coverage_gap_seconds" in result


# ---------------------------------------------------------------------------
# TestBuildFfmpegConcatCommand
# ---------------------------------------------------------------------------

class TestBuildFfmpegConcatCommand:

    def test_tpad_applied_when_narration_longer_than_clip(self, tmp_path):
        """
        Given entry.duration_seconds=20s and clip_duration_seconds=8s,
        the ffmpeg filter_complex includes tpad=stop_mode=clone:stop_duration=12.000.
        """
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")

        entry = _make_entry(duration_seconds=20.0, clip_duration_seconds=8.0, clip_path=clip_path)

        cmd = _build_ffmpeg_concat_command(
            timeline=[entry],
            narration_path=None,
            music_track=None,
            output_path=tmp_path / "out.mp4",
            resolution="1920x1080",
            episode_id="ep_test",
        )

        cmd_str = " ".join(str(c) for c in cmd)
        assert "tpad=stop_mode=clone:stop_duration=12.000" in cmd_str

    def test_each_entry_gets_own_hold_before_concat(self, tmp_path):
        """
        Multiple entries each get their own tpad hold before the concat filter.
        The pv0/pv1 output labels are used as concat inputs.
        """
        clip1 = tmp_path / "clip1.mp4"
        clip2 = tmp_path / "clip2.mp4"
        clip1.write_bytes(b"FAKEVIDEO")
        clip2.write_bytes(b"FAKEVIDEO")

        entries = [
            _make_entry(scene_index=0, duration_seconds=21.0, clip_duration_seconds=8.0, clip_path=clip1),
            _make_entry(scene_index=1, scene_id="sc_002", duration_seconds=23.0, clip_duration_seconds=8.0, clip_path=clip2),
        ]

        cmd = _build_ffmpeg_concat_command(
            timeline=entries,
            narration_path=None,
            music_track=None,
            output_path=tmp_path / "out.mp4",
            resolution="1920x1080",
            episode_id="ep_test",
        )

        cmd_str = " ".join(str(c) for c in cmd)
        assert "stop_duration=13.000" in cmd_str
        assert "stop_duration=15.000" in cmd_str
        assert "[pv0]" in cmd_str
        assert "[pv1]" in cmd_str

    def test_t_flag_equals_timeline_duration_not_clip_duration(self, tmp_path):
        """
        The ffmpeg -t flag equals total timeline (narration) duration, not clip duration.
        For a 25s beat with an 8s clip, -t must be 25.0, not 8.0.
        """
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")

        entry = _make_entry(duration_seconds=25.0, clip_duration_seconds=8.0, clip_path=clip_path)

        cmd = _build_ffmpeg_concat_command(
            timeline=[entry],
            narration_path=None,
            music_track=None,
            output_path=tmp_path / "out.mp4",
            resolution="1920x1080",
            episode_id="ep_test",
        )

        assert "-t" in cmd
        t_idx = cmd.index("-t")
        assert float(cmd[t_idx + 1]) == pytest.approx(25.0)

    def test_no_tpad_when_clip_covers_full_beat(self, tmp_path):
        """
        When clip_duration_seconds >= duration_seconds (beat fully covered),
        no tpad filter is added and no [pv0] label appears.
        """
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")

        entry = _make_entry(duration_seconds=8.0, clip_duration_seconds=8.0, clip_path=clip_path)

        cmd = _build_ffmpeg_concat_command(
            timeline=[entry],
            narration_path=None,
            music_track=None,
            output_path=tmp_path / "out.mp4",
            resolution="1920x1080",
            episode_id="ep_test",
        )

        fc_value = ""
        if "-filter_complex" in cmd:
            fc_idx = cmd.index("-filter_complex")
            fc_value = cmd[fc_idx + 1]
        assert "tpad" not in fc_value
        assert "[pv0]" not in fc_value

    def test_narration_audio_passed_at_full_duration(self, tmp_path):
        """
        The narration audio file is included in ffmpeg inputs and the -t flag
        matches the narration beat duration (not the clip duration).
        """
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")
        narration_path = tmp_path / "narration.mp3"
        narration_path.write_bytes(b"\xff\xfb" * 100)

        entry = _make_entry(
            duration_seconds=22.0,
            clip_duration_seconds=8.0,
            clip_path=clip_path,
            narration_path=narration_path,
        )

        cmd = _build_ffmpeg_concat_command(
            timeline=[entry],
            narration_path=narration_path,
            music_track=None,
            output_path=tmp_path / "out.mp4",
            resolution="1920x1080",
            episode_id="ep_test",
        )

        cmd_str = " ".join(str(c) for c in cmd)
        assert str(narration_path) in cmd_str
        t_idx = cmd.index("-t")
        assert float(cmd[t_idx + 1]) == pytest.approx(22.0)


# ---------------------------------------------------------------------------
# TestRunFfmpeg
# ---------------------------------------------------------------------------

class TestRunFfmpeg:

    def test_returns_false_on_nonzero_exit(self, tmp_path):
        """_run_ffmpeg returns False when subprocess exits non-zero."""
        log = _mock_log()
        output = tmp_path / "out.mp4"

        def _fail(cmd, capture_output, text, timeout):
            r = MagicMock()
            r.returncode = 1
            r.stderr = "error message"
            r.stdout = ""
            return r

        with patch("subprocess.run", side_effect=_fail):
            success = _run_ffmpeg(
                cmd=["ffmpeg", "-y", str(output)],
                output_path=output,
                component="test",
                episode_id="ep_001",
                timeout=60,
                log=log,
            )

        assert success is False
        log.error.assert_called()

    def test_returns_true_on_success(self, tmp_path):
        """_run_ffmpeg returns True when subprocess succeeds."""
        log = _mock_log()
        output = tmp_path / "out.mp4"
        output.write_bytes(b"FAKEMP4")

        with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
            success = _run_ffmpeg(
                cmd=["ffmpeg", "-y", str(output)],
                output_path=output,
                component="test",
                episode_id="ep_001",
                timeout=60,
                log=log,
            )

        assert success is True

    def test_timeout_handled_as_failure(self, tmp_path):
        """TimeoutExpired is caught and treated as a render failure."""
        log = _mock_log()
        output = tmp_path / "out.mp4"

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=5)):
            success = _run_ffmpeg(
                cmd=["ffmpeg", "-y", str(output)],
                output_path=output,
                component="test",
                episode_id="ep_001",
                timeout=5,
                log=log,
            )

        assert success is False
        log.error.assert_called()


# ---------------------------------------------------------------------------
# TestAudioMixingInternals
# ---------------------------------------------------------------------------

class TestAudioMixingInternals:

    def test_music_amplitude_factor_is_minus18db(self):
        """_MUSIC_AMPLITUDE_FACTOR is ≈0.126 (−18 dB relative to narration at 0 dB)."""
        expected = 10 ** (-18 / 20)
        assert abs(_MUSIC_AMPLITUDE_FACTOR - expected) < 0.01

    def test_section_without_music_uses_narration_only(self, tmp_path):
        """When no music track, ffmpeg command uses narration only (no amix)."""
        narration_path = tmp_path / "narration.mp3"
        narration_path.write_bytes(b"\xff\xfb" * 100)

        cmd = _build_ffmpeg_concat_command(
            timeline=[],
            narration_path=narration_path,
            music_track=None,
            output_path=tmp_path / "out.mp4",
            resolution="1920x1080",
            episode_id="ep_001",
        )

        cmd_str = " ".join(str(c) for c in cmd)
        assert "amix" not in cmd_str
        assert "volume=1.0" in cmd_str

    def test_mixing_produces_amix_at_minus18db(self, tmp_path):
        """When music + narration, ffmpeg command includes amix at −18 dB."""
        narration_path = tmp_path / "narration.mp3"
        narration_path.write_bytes(b"\xff\xfb" * 100)
        music_track = _make_music_track(tmp_path)

        cmd = _build_ffmpeg_concat_command(
            timeline=[],
            narration_path=narration_path,
            music_track=music_track,
            output_path=tmp_path / "out.mp4",
            resolution="1920x1080",
            episode_id="ep_001",
        )

        cmd_str = " ".join(str(c) for c in cmd)
        assert "amix" in cmd_str
        assert f"volume={_MUSIC_AMPLITUDE_FACTOR}" in cmd_str


# ---------------------------------------------------------------------------
# TestResolutionNormalization (scale+pad fix for mixed-resolution clips)
# ---------------------------------------------------------------------------

class TestResolutionNormalization:
    """
    Verify that _build_ffmpeg_concat_command normalizes every clip to the
    target resolution inside filter_complex before concat, so that clips
    with different source resolutions (e.g. 1280x720 vs 960x960) can be
    concatenated without ffmpeg error.
    """

    def _fc(self, cmd):
        """Extract filter_complex value from command list."""
        if "-filter_complex" in cmd:
            return cmd[cmd.index("-filter_complex") + 1]
        return ""

    def test_scale_pad_present_for_main_single_clip(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"FAKEVIDEO")
        entry = _make_entry(duration_seconds=8.0, clip_duration_seconds=8.0, clip_path=clip)
        cmd = _build_ffmpeg_concat_command(
            timeline=[entry], narration_path=None, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1920x1080", episode_id="ep_t",
        )
        fc = self._fc(cmd)
        assert "scale=1920:1080" in fc
        assert "force_original_aspect_ratio=decrease" in fc
        assert "pad=1920:1080" in fc

    def test_scale_pad_uses_black_background(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"FAKEVIDEO")
        entry = _make_entry(duration_seconds=8.0, clip_duration_seconds=8.0, clip_path=clip)
        cmd = _build_ffmpeg_concat_command(
            timeline=[entry], narration_path=None, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1920x1080", episode_id="ep_t",
        )
        assert ":black" in self._fc(cmd)

    def test_sv_labels_used_as_concat_inputs(self, tmp_path):
        """Scale-normalized [sv0] label feeds into concat, not raw [0:v]."""
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"FAKEVIDEO")
        entry = _make_entry(duration_seconds=8.0, clip_duration_seconds=8.0, clip_path=clip)
        cmd = _build_ffmpeg_concat_command(
            timeline=[entry], narration_path=None, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1920x1080", episode_id="ep_t",
        )
        fc = self._fc(cmd)
        assert "[sv0]" in fc

    def test_mixed_resolution_two_clips_both_normalized(self, tmp_path):
        """Two clips each get their own sv label in filter_complex."""
        clip1 = tmp_path / "clip1.mp4"
        clip2 = tmp_path / "clip2.mp4"
        clip1.write_bytes(b"FAKEVIDEO")
        clip2.write_bytes(b"FAKEVIDEO")
        entries = [
            _make_entry(scene_index=0, duration_seconds=8.0, clip_duration_seconds=8.0, clip_path=clip1),
            _make_entry(scene_index=1, scene_id="s02", duration_seconds=8.0, clip_duration_seconds=8.0, clip_path=clip2),
        ]
        cmd = _build_ffmpeg_concat_command(
            timeline=entries, narration_path=None, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1920x1080", episode_id="ep_t",
        )
        fc = self._fc(cmd)
        assert "[sv0]" in fc
        assert "[sv1]" in fc
        assert "concat=n=2" in fc

    def test_tpad_chains_from_sv_label(self, tmp_path):
        """When hold is needed, tpad receives [sv0] input, not raw [0:v]."""
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"FAKEVIDEO")
        entry = _make_entry(duration_seconds=20.0, clip_duration_seconds=8.0, clip_path=clip)
        cmd = _build_ffmpeg_concat_command(
            timeline=[entry], narration_path=None, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1920x1080", episode_id="ep_t",
        )
        fc = self._fc(cmd)
        # tpad must receive normalized input, not raw stream
        assert "[sv0]tpad" in fc
        assert "[pv0]" in fc

    def test_short_uses_scale_crop_not_pad(self, tmp_path):
        """Short renders use scale-to-fill + crop, not scale-to-fit + pad."""
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"FAKEVIDEO")
        entry = _make_entry(duration_seconds=8.0, clip_duration_seconds=8.0, clip_path=clip)
        cmd = _build_ffmpeg_concat_command(
            timeline=[entry], narration_path=None, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1080x1920", episode_id="ep_t",
            is_short=True,
        )
        fc = self._fc(cmd)
        assert "force_original_aspect_ratio=increase" in fc
        assert "crop=1080:1920" in fc
        # Must NOT use letterbox pad for shorts
        assert "force_original_aspect_ratio=decrease" not in fc
        assert ":black" not in fc

    def test_short_no_separate_vf_flag(self, tmp_path):
        """After normalization moved to filter_complex, no separate -vf flag is emitted."""
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"FAKEVIDEO")
        entry = _make_entry(duration_seconds=8.0, clip_duration_seconds=8.0, clip_path=clip)
        cmd = _build_ffmpeg_concat_command(
            timeline=[entry], narration_path=None, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1080x1920", episode_id="ep_t",
            is_short=True,
        )
        assert "-vf" not in cmd
