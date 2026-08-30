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

import re
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
        _beat_secs = float(getattr(config, "SHORT_NATIVE_MAX_BEAT_SECONDS", 8.5) or 8.5)
        _wps = float(getattr(config, "SHORT_NATIVE_WORDS_PER_SECOND", 2.9) or 2.9)
        _per_beat_words = max(8, int(_beat_secs * _wps))
        _beat_lo = max(3, _max_words // _per_beat_words)
        _beat_hi = max(_beat_lo, min(8, _max_words // max(1, int(6.0 * _wps))))
        _input_package["production_mode"] = "short_native"
        _input_package["short_native_directive"] = {
            "total_narration_words_max": _max_words,
            "sections": 1,
            "narration_beats": (
                f"{_beat_lo} to {_beat_hi} beats. EACH beat becomes exactly one "
                f"8-second vertical video clip, so each beat's spoken narration "
                f"MUST be sayable in {_beat_secs:.0f} seconds or less — hard cap "
                f"{_per_beat_words} words per beat. A beat that runs long will be "
                f"truncated automatically; write within the budget."
            ),
            "per_beat_words_max": _per_beat_words,
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

    if (getattr(config, "SHORT_NATIVE_MODE", False) is True):
        _enforce_short_native_budget(script_package, config, log)

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


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _enforce_short_native_budget(script_package: dict, config, log) -> None:
    """Deterministically make a short-native script fit its clip budget.

    SHORT_NATIVE gives every narration beat its own single 8s vertical clip
    (see scene_planner.build_scene_plan_input). If a beat's narration runs
    longer than one clip can cover, Assembly is forced onto the coverage-fill
    path -- the exact thing short-native exists to avoid. The LLM directive
    asks for a compliant script; this pass guarantees one, with no extra
    model call:

      1. per beat: keep only the leading whole sentences that fit
         SHORT_NATIVE_MAX_BEAT_SECONDS (always keep at least one sentence).
      2. across beats, in order: once the running total passes
         SHORT_NATIVE_MAX_NARRATION_WORDS, drop the remaining beats
         (never below 3 beats / 1 beat minimum).
      3. drop any text_overlay whose beat_reference no longer exists.
    """
    wps = float(getattr(config, "SHORT_NATIVE_WORDS_PER_SECOND", 2.9) or 2.9)
    max_beat_words = max(8, int(float(
        getattr(config, "SHORT_NATIVE_MAX_BEAT_SECONDS", 8.5) or 8.5) * wps))
    max_total_words = int(getattr(config, "SHORT_NATIVE_MAX_NARRATION_WORDS", 150) or 150)

    sections = script_package.get("sections", []) or []
    if not sections:
        return

    trimmed_beats = 0
    dropped_beats = 0
    running_total = 0
    kept_beat_ids: set = set()

    for section in sections:
        beats = section.get("narration_beats", []) or []
        new_beats: list = []
        for beat in beats:
            text = (beat.get("text") or "").strip()
            words = text.split()
            # (1) per-beat sentence-boundary trim
            if len(words) > max_beat_words:
                sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
                kept, count = [], 0
                for sent in sentences:
                    sw = len(sent.split())
                    if kept and count + sw > max_beat_words:
                        break
                    kept.append(sent)
                    count += sw
                if not kept:  # single sentence already over budget — hard word cut
                    kept = [" ".join(words[:max_beat_words])]
                    count = max_beat_words
                new_text = " ".join(kept).strip()
                if new_text != text:
                    beat["text"] = new_text
                    trimmed_beats += 1
                    words = new_text.split()
            # (2) total-budget beat drop (keep >=3 where possible, >=1 always)
            if new_beats and running_total >= max_total_words and len(new_beats) >= 3:
                dropped_beats += 1
                continue
            running_total += len(words)
            new_beats.append(beat)
            if beat.get("beat_id"):
                kept_beat_ids.add(beat["beat_id"])
        section["narration_beats"] = new_beats or beats[:1]
        for b in section["narration_beats"]:
            if b.get("beat_id"):
                kept_beat_ids.add(b["beat_id"])

    # (3) prune now-dangling overlays
    dropped_overlays = 0
    for short in script_package.get("shorts_scripts", []) or []:
        ov = short.get("text_overlays", []) or []
        keep = [o for o in ov if o.get("beat_reference") in kept_beat_ids]
        dropped_overlays += len(ov) - len(keep)
        short["text_overlays"] = keep

    if trimmed_beats or dropped_beats or dropped_overlays:
        log.info(
            "se_short_native_budget_enforced",
            trimmed_beats=trimmed_beats,
            dropped_beats=dropped_beats,
            dropped_overlays=dropped_overlays,
            per_beat_words_max=max_beat_words,
            total_words_max=max_total_words,
            final_total_words=running_total,
        )
