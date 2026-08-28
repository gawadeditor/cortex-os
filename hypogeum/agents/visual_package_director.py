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
    System prompt composes vpd_scene_prompts.txt (including a bridge section)
    with cinematic_prompt_architecture.md, the internal Veo and Seedance
    translation skills, and the pinned upstream Seedance2 and Veo Prompt
    Director skills (third_party/skills/seedance2/<PINNED_SEEDANCE2_SHA>/SKILL.md
    and third_party/skills/veo_prompt_director/<PINNED_VEO_PROMPT_DIRECTOR_VERSION>/SKILL.md,
    both loaded verbatim, never edited) — all via the existing
    compose_system_prompt() call, inside this same single run_agent()
    invocation. No second call, no new stage, no adapter. Provider routing
    and generation execution remain entirely downstream, unaffected
    (provider_router.py, MUAPI).

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
  vpd_stage_a4_started                   — info  — before Stage A4 batched loop begins
  vpd_stage_a4_batch_started             — info  — before each Stage A4 run_agent() call
  vpd_stage_a4_batch_resumed             — info  — Stage A4 batch checkpoint found, skipping
  vpd_stage_a4_batch_completed           — info  — Stage A4 batch plans merged
  vpd_stage_a4_batch_overflow            — warn  — TruncatedResponse; batch halved and retried
  vpd_stage_a4_batch_overflow_unsplittable — error — batch_size=1 still truncates; re-raised
  vpd_stage_a4_resumed                   — info  — final A4 checkpoint found, skipping all batches
  vpd_stage_a4_completed                 — info  — all A4 batches merged and written
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
from hypogeum.prompt_composition import SkillSpec, select_skills
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

# Pinned upstream Seedance2 skill (docs/architecture/UPSTREAM_SKILL_MANIFEST.md §3.3,
# docs/architecture/VIDEO_PROMPT_SKILLS_OWNERSHIP_AUDIT.md,
# config/upstream_skills_manifest.json) — loaded verbatim, never edited. Attached
# alongside, never in place of, the existing internal Veo/Seedance translation skills.
PINNED_SEEDANCE2_SHA = "e06c7c63a766d623004a2807881c30685ce517af"
_THIRD_PARTY_ROOT = Path(__file__).resolve().parent.parent.parent / "third_party"
_SEEDANCE2_SKILL_PATH = (
    _THIRD_PARTY_ROOT / "skills" / "seedance2" / PINNED_SEEDANCE2_SHA / "SKILL.md"
)

# Pinned upstream Veo Prompt Director skill (docs/architecture/UPSTREAM_SKILL_MANIFEST.md §3.4,
# docs/architecture/VIDEO_PROMPT_SKILLS_OWNERSHIP_AUDIT.md, config/upstream_skills_manifest.json)
# — loaded verbatim, never edited. Attached alongside, never in place of, the existing internal
# Veo/Seedance translation skills or the pinned Seedance2 skill above. Source is an authenticated
# MCP Market package download, not a git repository, so there is no commit SHA to pin — the
# package version ("1.0.0", from SKILL.md's own metadata.mcpmarket-version frontmatter field)
# is used as the installation identifier instead, exactly as it appears in the installed
# directory name. Only SKILL.md is composed into Stage B's prompt (matching Seedance2's
# single-file composition); the package's reference/ files are installed verbatim alongside it
# for provenance and are available for a future, separate composition if ever needed, but are
# not loaded at runtime by this task.
PINNED_VEO_PROMPT_DIRECTOR_VERSION = "1.0.0"
_VEO_PROMPT_DIRECTOR_SKILL_PATH = (
    _THIRD_PARTY_ROOT / "skills" / "veo_prompt_director"
    / PINNED_VEO_PROMPT_DIRECTOR_VERSION / "SKILL.md"
)

# All skill paths used by VPD — used for diagnostic logging
_VPD_ALL_SKILL_PATHS: list[Path] = [
    _CINEMATIC_SKILL_FILE,
    _VLE_SKILL_FILE,
    _VEO_TRANSLATION_SKILL_FILE,
    _SEEDANCE_TRANSLATION_SKILL_FILE,
    _SEEDANCE2_SKILL_PATH,
    _VEO_PROMPT_DIRECTOR_SKILL_PATH,
]

# Stage A4 — Scene Presentation Planner
_SCENE_PRESENTATION_PLANNER_PROMPT_FILE = _PROMPTS_DIR / "vpd_scene_presentation_planner.txt"
_VPD_PRESENTATION_PLAN_MAX_TOKENS = 8_192   # 15 scenes × ~200 tok/plan ≈ 3,000 tok; 8,192 has headroom
_PRESENTATION_PLAN_BATCH_SIZE = 15           # batched like Stage B — halved on TruncatedResponse

# Visual Intent QA prompt
_VISUAL_INTENT_QA_PROMPT_FILE = _PROMPTS_DIR / "vpd_visual_intent_qa.txt"
_VISUAL_INTENT_QA_MAX_TOKENS = 4_096   # 10 scenes × ~150 tok/result ≈ 1,500 tok; 4,096 has headroom
_VISUAL_INTENT_QA_BATCH_SIZE = 10      # batched: full prompts in input, short qa_results in output


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
        _SEEDANCE2_SKILL_PATH,
        _VEO_PROMPT_DIRECTOR_SKILL_PATH,
    )


# ---------------------------------------------------------------------------
# Stage B conditional skill attachment (Phase B1 Checkpoint 1, 2026-07-18)
#
# SCENE_PROMPTS_PROMPT (below) remains the full, unconditional 5-skill
# composition — kept as-is so existing callers/tests that depend on "the
# full stack" continue to see the same value. Actual Stage B run_agent()
# calls now use _compose_stage_b_prompt(mode), which narrows the two
# provider-translation skill pairs (Veo / Seedance) to only the family(s)
# actually present in that batch's tier routing. The Cinematic Prompt
# Architecture skill is not provider-specific and is always attached.
#
# mode is resolved per batch from Config's actual MUAPI_T{1,2,3}_MODEL
# values (never a hardcoded tier assumption), so it stays correct if tier
# routing configuration ever changes.
# ---------------------------------------------------------------------------

_TIER_MODEL_CONFIG_ATTRS = {
    1: "MUAPI_T1_MODEL",
    2: "MUAPI_T2_MODEL",
    3: "MUAPI_T3_MODEL",
}


def _tier_provider_family(tier: int, config: Config) -> str:
    """
    Map a tier number to a provider family ("seedance" or "veo") using
    Config's actual configured model name for that tier. Any model name
    that is not Seedance is treated as Veo family — accurate for all tiers
    today (T1/T2/T3 are all Veo-family per CLAUDE.md) and never narrows
    the skill stack incorrectly for a genuinely different provider (e.g.
    Runway), since neither translation skill is Runway-specific and today
    Runway batches receive both skill pairs unconditionally anyway.
    """
    attr = _TIER_MODEL_CONFIG_ATTRS.get(tier, "MUAPI_T2_MODEL")
    model = (getattr(config, attr, "") or "").lower()
    return "seedance" if "seedance" in model else "veo"


def _resolve_stage_b_mode(scenes: list[dict], config: Config) -> str:
    """
    Determine which provider family(s) are present across a Stage B batch,
    from each scene's primary_tier and fallback_tier. Returns one of
    "veo_only", "seedance_only", "mixed_tier", or "no_tier" (no resolvable
    tier info at all) for use as the select_skills() mode.
    """
    families: set[str] = set()
    for scene in scenes:
        routing = scene.get("production_spec", {}).get("provider_routing", {})
        for tier_key in ("primary_tier", "fallback_tier"):
            tier = routing.get(tier_key)
            if tier is None:
                continue
            families.add(_tier_provider_family(tier, config))
    if not families:
        return "no_tier"
    if families == {"veo"}:
        return "veo_only"
    if families == {"seedance"}:
        return "seedance_only"
    return "mixed_tier"


# Provider-specific Stage B skills only — Cinematic Prompt Architecture is
# always attached separately (not conditional, not provider-specific).
# "no_tier"/"mixed_tier" are included in every spec's modes so an
# unresolvable or mixed batch always gets the full stack (safe fallback),
# matching select_skills()'s own "never silently narrow when uncertain"
# contract.
_VPD_STAGE_B_PROVIDER_SKILL_SPECS: list[SkillSpec] = [
    SkillSpec(_VEO_TRANSLATION_SKILL_FILE, frozenset({"veo_only", "mixed_tier", "no_tier"})),
    SkillSpec(_SEEDANCE_TRANSLATION_SKILL_FILE, frozenset({"seedance_only", "mixed_tier", "no_tier"})),
    SkillSpec(_SEEDANCE2_SKILL_PATH, frozenset({"seedance_only", "mixed_tier", "no_tier"})),
    SkillSpec(_VEO_PROMPT_DIRECTOR_SKILL_PATH, frozenset({"veo_only", "mixed_tier", "no_tier"})),
]


def _compose_stage_b_prompt(mode: str) -> str:
    selection = select_skills(_VPD_STAGE_B_PROVIDER_SKILL_SPECS, mode)
    return compose_system_prompt(_SCENE_PROMPTS_PROMPT_FILE, _CINEMATIC_SKILL_FILE, *selection.paths)

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
    *,
    narration_package: Optional[dict] = None,
    channel_id: Optional[str] = None,
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
        script_package:    Validated script_package dict from the Script Engine.
        config:            Loaded Config instance.
        episode_id:        Episode identifier for log correlation and file naming.
        output_dir:        Optional output directory override (passed to run_agent()).
        narration_package: Validated narration_package dict from Audio Agent,
                            keyword-only and optional (defaults to {}). Audio
                            Agent now runs before Visual Package Director in
                            the canonical fresh workflow, so real, measured
                            per-beat narration durations are normally
                            available here — consumed by Cinematic Coverage
                            Planning in place of a pre-audio word-count
                            estimate; see _measured_durations_by_beat_id().
                            A caller that omits it (e.g. a resumed/legacy
                            path) falls back to Cinematic Director's own
                            per-beat estimate exactly as before.

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

        # Global scene_id uniqueness must be re-checked on this resume
        # branch too (2026-08-10) — it loads a persisted skeleton
        # checkpoint directly and skips Stage A2/A3 entirely, so a
        # checkpoint written by an older pipeline version (before this
        # guard existed) would otherwise reach Stage A4/Coverage Planning
        # unvalidated. See hypogeum.scene_identity for the fail-closed
        # contract.
        from hypogeum.scene_identity import (  # noqa: PLC0415
            SceneIdentityConflictError,
            enforce_global_scene_identity,
        )
        try:
            enforce_global_scene_identity(visual_package.get("scenes", []))
        except SceneIdentityConflictError as exc:
            log.error(
                "vpd_stage_a_resume_scene_identity_conflict",
                conflicts=[
                    {
                        "scene_id": c.scene_id,
                        "conflicting_beat_ids": c.conflicting_beat_ids,
                        "conflicting_deliverable_ids": c.conflicting_deliverable_ids,
                        "failure_code": c.failure_code,
                    }
                    for c in exc.result.conflicts
                ],
                path=str(skeleton_path),
                note="resumed skeleton checkpoint failed identity validation — fix the checkpoint before rerunning",
            )
            raise
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
        _renumber_short_scene_ids(all_scenes)
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

        # Global scene_id uniqueness (2026-08-10) — every downstream stage
        # (Cinematic Coverage Plan, Stage B prompts, per-shot allocation,
        # provider submission) trusts scene_id as an already-unique key;
        # nothing checked that assumption until now. Stage A2 batches are
        # planned independently (each batch call has no visibility into
        # what a different batch already named), so a collision across
        # two batches is structurally possible and must be caught here,
        # before Stage A4/Coverage Planning ever runs — not diagnosed
        # later from downstream symptoms. See hypogeum.scene_identity for
        # the fail-closed contract; A1/A2 checkpoints are preserved for
        # debugging exactly like the schema-validation failure above.
        from hypogeum.scene_identity import (  # noqa: PLC0415
            SceneIdentityConflictError,
            enforce_global_scene_identity,
        )
        try:
            enforce_global_scene_identity(visual_package.get("scenes", []))
        except SceneIdentityConflictError as exc:
            log.error(
                "vpd_stage_a3_scene_identity_conflict",
                conflicts=[
                    {
                        "scene_id": c.scene_id,
                        "conflicting_beat_ids": c.conflicting_beat_ids,
                        "conflicting_deliverable_ids": c.conflicting_deliverable_ids,
                        "failure_code": c.failure_code,
                    }
                    for c in exc.result.conflicts
                ],
                note="A1/A2 checkpoint files preserved — fix and rerun",
            )
            raise

        skeleton_path.write_text(
            json.dumps(visual_package, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info(
            "vpd_stage_a_completed",
            scenes=len(all_scenes),
            path=str(skeleton_path),
        )

    # ------------------------------------------------------------------
    # Stage A4 — Scene Presentation Planner (batched)
    # For every scene, determine HOW the narrative intent should be
    # communicated cinematically BEFORE prompts are written.
    # Batched like Stage B: _PRESENTATION_PLAN_BATCH_SIZE per call,
    # TruncatedResponse halves the batch and retries.
    # Final checkpoint: vpd_a4_presentation_{episode_id}.json
    # Per-batch checkpoints: vpd_a4_presentation_batch_{i}_{episode_id}.json
    # ------------------------------------------------------------------
    a4_final_path = _resolve_output_dir / f"vpd_a4_presentation_{episode_id}.json"
    if a4_final_path.exists():
        log.info("vpd_stage_a4_resumed", path=str(a4_final_path))
        all_presentation_plans = json.loads(
            a4_final_path.read_text(encoding="utf-8")
        ).get("presentation_plans", [])
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
        log.info(
            "vpd_stage_a4_started",
            scenes=len(scenes_for_a4),
            batch_size=_PRESENTATION_PLAN_BATCH_SIZE,
            max_tokens=_VPD_PRESENTATION_PLAN_MAX_TOKENS,
        )

        all_presentation_plans: list = []
        a4_cursor = 0
        a4_batch_idx = 0
        _a4_batch_size = _PRESENTATION_PLAN_BATCH_SIZE

        while a4_cursor < len(scenes_for_a4):
            a4_batch = scenes_for_a4[a4_cursor : a4_cursor + _a4_batch_size]
            if not a4_batch:
                break

            a4_batch_path = (
                _resolve_output_dir
                / f"vpd_a4_presentation_batch_{a4_batch_idx}_{episode_id}.json"
            )
            if a4_batch_path.exists():
                log.info(
                    "vpd_stage_a4_batch_resumed",
                    batch=a4_batch_idx,
                    path=str(a4_batch_path),
                )
                batch_result = json.loads(a4_batch_path.read_text(encoding="utf-8"))
                _a4_ckpt_plans = batch_result.get("presentation_plans", [])
                all_presentation_plans.extend(_a4_ckpt_plans)
                # Advance by actual count in checkpoint (may differ if batch_size changed)
                a4_cursor += len(_a4_ckpt_plans) if _a4_ckpt_plans else len(a4_batch)
                a4_batch_idx += 1
                continue

            a4_input = {
                "task": "plan_scene_presentations",
                "batch_index": a4_batch_idx,
                "episode_metadata": visual_package.get("episode_metadata", {}),
                "scenes": a4_batch,
            }
            log.info(
                "vpd_stage_a4_batch_started",
                batch=a4_batch_idx,
                scenes_in_batch=len(a4_batch),
                a4_cursor=a4_cursor,
                batch_size=_a4_batch_size,
                max_tokens=_VPD_PRESENTATION_PLAN_MAX_TOKENS,
            )
            try:
                batch_result = run_agent(
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
            except TruncatedResponse as exc:
                if _a4_batch_size <= 1:
                    log.error(
                        "vpd_stage_a4_batch_overflow_unsplittable",
                        batch=a4_batch_idx,
                        completion_tokens=exc.completion_tokens,
                        max_tokens=exc.max_tokens_set,
                        scenes_in_batch=len(a4_batch),
                    )
                    raise
                new_size = max(1, _a4_batch_size // 2)
                log.warn(
                    "vpd_stage_a4_batch_overflow",
                    batch=a4_batch_idx,
                    old_batch_size=_a4_batch_size,
                    new_batch_size=new_size,
                    a4_cursor=a4_cursor,
                )
                _a4_batch_size = new_size
                continue  # retry same cursor with smaller batch

            a4_batch_path.write_text(
                json.dumps(batch_result, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            batch_plans = batch_result.get("presentation_plans", [])
            all_presentation_plans.extend(batch_plans)
            log.info(
                "vpd_stage_a4_batch_completed",
                batch=a4_batch_idx,
                plans_in_batch=len(batch_plans),
                total_plans_so_far=len(all_presentation_plans),
            )
            a4_cursor += len(a4_batch)
            a4_batch_idx += 1

        # Merge all batches into final checkpoint
        presentation_plans_result = {"presentation_plans": all_presentation_plans}
        a4_final_path.write_text(
            json.dumps(presentation_plans_result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info(
            "vpd_stage_a4_completed",
            plans=len(all_presentation_plans),
            batches=a4_batch_idx,
            path=str(a4_final_path),
        )

    # Inject presentation_plan into each scene (keyed by scene_id)
    _presentation_plan_map = {
        p["scene_id"]: p
        for p in all_presentation_plans
    }
    for scene in visual_package.get("scenes", []):
        sid = scene.get("scene_id")
        if sid in _presentation_plan_map:
            scene["presentation_plan"] = _presentation_plan_map[sid]

    # ------------------------------------------------------------------
    # setting_type / lighting_selection — deterministic upstream groundwork
    # for the future Historical Authenticity DNA / Lighting DNA modules
    # (Phase B2.3). Must run after presentation planning (A4, needs
    # presentation_plan.presentation_method) and does not depend on Shot
    # Diversity Planner, so it runs here, before that stage. Sole writer —
    # see hypogeum.agents.setting_lighting_classifier module docstring.
    # No LLM call; deterministic code only.
    # ------------------------------------------------------------------
    from hypogeum.agents.setting_lighting_classifier import assign_setting_and_lighting  # noqa: PLC0415
    assign_setting_and_lighting(visual_package.get("scenes", []))

    # ------------------------------------------------------------------
    # Cinematic Director — plans an ordered, purposeful Cinematic Coverage
    # Plan (1-N shots, duration-aware) per beat. Must run after A4
    # (presentation_plan informs shot planning) and before Shot Diversity
    # Planner, which now assigns diversity per PLANNED SHOT rather than per
    # scene (2026-08-06 directing-rule fix — see hypogeum.agents.
    # cinematic_director module docstring for the full rationale).
    #
    # run_cinematic_director() is now a thin wrapper around the same
    # batched, deterministic, strict-schema planning implementation the
    # tools/regenerate_coverage_plan.py CLI uses -- it consumes
    # script_package/narration_package directly (real beat text and real
    # measured per-beat durations from Audio Agent, which now runs before
    # this stage) rather than pre-flattened dicts, and fails closed with
    # CinematicPlanningFailedError (propagated, uncaught, up through this
    # pipeline stage and out before Finance authorization) if every
    # bounded batched-planning attempt fails -- never a duration-blind
    # single-shot-per-beat fallback.
    # ------------------------------------------------------------------
    from hypogeum.agents.cinematic_director import run_cinematic_director  # noqa: PLC0415
    log.info(
        "vpd_cinematic_director_started",
        episode_id=episode_id,
        scenes=len(visual_package.get("scenes", [])),
    )
    coverage_plan = run_cinematic_director(
        script_package=script_package,
        narration_package=narration_package,
        visual_package=visual_package,
        episode_id=episode_id,
        config=config,
        output_dir=_resolve_output_dir,
        channel_id=channel_id,
    )

    # Planning-stage diversity gate (2026-08-07 refactor): reject the
    # Visual Package here, before Shot Diversity Planner, Stage B, or any
    # MUAPI/provider call runs, if the plan itself already contains two
    # compositionally equivalent shots in the same beat with no
    # diversity_justification — the exact planning failure cinematic_
    # director.txt's own "THE CENTRAL RULE" describes. See
    # hypogeum.agents.cinematic_director.validate_coverage_plan_diversity()
    # for the single source of truth this shares with clip_repair_agent's
    # own (secondary, defense-in-depth) fail-closed check.
    from hypogeum.agents.cinematic_director import validate_coverage_plan_diversity  # noqa: PLC0415
    validate_coverage_plan_diversity(coverage_plan, episode_id=episode_id)

    _coverage_plan_by_beat = {
        bp["beat_id"]: bp for bp in coverage_plan.get("beat_plans", [])
    }
    for scene in visual_package.get("scenes", []):
        beat_id = scene.get("narration_reference")
        if beat_id in _coverage_plan_by_beat:
            scene["cinematic_coverage_plan"] = _coverage_plan_by_beat[beat_id]
    log.info(
        "vpd_cinematic_director_completed",
        episode_id=episode_id,
        beat_plans=len(_coverage_plan_by_beat),
        total_shots=sum(len(bp.get("shots", [])) for bp in _coverage_plan_by_beat.values()),
    )

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

    # Same-scene consistency (2026-08-10): presentation_plan and
    # diversity_assignment were both injected above by dict-comprehensions
    # keyed on scene_id at injection time. Confirm every scene's
    # top-level scene_id (already proven globally unique) is still
    # echoed by both nested substructures now that both have run. See
    # hypogeum.scene_identity for the fail-closed contract.
    from hypogeum.scene_identity import (  # noqa: PLC0415
        SceneIdentityConflictError,
        enforce_scene_internal_consistency,
    )
    try:
        enforce_scene_internal_consistency(visual_package.get("scenes", []))
    except SceneIdentityConflictError as exc:
        log.error(
            "vpd_scene_internal_consistency_conflict",
            conflicts=[
                {
                    "scene_id": c.scene_id,
                    "conflicting_beat_ids": c.conflicting_beat_ids,
                    "failure_code": c.failure_code,
                }
                for c in exc.result.conflicts
            ],
        )
        raise

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
                    # setting_type / lighting_selection (Phase B2.3): data
                    # passthrough only. vpd_scene_prompts.txt is NOT updated
                    # this checkpoint to instruct compliance with these
                    # fields — that is deliberately deferred (no new
                    # Historical/Lighting DNA rules activated here; see
                    # docs/validation/prompt_writer_architecture/
                    # B1_3_ARCHITECTURE_CONTRACT.md).
                    "setting_type": s.get("setting_type", "unspecified"),
                    "lighting_selection": s.get("lighting_selection", "unspecified"),
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
                system_prompt=_compose_stage_b_prompt(_resolve_stage_b_mode(batch, config)),
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
    # Coverage Plan shot prompts (2026-08-07 refactor) — must run after
    # Stage B (each scene's own base primary_prompt/fallback_prompt is now
    # final) and before Visual Intent QA / Phase 2 MUAPI execution. Video
    # Prompt Writer renders every scene["cinematic_coverage_plan"]["shots"]
    # entry's own primary_prompt/fallback_prompt here, once — the single
    # place prompt text is ever constructed for a planned shot.
    # clip_repair_agent only selects from what is written here; it must
    # never render prompt text itself. Deterministic, no LLM call, safe to
    # run unconditionally on every VPD execution (resumed or not).
    # ------------------------------------------------------------------
    from hypogeum.video_prompt_writer import render_coverage_plan_shot_prompts  # noqa: PLC0415
    _shots_rendered = 0
    for scene in visual_package.get("scenes", []):
        plan = scene.get("cinematic_coverage_plan")
        if plan:
            render_coverage_plan_shot_prompts(scene)
            _shots_rendered += len(plan.get("shots", []))
    log.info(
        "vpd_coverage_plan_prompts_rendered",
        episode_id=episode_id,
        scenes_with_plan=sum(1 for s in visual_package.get("scenes", []) if s.get("cinematic_coverage_plan")),
        shots_rendered=_shots_rendered,
    )

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
    # Compiled Production Plan (2026-08-24 factory-level correction) —
    # the immutable, versioned, coverage-validated artifact that must
    # exist before Finance authorization and before any paid provider
    # call. Compiled from the already-batched, already-diverse Cinematic
    # Coverage Plan (coverage_plan, Commit A) — never a Hypogeum-specific
    # literal; the deliverable shape is derived from this episode's own
    # real script_package. Rejects the episode here, before Finance,
    # before any MUAPI call, if any beat cannot mathematically satisfy
    # coverage even counting every declared contingency asset — the exact
    # generic gap that let ep_af078f34 spend $48 before Assembly
    # discovered, after the fact, that its plan could never have
    # rendered. See hypogeum.production_plan for the full contract.
    # ------------------------------------------------------------------
    from hypogeum.production_plan import (  # noqa: PLC0415
        ProductionPlanCoverageError,
        compile_production_plan,
        persist_production_plan,
        validate_plan_coverage,
    )
    # channel_id is never substituted with a real-looking placeholder here:
    # "unknown" has no real Channel Package on disk, so a caller that omits
    # channel_id fails closed inside compile_production_plan() ->
    # load_deliverable_contract() with a clear ChannelPackagePolicyDriftError
    # rather than silently proceeding with an inferred or invented shape.
    production_plan = compile_production_plan(
        script_package=script_package,
        narration_package=narration_package or {},
        visual_package=visual_package,
        channel_id=channel_id or "unknown",
        episode_id=episode_id,
        config=config,
    )
    _plan_coverage = validate_plan_coverage(production_plan, narration_package or {})
    if not _plan_coverage.sufficient:
        raise ProductionPlanCoverageError(
            episode_id=episode_id, failing_beats=_plan_coverage.failing_beats,
        )
    persist_production_plan(production_plan, _resolve_output_dir)
    log.info(
        "vpd_production_plan_compiled",
        episode_id=episode_id,
        deliverable_ids=production_plan.deliverable_ids,
        asset_count=len(production_plan.assets),
        normal_expected_cost_usd=production_plan.normal_expected_cost_usd,
        maximum_bounded_cost_usd=production_plan.maximum_bounded_cost_usd,
        content_hash=production_plan.content_hash[:12],
    )

    # ------------------------------------------------------------------
    # Atomic PRE_PLAN -> plan-bound weekly-reservation upgrade (2026-08-28).
    # The Production Plan is now compiled + integrity-verified. Transition
    # the accepted e20bdcf weekly ledger from the pre-production hold to
    # the plan-bound CHANNEL_WEEKLY hold in ONE transaction BEFORE any
    # POST_PLAN paid boundary runs. Insufficient current-week capacity
    # fails closed here, before any POST_PLAN provider I/O. A run with no
    # channel_id (legacy) has no weekly ledger and no pre-production hold
    # to upgrade -- skipped.
    # ------------------------------------------------------------------
    if channel_id:
        from hypogeum.preplan_reservation import atomic_upgrade_to_plan_reservation  # noqa: PLC0415
        atomic_upgrade_to_plan_reservation(
            channel_id=channel_id, episode_id=episode_id, config=config, log=log,
        )

    # ------------------------------------------------------------------
    # Post-VPD finance gate — authoritative budget check from the compiled
    # Production Plan's own maximum bounded cost. Always runs, regardless
    # of whether a ceiling is configured (config.FINANCE_BUDGET_CEILING_USD
    # <= 0 is handled inside run_post_vpd_authorization() itself, which
    # still records a finance_record and returns AUTHORIZED) -- a caller
    # must never be able to reach MUAPI spend without at least attempting
    # Finance authorization, per the no-legacy-paid-bypass requirement
    # this correction enforces end to end.
    # ------------------------------------------------------------------
    from hypogeum.agents.finance_agent import (
        FinancePostVPDBlockedError,
        run_post_vpd_authorization,
    )
    post_vpd_finance = run_post_vpd_authorization(
        visual_package=visual_package,
        episode_id=episode_id,
        config=config,
        production_plan=production_plan,
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
        budget_guard=budget_guard, channel_id=channel_id,
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


def _flatten_beats_full_text_by_id(script_package: dict) -> dict:
    """
    Map every narration beat_id in the original, uncompacted script_package
    to its complete, untruncated text.

    Deliberately separate from _flatten_beats_for_a2(): that function reads
    the 20-word-capped script_package built for Stage A2's Claude prompt
    budget (see scene_planner.compact_beat()), which is the correct
    fidelity for that one consumer. Cinematic Director's word-count-based
    duration estimate (estimate_beat_duration_seconds()) needs the real
    text instead — sharing one flattening path between the two silently
    truncated every beat over 20 words down to the same estimated duration,
    collapsing compute_shot_budget() to a single shot regardless of the
    beat's true length. Keeping the two paths explicit and independent
    means a future change to either compaction policy or duration fidelity
    cannot silently leak into the other.
    """
    beat_texts: dict = {}
    for sec in script_package.get("sections", []):
        for beat in sec.get("narration_beats", []):
            beat_id = beat.get("beat_id", "")
            if beat_id:
                beat_texts[beat_id] = beat.get("text", "")
    for sh in script_package.get("shorts_scripts", []):
        for beat in sh.get("narration_beats", []):
            beat_id = beat.get("beat_id", "")
            if beat_id:
                beat_texts[beat_id] = beat.get("text", "")
    return beat_texts


def _measured_durations_by_beat_id(narration_package: dict) -> dict:
    """
    Map every beat_id to its real, measured narration duration from the
    validated narration_package (Audio Agent's output) -- the authoritative
    temporal planning input Cinematic Director's shot-budget arithmetic
    consumes, now that Audio Agent runs before Visual Package Director.

    Distinct from _flatten_beats_full_text_by_id(): that function supplies
    the semantic input (full narration text); this one supplies the
    temporal input (real measured seconds). The two are independent and
    both come from the original artifacts, never from a compacted
    intermediate.
    """
    durations: dict = {}
    for seg in narration_package.get("narration_segments", []):
        beat_id = seg.get("beat_reference", "")
        if beat_id:
            durations[beat_id] = seg.get("duration_seconds", 0.0)
    return durations


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

def _renumber_short_scene_ids(all_scenes: list) -> None:
    """
    Deterministically reassign the s{NN} suffix of every short scene's
    scene_id, sequentially per short_id, in all_scenes list order.

    Root cause (2026-08-23, first observed on ep_69df5bb0): each Stage A2
    batch call plans its own scene_id suffix for a short's beats with no
    visibility into what an earlier batch already assigned that same
    short_id -- start_scene_index gives the LLM a correct *global*
    position (reflected faithfully in scene_index, which is never wrong),
    but nothing tells it the correct *per-short* continuation point. Any
    short whose beats span more than one A2 batch (batch size ~7)
    therefore restarts at s01 in every batch it touches, deterministically
    colliding with the earlier batch's s01, s02, ... every time -- not an
    LLM sampling fluke, so blindly retrying the same cached A2 batch
    checkpoints reproduces the identical collision forever.

    Called once, after all Stage A2 batches are merged into all_scenes and
    before enforce_global_scene_identity() runs, so every downstream
    consumer only ever sees the corrected, already-unique scene_id.
    Main-episode scenes (short_id is None) are untouched -- their s{N}
    form already comes from the correct global running index and has
    never collided. scene_index itself is never touched here; it was
    already globally correct.
    """
    counters: dict = {}
    for s in all_scenes:
        short_id = s.get("short_id")
        if not short_id:
            continue
        counters[short_id] = counters.get(short_id, 0) + 1
        s["scene_id"] = f"sh_{short_id}_s{counters[short_id]:02d}"


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
        # Batched loop: full primary_prompts in input make each scene ~900 input tokens;
        # output is short (~150 tok/scene). 10 scenes/batch keeps input manageable.
        all_qa_results: list = []
        qa_cursor = 0
        qa_batch_idx = 0
        _qa_batch_size = _VISUAL_INTENT_QA_BATCH_SIZE

        while qa_cursor < len(scenes_for_qa):
            qa_batch = scenes_for_qa[qa_cursor : qa_cursor + _qa_batch_size]
            if not qa_batch:
                break

            qa_batch_path = (
                output_dir
                / f"vpd_visual_intent_qa_batch_{qa_batch_idx}_{episode_id}.json"
            )
            if qa_batch_path.exists():
                log.info(
                    "vpd_visual_intent_qa_batch_resumed",
                    batch=qa_batch_idx,
                    path=str(qa_batch_path),
                )
                batch_qa = json.loads(qa_batch_path.read_text(encoding="utf-8"))
                _qa_ckpt_results = batch_qa.get("qa_results", [])
                all_qa_results.extend(_qa_ckpt_results)
                # Advance by actual count in checkpoint (may differ if batch_size changed)
                qa_cursor += len(_qa_ckpt_results) if _qa_ckpt_results else len(qa_batch)
                qa_batch_idx += 1
                continue

            qa_input = {
                "task": "visual_intent_qa",
                "batch_index": qa_batch_idx,
                "episode_metadata": visual_package.get("episode_metadata", {}),
                "scenes": qa_batch,
            }
            log.info(
                "vpd_visual_intent_qa_batch_started",
                batch=qa_batch_idx,
                scenes_in_batch=len(qa_batch),
                qa_cursor=qa_cursor,
                batch_size=_qa_batch_size,
                max_tokens=_VISUAL_INTENT_QA_MAX_TOKENS,
            )
            try:
                batch_qa = run_agent(
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
            except TruncatedResponse as exc:
                if _qa_batch_size <= 1:
                    log.error(
                        "vpd_visual_intent_qa_batch_overflow_unsplittable",
                        batch=qa_batch_idx,
                        completion_tokens=exc.completion_tokens,
                        max_tokens=exc.max_tokens_set,
                    )
                    raise
                new_size = max(1, _qa_batch_size // 2)
                log.warn(
                    "vpd_visual_intent_qa_batch_overflow",
                    batch=qa_batch_idx,
                    old_batch_size=_qa_batch_size,
                    new_batch_size=new_size,
                )
                _qa_batch_size = new_size
                continue

            qa_batch_path.write_text(
                json.dumps(batch_qa, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            batch_results = batch_qa.get("qa_results", [])
            all_qa_results.extend(batch_results)
            log.info(
                "vpd_visual_intent_qa_batch_completed",
                batch=qa_batch_idx,
                results_in_batch=len(batch_results),
                total_qa_results_so_far=len(all_qa_results),
            )
            qa_cursor += len(qa_batch)
            qa_batch_idx += 1

        qa_result = {"qa_results": all_qa_results}
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
                    "setting_type": scene.get("setting_type", "unspecified"),
                    "lighting_selection": scene.get("lighting_selection", "unspecified"),
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
                system_prompt=_compose_stage_b_prompt(_resolve_stage_b_mode([scene], config)),
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
    channel_id: Optional[str] = None,
    *,
    script_package: Optional[dict] = None,
    narration_package: Optional[dict] = None,
) -> dict:
    """
    Resume VPD from an existing validated plan — skip Phase 1 (Claude planning),
    run Phase 2 (MUAPI execution) only.

    Used when a previous run completed Phase 1 and wrote visual_package to disk
    but failed during Phase 2 (e.g., MUAPIQuotaExhausted). Caller loads the
    existing visual_package JSON and passes it here to avoid re-billing Claude.

    Coverage-plan revalidation (2026-08-26): when script_package and
    narration_package are supplied, the episode's persisted Cinematic
    Coverage Plan is revalidated against the duration-aware shot budget
    (run_cinematic_director()'s own resume guard) and re-attached to every
    scene BEFORE any paid work, so long beats always have enough distinct
    planned shots and the Production Plan prices every contingency slot.
    If that changes the declared asset set, the verified persisted
    Production Plan is recompiled from the same real inputs and repersisted
    (superseding content hash) before the post-VPD finance gate reads it —
    a resume whose plan no longer describes its required assets must never
    authorize spend against the stale plan.

    Args:
        visual_package: Validated visual_package dict loaded from disk.
        config:         Loaded Config instance.
        episode_id:     Episode identifier for log correlation and file naming.
        output_dir:     Optional output directory override.
        script_package: Optional Script Package dict (enables coverage-plan
                        revalidation + Production Plan recompilation).
        narration_package: Optional Narration Package dict (same).

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

    # ------------------------------------------------------------------
    # Coverage-plan revalidation (2026-08-26) — before any paid work.
    # run_cinematic_director() itself decides whether the persisted plan
    # is budget-compliant; an underprovisioned (legacy) plan triggers its
    # own regeneration path. Either way the per-beat plans are re-attached
    # to every scene so clip_repair's shot-diversity fill and Production
    # Plan compilation see the same shot set. When the declared asset set
    # changes as a result, the verified persisted Production Plan is
    # recompiled from the real inputs and repersisted BEFORE the finance
    # gate below authorizes anything against it.
    # ------------------------------------------------------------------
    if script_package is not None and narration_package:
        from hypogeum.agents.cinematic_director import run_cinematic_director  # noqa: PLC0415

        # OWNERSHIP TOKEN for a possible supersede-release below (2026-08-27):
        # run_cinematic_director establishes the plan-scoped weekly
        # reservation itself (its regeneration Step 0) whenever this episode
        # is post-plan. We collect THAT call's WeeklyReservationOutcome via
        # the sink -- never a second reservation call here, and NO
        # MUAPI-specific dry-run condition: the OpenRouter regeneration is
        # real spend under MUAPI_DRY_RUN, so it reserves either way, and we
        # simply capture whatever it produced. If cinematic_director resumed
        # verbatim (no regeneration => no reservation), the sink stays empty
        # and a later supersede retains the old reservation conservatively.
        _weekly_sink: list = []

        coverage_plan = run_cinematic_director(
            script_package=script_package,
            narration_package=narration_package,
            visual_package=visual_package,
            episode_id=episode_id,
            config=config,
            output_dir=_resolve_output_dir,
            _weekly_reservation_sink=_weekly_sink,
            channel_id=channel_id,
        )
        _coverage_plan_by_beat = {
            bp["beat_id"]: bp for bp in coverage_plan.get("beat_plans", [])
        }
        for scene in visual_package.get("scenes", []):
            beat_id = scene.get("narration_reference")
            if beat_id in _coverage_plan_by_beat:
                scene["cinematic_coverage_plan"] = _coverage_plan_by_beat[beat_id]
        log.info(
            "vpd_resume_coverage_plan_reattached",
            episode_id=episode_id,
            beat_plans=len(_coverage_plan_by_beat),
            total_shots=sum(len(bp.get("shots", [])) for bp in _coverage_plan_by_beat.values()),
        )
        from hypogeum.production_plan import (  # noqa: PLC0415
            compile_production_plan,
            load_production_plan,
            persist_production_plan,
        )
        persisted_plan = load_production_plan(episode_id, _resolve_output_dir)
        if persisted_plan is not None and channel_id is not None:
            recompiled = compile_production_plan(
                script_package=script_package,
                narration_package=narration_package,
                visual_package=visual_package,
                channel_id=channel_id,
                episode_id=episode_id,
                config=config,
            )
            if recompiled.content_hash != persisted_plan.content_hash:
                persist_production_plan(recompiled, _resolve_output_dir, supersede=True)
                log.info(
                    "production_plan_recompiled_on_resume",
                    episode_id=episode_id,
                    old_content_hash=persisted_plan.content_hash[:12],
                    new_content_hash=recompiled.content_hash[:12],
                    old_maximum_bounded_cost_usd=persisted_plan.maximum_bounded_cost_usd,
                    new_maximum_bounded_cost_usd=recompiled.maximum_bounded_cost_usd,
                    reason="cinematic_coverage_plan_asset_set_changed",
                )
                if channel_id:
                    # One active plan reservation per channel-week: release
                    # the superseded plan's STILL-RESERVED row ONLY if THIS
                    # execution proved (via the reservation outcome
                    # run_cinematic_director's Step 0 produced -- collected in
                    # _weekly_sink) that it created that exact reservation.
                    # Otherwise it is retained conservatively -- the
                    # replacement's reservation then fails closed against the
                    # ceiling and the ledger requires reconciliation, rather
                    # than releasing a row another execution may own.
                    # Committed / unknown-acceptance rows are never touched.
                    from hypogeum.plan_reservation import (
                        release_superseded_plan_reservation,
                    )
                    _superseded_owner_token = (
                        _weekly_sink[0] if _weekly_sink else None
                    )
                    release_superseded_plan_reservation(
                        outcome=_superseded_owner_token,
                        superseded_content_hash=persisted_plan.content_hash,
                        channel_id=channel_id,
                        episode_id=episode_id,
                        log=log,
                    )

    # BGA Pass 1 — brand compliance gate on resume path (before MUAPI spend)
    if config.BGA_ENABLED:
        from hypogeum.agents.brand_guardian_agent import run_bga_pass1
        run_bga_pass1(visual_package, episode_id, config, _resolve_output_dir)

    # Post-VPD finance gate on resume path — reconciles the ALREADY
    # compiled, persisted Production Plan from this episode's Phase 1 run
    # (this function never compiles a fresh plan; a resume path has no
    # script_package to compile one from). A legacy/checkpoint episode
    # from before this correction, or any resume whose plan file is
    # missing for any other reason, fails closed with
    # ProductionPlanMissingError rather than silently reaching MUAPI
    # ungoverned by any plan.
    from hypogeum.agents.finance_agent import (
        FinancePostVPDBlockedError,
        run_post_vpd_authorization,
    )
    from hypogeum.production_plan import ProductionPlanMissingError, load_production_plan
    resumed_plan = load_production_plan(episode_id, _resolve_output_dir)
    if resumed_plan is None:
        raise ProductionPlanMissingError(episode_id=episode_id)
    post_vpd_finance = run_post_vpd_authorization(
        visual_package=visual_package,
        episode_id=episode_id,
        config=config,
        production_plan=resumed_plan,
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
        budget_guard=budget_guard, channel_id=channel_id,
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
    channel_id: Optional[str] = None,
) -> dict:
    """
    Execute MUAPI generation for all reference images and scenes.

    Returns the updated visual_package dict.
    Raises MUAPIAuthFailure, MUAPIQuotaExhausted, or MUAPIBudgetExhausted on halt conditions.

    M5.6 guardrails applied here:
      - EPISODE_ZERO_MAX_SCENES cap: truncates scene list before generation begins
      - budget_guard: passed to each scene generation call

    channel_id: forwarded to generate_all_scene_videos() -> generate_scene_video()
      -> muapi.submit_scene()'s own channel_id parameter. Every prior caller of
      this function omitted channel_id entirely, which meant every real
      production video-generation call reached submit_scene() with
      channel_id=None -- the exact condition under which submit_scene() falls
      through to its legacy, ungated _attempt_generation() path with no
      RunAuthorizationGrant, no GenerationExecutionEnvelope, and no
      submission_gate.submit() check at all (see submit_scene()'s own
      docstring/branch: "channel_id is not None" is what commits a caller to
      the enforced gate path; omitting it was previously indistinguishable
      from "not yet migrated"). This was a real, live gap, not a hypothetical
      one -- fixed 2026-08-24 as part of the Channel-Package-driven Production
      Plan / Finance correction.
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
        budget_guard=budget_guard, channel_id=channel_id,
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
