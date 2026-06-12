"""
tests/test_timeline_qa.py — Unit tests for hypogeum/agents/timeline_qa.py

Test classes:
  TestCheckClipAspectRatio   — Check A: clips not in 16:9 trigger warnings
  TestCheckHoldDuration      — Check B: excessive hold triggers warn/fail
  TestCheckMissingNarration  — Check C: missing beat references in narration_package
  TestCheckNarrationCoverage — Check D: non-self-referential narration file coverage
  TestRunTimelineQaOverall   — overall_status aggregation, ep_smoke_01 scenario
  TestProbeClipDimensions    — _probe_clip_dimensions() unit tests
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.timeline_builder import TimelineEntry, TimelinePlan
from hypogeum.agents.timeline_qa import (
    _probe_clip_dimensions,
    run_timeline_qa,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_PROBE_DIM_TARGET = "hypogeum.agents.timeline_qa.subprocess.run"


def _make_entry(
    scene_id: str = "sc_001",
    source_type: str = "main_episode",
    duration_seconds: float = 22.605,
    clip_duration_seconds: float = 8.0,
    clip_path: Path | None = None,
    narration_path: Path | None = None,
    narration_reference: str = "b_001",
    short_id: str | None = None,
) -> TimelineEntry:
    return TimelineEntry(
        scene_index=0,
        scene_id=scene_id,
        source_type=source_type,
        short_id=short_id,
        clip_path=clip_path,
        narration_path=narration_path,
        start_seconds=0.0,
        duration_seconds=duration_seconds,
        text_overlays=[],
        music_section="Mystery",
        text_safe_zone=None,
        clip_duration_seconds=clip_duration_seconds,
        narration_reference=narration_reference,
    )


def _make_plan(main: list[TimelineEntry], shorts: dict | None = None) -> TimelinePlan:
    return TimelinePlan(
        main_timeline=main,
        shorts_timelines=shorts or {},
        short_ids=list((shorts or {}).keys()),
    )


def _make_narration_package(beat_refs: list[str]) -> dict:
    """Build a minimal narration_package with the given beat references."""
    return {
        "narration_segments": [
            {
                "beat_reference": ref,
                "audio_asset_id": f"episodes/ep_test/audio/narration_{ref}",
                "duration_seconds": 10.0,
            }
            for ref in beat_refs
        ]
    }


# ---------------------------------------------------------------------------
# TestCheckClipAspectRatio (Check A)
# ---------------------------------------------------------------------------

class TestCheckClipAspectRatio:

    def test_square_clip_triggers_warning(self, tmp_path):
        """960×960 square clip → aspect ratio warn (narration_reference="" to isolate check A)."""
        clip = tmp_path / "sc_001.mp4"
        clip.write_bytes(b"x")
        # narration_reference="" isolates check A from check D (no audio gap)
        entry = _make_entry(clip_path=clip, narration_reference="",
                            duration_seconds=8.0, clip_duration_seconds=8.0)
        plan = _make_plan([entry])

        with patch(_PROBE_DIM_TARGET) as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="width=960\nheight=960\n"
            )
            report = run_timeline_qa(plan, narration_package=_make_narration_package([]))

        assert report["overall_status"] == "warn"
        check = next(c for c in report["checks"] if c["check_id"] == "c_clip_aspect_ratio")
        assert check["status"] == "warn"
        assert len(report["clip_aspect_ratio_offenders"]) == 1
        offender = report["clip_aspect_ratio_offenders"][0]
        assert offender["scene_id"] == "sc_001"
        assert offender["resolution"] == "960x960"

    def test_16_9_clip_passes(self, tmp_path):
        """1280×720 clip → aspect ratio pass."""
        clip = tmp_path / "sc_001.mp4"
        clip.write_bytes(b"x")
        entry = _make_entry(clip_path=clip, duration_seconds=10.0, clip_duration_seconds=10.0)
        plan = _make_plan([entry])

        with patch(_PROBE_DIM_TARGET) as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="width=1280\nheight=720\n"
            )
            report = run_timeline_qa(plan, narration_package=_make_narration_package(["b_001"]))

        check = next(c for c in report["checks"] if c["check_id"] == "c_clip_aspect_ratio")
        assert check["status"] == "pass"
        assert report["clip_aspect_ratio_offenders"] == []

    def test_no_clip_files_triggers_warn(self):
        """Entries with clip_path=None → warn that no clips could be inspected."""
        entry = _make_entry(clip_path=None)
        plan = _make_plan([entry])
        report = run_timeline_qa(plan)
        check = next(c for c in report["checks"] if c["check_id"] == "c_clip_aspect_ratio")
        assert check["status"] == "warn"
        assert "no clip files" in check["detail"]

    def test_ffprobe_failure_counted_as_unprobed(self, tmp_path):
        """ffprobe non-zero returncode → entry counted as unprobed, not offender."""
        clip = tmp_path / "sc_001.mp4"
        clip.write_bytes(b"x")
        entry = _make_entry(clip_path=clip)
        plan = _make_plan([entry])

        with patch(_PROBE_DIM_TARGET) as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            report = run_timeline_qa(plan)

        assert report["clip_aspect_ratio_offenders"] == []
        check = next(c for c in report["checks"] if c["check_id"] == "c_clip_aspect_ratio")
        # No clips probed → warn (unverified), not fail
        assert check["status"] == "warn"

    def test_multiple_clips_all_square_all_reported(self, tmp_path):
        """Three square clips → all three in offenders list."""
        entries = []
        for i in range(3):
            clip = tmp_path / f"sc_{i:03d}.mp4"
            clip.write_bytes(b"x")
            entries.append(_make_entry(scene_id=f"sc_{i:03d}", clip_path=clip))
        plan = _make_plan(entries)

        with patch(_PROBE_DIM_TARGET) as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="width=960\nheight=960\n"
            )
            report = run_timeline_qa(plan)

        assert len(report["clip_aspect_ratio_offenders"]) == 3

    def test_warning_message_includes_resolution(self, tmp_path):
        """Warning string must contain resolution and expected ratio."""
        clip = tmp_path / "sc_001.mp4"
        clip.write_bytes(b"x")
        entry = _make_entry(clip_path=clip)
        plan = _make_plan([entry])

        with patch(_PROBE_DIM_TARGET) as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="width=960\nheight=960\n"
            )
            report = run_timeline_qa(plan)

        matching = [w for w in report["warnings"] if "clip_aspect_ratio" in w]
        assert matching, "Expected at least one clip_aspect_ratio warning"
        assert "960x960" in matching[0]


# ---------------------------------------------------------------------------
# TestCheckHoldDuration (Check B)
# ---------------------------------------------------------------------------

class TestCheckHoldDuration:

    def test_hold_below_threshold_passes(self):
        """hold_seconds = 5s → below 10s warn threshold → pass."""
        entry = _make_entry(duration_seconds=13.0, clip_duration_seconds=8.0)
        plan = _make_plan([entry])
        report = run_timeline_qa(plan, hold_warn_threshold_seconds=10.0)
        check = next(c for c in report["checks"] if c["check_id"] == "c_hold_duration")
        assert check["status"] == "pass"

    def test_hold_above_warn_threshold_warns(self):
        """hold_seconds = 14s (narration 22s − clip 8s) → exceeds 10s → warn."""
        entry = _make_entry(duration_seconds=22.605, clip_duration_seconds=8.0)
        plan = _make_plan([entry])
        report = run_timeline_qa(plan, hold_warn_threshold_seconds=10.0)
        check = next(c for c in report["checks"] if c["check_id"] == "c_hold_duration")
        assert check["status"] == "warn"
        assert len(report["hold_offenders"]) >= 1

    def test_hold_above_fail_threshold_fails(self):
        """hold_seconds = 25s → exceeds 20s fail threshold → fail."""
        entry = _make_entry(duration_seconds=33.0, clip_duration_seconds=8.0)
        plan = _make_plan([entry])
        report = run_timeline_qa(plan, hold_fail_threshold_seconds=20.0)
        check = next(c for c in report["checks"] if c["check_id"] == "c_hold_duration")
        assert check["status"] == "fail"
        assert report["overall_status"] == "fail"
        assert any("hold_duration" in f for f in report["failures"])

    def test_hold_offenders_sorted_by_worst_first(self):
        """Offenders list is sorted worst hold first."""
        e1 = _make_entry(scene_id="sc_001", duration_seconds=30.0, clip_duration_seconds=8.0)
        e2 = _make_entry(scene_id="sc_002", duration_seconds=20.0, clip_duration_seconds=8.0)
        e3 = _make_entry(scene_id="sc_003", duration_seconds=25.0, clip_duration_seconds=8.0)
        plan = _make_plan([e1, e2, e3])
        report = run_timeline_qa(plan, hold_warn_threshold_seconds=5.0)
        holds = [h["hold_seconds"] for h in report["hold_offenders"]]
        assert holds == sorted(holds, reverse=True)

    def test_hold_of_zero_not_in_offenders(self):
        """Entry where clip matches narration exactly → no offender."""
        entry = _make_entry(duration_seconds=8.0, clip_duration_seconds=8.0)
        plan = _make_plan([entry])
        report = run_timeline_qa(plan)
        assert report["hold_offenders"] == []

    def test_hold_report_contains_scene_id_and_seconds(self):
        """Offender dict includes scene_id, hold_seconds, duration_seconds, clip_duration_seconds."""
        entry = _make_entry(
            scene_id="sc_smoke_01",
            duration_seconds=22.605,
            clip_duration_seconds=8.0,
        )
        plan = _make_plan([entry])
        report = run_timeline_qa(plan, hold_warn_threshold_seconds=10.0)
        offender = report["hold_offenders"][0]
        assert offender["scene_id"] == "sc_smoke_01"
        assert offender["hold_seconds"] == pytest.approx(14.605, abs=0.01)
        assert offender["duration_seconds"] == pytest.approx(22.605, abs=0.01)
        assert offender["clip_duration_seconds"] == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# TestCheckMissingNarration (Check C)
# ---------------------------------------------------------------------------

class TestCheckMissingNarration:

    def test_all_refs_present_passes(self, tmp_path):
        """All beat references exist in narration_package → pass."""
        audio = tmp_path / "narration_b_001.mp3"
        audio.write_bytes(b"audio")
        entry = _make_entry(narration_reference="b_001", narration_path=audio)
        plan = _make_plan([entry])
        pkg = _make_narration_package(["b_001"])
        report = run_timeline_qa(plan, narration_package=pkg)
        check = next(c for c in report["checks"] if c["check_id"] == "c_missing_narration_refs")
        assert check["status"] == "pass"

    def test_ref_not_in_package_warns(self):
        """b_017 referenced in timeline but narration_package only has b_001 → warn."""
        entry = _make_entry(narration_reference="b_017", narration_path=None)
        plan = _make_plan([entry])
        pkg = _make_narration_package(["b_001", "b_002"])
        report = run_timeline_qa(plan, narration_package=pkg)
        check = next(c for c in report["checks"] if c["check_id"] == "c_missing_narration_refs")
        assert check["status"] == "warn"
        assert any("b_017" in w for w in report["warnings"])

    def test_ref_in_package_but_file_missing_warns(self, tmp_path):
        """Beat in narration_package but audio file does not exist → warn."""
        missing_path = tmp_path / "narration_b_001.mp3"
        # deliberately NOT writing the file
        entry = _make_entry(narration_reference="b_001", narration_path=missing_path)
        plan = _make_plan([entry])
        pkg = _make_narration_package(["b_001"])
        report = run_timeline_qa(plan, narration_package=pkg)
        check = next(c for c in report["checks"] if c["check_id"] == "c_missing_narration_refs")
        assert check["status"] == "warn"
        assert any("audio file missing" in w for w in report["warnings"])

    def test_no_narration_package_warns(self):
        """narration_package=None → check is skipped with a warning."""
        entry = _make_entry(narration_reference="b_001")
        plan = _make_plan([entry])
        report = run_timeline_qa(plan, narration_package=None)
        check = next(c for c in report["checks"] if c["check_id"] == "c_missing_narration_refs")
        assert check["status"] == "warn"
        assert "not provided" in check["detail"]

    def test_empty_narration_reference_not_flagged(self):
        """Scene with no narration_reference (silent scene by design) → no flag."""
        entry = _make_entry(narration_reference="", narration_path=None)
        plan = _make_plan([entry])
        pkg = _make_narration_package([])
        report = run_timeline_qa(plan, narration_package=pkg)
        check = next(c for c in report["checks"] if c["check_id"] == "c_missing_narration_refs")
        assert check["status"] == "pass"
        assert report["missing_narration_refs"] == []

    def test_warning_identifies_which_beats_are_missing(self):
        """Warning messages name the specific missing beat references."""
        e1 = _make_entry(scene_id="sc_001", narration_reference="b_015", narration_path=None)
        e2 = _make_entry(scene_id="sc_002", narration_reference="b_017", narration_path=None)
        plan = _make_plan([e1, e2])
        pkg = _make_narration_package(["b_001"])
        report = run_timeline_qa(plan, narration_package=pkg)
        missing_msgs = " ".join(report["warnings"])
        assert "b_015" in missing_msgs
        assert "b_017" in missing_msgs


# ---------------------------------------------------------------------------
# TestCheckNarrationCoverage (Check D)
# ---------------------------------------------------------------------------

class TestCheckNarrationCoverage:

    def test_all_narration_files_present_passes(self, tmp_path):
        """All entries have narration_path on disk → coverage pass."""
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"audio")
        entry = _make_entry(
            duration_seconds=10.0,
            narration_reference="b_001",
            narration_path=audio,
        )
        plan = _make_plan([entry])
        report = run_timeline_qa(plan, narration_package=_make_narration_package(["b_001"]))
        check = next(c for c in report["checks"] if c["check_id"] == "c_narration_coverage_real")
        assert check["status"] == "pass"

    def test_missing_narration_file_creates_gap(self):
        """Entry with narration_reference but narration_path=None → gap = entry duration."""
        entry = _make_entry(
            duration_seconds=8.0,
            narration_reference="b_017",
            narration_path=None,
        )
        plan = _make_plan([entry])
        pkg = _make_narration_package([])
        report = run_timeline_qa(plan, narration_package=pkg)
        assert report["narration_coverage"]["gap_seconds"] == pytest.approx(8.0)

    def test_gap_above_5s_fails(self):
        """narration gap > 5s → check D fails."""
        entry = _make_entry(
            duration_seconds=10.0,
            narration_reference="b_001",
            narration_path=None,
        )
        plan = _make_plan([entry])
        report = run_timeline_qa(plan)
        check = next(c for c in report["checks"] if c["check_id"] == "c_narration_coverage_real")
        assert check["status"] == "fail"
        assert report["overall_status"] == "fail"

    def test_small_gap_warns_not_fails(self, tmp_path):
        """narration gap > 0 but ≤ 5s → warn only."""
        audio = tmp_path / "b_001.mp3"
        audio.write_bytes(b"audio")
        e1 = _make_entry(
            scene_id="sc_001",
            duration_seconds=3.0,
            narration_reference="b_001",
            narration_path=audio,
        )
        e2 = _make_entry(
            scene_id="sc_002",
            duration_seconds=3.0,
            narration_reference="b_002",
            narration_path=None,  # file missing — creates 3.0s gap
        )
        plan = _make_plan([e1, e2])
        report = run_timeline_qa(plan)
        check = next(c for c in report["checks"] if c["check_id"] == "c_narration_coverage_real")
        assert check["status"] == "warn"
        assert report["narration_coverage"]["gap_seconds"] == pytest.approx(3.0)

    def test_non_self_referential_vs_old_approach(self, tmp_path):
        """Demonstrate the fix: timeline_total != audio_covered when file is missing.
        Old approach always reported narration=X covered=X (tautology).
        New approach: covered < total when narration_path is None."""
        audio = tmp_path / "b_001.mp3"
        audio.write_bytes(b"audio")
        e1 = _make_entry(scene_id="sc_001", duration_seconds=22.605,
                         narration_reference="b_001", narration_path=audio)
        e2 = _make_entry(scene_id="sc_002", duration_seconds=8.0,
                         narration_reference="b_017", narration_path=None)
        plan = _make_plan([e1, e2])

        report = run_timeline_qa(plan)
        coverage = report["narration_coverage"]

        # Expected = both entries = 30.605s
        assert coverage["expected_seconds"] == pytest.approx(30.605, abs=0.01)
        # Covered = only e1 = 22.605s (e2 has no audio file)
        assert coverage["covered_seconds"] == pytest.approx(22.605, abs=0.01)
        # Gap = 8.0s (e2 missing)
        assert coverage["gap_seconds"] == pytest.approx(8.0, abs=0.01)

    def test_silent_entries_list_populated(self):
        """silent_entries list contains entries with missing narration files."""
        entry = _make_entry(
            scene_id="sc_002",
            duration_seconds=8.0,
            narration_reference="b_017",
            narration_path=None,
        )
        plan = _make_plan([entry])
        report = run_timeline_qa(plan)
        assert len(report["silent_entries"]) == 1
        assert report["silent_entries"][0]["scene_id"] == "sc_002"
        assert report["silent_entries"][0]["narration_reference"] == "b_017"


# ---------------------------------------------------------------------------
# TestRunTimelineQaOverall
# ---------------------------------------------------------------------------

class TestRunTimelineQaOverall:

    def test_all_pass_returns_pass_status(self, tmp_path):
        """All four checks pass → overall_status=pass."""
        clip = tmp_path / "sc_001.mp4"
        clip.write_bytes(b"x")
        audio = tmp_path / "b_001.mp3"
        audio.write_bytes(b"audio")
        entry = _make_entry(
            duration_seconds=8.0,
            clip_duration_seconds=8.0,
            clip_path=clip,
            narration_reference="b_001",
            narration_path=audio,
        )
        plan = _make_plan([entry])
        pkg = _make_narration_package(["b_001"])

        with patch(_PROBE_DIM_TARGET) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="width=1280\nheight=720\n")
            report = run_timeline_qa(plan, narration_package=pkg)

        assert report["overall_status"] == "pass"
        assert report["warnings"] == []
        assert report["failures"] == []

    def test_warn_status_when_only_warnings(self):
        """Warnings but no failures → overall_status=warn.
        narration_reference="" so check D has no gap; hold 14.605s triggers check B warn."""
        entry = _make_entry(
            duration_seconds=22.605,
            clip_duration_seconds=8.0,
            clip_path=None,
            narration_reference="",   # no narration reference → check D sees no expected audio
        )
        plan = _make_plan([entry])
        report = run_timeline_qa(plan, hold_warn_threshold_seconds=10.0,
                                 narration_package=_make_narration_package([]))
        assert report["overall_status"] == "warn"
        assert report["failures"] == []

    def test_fail_status_when_any_failure(self):
        """Any failure → overall_status=fail regardless of warnings."""
        entry = _make_entry(
            duration_seconds=30.0,
            clip_duration_seconds=8.0,
            narration_reference="b_001",
            narration_path=None,
        )
        plan = _make_plan([entry])
        report = run_timeline_qa(
            plan, hold_fail_threshold_seconds=20.0
        )
        assert report["overall_status"] == "fail"

    def test_ep_smoke_01_scenario(self):
        """
        Reproduce ep_smoke_01 timeline conditions and verify all checks fire.

        Scene s01 (b_001): 22.605s narration, 10.042s clip (veo3 960×960 square)
          → hold = 12.563s → WARN (>10s threshold)
          → aspect ratio 960×960 → WARN (non-16:9)
          → narration file exists → OK on check C/D

        Scene s02 (b_017): 8.0s fallback, no narration file
          → b_017 not in narration_package (only b_001/b_002) → WARN check C
          → narration_path=None → contributes to gap, WARN/FAIL check D
        """
        import tempfile, os

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)

            clip_s01 = tmp / "sc_001.mp4"
            clip_s01.write_bytes(b"x")

            audio_s01 = tmp / "b_001.mp3"
            audio_s01.write_bytes(b"audio")

            e1 = _make_entry(
                scene_id="sc_001",
                duration_seconds=22.605,
                clip_duration_seconds=10.042,
                clip_path=clip_s01,
                narration_reference="b_001",
                narration_path=audio_s01,
            )
            e2 = _make_entry(
                scene_id="sc_002",
                duration_seconds=8.0,
                clip_duration_seconds=8.0,
                clip_path=None,
                narration_reference="b_017",
                narration_path=None,
            )
            plan = _make_plan([e1, e2])
            pkg = _make_narration_package(["b_001", "b_002"])

            with patch(_PROBE_DIM_TARGET) as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="width=960\nheight=960\n"
                )
                report = run_timeline_qa(
                    plan,
                    narration_package=pkg,
                    hold_warn_threshold_seconds=10.0,
                )

        # Check A: square clip detected
        check_a = next(c for c in report["checks"] if c["check_id"] == "c_clip_aspect_ratio")
        assert check_a["status"] == "warn"
        assert report["clip_aspect_ratio_offenders"][0]["scene_id"] == "sc_001"

        # Check B: hold of 12.563s (22.605 - 10.042) exceeds 10s threshold
        check_b = next(c for c in report["checks"] if c["check_id"] == "c_hold_duration")
        assert check_b["status"] == "warn"
        assert report["hold_offenders"][0]["hold_seconds"] == pytest.approx(12.563, abs=0.01)

        # Check C: b_017 not in package
        check_c = next(c for c in report["checks"] if c["check_id"] == "c_missing_narration_refs")
        assert check_c["status"] == "warn"
        assert any("b_017" in w for w in report["warnings"])

        # Check D: 8.0s gap (sc_002 has no audio file) → 8s > 5s threshold → fail
        check_d = next(c for c in report["checks"] if c["check_id"] == "c_narration_coverage_real")
        assert check_d["status"] == "fail"
        assert report["narration_coverage"]["gap_seconds"] == pytest.approx(8.0, abs=0.01)

        # Overall is fail: check D fails due to 8s narration gap (sc_002 silent)
        assert report["overall_status"] == "fail"
        assert any("narration_coverage_real" in f for f in report["failures"])


# ---------------------------------------------------------------------------
# TestProbeClipDimensions
# ---------------------------------------------------------------------------

class TestProbeClipDimensions:

    def test_returns_width_height_on_success(self, tmp_path):
        clip = tmp_path / "test.mp4"
        clip.write_bytes(b"x")
        with patch(_PROBE_DIM_TARGET) as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="width=1280\nheight=720\n"
            )
            w, h = _probe_clip_dimensions(clip)
        assert w == 1280
        assert h == 720

    def test_returns_zeros_on_nonexistent_file(self, tmp_path):
        missing = tmp_path / "missing.mp4"
        w, h = _probe_clip_dimensions(missing)
        assert w == 0
        assert h == 0

    def test_returns_zeros_on_nonzero_returncode(self, tmp_path):
        clip = tmp_path / "test.mp4"
        clip.write_bytes(b"x")
        with patch(_PROBE_DIM_TARGET) as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            w, h = _probe_clip_dimensions(clip)
        assert w == 0
        assert h == 0

    def test_returns_zeros_on_exception(self, tmp_path):
        clip = tmp_path / "test.mp4"
        clip.write_bytes(b"x")
        with patch(_PROBE_DIM_TARGET, side_effect=FileNotFoundError("ffprobe not found")):
            w, h = _probe_clip_dimensions(clip)
        assert w == 0
        assert h == 0

    def test_960x960_parsed_correctly(self, tmp_path):
        clip = tmp_path / "test.mp4"
        clip.write_bytes(b"x")
        with patch(_PROBE_DIM_TARGET) as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="width=960\nheight=960\n"
            )
            w, h = _probe_clip_dimensions(clip)
        assert w == 960
        assert h == 960
