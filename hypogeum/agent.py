"""
hypogeum/agent.py — Agent runner

The single mechanism by which all 9 pipeline agents are invoked.
No agent calls OpenRouter, the logger, or the schema validator directly — they
all go through run_agent().

Execution sequence per call:
  1. Log agent_run_started
  2. Serialize input_package → JSON → user message
  3. call_claude() via hypogeum.openrouter (handles retry and fallback internally)
  4. Parse the response content as JSON
  5. Validate the parsed dict against output_schema via hypogeum.schemas
  6. Write the validated package to disk
  7. Log agent_run_completed; return the package

Integration contracts (must not be violated by callers or this module):
  - All environment access goes through the Config object passed in — never os.getenv()
  - All logging goes through hypogeum.logger.get_logger() — never print() or stdlib logging
  - All Claude calls go through hypogeum.openrouter.call_claude() — never requests directly
  - All output validation goes through hypogeum.schemas.validate() — no duplicate field checks

Logging events emitted:
  agent_run_started          — info  — before the Claude call
  agent_run_completed        — info  — after the package is written to disk
  agent_package_written      — info  — immediately after write, before completed
  agent_response_parse_failed — error — response was not valid JSON or not a dict
  agent_schema_validation_failed — error — response JSON failed schema validation
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.finance import record_cost
from hypogeum.logger import get_logger
from hypogeum.openrouter import call_claude
from hypogeum.schemas import SchemaError, validate


# ---------------------------------------------------------------------------
# Path constants (consistent with config.py and logger.py derivation)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ASSETS_ROOT  = _PROJECT_ROOT / ".hypogeum-assets"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AgentError(RuntimeError):
    """Base class for agent-runner errors."""


class ProductionAssetProtectionError(AgentError):
    """
    Raised when _write_package() would overwrite a path that has been registered
    as a protected production asset via protect_production_path().

    The production pipeline never calls protect_production_path() — protection is
    opt-in and is only activated by validation/experiment scripts that must not
    overwrite live production files.
    """
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"Write blocked: {path} is a protected production asset. "
            "Route validation output to .hypogeum-assets/experiments/ instead."
        )


# Paths registered here are write-protected. Empty by default so the
# production pipeline is never affected. Validation scripts populate this set
# before calling run_agent() to prevent accidental overwrites of live assets.
_PROTECTED_PRODUCTION_PATHS: set[str] = set()


def protect_production_path(path: Path) -> None:
    """
    Register a path as a protected production asset.

    Any subsequent call to _write_package() that resolves to this path will
    raise ProductionAssetProtectionError instead of writing.

    Args:
        path: Absolute path to protect. Resolved to its canonical form.
    """
    _PROTECTED_PRODUCTION_PATHS.add(str(path.resolve()))


class ResponseParseError(AgentError):
    """
    Claude's response could not be parsed as a JSON dict.

    This covers:
      - Response content that is not valid JSON at all
      - Response content that is valid JSON but not a dict (e.g., a list or null)

    The agent's system prompt must instruct Claude to return a JSON object.
    If this error fires frequently, review the system prompt's output instructions.
    """
    def __init__(self, agent_name: str, reason: str, content_preview: str = "") -> None:
        self.agent_name = agent_name
        self.reason = reason
        self.content_preview = content_preview
        detail = f": {content_preview!r}" if content_preview else ""
        super().__init__(
            f"Agent {agent_name!r} returned non-dict-JSON content — {reason}{detail}"
        )


# ---------------------------------------------------------------------------
# Markdown fence stripping
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_markdown_fence(content: str) -> str:
    """
    Strip an optional markdown code fence from a Claude response.

    Claude occasionally wraps JSON in ```json ... ``` even when instructed
    not to. This function strips the fence if present and returns the inner
    content; if no fence is found the original string is returned unchanged.

    Handles:
      ```json\\n{...}\\n```   — explicit JSON language tag
      ```\\n{...}\\n```       — bare fence
      {...}                  — raw JSON (no-op)
    """
    stripped = content.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return stripped


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_agent(
    agent_name: str,
    model_slug: str,
    system_prompt: str,
    input_package: dict,
    output_schema: str,
    episode_id: str,
    max_tokens: int,
    config: Config,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Execute one agent invocation end-to-end.

    Args:
        agent_name:    Agent identifier used in log entries and error messages.
                       Matches the agent's role name, e.g. "executive_producer".
        model_slug:    OpenRouter model slug, e.g. "anthropic/claude-sonnet-4.6".
                       Callers are responsible for resolving the correct slug from
                       the frozen model assignment table.
        system_prompt: The agent's system prompt text. Callers load this from disk
                       or construct it — run_agent() does not read prompt files.
        input_package: The input to send to Claude as the user message. Will be
                       JSON-serialized and sent verbatim as the user turn content.
        output_schema: The schema name to validate the response against.
                       Must be one of hypogeum.schemas.SCHEMA_NAMES.
        episode_id:    Current episode identifier. Used for log correlation and
                       to construct the output file path.
        max_tokens:    Maximum tokens Claude may generate. Set per agent based on
                       the expected output package size.
        config:        Loaded Config instance — provides API keys and all settings.
                       Never access os.getenv() directly.
        output_dir:    Directory where the output JSON file is written.
                       Defaults to .hypogeum-assets/episodes/{episode_id}/.
                       Override in tests to redirect output to a temp directory.

    Returns:
        The validated output package as a dict.

    Raises:
        ResponseParseError — Claude returned content that is not a JSON dict.
        SchemaError        — Response JSON is a dict but fails output_schema validation.
        AgentError         — Any other agent-runner-level failure.
        HardFailure, TruncatedResponse, ContentFiltered, EmptyResponse,
        CreditsExhausted, FallbackFailed — from hypogeum.openrouter; propagate
        unchanged so callers can apply the correct escalation logic.
    """
    log = get_logger(episode_id=episode_id, agent=agent_name)

    log.info(
        "agent_run_started",
        output_schema=output_schema,
        model_slug=model_slug,
        max_tokens=max_tokens,
    )

    # --- 1. Build messages -----------------------------------------------
    # System prompt is the agent's instruction set.
    # Input package is serialized to JSON and sent as the user turn.
    user_content = json.dumps(input_package, ensure_ascii=False)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_content},
    ]

    # --- 2. Call Claude (retry + fallback handled inside call_claude) -----
    result = call_claude(
        model_slug=model_slug,
        messages=messages,
        max_tokens=max_tokens,
        config=config,
        episode_id=episode_id,
        agent=agent_name,
    )

    # Record cost against active run context (non-blocking; silently skipped if
    # no run context is set, e.g. during unit tests)
    record_cost(
        provider="openrouter",
        model=result.model_used,
        call_type=agent_name,
        cost_usd=result.estimated_cost_usd,
        tokens_or_credits=float(result.usage.get("total_tokens", 0)),
    )

    # --- 3. Parse JSON response -------------------------------------------
    raw_content = _strip_markdown_fence(result.content)
    try:
        package = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        preview = raw_content[:300]
        log.error(
            "agent_response_parse_failed",
            output_schema=output_schema,
            reason="invalid_json",
            error=str(exc),
            content_preview=preview,
        )
        raise ResponseParseError(
            agent_name=agent_name,
            reason=f"JSON decode error: {exc}",
            content_preview=preview,
        ) from exc

    if not isinstance(package, dict):
        preview = raw_content[:300]
        log.error(
            "agent_response_parse_failed",
            output_schema=output_schema,
            reason="not_a_dict",
            got_type=type(package).__name__,
            content_preview=preview,
        )
        raise ResponseParseError(
            agent_name=agent_name,
            reason=f"expected JSON object, got {type(package).__name__}",
            content_preview=preview,
        )

    # --- 4. Validate output schema ----------------------------------------
    try:
        validate(output_schema, package)
    except SchemaError as exc:
        log.error(
            "agent_schema_validation_failed",
            output_schema=output_schema,
            path=exc.path,
            reason=exc.reason,
        )
        raise  # SchemaError carries schema_name, path, reason — do not wrap

    # --- 5. Write to disk -------------------------------------------------
    out_path = _resolve_output_path(episode_id, output_schema, output_dir)
    _write_package(package, out_path, output_schema, log)

    # --- 6. Log completion ------------------------------------------------
    log.info(
        "agent_run_completed",
        output_schema=output_schema,
        output_path=str(out_path),
        prompt_tokens=result.usage.get("prompt_tokens", 0),
        completion_tokens=result.usage.get("completion_tokens", 0),
        estimated_cost_usd=result.estimated_cost_usd,
        via_fallback=result.via_fallback,
    )

    return package


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_output_path(
    episode_id: str,
    output_schema: str,
    output_dir: Optional[Path],
) -> Path:
    """
    Construct the output file path.

    Default: .hypogeum-assets/episodes/{episode_id}/{output_schema}_{episode_id}.json
    Override: {output_dir}/{output_schema}_{episode_id}.json
    """
    base = output_dir if output_dir is not None else (_ASSETS_ROOT / "episodes" / episode_id)
    return base / f"{output_schema}_{episode_id}.json"


def _write_package(package: dict, path: Path, output_schema: str, log) -> None:
    """
    Serialize package to pretty-printed JSON and write to path.
    Creates parent directories if they do not exist.
    """
    if str(path.resolve()) in _PROTECTED_PRODUCTION_PATHS:
        raise ProductionAssetProtectionError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(package, ensure_ascii=False, indent=2)
    path.write_text(content, encoding="utf-8")
    log.info(
        "agent_package_written",
        output_schema=output_schema,
        path=str(path),
        size_bytes=len(content.encode("utf-8")),
    )
