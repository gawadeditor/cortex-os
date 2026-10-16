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

__all__ = ["check_muapi_spend_breaker", "clear_breaker", "trip_provider_billing",
           "probe_muapi_credit", "PROVIDER_BILLING_PREFIX"]

PROVIDER_BILLING_PREFIX = "provider_billing:"

_TERMINAL_FAIL = ("failed", "error")
_RUNNING = ("running", "claimed", "queued")
_STREAK_LOOKBACK_HOURS = 3  # only runs this recent count toward the failure streak


def clear_breaker(*, db_path: Optional[object] = None,
                  now: Optional[datetime] = None,
                  workspace_id: Optional[str] = None) -> None:
    """Operator resume after a breaker trip: un-pause the queue, drop the
    stored reason, and stamp breaker_cleared_at=now so the consecutive-
    failure check starts from a clean slate (only runs that begin AFTER
    this count toward a new streak). Use this instead of setting
    queue_paused=0 by hand, or the breaker re-trips on the same failures.

    workspace_id: resume just that workspace (multi-tenant). None = the
    operator's resume: the global switch AND the platform owner's own
    workspace pause — never another (client) workspace's."""
    from hypogeum.db import set_worker_control

    if workspace_id is not None:
        from hypogeum.workspace_controls import resume
        resume(workspace_id, db_path=db_path, now=now)
        return
    try:
        from hypogeum.workspace_controls import resume
        from hypogeum.workspaces import platform_workspace_id
        _pid = platform_workspace_id(db_path)
        if _pid:
            resume(_pid, db_path=db_path, now=now)
    except Exception:  # noqa: BLE001 — install without workspaces
        pass
    set_worker_control("queue_paused", "0", db_path=db_path)
    set_worker_control("queue_paused_reason", "", db_path=db_path)
    set_worker_control(
        "breaker_cleared_at", (now or datetime.now(timezone.utc)).isoformat(),
        db_path=db_path,
    )


def trip_provider_billing(
    provider: str, detail: str, *, db_path: Optional[object] = None, log=None,
) -> bool:
    """A provider account has no funds (a REAL HTTP 402 that survived every
    bounded retry). Pause new production at once, with the real cause as the
    pause reason, so the operator is told "MUAPI balance is out" instead of
    the downstream symptom (2026-10-14..15: three episodes burned ~100 min of
    EP/RD/NA/SE/audio spend and were reported only as "QA rejected").

    Reuses the spend breaker's pause (worker stops claiming, publishing keeps
    running) and its resume (clear_breaker / dashboard "resume"). Never
    overwrites an existing pause reason. No-raise: a failure to record is
    logged, never propagated into the generation call that hit the 402.
    Returns True when THIS call paused production."""
    try:
        from hypogeum.db import get_worker_control, set_worker_control
        if str(get_worker_control("queue_paused", "0", db_path=db_path)) == "1":
            return False
        set_worker_control("queue_paused", "1", db_path=db_path)
        set_worker_control("muapi_probe_has_credit", "0", db_path=db_path)  # forget an older recovery
        set_worker_control("muapi_probe_at", "", db_path=db_path)
        set_worker_control(
            "queue_paused_reason",
            f"{PROVIDER_BILLING_PREFIX} رصيد {provider} خلص ({detail})",
            db_path=db_path,
        )
        if log is not None:
            log.error("provider_billing_production_paused", provider=provider, detail=detail)
        return True
    except Exception as exc:  # noqa: BLE001 — no-raise contract
        if log is not None:
            try:
                log.warn("provider_billing_trip_failed", provider=provider, error=f"{type(exc).__name__}: {exc}"[:200])
            except Exception:  # noqa: BLE001
                pass
        return False


def probe_muapi_credit(config=None, *, post=None) -> Optional[bool]:
    """Does the MUAPI account have credit again? MUAPI has no balance endpoint
    (GET /user/balance is 404), but it checks credit BEFORE content rules and
    AFTER schema rules: a schema-valid request with an empty prompt answers 402
    INSUFFICIENT_CREDITS on an empty account (verified 2026-10-16), while any
    schema-invalid body answers 422 regardless of balance (useless as a probe).
    Returns False on 402, True on any other definite answer, None when unknown
    (401, 5xx, network). Used ONLY while production is paused for an empty
    balance. Caveat: on a funded account an empty prompt could, at worst, start
    one 8s clip (~$0.39) -- accepted once, because the probe stops running as
    soon as the operator resumes. No-raise."""
    try:
        import requests
        from hypogeum.config import Config
        from hypogeum.muapi import _get_headers
        cfg = config or Config()
        do_post = post or requests.post
        r = do_post(f"{cfg.MUAPI_BASE_URL}/{cfg.MUAPI_T2_MODEL}", headers=_get_headers(cfg),
                    json={"prompt": ""}, timeout=20)
        code = int(r.status_code)
        if code == 402:
            return False
        if code in (401, 403) or code >= 500:
            return None
        return True
    except Exception:  # noqa: BLE001
        return None


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
        "error": None, "tripped_workspaces": [],
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

            scopes = _breaker_scopes(db_path)   # [(workspace_id|None, channel predicate)]
            spend_limit_ = spend_limit
            for ws_id, in_scope in scopes:
                if ws_id is not None:
                    from hypogeum.workspace_controls import is_paused
                    if is_paused(ws_id, db_path=db_path):
                        continue
                hour_spend, streak = _scope_metrics(conn, now, in_scope, ws_id, db_path)
                out["hour_spend_usd"] = max(out["hour_spend_usd"], round(hour_spend, 2))
                out["consecutive_failures"] = max(out["consecutive_failures"], streak)

                reason = None
                if spend_limit_ and hour_spend >= spend_limit_:
                    reason = (
                        f"MUAPI spend in the last hour is ${hour_spend:.2f} "
                        f"(limit ${spend_limit_:.2f}) — a runaway/cascade; pausing new "
                        f"production so the vendor account is not drained. Publishing continues."
                    )
                elif fail_limit and streak >= fail_limit:
                    reason = (
                        f"{streak} episode runs failed consecutively (limit {fail_limit}) — "
                        f"something is systematically broken; pausing new production before it "
                        f"burns more MUAPI credit on episodes that cannot finish. Publishing continues."
                    )
                if reason is None:
                    continue
                if ws_id is None:
                    # single-tenant install: the global switch, exactly as before
                    set_worker_control("queue_paused", "1", db_path=db_path)
                    set_worker_control("queue_paused_reason", f"muapi_spend_breaker: {reason}", db_path=db_path)
                else:
                    from hypogeum.workspace_controls import pause
                    pause(ws_id, f"muapi_spend_breaker: {reason}", db_path=db_path)
                    out["tripped_workspaces"].append(ws_id)
                out["tripped_now"] = True
                out["reason"] = out["reason"] or reason
            out["checked"] = True
            if not out["tripped_now"]:
                return out
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


def _breaker_scopes(db_path) -> list:
    """[(workspace_id, in_scope(channel_id) -> bool)]. Single-tenant install
    (no client workspaces): one global scope (None) over every run, exactly
    the pre-workspace behaviour. Multi-tenant: one scope per workspace; the
    platform owner's covers every channel not owned by a client workspace."""
    try:
        from hypogeum.workspace_controls import _client_channels, has_client_workspaces
        from hypogeum.workspaces import list_workspaces, platform_workspace_id
        if not has_client_workspaces(db_path):
            return [(None, lambda ch: True)]
        clients = _client_channels(db_path)
        pid = platform_workspace_id(db_path)
        scopes = []
        for w in list_workspaces(db_path):
            wid = w["workspace_id"]
            if wid == pid:
                scopes.append((wid, lambda ch, _c=clients: ch not in _c))
            else:
                scopes.append((wid, lambda ch, _c=clients, _w=wid: _c.get(ch) == _w))
        return scopes
    except Exception:  # noqa: BLE001 — install without workspaces
        return [(None, lambda ch: True)]


def _scope_metrics(conn, now, in_scope, ws_id, db_path) -> tuple:
    """(rolling-hour MUAPI spend, consecutive terminal-failed runs) for the
    channels in scope. Two guards keep a since-resolved incident from
    re-tripping right after a resume: a 3h recency window, and the
    breaker_cleared_at stamp (global, plus ws_breaker_cleared_at:<id>)."""
    from hypogeum.db import get_worker_control

    cutoff = (now - timedelta(hours=1)).isoformat()
    hour_spend = 0.0
    try:
        spend_rows = conn.execute(
            "SELECT g.channel_id AS ch, CAST(COALESCE(c.charged_amount, c.reserved_amount) AS REAL) AS amt "
            "FROM run_authorization_grant_consumption c "
            "LEFT JOIN run_authorization_grants g ON g.grant_id = c.grant_id "
            "WHERE c.provider = 'muapi' AND c.created_at >= ? AND c.state != 'released'",
            (cutoff,),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — grant tables are created lazily
        if "no such table" not in str(exc):
            raise
        spend_rows = []   # no grant ledger yet = no gated spend; the failure streak still counts
    for r in spend_rows:
        if in_scope(r[0]):
            hour_spend += float(r[1] or 0.0)

    streak_cutoff = (now - timedelta(hours=_STREAK_LOOKBACK_HOURS)).isoformat()
    for key in ("breaker_cleared_at",) + ((f"ws_breaker_cleared_at:{ws_id}",) if ws_id else ()):
        cleared_at = str(get_worker_control(key, "", db_path=db_path) or "")
        if cleared_at > streak_cutoff:
            streak_cutoff = cleared_at
    streak = 0
    for r in conn.execute(
        "SELECT status, channel_id FROM runs WHERE started_at >= ? ORDER BY started_at DESC LIMIT 200",
        (streak_cutoff,),
    ).fetchall():
        if not in_scope(r[1]):
            continue
        st = str(r[0] or "").lower()
        if st in _RUNNING:
            continue
        if st in _TERMINAL_FAIL:
            streak += 1
            continue
        break
    return hour_spend, streak
