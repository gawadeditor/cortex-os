"""
hypogeum/agents/visual_package_director.py — Visual Package Director agent (M2.3/M2.4)

Two-phase execution + Thumbnail Handoff Brief extraction:

  Phase 1 — Claude planning (Stage A split into A1 + A2 + A3 + A4 + Diversity + Visual Intent QA):

    Stage A1 — metadata (vpd_skeleton_meta schema):
      Claude receives the compact script_package and produces all non-scene sections of
      the Visual Package: package_metadata, episode_metadata, visual_continuity_record
      (entities + palettes), provider_routing_manifest, visual_integrity_log,
      thumbnail_handoff_brief, asset_manifest, package_integrity.
      Max tokens: 4,096. Checkpoint: vpd_a1_meta_{episode_id}.json.

    Stage A2 — scene skeleton batches (vpd_skeleton_scene_batch schema):
      Claude receives a batch of narration beats plus the entity registry from A1 and
      returns a {"scenes": [...]} dict. One call per batch of _SCENE_SKELETON_BATCH_SIZE
      beats. Max tokens: 4,096 per batch.
      Checkpoints: vpd_a2_scenes_batch_{i}_{episode_id}.json.

    Stage A3 — Python merge:
      The A1 metadata dict and all A2 scene lists are merged into the full Visual Package
      skeleton. Scene counts in episode_metadata, package_integrity, and
      provider_routing_manifest are patched from the actual scene list.
      Writes: visual_package_skeleton_{episode_id}.json.

    Stage A4 — Scene Presentation Planner (vpd_presentation_plan_batch schema):
      For every scene, Claude determines HOW the narrative intent will be communicated
      cinematically — selecting a presentation_method from the Visual Language Engine
      and defining camera_strategy, spatial_relationship, and forbidden_techniques.
      This plan is injected into each scene before Stage B writes prompts.
      Max tokens: 4,096. Checkpoint: vpd_a4_presentation_{episode_id}.json.

    Shot Diversity Planner:
      Assigns shot_scale, camera_movement, camera_angle, composition, color_palette,
      environment, subject, visual_motif, and reuse_allowed to every scene.
      Runs after A4 and before B. Checkpoint: sdp_{episode_id}.json.

    Visual Intent QA:
      Verifies every written primary_prompt satisfies its presentation_plan before
      generation credits are spent. Scenes that fail are retried with rejection_reason
      injected into Stage B context. Max 1 retry per scene.
      Checkpoint: vpd_visual_intent_qa_{episode_id}.json.

  Stage B — cinematic prompts (batched):
    Claude writes primary_prompt + fallback_prompt for each scene. Batched at
    _SCENE_PROMPT_BATCH_SIZE scenes per call. Max tokens: 8,192 per batch.
    Checkpoints: vpd_prompts_batch_{i}_{episode_id}.json.

  Phase 2 — MUAPI execution:
    For each entity with lock_state == "final":
        generate_reference_image() → stores locally + uploads → gets hosted_url.
        Updates entity.reference_image_asset_id in memory.

    For each scene (in scene_index order):
        Build scene_spec from the planned scene, injecting reference_image_url
        for any primary entity that has a hosted_url from Phase 2a.
        Call submit_scene() → gets generation result.
        Updates asset_manifest entry with actual generation_status.

  After Phase 2:
    Re-validate the updated package and write the final version to disk,
    overwriting the planned version written by run_agent().

  M2.4 — Thumbnail Handoff Brief extraction:
    extract_thumbnail_handoff_brief() pulls thumbnail_handoff_brief from the
    finalized Visual Package, wraps it with envelope metadata, validates it
    against the standalone thumbnail_handoff_brief schema, and writes it to
    thumbnail_handoff_brief_{episode_id}.json. This is the Thumbnail Director's
    sole input — TD never reads the full Visual Package.

Public API:
    run_visual_package_director(script_package, config, episode_id, output_dir)
        → dict  (final Visual Package)

    extract_thumbnail_handoff_brief(visual_package, episode_id, output_dir)
        → dict  (standalone Thumbnail Handoff Brief)

Integration contracts (must not be violated):
  - All Claude calls go through run_agent() — never call_claude() directly
  - System prompt loaded from disk — never hardcoded inline
  - Output validated by run_agent() via schemas.py
  - No environment access — all settings via Config
  - All logging via get_logger() — never print() or stdlib logging
  - MUAPI calls go through muapi.submit_scene() and muapi.generate_reference_image()

Model: Claude Sonnet 4.6 (frozen — VPD governance decision)

Logging events emitted:
  vpd_planning_started          — info  — before Stage A begins
  vpd_stage_a1_started          — info  — before Stage A1 run_agent() call
  vpd_stage_a1_resumed          — info  — Stage A1 checkpoint found, skipping call
  vpd_stage_a1_completed        — info  — Stage A1 written to checkpoint
  vpd_stage_a2_batch_started    — info  — before each Stage A2 run_agent() call
  vpd_stage_a2_batch_resumed    — info  — Stage A2 batch checkpoint found, skipping call
  vpd_stage_a2_batch_completed  — info  — Stage A2 batch merged into all_scenes
  vpd_stage_a_completed         — info  — full skeleton written after A3 merge
  vpd_stage_a_resumed           — info  — full skeleton checkpoint found, skipping A1/A2/A3
  vpd_stage_b_batch_started     — info  — before each Stage B run_agent() call
  vpd_stage_b_batch_resumed              — info  — Stage B batch checkpoint found, skipping call
  vpd_stage_b_batch_completed            — info  — Stage B batch prompts merged into skeleton
  vpd_stage_b_batch_overflow             — warn  — TruncatedResponse; batch halved and retried
  vpd_stage_b_batch_overflow_unsplittable — error — batch_size=1 still truncates; re-raised
  vpd_stage_a4_started                   — info  — before Stage A4 run_agent() call
  vpd_stage_a4_resumed                   — info  — Stage A4 checkpoint found, skipping call
  vpd_stage_a4_completed                 — info  — Stage A4 written to checkpoint
  vpd_shot_diversity_started             — info  — before Shot Diversity Planner
  vpd_shot_diversity_completed           — info  — after Shot Diversity Planner
  vpd_visual_intent_qa_started           — info  — before Visual Intent QA
  vpd_visual_intent_qa_completed         — info  — after Visual Intent QA
  vpd_visual_intent_qa_scene_rejected    — warn  — scene failed QA, retry
  vpd_visual_intent_qa_scene_retry_failed — error — scene still failed after retry
  vpd_planning_completed                 — info  — all planning stages done
  vpd_muapi_phase_started       — info  — before MUAPI execution loop
  vpd_ref_image_started         — info  — before each generate_reference_image()
  vpd_ref_image_completed       — info  — after each generate_reference_image()
  vpd_ref_image_failed          — warn  — generate_reference_image() raised
  vpd_scene_generation_start    — info  — before each submit_scene()
  vpd_scene_generated           — info  — scene successfully generated
  vpd_scene_failed              — warn  — scene generation_failed (pipeline continues)
  vpd_muapi_phase_completed     — info  — after all scenes processed
  vpd_package_finalized         — info  — final package written to disk
  vpd_handoff_brief_written     — info  — thumbnail handoff brief written to disk
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from hypogeum.agent import run_agent
from hypogeum.agents.entity_continuity_manager import apply_reference_image_asset_ids
from hypogeum.agents.image_generation_agent import generate_entity_reference_images
from hypogeum.agents.provider_router import update_routing_manifest
from hypogeum.agents.scene_planner import build_scene_plan_input, order_and_filter_scenes
from hypogeum.agents.thumbnail_handoff_brief_builder import build_thumbnail_handoff_brief
from hypogeum.agents.video_generation_agent import generate_all_scene_videos
from hypogeum.config import Config
from hypogeum.logger import get_logger
from hypogeum.prompt_loader import compose_system_prompt
from hypogeum import skill_registry as _skill_registry
from hypogeum.muapi import (
    MUAPIAuthFailure,
    MUAPIBudgetExhausted,
    MUAPIBudgetGuard,
    MUAPIQuotaExhausted,
)
from hypogeum.openrouter import TruncatedResponse
from hypogeum.schemas import SchemaError, validate


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CLAUDE_SONNET   = "anthropic/claude-sonnet-4.6"
_AGENT_NAME      = "visual_package_director"

# Stage A1: non-scene metadata — budget 6,144 (entities × 6, palettes, thumbnail brief)
# Rationale: A1 output is bounded (≤6 entities, ≤3 thumbnail candidates, no scenes)
# but can reach 3,000-5,000 tokens for complex multi-entity episodes.
_VPD_SKELETON_META_MAX_TOKENS        = 6_144
# Stage A2: scene skeleton per batch — budget 4,096 (12 scenes × ~250 tok ≈ 3,000 tok output)
_VPD_SKELETON_SCENE_BATCH_MAX_TOKENS = 4_096
# Hard ceiling on beats per Stage A2 batch (never exceed regardless of estimate)
_SCENE_SKELETON_BATCH_SIZE           = 12
# Estimated output tokens per scene in an A2 call (8-word visual_spec × 9 fields + overhead)
_A2_TOKENS_PER_SCENE_BASE            = 280
# Tokens added per locked entity (entity_appearances + A2 input overhead per entity)
_A2_TOKENS_PER_ENTITY                = 12
# Safety factor applied to per-scene token estimate before budget comparison
_A2_SAFETY_FACTOR                    = 1.2
# Fraction of A2 max_tokens budget used for scene output (rest is JSON overhead, prompt)
_A2_OUTPUT_BUDGET_RATIO              = 0.80

# Stage B: cinematic prompts per batch — budget 8,192 = ~25% of 32,768 ceiling
_VPD_PROMPT_BATCH_MAX_TOKENS = 8_192
# Scenes per Stage B batch (10 × ~580 tok/scene ≈ 5,800 tok output — safe)
_SCENE_PROMPT_BATCH_SIZE    = 10

# Legacy constant kept for run_visual_package_director_from_plan (no Stage A/B needed there)
_VPD_MAX_TOKENS  = 32_768

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_SKELETON_META_PROMPT_FILE   = _PROMPTS_DIR / "vpd_skeleton_meta.txt"
_SKELETON_SCENE_PROMPT_FILE  = _PROMPTS_DIR / "vpd_skeleton_scene_batch.txt"
_SCENE_PROMPTS_PROMPT_FILE   = _PROMPTS_DIR / "vpd_scene_prompts.txt"
# Legacy prompt file — still used by run_visual_package_director_from_plan if needed
_PROMPT_FILE = _PROMPTS_DIR / "visual_package_director.txt"

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills" / "visual_package_director"
_CINEMATIC_SKILL_FILE = _SKILLS_DIR / "cinematic_prompt_architecture.md"

# Visual Intelligence skill files
# B1: VLE compact version used by default for Stage A4 (~5.1K chars vs 11K full).
_VI_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills" / "visual_intelligence"
_COMPILED_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills" / "compiled"
_VLE_SKILL_FILE = _COMPILED_SKILLS_DIR / "visual_language_engine.compact.md"

# Provider translation skills — wired to Stage B (scene prompt writing)
# B1: compact runtime versions used by default (~8K chars each vs 64K/56K full).
# Full skills retained at hypogeum/skills/{veo,seedance}_translation_skill.md for audit.
_COMPILED_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills" / "compiled"
_VEO_TRANSLATION_SKILL_FILE      = _COMPILED_SKILLS_DIR / "veo_translation_skill.compact.md"
_SEEDANCE_TRANSLATION_SKILL_FILE = _COMPILED_SKILLS_DIR / "seedance_translation_skill.compact.md"

# All skill paths used by VPD — used for diagnostic logging
_VPD_ALL_SKILL_PATHS: list[Path] = [
    _CINEMATIC_SKILL_FILE,
    _VLE_SKILL_FILE,
    _VEO_TRANSLATION_SKILL_FILE,
    _SEEDANCE_TRANSLATION_SKILL_FILE,
]

# Stage A4 — Scene Presentation Planner
_SCENE_PRESENTATION_PLANNER_PROMPT_FILE = _PROMPTS_DIR / "vpd_scene_presentation_planner.txt"
_VPD_PRESENTATION_PLAN_MAX_TOKENS = 4_096
_PRESENTATION_PLAN_BATCH_SIZE = 15

# Visual Intent QA prompt
_VISUAL_INTENT_QA_PROMPT_FILE = _PROMPTS_DIR / "vpd_visual_intent_qa.txt"
_VISUAL_INTENT_QA_MAX_TOKENS = 2_048


# ---------------------------------------------------------------------------
# System prompts — loaded once at import time
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    return compose_system_prompt(_PROMPT_FILE, _CINEMATIC_SKILL_FILE)

def _load_meta_prompt() -> str:
    return compose_system_prompt(_SKELETON_META_PROMPT_FILE)

def _load_scene_batch_prompt() -> str:
    return compose_system_prompt(_SKELETON_SCENE_PROMPT_FILE)

def _load_scene_prompts_prompt() -> str:
    return compose_system_prompt(
        _SCENE_PROMPTS_PROMPT_FILE,
        _CINEMATIC_SKILL_FILE,
        _VEO_TRANSLATION_SKILL_FILE,
        _SEEDANCE_TRANSLATION_SKILL_FILE,
    )

def _load_presentation_planner_prompt() -> str:
    return compose_system_prompt(_SCENE_PRESENTATION_PLANNER_PROMPT_FILE, _VLE_SKILL_FILE)

def _load_visual_intent_qa_prompt() -> str:
    return compose_system_prompt(_VISUAL_INTENT_QA_PROMPT_FILE)


SYSTEM_PROMPT: str              = _load_system_prompt()
META_PROMPT: str                = _load_meta_prompt()
SCENE_BATCH_PROMPT: str         = _load_scene_batch_prompt()
SCENE_PROMPTS_PROMPT: str       = _load_scene_prompts_prompt()
PRESENTATION_PLANNER_PROMPT: str = _load_presentation_planner_prompt()
VISUAL_INTENT_QA_PROMPT: str    = _load_visual_intent_qa_prompt()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_visual_package_director(
    script_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Execute the Visual Package Director end-to-end.

    Phase 1: Claude Sonnet 4.6 plans all scenes, entities, and thumbnail
    candidates. The plan is validated against the visual_package schema and
    written to disk by run_agent() (generation_status = "planned").

    Phase 2: For each final-locked entity, generate_reference_image() is called
    and the hosted URL is stored in memory. Then submit_scene() is called for
    every scene (in scene_index order), injecting the reference_image_url for
    any primary entity with a hosted URL. The package is updated with actual
    generation results and re-written to disk.

    Args:
        script_package: Validated script_package dict from the Script Engine.
        config:         Loaded Config instance.
        episode_id:     Episode identifier for log correlation and file naming.
        output_dir:     Optional output directory override (passed to run_agent()).

    Returns:
        The final Visual Package dict with actual generation results.

    Raises:
        MUAPIAuthFailure      — HTTP 401 during MUAPI execution (all-stop).
        MUAPIQuotaExhausted   — HTTP 402 during MUAPI execution (all-stop).
        ResponseParseError    — Claude returned non-dict JSON in Phase 1.
        SchemaError           — Phase 1 output failed schema validation.
    """
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)
    _skill_registry.log_loaded_skills(_AGENT_NAME, _VPD_ALL_SKILL_PATHS, log)

    # ------------------------------------------------------------------
    # Phase 1 — Claude planning
    # ------------------------------------------------------------------
    sections_count = len(script_package.get("sections", []))
    shorts_count   = len(script_package.get("shorts_scripts", []))

    # Determine generation mode flags for logging before delegating to scene_planner.
    is_episode_zero = (
        episode_id == "ep_000"
        or config.EPISODE_ZERO_MAX_SCENES > 0
    )
    dedicated_shorts_enabled = config.ENABLE_DEDICATED_SHORT_GENERATION
    suppress_shorts = is_episode_zero or not dedicated_shorts_enabled

    if not dedicated_shorts_enabled and not is_episode_zero:
        log.info(
            "vpd_dedicated_shorts_disabled",
            episode_id=episode_id,
            reason="ENABLE_DEDICATED_SHORT_GENERATION=false",
        )

    log.info(
        "vpd_planning_started",
        sections_count=sections_count,
        shorts_count=shorts_count,
        episode_zero_mode=is_episode_zero,
        suppress_shorts=suppress_shorts,
    )

    _resolve_output_dir = (
        output_dir
        if output_dir is not None
        else Path(__file__).resolve().parent.parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
    )
    _resolve_output_dir.mkdir(parents=True, exist_ok=True)

    input_package = build_scene_plan_input(script_package, config, episode_id)

    constraints = input_package.get("generation_constraints")
    if constraints:
        reason = "episode_zero" if is_episode_zero else "dedicated_shorts_disabled"
        log_kwargs = {k: v for k, v in constraints.items()}
        log_kwargs["reason"] = reason
        log.info("vpd_generation_constraints_injected", **log_kwargs)

    # ------------------------------------------------------------------
    # Stage A — Skeleton planning (no cinematic prompts)
    # Split into A1 (metadata) + A2 (scene batches) + A3 (merge).
    # Final checkpoint: visual_package_skeleton_{episode_id}.json
    # ------------------------------------------------------------------
    skeleton_path = _resolve_output_dir / f"visual_package_skeleton_{episode_id}.json"
    if skeleton_path.exists():
        log.info("vpd_stage_a_resumed", path=str(skeleton_path))
        visual_package = json.loads(skeleton_path.read_text(encoding="utf-8"))
    else:
        # ── Stage A1: non-scene metadata ──────────────────────────────
        # Checkpoint: vpd_a1_meta_{episode_id}.json
        a1_path = _resolve_output_dir / f"vpd_a1_meta_{episode_id}.json"
        if a1_path.exists():
            log.info("vpd_stage_a1_resumed", path=str(a1_path))
            meta_package = json.loads(a1_path.read_text(encoding="utf-8"))
        else:
            a1_input = {**input_package, "task": "plan_visual_package_meta"}
            log.info("vpd_stage_a1_started", max_tokens=_VPD_SKELETON_META_MAX_TOKENS)
            meta_package = run_agent(
                agent_name=_AGENT_NAME,
                model_slug=_CLAUDE_SONNET,
                system_prompt=META_PROMPT,
                input_package=a1_input,
                output_schema="vpd_skeleton_meta",
                episode_id=episode_id,
                max_tokens=_VPD_SKELETON_META_MAX_TOKENS,
                config=config,
                output_dir=None,
            )
            a1_path.write_text(
                json.dumps(meta_package, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            log.info(
                "vpd_stage_a1_completed",
                entities=len(
                    meta_package.get("visual_continuity_record", {}).get("locked_entities", [])
                ),
                path=str(a1_path),
            )

        # ── Stage A2: scene skeleton batches ─────────────────────────
        # Batches are sized dynamically: max _SCENE_SKELETON_BATCH_SIZE, shrunk
        # when estimated token load per scene is high (many entities, long context).
        # Checkpoint: vpd_a2_scenes_batch_{i}_{episode_id}.json
        locked_entities = (
            meta_package.get("visual_continuity_record", {}).get("locked_entities", [])
        )
        compact_entities = _compact_entities_for_a2(locked_entities)
        beats = _flatten_beats_for_a2(input_package)
        scene_batches = _compute_a2_batches(beats, len(compact_entities))
        log.info(
            "vpd_stage_a2_plan",
            total_beats=len(beats),
            total_batches=len(scene_batches),
            batch_sizes=[len(b) for b in scene_batches],
            entities=len(compact_entities),
        )
        all_scenes: list = []
        start_index = 0
        for batch_idx, batch_beats in enumerate(scene_batches):
            a2_path = _resolve_output_dir / f"vpd_a2_scenes_batch_{batch_idx}_{episode_id}.json"
            if a2_path.exists():
                log.info("vpd_stage_a2_batch_resumed", batch=batch_idx, path=str(a2_path))
                batch_result = json.loads(a2_path.read_text(encoding="utf-8"))
            else:
                a2_input: dict = {
                    "task": "plan_scene_batch",
                    "batch_index": batch_idx,
                    "start_scene_index": start_index,
                    "episode_metadata": meta_package.get("episode_metadata", {}),
                    "locked_entities": compact_entities,
                    "period_palettes": (
                        meta_package.get("visual_continuity_record", {}).get("period_palettes", [])
                    ),
                    "provider_capabilities": input_package.get("provider_capabilities", {}),
                    "beats": batch_beats,
                }
                if input_package.get("generation_constraints"):
                    a2_input["generation_constraints"] = input_package["generation_constraints"]
                log.info(
                    "vpd_stage_a2_batch_started",
                    batch=batch_idx,
                    beats_in_batch=len(batch_beats),
                    start_scene_index=start_index,
                    max_tokens=_VPD_SKELETON_SCENE_BATCH_MAX_TOKENS,
                )
                batch_result = run_agent(
                    agent_name=_AGENT_NAME,
                    model_slug=_CLAUDE_SONNET,
                    system_prompt=SCENE_BATCH_PROMPT,
                    input_package=a2_input,
                    output_schema="vpd_skeleton_scene_batch",
                    episode_id=episode_id,
                    max_tokens=_VPD_SKELETON_SCENE_BATCH_MAX_TOKENS,
                    config=config,
                    output_dir=None,
                )
                a2_path.write_text(
                    json.dumps(batch_result, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            batch_scenes = batch_result.get("scenes", [])
            all_scenes.extend(batch_scenes)
            start_index += len(batch_scenes)
            log.info(
                "vpd_stage_a2_batch_completed",
                batch=batch_idx,
                scenes_in_batch=len(batch_scenes),
                total_scenes_so_far=len(all_scenes),
            )

        # ── Stage A3: merge A1 + A2 batches → validate → write skeleton ──
        visual_package = _merge_a1_a2(meta_package, all_scenes, episode_id)
        try:
            validate("visual_package", visual_package)
        except SchemaError as exc:
            log.warn(
                "vpd_stage_a3_validation_failed",
                path=exc.path,
                reason=exc.reason,
                scenes=len(all_scenes),
                note="A1/A2 checkpoint files preserved — fix and rerun",
            )
            raise SchemaError(
                "visual_package",
                exc.path,
                f"Stage A3 merged package failed validation: {exc.reason}. "
                f"A1/A2 checkpoints preserved for debugging.",
            )
        skeleton_path.write_text(
            json.dumps(visual_package, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info(
            "vpd_stage_a_completed",
            scenes=len(all_scenes),
            path=str(skeleton_path),
        )

    # ------------------------------------------------------------------
    # Stage A4 — Scene Presentation Planner
    # For every scene, determine HOW the narrative intent should be
    # communicated cinematically BEFORE prompts are written.
    # This makes presentation decisions immutable — Stage B cannot
    # override them, only satisfy them.
    # Checkpoint: vpd_a4_presentation_{episode_id}.json
    # ------------------------------------------------------------------
    a4_path = _resolve_output_dir / f"vpd_a4_presentation_{episode_id}.json"
    if a4_path.exists():
        log.info("vpd_stage_a4_resumed", path=str(a4_path))
        presentation_plans_result = json.loads(a4_path.read_text(encoding="utf-8"))
    else:
        scenes_for_a4 = [
            {
                "scene_id": s.get("scene_id"),
                "scene_index": s.get("scene_index"),
                "source_type": s.get("source_type"),
                "visual_spec": s.get("visual_spec", {}),
                "narrative_signals": s.get("narrative_signals", []),
            }
            for s in visual_package.get("scenes", [])
        ]
        a4_input = {
            "task": "plan_scene_presentations",
            "episode_metadata": visual_package.get("episode_metadata", {}),
            "scenes": scenes_for_a4,
        }
        log.info(
            "vpd_stage_a4_started",
            scenes=len(scenes_for_a4),
            max_tokens=_VPD_PRESENTATION_PLAN_MAX_TOKENS,
        )
        presentation_plans_result = run_agent(
            agent_name=_AGENT_NAME,
            model_slug=_CLAUDE_SONNET,
            system_prompt=PRESENTATION_PLANNER_PROMPT,
            input_package=a4_input,
            output_schema="vpd_presentation_plan_batch",
            episode_id=episode_id,
            max_tokens=_VPD_PRESENTATION_PLAN_MAX_TOKENS,
            config=config,
            output_dir=None,
        )
        a4_path.write_text(
            json.dumps(presentation_plans_result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info(
            "vpd_stage_a4_completed",
            plans=len(presentation_plans_result.get("presentation_plans", [])),
            path=str(a4_path),
        )

    # Inject presentation_plan into each scene (keyed by scene_id)
    _presentation_plan_map = {
        p["scene_id"]: p
        for p in presentation_plans_result.get("presentation_plans", [])
    }
    for scene in visual_package.get("scenes", []):
        sid = scene.get("scene_id")
        if sid in _presentation_plan_map:
            scene["presentation_plan"] = _presentation_plan_map[sid]

    # ------------------------------------------------------------------
    # Shot Diversity Planner — distributes visual variety across episode
    # Must run after presentation planning (A4) and before prompt writing (B)
    # so Stage B knows the assigned shot_scale, camera_movement etc.
    # ------------------------------------------------------------------
    log.info(
        "vpd_shot_diversity_started",
        episode_id=episode_id,
        scenes=len(visual_package.get("scenes", [])),
    )
    from hypogeum.agents.shot_diversity_planner import run_shot_diversity_planner  # noqa: PLC0415
    diversity_plan = run_shot_diversity_planner(
        visual_package=visual_package,
        episode_id=episode_id,
        config=config,
        output_dir=_resolve_output_dir,
    )
    # Inject diversity assignments into each scene
    _diversity_map = {
        a["scene_id"]: a
        for a in diversity_plan.get("scene_assignments", [])
    }
    for scene in visual_package.get("scenes", []):
        sid = scene.get("scene_id")
        if sid in _diversity_map:
            scene["diversity_assignment"] = _diversity_map[sid]
    log.info(
        "vpd_shot_diversity_completed",
        episode_id=episode_id,
        assignments=len(_diversity_map),
        diversity_score=diversity_plan.get("diversity_score", 0.0),
    )

    # ------------------------------------------------------------------
    # Stage B — Cinematic prompts (batched, SCENE_PROMPT_BATCH_SIZE per call)
    # Checkpoint: vpd_prompts_batch_{i}_{episode_id}.json per batch.
    #
    # Uses a while loop so batch size can be halved when TruncatedResponse
    # is raised (finish_reason == "length"). Each halving is logged and the
    # same scene range is retried at the smaller size. If batch_size reaches
    # 1 and the call still truncates, the error is re-raised (unsplittable).
    #
    # On checkpoint resume, scene_cursor advances by the actual number of
    # prompts in the checkpoint file — safe even when batch sizes differ
    # between the original run and the resume.
    # ------------------------------------------------------------------
    scenes = visual_package.get("scenes", [])
    scene_cursor = 0
    batch_idx = 0
    _b_batch_size = _SCENE_PROMPT_BATCH_SIZE  # local mutable — may be halved on overflow

    while scene_cursor < len(scenes):
        batch = scenes[scene_cursor : scene_cursor + _b_batch_size]
        if not batch:
            break

        batch_path = _resolve_output_dir / f"vpd_prompts_batch_{batch_idx}_{episode_id}.json"

        if batch_path.exists():
            log.info("vpd_stage_b_batch_resumed", batch=batch_idx, path=str(batch_path))
            prompts_result = json.loads(batch_path.read_text(encoding="utf-8"))
            n_in_ckpt = len(prompts_result.get("scene_prompts", []))
            _merge_scene_prompts(visual_package, prompts_result)
            log.info(
                "vpd_stage_b_batch_completed",
                batch=batch_idx,
                prompts_merged=n_in_ckpt,
                resumed=True,
            )
            # Advance by actual count in checkpoint (may differ if batch_size changed)
            scene_cursor += n_in_ckpt if n_in_ckpt > 0 else len(batch)
            batch_idx += 1
            continue

        batch_input = {
            "task": "write_scene_prompts",
            "episode_metadata": visual_package.get("episode_metadata", {}),
            "scenes": [
                {
                    "scene_id": s.get("scene_id"),
                    "scene_index": s.get("scene_index"),
                    "source_type": s.get("source_type"),
                    "visual_spec": s.get("visual_spec", {}),
                    "narrative_signals": s.get("narrative_signals", []),
                    "entity_appearances": s.get("entity_appearances", []),
                    "production_spec": {
                        "aspect_ratio": s.get("production_spec", {}).get("aspect_ratio", "16:9"),
                        "duration_seconds": s.get("production_spec", {}).get("duration_seconds", 8),
                        "primary_tier": s.get("production_spec", {}).get(
                            "provider_routing", {}
                        ).get("primary_tier", 2),
                    },
                    "presentation_plan": s.get("presentation_plan", {}),
                    "diversity_assignment": s.get("diversity_assignment", {}),
                }
                for s in batch
            ],
        }
        log.info(
            "vpd_stage_b_batch_started",
            batch=batch_idx,
            scenes_in_batch=len(batch),
            scene_cursor=scene_cursor,
            batch_size=_b_batch_size,
            max_tokens=_VPD_PROMPT_BATCH_MAX_TOKENS,
        )

        try:
            prompts_result = run_agent(
                agent_name=_AGENT_NAME,
                model_slug=_CLAUDE_SONNET,
                system_prompt=SCENE_PROMPTS_PROMPT,
                input_package=batch_input,
                output_schema="vpd_scene_prompts_batch",
                episode_id=episode_id,
                max_tokens=_VPD_PROMPT_BATCH_MAX_TOKENS,
                config=config,
                output_dir=None,
            )
        except TruncatedResponse as exc:
            if _b_batch_size <= 1:
                log.error(
                    "vpd_stage_b_batch_overflow_unsplittable",
                    batch=batch_idx,
                    completion_tokens=exc.completion_tokens,
                    max_tokens=exc.max_tokens_set,
                    scenes_in_batch=len(batch),
                )
                raise
            new_size = max(1, _b_batch_size // 2)
            log.warn(
                "vpd_stage_b_batch_overflow",
                batch=batch_idx,
                completion_tokens=exc.completion_tokens,
                max_tokens=exc.max_tokens_set,
                old_batch_size=_b_batch_size,
                new_batch_size=new_size,
                scene_cursor=scene_cursor,
            )
            _b_batch_size = new_size
            continue  # retry same scene_cursor with smaller batch

        batch_path.write_text(
            json.dumps(prompts_result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        n_written = len(prompts_result.get("scene_prompts", []))
        _merge_scene_prompts(visual_package, prompts_result)
        log.info(
            "vpd_stage_b_batch_completed",
            batch=batch_idx,
            prompts_merged=n_written,
        )
        scene_cursor += len(batch)
        batch_idx += 1

    # ------------------------------------------------------------------
    # Visual Intent QA — Pre-generation prompt review
    # Verifies every prompt satisfies its presentation_plan before
    # spending generation credits. Scenes that fail are retried
    # with rejection_reason injected into Stage B.
    # ------------------------------------------------------------------
    _run_visual_intent_qa(
        visual_package=visual_package,
        episode_id=episode_id,
        config=config,
        output_dir=_resolve_output_dir,
        log=log,
    )

    # ------------------------------------------------------------------
    # Stage C — Package assembly and final validation
    # ------------------------------------------------------------------
    scenes_planned = len(visual_package.get("scenes", []))
    entities_count = len(
        visual_package.get("visual_continuity_record", {}).get("locked_entities", [])
    )
    log.info(
        "vpd_stage_c_assembled",
        scenes_planned=scenes_planned,
        entities_count=entities_count,
    )
    log.info(
        "vpd_planning_completed",
        scenes_planned=scenes_planned,
        entities_count=entities_count,
    )

    # ------------------------------------------------------------------
    # BGA Pass 1 — brand compliance gate (before MUAPI spend)
    # ------------------------------------------------------------------
    if config.BGA_ENABLED:
        from hypogeum.agents.brand_guardian_agent import BGARejectedError, run_bga_pass1
        run_bga_pass1(visual_package, episode_id, config, _resolve_output_dir)

    # ------------------------------------------------------------------
    # Post-VPD finance gate — authoritative budget check from scene routing
    # Runs after Phase 1 planning (routing data now known) and before MUAPI spend.
    # ------------------------------------------------------------------
    if config.FINANCE_BUDGET_CEILING_USD > 0:
        from hypogeum.agents.finance_agent import (
            FinancePostVPDBlockedError,
            run_post_vpd_authorization,
        )
        post_vpd_finance = run_post_vpd_authorization(
            visual_package=visual_package,
            episode_id=episode_id,
            config=config,
            script_package=script_package,
            output_dir=_resolve_output_dir,
        )
        log.info(
            "vpd_post_vpd_finance_gate",
            verdict=post_vpd_finance.get("verdict"),
            estimated_spend_usd=post_vpd_finance.get("estimated_spend_usd"),
            scene_count=post_vpd_finance.get("cost_breakdown", {}).get("scene_count"),
        )
        if post_vpd_finance.get("verdict") == "BLOCKED":
            # Check for an operator-granted single-use approval before blocking.
            _approved = _check_finance_approval(episode_id, post_vpd_finance, log)
            if not _approved:
                raise FinancePostVPDBlockedError(
                    episode_id=episode_id, record=post_vpd_finance
                )

    # ------------------------------------------------------------------
    # Phase 2 — MUAPI execution
    # ------------------------------------------------------------------
    log.info(
        "vpd_muapi_phase_started",
        scenes_total=scenes_planned,
    )

    # M5.6 — Create budget guard for this run
    budget_guard = MUAPIBudgetGuard(
        episode_budget_credits=config.MUAPI_EPISODE_BUDGET_CREDITS,
        scene_budget_credits=config.MUAPI_SCENE_BUDGET_CREDITS,
        max_paid_attempts=config.MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE,
        log=log,
    )

    visual_package = _execute_muapi_phase(
        visual_package, config, episode_id, _resolve_output_dir, log,
        budget_guard=budget_guard,
    )

    log.info(
        "vpd_muapi_phase_completed",
        scenes_total=scenes_planned,
        estimated_credits=budget_guard.estimated_spend,
        actual_credits=budget_guard.actual_spend,
    )

    # ------------------------------------------------------------------
    # Write final package to disk (overwrite the planned version)
    # ------------------------------------------------------------------
    _write_final_package(visual_package, episode_id, _resolve_output_dir, log)

    # ------------------------------------------------------------------
    # M2.4 — Extract and write Thumbnail Handoff Brief
    # ------------------------------------------------------------------
    build_thumbnail_handoff_brief(visual_package, episode_id, _resolve_output_dir, log)

    return visual_package


# ---------------------------------------------------------------------------
# Stage A2 helper — flatten beats for batching
# ---------------------------------------------------------------------------

def _flatten_beats_for_a2(input_package: dict) -> list:
    """
    Flatten all narration beats from the compacted script_package into a list for A2 batching.

    Respects generation_constraints.shorts_count = 0 (skip shorts) and
    max_main_scenes cap (truncate main episode beats).
    """
    sp = input_package.get("script_package", {})
    constraints = input_package.get("generation_constraints", {})
    suppress_shorts = constraints.get("shorts_count", -1) == 0
    max_main = constraints.get("max_main_scenes", 0)

    beats: list = []
    for sec in sp.get("sections", []):
        section_id       = sec.get("section_id", "")
        visual_directions = sec.get("visual_directions", "")
        for beat in sec.get("narration_beats", []):
            beats.append({
                "source_type":       "main_episode",
                "section_id":        section_id,
                "short_id":          None,
                "visual_directions": visual_directions,
                "beat_id":           beat.get("beat_id", ""),
                "beat_type":         beat.get("beat_type", ""),
                "text":              beat.get("text", ""),
            })

    if max_main > 0:
        beats = beats[:max_main]

    if not suppress_shorts:
        for sh in sp.get("shorts_scripts", []):
            short_id         = sh.get("short_id", "")
            visual_directions = sh.get("visual_directions", "")
            for beat in sh.get("narration_beats", []):
                beats.append({
                    "source_type":       "short",
                    "section_id":        f"sh_{short_id}",
                    "short_id":          short_id,
                    "visual_directions": visual_directions,
                    "beat_id":           beat.get("beat_id", ""),
                    "beat_type":         beat.get("beat_type", ""),
                    "text":              beat.get("text", ""),
                })

    return beats


# ---------------------------------------------------------------------------
# Stage A2 helpers — entity compaction and budget-aware batching
# ---------------------------------------------------------------------------

def _compact_entities_for_a2(locked_entities: list) -> list:
    """
    Return a minimal entity list for A2 scene batch input.

    Full locked_entities entries include long accuracy notes, distinctive_markers lists,
    and accuracy_basis text that are not needed by A2's scene planning task.
    This compact form reduces A2 input size by ~60% per entity while preserving
    the entity_id, type, name, lock_state, and a short visual summary for
    entity_appearances[] decisions.
    """
    result = []
    for ent in locked_entities:
        vs = ent.get("visual_spec", {})
        # Take the first sentence of the description only
        desc = vs.get("description", "")
        first_sentence = (desc.split(".")[0] + ".") if desc else ""
        result.append({
            "entity_id":       ent.get("entity_id", ""),
            "entity_type":     ent.get("entity_type", ""),
            "name":            ent.get("name", ""),
            "lock_state":      ent.get("lock_state", ""),
            "visual_summary":  first_sentence,
        })
    return result


def _estimate_a2_tokens_per_scene(n_entities: int) -> int:
    """
    Estimate output tokens required for one scene in an A2 call.

    Formula: base tokens + per-entity overhead, scaled by safety factor.
    n_entities: number of compact locked_entities passed to the A2 call.
    """
    raw = _A2_TOKENS_PER_SCENE_BASE + n_entities * _A2_TOKENS_PER_ENTITY
    return int(raw * _A2_SAFETY_FACTOR)


def _compute_a2_batches(beats: list, n_entities: int) -> list:
    """
    Split beats into budget-aware batches for Stage A2 calls.

    Each batch contains at most _SCENE_SKELETON_BATCH_SIZE beats. The size is
    further constrained by an estimated token budget: if the per-scene estimate
    would cause the batch to consume more than _A2_OUTPUT_BUDGET_RATIO of
    _VPD_SKELETON_SCENE_BATCH_MAX_TOKENS, the batch is capped earlier.

    Never returns an empty batch (minimum 1 beat per batch).
    Returns an empty list only when beats is empty.
    """
    if not beats:
        return []

    tokens_per_scene = _estimate_a2_tokens_per_scene(n_entities)
    output_budget    = int(_VPD_SKELETON_SCENE_BATCH_MAX_TOKENS * _A2_OUTPUT_BUDGET_RATIO)
    capacity         = max(1, output_budget // tokens_per_scene)
    batch_size       = min(_SCENE_SKELETON_BATCH_SIZE, capacity)

    batches = []
    for i in range(0, len(beats), batch_size):
        batches.append(beats[i : i + batch_size])
    return batches


# ---------------------------------------------------------------------------
# Stage A3 helper — merge A1 metadata + A2 scene batches
# ---------------------------------------------------------------------------

def _merge_a1_a2(meta_package: dict, all_scenes: list, episode_id: str) -> dict:
    """
    Merge Stage A1 metadata dict and Stage A2 scene lists into a full Visual Package skeleton.

    Patches scene counts in episode_metadata, package_integrity, and
    provider_routing_manifest based on the merged scene list.
    """
    visual_package = {k: v for k, v in meta_package.items() if k != "scenes"}
    visual_package["scenes"] = all_scenes

    main_scenes  = [s for s in all_scenes if s.get("source_type") == "main_episode"]
    short_scenes = [s for s in all_scenes if s.get("source_type") == "short"]
    total_duration = float(
        sum(s.get("production_spec", {}).get("duration_seconds", 8) for s in all_scenes)
    )

    em = visual_package.setdefault("episode_metadata", {})
    em["scene_count_main_episode"]      = len(main_scenes)
    em["scene_count_shorts"]            = len(short_scenes)
    em["total_planned_duration_seconds"] = total_duration

    # Build shorts_coverage (preserving insertion order per short_id)
    short_coverage: dict = {}
    short_order: list    = []
    for s in short_scenes:
        sid = s.get("short_id") or "unknown"
        if sid not in short_coverage:
            short_coverage[sid] = 0
            short_order.append(sid)
        short_coverage[sid] += 1

    thb = visual_package.get("thumbnail_handoff_brief", {})
    pi  = visual_package.setdefault("package_integrity", {})
    pi["scenes_total"]             = len(all_scenes)
    pi["scenes_main_episode"]      = len(main_scenes)
    pi["shorts_coverage"]          = [
        {"short_id": sid, "scene_count": short_coverage[sid]} for sid in short_order
    ]
    pi["thumbnail_candidates_count"] = len(thb.get("candidates", []))

    # Patch provider_routing_manifest with tier tallies from planned scenes
    tier_counts: dict = {"1": 0, "2": 0, "3": 0}
    for s in all_scenes:
        tier = str(s.get("production_spec", {}).get("provider_routing", {}).get("primary_tier", 2))
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
    prm = visual_package.setdefault("provider_routing_manifest", {})
    prm["scenes_by_provider"] = {"muapi": len(all_scenes)}
    prm["scenes_by_tier"]     = tier_counts

    return visual_package


# ---------------------------------------------------------------------------
# Stage B helper — merge cinematic prompts into skeleton
# ---------------------------------------------------------------------------

def _merge_scene_prompts(visual_package: dict, prompts_result: dict) -> None:
    """
    Merge primary_prompt and fallback_prompt from a vpd_scene_prompts_batch result
    into the corresponding scenes in visual_package (matched by scene_id).

    Mutates visual_package in-place. Unknown scene_ids in prompts_result are ignored.
    """
    prompt_by_id: dict = {
        sp["scene_id"]: sp
        for sp in prompts_result.get("scene_prompts", [])
        if isinstance(sp, dict) and sp.get("scene_id")
    }
    for scene in visual_package.get("scenes", []):
        sid = scene.get("scene_id")
        if sid not in prompt_by_id:
            continue
        sp = prompt_by_id[sid]
        routing = scene.setdefault("production_spec", {}).setdefault("provider_routing", {})
        routing["primary_prompt"]  = sp.get("primary_prompt", "")
        routing["fallback_prompt"] = sp.get("fallback_prompt", "")


# ---------------------------------------------------------------------------
# Visual Intent QA helper — pre-generation prompt verification
# ---------------------------------------------------------------------------

def _run_visual_intent_qa(
    visual_package: dict,
    episode_id: str,
    config: Config,
    output_dir: Path,
    log: object,
) -> None:
    """
    Run Visual Intent QA over all scenes that have a primary_prompt and a
    presentation_plan. Scenes that fail are retried via Stage B with the
    rejection_reason prepended to the scene context. Max 1 retry per scene.

    Mutates visual_package in-place if any scene prompts are rewritten on retry.

    Args:
        visual_package: Visual Package dict with scenes that have presentation_plan
                        and primary_prompt injected.
        episode_id:     Episode identifier.
        config:         Loaded Config instance.
        output_dir:     Directory for the QA checkpoint file.
        log:            Structured logger instance.
    """
    ckpt_path = output_dir / f"vpd_visual_intent_qa_{episode_id}.json"

    # Build list of scenes with both a primary_prompt and a presentation_plan
    scenes_for_qa = []
    for s in visual_package.get("scenes", []):
        primary_prompt = (
            s.get("production_spec", {}).get("provider_routing", {}).get("primary_prompt", "")
        )
        plan = s.get("presentation_plan", {})
        if primary_prompt and plan:
            scenes_for_qa.append({
                "scene_id": s.get("scene_id"),
                "primary_prompt": primary_prompt,
                "presentation_plan": plan,
            })

    if not scenes_for_qa:
        # No scenes ready for QA — skip silently (normal for bare skeleton runs)
        return

    log.info(
        "vpd_visual_intent_qa_started",
        episode_id=episode_id,
        scenes_for_qa=len(scenes_for_qa),
    )

    if ckpt_path.exists():
        qa_result = json.loads(ckpt_path.read_text(encoding="utf-8"))
    else:
        qa_input = {
            "task": "visual_intent_qa",
            "episode_metadata": visual_package.get("episode_metadata", {}),
            "scenes": scenes_for_qa,
        }
        qa_result = run_agent(
            agent_name=_AGENT_NAME,
            model_slug=_CLAUDE_SONNET,
            system_prompt=VISUAL_INTENT_QA_PROMPT,
            input_package=qa_input,
            output_schema="vpd_visual_intent_qa_batch",
            episode_id=episode_id,
            max_tokens=_VISUAL_INTENT_QA_MAX_TOKENS,
            config=config,
            output_dir=None,
        )
        ckpt_path.write_text(
            json.dumps(qa_result, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # Build a lookup of scene_id → QA result
    qa_map = {
        r["scene_id"]: r
        for r in qa_result.get("qa_results", [])
        if isinstance(r, dict) and r.get("scene_id")
    }

    # Build a lookup of scene_id → scene object for retry
    scene_map = {
        s.get("scene_id"): s for s in visual_package.get("scenes", [])
    }

    failed_scenes = [
        (sid, r)
        for sid, r in qa_map.items()
        if not r.get("qa_pass", True)
    ]

    pass_count = len(qa_map) - len(failed_scenes)
    log.info(
        "vpd_visual_intent_qa_completed",
        episode_id=episode_id,
        total=len(qa_map),
        passed=pass_count,
        failed=len(failed_scenes),
    )

    # Retry failed scenes (max 1 retry per scene)
    for sid, qa_result_item in failed_scenes:
        scene = scene_map.get(sid)
        if not scene:
            continue

        rejection_reason = qa_result_item.get("rejection_reason", "")
        log.warn(
            "vpd_visual_intent_qa_scene_rejected",
            episode_id=episode_id,
            scene_id=sid,
            rejection_reason=rejection_reason,
        )

        # Build a retry batch for this single scene
        primary_prompt = (
            scene.get("production_spec", {}).get("provider_routing", {}).get("primary_prompt", "")
        )
        retry_input = {
            "task": "write_scene_prompts",
            "episode_metadata": visual_package.get("episode_metadata", {}),
            "scenes": [
                {
                    "scene_id": sid,
                    "scene_index": scene.get("scene_index"),
                    "source_type": scene.get("source_type"),
                    "visual_spec": scene.get("visual_spec", {}),
                    "narrative_signals": scene.get("narrative_signals", []),
                    "entity_appearances": scene.get("entity_appearances", []),
                    "production_spec": {
                        "aspect_ratio": scene.get("production_spec", {}).get("aspect_ratio", "16:9"),
                        "duration_seconds": scene.get("production_spec", {}).get("duration_seconds", 8),
                        "primary_tier": scene.get("production_spec", {}).get(
                            "provider_routing", {}
                        ).get("primary_tier", 2),
                    },
                    "presentation_plan": scene.get("presentation_plan", {}),
                    "diversity_assignment": scene.get("diversity_assignment", {}),
                    "_qa_rejection_reason": (
                        f"PREVIOUS PROMPT FAILED QA. Rejection reason: {rejection_reason} "
                        f"Previous prompt: {primary_prompt[:300]}"
                    ),
                }
            ],
        }

        try:
            retry_result = run_agent(
                agent_name=_AGENT_NAME,
                model_slug=_CLAUDE_SONNET,
                system_prompt=SCENE_PROMPTS_PROMPT,
                input_package=retry_input,
                output_schema="vpd_scene_prompts_batch",
                episode_id=episode_id,
                max_tokens=_VPD_PROMPT_BATCH_MAX_TOKENS,
                config=config,
                output_dir=None,
            )
            # Inject the rewritten prompt back into the scene
            _merge_scene_prompts(visual_package, retry_result)
            log.info(
                "vpd_visual_intent_qa_scene_retry_succeeded",
                episode_id=episode_id,
                scene_id=sid,
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "vpd_visual_intent_qa_scene_retry_failed",
                episode_id=episode_id,
                scene_id=sid,
                error=str(exc),
            )


def run_visual_package_director_from_plan(
    visual_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Resume VPD from an existing validated plan — skip Phase 1 (Claude planning),
    run Phase 2 (MUAPI execution) only.

    Used when a previous run completed Phase 1 and wrote visual_package to disk
    but failed during Phase 2 (e.g., MUAPIQuotaExhausted). Caller loads the
    existing visual_package JSON and passes it here to avoid re-billing Claude.

    Args:
        visual_package: Validated visual_package dict loaded from disk.
        config:         Loaded Config instance.
        episode_id:     Episode identifier for log correlation and file naming.
        output_dir:     Optional output directory override.

    Returns:
        The final Visual Package dict with actual generation results.

    Raises:
        MUAPIAuthFailure      — HTTP 401 during MUAPI execution (all-stop).
        MUAPIQuotaExhausted   — HTTP 402 during MUAPI execution (all-stop).
        MUAPIBudgetExhausted  — Episode/scene budget limit reached (M5.6).
    """
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    scenes_planned = len(visual_package.get("scenes", []))
    log.info(
        "vpd_resume_from_plan",
        scenes_planned=scenes_planned,
        phase1_skipped=True,
        dry_run=config.MUAPI_DRY_RUN,
        cheap_mode=config.MUAPI_CHEAP_MODE,
        episode_budget_credits=config.MUAPI_EPISODE_BUDGET_CREDITS,
        episode_zero_max_scenes=config.EPISODE_ZERO_MAX_SCENES,
    )

    _resolve_output_dir = (
        output_dir
        if output_dir is not None
        else Path(__file__).resolve().parent.parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
    )

    # BGA Pass 1 — brand compliance gate on resume path (before MUAPI spend)
    if config.BGA_ENABLED:
        from hypogeum.agents.brand_guardian_agent import run_bga_pass1
        run_bga_pass1(visual_package, episode_id, config, _resolve_output_dir)

    # Post-VPD finance gate on resume path (script_package not available; MUAPI only)
    if config.FINANCE_BUDGET_CEILING_USD > 0:
        from hypogeum.agents.finance_agent import (
            FinancePostVPDBlockedError,
            run_post_vpd_authorization,
        )
        post_vpd_finance = run_post_vpd_authorization(
            visual_package=visual_package,
            episode_id=episode_id,
            config=config,
            script_package=None,
            output_dir=_resolve_output_dir,
        )
        log.info(
            "vpd_post_vpd_finance_gate",
            verdict=post_vpd_finance.get("verdict"),
            estimated_spend_usd=post_vpd_finance.get("estimated_spend_usd"),
            scene_count=post_vpd_finance.get("cost_breakdown", {}).get("scene_count"),
        )
        if post_vpd_finance.get("verdict") == "BLOCKED":
            raise FinancePostVPDBlockedError(
                episode_id=episode_id, record=post_vpd_finance
            )

    # M5.6 — Create budget guard for this run
    budget_guard = MUAPIBudgetGuard(
        episode_budget_credits=config.MUAPI_EPISODE_BUDGET_CREDITS,
        scene_budget_credits=config.MUAPI_SCENE_BUDGET_CREDITS,
        max_paid_attempts=config.MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE,
        log=log,
    )

    log.info("vpd_muapi_phase_started", scenes_total=scenes_planned)
    visual_package = _execute_muapi_phase(
        visual_package, config, episode_id, _resolve_output_dir, log,
        budget_guard=budget_guard,
    )
    log.info(
        "vpd_muapi_phase_completed",
        scenes_total=scenes_planned,
        estimated_credits=budget_guard.estimated_spend,
        actual_credits=budget_guard.actual_spend,
    )

    _write_final_package(visual_package, episode_id, _resolve_output_dir, log)
    build_thumbnail_handoff_brief(visual_package, episode_id, _resolve_output_dir, log)

    return visual_package


# ---------------------------------------------------------------------------
# Phase 2 — MUAPI execution
# ---------------------------------------------------------------------------

def _execute_muapi_phase(
    visual_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Path,
    log,
    budget_guard: Optional[MUAPIBudgetGuard] = None,
) -> dict:
    """
    Execute MUAPI generation for all reference images and scenes.

    Returns the updated visual_package dict.
    Raises MUAPIAuthFailure, MUAPIQuotaExhausted, or MUAPIBudgetExhausted on halt conditions.

    M5.6 guardrails applied here:
      - EPISODE_ZERO_MAX_SCENES cap: truncates scene list before generation begins
      - budget_guard: passed to each scene generation call
    """
    vcr = visual_package.get("visual_continuity_record", {})
    locked_entities = vcr.get("locked_entities", [])
    scenes = visual_package.get("scenes", [])

    # Step 1: Generate reference images for final-locked entities
    # entity_id → hosted_url (transient — not stored in package)
    entity_hosted_urls: dict[str, str] = generate_entity_reference_images(
        locked_entities, config, episode_id, output_dir, log,
        budget_guard=budget_guard,
    )

    # Update locked_entities[] with reference_image_asset_id from results
    apply_reference_image_asset_ids(locked_entities, entity_hosted_urls, episode_id)

    # Step 2: Generate each scene video
    scenes_sorted = order_and_filter_scenes(scenes, config, log)

    generate_all_scene_videos(
        visual_package, scenes_sorted, entity_hosted_urls,
        config, episode_id, output_dir, log,
        budget_guard=budget_guard,
    )

    # Step 3: Re-build provider_routing_manifest summary from actuals
    update_routing_manifest(visual_package)

    return visual_package


# ---------------------------------------------------------------------------
# Final package write
# ---------------------------------------------------------------------------

def _write_final_package(
    visual_package: dict,
    episode_id: str,
    output_dir: Path,
    log,
) -> None:
    """
    Re-validate and write the final (post-MUAPI) Visual Package to disk.

    Overwrites the planned version written by run_agent() in Phase 1.
    """
    try:
        validate("visual_package", visual_package)
    except SchemaError as exc:
        log.warn(
            "vpd_final_schema_warn",
            path=exc.path,
            reason=exc.reason,
        )
        # Log but do not raise — deliver what we have

    out_path = output_dir / f"visual_package_{episode_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(visual_package, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    assets = visual_package.get("asset_manifest", {}).get("assets", [])
    generated = sum(1 for a in assets if a.get("generation_status") in ("generated", "reused"))

    log.info(
        "vpd_package_finalized",
        output_path=str(out_path),
        scenes_total=len(visual_package.get("scenes", [])),
        scenes_generated=generated,
    )


# ---------------------------------------------------------------------------
# Finance approval check
# ---------------------------------------------------------------------------

def _check_finance_approval(
    episode_id: str,
    finance_record: dict,
    log,
) -> bool:
    """
    Check whether the operator has granted a single-use finance approval for
    this episode. If a valid (pending, non-expired) approval exists, consume it,
    log the event, and return True (run may proceed past the budget ceiling).

    Returns False when no valid approval exists (caller should raise
    FinancePostVPDBlockedError).

    Non-blocking: any DB error returns False so the gate behaves safely.
    """
    from datetime import datetime, timezone
    from hypogeum.db import (
        get_connection,
        get_valid_finance_approval,
        consume_finance_approval,
    )
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection(None)
        try:
            approval = get_valid_finance_approval(conn, episode_id, now)
            if approval is None:
                return False
            consume_finance_approval(conn, approval["approval_id"])
            conn.commit()
        finally:
            conn.close()

        log.info(
            "vpd_finance_gate_approved",
            episode_id=episode_id,
            approval_id=approval["approval_id"],
            approved_by=approval.get("approved_by", "operator"),
            estimated_spend_usd=finance_record.get("estimated_spend_usd"),
            ceiling_at_approval=approval.get("ceiling_at_approval"),
            overage_usd=approval.get("overage_usd"),
        )
        return True

    except Exception as exc:
        log.warn(
            "vpd_finance_approval_check_failed",
            episode_id=episode_id,
            error=str(exc)[:200],
        )
        return False  # safe default: block when approval check fails
