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
    generate_and_composite,
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

    def test_composite_mode_true_for_all_candidates(self):
        """Every ranked candidate must have composite_mode=True after attach."""
        plan = _make_generation_plan(3)
        _attach_concept_plans(plan, _make_concept_brief())
        for cand in plan["main_episode"]["ranked_candidates"]:
            assert cand.get("composite_mode") is True, (
                f"candidate rank={cand['rank']} missing composite_mode=True"
            )

    def test_composite_mode_true_regardless_of_trigger(self):
        """composite_mode must be True even when trigger is not 'embedded'-type."""
        for trigger in ("impossibility", "loss", "betrayal", "unknown_trigger"):
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
            assert cand.get("composite_mode") is True, (
                f"composite_mode not True for trigger={trigger!r}"
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
