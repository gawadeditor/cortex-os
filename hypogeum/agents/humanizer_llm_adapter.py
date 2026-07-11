"""
hypogeum/agents/humanizer_llm_adapter.py — Thin runtime adapter for the
pinned upstream Humanizer skill (0.1.0)

Loads third_party/skills/humanizer/1b48564898e999219882660237fde01bf4843a0f/
SKILL.md verbatim (via hypogeum.prompt_loader.load_skill()) and composes it
with a Hypogeum-authored instruction layer (hypogeum/prompts/
humanization_stage_llm_wrapper.txt) via the existing
hypogeum.prompt_loader.compose_system_prompt() pattern — the same base+skill
join every other agent already uses. The upstream file itself is never
edited, paraphrased, or copied into a replacement skill; it is read and
concatenated, unmodified, by the same generic composition function every
other agent's skill-injection already uses.

Claude Code's own skill auto-discovery only operates inside an interactive
Claude Code runtime session. Hypogeum's production agents call models
programmatically through hypogeum.openrouter.call_claude() — an installed
SKILL.md is not automatically visible to any of those calls. This module is
the explicit, code-level integration point that makes the pinned skill's
content actually reach a model call in this pipeline.

Scope: one call per narration beat (not a whole-package batch call), so a
per-beat LLM failure never affects any other beat's result — matching
docs/architecture/HUMANIZATION_STAGE_DESIGN.md §7.2/§8/§11's per-beat
isolation design.

Uses hypogeum.openrouter.call_claude() directly (not hypogeum.agent.run_agent()):
run_agent() couples a call to disk persistence under a registered pipeline
schema name (hypogeum.schemas.SCHEMA_NAMES) — appropriate for a whole
pipeline-stage package, not for many small per-beat calls that produce no
standalone package of their own. call_claude() is still the same "existing
provider infrastructure" (OpenRouter primary, Anthropic direct fallback,
retry handled internally) — no direct OpenAI/Anthropic SDK path is added.
Cost is tracked via hypogeum.finance.record_cost(), the same call
run_agent() itself makes, so this adapter's spend appears in the same
Finance/cost ledger as every other agent call.

No AskUserQuestion or any interactive capability is exposed — the composed
prompt (see the .txt file above) explicitly instructs the model not to ask
clarifying questions and not to expect a voice sample, which are the two
interactive branches the upstream skill's own frontmatter
(allowed-tools: [..., AskUserQuestion]) was designed for.

Public API:
    HumanizerLLMResult          — dataclass: humanized_text, model_id,
                                   provider, token counts, latency, via_fallback
    HumanizerLLMAdapterError    — raised on any failure (missing skill file,
                                   provider failure, malformed output). Never
                                   raised silently — callers must catch and
                                   apply the stage's own fallback policy.
    HUMANIZER_LLM_MODEL_ID      — frozen model slug this adapter calls
    call_humanizer_llm(...)     — the adapter's single public entry point
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.finance import record_cost
from hypogeum.humanization_fingerprint import PINNED_HUMANIZER_SHA
from hypogeum.logger import get_logger
from hypogeum.openrouter import OpenRouterError, call_claude
from hypogeum.prompt_loader import compose_system_prompt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HUMANIZER_LLM_MODEL_ID = "anthropic/claude-sonnet-4.6"

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_WRAPPER_PROMPT_PATH = _PROMPTS_DIR / "humanization_stage_llm_wrapper.txt"

_SKILLS_ROOT = Path(__file__).resolve().parent.parent.parent / "third_party" / "skills" / "humanizer"
_PINNED_SKILL_PATH = _SKILLS_ROOT / PINNED_HUMANIZER_SHA / "SKILL.md"

_MAX_TOKENS = 2_048  # a single narration beat is short; ample headroom for a rewrite

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


class HumanizerLLMAdapterError(Exception):
    """
    Raised for any adapter-level failure: the pinned skill file is missing,
    the provider call failed (OpenRouterError and all its subclasses), or
    the model's response could not be parsed as the required
    {"humanized_text": "..."} JSON shape.

    Never raised and swallowed inside this module — always propagates to
    the caller, which is responsible for applying the Humanization Stage's
    own fallback policy (revert to the deterministic-pass result, per
    HUMANIZATION_STAGE_DESIGN.md §11). This module performs no fallback
    logic itself; it only reports failure clearly.
    """


@dataclass
class HumanizerLLMResult:
    """Structured result of one successful call_humanizer_llm() invocation."""

    humanized_text: str
    model_id: str
    provider: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: int
    via_fallback: bool
    estimated_cost_usd: float


# ---------------------------------------------------------------------------
# System prompt composition — cached at module load; the pinned skill file
# and the wrapper instructions are both static for the process lifetime.
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    """
    Compose the wrapper instructions with the pinned upstream skill, via the
    same compose_system_prompt(base, *skills) every other agent uses.

    Raises HumanizerLLMAdapterError if the pinned skill file is absent —
    this adapter fails closed rather than silently proceeding without the
    skill content (compose_system_prompt()'s own load_skill() call would
    otherwise silently degrade to an empty string, which is the correct
    behavior for optional internal skills but not for this pinned,
    mandatory upstream source).
    """
    if not _PINNED_SKILL_PATH.exists():
        raise HumanizerLLMAdapterError(
            f"pinned upstream Humanizer skill not found at {_PINNED_SKILL_PATH} "
            "(commit-pinned path derived from PINNED_HUMANIZER_SHA) — refusing "
            "to proceed without the verified skill content"
        )
    return compose_system_prompt(_WRAPPER_PROMPT_PATH, _PINNED_SKILL_PATH)


_SYSTEM_PROMPT: Optional[str] = None


def _system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = _load_system_prompt()
    return _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _strip_markdown_fence(content: str) -> str:
    stripped = content.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return stripped


def _parse_humanized_text(raw_content: str) -> str:
    """
    Parse the model's response as {"humanized_text": "<non-empty string>"}.

    Raises HumanizerLLMAdapterError for any shape other than exactly that —
    not valid JSON, not a dict, missing key, or a non-string/empty value.
    Never attempts to salvage a partial or malformed response.
    """
    content = _strip_markdown_fence(raw_content)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HumanizerLLMAdapterError(
            f"malformed_llm_output: response is not valid JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise HumanizerLLMAdapterError(
            f"malformed_llm_output: expected a JSON object, got {type(parsed).__name__}"
        )

    text = parsed.get("humanized_text")
    if not isinstance(text, str) or not text.strip():
        raise HumanizerLLMAdapterError(
            "malformed_llm_output: missing or empty 'humanized_text' string field"
        )

    return text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def call_humanizer_llm(
    *,
    beat_id: str,
    original_text: str,
    episode_id: str,
    config: Config,
) -> HumanizerLLMResult:
    """
    Run the pinned upstream Humanizer skill on a single narration beat.

    Args:
        beat_id:       The beat's identifier — used for log correlation only,
                        never sent to the model (only original_text is).
        original_text: The narration beat text to humanize (this call's
                        input; the caller decides whether this is the raw
                        original or the deterministic-pass output).
        episode_id:    Current episode identifier, for log/cost correlation.
        config:        Loaded Config instance — provides API keys.

    Returns:
        HumanizerLLMResult with the rewritten text and call metadata.

    Raises:
        HumanizerLLMAdapterError — pinned skill missing, provider call
        failed (any OpenRouterError subclass), or the response could not be
        parsed as the required structured JSON shape. Never swallowed.
    """
    log = get_logger(episode_id=episode_id, agent="humanization_stage")

    system_prompt = _system_prompt()
    user_content = json.dumps(
        {"beat_id": beat_id, "text": original_text}, ensure_ascii=False
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    log.info(
        "humanizer_llm_call_started",
        episode_id=episode_id,
        beat_id=beat_id,
        model_slug=HUMANIZER_LLM_MODEL_ID,
    )

    try:
        result = call_claude(
            model_slug=HUMANIZER_LLM_MODEL_ID,
            messages=messages,
            max_tokens=_MAX_TOKENS,
            config=config,
            episode_id=episode_id,
            agent="humanization_stage",
        )
    except OpenRouterError as exc:
        log.error(
            "humanizer_llm_call_failed",
            episode_id=episode_id,
            beat_id=beat_id,
            reason="provider_failure",
            error_type=type(exc).__name__,
        )
        raise HumanizerLLMAdapterError(
            f"provider_failure: {type(exc).__name__}: {exc}"
        ) from exc

    record_cost(
        provider="openrouter",
        model=result.model_used,
        call_type="humanization_stage_llm",
        cost_usd=result.estimated_cost_usd,
        tokens_or_credits=float(result.usage.get("total_tokens", 0)),
    )

    try:
        humanized_text = _parse_humanized_text(result.content)
    except HumanizerLLMAdapterError:
        log.error(
            "humanizer_llm_call_failed",
            episode_id=episode_id,
            beat_id=beat_id,
            reason="malformed_llm_output",
        )
        raise

    log.info(
        "humanizer_llm_call_completed",
        episode_id=episode_id,
        beat_id=beat_id,
        model_used=result.model_used,
        via_fallback=result.via_fallback,
        prompt_tokens=result.usage.get("prompt_tokens", 0),
        completion_tokens=result.usage.get("completion_tokens", 0),
        latency_ms=result.response_time_ms,
    )

    input_tokens = int(result.usage.get("prompt_tokens", 0))
    output_tokens = int(result.usage.get("completion_tokens", 0))

    return HumanizerLLMResult(
        humanized_text=humanized_text,
        model_id=result.model_used,
        provider="openrouter",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        latency_ms=result.response_time_ms,
        via_fallback=result.via_fallback,
        estimated_cost_usd=result.estimated_cost_usd,
    )
