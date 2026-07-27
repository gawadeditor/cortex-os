"""
hypogeum/agents/entity_continuity_manager.py — Entity continuity utilities (Priority 8 extraction)

Extracted from visual_package_director.py. Responsible for:
  - Reference-image eligibility flags (lock_state == "final" gate)
  - Filtering entities by lock state
  - Building rich visual spec text from entity records for ref image models
  - Entity-to-scene mapping: resolving primary entity URL for a scene
  - Updating entity lock records with reference_image_asset_id after ref image generation

Not responsible for:
  - Scene list construction (scene_planner)
  - Prompt writing (VPD / prompt files)
  - Provider routing (VPD / _build_scene_spec)
  - Executing MUAPI reference image generation (VPD / muapi.generate_reference_image)
  - Visual QA
  - Final visual_package assembly

Public API:
    build_visual_spec_text(entity)
    is_ref_image_eligible(entity)
    get_final_entities(locked_entities)
    get_primary_entity_url(scene, entity_hosted_urls)
    apply_reference_image_asset_ids(locked_entities, entity_hosted_urls, episode_id)
"""

from __future__ import annotations

from typing import Optional

from hypogeum.brand import visual_identity


# ---------------------------------------------------------------------------
# Visual spec text construction
# ---------------------------------------------------------------------------

def build_visual_spec_text(entity: dict) -> str:
    """
    Build a rich text description from an entity's visual_spec for use
    as input to a reference image model.

    Combines description, period_detail, and distinctive_markers into a
    single string. Each component is appended only when non-empty.

    Args:
        entity: Entity dict containing a "visual_spec" sub-dict.

    Returns:
        Rich text description string.
    """
    visual_spec = entity.get("visual_spec", {})
    description   = visual_spec.get("description", "")
    period_detail = visual_spec.get("period_detail", "")
    markers       = ", ".join(visual_spec.get("distinctive_markers", []))

    text = description
    if period_detail:
        text += f" {period_detail}"
    if markers:
        text += f" Distinctive features: {markers}."

    if not text:
        return text

    return text + visual_identity.build_visual_identity_directive(mode="reference_image")


# ---------------------------------------------------------------------------
# Lock state / eligibility
# ---------------------------------------------------------------------------

def is_ref_image_eligible(entity: dict) -> bool:
    """
    Return True iff the entity's lock_state is "final".

    Only final-locked entities are eligible for reference image generation.
    Provisional and promoted entities are skipped.

    Args:
        entity: Entity dict with a "lock_state" field.

    Returns:
        True if lock_state == "final", False otherwise.
    """
    return entity.get("lock_state") == "final"


def get_final_entities(locked_entities: list) -> list:
    """
    Return the subset of locked_entities whose lock_state is "final".

    Preserves order. Returns the same dict objects (no deep copy).

    Args:
        locked_entities: List of entity dicts from visual_continuity_record.

    Returns:
        List of entities with lock_state == "final".
    """
    return [e for e in locked_entities if is_ref_image_eligible(e)]


# ---------------------------------------------------------------------------
# Entity-to-scene mapping
# ---------------------------------------------------------------------------

def get_primary_entity_url(
    scene: dict,
    entity_hosted_urls: dict[str, str],
) -> Optional[str]:
    """
    Return the hosted reference image URL for the first primary entity in a scene.

    Searches entity_appearances for the first entry with role == "primary" and
    returns its URL from entity_hosted_urls. Returns None if no primary entity
    is found or if the primary entity has no hosted URL.

    Args:
        scene:              Scene dict containing "entity_appearances" list.
        entity_hosted_urls: Mapping of entity_id → hosted_url from ref image generation.

    Returns:
        Hosted URL string, or None.
    """
    for ea in scene.get("entity_appearances", []):
        if ea.get("role") == "primary":
            return entity_hosted_urls.get(ea.get("entity_id", ""))
    return None


# ---------------------------------------------------------------------------
# Entity lock record update
# ---------------------------------------------------------------------------

def apply_reference_image_asset_ids(
    locked_entities: list,
    entity_hosted_urls: dict[str, str],
    episode_id: str,
) -> None:
    """
    Update reference_image_asset_id in-place on each final-locked entity
    that has a corresponding entry in entity_hosted_urls.

    The asset_id follows the path convention:
        episodes/{episode_id}/video/ref_{entity_id}

    Only final-locked entities whose ref image generation succeeded (i.e.
    their entity_id appears in entity_hosted_urls) are updated.

    Args:
        locked_entities:    List of entity dicts from visual_continuity_record.
        entity_hosted_urls: Mapping of entity_id → hosted_url (generation successes only).
        episode_id:         Episode identifier used to construct the asset path.
    """
    for entity in locked_entities:
        if not is_ref_image_eligible(entity):
            continue
        entity_id = entity.get("entity_id", "")
        if entity_id and entity_id in entity_hosted_urls:
            entity["reference_image_asset_id"] = (
                f"episodes/{episode_id}/video/ref_{entity_id}"
            )
