"""
tests/test_end_page.py — Unit tests for end-page card append in video_renderer.py

Tests:
  TestEndPageAppend
    - _append_end_page() calls ffmpeg with the end-page PNG as input
    - end-page duration is controlled by the duration argument
    - _append_end_page() succeeds on two clean ffmpeg runs and returns True
    - _append_end_page() restores body on end-page clip failure and returns False
    - _append_end_page() restores body on concat failure and returns False
    - render_episode() triggers end-page append when end_page_path is set
    - render_episode() skips end-page append when end_page_path is None
    - render_episode() skips end-page append when end_page_path does not exist
    - render_episode() skips end-page append when main render failed
    - end-page append is NOT applied to shorts
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.video_renderer import (
    FRAME_RATE,
    MAIN_RESOLUTION,
    SHORT_RESOLUTION,
    _MUSIC_TAIL_SECONDS,
    _append_end_page,
    _build_ffmpeg_concat_command,
    render_episode,
)
from hypogeum.agents.timeline_builder import TimelineEntry, TimelinePlan
from hypogeum.elevenlabs import MusicTrackResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_log():
    log = MagicMock()
    log.info = MagicMock()
    log.error = MagicMock()
    log.warn = MagicMock()
    return log


def _make_entry(scene_id="sc_001", source_type="main_episode", duration_seconds=4.5) -> TimelineEntry:
    return TimelineEntry(
        scene_index=0,
        scene_id=scene_id,
        source_type=source_type,
        short_id=None,
        clip_path=None,
        narration_path=None,
        start_seconds=0.0,
        duration_seconds=duration_seconds,
        text_overlays=[],
        music_section=None,
        text_safe_zone=None,
        clip_duration_seconds=8.0,
    )


def _make_plan(main_entries=None, shorts=None) -> TimelinePlan:
    main = main_entries or [_make_entry()]
    s = shorts or {}
    return TimelinePlan(main_timeline=main, shorts_timelines=s, short_ids=sorted(s.keys()))


def _ffmpeg_success_side_effect(cmd, capture_output, text, timeout):
    """Write a fake mp4 to the last path argument so existence checks pass."""
    out = Path(cmd[-1])
    if str(cmd[-1]) != "-":      # "-" is a stdout/null sink (blackdetect, luminance probes), never a real output file
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"FAKEMP4" * 500)
    r = MagicMock()
    r.returncode = 0
    r.stderr = ""
    r.stdout = ""
    return r


def _make_music_track(tmp_path: Path, name: str = "music") -> MusicTrackResult:
    music_path = tmp_path / f"{name}.mp3"
    music_path.write_bytes(b"\xff\xfb" * 100)
    return MusicTrackResult(
        track_id=name,
        category="Mystery",
        asset_id=f"channel/music/mystery/{name}",
        local_path=music_path,
        source="ai_generated",
        duration_seconds=120.0,
    )


def _spy_ffmpeg_recording(calls_recorded):
    """subprocess.run side_effect that records every call and writes a fake
    output file to the last argument so downstream existence checks pass."""
    def _side_effect(cmd, capture_output, text, timeout):
        calls_recorded.append(list(cmd))
        out = Path(cmd[-1])
        if str(cmd[-1]) != "-":      # "-" is a stdout/null sink (blackdetect, luminance probes), never a real output file
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"FAKEMP4" * 500)
        r = MagicMock()
        r.returncode = 0
        r.stderr = ""
        r.stdout = ""
        return r
    return _side_effect


# ---------------------------------------------------------------------------
# TestEndPageAppend — _append_end_page() unit tests
# ---------------------------------------------------------------------------

class TestEndPageAppend:

    def test_ffmpeg_called_with_end_page_png_as_input(self, tmp_path):
        """_append_end_page must pass the end-page PNG path as an ffmpeg input."""
        episode_dir = tmp_path / "ep_001"
        episode_dir.mkdir()

        main_mp4 = episode_dir / "main_ep_001.mp4"
        main_mp4.write_bytes(b"FAKEMP4" * 500)
        end_page_png = tmp_path / "end-page.png"
        end_page_png.write_bytes(b"FAKEPNG")

        calls_recorded = []

        def spy_ffmpeg(cmd, capture_output, text, timeout):
            calls_recorded.append(list(cmd))
            out = Path(cmd[-1])
            if str(cmd[-1]) != "-":      # "-" is a stdout/null sink (blackdetect, luminance probes), never a real output file
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"FAKEMP4" * 500)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            return r

        log = _mock_log()
        with patch("subprocess.run", side_effect=spy_ffmpeg):
            result = _append_end_page(
                main_output_path=main_mp4,
                end_page_path=end_page_png,
                duration=20.0,
                episode_id="ep_001",
                timeout=60,
                log=log,
            )

        assert result is True
        # At least one ffmpeg call must mention the end-page PNG
        png_mentioned = any(
            str(end_page_png) in " ".join(c) for c in calls_recorded
        )
        assert png_mentioned, f"End-page PNG not found in any ffmpeg call: {calls_recorded}"

    def test_duration_passed_to_ffmpeg_as_t_flag(self, tmp_path):
        """The duration argument must appear as a -t flag in the end-page clip command."""
        episode_dir = tmp_path / "ep_dur"
        episode_dir.mkdir()

        main_mp4 = episode_dir / "main_ep_dur.mp4"
        main_mp4.write_bytes(b"FAKEMP4" * 500)
        end_page_png = tmp_path / "end-page.png"
        end_page_png.write_bytes(b"FAKEPNG")

        calls_recorded = []

        def spy_ffmpeg(cmd, capture_output, text, timeout):
            calls_recorded.append(list(cmd))
            out = Path(cmd[-1])
            if str(cmd[-1]) != "-":      # "-" is a stdout/null sink (blackdetect, luminance probes), never a real output file
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"FAKEMP4" * 500)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            return r

        log = _mock_log()
        with patch("subprocess.run", side_effect=spy_ffmpeg):
            _append_end_page(
                main_output_path=main_mp4,
                end_page_path=end_page_png,
                duration=25.0,
                episode_id="ep_dur",
                timeout=60,
                log=log,
            )

        # The clip-generation call must include -t 25.000
        clip_call = next(
            c for c in calls_recorded
            if str(end_page_png) in " ".join(c)
        )
        assert "-t" in clip_call
        t_idx = clip_call.index("-t")
        # There may be a second -t; find the one right after the PNG input
        assert "25.000" in clip_call[t_idx + 1], (
            f"Expected -t 25.000 in clip call, got {clip_call}"
        )

    def test_returns_true_on_success(self, tmp_path):
        """_append_end_page() returns True when both ffmpeg passes succeed."""
        episode_dir = tmp_path / "ep_ok"
        episode_dir.mkdir()

        main_mp4 = episode_dir / "main_ep_ok.mp4"
        main_mp4.write_bytes(b"FAKEMP4" * 500)
        end_page_png = tmp_path / "end-page.png"
        end_page_png.write_bytes(b"FAKEPNG")

        log = _mock_log()
        with patch("subprocess.run", side_effect=_ffmpeg_success_side_effect):
            result = _append_end_page(
                main_output_path=main_mp4,
                end_page_path=end_page_png,
                duration=20.0,
                episode_id="ep_ok",
                timeout=60,
                log=log,
            )

        assert result is True

    def test_returns_false_and_restores_body_when_clip_fails(self, tmp_path):
        """When end-page clip creation fails, returns False and restores body.mp4."""
        episode_dir = tmp_path / "ep_clipfail"
        episode_dir.mkdir()

        main_mp4 = episode_dir / "main_ep_clipfail.mp4"
        main_mp4.write_bytes(b"ORIGINAL_CONTENT")
        end_page_png = tmp_path / "end-page.png"
        end_page_png.write_bytes(b"FAKEPNG")

        def _clip_fails(cmd, capture_output, text, timeout):
            # First call (clip generation): fail. Write nothing.
            r = MagicMock()
            r.returncode = 1
            r.stderr = "codec error"
            r.stdout = ""
            return r

        log = _mock_log()
        with patch("subprocess.run", side_effect=_clip_fails):
            result = _append_end_page(
                main_output_path=main_mp4,
                end_page_path=end_page_png,
                duration=20.0,
                episode_id="ep_clipfail",
                timeout=60,
                log=log,
            )

        assert result is False
        # Body must be restored as main_mp4
        assert main_mp4.exists(), "Body must be restored when clip creation fails"
        assert main_mp4.read_bytes() == b"ORIGINAL_CONTENT"

    def test_returns_false_and_restores_body_when_concat_fails(self, tmp_path):
        """When concat step fails, returns False and body is restored as main.mp4."""
        episode_dir = tmp_path / "ep_concatfail"
        episode_dir.mkdir()

        main_mp4 = episode_dir / "main_ep_concatfail.mp4"
        main_mp4.write_bytes(b"ORIGINAL_BODY")
        end_page_png = tmp_path / "end-page.png"
        end_page_png.write_bytes(b"FAKEPNG")

        call_count = [0]

        def _concat_fails(cmd, capture_output, text, timeout):
            call_count[0] += 1
            r = MagicMock()
            if call_count[0] == 1:
                # First call = clip creation → succeed
                out = Path(cmd[-1])
                if str(cmd[-1]) != "-":      # "-" is a stdout/null sink (blackdetect, luminance probes), never a real output file
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(b"FAKEMP4" * 100)
                r.returncode = 0
            else:
                # Second call = concat → fail
                r.returncode = 1
                r.stderr = "concat error"
            r.stdout = ""
            return r

        log = _mock_log()
        with patch("subprocess.run", side_effect=_concat_fails):
            result = _append_end_page(
                main_output_path=main_mp4,
                end_page_path=end_page_png,
                duration=20.0,
                episode_id="ep_concatfail",
                timeout=60,
                log=log,
            )

        assert result is False
        assert main_mp4.exists(), "Body must be restored when concat fails"
        assert main_mp4.read_bytes() == b"ORIGINAL_BODY"

    def _spy_run(self, calls_recorded):
        def spy_ffmpeg(cmd, capture_output, text, timeout):
            calls_recorded.append(list(cmd))
            out = Path(cmd[-1])
            if str(cmd[-1]) != "-":      # "-" is a stdout/null sink (blackdetect, luminance probes), never a real output file
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"FAKEMP4" * 500)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            return r
        return spy_ffmpeg

    def test_end_page_clip_defaults_to_landscape_resolution(self, tmp_path):
        ep = tmp_path / "ep_land"; ep.mkdir()
        (ep / "main_ep_land.mp4").write_bytes(b"FAKEMP4" * 500)
        png = tmp_path / "end-page.png"; png.write_bytes(b"FAKEPNG")
        calls: list = []
        with patch("subprocess.run", side_effect=self._spy_run(calls)):
            assert _append_end_page(
                main_output_path=ep / "main_ep_land.mp4", end_page_path=png,
                duration=20.0, episode_id="ep_land", timeout=60, log=_mock_log(),
            ) is True
        joined = " ".join(" ".join(c) for c in calls)
        assert f"scale={MAIN_RESOLUTION.replace('x', ':')}" in joined

    def test_end_page_clip_uses_portrait_resolution_for_short_native(self, tmp_path):
        """The end-page clip must be built at the body's resolution — a
        SHORT_NATIVE vertical main deliverable is 1080x1920, and a landscape
        end-page clip fails the filter_complex concat ('Input link parameters
        ... do not match')."""
        ep = tmp_path / "ep_vert"; ep.mkdir()
        (ep / "main_ep_vert.mp4").write_bytes(b"FAKEMP4" * 500)
        png = tmp_path / "end-page.png"; png.write_bytes(b"FAKEPNG")
        calls: list = []
        with patch("subprocess.run", side_effect=self._spy_run(calls)):
            assert _append_end_page(
                main_output_path=ep / "main_ep_vert.mp4", end_page_path=png,
                duration=20.0, episode_id="ep_vert", timeout=60, log=_mock_log(),
                resolution=SHORT_RESOLUTION,
            ) is True
        joined = " ".join(" ".join(c) for c in calls)
        assert f"scale={SHORT_RESOLUTION.replace('x', ':')}" in joined
        assert f"pad={SHORT_RESOLUTION.replace('x', ':')}" in joined
        assert "scale=1920:1080" not in joined


# ---------------------------------------------------------------------------
# TestRenderEpisodeEndPage — render_episode() integration
# ---------------------------------------------------------------------------

class TestRenderEpisodeEndPage:

    def _run_render(self, tmp_path, end_page_path=None, end_page_duration=20.0,
                    ffmpeg_side_effect=None):
        episode_dir = tmp_path / "ep_ep"
        episode_dir.mkdir(exist_ok=True)
        plan = _make_plan()
        log = _mock_log()

        side_effect = ffmpeg_side_effect or _ffmpeg_success_side_effect
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
                episode_id="ep_ep",
                ffmpeg_timeout=60,
                log=log,
                end_page_path=end_page_path,
                end_page_duration=end_page_duration,
            ), log

    def test_end_page_appended_when_path_set_and_exists(self, tmp_path):
        """render_episode() calls _append_end_page when end_page_path exists."""
        end_page_png = tmp_path / "end-page.png"
        end_page_png.write_bytes(b"FAKEPNG")

        with patch(
            "hypogeum.agents.video_renderer._append_end_page",
            return_value=True,
        ) as mock_append:
            self._run_render(tmp_path, end_page_path=end_page_png)

        mock_append.assert_called_once()

    def test_end_page_duration_passed_to_append(self, tmp_path):
        """The configured duration is forwarded to _append_end_page."""
        end_page_png = tmp_path / "end-page.png"
        end_page_png.write_bytes(b"FAKEPNG")

        with patch(
            "hypogeum.agents.video_renderer._append_end_page",
            return_value=True,
        ) as mock_append:
            self._run_render(tmp_path, end_page_path=end_page_png, end_page_duration=30.0)

        _, kwargs = mock_append.call_args
        assert kwargs.get("duration") == 30.0 or mock_append.call_args[0][2] == 30.0

    def test_end_page_skipped_when_path_is_none(self, tmp_path):
        """render_episode() does NOT call _append_end_page when end_page_path=None."""
        with patch(
            "hypogeum.agents.video_renderer._append_end_page",
        ) as mock_append:
            self._run_render(tmp_path, end_page_path=None)

        mock_append.assert_not_called()

    def test_end_page_skipped_when_file_does_not_exist(self, tmp_path):
        """render_episode() does NOT call _append_end_page when the PNG is absent."""
        missing = tmp_path / "no-such-file.png"
        assert not missing.exists()

        with patch(
            "hypogeum.agents.video_renderer._append_end_page",
        ) as mock_append:
            self._run_render(tmp_path, end_page_path=missing)

        mock_append.assert_not_called()

    def test_end_page_not_applied_when_main_render_failed(self, tmp_path):
        """When main episode render fails, _append_end_page must not be called."""
        end_page_png = tmp_path / "end-page.png"
        end_page_png.write_bytes(b"FAKEPNG")

        def _main_fails(cmd, capture_output, text, timeout):
            # Never write output → existence check fails → render_status = failed
            r = MagicMock()
            r.returncode = 1
            r.stderr = "render error"
            r.stdout = ""
            return r

        with patch(
            "hypogeum.agents.video_renderer._append_end_page",
        ) as mock_append:
            self._run_render(
                tmp_path,
                end_page_path=end_page_png,
                ffmpeg_side_effect=_main_fails,
            )

        mock_append.assert_not_called()

    def test_end_page_not_applied_to_shorts(self, tmp_path):
        """End page must never be passed to short video renders."""
        end_page_png = tmp_path / "end-page.png"
        end_page_png.write_bytes(b"FAKEPNG")

        episode_dir = tmp_path / "ep_short_ep"
        episode_dir.mkdir()

        short_entry = _make_entry(scene_id="sc_sh", source_type="short")
        plan = _make_plan(
            main_entries=[_make_entry()],
            shorts={"sh_001": [short_entry]},
        )
        log = _mock_log()

        short_render_calls = []

        def spy_ffmpeg(cmd, capture_output, text, timeout):
            if "short" in str(cmd[-1]):
                short_render_calls.append(list(cmd))
            out = Path(cmd[-1])
            if str(cmd[-1]) != "-":      # "-" is a stdout/null sink (blackdetect, luminance probes), never a real output file
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"FAKEMP4" * 500)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            return r

        with patch("subprocess.run", side_effect=spy_ffmpeg), \
             patch(
                 "hypogeum.agents.video_renderer._append_end_page",
                 return_value=True,
             ) as mock_append:
            render_episode(
                timeline_plan=plan,
                main_narration_path=None,
                short_narration_paths={},
                music_tracks={},
                clip_availability_rate=1.0,
                missing_clips=[],
                sections_without_music=[],
                episode_dir=episode_dir,
                episode_id="ep_short_ep",
                ffmpeg_timeout=60,
                log=log,
                end_page_path=end_page_png,
                end_page_duration=20.0,
            )

        # _append_end_page called exactly once (for main), never for a short
        mock_append.assert_called_once()
        call_args = mock_append.call_args
        # Confirm the call was for the main output, not a short path
        main_out_arg = call_args[1].get("main_output_path") or call_args[0][0]
        # Main path starts with "main_"; short paths start with "short_<index>_"
        assert str(main_out_arg.name).startswith("main_")


# ---------------------------------------------------------------------------
# TestEndPageMusicContinuity — regression coverage for the 2026-08-01 fix:
# the final shot froze on a static frame, and the end page then resumed the
# music at the wrong position (raw body_duration % usable_dur instead of
# elapsed-since-segment-start % usable_dur), producing an audible jump under
# Option B multi-segment music. Root cause: _append_end_page computed the
# loop offset from t=0 of the whole body, but under multi-segment music the
# segment actually playing at the end of the body did not start at t=0.
# ---------------------------------------------------------------------------

class TestEndPageMusicContinuity:

    def test_no_freeze_tail_by_default(self):
        """_MUSIC_TAIL_SECONDS must be 0 — no static-frame hold before the end
        page. A non-zero value here reintroduces the freeze-frame regression."""
        assert _MUSIC_TAIL_SECONDS == 0.0

    def test_body_has_no_tpad_hold_with_default_tail(self, tmp_path):
        """With the default (zero) music tail, the body's video filtergraph
        must not hold the last frame — no [vbody]tpad=stop_mode=clone stage,
        and no silence padding on the narration track."""
        narration_path = tmp_path / "narration.mp3"
        narration_path.write_bytes(b"\xff\xfb" * 100)
        entry = TimelineEntry(
            scene_index=0, scene_id="sc_001", source_type="main_episode",
            short_id=None, clip_path=None, narration_path=None,
            start_seconds=0.0, duration_seconds=4.5, text_overlays=[],
            music_section=None, text_safe_zone=None, clip_duration_seconds=8.0,
        )
        cmd = _build_ffmpeg_concat_command(
            timeline=[entry], narration_path=narration_path, music_track=None,
            output_path=tmp_path / "out.mp4", resolution=MAIN_RESOLUTION,
            episode_id="ep_001", music_tail_seconds=_MUSIC_TAIL_SECONDS,
        )
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "tpad=stop_mode=clone:stop_duration=" not in fc
        assert "apad=pad_dur=" not in fc

    def test_legacy_single_track_offset_uses_raw_body_duration(self, tmp_path):
        """Legacy single-track music plays from t=0 of the body (implicit
        segment start 0.0) — the offset formula must reduce to the original
        body_duration % usable_dur when music_segment_start_seconds is left
        at its default."""
        episode_dir = tmp_path / "ep_legacy"
        episode_dir.mkdir()
        main_mp4 = episode_dir / "main_ep_legacy.mp4"
        main_mp4.write_bytes(b"FAKEMP4" * 500)
        end_page_png = tmp_path / "end-page.png"
        end_page_png.write_bytes(b"FAKEPNG")
        track = _make_music_track(tmp_path)

        calls = []
        log = _mock_log()
        with patch("subprocess.run", side_effect=_spy_ffmpeg_recording(calls)), \
             patch(
                 "hypogeum.agents.video_renderer._probe_music_usable_duration",
                 return_value=100.0,
             ):
            result = _append_end_page(
                main_output_path=main_mp4,
                end_page_path=end_page_png,
                duration=20.0,
                episode_id="ep_legacy",
                timeout=60,
                log=log,
                music_track=track,
                body_duration=125.0,
                # music_segment_start_seconds defaults to 0.0
            )

        assert result is True
        clip_call = next(c for c in calls if str(track.local_path) in c and "-filter_complex" in c)
        ss_idx = clip_call.index("-ss")
        # 125.0 % 100.0 == 25.0
        assert float(clip_call[ss_idx + 1]) == pytest.approx(25.0)

    def test_multi_segment_offset_is_relative_to_segment_start(self, tmp_path):
        """The core regression: under Option B multi-segment music, the last
        segment playing at the end of the body did not start at t=0 of the
        body. The offset must be computed from elapsed time since that
        segment's own start, not raw body_duration -- otherwise the end page
        resumes the same track at the wrong position (an audible jump)."""
        episode_dir = tmp_path / "ep_multi"
        episode_dir.mkdir()
        main_mp4 = episode_dir / "main_ep_multi.mp4"
        main_mp4.write_bytes(b"FAKEMP4" * 500)
        end_page_png = tmp_path / "end-page.png"
        end_page_png.write_bytes(b"FAKEPNG")
        track = _make_music_track(tmp_path, name="last_segment_track")

        calls = []
        log = _mock_log()
        with patch("subprocess.run", side_effect=_spy_ffmpeg_recording(calls)), \
             patch(
                 "hypogeum.agents.video_renderer._probe_music_usable_duration",
                 return_value=120.0,
             ):
            result = _append_end_page(
                main_output_path=main_mp4,
                end_page_path=end_page_png,
                duration=20.0,
                episode_id="ep_multi",
                timeout=60,
                log=log,
                music_track=track,
                body_duration=702.5,
                music_segment_start_seconds=500.0,
            )

        assert result is True
        clip_call = next(c for c in calls if str(track.local_path) in c and "-filter_complex" in c)
        ss_idx = clip_call.index("-ss")
        # Correct: (702.5 - 500.0) % 120.0 == 82.5
        # Buggy (pre-fix) formula would give 702.5 % 120.0 == 102.5 instead --
        # a 20s jump to a different position in the same track.
        offset = float(clip_call[ss_idx + 1])
        assert offset == pytest.approx(82.5)
        assert offset != pytest.approx(102.5)

    def test_end_page_reuses_same_track_no_second_input(self, tmp_path):
        """The end page must continue the SAME music file, not introduce a
        second/different track -- exactly one -i for the track's path."""
        episode_dir = tmp_path / "ep_same"
        episode_dir.mkdir()
        main_mp4 = episode_dir / "main_ep_same.mp4"
        main_mp4.write_bytes(b"FAKEMP4" * 500)
        end_page_png = tmp_path / "end-page.png"
        end_page_png.write_bytes(b"FAKEPNG")
        track = _make_music_track(tmp_path)

        calls = []
        log = _mock_log()
        with patch("subprocess.run", side_effect=_spy_ffmpeg_recording(calls)), \
             patch(
                 "hypogeum.agents.video_renderer._probe_music_usable_duration",
                 return_value=100.0,
             ):
            _append_end_page(
                main_output_path=main_mp4,
                end_page_path=end_page_png,
                duration=20.0,
                episode_id="ep_same",
                timeout=60,
                log=log,
                music_track=track,
                body_duration=125.0,
            )

        clip_call = next(c for c in calls if str(track.local_path) in c and "-filter_complex" in c)
        assert clip_call.count(str(track.local_path)) == 1
        assert clip_call.count("-ss") == 1

    def test_end_page_video_is_a_clean_cut_with_and_without_music(self, tmp_path):
        """Current contract (5ed5200 + video_renderer's "clean cut everywhere"
        policy): the end-page VIDEO is a hard cut — scale+pad to [vout] with
        NO fade=t=in — whether or not music is present. Only the music gets a
        fade (afade=t=out), handled independently."""
        episode_dir = tmp_path / "ep_fade"
        episode_dir.mkdir()
        end_page_png = tmp_path / "end-page.png"
        end_page_png.write_bytes(b"FAKEPNG")
        track = _make_music_track(tmp_path)

        for with_music in (True, False):
            main_mp4 = episode_dir / f"main_{with_music}.mp4"
            main_mp4.write_bytes(b"FAKEMP4" * 500)
            calls = []
            log = _mock_log()
            with patch("subprocess.run", side_effect=_spy_ffmpeg_recording(calls)), \
                 patch(
                     "hypogeum.agents.video_renderer._probe_music_usable_duration",
                     return_value=100.0,
                 ):
                _append_end_page(
                    main_output_path=main_mp4,
                    end_page_path=end_page_png,
                    duration=20.0,
                    episode_id="ep_fade",
                    timeout=60,
                    log=log,
                    music_track=track if with_music else None,
                    body_duration=125.0 if with_music else 0.0,
                )
            clip_call = next(c for c in calls if str(end_page_png) in c and "-filter_complex" in c)
            fc = clip_call[clip_call.index("-filter_complex") + 1]
            assert "fade=t=in" not in fc, fc          # video is a clean cut
            assert "pad=" in fc and "[vout]" in fc     # scaled + padded still
            if with_music:
                assert "afade=t=out" in fc, fc         # music fades independently

    def test_render_episode_passes_last_segment_start_to_end_page(self, tmp_path):
        """render_episode() must resolve the LAST multi-segment track's own
        start_seconds and forward it as music_segment_start_seconds -- this
        is what _append_end_page needs to compute the correct offset."""
        episode_dir = tmp_path / "ep_wire"
        episode_dir.mkdir()
        plan = _make_plan()
        log = _mock_log()
        seg_a = _make_music_track(tmp_path, name="seg_a")
        seg_b = _make_music_track(tmp_path, name="seg_b")
        music_segments = [
            {"start_seconds": 0.0, "end_seconds": 40.0, "track": seg_a},
            {"start_seconds": 40.0, "end_seconds": 90.0, "track": seg_b},
        ]
        end_page_png = tmp_path / "end-page.png"
        end_page_png.write_bytes(b"FAKEPNG")

        with patch("subprocess.run", side_effect=_ffmpeg_success_side_effect), \
             patch(
                 "hypogeum.agents.video_renderer._append_end_page",
                 return_value=True,
             ) as mock_append:
            render_episode(
                timeline_plan=plan,
                main_narration_path=None,
                short_narration_paths={},
                music_tracks={},
                clip_availability_rate=1.0,
                missing_clips=[],
                sections_without_music=[],
                episode_dir=episode_dir,
                episode_id="ep_wire",
                ffmpeg_timeout=60,
                log=log,
                end_page_path=end_page_png,
                end_page_duration=20.0,
                music_segments=music_segments,
            )

        _, kwargs = mock_append.call_args
        assert kwargs.get("music_segment_start_seconds") == 40.0
        assert kwargs.get("music_track") is seg_b
