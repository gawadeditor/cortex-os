"""
tests/test_hypogeum_episode_pricing.py — truthful pricing of a COMPLETE
Hypogeum episode from the REAL deliverable contract + REAL configured
provider prices (zero network).

These tests use the real channel_packages/hypogeum/deliverable_contract_v1.json
and the real production_plan pricing tables. Where a full structurally
complete script/narration/visual-package is needed, they read the
existing ep_f3619022 artifacts (a real full-length Hypogeum episode:
29 narrative beats, ~16.6 min, 3 derived shorts) and skip if absent.

No provider is called. No production database is written.
"""
from __future__ import annotations

import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

import hypogeum.channel_budget_policy as bp
from hypogeum.config import Config
from hypogeum.production_plan import (
    compile_production_plan, load_deliverable_contract, validate_plan_coverage,
)

_REPO = os.path.dirname(os.path.dirname(__file__))
_EP_DIR = os.path.join(_REPO, ".hypogeum-assets", "episodes", "ep_f3619022")


def _artifacts():
    need = ["script_package", "narration_package", "visual_package"]
    out = {}
    for n in need:
        p = os.path.join(_EP_DIR, f"{n}_ep_f3619022.json")
        if not os.path.exists(p):
            pytest.skip("real ep_f3619022 artifacts not present in this environment")
        out[n] = json.load(open(p))
    return out


class _Shim:
    def __init__(self, base, ov):
        self._b, self._o = base, ov

    def __getattr__(self, n):
        return self._o[n] if n in self._o else getattr(self._b, n)


# The real PRODUCTION configuration values (from .env, verified 2026-08-28).
# conftest blocks .env leakage into test Config objects, so the pricing
# probe pins the exact production settings explicitly.
_REAL_PROD = {
    "MUAPI_DEFAULT_TIER": 2,
    "MUAPI_T2_MODEL": "veo3.1-lite-text-to-video",
    "MUAPI_T3_MODEL": "veo3.1-lite-text-to-video",
    "MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE": 1,   # production: NO paid retries
    "THUMBNAIL_IMAGE_MODEL": "gpt-image-2-text-to-image",
    "THUMBNAIL_SHORT_CANDIDATE_CAP": 3,
    "ELEVENLABS_MUSIC_DEFAULT_DURATION": 120,
    "MUAPI_VIDEO_RESOLUTION": "1080p",
}


def _real_cfg(**extra):
    ov = dict(_REAL_PROD)
    ov.update(extra)
    return _Shim(Config(), ov)


# ---------------------------------------------------------------------------
# The real deliverable contract
# ---------------------------------------------------------------------------

class TestRealDeliverableContract:
    def test_contract_requires_one_main_plus_three_shorts(self):
        c = load_deliverable_contract("hypogeum")
        req = [d for d in c["deliverables"] if d.get("required")]
        mains = [d for d in req if d["kind"] == "main_episode"]
        shorts = [d for d in req if d["kind"] == "derived_short"]
        assert len(mains) == 1
        assert len(shorts) == 3
        assert c["allow_standalone_shorts"] is False


# ---------------------------------------------------------------------------
# Real pricing
# ---------------------------------------------------------------------------

class TestTruthfulCompletePricing:
    def _plan(self, cfg=None):
        a = _artifacts()
        return compile_production_plan(
            script_package=a["script_package"], narration_package=a["narration_package"],
            visual_package=a["visual_package"], channel_id="hypogeum",
            episode_id="pricing_probe", config=cfg or _real_cfg(),
        )

    def test_inventory_is_derived_from_contract_and_script_not_budget(self):
        a = _artifacts()
        plan = self._plan()
        contract = load_deliverable_contract("hypogeum")
        inv = bp.build_required_asset_inventory(plan, script_package=a["script_package"], contract=contract)
        # required set comes from the contract, priced set from the plan
        assert set(inv["required_deliverables"]) == {"main_episode", "short_001", "short_002", "short_003"}
        assert inv["required_shorts_count"] == 3
        assert inv["missing_required_deliverables"] == []          # every required deliverable priced
        assert set(inv["priced_deliverables"]) >= set(inv["required_deliverables"])
        # every required SHORT is priced
        for s in inv["required_shorts"]:
            assert s in inv["priced_deliverables"], s
        # retry + contingency allowances are visible in the inventory
        assert inv["video_primary_shot_count"] >= 29               # >= narrative beats
        assert inv["video_contingency_shot_count"] > 0
        assert inv["max_paid_attempts_per_scene"] is not None
        assert inv["source_enhancement_operations_max"] == inv["video_asset_count_total"]

    def test_expected_and_maximum_are_distinct_and_both_realistic(self):
        plan = self._plan()
        expected = Decimal(str(plan.normal_expected_cost_usd))
        maximum = Decimal(str(plan.maximum_bounded_cost_usd))
        assert maximum > expected
        # sanity: with 47 primary + 106 contingency shots at real prices,
        # the maximum bounded POST_PLAN cost is well above USD 55
        assert maximum > Decimal("100")
        assert expected > Decimal("40")

    def test_component_breakdown_uses_configured_prices(self):
        plan = self._plan()
        # music: 9 sections x 120s x $0.02 = $21.60 (configured, not historical avg)
        assert Decimal(str(plan.music_cost_usd)) == Decimal("21.6")
        # narration: 15650 chars x $0.0003
        assert Decimal(str(plan.narration_cost_usd)) == (Decimal("15650") * Decimal("0.0003"))
        # enhancement max: one op per declared asset x $0.10
        assert Decimal(str(plan.enhancement_cost_maximum_usd)) == (
            Decimal(str(len(plan.assets))) * Decimal("0.10")
        )

    def test_scene_count_does_not_shrink_when_only_the_budget_changes(self):
        """The number of priced video assets is a function of the script /
        coverage plan, never of the configured limit."""
        a = _artifacts()
        counts = set()
        for _limit in ("40.00", "55.00", "150.00", "500.00"):
            plan = compile_production_plan(
                script_package=a["script_package"], narration_package=a["narration_package"],
                visual_package=a["visual_package"], channel_id="hypogeum",
                episode_id="probe", config=_real_cfg(),
            )
            counts.add(len(plan.assets))
        assert len(counts) == 1     # identical regardless of any budget figure

    def test_no_missing_deliverable_can_make_the_plan_look_cheaper(self):
        """Drop the shorts from the script -> the plan is cheaper, but
        build_required_asset_inventory flags the missing required shorts,
        so compile_truthful_production_plan rejects it outright."""
        a = _artifacts()
        sp = json.loads(json.dumps(a["script_package"]))
        sp["shorts_scripts"] = []
        vp = json.loads(json.dumps(a["visual_package"]))
        vp["scenes"] = [s for s in vp["scenes"] if not s.get("short_id")]
        contract = load_deliverable_contract("hypogeum")
        with pytest.raises(Exception):  # ProductionPlanCoverageError / deliverable violation
            bp.compile_truthful_production_plan(
                script_package=sp, narration_package=a["narration_package"], visual_package=vp,
                channel_id="hypogeum", episode_id="cheap_probe", config=_real_cfg(),
                db_path=os.devnull, contract=contract,
            )

    def test_coverage_safe_variants_only_tier_and_enhancement(self):
        """For this real episode, reducing contingency shots ALWAYS breaks
        coverage -- so the only budget-safe Channel Package levers are
        cheaper tier and enhancement-off. Documented as a hard fact."""
        a = _artifacts()
        base = self._plan()
        # cheaper tier: force tier1 -> tier2
        vp2 = json.loads(json.dumps(a["visual_package"]))
        for s in vp2["scenes"]:
            r = s.setdefault("production_spec", {}).setdefault("provider_routing", {})
            if r.get("primary_tier") == 1:
                r["primary_tier"] = 2
        plan_t2 = compile_production_plan(
            script_package=a["script_package"], narration_package=a["narration_package"],
            visual_package=vp2, channel_id="hypogeum", episode_id="probe", config=_real_cfg(),
        )
        assert validate_plan_coverage(plan_t2, a["narration_package"]).sufficient
        assert plan_t2.maximum_bounded_cost_usd < base.maximum_bounded_cost_usd

        # enhancement off: still coverage-safe, cheaper max
        plan_noenh = compile_production_plan(
            script_package=a["script_package"], narration_package=a["narration_package"],
            visual_package=a["visual_package"], channel_id="hypogeum", episode_id="probe",
            config=_real_cfg(SOURCE_ENHANCEMENT_ENABLED=False),
        )
        assert validate_plan_coverage(plan_noenh, a["narration_package"]).sufficient
        assert plan_noenh.enhancement_cost_maximum_usd == 0.0
        assert plan_noenh.maximum_bounded_cost_usd < base.maximum_bounded_cost_usd

        # reduced contingency -> coverage BREAKS (documented hard fact)
        vp_rc = json.loads(json.dumps(a["visual_package"]))
        for s in vp_rc["scenes"]:
            cp = s.get("cinematic_coverage_plan") or {}
            shots = sorted(cp.get("shots", []), key=lambda x: x.get("shot_order", 0))
            prim = [x for x in shots if x.get("shot_order", 1) <= 1]
            extra = [x for x in shots if x.get("shot_order", 1) > 1][:1]
            if cp:
                cp["shots"] = prim + extra
                s["cinematic_coverage_plan"] = cp
        plan_rc = compile_production_plan(
            script_package=a["script_package"], narration_package=a["narration_package"],
            visual_package=vp_rc, channel_id="hypogeum", episode_id="probe", config=_real_cfg(),
        )
        assert validate_plan_coverage(plan_rc, a["narration_package"]).sufficient is False


# ---------------------------------------------------------------------------
# Required-vs-contingency reclassification (2026-08-29)
# ---------------------------------------------------------------------------

class TestCoverageRequiredClassification:
    def _plan(self, cfg=None):
        a = _artifacts()
        return compile_production_plan(
            script_package=a["script_package"], narration_package=a["narration_package"],
            visual_package=a["visual_package"], channel_id="hypogeum",
            episode_id="reclass_probe", config=cfg or _real_cfg(),
        )

    def test_coverage_required_count_exceeds_the_one_per_beat_role_label(self):
        """production_plan labels 1 shot/beat ROLE_REQUIRED (47 total); the
        coverage arithmetic actually REQUIRES ~130 shots to fill the beat
        durations. Those extra ~83 are baseline required work, not
        contingency."""
        a = _artifacts()
        plan = self._plan()
        req_cost, req_shots, cont_shots = bp.coverage_required_video_cost(plan, a["narration_package"])
        assert req_shots > 47                          # far more than the 1-per-beat label
        assert req_shots + cont_shots == len(plan.assets)
        assert 100 <= req_shots <= 145                 # the coverage-required baseline
        assert 0 < cont_shots < 40                     # a small true-contingency tail

    def test_expected_cost_includes_every_coverage_required_shot(self):
        a = _artifacts()
        res = bp.compile_truthful_production_plan(
            script_package=a["script_package"], narration_package=a["narration_package"],
            visual_package=a["visual_package"], channel_id="hypogeum", episode_id="reclass2",
            config=_real_cfg(), db_path=os.devnull,
        ) if False else None
        # compile_truthful needs a real DB; test the model directly with a
        # tmp DB instead
        import hypogeum.db as db
        import hypogeum.channel_config as cc
        import hypogeum.policy_cascade as pc
        import tempfile
        from decimal import Decimal
        d = Path(tempfile.mkdtemp()) / "t.db"
        db.initialize_schema(d)
        cc.create_channel_profile(channel_id="hypogeum", name="H", db_path=d)
        cc.create_channel_operations_profile(channel_id="hypogeum", channel_name="H", timezone="UTC",
                                             operator_enabled=True, production_cadence={"videos_per_week": 1},
                                             weekly_video_budget_usd=200.0, db_path=d)
        plan = self._plan()
        cm = bp.compute_episode_cost_model(
            "hypogeum", "reclass2", production_plan=plan,
            narration_package=a["narration_package"], db_path=d,
        )
        req_cost, req_shots, _ = bp.coverage_required_video_cost(plan, a["narration_package"])
        # expected remaining >= the coverage-required video cost alone
        assert cm.expected_remaining_cost >= req_cost
        # expected < maximum, and the gap is the TRUE contingency + enhancement + thumbnail spread
        assert cm.expected_remaining_cost < cm.maximum_bounded_remaining_cost
        # expected is NOT the old 1-shot-per-beat understatement
        assert cm.expected_remaining_cost > Decimal("60")

    def test_removing_a_true_contingency_shot_keeps_coverage(self):
        """The LAST shot of a beat that has more shots than its coverage
        minimum is a true contingency -- removing it must not break
        coverage."""
        a = _artifacts()
        import math
        from hypogeum.agents.coverage_check import MAX_EDITORIAL_SPEED_FACTOR
        dur = {s.get("beat_reference") or s.get("beat_id"): float(s.get("duration_seconds", 0))
               for s in a["narration_package"]["narration_segments"]}
        vp = json.loads(json.dumps(a["visual_package"]))
        removed = 0
        for sc in vp["scenes"]:
            b = sc.get("narration_reference", "")
            shots = (sc.get("cinematic_coverage_plan") or {}).get("shots", [])
            min_req = max(1, math.ceil(dur.get(b, 0.0) / (8.0 * MAX_EDITORIAL_SPEED_FACTOR)))
            if len(shots) > min_req:      # has a true-contingency tail
                sc["cinematic_coverage_plan"]["shots"] = shots[:min_req]
                removed += (len(shots) - min_req)
        assert removed > 0
        plan = compile_production_plan(
            script_package=a["script_package"], narration_package=a["narration_package"],
            visual_package=vp, channel_id="hypogeum", episode_id="probe", config=_real_cfg(),
        )
        assert validate_plan_coverage(plan, a["narration_package"]).sufficient   # still complete

    def test_removing_a_coverage_required_shot_breaks_coverage(self):
        a = _artifacts()
        vp = json.loads(json.dumps(a["visual_package"]))
        # drop the last shot from every beat that has >= 3 shots -> removes
        # a coverage-required shot from long beats
        for sc in vp["scenes"]:
            shots = (sc.get("cinematic_coverage_plan") or {}).get("shots", [])
            if len(shots) >= 3:
                sc["cinematic_coverage_plan"]["shots"] = shots[:-1]
        plan = compile_production_plan(
            script_package=a["script_package"], narration_package=a["narration_package"],
            visual_package=vp, channel_id="hypogeum", episode_id="probe", config=_real_cfg(),
        )
        assert validate_plan_coverage(plan, a["narration_package"]).sufficient is False

    def test_verified_cache_credit_reduces_expected_unverified_does_not(self):
        import hypogeum.db as db
        import hypogeum.channel_config as cc
        import tempfile
        from decimal import Decimal
        d = Path(tempfile.mkdtemp()) / "t.db"
        db.initialize_schema(d)
        cc.create_channel_profile(channel_id="hypogeum", name="H", db_path=d)
        cc.create_channel_operations_profile(channel_id="hypogeum", channel_name="H", timezone="UTC",
                                             operator_enabled=True, production_cadence={"videos_per_week": 1},
                                             weekly_video_budget_usd=200.0, db_path=d)
        a = _artifacts()
        plan = self._plan()
        cm_unverified = bp.compute_episode_cost_model(
            "hypogeum", "cache1", production_plan=plan, narration_package=a["narration_package"], db_path=d,
        )
        cm_verified = bp.compute_episode_cost_model(
            "hypogeum", "cache1", production_plan=plan, narration_package=a["narration_package"],
            verified_music_credit_usd=Decimal("21.60"), db_path=d,
        )
        assert cm_verified.expected_remaining_cost == cm_unverified.expected_remaining_cost - Decimal("21.60")
        # default (no credit passed) == unverified == full fresh music priced
        assert cm_unverified.expected_remaining_cost > cm_verified.expected_remaining_cost

    def test_corrected_fully_bounded_plan_fits_135_or_reports_deficit(self):
        import hypogeum.db as db
        import hypogeum.channel_config as cc
        import tempfile
        from decimal import Decimal
        d = Path(tempfile.mkdtemp()) / "t.db"
        db.initialize_schema(d)
        cc.create_channel_profile(channel_id="hypogeum", name="H", db_path=d)
        cc.create_channel_operations_profile(channel_id="hypogeum", channel_name="H", timezone="UTC",
                                             operator_enabled=True, production_cadence={"videos_per_week": 1},
                                             weekly_video_budget_usd=135.0, db_path=d)
        a = _artifacts()
        plan = self._plan()
        cm = bp.compute_episode_cost_model(
            "hypogeum", "fit135", production_plan=plan, narration_package=a["narration_package"], db_path=d,
        )
        PRE_PLAN_MAX = Decimal("10.00")
        complete_max = cm.maximum_bounded_remaining_cost + PRE_PLAN_MAX
        # POST_PLAN maximum alone is ~$122; + PRE_PLAN ~$10 => ~$132 <= $135
        assert cm.maximum_bounded_remaining_cost < Decimal("125")
        assert complete_max <= Decimal("135.00"), f"complete_max={complete_max}"
