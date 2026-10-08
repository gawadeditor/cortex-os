"""hypogeum.publishing.platform_slot_guard — an evidence-based platform
override slot (hypogeum YouTube = Saturday 15:00 UTC, 2026-10-07) must never
put two episodes of a channel on the same platform on the same day."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing import platform_slot_guard as g
from hypogeum.publishing.schedule_compiler import ScheduleCompilationResult
from hypogeum.publishing.zernio_publish_runtime import DeliverableSchedule, SchedulePlan

UTC = timezone.utc
NOW = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)                  # Thursday
SAT17 = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)
WED14 = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)
SAT10 = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)
WEEKLY = ({"weekly_slots": [{"day_of_week": "monday", "time_of_day": "10:00"},
                            {"day_of_week": "wednesday", "time_of_day": "18:00"},
                            {"day_of_week": "saturday", "time_of_day": "15:00"}],
           "timezone": "UTC"}, "UTC")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    initialize_schema(p)
    monkeypatch.setattr(g, "_base_schedule", lambda ch, d, dbp: WEEKLY)
    return p


def _hold(db, ep, release_at):
    conn = get_connection(db)
    conn.execute("INSERT INTO youtube_cover_holds (post_id, channel_id, episode_id, release_at, upload_at, status, held_at) "
                 "VALUES (?,?,?,?,?,?,?)", (f"post_{ep}", "hypogeum", ep, release_at.isoformat(),
                                           release_at.isoformat(), "held", NOW.isoformat()))
    conn.commit()
    conn.close()


def _slot(db, ep, slot):
    conn = get_connection(db)
    conn.execute("INSERT INTO channel_publish_slots (channel_id, slot_utc, episode_id, created_at) VALUES (?,?,?,?)",
                 ("hypogeum", slot.isoformat(), ep, NOW.isoformat()))
    conn.commit()
    conn.close()


def _result(base, youtube):
    entry = DeliverableSchedule(scheduled_for=base, timezone_name="UTC", publish_now=False,
                                platform_scheduled_for_by_platform={"youtube": youtube})
    return ScheduleCompilationResult(channel_id="hypogeum", blocked=False, slot=base,
                                     schedule_plan=SchedulePlan(channel_id="hypogeum", entries={"main_episode": entry}))


def _yt(res):
    return res.schedule_plan.entries["main_episode"].platform_scheduled_for_by_platform["youtube"]


def test_free_preferred_slot_is_kept(db):
    assert _yt(g.resolve_platform_collisions("hypogeum", "ep_new", _result(WED14, SAT17), db_path=db, now=NOW)) == SAT17


def test_taken_preferred_day_falls_back_to_the_episodes_own_base_slot(db):
    _hold(db, "ep_sulawesi", SAT17)                              # already on YouTube that Saturday
    _slot(db, "ep_sulawesi", datetime(2026, 10, 12, 10, 0, tzinfo=UTC))
    res = g.resolve_platform_collisions("hypogeum", "ep_new", _result(WED14, SAT17), db_path=db, now=NOW)
    assert _yt(res) == WED14
    assert res.schedule_plan.entries["main_episode"].scheduled_for == WED14   # base untouched


def test_base_also_taken_moves_to_next_free_base_slot(db):
    _hold(db, "ep_a", SAT10)                                     # YouTube Sat 10 taken
    res = g.resolve_platform_collisions("hypogeum", "ep_new", _result(SAT10, SAT10), db_path=db, now=NOW)
    assert _yt(res) == datetime(2026, 10, 12, 10, 0, tzinfo=UTC)               # next Mon slot


def test_an_episode_without_a_hold_still_occupies_its_base_day_on_youtube(db):
    _slot(db, "ep_plain", SAT17)                                 # no override, YouTube at base time
    assert _yt(g.resolve_platform_collisions("hypogeum", "ep_new", _result(WED14, SAT17), db_path=db, now=NOW)) == WED14


def test_own_episode_is_not_a_collision_and_errors_leave_schedule_unchanged(db, monkeypatch):
    _hold(db, "ep_new", SAT17)                                   # a re-run of the same episode
    assert _yt(g.resolve_platform_collisions("hypogeum", "ep_new", _result(WED14, SAT17), db_path=db, now=NOW)) == SAT17
    monkeypatch.setattr(g, "taken_platform_days", lambda *a, **k: 1 / 0)
    res = _result(WED14, SAT17)
    assert g.resolve_platform_collisions("hypogeum", "ep_x", res, db_path=db, now=NOW) is res
