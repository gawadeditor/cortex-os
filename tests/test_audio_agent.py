"""
tests/test_audio_agent.py — Unit tests for hypogeum/agents/audio_agent.py

All ElevenLabs calls are mocked. No real HTTP calls, no real API credits.

Test coverage:
  TestRunAudioAgentSuccess        — happy path: package returned, files written, schema valid
  TestBeatOrchestration           — sequential processing, prosody chaining, beat count
  TestShortsBeatGeneration        — Shorts beats processed after main, short source_type
  TestBeatFailureIsolation        — failed beat → None, subsequent beats still attempted
  TestAuthFailurePropagation      — ElevenLabsAuthFailure halts immediately
  TestQuotaExhaustedPropagation   — ElevenLabsQuotaExhausted halts immediately
  TestConfigValidation            — missing API key / voice_id raises AudioAgentError
  TestNarrationPackageStructure   — schema passes, stitched_tracks=4, captions=4
  TestStitchedTracks              — track count, source_types, asset_id format
  TestCaptions                    — caption count, caption_method values, srt_asset_id
  TestSrtGeneration               — SRT file written for main and each Short
  TestAlignmentCascadeIntegration — alignment_source propagated to segment records
  TestQaReport                    — pass / warn / fail thresholds, production_ready
  TestPackageWritten              — narration_package JSON written to disk
  TestInternalHelpers             — _collect_main_beats, _collect_shorts_beats, _make_segment_record
  TestBeatCap                     — SMOKE_TEST_MAX_NARRATION_BEATS caps main beats before VGO
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.config import Config
from hypogeum.elevenlabs import (
    ElevenLabsAuthFailure,
    ElevenLabsQuotaExhausted,
    NarrationSegmentResult,
    WordTiming,
)
from hypogeum.agents.audio_agent import (
    AudioAgentError,
    _collect_main_beats,
    _collect_shorts_beats,
    run_audio_agent,
)
from hypogeum.agents.voice_generation_operator import _make_segment_record
from hypogeum.schemas import validate


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

def _make_config(api_key: str = "test-xi", voice_id: str = "voice-abc") -> Config:
    env = {
        "OPENROUTER_API_KEY": "or-key",
        "ELEVENLABS_API_KEY": api_key,
        "ELEVENLABS_VOICE_ID": voice_id,
        "ELEVENLABS_TIMEOUT_SECONDS": "10",
        "OPENAI_API_KEY": "sk-test",
    }
    with patch.dict(os.environ, env, clear=True):
        return Config()


def _make_config_no_openai() -> Config:
    env = {
        "OPENROUTER_API_KEY": "or-key",
        "ELEVENLABS_API_KEY": "test-xi",
        "ELEVENLABS_VOICE_ID": "voice-abc",
        "ELEVENLABS_TIMEOUT_SECONDS": "10",
    }
    with patch.dict(os.environ, env, clear=True):
        return Config()


# Minimal valid script_package (exactly 3 shorts — required by schema)
_SCRIPT_PACKAGE = {
    "episode_metadata": {"episode_id": "ep_001", "episode_title": "Test Episode"},
    "sections": [
        {
            "section_id": "s01",
            "title": "Section One",
            "narration_beats": [
                {"beat_id": "s01b01", "text": "Hello world."},
                {"beat_id": "s01b02", "text": "Second beat."},
            ],
        },
        {
            "section_id": "s02",
            "title": "Section Two",
            "narration_beats": [
                {"beat_id": "s02b01", "text": "Third beat."},
            ],
        },
    ],
    "shorts_scripts": [
        {
            "short_id": "sh_001",
            "narration_beats": [{"beat_id": "sh1b01", "text": "Short one."}],
            "text_overlays": [],
        },
        {
            "short_id": "sh_002",
            "narration_beats": [{"beat_id": "sh2b01", "text": "Short two."}],
            "text_overlays": [],
        },
        {
            "short_id": "sh_003",
            "narration_beats": [{"beat_id": "sh3b01", "text": "Short three."}],
            "text_overlays": [],
        },
    ],
    "traceability_manifest": {},
    "script_integrity": {},
}


def _fake_result(
    beat_id: str,
    tmp_path: Path,
    alignment_source: str = "tts",
) -> NarrationSegmentResult:
    """Create a NarrationSegmentResult with a real file on disk."""
    narration_dir = tmp_path / "narration"
    narration_dir.mkdir(parents=True, exist_ok=True)
    audio_path = narration_dir / f"{beat_id}.mp3"
    audio_path.write_bytes(b"\xff\xfb\x90\x00" * 20)
    return NarrationSegmentResult(
        beat_id=beat_id,
        asset_id=f"episodes/ep_001/narration/{beat_id}",
        local_path=audio_path,
        duration_seconds=1.5,
        request_id=f"req_{beat_id}",
        character_cost=len(beat_id) + 5,
        word_timings=[
            WordTiming("hello", 0.0, 0.5),
            WordTiming("world", 0.6, 1.0),
        ],
        alignment_source=alignment_source,
    )


def _mock_generate(tmp_path: Path, alignment_source: str = "tts"):
    """Return a side_effect function that creates real files on disk."""
    call_order = []

    def _side_effect(beat_id, text, config, episode_id, output_dir, **kwargs):
        call_order.append(beat_id)
        return _fake_result(beat_id, output_dir, alignment_source)

    _side_effect.call_order = call_order
    return _side_effect


# ---------------------------------------------------------------------------
# TestRunAudioAgentSuccess
# ---------------------------------------------------------------------------

class TestRunAudioAgentSuccess:

    def test_returns_dict(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        assert isinstance(result, dict)

    def test_schema_validates(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        # Should not raise
        validate("narration_package", result)

    def test_package_metadata_fields(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        assert result["package_metadata"]["episode_id"] == "ep_001"
        assert "narration_package_id" in result["package_metadata"]

    def test_voice_configuration_populated(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        assert result["voice_configuration"]["voice_id"] == "voice-abc"
        assert result["voice_configuration"]["environment_source"] == "ELEVENLABS_VOICE_ID"

    def test_production_ready_true_on_no_failures(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        assert result["package_integrity"]["production_ready"] is True

    def test_qa_status_pass_on_no_failures(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        assert result["audio_qa_report"]["overall_status"] == "pass"


# ---------------------------------------------------------------------------
# TestBeatOrchestration
# ---------------------------------------------------------------------------

class TestBeatOrchestration:

    def test_generate_called_for_each_beat(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)) as mock_gen:
            run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        # 3 main beats + 3 short beats = 6 total
        assert mock_gen.call_count == 6

    def test_main_beats_processed_before_shorts(self, tmp_path):
        cfg = _make_config()
        call_order = []

        def _track(beat_id, text, config, episode_id, output_dir, **kw):
            call_order.append(beat_id)
            return _fake_result(beat_id, output_dir)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_track):
            run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        main_ids = {"s01b01", "s01b02", "s02b01"}
        short_ids = {"sh1b01", "sh2b01", "sh3b01"}

        main_positions = [i for i, b in enumerate(call_order) if b in main_ids]
        short_positions = [i for i, b in enumerate(call_order) if b in short_ids]

        assert max(main_positions) < min(short_positions)

    def test_prosody_chain_passed_to_subsequent_beats(self, tmp_path):
        cfg = _make_config()
        received_prev_ids = {}

        def _capture(beat_id, text, config, episode_id, output_dir,
                     previous_request_ids=None, **kw):
            received_prev_ids[beat_id] = list(previous_request_ids or [])
            return _fake_result(beat_id, output_dir)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_capture):
            run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        # First main beat gets no previous_request_ids
        assert received_prev_ids.get("s01b01") == []
        # Second main beat gets the request_id of the first
        assert "req_s01b01" in received_prev_ids.get("s01b02", [])

    def test_prosody_chain_capped_at_3(self, tmp_path):
        # Script package with 5 main beats to test the cap
        sp = {
            **_SCRIPT_PACKAGE,
            "sections": [
                {
                    "section_id": "s01",
                    "title": "S",
                    "narration_beats": [
                        {"beat_id": f"b{i:02d}", "text": f"Beat {i}."}
                        for i in range(5)
                    ],
                }
            ],
        }
        cfg = _make_config()
        received = {}

        def _capture(beat_id, text, config, episode_id, output_dir,
                     previous_request_ids=None, **kw):
            received[beat_id] = list(previous_request_ids or [])
            return _fake_result(beat_id, output_dir)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_capture):
            run_audio_agent(sp, cfg, "ep_001", tmp_path)

        # Beat 4 (index 4) should have at most 3 previous IDs
        assert len(received.get("b04", [])) <= 3

    def test_shorts_prosody_chain_resets_per_short(self, tmp_path):
        cfg = _make_config()
        received = {}

        def _capture(beat_id, text, config, episode_id, output_dir,
                     previous_request_ids=None, **kw):
            received[beat_id] = list(previous_request_ids or [])
            return _fake_result(beat_id, output_dir)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_capture):
            run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        # Each Short's first beat should have no previous IDs from main episode
        assert received.get("sh1b01") == []
        assert received.get("sh2b01") == []
        assert received.get("sh3b01") == []


# ---------------------------------------------------------------------------
# TestShortsBeatGeneration
# ---------------------------------------------------------------------------

class TestShortsBeatGeneration:

    def test_short_segments_have_short_source_type(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        short_segs = [
            s for s in result["narration_segments"]
            if s["source_type"] == "short"
        ]
        assert len(short_segs) == 3  # one beat per Short

    def test_main_segments_have_main_source_type(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        main_segs = [
            s for s in result["narration_segments"]
            if s["source_type"] == "main_episode"
        ]
        assert len(main_segs) == 3  # two from s01 + one from s02

    def test_all_shorts_represented_in_stitched_tracks(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        short_tracks = [
            t for t in result["stitched_tracks"]
            if t["source_type"] == "short"
        ]
        assert len(short_tracks) == 3

    def test_short_beat_references_in_segments(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        beat_refs = {s["beat_reference"] for s in result["narration_segments"]}
        assert "sh1b01" in beat_refs
        assert "sh2b01" in beat_refs
        assert "sh3b01" in beat_refs


# ---------------------------------------------------------------------------
# TestBeatFailureIsolation
# ---------------------------------------------------------------------------

class TestBeatFailureIsolation:

    def test_failed_beat_marked_as_failed(self, tmp_path):
        cfg = _make_config()
        call_n = {"n": 0}

        def _side(beat_id, text, config, episode_id, output_dir, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1:
                raise Exception("Network timeout")
            return _fake_result(beat_id, output_dir)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_side):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        failed_segs = [
            s for s in result["narration_segments"]
            if s["generation_status"] == "failed"
        ]
        assert len(failed_segs) == 1

    def test_subsequent_beats_still_processed_after_failure(self, tmp_path):
        cfg = _make_config()
        call_n = {"n": 0}

        def _side(beat_id, text, config, episode_id, output_dir, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1:
                raise Exception("First beat fails")
            return _fake_result(beat_id, output_dir)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_side) as mock_gen:
            run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        # All 6 beats attempted
        assert mock_gen.call_count == 6

    def test_failed_beat_has_none_audio_asset_id(self, tmp_path):
        cfg = _make_config()

        def _side(beat_id, text, config, episode_id, output_dir, **kw):
            if beat_id == "s01b01":
                raise Exception("failed")
            return _fake_result(beat_id, output_dir)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_side):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        failed = next(
            s for s in result["narration_segments"]
            if s["beat_reference"] == "s01b01"
        )
        assert failed["audio_asset_id"] is None

    def test_qa_report_counts_failures(self, tmp_path):
        cfg = _make_config()
        fail_count = {"n": 0}

        def _side(beat_id, text, config, episode_id, output_dir, **kw):
            fail_count["n"] += 1
            if fail_count["n"] == 1:
                raise Exception("failed")
            return _fake_result(beat_id, output_dir)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_side):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        assert result["audio_qa_report"]["beats_failed"] == 1
        assert result["audio_qa_report"]["beats_generated"] == 5


# ---------------------------------------------------------------------------
# TestAuthFailurePropagation
# ---------------------------------------------------------------------------

class TestAuthFailurePropagation:

    def test_auth_failure_propagates(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=ElevenLabsAuthFailure()):
            with pytest.raises(ElevenLabsAuthFailure):
                run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

    def test_auth_failure_on_second_beat_still_propagates(self, tmp_path):
        cfg = _make_config()
        call_n = {"n": 0}

        def _side(beat_id, text, config, episode_id, output_dir, **kw):
            call_n["n"] += 1
            if call_n["n"] == 2:
                raise ElevenLabsAuthFailure()
            return _fake_result(beat_id, output_dir)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_side):
            with pytest.raises(ElevenLabsAuthFailure):
                run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)


# ---------------------------------------------------------------------------
# TestQuotaExhaustedPropagation
# ---------------------------------------------------------------------------

class TestQuotaExhaustedPropagation:

    def test_quota_exhausted_propagates(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=ElevenLabsQuotaExhausted()):
            with pytest.raises(ElevenLabsQuotaExhausted):
                run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

    def test_quota_exhausted_mid_run_propagates(self, tmp_path):
        cfg = _make_config()
        call_n = {"n": 0}

        def _side(beat_id, text, config, episode_id, output_dir, **kw):
            call_n["n"] += 1
            if call_n["n"] == 3:
                raise ElevenLabsQuotaExhausted()
            return _fake_result(beat_id, output_dir)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_side):
            with pytest.raises(ElevenLabsQuotaExhausted):
                run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)


# ---------------------------------------------------------------------------
# TestConfigValidation
# ---------------------------------------------------------------------------

class TestConfigValidation:

    def test_missing_api_key_raises_audio_agent_error(self, tmp_path):
        cfg = _make_config(api_key="")
        with pytest.raises(AudioAgentError, match="ELEVENLABS_API_KEY"):
            run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

    def test_missing_voice_id_raises_audio_agent_error(self, tmp_path):
        cfg = _make_config(voice_id="")
        with pytest.raises(AudioAgentError, match="ELEVENLABS_VOICE_ID"):
            run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

    def test_no_api_call_before_config_validation(self, tmp_path):
        cfg = _make_config(api_key="")
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment") as mock_gen:
            with pytest.raises(AudioAgentError):
                run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        mock_gen.assert_not_called()


# ---------------------------------------------------------------------------
# TestNarrationPackageStructure
# ---------------------------------------------------------------------------

class TestNarrationPackageStructure:

    def _run(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            return run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

    def test_stitched_tracks_count_is_4(self, tmp_path):
        result = self._run(tmp_path)
        assert len(result["stitched_tracks"]) == 4

    def test_captions_count_is_4(self, tmp_path):
        result = self._run(tmp_path)
        assert len(result["captions"]) == 4

    def test_narration_segments_count_matches_total_beats(self, tmp_path):
        result = self._run(tmp_path)
        # 3 main + 3 short (one beat per short) = 6
        assert len(result["narration_segments"]) == 6

    def test_schema_passes(self, tmp_path):
        result = self._run(tmp_path)
        validate("narration_package", result)

    def test_narration_package_id_format(self, tmp_path):
        result = self._run(tmp_path)
        assert result["package_metadata"]["narration_package_id"] == "np_ep_001"


# ---------------------------------------------------------------------------
# TestStitchedTracks
# ---------------------------------------------------------------------------

class TestStitchedTracks:

    def _run(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            return run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

    def test_exactly_one_main_episode_track(self, tmp_path):
        result = self._run(tmp_path)
        main_tracks = [t for t in result["stitched_tracks"] if t["source_type"] == "main_episode"]
        assert len(main_tracks) == 1

    def test_exactly_three_short_tracks(self, tmp_path):
        result = self._run(tmp_path)
        short_tracks = [t for t in result["stitched_tracks"] if t["source_type"] == "short"]
        assert len(short_tracks) == 3

    def test_main_track_audio_asset_id_format(self, tmp_path):
        result = self._run(tmp_path)
        main_track = next(t for t in result["stitched_tracks"] if t["source_type"] == "main_episode")
        assert "main_narration" in main_track["audio_asset_id"]
        assert "ep_001" in main_track["audio_asset_id"]

    def test_short_track_audio_asset_ids_contain_short_ids(self, tmp_path):
        result = self._run(tmp_path)
        short_tracks = [t for t in result["stitched_tracks"] if t["source_type"] == "short"]
        asset_ids = {t["audio_asset_id"] for t in short_tracks}
        assert any("sh_001" in aid for aid in asset_ids)
        assert any("sh_002" in aid for aid in asset_ids)
        assert any("sh_003" in aid for aid in asset_ids)

    def test_stitched_track_files_written_to_disk(self, tmp_path):
        self._run(tmp_path)
        assert (tmp_path / "narration" / "main_narration.mp3").exists()
        assert (tmp_path / "narration" / "sh_sh_001_narration.mp3").exists()
        assert (tmp_path / "narration" / "sh_sh_002_narration.mp3").exists()
        assert (tmp_path / "narration" / "sh_sh_003_narration.mp3").exists()

    def test_stitched_main_track_has_content(self, tmp_path):
        self._run(tmp_path)
        main_path = tmp_path / "narration" / "main_narration.mp3"
        assert main_path.stat().st_size > 0


# ---------------------------------------------------------------------------
# TestCaptions
# ---------------------------------------------------------------------------

class TestCaptions:

    def _run(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            return run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

    def test_exactly_one_main_episode_caption(self, tmp_path):
        result = self._run(tmp_path)
        main_caps = [c for c in result["captions"] if "main" in c["caption_id"]]
        assert len(main_caps) == 1

    def test_exactly_three_short_captions(self, tmp_path):
        result = self._run(tmp_path)
        short_caps = [c for c in result["captions"] if "sh_" in c["caption_id"]]
        assert len(short_caps) == 3

    def test_caption_method_elevenlabs_alignment_when_all_tts(self, tmp_path):
        result = self._run(tmp_path)
        for cap in result["captions"]:
            assert cap["caption_method"] == "elevenlabs_alignment"

    def test_caption_method_whisper_fallback_when_any_none_source(self, tmp_path):
        cfg = _make_config()

        def _side(beat_id, text, config, episode_id, output_dir, **kw):
            if beat_id == "s01b01":
                return _fake_result(beat_id, output_dir, alignment_source="none")
            return _fake_result(beat_id, output_dir, alignment_source="tts")

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_side):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        main_cap = next(c for c in result["captions"] if "main" in c["caption_id"])
        assert main_cap["caption_method"] == "whisper_fallback"

    def test_caption_method_whisper_fallback_when_any_whisper_source(self, tmp_path):
        cfg = _make_config()

        def _side(beat_id, text, config, episode_id, output_dir, **kw):
            if beat_id == "s02b01":
                return _fake_result(beat_id, output_dir, alignment_source="whisper")
            return _fake_result(beat_id, output_dir, alignment_source="tts")

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_side):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        main_cap = next(c for c in result["captions"] if "main" in c["caption_id"])
        assert main_cap["caption_method"] == "whisper_fallback"

    def test_short_caption_method_independent_of_main(self, tmp_path):
        """A main episode whisper fallback should not affect Short captions."""
        cfg = _make_config()

        def _side(beat_id, text, config, episode_id, output_dir, **kw):
            if beat_id in ("s01b01", "s01b02", "s02b01"):
                return _fake_result(beat_id, output_dir, alignment_source="none")
            return _fake_result(beat_id, output_dir, alignment_source="tts")

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_side):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        short_caps = [c for c in result["captions"] if "sh_" in c["caption_id"]]
        for cap in short_caps:
            assert cap["caption_method"] == "elevenlabs_alignment"

    def test_srt_files_written_to_disk(self, tmp_path):
        self._run(tmp_path)
        assert (tmp_path / "captions" / "main.srt").exists()
        assert (tmp_path / "captions" / "sh_sh_001.srt").exists()
        assert (tmp_path / "captions" / "sh_sh_002.srt").exists()
        assert (tmp_path / "captions" / "sh_sh_003.srt").exists()

    def test_main_srt_asset_id_format(self, tmp_path):
        result = self._run(tmp_path)
        main_cap = next(c for c in result["captions"] if "main" in c["caption_id"])
        assert "ep_001" in main_cap["srt_asset_id"]
        assert "main" in main_cap["srt_asset_id"]


# ---------------------------------------------------------------------------
# TestSrtGeneration
# ---------------------------------------------------------------------------

class TestSrtGeneration:

    def test_main_srt_has_content(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        srt = (tmp_path / "captions" / "main.srt").read_text()
        assert "-->" in srt  # has at least one timestamp line

    def test_all_srt_files_present(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        for name in ("main.srt", "sh_sh_001.srt", "sh_sh_002.srt", "sh_sh_003.srt"):
            assert (tmp_path / "captions" / name).exists()

    def test_srt_contains_sequential_indices(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        srt = (tmp_path / "captions" / "main.srt").read_text()
        assert "1\n" in srt


# ---------------------------------------------------------------------------
# TestAlignmentCascadeIntegration
# ---------------------------------------------------------------------------

class TestAlignmentCascadeIntegration:

    def test_tts_alignment_source_recorded(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path, alignment_source="tts")):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        generated_segs = [
            s for s in result["narration_segments"]
            if s["generation_status"] == "generated"
        ]
        for seg in generated_segs:
            assert seg["alignment_source"] == "tts"

    def test_forced_alignment_source_recorded(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path, alignment_source="forced_alignment")):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        generated_segs = [
            s for s in result["narration_segments"]
            if s["generation_status"] == "generated"
        ]
        for seg in generated_segs:
            assert seg["alignment_source"] == "forced_alignment"

    def test_none_alignment_source_on_failed_beat(self, tmp_path):
        cfg = _make_config()

        def _side(beat_id, text, config, episode_id, output_dir, **kw):
            if beat_id == "s01b01":
                raise Exception("fail")
            return _fake_result(beat_id, output_dir)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_side):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        failed_seg = next(
            s for s in result["narration_segments"]
            if s["beat_reference"] == "s01b01"
        )
        assert failed_seg["alignment_source"] == "none"


# ---------------------------------------------------------------------------
# TestQaReport
# ---------------------------------------------------------------------------

class TestQaReport:

    def test_pass_status_zero_failures(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        assert result["audio_qa_report"]["overall_status"] == "pass"

    def test_warn_status_at_exactly_one_failure_of_six(self, tmp_path):
        """1/6 ≈ 16.7% > 10% threshold → warn"""
        cfg = _make_config()
        call_n = {"n": 0}

        def _side(beat_id, text, config, episode_id, output_dir, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1:
                raise Exception("fail")
            return _fake_result(beat_id, output_dir)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_side):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        assert result["audio_qa_report"]["overall_status"] in ("warn", "fail")
        assert result["package_integrity"]["production_ready"] is False

    def test_fail_status_when_majority_fail(self, tmp_path):
        cfg = _make_config()

        def _side(beat_id, text, config, episode_id, output_dir, **kw):
            raise Exception("all fail")

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_side):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)

        assert result["audio_qa_report"]["overall_status"] == "fail"
        assert result["package_integrity"]["production_ready"] is False

    def test_character_cost_summed(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        assert result["audio_qa_report"]["total_characters_billed"] > 0

    def test_total_beats_count(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        assert result["audio_qa_report"]["total_beats"] == 6


# ---------------------------------------------------------------------------
# TestPackageWritten
# ---------------------------------------------------------------------------

class TestPackageWritten:

    def test_package_file_exists_after_run(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        assert (tmp_path / "narration_package_ep_001.json").exists()

    def test_package_file_is_valid_json(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        content = (tmp_path / "narration_package_ep_001.json").read_text()
        parsed = json.loads(content)
        assert parsed["package_metadata"]["episode_id"] == "ep_001"

    def test_returned_dict_matches_written_file(self, tmp_path):
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        written = json.loads((tmp_path / "narration_package_ep_001.json").read_text())
        assert written["package_metadata"]["narration_package_id"] == \
               result["package_metadata"]["narration_package_id"]


# ---------------------------------------------------------------------------
# TestInternalHelpers
# ---------------------------------------------------------------------------

class TestInternalHelpers:

    def test_collect_main_beats_flattens_sections(self):
        beats = _collect_main_beats(_SCRIPT_PACKAGE)
        assert len(beats) == 3  # 2 from s01 + 1 from s02

    def test_collect_main_beats_preserves_order(self):
        beats = _collect_main_beats(_SCRIPT_PACKAGE)
        assert beats[0]["beat_id"] == "s01b01"
        assert beats[1]["beat_id"] == "s01b02"
        assert beats[2]["beat_id"] == "s02b01"

    def test_collect_main_beats_empty_sections(self):
        sp = {**_SCRIPT_PACKAGE, "sections": []}
        assert _collect_main_beats(sp) == []

    def test_collect_shorts_beats_returns_three_tuples(self):
        result = _collect_shorts_beats(_SCRIPT_PACKAGE)
        assert len(result) == 3

    def test_collect_shorts_beats_short_ids(self):
        result = _collect_shorts_beats(_SCRIPT_PACKAGE)
        short_ids = [sid for sid, _ in result]
        assert short_ids == ["sh_001", "sh_002", "sh_003"]

    def test_collect_shorts_beats_beat_count(self):
        result = _collect_shorts_beats(_SCRIPT_PACKAGE)
        for _, beats in result:
            assert len(beats) == 1  # one beat per short in fixture

    def test_make_segment_record_success(self):
        beat = {"beat_id": "b01", "text": "hello"}
        wt = [WordTiming("hello", 0.0, 0.5)]
        result = NarrationSegmentResult(
            beat_id="b01",
            asset_id="episodes/ep_001/narration/b01",
            local_path=Path("/fake/b01.mp3"),
            duration_seconds=0.5,
            request_id="req_b01",
            character_cost=5,
            word_timings=wt,
            alignment_source="tts",
        )
        record = _make_segment_record(beat, result, "main_episode")
        assert record["segment_id"] == "seg_b01"
        assert record["beat_reference"] == "b01"
        assert record["source_type"] == "main_episode"
        assert record["generation_status"] == "generated"

    def test_make_segment_record_failure(self):
        beat = {"beat_id": "b02", "text": "world"}
        record = _make_segment_record(beat, None, "short")
        assert record["generation_status"] == "failed"
        assert record["audio_asset_id"] is None
        assert record["source_type"] == "short"

    def test_make_segment_record_beat_reference_matches_beat_id(self):
        beat = {"beat_id": "custom_id", "text": "test"}
        record = _make_segment_record(beat, None, "main_episode")
        assert record["beat_reference"] == "custom_id"
        assert record["segment_id"] == "seg_custom_id"


# ---------------------------------------------------------------------------
# TestSkipShorts  — M5.6: Episode Zero skip_shorts=True suppresses all Shorts
# ---------------------------------------------------------------------------

class TestSkipShorts:
    """
    When skip_shorts=True is passed to run_audio_agent(), no Short narration
    should be generated, stitched, or included in the output package.
    This is the Episode Zero / validation mode behaviour (shorts_count=0).
    """

    def test_no_short_stitched_tracks(self, tmp_path):
        """skip_shorts=True → stitched_tracks contains only main_episode."""
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(
                _SCRIPT_PACKAGE, cfg, "ep_000", tmp_path, skip_shorts=True
            )
        short_tracks = [
            t for t in result["stitched_tracks"] if t["source_type"] == "short"
        ]
        assert short_tracks == [], "No short stitched tracks expected when skip_shorts=True"

    def test_main_track_still_produced(self, tmp_path):
        """skip_shorts=True must not affect main episode track."""
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(
                _SCRIPT_PACKAGE, cfg, "ep_000", tmp_path, skip_shorts=True
            )
        main_tracks = [
            t for t in result["stitched_tracks"] if t["source_type"] == "main_episode"
        ]
        assert len(main_tracks) == 1

    def test_no_short_captions(self, tmp_path):
        """skip_shorts=True → captions list contains only the main SRT."""
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(
                _SCRIPT_PACKAGE, cfg, "ep_000", tmp_path, skip_shorts=True
            )
        short_caps = [
            c for c in result["captions"]
            if "short" in c.get("caption_id", "").lower()
               or "sh_" in c.get("srt_asset_id", "")
        ]
        assert short_caps == [], "No short caption entries expected when skip_shorts=True"

    def test_no_short_segments_in_narration_segments(self, tmp_path):
        """skip_shorts=True → no narration_segments with source_type='short'."""
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(
                _SCRIPT_PACKAGE, cfg, "ep_000", tmp_path, skip_shorts=True
            )
        short_segs = [
            s for s in result["narration_segments"] if s["source_type"] == "short"
        ]
        assert short_segs == []

    def test_generate_not_called_for_short_beats(self, tmp_path):
        """skip_shorts=True → generate_narration_segment never called with a Short beat_id."""
        cfg = _make_config()
        call_log = []

        def _spy(beat_id, text, **kwargs):
            call_log.append(beat_id)
            return _fake_result(beat_id, tmp_path)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_spy):
            run_audio_agent(
                _SCRIPT_PACKAGE, cfg, "ep_000", tmp_path, skip_shorts=True
            )

        short_beat_ids = {"sh1b01", "sh2b01", "sh3b01"}
        called_short = short_beat_ids & set(call_log)
        assert called_short == set(), f"Short beats should not be generated: {called_short}"

    def test_main_beats_still_called_when_skip_shorts(self, tmp_path):
        """Main beats are generated even when skip_shorts=True."""
        cfg = _make_config()
        call_log = []

        def _spy(beat_id, text, **kwargs):
            call_log.append(beat_id)
            return _fake_result(beat_id, tmp_path)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_spy):
            run_audio_agent(
                _SCRIPT_PACKAGE, cfg, "ep_000", tmp_path, skip_shorts=True
            )

        assert "s01b01" in call_log
        assert "s01b02" in call_log
        assert "s02b01" in call_log

    def test_skip_shorts_false_default_still_produces_3_short_tracks(self, tmp_path):
        """Default (skip_shorts=False) behaviour is unchanged — 3 short tracks produced."""
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_001", tmp_path)
        short_tracks = [
            t for t in result["stitched_tracks"] if t["source_type"] == "short"
        ]
        assert len(short_tracks) == 3

    def test_no_short_mp3_files_written_when_skip_shorts(self, tmp_path):
        """No sh_*_narration.mp3 files should be created when skip_shorts=True."""
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            run_audio_agent(
                _SCRIPT_PACKAGE, cfg, "ep_000", tmp_path, skip_shorts=True
            )
        narration_dir = tmp_path / "narration"
        short_mp3s = list(narration_dir.glob("sh_*.mp3"))
        assert short_mp3s == [], f"No short MP3 files expected, found: {short_mp3s}"

    def test_no_short_srt_files_written_when_skip_shorts(self, tmp_path):
        """No sh_*.srt files should be created in captions/ when skip_shorts=True."""
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            run_audio_agent(
                _SCRIPT_PACKAGE, cfg, "ep_000", tmp_path, skip_shorts=True
            )
        captions_dir = tmp_path / "captions"
        short_srts = list(captions_dir.glob("sh_*.srt"))
        assert short_srts == [], f"No short SRT files expected, found: {short_srts}"

    def test_package_integrity_production_ready_with_skip_shorts(self, tmp_path):
        """production_ready should still be True when all main beats succeed with skip_shorts."""
        cfg = _make_config()
        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_audio_agent(
                _SCRIPT_PACKAGE, cfg, "ep_000", tmp_path, skip_shorts=True
            )
        assert result["package_integrity"]["production_ready"] is True


# ---------------------------------------------------------------------------
# TestBeatCap — SMOKE_TEST_MAX_NARRATION_BEATS caps main beats before VGO
# ---------------------------------------------------------------------------

def _make_smoke_audio_config(max_beats: int) -> Config:
    env = {
        "OPENROUTER_API_KEY": "or-key",
        "ELEVENLABS_API_KEY": "test-xi",
        "ELEVENLABS_VOICE_ID": "voice-abc",
        "ELEVENLABS_TIMEOUT_SECONDS": "10",
        "SMOKE_TEST_MAX_NARRATION_BEATS": str(max_beats),
    }
    with patch.dict(os.environ, env, clear=True):
        return Config()


class TestBeatCap:
    """SMOKE_TEST_MAX_NARRATION_BEATS caps main beats sent to VGO."""

    def _run_and_capture_beats(self, cfg, tmp_path) -> list:
        """Run audio agent and return the main_beats list passed to VGO."""
        captured = {}

        def fake_vgo(main_beats, **kwargs):
            captured["main_beats"] = list(main_beats)
            # Return a minimal VGO result to satisfy the rest of the function
            vgo = MagicMock()
            vgo.narration_segments = []
            vgo.stitched_tracks = [
                {"track_id": "main", "source_type": "main_episode",
                 "asset_id": "ep/narration/main.mp3", "local_path": str(tmp_path)}
            ]
            vgo.main_results = []
            vgo.shorts_results = []
            vgo.short_track_paths = []
            vgo.total_chars_billed = 0
            return vgo

        with (
            patch("hypogeum.agents.audio_agent.run_voice_generation_operator", side_effect=fake_vgo),
            patch("hypogeum.agents.audio_agent.run_caption_agent", return_value={"captions": []}),
            patch("hypogeum.agents.audio_agent.run_audio_qa", return_value={
                "overall_status": "pass",
                "production_ready": True,
                "beats_failed": 0,
                "total_beats": 0,
                "failure_rate": 0.0,
                "total_characters_billed": 0,
            }),
        ):
            run_audio_agent(_SCRIPT_PACKAGE, cfg, "ep_smoke_01", tmp_path, skip_shorts=True)
        return captured["main_beats"]

    def test_cap_applied_when_set(self, tmp_path):
        # _SCRIPT_PACKAGE has 3 main beats; cap to 2
        cfg = _make_smoke_audio_config(max_beats=2)
        beats = self._run_and_capture_beats(cfg, tmp_path)
        assert len(beats) == 2

    def test_cap_keeps_first_beats_in_order(self, tmp_path):
        cfg = _make_smoke_audio_config(max_beats=1)
        beats = self._run_and_capture_beats(cfg, tmp_path)
        assert len(beats) == 1
        assert beats[0]["beat_id"] == "s01b01"

    def test_cap_zero_means_unlimited(self, tmp_path):
        # SMOKE_TEST_MAX_NARRATION_BEATS=0 → all 3 beats pass through
        cfg = _make_config()  # default: SMOKE_TEST_MAX_NARRATION_BEATS=0
        beats = self._run_and_capture_beats(cfg, tmp_path)
        assert len(beats) == 3
