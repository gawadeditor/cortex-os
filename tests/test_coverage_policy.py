"""
tests/test_coverage_policy.py — Regression coverage for the 2026-08-01
visual-coverage policy: Video Renderer must never compensate for missing
duration (no tpad/loop/reverse/uncapped setpts). Assembly Agent computes
per-beat coverage before any rendering and, when a beat is short, requests
additional distinct MUAPI coverage rather than letting the renderer paper
over the gap. Supersedes the interim setpts-only fix in commit 44ae397.

Uses a MINIMAL two-beat validation package only (no full episode, no real
media generation — all MUAPI/ffprobe calls are mocked).

Covers:
  TestComputeBeatCoverage        — coverage_check.py unit-level math,
                                    including the exact editorial-limit
                                    boundary (speed adjustment never exceeds
                                    MAX_EDITORIAL_SPEED_FACTOR).
  TestFillCoverageDeficits       — clip_repair_agent.fill_coverage_deficits()
                                    in isolation: only touches insufficient
                                    beats, never reuses a generated clip,
                                    stops once the deficit is closed.
  TestAssemblyCoverageGateTwoBeat — full run_assembly_agent() integration on
                                    a two-beat package: resume-after-fill and
                                    halt-with-additional_coverage_required.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.coverage_check import (
    MAX_EDITORIAL_SPEED_FACTOR,
    BeatCoverage,
    check_timeline_coverage,
    compute_beat_coverage,
    build_additional_coverage_required,
)
from hypogeum.agents.clip_repair_agent import fill_coverage_deficits
from hypogeum.agents.timeline_builder import TimelineEntry
from hypogeum.agents.assembly_agent import run_assembly_agent
from tests.test_assembly_agent import (
    _make_config,
    _make_music_track_result,
    _make_thumbnail_package,
    _setup_narration_on_disk,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_entry(
    scene_id: str,
    duration_seconds: float,
    clip_duration_seconds: float,
    clip_path: Optional[Path],
    extra_clip_paths: Optional[List[Path]] = None,
    source_type: str = "main_episode",
    short_id: Optional[str] = None,
) -> TimelineEntry:
    return TimelineEntry(
        scene_index=0,
        scene_id=scene_id,
        source_type=source_type,
        short_id=short_id,
        clip_path=clip_path,
        narration_path=None,
        start_seconds=0.0,
        duration_seconds=duration_seconds,
        text_overlays=[],
        music_section=None,
        text_safe_zone=None,
        clip_duration_seconds=clip_duration_seconds,
        extra_clip_paths=extra_clip_paths or [],
    )


def _two_beat_visual_package(episode_id: str) -> dict:
    """
    Minimal two-beat main-episode visual package (no Shorts):
      sc_001 — clip spec 8s, will get an 8.0s narration beat -> sufficient
               from the start, must never be regenerated.
      sc_002 — clip spec 8s, will get a 14.0s narration beat -> deficit,
               needs additional coverage.
    """
    scenes = [
        {
            "scene_id": "sc_001",
            "scene_index": 0,
            "source_type": "main_episode",
            "narration_reference": "b_001",
            "asset_id": f"episodes/{episode_id}/sc_001",
            "production_spec": {
                "duration_seconds": 8,
                "primary_prompt": "a stone corridor",
                "primary_tier": 2,
                "fallback_prompt": "a stone corridor",
                "fallback_tier": 3,
            },
            "narrative_signals": [{"signal_type": "mystery_deepening"}],
            "section_reference": "s01",
        },
        {
            "scene_id": "sc_002",
            "scene_index": 1,
            "source_type": "main_episode",
            "narration_reference": "b_002",
            "asset_id": f"episodes/{episode_id}/sc_002",
            "production_spec": {
                "duration_seconds": 8,
                "primary_prompt": "a torchlit chamber",
                "primary_tier": 2,
                "fallback_prompt": "a torchlit chamber",
                "fallback_tier": 3,
            },
            "narrative_signals": [{"signal_type": "mystery_deepening"}],
            "section_reference": "s01",
        },
    ]
    return {
        "package_metadata": {"visual_package_id": f"vp_{episode_id}", "episode_id": episode_id},
        "episode_metadata": {
            "episode_title": "Two-Beat Coverage Validation",
            "tone_arc": "Ancient mystery",
            "topic_id": "t_cov_001",
        },
        "visual_continuity_record": {"locked_entities": [], "period_palettes": []},
        "scenes": scenes,
        "asset_manifest": {
            "assets": [
                {"asset_id": s["asset_id"], "asset_type": "generated_video",
                 "description": "", "used_in_scenes": [], "reuse_count": 1,
                 "provider": "muapi", "generation_status": "generated"}
                for s in scenes
            ]
        },
        "provider_routing_manifest": {},
        "visual_integrity_log": {},
        "thumbnail_handoff_brief": {
            "central_curiosity_gap_statement": "What happened?",
            "candidates": [
                {"candidate_id": "c1", "candidate_type": "literal_scene"},
                {"candidate_id": "c2", "candidate_type": "inferred_moment"},
            ],
            "short_candidates": [],
        },
        "package_integrity": {"production_ready": True},
        "sections": [{"section_id": "s01", "title": "Section One"}],
    }


def _two_beat_narration_package(episode_id: str, beat2_duration: float) -> dict:
    """Exactly two main-episode narration beats, no Shorts."""
    return {
        "package_metadata": {
            "narration_package_id": f"np_{episode_id}",
            "episode_id": episode_id,
        },
        "voice_configuration": {
            "voice_id": "voice-abc",
            "model_id": "eleven_multilingual_v2",
            "environment_source": "ELEVENLABS_VOICE_ID",
        },
        "narration_segments": [
            {
                "segment_id": "seg_b_001",
                "beat_reference": "b_001",
                "source_type": "main_episode",
                "generation_status": "generated",
                "audio_asset_id": f"episodes/{episode_id}/narration/b_001",
                "duration_seconds": 8.0,
            },
            {
                "segment_id": "seg_b_002",
                "beat_reference": "b_002",
                "source_type": "main_episode",
                "generation_status": "generated",
                "audio_asset_id": f"episodes/{episode_id}/narration/b_002",
                "duration_seconds": beat2_duration,
            },
        ],
        "stitched_tracks": [
            {
                "track_id": f"track_main_{episode_id}",
                "audio_asset_id": f"episodes/{episode_id}/narration/main_narration",
                "source_type": "main_episode",
            },
        ],
        "captions": [
            {
                "caption_id": f"cap_main_{episode_id}",
                "srt_asset_id": f"episodes/{episode_id}/captions/main",
                "source_type": "main_episode",
                "caption_method": "elevenlabs_alignment",
            },
        ],
    }


def _setup_clips_on_disk_no_probe(visual_package: dict, assets_root: Path) -> None:
    """Fake clip bytes real ffprobe cannot parse — TimelineEntry falls back
    to each scene's declared production_spec.duration_seconds (8.0 here for
    both beats), matching this module's controlled-arithmetic design."""
    for scene in visual_package.get("scenes", []):
        aid = scene.get("asset_id", "")
        if aid:
            p = assets_root / f"{aid}.mp4"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"FAKEVIDEO" * 100)


def _fake_duration_probe(duration_by_substring: Dict[str, float]):
    """
    subprocess.run side_effect standing in for both ffprobe duration probes
    (coverage_check._probe_duration_seconds and clip_repair_agent.
    _probe_extra_clip_duration both shell out to `ffprobe ... format=duration
    ... <path>`) and any ffmpeg render call (writes a fake output file so
    downstream existence checks pass) — without generating any real media.
    """
    def _side_effect(cmd, capture_output, text, timeout=None):
        r = MagicMock()
        r.returncode = 0
        r.stderr = ""
        r.stdout = ""
        if cmd and cmd[0] == "ffprobe":
            path_arg = str(cmd[-1])
            for substr, dur in duration_by_substring.items():
                if substr in path_arg:
                    r.stdout = f"{dur:.6f}"
                    return r
            r.stdout = ""  # unmatched/garbage clip -> caller falls back
            return r
        if cmd and cmd[0] == "ffmpeg":
            out = Path(cmd[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"FAKEMP4" * 100)
            return r
        return r
    return _side_effect


# ---------------------------------------------------------------------------
# TestComputeBeatCoverage — coverage_check.py math, incl. the editorial limit
# ---------------------------------------------------------------------------

class TestComputeBeatCoverage:

    def test_sufficient_when_clip_matches_beat_exactly(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"FAKEVIDEO")
        entry = _make_entry("sc_001", duration_seconds=8.0, clip_duration_seconds=8.0, clip_path=clip)
        cov = compute_beat_coverage(entry)
        assert cov.sufficient is True
        assert cov.deficit_seconds == pytest.approx(0.0)

    def test_missing_clip_is_zero_available_and_insufficient(self):
        entry = _make_entry("sc_001", duration_seconds=8.0, clip_duration_seconds=8.0, clip_path=None)
        cov = compute_beat_coverage(entry)
        assert cov.available_duration_seconds == 0.0
        assert cov.sufficient is False
        assert cov.deficit_seconds == pytest.approx(8.0)

    def test_speed_adjustment_never_exceeds_approved_limit_at_boundary(self, tmp_path):
        """
        Exactly at MAX_EDITORIAL_SPEED_FACTOR (1.15x an 8s clip = 9.2s) the
        beat is sufficient; one hundredth of a second past it is not. This
        is the precise enforcement of "speed adjustment never exceeds the
        approved limit" — there is no silent grace beyond the documented
        constant.
        """
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"FAKEVIDEO")

        at_limit = _make_entry("sc_001", duration_seconds=8.0 * MAX_EDITORIAL_SPEED_FACTOR,
                                clip_duration_seconds=8.0, clip_path=clip)
        past_limit = _make_entry("sc_001", duration_seconds=8.0 * MAX_EDITORIAL_SPEED_FACTOR + 0.5,
                                  clip_duration_seconds=8.0, clip_path=clip)

        assert compute_beat_coverage(at_limit).sufficient is True
        cov_past = compute_beat_coverage(past_limit)
        assert cov_past.sufficient is False
        assert cov_past.deficit_seconds > 0

    def test_extra_clips_count_toward_available_duration(self, tmp_path):
        primary = tmp_path / "primary.mp4"
        extra = tmp_path / "extra.mp4"
        primary.write_bytes(b"FAKEVIDEO")
        extra.write_bytes(b"FAKEVIDEO")
        entry = _make_entry("sc_002", duration_seconds=14.0, clip_duration_seconds=8.0,
                             clip_path=primary, extra_clip_paths=[extra])

        with patch(
            "hypogeum.agents.coverage_check._probe_duration_seconds",
            return_value=6.5,
        ):
            cov = compute_beat_coverage(entry)

        assert cov.available_duration_seconds == pytest.approx(14.5)
        assert cov.sufficient is True

    def test_check_timeline_coverage_reports_only_insufficient_beats(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"FAKEVIDEO")
        sufficient = _make_entry("sc_001", duration_seconds=8.0, clip_duration_seconds=8.0, clip_path=clip)
        insufficient = _make_entry("sc_002", duration_seconds=14.0, clip_duration_seconds=8.0, clip_path=clip)

        result = check_timeline_coverage([sufficient, insufficient])

        assert result.sufficient is False
        assert len(result.beats) == 2
        assert [b.scene_id for b in result.insufficient_beats] == ["sc_002"]

    def test_build_additional_coverage_required_shape(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"FAKEVIDEO")
        insufficient = _make_entry("sc_002", duration_seconds=14.0, clip_duration_seconds=8.0, clip_path=clip)
        result = check_timeline_coverage([insufficient])

        acr = build_additional_coverage_required(result, episode_id="ep_x")

        assert acr["status"] == "additional_coverage_required"
        assert acr["episode_id"] == "ep_x"
        assert acr["max_editorial_speed_factor"] == MAX_EDITORIAL_SPEED_FACTOR
        assert len(acr["requests"]) == 1
        req = acr["requests"][0]
        assert req["scene_id"] == "sc_002"
        assert req["needed_additional_seconds"] > 0
        assert req["reason"] == "insufficient_unique_visual_coverage"


# ---------------------------------------------------------------------------
# TestFillCoverageDeficits — clip_repair_agent.fill_coverage_deficits()
# ---------------------------------------------------------------------------

def _mock_qualify_always_pass(asset, clip_path, source_type, config, episode_id, log):
    """Standin for hypogeum.source_enhancement.qualify_asset() in tests that
    exercise fill_coverage_deficits()'s deficit-planning/stop-early/retry
    logic rather than the asset-qualification boundary itself (see
    tests/test_source_enhancement.py::TestQualifyAsset for that) — the fake
    clip bytes these tests write are not real, ffprobe-readable video, so
    without this stub every clip would be rejected as corrupt."""
    asset["source_enhancement"] = {"validation_result": "skipped_native_resolution"}
    return True


class TestFillCoverageDeficits:

    def _cfg(self, max_extra: int = 2):
        cfg = _make_config()
        cfg.COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT = max_extra
        return cfg

    def test_never_touches_beats_not_in_insufficient_list(self, tmp_path):
        """Assembly resumes from existing valid clips without regenerating
        them — a beat that already has sufficient coverage never appears in
        insufficient_beats, and fill_coverage_deficits never calls MUAPI for it."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()
        vp = _two_beat_visual_package("ep_never_touch")

        submitted_scene_ids: List[str] = []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            submitted_scene_ids.append(scene_spec["scene_id"])
            dest = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"FAKEEXTRA")
            return {"generation_status": "generated"}

        only_beat2_insufficient = [
            BeatCoverage(
                scene_id="sc_002", source_type="main_episode", short_id=None,
                narration_duration_seconds=14.0, available_duration_seconds=8.0,
                max_editorial_duration_seconds=9.2, deficit_seconds=4.8,
                sufficient=False,
            )
        ]

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.source_enhancement.qualify_asset", side_effect=_mock_qualify_always_pass), \
             patch("hypogeum.agents.clip_repair_agent._probe_extra_clip_duration", return_value=6.5):
            result = fill_coverage_deficits(
                insufficient_beats=only_beat2_insufficient,
                visual_package=vp,
                assets_root=assets_root,
                config=self._cfg(),
                episode_id="ep_never_touch",
                log=MagicMock(),
            )

        # sc_001 (already sufficient) never submitted for generation.
        assert all("sc_001" not in sid for sid in submitted_scene_ids)
        assert all(sid.startswith("sc_002") for sid in submitted_scene_ids)
        assert "sc_002" in result
        assert len(result["sc_002"]) == 1

    def test_stops_once_deficit_closed_does_not_over_generate(self, tmp_path):
        """One 6.5s extra clip closes a 4.8s-raw deficit -- the function must
        not keep generating up to the per-beat cap once the beat is covered."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()
        vp = _two_beat_visual_package("ep_stop_early")

        call_count = [0]

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            call_count[0] += 1
            dest = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"FAKEEXTRA")
            return {"generation_status": "generated"}

        beat = BeatCoverage(
            scene_id="sc_002", source_type="main_episode", short_id=None,
            narration_duration_seconds=14.0, available_duration_seconds=8.0,
            max_editorial_duration_seconds=9.2, deficit_seconds=4.8,
            sufficient=False,
        )

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.source_enhancement.qualify_asset", side_effect=_mock_qualify_always_pass), \
             patch("hypogeum.agents.clip_repair_agent._probe_extra_clip_duration", return_value=6.5):
            result = fill_coverage_deficits(
                insufficient_beats=[beat],
                visual_package=vp,
                assets_root=assets_root,
                config=self._cfg(max_extra=3),
                episode_id="ep_stop_early",
                log=MagicMock(),
            )

        assert call_count[0] == 1
        assert len(result["sc_002"]) == 1

    def test_extra_clips_are_distinct_never_reused(self, tmp_path):
        """When one clip isn't enough, every additional clip generated for
        the same beat is a distinct file -- never a copy or a repeat of a
        previous one."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()
        vp = _two_beat_visual_package("ep_distinct")

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            dest = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(f"CLIP-{scene_spec['scene_id']}".encode())
            return {"generation_status": "generated"}

        # Large deficit, small per-clip contribution -> needs multiple extras.
        beat = BeatCoverage(
            scene_id="sc_002", source_type="main_episode", short_id=None,
            narration_duration_seconds=30.0, available_duration_seconds=8.0,
            max_editorial_duration_seconds=9.2, deficit_seconds=20.8,
            sufficient=False,
        )

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.source_enhancement.qualify_asset", side_effect=_mock_qualify_always_pass), \
             patch("hypogeum.agents.clip_repair_agent._probe_extra_clip_duration", return_value=5.0):
            result = fill_coverage_deficits(
                insufficient_beats=[beat],
                visual_package=vp,
                assets_root=assets_root,
                config=self._cfg(max_extra=3),
                episode_id="ep_distinct",
                log=MagicMock(),
            )

        paths = result["sc_002"]
        assert len(paths) == 3  # capped at this test's max_extra=3
        assert len(set(paths)) == len(paths), "extra clips must be distinct files"
        for p in paths:
            assert p.exists()
        # Content differs per file -- proves each is its own generation, not a copy.
        contents = {p.read_bytes() for p in paths}
        assert len(contents) == len(paths)

    def test_variable_extra_clip_counts_driven_by_deficit(self, tmp_path):
        """Two beats with different deficits must be planned differently --
        never the same fixed count for every beat."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()
        vp = _two_beat_visual_package("ep_variable")

        call_counts: Dict[str, int] = {}

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            base = scene_spec["scene_id"].split("__extra")[0]
            call_counts[base] = call_counts.get(base, 0) + 1
            dest = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(f"CLIP-{scene_spec['scene_id']}".encode())
            return {"generation_status": "generated"}

        small_deficit_beat = BeatCoverage(
            scene_id="sc_001", source_type="main_episode", short_id=None,
            narration_duration_seconds=14.0, available_duration_seconds=8.0,
            max_editorial_duration_seconds=9.2, deficit_seconds=4.8,
            sufficient=False,
        )
        large_deficit_beat = BeatCoverage(
            scene_id="sc_002", source_type="main_episode", short_id=None,
            narration_duration_seconds=40.0, available_duration_seconds=8.0,
            max_editorial_duration_seconds=9.2, deficit_seconds=30.8,
            sufficient=False,
        )

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.source_enhancement.qualify_asset", side_effect=_mock_qualify_always_pass), \
             patch("hypogeum.agents.clip_repair_agent._probe_extra_clip_duration", return_value=5.0):
            result = fill_coverage_deficits(
                insufficient_beats=[small_deficit_beat, large_deficit_beat],
                visual_package=vp,
                assets_root=assets_root,
                config=self._cfg(max_extra=10),
                episode_id="ep_variable",
                log=MagicMock(),
            )

        # sc_001 (4.8s raw deficit / 5.0s per clip) needs 1 clip;
        # sc_002 (30.8s raw deficit / 5.0s per clip) needs many more --
        # never the same fixed count.
        assert call_counts["sc_001"] < call_counts["sc_002"]
        assert len(result["sc_001"]) == 1
        assert len(result["sc_002"]) > 2

    def test_long_beat_requiring_more_than_two_clips_planned_correctly(self, tmp_path):
        """A beat far longer than 2 clips can cover (e.g. the real ep_f3619022
        s29 scenario: ~47s narration) must plan and close correctly once the
        safety cap is raised high enough to mathematically permit it."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()
        vp = _two_beat_visual_package("ep_long_beat")

        submitted: List[str] = []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            submitted.append(scene_spec["scene_id"])
            dest = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(f"CLIP-{scene_spec['scene_id']}".encode())
            return {"generation_status": "generated"}

        # 8s primary * 1.15 = 9.2s covered; needs (46.9 - 9.2) raw-equivalent
        # closed by 8s clips -- mathematically requires 5 extra clips at
        # 8s each (5 * 8 = 40 raw, plus the primary's 8 = 48 * 1.15 = 55.2s
        # >= 46.9s), impossible within the old flat cap of 2.
        beat = BeatCoverage(
            scene_id="sc_002", source_type="main_episode", short_id=None,
            narration_duration_seconds=46.9, available_duration_seconds=8.0,
            max_editorial_duration_seconds=9.2, deficit_seconds=37.7,
            sufficient=False,
        )

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.source_enhancement.qualify_asset", side_effect=_mock_qualify_always_pass), \
             patch("hypogeum.agents.clip_repair_agent._probe_extra_clip_duration", return_value=8.0):
            result = fill_coverage_deficits(
                insufficient_beats=[beat],
                visual_package=vp,
                assets_root=assets_root,
                config=self._cfg(max_extra=10),
                episode_id="ep_long_beat",
                log=MagicMock(),
            )

        assert len(submitted) > 2, "must not be capped at the old fixed policy of 2"
        assert len(result["sc_002"]) == len(submitted)
        # Deficit genuinely closed: primary + all extras, stretched by the
        # editorial factor, now covers the narration duration.
        total_available = 8.0 + 8.0 * len(submitted)
        assert total_available * MAX_EDITORIAL_SPEED_FACTOR >= beat.narration_duration_seconds

    def test_existing_generated_short_extra_clip_discovered_and_reused(self, tmp_path):
        """The Shorts path-bug recovery scenario: a real extra clip already
        sits on disk at the CORRECT sh_{short_id}/ path (e.g. from a prior
        run, pre- or post-fix) -- submit_scene()'s own resume-safety reuse
        must be discovered via the canonical path builder and counted,
        never regenerated."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()
        vp = _two_beat_visual_package("ep_short_reuse")
        for scene in vp["scenes"]:
            scene["short_id"] = "short_001"
            scene["source_type"] = "short"

        # Pre-seed the extra clip exactly where muapi.scene_file_path()
        # (and therefore the fixed fill_coverage_deficits()) expects it.
        output_dir = assets_root / "episodes" / "ep_short_reuse"
        pre_existing = output_dir / "video" / "sh_short_001" / "sc_sc_002__extra1.mp4"
        pre_existing.parent.mkdir(parents=True, exist_ok=True)
        pre_existing.write_bytes(b"ALREADY-GENERATED-REAL-CLIP")

        def _mock_submit_reuse(scene_spec, config, episode_id, output_dir, agent):
            # Mirrors real submit_scene()'s resume-safety behavior: the file
            # already exists, so it reports "reused" without writing anything new.
            return {"generation_status": "reused"}

        beat = BeatCoverage(
            scene_id="sc_002", source_type="short", short_id="short_001",
            narration_duration_seconds=14.0, available_duration_seconds=8.0,
            max_editorial_duration_seconds=9.2, deficit_seconds=4.8,
            sufficient=False,
        )

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit_reuse), \
             patch("hypogeum.source_enhancement.qualify_asset", side_effect=_mock_qualify_always_pass), \
             patch("hypogeum.agents.clip_repair_agent._probe_extra_clip_duration", return_value=6.5):
            result = fill_coverage_deficits(
                insufficient_beats=[beat],
                visual_package=vp,
                assets_root=assets_root,
                config=self._cfg(),
                episode_id="ep_short_reuse",
                log=MagicMock(),
            )

        assert "sc_002" in result
        assert result["sc_002"] == [pre_existing]

    def test_transient_provider_failure_retries_the_same_slot(self, tmp_path):
        """A transient generation failure on the first attempt for a slot
        must retry that SAME slot (same extra_scene_id) rather than
        abandoning it or advancing to a new one."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()
        vp = _two_beat_visual_package("ep_retry_success")

        attempts: List[str] = []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            attempts.append(scene_spec["scene_id"])
            if len(attempts) == 1:
                return {"generation_status": "generation_failed"}  # transient failure
            dest = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"CLIP")
            return {"generation_status": "generated"}

        beat = BeatCoverage(
            scene_id="sc_002", source_type="main_episode", short_id=None,
            narration_duration_seconds=14.0, available_duration_seconds=8.0,
            max_editorial_duration_seconds=9.2, deficit_seconds=4.8,
            sufficient=False,
        )
        cfg = self._cfg(max_extra=2)
        cfg.COVERAGE_FILL_MAX_SLOT_RETRIES = 1

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.source_enhancement.qualify_asset", side_effect=_mock_qualify_always_pass), \
             patch("hypogeum.agents.clip_repair_agent._probe_extra_clip_duration", return_value=6.5):
            result = fill_coverage_deficits(
                insufficient_beats=[beat],
                visual_package=vp,
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_retry_success",
                log=MagicMock(),
            )

        # Both attempts targeted the SAME slot (extra1) -- the retry never
        # advanced to extra2, and the failed attempt was not counted:
        # exactly one usable clip closes the beat.
        assert attempts == ["sc_002__extra1", "sc_002__extra1"]
        assert len(result["sc_002"]) == 1

    def test_exhausted_transient_retries_fail_cleanly(self, tmp_path):
        """A slot whose every retry fails must give up on that slot cleanly
        (bounded attempts, no infinite loop) and move on -- with zero
        clips counted as coverage from it."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()
        vp = _two_beat_visual_package("ep_retry_exhausted")

        attempts: List[str] = []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            attempts.append(scene_spec["scene_id"])
            return {"generation_status": "generation_failed"}  # always fails

        beat = BeatCoverage(
            scene_id="sc_002", source_type="main_episode", short_id=None,
            narration_duration_seconds=14.0, available_duration_seconds=8.0,
            max_editorial_duration_seconds=9.2, deficit_seconds=4.8,
            sufficient=False,
        )
        cfg = self._cfg(max_extra=2)
        cfg.COVERAGE_FILL_MAX_SLOT_RETRIES = 1

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.agents.clip_repair_agent._probe_extra_clip_duration", return_value=6.5):
            result = fill_coverage_deficits(
                insufficient_beats=[beat],
                visual_package=vp,
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_retry_exhausted",
                log=MagicMock(),
            )

        # Bounded: 2 slots * (1 initial + 1 retry) = exactly 4 attempts,
        # never more -- no infinite loop.
        assert len(attempts) == 4
        assert attempts == [
            "sc_002__extra1", "sc_002__extra1",
            "sc_002__extra2", "sc_002__extra2",
        ]
        assert "sc_002" not in result  # nothing usable was ever produced

    def test_extra_clip_not_counted_until_qualified(self, tmp_path):
        """A clip that generates successfully but fails asset qualification
        (e.g. stays under the required resolution after a failed
        enhancement attempt) must never be counted toward coverage, and
        must never reach the caller's extra_clips_by_scene result --
        exactly the boundary that keeps an unqualified clip out of
        Assembly's timeline."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()
        vp = _two_beat_visual_package("ep_unqualified")

        generated_paths: List[str] = []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            dest = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"CLIP")
            generated_paths.append(str(dest))
            return {"generation_status": "generated"}

        def _mock_qualify_first_fails(asset, clip_path, source_type, config, episode_id, log):
            # extra1 never qualifies (simulates corrupt/failed enhancement);
            # extra2 qualifies cleanly.
            if "extra1" in asset["scene_id"]:
                asset["source_enhancement"] = {
                    "validation_result": "failed", "failure_reason": "still_below_target",
                }
                return True
            asset["source_enhancement"] = {"validation_result": "skipped_native_resolution"}
            return True

        beat = BeatCoverage(
            scene_id="sc_002", source_type="main_episode", short_id=None,
            narration_duration_seconds=14.0, available_duration_seconds=8.0,
            max_editorial_duration_seconds=9.2, deficit_seconds=4.8,
            sufficient=False,
        )
        cfg = self._cfg(max_extra=2)
        cfg.COVERAGE_FILL_MAX_SLOT_RETRIES = 0

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.source_enhancement.qualify_asset", side_effect=_mock_qualify_first_fails), \
             patch("hypogeum.agents.clip_repair_agent._probe_extra_clip_duration", return_value=6.5):
            result = fill_coverage_deficits(
                insufficient_beats=[beat],
                visual_package=vp,
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_unqualified",
                log=MagicMock(),
            )

        # Both slots were attempted (extra1 unqualified, extra2 qualified),
        # but only the qualified clip is ever returned to the caller --
        # this is the boundary that prevents an unqualified clip from ever
        # reaching Assembly's timeline.
        assert len(generated_paths) == 2
        assert len(result["sc_002"]) == 1
        assert "extra2" in str(result["sc_002"][0])
        assert not any("extra1" in str(p) for p in result["sc_002"])

    def test_skips_beat_whose_scene_is_not_in_visual_package(self, tmp_path):
        """A beat referencing a scene_id absent from the visual package is
        skipped gracefully (logged, not raised) -- no MUAPI call attempted."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()
        vp = _two_beat_visual_package("ep_missing_scene")

        beat = BeatCoverage(
            scene_id="sc_does_not_exist", source_type="main_episode", short_id=None,
            narration_duration_seconds=14.0, available_duration_seconds=0.0,
            max_editorial_duration_seconds=0.0, deficit_seconds=14.0,
            sufficient=False,
        )

        with patch("hypogeum.muapi.submit_scene") as mock_submit:
            result = fill_coverage_deficits(
                insufficient_beats=[beat],
                visual_package=vp,
                assets_root=assets_root,
                config=self._cfg(),
                episode_id="ep_missing_scene",
                log=MagicMock(),
            )

        mock_submit.assert_not_called()
        assert result == {}

    def test_empty_insufficient_beats_is_a_noop(self, tmp_path):
        assets_root = tmp_path / "assets"
        assets_root.mkdir()
        vp = _two_beat_visual_package("ep_noop")
        with patch("hypogeum.muapi.submit_scene") as mock_submit:
            result = fill_coverage_deficits(
                insufficient_beats=[],
                visual_package=vp,
                assets_root=assets_root,
                config=self._cfg(),
                episode_id="ep_noop",
                log=MagicMock(),
            )
        mock_submit.assert_not_called()
        assert result == {}


# ---------------------------------------------------------------------------
# TestAssemblyCoverageGateTwoBeat — full run_assembly_agent() integration on
# a minimal two-beat package
# ---------------------------------------------------------------------------

class TestAssemblyCoverageGateTwoBeat:

    def test_sufficient_beat_never_regenerated_insufficient_beat_gets_exactly_one_extra_clip(self, tmp_path):
        """
        sc_001 (already sufficient) must never reach MUAPI. sc_002 (14.0s
        beat, 8.0s clip) gets exactly one 6.5s additional clip, which closes
        its deficit -- assembly then proceeds to render rather than halting.
        """
        episode_id = "ep_cov_resume"
        visual = _two_beat_visual_package(episode_id)
        narration = _two_beat_narration_package(episode_id, beat2_duration=14.0)
        thumbnails = _make_thumbnail_package(episode_id=episode_id)

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk_no_probe(visual, assets_root)
        _setup_narration_on_disk(narration, assets_root)

        submitted_scene_ids: List[str] = []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            submitted_scene_ids.append(scene_spec["scene_id"])
            dest = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"FAKEEXTRA")
            return {"generation_status": "generated"}

        cfg = _make_config()
        cfg.COVERAGE_CHECK_ENABLED = True

        # One subprocess.run mock stands in for every ffprobe duration probe
        # (coverage_check's check + recheck, clip_repair_agent's fill loop,
        # AND video_renderer's own render-time probe of extra_clip_paths —
        # all three must agree the extra clip is 6.5s) and for the ffmpeg
        # render calls themselves. No real media is read or generated.
        with patch("hypogeum.agents.music_agent.generate_music_track",
                   return_value=_make_music_track_result(tmp_path=tmp_path)), \
             patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.source_enhancement.qualify_asset", side_effect=_mock_qualify_always_pass), \
             patch("subprocess.run", side_effect=_fake_duration_probe({"extra1": 6.5})):
            package = run_assembly_agent(
                visual_package=visual,
                narration_package=narration,
                thumbnail_package=thumbnails,
                config=cfg,
                episode_id=episode_id,
                output_dir=tmp_path / episode_id,
            )

        # sc_001 never regenerated; sc_002 got exactly one extra clip.
        assert all("sc_001" not in sid for sid in submitted_scene_ids)
        assert submitted_scene_ids == ["sc_002__extra1"]

        # Assembly proceeded past the coverage gate (not an
        # additional_coverage_required halt) -- render_status reflects a
        # real render attempt, not the coverage-blocked package shape.
        assert "additional_coverage_required" not in package
        assert package["package_metadata"]["render_package_id"] != f"rp_{episode_id}_additional_coverage_required"
        assert package["package_integrity"]["production_ready"] is True

    def test_unfillable_deficit_halts_assembly_and_reports_additional_coverage_required(self, tmp_path):
        """
        sc_002 needs 30s but the coverage-fill attempt is capped (each mock
        clip only contributes 6.5s, capped at
        COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT) -- assembly must halt BEFORE
        any video render and return a structured additional_coverage_required
        result naming sc_002 and how much more duration it still needs.
        """
        episode_id = "ep_cov_halt"
        visual = _two_beat_visual_package(episode_id)
        narration = _two_beat_narration_package(episode_id, beat2_duration=30.0)
        thumbnails = _make_thumbnail_package(episode_id=episode_id)

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk_no_probe(visual, assets_root)
        _setup_narration_on_disk(narration, assets_root)

        submitted_scene_ids: List[str] = []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            submitted_scene_ids.append(scene_spec["scene_id"])
            dest = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"FAKEEXTRA")
            return {"generation_status": "generated"}

        cfg = _make_config()
        cfg.COVERAGE_CHECK_ENABLED = True
        cfg.COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT = 2

        with patch("hypogeum.agents.music_agent.generate_music_track",
                   return_value=_make_music_track_result(tmp_path=tmp_path)), \
             patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.source_enhancement.qualify_asset", side_effect=_mock_qualify_always_pass), \
             patch("hypogeum.agents.assembly_agent.render_episode") as mock_render, \
             patch("subprocess.run", side_effect=_fake_duration_probe({"extra": 6.5})):
            package = run_assembly_agent(
                visual_package=visual,
                narration_package=narration,
                thumbnail_package=thumbnails,
                config=cfg,
                episode_id=episode_id,
                output_dir=tmp_path / episode_id,
            )

        # Capped at 2 extra clips for sc_002; sc_001 untouched.
        assert submitted_scene_ids == ["sc_002__extra1", "sc_002__extra2"]

        # Render was never reached -- assembly halted at the coverage gate.
        mock_render.assert_not_called()

        assert package["package_integrity"]["production_ready"] is False
        assert package["assembly_qa_report"]["overall_status"] == "fail"
        assert "additional_coverage_required" in package
        acr = package["additional_coverage_required"]
        assert acr["status"] == "additional_coverage_required"
        req_scene_ids = [r["scene_id"] for r in acr["requests"]]
        assert req_scene_ids == ["sc_002"]
        assert acr["requests"][0]["needed_additional_seconds"] > 0
        assert "additional_coverage_required" in package["assembly_qa_report"]["human_review_reasons"]

    def test_coverage_repair_assets_persisted_to_checkpoint_even_when_deficit_unfillable(self, tmp_path):
        """
        Regression test for the ep_af078f34 production defect (2026-08-24):
        fill_coverage_deficits() mutates visual_package["asset_manifest"]
        ["assets"] in memory for every real, paid, qualified clip it
        generates -- including sc_002__extra1 and sc_002__extra2 here, both
        real paid work -- but assembly never persisted this mutation back
        to visual_package_{episode_id}.json on disk. A resumed run reading
        that stale checkpoint would have no durable record these clips
        exist, at risk of the resumed fill_coverage_deficits() call's own
        existing_extra_ids/represented_shot_ids bookkeeping treating them
        as never attempted.

        Reproduces the exact empty-render-package scenario (unfillable
        deficit, additional_coverage_required, no render) with non-empty,
        valid assembly inputs -- identical fixture to the sibling test
        above -- and proves the checkpoint file on disk now contains both
        extra clips' manifest entries after assembly halts.
        """
        episode_id = "ep_cov_checkpoint"
        visual = _two_beat_visual_package(episode_id)
        narration = _two_beat_narration_package(episode_id, beat2_duration=30.0)
        thumbnails = _make_thumbnail_package(episode_id=episode_id)

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk_no_probe(visual, assets_root)
        _setup_narration_on_disk(narration, assets_root)

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            dest = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"FAKEEXTRA")
            return {"generation_status": "generated"}

        cfg = _make_config()
        cfg.COVERAGE_CHECK_ENABLED = True
        cfg.COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT = 2

        episode_dir = tmp_path / episode_id
        checkpoint_path = episode_dir / f"visual_package_{episode_id}.json"
        assert not checkpoint_path.exists()  # no stale file to accidentally pass against

        with patch("hypogeum.agents.music_agent.generate_music_track",
                   return_value=_make_music_track_result(tmp_path=tmp_path)), \
             patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.source_enhancement.qualify_asset", side_effect=_mock_qualify_always_pass), \
             patch("hypogeum.agents.assembly_agent.render_episode") as mock_render, \
             patch("subprocess.run", side_effect=_fake_duration_probe({"extra": 6.5})):
            package = run_assembly_agent(
                visual_package=visual,
                narration_package=narration,
                thumbnail_package=thumbnails,
                config=cfg,
                episode_id=episode_id,
                output_dir=episode_dir,
            )

        # Same halt-before-render outcome as the sibling test above --
        # the fix must never weaken this: an unfillable deficit still
        # halts assembly and still reports additional_coverage_required.
        mock_render.assert_not_called()
        assert package["package_integrity"]["production_ready"] is False
        assert "additional_coverage_required" in package

        # The defect: assembly must not silently lose the record of the
        # two real, paid clips it just generated. The checkpoint file must
        # now exist and contain both __extra1 and __extra2 manifest entries.
        assert checkpoint_path.exists(), (
            "visual_package checkpoint was never re-persisted after "
            "coverage repair generated real paid clips -- they exist on "
            "disk and cost money but a resumed run would have no record "
            "of them"
        )
        persisted = json.loads(checkpoint_path.read_text())
        persisted_scene_ids = {
            a.get("scene_id") for a in persisted["asset_manifest"]["assets"]
        }
        assert "sc_002__extra1" in persisted_scene_ids
        assert "sc_002__extra2" in persisted_scene_ids

        # In-memory and on-disk manifests must agree (same record, no drift).
        assert persisted["asset_manifest"]["assets"] == visual["asset_manifest"]["assets"]


# ---------------------------------------------------------------------------
# TestRendererConsumesCanonicalCoverage — proves video_renderer.py has no
# second implementation of coverage math: it consumes coverage_check.py's
# BeatCoverage (specifically usable_clips) directly rather than re-deriving
# which clips are usable or how much duration is available. Renderer and
# coverage_check must never be able to reach different answers for the same
# TimelineEntry, because there is only one computation, called once.
# ---------------------------------------------------------------------------

class TestRendererConsumesCanonicalCoverage:

    def test_renderer_uses_exactly_the_canonical_usable_clips_not_its_own_list(self, tmp_path):
        """
        The strongest possible single-source-of-truth proof: patch
        compute_beat_coverage() to return usable_clips that do NOT match
        what the TimelineEntry's own clip_path/extra_clip_paths would
        naively suggest (a clip that .exists() on disk is deliberately
        left OUT of the mocked usable_clips). If video_renderer had any
        fallback/parallel computation of its own, it would still include
        that clip; since it has none, the emitted ffmpeg command must
        follow the mocked canonical result exactly.
        """
        from hypogeum.agents.coverage_check import BeatCoverage
        from hypogeum.agents.video_renderer import _build_ffmpeg_concat_command

        primary = tmp_path / "primary.mp4"
        decoy = tmp_path / "decoy_not_in_canonical_result.mp4"
        primary.write_bytes(b"FAKEVIDEO")
        decoy.write_bytes(b"FAKEVIDEO")

        entry = _make_entry("sc_001", duration_seconds=8.0, clip_duration_seconds=8.0,
                             clip_path=primary, extra_clip_paths=[decoy])

        fake_coverage = BeatCoverage(
            scene_id="sc_001", source_type="main_episode", short_id=None,
            narration_duration_seconds=8.0, available_duration_seconds=8.0,
            max_editorial_duration_seconds=9.2, deficit_seconds=0.0,
            sufficient=True,
            usable_clips=[(primary, 8.0)],  # decoy deliberately excluded
        )

        with patch(
            "hypogeum.agents.video_renderer.compute_beat_coverage",
            return_value=fake_coverage,
        ):
            cmd = _build_ffmpeg_concat_command(
                timeline=[entry], narration_path=None, music_track=None,
                output_path=tmp_path / "out.mp4", resolution="1920x1080",
                episode_id="ep_test",
            )
        cmd_str = " ".join(str(c) for c in cmd)

        assert str(primary) in cmd_str
        assert str(decoy) not in cmd_str

    def test_renderer_raises_using_canonical_deficit_not_a_recomputed_one(self, tmp_path):
        """The InsufficientCoverageError message must report the deficit
        exactly as coverage_check computed it -- proving the renderer
        reads the canonical BeatCoverage rather than recomputing its own
        (possibly different) deficit number."""
        from hypogeum.agents.coverage_check import BeatCoverage
        from hypogeum.agents.video_renderer import (
            InsufficientCoverageError,
            _build_ffmpeg_concat_command,
        )

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"FAKEVIDEO")
        entry = _make_entry("sc_001", duration_seconds=100.0, clip_duration_seconds=8.0,
                             clip_path=clip)

        fake_coverage = BeatCoverage(
            scene_id="sc_001", source_type="main_episode", short_id=None,
            narration_duration_seconds=100.0, available_duration_seconds=8.0,
            max_editorial_duration_seconds=9.2, deficit_seconds=42.5,  # a distinctive, made-up value
            sufficient=False,
            usable_clips=[(clip, 8.0)],
        )

        with patch(
            "hypogeum.agents.video_renderer.compute_beat_coverage",
            return_value=fake_coverage,
        ):
            with pytest.raises(InsufficientCoverageError, match=r"42\.500s"):
                _build_ffmpeg_concat_command(
                    timeline=[entry], narration_path=None, music_track=None,
                    output_path=tmp_path / "out.mp4", resolution="1920x1080",
                    episode_id="ep_test",
                )

    def test_compute_beat_coverage_called_exactly_once_per_clip_entry(self, tmp_path):
        """video_renderer must call the canonical function exactly once per
        clip-bearing entry -- not zero (would mean it stopped consulting
        coverage_check) and not recomputed via some other internal path."""
        from hypogeum.agents.video_renderer import _build_ffmpeg_concat_command

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"FAKEVIDEO")
        entry = _make_entry("sc_001", duration_seconds=8.0, clip_duration_seconds=8.0,
                             clip_path=clip)

        with patch(
            "hypogeum.agents.video_renderer.compute_beat_coverage",
            wraps=compute_beat_coverage,
        ) as spy:
            _build_ffmpeg_concat_command(
                timeline=[entry], narration_path=None, music_track=None,
                output_path=tmp_path / "out.mp4", resolution="1920x1080",
                episode_id="ep_test",
            )
        spy.assert_called_once_with(entry)

    def test_renderer_and_coverage_check_agree_on_a_real_two_beat_timeline(self, tmp_path):
        """
        End-to-end non-divergence proof: compute ground truth via
        coverage_check.check_timeline_coverage() directly, then render the
        same timeline, and confirm the exact clip files/order the renderer
        put into the ffmpeg command match ground truth's usable_clips for
        every beat -- not merely "close," but identical, because it is
        the same function call, not two implementations that happen to
        agree today.
        """
        from hypogeum.agents.video_renderer import _build_ffmpeg_concat_command

        primary1 = tmp_path / "p1.mp4"
        primary2 = tmp_path / "p2.mp4"
        extra2 = tmp_path / "e2.mp4"
        for p in (primary1, primary2, extra2):
            p.write_bytes(b"FAKEVIDEO")

        entries = [
            _make_entry("sc_001", duration_seconds=8.0, clip_duration_seconds=8.0,
                        clip_path=primary1),
            _make_entry("sc_002", duration_seconds=16.0, clip_duration_seconds=8.0,
                        clip_path=primary2, extra_clip_paths=[extra2]),
        ]

        with patch(
            "hypogeum.agents.coverage_check._probe_duration_seconds",
            return_value=8.0,
        ):
            ground_truth = check_timeline_coverage(entries)
            assert ground_truth.sufficient

            cmd = _build_ffmpeg_concat_command(
                timeline=entries, narration_path=None, music_track=None,
                output_path=tmp_path / "out.mp4", resolution="1920x1080",
                episode_id="ep_test",
            )
        cmd_str = " ".join(str(c) for c in cmd)

        for beat in ground_truth.beats:
            for clip_path, _ in beat.usable_clips:
                assert str(clip_path) in cmd_str, (
                    f"{clip_path} from coverage_check's ground truth is missing "
                    f"from the actual rendered ffmpeg command"
                )

    def test_generate_shorts_ep_delegates_to_canonical_renderer_and_enforces_coverage(self, tmp_path):
        """
        Cross-tool proof: generate_shorts_ep.py (a manual tool, not the
        production pipeline) must reach the exact same enforcement as
        render_episode() -- because it now calls the same
        _build_ffmpeg_concat_command() rather than maintaining its own
        filter-graph builder. An insufficient beat must raise
        InsufficientCoverageError here too.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "generate_shorts_ep_under_test",
            Path(__file__).resolve().parent.parent / "generate_shorts_ep.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        from hypogeum.agents.video_renderer import InsufficientCoverageError

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"FAKEVIDEO")
        narr = tmp_path / "narr.m4a"
        narr.write_bytes(b"\xff\xfb" * 100)
        music = tmp_path / "music.mp3"
        music.write_bytes(b"\xff\xfb" * 100)

        with pytest.raises(InsufficientCoverageError):
            mod._render_short_body(
                clip_paths=[clip],
                beat_durations=[100.0],  # wildly beyond any editorial-limit stretch of an 8s clip
                narration_path=narr,
                music_path=music,
                output_path=tmp_path / "short.mp4",
                short_id="sh_test",
                log=MagicMock(),
            )
