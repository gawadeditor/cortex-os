"""
tests/test_thumbnail_handoff_brief_builder.py — Unit tests for thumbnail_handoff_brief_builder (Priority 14)

No network calls. No API credits.

Test coverage:
  TestBuildThumbnailHandoffBrief
    - returns dict with envelope metadata
    - brief written to disk
    - written file is valid JSON
    - candidates from visual package
    - short_candidates exactly 3
    - primary_subject_identity preserved
    - negative_constraints preserved
    - central_curiosity_gap preserved
    - schema validation passes
    - schema error raised for missing candidates
    - schema error raised for wrong short_candidate count
    - log=None default creates logger
  TestThumbnailHandoffBriefSchema
    - valid brief passes
    - missing thumbnail_handoff_brief_id raises
    - missing episode_id raises
    - missing visual_package_id raises
    - missing central_curiosity_gap raises
    - only one candidate raises
    - invalid candidate_type raises
    - short_candidates wrong count raises
    - short candidate pool missing short_id raises
    - short candidate pool only one candidate raises
    - missing primary_subject_identity raises
    - primary_subject_identity missing entity_id raises
    - missing negative_constraints raises
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.thumbnail_handoff_brief_builder import build_thumbnail_handoff_brief
from hypogeum.schemas import SchemaError, validate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_visual_package(episode_id: str = "ep_001") -> dict:
    return {
        "package_metadata": {
            "visual_package_id": f"vp_{episode_id}",
            "episode_id": episode_id,
        },
        "thumbnail_handoff_brief": {
            "central_curiosity_gap_statement": "What secret did the pharaoh hide from history?",
            "candidates": [
                {
                    "candidate_id": "tc_001",
                    "candidate_type": "literal_scene",
                    "description": "Pharaoh at throne at dawn",
                    "supporting_evidence_or_reasoning": "Central character's first appearance",
                    "historical_integrity_flags": [],
                    "constraint_flags": [],
                    "scene_reference": "s01",
                    "candidate_strength_score": 8,
                },
                {
                    "candidate_id": "tc_002",
                    "candidate_type": "symbolic_representation",
                    "description": "Throne room in shadow",
                    "supporting_evidence_or_reasoning": "Metaphor for hidden power",
                    "historical_integrity_flags": [],
                    "constraint_flags": [],
                    "scene_reference": None,
                    "candidate_strength_score": 7,
                },
            ],
            "short_candidates": [
                {
                    "short_id": "sh_01",
                    "candidates": [
                        {"candidate_id": "sc_001", "candidate_type": "literal_scene",
                         "description": "Pharaoh close-up", "supporting_evidence_or_reasoning": "",
                         "historical_integrity_flags": [], "constraint_flags": [],
                         "candidate_strength_score": 7, "scene_reference": None},
                        {"candidate_id": "sc_002", "candidate_type": "symbolic_representation",
                         "description": "Sun disc over Nile", "supporting_evidence_or_reasoning": "",
                         "historical_integrity_flags": [], "constraint_flags": [],
                         "candidate_strength_score": 6, "scene_reference": None},
                    ],
                },
                {
                    "short_id": "sh_02",
                    "candidates": [
                        {"candidate_id": "sc_003", "candidate_type": "literal_scene",
                         "description": "Hieroglyphs close-up", "supporting_evidence_or_reasoning": "",
                         "historical_integrity_flags": [], "constraint_flags": [],
                         "candidate_strength_score": 7, "scene_reference": None},
                        {"candidate_id": "sc_004", "candidate_type": "inferred_moment",
                         "description": "Priests in procession", "supporting_evidence_or_reasoning": "",
                         "historical_integrity_flags": [], "constraint_flags": [],
                         "candidate_strength_score": 5, "scene_reference": None},
                    ],
                },
                {
                    "short_id": "sh_03",
                    "candidates": [
                        {"candidate_id": "sc_005", "candidate_type": "symbolic_representation",
                         "description": "Desert horizon at dusk", "supporting_evidence_or_reasoning": "",
                         "historical_integrity_flags": [], "constraint_flags": [],
                         "candidate_strength_score": 6, "scene_reference": None},
                        {"candidate_id": "sc_006", "candidate_type": "literal_scene",
                         "description": "Palace gate at dawn", "supporting_evidence_or_reasoning": "",
                         "historical_integrity_flags": [], "constraint_flags": [],
                         "candidate_strength_score": 7, "scene_reference": None},
                    ],
                },
            ],
            "primary_subject_identity": {
                "entity_id": "pharaoh_amenhotep",
                "visual_spec_summary": "Tall figure in white linen with double crown.",
            },
            "episode_color_palette": "pp_001",
            "negative_constraints": ["No supernatural elements"],
        },
    }


# ---------------------------------------------------------------------------
# TestBuildThumbnailHandoffBrief
# ---------------------------------------------------------------------------

class TestBuildThumbnailHandoffBrief:

    def test_returns_dict_with_envelope_metadata(self, tmp_path):
        vp = _make_visual_package()
        brief = build_thumbnail_handoff_brief(vp, "ep_001", tmp_path)
        assert brief["thumbnail_handoff_brief_id"] == "thb_ep_001"
        assert brief["episode_id"] == "ep_001"
        assert brief["visual_package_id"] == "vp_ep_001"

    def test_brief_written_to_disk(self, tmp_path):
        vp = _make_visual_package()
        build_thumbnail_handoff_brief(vp, "ep_001", tmp_path)
        out = tmp_path / "thumbnail_handoff_brief_ep_001.json"
        assert out.exists()

    def test_written_file_is_valid_json(self, tmp_path):
        vp = _make_visual_package()
        build_thumbnail_handoff_brief(vp, "ep_001", tmp_path)
        data = json.loads((tmp_path / "thumbnail_handoff_brief_ep_001.json").read_text())
        assert data["thumbnail_handoff_brief_id"] == "thb_ep_001"

    def test_candidates_from_visual_package(self, tmp_path):
        vp = _make_visual_package()
        brief = build_thumbnail_handoff_brief(vp, "ep_001", tmp_path)
        assert len(brief["candidates"]) == 2
        assert brief["candidates"][0]["candidate_id"] == "tc_001"
        assert brief["candidates"][1]["candidate_id"] == "tc_002"

    def test_short_candidates_exactly_3(self, tmp_path):
        vp = _make_visual_package()
        brief = build_thumbnail_handoff_brief(vp, "ep_001", tmp_path)
        assert len(brief["short_candidates"]) == 3

    def test_primary_subject_identity_preserved(self, tmp_path):
        vp = _make_visual_package()
        brief = build_thumbnail_handoff_brief(vp, "ep_001", tmp_path)
        assert brief["primary_subject_identity"]["entity_id"] == "pharaoh_amenhotep"

    def test_negative_constraints_preserved(self, tmp_path):
        vp = _make_visual_package()
        brief = build_thumbnail_handoff_brief(vp, "ep_001", tmp_path)
        assert brief["negative_constraints"] == ["No supernatural elements"]

    def test_central_curiosity_gap_preserved(self, tmp_path):
        vp = _make_visual_package()
        brief = build_thumbnail_handoff_brief(vp, "ep_001", tmp_path)
        assert brief["central_curiosity_gap_statement"] == "What secret did the pharaoh hide from history?"

    def test_schema_validation_passes(self, tmp_path):
        vp = _make_visual_package()
        brief = build_thumbnail_handoff_brief(vp, "ep_001", tmp_path)
        validate("thumbnail_handoff_brief", brief)

    def test_schema_error_raised_for_missing_candidates(self, tmp_path):
        vp = _make_visual_package()
        vp["thumbnail_handoff_brief"]["candidates"] = [
            vp["thumbnail_handoff_brief"]["candidates"][0]
        ]
        with pytest.raises(SchemaError):
            build_thumbnail_handoff_brief(vp, "ep_001", tmp_path)

    def test_schema_error_raised_for_wrong_short_candidate_count(self, tmp_path):
        vp = _make_visual_package()
        vp["thumbnail_handoff_brief"]["short_candidates"] = \
            vp["thumbnail_handoff_brief"]["short_candidates"][:2]
        with pytest.raises(SchemaError):
            build_thumbnail_handoff_brief(vp, "ep_001", tmp_path)

    def test_log_none_default_creates_logger(self, tmp_path):
        vp = _make_visual_package()
        brief = build_thumbnail_handoff_brief(vp, "ep_001", tmp_path, log=None)
        assert brief["thumbnail_handoff_brief_id"] == "thb_ep_001"


# ---------------------------------------------------------------------------
# TestThumbnailHandoffBriefSchema
# ---------------------------------------------------------------------------

class TestThumbnailHandoffBriefSchema:

    def _make_valid_brief(self) -> dict:
        return {
            "thumbnail_handoff_brief_id": "thb_ep_001",
            "episode_id": "ep_001",
            "visual_package_id": "vp_ep_001",
            "central_curiosity_gap_statement": "What was hidden in the tomb?",
            "candidates": [
                {"candidate_id": "tc_001", "candidate_type": "literal_scene",
                 "description": "A", "supporting_evidence_or_reasoning": "",
                 "historical_integrity_flags": [], "constraint_flags": [],
                 "scene_reference": None, "candidate_strength_score": 7},
                {"candidate_id": "tc_002", "candidate_type": "symbolic_representation",
                 "description": "B", "supporting_evidence_or_reasoning": "",
                 "historical_integrity_flags": [], "constraint_flags": [],
                 "scene_reference": None, "candidate_strength_score": 6},
            ],
            "short_candidates": [
                {"short_id": "sh_01", "candidates": [
                    {"candidate_id": "sc_001", "candidate_type": "literal_scene",
                     "description": "X", "supporting_evidence_or_reasoning": "",
                     "historical_integrity_flags": [], "constraint_flags": [],
                     "candidate_strength_score": 7, "scene_reference": None},
                    {"candidate_id": "sc_002", "candidate_type": "inferred_moment",
                     "description": "Y", "supporting_evidence_or_reasoning": "",
                     "historical_integrity_flags": [], "constraint_flags": [],
                     "candidate_strength_score": 6, "scene_reference": None},
                ]},
                {"short_id": "sh_02", "candidates": [
                    {"candidate_id": "sc_003", "candidate_type": "literal_scene",
                     "description": "A", "supporting_evidence_or_reasoning": "",
                     "historical_integrity_flags": [], "constraint_flags": [],
                     "candidate_strength_score": 7, "scene_reference": None},
                    {"candidate_id": "sc_004", "candidate_type": "symbolic_representation",
                     "description": "B", "supporting_evidence_or_reasoning": "",
                     "historical_integrity_flags": [], "constraint_flags": [],
                     "candidate_strength_score": 5, "scene_reference": None},
                ]},
                {"short_id": "sh_03", "candidates": [
                    {"candidate_id": "sc_005", "candidate_type": "literal_scene",
                     "description": "C", "supporting_evidence_or_reasoning": "",
                     "historical_integrity_flags": [], "constraint_flags": [],
                     "candidate_strength_score": 6, "scene_reference": None},
                    {"candidate_id": "sc_006", "candidate_type": "inferred_moment",
                     "description": "D", "supporting_evidence_or_reasoning": "",
                     "historical_integrity_flags": [], "constraint_flags": [],
                     "candidate_strength_score": 8, "scene_reference": None},
                ]},
            ],
            "primary_subject_identity": {"entity_id": "pharaoh_amenhotep",
                                         "visual_spec_summary": "Tall figure."},
            "episode_color_palette": "pp_001",
            "negative_constraints": [],
        }

    def test_valid_brief_passes(self):
        validate("thumbnail_handoff_brief", self._make_valid_brief())

    def test_missing_thumbnail_handoff_brief_id_raises(self):
        b = self._make_valid_brief()
        del b["thumbnail_handoff_brief_id"]
        with pytest.raises(SchemaError):
            validate("thumbnail_handoff_brief", b)

    def test_missing_episode_id_raises(self):
        b = self._make_valid_brief()
        del b["episode_id"]
        with pytest.raises(SchemaError):
            validate("thumbnail_handoff_brief", b)

    def test_missing_visual_package_id_raises(self):
        b = self._make_valid_brief()
        del b["visual_package_id"]
        with pytest.raises(SchemaError):
            validate("thumbnail_handoff_brief", b)

    def test_missing_central_curiosity_gap_raises(self):
        b = self._make_valid_brief()
        del b["central_curiosity_gap_statement"]
        with pytest.raises(SchemaError):
            validate("thumbnail_handoff_brief", b)

    def test_only_one_candidate_raises(self):
        b = self._make_valid_brief()
        b["candidates"] = b["candidates"][:1]
        with pytest.raises(SchemaError):
            validate("thumbnail_handoff_brief", b)

    def test_invalid_candidate_type_raises(self):
        b = self._make_valid_brief()
        b["candidates"][0]["candidate_type"] = "magical_vision"
        with pytest.raises(SchemaError):
            validate("thumbnail_handoff_brief", b)

    def test_short_candidates_wrong_count_raises(self):
        b = self._make_valid_brief()
        b["short_candidates"] = b["short_candidates"][:2]
        with pytest.raises(SchemaError):
            validate("thumbnail_handoff_brief", b)

    def test_short_candidate_pool_missing_short_id_raises(self):
        b = self._make_valid_brief()
        del b["short_candidates"][0]["short_id"]
        with pytest.raises(SchemaError):
            validate("thumbnail_handoff_brief", b)

    def test_short_candidate_pool_only_one_candidate_raises(self):
        b = self._make_valid_brief()
        b["short_candidates"][1]["candidates"] = b["short_candidates"][1]["candidates"][:1]
        with pytest.raises(SchemaError):
            validate("thumbnail_handoff_brief", b)

    def test_missing_primary_subject_identity_raises(self):
        b = self._make_valid_brief()
        del b["primary_subject_identity"]
        with pytest.raises(SchemaError):
            validate("thumbnail_handoff_brief", b)

    def test_primary_subject_identity_missing_entity_id_raises(self):
        b = self._make_valid_brief()
        del b["primary_subject_identity"]["entity_id"]
        with pytest.raises(SchemaError):
            validate("thumbnail_handoff_brief", b)

    def test_missing_negative_constraints_raises(self):
        b = self._make_valid_brief()
        del b["negative_constraints"]
        with pytest.raises(SchemaError):
            validate("thumbnail_handoff_brief", b)
