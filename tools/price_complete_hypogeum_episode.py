#!/usr/bin/env python3
"""
tools/price_complete_hypogeum_episode.py — operator decision evidence.

Prices a COMPLETE Hypogeum episode from the REAL deliverable contract
(channel_packages/hypogeum/deliverable_contract_v1.json) and the REAL
configured provider prices. Reads a structurally complete existing
episode's script / narration / visual-package artifacts (default:
ep_f3619022 -- 29 narrative beats, ~16.6 min, 3 derived shorts) so no
provider is called and no database is touched.

Prints:
  - the required-asset inventory (derived from the contract + script,
    NOT from any budget figure);
  - expected + maximum-bounded POST_PLAN cost with the component
    breakdown at unit price x count;
  - the six-quantity, single-count episode cost model vs. the CURRENT
    operator-configured weekly limit (read-only);
  - coverage-safe Channel Package variants (tier / enhancement) -- NOT
    applied, just priced.

This tool NEVER writes: no grant, no reservation, no config change.

Usage:
    python3 tools/price_complete_hypogeum_episode.py
    python3 tools/price_complete_hypogeum_episode.py --episode ep_af078f34
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hypogeum.channel_budget_policy import build_required_asset_inventory  # noqa: E402
from hypogeum.config import Config  # noqa: E402
from hypogeum.production_plan import (  # noqa: E402
    ROLE_CONTINGENCY, ROLE_REQUIRED, compile_production_plan, load_deliverable_contract,
    validate_plan_coverage,
)

# The real production configuration (from .env). Pinned so this tool is
# reproducible regardless of the shell environment.
_REAL_PROD = {
    "MUAPI_DEFAULT_TIER": 2,
    "MUAPI_T2_MODEL": "veo3.1-lite-text-to-video",
    "MUAPI_T3_MODEL": "veo3.1-lite-text-to-video",
    "MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE": 1,
    "THUMBNAIL_IMAGE_MODEL": "gpt-image-2-text-to-image",
    "THUMBNAIL_SHORT_CANDIDATE_CAP": 3,
    "ELEVENLABS_MUSIC_DEFAULT_DURATION": 120,
}

# Observed PRE_PLAN (text/research/planning) cost, from the costs ledger.
PRE_PLAN_EXPECTED_USD = Decimal("4.73")   # preplan_core ~$1.7 + VPD phase-1 ~$3.0 + $0.05
PRE_PLAN_MAXIMUM_USD = Decimal("9.40")    # RETRY_MAX=1 on all PRE_PLAN calls


class _Shim:
    def __init__(self, base, ov):
        self._b, self._o = base, ov

    def __getattr__(self, n):
        return self._o[n] if n in self._o else getattr(self._b, n)


def _cfg(**extra):
    ov = dict(_REAL_PROD)
    ov.update(extra)
    return _Shim(Config(), ov)


def _load(ep_dir: Path, name: str):
    p = ep_dir / f"{name}_{ep_dir.name}.json"
    if not p.exists():
        sys.exit(f"missing artifact: {p}")
    return json.loads(p.read_text())


def _price(cfg, sp, nar, vp, label):
    from hypogeum.channel_budget_policy import coverage_required_video_cost
    plan = compile_production_plan(
        script_package=sp, narration_package=nar, visual_package=vp,
        channel_id="hypogeum", episode_id="pricing_probe", config=cfg,
    )
    cov = validate_plan_coverage(plan, nar)
    # CORRECTED classification: a "shot" is coverage-REQUIRED (belongs in
    # expected) when it is needed to fill its beat's narration duration
    # under the <=1.15x editorial-speed cap. Only shots beyond that
    # per-beat minimum are TRUE contingency.
    req_video, req_shots, cont_shots = coverage_required_video_cost(plan, nar)
    max_video = sum(Decimal(str(a.unit_price_usd)) for a in plan.assets)
    enh_per_op = Decimal(str(plan.enhancement_cost_maximum_usd)) / Decimal(len(plan.assets))
    enh_exp = Decimal(req_shots) * enh_per_op
    enh_max = Decimal(str(plan.enhancement_cost_maximum_usd))
    music = Decimal(str(plan.music_cost_usd))          # FRESH -- cache unverified
    narr = Decimal(str(plan.narration_cost_usd))
    th_exp = Decimal(str(plan.thumbnail_cost_normal_usd))
    th_max = Decimal(str(plan.thumbnail_cost_maximum_usd))
    plan_est = Decimal(str(plan.planning_call_estimate_usd))
    exp = req_video + enh_exp + narr + music + th_exp + plan_est
    mx = max_video + enh_max + narr + music + th_max + plan_est
    print(f"\n{label}")
    print(f"  video shots      : {req_shots} coverage-required + {cont_shots} true contingency = {len(plan.assets)}"
          f"   models {dict(Counter(a.model for a in plan.assets))}")
    print(f"  video (required) : ${req_video:.2f}   video (all planned): ${max_video:.2f}")
    print(f"  enhancement      : expected ${enh_exp:.2f} ({req_shots} ops)   max ${enh_max:.2f} ({len(plan.assets)} ops)")
    print(f"  narration        : ${narr:.3f}  ({plan.narration_char_count} chars x $0.0003)")
    print(f"  music            : ${music:.2f}  FRESH ({plan.music_section_count} sections x 120s x $0.02) "
          f"-- cache credited only when verified before reservation")
    print(f"  thumbnail        : expected ${th_exp:.2f}   max ${th_max:.2f}")
    print(f"  planning estimate: ${plan_est:.2f}")
    print(f"  >>> POST_PLAN expected   ${exp}")
    print(f"  >>> POST_PLAN maximum    ${mx}")
    print(f"  >>> COMPLETE expected    ${PRE_PLAN_EXPECTED_USD + exp}   (PRE_PLAN ${PRE_PLAN_EXPECTED_USD} + POST_PLAN)")
    print(f"  >>> COMPLETE maximum     ${PRE_PLAN_MAXIMUM_USD + mx}   (PRE_PLAN ${PRE_PLAN_MAXIMUM_USD} + POST_PLAN)")
    print(f"  coverage sufficient: {cov.sufficient}")
    return plan


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", default="ep_f3619022", help="episode id whose artifacts to price")
    args = ap.parse_args(argv)

    ep_dir = _ROOT / ".hypogeum-assets" / "episodes" / args.episode
    if not ep_dir.exists():
        return int(bool(print(f"no such episode dir: {ep_dir}")))
    sp = _load(ep_dir, "script_package")
    nar = _load(ep_dir, "narration_package")
    vp = _load(ep_dir, "visual_package")

    contract = load_deliverable_contract("hypogeum")
    req_deliv = [d for d in contract["deliverables"] if d.get("required")]
    print("=" * 78)
    print(f"TRUTHFUL COMPLETE HYPOGEUM EPISODE PRICE  (source structure: {args.episode})")
    print("=" * 78)
    print(f"Deliverable contract: {contract['deliverable_contract_id']} v{contract['contract_version']}")
    print(f"  required deliverables: {[d['deliverable_id'] for d in req_deliv]}")
    print(f"  allow_standalone_shorts: {contract['allow_standalone_shorts']}")

    base = _price(_cfg(), sp, nar, vp, "== BASELINE (real config, full quality) ==")
    inv = build_required_asset_inventory(base, script_package=sp, contract=contract)
    print("\n-- required-asset inventory (derived from contract + script, NOT from budget) --")
    for k, v in inv.items():
        if k == "component_costs_usd":
            continue
        print(f"   {k}: {v}")

    # coverage-safe variants
    vp_t2 = copy.deepcopy(vp)
    for s in vp_t2["scenes"]:
        r = s.setdefault("production_spec", {}).setdefault("provider_routing", {})
        if r.get("primary_tier") == 1:
            r["primary_tier"] = 2
    _price(_cfg(), sp, nar, vp_t2, "== VARIANT: cheaper approved tier (tier1 -> tier2) ==")
    _price(_cfg(SOURCE_ENHANCEMENT_ENABLED=False), sp, nar, vp,
           "== VARIANT: enhancement-only-when-required (0 scenes flagged -> off) ==")
    _price(_cfg(SOURCE_ENHANCEMENT_ENABLED=False), sp, nar, vp_t2,
           "== VARIANT: combined economical (tier2 + enhancement off) ==")

    # reduced contingency -- documented as NOT coverage-safe for Hypogeum
    vp_rc = copy.deepcopy(vp)
    for s in vp_rc["scenes"]:
        cp = s.get("cinematic_coverage_plan") or {}
        shots = sorted(cp.get("shots", []), key=lambda x: x.get("shot_order", 0))
        prim = [x for x in shots if x.get("shot_order", 1) <= 1]
        extra = [x for x in shots if x.get("shot_order", 1) > 1][:1]
        if cp:
            cp["shots"] = prim + extra
            s["cinematic_coverage_plan"] = cp
    plan_rc = compile_production_plan(
        script_package=sp, narration_package=nar, visual_package=vp_rc,
        channel_id="hypogeum", episode_id="probe", config=_cfg(),
    )
    ok = validate_plan_coverage(plan_rc, nar).sufficient
    print(f"\n== VARIANT: reduced contingency (<=1 per scene) -> coverage_sufficient={ok} "
          f"(NOT a valid complete episode for Hypogeum) ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
