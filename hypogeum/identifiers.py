"""
hypogeum/identifiers.py — Canonical identifier generation

Single source of truth for all stable identifiers used across the pipeline.
All functions are pure: no I/O, no side effects, no imports from other
hypogeum modules.

Identifier contracts:
    run_id                  UUID4 string. Unique per pipeline execution.
                            Not deterministic. Never reuse across runs.

    episode_id              Caller-provided (e.g. "ep_000"). No generation
                            function here — the caller owns this identifier.

    asset_id                16-char hex. Deterministic from episode_id,
                            asset_type, provider, prompt, sequence_index.

    title_id                12-char hex. Deterministic from episode_id and
                            title_text (normalized: stripped, lowercased,
                            internal whitespace collapsed).

    thumbnail_candidate_id  14-char hex. Deterministic from episode_id, rank,
                            visual_prompt (stripped, case preserved), and
                            text_overlay (normalized: stripped + lowercased).

METADATA_VERSION: "v1"
    Schema version tag embedded in every durable record written by the
    pipeline. Increment to "v2" only when a breaking schema change requires
    a migration pass over existing records. Never mutate "v1" retroactively.
"""

from __future__ import annotations

import hashlib
import re
import uuid


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------

METADATA_VERSION: str = "v1"


# ---------------------------------------------------------------------------
# Run ID — non-deterministic, unique per execution
# ---------------------------------------------------------------------------

def generate_run_id() -> str:
    """
    Return a UUID4 string representing one pipeline execution.

    Non-deterministic. Two calls to this function will (with overwhelming
    probability) return different values. Never reuse a run_id.

    Returns:
        Lowercase UUID4 string, e.g. "a3f2b1c4-7e08-4d2f-9c1b-83f5e2a1d047".
    """
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Asset ID — deterministic
# ---------------------------------------------------------------------------

def generate_asset_id(
    episode_id: str,
    asset_type: str,
    provider: str,
    prompt: str = "",
    sequence_index: int = 0,
) -> str:
    """
    Return a 16-character hex ID for a generated pipeline asset.

    Deterministic: the same argument values always produce the same ID.

    Normalization applied:
        episode_id      stripped + lowercased
        asset_type      stripped + lowercased
        provider        stripped + lowercased
        prompt          stripped only (case preserved — prompts are
                        semantically case-sensitive for image/video models;
                        internal whitespace is significant)
        sequence_index  converted to string; differentiates multiple assets
                        of the same type within one episode

    Args:
        episode_id:     Episode identifier (e.g. "ep_000").
        asset_type:     One of: "video_clip", "narration_segment", "thumbnail",
                        "music", "reference_image", "final_video". Custom
                        values are accepted but not validated here.
        provider:       Generation provider slug ("muapi", "elevenlabs",
                        "openai", "openrouter").
        prompt:         Generation prompt or script text. Empty string is valid
                        (e.g. for assets with no explicit prompt).
        sequence_index: 0-based position among assets of the same type in
                        one episode. Default 0.

    Returns:
        16-character lowercase hexadecimal string.
    """
    normalized = "|".join([
        episode_id.strip().lower(),
        asset_type.strip().lower(),
        provider.strip().lower(),
        str(sequence_index),
        prompt.strip(),
    ])
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Title ID — deterministic
# ---------------------------------------------------------------------------

def generate_title_id(title_text: str, episode_id: str) -> str:
    """
    Return a 12-character hex ID for an episode title candidate.

    Deterministic: the same title_text and episode_id always produce the
    same ID. Titles are normalized before hashing so minor formatting
    variations in the same logical title produce the same ID.

    Normalization: stripped, lowercased, internal whitespace collapsed to
    a single space. "THE COMPUTER THAT VANISHED" and
    "the computer  that vanished" produce the same ID.

    Args:
        title_text: The episode title string.
        episode_id: Episode identifier (e.g. "ep_000"). Binds the ID to
                    one episode so identical titles across episodes are
                    assigned different IDs.

    Returns:
        12-character lowercase hexadecimal string.
    """
    normalized_title = _normalize_text(title_text)
    normalized_ep = episode_id.strip().lower()
    raw = f"{normalized_ep}|{normalized_title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Thumbnail Candidate ID — deterministic
# ---------------------------------------------------------------------------

def generate_thumbnail_candidate_id(
    episode_id: str,
    rank: int,
    visual_prompt: str,
    text_overlay: str,
) -> str:
    """
    Return a 14-character hex ID for a thumbnail generation candidate.

    Stable across reruns: if episode_id, rank, visual_prompt, and
    text_overlay are unchanged, the ID is identical. This enables
    cross-run join without a persistent ID store.

    Normalization applied:
        episode_id      stripped + lowercased
        rank            converted to string (integer rank from TD output)
        visual_prompt   stripped only (case preserved — the exact prompt
                        string is passed to the image generation model)
        text_overlay    stripped + lowercased (text content is semantically
                        case-insensitive for identity purposes)

    Args:
        episode_id:    Episode identifier (e.g. "ep_000").
        rank:          Candidate rank as assigned by Thumbnail Director
                       (1 = highest priority).
        visual_prompt: The image generation prompt for this candidate.
        text_overlay:  The text rendered onto the thumbnail image.

    Returns:
        14-character lowercase hexadecimal string.
    """
    normalized = "|".join([
        episode_id.strip().lower(),
        str(rank),
        visual_prompt.strip(),
        _normalize_text(text_overlay),
    ])
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:14]


# ---------------------------------------------------------------------------
# KG Seed ID — deterministic
# ---------------------------------------------------------------------------

def generate_kg_seed_id(episode_id: str, run_id: str) -> str:
    """
    Return a 14-character hex ID for a knowledge graph seed record.

    Deterministic from episode_id and run_id. Two extractions from the
    same run always produce the same kg_seed_id, enabling idempotent writes.

    Args:
        episode_id: Episode identifier (e.g. "ep_000").
        run_id:     Run UUID4 string from generate_run_id().

    Returns:
        14-character lowercase hexadecimal string.
    """
    raw = f"{episode_id.strip().lower()}|{run_id.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:14]


def generate_signal_id(episode_id: str, run_id: str) -> str:
    """
    Return a 14-character hex ID for a signal record.

    Deterministic from episode_id and run_id. Two ingestions from the same
    run produce the same signal_id, enabling idempotent writes.

    Args:
        episode_id: Episode identifier (e.g. "ep_000").
        run_id:     Run UUID4 string from generate_run_id().

    Returns:
        14-character lowercase hexadecimal string.
    """
    raw = f"signal|{episode_id.strip().lower()}|{run_id.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:14]


def generate_entity_id(name: str, entity_type: str, episode_id: str) -> str:
    """
    Return an 8-character hex ID for a KG entity.

    Deterministic: same name, type, and episode always produce the same ID.
    Names are normalized (stripped + lowercased) before hashing.

    Args:
        name:       Entity name (e.g. "The Antikythera Mechanism").
        entity_type: Entity class (e.g. "topic", "source", "section").
        episode_id: Episode identifier — scopes entity IDs per episode.

    Returns:
        8-character lowercase hexadecimal string.
    """
    normalized = "|".join([
        name.strip().lower(),
        entity_type.strip().lower(),
        episode_id.strip().lower(),
    ])
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize_text(text: str) -> str:
    """Strip, lowercase, and collapse internal whitespace to single spaces."""
    return re.sub(r"\s+", " ", text.strip().lower())
