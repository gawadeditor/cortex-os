"""
hypogeum/preplan_cost_model.py -- authoritative, Decimal-safe PRE_PLAN cost
model (2026-08-29).

WHY THIS EXISTS
---------------
Before this module the PRE_PLAN maximum was a hand-asserted estimate
(`Decimal("9.40")` in tools/price_complete_hypogeum_episode.py, copied
into proof tests). That is evidence, not an authoritative bound, and must
never be used to prove USD 135 covers a fully bounded episode.

This module derives PRE_PLAN cost from three authoritative sources ONLY:

  1. the compiled workflow definition
     (channel_packages/<channel>/workflow/<workflow_id>.json) -- which
     stages run, and in what order, before the first authoritative
     Production Plan is compiled (mid `visual_package_director`);
  2. the SIGNED Channel Operating Grant bindings -- the per-call cost
     ceiling (`max_cost_per_call`), the per-binding spend ceiling
     (`max_total_spend`), and the retry/fallback authorization for every
     paid (provider, model, provider_operation) reachable in PRE_PLAN;
  3. structural limits -- fixed research-phase count, fixed hook-repair
     cap, the real narration-segment count (Audio Agent has already run
     by plan-compile time, so `narration_package` is authoritative), the
     script beat count, and the batched-planning batch sizes / retry
     caps read from the actual modules.

Historical average call counts are NEVER used as a maximum bound. Any
reachable paid operation without (a) a matching signed grant binding,
(b) a finite `max_cost_per_call`, or (c) a finite structural maximum
call count makes the model INFEASIBLE -- `coverage_complete` is False,
the operation is listed in `unbounded_components`, and
`compute_preplan_cost_model(..., require_feasible=True)` raises
`PreplanModelInfeasibleError` before any paid call.

WHAT IS AND IS NOT PRE_PLAN
--------------------------
PRE_PLAN = every paid provider call reachable before
`hypogeum.agents.visual_package_director` compiles and persists the
Production Plan and `atomic_upgrade_to_plan_reservation()` runs. In the
frozen `hypogeum_main_production` order that is:

    executive_producer          openrouter text.completion   x2
    research_director            perplexity research.query    x4  (fixed phases)
    research_director            openrouter text.completion   x1  (synthesis)
    narrative_architect          openrouter text.completion   x1
    script_engine                openrouter text.completion   x1
    thumbnail_strategy           openrouter text.completion   x1 + hook-repair
    audio_agent                  elevenlabs voice.narration_segment  x(segments)
    visual_package_director P1   openrouter text.completion   x(batched planning)

Music is NOT pre-plan: `hypogeum.agents.audio_agent` seals only the
narration package; the Music Agent runs during Assembly, AFTER the plan
compiles (see hypogeum/production_plan.py compile_production_plan(),
"Music -- genuine forward estimate"). Music is POST_PLAN.

Narration IS pre-plan and IS ALSO carried in
`ProductionPlan.maximum_bounded_cost_usd` as `narration_cost_usd` with
`narration_is_realized=True`. `hypogeum.channel_budget_policy.
compute_complete_episode_cost()` nets that realized amount out of the
POST_PLAN remaining so the complete-episode arithmetic does not double
count it.

This module never touches the network and never touches the production
database.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

from hypogeum.provider_domains import (
    UnknownProviderOperationError,
    resolve_provider_domain,
)

__all__ = [
    "PreplanCostModelError",
    "PreplanModelInfeasibleError",
    "PreplanComponent",
    "PreplanCostModel",
    "PRE_PLAN_STAGES",
    "compute_preplan_cost_model",
]

# The workflow stage at which the first authoritative Production Plan is
# compiled and the PRE_PLAN reservation is upgraded. Everything strictly
# before this stage, plus this stage's Phase-1 Claude planning, is
# PRE_PLAN.
_PLAN_COMPILE_STAGE = "visual_package_director"

# The PRE_PLAN stages in the frozen hypogeum_main_production order (used
# for validation against the compiled workflow definition; the model
# fails closed if the workflow's stage_order disagrees).
PRE_PLAN_STAGES = (
    "executive_producer",
    "research_director",
    "narrative_architect",
    "script_engine",
    "finance_agent_authorization",   # no paid provider call
    "thumbnail_strategy",
    "audio_agent",
    "visual_package_director",        # Phase 1 only
)

# ---- provider retry / fallback structural constants -----------------------
# Read from the provider modules (kept in sync via test
# test_preplan_cost_model.py::test_provider_retry_constants_unchanged).
_OPENROUTER_RETRY_MAX = 1          # hypogeum.openrouter.RETRY_MAX
_OPENROUTER_MODEL_FALLBACK = 1     # one-shot different-model fallback
_PERPLEXITY_RETRY_MAX = 1          # hypogeum.perplexity: range(RETRY_MAX + 1)
_ELEVENLABS_RETRY_MAX = 1          # hypogeum.elevenlabs._RETRY_MAX

# chargeable attempts per single logical call (first try + retries + one
# fallback where the provider path supports it)
_OPENROUTER_CHARGEABLE_PER_CALL = 1 + _OPENROUTER_RETRY_MAX + _OPENROUTER_MODEL_FALLBACK   # 3
_PERPLEXITY_CHARGEABLE_PER_CALL = 1 + _PERPLEXITY_RETRY_MAX                                # 2
_ELEVENLABS_CHARGEABLE_PER_CALL = 1 + _ELEVENLABS_RETRY_MAX                                # 2

# ---- structural limits (read from the actual modules) --------------------
_RD_RESEARCH_PHASE_COUNT = 4          # research_director._RESEARCH_PHASES (fixed list)
_THUMBNAIL_HOOK_REPAIR_MAX = 2        # thumbnail_strategy._MAX_HOOK_TEXT_REPAIR_ATTEMPTS
_EP_CALLS = 2                         # executive_producer: scorecard (always) + brief (PASS/WARN)

# Visual Package Director Phase-1 batched-planning stages. Each is a
# `run_agent()` loop over batches of the scene list; a batch halves on
# TruncatedResponse down to size 1, and the outer loop retries up to
# CINEMATIC_DIRECTOR_MAX_RETRIES+1 times for the cinematic-coverage stage.
# batch sizes: hypogeum.agents.visual_package_director module constants.
_VPD_P1_STAGES = (
    # (stage_id, base_batch_size, outer_attempts)
    ("vpd_p1_meta",            None, 1),   # Stage A1: exactly one call
    ("vpd_p1_scene_skeleton",  12,   1),   # Stage A2: _SCENE_SKELETON_BATCH_SIZE
    ("vpd_p1_presentation",    15,   1),   # Stage A4: _PRESENTATION_PLAN_BATCH_SIZE
    ("vpd_p1_scene_prompts",   10,   1),   # Stage B:  _SCENE_PROMPT_BATCH_SIZE
    ("vpd_p1_visual_intent_qa",10,   1),   # Visual Intent QA: _VISUAL_INTENT_QA_BATCH_SIZE
    ("vpd_p1_cinematic",       10,   3),   # Cinematic Director: CINEMATIC_DIRECTOR_MAX_RETRIES(2)+1
)


class PreplanCostModelError(Exception):
    """Base class for PRE_PLAN cost model failures."""


class PreplanModelInfeasibleError(PreplanCostModelError):
    """A reachable PRE_PLAN paid operation has no finite authoritative
    bound (no signed binding, non-finite per-call cost, or non-finite
    structural call count). PRE_PLAN paid work must not proceed."""

    def __init__(self, unbounded: Iterable[str], detail: str = ""):
        self.unbounded = tuple(unbounded)
        msg = (
            "PRE_PLAN cost model is INFEASIBLE -- unbounded components: "
            f"{list(self.unbounded)}"
        )
        if detail:
            msg = f"{msg}; {detail}"
        super().__init__(msg)


@dataclass(frozen=True)
class PreplanComponent:
    """One paid PRE_PLAN operation type, fully bounded from authoritative
    sources (or flagged unbounded)."""

    component_id: str
    stage: str
    provider: str
    model: str
    provider_operation: str
    provider_domain: str

    max_cost_per_call: Optional[Decimal]      # from the signed grant binding; None => unbounded
    expected_call_count: int
    maximum_call_count: Optional[int]         # structural; None => unbounded
    chargeable_attempts_per_call: int
    retry_allowance: int
    fallback_allowance: int
    conditional: bool
    bound_source: str

    binding_present: bool = True
    binding_max_total_spend: Optional[Decimal] = None

    @property
    def is_bounded(self) -> bool:
        return (
            self.binding_present
            and self.max_cost_per_call is not None
            and self.maximum_call_count is not None
        )

    @property
    def expected_cost(self) -> Decimal:
        """Conservative expected: expected call count x policy per-call
        ceiling, NO retries. Still an estimate for forecasting -- the
        feasibility proof uses `maximum_cost` only."""
        if self.max_cost_per_call is None:
            return Decimal("0")
        return (Decimal(self.expected_call_count) * self.max_cost_per_call).quantize(Decimal("0.000001"))

    @property
    def maximum_cost(self) -> Decimal:
        """Authoritative maximum: structural max call count x chargeable
        attempts per call x policy per-call ceiling."""
        if not self.is_bounded:
            return Decimal("0")
        return (
            Decimal(self.maximum_call_count)
            * Decimal(self.chargeable_attempts_per_call)
            * self.max_cost_per_call
        ).quantize(Decimal("0.000001"))

    def to_dict(self) -> dict:
        return {
            "component_id": self.component_id,
            "stage": self.stage,
            "provider": self.provider,
            "model": self.model,
            "provider_operation": self.provider_operation,
            "provider_domain": self.provider_domain,
            "max_cost_per_call": (str(self.max_cost_per_call) if self.max_cost_per_call is not None else None),
            "expected_call_count": self.expected_call_count,
            "maximum_call_count": self.maximum_call_count,
            "chargeable_attempts_per_call": self.chargeable_attempts_per_call,
            "retry_allowance": self.retry_allowance,
            "fallback_allowance": self.fallback_allowance,
            "conditional": self.conditional,
            "binding_present": self.binding_present,
            "binding_max_total_spend": (
                str(self.binding_max_total_spend) if self.binding_max_total_spend is not None else None
            ),
            "expected_cost": str(self.expected_cost),
            "maximum_cost": str(self.maximum_cost),
            "bounded": self.is_bounded,
            "bound_source": self.bound_source,
        }


@dataclass(frozen=True)
class PreplanCostModel:
    preplan_expected_cost: Decimal

    # `preplan_maximum_bounded_cost` is the raw STRUCTURAL worst case:
    # every batched-planning stage halving its batch to 1 and exhausting
    # every retry + fallback. It is genuinely finite (this is the point --
    # no component is unbounded) but it is loose: the batched VPD Phase-1
    # stages can drive it far above any sane budget.
    #
    # `preplan_policy_capped_maximum_cost` is the number that matters for
    # a feasibility proof: for each signed binding, min(sum of that
    # binding's PRE_PLAN component structural maxima, binding.max_total_spend).
    # The weekly ledger + the binding ceilings HARD STOP spend there --
    # a pathological retry storm fails the episode closed rather than
    # overspending. Use this for `complete_maximum_bounded_cost`.
    preplan_maximum_bounded_cost: Decimal
    preplan_policy_capped_maximum_cost: Decimal

    preplan_required_reservation: Decimal
    preplan_component_breakdown: tuple
    unbounded_components: tuple
    coverage_complete: bool
    currency: str = "USD"

    # diagnostics: per-binding PRE_PLAN allocation vs the signed ceiling
    binding_allocation: tuple = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "preplan_expected_cost": str(self.preplan_expected_cost),
            "preplan_maximum_bounded_cost": str(self.preplan_maximum_bounded_cost),
            "preplan_policy_capped_maximum_cost": str(self.preplan_policy_capped_maximum_cost),
            "preplan_required_reservation": str(self.preplan_required_reservation),
            "coverage_complete": self.coverage_complete,
            "unbounded_components": list(self.unbounded_components),
            "currency": self.currency,
            "preplan_component_breakdown": [c for c in self.preplan_component_breakdown],
            "binding_allocation": [b for b in self.binding_allocation],
        }


# ---------------------------------------------------------------------------
# binding lookup
# ---------------------------------------------------------------------------

def _iter_bindings(bindings: Any) -> Iterable[Any]:
    for b in bindings or ():
        yield b


def _find_binding(bindings: Any, provider: str, model: str, provider_operation: str):
    """Exact (provider, model, provider_operation) match. No aliasing."""
    for b in _iter_bindings(bindings):
        if (
            getattr(b, "provider", None) == provider
            and getattr(b, "model", None) == model
            and getattr(b, "provider_operation", None) == provider_operation
        ):
            return b
    return None


def _decimal_or_none(v) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None
    if not d.is_finite() or d < 0:
        return None
    return d


# ---------------------------------------------------------------------------
# batched-planning call-count bound
# ---------------------------------------------------------------------------

def _batched_max_calls(n_items: int, base_batch_size: Optional[int], outer_attempts: int) -> int:
    """Authoritative upper bound on run_agent() calls for one batched
    VPD Phase-1 stage.

    A batch of size B halves on TruncatedResponse: B -> B/2 -> ... -> 1,
    and every level is re-issued. The worst case is therefore the sum of
    the batch counts at every halving level, i.e.
        ceil(n/B) + ceil(n/(B/2)) + ... + ceil(n/1)
    which is bounded above by 2*n (geometric). The outer loop multiplies
    that by `outer_attempts`. A non-batched stage (base_batch_size None)
    is exactly `outer_attempts` calls.
    """
    if base_batch_size is None:
        return max(1, outer_attempts)
    total = 0
    b = max(1, int(base_batch_size))
    while b >= 1:
        total += math.ceil(n_items / b)
        if b == 1:
            break
        b = b // 2
    return total * max(1, outer_attempts)


# ---------------------------------------------------------------------------
# structural inputs
# ---------------------------------------------------------------------------

def _narration_segment_count(narration_package: Optional[dict], script_package: Optional[dict]) -> Optional[int]:
    """Authoritative: the count Audio Agent already produced. Fallback:
    the script beat count (main beats + shorts beats). None => unbounded."""
    if narration_package:
        segs = narration_package.get("narration_segments")
        if isinstance(segs, list) and segs:
            return len(segs)
    if script_package:
        n = 0
        for s in script_package.get("sections", []) or []:
            n += len(s.get("narration_beats", []) or s.get("beats", []) or [])
        for sh in script_package.get("shorts_scripts", []) or []:
            n += len(sh.get("narration_beats", []) or [])
        if n > 0:
            return n
    return None


def _scene_count(script_package: Optional[dict], narration_package: Optional[dict]) -> Optional[int]:
    """VPD Phase-1 plans one scene skeleton per narrative beat. Use the
    beat count; None => unbounded (no script to size against)."""
    return _narration_segment_count(narration_package, script_package)


# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------

def compute_preplan_cost_model(
    *,
    bindings: Any,
    workflow_definition: Optional[dict] = None,
    config: Any = None,
    script_package: Optional[dict] = None,
    narration_package: Optional[dict] = None,
    require_feasible: bool = False,
) -> PreplanCostModel:
    """Derive the authoritative PRE_PLAN cost model.

    `bindings` -- an iterable of objects with .provider/.model/
    .provider_operation/.max_cost_per_call/.max_total_spend (e.g.
    hypogeum.authorization_store.GrantBinding). Every PRE_PLAN paid
    operation must have an exact match or the model is infeasible.

    `workflow_definition` -- the compiled workflow dict; when given, its
    `stage_order` is validated to contain the PRE_PLAN stages in order
    before `visual_package_director`.
    """
    if workflow_definition is not None:
        _validate_workflow_order(workflow_definition)

    n_segments = _narration_segment_count(narration_package, script_package)
    n_scenes = _scene_count(script_package, narration_package)

    # Short-native / Episode-Zero runs truncate the scene list before any
    # video generation (scene_planner.order_and_filter_scenes /
    # visual_package_director both honor EPISODE_ZERO_MAX_SCENES). The paid
    # video cost is bounded by that cap, not the raw beat count -- reflect
    # it here so the PRE_PLAN model is not needlessly infeasible for a
    # cheap ~45-60s Short.
    _scene_cap = int(getattr(config, "EPISODE_ZERO_MAX_SCENES", 0) or 0)
    if _scene_cap > 0 and n_scenes is not None:
        n_scenes = min(n_scenes, _scene_cap)

    components: list[PreplanComponent] = []

    def _openrouter(component_id, stage, model, expected_calls, maximum_calls, bound_source, conditional=False):
        b = _find_binding(bindings, "openrouter", model, "text.completion")
        mcpc = _decimal_or_none(getattr(b, "max_cost_per_call", None)) if b is not None else None
        components.append(PreplanComponent(
            component_id=component_id, stage=stage, provider="openrouter", model=model,
            provider_operation="text.completion",
            provider_domain=_domain("openrouter", "text.completion"),
            max_cost_per_call=mcpc,
            expected_call_count=expected_calls,
            maximum_call_count=maximum_calls,
            chargeable_attempts_per_call=_OPENROUTER_CHARGEABLE_PER_CALL,
            retry_allowance=_OPENROUTER_RETRY_MAX,
            fallback_allowance=_OPENROUTER_MODEL_FALLBACK,
            conditional=conditional,
            bound_source=bound_source,
            binding_present=b is not None,
            binding_max_total_spend=_decimal_or_none(getattr(b, "max_total_spend", None)) if b is not None else None,
        ))

    # -- executive_producer: 2 openrouter calls (scorecard always; brief on PASS/WARN)
    ep_model = _first_model(bindings, "openrouter", "text.completion", default="anthropic/claude-sonnet-4.6")
    _openrouter(
        "executive_producer_text", "executive_producer", ep_model,
        expected_calls=_EP_CALLS, maximum_calls=_EP_CALLS,
        bound_source="hypogeum.agents.executive_producer: exactly 2 run_agent() calls "
                     "(validation_scorecard always; topic_brief on PASS/WARN)",
        conditional=True,
    )

    # -- research_director: 4 fixed Perplexity queries + 1 openrouter synthesis
    pb = _find_binding(bindings, "perplexity", _first_model(bindings, "perplexity", "research.query", "sonar-pro"),
                       "research.query")
    p_mcpc = _decimal_or_none(getattr(pb, "max_cost_per_call", None)) if pb is not None else None
    components.append(PreplanComponent(
        component_id="research_director_perplexity", stage="research_director",
        provider="perplexity",
        model=_first_model(bindings, "perplexity", "research.query", "sonar-pro"),
        provider_operation="research.query",
        provider_domain=_domain("perplexity", "research.query"),
        max_cost_per_call=p_mcpc,
        expected_call_count=_RD_RESEARCH_PHASE_COUNT,
        maximum_call_count=_RD_RESEARCH_PHASE_COUNT,
        chargeable_attempts_per_call=_PERPLEXITY_CHARGEABLE_PER_CALL,
        retry_allowance=_PERPLEXITY_RETRY_MAX, fallback_allowance=0, conditional=False,
        bound_source="hypogeum.agents.research_director._RESEARCH_PHASES: exactly 4 fixed queries; "
                     "hypogeum.perplexity retry loop range(RETRY_MAX+1) = 2 chargeable attempts",
        binding_present=pb is not None,
        binding_max_total_spend=_decimal_or_none(getattr(pb, "max_total_spend", None)) if pb is not None else None,
    ))
    rd_synth_model = _first_model(bindings, "openrouter", "text.completion", "anthropic/claude-opus-4.6",
                                  prefer_substr="opus")
    _openrouter(
        "research_director_synthesis", "research_director", rd_synth_model,
        expected_calls=1, maximum_calls=1,
        bound_source="hypogeum.agents.research_director: exactly 1 run_agent() synthesis call",
    )

    # -- narrative_architect: 1 openrouter call
    na_model = _first_model(bindings, "openrouter", "text.completion", "anthropic/claude-opus-4.6", prefer_substr="opus")
    _openrouter("narrative_architect_text", "narrative_architect", na_model,
                expected_calls=1, maximum_calls=1,
                bound_source="hypogeum.agents.narrative_architect: exactly 1 run_agent() call")

    # -- script_engine: 1 openrouter call
    se_model = _first_model(bindings, "openrouter", "text.completion", "anthropic/claude-sonnet-4.6")
    _openrouter("script_engine_text", "script_engine", se_model,
                expected_calls=1, maximum_calls=1,
                bound_source="hypogeum.agents.script_engine: exactly 1 run_agent() call")

    # -- thumbnail_strategy: 1 + up to _MAX_HOOK_TEXT_REPAIR_ATTEMPTS
    ts_model = _first_model(bindings, "openrouter", "text.completion", "anthropic/claude-sonnet-4.6")
    _openrouter("thumbnail_strategy_text", "thumbnail_strategy", ts_model,
                expected_calls=1, maximum_calls=1 + _THUMBNAIL_HOOK_REPAIR_MAX,
                bound_source="hypogeum.agents.thumbnail_strategy: 1 run_agent() + "
                             f"_MAX_HOOK_TEXT_REPAIR_ATTEMPTS({_THUMBNAIL_HOOK_REPAIR_MAX}) regeneration attempts",
                conditional=True)

    # -- audio_agent: narration (PRE_PLAN); music is POST_PLAN (Assembly)
    nb = _find_binding(bindings, "elevenlabs",
                       _first_model(bindings, "elevenlabs", "voice.narration_segment", "eleven_multilingual_v2"),
                       "voice.narration_segment")
    n_mcpc = _decimal_or_none(getattr(nb, "max_cost_per_call", None)) if nb is not None else None
    components.append(PreplanComponent(
        component_id="audio_agent_narration", stage="audio_agent",
        provider="elevenlabs",
        model=_first_model(bindings, "elevenlabs", "voice.narration_segment", "eleven_multilingual_v2"),
        provider_operation="voice.narration_segment",
        provider_domain=_domain("elevenlabs", "voice.narration_segment"),
        max_cost_per_call=n_mcpc,
        expected_call_count=(n_segments or 0),
        maximum_call_count=n_segments,
        chargeable_attempts_per_call=_ELEVENLABS_CHARGEABLE_PER_CALL,
        retry_allowance=_ELEVENLABS_RETRY_MAX, fallback_allowance=0, conditional=False,
        bound_source=(
            "narration_package.narration_segments count (Audio Agent already ran by plan-compile time); "
            "hypogeum.elevenlabs._RETRY_MAX+1 = 2 chargeable attempts"
            if narration_package else
            "script_package beat count (main + shorts); narration_package not yet available"
        ),
        binding_present=nb is not None,
        binding_max_total_spend=_decimal_or_none(getattr(nb, "max_total_spend", None)) if nb is not None else None,
    ))

    # -- visual_package_director Phase 1: batched Claude planning
    vpd_model = _first_model(bindings, "openrouter", "text.completion", "anthropic/claude-sonnet-4.6")
    for stage_id, base_batch, outer in _VPD_P1_STAGES:
        if n_scenes is None:
            max_calls = None
            exp_calls = 0
            bs = ("visual_package_director Phase-1 batched planning: NO script/narration available to "
                  "size the scene list -- UNBOUNDED")
        else:
            max_calls = _batched_max_calls(n_scenes, base_batch, outer)
            # expected: one pass, no halving, no outer retry
            exp_calls = (1 if base_batch is None else math.ceil(n_scenes / base_batch))
            bs = (f"visual_package_director Phase-1 stage {stage_id}: "
                  f"batch base {base_batch}, halving to 1, outer_attempts {outer}, "
                  f"scene count {n_scenes} (= narrative beat count)")
        _openrouter(f"vpd_phase1_{stage_id}", "visual_package_director", vpd_model,
                    expected_calls=exp_calls, maximum_calls=max_calls, bound_source=bs)

    # ---- aggregate ----
    unbounded = tuple(c.component_id for c in components if not c.is_bounded)
    coverage_complete = not unbounded

    expected_total = sum((c.expected_cost for c in components), Decimal("0")).quantize(Decimal("0.000001"))
    maximum_total = sum((c.maximum_cost for c in components), Decimal("0")).quantize(Decimal("0.000001"))

    # per-binding allocation diagnostic (does the signed grant provision
    # enough headroom for PRE_PLAN on each binding it shares?)
    alloc: dict[str, dict] = {}
    for c in components:
        key = f"{c.provider}:{c.model}:{c.provider_operation}"
        a = alloc.setdefault(key, {
            "binding_key": key,
            "binding_present": c.binding_present,
            "binding_max_total_spend": (str(c.binding_max_total_spend) if c.binding_max_total_spend is not None else None),
            "preplan_maximum_cost": Decimal("0"),
            "components": [],
        })
        a["preplan_maximum_cost"] += c.maximum_cost
        a["components"].append(c.component_id)
    binding_allocation = []
    policy_capped_total = Decimal("0")
    for a in alloc.values():
        over = (
            a["binding_max_total_spend"] is not None
            and a["preplan_maximum_cost"] > Decimal(a["binding_max_total_spend"])
        )
        if a["binding_max_total_spend"] is not None:
            capped = min(a["preplan_maximum_cost"], Decimal(a["binding_max_total_spend"]))
        else:
            capped = a["preplan_maximum_cost"]
        policy_capped_total += capped
        binding_allocation.append({
            "binding_key": a["binding_key"],
            "binding_present": a["binding_present"],
            "binding_max_total_spend": a["binding_max_total_spend"],
            "preplan_maximum_cost": str(a["preplan_maximum_cost"].quantize(Decimal("0.000001"))),
            "preplan_policy_capped_cost": str(capped.quantize(Decimal("0.000001"))),
            "preplan_exceeds_binding_ceiling": bool(over),
            "components": a["components"],
        })

    model = PreplanCostModel(
        preplan_expected_cost=expected_total,
        preplan_maximum_bounded_cost=maximum_total,
        preplan_policy_capped_maximum_cost=policy_capped_total.quantize(Decimal("0.000001")),
        preplan_required_reservation=policy_capped_total.quantize(Decimal("0.000001")),
        preplan_component_breakdown=tuple(c.to_dict() for c in components),
        unbounded_components=unbounded,
        coverage_complete=coverage_complete,
        currency="USD",
        binding_allocation=tuple(binding_allocation),
    )

    if require_feasible and not coverage_complete:
        raise PreplanModelInfeasibleError(
            unbounded,
            detail="supply the signed grant bindings and the script/narration packages, "
                   "or add a finite structural cap to the offending stage",
        )
    return model


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _domain(provider: str, operation: str) -> str:
    try:
        return resolve_provider_domain(provider, operation)
    except UnknownProviderOperationError as exc:  # pragma: no cover - defensive
        raise PreplanCostModelError(str(exc)) from exc


def _first_model(bindings: Any, provider: str, provider_operation: str, default: str,
                 *, prefer_substr: Optional[str] = None) -> str:
    """Pick the bound model for a (provider, operation). If several are
    bound, prefer one whose name contains `prefer_substr`; else the
    cheapest by max_cost_per_call; else `default`."""
    matches = [
        b for b in _iter_bindings(bindings)
        if getattr(b, "provider", None) == provider
        and getattr(b, "provider_operation", None) == provider_operation
    ]
    if not matches:
        return default
    if prefer_substr:
        for b in matches:
            if prefer_substr in (getattr(b, "model", "") or ""):
                return b.model
    matches.sort(key=lambda b: _decimal_or_none(getattr(b, "max_cost_per_call", None)) or Decimal("0"))
    return matches[0].model


def _validate_workflow_order(workflow_definition: dict) -> None:
    order = list(workflow_definition.get("stage_order", []))
    if _PLAN_COMPILE_STAGE not in order:
        raise PreplanCostModelError(
            f"workflow stage_order has no {_PLAN_COMPILE_STAGE!r} stage -- cannot locate the PRE_PLAN boundary"
        )
    compile_idx = order.index(_PLAN_COMPILE_STAGE)
    preplan_in_order = [s for s in PRE_PLAN_STAGES if s != _PLAN_COMPILE_STAGE]
    seen_positions = []
    for s in preplan_in_order:
        if s not in order:
            raise PreplanCostModelError(
                f"workflow stage_order is missing expected PRE_PLAN stage {s!r}"
            )
        idx = order.index(s)
        if idx >= compile_idx:
            raise PreplanCostModelError(
                f"workflow stage {s!r} is not before {_PLAN_COMPILE_STAGE!r} -- PRE_PLAN boundary moved"
            )
        seen_positions.append(idx)
    if seen_positions != sorted(seen_positions):
        raise PreplanCostModelError(
            "workflow PRE_PLAN stage order differs from the expected frozen order"
        )
