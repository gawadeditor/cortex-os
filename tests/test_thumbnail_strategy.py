"""
tests/test_thumbnail_strategy.py — Tests for Thumbnail Strategy Skill (Phase 1)

Coverage:
  TestThumbnailConceptBriefSchema  — schema validator unit tests
    test_valid_brief_passes
    test_rejects_2_concepts
    test_rejects_4_concepts
    test_rejects_repeated_emotional_trigger
    test_rejects_missing_suggested_hook_text
    test_rejects_hook_text_longer_than_5_words
    test_rejects_hook_text_shorter_than_3_words
    test_rejects_duplicate_concept_id
    test_rejects_invalid_emotional_trigger
    test_rejects_missing_central_viewer_question
    test_rejects_missing_concept_name
    test_rejects_missing_viewer_promise
    test_rejects_missing_curiosity_gap
    test_rejects_missing_visual_thesis
    test_all_permitted_triggers_accepted

  TestThumbnailStrategyBriefBuilder — unit tests for _build_strategy_brief()
    test_extracts_episode_title
    test_extracts_hook_language_from_hook_section
    test_extracts_central_mystery
    test_extracts_viewer_entry_point
    test_truncates_hook_language_at_600_chars
    test_handles_missing_hook_section_gracefully
    test_handles_missing_fields_gracefully

  TestThumbnailStrategySkill — integration: run_agent mocked
    test_returns_concept_brief_dict
    test_passes_correct_schema_to_run_agent
    test_passes_correct_agent_name
    test_concept_brief_written_to_output_dir
"""

from __future__ import annotations

import os
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.schemas import SchemaError, validate
from hypogeum.skills.thumbnail_strategy import _build_strategy_brief, run_thumbnail_strategy


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_valid_concept(
    concept_id: str = "tsc_ep_001_A",
    trigger: str = "impossibility",
    hook: str = "IT SHOULDN'T EXIST",
) -> dict:
    return {
        "concept_id":          concept_id,
        "concept_name":        "The Impossible Machine",
        "viewer_promise":      "This machine should not exist, and we have no idea how it was built.",
        "curiosity_gap":       "A 2,000-year-old computer with no prototype, no predecessor, no explanation.",
        "emotional_trigger":   trigger,
        "visual_thesis":       "An artifact of impossible precision isolated in darkness — its complexity visible but its origin unknowable.",
        "suggested_hook_text": hook,
    }


def _make_valid_brief() -> dict:
    return {
        "thumbnail_concept_brief_id": "tcb_ep_001",
        "episode_id":                 "ep_001",
        "central_viewer_question":    "How did the ancient world build this — and why did the knowledge vanish?",
        "concepts": [
            _make_valid_concept("tsc_ep_001_A", "impossibility", "IT SHOULDN'T EXIST"),
            _make_valid_concept("tsc_ep_001_B", "loss",          "THE KNOWLEDGE DIED"),
            _make_valid_concept("tsc_ep_001_C", "betrayal",      "WHERE WAS IT GOING"),
        ],
    }


def _make_topic_brief() -> dict:
    return {
        "topic_id":                  "antikythera_mechanism",
        "episode_title":             "The Antikythera Mechanism: Who Taught the Greeks the Sky?",
        "topic_category":            "ancient_mystery",
        "curiosity_hook":            "Where did the knowledge come from — and why did it vanish?",
        "mystery_anchor":            "No precursor has ever been found anywhere on Earth.",
        "audience_demand_signal":    "Top-tier search volume; 50M views on reconstruction videos.",
        "differentiation_hypothesis": "Focus on the lost knowledge tradition, not the device itself.",
    }


def _make_script_package() -> dict:
    return {
        "episode_metadata": {"topic": "The Antikythera Mechanism", "episode_id": "ep_001"},
        "sections": [
            {
                "section_id": "s_001",
                "title":      "Hook",
                "narration_beats": [
                    {"beat_id": "b_001", "text": "In 1901, divers pulled something from a shipwreck that had no right to exist."},
                    {"beat_id": "b_002", "text": "A mechanical computer for predicting the movements of the cosmos."},
                    {"beat_id": "b_003", "text": "Nothing like it had ever been found. Nothing like it would be found again."},
                ],
            },
            {
                "section_id": "s_002",
                "title":      "Context",
                "narration_beats": [
                    {"beat_id": "b_004", "text": "What survives today is 82 bronze fragments."},
                ],
            },
        ],
    }


# ---------------------------------------------------------------------------
# TestThumbnailConceptBriefSchema
# ---------------------------------------------------------------------------

class TestThumbnailConceptBriefSchema:

    def test_valid_brief_passes(self):
        validate("thumbnail_concept_brief", _make_valid_brief())

    def test_rejects_2_concepts(self):
        p = _make_valid_brief()
        p["concepts"] = p["concepts"][:2]
        with pytest.raises(SchemaError, match="exactly 3"):
            validate("thumbnail_concept_brief", p)

    def test_rejects_4_concepts(self):
        p = _make_valid_brief()
        extra = deepcopy(p["concepts"][0])
        extra["concept_id"] = "tsc_ep_001_D"
        extra["emotional_trigger"] = "awe"
        p["concepts"].append(extra)
        with pytest.raises(SchemaError, match="exactly 3"):
            validate("thumbnail_concept_brief", p)

    def test_rejects_repeated_emotional_trigger(self):
        p = _make_valid_brief()
        p["concepts"][1]["emotional_trigger"] = "impossibility"  # same as concept[0]
        with pytest.raises(SchemaError, match="emotional_trigger"):
            validate("thumbnail_concept_brief", p)

    def test_rejects_missing_suggested_hook_text(self):
        p = _make_valid_brief()
        del p["concepts"][0]["suggested_hook_text"]
        with pytest.raises(SchemaError, match="suggested_hook_text"):
            validate("thumbnail_concept_brief", p)

    def test_rejects_hook_text_longer_than_5_words(self):
        p = _make_valid_brief()
        p["concepts"][0]["suggested_hook_text"] = "this hook text is way too long"  # 7 words
        with pytest.raises(SchemaError, match="3–5 words"):
            validate("thumbnail_concept_brief", p)

    def test_rejects_hook_text_shorter_than_3_words(self):
        p = _make_valid_brief()
        p["concepts"][0]["suggested_hook_text"] = "WHY"  # 1 word
        with pytest.raises(SchemaError, match="3–5 words"):
            validate("thumbnail_concept_brief", p)

    def test_rejects_duplicate_concept_id(self):
        p = _make_valid_brief()
        p["concepts"][1]["concept_id"] = p["concepts"][0]["concept_id"]  # same as [0]
        with pytest.raises(SchemaError, match="duplicated"):
            validate("thumbnail_concept_brief", p)

    def test_rejects_invalid_emotional_trigger(self):
        p = _make_valid_brief()
        p["concepts"][0]["emotional_trigger"] = "fear"  # not in permitted set
        with pytest.raises(SchemaError, match="emotional_trigger"):
            validate("thumbnail_concept_brief", p)

    def test_rejects_missing_central_viewer_question(self):
        p = _make_valid_brief()
        del p["central_viewer_question"]
        with pytest.raises(SchemaError):
            validate("thumbnail_concept_brief", p)

    def test_rejects_missing_concept_name(self):
        p = _make_valid_brief()
        del p["concepts"][0]["concept_name"]
        with pytest.raises(SchemaError):
            validate("thumbnail_concept_brief", p)

    def test_rejects_missing_viewer_promise(self):
        p = _make_valid_brief()
        del p["concepts"][0]["viewer_promise"]
        with pytest.raises(SchemaError):
            validate("thumbnail_concept_brief", p)

    def test_rejects_missing_curiosity_gap(self):
        p = _make_valid_brief()
        del p["concepts"][0]["curiosity_gap"]
        with pytest.raises(SchemaError):
            validate("thumbnail_concept_brief", p)

    def test_rejects_missing_visual_thesis(self):
        p = _make_valid_brief()
        del p["concepts"][0]["visual_thesis"]
        with pytest.raises(SchemaError):
            validate("thumbnail_concept_brief", p)

    def test_all_permitted_triggers_accepted(self):
        """Each permitted emotional_trigger value is individually valid."""
        permitted = ["awe", "dread", "impossibility", "betrayal", "discovery", "scale", "loss"]
        # Use the first 3 for a valid brief each time
        for trio in [
            ("awe", "dread", "impossibility"),
            ("betrayal", "discovery", "scale"),
            ("loss", "awe", "betrayal"),
        ]:
            p = _make_valid_brief()
            for i, trigger in enumerate(trio):
                p["concepts"][i]["emotional_trigger"] = trigger
                p["concepts"][i]["concept_id"] = f"tsc_ep_001_{chr(65+i)}_alt"
            validate("thumbnail_concept_brief", p)

    def test_5_word_hook_text_accepted(self):
        """Exactly 5 words is within the 3–5 word limit."""
        p = _make_valid_brief()
        p["concepts"][0]["suggested_hook_text"] = "THE MACHINE THAT TIME FORGOT"  # 5 words
        validate("thumbnail_concept_brief", p)

    def test_3_word_hook_text_accepted(self):
        """Exactly 3 words is within the 3–5 word limit."""
        p = _make_valid_brief()
        p["concepts"][0]["suggested_hook_text"] = "IT SHOULDN'T EXIST"  # 3 words
        validate("thumbnail_concept_brief", p)

    def test_rejects_missing_episode_id(self):
        p = _make_valid_brief()
        del p["episode_id"]
        with pytest.raises(SchemaError):
            validate("thumbnail_concept_brief", p)

    def test_rejects_missing_brief_id(self):
        p = _make_valid_brief()
        del p["thumbnail_concept_brief_id"]
        with pytest.raises(SchemaError):
            validate("thumbnail_concept_brief", p)

    def test_concepts_must_be_list_not_dict(self):
        p = _make_valid_brief()
        p["concepts"] = {"A": p["concepts"][0]}
        with pytest.raises(SchemaError):
            validate("thumbnail_concept_brief", p)

    def test_schema_name_registered(self):
        """thumbnail_concept_brief is registered in the schema registry."""
        from hypogeum.schemas import SCHEMA_NAMES
        assert "thumbnail_concept_brief" in SCHEMA_NAMES


# ---------------------------------------------------------------------------
# TestThumbnailStrategyBriefBuilder
# ---------------------------------------------------------------------------

class TestThumbnailStrategyBriefBuilder:

    def test_extracts_episode_title(self):
        brief = _build_strategy_brief(_make_topic_brief(), _make_script_package())
        assert brief["episode_title"] == "The Antikythera Mechanism: Who Taught the Greeks the Sky?"

    def test_extracts_hook_language_from_hook_section(self):
        brief = _build_strategy_brief(_make_topic_brief(), _make_script_package())
        assert "no right to exist" in brief["hook_language"]
        assert "mechanical computer" in brief["hook_language"]

    def test_extracts_central_mystery(self):
        brief = _build_strategy_brief(_make_topic_brief(), _make_script_package())
        assert brief["central_mystery"] == "No precursor has ever been found anywhere on Earth."

    def test_extracts_viewer_entry_point(self):
        brief = _build_strategy_brief(_make_topic_brief(), _make_script_package())
        assert brief["viewer_entry_point"] == "Where did the knowledge come from — and why did it vanish?"

    def test_truncates_hook_language_at_600_chars(self):
        topic = _make_topic_brief()
        script = _make_script_package()
        # Inject a very long beat
        script["sections"][0]["narration_beats"][0]["text"] = "x" * 1000
        brief = _build_strategy_brief(topic, script)
        assert len(brief["hook_language"]) <= 600

    def test_handles_missing_hook_section_gracefully(self):
        topic = _make_topic_brief()
        script = {"sections": [{"section_id": "s_001", "title": "Context", "narration_beats": []}]}
        brief = _build_strategy_brief(topic, script)
        assert brief["hook_language"] == ""

    def test_handles_missing_fields_gracefully(self):
        brief = _build_strategy_brief({}, {})
        assert isinstance(brief, dict)
        assert brief["episode_title"] == ""
        assert brief["central_mystery"] == ""

    def test_audience_profile_includes_category(self):
        brief = _build_strategy_brief(_make_topic_brief(), _make_script_package())
        assert "ancient_mystery" in brief["audience_profile"]

    def test_uses_topic_id_as_episode_id_fallback(self):
        topic = _make_topic_brief()  # has topic_id but no episode_id
        brief = _build_strategy_brief(topic, _make_script_package())
        assert brief["episode_id"] == "antikythera_mechanism"

    def test_does_not_include_full_script_in_output(self):
        """The brief must stay compact — not a raw dump of the script."""
        brief = _build_strategy_brief(_make_topic_brief(), _make_script_package())
        # hook_language is capped at 600 chars
        assert len(brief["hook_language"]) <= 600
        # Should not have full sections list
        assert "sections" not in brief


# ---------------------------------------------------------------------------
# TestThumbnailStrategySkill
# ---------------------------------------------------------------------------

def _make_mock_config():
    cfg = MagicMock()
    cfg.OPENROUTER_SONNET_MODEL_ID = "anthropic/claude-sonnet-4.6"
    return cfg


class TestThumbnailStrategySkill:

    def test_returns_concept_brief_dict(self, tmp_path):
        config = _make_mock_config()
        expected = _make_valid_brief()
        expected["episode_id"] = "ep_001"
        expected["thumbnail_concept_brief_id"] = "tcb_ep_001"

        with patch("hypogeum.skills.thumbnail_strategy.run_agent", return_value=expected):
            result = run_thumbnail_strategy(
                topic_brief=_make_topic_brief(),
                script_package=_make_script_package(),
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        assert isinstance(result, dict)
        assert result["thumbnail_concept_brief_id"] == "tcb_ep_001"
        assert len(result["concepts"]) == 3

    def test_passes_correct_schema_to_run_agent(self, tmp_path):
        config = _make_mock_config()

        with patch("hypogeum.skills.thumbnail_strategy.run_agent",
                   return_value=_make_valid_brief()) as mock_run:
            run_thumbnail_strategy(
                topic_brief=_make_topic_brief(),
                script_package=_make_script_package(),
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["output_schema"] == "thumbnail_concept_brief"

    def test_passes_correct_agent_name(self, tmp_path):
        config = _make_mock_config()

        with patch("hypogeum.skills.thumbnail_strategy.run_agent",
                   return_value=_make_valid_brief()) as mock_run:
            run_thumbnail_strategy(
                topic_brief=_make_topic_brief(),
                script_package=_make_script_package(),
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["agent_name"] == "thumbnail_strategy"

    def test_task_key_is_generate_thumbnail_concepts(self, tmp_path):
        config = _make_mock_config()

        with patch("hypogeum.skills.thumbnail_strategy.run_agent",
                   return_value=_make_valid_brief()) as mock_run:
            run_thumbnail_strategy(
                topic_brief=_make_topic_brief(),
                script_package=_make_script_package(),
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["input_package"]["task"] == "generate_thumbnail_concepts"

    def test_strategy_brief_in_input_package(self, tmp_path):
        config = _make_mock_config()

        with patch("hypogeum.skills.thumbnail_strategy.run_agent",
                   return_value=_make_valid_brief()) as mock_run:
            run_thumbnail_strategy(
                topic_brief=_make_topic_brief(),
                script_package=_make_script_package(),
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        pkg = mock_run.call_args[1]["input_package"]
        assert "thumbnail_strategy_brief" in pkg
        brief = pkg["thumbnail_strategy_brief"]
        assert "episode_title" in brief
        assert "central_mystery" in brief
        assert "viewer_entry_point" in brief

    def test_uses_sonnet_model_from_config(self, tmp_path):
        config = _make_mock_config()
        config.OPENROUTER_SONNET_MODEL_ID = "anthropic/claude-sonnet-4.6"

        with patch("hypogeum.skills.thumbnail_strategy.run_agent",
                   return_value=_make_valid_brief()) as mock_run:
            run_thumbnail_strategy(
                topic_brief=_make_topic_brief(),
                script_package=_make_script_package(),
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
            )

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["model_slug"] == "anthropic/claude-sonnet-4.6"
