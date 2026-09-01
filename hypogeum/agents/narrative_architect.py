"""
hypogeum/agents/narrative_architect.py — Narrative Architect agent (M1.4)

The Narrative Architect is the third real agent in the pipeline. It consumes the
validated Research Package and produces the episode's complete narrative architecture.

Execution sequence:
  1. Log na_narrative_started with research quality indicators
  2. Call run_agent() with Claude Opus → narrative_package
  3. Log na_narrative_completed with archetype and section count
  4. Return narrative_package

This is a single-call agent. All narrative construction decisions (archetype
selection, reveal placement, Shorts extraction, signal annotation) are delegated
to Claude Opus via the system prompt. The NA module enforces no governance rules
beyond what run_agent() validates through the schema.

Model: Claude Opus 4.6 (frozen — T0.1)
Max tokens: 8,192

Integration contracts (must not be violated):
  - All Claude calls go through run_agent() — never call_claude() directly
  - System prompt loaded from disk — never hardcoded inline
  - Output validated by run_agent() via schemas.py
  - No environment access — all settings via Config
  - All logging via get_logger() — never print() or stdlib logging

Logging events emitted:
  na_narrative_started     — info  — before run_agent() call
  na_narrative_completed   — info  — after narrative_package returned
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.agent import run_agent
from hypogeum.channel_creative_brief import channel_brief_for
from hypogeum.config import Config
from hypogeum.logger import get_logger
from hypogeum.prompt_loader import load_prompt


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CLAUDE_OPUS   = "anthropic/claude-opus-4.6"
_NA_MAX_TOKENS = 16_384
_AGENT_NAME    = "narrative_architect"

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILE = _PROMPTS_DIR / "narrative_architect.txt"


# ---------------------------------------------------------------------------
# System prompt — loaded once at import time
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    return load_prompt(_PROMPT_FILE)


SYSTEM_PROMPT: str = _load_system_prompt()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_narrative_architect(
    research_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path] = None,
    channel_id: Optional[str] = None,
) -> dict:
    """
    Execute the Narrative Architect agent end-to-end.

    Args:
        research_package: Validated research_package dict from the Research Director.
        config:           Loaded Config instance. Passed through to run_agent().
        episode_id:       Episode identifier for log correlation and file naming.
        output_dir:       Optional output directory override (passed to run_agent()).

    Returns:
        narrative_package — validated dict from run_agent(). Contains episode
        structure, narrative arc, reveal architecture, and exactly 3 Shorts.

    Raises:
        ResponseParseError — Claude returned non-dict JSON.
        SchemaError        — Claude output failed narrative_package schema validation.
        Any exception from run_agent() / hypogeum.openrouter that is not caught here.
    """
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    claims_count   = len(research_package.get("claims", []))
    quality        = research_package.get("research_run_summary", {}).get("overall_quality", "unknown")
    sources_count  = len(research_package.get("source_registry", []))

    log.info(
        "na_narrative_started",
        claims_count=claims_count,
        research_quality=quality,
        sources_count=sources_count,
    )

    _input_package: dict = {
        "task": "build_narrative",
        "research_package": research_package,
        "channel_brief": channel_brief_for(channel_id),
    }
    if (getattr(config, "SHORT_NATIVE_MODE", False) is True):
        _input_package["production_mode"] = "short_native"
        _mbw = int(float(getattr(config, "SHORT_NATIVE_MAX_BEAT_SECONDS", 8.5) or 8.5)
                   * float(getattr(config, "SHORT_NATIVE_WORDS_PER_SECOND", 2.9) or 2.9))
        _input_package["short_native_directive"] = {
            "format": "single vertical short, ~45-55 seconds total",
            "structure": "exactly ONE section: a hook that opens an "
                         "unresolved question or counter-intuitive fact, one "
                         "turn that deepens it, one payoff that resolves it",
            "sections": 1,
            "beats": (
                f"plan 5 to 7 short beats. Each beat is ONE 8-second video "
                f"clip; keep each beat's narration under ~{_mbw} words so it "
                f"fits a single clip."
            ),
            "shorts_selection": "empty — the short IS the whole deliverable",
            "chapters": "none",
            "tone": "front-load intrigue; every sentence must earn the next",
        }

    # Channel Learning: hook / pacing directives this channel accrued from its
    # own retention analytics (hypogeum.channel_learning). Advisory to the model.
    try:
        from hypogeum.channel_learning import learning_input_block  # noqa: PLC0415
        _input_package.update(learning_input_block(channel_id, "hook", "pacing"))
    except Exception:  # noqa: BLE001
        pass

    narrative_package = run_agent(
        agent_name=_AGENT_NAME,
        model_slug=_CLAUDE_OPUS,
        system_prompt=SYSTEM_PROMPT,
        input_package=_input_package,
        output_schema="narrative_package",
        episode_id=episode_id,
        max_tokens=_NA_MAX_TOKENS,
        config=config,
        output_dir=output_dir,
        channel_id=channel_id,
    )

    archetype      = narrative_package.get("episode_structure", {}).get("archetype", "unknown")
    sections_count = len(narrative_package.get("episode_structure", {}).get("sections", []))
    shorts_count   = len(narrative_package.get("shorts_selection", []))
    signals_count  = len(narrative_package.get("narrative_signals", []))

    log.info(
        "na_narrative_completed",
        archetype=archetype,
        sections_count=sections_count,
        shorts_count=shorts_count,
        signals_count=signals_count,
    )

    return narrative_package
