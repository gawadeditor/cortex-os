"""
hypogeum/channel_budget_policy.py — truth-first, Decimal-safe episode cost
model and configurable-budget evaluation (ADR-016 §5, corrected 2026-08-28).

The financial limit is NOT an architectural constant. It is an
operator-controlled, versioned Channel policy value read from the
authoritative Channel Operations Profile
(`channel_operations_profiles.weekly_video_budget_usd`, via
`hypogeum.weekly_budget.resolve_channel_weekly_policy`). It may increase
or decrease over time; when it changes, a new / revised Channel Operating
Grant is required (the runtime never raises its own limit).

Before any POST_PLAN paid provider call, this module:

  1. compiles the TRUTHFUL COMPLETE Production Plan -- every mandatory
     deliverable, every Short, narration, visual coverage, music,
     thumbnails, metadata, enhancement, plus the workflow's configured
     retry / fallback / contingency allowance -- and exposes both
     `expected_total_cost` and `maximum_bounded_total_cost`;
  2. resolves the six financial concepts distinctly (see EpisodeCostModel);
  3. compares `maximum_bounded_total_cost` with `remaining_weekly_capacity`.

If it fits: reserve `required_reservation_amount` and continue.
If it does not fit: raise ProductionPlanBudgetInfeasibleError with the
full component-by-component breakdown and the exact deficit -- it NEVER
raises the limit, hides incurred cost, or omits mandatory work.

A cheaper ALTERNATIVE plan is produced only when the Channel Package
explicitly permits substitutions (`substitution_permitted`), and only if
the alternative is still a complete valid episode (coverage + deliverable
contract re-validated).
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
    "EpisodeCostModel",
    "TruthfulPlanResult",
    "resolve_configured_weekly_limit",
    "compute_episode_cost_model",
    "compile_truthful_production_plan",
    "substitution_permitted",
    "DEFAULT_SUBSTITUTION_LADDER",
]

# A cheaper alternative plan is only ever produced when the Channel
# Package opts in. Each step is a bounded, deterministic, zero-network
# transform; none removes a mandatory deliverable, narration, the
# thumbnail, metadata, or a required Short.
DEFAULT_SUBSTITUTION_LADDER = (
    "trim_non_coverage_contingency",
    "suppress_optional_enhancement",
    "downgrade_premium_video_tiers",
    "reduce_paid_attempt_ceiling",
)


class ProductionPlanBudgetInfeasibleError(Exception):
    """The truthful complete Production Plan's maximum bounded total cost
    exceeds the remaining weekly capacity under the operator-configured
    limit. Carries every input so the operator can decide: raise the
    configured limit and re-sign the Operating Grant, change the
    deliverable contract / tier catalogue / cadence, or permit
    substitutions. Never resolved by the runtime lowering its own bar or
    dropping mandatory work."""

    def __init__(self, *, episode_id: str, cost_model: "EpisodeCostModel",
                 component_breakdown: dict, substitution_attempted: bool):
        self.episode_id = episode_id
        self.cost_model = cost_model
        self.component_breakdown = component_breakdown
        self.substitution_attempted = substitution_attempted
        self.deficit = cost_model.maximum_bounded_completion_cost - cost_model.remaining_weekly_capacity
        super().__init__(
            f"episode {episode_id}: maximum_bounded_completion_cost "
            f"{cost_model.maximum_bounded_completion_cost} exceeds remaining_weekly_capacity "
            f"{cost_model.remaining_weekly_capacity} (configured_weekly_budget_limit "
            f"{cost_model.configured_weekly_budget_limit}) by {self.deficit}. "
            f"Components: {component_breakdown}"
        )


@dataclass(frozen=True)
class EpisodeCostModel:
    """The six financial concepts, kept strictly separate. All Decimal."""

    # 1. money already spent + recorded in the costs ledger for this episode
    actual_incurred_cost: Decimal
    # 2. best realistic estimate to complete every mandatory deliverable
    expected_completion_cost: Decimal
    # 3. maximum authorized cost incl. planned retries / fallbacks /
    #    contingencies / alternates / enhancement / other bounded paid ops
    maximum_bounded_completion_cost: Decimal
    # 4. operator-controlled policy value from the authoritative profile
    configured_weekly_budget_limit: Decimal
    # 5. configured limit minus actual+reserved exposure of OTHER work this week
    remaining_weekly_capacity: Decimal
    # 6. amount needed to authorize this complete plan with no hidden future spend
    required_reservation_amount: Decimal

    currency: str = "USD"
    channel_week_ref: str = ""
    other_week_exposure: Decimal = Decimal("0")
    budget_policy_ref: str = ""

    def fits(self) -> bool:
        return self.maximum_bounded_completion_cost <= self.remaining_weekly_capacity

    def as_dict(self) -> dict:
        return {
            "actual_incurred_cost": str(self.actual_incurred_cost),
            "expected_completion_cost": str(self.expected_completion_cost),
            "maximum_bounded_completion_cost": str(self.maximum_bounded_completion_cost),
            "configured_weekly_budget_limit": str(self.configured_weekly_budget_limit),
            "remaining_weekly_capacity": str(self.remaining_weekly_capacity),
            "required_reservation_amount": str(self.required_reservation_amount),
            "other_week_exposure": str(self.other_week_exposure),
            "currency": self.currency,
            "channel_week_ref": self.channel_week_ref,
            "budget_policy_ref": self.budget_policy_ref,
        }


@dataclass
class TruthfulPlanResult:
    production_plan: Any
    cost_model: EpisodeCostModel
    coverage_sufficient: bool
    substitution_applied: list = field(default_factory=list)
    is_alternative_plan: bool = False

    def accounting_ok(self) -> bool:
        cm = self.cost_model
        return (
            cm.fits()
            and self.coverage_sufficient
            and cm.required_reservation_amount == Decimal(str(self.production_plan.maximum_bounded_cost_usd))
        )


def _q(x) -> Decimal:
    return Decimal(str(x if x is not None else "0"))


# ---------------------------------------------------------------------------
# 4. configured_weekly_budget_limit -- operator-controlled, versioned
# ---------------------------------------------------------------------------

def resolve_configured_weekly_limit(
    channel_id: str, *, db_path: Optional[Path] = None, conn=None,
) -> "tuple[str, Decimal, str]":
    """Returns (timezone, configured_weekly_budget_limit, budget_policy_ref).

    The limit is read from the authoritative Channel Operations Profile
    (`weekly_video_budget_usd`) via
    hypogeum.weekly_budget.resolve_channel_weekly_policy, which FAILS
    CLOSED (typed WeeklyBudgetError) on a missing / malformed /
    non-positive value -- there is NO hardcoded fallback. The
    budget_policy_ref identifies the policy revision the limit came from
    (the ops-profile updated_at + metadata_version), so an Operating
    Grant can be bound to it and detected as stale when the operator
    changes the limit."""
    from hypogeum import weekly_budget

    _own_conn = conn is None
    conn = conn or get_connection(db_path)
    try:
        tz_name, ceiling = weekly_budget.resolve_channel_weekly_policy(conn, channel_id)
        row = conn.execute(
            "SELECT updated_at, metadata_version FROM channel_operations_profiles WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
    finally:
        if _own_conn:
            conn.close()
    updated_at = (row["updated_at"] if row else "") or ""
    meta_ver = (row["metadata_version"] if row else "") or ""
    policy_ref = f"channel_operations_profile:{channel_id}:{meta_ver}:{updated_at}"
    return tz_name, _q(ceiling).quantize(Decimal("0.01")), policy_ref


# ---------------------------------------------------------------------------
# 1/5/6 -- ledger reads
# ---------------------------------------------------------------------------

def _other_week_exposure(conn, channel_id: str, episode_id: str, scope_ref: str) -> Decimal:
    from hypogeum.preplan_reservation import (
        preplan_idempotency_key, preplan_incurred_idempotency_key,
    )

    own_keys = {
        preplan_idempotency_key(channel_id, episode_id),
        preplan_incurred_idempotency_key(channel_id, episode_id),
    }
    plan_key_prefix = f"plan-weekly:{channel_id}:{episode_id}:"
    rows = conn.execute(
        "SELECT idempotency_key, reserved_amount FROM budget_reservations "
        "WHERE scope_kind = 'channel_weekly' AND scope_ref = ? AND status IN ('reserved','committed')",
        (scope_ref,),
    ).fetchall()
    total = Decimal("0")
    for r in rows:
        k = r["idempotency_key"]
        if k in own_keys or k.startswith(plan_key_prefix):
            continue
        total += _q(r["reserved_amount"])
    return total


def _incurred_for_episode(conn, episode_id: str) -> Decimal:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM costs WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    return _q(row["s"])


def compute_episode_cost_model(
    channel_id: str, episode_id: str, *, production_plan=None,
    now: Optional[datetime] = None, db_path: Optional[Path] = None,
) -> EpisodeCostModel:
    """Assemble the six financial concepts. `production_plan` supplies the
    truthful POST_PLAN estimate/maximum; when omitted (a pre-compile
    probe) the plan-derived fields are 0 and the model reports only
    incurred / configured limit / remaining capacity."""
    now = now or datetime.now(timezone.utc)
    conn = get_connection(db_path)
    try:
        from hypogeum import weekly_budget
        tz_name, limit, policy_ref = resolve_configured_weekly_limit(channel_id, conn=conn)
        window = weekly_budget.compute_weekly_window(now, tz_name)
        scope_ref = weekly_budget.week_scope_ref(channel_id, window)
        other_exposure = _other_week_exposure(conn, channel_id, episode_id, scope_ref)
        incurred = _incurred_for_episode(conn, episode_id)
    finally:
        conn.close()

    remaining_capacity = limit - other_exposure - incurred

    if production_plan is not None:
        post_plan_expected = _q(getattr(production_plan, "normal_expected_cost_usd", 0))
        post_plan_maximum = _q(getattr(production_plan, "maximum_bounded_cost_usd", 0))
    else:
        post_plan_expected = Decimal("0")
        post_plan_maximum = Decimal("0")

    return EpisodeCostModel(
        actual_incurred_cost=incurred,
        expected_completion_cost=incurred + post_plan_expected,
        maximum_bounded_completion_cost=incurred + post_plan_maximum,
        configured_weekly_budget_limit=limit,
        remaining_weekly_capacity=remaining_capacity,
        required_reservation_amount=post_plan_maximum,
        currency="USD",
        channel_week_ref=scope_ref,
        other_week_exposure=other_exposure,
        budget_policy_ref=policy_ref,
    )


# ---------------------------------------------------------------------------
# Substitution opt-in (Channel Package policy, default OFF)
# ---------------------------------------------------------------------------

def substitution_permitted(channel_id: str, config=None, *, db_path: Optional[Path] = None) -> bool:
    """A cheaper alternative plan is only produced when the Channel
    Package explicitly permits substitutions. Default: NOT permitted --
    budget insufficiency is disclosed, not silently absorbed."""
    if config is not None and bool(getattr(config, "BUDGET_SUBSTITUTION_PERMITTED", False)):
        return True
    try:
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT retry_policy_json FROM channel_operations_profiles WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
        finally:
            conn.close()
        if row and row["retry_policy_json"]:
            import json
            rp = json.loads(row["retry_policy_json"])
            return bool(rp.get("budget_substitution_permitted", False))
    except Exception:  # noqa: BLE001
        pass
    return False


# ---------------------------------------------------------------------------
# Deterministic substitution ladder (deep copy of visual_package)
# ---------------------------------------------------------------------------

def _apply_trim_non_coverage_contingency(vp: dict) -> Optional[str]:
    trimmed = 0
    for scene in vp.get("scenes", []):
        plan = scene.get("cinematic_coverage_plan") or {}
        shots = sorted(plan.get("shots", []), key=lambda s: s.get("shot_order", 0))
        new_shots, kept_extra = [], 0
        for shot in shots:
            if shot.get("shot_order", 1) <= 1:
                new_shots.append(shot)
                continue
            if kept_extra < 1:  # keep exactly one fallback slot for coverage
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
    return f"suppress_optional_enhancement: {touched} scenes" if touched else None


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

def compile_truthful_production_plan(
    *, script_package: dict, narration_package: dict, visual_package: dict,
    channel_id: str, episode_id: str, config, log=None,
    now: Optional[datetime] = None, db_path: Optional[Path] = None,
    substitution_ladder=DEFAULT_SUBSTITUTION_LADDER,
) -> TruthfulPlanResult:
    """Compile the truthful COMPLETE Production Plan, then evaluate it
    against the operator-configured weekly limit.

    - Fits: return with required_reservation_amount == plan.maximum_bounded_cost_usd.
    - Does not fit, substitutions NOT permitted: raise
      ProductionPlanBudgetInfeasibleError with the full breakdown.
    - Does not fit, substitutions permitted: apply the bounded
      zero-network ladder once and re-evaluate; still over ->
      ProductionPlanBudgetInfeasibleError. A substituted plan must still
      pass coverage + deliverable-contract validation.
    """
    def _compile(vp, cfg):
        plan = compile_production_plan(
            script_package=script_package, narration_package=narration_package or {},
            visual_package=vp, channel_id=channel_id, episode_id=episode_id, config=cfg,
        )
        cov = validate_plan_coverage(plan, narration_package or {})
        return plan, cov

    plan, cov = _compile(visual_package, config)
    if not cov.sufficient:
        # An incomplete plan is never valid -- it can never be made
        # "affordable" by understating the work. Reject before any budget
        # comparison.
        from hypogeum.production_plan import ProductionPlanCoverageError
        raise ProductionPlanCoverageError(
            episode_id=episode_id, failing_beats=getattr(cov, "failing_beats", []),
        )
    cost_model = compute_episode_cost_model(
        channel_id, episode_id, production_plan=plan, now=now, db_path=db_path,
    )

    def _breakdown(p, c) -> dict:
        return {
            "video_generation_max_usd": _plan_component(p, "video"),
            "enhancement_cost_maximum_usd": float(getattr(p, "enhancement_cost_maximum_usd", 0.0) or 0.0),
            "thumbnail_cost_maximum_usd": float(getattr(p, "thumbnail_cost_maximum_usd", 0.0) or 0.0),
            "narration_cost_usd": float(getattr(p, "narration_cost_usd", 0.0) or 0.0),
            "music_cost_usd": float(getattr(p, "music_cost_usd", 0.0) or 0.0),
            "planning_call_estimate_usd": float(getattr(p, "planning_call_estimate_usd", 0.0) or 0.0),
            "asset_count": len(getattr(p, "assets", []) or []),
            "deliverable_ids": list(getattr(p, "deliverable_ids", []) or []),
            "coverage_sufficient": bool(c.sufficient),
            "actual_incurred_cost_usd": float(cost_model.actual_incurred_cost),
            "other_week_exposure_usd": float(cost_model.other_week_exposure),
            "expected_completion_cost_usd": float(cost_model.expected_completion_cost),
            "maximum_bounded_completion_cost_usd": float(cost_model.maximum_bounded_completion_cost),
        }

    if cost_model.fits() and cov.sufficient:
        return TruthfulPlanResult(production_plan=plan, cost_model=cost_model, coverage_sufficient=True)

    permitted = substitution_permitted(channel_id, config, db_path=db_path)
    if not permitted:
        raise ProductionPlanBudgetInfeasibleError(
            episode_id=episode_id, cost_model=cost_model,
            component_breakdown=_breakdown(plan, cov), substitution_attempted=False,
        )

    # ---- one bounded, deterministic, zero-network alternative plan ----
    default_tier = int(getattr(config, "MUAPI_DEFAULT_TIER", 2) or 2)
    base_attempts = int(getattr(config, "MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE", 3) or 3)
    alt_vp = copy.deepcopy(visual_package)
    cfg_overrides: dict = {}
    applied: list = []
    for step in substitution_ladder:
        if step == "trim_non_coverage_contingency":
            note = _apply_trim_non_coverage_contingency(alt_vp)
        elif step == "suppress_optional_enhancement":
            note = _apply_suppress_optional_enhancement(alt_vp)
            if note:
                cfg_overrides["SOURCE_ENHANCEMENT_ENABLED"] = False
        elif step == "downgrade_premium_video_tiers":
            note = _apply_downgrade_premium_video_tiers(alt_vp, default_tier)
        elif step == "reduce_paid_attempt_ceiling":
            new_attempts = max(2, base_attempts - 1)
            note = (f"reduce_paid_attempt_ceiling: {base_attempts} -> {new_attempts}"
                    if new_attempts < base_attempts else None)
            if note:
                cfg_overrides["MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE"] = new_attempts
        else:  # pragma: no cover
            note = None
        if note:
            applied.append(note)

    shim = _ConfigShim(config, cfg_overrides) if cfg_overrides else config
    alt_plan, alt_cov = _compile(alt_vp, shim)
    if not alt_cov.sufficient:
        # A substitution that would drop mandatory coverage is not a valid
        # alternative -- never silently accepted.
        from hypogeum.production_plan import ProductionPlanCoverageError
        raise ProductionPlanCoverageError(
            episode_id=episode_id, failing_beats=getattr(alt_cov, "failing_beats", []),
        )
    alt_cost_model = compute_episode_cost_model(
        channel_id, episode_id, production_plan=alt_plan, now=now, db_path=db_path,
    )

    if alt_cost_model.fits() and alt_cov.sufficient:
        if log is not None:
            try:
                log.info(
                    "budget_alternative_plan", episode_id=episode_id,
                    original_maximum=str(cost_model.maximum_bounded_completion_cost),
                    alternative_maximum=str(alt_cost_model.maximum_bounded_completion_cost),
                    configured_limit=str(alt_cost_model.configured_weekly_budget_limit),
                    substitutions=applied,
                )
            except Exception:  # noqa: BLE001
                pass
        return TruthfulPlanResult(
            production_plan=alt_plan, cost_model=alt_cost_model, coverage_sufficient=bool(alt_cov.sufficient),
            substitution_applied=applied, is_alternative_plan=True,
        )

    raise ProductionPlanBudgetInfeasibleError(
        episode_id=episode_id, cost_model=alt_cost_model,
        component_breakdown={**_breakdown(alt_plan, alt_cov), "substitutions_attempted": applied},
        substitution_attempted=True,
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
