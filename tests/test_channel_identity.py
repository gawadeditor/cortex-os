"""
tests/test_channel_identity.py — Phase 4A: Channel Identity Document test suite

Coverage:
    TestSectionPresence     — all 8 required sections exist as H2 headings
    TestPaletteContent      — palette swatches and hex values present
    TestThumbnailContent    — thumbnail rules including face rule and dimensions
    TestTypographyContent   — two-typeface system documented
    TestForbiddenPatterns   — absolute prohibitions documented
    TestNarrationContent    — voice-over model documented
    TestSubtitleContent     — subtitle rules documented
    TestTitleContent        — title and hook rules documented
    TestDisclosureContent   — attribution and banned terms documented
"""

from __future__ import annotations

from pathlib import Path

import pytest

_DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "channel-identity.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    return _DOC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def headings(doc_text) -> list[str]:
    """Return all H2 headings (## ...) from the document."""
    lines = doc_text.splitlines()
    return [line.lstrip("# ").strip() for line in lines if line.startswith("## ")]


# ---------------------------------------------------------------------------
# TestSectionPresence
# ---------------------------------------------------------------------------

class TestSectionPresence:
    REQUIRED_SECTIONS = [
        "Visual Palette",
        "Thumbnail Rules",
        "Typography",
        "Forbidden Visual Patterns",
        "Narration",
        "Subtitle Rules",
        "Title",
        "Disclosure",
    ]

    def test_document_exists(self):
        assert _DOC_PATH.exists(), f"docs/channel-identity.md not found at {_DOC_PATH}"

    def test_all_required_sections_present(self, doc_text):
        missing = []
        for section in self.REQUIRED_SECTIONS:
            if section.lower() not in doc_text.lower():
                missing.append(section)
        assert missing == [], f"Missing sections: {missing}"

    def test_has_eight_or_more_h2_sections(self, headings):
        assert len(headings) >= 8, f"Expected >= 8 H2 sections, found {len(headings)}: {headings}"

    def test_visual_palette_heading(self, headings):
        matches = [h for h in headings if "palette" in h.lower()]
        assert matches, "No H2 heading containing 'palette'"

    def test_thumbnail_rules_heading(self, headings):
        matches = [h for h in headings if "thumbnail" in h.lower()]
        assert matches, "No H2 heading containing 'thumbnail'"

    def test_typography_heading(self, headings):
        matches = [h for h in headings if "typography" in h.lower() or "text" in h.lower()]
        assert matches, "No H2 heading containing 'typography' or 'text'"

    def test_forbidden_patterns_heading(self, headings):
        matches = [h for h in headings if "forbidden" in h.lower()]
        assert matches, "No H2 heading containing 'forbidden'"

    def test_narration_heading(self, headings):
        matches = [h for h in headings if "narration" in h.lower() or "audio" in h.lower()]
        assert matches, "No H2 heading containing 'narration' or 'audio'"

    def test_subtitle_heading(self, headings):
        matches = [h for h in headings if "subtitle" in h.lower()]
        assert matches, "No H2 heading containing 'subtitle'"

    def test_title_heading(self, headings):
        matches = [h for h in headings if "title" in h.lower() or "hook" in h.lower()]
        assert matches, "No H2 heading containing 'title' or 'hook'"

    def test_disclosure_heading(self, headings):
        matches = [h for h in headings if "disclosure" in h.lower() or "attribution" in h.lower()]
        assert matches, "No H2 heading containing 'disclosure' or 'attribution'"


# ---------------------------------------------------------------------------
# TestPaletteContent
# ---------------------------------------------------------------------------

class TestPaletteContent:
    def test_void_black_present(self, doc_text):
        assert "void_black" in doc_text

    def test_warm_gold_present(self, doc_text):
        assert "warm_gold" in doc_text

    def test_parchment_cream_present(self, doc_text):
        assert "parchment_cream" in doc_text

    def test_hex_void_black(self, doc_text):
        assert "#080C0E" in doc_text

    def test_hex_warm_gold(self, doc_text):
        assert "#E7D6B3" in doc_text

    def test_seven_swatches_mentioned(self, doc_text):
        # Check that 7 swatches worth of palette entries are present
        swatches = ["void_black", "deep_stone", "charcoal_earth", "ancient_soil",
                    "antique_gold", "warm_gold", "parchment_cream"]
        missing = [s for s in swatches if s not in doc_text]
        assert missing == [], f"Missing palette swatches: {missing}"

    def test_forbidden_colors_listed(self, doc_text):
        assert "neon" in doc_text.lower()
        assert "bright white" in doc_text.lower() or "bright" in doc_text.lower()


# ---------------------------------------------------------------------------
# TestThumbnailContent
# ---------------------------------------------------------------------------

class TestThumbnailContent:
    def test_face_rule_present(self, doc_text):
        assert "face" in doc_text.lower()
        assert "no human face" in doc_text.lower() or "no human faces" in doc_text.lower()

    def test_dimensions_present(self, doc_text):
        assert "1280" in doc_text
        assert "720" in doc_text

    def test_wordmark_dominance_mentioned(self, doc_text):
        assert "wordmark" in doc_text.lower()

    def test_em_dash_rule_present(self, doc_text):
        assert "em-dash" in doc_text.lower() or "em dash" in doc_text.lower()

    def test_forbidden_thumbnail_colors_listed(self, doc_text):
        assert "neon" in doc_text.lower()


# ---------------------------------------------------------------------------
# TestTypographyContent
# ---------------------------------------------------------------------------

class TestTypographyContent:
    def test_cinzel_present(self, doc_text):
        assert "Cinzel" in doc_text

    def test_raleway_present(self, doc_text):
        assert "Raleway" in doc_text

    def test_two_typeface_rule(self, doc_text):
        assert "two" in doc_text.lower() or "2" in doc_text

    def test_forbidden_typefaces_mentioned(self, doc_text):
        assert "forbidden" in doc_text.lower()
        # at least one example of a forbidden typeface
        assert any(f in doc_text for f in ["Arial", "Comic Sans", "Impact"])

    def test_semibold_weight_specified(self, doc_text):
        assert "SemiBold" in doc_text


# ---------------------------------------------------------------------------
# TestForbiddenPatterns
# ---------------------------------------------------------------------------

class TestForbiddenPatterns:
    def test_talking_head_forbidden(self, doc_text):
        assert "talking head" in doc_text.lower() or "talking-head" in doc_text.lower()

    def test_lip_sync_forbidden(self, doc_text):
        assert "lip sync" in doc_text.lower() or "lip-sync" in doc_text.lower()

    def test_neon_lighting_forbidden(self, doc_text):
        assert "neon" in doc_text.lower()

    def test_sensationalism_forbidden(self, doc_text):
        assert "sensationali" in doc_text.lower()

    def test_fabricated_footage_forbidden(self, doc_text):
        assert "fabricat" in doc_text.lower()


# ---------------------------------------------------------------------------
# TestNarrationContent
# ---------------------------------------------------------------------------

class TestNarrationContent:
    def test_voice_over_model_stated(self, doc_text):
        assert "voice" in doc_text.lower()
        assert "over" in doc_text.lower()

    def test_elevenlabs_voice_id_present(self, doc_text):
        assert "j9jfwdrw7BRfcR43Qohk" in doc_text

    def test_captions_gate_mentioned(self, doc_text):
        assert "captions" in doc_text.lower() or "srt" in doc_text.lower()


# ---------------------------------------------------------------------------
# TestSubtitleContent
# ---------------------------------------------------------------------------

class TestSubtitleContent:
    def test_line_length_limit_mentioned(self, doc_text):
        assert "42" in doc_text or "character" in doc_text.lower()

    def test_srt_format_mentioned(self, doc_text):
        assert "srt" in doc_text.lower() or "webvtt" in doc_text.lower()

    def test_subtitle_gate_check_mentioned(self, doc_text):
        assert "fail" in doc_text.lower()


# ---------------------------------------------------------------------------
# TestTitleContent
# ---------------------------------------------------------------------------

class TestTitleContent:
    def test_max_character_limit_mentioned(self, doc_text):
        assert "60" in doc_text

    def test_no_clickbait_rule(self, doc_text):
        assert "clickbait" in doc_text.lower() or "sensationalist" in doc_text.lower()

    def test_description_rules_present(self, doc_text):
        assert "description" in doc_text.lower()

    def test_chapter_markers_mentioned(self, doc_text):
        assert "chapter" in doc_text.lower()


# ---------------------------------------------------------------------------
# TestDisclosureContent
# ---------------------------------------------------------------------------

class TestDisclosureContent:
    def test_source_attribution_rule(self, doc_text):
        assert "attribution" in doc_text.lower() or "attribution" in doc_text.lower()

    def test_banned_terms_section(self, doc_text):
        assert "banned" in doc_text.lower() or "forbidden" in doc_text.lower()

    def test_conspiracy_term_in_banned_list(self, doc_text):
        assert "conspiracy" in doc_text.lower()

    def test_ai_disclosure_mentioned(self, doc_text):
        assert "ai" in doc_text.lower() and "disclosure" in doc_text.lower()

    def test_music_licensing_mentioned(self, doc_text):
        assert "licens" in doc_text.lower()
