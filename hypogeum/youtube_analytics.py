"""
hypogeum/youtube_analytics.py — YouTube Analytics API v2 sync

Fetches real performance metrics for published YouTube videos and stores them
in platform_analytics with source='api'.  No google-api-python-client dependency —
all calls use requests, identical to publishing/youtube.py.

YouTube Analytics API v2 docs:
  GET https://youtubeanalytics.googleapis.com/v2/reports
  Auth: Authorization: Bearer {access_token}

Metrics fetched per video:
  views, estimatedMinutesWatched → total_watch_time_s
  averageViewDuration            → avg_watch_time_s
  averageViewPercentage          → avg_view_pct  (API returns 0–100; stored as 0–1)
  likes, comments, shares, subscribersGained

Note: impressions and impressionsCtr are NOT available at video-level dimensions
in the Analytics API (only channel-level without video filter).  They must be
imported manually from YouTube Studio if needed.

Public API:
    fetch_video_analytics(
        access_token, video_id, start_date, end_date
    ) -> dict

    sync_youtube_analytics(
        config, db_path=None, window_days=28
    ) -> dict   # {ok, synced_count, skipped_count, errors, synced_at}

    get_last_youtube_sync(db_path=None) -> Optional[str]  # ISO timestamp or None
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Module-level import so tests can patch hypogeum.youtube_analytics.refresh_youtube_token
from hypogeum.publishing.youtube import refresh_youtube_token  # noqa: F401

_YT_ANALYTICS_BASE = "https://youtubeanalytics.googleapis.com/v2/reports"

_ANALYTICS_METRICS = (
    "views,"
    "estimatedMinutesWatched,"
    "averageViewDuration,"
    "averageViewPercentage,"
    "likes,"
    "comments,"
    "shares,"
    "subscribersGained"
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_video_id(raw_id: str) -> str:
    """Strip YouTube timestamp suffixes like &t=400s from stored video IDs."""
    if not raw_id:
        return raw_id
    return re.split(r"[&?#]", raw_id.strip())[0]


def fetch_video_analytics(
    access_token: str,
    video_id: str,
    start_date: str,
    end_date: str,
) -> dict:
    """
    Fetch YouTube Analytics for one video.

    Args:
        access_token: Valid OAuth2 Bearer token with yt-analytics.readonly scope.
        video_id:     YouTube video ID (clean, no timestamp suffix).
        start_date:   ISO date string YYYY-MM-DD.
        end_date:     ISO date string YYYY-MM-DD.

    Returns:
        Normalized metrics dict ready for store_platform_metrics().
        Empty dict if no rows returned (video too new / no data yet).

    Raises:
        ValueError  — API returned an error body (4xx/5xx).
        RuntimeError — network failure.
    """
    import requests as _req

    params = {
        "ids":        "channel==MINE",
        "startDate":  start_date,
        "endDate":    end_date,
        "metrics":    _ANALYTICS_METRICS,
        "dimensions": "video",
        "filters":    f"video=={video_id}",
    }
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        resp = _req.get(_YT_ANALYTICS_BASE, headers=headers, params=params, timeout=30)
    except _req.RequestException as exc:
        raise RuntimeError(f"YouTube Analytics network error: {exc}") from exc

    if not resp.ok:
        body = ""
        try:
            body = resp.json().get("error", {}).get("message", resp.text[:200])
        except Exception:
            body = resp.text[:200]
        raise ValueError(f"YouTube Analytics API HTTP {resp.status_code}: {body}")

    data = resp.json()
    col_headers = [h["name"] for h in data.get("columnHeaders", [])]
    rows = data.get("rows", [])

    if not rows:
        return {}

    # First row matches our video (dimensions=video filter)
    row = rows[0]
    raw: dict = dict(zip(col_headers, row))

    # Normalise to platform_analytics schema
    metrics: dict = {}
    if "views" in raw and raw["views"] is not None:
        metrics["views"] = int(raw["views"])
    if "estimatedMinutesWatched" in raw and raw["estimatedMinutesWatched"] is not None:
        metrics["total_watch_time_s"] = round(float(raw["estimatedMinutesWatched"]) * 60, 1)
    if "averageViewDuration" in raw and raw["averageViewDuration"] is not None:
        metrics["avg_watch_time_s"] = round(float(raw["averageViewDuration"]), 1)
    if "averageViewPercentage" in raw and raw["averageViewPercentage"] is not None:
        # API returns 0–100; schema stores 0–1
        metrics["avg_view_pct"] = round(float(raw["averageViewPercentage"]) / 100.0, 6)
    for field in ("likes", "comments", "shares"):
        if field in raw and raw[field] is not None:
            metrics[field] = int(raw[field])
    metrics["platform_video_id"] = video_id

    return metrics


def sync_youtube_analytics(
    config,
    db_path: Optional[Path] = None,
    window_days: int = 28,
) -> dict:
    """
    Sync analytics for all successfully published YouTube videos.

    Workflow:
      1. Refresh YouTube OAuth token using config credentials.
      2. Query publishing_outcomes for YouTube videos with status='success'
         that have a platform_video_id.
      3. Call fetch_video_analytics() for each.
      4. Store via store_platform_metrics(source='api').
      5. Return summary.

    Args:
        config:      Config instance with YOUTUBE_* credentials.
        db_path:     Override DB path.
        window_days: Analytics window in days back from today.

    Returns:
        {
          "ok": bool,
          "synced_count": int,
          "skipped_count": int,
          "errors": list[str],
          "synced_at": str,          # ISO timestamp
          "episodes": list[str],     # episode_ids successfully synced
        }
    """
    from hypogeum.analytics_store import store_platform_metrics
    from hypogeum.db import get_connection, initialize_schema
    from hypogeum.publishing.retry import HardFailureError, SoftFailureError

    resolved_db = Path(db_path) if db_path else _DEFAULT_DB
    initialize_schema(resolved_db)
    synced_at = _now_iso()
    errors: list[str] = []
    synced_episodes: list[str] = []
    skipped_count = 0

    # 1. Refresh token
    try:
        access_token = refresh_youtube_token(config)
    except (HardFailureError, SoftFailureError) as exc:
        return {
            "ok": False,
            "synced_count": 0,
            "skipped_count": 0,
            "errors": [f"Token refresh failed: {exc}"],
            "synced_at": synced_at,
            "episodes": [],
        }
    except Exception as exc:
        return {
            "ok": False,
            "synced_count": 0,
            "skipped_count": 0,
            "errors": [f"Token refresh error: {exc}"],
            "synced_at": synced_at,
            "episodes": [],
        }

    # 2. Discover published videos
    try:
        conn = get_connection(resolved_db)
        try:
            rows = conn.execute(
                """SELECT episode_id, platform_video_id
                   FROM publishing_outcomes
                   WHERE platform='youtube'
                     AND publishing_status='success'
                     AND platform_video_id IS NOT NULL
                   ORDER BY recorded_at DESC""",
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return {
            "ok": False,
            "synced_count": 0,
            "skipped_count": 0,
            "errors": [f"DB read failed: {exc}"],
            "synced_at": synced_at,
            "episodes": [],
        }

    # 3. Date range
    today = datetime.now(timezone.utc).date()
    start_date = (today - timedelta(days=window_days)).isoformat()
    end_date = today.isoformat()

    # 4. Fetch and store per video
    for row in rows:
        episode_id = row["episode_id"]
        raw_video_id = row["platform_video_id"] or ""
        video_id = _clean_video_id(raw_video_id)

        if not video_id:
            skipped_count += 1
            continue

        try:
            metrics = fetch_video_analytics(
                access_token=access_token,
                video_id=video_id,
                start_date=start_date,
                end_date=end_date,
            )
        except (ValueError, RuntimeError) as exc:
            errors.append(f"{episode_id} ({video_id}): {exc}")
            continue

        if not metrics:
            # No data yet (video too new or no views in window)
            skipped_count += 1
            continue

        try:
            store_platform_metrics(
                episode_id=episode_id,
                platform="youtube",
                metrics=metrics,
                source="api",
                snapshot_date=end_date,
                window_days=window_days,
                db_path=resolved_db,
            )
            synced_episodes.append(episode_id)
        except Exception as exc:
            errors.append(f"{episode_id}: store failed: {exc}")

    return {
        "ok": True,
        "synced_count": len(synced_episodes),
        "skipped_count": skipped_count,
        "errors": errors,
        "synced_at": synced_at,
        "episodes": synced_episodes,
    }


def get_last_youtube_sync(db_path: Optional[Path] = None) -> Optional[str]:
    """
    Return the ISO timestamp of the most recent successful YouTube API sync,
    or None if no API-sourced analytics exist.
    """
    from hypogeum.db import get_connection

    resolved_db = Path(db_path) if db_path else _DEFAULT_DB
    if not resolved_db.exists():
        return None
    try:
        conn = get_connection(resolved_db)
        try:
            row = conn.execute(
                """SELECT imported_at FROM platform_analytics
                   WHERE platform='youtube' AND source='api'
                   ORDER BY imported_at DESC LIMIT 1"""
            ).fetchone()
        finally:
            conn.close()
        return row["imported_at"] if row else None
    except Exception:
        return None
