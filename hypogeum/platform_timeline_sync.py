"""
hypogeum/platform_timeline_sync.py — how every post grew, day by day, on every platform, plus the watch curve a platform reports.

Zernio's /analytics/post-timeline returns, for one cross-platform post, one row per platform per day (impressions, reach,
views, likes, comments, shares, saves, replays, completion, follows) and — for Facebook — the per-second retention curve.
The factory already keeps a single rolled-up number per post; this keeps the SHAPE: how fast a post takes off on each
platform, and (Facebook) where viewers drop out. Feeds hypogeum/platform_learning.py and the moment learning.

Lock rule: all HTTP with NO database connection open; then one short write transaction. Never raises.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from hypogeum.logger import get_logger

log = get_logger(agent="platform_timeline_sync")

MAX_POSTS_PER_RUN = 15          # catch up gradually; each post is one request
REFETCH_AFTER_H = 20            # a post is re-read at most about once a day
STABLE_AFTER_DAYS = 21          # an old post that was read once is settled
_INT = ("impressions", "reach", "views", "likes", "comments", "shares", "saves", "clicks", "replays", "follows")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plan(conn, channel_id: str) -> List[dict]:
    """Posts worth reading now, newest first."""
    from hypogeum.dashboard_content_grid import _resolve_posts
    posts = [p for p in _resolve_posts(conn, channel_id) if p.get("provider") != "postiz"]
    if not posts:
        return []
    ids = [p["post_id"] for p in posts]
    marks = ",".join("?" for _ in ids)
    last = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT post_id, MAX(fetched_at), MIN(date) FROM post_timeline WHERE post_id IN ({marks}) GROUP BY post_id", ids)}
    now = _now()
    out = []
    for p in posts:
        fetched, first_date = last.get(p["post_id"], (None, None))
        if fetched:
            age_h = (now - datetime.fromisoformat(fetched)).total_seconds() / 3600
            if age_h < REFETCH_AFTER_H:
                continue
            try:
                if first_date and (now.date() - datetime.fromisoformat(first_date).date()).days > STABLE_AFTER_DAYS:
                    continue                                  # old and already read: settled
            except ValueError:
                pass
        p["_never"] = fetched is None
        out.append(p)
    out.sort(key=lambda p: (not p["_never"], p["post_id"]), reverse=False)   # never-read posts first
    return out


def _fetch(provider, post_id: str, db_path) -> Optional[List[dict]]:
    try:
        r = provider._get_analytics_resource("/analytics/post-timeline", db_path=db_path, params={"postId": post_id})
    except Exception as exc:  # noqa: BLE001
        log.warn("post_timeline_fetch_failed", post_id=post_id, error=str(exc)[:120])
        return None
    if r.get("error") or r.get("data") is None:
        return None
    return (r["data"] or {}).get("timeline") or []


def _int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def sync_channel_timelines(channel_id: str, *, provider=None, db_path=None, max_posts: int = MAX_POSTS_PER_RUN) -> dict:
    out = {"channel_id": channel_id, "posts": 0, "rows": 0, "curves": 0, "error": None}
    try:
        from hypogeum.db import get_connection, initialize_schema
        if provider is None:
            from hypogeum.publishing.zernio import ZernioProvider
            provider = ZernioProvider()
        initialize_schema(db_path)
        conn = get_connection(db_path)
        try:
            plan = _plan(conn, channel_id)[:max_posts]
        finally:
            conn.close()                                       # no connection during the HTTP below
        fetched: Dict[str, tuple] = {}
        for p in plan:
            tl = _fetch(provider, p["post_id"], db_path)
            if tl is not None:
                fetched[p["post_id"]] = (p, tl)
        if not fetched:
            return out
        now = _now().isoformat()
        conn = get_connection(db_path)
        try:
            for post_id, (p, tl) in fetched.items():
                last_curve: Dict[str, dict] = {}
                for row in tl:
                    plat = row.get("platform")
                    if not plat or not row.get("date"):
                        continue
                    vals = {k: _int(row.get(k)) for k in _INT}
                    conn.execute(
                        "INSERT OR REPLACE INTO post_timeline (post_id, platform, date, channel_id, episode_id, deliverable_id, impressions, "
                        "reach, views, likes, comments, shares, saves, clicks, replays, completion_rate, follows, fetched_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (post_id, plat, row["date"], channel_id, p.get("episode_id"), p.get("deliverable_id"), vals["impressions"], vals["reach"],
                         vals["views"], vals["likes"], vals["comments"], vals["shares"], vals["saves"], vals["clicks"], vals["replays"],
                         row.get("completionRate"), vals["follows"], now))
                    out["rows"] += 1
                    rc = row.get("retentionCurve")
                    if isinstance(rc, dict) and len(rc) >= 5:
                        last_curve[plat] = rc
                for plat, rc in last_curve.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO post_retention (post_id, platform, channel_id, episode_id, deliverable_id, curve_json, seconds, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (post_id, plat, channel_id, p.get("episode_id"), p.get("deliverable_id"), json.dumps(rc), len(rc), now))
                    out["curves"] += 1
                out["posts"] += 1
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
        log.error("post_timeline_sync_failed", channel_id=channel_id, error=out["error"])
    return out
