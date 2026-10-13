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


# ---- executors (hypogeum/revival_executor.py) -------------------------------------------------

def _approved(db, kind):
    aid = next(a["action_id"] for a in ra.propose_actions(CH, db_path=db) if a["kind"] == kind)
    ra.decide(aid, True, db_path=db)
    return aid


def test_queue_topic_caps_open_revival_topics_and_marks_applied(tmp_path, monkeypatch):
    from hypogeum import revival_executor as ex
    db = _gem_db(tmp_path)
    with get_connection(db) as c:
        for i in range(12):
            c.execute("INSERT INTO audience_comments VALUES (?,?,?,?,?,?,?)", (CH, f"c{i}", "v", f"Please make a video about subject number {i}?", i, None, "x"))
        c.commit()
    ids = [a["action_id"] for a in ra.propose_actions(CH, db_path=db) if a["kind"] == "audience_topic"]
    assert len(ids) == 3
    for i in ids:
        ra.decide(i, True, db_path=db)
    assert ex.queue_topic(ids[0], "x", db_path=db)["ok"] is False                      # too short to be a title
    assert ex.queue_topic(ids[0], "The Roman aqueducts nobody explains", db_path=db)["ok"] is True
    assert ex.queue_topic(ids[1], "How Roman concrete outlived the empire", db_path=db)["ok"] is True
    third = ex.queue_topic(ids[2], "Why the Antikythera gears still baffle us", db_path=db)
    assert third["ok"] is False and "limit" in third["reason"]                        # cap = 2 open revival topics
    with get_connection(db) as c:
        rows = c.execute("SELECT source, status, priority FROM episode_topics WHERE source='revival'").fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("revival", "queued", 0)] * 2        # default priority: waits its turn
    assert next(a for a in ra.list_actions(CH, db_path=db) if a["action_id"] == ids[0])["status"] == "applied"


def test_queue_topic_requires_approval(tmp_path):
    from hypogeum import revival_executor as ex
    db = _gem_db(tmp_path)
    aid = next(a["action_id"] for a in ra.propose_actions(CH, db_path=db) if a["kind"] == "repackage_post")
    assert ex.queue_topic(aid, "Anything at all goes here", db_path=db)["ok"] is False  # wrong kind and not approved


def test_cover_draft_limits(tmp_path, monkeypatch):
    from hypogeum import revival_executor as ex
    db = _gem_db(tmp_path)
    aid = _approved(db, "repackage_post")
    monkeypatch.setattr(ex, "_dry_run", lambda: True)
    assert ex.reserve_cover_draft(aid, db_path=db)["ok"] is False                     # dry run -> refuse
    monkeypatch.setattr(ex, "_dry_run", lambda: False)
    first = ex.reserve_cover_draft(aid, db_path=db)
    assert first["ok"] and first["episode_id"] == "ep_5"
    assert ex.reserve_cover_draft(aid, db_path=db)["ok"] is False                     # one draft at a time per action
    monkeypatch.setattr(ex, "DRAFTS_PER_DAY_CAP", 1)
    # the daily cap counts the draft already started today
    with get_connection(db) as c:
        c.execute("INSERT INTO revival_actions (action_id, channel_id, kind, platform, target_ref, status, title, rationale, created_at) "
                  "VALUES ('rv_x', ?, 'repackage_post', 'instagram', 'ep_1', 'approved', 't', 'r', '2026-10-13')", (CH,))
        c.commit()
    assert "limit" in ex.reserve_cover_draft("rv_x", db_path=db)["reason"]


def test_cover_draft_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    from hypogeum import revival_executor as ex
    from hypogeum.agents import vertical_thumbnail as vt
    db = _gem_db(tmp_path)
    aid = _approved(db, "repackage_post")
    monkeypatch.setattr(ex, "_dry_run", lambda: False)
    assert ex.reserve_cover_draft(aid, db_path=db)["ok"]
    monkeypatch.setattr(vt, "synthesize_concept_brief", lambda *a, **k: False)
    res = ex.generate_cover_draft(aid, db_path=db)
    assert res["ok"] is False
    d = next(a for a in ra.list_actions(CH, db_path=db) if a["action_id"] == aid)["draft"]
    assert d["status"] == "failed" and "concept" in d["reason"]
    assert ex.reserve_cover_draft(aid, db_path=db)["ok"] is True                      # a failed draft may be retried


def test_dashboard_executor_routes(tmp_path, monkeypatch):
    import run_dashboard as server
    from hypogeum import revival_executor as ex
    db = _gem_db(tmp_path)
    cover = _approved(db, "repackage_post")
    monkeypatch.setattr(ex, "_dry_run", lambda: True)
    old = server.app.config.get("DB_PATH")
    server.app.config["DB_PATH"] = str(db)
    try:
        c = server.app.test_client()
        r = c.post(f"/channel/{CH}/revival/action/{cover}", data={"do": "draft_cover"})
        assert r.status_code == 302 and "msg=" in r.headers["Location"]              # refused politely, no crash
        with get_connection(db) as conn:                                              # a ready draft is served back
            img = tmp_path / "d.png"
            img.write_bytes(b"\x89PNG")
            conn.execute("UPDATE revival_actions SET draft_json=? WHERE action_id=?",
                         (f'{{"status":"ready","path":"{img}"}}', cover))
            conn.commit()
        assert c.get(f"/channel/{CH}/revival/draft/{cover}").data == b"\x89PNG"
        assert c.get(f"/channel/{CH}/revival/draft/nope").status_code == 404
    finally:
        server.app.config["DB_PATH"] = old
