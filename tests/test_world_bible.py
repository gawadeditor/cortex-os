"""
tests/test_world_bible.py — Phase 5D: World Bible Schema

Coverage:
    TestWorldBibleFields        — required fields in returned world bible
    TestWorldBibleEntityMerge   — entities merged correctly from kg_seed
    TestWorldBibleDeduplication — same entity_id merged, not duplicated
    TestWorldBibleCrossEpisode  — entities from multiple episodes accumulate
    TestWorldBibleEpisodeList   — episodes list tracks all merged episodes
    TestWorldBibleSourceCount   — source_count and entity_count updated
    TestWorldBibleFileWritten   — world_bible.json written to output_dir
    TestWorldBibleIdempotency   — merging same kg_seed twice is idempotent
    TestWorldBibleEmpty         — works with no entities in kg_seed
    TestReadWorldBible          — read_world_bible returns current state
    TestResetWorldBible         — reset_world_bible clears the file
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hypogeum.identifiers import METADATA_VERSION, generate_entity_id
from hypogeum.knowledge_graph import extract_kg_seed
from hypogeum.world_bible import (
    read_world_bible,
    reset_world_bible,
    update_world_bible,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_kg_seed(
    episode_id: str = "ep_000",
    title: str = "The Antikythera Mechanism",
    section_titles: list = None,
    sources: list = None,
) -> dict:
    """Build a minimal kg_seed dict without writing files."""
    section_titles = section_titles or ["Introduction", "Discovery"]
    sources = sources or []

    entities = []
    relationships = []

    # Episode title entity
    if title:
        e = {
            "entity_id": generate_entity_id(title, "episode_title", episode_id),
            "name": title,
            "entity_type": "episode_title",
            "source_field": "production_metadata.selected_title",
        }
        entities.append(e)

    # Section entities
    for s in section_titles:
        e = {
            "entity_id": generate_entity_id(s, "section", episode_id),
            "name": s,
            "entity_type": "section",
            "source_field": "production_metadata.narrative_structure.section_titles",
        }
        entities.append(e)
        relationships.append({
            "from_entity": episode_id,
            "to_entity": e["entity_id"],
            "relation": "has_section",
        })

    # Source entities
    for src in sources:
        title_s = src.get("title", "")
        e = {
            "entity_id": generate_entity_id(title_s, "source", episode_id),
            "name": title_s,
            "entity_type": "source",
            "source_field": "source_registry",
        }
        entities.append(e)

    return {
        "kg_seed_id": f"kgseed_{episode_id}",
        "episode_id": episode_id,
        "run_id": "run-test-001",
        "extracted_at": "2026-06-12T10:00:00+00:00",
        "metadata_version": METADATA_VERSION,
        "entities": entities,
        "relationships": relationships,
        "sources": sources,
        "extraction_summary": {
            "entity_count": len(entities),
            "relationship_count": len(relationships),
            "source_count": len(sources),
        },
    }


# ---------------------------------------------------------------------------
# TestWorldBibleFields
# ---------------------------------------------------------------------------

class TestWorldBibleFields:
    REQUIRED_FIELDS = [
        "schema_version", "metadata_version", "updated_at",
        "episode_count", "episodes", "entity_registry",
        "entity_count", "source_count",
    ]

    def test_all_required_fields_present(self, tmp_path):
        bible = update_world_bible(_minimal_kg_seed(), output_dir=tmp_path)
        for f in self.REQUIRED_FIELDS:
            assert f in bible, f"Missing field: {f}"

    def test_schema_version_is_v1(self, tmp_path):
        bible = update_world_bible(_minimal_kg_seed(), output_dir=tmp_path)
        assert bible["schema_version"] == "v1"

    def test_metadata_version_is_v1(self, tmp_path):
        bible = update_world_bible(_minimal_kg_seed(), output_dir=tmp_path)
        assert bible["metadata_version"] == METADATA_VERSION

    def test_entity_registry_has_buckets(self, tmp_path):
        bible = update_world_bible(_minimal_kg_seed(), output_dir=tmp_path)
        reg = bible["entity_registry"]
        for bucket in ("episode_title", "section", "source"):
            assert bucket in reg, f"Missing bucket: {bucket}"

    def test_entity_registry_buckets_are_lists(self, tmp_path):
        bible = update_world_bible(_minimal_kg_seed(), output_dir=tmp_path)
        for k, v in bible["entity_registry"].items():
            assert isinstance(v, list), f"Bucket {k!r} is not a list"


# ---------------------------------------------------------------------------
# TestWorldBibleEntityMerge
# ---------------------------------------------------------------------------

class TestWorldBibleEntityMerge:
    def test_episode_title_in_registry(self, tmp_path):
        bible = update_world_bible(
            _minimal_kg_seed(title="The Lost City"), output_dir=tmp_path
        )
        titles = bible["entity_registry"]["episode_title"]
        assert any(e["name"] == "The Lost City" for e in titles)

    def test_sections_in_registry(self, tmp_path):
        bible = update_world_bible(
            _minimal_kg_seed(section_titles=["Intro", "Main"]), output_dir=tmp_path
        )
        sections = bible["entity_registry"]["section"]
        names = {e["name"] for e in sections}
        assert "Intro" in names
        assert "Main" in names

    def test_section_count_matches(self, tmp_path):
        bible = update_world_bible(
            _minimal_kg_seed(section_titles=["A", "B", "C"]), output_dir=tmp_path
        )
        assert len(bible["entity_registry"]["section"]) == 3

    def test_sources_in_registry(self, tmp_path):
        sources = [{"title": "Ancient History Overview", "source_id": "s1"}]
        bible = update_world_bible(
            _minimal_kg_seed(sources=sources), output_dir=tmp_path
        )
        src_entities = bible["entity_registry"]["source"]
        assert any(e["name"] == "Ancient History Overview" for e in src_entities)

    def test_entity_has_episodes_list(self, tmp_path):
        bible = update_world_bible(_minimal_kg_seed(), output_dir=tmp_path)
        for etype, entities in bible["entity_registry"].items():
            for e in entities:
                assert "episodes" in e, f"Entity in {etype} missing 'episodes'"
                assert isinstance(e["episodes"], list)

    def test_entity_episode_id_correct(self, tmp_path):
        bible = update_world_bible(
            _minimal_kg_seed(episode_id="ep_007"), output_dir=tmp_path
        )
        for etype, entities in bible["entity_registry"].items():
            for e in entities:
                assert "ep_007" in e["episodes"]


# ---------------------------------------------------------------------------
# TestWorldBibleDeduplication
# ---------------------------------------------------------------------------

class TestWorldBibleDeduplication:
    def test_same_section_not_duplicated_same_episode(self, tmp_path):
        seed = _minimal_kg_seed(section_titles=["Introduction"])
        update_world_bible(seed, output_dir=tmp_path)
        update_world_bible(seed, output_dir=tmp_path)  # merge again
        bible = read_world_bible(output_dir=tmp_path)
        sections = bible["entity_registry"]["section"]
        intro_count = sum(1 for e in sections if e["name"] == "Introduction")
        assert intro_count == 1

    def test_episode_id_not_duplicated_in_episodes_list(self, tmp_path):
        seed = _minimal_kg_seed(episode_id="ep_000")
        update_world_bible(seed, output_dir=tmp_path)
        update_world_bible(seed, output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        assert bible["episodes"].count("ep_000") == 1

    def test_entity_episodes_list_no_duplicates(self, tmp_path):
        seed = _minimal_kg_seed(episode_id="ep_000", section_titles=["Intro"])
        update_world_bible(seed, output_dir=tmp_path)
        update_world_bible(seed, output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        for e in bible["entity_registry"]["section"]:
            assert e["episodes"].count("ep_000") == 1


# ---------------------------------------------------------------------------
# TestWorldBibleCrossEpisode
# ---------------------------------------------------------------------------

class TestWorldBibleCrossEpisode:
    def test_two_episodes_both_in_registry(self, tmp_path):
        update_world_bible(_minimal_kg_seed(episode_id="ep_000", title="Alpha"), output_dir=tmp_path)
        update_world_bible(_minimal_kg_seed(episode_id="ep_001", title="Beta"), output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        titles = {e["name"] for e in bible["entity_registry"]["episode_title"]}
        assert "Alpha" in titles
        assert "Beta" in titles

    def test_shared_section_name_appears_in_both_episodes(self, tmp_path):
        update_world_bible(_minimal_kg_seed(
            episode_id="ep_000", section_titles=["Introduction"]
        ), output_dir=tmp_path)
        update_world_bible(_minimal_kg_seed(
            episode_id="ep_001", section_titles=["Introduction"]
        ), output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        # "Introduction" in ep_000 and ep_001 have DIFFERENT entity_ids
        # (entity_id is scoped per episode) — so two entries exist
        sections = bible["entity_registry"]["section"]
        intro_sections = [e for e in sections if e["name"] == "Introduction"]
        assert len(intro_sections) == 2

    def test_episode_count_increments(self, tmp_path):
        update_world_bible(_minimal_kg_seed(episode_id="ep_000"), output_dir=tmp_path)
        update_world_bible(_minimal_kg_seed(episode_id="ep_001"), output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        assert bible["episode_count"] == 2


# ---------------------------------------------------------------------------
# TestWorldBibleEpisodeList
# ---------------------------------------------------------------------------

class TestWorldBibleEpisodeList:
    def test_episodes_list_populated(self, tmp_path):
        update_world_bible(_minimal_kg_seed(episode_id="ep_000"), output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        assert "ep_000" in bible["episodes"]

    def test_multiple_episodes_in_list(self, tmp_path):
        for ep in ("ep_000", "ep_001", "ep_002"):
            update_world_bible(_minimal_kg_seed(episode_id=ep), output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        assert set(bible["episodes"]) == {"ep_000", "ep_001", "ep_002"}

    def test_episode_count_matches_list_length(self, tmp_path):
        for ep in ("ep_000", "ep_001"):
            update_world_bible(_minimal_kg_seed(episode_id=ep), output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        assert bible["episode_count"] == len(bible["episodes"])


# ---------------------------------------------------------------------------
# TestWorldBibleSourceCount
# ---------------------------------------------------------------------------

class TestWorldBibleSourceCount:
    def test_source_count_updated(self, tmp_path):
        sources = [
            {"title": "Source A", "source_id": "s1"},
            {"title": "Source B", "source_id": "s2"},
        ]
        update_world_bible(_minimal_kg_seed(sources=sources), output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        assert bible["source_count"] == 2

    def test_entity_count_includes_all_types(self, tmp_path):
        sources = [{"title": "Source A", "source_id": "s1"}]
        seed = _minimal_kg_seed(
            section_titles=["Intro", "Main"],
            sources=sources,
        )
        update_world_bible(seed, output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        # 1 episode_title + 2 sections + 1 source = 4
        assert bible["entity_count"] == 4

    def test_no_sources_gives_zero_source_count(self, tmp_path):
        update_world_bible(_minimal_kg_seed(sources=[]), output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        assert bible["source_count"] == 0


# ---------------------------------------------------------------------------
# TestWorldBibleFileWritten
# ---------------------------------------------------------------------------

class TestWorldBibleFileWritten:
    def test_file_created(self, tmp_path):
        update_world_bible(_minimal_kg_seed(), output_dir=tmp_path)
        assert (tmp_path / "world_bible.json").exists()

    def test_file_parseable(self, tmp_path):
        update_world_bible(_minimal_kg_seed(), output_dir=tmp_path)
        content = json.loads((tmp_path / "world_bible.json").read_text())
        assert "entity_registry" in content

    def test_file_content_matches_returned_dict(self, tmp_path):
        bible = update_world_bible(_minimal_kg_seed(), output_dir=tmp_path)
        content = json.loads((tmp_path / "world_bible.json").read_text())
        assert content["episode_count"] == bible["episode_count"]
        assert content["entity_count"] == bible["entity_count"]


# ---------------------------------------------------------------------------
# TestWorldBibleIdempotency
# ---------------------------------------------------------------------------

class TestWorldBibleIdempotency:
    def test_merging_same_seed_twice_is_idempotent(self, tmp_path):
        seed = _minimal_kg_seed()
        update_world_bible(seed, output_dir=tmp_path)
        update_world_bible(seed, output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        assert bible["episode_count"] == 1
        # Entity count should not double
        assert bible["entity_count"] == len(seed["entities"])

    def test_three_merges_same_seed_stable(self, tmp_path):
        seed = _minimal_kg_seed()
        for _ in range(3):
            update_world_bible(seed, output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        assert bible["episode_count"] == 1


# ---------------------------------------------------------------------------
# TestWorldBibleEmpty
# ---------------------------------------------------------------------------

class TestWorldBibleEmpty:
    def test_empty_entities_still_writes_file(self, tmp_path):
        seed = _minimal_kg_seed(title="", section_titles=[], sources=[])
        # Remove entities
        seed["entities"] = []
        update_world_bible(seed, output_dir=tmp_path)
        assert (tmp_path / "world_bible.json").exists()

    def test_empty_seed_episode_still_tracked(self, tmp_path):
        seed = _minimal_kg_seed(title="", section_titles=[], sources=[])
        seed["entities"] = []
        update_world_bible(seed, output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        assert "ep_000" in bible["episodes"]

    def test_no_entity_id_entity_skipped(self, tmp_path):
        seed = _minimal_kg_seed()
        seed["entities"].append({"name": "Orphan", "entity_type": "section"})  # no entity_id
        update_world_bible(seed, output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        orphans = [e for e in bible["entity_registry"].get("section", []) if e.get("name") == "Orphan"]
        assert len(orphans) == 0


# ---------------------------------------------------------------------------
# TestReadWorldBible
# ---------------------------------------------------------------------------

class TestReadWorldBible:
    def test_returns_empty_when_no_file(self, tmp_path):
        bible = read_world_bible(output_dir=tmp_path)
        assert bible["episode_count"] == 0
        assert bible["episodes"] == []
        assert bible["entity_count"] == 0

    def test_returns_current_state_after_update(self, tmp_path):
        update_world_bible(_minimal_kg_seed(episode_id="ep_000"), output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        assert "ep_000" in bible["episodes"]

    def test_returns_accumulated_state(self, tmp_path):
        update_world_bible(_minimal_kg_seed(episode_id="ep_000"), output_dir=tmp_path)
        update_world_bible(_minimal_kg_seed(episode_id="ep_001"), output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        assert bible["episode_count"] == 2


# ---------------------------------------------------------------------------
# TestResetWorldBible
# ---------------------------------------------------------------------------

class TestResetWorldBible:
    def test_reset_removes_file(self, tmp_path):
        update_world_bible(_minimal_kg_seed(), output_dir=tmp_path)
        assert (tmp_path / "world_bible.json").exists()
        reset_world_bible(output_dir=tmp_path)
        assert not (tmp_path / "world_bible.json").exists()

    def test_reset_on_absent_file_is_safe(self, tmp_path):
        reset_world_bible(output_dir=tmp_path)  # must not raise

    def test_read_after_reset_returns_empty(self, tmp_path):
        update_world_bible(_minimal_kg_seed(), output_dir=tmp_path)
        reset_world_bible(output_dir=tmp_path)
        bible = read_world_bible(output_dir=tmp_path)
        assert bible["episode_count"] == 0
