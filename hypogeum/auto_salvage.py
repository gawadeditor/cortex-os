"""
hypogeum/auto_salvage.py — let the worker rescue a failed-but-nearly-complete episode itself.

Why: a paid episode that dies at assembly/QA (a clip that arrived after a poll timeout, a
missing clip ...) used to be thrown away unless the operator (or an assistant) ran
tools/salvage_episode.py by hand -- pausing the worker, clearing stale checkpoints, restoring
the weekly hold. The recovery pieces now exist (muapi clip recovery, plan-weekly restore,
stale-checkpoint stash); this module is the trigger.

    find_candidates(db_path, now=None) -> [episode_id]
    run_auto_salvage(db_path, config, log) -> dict | None     # at most ONE episode per call

Rules (all fail-closed):
  * only an episode whose LATEST run failed in the last 48h with a QA rejection that is about the
    ASSEMBLY (no viable render / no video asset) -- never a content/compliance rejection;
  * at most ONE automatic attempt per episode, ever (recorded in worker_control BEFORE it starts);
  * only when nothing else is running or queued, and the queue is not paused;
  * episode_salvage's own preconditions, weekly-headroom proof and spend gates apply unchanged;
  * the outcome is recorded (worker_control 'auto_salvage:<episode>') for the daily summary.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

_WINDOW = timedelta(hours=48)
_KEY = "auto_salvage:"
_ASSEMBLY_MARKERS = ("assembly did not produce a viable package", "video_asset_id is empty")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def find_candidates(db_path: Optional[Path] = None, now: Optional[datetime] = None) -> List[str]:
    from hypogeum.db import get_connection, get_worker_control  # noqa: PLC0415
    now = now or _now()
    since = (now - _WINDOW).isoformat()
    conn = get_connection(db_path)
    try:
        busy = conn.execute("SELECT COUNT(*) FROM runs WHERE status IN ('running','queued','paused')").fetchone()[0]
        busy += conn.execute("SELECT COUNT(*) FROM run_queue WHERE status IN ('queued','claimed')").fetchone()[0]
        if busy:
            return []
        rows = conn.execute(
            "SELECT r.episode_id, r.status, r.error_message FROM runs r "
            "WHERE r.started_at >= ? AND r.started_at = ("
            "  SELECT MAX(r2.started_at) FROM runs r2 WHERE r2.episode_id = r.episode_id) "
            "ORDER BY r.started_at DESC", (since,)).fetchall()
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        err = str(r["error_message"] or "")
        if r["status"] not in ("failed", "error") or "blocked by QA Agent" not in err:
            continue
        if not any(m in err for m in _ASSEMBLY_MARKERS):
            continue
        if get_worker_control(_KEY + r["episode_id"], "", db_path=db_path):
            continue                                       # already attempted once
        out.append(r["episode_id"])
    return out


def _record(db_path, episode_id: str, **fields) -> None:
    from hypogeum.db import set_worker_control  # noqa: PLC0415
    fields["at"] = _now().isoformat()
    set_worker_control(_KEY + episode_id, json.dumps(fields, ensure_ascii=False), db_path=db_path)


def list_records(db_path: Optional[Path] = None, since: Optional[datetime] = None) -> List[dict]:
    """[{episode_id, status, ok, reason, at}] of automatic salvage attempts (newest first)."""
    from hypogeum.db import get_connection  # noqa: PLC0415
    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT key, value FROM worker_control WHERE key LIKE ?", (_KEY + "%",)).fetchall()
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            d = json.loads(r["value"])
        except (TypeError, ValueError):
            continue
        d["episode_id"] = r["key"][len(_KEY):]
        if since and str(d.get("at", "")) < since.isoformat():
            continue
        out.append(d)
    return sorted(out, key=lambda d: d.get("at", ""), reverse=True)


def run_auto_salvage(db_path: Optional[Path], config, log) -> Optional[dict]:
    """Salvage at most one candidate. Never raises. Returns the recorded outcome or None."""
    try:
        from hypogeum.db import get_worker_control  # noqa: PLC0415
        from hypogeum.episode_salvage import salvage_episode, salvage_preconditions  # noqa: PLC0415
        if get_worker_control("queue_paused", "0", db_path=db_path) == "1":
            return None
        for episode_id in find_candidates(db_path):
            pre = salvage_preconditions(episode_id, db_path=db_path)
            if not pre.get("ok"):
                continue                                   # not salvageable (and never will be): leave it
            _record(db_path, episode_id, status="started", ok=None, reason=None)
            log.info("auto_salvage_started", episode_id=episode_id, channel_id=pre.get("channel_id"),
                     clips=pre.get("clip_count"))
            # A long run must look like one to the watchdogs (check_worker allows a 4h heartbeat gap only
            # while worker_status == 'running').
            from hypogeum.db import set_worker_control  # noqa: PLC0415
            set_worker_control("worker_status", "running", db_path=db_path)
            try:
                res = salvage_episode(episode_id, config=config, db_path=db_path, log=log)
            finally:
                set_worker_control("worker_status", "idle", db_path=db_path)
            _record(db_path, episode_id, status="finished", ok=bool(res.get("ok")),
                    reason=str(res.get("reason") or "")[:300])
            log.info("auto_salvage_finished", episode_id=episode_id, ok=bool(res.get("ok")),
                     reason=str(res.get("reason") or "")[:200])
            return {"episode_id": episode_id, "ok": bool(res.get("ok")), "reason": res.get("reason")}
    except Exception as exc:  # noqa: BLE001
        try:
            log.warn("auto_salvage_error", error=str(exc)[:300])
        except Exception:  # noqa: BLE001
            pass
    return None
