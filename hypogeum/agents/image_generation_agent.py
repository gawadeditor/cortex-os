"""
hypogeum/agents/image_generation_agent.py — Reference image generation (Priority 11 extraction)

Extracted from visual_package_director.py. Responsible for:
  - Iterating final-locked entities and calling generate_reference_image()
  - Building the entity_hosted_urls map ({entity_id: hosted_url})
  - Per-entity logging (vpd_ref_image_started / completed / failed)
  - Halt on MUAPIAuthFailure / MUAPIQuotaExhausted (propagated immediately)
  - Non-halt on all other exceptions (entity skipped, loop continues)

Not responsible for:
  - Entity lock-state rules (entity_continuity_manager)
  - Scene video generation (video_generation_agent)
  - Provider routing (provider_router)
  - Scene prompts or scene planning (scene_planner)
  - Visual package final assembly (visual_package_director)
  - Writing reference_image_asset_id back onto entities
    (entity_continuity_manager.apply_reference_image_asset_ids)

MUAPI attribution: agent = "visual_package_director" (preserved from pre-extraction
attribution — continuity with existing MUAPI audit logs).

Public API:
    generate_entity_reference_images(
        locked_entities, config, episode_id, output_dir, log,
        budget_guard=None,
    ) -> dict[str, str]
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.agents.entity_continuity_manager import (
    build_visual_spec_text,
    is_ref_image_eligible,
)
from hypogeum.config import Config
from hypogeum.muapi import (
    MUAPIAuthFailure,
    MUAPIBudgetGuard,
    MUAPIQuotaExhausted,
    generate_reference_image,
)

# Attribution passed to MUAPI — generation is driven by the Visual Package Director.
_AGENT_NAME = "visual_package_director"


def generate_entity_reference_images(
    locked_entities: list,
    config: Config,
    episode_id: str,
    output_dir: Path,
    log,
    budget_guard: Optional[MUAPIBudgetGuard] = None,
) -> dict[str, str]:
    """
    Generate reference images for all final-locked entities.

    Iterates locked_entities, skipping any whose lock_state is not "final".
    For each eligible entity, calls generate_reference_image() and stores
    the returned hosted_url in the result dict.

    Does NOT raise on individual entity failure — logs vpd_ref_image_failed
    and continues to the next entity.

    Raises MUAPIAuthFailure / MUAPIQuotaExhausted immediately — these are
    halt conditions that abort the pipeline. All other exceptions (including
    MUAPIBudgetExhausted) are caught, logged as vpd_ref_image_failed, and
    the loop continues with the next entity.

    Args:
        locked_entities: List of entity dicts from visual_continuity_record.
        config:          Loaded Config instance (MUAPI credentials).
        episode_id:      Episode identifier for MUAPI attribution and logging.
        output_dir:      Output directory for local asset storage.
        log:             Logger instance.
        budget_guard:    Optional MUAPIBudgetGuard (M5.6).

    Returns:
        Dict mapping entity_id → hosted_url for every entity whose reference
        image was successfully generated.
    """
    entity_hosted_urls: dict[str, str] = {}

    for entity in locked_entities:
        if not is_ref_image_eligible(entity):
            continue

        entity_id        = entity["entity_id"]
        visual_spec_text = build_visual_spec_text(entity)

        log.info(
            "vpd_ref_image_started",
            entity_id=entity_id,
            lock_state="final",
        )

        try:
            result = generate_reference_image(
                entity_id=entity_id,
                visual_spec_text=visual_spec_text,
                config=config,
                episode_id=episode_id,
                output_dir=output_dir,
                agent=_AGENT_NAME,
                budget_guard=budget_guard,
            )
            entity_hosted_urls[entity_id] = result.hosted_url
            log.info(
                "vpd_ref_image_completed",
                entity_id=entity_id,
                asset_id=result.asset_id,
            )
        except (MUAPIAuthFailure, MUAPIQuotaExhausted):
            raise  # halt conditions — propagate immediately
        except Exception as exc:
            log.warn(
                "vpd_ref_image_failed",
                entity_id=entity_id,
                error=str(exc),
            )
            # Continue — scene generation proceeds without conditioning for this entity

    return entity_hosted_urls
