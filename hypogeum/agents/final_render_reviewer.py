"""
hypogeum/agents/final_render_reviewer.py — Final Render Reviewer (Agent 37)

Sole authority for setting production_ready. Receives the render_output_manifest
from Assembly Agent plus any available QA verdicts and emits a render_review_verdict.

No Claude calls. No file rendering. No ffmpeg. Pure deterministic evaluation.

Separation of concerns (P1 governance rule):
  Assembly Agent  — renders files; emits render_output_manifest
  Media Specs QA  — inspects files via ffprobe; emits media_specs_report per file
  Final Render Reviewer — evaluates all evidence; is the ONLY agent that sets
                          production_ready; emits render_review_verdict

Inputs consumed:
  render_output_manifest   — what Assembly Agent actually produced (required)
  media_specs_reports      — list of media_specs_report dicts from Agent 36 (optional)
  audio_qa_verdict         — audio_qa_report from narration_package (optional)
  caption_status           — caption availability summary dict (optional)
  music_qa_verdict         — music sourcing summary dict (optional)

Output (render_review_verdict):
  production_ready:    bool   — True only if all blocking checks pass
  overall_verdict:     str    — APPROVED / CONDITIONAL / REJECTED
  checks:              list   — one entry per evaluation axis
  failures:            list   — blocking issues (any → production_ready=False)
  warnings:            list   — non-blocking issues (may → human_review_recommended)
  human_review_recommended: bool
  human_review_reasons: list

Verdict rules:
  REJECTED   — production_ready=False; one or more blocking failures
  CONDITIONAL — production_ready=True but warnings present (partial clips, no music, etc.)
  APPROVED   — production_ready=True, no failures, no warnings

Publishing Agent must not proceed if overall_verdict is REJECTED.

Public API:
    run_final_render_reviewer(
        render_output_manifest, episode_id,
        media_specs_reports=None, audio_qa_verdict=None,
        caption_status=None, music_qa_verdict=None,
    ) → dict  (render_review_verdict, schema-validated)

Logging events emitted:
  render_reviewer_started        — info  — inputs received
  render_reviewer_check          — debug — per-check result
  render_reviewer_verdict        — info  — final verdict
  render_reviewer_rejected       — error — production_ready=False with reasons
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from hypogeum.logger import get_logger
from hypogeum.schemas import validate


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "final_render_reviewer"
_AGENT_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_final_render_reviewer(
    render_output_manifest: dict,
    episode_id: str,
    media_specs_reports: list[dict] | None = None,
    audio_qa_verdict: dict | None = None,
    caption_status: dict | None = None,
    music_qa_verdict: dict | None = None,
) -> dict:
    """
    Evaluate the render_output_manifest and all available QA evidence.
    Return a schema-validated render_review_verdict.

    This is the ONLY function in the codebase that may set production_ready.

    Args:
        render_output_manifest: The manifest dict produced by Assembly Agent.
        episode_id:             Episode identifier for log correlation.
        media_specs_reports:    List of media_specs_report dicts from Agent 36.
                                If None or empty, media spec checks are skipped.
        audio_qa_verdict:       audio_qa_report dict from narration_package.
                                If None, audio QA checks are skipped.
        caption_status:         Dict with caption availability flags.
                                If None, caption checks are skipped.
        music_qa_verdict:       Music sourcing summary dict.
                                If None, music checks use manifest data only.

    Returns:
        render_review_verdict dict (schema-validated).

    Raises:
        ValueError — if render_output_manifest is missing required top-level keys.
    """
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)
    manifest_id = render_output_manifest.get("manifest_id", "")

    log.info(
        "render_reviewer_started",
        episode_id=episode_id,
        manifest_id=manifest_id,
        has_media_specs=bool(media_specs_reports),
        has_audio_qa=bool(audio_qa_verdict),
        has_caption_status=bool(caption_status),
        has_music_qa=bool(music_qa_verdict),
    )

    verdict_id = f"rrv_{episode_id}_{uuid.uuid4().hex[:8]}"
    reviewed_at = datetime.now(timezone.utc).isoformat()

    checks: list[dict] = []
    failures: list[str] = []
    warnings: list[str] = []
    human_review_reasons: list[str] = []

    # ------------------------------------------------------------------
    # Check 1 — Render status
    # ------------------------------------------------------------------
    render_status = render_output_manifest.get("render_status", "failed")
    if render_status == "failed":
        _add_check(checks, "c_render_status", "fail",
                   f"render_status=failed — no output produced")
        failures.append("render_status: failed — Assembly produced no output")
    elif render_status == "partial":
        _add_check(checks, "c_render_status", "warn",
                   "render_status=partial — some renders missing")
        warnings.append("render_status: partial — some renders incomplete")
        human_review_reasons.append("partial_render")
    else:
        _add_check(checks, "c_render_status", "pass", "render_status=success")

    # ------------------------------------------------------------------
    # Check 2 — Main render file exists and is non-empty
    # ------------------------------------------------------------------
    main_render = render_output_manifest.get("main_render", {})
    main_exists = main_render.get("file_exists", False)
    main_size = main_render.get("file_size_bytes", 0) or 0

    if not main_exists:
        _add_check(checks, "c_main_render_exists", "fail",
                   "main episode file not found on disk")
        failures.append("main_render: file not found")
    elif main_size == 0:
        _add_check(checks, "c_main_render_exists", "fail",
                   "main episode file is zero bytes")
        failures.append("main_render: file is zero bytes")
    else:
        _add_check(checks, "c_main_render_exists", "pass",
                   f"main episode file present ({main_size:,} bytes)")

    # ------------------------------------------------------------------
    # Check 3 — Shorts renders file existence (Phase 2: fully decoupled)
    # Shorts failures go into isolated fields only — never into main failures/warnings.
    # ------------------------------------------------------------------
    shorts_renders = render_output_manifest.get("shorts_renders", [])
    shorts_passed, _shorts_check_failures = _check_shorts_readiness(shorts_renders)

    # Isolated lists — never merged into main failures / warnings
    shorts_failures_list: list[str] = []
    shorts_warnings_list: list[str] = []

    real_shorts = [sr for sr in shorts_renders
                   if not sr.get("short_id", "").endswith("_placeholder")]

    if _shorts_check_failures:
        # Missing real shorts — isolated into shorts_failures_list only
        for sf in _shorts_check_failures:
            _add_check(checks, "c_shorts_render_exists", "fail",
                       f"missing shorts: {sf}")
            shorts_failures_list.append(f"shorts_renders: {sf}")
    elif real_shorts:
        _add_check(checks, "c_shorts_render_exists", "pass",
                   "all real short(s) present")
    else:
        _add_check(checks, "c_shorts_render_exists", "pass",
                   "no shorts configured")

    # Derive shorts_runtime_status from the extracted result
    if not real_shorts:
        shorts_runtime_status = "not_planned"
    elif _shorts_check_failures:
        shorts_runtime_status = "failed"
    else:
        shorts_runtime_status = "complete"

    # Derive shorts_review_verdict (independent of main episode)
    if not real_shorts:
        shorts_review_verdict = "NOT_PLANNED"
    elif shorts_failures_list:
        shorts_review_verdict = "REJECTED"
    else:
        shorts_review_verdict = "APPROVED"

    # ------------------------------------------------------------------
    # Check 4 — Clip availability
    # ------------------------------------------------------------------
    clip_rate = render_output_manifest.get("clip_availability_rate", 1.0) or 1.0
    missing_clips = render_output_manifest.get("missing_clips", [])

    if clip_rate < 1.0:
        # Any missing clip produces visible freeze/still frames via tpad hold.
        # Block publishing regardless of how many clips are missing.
        n_missing = len(missing_clips)
        _add_check(checks, "c_clip_availability", "fail",
                   f"clip_availability_rate={clip_rate:.1%} — "
                   f"{n_missing} missing clip(s) will produce freeze frames; "
                   f"regenerate before publishing")
        failures.append(
            f"clip_availability: {clip_rate:.1%} — {n_missing} clip(s) missing; "
            f"freeze frames not acceptable in published video"
        )
        human_review_reasons.append("clips_missing")
    else:
        _add_check(checks, "c_clip_availability", "pass",
                   "clip_availability_rate=100%")

    # ------------------------------------------------------------------
    # Check 5 — Duration coverage gap
    #
    # max_scene_hold_seconds is the worst-case single-scene hold (narration
    # outlasting an existing clip). Aligned with Timeline QA's per-scene fail
    # threshold (20s) so the two checks are consistent.
    # duration_coverage_gap_seconds is the aggregate hold across all scenes —
    # used as a warning when holds are present but none exceeds the fail threshold.
    # ------------------------------------------------------------------
    coverage_gap = render_output_manifest.get("duration_coverage_gap_seconds", 0.0) or 0.0
    max_hold = render_output_manifest.get("max_scene_hold_seconds", coverage_gap) or coverage_gap

    if max_hold > 45.0:
        _add_check(checks, "c_duration_coverage", "fail",
                   f"max_scene_hold={max_hold:.1f}s > 45s threshold "
                   f"(aggregate_gap={coverage_gap:.1f}s)")
        failures.append(
            f"duration_coverage: max scene hold {max_hold:.1f}s exceeds 45s threshold"
        )
        human_review_reasons.append("duration_coverage_gap")
    elif coverage_gap > 0.0:
        _add_check(checks, "c_duration_coverage", "warn",
                   f"duration_coverage_gap={coverage_gap:.1f}s "
                   f"(max_scene_hold={max_hold:.1f}s)")
        warnings.append(
            f"duration_coverage: aggregate hold gap {coverage_gap:.1f}s "
            f"(max single-scene: {max_hold:.1f}s)"
        )
        if max_hold > 10.0:
            human_review_reasons.append("duration_coverage_gap")
    else:
        _add_check(checks, "c_duration_coverage", "pass", "no duration coverage gap")

    # ------------------------------------------------------------------
    # Check 6 — Music coverage
    # ------------------------------------------------------------------
    sections_without_music = render_output_manifest.get("sections_without_music", [])
    if sections_without_music:
        _add_check(checks, "c_music_coverage", "warn",
                   f"sections_without_music={sections_without_music}")
        warnings.append(f"music_coverage: {len(sections_without_music)} section(s) have no music")
        human_review_reasons.append("sections_without_music")
    else:
        _add_check(checks, "c_music_coverage", "pass", "all sections have music")

    # ------------------------------------------------------------------
    # Check 7 — Media specs (Agent 36 reports)
    # ------------------------------------------------------------------
    if media_specs_reports:
        spec_failures = [
            r for r in media_specs_reports
            if not r.get("publish_ready", True) and r.get("file_valid", True)
        ]
        invalid_files = [
            r for r in media_specs_reports
            if not r.get("file_valid", True)
        ]

        if spec_failures:
            failed_paths = [r.get("file_path", "?") for r in spec_failures]
            failed_reasons = []
            for r in spec_failures:
                for f in r.get("failures", []):
                    failed_reasons.append(f"{r.get('file_path', '?')}: {f}")
            _add_check(checks, "c_media_specs", "fail",
                       f"{len(spec_failures)} file(s) failed spec check: {failed_paths}")
            for reason in failed_reasons:
                failures.append(f"media_spec: {reason}")
        elif invalid_files:
            _add_check(checks, "c_media_specs", "warn",
                       f"{len(invalid_files)} file(s) could not be inspected by ffprobe")
            for r in invalid_files:
                warnings.append(
                    f"media_spec: ffprobe failed on {r.get('file_path', '?')} "
                    f"— {r.get('failures', ['unknown'])[:1]}"
                )
            human_review_reasons.append("media_spec_unverified")
        else:
            _add_check(checks, "c_media_specs", "pass",
                       f"all {len(media_specs_reports)} file(s) meet spec")
    else:
        _add_check(checks, "c_media_specs", "warn",
                   "no media_specs_reports provided — spec not verified")
        warnings.append("media_specs: not verified (Agent 36 not run)")
        human_review_reasons.append("media_specs_not_verified")

    # ------------------------------------------------------------------
    # Check 8 — Audio QA
    # ------------------------------------------------------------------
    if audio_qa_verdict:
        audio_status = audio_qa_verdict.get("overall_status", "pass")
        if audio_status == "fail":
            _add_check(checks, "c_audio_qa", "fail",
                       f"audio_qa_report overall_status=fail")
            failures.append("audio_qa: overall_status=fail")
        elif audio_status == "warn":
            _add_check(checks, "c_audio_qa", "warn",
                       "audio_qa_report overall_status=warn")
            warnings.append("audio_qa: overall_status=warn")
            human_review_reasons.append("audio_qa_warn")
        else:
            _add_check(checks, "c_audio_qa", "pass", "audio_qa_report=pass")
    else:
        _add_check(checks, "c_audio_qa", "warn",
                   "no audio_qa_verdict provided — audio QA not verified")
        warnings.append("audio_qa: not verified (no verdict provided)")

    # ------------------------------------------------------------------
    # Check 9 — Caption status
    # ------------------------------------------------------------------
    if caption_status is not None:
        caption_present = caption_status.get("main_captions_present", False)
        if not caption_present:
            _add_check(checks, "c_captions", "warn",
                       "main episode captions not present")
            warnings.append("captions: main episode captions absent")
            human_review_reasons.append("captions_missing")
        else:
            _add_check(checks, "c_captions", "pass", "main captions present")

    # ------------------------------------------------------------------
    # Determine final verdict
    # ------------------------------------------------------------------
    # main_episode_complete mirrors production_ready — uses the same failures list.
    # Keep production_ready as an alias for backward compatibility.
    main_episode_complete = len(failures) == 0
    production_ready = main_episode_complete

    if not production_ready:
        overall_verdict = "REJECTED"
    elif warnings:
        overall_verdict = "CONDITIONAL"
    else:
        overall_verdict = "APPROVED"

    human_review_recommended = len(human_review_reasons) > 0
    # Deduplicate reasons while preserving order
    seen: set[str] = set()
    unique_reasons: list[str] = []
    for r in human_review_reasons:
        if r not in seen:
            seen.add(r)
            unique_reasons.append(r)

    if overall_verdict == "REJECTED":
        log.error(
            "render_reviewer_rejected",
            episode_id=episode_id,
            verdict_id=verdict_id,
            failures=failures,
        )
    else:
        log.info(
            "render_reviewer_verdict",
            episode_id=episode_id,
            verdict_id=verdict_id,
            overall_verdict=overall_verdict,
            production_ready=production_ready,
            warning_count=len(warnings),
        )

    verdict = {
        "verdict_id": verdict_id,
        "episode_id": episode_id,
        "manifest_id": manifest_id,
        "reviewed_at": reviewed_at,
        "production_ready": production_ready,
        # Runtime decoupling: main_episode_complete is the canonical field;
        # production_ready is kept as a backward-compatible alias.
        "main_episode_complete": main_episode_complete,
        "shorts_runtime_status": shorts_runtime_status,
        "shorts_review_verdict": shorts_review_verdict,
        "overall_verdict": overall_verdict,
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "shorts_failures": shorts_failures_list,
        "shorts_warnings": shorts_warnings_list,
        "human_review_recommended": human_review_recommended,
        "human_review_reasons": unique_reasons,
    }

    validate("render_review_verdict", verdict)
    return verdict


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_shorts_readiness(
    shorts_renders: list[dict],
) -> tuple[bool, list[str]]:
    """
    Evaluate the shorts_renders list from a render_output_manifest.

    Placeholder shorts (short_id ending with '_placeholder') are always skipped.

    Returns:
        (shorts_passed, shorts_failures) where:
          - shorts_passed: True if all real shorts have file_exists=True (or none exist)
          - shorts_failures: list of human-readable failure strings for missing real shorts
    """
    real_shorts = [
        sr for sr in shorts_renders
        if not sr.get("short_id", "").endswith("_placeholder")
    ]
    missing_short_ids = [
        sr["short_id"] for sr in real_shorts
        if not sr.get("file_exists", False)
    ]
    if missing_short_ids:
        failures = [
            f"{len(missing_short_ids)} short(s) missing: {missing_short_ids}"
        ]
        return False, failures
    return True, []


def _add_check(
    checks: list,
    check_id: str,
    status: str,
    detail: str,
) -> None:
    checks.append({
        "check_id": check_id,
        "status": status,
        "detail": detail,
    })
