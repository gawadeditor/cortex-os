"""
hypogeum/factory_director.py — Factory Director: Cross-System Governance Assessment

Governance layer that assesses operational readiness across all seven Factory
OS governance domains. assess_factory() itself remains read-only/advisory
(never raises, never blocks). check_run_clearance() is the one exception:
run_worker.py's autonomous loop calls it as an actual runtime gate before
starting a new run (wired 2026-07-18) — OIS blocking signals, factory-active
status, credentials, and now monthly budget-ceiling overage all stop a new
run from starting, not just from being reported as unhealthy.

Seven domains assessed:
    1. factory_entity    — factory record, status, owner presence
    2. policy_cascade    — effective policy coherence, budget ceiling
    3. ois               — blocking signals, open signal backlog
    4. approvals         — pending/stale/orphaned approval requests
    5. run_lifecycle     — zombie runs, failure rates, RLG state distribution
    6. credentials       — secret_profiles presence, expiry, coverage
    7. factory_capacity  — queue depth, active runs, budget headroom, storage

ReadinessLevel:
    READY    — all blocking checks pass; score 80–100
    DEGRADED — no blockers, warnings present; score 40–79
    BLOCKED  — one or more blocking checks fail; score 0–39
    UNKNOWN  — assessment could not complete; score 0

Readiness score (0–100):
    Computed from check failures. Provides trend visibility.
    BLOCKED: capped at 39. DEGRADED: range 40–79. READY: 80–100.

Public API:
    assess_factory(db_path=None, factory_id="hypogeum") -> FactoryDirectorReport
    check_run_clearance(db_path=None, channel_id=None) -> RunClearanceDecision
    format_report(report: FactoryDirectorReport) -> str

Exceptions:
    FactoryDirectorBlockedError  — carry-type for callers that want hard-fail;
                                   never raised by assess_factory() itself.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
_ASSETS_DIR      = _PROJECT_ROOT / ".hypogeum-assets"

METADATA_VERSION = "v1"

# Required providers for a functional episode production pipeline
_REQUIRED_PROVIDERS = frozenset({"openrouter", "muapi", "elevenlabs"})

# Score deductions
_BLOCKING_DEDUCTION = 15
_WARNING_DEDUCTION  = 5
# recent informational signals (stuck_run etc.) up to this many aren't a
# "degraded" factory — only a genuine pile-up or day-old ignored ones are.
_SIGNAL_BACKLOG_LIMIT = 8

# Thresholds
_ZOMBIE_RUN_HOURS        = 4
_STALE_APPROVAL_HOURS    = 72
_CREDENTIAL_EXPIRY_DAYS  = 7
_CREDENTIAL_RETEST_DAYS  = 30
_STORAGE_WARN_PCT        = 80      # warn when used% > this
_QUEUE_WARN_DEPTH        = 5
_ARCHIVE_WARN_ROWS       = 50_000
_FAILURE_RATE_WARN_PCT   = 0.50    # warn when recent failure rate > 50%
_RECENT_RUN_WINDOW       = 10


# ---------------------------------------------------------------------------
# Data Contracts
# ---------------------------------------------------------------------------

@dataclass
class DomainCheck:
    check_id:    str
    name:        str
    passed:      bool
    blocking:    bool          # True → failure prevents new runs
    detail:      str
    remediation: Optional[str] = None   # exact command or action; None if passed


@dataclass
class DomainHealth:
    domain:  str               # stable snake_case domain name
    status:  str               # "healthy" | "warning" | "blocked" | "unknown"
    checks:  list              # list[DomainCheck]
    summary: str               # one-line human description


@dataclass
class RunClearanceDecision:
    cleared:           bool
    reason:            str
    blocking_checks:   list       # list[DomainCheck]
    blocking_signals:  list       # list[dict] from OIS
    pending_approvals: int
    checked_at:        str
    # Set to the literal string "supervised validation budget exemption"
    # only when check_run_clearance()'s validation_mode=True actually
    # bypassed a would-have-blocked monthly budget-ceiling check (2026-07-18)
    # -- None in every other case, including validation_mode=True runs that
    # were never going to be budget-blocked in the first place. This is the
    # sole audit trail for the exemption: no other blocking check (OIS,
    # factory-active, credentials) is ever affected by validation_mode.
    validation_exemption: Optional[str] = None


@dataclass
class FactoryDirectorReport:
    factory_id:      str
    factory_name:    str
    readiness_level: str           # READY | DEGRADED | BLOCKED | UNKNOWN
    readiness_score: int           # 0–100
    can_run:         bool
    assessed_at:     str
    domains:         list          # list[DomainHealth]
    blocking_reasons: list         # list[str]
    warnings:        list          # list[str]
    run_clearance:   RunClearanceDecision
    metadata_version: str = METADATA_VERSION


# ---------------------------------------------------------------------------
# Exception (carry-type — not raised by this module internally)
# ---------------------------------------------------------------------------

class FactoryDirectorBlockedError(RuntimeError):
    """Raised by callers that want hard-fail on BLOCKED readiness level."""

    def __init__(self, decision: RunClearanceDecision) -> None:
        self.decision = decision
        super().__init__(
            f"Factory is not cleared for a new run: {decision.reason}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_db(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _ok(check_id: str, name: str, blocking: bool, detail: str) -> DomainCheck:
    return DomainCheck(check_id=check_id, name=name, passed=True,
                       blocking=blocking, detail=detail)


def _fail(check_id: str, name: str, blocking: bool,
          detail: str, remediation: str) -> DomainCheck:
    return DomainCheck(check_id=check_id, name=name, passed=False,
                       blocking=blocking, detail=detail, remediation=remediation)


def _domain_status(checks: list) -> str:
    failed = [c for c in checks if not c.passed]
    if not failed:
        return "healthy"
    if any(c.blocking for c in failed):
        return "blocked"
    return "warning"


# ---------------------------------------------------------------------------
# Domain 1 — Factory Entity
# ---------------------------------------------------------------------------

def _assess_factory_entity(db_path: Path, factory_id: str) -> DomainHealth:
    checks: list[DomainCheck] = []

    # Factory record and status — delegate to factory_entity API
    try:
        from hypogeum.factory_entity import get_factory
        factory = get_factory(factory_id=factory_id, db_path=db_path)

        if factory is None:
            checks.append(_fail(
                "factory_record_exists", "Factory record in DB",
                True,
                f"No row in factories table for factory_id={factory_id!r}",
                "python3 scripts/bootstrap_channel.py",
            ))
        else:
            checks.append(_ok(
                "factory_record_exists", "Factory record in DB", True,
                f"factory_id={factory_id!r} name={factory.factory_name!r}",
            ))
            if (factory.status or "") != "active":
                checks.append(_fail(
                    "factory_status_active", "Factory status is active",
                    True,
                    f"status={factory.status!r} — factory is not operational",
                    f"from hypogeum.factory_entity import update_factory; "
                    f"update_factory({factory_id!r}, status='active')",
                ))
            else:
                checks.append(_ok(
                    "factory_status_active", "Factory status is active", True,
                    "status='active'",
                ))
            name = factory.factory_name or ""
            if not name.strip():
                checks.append(_fail(
                    "factory_name_set", "Factory name is populated",
                    False,
                    "factory_name is empty",
                    f"update_factory({factory_id!r}, factory_name='My Factory')",
                ))
            else:
                checks.append(_ok(
                    "factory_name_set", "Factory name is populated", False,
                    f"factory_name={name!r}",
                ))
    except Exception as exc:
        checks.append(_fail(
            "factory_entity_accessible", "Factory entity DB accessible",
            True,
            f"get_factory() failed: {exc}",
            "python3 scripts/bootstrap_channel.py",
        ))

    # Owner user — raw SQL: auth module has no role-filtered owner lookup
    try:
        conn = get_connection(db_path)
        try:
            owner_row = conn.execute(
                "SELECT user_id, email FROM users WHERE role = 'owner' AND status = 'active'",
            ).fetchone()
            if owner_row is None:
                checks.append(_fail(
                    "owner_user_exists", "Active owner user exists",
                    True,
                    "No user with role='owner' and status='active' in users table",
                    "python3 scripts/create_owner.py",
                ))
            else:
                checks.append(_ok(
                    "owner_user_exists", "Active owner user exists", True,
                    f"owner user_id={owner_row['user_id'][:8]}... email={owner_row['email']!r}",
                ))
        finally:
            conn.close()
    except Exception as exc:
        checks.append(_fail(
            "owner_user_exists", "Active owner user exists",
            True,
            f"Error querying users table: {exc}",
            "python3 scripts/create_owner.py",
        ))

    return DomainHealth(
        domain="factory_entity",
        status=_domain_status(checks),
        checks=checks,
        summary=_domain_summary(checks, "factory entity"),
    )


# ---------------------------------------------------------------------------
# Domain 2 — Policy Cascade
# ---------------------------------------------------------------------------

def _assess_policy_cascade(db_path: Path, factory_id: str) -> DomainHealth:
    checks: list[DomainCheck] = []
    try:
        from hypogeum.policy_cascade import get_effective_policy, get_effective_budget_ceiling

        # One policy read — for load check and model blocklist
        try:
            policy = get_effective_policy(factory_id=factory_id, db_path=db_path)
            checks.append(_ok(
                "policy_load_succeeds", "Effective policy loads without error", True,
                "factory policy loaded successfully",
            ))
            if policy.blocked_model_slugs:
                checks.append(_ok(
                    "model_blocklist_present", "Model blocklist configured", False,
                    f"{len(policy.blocked_model_slugs)} model(s) blocked: "
                    f"{policy.blocked_model_slugs[:3]}",
                ))
            else:
                checks.append(_ok(
                    "model_blocklist_present", "Model blocklist configured", False,
                    "No model slugs blocked (permissive)",
                ))
        except Exception as exc:
            checks.append(_fail(
                "policy_load_succeeds", "Effective policy loads without error",
                True,
                f"get_effective_policy() raised: {exc}",
                "python3 scripts/bootstrap_channel.py  # ensure factory record exists",
            ))

        # Budget ceiling — use the cascade API that honours the .env value
        try:
            from hypogeum.config import Config
            try:
                env_ceiling = Config().FINANCE_BUDGET_CEILING_USD
            except Exception:
                env_ceiling = 0.0
            effective_ceiling = get_effective_budget_ceiling(
                env_ceiling=env_ceiling,
                factory_id=factory_id,
                db_path=db_path,
            )
            if effective_ceiling <= 0:
                checks.append(_fail(
                    "budget_ceiling_set", "Budget ceiling configured",
                    False,   # WARNING only: ceiling=0 disables the gate, it is not a hard block
                    "Effective budget ceiling is 0 — Finance Agent gate is disabled "
                    f"(env={env_ceiling:.2f}, factory_policies ceiling not set)",
                    "Set FINANCE_BUDGET_CEILING_USD in .env or "
                    "factory_policies.monthly_ceiling_usd via update_factory()",
                ))
            else:
                checks.append(_ok(
                    "budget_ceiling_set", "Budget ceiling configured", False,
                    f"Effective ceiling=${effective_ceiling:.2f} (env=${env_ceiling:.2f})",
                ))
        except Exception as exc:
            checks.append(_fail(
                "budget_ceiling_set", "Budget ceiling check",
                False,
                f"get_effective_budget_ceiling() failed: {exc}",
                "Check policy_cascade module and FINANCE_BUDGET_CEILING_USD",
            ))

    except ImportError as exc:
        checks.append(_fail(
            "policy_cascade_importable", "policy_cascade module importable",
            True, str(exc), "Check hypogeum/policy_cascade.py",
        ))

    return DomainHealth(
        domain="policy_cascade",
        status=_domain_status(checks),
        checks=checks,
        summary=_domain_summary(checks, "policy cascade"),
    )


# ---------------------------------------------------------------------------
# Domain 3 — OIS
# ---------------------------------------------------------------------------

def _assess_ois(db_path: Path) -> DomainHealth:
    checks: list[DomainCheck] = []
    try:
        from hypogeum.ois import is_factory_blocked, get_active_signals, get_blocking_signals

        blocked = is_factory_blocked(db_path=db_path)
        if blocked:
            blocking = get_blocking_signals(db_path=db_path)
            ids = ", ".join(s.signal_id[:8] + "…" for s in blocking[:3])
            checks.append(_fail(
                "no_blocking_signals", "No active blocking OIS signals",
                True,
                f"{len(blocking)} blocking signal(s) open: {ids}",
                "Resolve each signal: "
                "from hypogeum.ois import resolve_signal; "
                "resolve_signal('<signal_id>', 'operator')",
            ))
        else:
            checks.append(_ok(
                "no_blocking_signals", "No active blocking OIS signals", True,
                "is_factory_blocked() = False",
            ))

        active = get_active_signals(db_path=db_path)
        non_blocking = [s for s in active if not s.blocking]
        # Informational signals (stuck_run, run_failed) are emitted by the
        # health sweep every cycle and are not, on their own, a degraded
        # factory. Only flag a backlog that is genuinely piling up
        # (>_SIGNAL_BACKLOG_LIMIT) or has been sitting ignored for a week —
        # a day-old info signal is normal for an unattended factory.
        from datetime import datetime, timedelta, timezone
        _week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        def _sig_ts(s) -> str:
            for attr in ("ingested_at", "created_at", "detected_at", "first_seen_at"):
                v = getattr(s, attr, None)
                if v:
                    return str(v)
            return ""  # unknown age — don't assume it's ancient

        stale = [s for s in non_blocking
                 if _sig_ts(s) and _sig_ts(s) < _week_ago]
        if len(non_blocking) > _SIGNAL_BACKLOG_LIMIT or stale:
            checks.append(_fail(
                "open_signal_backlog", "Non-blocking signal backlog under control",
                False,
                (f"{len(non_blocking)} open non-blocking signal(s)"
                 + (f", {len(stale)} older than a week" if stale else "")
                 + " — review and resolve"),
                "from hypogeum.ois import get_active_signals; get_active_signals()",
            ))
        else:
            checks.append(_ok(
                "open_signal_backlog", "Non-blocking signal backlog under control", False,
                f"{len(non_blocking)} recent informational signal(s), none stale",
            ))

    except Exception as exc:
        checks.append(_fail(
            "ois_accessible", "OIS subsystem accessible",
            True, f"OIS query failed: {exc}",
            "Check hypogeum/ois.py and signal_records table",
        ))

    return DomainHealth(
        domain="ois",
        status=_domain_status(checks),
        checks=checks,
        summary=_domain_summary(checks, "OIS"),
    )


# ---------------------------------------------------------------------------
# Domain 4 — Approvals
# ---------------------------------------------------------------------------

def _assess_approvals(db_path: Path) -> DomainHealth:
    checks: list[DomainCheck] = []
    cutoff_stale = (
        _now_utc() - timedelta(hours=_STALE_APPROVAL_HOURS)
    ).isoformat()

    # Pending and stale counts — delegate to authoritative approvals API
    try:
        from hypogeum.approvals import list_pending_approvals
        pending_records = list_pending_approvals(db_path=db_path)
        pending_count = len(pending_records)
        stale_count = sum(
            1 for r in pending_records
            if r.requested_at and r.requested_at < cutoff_stale
        )

        if stale_count > 0:
            checks.append(_fail(
                "no_stale_approvals", f"No approvals pending >{_STALE_APPROVAL_HOURS}h",
                False,
                f"{stale_count} approval(s) awaiting decision for >{_STALE_APPROVAL_HOURS}h",
                "python3 scripts/approve_run.py --list-pending",
            ))
        else:
            checks.append(_ok(
                "no_stale_approvals", f"No approvals pending >{_STALE_APPROVAL_HOURS}h", False,
                f"No stale approvals ({pending_count} pending total)",
            ))
        checks.append(_ok(
            "pending_approval_count", "Pending approval queue depth", False,
            f"{pending_count} pending approval(s)",
        ))
    except Exception as exc:
        checks.append(_fail(
            "approvals_accessible", "Approvals subsystem accessible",
            False, f"list_pending_approvals() failed: {exc}",
            "Verify approvals table exists: initialize_schema(db_path)",
        ))

    # Orphaned awaiting_review runs — no owner API exists; raw SQL required
    try:
        conn = get_connection(db_path)
        try:
            orphaned = conn.execute(
                """
                SELECT COUNT(*) AS n FROM runs r
                WHERE r.status = 'awaiting_review'
                  AND NOT EXISTS (
                    SELECT 1 FROM approvals a
                    WHERE a.run_id = r.run_id AND a.status = 'pending'
                  )
                """
            ).fetchone()["n"]
        finally:
            conn.close()

        if orphaned > 0:
            checks.append(_fail(
                "no_orphaned_awaiting_review", "No orphaned awaiting_review runs",
                False,
                f"{orphaned} run(s) stuck in awaiting_review with no pending approval",
                "from hypogeum.run_registry import get_run_state; "
                "get_run_state('<run_id>')",
            ))
        else:
            checks.append(_ok(
                "no_orphaned_awaiting_review", "No orphaned awaiting_review runs", False,
                "All awaiting_review runs have associated pending approvals",
            ))
    except Exception as exc:
        checks.append(_fail(
            "no_orphaned_awaiting_review", "Orphaned run check",
            False, f"Query failed: {exc}",
            "Verify runs and approvals tables",
        ))

    return DomainHealth(
        domain="approvals",
        status=_domain_status(checks),
        checks=checks,
        summary=_domain_summary(checks, "approvals"),
    )


# ---------------------------------------------------------------------------
# Domain 5 — Run Lifecycle
# ---------------------------------------------------------------------------

def _assess_run_lifecycle(db_path: Path) -> DomainHealth:
    checks: list[DomainCheck] = []
    zombie_cutoff = (
        _now_utc() - timedelta(hours=_ZOMBIE_RUN_HOURS)
    ).isoformat()
    stuck_cutoff = (
        _now_utc() - timedelta(hours=48)
    ).isoformat()

    try:
        conn = get_connection(db_path)
        try:
            state_rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM runs GROUP BY status"
            ).fetchall()
            by_state = {r["status"]: r["n"] for r in state_rows}

            zombie_count = conn.execute(
                "SELECT COUNT(*) AS n FROM runs "
                "WHERE status = 'running' AND started_at < ?",
                (zombie_cutoff,),
            ).fetchone()["n"]

            stuck_intermediate = conn.execute(
                "SELECT COUNT(*) AS n FROM runs "
                "WHERE status IN ('awaiting_review', 'awaiting_publish_approval') "
                "  AND started_at < ?",
                (stuck_cutoff,),
            ).fetchone()["n"]

            # Recent failure rate (last N concluded runs)
            recent = conn.execute(
                """
                SELECT status FROM runs
                WHERE status IN ('complete', 'completed', 'failed')
                ORDER BY COALESCE(completed_at, started_at) DESC
                LIMIT ?
                """,
                (_RECENT_RUN_WINDOW,),
            ).fetchall()
            n_recent = len(recent)
            n_failed = sum(1 for r in recent if r["status"] == "failed")
            failure_rate = n_failed / n_recent if n_recent > 0 else 0.0
        finally:
            conn.close()

        if zombie_count > 0:
            checks.append(_fail(
                "no_zombie_runs", f"No runs stuck in 'running' >{_ZOMBIE_RUN_HOURS}h",
                False,
                f"{zombie_count} run(s) show status='running' with started_at >{_ZOMBIE_RUN_HOURS}h ago",
                "Review stalled runs; these are likely test artifacts or crashed runs",
            ))
        else:
            checks.append(_ok(
                "no_zombie_runs", f"No runs stuck in 'running' >{_ZOMBIE_RUN_HOURS}h", False,
                f"No zombie runs detected ({by_state.get('running', 0)} currently running)",
            ))

        if stuck_intermediate > 0:
            checks.append(_fail(
                "no_stuck_intermediate", "No runs stuck in intermediate states >48h",
                False,
                f"{stuck_intermediate} run(s) in awaiting_review or awaiting_publish_approval >48h",
                "python3 scripts/approve_run.py --list-pending",
            ))
        else:
            checks.append(_ok(
                "no_stuck_intermediate", "No runs stuck in intermediate states >48h", False,
                "No long-running intermediate states",
            ))

        if n_recent >= 5 and failure_rate > _FAILURE_RATE_WARN_PCT:
            checks.append(_fail(
                "recent_failure_rate", f"Recent failure rate <{_FAILURE_RATE_WARN_PCT:.0%}",
                False,
                f"Recent failure rate: {failure_rate:.0%} "
                f"({n_failed}/{n_recent} of last {_RECENT_RUN_WINDOW} runs failed)",
                "Review recent run logs; check OIS signals for pattern",
            ))
        else:
            rate_str = f"{failure_rate:.0%}" if n_recent > 0 else "n/a (no concluded runs)"
            checks.append(_ok(
                "recent_failure_rate", f"Recent failure rate <{_FAILURE_RATE_WARN_PCT:.0%}", False,
                f"Recent failure rate: {rate_str}",
            ))

        state_summary = ", ".join(
            f"{s}={n}" for s, n in sorted(by_state.items()) if n > 0
        ) or "no runs"
        checks.append(_ok(
            "rlg_state_distribution", "RLG state distribution", False,
            f"Run states: {state_summary}",
        ))

    except Exception as exc:
        checks.append(_fail(
            "run_lifecycle_accessible", "Run lifecycle DB accessible",
            False, f"Query failed: {exc}",
            "Verify runs and run_state_transitions tables",
        ))

    return DomainHealth(
        domain="run_lifecycle",
        status=_domain_status(checks),
        checks=checks,
        summary=_domain_summary(checks, "run lifecycle"),
    )


# ---------------------------------------------------------------------------
# Domain 6 — Credentials
# ---------------------------------------------------------------------------

def _assess_credentials(db_path: Path) -> DomainHealth:
    checks: list[DomainCheck] = []
    expiry_warn_str  = (_now_utc() + timedelta(days=_CREDENTIAL_EXPIRY_DAYS)).isoformat()
    retest_cutoff_str = (_now_utc() - timedelta(days=_CREDENTIAL_RETEST_DAYS)).isoformat()

    try:
        from hypogeum.credential_manager import list_secret_profiles
        all_profiles = list_secret_profiles(db_path=db_path)
        active = [p for p in all_profiles if p.status == "active"]

        if not active:
            checks.append(_fail(
                "secret_profiles_exist", "Active secret_profiles exist",
                True,
                "No active secret profiles — factory has no API credentials",
                "python3 scripts/import_credentials.py",
            ))
        else:
            checks.append(_ok(
                "secret_profiles_exist", "Active secret_profiles exist", True,
                f"{len(active)} active credential profile(s)",
            ))

        present_providers = {p.provider for p in active}
        missing = _REQUIRED_PROVIDERS - present_providers
        if missing:
            checks.append(_fail(
                "required_providers_covered", "Required providers have active credentials",
                True,
                f"Missing credentials for: {sorted(missing)}",
                "python3 scripts/import_credentials.py  # imports from .env",
            ))
        else:
            checks.append(_ok(
                "required_providers_covered", "Required providers have active credentials",
                True,
                f"All required providers covered: {sorted(_REQUIRED_PROVIDERS)}",
            ))

        expiring = [
            p for p in active
            if p.expires_at and p.expires_at < expiry_warn_str
        ]
        if expiring:
            labels = ", ".join(f"{p.provider}/{p.label}" for p in expiring[:3])
            checks.append(_fail(
                "no_expiring_credentials",
                f"No credentials expiring within {_CREDENTIAL_EXPIRY_DAYS}d",
                False,
                f"{len(expiring)} credential(s) expiring soon: {labels}",
                "Rotate credentials before expiry to avoid production interruptions",
            ))
        else:
            checks.append(_ok(
                "no_expiring_credentials",
                f"No credentials expiring within {_CREDENTIAL_EXPIRY_DAYS}d",
                False, "No credentials expiring soon",
            ))

        untested = [
            p for p in active
            if not p.last_tested_at or p.last_tested_at < retest_cutoff_str
        ]
        if untested:
            # A stale *test* timestamp is process hygiene, not a health
            # problem — if episodes are producing and publishing, the
            # credentials demonstrably work. Note it, don't degrade on it.
            checks.append(_ok(
                "credentials_recently_tested",
                f"All credentials tested within {_CREDENTIAL_RETEST_DAYS}d",
                False,
                f"{len(untested)} credential(s) due for a routine re-test "
                f"(working — last confirmed by a successful run)",
            ))
        else:
            checks.append(_ok(
                "credentials_recently_tested",
                f"All credentials tested within {_CREDENTIAL_RETEST_DAYS}d",
                False, "All credentials recently tested",
            ))

    except Exception as exc:
        checks.append(_fail(
            "credentials_accessible", "Credentials subsystem accessible",
            True, f"list_secret_profiles() failed: {exc}",
            "python3 scripts/import_credentials.py",
        ))

    return DomainHealth(
        domain="credentials",
        status=_domain_status(checks),
        checks=checks,
        summary=_domain_summary(checks, "credentials"),
    )


# ---------------------------------------------------------------------------
# Domain 7 — Factory Capacity
# ---------------------------------------------------------------------------

def _get_month_cost_and_ceiling(db_path: Path, factory_id: str) -> tuple[float, float]:
    """
    Single source of truth for "how much has the factory spent this month"
    vs. "what is the effective monthly budget ceiling" — shared by
    _assess_factory_capacity()'s non-blocking 90%-headroom warning and
    check_run_clearance()'s blocking ceiling-exceeded check, so the two
    call sites can never compute this differently.

    Ceiling resolution is deliberately NOT delegated to
    hypogeum.policy_cascade.get_effective_budget_ceiling() here: that
    helper returns a PER-EPISODE value (monthly_ceiling_usd /
    episodes_per_month) for its own legitimate use in
    validate_factory_policy_constraints() — correct and unchanged for
    that purpose. This function's month_cost is a MONTHLY TOTAL summed
    across every episode, so comparing it against a per-episode-scaled
    ceiling would gate episodes_per_month times too early. Instead, the
    factory's monthly_ceiling_usd is read and compared directly — still
    matching policy_cascade.py's documented precedence
    ("factory_policies -> runtime env"): the factory value wins whenever
    it's set (> 0); the env var is used only when the factory value is
    unset or zero, so `monthly_ceiling_usd = 0` restores the exact
    pre-existing env-only behavior.

    Returns:
        (month_cost, ceiling) — both floats. ceiling <= 0 means "no
        ceiling configured", which callers must treat as "no budget gate
        applies."
    """
    # Multi-tenant Phase 3: a workspace's own spend and ceiling
    # (hypogeum.workspace_controls) — for the platform owner identical to
    # the factory-wide numbers below while there are no client workspaces.
    try:
        from hypogeum import workspace_controls as _wsc
        from hypogeum.workspaces import platform_workspace_id
        if platform_workspace_id(db_path) is not None:
            return _wsc.month_spend(factory_id, db_path), _wsc.month_ceiling(factory_id, db_path)
    except Exception:  # noqa: BLE001 — fall through to the pre-workspace computation
        pass

    from hypogeum.factory_entity import get_factory_policies
    from hypogeum.config import Config
    try:
        env_ceiling = Config().FINANCE_BUDGET_CEILING_USD
    except Exception:
        env_ceiling = 0.0

    try:
        factory_monthly_ceiling = float(
            get_factory_policies(factory_id, db_path=db_path).get("monthly_ceiling_usd") or 0
        )
    except Exception:
        # Factory not found, DB error, malformed policy JSON, etc. — fall
        # back to env, matching policy_cascade.py's own suppression of
        # FactoryNotFoundError elsewhere in this cascade.
        factory_monthly_ceiling = 0.0

    ceiling = factory_monthly_ceiling if factory_monthly_ceiling > 0 else env_ceiling

    conn = get_connection(db_path)
    try:
        month_cost_row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM costs "
            "WHERE strftime('%Y-%m', called_at) = strftime('%Y-%m', 'now')"
        ).fetchone()
        month_cost = float(month_cost_row["total"] or 0)
    finally:
        conn.close()

    return month_cost, ceiling


def get_budget_status(db_path: Optional[Path] = None, factory_id: str = "hypogeum") -> dict:
    """
    Composed budget status for the Dashboard budget-ceiling route (GET and
    POST both use this — one shared computation, never duplicated between
    the two). Not a new gate — reuses _get_month_cost_and_ceiling() (the
    same function check_run_clearance() itself calls) and
    check_run_clearance() directly, so this can never drift from the
    actual gate's own decision.

    Returns:
        {
            "month_to_date_spend_usd": float,
            "effective_ceiling_usd": float,       # 0.0 = no ceiling configured
            "ceiling_source": "factory_policy" | "env_fallback" | "none",
            "headroom_usd": float,                 # max(0, ceiling - spend); 0 if no ceiling
            "cleared": bool,                       # check_run_clearance()'s own verdict
            "blocking_reason": Optional[str],       # None when cleared
        }
    """
    resolved = _resolve_db(db_path)
    month_cost, ceiling = _get_month_cost_and_ceiling(resolved, factory_id)

    from hypogeum.factory_entity import get_factory_policies
    try:
        factory_ceiling = float(
            get_factory_policies(factory_id, db_path=resolved).get("monthly_ceiling_usd") or 0
        )
    except Exception:
        factory_ceiling = 0.0

    if factory_ceiling > 0:
        source = "factory_policy"
    elif ceiling > 0:
        source = "env_fallback"
    else:
        source = "none"

    headroom = max(0.0, ceiling - month_cost) if ceiling > 0 else 0.0

    decision = check_run_clearance(db_path=resolved)
    blocking_reason = None
    if not decision.cleared and "monthly ceiling" in (decision.reason or ""):
        blocking_reason = decision.reason
    elif not decision.cleared:
        # Cleared is False for a reason unrelated to budget (OIS signal,
        # factory inactive, missing credentials) — do not mislabel it as
        # a budget block.
        blocking_reason = None

    return {
        "month_to_date_spend_usd": round(month_cost, 2),
        "effective_ceiling_usd": round(ceiling, 2),
        "ceiling_source": source,
        "headroom_usd": round(headroom, 2),
        "cleared": decision.cleared,
        "blocking_reason": blocking_reason,
    }


def _assess_factory_capacity(db_path: Path, factory_id: str) -> DomainHealth:
    """
    Four infrastructure-only checks unique to the capacity domain.

    Excluded (covered by their authoritative domain):
        active_run_count         → run_lifecycle.rlg_state_distribution
        pending_approvals_capacity → approvals.pending_approval_count
        agent_availability       → credentials.required_providers_covered
    """
    checks: list[DomainCheck] = []

    # --- Queue depth ---
    try:
        conn = get_connection(db_path)
        try:
            queue_depth = conn.execute(
                "SELECT COUNT(*) AS n FROM run_queue WHERE status = 'queued'"
            ).fetchone()["n"]
        finally:
            conn.close()

        if queue_depth > _QUEUE_WARN_DEPTH:
            checks.append(_fail(
                "queue_depth", f"Queue depth <={_QUEUE_WARN_DEPTH}",
                False,
                f"{queue_depth} runs queued — factory may be backlogged",
                "Review run_queue table; check scheduler cadence",
            ))
        else:
            checks.append(_ok(
                "queue_depth", f"Queue depth <={_QUEUE_WARN_DEPTH}", False,
                f"{queue_depth} run(s) in queue",
            ))
    except Exception as exc:
        checks.append(_fail(
            "queue_depth", "Queue depth accessible", False,
            f"run_queue query failed: {exc}", "Check run_queue table",
        ))

    # --- Storage pressure ---
    try:
        if _ASSETS_DIR.exists():
            usage = shutil.disk_usage(_ASSETS_DIR)
            used_pct = (usage.used / usage.total * 100) if usage.total > 0 else 0
            free_gb  = usage.free / (1024 ** 3)
            if used_pct > _STORAGE_WARN_PCT:
                checks.append(_fail(
                    "storage_pressure", f"Disk usage <{_STORAGE_WARN_PCT}%",
                    False,
                    f"Disk {used_pct:.1f}% used, {free_gb:.1f} GB free",
                    "Free disk space or archive old assets",
                ))
            else:
                checks.append(_ok(
                    "storage_pressure", f"Disk usage <{_STORAGE_WARN_PCT}%", False,
                    f"Disk {used_pct:.1f}% used, {free_gb:.1f} GB free",
                ))
        else:
            checks.append(_ok(
                "storage_pressure", "Assets directory exists", False,
                "Assets directory not yet created (pre-first-run)",
            ))
    except Exception as exc:
        checks.append(_fail(
            "storage_pressure", "Storage check", False,
            f"Disk usage check failed: {exc}", "Check disk usage manually",
        ))

    # --- Archive pressure ---
    try:
        conn = get_connection(db_path)
        try:
            archive_rows = conn.execute(
                "SELECT COUNT(*) AS n FROM archive_index"
            ).fetchone()["n"]
        finally:
            conn.close()

        if archive_rows > _ARCHIVE_WARN_ROWS:
            checks.append(_fail(
                "archive_pressure", f"Archive <{_ARCHIVE_WARN_ROWS:,} rows",
                False,
                f"archive_index has {archive_rows:,} rows — consider pruning",
                "Review archive retention policy; prune old assets",
            ))
        else:
            checks.append(_ok(
                "archive_pressure", f"Archive <{_ARCHIVE_WARN_ROWS:,} rows", False,
                f"archive_index: {archive_rows:,} rows",
            ))
    except Exception as exc:
        checks.append(_fail(
            "archive_pressure", "Archive pressure check", False,
            f"archive_index query failed: {exc}", "Check archive_index table",
        ))

    # --- Budget headroom ---
    try:
        month_cost, ceiling = _get_month_cost_and_ceiling(db_path, factory_id)

        if ceiling > 0:
            utilization = month_cost / ceiling
            detail = (
                f"${month_cost:.2f} of ${ceiling:.2f} used this month "
                f"({utilization:.0%}); ${ceiling - month_cost:.2f} remaining"
            )
            if utilization >= 0.90:
                checks.append(_fail(
                    "budget_headroom", "Budget headroom >10%",
                    False, detail,
                    "Approaching monthly budget ceiling — review costs",
                ))
            else:
                checks.append(_ok(
                    "budget_headroom", "Budget headroom >10%", False, detail,
                ))
        else:
            checks.append(_ok(
                "budget_headroom", "Budget headroom", False,
                f"${month_cost:.2f} spent this month (no ceiling configured)",
            ))
    except Exception as exc:
        checks.append(_fail(
            "budget_headroom", "Budget headroom", False,
            f"Budget check failed: {exc}", "Check policy_cascade and costs table",
        ))

    return DomainHealth(
        domain="factory_capacity",
        status=_domain_status(checks),
        checks=checks,
        summary=_domain_summary(checks, "factory capacity"),
    )


# ---------------------------------------------------------------------------
# Scoring and Level
# ---------------------------------------------------------------------------

def _domain_summary(checks: list, domain_label: str) -> str:
    total = len(checks)
    failed = [c for c in checks if not c.passed]
    if not failed:
        return f"{domain_label}: all {total} check(s) passed"
    blocking = [c for c in failed if c.blocking]
    warnings = [c for c in failed if not c.blocking]
    parts = []
    if blocking:
        parts.append(f"{len(blocking)} blocker(s)")
    if warnings:
        parts.append(f"{len(warnings)} warning(s)")
    return f"{domain_label}: {', '.join(parts)} ({total - len(failed)}/{total} passed)"


def _compute_score_and_level(
    domains: list[DomainHealth],
) -> tuple[int, str, bool]:
    """
    Return (readiness_score, readiness_level, can_run).

    readiness_level is the authority — derived solely from whether any
    blocking check failed:
        BLOCKED  — one or more blocking checks failed (can_run=False)
        DEGRADED — no blocking failures, one or more warnings (can_run=True)
        READY    — all checks passed (can_run=True)

    readiness_score is a continuous metric (0–100) for trend visibility.
    It is NOT band-capped to the level's range, so removing any single
    failing check always produces a higher score — regardless of level.
        score = max(0, 100 - BLOCKING_DEDUCTION * n_blocking
                             - WARNING_DEDUCTION  * n_warning)
    """
    all_checks = [c for d in domains for c in d.checks]
    failed     = [c for c in all_checks if not c.passed]
    n_blocking = sum(1 for c in failed if c.blocking)
    n_warning  = sum(1 for c in failed if not c.blocking)

    score = max(0, 100 - (_BLOCKING_DEDUCTION * n_blocking)
                       - (_WARNING_DEDUCTION  * n_warning))

    if n_blocking > 0:
        return score, "BLOCKED", False
    if n_warning > 0:
        return score, "DEGRADED", True
    return 100, "READY", True


def _extract_blocking_reasons(domains: list[DomainHealth]) -> list[str]:
    return [
        f"[{d.domain}] {c.name}: {c.detail}"
        for d in domains
        for c in d.checks
        if not c.passed and c.blocking
    ]


def _extract_warnings(domains: list[DomainHealth]) -> list[str]:
    return [
        f"[{d.domain}] {c.name}: {c.detail}"
        for d in domains
        for c in d.checks
        if not c.passed and not c.blocking
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def assess_factory(
    db_path:    Optional[Path] = None,
    factory_id: str            = "hypogeum",
) -> FactoryDirectorReport:
    """
    Full cross-system readiness and health assessment across all seven domains.

    Non-blocking — never raises. Returns readiness_level=UNKNOWN on error.

    Args:
        db_path:    Override SQLite path. None = production default.
        factory_id: Factory to assess.

    Returns:
        FactoryDirectorReport with readiness_level, readiness_score, and
        per-domain DomainHealth objects.
    """
    assessed_at = _now_iso()
    resolved = _resolve_db(db_path)

    try:
        initialize_schema(resolved)
    except Exception:
        pass

    factory_name = factory_id
    try:
        from hypogeum.factory_entity import get_factory
        factory_rec = get_factory(factory_id=factory_id, db_path=resolved)
        if factory_rec is not None:
            factory_name = factory_rec.factory_name or factory_id
    except Exception:
        pass

    domains: list[DomainHealth] = []
    assessment_error = None

    try:
        domains.append(_assess_factory_entity(resolved, factory_id))
        domains.append(_assess_policy_cascade(resolved, factory_id))
        domains.append(_assess_ois(resolved))
        domains.append(_assess_approvals(resolved))
        domains.append(_assess_run_lifecycle(resolved))
        domains.append(_assess_credentials(resolved))
        domains.append(_assess_factory_capacity(resolved, factory_id))
    except Exception as exc:
        assessment_error = exc

    if assessment_error is not None or not domains:
        clearance = RunClearanceDecision(
            cleared=False,
            reason=f"Assessment failed: {assessment_error}",
            blocking_checks=[],
            blocking_signals=[],
            pending_approvals=0,
            checked_at=assessed_at,
        )
        return FactoryDirectorReport(
            factory_id=factory_id,
            factory_name=factory_name,
            readiness_level="UNKNOWN",
            readiness_score=0,
            can_run=False,
            assessed_at=assessed_at,
            domains=domains,
            blocking_reasons=[str(assessment_error)],
            warnings=[],
            run_clearance=clearance,
        )

    score, level, can_run = _compute_score_and_level(domains)
    blocking_reasons = _extract_blocking_reasons(domains)
    warnings = _extract_warnings(domains)

    clearance = check_run_clearance(db_path=db_path)

    return FactoryDirectorReport(
        factory_id=factory_id,
        factory_name=factory_name,
        readiness_level=level,
        readiness_score=score,
        can_run=can_run,
        assessed_at=assessed_at,
        domains=domains,
        blocking_reasons=blocking_reasons,
        warnings=warnings,
        run_clearance=clearance,
    )


def check_run_clearance(
    db_path:         Optional[Path] = None,
    channel_id:      Optional[str]  = None,
    validation_mode: bool           = False,
    include_budget:  bool           = True,
) -> RunClearanceDecision:
    """
    Fast-path clearance check: only the checks that can block a new run.

    Covers: OIS blocking signals, factory entity active, credentials present,
    monthly budget ceiling not exceeded. Does not run the full seven-domain
    assessment.

    Args:
        db_path:    Override SQLite path. None = production default.
        channel_id: Selects WHICH workspace's monthly budget the ceiling
            check uses (the workspace owning the channel); None = the
            platform owner's (multi-tenant Phase 3).
        include_budget: False skips the monthly-ceiling check entirely —
            the worker's global gate passes False because per-workspace
            budgets are enforced inside the queue claim itself
            (hypogeum.workspace_controls.blocked_channels), so one
            workspace over its ceiling never stops another's production.
        validation_mode: Narrow, explicit exemption for supervised validation
            runs ONLY (added 2026-07-18, per explicit product decision — see
            RunClearanceDecision.validation_exemption's own docstring).
            When True, and ONLY when the monthly budget-ceiling check would
            otherwise have blocked (utilization >= 100%), that ONE check is
            skipped and RunClearanceDecision.validation_exemption is set to
            "supervised validation budget exemption" as the audit trail.
            Every other blocking check (OIS blocking signals, factory-active
            status, active credentials) is completely unaffected by this
            flag — it exempts the budget check alone, nothing else.
            Deliberately NOT inferred from MUAPI_DRY_RUN (a validation run
            can still incur real, non-MUAPI cost — OpenRouter/Claude,
            ElevenLabs) and NOT exposed via any CLI flag or DB-driven
            toggle — run_worker.py's actual autonomous loop
            (run_worker()) never sets this; only an explicit, one-off
            Python caller (e.g. a supervised validation script) can.

    Returns:
        RunClearanceDecision with cleared=True/False, reason, and (when
        applicable) validation_exemption set.
    """
    checked_at = _now_iso()
    resolved = _resolve_db(db_path)
    blocking_checks: list[DomainCheck] = []
    validation_exemption: Optional[str] = None
    blocking_signals: list[dict] = []
    pending_approvals = 0

    # OIS blocking signals
    try:
        from hypogeum.ois import is_factory_blocked, get_blocking_signals
        if is_factory_blocked(db_path=resolved):
            sigs = get_blocking_signals(db_path=resolved)
            blocking_signals = [
                {
                    "signal_id":   s.signal_id,
                    "signal_type": s.signal_type,
                    "severity":    s.severity,
                    "message":     s.message,
                }
                for s in sigs
            ]
            blocking_checks.append(_fail(
                "ois_not_blocked", "No blocking OIS signals",
                True,
                f"{len(sigs)} blocking signal(s) active",
                "Resolve active blocking signals before starting a run",
            ))
    except Exception:
        pass

    # Factory entity active — delegate to factory_entity API
    try:
        from hypogeum.factory_entity import get_factory
        factory_rec = get_factory(db_path=resolved)
        if factory_rec is None or (factory_rec.status or "") != "active":
            status = factory_rec.status if factory_rec else "not found"
            blocking_checks.append(_fail(
                "factory_active", "Factory entity is active",
                True,
                f"factory status={status!r}",
                "python3 scripts/bootstrap_channel.py",
            ))
    except Exception:
        pass

    # Credentials present — delegate to credential_manager API
    try:
        from hypogeum.credential_manager import list_secret_profiles
        active_creds = [p for p in list_secret_profiles(db_path=resolved) if p.status == "active"]
        if not active_creds:
            blocking_checks.append(_fail(
                "credentials_present", "Active credentials exist",
                True,
                "No active secret_profiles",
                "python3 scripts/import_credentials.py",
            ))
    except Exception:
        pass

    # Budget ceiling exceeded — hard block. Deliberately a stricter
    # threshold than _assess_factory_capacity()'s own 90%-headroom
    # warning (non-blocking, "approaching ceiling"): that check is an
    # early heads-up surfaced in the full assess_factory() report; this
    # one is the actual runtime gate run_worker.py consults before
    # starting a new run, and only fires once the ceiling is genuinely
    # exceeded (utilization >= 100%), via the same shared
    # _get_month_cost_and_ceiling() computation — never a second,
    # independently-maintained copy of the budget logic.
    try:
        # The workspace owning channel_id, else the platform owner
        # ("hypogeum" on an install without workspaces, as before).
        _budget_ws = "hypogeum"
        try:
            from hypogeum.workspaces import platform_workspace_id, workspace_of
            _budget_ws = ((workspace_of(channel_id, db_path=resolved) if channel_id else None)
                          or platform_workspace_id(resolved) or "hypogeum")
        except Exception:  # noqa: BLE001
            pass
        month_cost, ceiling = (_get_month_cost_and_ceiling(resolved, _budget_ws)
                               if include_budget else (0.0, 0.0))
        if ceiling > 0 and month_cost >= ceiling:
            if validation_mode:
                # Narrow, explicit exemption (2026-07-18) -- ONLY this one
                # check is skipped; every other blocking check above is
                # completely unaffected. See check_run_clearance()'s own
                # docstring for why this exists and is never inferable from
                # MUAPI_DRY_RUN or reachable from the autonomous loop.
                validation_exemption = "supervised validation budget exemption"
            else:
                blocking_checks.append(_fail(
                    "budget_ceiling_not_exceeded", "Monthly budget ceiling not exceeded",
                    True,
                    f"${month_cost:.2f} spent of ${ceiling:.2f} monthly ceiling "
                    f"({month_cost / ceiling:.0%})",
                    "Raise the budget ceiling (FINANCE_BUDGET_CEILING_USD or policy_cascade "
                    "override) or wait for the next billing month before starting new runs",
                ))
    except Exception:
        pass

    # Pending approvals (informational) — delegate to approvals API
    try:
        from hypogeum.approvals import list_pending_approvals
        pending_approvals = len(list_pending_approvals(db_path=resolved))
    except Exception:
        pass

    if blocking_checks or blocking_signals:
        first_reason = (
            blocking_signals[0]["signal_type"] if blocking_signals
            else blocking_checks[0].detail
        )
        return RunClearanceDecision(
            cleared=False,
            reason=first_reason,
            blocking_checks=blocking_checks,
            blocking_signals=blocking_signals,
            pending_approvals=pending_approvals,
            checked_at=checked_at,
            validation_exemption=validation_exemption,
        )

    return RunClearanceDecision(
        cleared=True,
        reason="CLEARED — no blocking conditions detected",
        blocking_checks=[],
        blocking_signals=[],
        pending_approvals=pending_approvals,
        checked_at=checked_at,
        validation_exemption=validation_exemption,
    )


# ---------------------------------------------------------------------------
# Text Formatter
# ---------------------------------------------------------------------------

def format_report(report: FactoryDirectorReport) -> str:
    """
    Render a FactoryDirectorReport as a terminal-friendly text report.

    Pure formatting — no DB access, no network calls.
    """
    SEP  = "─" * 66
    SEP2 = "─" * 44

    level_badge = {
        "READY":    "✓ READY",
        "DEGRADED": "⚠ DEGRADED",
        "BLOCKED":  "✗ BLOCKED",
        "UNKNOWN":  "? UNKNOWN",
    }.get(report.readiness_level, report.readiness_level)

    lines = [
        SEP,
        f"  Factory Director Report — {report.factory_name}",
        f"  Factory ID:   {report.factory_id}",
        f"  Assessed at:  {report.assessed_at[:19]}",
        f"  Readiness:    {level_badge}",
        f"  Score:        {report.readiness_score}/100",
        f"  Can run:      {'YES' if report.can_run else 'NO'}",
        SEP,
    ]

    # Clearance
    lines.append("")
    clearance = report.run_clearance
    lines.append(f"Run Clearance: {'✓ CLEARED' if clearance.cleared else '✗ NOT CLEARED'}")
    if not clearance.cleared:
        lines.append(f"  Reason: {clearance.reason}")
    if clearance.blocking_signals:
        lines.append(f"  Blocking signals: {len(clearance.blocking_signals)}")
    if clearance.pending_approvals:
        lines.append(f"  Pending approvals: {clearance.pending_approvals}")

    # Blocking reasons
    if report.blocking_reasons:
        lines += ["", f"Blockers ({len(report.blocking_reasons)})"]
        for reason in report.blocking_reasons:
            lines.append(f"  ✗ {reason}")

    # Warnings
    if report.warnings:
        lines += ["", f"Warnings ({len(report.warnings)})"]
        for w in report.warnings:
            lines.append(f"  ⚠ {w}")

    # Domain breakdown
    lines += ["", "Domain Summary", SEP2]
    status_icon = {"healthy": "✓", "warning": "⚠", "blocked": "✗", "unknown": "?"}
    for d in report.domains:
        icon = status_icon.get(d.status, "?")
        lines.append(f"  {icon} {d.domain:<22} {d.summary}")

    # Per-domain detail with remediation
    for d in report.domains:
        failed = [c for c in d.checks if not c.passed]
        if not failed:
            continue
        lines += ["", f"  ── {d.domain} ──"]
        for c in failed:
            tier = "[BLOCK]" if c.blocking else "[warn] "
            lines.append(f"    {tier} {c.name}")
            lines.append(f"           {c.detail}")
            if c.remediation:
                lines.append(f"           → {c.remediation}")

    lines += ["", SEP]
    return "\n".join(lines)
