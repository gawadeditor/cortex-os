"""
hypogeum/agents/covered_topics_writer.py — Covered-Topics Writer

First-class owner of memory/covered-topics.md — the pipeline's long-term
topic memory file.

Responsibilities:
    - Own all reads and writes to covered-topics.md
    - Own deduplication semantics (one entry per episode_id)
    - Own file format validation
    - Own corruption handling and recovery behavior

Public API:
    read_topic_memory(project_root)                    → list[dict]
    record_published_topic(episode_id, youtube_video_id,
                           youtube_url, project_root)  → bool
    topic_already_covered(episode_id, project_root)    → bool

Prohibited responsibilities (owned elsewhere):
    - Publishing decisions (Publishing Agent)
    - Schema validation of pipeline artifacts (schemas.py)
    - Any provider API calls

No Claude calls. No external dependencies. Pure file I/O.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COVERED_TOPICS_RELPATH = "memory/covered-topics.md"
_FILE_HEADER = "# Covered Topics\n"

# Field order maintained for deterministic output
_ENTRY_FIELDS = ("youtube_video_id", "youtube_url", "published_at", "status")


# ---------------------------------------------------------------------------
# Internal — parsing and rendering
# ---------------------------------------------------------------------------

def _parse_entries(content: str) -> list[dict]:
    """
    Parse covered-topics.md content into a list of entry dicts.

    Each H2 section (`## episode_id`) is one entry. Lines matching
    `- key: value` are parsed as entry fields. Unknown lines are ignored.
    Entries with missing fields are included as partial entries.
    """
    entries: list[dict] = []

    # Split on H2 boundaries; parts[0] is the file header block
    parts = content.split("\n## ")
    for part in parts[1:]:
        lines = part.splitlines()
        if not lines:
            continue

        episode_id = lines[0].strip()
        if not episode_id:
            continue

        entry: dict = {"episode_id": episode_id}
        for line in lines[1:]:
            stripped = line.strip()
            if stripped.startswith("- ") and ": " in stripped:
                field_text = stripped[2:]  # remove leading "- "
                key, _, val = field_text.partition(": ")
                entry[key.strip()] = val.strip()

        entries.append(entry)

    return entries


def _render_file(entries: list[dict]) -> str:
    """
    Render a list of entry dicts back to covered-topics.md format.

    Output is deterministic: fields always appear in _ENTRY_FIELDS order.
    """
    parts: list[str] = [_FILE_HEADER]
    for entry in entries:
        episode_id = entry.get("episode_id", "")
        if not episode_id:
            continue
        parts.append(f"\n## {episode_id}\n")
        for key in _ENTRY_FIELDS:
            val = entry.get(key, "")
            if val:
                parts.append(f"- {key}: {val}\n")
    return "".join(parts)


def _is_valid_header(content: str) -> bool:
    """
    Returns True if the file content begins with the canonical header line.

    A file with no content (empty or whitespace-only) is considered valid
    but empty — it has no header corruption, just no data.
    """
    stripped = content.lstrip()
    return stripped == "" or stripped.startswith("# Covered Topics")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Internal — safe I/O
# ---------------------------------------------------------------------------

def _safe_read(topics_path: Path) -> Optional[str]:
    """
    Read covered-topics.md safely.

    Returns the file content on success.
    Returns None if the file does not exist.
    Raises CoveredTopicsCorruptionError if the file cannot be decoded.
    """
    if not topics_path.exists():
        return None
    try:
        return topics_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        raise CoveredTopicsCorruptionError(
            topics_path,
            f"cannot read file: {exc}",
        ) from exc


def _backup_corrupted(topics_path: Path) -> Path:
    """
    Rename a corrupted covered-topics.md to a timestamped .corrupt backup.

    Returns the backup path.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = topics_path.with_name(f"covered-topics.md.corrupt.{stamp}")
    topics_path.rename(backup)
    return backup


def _write_file(topics_path: Path, content: str) -> None:
    topics_path.parent.mkdir(parents=True, exist_ok=True)
    topics_path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CoveredTopicsCorruptionError(RuntimeError):
    """
    Raised internally when covered-topics.md cannot be read or parsed.

    Callers (read_topic_memory, record_published_topic) catch this,
    back up the corrupted file, and recover automatically. It is never
    surfaced to external callers — they receive [] or False instead.
    """
    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"covered-topics.md corrupted at {path}: {reason}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_topic_memory(project_root: Path) -> list[dict]:
    """
    Read all recorded topic entries from covered-topics.md.

    Returns a list of entry dicts, each containing at minimum:
        {"episode_id": str}

    and optionally:
        {"youtube_video_id": str, "youtube_url": str,
         "published_at": str, "status": str}

    Returns [] if the file does not exist or is empty.
    Returns [] (after backing up) if the file is corrupted.
    Never raises.
    """
    topics_path = project_root / _COVERED_TOPICS_RELPATH

    try:
        content = _safe_read(topics_path)
    except CoveredTopicsCorruptionError:
        try:
            _backup_corrupted(topics_path)
        except OSError:
            pass
        return []

    if content is None:
        return []

    if not _is_valid_header(content):
        # Header missing — corrupted or replaced; back up and return empty
        try:
            _backup_corrupted(topics_path)
        except OSError:
            pass
        return []

    return _parse_entries(content)


def record_published_topic(
    episode_id: str,
    youtube_video_id: str,
    youtube_url: str,
    project_root: Path,
) -> bool:
    """
    Append a new entry or update an existing entry in covered-topics.md.

    If the episode_id already exists in the file, its youtube_video_id,
    youtube_url, and status are updated to "completed". The published_at
    timestamp is preserved from the original entry.

    If the episode_id does not exist, a new entry is appended with
    status="completed" and the current UTC timestamp as published_at.

    Deduplication guarantee: after this call, exactly one entry for
    episode_id exists in covered-topics.md.

    Returns True on success, False on any I/O error.
    Never raises.
    """
    if not episode_id:
        return False

    topics_path = project_root / _COVERED_TOPICS_RELPATH
    recovered = False

    # -- Read existing entries --
    try:
        content = _safe_read(topics_path)
    except CoveredTopicsCorruptionError:
        try:
            _backup_corrupted(topics_path)
        except OSError:
            pass
        content = None
        recovered = True

    if content is not None and not _is_valid_header(content):
        try:
            _backup_corrupted(topics_path)
        except OSError:
            pass
        content = None
        recovered = True

    entries: list[dict]
    if content is None or content.strip() == "":
        entries = []
    else:
        entries = _parse_entries(content)

    # -- Deduplication: remove any existing entries for this episode_id --
    original_entry: Optional[dict] = None
    cleaned: list[dict] = []
    for entry in entries:
        if entry.get("episode_id") == episode_id:
            original_entry = entry
        else:
            cleaned.append(entry)

    # -- Build the new/updated entry --
    published_at = (
        original_entry.get("published_at", "")
        if original_entry
        else ""
    ) or _now_iso()

    updated_entry: dict = {
        "episode_id": episode_id,
        "youtube_video_id": youtube_video_id,
        "youtube_url": youtube_url,
        "published_at": published_at,
        "status": "completed",
    }

    cleaned.append(updated_entry)

    # -- Write --
    try:
        _write_file(topics_path, _render_file(cleaned))
        return True
    except OSError:
        return False


def topic_already_covered(episode_id: str, project_root: Path) -> bool:
    """
    Returns True if episode_id appears in covered-topics.md.

    A match is based solely on episode_id presence — status is not checked.
    Returns False if the file does not exist, is empty, or is corrupted.
    Never raises.
    """
    if not episode_id:
        return False
    entries = read_topic_memory(project_root)
    return any(e.get("episode_id") == episode_id for e in entries)
