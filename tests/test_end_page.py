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
    _append_end_page,
    render_episode,
)
from hypogeum.agents.timeline_builder import TimelineEntry, TimelinePlan


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
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"FAKEMP4" * 500)
    r = MagicMock()
    r.returncode = 0
    r.stderr = ""
    r.stdout = ""
    return r


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
