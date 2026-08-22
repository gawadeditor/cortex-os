"""
tests/test_assembly_agent.py — Unit tests for hypogeum/agents/assembly_agent.py

All subprocess/ffmpeg calls are mocked. No real ffmpeg is executed.
All filesystem existence checks use tmp_path or are mocked.
Music sourcing is pre-provided via music_track_package parameter or mocked via
hypogeum.agents.music_agent.generate_music_track.

Test coverage:
  TestAssetInventoryValidation       — clip availability logic and thresholds
  TestTimelineConstruction           — scene duration, trim/pad, ordering
  TestMusicSourcingChain             — music_track_package consumption by assembly
  TestAudioMixingOrchestration       — dB levels, ffmpeg amix args
  TestFfmpegExecutionLayer           — render paths, error handling, timeout
  TestFinalRenderPackage             — schema, statuses, video_assets, qa_report
  TestProductionReadyEvaluation      — production_ready flag conditions
  TestPipelineIntegration            — pipeline stub delegates to assembly agent
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.config import Config
from hypogeum.elevenlabs import MusicTrackResult, ElevenLabsError, ElevenLabsAuthFailure
from hypogeum.agents.assembly_agent import (
    AssemblyAgentError,
    _collect_thumbnail_references,
    _compute_chapter_timestamps,
    _find_caption_asset_id,
    _find_stitched_track_asset_id,
    _format_sources,
    run_assembly_agent,
)
from hypogeum.schemas import validate


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

def _make_config() -> Config:
    env = {
        "OPENROUTER_API_KEY": "or-key",
        "ELEVENLABS_API_KEY": "xi-key",
        "ELEVENLABS_VOICE_ID": "voice-abc",
        # Disable clip repair so existing assembly tests observe raw availability rates.
        "CLIP_REPAIR_ENABLED": "false",
        # Disable the coverage gate here — these fixtures don't control exact
        # narration/clip duration alignment and predate the 2026-08-01 coverage
        # policy. The gate itself is covered by tests/test_coverage_check.py.
        "COVERAGE_CHECK_ENABLED": "false",
    }
    with patch.dict(os.environ, env, clear=True):
        return Config()


def _make_visual_package(scenes=None, episode_id="ep_001") -> dict:
    if scenes is None:
        scenes = [
            {
                "scene_id": "sc_001",
                "scene_index": 0,
                "source_type": "main_episode",
                "narration_reference": "b_001",
                "asset_id": f"episodes/{episode_id}/sc_001",
                "production_spec": {"duration_seconds": 8},
                "narrative_signals": [{"signal_type": "mystery_deepening"}],
                "section_reference": "s01",
            },
            {
                "scene_id": "sc_002",
                "scene_index": 1,
                "source_type": "main_episode",
                "narration_reference": "b_002",
                "asset_id": f"episodes/{episode_id}/sc_002",
                "production_spec": {"duration_seconds": 8},
                "narrative_signals": [{"signal_type": "mystery_deepening"}],
                "section_reference": "s01",
            },
            {
                "scene_id": "sc_sh1_001",
                "scene_index": 0,
                "source_type": "short",
                "short_id": "sh_001",
                "narration_reference": "sh1b_001",
                "asset_id": f"episodes/{episode_id}/sc_sh1_001",
                "production_spec": {"duration_seconds": 5},
                "narrative_signals": [],
                "text_safe_zone": "bottom",
            },
            {
                "scene_id": "sc_sh2_001",
                "scene_index": 0,
                "source_type": "short",
                "short_id": "sh_002",
                "narration_reference": "sh2b_001",
                "asset_id": f"episodes/{episode_id}/sc_sh2_001",
                "production_spec": {"duration_seconds": 5},
                "narrative_signals": [],
                "text_safe_zone": "bottom",
            },
            {
                "scene_id": "sc_sh3_001",
                "scene_index": 0,
                "source_type": "short",
                "short_id": "sh_003",
                "narration_reference": "sh3b_001",
                "asset_id": f"episodes/{episode_id}/sc_sh3_001",
                "production_spec": {"duration_seconds": 5},
                "narrative_signals": [],
                "text_safe_zone": "bottom",
            },
        ]

    return {
        "package_metadata": {"visual_package_id": "vp_ep_001", "episode_id": episode_id},
        "episode_metadata": {
            "episode_title": "Test Episode",
            "tone_arc": "Ancient mystery",
            "topic_id": "t_001",
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
            "short_candidates": [
                {"short_id": "sh_001", "candidates": [
                    {"candidate_id": "sc1", "candidate_type": "literal_scene"},
                    {"candidate_id": "sc2", "candidate_type": "inferred_moment"},
                ]},
                {"short_id": "sh_002", "candidates": [
                    {"candidate_id": "sc3", "candidate_type": "literal_scene"},
                    {"candidate_id": "sc4", "candidate_type": "inferred_moment"},
                ]},
                {"short_id": "sh_003", "candidates": [
                    {"candidate_id": "sc5", "candidate_type": "literal_scene"},
                    {"candidate_id": "sc6", "candidate_type": "inferred_moment"},
                ]},
            ],
        },
        "package_integrity": {"production_ready": True},
        "sections": [
            {"section_id": "s01", "title": "Section One"},
        ],
    }


def _make_narration_package(episode_id="ep_001") -> dict:
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
                "duration_seconds": 4.5,
            },
            {
                "segment_id": "seg_b_002",
                "beat_reference": "b_002",
                "source_type": "main_episode",
                "generation_status": "generated",
                "audio_asset_id": f"episodes/{episode_id}/narration/b_002",
                "duration_seconds": 5.5,
            },
            {
                "segment_id": "seg_sh1b_001",
                "beat_reference": "sh1b_001",
                "source_type": "short",
                "generation_status": "generated",
                "audio_asset_id": f"episodes/{episode_id}/narration/sh1b_001",
                # Matches the sh_001 scene's production_spec.duration_seconds=5 in
                # _make_visual_package() — real, forward-only coverage (2026-08-01
                # policy) means these must not be left mismatched; see
                # hypogeum/agents/coverage_check.py.
                "duration_seconds": 5.0,
            },
            {
                "segment_id": "seg_sh2b_001",
                "beat_reference": "sh2b_001",
                "source_type": "short",
                "generation_status": "generated",
                "audio_asset_id": f"episodes/{episode_id}/narration/sh2b_001",
                "duration_seconds": 5.0,
            },
            {
                "segment_id": "seg_sh3b_001",
                "beat_reference": "sh3b_001",
                "source_type": "short",
                "generation_status": "generated",
                "audio_asset_id": f"episodes/{episode_id}/narration/sh3b_001",
                "duration_seconds": 5.0,
            },
        ],
        "stitched_tracks": [
            {
                "track_id": f"track_main_{episode_id}",
                "audio_asset_id": f"episodes/{episode_id}/narration/main_narration",
                "source_type": "main_episode",
            },
            {
                "track_id": f"track_sh_001_{episode_id}",
                "audio_asset_id": f"episodes/{episode_id}/narration/sh_sh_001_narration",
                "source_type": "short",
            },
            {
                "track_id": f"track_sh_002_{episode_id}",
                "audio_asset_id": f"episodes/{episode_id}/narration/sh_sh_002_narration",
                "source_type": "short",
            },
            {
                "track_id": f"track_sh_003_{episode_id}",
                "audio_asset_id": f"episodes/{episode_id}/narration/sh_sh_003_narration",
                "source_type": "short",
            },
        ],
        "captions": [
            {
                "caption_id": f"cap_main_{episode_id}",
                "srt_asset_id": f"episodes/{episode_id}/captions/main",
                "source_type": "main_episode",
                "caption_method": "elevenlabs_alignment",
            },
            {
                "caption_id": f"cap_sh001_{episode_id}",
                "srt_asset_id": f"episodes/{episode_id}/captions/sh_sh_001",
                "source_type": "short",
                "caption_method": "elevenlabs_alignment",
            },
            {
                "caption_id": f"cap_sh002_{episode_id}",
                "srt_asset_id": f"episodes/{episode_id}/captions/sh_sh_002",
                "source_type": "short",
                "caption_method": "elevenlabs_alignment",
            },
            {
                "caption_id": f"cap_sh003_{episode_id}",
                "srt_asset_id": f"episodes/{episode_id}/captions/sh_sh_003",
                "source_type": "short",
                "caption_method": "elevenlabs_alignment",
            },
        ],
        "audio_qa_report": {"overall_status": "pass"},
        "package_integrity": {"production_ready": True},
    }


def _make_thumbnail_package(episode_id="ep_001") -> dict:
    return {
        "thumbnail_package_id": f"tp_{episode_id}",
        "episode_id": episode_id,
        "main_thumbnail": {
            "asset_id": f"episodes/{episode_id}/thumbnails/main",
        },
        "short_thumbnails": [
            {"short_id": "sh_001", "asset_id": f"episodes/{episode_id}/thumbnails/sh_001"},
            {"short_id": "sh_002", "asset_id": f"episodes/{episode_id}/thumbnails/sh_002"},
            {"short_id": "sh_003", "asset_id": f"episodes/{episode_id}/thumbnails/sh_003"},
        ],
        "package_integrity": {"production_ready": True},
    }


def _make_music_track_result(
    category: str = "Mystery",
    source: str = "ai_generated",
    track_id: str = "track-abc",
    tmp_path: Optional[Path] = None,
) -> MusicTrackResult:
    """Create a MusicTrackResult with a real file on disk."""
    if tmp_path:
        music_dir = tmp_path / ".hypogeum-assets" / "channel" / "music" / "mystery"
        music_dir.mkdir(parents=True, exist_ok=True)
        local = music_dir / f"mystery_{track_id}.mp3"
        local.write_bytes(b"\xff\xfb" * 5000)
    else:
        local = Path(f"/tmp/test_music_{track_id}.mp3")

    return MusicTrackResult(
        track_id=track_id,
        category=category,
        asset_id=f"channel/music/mystery/mystery_{track_id}",
        local_path=local,
        source=source,
        duration_seconds=120.0,
        prompt_used="slow atmospheric dark underscore",
    )


def _make_music_track_package(
    episode_id: str = "ep_001",
    source: str = "ai_generated",
    tmp_path: Optional[Path] = None,
    sections_without_music: Optional[list] = None,
) -> dict:
    """Build a minimal valid music_track_package for testing."""
    track_result = _make_music_track_result(source=source, tmp_path=tmp_path)
    section_keys = ["main_episode", "short_sh_001", "short_sh_002", "short_sh_003"]
    if sections_without_music:
        sourced_keys = [k for k in section_keys if k not in sections_without_music]
    else:
        sourced_keys = section_keys
        sections_without_music = []

    tracks = {}
    music_log = []
    for sk in sourced_keys:
        tracks[sk] = {
            "section_key": sk,
            "category": "Mystery",
            "asset_id": track_result.asset_id,
            "local_path": str(track_result.local_path),
            "source": source,
            "duration_seconds": 120.0,
            "prompt_used": track_result.prompt_used or "",
        }
        music_log.append({
            "log_id": f"ml_{sk}",
            "placed_in": sk,
            "timeline_position_seconds": 0.0,
            "category": "Mystery",
            "music_asset_id": track_result.asset_id,
            "source": source,
            "provider": "elevenlabs" if source == "ai_generated" else None,
            "confidence_note": None,
            "fallback_trigger": None,
        })
    for sk in sections_without_music:
        music_log.append({
            "log_id": f"ml_{sk}_failed",
            "placed_in": sk,
            "timeline_position_seconds": 0.0,
            "category": "Mystery",
            "music_asset_id": "",
            "source": "ai_generated",
            "provider": None,
            "confidence_note": None,
            "fallback_trigger": "provider_failure",
        })

    sourcing_status = (
        "complete" if not sections_without_music
        else "failed" if not tracks
        else "partial"
    )
    return {
        "package_id": f"mtp_{episode_id}_test",
        "episode_id": episode_id,
        "sourced_at": "2026-01-01T00:00:00+00:00",
        "sourcing_status": sourcing_status,
        "tracks": tracks,
        "music_log": music_log,
        "sections_without_music": sections_without_music,
    }


def _setup_clips_on_disk(visual_package: dict, assets_root: Path, episode_id: str) -> None:
    """Create dummy clip files on disk for all scenes in visual package."""
    for scene in visual_package.get("scenes", []):
        aid = scene.get("asset_id", "")
        if aid:
            clip_path = assets_root / f"{aid}.mp4"
            clip_path.parent.mkdir(parents=True, exist_ok=True)
            clip_path.write_bytes(b"FAKEVIDEO" * 100)


def _setup_narration_on_disk(narration_package: dict, assets_root: Path) -> None:
    """Create dummy narration files on disk."""
    for track in narration_package.get("stitched_tracks", []):
        aid = track.get("audio_asset_id", "")
        if aid:
            p = assets_root / f"{aid}.mp3"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\xff\xfb" * 100)


def _mock_ffmpeg_success(output_path_arg_index=-1):
    """Return a mock that simulates successful ffmpeg by creating the output file."""
    def _side_effect(cmd, capture_output, text, timeout):
        # Find output path in cmd (last positional argument before flags)
        # The output is the last element in the command
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"FAKEMP4" * 1000)
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        result.stdout = ""
        return result
    return _side_effect


# ---------------------------------------------------------------------------
# TestAssetInventoryValidation
# ---------------------------------------------------------------------------

class TestAssetInventoryValidation:

    def test_all_clips_present_passes_validation(self, tmp_path):
        """When all clips exist, availability_rate = 1.0 and assembly proceeds."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "episodes" / "ep_001",
                )

        qa = result.get("assembly_qa_report", {})
        assert qa.get("clip_availability_rate", 0) >= 0.99

    def test_missing_clip_logged_and_counted(self, tmp_path):
        """Missing clips are counted and recorded in qa_report.scenes_missing."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        # Only create one clip out of 5 total (2 main + 3 shorts)
        scenes = visual["scenes"]
        first_scene = scenes[0]
        aid = first_scene.get("asset_id", "")
        if aid:
            clip_path = assets_root / f"{aid}.mp4"
            clip_path.parent.mkdir(parents=True, exist_ok=True)
            clip_path.write_bytes(b"FAKEVIDEO" * 100)
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "episodes" / "ep_001",
                )

        qa = result.get("assembly_qa_report", {})
        assert len(qa.get("scenes_missing", [])) > 0

    def test_below_60_percent_threshold_halts_assembly(self, tmp_path):
        """When clip_availability_rate < 0.60, render_status is 'failed'."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        # No clips at all — rate = 0.0
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            result = run_assembly_agent(
                visual_package=visual,
                narration_package=narration,
                thumbnail_package=thumbnails,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path / "episodes" / "ep_001",
            )

        qa = result.get("assembly_qa_report", {})
        assert qa.get("overall_status") == "fail"
        assert result["package_integrity"]["production_ready"] is False

    def test_exactly_60_percent_threshold_proceeds(self, tmp_path):
        """clip_availability_rate == 0.60 exactly should proceed (not halt)."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        scenes = visual["scenes"]
        # 5 total; 3 out of 5 = 0.60
        for scene in scenes[:3]:
            aid = scene.get("asset_id", "")
            if aid:
                clip_path = assets_root / f"{aid}.mp4"
                clip_path.parent.mkdir(parents=True, exist_ok=True)
                clip_path.write_bytes(b"FAKEVIDEO" * 100)
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "episodes" / "ep_001",
                )

        # Should have proceeded (not failed with clip_availability_below_threshold)
        qa = result.get("assembly_qa_report", {})
        rate = qa.get("clip_availability_rate", 0.0)
        assert rate >= 0.59  # 3/5 = 0.60

    def test_above_60_percent_threshold_proceeds(self, tmp_path):
        """clip_availability_rate > 0.60 should produce partial or success."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        scenes = visual["scenes"]
        # 4 out of 5 = 0.80 — well above 0.60 threshold
        for scene in scenes[:4]:
            aid = scene.get("asset_id", "")
            if aid:
                clip_path = assets_root / f"{aid}.mp4"
                clip_path.parent.mkdir(parents=True, exist_ok=True)
                clip_path.write_bytes(b"FAKEVIDEO" * 100)
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "episodes" / "ep_001",
                )

        assert result.get("package_metadata", {}).get("episode_id") == "ep_001"

    def test_narration_files_validated(self, tmp_path):
        """Narration file paths are checked during asset validation."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        # Do NOT set up narration files

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "episodes" / "ep_001",
                )
        # Should complete (narration missing → failed package or continues with empty audio)
        assert "package_metadata" in result

    def test_missing_narration_raises(self, tmp_path):
        """When main narration track is missing, assembly returns a failed package."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        # Do NOT set up any narration — main track will be missing

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "episodes" / "ep_001",
                )

        # Missing narration leads to failed package
        assert result["package_integrity"]["production_ready"] is False


# ---------------------------------------------------------------------------
# TestMusicSourcingChain
# ---------------------------------------------------------------------------

class TestMusicSourcingChain:
    """Assembly Agent correctly uses music_track_package from Music Agent."""

    def test_channel_library_source_flows_to_music_log(self, tmp_path):
        """When music_track_package has channel_library source, music_log reflects it."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        mtp = _make_music_track_package(source="channel_library", tmp_path=tmp_path)

        with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
            result = run_assembly_agent(
                visual_package=visual,
                narration_package=narration,
                thumbnail_package=thumbnails,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path / "episodes" / "ep_001",
                music_track_package=mtp,
            )

        music_log = result.get("music_log", [])
        assert any(e.get("source") == "channel_library" for e in music_log)

    def test_ai_generated_source_flows_to_music_log(self, tmp_path):
        """When music_track_package has ai_generated source, music_log reflects it."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        mtp = _make_music_track_package(source="ai_generated", tmp_path=tmp_path)

        with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
            result = run_assembly_agent(
                visual_package=visual,
                narration_package=narration,
                thumbnail_package=thumbnails,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path / "episodes" / "ep_001",
                music_track_package=mtp,
            )

        music_log = result.get("music_log", [])
        assert any(e.get("source") == "ai_generated" for e in music_log)

    def test_music_failure_does_not_halt_assembly(self, tmp_path):
        """Pre-built package with empty tracks: assembly still completes."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        # All sections failed — package with no tracks
        all_sections = ["main_episode", "short_sh_001", "short_sh_002", "short_sh_003"]
        mtp = _make_music_track_package(
            tmp_path=tmp_path,
            sections_without_music=all_sections,
        )

        with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
            result = run_assembly_agent(
                visual_package=visual,
                narration_package=narration,
                thumbnail_package=thumbnails,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path / "episodes" / "ep_001",
                music_track_package=mtp,
            )

        # Assembly must complete — not raise
        assert "package_metadata" in result
        assert result["package_metadata"]["episode_id"] == "ep_001"

    def test_music_log_populated_per_section(self, tmp_path):
        """music_log has one entry per section (main + each short)."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        mtp = _make_music_track_package(tmp_path=tmp_path)

        with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
            result = run_assembly_agent(
                visual_package=visual,
                narration_package=narration,
                thumbnail_package=thumbnails,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path / "episodes" / "ep_001",
                music_track_package=mtp,
            )

        music_log = result.get("music_log", [])
        assert len(music_log) >= 1
        assert all("log_id" in e and "source" in e for e in music_log)

    def test_sections_without_music_recorded_in_qa_report(self, tmp_path):
        """sections_without_music in package flows to qa_report."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        mtp = _make_music_track_package(
            tmp_path=tmp_path,
            sections_without_music=["main_episode"],
        )

        with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
            result = run_assembly_agent(
                visual_package=visual,
                narration_package=narration,
                thumbnail_package=thumbnails,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path / "episodes" / "ep_001",
                music_track_package=mtp,
            )

        qa = result.get("assembly_qa_report", {})
        assert "main_episode" in qa.get("sections_without_music", [])


# ---------------------------------------------------------------------------
# TestOptionBMusicSegments (Option B, 2026-07-19 — main episode only)
# ---------------------------------------------------------------------------

class TestOptionBMusicSegments:
    """
    Assembly Agent sources per-section main-episode music (compute_music_sections
    + run_music_agent(section_categories=...)) and hands the Video Renderer a
    ready-to-place music_segments list. Assembly derives the sections and
    categories; render_episode() only consumes them (verified separately in
    test_video_renderer.py::TestMultiSegmentMusic).
    """

    def _two_category_scenes(self, episode_id="ep_001"):
        return [
            {
                "scene_id": "sc_001", "scene_index": 0, "source_type": "main_episode",
                "narration_reference": "b_001",
                "asset_id": f"episodes/{episode_id}/sc_001",
                "production_spec": {"duration_seconds": 8},
                "narrative_signals": [{"signal_type": "mystery_deepening"}],
                "section_reference": "s01",
            },
            {
                "scene_id": "sc_002", "scene_index": 1, "source_type": "main_episode",
                "narration_reference": "b_002",
                "asset_id": f"episodes/{episode_id}/sc_002",
                "production_spec": {"duration_seconds": 8},
                "narrative_signals": [{"signal_type": "shock_fact"}],  # -> Tension
                "section_reference": "s01",
            },
            {
                "scene_id": "sc_sh1_001", "scene_index": 0, "source_type": "short",
                "short_id": "sh_001", "narration_reference": "sh1b_001",
                "asset_id": f"episodes/{episode_id}/sc_sh1_001",
                "production_spec": {"duration_seconds": 5},
                "narrative_signals": [], "text_safe_zone": "bottom",
            },
            {
                "scene_id": "sc_sh2_001", "scene_index": 0, "source_type": "short",
                "short_id": "sh_002", "narration_reference": "sh2b_001",
                "asset_id": f"episodes/{episode_id}/sc_sh2_001",
                "production_spec": {"duration_seconds": 5},
                "narrative_signals": [], "text_safe_zone": "bottom",
            },
            {
                "scene_id": "sc_sh3_001", "scene_index": 0, "source_type": "short",
                "short_id": "sh_003", "narration_reference": "sh3b_001",
                "asset_id": f"episodes/{episode_id}/sc_sh3_001",
                "production_spec": {"duration_seconds": 5},
                "narrative_signals": [], "text_safe_zone": "bottom",
            },
        ]

    def test_two_categories_source_two_main_sections_with_distinct_categories(self, tmp_path):
        """A category change between two consecutive main scenes results in
        two generate_music_track calls for the main episode, requesting the
        two distinct derived categories (Mystery, Tension) -- not the flat
        tone_arc category repeated for the whole episode."""
        cfg = _make_config()
        visual = _make_visual_package(scenes=self._two_category_scenes())
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        requested = []
        def _capture(**kwargs):
            requested.append((kwargs["category"], kwargs["episode_id"]))
            return _make_music_track_result(category=kwargs["category"], tmp_path=tmp_path)

        with patch("hypogeum.agents.music_agent.generate_music_track", side_effect=_capture):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "episodes" / "ep_001",
                )

        main_categories = {cat for cat, _ in requested[:2]}
        assert main_categories == {"Mystery", "Tension"}

    def test_render_episode_receives_music_segments_with_boundaries(self, tmp_path):
        """render_episode() is called with a music_segments list whose
        section boundaries match the two-category grouping, and whose
        tracks are the actually-sourced MusicTrackResult objects."""
        cfg = _make_config()
        visual = _make_visual_package(scenes=self._two_category_scenes())
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            side_effect=lambda **kw: _make_music_track_result(category=kw["category"], tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                with patch(
                    "hypogeum.agents.assembly_agent.render_episode",
                    wraps=__import__("hypogeum.agents.video_renderer", fromlist=["render_episode"]).render_episode,
                ) as mock_render:
                    run_assembly_agent(
                        visual_package=visual,
                        narration_package=narration,
                        thumbnail_package=thumbnails,
                        config=cfg,
                        episode_id="ep_001",
                        output_dir=tmp_path / "episodes" / "ep_001",
                    )

        assert mock_render.call_count == 1
        segments = mock_render.call_args.kwargs["music_segments"]
        assert len(segments) == 2
        assert segments[0]["start_seconds"] == pytest.approx(0.0)
        assert segments[1]["start_seconds"] == segments[0]["end_seconds"]
        assert all(s["track"] is not None for s in segments)

    def test_legacy_pre_supplied_package_falls_back_to_single_segment(self, tmp_path):
        """Backward compatibility: a pre-supplied music_track_package using
        the old flat 'main_episode' key (as every other test in this file
        still uses) produces music_segments whose per-section track lookups
        all miss -- render_episode() and the ffmpeg layer fall back to the
        legacy single music_tracks['main_episode'] track (verified directly
        in test_video_renderer.py; here we only confirm Assembly doesn't
        error and still produces a normal successful render)."""
        cfg = _make_config()
        visual = _make_visual_package()  # single-category default fixture
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        mtp = _make_music_track_package(tmp_path=tmp_path)  # legacy "main_episode" key

        with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
            result = run_assembly_agent(
                visual_package=visual,
                narration_package=narration,
                thumbnail_package=thumbnails,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path / "episodes" / "ep_001",
                music_track_package=mtp,
            )

        assert result["package_integrity"]["production_ready"] is True

    def test_shorts_music_sourcing_key_format_unchanged(self, tmp_path):
        """Shorts are never split into sections -- their section_durations
        keys remain 'short_{short_id}', exactly as before Option B."""
        cfg = _make_config()
        visual = _make_visual_package(scenes=self._two_category_scenes())
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.assembly_agent.run_music_agent",
            wraps=__import__(
                "hypogeum.agents.music_agent", fromlist=["run_music_agent"]
            ).run_music_agent,
        ) as mock_run_music_agent:
            with patch(
                "hypogeum.agents.music_agent.generate_music_track",
                side_effect=lambda **kw: _make_music_track_result(category=kw["category"], tmp_path=tmp_path),
            ):
                with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                    run_assembly_agent(
                        visual_package=visual,
                        narration_package=narration,
                        thumbnail_package=thumbnails,
                        config=cfg,
                        episode_id="ep_001",
                        output_dir=tmp_path / "episodes" / "ep_001",
                    )

        assert mock_run_music_agent.call_count == 1
        section_durations = mock_run_music_agent.call_args.kwargs["section_durations"]
        short_keys = {k for k in section_durations if k.startswith("short_")}
        assert short_keys == {"short_sh_001", "short_sh_002", "short_sh_003"}


# ---------------------------------------------------------------------------
# TestAudioMixingOrchestration
# ---------------------------------------------------------------------------

class TestAudioMixingOrchestration:

    def test_music_at_minus14db_relative_to_narration(self):
        """Music amplitude factor is ≈0.20 (−14 dB relative to narration at 0 dB)."""
        from hypogeum.agents.video_renderer import _MUSIC_AMPLITUDE_FACTOR
        # −14 dB = 10^(−14/20) ≈ 0.200
        expected = 10 ** (-14 / 20)
        assert abs(_MUSIC_AMPLITUDE_FACTOR - expected) < 0.01

    def test_section_without_music_uses_narration_only(self, tmp_path):
        """When no music track, ffmpeg command uses narration audio only."""
        from hypogeum.agents.video_renderer import _build_ffmpeg_concat_command

        narration_path = tmp_path / "narration.mp3"
        narration_path.write_bytes(b"\xff\xfb" * 100)

        cmd = _build_ffmpeg_concat_command(
            timeline=[],
            narration_path=narration_path,
            music_track=None,  # No music
            output_path=tmp_path / "out.mp4",
            resolution="1920x1080",
            episode_id="ep_001",
        )

        cmd_str = " ".join(str(c) for c in cmd)
        # Should not have amix (no music to mix)
        assert "amix" not in cmd_str
        # Should use volume=1.0 for narration
        assert "volume=1.0" in cmd_str

    def test_mixing_produces_valid_ffmpeg_args(self, tmp_path):
        """When music + narration, ffmpeg command includes amix."""
        from hypogeum.agents.video_renderer import _build_ffmpeg_concat_command, _MUSIC_AMPLITUDE_FACTOR

        narration_path = tmp_path / "narration.mp3"
        narration_path.write_bytes(b"\xff\xfb" * 100)

        music_dir = tmp_path / "music"
        music_dir.mkdir()
        music_path = music_dir / "mystery_track.mp3"
        music_path.write_bytes(b"\xff\xfb" * 100)

        track = MusicTrackResult(
            track_id="tr1",
            category="Mystery",
            asset_id="channel/music/mystery/tr1",
            local_path=music_path,
            source="ai_generated",
            duration_seconds=120.0,
        )

        cmd = _build_ffmpeg_concat_command(
            timeline=[],
            narration_path=narration_path,
            music_track=track,
            output_path=tmp_path / "out.mp4",
            resolution="1920x1080",
            episode_id="ep_001",
        )

        cmd_str = " ".join(str(c) for c in cmd)
        assert "amix" in cmd_str
        assert f"volume={_MUSIC_AMPLITUDE_FACTOR}" in cmd_str


# ---------------------------------------------------------------------------
# TestFfmpegExecutionLayer
# ---------------------------------------------------------------------------

class TestFfmpegExecutionLayer:

    def test_main_episode_rendered_to_correct_path(self, tmp_path):
        """Main episode renders to episodes/{episode_id}/main_{episode_id}.mp4."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()) as mock_sub:
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "ep_001",
                )

        ep_render = result.get("episode_render", {})
        assert "main_ep_001" in ep_render.get("video_asset_id", "")

    def test_shorts_rendered_to_correct_paths(self, tmp_path):
        """Shorts render to episodes/{episode_id}/short_{i}_{episode_id}.mp4."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "ep_001",
                )

        shorts_renders = result.get("shorts_renders", [])
        assert len(shorts_renders) == 3
        for i, sr in enumerate(shorts_renders):
            vid = sr.get("video_asset_id", "")
            assert f"short_{i}_ep_001" in vid or vid == ""

    def test_ffmpeg_nonzero_exit_sets_render_failed(self, tmp_path):
        """When ffmpeg exits non-zero, render_status reflects failure."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        def _fail_ffmpeg(cmd, capture_output, text, timeout):
            result = MagicMock()
            result.returncode = 1
            result.stderr = "ffmpeg: error: invalid codec"
            result.stdout = ""
            return result

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_fail_ffmpeg):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "ep_001",
                )

        # With all ffmpeg calls failing, production_ready should be False
        assert result["package_integrity"]["production_ready"] is False

    def test_ffmpeg_failure_issues_pdr(self, tmp_path):
        """ffmpeg failure results in a package with render failure recorded."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        def _fail_ffmpeg(cmd, capture_output, text, timeout):
            r = MagicMock()
            r.returncode = 1
            r.stderr = "error"
            r.stdout = ""
            return r

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_fail_ffmpeg):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "ep_001",
                )

        qa = result.get("assembly_qa_report", {})
        render_check = next(
            (c for c in qa.get("checks", []) if c.get("check_type") == "render_status"),
            None
        )
        assert render_check is not None
        # Render failed — status should be fail or warn (not pass)
        assert render_check.get("status") in ("fail", "warn")


# ---------------------------------------------------------------------------
# TestFinalRenderPackage
# ---------------------------------------------------------------------------

class TestFinalRenderPackage:

    def _run_full_success(self, tmp_path):
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        gen_track = _make_music_track_result(tmp_path=tmp_path)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=gen_track,
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                return run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "ep_001",
                )

    def test_render_status_success_when_all_clips_present(self, tmp_path):
        """With all clips present and ffmpeg success, render status is success."""
        result = self._run_full_success(tmp_path)
        qa = result.get("assembly_qa_report", {})
        # All clips present → render_check should be pass
        render_check = next(
            (c for c in qa.get("checks", []) if c.get("check_type") == "render_status"),
            None
        )
        assert render_check is not None
        assert render_check.get("status") in ("pass", "warn")

    def test_render_status_partial_when_some_clips_missing(self, tmp_path):
        """With some clips missing (but ≥80%), render_status is partial."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        scenes = visual["scenes"]
        # Create 4 out of 5 clips
        for scene in scenes[:4]:
            aid = scene.get("asset_id", "")
            if aid:
                clip_path = assets_root / f"{aid}.mp4"
                clip_path.parent.mkdir(parents=True, exist_ok=True)
                clip_path.write_bytes(b"FAKEVIDEO" * 100)
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "ep_001",
                )

        qa = result.get("assembly_qa_report", {})
        assert qa.get("clip_availability_rate", 1.0) < 1.0

    def test_render_status_failed_when_ffmpeg_fails(self, tmp_path):
        """When ffmpeg fails, production_ready is False."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        def _fail(cmd, capture_output, text, timeout):
            r = MagicMock()
            r.returncode = 1
            r.stderr = "error"
            r.stdout = ""
            return r

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_fail):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "ep_001",
                )

        assert result["package_integrity"]["production_ready"] is False

    def test_video_assets_list_populated(self, tmp_path):
        """episode_render.video_asset_id is populated on successful render."""
        result = self._run_full_success(tmp_path)
        ep_render = result.get("episode_render", {})
        assert ep_render.get("video_asset_id") != ""

    def test_music_log_in_package(self, tmp_path):
        """music_log is present and contains log entries."""
        result = self._run_full_success(tmp_path)
        music_log = result.get("music_log", [])
        assert isinstance(music_log, list)
        # Each entry must have required schema fields
        for entry in music_log:
            assert "log_id" in entry
            assert "source" in entry

    def test_qa_report_clip_availability_rate(self, tmp_path):
        """assembly_qa_report contains clip_availability_rate."""
        result = self._run_full_success(tmp_path)
        qa = result.get("assembly_qa_report", {})
        assert "clip_availability_rate" in qa
        assert 0.0 <= qa["clip_availability_rate"] <= 1.0

    def test_human_review_recommended_when_sections_lack_music(self, tmp_path):
        """When sections lack music, human_review_recommended is True."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            side_effect=ElevenLabsError("Music failed"),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "ep_001",
                )

        qa = result.get("assembly_qa_report", {})
        assert qa.get("human_review_recommended") is True
        assert "sections_without_music" in qa.get("human_review_reasons", [])

    def test_human_review_recommended_when_partial_clips(self, tmp_path):
        """When some clips are missing (but above threshold), human_review_recommended is True."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        scenes = visual["scenes"]
        # 4/5 = 0.80 → partial
        for scene in scenes[:4]:
            aid = scene.get("asset_id", "")
            if aid:
                clip_path = assets_root / f"{aid}.mp4"
                clip_path.parent.mkdir(parents=True, exist_ok=True)
                clip_path.write_bytes(b"FAKEVIDEO" * 100)
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "ep_001",
                )

        qa = result.get("assembly_qa_report", {})
        # Missing clips → human review recommended
        assert qa.get("human_review_recommended") is True


# ---------------------------------------------------------------------------
# TestProductionReadyEvaluation
# ---------------------------------------------------------------------------

class TestProductionReadyEvaluation:

    def test_production_ready_true_when_all_files_exist(self, tmp_path):
        """production_ready is True when main + all shorts exist and sizes > 0."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "ep_001",
                )

        assert result["package_integrity"]["production_ready"] is True

    def test_production_ready_false_when_main_missing(self, tmp_path):
        """production_ready is False when main episode file is missing."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        def _fail_main(cmd, capture_output, text, timeout):
            # Fail only for main, succeed for shorts
            r = MagicMock()
            if "main_" in str(cmd[-1]):
                r.returncode = 1
                r.stderr = "error"
            else:
                out = Path(cmd[-1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"FAKEMP4" * 1000)
                r.returncode = 0
                r.stderr = ""
            r.stdout = ""
            return r

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_fail_main):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "ep_001",
                )

        assert result["package_integrity"]["production_ready"] is False

    def test_production_ready_not_blocked_when_short_missing(self, tmp_path):
        """Phase 2: missing short does NOT block production_ready (main episode unaffected)."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        def _fail_first_short(cmd, capture_output, text, timeout):
            r = MagicMock()
            path_str = str(cmd[-1])
            if "short_0_" in path_str:
                # First short fails — don't create file
                r.returncode = 1
                r.stderr = "error"
            else:
                out = Path(path_str)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"FAKEMP4" * 1000)
                r.returncode = 0
                r.stderr = ""
            r.stdout = ""
            return r

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_fail_first_short):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "ep_001",
                )

        # Phase 2: missing shorts do NOT block main episode
        assert result["package_integrity"]["production_ready"] is True

    def test_production_ready_false_when_render_failed(self, tmp_path):
        """production_ready is False when render_status is 'failed'."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        # No clips → availability below threshold → failed
        assets_root = tmp_path / ".hypogeum-assets"
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            result = run_assembly_agent(
                visual_package=visual,
                narration_package=narration,
                thumbnail_package=thumbnails,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path / "ep_001",
            )

        assert result["package_integrity"]["production_ready"] is False


# ---------------------------------------------------------------------------
# TestPipelineIntegration
# ---------------------------------------------------------------------------

class TestPipelineIntegration:

    def _make_minimal_packages(self):
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()
        return visual, narration, thumbnails

    def test_assembly_receives_visual_narration_thumbnail_packages(self, tmp_path):
        """Assembly agent is called with visual, narration, and thumbnail packages."""
        from hypogeum.pipeline import _run_assembly_agent

        visual, narration, thumbnails = self._make_minimal_packages()
        cfg = _make_config()

        fake_result = {
            "package_metadata": {"render_package_id": "rp_ep", "episode_id": "ep"},
            "episode_render": {"video_asset_id": "", "caption_asset_id": ""},
            "shorts_renders": [],
            "thumbnail_references": [{"thumbnail_asset_id": "t", "format": "16:9"}],
            "music_log": [],
            "metadata_source": {
                "episode_title": "", "ai_disclosure_text": "", "section_titles": []
            },
            "assembly_qa_report": {"overall_status": "pass"},
            "package_integrity": {"production_ready": False},
        }

        with patch(
            "hypogeum.pipeline.run_assembly_agent",
            return_value=fake_result,
        ) as mock_asm:
            result = _run_assembly_agent(
                visual_package=visual,
                narration_package=narration,
                thumbnail_package=thumbnails,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        mock_asm.assert_called_once()
        call_kwargs = mock_asm.call_args[1]
        assert call_kwargs["visual_package"] is visual
        assert call_kwargs["narration_package"] is narration
        assert call_kwargs["thumbnail_package"] is thumbnails

    def test_assembly_returns_final_render_package(self, tmp_path):
        """Assembly agent returns a final_render_package dict."""
        from hypogeum.pipeline import _run_assembly_agent

        visual, narration, thumbnails = self._make_minimal_packages()
        cfg = _make_config()

        fake_result = {"package_metadata": {"render_package_id": "rp_001", "episode_id": "ep_001"}}

        with patch("hypogeum.pipeline.run_assembly_agent", return_value=fake_result):
            result = _run_assembly_agent(
                visual_package=visual,
                narration_package=narration,
                thumbnail_package=thumbnails,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert result is fake_result

    def test_assembly_pdr_on_below_threshold(self, tmp_path):
        """When clip availability < 60%, assembly returns failed package (not exception)."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        # No clips on disk at all
        assets_root = tmp_path / ".hypogeum-assets"
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            result = run_assembly_agent(
                visual_package=visual,
                narration_package=narration,
                thumbnail_package=thumbnails,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path / "ep_001",
            )

        # Must return a package, not raise
        assert isinstance(result, dict)
        assert result["package_integrity"]["production_ready"] is False


# ---------------------------------------------------------------------------
# TestFailedPackageMetadataWiring
# Regression tests for ep_003 QARejectedError — assembly failed packages must
# wire episode_title, description, and caption_asset_id from upstream packages.
# ---------------------------------------------------------------------------

class TestFailedPackageMetadataWiring:
    """Regression: when _build_failed_package is called (clip_rate < 60%), metadata must still be wired."""

    def _run_below_threshold(self, tmp_path, topic_brief=None, narration=None):
        cfg = _make_config()
        visual = _make_visual_package()
        narration = narration or _make_narration_package()
        thumbnails = _make_thumbnail_package()
        assets_root = tmp_path / ".hypogeum-assets"
        # No clips on disk → rate = 0.0 → halts
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            return run_assembly_agent(
                visual_package=visual,
                narration_package=narration,
                thumbnail_package=thumbnails,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path / "episodes" / "ep_001",
                topic_brief=topic_brief,
            )

    def test_failed_package_episode_title_from_topic_brief(self, tmp_path):
        """_build_failed_package wires episode_title from topic_brief when visual_package lacks it.

        ep_003's visual_package.episode_metadata has no episode_title key (it has topic/primary_period
        instead), so topic_brief is the only source of truth.
        """
        cfg = _make_config()
        # Build a visual package without episode_title in episode_metadata (matches ep_003 reality)
        visual = _make_visual_package()
        visual["episode_metadata"].pop("episode_title", None)
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()
        topic_brief = {
            "episode_title": "The Antikythera Mechanism: Who Taught the Greeks the Sky?",
            "logline": "The device is the last trace of a vanished tradition.",
        }

        assets_root = tmp_path / ".hypogeum-assets"
        # No clips → rate = 0.0 → halts below threshold → _build_failed_package called
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            result = run_assembly_agent(
                visual_package=visual,
                narration_package=narration,
                thumbnail_package=thumbnails,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path / "episodes" / "ep_001",
                topic_brief=topic_brief,
            )

        title = result["metadata_source"]["episode_title"]
        assert title == "The Antikythera Mechanism: Who Taught the Greeks the Sky?", (
            f"episode_title not wired from topic_brief; got {title!r}"
        )

    def test_failed_package_description_from_topic_brief_logline(self, tmp_path):
        """_build_failed_package wires description from topic_brief.logline."""
        topic_brief = {
            "episode_title": "Test Title",
            "logline": "The device is the last trace of a vanished tradition.",
        }
        result = self._run_below_threshold(tmp_path, topic_brief=topic_brief)
        description = result["metadata_source"].get("description", "")
        assert description == "The device is the last trace of a vanished tradition.", (
            f"description not wired from topic_brief.logline; got {description!r}"
        )

    def test_failed_package_description_empty_without_topic_brief(self, tmp_path):
        """_build_failed_package produces empty description when no topic_brief provided."""
        result = self._run_below_threshold(tmp_path, topic_brief=None)
        # Should be empty string, not missing key or error
        description = result["metadata_source"].get("description", "MISSING")
        assert description == "", f"Expected empty description, got {description!r}"

    def test_failed_package_caption_asset_id_from_narration_package(self, tmp_path):
        """_build_failed_package wires caption_asset_id from narration_package.captions."""
        narration = _make_narration_package("ep_001")
        result = self._run_below_threshold(tmp_path, narration=narration)
        caption_id = result["episode_render"]["caption_asset_id"]
        assert caption_id == "episodes/ep_001/captions/main", (
            f"caption_asset_id not wired from narration_package; got {caption_id!r}"
        )

    def test_failed_package_captions_included_flag(self, tmp_path):
        """package_integrity.captions_included=True when narration_package has captions."""
        narration = _make_narration_package("ep_001")
        result = self._run_below_threshold(tmp_path, narration=narration)
        assert result["package_integrity"]["captions_included"] is True

    def test_failed_package_human_review_reasons_no_duplicate(self, tmp_path):
        """_build_failed_package does not duplicate human_review_reasons entries."""
        result = self._run_below_threshold(tmp_path)
        hrr = result["assembly_qa_report"]["human_review_reasons"]
        assert hrr.count("clip_availability_below_threshold") == 1, (
            f"Duplicate human_review_reasons: {hrr}"
        )

    def test_normal_path_description_populated(self, tmp_path):
        """Normal assembly path populates metadata_source.description from topic_brief.logline."""
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()
        topic_brief = {
            "episode_title": "The Antikythera Mechanism",
            "logline": "The last trace of a vanished tradition.",
        }

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "episodes" / "ep_001",
                    topic_brief=topic_brief,
                )

        description = result["metadata_source"].get("description", "")
        assert description == "The last trace of a vanished tradition.", (
            f"Normal path: description not wired from topic_brief.logline; got {description!r}"
        )


# ---------------------------------------------------------------------------
# TestInternalHelpers
# ---------------------------------------------------------------------------

class TestInternalHelpers:

    def test_collect_thumbnail_references_populates(self):
        """_collect_thumbnail_references extracts main and short thumbnails."""
        tp = _make_thumbnail_package()
        refs = _collect_thumbnail_references(tp)
        assert any(r["format"] == "16:9" for r in refs)
        short_refs = [r for r in refs if r.get("short_id") is not None]
        assert len(short_refs) == 3

    def test_find_stitched_track_main(self):
        """Find main episode stitched track by source_type."""
        tracks = [
            {"track_id": "t1", "audio_asset_id": "episodes/ep/narration/main_narration", "source_type": "main_episode"},
            {"track_id": "t2", "audio_asset_id": "episodes/ep/narration/sh_narration", "source_type": "short"},
        ]
        result = _find_stitched_track_asset_id(tracks, "main_episode")
        assert result == "episodes/ep/narration/main_narration"

    def test_find_caption_asset_id_main_episode(self):
        """Real caption entries (see narration_package['captions']) never carry
        a source_type key — only caption_id/srt_asset_id/caption_method/
        has_content. main_episode lookup falls back to the 'main' substring
        check in srt_asset_id."""
        captions = [
            {"caption_id": "cap_main", "srt_asset_id": "episodes/ep/captions/main"},
            {"caption_id": "cap_s1", "srt_asset_id": "episodes/ep/captions/sh_short_001"},
        ]
        result = _find_caption_asset_id(captions, "main_episode")
        assert result == "episodes/ep/captions/main"

    def test_find_caption_asset_id_short_resolves_correctly(self):
        """
        Regression: 2026-08-01 quality rejection. Against real caption data
        (no source_type key), this previously returned None for every
        Short unconditionally — confirmed via a real final_render_package.json
        where all three Shorts had caption_asset_id="". Burned-in captions
        (video_renderer._build_caption_ass) depend on this resolving
        correctly, or no caption ever gets burned in.
        """
        captions = [
            {"caption_id": "cap_main", "srt_asset_id": "episodes/ep/captions/main"},
            {"caption_id": "cap_s1", "srt_asset_id": "episodes/ep/captions/sh_short_001"},
            {"caption_id": "cap_s2", "srt_asset_id": "episodes/ep/captions/sh_short_002"},
            {"caption_id": "cap_s3", "srt_asset_id": "episodes/ep/captions/sh_short_003"},
        ]
        assert _find_caption_asset_id(captions, "short", "short_001") == "episodes/ep/captions/sh_short_001"
        assert _find_caption_asset_id(captions, "short", "short_002") == "episodes/ep/captions/sh_short_002"
        assert _find_caption_asset_id(captions, "short", "short_003") == "episodes/ep/captions/sh_short_003"

    def test_find_caption_asset_id_unmatched_short_returns_none(self):
        captions = [{"caption_id": "cap_s1", "srt_asset_id": "episodes/ep/captions/sh_short_001"}]
        assert _find_caption_asset_id(captions, "short", "short_999") is None

    def test_find_caption_asset_id_empty_list_returns_none(self):
        assert _find_caption_asset_id([], "short", "short_001") is None


# ---------------------------------------------------------------------------
# TestComputeChapterTimestamps
# ---------------------------------------------------------------------------

class TestComputeChapterTimestamps:
    """Tests for _compute_chapter_timestamps() — chapter timestamp derivation."""

    def _make_script_sections(self):
        return [
            {
                "section_id": "s01",
                "title": "Introduction",
                "narration_beats": [
                    {"beat_id": "b_001"},
                    {"beat_id": "b_002"},
                ],
            },
            {
                "section_id": "s02",
                "title": "The Mystery",
                "narration_beats": [
                    {"beat_id": "b_003"},
                ],
            },
            {
                "section_id": "s03",
                "title": "The Reveal",
                "narration_beats": [
                    {"beat_id": "b_004"},
                ],
            },
        ]

    def _make_narration_segments(self):
        return [
            {"beat_reference": "b_001", "duration_seconds": 10.0},
            {"beat_reference": "b_002", "duration_seconds": 15.0},
            {"beat_reference": "b_003", "duration_seconds": 20.0},
            {"beat_reference": "b_004", "duration_seconds": 12.0},
        ]

    def test_first_section_starts_at_zero(self):
        result = _compute_chapter_timestamps(
            self._make_script_sections(),
            self._make_narration_segments(),
        )
        assert result[0]["start_seconds"] == 0.0

    def test_second_section_starts_after_first(self):
        result = _compute_chapter_timestamps(
            self._make_script_sections(),
            self._make_narration_segments(),
        )
        # Introduction has b_001 (10s) + b_002 (15s) = 25s
        assert result[1]["start_seconds"] == 25.0

    def test_third_section_starts_after_second(self):
        result = _compute_chapter_timestamps(
            self._make_script_sections(),
            self._make_narration_segments(),
        )
        # Introduction 25s + The Mystery 20s = 45s
        assert result[2]["start_seconds"] == 45.0

    def test_preserves_section_titles(self):
        result = _compute_chapter_timestamps(
            self._make_script_sections(),
            self._make_narration_segments(),
        )
        titles = [r["title"] for r in result]
        assert titles == ["Introduction", "The Mystery", "The Reveal"]

    def test_preserves_section_ids(self):
        result = _compute_chapter_timestamps(
            self._make_script_sections(),
            self._make_narration_segments(),
        )
        ids = [r["section_id"] for r in result]
        assert ids == ["s01", "s02", "s03"]

    def test_empty_script_sections_returns_empty(self):
        result = _compute_chapter_timestamps([], self._make_narration_segments())
        assert result == []

    def test_missing_beat_in_segments_defaults_to_zero_duration(self):
        sections = [
            {
                "section_id": "s01",
                "title": "Section A",
                "narration_beats": [{"beat_id": "b_unknown"}],
            },
            {
                "section_id": "s02",
                "title": "Section B",
                "narration_beats": [],
            },
        ]
        result = _compute_chapter_timestamps(sections, [])
        # Both start at 0 — missing beats contribute 0s
        assert result[0]["start_seconds"] == 0.0
        assert result[1]["start_seconds"] == 0.0

    def test_empty_narration_segments_all_start_at_zero(self):
        result = _compute_chapter_timestamps(
            self._make_script_sections(),
            [],
        )
        assert all(r["start_seconds"] == 0.0 for r in result)

    def test_section_with_no_beats_does_not_advance_cursor(self):
        sections = [
            {
                "section_id": "s01",
                "title": "No Beats",
                "narration_beats": [],
            },
            {
                "section_id": "s02",
                "title": "Has Beats",
                "narration_beats": [{"beat_id": "b_001"}],
            },
        ]
        segs = [{"beat_reference": "b_001", "duration_seconds": 30.0}]
        result = _compute_chapter_timestamps(sections, segs)
        assert result[0]["start_seconds"] == 0.0
        assert result[1]["start_seconds"] == 0.0  # No beats before → starts at 0


# ---------------------------------------------------------------------------
# TestFormatSources
# ---------------------------------------------------------------------------

class TestFormatSources:
    """Tests for _format_sources() — source_registry formatting."""

    def test_formats_title_and_url(self):
        registry = [{"title": "Wikipedia", "url": "https://en.wikipedia.org/wiki/Atlantis"}]
        result = _format_sources(registry)
        assert len(result) == 1
        assert result[0]["title"] == "Wikipedia"
        assert result[0]["url"] == "https://en.wikipedia.org/wiki/Atlantis"

    def test_skips_entries_with_no_title_or_url(self):
        registry = [{"source_id": "s001"}, {"title": "Valid", "url": "https://example.com"}]
        result = _format_sources(registry)
        assert len(result) == 1
        assert result[0]["title"] == "Valid"

    def test_entry_with_url_only_included(self):
        registry = [{"url": "https://example.com"}]
        result = _format_sources(registry)
        assert len(result) == 1
        assert result[0]["url"] == "https://example.com"
        assert result[0]["title"] == ""

    def test_empty_registry_returns_empty(self):
        assert _format_sources([]) == []

    def test_strips_whitespace(self):
        registry = [{"title": "  Source  ", "url": "  https://x.com  "}]
        result = _format_sources(registry)
        assert result[0]["title"] == "Source"
        assert result[0]["url"] == "https://x.com"

    def test_ignores_non_dict_entries(self):
        registry = ["not_a_dict", {"title": "Valid", "url": "https://x.com"}]
        result = _format_sources(registry)
        assert len(result) == 1

    def test_multiple_sources_preserved_in_order(self):
        registry = [
            {"title": "A", "url": "https://a.com"},
            {"title": "B", "url": "https://b.com"},
            {"title": "C", "url": "https://c.com"},
        ]
        result = _format_sources(registry)
        assert [r["title"] for r in result] == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# TestMetadataSourceSourceAttribution
# ---------------------------------------------------------------------------

class TestMetadataSourceSourceAttribution:
    """
    Integration tests: sources field is present in metadata_source when
    source_registry is passed to run_assembly_agent().
    """

    def test_sources_populated_from_source_registry(self, tmp_path):
        from unittest.mock import patch, MagicMock
        source_reg = [
            {"title": "Ancient Civ Journal", "url": "https://acj.example.com"},
            {"title": "Archaeology Today", "url": "https://at.example.com"},
        ]
        ep_id = "ep_src_test"
        config = _make_config()
        vp = _make_visual_package(episode_id=ep_id)
        np_ = _make_narration_package(episode_id=ep_id)
        tp = _make_thumbnail_package()

        # Create required asset files
        assets_root = tmp_path / ".hypogeum-assets"
        assets_root.mkdir()
        ep_dir = tmp_path / ".hypogeum-assets" / "episodes" / ep_id
        ep_dir.mkdir(parents=True)
        for scene in vp["scenes"]:
            aid = scene.get("asset_id", "")
            if aid:
                p = assets_root / f"{aid}.mp4"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"FAKEMP4")
        for track in np_["stitched_tracks"]:
            aid = track["audio_asset_id"]
            p = assets_root / f"{aid}.mp3"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"FAKEMP3")
        for cap in np_["captions"]:
            aid = cap["srt_asset_id"]
            p = assets_root / f"{aid}.srt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")

        with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
            result = run_assembly_agent(
                visual_package=vp,
                narration_package=np_,
                thumbnail_package=tp,
                config=config,
                episode_id=ep_id,
                output_dir=ep_dir,
                source_registry=source_reg,
            )

        sources = result["metadata_source"]["sources"]
        assert len(sources) == 2
        assert sources[0]["title"] == "Ancient Civ Journal"
        assert sources[0]["url"] == "https://acj.example.com"

    def test_sources_empty_when_no_source_registry(self, tmp_path):
        from unittest.mock import patch
        ep_id = "ep_no_src"
        config = _make_config()
        vp = _make_visual_package(episode_id=ep_id)
        np_ = _make_narration_package(episode_id=ep_id)
        tp = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        assets_root.mkdir()
        ep_dir = tmp_path / ".hypogeum-assets" / "episodes" / ep_id
        ep_dir.mkdir(parents=True)
        for scene in vp["scenes"]:
            aid = scene.get("asset_id", "")
            if aid:
                p = assets_root / f"{aid}.mp4"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"FAKEMP4")
        for track in np_["stitched_tracks"]:
            aid = track["audio_asset_id"]
            p = assets_root / f"{aid}.mp3"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"FAKEMP3")
        for cap in np_["captions"]:
            aid = cap["srt_asset_id"]
            p = assets_root / f"{aid}.srt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")

        with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
            result = run_assembly_agent(
                visual_package=vp,
                narration_package=np_,
                thumbnail_package=tp,
                config=config,
                episode_id=ep_id,
                output_dir=ep_dir,
            )

        assert result["metadata_source"]["sources"] == []


# ---------------------------------------------------------------------------
# TestClipAvailabilityIntegration
# ---------------------------------------------------------------------------

class TestClipAvailabilityIntegration:
    """Integration test: assembly sees non-zero clip_availability_rate via video/ subpath."""

    def test_clip_availability_rate_nonzero_from_video_subdir(self, tmp_path):
        """Assembly sees non-zero clip_availability_rate when clips exist at video/sc_*.mp4."""
        cfg = _make_config()

        # Build visual package with NO explicit asset_id on scenes — mirrors post-generation state
        # where asset_manifest.assets has entries but scene-level asset_id is absent
        ep_id = "ep_clip_test"
        scenes = [
            {
                "scene_id": "s01",
                "scene_index": 0,
                "source_type": "main_episode",
                "short_id": None,
                "narration_reference": "b_001",
                "production_spec": {"duration_seconds": 8},
                "narrative_signals": [],
                "section_reference": "sec_001",
            },
        ]
        asset_entries = [
            {
                "scene_id": "s01",
                "short_id": None,
                "asset_id": f"episodes/{ep_id}/video/sc_s01",
                "generation_status": "generated",
                "provider": "muapi",
                "model": "veo3-image-to-video",
                "source_type": "main_episode",
                "used_in_scenes": ["s01"],
            }
        ]
        vp = {
            "package_metadata": {
                "visual_package_id": f"vp_{ep_id}",
                "script_package_id": ep_id,
                "episode_id": ep_id,
                "topic_id": ep_id,
                "vpd_version": "v1.3",
            },
            "episode_metadata": {
                "primary_period": "Test",
                "secondary_periods": [],
                "tone_arc": "test",
                "visual_theme": "test",
                "scene_count_main_episode": 1,
                "scene_count_shorts": 0,
                "total_planned_duration_seconds": 8,
            },
            "visual_continuity_record": {
                "locked_entities": [],
                "period_palettes": [],
            },
            "scenes": scenes,
            "asset_manifest": {"assets": asset_entries},
            "provider_routing_manifest": {
                "scenes_by_provider": {"muapi": 1},
                "scenes_by_tier": {"T1": 1},
                "fallback_triggers_anticipated": 0,
                "capability_gaps": [],
                "scenes_generated": 1,
                "scenes_failed": 0,
            },
            "visual_integrity_log": {"flag_summary": {}, "flags": []},
            "thumbnail_handoff_brief": {
                "episode_id": ep_id,
                "main_thumbnail": {
                    "concept": "test",
                    "primary_subject": "test",
                    "composition": "test",
                    "text_overlay": "test",
                    "style_reference": "test",
                },
                "shorts_thumbnails": [],
            },
        }
        np_ = _make_narration_package(episode_id=ep_id)
        tp = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        # Place clip at the correct video/ subdir path (no scene-level asset_id)
        clip_path = assets_root / f"episodes/{ep_id}/video/sc_s01.mp4"
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        clip_path.write_bytes(b"FAKEVIDEO" * 100)
        _setup_narration_on_disk(np_, assets_root)

        ep_dir = tmp_path / "episodes" / ep_id
        ep_dir.mkdir(parents=True, exist_ok=True)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=vp,
                    narration_package=np_,
                    thumbnail_package=tp,
                    config=cfg,
                    episode_id=ep_id,
                    output_dir=ep_dir,
                )

        qa = result.get("assembly_qa_report", {})
        assert qa.get("clip_availability_rate", 0.0) > 0.0, (
            f"Expected non-zero clip_availability_rate; got {qa.get('clip_availability_rate')}"
        )


# ---------------------------------------------------------------------------
# TestHoldBasedAssembly — Strategy B: intentional per-beat freeze holds
# ---------------------------------------------------------------------------

class TestHoldBasedAssembly:
    """
    Integration tests for Strategy B hold-based assembly:
    QA duration coverage checks verified end-to-end.
    Unit tests for _build_ffmpeg_concat_command are in test_video_renderer.py.
    """

    def test_qa_duration_coverage_missing_clip_does_not_cause_fail(self, tmp_path):
        """
        A missing clip does NOT cause c_duration_coverage to fail.
        Missing clips are already captured in scenes_missing and clip_availability_rate.
        Only an existing-but-too-short clip with hold > 20s causes a failure.

        Setup: 5 scenes (2 main + 3 shorts), only 1 main clip present.
        Main narration: b_001=4.5s (clip present) + b_002=5.5s (clip MISSING).
        Hold for sc_001: max(0, 4.5 - actual_clip_dur). Probe fails on fake file → 0 hold.
        Missing sc_002 contributes 0 to hold_gap (skip in existing-clips computation).
        → c_duration_coverage should be 'pass' (no hold gap from existing clips > 20s).
        Clip availability: 4/5 = 80% → assembly proceeds (above 60% threshold).
        """
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        scenes = visual["scenes"]

        # Create clips for scenes[0] (main sc_001) and all three shorts, skip scenes[1] (main sc_002)
        for scene in [scenes[0], scenes[2], scenes[3], scenes[4]]:
            aid = scene.get("asset_id", "")
            if aid:
                clip_path = assets_root / f"{aid}.mp4"
                clip_path.parent.mkdir(parents=True, exist_ok=True)
                clip_path.write_bytes(b"FAKEVIDEO" * 100)
        # scenes[1] (main sc_002, b_002=5.5s beat) has NO clip file

        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            side_effect=ElevenLabsError("no music"),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "episodes" / "ep_001",
                )

        qa = result.get("assembly_qa_report", {})
        dur_check = next(
            (c for c in qa.get("checks", []) if c.get("check_type") == "duration_coverage"),
            None,
        )
        assert dur_check is not None, "c_duration_coverage check missing from qa_report"
        # Missing clip (sc_002) must NOT drive duration_coverage to 'fail'.
        # It is already captured in scenes_missing / clip_availability_rate.
        assert dur_check.get("status") in ("pass", "warn"), (
            f"Missing clip should not cause duration_coverage fail; got: {dur_check}"
        )

    def test_qa_duration_coverage_check_passes_when_all_clips_present(self, tmp_path):
        """
        When all clips are present (with tpad filling each beat),
        c_duration_coverage check status is 'pass' and gap is 0.
        """
        cfg = _make_config()
        visual = _make_visual_package()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "episodes" / "ep_001",
                )

        qa = result.get("assembly_qa_report", {})
        dur_check = next(
            (c for c in qa.get("checks", []) if c.get("check_type") == "duration_coverage"),
            None,
        )
        assert dur_check is not None, "c_duration_coverage check missing from qa_report"
        assert dur_check.get("status") == "pass", (
            f"Expected pass; detail: {dur_check.get('detail')}"
        )


class TestTopicBriefTitleFallback:
    """
    Verify that run_assembly_agent uses topic_brief as fallback for episode_title
    and logline when visual_package.episode_metadata does not carry those fields.
    """

    def _vp_without_title(self) -> dict:
        """Standard visual package with episode_title/logline stripped from ep_meta."""
        vp = _make_visual_package()
        vp["episode_metadata"].pop("episode_title", None)
        vp["episode_metadata"].pop("logline", None)
        return vp

    def test_episode_title_from_topic_brief_when_ep_meta_empty(self, tmp_path):
        """episode_title comes from topic_brief when ep_meta is missing it."""
        cfg = _make_config()
        vp = self._vp_without_title()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(vp, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        tb = {"episode_title": "The Great Mystery", "logline": "Nobody knows."}

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=vp,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "ep_001",
                    topic_brief=tb,
                )

        assert result["metadata_source"]["episode_title"] == "The Great Mystery"
        assert result["metadata_source"]["logline"] == "Nobody knows."

    def test_ep_meta_takes_precedence_over_topic_brief(self, tmp_path):
        """episode_title from ep_meta takes precedence when both are non-empty."""
        cfg = _make_config()
        vp = _make_visual_package()
        vp["episode_metadata"]["episode_title"] = "Meta Title"
        vp["episode_metadata"]["logline"] = "Meta logline."
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(vp, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        tb = {"episode_title": "Brief Title", "logline": "Brief logline."}

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=vp,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "ep_001",
                    topic_brief=tb,
                )

        assert result["metadata_source"]["episode_title"] == "Meta Title"
        assert result["metadata_source"]["logline"] == "Meta logline."

    def test_no_topic_brief_keeps_empty_string(self, tmp_path):
        """Without topic_brief and without ep_meta fields, episode_title stays empty."""
        cfg = _make_config()
        vp = self._vp_without_title()
        narration = _make_narration_package()
        thumbnails = _make_thumbnail_package()

        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(vp, assets_root, "ep_001")
        _setup_narration_on_disk(narration, assets_root)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                result = run_assembly_agent(
                    visual_package=vp,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path / "ep_001",
                )

        assert result["metadata_source"]["episode_title"] == ""


# ---------------------------------------------------------------------------
# TestAssemblyManifestContract — M4.2 required validation scenarios
# ---------------------------------------------------------------------------

class TestAssemblyManifestContract:
    """
    Required contract tests for the Assembly Agent manifest layer.

    Verifies:
      1. Successful manifest creation from valid inputs.
      2. Missing required package → failed manifest, no providers called.
      3. Malformed package (empty dict, missing key) → failed manifest.
      4. No provider (subprocess/music) calls when validation fails.
      5. Deterministic structural output for identical inputs.

    All tests run without ffmpeg or real providers. All filesystem ops use
    tmp_path. No content is generated.
    """

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _full_setup(self, tmp_path):
        """Return config + packages + assets_root with all files on disk."""
        cfg = _make_config()
        vp = _make_visual_package()
        np_ = _make_narration_package()
        tp = _make_thumbnail_package()
        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(vp, assets_root, "ep_001")
        _setup_narration_on_disk(np_, assets_root)
        mtp = _make_music_track_package(tmp_path=tmp_path)
        return cfg, vp, np_, tp, mtp

    def _run_with_mocks(self, cfg, vp, np_, tp, tmp_path, mtp=None, **extra_kwargs):
        """Run assembly_agent with subprocess and music mocked out."""
        kwargs = dict(
            visual_package=vp,
            narration_package=np_,
            thumbnail_package=tp,
            config=cfg,
            episode_id="ep_001",
            output_dir=tmp_path / "episodes" / "ep_001",
        )
        if mtp is not None:
            kwargs["music_track_package"] = mtp
        kwargs.update(extra_kwargs)
        with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
            return run_assembly_agent(**kwargs)

    # -----------------------------------------------------------------------
    # 1. Successful manifest creation
    # -----------------------------------------------------------------------

    def test_successful_manifest_has_required_top_level_keys(self, tmp_path):
        """A valid run produces a manifest dict with all required top-level keys."""
        cfg, vp, np_, tp, mtp = self._full_setup(tmp_path)
        result = self._run_with_mocks(cfg, vp, np_, tp, tmp_path, mtp=mtp)

        required_keys = {
            "package_metadata",
            "episode_render",
            "shorts_renders",
            "thumbnail_references",
            "music_log",
            "metadata_source",
            "assembly_qa_report",
            "package_integrity",
        }
        assert required_keys <= set(result.keys()), (
            f"missing keys: {required_keys - set(result.keys())}"
        )

    def test_successful_manifest_package_metadata_has_episode_id(self, tmp_path):
        cfg, vp, np_, tp, mtp = self._full_setup(tmp_path)
        result = self._run_with_mocks(cfg, vp, np_, tp, tmp_path, mtp=mtp)
        assert result["package_metadata"]["episode_id"] == "ep_001"

    def test_successful_manifest_with_upstream_packages(self, tmp_path):
        """Passing research_package and narrative_package as valid dicts succeeds."""
        cfg, vp, np_, tp, mtp = self._full_setup(tmp_path)
        result = self._run_with_mocks(
            cfg, vp, np_, tp, tmp_path, mtp=mtp,
            research_package={"source_registry": [], "claims": []},
            narrative_package={"narrative_arc": "mystery", "sections": []},
            topic_brief={"episode_title": "Test", "logline": "Test logline."},
            script_package={"sections": [], "title": "Test"},
        )
        assert result["package_metadata"]["episode_id"] == "ep_001"
        assert result["package_integrity"]["production_ready"] is True

    # -----------------------------------------------------------------------
    # 2. Missing required package → failed manifest
    # -----------------------------------------------------------------------

    def test_none_visual_package_returns_failed_manifest(self, tmp_path):
        """visual_package=None must return a failed manifest without raising."""
        cfg = _make_config()
        np_ = _make_narration_package()
        tp = _make_thumbnail_package()

        result = run_assembly_agent(
            visual_package=None,  # type: ignore[arg-type]
            narration_package=np_,
            thumbnail_package=tp,
            config=cfg,
            episode_id="ep_missing_vp",
            output_dir=tmp_path / "ep_missing_vp",
        )

        assert isinstance(result, dict)
        assert result["package_integrity"]["production_ready"] is False
        assert result["assembly_qa_report"]["overall_status"] == "fail"

    def test_none_narration_package_returns_failed_manifest(self, tmp_path):
        """narration_package=None must return a failed manifest without raising."""
        cfg = _make_config()
        vp = _make_visual_package()
        tp = _make_thumbnail_package()

        result = run_assembly_agent(
            visual_package=vp,
            narration_package=None,  # type: ignore[arg-type]
            thumbnail_package=tp,
            config=cfg,
            episode_id="ep_missing_np",
            output_dir=tmp_path / "ep_missing_np",
        )

        assert isinstance(result, dict)
        assert result["package_integrity"]["production_ready"] is False

    def test_none_thumbnail_package_returns_failed_manifest(self, tmp_path):
        """thumbnail_package=None must return a failed manifest without raising."""
        cfg = _make_config()
        vp = _make_visual_package()
        np_ = _make_narration_package()

        result = run_assembly_agent(
            visual_package=vp,
            narration_package=np_,
            thumbnail_package=None,  # type: ignore[arg-type]
            config=cfg,
            episode_id="ep_missing_tp",
            output_dir=tmp_path / "ep_missing_tp",
        )

        assert isinstance(result, dict)
        assert result["package_integrity"]["production_ready"] is False

    # -----------------------------------------------------------------------
    # 3. Malformed package → failed manifest
    # -----------------------------------------------------------------------

    def test_empty_visual_package_returns_failed_manifest(self, tmp_path):
        """visual_package={} (missing 'scenes' key) → failed manifest."""
        cfg = _make_config()
        np_ = _make_narration_package()
        tp = _make_thumbnail_package()

        result = run_assembly_agent(
            visual_package={},
            narration_package=np_,
            thumbnail_package=tp,
            config=cfg,
            episode_id="ep_empty_vp",
            output_dir=tmp_path / "ep_empty_vp",
        )

        assert result["package_integrity"]["production_ready"] is False
        assert result["assembly_qa_report"]["overall_status"] == "fail"

    def test_visual_package_missing_scenes_key_returns_failed_manifest(self, tmp_path):
        """visual_package without 'scenes' key → validation fails immediately."""
        cfg = _make_config()
        np_ = _make_narration_package()
        tp = _make_thumbnail_package()

        malformed_vp = {"package_metadata": {"visual_package_id": "vp_001"}}

        result = run_assembly_agent(
            visual_package=malformed_vp,
            narration_package=np_,
            thumbnail_package=tp,
            config=cfg,
            episode_id="ep_malformed",
            output_dir=tmp_path / "ep_malformed",
        )

        assert result["package_integrity"]["production_ready"] is False
        assert result["assembly_qa_report"]["overall_status"] == "fail"

    def test_empty_narration_package_returns_failed_manifest(self, tmp_path):
        """narration_package={} (missing 'stitched_tracks') → failed manifest."""
        cfg = _make_config()
        vp = _make_visual_package()
        tp = _make_thumbnail_package()

        result = run_assembly_agent(
            visual_package=vp,
            narration_package={},
            thumbnail_package=tp,
            config=cfg,
            episode_id="ep_empty_np",
            output_dir=tmp_path / "ep_empty_np",
        )

        assert result["package_integrity"]["production_ready"] is False

    def test_malformed_optional_package_returns_failed_manifest(self, tmp_path):
        """Passing a non-dict optional package → validation fails immediately."""
        cfg = _make_config()
        vp = _make_visual_package()
        np_ = _make_narration_package()
        tp = _make_thumbnail_package()

        result = run_assembly_agent(
            visual_package=vp,
            narration_package=np_,
            thumbnail_package=tp,
            config=cfg,
            episode_id="ep_bad_opt",
            output_dir=tmp_path / "ep_bad_opt",
            research_package="not_a_dict",  # type: ignore[arg-type]
        )

        assert result["package_integrity"]["production_ready"] is False

    # -----------------------------------------------------------------------
    # 4. No provider calls when validation fails
    # -----------------------------------------------------------------------

    def test_no_subprocess_call_on_validation_failure(self, tmp_path):
        """When input validation fails, subprocess.run (ffmpeg) is never called."""
        cfg = _make_config()
        np_ = _make_narration_package()
        tp = _make_thumbnail_package()

        with patch("subprocess.run") as mock_sub, \
             patch("hypogeum.agents.music_agent.generate_music_track") as mock_music, \
             patch("hypogeum.agents.music_agent.run_music_agent") as mock_music_agent:
            run_assembly_agent(
                visual_package=None,  # type: ignore[arg-type]
                narration_package=np_,
                thumbnail_package=tp,
                config=cfg,
                episode_id="ep_no_provider",
                output_dir=tmp_path / "ep_no_provider",
            )

        mock_sub.assert_not_called()
        mock_music.assert_not_called()
        mock_music_agent.assert_not_called()

    def test_no_subprocess_call_on_malformed_package(self, tmp_path):
        """Malformed visual_package → ffmpeg never called."""
        cfg = _make_config()
        np_ = _make_narration_package()
        tp = _make_thumbnail_package()

        with patch("subprocess.run") as mock_sub:
            run_assembly_agent(
                visual_package={},
                narration_package=np_,
                thumbnail_package=tp,
                config=cfg,
                episode_id="ep_no_ffmpeg",
                output_dir=tmp_path / "ep_no_ffmpeg",
            )

        mock_sub.assert_not_called()

    # -----------------------------------------------------------------------
    # 5. Deterministic structural output
    # -----------------------------------------------------------------------

    def test_deterministic_package_keys_for_same_inputs(self, tmp_path):
        """Running assembly twice with the same inputs produces identical key structure."""
        cfg, vp, np_, tp, mtp = self._full_setup(tmp_path)

        result_a = self._run_with_mocks(cfg, vp, np_, tp, tmp_path, mtp=mtp)
        result_b = self._run_with_mocks(cfg, vp, np_, tp, tmp_path, mtp=mtp)

        # Top-level keys must be identical
        assert set(result_a.keys()) == set(result_b.keys())
        # package_metadata keys must be identical
        assert set(result_a["package_metadata"].keys()) == set(result_b["package_metadata"].keys())
        # episode_id is stable
        assert result_a["package_metadata"]["episode_id"] == result_b["package_metadata"]["episode_id"]
        # package_integrity is stable
        assert result_a["package_integrity"]["production_ready"] == result_b["package_integrity"]["production_ready"]

    def test_deterministic_shorts_count(self, tmp_path):
        """shorts_renders always contains exactly 3 entries regardless of short_id count."""
        cfg, vp, np_, tp, mtp = self._full_setup(tmp_path)
        result = self._run_with_mocks(cfg, vp, np_, tp, tmp_path, mtp=mtp)
        assert len(result["shorts_renders"]) == 3

    # -----------------------------------------------------------------------
    # 6. _validate_input_packages unit tests
    # -----------------------------------------------------------------------

    def test_validate_returns_none_for_valid_inputs(self):
        from hypogeum.agents.assembly_agent import _validate_input_packages
        vp = _make_visual_package()
        np_ = _make_narration_package()
        tp = _make_thumbnail_package()
        err = _validate_input_packages(vp, np_, tp, None, None, None, None)
        assert err is None

    def test_validate_returns_error_for_none_visual(self):
        from hypogeum.agents.assembly_agent import _validate_input_packages
        np_ = _make_narration_package()
        tp = _make_thumbnail_package()
        err = _validate_input_packages(None, np_, tp, None, None, None, None)
        assert err is not None
        assert "visual_package" in err

    def test_validate_returns_error_for_missing_scenes_key(self):
        from hypogeum.agents.assembly_agent import _validate_input_packages
        np_ = _make_narration_package()
        tp = _make_thumbnail_package()
        err = _validate_input_packages({"other": 1}, np_, tp, None, None, None, None)
        assert err is not None
        assert "scenes" in err

    def test_validate_returns_error_for_none_narration(self):
        from hypogeum.agents.assembly_agent import _validate_input_packages
        vp = _make_visual_package()
        tp = _make_thumbnail_package()
        err = _validate_input_packages(vp, None, tp, None, None, None, None)
        assert err is not None
        assert "narration_package" in err

    def test_validate_returns_error_for_non_dict_optional(self):
        from hypogeum.agents.assembly_agent import _validate_input_packages
        vp = _make_visual_package()
        np_ = _make_narration_package()
        tp = _make_thumbnail_package()
        err = _validate_input_packages(vp, np_, tp, "not_a_dict", None, None, None)
        assert err is not None
        assert "script_package" in err

    def test_validate_passes_with_all_optional_packages(self):
        from hypogeum.agents.assembly_agent import _validate_input_packages
        vp = _make_visual_package()
        np_ = _make_narration_package()
        tp = _make_thumbnail_package()
        err = _validate_input_packages(
            vp, np_, tp,
            script_package={"sections": []},
            topic_brief={"episode_title": "Test"},
            research_package={"claims": []},
            narrative_package={"arc": "mystery"},
        )
        assert err is None


# ---------------------------------------------------------------------------
# TestShortsRendersNotPlaceholder
# Regression: when VPD produces real short scenes (source_type=short) and
# the mp4 assets exist on disk, assembly must set non-empty video_asset_id
# on shorts_renders (not placeholder stubs). When no short scenes exist,
# all 3 slots must be placeholder stubs.
# ---------------------------------------------------------------------------

class TestShortsRendersNotPlaceholder:

    def _run(self, cfg, visual, narration, thumbnails, tmp_path):
        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_sh")
        _setup_narration_on_disk(narration, assets_root)
        mtp = _make_music_track_package(episode_id="ep_sh", tmp_path=tmp_path)
        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                return run_assembly_agent(
                    visual_package=visual,
                    narration_package=narration,
                    thumbnail_package=thumbnails,
                    config=cfg,
                    episode_id="ep_sh",
                    output_dir=tmp_path / "ep_sh",
                )

    def test_real_short_scenes_produce_non_placeholder_video_asset_ids(self, tmp_path):
        """
        When visual_package includes scenes with source_type=short and the clip
        files exist on disk, assembly must write real video_asset_ids to
        shorts_renders (not empty placeholders).
        """
        cfg = _make_config()
        visual = _make_visual_package(episode_id="ep_sh")
        narration = _make_narration_package(episode_id="ep_sh")
        thumbnails = _make_thumbnail_package(episode_id="ep_sh")

        result = self._run(cfg, visual, narration, thumbnails, tmp_path)

        shorts_renders = result.get("shorts_renders", [])
        assert len(shorts_renders) == 3

        # The 3 real short IDs from _make_visual_package are sh_001, sh_002, sh_003
        real_shorts = [sr for sr in shorts_renders if "_placeholder" not in sr.get("short_id", "")]
        assert len(real_shorts) == 3, (
            f"Expected 3 real (non-placeholder) short renders, got: "
            f"{[sr['short_id'] for sr in shorts_renders]}"
        )
        for sr in real_shorts:
            assert sr["video_asset_id"] != "", (
                f"short {sr['short_id']} must have a non-empty video_asset_id "
                f"when clip assets exist on disk"
            )

    def test_no_short_scenes_produces_all_placeholder_shorts(self, tmp_path):
        """
        When visual_package has only main_episode scenes (no source_type=short),
        all 3 shorts_renders slots must be placeholder stubs.
        """
        cfg = _make_config()
        # Main-only visual package — no short scenes
        main_only_scenes = [
            {
                "scene_id": "sc_001", "scene_index": 0,
                "source_type": "main_episode",
                "narration_reference": "b_001",
                "asset_id": "episodes/ep_sh/sc_001",
                "production_spec": {"duration_seconds": 8},
                "narrative_signals": [], "section_reference": "s01",
            },
            {
                "scene_id": "sc_002", "scene_index": 1,
                "source_type": "main_episode",
                "narration_reference": "b_002",
                "asset_id": "episodes/ep_sh/sc_002",
                "production_spec": {"duration_seconds": 8},
                "narrative_signals": [], "section_reference": "s01",
            },
        ]
        visual = _make_visual_package(scenes=main_only_scenes, episode_id="ep_sh")
        narration = _make_narration_package(episode_id="ep_sh")
        thumbnails = _make_thumbnail_package(episode_id="ep_sh")

        result = self._run(cfg, visual, narration, thumbnails, tmp_path)

        shorts_renders = result.get("shorts_renders", [])
        assert len(shorts_renders) == 3

        for sr in shorts_renders:
            assert "_placeholder" in sr["short_id"], (
                f"Expected placeholder short_id, got {sr['short_id']!r}"
            )
            assert sr["video_asset_id"] == "", (
                f"Placeholder short must have empty video_asset_id, got {sr['video_asset_id']!r}"
            )

    def test_placeholder_short_ids_not_counted_as_successful_deliverables(self, tmp_path):
        """
        Placeholder shorts must not appear as production_ready=True deliverables.
        Assembly's package_integrity.production_ready must not be affected by
        placeholder shorts missing their video files.
        """
        cfg = _make_config()
        visual = _make_visual_package(episode_id="ep_sh")
        narration = _make_narration_package(episode_id="ep_sh")
        thumbnails = _make_thumbnail_package(episode_id="ep_sh")

        result = self._run(cfg, visual, narration, thumbnails, tmp_path)

        # Real shorts from the visual package are present and have files → production_ready
        assert result["package_integrity"]["production_ready"] is True

        # Verify: placeholder shorts have scene_count=0 and empty video_asset_id
        placeholder_shorts = [
            sr for sr in result["shorts_renders"]
            if "_placeholder" in sr.get("short_id", "")
        ]
        for ph in placeholder_shorts:
            assert ph["scene_count"] == 0
            assert ph["video_asset_id"] == ""


class TestShortsMetadataGenerationWiring:
    """
    hypogeum.agents.assembly_agent's wiring to
    hypogeum.agents.shorts_metadata.generate_shorts_titles() — replaces
    the historical {"short_id": sid, "title": sid} placeholder with real,
    grounded titles. Every test injects a fake shorts_title_llm_call (no
    real OpenRouter/Claude call, no network, no cost).
    """

    SCRIPT_SHORTS = [
        {
            "short_id": "short_001",
            "narration_beats": [{"beat_id": "b_1", "text": "The tunnel narrows to thirty centimetres wide."}],
            "text_overlays": [{"text": "30 cm wide."}],
        },
        {
            "short_id": "short_002",
            "narration_beats": [{"beat_id": "b_2", "text": "A tomb six thousand years old was found beneath the site."}],
            "text_overlays": [{"text": "6,000 years old."}],
        },
        {
            "short_id": "short_003",
            "narration_beats": [{"beat_id": "b_3", "text": "No medieval document ever mentions these structures."}],
            "text_overlays": [{"text": "Zero documents."}],
        },
    ]

    def _run(self, tmp_path, *, script_package=None, shorts_title_llm_call=None):
        cfg = _make_config()
        visual = _make_visual_package(episode_id="ep_sh_meta")
        narration = _make_narration_package(episode_id="ep_sh_meta")
        thumbnails = _make_thumbnail_package(episode_id="ep_sh_meta")
        assets_root = tmp_path / ".hypogeum-assets"
        _setup_clips_on_disk(visual, assets_root, "ep_sh_meta")
        _setup_narration_on_disk(narration, assets_root)
        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_music_track_result(tmp_path=tmp_path),
        ):
            with patch("subprocess.run", side_effect=_mock_ffmpeg_success()):
                return run_assembly_agent(
                    visual_package=visual, narration_package=narration, thumbnail_package=thumbnails,
                    config=cfg, episode_id="ep_sh_meta", output_dir=tmp_path / "ep_sh_meta",
                    script_package=script_package,
                    shorts_title_llm_call=shorts_title_llm_call,
                )

    def test_real_titles_replace_the_historical_placeholder(self, tmp_path):
        # The real short_ids in this default visual package are
        # sh_001/sh_002/sh_003 -- assembly re-keys script_package's
        # positional shorts_scripts entries to these before calling the
        # title generator, so the fake must respond keyed the same way.
        def _fake(system_prompt, input_package, *, config, episode_id, channel_id):
            return {"titles": [
                {"short_id": s["short_id"], "title": f"A Real Title For {s['short_id']}"}
                for s in input_package["shorts"]
            ]}

        result = self._run(
            tmp_path,
            script_package={"sections": [], "shorts_scripts": self.SCRIPT_SHORTS},
            shorts_title_llm_call=_fake,
        )
        shorts_titles = result["metadata_source"]["shorts_titles"]
        assert len(shorts_titles) == 3
        for entry in shorts_titles:
            assert entry["title"] != entry["short_id"], "must never be the historical placeholder"
            assert entry["title"].startswith("A Real Title For")

    def test_no_script_package_degrades_to_empty_title_never_placeholder(self, tmp_path):
        result = self._run(tmp_path, script_package=None)
        shorts_titles = result["metadata_source"]["shorts_titles"]
        assert len(shorts_titles) == 3
        for entry in shorts_titles:
            assert entry["title"] == ""
            assert entry["title"] != entry["short_id"]

    def test_generation_failure_degrades_to_empty_title_never_placeholder(self, tmp_path):
        def _raising(system_prompt, input_package, *, config, episode_id, channel_id):
            raise RuntimeError("simulated provider failure")

        result = self._run(
            tmp_path,
            script_package={"sections": [], "shorts_scripts": self.SCRIPT_SHORTS},
            shorts_title_llm_call=_raising,
        )
        shorts_titles = result["metadata_source"]["shorts_titles"]
        assert len(shorts_titles) == 3
        for entry in shorts_titles:
            assert entry["title"] == ""
            assert entry["title"] != entry["short_id"]

    def test_placeholder_returned_by_model_is_rejected_and_degrades_safely(self, tmp_path):
        # A misbehaving model that returns the historical placeholder
        # pattern itself must never leak through -- generate_shorts_titles
        # rejects it, and assembly degrades to empty rather than accepting it.
        def _placeholder_fake(system_prompt, input_package, *, config, episode_id, channel_id):
            return {"titles": [{"short_id": s["short_id"], "title": s["short_id"]} for s in input_package["shorts"]]}

        result = self._run(
            tmp_path,
            script_package={"sections": [], "shorts_scripts": self.SCRIPT_SHORTS},
            shorts_title_llm_call=_placeholder_fake,
        )
        shorts_titles = result["metadata_source"]["shorts_titles"]
        for entry in shorts_titles:
            assert entry["title"] == ""
