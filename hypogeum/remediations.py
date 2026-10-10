"""
hypogeum/remediations.py — the fixes the factory applies to itself (see hypogeum.self_heal).

Each remediation is keyed by the operator-alert key of the problem it answers, is idempotent, bounded by
the engine (max attempts / min interval), and NEVER touches money: no budget, credit, grant or ceiling is
ever changed here.

    partial:<episode>        restore_episode_assets   -- the release needs the episode's files; bring them back
    ytpublic:<post>          fix_public_youtube_short -- a Short queued PUBLIC on YouTube with no cover hold
    worker:down              restart_worker           -- the worker stopped beating and nothing is running
    buffer:<ch> stalled:<ch> kick_production          -- a channel is running out of episodes: start one
                                                         (OPT-IN: Config.SELF_HEAL_AUTO_PRODUCTION, default off,
                                                         because a run spends money)

(`kick_production` only adds one pending scheduled run -- the normal gates, budget reservations and plan
checks all still apply to it.)
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from hypogeum.self_heal import Ctx, Outcome, Remediation, register

_KICK_COOLDOWN = timedelta(hours=6)
_PRERUN_FAILURE_LOOKBACK = timedelta(hours=6)


def _arg(key: str) -> str:
    return key.split(":", 1)[1] if ":" in key else ""


def _conn(db_path):
    from hypogeum.db import get_connection  # noqa: PLC0415
    c = get_connection(Path(db_path) if db_path else None)
    c.execute("PRAGMA busy_timeout = 15000")
    return c


# ---------------------------------------------------------------------------
# partial:<episode> -- the files a release still needs
# ---------------------------------------------------------------------------

def _working_dir() -> Path:
    from hypogeum.archive_relocation import _WORKING_EPISODES_DIR  # noqa: PLC0415
    return _WORKING_EPISODES_DIR


def _has_files(d: Path) -> bool:
    try:
        return d.is_dir() and any(p.is_file() for p in d.rglob("*"))
    except OSError:
        return False


def _find_archived_dir(episode_id: str, db_path) -> Optional[Path]:
    from hypogeum.archive_storage import resolve_archive_root  # noqa: PLC0415
    root = resolve_archive_root(Path(db_path) if db_path else None)
    for pattern in (f"*/{episode_id} - *", f"*/{episode_id}"):
        for d in sorted(root.glob(pattern)):
            if d.is_dir():
                return d
    return None


def restore_episode_assets(ctx: Ctx) -> Outcome:
    episode_id = _arg(ctx.key)
    work = _working_dir() / episode_id
    if _has_files(work):
        # The files are here, so the missing legs are failing for a provider-side reason (the alert body names
        # it); copying files cannot help.
        return Outcome("ملفات الحلقة موجودة محليًا، فالمشكلة من المنصة نفسها", acted=False, give_up=True)
    src = _find_archived_dir(episode_id, ctx.db_path)
    if src is None:
        return Outcome("ملفات الحلقة مش موجودة لا محليًا ولا في الأرشيف (ممكن الـ NAS مش متوصل)", acted=False, give_up=True)
    shutil.copytree(src, work, dirs_exist_ok=True)
    return Outcome("رجّعت ملفات الحلقة من الأرشيف عشان باقي المنصات تتجدول", acted=True)


# ---------------------------------------------------------------------------
# ytpublic:<post> -- a YouTube Short queued public with no cover hold
# ---------------------------------------------------------------------------

def _lookup_post(db_path, post_id: str) -> Optional[dict]:
    conn = _conn(db_path)
    try:
        r = conn.execute(
            "SELECT channel_id, episode_id FROM provider_content_mappings WHERE provider_content_id = ? "
            "AND platform = 'youtube' LIMIT 1", (post_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _short_native(channel_id: str, db_path) -> bool:
    try:
        from hypogeum.channel_config import get_channel_operations_profile  # noqa: PLC0415
        prof = get_channel_operations_profile(channel_id, db_path=Path(db_path) if db_path else None)
        cad = getattr(prof, "production_cadence", None) or {}
        return int(cad.get("videos_per_week", 1)) == 0 and int(cad.get("shorts_per_week", 0)) > 0
    except Exception:  # noqa: BLE001
        return False


def find_public_youtube_shorts(db_path, now: datetime, postiz=None) -> List[dict]:
    """YouTube posts still in Postiz's queue as PUBLIC, belonging to a short-native channel's episode, with no
    cover-hold row: [{post_id, publish_date, channel_id, episode_id}]."""
    import json  # noqa: PLC0415
    from hypogeum.publishing.postiz import PostizProvider  # noqa: PLC0415
    postiz = postiz or PostizProvider()
    posts = postiz.list_posts(start=now - timedelta(hours=1), end=now + timedelta(days=14),
                              db_path=Path(db_path) if db_path else None)
    conn = _conn(db_path)
    try:
        held = {r[0] for r in conn.execute("SELECT post_id FROM youtube_cover_holds")}
    except Exception:  # noqa: BLE001
        held = set()
    finally:
        conn.close()
    out = []
    for p in posts:
        integ = p.get("integration")
        if isinstance(integ, str):
            integ = {}
        if (integ or {}).get("providerIdentifier") != "youtube" or str(p.get("state") or "").upper() != "QUEUE":
            continue
        try:
            st = json.loads(p["settings"]) if isinstance(p.get("settings"), str) else (p.get("settings") or {})
        except ValueError:
            continue
        if st.get("type") != "public" or p["id"] in held:
            continue
        m = _lookup_post(db_path, p["id"])
        if not m or not _short_native(m["channel_id"], db_path):
            continue
        out.append({"post_id": p["id"], "publish_date": p.get("publishDate"), **m})
    return out


def fix_public_youtube_short(ctx: Ctx, postiz=None) -> Outcome:
    from hypogeum.publishing.postiz import PostizProvider  # noqa: PLC0415
    from hypogeum.publishing.youtube_cover_hold import record_hold  # noqa: PLC0415
    post_id = _arg(ctx.key)
    postiz = postiz or PostizProvider()
    dbp = Path(ctx.db_path) if ctx.db_path else None
    found = [f for f in find_public_youtube_shorts(ctx.db_path, ctx.now, postiz) if f["post_id"] == post_id]
    if not found:
        return Outcome("المنشور مش في طابور Postiz كـ public (اتغيّر أو اتنشر)", acted=False)
    f = found[0]
    when = datetime.fromisoformat(str(f["publish_date"]).replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if not postiz.set_post_settings(post_id, {"type": "private"}, db_path=dbp):
        return Outcome("Postiz رفض تعديل إعدادات المنشور", acted=False)
    record_hold(post_id, f["channel_id"], f["episode_id"], release_at=when, upload_at=when, db_path=dbp)
    return Outcome("حوّلت الـ Short لـ private وسجّلت الـ hold، فمش هيتنشر public من غير الغلاف", acted=True)


# ---------------------------------------------------------------------------
# worker:down -- restart the worker
# ---------------------------------------------------------------------------

def restart_worker(ctx: Ctx) -> Outcome:
    import os  # noqa: PLC0415
    conn = _conn(ctx.db_path)
    try:
        running = conn.execute("SELECT COUNT(*) FROM runs WHERE status = 'running'").fetchone()[0]
    finally:
        conn.close()
    if running:
        return Outcome("فيه run شغال دلوقتي، مش هعمل restart (هيقطعه)", acted=False)
    if sys.platform != "darwin":
        return Outcome("restart تلقائي متاح على macOS بس", acted=False, give_up=True)
    rc = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.hypogeum.worker"],
                        capture_output=True, text=True, timeout=60)
    if rc.returncode != 0:
        return Outcome(f"launchctl فشل: {(rc.stderr or rc.stdout).strip()[:120]}", acted=False, give_up=True)
    return Outcome("عملت restart للـ worker", acted=True)


# ---------------------------------------------------------------------------
# buffer:<ch> / stalled:<ch> -- a channel is running out of episodes: start one (OPT-IN)
# ---------------------------------------------------------------------------

def _auto_production_enabled(ctx: Ctx) -> bool:
    cfg = ctx.config
    if cfg is None:
        try:
            from hypogeum.config import Config  # noqa: PLC0415
            cfg = Config()
        except Exception:  # noqa: BLE001
            return False
    return bool(getattr(cfg, "SELF_HEAL_AUTO_PRODUCTION", False))


def kick_production(ctx: Ctx) -> Outcome:
    from hypogeum.db import get_worker_control, insert_scheduled_run, set_worker_control  # noqa: PLC0415
    ch = _arg(ctx.key)
    if not _auto_production_enabled(ctx):
        return Outcome("الإنتاج التلقائي مقفول (SELF_HEAL_AUTO_PRODUCTION=false) لأن كل حلقة بتصرف فلوس", acted=False, give_up=True)
    dbp = Path(ctx.db_path) if ctx.db_path else None
    if get_worker_control("queue_paused", "0", db_path=dbp) == "1":
        return Outcome("الإنتاج موقوف (paused) بقرار، مش هبدأ حاجة", acted=False, give_up=True)
    kicked = get_worker_control(f"self_heal_kick:{ch}", "", db_path=dbp)
    try:
        last = datetime.fromisoformat(kicked) if kicked else None
    except ValueError:
        last = None
    if last and ctx.now - last < _KICK_COOLDOWN:
        return Outcome("بدأت إنتاج للقناة من أقل من 6 ساعات، مستني النتيجة", acted=False)
    conn = _conn(dbp)
    try:
        busy = conn.execute(
            "SELECT (SELECT COUNT(*) FROM runs WHERE channel_id = ? AND status IN ('running','queued','paused')) + "
            "(SELECT COUNT(*) FROM run_queue WHERE channel_id = ? AND status IN ('queued','claimed')) + "
            "(SELECT COUNT(*) FROM scheduled_runs WHERE channel_id = ? AND status = 'pending')", (ch, ch, ch)).fetchone()[0]
        if busy:
            return Outcome("فيه إنتاج شغال أو مستني للقناة دلوقتي", acted=False)
        since = (ctx.now - _PRERUN_FAILURE_LOOKBACK).isoformat()
        bad = conn.execute(
            "SELECT q.error_message FROM run_queue q WHERE q.channel_id = ? AND q.status = 'failed' "
            "AND COALESCE(q.failed_at, q.created_at) >= ? AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.episode_id = q.episode_id) "
            "ORDER BY q.created_at DESC LIMIT 1", (ch, since)).fetchone()
        if bad:
            return Outcome("آخر محاولة إنتاج فشلت قبل ما تبدأ ومش هكررها من غير ما السبب يتصلّح: "
                           + str(bad[0] or "")[:110], acted=False, give_up=True)
        sched = conn.execute("SELECT schedule_id FROM schedules WHERE channel_id = ? AND status = 'active' LIMIT 1", (ch,)).fetchone()
        if sched is None:
            return Outcome("مفيش جدول إنتاج نشط للقناة", acted=False, give_up=True)
        topics = conn.execute("SELECT COUNT(*) FROM episode_topics WHERE channel_id = ? AND status = 'queued'", (ch,)).fetchone()[0]
        if not topics:
            return Outcome("مفيش مواضيع في طابور القناة", acted=False, give_up=True)
        now_iso = ctx.now.isoformat()
        insert_scheduled_run(conn, scheduled_run_id=str(uuid.uuid4()), schedule_id=sched["schedule_id"],
                             scheduled_for=now_iso, created_at=now_iso, channel_id=ch, status="pending")
        conn.commit()
    finally:
        conn.close()
    set_worker_control(f"self_heal_kick:{ch}", ctx.now.isoformat(), db_path=dbp)
    return Outcome(f"بدأت إنتاج حلقة جديدة لـ {ch} (الميزانية والـ gates بتتفحص زي أي حلقة)", acted=True)


# ---------------------------------------------------------------------------

register(Remediation("restore_episode_assets", lambda k: k.startswith("partial:"), restore_episode_assets,
                     max_attempts=2, min_interval_s=120))
register(Remediation("fix_public_youtube_short", lambda k: k.startswith("ytpublic:"), fix_public_youtube_short,
                     max_attempts=2, min_interval_s=300))
register(Remediation("restart_worker", lambda k: k == "worker:down", restart_worker,
                     max_attempts=2, min_interval_s=600))
register(Remediation("kick_production", lambda k: k.startswith(("buffer:", "stalled:")), kick_production,
                     max_attempts=2, min_interval_s=3600))
