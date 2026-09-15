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
_LIVE_FETCH_TIMEOUT_SECONDS = 12  # short: one slow post must not stall the whole page load

# Manually-generated covers for posts Zernio itself has no thumbnail for and
# will never accept one on (repair_existing_post is rejected outright on a
# published post -- confirmed live 2026-09-11/12 on all 4 platforms, and
# Zernio's own docs only document post-publish editing for Discord). YouTube
# is the one platform where a human CAN still fix this -- from Studio's own
# "Edit thumbnail" control, not any API -- so these files exist purely for
# the operator to upload there by hand. Never auto-applied; this mapping
# only feeds the Grid page's "cover ready — YouTube only" note. Keyed by
# Zernio post_id; add an entry here (and drop the file in
# .hypogeum-assets/manual_youtube_covers/) whenever a new one turns up.
_MANUAL_COVER_FILES = {
    "6a9939e7823e3a6833083c3b": "ep_0e01db91",       # twinkle — Pip the Owl
    "6a9c2b94852558ef893023c2": "ep_19c9fc5f",       # hypogeum — Petra
    "6a995063aff2c842d38e452e": "ep_663eb92d",       # hypogeum — Colosseum
    "6a9567a0ed870051aa8dd08d": "ep_af078f34_short_001",  # hypogeum — Malta 70Hz short
    "6a99cf118272dfe44ae8724e": "ep_afd7fb9f",       # hypogeum — Valley of the Kings tomb
    "6a9c209e578347d2c4a8ddd1": "ep_bc175310",       # twinkle — Turtle & the Moon
    "6a9980306fef4de8c0100d7f": "ep_d4109e56",       # twinkle — Milo's Bakery
    "6a98797bb801aca05bac4450": "ep_f38dc597",       # twinkle — Mia & the Candy Kingdom
    "6a9b60d19b0f9840ea5b4fb3": "ep_f9184c3e",       # twinkle — Benny Bunny
}
_MANUAL_COVER_DIR = "manual_youtube_covers"  # under .hypogeum-assets/, served by /manual-cover/<key>.jpg


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_landscape(url: str) -> Optional[bool]:
    """A handful of posts predate the vertical-cover pivot and still carry a
    16:9 `candidate_1.jpg`-style cover (e.g. ep_af078f34's main -- see
    project_vertical_thumbnail_gap memory). Forced into the grid's normal
    9:16 box with `object-fit:contain`, a 16:9 image renders as a thin,
    heavily-letterboxed strip that (when the source image is itself mostly
    dark, as this channel's covers are) reads as a blank card. One small GET
    + PIL on first sight of a URL, cached alongside it -- never re-checked
    once known. Returns None (unknown -> render as if portrait) on any
    failure; never raises."""
    try:
        import requests
        from io import BytesIO
        from PIL import Image
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        im = Image.open(BytesIO(resp.content))
        w, h = im.size
        return w > h
    except Exception as exc:  # noqa: BLE001
        log.warn("content_grid_landscape_probe_failed", url=url, error=str(exc)[:160])
        return None


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


def _fetch_live(provider, post_id: str, *, known_thumbnail_url: Optional[str] = None,
                 known_is_landscape: Optional[bool] = None) -> dict:
    """One GET /posts/{id}, normalised into the row shape this module
    caches and the template renders. Never raises -- a transport failure
    lands in `fetch_error`; the caller preserves the previous cached
    fields for this post rather than blanking them out.

    `known_thumbnail_url`/`known_is_landscape` let a repeat fetch of an
    already-seen post skip the orientation probe's extra GET+PIL decode
    when the cover hasn't changed -- the one-GET-per-URL promise in
    `_is_landscape`'s docstring, actually enforced here rather than just
    described."""
    try:
        res = provider.get_post(post_id, timeout=_LIVE_FETCH_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        return {"fetch_error": str(exc)[:200]}
    post = res.get("post") or {}
    if res.get("status_code") != 200 or not post:
        return {"fetch_error": f"status={res.get('status_code')} error={res.get('error')!r}"[:200]}

    media_items = post.get("mediaItems") or []
    thumbnail_url = media_items[0].get("thumbnail") if media_items else None
    if thumbnail_url and thumbnail_url == known_thumbnail_url and known_is_landscape is not None:
        is_landscape = known_is_landscape
    else:
        is_landscape = _is_landscape(thumbnail_url) if thumbnail_url else None
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
        "thumb_is_landscape": (1 if is_landscape else (0 if is_landscape is False else None)),
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
             scheduled_for, published_at, thumbnail_url, thumb_is_landscape,
             platforms_json, fetch_error, cached_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(post_id) DO UPDATE SET
            channel_id=excluded.channel_id, episode_id=excluded.episode_id,
            deliverable_id=excluded.deliverable_id, title=excluded.title,
            status=excluded.status, scheduled_for=excluded.scheduled_for,
            published_at=excluded.published_at, thumbnail_url=excluded.thumbnail_url,
            thumb_is_landscape=excluded.thumb_is_landscape,
            platforms_json=excluded.platforms_json, fetch_error=excluded.fetch_error,
            cached_at=excluded.cached_at
        """,
        (post_id, channel_id, episode_id, deliverable_id,
         live.get("title"), live.get("status"), live.get("scheduled_for"),
         live.get("published_at"), live.get("thumbnail_url"), live.get("thumb_is_landscape"),
         live.get("platforms_json", "[]"), live.get("fetch_error"), _now_iso()),
    )


def get_content_grid(channel_id: str, db_path=None, *, force_refresh: bool = False) -> list[dict]:
    """Every post this channel has on Zernio, newest scheduled/published
    first. Each item: {post_id, episode_id, deliverable_id, display_title,
    status, scheduled_for, published_at, thumbnail_url, has_cover,
    platforms: [...], stale, fetch_error}.

    Deliberately three short DB sessions around one un-connected stretch of
    slow network I/O, not one connection held open end to end: a stale post
    means a live GET to Zernio plus a GET+PIL probe of its image. Holding
    one open write transaction across that whole stretch (the original
    shape of this function) starved any other request touching the DB
    (SQLite/WAL serialises writers), surfacing to an operator as "database
    is locked" on a live dashboard with real concurrent traffic -- fixed by
    reading the cache, closing; doing every network call with no connection
    open at all; writing results back in one quick final transaction, close.
    Stale posts are then fetched concurrently (a thread pool, not one at a
    time) -- run sequentially a channel with dozens of posts took tens of
    seconds, long enough that a normal page load could race a browser/
    proxy timeout and look intermittently broken rather than just slow."""
    initialize_schema(db_path)

    conn = get_connection(db_path)
    try:
        posts = _resolve_posts(conn, channel_id)
        if not posts:
            return []
        # Always read the cache, even on a forced refresh -- it is the
        # last-known-good fallback if the live fetch below errors, not
        # just a freshness check. `force_refresh` only changes which
        # posts get re-fetched (all of them), never whether we have a
        # fallback to fall back to.
        cached_by_post = {p["post_id"]: _cache_get(conn, p["post_id"]) for p in posts}
    finally:
        conn.close()

    to_fetch = [p for p in posts
                if (force_refresh
                    or cached_by_post.get(p["post_id"]) is None
                    or cached_by_post[p["post_id"]].get("_stale"))]
    rows_for_display = {
        p["post_id"]: cached_by_post[p["post_id"]] for p in posts if p not in to_fetch
    }

    # Each stale post costs a live Zernio GET plus (when it has a cover) a
    # second GET+PIL probe for orientation -- sequentially, tens of seconds
    # for a channel with dozens of posts, long enough to look flaky (a slow
    # page load racing a browser/proxy timeout looks identical to a broken
    # one). Fetched concurrently instead: these are independent, read-only,
    # network-bound calls (each opens its own short-lived DB connection
    # only for the quick credential SELECT before the network call, per
    # ZernioProvider.get_post -- safe under WAL from multiple threads).
    fresh_rows = {}  # post_id -> live dict
    if to_fetch:
        from hypogeum.publishing.zernio import ZernioProvider
        from concurrent.futures import ThreadPoolExecutor, as_completed
        provider = ZernioProvider()
        with ThreadPoolExecutor(max_workers=min(12, len(to_fetch))) as pool:
            future_to_post = {
                pool.submit(
                    _fetch_live, provider, p["post_id"],
                    known_thumbnail_url=(cached_by_post.get(p["post_id"]) or {}).get("thumbnail_url"),
                    known_is_landscape=(lambda v: bool(v) if v is not None else None)(
                        (cached_by_post.get(p["post_id"]) or {}).get("thumb_is_landscape")
                    ),
                ): p
                for p in to_fetch
            }
            for future in as_completed(future_to_post):
                p = future_to_post[future]
                try:
                    live = future.result()
                except Exception as exc:  # noqa: BLE001
                    live = {"fetch_error": str(exc)[:200]}
                if live.get("fetch_error"):
                    # A transient Zernio failure must not blank out data we
                    # already had -- keep the last known-good fields for
                    # this post and just surface the new error alongside
                    # them, so a flaky refresh cycle degrades to "slightly
                    # stale" rather than "looks empty/broken".
                    old = cached_by_post.get(p["post_id"]) or {}
                    live = {
                        "title": old.get("title"),
                        "status": old.get("status"),
                        "scheduled_for": old.get("scheduled_for"),
                        "published_at": old.get("published_at"),
                        "thumbnail_url": old.get("thumbnail_url"),
                        "thumb_is_landscape": old.get("thumb_is_landscape"),
                        "platforms_json": old.get("platforms_json", "[]"),
                        "fetch_error": live["fetch_error"],
                    }
                fresh_rows[p["post_id"]] = live
                rows_for_display[p["post_id"]] = {**p, "channel_id": channel_id, **live}

    if fresh_rows:
        conn = get_connection(db_path)
        try:
            for p in posts:
                if p["post_id"] in fresh_rows:
                    _cache_put(conn, channel_id=channel_id, episode_id=p["episode_id"],
                               deliverable_id=p["deliverable_id"], post_id=p["post_id"],
                               live=fresh_rows[p["post_id"]])
            conn.commit()
        finally:
            conn.close()

    conn = get_connection(db_path)
    try:
        out = []
        for p in posts:
            row = rows_for_display[p["post_id"]]
            platforms = []
            try:
                platforms = json.loads(row.get("platforms_json") or "[]")
            except Exception:  # noqa: BLE001
                pass

            display_title = row.get("title") or _local_episode_title(conn, p["episode_id"])
            has_cover = bool(row.get("thumbnail_url"))
            manual_key = _MANUAL_COVER_FILES.get(p["post_id"]) if not has_cover else None
            out.append({
                "post_id": p["post_id"],
                "episode_id": p["episode_id"],
                "deliverable_id": p["deliverable_id"],
                "display_title": display_title,
                "status": row.get("status"),
                "scheduled_for": row.get("scheduled_for"),
                "published_at": row.get("published_at"),
                "thumbnail_url": row.get("thumbnail_url"),
                "is_landscape": bool(row.get("thumb_is_landscape")),
                "has_cover": has_cover,
                "manual_cover_url": (f"/manual-cover/{manual_key}.jpg" if manual_key else None),
                "manual_cover_note": ("Cover ready — YouTube only (upload from Studio; "
                                      "Zernio/TikTok/Instagram/Facebook reject a post-publish "
                                      "cover change)") if manual_key else None,
                "platforms": platforms,
                "fetch_error": row.get("fetch_error"),
            })
    finally:
        conn.close()

    def _sort_key(item):
        # Newest activity first: published_at, else scheduled_for, else empty last.
        return item.get("published_at") or item.get("scheduled_for") or ""
    out.sort(key=_sort_key, reverse=True)
    return out
