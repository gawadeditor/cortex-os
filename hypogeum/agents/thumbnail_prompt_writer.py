"""
hypogeum/agents/thumbnail_prompt_writer.py — Thumbnail prompt planning (Priority 12 extraction)

Extracted from thumbnail_director.py Phase 1.

Owns:
- Sending the Thumbnail Handoff Brief to Claude via run_agent()
- Injecting thumbnail_concept_brief into the input package when present
- Logging td_planning_started before the call and td_planning_completed after
- Returning the validated thumbnail_generation_plan dict

Concept-driven mode:
    When concept_brief is provided, it is added to the input package under
    "thumbnail_concept_brief". The TD system prompt (SECTION 11C) instructs
    Claude to build image_prompts from the concept fields instead of from
    VPD candidates. Each concept maps to a fixed rank: A→1, B→2, C→3.

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
from hypogeum.config import Config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "thumbnail_director"

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILE = _PROMPTS_DIR / "thumbnail_director.txt"


def _load_system_prompt() -> str:
    return _PROMPT_FILE.read_text(encoding="utf-8")


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

    log.info(
        "td_planning_completed",
        main_candidates_planned=len(
            generation_plan.get("main_episode", {}).get("ranked_candidates", [])
        ),
        shorts_count=len(generation_plan.get("shorts", [])),
        concept_driven=concept_driven,
    )

    return generation_plan
