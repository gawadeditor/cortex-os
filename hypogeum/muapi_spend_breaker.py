"""
hypogeum/muapi_spend_breaker.py — runaway-spend circuit breaker for the
autonomous worker (Factory Core, 2026-09-06).

THE PROBLEM THIS CLOSES
-----------------------
~50-85% of episodes have been failing, and every failed episode still
burns $5-18 of real MUAPI credits (≈15 scene generations + 15 upscales)
before it dies at assembly/QA. A failure then feeds the next attempt, and
when something breaks systemically (grant cap hit, MUAPI slow, MUAPI
account dry) the loop *accelerates* — failed scenes trigger clip-repair
regeneration, which is more paid calls, which drains the vendor account
faster, which causes more scene failures. MUAPI's own auto-refill tops up
at a threshold and cannot outrun a $30-50/hour cascade, so the account
goes negative and production grinds while still spending.

The submission-gate breaker infrastructure (max_spend_per_hour,
max_consecutive_failures) exists but every limit on the signed grant
bindings is None — unconfigured, no-op. This module is the worker-level
guard that actually fires, and needs no grant re-sign.

WHAT IT DOES
------------
Once per worker iteration (a couple of indexed SELECTs), BEFORE the worker
claims new work, trip when EITHER:

  * rolling-60-minute MUAPI spend (run_authorization_grant_consumption,
    provider='muapi', non-released) ≥ MUAPI_HOURLY_SPEND_LIMIT_USD, or
  * the last N episode runs are all terminally failed, N ≥
    MUAPI_CONSECUTIVE_FAILURE_LIMIT (a currently-running run at the head
    is ignored — the streak is only counted once it resolves).

On a trip: set worker_control.queue_paused=1 (publishing keeps running —
see run_worker.py) and worker_control.queue_paused_reason to the cause,
and log muapi_spend_breaker_tripped loudly.

DELIBERATELY DOES NOT AUTO-RECOVER. Once tripped the operator resumes by
calling clear_breaker() (or the dashboard's resume) after fixing the
underlying cause — a breaker that un-trips itself just re-enters the
cascade. clear_breaker() stamps breaker_cleared_at so the resume gets a
clean slate: only runs that START after it count toward a new
consecutive-failure streak. Idempotent: a no-op when production is
already paused. No-raise contract — any failure here is logged and
swallowed, never propagated into the worker loop.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

__all__ = ["check_muapi_spend_breaker", "clear_breaker"]

_TERMINAL_FAIL = ("failed", "error")
_RUNNING = ("running", "claimed", "queued")
_STREAK_LOOKBACK_HOURS = 3  # only runs this recent count toward the failure streak


def clear_breaker(*, db_path: Optional[object] = None,
                  now: Optional[datetime] = None) -> None:
    """Operator resume after a breaker trip: un-pause the queue, drop the
    stored reason, and stamp breaker_cleared_at=now so the consecutive-
    failure check starts from a clean slate (only runs that begin AFTER
    this count toward a new streak). Use this instead of setting
    queue_paused=0 by hand, or the breaker re-trips on the same failures."""
    from hypogeum.db import set_worker_control

    set_worker_control("queue_paused", "0", db_path=db_path)
    set_worker_control("queue_paused_reason", "", db_path=db_path)
    set_worker_control(
        "breaker_cleared_at", (now or datetime.now(timezone.utc)).isoformat(),
        db_path=db_path,
    )


def check_muapi_spend_breaker(
    *, config, db_path: Optional[object] = None, log=None,
    now: Optional[datetime] = None,
) -> dict:
    """
    Returns:
        {
          "checked": bool,        # False only if it could not read state
          "tripped_now": bool,    # THIS call flipped queue_paused -> 1
          "already_paused": bool,
          "reason": str | None,
          "hour_spend_usd": float,
          "consecutive_failures": int,
          "error": str | None,
        }
    """
    out = {
        "checked": False, "tripped_now": False, "already_paused": False,
        "reason": None, "hour_spend_usd": 0.0, "consecutive_failures": 0,
        "error": None,
    }
    now = now or datetime.now(timezone.utc)
    spend_limit = getattr(config, "MUAPI_HOURLY_SPEND_LIMIT_USD", None) or 0.0
    fail_limit = int(getattr(config, "MUAPI_CONSECUTIVE_FAILURE_LIMIT", 0) or 0)

    try:
        from hypogeum.db import get_connection, get_worker_control, set_worker_control

        conn = get_connection(db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 15000")

            if str(get_worker_control("queue_paused", "0", db_path=db_path)) == "1":
                out["checked"] = True
                out["already_paused"] = True
                return out

            # (1) rolling-hour MUAPI spend
            cutoff = (now - timedelta(hours=1)).isoformat()
            row = conn.execute(
                "SELECT COALESCE(SUM(CAST(COALESCE(charged_amount, reserved_amount) AS REAL)), 0) "
                "FROM run_authorization_grant_consumption "
                "WHERE provider = 'muapi' AND created_at >= ? AND state != 'released'",
                (cutoff,),
            ).fetchone()
            hour_spend = float(row[0] or 0.0)
            out["hour_spend_usd"] = round(hour_spend, 2)

            # (2) consecutive terminal-failed runs among RECENT runs only
            # (skip a live run at the head). Two guards keep a since-resolved
            # incident from immediately re-tripping the breaker after an
            # operator resumes:
            #   - a 3h recency window (a real cascade is by definition recent);
            #   - worker_control.breaker_cleared_at — set to "now" whenever an
            #     operator clears queue_paused after a breaker trip; only runs
            #     that STARTED after it count, so the resume gets a clean slate
            #     and the breaker only re-trips on genuinely NEW failures.
            streak_cutoff = (now - timedelta(hours=_STREAK_LOOKBACK_HOURS)).isoformat()
            cleared_at = str(get_worker_control("breaker_cleared_at", "", db_path=db_path) or "")
            if cleared_at > streak_cutoff:
                streak_cutoff = cleared_at
            streak = 0
            for r in conn.execute(
                "SELECT status FROM runs WHERE started_at >= ? ORDER BY started_at DESC LIMIT 40",
                (streak_cutoff,),
            ).fetchall():
                st = str(r[0] or "").lower()
                if st in _RUNNING:
                    continue
                if st in _TERMINAL_FAIL:
                    streak += 1
                    continue
                break
            out["consecutive_failures"] = streak

            reason = None
            if spend_limit and hour_spend >= spend_limit:
                reason = (
                    f"MUAPI spend in the last hour is ${hour_spend:.2f} "
                    f"(limit ${spend_limit:.2f}) — a runaway/cascade; pausing new "
                    f"production so the vendor account is not drained. Publishing continues."
                )
            elif fail_limit and streak >= fail_limit:
                reason = (
                    f"{streak} episode runs failed consecutively (limit {fail_limit}) — "
                    f"something is systematically broken; pausing new production before it "
                    f"burns more MUAPI credit on episodes that cannot finish. Publishing continues."
                )

            out["checked"] = True
            if reason is None:
                return out

            set_worker_control("queue_paused", "1", db_path=db_path)
            set_worker_control("queue_paused_reason", f"muapi_spend_breaker: {reason}", db_path=db_path)
            out["tripped_now"] = True
            out["reason"] = reason
        finally:
            conn.close()

        if log is not None:
            try:
                log.error(
                    "muapi_spend_breaker_tripped",
                    reason=out["reason"],
                    hour_spend_usd=out["hour_spend_usd"],
                    consecutive_failures=out["consecutive_failures"],
                )
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 — no-raise contract
        out["error"] = f"{type(exc).__name__}: {exc}"
        if log is not None:
            try:
                log.warning("muapi_spend_breaker_error", error=out["error"])
            except Exception:  # noqa: BLE001
                pass
    return out
