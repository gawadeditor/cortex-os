"""hypogeum.alert_recipients — Phase 4: a client connects Telegram from a
dashboard link (one platform bot, no token handling), alerts route per
workspace."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hypogeum import alert_recipients as ar
from hypogeum import workspaces as ws
from hypogeum.db import initialize_schema
from hypogeum.factory_entity import create_factory


class FakeBot:
    def __init__(self):
        self.sent, self.updates = [], []

    def __call__(self, method, params):
        if method == "getMe":
            return {"ok": True, "result": {"username": "cortex_test_bot"}}
        if method == "sendMessage":
            self.sent.append((str(params["chat_id"]), params["text"]))
            return {"ok": True}
        if method == "getUpdates":
            off = params.get("offset", 0)
            return {"ok": True, "result": [u for u in self.updates if u["update_id"] >= off]}
        raise AssertionError(method)

    def user_says(self, chat_id, text, uid):
        self.updates.append({"update_id": uid, "message": {
            "chat": {"id": chat_id, "type": "private", "first_name": "Client"}, "text": text}})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import hypogeum.db as hdb
    p = tmp_path / "t.db"
    initialize_schema(p)
    monkeypatch.setattr(hdb, "_DEFAULT_DB_PATH", p)
    ws._READY.discard(str(p))
    create_factory("op", "Operator", "op", db_path=p)
    ws.backfill_platform_workspace(p)
    ws.create_workspace("Acme Media", workspace_id="acme", db_path=p)
    monkeypatch.setattr(ar, "_config", lambda: SimpleNamespace(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="999"))
    ar._bot_username_cache.clear()
    return p, FakeBot()


def test_link_start_connects_the_chat_to_its_workspace(env):
    db, bot = env
    link = ar.create_link("acme", db_path=db, http=bot)
    assert link["url"] == f"https://t.me/cortex_test_bot?start={link['code']}"
    bot.user_says(111, f"/start {link['code']}", 1)
    assert ar.poll_once(db, http=bot) == 1
    assert [r["address"] for r in ar.recipients_for("acme", db)] == ["111"]
    assert bot.sent[-1][0] == "111" and "Acme Media" in bot.sent[-1][1]
    assert ar.poll_once(db, http=bot) == 0                    # offset: never processed twice


def test_code_is_single_use_and_expires(env, monkeypatch):
    db, bot = env
    link = ar.create_link("acme", db_path=db, http=bot)
    bot.user_says(111, f"/start {link['code']}", 1)
    bot.user_says(222, f"/start {link['code']}", 2)          # someone reusing the same link
    ar.poll_once(db, http=bot)
    assert [r["address"] for r in ar.recipients_for("acme", db)] == ["111"]
    assert "expired" in bot.sent[-1][1]
    old = ar.create_link("acme", ttl_minutes=-1, db_path=db, http=bot)
    bot.user_says(333, f"/start {old['code']}", 3)
    ar.poll_once(db, http=bot)
    assert "333" not in [r["address"] for r in ar.recipients_for("acme", db)]


def test_routing_client_alert_reaches_client_and_operator_only(env):
    db, bot = env
    ws.create_workspace("Beta", workspace_id="beta", db_path=db)
    for wid, chat, uid in (("acme", 111, 1), ("beta", 222, 2)):
        link = ar.create_link(wid, db_path=db, http=bot)
        bot.user_says(chat, f"/start {link['code']}", uid)
    ar.poll_once(db, http=bot)
    cfg = SimpleNamespace(TELEGRAM_CHAT_ID="999")
    assert set(ar.chat_ids_for("acme", db_path=db, config=cfg)) == {"111", "999"}
    assert "222" not in ar.chat_ids_for("acme", db_path=db, config=cfg)   # never another client
    assert ar.chat_ids_for(None, db_path=db, config=cfg) == ["999"]        # platform-only alert


def test_stop_disconnects(env):
    db, bot = env
    link = ar.create_link("acme", db_path=db, http=bot)
    bot.user_says(111, f"/start {link['code']}", 1)
    bot.user_says(111, "/stop", 2)
    ar.poll_once(db, http=bot)
    assert ar.recipients_for("acme", db) == []


def test_unknown_workspace_link_rejected(env):
    db, bot = env
    with pytest.raises(ValueError):
        ar.create_link("nope", db_path=db, http=bot)


def test_checklist_filters_alert_types(env):
    db, bot = env
    link = ar.create_link("acme", db_path=db, http=bot)
    bot.user_says(111, f"/start {link['code']}", 1)
    ar.poll_once(db, http=bot)
    rid = ar.recipients_for("acme", db)[0]["recipient_id"]
    cfg = SimpleNamespace(TELEGRAM_CHAT_ID="999")
    assert "111" in ar.chat_ids_for("acme", db_path=db, config=cfg, alert_type="covers")
    ar.set_alert_types(rid, ["production"], db_path=db)
    assert "111" not in ar.chat_ids_for("acme", db_path=db, config=cfg, alert_type="covers")
    assert "111" in ar.chat_ids_for("acme", db_path=db, config=cfg, alert_type="production")


def test_system_alerts_never_reach_client_chats(env):
    db, bot = env
    link = ar.create_link("acme", db_path=db, http=bot)
    bot.user_says(111, f"/start {link['code']}", 1)
    ar.poll_once(db, http=bot)
    cfg = SimpleNamespace(TELEGRAM_CHAT_ID="999")
    assert ar.chat_ids_for("acme", db_path=db, config=cfg, alert_type="system") == ["999"]


def test_operator_env_chat_gets_a_checklist_row(env):
    db, bot = env
    cfg = SimpleNamespace(TELEGRAM_CHAT_ID="999")
    rid = ar.ensure_operator_recipient(db, config=cfg)
    assert ar.ensure_operator_recipient(db, config=cfg) == rid            # idempotent
    ar.set_alert_types(rid, ["system"], db_path=db)
    assert ar.chat_ids_for(None, db_path=db, config=cfg, alert_type="covers") == []
    assert ar.chat_ids_for(None, db_path=db, config=cfg, alert_type="system") == ["999"]


def test_alert_key_to_type():
    assert ar.alert_type_of("production:paused:acme") == "production"
    assert ar.alert_type_of("cover:abc") == "covers" and ar.alert_type_of("postcover:x") == "covers"
    assert ar.alert_type_of("disk:low") == "system"
