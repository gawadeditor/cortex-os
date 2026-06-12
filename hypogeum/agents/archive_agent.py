"""
hypogeum/agents/archive_agent.py — Publishing Archive Agent (R49)

Post-publish archival recorder. Validates publishing outcomes and assembles
a durable episode_summary from the final_render_package and publishing_record.
Emits an archive_record with verdict ARCHIVED, WARNING, or FAILED.

Does NOT gate the pipeline — archive outcomes are logged and stored in
PipelineResult but do not raise exceptions. The episode is already published;
archival failure only affects future discoverability and audit trails.

No Claude calls. Pure deterministic evaluation.

Inputs consumed:
  final_render_package   — Assembly Agent output (required); provides
                           metadata_source and episode_render asset IDs
  publishing_record      — Publishing Agent output (required); provides
                           platform results, overall_status, and summary counts

Checks (5):
  c_publishing_completed    — FAIL    — overall_status must not be "failed"
                              WARN    — "partially_published" is acceptable but flagged
  c_youtube_main_published  — WARN    — summary.youtube_main_published should be True
  c_video_asset_referenced  — WARN    — episode_render.video_asset_id non-empty
  c_episode_metadata        — WARN    — metadata_source.episode_title non-empty
  c_deliverables_count      — WARN    — summary.total_published > 0

Verdict rules:
  ARCHIVED — archived=True;  no failures, no warnings
  WARNING  — archived=True;  no failures, one or more warnings
  FAILED   — archived=False; one or more blocking failures

The archive_record also includes an episode_summary dict aggregated from
the two required inputs — this serves as the durable per-episode audit record.

Public API:
    run_archive_agent(
        final_render_package, publishing_record, episode_id,
        output_dir=None,
    ) → dict  (archive_record, schema-validated, written to disk)

Logging events emitted:
  archive_agent_started   — info  — inputs received
  archive_agent_check     — debug — per-check result
  archive_agent_verdict   — info  — final verdict
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger
from hypogeum.schemas import validate


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "archive_agent"
_AGENT_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_archive_agent(
    final_render_package: dict,
    publishing_record: dict,
    episode_id: str,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Validate publishing outcomes and assemble a durable archive_record.

    Args:
        final_render_package:  Final Render Package from Assembly Agent (required).
        publishing_record:     Publishing Agent output (required).
        episode_id:            Episode identifier for file naming and log correlation.
        output_dir:            Override directory for the output file.
                               Defaults to .hypogeum-assets/episodes/{episode_id}/.

    Returns:
        archive_record dict (schema-validated, written to disk).
    """
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    pub_summary = publishing_record.get("summary", {})
    pub_meta = publishing_record.get("package_metadata", {})

    log.info(
        "archive_agent_started",
        episode_id=episode_id,
        publishing_overall_status=pub_meta.get("overall_status", "unknown"),
        youtube_main_published=pub_summary.get("youtube_main_published", False),
        total_published=pub_summary.get("total_published", 0),
    )

    archive_id = f"ar_{episode_id}_{uuid.uuid4().hex[:8]}"
    archived_at = datetime.now(timezone.utc).isoformat()

    checks: list[dict] = []
    failures: list[str] = []
    warnings: list[str] = []

    metadata_source = final_render_package.get("metadata_source", {})
    episode_render = final_render_package.get("episode_render", {})
    overall_status = pub_meta.get("overall_status", "failed")
    youtube_published = pub_summary.get("youtube_main_published", False)
    total_published = pub_summary.get("total_published", 0)
    video_asset_id = episode_render.get("video_asset_id", "") or ""
    episode_title = metadata_source.get("episode_title", "") or ""

    # ------------------------------------------------------------------
    # Check 1 — Publishing completed (BLOCKING on "failed")
    # ------------------------------------------------------------------
    if overall_status == "failed":
        _add_check(checks, "c_publishing_completed", "fail",
                   "publishing_record.package_metadata.overall_status=failed — "
                   "no deliverables were published; archive is incomplete")
        failures.append(
            "publishing_completed: overall_status=failed — nothing was published to archive"
        )
    elif overall_status == "partially_published":
        _add_check(checks, "c_publishing_completed", "warn",
                   "publishing_record.package_metadata.overall_status=partially_published — "
                   "some deliverables failed; archive reflects partial outcome")
        warnings.append(
            "publishing_completed: overall_status=partially_published — partial archive only"
        )
    else:
        _add_check(checks, "c_publishing_completed", "pass",
                   "publishing_record.package_metadata.overall_status=published")

    # ------------------------------------------------------------------
    # Check 2 — YouTube main episode published (WARN)
    # ------------------------------------------------------------------
    if not youtube_published:
        _add_check(checks, "c_youtube_main_published", "warn",
                   "publishing_record.summary.youtube_main_published=False — "
                   "main episode not on YouTube")
        warnings.append(
            "youtube_main_published: False — YouTube is the primary distribution channel"
        )
    else:
        _add_check(checks, "c_youtube_main_published", "pass",
                   "YouTube main episode published successfully")

    # ------------------------------------------------------------------
    # Check 3 — Video asset referenced (WARN)
    # ------------------------------------------------------------------
    if not video_asset_id:
        _add_check(checks, "c_video_asset_referenced", "warn",
                   "final_render_package.episode_render.video_asset_id is empty — "
                   "archive cannot reference main video asset")
        warnings.append(
            "video_asset_referenced: video_asset_id is empty — main asset not referenced"
        )
    else:
        _add_check(checks, "c_video_asset_referenced", "pass",
                   f"main video asset referenced: {video_asset_id}")

    # ------------------------------------------------------------------
    # Check 4 — Episode metadata present (WARN)
    # ------------------------------------------------------------------
    if not episode_title.strip():
        _add_check(checks, "c_episode_metadata", "warn",
                   "metadata_source.episode_title is empty — "
                   "archive record lacks episode title for discoverability")
        warnings.append(
            "episode_metadata: episode_title is empty — archive cannot be indexed by title"
        )
    else:
        _add_check(checks, "c_episode_metadata", "pass",
                   f"episode title archived: {episode_title!r}")

    # ------------------------------------------------------------------
    # Check 5 — Deliverables published count (WARN)
    # ------------------------------------------------------------------
    if total_published == 0:
        _add_check(checks, "c_deliverables_count", "warn",
                   "publishing_record.summary.total_published=0 — "
                   "no deliverables confirmed published")
        warnings.append(
            "deliverables_count: total_published=0 — archive records an empty publish run"
        )
    else:
        _add_check(checks, "c_deliverables_count", "pass",
                   f"{total_published} deliverable(s) published and archived")

    # ------------------------------------------------------------------
    # Determine verdict
    # ------------------------------------------------------------------
    archived = len(failures) == 0

    if not archived:
        verdict = "FAILED"
    elif warnings:
        verdict = "WARNING"
    else:
        verdict = "ARCHIVED"

    # Build episode_summary — the durable per-episode audit record
    episode_summary = {
        "episode_title": episode_title,
        "video_asset_id": video_asset_id,
        "youtube_main_published": youtube_published,
        "total_published": total_published,
        "total_failed": pub_summary.get("total_failed", 0),
        "total_skipped": pub_summary.get("total_skipped", 0),
        "publishing_overall_status": overall_status,
    }

    log.info(
        "archive_agent_verdict",
        episode_id=episode_id,
        archive_id=archive_id,
        verdict=verdict,
        archived=archived,
        warning_count=len(warnings),
        failure_count=len(failures),
    )

    record = {
        "archive_id": archive_id,
        "episode_id": episode_id,
        "archived_at": archived_at,
        "archive_status": verdict.lower() if verdict != "ARCHIVED" else "archived",
        "verdict": verdict,
        "archived": archived,
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "episode_summary": episode_summary,
    }

    validate("archive_record", record)
    _write_record(record, episode_id, output_dir)

    return record


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


def _write_record(
    record: dict,
    episode_id: str,
    output_dir: Optional[Path],
) -> None:
    """Write archive_record_{episode_id}.json to disk."""
    if output_dir is None:
        output_dir = (
            Path(__file__).resolve().parent.parent.parent
            / ".hypogeum-assets" / "episodes" / episode_id
        )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"archive_record_{episode_id}.json"
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
