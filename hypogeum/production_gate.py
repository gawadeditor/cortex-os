"""
hypogeum/production_gate.py — Expensive-run safety gate (2026-07-13)

Blocks full paid episode production by default while the Brand Bible /
Thumbnail DNA / unified-integrated-thumbnail quality system is still under
validation (see docs/operations/CURRENT_WORK_RESUME.md). Deliberately reuses
existing config/budget/preflight systems rather than introducing a new
budget ledger, a new dollar-amount policy, or a parallel preflight checker:

  - hypogeum.config.Config.FULL_PRODUCTION_ENABLED / PRODUCTION_MODE
    (new, this module) — the explicit authorization switch.
  - hypogeum.config.Config.FINANCE_BUDGET_CEILING_USD /
    MUAPI_EPISODE_BUDGET_CREDITS (pre-existing) — the configured maximum
    spend. No new dollar amount is hardcoded here.
  - hypogeum.setup_preflight.get_setup_preflight_status() (pre-existing) —
    the existing preflight validation (env vars, ffmpeg, budget ceiling,
    OIS signals, active channel), including its existing estimated-cost
    range.

Single-image/single-scene review generations (the pattern used throughout
this validation phase — a standalone script calling
generate_unified_integrated_thumbnail() or similar directly, never through
hypogeum.pipeline.run_pipeline()) are NOT subject to this gate — they never
call require_full_production_authorized(). This module only gates the full
9-agent episode pipeline, which is the accidental-expensive-run vector.

Public API:
    check_full_production_authorized(config, log=None) -> dict
    require_full_production_authorized(config, log=None) -> None  (raises)

Exceptions:
    ProductionGateBlocked — raised by require_full_production_authorized()
        when any check fails. Callers (hypogeum.pipeline.run_pipeline(),
        Dashboard/CLI entry points) must not catch-and-continue; this is a
        hard stop, not a warning.
"""
from __future__ import annotations

from typing import Optional


class ProductionGateBlocked(RuntimeError):
    """
    Raised when full paid episode production is not authorized.

    Attributes:
        reasons: list[str] — every reason the gate is closed, so the caller
            can fix all of them in one pass rather than discovering them
            one at a time.
    """

    def __init__(self, reasons: list) -> None:
        self.reasons = reasons
        super().__init__(
            "Full production blocked by the expensive-run safety gate: "
            + "; ".join(reasons)
        )


def check_full_production_authorized(config, log=None) -> dict:
    """
    Evaluate whether a full paid episode production run is authorized.

    Returns a dict (never raises):
        {
          "authorized": bool,
          "reasons": list[str],            # empty iff authorized
          "production_mode": str,
          "full_production_enabled": bool,
          "configured_max_spend_usd": float,
          "provider": "muapi",
          "thumbnail_model": str | None,
          "preflight_ready": bool,
          "preflight_blockers": list[str],
        }
    """
    reasons: list = []

    full_production_enabled = bool(getattr(config, "FULL_PRODUCTION_ENABLED", False))
    if not full_production_enabled:
        reasons.append("FULL_PRODUCTION_ENABLED is not True")

    production_mode = getattr(config, "PRODUCTION_MODE", "validation") or "validation"
    if production_mode == "validation":
        reasons.append("PRODUCTION_MODE is 'validation'")

    finance_ceiling = getattr(config, "FINANCE_BUDGET_CEILING_USD", 0.0) or 0.0
    muapi_ceiling = getattr(config, "MUAPI_EPISODE_BUDGET_CREDITS", 0.0) or 0.0
    configured_max_spend_usd = max(finance_ceiling, muapi_ceiling)
    if configured_max_spend_usd <= 0:
        reasons.append(
            "no configured maximum spend "
            "(set FINANCE_BUDGET_CEILING_USD or MUAPI_EPISODE_BUDGET_CREDITS > 0)"
        )

    preflight_ready = False
    preflight_blockers: list = []
    try:
        from hypogeum.setup_preflight import get_setup_preflight_status
        status = get_setup_preflight_status()
        preflight_ready = bool(status.get("ready", False))
        preflight_blockers = [b.get("name", "?") for b in status.get("blockers", [])]
    except Exception as exc:  # noqa: BLE001 — preflight failure must block, not crash
        preflight_blockers = [f"preflight check raised: {exc}"[:200]]
    if not preflight_ready:
        reasons.append(
            "preflight validation not passed"
            + (f" (blockers: {', '.join(preflight_blockers)})" if preflight_blockers else "")
        )

    result = {
        "authorized": len(reasons) == 0,
        "reasons": reasons,
        "production_mode": production_mode,
        "full_production_enabled": full_production_enabled,
        "configured_max_spend_usd": configured_max_spend_usd,
        "provider": "muapi",
        "thumbnail_model": getattr(config, "THUMBNAIL_IMAGE_MODEL", None),
        "preflight_ready": preflight_ready,
        "preflight_blockers": preflight_blockers,
    }

    if log is not None:
        log.info("production_gate_checked", **result)

    return result


def require_full_production_authorized(config, log=None) -> None:
    """
    Raise ProductionGateBlocked if full paid episode production is not
    authorized. Call this before any paid agent call in a full-episode run —
    hypogeum.pipeline.run_pipeline() calls this first, before the Executive
    Producer (agent 1 of 9) runs.
    """
    result = check_full_production_authorized(config, log=log)
    if not result["authorized"]:
        if log is not None:
            log.error("production_gate_blocked", reasons=result["reasons"])
        raise ProductionGateBlocked(result["reasons"])
