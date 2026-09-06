"""
tests/test_scheduler.py — P2-03: Schedule Entity and Scheduler Process

Coverage:
  - schedules table CRUD (insert, status transitions, next_run advance)
  - scheduled_runs table CRUD (insert, status transitions)
  - compute_next_run: daily, weekly, manual cadences
  - get_due_schedules: only returns active schedules due now
  - tick: enqueues pending runs and advances next_run_at
  - Factory-scoped vs channel-scoped schedules
  - Paused / disabled schedules are not ticked
  - cancel_scheduled_run: pending→cancelled, triggered unaffected
  - get_pending_runs: returns only pending rows
  - No publishing, no content generation, no provider calls, no network I/O

Safety invariants checked:
  - tick() never calls the pipeline
  - tick() never touches publishing modules
  - No real HTTP/SMTP calls (socket-level ban via monkeypatch)
"""

from __future__ import annotations

import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hypogeum.db import (
    get_connection,
    initialize_schema,
    insert_scheduled_run,
    update_scheduled_run_status,
)
from hypogeum.scheduler import (
    cancel_scheduled_run,
    compute_next_publish_slot,
    compute_next_run,
    create_schedule,
    get_pending_runs,
    get_scheduled_run,
    is_scheduled_invocation,
    set_next_run,
    set_schedule_status,
    tick,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rows(db_path: Path, table: str) -> list[dict]:
    conn = get_connection(db_path)
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
    finally:
        conn.close()


def _schedule_row(db_path: Path, schedule_id: str) -> dict:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM schedules WHERE schedule_id = ?", (schedule_id,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _sched_run_row(db_path: Path, sr_id: str) -> dict:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM scheduled_runs WHERE scheduled_run_id = ?", (sr_id,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixture: isolated DB
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path) -> Path:
    p = tmp_path / "test.db"
    initialize_schema(p)
    return p


# ---------------------------------------------------------------------------
# Safety fixture: block real network calls
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    original_connect = socket.socket.connect

    def _block(self, address):
        raise AssertionError(
            f"scheduler tests must not open network connections (attempted: {address})"
        )

    monkeypatch.setattr(socket.socket, "connect", _block)
    yield


# ===========================================================================
# compute_next_run — pure function, no I/O
# ===========================================================================

class TestComputeNextRun:

    def test_daily(self):
        dt = _utc(2026, 6, 15, 9, 0, 0)
        result = compute_next_run("daily", dt)
        assert result == _utc(2026, 6, 16, 9, 0, 0)

    def test_weekly(self):
        dt = _utc(2026, 6, 15, 9, 0, 0)
        result = compute_next_run("weekly", dt)
        assert result == _utc(2026, 6, 22, 9, 0, 0)

    def test_manual_returns_none(self):
        dt = _utc(2026, 6, 15, 9, 0, 0)
        assert compute_next_run("manual", dt) is None

    def test_unknown_cadence_raises(self):
        with pytest.raises(ValueError, match="Unknown cadence"):
            compute_next_run("hourly", _now())

    def test_daily_preserves_time_of_day(self):
        dt = _utc(2026, 1, 31, 14, 30, 0)
        result = compute_next_run("daily", dt)
        assert result.hour == 14
        assert result.minute == 30

    def test_weekly_from_end_of_month(self):
        dt = _utc(2026, 1, 28, 0, 0, 0)
        result = compute_next_run("weekly", dt)
        assert result == _utc(2026, 2, 4, 0, 0, 0)


# ===========================================================================
# compute_next_publish_slot — pure function, no I/O (Scheduler Integration)
# ===========================================================================

class TestComputeNextPublishSlot:

    def test_returns_next_utc_occurrence_same_timezone(self):
        # 2026-06-15 is a Monday. Next Thursday 18:00 UTC is 2026-06-18.
        from_dt = _utc(2026, 6, 15, 9, 0, 0)
        result = compute_next_publish_slot(
            {"day_of_week": "thursday", "time_of_day": "18:00"}, "UTC", from_dt,
        )
        assert result == _utc(2026, 6, 18, 18, 0, 0)

    def test_case_insensitive_day_name(self):
        from_dt = _utc(2026, 6, 15, 9, 0, 0)
        result = compute_next_publish_slot(
            {"day_of_week": "THURSDAY", "time_of_day": "18:00"}, "UTC", from_dt,
        )
        assert result == _utc(2026, 6, 18, 18, 0, 0)

    def test_today_before_slot_time_returns_today(self):
        # 2026-06-18 is a Thursday, 09:00 UTC — before 18:00, so today's slot applies.
        from_dt = _utc(2026, 6, 18, 9, 0, 0)
        result = compute_next_publish_slot(
            {"day_of_week": "thursday", "time_of_day": "18:00"}, "UTC", from_dt,
        )
        assert result == _utc(2026, 6, 18, 18, 0, 0)

    def test_today_after_slot_time_rolls_to_next_week(self):
        # 2026-06-18 is a Thursday, 20:00 UTC — after 18:00, so next week's slot applies.
        from_dt = _utc(2026, 6, 18, 20, 0, 0)
        result = compute_next_publish_slot(
            {"day_of_week": "thursday", "time_of_day": "18:00"}, "UTC", from_dt,
        )
        assert result == _utc(2026, 6, 25, 18, 0, 0)

    def test_missing_day_of_week_is_daily_slot(self):
        # No day_of_week + a time_of_day == a daily release schedule: the
        # next occurrence of that time today (if still ahead) or tomorrow.
        # 2026-06-18 09:00 UTC, slot 18:00 -> today.
        result = compute_next_publish_slot(
            {"time_of_day": "18:00"}, "UTC", _utc(2026, 6, 18, 9, 0, 0),
        )
        assert result == _utc(2026, 6, 18, 18, 0, 0)

    def test_daily_slot_after_time_rolls_to_tomorrow(self):
        result = compute_next_publish_slot(
            {"time_of_day": "18:00"}, "UTC", _utc(2026, 6, 18, 20, 0, 0),
        )
        assert result == _utc(2026, 6, 19, 18, 0, 0)

    def test_daily_token_day_of_week_is_daily_slot(self):
        for token in ("daily", "every", "any", "*"):
            result = compute_next_publish_slot(
                {"day_of_week": token, "time_of_day": "16:00"}, "UTC",
                _utc(2026, 6, 18, 9, 0, 0),
            )
            assert result == _utc(2026, 6, 18, 16, 0, 0), token

    def test_daily_slot_non_utc_timezone(self):
        # 19:00 Asia/Dubai (UTC+4) == 15:00 UTC.
        result = compute_next_publish_slot(
            {"time_of_day": "19:00"}, "Asia/Dubai", _utc(2026, 6, 18, 9, 0, 0),
        )
        assert result == _utc(2026, 6, 18, 15, 0, 0)

    def test_missing_time_of_day_returns_none(self):
        result = compute_next_publish_slot({"day_of_week": "thursday"}, "UTC", _now())
        assert result is None

    def test_empty_publishing_schedule_returns_none(self):
        assert compute_next_publish_slot({}, "UTC", _now()) is None


class TestComputeNextPublishSlotPerWeekday:
    """
    Per-weekday shape: {"weekday_time_of_day": {...}, "default_time_of_day":
    "..."} — daily cadence (one slot every day, like the Daily shape), but
    each named weekday gets its own time instead of one fixed clock time
    for the whole week. Added 2026-09-04 for a channel's real Zernio
    best-time-to-post data (distinct Monday/Wednesday peaks, everything
    else falling back to a single default time).

    2026-06-15 is a Monday (same reference week the weekly-shape tests
    above use), so 06-16=Tue, 06-17=Wed, 06-18=Thu.
    """

    _SCHEDULE = {
        "weekday_time_of_day": {"monday": "10:00", "wednesday": "18:00"},
        "default_time_of_day": "16:00",
    }

    def test_named_weekday_before_its_own_time_uses_that_time_today(self):
        result = compute_next_publish_slot(self._SCHEDULE, "UTC", _utc(2026, 6, 15, 9, 0, 0))
        assert result == _utc(2026, 6, 15, 10, 0, 0)

    def test_named_weekday_after_its_own_time_rolls_to_next_day_default(self):
        # Monday 11:00 -- 10:00 already passed; Tuesday is unnamed, uses default.
        result = compute_next_publish_slot(self._SCHEDULE, "UTC", _utc(2026, 6, 15, 11, 0, 0))
        assert result == _utc(2026, 6, 16, 16, 0, 0)

    def test_unnamed_weekday_uses_default_time(self):
        # Tuesday, before the default -- today at the default time.
        result = compute_next_publish_slot(self._SCHEDULE, "UTC", _utc(2026, 6, 16, 9, 0, 0))
        assert result == _utc(2026, 6, 16, 16, 0, 0)

    def test_second_named_weekday_uses_its_own_time(self):
        result = compute_next_publish_slot(self._SCHEDULE, "UTC", _utc(2026, 6, 17, 9, 0, 0))
        assert result == _utc(2026, 6, 17, 18, 0, 0)

    def test_case_insensitive_weekday_key(self):
        schedule = {"weekday_time_of_day": {"MONDAY": "10:00"}, "default_time_of_day": "16:00"}
        result = compute_next_publish_slot(schedule, "UTC", _utc(2026, 6, 15, 9, 0, 0))
        assert result == _utc(2026, 6, 15, 10, 0, 0)

    def test_non_utc_timezone(self):
        # 10:00 Asia/Dubai (UTC+4) == 06:00 UTC.
        result = compute_next_publish_slot(self._SCHEDULE, "Asia/Dubai", _utc(2026, 6, 15, 3, 0, 0))
        assert result == _utc(2026, 6, 15, 6, 0, 0)

    def test_takes_priority_over_flat_time_of_day(self):
        schedule = {
            "weekday_time_of_day": {"monday": "10:00"}, "default_time_of_day": "16:00",
            "time_of_day": "23:00",  # must be ignored -- the per-weekday shape wins
        }
        result = compute_next_publish_slot(schedule, "UTC", _utc(2026, 6, 15, 9, 0, 0))
        assert result == _utc(2026, 6, 15, 10, 0, 0)

    def test_missing_default_time_of_day_returns_none(self):
        schedule = {"weekday_time_of_day": {"monday": "10:00"}}
        assert compute_next_publish_slot(schedule, "UTC", _now()) is None

    def test_unrecognized_weekday_name_returns_none(self):
        schedule = {"weekday_time_of_day": {"funday": "10:00"}, "default_time_of_day": "16:00"}
        assert compute_next_publish_slot(schedule, "UTC", _now()) is None

    def test_unparseable_named_time_returns_none(self):
        schedule = {"weekday_time_of_day": {"monday": "25:99"}, "default_time_of_day": "16:00"}
        assert compute_next_publish_slot(schedule, "UTC", _now()) is None

    def test_unparseable_default_time_returns_none(self):
        schedule = {"weekday_time_of_day": {"monday": "10:00"}, "default_time_of_day": "not-a-time"}
        assert compute_next_publish_slot(schedule, "UTC", _now()) is None

    def test_invalid_timezone_returns_none(self):
        result = compute_next_publish_slot(self._SCHEDULE, "Not/A_Real_Zone", _now())
        assert result is None


class TestComputeNextPublishSlotWeeklySlots:
    """Multi-slot weekly shape (2026-09-06): publish ONLY at a fixed set of
    (weekday, time) points — a real N×/week cadence. 2026-06-15 is a Monday;
    06-17 Wed, 06-19 Fri, 06-20 Sat."""

    _SLOTS = {
        "weekly_slots": [
            {"day_of_week": "monday", "time_of_day": "10:00"},
            {"day_of_week": "wednesday", "time_of_day": "18:00"},
            {"day_of_week": "friday", "time_of_day": "15:00"},
        ],
        "timezone": "UTC",
    }

    def test_next_slot_is_today_when_still_ahead(self):
        r = compute_next_publish_slot(self._SLOTS, "Asia/Dubai", _utc(2026, 6, 15, 9, 0, 0))
        assert r == _utc(2026, 6, 15, 10, 0, 0)  # policy timezone UTC overrides channel tz

    def test_skips_to_next_listed_slot_after_todays_passed(self):
        # Monday 11:00 — Mon 10:00 gone, next is Wed 18:00
        r = compute_next_publish_slot(self._SLOTS, "UTC", _utc(2026, 6, 15, 11, 0, 0))
        assert r == _utc(2026, 6, 17, 18, 0, 0)

    def test_unlisted_day_is_skipped_entirely(self):
        # Tuesday — no Tuesday slot; next is Wed 18:00
        r = compute_next_publish_slot(self._SLOTS, "UTC", _utc(2026, 6, 16, 12, 0, 0))
        assert r == _utc(2026, 6, 17, 18, 0, 0)

    def test_wraps_to_next_week(self):
        # Saturday — nothing left this week; next Monday 10:00
        r = compute_next_publish_slot(self._SLOTS, "UTC", _utc(2026, 6, 20, 12, 0, 0))
        assert r == _utc(2026, 6, 22, 10, 0, 0)

    def test_integer_day_of_week_accepted(self):
        s = {"weekly_slots": [{"day_of_week": 2, "time_of_day": "18:00"}]}
        r = compute_next_publish_slot(s, "UTC", _utc(2026, 6, 15, 9, 0, 0))
        assert r == _utc(2026, 6, 17, 18, 0, 0)

    def test_takes_priority_over_weekday_time_of_day_and_flat(self):
        s = {**self._SLOTS, "weekday_time_of_day": {"monday": "23:00"}, "time_of_day": "22:00"}
        r = compute_next_publish_slot(s, "UTC", _utc(2026, 6, 15, 9, 0, 0))
        assert r == _utc(2026, 6, 15, 10, 0, 0)

    def test_bad_time_returns_none(self):
        s = {"weekly_slots": [{"day_of_week": "monday", "time_of_day": "99:99"}]}
        assert compute_next_publish_slot(s, "UTC", _now()) is None

    def test_bad_day_returns_none(self):
        s = {"weekly_slots": [{"day_of_week": "someday", "time_of_day": "10:00"}]}
        assert compute_next_publish_slot(s, "UTC", _now()) is None

    def test_empty_list_falls_through(self):
        s = {"weekly_slots": [], "day_of_week": "thursday", "time_of_day": "18:00"}
        r = compute_next_publish_slot(s, "UTC", _utc(2026, 6, 15, 9, 0, 0))
        assert r == _utc(2026, 6, 18, 18, 0, 0)  # falls through to the weekly shape

    def test_invalid_day_name_returns_none(self):
        result = compute_next_publish_slot(
            {"day_of_week": "someday", "time_of_day": "18:00"}, "UTC", _now(),
        )
        assert result is None

    def test_invalid_time_format_returns_none(self):
        result = compute_next_publish_slot(
            {"day_of_week": "thursday", "time_of_day": "6pm"}, "UTC", _now(),
        )
        assert result is None

    def test_invalid_timezone_returns_none(self):
        result = compute_next_publish_slot(
            {"day_of_week": "thursday", "time_of_day": "18:00"}, "Not/AZone", _now(),
        )
        assert result is None

    def test_deterministic_for_fixed_from_dt(self):
        from_dt = _utc(2026, 6, 15, 9, 0, 0)
        schedule = {"day_of_week": "thursday", "time_of_day": "18:00"}
        a = compute_next_publish_slot(schedule, "UTC", from_dt)
        b = compute_next_publish_slot(schedule, "UTC", from_dt)
        assert a == b

    def test_non_utc_timezone_converts_correctly(self):
        # Thursday 18:00 America/New_York (EDT, UTC-4) == Thursday 22:00 UTC.
        from_dt = _utc(2026, 6, 15, 9, 0, 0)
        result = compute_next_publish_slot(
            {"day_of_week": "thursday", "time_of_day": "18:00"},
            "America/New_York", from_dt,
        )
        assert result == _utc(2026, 6, 18, 22, 0, 0)

    def test_result_is_timezone_aware_utc(self):
        result = compute_next_publish_slot(
            {"day_of_week": "thursday", "time_of_day": "18:00"}, "UTC", _now(),
        )
        assert result.tzinfo is not None
        assert result.utcoffset().total_seconds() == 0


# ===========================================================================
# is_scheduled_invocation — pure function, no I/O (Scheduler Integration
# race-condition correction: single source of truth for "is this scheduled",
# so process_next() doesn't hardcode a scheduled_run_id check inline)
# ===========================================================================

class TestIsScheduledInvocation:

    def test_true_when_scheduled_run_id_present(self):
        assert is_scheduled_invocation({"scheduled_run_id": "sr_1"}) is True

    def test_false_when_scheduled_run_id_none(self):
        assert is_scheduled_invocation({"scheduled_run_id": None}) is False

    def test_false_when_scheduled_run_id_absent(self):
        assert is_scheduled_invocation({}) is False

    def test_false_when_scheduled_run_id_empty_string(self):
        assert is_scheduled_invocation({"scheduled_run_id": ""}) is False


# ===========================================================================
# DB schema — tables exist
# ===========================================================================

class TestSchemaTablesExist:

    def test_schedules_table_exists(self, db_path):
        conn = get_connection(db_path)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "schedules" in tables

    def test_scheduled_runs_table_exists(self, db_path):
        conn = get_connection(db_path)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "scheduled_runs" in tables

    def test_schedules_columns(self, db_path):
        conn = get_connection(db_path)
        try:
            cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(schedules)").fetchall()
            }
        finally:
            conn.close()
        expected = {
            "schedule_id", "scope", "channel_id", "cadence", "status",
            "next_run_at", "last_run_at", "max_runs_per_day", "label",
            "created_at", "updated_at",
        }
        assert expected <= cols

    def test_scheduled_runs_columns(self, db_path):
        conn = get_connection(db_path)
        try:
            cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(scheduled_runs)").fetchall()
            }
        finally:
            conn.close()
        expected = {
            "scheduled_run_id", "schedule_id", "channel_id", "episode_id",
            "scheduled_for", "status", "triggered_at", "run_id", "created_at",
        }
        assert expected <= cols


# ===========================================================================
# create_schedule
# ===========================================================================

class TestCreateSchedule:

    def test_factory_scope_default(self, db_path):
        sid = create_schedule(db_path=db_path)
        row = _schedule_row(db_path, sid)
        assert row["scope"] == "factory"
        assert row["cadence"] == "manual"
        assert row["status"] == "active"
        assert row["channel_id"] is None

    def test_channel_scope(self, db_path):
        sid = create_schedule(db_path=db_path, scope="channel", channel_id="ch_001")
        row = _schedule_row(db_path, sid)
        assert row["scope"] == "channel"
        assert row["channel_id"] == "ch_001"

    def test_channel_scope_requires_channel_id(self, db_path):
        with pytest.raises(ValueError, match="channel_id is required"):
            create_schedule(db_path=db_path, scope="channel")

    def test_daily_cadence_with_next_run(self, db_path):
        t = _utc(2026, 6, 20, 9, 0, 0)
        sid = create_schedule(db_path=db_path, cadence="daily", next_run_at=t)
        row = _schedule_row(db_path, sid)
        assert row["cadence"] == "daily"
        assert "2026-06-20" in row["next_run_at"]

    def test_label_stored(self, db_path):
        sid = create_schedule(db_path=db_path, label="Hypogeum weekly")
        row = _schedule_row(db_path, sid)
        assert row["label"] == "Hypogeum weekly"

    def test_max_runs_per_day_stored(self, db_path):
        sid = create_schedule(db_path=db_path, max_runs_per_day=3)
        row = _schedule_row(db_path, sid)
        assert row["max_runs_per_day"] == 3

    def test_returns_uuid_string(self, db_path):
        sid = create_schedule(db_path=db_path)
        # Must be a valid UUID4
        parsed = uuid.UUID(sid)
        assert parsed.version == 4

    def test_multiple_schedules_distinct_ids(self, db_path):
        ids = {create_schedule(db_path=db_path) for _ in range(5)}
        assert len(ids) == 5


# ===========================================================================
# set_schedule_status
# ===========================================================================

class TestSetScheduleStatus:

    def test_active_to_paused(self, db_path):
        sid = create_schedule(db_path=db_path)
        set_schedule_status(sid, "paused", db_path=db_path)
        assert _schedule_row(db_path, sid)["status"] == "paused"

    def test_active_to_disabled(self, db_path):
        sid = create_schedule(db_path=db_path)
        set_schedule_status(sid, "disabled", db_path=db_path)
        assert _schedule_row(db_path, sid)["status"] == "disabled"

    def test_paused_to_active(self, db_path):
        sid = create_schedule(db_path=db_path)
        set_schedule_status(sid, "paused", db_path=db_path)
        set_schedule_status(sid, "active", db_path=db_path)
        assert _schedule_row(db_path, sid)["status"] == "active"

    def test_paused_to_disabled(self, db_path):
        sid = create_schedule(db_path=db_path)
        set_schedule_status(sid, "paused", db_path=db_path)
        set_schedule_status(sid, "disabled", db_path=db_path)
        assert _schedule_row(db_path, sid)["status"] == "disabled"

    def test_invalid_status_raises(self, db_path):
        sid = create_schedule(db_path=db_path)
        with pytest.raises(ValueError, match="Invalid status"):
            set_schedule_status(sid, "unknown", db_path=db_path)


# ===========================================================================
# set_next_run
# ===========================================================================

class TestSetNextRun:

    def test_set_next_run_updates_db(self, db_path):
        sid = create_schedule(db_path=db_path, cadence="manual")
        t = _utc(2026, 7, 1, 8, 0, 0)
        set_next_run(sid, t, db_path=db_path)
        row = _schedule_row(db_path, sid)
        assert "2026-07-01" in row["next_run_at"]

    def test_set_next_run_to_none_clears(self, db_path):
        t = _utc(2026, 7, 1, 8, 0, 0)
        sid = create_schedule(db_path=db_path, cadence="manual", next_run_at=t)
        set_next_run(sid, None, db_path=db_path)
        assert _schedule_row(db_path, sid)["next_run_at"] is None

    def test_manual_with_explicit_next_run_is_due(self, db_path):
        # Manual schedule with an explicit past next_run_at MUST be picked up by tick.
        # The docstring says NULL schedules are excluded — not manual cadence itself.
        past = _now() - timedelta(minutes=1)
        sid = create_schedule(db_path=db_path, cadence="manual", next_run_at=past)
        result = tick(db_path=db_path, now=_now())
        assert len(result) == 1
        sr = _sched_run_row(db_path, result[0])
        assert sr["schedule_id"] == sid
        # After the tick, next_run_at is cleared (manual returns None from compute_next_run)
        assert _schedule_row(db_path, sid)["next_run_at"] is None


# ===========================================================================
# tick — core scheduler pass
# ===========================================================================

class TestTick:

    def test_tick_no_due_schedules_returns_empty(self, db_path):
        # Schedule with next_run_at in the future
        future = _now() + timedelta(hours=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=future)
        result = tick(db_path=db_path, now=_now())
        assert result == []

    def test_tick_one_due_schedule(self, db_path):
        past = _now() - timedelta(minutes=5)
        sid = create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        result = tick(db_path=db_path, now=_now())
        assert len(result) == 1
        # Verify scheduled_run was created
        sr = _sched_run_row(db_path, result[0])
        assert sr["schedule_id"] == sid
        assert sr["status"] == "pending"

    def test_tick_advances_next_run_daily(self, db_path):
        t = _now() - timedelta(minutes=1)
        sid = create_schedule(db_path=db_path, cadence="daily", next_run_at=t)
        now = _now()
        tick(db_path=db_path, now=now)
        row = _schedule_row(db_path, sid)
        # next_run_at should be approximately now + 1 day
        next_dt = datetime.fromisoformat(row["next_run_at"])
        expected = now + timedelta(days=1)
        delta = abs((next_dt - expected).total_seconds())
        assert delta < 5

    def test_tick_advances_next_run_weekly(self, db_path):
        t = _now() - timedelta(minutes=1)
        sid = create_schedule(db_path=db_path, cadence="weekly", next_run_at=t)
        now = _now()
        tick(db_path=db_path, now=now)
        row = _schedule_row(db_path, sid)
        next_dt = datetime.fromisoformat(row["next_run_at"])
        expected = now + timedelta(weeks=1)
        delta = abs((next_dt - expected).total_seconds())
        assert delta < 5

    def test_tick_manual_cadence_clears_next_run(self, db_path):
        # Manual schedule manually triggered once: next_run_at should be cleared
        t = _now() - timedelta(minutes=1)
        sid = create_schedule(db_path=db_path, cadence="manual", next_run_at=t)
        tick(db_path=db_path, now=_now())
        row = _schedule_row(db_path, sid)
        assert row["next_run_at"] is None

    def test_tick_sets_last_run_at(self, db_path):
        t = _now() - timedelta(minutes=1)
        sid = create_schedule(db_path=db_path, cadence="daily", next_run_at=t)
        tick(db_path=db_path, now=_now())
        row = _schedule_row(db_path, sid)
        assert row["last_run_at"] is not None

    def test_tick_multiple_due_schedules(self, db_path):
        past = _now() - timedelta(minutes=5)
        for _ in range(3):
            create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        result = tick(db_path=db_path, now=_now())
        assert len(result) == 3

    def test_tick_paused_schedule_not_ticked(self, db_path):
        past = _now() - timedelta(minutes=5)
        sid = create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        set_schedule_status(sid, "paused", db_path=db_path)
        result = tick(db_path=db_path, now=_now())
        assert result == []

    def test_tick_disabled_schedule_not_ticked(self, db_path):
        past = _now() - timedelta(minutes=5)
        sid = create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        set_schedule_status(sid, "disabled", db_path=db_path)
        result = tick(db_path=db_path, now=_now())
        assert result == []

    def test_tick_manual_with_null_next_run_not_ticked(self, db_path):
        # Manual schedule with no next_run_at set — must NOT be enqueued
        create_schedule(db_path=db_path, cadence="manual")
        result = tick(db_path=db_path, now=_now())
        assert result == []

    def test_tick_does_not_execute_pipeline(self, db_path, monkeypatch):
        """tick() must never call run_pipeline or any pipeline entry point."""
        import hypogeum.pipeline as _pipeline
        monkeypatch.setattr(
            _pipeline, "run_pipeline",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("tick() must not call run_pipeline")
            ),
            raising=False,
        )
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        result = tick(db_path=db_path, now=_now())
        assert len(result) == 1  # enqueued, not executed

    def test_tick_scheduled_run_inherits_channel_id(self, db_path):
        past = _now() - timedelta(minutes=1)
        sid = create_schedule(
            db_path=db_path,
            scope="channel",
            channel_id="ch_042",
            cadence="daily",
            next_run_at=past,
        )
        result = tick(db_path=db_path, now=_now())
        sr = _sched_run_row(db_path, result[0])
        assert sr["channel_id"] == "ch_042"

    def test_tick_factory_scope_channel_id_null(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        result = tick(db_path=db_path, now=_now())
        sr = _sched_run_row(db_path, result[0])
        assert sr["channel_id"] is None

    def test_tick_returns_distinct_ids(self, db_path):
        past = _now() - timedelta(minutes=1)
        for _ in range(4):
            create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        ids = tick(db_path=db_path, now=_now())
        assert len(ids) == len(set(ids))

    def test_tick_idempotent_same_now(self, db_path):
        # Second tick with the same 'now' must not enqueue a duplicate.
        # After the first tick, next_run_at is advanced by one cadence period,
        # so the schedule is no longer due at the original 'now'.
        past = _now() - timedelta(minutes=5)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        now = _now()
        first = tick(db_path=db_path, now=now)
        second = tick(db_path=db_path, now=now)
        assert len(first) == 1
        assert len(second) == 0  # already advanced; no duplicate enqueue

    def test_max_runs_per_day_stored_only_p2_03(self, db_path):
        # max_runs_per_day is persisted but NOT enforced by tick() in P2-03.
        # Enforcement against the daily ceiling for manual triggers is deferred.
        sid = create_schedule(db_path=db_path, cadence="manual", max_runs_per_day=1)
        row = _schedule_row(db_path, sid)
        assert row["max_runs_per_day"] == 1
        # tick() does not consult max_runs_per_day; this test documents the deferred scope.

    def test_tick_does_not_call_publishing(self, db_path):
        """scheduler.py must not import or call publishing modules or run_pipeline."""
        import ast, inspect
        import hypogeum.scheduler as sched_mod
        tree = ast.parse(inspect.getsource(sched_mod))
        # Collect all Import / ImportFrom module names
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for name in imported:
            assert "publishing" not in name, (
                f"scheduler.py imports a publishing module: {name}"
            )
        # Verify run_pipeline is not called (no Name node with that id)
        called = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        assert "run_pipeline" not in called, "scheduler.py must not call run_pipeline"

    def test_tick_missed_window_still_enqueues_and_logs(self, db_path, monkeypatch):
        """A schedule ticked well past its due next_run_at is still enqueued
        (nothing dropped) and logs a scheduled_tick_missed_window event."""
        logged = []

        class _StubLogger:
            def warn(self, event, **fields):
                logged.append((event, fields))

        monkeypatch.setattr(
            "hypogeum.logger.get_logger", lambda **kw: _StubLogger(),
        )

        due = _now() - timedelta(minutes=10)  # well past the 300s grace period
        sid = create_schedule(db_path=db_path, cadence="daily", next_run_at=due)
        now = _now()
        result = tick(db_path=db_path, now=now)

        assert len(result) == 1  # still enqueued — nothing dropped
        events = [e for e, _ in logged]
        assert "scheduled_tick_missed_window" in events

    def test_tick_on_time_does_not_log_missed_window(self, db_path, monkeypatch):
        logged = []

        class _StubLogger:
            def warn(self, event, **fields):
                logged.append((event, fields))

        monkeypatch.setattr(
            "hypogeum.logger.get_logger", lambda **kw: _StubLogger(),
        )

        due = _now() - timedelta(seconds=5)  # well within the 300s grace period
        create_schedule(db_path=db_path, cadence="daily", next_run_at=due)
        tick(db_path=db_path, now=_now())

        events = [e for e, _ in logged]
        assert "scheduled_tick_missed_window" not in events


# ===========================================================================
# get_pending_runs
# ===========================================================================

class TestGetPendingRuns:

    def test_empty_when_no_runs(self, db_path):
        assert get_pending_runs(db_path=db_path) == []

    def test_returns_pending_rows(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())
        pending = get_pending_runs(db_path=db_path)
        assert len(pending) == 1
        assert pending[0]["status"] == "pending"

    def test_does_not_return_triggered_rows(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        sr_ids = tick(db_path=db_path, now=_now())
        conn = get_connection(db_path)
        try:
            update_scheduled_run_status(conn, sr_ids[0], "triggered",
                                        triggered_at=_now().isoformat())
            conn.commit()
        finally:
            conn.close()
        pending = get_pending_runs(db_path=db_path)
        assert len(pending) == 0  # triggered row must not appear in pending results

    def test_returns_list_of_dicts(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        tick(db_path=db_path, now=_now())
        pending = get_pending_runs(db_path=db_path)
        assert isinstance(pending, list)
        assert isinstance(pending[0], dict)
        assert "scheduled_run_id" in pending[0]


# ===========================================================================
# get_scheduled_run — read-only lookup by primary key (Scheduler Integration
# race-condition correction: anchors claim-time target_publish_at
# derivation to the immutable scheduled_for this returns)
# ===========================================================================

class TestGetScheduledRun:

    def test_returns_none_for_unknown_id(self, db_path):
        assert get_scheduled_run("does_not_exist", db_path=db_path) is None

    def test_returns_row_by_id(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        sr_ids = tick(db_path=db_path, now=_now())
        row = get_scheduled_run(sr_ids[0], db_path=db_path)
        assert row is not None
        assert row["scheduled_run_id"] == sr_ids[0]
        assert row["scheduled_for"] is not None

    def test_scheduled_for_unaffected_by_status_transitions(self, db_path):
        """scheduled_for must be immutable — update_scheduled_run_status()
        only ever touches status/triggered_at/run_id."""
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        sr_ids = tick(db_path=db_path, now=_now())
        original_scheduled_for = get_scheduled_run(sr_ids[0], db_path=db_path)["scheduled_for"]

        conn = get_connection(db_path)
        try:
            update_scheduled_run_status(conn, sr_ids[0], "triggered",
                                        triggered_at=_now().isoformat())
            conn.commit()
        finally:
            conn.close()

        assert get_scheduled_run(sr_ids[0], db_path=db_path)["scheduled_for"] == original_scheduled_for


# ===========================================================================
# cancel_scheduled_run
# ===========================================================================

class TestCancelScheduledRun:

    def test_pending_becomes_cancelled(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        sr_ids = tick(db_path=db_path, now=_now())
        cancel_scheduled_run(sr_ids[0], db_path=db_path)
        sr = _sched_run_row(db_path, sr_ids[0])
        assert sr["status"] == "cancelled"

    def test_cancel_triggered_is_noop(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        sr_ids = tick(db_path=db_path, now=_now())
        conn = get_connection(db_path)
        try:
            update_scheduled_run_status(conn, sr_ids[0], "triggered",
                                        triggered_at=_now().isoformat())
            conn.commit()
        finally:
            conn.close()
        # Cancel on an already-triggered row should not raise and should not change status
        cancel_scheduled_run(sr_ids[0], db_path=db_path)
        sr = _sched_run_row(db_path, sr_ids[0])
        assert sr["status"] == "triggered"

    def test_cancel_does_not_affect_other_runs(self, db_path):
        past = _now() - timedelta(minutes=1)
        for _ in range(3):
            create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        sr_ids = tick(db_path=db_path, now=_now())
        cancel_scheduled_run(sr_ids[0], db_path=db_path)
        pending = get_pending_runs(db_path=db_path)
        assert len(pending) == 2


# ===========================================================================
# Scope: factory vs channel
# ===========================================================================

class TestScheduleScope:

    def test_factory_and_channel_schedules_coexist(self, db_path):
        f_id = create_schedule(db_path=db_path, scope="factory", cadence="daily",
                               next_run_at=_now() - timedelta(minutes=1))
        c_id = create_schedule(db_path=db_path, scope="channel", channel_id="ch_A",
                               cadence="weekly",
                               next_run_at=_now() - timedelta(minutes=1))
        result = tick(db_path=db_path, now=_now())
        assert len(result) == 2

    def test_channel_scope_distinct_channels_both_ticked(self, db_path):
        for ch in ("ch_A", "ch_B", "ch_C"):
            create_schedule(db_path=db_path, scope="channel", channel_id=ch,
                            cadence="daily",
                            next_run_at=_now() - timedelta(minutes=1))
        result = tick(db_path=db_path, now=_now())
        assert len(result) == 3
        channel_ids = {_sched_run_row(db_path, r)["channel_id"] for r in result}
        assert channel_ids == {"ch_A", "ch_B", "ch_C"}

    def test_pausing_one_channel_does_not_affect_others(self, db_path):
        past = _now() - timedelta(minutes=1)
        c1 = create_schedule(db_path=db_path, scope="channel", channel_id="ch_1",
                             cadence="daily", next_run_at=past)
        create_schedule(db_path=db_path, scope="channel", channel_id="ch_2",
                        cadence="daily", next_run_at=past)
        set_schedule_status(c1, "paused", db_path=db_path)
        result = tick(db_path=db_path, now=_now())
        assert len(result) == 1
        sr = _sched_run_row(db_path, result[0])
        assert sr["channel_id"] == "ch_2"


# ===========================================================================
# update_scheduled_run_status DB helper
# ===========================================================================

class TestUpdateScheduledRunStatus:

    def test_pending_to_triggered(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        sr_ids = tick(db_path=db_path, now=_now())
        conn = get_connection(db_path)
        try:
            update_scheduled_run_status(
                conn, sr_ids[0], "triggered",
                triggered_at=_now().isoformat(),
                run_id="run-abc-123",
            )
            conn.commit()
        finally:
            conn.close()
        sr = _sched_run_row(db_path, sr_ids[0])
        assert sr["status"] == "triggered"
        assert sr["run_id"] == "run-abc-123"
        assert sr["triggered_at"] is not None

    def test_pending_to_skipped(self, db_path):
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, cadence="daily", next_run_at=past)
        sr_ids = tick(db_path=db_path, now=_now())
        conn = get_connection(db_path)
        try:
            update_scheduled_run_status(conn, sr_ids[0], "skipped")
            conn.commit()
        finally:
            conn.close()
        assert _sched_run_row(db_path, sr_ids[0])["status"] == "skipped"


# ===========================================================================
# Migration safety — existing DB gains new tables
# ===========================================================================

class TestSchemaMigration:

    def test_initialize_schema_idempotent(self, tmp_path):
        p = tmp_path / "mig.db"
        initialize_schema(p)
        initialize_schema(p)  # second call must not raise
        conn = get_connection(p)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "schedules" in tables
        assert "scheduled_runs" in tables

    def test_existing_db_gets_new_tables(self, tmp_path):
        p = tmp_path / "old.db"
        # Simulate a pre-P2-03 DB: create schema, then drop the new tables
        initialize_schema(p)
        conn = get_connection(p)
        try:
            conn.execute("DROP TABLE IF EXISTS scheduled_runs")
            conn.execute("DROP TABLE IF EXISTS schedules")
            conn.commit()
        finally:
            conn.close()
        # Re-run initialize_schema (startup path)
        initialize_schema(p)
        conn = get_connection(p)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "schedules" in tables
        assert "scheduled_runs" in tables

    def test_existing_rows_survive_reinitialize(self, tmp_path):
        # Existing schedule rows must not be dropped or truncated by a second
        # initialize_schema() call (the startup idempotency path).
        p = tmp_path / "survive.db"
        initialize_schema(p)
        sid = create_schedule(db_path=p, label="survivor", cadence="weekly")
        initialize_schema(p)  # simulate startup re-run
        row = _schedule_row(p, sid)
        assert row["label"] == "survivor"
        assert row["cadence"] == "weekly"

    def test_existing_scheduled_run_rows_survive_reinitialize(self, tmp_path):
        # Existing scheduled_run rows must survive repeated initialize_schema() calls.
        p = tmp_path / "survive_sr.db"
        initialize_schema(p)
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        sid = create_schedule(db_path=p, cadence="daily", next_run_at=past)
        sr_ids = tick(db_path=p, now=datetime.now(timezone.utc))
        assert len(sr_ids) == 1
        initialize_schema(p)  # simulate startup re-run
        sr = _sched_run_row(p, sr_ids[0])
        assert sr["schedule_id"] == sid
        assert sr["status"] == "pending"


class TestProduceAheadBufferGuard:
    """tick() skips producing for a channel already at the buffer ceiling."""

    def _seed_run_queue(self, db_path, channel_id, n, status="queued"):
        from hypogeum.queue import enqueue
        for _ in range(n):
            enqueue(channel_id=channel_id, priority=0, source="manual", db_path=db_path)

    def test_guard_skips_when_buffer_full_but_advances_next_run(self, db_path, monkeypatch):
        import hypogeum.scheduler as sch
        monkeypatch.setattr(sch, "_PRODUCTION_BUFFER_TARGET", 2)
        past = _now() - timedelta(minutes=1)
        sid = create_schedule(db_path=db_path, scope="channel", channel_id="ch_buf",
                              cadence="daily", next_run_at=past)
        self._seed_run_queue(db_path, "ch_buf", 2)          # depth 2 == target
        now = _now()
        result = tick(db_path=db_path, now=now)
        assert result == []                                  # nothing produced
        row = _schedule_row(db_path, sid)
        next_dt = datetime.fromisoformat(row["next_run_at"])
        assert abs((next_dt - (now + timedelta(days=1))).total_seconds()) < 5  # rolled forward

    def test_guard_lets_it_through_when_buffer_has_room(self, db_path, monkeypatch):
        import hypogeum.scheduler as sch
        monkeypatch.setattr(sch, "_PRODUCTION_BUFFER_TARGET", 2)
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, scope="channel", channel_id="ch_room",
                        cadence="daily", next_run_at=past)
        self._seed_run_queue(db_path, "ch_room", 1)          # depth 1 < target
        result = tick(db_path=db_path, now=_now())
        assert len(result) == 1

    def test_catchup_produces_multiple_when_behind(self, db_path, monkeypatch):
        """With PRODUCTION_CATCHUP_MAX > 1, a channel behind its target
        produces enough runs in one tick to close the gap."""
        import hypogeum.scheduler as sch
        monkeypatch.setattr(sch, "_PRODUCTION_BUFFER_TARGET", 2)
        monkeypatch.setattr(sch, "_PRODUCTION_CATCHUP_MAX", 2)
        past = _now() - timedelta(minutes=1)
        sid = create_schedule(db_path=db_path, scope="channel", channel_id="ch_behind",
                              cadence="daily", next_run_at=past)
        # depth 0 → gap 2 → 2 runs
        result = tick(db_path=db_path, now=_now())
        assert len(result) == 2
        assert {_sched_run_row(db_path, r)["channel_id"] for r in result} == {"ch_behind"}
        # schedule still advanced exactly once
        row = _schedule_row(db_path, sid)
        assert row["next_run_at"] is not None

    def test_catchup_capped_at_max(self, db_path, monkeypatch):
        import hypogeum.scheduler as sch
        monkeypatch.setattr(sch, "_PRODUCTION_BUFFER_TARGET", 5)
        monkeypatch.setattr(sch, "_PRODUCTION_CATCHUP_MAX", 2)
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, scope="channel", channel_id="ch_way_behind",
                        cadence="daily", next_run_at=past)
        result = tick(db_path=db_path, now=_now())   # gap 5, capped at 2
        assert len(result) == 2

    def test_guard_disabled_with_target_zero(self, db_path, monkeypatch):
        import hypogeum.scheduler as sch
        monkeypatch.setattr(sch, "_PRODUCTION_BUFFER_TARGET", 0)
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, scope="channel", channel_id="ch_off",
                        cadence="daily", next_run_at=past)
        self._seed_run_queue(db_path, "ch_off", 5)
        result = tick(db_path=db_path, now=_now())
        assert len(result) == 1                               # fires regardless

    def test_guard_ignores_other_channels(self, db_path, monkeypatch):
        import hypogeum.scheduler as sch
        monkeypatch.setattr(sch, "_PRODUCTION_BUFFER_TARGET", 2)
        past = _now() - timedelta(minutes=1)
        create_schedule(db_path=db_path, scope="channel", channel_id="ch_a",
                        cadence="daily", next_run_at=past)
        self._seed_run_queue(db_path, "ch_b", 5)              # buffer on a DIFFERENT channel
        result = tick(db_path=db_path, now=_now())
        assert len(result) == 1

    def test_reactive_topup_fires_for_a_not_yet_due_schedule(self, db_path, monkeypatch):
        """A schedule that already ticked today and rolled next_run_at
        forward (e.g. it saw the buffer full) must still get topped up the
        same day if the buffer later drops below target — e.g. a 'safety'
        episode turns out unpublishable and is superseded. Regression for
        the exact gap: nothing re-checked the buffer until tomorrow."""
        import hypogeum.scheduler as sch
        monkeypatch.setattr(sch, "_PRODUCTION_BUFFER_TARGET", 2)
        monkeypatch.setattr(sch, "_PRODUCTION_CATCHUP_MAX", 2)
        future = _now() + timedelta(hours=20)   # not due — already ticked today
        sid = create_schedule(db_path=db_path, scope="channel", channel_id="ch_topup",
                              cadence="daily", next_run_at=future)
        # buffer depth 0 (nothing queued) — below target of 2
        result = tick(db_path=db_path, now=_now())
        assert len(result) == 2
        assert {_sched_run_row(db_path, r)["channel_id"] for r in result} == {"ch_topup"}
        # next_run_at is untouched — this was a same-day supplement, not a new cycle
        row = _schedule_row(db_path, sid)
        assert row["next_run_at"] == future.isoformat()

    def test_reactive_topup_self_limits_once_buffer_is_full(self, db_path, monkeypatch):
        """Once the top-up closes the gap, the very next tick sees
        depth >= target and creates nothing more — no runaway enqueue loop."""
        import hypogeum.scheduler as sch
        monkeypatch.setattr(sch, "_PRODUCTION_BUFFER_TARGET", 2)
        monkeypatch.setattr(sch, "_PRODUCTION_CATCHUP_MAX", 2)
        future = _now() + timedelta(hours=20)
        create_schedule(db_path=db_path, scope="channel", channel_id="ch_selflimit",
                        cadence="daily", next_run_at=future)
        first = tick(db_path=db_path, now=_now())
        assert len(first) == 2
        second = tick(db_path=db_path, now=_now())
        assert second == []

    def test_reactive_topup_skips_a_channel_already_at_target(self, db_path, monkeypatch):
        import hypogeum.scheduler as sch
        monkeypatch.setattr(sch, "_PRODUCTION_BUFFER_TARGET", 2)
        future = _now() + timedelta(hours=20)
        create_schedule(db_path=db_path, scope="channel", channel_id="ch_ok",
                        cadence="daily", next_run_at=future)
        self._seed_run_queue(db_path, "ch_ok", 2)   # already at target
        result = tick(db_path=db_path, now=_now())
        assert result == []

    def test_reactive_topup_disabled_with_target_zero(self, db_path, monkeypatch):
        import hypogeum.scheduler as sch
        monkeypatch.setattr(sch, "_PRODUCTION_BUFFER_TARGET", 0)
        future = _now() + timedelta(hours=20)
        create_schedule(db_path=db_path, scope="channel", channel_id="ch_notarget",
                        cadence="daily", next_run_at=future)
        result = tick(db_path=db_path, now=_now())
        assert result == []
