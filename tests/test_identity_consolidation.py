"""
tests/test_identity_consolidation.py — Factory Identity Consolidation tests

Coverage:
    TestTIGPromptFile          — TIG prompt file exists and TIG loads it from disk
    TestBGAPromptHexClaim      — BGA prompt no longer says hex values are unverified
    TestVPDRuleIDs             — VPD prompt uses correct rule IDs (E-C05 for cool blues)
    TestTDRuleIDs              — TD prompt uses correct rule IDs (E-C05; no wrong E-C06)
    TestChannelIdentityHierarchy — channel-identity.md declares document hierarchy
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent

_TIG_PROMPT_FILE  = _ROOT / "hypogeum" / "prompts" / "thumbnail_identity_guardian.txt"
_BGA_PROMPT_FILE  = _ROOT / "hypogeum" / "prompts" / "brand_guardian_agent.txt"
_VPD_PROMPT_FILE  = _ROOT / "hypogeum" / "prompts" / "visual_package_director.txt"
_TD_PROMPT_FILE   = _ROOT / "hypogeum" / "prompts" / "thumbnail_director.txt"
_IDENTITY_DOC     = _ROOT / "docs" / "channel-identity.md"
_TIG_MODULE       = _ROOT / "hypogeum" / "agents" / "thumbnail_identity_guardian.py"


# ---------------------------------------------------------------------------
# TestTIGPromptFile
# ---------------------------------------------------------------------------

class TestTIGPromptFile:
    def test_tig_prompt_file_exists(self):
        assert _TIG_PROMPT_FILE.exists(), (
            "hypogeum/prompts/thumbnail_identity_guardian.txt must exist"
        )

    def test_tig_prompt_file_non_empty(self):
        text = _TIG_PROMPT_FILE.read_text(encoding="utf-8")
        assert len(text.strip()) > 100

    def test_tig_prompt_contains_blocking_rules(self):
        text = _TIG_PROMPT_FILE.read_text(encoding="utf-8")
        assert "TIG-01" in text
        assert "TIG-07" in text

    def test_tig_prompt_contains_warning_rules(self):
        text = _TIG_PROMPT_FILE.read_text(encoding="utf-8")
        assert "TIG-W01" in text

    def test_tig_module_loads_prompt_from_disk(self):
        """thumbnail_identity_guardian.py must load _SYSTEM_PROMPT from _PROMPT_FILE, not hardcode it."""
        source = _TIG_MODULE.read_text(encoding="utf-8")
        # Must reference a prompt file path
        assert "_PROMPT_FILE" in source, (
            "TIG module must define _PROMPT_FILE for disk-based prompt loading"
        )
        # Must use load_prompt (or read_text directly) to load from disk
        assert "load_prompt" in source or "read_text" in source, (
            "TIG module must call load_prompt() or read_text() to load _SYSTEM_PROMPT from disk"
        )

    def test_tig_module_no_longer_has_hardcoded_multiline_prompt(self):
        """The inline triple-quoted _SYSTEM_PROMPT string must be removed from the .py file."""
        source = _TIG_MODULE.read_text(encoding="utf-8")
        # The old hardcoded prompt started with a triple-quote + specific text
        assert '"""\\\nYou are the Thumbnail Identity Guardian' not in source, (
            "TIG module must not contain hardcoded triple-quoted system prompt"
        )

    def test_tig_system_prompt_matches_file(self):
        """The _SYSTEM_PROMPT loaded at runtime must equal the body of thumbnail_identity_guardian.txt.

        load_prompt() strips versioning headers (# KEY: VALUE lines) before returning the body,
        so we compare against the stripped body, not the raw file content.
        """
        import importlib
        import hypogeum.agents.thumbnail_identity_guardian as tig_mod
        from hypogeum.prompt_loader import load_prompt
        importlib.reload(tig_mod)
        expected = load_prompt(_TIG_PROMPT_FILE)
        assert tig_mod._SYSTEM_PROMPT == expected, (
            "_SYSTEM_PROMPT in module must exactly match the body of thumbnail_identity_guardian.txt"
        )


# ---------------------------------------------------------------------------
# TestBGAPromptHexClaim
# ---------------------------------------------------------------------------

class TestBGAPromptHexClaim:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _BGA_PROMPT_FILE.read_text(encoding="utf-8")

    def test_bga_prompt_no_longer_says_unverified(self, text):
        # The stale claim was: "not enforceable — all hex values are unverified"
        assert "all hex values are unverified" not in text, (
            "BGA prompt must not say 'all hex values are unverified' — they were verified 2026-06-08"
        )

    def test_bga_prompt_explains_hex_policy(self, text):
        # The corrected statement must explain WHY hex is not enforced
        text_lower = text.lower()
        assert "verified" in text_lower or "bitmap" in text_lower or "character-based" in text_lower, (
            "BGA prompt must explain the hex non-enforcement policy correctly"
        )

    def test_bga_prompt_still_says_hex_not_enforced(self, text):
        # The enforcement decision (no hex) must remain
        text_lower = text.lower()
        assert "hex" in text_lower and (
            "not enforceable" in text_lower or "not reliable" in text_lower
        ), (
            "BGA prompt must still state that hex values are not enforced"
        )


# ---------------------------------------------------------------------------
# TestVPDRuleIDs
# ---------------------------------------------------------------------------

class TestVPDRuleIDs:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _VPD_PROMPT_FILE.read_text(encoding="utf-8")

    def test_vpd_cool_blue_uses_e_c05_not_e_c04(self, text):
        """VPD must cite E-C05 (not E-C04) when referencing the cool-blues prohibition."""
        # E-C04 = neon hues. Cool blues = E-C05.
        # The wrong mapping was: "Cool blue-white is ALWAYS a blocking BGA violation (E-C04)"
        assert "(E-C04)" not in text or "cool blue" not in text.lower() or (
            # If E-C04 appears, it must not be paired with cool-blue language
            not any(
                "e-c04" in line.lower() and "cool" in line.lower()
                for line in text.split("\n")
            )
        ), (
            "VPD prompt must not map cool blues to E-C04 — cool blues = E-C05"
        )

    def test_vpd_cool_blue_cites_e_c05(self, text):
        """VPD must cite E-C05 for cool blue violations."""
        assert "E-C05" in text, (
            "VPD prompt must cite E-C05 (no cool blues) for cool blue-white prohibition"
        )

    def test_vpd_cool_blue_is_still_forbidden(self, text):
        """VPD must still prohibit cool blue regardless of rule ID fix."""
        text_lower = text.lower()
        assert "cool blue" in text_lower, (
            "VPD prompt must still explicitly forbid cool blue palette"
        )


# ---------------------------------------------------------------------------
# TestTDRuleIDs
# ---------------------------------------------------------------------------

class TestTDRuleIDs:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _TD_PROMPT_FILE.read_text(encoding="utf-8")

    def test_td_cold_cool_use_e_c05_not_e_c04(self, text):
        """TD must not map 'cold'/'cool' to E-C04 — those are E-C05 (no cool blues)."""
        lines = text.split("\n")
        for line in lines:
            if "e-c04" in line.lower() and ("cold" in line.lower() or "cool" in line.lower()):
                pytest.fail(
                    f"TD prompt must not map cold/cool to E-C04 (that's neon): {line.strip()!r}"
                )

    def test_td_cold_cool_cite_e_c05(self, text):
        """TD must cite E-C05 for cold/cool lighting prohibition."""
        assert "E-C05" in text, (
            "TD prompt must cite E-C05 (no cool blues) for cold/cool lighting prohibition"
        )

    def test_td_museum_lighting_not_mapped_to_e_c06(self, text):
        """TD must not map museum/fluorescent lighting to E-C06 (E-C06 = no greens)."""
        lines = text.split("\n")
        for line in lines:
            if "e-c06" in line.lower() and (
                "museum" in line.lower() or "fluorescent" in line.lower()
            ):
                pytest.fail(
                    f"TD prompt must not map museum/fluorescent lighting to E-C06 (that's greens): {line.strip()!r}"
                )

    def test_td_museum_lighting_still_forbidden(self, text):
        """TD must still prohibit museum/fluorescent lighting regardless of rule ID fix."""
        assert "fluorescent" in text.lower(), (
            "TD prompt must still explicitly forbid museum fluorescent / institutional lighting"
        )

    def test_td_cold_descriptors_still_forbidden(self, text):
        """TD must still prohibit 'cold' and 'cool' as lighting/atmosphere descriptors."""
        text_lower = text.lower()
        assert "cold" in text_lower and "cool" in text_lower, (
            "TD prompt must still forbid 'cold' and 'cool' descriptors"
        )


# ---------------------------------------------------------------------------
# TestChannelIdentityHierarchy
# ---------------------------------------------------------------------------

class TestChannelIdentityHierarchy:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _IDENTITY_DOC.read_text(encoding="utf-8")

    def test_declares_itself_authoritative_source(self, text):
        assert "single authoritative" in text.lower() or "authoritative operational" in text.lower(), (
            "channel-identity.md must declare itself the single authoritative operational identity source"
        )

    def test_document_hierarchy_section_present(self, text):
        assert "Document Hierarchy" in text, (
            "channel-identity.md must contain a 'Document Hierarchy' section"
        )

    def test_hierarchy_names_brand_spec_as_enforcement_mapping(self, text):
        assert "brand_spec_executable" in text and "enforcement" in text.lower(), (
            "hierarchy must identify brand_spec_executable.md as the enforcement mapping"
        )

    def test_hierarchy_names_brand_bible_as_historical(self, text):
        assert "brand_bible_hypogeum" in text and (
            "historical" in text.lower() or "inactive" in text.lower()
        ), (
            "hierarchy must identify brand_bible_hypogeum.md as historical / inactive"
        )

    def test_hierarchy_identifies_prompts_as_derived(self, text):
        assert "derived" in text.lower() and "prompt" in text.lower(), (
            "hierarchy must identify agent prompts as derived runtime artifacts"
        )

    def test_hierarchy_notes_no_agent_reads_at_runtime(self, text):
        # Must explicitly state the limitation that agents don't read this file
        text_lower = text.lower()
        assert "no agent reads" in text_lower or "not read at runtime" in text_lower or (
            "single-source" in text_lower and "planned" in text_lower
        ), (
            "channel-identity.md must acknowledge that no agent reads it at runtime"
        )
