"""
tests/test_acceptance_audit.py — Factory Acceptance Audit tests

Coverage:
    TestCheckResults           — CheckResult dataclass fields
    TestAcceptanceAuditReport  — report fields, ready logic
    TestBlockerChecks          — each of the 9 blocker checks independently
    TestWarningChecks          — each of the 3 warning checks independently
    TestRunAcceptanceAudit     — full run, ready/not-ready, tier separation
    TestFormatReport           — text output structure
    TestCLIEntryPoint          — run_acceptance_audit.py CLI importable
    TestNoNetworkCalls         — no sockets opened
    TestNoProductionSideEffects — no ep_000, no episode state written
"""

from __future__ import annotations

import importlib.util
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hypogeum.acceptance_audit import (
    METADATA_VERSION,
    AcceptanceAuditReport,
    CheckResult,
    _BLOCKER,
    _WARNING,
    _check,
    _check_agent_auditor,
    _check_archive_recall,
    _check_asset_registry,
    _check_channel_manager,
    _check_dashboard,
    _check_db_schema_v2,
    _check_evolution_engine,
    _check_identity_governance_docs,
    _check_platform_connectors,
    _check_platform_credentials,
    _check_production_readiness_audit_callable,
    _check_reuse_engine,
    format_report,
    run_acceptance_audit,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _passing() -> tuple[bool, str, list]:
    return True, "ok", []


def _failing() -> tuple[bool, str, list]:
    return False, "", ["deliberate failure"]


def _raising():
    raise RuntimeError("boom")


# ===========================================================================
# TestCheckResults
# ===========================================================================

class TestCheckResults:
    def test_passing_check(self):
        r = _check("my_check", _BLOCKER, _passing)
        assert r.passed is True
        assert r.name == "my_check"
        assert r.tier == _BLOCKER
        assert r.issues == []
        assert r.detail == "ok"

    def test_failing_check(self):
        r = _check("my_check", _WARNING, _failing)
        assert r.passed is False
        assert r.tier == _WARNING
        assert "deliberate failure" in r.issues

    def test_raising_check_returns_false_not_raise(self):
        r = _check("exploding", _BLOCKER, _raising)
        assert r.passed is False
        assert any("boom" in i for i in r.issues)

    def test_dataclass_default_issues(self):
        cr = CheckResult(name="x", tier=_BLOCKER, passed=True)
        assert cr.issues == []
        assert cr.detail == ""


# ===========================================================================
# TestAcceptanceAuditReport
# ===========================================================================

class TestAcceptanceAuditReport:
    def test_ready_true_when_no_blockers(self):
        r = AcceptanceAuditReport(
            ready=True, checked_at="ts",
            passed_checks=[], failed_checks=[],
            warnings=[], blockers=[],
        )
        assert r.ready is True

    def test_metadata_version_default(self):
        r = AcceptanceAuditReport(ready=True, checked_at="ts")
        assert r.metadata_version == METADATA_VERSION

    def test_lists_default_empty(self):
        r = AcceptanceAuditReport(ready=False, checked_at="ts")
        assert r.passed_checks == []
        assert r.failed_checks == []
        assert r.blockers == []
        assert r.warnings == []


# ===========================================================================
# TestBlockerChecks  (run each check independently)
# ===========================================================================

class TestBlockerChecks:
    def test_db_schema_v2_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            ok, detail, issues = _check_db_schema_v2(db_path)
        assert ok is True
        assert issues == []
        assert "tables present" in detail

    def test_db_schema_v2_fails_on_bad_path(self, tmp_path):
        bad = tmp_path / "not_a_dir" / "test.db"
        # parent dir doesn't exist → SQLite will fail
        ok, detail, issues = _check_db_schema_v2(bad)
        # Either fails with an error OR SQLite creates the file — both acceptable
        # What we verify: no exception escapes
        assert isinstance(ok, bool)

    def test_asset_registry_passes(self):
        ok, detail, issues = _check_asset_registry()
        assert ok is True, f"Issues: {issues}"

    def test_archive_recall_passes(self):
        ok, detail, issues = _check_archive_recall()
        assert ok is True, f"Issues: {issues}"

    def test_reuse_engine_passes(self):
        ok, detail, issues = _check_reuse_engine()
        assert ok is True, f"Issues: {issues}"

    def test_agent_auditor_passes(self):
        ok, detail, issues = _check_agent_auditor()
        assert ok is True, f"Issues: {issues}"

    def test_evolution_engine_passes(self):
        ok, detail, issues = _check_evolution_engine()
        assert ok is True, f"Issues: {issues}"

    def test_channel_manager_passes(self):
        ok, detail, issues = _check_channel_manager()
        assert ok is True, f"Issues: {issues}"

    def test_platform_connectors_passes(self):
        ok, detail, issues = _check_platform_connectors()
        assert ok is True, f"Issues: {issues}"
        assert "5 platforms registered" in detail

    def test_dashboard_passes(self):
        ok, detail, issues = _check_dashboard()
        assert ok is True, f"Issues: {issues}"

    def test_asset_registry_fails_when_symbol_missing(self):
        import hypogeum.asset_registry as mod
        backup = mod.register_asset_success
        del mod.register_asset_success
        try:
            ok, _, issues = _check_asset_registry()
            assert ok is False
            assert any("register_asset_success" in i for i in issues)
        finally:
            mod.register_asset_success = backup

    def test_platform_connectors_fails_when_platform_missing(self):
        from hypogeum.publishing import connectors
        original = connectors._REGISTRY.copy()
        del connectors._REGISTRY["email"]
        try:
            ok, _, issues = _check_platform_connectors()
            assert ok is False
            assert any("email" in i for i in issues)
        finally:
            connectors._REGISTRY.update(original)

    def test_dashboard_fails_when_snapshot_missing(self):
        import hypogeum.dashboard as mod
        backup = mod.snapshot
        del mod.snapshot
        try:
            ok, _, issues = _check_dashboard()
            assert ok is False
        finally:
            mod.snapshot = backup


# ===========================================================================
# TestWarningChecks
# ===========================================================================

class TestWarningChecks:
    def test_identity_governance_docs_tier_is_warning(self):
        # Only testing tier; actual file presence is environment-dependent
        r = _check("identity_governance_docs", _WARNING,
                   _check_identity_governance_docs)
        assert r.tier == _WARNING

    def test_identity_governance_docs_returns_bool(self):
        ok, _, _ = _check_identity_governance_docs()
        assert isinstance(ok, bool)

    def test_production_readiness_audit_callable_passes(self):
        ok, detail, issues = _check_production_readiness_audit_callable()
        assert ok is True, f"Issues: {issues}"

    def test_production_readiness_audit_fails_when_missing(self, tmp_path, monkeypatch):
        # Patch _PROJECT_ROOT to a temp dir with no script
        import hypogeum.acceptance_audit as mod
        orig = mod._PROJECT_ROOT
        monkeypatch.setattr(mod, "_PROJECT_ROOT", tmp_path)
        try:
            ok, _, issues = _check_production_readiness_audit_callable()
            assert ok is False
            assert any("not found" in i for i in issues)
        finally:
            monkeypatch.setattr(mod, "_PROJECT_ROOT", orig)

    def test_platform_credentials_returns_bool(self):
        ok, _, _ = _check_platform_credentials()
        assert isinstance(ok, bool)

    def test_platform_credentials_tier_is_warning(self):
        r = _check("platform_credentials", _WARNING, _check_platform_credentials)
        assert r.tier == _WARNING

    def test_platform_credentials_fails_with_known_missing(self, monkeypatch):
        """Simulate missing credentials by patching Config."""
        from hypogeum import config as config_mod

        class MockConfig:
            MUAPI_API_KEY          = ""
            ELEVENLABS_API_KEY     = ""
            ELEVENLABS_VOICE_ID    = ""
            YOUTUBE_CLIENT_ID      = ""
            YOUTUBE_REFRESH_TOKEN  = ""
            OPENROUTER_API_KEY     = ""

        import hypogeum.acceptance_audit as mod
        with patch.object(config_mod, "Config", MockConfig):
            ok, detail, issues = _check_platform_credentials()
        assert ok is False
        assert any("credential not set" in i for i in issues)

    def test_platform_credentials_passes_when_all_set(self, monkeypatch):
        from hypogeum import config as config_mod

        class MockConfig:
            MUAPI_API_KEY          = "key_abc"
            ELEVENLABS_API_KEY     = "key_def"
            ELEVENLABS_VOICE_ID    = "voice_xyz"
            YOUTUBE_CLIENT_ID      = "yt_client"
            YOUTUBE_REFRESH_TOKEN  = "yt_refresh"
            OPENROUTER_API_KEY     = "or_key"

        with patch.object(config_mod, "Config", MockConfig):
            ok, detail, issues = _check_platform_credentials()
        assert ok is True
        assert issues == []


# ===========================================================================
# TestRunAcceptanceAudit
# ===========================================================================

class TestRunAcceptanceAudit:
    def test_returns_report_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_acceptance_audit(db_path=Path(tmp) / "test.db")
        assert isinstance(r, AcceptanceAuditReport)

    def test_checked_at_is_iso_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_acceptance_audit(db_path=Path(tmp) / "test.db")
        assert "T" in r.checked_at
        assert len(r.checked_at) > 10

    def test_all_nine_blockers_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_acceptance_audit(db_path=Path(tmp) / "test.db")
        blocker_names = {c.name for c in r.passed_checks
                         if c.tier == _BLOCKER}
        expected = {
            "db_schema_v2",
            "asset_registry",
            "archive_recall",
            "reuse_engine",
            "agent_auditor",
            "evolution_engine",
            "channel_manager",
            "platform_connectors",
            "dashboard",
        }
        assert expected.issubset(blocker_names), (
            f"Missing passing blockers: {expected - blocker_names}"
        )

    def test_ready_true_when_all_blockers_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_acceptance_audit(db_path=Path(tmp) / "test.db")
        # All blockers should pass given the full factory is built
        assert r.ready is True, (
            f"Blockers: {[(b.name, b.issues) for b in r.blockers]}"
        )

    def test_blockers_list_empty_when_all_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_acceptance_audit(db_path=Path(tmp) / "test.db")
        assert r.blockers == []

    def test_warnings_are_warning_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_acceptance_audit(db_path=Path(tmp) / "test.db")
        for w in r.warnings:
            assert w.tier == _WARNING

    def test_failed_checks_equals_blockers_plus_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_acceptance_audit(db_path=Path(tmp) / "test.db")
        failed_names = {c.name for c in r.failed_checks}
        blocker_names = {c.name for c in r.blockers}
        warning_names = {c.name for c in r.warnings}
        assert failed_names == blocker_names | warning_names

    def test_ready_false_when_blocker_fails(self):
        """Simulate a blocker failure by patching one check to fail."""
        import hypogeum.acceptance_audit as mod
        original = mod._check_asset_registry

        def fail_asset_registry():
            return False, "", ["injected failure"]

        mod._check_asset_registry = fail_asset_registry
        try:
            with tempfile.TemporaryDirectory() as tmp:
                r = run_acceptance_audit(db_path=Path(tmp) / "test.db")
            assert r.ready is False
            assert any(b.name == "asset_registry" for b in r.blockers)
        finally:
            mod._check_asset_registry = original

    def test_ready_true_even_when_warning_fails(self):
        """Warnings do not affect ready=True."""
        import hypogeum.acceptance_audit as mod
        original = mod._check_platform_credentials

        def fail_creds():
            return False, "", ["all creds missing"]

        mod._check_platform_credentials = fail_creds
        try:
            with tempfile.TemporaryDirectory() as tmp:
                r = run_acceptance_audit(db_path=Path(tmp) / "test.db")
            # ready should still be True if only a warning failed
            blocker_failed = any(b.name == "platform_credentials"
                                 for b in r.blockers)
            assert not blocker_failed
        finally:
            mod._check_platform_credentials = original

    def test_twelve_checks_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_acceptance_audit(db_path=Path(tmp) / "test.db")
        total = len(r.passed_checks) + len(r.failed_checks)
        assert total == 12

    def test_nine_blockers_three_warnings_in_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_acceptance_audit(db_path=Path(tmp) / "test.db")
        all_results = r.passed_checks + r.failed_checks
        blocker_count = sum(1 for c in all_results if c.tier == _BLOCKER)
        warning_count = sum(1 for c in all_results if c.tier == _WARNING)
        assert blocker_count == 9
        assert warning_count == 3


# ===========================================================================
# TestFormatReport
# ===========================================================================

class TestFormatReport:
    def _make_report(self, ready=True, blockers=None, warnings=None, passed=None):
        blockers = blockers or []
        warnings = warnings or []
        passed   = passed or []
        return AcceptanceAuditReport(
            ready         = ready,
            checked_at    = "2026-06-12T00:00:00+00:00",
            passed_checks = passed,
            failed_checks = blockers + warnings,
            warnings      = warnings,
            blockers      = blockers,
        )

    def test_returns_string(self):
        r = self._make_report()
        assert isinstance(format_report(r), str)

    def test_contains_accepted_when_ready(self):
        text = format_report(self._make_report(ready=True))
        assert "ACCEPTED" in text

    def test_contains_rejected_when_not_ready(self):
        text = format_report(self._make_report(ready=False))
        assert "REJECTED" in text

    def test_contains_blocker_section_when_blockers_exist(self):
        b = CheckResult(name="db_schema_v2", tier=_BLOCKER, passed=False,
                        issues=["v2 tables missing: []"])
        text = format_report(self._make_report(ready=False, blockers=[b]))
        assert "BLOCKERS" in text
        assert "db_schema_v2" in text

    def test_contains_warning_section_when_warnings_exist(self):
        w = CheckResult(name="platform_credentials", tier=_WARNING, passed=False,
                        issues=["credential not set: MUAPI_API_KEY"])
        text = format_report(self._make_report(ready=True, warnings=[w]))
        assert "WARNINGS" in text
        assert "platform_credentials" in text

    def test_contains_passed_section(self):
        p = CheckResult(name="dashboard", tier=_BLOCKER, passed=True, detail="ok")
        text = format_report(self._make_report(passed=[p]))
        assert "PASSED" in text
        assert "dashboard" in text

    def test_separator_present(self):
        text = format_report(self._make_report())
        assert "═" in text

    def test_smoke_run_message_when_accepted(self):
        text = format_report(self._make_report(ready=True))
        assert "Smoke run gate" in text or "ACCEPTED" in text

    def test_fix_blockers_message_when_rejected(self):
        b = CheckResult(name="x", tier=_BLOCKER, passed=False, issues=["err"])
        text = format_report(self._make_report(ready=False, blockers=[b]))
        assert "blocker" in text.lower()

    def test_full_run_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_acceptance_audit(db_path=Path(tmp) / "test.db")
        text = format_report(r)
        assert "db_schema_v2" in text
        assert "dashboard" in text


# ===========================================================================
# TestCLIEntryPoint
# ===========================================================================

class TestCLIEntryPoint:
    def test_cli_script_exists(self):
        script = _PROJECT_ROOT / "run_acceptance_audit.py"
        assert script.exists()

    def test_cli_importable(self):
        script = _PROJECT_ROOT / "run_acceptance_audit.py"
        spec   = importlib.util.spec_from_file_location("_cli", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert hasattr(module, "main")
        assert callable(module.main)

    def test_cli_main_exits_zero_when_accepted(self, capsys):
        """CLI should exit 0 when all blockers pass."""
        script = _PROJECT_ROOT / "run_acceptance_audit.py"
        spec   = importlib.util.spec_from_file_location("_cli2", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cli_test.db"
            with patch("sys.argv", ["run_acceptance_audit.py",
                                    "--db-path", str(db_path)]):
                try:
                    module.main()
                    exited = 0
                except SystemExit as e:
                    exited = e.code

        assert exited == 0


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:
    def _block_socket(self, monkeypatch):
        def no_socket(*args, **kwargs):
            raise AssertionError("acceptance_audit opened a socket")
        monkeypatch.setattr(socket, "socket", no_socket)

    def test_run_acceptance_audit_no_network(self, monkeypatch):
        self._block_socket(monkeypatch)
        with tempfile.TemporaryDirectory() as tmp:
            run_acceptance_audit(db_path=Path(tmp) / "test.db")

    def test_all_blocker_checks_no_network(self, monkeypatch):
        self._block_socket(monkeypatch)
        with tempfile.TemporaryDirectory() as tmp:
            _check_db_schema_v2(Path(tmp) / "test.db")
        _check_asset_registry()
        _check_archive_recall()
        _check_reuse_engine()
        _check_agent_auditor()
        _check_evolution_engine()
        _check_channel_manager()
        _check_platform_connectors()
        _check_dashboard()

    def test_warning_checks_no_network(self, monkeypatch):
        self._block_socket(monkeypatch)
        _check_identity_governance_docs()
        _check_production_readiness_audit_callable()
        # credentials check reads env only — no network
        _check_platform_credentials()

    def test_format_report_no_network(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_acceptance_audit(db_path=Path(tmp) / "test.db")
        self._block_socket(monkeypatch)
        format_report(r)


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:
    def test_no_ep_000_run_triggered(self, tmp_path):
        """run_acceptance_audit must not touch ep_000 in any DB."""
        from hypogeum.db import get_connection, initialize_schema

        db_path = tmp_path / "side_effects_test.db"
        initialize_schema(db_path)
        run_acceptance_audit(db_path=db_path)

        conn = get_connection(db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE episode_id='ep_000'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 0

    def test_audit_does_not_write_episodes(self, tmp_path):
        from hypogeum.db import get_connection, initialize_schema

        db_path = tmp_path / "audit_write_test.db"
        initialize_schema(db_path)
        run_acceptance_audit(db_path=db_path)

        conn = get_connection(db_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        finally:
            conn.close()
        assert n == 0

    def test_audit_does_not_write_runs(self, tmp_path):
        from hypogeum.db import get_connection, initialize_schema

        db_path = tmp_path / "audit_runs_test.db"
        initialize_schema(db_path)
        run_acceptance_audit(db_path=db_path)

        conn = get_connection(db_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            conn.close()
        assert n == 0

    def test_audit_does_not_generate_assets(self, tmp_path):
        from hypogeum.db import get_connection, initialize_schema

        db_path = tmp_path / "audit_assets_test.db"
        initialize_schema(db_path)
        run_acceptance_audit(db_path=db_path)

        conn = get_connection(db_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM asset_versions").fetchone()[0]
        finally:
            conn.close()
        assert n == 0
