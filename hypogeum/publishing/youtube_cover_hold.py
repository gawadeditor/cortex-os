"""
hypogeum/publishing/youtube_cover_hold.py — no YouTube Short goes public
without its designed cover.

YouTube only accepts a custom Shorts thumbnail from Studio on a computer, on
an already-uploaded video (Zernio: "Custom thumbnails work on videos only,
not Shorts"); Zernio uploads a scheduled YouTube post only ~14 min before its
slot and YouTube releases it itself via publishAt — far too late to set a
cover first. So for every scheduled Zernio post with a pending YouTube entry
this module:

  1. HOLD — moves only the YouTube entry's own `scheduledFor` to
     `release_at - LEAD` with `platformSpecificData.visibility = "private"`
     (Facebook/Instagram/TikTok keep the original slot and their API covers).
     The result is re-read and verified; if YouTube's entry didn't take the
     change, or any other platform/field drifted, the YouTube entry is put
     back to its original time/visibility and the hold is not recorded.
  2. The video lands on YouTube private; the Content Grid flags it and the
     operator uploads the cover in Studio and presses Done
     (youtube_cover_confirmations).
  3. RELEASE — at `release_at`, a confirmed post is flipped public via
     Zernio's metadata endpoint. An unconfirmed one is marked `late` and
     stays private until Done is pressed, then released on the next sweep.

Public API:
    apply_hold(post_id, channel_id, episode_id=None, *, provider=None, db_path=None, now=None) -> dict
    release_due(*, provider=None, db_path=None, now=None) -> list[dict]
    sweep(*, provider=None, db_path=None, now=None) -> dict
    get_holds(db_path=None) -> dict[post_id, dict]
    start_youtube_cover_hold_loop(db_path=None, *, check_every_s=300) -> threading.Thread
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema
from hypogeum.logger import get_logger

log = get_logger(agent="youtube_cover_hold")

LEAD = timedelta(hours=24)            # private upload this long before the slot
_MIN_UPLOAD_DELAY = timedelta(minutes=30)   # never schedule the private upload sooner than this
_MIN_RELEASE_GAP = timedelta(minutes=45)    # slot closer than this: too late to hold
# Fields that must be identical before/after a hold (anything else drifting
# means the PUT did more than move YouTube — restore and stop).
_ROOT_FIELDS = ("title", "content", "tags", "hashtags", "scheduledFor", "timezone", "visibility")

_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _zernio_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _provider(provider):
    if provider is not None:
        return provider
    from hypogeum.publishing.zernio import ZernioProvider
    return ZernioProvider()


def _entry(post: dict, platform: str) -> Optional[dict]:
    return next((p for p in post.get("platforms") or [] if p.get("platform") == platform), None)


def _comparable_entry(e: dict) -> dict:
    from hypogeum.publishing.zernio import _whitelist_platform_entry_for_put
    return _whitelist_platform_entry_for_put(e)


def _drift(before: dict, after: dict) -> list[str]:
    """Everything that changed other than the YouTube entry."""
    problems = [f"root.{k}" for k in _ROOT_FIELDS if before.get(k) != after.get(k)]
    media = lambda p: [(m.get("url"), m.get("thumbnail")) for m in p.get("mediaItems") or []]
    if media(before) != media(after):
        problems.append("mediaItems")
    for b in before.get("platforms") or []:
        if b.get("platform") == "youtube":
            continue
        a = _entry(after, b.get("platform"))
        if a is None or _comparable_entry(a) != _comparable_entry(b):
            problems.append(f"platform.{b.get('platform')}")
    yb, ya = _entry(before, "youtube") or {}, _entry(after, "youtube") or {}
    psd_b = {k: v for k, v in (yb.get("platformSpecificData") or {}).items() if k != "visibility"}
    psd_a = {k: v for k, v in (ya.get("platformSpecificData") or {}).items() if k != "visibility"}
    if psd_b != psd_a:
        problems.append("youtube.platformSpecificData")
    return problems


def get_holds(db_path=None) -> dict:
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        return {r["post_id"]: dict(r) for r in conn.execute("SELECT * FROM youtube_cover_holds")}
    finally:
        conn.close()


def _confirmed(conn) -> set:
    return {r["post_id"] for r in conn.execute("SELECT post_id FROM youtube_cover_confirmations")}


def apply_hold(post_id: str, channel_id: str, episode_id: Optional[str] = None, *,
               provider=None, db_path=None, now: Optional[datetime] = None) -> dict:
    """Move one scheduled post's YouTube entry to a private upload LEAD before
    its slot, verify, and record the hold. Never raises."""
    now = now or _now()
    z = _provider(provider)
    try:
        post = z.get_post(post_id, db_path=db_path).get("post")
        yt = _entry(post or {}, "youtube")
        if not post or yt is None:
            return {"ok": False, "reason": "no_youtube_entry"}
        if post.get("status") != "scheduled" or yt.get("status") not in (None, "pending"):
            return {"ok": False, "reason": f"not_scheduled (post={post.get('status')}, youtube={yt.get('status')})"}
        release_at = _parse(yt.get("scheduledFor") or post["scheduledFor"])
        if release_at - now < _MIN_RELEASE_GAP:
            return {"ok": False, "reason": "too_late_to_hold"}
        upload_at = max(release_at - LEAD, now + _MIN_UPLOAD_DELAY)
        original_visibility = (yt.get("platformSpecificData") or {}).get("visibility") or "public"

        res = z.update_platform_entry(post_id, "youtube", scheduled_for=_zernio_ts(upload_at),
                                      platform_specific_data={"visibility": "private"},
                                      db_path=db_path)
        if res.get("error"):
            log.error("youtube_cover_hold_put_failed", post_id=post_id, error=str(res["error"])[:300])
            return {"ok": False, "reason": "put_failed", "error": str(res["error"])[:300]}

        after = z.get_post(post_id, db_path=db_path).get("post") or {}
        ya = _entry(after, "youtube") or {}
        yt_ok = (ya.get("scheduledFor") and _parse(ya["scheduledFor"]) == upload_at
                 and (ya.get("platformSpecificData") or {}).get("visibility") == "private")
        drift = _drift(post, after)
        # Zernio sets the post-level scheduledFor to the earliest platform
        # time, i.e. YouTube's new upload time (confirmed live 2026-09-29) --
        # expected, not drift. Readers use post_release_time() instead.
        if ("root.scheduledFor" in drift and after.get("scheduledFor")
                and _parse(after["scheduledFor"]) == upload_at):
            drift.remove("root.scheduledFor")
        if not yt_ok or drift:
            # Put YouTube back exactly as it was; the other drift (if any) is
            # reported for a human -- never guessed at.
            z.update_platform_entry(post_id, "youtube", scheduled_for=_zernio_ts(release_at),
                                    platform_specific_data={"visibility": original_visibility},
                                    db_path=db_path)
            log.error("youtube_cover_hold_verify_failed", post_id=post_id, youtube_ok=bool(yt_ok),
                      drift=drift)
            return {"ok": False, "reason": "verify_failed", "youtube_ok": bool(yt_ok), "drift": drift}

        initialize_schema(db_path)
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO youtube_cover_holds (post_id, channel_id, episode_id, release_at, "
                "upload_at, status, held_at, released_at, last_error) VALUES (?,?,?,?,?, 'held', ?, NULL, NULL)",
                (post_id, channel_id, episode_id, _zernio_ts(release_at), _zernio_ts(upload_at),
                 now.isoformat()))
            conn.commit()
        finally:
            conn.close()
        log.info("youtube_cover_hold_applied", post_id=post_id, channel_id=channel_id,
                 release_at=_zernio_ts(release_at), upload_at=_zernio_ts(upload_at))
        return {"ok": True, "release_at": _zernio_ts(release_at), "upload_at": _zernio_ts(upload_at)}
    except Exception as exc:  # noqa: BLE001
        log.error("youtube_cover_hold_error", post_id=post_id, error=str(exc)[:300])
        return {"ok": False, "reason": "error", "error": str(exc)[:300]}


def release_due(*, provider=None, db_path=None, now: Optional[datetime] = None) -> list:
    """Flip public every held post whose slot has arrived AND whose cover is
    confirmed; mark the unconfirmed ones late. Never raises."""
    now = now or _now()
    out = []
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        confirmed = _confirmed(conn)
        due = [dict(r) for r in conn.execute(
            "SELECT * FROM youtube_cover_holds WHERE status IN ('held','late','release_failed')")
            if _parse(r["release_at"]) <= now]
    finally:
        conn.close()
    if not due:
        return out

    z = None
    for h in due:
        pid = h["post_id"]
        if pid not in confirmed:
            if h["status"] == "held":
                _set_status(db_path, pid, "late")
                log.warn("youtube_cover_hold_late", post_id=pid, release_at=h["release_at"])
            out.append({"post_id": pid, "status": "late"})
            continue
        try:
            z = z or _provider(provider)
            res = z.set_youtube_privacy(pid, "public", db_path=db_path)
            err = res.get("error")
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
        if err:
            _set_status(db_path, pid, "release_failed", error=str(err)[:300])
            log.error("youtube_cover_hold_release_failed", post_id=pid, error=str(err)[:300])
            out.append({"post_id": pid, "status": "release_failed", "error": str(err)[:300]})
        else:
            _set_status(db_path, pid, "released", released_at=now.isoformat())
            log.info("youtube_cover_hold_released", post_id=pid)
            out.append({"post_id": pid, "status": "released"})
    return out


def _set_status(db_path, post_id, status, *, error=None, released_at=None):
    conn = get_connection(db_path)
    try:
        conn.execute("UPDATE youtube_cover_holds SET status=?, last_error=?, "
                     "released_at=COALESCE(?, released_at) WHERE post_id=?",
                     (status, error, released_at, post_id))
        conn.commit()
    finally:
        conn.close()


def _scheduled_youtube_posts(db_path) -> list:
    """(post_id, channel_id, episode_id) for every Zernio post a channel has,
    from the Content Grid's own (cached) view -- vertical covers only."""
    from hypogeum.dashboard_content_grid import get_content_grid
    conn = get_connection(db_path)
    try:
        channels = [r["channel_id"] for r in conn.execute(
            "SELECT DISTINCT channel_id FROM publishing_attempts WHERE channel_id IS NOT NULL")]
    finally:
        conn.close()
    out = []
    for ch in channels:
        for it in get_content_grid(ch, db_path=db_path):
            has_yt = any(p.get("platform") == "youtube" for p in it.get("platforms") or [])
            if it.get("status") == "scheduled" and has_yt and not it.get("is_landscape"):
                out.append((it["post_id"], ch, it.get("episode_id")))
    return out


def sweep(*, provider=None, db_path=None, now: Optional[datetime] = None) -> dict:
    """Hold every not-yet-held scheduled YouTube Short, then release/mark due
    ones. Never raises; one sweep at a time."""
    if not _lock.acquire(blocking=False):
        return {"skipped": "already_running"}
    try:
        held = get_holds(db_path)
        applied = []
        for post_id, ch, ep in _scheduled_youtube_posts(db_path):
            if post_id in held:
                continue
            r = apply_hold(post_id, ch, ep, provider=provider, db_path=db_path, now=now)
            applied.append({"post_id": post_id, **r})
        released = release_due(provider=provider, db_path=db_path, now=now)
        return {"applied": applied, "released": released}
    except Exception as exc:  # noqa: BLE001
        log.error("youtube_cover_hold_sweep_failed", error=str(exc)[:300])
        return {"error": str(exc)[:300]}
    finally:
        _lock.release()


def start_youtube_cover_hold_loop(db_path=None, *, check_every_s: int = 300) -> threading.Thread:
    stop = threading.Event()

    def _loop():
        while not stop.is_set():
            sweep(db_path=db_path)
            stop.wait(check_every_s)

    t = threading.Thread(target=_loop, name="youtube-cover-hold", daemon=True)
    t.stop_event = stop  # type: ignore[attr-defined]
    t.start()
    log.info("youtube_cover_hold_loop_started", check_every_s=check_every_s)
    return t
