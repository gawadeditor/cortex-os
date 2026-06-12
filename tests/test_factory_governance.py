"""
tests/test_factory_governance.py — Factory governance document tests

Coverage:
    TestThumbnailPromptWriterContext  — CONTEXT.md reflects openai/gpt-5.5-pro runtime model
    TestTIGContext                    — TIG CONTEXT.md exists and documents key properties
    TestBrandGuardianContext          — BGA CONTEXT.md documents production state and GPT mandate
    TestFactoryGovernanceGaps         — factory-governance-gaps.md exists and covers required gaps
    TestChannelIdentityGovernanceNote — channel-identity.md includes enforcement status section
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent

_TPW_CONTEXT    = _ROOT / ".octogent" / "tentacles" / "thumbnail-prompt-writer" / "CONTEXT.md"
_TIG_CONTEXT    = _ROOT / ".octogent" / "tentacles" / "thumbnail-identity-guardian" / "CONTEXT.md"
_BGA_CONTEXT    = _ROOT / ".octogent" / "tentacles" / "brand-guardian" / "CONTEXT.md"
_GAPS_DOC       = _ROOT / "docs" / "factory-governance-gaps.md"
_IDENTITY_DOC   = _ROOT / "docs" / "channel-identity.md"


# ---------------------------------------------------------------------------
# TestThumbnailPromptWriterContext
# ---------------------------------------------------------------------------

class TestThumbnailPromptWriterContext:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _TPW_CONTEXT.read_text(encoding="utf-8")

    def test_file_exists(self):
        assert _TPW_CONTEXT.exists(), "thumbnail-prompt-writer CONTEXT.md not found"

    def test_references_gpt_model(self, text):
        assert "openai/gpt-5.5-pro" in text, (
            "CONTEXT.md must reference openai/gpt-5.5-pro as the runtime model"
        )

    def test_does_not_claim_sonnet_as_model_slug(self, text):
        # Must not contain the stale claim that model_slug = claude-sonnet-4.6
        assert 'model_slug = "anthropic/claude-sonnet-4.6"' not in text, (
            "Stale model_slug claim still present — must be removed"
        )

    def test_references_td_planning_model_config_key(self, text):
        assert "TD_PLANNING_MODEL" in text, (
            "CONTEXT.md must document the TD_PLANNING_MODEL config key"
        )

    def test_states_gpt_mandate(self, text):
        # Must document that the thumbnail reasoning stack must remain GPT-based
        assert "GPT" in text or "gpt" in text.lower(), (
            "CONTEXT.md must state the GPT-based mandate"
        )


# ---------------------------------------------------------------------------
# TestTIGContext
# ---------------------------------------------------------------------------

class TestTIGContext:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _TIG_CONTEXT.read_text(encoding="utf-8")

    def test_file_exists(self):
        assert _TIG_CONTEXT.exists(), "thumbnail-identity-guardian CONTEXT.md not found"

    def test_documents_fail_closed_default(self, text):
        assert "fail-closed" in text.lower() or "TIG_FAIL_OPEN" in text, (
            "CONTEXT.md must document fail-closed default behavior"
        )

    def test_documents_tig_fail_open_config_key(self, text):
        assert "TIG_FAIL_OPEN" in text

    def test_documents_tig_enabled_config_key(self, text):
        assert "TIG_ENABLED" in text

    def test_documents_tig_model_config_key(self, text):
        assert "TIG_MODEL" in text

    def test_documents_approved_verdict(self, text):
        assert "APPROVED" in text

    def test_documents_blocked_verdict(self, text):
        assert "BLOCKED" in text

    def test_documents_warning_verdict(self, text):
        assert "WARNING" in text

    def test_documents_error_verdict(self, text):
        assert "error" in text

    def test_states_must_not_generate_images(self, text):
        assert "must not generate" in text.lower() or "no muapi" in text.lower() or "prohibited" in text.lower()

    def test_states_must_not_replace_bga_pass2(self, text):
        assert "BGA Pass 2" in text or "BGA" in text

    def test_states_must_not_replace_thumbnail_qa(self, text):
        assert "Thumbnail QA" in text

    def test_documents_output_file_pattern(self, text):
        assert "thumbnail_identity_" in text

    def test_implementation_file_present(self, text):
        assert "thumbnail_identity_guardian.py" in text


# ---------------------------------------------------------------------------
# TestBrandGuardianContext
# ---------------------------------------------------------------------------

class TestBrandGuardianContext:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _BGA_CONTEXT.read_text(encoding="utf-8")

    def test_file_exists(self):
        assert _BGA_CONTEXT.exists()

    def test_documents_production_bga_enabled_true(self, text):
        # Must explicitly state that production has BGA_ENABLED=true
        assert "BGA_ENABLED=true" in text

    def test_documents_bga_pass1_model_config_key(self, text):
        assert "BGA_PASS1_MODEL" in text

    def test_documents_bga_pass2_model_config_key(self, text):
        assert "BGA_PASS2_MODEL" in text

    def test_documents_pass2_is_gpt_based(self, text):
        # Must document that Pass 2 is GPT-based
        assert "gpt-5.5-pro" in text or "GPT" in text

    def test_documents_pass3_deferred(self, text):
        assert "Pass 3" in text and "deferred" in text.lower()


# ---------------------------------------------------------------------------
# TestFactoryGovernanceGaps
# ---------------------------------------------------------------------------

class TestFactoryGovernanceGaps:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _GAPS_DOC.read_text(encoding="utf-8")

    def test_file_exists(self):
        assert _GAPS_DOC.exists(), "docs/factory-governance-gaps.md not found"

    def test_documents_bga_pass3_gap(self, text):
        assert "BGA Pass 3" in text or "Pass 3" in text

    def test_documents_font_validation_gap(self, text):
        assert "font" in text.lower() and (
            "validation" in text.lower() or "unenforceable" in text.lower()
        )

    def test_documents_hex_validation_gap(self, text):
        assert "hex" in text.lower()

    def test_documents_prompt_only_enforcement(self, text):
        assert "prompt" in text.lower() and "only" in text.lower()

    def test_documents_future_work_section(self, text):
        assert "Required Future Work" in text or "future work" in text.lower()

    def test_documents_palette_sweep_gap(self, text):
        assert "palette" in text.lower()

    def test_documents_single_source_identity(self, text):
        # Must mention the prompt-inline drift risk
        assert "channel-identity.md" in text or "single-source" in text.lower() or "Single-Source" in text


# ---------------------------------------------------------------------------
# TestChannelIdentityGovernanceNote
# ---------------------------------------------------------------------------

class TestChannelIdentityGovernanceNote:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _IDENTITY_DOC.read_text(encoding="utf-8")

    def test_file_exists(self):
        assert _IDENTITY_DOC.exists()

    def test_has_enforcement_status_section(self, text):
        assert "Enforcement Status" in text

    def test_notes_partial_enforcement(self, text):
        assert "partial" in text.lower()

    def test_notes_hex_not_enforced(self, text):
        assert "hex" in text.lower() and (
            "not enforced" in text.lower() or "deferred" in text.lower()
        )

    def test_notes_typography_prompt_only(self, text):
        assert "Typography" in text or "Cinzel" in text or "font" in text.lower()

    def test_notes_bga_pass3_deferred(self, text):
        assert "BGA Pass 3" in text or ("Pass 3" in text and "Deferred" in text)

    def test_references_governance_gaps_doc(self, text):
        assert "factory-governance-gaps.md" in text
