"""
hypogeum/publishing/metadata.py — Platform metadata construction

Builds platform-specific titles, descriptions, and chapter blocks from
the final_render_package metadata_source.

Rules:
  - YouTube description = logline + chapter block + ai_disclosure_text
  - YouTube chapters from section_titles[] with timestamps
  - ALL descriptions MUST append ai_disclosure_text (hard gate)
  - Short titles from metadata_source.shorts_titles{}
"""

from __future__ import annotations

from typing import Optional

from hypogeum.humanizer import humanize_field
from hypogeum.logger import get_logger

_log = get_logger(agent="metadata")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# YouTube character limits
_YT_TITLE_MAX = 100
_YT_DESCRIPTION_MAX = 5000
_YT_TAGS_TOTAL_MAX = 500


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class MissingDisclosureError(RuntimeError):
    """
    Raised when ai_disclosure_text is absent or empty in metadata_source.
    This is a hard gate — publishing must not proceed without disclosure text.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_timestamp(seconds: float) -> str:
    """Convert seconds to MM:SS or HH:MM:SS timestamp string."""
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _build_sources_block(sources: list) -> str:
    """
    Build a research sources attribution block for YouTube description.

    Format:
      Sources:
      - Title (url)

    Returns empty string if sources list is empty or all entries lack url and title.
    """
    if not sources:
        return ""

    lines = ["Sources:"]
    for src in sources:
        if not isinstance(src, dict):
            continue
        title = src.get("title", "").strip()
        url = src.get("url", "").strip()
        if title and url:
            lines.append(f"- {title} ({url})")
        elif url:
            lines.append(f"- {url}")
        elif title:
            lines.append(f"- {title}")

    if len(lines) <= 1:
        return ""
    return "\n".join(lines)


def _build_chapter_block(section_titles: list) -> str:
    """
    Build a YouTube chapter block string from section_titles[].

    Each section_titles entry is expected to have:
      - title: str
      - start_seconds: float (optional, defaults to 0)

    Returns a string like:
      0:00 Introduction
      2:30 The Discovery
      ...
    """
    if not section_titles:
        return ""

    lines = []
    for entry in section_titles:
        if isinstance(entry, dict):
            title = entry.get("title", "")
            start = float(entry.get("start_seconds", 0.0))
        else:
            title = str(entry)
            start = 0.0
        if title:
            lines.append(f"{_format_timestamp(start)} {title}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_ai_disclosure_text(metadata_source: dict) -> str:
    """
    Extract and validate ai_disclosure_text from metadata_source.

    Args:
        metadata_source: The metadata_source dict from final_render_package.

    Returns:
        The ai_disclosure_text string.

    Raises:
        MissingDisclosureError — if ai_disclosure_text is absent or empty.
    """
    text = metadata_source.get("ai_disclosure_text", "")
    if not text or not text.strip():
        raise MissingDisclosureError(
            "ai_disclosure_text is missing or empty in metadata_source. "
            "Publishing cannot proceed — AI disclosure is mandatory."
        )
    return humanize_field(text.strip(), "ai_disclosure")


def build_youtube_description(
    metadata_source: dict,
    episode_id: Optional[str] = None,
) -> str:
    """
    Build the YouTube description for the main episode.
    Format: logline + blank line + chapter block + blank line + ai_disclosure_text

    Args:
        metadata_source: The metadata_source dict from final_render_package.
        episode_id:      For log correlation.

    Returns:
        YouTube description string (≤ 5000 chars).

    Raises:
        MissingDisclosureError — if ai_disclosure_text is missing.
    """
    log = get_logger(episode_id=episode_id, agent="metadata")

    disclosure = get_ai_disclosure_text(metadata_source)
    logline = humanize_field(metadata_source.get("logline", "").strip(), "logline")
    section_titles = metadata_source.get("section_titles", [])
    sources = metadata_source.get("sources", [])

    parts = []
    if logline:
        parts.append(logline)

    chapter_block = _build_chapter_block(section_titles)
    if chapter_block:
        parts.append(chapter_block)

    sources_block = _build_sources_block(sources)
    if sources_block:
        parts.append(sources_block)

    parts.append(disclosure)

    description = "\n\n".join(parts)

    if len(description) > _YT_DESCRIPTION_MAX:
        log.warn(
            "youtube_description_truncated",
            episode_id=episode_id,
            original_length=len(description),
            truncated_to=_YT_DESCRIPTION_MAX,
        )
        # Preserve disclosure at the end — truncate logline/chapters instead
        truncated_prefix = description[: _YT_DESCRIPTION_MAX - len(disclosure) - 2]
        description = truncated_prefix + "\n\n" + disclosure

    return description


def build_youtube_short_description(
    metadata_source: dict,
    short_id: str,
    episode_id: Optional[str] = None,
) -> str:
    """
    Build the YouTube description for a Short.
    Format: short title (if found) + blank line + ai_disclosure_text

    Args:
        metadata_source: The metadata_source dict from final_render_package.
        short_id:        Short identifier.
        episode_id:      For log correlation.

    Returns:
        Short YouTube description string.

    Raises:
        MissingDisclosureError — if ai_disclosure_text is missing.
    """
    disclosure = get_ai_disclosure_text(metadata_source)
    title = get_short_title(metadata_source, short_id)

    parts = []
    if title:
        parts.append(title)
    parts.append(disclosure)

    return "\n\n".join(parts)


def get_episode_title(metadata_source: dict) -> str:
    """
    Extract the episode title from metadata_source.

    Returns:
        Episode title string (may be empty if not set).
    """
    return humanize_field(metadata_source.get("episode_title", "").strip(), "title")


def get_short_title(metadata_source: dict, short_id: str) -> str:
    """
    Look up the title for a specific Short from metadata_source.shorts_titles.

    shorts_titles may be:
      - a dict: {short_id: title, ...}
      - a list: [{short_id: ..., title: ...}, ...]

    Args:
        metadata_source: The metadata_source dict from final_render_package.
        short_id:        Short identifier to look up.

    Returns:
        Title string, or empty string if not found.
    """
    shorts_titles = metadata_source.get("shorts_titles", {})

    if isinstance(shorts_titles, dict):
        raw = shorts_titles.get(short_id, "").strip()
        return humanize_field(raw, "title") if raw else raw

    if isinstance(shorts_titles, list):
        for entry in shorts_titles:
            if isinstance(entry, dict) and entry.get("short_id") == short_id:
                raw = entry.get("title", "").strip()
                return humanize_field(raw, "title") if raw else raw

    return ""


def build_facebook_description(
    metadata_source: dict,
    episode_id: Optional[str] = None,
) -> str:
    """
    Build the Facebook Video description.
    Format: logline + blank line + ai_disclosure_text

    Args:
        metadata_source: The metadata_source dict from final_render_package.
        episode_id:      For log correlation.

    Returns:
        Facebook description string.

    Raises:
        MissingDisclosureError — if ai_disclosure_text is missing.
    """
    disclosure = get_ai_disclosure_text(metadata_source)
    logline = humanize_field(metadata_source.get("logline", "").strip(), "logline")

    parts = []
    if logline:
        parts.append(logline)
    parts.append(disclosure)

    return "\n\n".join(parts)


def build_instagram_caption(
    metadata_source: dict,
    short_id: str,
    episode_id: Optional[str] = None,
) -> str:
    """
    Build the Instagram Reels caption for a Short.
    Format: short title (if found) + blank line + ai_disclosure_text

    Args:
        metadata_source: The metadata_source dict from final_render_package.
        short_id:        Short identifier.
        episode_id:      For log correlation.

    Returns:
        Instagram caption string.

    Raises:
        MissingDisclosureError — if ai_disclosure_text is missing.
    """
    disclosure = get_ai_disclosure_text(metadata_source)
    title = get_short_title(metadata_source, short_id)

    parts = []
    if title:
        parts.append(title)
    parts.append(disclosure)

    return "\n\n".join(parts)


def build_tiktok_title(
    metadata_source: dict,
    short_id: str,
    episode_id: Optional[str] = None,
) -> str:
    """
    Build the TikTok title for a Short.
    Uses short title from shorts_titles{} or falls back to episode title.

    Args:
        metadata_source: The metadata_source dict from final_render_package.
        short_id:        Short identifier.
        episode_id:      For log correlation.

    Returns:
        TikTok title string.
    """
    title = get_short_title(metadata_source, short_id)
    if not title:
        title = get_episode_title(metadata_source)
    return title
