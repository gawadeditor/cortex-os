"""
tests/test_weekly_budget_enforcement.py — Phase 3 core: per-channel
weekly paid-video budget enforcement (hypogeum.weekly_budget + the
CHANNEL_WEEKLY scope on hypogeum.db's budget_reservations ledger).

Covers, at the enforcement primitive level:
  - ISO Monday-00:00 channel-local window computation (UTC boundaries,
    DST spring-forward/fall-back weeks, ISO year rollover).
  - Fail-closed policy resolution (missing channel/timezone/ceiling,
    malformed/non-positive ceiling, unreadable ledger).
  - Atomic check-and-reserve semantics: inside budget, exact ceiling
    equality allowed, exceeds-remaining denied, multi-submission
    accumulation, structural channel isolation.
  - Idempotent replay never double-reserves (retry/resume safety).
  - Committed/unknown rows keep consuming headroom at their full reserved
    amount; released rows stop counting immediately.
  - Concurrent workers cannot over-reserve (BEGIN IMMEDIATE serialization).
  - In-week timezone changes cannot reset spend or mint fresh headroom;
    corrupt/unreadable persisted window evidence fails closed.
  - Week rollover addresses a fresh empty ledger scope.
  - Window-evidence snapshots persist inside the reservation insert.
  - The existing-database migration preserves every legacy row while
    widening scope_kind's CHECK constraint and adding evidence columns.

All tests run against tmp_path databases only — never the production DB
(tests/conftest.py enforces this independently).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Thread

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hypogeum.budget_scopes as bs
import hypogeum.channel_config as channel_config
import hypogeum.db as db
from hypogeum.weekly_budget import (
    WeeklyBudgetError,
    WeeklyReservationOutcome,
    WeeklyCeilingMalformedError,
    WeeklyCeilingMissingError,
    WeeklyCeilingNonPositiveError,
    WeeklyChannelMissingError,
    WeeklyCorruptWindowStateError,
    WeeklyTimezoneConflictError,
    WeeklyTimezoneInvalidError,
    WeeklyTimezoneMissingError,
    compute_weekly_window,
    ensure_channel_weekly_reservation,
    release_channel_weekly_reservation,
    resolve_channel_weekly_policy,
    week_scope_ref,
)

AMOUNT = Decimal("0.39")  # matches a plan-priced veo3.1-lite call


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "weekly_test.db"
    db.initialize_schema(path)
    return path


@pytest.fixture
def conn(db_path):
    c = db.get_connection(db_path)
    try:
        yield c
    finally:
        c.close()


def _configure_channel(db_path_or_conn, channel_id="ch_weekly", tz="UTC", ceiling="25.00"):
    """Create the channel_profiles + channel_operations_profiles rows with a
    weekly ceiling. Accepts either a db_path (public CRUD API) or an open
    connection (direct row for malformed-value cases)."""
    if not isinstance(db_path_or_conn, sqlite3.Connection):
        if channel_config.get_channel_profile(channel_id, db_path=db_path_or_conn) is None:
            channel_config.create_channel_profile(
                channel_id=channel_id, name="Weekly Test Channel", db_path=db_path_or_conn,
            )
        existing = channel_config.get_channel_operations_profile(channel_id, db_path=db_path_or_conn)
        kwargs = dict(timezone=tz)
        if ceiling is not None:
            kwargs["weekly_video_budget_usd"] = float(ceiling)
        if existing is None:
            channel_config.create_channel_operations_profile(
                channel_id=channel_id, channel_name="Weekly Test Channel",
                db_path=db_path_or_conn, **kwargs,
            )
        else:
            channel_config.update_channel_operations_profile(
                channel_id, db_path=db_path_or_conn, **kwargs,
            )
        return

    conn = db_path_or_conn
    channel_config._ensure_tables(conn)
    conn.execute(
        "INSERT OR IGNORE INTO channel_profiles (channel_id, name, created_at) VALUES (?,?,?)",
        (channel_id, "Weekly Test Channel", "2026-08-25T00:00:00+00:00"),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO channel_operations_profiles
            (channel_id, channel_name, timezone, weekly_video_budget_usd,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, '2026-08-25T00:00:00+00:00', '2026-08-25T00:00:00+00:00')
        """,
        (channel_id, "Weekly Test Channel", tz, float(ceiling) if ceiling is not None else None),
    )
    conn.commit()


def _ensure(conn, *, channel_id="ch_weekly", key="ep_001:s01b01:primary",
            amount=AMOUNT, currency="USD", now=None):
    """Returns a WeeklyReservationOutcome (`.request`, `.reservation_id`,
    `.created`)."""
    return ensure_channel_weekly_reservation(
        conn,
        channel_id=channel_id,
        amount=amount,
        currency=currency,
        idempotency_key=key,
        now_utc=now or datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc),
    )


def _weekly_rows(conn, *, channel_id="ch_weekly"):
    return conn.execute(
        """
        SELECT * FROM budget_reservations
        WHERE scope_kind = 'channel_weekly' AND channel_id = ?
        ORDER BY created_at, reservation_id
        """,
        (channel_id,),
    ).fetchall()


def _active_sum(conn, scope_ref):
    rows = conn.execute(
        """
        SELECT reserved_amount FROM budget_reservations
        WHERE scope_kind = 'channel_weekly' AND scope_ref = ?
          AND status IN ('reserved','committed')
        """,
        (scope_ref,),
    ).fetchall()
    return sum((Decimal(r[0]) for r in rows), Decimal("0"))


# ---------------------------------------------------------------------------
# Window computation
# ---------------------------------------------------------------------------


class TestWeeklyWindowComputation:
    def test_midweek_instant_maps_to_monday_started_window_utc(self):
        # Tue 2026-08-25T12:00Z -> week Mon 2026-08-24 .. Mon 2026-08-31 (UTC zone)
        w = compute_weekly_window(datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc), "UTC")
        assert w.week_key == "2026-W35"
        assert w.start_utc == datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)
        assert w.end_utc == datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)

    def test_channel_timezone_shifts_boundaries_not_the_label(self):
        # Mon 2026-08-24T02:00Z is already Mon 06:00 in Asia/Dubai (+4):
        # the channel-local week started Sun 20:00Z.
        w = compute_weekly_window(datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc), "Asia/Dubai")
        assert w.week_key == "2026-W35"
        assert w.start_utc == datetime(2026, 8, 23, 20, 0, tzinfo=timezone.utc)
        assert w.end_utc == datetime(2026, 8, 30, 20, 0, tzinfo=timezone.utc)

    def test_sunday_evening_belongs_to_running_week(self):
        # Sun 2026-08-30T21:30Z is Mon 01:30 in Dubai -> NEXT iso week there.
        w = compute_weekly_window(datetime(2026, 8, 30, 21, 30, tzinfo=timezone.utc), "Asia/Dubai")
        assert w.week_key == "2026-W36"

    def test_dst_spring_forward_week_is_167_hours(self):
        # America/New_York: DST starts Sun 2026-03-08 02:00 local.
        # Week Mon 2026-03-02 .. Mon 2026-03-09 loses one hour.
        w = compute_weekly_window(datetime(2026, 3, 4, 12, 0, tzinfo=timezone.utc), "America/New_York")
        assert w.end_utc - w.start_utc == timedelta(hours=167)
        assert w.week_key == "2026-W10"

    def test_dst_fall_back_week_is_169_hours(self):
        # America/New_York: DST ends Sun 2026-11-01 02:00 local.
        # Week Mon 2026-10-26 .. Mon 2026-11-02 gains one hour.
        w = compute_weekly_window(datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc), "America/New_York")
        assert w.end_utc - w.start_utc == timedelta(hours=169)
        assert w.week_key == "2026-W44"

    def test_iso_year_rollover_december_monday(self):
        # Mon 2024-12-30 is ISO 2025-W01 (year rollover mid-window label).
        w = compute_weekly_window(datetime(2024, 12, 30, 5, 0, tzinfo=timezone.utc), "UTC")
        assert w.week_key == "2025-W01"
        assert w.start_utc == datetime(2024, 12, 30, 0, 0, tzinfo=timezone.utc)

    def test_iso_year_rollover_week_53(self):
        # Mon 2020-12-28 belongs to ISO 2020-W53, not 2021-W01.
        w = compute_weekly_window(datetime(2020, 12, 29, 5, 0, tzinfo=timezone.utc), "UTC")
        assert w.week_key == "2020-W53"

    def test_january_first_maps_back_to_prior_monday(self):
        # Thu 2026-01-01 sits in 2026-W01 whose Monday is 2025-12-29.
        w = compute_weekly_window(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc), "UTC")
        assert w.week_key == "2026-W01"
        assert w.start_utc == datetime(2025, 12, 29, 0, 0, tzinfo=timezone.utc)

    def test_naive_now_rejected(self):
        with pytest.raises(WeeklyBudgetError):
            compute_weekly_window(datetime(2026, 8, 25, 12, 0), "UTC")

    def test_invalid_zone_rejected(self):
        with pytest.raises(WeeklyTimezoneInvalidError):
            compute_weekly_window(datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc), "Not/AZone")


# ---------------------------------------------------------------------------
# Policy resolution fail-closed matrix
# ---------------------------------------------------------------------------


class TestPolicyResolutionFailClosed:
    def test_happy_path_returns_tz_and_decimal_ceiling(self, conn, db_path):
        _configure_channel(db_path, ceiling="25.50")
        tz, ceiling = resolve_channel_weekly_policy(conn, "ch_weekly")
        assert tz == "UTC"
        assert ceiling == Decimal("25.5")

    def test_missing_channel_row_fails_closed(self, conn):
        channel_config._ensure_tables(conn)
        with pytest.raises(WeeklyChannelMissingError):
            resolve_channel_weekly_policy(conn, "no_such_channel")

    def test_blank_channel_id_fails_closed(self, conn):
        with pytest.raises(WeeklyChannelMissingError):
            resolve_channel_weekly_policy(conn, "   ")

    def test_missing_timezone_fails_closed(self, conn):
        # Raw row path: create/update APIs hard-validate timezone at write
        # time, so a missing-tz row can only exist via legacy/direct writes.
        _configure_channel(conn, tz="", ceiling="10")
        with pytest.raises(WeeklyTimezoneMissingError):
            resolve_channel_weekly_policy(conn, "ch_weekly")

    def test_invalid_timezone_fails_closed(self, conn):
        _configure_channel(conn, tz="Mars/Olympus", ceiling="10")
        with pytest.raises(WeeklyTimezoneInvalidError):
            resolve_channel_weekly_policy(conn, "ch_weekly")

    def test_missing_ceiling_fails_closed_never_unlimited(self, conn, db_path):
        _configure_channel(db_path, ceiling=None)
        with pytest.raises(WeeklyCeilingMissingError):
            resolve_channel_weekly_policy(conn, "ch_weekly")

    @pytest.mark.parametrize("bad", ["abc", "", "1.2.3", "nan"])
    def test_malformed_ceiling_fails_closed(self, conn, bad):
        _configure_channel(conn, ceiling=None)
        conn.execute(
            "UPDATE channel_operations_profiles SET weekly_video_budget_usd = ? "
            "WHERE channel_id = 'ch_weekly'",
            (bad,),
        )
        conn.commit()
        with pytest.raises((WeeklyCeilingMalformedError, WeeklyCeilingMissingError)):
            resolve_channel_weekly_policy(conn, "ch_weekly")

    @pytest.mark.parametrize("bad", [0, -1, 0.0])
    def test_non_positive_ceiling_fails_closed(self, conn, bad):
        _configure_channel(conn, ceiling=bad)
        with pytest.raises(WeeklyCeilingNonPositiveError):
            resolve_channel_weekly_policy(conn, "ch_weekly")

    def test_float_ceiling_parses_exactly(self, conn, db_path):
        _configure_channel(db_path, ceiling="12.34")
        _, ceiling = resolve_channel_weekly_policy(conn, "ch_weekly")
        assert ceiling == Decimal("12.34")


# ---------------------------------------------------------------------------
# Atomic check-and-reserve semantics
# ---------------------------------------------------------------------------


class TestAtomicReserveSemantics:
    def test_inside_budget_reserves_once_with_evidence_snapshot(self, conn, db_path):
        _configure_channel(db_path)
        outcome = _ensure(conn)
        assert outcome.created is True
        assert outcome.reservation_id == "ep_001:s01b01:primary:channel_weekly"
        request = outcome.request
        rows = _weekly_rows(conn)
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "reserved"
        assert row["scope_ref"] == "ch_weekly:2026-W35"
        assert Decimal(row["reserved_amount"]) == AMOUNT
        # Immutable window evidence written in the SAME insert:
        assert row["channel_id"] == "ch_weekly"
        assert row["weekly_timezone_snapshot"] == "UTC"
        assert row["weekly_window_start_utc"] == "2026-08-24T00:00:00+00:00"
        assert row["weekly_window_end_utc"] == "2026-08-31T00:00:00+00:00"
        assert Decimal(row["weekly_ceiling_snapshot"]) == Decimal("25.00")
        assert request.scope_kind == bs.BudgetScopeKind.CHANNEL_WEEKLY

    def test_exact_boundary_equality_is_allowed(self, conn, db_path):
        _configure_channel(db_path, ceiling="1.00")
        _ensure(conn, key="k1", amount=Decimal("0.39"))
        _ensure(conn, key="k2", amount=Decimal("0.61"))
        assert _active_sum(conn, "ch_weekly:2026-W35") == Decimal("1.00")

    def test_exceeding_remaining_denies_and_reserves_nothing(self, conn, db_path):
        _configure_channel(db_path, ceiling="1.00")
        _ensure(conn, key="k1", amount=Decimal("0.80"))
        with pytest.raises(bs.BudgetCeilingExceededError):
            _ensure(conn, key="k2", amount=Decimal("0.39"))
        rows = _weekly_rows(conn)
        assert len(rows) == 1  # denial left no partial row
        assert _active_sum(conn, "ch_weekly:2026-W35") == Decimal("0.80")

    def test_two_episodes_accumulate_in_one_channel_week(self, conn, db_path):
        _configure_channel(db_path, ceiling="2.00")
        _ensure(conn, key="ep_a:s01:primary")
        _ensure(conn, key="ep_b:s01:primary")
        _ensure(conn, key="ep_c:s01:primary")
        assert _active_sum(conn, "ch_weekly:2026-W35") == AMOUNT * 3

    def test_channels_are_structurally_isolated(self, conn, db_path):
        _configure_channel(db_path, channel_id="ch_a", ceiling="0.39")
        _configure_channel(db_path, channel_id="ch_b", ceiling="100.00")
        _ensure(conn, channel_id="ch_a", key="ep_a:s01:primary")  # exhausts ch_a
        with pytest.raises(bs.BudgetCeilingExceededError):
            _ensure(conn, channel_id="ch_a", key="ep_a:s02:primary")
        # ch_b neither sees ch_a's spend nor shares its ceiling:
        _ensure(conn, channel_id="ch_b", key="ep_b:s01:primary")
        _ensure(conn, channel_id="ch_b", key="ep_b:s02:primary")
        assert _active_sum(conn, "ch_b:2026-W35") == AMOUNT * 2
        assert _active_sum(conn, "ch_a:2026-W35") == AMOUNT

    def test_idempotent_replay_does_not_double_reserve(self, conn, db_path):
        _configure_channel(db_path)
        first = _ensure(conn, key="ep_x:s01:primary")
        again = _ensure(conn, key="ep_x:s01:primary")
        # OWNERSHIP SIGNAL: exactly one creator; the replay knows it does
        # not own the row.
        assert first.created is True
        assert again.created is False
        assert again.reservation_id == first.reservation_id
        rows = _weekly_rows(conn)
        assert len(rows) == 1
        expected_rid = "ep_x:s01:primary:channel_weekly"
        assert first.request.scope_ref == week_scope_ref("ch_weekly", compute_weekly_window(
            datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc), "UTC"))
        assert rows[0]["reservation_id"] == expected_rid
        assert _active_sum(conn, "ch_weekly:2026-W35") == AMOUNT

    def test_replay_ignores_current_ceiling_state(self, conn, db_path):
        """A replayed identity returns its existing row even when a fresh
        reservation of the same size would no longer fit -- the amount was
        validated and counted once, and retries must never re-check or
        re-charge it."""
        _configure_channel(db_path, ceiling="0.39")
        _ensure(conn, key="ep_x:s01:primary")
        with pytest.raises(bs.BudgetCeilingExceededError):
            _ensure(conn, key="ep_y:s01:primary")  # different identity denied
        replayed = _ensure(conn, key="ep_x:s01:primary")  # same identity replays
        assert replayed.created is False
        assert replayed.request.scope_ref == "ch_weekly:2026-W35"
        assert len(_weekly_rows(conn)) == 1

    def test_committed_keeps_consuming_headroom_at_reserved_amount(self, conn, db_path):
        """Never replace conservative reservation accounting with an
        unverified lower actual charge: committing at a lower charged
        amount keeps the full reserved amount counted."""
        _configure_channel(db_path, ceiling="1.00")
        outcome = _ensure(conn, key="ep_x:s01:primary", amount=Decimal("0.61"))
        rid = outcome.reservation_id
        status = db.commit_budget_reservation(conn, rid, "0.15", at="2026-08-25T13:00:00+00:00")
        assert status == "committed"
        assert _active_sum(conn, "ch_weekly:2026-W35") == Decimal("0.61")
        # Remaining headroom is only 0.39 -- a 0.40 ask now denies:
        with pytest.raises(bs.BudgetCeilingExceededError):
            _ensure(conn, key="ep_y:s01:primary", amount=Decimal("0.40"))

    def test_released_stops_counting_immediately(self, conn, db_path):
        _configure_channel(db_path, ceiling="0.60")
        outcome = _ensure(conn, key="ep_x:s01:primary")
        assert release_channel_weekly_reservation(
            conn, outcome, at="2026-08-25T13:00:00+00:00",
        )
        assert _active_sum(conn, "ch_weekly:2026-W35") == Decimal("0")
        _ensure(conn, key="ep_y:s01:primary")  # headroom restored
        assert _active_sum(conn, "ch_weekly:2026-W35") == AMOUNT

    def test_release_refuses_non_weekly_scope(self, conn):
        other = bs.ScopeRequest(bs.BudgetScopeKind.CALL, "s01", Decimal("1"), "USD")
        bogus = WeeklyReservationOutcome(
            request=other, reservation_id="x", idempotency_key="k", created=True,
        )
        with pytest.raises(WeeklyBudgetError):
            release_channel_weekly_reservation(conn, bogus, at="2026-08-25T13:00:00+00:00")


class TestConcurrency:
    def test_concurrent_workers_cannot_over_reserver(self, db_path):
        """N workers on independent connections race to reserve against the
        SAME channel-week; BEGIN IMMEDIATE serializes check-and-insert so
        accepted reservations can never exceed the ceiling."""
        _configure_channel(db_path, ceiling="1.56")
        workers = 12
        per_attempt = Decimal("0.39")
        barrier = Barrier(workers)
        results: list = []
        lock = __import__("threading").Lock()

        def _worker(i: int):
            c = db.get_connection(db_path)
            try:
                barrier.wait()
                try:
                    ensure_channel_weekly_reservation(
                        c, channel_id="ch_weekly",
                        amount=per_attempt, currency="USD",
                        idempotency_key=f"race:{i}",
                        now_utc=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
                    )
                    ok = True
                except bs.BudgetError:
                    ok = False
                with lock:
                    results.append(ok)
            finally:
                c.close()

        threads = [Thread(target=_worker, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 4  # floor(1.56 / 0.39) exactly
        conn = db.get_connection(db_path)
        try:
            assert _active_sum(conn, "ch_weekly:2026-W35") == Decimal("1.56")
            # Accepted reservations are exactly the visible rows; every denial
            # left no partial row behind.
            assert len(_weekly_rows(conn)) == sum(results)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Timezone-change protection (locked decision 5)
# ---------------------------------------------------------------------------


class TestTimezoneChangeProtection:
    def test_inweek_timezone_change_cannot_reset_headroom(self, conn, db_path):
        _configure_channel(db_path, tz="UTC", ceiling="1.00")
        _ensure(conn, key="ep_x:s01:primary")  # accepted & charged under UTC window
        rid = _weekly_rows(conn)[0]["reservation_id"]
        db.commit_budget_reservation(conn, rid, "0.15", at="2026-08-25T13:00:00+00:00")

        # Operator flips the channel timezone mid-week...
        channel_config.update_channel_operations_profile(
            "ch_weekly", timezone="America/New_York", db_path=db_path,
        )
        # ...a NEW authorization is rejected until the existing window closes:
        with pytest.raises(WeeklyTimezoneConflictError):
            _ensure(conn, key="ep_x:s02:primary")
        # The charged reservation still consumes its FULL reserved amount
        # (never the unverified lower actual charge), under the old window:
        assert _active_sum(conn, "ch_weekly:2026-W35") == AMOUNT

    def test_timezone_change_to_adjacent_week_key_is_also_blocked(self, conn, db_path):
        """The dangerous bypass shape: a tz flip near a week boundary moves
        'now' into a DIFFERENT ISO week key with an apparently fresh empty
        scope. The overlapping active window blocks it."""
        _configure_channel(db_path, tz="Asia/Dubai", ceiling="5.00")
        # Sun 2026-08-30T21:00Z = Mon 01:00 Dubai (W35); reserve there.
        _ensure(conn, key="ep_x:s01:primary",
                now=datetime(2026, 8, 30, 21, 0, tzinfo=timezone.utc))
        # Flip to UTC: same instant is Sun 21:00 UTC -> still W35 but under a
        # DIFFERENT timezone snapshot; and flipping such that boundaries move
        # must not mint fresh headroom either way.
        channel_config.update_channel_operations_profile(
            "ch_weekly", timezone="UTC", db_path=db_path,
        )
        with pytest.raises(WeeklyTimezoneConflictError):
            _ensure(conn, key="ep_x:s02:primary",
                    now=datetime(2026, 8, 30, 22, 0, tzinfo=timezone.utc))

    def test_after_window_closes_new_timezone_applies_cleanly(self, conn, db_path):
        _configure_channel(db_path, tz="UTC", ceiling="0.39")
        outcome = _ensure(conn, key="ep_x:s01:primary")
        release_channel_weekly_reservation(
            conn, outcome, at="2026-08-25T13:00:00+00:00",
        )
        channel_config.update_channel_operations_profile(
            "ch_weekly", timezone="America/New_York", db_path=db_path,
        )
        # No active windows remain -> next week's instant resolves cleanly:
        outcome = _ensure(conn, key="ep_y:s01:primary",
                          now=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc))
        assert outcome.request.scope_ref == "ch_weekly:2026-W36"


class TestWeekRollover:
    def test_rollover_addresses_fresh_empty_scope(self, conn, db_path):
        _configure_channel(db_path, ceiling="0.39")
        _ensure(conn, key="ep_w35:s01:primary",
                now=datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc))
        with pytest.raises(bs.BudgetCeilingExceededError):
            _ensure(conn, key="ep_w35:s02:primary",
                    now=datetime(2026, 8, 27, 12, 30, tzinfo=timezone.utc))
        # After rollover the same ceiling allows a fresh W36 reservation;
        # the old week's rows are frozen untouched as audit trail.
        outcome = _ensure(conn, key="ep_w36:s01:primary",
                          now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc))
        assert outcome.request.scope_ref == "ch_weekly:2026-W36"
        assert _active_sum(conn, "ch_weekly:2026-W35") == AMOUNT
        assert _active_sum(conn, "ch_weekly:2026-W36") == AMOUNT


# ---------------------------------------------------------------------------
# Corrupt / unreadable accounting state fails closed
# ---------------------------------------------------------------------------


class TestCorruptStateFailClosed:
    def test_active_row_with_null_window_evidence_denies(self, conn, db_path):
        _configure_channel(db_path)
        _ensure(conn, key="ep_x:s01:primary")
        conn.execute(
            "UPDATE budget_reservations SET weekly_window_start_utc = NULL "
            "WHERE scope_kind = 'channel_weekly'",
        )
        conn.commit()
        with pytest.raises(WeeklyCorruptWindowStateError):
            _ensure(conn, key="ep_x:s02:primary")

    def test_active_row_with_garbage_window_denies(self, conn, db_path):
        _configure_channel(db_path)
        _ensure(conn, key="ep_x:s01:primary")
        conn.execute(
            "UPDATE budget_reservations SET weekly_window_start_utc = 'not-a-timestamp' "
            "WHERE scope_kind = 'channel_weekly'",
        )
        conn.commit()
        with pytest.raises(WeeklyCorruptWindowStateError):
            _ensure(conn, key="ep_x:s02:primary")

    def test_active_row_with_naive_timestamps_denies(self, conn, db_path):
        _configure_channel(db_path)
        _ensure(conn, key="ep_x:s01:primary")
        conn.execute(
            "UPDATE budget_reservations SET weekly_window_start_utc = '2026-08-24T00:00:00', "
            "weekly_window_end_utc = '2026-08-31T00:00:00' WHERE scope_kind = 'channel_weekly'",
        )
        conn.commit()
        with pytest.raises(WeeklyCorruptWindowStateError):
            _ensure(conn, key="ep_x:s02:primary")

    def test_dropped_ledger_table_is_unreadable_and_denies(self, conn, db_path):
        _configure_channel(db_path)
        conn.execute("DROP TABLE budget_reservations")
        conn.commit()
        with pytest.raises(WeeklyBudgetError):
            _ensure(conn)


# ---------------------------------------------------------------------------
# Existing-database migration (authoritative mechanism: transactional
# table rebuild in hypogeum.db.initialize_schema)
# ---------------------------------------------------------------------------

_OLD_BUDGET_DDL = """
CREATE TABLE budget_reservations (
    reservation_id    TEXT NOT NULL PRIMARY KEY,
    scope_kind        TEXT NOT NULL
                           CHECK(scope_kind IN ('call','run','episode','organization','retry','fallback')),
    scope_ref         TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL,
    reserved_amount   TEXT NOT NULL,
    currency          TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'reserved'
                           CHECK(status IN ('reserved','committed','released')),
    committed_amount  TEXT,
    created_at        TEXT NOT NULL,
    resolved_at       TEXT,
    UNIQUE(scope_kind, scope_ref, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_budget_reservations_scope
    ON budget_reservations(scope_kind, scope_ref, status);
"""


class TestExistingDatabaseMigration:
    def _build_legacy_db(self, path: Path) -> None:
        raw = sqlite3.connect(str(path))
        try:
            raw.executescript(_OLD_BUDGET_DDL)
            raw.execute(
                """
                INSERT INTO budget_reservations
                    (reservation_id, scope_kind, scope_ref, idempotency_key,
                     reserved_amount, currency, status, committed_amount,
                     created_at, resolved_at)
                VALUES ('r1', 'call', 's01', 'ep_old:s01:primary',
                        '0.39', 'USD', 'committed', '0.15',
                        '2026-07-01T00:00:00+00:00', '2026-07-01T01:00:00+00:00')
                """
            )
            raw.execute(
                """
                INSERT INTO budget_reservations
                    (reservation_id, scope_kind, scope_ref, idempotency_key,
                     reserved_amount, currency, status, committed_amount,
                     created_at, resolved_at)
                VALUES ('r2', 'episode', 'ep_old', 'ep_old:s02:primary',
                        '0.60', 'USD', 'released', NULL,
                        '2026-07-01T00:00:00+00:00', '2026-07-01T02:00:00+00:00')
                """
            )
            raw.commit()
        finally:
            raw.close()

    def test_migration_preserves_data_and_widens_check(self, tmp_path):
        path = tmp_path / "legacy.db"
        self._build_legacy_db(path)

        db.initialize_schema(path)

        conn = sqlite3.connect(str(path))
        try:
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='budget_reservations'"
            ).fetchone()[0]
            assert "'channel_weekly'" in ddl
            cols = {row[1] for row in conn.execute("PRAGMA table_info(budget_reservations)").fetchall()}
            assert {
                "channel_id", "weekly_timezone_snapshot",
                "weekly_window_start_utc", "weekly_window_end_utc",
                "weekly_ceiling_snapshot",
            } <= cols

            rows = {
                r[0]: r
                for r in conn.execute(
                    "SELECT reservation_id, scope_kind, scope_ref, idempotency_key, "
                    "reserved_amount, currency, status, committed_amount FROM budget_reservations"
                ).fetchall()
            }
            assert set(rows) == {"r1", "r2"}
            assert rows["r1"][1:] == ("call", "s01", "ep_old:s01:primary", "0.39", "USD", "committed", "0.15")
            assert rows["r2"][1:] == ("episode", "ep_old", "ep_old:s02:primary", "0.60", "USD", "released", None)
        finally:
            conn.close()

    def test_migration_accepts_channel_weekly_inserts_afterwards(self, tmp_path):
        path = tmp_path / "legacy.db"
        self._build_legacy_db(path)
        db.initialize_schema(path)

        _configure_channel(path)
        conn = db.get_connection(path)
        try:
            outcome = _ensure(conn)
            assert outcome.request.scope_ref == "ch_weekly:2026-W35"
        finally:
            conn.close()
        # Legacy committed spend counts ONLY in its own scopes, never in the
        # new channel-weekly scope (structural attribution isolation):
        conn = db.get_connection(path)
        try:
            assert _active_sum(conn, "ch_weekly:2026-W35") == AMOUNT
        finally:
            conn.close()

    def test_migration_is_idempotent(self, tmp_path):
        path = tmp_path / "legacy.db"
        self._build_legacy_db(path)
        db.initialize_schema(path)
        db.initialize_schema(path)  # second run must be a clean no-op
        conn = sqlite3.connect(str(path))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM budget_reservations"
            ).fetchone()[0]
            assert count == 2
        finally:
            conn.close()

    def test_precheck_constraint_still_enforced_post_migration(self, tmp_path):
        path = tmp_path / "legacy.db"
        self._build_legacy_db(path)
        db.initialize_schema(path)
        conn = sqlite3.connect(str(path))
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO budget_reservations
                        (reservation_id, scope_kind, scope_ref, idempotency_key,
                         reserved_amount, currency, created_at)
                    VALUES ('bad', 'warp', 'x', 'y', '1', 'USD', 'now')
                    """
                )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Adversarial: compensating-release OWNERSHIP (Phase 3 correction)
#
# The exact attack shapes from the review:
#   A creates; B replays the same identity; B fails downstream; B must NOT
#   release A's reservation. A committed or unknown-held row must never be
#   releasable by a replaying attempt.
# ---------------------------------------------------------------------------


class TestReleaseOwnership:
    def test_replaying_attempt_cannot_release_creators_reservation(self, conn, db_path):
        _configure_channel(db_path)
        creator = _ensure(conn, key="ep_x:s01:primary")

        # The replaying attempt "fails" downstream and attempts a
        # compensating release. It does not own the row -> release is
        # refused by the ownership gate.
        replayer = _ensure(conn, key="ep_x:s01:primary")
        assert replayer.created is False
        assert release_channel_weekly_reservation(
            conn, replayer, at="2026-08-25T13:00:00+00:00",
        ) is False

        # The creator's reservation is untouched and still counts.
        rows = _weekly_rows(conn)
        assert len(rows) == 1
        assert rows[0]["status"] == "reserved"
        assert rows[0]["reservation_id"] == creator.reservation_id
        assert _active_sum(conn, "ch_weekly:2026-W35") == AMOUNT

    def test_creator_can_still_release_its_own_row(self, conn, db_path):
        _configure_channel(db_path)
        outcome = _ensure(conn, key="ep_x:s01:primary")
        assert release_channel_weekly_reservation(
            conn, outcome, at="2026-08-25T13:00:00+00:00",
        ) is True
        rows = _weekly_rows(conn)
        assert rows[0]["status"] == "released"

    def test_committed_row_is_never_releasable(self, conn, db_path):
        _configure_channel(db_path)
        outcome = _ensure(conn, key="ep_x:s01:primary")
        db.commit_budget_reservation(
            conn, outcome.reservation_id, str(AMOUNT), at="2026-08-25T13:00:00+00:00"
        )
        # Creator itself retries the release after commit: no-op.
        assert release_channel_weekly_reservation(
            conn, outcome, at="2026-08-25T14:00:00+00:00",
        ) is False
        rows = _weekly_rows(conn)
        assert rows[0]["status"] == "committed"
        assert _active_sum(conn, "ch_weekly:2026-W35") == AMOUNT

    def test_concurrent_creator_replayer_race_has_exactly_one_creator(self, db_path):
        """N workers race the SAME identity on independent connections:
        exactly ONE observes created=True; every other observes a replay
        (created=False) and therefore can never release the row."""
        import threading

        _configure_channel(db_path, ceiling="10.00")
        workers = 10
        barrier = threading.Barrier(workers)
        created_flags: list = []
        lock = threading.Lock()

        def _worker(i: int):
            c = db.get_connection(db_path)
            try:
                barrier.wait()
                outcome = ensure_channel_weekly_reservation(
                    c, channel_id="ch_weekly",
                    amount=AMOUNT, currency="USD",
                    idempotency_key="race:one-key",
                    now_utc=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
                )
                with lock:
                    created_flags.append(outcome.created)
            finally:
                c.close()

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(created_flags) == 1  # exactly one creator
        conn = db.get_connection(db_path)
        try:
            rows = _weekly_rows(conn)
            assert len(rows) == 1  # one row, never duplicated
            assert rows[0]["status"] == "reserved"  # replays could not release it
            assert _active_sum(conn, "ch_weekly:2026-W35") == AMOUNT
        finally:
            conn.close()

    def test_release_refuses_foreign_outcome_shape(self, conn):
        """A raw ScopeRequest (not this attempt's outcome) can never be
        smuggled into the release path."""
        other = bs.ScopeRequest(bs.BudgetScopeKind.CALL, "s01", Decimal("1"), "USD")
        bogus = WeeklyReservationOutcome(
            request=other, reservation_id="x", idempotency_key="k", created=True,
        )
        with pytest.raises(WeeklyBudgetError):
            release_channel_weekly_reservation(conn, bogus, at="2026-08-25T13:00:00+00:00")

    def test_multi_scope_compensating_release_skips_replayed_scopes(self, conn, db_path):
        """reserve_all_scopes' own partial-failure compensation releases only
        scopes this call CREATED -- a replayed CHANNEL_WEEKLY row survives an
        EPISODE-scope denial in the same call."""
        _configure_channel(db_path, ceiling="0.39")
        creator = _ensure(conn, key="plan-weekly:ch:ep:hash", amount=AMOUNT)
        assert creator.created is True

        # Mixed call: weekly REPLAYS (created=False), episode scope denies.
        mixed = bs.ScopeRequest(bs.BudgetScopeKind.CHANNEL_WEEKLY, "ch_weekly:2026-W35",
                                Decimal("10"), "USD")
        denied = bs.ScopeRequest(bs.BudgetScopeKind.EPISODE, "ep_other",
                                 Decimal("0.001"), "USD")  # ceiling below amount
        with pytest.raises(bs.BudgetCeilingExceededError):
            bs.reserve_all_scopes(conn, [mixed, denied], idempotency_key="other-attempt",
                                  amount=Decimal("5.00"), currency="USD",
                                  at="2026-08-25T15:00:00+00:00",
                                  reservation_id_prefix="other-attempt")
        # The pre-existing weekly row survived the partial-failure rollback.
        rows = _weekly_rows(conn)
        assert len(rows) == 1
        assert rows[0]["status"] == "reserved"
        assert _active_sum(conn, "ch_weekly:2026-W35") == AMOUNT
