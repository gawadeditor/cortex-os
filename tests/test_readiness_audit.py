"""
tests/test_readiness_audit.py — Production readiness audit tests

Coverage:
    TestModelSlugChecks     — reports invalid model slugs
    TestPhase5Check         — phase5_hooks_callable check works
    TestDbCheck             — db_write_path check works
    TestArchiveCheck        — archive_write_path check works
    TestNoContentGenerated  — audit does not write episode or pipeline files
    TestReportStructure     — run_audit returns expected report structure
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Import the check functions directly for unit testing
# ---------------------------------------------------------------------------

import os
from unittest.mock import patch as _patch

from run_production_readiness_audit import (
    check_model_slugs,
    check_db_write_path,
    check_archive_write_path,
    check_phase5_hooks_callable,
    run_audit,
    _KNOWN_INVALID_SLUGS,
)

_MINIMAL_ENV = {
    "OPENROUTER_API_KEY": "sk-or-test",
    "OPENROUTER_SONNET_MODEL_ID": "anthropic/claude-sonnet-4.6",
    "OPENROUTER_OPUS_MODEL_ID": "anthropic/claude-opus-4.6",
}


# ---------------------------------------------------------------------------
# TestModelSlugChecks
# ---------------------------------------------------------------------------

class TestModelSlugChecks:
    def _make_config(self, **overrides):
        class FakeConfig:
            EP_MODEL = "anthropic/claude-opus-4.6"
            RD_MODEL = "anthropic/claude-opus-4.6"
            TD_PLANNING_MODEL = "anthropic/claude-sonnet-4.6"
            BGA_PASS1_MODEL = "anthropic/claude-sonnet-4.6"
            BGA_PASS2_MODEL = "anthropic/claude-sonnet-4.6"
        for k, v in overrides.items():
            setattr(FakeConfig, k, v)
        return FakeConfig()

    def test_valid_slugs_pass(self):
        result = check_model_slugs(self._make_config())
        assert result["ok"] is True
        assert result["issues"] == []

    def test_empty_ep_model_fails_check(self):
        result = check_model_slugs(self._make_config(EP_MODEL=""))
        assert result["ok"] is False
        assert any("EP_MODEL" in i for i in result["issues"])

    def test_empty_td_planning_model_fails_check(self):
        result = check_model_slugs(self._make_config(TD_PLANNING_MODEL=""))
        assert result["ok"] is False
        assert any("TD_PLANNING_MODEL" in i for i in result["issues"])

    def test_empty_bga_pass2_model_fails_check(self):
        result = check_model_slugs(self._make_config(BGA_PASS2_MODEL=""))
        assert result["ok"] is False
        assert any("BGA_PASS2_MODEL" in i for i in result["issues"])

    def test_empty_ep_model_fails(self):
        result = check_model_slugs(self._make_config(EP_MODEL=""))
        assert result["ok"] is False
        assert any("EP_MODEL" in i for i in result["issues"])

    def test_three_empty_slugs_produces_three_issues(self):
        result = check_model_slugs(self._make_config(
            EP_MODEL="",
            TD_PLANNING_MODEL="",
            BGA_PASS2_MODEL="",
        ))
        assert result["ok"] is False
        assert len(result["issues"]) == 3

    def test_known_invalid_set_is_empty_after_verification(self):
        # Both anthropic/claude-fable-5 and openai/gpt-5.5-pro confirmed live
        # on OpenRouter 2026-06-12 — the set is empty until a slug is delisted
        assert "anthropic/claude-fable-5" not in _KNOWN_INVALID_SLUGS
        assert "openai/gpt-5.5-pro" not in _KNOWN_INVALID_SLUGS


# ---------------------------------------------------------------------------
# TestPhase5Check
# ---------------------------------------------------------------------------

class TestPhase5Check:
    def test_phase5_callable_check_returns_dict(self):
        result = check_phase5_hooks_callable()
        assert isinstance(result, dict)
        assert "ok" in result
        assert "check" in result

    def test_phase5_callable_check_passes(self):
        result = check_phase5_hooks_callable()
        assert result["ok"] is True, f"Phase 5 check failed: {result['issues']}"

    def test_phase5_check_name(self):
        result = check_phase5_hooks_callable()
        assert result["check"] == "phase5_hooks_callable"


# ---------------------------------------------------------------------------
# TestDbCheck
# ---------------------------------------------------------------------------

class TestDbCheck:
    def test_db_write_path_passes(self):
        result = check_db_write_path()
        assert result["ok"] is True

    def test_db_check_name(self):
        result = check_db_write_path()
        assert result["check"] == "db_write_path"


# ---------------------------------------------------------------------------
# TestArchiveCheck
# ---------------------------------------------------------------------------

class TestArchiveCheck:
    def test_archive_write_path_passes(self):
        result = check_archive_write_path()
        assert result["ok"] is True

    def test_archive_check_name(self):
        result = check_archive_write_path()
        assert result["check"] == "archive_write_path"


# ---------------------------------------------------------------------------
# TestNoContentGenerated
# ---------------------------------------------------------------------------

class TestNoContentGenerated:
    def test_audit_does_not_create_episode_files(self, tmp_path):
        """run_audit must not create any episode_ or pipeline_ prefixed files."""
        with _patch.dict(os.environ, _MINIMAL_ENV):
            run_audit(episode_id="ep_000")
        cwd = Path(__file__).resolve().parent.parent
        episode_files = list(cwd.glob("episode_*.json")) + list(cwd.glob("pipeline_*.json"))
        assert len(episode_files) == 0, f"Audit created episode files: {episode_files}"

    def test_audit_does_not_call_pipeline(self):
        """run_audit must not call run_pipeline or any agent entry point."""
        with _patch.dict(os.environ, _MINIMAL_ENV):
            with _patch("hypogeum.pipeline.run_pipeline") as mock_pipeline:
                run_audit(episode_id="ep_000")
                mock_pipeline.assert_not_called()


# ---------------------------------------------------------------------------
# TestReportStructure
# ---------------------------------------------------------------------------

class TestReportStructure:
    def _run(self, episode_id="ep_000"):
        with _patch.dict(os.environ, _MINIMAL_ENV):
            return run_audit(episode_id=episode_id)

    def test_report_has_required_fields(self):
        report = self._run()
        for field in ("schema_version", "episode_id", "audited_at", "ready",
                      "passed_count", "failed_count", "checks"):
            assert field in report, f"Missing field: {field}"

    def test_report_episode_id_matches(self):
        report = self._run(episode_id="ep_test")
        assert report["episode_id"] == "ep_test"

    def test_checks_is_list(self):
        report = self._run()
        assert isinstance(report["checks"], list)

    def test_each_check_has_ok_field(self):
        report = self._run()
        for check in report["checks"]:
            assert "ok" in check, f"Check missing 'ok': {check}"

    def test_each_check_has_check_name(self):
        report = self._run()
        for check in report["checks"]:
            assert "check" in check

    def test_passed_count_plus_failed_count_equals_total(self):
        report = self._run()
        assert report["passed_count"] + report["failed_count"] == len(report["checks"])

    def test_schema_version_v1(self):
        report = self._run()
        assert report["schema_version"] == "v1"

    def test_ready_is_bool(self):
        report = self._run()
        assert isinstance(report["ready"], bool)
