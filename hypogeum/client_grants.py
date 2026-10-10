"""
hypogeum/client_grants.py — Channel Operating Grants for client workspaces (multi-tenant plan, Phase 7).

Decision (operator, 2026-10-10): the PLATFORM operator signs every client channel's grant -- the Ed25519 private key
never leaves the operator's machine (ADR-016), and the client cannot authorize spending on their own. This module
adds what signing a CLIENT channel needs on top of the existing flow (tools/configure_channel_operating_grant.py,
hypogeum/grant_signing.py -- unchanged):

    grant_state(channel_id, db_path)            -> {active, days_left, weekly_ceiling_usd, expires_at}
    signing_preflight(channel_id, db_path)      -> {"blockers": [...], "warnings": [...]}  (shown before signing;
                                                   a blocker refuses the signature)
    channels_needing_grant(db_path, now)        -> [{workspace_id, channel_id, reason}]  (client channels that cannot
                                                   produce: no active grant, or expiring within EXPIRY_WARN_DAYS)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

EXPIRY_WARN_DAYS = 14
PROVIDER_KEYS = ("openrouter", "muapi", "elevenlabs", "perplexity")
WEEKS_PER_MONTH = 4.33


def _p(db_path):
    return Path(db_path) if db_path else None


def grant_state(channel_id: str, db_path=None, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    try:
        from hypogeum.authorization_authority import resolve_active_operating_grant  # noqa: PLC0415
        g = resolve_active_operating_grant(channel_id, at=now.isoformat(), db_path=_p(db_path))
    except Exception:  # noqa: BLE001
        g = None
    if g is None:
        return {"active": False, "days_left": 0, "weekly_ceiling_usd": None, "expires_at": None}
    try:
        exp = datetime.fromisoformat(str(g.expires_at).replace("Z", "+00:00"))
        days_left = max(0, (exp - now).days)
    except (TypeError, ValueError):
        days_left = 0
    ceiling = getattr(g, "weekly_exposure_ceiling_usd", None)
    return {"active": True, "days_left": days_left, "weekly_ceiling_usd": str(ceiling) if ceiling is not None else None,
            "expires_at": str(g.expires_at)}


def _weekly_limit(channel_id: str, db_path):
    try:
        from hypogeum.channel_budget_policy import resolve_configured_weekly_limit  # noqa: PLC0415
        return float(resolve_configured_weekly_limit(channel_id, db_path=_p(db_path))[1])
    except Exception:  # noqa: BLE001
        return None


def signing_preflight(channel_id: str, db_path=None) -> dict:
    """Problems to look at before signing. Platform-owner channels have none (their keys are `.env`). A blocker
    means signing would authorize spend that cannot work or has no owner."""
    from hypogeum import workspace_controls as wc, workspace_keys as wk, workspaces as ws  # noqa: PLC0415
    blockers, warnings = [], []
    owner = ws.workspace_of(channel_id, db_path=_p(db_path))
    kinds = {w["workspace_id"]: w["kind"] for w in ws.list_workspaces(_p(db_path))}
    multi = any(k == "client" for k in kinds.values())
    if owner is None:
        if multi:
            blockers.append("The channel belongs to no workspace, so nobody's balance would pay for its spending.")
        return {"blockers": blockers, "warnings": warnings, "workspace_id": None, "client": False}
    if kinds.get(owner) != "client":
        return {"blockers": [], "warnings": [], "workspace_id": owner, "client": False}
    missing = [p for p in PROVIDER_KEYS if not wk.workspace_key(p, owner, db_path=_p(db_path))]
    if missing:
        blockers.append("The workspace has no key for: " + ", ".join(missing) +
                        ". Add them first (scripts/workspaces.py set-key); the grant would authorize calls that cannot run.")
    if not any(wk.workspace_key(p, owner, db_path=_p(db_path)) for p in ("zernio", "postiz")):
        warnings.append("The workspace has no publishing key (Zernio or Postiz): videos can be made but not published.")
    weekly = _weekly_limit(channel_id, db_path)
    monthly = wc.month_ceiling(owner, db_path=_p(db_path))
    if weekly and monthly and weekly * WEEKS_PER_MONTH > monthly * 1.05:
        warnings.append(f"The weekly limit (${weekly:.2f}) is about ${weekly * WEEKS_PER_MONTH:.2f} a month, "
                        f"above the workspace's monthly ceiling (${monthly:.2f}); the ceiling will stop it first.")
    if weekly and not monthly:
        warnings.append("The workspace has no monthly ceiling set; only this weekly limit bounds its spending.")
    return {"blockers": blockers, "warnings": warnings, "workspace_id": owner, "client": True}


def channels_needing_grant(db_path=None, now: Optional[datetime] = None) -> list:
    from hypogeum import workspaces as ws  # noqa: PLC0415
    out = []
    for w in ws.list_workspaces(_p(db_path)):
        if w["kind"] != "client" or w.get("status") not in (None, "active"):
            continue
        for ch in ws.resources_of(w["workspace_id"], "channel", db_path=_p(db_path)):
            st = grant_state(ch, db_path, now)
            if not st["active"]:
                out.append({"workspace_id": w["workspace_id"], "channel_id": ch, "reason": "no active grant"})
            elif st["days_left"] <= EXPIRY_WARN_DAYS:
                out.append({"workspace_id": w["workspace_id"], "channel_id": ch,
                            "reason": f"grant expires in {st['days_left']} days"})
    return out
