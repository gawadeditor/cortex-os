"""
tests/test_entity_continuity_manager.py — Unit tests for entity_continuity_manager.py

Coverage:
  TestBuildVisualSpecText         — rich text construction from entity visual_spec
  TestIsRefImageEligible          — lock_state == "final" gate
  TestGetFinalEntities            — filter locked_entities by lock_state
  TestGetPrimaryEntityUrl         — entity-to-scene mapping for ref URL injection
  TestApplyReferenceImageAssetIds — entity lock record update after ref image generation
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.entity_continuity_manager import (
    apply_reference_image_asset_ids,
    build_visual_spec_text,
    get_final_entities,
    get_primary_entity_url,
    is_ref_image_eligible,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entity(
    entity_id: str = "pharaoh_amenhotep",
    lock_state: str = "final",
    description: str = "Tall imposing figure in white linen robes.",
    period_detail: str = "New Kingdom Egypt, 18th Dynasty.",
    markers: Optional[list] = None,
    reference_image_asset_id=None,
) -> dict:
    return {
        "entity_id": entity_id,
        "entity_type": "person",
        "lock_state": lock_state,
        "visual_spec": {
            "description": description,
            "period_detail": period_detail,
            "distinctive_markers": markers if markers is not None else ["double crown", "crook and flail"],
            "accuracy_basis": "Archaeological record.",
        },
        "reference_image_asset_id": reference_image_asset_id,
    }


def _make_scene(
    scene_id: str = "s01",
    entity_appearances: Optional[list] = None,
) -> dict:
    return {
        "scene_id": scene_id,
        "entity_appearances": entity_appearances or [],
    }


# ---------------------------------------------------------------------------
# TestBuildVisualSpecText
# ---------------------------------------------------------------------------

class TestBuildVisualSpecText:

    def test_description_only(self):
        entity = _make_entity(
            description="A tall figure in robes.",
            period_detail="",
            markers=[],
        )
        result = build_visual_spec_text(entity)
        assert result == "A tall figure in robes."

    def test_description_plus_period_detail(self):
        entity = _make_entity(
            description="A tall figure.",
            period_detail="New Kingdom Egypt.",
            markers=[],
        )
        result = build_visual_spec_text(entity)
        assert result == "A tall figure. New Kingdom Egypt."

    def test_description_plus_markers(self):
        entity = _make_entity(
            description="A tall figure.",
            period_detail="",
            markers=["double crown", "crook and flail"],
        )
        result = build_visual_spec_text(entity)
        assert "Distinctive features:" in result
        assert "double crown" in result
        assert "crook and flail" in result

    def test_description_plus_period_plus_markers(self):
        entity = _make_entity(
            description="A tall figure.",
            period_detail="18th Dynasty.",
            markers=["golden sceptre"],
        )
        result = build_visual_spec_text(entity)
        assert result.startswith("A tall figure.")
        assert "18th Dynasty." in result
        assert "golden sceptre" in result

    def test_markers_joined_with_comma(self):
        entity = _make_entity(
            description="Subject.",
            period_detail="",
            markers=["crown", "robes", "staff"],
        )
        result = build_visual_spec_text(entity)
        assert "crown, robes, staff" in result

    def test_single_marker(self):
        entity = _make_entity(
            description="Subject.",
            period_detail="",
            markers=["double crown"],
        )
        result = build_visual_spec_text(entity)
        assert "Distinctive features: double crown." in result

    def test_empty_visual_spec(self):
        entity = {"entity_id": "e1", "lock_state": "final", "visual_spec": {}}
        result = build_visual_spec_text(entity)
        assert result == ""

    def test_missing_visual_spec(self):
        entity = {"entity_id": "e1", "lock_state": "final"}
        result = build_visual_spec_text(entity)
        assert result == ""

    def test_period_detail_appended_with_space(self):
        entity = _make_entity(
            description="Subject.",
            period_detail="Ptolemaic period.",
            markers=[],
        )
        result = build_visual_spec_text(entity)
        # Must be "Subject. Ptolemaic period." — space-separated
        assert result == "Subject. Ptolemaic period."

    def test_markers_suffix_ends_with_period(self):
        entity = _make_entity(
            description="Subject.",
            period_detail="",
            markers=["marker_a"],
        )
        result = build_visual_spec_text(entity)
        assert result.endswith(".")


# ---------------------------------------------------------------------------
# TestIsRefImageEligible
# ---------------------------------------------------------------------------

class TestIsRefImageEligible:

    def test_final_lock_state_is_eligible(self):
        entity = _make_entity(lock_state="final")
        assert is_ref_image_eligible(entity) is True

    def test_provisional_lock_state_is_not_eligible(self):
        entity = _make_entity(lock_state="provisional")
        assert is_ref_image_eligible(entity) is False

    def test_promoted_lock_state_is_not_eligible(self):
        entity = _make_entity(lock_state="promoted")
        assert is_ref_image_eligible(entity) is False

    def test_missing_lock_state_is_not_eligible(self):
        entity = {"entity_id": "e1"}
        assert is_ref_image_eligible(entity) is False

    def test_empty_string_lock_state_is_not_eligible(self):
        entity = {"entity_id": "e1", "lock_state": ""}
        assert is_ref_image_eligible(entity) is False

    def test_arbitrary_string_is_not_eligible(self):
        entity = _make_entity(lock_state="unknown_state")
        assert is_ref_image_eligible(entity) is False

    def test_returns_bool_true(self):
        assert type(is_ref_image_eligible(_make_entity(lock_state="final"))) is bool

    def test_returns_bool_false(self):
        assert type(is_ref_image_eligible(_make_entity(lock_state="provisional"))) is bool


# ---------------------------------------------------------------------------
# TestGetFinalEntities
# ---------------------------------------------------------------------------

class TestGetFinalEntities:

    def test_all_final_returned(self):
        entities = [
            _make_entity("e1", "final"),
            _make_entity("e2", "final"),
        ]
        result = get_final_entities(entities)
        assert len(result) == 2

    def test_only_final_returned_from_mix(self):
        entities = [
            _make_entity("e1", "final"),
            _make_entity("e2", "provisional"),
            _make_entity("e3", "promoted"),
            _make_entity("e4", "final"),
        ]
        result = get_final_entities(entities)
        ids = [e["entity_id"] for e in result]
        assert ids == ["e1", "e4"]

    def test_empty_input_returns_empty(self):
        assert get_final_entities([]) == []

    def test_none_final_returns_empty(self):
        entities = [_make_entity("e1", "provisional"), _make_entity("e2", "promoted")]
        assert get_final_entities(entities) == []

    def test_returns_entity_objects_not_copies(self):
        """get_final_entities returns the same dict objects (no deep copy)."""
        e = _make_entity("e1", "final")
        result = get_final_entities([e])
        assert result[0] is e

    def test_preserves_order(self):
        entities = [
            _make_entity("e3", "final"),
            _make_entity("e1", "final"),
            _make_entity("e2", "final"),
        ]
        result = get_final_entities(entities)
        assert [e["entity_id"] for e in result] == ["e3", "e1", "e2"]

    def test_returns_list(self):
        assert isinstance(get_final_entities([]), list)


# ---------------------------------------------------------------------------
# TestGetPrimaryEntityUrl
# ---------------------------------------------------------------------------

class TestGetPrimaryEntityUrl:

    def test_primary_entity_url_returned(self):
        scene = _make_scene(entity_appearances=[
            {"entity_id": "pharaoh_amenhotep", "role": "primary"},
        ])
        urls = {"pharaoh_amenhotep": "https://cdn/ref.jpg"}
        result = get_primary_entity_url(scene, urls)
        assert result == "https://cdn/ref.jpg"

    def test_secondary_entity_does_not_match(self):
        scene = _make_scene(entity_appearances=[
            {"entity_id": "pharaoh_amenhotep", "role": "secondary"},
        ])
        urls = {"pharaoh_amenhotep": "https://cdn/ref.jpg"}
        result = get_primary_entity_url(scene, urls)
        assert result is None

    def test_no_entity_appearances_returns_none(self):
        scene = _make_scene(entity_appearances=[])
        result = get_primary_entity_url(scene, {"e1": "https://cdn/r.jpg"})
        assert result is None

    def test_primary_entity_without_url_returns_none(self):
        scene = _make_scene(entity_appearances=[
            {"entity_id": "pharaoh_amenhotep", "role": "primary"},
        ])
        result = get_primary_entity_url(scene, {})
        assert result is None

    def test_first_primary_entity_wins(self):
        scene = _make_scene(entity_appearances=[
            {"entity_id": "entity_a", "role": "secondary"},
            {"entity_id": "entity_b", "role": "primary"},
            {"entity_id": "entity_c", "role": "primary"},
        ])
        urls = {
            "entity_b": "https://cdn/b.jpg",
            "entity_c": "https://cdn/c.jpg",
        }
        result = get_primary_entity_url(scene, urls)
        assert result == "https://cdn/b.jpg"

    def test_empty_entity_hosted_urls_returns_none(self):
        scene = _make_scene(entity_appearances=[
            {"entity_id": "e1", "role": "primary"},
        ])
        result = get_primary_entity_url(scene, {})
        assert result is None

    def test_missing_entity_appearances_key_returns_none(self):
        scene = {"scene_id": "s01"}
        result = get_primary_entity_url(scene, {"e1": "https://cdn/r.jpg"})
        assert result is None

    def test_secondary_before_primary_skipped(self):
        scene = _make_scene(entity_appearances=[
            {"entity_id": "entity_a", "role": "secondary"},
            {"entity_id": "entity_b", "role": "primary"},
        ])
        urls = {"entity_a": "https://cdn/a.jpg", "entity_b": "https://cdn/b.jpg"}
        result = get_primary_entity_url(scene, urls)
        assert result == "https://cdn/b.jpg"

    def test_returns_none_type_when_no_match(self):
        scene = _make_scene()
        result = get_primary_entity_url(scene, {})
        assert result is None


# ---------------------------------------------------------------------------
# TestApplyReferenceImageAssetIds
# ---------------------------------------------------------------------------

class TestApplyReferenceImageAssetIds:

    def test_final_entity_gets_asset_id_set(self):
        entities = [_make_entity("pharaoh_amenhotep", "final")]
        urls = {"pharaoh_amenhotep": "https://cdn/r.jpg"}
        apply_reference_image_asset_ids(entities, urls, "ep_001")
        assert entities[0]["reference_image_asset_id"] == "episodes/ep_001/video/ref_pharaoh_amenhotep"

    def test_provisional_entity_not_updated(self):
        entities = [_make_entity("pharaoh_amenhotep", "provisional")]
        urls = {"pharaoh_amenhotep": "https://cdn/r.jpg"}
        apply_reference_image_asset_ids(entities, urls, "ep_001")
        assert entities[0]["reference_image_asset_id"] is None

    def test_promoted_entity_not_updated(self):
        entities = [_make_entity("e1", "promoted")]
        urls = {"e1": "https://cdn/r.jpg"}
        apply_reference_image_asset_ids(entities, urls, "ep_001")
        assert entities[0]["reference_image_asset_id"] is None

    def test_final_entity_without_hosted_url_not_updated(self):
        entities = [_make_entity("pharaoh_amenhotep", "final")]
        apply_reference_image_asset_ids(entities, {}, "ep_001")
        assert entities[0]["reference_image_asset_id"] is None

    def test_asset_id_format_uses_episode_id(self):
        entities = [_make_entity("e1", "final")]
        urls = {"e1": "https://cdn/r.jpg"}
        apply_reference_image_asset_ids(entities, urls, "ep_042")
        assert entities[0]["reference_image_asset_id"] == "episodes/ep_042/video/ref_e1"

    def test_asset_id_format_uses_entity_id(self):
        entities = [_make_entity("antikythera_mechanism", "final")]
        urls = {"antikythera_mechanism": "https://cdn/r.jpg"}
        apply_reference_image_asset_ids(entities, urls, "ep_001")
        assert entities[0]["reference_image_asset_id"] == "episodes/ep_001/video/ref_antikythera_mechanism"

    def test_multiple_entities_mixed_states(self):
        entities = [
            _make_entity("e1", "final"),
            _make_entity("e2", "provisional"),
            _make_entity("e3", "final"),
        ]
        urls = {"e1": "https://cdn/e1.jpg", "e3": "https://cdn/e3.jpg"}
        apply_reference_image_asset_ids(entities, urls, "ep_001")
        assert entities[0]["reference_image_asset_id"] == "episodes/ep_001/video/ref_e1"
        assert entities[1]["reference_image_asset_id"] is None
        assert entities[2]["reference_image_asset_id"] == "episodes/ep_001/video/ref_e3"

    def test_modifies_entities_in_place(self):
        e = _make_entity("e1", "final")
        entities = [e]
        urls = {"e1": "https://cdn/r.jpg"}
        apply_reference_image_asset_ids(entities, urls, "ep_001")
        assert e["reference_image_asset_id"] == "episodes/ep_001/video/ref_e1"

    def test_empty_entities_list_no_error(self):
        apply_reference_image_asset_ids([], {"e1": "https://cdn/r.jpg"}, "ep_001")

    def test_empty_urls_no_updates(self):
        entities = [_make_entity("e1", "final"), _make_entity("e2", "final")]
        apply_reference_image_asset_ids(entities, {}, "ep_001")
        for e in entities:
            assert e["reference_image_asset_id"] is None

    def test_does_not_raise_on_entity_missing_entity_id(self):
        """Entities without entity_id should not cause KeyError — skip gracefully."""
        entities = [{"lock_state": "final", "reference_image_asset_id": None}]
        # Should not raise even if entity_id is missing
        try:
            apply_reference_image_asset_ids(entities, {}, "ep_001")
        except KeyError:
            pytest.fail("apply_reference_image_asset_ids raised KeyError on missing entity_id")
