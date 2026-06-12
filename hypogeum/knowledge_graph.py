"""
hypogeum/knowledge_graph.py — Knowledge Graph Seed Extraction v1

Extracts a durable, structured knowledge graph seed from pipeline output
for each completed episode. The seed captures the episode's entities,
sections, and source references as structured records — without NLP,
without inference, without an intelligence layer.

The seed is the foundation for future learning: cross-episode topic
tracking, source reuse detection, entity continuity, and world-bible
construction. The v1 extractor is purely structural — it maps known
pipeline fields into entity/relationship form.

Architecture:
    extract_kg_seed(...)        — extract + write kg_seed_{episode_id}.json
    list_kg_seeds(...)          — list all kg_seed files for an episode or all

Entity types extracted (v1):
    "episode_title"   — the episode title from production_metadata
    "topic"           — logline or topic brief text (if available)
    "section"         — section titles from metadata_source.section_titles
    "source"          — research sources from source_registry

Relationship types (v1):
    "has_section"     — episode → section entity
    "cites_source"    — episode → source entity

Output:
    kg_seed_{episode_id}.json written to .hypogeum-assets/knowledge/

Public API:
    extract_kg_seed(
        episode_id, production_metadata=None,
        source_registry=None, output_dir=None,
    ) -> dict

    list_kg_seeds(knowledge_dir=None) -> list[dict]
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.identifiers import (
    METADATA_VERSION,
    generate_entity_id,
    generate_kg_seed_id,
)


# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_KNOWLEDGE_DIR = _PROJECT_ROOT / ".hypogeum-assets" / "knowledge"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_dir(output_dir: Optional[Path]) -> Path:
    return Path(output_dir) if output_dir is not None else _DEFAULT_KNOWLEDGE_DIR


def _write_seed(seed: dict, knowledge_dir: Path, episode_id: str) -> None:
    out_path = knowledge_dir / f"kg_seed_{episode_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(seed, indent=2, ensure_ascii=False), encoding="utf-8")


def _make_entity(name: str, entity_type: str, episode_id: str, source_field: str) -> dict:
    return {
        "entity_id": generate_entity_id(name, entity_type, episode_id),
        "name": name,
        "entity_type": entity_type,
        "source_field": source_field,
    }


# ---------------------------------------------------------------------------
# Extraction logic
# ---------------------------------------------------------------------------

def _extract_entities_from_production_metadata(
    episode_id: str,
    prod_meta: dict,
) -> tuple[list[dict], list[dict]]:
    """
    Extract entities and relationships from production_metadata dict.

    Returns (entities, relationships).
    """
    entities: list[dict] = []
    relationships: list[dict] = []

    # Episode title entity
    title = (prod_meta.get("selected_title") or "").strip()
    if title:
        e = _make_entity(title, "episode_title", episode_id, "production_metadata.selected_title")
        entities.append(e)

    # Section titles → section entities + has_section relationships
    # section_titles may be in production_metadata.narrative_structure or directly
    narrative = prod_meta.get("narrative_structure") or {}
    section_titles = narrative.get("section_titles") or []

    # Also check if directly available (from metadata_source — may be passed through)
    if not section_titles:
        section_titles = prod_meta.get("section_titles") or []

    for section_name in section_titles:
        if not isinstance(section_name, str) or not section_name.strip():
            continue
        e = _make_entity(section_name.strip(), "section", episode_id,
                         "production_metadata.narrative_structure.section_titles")
        entities.append(e)
        relationships.append({
            "from_entity": f"{episode_id}",
            "to_entity": e["entity_id"],
            "relation": "has_section",
        })

    return entities, relationships


def _extract_entities_from_source_registry(
    episode_id: str,
    source_registry: list,
) -> tuple[list[dict], list[dict]]:
    """
    Extract entities and relationships from research source_registry.

    Returns (entities, relationships).
    """
    entities: list[dict] = []
    relationships: list[dict] = []

    for src in source_registry:
        if not isinstance(src, dict):
            continue
        title = (src.get("title") or src.get("source_title") or src.get("name") or "").strip()
        if not title:
            continue
        e = _make_entity(title, "source", episode_id, "source_registry")
        # Preserve original source_id if present
        if src.get("source_id"):
            e["original_source_id"] = str(src["source_id"])
        if src.get("url"):
            e["url"] = str(src["url"])[:500]
        entities.append(e)
        relationships.append({
            "from_entity": f"{episode_id}",
            "to_entity": e["entity_id"],
            "relation": "cites_source",
        })

    return entities, relationships


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_kg_seed(
    episode_id: str,
    production_metadata: Optional[dict] = None,
    source_registry: Optional[list] = None,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Extract a knowledge graph seed from episode pipeline output.

    Performs purely structural extraction: maps known pipeline fields into
    entity/relationship form. No NLP. No inference.

    Args:
        episode_id:           Episode identifier.
        production_metadata:  production_metadata dict (from write_production_metadata).
                              Provides title, narrative_structure, run_id.
        source_registry:      List of source entry dicts from Research Director.
                              Provides source entities and cites_source relationships.
        output_dir:           Override output directory.
                              Defaults to .hypogeum-assets/knowledge/.

    Returns:
        kg_seed dict with keys:
            kg_seed_id, episode_id, run_id, extracted_at, metadata_version,
            episode_facts (title, section_count, source_count),
            entities (list), relationships (list), sources (list),
            extraction_summary (entity_count, relationship_count, source_count).
    """
    prod_meta = production_metadata or {}
    src_list = source_registry or []
    knowledge_dir = _resolve_dir(output_dir)

    run_id = prod_meta.get("run_id") or ""
    kg_seed_id = generate_kg_seed_id(episode_id, run_id) if run_id else \
        generate_kg_seed_id(episode_id, "no_run")

    # Extract entities and relationships
    meta_entities, meta_rels = _extract_entities_from_production_metadata(episode_id, prod_meta)
    src_entities, src_rels = _extract_entities_from_source_registry(episode_id, src_list)

    all_entities = meta_entities + src_entities
    all_relationships = meta_rels + src_rels

    # Deduplicate entities by entity_id (same entity may appear in both paths)
    seen_ids: set[str] = set()
    deduped_entities = []
    for e in all_entities:
        if e["entity_id"] not in seen_ids:
            seen_ids.add(e["entity_id"])
            deduped_entities.append(e)

    # Build sources list (flat, for direct lookup)
    sources = []
    for src in src_list:
        if not isinstance(src, dict):
            continue
        title = (src.get("title") or src.get("source_title") or src.get("name") or "").strip()
        sources.append({
            "source_id": str(src.get("source_id") or generate_entity_id(title, "source", episode_id)),
            "title": title,
            "url": str(src.get("url") or ""),
            "source_type": str(src.get("source_type") or src.get("type") or "unknown"),
        })

    # Episode facts summary
    narrative = prod_meta.get("narrative_structure") or {}
    section_titles = narrative.get("section_titles") or prod_meta.get("section_titles") or []

    episode_facts = {
        "title": (prod_meta.get("selected_title") or "").strip(),
        "section_count": len([e for e in deduped_entities if e["entity_type"] == "section"]),
        "source_count": len(sources),
        "research_source_count": int(prod_meta.get("research_source_count") or len(sources)),
        "section_titles": [s for s in section_titles if isinstance(s, str)],
    }

    seed = {
        "kg_seed_id": kg_seed_id,
        "episode_id": episode_id,
        "run_id": run_id,
        "extracted_at": _now_iso(),
        "metadata_version": METADATA_VERSION,
        "episode_facts": episode_facts,
        "entities": deduped_entities,
        "relationships": all_relationships,
        "sources": sources,
        "extraction_summary": {
            "entity_count": len(deduped_entities),
            "relationship_count": len(all_relationships),
            "source_count": len(sources),
            "sections_extracted": len([e for e in deduped_entities if e["entity_type"] == "section"]),
        },
    }

    _write_seed(seed, knowledge_dir, episode_id)
    return seed


def list_kg_seeds(knowledge_dir: Optional[Path] = None) -> list[dict]:
    """
    Return all kg_seed records from the knowledge directory.

    Scans for kg_seed_*.json files and returns their parsed contents.
    Returns [] if the directory is absent or no seeds exist.

    Args:
        knowledge_dir: Override knowledge directory.
                       Defaults to .hypogeum-assets/knowledge/.

    Returns:
        List of kg_seed dicts, sorted by filename.
    """
    resolved_dir = _resolve_dir(knowledge_dir)
    if not resolved_dir.exists():
        return []

    results = []
    for path in sorted(resolved_dir.glob("kg_seed_*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            results.append(record)
        except Exception:
            pass
    return results
