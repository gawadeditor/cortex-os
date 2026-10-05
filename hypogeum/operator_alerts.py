"""
hypogeum/operator_alerts.py — tell the operator about blockers on Telegram,
once per problem, and once more when it is resolved.

Why a separate layer from hypogeum.notifications.route_signal(): OIS signals
are telemetry (disk_low / repeated_failure re-emitted every ~15 min, generic
run_failed), so routing them to a phone is noise, while the real blockers of
2026-10-03 (OpenRouter credits exhausted, the spend breaker pausing
production, a Short waiting for its YouTube cover) raised nothing specific.
This module runs a small set of checks, each answering "does the operator
need to act?", keyed so a problem is sent once and its resolution once.

    run_operator_checks(db_path=None, config=None, now=None) -> dict
    alert(key, title, body, ...) / resolve(key, note, ...)   (also usable directly)

Messages are written in Arabic (operator chat language); keys are stable
English identifiers. Delivery uses notifications.send_telegram with
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from Config; when those are unset the
state is still tracked (so the dashboard/tests see it) and nothing is sent.
Never raises.
"""
from __future__ import annotations

import json
import os
import shutil
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from hypogeum.logger import get_logger

log = get_logger(agent="operator_alerts")

_DDL = """
CREATE TABLE IF NOT EXISTS operator_alerts (
    alert_key     TEXT NOT NULL PRIMARY KEY,
    status        TEXT NOT NULL CHECK(status IN ('open','resolved')),
    title         TEXT NOT NULL,
    body          TEXT,
    opened_at     TEXT NOT NULL,
    resolved_at   TEXT,
    last_sent_at  TEXT,
    send_ok       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS operator_alert_checks (
    check_name    TEXT NOT NULL PRIMARY KEY,
    last_run_at   TEXT NOT NULL
);
"""

# per-check minimum interval (seconds) — run_operator_checks is called from
# every worker iteration (~60s); the network-backed checks run less often.
_INTERVALS = {"adapters": 300, "run_failures": 60, "openrouter_credits": 1800, "workspace_credits": 1800, "worker": 300, "production_paused": 60, "blocking_signals": 120,
              "youtube_covers": 300, "posts": 1800, "accounts": 600, "archive": 300, "disk": 1800}
_DASHBOARD = "http://127.0.0.1:5001"
_CREDIT_LOW_USD = 3.0
_DISK_LOW_GB, _DISK_OK_GB = 30.0, 40.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _conn(db_path, *, ddl: bool = True):
    from hypogeum.db import get_connection
    c = get_connection(Path(db_path) if db_path else None)
    # wait out a concurrent writer (worker / cover-hold loop / sleep-wake
    # burst) instead of failing the check with "database is locked"
    c.execute("PRAGMA busy_timeout = 15000")
    if ddl:
        c.executescript(_DDL)
        cols = {r[1] for r in c.execute("PRAGMA table_info(operator_alerts)")}
        for col in ("workspace_id", "title_en", "body_en"):   # multi-tenant Phase 4
            if col not in cols:
                c.execute(f"ALTER TABLE operator_alerts ADD COLUMN {col} TEXT")
    return c


def _deliver(key: str, workspace_id: Optional[str], ar_text: str, en_text: Optional[str],
             config, db_path, *, with_buttons: bool = True) -> bool:
    """Send to every chat that should get this alert (hypogeum.alert_recipients):
    the workspace's own chats in the workspace's language, the operator's
    chats in Arabic; each chat's alert-type checklist applies."""
    from hypogeum import alert_recipients as ar
    cfg = _config(config)
    atype = ar.alert_type_of(key)
    operator_chats = set(ar.chat_ids_for(None, db_path=db_path, config=cfg, alert_type=atype))
    chats = ar.chat_ids_for(workspace_id, db_path=db_path, config=cfg, alert_type=atype)
    lang = ar._lang(workspace_id, db_path) if workspace_id else "ar"
    # the operator gets buttons that act on the alert (hypogeum.operator_actions);
    # a client chat gets the message only
    try:
        from hypogeum.operator_actions import buttons_for
        op_buttons = buttons_for(key, db_path) if with_buttons else None
    except Exception:  # noqa: BLE001
        op_buttons = None
    ok_any = False
    for chat in chats:
        is_op = chat in operator_chats
        text = ar_text if (is_op or lang != "en" or not en_text) else en_text
        ok_any = ar.send_to_chat(chat, text, config=cfg, reply_markup=op_buttons if is_op else None) or ok_any
    return ok_any


def _config(config):
    if config is not None:
        return config
    try:
        from hypogeum.config import Config
        return Config()
    except Exception:  # noqa: BLE001
        return None


def alert(key: str, title: str, body: str = "", *, db_path=None, config=None,
          send: Optional[Callable[[str], bool]] = None, workspace_id: Optional[str] = None,
          title_en: Optional[str] = None, body_en: Optional[str] = None) -> bool:
    """Open an alert and send it — only if it isn't already open.
    Returns True when a new alert was opened. workspace_id routes it to that
    workspace's chats too (title_en/body_en for an English workspace); a
    given `send` overrides routing (single sink, used by tests)."""
    try:
        conn = _conn(db_path)
        try:
            row = conn.execute("SELECT status FROM operator_alerts WHERE alert_key=?", (key,)).fetchone()
            if row and row["status"] == "open":
                return False
            # Mark open first, then send: if the DB write fails nothing is
            # sent, so a lock can never turn into a repeated message.
            now = _now().isoformat()
            conn.execute(
                "INSERT INTO operator_alerts (alert_key, status, title, body, opened_at, resolved_at, last_sent_at, "
                "send_ok, workspace_id, title_en, body_en) VALUES (?,?,?,?,?,NULL,?,0,?,?,?) "
                "ON CONFLICT(alert_key) DO UPDATE SET status='open', title=excluded.title, "
                "body=excluded.body, opened_at=excluded.opened_at, resolved_at=NULL, "
                "last_sent_at=excluded.last_sent_at, send_ok=0, "
                "workspace_id=excluded.workspace_id, title_en=excluded.title_en, body_en=excluded.body_en",
                (key, "open", title, body, now, now, workspace_id, title_en, body_en))
            conn.commit()
            ar_text = f"🔴 {title}\n\n{body}".strip()
            en_text = f"🔴 {title_en}\n\n{body_en or ''}".strip() if title_en else None
            if send is not None:
                ok = bool(send(ar_text))
            else:
                ok = _deliver(key, workspace_id, ar_text, en_text, config, db_path)
            conn.execute("UPDATE operator_alerts SET send_ok=? WHERE alert_key=?", (1 if ok else 0, key))
            conn.commit()
        finally:
            conn.close()
        log.info("operator_alert_opened", key=key, sent=ok)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warn("operator_alert_failed", key=key, error=str(exc)[:200])
        return False


def resolve(key: str, note: str = "", *, db_path=None, config=None,
            send: Optional[Callable[[str], bool]] = None) -> bool:
    """Close an open alert and send one 'resolved' message. Returns True if
    something was open."""
    try:
        conn = _conn(db_path)
        try:
            row = conn.execute("SELECT * FROM operator_alerts WHERE alert_key=?", (key,)).fetchone()
            if not row or row["status"] != "open":
                return False
            conn.execute("UPDATE operator_alerts SET status='resolved', resolved_at=? WHERE alert_key=?",
                         (_now().isoformat(), key))
            conn.commit()   # before sending, as in alert()
            ar_text = f"🟢 اتحلت: {row['title']}" + (f"\n{note}" if note else "")
            en_text = f"🟢 Resolved: {row['title_en']}" if row["title_en"] else None
            if not row["send_ok"] or key.startswith("run_failed:"):
                pass   # never reached anyone (2026-10-04) / a failure report needs no "resolved" follow-up
            elif send is not None:
                send(ar_text)
            else:
                _deliver(key, row["workspace_id"], ar_text, en_text, config, db_path, with_buttons=False)
        finally:
            conn.close()
        log.info("operator_alert_resolved", key=key)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warn("operator_alert_resolve_failed", key=key, error=str(exc)[:200])
        return False


def open_alert_keys(prefix: str = "", db_path=None) -> set:
    conn = _conn(db_path)
    try:
        return {r["alert_key"] for r in conn.execute(
            "SELECT alert_key FROM operator_alerts WHERE status='open' AND alert_key LIKE ?", (prefix + "%",))}
    finally:
        conn.close()


def _due(name: str, now: datetime, db_path) -> bool:
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT last_run_at FROM operator_alert_checks WHERE check_name=?", (name,)).fetchone()
        if row and (now - datetime.fromisoformat(row["last_run_at"])).total_seconds() < _INTERVALS.get(name, 300):
            return False
        conn.execute("INSERT INTO operator_alert_checks (check_name, last_run_at) VALUES (?,?) "
                     "ON CONFLICT(check_name) DO UPDATE SET last_run_at=excluded.last_run_at", (name, now.isoformat()))
        conn.commit()
        return True
    finally:
        conn.close()


def _sync(prefix: str, current: dict, db_path, config, send, resolved_note: str = "") -> None:
    """current = {key: (title, body)} for platform alerts, or
    {key: (title, body, {"ws": workspace_id, "en": (title_en, body_en)})} for
    a workspace's own — open new ones, resolve vanished ones."""
    for key, val in current.items():
        title, body = val[0], val[1]
        extra = val[2] if len(val) > 2 else {}
        en = extra.get("en") or (None, None)
        alert(key, title, body, db_path=db_path, config=config, send=send,
              workspace_id=extra.get("ws"), title_en=en[0], body_en=en[1])
    for key in open_alert_keys(prefix, db_path) - set(current):
        resolve(key, resolved_note, db_path=db_path, config=config, send=send)


def _ws_extra(channel_id: Optional[str], db_path, en: tuple) -> dict:
    """Route a channel's alert to its workspace (English text for clients)."""
    try:
        from hypogeum.workspaces import workspace_of
        ws = workspace_of(channel_id, db_path=Path(db_path) if db_path else None) if channel_id else None
    except Exception:  # noqa: BLE001
        ws = None
    return {"ws": ws, "en": en}


# ---------------------------------------------------------------------------
# checks — each returns {key: (title, body)} for what needs the operator now
# ---------------------------------------------------------------------------

def _drain_balance_check(config, http_get) -> None:
    """While the old drain key is in use, switch to the main key before its
    leftover balance runs out (hypogeum/openrouter_drain.py)."""
    if not (config is not None and getattr(config, "OPENROUTER_DRAIN_ACTIVE", False) is True):
        return
    try:
        if http_get is None:
            import urllib.request
            req = urllib.request.Request("https://openrouter.ai/api/v1/credits",
                                         headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"})
            data = json.load(urllib.request.urlopen(req, timeout=15)).get("data") or {}
        else:
            data = http_get()
        left = float(data.get("total_credits") or 0) - float(data.get("total_usage") or 0)
    except Exception:  # noqa: BLE001
        return
    from hypogeum.openrouter_drain import DRAIN_SWITCH_BELOW_USD, mark_exhausted, purchase_detected
    if purchase_detected(float(data.get("total_credits") or 0)):
        # The old account bought credits on its own (auto top-up): stop using
        # it right away -- the operator never wants that card charged.
        mark_exhausted("auto_topup_detected")
        config.OPENROUTER_API_KEY = config.OPENROUTER_MAIN_KEY
        config.OPENROUTER_DRAIN_ACTIVE = False
        try:
            alert("openrouter:drain_charged", "حساب OpenRouter القديم اتشحن من البطاقة",
                  "رصيد الحساب القديم زاد لوحده، يعني الـ Auto Top-Up شغال عليه وخصم من بطاقتك.\n"
                  "وقفت استخدامه فوراً والمصنع نقل على المفتاح الأساسي. اقفل Auto Top-Up أو امسح البطاقة "
                  "من openrouter.ai ← Credits في الحساب القديم.")
        except Exception:  # noqa: BLE001
            pass
        return
    if left < DRAIN_SWITCH_BELOW_USD:
        mark_exhausted(f"balance_${left:.2f}")
        config.OPENROUTER_API_KEY = config.OPENROUTER_MAIN_KEY
        config.OPENROUTER_DRAIN_ACTIVE = False


def check_openrouter_credits(config, *, http_get=None) -> Optional[dict]:
    """None = couldn't tell (network) -> leave state unchanged."""
    _drain_balance_check(config, http_get)
    # The low-balance alert is about the MAIN key: the drain key running low
    # is expected and just switches over.
    key = (getattr(config, "OPENROUTER_MAIN_KEY", None) or getattr(config, "OPENROUTER_API_KEY", None)) \
        if config else None
    if not key:
        return None
    try:
        if http_get is None:
            import urllib.request
            req = urllib.request.Request("https://openrouter.ai/api/v1/credits",
                                         headers={"Authorization": f"Bearer {key}"})
            data = json.load(urllib.request.urlopen(req, timeout=15)).get("data") or {}
        else:
            data = http_get()
        left = float(data.get("total_credits") or 0) - float(data.get("total_usage") or 0)
    except Exception:  # noqa: BLE001
        return None
    if left < _CREDIT_LOW_USD:
        return {"openrouter:credits": (
            "رصيد OpenRouter قرّب يخلص" if left > 0 else "رصيد OpenRouter خلص",
            f"الباقي: ${left:.2f}. المصنع بيستخدمه في كتابة الحلقات، ولو خلص الإنتاج بيقف.\n"
            "اشحن من openrouter.ai ← Credits (الحساب اللي فيه مفتاح المصنع).")}
    return {}


def check_workspace_credits(db_path, *, http_get=None) -> Optional[dict]:
    """Each CLIENT workspace's own OpenRouter balance (its key from the vault;
    the platform's is check_openrouter_credits). None = nothing to check."""
    try:
        from hypogeum.workspace_keys import workspace_key
        from hypogeum.workspaces import list_workspaces
        workspaces = [w for w in list_workspaces(Path(db_path) if db_path else None) if w["kind"] == "client"]
    except Exception:  # noqa: BLE001
        return None
    if not workspaces:
        return None
    out = {}
    for w in workspaces:
        key = workspace_key("openrouter", w["workspace_id"], db_path)
        if not key:
            out[f"openrouter_ws:missing:{w['workspace_id']}"] = (
                f"{w['workspace_id']}: مفيش مفتاح OpenRouter",
                "الـ Workspace دي مالهاش مفتاح OpenRouter، فإنتاجها مش هيشتغل.",
                {"ws": w["workspace_id"], "en": ("No OpenRouter key", "This workspace has no OpenRouter key, so "
                                                 "its episodes can't be written. Add one in Credentials.")})
            continue
        res = check_openrouter_credits(SimpleNamespace(OPENROUTER_API_KEY=key), http_get=http_get)
        for _k, (t, b) in (res or {}).items():
            out[f"openrouter_ws:credits:{w['workspace_id']}"] = (
                f"{w['workspace_id']}: {t}", b,
                {"ws": w["workspace_id"], "en": ("OpenRouter balance is low", "Your OpenRouter balance is almost "
                                                 "used up; production stops when it runs out. Top up at openrouter.ai → Credits.")})
    return out


def _recent_failures(db_path, since_iso: str, limit: int = 40) -> list:
    conn = _conn(db_path, ddl=False)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT run_id, channel_id, episode_id, status, started_at, error_message FROM runs "
            "WHERE started_at >= ? ORDER BY started_at DESC LIMIT ?", (since_iso, limit))]
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()


def check_run_failures(db_path, now: datetime) -> dict:
    """One alert per episode whose latest run failed in the last 6h — with the
    cause in plain words (2026-10-05: "لازم تجيلي رسالة اية سبب المشكلة").
    Retries of the same episode don't add messages; it closes silently once
    that episode runs again successfully or the failure ages out."""
    from hypogeum.db import get_worker_control
    from hypogeum.operator_actions import _plain_error
    since = (now - timedelta(hours=6)).isoformat()
    # failures from before the operator's last resume were already dealt with
    cleared = get_worker_control("breaker_cleared_at", "", db_path=Path(db_path) if db_path else None) or ""
    since = max(since, cleared)
    latest: dict = {}
    for r in _recent_failures(db_path, since):
        latest.setdefault(r["episode_id"], r)      # newest first
    out = {}
    for ep, r in latest.items():
        if r["status"] not in ("failed", "error"):
            continue
        cause = _plain_error(r.get("error_message") or "")
        raw = (r.get("error_message") or "").strip().splitlines()[0][:220] if r.get("error_message") else ""
        out[f"run_failed:{ep}"] = (
            f"حلقة {r['channel_id']} فشلت",
            f"السبب: {cause}\nالحلقة: {ep}\nالتفاصيل: {raw}",
            _ws_extra(r["channel_id"], db_path, (f"{r['channel_id']}: an episode failed", f"Cause: {raw[:160]}")))
    return out


def check_production_paused(db_path) -> dict:
    from hypogeum.db import get_worker_control
    dbp = Path(db_path) if db_path else None
    out = {}
    if get_worker_control("queue_paused", "0", db_path=dbp) == "1":
        reason = get_worker_control("queue_paused_reason", "", db_path=dbp) or "سبب غير مكتوب"
        causes = ""
        try:
            from hypogeum.operator_actions import _plain_error
            fails = [r for r in _recent_failures(dbp, (_now() - timedelta(hours=12)).isoformat(), 10)
                     if r["status"] in ("failed", "error")][:3]
            if fails:
                causes = "\nآخر الفشلات:\n" + "\n".join(
                    f"• {r['channel_id']}: {_plain_error(r.get('error_message') or '')}" for r in fails)
        except Exception:  # noqa: BLE001
            pass
        short_reason = reason.split(" — ")[0].replace("muapi_spend_breaker: ", "")
        out["production:paused"] = ("الإنتاج واقف",
                                    f"المصنع وقّف إنتاج الحلقات الجديدة (النشر شغال عادي).\n"
                                    f"ليه وقف: {short_reason}{causes}\n"
                                    "صلّح السبب، وبعدين اضغط «رجّع الإنتاج».")
    # multi-tenant: a workspace's own spend breaker pauses only that workspace
    try:
        from hypogeum.workspace_controls import paused_workspaces
        for wid, reason in paused_workspaces(dbp).items():
            out[f"production:paused:{wid}"] = (
                f"الإنتاج واقف لـ {wid}",
                f"إنتاج الحلقات الجديدة لقنوات {wid} واقف (باقي المساحات شغالة). النشر شغال عادي.\n"
                f"السبب: {reason or 'سبب غير مكتوب'}",
                {"ws": wid, "en": ("New episode production is paused",
                                   "Publishing of finished episodes continues. Production resumes once the "
                                   f"cause is fixed.\nReason: {reason or 'not recorded'}")})
    except Exception:  # noqa: BLE001
        pass
    return out


def check_adapters(db_path) -> dict:
    """Gate adapters whose code changed without re-registration: every
    gated call (writing, research, voice, video) is refused until they're
    superseded — production can't run at all."""
    from hypogeum.adapter_registry import stale_registrations
    stale = stale_registrations(Path(db_path) if db_path else None)
    if not stale:
        return {}
    names = ", ".join(sorted({f"{s['provider']}/{s['provider_operation']}" for s in stale}))
    return {"system:adapters_stale": (
        "كود الـ adapters اتغير ومتسجلش",
        f"{len(stale)} تسجيل قديم ({names}). أي حلقة هتبدأ هتفشل فوراً لحد ما يتسجلوا من جديد.\n"
        "اضغط «سجّل الـ adapters» (لازم الكود يكون متعمله commit).")}


def check_worker(db_path, now: datetime) -> dict:
    """The worker writes worker_heartbeat_at once per loop iteration — which
    can't happen while it's inside a pipeline run, so allow a long gap then."""
    from hypogeum.db import get_worker_control
    raw = get_worker_control("worker_heartbeat_at", "", db_path=Path(db_path) if db_path else None)
    status = get_worker_control("worker_status", "", db_path=Path(db_path) if db_path else None)
    try:
        hb = datetime.fromisoformat(raw)
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return {}
    limit_min = 240 if status == "running" else 20
    gap_min = (now - hb).total_seconds() / 60
    if gap_min <= limit_min:
        return {}
    return {"worker:down": ("الـ worker مش شغال",
                            f"آخر نبضة منه من {gap_min:.0f} دقيقة (حالته: {status or '?'}).\n"
                            "من غيره مفيش حلقات جديدة بتتعمل ولا بتتجدول. اضغط «شغّل الـ worker».")}


def check_blocking_signals(db_path) -> dict:
    conn = _conn(db_path, ddl=False)
    try:
        rows = conn.execute("SELECT signal_type, channel_id, full_record FROM signal_records "
                            "WHERE blocking=1 AND lifecycle_status='open' ORDER BY ingested_at DESC").fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        key = f"signal:{r['signal_type']}:{r['channel_id'] or 'factory'}"
        if key in out:
            continue
        try:
            msg = (json.loads(r["full_record"] or "{}") or {}).get("message") or ""
        except Exception:  # noqa: BLE001
            msg = ""
        out[key] = (f"تنبيه موقّف: {r['signal_type']}" + (f" ({r['channel_id']})" if r["channel_id"] else ""),
                    (msg[:400] + "\n" if msg else "") + "طول ما التنبيه ده مفتوح، الإنتاج واقف.")
    return out


def check_youtube_covers(db_path, now: datetime) -> dict:
    """Held Shorts already uploaded (private) on YouTube and still waiting for
    the operator's cover — the release slot can't go public without it."""
    conn = _conn(db_path, ddl=False)
    try:
        rows = conn.execute(
            "SELECT h.post_id, h.channel_id, h.release_at, h.status FROM youtube_cover_holds h "
            "WHERE h.status IN ('held','late','release_failed') AND h.upload_at <= ? "
            "AND h.post_id NOT IN (SELECT post_id FROM youtube_cover_confirmations)",
            (now.strftime("%Y-%m-%dT%H:%M:%S"),)).fetchall()
    except Exception:  # noqa: BLE001 — table absent on a fresh DB
        return {}
    finally:
        conn.close()
    out = {}
    for r in rows:
        try:
            rel = datetime.fromisoformat(r["release_at"].replace("Z", "+00:00"))
            local = (rel + timedelta(hours=4)).strftime("%a %d %b %H:%M")
        except Exception:  # noqa: BLE001
            local = r["release_at"]
        late = r["status"] != "held"
        out[f"cover:{r['post_id']}"] = (
            f"شورت {r['channel_id']} " + ("فات ميعاده ولسه private" if late else "مستني غلاف يوتيوب"),
            ("ميعاد النشر عدّى والشورت لسه مش للعامة. " if late else "")
            + f"اترفع private، ومش هيتنشر للعامة غير بعد الغلاف.\nميعاد النشر: {local} (دبي)\n"
            f"الغلاف والرابط في الـ Content Grid: {_DASHBOARD}/channel/{r['channel_id']}/grid",
            _ws_extra(r["channel_id"], db_path, (
                f"{r['channel_id']}: Short " + ("past its slot, still private" if late else "waiting for its YouTube cover"),
                f"It was uploaded as private and goes public only once its cover is uploaded in YouTube Studio.\n"
                f"Scheduled release: {r['release_at']} (UTC)")))
    return out


def check_posts(db_path, now: datetime) -> dict:
    """Zernio posts the Content Grid flags: a platform failed / stuck
    (needs_attention), or a live Short whose Studio cover isn't confirmed.
    Only posts slotted in the last 3 days — older failures are history, not
    something the operator can still act on in time."""
    from hypogeum.dashboard_content_grid import get_content_grid
    conn = _conn(db_path, ddl=False)
    try:
        channels = [r[0] for r in conn.execute(
            "SELECT DISTINCT channel_id FROM channel_provider_bindings WHERE channel_id IS NOT NULL")]
        try:   # a held Short is check_youtube_covers' job — don't alert it twice
            held = {r[0] for r in conn.execute(
                "SELECT post_id FROM youtube_cover_holds WHERE status IN ('held','late','release_failed')")}
        except Exception:  # noqa: BLE001
            held = set()
    finally:
        conn.close()
    since = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
    out = {}
    for ch in channels:
        for it in get_content_grid(ch, db_path=Path(db_path) if db_path else None):
            if (it.get("scheduled_for") or it.get("published_at") or "") < since:
                continue
            title = (it.get("display_title") or it["post_id"])[:80]
            if it.get("needs_attention"):
                stuck = ", ".join(x for x in (it.get("not_yet_live_platforms") or []) if x) or "?"
                out[f"post:{it['post_id']}"] = (
                    f"بوست على {ch} مانزلش صح",
                    f"«{title}»\nحالته في Zernio: {it.get('status')} — المنصات اللي لسه: {stuck}\n"
                    f"{_DASHBOARD}/channel/{ch}/grid",
                    _ws_extra(ch, db_path, (f"{ch}: a post didn't go out correctly",
                                            f"«{title}»\nStill missing on: {stuck}")))
            if it.get("youtube_cover_pending") and it["post_id"] not in held:
                out[f"postcover:{it['post_id']}"] = (
                    f"شورت {ch} نزل على يوتيوب من غير تأكيد الغلاف",
                    f"«{title}»\nارفع الغلاف من YouTube Studio واضغط Done في الـ Content Grid:\n"
                    f"{_DASHBOARD}/channel/{ch}/grid",
                    _ws_extra(ch, db_path, (f"{ch}: Short is live without its confirmed cover",
                                            f"«{title}»\nUpload the cover in YouTube Studio.")))
    return out


def check_accounts(db_path) -> dict:
    conn = _conn(db_path, ddl=False)
    try:
        rows = conn.execute("SELECT provider_id, platform, display_label FROM provider_accounts "
                            "WHERE needs_reconnection=1 OR status NOT IN ('active')").fetchall()
    except Exception:  # noqa: BLE001
        return {}
    finally:
        conn.close()
    return {f"account:{r['provider_id']}:{r['platform']}:{r['display_label']}": (
        f"حساب محتاج يتربط تاني: {r['platform']} ({r['provider_id']})",
        f"{r['display_label'] or ''}\nالنشر على الحساب ده هيفشل لحد ما يتربط من جديد.") for r in rows}


_ARCHIVE_GRACE_MIN = 30


def check_archive(db_path, now: datetime, *, mounted=None) -> Optional[dict]:
    """The episode archive is an SMB share on the NAS (scripts/
    mount_archive_share.sh retries every 5 min). Laptop sleep drops it for a
    few minutes routinely; down for 30+ min means the NAS is off or off the
    network and finished episodes can't be archived (2026-10-04: down for
    hours with nobody told). None = not a NAS-backed archive / unknown."""
    try:
        from hypogeum.archive_storage import _raw_configured_root
        raw, source = _raw_configured_root(Path(db_path) if db_path else None)
        parts = Path(raw).expanduser().parts
    except Exception:  # noqa: BLE001
        return None
    if source == "default" or len(parts) < 3 or parts[1] != "Volumes":
        return None
    mount_point = Path(parts[0], parts[1], parts[2])
    is_up = os.path.ismount(mount_point) if mounted is None else mounted
    conn = _conn(db_path)
    try:
        if is_up:
            conn.execute("DELETE FROM operator_alert_checks WHERE check_name='archive_down_since'")
            conn.commit()
            return {}
        row = conn.execute("SELECT last_run_at FROM operator_alert_checks WHERE check_name='archive_down_since'").fetchone()
        if not row:
            conn.execute("INSERT INTO operator_alert_checks (check_name, last_run_at) VALUES ('archive_down_since', ?)",
                         (now.isoformat(),))
            conn.commit()
            return None
        since = datetime.fromisoformat(row["last_run_at"])
    finally:
        conn.close()
    mins = (now - since).total_seconds() / 60
    if mins < _ARCHIVE_GRACE_MIN:
        return None
    return {"archive:down": ("أرشيف الـ NAS مش متوصّل",
                             f"الماك مش شايف الـ NAS ({mount_point}) من حوالي {mins:.0f} دقيقة.\n"
                             "الحلقات اللي بتخلص مش هتتنقل للأرشيف لحد ما يرجع. "
                             "اتأكد إن الـ NAS شغال وعلى نفس الشبكة.")}


def check_disk(path: str = "/System/Volumes/Data", *, usage=None) -> dict:
    try:
        u = usage or shutil.disk_usage(path if Path(path).exists() else "/")
    except Exception:  # noqa: BLE001
        return {}
    free_gb = u.free / 1e9
    if free_gb < _DISK_LOW_GB:
        return {"disk:low": ("مساحة الهارد قرّبت تخلص",
                             f"الفاضي {free_gb:.0f} جيجا بس. لو الهارد اتملى المصنع هيقف.\n"
                             "فضّي مساحة (ملفات كبيرة، تنزيلات قديمة، أو انقل حاجات للأرشيف).")}
    return {} if free_gb >= _DISK_OK_GB else None   # between thresholds: keep current state


def run_operator_checks(*, db_path=None, config=None, now: Optional[datetime] = None,
                        send: Optional[Callable[[str], bool]] = None, force: bool = False) -> dict:
    """Run every due check; open/resolve alerts. Returns {check: n_open}."""
    now = now or _now()
    config = _config(config)
    out: dict = {}
    just_woke = _note_tick_and_wake(db_path, now)
    checks = [
        ("openrouter_credits", "openrouter:credits", lambda: check_openrouter_credits(config)),
        ("workspace_credits", "openrouter_ws:", lambda: check_workspace_credits(db_path)),
        # right after the machine wakes, the worker hasn't beaten yet either —
        # don't call it "down" until it's had time to (2026-10-04 false alarm)
        ("worker", "worker:", lambda: None if just_woke else check_worker(db_path, now)),
        ("adapters", "system:adapters", lambda: check_adapters(db_path)),
        ("production_paused", "production:", lambda: check_production_paused(db_path)),
        ("run_failures", "run_failed:", lambda: check_run_failures(db_path, now)),
        ("blocking_signals", "signal:", lambda: check_blocking_signals(db_path)),
        ("youtube_covers", "cover:", lambda: check_youtube_covers(db_path, now)),
        ("posts", "post", lambda: check_posts(db_path, now)),
        ("accounts", "account:", lambda: check_accounts(db_path)),
        ("disk", "disk:", lambda: check_disk()),
        ("archive", "archive:", lambda: check_archive(db_path, now)),
    ]
    for name, prefix, fn in checks:
        try:
            if not force and not _due(name, now, db_path):
                continue
            current = fn()
            if current is None:
                continue
            _sync(prefix, current, db_path, config, send)
            out[name] = len(current)
        except Exception as exc:  # noqa: BLE001
            log.warn("operator_check_failed", check=name, error=str(exc)[:200])
    try:
        _retry_unsent(db_path, config, send, now)
    except Exception as exc:  # noqa: BLE001
        log.warn("operator_alert_retry_failed", error=str(exc)[:200])
    return out


_WAKE_GAP_S = 600      # loop silent this long = the machine was asleep
_WAKE_GRACE_S = 900    # give the worker this long to beat again after wake
_RETRY_AFTER_S = 120


def _note_tick_and_wake(db_path, now: datetime) -> bool:
    """Record this loop tick; return True while inside the post-wake grace."""
    conn = _conn(db_path)
    try:
        vals = {r["check_name"]: r["last_run_at"] for r in conn.execute(
            "SELECT * FROM operator_alert_checks WHERE check_name IN ('loop_tick','awake_since')")}
        last = vals.get("loop_tick")
        if last and (now - datetime.fromisoformat(last)).total_seconds() > _WAKE_GAP_S:
            vals["awake_since"] = now.isoformat()
            conn.execute("INSERT INTO operator_alert_checks (check_name, last_run_at) VALUES ('awake_since', ?) "
                         "ON CONFLICT(check_name) DO UPDATE SET last_run_at=excluded.last_run_at", (now.isoformat(),))
        conn.execute("INSERT INTO operator_alert_checks (check_name, last_run_at) VALUES ('loop_tick', ?) "
                     "ON CONFLICT(check_name) DO UPDATE SET last_run_at=excluded.last_run_at", (now.isoformat(),))
        conn.commit()
    finally:
        conn.close()
    aw = vals.get("awake_since")
    return bool(aw) and (now - datetime.fromisoformat(aw)).total_seconds() < _WAKE_GRACE_S


def _retry_unsent(db_path, config, send, now: datetime) -> None:
    """An open alert whose message never got out (network down right after a
    wake, Telegram hiccup) is re-sent — otherwise the operator would only
    ever see its 'resolved' message, or nothing."""
    conn = _conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM operator_alerts WHERE status='open' AND send_ok=0")]
    finally:
        conn.close()
    for r in rows:
        last = r.get("last_sent_at")
        if last and (now - datetime.fromisoformat(last)).total_seconds() < _RETRY_AFTER_S:
            continue
        ar_text = f"🔴 {r['title']}\n\n{r['body'] or ''}".strip()
        en_text = f"🔴 {r['title_en']}\n\n{r['body_en'] or ''}".strip() if r.get("title_en") else None
        # claim the attempt BEFORE sending: if this write fails nothing is
        # sent, so a busy DB can't turn into the same message every 2 minutes
        conn = _conn(db_path)
        try:
            conn.execute("UPDATE operator_alerts SET last_sent_at=? WHERE alert_key=? AND status='open'",
                         (now.isoformat(), r["alert_key"]))
            conn.commit()
        finally:
            conn.close()
        ok = bool(send(ar_text)) if send is not None else _deliver(
            r["alert_key"], r.get("workspace_id"), ar_text, en_text, config, db_path)
        if ok:
            conn = _conn(db_path)
            try:
                conn.execute("UPDATE operator_alerts SET send_ok=1 WHERE alert_key=?", (r["alert_key"],))
                conn.commit()
            finally:
                conn.close()


def start_operator_alerts_loop(db_path=None, *, check_every_s: int = 60):
    """Run the checks from the dashboard process (always up via its
    LaunchAgent) rather than the worker, which blocks for the length of a
    pipeline run and is itself one of the things being watched."""
    import threading
    stop = threading.Event()

    def _loop():
        stop.wait(20)   # let startup settle (sleep-wake DB contention)
        while not stop.is_set():
            run_operator_checks(db_path=db_path)
            stop.wait(check_every_s)

    t = threading.Thread(target=_loop, name="operator-alerts", daemon=True)
    t.stop_event = stop  # type: ignore[attr-defined]
    t.start()
    log.info("operator_alerts_loop_started", check_every_s=check_every_s)
    return t
