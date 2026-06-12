"""
tests/test_timeline_builder.py — Unit tests for hypogeum/agents/timeline_builder.py

Tests cover the public API surface:
  TestBuildTimelines           — build_timelines() end-to-end
  TestSumTimelineDuration      — sum_timeline_duration()
  TestComputeSectionDurations  — compute_section_durations()
  TestFindClipAssetId          — find_clip_asset_id()
  TestDeriveAssetId            — derive_asset_id()
  TestDeriveMusiacCategory     — _derive_music_category_for_scene()
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.timeline_builder import (
    TimelineEntry,
    TimelinePlan,
    build_timelines,
    compute_section_durations,
    derive_asset_id,
    find_clip_asset_id,
    sum_timeline_duration,
    _derive_music_category_for_scene,
    _probe_clip_duration,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_scene(
    scene_id="sc_001",
    scene_index=0,
    source_type="main_episode",
    short_id=None,
    narration_reference="b_001",
    clip_duration=8,
    narrative_signals=None,
    asset_id=None,
    text_safe_zone=None,
    text_overlays=None,
):
    s = {
        "scene_id": scene_id,
        "scene_index": scene_index,
        "source_type": source_type,
        "short_id": short_id,
        "narration_reference": narration_reference,
        "production_spec": {"duration_seconds": clip_duration},
        "narrative_signals": narrative_signals or [],
        "text_overlays": text_overlays or [],
    }
    if asset_id:
        s["asset_id"] = asset_id
    if text_safe_zone:
        s["text_safe_zone"] = text_safe_zone
    return s


def _make_segment(beat_ref="b_001", duration=4.5, audio_asset_id="episodes/ep_001/narration/b_001"):
    return {
        "beat_reference": beat_ref,
        "duration_seconds": duration,
        "audio_asset_id": audio_asset_id,
        "generation_status": "generated",
    }


def _make_entry(
    scene_index=0,
    scene_id="sc_001",
    duration_seconds=4.5,
    clip_duration_seconds=8.0,
    clip_path=None,
    narration_path=None,
):
    return TimelineEntry(
        scene_index=scene_index,
        scene_id=scene_id,
        source_type="main_episode",
        short_id=None,
        clip_path=clip_path,
        narration_path=narration_path,
        start_seconds=0.0,
        duration_seconds=duration_seconds,
        text_overlays=[],
        music_section=None,
        text_safe_zone=None,
        clip_duration_seconds=clip_duration_seconds,
    )


# ---------------------------------------------------------------------------
# TestBuildTimelines
# ---------------------------------------------------------------------------

class TestBuildTimelines:

    def test_returns_timeline_plan(self, tmp_path):
        """build_timelines() returns a TimelinePlan instance."""
        scenes = [_make_scene()]
        segments = [_make_segment()]
        plan = build_timelines(scenes, segments, {}, tmp_path, "ep_001")
        assert isinstance(plan, TimelinePlan)

    def test_main_timeline_populated(self, tmp_path):
        """main_episode scenes appear in main_timeline."""
        scenes = [_make_scene(source_type="main_episode")]
        segments = [_make_segment()]
        plan = build_timelines(scenes, segments, {}, tmp_path, "ep_001")
        assert len(plan.main_timeline) == 1
        assert plan.main_timeline[0].scene_id == "sc_001"

    def test_short_scenes_go_to_shorts_timelines(self, tmp_path):
        """short scenes appear in shorts_timelines keyed by short_id."""
        scenes = [_make_scene(source_type="short", short_id="sh_001", scene_id="sc_sh1")]
        segments = [_make_segment()]
        plan = build_timelines(scenes, segments, {}, tmp_path, "ep_001")
        assert "sh_001" in plan.shorts_timelines
        assert len(plan.shorts_timelines["sh_001"]) == 1

    def test_short_ids_sorted(self, tmp_path):
        """short_ids list is sorted."""
        scenes = [
            _make_scene(source_type="short", short_id="sh_003", scene_id="sc_a", narration_reference="b_003"),
            _make_scene(source_type="short", short_id="sh_001", scene_id="sc_b", narration_reference="b_001"),
            _make_scene(source_type="short", short_id="sh_002", scene_id="sc_c", narration_reference="b_002"),
        ]
        segments = [
            _make_segment("b_001"), _make_segment("b_002"), _make_segment("b_003"),
        ]
        plan = build_timelines(scenes, segments, {}, tmp_path, "ep_001")
        assert plan.short_ids == ["sh_001", "sh_002", "sh_003"]

    def test_narration_anchored_duration(self, tmp_path):
        """Scene duration equals narration beat duration when beat exists."""
        scenes = [_make_scene(narration_reference="b_001", clip_duration=8)]
        segments = [_make_segment("b_001", duration=4.5)]
        plan = build_timelines(scenes, segments, {}, tmp_path, "ep_001")
        assert plan.main_timeline[0].duration_seconds == pytest.approx(4.5)

    def test_clip_duration_fallback_when_no_beat(self, tmp_path):
        """Scene duration falls back to clip_duration when beat not found."""
        scenes = [_make_scene(narration_reference="b_missing", clip_duration=5)]
        segments = []  # no matching beat
        plan = build_timelines(scenes, segments, {}, tmp_path, "ep_001")
        assert plan.main_timeline[0].duration_seconds == pytest.approx(5.0)

    def test_scene_ordering_by_index(self, tmp_path):
        """Entries in main_timeline are ordered by scene_index regardless of input order."""
        scenes = [
            _make_scene(scene_id="sc_002", scene_index=1, narration_reference="b_002"),
            _make_scene(scene_id="sc_001", scene_index=0, narration_reference="b_001"),
        ]
        segments = [
            _make_segment("b_001", 4.0),
            _make_segment("b_002", 5.0),
        ]
        plan = build_timelines(scenes, segments, {}, tmp_path, "ep_001")
        assert plan.main_timeline[0].scene_index == 0
        assert plan.main_timeline[1].scene_index == 1

    def test_clip_path_none_when_file_missing(self, tmp_path):
        """clip_path is None when the asset file does not exist on disk."""
        scenes = [_make_scene(asset_id="episodes/ep_001/video/sc_001")]
        segments = [_make_segment()]
        plan = build_timelines(scenes, segments, {}, tmp_path, "ep_001")
        assert plan.main_timeline[0].clip_path is None

    def test_clip_path_set_when_file_exists(self, tmp_path):
        """clip_path is set when the asset file exists on disk."""
        asset_path = tmp_path / "episodes" / "ep_001" / "video" / "sc_001.mp4"
        asset_path.parent.mkdir(parents=True)
        asset_path.write_bytes(b"FAKEVIDEO")
        asset_lookup = {"episodes/ep_001/video/sc_001": asset_path}
        scenes = [_make_scene(asset_id="episodes/ep_001/video/sc_001")]
        segments = [_make_segment()]
        plan = build_timelines(scenes, segments, asset_lookup, tmp_path, "ep_001")
        assert plan.main_timeline[0].clip_path == asset_path

    def test_narration_path_none_when_file_missing(self, tmp_path):
        """narration_path is None when the audio asset file does not exist."""
        scenes = [_make_scene(narration_reference="b_001")]
        segments = [_make_segment("b_001", audio_asset_id="episodes/ep_001/narration/b_001")]
        plan = build_timelines(scenes, segments, {}, tmp_path, "ep_001")
        assert plan.main_timeline[0].narration_path is None

    def test_text_safe_zone_propagated_for_short(self, tmp_path):
        """text_safe_zone from scene is included in Short timeline entry."""
        scenes = [
            _make_scene(
                source_type="short", short_id="sh_001", scene_id="sc_sh",
                text_safe_zone="bottom",
            )
        ]
        segments = [_make_segment()]
        plan = build_timelines(scenes, segments, {}, tmp_path, "ep_001")
        assert plan.shorts_timelines["sh_001"][0].text_safe_zone == "bottom"

    def test_start_seconds_accumulates(self, tmp_path):
        """start_seconds of each entry equals sum of prior entry durations."""
        scenes = [
            _make_scene(scene_id="sc_001", scene_index=0, narration_reference="b_001"),
            _make_scene(scene_id="sc_002", scene_index=1, narration_reference="b_002"),
        ]
        segments = [_make_segment("b_001", 4.0), _make_segment("b_002", 6.0)]
        plan = build_timelines(scenes, segments, {}, tmp_path, "ep_001")
        assert plan.main_timeline[0].start_seconds == pytest.approx(0.0)
        assert plan.main_timeline[1].start_seconds == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# TestSumTimelineDuration
# ---------------------------------------------------------------------------

class TestSumTimelineDuration:

    def test_empty_timeline_returns_zero(self):
        assert sum_timeline_duration([]) == pytest.approx(0.0)

    def test_single_entry(self):
        assert sum_timeline_duration([_make_entry(duration_seconds=4.5)]) == pytest.approx(4.5)

    def test_multiple_entries_summed(self):
        entries = [
            _make_entry(scene_index=0, duration_seconds=4.5),
            _make_entry(scene_index=1, duration_seconds=5.5),
        ]
        assert sum_timeline_duration(entries) == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# TestComputeSectionDurations
# ---------------------------------------------------------------------------

class TestComputeSectionDurations:

    def test_main_episode_key_present(self):
        main = [_make_entry(duration_seconds=40.0)]
        result = compute_section_durations(main, {}, [])
        assert "main_episode" in result

    def test_main_floor_is_30_seconds(self):
        """Even if main timeline is very short, floor is 30.0s."""
        main = [_make_entry(duration_seconds=5.0)]
        result = compute_section_durations(main, {}, [])
        assert result["main_episode"] == pytest.approx(30.0)

    def test_main_uses_actual_duration_when_above_floor(self):
        main = [_make_entry(duration_seconds=60.0)]
        result = compute_section_durations(main, {}, [])
        assert result["main_episode"] == pytest.approx(60.0)

    def test_short_keys_added_for_each_short_id(self):
        short_entry = _make_entry(duration_seconds=15.0)
        shorts = {"sh_001": [short_entry]}
        result = compute_section_durations([], shorts, ["sh_001"])
        assert "short_sh_001" in result

    def test_short_floor_is_10_seconds(self):
        """Shorts floor is 10.0s."""
        short_entry = _make_entry(duration_seconds=3.0)
        shorts = {"sh_001": [short_entry]}
        result = compute_section_durations([], shorts, ["sh_001"])
        assert result["short_sh_001"] == pytest.approx(10.0)

    def test_short_uses_actual_duration_when_above_floor(self):
        short_entry = _make_entry(duration_seconds=25.0)
        shorts = {"sh_001": [short_entry]}
        result = compute_section_durations([], shorts, ["sh_001"])
        assert result["short_sh_001"] == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# TestFindClipAssetId
# ---------------------------------------------------------------------------

class TestFindClipAssetId:

    def test_explicit_asset_id_takes_precedence(self, tmp_path):
        """Explicit scene.asset_id is returned without any lookup."""
        scene = {"scene_id": "s01", "source_type": "main_episode", "short_id": None,
                 "asset_id": "episodes/ep_001/video/sc_s01_custom"}
        result = find_clip_asset_id(scene, {}, tmp_path, "ep_001")
        assert result == "episodes/ep_001/video/sc_s01_custom"

    def test_lookup_by_scene_id_in_asset_lookup_keys(self, tmp_path):
        """When scene_id substring matches an asset_lookup key, that key is returned."""
        scene = {"scene_id": "sc_001", "source_type": "main_episode", "short_id": None}
        asset_lookup = {"episodes/ep_001/video/sc_001": Path("/fake/path.mp4")}
        result = find_clip_asset_id(scene, asset_lookup, tmp_path, "ep_001")
        assert result == "episodes/ep_001/video/sc_001"

    def test_derives_main_scene_conventional_path(self, tmp_path):
        """Without explicit asset_id or lookup match, derives episodes/<ep>/video/sc_<scene_id>."""
        scene = {"scene_id": "s01", "source_type": "main_episode", "short_id": None}
        result = find_clip_asset_id(scene, {}, tmp_path, "ep_001")
        assert result == "episodes/ep_001/video/sc_s01"

    def test_derives_short_scene_conventional_path(self, tmp_path):
        """Short scene without asset_id derives episodes/<ep>/video/sh_<short_id>/sc_<scene_id>."""
        scene = {"scene_id": "s01", "source_type": "short", "short_id": "sh_000"}
        result = find_clip_asset_id(scene, {}, tmp_path, "ep_001")
        assert result == "episodes/ep_001/video/sh_sh_000/sc_s01"


# ---------------------------------------------------------------------------
# TestDeriveAssetId
# ---------------------------------------------------------------------------

class TestDeriveAssetId:

    def test_main_scene_path(self):
        scene = {"scene_id": "sc_001"}
        result = derive_asset_id(scene, "ep_001")
        assert result == "episodes/ep_001/video/sc_sc_001"

    def test_short_scene_path(self):
        scene = {"scene_id": "sc_001", "short_id": "sh_001"}
        result = derive_asset_id(scene, "ep_001")
        assert result == "episodes/ep_001/video/sh_sh_001/sc_sc_001"

    def test_missing_scene_id_defaults_to_unknown(self):
        scene = {}
        result = derive_asset_id(scene, "ep_001")
        assert result == "episodes/ep_001/video/sc_unknown"


# ---------------------------------------------------------------------------
# TestDeriveMusiacCategory
# ---------------------------------------------------------------------------

class TestDeriveMusiacCategory:

    def test_mystery_deepening_maps_to_mystery(self):
        scene = {"narrative_signals": [{"signal_type": "mystery_deepening"}]}
        assert _derive_music_category_for_scene(scene) == "Mystery"

    def test_shock_fact_maps_to_tension(self):
        scene = {"narrative_signals": [{"signal_type": "shock_fact"}]}
        assert _derive_music_category_for_scene(scene) == "Tension"

    def test_reveal_maps_to_resolution(self):
        scene = {"narrative_signals": [{"signal_type": "reveal"}]}
        assert _derive_music_category_for_scene(scene) == "Resolution"

    def test_discovery_maps_to_discovery(self):
        scene = {"narrative_signals": [{"signal_type": "discovery"}]}
        assert _derive_music_category_for_scene(scene) == "Discovery"

    def test_tension_maps_to_tension(self):
        scene = {"narrative_signals": [{"signal_type": "tension"}]}
        assert _derive_music_category_for_scene(scene) == "Tension"

    def test_foreshadowing_maps_to_mystery(self):
        scene = {"narrative_signals": [{"signal_type": "foreshadowing"}]}
        assert _derive_music_category_for_scene(scene) == "Mystery"

    def test_investigation_maps_to_investigation(self):
        scene = {"narrative_signals": [{"signal_type": "investigation"}]}
        assert _derive_music_category_for_scene(scene) == "Investigation"

    def test_unknown_signal_defaults_to_mystery(self):
        scene = {"narrative_signals": [{"signal_type": "unknown_signal_type"}]}
        assert _derive_music_category_for_scene(scene) == "Mystery"

    def test_empty_signals_defaults_to_mystery(self):
        scene = {"narrative_signals": []}
        assert _derive_music_category_for_scene(scene) == "Mystery"

    def test_no_signals_key_defaults_to_mystery(self):
        scene = {}
        assert _derive_music_category_for_scene(scene) == "Mystery"

    def test_string_signal_type_accepted(self):
        """Signal as plain string (not dict) is also handled."""
        scene = {"narrative_signals": ["reveal"]}
        assert _derive_music_category_for_scene(scene) == "Resolution"

    def test_only_first_signal_used(self):
        """When multiple signals are present, only the first determines the category."""
        scene = {"narrative_signals": [
            {"signal_type": "reveal"},
            {"signal_type": "tension"},
        ]}
        assert _derive_music_category_for_scene(scene) == "Resolution"


# ---------------------------------------------------------------------------
# TestProbeClipDuration
# ---------------------------------------------------------------------------

_PROBE_TARGET = "hypogeum.agents.timeline_builder.subprocess.run"


class TestProbeClipDuration:
    """Unit tests for _probe_clip_duration() in isolation."""

    def test_returns_probed_duration_when_file_exists(self, tmp_path):
        """Returns ffprobe-reported duration when file exists and probe succeeds."""
        clip = tmp_path / "sc_001.mp4"
        clip.write_bytes(b"fake")
        with patch(_PROBE_TARGET) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="10.041667\n")
            result = _probe_clip_duration(clip, fallback=8.0)
        assert result == pytest.approx(10.041667)

    def test_falls_back_when_file_missing(self, tmp_path):
        """Returns fallback when clip_path does not exist on disk."""
        missing = tmp_path / "nonexistent.mp4"
        result = _probe_clip_duration(missing, fallback=8.0)
        assert result == pytest.approx(8.0)

    def test_falls_back_when_clip_path_is_none(self):
        """Returns fallback when clip_path is None."""
        result = _probe_clip_duration(None, fallback=5.0)
        assert result == pytest.approx(5.0)

    def test_falls_back_on_nonzero_returncode(self, tmp_path):
        """Returns fallback when ffprobe exits non-zero (e.g. unreadable file)."""
        clip = tmp_path / "sc_001.mp4"
        clip.write_bytes(b"fake")
        with patch(_PROBE_TARGET) as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = _probe_clip_duration(clip, fallback=8.0)
        assert result == pytest.approx(8.0)

    def test_falls_back_on_subprocess_exception(self, tmp_path):
        """Returns fallback when subprocess.run raises (e.g. ffprobe not installed)."""
        clip = tmp_path / "sc_001.mp4"
        clip.write_bytes(b"fake")
        with patch(_PROBE_TARGET, side_effect=FileNotFoundError("ffprobe not found")):
            result = _probe_clip_duration(clip, fallback=8.0)
        assert result == pytest.approx(8.0)

    def test_falls_back_on_unparseable_output(self, tmp_path):
        """Returns fallback when ffprobe stdout cannot be parsed as float."""
        clip = tmp_path / "sc_001.mp4"
        clip.write_bytes(b"fake")
        with patch(_PROBE_TARGET) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not-a-number\n")
            result = _probe_clip_duration(clip, fallback=8.0)
        assert result == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# TestClipDurationFromFile
# ---------------------------------------------------------------------------

class TestClipDurationFromFile:
    """
    Regression tests: clip_duration_seconds on TimelineEntry must reflect the
    actual delivered file duration, not the production_spec requested duration.
    """

    def _make_clip(self, tmp_path, asset_id="episodes/ep_001/video/sc_001"):
        """Write a stub file and return (path, asset_lookup)."""
        parts = asset_id.split("/")
        p = tmp_path.joinpath(*parts).with_suffix(".mp4")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"fake")
        return p, {asset_id: p}

    def test_8s_spec_10s_file_uses_10s(self, tmp_path):
        """8s spec + file that probes as 10s → clip_duration_seconds=10s."""
        _, asset_lookup = self._make_clip(tmp_path)
        scenes = [_make_scene(asset_id="episodes/ep_001/video/sc_001", clip_duration=8)]
        segments = [_make_segment("b_001", duration=22.605)]

        with patch(_PROBE_TARGET) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="10.041667\n")
            plan = build_timelines(scenes, segments, asset_lookup, tmp_path, "ep_001")

        entry = plan.main_timeline[0]
        assert entry.clip_duration_seconds == pytest.approx(10.041667)

    def test_hold_calculated_from_actual_file_duration(self, tmp_path):
        """
        Renderer hold = duration_seconds - clip_duration_seconds.
        When file is 10s and narration is 22.605s, hold must be 12.564s, not 14.605s.
        """
        _, asset_lookup = self._make_clip(tmp_path)
        scenes = [_make_scene(asset_id="episodes/ep_001/video/sc_001", clip_duration=8)]
        segments = [_make_segment("b_001", duration=22.605)]

        with patch(_PROBE_TARGET) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="10.041667\n")
            plan = build_timelines(scenes, segments, asset_lookup, tmp_path, "ep_001")

        entry = plan.main_timeline[0]
        hold = entry.duration_seconds - entry.clip_duration_seconds
        assert hold == pytest.approx(22.605 - 10.041667, abs=1e-4)
        # Confirm the old (wrong) hold of 14.605s is NOT produced
        assert abs(hold - 14.605) > 1.0

    def test_matching_spec_and_file_duration_unchanged(self, tmp_path):
        """When spec duration matches file duration, behavior is identical to before."""
        _, asset_lookup = self._make_clip(tmp_path)
        scenes = [_make_scene(asset_id="episodes/ep_001/video/sc_001", clip_duration=8)]
        segments = [_make_segment("b_001", duration=10.0)]

        with patch(_PROBE_TARGET) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="8.0\n")
            plan = build_timelines(scenes, segments, asset_lookup, tmp_path, "ep_001")

        entry = plan.main_timeline[0]
        assert entry.clip_duration_seconds == pytest.approx(8.0)
        assert entry.duration_seconds == pytest.approx(10.0)  # still narration-anchored

    def test_no_file_falls_back_to_spec(self, tmp_path):
        """When clip does not exist on disk, clip_duration_seconds equals production_spec."""
        scenes = [_make_scene(clip_duration=8)]
        segments = [_make_segment("b_001", duration=15.0)]

        plan = build_timelines(scenes, segments, {}, tmp_path, "ep_001")

        entry = plan.main_timeline[0]
        assert entry.clip_duration_seconds == pytest.approx(8.0)

    def test_probe_failure_falls_back_to_spec(self, tmp_path):
        """When ffprobe fails, clip_duration_seconds falls back to production_spec."""
        _, asset_lookup = self._make_clip(tmp_path)
        scenes = [_make_scene(asset_id="episodes/ep_001/video/sc_001", clip_duration=8)]
        segments = [_make_segment("b_001", duration=15.0)]

        with patch(_PROBE_TARGET) as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            plan = build_timelines(scenes, segments, asset_lookup, tmp_path, "ep_001")

        entry = plan.main_timeline[0]
        assert entry.clip_duration_seconds == pytest.approx(8.0)

    def test_fallback_duration_used_for_scene_duration_when_no_beat(self, tmp_path):
        """
        When narration beat is missing AND file probe returns actual duration,
        scene_duration uses the probed clip duration (not spec).
        """
        _, asset_lookup = self._make_clip(tmp_path)
        scenes = [_make_scene(
            asset_id="episodes/ep_001/video/sc_001",
            narration_reference="b_missing",
            clip_duration=8,
        )]
        segments = []  # no matching beat

        with patch(_PROBE_TARGET) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="10.5\n")
            plan = build_timelines(scenes, segments, asset_lookup, tmp_path, "ep_001")

        entry = plan.main_timeline[0]
        # No beat → fallback to clip_duration; clip_duration came from probe → 10.5
        assert entry.duration_seconds == pytest.approx(10.5)
        assert entry.clip_duration_seconds == pytest.approx(10.5)
