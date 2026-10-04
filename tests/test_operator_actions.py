"""hypogeum.operator_actions — act on alerts from Telegram (operator only)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hypogeum import alert_recipients as ar
from hypogeum import operator_actions as oa
from hypogeum import workspaces as ws
from hypogeum.db import get_connection, get_worker_control, initialize_schema, set_worker_control
from hypogeum.factory_entity import create_factory


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import hypogeum.db as hdb
    p = tmp_path / "t.db"
    initialize_schema(p)
    monkeypatch.setattr(hdb, "_DEFAULT_DB_PATH", p)
    ws._READY.discard(str(p))
    create_factory("op", "Operator", "op", db_path=p)
    ws.backfill_platform_workspace(p)
    ws.create_workspace("Acme", workspace_id="acme", db_path=p)
    monkeypatch.setattr(ar, "_config", lambda: SimpleNamespace(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="999"))
    conn = ar._conn(p)
    conn.execute("INSERT INTO alert_recipients (recipient_id, workspace_id, channel, address, status, created_at, "
                 "updated_at) VALUES ('r1','acme','telegram','111','active','x','x')")
    conn.commit()
    conn.close()
    return p


def _cb(btns):
    return [b.get("callback_data") for row in btns["inline_keyboard"] for b in row if b.get("callback_data")]


def test_every_actionable_alert_has_a_button(db):
    assert _cb(oa.buttons_for("production:paused", db))[0] == "act:resume:"
    assert _cb(oa.buttons_for("production:paused:acme", db)) == ["act:resume:acme"]
    assert _cb(oa.buttons_for("worker:down", db))[0] == "act:restart_worker:"
    assert _cb(oa.buttons_for("archive:down", db)) == ["act:mount_archive:"]
    assert _cb(oa.buttons_for("disk:low", db)) == ["act:disk:"]
    assert _cb(oa.buttons_for("system:adapters_stale", db)) == ["act:adapters:"]
    assert _cb(oa.buttons_for("cover:abc123", db)) == ["act:cover_done:abc123"]
    assert oa.buttons_for("openrouter:credits", db)["inline_keyboard"][0][0]["url"].startswith("https://openrouter.ai")


def test_only_operator_chats_can_act(db):
    set_worker_control("queue_paused", "1", db_path=db)
    assert "can't" in oa.handle("111", "act:resume:", db_path=db)          # client chat
    assert get_worker_control("queue_paused", "0", db_path=db) == "1"
    assert oa.handle("999", "act:resume:", db_path=db).startswith("▶️")
    assert get_worker_control("queue_paused", "0", db_path=db) == "0"


def test_restart_worker_runs_launchctl(db, monkeypatch):
    calls = []
    monkeypatch.setattr(oa, "_RUN", lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert oa.handle("999", "act:restart_worker:", db_path=db).startswith("🔄")
    assert calls[0][:3] == ["launchctl", "kickstart", "-k"] and calls[0][3].endswith("com.hypogeum.worker")


def test_cover_done_records_confirmation(db):
    conn = get_connection(db)
    conn.executescript("""CREATE TABLE IF NOT EXISTS youtube_cover_holds (post_id TEXT PRIMARY KEY, channel_id TEXT,
        episode_id TEXT, release_at TEXT, upload_at TEXT, status TEXT, held_at TEXT, released_at TEXT, last_error TEXT);
        INSERT INTO youtube_cover_holds VALUES ('p1','hypogeum','ep','r','u','held','h',NULL,NULL);""")
    conn.commit()
    conn.close()
    assert oa.handle("999", "act:cover_done:p1", db_path=db).startswith("✅")
    conn = get_connection(db)
    assert conn.execute("SELECT channel_id FROM youtube_cover_confirmations WHERE post_id='p1'").fetchone()[0] == "hypogeum"
    conn.close()


def test_status_and_help_commands(db):
    set_worker_control("queue_paused", "1", db_path=db)
    set_worker_control("queue_paused_reason", "3 runs failed", db_path=db)
    text, markup = oa.handle_command("999", "/status", db_path=db)
    assert "الإنتاج: ⛔️ واقف" in text and "act:resume:" in _cb(markup)
    assert oa.handle_command("999", "/help", db_path=db)[0].startswith("أوامر")
    assert "only receives" in oa.handle_command("111", "/status", db_path=db)[0]
    assert oa.handle_command("999", "hello", db_path=db) is None


def test_resolved_message_not_sent_when_alert_never_arrived(db):
    from hypogeum import operator_alerts as alerts
    sent = []
    alerts.alert("worker:down", "الـ worker مش شغال", "b", db_path=db, send=lambda t: False)   # network down
    alerts.resolve("worker:down", db_path=db, send=lambda t: sent.append(t) or True)
    assert sent == []
