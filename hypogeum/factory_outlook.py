"""
hypogeum/factory_outlook.py — per channel: episodes ready ahead vs the buffer
target, when the next one gets produced (or why not yet), and the real
publishing schedule. One source for Telegram /status and the dashboard home
(2026-10-05: the home page showed a hand-typed "7 Shorts/week" target table
that had nothing to do with the real Mon/Wed/Sat schedule).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="factory_outlook")

_DAY_ABBR = {"monday": "Mon", "tuesday": "Tue", "wednesday": "Wed", "thursday": "Thu",
             "friday": "Fri", "saturday": "Sat", "sunday": "Sun"}


def schedule_slots(channel_id: str, db_path=None) -> dict:
    """{"slots": ["Mon 10:00", ...], "timezone": "UTC", "per_week": 3} for the
    channel's main deliverable, or {} when it has no weekly-slot schedule."""
    try:
        from hypogeum.channel_config import get_channel_operations_profile
        prof = get_channel_operations_profile(channel_id, db_path=db_path)
        dsp = getattr(prof, "deliverable_schedule_policy", None) or {}
        pol = (dsp.get("main_episode") or next(iter(dsp.values()), None)) if isinstance(dsp, dict) and dsp else None
        pol = pol or getattr(prof, "publishing_schedule", None) or {}
        slots = pol.get("weekly_slots") or []
        if not slots:
            return {}
        return {"slots": [f"{_DAY_ABBR.get(str(s.get('day_of_week')).lower(), s.get('day_of_week'))} "
                          f"{s.get('time_of_day')}" for s in slots],
                "timezone": pol.get("timezone") or getattr(prof, "timezone", None) or "UTC",
                "per_week": len(slots)}
    except Exception as exc:  # noqa: BLE001
        log.warn("schedule_slots_failed", channel_id=channel_id, error=str(exc)[:200])
        return {}


def channel_outlook(db_path=None, now: Optional[datetime] = None) -> list[dict]:
    """[{channel_id, ready, target, state, next_production_at, schedule}] —
    state: "enough" (buffer full) | "next_day" (today's attempt used; next at
    next_production_at) | "due" (production starts at its next tick)."""
    from hypogeum import scheduler as sch
    from hypogeum.db import get_connection
    now = now or datetime.now(timezone.utc)
    out = []
    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT channel_id, next_run_at, max_runs_per_day FROM schedules "
                            "WHERE status='active' AND channel_id IS NOT NULL ORDER BY channel_id").fetchall()
        target = sch._env_int("PRODUCTION_BUFFER_TARGET", sch._PRODUCTION_BUFFER_TARGET)
        for r in rows:
            ch = r["channel_id"]
            depth = sch.channel_buffer_depth(ch, conn, now=now)
            day_start = sch._channel_local_day_start_utc(ch, now, db_path)
            used = sch._scheduled_runs_since(ch, conn, day_start.isoformat())
            cap = r["max_runs_per_day"] or 0
            if target and depth >= target:
                state, nxt = "enough", None
            elif cap and used >= cap:
                state, nxt = "next_day", (day_start + timedelta(days=1)).isoformat()
            else:
                state, nxt = "due", r["next_run_at"]
            out.append({"channel_id": ch, "ready": depth, "target": target, "state": state,
                        "next_production_at": nxt, "schedule": schedule_slots(ch, db_path)})
    except Exception as exc:  # noqa: BLE001
        log.warn("channel_outlook_failed", error=str(exc)[:200])
    finally:
        conn.close()
    return out


# The open operator alerts (the same ones sent to Telegram, whose text is
# Arabic) in English for the dashboard, each with the action that fixes it.
def _alert_view(key: str, db_path) -> dict:
    v = {"key": key, "label": key, "detail": "", "action": None, "link": None}
    if key == "production:paused" or key.startswith("production:paused:"):
        from hypogeum.db import get_worker_control
        v.update(label="Production is paused",
                 detail=get_worker_control("queue_paused_reason", "", db_path=db_path) or "",
                 action=("resume", key.split(":", 2)[2] if key.count(":") >= 2 else "", "Resume production"))
    elif key == "worker:down":
        v.update(label="The worker isn't running", detail="Nothing gets produced until it is back.",
                 action=("restart_worker", "", "Restart worker"))
    elif key == "archive:down":
        v.update(label="The NAS archive isn't connected",
                 detail="Finished episodes stay on this computer until it is back (it reconnects on its own on the home network).",
                 action=("mount_archive", "", "Try to connect"))
    elif key == "disk:low":
        v.update(label="Disk almost full", detail="If the disk fills up, production stops.")
    elif key == "system:adapters_stale":
        v.update(label="Provider adapters changed but not re-registered",
                 detail="Every paid call is refused until they are registered.",
                 action=("adapters", "", "Register adapters"))
    elif key.startswith("run_failed:"):
        ep = key.split(":", 1)[1]
        v.update(label=f"Production attempt failed ({ep})", detail=_run_error(ep, db_path))
    elif key.startswith(("openrouter:", "openrouter_ws:")):
        v.update(label="OpenRouter credits are running out", detail="Scripts can't be written without credits.",
                 link=("https://openrouter.ai/settings/credits", "Top up"))
    elif key.startswith(("cover:", "postcover:")):
        v.update(label="A YouTube Short needs its cover", detail="Upload it in YouTube Studio, then mark it done on the Grid.")
    return v


def _run_error(episode_id: str, db_path) -> str:
    from hypogeum.db import get_connection
    conn = get_connection(db_path)
    try:
        r = conn.execute("SELECT error_message FROM runs WHERE episode_id=? ORDER BY started_at DESC LIMIT 1",
                         (episode_id,)).fetchone()
        return (r[0] or "")[:200] if r else ""
    finally:
        conn.close()


def open_operator_alerts(db_path=None) -> list[dict]:
    from hypogeum.db import get_connection
    try:
        conn = get_connection(db_path)
        try:
            keys = [r[0] for r in conn.execute(
                "SELECT alert_key FROM operator_alerts WHERE status='open' ORDER BY opened_at DESC")]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — table not created yet
        return []
    return [_alert_view(k, db_path) for k in keys]
