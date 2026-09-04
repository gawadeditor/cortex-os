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


_FAKE_CATALOGUE = [
    {"id": "cheap-t2v", "price_usd": 0.10, "family": "x", "notes": "", "dynamic_pricing": False},
    {"id": "mid-t2v", "price_usd": 0.40, "family": "y", "notes": "", "dynamic_pricing": True},
    {"id": "premium-t2v", "price_usd": 2.50, "family": "z", "notes": "", "dynamic_pricing": False},
]


@pytest.fixture
def stub_catalogue(monkeypatch):
    monkeypatch.setattr(bo, "list_video_models", lambda *a, **k: list(_FAKE_CATALOGUE))


def test_live_catalogue_filters_to_text_to_video(monkeypatch):
    raw = [
        {"name": "kling-x", "category": "Text to Video", "cost": 0.72, "family": "kling",
         "description": "d", "dynamic_pricing": True},
        {"name": "thumb-x", "category": "Text to Image", "cost": 0.01},
        {"name": "bad-cost", "category": "Text to Video", "cost": None},
        {"name": "cheap-x", "category": "Text to Video", "cost": 0.05, "family": "z"},
    ]
    monkeypatch.setattr(bo, "_fetch_muapi_models", lambda cfg: raw)
    bo._MODELS_CACHE.update(at=0.0, models=None)
    models = bo.list_video_models(force_refresh=True)
    bo._MODELS_CACHE.update(at=0.0, models=None)  # don't leak into other tests
    assert [m["id"] for m in models] == ["cheap-x", "kling-x"]  # cheapest first, bad-cost dropped


def test_live_catalogue_falls_back_when_api_down(monkeypatch):
    monkeypatch.setattr(bo, "_fetch_muapi_models", lambda cfg: None)
    bo._MODELS_CACHE.update(at=0.0, models=None)
    models = bo.list_video_models(force_refresh=True)
    bo._MODELS_CACHE.update(at=0.0, models=None)
    assert any(m["id"] == "veo3.1-lite-text-to-video" for m in models)


def test_estimate_cost_sums_selected_models(stub_catalogue):
    est = bo.estimate_cost(["cheap-t2v", "mid-t2v"])
    assert est["clip_count"] == 2
    assert est["total_usd"] == pytest.approx(0.50, abs=0.001)
    assert est["any_dynamic"] is True


def test_build_prompt_is_channel_specific(tmp_path):
    db = tmp_path / "x.db"
    from hypogeum.channel_config import create_brand_profile, create_channel_profile
    b_doc = create_brand_profile("Doc Brand", visual_style="warm earth tones, ancient stone",
                                 tone="investigative", db_path=db)
    b_kids = create_brand_profile("Kids Brand", visual_style="soft pastel watercolor, storybook",
                                  tone="gentle whimsical soothing", db_path=db)
    create_channel_profile("doc", "Doc", brand_profile_id=b_doc.brand_profile_id, db_path=db)
    create_channel_profile("kids", "Kids", brand_profile_id=b_kids.brand_profile_id, db_path=db)

    p_doc = bo.build_channel_scene_prompt("doc", aspect_ratio="16:9", db_path=db)
    p_kids = bo.build_channel_scene_prompt("kids", aspect_ratio="9:16", db_path=db)

    assert "SCENE:" in p_doc["prompt"] and "STYLE:" in p_doc["prompt"]
    assert p_doc["prompt"] != p_kids["prompt"], "prompt must differ by channel"
    assert "pastel" in p_kids["prompt"].lower()
    assert "ancient stone" in p_doc["prompt"].lower()
    assert p_kids["aspect_ratio"] == "9:16"


def test_run_bakeoff_dry_run_produces_one_clip_per_model(tmp_path, monkeypatch, stub_catalogue):
    monkeypatch.setattr(bo, "_BAKEOFF_ROOT", tmp_path / "bakeoffs")
    from hypogeum.config import Config
    cfg = Config()
    cfg.MUAPI_DRY_RUN = True

    manifest = bo.run_bakeoff(
        "hypogeum", "Scene: a lit stone chamber.",
        ["cheap-t2v", "mid-t2v"],
        aspect_ratio="16:9", config=cfg,
    )
    assert manifest["dry_run"] is True
    assert manifest["spent_cost_usd"] == 0.0
    assert len(manifest["results"]) == 2
    assert all(r["ok"] for r in manifest["results"])
    assert bo.clip_path(manifest["run_id"], "cheap-t2v") is not None
    assert bo.get_bakeoff(manifest["run_id"])["run_id"] == manifest["run_id"]


def test_run_bakeoff_writes_progress_fields(tmp_path, monkeypatch, stub_catalogue):
    monkeypatch.setattr(bo, "_BAKEOFF_ROOT", tmp_path / "bakeoffs")
    from hypogeum.config import Config
    cfg = Config(); cfg.MUAPI_DRY_RUN = True
    manifest = bo.run_bakeoff("c", "Scene: x.", ["cheap-t2v", "mid-t2v", "premium-t2v"],
                              config=cfg)
    assert manifest["status"] == "complete"
    assert manifest["done"] == 3 and manifest["total"] == 3
    disk = bo.get_bakeoff(manifest["run_id"])
    assert disk["status"] == "complete"
    assert {"status", "done", "total", "now_running"} <= set(disk)


def test_run_bakeoff_threads_reference_image_url_for_i2v(tmp_path, monkeypatch, stub_catalogue):
    """
    I2V models require a real hosted reference image (hypogeum.muapi.
    _build_job_payload's own docstring: Seedance I2V returns HTTP 422
    without one) -- run_bakeoff()/start_bakeoff() must actually pass it
    through to the real payload builder, not silently drop it the way the
    bake-off tool did before (2026-09-04: the tool was T2V-only).
    """
    monkeypatch.setattr(bo, "_BAKEOFF_ROOT", tmp_path / "bakeoffs")
    from hypogeum.config import Config
    cfg = Config(); cfg.MUAPI_DRY_RUN = True

    captured = []
    import hypogeum.muapi as muapi_mod
    real_build = muapi_mod._build_job_payload

    def _spy(prompt, model, aspect_ratio, req_dur, reference_image_url, **kw):
        captured.append(reference_image_url)
        return real_build(prompt, model, aspect_ratio, req_dur, reference_image_url, **kw)

    monkeypatch.setattr(muapi_mod, "_build_job_payload", _spy)

    ref_url = "https://cdn.muapi.ai/outputs/generated/fake_ref.jpg"
    manifest = bo.run_bakeoff(
        "twinkle_mini_tales", "Scene: a soft glowing bakery at night.",
        ["cheap-t2v"], aspect_ratio="9:16", config=cfg,
        reference_image_url=ref_url,
    )

    assert captured == [ref_url]
    assert manifest["reference_image_url"] == ref_url
    assert manifest["results"][0]["ok"] is True


def test_start_bakeoff_returns_immediately_and_finishes(tmp_path, monkeypatch, stub_catalogue):
    monkeypatch.setattr(bo, "_BAKEOFF_ROOT", tmp_path / "bakeoffs")
    from hypogeum.config import Config
    cfg = Config(); cfg.MUAPI_DRY_RUN = True

    run_id = bo.start_bakeoff("c", "Scene: x.", ["cheap-t2v", "mid-t2v"], config=cfg)
    # manifest exists straight away, status running
    m0 = bo.get_bakeoff(run_id)
    assert m0 and m0["status"] == "running" and m0["total"] == 2

    t = bo._ACTIVE.get(run_id)
    if t:
        t.join(timeout=30)
    m1 = bo.get_bakeoff(run_id)
    assert m1["status"] == "complete"
    assert len(m1["results"]) == 2


def test_run_bakeoff_rejects_unknown_and_empty(tmp_path, monkeypatch, stub_catalogue):
    monkeypatch.setattr(bo, "_BAKEOFF_ROOT", tmp_path / "b")
    from hypogeum.config import Config
    cfg = Config(); cfg.MUAPI_DRY_RUN = True
    with pytest.raises(ValueError):
        bo.run_bakeoff("c", "prompt", ["not-a-real-model"], config=cfg)
    with pytest.raises(ValueError):
        bo.run_bakeoff("c", "   ", ["cheap-t2v"], config=cfg)


def test_adopt_writes_video_model_to_channel(tmp_path):
    db = tmp_path / "t.db"
    mp = create_model_profile("hypogeum models", db_path=db)
    create_channel_profile("hypogeum", "Hypogeum", model_profile_id=mp.model_profile_id, db_path=db)

    from hypogeum.channel_config import update_model_profile
    assert update_model_profile(mp.model_profile_id, db_path=db,
                                video_model="veo3-text-to-video")
    assert get_model_profile(mp.model_profile_id, db_path=db).video_model == "veo3-text-to-video"
