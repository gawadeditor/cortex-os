"""Tests for hypogeum.revival_actions (Revival Engine step 3: propose -> approve -> apply -> measure)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypogeum.db import get_connection
from hypogeum import revival_actions as ra
from tests.test_revival_engine import CH, _seed


def _gem_db(tmp_path):
    db = tmp_path / "ra.db"
    _seed(db, [(1000, 0.2, 40.0, None)] * 5 + [(50, 0.9, 30.0, None)] + [(1000, 0.2, 40.0, None)], gained=2)
    return db


def test_propose_is_idempotent_and_never_touches_live_state(tmp_path):
    db = _gem_db(tmp_path)
    first = ra.propose_actions(CH, db_path=db)
    kinds = {a["kind"] for a in first}
    assert {"repackage_post", "conversion_cta"} <= kinds
    assert ra.propose_actions(CH, db_path=db) == []          # same findings -> no duplicates
    assert {a["status"] for a in ra.list_actions(CH, db_path=db)} == {"proposed"}


def test_must_approve_before_applying(tmp_path):
    db = _gem_db(tmp_path)
    aid = next(a["action_id"] for a in ra.propose_actions(CH, db_path=db) if a["kind"] == "repackage_post")
    assert ra.mark_applied(aid, db_path=db) is False         # not approved yet
    assert ra.decide(aid, True, db_path=db) is True
    assert ra.decide(aid, False, db_path=db) is False        # decided once
    assert ra.mark_applied(aid, db_path=db) is True
    row = next(a for a in ra.list_actions(CH, db_path=db) if a["action_id"] == aid)
    assert row["status"] == "applied" and row["baseline"]["metric"] == "reach"


def test_measurement_is_net_of_control_and_waits_for_window(tmp_path):
    db = _gem_db(tmp_path)
    aid = next(a["action_id"] for a in ra.propose_actions(CH, db_path=db) if a["kind"] == "repackage_post")
    ra.decide(aid, True, db_path=db)
    ra.mark_applied(aid, db_path=db)
    assert ra.measure_due(CH, db_path=db) == 0               # window has not passed
    with get_connection(db) as c:                            # target 50 -> 200 reach; the others stay put
        c.execute("UPDATE platform_analytics SET views=200, reach=200, snapshot_date='2026-10-20' WHERE episode_id='ep_5'")
        c.commit()
    later = datetime.now(timezone.utc) + timedelta(days=ra.MEASURE_AFTER_DAYS + 1)
    assert ra.measure_due(CH, db_path=db, now=later) == 1
    res = next(a for a in ra.list_actions(CH, db_path=db) if a["action_id"] == aid)["result"]
    assert res["lift_pct"] == 300.0 and res["control_lift_pct"] == 0.0 and res["verdict"] == "improved"


def test_small_movement_is_inconclusive(tmp_path):
    assert ra._verdict(4.0) == "inconclusive" and ra._verdict(-4.0) == "inconclusive"
    assert ra._verdict(None) == "not measurable"


def test_dashboard_route_decisions(tmp_path):
    import run_dashboard as server
    from hypogeum.channel_config import ChannelProfile  # noqa: F401  (route needs a known channel)
    db = _gem_db(tmp_path)
    aid = ra.propose_actions(CH, db_path=db)[0]["action_id"]
    old = server.app.config.get("DB_PATH")
    server.app.config["DB_PATH"] = str(db)
    try:
        r = server.app.test_client().post(f"/channel/{CH}/revival/action/{aid}", data={"do": "approve"})
        assert r.status_code == 302
        assert next(a for a in ra.list_actions(CH, db_path=db) if a["action_id"] == aid)["status"] == "approved"
    finally:
        server.app.config["DB_PATH"] = old
