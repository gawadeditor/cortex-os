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

Humanizer skill: the system prompt composes hypogeum/prompts/script_engine.txt
(this agent's own instructions, including a HUMANIZATION section bridging the
two) with the pinned upstream Humanizer skill
(third_party/skills/humanizer/<PINNED_HUMANIZER_SHA>/SKILL.md), loaded
verbatim via hypogeum.prompt_loader.compose_system_prompt() — the same
base+skill composition pattern every other skill-attached agent already uses
(hypogeum/skill_registry.py). Humanization happens inside this single
generation call, not as a separate stage, adapter, or pipeline pass: no
second LLM call, no separate checkpoint layer, no merge step. The pinned
skill file is never edited, paraphrased, or copied into this prompt file —
it is composed alongside it, unmodified, exactly like every other agent's
skill injection.

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
from hypogeum.prompt_loader import compose_system_prompt


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CLAUDE_SONNET  = "anthropic/claude-sonnet-4.6"
_SE_MAX_TOKENS  = 16_384
_AGENT_NAME     = "script_engine"

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILE = _PROMPTS_DIR / "script_engine.txt"

# Pinned upstream Humanizer skill (docs/architecture/UPSTREAM_SKILL_MANIFEST.md,
# config/upstream_skills_manifest.json) — loaded verbatim, never edited.
PINNED_HUMANIZER_SHA = "1b48564898e999219882660237fde01bf4843a0f"
_THIRD_PARTY_ROOT = Path(__file__).resolve().parent.parent.parent / "third_party"
_HUMANIZER_SKILL_PATH = (
    _THIRD_PARTY_ROOT / "skills" / "humanizer" / PINNED_HUMANIZER_SHA / "SKILL.md"
)


# ---------------------------------------------------------------------------
# System prompt — composed once at import time from this agent's own prompt
# plus the pinned Humanizer skill
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    return compose_system_prompt(_PROMPT_FILE, _HUMANIZER_SKILL_PATH)


SYSTEM_PROMPT: str = _load_system_prompt()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_script_engine(
    narrative_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path] = None,
    channel_id: Optional[str] = None,
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

    _input_package: dict = {
        "task": "write_script",
        "narrative_package": narrative_package,
    }
    if (getattr(config, "SHORT_NATIVE_MODE", False) is True):
        _max_words = int(getattr(config, "SHORT_NATIVE_MAX_NARRATION_WORDS", 150) or 150)
        _input_package["production_mode"] = "short_native"
        _input_package["short_native_directive"] = {
            "total_narration_words_max": _max_words,
            "sections": 1,
            "shorts_scripts": "empty list — do not write derived Shorts",
            "chapter_markers": "none",
            "cta": "at most one short spoken line",
            "pacing": "open on the hook in the first sentence; no throat-clearing",
        }

    script_package = run_agent(
        agent_name=_AGENT_NAME,
        model_slug=_CLAUDE_SONNET,
        system_prompt=SYSTEM_PROMPT,
        input_package=_input_package,
        output_schema="script_package",
        episode_id=episode_id,
        max_tokens=_SE_MAX_TOKENS,
        config=config,
        output_dir=output_dir,
        channel_id=channel_id,
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
