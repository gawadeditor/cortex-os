"""
hypogeum/agents/thumbnail_strategy.py — Thumbnail Strategy Skill (Phase 1)

Generates three marketing-concept CTR hypotheses for a given episode BEFORE
any visual package or scene planning occurs. This is the entry point for
concept-driven thumbnail generation — the alternative to the scene-candidate
system used by VPD → TD.

Phase 1: standalone skill, invoked manually via scripts/generate_thumbnail_strategy.py.
Phase 2 (2026-07-19): wired into hypogeum/pipeline.py between Script Engine and
Visual Package Director — see pipeline.py's own docstring and
_run_thumbnail_strategy(). scripts/generate_thumbnail_strategy.py remains
valid for manual/backfill use (e.g. re-running strategy for one episode)
but is no longer the only path that activates CONCEPT_DRIVEN_MODE.

Architecture position:
    topic_brief + script_package → [THIS SKILL] → thumbnail_concept_brief
    thumbnail_concept_brief → VPD + TD (automatic — TD's existing
        file-existence check on thumbnail_concept_brief_{episode_id}.json
        is what activates CONCEPT_DRIVEN_MODE; unchanged by this wiring)

Public API:
    run_thumbnail_strategy(
        topic_brief, script_package, config, episode_id, output_dir
    ) -> dict

Output schema: thumbnail_concept_brief
Output file:   thumbnail_concept_brief_{episode_id}.json

Logging events:
    tss_started            — info  — before the model call
    tss_completed          — info  — after validation passes
    tss_hook_text_repair_attempted — warn — a suggested_hook_text word-count
                                     violation triggered a bounded, deterministic
                                     regeneration retry (see _HOOK_TEXT_WORD_COUNT_RE
                                     and _MAX_HOOK_TEXT_REPAIR_ATTEMPTS below)

Hook-text word-count repair (2026-08-17):
    hypogeum.schemas._validate_thumbnail_concept_brief requires every concept's
    suggested_hook_text to be 3-5 words — a real, otherwise-compliant model
    response can violate this (observed: a 6-word hook). run_agent() validates
    strictly before persisting anything (no partial/invalid write is possible),
    but it has no retry-on-SchemaError behaviour of its own, so a single
    out-of-range hook previously failed the whole stage outright.
    run_thumbnail_strategy() now catches exactly the suggested_hook_text
    word-count SchemaError shape (nothing else) and retries the SAME model
    call, bounded by _MAX_HOOK_TEXT_REPAIR_ATTEMPTS, with an explicit
    correction instruction built deterministically from the SchemaError's own
    path/reason (concept id, offending text, actual word count) — asking the
    model to rewrite ONLY that concept's hook to 3-5 words while preserving
    its meaning. Never truncates or rewrites text locally: the fix is either a
    compliant regenerated response or, after the bound is exhausted, the
    original SchemaError propagates unchanged (fail closed — no episode ever
    receives an invalid or silently-altered hook).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from hypogeum.agent import run_agent
from hypogeum.config import Config
from hypogeum.prompt_loader import load_prompt
from hypogeum.schemas import SchemaError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "thumbnail_strategy"
_DEFAULT_MODEL = "anthropic/claude-sonnet-4.6"
_MAX_TOKENS = 1_500

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILE = _PROMPTS_DIR / "thumbnail_strategy.txt"

# Bounded retry count for the hook-text word-count repair path. A repair
# attempt is a full regeneration of the response (still schema-validated
# before any disk write), not a local edit — bounding it caps worst-case
# extra model calls at a small, fixed number rather than looping forever
# against a model that keeps violating the same constraint.
_MAX_HOOK_TEXT_REPAIR_ATTEMPTS = 2

# Matches schemas.py's exact suggested_hook_text word-count SchemaError shape:
#   path   = "concepts[<i>].suggested_hook_text"
#   reason = "must be 3–5 words (got <n>): '<hook text>'"
# Deliberately narrow: only this specific failure is eligible for repair.
# Any other SchemaError (missing field, duplicate trigger, wrong concept
# count, etc.) is never caught here and propagates immediately, unchanged.
_HOOK_TEXT_PATH_RE = re.compile(r"^concepts\[(\d+)\]\.suggested_hook_text$")
_HOOK_TEXT_REASON_RE = re.compile(r"must be 3–5 words \(got (\d+)\): '(.*)'$")


def _load_system_prompt() -> str:
    return load_prompt(_PROMPT_FILE)


def _hook_text_repair_instruction(exc: SchemaError) -> Optional[dict]:
    """
    Build a deterministic correction instruction from a suggested_hook_text
    word-count SchemaError, or return None if this SchemaError is not that
    specific, narrow shape (caller must re-raise in that case).
    """
    if exc.schema_name != "thumbnail_concept_brief":
        return None
    path_match = _HOOK_TEXT_PATH_RE.match(exc.path)
    reason_match = _HOOK_TEXT_REASON_RE.search(exc.reason)
    if not path_match or not reason_match:
        return None
    concept_index = int(path_match.group(1))
    word_count = int(reason_match.group(1))
    offending_text = reason_match.group(2)
    return {
        "concept_index": concept_index,
        "field": "suggested_hook_text",
        "offending_text": offending_text,
        "offending_word_count": word_count,
        "required_word_count_range": "3-5",
        "instruction": (
            f"Your previous response's concepts[{concept_index}].suggested_hook_text "
            f"was {word_count} words ({offending_text!r}), outside the required 3-5 "
            f"word range. Regenerate the full thumbnail_concept_brief response, "
            f"changing ONLY concepts[{concept_index}].suggested_hook_text to a "
            f"3-5 word phrase that preserves the same hook meaning and emotional "
            f"trigger — do not truncate the existing text, write a new compliant "
            f"phrase. Every other field must stay the same as your previous response."
        ),
    }


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

    brief = {
        "episode_id": topic_brief.get("topic_id") or topic_brief.get("episode_id", ""),
        "episode_title": topic_brief.get("episode_title", ""),
        "hook_language": hook_language,
        "central_mystery": topic_brief.get("mystery_anchor", ""),
        "viewer_entry_point": topic_brief.get("curiosity_hook", ""),
        "competitive_gap": competitive_gap,
        "audience_profile": audience_profile,
        "topic_category": category,
    }

    # Channel Learning: thumbnail directives this channel has accrued from
    # its own analytics (hypogeum.channel_learning). Advisory to the model;
    # empty for a channel with no learning history.
    try:
        from hypogeum.brand import channel_identity  # noqa: PLC0415
        from hypogeum.channel_learning import directive_texts  # noqa: PLC0415
        ch = channel_identity.current_channel()
        if ch:
            learned = directive_texts(ch, "thumbnail")
            if learned:
                brief["channel_thumbnail_learning"] = learned
    except Exception:  # noqa: BLE001
        pass

    return brief


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
        SchemaError  — model response failed validation and either was not a
                       repairable suggested_hook_text word-count violation, or
                       remained non-compliant after _MAX_HOOK_TEXT_REPAIR_ATTEMPTS
                       repair attempts (fail closed — nothing is persisted).
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

    input_package = {
        "task": "generate_thumbnail_concepts",
        "thumbnail_strategy_brief": strategy_brief,
    }

    concept_brief: Optional[dict] = None
    last_exc: Optional[SchemaError] = None
    for attempt in range(_MAX_HOOK_TEXT_REPAIR_ATTEMPTS + 1):
        try:
            concept_brief = run_agent(
                agent_name=_AGENT_NAME,
                model_slug=model,
                system_prompt=SYSTEM_PROMPT,
                input_package=input_package,
                output_schema="thumbnail_concept_brief",
                episode_id=episode_id,
                max_tokens=_MAX_TOKENS,
                config=config,
                output_dir=_resolve_output_dir,
            )
            break
        except SchemaError as exc:
            repair = _hook_text_repair_instruction(exc)
            if repair is None or attempt >= _MAX_HOOK_TEXT_REPAIR_ATTEMPTS:
                raise
            log.warn(
                "tss_hook_text_repair_attempted",
                episode_id=episode_id,
                attempt=attempt + 1,
                max_attempts=_MAX_HOOK_TEXT_REPAIR_ATTEMPTS,
                concept_index=repair["concept_index"],
                offending_text=repair["offending_text"],
                offending_word_count=repair["offending_word_count"],
            )
            last_exc = exc
            input_package = {
                "task": "generate_thumbnail_concepts",
                "thumbnail_strategy_brief": strategy_brief,
                "correction_required": repair,
            }

    if concept_brief is None:
        # Unreachable in practice (the loop either returns or raises), but
        # keeps the fail-closed contract explicit if that ever changes.
        raise last_exc or SchemaError(
            "thumbnail_concept_brief", "concepts", "repair loop exited without a result"
        )

    log.info(
        "tss_completed",
        episode_id=episode_id,
        concepts_count=len(concept_brief.get("concepts", [])),
        triggers=[c.get("emotional_trigger") for c in concept_brief.get("concepts", [])],
    )

    return concept_brief
