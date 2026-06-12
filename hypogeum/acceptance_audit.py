"""
hypogeum/acceptance_audit.py — Factory Acceptance Audit Gate

Hard gate that determines whether the factory infrastructure is complete
enough to allow any smoke run or episode run.

Two tiers of checks:
    BLOCKER  — must pass for ready=True. All nine factory sub-systems.
    WARNING  — logged but do not set ready=False. Credentials, docs, hooks.

ready=True iff all blocker checks pass.

No network calls. No writes to episode state. No external APIs called.
Credentials are checked for presence only (env var / Config field).

Public API:
    run_acceptance_audit(db_path=None) -> AcceptanceAuditReport
    format_report(report) -> str        # terminal-friendly text

CLI:
    python3 run_acceptance_audit.py [--json] [--db-path PATH]

Exit codes:
    0  — all blockers passed (ACCEPTED)
    1  — one or more blockers failed (REJECTED)
"""

from __future__ import annotations

import importlib
import inspect
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
METADATA_VERSION = "v1"

# Tier labels
_BLOCKER = "blocker"
_WARNING = "warning"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    name:     str
    tier:     str          # "blocker" | "warning"
    passed:   bool
    detail:   str = ""
    issues:   list[str] = field(default_factory=list)


@dataclass
class AcceptanceAuditReport:
    """
    Factory acceptance gate report.

    ready=True iff every blocker check passed.
    """
    ready:            bool
    checked_at:       str
    passed_checks:    list[CheckResult] = field(default_factory=list)
    failed_checks:    list[CheckResult] = field(default_factory=list)
    warnings:         list[CheckResult] = field(default_factory=list)
    blockers:         list[CheckResult] = field(default_factory=list)
    metadata_version: str               = METADATA_VERSION


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check(name: str, tier: str, fn) -> CheckResult:
    """Run fn() and wrap result in a CheckResult. Never raises."""
    try:
        passed, detail, issues = fn()
        return CheckResult(name=name, tier=tier, passed=passed,
                           detail=detail, issues=issues)
    except Exception as exc:
        return CheckResult(name=name, tier=tier, passed=False,
                           detail="", issues=[f"check raised: {exc}"])


def _assert_attrs(module, *names) -> tuple[bool, str, list[str]]:
    """Return (ok, detail, issues) asserting all names exist on module."""
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        return False, "", [f"missing: {', '.join(missing)}"]
    return True, f"{len(names)} symbols present", []


# ---------------------------------------------------------------------------
# BLOCKER checks (1–9)
# ---------------------------------------------------------------------------

def _check_db_schema_v2(db_path: Optional[Path]) -> tuple[bool, str, list[str]]:
    """DB schema v2 can be initialized and v2 tables exist."""
    from hypogeum.db import get_connection, initialize_schema

    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "acceptance_probe.db"
        initialize_schema(probe)
        conn = get_connection(probe)
        try:
            tables = {
                r[0] for r in
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()

    v2_required = {
        "stage_executions", "gate_verdicts", "asset_versions",
        "budget_authorizations", "archive_index", "learning_records",
    }
    missing = v2_required - tables
    if missing:
        return False, "", [f"v2 tables missing: {sorted(missing)}"]
    return True, f"{len(tables)} tables present", []


def _check_asset_registry() -> tuple[bool, str, list[str]]:
    """Asset Registry importable with required public API."""
    import hypogeum.asset_registry as m
    return _assert_attrs(m,
        "compute_asset_slot_id",
        "register_asset_attempt",
        "register_asset_success",
        "register_asset_failure",
        "register_existing_asset",
        "infer_source_stage",
        "normalize_asset_type",
    )


def _check_archive_recall() -> tuple[bool, str, list[str]]:
    """Archive Recall importable with required public API."""
    import hypogeum.archive_recall as m
    return _assert_attrs(m,
        "recall_assets",
        "recall_reusable_assets",
        "recall_by_semantic_tags",
        "recall_by_visual_style",
        "get_asset_lineage",
        "get_asset_versions",
        "get_best_asset_for_slot",
    )


def _check_reuse_engine() -> tuple[bool, str, list[str]]:
    """Reuse Engine importable with required public API."""
    import hypogeum.reuse_engine as m
    return _assert_attrs(m,
        "score_asset",
        "recommend_reuse",
        "get_top_candidate",
        "should_reuse",
        "ReuseCandidate",
    )


def _check_agent_auditor() -> tuple[bool, str, list[str]]:
    """Agent Performance Auditor importable with required public API."""
    import hypogeum.agent_auditor as m
    return _assert_attrs(m,
        "audit_stage",
        "audit_gates",
        "audit_asset_generation",
        "get_failure_summary",
        "run_health_summary",
        "audit_agents",
        "AgentAuditReport",
    )


def _check_evolution_engine() -> tuple[bool, str, list[str]]:
    """Factory Evolution Engine importable with required public API."""
    import hypogeum.evolution_engine as m
    return _assert_attrs(m,
        "get_provider_recommendations",
        "get_cost_recommendations",
        "get_gate_recommendations",
        "analyze_episode",
        "record_adjustment",
        "evolve",
        "EvolutionReport",
    )


def _check_channel_manager() -> tuple[bool, str, list[str]]:
    """Multi-Channel Architecture importable with required public API."""
    import hypogeum.channel_manager as m
    return _assert_attrs(m,
        "create_channel",
        "get_channel",
        "list_channels",
        "update_identity",
        "create_template",
        "get_templates",
        "duplicate_channel",
        "regenerate_prompt",
        "assign_episode_to_channel",
        "get_channel_episodes",
        "Channel",
        "ChannelIdentity",
    )


def _check_platform_connectors() -> tuple[bool, str, list[str]]:
    """Platform Connectors importable; all 5 platforms registered."""
    from hypogeum.publishing.connectors import (
        get_supported_platforms,
        validate_request,
        get_connector,
        build_publish_request,
    )
    supported = set(get_supported_platforms())
    required  = {"youtube", "facebook", "instagram", "tiktok", "email"}
    missing   = required - supported
    if missing:
        return False, "", [f"platforms not registered: {sorted(missing)}"]
    return True, f"{len(supported)} platforms registered", []


def _check_dashboard() -> tuple[bool, str, list[str]]:
    """Dashboard importable; snapshot and format_text callable."""
    import hypogeum.dashboard as m

    ok, detail, issues = _assert_attrs(m, "snapshot", "format_text",
                                       "DashboardSnapshot")
    if not ok:
        return ok, detail, issues

    # Verify snapshot() is callable (no args needed — uses default DB path)
    if not callable(m.snapshot):
        return False, "", ["snapshot is not callable"]
    if not callable(m.format_text):
        return False, "", ["format_text is not callable"]
    return True, "snapshot + format_text callable", []


# ---------------------------------------------------------------------------
# WARNING checks (10–12)
# ---------------------------------------------------------------------------

def _check_identity_governance_docs() -> tuple[bool, str, list[str]]:
    """Key identity governance files present on disk."""
    required = [
        _PROJECT_ROOT / "docs" / "channel-identity.md",
        _PROJECT_ROOT / "docs" / "brand_guardian" / "brand_bible_hypogeum.json",
        _PROJECT_ROOT / "hypogeum" / "world_bible.py",
    ]
    missing = [str(p.relative_to(_PROJECT_ROOT)) for p in required if not p.exists()]
    if missing:
        return False, "", [f"missing: {m}" for m in missing]
    return True, f"{len(required)} governance files present", []


def _check_production_readiness_audit_callable() -> tuple[bool, str, list[str]]:
    """run_production_readiness_audit.py exists and its run_audit() is importable."""
    script = _PROJECT_ROOT / "run_production_readiness_audit.py"
    if not script.exists():
        return False, "", ["run_production_readiness_audit.py not found"]

    try:
        import importlib.util
        spec   = importlib.util.spec_from_file_location("_pra", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, "run_audit"):
            return False, "", ["run_audit() not found in script"]
        return True, "run_audit() importable", []
    except Exception as exc:
        return False, "", [f"import failed: {exc}"]


def _check_platform_credentials() -> tuple[bool, str, list[str]]:
    """
    Check that required platform credentials are set.

    Reads from environment variables only — no external API calls.
    Missing credentials are reported as issues but this is a WARNING tier.
    """
    from hypogeum.config import Config
    try:
        cfg = Config()
    except Exception as exc:
        return False, "", [f"Config() failed: {exc}"]

    checks = {
        "MUAPI_API_KEY":          getattr(cfg, "MUAPI_API_KEY", "") or "",
        "ELEVENLABS_API_KEY":     getattr(cfg, "ELEVENLABS_API_KEY", "") or "",
        "ELEVENLABS_VOICE_ID":    getattr(cfg, "ELEVENLABS_VOICE_ID", "") or "",
        "YOUTUBE_CLIENT_ID":      getattr(cfg, "YOUTUBE_CLIENT_ID", "") or "",
        "YOUTUBE_REFRESH_TOKEN":  getattr(cfg, "YOUTUBE_REFRESH_TOKEN", "") or "",
        "OPENROUTER_API_KEY":     getattr(cfg, "OPENROUTER_API_KEY", "") or "",
    }
    missing = [k for k, v in checks.items() if not v.strip()]
    present = [k for k, v in checks.items() if v.strip()]

    if missing:
        return (
            False, f"{len(present)}/{len(checks)} credentials present",
            [f"credential not set: {k}" for k in sorted(missing)],
        )
    return True, f"all {len(checks)} credentials present", []


# ---------------------------------------------------------------------------
# Public API — run_acceptance_audit
# ---------------------------------------------------------------------------

def run_acceptance_audit(
    db_path: Optional[Path] = None,
) -> AcceptanceAuditReport:
    """
    Run all acceptance checks and return a structured report.

    Non-blocking: every individual check is wrapped in a try/except.
    The audit never raises; a failed check is recorded in failed_checks.

    Args:
        db_path: Override the SQLite path used for DB schema checks.
                 Defaults to .hypogeum-assets/hypogeum.db.

    Returns:
        AcceptanceAuditReport with ready=True iff all blockers pass.
    """
    blocker_specs = [
        ("db_schema_v2",         _BLOCKER, lambda: _check_db_schema_v2(db_path)),
        ("asset_registry",       _BLOCKER, _check_asset_registry),
        ("archive_recall",       _BLOCKER, _check_archive_recall),
        ("reuse_engine",         _BLOCKER, _check_reuse_engine),
        ("agent_auditor",        _BLOCKER, _check_agent_auditor),
        ("evolution_engine",     _BLOCKER, _check_evolution_engine),
        ("channel_manager",      _BLOCKER, _check_channel_manager),
        ("platform_connectors",  _BLOCKER, _check_platform_connectors),
        ("dashboard",            _BLOCKER, _check_dashboard),
    ]
    warning_specs = [
        ("identity_governance_docs",           _WARNING, _check_identity_governance_docs),
        ("production_readiness_audit_callable",_WARNING, _check_production_readiness_audit_callable),
        ("platform_credentials",               _WARNING, _check_platform_credentials),
    ]

    all_results: list[CheckResult] = []
    for name, tier, fn in blocker_specs + warning_specs:
        all_results.append(_check(name, tier, fn))

    passed   = [r for r in all_results if r.passed]
    failed   = [r for r in all_results if not r.passed]
    blockers = [r for r in failed if r.tier == _BLOCKER]
    warnings = [r for r in failed if r.tier == _WARNING]
    ready    = len(blockers) == 0

    return AcceptanceAuditReport(
        ready         = ready,
        checked_at    = _now_iso(),
        passed_checks = passed,
        failed_checks = failed,
        warnings      = warnings,
        blockers      = blockers,
    )


# ---------------------------------------------------------------------------
# Public API — format_report
# ---------------------------------------------------------------------------

def format_report(report: AcceptanceAuditReport) -> str:
    """
    Render an AcceptanceAuditReport as terminal-friendly text.

    Args:
        report: Report produced by run_acceptance_audit().

    Returns:
        Multi-line string.
    """
    sep    = "═" * 62
    lines  = [sep]
    status = "ACCEPTED" if report.ready else "REJECTED"
    lines.append("  Hypogeum Factory Acceptance Audit")
    lines.append(f"  {report.checked_at}")
    lines.append(f"  Result: {status}")
    total   = len(report.passed_checks) + len(report.failed_checks)
    lines.append(
        f"  {len(report.passed_checks)}/{total} checks passed  "
        f"({len(report.blockers)} blocker(s)  "
        f"{len(report.warnings)} warning(s))"
    )
    lines.append(sep)

    if report.blockers:
        lines.append("BLOCKERS (must fix before any run):")
        for r in report.blockers:
            lines.append(f"  ✗  {r.name}")
            for issue in r.issues:
                lines.append(f"       ! {issue}")
        lines.append("")

    if report.warnings:
        lines.append("WARNINGS (recommended but not blocking):")
        for r in report.warnings:
            lines.append(f"  ⚠  {r.name}")
            for issue in r.issues:
                lines.append(f"       · {issue}")
        lines.append("")

    lines.append("PASSED:")
    for r in report.passed_checks:
        tier_tag = "" if r.tier == _BLOCKER else " [warn]"
        lines.append(f"  ✓  {r.name}{tier_tag}")
        if r.detail:
            lines.append(f"       {r.detail}")

    lines.append("")
    lines.append(sep)
    if report.ready:
        lines.append("  Factory is ACCEPTED. Smoke run gate may now be evaluated.")
    else:
        lines.append(
            f"  Factory is REJECTED. Fix {len(report.blockers)} blocker(s) "
            "before any run."
        )
    lines.append(sep)
    return "\n".join(lines)
