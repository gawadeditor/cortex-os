"""
tests/test_grant_binding_hardening.py — Closure tests for the grant-
binding gap: hypogeum.authorization_authority.
activate_authorization_proposal() now REQUIRES grant_id/grant_version,
resolves and verifies the grant, validates every envelope's binding,
reserves ledger capacity for every envelope atomically (within one
BEGIN IMMEDIATE transaction, so the batch itself cannot oversubscribe),
and stamps the exact same parent_grant_id/parent_grant_version/
parent_grant_checksum/grant_reservation_id evidence
issue_child_authorization() already stamps -- making
hypogeum.submission_gate.submit()'s existing, UNCHANGED Step 8.5
boundary revalidation apply to proposal-activated records too, with
zero changes to submission_gate.py itself.

All fixtures are synthetic (never the real ep_f3619022 episode data).
Only hypogeum.providers.fake_provider_adapter.FakeProviderAdapter is
ever invoked -- structurally incapable of network access. No test
reads or mutates the real .hypogeum-assets/hypogeum.db.
"""

from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import hypogeum.authorization_authority as authority
import hypogeum.authorization_store as store
import hypogeum.db as db
from hypogeum.approval_artifact import sign_approval_artifact
from hypogeum.authorization_lifecycle import AuthorizationState
from tests.test_authorization_authority import _bindings, _make_env, _register_grant
from tests.test_grant_not_consumed_by_proposal_activation import (
    _make_awaiting_explicit_record,
    _make_proposal_fixture,
    _sign_proposal_artifact,
)

AT = "2026-01-01T00:00:00Z"


@pytest.fixture
def keypair(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    monkeypatch.setattr(authority, "_load_trusted_public_key", lambda: pub)
    return priv, pub


def _batch_grant(priv, db_path, *, grant_id="g_hardening", max_calls=10, max_total_spend="10.00", expires_at="2099-01-01T00:00:00Z"):
    return _register_grant(
        priv, db_path, grant_id=grant_id, expires_at=expires_at,
        bindings=_bindings(max_calls=max_calls, max_total_spend=Decimal(max_total_spend)),
        max_total_spend_all_bindings=Decimal(max_total_spend),
    )


def _one_item_proposal(db_path, *, idem_key="idem_1", authorization_id="auth_1", proposed_version=2):
    env = _make_env(idem_key=idem_key, authorization_id=authorization_id)
    checksum = _make_proposal_fixture(db_path, envs=[env], proposed_version=proposed_version)
    return env, checksum


class TestActivateAuthorizationProposalFailsClosedOnGrantProblems:

    def test_missing_grant_fails_closed_zero_mutation(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        env, checksum = _one_item_proposal(db_path)
        artifact = _sign_proposal_artifact(priv, checksum)

        with pytest.raises(authority.GrantNotFoundError):
            authority.activate_authorization_proposal(
                checksum, approval_artifact=artifact, grant_id="does_not_exist", grant_version=1, at=AT, db_path=db_path,
            )

        conn = db.get_connection(db_path)
        try:
            rec = store.get_authorization_record(conn, env.authorization_id)
            assert rec.current_state == AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION
        finally:
            conn.close()

    def test_revoked_grant_fails_closed(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _batch_grant(priv, db_path)
        authority.revoke_grant("g_hardening", 1, revoked_by="op", reason="test", at=AT, db_path=db_path)
        env, checksum = _one_item_proposal(db_path)
        artifact = _sign_proposal_artifact(priv, checksum)

        with pytest.raises(authority.GrantRevokedError):
            authority.activate_authorization_proposal(
                checksum, approval_artifact=artifact, grant_id="g_hardening", grant_version=1, at=AT, db_path=db_path,
            )

    def test_expired_grant_fails_closed(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _batch_grant(priv, db_path, expires_at="2026-01-01T00:00:00Z")  # expires exactly at AT
        env, checksum = _one_item_proposal(db_path)
        artifact = _sign_proposal_artifact(priv, checksum)

        with pytest.raises(authority.GrantExpiredError):
            authority.activate_authorization_proposal(
                checksum, approval_artifact=artifact, grant_id="g_hardening", grant_version=1, at=AT, db_path=db_path,
            )

    def test_exhausted_grant_fails_closed_zero_ledger_rows(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _batch_grant(priv, db_path, max_calls=1, max_total_spend="0.39")
        envs = [_make_env(idem_key=f"idem_{i}", authorization_id=f"auth_{i}") for i in range(2)]
        checksum = _make_proposal_fixture(db_path, envs=envs)
        artifact = _sign_proposal_artifact(priv, checksum)

        with pytest.raises(authority.GrantCapacityExhaustedError):
            authority.activate_authorization_proposal(
                checksum, approval_artifact=artifact, grant_id="g_hardening", grant_version=1, at=AT, db_path=db_path,
            )

        conn = db.get_connection(db_path)
        try:
            row = conn.execute("SELECT COUNT(*) c FROM run_authorization_grant_consumption").fetchone()
            assert row["c"] == 0  # all-or-nothing -- not even the first envelope's reservation survives
            for env in envs:
                rec = store.get_authorization_record(conn, env.authorization_id)
                assert rec.current_state == AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION
        finally:
            conn.close()

    def test_mismatched_binding_fails_closed(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(
            priv, db_path, grant_id="g_hardening",
            bindings=_bindings(provider="other_provider", model="other-model"),
        )
        env, checksum = _one_item_proposal(db_path)  # env is provider="fake", model="fake-model"
        artifact = _sign_proposal_artifact(priv, checksum)

        with pytest.raises(authority.NoMatchingGrantBindingError):
            authority.activate_authorization_proposal(
                checksum, approval_artifact=artifact, grant_id="g_hardening", grant_version=1, at=AT, db_path=db_path,
            )

    def test_envelope_cost_exceeds_binding_ceiling_fails_closed(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(
            priv, db_path, grant_id="g_hardening",
            bindings=_bindings(max_cost_per_call=Decimal("0.10")),  # env's cost is 0.39
        )
        env, checksum = _one_item_proposal(db_path)
        artifact = _sign_proposal_artifact(priv, checksum)

        with pytest.raises(authority.EnvelopeExceedsGrantLimitsError):
            authority.activate_authorization_proposal(
                checksum, approval_artifact=artifact, grant_id="g_hardening", grant_version=1, at=AT, db_path=db_path,
            )


class TestActivateAuthorizationProposalCreatesExactLedgerReservations:

    def test_batch_creates_one_reservation_per_envelope(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _batch_grant(priv, db_path)
        envs = [_make_env(idem_key=f"idem_{i}", authorization_id=f"auth_{i}") for i in range(3)]
        checksum = _make_proposal_fixture(db_path, envs=envs)
        artifact = _sign_proposal_artifact(priv, checksum)

        authority.activate_authorization_proposal(
            checksum, approval_artifact=artifact, grant_id="g_hardening", grant_version=1, at=AT, db_path=db_path,
        )

        conn = db.get_connection(db_path)
        try:
            rows = conn.execute("SELECT * FROM run_authorization_grant_consumption ORDER BY idempotency_key").fetchall()
            assert len(rows) == 3
            for row, env in zip(rows, envs):
                assert row["state"] == "reserved"
                assert row["reserved_amount"] == "0.39"
                assert row["idempotency_key"] == env.idempotency_key
                assert row["envelope_id"] == env.envelope_id
        finally:
            conn.close()


class TestConcurrentActivationCannotOversubscribeGrant:

    def test_two_racing_single_item_activations_against_capacity_for_one(self, keypair, tmp_path):
        """Grant has capacity for exactly ONE call. Two SEPARATE
        proposals (one envelope each) race, via real threads, to
        activate against the SAME grant. Exactly one must succeed;
        the other must fail closed with zero double-booking -- never
        both succeeding, never neither."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _batch_grant(priv, db_path, max_calls=1, max_total_spend="0.39")

        env_a, checksum_a = _one_item_proposal(db_path, idem_key="idem_a", authorization_id="auth_a", proposed_version=10)
        env_b, checksum_b = _one_item_proposal(db_path, idem_key="idem_b", authorization_id="auth_b", proposed_version=11)
        artifact_a = _sign_proposal_artifact(priv, checksum_a, approval_id="appr_a", nonce="nonce_a")
        artifact_b = _sign_proposal_artifact(priv, checksum_b, approval_id="appr_b", nonce="nonce_b")

        results = {}
        barrier = threading.Barrier(2)

        def _run(label, checksum, artifact):
            barrier.wait()
            try:
                authority.activate_authorization_proposal(
                    checksum, approval_artifact=artifact, grant_id="g_hardening", grant_version=1, at=AT, db_path=db_path,
                )
                results[label] = "success"
            except authority.GrantCapacityExhaustedError:
                results[label] = "capacity_exhausted"

        t1 = threading.Thread(target=_run, args=("a", checksum_a, artifact_a))
        t2 = threading.Thread(target=_run, args=("b", checksum_b, artifact_b))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert set(results.values()) == {"success", "capacity_exhausted"}, results

        conn = db.get_connection(db_path)
        try:
            rows = conn.execute("SELECT COUNT(*) c FROM run_authorization_grant_consumption").fetchone()
            assert rows["c"] == 1  # never zero, never two -- exactly one reservation survives
        finally:
            conn.close()


class TestGateRevalidationBlocksStaleGrantForProposalActivatedRecord:
    """Mirrors tests/test_submission_gate.py::TestGrantDerivedAuthorization
    Boundary exactly, but for a record produced via
    activate_authorization_proposal() instead of
    issue_child_authorization() -- proving submission_gate.py's existing,
    UNCHANGED Step 8.5 now genuinely applies to proposal-activated
    records, with zero changes to that module."""

    def _activate_via_proposal(self, harness, env, *, grant_id="g1", max_calls=5, max_total_spend=None):
        if max_total_spend is None:
            max_total_spend = env.max_cost_per_call * max_calls
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()
        authority._load_trusted_public_key = lambda: pub

        db_path = harness.tmp_path / "test.db"
        bindings = _bindings(
            provider=env.provider, model=env.model, provider_operation=env.provider_operation,
            currency=env.currency, max_cost_per_call=env.max_cost_per_call,
            max_calls=max_calls, max_total_spend=max_total_spend,
        )
        grant = _register_grant(
            priv, db_path, grant_id=grant_id, bindings=bindings, max_total_spend_all_bindings=max_total_spend,
            channel_id=env.channel_id, episode_id=env.episode_level_budget_ref,
        )
        checksum = _make_proposal_fixture(
            db_path, envs=[env], channel_id=env.channel_id, episode_id=env.episode_level_budget_ref,
        )
        artifact = _sign_proposal_artifact(priv, checksum)
        records = authority.activate_authorization_proposal(
            checksum, approval_artifact=artifact, grant_id=grant_id, grant_version=1, at=AT, db_path=db_path,
        )
        return records[0], grant

    def test_grant_revoked_after_activation_denies_at_boundary_with_zero_calls(self, tmp_path):
        from tests.test_submission_gate import GateHarness, _make_env as _sg_make_env

        harness = GateHarness(tmp_path)
        env = _sg_make_env(idempotency_key="idem_proposal_1", authorization_id="auth_proposal_1", workflow_version="1.0.0")
        harness.acquire_lease(env)
        record, grant = self._activate_via_proposal(harness, env)

        authority.revoke_grant("g1", 1, revoked_by="op", reason="test", at=AT, db_path=harness.tmp_path / "test.db")

        result = harness.submit(env, authorization=record)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        assert "03_authorization_active_and_unconsumed" in result.denied_at_check

        reservation_id = record.history[-1].evidence["grant_reservation_id"]
        reservation = store.get_ledger_reservation(harness.conn, reservation_id)
        assert reservation.state == "reserved"  # never advanced to boundary_entered

    def test_valid_grant_submission_succeeds_and_commits_ledger(self, tmp_path):
        from tests.test_submission_gate import GateHarness, _make_env as _sg_make_env

        harness = GateHarness(tmp_path)
        env = _sg_make_env(idempotency_key="idem_proposal_2", authorization_id="auth_proposal_2", workflow_version="1.0.0")
        harness.acquire_lease(env)
        record, grant = self._activate_via_proposal(harness, env)

        result = harness.submit(env, authorization=record)
        assert result.approved is True
        assert harness.adapter.invocation_count == 1

        reservation_id = record.history[-1].evidence["grant_reservation_id"]
        reservation = store.get_ledger_reservation(harness.conn, reservation_id)
        assert reservation.state == "committed"
        assert reservation.charged_amount == env.max_cost_per_call


    def test_no_write_transaction_is_open_during_the_provider_call(self, tmp_path):
        """Regression (2026-10-11): the 'reserved -> boundary_entered' UPDATE was left uncommitted
        on the lease connection, so every provider call (video generation, upscale, research) held
        the database-wide SQLite write lock for its whole duration and every other writer in the
        factory got 'database is locked'. The state must be durable BEFORE provider I/O."""
        from tests.test_submission_gate import GateHarness, _make_env as _sg_make_env

        harness = GateHarness(tmp_path)
        env = _sg_make_env(idempotency_key="idem_proposal_3", authorization_id="auth_proposal_3", workflow_version="1.0.0")
        harness.acquire_lease(env)
        record, _grant = self._activate_via_proposal(harness, env)

        seen = {}
        real_invoke = harness.adapter.invoke

        def spying_invoke(*args, **kwargs):
            seen["in_transaction"] = harness.conn.in_transaction
            reservation_id = record.history[-1].evidence["grant_reservation_id"]
            other = sqlite3.connect(str(harness.tmp_path / "test.db"))
            try:
                seen["durable_state"] = other.execute(
                    "SELECT state FROM run_authorization_grant_consumption WHERE reservation_id = ?", (reservation_id,),
                ).fetchone()[0]
            finally:
                other.close()
            return real_invoke(*args, **kwargs)

        harness.adapter.invoke = spying_invoke
        result = harness.submit(env, authorization=record)
        assert result.approved is True
        assert seen["in_transaction"] is False
        assert seen["durable_state"] == "boundary_entered"


class TestExistingGrantBoundChildAuthorizationBehaviorRemainsCompatible:
    """issue_child_authorization() itself was refactored (its grant
    resolution/validation/capacity-reservation logic extracted into the
    shared helpers activate_authorization_proposal() now also uses) --
    this proves its own external behavior/evidence shape is unchanged."""

    def test_evidence_shape_unchanged(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        grant = _register_grant(priv, db_path, grant_id="g_compat")
        env = _make_env(idem_key="idem_compat", authorization_id="auth_compat")

        record = authority.issue_child_authorization(env, "g_compat", 1, at=AT, db_path=db_path)
        assert record.current_state == AuthorizationState.AUTHORIZED
        evidence = record.history[-1].evidence
        assert set(evidence.keys()) == {
            "authorized_by", "authorization_method", "parent_grant_id",
            "parent_grant_version", "parent_grant_checksum", "grant_reservation_id",
        }
        assert evidence["authorization_method"] == "bounded_run_grant_autonomous"
        assert evidence["parent_grant_id"] == "g_compat"
        assert evidence["parent_grant_checksum"] == grant.grant_checksum

    def test_idempotent_replay_unchanged(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(priv, db_path, grant_id="g_compat2")
        env = _make_env(idem_key="idem_compat2", authorization_id="auth_compat2")

        r1 = authority.issue_child_authorization(env, "g_compat2", 1, at=AT, db_path=db_path)
        r2 = authority.issue_child_authorization(env, "g_compat2", 1, at=AT, db_path=db_path)
        assert r1.history == r2.history

    def test_capacity_exhaustion_unchanged(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(priv, db_path, grant_id="g_compat3", bindings=_bindings(max_calls=1, max_total_spend=Decimal("0.39")))
        env1 = _make_env(idem_key="idem_compat3a", authorization_id="auth_compat3a")
        env2 = _make_env(idem_key="idem_compat3b", authorization_id="auth_compat3b")

        authority.issue_child_authorization(env1, "g_compat3", 1, at=AT, db_path=db_path)
        with pytest.raises(authority.GrantCapacityExhaustedError):
            authority.issue_child_authorization(env2, "g_compat3", 1, at=AT, db_path=db_path)
