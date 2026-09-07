"""The weekly channel-budget ledger trues up a COMPLETED episode's
plan-weekly / preplan holds to its ACTUAL recorded spend (2026-09-07 fix).

Before: a finished $9 short kept occupying its ~$28 worst-case plan
reservation, so the $135/week ceiling capped each channel at ~5-6 episodes
and the operator got surprised by real charges the ledger didn't show
(compounded by the MUAPI gate path never recording video cost at all).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from hypogeum.db import (
    get_connection, initialize_schema, reserve_budget,
    _weekly_reserved_sum_trued_up,
)


def _seed_episode(conn, ep, status):
    conn.execute(
        "INSERT INTO episodes (episode_id, title, topic, created_at, status) "
        "VALUES (?,?,?,?,?)", (ep, ep, ep, "2026-09-07T00:00:00+00:00", status))


def _seed_run(conn, ep):
    rid = f"run_{ep}"
    conn.execute("INSERT INTO runs (run_id, episode_id, started_at, status) "
                 "VALUES (?,?,?,?)", (rid, ep, "2026-09-07T00:00:00+00:00", "completed"))
    return rid


def _cost(conn, rid, ep, usd):
    import uuid
    conn.execute(
        "INSERT INTO costs (cost_id, run_id, episode_id, provider, model, call_type, "
        "cost_usd, called_at, metadata_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), rid, ep, "muapi", "veo3", "scene_generation", usd,
         "2026-09-07T01:00:00+00:00", "v1"))


def _weekly_hold(conn, ep, key, amount):
    import uuid
    conn.execute(
        "INSERT INTO budget_reservations (reservation_id, scope_kind, scope_ref, "
        "idempotency_key, reserved_amount, currency, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (f"res_{uuid.uuid4().hex[:8]}", "channel_weekly", "ch:2026-W37", key,
         str(amount), "USD", "committed", "2026-09-07T00:30:00+00:00"))


@pytest.fixture()
def conn(tmp_path):
    p = tmp_path / "t.db"
    initialize_schema(p)
    c = get_connection(p)
    yield c
    c.close()


def test_completed_episode_trues_up_to_actual(conn):
    _seed_episode(conn, "ep_done", "completed")
    rid = _seed_run(conn, "ep_done")
    _cost(conn, rid, "ep_done", 9.30)                        # real spend
    _weekly_hold(conn, "ep_done", "plan-weekly:ch:ep_done:hash", Decimal("28.00"))
    _weekly_hold(conn, "ep_done", "preplan-incurred:ch:ep_done", Decimal("2.10"))
    conn.commit()

    rows = conn.execute(
        "SELECT idempotency_key, reserved_amount FROM budget_reservations "
        "WHERE scope_kind='channel_weekly'").fetchall()
    trued = _weekly_reserved_sum_trued_up(conn, "ch:2026-W37",
                                          [(r[0], r[1]) for r in rows])
    # min(actual 9.30, held 30.10) == 9.30
    assert trued == Decimal("9.30")


def test_in_flight_episode_keeps_worst_case_hold(conn):
    _seed_episode(conn, "ep_live", "in_progress")
    _weekly_hold(conn, "ep_live", "plan-weekly:ch:ep_live:hash", Decimal("28.00"))
    conn.commit()
    rows = conn.execute(
        "SELECT idempotency_key, reserved_amount FROM budget_reservations "
        "WHERE scope_kind='channel_weekly'").fetchall()
    trued = _weekly_reserved_sum_trued_up(conn, "ch:2026-W37",
                                          [(r[0], r[1]) for r in rows])
    assert trued == Decimal("28.00")  # unknown final cost -> full hold stands


def test_non_episode_row_counted_verbatim(conn):
    _weekly_hold(conn, "cap", "manual-observation-cap:ch:2026-W37", Decimal("50.00"))
    conn.commit()
    rows = conn.execute(
        "SELECT idempotency_key, reserved_amount FROM budget_reservations "
        "WHERE scope_kind='channel_weekly'").fetchall()
    trued = _weekly_reserved_sum_trued_up(conn, "ch:2026-W37",
                                          [(r[0], r[1]) for r in rows])
    assert trued == Decimal("50.00")


def test_reserve_budget_admits_more_after_true_up(conn):
    # ceiling 100; one finished episode holding 90 worst-case but only 8 real
    _seed_episode(conn, "ep_a", "completed")
    rid = _seed_run(conn, "ep_a")
    _cost(conn, rid, "ep_a", 8.00)
    _weekly_hold(conn, "ep_a", "plan-weekly:ch:ep_a:h", Decimal("90.00"))
    conn.commit()

    # a new 40 reservation: 8 (trued) + 40 = 48 <= 100 -> ACCEPTED
    accepted, _row, _new = reserve_budget(
        conn, "res_b", "channel_weekly", "ch:2026-W37",
        "plan-weekly:ch:ep_b:h", "40.00", "USD", "100",
        created_at="2026-09-07T00:40:00+00:00")
    assert accepted is True

    # without the true-up this would have been 90 + 40 = 130 > 100 -> denied
