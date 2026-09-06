"""
tests/test_video_renderer.py — Unit tests for hypogeum/agents/video_renderer.py

Tests cover the public API surface and critical internal helpers:
  TestRenderEpisode             — render_episode() end-to-end (manifest shape, statuses)
  TestBuildFfmpegConcatCommand  — _build_ffmpeg_concat_command() (tpad, -t, audio)
  TestRunFfmpeg                 — _run_ffmpeg() (success, failure, timeout)
  TestAudioMixingInternals      — audio mix constants and command structure
  TestNoAudioGapAtBodyTail      — real-ffmpeg regression: narration-shortfall no longer
                                  silences music / leaves a gap at the body-to-end-page seam
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.coverage_check import MAX_EDITORIAL_SPEED_FACTOR
from hypogeum.agents.video_renderer import (
    FRAME_RATE,
    MAIN_RESOLUTION,
    SHORT_RESOLUTION,
    InsufficientCoverageError,
    SubtitleFilterUnavailableError,
    _MUSIC_AMPLITUDE_FACTOR,
    _build_caption_ass,
    _build_ffmpeg_concat_command,
    _build_subtitles_filter,
    _parse_srt_cues,
    _run_ffmpeg,
    _srt_timestamp_to_ass,
    assert_subtitle_filter_available,
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

    def test_bounded_stretch_applied_within_editorial_limit(self, tmp_path):
        """
        Given entry.duration_seconds=8.8s and clip_duration_seconds=8.0s, factor=1.1
        is within MAX_EDITORIAL_SPEED_FACTOR (1.10) — a bounded sync-correction
        setpts is used. No reverse/loop/tpad filter is present.
        """
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")

        entry = _make_entry(duration_seconds=8.8, clip_duration_seconds=8.0, clip_path=clip_path)

        cmd = _build_ffmpeg_concat_command(
            timeline=[entry],
            narration_path=None,
            music_track=None,
            output_path=tmp_path / "out.mp4",
            resolution="1920x1080",
            episode_id="ep_test",
        )

        cmd_str = " ".join(str(c) for c in cmd)
        # factor=1.1 (within the editorial limit) → bounded setpts, no
        # tpad/reverse/loop. Precise ffmpeg filter-syntax check (not a bare
        # substring) — pytest's tmp_path is named after the test function.
        assert "]reverse[" not in cmd_str
        assert "loop=loop=-1" not in cmd_str
        assert "tpad" not in cmd_str
        assert "setpts=1.100000*PTS" in cmd_str
        assert "[pv0]" in cmd_str

    def test_large_factor_raises_instead_of_stretching(self, tmp_path):
        """
        A beat whose only clip cannot reach the narration duration even at
        MAX_EDITORIAL_SPEED_FACTOR must raise InsufficientCoverageError, not
        silently apply a large setpts factor to conceal the gap (2026-08-01
        policy — supersedes the interim unconditional-setpts fix in commit
        44ae397).
        """
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")

        entry = _make_entry(duration_seconds=20.0, clip_duration_seconds=8.0, clip_path=clip_path)

        with pytest.raises(InsufficientCoverageError):
            _build_ffmpeg_concat_command(
                timeline=[entry],
                narration_path=None,
                music_track=None,
                output_path=tmp_path / "out.mp4",
                resolution="1920x1080",
                episode_id="ep_test",
            )

    def test_each_entry_gets_own_bounded_stretch_before_concat(self, tmp_path):
        """
        Multiple entries each within the editorial limit get their own bounded
        setpts stretch before concat (no reverse/loop/tpad). Both entries use
        clip_duration=8s with beat durations 8.4s and 8.8s (factors 1.05 and
        1.10). The output labels [pv0] and [pv1] are used as concat inputs.
        """
        clip1 = tmp_path / "clip1.mp4"
        clip2 = tmp_path / "clip2.mp4"
        clip1.write_bytes(b"FAKEVIDEO")
        clip2.write_bytes(b"FAKEVIDEO")

        entries = [
            _make_entry(scene_index=0, duration_seconds=8.4, clip_duration_seconds=8.0, clip_path=clip1),
            _make_entry(scene_index=1, scene_id="sc_002", duration_seconds=8.8, clip_duration_seconds=8.0, clip_path=clip2),
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
        # No entry ever uses reverse/loop/tpad, regardless of factor size
        # Precise ffmpeg filter-syntax check (not a bare substring) — pytest's
        # tmp_path is named after the test function, which itself contains
        # "reverse", so a bare "reverse" in cmd_str check would false-positive
        # on the clip's own file path.
        assert "]reverse[" not in cmd_str
        assert "loop=loop=-1" not in cmd_str
        assert "tpad" not in cmd_str
        assert "[pv0]" in cmd_str
        assert "[pv1]" in cmd_str
        # Both entries get their own bounded setpts factor: 8.4/8=1.05, 8.8/8=1.10
        assert "setpts=1.050000*PTS" in cmd_str
        assert "setpts=1.100000*PTS" in cmd_str

    def test_t_flag_equals_timeline_duration_not_clip_duration(self, tmp_path):
        """
        The ffmpeg -t flag equals total timeline (narration) duration, not clip
        duration. For an 8.8s beat with an 8s clip (within the editorial
        limit), -t must be 8.8, not 8.0.
        """
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")

        entry = _make_entry(duration_seconds=8.8, clip_duration_seconds=8.0, clip_path=clip_path)

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
        assert float(cmd[t_idx + 1]) == pytest.approx(8.8)

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
            duration_seconds=8.8,
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
        assert float(cmd[t_idx + 1]) == pytest.approx(8.8)


# ---------------------------------------------------------------------------
# TestCleanCutPolicy (2026-08-06)
#
# Regression coverage for the production-review directing rule: the default
# transition everywhere is a clean cut. No fade=t=out/fade=t=in, no dissolve,
# no xfade, no dip-to-black may appear at a normal shot boundary. A former
# 2-frame tail micro-fade (_CLIP_TAIL_FADE_SECS) was removed for exactly
# this reason — these tests assert its absence from every code path that
# used to add it: an earlier clip in a multi-clip beat, a trimmed last
# clip, and a speed-nudged last clip.
# ---------------------------------------------------------------------------

class TestCleanCutPolicy:

    def _assert_no_fade_or_transition_filters(self, cmd_str: str) -> None:
        assert "fade=t=out" not in cmd_str
        assert "fade=t=in" not in cmd_str
        assert "xfade" not in cmd_str
        assert "dissolve" not in cmd_str

    def test_single_clip_beat_has_no_fade(self, tmp_path):
        """The exact-fit single-clip case (no trim, no stretch needed) has
        never had a reason to fade — confirm it still doesn't."""
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")
        entry = _make_entry(duration_seconds=8.0, clip_duration_seconds=8.0, clip_path=clip_path)

        cmd = _build_ffmpeg_concat_command(
            timeline=[entry], narration_path=None, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1920x1080", episode_id="ep_test",
        )
        cmd_str = " ".join(str(c) for c in cmd)
        self._assert_no_fade_or_transition_filters(cmd_str)

    def test_earlier_clip_in_multi_clip_beat_has_no_fade(self, tmp_path):
        """The non-last clip in a multi-clip beat (coverage-fill extras) —
        this was one of the three fade sites removed."""
        primary = tmp_path / "primary.mp4"
        extra1 = tmp_path / "extra1.mp4"
        primary.write_bytes(b"FAKEVIDEO")
        extra1.write_bytes(b"FAKEVIDEO")

        entry = _make_entry(duration_seconds=16.0, clip_duration_seconds=8.0, clip_path=primary)
        entry.extra_clip_paths = [extra1]

        with patch("hypogeum.agents.coverage_check._probe_duration_seconds", return_value=8.0):
            cmd = _build_ffmpeg_concat_command(
                timeline=[entry], narration_path=None, music_track=None,
                output_path=tmp_path / "out.mp4", resolution="1920x1080", episode_id="ep_test",
            )
        cmd_str = " ".join(str(c) for c in cmd)
        self._assert_no_fade_or_transition_filters(cmd_str)

    def test_trimmed_last_clip_has_no_fade(self, tmp_path):
        """Last clip runs long and is trimmed to the beat's remaining
        duration — trimming real excess footage is not a transition and
        must not carry a fade."""
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")
        # clip_duration 8.0s, beat only needs 5.0s -> trim branch.
        entry = _make_entry(duration_seconds=5.0, clip_duration_seconds=8.0, clip_path=clip_path)

        cmd = _build_ffmpeg_concat_command(
            timeline=[entry], narration_path=None, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1920x1080", episode_id="ep_test",
        )
        cmd_str = " ".join(str(c) for c in cmd)
        self._assert_no_fade_or_transition_filters(cmd_str)
        assert "trim=duration=5.000" in cmd_str

    def test_speed_nudged_last_clip_has_no_fade(self, tmp_path):
        """Last clip is a hair short even at full length -- bounded
        editorial-limit setpts nudge, still no fade."""
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")
        entry = _make_entry(duration_seconds=8.8, clip_duration_seconds=8.0, clip_path=clip_path)

        cmd = _build_ffmpeg_concat_command(
            timeline=[entry], narration_path=None, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1920x1080", episode_id="ep_test",
        )
        cmd_str = " ".join(str(c) for c in cmd)
        self._assert_no_fade_or_transition_filters(cmd_str)
        assert "setpts=1.100000*PTS" in cmd_str

    def test_no_black_frame_inserted_between_normal_clips(self, tmp_path):
        """A normal (present) clip must never be replaced by or preceded by
        a black lavfi source -- that fallback exists only for a genuinely
        missing clip, a distinct failure mode from a transition."""
        clip1 = tmp_path / "clip1.mp4"
        clip2 = tmp_path / "clip2.mp4"
        clip1.write_bytes(b"FAKEVIDEO")
        clip2.write_bytes(b"FAKEVIDEO")
        entries = [
            _make_entry(scene_index=0, duration_seconds=8.0, clip_duration_seconds=8.0, clip_path=clip1),
            _make_entry(scene_index=1, scene_id="sc_002", duration_seconds=8.0, clip_duration_seconds=8.0, clip_path=clip2),
        ]
        cmd = _build_ffmpeg_concat_command(
            timeline=entries, narration_path=None, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1920x1080", episode_id="ep_test",
        )
        cmd_str = " ".join(str(c) for c in cmd)
        assert "color=c=black" not in cmd_str
        self._assert_no_fade_or_transition_filters(cmd_str)


# ---------------------------------------------------------------------------
# TestEndCardTransition (2026-08-06)
#
# Regression coverage for the production-review directing rule on the
# story-to-end-card boundary: clean cut into a visible end card, continuous
# music across the cut, exactly one natural music fade-out positioned at
# the end-card entrance (never stacked, never at the tail of the whole
# file, never paired with a video fade).
# ---------------------------------------------------------------------------

class TestEndCardTransition:

    def _call_append_end_page(self, tmp_path, music_track=None):
        from hypogeum.agents.video_renderer import _append_end_page

        main_output = tmp_path / "main.mp4"
        main_output.write_bytes(b"FAKEBODY")
        end_page_path = tmp_path / "endpage.png"
        end_page_path.write_bytes(b"FAKEPNG")

        captured_cmds = []

        def _fake_run_ffmpeg(cmd, output_path, component, episode_id, timeout, log):
            captured_cmds.append(cmd)
            Path(output_path).write_bytes(b"FAKEOUT")
            return True

        with patch("hypogeum.agents.video_renderer._probe_music_usable_duration", return_value=100.0), \
             patch("hypogeum.agents.video_renderer._run_ffmpeg", side_effect=_fake_run_ffmpeg):
            ok = _append_end_page(
                main_output_path=main_output,
                end_page_path=end_page_path,
                duration=8.0,
                episode_id="ep_test",
                timeout=60,
                log=MagicMock(),
                music_track=music_track,
                body_duration=700.0,
                music_segment_start_seconds=0.0,
            )
        assert ok is True
        return captured_cmds

    def test_end_card_appears_through_a_clean_cut(self, tmp_path):
        """Neither the end-page clip's own generation nor the final concat
        may contain a video fade -- the body's last real frame cuts
        directly to the end card's first visible frame."""
        cmds = self._call_append_end_page(tmp_path, music_track=_make_music_track(tmp_path))
        all_str = " ".join(" ".join(str(c) for c in cmd) for cmd in cmds)
        assert "fade=t=in" not in all_str
        assert "fade=t=out:st=" not in all_str.replace("afade=t=out:st=", "")  # video fade only
        assert "[vc]fade" not in all_str
        assert "xfade" not in all_str
        assert "dissolve" not in all_str

    def test_music_continuous_and_exactly_one_fade(self, tmp_path):
        """Music must continue seamlessly from the body (via the existing
        offset/aloop continuation) and fade exactly once -- never stacked
        across the end-page clip AND the final concat."""
        cmds = self._call_append_end_page(tmp_path, music_track=_make_music_track(tmp_path))
        all_str = " ".join(" ".join(str(c) for c in cmd) for cmd in cmds)
        assert all_str.count("afade=t=out") == 1
        # Continuation mechanism (offset seek + aloop) still present -- not a restart.
        assert "-ss" in cmds[0]
        assert "aloop=loop=-1" in all_str

    def test_music_fade_starts_at_end_card_entrance_not_file_tail(self, tmp_path):
        """The one music fade starts at st=0 of the end-page clip's own
        timeline -- i.e. exactly when the end card enters -- not at
        total_duration-2 (the old tail-of-whole-file position)."""
        cmds = self._call_append_end_page(tmp_path, music_track=_make_music_track(tmp_path))
        all_str = " ".join(" ".join(str(c) for c in cmd) for cmd in cmds)
        assert "afade=t=out:st=0.000:d=" in all_str

    def test_no_black_frame_interval_no_music_no_fade_needed(self, tmp_path):
        """Silent end page (no music track): still no video fade, and no
        stray audio fade on a track that was never playing."""
        cmds = self._call_append_end_page(tmp_path, music_track=None)
        all_str = " ".join(" ".join(str(c) for c in cmd) for cmd in cmds)
        assert "fade=t=in" not in all_str
        assert "[vc]fade" not in all_str
        assert "afade=t=out" not in all_str  # nothing playing -- nothing to fade

    def test_concat_step_has_no_stacked_fade(self, tmp_path):
        """The final body+endpage concat command specifically must carry no
        fade filter of its own -- the end-page clip's own single fade
        (asserted above) is the only one that may exist."""
        cmds = self._call_append_end_page(tmp_path, music_track=_make_music_track(tmp_path))
        concat_cmd = cmds[-1]  # concat is always the second/last _run_ffmpeg call
        concat_str = " ".join(str(c) for c in concat_cmd)
        assert "fade" not in concat_str
        assert "concat=n=2:v=1:a=1" in concat_str


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
    """
    2026-08-01 policy update: the interim fix (commit 44ae397) removed the
    reverse/ping-pong branch but still applied an UNCONDITIONAL setpts
    stretch to fill any size of coverage gap. That is now itself forbidden
    ("no excessive slow-motion used to fill missing duration") — a factor
    beyond MAX_EDITORIAL_SPEED_FACTOR must raise InsufficientCoverageError
    instead of being silently applied. These tests are split accordingly:
    small, in-limit factors still produce clean forward-only setpts; large,
    out-of-limit factors raise rather than stretch.
    """

    @pytest.mark.parametrize("hold_factor", [1.0, 1.05, 1.08, 1.1])
    def test_no_reverse_filter_within_editorial_limit(self, tmp_path, hold_factor):
        """No `reverse` ffmpeg filter is ever emitted for an in-limit factor."""
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

    @pytest.mark.parametrize("hold_factor", [1.11, 1.16, 1.5, 2.0, 2.5, 3.0, 5.0, 10.0, 50.0])
    def test_large_factor_raises_rather_than_conceals_gap(self, tmp_path, hold_factor):
        """A factor beyond MAX_EDITORIAL_SPEED_FACTOR must raise
        InsufficientCoverageError — Video Renderer is not permitted to
        conceal a real coverage gap with an oversized setpts stretch,
        regardless of how large the factor is."""
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")
        clip_duration = 8.0
        entry = _make_entry(
            duration_seconds=clip_duration * hold_factor,
            clip_duration_seconds=clip_duration,
            clip_path=clip_path,
        )

        with pytest.raises(InsufficientCoverageError):
            _build_ffmpeg_concat_command(
                timeline=[entry],
                narration_path=None,
                music_track=None,
                output_path=tmp_path / "out.mp4",
                resolution="1920x1080",
                episode_id="ep_test",
            )

    @pytest.mark.parametrize("hold_factor", [1.0, 1.05, 1.08, 1.1])
    def test_no_negative_time_playback(self, tmp_path, hold_factor):
        """Every setpts multiplier used for an in-limit sync correction must
        be strictly positive — a negative or zero PTS multiplier is the
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
        for raw in setpts_multipliers:
            assert float(raw) > 0, f"found non-positive setpts multiplier: {raw}"

    def test_no_loop_or_tpad_filter_for_bounded_stretch(self, tmp_path):
        """The ffmpeg `loop` and `tpad` filters must not appear for an
        in-limit sync correction — it is pure setpts time-stretching with no
        loop boundary and no frozen frame at all."""
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")
        entry = _make_entry(duration_seconds=8.8, clip_duration_seconds=8.0, clip_path=clip_path)

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
        # Precise ffmpeg filter-syntax check — pytest's tmp_path is named
        # after the test function, which itself contains "tpad" as a
        # substring, so a bare "tpad" in cmd_str check false-positives on
        # the clip's own file path.
        assert "tpad=stop_mode" not in cmd_str

    def test_clip_content_never_repeats_within_a_segment(self, tmp_path):
        """
        A single-clip beat must reference its source clip's content exactly
        once (one setpts-stretched pass) — never a concat of the same clip
        with itself, which would be a visible repeat.
        """
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")
        entry = _make_entry(duration_seconds=8.8, clip_duration_seconds=8.0, clip_path=clip_path)

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

    def test_extra_coverage_clips_never_repeat_within_a_beat(self, tmp_path):
        """
        When a beat's deficit is closed with additional distinct clips
        (TimelineEntry.extra_clip_paths — 2026-08-01 coverage policy), every
        clip file is referenced exactly once: the primary clip plus each
        extra, all forward, none repeated. This is the real mechanism that
        satisfies "no repeated clip within a beat" for beats that need more
        than one source clip to reach their narration duration.
        """
        primary = tmp_path / "primary.mp4"
        extra1 = tmp_path / "extra1.mp4"
        extra2 = tmp_path / "extra2.mp4"
        for p in (primary, extra1, extra2):
            p.write_bytes(b"FAKEVIDEO")

        entry = _make_entry(duration_seconds=20.0, clip_duration_seconds=8.0, clip_path=primary)
        entry.extra_clip_paths = [extra1, extra2]

        # Coverage is now computed exclusively by coverage_check.py (per the
        # 2026-08-01 "single source of truth" refactor) — patch its probe,
        # not video_renderer's (which no longer probes extras at all).
        with patch(
            "hypogeum.agents.coverage_check._probe_duration_seconds",
            return_value=8.0,
        ):
            cmd = _build_ffmpeg_concat_command(
                timeline=[entry],
                narration_path=None,
                music_track=None,
                output_path=tmp_path / "out.mp4",
                resolution="1920x1080",
                episode_id="ep_test",
            )
        cmd_str = " ".join(str(c) for c in cmd)

        # Every clip file appears in the command exactly once.
        assert cmd_str.count(str(primary)) == 1
        assert cmd_str.count(str(extra1)) == 1
        assert cmd_str.count(str(extra2)) == 1
        # Concatenated forward, once each — 3 inputs feed the outer concat.
        assert "concat=n=3:v=1:a=0" in cmd_str
        assert "]reverse[" not in cmd_str
        assert "loop=" not in cmd_str
        assert "tpad" not in cmd_str


# ---------------------------------------------------------------------------
# TestBurnedInCaptions
#
# Regression coverage for the 2026-08-01 quality rejection: Shorts had
# platform CC only, no burned-in on-screen text. The fix burns captions
# into pixels via ffmpeg's native `subtitles` filter — but a plain .srt
# has no embedded PlayResX/PlayResY, so libass falls back to an internal
# default script resolution and force_style values (FontSize, MarginV)
# get interpreted in THAT space, not the real output resolution —
# confirmed empirically: text rendered gigantic and pinned to the top of
# frame instead of a readable bottom-center caption. The fix converts the
# SRT to a minimal ASS with explicit PlayResX/PlayResY matching the real
# render resolution. These tests assert on exactly that value being
# present and correct — the root cause of the original bug.
# ---------------------------------------------------------------------------

class TestBurnedInCaptions:

    def test_srt_timestamp_to_ass_conversion(self):
        assert _srt_timestamp_to_ass("00", "00", "01", "602") == "0:00:01.60"
        assert _srt_timestamp_to_ass("01", "23", "45", "678") == "1:23:45.67"
        assert _srt_timestamp_to_ass("00", "00", "00", "046") == "0:00:00.04"

    def test_parse_srt_cues_basic(self):
        srt = (
            "1\n00:00:00,046 --> 00:00:01,602\nThere is a passage\n\n"
            "2\n00:00:01,672 --> 00:00:04,331\ncalled a schlupf.\n"
        )
        cues = _parse_srt_cues(srt)
        assert len(cues) == 2
        assert cues[0] == ("0:00:00.04", "0:00:01.60", "There is a passage")
        assert cues[1] == ("0:00:01.67", "0:00:04.33", "called a schlupf.")

    def test_parse_srt_cues_multiline_joined_with_ass_linebreak(self):
        srt = "1\n00:00:00,000 --> 00:00:02,000\nLine one\nLine two\n"
        cues = _parse_srt_cues(srt)
        assert cues[0][2] == "Line one\\NLine two"

    def test_parse_srt_cues_skips_malformed_blocks(self):
        srt = "1\nnot a timing line\ntext\n\n2\n00:00:00,000 --> 00:00:01,000\nreal cue\n"
        cues = _parse_srt_cues(srt)
        assert len(cues) == 1
        assert cues[0][2] == "real cue"

    def test_parse_srt_cues_empty_input_returns_empty_list(self):
        assert _parse_srt_cues("") == []

    def test_build_caption_ass_sets_correct_playres(self, tmp_path):
        """The exact root cause of the original bug: PlayResX/PlayResY must
        match the real render resolution, not be absent or a mismatched default."""
        srt_path = tmp_path / "cap.srt"
        srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
        dest = tmp_path / "cap.ass"

        ok = _build_caption_ass(srt_path, "1080x1920", dest)

        assert ok is True
        content = dest.read_text()
        assert "PlayResX: 1080" in content
        assert "PlayResY: 1920" in content

    def test_build_caption_ass_uses_given_resolution_not_a_default(self, tmp_path):
        """A different resolution (main episode, if ever used) must produce different PlayRes."""
        srt_path = tmp_path / "cap.srt"
        srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
        dest = tmp_path / "cap.ass"

        _build_caption_ass(srt_path, "1920x1080", dest)

        content = dest.read_text()
        assert "PlayResX: 1920" in content
        assert "PlayResY: 1080" in content

    def test_build_caption_ass_style_is_high_contrast_bottom_center(self, tmp_path):
        srt_path = tmp_path / "cap.srt"
        srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
        dest = tmp_path / "cap.ass"

        _build_caption_ass(srt_path, "1080x1920", dest)

        content = dest.read_text()
        assert "&H00FFFFFF" in content  # white fill, opaque
        assert "&H00000000" in content  # black outline, opaque
        style_line = next(l for l in content.splitlines() if l.startswith("Style: Default"))
        fields = style_line.split(",")
        assert fields[-5] == "2"  # Alignment: bottom-center

    def test_build_caption_ass_dialogue_lines_present(self, tmp_path):
        srt_path = tmp_path / "cap.srt"
        srt_path.write_text("1\n00:00:00,046 --> 00:00:01,602\nThere is a passage\n")
        dest = tmp_path / "cap.ass"

        _build_caption_ass(srt_path, "1080x1920", dest)

        content = dest.read_text()
        assert "Dialogue: 0,0:00:00.04,0:00:01.60,Default,,0,0,0,,There is a passage" in content

    def test_build_caption_ass_returns_false_for_missing_file(self, tmp_path):
        ok = _build_caption_ass(tmp_path / "does_not_exist.srt", "1080x1920", tmp_path / "out.ass")
        assert ok is False
        assert not (tmp_path / "out.ass").exists()

    def test_build_caption_ass_returns_false_for_empty_srt(self, tmp_path):
        srt_path = tmp_path / "empty.srt"
        srt_path.write_text("")
        ok = _build_caption_ass(srt_path, "1080x1920", tmp_path / "out.ass")
        assert ok is False

    def test_build_subtitles_filter_references_ass_path(self, tmp_path):
        ass_path = tmp_path / "captions" / "sh_short_001.ass"
        filt = _build_subtitles_filter(ass_path)
        assert filt.startswith("subtitles=filename='")
        assert str(ass_path) in filt

    def test_build_subtitles_filter_escapes_colon_in_path(self):
        # Windows-style drive-letter colon, or any literal colon in a path,
        # must be backslash-escaped per ffmpeg's filtergraph syntax.
        filt = _build_subtitles_filter(Path("C:/videos/cap.ass"))
        assert "C\\:/videos/cap.ass" in filt

    def test_short_render_burns_captions_when_srt_provided(self, tmp_path):
        """End-to-end command construction: a Short with srt_path produces a
        subtitles filter chained onto the video output; no subtitle filter
        appears when srt_path is omitted.

        Patches the libass capability probe True — this test is about filter
        CONSTRUCTION, not about the 2026-08-22 capability GATE itself (see
        TestSubtitleFilterCapabilityGate for that dedicated coverage)."""
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")
        srt_path = tmp_path / "cap.srt"
        srt_path.write_text("1\n00:00:00,000 --> 00:00:04,000\nHello world\n")
        entry = _make_entry(duration_seconds=4.0, clip_duration_seconds=4.0, clip_path=clip_path)

        with patch("hypogeum.agents.video_renderer._probe_subtitle_filter_available", return_value=True):
            cmd_with = _build_ffmpeg_concat_command(
                timeline=[entry], narration_path=None, music_track=None,
                output_path=tmp_path / "out.mp4", resolution=SHORT_RESOLUTION,
                episode_id="ep_test", is_short=True, srt_path=srt_path,
            )
        cmd_without = _build_ffmpeg_concat_command(
            timeline=[entry], narration_path=None, music_track=None,
            output_path=tmp_path / "out2.mp4", resolution=SHORT_RESOLUTION,
            episode_id="ep_test", is_short=True, srt_path=None,
        )

        cmd_with_str = " ".join(str(c) for c in cmd_with)
        cmd_without_str = " ".join(str(c) for c in cmd_without)
        assert "subtitles=filename=" in cmd_with_str
        assert "[vsub]" in cmd_with_str
        assert "subtitles=filename=" not in cmd_without_str
        # The .ass sidecar file must actually be written alongside the .srt
        assert (tmp_path / "cap.ass").exists()


class TestSubtitleFilterCapabilityGate:
    """
    2026-08-22 fix: an ffmpeg build without libass must never be allowed to
    silently produce a caption-less Short render. See the forensic audit
    (docs/operations/audits/EP_F3619022_ASSET_CONTINUITY_AND_BURNED_CAPTIONS_AUDIT_2026-08-22.md)
    this closes — ep_f3619022's 3 Shorts rendered successfully (exit 0,
    valid output) with zero burned-in caption pixels and no error anywhere.
    """

    def test_assert_raises_when_probe_reports_no_libass(self):
        with patch("hypogeum.agents.video_renderer._probe_subtitle_filter_available", return_value=False):
            with pytest.raises(SubtitleFilterUnavailableError):
                assert_subtitle_filter_available("ffmpeg")

    def test_assert_passes_silently_when_probe_reports_libass(self):
        with patch("hypogeum.agents.video_renderer._probe_subtitle_filter_available", return_value=True):
            assert_subtitle_filter_available("ffmpeg")  # must not raise

    def test_build_command_raises_before_any_filter_construction_when_libass_missing(self, tmp_path):
        """The gate must fire the moment srt_path is supplied -- before
        _build_caption_ass() ever writes a file or the filtergraph is built."""
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")
        srt_path = tmp_path / "cap.srt"
        srt_path.write_text("1\n00:00:00,000 --> 00:00:04,000\nHello world\n")
        entry = _make_entry(duration_seconds=4.0, clip_duration_seconds=4.0, clip_path=clip_path)

        with patch("hypogeum.agents.video_renderer._probe_subtitle_filter_available", return_value=False):
            with pytest.raises(SubtitleFilterUnavailableError):
                _build_ffmpeg_concat_command(
                    timeline=[entry], narration_path=None, music_track=None,
                    output_path=tmp_path / "out.mp4", resolution=SHORT_RESOLUTION,
                    episode_id="ep_test", is_short=True, srt_path=srt_path,
                )
        # The gate fired before _build_caption_ass() ever ran.
        assert not (tmp_path / "cap.ass").exists()

    def test_no_srt_path_never_probes_capability_at_all(self, tmp_path):
        """A Short with no srt_path (no captions required for it) must never
        even consult the capability probe -- proves the gate is scoped
        exactly to when captions were actually requested."""
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"FAKEVIDEO")
        entry = _make_entry(duration_seconds=4.0, clip_duration_seconds=4.0, clip_path=clip_path)

        with patch("hypogeum.agents.video_renderer._probe_subtitle_filter_available") as mock_probe:
            _build_ffmpeg_concat_command(
                timeline=[entry], narration_path=None, music_track=None,
                output_path=tmp_path / "out.mp4", resolution=SHORT_RESOLUTION,
                episode_id="ep_test", is_short=True, srt_path=None,
            )
        mock_probe.assert_not_called()


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

    def test_narration_music_combine_uses_duration_longest_not_first(self, tmp_path):
        """
        Regression for the ep_f3619022 body-to-end-page audio gap: with the
        current production default (music_tail_seconds=0, i.e. no outro
        pause), the [narr][msc]amix combine step must use duration=longest,
        never duration=first.

        Root cause: narration's atrim endpoint (narr_dur) is the TIMELINE's
        planned beat-duration sum, not a guarantee about the real stitched
        narration file's actual length -- TTS output can legitimately come
        in a little short. atrim cannot manufacture missing samples, so a
        short real narration file ends inside the filtergraph before
        narr_dur is reached. duration=first ties the WHOLE mix's length to
        that (possibly-truncated) first input, silencing music too for the
        remainder -- an audio gap exactly at the body's tail, i.e. exactly
        at the seam into the end page. Music is always looped via aloop to
        cover the full body_dur regardless of the source track's own
        length, so duration=longest lets the mix keep going on music alone
        for whatever narration didn't reach, with no change to video
        timing and no fade/pad of any kind.
        """
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
        fc = cmd[cmd.index("-filter_complex") + 1]
        narr_msc_step = next(part for part in fc.split(";") if "[narr][msc]amix" in part)
        assert "duration=longest" in narr_msc_step, narr_msc_step
        assert "duration=first" not in narr_msc_step, narr_msc_step

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
# TestNoAudioGapAtBodyTail — real-ffmpeg regression for the ep_f3619022
# body-to-end-page audio gap.
#
# ep_f3619022's real narration audio (main_narration.mp3) measured
# 743.220227s while the rendered body's video duration measured
# 744.060229s -- narration ran ~0.84s short of the timeline's planned beat
# duration, because a stitched TTS narration file's real length is not
# guaranteed to hit its planned duration exactly. With the old
# duration=first combine, that shortfall silenced the WHOLE mix (music
# included) for the remainder, producing a genuine, measurable audio gap
# at the exact seam into the end page (confirmed via ffprobe: the output's
# audio stream was measurably shorter than its video stream). These tests
# run real ffmpeg (no mocking) to prove the fix closes that specific gap
# without touching video timing.
# ---------------------------------------------------------------------------

def _make_real_tone(path: Path, duration: float, frequency: int = 440) -> None:
    """A real, audible (non-silent) audio file via ffmpeg's lavfi sine source."""
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration={duration}",
         "-c:a", "libmp3lame", str(path)],
        capture_output=True, timeout=30,
    )


def _probe_stream_duration(path: Path, stream_type: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream_type,
         "-show_entries", "stream=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    return float(result.stdout.strip().splitlines()[0])


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="real ffmpeg required")
class TestNoAudioGapAtBodyTail:

    def test_short_narration_no_longer_truncates_music_or_leaves_a_gap(self, tmp_path):
        """
        Reproduces the ep_f3619022 shortfall proportionally: a real 3.0s
        narration recording against a 4.0s timeline-planned body duration
        (a ~25% shortfall, comparable to ep_f3619022's ~0.84s/744.06s
        ratio), with real (audible) music covering the full body. Asserts
        the rendered output's audio stream duration exactly matches its
        video stream duration -- i.e. no gap, real ffmpeg, real files.
        """
        narration_path = tmp_path / "narration.mp3"
        _make_real_tone(narration_path, duration=3.0, frequency=440)
        music_path = tmp_path / "music.mp3"
        _make_real_tone(music_path, duration=10.0, frequency=220)
        music_track = MusicTrackResult(
            track_id="m", category="Mystery", asset_id="channel/music/mystery/m",
            local_path=music_path, source="ai_generated", duration_seconds=10.0,
        )
        entry = _make_entry(duration_seconds=4.0)  # no clip_path -> black-video body of 4.0s

        output_path = tmp_path / "out.mp4"
        cmd = _build_ffmpeg_concat_command(
            timeline=[entry],
            narration_path=narration_path,
            music_track=music_track,
            output_path=output_path,
            resolution="1920x1080",
            episode_id="ep_gap_regress",
        )
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr[-2000:]

        video_dur = _probe_stream_duration(output_path, "v")
        audio_dur = _probe_stream_duration(output_path, "a")
        assert audio_dur == pytest.approx(video_dur, abs=0.05), (
            f"audio stream ({audio_dur}s) does not cover the full video "
            f"stream ({video_dur}s) -- this is the ep_f3619022 body-to-"
            f"end-page audio gap: narration ran short and silenced the "
            f"whole mix for the remainder instead of continuing on music"
        )
        assert audio_dur == pytest.approx(4.0, abs=0.05)

    def test_no_shortfall_case_is_unaffected(self, tmp_path):
        """When real narration content already covers the full planned
        duration (the common/healthy case, no drift), duration=longest
        must produce byte-identical timing to the old duration=first --
        both inputs already reach body_dur, so the combine mode choice is
        a no-op here. Locks in 'preserve existing behaviour everywhere
        else' for the fix."""
        narration_path = tmp_path / "narration.mp3"
        _make_real_tone(narration_path, duration=4.0, frequency=440)  # matches target exactly
        music_path = tmp_path / "music.mp3"
        _make_real_tone(music_path, duration=10.0, frequency=220)
        music_track = MusicTrackResult(
            track_id="m", category="Mystery", asset_id="channel/music/mystery/m",
            local_path=music_path, source="ai_generated", duration_seconds=10.0,
        )
        entry = _make_entry(duration_seconds=4.0)

        output_path = tmp_path / "out.mp4"
        cmd = _build_ffmpeg_concat_command(
            timeline=[entry],
            narration_path=narration_path,
            music_track=music_track,
            output_path=output_path,
            resolution="1920x1080",
            episode_id="ep_no_shortfall",
        )
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr[-2000:]

        video_dur = _probe_stream_duration(output_path, "v")
        audio_dur = _probe_stream_duration(output_path, "a")
        assert audio_dur == pytest.approx(video_dur, abs=0.05)
        assert audio_dur == pytest.approx(4.0, abs=0.05)


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
        """When the bounded stretch branch applies (factor within
        MAX_EDITORIAL_SPEED_FACTOR), the setpts branch receives [sv0] input
        directly (no split/reverse/loop)."""
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"FAKEVIDEO")
        entry = _make_entry(duration_seconds=8.8, clip_duration_seconds=8.0, clip_path=clip)
        cmd = _build_ffmpeg_concat_command(
            timeline=[entry], narration_path=None, music_track=None,
            output_path=tmp_path / "out.mp4", resolution="1920x1080", episode_id="ep_t",
        )
        fc = self._fc(cmd)
        # factor=1.1 (within limit) → bounded setpts branch chains directly off [sv0]
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


# ---------------------------------------------------------------------------
# Short-native hard duration backstop
# ---------------------------------------------------------------------------

class TestShortNativeDurationCap:
    """A short-native (vertical) deliverable is trimmed to stay under a
    minute no matter how long the narration ran."""

    def _run(self, tmp_path, monkeypatch, *, total_beat_seconds, vertical):
        from hypogeum.agents import video_renderer as vr

        clip = tmp_path / "c.mp4"; clip.write_bytes(b"FAKEVIDEO")
        # beats of <=7s each (fully covered by an 8s clip), summing to ~total
        each = 7.0
        n = max(1, round(total_beat_seconds / each))
        each = total_beat_seconds / n
        timeline = [_make_entry(scene_index=i, scene_id=f"sc_{i}",
                                duration_seconds=each, clip_duration_seconds=8.0,
                                clip_path=clip) for i in range(n)]

        seen = {}
        monkeypatch.setattr(vr, "_run_ffmpeg",
                            lambda **kw: (kw["output_path"].write_bytes(b"OUT") or True))
        def _fake_trim(*, input_path, target_duration, output_path, **kw):
            seen["target"] = target_duration
            output_path.write_bytes(b"TRIMMED")
            return True
        monkeypatch.setattr(vr, "_trim_video_to_duration", _fake_trim)
        monkeypatch.setattr(vr, "_probe_output_duration", lambda p: seen.get("target", 0.0))

        # probe of the raw body: pretend it came out at the full narration length
        import subprocess as _sp
        class _R:
            returncode = 0
            stdout = ('{"streams":[{"codec_type":"video","duration":"%f"},'
                      '{"codec_type":"audio","duration":"%f"}]}' % (total_beat_seconds, total_beat_seconds))
        monkeypatch.setattr(_sp, "run", lambda *a, **k: _R())

        vr._render_main_episode(
            timeline=timeline, narration_path=None, music_track=None,
            output_path=tmp_path / "main.mp4", episode_id="ep_t",
            timeout=60, log=_NullLog(), vertical=vertical,
        )
        return seen["target"]

    def test_vertical_over_a_minute_is_capped(self, tmp_path, monkeypatch):
        target = self._run(tmp_path, monkeypatch, total_beat_seconds=72.0, vertical=True)
        assert target == vr_SHORT_MAX()
        assert target < 60.0

    def test_vertical_under_the_cap_is_untouched(self, tmp_path, monkeypatch):
        target = self._run(tmp_path, monkeypatch, total_beat_seconds=38.0, vertical=True)
        assert target == pytest.approx(38.0 + _music_tail())

    def test_landscape_is_never_capped(self, tmp_path, monkeypatch):
        # a 90s landscape body stays 90s (+ tail); the cap only applies vertical
        target = self._run(tmp_path, monkeypatch, total_beat_seconds=90.0, vertical=False)
        assert target == pytest.approx(90.0 + _music_tail())


class _NullLog:
    def __getattr__(self, _): return lambda *a, **k: None


def vr_SHORT_MAX():
    from hypogeum.agents.video_renderer import SHORT_NATIVE_HARD_MAX_SECONDS
    return SHORT_NATIVE_HARD_MAX_SECONDS


def _music_tail():
    from hypogeum.agents.video_renderer import _MUSIC_TAIL_SECONDS
    return _MUSIC_TAIL_SECONDS
