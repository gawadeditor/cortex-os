"""A channel's adopted video model (Video Model bake-off) drives production:
resolution helper, Production Plan pricing, and the plan/submission model-match.
"""

from __future__ import annotations

import pytest

from hypogeum.channel_config import (
    create_brand_profile, create_channel_profile, create_model_profile,
    resolve_channel_video_model, update_channel_profile, update_model_profile,
)
from hypogeum.config import Config
from hypogeum.muapi import MODEL_CREDIT_COSTS, _resolve_scene_model, _tier_to_model
from hypogeum.production_plan import ProductionPlanPricingError, resolve_video_price


def _cfg(**over):
    c = Config()
    c.MUAPI_T1_MODEL = "veo3-text-to-video"
    c.MUAPI_T2_MODEL = "veo3.1-lite-text-to-video"
    c.MUAPI_T3_MODEL = "veo3.1-lite-text-to-video"
    c.MUAPI_I2V_MODEL = "veo3-image-to-video"
    c.SMOKE_TEST_MODE = False
    c.MUAPI_CHEAP_MODE = False
    c.MUAPI_RUNWAY_ONLY_MODE = False
    for k, v in over.items():
        setattr(c, k, v)
    return c


def _channel_with_model(db, channel_id="c", model="seedance-pro-t2v-fast", price=0.06):
    mp = create_model_profile(f"{channel_id} models", db_path=db)
    create_channel_profile(channel_id, channel_id.title(),
                           model_profile_id=mp.model_profile_id, db_path=db)
    if model:
        update_model_profile(mp.model_profile_id, db_path=db,
                             video_model=model, video_model_price_usd=price)
    return mp


# ── resolve_channel_video_model ──────────────────────────────────────────

def test_resolve_channel_video_model(tmp_path):
    db = tmp_path / "t.db"
    _channel_with_model(db, "ch1", "kling-v3.0-pro-text-to-video", 0.72)
    assert resolve_channel_video_model("ch1", db_path=db) == ("kling-v3.0-pro-text-to-video", 0.72)
    # unknown / no-profile channel
    assert resolve_channel_video_model("nope", db_path=db) == (None, None)


def test_resolve_channel_video_model_none_when_unset(tmp_path):
    db = tmp_path / "t.db"
    _channel_with_model(db, "ch2", model=None)
    assert resolve_channel_video_model("ch2", db_path=db) == (None, None)


# ── _resolve_scene_model precedence ──────────────────────────────────────

def test_scene_model_uses_channel_override(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _channel_with_model(db, "ovr", "seedance-pro-t2v-fast", 0.06)
    monkeypatch.setattr("hypogeum.channel_config._resolve_db_path", lambda p: db)
    assert _resolve_scene_model(2, None, _cfg(), "ovr") == "seedance-pro-t2v-fast"
    # no channel -> global tier config
    assert _resolve_scene_model(2, None, _cfg(), None) == "veo3.1-lite-text-to-video"


def test_i2v_always_wins_over_channel_override(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _channel_with_model(db, "ovr", "seedance-pro-t2v-fast", 0.06)
    monkeypatch.setattr("hypogeum.channel_config._resolve_db_path", lambda p: db)
    assert _resolve_scene_model(2, "http://ref.png", _cfg(), "ovr") == "veo3-image-to-video"


@pytest.mark.parametrize("mode", ["SMOKE_TEST_MODE", "MUAPI_CHEAP_MODE", "MUAPI_RUNWAY_ONLY_MODE"])
def test_global_safety_mode_wins_over_channel_override(tmp_path, monkeypatch, mode):
    db = tmp_path / "t.db"
    _channel_with_model(db, "ovr", "seedance-pro-t2v-fast", 0.06)
    monkeypatch.setattr("hypogeum.channel_config._resolve_db_path", lambda p: db)
    cfg = _cfg(**{mode: True})
    got = _resolve_scene_model(2, None, cfg, "ovr")
    assert got == _tier_to_model(2, None, cfg)  # global config, not the channel model


# ── resolve_video_price channel_price fallback ───────────────────────────

def test_price_uses_channel_price_for_off_table_model():
    # wan2.5-text-to-video is not in MODEL_CREDIT_COSTS (seedance-pro-t2v-fast
    # was added to the table at its effective billed price in 879320e, so it
    # no longer exercises the channel_price fallback path).
    assert "wan2.5-text-to-video" not in MODEL_CREDIT_COSTS
    assert resolve_video_price("wan2.5-text-to-video", episode_id="e",
                               channel_price=0.06) == 0.06


def test_price_still_refuses_unknown_model_with_no_channel_price():
    with pytest.raises(ProductionPlanPricingError):
        resolve_video_price("totally-made-up-model", episode_id="e")


def test_price_table_wins_over_channel_price_for_known_model():
    # a known model ignores channel_price and uses the authoritative table
    from hypogeum.muapi import MODEL_CREDIT_COSTS
    known = "veo3.1-lite-text-to-video"
    assert resolve_video_price(known, episode_id="e", channel_price=99.0) == \
        float(MODEL_CREDIT_COSTS[known])


# ── end-to-end: the plan declares the channel's model, at the channel's price ──

def test_production_plan_declares_channel_model_and_price(monkeypatch):
    from tests.test_production_plan import _compile

    # An off-table adopted model so the channel_price path is what's exercised.
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_model",
        lambda cid, db_path=None: ("wan2.5-text-to-video", 0.06),
    )
    plan = _compile()
    video_assets = [a for a in plan.assets if a.provider == "muapi"
                    and a.model == "wan2.5-text-to-video"]
    other = [a for a in plan.assets if a.provider == "muapi"
             and a.model != "wan2.5-text-to-video" and "image" not in a.model]
    assert video_assets, "expected video assets on the channel model"
    assert not other, f"some video assets kept a non-channel model: {[a.model for a in other]}"
    for a in video_assets:
        assert a.unit_price_usd == 0.06

    # _submit_scene_via_gate would compute the SAME model -> no plan mismatch
    from hypogeum.muapi import _resolve_scene_model
    c = _cfg()
    assert _resolve_scene_model(2, None, c, "any_channel") == "wan2.5-text-to-video"


def test_production_plan_unchanged_for_channel_without_override(monkeypatch):
    from tests.test_production_plan import _compile
    monkeypatch.setattr("hypogeum.channel_config.resolve_channel_video_model",
                        lambda cid, db_path=None: (None, None))
    plan = _compile()
    models = {a.model for a in plan.assets if a.provider == "muapi"}
    assert "seedance-pro-t2v-fast" not in models
    assert "veo3.1-lite-text-to-video" in models


# ── plan/gate symmetry: I2V-predicted scenes + off-table channel price ──

def _vp_with_final_locked_primary():
    """The standard hypogeum visual package, but s01 gets a final-locked
    primary entity (→ I2V at submission) and s02 references it only as
    secondary (→ stays on the channel T2V model)."""
    from tests.test_production_plan import _make_visual_package
    vp = _make_visual_package()
    vp["scenes"][0]["entity_appearances"] = [{"entity_id": "hero", "role": "primary"}]
    vp["scenes"][1]["entity_appearances"] = [{"entity_id": "hero", "role": "secondary"}]
    vp["visual_continuity_record"] = {
        "locked_entities": [
            {"entity_id": "hero", "lock_state": "final",
             "reference_image_asset_id": "episodes/ep_test/video/ref_hero"},
        ],
    }
    return vp


def test_plan_predicts_i2v_for_final_locked_primary_scene(monkeypatch):
    from tests.test_production_plan import _compile

    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_model",
        lambda cid, db_path=None: ("wan2.5-text-to-video", 0.06),
    )
    plan = _compile(visual_package=_vp_with_final_locked_primary())

    by_scene = {a.scene_id: a for a in plan.assets if a.role == "required"}
    # s01: final-locked primary → declared as the I2V model at its table price
    assert "image-to-video" in by_scene["s01"].model
    assert by_scene["s01"].unit_price_usd == MODEL_CREDIT_COSTS[by_scene["s01"].model]
    # s02: no final-locked primary → channel T2V model at the channel price
    assert by_scene["s02"].model == "wan2.5-text-to-video"
    assert by_scene["s02"].unit_price_usd == 0.06


def test_gate_unit_price_matches_plan_for_every_model(tmp_path):
    """_resolve_gate_unit_price must return the SAME number the plan priced
    each model at — otherwise a legit submission fails plan_price_mismatch."""
    from hypogeum.muapi import _resolve_gate_unit_price, MODEL_CREDIT_COSTS
    from hypogeum.production_plan import resolve_video_price

    db = tmp_path / "t.db"
    _channel_with_model(db, "cc", "wan2.5-text-to-video", 0.06)
    import hypogeum.channel_config as cc
    orig = cc._resolve_db_path
    cc._resolve_db_path = lambda p: db
    try:
        # off-table channel model: gate price == channel price == plan price
        gate = _resolve_gate_unit_price("wan2.5-text-to-video", "cc", "ep")
        plan = resolve_video_price("wan2.5-text-to-video", episode_id="ep", channel_price=0.06)
        assert gate == plan == 0.06
        # on-table model (I2V): both use the table
        m = "veo3-image-to-video"
        assert _resolve_gate_unit_price(m, "cc", "ep") == MODEL_CREDIT_COSTS[m]
        assert resolve_video_price(m, episode_id="ep") == MODEL_CREDIT_COSTS[m]
    finally:
        cc._resolve_db_path = orig


# ── measured clip duration: channel model shot-budget symmetry ──

def test_resolve_channel_video_duration(tmp_path):
    db = tmp_path / "t.db"
    mp = _channel_with_model(db, "ch1", "seedance-pro-t2v-fast", 0.06)
    from hypogeum.channel_config import update_model_profile, resolve_channel_video_duration
    assert resolve_channel_video_duration("ch1", db_path=db) is None
    update_model_profile(mp.model_profile_id, db_path=db, video_model_duration_seconds=5.041667)
    assert resolve_channel_video_duration("ch1", db_path=db) == 5.041667
    assert resolve_channel_video_duration("nope", db_path=db) is None


def test_get_requested_clip_duration_uses_channel_measured_value(monkeypatch):
    from hypogeum.muapi import get_requested_clip_duration_seconds
    c = _cfg()
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_model",
        lambda cid, db_path=None: ("seedance-pro-t2v-fast", 0.06),
    )
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_duration",
        lambda cid, db_path=None: 5.041667,
    )
    assert get_requested_clip_duration_seconds(c, "twinkle") == 5.041667
    # no channel_id -> unchanged global default
    assert get_requested_clip_duration_seconds(c) == 8.0


def test_get_requested_clip_duration_falls_back_without_measurement(monkeypatch):
    from hypogeum.muapi import get_requested_clip_duration_seconds
    c = _cfg()
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_model",
        lambda cid, db_path=None: ("seedance-pro-t2v-fast", 0.06),
    )
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_duration",
        lambda cid, db_path=None: None,
    )
    # adopted model with no measurement on record -> conservative 8s default
    assert get_requested_clip_duration_seconds(c, "twinkle") == 8.0


def test_compute_shot_budget_plans_more_shots_for_short_clip_model(monkeypatch):
    from hypogeum.agents.cinematic_director import compute_shot_budget
    c = _cfg()
    c.CINEMATIC_SECONDS_PER_SHOT = 100.0  # neutralize the editorial-pacing term
    c.CINEMATIC_MAX_SHOTS_PER_BEAT = 10
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_model",
        lambda cid, db_path=None: ("seedance-pro-t2v-fast", 0.06),
    )
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_duration",
        lambda cid, db_path=None: 5.041667,
    )
    # a 12s beat needs 3 shots at ~5s/shot but only 2 at the (wrong) 8s default
    budget_with_channel = compute_shot_budget(12.0, c, "twinkle")
    budget_without_channel = compute_shot_budget(12.0, c, None)
    assert budget_with_channel > budget_without_channel


# ── per-channel I2V model (cheap image-to-video for entity scenes) ──

def test_channel_i2v_model_wins_over_global_default(tmp_path):
    db = tmp_path / "t.db"
    mp = _channel_with_model(db, "twk", "seedance-pro-t2v-fast", 0.06)
    from hypogeum.channel_config import update_model_profile, resolve_channel_i2v_model
    update_model_profile(mp.model_profile_id, db_path=db,
                         video_i2v_model="runway-image-to-video",
                         video_i2v_model_price_usd=0.15)
    assert resolve_channel_i2v_model("twk", db_path=db) == ("runway-image-to-video", 0.15)

    import hypogeum.channel_config as cc
    orig = cc._resolve_db_path
    cc._resolve_db_path = lambda p: db
    try:
        c = _cfg()
        # reference image set -> the channel's I2V model, not the global veo3
        assert _resolve_scene_model(2, "http://ref.png", c, "twk") == "runway-image-to-video"
        # a channel with no I2V model set -> still the global default
        _channel_with_model(db, "twk2", "seedance-pro-t2v-fast", 0.06)
        assert _resolve_scene_model(2, "http://ref.png", c, "twk2") == c.MUAPI_I2V_MODEL
    finally:
        cc._resolve_db_path = orig


def test_plan_and_gate_price_the_channel_i2v_model_identically(monkeypatch):
    from hypogeum.muapi import _resolve_gate_unit_price
    from hypogeum.production_plan import resolve_video_price
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_model",
        lambda cid, db_path=None: ("seedance-pro-t2v-fast", 0.06),
    )
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_i2v_model",
        lambda cid, db_path=None: ("some-exotic-i2v-model", 0.22),
    )
    # off-table I2V model priced from the channel record, same both sides
    gate = _resolve_gate_unit_price("some-exotic-i2v-model", "twk", "ep")
    plan = resolve_video_price("some-exotic-i2v-model", episode_id="ep", channel_price=0.22)
    assert gate == plan == 0.22


def test_production_plan_prices_i2v_scenes_at_channel_i2v_price(monkeypatch):
    from tests.test_production_plan import _compile
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_video_model",
        lambda cid, db_path=None: ("seedance-pro-t2v-fast", 0.06),
    )
    monkeypatch.setattr(
        "hypogeum.channel_config.resolve_channel_i2v_model",
        lambda cid, db_path=None: ("runway-image-to-video", 0.15),
    )
    plan = _compile(visual_package=_vp_with_final_locked_primary())
    by_scene = {a.scene_id: a for a in plan.assets if a.role == "required"}
    # s01 final-locked primary -> the channel's I2V model at its price ($0.15,
    # NOT the $2.50 global veo3-image-to-video)
    assert by_scene["s01"].model == "runway-image-to-video"
    assert by_scene["s01"].unit_price_usd == 0.15
    assert by_scene["s02"].model == "seedance-pro-t2v-fast"
