"""
tests/test_audio_qa.py — Unit tests for Audio QA Agent (audio_qa.py)

Tests cover:
  TestRunAudioQA          — basic pass/warn/fail verdict construction (8 tests)
  TestFailRateThreshold   — boundary conditions around 10% threshold (5 tests)
  TestChecks              — individual check correctness (8 tests)
  TestEdgeCases           — empty segments, all-failed, single segment (5 tests)
  TestSchemaContract      — verdict dict satisfies audio_qa_verdict schema (4 tests)
"""

from __future__ import annotations

import pytest

from hypogeum.agents.audio_qa import run_audio_qa
from hypogeum.schemas import SCHEMA_NAMES, validate, SchemaError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_seg(
    beat_id: str,
    status: str = "generated",
    duration: float = 5.0,
) -> dict:
    """Build a minimal narration segment dict."""
    return {
        "segment_id": f"seg_{beat_id}",
        "beat_reference": beat_id,
        "source_type": "main_episode",
        "generation_status": status,
        "audio_asset_id": f"episodes/ep_test/narration/{beat_id}" if status == "generated" else None,
        "duration_seconds": duration if status == "generated" else 0.0,
        "character_cost": 100 if status == "generated" else 0,
        "alignment_source": "tts" if status == "generated" else "none",
        "word_count": 10 if status == "generated" else 0,
    }


def _all_pass(segs: list[dict]) -> list[dict]:
    """Return only generated segments."""
    return [_make_seg(f"b{i}") for i in range(segs if isinstance(segs, int) else len(segs))]


def _make_segments(n_generated: int, n_failed: int, duration: float = 5.0) -> list[dict]:
    segs = []
    for i in range(n_generated):
        segs.append(_make_seg(f"g{i}", "generated", duration))
    for i in range(n_failed):
        segs.append(_make_seg(f"f{i}", "failed"))
    return segs


# ---------------------------------------------------------------------------
# TestRunAudioQA
# ---------------------------------------------------------------------------

class TestRunAudioQA:
    def test_returns_dict(self):
        segs = _make_segments(5, 0)
        verdict = run_audio_qa(segs, 500, "ep_test")
        assert isinstance(verdict, dict)

    def test_verdict_id_uses_episode_id(self):
        segs = _make_segments(3, 0)
        verdict = run_audio_qa(segs, 300, "ep_abc")
        assert verdict["verdict_id"] == "aqv_ep_abc"

    def test_episode_id_stored(self):
        segs = _make_segments(3, 0)
        verdict = run_audio_qa(segs, 300, "ep_xyz")
        assert verdict["episode_id"] == "ep_xyz"

    def test_evaluated_at_is_string(self):
        segs = _make_segments(2, 0)
        verdict = run_audio_qa(segs, 200, "ep_t")
        assert isinstance(verdict["evaluated_at"], str)
        assert len(verdict["evaluated_at"]) > 10  # ISO format has content

    def test_total_chars_billed_stored(self):
        segs = _make_segments(4, 0)
        verdict = run_audio_qa(segs, 1234, "ep_t")
        assert verdict["total_characters_billed"] == 1234

    def test_beat_counts(self):
        segs = _make_segments(7, 3)
        verdict = run_audio_qa(segs, 700, "ep_t")
        assert verdict["total_beats"] == 10
        assert verdict["beats_generated"] == 7
        assert verdict["beats_failed"] == 3

    def test_checks_is_list(self):
        segs = _make_segments(5, 0)
        verdict = run_audio_qa(segs, 500, "ep_t")
        assert isinstance(verdict["checks"], list)
        assert len(verdict["checks"]) == 4

    def test_each_check_has_required_fields(self):
        segs = _make_segments(5, 0)
        verdict = run_audio_qa(segs, 500, "ep_t")
        for check in verdict["checks"]:
            assert "check_id" in check
            assert "status" in check
            assert "detail" in check
            assert check["status"] in ("pass", "warn", "fail")


# ---------------------------------------------------------------------------
# TestFailRateThreshold
# ---------------------------------------------------------------------------

class TestFailRateThreshold:
    def test_all_pass_gives_pass_status(self):
        segs = _make_segments(10, 0)
        verdict = run_audio_qa(segs, 1000, "ep_t")
        assert verdict["overall_status"] == "pass"
        assert verdict["production_ready"] is True

    def test_exactly_10_percent_failure_gives_warn(self):
        # 1/10 = 10% → at threshold → warn, production_ready = True
        segs = _make_segments(9, 1)
        verdict = run_audio_qa(segs, 900, "ep_t")
        assert verdict["overall_status"] == "warn"
        assert verdict["production_ready"] is True

    def test_above_10_percent_gives_fail(self):
        # 2/10 = 20% → above threshold
        segs = _make_segments(8, 2)
        verdict = run_audio_qa(segs, 800, "ep_t")
        assert verdict["overall_status"] == "fail"
        assert verdict["production_ready"] is False

    def test_failure_rate_rounded_to_4_places(self):
        segs = _make_segments(6, 1)  # 1/7 ≈ 0.142857...
        verdict = run_audio_qa(segs, 600, "ep_t")
        assert verdict["failure_rate"] == round(1 / 7, 4)

    def test_all_failed_gives_fail(self):
        segs = _make_segments(0, 5)
        verdict = run_audio_qa(segs, 0, "ep_t")
        assert verdict["overall_status"] == "fail"
        assert verdict["production_ready"] is False
        assert verdict["failure_rate"] == 1.0


# ---------------------------------------------------------------------------
# TestChecks
# ---------------------------------------------------------------------------

class TestChecks:
    def _get_check(self, verdict: dict, check_id: str) -> dict:
        for c in verdict["checks"]:
            if c["check_id"] == check_id:
                return c
        raise KeyError(f"check_id '{check_id}' not found")

    def test_beat_failure_rate_check_present(self):
        segs = _make_segments(5, 0)
        verdict = run_audio_qa(segs, 500, "ep_t")
        ck = self._get_check(verdict, "beat_failure_rate")
        assert ck["status"] == "pass"

    def test_beat_failure_rate_check_warns_on_some_failures(self):
        segs = _make_segments(9, 1)
        verdict = run_audio_qa(segs, 900, "ep_t")
        ck = self._get_check(verdict, "beat_failure_rate")
        assert ck["status"] == "warn"

    def test_zero_duration_check_pass_when_no_zero(self):
        segs = _make_segments(5, 0, duration=3.0)
        verdict = run_audio_qa(segs, 500, "ep_t")
        ck = self._get_check(verdict, "zero_duration_generated")
        assert ck["status"] == "pass"

    def test_zero_duration_check_warns_on_zero_duration_generated(self):
        segs = [_make_seg("b0", "generated", 0.0)]
        verdict = run_audio_qa(segs, 100, "ep_t")
        ck = self._get_check(verdict, "zero_duration_generated")
        assert ck["status"] == "warn"
        assert "seg_b0" in ck["detail"]

    def test_short_duration_check_pass_when_all_normal(self):
        segs = _make_segments(5, 0, duration=2.0)
        verdict = run_audio_qa(segs, 500, "ep_t")
        ck = self._get_check(verdict, "short_segment_duration")
        assert ck["status"] == "pass"

    def test_short_duration_check_warns_on_very_short_segment(self):
        segs = [_make_seg("b0", "generated", 0.1)]
        verdict = run_audio_qa(segs, 100, "ep_t")
        ck = self._get_check(verdict, "short_segment_duration")
        assert ck["status"] == "warn"

    def test_duration_consistency_check_present(self):
        segs = _make_segments(5, 0, duration=5.0)
        verdict = run_audio_qa(segs, 500, "ep_t")
        ck = self._get_check(verdict, "duration_consistency")
        assert ck["status"] == "pass"

    def test_duration_consistency_warns_on_high_variance(self):
        # Create segments with wildly different durations
        segs = [
            _make_seg("b0", "generated", 0.5),
            _make_seg("b1", "generated", 100.0),
        ]
        verdict = run_audio_qa(segs, 200, "ep_t")
        ck = self._get_check(verdict, "duration_consistency")
        # std-dev = ~49.75 > 15s threshold
        assert ck["status"] == "warn"


# ---------------------------------------------------------------------------
# TestEdgeCases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_segments_returns_pass(self):
        verdict = run_audio_qa([], 0, "ep_t")
        assert verdict["overall_status"] == "pass"
        assert verdict["total_beats"] == 0
        assert verdict["beats_generated"] == 0
        assert verdict["beats_failed"] == 0
        assert verdict["failure_rate"] == 0.0
        assert verdict["production_ready"] is True

    def test_single_generated_segment(self):
        segs = [_make_seg("b0", "generated", 5.0)]
        verdict = run_audio_qa(segs, 100, "ep_t")
        assert verdict["total_beats"] == 1
        assert verdict["beats_generated"] == 1
        assert verdict["beats_failed"] == 0
        assert verdict["overall_status"] == "pass"

    def test_single_failed_segment_gives_fail(self):
        segs = [_make_seg("b0", "failed")]
        verdict = run_audio_qa(segs, 0, "ep_t")
        assert verdict["overall_status"] == "fail"
        assert verdict["failure_rate"] == 1.0
        assert verdict["production_ready"] is False

    def test_warn_from_secondary_check_only(self):
        # All generated but one has zero duration → overall warn, but production_ready True
        segs = [
            _make_seg("b0", "generated", 0.0),
            _make_seg("b1", "generated", 5.0),
        ]
        verdict = run_audio_qa(segs, 200, "ep_t")
        assert verdict["overall_status"] == "warn"
        assert verdict["production_ready"] is True

    def test_failed_segment_zero_duration_not_flagged_as_zero_duration_generated(self):
        # Failed segments always have duration_seconds=0.0; they must not trigger
        # the zero_duration_generated check (which only applies to 'generated' segs)
        segs = _make_segments(0, 3)
        verdict = run_audio_qa(segs, 0, "ep_t")
        ck = next(c for c in verdict["checks"] if c["check_id"] == "zero_duration_generated")
        assert ck["status"] == "pass"


# ---------------------------------------------------------------------------
# TestSchemaContract
# ---------------------------------------------------------------------------

class TestSchemaContract:
    def test_schema_is_registered(self):
        assert "audio_qa_verdict" in SCHEMA_NAMES

    def test_valid_verdict_passes_schema(self):
        segs = _make_segments(5, 1)
        verdict = run_audio_qa(segs, 500, "ep_t")
        # Should not raise
        validate("audio_qa_verdict", verdict)

    def test_missing_verdict_id_fails_schema(self):
        segs = _make_segments(5, 0)
        verdict = run_audio_qa(segs, 500, "ep_t")
        del verdict["verdict_id"]
        with pytest.raises(SchemaError):
            validate("audio_qa_verdict", verdict)

    def test_invalid_overall_status_fails_schema(self):
        segs = _make_segments(5, 0)
        verdict = run_audio_qa(segs, 500, "ep_t")
        verdict["overall_status"] = "ok"
        with pytest.raises(SchemaError):
            validate("audio_qa_verdict", verdict)
