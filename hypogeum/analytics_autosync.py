"""
hypogeum/analytics_autosync.py — keep real analytics fresh from the dashboard.

The hourly analytics sync used to live only inside run_worker.py, so it
stopped whenever the worker wasn't running — including after every Mac
reboot, because the dashboard is a LaunchAgent that comes back on its own
and the worker is not (2026-09-26: no sync for ~2 days after a shutdown).

The dashboard starts one daemon thread that wakes every `check_every_s`
and runs the full `run_platform_sync()` (Zernio per-post + aggregates +
native retention + learning refresh) once the newest real analytics row is
older than `max_age_s`. Keying on the data's own age — not a timer — means
it catches up immediately after a reboot or wake from sleep, and skips when
the worker (if running) already synced recently, so the two never double up.

Public API:
    analytics_age_s(db_path=None) -> Optional[float]
    run_autosync_if_due(db_path=None, max_age_s=3600) -> dict
    start_analytics_autosync(db_path=None, *, max_age_s=3600, check_every_s=300) -> threading.Thread
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="analytics_autosync")

_lock = threading.Lock()


def analytics_age_s(db_path: Optional[Path] = None) -> Optional[float]:
    """Seconds since the newest source='api' platform_analytics import, or
    None when there is none yet."""
    from hypogeum.db import get_connection
    conn = get_connection(Path(db_path) if db_path else None)
    try:
        row = conn.execute(
            "SELECT MAX(imported_at) AS last FROM platform_analytics WHERE source='api'"
        ).fetchone()
    finally:
        conn.close()
    last = row["last"] if row else None
    if not last:
        return None
    ts = datetime.fromisoformat(last)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def run_autosync_if_due(db_path: Optional[Path] = None, max_age_s: int = 3600) -> dict:
    """Run one full platform sync when the analytics are stale. Never raises.

    Returns {ran, reason, age_s?, ok?, total_records?}.
    """
    if not _lock.acquire(blocking=False):
        return {"ran": False, "reason": "already_running"}
    try:
        age = analytics_age_s(db_path)
        if age is not None and age < max_age_s:
            return {"ran": False, "reason": "fresh", "age_s": round(age)}

        from hypogeum.platform_sync import run_platform_sync
        result = run_platform_sync(db_path=db_path, window_days=30,
                                   triggered_by="dashboard_autosync")
        try:
            from hypogeum.publishing.zernio_accounts import sync_zernio_accounts
            sync_zernio_accounts(db_path)
        except Exception as exc:  # noqa: BLE001
            log.warn("analytics_autosync_accounts_failed", error=str(exc)[:200])
        log.info("analytics_autosync_ran", ok=result.get("ok"),
                 total_records=result.get("total_records"),
                 stale_age_s=None if age is None else round(age))
        return {"ran": True, "reason": "stale", "ok": result.get("ok"),
                "total_records": result.get("total_records")}
    except Exception as exc:  # noqa: BLE001
        log.error("analytics_autosync_failed", error=str(exc)[:300])
        return {"ran": False, "reason": "error", "error": str(exc)[:300]}
    finally:
        _lock.release()


def start_analytics_autosync(db_path: Optional[Path] = None, *,
                             max_age_s: int = 3600,
                             check_every_s: int = 300) -> threading.Thread:
    """Start the daemon thread. The first check runs immediately."""
    stop = threading.Event()

    def _loop():
        while not stop.is_set():
            run_autosync_if_due(db_path, max_age_s=max_age_s)
            stop.wait(check_every_s)

    t = threading.Thread(target=_loop, name="analytics-autosync", daemon=True)
    t.stop_event = stop  # type: ignore[attr-defined]
    t.start()
    log.info("analytics_autosync_started", max_age_s=max_age_s, check_every_s=check_every_s)
    return t
