"""
hypogeum/agents/visual_package_director.py — Visual Package Director agent (M2.3/M2.4)

Two-phase execution + Thumbnail Handoff Brief extraction:

  Phase 1 — Claude planning:
    run_agent() sends the Script Package to Claude Sonnet 4.6. Claude returns the
    full Visual Package v1.3 JSON with generation_status = "planned" on all assets
    and reference_image_asset_id = null on all locked_entities.

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
Max tokens: 12,288 (Visual Package is large — many scenes with full prompts)

Logging events emitted:
  vpd_planning_started        — info  — before run_agent() call
  vpd_planning_completed      — info  — after run_agent() returns
  vpd_muapi_phase_started     — info  — before MUAPI execution loop
  vpd_ref_image_started       — info  — before each generate_reference_image()
  vpd_ref_image_completed     — info  — after each generate_reference_image()
  vpd_ref_image_failed        — warn  — generate_reference_image() raised
  vpd_scene_generation_start  — info  — before each submit_scene()
  vpd_scene_generated         — info  — scene successfully generated
  vpd_scene_failed            — warn  — scene generation_failed (pipeline continues)
  vpd_muapi_phase_completed   — info  — after all scenes processed
  vpd_package_finalized       — info  — final package written to disk
  vpd_handoff_brief_written   — info  — thumbnail handoff brief written to disk
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
from hypogeum.muapi import (
    MUAPIAuthFailure,
    MUAPIBudgetExhausted,
    MUAPIBudgetGuard,
    MUAPIQuotaExhausted,
)
from hypogeum.schemas import SchemaError, validate


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CLAUDE_SONNET   = "anthropic/claude-sonnet-4.6"
_VPD_MAX_TOKENS  = 32_768
_AGENT_NAME      = "visual_package_director"

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILE = _PROMPTS_DIR / "visual_package_director.txt"


# ---------------------------------------------------------------------------
# System prompt — loaded once at import time
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    return _PROMPT_FILE.read_text(encoding="utf-8")


SYSTEM_PROMPT: str = _load_system_prompt()


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

    input_package = build_scene_plan_input(script_package, config, episode_id)

    constraints = input_package.get("generation_constraints")
    if constraints:
        reason = "episode_zero" if is_episode_zero else "dedicated_shorts_disabled"
        log_kwargs = {k: v for k, v in constraints.items()}
        log_kwargs["reason"] = reason
        log.info("vpd_generation_constraints_injected", **log_kwargs)

    visual_package = run_agent(
        agent_name=_AGENT_NAME,
        model_slug=_CLAUDE_SONNET,
        system_prompt=SYSTEM_PROMPT,
        input_package=input_package,
        output_schema="visual_package",
        episode_id=episode_id,
        max_tokens=_VPD_MAX_TOKENS,
        config=config,
        output_dir=output_dir,
    )

    scenes_planned = len(visual_package.get("scenes", []))
    entities_count = len(
        visual_package.get("visual_continuity_record", {}).get("locked_entities", [])
    )

    log.info(
        "vpd_planning_completed",
        scenes_planned=scenes_planned,
        entities_count=entities_count,
    )

    _resolve_output_dir = (
        output_dir
        if output_dir is not None
        else Path(__file__).resolve().parent.parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
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


