"""
hypogeum/analytics_ingest.py — Analytics Ingestion v1

Reads analytics_pending records, fetches YouTube Analytics data via the
YouTube Analytics API, writes a durable analytics_snapshot, and marks
the pending record as captured.

No scheduling. No reasoning. No optimization. Pure fetch-and-store.

Architecture:
    list_pending()                     — scan for analytics_pending records
    fetch_and_store_analytics(...)     — fetch from YouTube API, write snapshot,
                                         mark pending as captured

The YouTube Analytics client is injected by the caller (or constructed from
the Config object). This allows tests to pass a mock client without patching.

Snapshot format:
    analytics_snapshot_{episode_id}.json
    Written to .hypogeum-assets/analytics/ (same dir as pending records).

Pending status transitions:
    "pending"  → "captured"  (after successful fetch + snapshot write)
    "pending"  → "error"     (on API failure; pending file updated in-place)

Public API:
    list_pending(analytics_dir=None)              -> list[dict]
    fetch_and_store_analytics(
        episode_id, youtube_video_id,
        yt_client=None, config=None,
        window_days=7, analytics_dir=None,
    ) -> dict
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.identifiers import METADATA_VERSION
from hypogeum.production_metadata import list_analytics_pending


# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ANALYTICS_DIR = _PROJECT_ROOT / ".hypogeum-assets" / "analytics"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_analytics_dir(analytics_dir: Optional[Path]) -> Path:
    return Path(analytics_dir) if analytics_dir is not None else _DEFAULT_ANALYTICS_DIR


def _read_pending_file(episode_id: str, analytics_dir: Path) -> Optional[dict]:
    """Read the analytics_pending_{episode_id}.json file, or None if absent."""
    path = analytics_dir / f"analytics_pending_{episode_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_file(path: Path, record: dict) -> None:
    """Write a dict as pretty-printed JSON to path, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")


def _update_pending_status(episode_id: str, status: str, analytics_dir: Path, **extra) -> None:
    """Update the status field in the analytics_pending file (in-place)."""
    path = analytics_dir / f"analytics_pending_{episode_id}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        record["status"] = status
        record.update(extra)
        _write_file(path, record)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# list_pending
# ---------------------------------------------------------------------------

def list_pending(analytics_dir: Optional[Path] = None) -> list[dict]:
    """
    Return all analytics_pending records with status='pending'.

    Delegates to production_metadata.list_analytics_pending() for scanning
    and filtering. Returns [] if the directory is absent or empty.

    Args:
        analytics_dir: Override analytics directory.
                       Defaults to .hypogeum-assets/analytics/.

    Returns:
        List of analytics_pending dicts. Empty list if none found.
    """
    return list_analytics_pending(output_dir=analytics_dir)


# ---------------------------------------------------------------------------
# fetch_and_store_analytics
# ---------------------------------------------------------------------------

def fetch_and_store_analytics(
    episode_id: str,
    youtube_video_id: str,
    yt_client=None,
    config=None,
    window_days: int = 7,
    analytics_dir: Optional[Path] = None,
) -> dict:
    """
    Fetch YouTube Analytics for one episode and write a durable snapshot.

    Reads the analytics_pending record to get context (published_at, run_id).
    Calls the YouTube Analytics API for the specified window. Writes
    analytics_snapshot_{episode_id}.json. Marks the pending record as
    "captured" on success or "error" on failure.

    Idempotent: if a snapshot already exists for this episode, it is
    overwritten with fresh data. The pending record status is updated each time.

    Args:
        episode_id:         Episode identifier.
        youtube_video_id:   YouTube video ID to fetch analytics for.
        yt_client:          YouTube Analytics API client. Must support:
                            yt_client.reports().query(ids, metrics, startDate,
                            endDate, dimensions, filters).execute()
                            If None, the function returns an error snapshot
                            (no client configured).
        config:             Config object (unused in v1 — reserved for API key
                            resolution in a future milestone).
        window_days:        Number of days to include in the analytics window.
                            Measured back from today. Default 7.
        analytics_dir:      Override analytics directory.
                            Defaults to .hypogeum-assets/analytics/.

    Returns:
        analytics_snapshot dict with fetched metrics or error details.
    """
    resolved_dir = _resolve_analytics_dir(analytics_dir)

    # Read pending record for context (run_id, published_at)
    pending = _read_pending_file(episode_id, resolved_dir)
    run_id = (pending or {}).get("run_id", "")
    published_at = (pending or {}).get("published_at", "")

    # Build snapshot scaffold
    snapshot = {
        "episode_id": episode_id,
        "youtube_video_id": youtube_video_id,
        "run_id": run_id,
        "published_at": published_at,
        "window_days": window_days,
        "fetched_at": _now_iso(),
        "status": "pending",
        "metrics": {},
        "metadata_version": METADATA_VERSION,
    }

    if yt_client is None:
        snapshot["status"] = "error"
        snapshot["error"] = "no_client_configured"
        try:
            _write_file(resolved_dir / f"analytics_snapshot_{episode_id}.json", snapshot)
        except Exception:
            pass
        _update_pending_status(episode_id, "error",
                                resolved_dir, error="no_client_configured")
        return snapshot

    # Compute date range
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    start_date = (today - timedelta(days=window_days)).isoformat()
    end_date = today.isoformat()

    try:
        response = yt_client.reports().query(
            ids=f"channel==MINE",
            metrics="views,estimatedMinutesWatched,averageViewDuration,likes,comments,shares,subscribersGained",
            startDate=start_date,
            endDate=end_date,
            dimensions="video",
            filters=f"video=={youtube_video_id}",
        ).execute()

        # Parse columnHeaders + rows into a flat metrics dict
        headers = [h["name"] for h in response.get("columnHeaders", [])]
        rows = response.get("rows", [])
        metrics: dict = {}
        if rows:
            for header, value in zip(headers, rows[0]):
                if header != "video":
                    metrics[header] = value

        snapshot["metrics"] = metrics
        snapshot["status"] = "captured"
        snapshot["date_range"] = {"start": start_date, "end": end_date}

        _write_file(resolved_dir / f"analytics_snapshot_{episode_id}.json", snapshot)
        _update_pending_status(episode_id, "captured", resolved_dir,
                                captured_at=snapshot["fetched_at"])

    except Exception as exc:
        snapshot["status"] = "error"
        snapshot["error"] = str(exc)[:500]
        try:
            _write_file(resolved_dir / f"analytics_snapshot_{episode_id}.json", snapshot)
        except Exception:
            pass
        _update_pending_status(episode_id, "error", resolved_dir, error=str(exc)[:500])

    return snapshot
