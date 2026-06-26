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
    load_prompt(path)          -> str   (body text, headers stripped)
    read_metadata(path)        -> dict  (parsed header key/value pairs)
    load_music_categories(path) -> dict (categories from music_categories.json)

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
