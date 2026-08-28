"""
hypogeum/channel_budget_policy.py — bounded, Decimal-safe budget-aware
Production Plan compilation for an autonomously admitted episode (ADR-016 §5).

The autonomous runtime must be able to PRODUCE a complete episode within the
approved channel weekly ceiling (Hypogeum: USD 55.00), not merely fail closed
after compiling an unaffordable plan. Before Production Plan compilation this
module:

  1. reads the current channel-week's authoritative exposure from the accepted
     weekly-budget ledger (`budget_reservations`), excluding this episode's own
     PRE_PLAN / incurred / plan rows;
  2. reads this execution's actual incurred PRE_PLAN cost from `costs`;
  3. computes  remaining_allowance = ceiling - other_exposure - incurred_preplan ;
  4. compiles the plan and, if `maximum_bounded_cost_usd` exceeds the
     allowance, applies a fixed, deterministic, zero-network reduction ladder
     (channel-package policy, never Factory-Core creative knowledge) and
     recompiles EXACTLY ONCE;
  5. if the reduced plan still does not fit, raises
     ProductionPlanBudgetInfeasibleError with the exact irreducible cost
     components -- it never raises the ceiling, hides incurred cost, or drops
     a mandatory deliverable to make the arithmetic pass.

The accepted plan satisfies:
    incurred_preplan + plan.maximum_bounded_cost_usd
        <= remaining_allowance + incurred_preplan
        <= ceiling  (USD 55.00)
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from hypogeum.db import get_connection
from hypogeum.production_plan import compile_production_plan, validate_plan_coverage

__all__ = [
    "ProductionPlanBudgetInfeasibleError",
    "BudgetAwarePlanResult",
    "compute_remaining_post_plan_allowance",
    "compile_plan_within_allowance",
    "DEFAULT_REDUCTION_LADDER",
]

APPROVED_CEILING_FALLBACK_USD = Decimal("55.00")

# The channel-package reduction ladder. Order matters; each step is applied
# only if the plan still exceeds the allowance after the previous step. A
# channel package may override this list; the default is conservative and
# never removes a mandatory deliverable, narration, the thumbnail, or
# metadata.
DEFAULT_REDUCTION_LADDER = (
    "trim_non_coverage_contingency",   # step 2: drop contingency shots not needed for coverage
    "suppress_optional_enhancement",   # step 3: skip source enhancement (delivery contract already met)
    "downgrade_premium_video_tiers",   # step 4: route premium scenes to the channel default tier
    "reduce_paid_attempt_ceiling",     # step 4b: lower the per-scene paid-attempt bound (floor 2)
)


class ProductionPlanBudgetInfeasibleError(Exception):
    """Even after the full bounded reduction ladder, a contract-valid
    plan does not fit under the remaining weekly allowance. Carries the
    exact irreducible cost breakdown so the operator can see what to
    change (cadence, tier catalogue, deliverable contract)."""

    def __init__(self, *, episode_id: str, remaining_allowance: Decimal,
                 projected_maximum: Decimal, irreducible_components: dict):
        self.episode_id = episode_id
        self.remaining_allowance = remaining_allowance
        self.projected_maximum = projected_maximum
        self.irreducible_components = irreducible_components
        deficit = projected_maximum - remaining_allowance
        super().__init__(
            f"episode {episode_id}: reduced plan maximum {projected_maximum} exceeds "
            f"remaining weekly allowance {remaining_allowance} by {deficit}. "
            f"Irreducible components: {irreducible_components}"
        )


@dataclass
class BudgetAwarePlanResult:
    production_plan: Any
    remaining_allowance: Decimal
    incurred_preplan_usd: Decimal
    other_week_exposure_usd: Decimal
    ceiling_usd: Decimal
    reductions_applied: list = field(default_factory=list)
    recompiled: bool = False

    def accounting_ok(self) -> bool:
        pm = Decimal(str(self.production_plan.maximum_bounded_cost_usd))
        return (self.incurred_preplan_usd + pm) <= self.ceiling_usd and pm <= self.remaining_allowance


# ---------------------------------------------------------------------------
# Allowance
# ---------------------------------------------------------------------------

def _q(x) -> Decimal:
    return Decimal(str(x if x is not None else "0"))


def compute_remaining_post_plan_allowance(
    channel_id: str, episode_id: str, *, config=None,
    now: Optional[datetime] = None, db_path: Optional[Path] = None,
) -> dict:
    """Decimal-safe. Returns a dict with ceiling, other_week_exposure,
    incurred_preplan, remaining_allowance -- all Decimal."""
    from hypogeum import weekly_budget
    from hypogeum.preplan_reservation import (
        preplan_idempotency_key,
        preplan_incurred_idempotency_key,
    )
    from hypogeum.plan_reservation import plan_weekly_idempotency_key

    now = now or datetime.now(timezone.utc)
    conn = get_connection(db_path)
    try:
        tz_name, ceiling = weekly_budget.resolve_channel_weekly_policy(conn, channel_id)
        ceiling = _q(ceiling) if ceiling else APPROVED_CEILING_FALLBACK_USD
        window = weekly_budget.compute_weekly_window(now, tz_name)
        scope_ref = weekly_budget.week_scope_ref(channel_id, window)

        own_keys = {
            preplan_idempotency_key(channel_id, episode_id),
            preplan_incurred_idempotency_key(channel_id, episode_id),
        }
        # Plan key needs the content hash; exclude by prefix instead.
        plan_key_prefix = f"plan-weekly:{channel_id}:{episode_id}:"

        rows = conn.execute(
            "SELECT idempotency_key, reserved_amount FROM budget_reservations "
            "WHERE scope_kind = 'channel_weekly' AND scope_ref = ? "
            "AND status IN ('reserved','committed')",
            (scope_ref,),
        ).fetchall()
        other_exposure = Decimal("0")
        for r in rows:
            k = r["idempotency_key"]
            if k in own_keys or k.startswith(plan_key_prefix):
                continue
            other_exposure += _q(r["reserved_amount"])

        incurred_row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM costs WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        incurred_preplan = _q(incurred_row["s"])
    finally:
        conn.close()

    remaining = ceiling - other_exposure - incurred_preplan
    return {
        "ceiling_usd": ceiling,
        "other_week_exposure_usd": other_exposure,
        "incurred_preplan_usd": incurred_preplan,
        "remaining_allowance_usd": remaining,
        "channel_week_ref": scope_ref,
    }


# ---------------------------------------------------------------------------
# Deterministic reduction ladder (operates on a deep copy of visual_package)
# ---------------------------------------------------------------------------

def _min_contingency_for_coverage(scene: dict) -> int:
    """Number of contingency (order>=2) shots to KEEP for this beat so
    mathematical coverage still holds. Conservative: keep 1 contingency
    shot per beat (the coverage formula in hypogeum.production_plan
    requires at least one fallback slot); drop the rest."""
    return 1


def _apply_trim_non_coverage_contingency(vp: dict) -> Optional[str]:
    trimmed = 0
    for scene in vp.get("scenes", []):
        plan = scene.get("cinematic_coverage_plan") or {}
        shots = sorted(plan.get("shots", []), key=lambda s: s.get("shot_order", 0))
        keep_extra = _min_contingency_for_coverage(scene)
        new_shots, kept_extra = [], 0
        for shot in shots:
            if shot.get("shot_order", 1) <= 1:
                new_shots.append(shot)
                continue
            if kept_extra < keep_extra:
                new_shots.append(shot)
                kept_extra += 1
            else:
                trimmed += 1
        if trimmed and plan:
            plan["shots"] = new_shots
            scene["cinematic_coverage_plan"] = plan
    return f"trim_non_coverage_contingency: dropped {trimmed} contingency shots" if trimmed else None


def _apply_suppress_optional_enhancement(vp: dict) -> Optional[str]:
    touched = 0
    for scene in vp.get("scenes", []):
        spec = scene.setdefault("production_spec", {})
        if spec.get("enhancement_required") is not True and not spec.get("_budget_enhancement_suppressed"):
            spec["_budget_enhancement_suppressed"] = True
            touched += 1
    return f"suppress_optional_enhancement: suppressed on {touched} scenes" if touched else None


def _apply_downgrade_premium_video_tiers(vp: dict, default_tier: int) -> Optional[str]:
    downgraded = 0
    for scene in vp.get("scenes", []):
        routing = scene.setdefault("production_spec", {}).setdefault("provider_routing", {})
        tier = routing.get("primary_tier")
        if tier is not None and int(tier) < default_tier:
            routing["primary_tier"] = default_tier
            downgraded += 1
    return f"downgrade_premium_video_tiers: {downgraded} scenes -> tier {default_tier}" if downgraded else None


class _ConfigShim:
    """Read-through wrapper letting the budget policy override a small set
    of pricing-relevant config attributes without mutating the real
    Config object."""

    def __init__(self, base, overrides: dict):
        self._base = base
        self._ov = overrides

    def __getattr__(self, name):
        if name in self._ov:
            return self._ov[name]
        return getattr(self._base, name)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compile_plan_within_allowance(
    *, script_package: dict, narration_package: dict, visual_package: dict,
    channel_id: str, episode_id: str, config, log=None,
    now: Optional[datetime] = None, db_path: Optional[Path] = None,
    reduction_ladder=DEFAULT_REDUCTION_LADDER,
) -> BudgetAwarePlanResult:
    alloc = compute_remaining_post_plan_allowance(
        channel_id, episode_id, config=config, now=now, db_path=db_path,
    )
    remaining = alloc["remaining_allowance_usd"]
    ceiling = alloc["ceiling_usd"]
    incurred = alloc["incurred_preplan_usd"]

    def _compile(vp, cfg):
        plan = compile_production_plan(
            script_package=script_package, narration_package=narration_package or {},
            visual_package=vp, channel_id=channel_id, episode_id=episode_id, config=cfg,
        )
        cov = validate_plan_coverage(plan, narration_package or {})
        return plan, cov

    plan, cov = _compile(visual_package, config)
    result = BudgetAwarePlanResult(
        production_plan=plan, remaining_allowance=remaining, incurred_preplan_usd=incurred,
        other_week_exposure_usd=alloc["other_week_exposure_usd"], ceiling_usd=ceiling,
    )

    pm = Decimal(str(plan.maximum_bounded_cost_usd))
    if pm <= remaining and cov.sufficient:
        return result

    # ---- one bounded, deterministic, zero-network recompile ----
    default_tier = int(getattr(config, "MUAPI_DEFAULT_TIER", 2) or 2)
    base_attempts = int(getattr(config, "MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE", 3) or 3)
    reduced_vp = copy.deepcopy(visual_package)
    cfg_overrides: dict = {}
    reductions: list = []

    for step in reduction_ladder:
        if step == "trim_non_coverage_contingency":
            note = _apply_trim_non_coverage_contingency(reduced_vp)
        elif step == "suppress_optional_enhancement":
            note = _apply_suppress_optional_enhancement(reduced_vp)
            if note:
                cfg_overrides["SOURCE_ENHANCEMENT_ENABLED"] = False
        elif step == "downgrade_premium_video_tiers":
            note = _apply_downgrade_premium_video_tiers(reduced_vp, default_tier)
        elif step == "reduce_paid_attempt_ceiling":
            new_attempts = max(2, base_attempts - 1)
            if new_attempts < base_attempts:
                cfg_overrides["MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE"] = new_attempts
                note = f"reduce_paid_attempt_ceiling: {base_attempts} -> {new_attempts}"
            else:
                note = None
        else:  # pragma: no cover - unknown ladder step
            note = None
        if note:
            reductions.append(note)

    shim = _ConfigShim(config, cfg_overrides) if cfg_overrides else config
    reduced_plan, reduced_cov = _compile(reduced_vp, shim)
    reduced_pm = Decimal(str(reduced_plan.maximum_bounded_cost_usd))

    result.production_plan = reduced_plan
    result.reductions_applied = reductions
    result.recompiled = True

    if reduced_pm <= remaining and reduced_cov.sufficient:
        if log is not None:
            try:
                log.info(
                    "budget_aware_plan_reduced", episode_id=episode_id,
                    original_maximum=str(pm), reduced_maximum=str(reduced_pm),
                    remaining_allowance=str(remaining), reductions=reductions,
                )
            except Exception:  # noqa: BLE001
                pass
        return result

    # ---- irreducible -> fail closed BEFORE any POST_PLAN provider I/O ----
    raise ProductionPlanBudgetInfeasibleError(
        episode_id=episode_id, remaining_allowance=remaining, projected_maximum=reduced_pm,
        irreducible_components={
            "video_required_max_usd": _plan_component(reduced_plan, "video"),
            "enhancement_cost_maximum_usd": float(getattr(reduced_plan, "enhancement_cost_maximum_usd", 0.0) or 0.0),
            "thumbnail_cost_maximum_usd": float(getattr(reduced_plan, "thumbnail_cost_maximum_usd", 0.0) or 0.0),
            "narration_cost_usd": float(getattr(reduced_plan, "narration_cost_usd", 0.0) or 0.0),
            "music_cost_usd": float(getattr(reduced_plan, "music_cost_usd", 0.0) or 0.0),
            "planning_call_estimate_usd": float(getattr(reduced_plan, "planning_call_estimate_usd", 0.0) or 0.0),
            "coverage_sufficient": bool(reduced_cov.sufficient),
            "reductions_attempted": reductions,
            "other_week_exposure_usd": float(alloc["other_week_exposure_usd"]),
            "incurred_preplan_usd": float(incurred),
        },
    )


def _plan_component(plan, prefix: str) -> float:
    total = 0.0
    for name in dir(plan):
        if name.startswith(prefix) and name.endswith("_usd"):
            try:
                total += float(getattr(plan, name) or 0.0)
            except (TypeError, ValueError):
                pass
    return round(total, 4)
