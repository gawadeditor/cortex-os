"""
tests/test_preplan_cost_model.py -- the authoritative PRE_PLAN cost model
(hypogeum.preplan_cost_model) and the authoritative complete-episode
arithmetic (hypogeum.channel_budget_policy.compute_complete_episode_cost).

Every bound here comes from an authoritative source: the compiled
workflow definition, the signed grant bindings, provider retry constants,
and structural limits (research-phase count, hook-repair cap, real
narration-segment count, script beat count, VPD Phase-1 batch sizes).
No historical average is asserted as a maximum. The legacy USD 4.73 /
USD 9.40 numbers appear ONLY as clearly-labelled non-authoritative
historical comparison data.
"""
from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

import hypogeum.channel_budget_policy as bp
import hypogeum.channel_config as cc
import hypogeum.db as db
from hypogeum.authorization_store import GrantBinding
from hypogeum.preplan_cost_model import (
    PreplanModelInfeasibleError,
    compute_preplan_cost_model,
)

# ---- non-authoritative historical comparison only (never a proof) --------
_HISTORICAL_PREPLAN_EXPECTED_ESTIMATE_USD = Decimal("4.73")   # NOT a bound
_HISTORICAL_PREPLAN_MAXIMUM_ESTIMATE_USD = Decimal("9.40")    # NOT a bound

_ROOT = Path(__file__).resolve().parent.parent
_EP_DIR = _ROOT / ".hypogeum-assets" / "episodes" / "ep_f3619022"


def _full_bindings(**over):
    base = [
        ("openrouter", "anthropic/claude-sonnet-4.6", "text.completion", "0.60", 400, "25.00"),
        ("openrouter", "anthropic/claude-opus-4.6", "text.completion", "1.50", 40, "15.00"),
        ("perplexity", "sonar-pro", "research.query", "0.10", 60, "5.00"),
        ("elevenlabs", "eleven_multilingual_v2", "voice.narration_segment", "0.05", 300, "12.00"),
        ("elevenlabs", "music_v1", "music.track", "3.00", 12, "27.00"),
        ("muapi", "veo3.1-lite-text-to-video", "video.text_to_video", "0.39", 220, "86.00"),
        ("muapi", "gpt-image-2-text-to-image", "image.thumbnail", "0.09", 40, "4.00"),
    ]
    out = [
        GrantBinding(provider=p, model=m, provider_operation=o, currency="USD",
                     max_cost_per_call=Decimal(c), max_calls=n, max_total_spend=Decimal(t))
        for (p, m, o, c, n, t) in base
    ]
    return out


def _workflow():
    return json.loads(
        (_ROOT / "channel_packages" / "hypogeum" / "workflow" / "hypogeum_main_production.json").read_text()
    )


def _artifacts():
    if not (_EP_DIR / "script_package_ep_f3619022.json").exists():
        pytest.skip("real ep_f3619022 artifacts not present")
    return {
        "script_package": json.loads((_EP_DIR / "script_package_ep_f3619022.json").read_text()),
        "narration_package": json.loads((_EP_DIR / "narration_package_ep_f3619022.json").read_text()),
        "visual_package": json.loads((_EP_DIR / "visual_package_ep_f3619022.json").read_text()),
    }


class TestEnumerationAndBounds:

    def test_every_preplan_paid_operation_is_enumerated(self):
        a = _artifacts()
        m = compute_preplan_cost_model(
            bindings=_full_bindings(), workflow_definition=_workflow(),
            script_package=a["script_package"], narration_package=a["narration_package"],
        )
        ids = {c["component_id"] for c in m.preplan_component_breakdown}
        assert {"executive_producer_text", "research_director_perplexity",
                "research_director_synthesis", "narrative_architect_text",
                "script_engine_text", "thumbnail_strategy_text",
                "audio_agent_narration"} <= ids
        assert any(cid.startswith("vpd_phase1_") for cid in ids)

    def test_music_is_not_a_preplan_component(self):
        a = _artifacts()
        m = compute_preplan_cost_model(
            bindings=_full_bindings(), workflow_definition=_workflow(),
            script_package=a["script_package"], narration_package=a["narration_package"],
        )
        for c in m.preplan_component_breakdown:
            assert c["provider_operation"] != "music.track"

    def test_each_component_declares_provider_model_operation_domain_and_source(self):
        a = _artifacts()
        m = compute_preplan_cost_model(
            bindings=_full_bindings(), workflow_definition=_workflow(),
            script_package=a["script_package"], narration_package=a["narration_package"],
        )
        for c in m.preplan_component_breakdown:
            assert c["provider"] and c["model"] and c["provider_operation"]
            assert c["provider_domain"] in {
                "openrouter_text", "perplexity_research", "elevenlabs_voice",
            }
            assert c["bound_source"]
            assert c["chargeable_attempts_per_call"] >= 1

    def test_narration_count_is_the_real_segment_count(self):
        a = _artifacts()
        n_segments = len(a["narration_package"]["narration_segments"])
        m = compute_preplan_cost_model(
            bindings=_full_bindings(), workflow_definition=_workflow(),
            script_package=a["script_package"], narration_package=a["narration_package"],
        )
        narr = next(c for c in m.preplan_component_breakdown if c["component_id"] == "audio_agent_narration")
        assert narr["maximum_call_count"] == n_segments
        assert narr["expected_call_count"] == n_segments

    def test_research_queries_are_the_fixed_four(self):
        a = _artifacts()
        m = compute_preplan_cost_model(
            bindings=_full_bindings(), workflow_definition=_workflow(),
            script_package=a["script_package"], narration_package=a["narration_package"],
        )
        rd = next(c for c in m.preplan_component_breakdown if c["component_id"] == "research_director_perplexity")
        assert rd["maximum_call_count"] == 4


class TestFailClosed:

    def test_missing_binding_makes_model_infeasible(self):
        a = _artifacts()
        # drop EVERY openrouter text.completion binding -> those ops are unbounded
        bindings = [b for b in _full_bindings()
                    if not (b.provider == "openrouter" and b.provider_operation == "text.completion")]
        m = compute_preplan_cost_model(
            bindings=bindings, workflow_definition=_workflow(),
            script_package=a["script_package"], narration_package=a["narration_package"],
        )
        assert m.coverage_complete is False
        assert m.unbounded_components
        with pytest.raises(PreplanModelInfeasibleError):
            compute_preplan_cost_model(
                bindings=bindings, workflow_definition=_workflow(),
                script_package=a["script_package"], narration_package=a["narration_package"],
                require_feasible=True,
            )

    def test_no_script_or_narration_makes_narration_and_vpd_unbounded(self):
        m = compute_preplan_cost_model(
            bindings=_full_bindings(), workflow_definition=_workflow(),
            script_package=None, narration_package=None,
        )
        assert m.coverage_complete is False
        assert "audio_agent_narration" in m.unbounded_components
        assert any(u.startswith("vpd_phase1_") for u in m.unbounded_components)

    def test_non_finite_per_call_cost_is_unbounded(self):
        a = _artifacts()
        bad = _full_bindings()
        for b in bad:
            if b.provider == "perplexity":
                b.max_cost_per_call = Decimal("Infinity")
        m = compute_preplan_cost_model(
            bindings=bad, workflow_definition=_workflow(),
            script_package=a["script_package"], narration_package=a["narration_package"],
        )
        assert "research_director_perplexity" in m.unbounded_components

    def test_workflow_order_moved_fails_closed(self):
        wf = _workflow()
        wf["stage_order"] = ["visual_package_director", "executive_producer"]  # VPD first
        with pytest.raises(Exception):
            compute_preplan_cost_model(
                bindings=_full_bindings(), workflow_definition=wf,
                script_package=None, narration_package=None,
            )


class TestPolicyCap:

    def test_structural_worst_case_exceeds_binding_and_is_capped(self):
        a = _artifacts()
        m = compute_preplan_cost_model(
            bindings=_full_bindings(), workflow_definition=_workflow(),
            script_package=a["script_package"], narration_package=a["narration_package"],
        )
        # VPD Phase-1 full-halving worst case blows past any sane budget...
        assert m.preplan_maximum_bounded_cost > Decimal("500")
        # ...but the ledger + binding ceilings hard-stop it:
        assert m.preplan_policy_capped_maximum_cost < m.preplan_maximum_bounded_cost
        alloc = {a2["binding_key"]: a2 for a2 in m.binding_allocation}
        sonnet = alloc["openrouter:anthropic/claude-sonnet-4.6:text.completion"]
        assert sonnet["preplan_exceeds_binding_ceiling"] is True
        assert Decimal(sonnet["preplan_policy_capped_cost"]) == Decimal("25.00")

    def test_policy_capped_max_is_the_reservation_recommendation(self):
        a = _artifacts()
        m = compute_preplan_cost_model(
            bindings=_full_bindings(), workflow_definition=_workflow(),
            script_package=a["script_package"], narration_package=a["narration_package"],
        )
        assert m.preplan_required_reservation == m.preplan_policy_capped_maximum_cost


class TestCompleteEpisodeArithmetic:
    """The five DISTINCT quantities + the two DISTINCT checks."""

    def _cfg(self):
        from hypogeum.config import Config

        class _Shim:
            def __init__(s, b, o): s._b, s._o = b, o
            def __getattr__(s, n): return s._o[n] if n in s._o else getattr(s._b, n)

        return _Shim(Config(), dict(
            MUAPI_DEFAULT_TIER=2, MUAPI_T1_MODEL="veo3-text-to-video",
            MUAPI_T2_MODEL="veo3.1-lite-text-to-video", MUAPI_T3_MODEL="veo3.1-lite-text-to-video",
            MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=1, THUMBNAIL_IMAGE_MODEL="gpt-image-2-text-to-image",
            THUMBNAIL_SHORT_CANDIDATE_CAP=3, ELEVENLABS_MUSIC_DEFAULT_DURATION=120,
            FINANCE_BUDGET_CEILING_USD=135.0,
        ))

    def _prep(self, tmp_path, budget=135.0):
        d = tmp_path / "t.db"
        db.initialize_schema(d)
        cc.create_channel_profile(channel_id="hypogeum", name="H", db_path=d)
        cc.create_channel_operations_profile(
            channel_id="hypogeum", channel_name="H", timezone="UTC", operator_enabled=True,
            production_cadence={"videos_per_week": 1}, weekly_video_budget_usd=budget, db_path=d,
        )
        return d

    def _plan(self, d):
        a = _artifacts()
        return bp.compile_truthful_production_plan(
            script_package=a["script_package"], narration_package=a["narration_package"],
            visual_package=a["visual_package"], channel_id="hypogeum", episode_id="ep_cec",
            config=self._cfg(), db_path=d,
        ), a

    def test_five_quantities_are_distinct_and_no_double_count(self, tmp_path):
        d = self._prep(tmp_path)
        res, a = self._plan(d)
        pm = compute_preplan_cost_model(
            bindings=_full_bindings(), workflow_definition=_workflow(),
            script_package=a["script_package"], narration_package=a["narration_package"],
        )
        cec = bp.compute_complete_episode_cost(
            "hypogeum", "ep_cec", preplan_model=pm, production_plan=res.production_plan,
            narration_package=a["narration_package"], db_path=d,
        )
        # realized narration is netted out of POST_PLAN, not counted twice
        assert cec.realized_narration_netted_out == Decimal(str(res.production_plan.narration_cost_usd))
        assert cec.postplan_maximum_bounded_remaining == (
            Decimal(str(res.production_plan.maximum_bounded_cost_usd)) - cec.realized_narration_netted_out
        )
        # the five quantities
        assert cec.preplan_maximum_bounded == pm.preplan_policy_capped_maximum_cost
        assert cec.postplan_required_future_reservation == cec.postplan_maximum_bounded_remaining
        assert cec.complete_expected_cost == cec.preplan_expected + cec.postplan_expected_remaining
        assert cec.complete_maximum_bounded_cost == (
            cec.preplan_maximum_bounded + cec.postplan_maximum_bounded_remaining
        )
        # complete maximum is NOT the POST_PLAN maximum
        assert cec.complete_maximum_bounded_cost > cec.postplan_maximum_bounded_remaining

    def test_authoritative_135_feasibility_is_reported_truthfully(self, tmp_path):
        d = self._prep(tmp_path)
        res, a = self._plan(d)
        pm = compute_preplan_cost_model(
            bindings=_full_bindings(), workflow_definition=_workflow(),
            script_package=a["script_package"], narration_package=a["narration_package"],
        )
        cec = bp.compute_complete_episode_cost(
            "hypogeum", "ep_cec", preplan_model=pm, production_plan=res.production_plan,
            narration_package=a["narration_package"], db_path=d,
        )
        # As of 2026-08-29 the authoritative fully-bounded maximum does NOT
        # fit USD 135 -- this test documents the current truthful state and
        # the exact deficit. If a future change (tighter VPD Phase-1 bound,
        # verified music cache, revised bindings) makes it fit, update the
        # expectation deliberately -- do not silently loosen it.
        assert cec.forecast_feasible is False
        assert cec.forecast_deficit > Decimal("0")
        assert cec.complete_maximum_bounded_cost > Decimal("135.00")
        # the expected case does fit
        assert cec.complete_expected_cost < Decimal("135.00")

    def test_ledger_upgrade_invariant_is_a_separate_check(self, tmp_path):
        d = self._prep(tmp_path)
        res, a = self._plan(d)
        pm = compute_preplan_cost_model(
            bindings=_full_bindings(), workflow_definition=_workflow(),
            script_package=a["script_package"], narration_package=a["narration_package"],
        )
        cec = bp.compute_complete_episode_cost(
            "hypogeum", "ep_cec", preplan_model=pm, production_plan=res.production_plan,
            narration_package=a["narration_package"], db_path=d,
        )
        # no PRE_PLAN cost recorded in this static computation -> ledger form
        # uses actual incurred (0) + required future reservation + other
        assert cec.actual_preplan_incurred == Decimal("0")
        assert cec.final_weekly_exposure == (
            cec.actual_preplan_incurred + cec.postplan_required_future_reservation + cec.other_weekly_exposure
        )
        # the two checks are genuinely different: forecast fails, ledger form
        # (which excludes the un-incurred PRE_PLAN maximum) can still pass
        assert cec.forecast_feasible is False
        assert cec.ledger_upgrade_ok is True

    def test_other_weekly_exposure_counts_against_both_forms(self, tmp_path):
        d = self._prep(tmp_path)
        res, a = self._plan(d)
        # simulate another episode's committed weekly exposure
        conn = db.get_connection(d)
        try:
            from hypogeum import weekly_budget
            import datetime as _dt
            now = _dt.datetime(2026, 8, 29, 12, 0, tzinfo=_dt.timezone.utc)
            win = weekly_budget.compute_weekly_window(now, "UTC")
            scope = weekly_budget.week_scope_ref("hypogeum", win)
            conn.execute(
                "INSERT INTO budget_reservations (reservation_id, idempotency_key, scope_kind, scope_ref, "
                "channel_id, reserved_amount, currency, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("other_ep_resv", "other-ep-week", "channel_weekly", scope, "hypogeum",
                 "40.00", "USD", "committed", now.isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        pm = compute_preplan_cost_model(
            bindings=_full_bindings(), workflow_definition=_workflow(),
            script_package=a["script_package"], narration_package=a["narration_package"],
        )
        import datetime as _dt
        cec = bp.compute_complete_episode_cost(
            "hypogeum", "ep_cec", preplan_model=pm, production_plan=res.production_plan,
            narration_package=a["narration_package"],
            now=_dt.datetime(2026, 8, 29, 12, 0, tzinfo=_dt.timezone.utc), db_path=d,
        )
        assert cec.other_weekly_exposure == Decimal("40.00")
        assert cec.final_weekly_exposure == (
            cec.actual_preplan_incurred + cec.postplan_required_future_reservation + Decimal("40.00")
        )

    def test_verified_music_credit_lowers_postplan_expected(self, tmp_path):
        d = self._prep(tmp_path)
        res, a = self._plan(d)
        pm = compute_preplan_cost_model(
            bindings=_full_bindings(), workflow_definition=_workflow(),
            script_package=a["script_package"], narration_package=a["narration_package"],
        )
        no_credit = bp.compute_complete_episode_cost(
            "hypogeum", "ep_cec", preplan_model=pm, production_plan=res.production_plan,
            narration_package=a["narration_package"], db_path=d,
        )
        with_credit = bp.compute_complete_episode_cost(
            "hypogeum", "ep_cec", preplan_model=pm, production_plan=res.production_plan,
            narration_package=a["narration_package"],
            verified_music_credit_usd=Decimal("21.60"), db_path=d,
        )
        assert with_credit.postplan_expected_remaining < no_credit.postplan_expected_remaining


class TestHistoricalComparisonOnly:
    """The legacy 4.73 / 9.40 numbers are retained here ONLY as labelled
    historical context, never as an authoritative bound or a proof."""

    def test_authoritative_expected_differs_from_the_legacy_estimate(self):
        a = _artifacts()
        m = compute_preplan_cost_model(
            bindings=_full_bindings(), workflow_definition=_workflow(),
            script_package=a["script_package"], narration_package=a["narration_package"],
        )
        # The legacy estimate omitted narration + understated VPD Phase-1;
        # the authoritative model (policy-ceiling per call) is higher.
        assert m.preplan_expected_cost != _HISTORICAL_PREPLAN_EXPECTED_ESTIMATE_USD
        assert m.preplan_policy_capped_maximum_cost != _HISTORICAL_PREPLAN_MAXIMUM_ESTIMATE_USD
