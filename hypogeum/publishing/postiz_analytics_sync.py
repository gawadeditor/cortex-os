"""
hypogeum/publishing/postiz_analytics_sync.py — per-post analytics for posts
Postiz published, into platform_analytics (source='api'), the same table and
aggregation zernio_analytics_sync feeds.

Zernio's /analytics lists posts by Zernio's own id, so a post published
through Postiz never matches its ledger. Here the match is exact: the Postiz
post id is the receipt's provider_publication_id, and the receipt's attempt
names the episode, channel and platform.

Postiz returns a series per metric: [{label, data: [{total, date}, ...]}].
A post's number for a metric is the sum of its daily totals. Labels this
module does not know are kept in raw_json and never guessed into a column.

Rate limit: Postiz allows 30 API requests/hour by default and this costs one
request per post, so a run reads at most `max_posts` posts (newest first) and
stops early on a 429.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger
from hypogeum.publishing.zernio_analytics_sync import _aggregate_episode_platform

log = get_logger(agent="postiz_analytics_sync")

# Postiz series label (lowercased) -> platform_analytics column.
_LABEL_TO_COLUMN = {
    "views": "views", "view": "views", "plays": "views", "video views": "views",
    "impressions": "impressions",
    "likes": "likes", "like": "likes",
    "comments": "comments", "comment": "comments",
    "shares": "shares", "share": "shares",
    "saves": "saves", "saved": "saves",
    "reach": "reach",
}


def _series_total(series: dict) -> Optional[float]:
    points = series.get("data")
    if not isinstance(points, list):
        return None
    total, seen = 0.0, False
    for pt in points:
        try:
            total += float(pt.get("total"))
            seen = True
        except (TypeError, ValueError, AttributeError):
            continue
    return total if seen else None


def translate_postiz_series(series_list, *, platform: str) -> dict:
    """Postiz per-post series -> the metrics dict store_platform_metrics takes."""
    out: dict = {}
    raw: dict = {}
    for series in series_list if isinstance(series_list, list) else []:
        if not isinstance(series, dict):
            continue
        label = str(series.get("label") or "").strip()
        total = _series_total(series)
        if not label or total is None:
            continue
        raw[label] = total
        column = _LABEL_TO_COLUMN.get(label.lower())
        if label.lower() == "estimated minutes watched":
            out["total_watch_time_s"] = round(total * 60.0, 2)
        elif column:
            if column == "reach" and platform not in ("facebook", "instagram"):
                continue
            out[column] = int(total)
    out["raw_json"] = raw
    return out


def _postiz_receipts(db_path, since_iso: str) -> list:
    from hypogeum.db import get_connection
    conn = get_connection(Path(db_path) if db_path else None)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT r.provider_publication_id AS post_id, a.episode_id, a.channel_id, a.platform "
            "FROM publishing_receipts r JOIN publishing_attempts a ON a.attempt_id = r.attempt_id "
            "WHERE a.provider_id = 'postiz' AND a.state = 'succeeded' "
            "AND r.provider_publication_id IS NOT NULL AND r.acknowledged_at >= ? "
            "ORDER BY r.acknowledged_at DESC", (since_iso,),
        )]
    finally:
        conn.close()


def sync_postiz_analytics(*, provider=None, db_path: Optional[Path] = None, window_days: int = 7,
                          lookback_days: int = 30, max_posts: int = 20) -> dict:
    """Never raises. {ok, posts_read, rows_written, skipped, error?}"""
    out = {"ok": False, "posts_read": 0, "rows_written": 0, "skipped": 0}
    try:
        from hypogeum.analytics_store import store_platform_metrics, VALID_PLATFORMS
        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        receipts = _postiz_receipts(db_path, since)
        if not receipts:
            out["ok"] = True
            return out
        if provider is None:
            from hypogeum.publishing.postiz import PostizProvider
            provider = PostizProvider()

        grouped: dict = {}
        for rc in receipts[:max_posts]:
            if rc["platform"] not in VALID_PLATFORMS:
                continue
            try:
                series = provider.get_post_analytics(rc["post_id"], days=lookback_days, db_path=db_path)
            except Exception as exc:  # noqa: BLE001 — a rate limit or outage stops the run, keeps what we have
                out["error"] = f"{type(exc).__name__}: {exc}"
                log.warn("postiz_analytics_fetch_failed", post_id=rc["post_id"], error=str(exc)[:200])
                if "429" in str(exc):
                    break
                out["skipped"] += 1
                continue
            out["posts_read"] += 1
            metrics = translate_postiz_series(series, platform=rc["platform"])
            if not any(metrics.get(k) for k in ("views", "impressions", "likes", "comments", "shares",
                                                 "saves", "reach", "total_watch_time_s")):
                out["skipped"] += 1
                continue
            metrics["platform_video_id"] = rc["post_id"]
            grouped.setdefault(((rc["episode_id"], rc["channel_id"]), rc["platform"]), []).append(metrics)

        for ((episode_id, _channel_id), platform), posts in grouped.items():
            store_platform_metrics(episode_id, platform, _aggregate_episode_platform(posts),
                                   source="api", window_days=window_days, db_path=db_path)
            out["rows_written"] += 1
        out["ok"] = "error" not in out or out["rows_written"] > 0
        log.info("postiz_analytics_synced", posts_read=out["posts_read"], rows_written=out["rows_written"])
        return out
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
