"""
hypogeum/prompt_registry.py — Prompt Registry

Scans hypogeum/prompts/ for versioned prompt files (.txt only) and builds
an in-memory registry of all prompts with parsed metadata.

Design rules:
  - Pure filesystem access — no network calls, no DB, no git.
  - Prompt names are filenames without extension (e.g. "executive_producer").
  - Body text returned by get_prompt() has all header lines stripped.
  - list_prompts() never loads body text (fast path; metadata only).
  - get_prompt() and get_prompt_metadata() return None for unknown names.
  - music_categories.json is excluded — it has its own loader in prompt_loader.py.

Public API:
    list_prompts()               -> list[PromptRecord]   (body=None on each)
    get_prompt(name)             -> PromptRecord | None   (body populated)
    get_prompt_metadata(name)    -> dict | None           (headers only)
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from hypogeum.prompt_loader import load_prompt, read_metadata

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class PromptRecord:
    """
    Metadata and optional body for one versioned prompt file.

    ``body`` is None when constructed by list_prompts() (metadata-only).
    It is populated when returned by get_prompt().
    """
    name: str
    """Filename stem — e.g. ``executive_producer`` (no ``.txt``)."""

    file_path: str
    """Path relative to the project root — e.g. ``hypogeum/prompts/executive_producer.txt``."""

    prompt_version: Optional[str]
    """``PROMPT_VERSION`` header value — e.g. ``"v1"``."""

    owner_agent: Optional[str]
    """``OWNER_AGENT`` header value."""

    model_target: Optional[str]
    """``MODEL_TARGET`` header value — e.g. ``"anthropic/claude-sonnet-4.6"``."""

    purpose: Optional[str]
    """``PURPOSE`` header value — one-line description."""

    char_count: int
    """Character count of the body text (headers excluded)."""

    body: Optional[str] = field(default=None)
    """
    Full prompt body text with header lines stripped.
    Always ``None`` when returned from ``list_prompts()``.
    Populated by ``get_prompt()``.
    """

    def to_dict(self, include_body: bool = False) -> dict:
        """Serialise to a plain dict suitable for JSON output."""
        d = {
            "name":           self.name,
            "file_path":      self.file_path,
            "prompt_version": self.prompt_version,
            "owner_agent":    self.owner_agent,
            "model_target":   self.model_target,
            "purpose":        self.purpose,
            "char_count":     self.char_count,
        }
        if include_body:
            d["body"] = self.body
        return d


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_prompts() -> list[PromptRecord]:
    """
    Return metadata records for every .txt prompt file in hypogeum/prompts/.

    Body text is NOT loaded (body=None on every record).
    Records are sorted alphabetically by name.
    Files that fail to parse are silently skipped.

    Returns:
        Sorted list of PromptRecord with body=None.
    """
    records: list[PromptRecord] = []
    for path in sorted(_PROMPTS_DIR.glob("*.txt")):
        record = _build_record(path, load_body=False)
        if record is not None:
            records.append(record)
    return records


def get_prompt(name: str) -> Optional[PromptRecord]:
    """
    Return the full PromptRecord for the named prompt, including body text.

    Args:
        name: Prompt name — filename stem without ``.txt``
              (e.g. ``"executive_producer"``).

    Returns:
        PromptRecord with body populated, or None if the file does not exist.
    """
    path = _PROMPTS_DIR / f"{name}.txt"
    if not path.exists():
        return None
    return _build_record(path, load_body=True)


def get_prompt_metadata(name: str) -> Optional[dict]:
    """
    Return only the parsed header metadata for the named prompt.

    Args:
        name: Prompt name (filename stem, no extension).

    Returns:
        Dict with keys PROMPT_VERSION, OWNER_AGENT, MODEL_TARGET, PURPOSE
        (values may be None if header is absent), or None if file missing.
    """
    path = _PROMPTS_DIR / f"{name}.txt"
    if not path.exists():
        return None
    return read_metadata(path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_record(path: Path, load_body: bool) -> Optional[PromptRecord]:
    """
    Parse one prompt file into a PromptRecord.

    Returns None if the file cannot be read or is empty after stripping.
    """
    try:
        meta = read_metadata(path)
        body_text = load_prompt(path) if load_body else None
        # char_count is always computed from the body (strip headers even
        # if we are not returning the body, to get a consistent count).
        body_for_count = body_text if load_body else _body_only(path)
        char_count = len(body_for_count) if body_for_count else 0

        rel_path = str(path.relative_to(_PROJECT_ROOT))

        return PromptRecord(
            name=path.stem,
            file_path=rel_path,
            prompt_version=meta.get("PROMPT_VERSION"),
            owner_agent=meta.get("OWNER_AGENT"),
            model_target=meta.get("MODEL_TARGET"),
            purpose=meta.get("PURPOSE"),
            char_count=char_count,
            body=body_text,
        )
    except Exception:
        return None


def _body_only(path: Path) -> str:
    """Load body text (headers stripped) without caching — used for char_count."""
    try:
        return load_prompt(path)
    except Exception:
        return ""
