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


def _force_short_native_aspect(scene_spec: dict, config) -> None:
    """Deterministically pin a repair/coverage-fill clip to the deliverable's
    portrait aspect when the episode is short-native.

    scene_planner.order_and_filter_scenes() forces production_spec.aspect_ratio
    to 9:16 for every VPD scene, but that forced value is not always persisted
    onto the scene dicts this agent later reads from the visual_package — so a
    repair or coverage-fill clip built by build_scene_spec() inherits the "16:9"
    default and the provider generates a landscape clip that then fails the
    portrait source-resolution qualifier (>=1080x1920) on every retry. This is
    the same single deterministic guarantee scene_planner applies, at this
    agent's submit_scene() call sites. No-op unless SHORT_NATIVE_MODE.
    """
    if getattr(config, "SHORT_NATIVE_MODE", False) is not True:
        return
    want = getattr(config, "SHORT_NATIVE_ASPECT_RATIO", "9:16") or "9:16"
    vs = scene_spec.get("visual_spec")
    if isinstance(vs, dict):
        vs["aspect_ratio"] = want
    ps = scene_spec.get("production_spec")
    if isinstance(ps, dict):
        ps["aspect_ratio"] = want


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
    channel_id: Optional[str] = None,
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
                _force_short_native_aspect(submit_spec, config)

                # channel_id is included only when not None, so every
                # existing caller's exact prior submit_scene() kwarg set
                # (and any test mock with a fixed signature) is completely
                # unaffected -- mirrors the same "omit entirely, don't
                # pass None" discipline used at this function's other two
                # gate-eligible call sites.
                _gate_kwargs = {"channel_id": channel_id} if channel_id is not None else {}
                asset_result = submit_scene(
                    scene_spec=submit_spec,
                    config=config,
                    episode_id=episode_id,
                    output_dir=output_dir,
                    agent="clip_repair_agent",
                    **_gate_kwargs,
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
    channel_id: Optional[str] = None,
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

    Shot diversity (2026-08-07 fix, refactored 2026-08-07 — see the
    ownership note above the "Internal helpers" section below): when the
    beat's scene carries a scene["cinematic_coverage_plan"]
    (hypogeum.agents.cinematic_director, wired in by
    visual_package_director.py — present on every scene VPD plans from
    this point forward, each shot already carrying its own rendered
    primary_prompt/fallback_prompt written by hypogeum.video_prompt_writer.
    render_coverage_plan_shot_prompts()), each extra slot SELECTS the next
    distinct planned shot's already-rendered prompt (see
    hypogeum.agents.cinematic_director.shot_diversity_key(), imported
    below) instead of resubmitting the scene's own unmodified prompt. This
    function never renders prompt text itself. A beat whose plan cannot
    supply a genuinely distinct, already-rendered shot for a slot — plan
    exhausted, the next shot is compositionally equivalent to one already
    used in this beat with no diversity_justification, or the next shot
    has no rendered prompt — fails closed for that slot rather than
    submitting a perceptually repeated clip; the beat is then reported via
    the existing "coverage_fill_beat_unresolvable" path below, same as any
    other unresolvable deficit. Scenes with no plan attached (legacy state
    only) keep the prior single-prompt behavior unchanged.

    Returns {scene_id: [Path, ...]} — only the NEWLY generated, qualified
    extra clip paths for beats that got at least one. Callers attach
    these to the corresponding TimelineEntry.extra_clip_paths and re-run
    check_timeline_coverage() to see whether the deficit is now closed.
    """
    import math

    from hypogeum.agents.coverage_check import MAX_EDITORIAL_SPEED_FACTOR  # noqa: PLC0415
    from hypogeum.source_enhancement import qualify_asset  # noqa: PLC0415
    # Shared source of truth for "same shot" — owned by Cinematic Director,
    # not reimplemented here (see the ownership note above).
    from hypogeum.agents.cinematic_director import shot_diversity_key  # noqa: PLC0415

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

        # Shot-diversity wiring (2026-08-07 fix, refactored 2026-08-07 to
        # separate concerns — see module docstring addendum below).
        # scene["cinematic_coverage_plan"] is written by hypogeum.agents.
        # cinematic_director, wired into visual_package_director.py's
        # orchestration (commit 9ba27e1) — every beat gets an ordered list
        # of already-distinct planned shots, each already carrying its own
        # fully rendered primary_prompt/fallback_prompt (written once, by
        # hypogeum.video_prompt_writer.render_coverage_plan_shot_prompts(),
        # also wired into VPD's orchestration, right after Stage B). This
        # loop only SELECTS one pre-rendered shot per extra slot — it never
        # constructs prompt text itself.
        #
        # Scenes with no plan attached (legacy/pre-9ba27e1 visual
        # packages, or fixtures that never set one) fall back to the
        # pre-existing behavior of resubmitting the scene's own baked
        # prompt unchanged — a deliberate, disclosed scope boundary: every
        # NEW episode always gets a plan (cinematic_director's own
        # LLM-failure fallback still produces a minimal single-shot plan),
        # so this fallback exists only for already-generated legacy state,
        # not for the normal production path.
        coverage_plan = scene.get("cinematic_coverage_plan") or {}
        planned_shots = sorted(
            coverage_plan.get("shots", []) or [],
            key=lambda sh: sh.get("shot_order", 0),
        )

        # Plan-aware resume state (2026-08-07): determine which planned
        # shots are ALREADY represented for this beat and which extra
        # slot numbers are already occupied, from the asset_manifest —
        # the same authoritative record planned_shot_id was added to for
        # exactly this purpose — rather than assuming a fresh call always
        # starts at slot 1 / shot index 0. A resumed run (assembly_agent
        # invoked again after a prior partial repair, or manually re-run
        # after raising COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT) must
        # continue from the next unrepresented shot and the next free
        # slot number, never re-walk the plan from the beginning.
        existing_extra_prefix = f"{sid}__extra"
        existing_extra_ids = {
            a.get("scene_id") for a in manifest_assets
            if (a.get("scene_id") or "").startswith(existing_extra_prefix)
        }
        represented_shot_ids = {
            a.get("planned_shot_id") for a in manifest_assets
            if (a.get("scene_id") or "").startswith(existing_extra_prefix)
            and a.get("planned_shot_id")
        }
        # shot_order 1 is the beat's PRIMARY scene clip's slot (sc_{sid}) --
        # hypogeum.production_plan.compile_production_plan() declares
        # contingency PlannedAssets only from shot_order >= 2, naming them
        # sc_{sid}__extra1 (from shot_order 2), __extra2 (shot_order 3), ... .
        # This coverage-fill walk MUST use the same offset or __extra1 is
        # submitted with shot_order 1's prompt and the paid video gate rejects
        # it (prompt_mismatch / undeclared_asset) against the plan's declared
        # __extra fingerprints -- the off-by-one that stalled ep_af078f34 and
        # the first autonomous short.
        available_shots = [
            sh for sh in planned_shots
            if sh.get("shot_order", 1) > 1 and sh.get("shot_id") not in represented_shot_ids
        ]
        used_shot_keys: Set[tuple] = {
            shot_diversity_key(sh) for sh in planned_shots
            if sh.get("shot_id") in represented_shot_ids or sh.get("shot_order", 1) <= 1
        }
        if represented_shot_ids:
            log.info(
                "coverage_fill_beat_resumed",
                episode_id=episode_id,
                scene_id=sid,
                already_represented_shots=len(represented_shot_ids),
                available_shots=len(available_shots),
                existing_extra_slots=len(existing_extra_ids),
            )

        # Resume completeness (2026-08-31): re-attach the coverage-fill
        # clips this beat ALREADY holds on the asset_manifest — qualified
        # and paid for by a prior run — so a resumed assembly counts them
        # toward coverage even for a beat that needs zero NEW generation.
        # Without this, fill_coverage_deficits() returned ONLY clips it
        # generated in the current call, so an interrupted-then-resumed
        # coverage-fill (ep_af078f34: 83/86 clips over ~5h, then a restart)
        # re-flagged every already-filled beat as insufficient because
        # check_timeline_coverage() never saw the existing footage.
        from hypogeum.muapi import scene_file_path as _scene_file_path  # noqa: PLC0415
        existing_extra_paths: List[Path] = []
        for _a in manifest_assets:
            _asid = _a.get("scene_id") or ""
            if not _asid.startswith(existing_extra_prefix):
                continue
            _p = _scene_file_path(output_dir, _asid, beat.short_id)
            if _p.exists() and _p not in existing_extra_paths:
                existing_extra_paths.append(_p)
        if existing_extra_paths:
            new_paths.extend(existing_extra_paths)
            for _p in existing_extra_paths:
                remaining -= _probe_extra_clip_duration(_p)
            log.info(
                "coverage_fill_existing_clips_reattached",
                episode_id=episode_id,
                scene_id=sid,
                reattached=len(existing_extra_paths),
                remaining_deficit_seconds=round(max(0.0, remaining), 3),
            )

        next_shot_idx = 0  # index into available_shots, not planned_shots

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

        # Slot numbering (2026-08-07: plan-aware resume, see above) starts
        # from however many extra slots this beat already occupies on the
        # asset_manifest — never from 0 — so a resumed call requests
        # sc_{sid}__extra{N+1} onward, not a filename already used by a
        # prior run's clip. submit_scene()'s own resume-safety reuse
        # (path-exists check) remains a correct, independent safety net
        # for the case where a slot's file exists but is somehow not yet
        # reflected in the manifest, but is no longer this function's
        # PRIMARY mechanism for recognizing already-completed work.
        slot_index = len(existing_extra_ids)
        while remaining > 0.001 and slot_index < max_extra_per_beat:
            slot_index += 1
            extra_scene_id = f"{sid}__extra{slot_index}"

            # Fail-closed shot SELECTION (requirement: coverage must never
            # be considered satisfied by perceptually/compositionally
            # equivalent clips). Only engages when a plan is actually
            # present — see the scope-boundary note above `planned_shots`.
            # This block selects which already-rendered shot to use; it
            # never renders prompt text (see module docstring addendum).
            # Selection is by NEXT AVAILABLE planned shot (available_shots,
            # already filtered for what this beat has represented before
            # this call), never by positional slot_index — a beat resuming
            # mid-plan continues from where it left off, it does not
            # re-walk planned_shots from index 0.
            planned_shot: Optional[dict] = None
            if planned_shots:
                if next_shot_idx >= len(available_shots):
                    log.error(
                        "coverage_fill_shot_plan_exhausted",
                        episode_id=episode_id, scene_id=sid, extra_scene_id=extra_scene_id,
                        planned_shot_count=len(planned_shots),
                        already_represented=len(represented_shot_ids),
                        available_shot_count=len(available_shots),
                        reason="no distinct, not-yet-represented planned shot left; "
                               "refusing to reuse an earlier shot assignment as "
                               "duration compensation",
                    )
                    break  # fail-closed — never fall back to an identical prompt

                candidate_shot = available_shots[next_shot_idx]
                shot_key = shot_diversity_key(candidate_shot)
                if shot_key in used_shot_keys and not candidate_shot.get("diversity_justification"):
                    log.error(
                        "coverage_fill_shot_plan_duplicate",
                        episode_id=episode_id, scene_id=sid, extra_scene_id=extra_scene_id,
                        shot_id=candidate_shot.get("shot_id"),
                        reason="planned shot is compositionally equivalent to an "
                               "already-used (this call or a prior run) shot in "
                               "this beat with no diversity_justification; "
                               "refusing to submit a perceptually-repeated clip",
                    )
                    break  # fail-closed
                next_shot_idx += 1

                # The shot must already carry a rendered prompt (written by
                # hypogeum.video_prompt_writer.render_coverage_plan_shot_prompts()
                # during VPD, before Assembly/Repair ever runs). Repair never
                # constructs prompt text itself — a shot missing one is not
                # usable coverage, not a case to paper over by falling back
                # to the base scene prompt (that would reintroduce the exact
                # repeated-prompt bug this fix corrects).
                if not (candidate_shot.get("primary_prompt") or "").strip():
                    log.error(
                        "coverage_fill_shot_prompt_missing",
                        episode_id=episode_id, scene_id=sid, extra_scene_id=extra_scene_id,
                        shot_id=candidate_shot.get("shot_id"), slot_index=slot_index,
                        reason="planned shot has no rendered primary_prompt; VPD's "
                               "render_coverage_plan_shot_prompts() did not run for "
                               "this scene — refusing to submit without a "
                               "pre-rendered prompt",
                    )
                    break  # fail-closed

                used_shot_keys.add(shot_key)
                planned_shot = candidate_shot

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
                    # fix and the entity_hosted_urls={} rationale). Always
                    # built from the original scene (visual_spec/tier/
                    # reference_image_url are properties of the SCENE, not
                    # the shot) — never a shot-modified copy.
                    extra_scene_spec = build_scene_spec(scene, entity_hosted_urls={})
                    extra_scene_spec["scene_id"] = extra_scene_id
                    _force_short_native_aspect(extra_scene_spec, config)
                    if planned_shot is not None:
                        # Selection, not construction (2026-08-07 refactor):
                        # swap in the shot's own pre-rendered prompt — the
                        # only thing this function is allowed to do with a
                        # planned shot's prompt text.
                        extra_scene_spec["production_spec"]["primary_prompt"] = (
                            planned_shot.get("primary_prompt", "")
                        )
                        extra_scene_spec["production_spec"]["fallback_prompt"] = (
                            planned_shot.get("fallback_prompt")
                            or planned_shot.get("primary_prompt", "")
                        )

                    output_dir.mkdir(parents=True, exist_ok=True)
                    _gate_kwargs = {"channel_id": channel_id} if channel_id is not None else {}
                    asset_result = submit_scene(
                        scene_spec=extra_scene_spec,
                        config=config,
                        episode_id=episode_id,
                        output_dir=output_dir,
                        agent="clip_repair_agent",
                        **_gate_kwargs,
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
                        # Traceability (2026-08-07 fix): which Cinematic
                        # Coverage Plan shot this clip's prompt was rendered
                        # from, or None on the legacy no-plan fallback path.
                        "planned_shot_id": planned_shot.get("shot_id") if planned_shot else None,
                    }
                    qualify_asset(
                        extra_asset, extra_path, beat.source_type, config, episode_id, log,
                        channel_id=channel_id, output_dir=output_dir,
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


def _is_billing_contradiction(failure_reason: str) -> bool:
    """
    Generic, provider-agnostic detector for a billing/credit-state
    rejection embedded in a failure reason string (see muapi.py's
    _poll_until_complete(), which now carries the provider's own response
    text through on a persistent-400 abort). Deliberately keyword-based
    and not tied to any one provider's exact wording, since the same
    class of failure (account balance) is inherently systemic — every
    subsequent shot in a batch would hit the identical wall — unlike a
    shot-specific rejection (e.g. bad prompt content), which is not a
    reason to stop the whole batch.
    """
    text = (failure_reason or "").lower()
    return "insufficient" in text and any(
        marker in text for marker in ("credit", "balance", "top up", "top-up", "top_up")
    )


def _next_free_extra_slot(
    sid: str,
    short_id: Optional[str],
    output_dir: Path,
    occupied_extra_ids: Set[str],
) -> "tuple[str, Path]":
    """
    Find the lowest-numbered sc_{sid}__extra{N} slot that is free, checking
    BOTH the caller-supplied occupied_extra_ids (manifest entries, plus any
    slot already reserved earlier in the same batch) AND the actual
    filesystem via muapi.scene_file_path() — the single canonical path
    builder, never reimplemented here. The filesystem check is
    authoritative even when a slot is completely absent from the manifest
    (every clip generated before planned_shot_id traceability existed):
    a real file on disk always wins over an incomplete manifest record.
    """
    from hypogeum.muapi import scene_file_path  # noqa: PLC0415

    slot_index = 1
    while True:
        extra_scene_id = f"{sid}__extra{slot_index}"
        path = scene_file_path(output_dir, extra_scene_id, short_id)
        if extra_scene_id not in occupied_extra_ids and not path.exists():
            return extra_scene_id, path
        slot_index += 1


def _manifest_extra_ids_by_scene(manifest_assets: List[dict]) -> Dict[str, Set[str]]:
    """Group every manifest asset entry whose scene_id encodes an
    __extraN suffix by its base scene_id — the manifest half of slot
    occupancy (see _next_free_extra_slot's docstring for why this alone
    is not sufficient)."""
    by_scene: Dict[str, Set[str]] = {}
    for a in manifest_assets:
        raw_sid = a.get("scene_id") or ""
        if "__extra" in raw_sid:
            base_sid = raw_sid.split("__extra", 1)[0]
            by_scene.setdefault(base_sid, set()).add(raw_sid)
    return by_scene


def plan_confirmed_shot_allocations(
    missing_shots: List[Dict[str, Optional[str]]],
    visual_package: dict,
    assets_root: Path,
    episode_id: str,
) -> List[Dict[str, object]]:
    """
    Read-only allocation preview for fill_confirmed_missing_shots(): for
    each requested missing shot, determine the exact extra_scene_id and
    output path it would receive if generation ran right now — without
    making any provider call, writing any file, or mutating
    visual_package. Reuses the identical slot-discovery logic
    (_next_free_extra_slot, manifest + filesystem, filesystem
    authoritative) so a dry run and the real run always agree on the
    same input state.

    Two shots requesting the same scene_id within one call correctly
    receive two distinct slots (tracked in-memory only, per call) — this
    is what makes the shot_id -> output_path mapping stable and
    collision-free within a batch, not just against pre-existing files.

    Fail-closed on scene identity (2026-08-10): raises
    SceneIdentityConflictError before computing any allocation if
    visual_package's own scenes contain a duplicate scene_id — allocating
    a destination path derived from a non-unique scene_id would be
    non-deterministic (which beat does the resulting file really belong
    to), so this must never silently proceed. See hypogeum.scene_identity.
    """
    from hypogeum.scene_identity import (  # noqa: PLC0415
        enforce_global_scene_identity,
        enforce_scene_internal_consistency,
    )

    scenes = visual_package.get("scenes", [])
    enforce_global_scene_identity(scenes)
    enforce_scene_internal_consistency(scenes)
    short_id_by_scene = {s.get("scene_id"): s.get("short_id") for s in scenes}
    manifest_assets = (visual_package.get("asset_manifest") or {}).get("assets", [])
    manifest_extra_ids_by_scene = _manifest_extra_ids_by_scene(manifest_assets)

    output_dir = assets_root / "episodes" / episode_id
    reserved_by_scene: Dict[str, Set[str]] = {}
    allocations: List[Dict[str, object]] = []

    for item in missing_shots:
        sid = item["scene_id"]
        short_id = item.get("short_id") or short_id_by_scene.get(sid)
        occupied = manifest_extra_ids_by_scene.get(sid, set()) | reserved_by_scene.setdefault(sid, set())
        extra_scene_id, path = _next_free_extra_slot(sid, short_id, output_dir, occupied)
        reserved_by_scene[sid].add(extra_scene_id)
        allocations.append({**item, "extra_scene_id": extra_scene_id, "output_path": str(path)})

    return allocations


GENERATION_COST_ESTIMATE_USD = 0.39
"""Per-shot cost for the current default production tier
(veo3.1-lite-text-to-video, Tier 2/3 — see hypogeum.muapi.
MODEL_CREDIT_COSTS). Used only for PREFLIGHT authorization math in
compute_confirmed_shot_costs()/authorize_compliant_shot_generation() —
real spend always comes from the provider's own returned cost."""

UPSCALE_COST_ESTIMATE_USD = 0.08
"""Conservative per-clip estimate for the configured explicit AI video
upscaler (dynamic pricing; measured $0.05-0.08/clip per hypogeum.
providers.muapi_video_upscaler's own docstring). Used only for PREFLIGHT
authorization math and as the pre-submission budget check in
fill_confirmed_missing_shots() — real spend always comes from the
provider's own returned EnhancementResult.cost_usd."""


def compute_confirmed_shot_costs(
    missing_count: int,
    generation_cost_per_shot: float = GENERATION_COST_ESTIMATE_USD,
    upscale_cost_per_shot: float = UPSCALE_COST_ESTIMATE_USD,
) -> Dict[str, float]:
    """
    Pure cost projection for filling `missing_count` confirmed shots — no
    provider call, no side effects. Distinguishes generation-only cost
    (what a caller who skips source qualification would spend) from the
    full compliant cost (generation plus one explicit upscale attempt per
    shot, worst case) — treating the former as authorization for the
    latter is exactly the contradiction this policy exists to prevent.
    """
    return {
        "missing_count": missing_count,
        "generation_cost_per_shot": generation_cost_per_shot,
        "upscale_cost_per_shot": upscale_cost_per_shot,
        "generation_only_cost_usd": round(missing_count * generation_cost_per_shot, 2),
        "compliant_cost_usd": round(
            missing_count * (generation_cost_per_shot + upscale_cost_per_shot), 2
        ),
    }


def authorize_compliant_shot_generation(
    missing_count: int,
    authorized_budget_credits: float,
    generation_cost_per_shot: float = GENERATION_COST_ESTIMATE_USD,
    upscale_cost_per_shot: float = UPSCALE_COST_ESTIMATE_USD,
) -> Dict[str, object]:
    """
    Fail-closed preflight authorization check — no provider call. Compares
    authorized_budget_credits against the full COMPLIANT projected cost
    (generation + explicit upscale for every shot), never the
    generation-only figure, since a budget authorized only for raw
    generation does not authorize the upscale step this policy requires
    before any shot may be registered. Callers must check the returned
    "authorized" flag and stop before any provider submission when False.
    """
    costs = compute_confirmed_shot_costs(
        missing_count, generation_cost_per_shot, upscale_cost_per_shot
    )
    authorized = authorized_budget_credits >= costs["compliant_cost_usd"]
    shortfall = max(0.0, costs["compliant_cost_usd"] - authorized_budget_credits)
    return {
        **costs,
        "authorized_budget_credits": authorized_budget_credits,
        "authorized": authorized,
        "shortfall_usd": round(shortfall, 2),
    }


def fill_confirmed_missing_shots(
    missing_shots: List[Dict[str, Optional[str]]],
    visual_package: dict,
    assets_root: Path,
    config: Config,
    episode_id: str,
    episode_budget_credits: float,
    forced_tier: Optional[int] = None,
    require_source_qualification: bool = False,
    upscale_budget_credits: float = 0.0,
    log=None,
    channel_id: Optional[str] = None,
) -> Dict[str, object]:
    """
    Generate exactly the caller-confirmed missing planned shots — the
    sibling of fill_coverage_deficits() for callers that have already
    determined, by their own bounded reconciliation, precisely which
    shot_ids need real footage (rather than letting a duration-deficit
    computation decide). Never used to fill ordinary coverage gaps; never
    used for a first-pass beat that has no confirmed missing-shot list.

    missing_shots: ordered list of {"beat_id", "shot_id", "scene_id",
    "short_id"} dicts — exactly the shots to generate, already vetted by
    the caller's own compatibility policy (e.g.
    hypogeum.shot_compatibility_policy). This function performs no
    selection, no duration math, and no diversity filtering of its own —
    it only executes the caller's explicit list, one paid generation
    attempt per shot, in order.

    require_source_qualification (default False, preserves prior
    behavior) routes every generated source through
    hypogeum.source_enhancement.qualify_asset() — the one canonical
    asset-qualification boundary already used everywhere else in this
    codebase — before a shot may be registered as usable. qualify_asset()
    itself: measures the ACTUAL downloaded source's dimensions (never
    trusts a provider's claimed resolution label or an "upgrade
    submitted" flag as proof); invokes the configured explicit AI video
    upscaler ONLY when the measured source is below the deliverable-
    specific requirement (hypogeum.artifact_compatibility.
    required_source_resolution(source_type) — correctly distinguishes
    long-form 1920x1080 from Short 1080x1920); re-probes the upscaled
    candidate independently before ever trusting it; and fails closed,
    quarantining (never deleting) any candidate that doesn't pass. A shot
    whose source is already natively compliant costs nothing extra here
    — qualify_asset() skips the provider call entirely in that case.

    Regardless of this flag, a shot whose raw generated source fails the
    existing free technical-validity check
    (hypogeum.artifact_compatibility.evaluate_video_clip_compatibility)
    is never registered — this was previously an unconditional-append
    bug (a corrupt/non-compliant clip could reach the manifest looking
    identical to a usable one); fixed here as part of the same
    registration-integrity guarantee this flag exists to provide, not a
    weakening of any existing threshold.

    Resumability: before generating, checks whether an earlier
    (possibly interrupted) call already produced a technically-valid
    source for this exact planned_shot_id that still exists on disk —
    if so, reuses it and its manifest entry instead of submitting a new
    paid generation request. This is what lets a source that generated
    successfully but never completed qualification (process stopped
    before the upscale step, or the upscale step itself failed and is
    being retried under corrected policy) be qualified later without
    paying for generation twice.

    Job-ID and metadata separation: each manifest entry records the
    GENERATION provider's own request_id as "generation_request_id"
    (source-side metadata) separately from whatever upscale attempt
    qualify_asset() records inside "source_enhancement" — including that
    provider's own "request_id" field — the FINAL-artifact metadata.
    Never conflated into a single field.

    Budget enforcement is real, not advisory, on both spend streams:
      - Generation: a muapi.MUAPIBudgetGuard is constructed from
        episode_budget_credits with max_paid_attempts=1 per scene_id, so
        neither this function nor submit_scene()'s own internal fallback
        (governed separately by config.MUAPI_ALLOW_PAID_FALLBACKS) can
        spend beyond what the caller authorized for generation.
      - Upscale (only when require_source_qualification=True): before
        calling qualify_asset() for a shot, checks whether the running
        upscale total plus one more UPSCALE_COST_ESTIMATE_USD would
        exceed upscale_budget_credits — stops the whole batch rather
        than submitting that request if so. Callers should derive
        upscale_budget_credits from authorize_compliant_shot_generation()
        rather than reusing episode_budget_credits for both streams.
    Stops immediately — returning whatever succeeded so far — on
    MUAPIBudgetExhausted/MUAPIAuthFailure/MUAPIQuotaExhausted, an
    exhausted upscale budget, or a billing-contradiction pattern
    (hypogeum.agents.clip_repair_agent._is_billing_contradiction) from
    EITHER the generation or the upscale provider response — a systemic
    account-state failure on either stream means every remaining shot
    would hit the identical wall. Any other per-shot failure is recorded
    and the loop continues to the next shot.

    forced_tier, when given, overrides EVERY shot's own scene-level
    provider_routing tier (which may specify Tier 1 for "hero" scenes at
    $2.50/gen) with this exact tier for both primary and fallback —
    letting a caller whose authorization specifies an exact model/tier
    guarantee the per-shot cost their budget math assumed. None (default)
    preserves each scene's own configured routing.

    Returns {"generated": [...], "failed": [...], "stopped_reason": str|None,
    "total_generation_cost_usd": float, "total_upscale_cost_usd": float,
    "total_cost_usd": float} — generated/failed are lists of the input
    missing_shots dicts, annotated with clip_path/cost fields or
    failure_reason respectively. A shot only appears in "generated" once
    it has passed every check this call was configured to require.

    channel_id (Commit F): forwarded to submit_scene()'s own channel_id
    parameter unchanged. When a real, verified grant resolves (never
    true in production until a human operator runs the separate
    Enforcement Cutover -- see the plan), forced_tier's effect on
    scene_spec is now gate-enforced for free: submit_scene()'s tier
    resolution reads the SAME (possibly forced) scene_spec this
    function already mutated above, so the compiled envelope's
    provider/model always reflects the actual forced tier, and
    hypogeum.submission_gate.submit()'s existing model/provider-match
    check denies any attempt whose resulting binding doesn't match what
    the grant/authorization actually cover -- no new enforcement code,
    the existing shared gate check already does this.
    """
    from hypogeum.agents.provider_router import build_scene_spec  # noqa: PLC0415
    from hypogeum.artifact_compatibility import evaluate_video_clip_compatibility  # noqa: PLC0415
    from hypogeum.muapi import (  # noqa: PLC0415
        MODEL_CREDIT_COSTS,
        MUAPIAuthFailure,
        MUAPIBudgetExhausted,
        MUAPIBudgetGuard,
        MUAPIQuotaExhausted,
        scene_file_path,
        submit_scene,
    )
    from hypogeum.source_enhancement import qualify_asset  # noqa: PLC0415
    from hypogeum.scene_identity import (  # noqa: PLC0415
        enforce_global_scene_identity,
        enforce_scene_internal_consistency,
    )

    if log is None:
        log = get_logger(episode_id=episode_id, agent="clip_repair_agent")

    scenes: List[dict] = visual_package.get("scenes", [])
    # Fail-closed on scene identity (2026-08-10), before any provider
    # submission: a duplicate scene_id makes the generation destination
    # non-deterministic (which beat does the resulting file belong to),
    # so this must stop the whole batch rather than submit anything
    # against an ambiguous scene. See hypogeum.scene_identity.
    enforce_global_scene_identity(scenes)
    enforce_scene_internal_consistency(scenes)
    scene_by_id: Dict[str, dict] = {s["scene_id"]: s for s in scenes}
    plan_by_beat = {
        s.get("narration_reference"): (s.get("cinematic_coverage_plan") or {})
        for s in scenes
    }
    output_dir = assets_root / "episodes" / episode_id
    manifest_assets: List[dict] = visual_package.setdefault(
        "asset_manifest", {}
    ).setdefault("assets", [])
    manifest_extra_ids_by_scene = _manifest_extra_ids_by_scene(manifest_assets)
    reserved_by_scene: Dict[str, Set[str]] = {}
    manifest_entry_by_shot_id: Dict[str, dict] = {
        a["planned_shot_id"]: a for a in manifest_assets if a.get("planned_shot_id")
    }

    guard = MUAPIBudgetGuard(
        episode_budget_credits=episode_budget_credits,
        scene_budget_credits=0,
        max_paid_attempts=1,
        log=log,
    )

    generated: List[dict] = []
    failed: List[dict] = []
    stopped_reason: Optional[str] = None
    total_generation_cost_usd = 0.0
    total_upscale_cost_usd = 0.0

    for item in missing_shots:
        beat_id = item["beat_id"]
        shot_id = item["shot_id"]
        sid = item["scene_id"]
        short_id = item.get("short_id")

        scene = scene_by_id.get(sid)
        plan = plan_by_beat.get(beat_id) or {}
        planned_shot = next(
            (sh for sh in plan.get("shots", []) if sh.get("shot_id") == shot_id), None
        )
        if scene is None or planned_shot is None:
            failed.append({**item, "failure_reason": "scene or planned_shot not found"})
            continue
        if not (planned_shot.get("primary_prompt") or "").strip():
            failed.append({**item, "failure_reason": "planned shot has no rendered primary_prompt"})
            continue

        source_type = scene.get("source_type")

        # --- Resumability: reuse an existing valid generated source
        # instead of paying for generation again (see docstring). ---
        manifest_entry = manifest_entry_by_shot_id.get(shot_id)
        resumed = False
        clip_path: Optional[Path] = None
        extra_scene_id: Optional[str] = None
        if manifest_entry is not None:
            prior_path = scene_file_path(output_dir, manifest_entry["scene_id"], short_id)
            if (
                manifest_entry.get("generation_status") == "generated"
                and manifest_entry.get("basic_integrity_ok", manifest_entry.get("technically_valid"))
                and prior_path.exists()
            ):
                clip_path = prior_path
                extra_scene_id = manifest_entry["scene_id"]
                resumed = True
                log.info(
                    "confirmed_shot_fill_resumed",
                    episode_id=episode_id, shot_id=shot_id, extra_scene_id=extra_scene_id,
                )

        if not resumed:
            occupied = manifest_extra_ids_by_scene.get(sid, set()) | reserved_by_scene.setdefault(sid, set())
            extra_scene_id, _predicted_path = _next_free_extra_slot(sid, short_id, output_dir, occupied)
            reserved_by_scene[sid].add(extra_scene_id)

            scene_spec = build_scene_spec(scene, entity_hosted_urls={})
            scene_spec["scene_id"] = extra_scene_id
            _force_short_native_aspect(scene_spec, config)
            scene_spec["production_spec"]["primary_prompt"] = planned_shot.get("primary_prompt", "")
            scene_spec["production_spec"]["fallback_prompt"] = (
                planned_shot.get("fallback_prompt") or planned_shot.get("primary_prompt", "")
            )
            if forced_tier is not None:
                scene_spec["production_spec"]["primary_tier"] = forced_tier
                scene_spec["production_spec"]["fallback_tier"] = forced_tier

            try:
                output_dir.mkdir(parents=True, exist_ok=True)
                _gate_kwargs = {"channel_id": channel_id} if channel_id is not None else {}
                asset_result = submit_scene(
                    scene_spec=scene_spec,
                    config=config,
                    episode_id=episode_id,
                    output_dir=output_dir,
                    agent="clip_repair_agent",
                    budget_guard=guard,
                    **_gate_kwargs,
                )
            except (MUAPIBudgetExhausted, MUAPIAuthFailure, MUAPIQuotaExhausted) as exc:
                stopped_reason = f"{type(exc).__name__}: {exc}"
                log.error(
                    "confirmed_shot_fill_stopped",
                    episode_id=episode_id, shot_id=shot_id, reason=stopped_reason,
                )
                break

            status = asset_result.get("generation_status", "generation_failed")
            clip_path = scene_file_path(output_dir, extra_scene_id, short_id)
            model_used = asset_result.get("model_used")
            generation_cost_usd = MODEL_CREDIT_COSTS.get(model_used, 0.0) if status == "generated" else 0.0

            if status != "generated" or not clip_path.exists():
                reason_text = asset_result.get("failure_reason") or f"status={status}"
                failed.append({**item, "failure_reason": reason_text})
                log.warn(
                    "confirmed_shot_fill_failed",
                    episode_id=episode_id, shot_id=shot_id, extra_scene_id=extra_scene_id,
                    status=status,
                )
                if _is_billing_contradiction(reason_text):
                    # A provider-reported billing/credit rejection is not a
                    # shot-specific failure -- every remaining shot would hit
                    # the identical wall. Stop the whole batch here rather
                    # than burning through the rest one at a time; the
                    # per-shot failure above is still recorded so this shot
                    # isn't silently dropped from the report.
                    stopped_reason = f"billing_contradiction: {reason_text}"
                    log.error(
                        "confirmed_shot_fill_stopped",
                        episode_id=episode_id, shot_id=shot_id, reason=stopped_reason,
                    )
                    break
                continue

            total_generation_cost_usd += generation_cost_usd

            # Deliverable-aware, structured technical check — the
            # canonical service itself (hypogeum.artifact_compatibility)
            # resolves the correct required resolution for source_type
            # and returns stable structured fields; this module never
            # re-derives or duplicates that decision from reason text.
            tech = evaluate_video_clip_compatibility(clip_path, source_type=source_type)

            manifest_entry = {
                "scene_id": extra_scene_id,
                "asset_id": asset_result.get("asset_id"),
                "generation_status": status,
                "provider": asset_result.get("provider"),
                "model": model_used,
                "tier_used": asset_result.get("tier_used"),
                "prompt_used": asset_result.get("prompt_used"),
                "fallback_used": asset_result.get("fallback_used", False),
                "planned_shot_id": shot_id,
                # Source-side job id, kept separate from any upscale job
                # id qualify_asset() records inside source_enhancement.
                "generation_request_id": asset_result.get("request_id"),
                "technically_valid": tech.decision.value == "valid_and_compatible",
                "technical_detail": tech.reasons,
                # basic_integrity_ok is True whenever the source is real,
                # playable, non-corrupt, non-placeholder footage — even if
                # it fails the resolution requirement, since a resolution
                # shortfall alone is exactly what the explicit upscale
                # step (require_source_qualification=True) exists to fix,
                # not evidence of a broken source. A caller that does NOT
                # intend to upscale must still gate on technically_valid
                # (integrity AND resolution), not this field alone.
                "basic_integrity_ok": bool(tech.integrity_valid),
                "measured_resolution": tech.measured_resolution,
                "required_resolution": tech.required_resolution,
                "failure_codes": tech.failure_codes,
                "source_enhancement": None,
            }
            manifest_assets.append(manifest_entry)
            manifest_entry_by_shot_id[shot_id] = manifest_entry

        # --- From here, manifest_entry/clip_path/extra_scene_id are set
        # whether this shot was freshly generated or resumed. ---

        # A resolution-only shortfall is not corruption -- it's exactly
        # what require_source_qualification=True's upscale step exists to
        # fix. Gate on basic_integrity_ok (real, playable, non-corrupt,
        # non-placeholder) here; the full deliverable-aware
        # technically_valid check still applies below for the
        # no-qualification path, and again implicitly via qualify_asset()
        # for the qualification path.
        if not manifest_entry.get("basic_integrity_ok", manifest_entry.get("technically_valid")):
            failed.append({
                **item,
                "clip_path": str(clip_path),
                "failure_reason": "generated source failed technical validation",
            })
            continue

        if not require_source_qualification:
            if not manifest_entry.get("technically_valid"):
                failed.append({
                    **item,
                    "clip_path": str(clip_path),
                    "failure_reason": (
                        manifest_entry.get("technical_detail", ["resolution non-compliant"])[0]
                    ),
                })
                continue
            generated.append({
                **item,
                "clip_path": str(clip_path),
                "technically_valid": True,
            })
            log.info(
                "confirmed_shot_fill_generated",
                episode_id=episode_id, shot_id=shot_id, extra_scene_id=extra_scene_id,
            )
            continue

        # --- Explicit source qualification / upscale step ---
        if total_upscale_cost_usd + UPSCALE_COST_ESTIMATE_USD > upscale_budget_credits:
            stopped_reason = (
                f"upscale_budget_would_be_exceeded: "
                f"{total_upscale_cost_usd + UPSCALE_COST_ESTIMATE_USD:.2f} > {upscale_budget_credits:.2f}"
            )
            log.error(
                "confirmed_shot_fill_stopped",
                episode_id=episode_id, shot_id=shot_id, reason=stopped_reason,
            )
            break

        qualify_asset(
            manifest_entry, clip_path, source_type, config, episode_id, log,
            channel_id=channel_id, output_dir=output_dir,
        )
        se = manifest_entry.get("source_enhancement") or {}
        if se.get("applied"):
            total_upscale_cost_usd += se.get("cost_usd") or 0.0

        qualified = se.get("validation_result") in ("passed", "skipped_native_resolution")
        if not qualified:
            reason_text = se.get("failure_reason") or se.get("validation_result") or "qualification_failed"
            failed.append({
                **item,
                "clip_path": str(clip_path),
                "failure_reason": reason_text,
            })
            log.warn(
                "confirmed_shot_fill_qualification_failed",
                episode_id=episode_id, shot_id=shot_id, extra_scene_id=extra_scene_id,
                reason=reason_text,
            )
            if se.get("applied") and _is_billing_contradiction(reason_text):
                stopped_reason = f"billing_contradiction: {reason_text}"
                log.error(
                    "confirmed_shot_fill_stopped",
                    episode_id=episode_id, shot_id=shot_id, reason=stopped_reason,
                )
                break
            continue

        generated.append({
            **item,
            "clip_path": str(clip_path),
            "technically_valid": True,
            "qualification": se.get("validation_result"),
        })
        log.info(
            "confirmed_shot_fill_generated",
            episode_id=episode_id, shot_id=shot_id, extra_scene_id=extra_scene_id,
            qualification=se.get("validation_result"),
        )

    total_cost_usd = total_generation_cost_usd + total_upscale_cost_usd
    log.info(
        "confirmed_shot_fill_done",
        episode_id=episode_id,
        requested=len(missing_shots),
        generated=len(generated),
        failed=len(failed),
        total_generation_cost_usd=round(total_generation_cost_usd, 2),
        total_upscale_cost_usd=round(total_upscale_cost_usd, 2),
        total_cost_usd=round(total_cost_usd, 2),
        stopped_reason=stopped_reason,
    )

    return {
        "generated": generated,
        "failed": failed,
        "stopped_reason": stopped_reason,
        "total_generation_cost_usd": total_generation_cost_usd,
        "total_upscale_cost_usd": total_upscale_cost_usd,
        "total_cost_usd": total_cost_usd,
    }


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
# Shot-diversity wiring — ownership note (2026-08-07, refactored)
#
# fill_coverage_deficits() used to call build_scene_spec() with the same
# unmodified scene dict for every extra slot, so extra1/extra2/extra3...
# for one beat always carried the identical production_spec.provider_
# routing.primary_prompt -- byte-distinct generated files that were
# perceptually/compositionally the same shot repeated for 30-47s, exactly
# what a real production acceptance review flagged for ep_f3619022's scenes
# s29 and s11.
#
# This module is NOT a second Prompt Writer and must never become one:
#   - hypogeum.agents.cinematic_director decides the ordered planned shots
#     (camera_angle, shot_scale, composition, etc.) and owns shot IDENTITY
#     — shot_diversity_key(), imported below, is its single source of
#     truth for "what makes two shots the same shot."
#   - hypogeum.video_prompt_writer.render_coverage_plan_shot_prompts(),
#     wired into visual_package_director.py right after Stage B, renders
#     each planned shot's fields into actual prompt text, once, during VPD
#     — before Assembly or Repair ever runs.
#   - This module (Repair) only SELECTS one already-rendered
#     planned_shot["primary_prompt"] / ["fallback_prompt"] per extra slot.
#     No prompt templates, no text construction, no camera-directive
#     rendering live here — see fill_coverage_deficits()'s slot loop below.
# ---------------------------------------------------------------------------


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
