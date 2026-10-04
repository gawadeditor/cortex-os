"""
hypogeum/operator_actions.py — act on an alert straight from Telegram.

Every operator alert comes with buttons for the thing the operator would
otherwise have to open a terminal for (2026-10-05: "I got the messages and
had nothing I could do with them"). Pressing one runs the action here and
the bot answers with the outcome. Plus two commands: /status (factory summary
+ buttons for whatever needs attention) and /help.

Only the OPERATOR's chats (the platform workspace's recipients and the .env
chat) may run these — a client chat only ever gets its approval buttons
(hypogeum.publish_approvals).

    buttons_for(alert_key) -> Optional[dict]            # Telegram inline_keyboard
    handle(chat_id, data, *, db_path=None) -> str        # 'act:<name>:<arg>' press
    handle_command(chat_id, text, *, db_path=None) -> Optional[tuple[str, dict|None]]
    status_text(db_path=None) -> tuple[str, dict|None]

Actions (callback data 'act:<name>:<arg>', <= 64 bytes):
    resume            clear the spend breaker (global + operator workspace) / arg = client workspace
    restart_worker    launchctl kickstart -k com.hypogeum.worker
    mount_archive     run scripts/mount_archive_share.sh
    disk              biggest folders on this machine
    cover_done        arg = post_id: record that the YouTube cover was uploaded in Studio
    adapters          supersede stale adapter registrations (committed code only)
    status            same as /status
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

from hypogeum.logger import get_logger

log = get_logger(agent="operator_actions")

_ROOT = Path(__file__).resolve().parent.parent
_RUN: Callable = subprocess.run          # tests replace this


def _p(db_path):
    if db_path:
        return Path(db_path)
    import hypogeum.db as _db
    return Path(_db._DEFAULT_DB_PATH)


def _btn(text: str, action: str, arg: str = "") -> dict:
    return {"text": text, "callback_data": f"act:{action}:{arg}"[:64]}


def _url(text: str, url: str) -> dict:
    return {"text": text, "url": url}


def _studio_url(post_id: str, db_path=None) -> Optional[str]:
    """YouTube Studio edit page for this post's YouTube video, if known."""
    import json
    from hypogeum.db import get_connection
    try:
        conn = get_connection(_p(db_path))
        try:
            row = conn.execute("SELECT platforms_json FROM content_grid_cache WHERE post_id=?", (post_id,)).fetchone()
        finally:
            conn.close()
        for pl in json.loads(row["platforms_json"] or "[]") if row else []:
            url = pl.get("url") or ""
            if pl.get("platform") == "youtube" and "v=" in url:
                return f"https://studio.youtube.com/video/{url.split('v=')[1].split('&')[0]}/edit"
            if pl.get("platform") == "youtube" and "/shorts/" in url:
                return f"https://studio.youtube.com/video/{url.split('/shorts/')[1].split('?')[0]}/edit"
    except Exception:  # noqa: BLE001
        pass
    return None


def buttons_for(alert_key: str, db_path=None) -> Optional[dict]:
    """The action buttons that resolve this alert (operator chats only)."""
    rows: list = []
    if alert_key == "production:paused":
        rows = [[_btn("▶️ رجّع الإنتاج", "resume")], [_btn("📋 حالة المصنع", "status")]]
    elif alert_key.startswith("production:paused:"):
        wid = alert_key.split(":", 2)[2].split(":")[0]
        rows = [[_btn(f"▶️ رجّع إنتاج {wid}", "resume", wid)]]
    elif alert_key == "worker:down":
        rows = [[_btn("🔄 شغّل الـ worker", "restart_worker")], [_btn("📋 حالة المصنع", "status")]]
    elif alert_key == "archive:down":
        rows = [[_btn("🔄 جرّب أوصّل الأرشيف", "mount_archive")]]
    elif alert_key == "disk:low":
        rows = [[_btn("📊 إيه اللي واخد المساحة", "disk")]]
    elif alert_key == "system:adapters_stale":
        rows = [[_btn("🛠 سجّل الـ adapters", "adapters")]]
    elif alert_key.startswith(("openrouter:", "openrouter_ws:")):
        rows = [[_url("💳 اشحن OpenRouter", "https://openrouter.ai/settings/credits")]]
    elif alert_key.startswith(("cover:", "postcover:")):
        post_id = alert_key.split(":", 1)[1]
        studio = _studio_url(post_id, db_path)
        rows = ([[_url("🎬 افتح YouTube Studio", studio)]] if studio else []) + \
               [[_btn("✅ رفعت الغلاف", "cover_done", post_id)]]
    return {"inline_keyboard": rows} if rows else None


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

def _resume(arg: str, db_path) -> str:
    from hypogeum.muapi_spend_breaker import clear_breaker
    from hypogeum.workspaces import platform_workspace_id
    if arg and arg != platform_workspace_id(_p(db_path)):
        clear_breaker(db_path=_p(db_path), workspace_id=arg)
        return f"▶️ رجّعت إنتاج {arg}. أول حلقة جاية هتتعمل في ميعادها."
    clear_breaker(db_path=_p(db_path))
    return ("▶️ رجّعت الإنتاج. عدّاد الفشلات اتصفّر، فلو السبب لسه موجود الفرامل هتقفل تاني بعد 3 فشلات "
            "ويوصلك تنبيه. الحلقة الجاية هتتعمل في ميعادها.")


def _restart_worker(arg: str, db_path) -> str:
    r = _RUN(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.hypogeum.worker"],
             capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return f"⚠️ مقدرتش أشغّله: {(r.stderr or r.stdout or '').strip()[:200]}"
    return "🔄 الـ worker اشتغل من جديد. هيبدأ يبعت نبضه خلال دقيقة، ولو موصلش هيوصلك تنبيه."


def _mount_archive(arg: str, db_path) -> str:
    script = _ROOT / "scripts" / "mount_archive_share.sh"
    try:
        _RUN(["/bin/bash", str(script)], capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        pass
    from hypogeum.archive_storage import _raw_configured_root
    raw, _src = _raw_configured_root(_p(db_path))
    parts = Path(raw).parts
    mounted = len(parts) >= 3 and os.path.ismount(Path(parts[0], parts[1], parts[2]))
    return ("✅ الأرشيف اتوصّل." if mounted else
            "⚠️ لسه مش متوصّل. الماك مش لاقي الـ NAS على الشبكة — اتأكد إنه شغال وعلى نفس الواي فاي.")


def _disk(arg: str, db_path) -> str:
    import shutil
    u = shutil.disk_usage("/System/Volumes/Data" if Path("/System/Volumes/Data").exists() else "/")
    lines = [f"📊 الفاضي: {u.free / 1e9:.0f} جيجا من {u.total / 1e9:.0f}"]
    home = Path.home()
    cands = [home / d for d in ("Downloads", "Desktop", "Movies", "Documents", "Library/Caches")] + \
            [_ROOT / ".hypogeum-assets", _ROOT / "vendor"]
    sizes = []
    for c in cands:
        if c.exists():
            r = _RUN(["du", "-sk", str(c)], capture_output=True, text=True, timeout=120)
            try:
                sizes.append((int(r.stdout.split()[0]) / 1e6, c))
            except Exception:  # noqa: BLE001
                pass
    for gb, c in sorted(sizes, reverse=True)[:6]:
        lines.append(f"• {str(c).replace(str(home), '~')}: {gb:.1f} جيجا")
    return "\n".join(lines)


def _cover_done(post_id: str, db_path) -> str:
    from hypogeum.db import get_connection
    from hypogeum.dashboard_content_grid import mark_youtube_cover_done
    conn = get_connection(_p(db_path))
    try:
        row = conn.execute("SELECT channel_id FROM youtube_cover_holds WHERE post_id=?", (post_id,)).fetchone() \
            or conn.execute("SELECT channel_id FROM content_grid_cache WHERE post_id=?", (post_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return "⚠️ مش لاقي البوست ده."
    mark_youtube_cover_done(post_id, row["channel_id"], db_path=_p(db_path))
    return "✅ اتسجّل إن الغلاف اترفع. الشورت هيبقى public في ميعاده."


def _adapters(arg: str, db_path) -> str:
    r = _RUN(["python3", str(_ROOT / "scripts" / "supersede_changed_adapters.py"), "--apply",
              "--actor", "telegram-operator", "--reason", "re-registered from Telegram after an adapter code change"],
             capture_output=True, text=True, timeout=180, cwd=str(_ROOT))
    out = (r.stdout or "").strip().splitlines()
    return ("🛠 " + (out[-1] if out else "done")) if r.returncode == 0 else \
        f"⚠️ مقدرتش أسجّلهم (لازم الكود يكون متعمله commit): {(r.stderr or '').strip()[-200:]}"


ACTIONS = {"resume": _resume, "restart_worker": _restart_worker, "mount_archive": _mount_archive,
           "disk": _disk, "cover_done": _cover_done, "adapters": _adapters}


def _operator_chats(db_path) -> set:
    from hypogeum import alert_recipients as ar
    return set(ar.chat_ids_for(None, db_path=db_path))


def handle(chat_id: str, data: str, *, db_path=None) -> str:
    try:
        _, name, arg = data.split(":", 2)
    except ValueError:
        return "Unknown action"
    if str(chat_id) not in _operator_chats(db_path):
        log.warn("operator_action_unauthorized_chat", action=name)
        return "This chat can't run factory actions"
    if name == "status":
        return status_text(db_path)[0]
    fn = ACTIONS.get(name)
    if not fn:
        return "Unknown action"
    log.info("operator_action", action=name, arg=arg)
    try:
        return fn(arg, db_path)
    except Exception as exc:  # noqa: BLE001
        log.warn("operator_action_failed", action=name, error=str(exc)[:200])
        return f"⚠️ حصل خطأ: {str(exc)[:200]}"


# ---------------------------------------------------------------------------
# /status and /help
# ---------------------------------------------------------------------------

def status_text(db_path=None) -> tuple:
    """Plain-language factory summary + buttons for whatever needs action."""
    from datetime import datetime, timezone
    from hypogeum.db import get_connection, get_worker_control
    dbp = _p(db_path)
    lines, rows = ["📋 حالة المصنع"], []
    hb = get_worker_control("worker_heartbeat_at", "", db_path=dbp)
    status = get_worker_control("worker_status", "", db_path=dbp)
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb)).total_seconds() / 60
        lines.append(f"• الـ worker: {status or '?'} — آخر نبضة من {age:.0f} دقيقة")
        if age > 20 and status != "running":
            rows.append([_btn("🔄 شغّل الـ worker", "restart_worker")])
    except Exception:  # noqa: BLE001
        lines.append("• الـ worker: مفيش نبضة مسجلة")
        rows.append([_btn("🔄 شغّل الـ worker", "restart_worker")])
    from hypogeum.workspace_controls import operator_production_paused, paused_workspaces
    if operator_production_paused(dbp):
        reason = get_worker_control("queue_paused_reason", "", db_path=dbp)
        lines.append(f"• الإنتاج: ⛔️ واقف — {reason[:160] or 'سبب غير مكتوب'}")
        rows.append([_btn("▶️ رجّع الإنتاج", "resume")])
    else:
        lines.append("• الإنتاج: ✅ شغال")
    for wid, reason in paused_workspaces(dbp).items():
        lines.append(f"• {wid}: ⛔️ واقف — {reason[:120]}")
    conn = get_connection(dbp)
    try:
        run = conn.execute("SELECT channel_id, episode_id, status, started_at FROM runs "
                           "ORDER BY started_at DESC LIMIT 1").fetchone()
        try:
            opened = [r["title"] for r in conn.execute(
                "SELECT title FROM operator_alerts WHERE status='open' ORDER BY opened_at")]
        except Exception:  # noqa: BLE001
            opened = []
        try:
            covers = conn.execute("SELECT COUNT(*) FROM youtube_cover_holds WHERE status IN ('held','late') "
                                  "AND post_id NOT IN (SELECT post_id FROM youtube_cover_confirmations)").fetchone()[0]
        except Exception:  # noqa: BLE001
            covers = 0
    finally:
        conn.close()
    if run:
        lines.append(f"• آخر حلقة: {run['channel_id']} {run['episode_id']} — {run['status']} ({run['started_at'][:16]} UTC)")
    if covers:
        lines.append(f"• أغلفة يوتيوب مستنية: {covers}")
    lines.append("• تنبيهات مفتوحة: " + ("، ".join(opened) if opened else "مفيش"))
    return "\n".join(lines), ({"inline_keyboard": rows} if rows else None)


_HELP = ("أوامر البوت:\n/status — حالة المصنع وزراير لأي حاجة محتاجة تدخّل\n/help — الأوامر دي\n\n"
         "وكل تنبيه بييجي معاه زراير تحل المشكلة من هنا.")


def handle_command(chat_id: str, text: str, *, db_path=None) -> Optional[tuple]:
    """(reply, reply_markup) for an operator command, or None if not one."""
    cmd = (text or "").strip().split()[0].lower() if (text or "").strip() else ""
    if cmd not in ("/status", "/help"):
        return None
    if str(chat_id) not in _operator_chats(db_path):
        return ("This chat only receives its workspace's alerts and approvals.", None)
    if cmd == "/help":
        return (_HELP, None)
    return status_text(db_path)
