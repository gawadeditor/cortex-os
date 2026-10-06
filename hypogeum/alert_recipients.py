"""
hypogeum/alert_recipients.py — who receives alerts, per Workspace, and how a
client connects Telegram without ever touching a bot token. Multi-tenant
plan, Phase 4 (docs/plans/MULTI_TENANT_PLAN.md).

One platform bot (TELEGRAM_BOT_TOKEN, @cortex_gawad_alerts_bot). To connect a
workspace the dashboard creates a one-time link code and shows
    https://t.me/<bot>?start=<code>
The client opens it and presses Start; the bot (polled from the dashboard
process — no webhook, no public URL needed) matches the code to its
workspace, stores the chat as a recipient and answers with a confirmation.
"/stop" in the chat disconnects it.

    create_link(workspace_id, ttl_minutes=30) -> {"code", "url", "expires_at"}
    recipients_for(workspace_id) -> list[dict]      # active telegram chats
    chat_ids_for(workspace_id, include_platform=True) -> list[str]
    disconnect(recipient_id) / send_test(recipient_id)
    poll_once(db_path=None, http=None) -> int        # updates handled
    start_bot_loop(db_path=None)                     # dashboard thread

The operator's chat from .env (TELEGRAM_CHAT_ID, set by
scripts/connect_telegram.py) is the platform workspace's implicit recipient,
so nothing changes for the operator. Platform recipients are copied on every
workspace alert (the operator runs the managed service and sees everything).
"""
from __future__ import annotations

import json
import secrets
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from hypogeum.logger import get_logger

log = get_logger(agent="alert_recipients")

_DDL = """
CREATE TABLE IF NOT EXISTS alert_recipients (
    recipient_id  TEXT NOT NULL PRIMARY KEY,
    workspace_id  TEXT NOT NULL,
    channel       TEXT NOT NULL DEFAULT 'telegram',
    address       TEXT NOT NULL,
    label         TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disconnected')),
    alert_types   TEXT,              -- JSON list; NULL = every type
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (workspace_id, channel, address)
);
CREATE TABLE IF NOT EXISTS alert_link_codes (
    code          TEXT NOT NULL PRIMARY KEY,
    workspace_id  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    used_at       TEXT,
    used_by       TEXT
);
"""

_bot_username_cache: dict = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _p(db_path):
    if db_path:
        return Path(db_path)
    import hypogeum.db as _db
    return Path(_db._DEFAULT_DB_PATH)


def _conn(db_path):
    from hypogeum.db import get_connection
    c = get_connection(_p(db_path))
    c.execute("PRAGMA busy_timeout = 15000")
    c.executescript(_DDL)
    return c


def _config():
    from hypogeum.config import Config
    return Config()


def _api(method: str, *, token: str, http=None, **params) -> dict:
    if http is not None:
        return http(method, params)
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=40))


def bot_username(token: Optional[str] = None, http=None) -> Optional[str]:
    token = token or getattr(_config(), "TELEGRAM_BOT_TOKEN", None)
    if not token:
        return None
    if token not in _bot_username_cache:
        _bot_username_cache[token] = _api("getMe", token=token, http=http)["result"]["username"]
    return _bot_username_cache[token]


# Alert types a chat can opt in/out of (dashboard checklist). Each operator
# alert key prefix maps to one type; "system" types are platform-only.
ALERT_TYPES = [
    ("production", "Production paused", "A workspace's new episodes stopped (failures, spend, budget)."),
    ("credits", "Provider balance & keys", "A provider balance is running out or a key stopped working."),
    ("covers", "YouTube covers", "A Short is waiting for its cover before it can go public."),
    ("posts", "Failed posts", "A post didn't go out on one of the platforms."),
    ("accounts", "Account connections", "A social account needs to be reconnected."),
    ("approvals", "Approvals", "An episode is waiting for approval before publishing."),
    ("system", "Factory health", "Worker down, disk space, archive storage, factory-wide blocks (platform only)."),
]
_PREFIX_TYPE = [("production:", "production"), ("run_failed:", "production"),
                ("unpublished:", "production"), ("repair:", "production"), ("openrouter:", "credits"), ("openrouter_ws:", "credits"), ("cover:", "covers"),
                ("postcover:", "covers"), ("post:", "posts"), ("account:", "accounts"),
                ("approval:", "approvals"), ("worker:", "system"), ("disk:", "system"),
                ("archive:", "system"), ("signal:", "system"), ("system:", "system")]


def alert_type_of(alert_key: str) -> str:
    for prefix, t in _PREFIX_TYPE:
        if alert_key.startswith(prefix):
            return t
    return "system"


def _types(row) -> set:
    raw = row["alert_types"] if "alert_types" in row.keys() else None
    if not raw:
        return {t for t, _, _ in ALERT_TYPES}
    try:
        return set(json.loads(raw))
    except Exception:  # noqa: BLE001
        return {t for t, _, _ in ALERT_TYPES}


def set_alert_types(recipient_id: str, types: list, db_path=None) -> None:
    valid = {t for t, _, _ in ALERT_TYPES}
    chosen = sorted(set(types) & valid)
    conn = _conn(db_path)
    try:
        conn.execute("UPDATE alert_recipients SET alert_types=?, updated_at=? WHERE recipient_id=?",
                     (json.dumps(chosen), _now().isoformat(), recipient_id))
        conn.commit()
    finally:
        conn.close()


def ensure_operator_recipient(db_path=None, config=None) -> Optional[str]:
    """The operator's .env chat (scripts/connect_telegram.py) as a normal
    platform-workspace recipient, so it gets a checklist like any other."""
    from hypogeum.workspaces import platform_workspace_id
    chat = getattr(config or _config(), "TELEGRAM_CHAT_ID", None)
    pid = platform_workspace_id(_p(db_path))
    if not chat or not pid:
        return None
    now = _now().isoformat()
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT recipient_id FROM alert_recipients WHERE workspace_id=? AND channel='telegram' "
                           "AND address=?", (pid, str(chat))).fetchone()
        if row:
            return row["recipient_id"]
        rid = secrets.token_hex(8)
        conn.execute("INSERT INTO alert_recipients (recipient_id, workspace_id, channel, address, label, status, "
                     "created_at, updated_at) VALUES (?,?,?,?,?,'active',?,?)",
                     (rid, pid, "telegram", str(chat), "Operator (this machine)", now, now))
        conn.commit()
        return rid
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# links & recipients
# ---------------------------------------------------------------------------

def create_link(workspace_id: str, *, ttl_minutes: int = 30, db_path=None, http=None) -> dict:
    from hypogeum.workspaces import list_workspaces
    if workspace_id not in {w["workspace_id"] for w in list_workspaces(_p(db_path))}:
        raise ValueError(f"unknown workspace {workspace_id!r}")
    code = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]
    now = _now()
    expires = (now + timedelta(minutes=ttl_minutes)).isoformat()
    conn = _conn(db_path)
    try:
        conn.execute("INSERT INTO alert_link_codes (code, workspace_id, created_at, expires_at) VALUES (?,?,?,?)",
                     (code, workspace_id, now.isoformat(), expires))
        conn.commit()
    finally:
        conn.close()
    user = bot_username(http=http)
    url = f"https://t.me/{user}?start={urllib.parse.quote(code)}" if user else None
    log.info("alert_link_created", workspace_id=workspace_id)
    return {"code": code, "url": url, "expires_at": expires, "bot": user}


def recipients_for(workspace_id: str, db_path=None) -> list[dict]:
    conn = _conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM alert_recipients WHERE workspace_id=? AND status='active' ORDER BY created_at",
            (workspace_id,))]
    finally:
        conn.close()


def all_recipients(db_path=None) -> list[dict]:
    conn = _conn(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM alert_recipients ORDER BY workspace_id, created_at")]
    finally:
        conn.close()


def chat_ids_for(workspace_id: Optional[str], *, include_platform: bool = True, db_path=None,
                 config=None, alert_type: Optional[str] = None) -> list[str]:
    """Telegram chats an alert for `workspace_id` goes to: that workspace's
    connected chats, plus (include_platform) the platform workspace's chats
    and the operator's .env chat. workspace_id None = platform only.
    alert_type filters by each chat's checklist; 'system' alerts never go
    to a client workspace's chats."""
    from hypogeum.workspaces import platform_workspace_id
    pid = platform_workspace_id(_p(db_path))
    out: list[str] = []
    targets = []
    if workspace_id and workspace_id != pid and alert_type != "system":
        targets.append(workspace_id)
    if include_platform or not workspace_id or workspace_id == pid:
        if pid:
            targets.append(pid)
        env_chat = getattr(config or _config(), "TELEGRAM_CHAT_ID", None)
        if env_chat:
            # the operator chat, unless its own checklist row opts out of this type
            row = next((r for r in (recipients_for(pid, db_path) if pid else []) if r["address"] == str(env_chat)), None)
            if alert_type is None or row is None or alert_type in _types(row):
                out.append(str(env_chat))
    for wid in targets:
        out += [r["address"] for r in recipients_for(wid, db_path)
                if r["channel"] == "telegram" and (alert_type is None or alert_type in _types(r))]
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def disconnect(recipient_id: str, db_path=None) -> bool:
    conn = _conn(db_path)
    try:
        n = conn.execute("UPDATE alert_recipients SET status='disconnected', updated_at=? WHERE recipient_id=?",
                         (_now().isoformat(), recipient_id)).rowcount
        conn.commit()
    finally:
        conn.close()
    return bool(n)


def send_to_chat(chat_id: str, text: str, *, http=None, config=None, reply_markup=None) -> bool:
    token = getattr(config or _config(), "TELEGRAM_BOT_TOKEN", None)
    if not token:
        return False
    try:
        extra = {"reply_markup": reply_markup} if reply_markup else {}
        return bool(_api("sendMessage", token=token, http=http, chat_id=chat_id, text=text, **extra).get("ok"))
    except Exception as exc:  # noqa: BLE001
        log.warn("telegram_send_failed", error=str(exc)[:200])
        return False


def send_test(recipient_id: str, db_path=None, http=None) -> bool:
    conn = _conn(db_path)
    try:
        r = conn.execute("SELECT * FROM alert_recipients WHERE recipient_id=?", (recipient_id,)).fetchone()
    finally:
        conn.close()
    if not r or r["status"] != "active":
        return False
    return send_to_chat(r["address"], _msg(r["workspace_id"], "test", db_path), http=http)


# ---------------------------------------------------------------------------
# bot side
# ---------------------------------------------------------------------------

def _lang(workspace_id: Optional[str], db_path) -> str:
    try:
        from hypogeum.workspaces import list_workspaces
        for w in list_workspaces(_p(db_path)):
            if w["workspace_id"] == workspace_id:
                return (w.get("language") or "en")[:2]
    except Exception:  # noqa: BLE001
        pass
    return "en"


_MSGS = {
    "connected": {"ar": "✅ تليجرام اتربط بـ {name}. هيوصلك هنا أي حاجة توقّف الإنتاج أو محتاجة منك تدخّل. لإلغاء الربط ابعت /stop.",
                  "en": "✅ Telegram is now connected to {name}. You'll get a message here whenever something blocks "
                        "production or needs your action. Send /stop to disconnect."},
    "test": {"ar": "رسالة تجربة من Cortex: التنبيهات شغالة ✅", "en": "Test message from Cortex: alerts are working ✅"},
    "bad_code": {"ar": "اللينك ده انتهى أو اتستخدم قبل كده. اعمل لينك جديد من الداشبورد.",
                 "en": "This link has expired or was already used. Create a new one from the dashboard."},
    "no_code": {"ar": "علشان تربط التنبيهات، استخدم زرار Connect Telegram في الداشبورد.",
                "en": "To connect alerts, use the Connect Telegram button in your Cortex dashboard."},
    "stopped": {"ar": "اتلغى ربط التنبيهات للمحادثة دي.", "en": "Alerts are disconnected for this chat."},
}


def _msg(workspace_id, key, db_path, **kw) -> str:
    return _MSGS[key][_lang(workspace_id, db_path) if workspace_id else "en"].format(**kw)


def _workspace_name(workspace_id, db_path) -> str:
    from hypogeum.workspaces import list_workspaces
    for w in list_workspaces(_p(db_path)):
        if w["workspace_id"] == workspace_id:
            return w["name"]
    return workspace_id


def handle_message(chat: dict, text: str, *, db_path=None, reply: Callable[[str, str], bool]) -> Optional[str]:
    """Process one private message. Returns the workspace connected, if any."""
    if chat.get("type") != "private":
        return None
    chat_id = str(chat["id"])
    text = (text or "").strip()
    if text.startswith("/stop"):
        conn = _conn(db_path)
        try:
            conn.execute("UPDATE alert_recipients SET status='disconnected', updated_at=? "
                         "WHERE address=? AND status='active'", (_now().isoformat(), chat_id))
            conn.commit()
        finally:
            conn.close()
        reply(chat_id, _msg(None, "stopped", db_path))
        return None
    if not text.startswith("/start"):
        return None
    parts = text.split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""
    if not code:
        reply(chat_id, _msg(None, "no_code", db_path))
        return None
    now = _now()
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM alert_link_codes WHERE code=?", (code,)).fetchone()
        if not row or row["used_at"] or row["expires_at"] < now.isoformat():
            conn.close()
            reply(chat_id, _msg(None, "bad_code", db_path))
            return None
        wid = row["workspace_id"]
        label = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username")
        conn.execute("UPDATE alert_link_codes SET used_at=?, used_by=? WHERE code=? AND used_at IS NULL",
                     (now.isoformat(), chat_id, code))
        conn.execute(
            "INSERT INTO alert_recipients (recipient_id, workspace_id, channel, address, label, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,'active',?,?) ON CONFLICT(workspace_id, channel, address) DO UPDATE SET "
            "status='active', label=excluded.label, updated_at=excluded.updated_at",
            (secrets.token_hex(8), wid, "telegram", chat_id, label, now.isoformat(), now.isoformat()))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    reply(chat_id, _msg(wid, "connected", db_path, name=_workspace_name(wid, db_path)))
    log.info("alert_recipient_connected", workspace_id=wid)
    return wid


def _get_offset(db_path) -> int:
    conn = _conn(db_path)
    try:
        r = conn.execute("SELECT value FROM worker_control WHERE key='telegram_update_offset'").fetchone()
        return int(r[0]) if r and r[0] else 0
    finally:
        conn.close()


def _save_offset(db_path, offset: int) -> None:
    """Raises if it can't be saved — the caller must then NOT handle the update."""
    conn = _conn(db_path)   # busy_timeout 15s: waits out a busy DB instead of failing at once
    try:
        conn.execute("INSERT INTO worker_control (key, value, updated_at) VALUES ('telegram_update_offset', ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                     (str(offset), _now().isoformat()))
        conn.commit()
    finally:
        conn.close()


def poll_once(db_path=None, *, http=None, timeout: int = 0) -> int:
    """Fetch pending bot updates once and handle each AT MOST ONCE: the
    update's offset is saved BEFORE it is handled. 2026-10-04: handling
    first and saving after meant a busy DB ('database is locked' while an
    episode was producing) made the bot re-handle the same /status 27 times
    — and a button like 'restart worker' would have run 27 times too."""
    token = getattr(_config(), "TELEGRAM_BOT_TOKEN", None)
    if not token and http is None:
        return 0
    offset = _get_offset(db_path)
    params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset:
        params["offset"] = offset
    updates = _api("getUpdates", token=token, http=http, **params).get("result", [])
    handled = 0
    for upd in updates:
        offset = max(offset, upd["update_id"] + 1)
        _save_offset(db_path, offset)       # first; if this raises, nothing below runs
        msg = upd.get("message") or {}
        try:
            if upd.get("callback_query"):
                _handle_button(upd["callback_query"], token=token, http=http, db_path=db_path)
            else:
                from hypogeum.operator_actions import handle_command
                res = handle_command(str((msg.get("chat") or {}).get("id", "")), msg.get("text"), db_path=db_path) \
                    if (msg.get("chat") or {}).get("type") == "private" else None
                if res:
                    send_to_chat(str(msg["chat"]["id"]), res[0], http=http, reply_markup=res[1])
                else:
                    handle_message(msg.get("chat") or {}, msg.get("text") or "", db_path=db_path,
                                   reply=lambda cid, t: send_to_chat(cid, t, http=http))
            handled += 1
        except Exception as exc:  # noqa: BLE001
            log.warn("telegram_update_failed", error=str(exc)[:200])
    return handled


def _handle_button(cq: dict, *, token, http, db_path) -> None:
    """Inline-button press (publish approval: data 'appr:<id>:a|r')."""
    msg = cq.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    user = cq.get("from") or {}
    label = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or user.get("username") or chat_id
    data = cq.get("data") or ""
    answer = "Unknown action"
    if data.startswith("act:"):
        # operator action from an alert's button: run it, report the outcome
        # as a message (can be long), acknowledge the press briefly
        from hypogeum.operator_actions import handle as _act
        _api("answerCallbackQuery", token=token, http=http, callback_query_id=cq.get("id"), text="⏳")
        result = _act(chat_id, data, db_path=db_path)
        markup = None
        if data.startswith("act:status:"):
            from hypogeum.operator_actions import status_text
            result, markup = status_text(db_path)
        send_to_chat(chat_id, result, http=http, reply_markup=markup)
        return
    if data.startswith("appr:"):
        from hypogeum.publish_approvals import handle_callback
        answer = handle_callback(chat_id, data, label, db_path=db_path)
    _api("answerCallbackQuery", token=token, http=http, callback_query_id=cq.get("id"), text=answer[:190])
    if msg.get("message_id") and chat_id:
        try:   # the decision is made (or refused): drop the buttons from this copy
            _api("editMessageReplyMarkup", token=token, http=http, chat_id=chat_id,
                 message_id=msg["message_id"], reply_markup={"inline_keyboard": []})
        except Exception:  # noqa: BLE001
            pass


def start_bot_loop(db_path=None) -> Optional[threading.Thread]:
    if not getattr(_config(), "TELEGRAM_BOT_TOKEN", None):
        return None
    stop = threading.Event()
    try:   # the bot's command menu in Telegram
        _api("setMyCommands", token=getattr(_config(), "TELEGRAM_BOT_TOKEN", None), commands=[
            {"command": "menu", "description": "قايمة الزراير"},
            {"command": "status", "description": "حالة المصنع"}])
    except Exception as exc:  # noqa: BLE001
        log.warn("telegram_set_commands_failed", error=str(exc)[:200])

    def _loop():
        while not stop.is_set():
            try:
                poll_once(db_path, timeout=25)
            except Exception as exc:  # noqa: BLE001
                log.warn("telegram_poll_failed", error=str(exc)[:200])
                stop.wait(30)

    t = threading.Thread(target=_loop, name="telegram-bot", daemon=True)
    t.stop_event = stop  # type: ignore[attr-defined]
    t.start()
    log.info("telegram_bot_loop_started")
    return t
