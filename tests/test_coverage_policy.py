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
