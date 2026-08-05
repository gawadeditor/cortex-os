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
                from hypogeum.agents.provider_router import build_scene_spec  # noqa: PLC0415

                output_dir = target_path.parent
                output_dir.mkdir(parents=True, exist_ok=True)

                # missing_scene is a raw VPD scene dict: production_spec.
                # provider_routing.primary_prompt (nested), not the flat
                # production_spec.primary_prompt submit_scene() requires.
                # build_scene_spec() is the same canonical translator VPD's
                # own Phase 2 generation uses — reused here rather than
                # duplicated (2026-08-04 fix). entity_hosted_urls={}: a
                # repair/extra clip is requested independently of the
                # original reference-image generation pass, so I2V entity-
                # lock conditioning is not available here — the clip is
                # generated as text-to-video only, a narrow, pre-existing
                # limitation (this path never worked at all before this
                # fix), not a new regression.
                submit_spec = build_scene_spec(missing_scene, entity_hosted_urls={})

                asset_result = submit_scene(
                    scene_spec=submit_spec,
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
# Coverage-deficit filling (2026-08-01 policy) — sibling to Strategy B above,
# but for beats whose clip EXISTS yet doesn't run long enough for its
# narration beat (a duration deficit), not for scenes with no clip at all.
# Video Renderer is no longer permitted to compensate for this with tpad/
# loop/reverse/large setpts (see hypogeum/agents/video_renderer.py and
# hypogeum/agents/coverage_check.py) — the factory generates more real,
# distinct, forward-only footage instead.
# ---------------------------------------------------------------------------

def fill_coverage_deficits(
    insufficient_beats: list,  # List[coverage_check.BeatCoverage]
    visual_package: dict,
    assets_root: Path,
    config: Config,
    episode_id: str,
    log=None,
) -> Dict[str, List[Path]]:
    """
    Request additional distinct clips for beats whose available visual
    coverage (primary clip + any already-attached extras), even after the
    approved editorial speed adjustment, falls short of the narration
    beat's duration.

    Only touches the beats passed in — a beat that already has sufficient
    coverage is never regenerated (its scene_id simply won't appear in
    insufficient_beats, since that list is computed by
    hypogeum.agents.coverage_check.check_timeline_coverage()).

    Deficit-aware planning (2026-08-05 policy, replacing the prior fixed
    "always try exactly COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT" behavior,
    which was mathematically incapable of closing long narration beats):
    the number of additional clips requested for one beat is driven by
    that beat's own measured remaining deficit, recalculated after every
    real clip — never a flat count applied uniformly to every beat.
    config.COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT is now a finite safety
    cap (stops one stubborn beat from generating unbounded cost), not the
    assumed target. A beat that cannot close within the cap logs an
    explicit structured "coverage_fill_beat_unresolvable" reason
    (required/allowed clip counts, unresolved deficit) rather than
    silently looping or silently giving up.

    Each candidate clip slot gets a bounded retry
    (config.COVERAGE_FILL_MAX_SLOT_RETRIES) on a transient generation or
    qualification failure — retrying the SAME slot, never advancing to a
    new one and never counting a failed attempt as coverage, so one
    provider-side hiccup doesn't permanently cost a beat one of its
    mathematically required clips.

    Every new clip is routed through hypogeum.source_enhancement.
    qualify_asset() — the same canonical asset-qualification boundary VPD
    primaries use — before its duration counts toward closing the
    deficit. A clip that fails qualification (corrupt, or still below the
    required resolution after a failed enhancement attempt) is never
    counted and never reaches Assembly, regardless of its duration.

    Returns {scene_id: [Path, ...]} — only the NEWLY generated, qualified
    extra clip paths for beats that got at least one. Callers attach
    these to the corresponding TimelineEntry.extra_clip_paths and re-run
    check_timeline_coverage() to see whether the deficit is now closed.
    """
    import math

    from hypogeum.agents.coverage_check import MAX_EDITORIAL_SPEED_FACTOR  # noqa: PLC0415
    from hypogeum.source_enhancement import qualify_asset  # noqa: PLC0415

    if log is None:
        log = get_logger(episode_id=episode_id, agent="clip_repair_agent")

    if not insufficient_beats:
        return {}

    scenes: List[dict] = visual_package.get("scenes", [])
    scene_by_id: Dict[str, dict] = {s["scene_id"]: s for s in scenes}
    max_extra_per_beat = max(0, getattr(config, "COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT", 2))
    max_slot_retries = max(0, getattr(config, "COVERAGE_FILL_MAX_SLOT_RETRIES", 1))
    assumed_clip_duration = max(0.1, getattr(config, "COVERAGE_FILL_ASSUMED_CLIP_DURATION_SECONDS", 8.0))

    # Real production convention (matches VPD's Phase 2 caller): output_dir
    # is the episode root — muapi.scene_file_path() appends "video/" itself.
    output_dir = assets_root / "episodes" / episode_id

    manifest_assets: List[dict] = visual_package.setdefault(
        "asset_manifest", {}
    ).setdefault("assets", [])

    log.info(
        "coverage_fill_started",
        episode_id=episode_id,
        beats_to_fill=len(insufficient_beats),
        max_extra_per_beat=max_extra_per_beat,
        max_slot_retries=max_slot_retries,
    )

    extra_clips_by_scene: Dict[str, List[Path]] = {}

    for beat in insufficient_beats:
        sid = beat.scene_id
        scene = scene_by_id.get(sid)
        if scene is None:
            log.warn("coverage_fill_skip", episode_id=episode_id, scene_id=sid,
                      reason="scene_not_found_in_visual_package")
            continue

        # Raw additional seconds needed so that (available + new) * MAX_FACTOR
        # covers the narration duration — smaller than the raw deficit itself,
        # since the newly generated footage also benefits from the same
        # bounded editorial stretch as the existing clip.
        remaining = beat.deficit_seconds / MAX_EDITORIAL_SPEED_FACTOR
        new_paths: List[Path] = []

        # Cost-visibility estimate (planning only — the real stopping
        # condition is always `remaining`, recalculated from each clip's
        # measured duration below, never this estimate).
        effective_coverage_per_clip = assumed_clip_duration * MAX_EDITORIAL_SPEED_FACTOR
        required_extra_count_estimate = math.ceil(beat.deficit_seconds / effective_coverage_per_clip)
        log.info(
            "coverage_fill_beat_planned",
            episode_id=episode_id,
            scene_id=sid,
            deficit_seconds=round(beat.deficit_seconds, 3),
            required_extra_count_estimate=required_extra_count_estimate,
            allowed_extra_count=max_extra_per_beat,
        )

        slot_index = 0
        while remaining > 0.001 and slot_index < max_extra_per_beat:
            slot_index += 1
            extra_scene_id = f"{sid}__extra{slot_index}"

            qualified_path: Optional[Path] = None
            for retry_num in range(max_slot_retries + 1):
                try:
                    from hypogeum.muapi import submit_scene, scene_file_path  # noqa: PLC0415
                    from hypogeum.agents.provider_router import build_scene_spec  # noqa: PLC0415

                    # scene is a raw VPD scene dict: production_spec.
                    # provider_routing.primary_prompt (nested), not the flat
                    # production_spec.primary_prompt submit_scene() requires —
                    # build_scene_spec() is the same canonical translator VPD's
                    # own Phase 2 generation uses (2026-08-04 fix; see
                    # run_clip_repair()'s Strategy B above for the identical
                    # fix and the entity_hosted_urls={} rationale).
                    extra_scene_spec = build_scene_spec(scene, entity_hosted_urls={})
                    extra_scene_spec["scene_id"] = extra_scene_id

                    output_dir.mkdir(parents=True, exist_ok=True)
                    asset_result = submit_scene(
                        scene_spec=extra_scene_spec,
                        config=config,
                        episode_id=episode_id,
                        output_dir=output_dir,
                        agent="clip_repair_agent",
                    )
                    status = asset_result.get("generation_status", "generation_failed")
                    # Single source of truth (2026-08-05 fix): this used to
                    # reimplement the "sh_{short_id}/sc_{scene_id}.mp4" pattern
                    # inline and dropped the "sh_" prefix, so the existence
                    # check below looked in the wrong directory for every Short
                    # beat — real, already-generated, already-paid clips were
                    # misreported as coverage_fill_clip_failed. Always delegate
                    # to muapi.scene_file_path(), the same function submit_scene()
                    # itself used to write the file.
                    extra_path = scene_file_path(output_dir, extra_scene_id, beat.short_id)

                    if status not in ("generated", "reused") or not extra_path.exists():
                        log.warn(
                            "coverage_fill_slot_attempt_failed",
                            episode_id=episode_id, scene_id=sid, extra_scene_id=extra_scene_id,
                            retry_num=retry_num, max_slot_retries=max_slot_retries,
                            generation_status=status,
                        )
                        continue  # bounded retry — same slot, does not advance/consume a new one

                    # Quality-gate the new clip before it counts as coverage
                    # (Part H): reused canonical qualify_asset() boundary,
                    # never a raw provider clip reaching Assembly. Recorded
                    # onto the asset_manifest as a new entry so a resumed
                    # run sees this decision too (2026-08-05).
                    extra_asset = {
                        "scene_id": extra_scene_id,
                        "asset_id": asset_result.get("asset_id"),
                        "model": asset_result.get("model_used"),
                        "tier_used": asset_result.get("tier_used"),
                        "source_enhancement": None,
                    }
                    qualify_asset(
                        extra_asset, extra_path, beat.source_type, config, episode_id, log,
                    )
                    se = extra_asset.get("source_enhancement") or {}
                    qualified = se.get("validation_result") in (
                        "passed", "skipped_native_resolution",
                    )

                    if not qualified:
                        log.warn(
                            "coverage_fill_clip_unqualified",
                            episode_id=episode_id, scene_id=sid, extra_scene_id=extra_scene_id,
                            retry_num=retry_num, max_slot_retries=max_slot_retries,
                            validation_result=se.get("validation_result"),
                            failure_reason=se.get("failure_reason"),
                        )
                        continue  # bounded retry — same slot, corrupt/failed candidate already archived by qualify_asset

                    manifest_assets.append(extra_asset)
                    qualified_path = extra_path
                    break

                except Exception as exc:
                    log.warn(
                        "coverage_fill_clip_error",
                        episode_id=episode_id, scene_id=sid, extra_scene_id=extra_scene_id,
                        retry_num=retry_num, max_slot_retries=max_slot_retries,
                        error=str(exc),
                    )
                    continue  # bounded retry — same slot

            if qualified_path is None:
                log.warn(
                    "coverage_fill_slot_exhausted",
                    episode_id=episode_id, scene_id=sid, extra_scene_id=extra_scene_id,
                    retries_used=max_slot_retries,
                )
                continue  # slot consumed, genuinely could not produce usable coverage this attempt

            extra_duration = _probe_extra_clip_duration(qualified_path)
            new_paths.append(qualified_path)
            remaining -= extra_duration
            log.info(
                "coverage_fill_clip_generated",
                episode_id=episode_id,
                scene_id=sid,
                extra_scene_id=extra_scene_id,
                extra_duration_seconds=round(extra_duration, 3),
                remaining_deficit_seconds=round(max(0.0, remaining), 3),
            )

        if new_paths:
            extra_clips_by_scene[sid] = new_paths

        if remaining > 0.001:
            log.error(
                "coverage_fill_beat_unresolvable",
                episode_id=episode_id,
                scene_id=sid,
                required_extra_count_estimate=required_extra_count_estimate,
                allowed_extra_count=max_extra_per_beat,
                clips_obtained=len(new_paths),
                unresolved_deficit_seconds=round(remaining * MAX_EDITORIAL_SPEED_FACTOR, 3),
            )

    log.info(
        "coverage_fill_done",
        episode_id=episode_id,
        beats_with_new_clips=len(extra_clips_by_scene),
        total_new_clips=sum(len(v) for v in extra_clips_by_scene.values()),
    )

    return extra_clips_by_scene


def _probe_extra_clip_duration(path: Path) -> float:
    """Real duration of a generated clip via ffprobe. Returns 0.0 on failure."""
    import subprocess
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        val = result.stdout.strip()
        if result.returncode == 0 and val:
            return float(val)
    except Exception:
        pass
    return 0.0


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
        # narrative_signals may be dicts (legacy) or plain strings (new VPD A2 format)
        if isinstance(sig, str):
            t = sig
        else:
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
      2. Fallback (no asset_id yet, e.g. a scene that never generated a
         primary clip at all): delegate to muapi.scene_file_path(), the
         single canonical builder — never reimplement the "sh_{short_id}/
         sc_{scene_id}.mp4" convention here. A prior inline duplicate
         dropped the "sh_" prefix for Short scenes (2026-08-05 fix).
    """
    from hypogeum.muapi import scene_file_path  # noqa: PLC0415

    asset_id = scene.get("asset_id")
    if asset_id:
        return assets_root / f"{asset_id}.mp4"

    short_id = scene.get("short_id")
    return scene_file_path(assets_root / "episodes" / episode_id, scene_id, short_id)
