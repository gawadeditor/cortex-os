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
import re
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


def _make_music_track_named(tmp_path: Path, name: str, category: str = "Mystery") -> MusicTrackResult:
    music_path = tmp_path / f"{name}.mp3"
    music_path.write_bytes(b"\xff\xfb" * 100)
    return MusicTrackResult(
        track_id=name,
        category=category,
        asset_id=f"channel/music/mystery/{name}",
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
        Given entry.duration_seconds=20s and clip_duration_seconds=8s, factor=2.5 > 1.0,
        so slow-motion setpts is used (not tpad). No reverse/loop filter is present —
        ping-pong looping was removed (forbidden: reverses causal motion).
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
        # factor=2.5 → slow-motion setpts branch (not tpad, not reverse/loop)
        # Precise ffmpeg filter-syntax check (not a bare substring) — pytest's
        # tmp_path is named after the test function, which itself contains
        # "reverse", so a bare "reverse" in cmd_str check would false-positive
        # on the clip's own file path.
        assert "]reverse[" not in cmd_str
        assert "loop=loop=-1" not in cmd_str
        assert "setpts=2.500000*PTS" in cmd_str
        assert "[pv0]" in cmd_str

    def test_each_entry_gets_own_hold_before_concat(self, tmp_path):
        """
        Multiple entries with factor > 2.0 each get their own slow-motion setpts
        stretch before concat (no reverse/loop). Both entries use clip_duration=8s
        with beat durations 21s and 23s (factors 2.625 and 2.875). The output
        labels [pv0] and [pv1] are used as concat inputs.
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
        # No entry ever uses reverse/loop, regardless of factor size
        # Precise ffmpeg filter-syntax check (not a bare substring) — pytest's
        # tmp_path is named after the test function, which itself contains
        # "reverse", so a bare "reverse" in cmd_str check would false-positive
        # on the clip's own file path.
        assert "]reverse[" not in cmd_str
        assert "loop=loop=-1" not in cmd_str
        assert "[pv0]" in cmd_str
        assert "[pv1]" in cmd_str
        # Both entries get their own setpts factor: 21/8=2.625, 23/8=2.875
        assert "setpts=2.625000*PTS" in cmd_str
        assert "setpts=2.875000*PTS" in cmd_str

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
# TestNoReverseOrPingPong
#
# Regression coverage for the 2026-08-01 quality rejection: the assembled
# video repeatedly played clips forward -> reverse -> forward -> reverse.
# ffmpeg's `reverse` filter and any negative-time playback are forbidden —
# a clip's real motion must only ever play forward, and must never be
# shown more than once within a single narration segment.
# ---------------------------------------------------------------------------

class TestNoReverseOrPingPong:

    @pytest.mark.parametrize("hold_factor", [1.5, 2.0, 2.001, 2.5, 3.0, 5.0, 10.0, 50.0])
    def test_no_reverse_filter_at_any_factor(self, tmp_path, hold_factor):
        """No `reverse` ffmpeg filter is ever emitted, regardless of how large
        the narration-to-clip duration factor is."""
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")
        clip_duration = 8.0
        entry = _make_entry(
            duration_seconds=clip_duration * hold_factor,
            clip_duration_seconds=clip_duration,
            clip_path=clip_path,
        )

        cmd = _build_ffmpeg_concat_command(
            timeline=[entry],
            narration_path=None,
            music_track=None,
            output_path=tmp_path / "out.mp4",
            resolution="1920x1080",
            episode_id="ep_test",
        )
        cmd_str = " ".join(str(c) for c in cmd)
        # Precise ffmpeg filter-syntax check (not a bare substring) — pytest's
        # tmp_path is named after the test function, which itself contains
        # "reverse", so a bare "reverse" in cmd_str check would false-positive
        # on the clip's own file path.
        assert "]reverse[" not in cmd_str
        assert "split=2" not in cmd_str

    @pytest.mark.parametrize("hold_factor", [1.5, 2.5, 5.0, 20.0])
    def test_no_negative_time_playback(self, tmp_path, hold_factor):
        """Every setpts multiplier used for a motion-extension hold must be
        strictly positive — a negative or zero PTS multiplier is the
        signature of reversed or frozen (non-causal) playback."""
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")
        clip_duration = 8.0
        entry = _make_entry(
            duration_seconds=clip_duration * hold_factor,
            clip_duration_seconds=clip_duration,
            clip_path=clip_path,
        )

        cmd = _build_ffmpeg_concat_command(
            timeline=[entry],
            narration_path=None,
            music_track=None,
            output_path=tmp_path / "out.mp4",
            resolution="1920x1080",
            episode_id="ep_test",
        )
        cmd_str = " ".join(str(c) for c in cmd)

        setpts_multipliers = re.findall(r"setpts=(-?\d+\.\d+)\*PTS", cmd_str)
        assert setpts_multipliers, "expected at least one setpts multiplier for a motion-extension hold"
        for raw in setpts_multipliers:
            assert float(raw) > 0, f"found non-positive setpts multiplier: {raw}"

    def test_no_loop_filter_for_motion_extension(self, tmp_path):
        """The ffmpeg `loop` filter (used by both the pre-2a10a34 forward-only
        loop and the later ping-pong loop) must not appear — motion extension
        is pure setpts time-stretching with no loop boundary at all."""
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")
        entry = _make_entry(duration_seconds=80.0, clip_duration_seconds=8.0, clip_path=clip_path)

        cmd = _build_ffmpeg_concat_command(
            timeline=[entry],
            narration_path=None,
            music_track=None,
            output_path=tmp_path / "out.mp4",
            resolution="1920x1080",
            episode_id="ep_test",
        )
        cmd_str = " ".join(str(c) for c in cmd)
        assert "loop=" not in cmd_str

    def test_clip_content_never_repeats_within_a_segment(self, tmp_path):
        """
        A single motion-extension entry must reference its source clip's
        content exactly once (one setpts-stretched pass) — never a concat of
        the same clip with itself, which would be a visible repeat.
        """
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")
        entry = _make_entry(duration_seconds=40.0, clip_duration_seconds=8.0, clip_path=clip_path)

        cmd = _build_ffmpeg_concat_command(
            timeline=[entry],
            narration_path=None,
            music_track=None,
            output_path=tmp_path / "out.mp4",
            resolution="1920x1080",
            episode_id="ep_test",
        )
        cmd_str = " ".join(str(c) for c in cmd)
        # Only one -i for this entry's own clip; no self-concat of the clip stream.
        assert cmd_str.count(str(clip_path)) == 1
        assert "concat=n=2:v=1:a=0" not in cmd_str


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

    def test_music_amplitude_factor_is_minus14db(self):
        """_MUSIC_AMPLITUDE_FACTOR is ≈0.20 (−14 dB relative to narration at 0 dB) — audible background bed."""
        expected = 10 ** (-14 / 20)
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

    def test_short_with_clip_narration_and_music_all_audio_indices_valid(self, tmp_path):
        """Regression test for a real 2026-07-05 production failure: a Short
        with one video clip + narration + music produced a filtergraph
        referencing [3:a] when only 3 inputs (indices 0-2) existed --
        'Invalid file index 3 in filtergraph description', causing every
        short in that episode to fail and cascading into
        production_ready=False. Already fixed in code (audio_input_index is
        derived from inputs.count('-i'), not a hardcoded offset), but no
        test previously covered this exact clip+narration+music+is_short
        combination -- this locks the fix in so it cannot silently regress."""
        import re

        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"\xff\xfb" * 100)
        narration_path = tmp_path / "narration.mp3"
        narration_path.write_bytes(b"\xff\xfb" * 100)
        music_track = _make_music_track(tmp_path)

        entry = _make_entry(
            source_type="short", short_id="short_001",
            clip_path=clip_path, duration_seconds=4.5, clip_duration_seconds=4.5,
        )

        cmd = _build_ffmpeg_concat_command(
            timeline=[entry],
            narration_path=narration_path,
            music_track=music_track,
            output_path=tmp_path / "out.mp4",
            resolution="1080x1920",
            episode_id="ep_001",
            is_short=True,
        )

        n_inputs = cmd.count("-i")
        filter_str = " ".join(str(c) for c in cmd)
        referenced_indices = {int(m) for m in re.findall(r"\[(\d+):a\]", filter_str)}
        assert referenced_indices, "expected at least one [N:a] audio reference"
        for idx in referenced_indices:
            assert idx < n_inputs, (
                f"filtergraph references audio input index {idx}, but only "
                f"{n_inputs} -i argument(s) were supplied (valid range "
                f"0..{n_inputs - 1}) -- this is the exact 'Invalid file "
                f"index' failure mode from the 2026-07-05 incident"
            )


# ---------------------------------------------------------------------------
# TestMultiSegmentMusic (Option B, 2026-07-19 — main episode only)
# ---------------------------------------------------------------------------

class TestMultiSegmentMusic:
    """
    _build_ffmpeg_concat_command() places multiple timed music segments into
    one [msc] stream (see _build_multi_segment_music_filter). Everything
    downstream of [msc] -- the narration mix, loudnorm -- is byte-identical
    to the single-track path; these tests only probe how [msc] itself gets
    built.
    """

    def _fc(self, cmd):
        if "-filter_complex" in cmd:
            return cmd[cmd.index("-filter_complex") + 1]
        return ""

    def test_two_segments_produce_two_delayed_streams_summed_via_amix(self, tmp_path):
        narration_path = tmp_path / "narration.mp3"
        narration_path.write_bytes(b"\xff\xfb" * 100)
        seg_a = _make_music_track_named(tmp_path, "seg_a", category="Mystery")
        seg_b = _make_music_track_named(tmp_path, "seg_b", category="Tension")
        music_segments = [
            {"start_seconds": 0.0, "end_seconds": 40.0, "track": seg_a},
            {"start_seconds": 40.0, "end_seconds": 90.0, "track": seg_b},
        ]

        cmd = _build_ffmpeg_concat_command(
            timeline=[], narration_path=narration_path, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1920x1080",
            episode_id="ep_001", music_segments=music_segments,
        )
        fc = self._fc(cmd)

        assert "[mscseg0]" in fc
        assert "[mscseg1]" in fc
        assert "adelay=delays=0:all=1" in fc          # first segment starts at t=0
        assert "adelay=delays=40000:all=1" in fc      # second segment starts at t=40s
        assert "amix=inputs=2" in fc
        assert "[msc]" in fc
        assert "[narr][msc]amix" in fc                # unchanged downstream combine step

    def test_single_segment_uses_anull_not_amix_for_segment_combine(self, tmp_path):
        """With exactly one real-track segment, the segment stream is
        passed straight through via anull -- no self-amix of one input."""
        narration_path = tmp_path / "narration.mp3"
        narration_path.write_bytes(b"\xff\xfb" * 100)
        seg = _make_music_track_named(tmp_path, "seg_only")
        music_segments = [{"start_seconds": 0.0, "end_seconds": 60.0, "track": seg}]

        cmd = _build_ffmpeg_concat_command(
            timeline=[], narration_path=narration_path, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1920x1080",
            episode_id="ep_001", music_segments=music_segments,
        )
        fc = self._fc(cmd)

        assert "[mscseg0]anull[msc]" in fc
        assert "amix=inputs=1" not in fc

    def test_none_music_segments_falls_back_to_legacy_single_track(self, tmp_path):
        """music_segments omitted entirely reproduces the original
        single-track filtergraph byte-for-byte."""
        narration_path = tmp_path / "narration.mp3"
        narration_path.write_bytes(b"\xff\xfb" * 100)
        music_track = _make_music_track(tmp_path)

        cmd_legacy = _build_ffmpeg_concat_command(
            timeline=[], narration_path=narration_path, music_track=music_track,
            output_path=tmp_path / "out.mp4", resolution="1920x1080",
            episode_id="ep_001",
        )
        cmd_explicit_none = _build_ffmpeg_concat_command(
            timeline=[], narration_path=narration_path, music_track=music_track,
            output_path=tmp_path / "out.mp4", resolution="1920x1080",
            episode_id="ep_001", music_segments=None,
        )
        assert cmd_legacy == cmd_explicit_none
        assert "mscseg" not in " ".join(str(c) for c in cmd_legacy)

    def test_segments_with_no_track_fall_back_to_legacy_single_track(self, tmp_path):
        """Backward compatibility with an older single-key music_track_package:
        when every segment's track is None (Assembly could not resolve any
        per-section track), the legacy music_track parameter is used
        instead of an empty/silent multi-segment graph."""
        narration_path = tmp_path / "narration.mp3"
        narration_path.write_bytes(b"\xff\xfb" * 100)
        legacy_track = _make_music_track(tmp_path)
        music_segments = [
            {"start_seconds": 0.0, "end_seconds": 40.0, "track": None},
            {"start_seconds": 40.0, "end_seconds": 90.0, "track": None},
        ]

        cmd = _build_ffmpeg_concat_command(
            timeline=[], narration_path=narration_path, music_track=legacy_track,
            output_path=tmp_path / "out.mp4", resolution="1920x1080",
            episode_id="ep_001", music_segments=music_segments,
        )
        fc = self._fc(cmd)

        assert "mscseg" not in fc
        assert "[msc]" in fc  # legacy single-track loop path still present

    def test_missing_track_segment_excluded_leaves_gap_not_error(self, tmp_path):
        """A mix of present and missing per-section tracks only emits inputs
        for the present ones -- the missing section's window is silence
        (excluded), not a crash or a stretched neighbor."""
        narration_path = tmp_path / "narration.mp3"
        narration_path.write_bytes(b"\xff\xfb" * 100)
        seg_a = _make_music_track_named(tmp_path, "present")
        music_segments = [
            {"start_seconds": 0.0, "end_seconds": 40.0, "track": seg_a},
            {"start_seconds": 40.0, "end_seconds": 90.0, "track": None},
        ]

        cmd = _build_ffmpeg_concat_command(
            timeline=[], narration_path=narration_path, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1920x1080",
            episode_id="ep_001", music_segments=music_segments,
        )
        fc = self._fc(cmd)

        assert "[mscseg0]" in fc
        assert "[mscseg1]" not in fc
        assert "[mscseg0]anull[msc]" in fc

    def test_multi_segment_all_audio_indices_valid(self, tmp_path):
        """Same class of regression as the 2026-07-05 [3:a] incident: every
        [N:a] index referenced in the filtergraph must correspond to a real
        -i input, including the new multi-segment music inputs."""
        import re

        narration_path = tmp_path / "narration.mp3"
        narration_path.write_bytes(b"\xff\xfb" * 100)
        seg_a = _make_music_track_named(tmp_path, "seg_a")
        seg_b = _make_music_track_named(tmp_path, "seg_b")
        music_segments = [
            {"start_seconds": 0.0, "end_seconds": 40.0, "track": seg_a},
            {"start_seconds": 40.0, "end_seconds": 90.0, "track": seg_b},
        ]

        cmd = _build_ffmpeg_concat_command(
            timeline=[], narration_path=narration_path, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1920x1080",
            episode_id="ep_001", music_segments=music_segments,
        )
        n_inputs = cmd.count("-i")
        fc = self._fc(cmd)
        referenced_indices = {int(m) for m in re.findall(r"\[(\d+):a\]", fc)}
        assert referenced_indices
        for idx in referenced_indices:
            assert idx < n_inputs

    def test_last_segment_extended_by_music_tail_seconds(self, tmp_path):
        """The final segment's atrim duration is extended by
        music_tail_seconds so music continues into the outro hold, matching
        single-track behavior; earlier segments are not extended."""
        narration_path = tmp_path / "narration.mp3"
        narration_path.write_bytes(b"\xff\xfb" * 100)
        seg_a = _make_music_track_named(tmp_path, "seg_a")
        seg_b = _make_music_track_named(tmp_path, "seg_b")
        music_segments = [
            {"start_seconds": 0.0, "end_seconds": 40.0, "track": seg_a},
            {"start_seconds": 40.0, "end_seconds": 90.0, "track": seg_b},
        ]

        cmd = _build_ffmpeg_concat_command(
            timeline=[], narration_path=narration_path, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1920x1080",
            episode_id="ep_001", music_segments=music_segments,
            music_tail_seconds=2.5,
        )
        fc = self._fc(cmd)
        # seg_a (not last): atrim=0:40.000000 unchanged by the tail.
        assert "atrim=0:40.000000,asetpts=PTS-STARTPTS,adelay=delays=0" in fc
        # seg_b (last): 50s window + 2.5s tail = 52.5s.
        assert "atrim=0:52.500000,asetpts=PTS-STARTPTS,adelay=delays=40000" in fc

    def test_is_short_ignores_music_segments(self, tmp_path):
        """Shorts must remain completely unchanged (Option 1, 2026-07-19) --
        even if a caller mistakenly passed music_segments for a Short, the
        renderer path itself is untouched by is_short (Assembly Agent never
        actually does this in production; this only guards the ffmpeg
        command builder's own behavior)."""
        narration_path = tmp_path / "narration.mp3"
        narration_path.write_bytes(b"\xff\xfb" * 100)
        music_track = _make_music_track(tmp_path)

        cmd_no_segments = _build_ffmpeg_concat_command(
            timeline=[], narration_path=narration_path, music_track=music_track,
            output_path=tmp_path / "out.mp4", resolution="1080x1920",
            episode_id="ep_001", is_short=True,
        )
        assert "mscseg" not in " ".join(str(c) for c in cmd_no_segments)


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

    def test_motion_extension_chains_from_sv_label(self, tmp_path):
        """When hold > 1.0s with factor > 2.0, the slow-motion setpts branch receives [sv0] input directly (no split/reverse/loop)."""
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"FAKEVIDEO")
        entry = _make_entry(duration_seconds=20.0, clip_duration_seconds=8.0, clip_path=clip)
        cmd = _build_ffmpeg_concat_command(
            timeline=[entry], narration_path=None, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1920x1080", episode_id="ep_t",
        )
        fc = self._fc(cmd)
        # factor=2.5 → slow-motion setpts branch chains directly off [sv0]
        assert "[sv0]setpts=" in fc
        assert "split=2" not in fc
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
