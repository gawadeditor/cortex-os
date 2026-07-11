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

from hypogeum.agents.thumbnail_prompt_writer import (
    PINNED_GPT_IMAGE2_SHA,
    SYSTEM_PROMPT,
    _GPT_IMAGE2_SKILL_PATH,
    _PROMPT_FILE,
    _THUMBNAIL_INTELLIGENCE_SKILL_FILE,
    write_thumbnail_prompts,
)


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


# ---------------------------------------------------------------------------
# TestSystemPrompt — GPT-Image2 skill composition
#
# Mirrors tests/test_script_engine.py::TestSystemPrompt, the equivalent
# coverage already proven for the Humanizer/Script Engine attachment.
# ---------------------------------------------------------------------------

class TestSystemPrompt:
    def test_system_prompt_contains_base_prompt_file(self):
        from hypogeum.prompt_loader import load_prompt
        assert load_prompt(_PROMPT_FILE) in SYSTEM_PROMPT

    def test_system_prompt_contains_internal_thumbnail_intelligence_skill(self):
        from hypogeum.prompt_loader import load_skill
        internal_skill = load_skill(_THUMBNAIL_INTELLIGENCE_SKILL_FILE)
        assert internal_skill
        assert internal_skill in SYSTEM_PROMPT

    def test_pinned_gpt_image2_skill_path_exists(self):
        assert _GPT_IMAGE2_SKILL_PATH.exists()
        assert PINNED_GPT_IMAGE2_SHA in str(_GPT_IMAGE2_SKILL_PATH)
        assert len(PINNED_GPT_IMAGE2_SHA) == 40

    def test_pinned_gpt_image2_skill_loaded_verbatim(self):
        """The composed system prompt must contain the pinned SKILL.md's
        exact on-disk bytes — no paraphrase, no summarization, no rewrite."""
        on_disk = _GPT_IMAGE2_SKILL_PATH.read_text(encoding="utf-8")
        assert on_disk in SYSTEM_PROMPT

    def test_system_prompt_composed_via_compose_system_prompt(self):
        from hypogeum.prompt_loader import compose_system_prompt
        assert SYSTEM_PROMPT == compose_system_prompt(
            _PROMPT_FILE, _THUMBNAIL_INTELLIGENCE_SKILL_FILE, _GPT_IMAGE2_SKILL_PATH,
        )

    def test_composition_order_is_base_then_internal_then_gpt_image2(self):
        from hypogeum.prompt_loader import load_prompt, load_skill
        base = load_prompt(_PROMPT_FILE)
        internal_skill = load_skill(_THUMBNAIL_INTELLIGENCE_SKILL_FILE)
        gpt_image2_skill = _GPT_IMAGE2_SKILL_PATH.read_text(encoding="utf-8")

        idx_base = SYSTEM_PROMPT.find(base)
        idx_internal = SYSTEM_PROMPT.find(internal_skill)
        idx_gpt_image2 = SYSTEM_PROMPT.find(gpt_image2_skill)

        assert idx_base == 0
        assert idx_base < idx_internal < idx_gpt_image2

    def test_base_prompt_does_not_duplicate_gpt_image2_distinctive_heading(self):
        """A representative, distinctive upstream heading must not be
        duplicated inside thumbnail_director.txt's own authored content —
        it must come from the composed skill file only."""
        from hypogeum.prompt_loader import load_prompt
        base_only = load_prompt(_PROMPT_FILE)
        skill_text = _GPT_IMAGE2_SKILL_PATH.read_text(encoding="utf-8")
        assert "## Scale references" not in base_only
        assert "## Scale references" in skill_text

    def test_bridge_section_present_in_base_prompt(self):
        from hypogeum.prompt_loader import load_prompt
        base_only = load_prompt(_PROMPT_FILE)
        assert "SECTION 12B" in base_only
        assert "GPT-IMAGE2" in base_only.upper()

    def test_bridge_disables_cli_execution(self):
        from hypogeum.prompt_loader import load_prompt
        base_only = load_prompt(_PROMPT_FILE)
        assert "generate.py" in base_only
        assert "uvx gpt-image" in base_only or "gpt-image" in base_only

    def test_bridge_disables_direct_provider_api_calls(self):
        from hypogeum.prompt_loader import load_prompt
        base_only = load_prompt(_PROMPT_FILE)
        assert "OpenAI" in base_only
        assert "do not call" in base_only.lower()

    def test_bridge_disables_env_key_access_and_file_writing(self):
        from hypogeum.prompt_loader import load_prompt
        base_only = load_prompt(_PROMPT_FILE)
        assert "OPENAI_API_KEY" in base_only
        assert "write or download files" in base_only.lower()

    def test_bridge_disables_interactive_questions_and_upstream_output_format(self):
        from hypogeum.prompt_loader import load_prompt
        base_only = load_prompt(_PROMPT_FILE)
        assert "ask the user questions" in base_only.lower()
        assert "standalone response format" in base_only.lower()

    def test_bridge_preserves_existing_json_contract(self):
        from hypogeum.prompt_loader import load_prompt
        base_only = load_prompt(_PROMPT_FILE)
        assert "SECTION 13" in base_only
        assert "thumbnail JSON contract" in base_only

    def test_bridge_limits_effect_to_image_prompt_field(self):
        from hypogeum.prompt_loader import load_prompt
        base_only = load_prompt(_PROMPT_FILE)
        assert "image_prompt" in base_only
        assert "does not generate the image" in base_only
        assert "does not control provider routing" in base_only

    def test_bridge_states_existing_requirements_take_precedence(self):
        from hypogeum.prompt_loader import load_prompt
        base_only = load_prompt(_PROMPT_FILE)
        assert "takes precedence" in base_only

    def test_module_contains_exactly_one_run_agent_call(self):
        import ast
        import inspect

        import hypogeum.agents.thumbnail_prompt_writer as mod
        tree = ast.parse(inspect.getsource(mod))
        call_names = [
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        assert call_names.count("run_agent") == 1

    def test_module_has_no_new_external_call_surface(self):
        import inspect

        import hypogeum.agents.thumbnail_prompt_writer as mod
        source = inspect.getsource(mod)
        for forbidden in ("OpenAI(", "requests.", "httpx.", "subprocess.", "call_claude("):
            assert forbidden not in source, f"found forbidden call surface: {forbidden}"

    def test_system_prompt_passed_to_run_agent(self):
        with patch(_PATCH, return_value=_make_generation_plan()) as mock_ra:
            write_thumbnail_prompts(
                _make_handoff_brief(), MagicMock(), "ep_001", Path("/tmp"), _make_log(),
            )
        assert mock_ra.call_args[1]["system_prompt"] == SYSTEM_PROMPT
