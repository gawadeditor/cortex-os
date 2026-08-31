"""
hypogeum/channel_readiness.py — one plain-language readiness report per channel.

Merges every "can this channel actually run?" signal the dashboard already
computes in four different places, and states each as an item an operator (or
a client in the Channel Builder wizard) can act on:

    calog.run_activation_check            — profile links, credentials, budget>0, OIS clear
    channel_runtime_status.get_*          — operating grant, worker health, run history,
                                            schedule, weekly-budget headroom, blockers
    zernio_accounts.get_*_snapshot        — the social platform links
    (local) budget-sanity                 — per-episode ceiling vs cadence, the trap that
                                            denied the first autonomous Short this session

Every check is best-effort and never raises. Each item is:

    {"id": str, "level": "ok"|"warn"|"blocker",
     "title": str, "detail": str, "fix": str|None}

Public API:
    get_channel_readiness(channel_id, db_path=None) -> dict
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

_SHORTS_CEILING_HINT = 20.0   # a ~60s Short compiles to a ~$9 plan (commit c42ac0a)
_LONG_CEILING_HINT = 60.0


def _item(id_: str, level: str, title: str, detail: str, fix: Optional[str] = None) -> dict:
    return {"id": id_, "level": level, "title": title, "detail": detail, "fix": fix}


def get_channel_readiness(channel_id: str, db_path: Optional[Path] = None) -> dict:
    """
    Returns::

        {
          "channel_id": str,
          "ready": bool,                 # no blockers
          "items": [ {id, level, title, detail, fix}, ... ],   # blockers first
          "counts": {"ok": int, "warn": int, "blocker": int},
        }
    """
    items: list[dict] = []

    # ── 1. activation gates (calog) ─────────────────────────────────────
    try:
        from hypogeum.calog import run_activation_check  # noqa: PLC0415
        res = run_activation_check(channel_id, db_path=db_path)
        for g in getattr(res, "gate_results", []) or []:
            if g.passed:
                items.append(_item(f"gate.{g.gate}", "ok", _GATE_TITLE.get(g.gate, g.gate), g.reason))
            else:
                items.append(_item(
                    f"gate.{g.gate}", "blocker", _GATE_TITLE.get(g.gate, g.gate), g.reason,
                    _GATE_FIX.get(g.gate),
                ))
    except Exception as exc:  # noqa: BLE001
        items.append(_item("gate.error", "warn", "Activation gates",
                           f"could not evaluate activation gates: {exc}"))

    # ── 2. runtime status (grant, worker, schedule, weekly budget) ──────
    rt: dict = {}
    try:
        from hypogeum.channel_runtime_status import get_channel_runtime_status  # noqa: PLC0415
        rt = get_channel_runtime_status(channel_id, db_path=db_path) or {}
    except Exception as exc:  # noqa: BLE001
        items.append(_item("runtime.error", "warn", "Runtime status",
                           f"could not read runtime status: {exc}"))

    grant = rt.get("operating_grant") or {}
    ops = rt.get("operations") or {}
    if ops.get("operator_enabled"):
        if not grant.get("active"):
            items.append(_item(
                "grant.active", "blocker", "Channel Operating Grant",
                "no active grant — autonomous paid work cannot be authorized.",
                "Sign one offline: python3 tools/configure_channel_operating_grant.py "
                f"--channel {channel_id}",
            ))
        elif grant.get("ceiling_matches_config") is False:
            items.append(_item(
                "grant.ceiling", "blocker", "Channel Operating Grant",
                "grant weekly ceiling no longer matches the configured budget.",
                "Re-sign the grant after the budget change.",
            ))
        else:
            items.append(_item(
                "grant.active", "ok", "Channel Operating Grant",
                f"active — {grant.get('days_left', '?')} days left, "
                f"${grant.get('weekly_exposure_ceiling_usd', '?')}/wk.",
            ))
    else:
        items.append(_item(
            "operator.enabled", "warn", "Operator switch",
            "the channel is configured but the operator switch is off — it will "
            "not run autonomously yet.",
            "Turn on 'operator enabled' in the channel's production plan.",
        ))

    worker = rt.get("worker") or {}
    sched = rt.get("schedule") or {}
    if sched.get("has_active_schedule") and worker.get("stale"):
        items.append(_item(
            "worker.stale", "blocker", "Production worker",
            "a schedule is active but the worker isn't dispatching (no recent heartbeat).",
            "Start the worker from the top bar, or POST /api/worker/start.",
        ))
    elif worker.get("status") in ("running", "claiming"):
        items.append(_item("worker.status", "ok", "Production worker", "running."))

    health = (rt.get("health") or {}).get("latest") or {}
    if health.get("bucket") and health["bucket"] not in ("ok", "operator_stopped"):
        items.append(_item(
            "run.health", "warn", "Last production run",
            f"the last run ended in '{health['bucket']}'"
            + (f": {health.get('error_message')}" if health.get("error_message") else "."),
        ))

    # ── 3. weekly-budget headroom + per-episode ceiling sanity ──────────
    wb = rt.get("weekly_budget") or {}
    cadence = ops.get("production_cadence") or {}
    shorts_only = cadence.get("videos_per_week") == 0 and (cadence.get("shorts_per_week") or 0) >= 1
    per_ep = _resolve_effective_per_episode_ceiling(channel_id, ops, db_path)

    if shorts_only and (per_ep is None or per_ep > 30.0):
        items.append(_item(
            "budget.per_episode", "warn", "Per-episode ceiling",
            f"this is a shorts-only channel but the per-episode ceiling is "
            f"{'unset (inherits the long-form factory ceiling)' if per_ep is None else f'${per_ep:.0f}'}. "
            f"A ~60s Short costs ≈ $9–11; the admission hold reserves the full ceiling, "
            f"which can deny a run against a tight weekly budget.",
            f"Set the per-episode ceiling to ≈ ${_SHORTS_CEILING_HINT:.0f} in the production plan.",
        ))

    remaining = wb.get("remaining_usd")
    if isinstance(remaining, (int, float)) and per_ep and remaining < per_ep:
        items.append(_item(
            "budget.headroom", "warn", "Weekly budget headroom",
            f"only ${remaining:.2f} left this week (ceiling ${wb.get('ceiling_usd', '?')}), "
            f"below one episode's ${per_ep:.0f} admission hold. Runs will be denied until "
            f"the week resets ({wb.get('window_end_utc', '?')}) or the ceiling is raised.",
        ))

    # ── 4. Zernio platform links ───────────────────────────────────────
    try:
        from hypogeum.publishing.zernio_accounts import get_zernio_accounts_snapshot  # noqa: PLC0415
        snap = get_zernio_accounts_snapshot(db_path) or {}
        by_p = snap.get("by_platform") or {}
        wanted = _enabled_platforms(channel_id, db_path)
        if not by_p:
            items.append(_item(
                "zernio.accounts", "warn", "Publishing (Zernio)",
                "Zernio has not reported any connected platform accounts yet.",
                "Open Platform Connections and Sync from Zernio.",
            ))
        else:
            bad = [p for p in wanted if p in by_p and by_p[p].get("status") != "active"]
            missing = [p for p in wanted if p not in by_p]
            if bad or missing:
                items.append(_item(
                    "zernio.accounts", "warn", "Publishing (Zernio)",
                    "some target platforms are not linked/active on Zernio: "
                    + ", ".join(bad + missing) + ".",
                    "Reconnect them in the Zernio dashboard, then Sync.",
                ))
            else:
                items.append(_item(
                    "zernio.accounts", "ok", "Publishing (Zernio)",
                    f"{len([p for p in by_p.values() if p.get('status') == 'active'])} platform "
                    f"account(s) active"
                    + (" · analytics add-on on" if snap.get("has_analytics_access") else "") + ".",
                ))
    except Exception as exc:  # noqa: BLE001
        items.append(_item("zernio.error", "warn", "Publishing (Zernio)",
                           f"could not read the Zernio snapshot: {exc}"))

    # ── order: blockers, then warnings, then ok ────────────────────────
    order = {"blocker": 0, "warn": 1, "ok": 2}
    items.sort(key=lambda i: order.get(i["level"], 3))
    counts = {
        "ok": sum(1 for i in items if i["level"] == "ok"),
        "warn": sum(1 for i in items if i["level"] == "warn"),
        "blocker": sum(1 for i in items if i["level"] == "blocker"),
    }
    return {
        "channel_id": channel_id,
        "ready": counts["blocker"] == 0,
        "items": items,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _resolve_effective_per_episode_ceiling(channel_id, ops, db_path) -> Optional[float]:
    chan = ops.get("cost_ceiling_usd_per_episode")
    if not isinstance(chan, (int, float)):
        try:
            from hypogeum.channel_config import get_channel_operations_profile  # noqa: PLC0415
            _p = get_channel_operations_profile(channel_id, db_path=db_path)
            chan = getattr(_p, "cost_ceiling_usd_per_episode", None) if _p else None
        except Exception:  # noqa: BLE001
            chan = None
    if isinstance(chan, (int, float)) and chan > 0:
        return float(chan)
    try:
        from hypogeum.policy_cascade import get_effective_budget_ceiling  # noqa: PLC0415
        from hypogeum.config import Config  # noqa: PLC0415
        env_c = float(getattr(Config(), "FINANCE_BUDGET_CEILING_USD", 0.0) or 0.0)
        eff = get_effective_budget_ceiling(env_ceiling=env_c, channel_id=channel_id, db_path=db_path)
        return float(eff) if eff and eff > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _enabled_platforms(channel_id, db_path) -> list:
    try:
        from hypogeum.channel_config import load_channel_config  # noqa: PLC0415
        cfg = load_channel_config(channel_id, db_path=db_path)
        pub = getattr(cfg, "publishing", None)
        return list(getattr(pub, "enabled_platforms", []) or []) if pub else []
    except Exception:  # noqa: BLE001
        return []


_GATE_TITLE = {
    "channel_exists": "Channel record",
    "profile_complete": "Configuration profiles",
    "credentials": "Provider credentials",
    "budget": "Budget ceiling",
    "ois_clear": "Operational blocks",
}
_GATE_FIX = {
    "profile_complete": "Finish the wizard — brand, models and publishing must all be set.",
    "credentials": "Add the core provider API keys in Secrets / API Keys.",
    "budget": "Set a weekly budget and a per-episode ceiling in the production plan.",
    "ois_clear": "Clear the blocking operational signal before activating.",
}
