"""
tests/test_knowledge_graph.py — Phase 5A: Knowledge Graph Seed Extraction

Coverage:
    TestExtractKgSeedFields      — required fields in returned seed dict
    TestExtractKgSeedEntities    — entities extracted from production_metadata
    TestExtractKgSeedSources     — source entities extracted from source_registry
    TestExtractKgSeedRelationships — relationships wired correctly
    TestExtractKgSeedIdempotency — second extraction overwrites cleanly
    TestExtractKgSeedFileWritten — kg_seed file written to output_dir
    TestExtractKgSeedEmpty       — works with no production_metadata / no sources
    TestListKgSeeds              — list_kg_seeds returns correct records
    TestEntityId                 — generate_entity_id is deterministic
    TestKgSeedId                 — generate_kg_seed_id is deterministic
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hypogeum.identifiers import (
    METADATA_VERSION,
    generate_entity_id,
    generate_kg_seed_id,
)
from hypogeum.knowledge_graph import extract_kg_seed, list_kg_seeds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prod_meta(
    title: str = "The Antikythera Mechanism",
    run_id: str = "run-abc-001",
    section_titles: list = None,
    source_count: int = 3,
) -> dict:
    return {
        "run_id": run_id,
        "episode_id": "ep_000",
        "selected_title": title,
        "research_source_count": source_count,
        "narrative_structure": {
            "section_titles": ["Introduction", "Discovery", "Significance"] if section_titles is None else section_titles,
        },
    }


def _source_registry(n: int = 2) -> list:
    return [
        {
            "source_id": f"src_{i:03d}",
            "title": f"Source Title {i}",
            "url": f"https://example.com/source/{i}",
            "source_type": "academic",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# TestExtractKgSeedFields
# ---------------------------------------------------------------------------

class TestExtractKgSeedFields:
    REQUIRED_FIELDS = [
        "kg_seed_id", "episode_id", "run_id", "extracted_at",
        "metadata_version", "episode_facts", "entities",
        "relationships", "sources", "extraction_summary",
    ]

    def test_all_required_fields_present(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(), _source_registry(), output_dir=tmp_path)
        for f in self.REQUIRED_FIELDS:
            assert f in seed, f"Missing field: {f}"

    def test_metadata_version_is_v1(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(), output_dir=tmp_path)
        assert seed["metadata_version"] == METADATA_VERSION

    def test_episode_id_correct(self, tmp_path):
        seed = extract_kg_seed("ep_042", _prod_meta(), output_dir=tmp_path)
        assert seed["episode_id"] == "ep_042"

    def test_run_id_from_production_metadata(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(run_id="run-xyz-999"), output_dir=tmp_path)
        assert seed["run_id"] == "run-xyz-999"

    def test_episode_facts_has_title(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(title="Malta Mysteries"), output_dir=tmp_path)
        assert seed["episode_facts"]["title"] == "Malta Mysteries"

    def test_episode_facts_has_source_count(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(source_count=7), output_dir=tmp_path)
        assert seed["episode_facts"]["research_source_count"] == 7

    def test_extraction_summary_has_counts(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(), _source_registry(3), output_dir=tmp_path)
        summary = seed["extraction_summary"]
        assert "entity_count" in summary
        assert "relationship_count" in summary
        assert "source_count" in summary
        assert summary["source_count"] == 3


# ---------------------------------------------------------------------------
# TestExtractKgSeedEntities
# ---------------------------------------------------------------------------

class TestExtractKgSeedEntities:
    def test_section_entities_extracted(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(
            section_titles=["Intro", "Discovery", "Outro"]
        ), output_dir=tmp_path)
        entity_types = {e["entity_type"] for e in seed["entities"]}
        assert "section" in entity_types

    def test_section_count_matches(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(
            section_titles=["A", "B", "C", "D"]
        ), output_dir=tmp_path)
        sections = [e for e in seed["entities"] if e["entity_type"] == "section"]
        assert len(sections) == 4

    def test_episode_title_entity_extracted(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(title="The Lost City"), output_dir=tmp_path)
        title_entities = [e for e in seed["entities"] if e["entity_type"] == "episode_title"]
        assert len(title_entities) == 1
        assert title_entities[0]["name"] == "The Lost City"

    def test_entity_has_entity_id(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(), output_dir=tmp_path)
        for e in seed["entities"]:
            assert "entity_id" in e
            assert len(e["entity_id"]) == 8

    def test_entity_has_source_field(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(), output_dir=tmp_path)
        for e in seed["entities"]:
            assert "source_field" in e

    def test_no_duplicate_entities(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(), _source_registry(2), output_dir=tmp_path)
        ids = [e["entity_id"] for e in seed["entities"]]
        assert len(ids) == len(set(ids)), "Duplicate entity_ids found"


# ---------------------------------------------------------------------------
# TestExtractKgSeedSources
# ---------------------------------------------------------------------------

class TestExtractKgSeedSources:
    def test_sources_list_populated(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(), _source_registry(3), output_dir=tmp_path)
        assert len(seed["sources"]) == 3

    def test_source_has_required_fields(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(), _source_registry(1), output_dir=tmp_path)
        src = seed["sources"][0]
        for f in ("source_id", "title", "url", "source_type"):
            assert f in src, f"Missing source field: {f}"

    def test_source_title_correct(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(), [
            {"source_id": "s1", "title": "Ancient Greece Overview", "url": "https://x.com"}
        ], output_dir=tmp_path)
        assert seed["sources"][0]["title"] == "Ancient Greece Overview"

    def test_empty_source_registry_produces_empty_sources(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(), [], output_dir=tmp_path)
        assert seed["sources"] == []

    def test_source_entities_in_entities_list(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(), _source_registry(2), output_dir=tmp_path)
        source_entities = [e for e in seed["entities"] if e["entity_type"] == "source"]
        assert len(source_entities) == 2


# ---------------------------------------------------------------------------
# TestExtractKgSeedRelationships
# ---------------------------------------------------------------------------

class TestExtractKgSeedRelationships:
    def test_has_section_relationships_present(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(
            section_titles=["Intro", "Main"]
        ), output_dir=tmp_path)
        rel_types = {r["relation"] for r in seed["relationships"]}
        assert "has_section" in rel_types

    def test_cites_source_relationships_present(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(), _source_registry(2), output_dir=tmp_path)
        rel_types = {r["relation"] for r in seed["relationships"]}
        assert "cites_source" in rel_types

    def test_relationship_count_matches_sections_plus_sources(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(
            section_titles=["A", "B"]
        ), _source_registry(3), output_dir=tmp_path)
        # 2 has_section + 3 cites_source = 5
        assert seed["extraction_summary"]["relationship_count"] == 5

    def test_relationship_has_required_fields(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(), _source_registry(1), output_dir=tmp_path)
        for r in seed["relationships"]:
            assert "from_entity" in r
            assert "to_entity" in r
            assert "relation" in r


# ---------------------------------------------------------------------------
# TestExtractKgSeedFileWritten
# ---------------------------------------------------------------------------

class TestExtractKgSeedFileWritten:
    def test_file_created(self, tmp_path):
        extract_kg_seed("ep_000", _prod_meta(), output_dir=tmp_path)
        assert (tmp_path / "kg_seed_ep_000.json").exists()

    def test_file_parseable(self, tmp_path):
        extract_kg_seed("ep_000", _prod_meta(), output_dir=tmp_path)
        content = json.loads((tmp_path / "kg_seed_ep_000.json").read_text())
        assert content["episode_id"] == "ep_000"

    def test_file_named_by_episode_id(self, tmp_path):
        extract_kg_seed("ep_042", _prod_meta(), output_dir=tmp_path)
        assert (tmp_path / "kg_seed_ep_042.json").exists()

    def test_file_content_matches_returned_dict(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(), output_dir=tmp_path)
        content = json.loads((tmp_path / "kg_seed_ep_000.json").read_text())
        assert content["kg_seed_id"] == seed["kg_seed_id"]
        assert content["entities"] == seed["entities"]


# ---------------------------------------------------------------------------
# TestExtractKgSeedIdempotency
# ---------------------------------------------------------------------------

class TestExtractKgSeedIdempotency:
    def test_second_extraction_overwrites(self, tmp_path):
        extract_kg_seed("ep_000", _prod_meta(title="Old Title"), output_dir=tmp_path)
        extract_kg_seed("ep_000", _prod_meta(title="New Title"), output_dir=tmp_path)
        content = json.loads((tmp_path / "kg_seed_ep_000.json").read_text())
        assert content["episode_facts"]["title"] == "New Title"

    def test_same_run_id_produces_same_kg_seed_id(self, tmp_path):
        seed1 = extract_kg_seed("ep_000", _prod_meta(run_id="run-fixed"), output_dir=tmp_path)
        seed2 = extract_kg_seed("ep_000", _prod_meta(run_id="run-fixed"), output_dir=tmp_path)
        assert seed1["kg_seed_id"] == seed2["kg_seed_id"]


# ---------------------------------------------------------------------------
# TestExtractKgSeedEmpty
# ---------------------------------------------------------------------------

class TestExtractKgSeedEmpty:
    def test_no_production_metadata(self, tmp_path):
        seed = extract_kg_seed("ep_000", output_dir=tmp_path)
        assert seed["episode_id"] == "ep_000"
        assert seed["entities"] == []
        assert seed["sources"] == []

    def test_no_source_registry(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(), output_dir=tmp_path)
        assert seed["sources"] == []

    def test_no_section_titles(self, tmp_path):
        seed = extract_kg_seed("ep_000", _prod_meta(section_titles=[]), output_dir=tmp_path)
        sections = [e for e in seed["entities"] if e["entity_type"] == "section"]
        assert sections == []

    def test_file_written_even_when_empty(self, tmp_path):
        extract_kg_seed("ep_000", output_dir=tmp_path)
        assert (tmp_path / "kg_seed_ep_000.json").exists()


# ---------------------------------------------------------------------------
# TestListKgSeeds
# ---------------------------------------------------------------------------

class TestListKgSeeds:
    def test_returns_empty_when_dir_absent(self, tmp_path):
        result = list_kg_seeds(knowledge_dir=tmp_path / "nonexistent")
        assert result == []

    def test_returns_all_seeds(self, tmp_path):
        extract_kg_seed("ep_000", _prod_meta(), output_dir=tmp_path)
        extract_kg_seed("ep_001", _prod_meta(), output_dir=tmp_path)
        seeds = list_kg_seeds(knowledge_dir=tmp_path)
        assert len(seeds) == 2

    def test_seeds_have_episode_id(self, tmp_path):
        extract_kg_seed("ep_007", _prod_meta(), output_dir=tmp_path)
        seeds = list_kg_seeds(knowledge_dir=tmp_path)
        assert seeds[0]["episode_id"] == "ep_007"


# ---------------------------------------------------------------------------
# TestEntityId
# ---------------------------------------------------------------------------

class TestEntityId:
    def test_same_inputs_same_id(self):
        id1 = generate_entity_id("The Antikythera Mechanism", "topic", "ep_000")
        id2 = generate_entity_id("The Antikythera Mechanism", "topic", "ep_000")
        assert id1 == id2

    def test_different_name_different_id(self):
        id1 = generate_entity_id("Malta", "topic", "ep_000")
        id2 = generate_entity_id("Greece", "topic", "ep_000")
        assert id1 != id2

    def test_different_type_different_id(self):
        id1 = generate_entity_id("Malta", "topic", "ep_000")
        id2 = generate_entity_id("Malta", "source", "ep_000")
        assert id1 != id2

    def test_different_episode_different_id(self):
        id1 = generate_entity_id("Malta", "topic", "ep_000")
        id2 = generate_entity_id("Malta", "topic", "ep_001")
        assert id1 != id2

    def test_id_is_8_chars(self):
        eid = generate_entity_id("Test", "topic", "ep_000")
        assert len(eid) == 8

    def test_id_is_hex(self):
        eid = generate_entity_id("Test", "topic", "ep_000")
        int(eid, 16)  # raises if not valid hex


# ---------------------------------------------------------------------------
# TestKgSeedId
# ---------------------------------------------------------------------------

class TestKgSeedId:
    def test_deterministic(self):
        id1 = generate_kg_seed_id("ep_000", "run-abc")
        id2 = generate_kg_seed_id("ep_000", "run-abc")
        assert id1 == id2

    def test_different_run_different_id(self):
        id1 = generate_kg_seed_id("ep_000", "run-abc")
        id2 = generate_kg_seed_id("ep_000", "run-xyz")
        assert id1 != id2

    def test_id_is_14_chars(self):
        kid = generate_kg_seed_id("ep_000", "run-abc")
        assert len(kid) == 14
