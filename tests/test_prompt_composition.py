"""
tests/test_prompt_composition.py — Unit tests for hypogeum/prompt_composition.py

Pure-function tests only. No I/O, no network, no provider calls.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.prompt_composition import KNOWN_MODES, SkillSpec, select_skills

_A = Path("/fake/skill_a.md")
_B = Path("/fake/skill_b.md")
_C = Path("/fake/skill_c.md")


def _specs() -> list[SkillSpec]:
    return [
        SkillSpec(_A, frozenset({"legacy", "concept_driven"})),
        SkillSpec(_B, frozenset({"concept_driven"})),
        SkillSpec(_C, frozenset({"legacy"})),
    ]


class TestSelectSkillsModes:
    def test_legacy_mode_selects_expected_specs(self):
        result = select_skills(_specs(), "legacy")
        assert result.paths == (_A, _C)
        assert result.reason == "mode=legacy:selected=2_of_3"

    def test_concept_driven_mode_selects_expected_specs(self):
        result = select_skills(_specs(), "concept_driven")
        assert result.paths == (_A, _B)
        assert result.reason == "mode=concept_driven:selected=2_of_3"


class TestSelectSkillsFallback:
    def test_missing_mode_signal_returns_full_stack_with_reason(self):
        result = select_skills(_specs(), None)
        assert result.paths == (_A, _B, _C)
        assert result.reason == "fallback_full_stack:unrecognised_mode=None"

    def test_ambiguous_mode_signal_returns_full_stack_with_reason(self):
        result = select_skills(_specs(), "some_unknown_mode")
        assert result.paths == (_A, _B, _C)
        assert "fallback_full_stack" in result.reason
        assert "some_unknown_mode" in result.reason

    def test_known_modes_constant_matches_recognised_modes(self):
        # "legacy"/"concept_driven" — Thumbnail Prompt Writer.
        # "veo_only"/"seedance_only"/"mixed_tier"/"no_tier" — Visual Package
        # Director Stage B (Phase B1 Checkpoint 1).
        assert KNOWN_MODES == frozenset({
            "legacy", "concept_driven",
            "veo_only", "seedance_only", "mixed_tier", "no_tier",
        })

    def test_vpd_stage_b_modes_are_recognised_not_falling_back(self):
        specs = [
            SkillSpec(_A, frozenset({"veo_only", "mixed_tier", "no_tier"})),
            SkillSpec(_B, frozenset({"seedance_only", "mixed_tier", "no_tier"})),
        ]
        result = select_skills(specs, "veo_only")
        assert result.paths == (_A,)
        assert result.reason == "mode=veo_only:selected=1_of_2"

        result = select_skills(specs, "seedance_only")
        assert result.paths == (_B,)

        result = select_skills(specs, "mixed_tier")
        assert result.paths == (_A, _B)

        result = select_skills(specs, "no_tier")
        assert result.paths == (_A, _B)


class TestSelectSkillsOrderingAndDeduplication:
    def test_deterministic_order_preserved(self):
        specs = [
            SkillSpec(_C, frozenset({"legacy", "concept_driven"})),
            SkillSpec(_A, frozenset({"legacy", "concept_driven"})),
            SkillSpec(_B, frozenset({"legacy", "concept_driven"})),
        ]
        result = select_skills(specs, "legacy")
        assert result.paths == (_C, _A, _B)  # input order, not sorted

    def test_no_duplicate_skills_in_output(self):
        specs = [
            SkillSpec(_A, frozenset({"legacy"})),
            SkillSpec(_A, frozenset({"legacy"})),  # same path, listed twice
            SkillSpec(_B, frozenset({"legacy"})),
        ]
        result = select_skills(specs, "legacy")
        assert result.paths == (_A, _B)
        assert len(result.paths) == len(set(result.paths))

    def test_empty_spec_list_returns_empty_selection(self):
        result = select_skills([], "legacy")
        assert result.paths == ()
        assert result.reason == "mode=legacy:selected=0_of_0"
