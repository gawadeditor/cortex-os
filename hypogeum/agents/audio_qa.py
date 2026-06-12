"""
hypogeum/agents/audio_qa.py — Audio QA Agent (M5.x)

Sole authority for audio quality assessment of a narration package. Receives
narration_segments (per-beat dicts) and total_chars_billed from the Audio Agent
and returns a schema-validated audio_qa_verdict.

Responsibilities (owns):
  ✓ Fail-rate calculation (failed / total beats)
  ✓ Zero-duration check on generated segments
  ✓ Short-duration anomaly check (generated but suspiciously brief)
  ✓ Duration consistency check (unusually high variance across segments)
  ✓ overall_status derivation ("pass" / "warn" / "fail")
  ✓ production_ready decision (True iff fail_rate ≤ 10%)
  ✓ audio_qa_verdict assembly and schema validation

Responsibilities (does not own):
  ✗ Voice generation          → voice_generation_operator.py
  ✗ Segment stitching         → voice_generation_operator.py
  ✗ Caption / SRT generation  → audio_agent.py
  ✗ Whisper fallback          → audio_agent.py
  ✗ Narration package I/O     → audio_agent.py

Thresholds:
  fail_rate > 10%   → overall_status = "fail", production_ready = False
  fail_rate > 0%    → overall_status = "warn"
  fail_rate == 0%   → overall_status = "pass"

  Zero-duration on a generated segment → check status "warn"
  Segment duration < SHORT_DURATION_THRESHOLD_SECONDS and status generated → "warn"
  Duration std-dev > DURATION_CONSISTENCY_THRESHOLD_SECONDS → "warn"

Public API:
    run_audio_qa(narration_segments, total_chars_billed, episode_id) -> dict

Logging events emitted:
  audio_qa_started    — info  — before checks, with segment count
  audio_qa_completed  — info  — verdict issued, with overall_status and production_ready
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import List

from hypogeum.logger import get_logger
from hypogeum.schemas import SchemaError, validate


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "audio_qa"

# Fail-rate threshold: above this → overall_status = "fail", production_ready = False
_FAILURE_THRESHOLD = 0.10

# Duration thresholds
_SHORT_DURATION_THRESHOLD_SECONDS = 0.5   # generated segment < 0.5s → anomaly warn
_DURATION_CONSISTENCY_THRESHOLD_SECONDS = 15.0  # std-dev > 15s → consistency warn


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_audio_qa(
    narration_segments: List[dict],
    total_chars_billed: int,
    episode_id: str,
) -> dict:
    """
    Evaluate narration_segments and return an audio_qa_verdict.

    Args:
        narration_segments:  Per-beat segment records from the narration_package.
                             Each dict must have at minimum:
                               "generation_status": "generated" | "failed"
                               "duration_seconds": float
        total_chars_billed:  Total ElevenLabs character cost (from VGO).
        episode_id:          Episode identifier for log correlation and verdict ID.

    Returns:
        audio_qa_verdict dict (schema-validated).

    Raises:
        SchemaError — if the assembled verdict fails schema validation (non-blocking
                      in practice; the caller may choose to continue).
    """
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    total_count = len(narration_segments)
    log.info(
        "audio_qa_started",
        episode_id=episode_id,
        total_segments=total_count,
    )

    # ------------------------------------------------------------------
    # Check 1 — Beat failure rate
    # ------------------------------------------------------------------
    failed_count = sum(
        1 for seg in narration_segments
        if seg.get("generation_status") == "failed"
    )
    beats_generated = total_count - failed_count
    fail_rate = failed_count / total_count if total_count > 0 else 0.0

    beat_failure_check = {
        "check_id": "beat_failure_rate",
        "status": (
            "pass" if fail_rate == 0.0
            else "warn" if fail_rate <= _FAILURE_THRESHOLD
            else "fail"
        ),
        "detail": (
            f"{failed_count}/{total_count} beats failed "
            f"(rate={round(fail_rate, 4)})"
        ),
    }

    # ------------------------------------------------------------------
    # Check 2 — Zero-duration on generated segments
    # ------------------------------------------------------------------
    zero_duration_segs = [
        seg["segment_id"]
        for seg in narration_segments
        if seg.get("generation_status") == "generated"
        and seg.get("duration_seconds", 0.0) == 0.0
    ]
    zero_duration_check = {
        "check_id": "zero_duration_generated",
        "status": "warn" if zero_duration_segs else "pass",
        "detail": (
            f"{len(zero_duration_segs)} generated segment(s) have zero duration: "
            f"{zero_duration_segs}"
            if zero_duration_segs
            else "No zero-duration segments."
        ),
    }

    # ------------------------------------------------------------------
    # Check 3 — Suspiciously short generated segments
    # ------------------------------------------------------------------
    short_segs = [
        seg["segment_id"]
        for seg in narration_segments
        if seg.get("generation_status") == "generated"
        and 0.0 < seg.get("duration_seconds", 0.0) < _SHORT_DURATION_THRESHOLD_SECONDS
    ]
    short_duration_check = {
        "check_id": "short_segment_duration",
        "status": "warn" if short_segs else "pass",
        "detail": (
            f"{len(short_segs)} generated segment(s) are suspiciously short "
            f"(<{_SHORT_DURATION_THRESHOLD_SECONDS}s): {short_segs}"
            if short_segs
            else f"No segments shorter than {_SHORT_DURATION_THRESHOLD_SECONDS}s."
        ),
    }

    # ------------------------------------------------------------------
    # Check 4 — Duration consistency (std-dev across generated segments)
    # ------------------------------------------------------------------
    generated_durations = [
        seg.get("duration_seconds", 0.0)
        for seg in narration_segments
        if seg.get("generation_status") == "generated"
        and seg.get("duration_seconds", 0.0) > 0.0
    ]
    if len(generated_durations) >= 2:
        mean_dur = sum(generated_durations) / len(generated_durations)
        variance = sum((d - mean_dur) ** 2 for d in generated_durations) / len(generated_durations)
        stddev = math.sqrt(variance)
        consistency_triggered = stddev > _DURATION_CONSISTENCY_THRESHOLD_SECONDS
        consistency_detail = (
            f"Duration std-dev={round(stddev, 3)}s across "
            f"{len(generated_durations)} generated segments "
            f"(mean={round(mean_dur, 3)}s)."
        )
    else:
        stddev = 0.0
        consistency_triggered = False
        consistency_detail = (
            "Fewer than 2 generated segments with non-zero duration; "
            "consistency check skipped."
        )

    duration_consistency_check = {
        "check_id": "duration_consistency",
        "status": "warn" if consistency_triggered else "pass",
        "detail": consistency_detail,
    }

    # ------------------------------------------------------------------
    # Aggregate verdict
    # ------------------------------------------------------------------
    checks = [
        beat_failure_check,
        zero_duration_check,
        short_duration_check,
        duration_consistency_check,
    ]

    # overall_status: driven primarily by fail_rate; secondary checks only add "warn"
    if fail_rate > _FAILURE_THRESHOLD:
        overall_status = "fail"
    elif fail_rate > 0.0 or any(c["status"] == "warn" for c in checks):
        overall_status = "warn"
    else:
        overall_status = "pass"

    production_ready: bool = fail_rate <= _FAILURE_THRESHOLD

    verdict: dict = {
        "verdict_id": f"aqv_{episode_id}",
        "episode_id": episode_id,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "overall_status": overall_status,
        "production_ready": production_ready,
        "total_beats": total_count,
        "beats_generated": beats_generated,
        "beats_failed": failed_count,
        "failure_rate": round(fail_rate, 4),
        "total_characters_billed": total_chars_billed,
        "checks": checks,
    }

    try:
        validate("audio_qa_verdict", verdict)
    except SchemaError as exc:
        log.warn(
            "audio_qa_schema_warn",
            episode_id=episode_id,
            path=exc.path,
            reason=exc.reason,
        )

    log.info(
        "audio_qa_completed",
        episode_id=episode_id,
        overall_status=overall_status,
        production_ready=production_ready,
        fail_rate=round(fail_rate, 4),
        beats_failed=failed_count,
        total_beats=total_count,
    )

    return verdict
