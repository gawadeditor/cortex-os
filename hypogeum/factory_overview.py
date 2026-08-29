"""
hypogeum/factory_overview.py — factory-wide, multi-channel operational
rollup for the dashboard (Phase 5).

Phase 2 gave a per-channel operational snapshot
(hypogeum.channel_runtime_status). This module is the tier above it: one
read-only view of the WHOLE factory —

  * the shared factory-clearance verdict (OIS / factory-active /
    credentials / monthly budget)
  * the autonomous worker's heartbeat
  * every channel's runtime status (reusing get_channel_runtime_status)
  * aggregate weekly budget: reserved+committed vs configured ceiling,
    summed across channels
  * the factory's self-learning state (insights, pending learning actions,
    top recommendation)

Read-only. Never writes, never opens a network connection, never calls a
provider. Every section is individually fail-soft.

Public API:
  get_factory_overview(db_path=None, now_utc=None) -> dict
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

_HEARTBEAT_STALE_SECONDS = 300


def _resolve_db(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _factory_clearance(db: Path) -> dict:
    out = {"cleared": None, "reason": None}
    try:
        from hypogeum.factory_director import check_run_clearance

        result = check_run_clearance(db_path=db)
        out["cleared"] = bool(result.cleared)
        out["reason"] = result.reason
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _worker(db: Path, now: datetime) -> dict:
    out = {"status": None, "last_heartbeat_at": None,
           "seconds_since_heartbeat": None, "stale": None, "ever_started": False}
    try:
        from hypogeum.db import get_connection

        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT value, updated_at FROM worker_control WHERE key = 'worker_status'"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return out
        out["ever_started"] = True
        out["status"] = row["value"]
        try:
            hb = datetime.fromisoformat(str(row["updated_at"]))
            if hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
            out["last_heartbeat_at"] = hb.isoformat()
            delta = (now - hb).total_seconds()
            out["seconds_since_heartbeat"] = int(delta)
            out["stale"] = delta > _HEARTBEAT_STALE_SECONDS
        except (TypeError, ValueError):
            pass
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _channels(db: Path, now: datetime) -> list:
    try:
        from hypogeum.channel_config import list_channel_profiles
        from hypogeum.channel_runtime_status import get_channel_runtime_status
    except Exception:
        return []

    rows: list = []
    try:
        profiles = list_channel_profiles(db_path=db)
    except Exception:
        profiles = []

    for cp in profiles:
        cid = getattr(cp, "channel_id", None)
        if not cid:
            continue
        try:
            rt = get_channel_runtime_status(cid, db_path=db, now_utc=now)
        except Exception as exc:  # noqa: BLE001 - get_channel_runtime_status is fail-soft, be defensive
            rows.append({"channel_id": cid, "error": f"{type(exc).__name__}: {exc}"})
            continue
        wb = rt.get("weekly_budget", {})
        rows.append({
            "channel_id": cid,
            "name": rt.get("identity", {}).get("name"),
            "calog_status": rt.get("identity", {}).get("calog_status"),
            "autonomous_ready": rt.get("autonomous_ready"),
            "blockers": rt.get("blockers", []),
            "operator_enabled": rt.get("operations", {}).get("operator_enabled"),
            "unattended_operation_enabled": rt.get("operations", {}).get("unattended_operation_enabled"),
            "weekly_budget": {
                "configured": wb.get("configured"),
                "ceiling_usd": wb.get("ceiling_usd"),
                "reserved_committed_usd": wb.get("reserved_committed_usd"),
                "remaining_usd": wb.get("remaining_usd"),
                "pct_used": wb.get("pct_used"),
            },
            "queue": rt.get("queue", {}),
            "latest_run": (rt.get("latest_run") or {}).get("status") if rt.get("latest_run") else None,
            "next_scheduled_slot": rt.get("schedule", {}).get("next_run_at"),
        })
    return rows


def _aggregate(channels: list) -> dict:
    total = len(channels)
    ready = sum(1 for c in channels if c.get("autonomous_ready") is True)
    reserved = 0.0
    ceiling = 0.0
    for c in channels:
        wb = c.get("weekly_budget", {})
        if wb.get("configured"):
            reserved += float(wb.get("reserved_committed_usd") or 0.0)
            ceiling += float(wb.get("ceiling_usd") or 0.0)
    return {
        "channels_total": total,
        "channels_autonomous_ready": ready,
        "channels_blocked": total - ready,
        "weekly_reserved_committed_usd": round(reserved, 2),
        "weekly_ceiling_usd": round(ceiling, 2),
        "weekly_pct_used": (round(reserved / ceiling * 100, 1) if ceiling > 0 else None),
    }


def _learning(db: Path) -> dict:
    try:
        from hypogeum.learning_engine import get_factory_learning_state

        state = get_factory_learning_state(db_path=db)
        return {
            "total_analytics_records": state.get("total_analytics_records", 0),
            "total_insights": state.get("total_insights", 0),
            "pending_actions": state.get("pending_actions", 0),
            "applied_actions": state.get("applied_actions", 0),
            "top_recommendation": state.get("top_recommendation"),
            "recent_actions": state.get("recent_actions", []),
            "dimensions_with_signal": state.get("dimensions_with_signal", []),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def get_factory_overview(
    db_path: Optional[Path] = None,
    now_utc: Optional[datetime] = None,
) -> dict:
    """
    Compose a read-only, factory-wide operational rollup. Never raises.
    """
    db = _resolve_db(db_path)
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    channels = _channels(db, now)
    return {
        "generated_at": now.isoformat(),
        "db_path": str(db),
        "factory_clearance": _factory_clearance(db),
        "worker": _worker(db, now),
        "aggregate": _aggregate(channels),
        "channels": channels,
        "learning": _learning(db),
    }
