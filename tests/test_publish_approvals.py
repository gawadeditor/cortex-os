"""hypogeum.publish_approvals — Phase 4d: nothing publishes before approval
when the workspace (or channel) requires it."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from hypogeum import publish_approvals as pa
from hypogeum import workspaces as ws
from hypogeum.db import initialize_schema
from hypogeum.factory_entity import create_factory

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
PREVIEW = {"title": "Pip the Owl", "description": "a calm story", "cover_path": None, "video_path": None}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import hypogeum.db as hdb
    p = tmp_path / "t.db"
    initialize_schema(p)
    monkeypatch.setattr(hdb, "_DEFAULT_DB_PATH", p)
    ws._READY.discard(str(p))
    create_factory("op", "Operator", "op", db_path=p)
    ws.backfill_platform_workspace(p)
    ws.assign("channel", "op_ch", "op", db_path=p)
    ws.create_workspace("Acme", workspace_id="acme", db_path=p)
    ws.assign("channel", "acme_ch", "acme", db_path=p)
    return p


class Notes(list):
    def __call__(self, row, kind):
        self.append((row["episode_id"], kind))


def test_not_required_by_default(db):
    assert pa.gate("op_ch", "ep1", PREVIEW, db_path=db, now=T0, notify=Notes()) == "proceed"
    assert pa.gate("acme_ch", "ep1", PREVIEW, db_path=db, now=T0, notify=Notes()) == "proceed"


def test_hold_until_approved(db):
    pa.set_settings("workspace", "acme", required=True, db_path=db)
    n = Notes()
    assert pa.gate("acme_ch", "ep1", PREVIEW, db_path=db, now=T0, notify=n) == "hold"
    assert n == [("ep1", "request")]
    assert pa.gate("acme_ch", "ep1", PREVIEW, db_path=db, now=T0 + timedelta(hours=1), notify=n) == "hold"
    assert len(n) == 1                                         # asked once
    aid = pa.list_for("acme", db_path=db)[0]["approval_id"]
    assert pa.decide(aid, "approved", "client", db_path=db, notify=n)
    assert pa.gate("acme_ch", "ep1", PREVIEW, db_path=db, now=T0 + timedelta(hours=2), notify=n) == "proceed"
    assert not pa.decide(aid, "rejected", "late click", db_path=db, notify=n)   # first answer wins


def test_hold_policy_never_publishes_without_answer_but_reminds(db):
    pa.set_settings("workspace", "acme", required=True, on_timeout="hold", timeout_hours=24, db_path=db)
    n = Notes()
    pa.gate("acme_ch", "ep1", PREVIEW, db_path=db, now=T0, notify=n)
    assert pa.gate("acme_ch", "ep1", PREVIEW, db_path=db, now=T0 + timedelta(days=5), notify=n) == "hold"
    assert ("ep1", "reminder") in n and n.count(("ep1", "reminder")) == 1


def test_publish_policy_auto_publishes_after_timeout(db):
    pa.set_settings("workspace", "acme", required=True, on_timeout="publish", timeout_hours=12, db_path=db)
    n = Notes()
    pa.gate("acme_ch", "ep1", PREVIEW, db_path=db, now=T0, notify=n)
    assert pa.gate("acme_ch", "ep1", PREVIEW, db_path=db, now=T0 + timedelta(hours=7), notify=n) == "hold"
    assert ("ep1", "reminder") in n                              # halfway reminder
    assert pa.gate("acme_ch", "ep1", PREVIEW, db_path=db, now=T0 + timedelta(hours=13), notify=n) == "proceed"
    assert ("ep1", "auto_published") in n


def test_rejected_stays_held(db):
    pa.set_settings("workspace", "acme", required=True, db_path=db)
    pa.gate("acme_ch", "ep1", PREVIEW, db_path=db, now=T0, notify=Notes())
    aid = pa.list_for("acme", db_path=db)[0]["approval_id"]
    pa.decide(aid, "rejected", "client", db_path=db, notify=Notes())
    assert pa.gate("acme_ch", "ep1", PREVIEW, db_path=db, now=T0 + timedelta(days=30), notify=Notes()) == "hold"


def test_channel_override_beats_workspace(db):
    pa.set_settings("workspace", "op", required=False, db_path=db)
    pa.set_settings("channel", "op_ch", required=True, db_path=db)
    assert pa.gate("op_ch", "ep9", PREVIEW, db_path=db, now=T0, notify=Notes()) == "hold"


def test_telegram_button_only_from_an_allowed_chat(db, monkeypatch):
    from hypogeum import alert_recipients as ar
    monkeypatch.setattr(ar, "_config", lambda: SimpleNamespace(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="999"))
    conn = ar._conn(db)
    conn.execute("INSERT INTO alert_recipients (recipient_id, workspace_id, channel, address, status, created_at, "
                 "updated_at) VALUES ('r1','acme','telegram','111','active','x','x')")
    conn.commit()
    conn.close()
    pa.set_settings("workspace", "acme", required=True, db_path=db)
    pa.gate("acme_ch", "ep1", PREVIEW, db_path=db, now=T0, notify=Notes())
    aid = pa.list_for("acme", db_path=db)[0]["approval_id"]
    assert "can't approve" in pa.handle_callback("555", f"appr:{aid}:a", "stranger", db_path=db, notify=Notes())
    assert pa.get(aid, db)["status"] == "pending"
    assert pa.handle_callback("111", f"appr:{aid}:a", "Client", db_path=db, notify=Notes()).startswith("Approved")
    assert pa.get(aid, db)["status"] == "approved" and pa.get(aid, db)["decided_by"] == "telegram:Client"
    assert pa.handle_callback("999", f"appr:{aid}:r", "Op", db_path=db, notify=Notes()).startswith("Already")
