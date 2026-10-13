"""Tests for hypogeum.revival_autopilot — every provider/YouTube/Telegram call is faked; nothing external is touched."""
from __future__ import annotations

import json

from hypogeum import revival_autopilot as ap
from hypogeum import revival_executor as ex
from hypogeum.db import get_connection
from tests.test_revival_actions import _gem_db
from tests.test_revival_engine import CH


def _comments(db, n=12):
    with get_connection(db) as c:
        for i in range(n):
            c.execute("INSERT INTO audience_comments VALUES (?,?,?,?,?,?,?)",
                      (CH, f"c{i}", "v", f"Please make a video about subject number {i}?" if i < 2 else f"great number {i} amazing", i, None, "x"))
        c.commit()


def _fake_generation(monkeypatch, status="ready"):
    """Replace the (paid) cover generation with one that just records the outcome."""
    monkeypatch.setattr(ex, "_dry_run", lambda: False)

    def fake(action_id, *, db_path=None):
        with get_connection(db_path) as conn:
            if status == "ready":
                ex._set_draft(conn, action_id, {"status": "ready", "path": "/x.jpg", "check": "faithful", "started_at": "2026-10-13T00:00:00+00:00"})
            else:
                ex._set_draft(conn, action_id, {"status": "failed", "reason": "boom", "started_at": "2026-10-13T00:00:00+00:00"})
        return {"ok": status == "ready"}
    monkeypatch.setattr(ex, "generate_cover_draft", fake)


def test_default_policy_is_off_and_does_nothing(tmp_path):
    db = _gem_db(tmp_path)
    assert ap.get_mode(CH, db_path=db) == "off"
    assert ap.run_channel(CH, db_path=db)["proposed"] == 0
    with get_connection(db) as c:
        assert c.execute("SELECT COUNT(*) FROM revival_actions").fetchone()[0] == 0


def test_observe_only_proposes(tmp_path):
    db = _gem_db(tmp_path)
    ap.set_mode(CH, "observe", db_path=db)
    out = ap.run_channel(CH, db_path=db)
    assert out["proposed"] > 0 and out["directives"] == 0
    with get_connection(db) as c:
        assert {r[0] for r in c.execute("SELECT status FROM revival_actions")} == {"proposed"}


def test_auto_turns_findings_into_directives_without_asking(tmp_path, monkeypatch):
    db = _gem_db(tmp_path)
    _comments(db)
    _fake_generation(monkeypatch)
    monkeypatch.setattr(ap, "_alert_hand", lambda a, db_path: None)
    ap.set_mode(CH, "auto", db_path=db)
    out = ap.run_channel(CH, db_path=db)
    assert out["directives"] >= 3                      # 2 audience requests + the conversion leak
    with get_connection(db) as c:
        dims = {r[0] for r in c.execute("SELECT dimension FROM channel_learning_directives WHERE channel_id=? AND status='active'", (CH,))} \
            if "status" in [x[1] for x in c.execute("PRAGMA table_info(channel_learning_directives)")] else \
            {r[0] for r in c.execute("SELECT dimension FROM channel_learning_directives WHERE channel_id=?", (CH,))}
    assert {"topic", "description"} <= dims


def test_cover_without_api_path_alerts_the_operator_once(tmp_path, monkeypatch):
    db = _gem_db(tmp_path)
    _fake_generation(monkeypatch)
    alerts = []
    monkeypatch.setattr(ap, "_alert_hand", lambda a, db_path: alerts.append(a["action_id"]))
    monkeypatch.setattr(ap, "_live_apply", lambda: {})          # no platform can set a cover by API
    ap.set_mode(CH, "auto", db_path=db)
    out = ap.run_channel(CH, db_path=db)
    assert out["covers"] == 1 and out["alerts"] == 1 and out["live"] == 0   # one generation per pass


def test_cover_applied_by_api_respects_the_daily_cap(tmp_path, monkeypatch):
    db = _gem_db(tmp_path)
    _fake_generation(monkeypatch)
    applied = []

    def fake_apply(action_id, *, db_path=None):
        from hypogeum.revival_actions import mark_applied
        mark_applied(action_id, db_path=db_path)
        applied.append(action_id)
        return {"ok": True}
    monkeypatch.setattr(ap, "_live_apply", lambda: {"instagram": fake_apply})   # the seeded gem is on instagram
    monkeypatch.setattr(ap, "_alert_hand", lambda a, db_path: None)
    ap.set_mode(CH, "auto", db_path=db)
    for _ in range(3):
        ap.run_channel(CH, db_path=db)
    assert len(applied) == ap.MAX_LIVE_CHANGES_PER_DAY


def test_failed_generation_is_retried_a_bounded_number_of_times(tmp_path, monkeypatch):
    db = _gem_db(tmp_path)
    _fake_generation(monkeypatch, status="failed")
    monkeypatch.setattr(ap, "_alert_hand", lambda a, db_path: None)
    monkeypatch.setattr(ap, "_live_apply", lambda: {})
    ap.set_mode(CH, "auto", db_path=db)
    gens = sum(ap.run_channel(CH, db_path=db)["covers"] for _ in range(6))
    assert gens == ap.MAX_COVER_ATTEMPTS


def test_youtube_apply_refuses_an_unchecked_draft(tmp_path):
    db = _gem_db(tmp_path)
    from hypogeum import revival_actions as ra
    aid = next(a["action_id"] for a in ra.propose_actions(CH, db_path=db) if a["kind"] == "repackage_post")
    ra.decide(aid, True, db_path=db)
    with get_connection(db) as c:                         # a ready draft that never passed the consistency check
        c.execute("UPDATE revival_actions SET platform='youtube', draft_json=? WHERE action_id=?",
                  (json.dumps({"status": "ready", "path": "/x.jpg"}), aid))
        c.commit()
    res = ex.apply_youtube_cover(aid, db_path=db)
    assert res["ok"] is False and "consistency" in res["reason"]
