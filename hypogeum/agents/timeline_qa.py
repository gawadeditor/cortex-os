"""
hypogeum/agents/timeline_qa.py — Timeline QA (pre-publish hardening)

Four deterministic checks run against the timeline plan immediately after
timeline construction, before ffmpeg rendering. Results are embedded in
final_render_package.timeline_qa_report and evaluated by the QA Agent.

Checks:
  A. clip_aspect_ratio  — probe actual clip dimensions; warn on non-16:9 clips
  B. hold_duration      — compute hold_seconds per entry; warn/fail on excess
  C. missing_narration  — beat references in timeline absent from narration_package
  D. narration_coverage — timeline entries with non-empty beat reference but no audio file

These checks were designed to prevent a repeat of ep_smoke_01, where a visibly
broken video (square clip, 14+ second hold on first scene, silent second scene)
received an approved=true verdict because none of the prior checks inspected
actual clip dimensions, hold magnitude, or audio file presence.

Public API:
    run_timeline_qa(
        timeline_plan,
        narration_package=None,
        hold_warn_threshold_seconds=10.0,
        hold_fail_threshold_seconds=20.0,
    ) -> dict

    _probe_clip_dimensions(clip_path) -> tuple[int, int]  (width, height; 0,0 on error)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from hypogeum.agents.timeline_builder import TimelinePlan, TimelineEntry


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 16:9 = 1.7778; tolerance ±0.05 handles minor rounding across resolutions.
_EXPECTED_MAIN_RATIO: float = 16 / 9
_ASPECT_RATIO_TOLERANCE: float = 0.05

# Hold thresholds
_DEFAULT_HOLD_WARN_SECONDS: float = 10.0
_DEFAULT_HOLD_FAIL_SECONDS: float = 20.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_timeline_qa(
    timeline_plan: TimelinePlan,
    narration_package: Optional[dict] = None,
    hold_warn_threshold_seconds: float = _DEFAULT_HOLD_WARN_SECONDS,
    hold_fail_threshold_seconds: float = _DEFAULT_HOLD_FAIL_SECONDS,
) -> dict:
    """
    Run all four timeline QA checks and return a timeline_qa_report dict.

    Args:
        timeline_plan:               TimelinePlan from build_timelines().
        narration_package:           narration_package dict from Audio Agent.
                                     If None, check C is skipped.
        hold_warn_threshold_seconds: Hold duration above which a warning is issued.
                                     Default: 10.0s.
        hold_fail_threshold_seconds: Hold duration above which a failure is raised.
                                     Default: 20.0s.

    Returns:
        dict with overall_status ("pass"|"warn"|"fail"), checks, warnings,
        failures, and per-check detail lists.
    """
    all_entries = list(timeline_plan.main_timeline)
    for tl in timeline_plan.shorts_timelines.values():
        all_entries.extend(tl)

    # Build narration segment lookup once (beat_reference → segment dict)
    segment_by_beat: dict[str, dict] = {}
    if narration_package:
        for seg in narration_package.get("narration_segments", []):
            ref = seg.get("beat_reference", "")
            if ref:
                segment_by_beat[ref] = seg

    checks: list[dict] = []
    warnings: list[str] = []
    failures: list[str] = []

    # ------------------------------------------------------------------
    # Check A — Clip Aspect Ratio
    # ------------------------------------------------------------------
    aspect_offenders: list[dict] = []
    aspect_checked: int = 0
    aspect_unprobed: int = 0

    for entry in all_entries:
        if entry.clip_path is None or not entry.clip_path.exists():
            continue
        w, h = _probe_clip_dimensions(entry.clip_path)
        if w == 0 or h == 0:
            aspect_unprobed += 1
            continue
        aspect_checked += 1
        ratio = w / h
        is_16_9 = abs(ratio - _EXPECTED_MAIN_RATIO) <= _ASPECT_RATIO_TOLERANCE
        if not is_16_9:
            aspect_offenders.append({
                "scene_id": entry.scene_id,
                "source_type": entry.source_type,
                "resolution": f"{w}x{h}",
                "actual_ratio": round(ratio, 4),
                "expected_ratio": round(_EXPECTED_MAIN_RATIO, 4),
            })

    if aspect_offenders:
        detail = (
            f"{len(aspect_offenders)}/{aspect_checked + len(aspect_offenders)} "
            f"clip(s) are not 16:9"
        )
        _add_check(checks, "c_clip_aspect_ratio", "warn", detail)
        for off in aspect_offenders:
            warnings.append(
                f"clip_aspect_ratio: scene={off['scene_id']} "
                f"resolution={off['resolution']} ratio={off['actual_ratio']:.3f} "
                f"(expected 16:9=1.778)"
            )
    elif aspect_checked == 0:
        _add_check(checks, "c_clip_aspect_ratio", "warn",
                   "no clip files on disk — aspect ratio not verified")
        warnings.append("clip_aspect_ratio: no clip files found on disk to inspect")
    else:
        _add_check(checks, "c_clip_aspect_ratio", "pass",
                   f"all {aspect_checked} probed clip(s) are 16:9")

    # ------------------------------------------------------------------
    # Check B — Freeze/Hold Duration
    #
    # Only evaluate entries where a clip EXISTS on disk. Missing clips are
    # already surfaced in the scenes_missing list and in the clip_availability
    # check — double-counting them here produces spurious hold failures.
    # ------------------------------------------------------------------
    hold_offenders: list[dict] = []

    for entry in all_entries:
        if entry.clip_path is None or not entry.clip_path.exists():
            continue  # missing clip — already captured in scenes_missing
        hold_seconds = max(0.0, entry.duration_seconds - entry.clip_duration_seconds)
        if hold_seconds > 0.5:  # ignore sub-second rounding noise
            hold_offenders.append({
                "scene_id": entry.scene_id,
                "source_type": entry.source_type,
                "hold_seconds": round(hold_seconds, 3),
                "duration_seconds": round(entry.duration_seconds, 3),
                "clip_duration_seconds": round(entry.clip_duration_seconds, 3),
            })

    hold_offenders.sort(key=lambda x: x["hold_seconds"], reverse=True)
    failing_holds = [h for h in hold_offenders if h["hold_seconds"] > hold_fail_threshold_seconds]
    warning_holds = [h for h in hold_offenders if h["hold_seconds"] > hold_warn_threshold_seconds]

    if failing_holds:
        worst = failing_holds[0]
        _add_check(checks, "c_hold_duration", "fail",
                   f"{len(failing_holds)} entry(ies) exceed {hold_fail_threshold_seconds}s "
                   f"hold threshold; worst={worst['hold_seconds']}s (scene={worst['scene_id']})")
        for h in failing_holds:
            failures.append(
                f"hold_duration: scene={h['scene_id']} hold={h['hold_seconds']}s "
                f"(narration={h['duration_seconds']}s, clip={h['clip_duration_seconds']}s)"
            )
    elif warning_holds:
        worst = warning_holds[0]
        _add_check(checks, "c_hold_duration", "warn",
                   f"{len(warning_holds)} entry(ies) exceed {hold_warn_threshold_seconds}s "
                   f"hold threshold; worst={worst['hold_seconds']}s (scene={worst['scene_id']})")
        for h in warning_holds:
            warnings.append(
                f"hold_duration: scene={h['scene_id']} hold={h['hold_seconds']}s "
                f"(narration={h['duration_seconds']}s, clip={h['clip_duration_seconds']}s)"
            )
    else:
        _add_check(checks, "c_hold_duration", "pass",
                   f"no entries exceed {hold_warn_threshold_seconds}s hold threshold")

    # ------------------------------------------------------------------
    # Check C — Missing Narration References
    # ------------------------------------------------------------------
    missing_refs: list[dict] = []
    file_missing_refs: list[dict] = []

    for entry in all_entries:
        ref = entry.narration_reference or ""
        if not ref:
            continue  # scene has no narration reference — not an error
        if narration_package is not None and ref not in segment_by_beat:
            # Reference exists in timeline but not in narration_package segments
            missing_refs.append({"scene_id": entry.scene_id, "narration_reference": ref})
        elif entry.narration_path is None or not entry.narration_path.exists():
            # Reference is in package but audio file is missing on disk
            file_missing_refs.append({"scene_id": entry.scene_id, "narration_reference": ref})

    if narration_package is None:
        _add_check(checks, "c_missing_narration_refs", "warn",
                   "narration_package not provided — cross-reference check skipped")
        warnings.append("missing_narration_refs: narration_package not provided")
    elif missing_refs or file_missing_refs:
        total_missing = len(missing_refs) + len(file_missing_refs)
        _add_check(checks, "c_missing_narration_refs", "warn",
                   f"{total_missing} narration reference(s) unresolved: "
                   f"{len(missing_refs)} not in package, "
                   f"{len(file_missing_refs)} file(s) missing on disk")
        for m in missing_refs:
            warnings.append(
                f"missing_narration_refs: scene={m['scene_id']} "
                f"reference={m['narration_reference']!r} not found in narration_package"
            )
        for m in file_missing_refs:
            warnings.append(
                f"missing_narration_refs: scene={m['scene_id']} "
                f"reference={m['narration_reference']!r} in package but audio file missing"
            )
    else:
        _add_check(checks, "c_missing_narration_refs", "pass",
                   "all narration references resolved to audio files on disk")

    # ------------------------------------------------------------------
    # Check D — Real Narration Coverage (non-self-referential)
    #
    # Prior approach: assembly_qa_report compared timeline-sum against itself,
    # always reporting 100% coverage even when audio files were absent.
    #
    # New approach: separately sum (a) expected narration duration from the
    # timeline and (b) duration actually reachable via audio files on disk.
    # Gap = expected − reachable. A positive gap means silent scenes.
    # ------------------------------------------------------------------
    main_entries = list(timeline_plan.main_timeline)
    narration_expected_seconds = sum(
        e.duration_seconds for e in main_entries if (e.narration_reference or "")
    )
    narration_covered_seconds = sum(
        e.duration_seconds
        for e in main_entries
        if (e.narration_reference or "")
        and e.narration_path is not None
        and e.narration_path.exists()
    )
    narration_gap_seconds = max(0.0, narration_expected_seconds - narration_covered_seconds)

    silent_entries = [
        {
            "scene_id": e.scene_id,
            "narration_reference": e.narration_reference,
            "duration_seconds": round(e.duration_seconds, 3),
            "reason": (
                "reference_not_in_package"
                if narration_package is not None
                and (e.narration_reference or "") not in segment_by_beat
                else "audio_file_missing_on_disk"
            ),
        }
        for e in main_entries
        if (e.narration_reference or "")
        and (e.narration_path is None or not e.narration_path.exists())
    ]

    if narration_gap_seconds > 5.0:
        _add_check(checks, "c_narration_coverage_real", "fail",
                   f"narration gap={narration_gap_seconds:.1f}s — "
                   f"{len(silent_entries)} scene(s) will render silent")
        failures.append(
            f"narration_coverage_real: {narration_gap_seconds:.1f}s of expected narration "
            f"has no audio file — {len(silent_entries)} scene(s) will be silent"
        )
    elif narration_gap_seconds > 0.0:
        _add_check(checks, "c_narration_coverage_real", "warn",
                   f"narration gap={narration_gap_seconds:.1f}s — "
                   f"{len(silent_entries)} scene(s) may render silent")
        warnings.append(
            f"narration_coverage_real: {narration_gap_seconds:.1f}s gap — "
            f"{len(silent_entries)} scene(s) may render without narration audio"
        )
    else:
        _add_check(checks, "c_narration_coverage_real", "pass",
                   f"narration fully covered: {narration_covered_seconds:.1f}s / "
                   f"{narration_expected_seconds:.1f}s")

    # ------------------------------------------------------------------
    # Overall status
    # ------------------------------------------------------------------
    if failures:
        overall_status = "fail"
    elif warnings:
        overall_status = "warn"
    else:
        overall_status = "pass"

    return {
        "overall_status": overall_status,
        "checks": checks,
        "warnings": warnings,
        "failures": failures,
        "clip_aspect_ratio_offenders": aspect_offenders,
        "hold_offenders": hold_offenders,
        "missing_narration_refs": missing_refs + file_missing_refs,
        "silent_entries": silent_entries,
        "narration_coverage": {
            "expected_seconds": round(narration_expected_seconds, 3),
            "covered_seconds": round(narration_covered_seconds, 3),
            "gap_seconds": round(narration_gap_seconds, 3),
        },
    }


def _probe_clip_dimensions(clip_path: Path) -> tuple[int, int]:
    """
    Return (width, height) of a video clip using ffprobe.

    Returns (0, 0) when:
    - clip_path does not exist
    - ffprobe is unavailable or exits non-zero
    - Output cannot be parsed as integers

    Used by check A (clip aspect ratio validation).
    """
    if not clip_path.exists():
        return 0, 0
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "stream=width,height",
                "-of", "default=noprint_wrappers=1",
                str(clip_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return 0, 0
        width = height = 0
        for line in result.stdout.splitlines():
            if line.startswith("width="):
                width = int(line.split("=", 1)[1])
            elif line.startswith("height="):
                height = int(line.split("=", 1)[1])
        return width, height
    except Exception:
        return 0, 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _add_check(checks: list, check_id: str, status: str, detail: str) -> None:
    checks.append({"check_id": check_id, "status": status, "detail": detail})
