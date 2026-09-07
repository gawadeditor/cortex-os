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

# Every staged Zernio media file is named ``..._<deliverable>_<episode_id>.mp4``
# (see hypogeum.publishing.zernio_media_staging) — episode_id is ``ep_`` +
# 8 hex. This is present on 100% of real posts and survives a title edit,
# so it is a far more reliable attribution signal than an exact title match.
_EP_ID_IN_URL = re.compile(r"(ep_[0-9a-fA-F]{8})")


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

        cred = _resolve_zernio_credential(db_path)
        if cred is None:
            out["error"] = "no active zernio credential"
            return out
        tp = transport or RequestsZernioTransport()
        resp = tp.get(
            url=f"{_ZERNIO_POSTS_URL}?limit={_ZERNIO_LIST_LIMIT}",
            headers={"Authorization": f"Bearer {cred}"},
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
        del cred
        if resp.status_code != 200 or not isinstance(resp.json_body, dict):
            out["error"] = f"zernio list HTTP {resp.status_code}"
            return out
        posts = resp.json_body.get("posts", []) or []

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
                sf = (p.get("scheduledFor") or "").strip()
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
                if not channel_id:
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
                for channel_id in synced_channels:
                    existing = conn.execute(
                        "SELECT slot_utc, episode_id FROM channel_publish_slots WHERE channel_id = ?",
                        (channel_id,),
                    ).fetchall()
                    want_days = set(desired[channel_id].keys())
                    # Keep a local future slot only when Zernio has NO post
                    # for that channel on that day yet (a fresh assignment
                    # made moments before its provider call).
                    keep: list[tuple[str, str]] = []
                    for r in existing:
                        s = str(r["slot_utc"])
                        if s[:10] not in want_days and s > now.isoformat():
                            keep.append((s, r["episode_id"]))
                    removed = len(existing) - len(keep)
                    conn.execute("DELETE FROM channel_publish_slots WHERE channel_id = ?", (channel_id,))
                    rows = [(slot, ep) for slot, ep in desired[channel_id].values()] + keep
                    for slot, ep in rows:
                        conn.execute(
                            "INSERT INTO channel_publish_slots (channel_id, slot_utc, episode_id, created_at) "
                            "VALUES (?,?,?,?) ON CONFLICT(channel_id, slot_utc) DO UPDATE SET episode_id=excluded.episode_id",
                            (channel_id, slot, ep, now.isoformat()),
                        )
                    out["slots_written"] += len(rows)
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
