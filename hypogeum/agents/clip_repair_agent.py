"""
hypogeum/agents/clip_repair_agent.py — Clip Repair Agent

Runs inside Assembly Agent between asset inventory validation and timeline
construction. Attempts to fill missing clips before QA can reject the episode.

Strategy A — Semantic reuse
    For each missing scene, find the most semantically relevant existing clip
    already on disk. Copy it to the expected path.

    Constraints enforced:
      - No consecutive duplicates in the repaired timeline: the repair clip
        for scene N cannot be the same file as the clip for scene N-1 or N+1.
      - No same source clip used twice across all repairs: each donor clip is
        used for at most one missing slot.
      - Never random selection: always pick by descending semantic score.

    Semantic scoring: Jaccard similarity on whitespace-split token sets from
    the scene's visual description fields (prompt, visual_directions,
    scene_description, narrative_signals). No LLM, no stopword filtering —
    domain-specific vocabulary makes simple overlap effective.

Strategy B — Extra clip generation
    If missing clips remain after Strategy A, generate up to
    config.CLIP_REPAIR_MAX_GENERATED (default 5) clips via MUAPI.
    Only the scenes with the highest scene_index priority are generated;
    the rest are left missing and QA decides.

    Quality > cost: ~5 clips at T2 costs ≈ $3 — acceptable for production.

    Uses muapi.submit_scene() directly with the existing scene spec from the
    visual_package. All MUAPI safety flags (DRY_RUN, CHEAP_MODE, etc.) apply.

QA rejection gate
    Only after both A and B are exhausted (or disabled) does the Assembly Agent
    hand off to the Final Render Reviewer with the remaining missing clips.
    If repair fills all gaps, clip_availability_rate reaches 1.0 and the reviewer
    sees no freeze-frame failure.

Public API:
    run_clip_repair(
        visual_package, missing_scene_ids, present_clip_ids,
        assets_root, config, episode_id, log,
    ) -> ClipRepairResult

Logging events emitted:
    clip_repair_started            — info  — inputs received
    clip_repair_strategy_a_match   — info  — semantic match found for a scene
    clip_repair_strategy_a_skip    — warn  — no eligible donor found
    clip_repair_strategy_a_done    — info  — strategy A summary
    clip_repair_strategy_b_start   — info  — extra clip generation begins
    clip_repair_strategy_b_clip    — info  — one clip generated
    clip_repair_strategy_b_skip    — warn  — generation failed for a scene
    clip_repair_done               — info  — final summary
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from hypogeum.config import Config
from hypogeum.logger import get_logger


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ClipRepairResult:
    """Summary of what the Clip Repair Agent did."""
    repaired_by_reuse: List[str] = field(default_factory=list)    # scene_ids fixed by Strategy A
    repaired_by_generation: List[str] = field(default_factory=list)  # scene_ids fixed by Strategy B
    still_missing: List[str] = field(default_factory=list)        # scene_ids still missing after both


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_clip_repair(
    visual_package: dict,
    missing_scene_ids: List[str],
    present_clip_ids: List[str],
    assets_root: Path,
    config: Config,
    episode_id: str,
    log=None,
) -> ClipRepairResult:
    """
    Attempt to fill missing clips via semantic reuse (A) then generation (B).

    Args:
        visual_package:    Validated Visual Package from VPD.
        missing_scene_ids: scene_id values for clips not found on disk.
        present_clip_ids:  asset_id values for clips that ARE present.
        assets_root:       Root of .hypogeum-assets/ tree.
        config:            Loaded Config instance.
        episode_id:        For log correlation.
        log:               Logger (created if None).

    Returns:
        ClipRepairResult with per-strategy outcome lists.
    """
    if log is None:
        log = get_logger(episode_id=episode_id, agent="clip_repair_agent")

    log.info(
        "clip_repair_started",
        episode_id=episode_id,
        missing_count=len(missing_scene_ids),
        present_count=len(present_clip_ids),
    )

    if not missing_scene_ids:
        return ClipRepairResult()

    scenes: List[dict] = visual_package.get("scenes", [])
    asset_manifest: dict = visual_package.get("asset_manifest", {})
    assets_list: List[dict] = asset_manifest.get("assets", [])

    # Build scene_id → scene dict for fast lookup
    scene_by_id: Dict[str, dict] = {s["scene_id"]: s for s in scenes}

    # Build asset_id → file Path for present clips
    present_paths: Dict[str, Path] = {}
    for asset in assets_list:
        aid = asset.get("asset_id", "")
        if aid and aid in present_clip_ids:
            p = assets_root / f"{aid}.mp4"
            if p.exists() and p.stat().st_size > 0:
                present_paths[aid] = p

    # Build ordered scene list (main episode only, by scene_index) for
    # consecutive-duplicate constraint.
    main_scenes_ordered = sorted(
        [s for s in scenes if s.get("source_type") == "main_episode"],
        key=lambda s: s.get("scene_index", 0),
    )
    scene_id_order = [s["scene_id"] for s in main_scenes_ordered]

    # ---------------------------------------------------------------------------
    # Strategy A: Semantic reuse
    # ---------------------------------------------------------------------------
    result = ClipRepairResult()
    donated_asset_ids: Set[str] = set()  # each donor used at most once

    # Tracks what clip (asset_id) will be at each position after repair
    # (for consecutive-duplicate enforcement).
    assigned: Dict[str, str] = {}  # scene_id → asset_id (donor)

    # Seed assigned with the clips we already have
    for asset in assets_list:
        aid = asset.get("asset_id", "")
        sid = _asset_id_to_scene_id(aid, scenes)
        if sid and aid in present_paths:
            assigned[sid] = aid

    still_missing = list(missing_scene_ids)

    for missing_sid in list(still_missing):
        missing_scene = scene_by_id.get(missing_sid, {})
        missing_desc = _scene_description(missing_scene)

        # Score all present clips by semantic overlap
        candidates: List[tuple] = []
        for donor_aid, donor_path in present_paths.items():
            if donor_aid in donated_asset_ids:
                continue  # already used this donor
            donor_sid = _asset_id_to_scene_id(donor_aid, scenes)
            donor_scene = scene_by_id.get(donor_sid or "", {})
            donor_desc = _scene_description(donor_scene)
            score = _jaccard(missing_desc, donor_desc)
            candidates.append((score, donor_aid, donor_path))

        # Sort by score descending; for equal scores, by asset_id for determinism
        candidates.sort(key=lambda c: (-c[0], c[1]))

        chosen_aid: Optional[str] = None
        chosen_path: Optional[Path] = None

        for _score, donor_aid, donor_path in candidates:
            # Consecutive-duplicate check: no same clip as the neighbour before/after
            pos = scene_id_order.index(missing_sid) if missing_sid in scene_id_order else -1
            prev_sid = scene_id_order[pos - 1] if pos > 0 else None
            next_sid = scene_id_order[pos + 1] if 0 <= pos < len(scene_id_order) - 1 else None
            prev_assigned = assigned.get(prev_sid) if prev_sid else None
            next_assigned = assigned.get(next_sid) if next_sid else None

            if donor_aid == prev_assigned or donor_aid == next_assigned:
                continue  # would create consecutive duplicate

            chosen_aid = donor_aid
            chosen_path = donor_path
            break

        if chosen_aid is None or chosen_path is None:
            log.warn(
                "clip_repair_strategy_a_skip",
                episode_id=episode_id,
                scene_id=missing_sid,
                reason="no_eligible_donor",
            )
            continue

        # Copy donor to the expected path for the missing scene
        target_path = _expected_clip_path(missing_sid, scene_by_id.get(missing_sid, {}),
                                           assets_root, episode_id)
        if target_path is None:
            log.warn(
                "clip_repair_strategy_a_skip",
                episode_id=episode_id,
                scene_id=missing_sid,
                reason="cannot_resolve_target_path",
            )
            continue

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(chosen_path), str(target_path))
        except OSError as exc:
            log.warn(
                "clip_repair_strategy_a_skip",
                episode_id=episode_id,
                scene_id=missing_sid,
                reason=f"copy_failed: {exc}",
            )
            continue

        donated_asset_ids.add(chosen_aid)
        assigned[missing_sid] = chosen_aid
        result.repaired_by_reuse.append(missing_sid)
        still_missing.remove(missing_sid)

        log.info(
            "clip_repair_strategy_a_match",
            episode_id=episode_id,
            scene_id=missing_sid,
            donor_asset_id=chosen_aid,
            target_path=str(target_path),
        )

    log.info(
        "clip_repair_strategy_a_done",
        episode_id=episode_id,
        repaired=len(result.repaired_by_reuse),
        still_missing=len(still_missing),
    )

    # ---------------------------------------------------------------------------
    # Strategy B: Extra clip generation via MUAPI
    # ---------------------------------------------------------------------------
    max_generated = getattr(config, "CLIP_REPAIR_MAX_GENERATED", 5)

    if still_missing and max_generated > 0:
        log.info(
            "clip_repair_strategy_b_start",
            episode_id=episode_id,
            scenes_to_generate=min(len(still_missing), max_generated),
            max_generated=max_generated,
        )

        # Prioritise main_episode scenes over short scenes; within each group by scene_index
        def _priority(sid: str) -> tuple:
            s = scene_by_id.get(sid, {})
            is_short = s.get("source_type") == "short"
            return (int(is_short), s.get("scene_index", 999))

        to_generate = sorted(still_missing, key=_priority)[:max_generated]

        for missing_sid in to_generate:
            missing_scene = scene_by_id.get(missing_sid, {})
            if not missing_scene:
                continue

            target_path = _expected_clip_path(missing_sid, missing_scene,
                                               assets_root, episode_id)
            if target_path is None:
                continue

            try:
                # Import here to avoid circular imports — muapi is a leaf module.
                from hypogeum.muapi import submit_scene  # noqa: PLC0415

                output_dir = target_path.parent
                output_dir.mkdir(parents=True, exist_ok=True)

                asset_result = submit_scene(
                    scene_spec=missing_scene,
                    config=config,
                    episode_id=episode_id,
                    output_dir=output_dir,
                    agent="clip_repair_agent",
                )
                status = asset_result.get("generation_status", "generation_failed")
                if status in ("generated", "reused", "dry_run") and target_path.exists():
                    result.repaired_by_generation.append(missing_sid)
                    still_missing.remove(missing_sid)
                    log.info(
                        "clip_repair_strategy_b_clip",
                        episode_id=episode_id,
                        scene_id=missing_sid,
                        generation_status=status,
                        target_path=str(target_path),
                    )
                else:
                    log.warn(
                        "clip_repair_strategy_b_skip",
                        episode_id=episode_id,
                        scene_id=missing_sid,
                        generation_status=status,
                    )
            except Exception as exc:
                log.warn(
                    "clip_repair_strategy_b_skip",
                    episode_id=episode_id,
                    scene_id=missing_sid,
                    error=str(exc),
                )

    result.still_missing = list(still_missing)

    log.info(
        "clip_repair_done",
        episode_id=episode_id,
        repaired_by_reuse=len(result.repaired_by_reuse),
        repaired_by_generation=len(result.repaired_by_generation),
        still_missing=len(result.still_missing),
    )

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _scene_description(scene: dict) -> str:
    """
    Concatenate all text fields that carry semantic content for a scene.
    Used for Jaccard overlap scoring.
    """
    parts: List[str] = []

    prod_spec = scene.get("production_spec", {})
    for key in ("primary_prompt", "fallback_prompt"):
        v = prod_spec.get(key, "") or ""
        if v:
            parts.append(v)

    for key in ("visual_directions", "scene_description", "description"):
        v = scene.get(key, "") or ""
        if v:
            parts.append(v)

    signals = scene.get("narrative_signals", [])
    for sig in signals:
        t = sig.get("signal_type", "") or sig.get("description", "") or ""
        if t:
            parts.append(t)

    return " ".join(parts)


def _jaccard(a: str, b: str) -> float:
    """Jaccard similarity on whitespace-split token sets."""
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())
    if not tokens_a and not tokens_b:
        return 0.0
    union = tokens_a | tokens_b
    if not union:
        return 0.0
    return len(tokens_a & tokens_b) / len(union)


def _asset_id_to_scene_id(asset_id: str, scenes: List[dict]) -> Optional[str]:
    """Return the scene_id whose asset_id matches, or None."""
    for s in scenes:
        if s.get("asset_id") == asset_id:
            return s.get("scene_id")
    return None


def _expected_clip_path(
    scene_id: str,
    scene: dict,
    assets_root: Path,
    episode_id: str,
) -> Optional[Path]:
    """
    Resolve the path where the clip for `scene_id` is expected on disk.

    Priority:
      1. scene["asset_id"] → assets_root / f"{asset_id}.mp4"
      2. Short scene: assets_root/episodes/{episode_id}/video/{short_id}/{scene_id}.mp4
      3. Main scene:  assets_root/episodes/{episode_id}/video/{scene_id}.mp4
    """
    asset_id = scene.get("asset_id")
    if asset_id:
        return assets_root / f"{asset_id}.mp4"

    short_id = scene.get("short_id")
    if short_id:
        return assets_root / "episodes" / episode_id / "video" / short_id / f"{scene_id}.mp4"

    return assets_root / "episodes" / episode_id / "video" / f"{scene_id}.mp4"
