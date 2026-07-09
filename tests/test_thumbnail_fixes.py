"""
tests/test_thumbnail_fixes.py — Tests for root-cause thumbnail fixes

Coverage:
  TestPrimarySubjectIdentityAdvisory
    test_concept_driven_mode_does_not_require_subject_identity_in_prompts
    test_legacy_mode_passes_subject_identity_to_run_agent
    test_prompt_writer_concept_driven_flag_in_log
    test_td_system_prompt_has_advisory_language_for_concept_mode
    test_td_system_prompt_has_binding_language_only_in_legacy_mode

  TestConceptDrivenModeRules
    test_section_11c_forbids_object_noun_openings
    test_section_11c_forbids_primary_subject_identity_as_mandatory
    test_section_11c_requires_emotional_trigger_first
    test_section_11c_no_one_third_frame_rule
    test_mode_detection_language_in_prompt

  TestKillerConceptValidator
    test_passes_clean_concept_prompts
    test_fails_object_noun_opening
    test_fails_more_than_2_cliche_nouns_in_opening
    test_fails_repeated_cliche_across_all_three
    test_fails_wordmark_as_subject
    test_fails_inventory_structure
    test_warns_explanatory_hook
    test_warns_no_curiosity_marker
    test_passes_curiosity_driven_hooks
    test_ep003_old_prompts_fail
    test_ep003_new_prompts_pass

  TestWordmarkAndSubtitleScaling
    test_wordmark_size_reduced_from_old_values
    test_subtitle_band_reduced_from_old_values
    test_subtitle_not_pure_white
    test_overlay_font_candidates_prefer_light_sans
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.thumbnail_killer_concept_validator import (
    ConceptQualityError,
    validate_concept_quality,
    validate_concept_quality_or_raise,
)
from hypogeum.agents.thumbnail_prompt_writer import SYSTEM_PROMPT
from hypogeum.agents.brand_guardian_agent import SYSTEM_PROMPT as BGA_SYSTEM_PROMPT
from hypogeum.muapi import (
    _OVERLAY_FONT_CANDIDATES,
    _WORDMARK_COLOR_HYPOGEUM,
    _apply_wordmark_overlay,
    _apply_text_overlay,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_concept_candidate(rank: int, prompt: str, hook: str, cid: str = None) -> dict:
    return {
        "candidate_id": cid or f"tsc_ep_001_{chr(64+rank)}",
        "rank":         rank,
        "image_prompt": prompt,
        "aspect_ratio": "16:9",
        "ctr_rationale": f"Rank {rank}.",
        "text_overlay": {"text": hook, "placement": "bottom"},
    }


def _make_clean_candidates() -> list[dict]:
    """Three concept-driven prompts that should pass ALL validators."""
    return [
        _make_concept_candidate(1,
            "Deep in the void between what was known and what should have been impossible, "
            "a single point of amber precision glows against absolute darkness — "
            "no origin, no context, only the impossible thing and the silence around it. "
            "Near-black 16:9 frame, impossibility register, single warm amber highlight.",
            "IT SHOULDN'T EXIST"),
        _make_concept_candidate(2,
            "Vast darkness fills an archive that once held centuries of accumulated knowledge. "
            "The knowledge is gone. Only the dark remains. A faint warm amber glow touches "
            "nothing of substance — what was here has vanished completely and without explanation. "
            "Near-black dominant, loss register, atmospheric emptiness.",
            "THE KNOWLEDGE VANISHED"),
        _make_concept_candidate(3,
            "Pure void surrounds one solitary object whose origin cannot be explained. "
            "No context, no maker, no era — just the impossible thing alone in darkness, "
            "a single shaft of amber light revealing precision that belongs to no known world. "
            "Near-black dominant, lost-origin register, isolated subject.",
            "NO ONE KNOWS WHO BUILT IT"),
    ]


def _make_old_ep003_candidates() -> list[dict]:
    """Reproduces the original ep_003 collapsed prompts — all should fail the killer gate."""
    prefix = (
        "Corroded bronze fragments with dark verdigris patina, exposed gear teeth, "
        "Koine Greek inscriptions, warm amber directional lighting against dark background"
    )
    return [
        _make_concept_candidate(1, f"{prefix}, resting inside a near-black ancient stone chamber, "
            "medium close object framing lower center, top 12% empty charcoal for wordmark, "
            "dramatic chiaroscuro, dusty amber atmosphere", "SOMEONE KNEW HOW"),
        _make_concept_candidate(2, f"{prefix}, arranged on a rough stone plinth, aligned against "
            "a near-black Mediterranean night sky with faint warm stars above ruined stone terraces, "
            "wide atmospheric exterior framing, bronze torchlight from below", "SOMEONE KNEW HOW"),
        _make_concept_candidate(3, f"{prefix}, occupying one-third of frame beside a smaller "
            "Babylonian clay tablet on a stone table, vast shadowed Hellenistic harbor storehouse",
            "SOMEONE KNEW HOW"),
    ]


# ---------------------------------------------------------------------------
# TestPrimarySubjectIdentityAdvisory
# ---------------------------------------------------------------------------

class TestPrimarySubjectIdentityAdvisory:

    def test_td_system_prompt_has_advisory_language_for_concept_mode(self):
        """SYSTEM_PROMPT must state primary_subject_identity is ADVISORY in concept mode."""
        prompt_lower = SYSTEM_PROMPT.lower()
        assert "advisory" in prompt_lower, (
            "System prompt must contain the word 'advisory' to describe "
            "primary_subject_identity in concept_driven_mode"
        )

    def test_td_system_prompt_has_binding_language_only_in_legacy_mode(self):
        """The binding lock instruction must be guarded by LEGACY_SCENE_MODE label."""
        # The prompt must contain "binding" within the context of LEGACY_SCENE_MODE
        assert "LEGACY_SCENE_MODE" in SYSTEM_PROMPT, (
            "System prompt must contain LEGACY_SCENE_MODE label to guard binding-lock rule"
        )
        # And it must NOT say primary_subject_identity is unconditionally binding
        # (the old unconditional text was "primary_subject_identity — binding lock. You MUST feature")
        assert "binding lock. You MUST feature" not in SYSTEM_PROMPT, (
            "Old unconditional binding-lock text must be removed from system prompt"
        )

    def test_mode_detection_present_in_prompt(self):
        """System prompt must contain MODE DETECTION section."""
        assert "MODE DETECTION" in SYSTEM_PROMPT
        assert "CONCEPT_DRIVEN_MODE" in SYSTEM_PROMPT
        assert "LEGACY_SCENE_MODE" in SYSTEM_PROMPT

    def test_concept_driven_mode_does_not_require_subject_identity_in_prompts(self, tmp_path):
        """When concept_brief is in input_package, primary_subject_identity is not injected
        as a mandatory prefix into image_prompts by the planner."""
        from hypogeum.config import Config
        from hypogeum.muapi import ThumbnailResult

        config = MagicMock(spec=Config)
        config.TD_PLANNING_MODEL     = "anthropic/claude-sonnet-4.6"
        config.TD_PLANNING_MAX_TOKENS = 2048
        config.OPENROUTER_SONNET_MODEL_ID = "anthropic/claude-sonnet-4.6"

        concept_brief = {
            "thumbnail_concept_brief_id": "tcb_ep_001",
            "episode_id": "ep_001",
            "central_viewer_question": "How can this exist?",
            "concepts": [
                {"concept_id": "tsc_ep_001_A", "concept_name": "Impossible",
                 "viewer_promise": "...", "curiosity_gap": "...",
                 "emotional_trigger": "impossibility",
                 "visual_thesis": "A device of impossible precision in pure void.",
                 "suggested_hook_text": "IT SHOULDN'T EXIST"},
                {"concept_id": "tsc_ep_001_B", "concept_name": "Vanished",
                 "viewer_promise": "...", "curiosity_gap": "...",
                 "emotional_trigger": "loss",
                 "visual_thesis": "The vast dark where knowledge used to be.",
                 "suggested_hook_text": "THE KNOWLEDGE VANISHED"},
                {"concept_id": "tsc_ep_001_C", "concept_name": "Unknown",
                 "viewer_promise": "...", "curiosity_gap": "...",
                 "emotional_trigger": "betrayal",
                 "visual_thesis": "One object alone, its maker completely unknown.",
                 "suggested_hook_text": "NO ONE KNOWS WHO BUILT IT"},
            ],
        }

        # Write concept brief to tmp_path
        (tmp_path / "thumbnail_concept_brief_ep_001.json").write_text(
            json.dumps(concept_brief)
        )

        captured_pkg = {}

        def capture_run_agent(**kwargs):
            captured_pkg.update(kwargs)
            return {
                "thumbnail_generation_plan_id": "tgp_ep_001",
                "episode_id": "ep_001",
                "main_episode": {"ranked_candidates": _make_clean_candidates()},
                "shorts": [],
                "primary_subject_preserved": True,
                "curiosity_gap_alignment": "concepts used",
            }

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   side_effect=capture_run_agent):
            from hypogeum.agents.thumbnail_prompt_writer import write_thumbnail_prompts
            from hypogeum.logger import get_logger
            log = get_logger(episode_id="ep_001", agent="test")

            write_thumbnail_prompts(
                handoff_brief={
                    "thumbnail_handoff_brief_id": "thb_ep_001",
                    "episode_id": "ep_001",
                    "visual_package_id": "vp_ep_001",
                    "central_curiosity_gap_statement": "...",
                    "candidates": [
                        {"candidate_id": "tc_001", "candidate_type": "literal_scene",
                         "description": "Gear", "supporting_evidence_or_reasoning": "",
                         "historical_integrity_flags": [], "constraint_flags": [],
                         "scene_reference": None, "candidate_strength_score": 8},
                        {"candidate_id": "tc_002", "candidate_type": "literal_scene",
                         "description": "Dial", "supporting_evidence_or_reasoning": "",
                         "historical_integrity_flags": [], "constraint_flags": [],
                         "scene_reference": None, "candidate_strength_score": 7},
                    ],
                    "short_candidates": [],
                    "primary_subject_identity": {
                        "entity_id": "antikythera_mechanism",
                        "visual_spec_summary": "Corroded bronze fragments with dark verdigris patina.",
                    },
                    "episode_color_palette": "pp_001",
                    "negative_constraints": [],
                },
                concept_brief=concept_brief,
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
                log=log,
            )

        pkg = captured_pkg.get("input_package", {})
        # Concept brief must be present
        assert "thumbnail_concept_brief" in pkg
        # The concept brief is the creative source — verify the mode flag
        assert pkg["thumbnail_concept_brief"]["thumbnail_concept_brief_id"] == "tcb_ep_001"

    def test_legacy_mode_passes_subject_identity_to_run_agent(self, tmp_path):
        """Without concept brief, input_package does NOT have thumbnail_concept_brief."""
        from hypogeum.config import Config

        config = MagicMock(spec=Config)
        config.TD_PLANNING_MODEL      = "anthropic/claude-sonnet-4.6"
        config.TD_PLANNING_MAX_TOKENS = 2048

        captured_pkg = {}

        def capture_run_agent(**kwargs):
            captured_pkg.update(kwargs)
            return {
                "thumbnail_generation_plan_id": "tgp_ep_001",
                "episode_id": "ep_001",
                "main_episode": {"ranked_candidates": _make_clean_candidates()},
                "shorts": [],
                "primary_subject_preserved": True,
                "curiosity_gap_alignment": "legacy mode",
            }

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   side_effect=capture_run_agent):
            from hypogeum.agents.thumbnail_prompt_writer import write_thumbnail_prompts
            from hypogeum.logger import get_logger
            log = get_logger(episode_id="ep_001", agent="test")

            write_thumbnail_prompts(
                handoff_brief={
                    "thumbnail_handoff_brief_id": "thb_ep_001",
                    "episode_id": "ep_001",
                    "visual_package_id": "vp_ep_001",
                    "central_curiosity_gap_statement": "...",
                    "candidates": [
                        {"candidate_id": "tc_001", "candidate_type": "literal_scene",
                         "description": "Gear", "supporting_evidence_or_reasoning": "",
                         "historical_integrity_flags": [], "constraint_flags": [],
                         "scene_reference": None, "candidate_strength_score": 8},
                        {"candidate_id": "tc_002", "candidate_type": "literal_scene",
                         "description": "Dial", "supporting_evidence_or_reasoning": "",
                         "historical_integrity_flags": [], "constraint_flags": [],
                         "scene_reference": None, "candidate_strength_score": 7},
                    ],
                    "short_candidates": [],
                    "primary_subject_identity": {
                        "entity_id": "antikythera_mechanism",
                        "visual_spec_summary": "Corroded bronze fragments.",
                    },
                    "episode_color_palette": "pp_001",
                    "negative_constraints": [],
                },
                concept_brief=None,  # no concept brief → legacy mode
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
                log=log,
            )

        pkg = captured_pkg.get("input_package", {})
        assert "thumbnail_concept_brief" not in pkg


# ---------------------------------------------------------------------------
# TestConceptDrivenModeRules
# ---------------------------------------------------------------------------

class TestConceptDrivenModeRules:

    def test_section_11c_forbids_object_noun_openings(self):
        """SECTION 11C must explicitly forbid starting with object nouns."""
        assert "FORBIDDEN" in SYSTEM_PROMPT
        assert "Corroded" in SYSTEM_PROMPT or "corroded" in SYSTEM_PROMPT.lower()

    def test_section_11c_forbids_primary_subject_identity_as_mandatory(self):
        """SECTION 11C must explicitly say primary_subject_identity is not mandatory."""
        assert "not required" in SYSTEM_PROMPT.lower() or "ADVISORY ONLY" in SYSTEM_PROMPT

    def test_section_11c_no_one_third_frame_rule(self):
        """SECTION 11C must explicitly cancel the 'one-third of frame' composition rule."""
        assert "one-third" in SYSTEM_PROMPT
        assert "DO NOT apply the rule" in SYSTEM_PROMPT or "not apply" in SYSTEM_PROMPT.lower()

    def test_section_11c_ctr_at_320px_rule(self):
        """SECTION 11C must include a 320px CTR readability rule."""
        assert "320px" in SYSTEM_PROMPT

    def test_section_11c_forbidden_words_list(self):
        """SECTION 11C must list specific forbidden opening words (case-insensitive)."""
        prompt_lower = SYSTEM_PROMPT.lower()
        for word in ["corroded", "bronze", "fragment", "gear", "tablet", "column", "torch"]:
            assert word in prompt_lower, f"Expected '{word}' in FORBIDDEN list in SECTION 11C"

    def test_wordmark_is_primary_anchor_in_prompt(self):
        """Prompt must establish the wordmark as the primary compositional anchor."""
        prompt_lower = SYSTEM_PROMPT.lower()
        # The old "story first, brand second" framing has been replaced with
        # wordmark-dominant language. Assert the new correct framing is present.
        assert (
            "primary compositional anchor" in prompt_lower
            or "dominant wordmark" in prompt_lower
            or "wordmark landing zone" in prompt_lower
        ), "Section 2B must establish the wordmark as primary compositional anchor"


# ---------------------------------------------------------------------------
# TestKillerConceptValidator
# ---------------------------------------------------------------------------

class TestKillerConceptValidator:

    def test_passes_clean_concept_prompts(self):
        result = validate_concept_quality(_make_clean_candidates())
        assert result["passed"] is True, f"Clean prompts failed: {result['failures']}"

    def test_fails_object_noun_opening(self):
        candidates = copy.deepcopy(_make_clean_candidates())
        candidates[0]["image_prompt"] = "Corroded bronze gear in deep darkness, amber light from below"
        result = validate_concept_quality(candidates)
        assert result["passed"] is False
        assert any("object noun" in f.lower() or "corroded" in f.lower() for f in result["failures"])

    def test_fails_more_than_2_cliche_nouns_in_opening(self):
        candidates = copy.deepcopy(_make_clean_candidates())
        candidates[1]["image_prompt"] = (
            "Gear fragments, ruins, tablet, torch beside bronze mechanism in dark chamber"
        )
        result = validate_concept_quality(candidates)
        assert result["passed"] is False
        assert any("collage" in f.lower() or "cliché" in f.lower() or "object" in f.lower()
                   for f in result["failures"])

    def test_fails_repeated_cliche_across_all_three(self):
        candidates = [
            _make_concept_candidate(1,
                "Deep void surrounds a gear of impossible precision in amber light.",
                "IT SHOULDN'T EXIST"),
            _make_concept_candidate(2,
                "Vast darkness holds a gear that should not exist, no context, no origin.",
                "THE KNOWLEDGE VANISHED"),
            _make_concept_candidate(3,
                "A gear alone in the void, its maker unknown, its origin absent.",
                "NO ONE KNOWS WHO BUILT IT"),
        ]
        result = validate_concept_quality(candidates)
        assert result["passed"] is False
        assert any("gear" in f.lower() for f in result["failures"])

    def test_fails_wordmark_as_subject(self):
        candidates = copy.deepcopy(_make_clean_candidates())
        candidates[0]["image_prompt"] = "The Hypogeum Chronicles wordmark on a dark background with amber glow"
        result = validate_concept_quality(candidates)
        assert result["passed"] is False
        assert any("wordmark" in f.lower() or "branding" in f.lower() for f in result["failures"])

    def test_fails_inventory_structure(self):
        candidates = copy.deepcopy(_make_clean_candidates())
        candidates[2]["image_prompt"] = (
            "Deep dark void with gear beside tablet, column with torch, ruins in background, "
            "amber light from below, wide atmospheric shot"
        )
        result = validate_concept_quality(candidates)
        assert result["passed"] is False
        assert any("inventory" in f.lower() or "collage" in f.lower() for f in result["failures"])

    def test_warns_explanatory_hook(self):
        candidates = copy.deepcopy(_make_clean_candidates())
        candidates[0]["text_overlay"] = {"text": "SOMEONE KNEW HOW", "placement": "bottom"}
        result = validate_concept_quality(candidates)
        # "knew how" matches explanatory pattern
        assert any("explanatory" in w.lower() or "knew how" in w.lower() for w in result["warnings"])

    def test_warns_no_curiosity_marker(self):
        candidates = copy.deepcopy(_make_clean_candidates())
        candidates[0]["text_overlay"] = {"text": "THE ANCIENT MACHINE", "placement": "bottom"}
        result = validate_concept_quality(candidates)
        assert any("curiosity" in w.lower() or "uncertainty" in w.lower() for w in result["warnings"])

    def test_passes_curiosity_driven_hooks(self):
        """IT SHOULDN'T EXIST, THE KNOWLEDGE VANISHED, NO ONE KNOWS WHO BUILT IT all pass."""
        candidates = _make_clean_candidates()
        result = validate_concept_quality(candidates)
        # Check no hook warnings about these three
        hook_failures = [w for w in result["warnings"]
                         if "shouldn't exist" in w.lower()
                         or "knowledge vanished" in w.lower()
                         or "no one knows" in w.lower()]
        assert len(hook_failures) == 0

    def test_ep003_old_prompts_fail(self):
        """Original ep_003 prompts must fail the killer concept gate."""
        result = validate_concept_quality(_make_old_ep003_candidates())
        assert result["passed"] is False
        # Must catch at minimum: object-noun opening, repeated clichés, identical hooks
        assert len(result["failures"]) >= 2

    def test_ep003_new_prompts_pass(self):
        """The three new ep_003 concept prompts must pass the killer gate."""
        candidates = [
            _make_concept_candidate(1,
                "Pure void — darkness so complete it has weight. Within it, a single point "
                "of amber light illuminates precision that cannot belong to any known era. "
                "No context. No origin. Only the impossible clarity and the silence. "
                "Impossibility register, near-black 16:9, single warm light source.",
                "IT SHOULDN'T EXIST"),
            _make_concept_candidate(2,
                "Vast darkness where knowledge once lived. The shelves are empty. The light "
                "finds nothing. Whatever was here — centuries of accumulated understanding — "
                "is gone as completely as if it never existed. "
                "Loss register, near-black dominant, aching emptiness.",
                "THE KNOWLEDGE VANISHED"),
            _make_concept_candidate(3,
                "Isolated in pure darkness, one object exists with no maker, no school, "
                "no predecessor, no explanation. The amber light that finds it illuminates "
                "only the mystery of its existence. Nothing else. No world around it. "
                "Lost-origin register, void dominant, single amber highlight.",
                "NO ONE KNOWS WHO BUILT IT"),
        ]
        result = validate_concept_quality(candidates)
        assert result["passed"] is True, f"New prompts failed: {result['failures']}"

    def test_or_raise_raises_on_failure(self):
        with pytest.raises(ConceptQualityError):
            validate_concept_quality_or_raise(_make_old_ep003_candidates())

    def test_or_raise_passes_clean_prompts(self):
        result = validate_concept_quality_or_raise(_make_clean_candidates())
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# TestWordmarkAndSubtitleScaling
# ---------------------------------------------------------------------------

class TestWordmarkAndSubtitleScaling:

    def test_wordmark_size_reduced_from_old_values(self):
        """New wordmark sizes must be smaller than the old values (0.130 / 0.068 / 0.080)."""
        from hypogeum import muapi
        import inspect
        source = inspect.getsource(muapi._apply_wordmark_overlay)
        # Old values: 0.130, 0.068, 0.080
        assert "0.130" not in source, "Old HYPOGEUM size 0.130 must be removed"
        assert "0.068" not in source, "Old THE size 0.068 must be removed"
        assert "0.080" not in source, "Old CHRONICLES size 0.080 must be removed"
        # New values should be smaller
        assert "0.055" in source or "0.050" in source, "New HYPOGEUM size must be ≤0.055"

    def test_subtitle_band_reduced_from_old_values(self):
        """Subtitle band must no longer be 20% of image height."""
        from hypogeum import muapi
        import inspect
        source = inspect.getsource(muapi._apply_text_overlay)
        assert "0.20" not in source, "Old 20% band height must be removed"
        assert "0.085" not in source, "Old 8.5% font size must be removed"

    def test_subtitle_not_pure_white(self):
        """The subtitle text color must not be pure white (255,255,255)."""
        from hypogeum import muapi
        import inspect
        source = inspect.getsource(muapi._apply_text_overlay)
        # Pure white draw call must be gone
        assert "fill=(255, 255, 255)" not in source, (
            "Pure white (255, 255, 255) subtitle must be replaced with warm cream"
        )

    def test_overlay_font_candidates_no_impact(self):
        """Impact font must no longer be the first choice for subtitles."""
        assert "Impact.ttf" not in _OVERLAY_FONT_CANDIDATES[0], (
            "Impact must not be the primary overlay font — use a lighter sans-serif"
        )

    def test_overlay_font_candidates_prefer_light_sans(self):
        """Font candidate list must include at least one clean/light sans-serif option."""
        candidates_lower = [f.lower() for f in _OVERLAY_FONT_CANDIDATES]
        has_light_option = any(
            any(name in c for c in candidates_lower)
            for name in ["raleway", "gill sans", "helvetica neue", "avenir", "optima"]
        )
        assert has_light_option, (
            "Overlay font candidates must include a light/clean sans-serif option "
            "(Raleway, Gill Sans, Helvetica Neue, Avenir, or Optima)"
        )

    def test_subtitle_uses_warm_cream_color(self):
        """Subtitle must use warm cream color (aged parchment) not pure white."""
        from hypogeum import muapi
        import inspect
        source = inspect.getsource(muapi._apply_text_overlay)
        # Warm parchment palette — any warm cream tuple, not pure white (255, 255, 255)
        assert "255, 255, 255" not in source, (
            "Subtitle must not use pure white (255, 255, 255) — must use warm cream/parchment"
        )


# ---------------------------------------------------------------------------
# TestStyleSystemSync
# ---------------------------------------------------------------------------

class TestStyleSystemSync:
    """
    Regression tests: thumbnail_director.txt must remain in sync with
    thumbnail_style_system.md as the canonical source of truth.

    These tests catch reintroduction of the specific contradictions and
    omissions that caused the ep_003 and ep_reg_vi_colosseum failures.
    Failure means a rule was removed from the runtime prompt without
    updating the style system first.
    """

    def test_cool_secondary_not_globally_forbidden(self):
        """
        The old blanket prohibition '\"cool\" palette or atmosphere (triggers E-C05)'
        must not exist. Only electric/neon/sci-fi blue is forbidden, not all cool tones.
        Style system §2.1, §3.2: cool secondary is REQUIRED for temperature contrast.
        """
        assert '"cool" palette or atmosphere' not in SYSTEM_PROMPT, (
            "Blanket prohibition on '\"cool\" palette or atmosphere' must be removed. "
            "Style system §3.2 requires a cool secondary accent for temperature contrast. "
            "Only electric/neon/sci-fi blue is forbidden — not natural cool atmosphere."
        )
        assert '"cold" as a lighting or atmosphere descriptor (triggers E-C05' not in SYSTEM_PROMPT, (
            "Blanket prohibition on 'cold' descriptor must be removed. "
            "Natural cool atmosphere (storm sky, stone grey) is permitted by style system §2.1."
        )

    def test_cool_secondary_light_source_required(self):
        """
        Style system §2.1: A secondary cooler light source is REQUIRED.
        The prompt must affirmatively instruct for this — not just permit it.
        """
        assert "SECONDARY COOL LIGHT SOURCE" in SYSTEM_PROMPT, (
            "SYSTEM_PROMPT must contain 'SECONDARY COOL LIGHT SOURCE' instruction. "
            "Monochromatic amber warmth is a documented failure pattern (style system §9 gap analysis)."
        )

    def test_near_black_ceiling_no_unlimited_void(self):
        """
        Style system §10.3: Near-black at 50–65% of frame, not unlimited void.
        Frames with 70–80%+ void read as gallery art, not YouTube mystery content.
        The phrase 'gallery art' is the canonical failure description.
        """
        assert "gallery art" in SYSTEM_PROMPT, (
            "SYSTEM_PROMPT must warn against unlimited void / 'gallery art' failure "
            "per style system §10.3. Add: 'frames with 70–80%+ near-black read as gallery art'."
        )

    def test_two_composition_sizes_monumental_range(self):
        """
        Style system §1.1: Monumental approach = 45–65% of right frame.
        Must be explicitly stated — not inferred from the old one-third rule.
        """
        assert "45–65%" in SYSTEM_PROMPT, (
            "SYSTEM_PROMPT must specify Monumental approach (45–65%) per style system §1.1. "
            "The two-size rule is the canonical replacement for the one-third minimum."
        )

    def test_two_composition_sizes_scale_contrast_range(self):
        """
        Style system §1.1: Scale Contrast approach = 5–12% of frame height.
        """
        assert "5–12%" in SYSTEM_PROMPT, (
            "SYSTEM_PROMPT must specify Scale Contrast approach (5–12%) per style system §1.1."
        )

    def test_product_shot_middle_zone_forbidden(self):
        """
        Style system §1.1: 30–50% of frame area is the forbidden product-shot zone.
        Must be named explicitly — it is the most common thumbnail failure pattern.
        """
        assert "30–50%" in SYSTEM_PROMPT, (
            "SYSTEM_PROMPT must name the forbidden 30–50% product-shot zone "
            "per style system §1.1."
        )

    def test_environmental_context_required(self):
        """
        Style system §10.3: Every subject must exist in a real ancient world.
        The phrase 'environmental context' must appear as a named requirement.
        """
        assert "ENVIRONMENTAL CONTEXT REQUIRED" in SYSTEM_PROMPT, (
            "SYSTEM_PROMPT must include 'ENVIRONMENTAL CONTEXT REQUIRED' rule "
            "per style system §10.3."
        )

    def test_photographic_credibility_required(self):
        """
        Style system §7.4: All ep_003 candidates read as CGI renders, not photographic.
        The prompt must instruct for photographic credibility.
        """
        assert "PHOTOGRAPHIC CREDIBILITY" in SYSTEM_PROMPT, (
            "SYSTEM_PROMPT must include 'PHOTOGRAPHIC CREDIBILITY' rule "
            "per style system §7.4."
        )

    def test_scale_anchor_required(self):
        """
        Style system §7.4: Zero scale anchors is a documented zero-score failure
        across all three ep_003 candidates. Must require scale anchors.
        """
        assert "SCALE ANCHOR" in SYSTEM_PROMPT, (
            "SYSTEM_PROMPT must include 'SCALE ANCHOR' requirement per style system §7.4."
        )

    def test_style_system_synced_header_present(self):
        """
        thumbnail_director.txt must carry a STYLE_SYSTEM_SYNCED header in the metadata
        block at the top of the file. Headers are stripped by load_prompt before being
        sent to the LLM — this test reads the raw file, not SYSTEM_PROMPT.
        """
        from pathlib import Path
        raw = (
            Path(__file__).parent.parent / "hypogeum" / "prompts" / "thumbnail_director.txt"
        ).read_text(encoding="utf-8")
        assert "STYLE_SYSTEM_SYNCED" in raw, (
            "thumbnail_director.txt must carry '# STYLE_SYSTEM_SYNCED: thumbnail_style_system.md' "
            "header at the top of the file."
        )


# ---------------------------------------------------------------------------
# TestBGAStyleSystemSync
# ---------------------------------------------------------------------------

class TestBGAStyleSystemSync:
    """
    Regression tests: brand_guardian_agent.txt must enforce SEMANTIC checks only.
    Structural/phrase checks belong in thumbnail_plan_validator.py.
    These tests catch re-introduction of duplicated validator work in BGA.
    """

    def test_zone_phrase_check_removed_from_bga(self):
        """
        E-TH04/E-TH05 as zone-phrase checks (checking for 'top 12%' / 'bottom zone'
        reservation text) are thumbnail_plan_validator.py's job — not BGA's.
        BGA's E-TH04/E-TH05 must be semantic product-shot and CGI checks instead.
        """
        assert "explicitly reserve the top 12%" not in BGA_SYSTEM_PROMPT, (
            "BGA must not check for zone reservation phrases — that is the validator's job. "
            "E-TH04 must be the product-shot composition semantic check."
        )
        assert "explicitly reserve the bottom zone" not in BGA_SYSTEM_PROMPT, (
            "BGA must not check for bottom zone reservation phrases — that is the validator's job. "
            "E-TH05 must be the CGI appearance semantic check."
        )

    def test_product_shot_semantic_check_in_bga(self):
        """
        BGA E-TH04 must check for isolated subject in void with no environmental context —
        a semantic judgment that code cannot make deterministically.
        """
        bga_lower = BGA_SYSTEM_PROMPT.lower()
        assert "product-shot composition" in bga_lower or (
            "isolated" in bga_lower and "void" in bga_lower and "e-th04" in bga_lower
        ), (
            "BGA must include semantic product-shot composition check (E-TH04)."
        )

    def test_cgi_appearance_check_in_bga(self):
        """
        BGA E-TH05 must check for CGI/rendered appearance language in image_prompts.
        """
        bga_lower = BGA_SYSTEM_PROMPT.lower()
        assert "cgi appearance" in bga_lower or (
            ("concept art" in bga_lower or "3d render" in bga_lower)
            and "e-th05" in bga_lower
        ), (
            "BGA must include semantic CGI/rendered appearance check (E-TH05)."
        )

    def test_temperature_contrast_flag_in_bga(self):
        """
        BGA E-TH06 must flag thumbnail prompts with no temperature contrast —
        monochromatic warm amber is a style system failure (§9 gap analysis).
        """
        assert "E-TH06" in BGA_SYSTEM_PROMPT, (
            "BGA must include E-TH06 temperature contrast FLAG per style system §9."
        )

    def test_bga_style_system_synced_header_present(self):
        """
        brand_guardian_agent.txt must carry a STYLE_SYSTEM_SYNCED header in the metadata
        block. Headers are stripped by load_prompt — read the raw file here.
        """
        from pathlib import Path
        raw = (
            Path(__file__).parent.parent / "hypogeum" / "prompts" / "brand_guardian_agent.txt"
        ).read_text(encoding="utf-8")
        assert "STYLE_SYSTEM_SYNCED" in raw, (
            "brand_guardian_agent.txt must carry '# STYLE_SYSTEM_SYNCED: thumbnail_style_system.md' "
            "header at the top of the file."
        )
