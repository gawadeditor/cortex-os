"""
hypogeum/agents/thumbnail_strategy.py — Thumbnail Strategy Skill (Phase 1)

Generates three marketing-concept CTR hypotheses for a given episode BEFORE
any visual package or scene planning occurs. This is the entry point for
concept-driven thumbnail generation — the alternative to the scene-candidate
system used by VPD → TD.

Phase 1: standalone skill, invoked manually via scripts/generate_thumbnail_strategy.py.
Phase 2 (future): wired into the pipeline between Script Engine and VPD.

Architecture position:
    topic_brief + script_package → [THIS SKILL] → thumbnail_concept_brief
    thumbnail_concept_brief → (future) VPD + TD

Public API:
    run_thumbnail_strategy(
        topic_brief, script_package, config, episode_id, output_dir
    ) -> dict

Output schema: thumbnail_concept_brief
Output file:   thumbnail_concept_brief_{episode_id}.json

Logging events:
    tss_started            — info  — before the model call
    tss_completed          — info  — after validation passes
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from hypogeum.agent import run_agent
from hypogeum.config import Config
from hypogeum.prompt_loader import load_prompt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "thumbnail_strategy"
_DEFAULT_MODEL = "anthropic/claude-sonnet-4.6"
_MAX_TOKENS = 1_500

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILE = _PROMPTS_DIR / "thumbnail_strategy.txt"


def _load_system_prompt() -> str:
    return load_prompt(_PROMPT_FILE)


SYSTEM_PROMPT: str = _load_system_prompt()


# ---------------------------------------------------------------------------
# Input assembly
# ---------------------------------------------------------------------------

def _build_strategy_brief(
    topic_brief: dict,
    script_package: dict,
) -> dict:
    """
    Distil topic_brief and script_package into a compact thumbnail_strategy_brief
    that gives the model the marketing-critical information without noise.
    """
    # Extract the Hook section narration beats (first 4 beats, ~150 words of strongest language)
    hook_beats: list[str] = []
    for section in script_package.get("sections", []):
        if section.get("title", "").lower() in ("hook", "opening", "intro"):
            for beat in section.get("narration_beats", [])[:4]:
                text = beat.get("text", "").strip()
                if text:
                    hook_beats.append(text)
            break

    hook_language = " ".join(hook_beats)[:600]  # cap at 600 chars; model doesn't need the full transcript

    # Audience profile derived from category + demand signal
    category = topic_brief.get("topic_category", "ancient_mystery")
    demand_signal = topic_brief.get("audience_demand_signal", "")
    audience_profile = f"Category: {category}. {demand_signal[:200]}" if demand_signal else f"Category: {category}."

    # Competitive differentiation (1 sentence condensed from differentiation_hypothesis)
    diff_hyp = topic_brief.get("differentiation_hypothesis", "")
    competitive_gap = diff_hyp[:250] if diff_hyp else ""

    return {
        "episode_id": topic_brief.get("topic_id") or topic_brief.get("episode_id", ""),
        "episode_title": topic_brief.get("episode_title", ""),
        "hook_language": hook_language,
        "central_mystery": topic_brief.get("mystery_anchor", ""),
        "viewer_entry_point": topic_brief.get("curiosity_hook", ""),
        "competitive_gap": competitive_gap,
        "audience_profile": audience_profile,
        "topic_category": category,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_thumbnail_strategy(
    topic_brief: dict,
    script_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Generate three marketing-concept CTR hypotheses for an episode.

    Reads topic_brief and script_package, assembles a compact strategy brief,
    and calls Claude to generate a thumbnail_concept_brief with exactly 3
    distinct concepts (one per emotional register).

    Args:
        topic_brief:    Validated topic_brief dict from the Executive Producer.
        script_package: Validated script_package dict from the Script Engine.
        config:         Loaded Config instance.
        episode_id:     Episode identifier.
        output_dir:     Output directory. Defaults to
                        .hypogeum-assets/episodes/{episode_id}/.

    Returns:
        Validated thumbnail_concept_brief dict.

    Raises:
        SchemaError  — model response failed validation.
        MUAPIError   — not raised (this skill makes no MUAPI calls).
    """
    _resolve_output_dir = output_dir or (
        Path(__file__).resolve().parent.parent.parent
        / ".hypogeum-assets" / "episodes" / episode_id
    )

    from hypogeum.logger import get_logger
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    strategy_brief = _build_strategy_brief(topic_brief, script_package)

    log.info(
        "tss_started",
        episode_id=episode_id,
        episode_title=strategy_brief.get("episode_title", ""),
        topic_category=strategy_brief.get("topic_category", ""),
    )

    model = getattr(config, "OPENROUTER_SONNET_MODEL_ID", _DEFAULT_MODEL) or _DEFAULT_MODEL

    concept_brief = run_agent(
        agent_name=_AGENT_NAME,
        model_slug=model,
        system_prompt=SYSTEM_PROMPT,
        input_package={
            "task": "generate_thumbnail_concepts",
            "thumbnail_strategy_brief": strategy_brief,
        },
        output_schema="thumbnail_concept_brief",
        episode_id=episode_id,
        max_tokens=_MAX_TOKENS,
        config=config,
        output_dir=_resolve_output_dir,
    )

    log.info(
        "tss_completed",
        episode_id=episode_id,
        concepts_count=len(concept_brief.get("concepts", [])),
        triggers=[c.get("emotional_trigger") for c in concept_brief.get("concepts", [])],
    )

    return concept_brief
