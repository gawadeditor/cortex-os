"""
tests/test_voice_generation_operator.py — Unit tests for
hypogeum/agents/voice_generation_operator.py

All generate_narration_segment() calls are mocked. No real ElevenLabs API calls.

Test coverage:
  TestRunVGOSuccess        — happy path: all beats generated, tracks stitched
  TestProsodyChaining      — prev request IDs passed correctly, chain resets per Short
  TestBeatFailureIsolation — failed beat → None result, subsequent beats still run
  TestHaltConditions       — ElevenLabsAuthFailure / ElevenLabsQuotaExhausted propagate
  TestStitching            — stitched track files created, bytes concatenated
  TestNarrationSegments    — segment records structure, status, source_type
  TestReturnedFields       — VoiceGenerationResult fields populated correctly
  TestMakeSegmentRecord    — _make_segment_record helper
"""

from __future__ import annotations

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
from hypogeum.agents.voice_generation_operator import (
    VoiceGenerationResult,
    _make_segment_record,
    run_voice_generation_operator,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _make_config() -> Config:
    env = {
        "OPENROUTER_API_KEY": "or-key",
        "ELEVENLABS_API_KEY": "xi-key",
        "ELEVENLABS_VOICE_ID": "voice-abc",
        "ELEVENLABS_TIMEOUT_SECONDS": "10",
    }
    with patch.dict(os.environ, env, clear=True):
        return Config()


def _make_beats(beat_ids: List[str]) -> List[dict]:
    return [{"beat_id": bid, "text": f"Text for {bid}."} for bid in beat_ids]


def _fake_result(beat_id: str, tmp_path: Path, alignment_source: str = "tts") -> NarrationSegmentResult:
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
        word_timings=[WordTiming("hello", 0.0, 0.5)],
        alignment_source=alignment_source,
    )


def _mock_generate(tmp_path: Path, alignment_source: str = "tts"):
    """Returns a side_effect that creates real audio files on disk."""
    call_log = []

    def _side_effect(beat_id, text, config, episode_id, output_dir, **kwargs):
        call_log.append({"beat_id": beat_id, "prev": kwargs.get("previous_request_ids")})
        return _fake_result(beat_id, output_dir, alignment_source)

    _side_effect.call_log = call_log
    return _side_effect


# ---------------------------------------------------------------------------
# TestRunVGOSuccess
# ---------------------------------------------------------------------------

class TestRunVGOSuccess:

    def test_returns_voice_generation_result(self, tmp_path):
        """run_voice_generation_operator returns a VoiceGenerationResult."""
        cfg = _make_config()
        main_beats = _make_beats(["b01", "b02"])

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_voice_generation_operator(
                main_beats=main_beats,
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert isinstance(result, VoiceGenerationResult)

    def test_all_segments_generated_on_success(self, tmp_path):
        """All main beats appear in narration_segments with generated status."""
        cfg = _make_config()
        main_beats = _make_beats(["b01", "b02", "b03"])

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_voice_generation_operator(
                main_beats=main_beats,
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert len(result.narration_segments) == 3
        assert all(s["generation_status"] == "generated" for s in result.narration_segments)

    def test_voice_configuration_populated(self, tmp_path):
        """voice_configuration contains correct voice_id and model_id."""
        cfg = _make_config()

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_voice_generation_operator(
                main_beats=_make_beats(["b01"]),
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert result.voice_configuration["voice_id"] == "voice-abc"
        assert result.voice_configuration["environment_source"] == "ELEVENLABS_VOICE_ID"

    def test_shorts_segments_included(self, tmp_path):
        """Short beats appear in narration_segments with source_type='short'."""
        cfg = _make_config()
        shorts_info = [
            ("sh_001", _make_beats(["sh1b01"])),
            ("sh_002", _make_beats(["sh2b01"])),
        ]

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_voice_generation_operator(
                main_beats=_make_beats(["b01"]),
                shorts_info=shorts_info,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        short_segs = [s for s in result.narration_segments if s["source_type"] == "short"]
        assert len(short_segs) == 2

    def test_empty_beats_returns_empty_segments(self, tmp_path):
        """No beats → empty narration_segments, no generate calls."""
        cfg = _make_config()

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment") as mock_gen:
            result = run_voice_generation_operator(
                main_beats=[],
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert result.narration_segments == []
        assert mock_gen.call_count == 0

    def test_total_chars_billed_summed(self, tmp_path):
        """total_chars_billed is sum of character_cost from all successful segments."""
        cfg = _make_config()
        main_beats = _make_beats(["b01", "b02"])

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_voice_generation_operator(
                main_beats=main_beats,
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert result.total_chars_billed > 0
        expected = sum(
            r.character_cost for r in result.main_results if r is not None
        )
        assert result.total_chars_billed == expected


# ---------------------------------------------------------------------------
# TestProsodyChaining
# ---------------------------------------------------------------------------

class TestProsodyChaining:

    def test_first_beat_has_no_prev_ids(self, tmp_path):
        """First beat is called with None or empty previous_request_ids."""
        cfg = _make_config()
        side = _mock_generate(tmp_path)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=side):
            run_voice_generation_operator(
                main_beats=_make_beats(["b01", "b02"]),
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        first_call_prev = side.call_log[0]["prev"]
        assert not first_call_prev  # None or empty list

    def test_second_beat_has_first_request_id(self, tmp_path):
        """Second beat receives first beat's request_id as previous_request_ids."""
        cfg = _make_config()
        call_log = []

        def _capture(beat_id, text, config, episode_id, output_dir,
                     previous_request_ids=None, **kwargs):
            call_log.append((beat_id, list(previous_request_ids or [])))
            return _fake_result(beat_id, output_dir)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_capture):
            run_voice_generation_operator(
                main_beats=_make_beats(["b01", "b02"]),
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        # b01 gets empty, b02 gets req_b01
        assert call_log[0] == ("b01", [])
        assert call_log[1][0] == "b02"
        assert "req_b01" in call_log[1][1]

    def test_chain_capped_at_three(self, tmp_path):
        """previous_request_ids never exceeds 3 entries."""
        cfg = _make_config()
        call_log = []

        def _capture(beat_id, text, config, episode_id, output_dir,
                     previous_request_ids=None, **kwargs):
            call_log.append(list(previous_request_ids or []))
            return _fake_result(beat_id, output_dir)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_capture):
            run_voice_generation_operator(
                main_beats=_make_beats(["b1", "b2", "b3", "b4", "b5"]),
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        # After 3+ beats, the chain should never exceed 3
        for i, prev_ids in enumerate(call_log):
            assert len(prev_ids) <= 3, f"beat {i}: prev_ids had {len(prev_ids)} entries"

    def test_chain_resets_between_shorts(self, tmp_path):
        """Prosody chain resets to empty at the start of each Short."""
        cfg = _make_config()
        call_log = {}

        def _capture(beat_id, text, config, episode_id, output_dir,
                     previous_request_ids=None, **kwargs):
            call_log[beat_id] = list(previous_request_ids or [])
            return _fake_result(beat_id, output_dir)

        shorts_info = [
            ("sh_001", _make_beats(["sh1b01", "sh1b02"])),
            ("sh_002", _make_beats(["sh2b01"])),
        ]

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_capture):
            run_voice_generation_operator(
                main_beats=_make_beats(["main_b01"]),
                shorts_info=shorts_info,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        # First beat of each Short has empty prev ids
        assert call_log["sh1b01"] == []
        assert call_log["sh2b01"] == []
        # Second beat of sh_001 has sh1b01's request_id
        assert "req_sh1b01" in call_log["sh1b02"]


# ---------------------------------------------------------------------------
# TestBeatFailureIsolation
# ---------------------------------------------------------------------------

class TestBeatFailureIsolation:

    def test_failed_beat_is_none_in_main_results(self, tmp_path):
        """Non-halt failure → result is None; subsequent beats still run."""
        cfg = _make_config()

        call_count = {"n": 0}
        def _fail_second(beat_id, text, config, episode_id, output_dir, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("Network error")
            return _fake_result(beat_id, output_dir)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_fail_second):
            result = run_voice_generation_operator(
                main_beats=_make_beats(["b01", "b02", "b03"]),
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        # All 3 beats attempted
        assert call_count["n"] == 3
        # Second result is None
        assert result.main_results[1] is None
        # Segment record for failed beat has status "failed"
        assert result.narration_segments[1]["generation_status"] == "failed"

    def test_failed_beat_not_included_in_chars_billed(self, tmp_path):
        """Failed beats do not contribute to total_chars_billed."""
        cfg = _make_config()

        call_count = {"n": 0}
        def _fail_all(beat_id, text, config, episode_id, output_dir, **kwargs):
            raise RuntimeError("always fails")

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_fail_all):
            result = run_voice_generation_operator(
                main_beats=_make_beats(["b01", "b02"]),
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert result.total_chars_billed == 0

    def test_all_beats_failed_segments_still_present(self, tmp_path):
        """If all beats fail, narration_segments has entries with 'failed' status."""
        cfg = _make_config()

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=RuntimeError("fail")):
            result = run_voice_generation_operator(
                main_beats=_make_beats(["b01", "b02"]),
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert len(result.narration_segments) == 2
        assert all(s["generation_status"] == "failed" for s in result.narration_segments)


# ---------------------------------------------------------------------------
# TestHaltConditions
# ---------------------------------------------------------------------------

class TestHaltConditions:

    def test_auth_failure_propagates_immediately(self, tmp_path):
        """ElevenLabsAuthFailure from any beat propagates out of VGO."""
        cfg = _make_config()

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=ElevenLabsAuthFailure()):
            with pytest.raises(ElevenLabsAuthFailure):
                run_voice_generation_operator(
                    main_beats=_make_beats(["b01"]),
                    shorts_info=[],
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path,
                )

    def test_quota_exhausted_propagates_immediately(self, tmp_path):
        """ElevenLabsQuotaExhausted from any beat propagates out of VGO."""
        cfg = _make_config()

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=ElevenLabsQuotaExhausted()):
            with pytest.raises(ElevenLabsQuotaExhausted):
                run_voice_generation_operator(
                    main_beats=_make_beats(["b01"]),
                    shorts_info=[],
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path,
                )

    def test_auth_failure_on_second_beat_halts(self, tmp_path):
        """Auth failure on second beat halts immediately — third beat is never called."""
        cfg = _make_config()
        call_count = {"n": 0}

        def _fail_on_second(beat_id, text, config, episode_id, output_dir, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise ElevenLabsAuthFailure()
            return _fake_result(beat_id, output_dir)

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_fail_on_second):
            with pytest.raises(ElevenLabsAuthFailure):
                run_voice_generation_operator(
                    main_beats=_make_beats(["b01", "b02", "b03"]),
                    shorts_info=[],
                    config=cfg,
                    episode_id="ep_001",
                    output_dir=tmp_path,
                )

        assert call_count["n"] == 2  # third beat never reached


# ---------------------------------------------------------------------------
# TestStitching
# ---------------------------------------------------------------------------

class TestStitching:

    def test_main_stitched_file_created(self, tmp_path):
        """main_narration.mp3 exists after VGO run."""
        cfg = _make_config()

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            run_voice_generation_operator(
                main_beats=_make_beats(["b01", "b02"]),
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert (tmp_path / "narration" / "main_narration.mp3").exists()

    def test_short_stitched_file_created(self, tmp_path):
        """Short stitched track is written to disk."""
        cfg = _make_config()

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            run_voice_generation_operator(
                main_beats=_make_beats(["b01"]),
                shorts_info=[("sh_001", _make_beats(["sh1b01"]))],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert (tmp_path / "narration" / "sh_sh_001_narration.mp3").exists()

    def test_stitched_file_is_concat_of_segments(self, tmp_path):
        """Stitched file contains bytes from all segment files concatenated."""
        cfg = _make_config()

        segment_bytes = b"\xff\xfb\x90\x00" * 20  # fixed size per _fake_result

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            run_voice_generation_operator(
                main_beats=_make_beats(["b01", "b02"]),
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        stitched = (tmp_path / "narration" / "main_narration.mp3").read_bytes()
        assert len(stitched) == len(segment_bytes) * 2

    def test_empty_stitched_file_when_all_fail(self, tmp_path):
        """Empty stitched file created when all beats fail."""
        cfg = _make_config()

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=RuntimeError("fail")):
            run_voice_generation_operator(
                main_beats=_make_beats(["b01"]),
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        stitched = tmp_path / "narration" / "main_narration.mp3"
        assert stitched.exists()
        assert stitched.stat().st_size == 0


# ---------------------------------------------------------------------------
# TestNarrationSegments
# ---------------------------------------------------------------------------

class TestNarrationSegments:

    def test_segment_beat_reference_matches_beat_id(self, tmp_path):
        """Each segment's beat_reference equals its beat_id."""
        cfg = _make_config()

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_voice_generation_operator(
                main_beats=_make_beats(["b01", "b02"]),
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        beat_refs = {s["beat_reference"] for s in result.narration_segments}
        assert beat_refs == {"b01", "b02"}

    def test_main_episode_source_type(self, tmp_path):
        """Main beats have source_type='main_episode'."""
        cfg = _make_config()

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_voice_generation_operator(
                main_beats=_make_beats(["b01"]),
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert result.narration_segments[0]["source_type"] == "main_episode"

    def test_short_source_type(self, tmp_path):
        """Short beats have source_type='short'."""
        cfg = _make_config()

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_voice_generation_operator(
                main_beats=_make_beats(["b01"]),
                shorts_info=[("sh_001", _make_beats(["sh1b01"]))],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        short_seg = next(s for s in result.narration_segments if s["beat_reference"] == "sh1b01")
        assert short_seg["source_type"] == "short"

    def test_order_main_then_shorts(self, tmp_path):
        """narration_segments lists main beats before Short beats."""
        cfg = _make_config()

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_voice_generation_operator(
                main_beats=_make_beats(["mb01"]),
                shorts_info=[("sh_001", _make_beats(["sh1b01"]))],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert result.narration_segments[0]["beat_reference"] == "mb01"
        assert result.narration_segments[1]["beat_reference"] == "sh1b01"


# ---------------------------------------------------------------------------
# TestReturnedFields
# ---------------------------------------------------------------------------

class TestReturnedFields:

    def test_stitched_tracks_count(self, tmp_path):
        """stitched_tracks has 1 + len(short_ids) entries."""
        cfg = _make_config()
        shorts_info = [
            ("sh_001", _make_beats(["sh1b01"])),
            ("sh_002", _make_beats(["sh2b01"])),
        ]

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_voice_generation_operator(
                main_beats=_make_beats(["b01"]),
                shorts_info=shorts_info,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert len(result.stitched_tracks) == 3  # 1 main + 2 shorts

    def test_main_track_source_type(self, tmp_path):
        """First stitched_track has source_type='main_episode'."""
        cfg = _make_config()

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_voice_generation_operator(
                main_beats=_make_beats(["b01"]),
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert result.stitched_tracks[0]["source_type"] == "main_episode"

    def test_short_ids_match_shorts_info(self, tmp_path):
        """short_ids matches the Short IDs from shorts_info in order."""
        cfg = _make_config()
        shorts_info = [
            ("sh_001", _make_beats(["s1b01"])),
            ("sh_003", _make_beats(["s3b01"])),
        ]

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_voice_generation_operator(
                main_beats=_make_beats(["b01"]),
                shorts_info=shorts_info,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert result.short_ids == ["sh_001", "sh_003"]

    def test_main_results_length_matches_main_beats(self, tmp_path):
        """main_results has same length as main_beats."""
        cfg = _make_config()

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_voice_generation_operator(
                main_beats=_make_beats(["b01", "b02", "b03"]),
                shorts_info=[],
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert len(result.main_results) == 3

    def test_short_track_paths_count(self, tmp_path):
        """short_track_paths has one entry per Short."""
        cfg = _make_config()
        shorts_info = [("sh_001", _make_beats(["s1b01"])), ("sh_002", _make_beats(["s2b01"]))]

        with patch("hypogeum.agents.voice_generation_operator.generate_narration_segment",
                   side_effect=_mock_generate(tmp_path)):
            result = run_voice_generation_operator(
                main_beats=_make_beats(["b01"]),
                shorts_info=shorts_info,
                config=cfg,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert len(result.short_track_paths) == 2


# ---------------------------------------------------------------------------
# TestMakeSegmentRecord
# ---------------------------------------------------------------------------

class TestMakeSegmentRecord:

    def test_success_record_fields(self, tmp_path):
        """Successful result produces correct segment record."""
        beat = {"beat_id": "b01", "text": "hello"}
        result = _fake_result("b01", tmp_path)
        record = _make_segment_record(beat, result, "main_episode")

        assert record["segment_id"] == "seg_b01"
        assert record["beat_reference"] == "b01"
        assert record["source_type"] == "main_episode"
        assert record["generation_status"] == "generated"
        assert record["audio_asset_id"] == result.asset_id

    def test_failure_record_fields(self):
        """None result produces a 'failed' segment record."""
        beat = {"beat_id": "b02", "text": "world"}
        record = _make_segment_record(beat, None, "short")

        assert record["generation_status"] == "failed"
        assert record["audio_asset_id"] is None
        assert record["duration_seconds"] == 0.0
        assert record["source_type"] == "short"

    def test_beat_reference_matches_beat_id(self):
        """beat_reference in record equals the beat's beat_id."""
        beat = {"beat_id": "custom_xyz", "text": "test"}
        record = _make_segment_record(beat, None, "main_episode")
        assert record["beat_reference"] == "custom_xyz"

    def test_segment_id_prefixed(self):
        """segment_id is 'seg_' + beat_id."""
        beat = {"beat_id": "abc123", "text": "test"}
        record = _make_segment_record(beat, None, "main_episode")
        assert record["segment_id"] == "seg_abc123"
