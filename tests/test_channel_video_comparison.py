"""Tests for hypogeum.channel_video_comparison — the per-channel saved
video-model head-to-head."""
from __future__ import annotations

import json

import pytest

from hypogeum import channel_video_comparison as cvc


@pytest.fixture()
def store(tmp_path, monkeypatch):
    root = tmp_path / "model_comparisons"
    monkeypatch.setattr(cvc, "_ROOT", root)
    d = root / "ch1"
    d.mkdir(parents=True)
    (d / "wan2.7-text-to-video.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42fake-wan")
    (d / "veo3.1-lite-text-to-video.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42fake-veo")
    (d / "comparison.json").write_text(json.dumps({
        "channel_id": "ch1",
        "source": {"episode_id": "ep_a", "episode_title": "T", "scene_id": "s02",
                   "aspect_ratio": "9:16", "requested_duration_s": 8.0, "prompt": "SCENE: ..."},
        "current": {"model": "wan2.7-text-to-video", "role": "current",
                    "clip_file": "wan2.7-text-to-video.mp4", "price_per_call": 0.65,
                    "duration_s": 5.0, "native_resolution": "720x1280", "provider": "MUAPI"},
        "alternative": {"model": "veo3.1-lite-text-to-video", "role": "alternative",
                        "clip_file": "veo3.1-lite-text-to-video.mp4", "price_per_call": 0.39,
                        "duration_s": 8.0, "native_resolution": "720x1280", "provider": "MUAPI"},
        "note": "both 720p",
    }))
    return root


def test_has_comparison(store):
    assert cvc.has_comparison("ch1") is True
    assert cvc.has_comparison("nope") is False


def test_get_comparison_computes_costs(store):
    r = cvc.get_comparison("ch1")
    assert r is not None
    cur, alt = r["current"], r["alternative"]
    # gen + $0.05 upscale, x15 scenes
    assert cur["cost_per_shot_total"] == 0.70
    assert cur["cost_per_episode"] == 10.50
    assert alt["cost_per_shot_total"] == 0.44
    assert alt["cost_per_episode"] == 6.60
    # per-second: wan 0.65/5, veo 0.39/8
    assert cur["cost_per_second"] == 0.13
    assert alt["cost_per_second"] == 0.049
    # delta: switching to alt saves
    assert r["delta"]["per_episode"] == -3.9
    assert r["delta"]["per_week"] == -27.3
    assert cur["clip_url"] == "/channel/ch1/video-compare/clip/wan2.7-text-to-video"


def test_get_comparison_none_when_absent(store):
    assert cvc.get_comparison("ghost") is None


def test_clip_path_only_recorded_files(store):
    assert cvc.clip_path("ch1", "wan2.7-text-to-video") is not None
    assert cvc.clip_path("ch1", "veo3.1-lite-text-to-video") is not None
    # not a recorded clip -> None (no path traversal / arbitrary serve)
    assert cvc.clip_path("ch1", "../comparison") is None
    assert cvc.clip_path("ch1", "some-other-model") is None


def test_get_comparison_overlays_billed_price(store, monkeypatch):
    class _S:
        effective_price = 0.72
        n_calls = 40

    monkeypatch.setattr(
        "hypogeum.model_effective_pricing.effective_prices",
        lambda **kw: {"wan2.7-text-to-video": _S()},
    )
    r = cvc.get_comparison("ch1")
    assert r["current"]["price_per_call"] == 0.72
    assert r["current"]["billed_calls"] == 40
    assert r["current"]["cost_per_shot_total"] == 0.77
