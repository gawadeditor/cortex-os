"""
tests/test_thumbnail_composition_typography.py

Tests for:
  - thumbnail_composition_planner.plan_composition()
  - thumbnail_typography_planner.plan_typography()
  - thumbnail_plan_validator (pre-generation + post-compositing)
  - MUAPI compositor: typography_plan used instead of old text_overlay
  - MUAPI compositor: old text_overlay still works in legacy mode
  - concept candidate has composition_plan + typography_plan after write_thumbnail_prompts

All network calls and real image generation are mocked. No MUAPI credits consumed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.thumbnail_composition_planner import (
    FOCAL_ZONE_RESOLUTION_POOL,
    plan_composition,
    resolve_composition_distinctness,
)
from hypogeum.agents.thumbnail_typography_planner import plan_typography
from hypogeum.agents.thumbnail_plan_validator import (
    validate_composition_typography_pre_generation,
    validate_post_compositing,
    validate_all_candidates_pre_generation,
    validate_thumbnail_dna,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _comp(trigger: str) -> dict:
    return plan_composition(trigger)


def _typo(hook: str, trigger: str, comp: dict = None) -> dict:
    if comp is None:
        comp = _comp(trigger)
    return plan_typography(hook, trigger, comp)


# ===========================================================================
# TestCompositionPlanner
# ===========================================================================

class TestCompositionPlanner:

    def test_returns_dict(self):
        p = plan_composition("impossibility")
        assert isinstance(p, dict)

    def test_required_keys_present(self):
        p = plan_composition("impossibility")
        for k in ("focal_zone", "focal_scale", "negative_space_zones",
                  "dominant_tone", "text_zone", "text_safe_brightness", "eye_path",
                  "environment_level", "contrast_mechanism", "subject_readability_at_320px"):
            assert k in p, f"Missing key: {k}"

    def test_dominant_tone_is_near_black(self):
        for trigger in ("impossibility", "loss", "betrayal", "discovery", "awe", "dread"):
            assert plan_composition(trigger)["dominant_tone"] == "near_black"

    def test_impossibility_focal_scale_medium(self):
        # DNA v2 (2026-07-13, V4 regression correction): tightened from
        # "large" (40-60% of right frame) to "medium" (25-40%), consistent
        # with the new hero_object_max_frame_share_pct=[28, 32] ceiling —
        # the two fields must not contradict each other.
        p = plan_composition("impossibility")
        assert p["focal_scale"] == "medium"

    def test_impossibility_focal_zone_center_right(self):
        p = plan_composition("impossibility")
        assert p["focal_zone"] == "center_right"

    def test_impossibility_text_zone_lower_left(self):
        p = plan_composition("impossibility")
        assert p["text_zone"] == "lower_left"

    def test_impossibility_negative_space_includes_lower_left(self):
        p = plan_composition("impossibility")
        assert "lower_left" in p["negative_space_zones"]

    def test_impossibility_environment_level_contextual(self):
        p = plan_composition("impossibility")
        assert p["environment_level"] == "contextual"

    def test_loss_focal_scale_trace(self):
        """loss uses focal_scale='trace' — remnant element, not full absence."""
        p = plan_composition("loss")
        assert p["focal_scale"] == "trace"

    def test_loss_focal_zone_center(self):
        p = plan_composition("loss")
        assert p["focal_zone"] == "center"

    def test_loss_negative_space_includes_sides(self):
        p = plan_composition("loss")
        zones = p["negative_space_zones"]
        assert len(zones) >= 1

    def test_loss_eye_path_contains_hook_text(self):
        p = plan_composition("loss")
        assert "hook_text" in p["eye_path"]
        assert "wordmark" in p["eye_path"]

    def test_loss_environment_level_suggestive(self):
        p = plan_composition("loss")
        assert p["environment_level"] == "suggestive"

    def test_betrayal_negative_zones_include_lower_left(self):
        p = plan_composition("betrayal")
        assert "lower_left" in p["negative_space_zones"]

    def test_betrayal_focal_zone_center_right(self):
        p = plan_composition("betrayal")
        assert p["focal_zone"] == "center_right"

    def test_betrayal_contrast_mechanism_human_vs_artifact(self):
        p = plan_composition("betrayal")
        assert p["contrast_mechanism"] == "human_vs_artifact"

    def test_awe_focal_scale_small(self):
        p = plan_composition("awe")
        assert p["focal_scale"] == "small"

    def test_dread_focal_zone_center_right(self):
        p = plan_composition("dread")
        assert p["focal_zone"] == "center_right"

    def test_unknown_trigger_returns_default(self):
        p = plan_composition("unknown_trigger_xyz")
        assert isinstance(p, dict)
        assert "text_zone" in p
        assert "focal_zone" in p

    def test_negative_space_zones_is_list(self):
        p = plan_composition("impossibility")
        assert isinstance(p["negative_space_zones"], list)

    def test_eye_path_is_list(self):
        p = plan_composition("betrayal")
        assert isinstance(p["eye_path"], list)

    def test_text_safe_brightness_set(self):
        p = plan_composition("loss")
        assert p["text_safe_brightness"] == "< 15%"


# ===========================================================================
# TestTypographyPlanner
# ===========================================================================

class TestTypographyPlanner:

    def test_returns_dict(self):
        p = _typo("IT SHOULDN'T EXIST", "impossibility")
        assert isinstance(p, dict)

    def test_required_keys_present(self):
        p = _typo("IT SHOULDN'T EXIST", "impossibility")
        for k in ("headline", "emphasis", "layout", "alignment",
                  "zone", "emphasis_scale", "headline_weight", "emphasis_weight"):
            assert k in p, f"Missing key: {k}"

    def test_splits_it_shouldnt_exist_correctly(self):
        """typography_plan splits 'IT SHOULDN'T EXIST' into headline='IT SHOULDN'T' and emphasis='EXIST'."""
        p = _typo("IT SHOULDN'T EXIST", "impossibility")
        assert p["headline"] == "IT SHOULDN'T"
        assert p["emphasis"] == "EXIST"

    def test_splits_the_knowledge_vanished(self):
        p = _typo("THE KNOWLEDGE VANISHED", "loss")
        assert p["headline"] == "THE KNOWLEDGE"
        assert p["emphasis"] == "VANISHED"

    def test_splits_long_hook_last_two_words(self):
        """Hook >4 words: last 2 words become emphasis."""
        p = _typo("NO ONE KNOWS WHO BUILT IT", "betrayal")
        assert p["emphasis"] == "BUILT IT"
        assert p["headline"] == "NO ONE KNOWS WHO"

    def test_layout_always_stacked(self):
        for trigger in ("impossibility", "loss", "betrayal"):
            assert _typo("IT SHOULDN'T EXIST", trigger)["layout"] == "stacked"

    def test_loss_uses_left_alignment(self):
        p = _typo("THE KNOWLEDGE VANISHED", "loss")
        assert p["alignment"] == "left"

    def test_betrayal_uses_left_alignment(self):
        p = _typo("NO ONE KNOWS WHO BUILT IT", "betrayal")
        assert p["alignment"] == "left"

    def test_impossibility_left_when_zone_not_center(self):
        comp = _comp("impossibility")
        assert comp["text_zone"] == "lower_left"
        p = plan_typography("IT SHOULDN'T EXIST", "impossibility", comp)
        assert p["alignment"] == "left"

    def test_impossibility_center_when_zone_is_center(self):
        """If composition_plan forces text_zone=center, impossibility may center-align."""
        comp = _comp("impossibility")
        comp["text_zone"] = "center"
        p = plan_typography("IT SHOULDN'T EXIST", "impossibility", comp)
        assert p["alignment"] == "center"

    def test_awe_center_when_zone_is_center(self):
        comp = _comp("awe")
        comp["text_zone"] = "center"
        p = plan_typography("TOO VAST", "awe", comp)
        assert p["alignment"] == "center"

    def test_zone_mirrors_composition_plan_text_zone(self):
        """typography_plan.zone must match composition_plan.text_zone."""
        comp = _comp("loss")
        p = plan_typography("THE KNOWLEDGE VANISHED", "loss", comp)
        assert p["zone"] == comp["text_zone"]

    def test_emphasis_scale_single_word_gte_1_7(self):
        p = _typo("IT SHOULDN'T EXIST", "impossibility")
        assert p["emphasis"] == "EXIST"
        assert p["emphasis_scale"] >= 1.7

    def test_emphasis_scale_two_words_gte_1_4(self):
        p = _typo("NO ONE KNOWS WHO BUILT IT", "betrayal")
        assert p["emphasis"] == "BUILT IT"
        assert p["emphasis_scale"] >= 1.4

    def test_headline_weight_regular(self):
        p = _typo("IT SHOULDN'T EXIST", "impossibility")
        assert p["headline_weight"] == "regular"

    def test_emphasis_weight_bold(self):
        p = _typo("IT SHOULDN'T EXIST", "impossibility")
        assert p["emphasis_weight"] == "bold"

    def test_single_word_hook_empty_headline(self):
        p = _typo("IMPOSSIBLE", "impossibility")
        assert p["headline"] == ""
        assert p["emphasis"] == "IMPOSSIBLE"


# ===========================================================================
# TestPlanValidator — pre-generation
# ===========================================================================

class TestPlanValidatorPreGeneration:

    def _make_candidate(
        self,
        comp_zone: str = "lower_left",
        typo_zone: str = "lower_left",
        prompt: str = "something lower-left zone intentionally empty void",
        emphasis: str = "EXIST",
        headline: str = "IT SHOULDN'T",
    ) -> dict:
        return {
            "candidate_id": "tc_001",
            "image_prompt": prompt,
            "composition_plan": {
                "text_zone": comp_zone,
                "negative_space_zones": ["lower_left"],
                "focal_zone": "center_right",
                "focal_scale": "small",
            },
            "typography_plan": {
                "zone": typo_zone,
                "headline": headline,
                "emphasis": emphasis,
                "alignment": "left",
            },
        }

    def test_valid_candidate_passes(self):
        result = validate_composition_typography_pre_generation(self._make_candidate())
        assert result["passed"] is True
        assert result["errors"] == []

    def test_zone_mismatch_fails(self):
        """text_zone and typography zone must match."""
        c = self._make_candidate(comp_zone="lower_left", typo_zone="lower_right")
        result = validate_composition_typography_pre_generation(c)
        assert result["passed"] is False
        assert any("text_zone" in e or "zone" in e for e in result["errors"])

    def test_empty_emphasis_fails(self):
        c = self._make_candidate(emphasis="")
        result = validate_composition_typography_pre_generation(c)
        assert result["passed"] is False
        assert any("emphasis" in e for e in result["errors"])

    def test_empty_headline_fails(self):
        c = self._make_candidate(headline="")
        result = validate_composition_typography_pre_generation(c)
        assert result["passed"] is False
        assert any("headline" in e for e in result["errors"])

    def test_prompt_not_mentioning_zone_fails(self):
        c = self._make_candidate(prompt="some image with no zone mention whatsoever")
        result = validate_composition_typography_pre_generation(c)
        assert result["passed"] is False
        assert any("text zone" in e for e in result["errors"])

    def test_prompt_mentioning_void_passes_negative_space_check(self):
        c = self._make_candidate(prompt="lower left empty void for text overlay zone reserved")
        result = validate_composition_typography_pre_generation(c)
        # Should pass negative space check due to "void"
        assert not any("negative space" in e for e in result["errors"])

    def test_no_plans_skips_validation(self):
        """Candidate without composition_plan/typography_plan skips all checks."""
        c = {"candidate_id": "tc_no_plans", "image_prompt": "some prompt"}
        result = validate_composition_typography_pre_generation(c)
        assert result["passed"] is True


class TestValidateAllCandidatesPreGeneration:

    def test_empty_list_passes(self):
        result = validate_all_candidates_pre_generation([])
        assert result["passed"] is True
        assert result["failures"] == []

    def test_one_failure_reported(self):
        c = {
            "candidate_id": "tc_bad",
            "image_prompt": "no zone mentioned",
            "composition_plan": {"text_zone": "lower_left", "negative_space_zones": ["lower_left"]},
            "typography_plan": {"zone": "lower_right", "headline": "IT SHOULDN'T", "emphasis": "EXIST"},
        }
        result = validate_all_candidates_pre_generation([c])
        assert result["passed"] is False
        assert len(result["failures"]) == 1
        assert result["failures"][0]["candidate_id"] == "tc_bad"


# ===========================================================================
# TestPlanValidatorPostCompositing
# ===========================================================================

class TestPlanValidatorPostCompositing:

    def test_valid_candidate_passes(self):
        c = {"typography_plan": {"zone": "lower_left", "layout": "stacked"}}
        result = validate_post_compositing(c)
        assert result["passed"] is True

    def test_full_width_band_fails(self):
        c = {"typography_plan": {"zone": "lower_left", "layout": "full_width_band"}}
        result = validate_post_compositing(c)
        assert result["passed"] is False
        assert any("full-width" in e for e in result["errors"])

    def test_upper_zone_fails(self):
        c = {"typography_plan": {"zone": "upper_center", "layout": "stacked"}}
        result = validate_post_compositing(c)
        assert result["passed"] is False
        assert any("wordmark" in e for e in result["errors"])

    def test_no_typography_plan_skips(self):
        c = {"typography_plan": None}
        result = validate_post_compositing(c)
        assert result["passed"] is True


# ===========================================================================
# TestMUAPICompositor — typography_plan vs text_overlay routing
# ===========================================================================

class TestMUAPICompositorTypography:
    """
    Test that _attempt_thumbnail_generation routes to _apply_typography_plan
    when typography_plan is present, and falls back to _apply_text_overlay
    in legacy mode.
    """

    _MUAPI_MODULE = "hypogeum.muapi"

    def _make_fake_image(self, tmp_path: Path) -> Path:
        """Create a minimal JPEG-like file for the compositor to open."""
        thumb_dir = tmp_path / "thumbnails"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        try:
            from PIL import Image as PILImage
            img = PILImage.new("RGB", (1280, 720), color=(10, 10, 10))
            path = thumb_dir / "test_deliverable.jpg"
            img.save(path, "JPEG")
            return path
        except ImportError:
            pytest.skip("PIL not available")

    def test_typography_plan_calls_apply_typography_plan(self, tmp_path):
        """MUAPI compositor uses typography_plan instead of old text_overlay."""
        from hypogeum.muapi import _attempt_thumbnail_generation
        from hypogeum.config import Config

        fake_path = self._make_fake_image(tmp_path)

        # Patch _generate_thumbnail_with_model to no-op (just create the file)
        def fake_gen(**kwargs):
            pass  # file already exists from _make_fake_image

        typography_plan = {
            "headline": "IT SHOULDN'T",
            "emphasis": "EXIST",
            "layout": "stacked",
            "alignment": "left",
            "zone": "lower_left",
            "emphasis_scale": 2.5,
            "headline_weight": "light",
            "emphasis_weight": "bold",
        }
        text_overlay = {"text": "IT SHOULDN'T EXIST", "placement": "bottom"}

        with patch(f"{self._MUAPI_MODULE}._apply_text_overlay") as mock_old, \
             patch(f"{self._MUAPI_MODULE}._apply_typography_plan") as mock_new, \
             patch(f"{self._MUAPI_MODULE}._normalize_thumbnail"), \
             patch(f"{self._MUAPI_MODULE}._apply_wordmark_overlay"), \
             patch(f"{self._MUAPI_MODULE}._generate_thumbnail_with_model"):

            # Simulate that the file was created by the generation step
            fake_path.touch()

            config = MagicMock()
            config.THUMBNAIL_IMAGE_MODEL = "flux-2-dev"
            config.THUMBNAIL_FALLBACK_MODEL = ""
            config.MUAPI_DRY_RUN = False
            log = MagicMock()

            _attempt_thumbnail_generation(
                deliverable_id="test_deliverable",
                image_prompt="test prompt",
                aspect_ratio="16:9",
                primary_model="flux-2-dev",
                fallback_model="",
                config=config,
                output_dir=tmp_path,
                episode_id="ep_test",
                budget_guard=None,
                text_overlay=text_overlay,
                typography_plan=typography_plan,
                log=log,
            )

        mock_new.assert_called_once()
        mock_old.assert_not_called()

    def test_text_overlay_used_in_legacy_mode(self, tmp_path):
        """old text_overlay still works in legacy mode (no typography_plan)."""
        from hypogeum.muapi import _attempt_thumbnail_generation

        fake_path = self._make_fake_image(tmp_path)
        text_overlay = {"text": "WHO BUILT IT", "placement": "bottom"}

        with patch(f"{self._MUAPI_MODULE}._apply_text_overlay") as mock_old, \
             patch(f"{self._MUAPI_MODULE}._apply_typography_plan") as mock_new, \
             patch(f"{self._MUAPI_MODULE}._normalize_thumbnail"), \
             patch(f"{self._MUAPI_MODULE}._apply_wordmark_overlay"), \
             patch(f"{self._MUAPI_MODULE}._generate_thumbnail_with_model"):

            fake_path.touch()

            config = MagicMock()
            config.THUMBNAIL_IMAGE_MODEL = "flux-2-dev"
            config.THUMBNAIL_FALLBACK_MODEL = ""
            config.MUAPI_DRY_RUN = False
            log = MagicMock()

            _attempt_thumbnail_generation(
                deliverable_id="test_deliverable",
                image_prompt="test prompt",
                aspect_ratio="16:9",
                primary_model="flux-2-dev",
                fallback_model="",
                config=config,
                output_dir=tmp_path,
                episode_id="ep_test",
                budget_guard=None,
                text_overlay=text_overlay,
                typography_plan=None,  # legacy mode
                log=log,
            )

        mock_old.assert_called_once()
        mock_new.assert_not_called()

    def test_no_text_compositing_when_both_none(self, tmp_path):
        """No text compositing calls when both text_overlay and typography_plan are None."""
        from hypogeum.muapi import _attempt_thumbnail_generation

        fake_path = self._make_fake_image(tmp_path)

        with patch(f"{self._MUAPI_MODULE}._apply_text_overlay") as mock_old, \
             patch(f"{self._MUAPI_MODULE}._apply_typography_plan") as mock_new, \
             patch(f"{self._MUAPI_MODULE}._normalize_thumbnail"), \
             patch(f"{self._MUAPI_MODULE}._apply_wordmark_overlay"), \
             patch(f"{self._MUAPI_MODULE}._generate_thumbnail_with_model"):

            fake_path.touch()

            config = MagicMock()
            config.MUAPI_DRY_RUN = False
            log = MagicMock()

            _attempt_thumbnail_generation(
                deliverable_id="test_deliverable",
                image_prompt="test prompt",
                aspect_ratio="16:9",
                primary_model="flux-2-dev",
                fallback_model="",
                config=config,
                output_dir=tmp_path,
                episode_id="ep_test",
                budget_guard=None,
                text_overlay=None,
                typography_plan=None,
                log=log,
            )

        mock_old.assert_not_called()
        mock_new.assert_not_called()


# ===========================================================================
# TestConceptCandidatePlans — write_thumbnail_prompts attaches plans
# ===========================================================================

class TestConceptCandidatePlans:
    """
    concept candidate has composition_plan + typography_plan after write_thumbnail_prompts.
    """

    def _make_concept_brief(self) -> dict:
        return {
            "thumbnail_concept_brief_id": "tcb_ep_test",
            "episode_id": "ep_test",
            "central_viewer_question": "Why does this thing exist?",
            "concepts": [
                {
                    "concept_id": "tsc_A",
                    "concept_name": "Impossible Machine",
                    "viewer_promise": "It shouldn't exist.",
                    "curiosity_gap": "No prototype.",
                    "emotional_trigger": "impossibility",
                    "visual_thesis": "An impossible object in darkness.",
                    "suggested_hook_text": "IT SHOULDN'T EXIST",
                },
                {
                    "concept_id": "tsc_B",
                    "concept_name": "Vanished Knowledge",
                    "viewer_promise": "The knowledge is gone.",
                    "curiosity_gap": "Gone forever.",
                    "emotional_trigger": "loss",
                    "visual_thesis": "Empty archive.",
                    "suggested_hook_text": "THE KNOWLEDGE VANISHED",
                },
                {
                    "concept_id": "tsc_C",
                    "concept_name": "Lost Origin",
                    "viewer_promise": "No one knows.",
                    "curiosity_gap": "One object, no origin.",
                    "emotional_trigger": "betrayal",
                    "visual_thesis": "Object in pure darkness.",
                    "suggested_hook_text": "NO ONE KNOWS WHO BUILT IT",
                },
            ],
        }

    def _make_generation_plan(self) -> dict:
        """Claude's raw response — no composition/typography plans yet."""
        def _cand(cid, rank, trigger, hook):
            return {
                "candidate_id": cid,
                "rank": rank,
                "image_prompt": f"test prompt for {trigger} lower left zone intentionally empty void",
                "aspect_ratio": "16:9",
                "ctr_rationale": "test",
                "text_overlay": {"text": hook, "placement": "bottom"},
            }
        return {
            "thumbnail_generation_plan_id": "tgp_ep_test",
            "episode_id": "ep_test",
            "main_episode": {
                "ranked_candidates": [
                    _cand("tsc_A", 1, "impossibility", "IT SHOULDN'T EXIST"),
                    _cand("tsc_B", 2, "loss",           "THE KNOWLEDGE VANISHED"),
                    _cand("tsc_C", 3, "betrayal",       "NO ONE KNOWS WHO BUILT IT"),
                ]
            },
            "shorts": [],
            "primary_subject_preserved": False,
            "curiosity_gap_alignment": "concept driven",
        }

    def test_concept_candidate_has_composition_plan(self, tmp_path):
        """After write_thumbnail_prompts in concept_driven_mode, each candidate has composition_plan."""
        from hypogeum.agents.thumbnail_prompt_writer import write_thumbnail_prompts
        from hypogeum.config import Config

        concept_brief   = self._make_concept_brief()
        generation_plan = self._make_generation_plan()
        log             = MagicMock()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=generation_plan):
            config = MagicMock()
            config.TD_PLANNING_MODEL = "test-model"
            config.TD_PLANNING_MAX_TOKENS = 4096
            result = write_thumbnail_prompts(
                handoff_brief={"candidates": [], "short_candidates": []},
                config=config,
                episode_id="ep_test",
                output_dir=tmp_path,
                log=log,
                concept_brief=concept_brief,
            )

        cands = result["main_episode"]["ranked_candidates"]
        for c in cands:
            assert "composition_plan" in c, f"Missing composition_plan on {c['candidate_id']}"
            assert isinstance(c["composition_plan"], dict)

    def test_concept_candidate_has_typography_plan(self, tmp_path):
        """After write_thumbnail_prompts in concept_driven_mode, each candidate has typography_plan."""
        from hypogeum.agents.thumbnail_prompt_writer import write_thumbnail_prompts

        concept_brief   = self._make_concept_brief()
        generation_plan = self._make_generation_plan()
        log             = MagicMock()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=generation_plan):
            config = MagicMock()
            config.TD_PLANNING_MODEL = "test-model"
            config.TD_PLANNING_MAX_TOKENS = 4096
            result = write_thumbnail_prompts(
                handoff_brief={"candidates": [], "short_candidates": []},
                config=config,
                episode_id="ep_test",
                output_dir=tmp_path,
                log=log,
                concept_brief=concept_brief,
            )

        cands = result["main_episode"]["ranked_candidates"]
        for c in cands:
            assert "typography_plan" in c, f"Missing typography_plan on {c['candidate_id']}"
            assert isinstance(c["typography_plan"], dict)

    def test_rank1_typography_splits_correctly(self, tmp_path):
        """Rank-1 candidate (impossibility/'IT SHOULDN'T EXIST') has correct headline/emphasis."""
        from hypogeum.agents.thumbnail_prompt_writer import write_thumbnail_prompts

        concept_brief   = self._make_concept_brief()
        generation_plan = self._make_generation_plan()
        log             = MagicMock()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=generation_plan):
            config = MagicMock()
            config.TD_PLANNING_MODEL = "test-model"
            config.TD_PLANNING_MAX_TOKENS = 4096
            result = write_thumbnail_prompts(
                handoff_brief={"candidates": [], "short_candidates": []},
                config=config,
                episode_id="ep_test",
                output_dir=tmp_path,
                log=log,
                concept_brief=concept_brief,
            )

        rank1 = next(c for c in result["main_episode"]["ranked_candidates"] if c["rank"] == 1)
        typo  = rank1["typography_plan"]
        assert typo["headline"] == "IT SHOULDN'T"
        assert typo["emphasis"]  == "EXIST"

    def test_text_zone_and_typography_zone_match(self, tmp_path):
        """composition_plan.text_zone == typography_plan.zone for all concept candidates."""
        from hypogeum.agents.thumbnail_prompt_writer import write_thumbnail_prompts

        concept_brief   = self._make_concept_brief()
        generation_plan = self._make_generation_plan()
        log             = MagicMock()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=generation_plan):
            config = MagicMock()
            config.TD_PLANNING_MODEL = "test-model"
            config.TD_PLANNING_MAX_TOKENS = 4096
            result = write_thumbnail_prompts(
                handoff_brief={"candidates": [], "short_candidates": []},
                config=config,
                episode_id="ep_test",
                output_dir=tmp_path,
                log=log,
                concept_brief=concept_brief,
            )

        for c in result["main_episode"]["ranked_candidates"]:
            comp = c.get("composition_plan", {})
            typo = c.get("typography_plan", {})
            assert comp.get("text_zone") == typo.get("zone"), (
                f"Zone mismatch on {c['candidate_id']}: "
                f"comp.text_zone={comp.get('text_zone')!r} != typo.zone={typo.get('zone')!r}"
            )

    def test_legacy_mode_no_plans_attached(self, tmp_path):
        """Without concept_brief, no composition_plan or typography_plan is attached."""
        from hypogeum.agents.thumbnail_prompt_writer import write_thumbnail_prompts

        generation_plan = self._make_generation_plan()
        log             = MagicMock()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=generation_plan):
            config = MagicMock()
            config.TD_PLANNING_MODEL = "test-model"
            config.TD_PLANNING_MAX_TOKENS = 4096
            result = write_thumbnail_prompts(
                handoff_brief={"candidates": [], "short_candidates": []},
                config=config,
                episode_id="ep_test",
                output_dir=tmp_path,
                log=log,
                concept_brief=None,  # legacy mode
            )

        for c in result["main_episode"]["ranked_candidates"]:
            assert "composition_plan" not in c
            assert "typography_plan" not in c


# ===========================================================================
# TestResolveCompositionDistinctness (2026-07-19 — candidate-assembly-layer
# fix for the real thumbnail_composition_distinctness failure found during
# the 2026-07-19 supervised validation campaign; see
# hypogeum/agents/thumbnail_composition_planner.py's own module-level
# comment for the full root-cause history).
# ===========================================================================

class TestResolveCompositionDistinctness:

    def _cand(self, rank: int, trigger: str) -> dict:
        return {
            "candidate_id": f"cand_{rank}",
            "rank": rank,
            "emotional_trigger": trigger,
            "concept_name": f"concept for {trigger}",
            "ctr_rationale": f"rationale for {trigger}",
            "image_prompt": f"image prompt for {trigger}",
            "composition_plan": plan_composition(trigger),
        }

    def test_three_candidates_sharing_center_right_become_distinct(self):
        """The exact real-world collision from Validation Run 2
        (ep_74237788): impossibility, betrayal, discovery all share
        focal_zone='center_right' before resolution."""
        candidates = [
            self._cand(1, "impossibility"),
            self._cand(2, "betrayal"),
            self._cand(3, "discovery"),
        ]
        zones_before = [c["composition_plan"]["focal_zone"] for c in candidates]
        assert len(set(zones_before)) == 1  # sanity: the collision is real

        resolve_composition_distinctness(candidates)

        zones_after = [c["composition_plan"]["focal_zone"] for c in candidates]
        assert len(set(zones_after)) == 3
        assert set(zones_after) <= set(FOCAL_ZONE_RESOLUTION_POOL)

    def test_two_candidates_sharing_a_zone(self):
        """Rank 1 and Rank 3 collide (both 'discovery' -> center_right);
        Rank 2 ('loss' -> center) is already distinct and must be left
        alone -- only the actual collision is touched."""
        candidates = [
            self._cand(1, "discovery"),   # center_right
            self._cand(2, "loss"),        # center
            self._cand(3, "dread"),       # center_right -- collides with rank 1
        ]
        rank2_zone_before = candidates[1]["composition_plan"]["focal_zone"]

        resolve_composition_distinctness(candidates)

        zones_after = [c["composition_plan"]["focal_zone"] for c in candidates]
        assert len(set(zones_after)) == 3
        assert candidates[1]["composition_plan"]["focal_zone"] == rank2_zone_before

    def test_three_candidates_already_distinct_is_a_no_op(self):
        """When focal_zone is already fully distinct, nothing changes at all
        -- not just the zone values, the whole composition_plan dict."""
        candidates = [
            self._cand(1, "discovery"),   # center_right
            self._cand(2, "loss"),        # center
        ]
        # Manually force a third, already-distinct zone that isn't even in
        # the resolution pool, to prove the no-op path doesn't force
        # candidates onto pool values when no collision exists.
        candidates.append(self._cand(3, "discovery"))
        candidates[2]["composition_plan"]["focal_zone"] = "some_other_zone"

        before = [dict(c["composition_plan"]) for c in candidates]
        resolve_composition_distinctness(candidates)
        after = [c["composition_plan"] for c in candidates]

        assert before == after

    def test_rank1_never_modified(self):
        """Rank 1's composition_plan is byte-for-byte identical before and
        after resolution, regardless of collisions among ranks 2/3."""
        candidates = [
            self._cand(1, "impossibility"),
            self._cand(2, "betrayal"),
            self._cand(3, "discovery"),
        ]
        rank1_before = dict(candidates[0]["composition_plan"])

        resolve_composition_distinctness(candidates)

        assert candidates[0]["composition_plan"] == rank1_before

    def test_rank1_never_modified_even_when_processed_out_of_list_order(self):
        """The function sorts by rank internally -- passing candidates in a
        shuffled list order must not change which one is 'rank 1'."""
        candidates = [
            self._cand(3, "discovery"),
            self._cand(1, "impossibility"),
            self._cand(2, "betrayal"),
        ]
        rank1_cand = next(c for c in candidates if c["rank"] == 1)
        rank1_before = dict(rank1_cand["composition_plan"])

        resolve_composition_distinctness(candidates)

        assert rank1_cand["composition_plan"] == rank1_before

    def test_only_focal_zone_field_is_modified(self):
        """Every other field on a reassigned candidate -- trigger, concept,
        CTR rationale, image_prompt, and every other composition_plan key --
        is preserved exactly."""
        candidates = [
            self._cand(1, "impossibility"),
            self._cand(2, "betrayal"),
            self._cand(3, "discovery"),
        ]
        snapshot = [
            {k: v for k, v in c.items() if k != "composition_plan"}
            for c in candidates
        ]
        comp_snapshot = [
            {k: v for k, v in c["composition_plan"].items() if k != "focal_zone"}
            for c in candidates
        ]

        resolve_composition_distinctness(candidates)

        for i, c in enumerate(candidates):
            assert {k: v for k, v in c.items() if k != "composition_plan"} == snapshot[i]
            assert {
                k: v for k, v in c["composition_plan"].items() if k != "focal_zone"
            } == comp_snapshot[i]

    def test_deterministic_across_repeated_runs(self):
        """Running the resolver on independently-built, identical input
        always produces the same output -- no randomness."""
        def _build():
            return [
                self._cand(1, "impossibility"),
                self._cand(2, "betrayal"),
                self._cand(3, "discovery"),
            ]

        results = []
        for _ in range(10):
            candidates = _build()
            resolve_composition_distinctness(candidates)
            results.append([c["composition_plan"]["focal_zone"] for c in candidates])

        assert all(r == results[0] for r in results)

    def test_does_not_modify_trigger_map(self):
        """Resolving a collision must never mutate the shared, frozen
        per-trigger defaults -- calling plan_composition() again for the
        same trigger after a resolution pass must return the original,
        untouched value."""
        candidates = [
            self._cand(1, "impossibility"),
            self._cand(2, "betrayal"),
            self._cand(3, "discovery"),
        ]
        resolve_composition_distinctness(candidates)

        assert plan_composition("betrayal")["focal_zone"] == "center_right"
        assert plan_composition("discovery")["focal_zone"] == "center_right"
        assert plan_composition("impossibility")["focal_zone"] == "center_right"

    def test_no_op_when_composition_plan_missing(self):
        """Candidates without a composition_plan (Shorts, legacy-mode) are
        skipped safely -- no KeyError, no crash."""
        candidates = [
            {"rank": 1, "candidate_id": "a"},
            {"rank": 2, "candidate_id": "b"},
        ]
        resolve_composition_distinctness(candidates)  # must not raise
        assert "composition_plan" not in candidates[0]
        assert "composition_plan" not in candidates[1]

    def test_empty_list_is_safe(self):
        resolve_composition_distinctness([])  # must not raise

    def test_single_candidate_is_a_no_op(self):
        candidates = [self._cand(1, "impossibility")]
        before = dict(candidates[0]["composition_plan"])
        resolve_composition_distinctness(candidates)
        assert candidates[0]["composition_plan"] == before

    def test_resolved_candidates_pass_validate_thumbnail_dna(self):
        """Every reassigned focal_zone must still satisfy
        validate_thumbnail_dna()'s text_zone-overlap check (focal_zone !=
        text_zone) -- compatibility with the composition validator."""
        candidates = [
            self._cand(1, "impossibility"),
            self._cand(2, "betrayal"),
            self._cand(3, "discovery"),
        ]
        resolve_composition_distinctness(candidates)

        for c in candidates:
            result = validate_thumbnail_dna(c)
            assert result["passed"], result["errors"]
            assert c["composition_plan"]["focal_zone"] != c["composition_plan"]["text_zone"]

    def test_resolved_candidates_pass_typography_pre_generation_validator(self):
        """Reassigning focal_zone must not disturb typography_plan.zone
        consistency -- compatibility with the typography validator.
        typography_plan is derived from text_zone (fixed at 'lower_left'
        for every trigger), never from focal_zone, so this must hold
        trivially, but is verified directly rather than assumed."""
        candidates = [
            self._cand(1, "impossibility"),
            self._cand(2, "betrayal"),
            self._cand(3, "discovery"),
        ]
        for c in candidates:
            c["typography_plan"] = plan_typography(
                "IT SHOULDN'T EXIST", c["emotional_trigger"], c["composition_plan"],
            )
            # composite/embedded mode skip the zone-reservation-in-prompt
            # checks (2 and 3); this test only exercises check 1 (zone
            # consistency), which is unaffected by that skip.
        resolve_composition_distinctness(candidates)

        for c in candidates:
            result = validate_composition_typography_pre_generation(c)
            assert c["composition_plan"]["text_zone"] == c["typography_plan"]["zone"]
            assert not any("text_zone" in e and "!=" in e for e in result["errors"])

    def test_resolution_pool_never_includes_text_zone(self):
        """Structural guard: the pool itself must never contain the fixed
        text_zone value, independent of any specific trigger's plan."""
        assert "lower_left" not in FOCAL_ZONE_RESOLUTION_POOL


# ===========================================================================
# TestConceptCandidatePlansDistinctness (end-to-end via write_thumbnail_prompts)
# ===========================================================================

class TestConceptCandidatePlansDistinctness(TestConceptCandidatePlans):
    """
    Reuses TestConceptCandidatePlans' exact concept_brief/generation_plan
    fixtures (impossibility/loss/betrayal -- already a real rank1/rank3
    'center_right' collision) to verify the distinctness resolution runs
    automatically as part of write_thumbnail_prompts(), the real
    candidate-assembly entry point, not just when calling
    resolve_composition_distinctness() directly.
    """

    def test_write_thumbnail_prompts_produces_distinct_focal_zones(self, tmp_path):
        from hypogeum.agents.thumbnail_prompt_writer import write_thumbnail_prompts

        concept_brief   = self._make_concept_brief()
        generation_plan = self._make_generation_plan()
        log             = MagicMock()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=generation_plan):
            config = MagicMock()
            config.TD_PLANNING_MODEL = "test-model"
            config.TD_PLANNING_MAX_TOKENS = 4096
            result = write_thumbnail_prompts(
                handoff_brief={"candidates": [], "short_candidates": []},
                config=config,
                episode_id="ep_test",
                output_dir=tmp_path,
                log=log,
                concept_brief=concept_brief,
            )

        cands = sorted(
            result["main_episode"]["ranked_candidates"], key=lambda c: c["rank"],
        )
        zones = [c["composition_plan"]["focal_zone"] for c in cands]
        assert len(set(zones)) == 3
        # Rank 1 (impossibility) keeps its untouched CTR-first default.
        assert cands[0]["composition_plan"]["focal_zone"] == "center_right"
