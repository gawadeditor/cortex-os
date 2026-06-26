"""
hypogeum/agents/script_engine.py — Script Engine agent (M1.5)

The Script Engine is the fourth real agent in the pipeline. It consumes the
validated Narrative Package and produces the episode's complete script_package —
narration-ready beats, CTAs, chapter markers, Shorts scripts, text overlays,
and shorts extraction markers.

Execution sequence:
  1. Log se_script_started with narrative quality indicators
  2. Call run_agent() with Claude Sonnet → script_package
  3. Log se_script_completed with section count, CTA count, Shorts count
  4. Return script_package

This is a single-call agent. All script writing decisions (beat composition,
CTA placement, chapter title generation, Shorts narration) are delegated to
Claude Sonnet via the system prompt. The SE module enforces no governance rules
beyond what run_agent() validates through the schema.

Model: Claude Sonnet 4.6 (frozen — T0.1)
Max tokens: 8,192

Integration contracts (must not be violated):
  - All Claude calls go through run_agent() — never call_claude() directly
  - System prompt loaded from disk — never hardcoded inline
  - Output validated by run_agent() via schemas.py
  - No environment access — all settings via Config
  - All logging via get_logger() — never print() or stdlib logging

Logging events emitted:
  se_script_started     — info  — before run_agent() call
  se_script_completed   — info  — after script_package returned
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.agent import run_agent
from hypogeum.config import Config
from hypogeum.humanizer import humanize_beats
from hypogeum.logger import get_logger
from hypogeum.prompt_loader import load_prompt


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CLAUDE_SONNET  = "anthropic/claude-sonnet-4.6"
_SE_MAX_TOKENS  = 16_384
_AGENT_NAME     = "script_engine"

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILE = _PROMPTS_DIR / "script_engine.txt"


# ---------------------------------------------------------------------------
# System prompt — loaded once at import time
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    return load_prompt(_PROMPT_FILE)


SYSTEM_PROMPT: str = _load_system_prompt()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_script_engine(
    narrative_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Execute the Script Engine agent end-to-end.

    Args:
        narrative_package: Validated narrative_package dict from the Narrative Architect.
        config:            Loaded Config instance. Passed through to run_agent().
        episode_id:        Episode identifier for log correlation and file naming.
        output_dir:        Optional output directory override (passed to run_agent()).

    Returns:
        script_package — validated dict from run_agent(). Contains narration beats
        for all sections, exactly 3 Shorts scripts with text overlays, chapter markers,
        shorts extraction markers, traceability manifest, and script integrity audit.

    Raises:
        ResponseParseError — Claude returned non-dict JSON.
        SchemaError        — Claude output failed script_package schema validation.
        Any exception from run_agent() / hypogeum.openrouter that is not caught here.
    """
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    archetype     = narrative_package.get("episode_metadata", {}).get("archetype", "unknown")
    sections_count = len(
        narrative_package.get("episode_structure", {}).get("sections", [])
    )
    shorts_count  = len(narrative_package.get("shorts_selection", []))

    log.info(
        "se_script_started",
        archetype=archetype,
        sections_count=sections_count,
        shorts_count=shorts_count,
    )

    script_package = run_agent(
        agent_name=_AGENT_NAME,
        model_slug=_CLAUDE_SONNET,
        system_prompt=SYSTEM_PROMPT,
        input_package={
            "task": "write_script",
            "narrative_package": narrative_package,
        },
        output_schema="script_package",
        episode_id=episode_id,
        max_tokens=_SE_MAX_TOKENS,
        config=config,
        output_dir=output_dir,
    )

    # Humanize beat text fields (deterministic — no schema changes)
    for section in script_package.get("sections", []):
        humanize_beats(section.get("narration_beats", []))
    for short in script_package.get("shorts_scripts", []):
        humanize_beats(short.get("narration_beats", []))

    output_sections = len(script_package.get("sections", []))
    cta_count       = script_package.get("script_integrity", {}).get("cta_count", 0)
    output_shorts   = len(script_package.get("shorts_scripts", []))
    chapter_count   = len(script_package.get("chapter_markers", []))

    log.info(
        "se_script_completed",
        sections_count=output_sections,
        cta_count=cta_count,
        shorts_count=output_shorts,
        chapter_count=chapter_count,
    )

    return script_package
