"""
hypogeum/agents/executive_producer.py — Executive Producer agent (M1.2)

The Executive Producer is the first real agent in the pipeline. It evaluates
proposed episode topics and commissions Topic Briefs for approved topics.

Execution sequence:
  1. Load system prompt from hypogeum/prompts/executive_producer.txt (at import)
  2. Call run_agent() for validation_scorecard — always
  3. If decision is PASS or WARN, call run_agent() for topic_brief
  4. If decision is REJECT, return (scorecard, {}) immediately

Model: config.EP_MODEL (default: anthropic/claude-sonnet-4.6)
Max tokens: 4,096 per call (both calls)

Integration contracts (must not be violated):
  - All Claude calls go through run_agent() — never call_claude() directly
  - System prompt is loaded from disk — never hardcoded inline
  - Both output packages are validated by run_agent() via schemas.py
  - No environment access — all settings via Config
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.agent import run_agent
from hypogeum.config import Config
from hypogeum.prompt_loader import load_prompt


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MODEL_SLUG  = "anthropic/claude-sonnet-4.6"
_MAX_TOKENS  = 4_096
_AGENT_NAME  = "executive_producer"

_PROMPTS_DIR  = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_FILE  = _PROMPTS_DIR / "executive_producer.txt"


# ---------------------------------------------------------------------------
# System prompt — loaded once at import time
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    """Load the EP system prompt, stripping version headers before returning."""
    return load_prompt(_PROMPT_FILE)


SYSTEM_PROMPT: str = _load_system_prompt()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_executive_producer(
    topic_input: str,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path] = None,
    channel_id: Optional[str] = None,
) -> tuple[dict, dict]:
    """
    Execute the Executive Producer agent end-to-end.

    Makes two run_agent() calls:
      Call 1 — validation_scorecard (always).
      Call 2 — topic_brief (only when decision is PASS or WARN).

    Args:
        topic_input:  The proposed topic string, e.g. "The Antikythera Mechanism".
        config:       Loaded Config instance. Passed through to run_agent().
        episode_id:   Episode identifier for log correlation and output file naming.
        output_dir:   Optional output directory override. Passed through to run_agent().
                      When None, run_agent() uses the default assets directory.

    Returns:
        (validation_scorecard, topic_brief)
        Both are validated dicts returned by run_agent().
        When decision is REJECT, topic_brief is an empty dict {}.

    Raises:
        ResponseParseError — Claude returned content that is not a JSON dict.
        SchemaError        — Response JSON failed schema validation.
        Any exception from hypogeum.openrouter that run_agent() propagates unchanged.
    """
    # The channel's editorial identity — what this channel is, who it's for,
    # and what makes a good topic FOR IT. The Core prompt describes only the
    # role; the channel supplies the subject / audience / standard of quality.
    from hypogeum.channel_creative_brief import build_creative_brief  # noqa: PLC0415
    channel_brief = build_creative_brief(channel_id) if channel_id else build_creative_brief("")

    # -----------------------------------------------------------------------
    # Call 1: Validation Scorecard — always executed
    # -----------------------------------------------------------------------
    scorecard = run_agent(
        agent_name=_AGENT_NAME,
        model_slug=getattr(config, "EP_MODEL", _MODEL_SLUG),
        system_prompt=SYSTEM_PROMPT,
        input_package={"task": "validate_topic", "topic": topic_input,
                       "channel_brief": channel_brief},
        output_schema="validation_scorecard",
        episode_id=episode_id,
        max_tokens=_MAX_TOKENS,
        config=config,
        output_dir=output_dir,
        channel_id=channel_id,
    )

    decision = scorecard.get("decision")

    # -----------------------------------------------------------------------
    # REJECT — return without commissioning a Topic Brief.
    # The pipeline layer (run_pipeline) checks the decision and raises
    # PipelineRejectedError. This function returns a plain empty dict
    # so the caller has a consistent (scorecard, brief) tuple to unpack.
    # -----------------------------------------------------------------------
    if decision == "REJECT":
        return scorecard, {}

    # -----------------------------------------------------------------------
    # Call 2: Topic Brief — only for PASS and WARN
    # -----------------------------------------------------------------------
    brief = run_agent(
        agent_name=_AGENT_NAME,
        model_slug=getattr(config, "EP_MODEL", _MODEL_SLUG),
        system_prompt=SYSTEM_PROMPT,
        input_package={
            "task": "create_topic_brief",
            "topic": topic_input,
            "scorecard": scorecard,
            "channel_brief": channel_brief,
        },
        output_schema="topic_brief",
        episode_id=episode_id,
        max_tokens=_MAX_TOKENS,
        config=config,
        output_dir=output_dir,
        channel_id=channel_id,
    )

    return scorecard, brief
