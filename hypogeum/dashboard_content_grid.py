"""
hypogeum/dashboard_content_grid.py — data layer for /channel/<id>/grid, an
in-dashboard "content grid": every post a channel has staged or published on
Zernio, with its poster, per-platform status, and scheduled/published dates
— the same view an operator would otherwise open Zernio itself to see.

Zernio's GET /posts/{id} is the source of truth for all of this (status,
scheduledFor/publishedAt, mediaItems[].thumbnail, per-platform state) — we
never store our own copy of it as authoritative. `content_grid_cache` is
purely a speed/rate-limit buffer: a channel with dozens of posts would mean
dozens of live GETs on every page load otherwise. A row is reused while
fresh (`_CACHE_TTL_SECONDS`) and re-fetched on expiry or an explicit
operator refresh (`force_refresh=True`, wired to a "Refresh" button).

Public API:
    get_content_grid(channel_id, db_path=None, force_refresh=False) -> list[dict]
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from hypogeum.db import get_connection, initialize_schema
from hypogeum.logger import get_logger

log = get_logger(agent="dashboard_content_grid")

_CACHE_TTL_SECONDS = 900  # 15 min — matches the dashboard's other soft-refresh windows


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_posts(conn, channel_id: str) -> list[dict]:
    """Every distinct Zernio post this channel has a successful attempt+
    receipt for, one row per (post_id), earliest deliverable_id kept for
    display (a multi-deliverable episode's main takes priority alphabetically
    -- 'main_episode' < 'short_*')."""
    rows = conn.execute(
        """
        SELECT pr.provider_publication_id AS post_id, pa.episode_id,
               MIN(pa.deliverable_id) AS deliverable_id
        FROM publishing_attempts pa
        JOIN publishing_receipts pr ON pr.attempt_id = pa.attempt_id
        WHERE pa.state = 'succeeded' AND pa.channel_id = ?
              AND pr.provider_publication_id IS NOT NULL
        GROUP BY pr.provider_publication_id
        """,
        (channel_id,),
    ).fetchall()
    return [
        {"post_id": r["post_id"], "episode_id": r["episode_id"], "deliverable_id": r["deliverable_id"]}
        for r in rows
    ]


def _local_episode_title(conn, episode_id: str) -> str:
    row = conn.execute("SELECT title, topic FROM episodes WHERE episode_id = ?", (episode_id,)).fetchone()
    if not row:
        return episode_id
    return row["title"] or row["topic"] or episode_id


def _fetch_live(provider, post_id: str) -> dict:
    """One GET /posts/{id}, normalised into the row shape this module
    caches and the template renders. Never raises -- a transport failure
    lands in `fetch_error`, the row still renders (poster-less, dated
    'unknown') rather than disappearing."""
    try:
        res = provider.get_post(post_id)
    except Exception as exc:  # noqa: BLE001
        return {"fetch_error": str(exc)[:200]}
    post = res.get("post") or {}
    if res.get("status_code") != 200 or not post:
        return {"fetch_error": f"status={res.get('status_code')} error={res.get('error')!r}"[:200]}

    media_items = post.get("mediaItems") or []
    thumbnail_url = media_items[0].get("thumbnail") if media_items else None
    platforms = []
    for p in post.get("platforms") or []:
        platforms.append({
            "platform": p.get("platform"),
            "status": p.get("status"),
            "url": p.get("platformPostUrl"),
            "published_at": p.get("publishedAt"),
        })
    return {
        "title": post.get("title"),
        "status": post.get("status"),
        "scheduled_for": post.get("scheduledFor"),
        "published_at": next((p["published_at"] for p in platforms if p.get("published_at")), None),
        "thumbnail_url": thumbnail_url,
        "platforms_json": json.dumps(platforms),
        "fetch_error": None,
    }


def _cache_get(conn, post_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM content_grid_cache WHERE post_id = ?", (post_id,)).fetchone()
    if not row:
        return None
    row = dict(row)
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["cached_at"])).total_seconds()
    except Exception:  # noqa: BLE001
        age = _CACHE_TTL_SECONDS + 1
    row["_stale"] = age >= _CACHE_TTL_SECONDS
    return row


def _cache_put(conn, *, post_id, channel_id, episode_id, deliverable_id, live: dict) -> None:
    conn.execute(
        """
        INSERT INTO content_grid_cache
            (post_id, channel_id, episode_id, deliverable_id, title, status,
             scheduled_for, published_at, thumbnail_url, platforms_json, fetch_error, cached_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(post_id) DO UPDATE SET
            channel_id=excluded.channel_id, episode_id=excluded.episode_id,
            deliverable_id=excluded.deliverable_id, title=excluded.title,
            status=excluded.status, scheduled_for=excluded.scheduled_for,
            published_at=excluded.published_at, thumbnail_url=excluded.thumbnail_url,
            platforms_json=excluded.platforms_json, fetch_error=excluded.fetch_error,
            cached_at=excluded.cached_at
        """,
        (post_id, channel_id, episode_id, deliverable_id,
         live.get("title"), live.get("status"), live.get("scheduled_for"),
         live.get("published_at"), live.get("thumbnail_url"),
         live.get("platforms_json", "[]"), live.get("fetch_error"), _now_iso()),
    )


def get_content_grid(channel_id: str, db_path=None, *, force_refresh: bool = False) -> list[dict]:
    """Every post this channel has on Zernio, newest scheduled/published
    first. Each item: {post_id, episode_id, deliverable_id, display_title,
    status, scheduled_for, published_at, thumbnail_url, has_cover,
    platforms: [...], stale, fetch_error}."""
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        posts = _resolve_posts(conn, channel_id)
        if not posts:
            return []

        provider = None
        out = []
        for p in posts:
            cached = None if force_refresh else _cache_get(conn, p["post_id"])
            if cached is None or cached.get("_stale"):
                if provider is None:
                    from hypogeum.publishing.zernio import ZernioProvider
                    provider = ZernioProvider()
                live = _fetch_live(provider, p["post_id"])
                _cache_put(conn, channel_id=channel_id, episode_id=p["episode_id"],
                           deliverable_id=p["deliverable_id"], post_id=p["post_id"], live=live)
                row = {**p, "channel_id": channel_id, **live}
            else:
                row = cached

            platforms = []
            try:
                platforms = json.loads(row.get("platforms_json") or "[]")
            except Exception:  # noqa: BLE001
                pass

            display_title = row.get("title") or _local_episode_title(conn, p["episode_id"])
            out.append({
                "post_id": p["post_id"],
                "episode_id": p["episode_id"],
                "deliverable_id": p["deliverable_id"],
                "display_title": display_title,
                "status": row.get("status"),
                "scheduled_for": row.get("scheduled_for"),
                "published_at": row.get("published_at"),
                "thumbnail_url": row.get("thumbnail_url"),
                "has_cover": bool(row.get("thumbnail_url")),
                "platforms": platforms,
                "fetch_error": row.get("fetch_error"),
            })
        conn.commit()
    finally:
        conn.close()

    def _sort_key(item):
        # Newest activity first: published_at, else scheduled_for, else empty last.
        return item.get("published_at") or item.get("scheduled_for") or ""
    out.sort(key=_sort_key, reverse=True)
    return out
