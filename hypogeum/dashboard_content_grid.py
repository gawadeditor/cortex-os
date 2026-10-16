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
from hypogeum.publishing.zernio_schedule_sync import post_release_time

log = get_logger(agent="dashboard_content_grid")

_CACHE_TTL_SECONDS = 900  # 15 min — matches the dashboard's other soft-refresh windows
_LIVE_FETCH_TIMEOUT_SECONDS = 12  # short: one slow post must not stall the whole page load

# Statuses that mean "this post is in the state it's supposed to be in" --
# anything else (partial, failed, or a status this module hasn't seen yet)
# needs a human to look at it. Added 2026-09-26 after a real post
# ("Pip the Tiny Star", twinkle_mini_tales) sat in Zernio's own `partial`
# status with a wrong live YouTube thumbnail for hours before the operator
# caught it by chance -- the signal was already in Zernio's response the
# whole time, nothing was surfacing it. See project memory for the incident.
_OK_STATUSES = {"published", "scheduled"}

# Manually-generated covers for posts Zernio itself has no thumbnail for and
# will never accept one on via its own API (repair_existing_post is rejected
# outright on a published post -- confirmed live 2026-09-11/12 on all 4
# platforms). A human CAN still fix this directly on the platform itself,
# outside Zernio entirely: YouTube from Studio's own "Edit thumbnail"
# control, and Facebook from the Page's own post-editing settings (found
# and confirmed working live by the operator 2026-09-17) -- so these files
# exist purely for the operator to upload there by hand. Never auto-applied;
# this mapping only feeds the Grid page's cover-ready note. Keyed by Zernio
# post_id; add an entry here (and drop the file in
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

# Posts from _MANUAL_COVER_FILES the operator has confirmed they've already
# uploaded by hand on every platform that has a manual fix path (2026-09-17:
# all 9 then-known ones, on both YouTube and Facebook). Zernio's own post
# record can never reflect this -- a native, out-of-band platform edit never
# syncs back to it -- so without this list the Grid page would nag "no
# cover" on these forever even though they're actually fine. Purely a
# manually-maintained acknowledgement, same spirit as _MANUAL_COVER_FILES
# itself: move a post_id here once you've actually uploaded its file
# everywhere you can, never before.
_MANUALLY_COVER_RESOLVED = {
    "6a9939e7823e3a6833083c3b",  # twinkle — Pip the Owl
    "6a9c2b94852558ef893023c2",  # hypogeum — Petra
    "6a995063aff2c842d38e452e",  # hypogeum — Colosseum
    "6a9567a0ed870051aa8dd08d",  # hypogeum — Malta 70Hz short
    "6a99cf118272dfe44ae8724e",  # hypogeum — Valley of the Kings tomb
    "6a9c209e578347d2c4a8ddd1",  # twinkle — Turtle & the Moon
    "6a9980306fef4de8c0100d7f",  # twinkle — Milo's Bakery
    "6a98797bb801aca05bac4450",  # twinkle — Mia & the Candy Kingdom
    "6a9b60d19b0f9840ea5b4fb3",  # twinkle — Benny Bunny
}


# YouTube Shorts ignore the cover Zernio sends: YouTube only takes a custom
# Shorts thumbnail from Studio on a computer (no API path; Zernio's docs:
# "Custom thumbnails work on videos only, not Shorts"). Confirmed live
# 2026-09-29 on 3 posts whose Zernio record carried the right cover while
# Studio/Shorts shelf showed an auto-picked frame. So every published YouTube
# Short is flagged until the operator uploads its cover in Studio and presses
# "Done" (-> youtube_cover_confirmations). Everything published before this
# cutoff was re-uploaded and checked by eye in the 2026-09-25 remediation
# (all 33 videos on both channels) and is treated as already done.
_YT_COVER_REMEDIATED_BEFORE = "2026-09-25T14:00"


def _youtube_video_id(platforms: list[dict]) -> Optional[str]:
    """The YouTube video id from the post's youtube platform URL
    (watch?v=<id> or /shorts/<id>), or None."""
    for p in platforms:
        if p.get("platform") != "youtube" or not p.get("url"):
            continue
        url = p["url"]
        if "v=" in url:
            return url.split("v=", 1)[1].split("&", 1)[0] or None
        if "/shorts/" in url:
            return url.split("/shorts/", 1)[1].split("?", 1)[0] or None
    return None


def _confirmed_youtube_covers(conn) -> set:
    return {r["post_id"] for r in conn.execute("SELECT post_id FROM youtube_cover_confirmations")}


def mark_youtube_cover_done(post_id: str, channel_id: str, db_path=None) -> None:
    """Record that the operator uploaded this post's cover in YouTube Studio."""
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO youtube_cover_confirmations (post_id, channel_id, confirmed_at) "
            "VALUES (?, ?, ?)", (post_id, channel_id, _now_iso()))
        conn.commit()
    finally:
        conn.close()
    log.info("youtube_cover_marked_done", post_id=post_id, channel_id=channel_id)


def record_studio_cover_check(post_id: str, channel_id: str, note: str = "", db_path=None) -> None:
    """Record that someone LOOKED at the Short in YouTube Studio and its designed cover was there.
    Unlike mark_youtube_cover_done this is never written on a button press alone."""
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.execute("INSERT OR REPLACE INTO youtube_cover_studio_checks (post_id, channel_id, checked_at, note) "
                     "VALUES (?, ?, ?, ?)", (post_id, channel_id, _now_iso(), (note or "")[:200]))
        conn.commit()
    finally:
        conn.close()
    log.info("youtube_cover_studio_checked", post_id=post_id, channel_id=channel_id)


def _live_confirmations(conn) -> set:
    return {(r["post_id"], r["platform"]) for r in conn.execute(
        "SELECT post_id, platform FROM grid_live_confirmations")}


def mark_platform_live(post_id: str, platform: str, channel_id: str, db_path=None) -> None:
    """Record that the operator checked `platform` and the post IS live there,
    although Zernio reported it failed (see schema.sql: grid_live_confirmations)."""
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO grid_live_confirmations (post_id, platform, channel_id, confirmed_at) "
            "VALUES (?, ?, ?, ?)", (post_id, platform, channel_id, _now_iso()))
        conn.commit()
    finally:
        conn.close()
    log.info("grid_platform_marked_live", post_id=post_id, platform=platform, channel_id=channel_id)


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
    """Every distinct post this channel has a successful attempt+receipt
    for, one row per (post_id), earliest deliverable_id kept for display
    (a multi-deliverable episode's main takes priority alphabetically --
    'main_episode' < 'short_*').

    Zernio carries all platforms of a deliverable in ONE post, so one
    post_id is one card. Postiz carries one post per platform, so its posts
    are folded into ONE card per (episode, deliverable): post_id is the
    smallest of its Postiz post ids (stable), `postiz_ids` lists them all,
    and `provider` is 'postiz'."""
    rows = conn.execute(
        """
        SELECT pr.provider_publication_id AS post_id, pa.episode_id,
               MIN(pa.deliverable_id) AS deliverable_id, pa.provider_id AS provider_id,
               pa.platform AS platform
        FROM publishing_attempts pa
        JOIN publishing_receipts pr ON pr.attempt_id = pa.attempt_id
        WHERE pa.state = 'succeeded' AND pa.channel_id = ?
              AND pr.provider_publication_id IS NOT NULL
        GROUP BY pr.provider_publication_id
        """,
        (channel_id,),
    ).fetchall()
    out: list[dict] = []
    postiz: dict = {}
    for r in rows:
        if r["provider_id"] == "postiz":
            postiz.setdefault((r["episode_id"], r["deliverable_id"]), []).append(
                (r["post_id"], r["platform"]))
        else:
            out.append({"post_id": r["post_id"], "episode_id": r["episode_id"],
                        "deliverable_id": r["deliverable_id"]})
    for (episode_id, deliverable_id), ids in postiz.items():
        # The card is keyed by its YouTube post when it has one: the cover hold and
        # the "cover done" confirmation are recorded against that post id.
        yt = sorted(pid for pid, plat in ids if plat == "youtube")
        ids = sorted(pid for pid, _ in ids)
        out.append({"post_id": (yt or ids)[0], "episode_id": episode_id, "deliverable_id": deliverable_id,
                    "provider": "postiz", "postiz_ids": ids})
    return out


def _local_episode_title(conn, episode_id: str) -> str:
    row = conn.execute("SELECT title, topic FROM episodes WHERE episode_id = ?", (episode_id,)).fetchone()
    if not row:
        return episode_id
    return row["title"] or row["topic"] or episode_id



def _plain_fetch_error(exc: Exception) -> str:
    """What the operator sees on a card when a live refresh failed. The
    data shown is the last good copy, so say that plainly instead of a raw
    urllib3 traceback (2026-10-05: a network drop put a NameResolutionError
    on every card and it looked like the posts were broken)."""
    text = str(exc)
    if any(k in text for k in ("NameResolution", "Max retries", "ConnectionError", "timed out", "Timeout")):
        return "Couldn't reach Zernio (network) — showing the last saved copy; it refreshes on its own."
    return f"Couldn't refresh from Zernio ({text[:120]}) — showing the last saved copy."

def _fetch_live(provider, post_id: str, *, known_thumbnail_url: Optional[str] = None,
                 known_is_landscape: Optional[bool] = None, channel_id: Optional[str] = None) -> dict:
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
        # the key of the workspace owning this channel (multi-tenant Phase 5)
        res = (provider.get_post(post_id, timeout=_LIVE_FETCH_TIMEOUT_SECONDS, channel_id=channel_id)
               if channel_id else provider.get_post(post_id, timeout=_LIVE_FETCH_TIMEOUT_SECONDS))
    except Exception as exc:  # noqa: BLE001
        return {"fetch_error": _plain_fetch_error(exc)}
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
            "error": (p.get("errorMessage") or "")[:300] or None,
        })
    return {
        "title": post.get("title"),
        "status": post.get("status"),
        "scheduled_for": post_release_time(post) or None,
        "published_at": next((p["published_at"] for p in platforms if p.get("published_at")), None),
        "thumbnail_url": thumbnail_url,
        "thumb_is_landscape": (1 if is_landscape else (0 if is_landscape is False else None)),
        "platforms_json": json.dumps(platforms),
        "fetch_error": None,
    }


# Postiz's own post states -> the status words this page already understands.
_POSTIZ_STATE = {"PUBLISHED": "published", "QUEUE": "scheduled", "ERROR": "failed", "DRAFT": "draft"}


def _postiz_card_status(platforms: list[dict]) -> Optional[str]:
    """One post-level status for a card whose platforms are separate Postiz
    posts. A platform still queued next to a published one is a normal
    staggered release (cover hold, per-platform slots), so it reads
    'scheduled', never 'partial'."""
    states = {p.get("status") for p in platforms}
    if not states:
        return None
    if states == {"published"}:
        return "published"
    if "failed" in states:
        return "partial" if "published" in states else "failed"
    return "scheduled"


def _fetch_live_postiz(provider, posts: list[dict], *, channel_id: Optional[str] = None,
                       db_path=None) -> dict:
    """Live rows for every Postiz card in `posts`, from ONE list call (the
    API allows only 30 requests/hour, so a per-post GET is not an option).
    Returns {card post_id: live dict}; on a failed list every card gets a
    `fetch_error` row, which the caller turns into 'keep the cached copy'."""
    import datetime as _dt
    from urllib.parse import urlencode
    from hypogeum.publishing.postiz import _iso_utc
    now = _dt.datetime.now(_dt.timezone.utc)
    try:
        headers = provider._headers(db_path, channel_id=channel_id)
        q = urlencode({"startDate": _iso_utc(now - _dt.timedelta(days=60)),
                       "endDate": _iso_utc(now + _dt.timedelta(days=30))})
        resp = provider._transport.get(url=f"{provider._base_url}/posts?{q}", headers=headers,
                                       timeout=_LIVE_FETCH_TIMEOUT_SECONDS)
        if resp.status_code != 200 or not isinstance(resp.json_body, dict):
            raise RuntimeError(f"HTTP {resp.status_code}")
        listed = {p.get("id"): p for p in resp.json_body.get("posts") or [] if isinstance(p, dict)}
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        msg = ("Couldn't reach Postiz (network) — showing the last saved copy; it refreshes on its own."
               if any(k in text for k in ("NameResolution", "Max retries", "ConnectionError", "timed out", "Timeout"))
               else f"Couldn't refresh from Postiz ({text[:120]}) — showing the last saved copy.")
        return {p["post_id"]: {"fetch_error": msg} for p in posts}

    out = {}
    for card in posts:
        platforms, title, dates = [], None, []
        for pid in card.get("postiz_ids") or [card["post_id"]]:
            lp = listed.get(pid)
            if lp is None:
                continue
            state = _POSTIZ_STATE.get(str(lp.get("state") or "").upper(), "scheduled")
            platform = (lp.get("integration") or {}).get("providerIdentifier")
            url = lp.get("releaseURL") if state == "published" else None
            if platform == "tiktok" and url and "/video/" not in url:
                url = None  # Postiz stores TikTok's inbox link when the upload is a draft
            platforms.append({
                "platform": platform, "status": state, "url": url,
                "published_at": lp.get("publishDate") if state == "published" else None,
                "error": ("Postiz reports this post failed — open it in Postiz for the reason."
                          if state == "failed" else None),
            })
            dates.append(lp.get("publishDate"))
            if title is None and platform == "youtube":
                try:
                    title = (json.loads(lp.get("settings") or "{}") or {}).get("title")
                except Exception:  # noqa: BLE001
                    pass
        if not platforms:
            out[card["post_id"]] = {"fetch_error": "Not found in Postiz (outside the listed window)."}
            continue
        dates = [d for d in dates if d]
        out[card["post_id"]] = {
            "title": title,
            "status": _postiz_card_status(platforms),
            "scheduled_for": max(dates) if dates else None,
            "published_at": next((p["published_at"] for p in platforms if p.get("published_at")), None),
            # The cover the adapter attached at publish time is the episode's own designed
            # thumbnail; Postiz's list carries no media, so show that file.
            "thumbnail_url": f"/episode/{card['episode_id']}/thumbnail",
            "thumb_is_landscape": None,
            "platforms_json": json.dumps(platforms),
            "fetch_error": None,
        }
    return out


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
    status, needs_attention, not_yet_live_platforms, scheduled_for,
    published_at, thumbnail_url, has_cover, platforms: [...], stale,
    fetch_error}. `needs_attention` is true whenever Zernio's own status
    is anything other than a normal 'published'/'scheduled' (e.g. 'partial',
    'failed') -- surface this prominently, it is a real, already-available
    signal that a post did not go out the way it was supposed to.

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
    postiz_fetch = [p for p in to_fetch if p.get("provider") == "postiz"]
    if postiz_fetch:
        from hypogeum.publishing.postiz import PostizProvider
        for post_id, live in _fetch_live_postiz(
                PostizProvider(), postiz_fetch, channel_id=channel_id, db_path=db_path).items():
            p = next(x for x in postiz_fetch if x["post_id"] == post_id)
            if live.get("fetch_error"):
                live_error = live["fetch_error"]
                old = cached_by_post.get(post_id) or {}
                live = {k: old.get(k) for k in ("title", "status", "scheduled_for", "published_at",
                                                "thumbnail_url", "thumb_is_landscape")}
                live["platforms_json"] = old.get("platforms_json", "[]")
                live["fetch_error"] = live_error
            fresh_rows[post_id] = live
            rows_for_display[post_id] = {**p, "channel_id": channel_id, **live}
    to_fetch = [p for p in to_fetch if p.get("provider") != "postiz"]
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
                    channel_id=channel_id,
                ): p
                for p in to_fetch
            }
            for future in as_completed(future_to_post):
                p = future_to_post[future]
                try:
                    live = future.result()
                except Exception as exc:  # noqa: BLE001
                    live = {"fetch_error": _plain_fetch_error(exc)}
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
        yt_confirmed = _confirmed_youtube_covers(conn)
        yt_holds = {r["post_id"]: dict(r) for r in conn.execute("SELECT * FROM youtube_cover_holds")}
        live_ok = _live_confirmations(conn)
        out = []
        for p in posts:
            row = rows_for_display[p["post_id"]]
            platforms = []
            try:
                platforms = json.loads(row.get("platforms_json") or "[]")
            except Exception:  # noqa: BLE001
                pass

            display_title = row.get("title") or _local_episode_title(conn, p["episode_id"])
            # A TikTok-only post (FB/IG/YouTube moved to Postiz) is uploaded as a bare video: TikTok takes no
            # custom cover, so Zernio keeps no thumbnail for it. That is not a missing cover -- show the
            # episode's own designed cover as the poster instead of a "No cover" warning.
            if not row.get("thumbnail_url") and platforms and all(pl.get("platform") == "tiktok" for pl in platforms):
                row = {**row, "thumbnail_url": f"/episode/{p['episode_id']}/thumbnail"}
            zernio_has_cover = bool(row.get("thumbnail_url"))
            manual_key = _MANUAL_COVER_FILES.get(p["post_id"]) if not zernio_has_cover else None
            resolved_manually = p["post_id"] in _MANUALLY_COVER_RESOLVED
            # Zernio's own record never reflects a native, out-of-band
            # platform edit -- once the operator confirms they've uploaded
            # the manual file everywhere by hand, treat the post as covered
            # (no more "no cover" warning) while still showing that same
            # file as the poster image, since it's the accurate one now.
            has_cover = zernio_has_cover or resolved_manually
            status = row.get("status")
            # A platform the operator checked by hand and confirmed live
            # counts as published, whatever Zernio's record says.
            for pl in platforms:
                if (p["post_id"], pl.get("platform")) in live_ok:
                    pl["status"] = "published"
                    pl["confirmed_by_hand"] = True
            not_yet_live_platforms = [
                p.get("platform") for p in platforms
                if p.get("platform") and p.get("status") not in ("published", None)
            ]
            needs_attention = bool(status) and status not in _OK_STATUSES
            if needs_attention and status != "failed" and not not_yet_live_platforms:
                needs_attention = False
            # A YouTube cover hold uploads YouTube ~24h before the other
            # platforms, so Zernio may report the post mid-way (e.g. partial)
            # while nothing is wrong -- only flag it if a platform actually failed.
            if needs_attention and p["post_id"] in yt_holds and not any(
                    pl.get("status") not in ("published", "pending", "scheduled", None) for pl in platforms):
                needs_attention = False
            # A vertical (or not-yet-probed) cover on a live YouTube post is a
            # Short; a 16:9 long-form video takes Zernio's API cover fine.
            yt_live = any(pl.get("platform") == "youtube" and pl.get("status") == "published"
                          for pl in platforms)
            youtube_video_id = _youtube_video_id(platforms)
            youtube_cover_pending = (
                yt_live and zernio_has_cover and not row.get("thumb_is_landscape")
                and p["post_id"] not in yt_confirmed
                and p["post_id"] not in _MANUALLY_COVER_RESOLVED
                and (row.get("published_at") or "") >= _YT_COVER_REMEDIATED_BEFORE
            )
            out.append({
                "post_id": p["post_id"],
                "provider": p.get("provider", "zernio"),
                "episode_id": p["episode_id"],
                "deliverable_id": p["deliverable_id"],
                "display_title": display_title,
                "status": status,
                "needs_attention": needs_attention,
                "not_yet_live_platforms": not_yet_live_platforms if needs_attention else [],
                # failed platforms with Zernio's own reason, for the card
                "failed_platforms": [{"platform": pl.get("platform"), "error": pl.get("error")}
                                     for pl in platforms if pl.get("status") == "failed"]
                if needs_attention else [],
                "scheduled_for": row.get("scheduled_for"),
                "published_at": row.get("published_at"),
                "thumbnail_url": row.get("thumbnail_url"),
                "is_landscape": bool(row.get("thumb_is_landscape")),
                "has_cover": has_cover,
                "manual_cover_url": (f"/manual-cover/{manual_key}.jpg" if manual_key else None),
                "manual_cover_note": (
                    "Cover confirmed uploaded by hand (Zernio's own record can't reflect a "
                    "native platform edit, so this page can't detect it automatically)"
                    if manual_key and resolved_manually else
                    "Cover ready — YouTube + Facebook accept a manual upload from their own "
                    "settings; Zernio/TikTok/Instagram reject a post-publish cover change via "
                    "any API"
                    if manual_key else None
                ),
                "platforms": platforms,
                "fetch_error": row.get("fetch_error"),
                "youtube_cover_pending": youtube_cover_pending,
                "youtube_studio_url": (f"https://studio.youtube.com/video/{youtube_video_id}/edit"
                                       if youtube_video_id else None),
                # youtube_cover_hold state: held | late | released | release_failed | None
                "youtube_hold_status": (yt_holds.get(p["post_id"]) or {}).get("status"),
                "youtube_hold_release_at": (yt_holds.get(p["post_id"]) or {}).get("release_at"),
                "youtube_hold_upload_at": (yt_holds.get(p["post_id"]) or {}).get("upload_at"),
            })
    finally:
        conn.close()

    def _sort_key(item):
        # Newest activity first: published_at, else scheduled_for, else empty last.
        return item.get("published_at") or item.get("scheduled_for") or ""
    out.sort(key=_sort_key, reverse=True)
    return out
