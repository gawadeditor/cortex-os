"""
hypogeum/production_metadata.py — Production Metadata Capture v1

Writes durable metadata records for each completed pipeline run.

Two records are produced:
  production_metadata_{episode_id}.json
      Written at pipeline completion (after all agents succeed).
      Contains the full production snapshot: run_id, title, thumbnail,
      narrative structure, research provenance, model assignments.

  analytics_pending_{episode_id}.json
      Written after successful publishing only (youtube_main_published=True).
      Signals the Analytics Ingestion layer that this episode has been
      published and analytics can be collected.
      Does NOT contain analytics — only the metadata needed to kick off
      ingestion (episode_id, youtube_video_id, published_at, window_days).

Architecture:
    pipeline.py calls write_production_metadata() before PipelineResult is returned.
    pipeline.py calls write_analytics_pending() only when publishing_record confirms
    youtube_main_published=True.

Public API:
    write_production_metadata(
        run_id, episode_id, title_id, selected_title, selected_thumbnail,
        narrative_structure, research_source_count, model_assignments,
        db_path=None, output_dir=None,
    ) -> dict

    write_analytics_pending(
        episode_id, youtube_video_id, published_at,
        run_id=None, window_days=7, output_dir=None,
    ) -> dict

    list_analytics_pending(output_dir=None) -> list[dict]

All writes are non-blocking — errors produce an "error" key in the returned dict
and never raise.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.identifiers import METADATA_VERSION


# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_METADATA_DIR = _PROJECT_ROOT / ".hypogeum-assets" / "metadata"
_DEFAULT_ANALYTICS_DIR = _PROJECT_ROOT / ".hypogeum-assets" / "analytics"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_output_dir(output_dir: Optional[Path], default: Path) -> Path:
    return Path(output_dir) if output_dir is not None else default


# ---------------------------------------------------------------------------
# Production Metadata
# ---------------------------------------------------------------------------

def write_production_metadata(
    run_id: str,
    episode_id: str,
    title_id: str,
    selected_title: str,
    selected_thumbnail: dict,
    narrative_structure: dict,
    research_source_count: int,
    model_assignments: dict,
    db_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Write production_metadata_{episode_id}.json at pipeline completion.

    This record captures the production decisions made for one episode run.
    It is the durable link between run_id, the creative output (title,
    thumbnail), and the research/narrative inputs that produced it.

    Args:
        run_id:                UUID4 string from register_run().
        episode_id:            Episode identifier (e.g. "ep_000").
        title_id:              Deterministic ID from generate_title_id().
        selected_title:        The episode title string chosen for publication.
        selected_thumbnail:    Dict with at minimum thumbnail_candidate_id,
                               text_overlay, visual_prompt. Sourced from
                               thumbnail_package.main_thumbnail.
        narrative_structure:   Dict describing the narrative arc. Sourced from
                               narrative_package (structure fields only).
        research_source_count: Number of sources in research_package.source_registry.
        model_assignments:     Dict of agent→model slug mappings from run record.
        db_path:               Override SQLite path (unused in v1 — for future FK writes).
        output_dir:            Override output directory.
                               Defaults to .hypogeum-assets/metadata/.

    Returns:
        production_metadata dict. Includes "error" key on write failure.
    """
    resolved_dir = _resolve_output_dir(output_dir, _DEFAULT_METADATA_DIR)

    record = {
        "run_id": run_id,
        "episode_id": episode_id,
        "title_id": title_id,
        "selected_title": selected_title,
        "selected_thumbnail": {
            "thumbnail_candidate_id": selected_thumbnail.get("thumbnail_candidate_id", ""),
            "text_overlay": selected_thumbnail.get("text_overlay", ""),
            "visual_prompt": selected_thumbnail.get("visual_prompt", ""),
            "prompt_hash": selected_thumbnail.get("prompt_hash", ""),
        },
        "narrative_structure": narrative_structure,
        "research_source_count": research_source_count,
        "model_assignments": model_assignments,
        "recorded_at": _now_iso(),
        "metadata_version": METADATA_VERSION,
    }

    try:
        resolved_dir.mkdir(parents=True, exist_ok=True)
        out_path = resolved_dir / f"production_metadata_{episode_id}.json"
        out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        record["error"] = f"write_failed: {exc}"

    return record


# ---------------------------------------------------------------------------
# Analytics Pending
# ---------------------------------------------------------------------------

def write_analytics_pending(
    episode_id: str,
    youtube_video_id: str,
    published_at: str,
    run_id: Optional[str] = None,
    window_days: int = 7,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Write analytics_pending_{episode_id}.json after successful YouTube publishing.

    This record signals the Analytics Ingestion layer that an episode has been
    published and that analytics collection should begin. It contains only the
    identifiers and parameters needed to drive ingestion — not analytics data.

    Args:
        episode_id:       Episode identifier.
        youtube_video_id: The YouTube video ID returned by the Publishing Agent.
        published_at:     ISO 8601 UTC timestamp of the YouTube publication.
        run_id:           The run_id that produced this episode (for traceability).
        window_days:      Analytics collection window in days. Default 7.
        output_dir:       Override output directory.
                          Defaults to .hypogeum-assets/analytics/.

    Returns:
        analytics_pending dict. Includes "error" key on write failure.
    """
    resolved_dir = _resolve_output_dir(output_dir, _DEFAULT_ANALYTICS_DIR)

    record = {
        "episode_id": episode_id,
        "youtube_video_id": youtube_video_id,
        "published_at": published_at,
        "run_id": run_id or "",
        "window_days": window_days,
        "status": "pending",
        "created_at": _now_iso(),
        "metadata_version": METADATA_VERSION,
    }

    try:
        resolved_dir.mkdir(parents=True, exist_ok=True)
        out_path = resolved_dir / f"analytics_pending_{episode_id}.json"
        out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        record["error"] = f"write_failed: {exc}"

    return record


# ---------------------------------------------------------------------------
# Analytics Pending — list
# ---------------------------------------------------------------------------

def list_analytics_pending(output_dir: Optional[Path] = None) -> list[dict]:
    """
    Return all analytics_pending records with status='pending'.

    Scans the analytics directory for analytics_pending_*.json files and
    returns those whose status field is "pending". Returns [] if the
    directory is absent or no pending records exist.

    Args:
        output_dir: Override analytics directory.
                    Defaults to .hypogeum-assets/analytics/.

    Returns:
        List of analytics_pending dicts. Empty list if none found.
    """
    resolved_dir = _resolve_output_dir(output_dir, _DEFAULT_ANALYTICS_DIR)
    if not resolved_dir.exists():
        return []

    results = []
    for path in sorted(resolved_dir.glob("analytics_pending_*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("status") == "pending":
                results.append(record)
        except Exception:
            pass

    return results
