"""
hypogeum/agents/entity_continuity_manager.py — Entity continuity utilities (Priority 8 extraction)

Extracted from visual_package_director.py. Responsible for:
  - Reference-image eligibility flags (lock_state == "final" gate)
  - Filtering entities by lock state
  - Building rich visual spec text from entity records: build_character_
    sheet_prompt (the current, live reference-image prompt — a multi-angle
    studio-shot framing, not a single flat pose), build_visual_spec_text
    (the older single-pose framing, no longer called from the production
    path but kept for any other caller), and build_entity_description_
    for_scene / inject_entity_descriptions_into_scenes (direct video-
    scene-prompt text injection, additive to I2V, not a replacement for it)
  - Entity-to-scene mapping: resolving primary entity URL for a scene
  - Updating entity lock records with reference_image_asset_id after ref image generation

History (2026-09-04/05): I2V (reference-image conditioning) was briefly
retired after real production episodes showed the reference image was
generated in the wrong aspect ratio regardless of channel orientation
(hypogeum.muapi.generate_reference_image()'s video_aspect_ratio was never
threaded through from the channel's real orientation — fixed in
hypogeum.agents.image_generation_agent, see that module's own docstring),
then restored the same day once the actual bug was understood and the
operator asked for I2V models to be properly qualified per channel
(Video Model Bake-off) rather than removed. The text-injection mechanism
added during the brief retirement was kept — it is additive (entity
consistency is now carried by BOTH a text description in the scene prompt
AND a real multi-angle reference image), not a replacement.

Not responsible for:
  - Scene list construction (scene_planner)
  - Prompt writing (VPD / prompt files)
  - Provider routing (VPD / _build_scene_spec)
  - Executing MUAPI reference image generation (VPD / muapi.generate_reference_image)
  - Visual QA
  - Final visual_package assembly

Public API:
    build_character_sheet_prompt(entity)
    build_visual_spec_text(entity)
    build_entity_description_for_scene(entity)
    inject_entity_descriptions_into_scenes(scenes, locked_entities)
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

def _build_core_entity_description(entity: dict) -> str:
    """Description + period_detail + distinctive_markers, combined into
    one string. Each component is appended only when non-empty. Shared
    core for build_visual_spec_text() (reference-image mode, historical)
    and build_entity_description_for_scene() (video-prompt mode, current
    production path — see that function's own docstring)."""
    visual_spec = entity.get("visual_spec", {})
    description   = visual_spec.get("description", "")
    period_detail = visual_spec.get("period_detail", "")
    markers       = ", ".join(visual_spec.get("distinctive_markers", []))

    text = description
    if period_detail:
        text += f" {period_detail}"
    if markers:
        text += f" Distinctive features: {markers}."
    return text


def build_visual_spec_text(entity: dict) -> str:
    """
    Build a rich text description from an entity's visual_spec for use
    as input to a reference image model — a single-pose framing (no
    multi-angle composition instruction).

    Combines description, period_detail, and distinctive_markers into a
    single string, then appends the reference-image-mode visual identity
    directive. Each component is appended only when non-empty.

    The current production path calls build_character_sheet_prompt()
    instead (a proper multi-angle studio-shot framing — a single flat
    pose gives an I2V model much weaker material to condition scenes on
    than a real turnaround/reference sheet). Kept for any other direct
    caller that specifically wants the plain single-pose framing.

    Args:
        entity: Entity dict containing a "visual_spec" sub-dict.

    Returns:
        Rich text description string.
    """
    text = _build_core_entity_description(entity)

    if not text:
        return text

    return text + visual_identity.build_visual_identity_directive(mode="reference_image")


def build_character_sheet_prompt(entity: dict) -> str:
    """
    Build the reference-image generation prompt as a "Multi-Angle Studio
    Shot" — a proper character/object reference sheet showing the subject
    from several angles on one plain, neutrally-lit background, rather
    than build_visual_spec_text()'s single flat pose.

    This is the current production prompt for I2V reference-image
    generation (hypogeum.agents.image_generation_agent.
    generate_entity_reference_images() -> hypogeum.muapi.
    generate_reference_image()): a single-pose image gives an I2V model
    far weaker material to hold a subject consistent across many
    different camera angles/scenes than a real multi-view reference does
    — this is the standard character-sheet/turnaround technique used for
    exactly that reason.

    Wording is entity_type-neutral ("the subject") so it reads naturally
    for both a person/character entity and an inanimate object entity —
    entity_continuity_manager tracks both under the same lock-state
    mechanism, and multi-angle studio reference photography is an
    equally standard technique for either.

    Args:
        entity: Entity dict containing a "visual_spec" sub-dict.

    Returns:
        The character-sheet prompt string, or "" if the entity's
        visual_spec has no usable description (same empty-input
        contract as build_visual_spec_text()/build_entity_description_
        for_scene()).
    """
    core = _build_core_entity_description(entity)
    if not core:
        return core

    return (
        "Character reference sheet, multi-angle studio shot. "
        f"Subject: {core} "
        "Show the same subject from multiple angles — front view, three-quarter "
        "view, and side profile — arranged clearly on a single plain, neutral "
        "background with consistent, even studio lighting. No scene, no "
        "environment, no props, no narrative action: a clean reference sheet "
        "for maintaining this exact subject's appearance consistent across "
        "separately generated video scenes."
        + visual_identity.build_visual_identity_directive(mode="reference_image")
    )


def build_entity_description_for_scene(entity: dict) -> str:
    """
    Core entity description text for injection directly into a VIDEO
    scene prompt (production_spec.provider_routing.primary_prompt /
    fallback_prompt) — the replacement for I2V reference-image
    conditioning (retired 2026-09-04: repeated real production episodes
    showed reference images were generated in the wrong aspect ratio
    regardless of channel orientation — hypogeum.muapi.
    generate_reference_image()'s video_aspect_ratio was never actually
    threaded through from the channel's real orientation, so an I2V scene
    always inherited a distorted seed image no matter which I2V model was
    used). A precise, deterministic text description repeated verbatim in
    every scene featuring that entity carries no aspect-ratio dependency
    and no per-model image-conditioning field-name quirks.

    Deliberately does NOT append build_visual_spec_text()'s
    visual_identity directive tail: that directive is reference-image-
    mode brand guidance, and a video scene prompt already gets its own,
    separate, video-scene-mode Brand DNA section from
    hypogeum.video_prompt_writer.assemble_video_scene_prompt() —
    appending the reference-image-mode one here would be a redundant,
    wrong-mode duplicate.
    """
    return _build_core_entity_description(entity)


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


def inject_entity_descriptions_into_scenes(scenes: list, locked_entities: list) -> None:
    """
    Append each scene's primary final-locked entity's visual description
    directly into that scene's own primary_prompt / fallback_prompt text,
    in place — the current production mechanism for entity visual
    consistency (I2V retired 2026-09-04; see build_entity_description_
    for_scene()'s own docstring for why).

    MUST run before hypogeum.production_plan.compile_production_plan():
    the plan's prompt_fingerprint is computed from the prompt text as it
    exists at compile time, and hypogeum.muapi._submit_scene_via_gate()'s
    plan-conformance check fail-closed rejects (prompt_mismatch) any
    submission whose real, fully-assembled prompt doesn't match what the
    plan declared — so the injected text must already be present in the
    plan's own declared prompt, not added afterward.

    Same eligibility gate as the retired I2V path (is_ref_image_eligible:
    only "final"-locked entities) and the same "first primary
    entity_appearance only" scope get_primary_entity_url() used — this
    is a mechanism swap (text instead of an image), not an expansion of
    what gets carried for consistency.

    Idempotent (skips a prompt that already contains the description
    text verbatim) and defensive (a malformed scene or entity dict is
    skipped, never fatal to the whole episode) — never raises.

    Args:
        scenes:          visual_package["scenes"], mutated in place.
        locked_entities: visual_package["visual_continuity_record"]["locked_entities"].
    """
    try:
        entities_by_id = {
            e.get("entity_id"): e for e in locked_entities if is_ref_image_eligible(e)
        }
    except Exception:  # noqa: BLE001
        return

    for scene in scenes:
        try:
            entity_id = None
            for ea in scene.get("entity_appearances", []) or []:
                if ea.get("role") == "primary":
                    entity_id = ea.get("entity_id")
                    break
            if not entity_id:
                continue
            entity = entities_by_id.get(entity_id)
            if not entity:
                continue
            description = build_entity_description_for_scene(entity)
            if not description:
                continue

            routing = scene.get("production_spec", {}).get("provider_routing", {})
            for key in ("primary_prompt", "fallback_prompt"):
                current = routing.get(key, "") or ""
                if current and description not in current:
                    routing[key] = f"{current} {description}"
        except Exception:  # noqa: BLE001
            continue


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
