"""
hypogeum/prompt_loader.py — Versioned prompt file loader

Loads agent system prompts from hypogeum/prompts/*.txt and metadata from
the comment header block at the top of each file.

Header format (must appear at the very top of each prompt file, before
any non-comment line):

    # PROMPT_VERSION: v1
    # OWNER_AGENT: executive_producer
    # MODEL_TARGET: anthropic/claude-sonnet-4.6
    # PURPOSE: One-line description of what this prompt does

Header lines are stripped before the text is returned to callers.  The LLM
never sees them.  This preserves exact pre-Phase-2A prompt behaviour while
making version/ownership machine-readable.

Public API:
    load_prompt(path)                    -> str   (body text, headers stripped)
    load_skill(path)                     -> str   (skill markdown, empty if absent)
    compose_system_prompt(base, *skills) -> str   (base + skills, missing skills skipped)
    read_metadata(path)                  -> dict  (parsed header key/value pairs)
    load_music_categories(path)          -> dict  (categories from music_categories.json)

Raises:
    FileNotFoundError — prompt file does not exist (raised as-is; callers rely
                        on this to detect missing prompts at import time).
    ValueError        — prompt file exists but is empty after stripping headers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

_HEADER_RE = re.compile(r"^#\s*([A-Z_]+):\s*(.*)\s*$")
_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Required header keys — enforced by read_metadata, not by load_prompt.
REQUIRED_HEADER_KEYS = {"PROMPT_VERSION", "OWNER_AGENT", "MODEL_TARGET", "PURPOSE"}


def load_prompt(path: Path) -> str:
    """
    Read a versioned prompt file and return the body text with headers stripped.

    Args:
        path: Absolute path to the prompt .txt file.

    Returns:
        Prompt body text (no leading ``# KEY: VALUE`` lines).

    Raises:
        FileNotFoundError: File does not exist.
        ValueError: File is empty or contains only header lines.
    """
    raw = path.read_text(encoding="utf-8")
    body = _strip_headers(raw)
    if not body:
        raise ValueError(
            f"Prompt file is empty after stripping headers: {path}"
        )
    return body


def read_metadata(path: Path) -> dict:
    """
    Parse the ``# KEY: VALUE`` header block from a prompt file.

    Returns a dict of all recognised header fields.  Missing required keys
    are included in the returned dict as ``None``.

    Args:
        path: Absolute path to the prompt .txt file.

    Returns:
        dict with at minimum the REQUIRED_HEADER_KEYS as keys.
        Extra header keys (beyond the required set) are also returned.

    Raises:
        FileNotFoundError: File does not exist.
    """
    raw = path.read_text(encoding="utf-8")
    meta: dict = {}
    for line in raw.splitlines():
        m = _HEADER_RE.match(line)
        if not m:
            break  # stop at first non-header line
        meta[m.group(1)] = m.group(2).strip()
    # Ensure all required keys present
    for key in REQUIRED_HEADER_KEYS:
        meta.setdefault(key, None)
    return meta


def load_skill(path: Path) -> str:
    """
    Read a skill markdown file and return its full content.

    Unlike load_prompt(), does not strip headers — skill files are plain markdown.
    Returns an empty string if the file does not exist (safe degradation).

    Args:
        path: Absolute path to the skill .md file.

    Returns:
        Skill file content, or empty string if the file is absent.
    """
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def compose_system_prompt(base_prompt_path: Path, *skill_paths: Path) -> str:
    """
    Compose a system prompt from a base prompt file and zero or more skill files.

    Loads the base prompt via load_prompt(), then appends each skill via
    load_skill() in the order given, separated by ``\\n\\n---\\n\\n``.
    Skills whose files are absent are silently skipped — the base prompt is
    always returned even when every skill path is missing.

    This is the generic entry point for all agents that attach skills.
    No agent should concatenate base + skill manually.

    Args:
        base_prompt_path: Absolute path to the versioned base prompt (.txt).
        *skill_paths:     Zero or more absolute paths to skill (.md) files.

    Returns:
        Composed system prompt string ready to pass to run_agent().

    Raises:
        FileNotFoundError: base_prompt_path does not exist.
        ValueError:        base_prompt_path is empty after stripping headers.
    """
    parts = [load_prompt(base_prompt_path)]
    for skill_path in skill_paths:
        skill = load_skill(skill_path)
        if skill:
            parts.append(skill)
    return "\n\n---\n\n".join(parts)


def load_music_categories(path: Path) -> dict:
    """
    Load music category prompts from music_categories.json.

    The JSON file has a ``_meta`` key (ignored here) and a ``categories``
    key mapping category names to prompt strings.

    Args:
        path: Absolute path to music_categories.json.

    Returns:
        dict mapping category name (str) → prompt text (str).

    Raises:
        FileNotFoundError: File does not exist.
        KeyError: JSON file is missing the ``categories`` key.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["categories"]


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _strip_headers(text: str) -> str:
    """Strip leading ``# KEY: VALUE`` lines and return the remaining body."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.rstrip("\n\r")
        if _HEADER_RE.match(stripped):
            continue
        # First non-header line — return from here onward, strip blank lead
        return "".join(lines[i:]).lstrip("\n")
    return ""
