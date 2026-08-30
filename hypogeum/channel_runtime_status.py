"""
hypogeum/channel_runtime_status.py -- read-only per-channel operational snapshot
for the dashboard.

Phase 2 ("connect the dashboard to real runtime state"). The dashboard's
existing views (get_executive_overview, get_channel_summaries) surface
profile completeness and factory-wide run health, but nothing shows an
operator, per channel, the answer to "is this channel actually able to run
autonomously right now, and where does it stand this week?".

This module composes that answer from the authoritative tables only -- it
never writes, never opens a network connection, never calls a provider,
never touches os.getenv(). Every sub-section is individually fail-soft: a
failure in one block (e.g. no operations profile yet) degrades that block
to a typed "unknown / not configured" shape without blanking the rest.

Sources (all read-only):
  channel_profiles                -> identity (name, calog status)
  channel_operations_profiles     -> operator intent + cadence + schedule + weekly ceiling
  budget_reservations             -> weekly reserved+committed spend (via hypogeum.weekly_budget window)
  worker_control                  -> worker heartbeat / status
  episode_topics                  -> per-channel queue depth + episode_id linkage
  runs                            -> latest attributable pipeline run
  schedules                       -> next autonomous invocation slot
  factories                       -> factory clearance precondition

Public API:
  get_channel_runtime_status(channel_id, db_path=None, now_utc=None) -> dict
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

# A worker heartbeat older than this is treated as stale (the worker loop
# touches worker_control.worker_status every iteration; default interval is
# 60s, so ~5 missed iterations).
_HEARTBEAT_STALE_SECONDS = 300


def _resolve_db(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_decimal(value) -> Optional[Decimal]:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Section builders -- each returns a plain dict and never raises
# ---------------------------------------------------------------------------

def _identity(channel_id: str, db: Path) -> dict:
    out = {"channel_id": channel_id, "name": None, "calog_status": None, "exists": False}
    try:
        from hypogeum.channel_config import get_channel_profile

        cp = get_channel_profile(channel_id, db_path=db)
        if cp is not None:
            out["exists"] = True
            out["name"] = getattr(cp, "name", None)
            out["calog_status"] = getattr(cp, "status", None)
    except Exception as exc:  # noqa: BLE001 - fail-soft snapshot
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _operations(channel_id: str, db: Path) -> dict:
    out = {
        "configured": False,
        "operator_enabled": None,
        "discovery_enabled": None,
        "production_enabled": None,
        "publishing_enabled": None,
        "unattended_operation_enabled": None,
        "production_cadence": None,
        "publishing_schedule": None,
        "timezone": None,
        "weekly_video_budget_usd": None,
    }
    try:
        from hypogeum.channel_config import get_channel_operations_profile

        prof = get_channel_operations_profile(channel_id, db_path=db)
        if prof is None:
            return out
        out["configured"] = True
        for field in (
            "operator_enabled",
            "discovery_enabled",
            "production_enabled",
            "publishing_enabled",
            "unattended_operation_enabled",
            "production_cadence",
            "publishing_schedule",
            "timezone",
            "weekly_video_budget_usd",
        ):
            out[field] = getattr(prof, field, None)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _weekly_budget(channel_id: str, db: Path, now_utc: datetime) -> dict:
    """Reserved+committed spend for the channel's current ISO week against
    its configured weekly ceiling. Mirrors the enforcement path in
    hypogeum.weekly_budget without touching the ledger."""
    out = {
        "configured": False,
        "ceiling_usd": None,
        "reserved_committed_usd": None,
        "remaining_usd": None,
        "pct_used": None,
        "week_key": None,
        "timezone": None,
        "window_start_utc": None,
        "window_end_utc": None,
    }
    try:
        from hypogeum.db import get_connection, sum_reserved_and_committed
        from hypogeum.weekly_budget import (
            compute_weekly_window,
            resolve_channel_weekly_policy,
            week_scope_ref,
        )

        conn = get_connection(db)
        try:
            tz_name, ceiling = resolve_channel_weekly_policy(conn, channel_id)
            window = compute_weekly_window(now_utc, tz_name)
            ref = week_scope_ref(channel_id, window)
            spent = _to_decimal(
                sum_reserved_and_committed(conn, "channel_weekly", ref)
            ) or Decimal("0")
        finally:
            conn.close()

        remaining = ceiling - spent
        out.update(
            configured=True,
            ceiling_usd=float(ceiling),
            reserved_committed_usd=float(spent),
            remaining_usd=float(remaining),
            pct_used=(float(spent / ceiling * 100) if ceiling > 0 else None),
            week_key=window.week_key,
            timezone=tz_name,
            window_start_utc=window.start_utc.isoformat(),
            window_end_utc=window.end_utc.isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 - includes typed WeeklyBudgetError (not configured / malformed)
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _worker(db: Path, now_utc: datetime) -> dict:
    out = {
        "status": None,
        "active_queue_id": None,
        "last_heartbeat_at": None,
        "seconds_since_heartbeat": None,
        "stale": None,
        "ever_started": False,
    }
    try:
        from hypogeum.db import get_connection

        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT value, updated_at FROM worker_control WHERE key = 'worker_status'"
            ).fetchone()
            aq = conn.execute(
                "SELECT value FROM worker_control WHERE key = 'active_queue_id'"
            ).fetchone()
        finally:
            conn.close()

        if aq is not None and aq["value"]:
            out["active_queue_id"] = aq["value"]

        if row is None:
            return out

        out["ever_started"] = True
        out["status"] = row["value"]
        hb = _parse_iso(row["updated_at"])
        if hb is not None:
            out["last_heartbeat_at"] = hb.isoformat()
            delta = (now_utc - hb).total_seconds()
            out["seconds_since_heartbeat"] = int(delta)
            out["stale"] = delta > _HEARTBEAT_STALE_SECONDS
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _queue_and_run(channel_id: str, db: Path) -> dict:
    out = {
        "queued_topics": 0,
        "claimed_topics": 0,
        "latest_run": None,
    }
    try:
        from hypogeum.db import get_connection

        conn = get_connection(db)
        try:
            qr = conn.execute(
                "SELECT status, COUNT(*) AS n FROM episode_topics "
                "WHERE channel_id = ? GROUP BY status",
                (channel_id,),
            ).fetchall()
            counts = {r["status"]: r["n"] for r in qr}
            out["queued_topics"] = int(counts.get("queued", 0))
            out["claimed_topics"] = int(counts.get("claimed", 0))

            # Attribute a run to the channel via episode_topics.episode_id.
            run_row = conn.execute(
                """
                SELECT r.run_id, r.episode_id, r.status, r.started_at, r.completed_at,
                       r.error_message
                FROM runs r
                WHERE r.episode_id IN (
                    SELECT episode_id FROM episode_topics
                    WHERE channel_id = ? AND episode_id IS NOT NULL
                )
                ORDER BY r.started_at DESC
                LIMIT 1
                """,
                (channel_id,),
            ).fetchone()
            if run_row is not None:
                out["latest_run"] = {
                    "run_id": run_row["run_id"],
                    "episode_id": run_row["episode_id"],
                    "status": run_row["status"],
                    "started_at": run_row["started_at"],
                    "completed_at": run_row["completed_at"],
                    "error_message": run_row["error_message"],
                }
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _schedule(channel_id: str, db: Path) -> dict:
    out = {"has_active_schedule": False, "next_run_at": None, "cadence": None, "label": None}
    try:
        from hypogeum.db import get_connection

        conn = get_connection(db)
        try:
            row = conn.execute(
                """
                SELECT schedule_id, cadence, status, next_run_at, label
                FROM schedules
                WHERE scope = 'channel' AND channel_id = ? AND status = 'active'
                ORDER BY (next_run_at IS NULL), next_run_at ASC
                LIMIT 1
                """,
                (channel_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            out.update(
                has_active_schedule=True,
                schedule_id=row["schedule_id"],
                next_run_at=row["next_run_at"],
                cadence=row["cadence"],
                label=row["label"],
            )
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _factory_clearance(channel_id: str, db: Path) -> dict:
    """The worker requires factory_director.check_run_clearance to pass before
    it will dispatch a run. Surface its verdict verbatim."""
    out = {"cleared": None, "reason": None}
    try:
        from hypogeum.factory_director import check_run_clearance

        result = check_run_clearance(db_path=db, channel_id=channel_id)
        out["cleared"] = bool(result.cleared)
        out["reason"] = result.reason
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

_OK_LAST_RUN_STATES = ("completed", "complete", "ready_to_publish", "succeeded", "success")


def _classify_run_failure(error_message: Optional[str]) -> str:
    """Coarse failure bucket for a runs.error_message. Kept in sync with the
    taxonomy in memory:project-pipeline-failure-review so the dashboard and
    that review speak the same language."""
    if not error_message:
        return "ok"
    m = error_message.lower()
    if "qa agent" in m and "reject" in m:
        return "qa_rejected"
    if "finance" in m and "block" in m:
        return "finance_blocked"
    if "ceiling would be exceeded" in m or "budgetceiling" in m:
        return "weekly_budget"
    if "credits exhausted" in m or "http 402" in m:
        return "openrouter_credits"
    if "no compiled production plan" in m or "productionplanmissing" in m:
        return "no_production_plan"
    if "muapi_gate_enforced_no_active_grant" in m or "no_active_authorization_grant" in m \
            or "operating grant" in m:
        return "no_authorization_grant"
    if "scene_content must include" in m or "additional_coverage_required" in m \
            or "coverage" in m:
        return "coverage"
    if "truncated" in m and "max_token" in m:
        return "llm_truncated"
    if "orphaned" in m or "worker not active" in m:
        return "run_orphaned"
    if "research_director" in m and ("non-dict" in m or "could not parse" in m):
        return "rd_bad_json"
    if "anthropic_api_key is not set" in m:
        return "no_anthropic_key"
    if "bga pass" in m:
        return "bga_rejected"
    if "thumbnail" in m and ("block" in m or "pre-generation" in m):
        return "thumbnail_pregen_block"
    if "timed out" in m or "timeout" in m or "connection reset" in m:
        return "network_timeout"
    if "muapi budget exhausted" in m:
        return "muapi_episode_budget"
    if "stopped by operator" in m or "terminated manually" in m or "cancelled" in m:
        return "operator_stopped"
    if "rejected by executive producer" in m:
        return "ep_rejected"
    return "other"


def _run_health(channel_id: str, db: Path, now_utc: datetime) -> dict:
    """Recent pipeline-run outcomes for the channel, straight off runs.channel_id.
    Distinguishes 'configured to run' from 'actually making progress'."""
    out = {
        "latest": None,
        "consecutive_failures": 0,
        "last_success_at": None,
        "recent_buckets": [],
    }
    try:
        from hypogeum.db import get_connection

        conn = get_connection(db)
        try:
            rows = conn.execute(
                """
                SELECT run_id, episode_id, status, started_at, completed_at, error_message
                FROM runs
                WHERE channel_id = ?
                ORDER BY started_at DESC
                LIMIT 8
                """,
                (channel_id,),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return out

        def _is_fail(r) -> bool:
            return str(r["status"]).lower() in ("failed", "error")

        top = rows[0]
        out["latest"] = {
            "run_id": top["run_id"],
            "episode_id": top["episode_id"],
            "status": top["status"],
            "completed_at": top["completed_at"],
            "error_message": top["error_message"],
            "bucket": _classify_run_failure(top["error_message"]) if _is_fail(top)
            else ("ok" if str(top["status"]).lower() in _OK_LAST_RUN_STATES else "in_progress"),
        }
        streak = 0
        for r in rows:
            if _is_fail(r):
                streak += 1
                out["recent_buckets"].append(_classify_run_failure(r["error_message"]))
            else:
                break
        out["consecutive_failures"] = streak
        for r in rows:
            if str(r["status"]).lower() in _OK_LAST_RUN_STATES:
                out["last_success_at"] = r["completed_at"] or r["started_at"]
                break
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _operating_grant(channel_id: str, db: Path, now_utc: datetime) -> dict:
    """ADR-016 Channel Operating Grant status for the channel: is there an
    active, in-window, signature-verified grant, and does its bound weekly
    ceiling still match the operator-configured budget."""
    out = {
        "active": False,
        "grant_id": None,
        "expires_at": None,
        "days_left": None,
        "weekly_exposure_ceiling_usd": None,
        "ceiling_matches_config": None,
    }
    try:
        import hypogeum.authorization_authority as _authority

        grant = _authority.resolve_active_operating_grant(
            channel_id, at=now_utc.isoformat(), db_path=db,
        )
        if grant is None:
            return out
        out["active"] = True
        out["grant_id"] = grant.grant_id
        out["expires_at"] = grant.expires_at
        exp = _parse_iso(grant.expires_at)
        if exp is not None:
            out["days_left"] = round((exp - now_utc).total_seconds() / 86400, 1)
        gc = _to_decimal(getattr(grant, "weekly_exposure_ceiling_usd", None))
        if gc is not None:
            out["weekly_exposure_ceiling_usd"] = float(gc)
        try:
            from hypogeum.channel_budget_policy import resolve_configured_weekly_limit
            from hypogeum.db import get_connection

            conn = get_connection(db)
            try:
                _tz, configured, _ref = resolve_configured_weekly_limit(channel_id, conn=conn)
            finally:
                conn.close()
            if gc is not None:
                out["ceiling_matches_config"] = (gc == _to_decimal(configured))
        except Exception:  # noqa: BLE001 -- config unreadable; leave match as None
            pass
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _derive_blockers(identity: dict, ops: dict, budget: dict,
                     clearance: dict, schedule: dict,
                     health: Optional[dict] = None,
                     grant: Optional[dict] = None) -> list:
    """Human-readable list of why the channel cannot run autonomously right
    now. Empty list == nothing structural is stopping an autonomous run."""
    blockers: list = []

    if not identity.get("exists"):
        blockers.append("channel profile does not exist")
    if identity.get("calog_status") and identity["calog_status"] != "active":
        blockers.append(f"calog channel state is {identity['calog_status']!r}, not 'active'")

    if not ops.get("configured"):
        blockers.append("no channel_operations_profile configured")
    else:
        if not ops.get("operator_enabled"):
            blockers.append("operator_enabled is False (operator has not opted this channel in)")
        if not ops.get("unattended_operation_enabled"):
            blockers.append("unattended_operation_enabled is False (worker will not dispatch)")
        if not ops.get("production_enabled"):
            blockers.append("production_enabled is False")
        if not (ops.get("publishing_schedule") or {}):
            blockers.append("publishing_schedule is empty (backward planner cannot place a deadline)")

    if budget.get("configured"):
        if budget.get("remaining_usd") is not None and budget["remaining_usd"] <= 0:
            blockers.append("weekly video budget for the current week is exhausted")
    else:
        blockers.append(
            "weekly_video_budget_usd not usable: " + (budget.get("error") or "not configured")
        )

    if clearance.get("cleared") is False:
        blockers.append(f"factory clearance denied: {clearance.get('reason') or 'unknown reason'}")

    if not schedule.get("has_active_schedule"):
        blockers.append("no active channel schedule (autonomous loop has no trigger)")

    # --- actual-progress signals (not just static config) ---------------
    health = health or {}
    grant = grant or {}
    latest = health.get("latest") or {}
    bucket = latest.get("bucket")
    streak = int(health.get("consecutive_failures") or 0)
    if streak >= 3:
        blockers.append(f"{streak} consecutive failed runs (last: {bucket}) — needs attention")
    elif bucket and bucket not in ("ok", "in_progress", "operator_stopped"):
        blockers.append(f"last run failed: {bucket}")

    if health.get("_worker_stale_active_schedule"):
        blockers.append("worker heartbeat is stale while a schedule is active — autonomous loop is not dispatching")

    if ops.get("configured") and ops.get("operator_enabled"):
        if grant.get("active") is False:
            blockers.append(
                "no active Channel Operating Grant — autonomous paid generation "
                "will fail closed (sign one: tools/configure_channel_operating_grant.py)"
            )
        elif grant.get("ceiling_matches_config") is False:
            blockers.append(
                "Channel Operating Grant weekly ceiling no longer matches the "
                "configured weekly budget — re-sign the operating grant"
            )

    return blockers


def get_channel_runtime_status(
    channel_id: str,
    db_path: Optional[Path] = None,
    now_utc: Optional[datetime] = None,
) -> dict:
    """
    Compose a read-only operational snapshot for one channel.

    Never raises: every section degrades to a typed "unknown" shape on
    failure and records its error string. `autonomous_ready` is True only
    when `blockers` is empty.
    """
    db = _resolve_db(db_path)
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    identity = _identity(channel_id, db)
    ops = _operations(channel_id, db)
    budget = _weekly_budget(channel_id, db, now)
    worker = _worker(db, now)
    queue_run = _queue_and_run(channel_id, db)
    schedule = _schedule(channel_id, db)
    clearance = _factory_clearance(channel_id, db)
    health = _run_health(channel_id, db, now)
    grant = _operating_grant(channel_id, db, now)

    health["_worker_stale_active_schedule"] = bool(
        worker.get("stale") and schedule.get("has_active_schedule")
    )
    blockers = _derive_blockers(identity, ops, budget, clearance, schedule, health, grant)
    health.pop("_worker_stale_active_schedule", None)

    return {
        "channel_id": channel_id,
        "generated_at": now.isoformat(),
        "db_path": str(db),
        "identity": identity,
        "operations": ops,
        "weekly_budget": budget,
        "worker": worker,
        "queue": {
            "queued_topics": queue_run.get("queued_topics", 0),
            "claimed_topics": queue_run.get("claimed_topics", 0),
        },
        "latest_run": queue_run.get("latest_run"),
        "health": health,
        "operating_grant": grant,
        "schedule": schedule,
        "factory_clearance": clearance,
        "blockers": blockers,
        "autonomous_ready": not blockers,
    }
