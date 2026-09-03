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


def test_advances_past_latest_future_slot(db_path):
    base = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    psl.record_slot("ch", datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc), "ep_a", db_path=db_path)
    psl.record_slot("ch", datetime(2026, 9, 6, 16, 0, tzinfo=timezone.utc), "ep_b", db_path=db_path)
    out = psl.next_free_now("ch", base, db_path=db_path)
    assert out == datetime(2026, 9, 6, 16, 1, tzinfo=timezone.utc)


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
    """Three back-to-back releases -> three consecutive daily slots."""
    from hypogeum.scheduler import compute_next_publish_slot
    sched = {"time_of_day": "16:00"}
    tz = "UTC"
    day0 = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)  # before 16:00
    got = []
    for i in range(3):
        eff = psl.next_free_now("ch", day0 if i == 0 else datetime.now(timezone.utc), db_path=db_path)
        # first iteration anchored at day0; later ones the ledger dominates
        slot = compute_next_publish_slot(sched, tz, max(eff, day0))
        psl.record_slot("ch", slot, f"ep_{i}", db_path=db_path)
        got.append(slot)
    assert got[0] == datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc)
    assert got[1] == datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc)
    assert got[2] == datetime(2026, 9, 6, 16, 0, tzinfo=timezone.utc)
