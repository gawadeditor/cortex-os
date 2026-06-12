"""
tests/test_provider_router.py — Unit tests for provider_router.py

Coverage:
  TestNormalizeTier          — int coercion with defaults
  TestBuildSceneSpec         — full scene_spec construction from planned scene
  TestUpdateRoutingManifest  — provider_routing_manifest recalculation from asset manifest
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.provider_router import (
    build_scene_spec,
    normalize_tier,
    update_routing_manifest,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scene(
    scene_id: str = "s01",
    source_type: str = "main_episode",
    short_id: Optional[str] = None,
    aspect_ratio: str = "16:9",
    duration: float = 8.0,
    primary_tier: int = 2,
    fallback_tier: int = 3,
    primary_prompt: str = "Ancient throne room, dramatic lighting.",
    fallback_prompt: str = "Egyptian palace interior.",
    entity_appearances: Optional[list] = None,
) -> dict:
    return {
        "scene_id": scene_id,
        "source_type": source_type,
        "short_id": short_id,
        "entity_appearances": entity_appearances or [],
        "production_spec": {
            "aspect_ratio": aspect_ratio,
            "duration_seconds": duration,
            "provider_routing": {
                "primary_provider": "muapi",
                "primary_tier": primary_tier,
                "primary_prompt": primary_prompt,
                "fallback_provider": "muapi",
                "fallback_tier": fallback_tier,
                "fallback_prompt": fallback_prompt,
                "routing_rationale": "Standard tier selection.",
            },
        },
    }


def _make_visual_package(assets: list) -> dict:
    return {
        "asset_manifest": {"assets": assets},
        "provider_routing_manifest": {
            "scenes_by_provider": {"muapi": 0},
            "scenes_by_tier": {"1": 0, "2": 0, "3": 0},
        },
    }


def _make_asset(generation_status: str = "generated") -> dict:
    return {
        "asset_id": "ep/video/sc_s01",
        "generation_status": generation_status,
        "used_in_scenes": ["s01"],
    }


# ---------------------------------------------------------------------------
# TestNormalizeTier
# ---------------------------------------------------------------------------

class TestNormalizeTier:

    def test_int_passthrough(self):
        assert normalize_tier(2) == 2

    def test_tier_1(self):
        assert normalize_tier(1) == 1

    def test_tier_3(self):
        assert normalize_tier(3) == 3

    def test_string_coerced_to_int(self):
        assert normalize_tier("2") == 2

    def test_float_coerced_to_int(self):
        assert normalize_tier(2.0) == 2

    def test_none_uses_default(self):
        assert normalize_tier(None, default=2) == 2

    def test_none_uses_explicit_default_3(self):
        assert normalize_tier(None, default=3) == 3

    def test_missing_value_uses_default(self):
        routing = {}
        assert normalize_tier(routing.get("primary_tier"), default=2) == 2

    def test_returns_int_type(self):
        result = normalize_tier(2)
        assert type(result) is int

    def test_string_tier_returns_int_type(self):
        result = normalize_tier("1")
        assert type(result) is int


# ---------------------------------------------------------------------------
# TestBuildSceneSpec
# ---------------------------------------------------------------------------

class TestBuildSceneSpec:

    # --- scene_id / short_id ---

    def test_scene_id_preserved(self):
        scene = _make_scene(scene_id="s42")
        spec = build_scene_spec(scene, {})
        assert spec["scene_id"] == "s42"

    def test_short_id_preserved(self):
        scene = _make_scene(scene_id="sh_01_s01", source_type="short", short_id="sh_01")
        spec = build_scene_spec(scene, {})
        assert spec["short_id"] == "sh_01"

    def test_main_episode_short_id_is_none(self):
        scene = _make_scene(source_type="main_episode")
        spec = build_scene_spec(scene, {})
        assert spec["short_id"] is None

    # --- visual_spec ---

    def test_aspect_ratio_main_episode(self):
        scene = _make_scene(source_type="main_episode", aspect_ratio="16:9")
        spec = build_scene_spec(scene, {})
        assert spec["visual_spec"]["aspect_ratio"] == "16:9"

    def test_aspect_ratio_short(self):
        scene = _make_scene(source_type="short", short_id="sh_01", aspect_ratio="9:16")
        spec = build_scene_spec(scene, {})
        assert spec["visual_spec"]["aspect_ratio"] == "9:16"

    def test_duration_seconds_as_float(self):
        scene = _make_scene(duration=5.0)
        spec = build_scene_spec(scene, {})
        assert spec["visual_spec"]["duration_seconds"] == 5.0
        assert isinstance(spec["visual_spec"]["duration_seconds"], float)

    def test_duration_int_coerced_to_float(self):
        scene = _make_scene(duration=8)
        spec = build_scene_spec(scene, {})
        assert isinstance(spec["visual_spec"]["duration_seconds"], float)

    def test_duration_default_8_when_missing(self):
        scene = _make_scene()
        del scene["production_spec"]["duration_seconds"]
        spec = build_scene_spec(scene, {})
        assert spec["visual_spec"]["duration_seconds"] == 8.0

    def test_aspect_ratio_default_16_9_when_missing(self):
        scene = _make_scene()
        del scene["production_spec"]["aspect_ratio"]
        spec = build_scene_spec(scene, {})
        assert spec["visual_spec"]["aspect_ratio"] == "16:9"

    # --- production_spec routing ---

    def test_primary_prompt_extracted(self):
        scene = _make_scene(primary_prompt="Pharaoh on golden throne.")
        spec = build_scene_spec(scene, {})
        assert spec["production_spec"]["primary_prompt"] == "Pharaoh on golden throne."

    def test_fallback_prompt_extracted(self):
        scene = _make_scene(fallback_prompt="Egyptian palace.")
        spec = build_scene_spec(scene, {})
        assert spec["production_spec"]["fallback_prompt"] == "Egyptian palace."

    def test_primary_tier_normalized_to_int(self):
        scene = _make_scene(primary_tier=2)
        spec = build_scene_spec(scene, {})
        assert spec["production_spec"]["primary_tier"] == 2
        assert type(spec["production_spec"]["primary_tier"]) is int

    def test_fallback_tier_normalized_to_int(self):
        scene = _make_scene(fallback_tier=3)
        spec = build_scene_spec(scene, {})
        assert spec["production_spec"]["fallback_tier"] == 3
        assert type(spec["production_spec"]["fallback_tier"]) is int

    def test_primary_tier_default_2_when_missing(self):
        scene = _make_scene()
        del scene["production_spec"]["provider_routing"]["primary_tier"]
        spec = build_scene_spec(scene, {})
        assert spec["production_spec"]["primary_tier"] == 2

    def test_fallback_tier_default_3_when_missing(self):
        scene = _make_scene()
        del scene["production_spec"]["provider_routing"]["fallback_tier"]
        spec = build_scene_spec(scene, {})
        assert spec["production_spec"]["fallback_tier"] == 3

    def test_primary_tier_1(self):
        scene = _make_scene(primary_tier=1)
        spec = build_scene_spec(scene, {})
        assert spec["production_spec"]["primary_tier"] == 1

    def test_primary_prompt_empty_when_missing(self):
        scene = _make_scene()
        del scene["production_spec"]["provider_routing"]["primary_prompt"]
        spec = build_scene_spec(scene, {})
        assert spec["production_spec"]["primary_prompt"] == ""

    def test_fallback_prompt_empty_when_missing(self):
        scene = _make_scene()
        del scene["production_spec"]["provider_routing"]["fallback_prompt"]
        spec = build_scene_spec(scene, {})
        assert spec["production_spec"]["fallback_prompt"] == ""

    # --- reference_image_url (entity-to-scene mapping delegated to ECM) ---

    def test_reference_image_url_injected_for_primary_entity(self):
        scene = _make_scene(entity_appearances=[
            {"entity_id": "pharaoh_amenhotep", "role": "primary"},
        ])
        urls = {"pharaoh_amenhotep": "https://cdn.muapi.ai/ref.jpg"}
        spec = build_scene_spec(scene, urls)
        assert spec["production_spec"]["reference_image_url"] == "https://cdn.muapi.ai/ref.jpg"

    def test_no_injection_for_secondary_entity(self):
        scene = _make_scene(entity_appearances=[
            {"entity_id": "pharaoh_amenhotep", "role": "secondary"},
        ])
        urls = {"pharaoh_amenhotep": "https://cdn.muapi.ai/ref.jpg"}
        spec = build_scene_spec(scene, urls)
        assert spec["production_spec"]["reference_image_url"] is None

    def test_no_injection_when_entity_has_no_hosted_url(self):
        scene = _make_scene(entity_appearances=[
            {"entity_id": "pharaoh_amenhotep", "role": "primary"},
        ])
        spec = build_scene_spec(scene, {})
        assert spec["production_spec"]["reference_image_url"] is None

    def test_no_entity_appearances_gives_none_url(self):
        scene = _make_scene(entity_appearances=[])
        spec = build_scene_spec(scene, {"e1": "https://cdn/r.jpg"})
        assert spec["production_spec"]["reference_image_url"] is None

    def test_first_primary_entity_wins(self):
        scene = _make_scene(entity_appearances=[
            {"entity_id": "entity_a", "role": "secondary"},
            {"entity_id": "entity_b", "role": "primary"},
            {"entity_id": "entity_c", "role": "primary"},
        ])
        urls = {
            "entity_b": "https://cdn/b.jpg",
            "entity_c": "https://cdn/c.jpg",
        }
        spec = build_scene_spec(scene, urls)
        assert spec["production_spec"]["reference_image_url"] == "https://cdn/b.jpg"

    # --- missing production_spec / routing ---

    def test_missing_production_spec_uses_defaults(self):
        scene = {"scene_id": "s01", "short_id": None, "entity_appearances": []}
        spec = build_scene_spec(scene, {})
        assert spec["visual_spec"]["aspect_ratio"] == "16:9"
        assert spec["visual_spec"]["duration_seconds"] == 8.0
        assert spec["production_spec"]["primary_tier"] == 2
        assert spec["production_spec"]["fallback_tier"] == 3

    def test_missing_provider_routing_uses_defaults(self):
        scene = _make_scene()
        del scene["production_spec"]["provider_routing"]
        spec = build_scene_spec(scene, {})
        assert spec["production_spec"]["primary_prompt"] == ""
        assert spec["production_spec"]["primary_tier"] == 2
        assert spec["production_spec"]["fallback_tier"] == 3

    # --- output structure ---

    def test_output_has_scene_id_key(self):
        spec = build_scene_spec(_make_scene(), {})
        assert "scene_id" in spec

    def test_output_has_short_id_key(self):
        spec = build_scene_spec(_make_scene(), {})
        assert "short_id" in spec

    def test_output_has_visual_spec_key(self):
        spec = build_scene_spec(_make_scene(), {})
        assert "visual_spec" in spec

    def test_output_has_production_spec_key(self):
        spec = build_scene_spec(_make_scene(), {})
        assert "production_spec" in spec

    def test_visual_spec_has_expected_keys(self):
        spec = build_scene_spec(_make_scene(), {})
        assert set(spec["visual_spec"].keys()) == {"aspect_ratio", "duration_seconds"}

    def test_production_spec_has_expected_keys(self):
        spec = build_scene_spec(_make_scene(), {})
        assert set(spec["production_spec"].keys()) == {
            "primary_prompt", "primary_tier",
            "fallback_prompt", "fallback_tier",
            "reference_image_url",
        }


# ---------------------------------------------------------------------------
# TestUpdateRoutingManifest
# ---------------------------------------------------------------------------

class TestUpdateRoutingManifest:

    def test_generated_count_correct(self):
        vp = _make_visual_package([
            _make_asset("generated"),
            _make_asset("generated"),
            _make_asset("generation_failed"),
        ])
        update_routing_manifest(vp)
        assert vp["provider_routing_manifest"]["scenes_generated"] == 2

    def test_failed_count_correct(self):
        vp = _make_visual_package([
            _make_asset("generated"),
            _make_asset("generation_failed"),
            _make_asset("generation_failed"),
        ])
        update_routing_manifest(vp)
        assert vp["provider_routing_manifest"]["scenes_failed"] == 2

    def test_all_generated(self):
        vp = _make_visual_package([_make_asset("generated")] * 5)
        update_routing_manifest(vp)
        assert vp["provider_routing_manifest"]["scenes_generated"] == 5
        assert vp["provider_routing_manifest"]["scenes_failed"] == 0

    def test_all_failed(self):
        vp = _make_visual_package([_make_asset("generation_failed")] * 3)
        update_routing_manifest(vp)
        assert vp["provider_routing_manifest"]["scenes_generated"] == 0
        assert vp["provider_routing_manifest"]["scenes_failed"] == 3

    def test_empty_assets_gives_zero_counts(self):
        vp = _make_visual_package([])
        update_routing_manifest(vp)
        assert vp["provider_routing_manifest"]["scenes_generated"] == 0
        assert vp["provider_routing_manifest"]["scenes_failed"] == 0

    def test_creates_manifest_if_missing(self):
        vp = {"asset_manifest": {"assets": [_make_asset("generated")]}}
        update_routing_manifest(vp)
        assert "provider_routing_manifest" in vp
        assert vp["provider_routing_manifest"]["scenes_generated"] == 1

    def test_planned_status_not_counted(self):
        vp = _make_visual_package([
            _make_asset("generated"),
            _make_asset("planned"),
        ])
        update_routing_manifest(vp)
        assert vp["provider_routing_manifest"]["scenes_generated"] == 1
        assert vp["provider_routing_manifest"]["scenes_failed"] == 0

    def test_modifies_package_in_place(self):
        vp = _make_visual_package([_make_asset("generated")])
        original_manifest = vp["provider_routing_manifest"]
        update_routing_manifest(vp)
        assert vp["provider_routing_manifest"] is original_manifest

    def test_overwrites_previous_counts(self):
        vp = _make_visual_package([_make_asset("generated")])
        vp["provider_routing_manifest"]["scenes_generated"] = 99
        vp["provider_routing_manifest"]["scenes_failed"] = 99
        update_routing_manifest(vp)
        assert vp["provider_routing_manifest"]["scenes_generated"] == 1
        assert vp["provider_routing_manifest"]["scenes_failed"] == 0

    def test_missing_asset_manifest_gives_zero_counts(self):
        vp = {}
        update_routing_manifest(vp)
        assert vp["provider_routing_manifest"]["scenes_generated"] == 0
        assert vp["provider_routing_manifest"]["scenes_failed"] == 0
