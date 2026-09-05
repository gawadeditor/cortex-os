"""
tests/test_weekly_reservation_reaper.py — the reaper that trues down a
dead episode's committed channel_weekly plan hold to its real post-plan
spend (regression for the 2026-09-05 "hypogeum wedged at its weekly
ceiling on failed/superseded episodes" incident).

Covers:
  1. a superseded episode's committed plan-weekly row shrinks to
     (call-scope committed  −  preplan-incurred).
  2. a failed episode with no later success is reaped the same way.
  3. a PUBLISHED episode is never touched (it shipped — it keeps its hold).
  4. a still-LIVE episode (running / queued) is never touched.
  5. an episode with zero post-plan spend is RELEASED outright.
  6. the reaper is idempotent — a second pass frees nothing more.
  7. it never inflates — a row already below the computed target is left.
  8. no-raise: a broken DB path returns ok=False, never propagates.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import hypogeum.db as db
from hypogeum.weekly_reservation_reaper import (
    reconcile_terminal_episode_weekly_reservations as reap,
)

WK = f"{datetime.now(timezone.utc).isocalendar()[0]}-W{datetime.now(timezone.utc).isocalendar()[1]:02d}"
NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def dbp(tmp_path):
    p = tmp_path / "reap.db"
    db.initialize_schema(p)
    return p


def _weekly(conn, ch, ep, plan_max, phash="h0", status="committed"):
    rid = f"plan-weekly:{ch}:{ep}:{phash}"
    conn.execute(
        "INSERT INTO budget_reservations (reservation_id, scope_kind, scope_ref, "
        "idempotency_key, reserved_amount, currency, status, committed_amount, created_at, channel_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rid, "channel_weekly", f"{ch}:{WK}", rid, f"{plan_max:.6f}", "USD",
         status, f"{plan_max:.6f}" if status == "committed" else None, NOW, ch),
    )
    conn.commit()
    return rid


def _preplan(conn, ch, ep, amt):
    conn.execute(
        "INSERT INTO budget_reservations (reservation_id, scope_kind, scope_ref, "
        "idempotency_key, reserved_amount, currency, status, committed_amount, created_at, channel_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"preplan-incurred:{ch}:{ep}", "channel_weekly", f"{ch}:{WK}",
         f"preplan-incurred:{ch}:{ep}", f"{amt:.6f}", "USD", "committed",
         f"{amt:.6f}", NOW, ch),
    )
    conn.commit()


def _call(conn, ep, key, amt):
    conn.execute(
        "INSERT INTO budget_reservations (reservation_id, scope_kind, scope_ref, "
        "idempotency_key, reserved_amount, currency, status, committed_amount, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (f"{ep}:{key}", "call", f"{ep}:{key}", f"{ep}:{key}", f"{amt:.6f}",
         "USD", "committed", f"{amt:.6f}", NOW),
    )
    conn.commit()


def _run(conn, ep, status, ch="hypogeum"):
    conn.execute(
        "INSERT INTO runs (run_id, episode_id, started_at, status, channel_id) VALUES (?,?,?,?,?)",
        (f"run_{ep}_{status}", ep, NOW, status, ch),
    )
    conn.commit()


def _episode(conn, ep, status, title="T"):
    conn.execute(
        "INSERT INTO episodes (episode_id, title, created_at, status) VALUES (?,?,?,?)",
        (ep, title, NOW, status),
    )
    conn.commit()


def _weekly_sum(p, ch):
    return float(db.sum_reserved_and_committed(db.get_connection(p), "channel_weekly", f"{ch}:{WK}"))


def test_superseded_episode_is_trued_down(dbp):
    conn = db.get_connection(dbp)
    _weekly(conn, "hypogeum", "ep_sup", 15.0)
    _preplan(conn, "hypogeum", "ep_sup", 2.0)
    _call(conn, "ep_sup", "research", 2.0)          # preplan LLM
    _call(conn, "ep_sup", "video:s01", 5.0)
    _call(conn, "ep_sup", "video:s02", 4.0)         # post-plan real = 11 - 2 = 9
    _episode(conn, "ep_sup", "superseded")
    _run(conn, "ep_sup", "completed")

    out = reap(db_path=dbp)
    assert out["ok"] and len(out["reaped"]) == 1
    row = out["reaped"][0]
    assert row["episode_id"] == "ep_sup"
    assert row["to_usd"] == pytest.approx(9.0, abs=0.01)
    assert out["freed_usd"] == pytest.approx(6.0, abs=0.01)
    # ceiling now sees 9 (weekly plan) + 2 (preplan) = 11
    assert _weekly_sum(dbp, "hypogeum") == pytest.approx(11.0, abs=0.01)


def test_failed_episode_with_no_success_is_reaped(dbp):
    conn = db.get_connection(dbp)
    _weekly(conn, "hypogeum", "ep_fail", 12.0)
    _preplan(conn, "hypogeum", "ep_fail", 1.5)
    _call(conn, "ep_fail", "research", 1.5)
    _call(conn, "ep_fail", "video:s01", 3.0)
    _episode(conn, "ep_fail", "in_progress")
    _run(conn, "ep_fail", "failed")

    out = reap(db_path=dbp)
    assert len(out["reaped"]) == 1
    assert out["reaped"][0]["to_usd"] == pytest.approx(3.0, abs=0.01)


def test_published_episode_is_never_touched(dbp):
    conn = db.get_connection(dbp)
    _weekly(conn, "hypogeum", "ep_pub", 20.0)
    _episode(conn, "ep_pub", "superseded")   # even if flagged superseded
    _run(conn, "ep_pub", "completed")
    conn.execute(
        "INSERT INTO publishing_outcomes (outcome_id, episode_id, platform, publishing_status, recorded_at) "
        "VALUES (?,?,?,?,?)", ("o1", "ep_pub", "youtube", "success", NOW),
    )
    conn.commit()

    out = reap(db_path=dbp)
    assert out["reaped"] == []
    assert _weekly_sum(dbp, "hypogeum") == pytest.approx(20.0)


def test_live_episode_is_never_touched(dbp):
    conn = db.get_connection(dbp)
    _weekly(conn, "hypogeum", "ep_live", 18.0)
    _episode(conn, "ep_live", "in_progress")
    _run(conn, "ep_live", "failed")          # an earlier attempt failed…
    conn.execute(
        "INSERT INTO run_queue (queue_id, channel_id, episode_id, status, queued_at, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("q1", "hypogeum", "ep_live", "queued", NOW, NOW),
    )                                        # …but it is queued again
    conn.commit()

    out = reap(db_path=dbp)
    assert out["reaped"] == []


def test_zero_post_plan_spend_is_released(dbp):
    conn = db.get_connection(dbp)
    _weekly(conn, "hypogeum", "ep_zero", 10.0)
    _preplan(conn, "hypogeum", "ep_zero", 1.8)
    _call(conn, "ep_zero", "research", 1.8)   # only preplan spend, no video
    _episode(conn, "ep_zero", "superseded")
    _run(conn, "ep_zero", "failed")

    out = reap(db_path=dbp)
    assert out["reaped"][0]["result"] == "released"
    assert out["reaped"][0]["to_usd"] == 0.0
    # only the preplan row still counts
    assert _weekly_sum(dbp, "hypogeum") == pytest.approx(1.8, abs=0.01)


def test_reaper_is_idempotent(dbp):
    conn = db.get_connection(dbp)
    _weekly(conn, "hypogeum", "ep_idem", 15.0)
    _preplan(conn, "hypogeum", "ep_idem", 2.0)
    _call(conn, "ep_idem", "research", 2.0)
    _call(conn, "ep_idem", "video:s01", 6.0)
    _episode(conn, "ep_idem", "superseded")
    _run(conn, "ep_idem", "completed")

    first = reap(db_path=dbp)
    assert first["freed_usd"] == pytest.approx(9.0, abs=0.01)
    after1 = _weekly_sum(dbp, "hypogeum")
    second = reap(db_path=dbp)
    assert second["reaped"] == []
    assert _weekly_sum(dbp, "hypogeum") == pytest.approx(after1)


def test_reaper_never_inflates(dbp):
    conn = db.get_connection(dbp)
    # committed hold already BELOW real call spend (operator trued it down
    # by hand once) — the reaper must not raise it back up.
    _weekly(conn, "hypogeum", "ep_lo", 3.0)
    _preplan(conn, "hypogeum", "ep_lo", 1.0)
    _call(conn, "ep_lo", "video:s01", 50.0)
    _episode(conn, "ep_lo", "superseded")
    _run(conn, "ep_lo", "completed")

    out = reap(db_path=dbp)
    assert out["reaped"] == []
    # 3.0 weekly hold + 1.0 preplan, both untouched
    assert _weekly_sum(dbp, "hypogeum") == pytest.approx(4.0)


def test_reserved_preplan_and_planweekly_holds_are_released_for_a_dead_episode(dbp):
    conn = db.get_connection(dbp)
    ch, ep = "hypogeum", "ep_dead"
    # a $20 preplan hold (reserved) + a plan-weekly row that never committed
    for key, amt in [(f"preplan:{ch}:{ep}", 20.0), (f"plan-weekly:{ch}:{ep}:h9", 10.4)]:
        conn.execute(
            "INSERT INTO budget_reservations (reservation_id, scope_kind, scope_ref, "
            "idempotency_key, reserved_amount, currency, status, created_at, channel_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (key, "channel_weekly", f"{ch}:{WK}", key, f"{amt:.6f}", "USD", "reserved", NOW, ch),
        )
    conn.execute(
        "INSERT INTO budget_reservations (reservation_id, scope_kind, scope_ref, idempotency_key, "
        "reserved_amount, currency, status, committed_amount, created_at, channel_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"preplan-incurred:{ch}:{ep}", "channel_weekly", f"{ch}:{WK}",
         f"preplan-incurred:{ch}:{ep}", "1.9", "USD", "committed", "1.9", NOW, ch),
    )
    conn.commit()
    _episode(conn, ep, "superseded")
    _run(conn, ep, "failed")

    out = reap(db_path=dbp)
    assert out["ok"]
    released = {r["episode_id"]: r for r in out["reaped"] if r["result"] == "released_reserved"}
    assert ep in released
    assert out["freed_usd"] == pytest.approx(30.4, abs=0.01)
    # only the real pre-plan LLM cost still counts
    assert _weekly_sum(dbp, ch) == pytest.approx(1.9, abs=0.01)


def test_reserved_hold_for_a_live_episode_is_kept(dbp):
    conn = db.get_connection(dbp)
    ch, ep = "hypogeum", "ep_live2"
    conn.execute(
        "INSERT INTO budget_reservations (reservation_id, scope_kind, scope_ref, idempotency_key, "
        "reserved_amount, currency, status, created_at, channel_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"preplan:{ch}:{ep}", "channel_weekly", f"{ch}:{WK}", f"preplan:{ch}:{ep}",
         "20.0", "USD", "reserved", NOW, ch),
    )
    conn.commit()
    _run(conn, ep, "running")
    out = reap(db_path=dbp)
    assert not any(r["episode_id"] == ep for r in out["reaped"])
    assert _weekly_sum(dbp, ch) == pytest.approx(20.0)


def test_no_raise_on_broken_db(tmp_path):
    out = reap(db_path=tmp_path / "does_not_exist_dir" / "x.db")
    assert out["ok"] is False
    assert out["error"]
