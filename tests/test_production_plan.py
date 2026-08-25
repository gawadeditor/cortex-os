"""
tests/test_production_plan.py — Unit tests for hypogeum/production_plan.py

Covers:
  TestLoadDeliverableContract       — checksum-verified Channel Package load.
  TestValidateDeliverableShape      — script_package validated AGAINST the
                                       contract, never the reverse.
  TestChannelIndependence           — a second, differently-shaped
                                       synthetic Channel Package compiles
                                       with zero Factory Core changes.
  TestAuthoritativePricing          — every price traces to the SAME
                                       constant the real reservation/ledger
                                       path uses; missing prices reject.
  TestCompileProductionPlan         — real per-scene routing, canonical
                                       scene asset IDs, cost-complete
                                       totals, separated per-operation
                                       retry accounting.
  TestMixedRouting                  — a premium scene + standard Lite
                                       scenes + a portrait Short scene all
                                       price from their OWN real routing,
                                       never a single uniform tier.
  TestContentHash                   — deterministic, sensitive to prompt/
                                       shot-signature changes, excludes
                                       compiled_at.
  TestValidatePlanCoverage          — per-beat/per-deliverable coverage.
  TestPersistence                   — atomic, write-once, immutable.
  TestPersistenceIntegrity          — load re-verifies the persisted
                                       content_hash by recomputing it;
                                       tampered/malformed plans rejected.
  TestAssetLookup                   — ProductionPlan.asset_by_id(), the
                                       lookup the provider boundary uses
                                       (no separate registry class).
  TestGrantBindingSpec              — bridges Finance's approved maximum
                                       into scripts/approve_run_grant.py's
                                       own grant spec binding shape.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.production_plan import (
    MAIN_DELIVERABLE_ID,
    ROLE_CONTINGENCY,
    ROLE_REQUIRED,
    ChannelPackagePolicyDriftError,
    DeliverableContractViolationError,
    ProductionPlanImmutabilityError,
    ProductionPlanIntegrityError,
    ProductionPlanPricingError,
    compile_production_plan,
    load_deliverable_contract,
    load_production_plan,
    normalize_prompt,
    persist_production_plan,
    production_plan_to_grant_binding_spec,
    prompt_fingerprint,
    payload_content_hash,
    validate_deliverable_shape,
    validate_plan_coverage,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HYPOGEUM_PACKAGES_ROOT = _REPO_ROOT / "channel_packages"
_SYNTHETIC_PACKAGES_ROOT = Path(__file__).resolve().parent / "fixtures" / "channel_packages"


def _make_config(**overrides) -> SimpleNamespace:
    base = dict(
        MUAPI_DEFAULT_TIER=2,
        MUAPI_T1_MODEL="veo3-text-to-video",
        MUAPI_T2_MODEL="veo3.1-lite-text-to-video",
        MUAPI_T3_MODEL="veo3.1-lite-text-to-video",
        MUAPI_I2V_MODEL="veo3-image-to-video",
        MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=1,
        THUMBNAIL_IMAGE_MODEL="gpt-image-2-text-to-image",
        THUMBNAIL_SHORT_CANDIDATE_CAP=3,
        ELEVENLABS_MUSIC_DEFAULT_DURATION=120,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _shot(beat_id: str, order: int, **overrides) -> dict:
    shot = {
        "shot_id": f"{beat_id}__shot{order}", "parent_beat_id": beat_id, "shot_order": order,
        "primary_prompt": f"A distinct rendered prompt for {beat_id} shot {order}.",
        "camera_angle": "eye_level", "shot_scale": "medium", "camera_height": "eye_level",
        "camera_direction": "frontal", "camera_movement": "slow_push_in",
        "composition": "rule_of_thirds_left", "background_depth": "shallow",
        "screen_direction": "neutral", "motion_energy": "low",
    }
    shot.update(overrides)
    return shot


def _make_scene(scene_id: str, beat_id: str, *, tier: int = 2, short_id=None,
                 aspect_ratio: str = "16:9", extra_shot_count: int = 0,
                 prompt: str | None = None) -> dict:
    scene = {
        "scene_id": scene_id, "narration_reference": beat_id, "short_id": short_id,
        "production_spec": {
            "aspect_ratio": aspect_ratio,
            "provider_routing": {
                "primary_tier": tier,
                "primary_prompt": prompt or f"Primary prompt for {scene_id}.",
            },
            "reference_image_url": None,
        },
    }
    if extra_shot_count:
        shots = [_shot(beat_id, 1)] + [_shot(beat_id, i + 2) for i in range(extra_shot_count)]
        scene["cinematic_coverage_plan"] = {"beat_id": beat_id, "shots": shots}
    return scene


def _make_script_package(*, short_ids=("short_001", "short_002", "short_003")) -> dict:
    beats = [{"beat_id": "b_001", "text": "one"}, {"beat_id": "b_002", "text": "two"}]
    shorts_scripts = [
        {"short_id": sid, "narration_beats": [{"beat_id": f"b_{sid}", "text": sid}]}
        for sid in short_ids
    ]
    return {
        "sections": [{"section_id": "s1", "narration_beats": beats}],
        "shorts_scripts": shorts_scripts,
    }


def _make_narration_package(*, short_ids=("short_001", "short_002", "short_003")) -> dict:
    segs = [
        {"beat_reference": "b_001", "duration_seconds": 8.0, "character_cost": 100},
        {"beat_reference": "b_002", "duration_seconds": 8.0, "character_cost": 100},
    ]
    for sid in short_ids:
        segs.append({"beat_reference": f"b_{sid}", "duration_seconds": 8.0, "character_cost": 50})
    return {"narration_segments": segs}


def _make_visual_package(*, short_ids=("short_001", "short_002", "short_003"), tier: int = 2) -> dict:
    scenes = [
        _make_scene("s01", "b_001", tier=tier),
        _make_scene("s02", "b_002", tier=tier, extra_shot_count=1),
    ]
    for sid in short_ids:
        scenes.append(_make_scene(f"sh_{sid}", f"b_{sid}", tier=tier, short_id=sid, aspect_ratio="9:16"))
    return {"episode_id": "ep_test", "scenes": scenes}


def _compile(**overrides) -> "ProductionPlan":  # noqa: F821
    cfg = overrides.pop("config", None) or _make_config()
    short_ids = overrides.pop("short_ids", ("short_001", "short_002", "short_003"))
    return compile_production_plan(
        script_package=overrides.pop("script_package", None) or _make_script_package(short_ids=short_ids),
        narration_package=overrides.pop("narration_package", None) or _make_narration_package(short_ids=short_ids),
        visual_package=overrides.pop("visual_package", None) or _make_visual_package(short_ids=short_ids),
        channel_id="hypogeum", episode_id="ep_test", config=cfg,
        packages_root=_HYPOGEUM_PACKAGES_ROOT,
        **overrides,
    )


# ---------------------------------------------------------------------------
# TestLoadDeliverableContract
# ---------------------------------------------------------------------------

class TestLoadDeliverableContract:

    def test_loads_real_hypogeum_contract(self):
        contract = load_deliverable_contract("hypogeum", packages_root=_HYPOGEUM_PACKAGES_ROOT)
        assert contract["allow_standalone_shorts"] is False
        ids = {d["deliverable_id"] for d in contract["deliverables"]}
        assert ids == {"main_episode", "short_001", "short_002", "short_003"}

    def test_unknown_channel_fails_closed(self, tmp_path):
        with pytest.raises(ChannelPackagePolicyDriftError):
            load_deliverable_contract("no_such_channel", packages_root=tmp_path)

    def test_checksum_drift_fails_closed(self, tmp_path):
        channel_dir = tmp_path / "drifted"
        channel_dir.mkdir()
        (channel_dir / "deliverable_contract_v1.json").write_text(
            json.dumps({"deliverables": [], "allow_standalone_shorts": False})
        )
        (channel_dir / "channel_package_manifest.json").write_text(json.dumps({
            "channel_id": "drifted", "artifacts": [{
                "path": "deliverable_contract_v1.json", "role": "deliverable_contract",
                "sha256": "0" * 64,
            }],
        }))
        with pytest.raises(ChannelPackagePolicyDriftError):
            load_deliverable_contract("drifted", packages_root=tmp_path)


# ---------------------------------------------------------------------------
# TestValidateDeliverableShape — script_package validated AGAINST the
# contract, never the reverse.
# ---------------------------------------------------------------------------

class TestValidateDeliverableShape:

    def test_matching_shape_passes(self):
        contract = load_deliverable_contract("hypogeum", packages_root=_HYPOGEUM_PACKAGES_ROOT)
        validate_deliverable_shape(
            contract, _make_script_package(), channel_id="hypogeum", episode_id="ep_test",
        )  # must not raise

    def test_wrong_short_count_rejected(self):
        contract = load_deliverable_contract("hypogeum", packages_root=_HYPOGEUM_PACKAGES_ROOT)
        wrong = _make_script_package(short_ids=("short_001", "short_002"))
        with pytest.raises(DeliverableContractViolationError):
            validate_deliverable_shape(contract, wrong, channel_id="hypogeum", episode_id="ep_test")

    def test_extra_short_rejected(self):
        contract = load_deliverable_contract("hypogeum", packages_root=_HYPOGEUM_PACKAGES_ROOT)
        wrong = _make_script_package(short_ids=("short_001", "short_002", "short_003", "short_004"))
        with pytest.raises(DeliverableContractViolationError):
            validate_deliverable_shape(contract, wrong, channel_id="hypogeum", episode_id="ep_test")

    def test_compile_rejects_wrong_shape_before_any_asset_enumerated(self):
        with pytest.raises(DeliverableContractViolationError):
            _compile(short_ids=("short_001",))


# ---------------------------------------------------------------------------
# TestChannelIndependence — a different Channel Package, different shape,
# zero Factory Core changes.
# ---------------------------------------------------------------------------

class TestChannelIndependence:

    def test_synthetic_two_shorts_channel_compiles_with_its_own_shape(self):
        sp = _make_script_package(short_ids=("clip_a", "clip_b"))
        np = _make_narration_package(short_ids=("clip_a", "clip_b"))
        vp = _make_visual_package(short_ids=("clip_a", "clip_b"))
        plan = compile_production_plan(
            script_package=sp, narration_package=np, visual_package=vp,
            channel_id="synthetic_two_shorts_channel", episode_id="ep_synth", config=_make_config(),
            packages_root=_SYNTHETIC_PACKAGES_ROOT,
        )
        assert plan.deliverable_ids == [MAIN_DELIVERABLE_ID, "clip_a", "clip_b"]

    def test_synthetic_channel_rejects_the_hypogeum_shape(self):
        """Proof the contract is genuinely enforced, not decorative: the
        REAL Hypogeum 3-short shape must be rejected against the synthetic
        2-short channel's own contract."""
        with pytest.raises(DeliverableContractViolationError):
            compile_production_plan(
                script_package=_make_script_package(),  # 3 shorts
                narration_package=_make_narration_package(),
                visual_package=_make_visual_package(),
                channel_id="synthetic_two_shorts_channel", episode_id="ep_synth", config=_make_config(),
                packages_root=_SYNTHETIC_PACKAGES_ROOT,
            )


# ---------------------------------------------------------------------------
# TestAuthoritativePricing
# ---------------------------------------------------------------------------

class TestAuthoritativePricing:

    def test_video_price_matches_model_credit_costs_table(self):
        from hypogeum.muapi import MODEL_CREDIT_COSTS
        plan = _compile()
        required = [a for a in plan.assets if a.role == ROLE_REQUIRED][0]
        assert required.unit_price_usd == MODEL_CREDIT_COSTS["veo3.1-lite-text-to-video"]

    def test_enhancement_price_matches_upscaler_ceiling_constant(self):
        from hypogeum.providers.muapi_video_upscaler import UPSCALE_CEILING_USD
        plan = _compile()
        assert plan.assets[0].enhancement_price_usd == float(UPSCALE_CEILING_USD)

    def test_narration_price_matches_elevenlabs_tts_constant(self):
        from hypogeum.elevenlabs import TTS_USD_PER_CHAR
        plan = _compile()
        assert plan.narration_price_per_char_usd == TTS_USD_PER_CHAR

    def test_music_price_matches_elevenlabs_music_constant(self):
        from hypogeum.elevenlabs import MUSIC_USD_PER_SEC
        plan = _compile()
        assert plan.music_price_per_second_usd == MUSIC_USD_PER_SEC

    def test_thumbnail_price_matches_model_credit_costs_table(self):
        from hypogeum.muapi import MODEL_CREDIT_COSTS
        plan = _compile()
        assert plan.thumbnail_unit_price_usd == MODEL_CREDIT_COSTS["gpt-image-2-text-to-image"]

    def test_thumbnail_attempts_per_candidate_matches_thumbnail_agent_constant(self):
        from hypogeum.agents.thumbnail_generation_agent import _MAX_ATTEMPTS_PER_CANDIDATE
        plan = _compile()
        assert plan.thumbnail_attempts_per_candidate == _MAX_ATTEMPTS_PER_CANDIDATE

    def test_unregistered_video_model_rejected(self):
        cfg = _make_config(MUAPI_T2_MODEL="some-brand-new-unregistered-model")
        with pytest.raises(ProductionPlanPricingError):
            _compile(config=cfg)

    def test_unbounded_max_paid_attempts_rejected(self):
        """0 = unlimited in MUAPIBudgetGuard's own semantics -- a
        Production Plan cannot form a maximum bounded cost against an
        unlimited attempt count and must reject, never assume a number."""
        cfg = _make_config(MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=0)
        with pytest.raises(ProductionPlanPricingError):
            _compile(config=cfg)

    def test_missing_thumbnail_model_rejected(self):
        cfg = _make_config(THUMBNAIL_IMAGE_MODEL="")
        with pytest.raises(ProductionPlanPricingError):
            _compile(config=cfg)


# ---------------------------------------------------------------------------
# TestCompileProductionPlan
# ---------------------------------------------------------------------------

class TestCompileProductionPlan:

    def test_one_required_asset_per_scene(self):
        plan = _compile()
        required = [a for a in plan.assets if a.role == ROLE_REQUIRED]
        assert len(required) == 5  # s01, s02, 3 shorts

    def test_contingency_assets_only_for_scenes_with_extra_planned_shots(self):
        plan = _compile()
        contingency = [a for a in plan.assets if a.role == ROLE_CONTINGENCY]
        assert {a.beat_id for a in contingency} == {"b_002"}

    def test_canonical_asset_id_matches_real_compute_asset_id(self):
        """Asset IDs are the exact canonical identity submit_scene()/the
        ledger use -- hypogeum.muapi._compute_asset_id() -- never a
        parallel beat-ID identity system."""
        from hypogeum.muapi import _compute_asset_id
        plan = _compile()
        required = {a.scene_id: a for a in plan.assets if a.role == ROLE_REQUIRED}
        assert required["s01"].asset_id == _compute_asset_id("ep_test", "s01", None)
        short_asset = next(a for a in plan.assets if a.short_id == "short_001")
        assert short_asset.asset_id == _compute_asset_id("ep_test", "sh_short_001", "short_001")

    def test_contingency_scene_id_matches_clip_repair_naming(self):
        """clip_repair_agent.py's real extra-slot naming is
        f"{sid}__extra{slot_index}" -- the plan's contingency scene_id must
        match exactly so the provider boundary can find it."""
        plan = _compile()
        contingency = [a for a in plan.assets if a.role == ROLE_CONTINGENCY][0]
        assert contingency.scene_id == "s02__extra1"

    def test_plan_is_cost_complete_all_categories_present(self):
        plan = _compile()
        assert plan.narration_cost_usd > 0
        assert plan.music_cost_usd > 0
        assert plan.thumbnail_cost_normal_usd > 0
        assert plan.planning_call_estimate_usd > 0
        video_cost = sum(a.unit_price_usd for a in plan.assets if a.role == ROLE_REQUIRED)
        assert video_cost > 0
        assert plan.enhancement_cost_maximum_usd > 0

    def test_narration_is_realized_when_real_segments_present(self):
        plan = _compile()
        assert plan.narration_is_realized is True

    def test_narration_not_realized_when_no_segments(self):
        plan = compile_production_plan(
            script_package=_make_script_package(), narration_package={},
            visual_package=_make_visual_package(),
            channel_id="hypogeum", episode_id="ep_test", config=_make_config(),
            packages_root=_HYPOGEUM_PACKAGES_ROOT,
        )
        assert plan.narration_is_realized is False
        assert plan.narration_cost_usd == 0

    def test_enhancement_excluded_from_normal_included_in_maximum(self):
        """Enhancement applicability is only known post-generation (real
        probed resolution vs DELIVERABLE_RESOLUTION_POLICY) -- never
        knowable at plan-compile time, so normal excludes it entirely and
        maximum bounds every declared asset at one attempt each."""
        plan = _compile()
        from hypogeum.providers.muapi_video_upscaler import UPSCALE_CEILING_USD
        expected_max = round(len(plan.assets) * float(UPSCALE_CEILING_USD), 4)
        assert plan.enhancement_cost_maximum_usd == pytest.approx(expected_max, abs=0.0001)
        video_normal = sum(a.unit_price_usd for a in plan.assets if a.role == ROLE_REQUIRED)
        implied_normal_without_enhancement = (
            video_normal + plan.thumbnail_cost_normal_usd + plan.narration_cost_usd
            + plan.music_cost_usd + plan.planning_call_estimate_usd
        )
        assert plan.normal_expected_cost_usd == pytest.approx(implied_normal_without_enhancement, abs=0.0001)

    def test_enhancement_not_multiplied_by_video_retry_bound(self):
        """Regression: enhancement must be its OWN bounded operation, never
        (video_price + enhancement_price) * max_chargeable_attempts."""
        plan_1 = _compile(config=_make_config(MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=1))
        plan_5 = _compile(config=_make_config(MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=5))
        assert plan_1.enhancement_cost_maximum_usd == pytest.approx(plan_5.enhancement_cost_maximum_usd, abs=0.0001)
        assert plan_5.maximum_bounded_cost_usd > plan_1.maximum_bounded_cost_usd

    def test_normal_cost_uses_single_attempt_per_required_asset_no_contingency(self):
        plan = _compile()
        video_normal = sum(a.unit_price_usd for a in plan.assets if a.role == ROLE_REQUIRED)
        expected_normal = (
            video_normal + plan.thumbnail_cost_normal_usd + plan.narration_cost_usd
            + plan.music_cost_usd + plan.planning_call_estimate_usd
        )
        assert plan.normal_expected_cost_usd == pytest.approx(expected_normal, abs=0.0001)

    def test_maximum_cost_includes_contingency_and_all_categories(self):
        cfg = _make_config(MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=3)
        plan = _compile(config=cfg)
        video_max = sum(a.unit_price_usd * a.max_chargeable_attempts for a in plan.assets)
        expected_max = (
            video_max + plan.enhancement_cost_maximum_usd + plan.thumbnail_cost_maximum_usd
            + plan.narration_cost_usd + plan.music_cost_usd + plan.planning_call_estimate_usd
        )
        assert plan.maximum_bounded_cost_usd == pytest.approx(expected_max, abs=0.0001)

    def test_more_attempts_strictly_increases_maximum_but_not_normal(self):
        plan_1 = _compile(config=_make_config(MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=1))
        plan_3 = _compile(config=_make_config(MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=3))
        assert plan_3.maximum_bounded_cost_usd > plan_1.maximum_bounded_cost_usd
        assert plan_3.normal_expected_cost_usd == pytest.approx(plan_1.normal_expected_cost_usd, abs=0.0001)

    def test_thumbnail_main_count_is_schema_fixed_three(self):
        plan = _compile()
        assert plan.thumbnail_main_candidate_count == 3

    def test_thumbnail_maximum_bounds_shorts_by_configured_cap(self):
        cfg = _make_config(THUMBNAIL_SHORT_CANDIDATE_CAP=5)
        plan = _compile(config=cfg)
        expected = (3 + 3 * 5 * plan.thumbnail_attempts_per_candidate) * plan.thumbnail_unit_price_usd
        assert plan.thumbnail_cost_maximum_usd == pytest.approx(expected, abs=0.0001)


# ---------------------------------------------------------------------------
# TestMixedRouting — real per-scene routing, never a single uniform tier.
# ---------------------------------------------------------------------------

class TestMixedRouting:

    _SHORT_IDS = ("short_001", "short_002", "short_003")

    def _mixed_visual_package(self) -> dict:
        scenes = [
            _make_scene("s01", "b_001", tier=1),  # premium
            _make_scene("s02", "b_002", tier=2),  # standard Lite
            _make_scene("s03", "b_003", tier=3),  # cheap-mode alias
        ]
        for sid in self._SHORT_IDS:
            scenes.append(_make_scene(f"sh_{sid}", f"b_{sid}", tier=2, short_id=sid, aspect_ratio="9:16"))
        return {"episode_id": "ep_mix", "scenes": scenes}

    def _mixed_script_package(self) -> dict:
        return {
            "sections": [{"section_id": "s1", "narration_beats": [
                {"beat_id": "b_001", "text": "one"}, {"beat_id": "b_002", "text": "two"},
                {"beat_id": "b_003", "text": "three"},
            ]}],
            "shorts_scripts": [
                {"short_id": sid, "narration_beats": [{"beat_id": f"b_{sid}", "text": sid}]}
                for sid in self._SHORT_IDS
            ],
        }

    def _mixed_narration_package(self) -> dict:
        segs = [
            {"beat_reference": "b_001", "duration_seconds": 8.0, "character_cost": 50},
            {"beat_reference": "b_002", "duration_seconds": 8.0, "character_cost": 50},
            {"beat_reference": "b_003", "duration_seconds": 8.0, "character_cost": 50},
        ]
        for sid in self._SHORT_IDS:
            segs.append({"beat_reference": f"b_{sid}", "duration_seconds": 8.0, "character_cost": 30})
        return {"narration_segments": segs}

    def test_each_scene_prices_from_its_own_real_routing_tier(self):
        from hypogeum.muapi import MODEL_CREDIT_COSTS
        plan = compile_production_plan(
            script_package=self._mixed_script_package(),
            narration_package=self._mixed_narration_package(),
            visual_package=self._mixed_visual_package(),
            channel_id="hypogeum", episode_id="ep_mix", config=_make_config(),
            packages_root=_HYPOGEUM_PACKAGES_ROOT,
        )
        by_scene = {a.scene_id: a for a in plan.assets}
        assert by_scene["s01"].model == "veo3-text-to-video"
        assert by_scene["s01"].unit_price_usd == MODEL_CREDIT_COSTS["veo3-text-to-video"]
        assert by_scene["s02"].model == "veo3.1-lite-text-to-video"
        assert by_scene["s03"].model == "veo3.1-lite-text-to-video"
        assert by_scene["sh_short_001"].model == "veo3.1-lite-text-to-video"
        assert by_scene["sh_short_001"].aspect_ratio == "9:16"

    def test_compiled_cost_matches_sum_of_real_per_scene_routing(self):
        from hypogeum.muapi import MODEL_CREDIT_COSTS
        plan = compile_production_plan(
            script_package=self._mixed_script_package(),
            narration_package=self._mixed_narration_package(),
            visual_package=self._mixed_visual_package(),
            channel_id="hypogeum", episode_id="ep_mix", config=_make_config(),
            packages_root=_HYPOGEUM_PACKAGES_ROOT,
        )
        expected_video_normal = (
            MODEL_CREDIT_COSTS["veo3-text-to-video"]
            + MODEL_CREDIT_COSTS["veo3.1-lite-text-to-video"]
            + MODEL_CREDIT_COSTS["veo3.1-lite-text-to-video"]
            + MODEL_CREDIT_COSTS["veo3.1-lite-text-to-video"] * 3  # three Shorts, all tier 2
        )
        actual_video_normal = sum(a.unit_price_usd for a in plan.assets if a.role == ROLE_REQUIRED)
        assert actual_video_normal == pytest.approx(expected_video_normal, abs=0.0001)


# ---------------------------------------------------------------------------
# TestContentHash
# ---------------------------------------------------------------------------

class TestContentHash:

    def test_identical_inputs_produce_identical_hash(self):
        assert _compile().content_hash == _compile().content_hash

    def test_hash_excludes_compiled_at_timestamp(self):
        import time
        plan_a = _compile()
        time.sleep(0.01)
        plan_b = _compile()
        assert plan_a.compiled_at != plan_b.compiled_at
        assert plan_a.content_hash == plan_b.content_hash

    def test_different_max_attempts_changes_hash(self):
        plan_1 = _compile(config=_make_config(MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=1))
        plan_2 = _compile(config=_make_config(MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=2))
        assert plan_1.content_hash != plan_2.content_hash

    def test_changed_asset_prompt_changes_hash(self):
        vp_a = _make_visual_package()
        vp_b = _make_visual_package()
        vp_b["scenes"][0]["production_spec"]["provider_routing"]["primary_prompt"] = "A completely different prompt."
        plan_a = _compile(visual_package=vp_a)
        plan_b = _compile(visual_package=vp_b)
        assert plan_a.content_hash != plan_b.content_hash

    def test_changed_shot_signature_changes_hash(self):
        vp_a = _make_visual_package()
        vp_b = _make_visual_package()
        vp_b["scenes"][1]["cinematic_coverage_plan"]["shots"][1]["camera_angle"] = "overhead"
        plan_a = _compile(visual_package=vp_a)
        plan_b = _compile(visual_package=vp_b)
        assert plan_a.content_hash != plan_b.content_hash


class TestPromptFingerprintIdentity:

    def test_equivalent_normalized_prompts_produce_same_fingerprint(self):
        assert prompt_fingerprint("Hello World") == prompt_fingerprint("  hello   world  ")

    def test_different_prompts_produce_different_fingerprints(self):
        assert prompt_fingerprint("Hello World") != prompt_fingerprint("Goodbye World")

    def test_normalize_prompt_collapses_whitespace_and_case(self):
        assert normalize_prompt("  Hello   World  ") == "hello world"


# ---------------------------------------------------------------------------
# TestValidatePlanCoverage
# ---------------------------------------------------------------------------

class TestValidatePlanCoverage:

    def test_sufficient_beats_pass(self):
        plan = _compile()
        result = validate_plan_coverage(plan, _make_narration_package())
        assert result.sufficient

    def test_long_beat_with_insufficient_shots_fails(self):
        np = _make_narration_package()
        np["narration_segments"][1]["duration_seconds"] = 40.0  # b_002 needs way more than 2x8s*1.15
        plan = _compile(narration_package=np)
        result = validate_plan_coverage(plan, np)
        assert not result.sufficient
        assert [b["beat_id"] for b in result.failing_beats] == ["b_002"]

    def test_evaluation_is_per_beat_never_aggregate(self):
        np = _make_narration_package()
        np["narration_segments"][1]["duration_seconds"] = 40.0
        plan = _compile(narration_package=np)
        result = validate_plan_coverage(plan, np)
        others = [b for b in result.beat_reports if b["beat_id"] != "b_002"]
        assert all(b["sufficient"] for b in others)


# ---------------------------------------------------------------------------
# TestPersistence
# ---------------------------------------------------------------------------

class TestPersistence:

    def test_writes_plan_atomically(self, tmp_path):
        plan = _compile()
        path = persist_production_plan(plan, tmp_path)
        assert path.exists()
        assert not list(tmp_path.glob("*.tmp*"))
        assert json.loads(path.read_text())["content_hash"] == plan.content_hash

    def test_identical_replay_is_a_noop(self, tmp_path):
        plan = _compile()
        path_a = persist_production_plan(plan, tmp_path)
        mtime_a = path_a.stat().st_mtime_ns
        path_b = persist_production_plan(plan, tmp_path)
        assert path_b.stat().st_mtime_ns == mtime_a

    def test_different_plan_for_same_episode_is_refused(self, tmp_path):
        plan_a = _compile()
        persist_production_plan(plan_a, tmp_path)
        plan_b = _compile(config=_make_config(MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=5))
        with pytest.raises(ProductionPlanImmutabilityError):
            persist_production_plan(plan_b, tmp_path)

    def test_load_round_trips(self, tmp_path):
        plan = _compile()
        persist_production_plan(plan, tmp_path)
        loaded = load_production_plan("ep_test", tmp_path)
        assert loaded.content_hash == plan.content_hash

    def test_load_returns_none_when_absent(self, tmp_path):
        assert load_production_plan("ep_missing", tmp_path) is None


# ---------------------------------------------------------------------------
# TestPersistenceIntegrity — load_production_plan() re-verifies the
# persisted content_hash by RECOMPUTING it from the payload; any
# post-persistence mutation is rejected, never silently loaded.
# ---------------------------------------------------------------------------

def _plan_payload(tmp_path: Path) -> tuple[Path, dict]:
    plan = _compile()
    path = persist_production_plan(plan, tmp_path)
    return path, json.loads(path.read_text())


class TestPersistenceIntegrity:

    def test_recomputed_hash_matches_for_an_untouched_persisted_plan(self, tmp_path):
        path, payload = _plan_payload(tmp_path)
        assert payload_content_hash(payload) == payload["content_hash"]
        loaded = load_production_plan("ep_test", tmp_path)
        assert loaded is not None
        assert loaded.content_hash == payload["content_hash"]

    def test_tampered_asset_without_hash_update_is_rejected(self, tmp_path):
        path, payload = _plan_payload(tmp_path)
        payload["assets"][0]["model"] = "veo3-text-to-video"
        path.write_text(json.dumps(payload))
        with pytest.raises(ProductionPlanIntegrityError):
            load_production_plan("ep_test", tmp_path)

    def test_tampered_cost_total_without_hash_update_is_rejected(self, tmp_path):
        path, payload = _plan_payload(tmp_path)
        payload["maximum_bounded_cost_usd"] = 0.0
        path.write_text(json.dumps(payload))
        with pytest.raises(ProductionPlanIntegrityError):
            load_production_plan("ep_test", tmp_path)

    def test_tampered_asset_with_manually_replaced_wrong_hash_is_rejected(self, tmp_path):
        path, payload = _plan_payload(tmp_path)
        payload["assets"][0]["model"] = "veo3-text-to-video"
        payload["content_hash"] = hashlib.sha256(b"attacker-chosen").hexdigest()
        path.write_text(json.dumps(payload))
        with pytest.raises(ProductionPlanIntegrityError) as excinfo:
            load_production_plan("ep_test", tmp_path)
        assert "does not match" in excinfo.value.reason

    def test_missing_content_hash_is_rejected(self, tmp_path):
        path, payload = _plan_payload(tmp_path)
        del payload["content_hash"]
        path.write_text(json.dumps(payload))
        with pytest.raises(ProductionPlanIntegrityError) as excinfo:
            load_production_plan("ep_test", tmp_path)
        assert "missing or malformed" in excinfo.value.reason

    def test_malformed_content_hash_is_rejected(self, tmp_path):
        path, payload = _plan_payload(tmp_path)
        for bogus in ("not-a-hash", "", 12345, "deadbeef"):
            payload["content_hash"] = bogus
            path.write_text(json.dumps(payload))
            with pytest.raises(ProductionPlanIntegrityError):
                load_production_plan("ep_test", tmp_path)

    def test_malformed_asset_entry_is_rejected_not_crashed_on(self, tmp_path):
        path, payload = _plan_payload(tmp_path)
        payload["assets"][0].pop("asset_id")
        path.write_text(json.dumps(payload))
        with pytest.raises(ProductionPlanIntegrityError):
            load_production_plan("ep_test", tmp_path)

    def test_persist_then_load_is_deterministic_across_repeated_reads(self, tmp_path):
        plan = _compile()
        persist_production_plan(plan, tmp_path)
        first = load_production_plan("ep_test", tmp_path)
        second = load_production_plan("ep_test", tmp_path)
        assert first.content_hash == second.content_hash == plan.content_hash
        assert first.normal_expected_cost_usd == second.normal_expected_cost_usd
        assert first.maximum_bounded_cost_usd == second.maximum_bounded_cost_usd
        assert [(a.asset_id, a.model, a.prompt_fingerprint) for a in first.assets] == \
               [(a.asset_id, a.model, a.prompt_fingerprint) for a in second.assets]

    def test_immutability_behavior_remains_intact_alongside_verification(self, tmp_path):
        plan_a = _compile()
        persist_production_plan(plan_a, tmp_path)
        # A verified, untouched plan still loads...
        assert load_production_plan("ep_test", tmp_path).content_hash == plan_a.content_hash
        # ...and a differing re-persist is still refused as before.
        plan_b = _compile(config=_make_config(MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=5))
        with pytest.raises(ProductionPlanImmutabilityError):
            persist_production_plan(plan_b, tmp_path)

    # -- Review finding #1 closure: "assets" must be present AND a list. --
    # Baseline: a legitimately zero-asset plan whose stored hash was
    # computed over [] is verifiable and loads; replacing its assets value
    # with any non-list shape (or removing the key) must be rejected at
    # VERIFICATION time -- never silently canonicalized to [] and never
    # escaping as a raw KeyError/TypeError after verification passed.

    def _persist_zero_asset_plan(self, tmp_path: Path) -> Path:
        payload = _compile().to_dict()
        payload["assets"] = []
        payload = {**payload, "content_hash": payload_content_hash(payload)}
        path = tmp_path / "production_plan_ep_test.json"
        path.write_text(json.dumps(payload))
        return path

    def test_zero_asset_plan_with_list_assets_still_loads(self, tmp_path):
        self._persist_zero_asset_plan(tmp_path)
        loaded = load_production_plan("ep_test", tmp_path)
        assert loaded is not None and loaded.assets == []

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda p: p.pop("assets"), id="missing"),
            pytest.param(lambda p: p.update(assets=None), id="null"),
            pytest.param(lambda p: p.update(assets={"not": "a list"}), id="mapping"),
            pytest.param(lambda p: p.update(assets="assets-as-string"), id="string"),
        ],
    )
    def test_non_list_assets_shape_is_rejected_as_malformed(self, tmp_path, mutate):
        path = self._persist_zero_asset_plan(tmp_path)
        payload = json.loads(path.read_text())
        mutate(payload)
        path.write_text(json.dumps(payload))
        with pytest.raises(ProductionPlanIntegrityError) as excinfo:
            load_production_plan("ep_test", tmp_path)
        assert "malformed" in excinfo.value.reason
        assert isinstance(excinfo.value.__cause__, (KeyError, TypeError))

    # -- Review finding #2 closure: byte-level and OS-level read failures. --

    def test_invalid_utf8_bytes_are_rejected_as_unreadable_text(self, tmp_path):
        persist_production_plan(_compile(), tmp_path)
        path = tmp_path / "production_plan_ep_test.json"
        path.write_bytes(b"\x80\x81\x82 this is not valid utf-8")
        with pytest.raises(ProductionPlanIntegrityError) as excinfo:
            load_production_plan("ep_test", tmp_path)
        assert "UTF-8" in excinfo.value.reason
        assert isinstance(excinfo.value.__cause__, UnicodeDecodeError)

    def test_oserror_on_read_is_wrapped_without_permission_changes(self, tmp_path):
        """Narrow controlled OSError: swap the persisted plan file for a
        directory at the exact same path, so Path.read_text raises
        IsADirectoryError (an OSError subclass) -- no real filesystem
        permissions are modified and nothing outside tmp_path is touched."""
        path = persist_production_plan(_compile(), tmp_path)
        path.unlink()
        path.mkdir()
        with pytest.raises(ProductionPlanIntegrityError) as excinfo:
            load_production_plan("ep_test", tmp_path)
        assert "unreadable" in excinfo.value.reason
        assert isinstance(excinfo.value.__cause__, OSError)


# ---------------------------------------------------------------------------
# TestAssetLookup — the exact lookup the provider boundary uses; no
# separate registry class.
# ---------------------------------------------------------------------------

class TestAssetLookup:

    def test_finds_a_declared_asset_by_canonical_id(self):
        plan = _compile()
        asset = plan.assets[0]
        assert plan.asset_by_id(asset.asset_id) is asset

    def test_returns_none_for_undeclared_asset_id(self):
        plan = _compile()
        assert plan.asset_by_id("never_declared") is None

    def test_round_trips_through_persistence(self, tmp_path):
        plan = _compile()
        persist_production_plan(plan, tmp_path)
        loaded = load_production_plan("ep_test", tmp_path)
        asset = plan.assets[0]
        found = loaded.asset_by_id(asset.asset_id)
        assert found is not None
        assert found.prompt_fingerprint == asset.prompt_fingerprint
        assert found.model == asset.model


# ---------------------------------------------------------------------------
# TestGrantBindingSpec
# ---------------------------------------------------------------------------

class TestGrantBindingSpec:

    def test_produces_one_binding_per_distinct_model(self):
        plan = _compile()
        fragment = production_plan_to_grant_binding_spec(plan)
        bindings = fragment["bindings"]
        models = {b["model"] for b in bindings}
        assert models == {a.model for a in plan.assets}

    def test_max_total_spend_matches_plan_video_maximum_for_that_model(self):
        plan = _compile(config=_make_config(MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=2))
        bindings = production_plan_to_grant_binding_spec(plan)["bindings"]
        binding = bindings[0]
        model_assets = [a for a in plan.assets if a.model == binding["model"]]
        expected = sum(a.unit_price_usd * a.max_chargeable_attempts for a in model_assets)
        assert float(binding["max_total_spend"]) == pytest.approx(expected, abs=0.01)

    def test_max_calls_matches_sum_of_attempt_bounds(self):
        plan = _compile()
        bindings = production_plan_to_grant_binding_spec(plan)["bindings"]
        binding = bindings[0]
        model_assets = [a for a in plan.assets if a.model == binding["model"]]
        assert binding["max_calls"] == sum(a.max_chargeable_attempts for a in model_assets)

    def test_carries_the_plan_content_hash_for_grant_binding(self):
        """Phase 2 plan-bound authorization: the emitted grant-spec
        fragment carries the plan's own content_hash so the offline
        approval ceremony signs the grant over that exact verified
        persisted Production Plan."""
        plan = _compile()
        fragment = production_plan_to_grant_binding_spec(plan)
        assert fragment["production_plan_content_hash"] == plan.content_hash
        assert len(fragment["production_plan_content_hash"]) == 64
