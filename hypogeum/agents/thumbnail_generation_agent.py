"""
hypogeum/agents/thumbnail_generation_agent.py — Thumbnail MUAPI generation (Priority 13 extraction)

Extracted from thumbnail_director.py Phase 2.

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
) -> dict:
    """
    Execute Phase 2 MUAPI thumbnail generation for all deliverables.

    Iterates ranked candidates for main episode and each Short. For each
    candidate, up to _MAX_ATTEMPTS_PER_CANDIDATE generation attempts are made.
    First success records the asset_id and moves to the next deliverable.
    All candidates exhausted → deliverable recorded as failed (ok=False).

    Args:
        generation_plan: Validated thumbnail_generation_plan dict from
                         thumbnail_prompt_writer.
        config:          Loaded Config instance.
        episode_id:      Episode identifier.
        output_dir:      Episode output directory.
        log:             Caller-provided logger.
        budget_guard:    Optional M5.6 budget guardrail.

    Returns:
        dict with keys:
            main_asset_id  — str asset_id (or failed placeholder)
            main_ok        — bool True iff main succeeded
            short_results  — list of (short_id, asset_id, ok) tuples

    Raises:
        MUAPIAuthFailure     — propagated immediately (halt).
        MUAPIQuotaExhausted  — propagated immediately (halt).
        MUAPIBudgetExhausted — propagated immediately (halt).
    """
    log.info("td_muapi_phase_started")

    main_asset_id, main_ok = _generate_for_deliverable(
        deliverable_id="main",
        ranked_candidates=generation_plan["main_episode"]["ranked_candidates"],
        config=config,
        episode_id=episode_id,
        output_dir=output_dir,
        log=log,
        budget_guard=budget_guard,
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
        "main_asset_id": main_asset_id,
        "main_ok": main_ok,
        "short_results": short_results,
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
        candidate_id = candidate.get("candidate_id", "unknown")
        image_prompt  = candidate.get("image_prompt", "")
        aspect_ratio  = candidate.get("aspect_ratio", "16:9")
        text_overlay  = candidate.get("text_overlay")  # dict or None

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
