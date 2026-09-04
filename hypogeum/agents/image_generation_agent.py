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
        budget_guard=None, video_aspect_ratio="16:9", channel_id=None,
    ) -> dict[str, str]
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import hypogeum.asset_registry as _asset_registry
from hypogeum.agents.entity_continuity_manager import (
    build_character_sheet_prompt,
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_entity_reference_images(
    locked_entities: list,
    config: Config,
    episode_id: str,
    output_dir: Path,
    log,
    budget_guard: Optional[MUAPIBudgetGuard] = None,
    video_aspect_ratio: str = "16:9",
    channel_id: Optional[str] = None,
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
        video_aspect_ratio: The channel's real deliverable orientation
                         ("16:9" main episode, "9:16" short-native) —
                         forwarded to generate_reference_image() so the
                         reference image is generated in the SAME
                         orientation the video it seeds will use. Default
                         "16:9" only for backward compatibility with a
                         caller that hasn't been updated to pass the real
                         value; every real production caller (VPD's own
                         Phase 2) always supplies it explicitly. Fixed
                         2026-09-05 — previously never threaded through at
                         all, so a short-native/portrait channel's
                         reference images were always generated 16:9
                         regardless, and any I2V scene conditioned on one
                         inherited a distorted seed image no matter which
                         I2V model was used.
        channel_id:      Forwarded to generate_reference_image()'s own
                         channel_id parameter (Authorization Authority gate
                         path). Optional; every existing caller that omits
                         it is unaffected.

    Returns:
        Dict mapping entity_id → hosted_url for every entity whose reference
        image was successfully generated.
    """
    entity_hosted_urls: dict[str, str] = {}

    for entity in locked_entities:
        if not is_ref_image_eligible(entity):
            continue

        entity_id        = entity["entity_id"]
        visual_spec_text = build_character_sheet_prompt(entity)

        log.info(
            "vpd_ref_image_started",
            entity_id=entity_id,
            lock_state="final",
        )

        attempted_at = _now_iso()
        asset_slot_id = _asset_registry.compute_asset_slot_id(
            episode_id, "reference_image", "muapi", variant=entity_id,
        )
        version_id = _asset_registry.register_asset_attempt(
            asset_slot_id=asset_slot_id,
            asset_type="reference_image",
            provider="muapi",
            attempt_number=1,
            version_index=0,
            attempted_at=attempted_at,
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
                video_aspect_ratio=video_aspect_ratio,
                channel_id=channel_id,
            )
            entity_hosted_urls[entity_id] = result.hosted_url
            log.info(
                "vpd_ref_image_completed",
                entity_id=entity_id,
                asset_id=result.asset_id,
            )
            _asset_registry.register_asset_success(
                asset_slot_id=asset_slot_id,
                version_id=version_id,
                asset_id=result.asset_id,
                asset_type="reference_image",
                provider="muapi",
                attempted_at=attempted_at,
                reuse_eligible=True,
            )
        except (MUAPIAuthFailure, MUAPIQuotaExhausted) as exc:
            _asset_registry.register_asset_failure(
                asset_slot_id=asset_slot_id,
                version_id=version_id,
                asset_type="reference_image",
                provider="muapi",
                error_type=type(exc).__name__,
                error_message=str(exc),
                attempted_at=attempted_at,
            )
            raise  # halt conditions — propagate immediately
        except Exception as exc:
            log.warn(
                "vpd_ref_image_failed",
                entity_id=entity_id,
                error=str(exc),
            )
            _asset_registry.register_asset_failure(
                asset_slot_id=asset_slot_id,
                version_id=version_id,
                asset_type="reference_image",
                provider="muapi",
                error_type=type(exc).__name__,
                error_message=str(exc),
                attempted_at=attempted_at,
            )
            # Continue — scene generation proceeds without conditioning for this entity

    return entity_hosted_urls
