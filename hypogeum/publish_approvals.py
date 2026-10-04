"""
hypogeum/publish_approvals.py — approval before publishing, per Workspace /
channel. Multi-tenant plan, Phase 4d (docs/plans/MULTI_TENANT_PLAN.md).

When approval is required for a channel, a finished episode that passed the
production output gate is NOT sent to the publishing provider until someone
approves it. The request goes to Telegram (the video, or its cover, with the
title and description and Approve / Reject buttons) — to the owning
workspace's chats and the operator's — and is listed on the dashboard's
Workspaces page with the same two buttons.

Gate seam: zernio_channel_release_trigger.process_channel_ready_bundles(),
after the output gate and before any staging / Zernio post — so nothing
(including the early private YouTube upload of the cover hold) happens
before approval.

Settings (operator decision 2026-10-04): per workspace, overridable per
channel —
    required       bool    (default False; the operator's own channels stay off)
    on_timeout     'hold' (default: never publish without approval)
                   | 'publish' (no answer within timeout_hours = approved)
    timeout_hours  float   (default 24; with 'hold' it only drives the reminder)

    settings_for(channel_id) -> dict
    set_settings(scope, scope_id, *, required, on_timeout, timeout_hours)
    gate(channel_id, episode_id, preview, *, now=None, notify=None) -> 'proceed' | 'hold'
    decide(approval_id, decision, decided_by, *, note=None) -> bool
    handle_callback(chat_id, data, user_label) -> str      # Telegram button press
    list_for(workspace_id, statuses=('pending',)) -> list[dict]
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from hypogeum.logger import get_logger

log = get_logger(agent="publish_approvals")

DEFAULTS = {"required": False, "on_timeout": "hold", "timeout_hours": 24.0}
_TG_VIDEO_LIMIT = 49 * 1024 * 1024

_DDL = """
CREATE TABLE IF NOT EXISTS approval_settings (
    scope          TEXT NOT NULL CHECK(scope IN ('workspace','channel')),
    scope_id       TEXT NOT NULL,
    required       INTEGER NOT NULL DEFAULT 0,
    on_timeout     TEXT NOT NULL DEFAULT 'hold' CHECK(on_timeout IN ('hold','publish')),
    timeout_hours  REAL NOT NULL DEFAULT 24,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (scope, scope_id)
);
CREATE TABLE IF NOT EXISTS publish_approvals (
    approval_id    TEXT NOT NULL PRIMARY KEY,
    workspace_id   TEXT,
    channel_id     TEXT NOT NULL,
    episode_id     TEXT NOT NULL,
    status         TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','auto_published')),
    title          TEXT,
    description    TEXT,
    cover_path     TEXT,
    video_path     TEXT,
    requested_at   TEXT NOT NULL,
    decided_at     TEXT,
    decided_by     TEXT,
    note           TEXT,
    reminded_at    TEXT,
    UNIQUE (channel_id, episode_id)
);
"""


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


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def _row_settings(row) -> dict:
    return {"required": bool(row["required"]), "on_timeout": row["on_timeout"],
            "timeout_hours": float(row["timeout_hours"])}


def get_settings(scope: str, scope_id: str, db_path=None) -> Optional[dict]:
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM approval_settings WHERE scope=? AND scope_id=?", (scope, scope_id)).fetchone()
    finally:
        conn.close()
    return _row_settings(row) if row else None


def set_settings(scope: str, scope_id: str, *, required: bool, on_timeout: str = "hold",
                 timeout_hours: float = 24.0, db_path=None) -> None:
    if scope not in ("workspace", "channel"):
        raise ValueError("scope must be 'workspace' or 'channel'")
    if on_timeout not in ("hold", "publish"):
        raise ValueError("on_timeout must be 'hold' or 'publish'")
    hours = max(1.0, min(float(timeout_hours), 24.0 * 14))
    conn = _conn(db_path)
    try:
        conn.execute(
            "INSERT INTO approval_settings (scope, scope_id, required, on_timeout, timeout_hours, updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(scope, scope_id) DO UPDATE SET required=excluded.required, "
            "on_timeout=excluded.on_timeout, timeout_hours=excluded.timeout_hours, updated_at=excluded.updated_at",
            (scope, scope_id, 1 if required else 0, on_timeout, hours, _now().isoformat()))
        conn.commit()
    finally:
        conn.close()
    log.info("approval_settings_set", scope=scope, scope_id=scope_id, required=required, on_timeout=on_timeout)


def settings_for(channel_id: str, db_path=None) -> dict:
    """Channel override, else its workspace's setting, else the defaults."""
    own = get_settings("channel", channel_id, db_path)
    if own:
        return own
    from hypogeum.workspaces import workspace_of
    ws = workspace_of(channel_id, db_path=_p(db_path))
    return (get_settings("workspace", ws, db_path) if ws else None) or dict(DEFAULTS)


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------

def _get(conn, channel_id, episode_id):
    return conn.execute("SELECT * FROM publish_approvals WHERE channel_id=? AND episode_id=?",
                        (channel_id, episode_id)).fetchone()


def gate(channel_id: str, episode_id: str, preview: dict, *, db_path=None,
         now: Optional[datetime] = None, notify: Optional[Callable[[dict, str], None]] = None) -> str:
    """'proceed' or 'hold' for this episode. preview = {title, description,
    cover_path, video_path}. Creates (and sends) the request on first sight.
    notify(approval_row, kind) — kind in request|reminder|auto_published —
    defaults to Telegram."""
    now = now or _now()
    st = settings_for(channel_id, db_path)
    if not st["required"]:
        return "proceed"
    notify = notify if notify is not None else _telegram_notify
    conn = _conn(db_path)
    try:
        row = _get(conn, channel_id, episode_id)
        if row is None:
            from hypogeum.workspaces import workspace_of
            aid = secrets.token_hex(6)
            conn.execute(
                "INSERT INTO publish_approvals (approval_id, workspace_id, channel_id, episode_id, status, title, "
                "description, cover_path, video_path, requested_at) VALUES (?,?,?,?, 'pending', ?,?,?,?,?)",
                (aid, workspace_of(channel_id, db_path=_p(db_path)), channel_id, episode_id,
                 preview.get("title"), preview.get("description"),
                 str(preview["cover_path"]) if preview.get("cover_path") else None,
                 str(preview["video_path"]) if preview.get("video_path") else None, now.isoformat()))
            conn.commit()
            row = _get(conn, channel_id, episode_id)
            log.info("publish_approval_requested", channel_id=channel_id, episode_id=episode_id)
            _safe(notify, dict(row), "request")
            return "hold"
        status = row["status"]
        if status in ("approved", "auto_published"):
            return "proceed"
        if status == "rejected":
            return "hold"
        age_h = (now - datetime.fromisoformat(row["requested_at"])).total_seconds() / 3600
        if st["on_timeout"] == "publish" and age_h >= st["timeout_hours"]:
            conn.execute("UPDATE publish_approvals SET status='auto_published', decided_at=?, decided_by=? "
                         "WHERE approval_id=? AND status='pending'",
                         (now.isoformat(), f"no answer within {st['timeout_hours']:g}h", row["approval_id"]))
            conn.commit()
            _safe(notify, dict(_get(conn, channel_id, episode_id)), "auto_published")
            return "proceed"
        remind_at_h = st["timeout_hours"] / 2 if st["on_timeout"] == "publish" else st["timeout_hours"]
        if not row["reminded_at"] and age_h >= remind_at_h:
            conn.execute("UPDATE publish_approvals SET reminded_at=? WHERE approval_id=?",
                         (now.isoformat(), row["approval_id"]))
            conn.commit()
            _safe(notify, dict(row), "reminder")
        return "hold"
    finally:
        conn.close()


def _safe(fn, row, kind):
    try:
        fn(row, kind)
    except Exception as exc:  # noqa: BLE001 — a failed notification never blocks the gate decision
        log.warn("publish_approval_notify_failed", kind=kind, error=str(exc)[:200])


def decide(approval_id: str, decision: str, decided_by: str, *, note: Optional[str] = None,
           db_path=None, notify: Optional[Callable[[dict, str], None]] = None) -> bool:
    """Approve / reject a pending request. Returns False if it was no longer
    pending (already decided — first answer wins)."""
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be 'approved' or 'rejected'")
    conn = _conn(db_path)
    try:
        n = conn.execute("UPDATE publish_approvals SET status=?, decided_at=?, decided_by=?, note=? "
                         "WHERE approval_id=? AND status='pending'",
                         (decision, _now().isoformat(), decided_by, note, approval_id)).rowcount
        conn.commit()
        row = conn.execute("SELECT * FROM publish_approvals WHERE approval_id=?", (approval_id,)).fetchone()
    finally:
        conn.close()
    if n:
        log.info("publish_approval_decided", approval_id=approval_id, decision=decision)
        _safe(notify if notify is not None else _telegram_notify, dict(row), decision)
    return bool(n)


def list_for(workspace_id: Optional[str] = None, statuses=("pending",), db_path=None, limit: int = 50) -> list:
    conn = _conn(db_path)
    try:
        q = "SELECT * FROM publish_approvals WHERE status IN (%s)" % ",".join("?" * len(statuses))
        args = list(statuses)
        if workspace_id:
            q += " AND workspace_id=?"
            args.append(workspace_id)
        q += " ORDER BY requested_at DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in conn.execute(q, args)]
    finally:
        conn.close()


def get(approval_id: str, db_path=None) -> Optional[dict]:
    conn = _conn(db_path)
    try:
        r = conn.execute("SELECT * FROM publish_approvals WHERE approval_id=?", (approval_id,)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _texts(row: dict, kind: str, lang: str) -> str:
    title = row.get("title") or row["episode_id"]
    desc = (row.get("description") or "").strip()
    if len(desc) > 600:
        desc = desc[:600] + "…"
    if lang == "ar":
        head = {"request": "🟡 حلقة جديدة محتاجة موافقة قبل النشر", "reminder": "⏰ تذكير: حلقة لسه مستنية موافقة",
                "auto_published": "✅ مفيش رد في الوقت المحدد، فالحلقة هتتنشر حسب الإعدادات",
                "approved": "✅ اتوافق على الحلقة وهتتنشر في ميعادها", "rejected": "⛔️ الحلقة اترفضت ومش هتتنشر"}[kind]
        return f"{head}\nالقناة: {row['channel_id']}\n\n«{title}»\n\n{desc}".strip()
    head = {"request": "🟡 New episode waiting for your approval before publishing",
            "reminder": "⏰ Reminder: an episode is still waiting for approval",
            "auto_published": "✅ No answer in time — publishing it as your settings say",
            "approved": "✅ Approved — it will publish at its scheduled time",
            "rejected": "⛔️ Rejected — it won't be published"}[kind]
    return f"{head}\nChannel: {row['channel_id']}\n\n“{title}”\n\n{desc}".strip()


def _telegram_notify(row: dict, kind: str) -> None:
    """Request/reminder: the video (or cover) + buttons to the workspace's and
    the operator's approval chats. Outcomes: a short text to the same chats."""
    from hypogeum import alert_recipients as ar
    cfg = ar._config()
    token = getattr(cfg, "TELEGRAM_BOT_TOKEN", None)
    if not token:
        return
    ws = row.get("workspace_id")
    operator = set(ar.chat_ids_for(None, alert_type="approvals"))
    lang = ar._lang(ws, None) if ws else "ar"
    buttons = None
    if kind in ("request", "reminder"):
        aid = row["approval_id"]
        approve = "✅ موافقة" if lang == "ar" else "✅ Approve"
        reject = "⛔️ رفض" if lang == "ar" else "⛔️ Reject"
        buttons = {"inline_keyboard": [[{"text": approve, "callback_data": f"appr:{aid}:a"},
                                        {"text": reject, "callback_data": f"appr:{aid}:r"}]]}
    for chat in ar.chat_ids_for(ws, alert_type="approvals"):
        text = _texts(row, kind, "ar" if chat in operator else lang)
        if kind == "request":
            _send_media(token, chat, row, text, buttons)
        else:
            payload = {"chat_id": chat, "text": text}
            if buttons:
                payload["reply_markup"] = buttons
            _post(token, "sendMessage", json_body=payload)


def _post(token: str, method: str, *, json_body=None, data=None, files=None) -> dict:
    import requests
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=json_body, data=data,
                      files=files, timeout=120)
    return r.json() if r.content else {}


def _send_media(token: str, chat: str, row: dict, caption: str, buttons) -> None:
    cap = caption[:1000]
    markup = json.dumps(buttons) if buttons else None
    video = Path(row["video_path"]) if row.get("video_path") else None
    cover = Path(row["cover_path"]) if row.get("cover_path") else None
    if video and video.exists() and video.stat().st_size <= _TG_VIDEO_LIMIT:
        with video.open("rb") as fh:
            res = _post(token, "sendVideo", data={"chat_id": chat, "caption": cap, "reply_markup": markup,
                                                  "supports_streaming": "true"}, files={"video": fh})
        if res.get("ok"):
            return
    if cover and cover.exists():
        with cover.open("rb") as fh:
            res = _post(token, "sendPhoto", data={"chat_id": chat, "caption": cap, "reply_markup": markup},
                        files={"photo": fh})
        if res.get("ok"):
            return
    _post(token, "sendMessage", json_body={"chat_id": chat, "text": caption, **({"reply_markup": buttons} if buttons else {})})


def handle_callback(chat_id: str, data: str, user_label: str, *, db_path=None,
                    notify: Optional[Callable[[dict, str], None]] = None) -> str:
    """A press on Approve / Reject. Only a chat that receives this workspace's
    approvals (its own chats or the operator's) may decide. Returns the short
    text for answerCallbackQuery."""
    from hypogeum import alert_recipients as ar
    try:
        _, aid, choice = data.split(":", 2)
    except ValueError:
        return "Unknown action"
    row = get(aid, db_path)
    if not row:
        return "This request no longer exists"
    allowed = set(ar.chat_ids_for(row.get("workspace_id"), db_path=db_path))
    if str(chat_id) not in allowed:
        log.warn("publish_approval_unauthorized_chat", approval_id=aid)
        return "This chat can't approve this episode"
    if row["status"] != "pending":
        return f"Already {row['status'].replace('_', ' ')}"
    ok = decide(aid, "approved" if choice == "a" else "rejected", f"telegram:{user_label or chat_id}",
                db_path=db_path, notify=notify)
    return ("Approved ✅" if choice == "a" else "Rejected ⛔️") if ok else "Already decided"
