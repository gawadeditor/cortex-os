"""
tests/test_thumbnail_unified_integrated_path.py — Unified integrated-image
thumbnail path tests (Thumbnail DNA v1 runtime-integration correction, 2026-07-13)

Covers the gap identified in the runtime audit: the successful integrated-text
thumbnails (ep003_tsc_A_integrated_text01.jpg, ep003_tsc_A_v3_review01.jpg)
were produced by manual validation scripts calling _generate_thumbnail_with_model()
directly — no reusable production function assembled DNA + brand directives
automatically. This file proves the correction:

  hypogeum.agents.thumbnail_compositor.generate_unified_integrated_thumbnail()

is now that reusable function, wired into normal production routing via
thumbnail_generation_agent.generate_thumbnails() and
thumbnail_prompt_writer._attach_concept_plans(), and that
thumbnail_plan_validator.validate_thumbnail_dna() is actually invoked by the
production pre-generation gate in thumbnail_director.py.

Zero provider calls anywhere in this file.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.agents.thumbnail_composition_planner import plan_composition
from hypogeum.agents.thumbnail_typography_planner import plan_typography


class _FakeConfig:
    THUMBNAIL_IMAGE_MODEL = "gpt-image-2-text-to-image"


class _FakeLog:
    def __init__(self):
        self.entries = []

    def info(self, event, **kwargs):
        self.entries.append(("info", event, kwargs))

    def warn(self, event, **kwargs):
        self.entries.append(("warn", event, kwargs))

    def error(self, event, **kwargs):
        self.entries.append(("error", event, kwargs))


def _make_candidate(image_prompt="A dark ancient chamber with a mysterious mechanism.",
                     trigger="impossibility", hook="IT SHOULDN'T EXIST"):
    comp_plan = plan_composition(trigger)
    typo_plan = plan_typography(hook, trigger, comp_plan)
    return {
        "candidate_id": "c1",
        "rank": 1,
        "image_prompt": image_prompt,
        "typography_plan": typo_plan,
        "composition_plan": comp_plan,
    }


def _run_unified(candidate, tmp_path, apply_wordmark=False):
    """Run generate_unified_integrated_thumbnail() with the provider call
    mocked out — captures the exact prompt string that would have been sent."""
    import hypogeum.agents.thumbnail_compositor as tc

    captured = {}

    def fake_generate(model, deliverable_id, image_prompt, aspect_ratio, config,
                       local_path, budget_guard, log):
        captured["prompt"] = image_prompt
        captured["model"] = model
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"fake-image-bytes")

    with patch.object(tc, "_generate_thumbnail_with_model", side_effect=fake_generate), \
         patch.object(tc, "_normalize_thumbnail"):
        ok = tc.generate_unified_integrated_thumbnail(
            candidate=candidate,
            config=_FakeConfig(),
            episode_id="ep_003",
            output_dir=tmp_path,
            deliverable_id="test01",
            log=_FakeLog(),
            apply_wordmark=apply_wordmark,
        )
    return ok, captured


# ---------------------------------------------------------------------------
# 1-4: prompt composition — DNA, brand, typography, exactly once each
# ---------------------------------------------------------------------------

class TestUnifiedPromptComposition:
    def test_prompt_contains_dna_directive(self, tmp_path):
        ok, captured = _run_unified(_make_candidate(), tmp_path)
        assert ok is True
        assert "THUMBNAIL COMPOSITION DNA" in captured["prompt"]

    def test_prompt_contains_compact_brand_enforcement(self, tmp_path):
        # 2026-07-13 (V5.1 provider-safe prompt correction): the unified path
        # now uses the compact brand directive, not the verbose/full one —
        # the compact form preserves the same palette ratios (still a
        # FULL-tier percentage target, not adaptive/light) with shorter prose.
        ok, captured = _run_unified(_make_candidate(), tmp_path)
        assert "HYPOGEUM BRAND (thumbnail, compact)" in captured["prompt"]
        assert "60-70%" in captured["prompt"]

    def test_integrated_headline_instructions_present(self, tmp_path):
        ok, captured = _run_unified(_make_candidate(), tmp_path)
        assert "INTEGRATED TYPOGRAPHY" in captured["prompt"]
        assert "IT SHOULDN'T" in captured["prompt"] or "IT SHOULDNT" in captured["prompt"].upper()
        assert "EXIST" in captured["prompt"]

    def test_each_directive_present_exactly_once(self, tmp_path):
        ok, captured = _run_unified(_make_candidate(), tmp_path)
        p = captured["prompt"]
        assert p.count("INTEGRATED TYPOGRAPHY") == 1
        assert p.count("THUMBNAIL COMPOSITION DNA") == 1
        assert p.count("HYPOGEUM BRAND") == 1

    def test_original_topic_prompt_is_exact_unmutated_prefix(self, tmp_path):
        base = "A dark ancient chamber with a mysterious mechanism."
        ok, captured = _run_unified(_make_candidate(image_prompt=base), tmp_path)
        assert captured["prompt"].startswith(base)

    def test_component_order_topic_then_typography_then_dna_then_brand(self, tmp_path):
        ok, captured = _run_unified(_make_candidate(), tmp_path)
        p = captured["prompt"]
        assert 0 == p.find("A dark ancient chamber")
        assert p.find("INTEGRATED TYPOGRAPHY") < p.find("THUMBNAIL COMPOSITION DNA")
        assert p.find("THUMBNAIL COMPOSITION DNA") < p.find("HYPOGEUM BRAND")

    def test_prompt_length_under_safe_ceiling(self, tmp_path):
        # V5.1 provider-safe ceiling: preferably under 4,500 characters.
        ok, captured = _run_unified(_make_candidate(), tmp_path)
        assert len(captured["prompt"]) < 4500

    def test_single_source_no_duplicated_dna_or_brand_prose_in_the_function(self):
        # The unified function must call the canonical compact builders, not
        # embed copies of DNA/brand prose itself.
        source = Path(
            "hypogeum/agents/thumbnail_compositor.py"
        ).read_text()
        func_start = source.index("def generate_unified_integrated_thumbnail")
        func_end = source.index("\ndef ", func_start + 10)
        func_source = source[func_start:func_end]
        assert "describe_dna_constraints_compact(" in func_source
        assert 'build_visual_identity_directive_compact(mode="thumbnail")' in func_source
        # No hardcoded hex or DNA percentage literals inside the function body.
        assert "60-70%" not in func_source
        assert "#080C0E" not in func_source


# ---------------------------------------------------------------------------
# 6-7: no PIL headline, no layered calls
# ---------------------------------------------------------------------------

class TestUnifiedPathNoLayeredNoPil:
    def test_no_pil_typography_helper_called(self, tmp_path):
        import hypogeum.agents.thumbnail_compositor as tc

        with patch.object(tc, "_render_typography_pil") as mock_pil, \
             patch.object(tc, "_generate_thumbnail_with_model") as mock_gen, \
             patch.object(tc, "_normalize_thumbnail"):
            mock_gen.side_effect = lambda **kw: kw["local_path"].parent.mkdir(
                parents=True, exist_ok=True
            ) or kw["local_path"].write_bytes(b"x")
            tc.generate_unified_integrated_thumbnail(
                candidate=_make_candidate(), config=_FakeConfig(), episode_id="ep_003",
                output_dir=tmp_path, deliverable_id="test01", log=_FakeLog(),
                apply_wordmark=False,
            )
        mock_pil.assert_not_called()

    def test_no_layered_base_hero_fx_calls(self, tmp_path):
        import hypogeum.agents.thumbnail_compositor as tc

        with patch.object(tc, "_generate_layer") as mock_layer, \
             patch.object(tc, "_composite_layers") as mock_composite, \
             patch.object(tc, "_generate_thumbnail_with_model") as mock_gen, \
             patch.object(tc, "_normalize_thumbnail"):
            mock_gen.side_effect = lambda **kw: kw["local_path"].parent.mkdir(
                parents=True, exist_ok=True
            ) or kw["local_path"].write_bytes(b"x")
            tc.generate_unified_integrated_thumbnail(
                candidate=_make_candidate(), config=_FakeConfig(), episode_id="ep_003",
                output_dir=tmp_path, deliverable_id="test01", log=_FakeLog(),
                apply_wordmark=False,
            )
        mock_layer.assert_not_called()
        mock_composite.assert_not_called()

    def test_exactly_one_provider_call(self, tmp_path):
        import hypogeum.agents.thumbnail_compositor as tc

        with patch.object(tc, "_generate_thumbnail_with_model") as mock_gen, \
             patch.object(tc, "_normalize_thumbnail"):
            mock_gen.side_effect = lambda **kw: kw["local_path"].parent.mkdir(
                parents=True, exist_ok=True
            ) or kw["local_path"].write_bytes(b"x")
            tc.generate_unified_integrated_thumbnail(
                candidate=_make_candidate(), config=_FakeConfig(), episode_id="ep_003",
                output_dir=tmp_path, deliverable_id="test01", log=_FakeLog(),
                apply_wordmark=False,
            )
        assert mock_gen.call_count == 1


# ---------------------------------------------------------------------------
# 8: normal production routing selects unified mode
# ---------------------------------------------------------------------------

class TestProductionRouting:
    def test_attach_concept_plans_defaults_to_unified_for_recognized_trigger(self):
        from hypogeum.agents.thumbnail_prompt_writer import _attach_concept_plans

        generation_plan = {
            "main_episode": {"ranked_candidates": [{"candidate_id": "c1", "rank": 1}]}
        }
        concept_brief = {
            "concepts": [{"emotional_trigger": "impossibility", "suggested_hook_text": "IT SHOULDN'T EXIST"}]
        }
        _attach_concept_plans(generation_plan, concept_brief)
        candidate = generation_plan["main_episode"]["ranked_candidates"][0]
        assert candidate["composite_mode"] is False
        assert candidate["typography_integration"] == "embedded"
        # Layered rollback data is still attached, just not used by default.
        assert "layer_prompts" in candidate

    def test_attach_concept_plans_falls_back_to_layered_for_unrecognized_trigger(self):
        from hypogeum.agents.thumbnail_prompt_writer import _attach_concept_plans

        generation_plan = {
            "main_episode": {"ranked_candidates": [{"candidate_id": "c1", "rank": 1}]}
        }
        concept_brief = {
            "concepts": [{"emotional_trigger": "not_a_real_trigger", "suggested_hook_text": "SOMETHING"}]
        }
        _attach_concept_plans(generation_plan, concept_brief)
        candidate = generation_plan["main_episode"]["ranked_candidates"][0]
        assert candidate["composite_mode"] is True

    def test_generate_thumbnails_routes_unified_candidate_to_unified_function(self, tmp_path):
        import hypogeum.agents.thumbnail_generation_agent as tga

        candidate = _make_candidate()
        candidate.update({"candidate_id": "c1", "rank": 1, "composite_mode": False,
                           "typography_integration": "embedded"})
        generation_plan = {
            "main_episode": {"ranked_candidates": [candidate]},
            "shorts": [],
        }

        with patch.object(tga, "generate_unified_integrated_thumbnail") as mock_unified, \
             patch.object(tga, "generate_and_composite") as mock_composite:
            mock_unified.return_value = True
            result = tga.generate_thumbnails(
                generation_plan=generation_plan, config=_FakeConfig(), episode_id="ep_003",
                output_dir=tmp_path, log=_FakeLog(),
            )
        mock_unified.assert_called_once()
        mock_composite.assert_not_called()
        assert result["main_ok"] is True

    def test_generate_thumbnails_still_routes_composite_candidate_to_layered_function(self, tmp_path):
        import hypogeum.agents.thumbnail_generation_agent as tga

        candidate = {
            "candidate_id": "c1", "rank": 1, "composite_mode": True,
            "layer_prompts": {"base_scene": "x", "hero_subject": "y", "fx": "z"},
        }
        generation_plan = {
            "main_episode": {"ranked_candidates": [candidate]},
            "shorts": [],
        }

        with patch.object(tga, "generate_unified_integrated_thumbnail") as mock_unified, \
             patch.object(tga, "generate_and_composite") as mock_composite:
            mock_composite.return_value = True
            result = tga.generate_thumbnails(
                generation_plan=generation_plan, config=_FakeConfig(), episode_id="ep_003",
                output_dir=tmp_path, log=_FakeLog(),
            )
        mock_composite.assert_called_once()
        mock_unified.assert_not_called()
        # Layered path preserved, not removed.
        assert result["main_ok"] is True


# ---------------------------------------------------------------------------
# 9: Antikythera V4 receives the same behavior with no episode-specific code
# ---------------------------------------------------------------------------

class TestAntikytheraV4UnifiedPath:
    def test_v4_candidate_uses_the_same_generic_function_no_special_case(self, tmp_path):
        candidate = _make_candidate(
            image_prompt=(
                "Premium archaeological investigative documentary YouTube thumbnail, 16:9 — "
                "a serious archaeological-mystery editorial poster."
            ),
            trigger="impossibility",
            hook="IT SHOULDN'T EXIST",
        )
        ok, captured = _run_unified(candidate, tmp_path)
        assert ok is True
        assert captured["prompt"].count("HYPOGEUM BRAND") == 1
        assert captured["prompt"].count("THUMBNAIL COMPOSITION DNA") == 1

        # Scope the check to the new function itself — build_layer_prompts()
        # (the pre-existing, unrelated layered path) legitimately contains
        # topic-flavored fallback text for the "impossibility" trigger;
        # generate_unified_integrated_thumbnail() must contain none of it.
        source = Path("hypogeum/agents/thumbnail_compositor.py").read_text()
        func_start = source.index("def generate_unified_integrated_thumbnail")
        func_end = source.index("\ndef ", func_start + 10)
        func_source = source[func_start:func_end].lower()
        for forbidden in ("antikythera", "ep_003", "ep003"):
            assert forbidden not in func_source


# ---------------------------------------------------------------------------
# validate_thumbnail_dna wired into the production pre-generation gate
# ---------------------------------------------------------------------------

class TestZoneReservationChecksSkipEmbeddedTypography:
    """
    Regression coverage for a gap surfaced by flipping composite_mode's
    default: validate_composition_typography_pre_generation()'s "image_prompt
    must mention the reserved text zone / negative space" checks previously
    keyed only on composite_mode=True. Unified-mode candidates
    (composite_mode=False, typography_integration="embedded") equally must
    not be required to describe blank space in their raw image_prompt — the
    typography instruction is appended deterministically at generation time,
    not left for the image model to guess at from zone-reservation prose.
    """

    def test_embedded_typography_skips_zone_mention_check(self):
        from hypogeum.agents.thumbnail_plan_validator import (
            validate_composition_typography_pre_generation,
        )

        comp_plan = plan_composition("impossibility")
        typo_plan = plan_typography("IT SHOULDN'T EXIST", "impossibility", comp_plan)
        candidate = {
            "composition_plan": comp_plan,
            "typography_plan": typo_plan,
            "image_prompt": "A dark chamber with a mechanism.",  # no zone/negative-space phrases
            "composite_mode": False,
            "typography_integration": "embedded",
        }
        result = validate_composition_typography_pre_generation(candidate)
        assert result["passed"] is True

    def test_composited_typography_without_embedded_flag_still_requires_zone_mention(self):
        # Guard against over-broadening the skip: a candidate that is neither
        # composite_mode nor embedded (i.e. the PIL text-overlay LEGACY path)
        # still needs the image model to reserve the zone.
        from hypogeum.agents.thumbnail_plan_validator import (
            validate_composition_typography_pre_generation,
        )

        comp_plan = plan_composition("impossibility")
        typo_plan = plan_typography("IT SHOULDN'T EXIST", "impossibility", comp_plan)
        candidate = {
            "composition_plan": comp_plan,
            "typography_plan": typo_plan,
            "image_prompt": "A dark chamber with a mechanism.",
            "composite_mode": False,
            "typography_integration": "composited",
        }
        result = validate_composition_typography_pre_generation(candidate)
        assert result["passed"] is False
        assert any("reserved text zone" in e for e in result["errors"])


class TestThumbnailDnaGateWiredIntoProduction:
    def test_thumbnail_director_imports_and_calls_validate_thumbnail_dna(self):
        source = Path("hypogeum/agents/thumbnail_director.py").read_text()
        assert "validate_all_candidates_thumbnail_dna" in source

    def test_blocking_dna_failure_raises_before_any_generation(self, monkeypatch, tmp_path):
        # Full end-to-end pre-generation gate test: a candidate with a
        # blocking DNA violation (no eye_path) must raise
        # ThumbnailPreGenerationError before generate_thumbnails() is ever
        # reached — zero provider calls possible.
        import hypogeum.agents.thumbnail_director as td

        bad_comp_plan = plan_composition("impossibility")
        bad_comp_plan["eye_path"] = []  # blocking: missing hierarchy
        good_typo_plan = plan_typography("IT SHOULDN'T EXIST", "impossibility", bad_comp_plan)
        candidates = [{
            "candidate_id": "c1", "rank": 1,
            "composition_plan": bad_comp_plan,
            "typography_plan": good_typo_plan,
            "image_prompt": "x",
            "composite_mode": True,  # skip zone-mention prompt-text checks upstream
        }]

        monkeypatch.setattr(
            td, "write_thumbnail_prompts",
            lambda **kw: {"main_episode": {"ranked_candidates": candidates}, "shorts": []},
        )
        monkeypatch.setattr(
            td, "validate_prompt_distinctness",
            lambda c: {"passed": True, "failures": [], "warnings": []},
        )

        generate_called = {"count": 0}
        monkeypatch.setattr(
            td, "generate_thumbnails",
            lambda **kw: generate_called.__setitem__("count", generate_called["count"] + 1) or {
                "main_asset_id": "x", "main_ok": True, "main_candidates": [], "short_results": []
            },
        )

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        from hypogeum.config import Config
        config = Config()

        with pytest.raises(td.ThumbnailPreGenerationError) as exc_info:
            td.run_thumbnail_director(
                handoff_brief={"candidates": [], "short_candidates": []},
                config=config,
                episode_id="ep_003",
                output_dir=tmp_path,
            )
        assert exc_info.value.validator == "thumbnail_dna"
        assert generate_called["count"] == 0
