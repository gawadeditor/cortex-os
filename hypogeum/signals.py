"""
hypogeum/signals.py — Signal Ingestion v1

Converts a raw analytics_snapshot into a normalized, durable signal_record
suitable for learning loops, performance tracking, and world-bible updates.

No inference. No optimization. Pure normalization: maps known snapshot fields
into a stable, versioned schema.

Architecture:
    ingest_signal(...)           — normalize + write signal_record_{episode_id}.json
    list_signal_records(...)     — list all signal_record files

Signal record schema:
    signal_id           — deterministic 14-char hex from episode_id + run_id
    episode_id
    run_id
    ingested_at         — ISO timestamp of ingestion
    metadata_version
    youtube_video_id
    published_at        — from analytics_snapshot
    window_days         — analytics window from snapshot
    data_quality        — "captured" | "error" | "no_data"
    metrics             — flat dict of raw YouTube Analytics values
    normalized_metrics  — per-day rates (views_per_day, watch_minutes_per_day)
    signal_summary      — metric_count, data_quality, has_views

Output:
    signal_record_{episode_id}.json written to .hypogeum-assets/signals/

Public API:
    ingest_signal(
        episode_id, analytics_snapshot=None,
        output_dir=None,
    ) -> dict

    list_signal_records(signals_dir=None) -> list[dict]
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.identifiers import (
    METADATA_VERSION,
    generate_signal_id,
)


# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SIGNALS_DIR = _PROJECT_ROOT / ".hypogeum-assets" / "signals"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_dir(output_dir: Optional[Path]) -> Path:
    return Path(output_dir) if output_dir is not None else _DEFAULT_SIGNALS_DIR


def _write_record(record: dict, signals_dir: Path, episode_id: str) -> None:
    out_path = signals_dir / f"signal_record_{episode_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Known YouTube Analytics metric keys (v1)
# ---------------------------------------------------------------------------

_KNOWN_METRICS = (
    "views",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "likes",
    "comments",
    "shares",
    "subscribersGained",
)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize_metrics(raw_metrics: dict, window_days: int) -> dict:
    """
    Produce per-day normalized metrics from raw YouTube Analytics values.

    Only computes rates for metrics that are present and numeric.
    Returns empty dict if window_days <= 0 or metrics are absent.
    """
    if not raw_metrics or window_days <= 0:
        return {}

    normalized: dict = {}
    views = raw_metrics.get("views")
    watch_minutes = raw_metrics.get("estimatedMinutesWatched")

    if isinstance(views, (int, float)):
        normalized["views_per_day"] = round(views / window_days, 4)
    if isinstance(watch_minutes, (int, float)):
        normalized["watch_minutes_per_day"] = round(watch_minutes / window_days, 4)

    return normalized


def _infer_data_quality(snapshot: dict) -> str:
    """
    Return "captured", "error", or "no_data" based on snapshot state.
    """
    status = snapshot.get("status", "")
    if status == "captured":
        metrics = snapshot.get("metrics") or {}
        if metrics:
            return "captured"
        return "no_data"
    if status == "error":
        return "error"
    return "no_data"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ingest_signal(
    episode_id: str,
    analytics_snapshot: Optional[dict] = None,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Ingest an analytics_snapshot and write a durable signal_record.

    Normalizes raw YouTube Analytics metrics into a stable, versioned
    schema. Writes signal_record_{episode_id}.json. Idempotent: a second
    call overwrites the previous record.

    Args:
        episode_id:          Episode identifier.
        analytics_snapshot:  analytics_snapshot dict from analytics_ingest.
                             If None, produces an empty no_data record.
        output_dir:          Override output directory.
                             Defaults to .hypogeum-assets/signals/.

    Returns:
        signal_record dict with keys:
            signal_id, episode_id, run_id, ingested_at, metadata_version,
            youtube_video_id, published_at, window_days, data_quality,
            metrics, normalized_metrics, signal_summary.
    """
    snapshot = analytics_snapshot or {}
    signals_dir = _resolve_dir(output_dir)

    run_id = str(snapshot.get("run_id") or "")
    signal_id = generate_signal_id(episode_id, run_id) if run_id else \
        generate_signal_id(episode_id, "no_run")

    youtube_video_id = str(snapshot.get("youtube_video_id") or "")
    published_at = str(snapshot.get("published_at") or "")
    window_days = int(snapshot.get("window_days") or 0)

    # Extract only known metric keys from snapshot.metrics
    raw_metrics = snapshot.get("metrics") or {}
    metrics: dict = {}
    for key in _KNOWN_METRICS:
        if key in raw_metrics:
            metrics[key] = raw_metrics[key]

    data_quality = _infer_data_quality(snapshot)
    normalized_metrics = _normalize_metrics(metrics, window_days)

    record = {
        "signal_id": signal_id,
        "episode_id": episode_id,
        "run_id": run_id,
        "ingested_at": _now_iso(),
        "metadata_version": METADATA_VERSION,
        "youtube_video_id": youtube_video_id,
        "published_at": published_at,
        "window_days": window_days,
        "data_quality": data_quality,
        "metrics": metrics,
        "normalized_metrics": normalized_metrics,
        "signal_summary": {
            "metric_count": len(metrics),
            "data_quality": data_quality,
            "has_views": isinstance(metrics.get("views"), (int, float)) and metrics.get("views", 0) > 0,
        },
    }

    _write_record(record, signals_dir, episode_id)
    return record


def list_signal_records(signals_dir: Optional[Path] = None) -> list[dict]:
    """
    Return all signal_record records from the signals directory.

    Scans for signal_record_*.json files and returns their parsed contents.
    Returns [] if the directory is absent or no records exist.

    Args:
        signals_dir: Override signals directory.
                     Defaults to .hypogeum-assets/signals/.

    Returns:
        List of signal_record dicts, sorted by filename.
    """
    resolved_dir = _resolve_dir(signals_dir)
    if not resolved_dir.exists():
        return []

    results = []
    for path in sorted(resolved_dir.glob("signal_record_*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            results.append(record)
        except Exception:
            pass
    return results
