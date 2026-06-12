"""
hypogeum/agents/provider_router.py — Provider tier routing utilities (Priority 9 extraction)

Extracted from visual_package_director.py. Responsible for:
  - T1/T2/T3 routing interpretation
  - primary_tier / fallback_tier normalization
  - provider routing manifest calculations
  - Building the scene_spec production_spec from a planned scene's provider_routing
  - Fallback routing rules (default tiers when absent)

Not responsible for:
  - Scene planning (scene_planner)
  - Prompt writing (VPD / prompt files)
  - Entity continuity (entity_continuity_manager)
  - MUAPI generation execution
  - Asset manifest writing
  - Visual QA
  - Final visual_package assembly

Public API:
    normalize_tier(value, default)
    build_scene_spec(scene, entity_hosted_urls)
    update_routing_manifest(visual_package)
"""

from __future__ import annotations

from typing import Optional

from hypogeum.agents.entity_continuity_manager import get_primary_entity_url


# ---------------------------------------------------------------------------
# Tier normalization
# ---------------------------------------------------------------------------

def normalize_tier(value, default: int = 2) -> int:
    """
    Coerce a tier value to int, falling back to *default* when the value is
    None or absent.

    Handles int, float, and numeric-string inputs (e.g. "2" → 2).
    Valid production tiers are 1, 2, and 3.

    Args:
        value:   Raw tier value from a provider_routing dict (may be None).
        default: Fallback int to return when value is None.

    Returns:
        Integer tier value.
    """
    if value is None:
        return default
    return int(value)


# ---------------------------------------------------------------------------
# Scene spec construction
# ---------------------------------------------------------------------------

def build_scene_spec(scene: dict, entity_hosted_urls: dict[str, str]) -> dict:
    """
    Construct the scene_spec dict expected by muapi.submit_scene() from a
    planned scene dict.

    Normalizes tier values to int, applies default fallback routing rules when
    routing fields are absent, and delegates entity-to-scene URL mapping to
    entity_continuity_manager.get_primary_entity_url().

    Args:
        scene:              Planned scene dict from the visual_package.
        entity_hosted_urls: entity_id → hosted_url mapping from ref image generation.

    Returns:
        scene_spec dict with keys: scene_id, short_id, visual_spec, production_spec.
    """
    prod_spec = scene.get("production_spec", {})
    routing   = prod_spec.get("provider_routing", {})

    return {
        "scene_id":  scene["scene_id"],
        "short_id":  scene.get("short_id"),
        "visual_spec": {
            "aspect_ratio":     prod_spec.get("aspect_ratio", "16:9"),
            "duration_seconds": float(prod_spec.get("duration_seconds", 8.0)),
        },
        "production_spec": {
            "primary_prompt":      routing.get("primary_prompt", ""),
            "primary_tier":        normalize_tier(routing.get("primary_tier"), default=2),
            "fallback_prompt":     routing.get("fallback_prompt", ""),
            "fallback_tier":       normalize_tier(routing.get("fallback_tier"), default=3),
            "reference_image_url": get_primary_entity_url(scene, entity_hosted_urls),
        },
    }


# ---------------------------------------------------------------------------
# Routing manifest update
# ---------------------------------------------------------------------------

def update_routing_manifest(visual_package: dict) -> None:
    """
    Recount scenes_generated and scenes_failed in provider_routing_manifest
    from the actual asset generation results in asset_manifest.

    Called after all scenes have been processed in Phase 2 to reflect actual
    generation outcomes rather than planned estimates.

    Args:
        visual_package: Visual Package dict (modified in place).
    """
    assets    = visual_package.get("asset_manifest", {}).get("assets", [])
    generated = sum(1 for a in assets if a.get("generation_status") == "generated")
    failed    = sum(1 for a in assets if a.get("generation_status") == "generation_failed")

    manifest = visual_package.setdefault("provider_routing_manifest", {})
    manifest["scenes_generated"] = generated
    manifest["scenes_failed"]    = failed
