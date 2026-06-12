"""
tests/test_caption_agent.py — Unit tests for hypogeum/agents/caption_agent.py

All whisper_fallback calls are mocked. No real HTTP or OpenAI calls.

Test coverage:
  TestRunCaptionAgent         — happy path: package returned, files written (8 tests)
  TestCaptionMethod           — alignment_source drives caption_method (6 tests)
  TestWhisperFallback         — stitched-track whisper path (5 tests)
  TestShortsCaptions          — Short component handling; shorts=[] no short files (5 tests)
  TestCaptionChecks           — per-component content checks (4 tests)
  TestSchemaContract          — caption_package schema (5 tests)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.config import Config
from hypogeum.elevenlabs import NarrationSegmentResult, WordTiming
from hypogeum.agents.caption_agent import run_caption_agent, _build_srt_for_component
from hypogeum.schemas import SCHEMA_NAMES, validate, SchemaError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(openai_key: str = "sk-test") -> Config:
    env = {
        "OPENROUTER_API_KEY": "or-key",
        "ELEVENLABS_API_KEY": "xi-key",
        "ELEVENLABS_VOICE_ID": "voice-abc",
    }
    if openai_key:
        env["OPENAI_API_KEY"] = openai_key
    with patch.dict(os.environ, env, clear=True):
        return Config()


def _make_result(
    beat_id: str,
    tmp_path: Path,
    alignment_source: str = "tts",
    word_timings: Optional[List[WordTiming]] = None,
) -> NarrationSegmentResult:
    """Create a NarrationSegmentResult with a real audio file on disk."""
    narration_dir = tmp_path / "narration"
    narration_dir.mkdir(parents=True, exist_ok=True)
    audio_path = narration_dir / f"{beat_id}.mp3"
    audio_path.write_bytes(b"\xff\xfb\x90\x00" * 20)
    if word_timings is None:
        word_timings = [WordTiming("hello", 0.0, 0.5), WordTiming("world", 0.6, 1.0)]
    return NarrationSegmentResult(
        beat_id=beat_id,
        asset_id=f"episodes/ep_001/narration/{beat_id}",
        local_path=audio_path,
        duration_seconds=1.5,
        request_id=f"req_{beat_id}",
        character_cost=10,
        word_timings=word_timings,
        alignment_source=alignment_source,
    )


def _make_stitched_track(tmp_path: Path, name: str = "main_narration.mp3") -> Path:
    """Create a non-empty stitched track file."""
    narration_dir = tmp_path / "narration"
    narration_dir.mkdir(parents=True, exist_ok=True)
    track = narration_dir / name
    track.write_bytes(b"\xff\xfb\x90\x00" * 100)
    return track


def _run_main_only(tmp_path: Path, alignment_source: str = "tts") -> dict:
    """Run caption_agent with one main beat and no shorts."""
    cfg = _make_config()
    result = _make_result("b01", tmp_path, alignment_source)
    main_track = _make_stitched_track(tmp_path)
    return run_caption_agent(
        main_results=[result],
        shorts_results=[],
        main_track_path=main_track,
        short_track_paths=[],
        config=cfg,
        episode_id="ep_001",
        output_dir=tmp_path,
    )


# ---------------------------------------------------------------------------
# TestRunCaptionAgent
# ---------------------------------------------------------------------------

class TestRunCaptionAgent:

    def test_returns_dict(self, tmp_path):
        pkg = _run_main_only(tmp_path)
        assert isinstance(pkg, dict)

    def test_package_metadata_present(self, tmp_path):
        pkg = _run_main_only(tmp_path)
        assert pkg["package_metadata"]["caption_package_id"] == "cp_ep_001"
        assert pkg["package_metadata"]["episode_id"] == "ep_001"

    def test_captions_list_present(self, tmp_path):
        pkg = _run_main_only(tmp_path)
        assert isinstance(pkg["captions"], list)
        assert len(pkg["captions"]) == 1

    def test_main_caption_entry_fields(self, tmp_path):
        pkg = _run_main_only(tmp_path)
        cap = pkg["captions"][0]
        assert cap["caption_id"] == "cap_main_ep_001"
        assert cap["srt_asset_id"] == "episodes/ep_001/captions/main"
        assert cap["caption_method"] in ("elevenlabs_alignment", "whisper_fallback")
        assert isinstance(cap["has_content"], bool)

    def test_main_srt_file_written(self, tmp_path):
        _run_main_only(tmp_path)
        assert (tmp_path / "captions" / "main.srt").exists()

    def test_main_srt_has_content_when_word_timings_present(self, tmp_path):
        _run_main_only(tmp_path)
        srt = (tmp_path / "captions" / "main.srt").read_text()
        assert "-->" in srt

    def test_caption_checks_list_present(self, tmp_path):
        pkg = _run_main_only(tmp_path)
        assert isinstance(pkg["caption_checks"], list)
        assert len(pkg["caption_checks"]) == 1

    def test_caption_checks_main_component(self, tmp_path):
        pkg = _run_main_only(tmp_path)
        ck = pkg["caption_checks"][0]
        assert ck["check_id"] == "caption_content_main_episode"
        assert ck["component"] == "main_episode"
        assert ck["status"] in ("pass", "warn")
        assert isinstance(ck["detail"], str)


# ---------------------------------------------------------------------------
# TestCaptionMethod
# ---------------------------------------------------------------------------

class TestCaptionMethod:

    def test_tts_source_gives_elevenlabs_alignment(self, tmp_path):
        pkg = _run_main_only(tmp_path, alignment_source="tts")
        assert pkg["captions"][0]["caption_method"] == "elevenlabs_alignment"

    def test_forced_alignment_source_gives_elevenlabs_alignment(self, tmp_path):
        pkg = _run_main_only(tmp_path, alignment_source="forced_alignment")
        assert pkg["captions"][0]["caption_method"] == "elevenlabs_alignment"

    def test_none_source_gives_whisper_fallback(self, tmp_path):
        pkg = _run_main_only(tmp_path, alignment_source="none")
        assert pkg["captions"][0]["caption_method"] == "whisper_fallback"

    def test_whisper_source_gives_whisper_fallback(self, tmp_path):
        pkg = _run_main_only(tmp_path, alignment_source="whisper")
        assert pkg["captions"][0]["caption_method"] == "whisper_fallback"

    def test_mixed_sources_gives_whisper_fallback(self, tmp_path):
        """If any segment has degraded alignment, method = whisper_fallback."""
        cfg = _make_config()
        r1 = _make_result("b01", tmp_path, "tts")
        r2 = _make_result("b02", tmp_path, "none")
        track = _make_stitched_track(tmp_path)
        pkg = run_caption_agent(
            main_results=[r1, r2],
            shorts_results=[],
            main_track_path=track,
            short_track_paths=[],
            config=cfg,
            episode_id="ep_001",
            output_dir=tmp_path,
        )
        assert pkg["captions"][0]["caption_method"] == "whisper_fallback"

    def test_failed_beat_none_skipped_for_alignment(self, tmp_path):
        """None entries (failed beats) are excluded from alignment check."""
        cfg = _make_config()
        r1 = _make_result("b01", tmp_path, "tts")
        track = _make_stitched_track(tmp_path)
        pkg = run_caption_agent(
            main_results=[r1, None],  # second beat failed
            shorts_results=[],
            main_track_path=track,
            short_track_paths=[],
            config=cfg,
            episode_id="ep_001",
            output_dir=tmp_path,
        )
        # Only r1 (tts) is counted — None is skipped
        assert pkg["captions"][0]["caption_method"] == "elevenlabs_alignment"


# ---------------------------------------------------------------------------
# TestWhisperFallback
# ---------------------------------------------------------------------------

class TestWhisperFallback:

    def test_whisper_not_called_when_srt_has_content(self, tmp_path):
        """When ElevenLabs word_timings produce content, whisper is never called."""
        cfg = _make_config()
        result = _make_result("b01", tmp_path, "tts")  # has word_timings
        track = _make_stitched_track(tmp_path)
        with patch("hypogeum.agents.caption_agent.whisper_fallback") as mock_w:
            run_caption_agent(
                main_results=[result],
                shorts_results=[],
                main_track_path=track,
                short_track_paths=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )
        mock_w.assert_not_called()

    def test_whisper_not_called_when_no_openai_key(self, tmp_path):
        """No whisper call when OPENAI_API_KEY is absent."""
        cfg = _make_config(openai_key="")
        # Result with no word_timings → empty SRT
        result = _make_result("b01", tmp_path, "tts", word_timings=[])
        track = _make_stitched_track(tmp_path)
        with patch("hypogeum.agents.caption_agent.whisper_fallback") as mock_w:
            run_caption_agent(
                main_results=[result],
                shorts_results=[],
                main_track_path=track,
                short_track_paths=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )
        mock_w.assert_not_called()

    def test_whisper_not_called_when_track_missing(self, tmp_path):
        """No whisper call when stitched track does not exist."""
        cfg = _make_config()
        result = _make_result("b01", tmp_path, "tts", word_timings=[])
        missing_track = tmp_path / "narration" / "nonexistent.mp3"
        with patch("hypogeum.agents.caption_agent.whisper_fallback") as mock_w:
            run_caption_agent(
                main_results=[result],
                shorts_results=[],
                main_track_path=missing_track,
                short_track_paths=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )
        mock_w.assert_not_called()

    def test_whisper_called_when_srt_empty_and_track_exists(self, tmp_path):
        """Whisper fallback fires when no SRT content but track is on disk."""
        cfg = _make_config()
        result = _make_result("b01", tmp_path, "tts", word_timings=[])
        track = _make_stitched_track(tmp_path)
        mock_timings = [WordTiming("hi", 0.0, 0.5)]
        with patch(
            "hypogeum.agents.caption_agent.whisper_fallback",
            return_value=mock_timings,
        ):
            pkg = run_caption_agent(
                main_results=[result],
                shorts_results=[],
                main_track_path=track,
                short_track_paths=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )
        assert pkg["captions"][0]["caption_method"] == "whisper_fallback"

    def test_whisper_exception_handled_gracefully(self, tmp_path):
        """If whisper raises, SRT is empty but no exception propagates."""
        cfg = _make_config()
        result = _make_result("b01", tmp_path, "tts", word_timings=[])
        track = _make_stitched_track(tmp_path)
        with patch(
            "hypogeum.agents.caption_agent.whisper_fallback",
            side_effect=RuntimeError("OpenAI down"),
        ):
            pkg = run_caption_agent(
                main_results=[result],
                shorts_results=[],
                main_track_path=track,
                short_track_paths=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )
        # Should not raise; SRT may be empty
        assert isinstance(pkg, dict)


# ---------------------------------------------------------------------------
# TestShortsCaptions
# ---------------------------------------------------------------------------

class TestShortsCaptions:

    def _run_with_shorts(self, tmp_path: Path, n_shorts: int = 3) -> dict:
        cfg = _make_config()
        main_result = _make_result("main_b01", tmp_path, "tts")
        main_track = _make_stitched_track(tmp_path)

        shorts_results = []
        short_track_paths = []
        for i in range(1, n_shorts + 1):
            short_id = f"sh_00{i}"
            r = _make_result(f"sh{i}b01", tmp_path, "tts")
            shorts_results.append((short_id, [r]))
            track = _make_stitched_track(tmp_path, f"sh_{short_id}_narration.mp3")
            short_track_paths.append((short_id, track))

        return run_caption_agent(
            main_results=[main_result],
            shorts_results=shorts_results,
            main_track_path=main_track,
            short_track_paths=short_track_paths,
            config=cfg,
            episode_id="ep_001",
            output_dir=tmp_path,
        )

    def test_three_shorts_give_four_captions(self, tmp_path):
        pkg = self._run_with_shorts(tmp_path, 3)
        assert len(pkg["captions"]) == 4  # 1 main + 3 shorts

    def test_short_caption_ids_present(self, tmp_path):
        pkg = self._run_with_shorts(tmp_path, 3)
        cap_ids = {c["caption_id"] for c in pkg["captions"]}
        assert "cap_sh_001_ep_001" in cap_ids
        assert "cap_sh_002_ep_001" in cap_ids
        assert "cap_sh_003_ep_001" in cap_ids

    def test_short_srt_files_written(self, tmp_path):
        self._run_with_shorts(tmp_path, 3)
        assert (tmp_path / "captions" / "sh_sh_001.srt").exists()
        assert (tmp_path / "captions" / "sh_sh_002.srt").exists()
        assert (tmp_path / "captions" / "sh_sh_003.srt").exists()

    def test_no_shorts_gives_one_caption(self, tmp_path):
        """When shorts_results=[], only main caption is produced."""
        pkg = _run_main_only(tmp_path)
        assert len(pkg["captions"]) == 1

    def test_no_short_srt_files_when_no_shorts(self, tmp_path):
        _run_main_only(tmp_path)
        captions_dir = tmp_path / "captions"
        short_srts = list(captions_dir.glob("sh_*.srt"))
        assert short_srts == []


# ---------------------------------------------------------------------------
# TestCaptionChecks
# ---------------------------------------------------------------------------

class TestCaptionChecks:

    def test_pass_check_when_srt_has_content(self, tmp_path):
        pkg = _run_main_only(tmp_path, alignment_source="tts")
        ck = pkg["caption_checks"][0]
        assert ck["status"] == "pass"
        assert ck["has_content"] if "has_content" in ck else True

    def test_warn_check_when_srt_empty(self, tmp_path):
        """No word_timings and no whisper → empty SRT → warn check."""
        cfg = _make_config(openai_key="")  # no whisper fallback
        result = _make_result("b01", tmp_path, "tts", word_timings=[])
        track = _make_stitched_track(tmp_path)
        pkg = run_caption_agent(
            main_results=[result],
            shorts_results=[],
            main_track_path=track,
            short_track_paths=[],
            config=cfg,
            episode_id="ep_001",
            output_dir=tmp_path,
        )
        ck = pkg["caption_checks"][0]
        assert ck["status"] == "warn"

    def test_check_count_matches_component_count(self, tmp_path):
        """One check per component."""
        cfg = _make_config()
        main_result = _make_result("main_b01", tmp_path)
        main_track = _make_stitched_track(tmp_path)
        short_result = _make_result("sh1b01", tmp_path)
        short_track = _make_stitched_track(tmp_path, "sh_sh_001_narration.mp3")
        pkg = run_caption_agent(
            main_results=[main_result],
            shorts_results=[("sh_001", [short_result])],
            main_track_path=main_track,
            short_track_paths=[("sh_001", short_track)],
            config=cfg,
            episode_id="ep_001",
            output_dir=tmp_path,
        )
        assert len(pkg["caption_checks"]) == 2

    def test_check_component_field_set_correctly(self, tmp_path):
        pkg = _run_main_only(tmp_path)
        ck = pkg["caption_checks"][0]
        assert ck["component"] == "main_episode"


# ---------------------------------------------------------------------------
# TestSchemaContract
# ---------------------------------------------------------------------------

class TestSchemaContract:

    def test_schema_is_registered(self):
        assert "caption_package" in SCHEMA_NAMES

    def test_valid_package_passes_schema(self, tmp_path):
        pkg = _run_main_only(tmp_path)
        validate("caption_package", pkg)  # must not raise

    def test_missing_package_metadata_fails_schema(self, tmp_path):
        pkg = _run_main_only(tmp_path)
        del pkg["package_metadata"]
        with pytest.raises(SchemaError):
            validate("caption_package", pkg)

    def test_empty_captions_fails_schema(self, tmp_path):
        pkg = _run_main_only(tmp_path)
        pkg["captions"] = []
        with pytest.raises(SchemaError):
            validate("caption_package", pkg)

    def test_invalid_caption_method_fails_schema(self, tmp_path):
        pkg = _run_main_only(tmp_path)
        pkg["captions"][0]["caption_method"] = "invalid"
        with pytest.raises(SchemaError):
            validate("caption_package", pkg)
