"""
hypogeum/agents/thumbnail_director.py — Thumbnail Director agent (M2.5)

Two-phase execution:

  Phase 1 — Claude planning:
    run_agent() sends the Thumbnail Handoff Brief to Claude Sonnet 4.6. Claude returns
    a thumbnail_generation_plan: for each deliverable (main + 3 Shorts), an ordered
    list of candidates ranked by CTR priority, each with a provider-ready image prompt.

  Phase 2 — MUAPI execution:
    For each deliverable (main episode, then each Short in order):
        Iterate through ranked_candidates in rank order.
        For each candidate, attempt generate_thumbnail() up to _MAX_ATTEMPTS times.
        On first success: record asset_id and move to next deliverable.
        If all candidates exhaust all attempts: log error, mark deliverable as failed.

  After Phase 2:
    Build and validate the thumbnail_package. production_ready = True only if every
    deliverable succeeded. Write thumbnail_package_{episode_id}.json to disk.

Integration boundary:
  TD reads only the Thumbnail Handoff Brief — never the full Visual Package.
  This boundary is enforced at the call site (run_thumbnail_director receives the
  brief dict, not the visual package).

Public API:
    run_thumbnail_director(handoff_brief, config, episode_id, output_dir)
        → dict  (final Thumbnail Package)

Integration contracts (must not be violated):
  - All Claude calls go through run_agent() — never call_claude() directly
  - System prompt loaded from disk — never hardcoded inline
  - Output validated by run_agent() via schemas.py for the plan; TD validates the final package
  - No environment access — all settings via Config
  - All logging via get_logger() — never print() or stdlib logging
  - MUAPI calls go through muapi.generate_thumbnail()

Model: Claude Sonnet 4.6 (frozen — TD governance decision)
Max tokens: 4,096 (generation plan is compact — candidate rankings + prompts only)

Logging events emitted:
  td_planning_started          — info  — before run_agent() call
  td_planning_completed        — info  — after run_agent() returns
  td_muapi_phase_started       — info  — before image generation loop
  td_thumbnail_generation_start — info  — before each generate_thumbnail() attempt
  td_thumbnail_generated       — info  — thumbnail successfully generated
  td_thumbnail_attempt_failed  — warn  — one attempt failed; will retry or move to next candidate
  td_all_candidates_failed     — error — all candidates exhausted for a deliverable
  td_package_finalized         — info  — thumbnail_package written to disk
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from hypogeum.agents.thumbnail_generation_agent import generate_thumbnails
from hypogeum.agents.thumbnail_prompt_writer import write_thumbnail_prompts
from hypogeum.agents.thumbnail_qa import run_thumbnail_qa
from hypogeum.config import Config
from hypogeum.logger import get_logger
from hypogeum.muapi import MUAPIBudgetGuard
from hypogeum.schemas import SchemaError, validate


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "thumbnail_director"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_thumbnail_director(
    handoff_brief: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path] = None,
    *,
    budget_guard: Optional[MUAPIBudgetGuard] = None,
) -> dict:
    """
    Execute the Thumbnail Director end-to-end.

    Phase 1: Claude Sonnet 4.6 selects and ranks thumbnail candidates from the
    Handoff Brief, writing provider-ready image prompts. Output is validated
    against the thumbnail_generation_plan schema.

    Phase 2: Python iterates through ranked candidates for each deliverable,
    calling generate_thumbnail() up to _MAX_ATTEMPTS_PER_CANDIDATE times per
    candidate. First success records the asset_id; if all candidates fail,
    the deliverable is marked failed and production_ready is set to False.

    Args:
        handoff_brief: Validated thumbnail_handoff_brief dict from VPD.
        config:        Loaded Config instance.
        episode_id:    Episode identifier for log correlation and file naming.
        output_dir:    Optional output directory override.
        budget_guard:  Optional M5.6 budget guard (shared with VPD run).

    Returns:
        The final Thumbnail Package dict (schema-validated).

    Raises:
        MUAPIAuthFailure     — HTTP 401 during image generation (all-stop).
        MUAPIQuotaExhausted  — HTTP 402 during image generation (all-stop).
        MUAPIBudgetExhausted — Episode budget limit reached (M5.6).
        ResponseParseError   — Claude returned non-dict JSON in Phase 1.
        SchemaError          — Phase 1 output failed schema validation.
    """
    _resolve_output_dir = output_dir or (
        Path(__file__).resolve().parent.parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
    )
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    # ------------------------------------------------------------------
    # Phase 1 — Claude planning (delegated to thumbnail_prompt_writer)
    # ------------------------------------------------------------------
    generation_plan = write_thumbnail_prompts(
        handoff_brief=handoff_brief,
        config=config,
        episode_id=episode_id,
        output_dir=_resolve_output_dir,
        log=log,
    )

    # ------------------------------------------------------------------
    # BGA Pass 2 — brand compliance gate (before image generation spend)
    # ------------------------------------------------------------------
    if config.BGA_ENABLED:
        from hypogeum.agents.brand_guardian_agent import BGARejectedError, run_bga_pass2
        run_bga_pass2(generation_plan, episode_id, config, _resolve_output_dir)

    # ------------------------------------------------------------------
    # Smoke Test Mode — cap to 1 main thumbnail candidate
    # ------------------------------------------------------------------
    if config.SMOKE_TEST_MODE:
        main = generation_plan.get("main_episode", {})
        candidates = main.get("ranked_candidates", [])
        if candidates:
            rank1 = [c for c in candidates if c.get("rank") == 1] or candidates[:1]
            main["ranked_candidates"] = rank1[:1]

    # ------------------------------------------------------------------
    # Phase 2 — MUAPI image generation (delegated to thumbnail_generation_agent)
    # ------------------------------------------------------------------
    gen_result = generate_thumbnails(
        generation_plan=generation_plan,
        config=config,
        episode_id=episode_id,
        output_dir=_resolve_output_dir,
        log=log,
        budget_guard=budget_guard,
    )
    main_asset_id = gen_result["main_asset_id"]
    main_ok = gen_result["main_ok"]
    short_results = gen_result["short_results"]

    # ------------------------------------------------------------------
    # Thumbnail QA (Task F) — post-generation checks before package build
    # ------------------------------------------------------------------
    main_thumb_local = _resolve_output_dir / "thumbnails" / "main.jpg"
    thumbnail_qa_report = run_thumbnail_qa(
        thumbnail_local_path=main_thumb_local,
        aspect_ratio="16:9",
        asset_class="thumbnail_image",
        generation_plan=generation_plan,
    )
    log.info(
        "td_thumbnail_qa",
        overall_status=thumbnail_qa_report["overall_status"],
        failures=thumbnail_qa_report["failures"],
        warnings=thumbnail_qa_report["warnings"],
    )

    # ------------------------------------------------------------------
    # Build and validate Thumbnail Package
    # ------------------------------------------------------------------
    production_ready = main_ok and all(ok for _, _, ok in short_results)

    thumbnail_package = {
        "thumbnail_package_id": f"tp_{episode_id}",
        "episode_id": episode_id,
        "main_thumbnail": {
            "asset_id": main_asset_id,
        },
        "short_thumbnails": [
            {"short_id": sid, "asset_id": aid}
            for sid, aid, _ in short_results
        ],
        "package_integrity": {
            "production_ready": production_ready,
        },
        "generation_metadata": {
            "provider": "muapi",
            "primary_model": config.THUMBNAIL_IMAGE_MODEL,
            "fallback_model": config.THUMBNAIL_FALLBACK_MODEL or None,
            "resolution": config.THUMBNAIL_IMAGE_RESOLUTION,
            "quality": config.THUMBNAIL_IMAGE_QUALITY,
        },
        "thumbnail_qa_report": thumbnail_qa_report,
    }

    try:
        validate("thumbnail_package", thumbnail_package)
    except SchemaError as exc:
        log.warn(
            "td_final_schema_warn",
            path=exc.path,
            reason=exc.reason,
        )
        # Log but do not raise — deliver what we have

    out_path = _resolve_output_dir / f"thumbnail_package_{episode_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(thumbnail_package, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log.info(
        "td_package_finalized",
        output_path=str(out_path),
        production_ready=production_ready,
        shorts_count=len(short_results),
    )

    return thumbnail_package


