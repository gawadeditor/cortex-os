"""
tests/test_thumbnail_compositor_regression.py — Regression tests for Track A fix

Assertions:
  1. _attach_concept_plans always sets composite_mode=True for every candidate,
     regardless of typography_integration value.
  2. build_layer_prompts returns no 'typography' key — MUAPI is never called
     for the typography layer.
  3. generate_and_composite never calls _generate_layer with layer_name='typography'.
  4. generate_and_composite calls _apply_wordmark on the composite output.
  5. The PIL typography renderer creates the layer file without any MUAPI call.
  6. The wordmark pixel test: after _apply_wordmark the top-center zone of the
     image contains at least one non-black pixel (wordmark is visible).

No paid API calls. All MUAPI / run_agent paths are mocked.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.thumbnail_compositor import (
    _apply_wordmark,
    _render_typography_pil,
    build_layer_prompts,
    finish_unified_thumbnail,
    generate_and_composite,
    generate_hybrid_thumbnail_experiment,
)
from hypogeum.agents.thumbnail_prompt_writer import _attach_concept_plans


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_concept_brief(trigger: str = "impossibility") -> dict:
    return {
        "concepts": [
            {
                "concept_id": "A",
                "emotional_trigger": trigger,
                "suggested_hook_text": "IT SHOULDN'T EXIST",
                "visual_thesis": "impossible precision",
            },
            {
                "concept_id": "B",
                "emotional_trigger": "loss",
                "suggested_hook_text": "THE KNOWLEDGE VANISHED",
                "visual_thesis": "erasure of memory",
            },
            {
                "concept_id": "C",
                "emotional_trigger": "betrayal",
                "suggested_hook_text": "THEY KNEW",
                "visual_thesis": "hidden truth",
            },
        ]
    }


def _make_generation_plan(num_candidates: int = 3) -> dict:
    return {
        "thumbnail_generation_plan_id": "tgp_test",
        "episode_id": "ep_test",
        "main_episode": {
            "ranked_candidates": [
                {
                    "candidate_id": f"tc_{i}",
                    "rank": i,
                    "image_prompt": f"Scene {i}.",
                    "aspect_ratio": "16:9",
                    "ctr_rationale": "strong",
                    "text_overlay": None,
                }
                for i in range(1, num_candidates + 1)
            ]
        },
        "shorts": [],
    }


def _make_candidate_with_plans() -> dict:
    """Return a candidate dict already populated by _attach_concept_plans."""
    plan = _make_generation_plan(1)
    brief = _make_concept_brief()
    _attach_concept_plans(plan, brief)
    return plan["main_episode"]["ranked_candidates"][0]


def _make_log() -> MagicMock:
    log = MagicMock()
    log.info = MagicMock()
    log.warn = MagicMock()
    log.error = MagicMock()
    return log


# ---------------------------------------------------------------------------
# 1. _attach_concept_plans always sets composite_mode=True
# ---------------------------------------------------------------------------

class TestAttachConceptPlansCompositeMode:
    """
    As of the Thumbnail DNA v1 runtime-integration correction (2026-07-13),
    unified integrated-image generation is the approved primary path:
    composite_mode is now False (routing to
    generate_unified_integrated_thumbnail()) for every recognized
    emotional_trigger, since every entry in
    thumbnail_composition_planner._TRIGGER_MAP carries
    typography_integration="embedded". composite_mode remains True only as
    the safety-net fallback for an unrecognized trigger
    (thumbnail_composition_planner._FALLBACK carries "composited").
    """

    def test_composite_mode_false_for_recognized_triggers(self):
        """Recognized triggers route to the unified path (composite_mode=False)."""
        plan = _make_generation_plan(3)
        _attach_concept_plans(plan, _make_concept_brief())
        for cand in plan["main_episode"]["ranked_candidates"]:
            assert cand.get("composite_mode") is False, (
                f"candidate rank={cand['rank']} expected composite_mode=False (unified path)"
            )
            assert cand.get("typography_integration") == "embedded"

    def test_composite_mode_true_only_for_unrecognized_trigger(self):
        """composite_mode is False for every known trigger, True as the
        fallback safety net for an unrecognized one."""
        for trigger in ("impossibility", "loss", "betrayal"):
            plan = _make_generation_plan(1)
            brief = {
                "concepts": [{
                    "concept_id": "A",
                    "emotional_trigger": trigger,
                    "suggested_hook_text": "TEST HOOK",
                    "visual_thesis": "test",
                }]
            }
            _attach_concept_plans(plan, brief)
            cand = plan["main_episode"]["ranked_candidates"][0]
            assert cand.get("composite_mode") is False, (
                f"composite_mode not False for recognized trigger={trigger!r}"
            )

        plan = _make_generation_plan(1)
        brief = {
            "concepts": [{
                "concept_id": "A",
                "emotional_trigger": "unknown_trigger",
                "suggested_hook_text": "TEST HOOK",
                "visual_thesis": "test",
            }]
        }
        _attach_concept_plans(plan, brief)
        cand = plan["main_episode"]["ranked_candidates"][0]
        assert cand.get("composite_mode") is True, (
            "composite_mode should be True (layered fallback) for an unrecognized trigger"
        )

    def test_layer_prompts_present_for_all_candidates(self):
        """layer_prompts must be a non-empty dict for every candidate."""
        plan = _make_generation_plan(3)
        _attach_concept_plans(plan, _make_concept_brief())
        for cand in plan["main_episode"]["ranked_candidates"]:
            lp = cand.get("layer_prompts")
            assert isinstance(lp, dict) and lp, (
                f"candidate rank={cand['rank']} has empty/missing layer_prompts"
            )

    def test_typography_plan_always_attached(self):
        """typography_plan must be set on every candidate."""
        plan = _make_generation_plan(3)
        _attach_concept_plans(plan, _make_concept_brief())
        for cand in plan["main_episode"]["ranked_candidates"]:
            assert cand.get("typography_plan"), (
                f"candidate rank={cand['rank']} missing typography_plan"
            )


# ---------------------------------------------------------------------------
# 2. build_layer_prompts returns no 'typography' key
# ---------------------------------------------------------------------------

class TestBuildLayerPromptsNoTypography:

    def test_no_typography_key(self):
        """build_layer_prompts must not return a 'typography' key."""
        concept = {
            "concept_id": "A",
            "emotional_trigger": "impossibility",
            "suggested_hook_text": "IT SHOULDN'T EXIST",
            "visual_thesis": "impossible precision",
        }
        from hypogeum.agents.thumbnail_composition_planner import plan_composition
        comp_plan = plan_composition("impossibility")
        result = build_layer_prompts(concept, comp_plan)
        assert "typography" not in result, (
            "build_layer_prompts returned a 'typography' key — MUAPI must not "
            "be used for typography rendering"
        )

    def test_required_visual_keys_present(self):
        """base_scene, hero_subject, fx must all be present and non-empty."""
        concept = {
            "concept_id": "A",
            "emotional_trigger": "impossibility",
            "suggested_hook_text": "IT SHOULDN'T EXIST",
            "visual_thesis": "test",
        }
        from hypogeum.agents.thumbnail_composition_planner import plan_composition
        comp_plan = plan_composition("impossibility")
        result = build_layer_prompts(concept, comp_plan)
        for key in ("base_scene", "hero_subject", "fx"):
            assert key in result and result[key], f"'{key}' missing from build_layer_prompts"


# ---------------------------------------------------------------------------
# 3. generate_and_composite never calls _generate_layer with 'typography'
# ---------------------------------------------------------------------------

class TestGenerateCompositeNoMuapiTypography:

    def test_generate_layer_never_called_with_typography(self, tmp_path):
        """_generate_layer must not be called with layer_name='typography'."""
        candidate = _make_candidate_with_plans()
        config    = MagicMock()
        log       = _make_log()

        # Create a fake base_scene image so compositing doesn't fail
        _make_fake_layer(tmp_path, "ep003_A_base_scene.jpg")

        layer_call_names = []

        def fake_generate_layer(layer_name, prompt, deliverable_id, cfg, out_dir, lg):
            layer_call_names.append(layer_name)
            if layer_name == "base_scene":
                return _make_fake_layer(out_dir, f"{deliverable_id}_{layer_name}.jpg")
            return None  # hero_subject and fx are optional

        with patch(
            "hypogeum.agents.thumbnail_compositor._generate_layer",
            side_effect=fake_generate_layer,
        ):
            generate_and_composite(
                candidate, config, "ep_test", tmp_path, "ep003_A", log
            )

        assert "typography" not in layer_call_names, (
            f"_generate_layer was called with 'typography' — MUAPI must not be "
            f"used for typography. Calls: {layer_call_names}"
        )

    def test_generate_layer_called_for_visual_layers(self, tmp_path):
        """_generate_layer must be called for base_scene, hero_subject, and fx."""
        candidate = _make_candidate_with_plans()
        config    = MagicMock()
        log       = _make_log()

        layer_call_names = []

        def fake_generate_layer(layer_name, prompt, deliverable_id, cfg, out_dir, lg):
            layer_call_names.append(layer_name)
            if layer_name == "base_scene":
                return _make_fake_layer(out_dir, f"{deliverable_id}_{layer_name}.jpg")
            return None

        with patch(
            "hypogeum.agents.thumbnail_compositor._generate_layer",
            side_effect=fake_generate_layer,
        ):
            generate_and_composite(
                candidate, config, "ep_test", tmp_path, "ep003_A", log
            )

        for layer in ("base_scene", "hero_subject", "fx"):
            assert layer in layer_call_names, (
                f"_generate_layer was not called for '{layer}'"
            )


# ---------------------------------------------------------------------------
# 4. _apply_wordmark is called on composite output
# ---------------------------------------------------------------------------

class TestWordmarkApplied:

    def test_apply_wordmark_called_on_success(self, tmp_path):
        """_apply_wordmark must be called on the composite output path."""
        candidate = _make_candidate_with_plans()
        config    = MagicMock()
        log       = _make_log()

        def fake_generate_layer(layer_name, prompt, deliverable_id, cfg, out_dir, lg):
            if layer_name == "base_scene":
                return _make_fake_layer(out_dir, f"{deliverable_id}_{layer_name}.jpg")
            return None

        with patch("hypogeum.agents.thumbnail_compositor._generate_layer",
                   side_effect=fake_generate_layer), \
             patch("hypogeum.agents.thumbnail_compositor._apply_wordmark") as mock_wm:
            generate_and_composite(
                candidate, config, "ep_test", tmp_path, "ep003_A", log
            )

        mock_wm.assert_called_once()
        called_path = mock_wm.call_args[0][0]
        assert str(called_path).endswith("ep003_A.jpg"), (
            f"_apply_wordmark called with unexpected path: {called_path}"
        )

    def test_apply_wordmark_not_called_when_base_fails(self, tmp_path):
        """_apply_wordmark must NOT be called when base_scene generation fails."""
        candidate = _make_candidate_with_plans()
        config    = MagicMock()
        log       = _make_log()

        with patch("hypogeum.agents.thumbnail_compositor._generate_layer",
                   return_value=None), \
             patch("hypogeum.agents.thumbnail_compositor._apply_wordmark") as mock_wm:
            result = generate_and_composite(
                candidate, config, "ep_test", tmp_path, "ep003_A", log
            )

        assert result is False
        mock_wm.assert_not_called()


# ---------------------------------------------------------------------------
# EXPERIMENTAL — generate_hybrid_thumbnail_experiment()
#
# Isolated hybrid path (docs/validation/thumbnail_image_generation_validation/
# COMPOSITE_PIPELINE_QUALITY_AUDIT.md): one unified image generation call
# instead of separate base_scene + hero_subject layers. No FX in this
# experiment. Typography and wordmark reuse the same deterministic PIL steps
# as the production layered path. generate_and_composite() itself must be
# completely unaffected by this addition.
# ---------------------------------------------------------------------------

class TestGenerateHybridThumbnailExperiment:

    def _typography_plan(self) -> dict:
        return {"headline": "IT SHOULDN'T", "emphasis": "EXIST", "zone": "lower_left"}

    def test_uses_exactly_one_unified_image_generation_call(self, tmp_path):
        """The hybrid path must call _generate_thumbnail_with_model exactly once."""
        config = MagicMock()
        config.THUMBNAIL_IMAGE_MODEL = "gpt-image-2-text-to-image"
        log = _make_log()

        def fake_generate(*, model, deliverable_id, image_prompt, aspect_ratio,
                           config, local_path, budget_guard, log):
            from PIL import Image
            local_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (1280, 720), (10, 8, 5)).save(str(local_path), "JPEG", quality=80)

        with patch("hypogeum.agents.thumbnail_compositor._generate_thumbnail_with_model",
                   side_effect=fake_generate) as mock_gen, \
             patch("hypogeum.agents.thumbnail_compositor._normalize_thumbnail"):
            generate_hybrid_thumbnail_experiment(
                unified_image_prompt="unified scene prompt",
                typography_plan=self._typography_plan(),
                config=config, episode_id="ep_test", output_dir=tmp_path,
                deliverable_id="hybrid_test", log=log,
            )

        mock_gen.assert_called_once()
        assert mock_gen.call_args.kwargs["model"] == "gpt-image-2-text-to-image"

    def test_base_scene_and_hero_subject_not_generated_separately(self, tmp_path):
        """_generate_layer (the per-layer production function) must never be
        called by the hybrid path — it does not use layer_prompts at all."""
        config = MagicMock()
        config.THUMBNAIL_IMAGE_MODEL = "gpt-image-2-text-to-image"
        log = _make_log()

        def fake_generate(*, model, deliverable_id, image_prompt, aspect_ratio,
                           config, local_path, budget_guard, log):
            from PIL import Image
            local_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (1280, 720), (10, 8, 5)).save(str(local_path), "JPEG", quality=80)

        with patch("hypogeum.agents.thumbnail_compositor._generate_thumbnail_with_model",
                   side_effect=fake_generate), \
             patch("hypogeum.agents.thumbnail_compositor._normalize_thumbnail"), \
             patch("hypogeum.agents.thumbnail_compositor._generate_layer") as mock_layer:
            generate_hybrid_thumbnail_experiment(
                unified_image_prompt="unified scene prompt",
                typography_plan=self._typography_plan(),
                config=config, episode_id="ep_test", output_dir=tmp_path,
                deliverable_id="hybrid_test", log=log,
            )

        mock_layer.assert_not_called()

    def test_typography_still_uses_pil(self, tmp_path):
        """_render_typography_pil must still be called — typography stays
        deterministic in the hybrid path."""
        config = MagicMock()
        config.THUMBNAIL_IMAGE_MODEL = "gpt-image-2-text-to-image"
        log = _make_log()

        def fake_generate(*, model, deliverable_id, image_prompt, aspect_ratio,
                           config, local_path, budget_guard, log):
            from PIL import Image
            local_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (1280, 720), (10, 8, 5)).save(str(local_path), "JPEG", quality=80)

        with patch("hypogeum.agents.thumbnail_compositor._generate_thumbnail_with_model",
                   side_effect=fake_generate), \
             patch("hypogeum.agents.thumbnail_compositor._normalize_thumbnail"), \
             patch("hypogeum.agents.thumbnail_compositor._render_typography_pil") as mock_typo:
            mock_typo.return_value = None
            generate_hybrid_thumbnail_experiment(
                unified_image_prompt="unified scene prompt",
                typography_plan=self._typography_plan(),
                config=config, episode_id="ep_test", output_dir=tmp_path,
                deliverable_id="hybrid_test", log=log,
            )

        mock_typo.assert_called_once()
        assert mock_typo.call_args[0][2] == self._typography_plan()

    def test_wordmark_still_applies(self, tmp_path):
        """_apply_wordmark must still be called on the composite output."""
        config = MagicMock()
        config.THUMBNAIL_IMAGE_MODEL = "gpt-image-2-text-to-image"
        log = _make_log()

        def fake_generate(*, model, deliverable_id, image_prompt, aspect_ratio,
                           config, local_path, budget_guard, log):
            from PIL import Image
            local_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (1280, 720), (10, 8, 5)).save(str(local_path), "JPEG", quality=80)

        with patch("hypogeum.agents.thumbnail_compositor._generate_thumbnail_with_model",
                   side_effect=fake_generate), \
             patch("hypogeum.agents.thumbnail_compositor._normalize_thumbnail"), \
             patch("hypogeum.agents.thumbnail_compositor._apply_wordmark") as mock_wm:
            generate_hybrid_thumbnail_experiment(
                unified_image_prompt="unified scene prompt",
                typography_plan=self._typography_plan(),
                config=config, episode_id="ep_test", output_dir=tmp_path,
                deliverable_id="hybrid_test", log=log,
            )

        mock_wm.assert_called_once()
        called_path = mock_wm.call_args[0][0]
        assert str(called_path).endswith("hybrid_test.jpg")

    def test_returns_false_on_generation_failure_no_fallback_attempted(self, tmp_path):
        """On failure, the hybrid path returns False and never attempts a
        fallback provider/model of any kind."""
        config = MagicMock()
        config.THUMBNAIL_IMAGE_MODEL = "gpt-image-2-text-to-image"
        log = _make_log()

        with patch("hypogeum.agents.thumbnail_compositor._generate_thumbnail_with_model",
                   side_effect=RuntimeError("provider error")):
            result = generate_hybrid_thumbnail_experiment(
                unified_image_prompt="unified scene prompt",
                typography_plan=self._typography_plan(),
                config=config, episode_id="ep_test", output_dir=tmp_path,
                deliverable_id="hybrid_test", log=log,
            )

        assert result is False

    def test_no_openrouter_or_openai_reference_in_module(self):
        """The module implementing the hybrid path must never reference the
        single-image provider-chain functions — GPT Image 2 only, by
        construction, matching the production layered path's guarantee."""
        import inspect
        import hypogeum.agents.thumbnail_compositor as mod
        source = inspect.getsource(mod)
        assert "_generate_thumbnail_openrouter" not in source
        assert "_generate_thumbnail_openai" not in source

    def test_production_layered_path_unaffected(self, tmp_path):
        """generate_and_composite() must behave identically to before this
        addition — still uses _generate_layer for all three visual layers."""
        candidate = _make_candidate_with_plans()
        config = MagicMock()
        log = _make_log()

        layer_call_names = []

        def fake_generate_layer(layer_name, prompt, deliverable_id, cfg, out_dir, lg):
            layer_call_names.append(layer_name)
            if layer_name == "base_scene":
                return _make_fake_layer(out_dir, f"{deliverable_id}_{layer_name}.jpg")
            return None

        with patch("hypogeum.agents.thumbnail_compositor._generate_layer",
                   side_effect=fake_generate_layer):
            generate_and_composite(candidate, config, "ep_test", tmp_path, "ep003_A", log)

        for layer in ("base_scene", "hero_subject", "fx"):
            assert layer in layer_call_names

    def test_final_output_dimensions_1280x720(self, tmp_path):
        """The final composite must be 1280x720."""
        from PIL import Image
        config = MagicMock()
        config.THUMBNAIL_IMAGE_MODEL = "gpt-image-2-text-to-image"
        log = _make_log()

        def fake_generate(*, model, deliverable_id, image_prompt, aspect_ratio,
                           config, local_path, budget_guard, log):
            from PIL import Image
            local_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (1280, 720), (10, 8, 5)).save(str(local_path), "JPEG", quality=80)

        with patch("hypogeum.agents.thumbnail_compositor._generate_thumbnail_with_model",
                   side_effect=fake_generate), \
             patch("hypogeum.agents.thumbnail_compositor._normalize_thumbnail"):
            generate_hybrid_thumbnail_experiment(
                unified_image_prompt="unified scene prompt",
                typography_plan=self._typography_plan(),
                config=config, episode_id="ep_test", output_dir=tmp_path,
                deliverable_id="hybrid_test", log=log,
            )

        out_path = tmp_path / "thumbnails" / "hybrid_test.jpg"
        assert out_path.exists()
        img = Image.open(out_path)
        assert img.size == (1280, 720)

    def test_preview_resize_produces_320x180(self, tmp_path):
        """A 320px-wide preview derived from a 1280x720 final thumbnail must
        be exactly 320x180 (16:9 preserved)."""
        from PIL import Image
        src = Image.new("RGB", (1280, 720), (10, 8, 5))
        ratio = 320 / src.width
        preview = src.resize((320, round(src.height * ratio)))
        assert preview.size == (320, 180)


# ---------------------------------------------------------------------------
# EXPERIMENTAL — finish_unified_thumbnail()
#
# Finishes an ALREADY-GENERATED unified source image with typography +
# wordmark only — zero provider calls, no color grade, no vignette, no
# shadow-floor clamp (docs/validation/thumbnail_image_generation_validation/
# COMPOSITE_PIPELINE_QUALITY_AUDIT.md). Must not regenerate anything and
# must not touch the production layered path.
# ---------------------------------------------------------------------------

class TestFinishUnifiedThumbnail:

    def _typography_plan(self) -> dict:
        return {"headline": "IT SHOULDN'T", "emphasis": "EXIST", "zone": "lower_left"}

    def _make_unified_source(self, tmp_path, color=(120, 90, 40)) -> Path:
        from PIL import Image
        p = tmp_path / "thumbnails" / "layers" / "src_unified.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1280, 720), color).save(str(p), "JPEG", quality=95)
        return p

    def test_zero_provider_calls(self, tmp_path):
        """No MUAPI/provider function may be called by this function."""
        log = _make_log()
        unified = self._make_unified_source(tmp_path)
        with patch("hypogeum.agents.thumbnail_compositor._generate_thumbnail_with_model") as mock_gen, \
             patch("hypogeum.agents.thumbnail_compositor._generate_layer") as mock_layer:
            finish_unified_thumbnail(
                unified_path=unified, typography_plan=self._typography_plan(),
                output_dir=tmp_path, deliverable_id="finish_test", log=log,
            )
        mock_gen.assert_not_called()
        mock_layer.assert_not_called()

    def test_no_shadow_floor_clamp_applied(self, tmp_path):
        """_shadow_floor_clamp must never be called by this function."""
        log = _make_log()
        unified = self._make_unified_source(tmp_path)
        with patch("hypogeum.agents.thumbnail_compositor._shadow_floor_clamp") as mock_clamp:
            finish_unified_thumbnail(
                unified_path=unified, typography_plan=self._typography_plan(),
                output_dir=tmp_path, deliverable_id="finish_test", log=log,
            )
        mock_clamp.assert_not_called()

    def test_no_vignette_or_color_grade_applied(self, tmp_path):
        """_apply_vignette must never be called by this function."""
        log = _make_log()
        unified = self._make_unified_source(tmp_path)
        with patch("hypogeum.agents.thumbnail_compositor._apply_vignette") as mock_vig:
            finish_unified_thumbnail(
                unified_path=unified, typography_plan=self._typography_plan(),
                output_dir=tmp_path, deliverable_id="finish_test", log=log,
            )
        mock_vig.assert_not_called()

    def test_source_pixels_unchanged_outside_typography_and_wordmark_zones(self, tmp_path):
        """A pixel well outside both the typography zone (left ~4-42% x,
        30-88% y) and the wordmark zone (top 12%, centered) must be
        unchanged within normal JPEG re-encoding tolerance."""
        from PIL import Image
        color = (120, 90, 40)
        unified = self._make_unified_source(tmp_path, color=color)
        log = _make_log()
        finish_unified_thumbnail(
            unified_path=unified, typography_plan=self._typography_plan(),
            output_dir=tmp_path, deliverable_id="finish_test", log=log,
        )
        out_path = tmp_path / "thumbnails" / "finish_test.jpg"
        assert out_path.exists()
        img = Image.open(out_path).convert("RGB")
        # Right-center point: x=900 (>42% width, outside typography zone,
        # right of hero region too), y=400 (>12% height, outside wordmark zone).
        px = img.getpixel((900, 400))
        for channel_out, channel_in in zip(px, color):
            assert abs(channel_out - channel_in) <= 6, (
                f"pixel outside typography/wordmark zones changed too much: {px} vs {color}"
            )

    def test_typography_applied(self, tmp_path):
        log = _make_log()
        unified = self._make_unified_source(tmp_path)
        with patch("hypogeum.agents.thumbnail_compositor._render_typography_pil") as mock_typo:
            mock_typo.return_value = None
            finish_unified_thumbnail(
                unified_path=unified, typography_plan=self._typography_plan(),
                output_dir=tmp_path, deliverable_id="finish_test", log=log,
            )
        mock_typo.assert_called_once()
        assert mock_typo.call_args[0][2] == self._typography_plan()

    def test_wordmark_applied(self, tmp_path):
        log = _make_log()
        unified = self._make_unified_source(tmp_path)
        with patch("hypogeum.agents.thumbnail_compositor._apply_wordmark") as mock_wm:
            finish_unified_thumbnail(
                unified_path=unified, typography_plan=self._typography_plan(),
                output_dir=tmp_path, deliverable_id="finish_test", log=log,
            )
        mock_wm.assert_called_once()
        assert str(mock_wm.call_args[0][0]).endswith("finish_test.jpg")

    def test_returns_false_when_source_missing(self, tmp_path):
        log = _make_log()
        missing = tmp_path / "thumbnails" / "layers" / "does_not_exist.jpg"
        result = finish_unified_thumbnail(
            unified_path=missing, typography_plan=self._typography_plan(),
            output_dir=tmp_path, deliverable_id="finish_test", log=log,
        )
        assert result is False

    def test_final_output_dimensions_1280x720(self, tmp_path):
        from PIL import Image
        log = _make_log()
        unified = self._make_unified_source(tmp_path)
        finish_unified_thumbnail(
            unified_path=unified, typography_plan=self._typography_plan(),
            output_dir=tmp_path, deliverable_id="finish_test", log=log,
        )
        out_path = tmp_path / "thumbnails" / "finish_test.jpg"
        assert Image.open(out_path).size == (1280, 720)

    def test_preview_resize_produces_320x180(self, tmp_path):
        from PIL import Image
        log = _make_log()
        unified = self._make_unified_source(tmp_path)
        finish_unified_thumbnail(
            unified_path=unified, typography_plan=self._typography_plan(),
            output_dir=tmp_path, deliverable_id="finish_test", log=log,
        )
        out_path = tmp_path / "thumbnails" / "finish_test.jpg"
        img = Image.open(out_path)
        ratio = 320 / img.width
        preview = img.resize((320, round(img.height * ratio)))
        assert preview.size == (320, 180)

    def test_production_layered_path_unchanged(self, tmp_path):
        """generate_and_composite() must still use _generate_layer for all
        three visual layers — unaffected by finish_unified_thumbnail()."""
        candidate = _make_candidate_with_plans()
        config = MagicMock()
        log = _make_log()

        layer_call_names = []

        def fake_generate_layer(layer_name, prompt, deliverable_id, cfg, out_dir, lg):
            layer_call_names.append(layer_name)
            if layer_name == "base_scene":
                return _make_fake_layer(out_dir, f"{deliverable_id}_{layer_name}.jpg")
            return None

        with patch("hypogeum.agents.thumbnail_compositor._generate_layer",
                   side_effect=fake_generate_layer):
            generate_and_composite(candidate, config, "ep_test", tmp_path, "ep003_A", log)

        for layer in ("base_scene", "hero_subject", "fx"):
            assert layer in layer_call_names


# ---------------------------------------------------------------------------
# 5. PIL typography renderer creates layer file without MUAPI
# ---------------------------------------------------------------------------

class TestRenderTypographyPil:

    def test_creates_layer_file(self, tmp_path):
        """_render_typography_pil must create the typography layer JPEG."""
        log = _make_log()
        typo_plan = {"headline": "IT SHOULDN'T", "emphasis": "EXIST"}
        path = _render_typography_pil(tmp_path, "ep003_A", typo_plan, log)
        assert path is not None, "_render_typography_pil returned None"
        assert path.exists(), f"Typography layer file not created: {path}"
        assert path.stat().st_size > 0

    def test_layer_file_is_valid_jpeg(self, tmp_path):
        """The typography layer file must be a valid JPEG image."""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not available")
        log = _make_log()
        typo_plan = {"headline": "THE KNOWLEDGE", "emphasis": "VANISHED"}
        path = _render_typography_pil(tmp_path, "ep003_B", typo_plan, log)
        assert path is not None
        img = Image.open(path)
        assert img.mode == "RGB"
        assert img.size == (1280, 720)

    def test_no_muapi_import_in_typography_render(self, tmp_path):
        """_render_typography_pil must not import or call any MUAPI function."""
        log = _make_log()
        typo_plan = {"headline": "THEY", "emphasis": "KNEW"}
        with patch("hypogeum.agents.thumbnail_compositor._generate_thumbnail_with_model") as mock_muapi:
            _render_typography_pil(tmp_path, "ep003_C", typo_plan, log)
        mock_muapi.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Wordmark pixel test — top-center zone is non-black after _apply_wordmark
# ---------------------------------------------------------------------------

class TestWordmarkPixelPresence:

    def test_wordmark_visible_in_top_zone(self, tmp_path):
        """After _apply_wordmark, top-center pixels must not all be pure black."""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not available")

        log = _make_log()

        # Create a pure-black 1280×720 image to start
        black_path = tmp_path / "thumbnails" / "test_wm.jpg"
        black_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1280, 720), (0, 0, 0)).save(str(black_path), "JPEG", quality=95)

        _apply_wordmark(black_path, log)

        img = Image.open(black_path).convert("RGB")
        W, H = img.size

        # Sample top-center strip: x in [30%, 70%], y in [0, 12%]
        x0, x1 = int(W * 0.30), int(W * 0.70)
        y0, y1 = 0, int(H * 0.12)

        max_brightness = 0
        for y in range(y0, y1, 2):
            for x in range(x0, x1, 2):
                r, g, b = img.getpixel((x, y))
                max_brightness = max(max_brightness, r, g, b)

        assert max_brightness > 20, (
            f"Wordmark zone is pure black (max brightness={max_brightness}). "
            "Wordmark was not rendered."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_layer(output_dir: Path, filename: str) -> Path:
    """Create a tiny valid 1280×720 black JPEG to stand in for a MUAPI layer."""
    try:
        from PIL import Image
        layers_dir = output_dir / "thumbnails" / "layers"
        layers_dir.mkdir(parents=True, exist_ok=True)
        p = layers_dir / filename
        Image.new("RGB", (1280, 720), (10, 8, 5)).save(str(p), "JPEG", quality=80)
        return p
    except ImportError:
        # If PIL not available, write a minimal fake JPEG header
        layers_dir = output_dir / "thumbnails" / "layers"
        layers_dir.mkdir(parents=True, exist_ok=True)
        p = layers_dir / filename
        p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9")
        return p
