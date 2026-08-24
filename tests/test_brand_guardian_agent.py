"""
tests/test_brand_guardian_agent.py — Brand Guardian Agent v1A test suite

Coverage:
  TestBGASystemPrompt          — prompt file exists, non-empty, loaded at import
  TestParseViolations          — _parse_violations() JSON parsing behavior
  TestNormalizeViolation       — _normalize_violation() field validation
  TestComputeOutcomePass1      — rejection threshold logic for Pass 1
  TestComputeOutcomePass2      — rejection threshold logic for Pass 2
  TestBuildReport              — _build_report() output structure
  TestBGARejectedError         — exception attributes
  TestRunBGAPass1Approved      — end-to-end Pass 1 with no violations
  TestRunBGAPass1Flags         — end-to-end Pass 1 with flag violations below threshold
  TestRunBGAPass1Rejected      — end-to-end Pass 1 blocked + high flag rate paths
  TestRunBGAPass2Approved      — end-to-end Pass 2 with no violations
  TestRunBGAPass2Rejected      — end-to-end Pass 2 rejection paths
  TestOutputFile               — report written to disk at expected path
  TestSchemaValidation         — bga_compliance_report schema validates all outcomes
  TestExtractPass1Scenes       — _extract_pass1_scenes() field extraction
  TestExtractPass2Candidates   — _extract_pass2_candidates() field extraction
  TestVPDWiring                — run_visual_package_director calls BGA when BGA_ENABLED
  TestTDWiring                 — run_thumbnail_director calls BGA when BGA_ENABLED
  TestVPDFromPlanWiring        — run_visual_package_director_from_plan calls BGA when BGA_ENABLED (Blocker 2 fix)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from hypogeum.agents.brand_guardian_agent import (
    SYSTEM_PROMPT,
    BGARejectedError,
    _build_report,
    _clean_violations,
    _compute_outcome_pass1,
    _compute_outcome_pass2,
    _extract_pass1_scenes,
    _extract_pass2_candidates,
    _normalize_violation,
    _parse_violations,
    run_bga_pass1,
    run_bga_pass2,
)
from hypogeum.schemas import validate, SchemaError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_config(bga_enabled: bool = True):
    """Minimal config stub."""
    cfg = MagicMock()
    cfg.BGA_ENABLED = bga_enabled
    cfg.OPENROUTER_API_KEY = "sk-or-test"
    cfg.FINANCE_BUDGET_CEILING_USD = 0  # disable post-VPD finance gate in BGA wiring tests
    # Thumbnail provider fields — must be real strings for json.dumps() in thumbnail_director
    cfg.THUMBNAIL_IMAGE_MODEL = "gpt-image-2-text-to-image"
    cfg.THUMBNAIL_FALLBACK_MODEL = "flux-2-dev"
    cfg.THUMBNAIL_IMAGE_RESOLUTION = "2K"
    cfg.THUMBNAIL_IMAGE_QUALITY = "high"
    return cfg


def _call_result(content: str):
    """Build a fake call_claude() return value."""
    r = SimpleNamespace()
    r.content = content
    return r


def _violations_json(violations: list) -> str:
    return json.dumps({"violations": violations})


def _no_violations_json() -> str:
    return _violations_json([])


def _blocking_violation(item_id: str = "sc_001", rule_id: str = "E-VG05") -> dict:
    return {
        "item_id": item_id,
        "rule_id": rule_id,
        "severity": "BLOCKING",
        "description": "Scene describes a direct-to-camera presenter.",
        "prompt_excerpt": "archaeologist faces camera",
    }


def _flag_violation(item_id: str = "sc_002", rule_id: str = "E-C08") -> dict:
    return {
        "item_id": item_id,
        "rule_id": rule_id,
        "severity": "FLAG",
        "description": "Scene appears to have bright commercial palette.",
        "prompt_excerpt": "bright cheerful studio setting",
    }


# run_cinematic_director() now delegates to
# regenerate_cinematic_coverage_plan() (2026-08-24 refactor) -- this file's
# fixtures pass an empty script_package ({}), so there are no authoritative
# beats to plan; stub the whole call rather than exercise real batched
# planning, which is not what TestVPDWiring/TestTDWiring are testing.
_EMPTY_INSTALLED_COVERAGE_PLAN = {
    "status": "installed", "reason": None,
    "plan": {"episode_id": "ep_001", "beat_plans": []},
    "active_plan_path": None, "backup_path": None, "candidate_path": None, "totals": None,
}


def _make_visual_package(n_scenes: int = 3) -> dict:
    return {
        "scenes": [
            {
                "scene_id": f"sc_{i:03d}",
                "production_spec": {
                    "primary_prompt": f"Ancient ruins scene {i}",
                    "fallback_prompt": f"Archaeological site {i}",
                },
                "visual_spec": {
                    "subject": "stone columns",
                    "lighting": "warm directional",
                    "mood": "mysterious",
                    "atmosphere": "ancient",
                    "color_treatment": "warm earth tones",
                },
            }
            for i in range(1, n_scenes + 1)
        ]
    }


_DISTINCT_MAIN_PROMPTS = [
    (
        "Subterranean vault corridor, near-black void, warm amber torchlight "
        "raking across ancient carved stone, atmospheric depth. "
        "Top 12% of frame near-black and completely clear of all subject detail, "
        "reserved for wordmark overlay. "
        "Bottom 10% of frame completely empty — no scene content, no inscriptions, "
        "no symbols, no captions, no labels."
    ),
    (
        "Aerial view of collapsed Roman amphitheatre at dusk, deep shadow "
        "filling the arena floor, golden-hour rim light on stone tiers. "
        "Top 12% of frame near-black and completely clear of all subject detail, "
        "reserved for wordmark overlay. "
        "Bottom 10% of frame completely empty — no scene content, no inscriptions, "
        "no symbols, no captions, no labels."
    ),
    (
        "Lone silhouette standing at threshold of massive ancient doorway, "
        "near-black background, bronze light from one side, scale contrast. "
        "Top 12% of frame near-black and completely clear of all subject detail, "
        "reserved for wordmark overlay. "
        "Bottom 10% of frame completely empty — no scene content, no inscriptions, "
        "no symbols, no captions, no labels."
    ),
]

_DISTINCT_MAIN_HOOKS = [
    "Who was he hiding?",
    "What did they find?",
    "Why was it buried?",
]


def _make_generation_plan(n_main: int = 3, n_shorts: int = 0) -> dict:
    main_cands = [
        {
            "candidate_id": f"main_{i:02d}",
            "rank": i,
            "image_prompt": _DISTINCT_MAIN_PROMPTS[(i - 1) % len(_DISTINCT_MAIN_PROMPTS)],
            "aspect_ratio": "16:9",
            "text_overlay": {"text": _DISTINCT_MAIN_HOOKS[(i - 1) % len(_DISTINCT_MAIN_HOOKS)], "placement": "bottom"},
        }
        for i in range(1, n_main + 1)
    ]
    shorts = [
        {
            "short_id": f"s{j}",
            "ranked_candidates": [
                {
                    "candidate_id": f"short_{j}_01",
                    "rank": 1,
                    "image_prompt": "Underground chamber vertical",
                    "aspect_ratio": "9:16",
                    "text_overlay": None,
                }
            ],
        }
        for j in range(1, n_shorts + 1)
    ]
    return {
        "thumbnail_generation_plan_id": "tgp_test",
        "episode_id": "ep_test",
        "main_episode": {"ranked_candidates": main_cands},
        "shorts": shorts,
    }


# ---------------------------------------------------------------------------
# TestBGASystemPrompt
# ---------------------------------------------------------------------------

class TestBGASystemPrompt:
    def test_prompt_file_exists(self):
        from hypogeum.agents.brand_guardian_agent import _PROMPT_FILE
        assert _PROMPT_FILE.exists(), f"Prompt file not found: {_PROMPT_FILE}"

    def test_system_prompt_non_empty(self):
        assert len(SYSTEM_PROMPT) > 100

    def test_system_prompt_contains_blocking(self):
        assert "BLOCKING" in SYSTEM_PROMPT

    def test_system_prompt_contains_flag(self):
        assert "FLAG" in SYSTEM_PROMPT

    def test_system_prompt_contains_output_format(self):
        assert "violations" in SYSTEM_PROMPT

    def test_system_prompt_references_key_rules(self):
        # Must reference at least the core enforcement rules
        assert "E-VG05" in SYSTEM_PROMPT or "talking head" in SYSTEM_PROMPT.lower()


# ---------------------------------------------------------------------------
# TestParseViolations
# ---------------------------------------------------------------------------

class TestParseViolations:
    def test_parses_violations_list(self):
        raw = _violations_json([_blocking_violation()])
        result = _parse_violations(raw)
        assert len(result) == 1

    def test_returns_empty_for_empty_list(self):
        result = _parse_violations(_no_violations_json())
        assert result == []

    def test_returns_empty_for_invalid_json(self):
        result = _parse_violations("NOT JSON")
        assert result == []

    def test_returns_empty_for_missing_violations_key(self):
        result = _parse_violations('{"no_violations": []}')
        assert result == []

    def test_strips_markdown_fence(self):
        raw = "```json\n" + _violations_json([_blocking_violation()]) + "\n```"
        result = _parse_violations(raw)
        assert len(result) == 1

    def test_returns_empty_for_wrong_type(self):
        result = _parse_violations('{"violations": "not_a_list"}')
        assert result == []

    def test_multiple_violations(self):
        raw = _violations_json([_blocking_violation(), _flag_violation()])
        result = _parse_violations(raw)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# TestNormalizeViolation
# ---------------------------------------------------------------------------

class TestNormalizeViolation:
    def test_valid_blocking_normalized(self):
        v = _blocking_violation()
        result = _normalize_violation(v)
        assert result is not None
        assert result["severity"] == "BLOCKING"

    def test_valid_flag_normalized(self):
        v = _flag_violation()
        result = _normalize_violation(v)
        assert result is not None
        assert result["severity"] == "FLAG"

    def test_missing_item_id_returns_none(self):
        v = {**_blocking_violation(), "item_id": ""}
        assert _normalize_violation(v) is None

    def test_missing_rule_id_returns_none(self):
        v = {**_blocking_violation(), "rule_id": ""}
        assert _normalize_violation(v) is None

    def test_invalid_severity_returns_none(self):
        v = {**_blocking_violation(), "severity": "CRITICAL"}
        assert _normalize_violation(v) is None

    def test_missing_description_returns_none(self):
        v = {**_blocking_violation(), "description": ""}
        assert _normalize_violation(v) is None

    def test_non_dict_returns_none(self):
        assert _normalize_violation("not_a_dict") is None
        assert _normalize_violation(42) is None
        assert _normalize_violation(None) is None

    def test_prompt_excerpt_truncated_to_200(self):
        v = {**_blocking_violation(), "prompt_excerpt": "x" * 300}
        result = _normalize_violation(v)
        assert result is not None
        assert len(result["prompt_excerpt"]) == 200

    def test_missing_prompt_excerpt_defaults_to_empty(self):
        v = {k: val for k, val in _blocking_violation().items() if k != "prompt_excerpt"}
        result = _normalize_violation(v)
        assert result is not None
        assert result["prompt_excerpt"] == ""


# ---------------------------------------------------------------------------
# TestComputeOutcomePass1
# ---------------------------------------------------------------------------

class TestComputeOutcomePass1:
    def test_no_violations_approved(self):
        outcome, reason = _compute_outcome_pass1([], total_scenes=10)
        assert outcome == "APPROVED"
        assert reason is None

    def test_single_blocking_rejected(self):
        violations = [_blocking_violation()]
        outcome, reason = _compute_outcome_pass1(violations, total_scenes=10)
        assert outcome == "REJECTED"
        assert reason is not None
        assert "blocking" in reason.lower()

    def test_flags_below_threshold_approved_with_flags(self):
        # 2 flags out of 10 scenes = 20% < 25% threshold
        violations = [_flag_violation(f"sc_{i:03d}") for i in range(2)]
        outcome, reason = _compute_outcome_pass1(violations, total_scenes=10)
        assert outcome == "APPROVED_WITH_FLAGS"
        assert reason is None

    def test_flags_at_threshold_rejected(self):
        # 3 flags out of 10 scenes = 30% >= 25% threshold
        violations = [_flag_violation(f"sc_{i:03d}") for i in range(3)]
        outcome, reason = _compute_outcome_pass1(violations, total_scenes=10)
        assert outcome == "REJECTED"
        assert reason is not None

    def test_flags_exactly_at_threshold_rejected(self):
        # 1 flag out of 4 scenes = 25% == threshold → REJECTED
        violations = [_flag_violation("sc_001")]
        outcome, reason = _compute_outcome_pass1(violations, total_scenes=4)
        assert outcome == "REJECTED"

    def test_flags_just_below_threshold_approved_with_flags(self):
        # 24 flags out of 100 scenes = 24% < 25% threshold
        violations = [_flag_violation(f"sc_{i:03d}") for i in range(24)]
        outcome, reason = _compute_outcome_pass1(violations, total_scenes=100)
        assert outcome == "APPROVED_WITH_FLAGS"

    def test_zero_scenes_no_division_error(self):
        violations = []
        outcome, reason = _compute_outcome_pass1(violations, total_scenes=0)
        assert outcome == "APPROVED"

    def test_blocking_takes_priority_over_flag_rate(self):
        # 1 blocking + 3 flags in 10 scenes — blocking should determine outcome
        violations = [_blocking_violation()] + [_flag_violation(f"sc_{i:03d}") for i in range(3)]
        outcome, _ = _compute_outcome_pass1(violations, total_scenes=10)
        assert outcome == "REJECTED"


# ---------------------------------------------------------------------------
# TestComputeOutcomePass2
# ---------------------------------------------------------------------------

class TestComputeOutcomePass2:
    def _plan(self, n_main: int = 3) -> dict:
        return _make_generation_plan(n_main=n_main)

    def test_no_violations_approved(self):
        plan = self._plan()
        outcome, reason = _compute_outcome_pass2([], plan)
        assert outcome == "APPROVED"

    def test_rank1_blocking_rejected(self):
        plan = self._plan()
        # main_01 has rank=1
        violations = [_blocking_violation(item_id="main_01")]
        outcome, reason = _compute_outcome_pass2(violations, plan)
        assert outcome == "REJECTED"
        assert reason is not None
        assert "rank-1" in reason.lower() or "rank" in reason.lower()

    def test_non_rank1_blocking_approved_with_flags(self):
        plan = self._plan()
        # main_02 has rank=2 — not rank-1 so not an immediate rejection
        violations = [_blocking_violation(item_id="main_02")]
        outcome, reason = _compute_outcome_pass2(violations, plan)
        assert outcome == "APPROVED_WITH_FLAGS"

    def test_all_main_blocking_rejected(self):
        plan = self._plan(n_main=3)
        violations = [
            _blocking_violation(item_id="main_01"),
            _blocking_violation(item_id="main_02"),
            _blocking_violation(item_id="main_03"),
        ]
        outcome, reason = _compute_outcome_pass2(violations, plan)
        assert outcome == "REJECTED"

    def test_flag_on_rank1_approved_with_flags(self):
        plan = self._plan()
        violations = [_flag_violation(item_id="main_01")]
        outcome, reason = _compute_outcome_pass2(violations, plan)
        assert outcome == "APPROVED_WITH_FLAGS"

    def test_short_candidate_blocking_not_rejected(self):
        plan = _make_generation_plan(n_main=3, n_shorts=1)
        # Blocking on short candidate — short candidates don't control rejection
        violations = [_blocking_violation(item_id="short_1_01")]
        outcome, reason = _compute_outcome_pass2(violations, plan)
        # short blocking doesn't cause REJECTED — main_01 (rank-1) has no blocking
        assert outcome == "APPROVED_WITH_FLAGS"


# ---------------------------------------------------------------------------
# TestBuildReport
# ---------------------------------------------------------------------------

class TestBuildReport:
    def test_approved_report_structure(self):
        report = _build_report(1, "ep_001", [], "APPROVED", 10, None)
        assert report["report_id"] == "bga_p1_ep_001"
        assert report["pass_number"] == 1
        assert report["episode_id"] == "ep_001"
        assert report["outcome"] == "APPROVED"
        assert report["violations"] == []
        assert report["summary"]["total_items"] == 10
        assert report["summary"]["blocking_count"] == 0
        assert report["summary"]["flag_count"] == 0

    def test_blocking_count_computed_correctly(self):
        violations = [_blocking_violation(), _flag_violation()]
        report = _build_report(1, "ep_001", violations, "REJECTED", 10, "test reason")
        assert report["summary"]["blocking_count"] == 1
        assert report["summary"]["flag_count"] == 1

    def test_rejection_reason_included_when_present(self):
        report = _build_report(1, "ep_001", [], "REJECTED", 10, "my reason")
        assert "rejection_reason" in report["summary"]
        assert report["summary"]["rejection_reason"] == "my reason"

    def test_no_rejection_reason_field_when_none(self):
        report = _build_report(1, "ep_001", [], "APPROVED", 10, None)
        assert "rejection_reason" not in report["summary"]

    def test_produced_at_is_iso_format(self):
        report = _build_report(1, "ep_001", [], "APPROVED", 10, None)
        # Just check it's a non-empty string with the expected shape
        assert len(report["produced_at"]) > 15
        assert "T" in report["produced_at"]

    def test_bible_and_bga_version_set(self):
        report = _build_report(1, "ep_001", [], "APPROVED", 5, None)
        assert report["bible_version"] == "1.1"
        assert report["bga_version"] == "1A"


# ---------------------------------------------------------------------------
# TestBGARejectedError
# ---------------------------------------------------------------------------

class TestBGARejectedError:
    def test_attributes_set(self):
        report = _build_report(1, "ep_001", [_blocking_violation()], "REJECTED", 10, "test reason")
        exc = BGARejectedError("ep_001", 1, report)
        assert exc.episode_id == "ep_001"
        assert exc.pass_number == 1
        assert exc.report is report

    def test_message_contains_pass_number(self):
        report = _build_report(2, "ep_001", [], "REJECTED", 5, "bad thumb")
        exc = BGARejectedError("ep_001", 2, report)
        assert "Pass 2" in str(exc) or "2" in str(exc)

    def test_is_runtime_error(self):
        report = _build_report(1, "ep_001", [], "REJECTED", 5, "r")
        exc = BGARejectedError("ep_001", 1, report)
        assert isinstance(exc, RuntimeError)


# ---------------------------------------------------------------------------
# TestRunBGAPass1Approved
# ---------------------------------------------------------------------------

class TestRunBGAPass1Approved:
    def test_returns_report_on_approved(self, tmp_path):
        cfg = _make_config()
        vp = _make_visual_package(n_scenes=3)
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_no_violations_json()),
        ):
            report = run_bga_pass1(vp, "ep_001", cfg, output_dir=tmp_path)
        assert report["outcome"] == "APPROVED"
        assert report["episode_id"] == "ep_001"
        assert report["pass_number"] == 1

    def test_does_not_raise_on_approved(self, tmp_path):
        cfg = _make_config()
        vp = _make_visual_package()
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_no_violations_json()),
        ):
            run_bga_pass1(vp, "ep_001", cfg, output_dir=tmp_path)  # must not raise

    def test_returns_report_with_flags_below_threshold(self, tmp_path):
        cfg = _make_config()
        vp = _make_visual_package(n_scenes=10)
        # 2 flags in 10 scenes = 20% < 25% threshold
        violations = [_flag_violation(f"sc_{i:03d}") for i in range(1, 3)]
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_violations_json(violations)),
        ):
            report = run_bga_pass1(vp, "ep_001", cfg, output_dir=tmp_path)
        assert report["outcome"] == "APPROVED_WITH_FLAGS"


# ---------------------------------------------------------------------------
# TestRunBGAPass1Rejected
# ---------------------------------------------------------------------------

class TestRunBGAPass1Rejected:
    def test_raises_on_blocking_violation(self, tmp_path):
        cfg = _make_config()
        vp = _make_visual_package()
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_violations_json([_blocking_violation()])),
        ):
            with pytest.raises(BGARejectedError) as exc_info:
                run_bga_pass1(vp, "ep_001", cfg, output_dir=tmp_path)
        assert exc_info.value.pass_number == 1
        assert exc_info.value.episode_id == "ep_001"

    def test_raises_on_high_flag_rate(self, tmp_path):
        cfg = _make_config()
        vp = _make_visual_package(n_scenes=4)
        # 1 flag in 4 scenes = 25% = threshold → REJECTED
        violations = [_flag_violation("sc_001")]
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_violations_json(violations)),
        ):
            with pytest.raises(BGARejectedError):
                run_bga_pass1(vp, "ep_001", cfg, output_dir=tmp_path)

    def test_report_attached_to_exception(self, tmp_path):
        cfg = _make_config()
        vp = _make_visual_package()
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_violations_json([_blocking_violation()])),
        ):
            with pytest.raises(BGARejectedError) as exc_info:
                run_bga_pass1(vp, "ep_001", cfg, output_dir=tmp_path)
        report = exc_info.value.report
        assert report["outcome"] == "REJECTED"
        assert report["summary"]["blocking_count"] >= 1

    def test_invalid_claude_response_does_not_raise_parse_error(self, tmp_path):
        cfg = _make_config()
        vp = _make_visual_package()
        # Claude returns garbage → parse fails → empty violations → APPROVED
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result("GARBAGE RESPONSE"),
        ):
            report = run_bga_pass1(vp, "ep_001", cfg, output_dir=tmp_path)
        assert report["outcome"] == "APPROVED"


# ---------------------------------------------------------------------------
# TestRunBGAPass2Approved
# ---------------------------------------------------------------------------

class TestRunBGAPass2Approved:
    def test_returns_report_on_approved(self, tmp_path):
        cfg = _make_config()
        plan = _make_generation_plan()
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_no_violations_json()),
        ):
            report = run_bga_pass2(plan, "ep_001", cfg, output_dir=tmp_path)
        assert report["outcome"] == "APPROVED"
        assert report["pass_number"] == 2

    def test_does_not_raise_on_approved(self, tmp_path):
        cfg = _make_config()
        plan = _make_generation_plan()
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_no_violations_json()),
        ):
            run_bga_pass2(plan, "ep_001", cfg, output_dir=tmp_path)  # must not raise


# ---------------------------------------------------------------------------
# TestRunBGAPass2Rejected
# ---------------------------------------------------------------------------

class TestRunBGAPass2Rejected:
    def test_raises_on_rank1_blocking(self, tmp_path):
        cfg = _make_config()
        plan = _make_generation_plan()
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_violations_json([_blocking_violation(item_id="main_01")])),
        ):
            with pytest.raises(BGARejectedError) as exc_info:
                run_bga_pass2(plan, "ep_001", cfg, output_dir=tmp_path)
        assert exc_info.value.pass_number == 2

    def test_raises_when_all_main_blocking(self, tmp_path):
        cfg = _make_config()
        plan = _make_generation_plan(n_main=2)
        violations = [
            _blocking_violation(item_id="main_01"),
            _blocking_violation(item_id="main_02"),
        ]
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_violations_json(violations)),
        ):
            with pytest.raises(BGARejectedError):
                run_bga_pass2(plan, "ep_001", cfg, output_dir=tmp_path)

    def test_does_not_raise_for_non_rank1_blocking(self, tmp_path):
        cfg = _make_config()
        plan = _make_generation_plan(n_main=3)
        # Blocking only on rank-2 candidate
        violations = [_blocking_violation(item_id="main_02")]
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_violations_json(violations)),
        ):
            report = run_bga_pass2(plan, "ep_001", cfg, output_dir=tmp_path)
        assert report["outcome"] == "APPROVED_WITH_FLAGS"


# ---------------------------------------------------------------------------
# TestBGAModelSelection
# ---------------------------------------------------------------------------

class TestBGAModelSelection:
    """BGA passes use model slugs from config."""

    def test_pass1_uses_bga_pass1_model(self, tmp_path):
        cfg = _make_config()
        cfg.BGA_PASS1_MODEL = "test-pass1-model"
        vp = _make_visual_package()
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_no_violations_json()),
        ) as mock_cc:
            run_bga_pass1(vp, "ep_001", cfg, output_dir=tmp_path)
        assert mock_cc.call_args[1]["model_slug"] == "test-pass1-model"

    def test_pass2_uses_bga_pass2_model(self, tmp_path):
        cfg = _make_config()
        cfg.BGA_PASS2_MODEL = "test-pass2-model"
        plan = _make_generation_plan()
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_no_violations_json()),
        ) as mock_cc:
            run_bga_pass2(plan, "ep_001", cfg, output_dir=tmp_path)
        assert mock_cc.call_args[1]["model_slug"] == "test-pass2-model"


# ---------------------------------------------------------------------------
# TestOutputFile
# ---------------------------------------------------------------------------

class TestOutputFile:
    def test_pass1_file_written(self, tmp_path):
        cfg = _make_config()
        vp = _make_visual_package()
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_no_violations_json()),
        ):
            run_bga_pass1(vp, "ep_001", cfg, output_dir=tmp_path)
        expected = tmp_path / "bga_compliance_report_p1_ep_001.json"
        assert expected.exists()

    def test_pass2_file_written(self, tmp_path):
        cfg = _make_config()
        plan = _make_generation_plan()
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_no_violations_json()),
        ):
            run_bga_pass2(plan, "ep_001", cfg, output_dir=tmp_path)
        expected = tmp_path / "bga_compliance_report_p2_ep_001.json"
        assert expected.exists()

    def test_file_content_is_valid_json(self, tmp_path):
        cfg = _make_config()
        vp = _make_visual_package()
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_no_violations_json()),
        ):
            run_bga_pass1(vp, "ep_001", cfg, output_dir=tmp_path)
        content = (tmp_path / "bga_compliance_report_p1_ep_001.json").read_text(encoding="utf-8")
        parsed = json.loads(content)
        assert isinstance(parsed, dict)
        assert "outcome" in parsed

    def test_file_written_even_on_rejection(self, tmp_path):
        cfg = _make_config()
        vp = _make_visual_package()
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_violations_json([_blocking_violation()])),
        ):
            with pytest.raises(BGARejectedError):
                run_bga_pass1(vp, "ep_001", cfg, output_dir=tmp_path)
        expected = tmp_path / "bga_compliance_report_p1_ep_001.json"
        assert expected.exists()


# ---------------------------------------------------------------------------
# TestSchemaValidation
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def _approved_report(self) -> dict:
        return _build_report(1, "ep_001", [], "APPROVED", 10, None)

    def _approved_with_flags_report(self) -> dict:
        violations = [_flag_violation()]
        return _build_report(1, "ep_001", violations, "APPROVED_WITH_FLAGS", 10, None)

    def _rejected_report(self) -> dict:
        violations = [_blocking_violation()]
        return _build_report(1, "ep_001", violations, "REJECTED", 10, "test reason")

    def test_approved_passes_schema(self):
        validate("bga_compliance_report", self._approved_report())  # must not raise

    def test_approved_with_flags_passes_schema(self):
        validate("bga_compliance_report", self._approved_with_flags_report())

    def test_rejected_passes_schema(self):
        validate("bga_compliance_report", self._rejected_report())

    def test_pass2_report_passes_schema(self):
        report = _build_report(2, "ep_001", [], "APPROVED", 5, None)
        validate("bga_compliance_report", report)

    def test_missing_report_id_fails_schema(self):
        report = self._approved_report()
        del report["report_id"]
        with pytest.raises(SchemaError):
            validate("bga_compliance_report", report)

    def test_invalid_outcome_fails_schema(self):
        report = self._approved_report()
        report["outcome"] = "PASS"
        with pytest.raises(SchemaError):
            validate("bga_compliance_report", report)

    def test_violation_missing_severity_fails_schema(self):
        violations = [_blocking_violation()]
        report = _build_report(1, "ep_001", violations, "REJECTED", 10, "r")
        del report["violations"][0]["severity"]
        with pytest.raises(SchemaError):
            validate("bga_compliance_report", report)


# ---------------------------------------------------------------------------
# TestExtractPass1Scenes
# ---------------------------------------------------------------------------

class TestExtractPass1Scenes:
    def test_extracts_correct_count(self):
        vp = _make_visual_package(n_scenes=5)
        scenes = _extract_pass1_scenes(vp)
        assert len(scenes) == 5

    def test_extracts_scene_id(self):
        vp = _make_visual_package(n_scenes=1)
        scenes = _extract_pass1_scenes(vp)
        assert scenes[0]["scene_id"] == "sc_001"

    def test_extracts_primary_prompt(self):
        vp = _make_visual_package(n_scenes=1)
        scenes = _extract_pass1_scenes(vp)
        assert "primary_prompt" in scenes[0]
        assert len(scenes[0]["primary_prompt"]) > 0

    def test_extracts_visual_spec_fields(self):
        vp = _make_visual_package(n_scenes=1)
        scenes = _extract_pass1_scenes(vp)
        s = scenes[0]
        for field in ("subject", "lighting", "mood", "atmosphere", "color_treatment"):
            assert field in s

    def test_empty_package_returns_empty_list(self):
        scenes = _extract_pass1_scenes({"scenes": []})
        assert scenes == []


# ---------------------------------------------------------------------------
# TestExtractPass2Candidates
# ---------------------------------------------------------------------------

class TestExtractPass2Candidates:
    def test_extracts_main_candidates(self):
        plan = _make_generation_plan(n_main=3)
        candidates = _extract_pass2_candidates(plan)
        main = [c for c in candidates if c["source"] == "main_episode"]
        assert len(main) == 3

    def test_extracts_short_candidates(self):
        plan = _make_generation_plan(n_main=3, n_shorts=2)
        candidates = _extract_pass2_candidates(plan)
        shorts = [c for c in candidates if c["source"].startswith("short:")]
        assert len(shorts) == 2

    def test_null_text_overlay_becomes_empty_string(self):
        plan = _make_generation_plan(n_main=1)
        plan["main_episode"]["ranked_candidates"][0]["text_overlay"] = None
        candidates = _extract_pass2_candidates(plan)
        assert candidates[0]["text_overlay"] == ""

    def test_dict_text_overlay_extracts_text(self):
        plan = _make_generation_plan(n_main=1)
        plan["main_episode"]["ranked_candidates"][0]["text_overlay"] = {
            "text": "Episode Title",
            "placement": "bottom",
        }
        candidates = _extract_pass2_candidates(plan)
        assert candidates[0]["text_overlay"] == "Episode Title"

    def test_candidate_id_present(self):
        plan = _make_generation_plan(n_main=2)
        candidates = _extract_pass2_candidates(plan)
        assert all(c["candidate_id"] for c in candidates)


# ---------------------------------------------------------------------------
# TestVPDWiring
# ---------------------------------------------------------------------------

class TestVPDWiring:
    """Verify run_visual_package_director calls BGA Pass 1 when BGA_ENABLED=True."""

    def test_bga_called_when_enabled(self, tmp_path):
        from hypogeum.agents.visual_package_director import run_visual_package_director

        vp = _make_visual_package(n_scenes=2)
        cfg = _make_config(bga_enabled=True)
        cfg.EPISODE_ZERO_MAX_SCENES = 0
        cfg.ENABLE_DEDICATED_SHORT_GENERATION = False

        # Patch at source module — VPD uses a local import so patch must be on BGA module.
        # validate is mocked so the minimal _make_visual_package fixture doesn't need to
        # satisfy the full visual_package schema (Stage A3 validation).
        with (
            patch("hypogeum.agents.visual_package_director.run_agent", return_value=vp),
            patch("hypogeum.agents.cinematic_director.regenerate_cinematic_coverage_plan",
                  return_value=_EMPTY_INSTALLED_COVERAGE_PLAN),
            patch("hypogeum.agents.brand_guardian_agent.run_bga_pass1") as mock_bga,
            patch("hypogeum.agents.visual_package_director._execute_muapi_phase", return_value=vp),
            patch("hypogeum.agents.visual_package_director._write_final_package"),
            patch("hypogeum.agents.visual_package_director.build_thumbnail_handoff_brief"),
            patch("hypogeum.agents.visual_package_director.validate"),
        ):
            run_visual_package_director({}, cfg, "ep_001", output_dir=tmp_path)

        mock_bga.assert_called_once()

    def test_bga_not_called_when_disabled(self, tmp_path):
        from hypogeum.agents.visual_package_director import run_visual_package_director

        vp = _make_visual_package(n_scenes=2)
        cfg = _make_config(bga_enabled=False)
        cfg.EPISODE_ZERO_MAX_SCENES = 0
        cfg.ENABLE_DEDICATED_SHORT_GENERATION = False

        with (
            patch("hypogeum.agents.visual_package_director.run_agent", return_value=vp),
            patch("hypogeum.agents.cinematic_director.regenerate_cinematic_coverage_plan",
                  return_value=_EMPTY_INSTALLED_COVERAGE_PLAN),
            patch("hypogeum.agents.brand_guardian_agent.run_bga_pass1") as mock_bga,
            patch("hypogeum.agents.visual_package_director._execute_muapi_phase", return_value=vp),
            patch("hypogeum.agents.visual_package_director._write_final_package"),
            patch("hypogeum.agents.visual_package_director.build_thumbnail_handoff_brief"),
            patch("hypogeum.agents.visual_package_director.validate"),
        ):
            run_visual_package_director({}, cfg, "ep_001", output_dir=tmp_path)

        mock_bga.assert_not_called()

    def test_bga_rejection_propagates(self, tmp_path):
        from hypogeum.agents.visual_package_director import run_visual_package_director

        vp = _make_visual_package()
        cfg = _make_config(bga_enabled=True)
        cfg.EPISODE_ZERO_MAX_SCENES = 0
        cfg.ENABLE_DEDICATED_SHORT_GENERATION = False

        fake_report = _build_report(1, "ep_001", [_blocking_violation()], "REJECTED", 3, "r")

        with (
            patch("hypogeum.agents.visual_package_director.run_agent", return_value=vp),
            patch("hypogeum.agents.cinematic_director.regenerate_cinematic_coverage_plan",
                  return_value=_EMPTY_INSTALLED_COVERAGE_PLAN),
            patch("hypogeum.agents.visual_package_director.validate"),
            patch(
                "hypogeum.agents.brand_guardian_agent.run_bga_pass1",
                side_effect=BGARejectedError("ep_001", 1, fake_report),
            ),
        ):
            with pytest.raises(BGARejectedError):
                run_visual_package_director({}, cfg, "ep_001", output_dir=tmp_path)


# ---------------------------------------------------------------------------
# TestTDWiring
# ---------------------------------------------------------------------------

class TestTDWiring:
    """Verify run_thumbnail_director calls BGA Pass 2 when BGA_ENABLED=True."""

    def _make_td_inputs(self):
        handoff_brief = {"episode_id": "ep_001"}
        generation_plan = _make_generation_plan()
        thumbnail_pkg = {
            "thumbnail_package_id": "tp_ep_001",
            "episode_id": "ep_001",
            "main_thumbnail": {"asset_id": "aid_main"},
            "short_thumbnails": [],
            "package_integrity": {"production_ready": True},
        }
        return handoff_brief, generation_plan, thumbnail_pkg

    def test_bga_called_when_enabled(self, tmp_path):
        from hypogeum.agents.thumbnail_director import run_thumbnail_director

        handoff_brief, gen_plan, thumb_pkg = self._make_td_inputs()
        cfg = _make_config(bga_enabled=True)

        gen_result = {
            "main_asset_id": "aid_main",
            "main_ok": True,
            "short_results": [],
        }

        # Patch at source module — TD uses a local import so patch must be on BGA module
        with (
            patch(
                "hypogeum.agents.thumbnail_director.write_thumbnail_prompts",
                return_value=gen_plan,
            ),
            patch("hypogeum.agents.brand_guardian_agent.run_bga_pass2") as mock_bga,
            patch(
                "hypogeum.agents.thumbnail_director.generate_thumbnails",
                return_value=gen_result,
            ),
        ):
            run_thumbnail_director(handoff_brief, cfg, "ep_001", output_dir=tmp_path)

        mock_bga.assert_called_once()

    def test_bga_not_called_when_disabled(self, tmp_path):
        from hypogeum.agents.thumbnail_director import run_thumbnail_director

        handoff_brief, gen_plan, _ = self._make_td_inputs()
        cfg = _make_config(bga_enabled=False)

        gen_result = {
            "main_asset_id": "aid_main",
            "main_ok": True,
            "short_results": [],
        }

        with (
            patch(
                "hypogeum.agents.thumbnail_director.write_thumbnail_prompts",
                return_value=gen_plan,
            ),
            patch("hypogeum.agents.brand_guardian_agent.run_bga_pass2") as mock_bga,
            patch(
                "hypogeum.agents.thumbnail_director.generate_thumbnails",
                return_value=gen_result,
            ),
        ):
            run_thumbnail_director(handoff_brief, cfg, "ep_001", output_dir=tmp_path)

        mock_bga.assert_not_called()

    def test_bga_rejection_propagates(self, tmp_path):
        from hypogeum.agents.thumbnail_director import run_thumbnail_director

        handoff_brief, gen_plan, _ = self._make_td_inputs()
        cfg = _make_config(bga_enabled=True)

        fake_report = _build_report(2, "ep_001", [_blocking_violation()], "REJECTED", 3, "r")

        with (
            patch(
                "hypogeum.agents.thumbnail_director.write_thumbnail_prompts",
                return_value=gen_plan,
            ),
            patch(
                "hypogeum.agents.brand_guardian_agent.run_bga_pass2",
                side_effect=BGARejectedError("ep_001", 2, fake_report),
            ),
        ):
            with pytest.raises(BGARejectedError):
                run_thumbnail_director(handoff_brief, cfg, "ep_001", output_dir=tmp_path)


# ---------------------------------------------------------------------------
# TestVPDFromPlanWiring
# ---------------------------------------------------------------------------

class TestVPDFromPlanWiring:
    """Verify run_visual_package_director_from_plan calls BGA Pass 1 when BGA_ENABLED=True.

    The from-plan (resume) path bypassed BGA Pass 1 before Blocker 2 was fixed.
    These tests confirm the gate is present and behaves identically to the
    normal VPD path.
    """

    def test_bga_called_when_enabled(self, tmp_path):
        from hypogeum.agents.visual_package_director import (
            run_visual_package_director_from_plan,
        )

        vp = _make_visual_package(n_scenes=2)
        cfg = _make_config(bga_enabled=True)

        with (
            patch("hypogeum.agents.brand_guardian_agent.run_bga_pass1") as mock_bga,
            patch(
                "hypogeum.agents.visual_package_director._execute_muapi_phase",
                return_value=vp,
            ),
            patch("hypogeum.agents.visual_package_director._write_final_package"),
            patch("hypogeum.agents.visual_package_director.build_thumbnail_handoff_brief"),
            patch("hypogeum.agents.visual_package_director.MUAPIBudgetGuard"),
        ):
            run_visual_package_director_from_plan(vp, cfg, "ep_000", output_dir=tmp_path)

        mock_bga.assert_called_once()

    def test_bga_not_called_when_disabled(self, tmp_path):
        from hypogeum.agents.visual_package_director import (
            run_visual_package_director_from_plan,
        )

        vp = _make_visual_package(n_scenes=2)
        cfg = _make_config(bga_enabled=False)

        with (
            patch("hypogeum.agents.brand_guardian_agent.run_bga_pass1") as mock_bga,
            patch(
                "hypogeum.agents.visual_package_director._execute_muapi_phase",
                return_value=vp,
            ),
            patch("hypogeum.agents.visual_package_director._write_final_package"),
            patch("hypogeum.agents.visual_package_director.build_thumbnail_handoff_brief"),
            patch("hypogeum.agents.visual_package_director.MUAPIBudgetGuard"),
        ):
            run_visual_package_director_from_plan(vp, cfg, "ep_000", output_dir=tmp_path)

        mock_bga.assert_not_called()

    def test_bga_rejection_halts_before_muapi(self, tmp_path):
        """BGARejectedError must propagate before _execute_muapi_phase is called."""
        from hypogeum.agents.visual_package_director import (
            run_visual_package_director_from_plan,
        )

        vp = _make_visual_package()
        cfg = _make_config(bga_enabled=True)
        fake_report = _build_report(1, "ep_000", [_blocking_violation()], "REJECTED", 3, "r")

        mock_muapi = MagicMock()
        with (
            patch(
                "hypogeum.agents.brand_guardian_agent.run_bga_pass1",
                side_effect=BGARejectedError("ep_000", 1, fake_report),
            ),
            patch(
                "hypogeum.agents.visual_package_director._execute_muapi_phase",
                mock_muapi,
            ),
            patch("hypogeum.agents.visual_package_director.MUAPIBudgetGuard"),
        ):
            with pytest.raises(BGARejectedError):
                run_visual_package_director_from_plan(vp, cfg, "ep_000", output_dir=tmp_path)

        mock_muapi.assert_not_called()


# ---------------------------------------------------------------------------
# TestGateVerdictPersistence  (P12-03: BGA observability fix)
# ---------------------------------------------------------------------------

class TestGateVerdictPersistence:
    """
    Verify that _write_gate_verdict() persists verdict rows to gate_verdicts
    for every BGA outcome, including rejections — which previously lost their
    violation details as soon as BGARejectedError was raised.
    """

    def _setup_db(self, tmp_path, run_id: str, episode_id: str):
        """
        Initialise an isolated test DB, insert the required runs row so the
        gate_verdicts FK constraint is satisfied, and return the DB path.
        """
        from hypogeum.db import initialize_schema, get_connection
        from datetime import datetime, timezone
        db = tmp_path / "test.db"
        initialize_schema(db)
        # gate_verdicts.run_id FK references runs.run_id
        conn = get_connection(db)
        conn.execute(
            """INSERT OR IGNORE INTO runs (run_id, episode_id, started_at, status, metadata_version)
               VALUES (?, ?, ?, 'running', 'v1')""",
            (run_id, episode_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
        return db

    def _read_verdicts(self, db):
        import sqlite3
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM gate_verdicts ORDER BY evaluated_at").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def test_pass1_rejection_writes_gate_verdict(self, tmp_path):
        """A blocking violation must produce a gate_verdicts row with REJECTED verdict."""
        db = self._setup_db(tmp_path, "run-test-bga-001", "ep_p12_03")
        cfg = _make_config()
        vp  = _make_visual_package(n_scenes=3)

        from hypogeum.finance import set_run_context, clear_run_context
        set_run_context("run-test-bga-001", "ep_p12_03", db_path=db)
        try:
            with patch(
                "hypogeum.agents.brand_guardian_agent.call_claude",
                return_value=_call_result(_violations_json([_blocking_violation()])),
            ):
                with pytest.raises(BGARejectedError):
                    run_bga_pass1(vp, "ep_p12_03", cfg, output_dir=tmp_path)
        finally:
            clear_run_context()

        rows = self._read_verdicts(db)
        assert len(rows) == 1
        assert rows[0]["verdict"] == "REJECTED"
        assert rows[0]["episode_id"] == "ep_p12_03"
        assert rows[0]["run_id"] == "run-test-bga-001"
        assert rows[0]["gate_name"] == "bga_pass1"

    def test_pass1_rejection_persists_full_violation_payload(self, tmp_path):
        """The full_report JSON in gate_verdicts must contain the violation list."""
        db = self._setup_db(tmp_path, "run-test-bga-002", "ep_p12_03")
        cfg = _make_config()
        vp  = _make_visual_package(n_scenes=3)
        violation = _blocking_violation(item_id="sc_001", rule_id="E-VG05")

        from hypogeum.finance import set_run_context, clear_run_context
        set_run_context("run-test-bga-002", "ep_p12_03", db_path=db)
        try:
            with patch(
                "hypogeum.agents.brand_guardian_agent.call_claude",
                return_value=_call_result(_violations_json([violation])),
            ):
                with pytest.raises(BGARejectedError):
                    run_bga_pass1(vp, "ep_p12_03", cfg, output_dir=tmp_path)
        finally:
            clear_run_context()

        rows = self._read_verdicts(db)
        assert len(rows) == 1
        full = json.loads(rows[0]["full_report"])
        assert full["outcome"] == "REJECTED"
        assert len(full["violations"]) == 1
        assert full["violations"][0]["rule_id"] == "E-VG05"
        assert full["violations"][0]["item_id"] == "sc_001"

    def test_pass1_rejection_blocking_count_and_rule_ids(self, tmp_path):
        """blocking_count column and rule_ids_flagged must be populated on rejection."""
        db = self._setup_db(tmp_path, "run-test-bga-003", "ep_p12_03")
        cfg = _make_config()
        vp  = _make_visual_package(n_scenes=4)
        violations = [
            _blocking_violation("sc_001", "E-VG05"),
            _blocking_violation("sc_002", "E-C02"),
        ]

        from hypogeum.finance import set_run_context, clear_run_context
        set_run_context("run-test-bga-003", "ep_p12_03", db_path=db)
        try:
            with patch(
                "hypogeum.agents.brand_guardian_agent.call_claude",
                return_value=_call_result(_violations_json(violations)),
            ):
                with pytest.raises(BGARejectedError):
                    run_bga_pass1(vp, "ep_p12_03", cfg, output_dir=tmp_path)
        finally:
            clear_run_context()

        rows = self._read_verdicts(db)
        assert rows[0]["blocking_count"] == 2
        rule_ids = set((rows[0]["rule_ids_flagged"] or "").split(","))
        assert "E-VG05" in rule_ids
        assert "E-C02" in rule_ids

    def test_approved_outcome_also_writes_verdict(self, tmp_path):
        """APPROVED outcome must also be recorded so the audit trail is complete."""
        db = self._setup_db(tmp_path, "run-test-bga-004", "ep_p12_03")
        cfg = _make_config()
        vp  = _make_visual_package(n_scenes=3)

        from hypogeum.finance import set_run_context, clear_run_context
        set_run_context("run-test-bga-004", "ep_p12_03", db_path=db)
        try:
            with patch(
                "hypogeum.agents.brand_guardian_agent.call_claude",
                return_value=_call_result(_no_violations_json()),
            ):
                run_bga_pass1(vp, "ep_p12_03", cfg, output_dir=tmp_path)
        finally:
            clear_run_context()

        rows = self._read_verdicts(db)
        assert len(rows) == 1
        assert rows[0]["verdict"] == "APPROVED"
        assert rows[0]["blocking_count"] == 0

    def test_no_run_context_does_not_raise(self, tmp_path):
        """If no active run context (standalone BGA call), write is silently skipped."""
        from hypogeum.finance import clear_run_context
        clear_run_context()   # ensure no context

        cfg = _make_config()
        vp  = _make_visual_package(n_scenes=3)

        # Must not raise — the DB write is silently skipped
        with patch(
            "hypogeum.agents.brand_guardian_agent.call_claude",
            return_value=_call_result(_violations_json([_blocking_violation()])),
        ):
            with pytest.raises(BGARejectedError):
                run_bga_pass1(vp, "ep_p12_03", cfg, output_dir=tmp_path)
        # Reaching here means no exception from the missing context

    def test_pass2_rejection_writes_gate_verdict(self, tmp_path):
        """Pass 2 rejection (rank-1 blocking) must also persist a gate_verdicts row."""
        db = self._setup_db(tmp_path, "run-test-bga-005", "ep_p12_03")
        cfg = _make_config()
        plan = _make_generation_plan(n_main=3)

        from hypogeum.finance import set_run_context, clear_run_context
        set_run_context("run-test-bga-005", "ep_p12_03", db_path=db)
        try:
            with patch(
                "hypogeum.agents.brand_guardian_agent.call_claude",
                return_value=_call_result(_violations_json([_blocking_violation(item_id="main_01")])),
            ):
                with pytest.raises(BGARejectedError):
                    run_bga_pass2(plan, "ep_p12_03", cfg, output_dir=tmp_path)
        finally:
            clear_run_context()

        rows = self._read_verdicts(db)
        assert len(rows) == 1
        assert rows[0]["gate_name"] == "bga_pass2"
        assert rows[0]["verdict"] == "REJECTED"
        full = json.loads(rows[0]["full_report"])
        assert full["violations"][0]["item_id"] == "main_01"
