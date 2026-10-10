"""tests/test_coverage_auto_stretch.py — a barely-short beat is slowed down instead of sinking the episode."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hypogeum.agents.coverage_check import (
    MAX_AUTO_STRETCH_FACTOR, MAX_EDITORIAL_SPEED_FACTOR, _probe_duration_seconds, check_timeline_coverage,
    stretch_short_clips,
)
from hypogeum.agents.timeline_builder import TimelineEntry

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _clip(path: Path, seconds: float) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=270x480:rate=30:duration={seconds}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def _entry(clip: Path, narration: float) -> TimelineEntry:
    return TimelineEntry(
        scene_index=0, scene_id="s16", source_type="main_episode", short_id=None, clip_path=clip, narration_path=None,
        start_seconds=0.0, duration_seconds=narration, text_overlays=[], music_section=None, text_safe_zone=None,
        clip_duration_seconds=_probe_duration_seconds(clip),
    )


def _run(tmp_path, clip_s, narration_s, max_deficit=1.5):
    clip = _clip(tmp_path / "sc_s16.mp4", clip_s)
    entry = _entry(clip, narration_s)
    cov = check_timeline_coverage([entry])
    done = stretch_short_clips([entry], cov.insufficient_beats, max_deficit_seconds=max_deficit)
    return clip, entry, cov, done


def test_a_beat_short_by_under_a_second_and_a_half_is_slowed_down_until_it_is_sufficient(tmp_path):
    # the real case: clip 3.8s, narration ~5.155s -> deficit 0.975s after the 1.10x speed-up
    clip, entry, cov, done = _run(tmp_path, 3.8, 5.155)
    assert 0 < cov.insufficient_beats[0].deficit_seconds < 1.5
    assert done == ["s16"]
    assert _probe_duration_seconds(clip) > 3.8 and entry.clip_duration_seconds == pytest.approx(_probe_duration_seconds(clip))
    assert (tmp_path / "sc_s16.pre_stretch.mp4").exists()                       # the original is kept
    assert _probe_duration_seconds(tmp_path / "sc_s16.pre_stretch.mp4") == pytest.approx(3.8, abs=0.1)
    assert check_timeline_coverage([entry]).sufficient is True


def test_a_beat_short_by_1_5_seconds_or_more_is_left_alone(tmp_path):
    clip, entry, cov, done = _run(tmp_path, 3.8, 6.2)                           # deficit ~2.0s
    assert cov.insufficient_beats[0].deficit_seconds >= 1.5
    assert done == [] and _probe_duration_seconds(clip) == pytest.approx(3.8, abs=0.1)
    assert not (tmp_path / "sc_s16.pre_stretch.mp4").exists()


def test_disabled_with_zero(tmp_path):
    clip, entry, cov, done = _run(tmp_path, 3.8, 5.155, max_deficit=0)
    assert done == [] and _probe_duration_seconds(clip) == pytest.approx(3.8, abs=0.1)


def test_never_slows_one_clip_more_than_the_cap(tmp_path):
    # 1.2s clip needing ~2.0s: deficit under 1.5 but the slow-down would be ~1.7x
    clip, entry, cov, done = _run(tmp_path, 1.2, 2.5, max_deficit=1.5)
    assert cov.insufficient_beats and cov.insufficient_beats[0].deficit_seconds < 1.5
    assert (2.5 / MAX_EDITORIAL_SPEED_FACTOR) / 1.2 > MAX_AUTO_STRETCH_FACTOR
    assert done == []
