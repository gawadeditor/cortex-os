"""
hypogeum/agents/qa_agent.py — QA Agent (R47)

Pre-publish approval gate. Reads the final_render_package plus all available
QA verdicts and emits a qa_approval_report with an APPROVED, WARNING, or REJECTED
verdict. The Publishing Agent must not run unless the verdict is APPROVED or WARNING.
REJECTED blocks publishing entirely.

No Claude calls. Pure deterministic evaluation.

Inputs consumed:
  final_render_package    — Assembly Agent output (required)
  render_review_verdict   — Final Render Reviewer verdict (optional)
  audio_qa_verdict        — Audio QA Agent verdict (optional)
  media_specs_reports     — list of media_specs_report dicts from Agent 36 (optional)
  thumbnail_package       — Thumbnail Director output (optional)

Output (qa_approval_report):
  verdict:   str   — APPROVED / WARNING / REJECTED
  approved:  bool  — True for APPROVED and WARNING; False for REJECTED
  checks:    list  — one entry per evaluation axis
  failures:  list  — blocking issues (any → verdict=REJECTED, approved=False)
  warnings:  list  — non-blocking issues (→ verdict=WARNING if no failures)

Verdict rules:
  REJECTED — approved=False; one or more blocking failures
  WARNING  — approved=True; no failures but warnings present
  APPROVED — approved=True; no failures and no warnings

Publishing Agent gate: pipeline raises QARejectedError if verdict is REJECTED.

Public API:
    run_qa_agent(
        final_render_package, episode_id,
        render_review_verdict=None,
        audio_qa_verdict=None,
        media_specs_reports=None,
        thumbnail_package=None,
        output_dir=None,
    ) → dict  (qa_approval_report, schema-validated, written to disk)

Logging events emitted:
  qa_agent_started    — info  — inputs received
  qa_agent_check      — debug — per-check result
  qa_agent_verdict    — info  — final verdict (APPROVED/WARNING)
  qa_agent_rejected   — error — verdict=REJECTED with failure list
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.identifiers import METADATA_VERSION
from hypogeum.logger import get_logger
from hypogeum.schemas import validate


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "qa_agent"
_AGENT_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_qa_agent(
    final_render_package: dict,
    episode_id: str,
    render_review_verdict: Optional[dict] = None,
    audio_qa_verdict: Optional[dict] = None,
    media_specs_reports: Optional[list] = None,
    thumbnail_package: Optional[dict] = None,
    run_id: Optional[str] = None,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Evaluate all pre-publish evidence and return a qa_approval_report.

    Args:
        final_render_package:   Final Render Package from Assembly Agent (required).
        episode_id:             Episode identifier for file naming and log correlation.
        render_review_verdict:  render_review_verdict from Final Render Reviewer (optional).
        audio_qa_verdict:       audio_qa_verdict dict from Audio QA Agent (optional).
        media_specs_reports:    List of media_specs_report dicts from Agent 36 (optional).
        thumbnail_package:      thumbnail_package from Thumbnail Director (optional).
        output_dir:             Override directory for the output file.
                                Defaults to .hypogeum-assets/episodes/{episode_id}/.

    Returns:
        qa_approval_report dict (schema-validated, written to disk).
    """
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    log.info(
        "qa_agent_started",
        episode_id=episode_id,
        has_render_review=bool(render_review_verdict),
        has_audio_qa=bool(audio_qa_verdict),
        has_media_specs=bool(media_specs_reports),
        has_thumbnail=bool(thumbnail_package),
    )

    report_id = f"qa_{episode_id}_{uuid.uuid4().hex[:8]}"
    evaluated_at = datetime.now(timezone.utc).isoformat()

    checks: list[dict] = []
    failures: list[str] = []
    warnings: list[str] = []

    # ------------------------------------------------------------------
    # Check 1 — Render package production_ready
    # ------------------------------------------------------------------
    pkg_integrity = final_render_package.get("package_integrity", {})
    pkg_ready = pkg_integrity.get("production_ready", False)

    if not isinstance(pkg_ready, bool) or not pkg_ready:
        _add_check(checks, "c_render_package_production_ready", "fail",
                   "final_render_package.package_integrity.production_ready is not True")
        failures.append(
            "render_package: production_ready=False — assembly did not produce a viable package"
        )
    else:
        _add_check(checks, "c_render_package_production_ready", "pass",
                   "final_render_package.package_integrity.production_ready=True")

    # ------------------------------------------------------------------
    # Check 2 — Assembly QA report overall_status
    # ------------------------------------------------------------------
    assembly_qa = final_render_package.get("assembly_qa_report", {})
    assembly_status = assembly_qa.get("overall_status", "fail")

    if assembly_status == "fail":
        _add_check(checks, "c_assembly_qa_report", "fail",
                   "assembly_qa_report.overall_status=fail")
        failures.append("assembly_qa: overall_status=fail")
    elif assembly_status == "warn":
        _add_check(checks, "c_assembly_qa_report", "warn",
                   "assembly_qa_report.overall_status=warn")
        warnings.append("assembly_qa: overall_status=warn")
    else:
        _add_check(checks, "c_assembly_qa_report", "pass",
                   "assembly_qa_report.overall_status=pass")

    # ------------------------------------------------------------------
    # Check 3 — Main video asset present
    # ------------------------------------------------------------------
    episode_render = final_render_package.get("episode_render", {})
    video_asset_id = episode_render.get("video_asset_id", "")

    if not video_asset_id:
        _add_check(checks, "c_main_video_asset", "fail",
                   "episode_render.video_asset_id is empty — no main video to publish")
        failures.append("main_video_asset: video_asset_id is empty")
    else:
        _add_check(checks, "c_main_video_asset", "pass",
                   f"main video asset present: {video_asset_id}")

    # ------------------------------------------------------------------
    # Check 4 — Captions asset present
    # ------------------------------------------------------------------
    caption_asset_id = episode_render.get("caption_asset_id", "")

    if not caption_asset_id:
        _add_check(checks, "c_captions_asset", "fail",
                   "episode_render.caption_asset_id is empty — captions are required")
        failures.append("captions_asset: caption_asset_id is empty — captions are required for publish")
    else:
        _add_check(checks, "c_captions_asset", "pass",
                   f"captions asset present: {caption_asset_id}")

    # ------------------------------------------------------------------
    # Check 5 — Publishing metadata complete
    # ------------------------------------------------------------------
    metadata_source = final_render_package.get("metadata_source", {})
    episode_title = metadata_source.get("episode_title", "")
    ai_disclosure = metadata_source.get("ai_disclosure_text", "")

    if not episode_title:
        _add_check(checks, "c_metadata_complete", "fail",
                   "metadata_source.episode_title is empty — cannot publish without title")
        failures.append("metadata_complete: episode_title is empty")
    elif not ai_disclosure:
        _add_check(checks, "c_metadata_complete", "fail",
                   "metadata_source.ai_disclosure_text is empty — AI disclosure required")
        failures.append("metadata_complete: ai_disclosure_text is empty")
    else:
        _add_check(checks, "c_metadata_complete", "pass",
                   f"metadata complete — title={episode_title!r}")

    # ------------------------------------------------------------------
    # Check 6 — Render review verdict (if provided)
    # ------------------------------------------------------------------
    if render_review_verdict is not None:
        rrv = render_review_verdict.get("overall_verdict", "REJECTED")
        if rrv == "REJECTED":
            _add_check(checks, "c_render_review", "fail",
                       "render_review_verdict.overall_verdict=REJECTED")
            failures.append(
                "render_review: overall_verdict=REJECTED — render reviewer blocked publishing"
            )
        elif rrv == "CONDITIONAL":
            _add_check(checks, "c_render_review", "warn",
                       "render_review_verdict.overall_verdict=CONDITIONAL — human review recommended")
            warnings.append("render_review: overall_verdict=CONDITIONAL")
        else:
            _add_check(checks, "c_render_review", "pass",
                       "render_review_verdict.overall_verdict=APPROVED")
    else:
        _add_check(checks, "c_render_review", "warn",
                   "no render_review_verdict provided — render review not verified")
        warnings.append("render_review: not verified (Final Render Reviewer not run)")

    # ------------------------------------------------------------------
    # Check 7 — Audio QA verdict (if provided)
    # ------------------------------------------------------------------
    if audio_qa_verdict is not None:
        audio_ready = audio_qa_verdict.get("production_ready", False)
        audio_status = audio_qa_verdict.get("overall_status", "fail")
        if not audio_ready or audio_status == "fail":
            _add_check(checks, "c_audio_qa", "fail",
                       f"audio_qa_verdict.production_ready={audio_ready}, "
                       f"overall_status={audio_status}")
            failures.append(
                f"audio_qa: production_ready={audio_ready} — narration not approved"
            )
        elif audio_status == "warn":
            _add_check(checks, "c_audio_qa", "warn",
                       "audio_qa_verdict.overall_status=warn")
            warnings.append("audio_qa: overall_status=warn — some narration issues")
        else:
            _add_check(checks, "c_audio_qa", "pass",
                       "audio_qa_verdict: production_ready=True, overall_status=pass")
    else:
        _add_check(checks, "c_audio_qa", "warn",
                   "no audio_qa_verdict provided — audio QA not verified")
        warnings.append("audio_qa: not verified (no verdict provided)")

    # ------------------------------------------------------------------
    # Check 8 — Thumbnail present (if thumbnail_package provided)
    # ------------------------------------------------------------------
    if thumbnail_package is not None:
        main_thumb = thumbnail_package.get("main_thumbnail", {})
        thumb_asset_id = main_thumb.get("asset_id", "")
        if not thumb_asset_id:
            _add_check(checks, "c_thumbnail_present", "fail",
                       "thumbnail_package.main_thumbnail.asset_id is empty — thumbnail required")
            failures.append("thumbnail: main thumbnail asset_id missing — thumbnail required for publish")
        else:
            _add_check(checks, "c_thumbnail_present", "pass",
                       f"main thumbnail present: {thumb_asset_id}")
    else:
        _add_check(checks, "c_thumbnail_present", "warn",
                   "no thumbnail_package provided — thumbnail not verified")
        warnings.append("thumbnail: not verified (no thumbnail_package provided)")

    # ------------------------------------------------------------------
    # Check 10 — Thumbnail QA report (dimensions, asset_class, path, text overlay)
    # ------------------------------------------------------------------
    tq_report: dict = {}
    if thumbnail_package is not None:
        tq_report = thumbnail_package.get("thumbnail_qa_report", {})

    tq_status = tq_report.get("overall_status", "")

    if not tq_report or not tq_status:
        _add_check(checks, "c_thumbnail_qa", "warn",
                   "thumbnail_qa_report not present in thumbnail_package — "
                   "run Thumbnail Director with QA enabled")
        warnings.append("thumbnail_qa: not present in thumbnail_package")
    elif tq_status == "fail":
        _add_check(checks, "c_thumbnail_qa", "fail",
                   f"thumbnail_qa_report.overall_status=fail — "
                   f"{len(tq_report.get('failures', []))} blocking issue(s)")
        for f in tq_report.get("failures", []):
            failures.append(f"thumbnail_qa: {f}")
    elif tq_status == "warn":
        tq_warnings = tq_report.get("warnings", [])
        _add_check(checks, "c_thumbnail_qa", "warn",
                   f"thumbnail_qa_report.overall_status=warn — "
                   f"{len(tq_warnings)} issue(s) detected")
        for w in tq_warnings:
            warnings.append(f"thumbnail_qa: {w}")
    else:
        _add_check(checks, "c_thumbnail_qa", "pass",
                   "thumbnail_qa_report.overall_status=pass — all thumbnail checks clean")

    # ------------------------------------------------------------------
    # Check 9 — Timeline QA report (aspect ratio, hold, narration coverage)
    # ------------------------------------------------------------------
    timeline_qa = final_render_package.get("timeline_qa_report", {})
    tqa_status = timeline_qa.get("overall_status", "")

    if not timeline_qa or not tqa_status:
        _add_check(checks, "c_timeline_qa", "warn",
                   "timeline_qa_report not present — run assembly with Timeline QA enabled")
        warnings.append("timeline_qa: not present in final_render_package")
    elif tqa_status == "fail":
        _add_check(checks, "c_timeline_qa", "fail",
                   f"timeline_qa_report.overall_status=fail — "
                   f"{len(timeline_qa.get('failures', []))} blocking issue(s)")
        for f in timeline_qa.get("failures", []):
            failures.append(f"timeline_qa: {f}")
    elif tqa_status == "warn":
        tqa_warnings = timeline_qa.get("warnings", [])
        _add_check(checks, "c_timeline_qa", "warn",
                   f"timeline_qa_report.overall_status=warn — "
                   f"{len(tqa_warnings)} issue(s) detected")
        for w in tqa_warnings:
            warnings.append(f"timeline_qa: {w}")
    else:
        _add_check(checks, "c_timeline_qa", "pass",
                   "timeline_qa_report.overall_status=pass — all timeline checks clean")

    # ------------------------------------------------------------------
    # Check 11 — Video dimensions (1920×1080 required)
    # ------------------------------------------------------------------
    video_dim_checked = False
    if media_specs_reports:
        for msr in media_specs_reports:
            if isinstance(msr, dict) and msr.get("file_type") in ("video", "render"):
                obs = msr.get("specs_observed", {})
                w = obs.get("width")
                h = obs.get("height")
                if w is not None and h is not None:
                    video_dim_checked = True
                    if w == 1920 and h == 1080:
                        _add_check(checks, "c_video_dimensions", "pass",
                                   f"main video dimensions {w}×{h} match 1920×1080")
                    else:
                        _add_check(checks, "c_video_dimensions", "fail",
                                   f"main video is {w}×{h}, expected 1920×1080")
                        failures.append(f"video_dimensions: {w}×{h} does not match required 1920×1080")
                    break

    if not video_dim_checked:
        _add_check(checks, "c_video_dimensions", "warn",
                   "video dimensions not verified — no media_specs_report with width/height")
        warnings.append("video_dimensions: not verified (no media_specs_report provided)")

    # ------------------------------------------------------------------
    # Check 12 — Thumbnail dimensions (1280×720 required)
    # ------------------------------------------------------------------
    if thumbnail_package is not None:
        main_thumb_dim = thumbnail_package.get("main_thumbnail", {})
        tw = main_thumb_dim.get("width")
        th = main_thumb_dim.get("height")
        if tw is not None and th is not None:
            if tw == 1280 and th == 720:
                _add_check(checks, "c_thumbnail_dimensions", "pass",
                           f"thumbnail dimensions {tw}×{th} match 1280×720")
            else:
                _add_check(checks, "c_thumbnail_dimensions", "fail",
                           f"thumbnail is {tw}×{th}, expected 1280×720")
                failures.append(f"thumbnail_dimensions: {tw}×{th} does not match required 1280×720")
        else:
            _add_check(checks, "c_thumbnail_dimensions", "warn",
                       "thumbnail dimensions not available in thumbnail_package.main_thumbnail")
            warnings.append("thumbnail_dimensions: not verified (width/height absent from main_thumbnail)")
    else:
        _add_check(checks, "c_thumbnail_dimensions", "warn",
                   "no thumbnail_package provided — thumbnail dimensions not verified")
        warnings.append("thumbnail_dimensions: not verified (no thumbnail_package provided)")

    # ------------------------------------------------------------------
    # Check 13 — Description present
    # ------------------------------------------------------------------
    description = metadata_source.get("description", "")
    if not description:
        _add_check(checks, "c_description_present", "fail",
                   "metadata_source.description is empty — description required for publish")
        failures.append("description_present: description is empty — required for YouTube publish")
    else:
        _add_check(checks, "c_description_present", "pass",
                   "description present")

    # ------------------------------------------------------------------
    # Check 14 — Chapter markers (section_titles non-empty)
    # ------------------------------------------------------------------
    section_titles = metadata_source.get("section_titles", [])
    if not section_titles:
        _add_check(checks, "c_chapter_markers", "warn",
                   "metadata_source.section_titles is empty — chapter markers recommended")
        warnings.append("chapter_markers: section_titles empty — chapter markers recommended for long-form content")
    else:
        _add_check(checks, "c_chapter_markers", "pass",
                   f"chapter markers present: {len(section_titles)} section(s)")

    # ------------------------------------------------------------------
    # Determine final verdict
    # ------------------------------------------------------------------
    approved = len(failures) == 0

    if not approved:
        verdict = "REJECTED"
    elif warnings:
        verdict = "WARNING"
    else:
        verdict = "APPROVED"

    if verdict == "REJECTED":
        log.error(
            "qa_agent_rejected",
            episode_id=episode_id,
            report_id=report_id,
            failures=failures,
        )
    else:
        log.info(
            "qa_agent_verdict",
            episode_id=episode_id,
            report_id=report_id,
            verdict=verdict,
            approved=approved,
            warning_count=len(warnings),
        )

    report = {
        "report_id": report_id,
        "episode_id": episode_id,
        "run_id": run_id or "",
        "evaluated_at": evaluated_at,
        "verdict": verdict,
        "approved": approved,
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "metadata_version": METADATA_VERSION,
    }

    validate("qa_approval_report", report)
    _write_report(report, episode_id, output_dir)
    _write_qa_report(report, episode_id, output_dir)

    return report


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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


def _write_report(
    report: dict,
    episode_id: str,
    output_dir: Optional[Path],
) -> None:
    """Write qa_approval_report_{episode_id}.json to disk."""
    if output_dir is None:
        output_dir = (
            Path(__file__).resolve().parent.parent.parent
            / ".hypogeum-assets" / "episodes" / episode_id
        )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"qa_approval_report_{episode_id}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def _write_qa_report(
    report: dict,
    episode_id: str,
    output_dir: Optional[Path],
) -> None:
    """Write qa_report_{episode_id}.json to disk (enriched format with run_id + metadata_version)."""
    if output_dir is None:
        output_dir = (
            Path(__file__).resolve().parent.parent.parent
            / ".hypogeum-assets" / "episodes" / episode_id
        )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"qa_report_{episode_id}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
