"""
hypogeum/agents/video_generation_agent.py — Scene video generation (Priority 10 extraction)

Extracted from visual_package_director.py. Responsible for:
  - submit_scene() execution per scene
  - Per-scene MUAPI generation loop
  - Asset manifest write-back (update existing entry or append new entry)
  - generation_status updates (generated / generation_failed)
  - Reuse detection via asset_index (existing clip check)
  - Provider/model/status fields from MUAPI manifest_entry

Not responsible for:
  - Scene planning (scene_planner)
  - Entity continuity (entity_continuity_manager)
  - Provider routing decisions (provider_router)
  - Prompt writing
  - Reference image generation
  - Visual QA / Media Specs QA
  - Final visual_package assembly

Public API:
    build_asset_index(visual_package)
    generate_scene_video(scene, asset_index, asset_manifest_assets,
                         entity_hosted_urls, config, episode_id, output_dir, log,
                         budget_guard=None)
    generate_all_scene_videos(visual_package, scenes_sorted, entity_hosted_urls,
                              config, episode_id, output_dir, log, budget_guard=None)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import hypogeum.asset_registry as _asset_registry
from hypogeum.agents.provider_router import build_scene_spec
from hypogeum.config import Config
from hypogeum.muapi import (
    MUAPIAuthFailure,
    MUAPIBudgetExhausted,
    MUAPIBudgetGuard,
    MUAPIQuotaExhausted,
    submit_scene,
)

# Attribution passed to MUAPI — generation is driven by the Visual Package Director.
_AGENT_NAME = "visual_package_director"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Asset index
# ---------------------------------------------------------------------------

def build_asset_index(visual_package: dict) -> dict:
    """
    Build a {scene_id: asset_dict} index from asset_manifest for O(1) updates.

    Keyed by the first entry in each asset's used_in_scenes list (assets are
    1:1 with scenes in this pipeline).

    Args:
        visual_package: Visual Package dict containing asset_manifest.

    Returns:
        Dict mapping scene_id → asset dict (same objects, not copies).
    """
    index: dict = {}
    for asset in visual_package.get("asset_manifest", {}).get("assets", []):
        used = asset.get("used_in_scenes", [])
        if used:
            index[used[0]] = asset
    return index


# ---------------------------------------------------------------------------
# Single-scene generation
# ---------------------------------------------------------------------------

def generate_scene_video(
    scene: dict,
    asset_index: dict,
    asset_manifest_assets: list,
    entity_hosted_urls: dict[str, str],
    config: Config,
    episode_id: str,
    output_dir: Path,
    log,
    budget_guard: Optional[MUAPIBudgetGuard] = None,
) -> None:
    """
    Generate video for one scene and write the result back to asset_manifest.

    Does NOT raise on generation failure — generation_failed scenes are
    documented in the asset_manifest and the loop continues.

    Raises MUAPIAuthFailure / MUAPIQuotaExhausted / MUAPIBudgetExhausted
    immediately — these are halt conditions that abort the pipeline.

    Asset manifest write-back rules:
      - If scene_id already exists in asset_index (pre-populated manifest):
          update generation_status and asset_id in place.
      - Otherwise (empty manifest, production default):
          append a new entry with all provider/model/status fields.

    Args:
        scene:                 Planned scene dict from the visual_package.
        asset_index:           {scene_id: asset_dict} index for O(1) lookup.
        asset_manifest_assets: asset_manifest["assets"] list (mutated in place).
        entity_hosted_urls:    entity_id → hosted_url for ref image injection.
        config:                Loaded Config instance.
        episode_id:            Episode identifier for MUAPI attribution.
        output_dir:            Output directory for asset storage.
        log:                   Logger instance.
        budget_guard:          Optional MUAPIBudgetGuard (M5.6).
    """
    scene_id   = scene["scene_id"]
    scene_spec = build_scene_spec(scene, entity_hosted_urls)

    log.info(
        "vpd_scene_generation_start",
        scene_id=scene_id,
        source_type=scene.get("source_type"),
        short_id=scene.get("short_id"),
    )

    attempted_at = _now_iso()
    asset_slot_id = _asset_registry.compute_asset_slot_id(
        episode_id, "video_clip", "muapi", variant=scene_id,
    )
    version_id = _asset_registry.register_asset_attempt(
        asset_slot_id=asset_slot_id,
        asset_type="video_clip",
        provider="muapi",
        attempt_number=1,
        version_index=0,
        attempted_at=attempted_at,
    )

    try:
        manifest_entry = submit_scene(
            scene_spec,
            config,
            episode_id,
            output_dir,
            agent=_AGENT_NAME,
            budget_guard=budget_guard,
        )
    except (MUAPIAuthFailure, MUAPIQuotaExhausted, MUAPIBudgetExhausted) as exc:
        _asset_registry.register_asset_failure(
            asset_slot_id=asset_slot_id,
            version_id=version_id,
            asset_type="video_clip",
            provider="muapi",
            error_type=type(exc).__name__,
            error_message=str(exc),
            attempted_at=attempted_at,
        )
        raise

    gen_status = manifest_entry.get("generation_status", "generation_failed")

    if gen_status in ("generated", "reused"):
        log.info(
            "vpd_scene_generated",
            scene_id=scene_id,
            asset_id=manifest_entry.get("asset_id"),
            model_used=manifest_entry.get("model_used"),
            fallback_used=manifest_entry.get("fallback_used"),
            reused=gen_status == "reused",
        )
        _asset_registry.register_asset_success(
            asset_slot_id=asset_slot_id,
            version_id=version_id,
            asset_id=manifest_entry.get("asset_id", ""),
            asset_type="video_clip",
            provider=manifest_entry.get("provider", "muapi"),
            attempted_at=attempted_at,
            model=manifest_entry.get("model_used"),
            reuse_eligible=True,
        )
    else:
        log.warn(
            "vpd_scene_failed",
            scene_id=scene_id,
            failure_reason=manifest_entry.get("failure_reason"),
        )
        _asset_registry.register_asset_failure(
            asset_slot_id=asset_slot_id,
            version_id=version_id,
            asset_type="video_clip",
            provider="muapi",
            error_type="generation_failed",
            error_message=manifest_entry.get("failure_reason", ""),
            attempted_at=attempted_at,
        )

    # Update existing entry or append new one
    if scene_id in asset_index:
        asset = asset_index[scene_id]
        asset["generation_status"] = gen_status
        asset["asset_id"] = manifest_entry.get("asset_id", asset.get("asset_id", ""))
        # Write provenance fields back so asset_archive_agent can capture them.
        asset["provider"]     = manifest_entry.get("provider", "muapi")
        asset["model"]        = manifest_entry.get("model_used", "")
        asset["prompt_used"]  = manifest_entry.get("prompt_used")
        asset["tier_used"]    = manifest_entry.get("tier_used")
        asset["fallback_used"] = manifest_entry.get("fallback_used", False)
    else:
        asset_manifest_assets.append({
            "scene_id":          scene_id,
            "short_id":          scene.get("short_id"),
            "asset_id":          manifest_entry.get("asset_id", ""),
            "generation_status": gen_status,
            "provider":          manifest_entry.get("provider", ""),
            "model":             manifest_entry.get("model_used", ""),
            "prompt_used":       manifest_entry.get("prompt_used"),
            "tier_used":         manifest_entry.get("tier_used"),
            "fallback_used":     manifest_entry.get("fallback_used", False),
            "source_type":       scene.get("source_type", "main_episode"),
            "used_in_scenes":    [scene_id],
        })


# ---------------------------------------------------------------------------
# Full scene generation loop
# ---------------------------------------------------------------------------

def generate_all_scene_videos(
    visual_package: dict,
    scenes_sorted: list,
    entity_hosted_urls: dict[str, str],
    config: Config,
    episode_id: str,
    output_dir: Path,
    log,
    budget_guard: Optional[MUAPIBudgetGuard] = None,
) -> None:
    """
    Run the per-scene MUAPI generation loop over all ordered, filtered scenes.

    Builds the asset_index once before the loop and calls generate_scene_video
    for each scene. Mutates visual_package["asset_manifest"]["assets"] in place.

    Raises MUAPIAuthFailure / MUAPIQuotaExhausted / MUAPIBudgetExhausted on
    the first halt condition encountered — the loop stops immediately.

    Args:
        visual_package:    Visual Package dict (asset_manifest mutated in place).
        scenes_sorted:     Ordered, filtered scene list from scene_planner.
        entity_hosted_urls: entity_id → hosted_url for ref image injection.
        config:            Loaded Config instance.
        episode_id:        Episode identifier.
        output_dir:        Output directory for asset storage.
        log:               Logger instance.
        budget_guard:      Optional MUAPIBudgetGuard (M5.6).
    """
    asset_index           = build_asset_index(visual_package)
    asset_manifest_assets = visual_package["asset_manifest"]["assets"]

    # Statuses that mean "this scene already has a clip — do not submit to MUAPI".
    # "deferred" is set externally (e.g. selective regeneration plan) to park a
    # scene without consuming budget.  "reused"/"generated"/"dry_run" are the
    # normal terminal-success values written by submit_scene or the salvage tool.
    _SKIP_STATUSES = {"reused", "generated", "dry_run", "deferred"}

    for scene in scenes_sorted:
        sid = scene.get("scene_id", "")
        existing = asset_index.get(sid, {})
        if existing.get("generation_status") in _SKIP_STATUSES:
            log.info(
                "vpd_scene_skipped",
                scene_id=sid,
                generation_status=existing.get("generation_status"),
                reason="pre_populated_terminal_status",
            )
            continue
        generate_scene_video(
            scene, asset_index, asset_manifest_assets, entity_hosted_urls,
            config, episode_id, output_dir, log,
            budget_guard=budget_guard,
        )
