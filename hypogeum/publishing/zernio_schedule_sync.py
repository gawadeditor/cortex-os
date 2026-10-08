"""
hypogeum/publishing/zernio_schedule_sync.py — mirror Zernio's real post
schedule into the local channel_publish_slots ledger.

hypogeum.publishing.publish_slot_ledger's own docstring says "the provider
(Zernio scheduledFor) remains the source of truth for the actual schedule;
this only spaces out assignment." But nothing ever synced Zernio's truth
BACK into channel_publish_slots — so day_is_taken() / next_free_now() were
deciding one-episode-per-day off a ledger that could drift arbitrarily far
from what Zernio actually has scheduled or published. On 2026-09-05 a
forced republish + a stale ledger produced two same-day posts on one
channel.

This module makes channel_publish_slots an exact mirror of Zernio for every
(channel, calendar-day) Zernio has a post on — scheduled OR published,
within a lookback window — so the daily-cap guards are reliable. A local
future slot that Zernio does not yet have a post for (a fresh assignment
made moments before its provider call) is preserved.

Public API:
    sync_zernio_schedule(*, db_path=None, window_days_back=3,
                         transport=None) -> dict
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_ZERNIO_LIST_LIMIT = 100

# A local future slot that Zernio's list does not (yet) confirm is kept only
# this long after it was first recorded — long enough to cover the gap
# between a fresh assignment and its provider call + the list catching up,
# short enough that a slot whose Zernio post was later deleted/moved stops
# haunting the mirror (and the one-episode-per-day guards) for a week.
_LOCAL_SLOT_GRACE_SECONDS = 2 * 3600

# Every staged Zernio media file is named ``..._<deliverable>_<episode_id>.mp4``
# (see hypogeum.publishing.zernio_media_staging) — episode_id is ``ep_`` +
# 8 hex. This is present on 100% of real posts and survives a title edit,
# so it is a far more reliable attribution signal than an exact title match.
_EP_ID_IN_URL = re.compile(r"(ep_[0-9a-fA-F]{8})")


# Postiz's API allows 30 requests/hour by default and the worker syncs every
# iteration, so the Postiz post list is cached between syncs. A stale list is
# safe: it only ever ADDS slots for posts we made, and the day-cap guards
# need "is there already a post that day", which a few minutes' lag cannot
# change (a fresh assignment is also kept by the local grace window).
_POSTIZ_MIN_INTERVAL_SECONDS = 300
_postiz_cache: dict = {"at": 0.0, "key": None, "posts": []}


def _postiz_publishing_in_use(db_path) -> bool:
    """True when at least one enabled publishing binding names Postiz."""
    from hypogeum.db import get_connection
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM channel_provider_bindings WHERE provider_id = 'postiz' "
            "AND capability = 'publishing' AND enabled = 1 LIMIT 1"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _normalize_postiz_post(p: dict) -> Optional[dict]:
    """A Postiz /posts entry in the Zernio post shape this module reads.
    Attribution goes through provider_content_mappings (post id), the same
    first step as for Zernio posts; Postiz-side posts we did not create find
    no mapping and are skipped as unattributed."""
    post_id = str(p.get("id") or "")
    when = str(p.get("publishDate") or "").strip()
    if not post_id or not when:
        return None
    if str(p.get("state") or "").upper() == "ERROR":
        return None  # a failed post holds no slot
    return {"_id": post_id, "scheduledFor": when, "title": "", "platforms": []}


def _fetch_postiz_posts(db_path, *, floor: datetime, now: datetime,
                        provider=None, force: bool = False) -> list:
    """Postiz posts from `floor` to 30 days ahead, normalized; [] when no
    channel is bound to Postiz. Cached for _POSTIZ_MIN_INTERVAL_SECONDS.
    Raises on a failed list so the caller can report it."""
    import time
    if provider is None and not _postiz_publishing_in_use(db_path):
        return []
    key = (str(db_path), floor.date().isoformat())
    if (not force and _postiz_cache["key"] == key
            and time.monotonic() - _postiz_cache["at"] < _POSTIZ_MIN_INTERVAL_SECONDS):
        return list(_postiz_cache["posts"])
    from urllib.parse import urlencode
    from hypogeum.publishing.postiz import (
        PostizProvider, PostizAdapterError, _iso_utc, _DEFAULT_TIMEOUT_SECONDS,
    )
    prov = provider or PostizProvider()
    headers = prov._headers(db_path)
    q = urlencode({"startDate": _iso_utc(floor), "endDate": _iso_utc(now + timedelta(days=30))})
    resp = prov._transport.get(url=f"{prov._base_url}/posts?{q}", headers=headers,
                               timeout=_DEFAULT_TIMEOUT_SECONDS)
    if resp.status_code != 200 or not isinstance(resp.json_body, dict):
        raise PostizAdapterError(f"postiz list HTTP {resp.status_code}")
    posts = [n for n in (_normalize_postiz_post(p) for p in resp.json_body.get("posts") or [])
             if n is not None]
    _postiz_cache.update({"at": time.monotonic(), "key": key, "posts": posts})
    return list(posts)


def post_release_time(post: dict) -> str:
    """The post's public release time: the LATEST per-platform scheduledFor,
    else the post-level one. Zernio sets the post-level scheduledFor to the
    EARLIEST platform time, and a YouTube cover hold
    (hypogeum.publishing.youtube_cover_hold) moves YouTube's private upload
    ~24h ahead of the real slot — so the post-level value would put the post
    on the wrong calendar day. Without a hold every platform shares one time
    and this equals the post-level value."""
    times = [str(pl.get("scheduledFor")).strip() for pl in post.get("platforms") or []
             if isinstance(pl, dict) and pl.get("scheduledFor")]
    if times:
        return max(times, key=lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")))
    return (post.get("scheduledFor") or "").strip()


def _episode_id_from_media(post: dict) -> Optional[str]:
    """Parse ``ep_<id>`` out of any media URL on the post, or None."""
    media = post.get("mediaItems") or post.get("media") or []
    if not isinstance(media, list):
        return None
    for m in media:
        if not isinstance(m, dict):
            continue
        hit = _EP_ID_IN_URL.search(str(m.get("url") or ""))
        if hit:
            return hit.group(1).lower()
    return None


def _resolve_channel_and_episode(
    conn, post_id: str, title: str, post: Optional[dict] = None,
) -> tuple[Optional[str], Optional[str]]:
    """(channel_id, episode_id) for a Zernio post: the provider_content_
    mappings link first, then the ``ep_<id>`` embedded in the staged media
    URL, then an exact title match against episodes.title, else (None,
    None) — an unattributable post is skipped, never guessed onto a
    channel."""
    row = conn.execute(
        "SELECT channel_id, episode_id FROM provider_content_mappings "
        "WHERE provider_content_id = ? AND channel_id IS NOT NULL LIMIT 1",
        (post_id,),
    ).fetchone()
    if row and row["channel_id"]:
        return row["channel_id"], row["episode_id"]

    media_ep = _episode_id_from_media(post) if post else None
    if media_ep:
        row = conn.execute(
            "SELECT e.episode_id, "
            "  COALESCE(t.channel_id, r.channel_id) AS channel_id "
            "FROM episodes e "
            "LEFT JOIN episode_topics t ON t.episode_id = e.episode_id "
            "LEFT JOIN runs r ON r.episode_id = e.episode_id "
            "WHERE e.episode_id = ? LIMIT 1",
            (media_ep,),
        ).fetchone()
        if row and row["channel_id"]:
            return row["channel_id"], row["episode_id"]

    if title:
        row = conn.execute(
            "SELECT e.episode_id, t.channel_id FROM episodes e "
            "LEFT JOIN episode_topics t ON t.episode_id = e.episode_id "
            "WHERE e.title = ? LIMIT 1",
            (title,),
        ).fetchone()
        if row and row["channel_id"]:
            return row["channel_id"], row["episode_id"]
    return None, None


def sync_zernio_schedule(
    *,
    db_path: Optional[Path] = None,
    window_days_back: int = 3,
    transport=None,
) -> dict:
    """Rebuild channel_publish_slots to mirror Zernio's post schedule.

    Returns {ok, channels, slots_written, slots_removed, unattributed,
    error?}. Never raises.
    """
    out: dict = {"ok": False, "channels": [], "slots_written": 0,
                 "slots_removed": 0, "unattributed": 0}
    try:
        from hypogeum.db import get_connection
        from hypogeum.publishing.zernio import (
            RequestsZernioTransport, _resolve_zernio_credential,
            _ZERNIO_POSTS_URL, _DEFAULT_TIMEOUT_SECONDS,
        )

        # Multi-tenant Phase 5: each workspace's own Zernio account is read
        # with its own key, and its posts may only fill slots of channels
        # that workspace owns (owner_of_post below).
        accounts = []
        try:
            from hypogeum.workspaces import list_workspaces
            _ws_ids = [w["workspace_id"] for w in list_workspaces(db_path)]
        except Exception:  # noqa: BLE001 — install without workspaces
            _ws_ids = []
        for _wid in _ws_ids:
            _c = _resolve_zernio_credential(db_path, workspace_id=_wid)
            if _c:
                accounts.append((_wid, _c))
        if not _ws_ids:
            _c = _resolve_zernio_credential(db_path)
            if _c:
                accounts.append((None, _c))
        if not accounts:
            out["error"] = "no active zernio credential"
            return out
        tp = transport or RequestsZernioTransport()
        posts, owner_of_post = [], {}
        for ws_id, cred in accounts:
            resp = tp.get(
                url=f"{_ZERNIO_POSTS_URL}?limit={_ZERNIO_LIST_LIMIT}",
                headers={"Authorization": f"Bearer {cred}"},
                timeout=_DEFAULT_TIMEOUT_SECONDS,
            )
            if resp.status_code != 200 or not isinstance(resp.json_body, dict):
                out["error"] = f"zernio list HTTP {resp.status_code}"
                return out
            for p in resp.json_body.get("posts", []) or []:
                posts.append(p)
                owner_of_post[id(p)] = ws_id
        del accounts
        # Channels moved to Postiz publish there, so Zernio no longer lists
        # their posts. Without folding Postiz's posts in, those channels'
        # slots would be dropped after the grace window and the
        # one-episode-per-day guards would let a second episode through.
        try:
            _now_pz = datetime.now(timezone.utc)
            for p in _fetch_postiz_posts(
                db_path, floor=_now_pz - timedelta(days=max(0, window_days_back) + 1), now=_now_pz,
            ):
                posts.append(p)
                owner_of_post[id(p)] = None
        except Exception as exc:  # noqa: BLE001 — a Postiz outage must not break the Zernio mirror
            out["postiz_error"] = f"{type(exc).__name__}: {exc}"
        try:
            from hypogeum.workspace_controls import _client_channels
            from hypogeum.workspaces import platform_workspace_id
            _clients = _client_channels(db_path)
            _platform = platform_workspace_id(db_path)
        except Exception:  # noqa: BLE001
            _clients, _platform = {}, None

        def _channel_ok(channel_id, ws_id) -> bool:
            if ws_id is None:            # install without workspaces
                return True
            owner = _clients.get(channel_id, _platform)
            return owner == ws_id

        now = datetime.now(timezone.utc)
        floor = now - timedelta(days=max(0, window_days_back))

        # desired[channel_id] = { "YYYY-MM-DD": (slot_utc_iso, episode_id) }
        desired: dict[str, dict[str, tuple[str, str]]] = {}

        conn = get_connection(db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 20000")
        except Exception:  # noqa: BLE001
            pass
        try:
            for p in posts:
                sf = post_release_time(p)
                if not sf:
                    continue
                try:
                    dt = datetime.fromisoformat(sf.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < floor:
                    continue
                post_id = str(p.get("_id") or "")
                title = (p.get("title") or "").strip()
                channel_id, episode_id = _resolve_channel_and_episode(conn, post_id, title, p)
                if not channel_id or not _channel_ok(channel_id, owner_of_post.get(id(p))):
                    out["unattributed"] += 1
                    continue
                marker_ep = episode_id or f"zernio:{post_id}"
                day = dt.astimezone(timezone.utc).isoformat()[:10]
                # earliest post on a day wins the slot record for that day
                bucket = desired.setdefault(channel_id, {})
                if day not in bucket or dt.astimezone(timezone.utc).isoformat() < bucket[day][0]:
                    bucket[day] = (dt.astimezone(timezone.utc).isoformat(), marker_ep)

            synced_channels = sorted(desired.keys())
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                grace_floor = (now - timedelta(seconds=_LOCAL_SLOT_GRACE_SECONDS)).isoformat()
                for channel_id in synced_channels:
                    existing = conn.execute(
                        "SELECT slot_utc, episode_id, created_at FROM channel_publish_slots WHERE channel_id = ?",
                        (channel_id,),
                    ).fetchall()
                    want_days = set(desired[channel_id].keys())
                    # Keep a local future slot Zernio has not confirmed ONLY
                    # when it is still inside its grace window (a fresh
                    # assignment made moments before its provider call, with
                    # Zernio's list momentarily behind). A slot older than
                    # that whose Zernio post the list does not carry has been
                    # deleted/moved on the provider — drop it rather than let
                    # it haunt the daily-cap guards forever. Its original
                    # created_at is preserved on re-insert so the window can
                    # actually expire (bumping it to now made stale kept rows
                    # immortal — the ep_8154490c Sep-15 ghost, 2026-09-07).
                    keep: list[tuple[str, str, str]] = []
                    for r in existing:
                        s = str(r["slot_utc"])
                        created = str(r["created_at"] or "")
                        if s[:10] not in want_days and s > now.isoformat() and created >= grace_floor:
                            keep.append((s, r["episode_id"], created))
                    removed = len(existing) - len(keep)
                    conn.execute("DELETE FROM channel_publish_slots WHERE channel_id = ?", (channel_id,))
                    zernio_rows = [
                        (slot, ep, now.isoformat()) for slot, ep in desired[channel_id].values()
                    ]
                    for slot, ep, created in zernio_rows + keep:
                        conn.execute(
                            "INSERT INTO channel_publish_slots (channel_id, slot_utc, episode_id, created_at) "
                            "VALUES (?,?,?,?) ON CONFLICT(channel_id, slot_utc) DO UPDATE SET episode_id=excluded.episode_id",
                            (channel_id, slot, ep, created),
                        )
                    out["slots_written"] += len(zernio_rows) + len(keep)
                    out["slots_removed"] += max(0, removed)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()

        out["ok"] = True
        out["channels"] = synced_channels
        return out
    except Exception as exc:  # noqa: BLE001 — no-raise contract
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
