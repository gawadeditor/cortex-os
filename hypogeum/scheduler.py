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
    tick(db_path, now)            -> list[str]     (scheduled_run_ids created)
    get_pending_runs(db_path)     -> list[dict]
    cancel_scheduled_run(...)     -> None
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


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

    conn = get_connection(db_path)
    try:
        due = get_due_schedules(conn, as_of=now_iso)

        for row in due:
            scheduled_run_id = _new_id()
            cadence = row["cadence"]
            next_dt = compute_next_run(cadence, now)

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

            update_schedule_next_run(
                conn,
                schedule_id=row["schedule_id"],
                next_run_at=_iso(next_dt) if next_dt is not None else None,
                last_run_at=now_iso,
                updated_at=now_iso,
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
