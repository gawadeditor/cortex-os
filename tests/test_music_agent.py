"""
tests/test_music_agent.py — Unit tests for hypogeum/agents/music_agent.py

All generate_music_track() calls are mocked. No real ElevenLabs API calls.

Test coverage:
  TestRunMusicAgent           — end-to-end sourcing, status, schema contract
  TestThreeConsecutiveFailure — provider_unavailable safety mechanism
  TestMusicLogEntries         — music_log[] structure and fallback_trigger values
  TestSectionCategories       — tone_arc → category mapping
  TestSchemaContract          — validate() called; package fields present
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.config import Config
from hypogeum.elevenlabs import ElevenLabsError, MusicTrackResult
from hypogeum.agents.music_agent import (
    _build_section_categories,
    _music_log_entry,
    _tone_arc_to_category,
    run_music_agent,
)
from hypogeum.schemas import validate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config() -> Config:
    env = {
        "OPENROUTER_API_KEY": "or-key",
        "ELEVENLABS_API_KEY": "xi-key",
        "ELEVENLABS_VOICE_ID": "voice-abc",
    }
    with patch.dict(os.environ, env, clear=True):
        return Config()


def _make_visual_package(tone_arc: str = "Ancient mystery") -> dict:
    return {
        "package_metadata": {"visual_package_id": "vp_ep_001", "episode_id": "ep_001"},
        "episode_metadata": {
            "episode_title": "Test",
            "tone_arc": tone_arc,
            "topic_id": "t_001",
        },
        "scenes": [],
    }


def _make_track_result(
    source: str = "ai_generated",
    tmp_path: Optional[Path] = None,
) -> MusicTrackResult:
    if tmp_path:
        music_dir = tmp_path / ".hypogeum-assets" / "channel" / "music" / "mystery"
        music_dir.mkdir(parents=True, exist_ok=True)
        local = music_dir / "track_abc.mp3"
        local.write_bytes(b"\xff\xfb" * 5000)
    else:
        local = Path("/tmp/test_music_track.mp3")
    return MusicTrackResult(
        track_id="track-abc",
        category="Mystery",
        asset_id="channel/music/mystery/track_abc",
        local_path=local,
        source=source,
        duration_seconds=120.0,
        prompt_used="slow atmospheric dark underscore",
    )


# ---------------------------------------------------------------------------
# TestRunMusicAgent
# ---------------------------------------------------------------------------

class TestRunMusicAgent:

    def test_all_sections_sourced_returns_complete_status(self, tmp_path):
        """All sections successfully sourced → sourcing_status = 'complete'."""
        cfg = _make_config()
        visual = _make_visual_package()
        section_durations = {"main_episode": 60.0, "short_sh_001": 30.0}

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_track_result(tmp_path=tmp_path),
        ):
            result = run_music_agent(
                visual_package=visual,
                episode_id="ep_001",
                config=cfg,
                assets_root=tmp_path / ".hypogeum-assets",
                section_durations=section_durations,
            )

        assert result["sourcing_status"] == "complete"
        assert len(result["tracks"]) == 2
        assert result["sections_without_music"] == []

    def test_partial_failure_returns_partial_status(self, tmp_path):
        """One section fails → sourcing_status = 'partial'."""
        cfg = _make_config()
        visual = _make_visual_package()
        section_durations = {"main_episode": 60.0, "short_sh_001": 30.0}

        call_count = {"n": 0}
        def _side_effect(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _make_track_result(tmp_path=tmp_path)
            raise ElevenLabsError("second section failed")

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            side_effect=_side_effect,
        ):
            result = run_music_agent(
                visual_package=visual,
                episode_id="ep_001",
                config=cfg,
                assets_root=tmp_path / ".hypogeum-assets",
                section_durations=section_durations,
            )

        assert result["sourcing_status"] == "partial"
        assert len(result["tracks"]) == 1
        assert len(result["sections_without_music"]) == 1

    def test_all_sections_fail_returns_failed_status(self, tmp_path):
        """All sections fail → sourcing_status = 'failed'."""
        cfg = _make_config()
        visual = _make_visual_package()
        section_durations = {"main_episode": 60.0}

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            side_effect=ElevenLabsError("API error"),
        ):
            result = run_music_agent(
                visual_package=visual,
                episode_id="ep_001",
                config=cfg,
                assets_root=tmp_path / ".hypogeum-assets",
                section_durations=section_durations,
            )

        assert result["sourcing_status"] == "failed"
        assert len(result["tracks"]) == 0
        assert "main_episode" in result["sections_without_music"]

    def test_empty_section_durations_returns_complete(self, tmp_path):
        """No sections requested → complete (vacuously)."""
        cfg = _make_config()
        visual = _make_visual_package()

        result = run_music_agent(
            visual_package=visual,
            episode_id="ep_001",
            config=cfg,
            assets_root=tmp_path / ".hypogeum-assets",
            section_durations={},
        )

        assert result["sourcing_status"] == "complete"
        assert result["tracks"] == {}

    def test_track_dict_fields_present(self, tmp_path):
        """Each entry in tracks[] has all required fields."""
        cfg = _make_config()
        visual = _make_visual_package()
        section_durations = {"main_episode": 60.0}

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_track_result(tmp_path=tmp_path),
        ):
            result = run_music_agent(
                visual_package=visual,
                episode_id="ep_001",
                config=cfg,
                assets_root=tmp_path / ".hypogeum-assets",
                section_durations=section_durations,
            )

        track = result["tracks"]["main_episode"]
        for field in ("section_key", "category", "asset_id", "local_path", "source", "duration_seconds", "prompt_used"):
            assert field in track

    def test_episode_id_in_package(self, tmp_path):
        """Package contains the episode_id."""
        cfg = _make_config()
        visual = _make_visual_package()

        result = run_music_agent(
            visual_package=visual,
            episode_id="ep_test_42",
            config=cfg,
            assets_root=tmp_path / ".hypogeum-assets",
            section_durations={},
        )

        assert result["episode_id"] == "ep_test_42"

    def test_ai_generated_source_recorded(self, tmp_path):
        """ai_generated source is preserved in track dict."""
        cfg = _make_config()
        visual = _make_visual_package()
        section_durations = {"main_episode": 60.0}

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_track_result(source="ai_generated", tmp_path=tmp_path),
        ):
            result = run_music_agent(
                visual_package=visual,
                episode_id="ep_001",
                config=cfg,
                assets_root=tmp_path / ".hypogeum-assets",
                section_durations=section_durations,
            )

        assert result["tracks"]["main_episode"]["source"] == "ai_generated"

    def test_channel_library_source_recorded(self, tmp_path):
        """channel_library source is preserved in track dict."""
        cfg = _make_config()
        visual = _make_visual_package()
        section_durations = {"main_episode": 60.0}

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_track_result(source="channel_library", tmp_path=tmp_path),
        ):
            result = run_music_agent(
                visual_package=visual,
                episode_id="ep_001",
                config=cfg,
                assets_root=tmp_path / ".hypogeum-assets",
                section_durations=section_durations,
            )

        assert result["tracks"]["main_episode"]["source"] == "channel_library"


# ---------------------------------------------------------------------------
# TestThreeConsecutiveFailure
# ---------------------------------------------------------------------------

class TestThreeConsecutiveFailure:

    def test_three_failures_marks_provider_unavailable(self, tmp_path):
        """After 3 consecutive failures, remaining sections are skipped."""
        cfg = _make_config()
        visual = _make_visual_package()
        # 5 sections; first 3 fail → provider marked unavailable; sections 4+5 skipped
        section_durations = {
            "main_episode": 60.0,
            "short_sh_001": 30.0,
            "short_sh_002": 30.0,
            "short_sh_003": 30.0,
            "short_sh_004": 30.0,
        }

        call_count = {"n": 0}
        def _always_fail(**kwargs):
            call_count["n"] += 1
            raise ElevenLabsError("always fails")

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            side_effect=_always_fail,
        ):
            result = run_music_agent(
                visual_package=visual,
                episode_id="ep_001",
                config=cfg,
                assets_root=tmp_path / ".hypogeum-assets",
                section_durations=section_durations,
            )

        # generate_music_track was called exactly 3 times (threshold), not 5
        assert call_count["n"] == 3
        # All 5 sections are in sections_without_music
        assert len(result["sections_without_music"]) == 5
        assert result["sourcing_status"] == "failed"

    def test_two_failures_does_not_mark_unavailable(self, tmp_path):
        """Two failures do not trigger provider_unavailable; third section is attempted."""
        cfg = _make_config()
        visual = _make_visual_package()
        section_durations = {
            "main_episode": 60.0,
            "short_sh_001": 30.0,
            "short_sh_002": 30.0,
        }

        call_count = {"n": 0}
        def _fail_twice_then_succeed(**kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise ElevenLabsError("fails")
            return _make_track_result(tmp_path=tmp_path)

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            side_effect=_fail_twice_then_succeed,
        ):
            result = run_music_agent(
                visual_package=visual,
                episode_id="ep_001",
                config=cfg,
                assets_root=tmp_path / ".hypogeum-assets",
                section_durations=section_durations,
            )

        # All 3 sections were attempted
        assert call_count["n"] == 3
        # Third section succeeded
        assert len(result["tracks"]) == 1
        assert result["sourcing_status"] == "partial"

    def test_failure_then_success_resets_counter(self, tmp_path):
        """A success after failure(s) resets the consecutive_failures counter."""
        cfg = _make_config()
        visual = _make_visual_package()
        # fail, succeed, fail, fail → only 2 consecutive failures at the end → still attempted
        section_durations = {
            "main_episode": 60.0,
            "short_sh_001": 30.0,
            "short_sh_002": 30.0,
            "short_sh_003": 30.0,
        }

        call_count = {"n": 0}
        def _alternating(**kwargs):
            call_count["n"] += 1
            n = call_count["n"]
            if n == 1:
                raise ElevenLabsError("fail 1")
            if n == 2:
                return _make_track_result(tmp_path=tmp_path)  # resets counter
            if n == 3:
                raise ElevenLabsError("fail 3")
            raise ElevenLabsError("fail 4")  # n=4

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            side_effect=_alternating,
        ):
            result = run_music_agent(
                visual_package=visual,
                episode_id="ep_001",
                config=cfg,
                assets_root=tmp_path / ".hypogeum-assets",
                section_durations=section_durations,
            )

        # All 4 sections were attempted (never reached 3 consecutive)
        assert call_count["n"] == 4
        # 1 succeeded
        assert len(result["tracks"]) == 1


# ---------------------------------------------------------------------------
# TestMusicLogEntries
# ---------------------------------------------------------------------------

class TestMusicLogEntries:

    def test_music_log_has_entry_per_section(self, tmp_path):
        """music_log has exactly one entry per section."""
        cfg = _make_config()
        visual = _make_visual_package()
        section_durations = {"main_episode": 60.0, "short_sh_001": 30.0}

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_track_result(tmp_path=tmp_path),
        ):
            result = run_music_agent(
                visual_package=visual,
                episode_id="ep_001",
                config=cfg,
                assets_root=tmp_path / ".hypogeum-assets",
                section_durations=section_durations,
            )

        assert len(result["music_log"]) == 2

    def test_failed_section_has_provider_failure_trigger(self, tmp_path):
        """Failed section has fallback_trigger = 'provider_failure' in music_log."""
        cfg = _make_config()
        visual = _make_visual_package()
        section_durations = {"main_episode": 60.0}

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            side_effect=ElevenLabsError("fail"),
        ):
            result = run_music_agent(
                visual_package=visual,
                episode_id="ep_001",
                config=cfg,
                assets_root=tmp_path / ".hypogeum-assets",
                section_durations=section_durations,
            )

        log_entry = result["music_log"][0]
        assert log_entry["fallback_trigger"] == "provider_failure"

    def test_unavailable_section_has_provider_unavailable_trigger(self, tmp_path):
        """Section skipped due to provider_unavailable has correct fallback_trigger."""
        cfg = _make_config()
        visual = _make_visual_package()
        # 4 sections: first 3 fail → 4th is skipped with provider_unavailable
        section_durations = {
            "main_episode": 60.0,
            "short_sh_001": 30.0,
            "short_sh_002": 30.0,
            "short_sh_003": 30.0,
        }

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            side_effect=ElevenLabsError("fail"),
        ):
            result = run_music_agent(
                visual_package=visual,
                episode_id="ep_001",
                config=cfg,
                assets_root=tmp_path / ".hypogeum-assets",
                section_durations=section_durations,
            )

        # 4th entry should have provider_unavailable trigger
        skipped_entries = [
            e for e in result["music_log"]
            if e.get("fallback_trigger") == "provider_unavailable"
        ]
        assert len(skipped_entries) >= 1

    def test_music_log_entry_structure(self):
        """_music_log_entry returns dict with all required fields."""
        entry = _music_log_entry(
            log_id="ml_001",
            placed_in="main_episode",
            category="Mystery",
            source="ai_generated",
            asset_id="channel/music/mystery/track",
            fallback_trigger=None,
        )
        assert entry["log_id"] == "ml_001"
        assert entry["placed_in"] == "main_episode"
        assert entry["category"] == "Mystery"
        assert entry["source"] == "ai_generated"
        assert entry["music_asset_id"] == "channel/music/mystery/track"
        assert entry["fallback_trigger"] is None
        assert "timeline_position_seconds" in entry


# ---------------------------------------------------------------------------
# TestSectionCategories
# ---------------------------------------------------------------------------

class TestSectionCategories:

    def test_ancient_tone_arc_maps_to_ancient_civilizations(self):
        assert _tone_arc_to_category("ancient ruins discovery") == "Ancient Civilizations"

    def test_mystery_tone_arc_maps_to_mystery(self):
        assert _tone_arc_to_category("mystery of the unknown") == "Mystery"

    def test_discover_tone_arc_maps_to_discovery(self):
        assert _tone_arc_to_category("explore and discover new worlds") == "Discovery"

    def test_investigation_tone_arc(self):
        assert _tone_arc_to_category("an investigation into events") == "Investigation"

    def test_tension_tone_arc(self):
        assert _tone_arc_to_category("urgent tension builds") == "Tension"

    def test_resolution_tone_arc(self):
        assert _tone_arc_to_category("a conclusion resolves") == "Resolution"

    def test_unknown_tone_arc_defaults_to_mystery(self):
        assert _tone_arc_to_category("unrelated topic") == "Mystery"

    def test_empty_tone_arc_defaults_to_mystery(self):
        assert _tone_arc_to_category("") == "Mystery"

    def test_build_section_categories_returns_all_keys(self):
        """All section keys are present in result."""
        keys = ["main_episode", "short_sh_001", "short_sh_002"]
        result = _build_section_categories(keys, "mystery of the ancient world")
        assert set(result.keys()) == set(keys)

    def test_build_section_categories_uses_tone_arc(self):
        """Category derived from tone_arc, not hardcoded."""
        result = _build_section_categories(["main_episode"], "discovery and exploration")
        assert result["main_episode"] == "Discovery"


# ---------------------------------------------------------------------------
# TestSchemaContract
# ---------------------------------------------------------------------------

class TestSchemaContract:

    def test_package_passes_schema_validation(self, tmp_path):
        """run_music_agent output passes validate('music_track_package')."""
        cfg = _make_config()
        visual = _make_visual_package()
        section_durations = {"main_episode": 60.0}

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            return_value=_make_track_result(tmp_path=tmp_path),
        ):
            result = run_music_agent(
                visual_package=visual,
                episode_id="ep_001",
                config=cfg,
                assets_root=tmp_path / ".hypogeum-assets",
                section_durations=section_durations,
            )

        # Must not raise
        validate("music_track_package", result)

    def test_failed_package_passes_schema_validation(self, tmp_path):
        """Package with all-failed sourcing also passes schema validation."""
        cfg = _make_config()
        visual = _make_visual_package()
        section_durations = {"main_episode": 60.0}

        with patch(
            "hypogeum.agents.music_agent.generate_music_track",
            side_effect=ElevenLabsError("fail"),
        ):
            result = run_music_agent(
                visual_package=visual,
                episode_id="ep_001",
                config=cfg,
                assets_root=tmp_path / ".hypogeum-assets",
                section_durations=section_durations,
            )

        validate("music_track_package", result)

    def test_required_top_level_fields_present(self, tmp_path):
        """package_id, episode_id, sourced_at, sourcing_status, tracks, music_log, sections_without_music."""
        cfg = _make_config()
        visual = _make_visual_package()

        result = run_music_agent(
            visual_package=visual,
            episode_id="ep_001",
            config=cfg,
            assets_root=tmp_path / ".hypogeum-assets",
            section_durations={},
        )

        for field in ("package_id", "episode_id", "sourced_at", "sourcing_status",
                      "tracks", "music_log", "sections_without_music"):
            assert field in result, f"Missing field: {field}"

    def test_sourcing_status_valid_values(self, tmp_path):
        """sourcing_status must be 'complete', 'partial', or 'failed'."""
        cfg = _make_config()
        visual = _make_visual_package()

        result = run_music_agent(
            visual_package=visual,
            episode_id="ep_001",
            config=cfg,
            assets_root=tmp_path / ".hypogeum-assets",
            section_durations={},
        )

        assert result["sourcing_status"] in ("complete", "partial", "failed")
