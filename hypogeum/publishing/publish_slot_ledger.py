"""
hypogeum/publishing/publish_slot_ledger.py — space out the publish slots of
a produce-ahead buffer.

A shorts channel releases one episode per day at a fixed wall-clock time
(``deliverable_schedule_policy.main_episode.time_of_day``). The schedule
compiler resolves that purely: "the next occurrence of time_of_day at or
after `now`" — today if still ahead, otherwise tomorrow. It has no idea
another finished episode is already sitting in the buffer waiting for that
same slot.

So when several episodes are produced back-to-back (seeding or maintaining
a 1-2 day buffer), every release-flow compilation independently resolves to
the same "today/tomorrow" slot and they collide — the buffer dumps at once
instead of releasing one per day.

This ledger records the slot each episode was assigned and, for the next
episode, returns an ``effective_now`` moved to the EARLIEST calendar day
that has no recorded slot for that channel — backfilling a gap between now
and the last scheduled day before ever appending a fresh day past the last
one. `compute_next_publish_slot` then resolves that day's ``time_of_day``.
The provider (Zernio ``scheduledFor``) remains the source of truth for the
actual schedule (and ``hypogeum.publishing.zernio_schedule_sync`` mirrors
it back into ``channel_publish_slots`` every worker iteration, so the gap
scan sees provider reality); this only spaces out assignment.

Public API:
    next_free_now(channel_id, base_now, *, db_path=None) -> datetime
    record_slot(channel_id, slot_utc, episode_id, *, db_path=None) -> None
    channel_slots(channel_id, *, db_path=None, upcoming_only=True) -> list[dict]
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def next_free_now(channel_id: str, base_now: datetime, *,
                  db_path: Optional[Path] = None) -> datetime:
    """`base_now`, advanced to the EARLIEST calendar day (UTC) from
    `base_now` forward that has no recorded publish slot for this channel.

    Feed the result to hypogeum.scheduler.compute_next_publish_slot() so a
    buffered episode fills the earliest OPEN day: it backfills a gap
    between now and the last scheduled day (e.g. the channel has Sun/Mon/
    Wed/Fri booked but not Tue — a new episode takes Tue) before it ever
    appends a fresh day past the last one. Days that already carry a slot
    are skipped, so back-to-back buffer releases still land on distinct
    days and never collide.

    `base_now`'s own day, when free, is returned verbatim (keeping the
    intra-day time so compute_next_publish_slot can still choose today vs.
    tomorrow); a later free day is returned at 00:00 UTC so the day's own
    time_of_day resolves cleanly. Never raises — returns `base_now` on any
    problem.
    """
    if base_now.tzinfo is None:
        base_now = base_now.replace(tzinfo=timezone.utc)
    try:
        from hypogeum.db import get_connection
        day0 = base_now.astimezone(timezone.utc).date()
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            rows = conn.execute(
                "SELECT DISTINCT substr(slot_utc, 1, 10) FROM channel_publish_slots "
                "WHERE channel_id = ? AND slot_utc >= ?",
                (channel_id, day0.isoformat()),
            ).fetchall()
        finally:
            conn.close()
        taken = {str(r[0]) for r in rows if r and r[0]}
        if day0.isoformat() not in taken:
            return base_now
        for i in range(1, 400):
            d = day0 + timedelta(days=i)
            if d.isoformat() not in taken:
                return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        pass
    return base_now


def day_is_taken(channel_id: str, slot_utc, episode_id: str, *,
                 db_path: Optional[Path] = None) -> Optional[str]:
    """If another episode of `channel_id` is already assigned a slot on the
    SAME calendar day (UTC) as `slot_utc`, return that episode_id — the
    caller must not schedule a second release for that day. None = the day
    is free (or already this episode's). Never raises → None on error, so a
    counting bug can't block a legitimate release."""
    if isinstance(slot_utc, datetime):
        day = _iso(slot_utc)[:10]
    else:
        day = str(slot_utc)[:10]
    try:
        from hypogeum.db import get_connection
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            row = conn.execute(
                "SELECT episode_id, slot_utc FROM channel_publish_slots "
                "WHERE channel_id = ? AND substr(slot_utc, 1, 10) = ? "
                "AND episode_id != ? LIMIT 1",
                (channel_id, day, episode_id or ""),
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None


def record_slot(channel_id: str, slot_utc, episode_id: str, *,
                db_path: Optional[Path] = None) -> None:
    """Record that `episode_id` has been assigned `slot_utc` for `channel_id`.
    Idempotent on (channel_id, slot_utc). Never raises."""
    if not channel_id or not slot_utc or not episode_id:
        return
    if isinstance(slot_utc, datetime):
        slot_utc = _iso(slot_utc)
    try:
        from hypogeum.db import get_connection
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            conn.execute(
                "INSERT INTO channel_publish_slots (channel_id, slot_utc, episode_id, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(channel_id, slot_utc) DO UPDATE SET "
                "episode_id = excluded.episode_id",
                (channel_id, str(slot_utc), episode_id,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def channel_slots(channel_id: str, *, db_path: Optional[Path] = None,
                  upcoming_only: bool = True) -> list[dict]:
    """Recorded slots for a channel, soonest first. Never raises."""
    try:
        from hypogeum.db import get_connection
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            q = ("SELECT channel_id, slot_utc, episode_id, created_at "
                 "FROM channel_publish_slots WHERE channel_id = ?")
            params: list = [channel_id]
            if upcoming_only:
                q += " AND slot_utc >= ?"
                params.append(datetime.now(timezone.utc).isoformat())
            q += " ORDER BY slot_utc ASC"
            return [dict(r) for r in conn.execute(q, params)]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []
