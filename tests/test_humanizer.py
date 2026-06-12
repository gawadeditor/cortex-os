"""
tests/test_humanizer.py — Unit tests for hypogeum/humanizer.py

Verifies:
  TestP14EmDash         — em dash and en dash replacements (HARD CONSTRAINT)
  TestP01Inflation      — significance-inflation phrase removal
  TestP07Vocabulary     — overused AI vocabulary replacement
  TestP20Discourse      — chatbot discourse marker removal
  TestP23Filler         — filler phrase simplification
  TestP24Hedging        — excessive hedging reduction
  TestIdempotent        — calling humanize_field twice = same as once
  TestEdgeCases         — empty string, whitespace-only, no-match text unchanged
  TestHumanizeBeats     — beat list walked; text fields modified; structure intact
  TestJSONStructureUnchanged — script_package-like dicts pass through with structure unchanged
  TestMetadataIntegration   — metadata.py functions apply humanization at correct points
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.humanizer import humanize_beats, humanize_field
from hypogeum.publishing.metadata import (
    build_facebook_description,
    build_youtube_description,
    get_episode_title,
    get_short_title,
)


# ---------------------------------------------------------------------------
# P14 — Em dash and en dash
# ---------------------------------------------------------------------------

class TestP14EmDash:
    def test_em_dash_between_words_becomes_comma(self):
        result = humanize_field("the discovery\u2014which changed history\u2014was pivotal")
        assert "\u2014" not in result
        assert "," in result

    def test_em_dash_with_spaces_removed(self):
        result = humanize_field("Rome fell \u2014 and the world changed")
        assert "\u2014" not in result

    def test_en_dash_replaced(self):
        result = humanize_field("pages 10\u201320 of the manuscript")
        assert "\u2013" not in result

    def test_no_dashes_text_unchanged_structurally(self):
        text = "The aqueduct supplied water to the city for centuries."
        assert humanize_field(text) == text

    def test_em_dash_in_title(self):
        result = humanize_field("Rome\u2014The Eternal City", "title")
        assert "\u2014" not in result

    def test_multiple_em_dashes(self):
        result = humanize_field("One\u2014two\u2014three\u2014four")
        assert "\u2014" not in result


# ---------------------------------------------------------------------------
# P01 — Significance inflation
# ---------------------------------------------------------------------------

class TestP01Inflation:
    def test_stands_as_testament(self):
        result = humanize_field("This artifact stands as a testament to Roman engineering.")
        assert "stands as a testament to" not in result
        assert "shows" in result

    def test_is_a_testament(self):
        result = humanize_field("The Colosseum is a testament to their ambition.")
        assert "is a testament to" not in result
        assert "shows" in result

    def test_pivotal_moment(self):
        result = humanize_field("It was a pivotal moment in history.")
        assert "pivotal moment" not in result
        assert "moment" in result

    def test_testifies_to(self):
        result = humanize_field("The evidence testifies to a forgotten civilization.")
        assert "testifies to" not in result
        assert "shows" in result

    def test_case_insensitive(self):
        result = humanize_field("This Stands As A Testament To their genius.")
        assert "Testament To" not in result


# ---------------------------------------------------------------------------
# P07 — AI vocabulary
# ---------------------------------------------------------------------------

class TestP07Vocabulary:
    def test_delve_into(self):
        result = humanize_field("We delve into the history of ancient Rome.")
        assert "delve into" not in result
        assert "explore" in result

    def test_showcase(self):
        result = humanize_field("This documentary will showcase the findings.")
        assert "showcase" not in result
        assert "show" in result

    def test_seamlessly(self):
        result = humanize_field("The two techniques seamlessly combine.")
        assert "seamlessly" not in result

    def test_intricate(self):
        result = humanize_field("The intricate carvings tell a story.")
        assert "intricate" not in result
        assert "complex" in result

    def test_meticulous(self):
        result = humanize_field("Meticulous excavation revealed the truth.")
        assert "meticulous" not in result.lower()

    def test_leverage(self):
        result = humanize_field("They leverage ancient techniques.")
        assert "leverage" not in result
        assert "use" in result


# ---------------------------------------------------------------------------
# P20 — Chatbot discourse markers
# ---------------------------------------------------------------------------

class TestP20Discourse:
    def test_certainly(self):
        result = humanize_field("Certainly! The evidence is clear.")
        assert result.startswith("The evidence")

    def test_i_hope_this_helps(self):
        result = humanize_field("This was a fascinating discovery. I hope this helps!")
        assert "I hope this helps" not in result

    def test_it_is_worth_noting(self):
        result = humanize_field("It is worth noting that the ruins date to 200 BC.")
        assert "It is worth noting that" not in result
        assert "the ruins date to 200 BC" in result

    def test_its_worth_noting(self):
        result = humanize_field("It's worth noting that this was unusual.")
        assert "worth noting that" not in result

    def test_mid_sentence_word_not_stripped(self):
        # ordinary words mid-sentence are not stripped
        result = humanize_field("We are confident the date is correct.")
        assert "We are confident" in result


# ---------------------------------------------------------------------------
# P23 — Filler phrases
# ---------------------------------------------------------------------------

class TestP23Filler:
    def test_in_order_to_capitalised(self):
        result = humanize_field("In order to understand, we must look at the evidence.")
        assert "in order to" not in result.lower()
        assert "understand" in result

    def test_in_order_to_lowercase(self):
        result = humanize_field("They dug deeper in order to find the chamber.")
        assert "in order to" not in result
        assert "find" in result

    def test_due_to_the_fact_that(self):
        result = humanize_field("Due to the fact that the walls collapsed, entry was impossible.")
        assert "due to the fact that" not in result.lower()
        assert "because" in result.lower()

    def test_prior_to(self):
        result = humanize_field("Prior to the excavation, nothing was known.")
        assert "Prior to" not in result
        assert "Before" in result or "before" in result

    def test_subsequent_to(self):
        result = humanize_field("Subsequent to the discovery, funding arrived.")
        assert "Subsequent to" not in result


# ---------------------------------------------------------------------------
# P24 — Excessive hedging
# ---------------------------------------------------------------------------

class TestP24Hedging:
    def test_could_potentially_possibly(self):
        result = humanize_field("This could potentially possibly explain the mystery.")
        assert "potentially possibly" not in result

    def test_it_could_be_argued_that(self):
        result = humanize_field("It could be argued that the Romans knew more.")
        assert "it could be argued that" not in result.lower()

    def test_one_might_suggest_that(self):
        result = humanize_field("One might suggest that trade routes existed here.")
        assert "one might suggest that" not in result.lower()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotent:
    def test_double_call_same_result(self):
        text = "This intricate artifact delve into the pivotal moment of Roman history."
        once = humanize_field(text)
        twice = humanize_field(once)
        assert once == twice

    def test_clean_text_idempotent(self):
        text = "Historians found evidence of a trade network spanning three continents."
        assert humanize_field(text) == text


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_string(self):
        assert humanize_field("") == ""

    def test_whitespace_only(self):
        assert humanize_field("   ") == "   "

    def test_no_patterns_match_text_unchanged(self):
        text = "Historians discovered a 2,000-year-old aqueduct beneath the forum."
        assert humanize_field(text) == text

    def test_field_name_does_not_change_result(self):
        text = "delve into the evidence"
        assert humanize_field(text, "narration") == humanize_field(text, "title")
        assert humanize_field(text, "cta") == humanize_field(text, "description")

    def test_none_not_passed(self):
        # humanize_field expects str — test that empty string returns correctly
        assert humanize_field("") == ""


# ---------------------------------------------------------------------------
# humanize_beats
# ---------------------------------------------------------------------------

class TestHumanizeBeats:
    def test_modifies_text_in_place(self):
        beats = [
            {"beat_id": "b_001", "text": "We delve into ancient Rome.", "beat_type": "narration"},
            {"beat_id": "b_002", "text": "A pivotal moment in history.", "beat_type": "reveal"},
        ]
        result = humanize_beats(beats)
        assert "delve into" not in result[0]["text"]
        assert "pivotal moment" not in result[1]["text"]

    def test_cta_field_name_used(self):
        beats = [
            {"beat_id": "b_003", "text": "Certainly! Subscribe now.", "beat_type": "cta"},
        ]
        result = humanize_beats(beats)
        assert "Certainly" not in result[0]["text"]

    def test_returns_same_list_object(self):
        beats = [{"beat_id": "b_001", "text": "clean text", "beat_type": "narration"}]
        result = humanize_beats(beats)
        assert result is beats

    def test_non_dict_entries_skipped(self):
        beats = ["not_a_dict", {"beat_id": "b_001", "text": "text", "beat_type": "narration"}]
        result = humanize_beats(beats)
        assert result[0] == "not_a_dict"

    def test_entry_without_text_skipped(self):
        beats = [{"beat_id": "b_001", "beat_type": "narration"}]
        result = humanize_beats(beats)
        assert "text" not in result[0]

    def test_non_string_text_skipped(self):
        beats = [{"beat_id": "b_001", "text": 42, "beat_type": "narration"}]
        result = humanize_beats(beats)
        assert result[0]["text"] == 42

    def test_structure_fields_unchanged(self):
        beats = [
            {"beat_id": "b_007", "text": "delve into history", "beat_type": "narration", "extra": "data"},
        ]
        result = humanize_beats(beats)
        assert result[0]["beat_id"] == "b_007"
        assert result[0]["beat_type"] == "narration"
        assert result[0]["extra"] == "data"

    def test_empty_list(self):
        assert humanize_beats([]) == []


# ---------------------------------------------------------------------------
# JSON structure unchanged (script_package-like dict)
# ---------------------------------------------------------------------------

class TestJSONStructureUnchanged:
    def _make_script_package(self) -> dict:
        return {
            "sections": [
                {
                    "section_id": "s_001",
                    "section_title": "The Discovery",
                    "narration_beats": [
                        {"beat_id": "b_001", "text": "Archaeologists delve into this pivotal moment.", "beat_type": "narration"},
                        {"beat_id": "b_002", "text": "Subscribe to showcase your support.", "beat_type": "cta"},
                    ],
                }
            ],
            "shorts_scripts": [
                {
                    "short_id": "sh_001",
                    "narration_beats": [
                        {"beat_id": "b_101", "text": "This intricate artifact\u2014found in 1900\u2014changed history.", "beat_type": "narration"},
                    ],
                }
            ],
            "chapter_markers": [{"title": "Introduction", "start_seconds": 0}],
            "script_integrity": {"cta_count": 1},
        }

    def test_top_level_keys_preserved(self):
        pkg = self._make_script_package()
        original_keys = set(pkg.keys())
        # Simulate what script_engine does post-run_agent
        for section in pkg.get("sections", []):
            humanize_beats(section.get("narration_beats", []))
        for short in pkg.get("shorts_scripts", []):
            humanize_beats(short.get("narration_beats", []))
        assert set(pkg.keys()) == original_keys

    def test_section_structure_preserved(self):
        pkg = self._make_script_package()
        for section in pkg.get("sections", []):
            humanize_beats(section.get("narration_beats", []))
        section = pkg["sections"][0]
        assert section["section_id"] == "s_001"
        assert section["section_title"] == "The Discovery"
        assert len(section["narration_beats"]) == 2

    def test_beat_ids_preserved(self):
        pkg = self._make_script_package()
        for section in pkg.get("sections", []):
            humanize_beats(section.get("narration_beats", []))
        beat_ids = [b["beat_id"] for b in pkg["sections"][0]["narration_beats"]]
        assert beat_ids == ["b_001", "b_002"]

    def test_beat_types_preserved(self):
        pkg = self._make_script_package()
        for section in pkg.get("sections", []):
            humanize_beats(section.get("narration_beats", []))
        types = [b["beat_type"] for b in pkg["sections"][0]["narration_beats"]]
        assert types == ["narration", "cta"]

    def test_chapter_markers_untouched(self):
        pkg = self._make_script_package()
        before = copy.deepcopy(pkg["chapter_markers"])
        for section in pkg.get("sections", []):
            humanize_beats(section.get("narration_beats", []))
        assert pkg["chapter_markers"] == before

    def test_script_integrity_untouched(self):
        pkg = self._make_script_package()
        before = copy.deepcopy(pkg["script_integrity"])
        for section in pkg.get("sections", []):
            humanize_beats(section.get("narration_beats", []))
        assert pkg["script_integrity"] == before

    def test_em_dash_removed_from_short_beat(self):
        pkg = self._make_script_package()
        for short in pkg.get("shorts_scripts", []):
            humanize_beats(short.get("narration_beats", []))
        text = pkg["shorts_scripts"][0]["narration_beats"][0]["text"]
        assert "\u2014" not in text

    def test_text_changed_not_structure(self):
        pkg = self._make_script_package()
        original_text = pkg["sections"][0]["narration_beats"][0]["text"]
        for section in pkg.get("sections", []):
            humanize_beats(section.get("narration_beats", []))
        new_text = pkg["sections"][0]["narration_beats"][0]["text"]
        # Text was humanized (delve into → explore, pivotal moment → moment)
        assert new_text != original_text
        # But beat structure is intact
        assert "beat_id" in pkg["sections"][0]["narration_beats"][0]


# ---------------------------------------------------------------------------
# Metadata integration
# ---------------------------------------------------------------------------

class TestMetadataIntegration:
    def _ms(self, **overrides) -> dict:
        base = {
            "episode_title": "The Lost City",
            "logline": "Historians delve into evidence for Atlantis.",
            "section_titles": [],
            "sources": [],
            "shorts_titles": {"sh_001": "The Pivotal Moment Revealed"},
            "ai_disclosure_text": "This video was produced with AI assistance.",
        }
        base.update(overrides)
        return base

    def test_get_episode_title_humanized(self):
        ms = self._ms(episode_title="The Intricate History of Rome")
        title = get_episode_title(ms)
        assert "intricate" not in title.lower()
        assert "complex" in title.lower() or "History of Rome" in title

    def test_get_short_title_humanized_dict(self):
        ms = self._ms(shorts_titles={"sh_001": "Delve Into the Mystery"})
        title = get_short_title(ms, "sh_001")
        assert "delve" not in title.lower()
        assert "explore" in title.lower()

    def test_get_short_title_humanized_list(self):
        ms = self._ms(shorts_titles=[{"short_id": "sh_001", "title": "Delve Into the Mystery"}])
        title = get_short_title(ms, "sh_001")
        assert "delve" not in title.lower()

    def test_youtube_description_logline_humanized(self):
        ms = self._ms(logline="Historians delve into the pivotal moment of Roman history.")
        desc = build_youtube_description(ms)
        assert "delve into" not in desc
        assert "pivotal moment" not in desc

    def test_youtube_description_still_contains_disclosure(self):
        ms = self._ms()
        desc = build_youtube_description(ms)
        assert "AI assistance" in desc

    def test_facebook_description_logline_humanized(self):
        ms = self._ms(logline="We delve into intricate evidence for this discovery.")
        desc = build_facebook_description(ms)
        assert "delve into" not in desc
        assert "intricate" not in desc

    def test_ai_disclosure_em_dash_cleaned(self):
        ms = self._ms(ai_disclosure_text="AI\u2014assisted production. All facts verified.")
        desc = build_youtube_description(ms)
        assert "\u2014" not in desc

    def test_missing_short_title_still_empty(self):
        ms = self._ms(shorts_titles={})
        assert get_short_title(ms, "sh_999") == ""

    def test_episode_title_empty_still_empty(self):
        ms = self._ms(episode_title="")
        assert get_episode_title(ms) == ""
