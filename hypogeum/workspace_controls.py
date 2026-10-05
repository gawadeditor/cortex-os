"""
hypogeum/workspace_controls.py — production pause and monthly budget per
Workspace. Multi-tenant plan, Phase 3 (docs/plans/MULTI_TENANT_PLAN.md).

Before this, a single worker_control.queue_paused stopped every channel and
one monthly ceiling was compared against every cost in the factory — so a
client's failure streak would have paused the operator's channels (and vice
versa), and one workspace's spend would have eaten another's budget.

Now:
  * worker_control.queue_paused stays the operator's GLOBAL emergency switch
    (manual; stops everything) — unchanged.
  * Each workspace has its own pause: worker_control keys
    ws_paused:<id> / ws_paused_reason:<id> / ws_breaker_cleared_at:<id>.
    The spend breaker (hypogeum.muapi_spend_breaker) trips per workspace.
  * Each workspace has its own monthly spend and ceiling. A client's spend is
    the costs of runs on its channels; the platform owner's spend is every
    cost NOT attributed to a client workspace — exactly today's number while
    there are no clients. Ceiling: the workspace's factory_policies
    monthly_ceiling_usd; the platform owner additionally falls back to
    FINANCE_BUDGET_CEILING_USD (today's behaviour). 0 = no ceiling.
  * blocked_channels() is consulted inside the queue claim itself
    (db.claim_next_queue_item) so no claim path can start a run for a paused
    or over-budget workspace.

    pause(workspace_id, reason) / resume(workspace_id)
    paused_workspaces() -> {workspace_id: reason}
    month_spend(workspace_id) -> float
    month_ceiling(workspace_id) -> float
    budget_status(workspace_id) -> dict
    blocked_channels(db_path=None) -> dict[channel_id, reason]
    has_client_workspaces(db_path=None) -> bool
    production_start(workspace_id) -> "HH:MM"  /  set_production_start(...)

Production start time (2026-10-05): each workspace picks the local hour its
production day begins (worker_control ws_production_start:<id>, "HH:MM" in
each channel's own timezone; default 00:00). The scheduler's daily ceiling
counts from that moment, so a channel's next episode can't start before it
-- the operator's laptop is usually closed at midnight, so theirs is 02:00.
If the machine is still closed then, the worker starts it at its first tick
once the machine is awake.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="workspace_controls")


def _p(db_path):
    if db_path:
        return Path(db_path)
    import hypogeum.db as _db
    return Path(_db._DEFAULT_DB_PATH)


def _key(name: str, workspace_id: str) -> str:
    return f"{name}:{workspace_id}"


def pause(workspace_id: str, reason: str, db_path=None) -> None:
    from hypogeum.db import set_worker_control
    set_worker_control(_key("ws_paused", workspace_id), "1", db_path=_p(db_path))
    set_worker_control(_key("ws_paused_reason", workspace_id), reason, db_path=_p(db_path))
    log.error("workspace_paused", workspace_id=workspace_id, reason=reason)


def resume(workspace_id: str, db_path=None, now: Optional[datetime] = None) -> None:
    """Un-pause, and stamp ws_breaker_cleared_at so the failure streak
    restarts from a clean slate (only runs that start after this count)."""
    from hypogeum.db import set_worker_control
    set_worker_control(_key("ws_paused", workspace_id), "0", db_path=_p(db_path))
    set_worker_control(_key("ws_paused_reason", workspace_id), "", db_path=_p(db_path))
    set_worker_control(_key("ws_breaker_cleared_at", workspace_id),
                       (now or datetime.now(timezone.utc)).isoformat(), db_path=_p(db_path))
    log.info("workspace_resumed", workspace_id=workspace_id)


DEFAULT_PRODUCTION_START = "00:00"


def _parse_hhmm(value: str) -> tuple:
    h, m = (int(x) for x in str(value).strip().split(":", 1))
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"not a time of day: {value!r}")
    return h, m


def production_start(workspace_id: Optional[str], db_path=None) -> str:
    """The workspace's production start time, "HH:MM" local (default 00:00)."""
    if not workspace_id:
        return DEFAULT_PRODUCTION_START
    from hypogeum.db import get_worker_control
    raw = get_worker_control(_key("ws_production_start", workspace_id), "", db_path=_p(db_path))
    try:
        h, m = _parse_hhmm(raw)
        return f"{h:02d}:{m:02d}"
    except Exception:  # noqa: BLE001
        return DEFAULT_PRODUCTION_START


def set_production_start(workspace_id: str, value: str, db_path=None) -> str:
    from hypogeum.db import set_worker_control
    h, m = _parse_hhmm(value)
    norm = f"{h:02d}:{m:02d}"
    set_worker_control(_key("ws_production_start", workspace_id), norm, db_path=_p(db_path))
    log.info("workspace_production_start_set", workspace_id=workspace_id, start=norm)
    return norm


def production_start_for_channel(channel_id: str, db_path=None) -> tuple:
    """(hour, minute) the channel's production day begins, from its workspace."""
    try:
        from hypogeum.workspaces import workspace_of
        return _parse_hhmm(production_start(workspace_of(channel_id, _p(db_path)), db_path))
    except Exception:  # noqa: BLE001
        return (0, 0)


def is_paused(workspace_id: str, db_path=None) -> bool:
    from hypogeum.db import get_worker_control
    return get_worker_control(_key("ws_paused", workspace_id), "0", db_path=_p(db_path)) == "1"


def paused_workspaces(db_path=None) -> dict:
    from hypogeum.db import get_connection
    conn = get_connection(_p(db_path))
    try:
        rows = conn.execute("SELECT key, value FROM worker_control WHERE key LIKE 'ws_paused%'").fetchall()
    finally:
        conn.close()
    vals = {r["key"]: r["value"] for r in rows}
    return {k.split(":", 1)[1]: vals.get(_key("ws_paused_reason", k.split(":", 1)[1]), "")
            for k, v in vals.items() if k.startswith("ws_paused:") and v == "1"}


def _client_channels(db_path) -> dict:
    """{channel_id: workspace_id} for every channel owned by a client workspace."""
    from hypogeum.workspaces import list_workspaces, resources_of
    out = {}
    for w in list_workspaces(_p(db_path)):
        if w["kind"] == "client":
            for ch in resources_of(w["workspace_id"], "channel", db_path=_p(db_path)):
                out[ch] = w["workspace_id"]
    return out


def _month_costs_by_channel(conn) -> dict:
    """{channel_id or None: this calendar month's cost}, attributing a cost
    to its run's channel (costs has run_id, runs has channel_id)."""
    out: dict = {}
    for r in conn.execute(
        "SELECT r.channel_id AS ch, COALESCE(SUM(c.cost_usd), 0) AS total FROM costs c "
        "LEFT JOIN runs r ON r.run_id = c.run_id "
        "WHERE strftime('%Y-%m', c.called_at) = strftime('%Y-%m', 'now') GROUP BY r.channel_id"
    ):
        out[r["ch"]] = float(r["total"] or 0)
    return out


def month_spend(workspace_id: str, db_path=None) -> float:
    from hypogeum.db import get_connection
    from hypogeum.workspaces import platform_workspace_id
    clients = _client_channels(db_path)
    conn = get_connection(_p(db_path))
    try:
        by_ch = _month_costs_by_channel(conn)
    finally:
        conn.close()
    if workspace_id == platform_workspace_id(_p(db_path)):
        return round(sum(v for ch, v in by_ch.items() if ch not in clients), 6)
    return round(sum(v for ch, v in by_ch.items() if clients.get(ch) == workspace_id), 6)


def month_ceiling(workspace_id: str, db_path=None) -> float:
    from hypogeum.factory_entity import get_factory_policies
    from hypogeum.workspaces import platform_workspace_id
    try:
        own = float(get_factory_policies(workspace_id, db_path=_p(db_path)).get("monthly_ceiling_usd") or 0)
    except Exception:  # noqa: BLE001
        own = 0.0
    if own > 0 or workspace_id != platform_workspace_id(_p(db_path)):
        return own
    try:
        from hypogeum.config import Config
        return float(Config().FINANCE_BUDGET_CEILING_USD or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def budget_status(workspace_id: str, db_path=None) -> dict:
    spent, ceiling = month_spend(workspace_id, db_path), month_ceiling(workspace_id, db_path)
    return {"workspace_id": workspace_id, "month_spend_usd": spent, "ceiling_usd": ceiling,
            "over": bool(ceiling > 0 and spent >= ceiling)}


def blocked_channels(db_path=None) -> dict:
    """{channel_id: reason} for channels whose workspace is paused or over its
    monthly ceiling. Channels with no owner are never listed here (Phase 2
    refuses their spend once client workspaces exist)."""
    import sqlite3
    from hypogeum.workspaces import list_workspaces, resources_of
    try:
        workspaces = list_workspaces(_p(db_path))
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {}   # install without workspaces: nothing to block per workspace
        raise
    if not workspaces:
        return {}
    # Single-tenant install (no client workspaces): the monthly ceiling stays
    # in factory_director.check_run_clearance()'s global gate exactly as
    # before (incl. its supervised validation_mode exemption) — only a
    # workspace pause blocks here. Multi-tenant: budgets are per workspace,
    # here, and the worker's global gate skips the budget check.
    per_workspace_budget = any(w["kind"] == "client" for w in workspaces)
    paused = paused_workspaces(db_path)
    out = {}
    for w in workspaces:
        wid = w["workspace_id"]
        reason = None
        if wid in paused:
            reason = f"workspace {wid} paused: {paused[wid] or 'no reason recorded'}"
        elif per_workspace_budget:
            st = budget_status(wid, db_path)
            if st["over"]:
                reason = (f"workspace {wid} over its monthly ceiling "
                          f"(${st['month_spend_usd']:.2f} of ${st['ceiling_usd']:.2f})")
        if reason:
            for ch in resources_of(wid, "channel", db_path=_p(db_path)):
                out[ch] = reason
    return out


def has_client_workspaces(db_path=None) -> bool:
    from hypogeum.workspace_keys import _has_client_workspaces
    return _has_client_workspaces(_p(db_path))


def operator_production_paused(db_path=None) -> bool:
    """The operator's own production is stopped: the global switch, or the
    platform owner's workspace pause (multi-tenant breaker trip)."""
    from hypogeum.db import get_worker_control
    if get_worker_control("queue_paused", "0", db_path=_p(db_path)) == "1":
        return True
    try:
        from hypogeum.workspaces import platform_workspace_id
        pid = platform_workspace_id(_p(db_path))
        return bool(pid) and is_paused(pid, db_path)
    except Exception:  # noqa: BLE001
        return False
