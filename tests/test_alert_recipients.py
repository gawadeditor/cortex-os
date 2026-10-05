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
        if method in ("answerCallbackQuery", "editMessageReplyMarkup"):
            self.sent.append((method, params.get("text") or params.get("reply_markup")))
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


def test_approve_button_press_through_the_bot(env, monkeypatch):
    db, bot = env
    from hypogeum import publish_approvals as pa
    monkeypatch.setattr(pa, "_telegram_notify", lambda row, kind: None)   # no real network in tests
    from hypogeum import workspaces as ws
    ws.assign("channel", "acme_ch", "acme", db_path=db)
    link = ar.create_link("acme", db_path=db, http=bot)
    bot.user_says(111, f"/start {link['code']}", 1)
    ar.poll_once(db, http=bot)
    pa.set_settings("workspace", "acme", required=True, db_path=db)
    pa.gate("acme_ch", "ep1", {"title": "t"}, db_path=db, notify=lambda r, k: None)
    aid = pa.list_for("acme", db_path=db)[0]["approval_id"]
    bot.updates.append({"update_id": 2, "callback_query": {
        "id": "cq1", "data": f"appr:{aid}:a", "from": {"first_name": "Client"},
        "message": {"message_id": 7, "chat": {"id": 111, "type": "private"}}}})
    ar.poll_once(db, http=bot)
    assert pa.get(aid, db)["status"] == "approved"
    assert ("answerCallbackQuery", "Approved ✅") in bot.sent


def test_status_command_and_action_button_through_the_bot(env):
    db, bot = env
    from hypogeum.db import get_worker_control, set_worker_control
    set_worker_control("queue_paused", "1", db_path=db)
    bot.user_says(999, "/status", 1)                       # the operator's .env chat
    ar.poll_once(db, http=bot)
    status_msg = [t for c, t in bot.sent if c == "999"][-1]
    assert "حالة المصنع" in status_msg
    bot.updates.append({"update_id": 2, "callback_query": {
        "id": "cq", "data": "act:resume:", "from": {"first_name": "Op"},
        "message": {"message_id": 5, "chat": {"id": 999, "type": "private"}}}})
    ar.poll_once(db, http=bot)
    assert get_worker_control("queue_paused", "0", db_path=db) == "0"
    assert [t for c, t in bot.sent if c == "999"][-1].startswith("▶️")


def test_update_handled_at_most_once_even_if_offset_save_fails(env, monkeypatch):
    """2026-10-04: a busy DB made the bot answer the same /status 27 times."""
    db, bot = env
    bot.user_says(999, "/status", 1)
    real_save = ar._save_offset
    calls = {"n": 0}

    def flaky_save(db_path, offset):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real_save(db_path, offset)
    monkeypatch.setattr(ar, "_save_offset", flaky_save)
    with pytest.raises(RuntimeError):
        ar.poll_once(db, http=bot)
    assert [c for c, _ in bot.sent if c == "999"] == []      # not answered while unsaved
    ar.poll_once(db, http=bot)
    ar.poll_once(db, http=bot)
    assert len([c for c, _ in bot.sent if c == "999"]) == 1  # answered exactly once
