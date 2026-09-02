"""
tests/test_brand_visual_identity.py — Canonical brand visual identity tests

Covers:
  - hypogeum.brand.visual_identity: single-source palette loading, directive
    injection into thumbnail / video / reference-image prompt paths
  - hypogeum.brand.palette_validator: perceptual palette compliance checks
  - Absence of reintroduced duplicate hardcoded palette blocks in known
    former-offender modules
  - The Antikythera composition blueprint's stated hierarchy

Zero provider calls anywhere in this file.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from PIL import Image

from hypogeum.brand import visual_identity
from hypogeum.brand.palette_validator import validate_palette_compliance

_REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeLog:
    def __init__(self):
        self.entries = []

    def info(self, event, **kwargs):
        self.entries.append(("info", event, kwargs))

    def warn(self, event, **kwargs):
        self.entries.append(("warn", event, kwargs))

    def error(self, event, **kwargs):
        self.entries.append(("error", event, kwargs))


# ---------------------------------------------------------------------------
# Single canonical source
# ---------------------------------------------------------------------------

class TestCanonicalPaletteSource:
    def test_palette_loads_from_brand_bible_json(self):
        palette = visual_identity.get_palette()
        assert palette["near_black"]["hex"] == "#080C0E"
        assert palette["aged_gold"]["hex"] == "#C49A42"
        assert palette["warm_ivory"]["hex"] == "#E7D6B3"
        assert len(palette) == 7

    def test_palette_reads_from_the_one_canonical_file_not_a_duplicate(self):
        bible_path = (
            _REPO_ROOT / "docs" / "brand_guardian" / "brand_bible_hypogeum.json"
        )
        assert bible_path.exists()
        assert visual_identity._BRAND_BIBLE_PATH == bible_path
        # No competing hypogeum/brand/visual_identity.json was created.
        assert not (_REPO_ROOT / "hypogeum" / "brand" / "visual_identity.json").exists()

    def test_usage_ratios_present_and_sane(self):
        ratios = visual_identity.get_usage_ratios()
        dark_lo, dark_hi = ratios["near_black_and_charcoal"]["target_range_pct"]
        gold_lo, gold_hi = ratios["aged_gold"]["target_range_pct"]
        assert dark_lo < dark_hi
        assert gold_lo < gold_hi
        assert dark_lo > gold_hi  # dark should dominate far more than gold

    def test_forbidden_color_behavior_includes_required_items(self):
        items = visual_identity.get_forbidden_color_behavior()
        joined = " ".join(items).lower()
        for required in ["teal", "verdigris", "sepia", "polished gold", "fluorescent", "saturated"]:
            assert required in joined

    def test_visual_finish_rules_present(self):
        finish = visual_identity.get_visual_finish_rules()
        assert "near-black dominant" in finish["positive"]
        assert "generic fantasy-art finish" in finish["forbidden"]

    def test_brand_spec_version_reported(self):
        assert visual_identity.get_brand_spec_version() == "v1.1"


# ---------------------------------------------------------------------------
# Directive injection
# ---------------------------------------------------------------------------

class TestDirectiveInjection:
    def test_directive_contains_palette_and_forbidden_and_finish(self):
        directive = visual_identity.build_visual_identity_directive("thumbnail")
        assert "#080C0E" in directive
        assert "#C49A42" in directive
        assert "teal lighting" in directive
        assert "generic fantasy-art finish" in directive

    def test_directive_falls_back_to_generic_mode_for_unknown_mode(self):
        directive = visual_identity.build_visual_identity_directive("not_a_real_mode")
        assert "(generic, full enforcement)" in directive

    def test_log_palette_injection_emits_structured_fields(self):
        log = _FakeLog()
        visual_identity.log_palette_injection(log, mode="thumbnail", injected=True)
        assert len(log.entries) == 1
        level, event, fields = log.entries[0]
        assert event == "brand_visual_identity_injected"
        assert fields["brand_spec_version"] == "v1.1"
        assert fields["palette_injected"] is True
        assert fields["applicable_visual_mode"] == "thumbnail"
        assert fields["enforcement_level"] == "full"


# ---------------------------------------------------------------------------
# Context-aware enforcement modes (Brand Bible v1.1 activation, 2026-07-13)
# ---------------------------------------------------------------------------

class TestContextAwareEnforcementModes:
    @pytest.mark.parametrize("mode", [
        "thumbnail", "thumbnail_compositor", "thumbnail_typography",
        "thumbnail_prompt_writer", "endscreen", "channel_artwork", "generic",
    ])
    def test_full_enforcement_modes(self, mode):
        assert visual_identity.get_enforcement_level(mode) == "full"
        directive = visual_identity.build_visual_identity_directive(mode)
        assert "% of frame" in directive
        assert "#080C0E" in directive
        assert f"({mode}, full enforcement)" in directive

    def test_video_scene_is_adaptive(self):
        assert visual_identity.get_enforcement_level("video_scene") == "adaptive"
        directive = visual_identity.build_visual_identity_directive("video_scene")
        assert "(video_scene, adaptive enforcement" in directive
        # No rigid frame-coverage percentage targets for video.
        assert "% of frame" not in directive
        assert "60-70%" not in directive
        # Scene-truth language must be present.
        for phrase in ["scene-truth", "historically", "sea, sky, vegetation"]:
            assert phrase in directive
        # Hard style prohibitions moved to a dedicated forbidden-items list
        # (2026-07-14 Prompt Writer architecture revision) rather than being
        # embedded inline in the positive directive.
        assert "generic fantasy-art finish" not in directive
        assert "generic fantasy-art finish" in visual_identity.get_adaptive_video_forbidden_items()

    def test_reference_image_is_light(self):
        assert visual_identity.get_enforcement_level("reference_image") == "light"
        directive = visual_identity.build_visual_identity_directive("reference_image")
        assert "(reference_image, light enforcement)" in directive
        assert "% of frame" not in directive
        assert "60-70%" not in directive
        assert "entity continuity" in directive.lower()
        assert "priority order" in directive.lower()

    def test_endscreen_and_channel_artwork_reuse_full_mode(self):
        # "Add a reusable mode if no appropriate mode exists" — endscreen and
        # channel_artwork are valid modes even though no generator currently
        # calls them (docs/brand_guardian/hypogeum_endscreen_master.png is a
        # static asset; no endscreen generator exists in the repo today).
        for mode in ("endscreen", "channel_artwork"):
            directive = visual_identity.build_visual_identity_directive(mode)
            assert "% of frame" in directive

    def test_full_and_adaptive_and_light_directives_are_distinct(self):
        full = visual_identity.build_visual_identity_directive("thumbnail")
        adaptive = visual_identity.build_visual_identity_directive("video_scene")
        light = visual_identity.build_visual_identity_directive("reference_image")
        assert full != adaptive != light != full

    def test_unknown_mode_defaults_to_full_not_adaptive_or_light(self):
        # Full enforcement is the safe default for unrecognized modes.
        assert visual_identity.get_enforcement_level("totally_unknown_mode") == "full"


class TestThumbnailPromptsReceiveDirective:
    def test_build_layer_prompts_injects_directive_into_all_three_layers(self):
        from hypogeum.agents.thumbnail_compositor import build_layer_prompts

        prompts = build_layer_prompts(
            {"suggested_hook_text": "IT SHOULDNT EXIST", "visual_thesis": "x",
             "emotional_trigger": "impossibility"},
            {"environment_level": "contextual"},
        )
        for layer_name in ("base_scene", "hero_subject", "fx"):
            assert prompts[layer_name].count("HYPOGEUM BRAND VISUAL IDENTITY") == 1
            assert "(thumbnail_compositor, full enforcement)" in prompts[layer_name]
            assert "#080C0E" in prompts[layer_name]

    def test_embed_typography_in_prompt_is_typography_only(self):
        # As of the unified-integrated-path correction (2026-07-13),
        # _embed_typography_in_prompt() is a pure typography-embedding
        # building block — it does NOT append the brand directive itself.
        # generate_unified_integrated_thumbnail() owns composing the full
        # unified prompt from all required components, each exactly once.
        from hypogeum.agents.thumbnail_prompt_writer import _embed_typography_in_prompt

        out = _embed_typography_in_prompt(
            "A dark ancient chamber.",
            {"headline": "IT SHOULDNT", "emphasis": "EXIST", "zone": "lower_left",
             "emphasis_scale": 1.7},
        )
        assert out.startswith("A dark ancient chamber.")
        assert "INTEGRATED TYPOGRAPHY" in out
        assert "HYPOGEUM BRAND VISUAL IDENTITY" not in out

    def test_write_thumbnail_prompts_system_prompt_includes_directive(self, monkeypatch):
        import hypogeum.agents.thumbnail_prompt_writer as tpw

        captured = {}

        def _fake_run_agent(**kwargs):
            captured["system_prompt"] = kwargs["system_prompt"]
            return {"main_episode": {"ranked_candidates": []}, "shorts": []}

        monkeypatch.setattr(tpw, "run_agent", _fake_run_agent)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        from hypogeum.config import Config

        tpw.write_thumbnail_prompts(
            handoff_brief={"candidates": [], "short_candidates": []},
            config=Config(),
            episode_id="ep_test",
            output_dir=Path("/tmp/does-not-need-to-exist"),
            log=_FakeLog(),
        )
        assert "HYPOGEUM BRAND VISUAL IDENTITY (thumbnail_prompt_writer, full enforcement)" in captured["system_prompt"]


class TestVideoPromptsReceiveDirective:
    def test_submit_scene_appends_directive_to_primary_and_fallback_prompt(self, monkeypatch):
        import hypogeum.muapi as muapi

        captured_prompts = []

        def _fake_attempt_generation(prompt, **kwargs):
            captured_prompts.append(prompt)
            return ({"scene_id": "sc_01", "asset_id": "a1", "generation_status": "generated",
                      "provider": "muapi", "tier_used": 2, "model_used": "m",
                      "prompt_used": prompt, "fallback_used": False, "failure_reason": None}, None)

        monkeypatch.setattr(muapi, "_attempt_generation", _fake_attempt_generation)

        scene_spec = {
            "scene_id": "sc_01",
            "visual_spec": {"aspect_ratio": "16:9", "duration_seconds": 8.0},
            "production_spec": {
                "primary_prompt": "A stone chamber with a bronze mechanism.",
                "fallback_prompt": "A stone chamber, alternate framing.",
                "primary_tier": 2,
                "fallback_tier": 2,
            },
        }

        class _FakeConfig:
            MUAPI_CHEAP_MODE = False
            MUAPI_RUNWAY_ONLY_MODE = False
            MUAPI_DEFAULT_TIER = 2
            MUAPI_ALLOW_PAID_FALLBACKS = False

        output_dir = Path("/tmp/hypogeum-test-submit-scene")
        output_dir.mkdir(parents=True, exist_ok=True)

        muapi.submit_scene(
            scene_spec=scene_spec,
            episode_id="ep_test",
            output_dir=output_dir,
            config=_FakeConfig(),
            budget_guard=None,
            agent="test",
            episode_id_for_asset=None,
        )

        assert captured_prompts, "expected _attempt_generation to be called"
        sent = captured_prompts[0]
        # 2026-07-14 Prompt Writer architecture revision: submit_scene() now
        # assembles prompts via hypogeum.video_prompt_writer's structured
        # 9-section format. Original scene text remains byte-identical as
        # the Scene section's exact body; the brand directive (no
        # frame-coverage percentages) is injected exactly once, inside the
        # BRAND DNA section.
        assert sent.startswith("SCENE: A stone chamber with a bronze mechanism.")
        assert "% of frame" not in sent
        assert sent.count("BRAND DNA:") == 1
        assert "% of frame" not in sent

    def test_entity_reference_image_prompt_receives_directive(self):
        from hypogeum.agents.entity_continuity_manager import build_visual_spec_text

        text = build_visual_spec_text({
            "visual_spec": {
                "description": "A bronze mechanism.",
                "period_detail": "Hellenistic era.",
                "distinctive_markers": ["corroded gears"],
            }
        })
        assert text.startswith("A bronze mechanism. Hellenistic era.")
        assert text.count("HYPOGEUM BRAND VISUAL IDENTITY") == 1
        assert "(reference_image, light enforcement)" in text
        # Reference images must NOT receive thumbnail-strength palette ratios.
        assert "% of frame" not in text

    def test_entity_reference_image_empty_input_receives_no_directive(self):
        # Guard: an entity with no visual_spec content must not produce a
        # directive-only "description" — build_visual_spec_text() must
        # return "" unchanged rather than injecting brand text into nothing.
        from hypogeum.agents.entity_continuity_manager import build_visual_spec_text

        assert build_visual_spec_text({"visual_spec": {}}) == ""
        assert build_visual_spec_text({}) == ""


class TestBrandGuardianReceivesRules:
    def test_brand_guardian_prompt_file_contains_v1_1_forbidden_color_behavior(self):
        from hypogeum.prompt_loader import load_prompt

        body = load_prompt(
            _REPO_ROOT / "hypogeum" / "prompts" / "brand_guardian_agent.txt"
        )
        lowered = body.lower()
        for phrase in ["turquoise verdigris", "full-frame sepia", "bright polished gold",
                       "museum-display finish", "glossy modern product-render"]:
            assert phrase in lowered

    def test_brand_guardian_prompt_is_context_aware_by_pass(self):
        # Pass 1 (video scenes) must carve out natural sky/sea/daylight from
        # the cool-color prohibition; Pass 2 (thumbnails) must not.
        from hypogeum.prompt_loader import load_prompt

        body = load_prompt(
            _REPO_ROOT / "hypogeum" / "prompts" / "brand_guardian_agent.txt"
        )
        assert "Pass 2 (thumbnails):" in body and "ALWAYS BLOCKING" in body
        assert "Pass 1 (video scenes):" in body
        assert "natural daytime sky" in body
        # Fantasy-art / glossy-render finish must remain non-negotiable in
        # both passes — no scene-truth carve-out for rendering style.
        assert "no scene-truth exception" in body.lower() or "never a factual requirement" in body.lower()


# ---------------------------------------------------------------------------
# No duplicate hardcoded palette blocks reintroduced
# ---------------------------------------------------------------------------

class TestNoDuplicateHardcodedPaletteBlocks:
    _KNOWN_FORMER_OFFENDER_HEX_TUPLES = [
        "(196, 154, 66)",   # antique_gold — now sourced via visual_identity
        "(231, 214, 179)",  # warm_gold
        "(245, 243, 238)",  # parchment_cream
        "(8, 12, 14)",      # void_black
    ]

    @pytest.mark.parametrize("relative_path", [
        "hypogeum/agents/thumbnail_compositor.py",
        "hypogeum/muapi.py",
    ])
    def test_hex_tuples_not_hardcoded_in_former_offender_files(self, relative_path):
        source = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
        for tup in self._KNOWN_FORMER_OFFENDER_HEX_TUPLES:
            assert tup not in source, (
                f"{relative_path} contains a hardcoded palette tuple {tup} — "
                f"palette values must come from hypogeum.brand.visual_identity"
            )

    def test_visual_identity_module_is_the_only_place_defining_the_palette_dict(self):
        # get_palette() must resolve every alias from the Brand Bible JSON,
        # not from a second hardcoded dict inside visual_identity.py itself.
        source = (_REPO_ROOT / "hypogeum" / "brand" / "visual_identity.py").read_text()
        assert re.search(r'"#[0-9A-Fa-f]{6}"', source) is None, (
            "visual_identity.py must not hardcode hex literals — it must read "
            "them from brand_bible_hypogeum.json"
        )


# ---------------------------------------------------------------------------
# Palette validator
# ---------------------------------------------------------------------------

class TestPaletteValidator:
    def _solid(self, tmp_path, name, rgb):
        path = tmp_path / name
        Image.new("RGB", (160, 90), rgb).save(path)
        return path

    def test_detects_excessive_blue_teal(self, tmp_path):
        path = self._solid(tmp_path, "teal.jpg", (0, 180, 180))
        result = validate_palette_compliance(path)
        assert result["pass"] is False
        assert "blue_or_cyan_or_teal_lighting" in result["forbidden_casts_detected"]

    def test_detects_excessive_orange_gold_wash(self, tmp_path):
        path = self._solid(tmp_path, "orange.jpg", (230, 140, 30))
        result = validate_palette_compliance(path)
        assert result["pass"] is False
        assert "orange_yellow_scene_wash" in result["forbidden_casts_detected"]

    def test_detects_turquoise_verdigris(self, tmp_path):
        path = self._solid(tmp_path, "verdigris.jpg", (30, 200, 170))
        result = validate_palette_compliance(path)
        assert result["pass"] is False
        assert "turquoise_verdigris" in result["forbidden_casts_detected"]

    def test_compliant_near_black_sample_passes(self, tmp_path):
        path = self._solid(tmp_path, "void_black.jpg", (8, 12, 14))
        result = validate_palette_compliance(path)
        assert result["pass"] is True
        assert result["forbidden_casts_detected"] == []

    def test_compliant_dark_plus_restrained_gold_sample_passes(self, tmp_path):
        img = Image.new("RGB", (160, 90), (8, 12, 14))
        for x in range(140, 160):
            for y in range(0, 15):
                img.putpixel((x, y), (196, 154, 66))
        path = tmp_path / "mix.jpg"
        img.save(path)
        result = validate_palette_compliance(path)
        assert result["pass"] is True

    def test_does_not_reject_on_exact_pixel_hex_mismatch(self, tmp_path):
        # A near-black sample one shade off the canonical hex must still pass —
        # the validator is perceptual/tolerance-based, not exact-hex.
        path = self._solid(tmp_path, "near_black_variant.jpg", (14, 16, 18))
        result = validate_palette_compliance(path)
        assert result["pass"] is True

    def test_result_shape_matches_required_fields(self, tmp_path):
        path = self._solid(tmp_path, "black.jpg", (8, 12, 14))
        result = validate_palette_compliance(path)
        for key in ("pass", "dominant_colors", "palette_distribution",
                    "forbidden_casts_detected", "reason"):
            assert key in result

    def test_optional_log_receives_structured_entry(self, tmp_path):
        path = self._solid(tmp_path, "black.jpg", (8, 12, 14))
        log = _FakeLog()
        validate_palette_compliance(path, log=log)
        assert any(event == "palette_compliance_checked" for _, event, _ in log.entries)

    def test_does_not_regenerate_or_call_any_provider(self, tmp_path, monkeypatch):
        # Guard against future regressions: validator must never import muapi.
        source = Path("hypogeum/brand/palette_validator.py").read_text()
        assert "muapi" not in source
        assert "requests" not in source


# ---------------------------------------------------------------------------
# Composition blueprint hierarchy
# ---------------------------------------------------------------------------

class TestCompositionBlueprint:
    def _blueprint_text(self):
        path = (
            _REPO_ROOT / "docs" / "validation" / "thumbnail_image_generation_validation"
            / "ANTIKYTHERA_COMPOSITION_BLUEPRINT_V3.md"
        )
        assert path.exists()
        return path.read_text(encoding="utf-8")

    def test_blueprint_declares_headline_first_hierarchy(self):
        text = self._blueprint_text()
        headline_idx = text.find("**Headline** — first read")
        figure_idx = text.find("second read")
        mechanism_idx = text.find("third read")
        assert -1 < headline_idx < figure_idx < mechanism_idx

    def test_blueprint_prompt_contains_exact_headline_text(self):
        text = self._blueprint_text()
        assert "IT SHOULDN'T\nEXIST" in text

    def test_blueprint_prompt_avoids_perfectly_centered_doorway(self):
        text = self._blueprint_text()
        assert "off-axis" in text or "not centered" in text

    def test_blueprint_includes_canonical_brand_directive_verbatim(self):
        text = self._blueprint_text()
        live_directive = visual_identity.build_visual_identity_directive("thumbnail")
        assert live_directive in text
