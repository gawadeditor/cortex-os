"""Video Model bake-off — model catalogue, cost preview, dry-run generation,
per-channel adoption."""

from __future__ import annotations

import os

import pytest

from hypogeum import video_bakeoff as bo
from hypogeum.channel_config import (
    create_channel_profile, create_model_profile, get_model_profile,
    update_channel_profile,
)


def test_model_catalogue_has_prices_cheapest_first():
    models = bo.list_video_models()
    assert models, "expected MUAPI video models"
    prices = [m["price_usd"] for m in models]
    assert prices == sorted(prices)
    ids = {m["id"] for m in models}
    assert "veo3.1-lite-text-to-video" in ids
    assert all("image-to-video" not in i for i in ids)


def test_estimate_cost_sums_selected_models():
    est = bo.estimate_cost(["veo3.1-lite-text-to-video", "runway-text-to-video"])
    assert est["clip_count"] == 2
    assert est["total_usd"] == pytest.approx(0.54, abs=0.01)


def test_build_prompt_returns_channel_flavoured_scene(tmp_path):
    pack = bo.build_channel_scene_prompt("hypogeum", aspect_ratio="9:16", db_path=tmp_path / "x.db")
    assert pack["aspect_ratio"] == "9:16"
    assert "SCENE:" in pack["prompt"].upper() or "Scene:" in pack["prompt"]
    assert len(pack["prompt"]) > 200  # DNA sections appended


def test_run_bakeoff_dry_run_produces_one_clip_per_model(tmp_path, monkeypatch):
    monkeypatch.setattr(bo, "_BAKEOFF_ROOT", tmp_path / "bakeoffs")
    from hypogeum.config import Config
    cfg = Config()
    cfg.MUAPI_DRY_RUN = True

    manifest = bo.run_bakeoff(
        "hypogeum", "Scene: a lit stone chamber.",
        ["veo3.1-lite-text-to-video", "runway-text-to-video"],
        aspect_ratio="16:9", config=cfg,
    )
    assert manifest["dry_run"] is True
    assert manifest["spent_cost_usd"] == 0.0
    assert len(manifest["results"]) == 2
    assert all(r["ok"] for r in manifest["results"])
    assert bo.clip_path(manifest["run_id"], "veo3.1-lite-text-to-video") is not None
    assert bo.get_bakeoff(manifest["run_id"])["run_id"] == manifest["run_id"]


def test_run_bakeoff_rejects_unknown_and_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(bo, "_BAKEOFF_ROOT", tmp_path / "b")
    from hypogeum.config import Config
    cfg = Config(); cfg.MUAPI_DRY_RUN = True
    with pytest.raises(ValueError):
        bo.run_bakeoff("c", "prompt", ["not-a-real-model"], config=cfg)
    with pytest.raises(ValueError):
        bo.run_bakeoff("c", "   ", ["veo3.1-lite-text-to-video"], config=cfg)


def test_adopt_writes_video_model_to_channel(tmp_path):
    db = tmp_path / "t.db"
    mp = create_model_profile("hypogeum models", db_path=db)
    create_channel_profile("hypogeum", "Hypogeum", model_profile_id=mp.model_profile_id, db_path=db)

    from hypogeum.channel_config import update_model_profile
    assert update_model_profile(mp.model_profile_id, db_path=db,
                                video_model="veo3-text-to-video")
    assert get_model_profile(mp.model_profile_id, db_path=db).video_model == "veo3-text-to-video"
