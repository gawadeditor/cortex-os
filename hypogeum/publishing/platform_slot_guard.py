"""
hypogeum/publishing/platform_slot_guard.py — keep a per-platform override
slot from stacking two episodes on the same platform on the same day.

hypogeum.publishing.publish_slot_ledger spaces out each episode's BASE slot
(one episode per channel per day). A platform_overrides entry written by
hypogeum.publishing.evidence_based_scheduling (e.g. hypogeum YouTube =
"Saturday 15:00 UTC", 2026-10-07) is resolved independently as "the next
Saturday" for every episode — so two episodes finished in the same week both
landed on the same Saturday 15:00 on YouTube.

    resolve_platform_collisions(channel_id, episode_id, schedule_result, *,
                                db_path=None, now=None) -> ScheduleCompilationResult

For each deliverable's platform override: keep it when that platform has no
other episode of this channel on that UTC day; otherwise use the
deliverable's own base slot, or the next base-policy slot after it, that is
free on that platform. The preferred (evidence-based) slot still wins
whenever it is free. Never raises — on any error the schedule is returned
unchanged.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="platform_slot_guard")

_MAX_CANDIDATES = 21


def _day(dt) -> str:
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()[:10]
    return str(dt)[:10]


def taken_platform_days(channel_id: str, platform: str, episode_id: str, *,
                        db_path: Optional[Path] = None, now: Optional[datetime] = None) -> set:
    """UTC days on which ANOTHER episode of this channel is (or was just)
    released on `platform`. YouTube's own time is in youtube_cover_holds
    (it can differ from the post's base slot); every other booking uses the
    episode's base slot in channel_publish_slots."""
    from hypogeum.db import get_connection

    since = ((now or datetime.now(timezone.utc)) - timedelta(days=1)).isoformat()[:10]
    conn = get_connection(Path(db_path) if db_path else None)
    try:
        days: set = set()
        held_eps: set = set()
        if platform == "youtube":
            try:
                for r in conn.execute(
                    "SELECT episode_id, release_at FROM youtube_cover_holds WHERE channel_id=? "
                    "AND status != 'opted_out'", (channel_id,)):
                    held_eps.add(r[0])
                    if r[0] != episode_id and str(r[1])[:10] >= since:
                        days.add(str(r[1])[:10])
            except Exception:  # noqa: BLE001 — table absent on a fresh DB
                pass
        for r in conn.execute(
            "SELECT episode_id, slot_utc FROM channel_publish_slots WHERE channel_id=? AND episode_id != ?",
            (channel_id, episode_id or "")):
            if r[0] in held_eps:
                continue
            if str(r[1])[:10] >= since:
                days.add(str(r[1])[:10])
        return days
    finally:
        conn.close()


def _base_schedule(channel_id: str, deliverable_id: str, db_path) -> Optional[tuple]:
    from hypogeum.channel_config import get_channel_operations_profile
    profile = get_channel_operations_profile(channel_id, db_path=Path(db_path) if db_path else None)
    entry = ((getattr(profile, "deliverable_schedule_policy", None) or {}).get(deliverable_id)) if profile else None
    if not entry:
        return None
    sched = {k: entry.get(k) for k in ("day_of_week", "time_of_day", "weekday_time_of_day",
                                        "default_time_of_day", "weekly_slots", "timezone")}
    return sched, (entry.get("timezone") or profile.timezone)


def _free_slot(preferred: datetime, base: datetime, taken: set, base_sched, now: datetime) -> Optional[datetime]:
    """preferred if its day is free, else the base slot, else the next
    base-policy slots after it — the first whose day is free."""
    from hypogeum.scheduler import compute_next_publish_slot

    if _day(preferred) not in taken:
        return preferred
    cursor = base
    for _ in range(_MAX_CANDIDATES):
        if cursor is None:
            return None
        if cursor >= now and _day(cursor) not in taken:
            return cursor
        if base_sched is None:
            return None
        cursor = compute_next_publish_slot(base_sched[0], base_sched[1], cursor)
    return None


def resolve_platform_collisions(channel_id: str, episode_id: str, schedule_result, *,
                                db_path: Optional[Path] = None, now: Optional[datetime] = None):
    try:
        plan = getattr(schedule_result, "schedule_plan", None)
        if plan is None or not plan.entries:
            return schedule_result
        now = now or datetime.now(timezone.utc)
        new_entries = {}
        changed = False
        for deliverable_id, entry in plan.entries.items():
            overrides = dict(entry.platform_scheduled_for_by_platform or {})
            if not overrides or entry.publish_now:
                new_entries[deliverable_id] = entry
                continue
            base_sched = _base_schedule(channel_id, deliverable_id, db_path)
            for platform, preferred in list(overrides.items()):
                taken = taken_platform_days(channel_id, platform, episode_id, db_path=db_path, now=now)
                chosen = _free_slot(preferred, entry.scheduled_for, taken, base_sched, now)
                if chosen is None:
                    log.warn("platform_slot_no_free_day", channel_id=channel_id, episode_id=episode_id,
                             platform=platform, preferred=str(preferred))
                    continue
                if chosen != preferred:
                    log.info("platform_slot_moved_off_taken_day", channel_id=channel_id,
                             episode_id=episode_id, platform=platform,
                             preferred=str(preferred), chosen=str(chosen))
                    overrides[platform] = chosen
                    changed = True
            new_entries[deliverable_id] = replace(entry, platform_scheduled_for_by_platform=overrides)
        if not changed:
            return schedule_result
        return replace(schedule_result, schedule_plan=replace(plan, entries=new_entries))
    except Exception as exc:  # noqa: BLE001
        log.warn("platform_slot_guard_failed", channel_id=channel_id, episode_id=episode_id, error=str(exc)[:200])
        return schedule_result
