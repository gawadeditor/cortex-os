"""
hypogeum/scheduler.py — Schedule Entity and Scheduler Process (P2-03)

Manages run schedules for the factory. The scheduler enqueues run slots
into scheduled_runs without executing episodes or generating content.
Pipeline execution is a separate concern: the pipeline reads pending
scheduled_runs rows independently.

Local-safe design:
  - No pipeline calls.
  - No provider calls.
  - No publishing.
  - No content generation.
  - Pure DB operations only.

Schedule lifecycle:
    active  → paused    (temporarily suspended; resumes on set_active)
    active  → disabled  (permanently off; no further runs enqueued)
    paused  → active    (resume)
    paused  → disabled

Scheduled-run lifecycle:
    pending → triggered  (pipeline claims and starts the run)
    pending → skipped    (missed window; pipeline decided to skip)
    pending → cancelled  (operator cancelled before pipeline picked it up)

Public API:
    create_schedule(...)          -> str           (schedule_id)
    set_schedule_status(...)      -> None
    set_next_run(...)             -> None
    compute_next_run(cadence, dt) -> datetime | None
    compute_next_publish_slot(...) -> datetime | None
    is_scheduled_invocation(...)  -> bool
    tick(db_path, now)            -> list[str]     (scheduled_run_ids created)
    get_pending_runs(db_path)     -> list[dict]
    get_scheduled_run(scheduled_run_id, db_path=None) -> dict | None
    cancel_scheduled_run(...)     -> None
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import (
    get_connection,
    initialize_schema,
    insert_schedule,
    insert_scheduled_run,
    get_due_schedules,
    update_schedule_next_run,
    update_schedule_status,
    update_scheduled_run_status,
)

# Schedules ticked later than this many seconds past their due next_run_at
# are logged as missed/catch-up firings (req 6, Scheduler Integration
# milestone) — observability only, no schema/data change. Nothing is ever
# dropped: get_due_schedules() already picks up any overdue schedule on the
# very next tick regardless of how late it is.
_MISSED_SCHEDULE_GRACE_SECONDS = 300

import os as _os


def _env_int(name: str, default: int, floor: int = 0) -> int:
    """Read an int env var at CALL time (not import time) so a .env loaded
    later by hypogeum.config still takes effect. Module-level names below
    are kept for tests that monkeypatch them."""
    try:
        return max(floor, int(_os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


# Produce-ahead buffer ceiling. A daily-cadence channel keeps roughly this
# many episodes finished (or in flight) ahead of what has published, so a
# failed production day never risks the publish schedule. tick() skips
# creating a scheduled_run for a channel already at/above this depth and
# advances its next_run_at to the next period. Env PRODUCTION_BUFFER_TARGET;
# 0 disables the guard entirely.
_PRODUCTION_BUFFER_TARGET = _env_int("PRODUCTION_BUFFER_TARGET", 2)

# When a channel has fallen BEHIND its produce-ahead target, one
# scheduled_run per daily tick takes `target` days to rebuild the lead —
# leaving the publish schedule exposed the whole time. Set
# PRODUCTION_CATCHUP_MAX > 1 and a behind channel enqueues enough runs to
# close the gap in one tick, capped at this many. Default 1 = one episode
# per tick (unchanged behavior); the factory opts in via .env.
_PRODUCTION_CATCHUP_MAX = _env_int("PRODUCTION_CATCHUP_MAX", 1, floor=1)


def channel_buffer_depth(channel_id: str, conn) -> int:
    """How many episodes for `channel_id` are already produced-but-unpublished
    or in flight toward a publish slot: non-terminal run_queue items +
    pending scheduled_runs + finished runs with no succeeded publication and
    not superseded. Best-effort — returns 0 on any query error so the guard
    fails OPEN (never blocks production because of a counting bug)."""
    try:
        q = int(conn.execute(
            "SELECT COUNT(*) FROM run_queue WHERE channel_id = ? "
            "AND status IN ('queued','claimed')", (channel_id,),
        ).fetchone()[0] or 0)
        try:
            sr = int(conn.execute(
                "SELECT COUNT(*) FROM scheduled_runs WHERE channel_id = ? "
                "AND status = 'pending'", (channel_id,),
            ).fetchone()[0] or 0)
        except Exception:  # noqa: BLE001
            sr = 0
        try:
            done_unpub = int(conn.execute(
                "SELECT COUNT(DISTINCT r.episode_id) FROM runs r "
                "WHERE r.channel_id = ? AND r.status IN ('complete','completed') "
                "AND r.episode_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM publication_intents pi "
                "  WHERE pi.episode_id = r.episode_id AND pi.status = 'succeeded') "
                "AND NOT EXISTS (SELECT 1 FROM episodes e "
                "  WHERE e.episode_id = r.episode_id AND e.status = 'superseded')",
                (channel_id,),
            ).fetchone()[0] or 0)
        except Exception:  # noqa: BLE001
            done_unpub = 0
        return q + sr + done_unpub
    except Exception:  # noqa: BLE001
        return 0

_WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_dt_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp to an offset-aware UTC datetime.

    Tolerates a naive string (no offset) — e.g. one written by a raw
    `UPDATE ... SET next_run_at = datetime('now')` — by assuming UTC, so a
    single hand-edited schedule row can never crash tick() (and with it the
    whole worker loop) on an offset-naive/-aware subtraction.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Cadence computation — pure, no I/O
# ---------------------------------------------------------------------------

def compute_next_run(cadence: str, from_dt: datetime) -> Optional[datetime]:
    """
    Return the next run datetime after from_dt for the given cadence.

    Args:
        cadence: "daily" | "weekly" | "manual"
        from_dt: Reference datetime (timezone-aware UTC).

    Returns:
        Next run datetime (UTC), or None for "manual" cadence.

    Raises:
        ValueError: If cadence is not a known value.
    """
    if cadence == "daily":
        return from_dt + timedelta(days=1)
    if cadence == "weekly":
        return from_dt + timedelta(weeks=1)
    if cadence == "manual":
        return None
    raise ValueError(f"Unknown cadence: {cadence!r}")


def compute_next_publish_slot(
    publishing_schedule: dict,
    tz_name: str,
    from_dt: datetime,
) -> Optional[datetime]:
    """
    Return the next occurrence of publishing_schedule's day_of_week +
    time_of_day at or after from_dt, interpreted in the channel's own
    timezone (ChannelOperationsProfile.timezone — the same field
    hypogeum.backward_planner.build_backward_plan() already uses).

    Pure and deterministic: never reads the wall clock, always returns the
    same result for the same inputs. Generalizes
    hypogeum.dashboard_control.next_thursday_publish_slot()'s "next weekday
    at fixed time" pattern to any channel-configured day/time/timezone.

    Two schedule shapes are accepted:
      - Weekly: {"day_of_week": "thursday", "time_of_day": "18:00"} — the
        next occurrence of that weekday at that time (the original behavior).
      - Daily:  {"time_of_day": "15:00"} with day_of_week absent, or
        day_of_week set to "daily"/"every"/"any"/"*" — the next occurrence
        of that time today (if still ahead) or tomorrow. This is what a
        shorts-only channel (production_cadence.videos_per_week == 0) needs:
        one release per day at a fixed wall-clock time, which the weekly
        7-slot recurrence cannot express.

    Args:
        publishing_schedule: see the two shapes above.
        tz_name:             IANA timezone name (ChannelOperationsProfile.timezone).
        from_dt:              Timezone-aware reference datetime.

    Returns:
        A timezone-aware UTC datetime, or None if time_of_day is missing or
        unparseable, day_of_week is present-but-unrecognized, or tz_name is
        invalid — never fabricates a value; the caller
        (queue.consume_scheduled_runs) treats None as "nothing to persist,"
        matching worker_integration.py's existing fail-closed behavior for
        scheduled runs with no target_publish_at.
    """
    day_raw = publishing_schedule.get("day_of_week")
    time_raw = publishing_schedule.get("time_of_day")
    if not time_raw:
        return None

    _daily_tokens = {"daily", "every", "everyday", "every_day", "any", "*"}
    is_daily = (not day_raw) or (str(day_raw).strip().lower() in _daily_tokens)

    weekday = None
    if not is_daily:
        weekday = _WEEKDAY_NAMES.get(str(day_raw).strip().lower())
        if weekday is None:
            return None

    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(time_raw)):
        return None
    hour, minute = (int(p) for p in str(time_raw).split(":"))

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        return None

    local_from = from_dt.astimezone(tz)
    if is_daily:
        candidate = local_from.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_from:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    days_ahead = (weekday - local_from.weekday()) % 7
    candidate = (local_from + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    if candidate <= local_from:
        candidate += timedelta(weeks=1)

    return candidate.astimezone(timezone.utc)


def is_scheduled_invocation(claimed_row: dict) -> bool:
    """
    True when a claimed run_queue row represents a scheduler-driven
    invocation (hypogeum.worker_integration.InvocationMode.SCHEDULED), as
    opposed to a manually/directly enqueued one.

    Single source of truth for this decision: hypogeum.queue.process_next()
    calls this instead of checking claimed_row.get("scheduled_run_id")
    inline, so that "what makes something a scheduled invocation" lives in
    exactly one place — future-proof if a second scheduled-execution source
    is ever introduced. Currently keyed on scheduled_run_id being set.
    """
    return bool(claimed_row.get("scheduled_run_id"))


# ---------------------------------------------------------------------------
# Schedule management
# ---------------------------------------------------------------------------

def create_schedule(
    db_path: Optional[Path] = None,
    scope: str = "factory",
    channel_id: Optional[str] = None,
    cadence: str = "manual",
    next_run_at: Optional[datetime] = None,
    max_runs_per_day: int = 1,
    label: Optional[str] = None,
) -> str:
    """
    Create a new schedule and return its schedule_id.

    Args:
        db_path:          Override DB path (for tests).
        scope:            "factory" | "channel"
        channel_id:       Required when scope="channel".
        cadence:          "daily" | "weekly" | "manual"
        next_run_at:      When the first run should be enqueued (UTC datetime).
                          None for manual cadence.
        max_runs_per_day: Safety ceiling on daily run count.
        label:            Human-readable name.

    Returns:
        New schedule_id (UUID4 string).
    """
    if scope == "channel" and not channel_id:
        raise ValueError("channel_id is required when scope='channel'")

    initialize_schema(db_path)
    schedule_id = _new_id()
    now = _iso(_now_utc())

    conn = get_connection(db_path)
    try:
        insert_schedule(
            conn,
            schedule_id=schedule_id,
            scope=scope,
            channel_id=channel_id,
            cadence=cadence,
            status="active",
            next_run_at=_iso(next_run_at) if next_run_at is not None else None,
            last_run_at=None,
            max_runs_per_day=max_runs_per_day,
            label=label,
            created_at=now,
            updated_at=now,
        )
        conn.commit()
    finally:
        conn.close()

    return schedule_id


def set_schedule_status(
    schedule_id: str,
    status: str,
    db_path: Optional[Path] = None,
) -> None:
    """
    Transition a schedule's status to 'active', 'paused', or 'disabled'.

    Args:
        schedule_id: Target schedule.
        status:      New status value.
        db_path:     Override DB path (for tests).
    """
    if status not in ("active", "paused", "disabled"):
        raise ValueError(f"Invalid status: {status!r}")

    conn = get_connection(db_path)
    try:
        update_schedule_status(conn, schedule_id, status, updated_at=_iso(_now_utc()))
        conn.commit()
    finally:
        conn.close()


def set_next_run(
    schedule_id: str,
    next_run_at: Optional[datetime],
    db_path: Optional[Path] = None,
) -> None:
    """
    Manually set next_run_at for a schedule (used for manual cadence).

    Args:
        schedule_id: Target schedule.
        next_run_at: UTC datetime for the next run, or None to clear.
        db_path:     Override DB path (for tests).
    """
    now = _now_utc()
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE schedules SET next_run_at = ?, updated_at = ? WHERE schedule_id = ?",
            (
                _iso(next_run_at) if next_run_at is not None else None,
                _iso(now),
                schedule_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scheduler tick — core process, no side effects beyond DB writes
# ---------------------------------------------------------------------------

def tick(
    db_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> list[str]:
    """
    Single scheduler pass: enqueue pending runs for all due active schedules.

    For each active schedule whose next_run_at <= now:
      1. Insert a 'pending' scheduled_run row.
      2. Advance schedule.next_run_at to the next interval.
      3. Update schedule.last_run_at.

    Never executes the pipeline. Never publishes. Never generates content.

    max_runs_per_day is stored on the schedule entity but NOT enforced by
    tick() in P2-03. For daily/weekly cadences, advancing next_run_at
    after each tick naturally limits to one run per cadence period.
    Enforcement of the daily ceiling across manual triggers is deferred
    to a future milestone.

    Args:
        db_path: Override DB path (for tests).
        now:     Override current time (for tests). Defaults to UTC now.

    Returns:
        List of scheduled_run_ids created this tick (one per due schedule).
    """
    if now is None:
        now = _now_utc()

    created_ids: list[str] = []
    now_iso = _iso(now)

    # Resolve at call time — env wins, module globals are the fallback (and
    # what tests monkeypatch). This makes a .env loaded by hypogeum.config
    # after this module was imported still take effect in the worker.
    buffer_target = _env_int("PRODUCTION_BUFFER_TARGET", _PRODUCTION_BUFFER_TARGET)
    catchup_max   = _env_int("PRODUCTION_CATCHUP_MAX", _PRODUCTION_CATCHUP_MAX, floor=1)

    conn = get_connection(db_path)
    try:
        due = get_due_schedules(conn, as_of=now_iso)

        for row in due:
            cadence = row["cadence"]
            next_dt = compute_next_run(cadence, now)

            # Produce-ahead buffer guard: if this channel already has enough
            # finished / in-flight episodes ahead of what has published, skip
            # producing today — just roll next_run_at forward. The buffer
            # covers this slot. (0 disables; a channel with no channel_id or
            # a non-recurring cadence is never guarded.)
            channel_id_g = row["channel_id"] if "channel_id" in row.keys() else None

            # How many production runs to create this tick. Default 1 (one
            # episode per due schedule). When the buffer guard is active and
            # this channel has fallen BEHIND its target, produce enough to
            # close the gap in one go (capped) so the publish schedule is
            # never left exposed for days.
            n_runs = 1
            if buffer_target and channel_id_g and next_dt is not None:
                depth = channel_buffer_depth(channel_id_g, conn)
                if depth >= buffer_target:
                    from hypogeum.logger import get_logger
                    get_logger(agent="scheduler").info(
                        "scheduled_tick_skipped_buffer_full",
                        schedule_id=row["schedule_id"], channel_id=channel_id_g,
                        buffer_depth=depth, buffer_target=buffer_target,
                        next_run_at=_iso(next_dt),
                    )
                    update_schedule_next_run(
                        conn, schedule_id=row["schedule_id"],
                        next_run_at=_iso(next_dt), last_run_at=now_iso,
                        updated_at=now_iso,
                    )
                    continue
                gap = buffer_target - depth
                n_runs = min(max(1, gap), catchup_max)
                if n_runs > 1:
                    from hypogeum.logger import get_logger
                    get_logger(agent="scheduler").info(
                        "scheduled_tick_catchup",
                        schedule_id=row["schedule_id"], channel_id=channel_id_g,
                        buffer_depth=depth, buffer_target=buffer_target,
                        runs_created=n_runs,
                    )

            due_at = row["next_run_at"]
            if due_at:
                delay_seconds = (now - _parse_dt_utc(due_at)).total_seconds()
                if delay_seconds > _MISSED_SCHEDULE_GRACE_SECONDS:
                    from hypogeum.logger import get_logger
                    get_logger(agent="scheduler").warn(
                        "scheduled_tick_missed_window",
                        schedule_id=row["schedule_id"],
                        channel_id=row["channel_id"],
                        due_at=due_at,
                        fired_at=now_iso,
                        delay_seconds=int(delay_seconds),
                    )

            for _ in range(n_runs):
                scheduled_run_id = _new_id()
                insert_scheduled_run(
                    conn,
                    scheduled_run_id=scheduled_run_id,
                    schedule_id=row["schedule_id"],
                    channel_id=row["channel_id"],
                    episode_id=None,
                    scheduled_for=now_iso,
                    status="pending",
                    created_at=now_iso,
                )
                created_ids.append(scheduled_run_id)

            update_schedule_next_run(
                conn,
                schedule_id=row["schedule_id"],
                next_run_at=_iso(next_dt) if next_dt is not None else None,
                last_run_at=now_iso,
                updated_at=now_iso,
            )

        # Reactive buffer top-up (2026-09-04). The block above only re-checks
        # a channel's buffer at its own next_run_at — a schedule that isn't
        # due yet is invisible to it. So an episode invalidated mid-day
        # (e.g. superseded as unpublishable) drops the buffer below target
        # and NOTHING notices until tomorrow's tick, even though the whole
        # point of the buffer/catch-up guard is "never leave the channel
        # short." This pass re-checks every active channel schedule NOT
        # already handled above, regardless of next_run_at, and tops the
        # buffer up immediately — without touching next_run_at/last_run_at,
        # so the channel's own cadence timer is untouched; this is a
        # same-day supplement, not a new cycle. Self-limiting: each
        # scheduled_run created here immediately counts toward
        # channel_buffer_depth, so a channel that is topped up this tick
        # reads depth >= target on the very next tick and stops firing.
        if buffer_target:
            handled_schedule_ids = {row["schedule_id"] for row in due}
            other_active = conn.execute(
                "SELECT * FROM schedules WHERE status = 'active' AND channel_id IS NOT NULL"
            ).fetchall()
            for row in other_active:
                if row["schedule_id"] in handled_schedule_ids:
                    continue
                channel_id_r = row["channel_id"]
                depth = channel_buffer_depth(channel_id_r, conn)
                if depth >= buffer_target:
                    continue
                gap = buffer_target - depth
                n_runs = min(max(1, gap), catchup_max)
                from hypogeum.logger import get_logger
                get_logger(agent="scheduler").info(
                    "scheduled_tick_reactive_topup",
                    schedule_id=row["schedule_id"], channel_id=channel_id_r,
                    buffer_depth=depth, buffer_target=buffer_target,
                    runs_created=n_runs,
                )
                for _ in range(n_runs):
                    scheduled_run_id = _new_id()
                    insert_scheduled_run(
                        conn,
                        scheduled_run_id=scheduled_run_id,
                        schedule_id=row["schedule_id"],
                        channel_id=channel_id_r,
                        episode_id=None,
                        scheduled_for=now_iso,
                        status="pending",
                        created_at=now_iso,
                    )
                    created_ids.append(scheduled_run_id)

        conn.commit()
    finally:
        conn.close()

    return created_ids


# ---------------------------------------------------------------------------
# Pending run queries
# ---------------------------------------------------------------------------

def get_pending_runs(db_path: Optional[Path] = None) -> list[dict]:
    """
    Return all scheduled_runs rows with status='pending', newest first.

    Args:
        db_path: Override DB path (for tests).

    Returns:
        List of dicts with keys: scheduled_run_id, schedule_id, channel_id,
        episode_id, scheduled_for, status, triggered_at, run_id, created_at.
    """
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """
            SELECT * FROM scheduled_runs
            WHERE status = 'pending'
            ORDER BY scheduled_for DESC
            """
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_scheduled_run(
    scheduled_run_id: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """
    Return a single scheduled_runs row as a dict, or None if not found.

    Read-only lookup by primary key — no ordering, no candidate selection,
    no positional assumption. Used by hypogeum.queue.process_next() to
    anchor claim-time target_publish_at derivation to scheduled_for (the
    timestamp tick() recorded once, when this run was decided due, and
    which is never mutated afterward — update_scheduled_run_status() only
    ever touches status/triggered_at/run_id), instead of the live wall
    clock at claim time. This keeps the derived publish slot identical
    regardless of how long the run_queue item sits before being claimed.

    Args:
        scheduled_run_id: Target scheduled run.
        db_path:          Override DB path (for tests).
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM scheduled_runs WHERE scheduled_run_id = ?",
            (scheduled_run_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def cancel_scheduled_run(
    scheduled_run_id: str,
    db_path: Optional[Path] = None,
) -> None:
    """
    Cancel a pending scheduled run.

    Only 'pending' rows can be cancelled. Already-triggered or skipped
    rows are left unchanged (no-op, no error raised).

    Args:
        scheduled_run_id: Target scheduled run.
        db_path:          Override DB path (for tests).
    """
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            UPDATE scheduled_runs
            SET status = 'cancelled'
            WHERE scheduled_run_id = ? AND status = 'pending'
            """,
            (scheduled_run_id,),
        )
        conn.commit()
    finally:
        conn.close()
