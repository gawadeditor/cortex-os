"""
hypogeum/agents/thumbnail_generation_agent.py — Thumbnail MUAPI generation (Priority 13 extraction)

Extracted from thumbnail_director.py Phase 2.

Thumbnail mode routing
----------------------
UNIFIED_INTEGRATED (unified_integrated) — candidate["composite_mode"] falsy AND
    typography_integration == "embedded". APPROVED PRIMARY PATH (Thumbnail DNA
    v1, 2026-07-13) for all concept-driven main-episode candidates.
    Routed to generate_unified_integrated_thumbnail() in thumbnail_compositor.
    One unified MUAPI call: topic concept + integrated typography + canonical
    Thumbnail DNA directive + canonical FULL brand directive, each exactly
    once. No PIL headline. Wordmark still applied via PIL (TIG-07).

PREMIUM  (multi_layer_composite)   — candidate["composite_mode"] = True AND layer_prompts present.
    Routed to generate_and_composite() in thumbnail_compositor.
    4 focused MUAPI calls (base_scene, hero_subject, fx, typography) + PIL screen-blend composite.
    Layered path — preserved for rollback, no longer the default for
    concept-driven candidates (see thumbnail_prompt_writer._attach_concept_plans).
    Also the safety net when _attach_concept_plans() sees an unrecognized
    emotional_trigger (thumbnail_composition_planner._FALLBACK carries
    typography_integration="composited", which sets composite_mode=True).

FAST     (single_pass_integrated)  — candidate["composite_mode"] falsy, typography_integration=embedded,
    reached only for legacy (non-concept-driven) candidates whose image_prompt
    was authored directly by the LLM rather than by _attach_concept_plans().
    Routed to _generate_for_deliverable() → generate_thumbnail() with embedded typography prompt.
    Single MUAPI call.

LEGACY   (pil_composited_text)     — candidate["composite_mode"] falsy, typography_integration=composited.
    Routed to _generate_for_deliverable() → generate_thumbnail() + PIL text overlay.
    Fallback only — reached for legacy (non-concept-driven) candidates.

Owns:
- Iterating ranked candidates for each deliverable (main + shorts) in rank order
- Per-candidate retry loop: up to _MAX_ATTEMPTS_PER_CANDIDATE attempts per candidate
- Halt propagation: MUAPIAuthFailure, MUAPIQuotaExhausted, MUAPIBudgetExhausted
- Non-halt: all other exceptions — attempt logged as warn, next attempt or candidate tried
- Per-deliverable failure: all candidates exhausted → failed asset_id recorded, ok=False
- Logging: td_muapi_phase_started, td_thumbnail_generation_start,
           td_thumbnail_generated, td_thumbnail_attempt_failed, td_all_candidates_failed

Does NOT own:
- Claude planning (thumbnail_prompt_writer owns this)
- Final thumbnail selection or quality judgement (callers own this)
- thumbnail_package assembly or disk writes (thumbnail_director owns this)
- CTR / brand evaluation

MUAPI attribution: agent = "thumbnail_director" (preserved from pre-extraction
attribution — continuity with existing MUAPI audit logs).

Public API:
    generate_thumbnails(
        generation_plan, config, episode_id, output_dir, log,
        budget_guard=None,
    ) -> dict
    Returns: {"main_asset_id": str, "main_ok": bool, "short_results": list[tuple[str, str, bool]]}
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import hypogeum.asset_registry as _asset_registry
from hypogeum.agents.thumbnail_compositor import generate_and_composite, generate_unified_integrated_thumbnail
from hypogeum.config import Config
from hypogeum.muapi import (
    MUAPIAuthFailure,
    MUAPIBudgetExhausted,
    MUAPIBudgetGuard,
    MUAPIQuotaExhausted,
    generate_thumbnail,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "thumbnail_director"  # preserved for MUAPI audit log continuity
_MAX_ATTEMPTS_PER_CANDIDATE = 2


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_thumbnails(
    generation_plan: dict,
    config: Config,
    episode_id: str,
    output_dir: Path,
    log,
    budget_guard: Optional[MUAPIBudgetGuard] = None,
    channel_id: Optional[str] = None,
) -> dict:
    """
    Execute Phase 2 MUAPI thumbnail generation for all deliverables.

    Main episode: generates ALL ranked candidates (A/B/C for comparison), not
    just the first success. Each candidate is saved as candidate_<rank>.jpg so
    the dashboard can present all three for operator review and A/B testing.
    The rank-1 candidate is also copied to main.jpg for pipeline compatibility.

    Shorts: still uses first-success model (cost control).

    Args:
        generation_plan: Validated thumbnail_generation_plan dict.
        config:          Loaded Config instance.
        episode_id:      Episode identifier.
        output_dir:      Episode output directory.
        log:             Caller-provided logger.
        budget_guard:    Optional budget guardrail.
        channel_id:      Optional authoritative channel id. When supplied, the
                         verified persisted Production Plan's weekly
                         reservation is established/replayed BEFORE any image
                         generation (2026-08-26 plan-boundary rule); a denial
                         raises before any provider call.

    Returns:
        dict with keys:
            main_asset_id     — str asset_id of rank-1 candidate (or failed placeholder)
            main_ok           — bool True iff rank-1 succeeded
            main_candidates   — list of {rank, candidate_id, asset_id, ok, file_path}
            short_results     — list of (short_id, asset_id, ok) tuples

    Raises:
        MUAPIAuthFailure / MUAPIQuotaExhausted / MUAPIBudgetExhausted — halt immediately.
        hypogeum.plan_reservation.PlanReservationError and weekly-budget
        denials — plan-level reservation refused (before any spend).
    """
    log.info("td_muapi_phase_started")

    # Plan-level weekly reservation boundary (2026-08-26, hardened
    # 2026-08-27): once a persisted Production Plan file exists for this
    # episode, Phase-2 image generation is post-plan paid work governed by
    # the plan's one CHANNEL_WEEKLY reservation. channel_id is then
    # MANDATORY -- a missing/mismatched one fails closed here, before the
    # first image call. The ONE demonstrable zero-network exemption is a
    # MUAPI dry-run whose PRIMARY thumbnail provider is muapi (the dry-run
    # placeholder short-circuits before any fallback provider is tried);
    # a MUAPI dry-run with a non-MUAPI primary can still reach the network,
    # so it is NOT exempt.
    from hypogeum.production_plan import PlanLifecycle, classify_plan_lifecycle
    _thumb_zero_network = (
        getattr(config, "MUAPI_DRY_RUN", False)
        and getattr(config, "THUMBNAIL_IMAGE_PROVIDER", None) == "muapi"
    )
    if (
        not _thumb_zero_network
        and classify_plan_lifecycle(episode_id, output_dir) is not PlanLifecycle.PRE_PLAN
    ):
        from hypogeum.plan_reservation import (
            ensure_verified_plan_weekly_reservation,
        )
        ensure_verified_plan_weekly_reservation(
            channel_id=channel_id,
            episode_id=episode_id,
            config=config,
            log=log,
            output_dir=output_dir,
        )

    ranked_candidates = generation_plan["main_episode"]["ranked_candidates"]
    sorted_main = sorted(ranked_candidates, key=lambda c: c.get("rank", 999))

    main_candidates: list[dict] = []
    main_asset_id = f"episodes/{episode_id}/thumbnails/main"  # fallback placeholder
    main_ok = False

    for candidate in sorted_main:
        rank = candidate.get("rank", 999)
        candidate_id = candidate.get("candidate_id", f"cand_{rank}")
        deliverable_id = f"candidate_{rank}"

        # Composite mode: multi-layer generation + PIL compositing
        if candidate.get("composite_mode") and candidate.get("layer_prompts"):
            log.info("td_composite_mode_started", deliverable_id=deliverable_id)
            ok = generate_and_composite(
                candidate=candidate,
                config=config,
                episode_id=episode_id,
                output_dir=output_dir,
                deliverable_id=deliverable_id,
                log=log,
            )
            thumb_dir = output_dir / "thumbnails"
            cand_path = thumb_dir / f"candidate_{rank}.jpg"
            asset_id  = (
                f"episodes/{episode_id}/thumbnails/{deliverable_id}"
                if ok else
                f"episodes/{episode_id}/thumbnails/{deliverable_id}_failed"
            )
            file_path = f"episodes/{episode_id}/thumbnails/candidate_{rank}.jpg" if ok else None
            main_candidates.append({
                "rank":              rank,
                "candidate_id":      candidate_id,
                "asset_id":          asset_id,
                "ok":                ok,
                "file_path":         file_path,
                "text_overlay":      candidate.get("text_overlay"),
                "image_prompt":      candidate.get("image_prompt"),
                "ctr_rationale":     candidate.get("ctr_rationale"),
                "composition_plan":  candidate.get("composition_plan"),
                "typography_plan":   candidate.get("typography_plan"),
                "composite_mode":    True,
            })
            if rank == 1:
                main_ok = ok
                main_asset_id = asset_id
                if ok and cand_path.exists():
                    import shutil
                    shutil.copy2(str(cand_path), str(thumb_dir / "main.jpg"))
            continue

        # Unified integrated mode — approved primary path (Thumbnail DNA v1,
        # 2026-07-13). One unified MUAPI call: topic concept + integrated
        # typography + canonical DNA directive + canonical FULL brand
        # directive, each exactly once. No layered base/hero/FX calls.
        if not candidate.get("composite_mode") and candidate.get("typography_integration") == "embedded":
            log.info("td_unified_integrated_mode_started", deliverable_id=deliverable_id)
            ok = generate_unified_integrated_thumbnail(
                candidate=candidate,
                config=config,
                episode_id=episode_id,
                output_dir=output_dir,
                deliverable_id=deliverable_id,
                log=log,
            )
            thumb_dir = output_dir / "thumbnails"
            cand_path = thumb_dir / f"candidate_{rank}.jpg"
            gen_path  = thumb_dir / f"{deliverable_id}.jpg"
            if ok and gen_path.exists() and gen_path != cand_path:
                gen_path.rename(cand_path)
            asset_id  = (
                f"episodes/{episode_id}/thumbnails/{deliverable_id}"
                if ok else
                f"episodes/{episode_id}/thumbnails/{deliverable_id}_failed"
            )
            file_path = f"episodes/{episode_id}/thumbnails/candidate_{rank}.jpg" if ok else None
            main_candidates.append({
                "rank":              rank,
                "candidate_id":      candidate_id,
                "asset_id":          asset_id,
                "ok":                ok,
                "file_path":         file_path,
                "text_overlay":      candidate.get("text_overlay"),
                "image_prompt":      candidate.get("image_prompt"),
                "ctr_rationale":     candidate.get("ctr_rationale"),
                "composition_plan":  candidate.get("composition_plan"),
                "typography_plan":   candidate.get("typography_plan"),
                "composite_mode":    False,
                "thumbnail_mode":    "unified_integrated",
            })
            if rank == 1:
                main_ok = ok
                main_asset_id = asset_id
                if ok and cand_path.exists():
                    import shutil
                    shutil.copy2(str(cand_path), str(thumb_dir / "main.jpg"))
            continue

        asset_id, ok = _generate_for_deliverable(
            deliverable_id=deliverable_id,
            ranked_candidates=[candidate],
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            log=log,
            budget_guard=budget_guard,
        )

        # If successful, the file was written to thumbnails/<deliverable_id>.jpg
        # Rename / copy to ensure consistent candidate_<rank>.jpg naming
        thumb_dir = output_dir / "thumbnails"
        gen_path  = thumb_dir / f"{deliverable_id}.jpg"
        cand_path = thumb_dir / f"candidate_{rank}.jpg"
        if ok and gen_path.exists() and gen_path != cand_path:
            gen_path.rename(cand_path)
            file_path = f"episodes/{episode_id}/thumbnails/candidate_{rank}.jpg"
        elif ok and cand_path.exists():
            file_path = f"episodes/{episode_id}/thumbnails/candidate_{rank}.jpg"
        else:
            file_path = None

        main_candidates.append({
            "rank":            rank,
            "candidate_id":    candidate_id,
            "asset_id":        asset_id,
            "ok":              ok,
            "file_path":       file_path,
            "text_overlay":    candidate.get("text_overlay"),
            "placement":       (candidate.get("text_overlay") or {}).get("placement"),
            "image_prompt":    candidate.get("image_prompt"),
            "ctr_rationale":   candidate.get("ctr_rationale"),
            "composition_plan": candidate.get("composition_plan"),
            "typography_plan":  candidate.get("typography_plan"),
        })

        # rank-1 becomes the primary (main.jpg) for pipeline compatibility
        if rank == 1:
            main_ok = ok
            main_asset_id = asset_id
            if ok and cand_path.exists():
                import shutil
                shutil.copy2(str(cand_path), str(thumb_dir / "main.jpg"))
                log.info("td_main_thumbnail_set", rank=rank, asset_id=asset_id)

    log.info(
        "td_all_candidates_generated",
        total=len(main_candidates),
        succeeded=sum(1 for c in main_candidates if c["ok"]),
    )

    short_results: list[tuple[str, str, bool]] = []
    for short_entry in generation_plan.get("shorts", []):
        short_id = short_entry["short_id"]
        asset_id, ok = _generate_for_deliverable(
            deliverable_id=short_id,
            ranked_candidates=short_entry["ranked_candidates"],
            config=config,
            episode_id=episode_id,
            output_dir=output_dir,
            log=log,
            budget_guard=budget_guard,
        )
        short_results.append((short_id, asset_id, ok))

    return {
        "main_asset_id":   main_asset_id,
        "main_ok":         main_ok,
        "main_candidates": main_candidates,
        "short_results":   short_results,
    }


# ---------------------------------------------------------------------------
# Internal: per-deliverable generation with ranked fallback
# ---------------------------------------------------------------------------

def _generate_for_deliverable(
    deliverable_id: str,
    ranked_candidates: list,
    config: Config,
    episode_id: str,
    output_dir: Path,
    log,
    budget_guard: Optional[MUAPIBudgetGuard] = None,
) -> tuple[str, bool]:
    """
    Attempt image generation for one thumbnail deliverable.

    Iterates through ranked_candidates in rank order. For each candidate,
    attempts generate_thumbnail() up to _MAX_ATTEMPTS_PER_CANDIDATE times.
    Returns (asset_id, True) on the first success.
    Returns (failed_asset_id, False) if all candidates are exhausted.

    Raises MUAPIAuthFailure / MUAPIQuotaExhausted / MUAPIBudgetExhausted
    immediately (halt conditions).
    """
    sorted_candidates = sorted(ranked_candidates, key=lambda c: c.get("rank", 999))
    version_index = 0

    for candidate in sorted_candidates:
        candidate_id          = candidate.get("candidate_id", "unknown")
        image_prompt          = candidate.get("image_prompt", "")
        aspect_ratio          = candidate.get("aspect_ratio", "16:9")
        text_overlay          = candidate.get("text_overlay")           # dict or None
        typography_plan       = candidate.get("typography_plan")        # dict or None
        typography_integration = candidate.get("typography_integration", "composited")

        for attempt in range(_MAX_ATTEMPTS_PER_CANDIDATE):
            log.info(
                "td_thumbnail_generation_start",
                deliverable_id=deliverable_id,
                candidate_id=candidate_id,
                attempt=attempt + 1,
            )
            attempted_at = _now_iso()
            asset_slot_id = _asset_registry.compute_asset_slot_id(
                episode_id, "thumbnail", "muapi", variant=deliverable_id,
            )
            version_id = _asset_registry.register_asset_attempt(
                asset_slot_id=asset_slot_id,
                asset_type="thumbnail",
                provider="muapi",
                attempt_number=attempt + 1,
                version_index=version_index,
                attempted_at=attempted_at,
            )
            try:
                result = generate_thumbnail(
                    deliverable_id=deliverable_id,
                    image_prompt=image_prompt,
                    aspect_ratio=aspect_ratio,
                    config=config,
                    episode_id=episode_id,
                    output_dir=output_dir,
                    agent=_AGENT_NAME,
                    budget_guard=budget_guard,
                    text_overlay=text_overlay,
                    typography_plan=typography_plan,
                    typography_integration=typography_integration,
                )
                log.info(
                    "td_thumbnail_generated",
                    deliverable_id=deliverable_id,
                    candidate_id=candidate_id,
                    asset_id=result.asset_id,
                )
                _asset_registry.register_asset_success(
                    asset_slot_id=asset_slot_id,
                    version_id=version_id,
                    asset_id=result.asset_id,
                    asset_type="thumbnail",
                    provider="muapi",
                    attempted_at=attempted_at,
                    reuse_eligible=True,
                )
                return result.asset_id, True

            except (MUAPIAuthFailure, MUAPIQuotaExhausted, MUAPIBudgetExhausted) as exc:
                _asset_registry.register_asset_failure(
                    asset_slot_id=asset_slot_id,
                    version_id=version_id,
                    asset_type="thumbnail",
                    provider="muapi",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    attempted_at=attempted_at,
                )
                raise  # halt conditions — propagate immediately

            except Exception as exc:
                log.warn(
                    "td_thumbnail_attempt_failed",
                    deliverable_id=deliverable_id,
                    candidate_id=candidate_id,
                    attempt=attempt + 1,
                    error=str(exc),
                )
                _asset_registry.register_asset_failure(
                    asset_slot_id=asset_slot_id,
                    version_id=version_id,
                    asset_type="thumbnail",
                    provider="muapi",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    attempted_at=attempted_at,
                )
                version_index += 1
                # Continue — try next attempt or next candidate

    # All candidates exhausted
    log.error(
        "td_all_candidates_failed",
        deliverable_id=deliverable_id,
        candidates_tried=len(sorted_candidates),
    )
    failed_asset_id = f"episodes/{episode_id}/thumbnails/{deliverable_id}_failed"
    return failed_asset_id, False
