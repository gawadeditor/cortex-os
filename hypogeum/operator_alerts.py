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
_INTERVALS = {"adapters": 300, "run_failures": 60, "queue_failures": 120, "buffer_gap": 1800, "production_stalled": 1800, "partial_releases": 300, "public_youtube_shorts": 600, "postiz_post_errors": 600, "client_grants": 3600, "client_drafts": 600, "client_reviews": 900, "client_publishing_media": 600, "client_publishing_posts": 600, "openrouter_credits": 1800, "workspace_credits": 1800, "worker": 300, "production_paused": 60, "blocking_signals": 120,
              "youtube_covers": 300, "stuck_releases": 300, "youtube_flip_auth": 3600, "posts": 1800, "unpublished": 300, "accounts": 600, "archive": 300, "archive_backlog": 600, "disk": 1800}
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
    if workspace_id:                      # a client workspace's portal users also get it by email (unless they opted out)
        try:
            from hypogeum import portal_auth as pa
            body = en_text if (lang == "en" and en_text) else ar_text
            ok_any = pa.send_workspace_alert_email(workspace_id, body, db_path=db_path, config=cfg) or ok_any
        except Exception:  # noqa: BLE001
            pass
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
    a workspace's own — open new ones, resolve vanished ones.

    Between detection and notification sits the self-healing engine (hypogeum.self_heal): a problem that has a
    remediation is ATTEMPTED first and its alert deferred while attempts are left; if it clears afterwards the
    operator gets one "the factory fixed it" message; if not, the normal alert (with what was tried)."""
    from hypogeum import self_heal
    for key, val in current.items():
        title, body = val[0], val[1]
        extra = val[2] if len(val) > 2 else {}
        en = extra.get("en") or (None, None)
        verdict, note = self_heal.handle_detected(key, db_path=db_path, config=config)
        if verdict == "defer":
            continue
        alert(key, title, body + note, db_path=db_path, config=config, send=send,
              workspace_id=extra.get("ws"), title_en=en[0], body_en=en[1])
    vanished = (open_alert_keys(prefix, db_path) | self_heal.healing_keys(prefix, db_path)) - set(current)
    open_now = open_alert_keys(prefix, db_path)
    for key in vanished:
        if key in open_now:
            resolve(key, resolved_note, db_path=db_path, config=config, send=send)
        fixed = self_heal.handle_cleared(key, db_path=db_path)
        if fixed:
            if send is not None:
                send(fixed)
            else:
                _deliver("selfheal:" + key, None, fixed, None, config, db_path, with_buttons=False)


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
                                                 "its episodes can't be written. Add your key under Setup in your portal.")})
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


def check_queue_failures(db_path, now: datetime) -> dict:
    """Production attempts that died BEFORE a run existed (no `runs` row), so
    check_run_failures can't see them: the backward plan was infeasible, no
    topic, a gate refused it at admission... 2026-10-09: twinkle failed 7
    times in one day this way and nobody was told. One alert per channel, with
    the cause; it closes once a later attempt completes or the failures age
    out (6h), exactly like check_run_failures."""
    from hypogeum.db import get_worker_control
    from hypogeum.operator_actions import _plain_error
    since = (now - timedelta(hours=6)).isoformat()
    cleared = get_worker_control("breaker_cleared_at", "", db_path=Path(db_path) if db_path else None) or ""
    since = max(since, cleared)
    conn = _conn(db_path, ddl=False)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT q.channel_id, q.episode_id, q.error_message, "
            "COALESCE(q.failed_at, q.created_at) AS t FROM run_queue q "
            "WHERE q.status = 'failed' AND COALESCE(q.failed_at, q.created_at) >= ? "
            "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.episode_id = q.episode_id) "
            "ORDER BY t DESC", (since,))]
        done = {r[0]: r[1] for r in conn.execute(
            "SELECT channel_id, MAX(completed_at) FROM run_queue WHERE status = 'completed' GROUP BY channel_id")}
    except Exception:  # noqa: BLE001
        return {}
    finally:
        conn.close()
    by_channel: dict = {}
    for r in rows:
        by_channel.setdefault(r["channel_id"], []).append(r)
    out = {}
    for ch, fails in by_channel.items():
        latest = fails[0]
        if (done.get(ch) or "") > (latest["t"] or ""):
            continue                                    # it produced again since
        raw = (latest.get("error_message") or "").strip().splitlines()[0][:220] if latest.get("error_message") else ""
        out[f"queue_failed:{ch}"] = (
            f"إنتاج {ch} بيفشل قبل ما يبدأ",
            f"السبب: {_plain_error(latest.get('error_message') or '')}\n"
            f"المحاولات الفاشلة في آخر 6 ساعات: {len(fails)}\n"
            f"آخر حلقة: {latest['episode_id']}\nالتفاصيل: {raw}",
            _ws_extra(ch, db_path, (f"{ch}: production attempts are failing before they start",
                                    f"{len(fails)} attempts in the last 6h. Cause: {raw[:160]}")))
    return out


_BUFFER_ALERT_HOURS = 72   # the next EMPTY publish slot is this close and nothing is being produced


def check_buffer_gap(db_path, now: datetime) -> dict:
    """A channel with more upcoming publish slots than episodes to fill them,
    where the first uncovered slot is close and nothing is queued or in
    production. 72h leaves time for the ~36h production lead the plan needs.

    Counts EPISODES against SLOTS rather than matching episode->slot dates:
    channel_publish_slots mirrors only an episode's LATEST per-platform release
    (zernio_schedule_sync), so a channel with a YouTube day override shows an
    episode on its YouTube date while its other platforms use an earlier slot
    (2026-10-09: a false "Mon 12 Oct is empty" while that episode held it)."""
    from hypogeum.db import get_worker_control
    dbp = Path(db_path) if db_path else None
    if get_worker_control("queue_paused", "0", db_path=dbp) == "1":
        return {}                                       # check_production_paused already says so
    now_iso = now.isoformat()
    conn = _conn(db_path, ddl=False)
    try:
        channels = [r[0] for r in conn.execute(
            "SELECT DISTINCT channel_id FROM schedules WHERE status = 'active' AND channel_id IS NOT NULL")]
        busy = {r[0] for r in conn.execute(
            "SELECT DISTINCT channel_id FROM run_queue WHERE status IN ('queued','claimed')")}
        try:
            busy |= {r[0] for r in conn.execute(
                "SELECT DISTINCT channel_id FROM scheduled_runs WHERE status = 'pending'")}
        except Exception:  # noqa: BLE001
            pass
        covering: dict = {}
        for ch, ep in conn.execute(
                "SELECT channel_id, episode_id FROM channel_publish_slots "
                "WHERE episode_id IS NOT NULL AND slot_utc > ?", (now_iso,)):
            covering.setdefault(ch, set()).add(ep)
        try:   # a held YouTube Short's episode that is not in the mirror yet
            for ch, ep in conn.execute(
                    "SELECT channel_id, episode_id FROM youtube_cover_holds "
                    "WHERE status IN ('held','late') AND episode_id IS NOT NULL AND release_at > ?", (now_iso,)):
                covering.setdefault(ch, set()).add(ep)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        return {}
    finally:
        conn.close()
    from hypogeum.channel_config import get_channel_operations_profile
    from hypogeum.scheduler import compute_next_publish_slot
    out = {}
    for ch in channels:
        if ch in busy:
            continue
        try:
            profile = get_channel_operations_profile(ch, db_path=dbp)
            if profile is None or not getattr(profile, "production_enabled", True):
                continue
            # same policy the claim-time target derivation and the release-time
            # schedule compiler use (see queue.process_next)
            dsp = getattr(profile, "deliverable_schedule_policy", None) or {}
            policy = (dsp.get("main_episode") or next(iter(dsp.values()), None)
                      if isinstance(dsp, dict) and dsp else None) or profile.publishing_schedule
            slot, from_dt = None, now
            for _ in range(len(covering.get(ch, ())) + 1):     # the (n+1)th upcoming slot is the first uncovered
                slot = compute_next_publish_slot(policy, profile.timezone, from_dt)
                if slot is None:
                    break
                from_dt = slot + timedelta(minutes=1)
        except Exception:  # noqa: BLE001
            continue
        if slot is None:
            continue
        hours = (slot - now).total_seconds() / 3600
        if hours > _BUFFER_ALERT_HOURS:
            continue
        local = (slot + timedelta(hours=4)).strftime("%a %d %b %H:%M")
        out[f"buffer:{ch}"] = (
            f"{ch}: مفيش حلقة جاهزة للميعاد الجاي",
            f"أقرب ميعاد نشر مالوش حلقة: {local} (دبي)، بعد حوالي {int(hours)} ساعة.\n"
            "مفيش حلقة جاهزة ولا شغالة عليه. الإنتاج محتاج حوالي 36 ساعة، فلو ما بدأش دلوقتي الميعاد هيروح فاضي.",
            _ws_extra(ch, db_path, (f"{ch}: no episode is ready for the next publish slot",
                                    f"First slot without an episode: {slot.isoformat()} (UTC), about {int(hours)}h away, "
                                    "and nothing is queued or in production for it.")))
    return out


def _last_finished_by_channel(conn) -> dict:
    """{channel_id: ISO time of the newest finished episode}: a queue item that completed OR a run that
    completed (an automatic/manual salvage finishes an episode without a queue item)."""
    out: dict = {}
    for sql in (
        "SELECT channel_id, MAX(COALESCE(completed_at, queued_at)) FROM run_queue WHERE status = 'completed' GROUP BY channel_id",
        "SELECT channel_id, MAX(completed_at) FROM runs WHERE status IN ('completed','complete') AND channel_id IS NOT NULL GROUP BY channel_id",
    ):
        try:
            for ch, ts in conn.execute(sql):
                if ts and (ch not in out or str(ts) > str(out[ch])):
                    out[ch] = ts
        except Exception:  # noqa: BLE001
            continue
    return out


_STALL_ALERT_DAYS = 3   # no episode finished for this long while the buffer is below target


def check_production_stalled(db_path, now: datetime) -> dict:
    """A channel whose buffer is below target and that has not FINISHED an
    episode for 3+ days, whatever the reason (worker stopped on purpose, paused,
    failing, stuck). 2026-09-07..10-03: production sat stopped for 26 days and
    nothing said so. Failures and pauses have their own alerts; this one is the
    long-silence backstop, and says how long it has been. One alert per channel
    (not repeated while open). Channels that never finished an episode, or whose
    buffer is full, are skipped."""
    from hypogeum.db import get_worker_control
    dbp = Path(db_path) if db_path else None
    conn = _conn(db_path, ddl=False)
    try:
        channels = [r[0] for r in conn.execute(
            "SELECT DISTINCT channel_id FROM schedules WHERE status = 'active' AND channel_id IS NOT NULL")]
        last_done = _last_finished_by_channel(conn)
        from hypogeum.scheduler import _PRODUCTION_BUFFER_TARGET, _env_int, channel_buffer_depth
        target = _env_int("PRODUCTION_BUFFER_TARGET", _PRODUCTION_BUFFER_TARGET)
        depths = {ch: channel_buffer_depth(ch, conn, now=now) for ch in channels} if target else {}
    except Exception:  # noqa: BLE001
        return {}
    finally:
        conn.close()
    if not target:
        return {}
    from hypogeum.channel_config import get_channel_operations_profile
    stopped = get_worker_control("stop_requested", "0", db_path=dbp) == "1"
    paused = get_worker_control("queue_paused", "0", db_path=dbp) == "1"
    out = {}
    for ch in channels:
        done = last_done.get(ch)
        if not done or depths.get(ch, target) >= target:
            continue
        try:
            profile = get_channel_operations_profile(ch, db_path=dbp)
            if profile is None or not getattr(profile, "production_enabled", True):
                continue
            idle = now - datetime.fromisoformat(str(done).replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            continue
        if idle < timedelta(days=_STALL_ALERT_DAYS):
            continue
        days = idle.days
        if stopped:
            why = "الـ worker متوقف بأمر (stop)."
        elif paused:
            why = "الإنتاج موقوف (paused): " + (get_worker_control("queue_paused_reason", "", db_path=dbp) or "سبب غير مكتوب")
        else:
            why = "مفيش إيقاف مسجّل. شوف تنبيهات فشل الإنتاج أو حالة الـ worker."
        last_local = (datetime.fromisoformat(str(done).replace("Z", "+00:00")) + timedelta(hours=4)).strftime("%d %b")
        out[f"stalled:{ch}"] = (
            f"مفيش حلقات جديدة بتتنتج على {ch} من {days} يوم",
            f"ده عن إنتاج حلقات جديدة لقناة {ch} بس. النشر شغال من المخزون المتبقي.\n"
            f"آخر حلقة خلّص إنتاجها: {last_local} (دبي).\n"
            f"الحلقات الجاهزة أو الشغالة دلوقتي: {depths[ch]} من {target}.\n{why}",
            _ws_extra(ch, db_path, (f"{ch}: production has been stalled for {days} days",
                                    f"No episode finished since {done}. Buffer {depths[ch]} of {target}.")))
    return out


_PARTIAL_RELEASE_ALERT_SECONDS = 30 * 60


def check_partial_releases(db_path, now: datetime) -> dict:
    """An episode booked on some platforms but not all, for 30+ minutes (2026-10-10: Denisovans went
    out on TikTok only and nothing said so). One alert per episode, naming the platforms still missing
    and the last provider error for each; it closes once every target platform is booked. Only
    episodes released in the last 7 days."""
    from hypogeum.release_completeness import incomplete_release
    since = (now - timedelta(days=7)).isoformat()
    conn = _conn(db_path, ddl=False)
    try:
        eps = conn.execute(
            "SELECT DISTINCT episode_id, channel_id FROM publication_intents "
            "WHERE status = 'succeeded' AND updated_at >= ?", (since,)).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    finally:
        conn.close()
    out = {}
    for r in eps:
        inc = incomplete_release(r["episode_id"], r["channel_id"], db_path=db_path, now=now)
        if not inc or inc["age_s"] < _PARTIAL_RELEASE_ALERT_SECONDS:
            continue
        lines = []
        for p in inc["missing"]:
            err = (inc["last_errors"].get(p) or "").strip().splitlines()[0][:140] if inc["last_errors"].get(p) else ""
            lines.append(f"• {p}: " + (err or "ما اتعملش محاولة (غالبًا ملفات الحلقة أو الـ metadata ناقصة)"))
        out[f"partial:{r['episode_id']}"] = (
            f"حلقة {r['channel_id']} نزلت على بعض المنصات بس",
            f"الحلقة: {r['episode_id']}\nاتحجزت على: {', '.join(inc['succeeded'])}\n"
            f"لسه ناقص ({int(inc['age_s'] // 60)} دقيقة):\n" + "\n".join(lines),
            _ws_extra(r["channel_id"], db_path, (f"{r['channel_id']}: an episode is only partly released",
                                                 f"{r['episode_id']} is booked on {', '.join(inc['succeeded'])}; still missing: "
                                                 f"{', '.join(inc['missing'])}")))
    return out


def check_public_youtube_shorts(db_path, now: datetime) -> dict:
    """A YouTube Short of a short-native channel sitting in Postiz's queue as PUBLIC with no cover hold would go
    live with the auto-picked frame instead of its designed cover (2026-10-10: Fern). The remediation
    (hypogeum.remediations.fix_public_youtube_short) turns it private and records the hold; this alert only
    reaches the operator if that fails. A Postiz outage is not a finding: returns {}."""
    try:
        from hypogeum.remediations import find_public_youtube_shorts
        found = find_public_youtube_shorts(db_path, now)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for f in found:
        out[f"ytpublic:{f['post_id']}"] = (
            f"Short على يوتيوب هيتنشر public من غير غلاف ({f['channel_id']})",
            f"الحلقة: {f['episode_id']}\nميعاد النشر: {f['publish_date']}\n"
            "المنشور في طابور Postiz بإعداد public ومن غير hold، فهيظهر بصورة تلقائية بدل الغلاف المصمم.",
            _ws_extra(f["channel_id"], db_path, (f"{f['channel_id']}: a Short will go public on YouTube without its cover",
                                                  f"{f['episode_id']} is queued as public with no cover hold.")))
    return out


def check_postiz_post_errors(db_path, now: datetime) -> dict:
    """A post Postiz itself marked ERROR within the last 24h (rejected by the platform, token expired, media
    refused ...) and not superseded by a newer post for the same episode/platform. The factory's own publish
    check only looks at what it sent; this reads Postiz's verdict, so a failure that happens AFTER the send
    reaches the operator on the next tick. A Postiz outage is not a finding: returns {}."""
    try:
        from hypogeum.db import get_connection
        from hypogeum.publishing.postiz import PostizProvider
        dbp = Path(db_path) if db_path else None
        posts = PostizProvider().list_posts(start=now - timedelta(hours=24), end=now + timedelta(hours=1), db_path=dbp)
        bad = [p for p in posts if str(p.get("state") or "").upper() == "ERROR" and p.get("id")]
        if not bad:
            return {}
        conn = get_connection(dbp)
        try:
            out = {}
            for p in bad:
                pid = str(p["id"])
                m = conn.execute("SELECT channel_id, episode_id, platform, deliverable_id, status FROM provider_content_mappings "
                                 "WHERE provider_content_id = ? LIMIT 1", (pid,)).fetchone()
                if m is None or m["status"] != "confirmed":
                    continue                               # not one of ours / already superseded
                newer = conn.execute(
                    "SELECT 1 FROM provider_content_mappings WHERE episode_id = ? AND platform = ? AND deliverable_id = ? "
                    "AND provider_content_id != ? AND created_at > (SELECT created_at FROM provider_content_mappings "
                    "WHERE provider_content_id = ? LIMIT 1) LIMIT 1",
                    (m["episode_id"], m["platform"], m["deliverable_id"], pid, pid)).fetchone()
                if newer:
                    continue                               # already re-posted
                integ = p.get("integration") if isinstance(p.get("integration"), dict) else {}
                out[f"pzerr:{pid}"] = (
                    f"Postiz رفض بوست {m['platform']} ({m['channel_id']})",
                    f"الحلقة: {m['episode_id']} ({m['deliverable_id']})\nالحساب: {integ.get('name') or '-'}\n"
                    f"ميعاد النشر: {p.get('publishDate')}\nPostiz علّم البوست ERROR، يعني مانزلش على المنصة.",
                    _ws_extra(m["channel_id"], db_path, (f"{m['channel_id']}: a {m['platform']} post failed in Postiz",
                                                         f"{m['episode_id']} was marked ERROR by Postiz.")))
            return out
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return {}


def check_client_grants(db_path, now: datetime) -> dict:
    """A CLIENT workspace's channel cannot produce without an active Operating Grant, and only the platform
    operator can sign it (ADR-016: the key stays on this machine). So a client channel with no grant -- or one
    that expires within two weeks -- is the operator's to-do, and must reach Telegram."""
    try:
        from hypogeum.client_grants import channels_needing_grant
        rows = channels_needing_grant(db_path, now)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for r in rows:
        ch = r["channel_id"]
        out[f"clientgrant:{ch}"] = (
            f"قناة عميل محتاجة توقيع إذن الصرف ({ch})",
            f"السبب: {r['reason']}\nالـ Workspace: {r['workspace_id']}\n"
            f"من الداشبورد: /channel/{ch}/operating-grant (أنت اللي بتوقّع، والعميل ما يقدرش).")
    return out


def check_client_drafts(db_path, now: datetime) -> dict:
    """A client's drafted post is due within a week and nobody has decided: ask the operator, with the draft in
    the message and Approve / Skip buttons (hypogeum.client_post_approvals). Drafts whose date passed are
    marked skipped first, so they stop asking."""
    try:
        from hypogeum import client_post_approvals as cpa
        cpa.expire_past(db_path, now.date())
        items = cpa.due_drafts(db_path, now.date())
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for it in items:
        title, body = cpa.alert_text(it)
        out[f"clientdraft:{it['client_id']}:{it['post']['date']}"] = (title, body)
    return out


def check_client_publishing(db_path, now: datetime) -> dict:
    """An approved Instagram post that cannot go out (no material, a [placeholder] left, account not connected in
    Postiz) or whose send failed / is uncertain (hypogeum.client_publishing). Only clients switched to 'manage'."""
    try:
        from hypogeum.client_publishing import _managed
        from hypogeum.dashboard_clients import calendars_of
        clients = _managed(db_path)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for c in clients:
        for cal in calendars_of(c.get("brief") or {}).values():
            for p in cal.get("posts") or []:
                a = (p.get("auto") or {}).get("instagram") or {}
                if (p.get("status") or "draft") != "approved":
                    continue
                key = f"{c['client_id']}:{p.get('date')}"
                if a.get("state") == "blocked":
                    out[f"clientmedia:{key}"] = (
                        f"بوست Instagram موافق عليه بس مش قادر يتنشر · {p.get('date')}",
                        "السبب: " + "؛ ".join(a.get("issues") or ["غير معروف"]) + f"\nمن الداشبورد: /client/{c['client_id']}/plan")
                elif a.get("state") == "failed":
                    out[f"clientpost:{key}"] = (f"بوست Instagram فشل · {p.get('date')}", f"{a.get('error') or ''}\nراجع /client/{c['client_id']}/plan")
                elif a.get("state") == "ambiguous":
                    out[f"clientpost:{key}"] = (
                        f"بوست Instagram مش متأكدين اتنشر ولا لأ · {p.get('date')}",
                        "افتح حساب Instagram وبوستز وشوف الأول. المصنع مش هيبعته تاني لوحده عشان ما يتنشرش مرتين.\n" + (a.get("error") or ""))
    return out


def check_client_reviews(db_path, now: datetime) -> dict:
    """A monthly strategy review is waiting for the operator (hypogeum.client_review): what last month showed and
    what the proposal changes, each with its evidence. Apply or discard from the buttons or the plan page."""
    try:
        from hypogeum import client_manager as cm
        clients = cm.list_clients(db_path=Path(db_path) if db_path else None)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for c in clients:
        prop = (c.get("brief") or {}).get("strategy_proposal")
        if not prop or prop.get("status") != "pending":
            continue
        lines = [prop.get("summary") or ""]
        if prop.get("thin_data"):
            lines.append(f"⚠️ البيانات قليلة ({prop.get('measured_posts', 0)} بوستات متقاسة)، فالتغييرات المقترحة محدودة.")
        for ch in (prop.get("changes") or [])[:6]:
            lines.append(f"• {ch['what']}\n   لأن: {ch['why']}\n   الدليل: {ch['evidence']}")
        for k in (prop.get("keep") or [])[:3]:
            lines.append(f"✓ يفضل: {k}")
        lines.append(f"التفاصيل: /client/{c['client_id']}/plan")
        out[f"clientreview:{c['client_id']}:{prop['month']}"] = (
            f"مراجعة {prop['month']} لاستراتيجية {c.get('name') or c['client_id']}: اقتراح محتاج قرارك", "\n".join(lines)[:3500])
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
    # A hold whose YouTube upload itself FAILED (e.g. 401) is not "waiting for
    # a cover": the video isn't on YouTube at all (2026-10-08 Wren). The grid
    # knows which platforms failed; best-effort, falls back to the old wording.
    upload_failed: dict = {}
    try:
        from hypogeum.dashboard_content_grid import get_content_grid
        for ch in {r["channel_id"] for r in rows}:
            for it in get_content_grid(ch, db_path=Path(db_path) if db_path else None):
                for fp in (it.get("failed_platforms") or []):
                    if fp.get("platform") == "youtube":
                        upload_failed[it["post_id"]] = str(fp.get("error") or "")
    except Exception:  # noqa: BLE001
        upload_failed = {}
    out = {}
    for r in rows:
        try:
            rel = datetime.fromisoformat(r["release_at"].replace("Z", "+00:00"))
            local = (rel + timedelta(hours=4)).strftime("%a %d %b %H:%M")
        except Exception:  # noqa: BLE001
            local = r["release_at"]
        if r["post_id"] in upload_failed:
            why = upload_failed[r["post_id"]].strip().replace("\n", " ")[:160]
            out[f"cover:{r['post_id']}"] = (
                f"رفع يوتيوب لشورت {r['channel_id']} فشل",
                f"الفيديو مش على يوتيوب أصلاً (الرفع فشل، مش مستني غلاف).\nميعاد النشر: {local} (دبي)\n"
                f"السبب: {why}\nالـ Content Grid: {_DASHBOARD}/channel/{r['channel_id']}/grid",
                _ws_extra(r["channel_id"], db_path, (
                    f"{r['channel_id']}: the YouTube upload failed",
                    f"The Short is not on YouTube at all. Scheduled release: {r['release_at']} (UTC). {why}")))
            continue
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


def check_stuck_releases(db_path, now: datetime) -> dict:
    """Cover confirmed and the slot has passed, yet the Short is still private
    — the flip to public failed or never ran (dead token, server down, laptop
    asleep). check_youtube_covers only watches the unconfirmed ones."""
    conn = _conn(db_path, ddl=False)
    try:
        rows = conn.execute(
            "SELECT h.post_id, h.channel_id, h.release_at, h.status, h.last_error FROM youtube_cover_holds h "
            "WHERE h.status IN ('held','late','release_failed') AND h.release_at <= ? "
            "AND h.post_id IN (SELECT post_id FROM youtube_cover_confirmations)",
            ((now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S"),)).fetchall()
    except Exception:  # noqa: BLE001 — table absent on a fresh DB
        return {}
    finally:
        conn.close()
    out = {}
    for r in rows:
        why = (r["last_error"] or "")[:200]
        out[f"stuckrelease:{r['post_id']}"] = (
            f"شورت {r['channel_id']} الغلاف اتأكد بس لسه private",
            "ميعاده عدّى والغلاف متأكد، لكن قلبه للعامة ماحصلش." + (f"\nالسبب: {why}" if why else "")
            + f"\nلو توكن يوتيوب منتهي: {_DASHBOARD}/platforms/youtube/authorize?channel={r['channel_id']}"
            f"\nالـ Content Grid: {_DASHBOARD}/channel/{r['channel_id']}/grid",
            _ws_extra(r["channel_id"], db_path, (
                f"{r['channel_id']}: cover confirmed but the Short is still private",
                f"Its slot has passed and the cover is confirmed, but it was not made public. {why}".strip())))
    return out


def check_youtube_flip_auth(db_path, *, probe=None) -> dict:
    """The per-channel YouTube authorization that flips held Shorts public must
    still work. A refresh token that Google has revoked/expired (a Testing-mode
    OAuth app expires them after 7 days) would leave every held Short private."""
    conn = _conn(db_path, ddl=False)
    try:
        channels = [r[0] for r in conn.execute(
            "SELECT DISTINCT channel_id FROM channel_provider_bindings WHERE provider_id='postiz' "
            "AND platform='youtube' AND capability='publishing' AND enabled=1")]
    except Exception:  # noqa: BLE001
        return {}
    finally:
        conn.close()
    if probe is None:
        from hypogeum.publishing.youtube_privacy import _access_token

        def probe(ch):
            _access_token(ch, Path(db_path) if db_path else None)
    out = {}
    for ch in channels:
        try:
            probe(ch)
        except Exception as exc:  # noqa: BLE001 — transient network trouble is not an expired token
            if type(exc).__name__ == "SoftFailureError":
                continue
            out[f"ytauth:{ch}"] = (
                f"تصريح يوتيوب لقناة {ch} خلص أو مش موجود",
                f"من غيره الشورتس هتفضل private بعد الغلاف. {str(exc)[:160]}\n"
                f"جدّده (مرة واحدة): {_DASHBOARD}/platforms/youtube/authorize?channel={ch}",
                _ws_extra(ch, db_path, (f"{ch}: YouTube authorization expired",
                                        "Held Shorts cannot be made public until it is renewed.")))
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
                    f"«{title}»\nحالته عند الناشر: {it.get('status')} — المنصات اللي لسه: {stuck}\n"
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


_UNPUBLISHED_GATE_GRACE_MIN = 20     # release trigger re-runs the gate every ~minute
_UNPUBLISHED_NO_ATTEMPT_H = 6        # finished but never handed to publishing
_GATE_PLAIN = {
    "A_VIDEO": "مشهد أو أكتر جودته أقل من المطلوب",
    "B_VISUAL_PLAYBACK": "الفيديو النهائي فيه مشكلة في التشغيل",
    "C_THUMBNAIL": "الغلاف ناقص أو قديم",
    "D_SHORTS_CAPTIONS": "مشكلة في الكابشن",
    "E_END_PAGE": "مشكلة في صفحة النهاية",
    "F_ARTIFACT_COMPATIBILITY": "ملفات قديمة أو مش متوافقة",
    "G_SHORTS_VISUAL_COVERAGE": "فيه أجزاء من الفيديو من غير صورة",
    "H_VERTICAL_MASTER": "مقاس الفيديو النهائي مش مظبوط",
}


def check_unpublished_episodes(db_path, now: datetime) -> dict:
    """A finished episode that isn't going out: the production output gate
    keeps rejecting it, or it was never handed to publishing at all. The run
    itself "completed", so check_run_failures never sees it (2026-10-06:
    ep_31d1891b sat behind a failed gate for ~21h with nobody told)."""
    from hypogeum.db import _DEFAULT_DB_PATH
    dbp = Path(db_path) if db_path else _DEFAULT_DB_PATH
    episodes_dir = dbp.parent / "episodes"
    since = (now - timedelta(days=3)).isoformat()
    conn = _conn(db_path, ddl=False)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT r.episode_id, r.channel_id, MAX(r.completed_at) AS completed_at, "
            "       COALESCE(e.topic, e.title, r.episode_id) AS topic "
            "FROM runs r LEFT JOIN episodes e ON e.episode_id = r.episode_id "
            "WHERE r.status = 'completed' AND r.completed_at >= ? "
            "AND COALESCE(e.status, '') != 'superseded' "
            "AND r.episode_id NOT IN (SELECT episode_id FROM publishing_attempts "
            "                         WHERE state = 'succeeded' AND episode_id IS NOT NULL) "
            "GROUP BY r.episode_id", (since,))]
    except Exception:  # noqa: BLE001 — tables absent on a fresh DB
        return {}
    finally:
        conn.close()
    out = {}
    for r in rows:
        try:
            done = datetime.fromisoformat(r["completed_at"])
            if done.tzinfo is None:
                done = done.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            continue
        age_min = (now - done).total_seconds() / 60
        ep, ch, topic = r["episode_id"], r["channel_id"], (r["topic"] or "")[:90]
        gate = None
        try:
            gate = json.loads((episodes_dir / ep / f"production_output_gate_{ep}.json").read_text())
        except Exception:  # noqa: BLE001
            pass
        if gate is not None and gate.get("passed") is False and age_min >= _UNPUBLISHED_GATE_GRACE_MIN:
            failed = [c for c in gate.get("checks") or [] if c.get("status") == "fail"]
            lines = [f"• {_GATE_PLAIN.get(c.get('capability'), c.get('capability'))}: {(c.get('reason') or '')[:160]}"
                     for c in failed]
            out[f"unpublished:{ep}"] = (
                f"حلقة {ch} خلصت بس واقفة قبل النشر",
                f"«{topic}»\nبوابة الجودة رافضاها من {age_min / 60:.0f} ساعة:\n" + "\n".join(lines)
                + f"\nالحلقة: {ep}",
                _ws_extra(ch, db_path, (f"{ch}: finished episode blocked before publishing",
                                        f"«{topic}»\n" + "\n".join(
                                            f"• {c.get('capability')}: {(c.get('reason') or '')[:160]}" for c in failed))))
        elif gate is None and age_min >= _UNPUBLISHED_NO_ATTEMPT_H * 60:
            out[f"unpublished:{ep}"] = (
                f"حلقة {ch} خلصت ومتجدولتش",
                f"«{topic}»\nخلصت من {age_min / 60:.0f} ساعة ولسه متبعتتش للنشر.\nالحلقة: {ep}",
                _ws_extra(ch, db_path, (f"{ch}: finished episode not scheduled",
                                        f"«{topic}»\nFinished {age_min / 60:.0f}h ago, not handed to publishing.")))
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


_ARCHIVE_BACKLOG_GRACE_H = 2


def check_archive_backlog(db_path, now: datetime, *, pending=None) -> dict:
    """Published episodes whose files still sit on the laptop hours later
    although the sweep retries every minute. 2026-10-07: the NAS was mounted
    but macOS refused the background worker access to it ("Operation not
    permitted") and ep_b2853bbc failed to move every minute, silently — the
    mount itself looked healthy so check_archive stayed quiet."""
    if pending is None:
        from hypogeum.archive_relocation import find_pending_published_episodes
        pending = find_pending_published_episodes(Path(db_path) if db_path else None)
    current = {ep for ep, _ch in pending}
    conn = _conn(db_path)
    try:
        seen = {r["check_name"][len("archive_pending_since:"):]: r["last_run_at"] for r in conn.execute(
            "SELECT * FROM operator_alert_checks WHERE check_name LIKE 'archive_pending_since:%'")}
        for ep in current - set(seen):
            conn.execute("INSERT INTO operator_alert_checks (check_name, last_run_at) VALUES (?, ?)",
                         (f"archive_pending_since:{ep}", now.isoformat()))
            seen[ep] = now.isoformat()
        for ep in set(seen) - current:
            conn.execute("DELETE FROM operator_alert_checks WHERE check_name=?", (f"archive_pending_since:{ep}",))
        conn.commit()
    finally:
        conn.close()
    stuck = sorted(ep for ep in current
                   if (now - datetime.fromisoformat(seen[ep])).total_seconds() >= _ARCHIVE_BACKLOG_GRACE_H * 3600)
    if not stuck:
        return {}
    return {"archive_backlog:stuck": (
        "حلقات منشورة لسه ماتنقلتش للأرشيف",
        f"{len(stuck)} حلقة ملفاتها لسه على اللابتوب من أكتر من {_ARCHIVE_BACKLOG_GRACE_H} ساعة رغم إن النقل "
        f"بيتعاد كل دقيقة: {', '.join(stuck[:5])}\n"
        "لو الـ NAS متوصّل، غالباً macOS مانع البرامج اللي شغالة في الخلفية من الوصول له "
        "(Privacy & Security → Full Disk Access).")}


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


# ---------------------------------------------------------------------------
# Daily summary -- the operator should not have to ask what the state is.
# ---------------------------------------------------------------------------

_SUMMARY_HOUR_UAE = 9          # sent once a day, the first tick at/after 09:00 UAE (UTC+4)
_UAE = timedelta(hours=4)


def _uae(dt: datetime) -> str:
    return (dt + _UAE).strftime("%a %d %b %H:%M")


def build_daily_summary(db_path, now: datetime) -> str:
    """One Arabic message: per channel the buffer, the last finished episode and what is booked
    next; plus YouTube covers waiting for the operator, open alerts and automatic salvages."""
    import os  # noqa: PLC0415
    from hypogeum.scheduler import _PRODUCTION_BUFFER_TARGET, _env_int, channel_buffer_depth
    target = _env_int("PRODUCTION_BUFFER_TARGET", _PRODUCTION_BUFFER_TARGET)
    now_iso = now.isoformat()
    lines = [f"📋 ملخص يومي — {_uae(now)} (الإمارات)", ""]
    conn = _conn(db_path)
    try:
        channels = [r[0] for r in conn.execute(
            "SELECT DISTINCT channel_id FROM schedules WHERE status = 'active' AND channel_id IS NOT NULL ORDER BY channel_id")]
        for ch in channels:
            depth = channel_buffer_depth(ch, conn, now=now)
            last = _last_finished_by_channel(conn).get(ch)
            lines.append(f"▪️ {ch}")
            lines.append(f"   البافر: {depth} من {target}" + (f" · آخر حلقة خلصت: {_uae(_parse_iso(last))}" if last else ""))
            slots = conn.execute(
                "SELECT s.slot_utc, COALESCE(NULLIF(e.title,''), s.episode_id) AS t FROM channel_publish_slots s "
                "LEFT JOIN episodes e ON e.episode_id = s.episode_id "
                "WHERE s.channel_id = ? AND s.slot_utc > ? ORDER BY s.slot_utc LIMIT 4", (ch, now_iso)).fetchall()
            if slots:
                for sl in slots:
                    lines.append(f"   ⏭ {_uae(_parse_iso(sl['slot_utc']))} — {str(sl['t'])[:48]}")
            else:
                lines.append("   ⚠️ مفيش حاجة متجدولة")
        try:
            covers = conn.execute(
                "SELECT h.channel_id, h.release_at, COALESCE(NULLIF(e.title,''), h.episode_id) AS t FROM youtube_cover_holds h "
                "LEFT JOIN episodes e ON e.episode_id = h.episode_id WHERE h.status IN ('held','late') "
                "AND h.post_id NOT IN (SELECT post_id FROM youtube_cover_confirmations) ORDER BY h.release_at").fetchall()
        except Exception:  # noqa: BLE001
            covers = []
        if covers:
            lines += ["", "🖼 أغلفة يوتيوب مطلوبة منك:"]
            lines += [f"   • {c['channel_id']}: {str(c['t'])[:44]} (قبل {_uae(_parse_iso(c['release_at']))})" for c in covers]
        opened = conn.execute("SELECT title FROM operator_alerts WHERE status = 'open' ORDER BY opened_at").fetchall()
        lines += ["", "🔴 تنبيهات مفتوحة: " + (str(len(opened)) if opened else "مفيش")]
        lines += [f"   • {str(r['title'])[:70]}" for r in opened[:5]]
    finally:
        conn.close()
    try:
        from hypogeum import self_heal
        fixes = [i for i in self_heal.recent_incidents(db_path, since=now - timedelta(hours=24)) if i.get("acted")]
        if fixes:
            lines += ["", "🛠 المصنع صلّح بنفسه (آخر 24 ساعة):"]
            for i in fixes[:5]:
                last = [h for h in i["history"] if h.get("acted")][-1]["summary"]
                lines.append(f"   • {i['incident_key']}: {last}" + (" ✅" if i["status"] == "resolved" else " ⏳"))
    except Exception:  # noqa: BLE001
        pass
    try:
        from hypogeum.auto_salvage import list_records
        recs = list_records(db_path, since=now - timedelta(hours=24))
        if recs:
            lines += ["", "🛟 إنقاذ تلقائي (آخر 24 ساعة):"]
            lines += [f"   • {r['episode_id']}: " + ("نجح" if r.get("ok") else ("شغال" if r.get("ok") is None else "فشل")) for r in recs[:4]]
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)


def _parse_iso(ts) -> datetime:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def maybe_send_daily_summary(db_path, config, now: datetime, send: Optional[Callable[[str], bool]] = None) -> bool:
    """Send the summary once per UAE day, at the first tick at/after 09:00 UAE. Never raises."""
    try:
        from hypogeum.db import get_worker_control, set_worker_control
        dbp = Path(db_path) if db_path else None
        local = now + _UAE
        today = local.strftime("%Y-%m-%d")
        if local.hour < _SUMMARY_HOUR_UAE or get_worker_control("daily_summary_sent", "", db_path=dbp) == today:
            return False
        set_worker_control("daily_summary_sent", today, db_path=dbp)       # mark first: a send problem must not repeat
        text = build_daily_summary(db_path, now)
        if send is not None:
            return bool(send(text))
        return bool(_deliver("summary:daily", None, text, None, config, db_path, with_buttons=False))
    except Exception as exc:  # noqa: BLE001
        log.warn("daily_summary_failed", error=str(exc)[:200])
        return False


def run_operator_checks(*, db_path=None, config=None, now: Optional[datetime] = None,
                        send: Optional[Callable[[str], bool]] = None, force: bool = False) -> dict:
    """Run every due check; open/resolve alerts. Returns {check: n_open}."""
    now = now or _now()
    config = _config(config)
    out: dict = {}
    try:
        just_woke = _note_tick_and_wake(db_path, now)
    except Exception as exc:  # noqa: BLE001 — a locked/full DB must not end the loop
        log.warn("operator_tick_failed", error=str(exc)[:200])
        just_woke = False
    checks = [
        ("openrouter_credits", "openrouter:credits", lambda: check_openrouter_credits(config)),
        ("workspace_credits", "openrouter_ws:", lambda: check_workspace_credits(db_path)),
        # right after the machine wakes, the worker hasn't beaten yet either —
        # don't call it "down" until it's had time to (2026-10-04 false alarm)
        ("worker", "worker:", lambda: None if just_woke else check_worker(db_path, now)),
        ("adapters", "system:adapters", lambda: check_adapters(db_path)),
        ("production_paused", "production:", lambda: check_production_paused(db_path)),
        ("run_failures", "run_failed:", lambda: check_run_failures(db_path, now)),
        ("queue_failures", "queue_failed:", lambda: check_queue_failures(db_path, now)),
        ("buffer_gap", "buffer:", lambda: check_buffer_gap(db_path, now)),
        ("production_stalled", "stalled:", lambda: check_production_stalled(db_path, now)),
        ("partial_releases", "partial:", lambda: check_partial_releases(db_path, now)),
        ("public_youtube_shorts", "ytpublic:", lambda: check_public_youtube_shorts(db_path, now)),
        ("postiz_post_errors", "pzerr:", lambda: check_postiz_post_errors(db_path, now)),
        ("client_grants", "clientgrant:", lambda: check_client_grants(db_path, now)),
        ("client_drafts", "clientdraft:", lambda: check_client_drafts(db_path, now)),
        ("client_reviews", "clientreview:", lambda: check_client_reviews(db_path, now)),
        ("client_publishing_media", "clientmedia:", lambda: {k: v for k, v in check_client_publishing(db_path, now).items() if k.startswith("clientmedia:")}),
        ("client_publishing_posts", "clientpost:", lambda: {k: v for k, v in check_client_publishing(db_path, now).items() if k.startswith("clientpost:")}),
        ("blocking_signals", "signal:", lambda: check_blocking_signals(db_path)),
        ("youtube_covers", "cover:", lambda: check_youtube_covers(db_path, now)),
        ("stuck_releases", "stuckrelease:", lambda: check_stuck_releases(db_path, now)),
        ("youtube_flip_auth", "ytauth:", lambda: check_youtube_flip_auth(db_path)),
        ("posts", "post", lambda: check_posts(db_path, now)),
        ("unpublished", "unpublished:", lambda: check_unpublished_episodes(db_path, now)),
        ("accounts", "account:", lambda: check_accounts(db_path)),
        ("disk", "disk:", lambda: check_disk()),
        ("archive", "archive:", lambda: check_archive(db_path, now)),
        ("archive_backlog", "archive_backlog:", lambda: check_archive_backlog(db_path, now)),
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
    maybe_send_daily_summary(db_path, config, now, send)
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
            # one uncaught error used to end this thread for good, silently:
            # 2026-10-05 a "database is locked" while the disk was full stopped
            # every alert (disk full included) for ~22h. Never again.
            try:
                run_operator_checks(db_path=db_path)
            except Exception as exc:  # noqa: BLE001
                log.warn("operator_alerts_loop_error", error=str(exc)[:200])
            stop.wait(check_every_s)

    t = threading.Thread(target=_loop, name="operator-alerts", daemon=True)
    t.stop_event = stop  # type: ignore[attr-defined]
    t.start()
    log.info("operator_alerts_loop_started", check_every_s=check_every_s)
    return t
