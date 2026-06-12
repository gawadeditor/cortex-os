"""
hypogeum/world_bible.py — World Bible v1

The world bible is an accumulated, cross-episode knowledge record for the
channel. It grows with each episode: entities from kg_seeds are merged into
a persistent registry that spans all episodes.

v1 is purely structural: no NLP, no inference, no clustering. It merges
entities by entity_id (a deterministic hash of name + type + episode_id)
and accumulates them in a flat registry per entity_type.

The world bible enables:
    - Cross-episode topic tracking (have we covered this before?)
    - Source reuse detection (did we cite this source before?)
    - Entity continuity (has this person/place appeared in prior episodes?)
    - World-bible construction for Brand Guardian context

Architecture:
    update_world_bible(kg_seed, output_dir=None)   — merge kg_seed → world bible
    read_world_bible(output_dir=None)               — load current world bible
    reset_world_bible(output_dir=None)              — clear for tests only

World bible format:
    world_bible.json written to .hypogeum-assets/knowledge/

Schema:
    {
        "schema_version": "v1",
        "updated_at": "<ISO timestamp>",
        "metadata_version": "v1",
        "episode_count": <int>,
        "episodes": ["ep_000", "ep_001", ...],
        "entity_registry": {
            "episode_title": [<entity>, ...],
            "section":       [<entity>, ...],
            "source":        [<entity>, ...],
        },
        "entity_count": <int>,
        "source_count": <int>,
    }

Each entity in entity_registry carries the original entity fields plus
an "episodes" list tracking which episodes it appears in.

Public API:
    update_world_bible(kg_seed, output_dir=None) -> dict
    read_world_bible(output_dir=None) -> dict
    reset_world_bible(output_dir=None) -> None
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.identifiers import METADATA_VERSION


# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_KNOWLEDGE_DIR = _PROJECT_ROOT / ".hypogeum-assets" / "knowledge"
_WORLD_BIBLE_FILENAME = "world_bible.json"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_dir(output_dir: Optional[Path]) -> Path:
    return Path(output_dir) if output_dir is not None else _DEFAULT_KNOWLEDGE_DIR


def _bible_path(knowledge_dir: Path) -> Path:
    return knowledge_dir / _WORLD_BIBLE_FILENAME


def _empty_bible() -> dict:
    return {
        "schema_version": "v1",
        "metadata_version": METADATA_VERSION,
        "updated_at": _now_iso(),
        "episode_count": 0,
        "episodes": [],
        "entity_registry": {
            "episode_title": [],
            "section": [],
            "source": [],
        },
        "entity_count": 0,
        "source_count": 0,
    }


def _load_bible(knowledge_dir: Path) -> dict:
    path = _bible_path(knowledge_dir)
    if not path.exists():
        return _empty_bible()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_bible()


def _save_bible(bible: dict, knowledge_dir: Path) -> None:
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    _bible_path(knowledge_dir).write_text(
        json.dumps(bible, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

def _merge_entity(registry_list: list, entity: dict, episode_id: str) -> None:
    """
    Merge an entity into a registry list (in-place).

    If entity_id already exists, add episode_id to its "episodes" list.
    If it's new, append with "episodes": [episode_id].
    """
    entity_id = entity.get("entity_id")
    if not entity_id:
        return

    for existing in registry_list:
        if existing.get("entity_id") == entity_id:
            if episode_id not in existing.get("episodes", []):
                existing.setdefault("episodes", []).append(episode_id)
            return

    # New entity
    merged = {k: v for k, v in entity.items()}
    merged["episodes"] = [episode_id]
    registry_list.append(merged)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def update_world_bible(
    kg_seed: dict,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Merge a kg_seed into the world bible and write the updated bible.

    Merges episode_title, section, and source entities from the kg_seed
    into the world bible's entity_registry. Entities are deduplicated by
    entity_id. If an entity appears in multiple episodes, each episode_id
    is added to its "episodes" list.

    Idempotent: merging the same kg_seed twice produces the same result
    (episode_id appears only once per entity's "episodes" list).

    Args:
        kg_seed:    kg_seed dict from extract_kg_seed(). Must have
                    "episode_id" and "entities" keys.
        output_dir: Override knowledge directory.
                    Defaults to .hypogeum-assets/knowledge/.

    Returns:
        Updated world bible dict.
    """
    knowledge_dir = _resolve_dir(output_dir)
    bible = _load_bible(knowledge_dir)

    episode_id = str(kg_seed.get("episode_id") or "")
    if not episode_id:
        return bible

    # Track which episodes have been merged
    if episode_id not in bible["episodes"]:
        bible["episodes"].append(episode_id)

    # Ensure all known entity_type buckets exist
    registry = bible.setdefault("entity_registry", {})
    for etype in ("episode_title", "section", "source"):
        registry.setdefault(etype, [])

    # Merge entities
    for entity in kg_seed.get("entities", []):
        etype = entity.get("entity_type")
        if etype not in registry:
            registry[etype] = []
        _merge_entity(registry[etype], entity, episode_id)

    # Update summary counts
    bible["episode_count"] = len(bible["episodes"])
    bible["entity_count"] = sum(len(v) for v in registry.values())
    bible["source_count"] = len(registry.get("source", []))
    bible["updated_at"] = _now_iso()
    bible["metadata_version"] = METADATA_VERSION

    _save_bible(bible, knowledge_dir)
    return bible


def read_world_bible(output_dir: Optional[Path] = None) -> dict:
    """
    Load and return the current world bible.

    Returns an empty world bible if no file exists yet.

    Args:
        output_dir: Override knowledge directory.
                    Defaults to .hypogeum-assets/knowledge/.

    Returns:
        World bible dict.
    """
    knowledge_dir = _resolve_dir(output_dir)
    return _load_bible(knowledge_dir)


def reset_world_bible(output_dir: Optional[Path] = None) -> None:
    """
    Delete the world bible file (for test isolation only).

    Args:
        output_dir: Override knowledge directory.
    """
    knowledge_dir = _resolve_dir(output_dir)
    path = _bible_path(knowledge_dir)
    if path.exists():
        path.unlink()
