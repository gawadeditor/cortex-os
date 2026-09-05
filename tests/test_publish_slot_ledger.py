"""Tests for hypogeum.publishing.publish_slot_ledger — spacing a
produce-ahead buffer's publish slots."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hypogeum.db import initialize_schema
from hypogeum.publishing import publish_slot_ledger as psl


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "t.db"
    initialize_schema(p)
    return p


def test_no_slots_returns_base_now(db_path):
    base = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    assert psl.next_free_now("ch", base, db_path=db_path) == base


def test_backfills_earliest_gap_then_appends(db_path):
    # today (Sep 4) is free -> return base_now verbatim
    base = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    psl.record_slot("ch", datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc), "ep_today", db_path=db_path)
    psl.record_slot("ch", datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc), "ep_a", db_path=db_path)
    psl.record_slot("ch", datetime(2026, 9, 7, 16, 0, tzinfo=timezone.utc), "ep_c", db_path=db_path)
    # Sep 6 is the gap -> filled before anything past Sep 7
    assert psl.next_free_now("ch", base, db_path=db_path) == datetime(2026, 9, 6, tzinfo=timezone.utc)
    psl.record_slot("ch", datetime(2026, 9, 6, 16, 0, tzinfo=timezone.utc), "ep_gap", db_path=db_path)
    # no gaps left -> append the first day past the last slot
    assert psl.next_free_now("ch", base, db_path=db_path) == datetime(2026, 9, 8, tzinfo=timezone.utc)


def test_todays_free_day_returns_base_now(db_path):
    base = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    psl.record_slot("ch", datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc), "ep_a", db_path=db_path)
    psl.record_slot("ch", datetime(2026, 9, 6, 16, 0, tzinfo=timezone.utc), "ep_b", db_path=db_path)
    # Sep 4 itself has no slot -> earliest open day is today
    assert psl.next_free_now("ch", base, db_path=db_path) == base


def test_ignores_past_slots(db_path):
    base = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    psl.record_slot("ch", datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc), "ep_old", db_path=db_path)
    assert psl.next_free_now("ch", base, db_path=db_path) == base


def test_per_channel_isolation(db_path):
    base = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    psl.record_slot("ch_a", datetime(2026, 9, 9, 16, 0, tzinfo=timezone.utc), "ep_a", db_path=db_path)
    assert psl.next_free_now("ch_b", base, db_path=db_path) == base


def test_record_slot_idempotent_on_slot(db_path):
    psl.record_slot("ch", datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc), "ep_a", db_path=db_path)
    psl.record_slot("ch", datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc), "ep_b", db_path=db_path)
    slots = psl.channel_slots("ch", db_path=db_path, upcoming_only=False)
    assert len(slots) == 1
    assert slots[0]["episode_id"] == "ep_b"


def test_buffer_lands_on_consecutive_days(db_path):
    """Three releases compiled in one tick -> three consecutive daily slots."""
    from hypogeum.scheduler import compute_next_publish_slot
    sched = {"time_of_day": "16:00"}
    tz = "UTC"
    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)  # fixed; before 16:00
    got = []
    for i in range(3):
        eff = psl.next_free_now("ch", now, db_path=db_path)
        slot = compute_next_publish_slot(sched, tz, eff)
        psl.record_slot("ch", slot, f"ep_{i}", db_path=db_path)
        got.append(slot)
    assert got[0] == datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc)
    assert got[1] == datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc)
    assert got[2] == datetime(2026, 9, 6, 16, 0, tzinfo=timezone.utc)


def test_buffer_backfills_a_gap_before_appending(db_path):
    """A buffered release fills an open day between now and the last slot."""
    from hypogeum.scheduler import compute_next_publish_slot
    sched = {"time_of_day": "16:00"}
    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    for d, ep in [(4, "ep_a"), (5, "ep_b"), (7, "ep_d")]:  # Sep 6 left open
        psl.record_slot("ch", datetime(2026, 9, d, 16, 0, tzinfo=timezone.utc), ep, db_path=db_path)
    eff = psl.next_free_now("ch", now, db_path=db_path)
    slot = compute_next_publish_slot(sched, "UTC", eff)
    assert slot == datetime(2026, 9, 6, 16, 0, tzinfo=timezone.utc)


def test_day_is_taken(db_path):
    psl.record_slot("ch", datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc), "ep_a", db_path=db_path)
    # same day, different episode -> taken
    assert psl.day_is_taken("ch", datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc),
                             "ep_b", db_path=db_path) == "ep_a"
    # same day, SAME episode -> not a clash
    assert psl.day_is_taken("ch", datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
                            "ep_a", db_path=db_path) is None
    # different day -> free
    assert psl.day_is_taken("ch", datetime(2026, 9, 6, 16, 0, tzinfo=timezone.utc),
                            "ep_b", db_path=db_path) is None
    # different channel -> free
    assert psl.day_is_taken("ch2", datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc),
                            "ep_b", db_path=db_path) is None
