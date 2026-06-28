"""
hypogeum/meta_analytics.py — Meta Graph API analytics sync.

Pulls per-video analytics for Facebook Pages and Instagram Business accounts
using stored OAuth2 tokens from secret_profiles. Only syncs videos that have
a publishing_outcomes record so episode_id→video mapping is exact.

Public API:
    sync_meta_analytics(platform, cfg, db_path, window_days) -> dict
    get_meta_token(platform, db_path) -> str | None
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

_GRAPH_BASE = "https://graph.facebook.com"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve(db_path) -> Path:
    return Path(db_path) if db_path else _DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def get_meta_token(platform: str, db_path=None) -> Optional[str]:
    """
    Return the stored OAuth2 access token for 'facebook' or 'instagram'.
    Returns None if no active credential exists.
    """
    db = _resolve(db_path)
    if not db.exists():
        return None
    try:
        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            row = conn.execute(
                """SELECT credential_data FROM secret_profiles
                   WHERE provider = ? AND status = 'active'
                   ORDER BY created_at DESC LIMIT 1""",
                (platform,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        data = row["credential_data"] or ""
        # credential_data may be a raw token string or JSON
        data = data.strip()
        if data.startswith("{"):
            try:
                parsed = _json.loads(data)
                return parsed.get("access_token") or parsed.get("token")
            except Exception:
                return None
        return data or None
    except Exception:
        return None


def get_meta_token_expiry(platform: str, db_path=None) -> Optional[str]:
    """Return expires_at for the active platform credential, or None."""
    db = _resolve(db_path)
    if not db.exists():
        return None
    try:
        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            row = conn.execute(
                """SELECT expires_at FROM secret_profiles
                   WHERE provider = ? AND status = 'active'
                   ORDER BY created_at DESC LIMIT 1""",
                (platform,),
            ).fetchone()
        finally:
            conn.close()
        return row["expires_at"] if row else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Episode ↔ platform video mapping
# ---------------------------------------------------------------------------

def _get_published_video_ids(platform: str, db_path) -> list[dict]:
    """
    Return [{episode_id, platform_video_id}] for successfully published videos
    on this platform. Used to scope analytics fetches to known episodes.
    """
    db = _resolve(db_path)
    if not db.exists():
        return []
    try:
        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            rows = conn.execute(
                """SELECT episode_id, platform_video_id
                   FROM publishing_outcomes
                   WHERE platform = ? AND publishing_status = 'success'
                     AND platform_video_id IS NOT NULL
                   ORDER BY published_at DESC""",
                (platform,),
            ).fetchall()
        finally:
            conn.close()
        return [{"episode_id": r["episode_id"], "platform_video_id": r["platform_video_id"]}
                for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Facebook Page video insights
# ---------------------------------------------------------------------------

_FB_VIDEO_METRICS = (
    "total_video_views,"
    "total_video_impressions,"
    "total_video_avg_time_watched,"
    "total_video_complete_views,"
    "total_video_reactions_by_type_total,"
    "total_video_shares,"
    "total_video_comments"
)

_FB_METRIC_MAP = {
    "total_video_views":                    "views",
    "total_video_impressions":              "impressions",
    "total_video_avg_time_watched":         "avg_watch_time_s",
    "total_video_complete_views":           None,   # used to compute avg_view_pct
    "total_video_reactions_by_type_total":  "likes",
    "total_video_shares":                   "shares",
    "total_video_comments":                 "comments",
}


def _parse_fb_insights(insight_data: list) -> dict:
    """Flatten Meta Graph API video_insights response → normalised metrics dict."""
    raw: dict = {}
    for item in insight_data:
        name   = item.get("name", "")
        values = item.get("values", [])
        val    = values[0].get("value", 0) if values else 0
        if isinstance(val, dict):
            val = sum(val.values()) if val else 0
        raw[name] = val

    views    = raw.get("total_video_views") or 0
    complete = raw.get("total_video_complete_views") or 0
    awt_ms   = raw.get("total_video_avg_time_watched") or 0  # in milliseconds

    metrics: dict = {"views": int(views)}
    if raw.get("total_video_impressions"):
        metrics["impressions"] = int(raw["total_video_impressions"])
    if awt_ms:
        metrics["avg_watch_time_s"] = round(awt_ms / 1000.0, 1)
    if views and complete:
        metrics["avg_view_pct"] = round(complete / views, 4)
    if raw.get("total_video_reactions_by_type_total"):
        metrics["likes"] = int(raw["total_video_reactions_by_type_total"])
    if raw.get("total_video_shares"):
        metrics["shares"] = int(raw["total_video_shares"])
    if raw.get("total_video_comments"):
        metrics["comments"] = int(raw["total_video_comments"])
    return metrics


def _fetch_fb_video_insights(video_id: str, token: str, version: str) -> Optional[dict]:
    """Fetch insights for one Facebook video. Returns normalised metrics or None."""
    import urllib.request
    import urllib.parse
    params = urllib.parse.urlencode({
        "metric":       _FB_VIDEO_METRICS,
        "access_token": token,
    })
    url = f"{_GRAPH_BASE}/{version}/{video_id}/video_insights?{params}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            body = _json.loads(resp.read().decode("utf-8"))
        data = body.get("data") or []
        if not data:
            return None
        return _parse_fb_insights(data)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Instagram video insights
# ---------------------------------------------------------------------------

_IG_METRICS = "plays,reach,saved,comments,likes,shares,ig_reels_avg_watch_time"


def _fetch_ig_media_insights(media_id: str, token: str, version: str) -> Optional[dict]:
    """Fetch insights for one Instagram media item. Returns normalised metrics or None."""
    import urllib.request
    import urllib.parse
    params = urllib.parse.urlencode({
        "metric":       _IG_METRICS,
        "access_token": token,
    })
    url = f"{_GRAPH_BASE}/{version}/{media_id}/insights?{params}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            body = _json.loads(resp.read().decode("utf-8"))
        data = body.get("data") or []
        if not data:
            return None
        metrics: dict = {}
        for item in data:
            name = item.get("name", "")
            val  = item.get("values", [{}])[0].get("value") if item.get("values") else item.get("value")
            if val is None:
                continue
            if name == "plays":
                metrics["views"] = int(val)
            elif name == "reach":
                metrics["impressions"] = int(val)
            elif name == "saved":
                metrics["saves"] = int(val)
            elif name == "comments":
                metrics["comments"] = int(val)
            elif name == "likes":
                metrics["likes"] = int(val)
            elif name == "shares":
                metrics["shares"] = int(val)
            elif name == "ig_reels_avg_watch_time":
                # milliseconds → seconds
                metrics["avg_watch_time_s"] = round(val / 1000.0, 1)
        return metrics if metrics else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main sync entry point
# ---------------------------------------------------------------------------

def sync_meta_analytics(
    platform: str,
    cfg,
    db_path=None,
    window_days: int = 90,
) -> dict:
    """
    Sync Meta Graph API analytics for 'facebook' or 'instagram'.

    Algorithm:
      1. Load the stored access token from secret_profiles.
      2. Find published videos for this platform in publishing_outcomes.
      3. For each video, fetch insights from the Graph API.
      4. Store results in platform_analytics with source='api'.

    Returns {ok, synced_count, skipped_count, errors, note}.
    Non-blocking — always returns a dict.
    """
    if platform not in ("facebook", "instagram"):
        return {"ok": False, "synced_count": 0, "errors": [f"unsupported platform {platform!r}"]}

    token = get_meta_token(platform, db_path=db_path)
    if not token:
        return {
            "ok": False, "synced_count": 0, "skipped_count": 0,
            "errors": [f"No active {platform} credential found in secret_profiles"],
        }

    version = (getattr(cfg, "META_GRAPH_API_VERSION", None) or "v19.0")
    videos  = _get_published_video_ids(platform, db_path)

    if not videos:
        return {
            "ok": True, "synced_count": 0, "skipped_count": 0,
            "errors": [],
            "note": f"No published {platform} videos found in publishing_outcomes — nothing to sync",
        }

    from hypogeum.analytics_store import store_platform_metrics
    from datetime import datetime, timezone

    synced = skipped = 0
    errors: list[str] = []
    snap_date = datetime.now(timezone.utc).date().isoformat()

    for v in videos:
        ep_id    = v["episode_id"]
        vid_id   = v["platform_video_id"]
        metrics  = None

        if platform == "facebook":
            metrics = _fetch_fb_video_insights(vid_id, token, version)
        elif platform == "instagram":
            metrics = _fetch_ig_media_insights(vid_id, token, version)

        if not metrics:
            skipped += 1
            errors.append(f"No insights returned for {platform} video {vid_id}")
            continue

        try:
            metrics["platform_video_id"] = vid_id
            store_platform_metrics(
                episode_id=ep_id,
                platform=platform,
                metrics=metrics,
                source="api",
                snapshot_date=snap_date,
                window_days=window_days,
                db_path=db_path,
            )
            synced += 1
        except Exception as exc:
            skipped += 1
            errors.append(f"Store failed for {vid_id}: {exc}")

    return {
        "ok":           True,
        "synced_count": synced,
        "skipped_count": skipped,
        "errors":       errors,
    }
