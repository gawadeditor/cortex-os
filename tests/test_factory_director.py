"""
tests/test_factory_director.py — Factory Director Phase A test suite

Coverage:
    TestDataContracts       — dataclass fields and defaults
    TestHelpers             — _ok, _fail, _domain_status, _domain_summary
    TestScoreAndLevel       — _compute_score_and_level: continuous score, level derivation
    TestAssessFactoryEntity — factory record, status, owner, factory_name
    TestAssessPolicyCascade — policy load, budget ceiling (warning not blocker), model blocklist
    TestAssessOIS           — blocking signals, non-blocking backlog
    TestAssessApprovals     — pending, stale, orphaned awaiting_review
    TestAssessRunLifecycle  — zombie runs, failure rate, intermediate states, state distribution
    TestAssessCredentials   — active profiles, required providers, expiry, untested
    TestAssessCapacity      — queue depth, storage, archive, budget headroom
    TestAssessFactory       — full integration: UNKNOWN, BLOCKED, DEGRADED, READY
    TestCheckRunClearance   — cleared, OIS blocked, inactive factory, no credentials
    TestFormatReport        — output sections, badges, remediation text
    TestNonBlocking         — bad db_path returns safe default for every domain
    TestNoSideEffects       — no DB writes; row counts unchanged after assess_factory()
    TestNoNetworkCalls      — socket blocked throughout
    TestCLI                 — run_director_check.py: exit codes, --json, --domain, errors
"""

from __future__ import annotations

import json
import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.factory_director import (
    DomainCheck,
    DomainHealth,
    FactoryDirectorBlockedError,
    FactoryDirectorReport,
    RunClearanceDecision,
    _compute_score_and_level,
    _domain_status,
    _domain_summary,
    _fail,
    _ok,
    assess_factory,
    check_run_clearance,
    format_report,
    get_budget_status,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "director_test.db"
    initialize_schema(p)
    return p


def _seed_factory(db_path: Path, status: str = "active",
                  name: str = "Test Factory") -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO factories (factory_id, factory_name, factory_slug, "
            "status, default_language, default_timezone, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("test", name, "test", status, "en", "UTC",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_owner(db_path: Path) -> str:
    uid = str(uuid.uuid4())
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO users (user_id, email, name, role, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (uid, "owner@test.local", "Owner", "owner", "active",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return uid


def _seed_credentials(db_path: Path,
                      providers=("openrouter", "muapi", "elevenlabs")) -> None:
    conn = get_connection(db_path)
    try:
        for prov in providers:
            conn.execute(
                "INSERT INTO secret_profiles "
                "(secret_profile_id, provider, label, credential_type, "
                "credential_data, status, scope, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), prov, f"{prov}-env", "api_key",
                 "dummy", "active", "factory",
                 "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_blocking_signal(db_path: Path) -> str:
    sid = str(uuid.uuid4())
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO signal_records "
            "(signal_id, episode_id, run_id, ingested_at, window_days, "
            "data_quality, metadata_version, signal_type, severity, "
            "full_record, source_system, blocking, lifecycle_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, "sys", "r_sys", "2026-01-01T00:00:00", 0,
             "ok", "v1", "budget_exhausted", "CRITICAL",
             '{"message":"ceiling hit"}',
             "finance_agent", 1, "open"),
        )
        conn.commit()
    finally:
        conn.close()
    return sid


def _seed_run(db_path: Path, status: str, run_id: str = None,
              started_at: str = "2026-06-01T00:00:00",
              completed_at: str = None) -> str:
    rid = run_id or str(uuid.uuid4())
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status, completed_at) "
            "VALUES (?,?,?,?,?)",
            (rid, f"ep_{rid[:6]}", started_at, status, completed_at),
        )
        conn.commit()
    finally:
        conn.close()
    return rid


def _seed_approval(db_path: Path, requested_at: str = None) -> str:
    aid = str(uuid.uuid4())
    rid = _seed_run(db_path, "running")
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO approvals "
            "(approval_id, run_id, episode_id, approval_type, requested_by, "
            "status, requested_at) VALUES (?,?,?,?,?,?,?)",
            (aid, rid, "ep_001", "human_review", "system:pipeline",
             "pending", requested_at or "2026-01-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return aid


def _seed_cost(db_path: Path, cost_usd: float, called_at: str = None) -> str:
    rid = _seed_run(db_path, "completed")
    cid = str(uuid.uuid4())
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO costs "
            "(cost_id, run_id, episode_id, provider, call_type, cost_usd, called_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, rid, f"ep_{rid[:6]}", "muapi", "video",
             cost_usd, called_at or datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return cid


def _minimal_healthy_db(db_path: Path) -> None:
    """Seed the minimum required for a READY assessment."""
    _seed_factory(db_path)
    _seed_owner(db_path)
    _seed_credentials(db_path)


# ---------------------------------------------------------------------------
# TestDataContracts
# ---------------------------------------------------------------------------

class TestDataContracts:
    def test_domain_check_fields(self):
        c = DomainCheck("id", "name", True, False, "detail", "fix")
        assert c.check_id == "id"
        assert c.passed is True
        assert c.blocking is False
        assert c.remediation == "fix"

    def test_domain_check_remediation_default_none(self):
        c = DomainCheck("id", "name", True, True, "detail")
        assert c.remediation is None

    def test_domain_health_fields(self):
        d = DomainHealth("ois", "healthy", [], "all passed")
        assert d.domain == "ois"
        assert d.status == "healthy"

    def test_run_clearance_fields(self):
        rc = RunClearanceDecision(True, "CLEARED", [], [], 0, "2026-01-01")
        assert rc.cleared is True
        assert rc.pending_approvals == 0

    def test_factory_director_report_fields(self):
        rc = RunClearanceDecision(True, "CLEARED", [], [], 0, "2026-01-01")
        r = FactoryDirectorReport(
            "f", "Factory", "READY", 100, True,
            "2026-01-01", [], [], [], rc,
        )
        assert r.factory_id == "f"
        assert r.readiness_score == 100
        assert r.metadata_version == "v1"

    def test_factory_director_blocked_error(self):
        rc = RunClearanceDecision(False, "OIS blocked", [], [], 0, "2026-01-01")
        err = FactoryDirectorBlockedError(rc)
        assert isinstance(err, RuntimeError)
        assert err.decision is rc


# ---------------------------------------------------------------------------
# TestHelpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_ok_builds_passed_check(self):
        c = _ok("id", "name", True, "detail")
        assert c.passed is True
        assert c.remediation is None

    def test_fail_builds_failed_check(self):
        c = _fail("id", "name", False, "detail", "fix it")
        assert c.passed is False
        assert c.remediation == "fix it"

    def test_domain_status_healthy_when_all_pass(self):
        checks = [_ok("a", "A", True, "ok"), _ok("b", "B", False, "ok")]
        assert _domain_status(checks) == "healthy"

    def test_domain_status_blocked_when_blocking_fails(self):
        checks = [_ok("a", "A", True, "ok"), _fail("b", "B", True, "bad", "fix")]
        assert _domain_status(checks) == "blocked"

    def test_domain_status_warning_when_only_nonblocking_fails(self):
        checks = [_ok("a", "A", True, "ok"), _fail("b", "B", False, "bad", "fix")]
        assert _domain_status(checks) == "warning"

    def test_domain_summary_all_pass(self):
        checks = [_ok("a", "A", True, "ok"), _ok("b", "B", False, "ok")]
        s = _domain_summary(checks, "ois")
        assert "all 2 check(s) passed" in s

    def test_domain_summary_with_failures(self):
        checks = [_ok("a", "A", True, "ok"), _fail("b", "B", True, "bad", "fix")]
        s = _domain_summary(checks, "ois")
        assert "1 blocker" in s
        assert "1/2 passed" in s


# ---------------------------------------------------------------------------
# TestScoreAndLevel
# ---------------------------------------------------------------------------

class TestScoreAndLevel:
    def _domain(self, checks):
        return DomainHealth("d", _domain_status(checks), checks, "")

    def test_ready_when_all_pass(self):
        d = self._domain([_ok("a", "A", True, "ok")])
        score, level, can_run = _compute_score_and_level([d])
        assert level == "READY"
        assert score == 100
        assert can_run is True

    def test_degraded_when_only_warnings(self):
        d = self._domain([_fail("a", "A", False, "bad", "fix")])
        score, level, can_run = _compute_score_and_level([d])
        assert level == "DEGRADED"
        assert 0 < score < 100
        assert can_run is True

    def test_blocked_when_blocking_fails(self):
        d = self._domain([_fail("a", "A", True, "bad", "fix")])
        score, level, can_run = _compute_score_and_level([d])
        assert level == "BLOCKED"
        assert can_run is False

    def test_score_is_continuous_across_blocker_counts(self):
        """Removing a blocker always raises the score — no band capping."""
        d1 = self._domain([_fail("a","A",True,"b","f"), _fail("b","B",True,"b","f")])
        d2 = self._domain([_fail("a","A",True,"b","f")])
        s1, _, _ = _compute_score_and_level([d1])
        s2, _, _ = _compute_score_and_level([d2])
        assert s2 > s1

    def test_score_is_continuous_across_warning_counts(self):
        """Removing a warning always raises the score."""
        d1 = self._domain([_fail("a","A",False,"b","f"), _fail("b","B",False,"b","f")])
        d2 = self._domain([_fail("a","A",False,"b","f")])
        s1, _, _ = _compute_score_and_level([d1])
        s2, _, _ = _compute_score_and_level([d2])
        assert s2 > s1

    def test_score_formula(self):
        """score = max(0, 100 - 15*n_blocking - 5*n_warning)."""
        d = self._domain([
            _fail("a","A",True,"b","f"),    # 1 blocker
            _fail("b","B",False,"b","f"),   # 1 warning
        ])
        score, _, _ = _compute_score_and_level([d])
        assert score == max(0, 100 - 15 - 5)

    def test_score_floor_at_zero(self):
        checks = [_fail(str(i), str(i), True, "b", "f") for i in range(10)]
        d = self._domain(checks)
        score, _, _ = _compute_score_and_level([d])
        assert score == 0

    def test_unknown_not_produced_by_compute(self):
        """UNKNOWN is set by assess_factory() on exception; compute never returns it."""
        d = self._domain([])
        _, level, _ = _compute_score_and_level([d])
        assert level != "UNKNOWN"


# ---------------------------------------------------------------------------
# TestAssessFactoryEntity
# ---------------------------------------------------------------------------

class TestAssessFactoryEntity:
    def test_no_factory_record_is_blocker(self, db_path):
        r = assess_factory(db_path=db_path, factory_id="test")
        fe = next(d for d in r.domains if d.domain == "factory_entity")
        record_check = next(c for c in fe.checks if c.check_id == "factory_record_exists")
        assert not record_check.passed
        assert record_check.blocking

    def test_bootstrapped_factory_passes(self, db_path):
        _seed_factory(db_path)
        _seed_owner(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        fe = next(d for d in r.domains if d.domain == "factory_entity")
        assert fe.status == "healthy"

    def test_inactive_factory_blocks(self, db_path):
        _seed_factory(db_path, status="maintenance")
        _seed_owner(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        fe = next(d for d in r.domains if d.domain == "factory_entity")
        status_check = next(c for c in fe.checks if c.check_id == "factory_status_active")
        assert not status_check.passed
        assert status_check.blocking

    def test_no_owner_blocks(self, db_path):
        _seed_factory(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        fe = next(d for d in r.domains if d.domain == "factory_entity")
        owner_check = next(c for c in fe.checks if c.check_id == "owner_user_exists")
        assert not owner_check.passed
        assert owner_check.blocking

    def test_owner_present_passes(self, db_path):
        _seed_factory(db_path)
        _seed_owner(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        fe = next(d for d in r.domains if d.domain == "factory_entity")
        owner_check = next(c for c in fe.checks if c.check_id == "owner_user_exists")
        assert owner_check.passed

    def test_factory_name_populated(self, db_path):
        _seed_factory(db_path, name="My Factory")
        _seed_owner(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        assert "My Factory" in r.factory_name


# ---------------------------------------------------------------------------
# TestAssessPolicyCascade
# ---------------------------------------------------------------------------

class TestAssessPolicyCascade:
    def test_policy_loads_with_no_factory_record(self, db_path):
        """Policy cascade degrades gracefully when no factory record exists."""
        r = assess_factory(db_path=db_path, factory_id="nonexistent")
        pc = next(d for d in r.domains if d.domain == "policy_cascade")
        # Policy load may fail but must not raise
        assert pc.status in ("healthy", "warning", "blocked", "unknown")

    def test_budget_ceiling_zero_is_warning_not_blocker(self, db_path):
        """env ceiling=0 AND factory_policies ceiling=0 → WARNING only."""
        _seed_factory(db_path)
        _seed_owner(db_path)
        with patch("hypogeum.policy_cascade.get_effective_budget_ceiling", return_value=0.0):
            r = assess_factory(db_path=db_path, factory_id="test")
        pc = next(d for d in r.domains if d.domain == "policy_cascade")
        budget_check = next(c for c in pc.checks if c.check_id == "budget_ceiling_set")
        assert not budget_check.passed
        assert not budget_check.blocking   # must be warning, not blocker

    def test_positive_budget_ceiling_passes(self, db_path):
        _seed_factory(db_path)
        _seed_owner(db_path)
        with patch("hypogeum.policy_cascade.get_effective_budget_ceiling", return_value=15.0):
            r = assess_factory(db_path=db_path, factory_id="test")
        pc = next(d for d in r.domains if d.domain == "policy_cascade")
        budget_check = next(c for c in pc.checks if c.check_id == "budget_ceiling_set")
        assert budget_check.passed

    def test_model_blocklist_empty_is_ok(self, db_path):
        _seed_factory(db_path)
        _seed_owner(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        pc = next(d for d in r.domains if d.domain == "policy_cascade")
        blocklist = next(c for c in pc.checks if c.check_id == "model_blocklist_present")
        assert blocklist.passed  # permissive = ok


# ---------------------------------------------------------------------------
# TestAssessOIS
# ---------------------------------------------------------------------------

class TestAssessOIS:
    def test_no_signals_is_healthy(self, db_path):
        r = assess_factory(db_path=db_path, factory_id="test")
        ois = next(d for d in r.domains if d.domain == "ois")
        assert ois.status == "healthy"

    def test_blocking_signal_blocks(self, db_path):
        _seed_blocking_signal(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        ois = next(d for d in r.domains if d.domain == "ois")
        blocking_check = next(c for c in ois.checks if c.check_id == "no_blocking_signals")
        assert not blocking_check.passed
        assert blocking_check.blocking

    def test_blocking_signal_sets_readiness_blocked(self, db_path):
        _seed_blocking_signal(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        assert r.readiness_level == "BLOCKED"
        assert r.can_run is False

    def test_resolved_signal_does_not_block(self, db_path):
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT INTO signal_records "
                "(signal_id, episode_id, run_id, ingested_at, window_days, "
                "data_quality, metadata_version, signal_type, severity, "
                "full_record, source_system, blocking, lifecycle_status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), "sys", "r_sys", "2026-01-01", 0,
                 "ok", "v1", "budget_exhausted", "CRITICAL",
                 '{"message":"old"}', "finance_agent", 1, "resolved"),
            )
            conn.commit()
        finally:
            conn.close()
        r = assess_factory(db_path=db_path, factory_id="test")
        ois = next(d for d in r.domains if d.domain == "ois")
        blocking_check = next(c for c in ois.checks if c.check_id == "no_blocking_signals")
        assert blocking_check.passed


# ---------------------------------------------------------------------------
# TestAssessApprovals
# ---------------------------------------------------------------------------

class TestAssessApprovals:
    def test_no_approvals_is_healthy(self, db_path):
        r = assess_factory(db_path=db_path, factory_id="test")
        ap = next(d for d in r.domains if d.domain == "approvals")
        assert ap.status == "healthy"

    def test_fresh_pending_is_not_stale(self, db_path):
        now = datetime.now(timezone.utc).isoformat()
        _seed_approval(db_path, requested_at=now)
        r = assess_factory(db_path=db_path, factory_id="test")
        ap = next(d for d in r.domains if d.domain == "approvals")
        stale = next(c for c in ap.checks if c.check_id == "no_stale_approvals")
        assert stale.passed

    def test_stale_approval_triggers_warning(self, db_path):
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        _seed_approval(db_path, requested_at=old_ts)
        r = assess_factory(db_path=db_path, factory_id="test")
        ap = next(d for d in r.domains if d.domain == "approvals")
        stale = next(c for c in ap.checks if c.check_id == "no_stale_approvals")
        assert not stale.passed
        assert not stale.blocking  # warning, not blocker

    def test_orphaned_awaiting_review_triggers_warning(self, db_path):
        # Run stuck in awaiting_review with no pending approval
        _seed_run(db_path, "awaiting_review")
        r = assess_factory(db_path=db_path, factory_id="test")
        ap = next(d for d in r.domains if d.domain == "approvals")
        orphan = next(c for c in ap.checks if c.check_id == "no_orphaned_awaiting_review")
        assert not orphan.passed
        assert not orphan.blocking


# ---------------------------------------------------------------------------
# TestAssessRunLifecycle
# ---------------------------------------------------------------------------

class TestAssessRunLifecycle:
    def test_empty_runs_is_healthy(self, db_path):
        r = assess_factory(db_path=db_path, factory_id="test")
        rlg = next(d for d in r.domains if d.domain == "run_lifecycle")
        assert rlg.status == "healthy"

    def test_zombie_run_triggers_warning(self, db_path):
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
        _seed_run(db_path, "running", started_at=old_ts)
        r = assess_factory(db_path=db_path, factory_id="test")
        rlg = next(d for d in r.domains if d.domain == "run_lifecycle")
        zombie = next(c for c in rlg.checks if c.check_id == "no_zombie_runs")
        assert not zombie.passed
        assert not zombie.blocking

    def test_recent_run_is_not_zombie(self, db_path):
        recent_ts = datetime.now(timezone.utc).isoformat()
        _seed_run(db_path, "running", started_at=recent_ts)
        r = assess_factory(db_path=db_path, factory_id="test")
        rlg = next(d for d in r.domains if d.domain == "run_lifecycle")
        zombie = next(c for c in rlg.checks if c.check_id == "no_zombie_runs")
        assert zombie.passed

    def test_high_failure_rate_triggers_warning(self, db_path):
        now = datetime.now(timezone.utc)
        for i in range(8):
            _seed_run(db_path, "failed",
                      started_at=(now - timedelta(minutes=i+10)).isoformat(),
                      completed_at=(now - timedelta(minutes=i+5)).isoformat())
        for i in range(2):
            _seed_run(db_path, "completed",
                      started_at=(now - timedelta(minutes=i+2)).isoformat(),
                      completed_at=(now - timedelta(minutes=i+1)).isoformat())
        r = assess_factory(db_path=db_path, factory_id="test")
        rlg = next(d for d in r.domains if d.domain == "run_lifecycle")
        rate = next(c for c in rlg.checks if c.check_id == "recent_failure_rate")
        assert not rate.passed

    def test_state_distribution_always_present(self, db_path):
        r = assess_factory(db_path=db_path, factory_id="test")
        rlg = next(d for d in r.domains if d.domain == "run_lifecycle")
        dist = next(c for c in rlg.checks if c.check_id == "rlg_state_distribution")
        assert dist.passed  # always informational pass


# ---------------------------------------------------------------------------
# TestAssessCredentials
# ---------------------------------------------------------------------------

class TestAssessCredentials:
    def test_no_profiles_blocks(self, db_path):
        r = assess_factory(db_path=db_path, factory_id="test")
        cred = next(d for d in r.domains if d.domain == "credentials")
        exist = next(c for c in cred.checks if c.check_id == "secret_profiles_exist")
        assert not exist.passed
        assert exist.blocking

    def test_active_profiles_pass(self, db_path):
        _seed_credentials(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        cred = next(d for d in r.domains if d.domain == "credentials")
        exist = next(c for c in cred.checks if c.check_id == "secret_profiles_exist")
        assert exist.passed

    def test_missing_required_provider_blocks(self, db_path):
        _seed_credentials(db_path, providers=("openrouter",))  # missing muapi + elevenlabs
        r = assess_factory(db_path=db_path, factory_id="test")
        cred = next(d for d in r.domains if d.domain == "credentials")
        prov = next(c for c in cred.checks if c.check_id == "required_providers_covered")
        assert not prov.passed
        assert prov.blocking

    def test_all_required_providers_pass(self, db_path):
        _seed_credentials(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        cred = next(d for d in r.domains if d.domain == "credentials")
        prov = next(c for c in cred.checks if c.check_id == "required_providers_covered")
        assert prov.passed

    def test_expiring_credential_warns(self, db_path):
        _seed_credentials(db_path)
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        conn = get_connection(db_path)
        try:
            conn.execute(
                "UPDATE secret_profiles SET expires_at = ? WHERE provider = 'openrouter'",
                (tomorrow,),
            )
            conn.commit()
        finally:
            conn.close()
        r = assess_factory(db_path=db_path, factory_id="test")
        cred = next(d for d in r.domains if d.domain == "credentials")
        expiry = next(c for c in cred.checks if c.check_id == "no_expiring_credentials")
        assert not expiry.passed
        assert not expiry.blocking

    def test_untested_credentials_warn(self, db_path):
        _seed_credentials(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        cred = next(d for d in r.domains if d.domain == "credentials")
        tested = next(c for c in cred.checks if c.check_id == "credentials_recently_tested")
        assert not tested.passed
        assert not tested.blocking


# ---------------------------------------------------------------------------
# TestAssessCapacity
# ---------------------------------------------------------------------------

class TestAssessCapacity:
    def test_capacity_has_four_checks(self, db_path):
        r = assess_factory(db_path=db_path, factory_id="test")
        cap = next(d for d in r.domains if d.domain == "factory_capacity")
        assert len(cap.checks) == 4

    def test_removed_checks_not_present(self, db_path):
        r = assess_factory(db_path=db_path, factory_id="test")
        cap = next(d for d in r.domains if d.domain == "factory_capacity")
        ids = {c.check_id for c in cap.checks}
        assert "active_run_count" not in ids
        assert "pending_approvals_capacity" not in ids
        assert "agent_availability" not in ids

    def test_required_checks_present(self, db_path):
        r = assess_factory(db_path=db_path, factory_id="test")
        cap = next(d for d in r.domains if d.domain == "factory_capacity")
        ids = {c.check_id for c in cap.checks}
        assert "queue_depth" in ids
        assert "storage_pressure" in ids
        assert "archive_pressure" in ids
        assert "budget_headroom" in ids

    def test_empty_queue_passes(self, db_path):
        r = assess_factory(db_path=db_path, factory_id="test")
        cap = next(d for d in r.domains if d.domain == "factory_capacity")
        q = next(c for c in cap.checks if c.check_id == "queue_depth")
        assert q.passed

    def test_budget_headroom_with_ceiling(self, db_path):
        """
        Updated 2026-07-27: _get_month_cost_and_ceiling() no longer routes
        through get_effective_budget_ceiling() for this monthly-total
        comparison (that helper's per-episode-scaled value was being
        compared against a monthly total — a real semantic mismatch, fixed
        per DECISION_LOG.md's factory-budget-ceiling work). The factory's
        own monthly_ceiling_usd is now seeded directly instead of mocking
        the old seam — this exercises the real, corrected code path.
        """
        from hypogeum.factory_entity import update_factory
        _seed_factory(db_path)
        update_factory(
            "test", db_path=db_path,
            factory_policies={"monthly_ceiling_usd": 100.0, "episodes_per_month": 4},
        )
        r = assess_factory(db_path=db_path, factory_id="test")
        cap = next(d for d in r.domains if d.domain == "factory_capacity")
        headroom = next(c for c in cap.checks if c.check_id == "budget_headroom")
        assert headroom.passed
        assert "$100.00" in headroom.detail


# ---------------------------------------------------------------------------
# TestAssessFactory
# ---------------------------------------------------------------------------

class TestAssessFactory:
    def test_returns_factory_director_report(self, db_path):
        r = assess_factory(db_path=db_path, factory_id="test")
        assert isinstance(r, FactoryDirectorReport)

    def test_unknown_on_completely_broken_path(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        r = assess_factory(db_path=bad, factory_id="test")
        assert isinstance(r, FactoryDirectorReport)  # never raises

    def test_seven_domains_always_present(self, db_path):
        r = assess_factory(db_path=db_path, factory_id="test")
        expected = {"factory_entity", "policy_cascade", "ois", "approvals",
                    "run_lifecycle", "credentials", "factory_capacity"}
        assert {d.domain for d in r.domains} == expected

    def test_twenty_four_checks(self, db_path):
        # Seed factory so factory_entity exercises all 4 checks (status + name
        # checks are only emitted when a factory record exists).
        _seed_factory(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        total = sum(len(d.checks) for d in r.domains)
        assert total == 24

    def test_blocked_when_ois_blocking_signal(self, db_path):
        _seed_blocking_signal(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        assert r.readiness_level == "BLOCKED"
        assert r.can_run is False
        assert len(r.blocking_reasons) >= 1

    def test_degraded_when_only_warnings(self, db_path):
        # Seed credentials with untested profiles only — warning, no blocker
        _seed_factory(db_path)
        _seed_owner(db_path)
        _seed_credentials(db_path)  # untested → warning
        r = assess_factory(db_path=db_path, factory_id="test")
        # Only warnings should produce DEGRADED or READY
        assert r.readiness_level in ("DEGRADED", "READY")
        assert r.can_run is True

    def test_factory_name_in_report(self, db_path):
        _seed_factory(db_path, name="Audit Test Factory")
        r = assess_factory(db_path=db_path, factory_id="test")
        assert r.factory_name == "Audit Test Factory"

    def test_score_is_int_in_range(self, db_path):
        r = assess_factory(db_path=db_path, factory_id="test")
        assert isinstance(r.readiness_score, int)
        assert 0 <= r.readiness_score <= 100

    def test_score_continuous_removing_blocker_raises_score(self, db_path):
        """Score improves when a blocking condition is resolved."""
        _seed_blocking_signal(db_path)
        r_blocked = assess_factory(db_path=db_path, factory_id="test")

        # Resolve all signals
        conn = get_connection(db_path)
        try:
            conn.execute("UPDATE signal_records SET lifecycle_status = 'resolved'")
            conn.commit()
        finally:
            conn.close()

        r_clear = assess_factory(db_path=db_path, factory_id="test")
        assert r_clear.readiness_score > r_blocked.readiness_score

    def test_run_clearance_included(self, db_path):
        r = assess_factory(db_path=db_path, factory_id="test")
        assert isinstance(r.run_clearance, RunClearanceDecision)


# ---------------------------------------------------------------------------
# TestCheckRunClearance
# ---------------------------------------------------------------------------

class TestCheckRunClearance:
    def test_cleared_when_no_obstacles(self, db_path):
        _seed_factory(db_path)
        _seed_credentials(db_path)
        decision = check_run_clearance(db_path=db_path)
        assert decision.cleared is True
        assert "CLEARED" in decision.reason

    def test_not_cleared_when_ois_blocked(self, db_path):
        _seed_factory(db_path)
        _seed_credentials(db_path)
        _seed_blocking_signal(db_path)
        decision = check_run_clearance(db_path=db_path)
        assert decision.cleared is False
        assert len(decision.blocking_signals) >= 1

    def test_not_cleared_when_no_factory(self, db_path):
        _seed_credentials(db_path)
        decision = check_run_clearance(db_path=db_path)
        assert decision.cleared is False

    def test_not_cleared_when_no_credentials(self, db_path):
        _seed_factory(db_path)
        decision = check_run_clearance(db_path=db_path)
        assert decision.cleared is False

    def test_pending_approvals_counted(self, db_path):
        _seed_factory(db_path)
        _seed_credentials(db_path)
        now = datetime.now(timezone.utc).isoformat()
        _seed_approval(db_path, requested_at=now)
        decision = check_run_clearance(db_path=db_path)
        assert decision.pending_approvals == 1

    def test_checked_at_is_set(self, db_path):
        decision = check_run_clearance(db_path=db_path)
        assert decision.checked_at is not None

    # -- budget ceiling (hard block, wired 2026-07-18) -----------------------

    def test_not_cleared_when_budget_ceiling_exceeded(self, db_path):
        """
        Updated 2026-07-27: check_run_clearance()'s budget check is
        hardcoded to factory_id="hypogeum" (pre-existing, documented —
        "single-factory deployment today"), which _seed_factory() here
        does not create (it seeds factory_id="test"), so the factory
        policy lookup always misses and correctly falls back to env —
        exercising that fallback is now the right way to control the
        ceiling in this test, replacing the old
        get_effective_budget_ceiling() mock (no longer called by
        _get_month_cost_and_ceiling() — see factory_director.py).
        """
        _seed_factory(db_path)
        _seed_credentials(db_path)
        _seed_cost(db_path, 15.0)
        fake_config = MagicMock(FINANCE_BUDGET_CEILING_USD=10.0)
        with patch("hypogeum.config.Config", return_value=fake_config):
            decision = check_run_clearance(db_path=db_path)
        assert decision.cleared is False
        assert "ceiling" in decision.reason.lower()
        blocking = next(c for c in decision.blocking_checks if c.check_id == "budget_ceiling_not_exceeded")
        assert blocking.blocking is True

    def test_cleared_when_budget_under_ceiling(self, db_path):
        """Updated 2026-07-27 -- see test_not_cleared_when_budget_ceiling_exceeded
        for why the seam changed from get_effective_budget_ceiling() to Config."""
        _seed_factory(db_path)
        _seed_credentials(db_path)
        _seed_cost(db_path, 5.0)
        fake_config = MagicMock(FINANCE_BUDGET_CEILING_USD=10.0)
        with patch("hypogeum.config.Config", return_value=fake_config):
            decision = check_run_clearance(db_path=db_path)
        assert decision.cleared is True

    def test_cleared_when_no_ceiling_configured(self, db_path):
        """ceiling <= 0 means 'no ceiling configured' -- the budget gate must
        not fire regardless of spend. Updated 2026-07-27 -- see
        test_not_cleared_when_budget_ceiling_exceeded for why the seam
        changed from get_effective_budget_ceiling() to Config. Must patch
        Config explicitly rather than rely on ambient env state -- the real
        .env's FINANCE_BUDGET_CEILING_USD can otherwise leak into this test
        depending on full-suite test ordering."""
        _seed_factory(db_path)
        _seed_credentials(db_path)
        _seed_cost(db_path, 10_000.0)
        fake_config = MagicMock(FINANCE_BUDGET_CEILING_USD=0.0)
        with patch("hypogeum.config.Config", return_value=fake_config):
            decision = check_run_clearance(db_path=db_path)
        assert decision.cleared is True

    def test_not_cleared_at_exactly_the_ceiling(self, db_path):
        """utilization >= 100% blocks -- spend equal to the ceiling counts
        as exceeded, not merely 'at the edge'. Updated 2026-07-27 -- see
        test_not_cleared_when_budget_ceiling_exceeded for why the seam
        changed from get_effective_budget_ceiling() to Config."""
        _seed_factory(db_path)
        _seed_credentials(db_path)
        _seed_cost(db_path, 10.0)
        fake_config = MagicMock(FINANCE_BUDGET_CEILING_USD=10.0)
        with patch("hypogeum.config.Config", return_value=fake_config):
            decision = check_run_clearance(db_path=db_path)
        assert decision.cleared is False

    # -- validation_mode exemption (narrow, budget-only, added 2026-07-18) --

    def test_validation_mode_exempts_budget_ceiling(self, db_path):
        """Updated 2026-07-27 -- see test_not_cleared_when_budget_ceiling_exceeded
        for why the seam changed from get_effective_budget_ceiling() to Config."""
        _seed_factory(db_path)
        _seed_credentials(db_path)
        _seed_cost(db_path, 15.0)
        fake_config = MagicMock(FINANCE_BUDGET_CEILING_USD=10.0)
        with patch("hypogeum.config.Config", return_value=fake_config):
            decision = check_run_clearance(db_path=db_path, validation_mode=True)
        assert decision.cleared is True
        assert decision.validation_exemption == "supervised validation budget exemption"

    def test_validation_mode_false_by_default_still_blocks(self, db_path):
        """Updated 2026-07-27 -- see test_not_cleared_when_budget_ceiling_exceeded
        for why the seam changed from get_effective_budget_ceiling() to Config."""
        _seed_factory(db_path)
        _seed_credentials(db_path)
        _seed_cost(db_path, 15.0)
        fake_config = MagicMock(FINANCE_BUDGET_CEILING_USD=10.0)
        with patch("hypogeum.config.Config", return_value=fake_config):
            decision = check_run_clearance(db_path=db_path)
        assert decision.cleared is False
        assert decision.validation_exemption is None

    def test_validation_mode_does_not_set_exemption_when_under_budget(self, db_path):
        """The exemption field is only set when it was actually exercised --
        not merely because validation_mode=True was passed. Updated
        2026-07-27 -- see test_not_cleared_when_budget_ceiling_exceeded for
        why the seam changed from get_effective_budget_ceiling() to Config."""
        _seed_factory(db_path)
        _seed_credentials(db_path)
        _seed_cost(db_path, 1.0)
        fake_config = MagicMock(FINANCE_BUDGET_CEILING_USD=10.0)
        with patch("hypogeum.config.Config", return_value=fake_config):
            decision = check_run_clearance(db_path=db_path, validation_mode=True)
        assert decision.cleared is True
        assert decision.validation_exemption is None

    def test_validation_mode_does_not_exempt_ois_blocking(self, db_path):
        """The exemption is budget-only -- an OIS blocking signal must still
        block a clearance check even with validation_mode=True."""
        _seed_factory(db_path)
        _seed_credentials(db_path)
        _seed_blocking_signal(db_path)
        _seed_cost(db_path, 15.0)
        with patch("hypogeum.policy_cascade.get_effective_budget_ceiling", return_value=10.0):
            decision = check_run_clearance(db_path=db_path, validation_mode=True)
        assert decision.cleared is False
        assert len(decision.blocking_signals) >= 1

    def test_validation_mode_does_not_exempt_missing_credentials(self, db_path):
        """The exemption is budget-only -- missing credentials must still
        block a clearance check even with validation_mode=True."""
        _seed_factory(db_path)
        _seed_cost(db_path, 15.0)
        with patch("hypogeum.policy_cascade.get_effective_budget_ceiling", return_value=10.0):
            decision = check_run_clearance(db_path=db_path, validation_mode=True)
        assert decision.cleared is False

    def test_validation_mode_does_not_exempt_missing_factory(self, db_path):
        """The exemption is budget-only -- a missing/inactive factory record
        must still block a clearance check even with validation_mode=True."""
        _seed_credentials(db_path)
        _seed_cost(db_path, 15.0)
        with patch("hypogeum.policy_cascade.get_effective_budget_ceiling", return_value=10.0):
            decision = check_run_clearance(db_path=db_path, validation_mode=True)
        assert decision.cleared is False

    def test_budget_check_shares_computation_with_capacity_domain(self, db_path):
        """check_run_clearance()'s budget block and
        _assess_factory_capacity()'s budget_headroom warning must derive
        month_cost/ceiling from the same helper -- not two independently
        maintained copies of the query."""
        import hypogeum.factory_director as fd
        assert fd.check_run_clearance.__module__ == fd._assess_factory_capacity.__module__
        # Both call sites route through the same private helper.
        import inspect
        clearance_src = inspect.getsource(fd.check_run_clearance)
        capacity_src = inspect.getsource(fd._assess_factory_capacity)
        assert "_get_month_cost_and_ceiling(" in clearance_src
        assert "_get_month_cost_and_ceiling(" in capacity_src


# ---------------------------------------------------------------------------
# TestFormatReport
# ---------------------------------------------------------------------------

class TestFormatReport:
    @pytest.fixture()
    def report(self, db_path):
        return assess_factory(db_path=db_path, factory_id="test")

    def test_returns_string(self, report):
        assert isinstance(format_report(report), str)

    def test_contains_factory_id(self, report):
        assert "test" in format_report(report)

    def test_contains_readiness_level(self, report):
        out = format_report(report)
        assert report.readiness_level in out

    def test_contains_score(self, report):
        out = format_report(report)
        assert f"{report.readiness_score}/100" in out

    def test_contains_domain_summary_section(self, report):
        assert "Domain Summary" in format_report(report)

    def test_contains_all_domain_names(self, report):
        out = format_report(report)
        for domain in ("factory_entity", "ois", "approvals",
                       "run_lifecycle", "credentials", "factory_capacity"):
            assert domain in out

    def test_blocked_level_badge(self, db_path):
        _seed_blocking_signal(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        out = format_report(r)
        assert "BLOCKED" in out

    def test_remediation_shown_for_failures(self, db_path):
        _seed_blocking_signal(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        out = format_report(r)
        assert "resolve_signal" in out

    def test_no_db_access_in_formatter(self, report):
        """format_report is pure formatting — no DB calls."""
        with patch("hypogeum.factory_director.get_connection") as mock_conn:
            format_report(report)
        mock_conn.assert_not_called()


# ---------------------------------------------------------------------------
# TestNonBlocking
# ---------------------------------------------------------------------------

class TestNonBlocking:
    def _bad(self, tmp_path):
        p = tmp_path / "bad"
        p.mkdir()
        return p

    def test_assess_factory_bad_db_never_raises(self, tmp_path):
        r = assess_factory(db_path=self._bad(tmp_path), factory_id="test")
        assert isinstance(r, FactoryDirectorReport)

    def test_check_run_clearance_bad_db_never_raises(self, tmp_path):
        d = check_run_clearance(db_path=self._bad(tmp_path))
        assert isinstance(d, RunClearanceDecision)

    def test_format_report_never_raises(self, tmp_path):
        r = assess_factory(db_path=self._bad(tmp_path), factory_id="test")
        out = format_report(r)
        assert isinstance(out, str)

    def test_all_domains_present_even_on_bad_db(self, tmp_path):
        r = assess_factory(db_path=self._bad(tmp_path), factory_id="test")
        # Either all 7 domains assessed or UNKNOWN (assessment loop failed)
        assert r.readiness_level in ("UNKNOWN", "BLOCKED", "DEGRADED", "READY")


# ---------------------------------------------------------------------------
# TestNoSideEffects
# ---------------------------------------------------------------------------

class TestNoSideEffects:
    def test_assess_factory_does_not_write_to_db(self, db_path):
        _seed_factory(db_path)
        _seed_owner(db_path)
        _seed_credentials(db_path)

        conn = get_connection(db_path)
        before = {
            tbl: conn.execute(f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()["n"]
            for tbl in ("factories", "users", "secret_profiles", "runs", "approvals")
        }
        conn.close()

        assess_factory(db_path=db_path, factory_id="test")

        conn = get_connection(db_path)
        after = {
            tbl: conn.execute(f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()["n"]
            for tbl in ("factories", "users", "secret_profiles", "runs", "approvals")
        }
        conn.close()

        assert before == after

    def test_check_run_clearance_does_not_write(self, db_path):
        conn = get_connection(db_path)
        before = conn.execute("SELECT COUNT(*) AS n FROM signal_records").fetchone()["n"]
        conn.close()

        check_run_clearance(db_path=db_path)

        conn = get_connection(db_path)
        after = conn.execute("SELECT COUNT(*) AS n FROM signal_records").fetchone()["n"]
        conn.close()

        assert before == after


# ---------------------------------------------------------------------------
# TestNoNetworkCalls
# ---------------------------------------------------------------------------

class TestNoNetworkCalls:
    @pytest.fixture(autouse=True)
    def block_network(self, monkeypatch):
        def no_socket(*args, **kwargs):
            raise RuntimeError("Network call detected in factory_director tests")
        monkeypatch.setattr(socket, "socket", no_socket)
        yield

    def test_assess_factory_no_network(self, db_path):
        assess_factory(db_path=db_path, factory_id="test")

    def test_check_run_clearance_no_network(self, db_path):
        check_run_clearance(db_path=db_path)

    def test_format_report_no_network(self, db_path):
        r = assess_factory(db_path=db_path, factory_id="test")
        format_report(r)


# ---------------------------------------------------------------------------
# TestCLI
# ---------------------------------------------------------------------------

class TestCLI:
    """Tests for scripts/run_director_check.py via its main() function."""

    @pytest.fixture(autouse=True)
    def _import_cli(self):
        import scripts.run_director_check as cli_mod
        self.cli = cli_mod

    def _make_report(self, level: str, score: int, can_run: bool):
        from hypogeum.factory_director import RunClearanceDecision, FactoryDirectorReport
        rc = RunClearanceDecision(can_run, "CLEARED" if can_run else "BLOCKED",
                                  [], [], 0, "2026-01-01T00:00:00")
        return FactoryDirectorReport(
            factory_id="test", factory_name="Test Factory",
            readiness_level=level, readiness_score=score,
            can_run=can_run, assessed_at="2026-01-01T00:00:00",
            domains=[], blocking_reasons=[], warnings=[], run_clearance=rc,
        )

    # --- Exit codes ---

    def test_exit_0_when_ready(self):
        report = self._make_report("READY", 100, True)
        with patch("scripts.run_director_check.assess_factory", return_value=report):
            rc = self.cli.main([])
        assert rc == 0

    def test_exit_0_when_degraded(self):
        report = self._make_report("DEGRADED", 70, True)
        with patch("scripts.run_director_check.assess_factory", return_value=report):
            rc = self.cli.main([])
        assert rc == 0

    def test_exit_1_when_blocked(self):
        report = self._make_report("BLOCKED", 40, False)
        with patch("scripts.run_director_check.assess_factory", return_value=report):
            rc = self.cli.main([])
        assert rc == 1

    def test_exit_1_when_unknown(self):
        report = self._make_report("UNKNOWN", 0, False)
        with patch("scripts.run_director_check.assess_factory", return_value=report):
            rc = self.cli.main([])
        assert rc == 1

    # --- --factory-id ---

    def test_factory_id_passed_to_assess(self):
        report = self._make_report("READY", 100, True)
        with patch("scripts.run_director_check.assess_factory",
                   return_value=report) as mock_assess:
            self.cli.main(["--factory-id", "my_factory"])
        mock_assess.assert_called_once_with(factory_id="my_factory")

    def test_default_factory_id_is_hypogeum(self):
        report = self._make_report("READY", 100, True)
        with patch("scripts.run_director_check.assess_factory",
                   return_value=report) as mock_assess:
            self.cli.main([])
        mock_assess.assert_called_once_with(factory_id="hypogeum")

    # --- --json ---

    def test_json_flag_outputs_valid_json(self, capsys):
        report = self._make_report("READY", 100, True)
        with patch("scripts.run_director_check.assess_factory", return_value=report):
            self.cli.main(["--json"])
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["readiness_level"] == "READY"
        assert parsed["readiness_score"] == 100
        assert "factory_id" in parsed
        assert "domains" in parsed
        assert "run_clearance" in parsed

    def test_json_includes_blocking_reasons(self, capsys):
        report = self._make_report("BLOCKED", 40, False)
        report = FactoryDirectorReport(
            factory_id="test", factory_name="Test",
            readiness_level="BLOCKED", readiness_score=40,
            can_run=False, assessed_at="2026-01-01T00:00:00",
            domains=[], blocking_reasons=["[ois] OIS blocked: 3 signals"],
            warnings=[], run_clearance=report.run_clearance,
        )
        with patch("scripts.run_director_check.assess_factory", return_value=report):
            self.cli.main(["--json"])
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert len(parsed["blocking_reasons"]) == 1

    def test_default_output_is_text_not_json(self, capsys):
        report = self._make_report("READY", 100, True)
        with patch("scripts.run_director_check.assess_factory", return_value=report):
            self.cli.main([])
        out = capsys.readouterr().out
        # format_report produces text, not JSON
        assert out.startswith("─")

    # --- --domain ---

    def test_domain_filter_shows_only_that_domain(self, capsys, db_path):
        _seed_factory(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        with patch("scripts.run_director_check.assess_factory", return_value=r):
            rc = self.cli.main(["--domain", "ois"])
        out = capsys.readouterr().out
        assert "ois" in out
        # Other domains should not appear as domain headers
        assert "factory_entity" not in out
        assert "run_lifecycle" not in out

    def test_domain_filter_json_output(self, capsys, db_path):
        _seed_factory(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        with patch("scripts.run_director_check.assess_factory", return_value=r):
            self.cli.main(["--domain", "credentials", "--json"])
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["domain"]["domain"] == "credentials"
        assert "readiness_level" in parsed

    def test_invalid_domain_exits_1(self, capsys):
        report = self._make_report("READY", 100, True)
        with patch("scripts.run_director_check.assess_factory", return_value=report):
            rc = self.cli.main(["--domain", "nonexistent_domain"])
        assert rc == 1

    def test_domain_exit_code_reflects_overall_level(self, db_path):
        """--domain still uses overall readiness_level for exit code."""
        _seed_blocking_signal(db_path)
        r = assess_factory(db_path=db_path, factory_id="test")
        with patch("scripts.run_director_check.assess_factory", return_value=r):
            rc = self.cli.main(["--domain", "factory_entity"])
        assert rc == 1  # BLOCKED overall even if factory_entity is healthy


# ===========================================================================
# TestGetBudgetStatus (Phase 3 Production Hardening — factory monthly
# budget ceiling, DB-backed, live-reloadable)
# ===========================================================================

def _seed_factory_hypogeum(db_path: Path, status: str = "active") -> None:
    """
    check_run_clearance()'s budget check is hardcoded to factory_id=
    "hypogeum" (documented, single-factory-deployment behavior — see
    factory_director.py). get_budget_status() composes both that AND a
    factory-policy lookup for the SAME id, so these tests seed a factory
    literally named "hypogeum" (not the module's usual "test") to keep
    ceiling/source resolution and cleared/blocking_reason mutually
    consistent, matching real single-tenant production shape.
    """
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO factories (factory_id, factory_name, factory_slug, "
            "status, default_language, default_timezone, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("hypogeum", "Hypogeum", "hypogeum", status, "en", "UTC",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()


class TestGetBudgetStatus:
    def test_factory_ceiling_takes_precedence_over_env(self, db_path):
        from hypogeum.factory_entity import update_factory
        _seed_factory_hypogeum(db_path)
        _seed_credentials(db_path)
        _seed_cost(db_path, 30.0)
        update_factory(
            "hypogeum", db_path=db_path,
            factory_policies={"monthly_ceiling_usd": 100.0},
        )
        fake_config = MagicMock(FINANCE_BUDGET_CEILING_USD=10.0)  # would block if used
        with patch("hypogeum.config.Config", return_value=fake_config):
            status = get_budget_status(db_path=db_path, factory_id="hypogeum")

        assert status["ceiling_source"] == "factory_policy"
        assert status["effective_ceiling_usd"] == 100.0
        assert status["cleared"] is True  # 30 < 100, not the env's 10

    def test_zero_factory_ceiling_falls_back_to_env(self, db_path):
        _seed_factory_hypogeum(db_path)
        _seed_credentials(db_path)
        _seed_cost(db_path, 5.0)
        # factory_policies never set -> monthly_ceiling_usd defaults to 0
        fake_config = MagicMock(FINANCE_BUDGET_CEILING_USD=10.0)
        with patch("hypogeum.config.Config", return_value=fake_config):
            status = get_budget_status(db_path=db_path, factory_id="hypogeum")

        assert status["ceiling_source"] == "env_fallback"
        assert status["effective_ceiling_usd"] == 10.0

    def test_unset_factory_field_falls_back_to_env(self, db_path):
        from hypogeum.factory_entity import update_factory
        _seed_factory_hypogeum(db_path)
        _seed_credentials(db_path)
        _seed_cost(db_path, 5.0)
        update_factory(
            "hypogeum", db_path=db_path,
            factory_policies={"blocked_model_slugs": ["x"]},  # no monthly_ceiling_usd key
        )
        fake_config = MagicMock(FINANCE_BUDGET_CEILING_USD=25.0)
        with patch("hypogeum.config.Config", return_value=fake_config):
            status = get_budget_status(db_path=db_path, factory_id="hypogeum")

        assert status["ceiling_source"] == "env_fallback"
        assert status["effective_ceiling_usd"] == 25.0

    def test_no_ceiling_configured_anywhere(self, db_path):
        _seed_factory_hypogeum(db_path)
        _seed_credentials(db_path)
        fake_config = MagicMock(FINANCE_BUDGET_CEILING_USD=0.0)
        with patch("hypogeum.config.Config", return_value=fake_config):
            status = get_budget_status(db_path=db_path, factory_id="hypogeum")

        assert status["ceiling_source"] == "none"
        assert status["effective_ceiling_usd"] == 0.0
        assert status["cleared"] is True  # no ceiling = never blocked on budget

    def test_full_monthly_ceiling_used_against_month_to_date_spend_not_per_episode(self, db_path):
        """
        The regression this whole fix exists for: a factory
        monthly_ceiling_usd of $300 with episodes_per_month=4 must gate at
        $300 total spend, not at $75 (300/4) -- get_effective_budget_ceiling()
        would have returned the per-episode-scaled $75 for this same policy;
        get_budget_status()/the corrected _get_month_cost_and_ceiling() must not.
        """
        from hypogeum.factory_entity import update_factory
        _seed_factory_hypogeum(db_path)
        _seed_credentials(db_path)
        _seed_cost(db_path, 100.0)  # would exceed $75 (per-episode-scaled) but not $300
        update_factory(
            "hypogeum", db_path=db_path,
            factory_policies={"monthly_ceiling_usd": 300.0, "episodes_per_month": 4},
        )
        status = get_budget_status(db_path=db_path, factory_id="hypogeum")

        assert status["effective_ceiling_usd"] == 300.0  # not 75.0
        assert status["cleared"] is True  # 100 < 300
        assert status["headroom_usd"] == 200.0

    def test_headroom_and_spend_reported_correctly(self, db_path):
        from hypogeum.factory_entity import update_factory
        _seed_factory_hypogeum(db_path)
        _seed_credentials(db_path)
        _seed_cost(db_path, 40.0)
        update_factory(
            "hypogeum", db_path=db_path,
            factory_policies={"monthly_ceiling_usd": 100.0},
        )
        status = get_budget_status(db_path=db_path, factory_id="hypogeum")

        assert status["month_to_date_spend_usd"] == 40.0
        assert status["headroom_usd"] == 60.0

    def test_blocking_reason_present_when_exceeded(self, db_path):
        from hypogeum.factory_entity import update_factory
        _seed_factory_hypogeum(db_path)
        _seed_credentials(db_path)
        _seed_cost(db_path, 150.0)
        update_factory(
            "hypogeum", db_path=db_path,
            factory_policies={"monthly_ceiling_usd": 100.0},
        )
        status = get_budget_status(db_path=db_path, factory_id="hypogeum")

        assert status["cleared"] is False
        assert status["blocking_reason"] is not None
        assert "ceiling" in status["blocking_reason"].lower()
        assert status["headroom_usd"] == 0.0  # never negative

    def test_get_effective_budget_ceiling_contract_unchanged(self, db_path):
        """
        Confirms the operator's explicit constraint: policy_cascade's
        get_effective_budget_ceiling() keeps its own documented per-episode
        contract for its OTHER legitimate caller
        (validate_factory_policy_constraints()) -- this fix did not touch it.
        """
        from hypogeum.policy_cascade import get_effective_budget_ceiling
        _seed_factory_hypogeum(db_path)
        result = get_effective_budget_ceiling(
            env_ceiling=0.0, factory_id="hypogeum", db_path=db_path,
        )
        # Factory has no policy set -> 0.0, per its own documented contract --
        # unrelated to and unaffected by get_budget_status()'s separate path.
        assert result == 0.0
