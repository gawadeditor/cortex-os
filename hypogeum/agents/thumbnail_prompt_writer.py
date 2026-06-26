"""
hypogeum/agents/thumbnail_prompt_writer.py — Thumbnail prompt planning (Priority 12 extraction)

Extracted from thumbnail_director.py Phase 1.

Owns:
- Sending the Thumbnail Handoff Brief to Claude via run_agent()
- Injecting thumbnail_concept_brief into the input package when present
- Logging td_planning_started before the call and td_planning_completed after
- Attaching deterministic composition_plan + typography_plan to each
  main_episode candidate in concept_driven_mode (rank → concept mapping)
- Returning the validated thumbnail_generation_plan dict

Concept-driven mode:
    When concept_brief is provided, it is included in the input package under
    "thumbnail_concept_brief". The TD system prompt (SECTION 11C) instructs
    Claude to build image_prompts from the concept fields instead of from
    VPD candidates. Concept A → rank 1, B → rank 2, C → rank 3.

    After Claude returns the plan, composition_plan and typography_plan are
    attached deterministically to each main_episode candidate using the
    thumbnail_composition_planner and thumbnail_typography_planner modules.
    This is authoritative — Claude's own plan fields (if any) are overwritten.

Model: config.TD_PLANNING_MODEL (default: openai/gpt-5.5-pro)
Max tokens: config.TD_PLANNING_MAX_TOKENS (default: 8192)

Public API:
    write_thumbnail_prompts(
        handoff_brief, config, episode_id, output_dir, log,
        concept_brief=None,
    ) -> dict
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.agent import run_agent
from hypogeum.agents.thumbnail_composition_planner import plan_composition
from hypogeum.agents.thumbnail_compositor import build_layer_prompts
from hypogeum.agents.thumbnail_typography_planner import plan_typography
from hypogeum.config import Config
from hypogeum.prompt_loader import load_prompt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "thumbnail_director"

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILE = _PROMPTS_DIR / "thumbnail_director.txt"


def _load_system_prompt() -> str:
    return load_prompt(_PROMPT_FILE)


SYSTEM_PROMPT: str = _load_system_prompt()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_thumbnail_prompts(
    handoff_brief: dict,
    config: Config,
    episode_id: str,
    output_dir: Path,
    log,
    concept_brief: Optional[dict] = None,
) -> dict:
    """
    Send the Thumbnail Handoff Brief (and optionally a concept brief) to Claude
    and return the validated thumbnail_generation_plan.

    When concept_brief is provided, it is included in the input package under
    "thumbnail_concept_brief". The TD system prompt (SECTION 11C) instructs
    Claude to use the three marketing concepts as the primary creative source
    rather than VPD scene candidates. Concept A → rank 1, B → rank 2, C → rank 3.
    """
    candidates_count = len(handoff_brief.get("candidates", []))
    short_candidates_count = len(handoff_brief.get("short_candidates", []))
    concept_driven = concept_brief is not None and bool(concept_brief.get("concepts"))

    log.info(
        "td_planning_started",
        candidates_count=candidates_count,
        short_candidates_count=short_candidates_count,
        concept_driven=concept_driven,
    )

    input_package: dict = {
        "task": "plan_thumbnail_generation",
        "thumbnail_handoff_brief": handoff_brief,
    }
    if concept_driven:
        input_package["thumbnail_concept_brief"] = concept_brief

    generation_plan = run_agent(
        agent_name=_AGENT_NAME,
        model_slug=config.TD_PLANNING_MODEL,
        system_prompt=SYSTEM_PROMPT,
        input_package=input_package,
        output_schema="thumbnail_generation_plan",
        episode_id=episode_id,
        max_tokens=config.TD_PLANNING_MAX_TOKENS,
        config=config,
        output_dir=output_dir,
    )

    if concept_driven:
        _attach_concept_plans(generation_plan, concept_brief)

    log.info(
        "td_planning_completed",
        main_candidates_planned=len(
            generation_plan.get("main_episode", {}).get("ranked_candidates", [])
        ),
        shorts_count=len(generation_plan.get("shorts", [])),
        concept_driven=concept_driven,
    )

    return generation_plan


# ---------------------------------------------------------------------------
# Internal: concept plan attachment
# ---------------------------------------------------------------------------

def _attach_concept_plans(generation_plan: dict, concept_brief: dict) -> None:
    """
    Attach deterministic composition_plan, typography_plan, layer_prompts, and
    composite_mode=True to every main_episode ranked_candidate.

    composite_mode is always True for premium episodes regardless of
    typography_integration. The compositor renders typography via PIL
    (_render_typography_pil), not via MUAPI, so the typography_integration
    field on composition_plan no longer controls routing.

    Mapping: concepts[0] → rank 1, concepts[1] → rank 2, concepts[2] → rank 3.
    Mutates generation_plan in place.
    """
    concepts = concept_brief.get("concepts", [])
    candidates = generation_plan.get("main_episode", {}).get("ranked_candidates", [])

    concept_by_rank: dict[int, dict] = {i + 1: c for i, c in enumerate(concepts)}

    for candidate in candidates:
        rank = candidate.get("rank", 0)
        concept = concept_by_rank.get(rank)
        if concept is None:
            continue
        trigger   = concept.get("emotional_trigger", "")
        hook_text = concept.get("suggested_hook_text", "")
        comp_plan = plan_composition(trigger)
        typo_plan = plan_typography(hook_text, trigger, comp_plan)
        candidate["composition_plan"]      = comp_plan
        candidate["typography_plan"]       = typo_plan
        candidate["typography_integration"] = comp_plan.get("typography_integration", "composited")
        # Always use the multi-layer compositor. Typography is PIL-rendered.
        candidate["layer_prompts"]  = build_layer_prompts(concept, comp_plan)
        candidate["composite_mode"] = True


def _embed_typography_in_prompt(base_prompt: str, typography_plan: dict) -> str:
    """
    Append an integrated typography instruction block to a base image_prompt.

    The instruction tells the image model to render the hook text INSIDE the
    generated image — not as a subtitle or overlay, but as physically integrated
    text that receives the scene's lighting, shadows, and depth.

    The PIL compositor will skip typography rendering when typography_integration
    is "embedded", trusting the image model to have rendered it correctly.
    """
    headline      = typography_plan.get("headline", "").upper()
    emphasis      = typography_plan.get("emphasis", "").upper()
    zone          = typography_plan.get("zone", "lower_left")
    emph_scale    = typography_plan.get("emphasis_scale", 1.7)

    zone_desc = {
        "lower_left":  "bottom-left quadrant of the frame",
        "lower_right": "bottom-right quadrant of the frame",
        "lower_center":"bottom-center of the frame",
        "center":      "center of the frame",
    }.get(zone, "bottom-left quadrant of the frame")

    # Describe the hierarchy: headline small above, emphasis huge below
    if headline and emphasis:
        text_block = (
            f'"{headline}" in smaller, lighter weight on one line, '
            f'with "{emphasis}" in enormous dominant bold letters directly below it'
        )
    elif emphasis:
        text_block = f'"{emphasis}" in enormous dominant bold letters'
    else:
        return base_prompt

    integration_block = (
        f" — INTEGRATED TYPOGRAPHY: in the {zone_desc}, large bold text reads: "
        f"{text_block}. "
        f"The {emphasis!r} word is dramatically larger than {headline!r} "
        f"(approximately {emph_scale:.0f}× taller). "
        f"The text is physically part of the image — it receives realistic shadows, "
        f"warm amber light from the scene's key light source, and depth that matches "
        f"the surrounding environment. "
        f"The text is NOT a subtitle, NOT an overlay, NOT in a box, NOT a lower-third. "
        f"It feels designed together with the image. "
        f"Text color: warm cream or aged parchment (#F5E3B9), matching the scene's warm palette. "
        f"Text must be fully legible and correctly spelled."
    )

    return base_prompt.rstrip(" ,;.") + integration_block
