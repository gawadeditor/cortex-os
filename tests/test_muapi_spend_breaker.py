"""Tests for hypogeum.muapi_spend_breaker — the worker-level runaway-spend
circuit breaker that auto-pauses new production (publishing keeps running)
when rolling-hour MUAPI spend or the consecutive-failure streak crosses its
limit. Regression for the 2026-09-05 MUAPI-account-drain incident."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import hypogeum.db as db
from hypogeum.muapi_spend_breaker import check_muapi_spend_breaker, clear_breaker

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def _cfg(spend=45.0, fails=3):
    return SimpleNamespace(
        MUAPI_HOURLY_SPEND_LIMIT_USD=spend, MUAPI_CONSECUTIVE_FAILURE_LIMIT=fails,
    )


@pytest.fixture()
def dbp(tmp_path):
    p = tmp_path / "b.db"
    db.initialize_schema(p)
    import hypogeum.authorization_store as _st
    _c = db.get_connection(p)
    try:
        _st.ensure_tables(_c)
    finally:
        _c.close()
    return p


def _muapi_call(conn, ep, scene, amount, minutes_ago, state="committed"):
    ts = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    conn.execute(
        "INSERT INTO run_authorization_grant_consumption "
        "(reservation_id, grant_id, grant_version, provider, model, provider_operation, "
        " idempotency_key, envelope_id, authorization_id, reserved_amount, charged_amount, "
        " state, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{ep}:{scene}:r", "g", 1, "muapi", "veo3.1-lite-text-to-video", "video.text_to_video",
         f"{ep}:{scene}:primary", f"e_{ep}_{scene}", f"a_{ep}_{scene}",
         f"{amount:.4f}", f"{amount:.4f}" if state == "committed" else None, state, ts, ts),
    )
    conn.commit()


def _run(conn, ep, status, minutes_ago):
    ts = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    conn.execute(
        "INSERT INTO runs (run_id, episode_id, started_at, status, channel_id) VALUES (?,?,?,?,?)",
        (f"run_{ep}", ep, ts, status, "hypogeum"),
    )
    conn.commit()


def _paused(dbp):
    return db.get_worker_control("queue_paused", "0", db_path=dbp)


def test_quiet_factory_does_not_trip(dbp):
    conn = db.get_connection(dbp)
    _muapi_call(conn, "ep_a", "s01", 10.0, minutes_ago=30)  # one normal episode
    _run(conn, "ep_a", "completed", 20)
    out = check_muapi_spend_breaker(config=_cfg(), db_path=dbp, now=NOW)
    assert out["checked"] and not out["tripped_now"]
    assert _paused(dbp) == "0"


def test_hourly_spend_over_limit_trips(dbp):
    conn = db.get_connection(dbp)
    for i in range(6):
        _muapi_call(conn, "ep_burn", f"s{i}", 9.0, minutes_ago=15)  # $54 in the hour
    out = check_muapi_spend_breaker(config=_cfg(spend=45.0), db_path=dbp, now=NOW)
    assert out["tripped_now"]
    assert out["hour_spend_usd"] == pytest.approx(54.0, abs=0.01)
    assert "runaway" in out["reason"] or "last hour" in out["reason"]
    assert _paused(dbp) == "1"
    assert "muapi_spend_breaker" in db.get_worker_control("queue_paused_reason", "", db_path=dbp)


def test_spend_outside_the_hour_is_ignored(dbp):
    conn = db.get_connection(dbp)
    for i in range(6):
        _muapi_call(conn, "ep_old", f"s{i}", 9.0, minutes_ago=90)  # 1.5h ago
    out = check_muapi_spend_breaker(config=_cfg(spend=45.0), db_path=dbp, now=NOW)
    assert not out["tripped_now"]
    assert out["hour_spend_usd"] == 0.0


def test_consecutive_failures_trip(dbp):
    conn = db.get_connection(dbp)
    _run(conn, "ep_1", "failed", 40)
    _run(conn, "ep_2", "failed", 25)
    _run(conn, "ep_3", "failed", 10)
    out = check_muapi_spend_breaker(config=_cfg(fails=3), db_path=dbp, now=NOW)
    assert out["tripped_now"]
    assert out["consecutive_failures"] == 3
    assert "consecutively" in out["reason"]
    assert _paused(dbp) == "1"


def test_a_running_run_at_the_head_does_not_break_the_streak(dbp):
    conn = db.get_connection(dbp)
    _run(conn, "ep_1", "failed", 40)
    _run(conn, "ep_2", "failed", 25)
    _run(conn, "ep_3", "failed", 15)
    _run(conn, "ep_4", "running", 2)   # currently in flight — ignored
    out = check_muapi_spend_breaker(config=_cfg(fails=3), db_path=dbp, now=NOW)
    assert out["tripped_now"] and out["consecutive_failures"] == 3


def test_a_recent_success_resets_the_streak(dbp):
    conn = db.get_connection(dbp)
    _run(conn, "ep_1", "failed", 40)
    _run(conn, "ep_2", "completed", 25)
    _run(conn, "ep_3", "failed", 10)
    out = check_muapi_spend_breaker(config=_cfg(fails=3), db_path=dbp, now=NOW)
    assert not out["tripped_now"]
    assert out["consecutive_failures"] == 1


def test_stale_failures_do_not_retrip_after_resume(dbp):
    # 4 failures from a since-resolved incident, all > 3h ago
    conn = db.get_connection(dbp)
    for i, ep in enumerate(("ep_1", "ep_2", "ep_3", "ep_4")):
        _run(conn, ep, "failed", minutes_ago=300 + i * 20)
    out = check_muapi_spend_breaker(config=_cfg(fails=3), db_path=dbp, now=NOW)
    assert not out["tripped_now"]
    assert out["consecutive_failures"] == 0


def test_already_paused_is_a_noop(dbp):
    db.set_worker_control("queue_paused", "1", db_path=dbp)
    conn = db.get_connection(dbp)
    for i in range(6):
        _muapi_call(conn, "ep_burn", f"s{i}", 9.0, minutes_ago=15)
    out = check_muapi_spend_breaker(config=_cfg(), db_path=dbp, now=NOW)
    assert out["already_paused"] and not out["tripped_now"]


def test_limits_of_zero_disable_each_check(dbp):
    conn = db.get_connection(dbp)
    for i in range(10):
        _muapi_call(conn, "ep_burn", f"s{i}", 20.0, minutes_ago=10)
    _run(conn, "ep_1", "failed", 30)
    _run(conn, "ep_2", "failed", 20)
    _run(conn, "ep_3", "failed", 10)
    out = check_muapi_spend_breaker(config=_cfg(spend=0, fails=0), db_path=dbp, now=NOW)
    assert not out["tripped_now"]
    assert _paused(dbp) == "0"


def test_clear_breaker_gives_a_clean_slate(dbp):
    conn = db.get_connection(dbp)
    # 5 recent failures -> trips
    for i in range(5):
        _run(conn, f"ep_old{i}", "failed", minutes_ago=120 - i * 10)
    assert check_muapi_spend_breaker(config=_cfg(fails=3), db_path=dbp, now=NOW)["tripped_now"]
    assert _paused(dbp) == "1"

    # operator resumes via clear_breaker(): the OLD failures no longer count
    clear_breaker(db_path=dbp, now=NOW)
    assert _paused(dbp) == "0"
    out = check_muapi_spend_breaker(config=_cfg(fails=3), db_path=dbp, now=NOW + timedelta(seconds=1))
    assert not out["tripped_now"]
    assert out["consecutive_failures"] == 0

    # but NEW failures that START after the resume DO count
    _run(conn, "ep_new1", "failed", minutes_ago=-3)   # 3 min after NOW
    _run(conn, "ep_new2", "failed", minutes_ago=-4)
    _run(conn, "ep_new3", "failed", minutes_ago=-5)
    out2 = check_muapi_spend_breaker(config=_cfg(fails=3), db_path=dbp, now=NOW + timedelta(minutes=10))
    assert out2["tripped_now"]
    assert out2["consecutive_failures"] == 3


def test_no_raise_on_broken_db(tmp_path):
    out = check_muapi_spend_breaker(config=_cfg(), db_path=tmp_path / "nope" / "x.db", now=NOW)
    assert out["checked"] is False
    assert out["error"]
