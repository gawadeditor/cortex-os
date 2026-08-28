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
     retry / fallback / contingency allowance;
  2. assembles EpisodeCostModel from NON-OVERLAPPING primitives
     (`episode_cost_incurred` counted EXACTLY ONCE):
         weekly_capacity_before_episode = configured_weekly_budget_limit - other_weekly_exposure
         future_capacity_available      = weekly_capacity_before_episode - episode_cost_incurred
         maximum_bounded_complete_episode_cost = episode_cost_incurred + maximum_bounded_remaining_cost
         required_future_reservation    = maximum_bounded_remaining_cost
  3. canonical fit check:  maximum_bounded_remaining_cost <= future_capacity_available
     (identical to  maximum_bounded_complete_episode_cost <= weekly_capacity_before_episode;
      tests assert both forms always agree).

If it fits: reserve `required_future_reservation` and continue.
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
    "build_required_asset_inventory",
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
        # deficit is measured on the NON-OVERLAPPING quantities:
        # maximum_bounded_remaining_cost vs future_capacity_available.
        self.deficit = (
            cost_model.maximum_bounded_remaining_cost - cost_model.future_capacity_available
        )
        super().__init__(
            f"episode {episode_id}: maximum_bounded_remaining_cost "
            f"{cost_model.maximum_bounded_remaining_cost} exceeds future_capacity_available "
            f"{cost_model.future_capacity_available} "
            f"(configured_weekly_budget_limit {cost_model.configured_weekly_budget_limit}, "
            f"other_weekly_exposure {cost_model.other_weekly_exposure}, "
            f"episode_cost_incurred {cost_model.episode_cost_incurred}) by {self.deficit}. "
            f"Components: {component_breakdown}"
        )


@dataclass(frozen=True)
class EpisodeCostModel:
    """Episode financial state as NON-OVERLAPPING primitives plus derived
    views. `episode_cost_incurred` is counted EXACTLY ONCE: it is
    subtracted from weekly capacity (to get `future_capacity_available`)
    and added to remaining cost (to get the complete-episode totals) --
    never both in the same comparison. All Decimal.

    Stored primitives (no overlap):
        configured_weekly_budget_limit  -- operator policy value
        other_weekly_exposure           -- reserved/committed exposure of
                                           OTHER work this channel-week
                                           (this episode's own PRE_PLAN /
                                           incurred-marker / plan rows are
                                           excluded)
        episode_cost_incurred           -- this episode's recorded spend
        expected_remaining_cost         -- realistic cost to finish (plan)
        maximum_bounded_remaining_cost  -- max authorized cost to finish
    """

    configured_weekly_budget_limit: Decimal
    other_weekly_exposure: Decimal
    episode_cost_incurred: Decimal
    expected_remaining_cost: Decimal
    maximum_bounded_remaining_cost: Decimal

    currency: str = "USD"
    channel_week_ref: str = ""
    budget_policy_ref: str = ""

    # ---- derived (each uses episode_cost_incurred at most once) ----
    @property
    def weekly_capacity_before_episode(self) -> Decimal:
        return self.configured_weekly_budget_limit - self.other_weekly_exposure

    @property
    def future_capacity_available(self) -> Decimal:
        return self.weekly_capacity_before_episode - self.episode_cost_incurred

    @property
    def expected_complete_episode_cost(self) -> Decimal:
        return self.episode_cost_incurred + self.expected_remaining_cost

    @property
    def maximum_bounded_complete_episode_cost(self) -> Decimal:
        return self.episode_cost_incurred + self.maximum_bounded_remaining_cost

    @property
    def required_future_reservation(self) -> Decimal:
        return self.maximum_bounded_remaining_cost

    def fits(self) -> bool:
        """THE canonical comparison. Equivalent to
        `maximum_bounded_complete_episode_cost <= weekly_capacity_before_episode`
        (proven equal in tests) -- this form is used everywhere because it
        makes the single-count of episode_cost_incurred obvious."""
        return self.maximum_bounded_remaining_cost <= self.future_capacity_available

    def fits_complete_form(self) -> bool:
        """The algebraically equivalent form -- used only by tests to
        assert both equations always agree."""
        return self.maximum_bounded_complete_episode_cost <= self.weekly_capacity_before_episode

    def deficit(self) -> Decimal:
        """Shortfall on the non-overlapping quantities; 0 when it fits."""
        gap = self.maximum_bounded_remaining_cost - self.future_capacity_available
        return gap if gap > 0 else Decimal("0")

    def min_weekly_limit_for(self, remaining_cost: Decimal) -> Decimal:
        """The smallest configured_weekly_budget_limit that would let a
        plan whose remaining cost is `remaining_cost` proceed this week,
        given the current other exposure + incurred spend."""
        return (self.other_weekly_exposure + self.episode_cost_incurred + remaining_cost)

    # ---- backward-compatible aliases (do not add new callers) ----
    @property
    def actual_incurred_cost(self) -> Decimal:
        return self.episode_cost_incurred

    @property
    def expected_completion_cost(self) -> Decimal:
        return self.expected_complete_episode_cost

    @property
    def maximum_bounded_completion_cost(self) -> Decimal:
        return self.maximum_bounded_complete_episode_cost

    @property
    def remaining_weekly_capacity(self) -> Decimal:
        return self.future_capacity_available

    @property
    def required_reservation_amount(self) -> Decimal:
        return self.required_future_reservation

    @property
    def other_week_exposure(self) -> Decimal:
        return self.other_weekly_exposure

    def as_dict(self) -> dict:
        return {
            "configured_weekly_budget_limit": str(self.configured_weekly_budget_limit),
            "other_weekly_exposure": str(self.other_weekly_exposure),
            "episode_cost_incurred": str(self.episode_cost_incurred),
            "weekly_capacity_before_episode": str(self.weekly_capacity_before_episode),
            "future_capacity_available": str(self.future_capacity_available),
            "expected_remaining_cost": str(self.expected_remaining_cost),
            "maximum_bounded_remaining_cost": str(self.maximum_bounded_remaining_cost),
            "expected_complete_episode_cost": str(self.expected_complete_episode_cost),
            "maximum_bounded_complete_episode_cost": str(self.maximum_bounded_complete_episode_cost),
            "required_future_reservation": str(self.required_future_reservation),
            "fits": self.fits(),
            "deficit": str(self.deficit()),
            "currency": self.currency,
            "channel_week_ref": self.channel_week_ref,
            "budget_policy_ref": self.budget_policy_ref,
        }


def build_required_asset_inventory(production_plan, *, script_package: dict, contract: dict) -> dict:
    """Explicit, itemised inventory of EVERY paid operation the compiled
    complete plan will reach before `ready_to_publish` -- derived from the
    deliverable contract + script structure + the plan's own priced
    assets, NEVER from any budget figure. Used for disclosure and to
    prove (in tests) that the inventory exists before any budget
    comparison and that every required deliverable is priced.

    `production_plan` is a compiled hypogeum.production_plan.ProductionPlan
    (or a compatible object exposing .assets / the *_cost_usd fields)."""
    from collections import Counter

    assets = list(getattr(production_plan, "assets", []) or [])
    roles = Counter(getattr(a, "role", "?") for a in assets)
    models = Counter(getattr(a, "model", "?") for a in assets)
    required_deliverables = [
        d["deliverable_id"] for d in contract.get("deliverables", []) if d.get("required")
    ]
    required_shorts = [
        d["deliverable_id"] for d in contract.get("deliverables", [])
        if d.get("required") and d.get("kind") in ("derived_short", "short")
    ]
    priced_deliverables = set(getattr(production_plan, "deliverable_ids", []) or [])
    missing = [d for d in required_deliverables if d not in priced_deliverables]

    sections = script_package.get("sections", []) or []
    shorts_scripts = script_package.get("shorts_scripts", []) or []
    beats = script_package.get("beats", []) or []
    if not beats:
        for s in sections:
            beats += s.get("beats", []) or s.get("narration_beats", []) or []

    def _f(name):
        return float(getattr(production_plan, name, 0.0) or 0.0)

    return {
        "required_deliverables": required_deliverables,
        "required_shorts_count": len(required_shorts),
        "required_shorts": required_shorts,
        "priced_deliverables": sorted(priced_deliverables),
        "missing_required_deliverables": missing,   # non-empty -> plan is INCOMPLETE
        "narrative_beat_count": len(beats),
        "script_section_count": len(sections),
        "shorts_script_count": len(shorts_scripts),
        "narration_char_count": int(getattr(production_plan, "narration_char_count", 0) or 0),
        "video_asset_count_total": len(assets),
        "video_primary_shot_count": roles.get("required", 0),
        "video_contingency_shot_count": roles.get("contingency", 0),
        "video_models": dict(models),
        "max_paid_attempts_per_scene": getattr(production_plan, "max_chargeable_attempts", None)
            or (getattr(assets[0], "max_chargeable_attempts", None) if assets else None),
        "source_enhancement_operations_max": len(assets),
        "music_section_count": int(getattr(production_plan, "music_section_count", 0) or 0),
        "thumbnail_main_candidate_count": int(getattr(production_plan, "thumbnail_main_candidate_count", 0) or 0),
        "thumbnail_short_candidate_cap": int(getattr(production_plan, "thumbnail_short_candidate_cap", 0) or 0),
        "thumbnail_attempts_per_candidate": int(getattr(production_plan, "thumbnail_attempts_per_candidate", 0) or 0),
        "component_costs_usd": {
            "narration": _f("narration_cost_usd"),
            "music": _f("music_cost_usd"),
            "enhancement_maximum": _f("enhancement_cost_maximum_usd"),
            "thumbnail_normal": _f("thumbnail_cost_normal_usd"),
            "thumbnail_maximum": _f("thumbnail_cost_maximum_usd"),
            "planning_call_estimate": _f("planning_call_estimate_usd"),
            "post_plan_normal_total": _f("normal_expected_cost_usd"),
            "post_plan_maximum_total": _f("maximum_bounded_cost_usd"),
        },
    }


@dataclass
class TruthfulPlanResult:
    production_plan: Any
    cost_model: EpisodeCostModel
    coverage_sufficient: bool
    substitution_applied: list = field(default_factory=list)
    is_alternative_plan: bool = False
    asset_inventory: dict = field(default_factory=dict)

    def accounting_ok(self) -> bool:
        cm = self.cost_model
        return (
            cm.fits()
            and cm.fits() == cm.fits_complete_form()   # both equations agree
            and self.coverage_sufficient
            and cm.required_future_reservation == Decimal(str(self.production_plan.maximum_bounded_cost_usd))
            and cm.deficit() == Decimal("0")
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


def coverage_required_video_cost(production_plan, narration_package: dict):
    """Per beat, the number of video shots the coverage arithmetic
    (hypogeum.agents.coverage_check) genuinely REQUIRES to fill that
    beat's narration duration under the <=1.15x editorial-speed cap, and
    the summed price of exactly that many shots. A planned shot BEYOND
    the per-beat minimum is a TRUE contingency asset (an alternate /
    replacement); a planned shot within the minimum is baseline required
    work and belongs in the EXPECTED cost, not only the maximum.

    Returns (required_video_cost: Decimal, required_shot_count: int,
    true_contingency_shot_count: int).
    """
    import math
    from collections import defaultdict
    from hypogeum.agents.coverage_check import MAX_EDITORIAL_SPEED_FACTOR

    durations = {}
    for seg in (narration_package or {}).get("narration_segments", []) or []:
        durations[seg.get("beat_reference") or seg.get("beat_id")] = float(seg.get("duration_seconds", 0.0))

    by_beat = defaultdict(list)
    for a in getattr(production_plan, "assets", []) or []:
        by_beat[getattr(a, "beat_id", "")].append(a)

    req_cost = Decimal("0")
    req_shots = 0
    cont_shots = 0
    for beat, assets in by_beat.items():
        assets = sorted(assets, key=lambda x: float(getattr(x, "unit_price_usd", 0)))
        nd = durations.get(beat, 0.0)
        per_clip = float(getattr(assets[0], "conservative_usable_duration_seconds", 8.0)) * MAX_EDITORIAL_SPEED_FACTOR
        min_req = max(1, math.ceil(nd / per_clip)) if (nd > 0 and per_clip > 0) else 1
        min_req = min(min_req, len(assets))
        for a in assets[:min_req]:
            req_cost += _q(getattr(a, "unit_price_usd", 0))
        req_shots += min_req
        cont_shots += len(assets) - min_req
    return req_cost, req_shots, cont_shots


def compute_episode_cost_model(
    channel_id: str, episode_id: str, *, production_plan=None, narration_package: Optional[dict] = None,
    verified_music_credit_usd: Decimal = Decimal("0"),
    now: Optional[datetime] = None, db_path: Optional[Path] = None,
) -> EpisodeCostModel:
    """Assemble the financial concepts. When `production_plan` is given,
    `expected_remaining_cost` counts EVERY coverage-required video shot
    (not only the first per beat -- see coverage_required_video_cost),
    plus enhancement on every required clip, narration, music (priced
    FRESH unless `verified_music_credit_usd` is passed after the planner
    confirmed the exact reusable category tracks exist), the normal
    thumbnail set, and the planning estimate. `maximum_bounded_remaining_cost`
    is the plan's own maximum_bounded_cost_usd (all shots + all
    contingency + all enhancement + fresh music + max thumbnails).

    A machine-readable per-component expected-vs-maximum explanation is
    available via `EpisodeCostModel`-adjacent helpers / the truthful-plan
    result; see compile_truthful_production_plan."""
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

    if production_plan is not None:
        post_plan_maximum = _q(getattr(production_plan, "maximum_bounded_cost_usd", 0))
        assets = list(getattr(production_plan, "assets", []) or [])
        if assets and narration_package is not None:
            req_video, req_shots, _cont = coverage_required_video_cost(production_plan, narration_package)
            enh_per_op = (
                _q(getattr(production_plan, "enhancement_cost_maximum_usd", 0)) / Decimal(len(assets))
                if assets else Decimal("0")
            )
            fresh_music = _q(getattr(production_plan, "music_cost_usd", 0))
            music_expected = max(Decimal("0"), fresh_music - _q(verified_music_credit_usd))
            post_plan_expected = (
                req_video
                + Decimal(req_shots) * enh_per_op                      # enhancement on every required clip
                + _q(getattr(production_plan, "narration_cost_usd", 0))
                + music_expected
                + _q(getattr(production_plan, "thumbnail_cost_normal_usd", 0))
                + _q(getattr(production_plan, "planning_call_estimate_usd", 0))
            )
        else:
            # no narration durations available -> fall back to the plan's
            # own normal_expected (still separate from the maximum)
            post_plan_expected = _q(getattr(production_plan, "normal_expected_cost_usd", 0))
    else:
        post_plan_expected = Decimal("0")
        post_plan_maximum = Decimal("0")

    return EpisodeCostModel(
        configured_weekly_budget_limit=limit,
        other_weekly_exposure=other_exposure,
        episode_cost_incurred=incurred,
        expected_remaining_cost=post_plan_expected,
        maximum_bounded_remaining_cost=post_plan_maximum,
        currency="USD",
        channel_week_ref=scope_ref,
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
    contract: Optional[dict] = None,
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
    # The full required-asset inventory is derived from the deliverable
    # contract + script structure BEFORE any budget comparison.
    if contract is None:
        from hypogeum.production_plan import load_deliverable_contract
        contract = load_deliverable_contract(channel_id)
    inventory = build_required_asset_inventory(plan, script_package=script_package, contract=contract)
    if not cov.sufficient or inventory["missing_required_deliverables"]:
        # An incomplete plan is never valid -- it can never be made
        # "affordable" by understating the work. Reject before any budget
        # comparison.
        from hypogeum.production_plan import ProductionPlanCoverageError
        _failing = list(getattr(cov, "failing_beats", []) or [])
        _failing += [{"beat_id": d} for d in inventory["missing_required_deliverables"]]
        raise ProductionPlanCoverageError(episode_id=episode_id, failing_beats=_failing)
    cost_model = compute_episode_cost_model(
        channel_id, episode_id, production_plan=plan,
        narration_package=narration_package or {}, now=now, db_path=db_path,
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
            "configured_weekly_budget_limit_usd": float(cost_model.configured_weekly_budget_limit),
            "other_weekly_exposure_usd": float(cost_model.other_weekly_exposure),
            "episode_cost_incurred_usd": float(cost_model.episode_cost_incurred),
            "weekly_capacity_before_episode_usd": float(cost_model.weekly_capacity_before_episode),
            "future_capacity_available_usd": float(cost_model.future_capacity_available),
            "expected_remaining_cost_usd": float(cost_model.expected_remaining_cost),
            "maximum_bounded_remaining_cost_usd": float(cost_model.maximum_bounded_remaining_cost),
            "expected_complete_episode_cost_usd": float(cost_model.expected_complete_episode_cost),
            "maximum_bounded_complete_episode_cost_usd": float(cost_model.maximum_bounded_complete_episode_cost),
            "deficit_usd": float(cost_model.deficit()),
            "min_weekly_limit_for_expected_usd": float(
                cost_model.min_weekly_limit_for(cost_model.expected_remaining_cost)
            ),
            "min_weekly_limit_for_maximum_usd": float(
                cost_model.min_weekly_limit_for(cost_model.maximum_bounded_remaining_cost)
            ),
            "expected_vs_maximum_explanation": _expected_vs_max_explanation(p, narration_package or {}),
        }

    if cost_model.fits() and cov.sufficient:
        return TruthfulPlanResult(
            production_plan=plan, cost_model=cost_model, coverage_sufficient=True,
            asset_inventory=inventory,
        )

    permitted = substitution_permitted(channel_id, config, db_path=db_path)
    if not permitted:
        raise ProductionPlanBudgetInfeasibleError(
            episode_id=episode_id, cost_model=cost_model,
            component_breakdown={**_breakdown(plan, cov), "asset_inventory": inventory},
            substitution_attempted=False,
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
        channel_id, episode_id, production_plan=alt_plan,
        narration_package=narration_package or {}, now=now, db_path=db_path,
    )

    if alt_cost_model.fits() and alt_cov.sufficient:
        if log is not None:
            try:
                log.info(
                    "budget_alternative_plan", episode_id=episode_id,
                    original_maximum_bounded_complete=str(cost_model.maximum_bounded_complete_episode_cost),
                    alternative_maximum_bounded_complete=str(alt_cost_model.maximum_bounded_complete_episode_cost),
                    configured_limit=str(alt_cost_model.configured_weekly_budget_limit),
                    substitutions=applied,
                )
            except Exception:  # noqa: BLE001
                pass
        alt_inventory = build_required_asset_inventory(
            alt_plan, script_package=script_package, contract=contract,
        )
        return TruthfulPlanResult(
            production_plan=alt_plan, cost_model=alt_cost_model, coverage_sufficient=bool(alt_cov.sufficient),
            substitution_applied=applied, is_alternative_plan=True, asset_inventory=alt_inventory,
        )

    raise ProductionPlanBudgetInfeasibleError(
        episode_id=episode_id, cost_model=alt_cost_model,
        component_breakdown={
            **_breakdown(alt_plan, alt_cov), "substitutions_attempted": applied,
            "asset_inventory": build_required_asset_inventory(
                alt_plan, script_package=script_package, contract=contract,
            ),
        },
        substitution_attempted=True,
    )


def _expected_vs_max_explanation(plan, narration_package: dict) -> dict:
    """Machine-readable: WHY each component's maximum exceeds its expected.
    Every difference is a specific, bounded allowance -- never hidden
    baseline work."""
    assets = list(getattr(plan, "assets", []) or [])
    if not assets:
        return {}
    req_video, req_shots, cont_shots = coverage_required_video_cost(plan, narration_package)
    max_video = sum(_q(getattr(a, "unit_price_usd", 0)) for a in assets)
    enh_per_op = (
        _q(getattr(plan, "enhancement_cost_maximum_usd", 0)) / Decimal(len(assets)) if assets else Decimal("0")
    )
    return {
        "video": {
            "expected_usd": float(req_video),
            "maximum_usd": float(max_video),
            "delta_usd": float(max_video - req_video),
            "reason": (
                f"{req_shots} coverage-required shots (baseline, in expected) + "
                f"{cont_shots} true contingency shots (alternate/replacement footage, "
                f"consumed only if a required shot fails QA)"
            ),
        },
        "enhancement": {
            "expected_usd": float(Decimal(req_shots) * enh_per_op),
            "maximum_usd": float(getattr(plan, "enhancement_cost_maximum_usd", 0.0) or 0.0),
            "delta_usd": float(_q(getattr(plan, "enhancement_cost_maximum_usd", 0)) - Decimal(req_shots) * enh_per_op),
            "reason": "one upscale op per required clip (expected); one per every planned clip (maximum)",
        },
        "music": {
            "expected_usd": float(getattr(plan, "music_cost_usd", 0.0) or 0.0),
            "maximum_usd": float(getattr(plan, "music_cost_usd", 0.0) or 0.0),
            "delta_usd": 0.0,
            "reason": (
                "priced FRESH in both -- cache reuse credited only when the planner verifies "
                "the exact reusable category tracks exist before reservation"
            ),
        },
        "thumbnail": {
            "expected_usd": float(getattr(plan, "thumbnail_cost_normal_usd", 0.0) or 0.0),
            "maximum_usd": float(getattr(plan, "thumbnail_cost_maximum_usd", 0.0) or 0.0),
            "delta_usd": float(
                _q(getattr(plan, "thumbnail_cost_maximum_usd", 0)) - _q(getattr(plan, "thumbnail_cost_normal_usd", 0))
            ),
            "reason": "first candidate succeeds (expected); every candidate x every attempt (maximum)",
        },
        "narration": {
            "expected_usd": float(getattr(plan, "narration_cost_usd", 0.0) or 0.0),
            "maximum_usd": float(getattr(plan, "narration_cost_usd", 0.0) or 0.0),
            "delta_usd": 0.0,
            "reason": "already realized -- fixed character count",
        },
    }


def _plan_component(plan, prefix: str) -> float:
    total = 0.0
    for name in dir(plan):
        if name.startswith(prefix) and name.endswith("_usd"):
            try:
                total += float(getattr(plan, name) or 0.0)
            except (TypeError, ValueError):
                pass
    return round(total, 4)
