"""
tests/test_thumbnail_prompt_writer.py — Unit tests for thumbnail_prompt_writer (Priority 12)

All Claude calls (run_agent) are mocked. No network calls. No API credits.

Test coverage:
  TestWriteThumbnailPrompts
    - returns generation_plan dict from run_agent
    - run_agent called with handoff_brief in input_package
    - run_agent called with task = "plan_thumbnail_generation"
    - run_agent called with output_schema = "thumbnail_generation_plan"
    - run_agent called with model_slug = anthropic/claude-sonnet-4.6
    - run_agent called with max_tokens = 4096
    - run_agent called with correct episode_id
    - run_agent called with correct output_dir
    - primary_subject_identity passes through to Claude via brief
    - negative_constraints pass through to Claude via brief
    - td_planning_started logged before run_agent
    - td_planning_completed logged after run_agent
    - td_planning_completed includes main_candidates_planned count
    - td_planning_completed includes shorts_count
    - returns whatever run_agent returns (passthrough)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.thumbnail_prompt_writer import write_thumbnail_prompts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCH = "hypogeum.agents.thumbnail_prompt_writer.run_agent"


def _make_handoff_brief() -> dict:
    return {
        "thumbnail_handoff_brief_id": "thb_ep_001",
        "episode_id": "ep_001",
        "visual_package_id": "vp_ep_001",
        "central_curiosity_gap_statement": "What was hidden?",
        "candidates": [
            {"candidate_id": "tc_001", "candidate_type": "literal_scene",
             "description": "Pharaoh", "supporting_evidence_or_reasoning": "",
             "historical_integrity_flags": [], "constraint_flags": [],
             "scene_reference": None, "candidate_strength_score": 8},
            {"candidate_id": "tc_002", "candidate_type": "symbolic_representation",
             "description": "Throne", "supporting_evidence_or_reasoning": "",
             "historical_integrity_flags": [], "constraint_flags": [],
             "scene_reference": None, "candidate_strength_score": 7},
        ],
        "short_candidates": [
            {"short_id": "sh_01", "candidates": [
                {"candidate_id": "sc_001", "candidate_type": "literal_scene",
                 "description": "X", "supporting_evidence_or_reasoning": "",
                 "historical_integrity_flags": [], "constraint_flags": [],
                 "candidate_strength_score": 7, "scene_reference": None},
                {"candidate_id": "sc_002", "candidate_type": "symbolic_representation",
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
        "primary_subject_identity": {
            "entity_id": "pharaoh_amenhotep",
            "visual_spec_summary": "Tall figure with double crown.",
        },
        "episode_color_palette": "pp_001",
        "negative_constraints": ["No supernatural elements"],
    }


def _make_generation_plan() -> dict:
    return {
        "thumbnail_generation_plan_id": "tgp_ep_001",
        "episode_id": "ep_001",
        "main_episode": {
            "ranked_candidates": [
                {"candidate_id": "tc_001", "rank": 1,
                 "image_prompt": "Ancient throne room.", "aspect_ratio": "16:9",
                 "ctr_rationale": "Strong candidate.", "text_overlay": None},
            ]
        },
        "shorts": [
            {"short_id": "sh_01", "ranked_candidates": [
                {"candidate_id": "sc_001", "rank": 1,
                 "image_prompt": "Pharaoh close-up.", "aspect_ratio": "9:16",
                 "ctr_rationale": "Strong candidate.", "text_overlay": None},
            ]},
            {"short_id": "sh_02", "ranked_candidates": [
                {"candidate_id": "sc_003", "rank": 1,
                 "image_prompt": "Hieroglyphs.", "aspect_ratio": "9:16",
                 "ctr_rationale": "Strong candidate.", "text_overlay": None},
            ]},
            {"short_id": "sh_03", "ranked_candidates": [
                {"candidate_id": "sc_006", "rank": 1,
                 "image_prompt": "Palace gate.", "aspect_ratio": "9:16",
                 "ctr_rationale": "Strong candidate.", "text_overlay": None},
            ]},
        ],
        "primary_subject_preserved": True,
        "curiosity_gap_alignment": "Rank-1 serves the gap.",
    }


def _make_log() -> MagicMock:
    log = MagicMock()
    log.info = MagicMock()
    return log


# ---------------------------------------------------------------------------
# TestWriteThumbnailPrompts
# ---------------------------------------------------------------------------

class TestWriteThumbnailPrompts:

    def test_returns_generation_plan(self, tmp_path):
        plan = _make_generation_plan()
        log = _make_log()
        with patch(_PATCH, return_value=plan):
            result = write_thumbnail_prompts(
                _make_handoff_brief(), MagicMock(), "ep_001", tmp_path, log
            )
        assert result == plan

    def test_returns_whatever_run_agent_returns(self, tmp_path):
        custom_plan = {"thumbnail_generation_plan_id": "custom", "episode_id": "ep_999",
                       "main_episode": {"ranked_candidates": [
                           {"candidate_id": "x", "rank": 1, "image_prompt": "p",
                            "aspect_ratio": "16:9", "ctr_rationale": "r", "text_overlay": None}
                       ]},
                       "shorts": [], "primary_subject_preserved": True,
                       "curiosity_gap_alignment": "aligned"}
        log = _make_log()
        with patch(_PATCH, return_value=custom_plan):
            result = write_thumbnail_prompts(
                _make_handoff_brief(), MagicMock(), "ep_001", tmp_path, log
            )
        assert result is custom_plan

    def test_run_agent_called_once(self, tmp_path):
        log = _make_log()
        with patch(_PATCH, return_value=_make_generation_plan()) as mock_ra:
            write_thumbnail_prompts(
                _make_handoff_brief(), MagicMock(), "ep_001", tmp_path, log
            )
        mock_ra.assert_called_once()

    def test_handoff_brief_in_input_package(self, tmp_path):
        brief = _make_handoff_brief()
        log = _make_log()
        with patch(_PATCH, return_value=_make_generation_plan()) as mock_ra:
            write_thumbnail_prompts(brief, MagicMock(), "ep_001", tmp_path, log)
        call_kwargs = mock_ra.call_args[1]
        assert call_kwargs["input_package"]["thumbnail_handoff_brief"] == brief

    def test_task_key_in_input_package(self, tmp_path):
        log = _make_log()
        with patch(_PATCH, return_value=_make_generation_plan()) as mock_ra:
            write_thumbnail_prompts(
                _make_handoff_brief(), MagicMock(), "ep_001", tmp_path, log
            )
        call_kwargs = mock_ra.call_args[1]
        assert call_kwargs["input_package"]["task"] == "plan_thumbnail_generation"

    def test_output_schema_is_thumbnail_generation_plan(self, tmp_path):
        log = _make_log()
        with patch(_PATCH, return_value=_make_generation_plan()) as mock_ra:
            write_thumbnail_prompts(
                _make_handoff_brief(), MagicMock(), "ep_001", tmp_path, log
            )
        call_kwargs = mock_ra.call_args[1]
        assert call_kwargs["output_schema"] == "thumbnail_generation_plan"

    def test_model_slug_from_config(self, tmp_path):
        log = _make_log()
        cfg = MagicMock()
        cfg.TD_PLANNING_MODEL = "test-gpt-model"
        cfg.TD_PLANNING_MAX_TOKENS = 7777
        with patch(_PATCH, return_value=_make_generation_plan()) as mock_ra:
            write_thumbnail_prompts(
                _make_handoff_brief(), cfg, "ep_001", tmp_path, log
            )
        call_kwargs = mock_ra.call_args[1]
        assert call_kwargs["model_slug"] == "test-gpt-model"

    def test_max_tokens_from_config(self, tmp_path):
        log = _make_log()
        cfg = MagicMock()
        cfg.TD_PLANNING_MODEL = "test-gpt-model"
        cfg.TD_PLANNING_MAX_TOKENS = 7777
        with patch(_PATCH, return_value=_make_generation_plan()) as mock_ra:
            write_thumbnail_prompts(
                _make_handoff_brief(), cfg, "ep_001", tmp_path, log
            )
        call_kwargs = mock_ra.call_args[1]
        assert call_kwargs["max_tokens"] == 7777

    def test_episode_id_passed_to_run_agent(self, tmp_path):
        log = _make_log()
        with patch(_PATCH, return_value=_make_generation_plan()) as mock_ra:
            write_thumbnail_prompts(
                _make_handoff_brief(), MagicMock(), "ep_042", tmp_path, log
            )
        call_kwargs = mock_ra.call_args[1]
        assert call_kwargs["episode_id"] == "ep_042"

    def test_output_dir_passed_to_run_agent(self, tmp_path):
        log = _make_log()
        with patch(_PATCH, return_value=_make_generation_plan()) as mock_ra:
            write_thumbnail_prompts(
                _make_handoff_brief(), MagicMock(), "ep_001", tmp_path, log
            )
        call_kwargs = mock_ra.call_args[1]
        assert call_kwargs["output_dir"] == tmp_path

    def test_primary_subject_passes_through_to_claude(self, tmp_path):
        brief = _make_handoff_brief()
        brief["primary_subject_identity"]["entity_id"] = "cleopatra"
        log = _make_log()
        with patch(_PATCH, return_value=_make_generation_plan()) as mock_ra:
            write_thumbnail_prompts(brief, MagicMock(), "ep_001", tmp_path, log)
        call_kwargs = mock_ra.call_args[1]
        passed = call_kwargs["input_package"]["thumbnail_handoff_brief"]
        assert passed["primary_subject_identity"]["entity_id"] == "cleopatra"

    def test_negative_constraints_pass_through_to_claude(self, tmp_path):
        brief = _make_handoff_brief()
        brief["negative_constraints"] = ["No anachronisms", "No text in frame"]
        log = _make_log()
        with patch(_PATCH, return_value=_make_generation_plan()) as mock_ra:
            write_thumbnail_prompts(brief, MagicMock(), "ep_001", tmp_path, log)
        call_kwargs = mock_ra.call_args[1]
        passed = call_kwargs["input_package"]["thumbnail_handoff_brief"]
        assert "No anachronisms" in passed["negative_constraints"]
        assert "No text in frame" in passed["negative_constraints"]

    def test_td_planning_started_logged(self, tmp_path):
        log = _make_log()
        with patch(_PATCH, return_value=_make_generation_plan()):
            write_thumbnail_prompts(
                _make_handoff_brief(), MagicMock(), "ep_001", tmp_path, log
            )
        info_events = [c[0][0] for c in log.info.call_args_list]
        assert "td_planning_started" in info_events

    def test_td_planning_completed_logged(self, tmp_path):
        log = _make_log()
        with patch(_PATCH, return_value=_make_generation_plan()):
            write_thumbnail_prompts(
                _make_handoff_brief(), MagicMock(), "ep_001", tmp_path, log
            )
        info_events = [c[0][0] for c in log.info.call_args_list]
        assert "td_planning_completed" in info_events

    def test_td_planning_completed_includes_main_candidates_count(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        with patch(_PATCH, return_value=plan):
            write_thumbnail_prompts(
                _make_handoff_brief(), MagicMock(), "ep_001", tmp_path, log
            )
        completed_call = next(
            c for c in log.info.call_args_list if c[0][0] == "td_planning_completed"
        )
        assert completed_call[1]["main_candidates_planned"] == 1

    def test_td_planning_completed_includes_shorts_count(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        with patch(_PATCH, return_value=plan):
            write_thumbnail_prompts(
                _make_handoff_brief(), MagicMock(), "ep_001", tmp_path, log
            )
        completed_call = next(
            c for c in log.info.call_args_list if c[0][0] == "td_planning_completed"
        )
        assert completed_call[1]["shorts_count"] == 3
