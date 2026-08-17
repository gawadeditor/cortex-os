"""
tests/test_grant_scope_binding.py — Closes the one remaining
authorization-security gap found by the forensic audit of commit
8ba33a3: neither issue_child_authorization() nor activate_authorization
_proposal() verified that a RunAuthorizationGrant's own channel_id/
episode_id/workflow identity matched the authorization target's --
only provider/model/operation/currency/cost bindings were checked. A
grant registered for one channel/episode/workflow could therefore have
authorized a target belonging to a completely different one, as long
as the provider/model/operation/cost/currency happened to match.

hypogeum.authorization_authority._validate_grant_scope_binding() is
the ONE shared fail-closed check both authorization-issuance routes
now call, before any ledger reservation or authorization state
transition -- never two divergent implementations. All fixtures are
synthetic (never the real ep_f3619022 episode data). Only
hypogeum.providers.fake_provider_adapter.FakeProviderAdapter is ever
invoked (indirectly, via issue_child_authorization()/activate_
authorization_proposal(), which never touch a provider adapter at
all) -- no test makes a network call or mutates the real
.hypogeum-assets/hypogeum.db.
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import hypogeum.authorization_authority as authority
import hypogeum.authorization_store as store
import hypogeum.db as db
from hypogeum.authorization_lifecycle import AuthorizationState
from tests.test_authorization_authority import _bindings, _make_env, _register_grant
from tests.test_grant_not_consumed_by_proposal_activation import (
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


def _grant(priv, db_path, *, grant_id="g1", channel_id="test_channel", episode_id="ep_test",
           max_calls=10, max_total_spend="10.00"):
    return _register_grant(
        priv, db_path, grant_id=grant_id, channel_id=channel_id, episode_id=episode_id,
        bindings=_bindings(max_calls=max_calls, max_total_spend=Decimal(max_total_spend)),
        max_total_spend_all_bindings=Decimal(max_total_spend),
    )


def _ledger_row_count(db_path):
    conn = db.get_connection(db_path)
    try:
        return conn.execute("SELECT COUNT(*) c FROM run_authorization_grant_consumption").fetchone()["c"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Exact matching channel/episode/run(workflow) succeeds -- issue_child_authorization()
# ---------------------------------------------------------------------------

class TestExactMatchSucceeds:

    def test_issue_child_authorization_succeeds_with_exact_scope_match(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a")
        env = _make_env(channel_id="ch_a", episode_id="ep_a", workflow_version="1.0.0")

        record = authority.issue_child_authorization(env, "g1", 1, at=AT, db_path=db_path)
        assert record.current_state == AuthorizationState.AUTHORIZED
        assert _ledger_row_count(db_path) == 1

    def test_activate_authorization_proposal_succeeds_with_exact_scope_match(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a")
        env = _make_env(channel_id="ch_a", episode_id="ep_a", workflow_version="1.0.0")
        checksum = _make_proposal_fixture(db_path, envs=[env], channel_id="ch_a", episode_id="ep_a")
        artifact = _sign_proposal_artifact(priv, checksum)

        records = authority.activate_authorization_proposal(
            checksum, approval_artifact=artifact, grant_id="g1", grant_version=1, at=AT, db_path=db_path,
        )
        assert records[0].current_state == AuthorizationState.AUTHORIZED
        assert _ledger_row_count(db_path) == 1


# ---------------------------------------------------------------------------
# 2/3/4. Wrong channel / episode / run(workflow) fails, zero mutation
# ---------------------------------------------------------------------------

class TestWrongScopeFailsClosedZeroMutation:

    def test_issue_child_authorization_wrong_channel_fails_zero_ledger_zero_transition(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a")
        env = _make_env(channel_id="ch_WRONG", episode_id="ep_a", workflow_version="1.0.0")

        with pytest.raises(authority.GrantScopeMismatchError):
            authority.issue_child_authorization(env, "g1", 1, at=AT, db_path=db_path)

        assert _ledger_row_count(db_path) == 0
        conn = db.get_connection(db_path)
        try:
            assert store.get_authorization_record(conn, env.authorization_id) is None  # never created
        finally:
            conn.close()

    def test_issue_child_authorization_wrong_episode_fails_zero_ledger_zero_transition(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a")
        env = _make_env(channel_id="ch_a", episode_id="ep_WRONG", workflow_version="1.0.0")

        with pytest.raises(authority.GrantScopeMismatchError):
            authority.issue_child_authorization(env, "g1", 1, at=AT, db_path=db_path)

        assert _ledger_row_count(db_path) == 0
        conn = db.get_connection(db_path)
        try:
            assert store.get_authorization_record(conn, env.authorization_id) is None
        finally:
            conn.close()

    def test_issue_child_authorization_wrong_workflow_version_fails(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a")
        env = _make_env(channel_id="ch_a", episode_id="ep_a", workflow_version="9.9.9")

        with pytest.raises(authority.GrantScopeMismatchError):
            authority.issue_child_authorization(env, "g1", 1, at=AT, db_path=db_path)
        assert _ledger_row_count(db_path) == 0

    def test_activate_authorization_proposal_wrong_channel_fails_zero_mutation(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a")
        env = _make_env(channel_id="ch_a", episode_id="ep_a", workflow_version="1.0.0")
        checksum = _make_proposal_fixture(db_path, envs=[env], channel_id="ch_WRONG", episode_id="ep_a")
        artifact = _sign_proposal_artifact(priv, checksum)

        with pytest.raises(authority.GrantScopeMismatchError):
            authority.activate_authorization_proposal(
                checksum, approval_artifact=artifact, grant_id="g1", grant_version=1, at=AT, db_path=db_path,
            )
        assert _ledger_row_count(db_path) == 0
        conn = db.get_connection(db_path)
        try:
            rec = store.get_authorization_record(conn, env.authorization_id)
            assert rec.current_state == AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION
        finally:
            conn.close()

    def test_activate_authorization_proposal_wrong_episode_fails_zero_mutation(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a")
        env = _make_env(channel_id="ch_a", episode_id="ep_a", workflow_version="1.0.0")
        checksum = _make_proposal_fixture(db_path, envs=[env], channel_id="ch_a", episode_id="ep_WRONG")
        artifact = _sign_proposal_artifact(priv, checksum)

        with pytest.raises(authority.GrantScopeMismatchError):
            authority.activate_authorization_proposal(
                checksum, approval_artifact=artifact, grant_id="g1", grant_version=1, at=AT, db_path=db_path,
            )
        assert _ledger_row_count(db_path) == 0
        conn = db.get_connection(db_path)
        try:
            rec = store.get_authorization_record(conn, env.authorization_id)
            assert rec.current_state == AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION
        finally:
            conn.close()

    def test_activate_authorization_proposal_wrong_workflow_id_fails(self, keypair, tmp_path):
        """The proposal-level check has full workflow identity
        (workflow_id + workflow_checksum), unlike a bare envelope --
        exercised here since it is only representable at this level."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a")
        env = _make_env(channel_id="ch_a", episode_id="ep_a", workflow_version="1.0.0")
        checksum = _make_proposal_fixture(db_path, envs=[env], channel_id="ch_a", episode_id="ep_a")

        # Tamper the persisted proposal's own workflow_id after the fact,
        # to a value that differs from the grant's "wf" -- simulates a
        # proposal genuinely compiled against a different workflow.
        conn = db.get_connection(db_path)
        try:
            conn.execute(
                "UPDATE authorization_proposals SET workflow_id = 'a_different_workflow' WHERE proposal_checksum = ?",
                (checksum,),
            )
            conn.commit()
        finally:
            conn.close()

        artifact = _sign_proposal_artifact(priv, checksum)
        with pytest.raises(authority.GrantScopeMismatchError):
            authority.activate_authorization_proposal(
                checksum, approval_artifact=artifact, grant_id="g1", grant_version=1, at=AT, db_path=db_path,
            )
        assert _ledger_row_count(db_path) == 0


# ---------------------------------------------------------------------------
# 5/6. Missing channel/episode evidence fails closed
# ---------------------------------------------------------------------------

class TestMissingScopeEvidenceFailsClosed:
    """GenerationExecutionEnvelope.create() itself already refuses an
    empty channel_id/episode_level_budget_ref (schema validation), so
    a real envelope can never actually reach _validate_grant_scope_
    binding() with missing evidence -- these tests exercise the shared
    function's own contract directly, proving it fails closed rather
    than silently skipping the check, independent of whether today's
    envelope schema happens to make that state reachable."""

    def test_missing_channel_id_fails_closed(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        grant = _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a")
        with pytest.raises(authority.GrantScopeMismatchError, match="channel_id"):
            authority._validate_grant_scope_binding(
                grant, channel_id=None, episode_id="ep_a", workflow_version="1.0.0",
                target_description="synthetic target",
            )
        with pytest.raises(authority.GrantScopeMismatchError, match="channel_id"):
            authority._validate_grant_scope_binding(
                grant, channel_id="", episode_id="ep_a", workflow_version="1.0.0",
                target_description="synthetic target",
            )

    def test_missing_episode_id_fails_closed(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        grant = _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a")
        with pytest.raises(authority.GrantScopeMismatchError, match="episode_id"):
            authority._validate_grant_scope_binding(
                grant, channel_id="ch_a", episode_id=None, workflow_version="1.0.0",
                target_description="synthetic target",
            )
        with pytest.raises(authority.GrantScopeMismatchError, match="episode_id"):
            authority._validate_grant_scope_binding(
                grant, channel_id="ch_a", episode_id="", workflow_version="1.0.0",
                target_description="synthetic target",
            )

    def test_missing_workflow_version_fails_closed(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        grant = _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a")
        with pytest.raises(authority.GrantScopeMismatchError, match="workflow_version"):
            authority._validate_grant_scope_binding(
                grant, channel_id="ch_a", episode_id="ep_a", workflow_version=None,
                target_description="synthetic target",
            )

    def test_proposal_with_empty_episode_id_fails_closed_end_to_end(self, keypair, tmp_path):
        """Integration-level: AuthorizationProposal itself carries no
        constructor validation, so an empty episode_id CAN be persisted
        -- proving activate_authorization_proposal() itself, not just
        the low-level helper, fails closed on it, with zero mutation."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a")
        env = _make_env(channel_id="ch_a", episode_id="ep_a", workflow_version="1.0.0")
        checksum = _make_proposal_fixture(db_path, envs=[env], channel_id="ch_a", episode_id="ep_a")

        conn = db.get_connection(db_path)
        try:
            conn.execute(
                "UPDATE authorization_proposals SET episode_id = '' WHERE proposal_checksum = ?", (checksum,),
            )
            conn.commit()
        finally:
            conn.close()

        artifact = _sign_proposal_artifact(priv, checksum)
        with pytest.raises(authority.GrantScopeMismatchError, match="episode_id"):
            authority.activate_authorization_proposal(
                checksum, approval_artifact=artifact, grant_id="g1", grant_version=1, at=AT, db_path=db_path,
            )
        assert _ledger_row_count(db_path) == 0


# ---------------------------------------------------------------------------
# 7/8/9. Batch-level: one mismatched envelope, mixed-channel, mixed-episode
# ---------------------------------------------------------------------------

class TestBatchLevelMismatchRollsBackTheWholeBatch:

    def test_one_mismatched_envelope_rolls_back_the_entire_batch(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a")
        good1 = _make_env(idem_key="idem_g1", authorization_id="auth_g1", channel_id="ch_a", episode_id="ep_a", workflow_version="1.0.0")
        good2 = _make_env(idem_key="idem_g2", authorization_id="auth_g2", channel_id="ch_a", episode_id="ep_a", workflow_version="1.0.0")
        bad = _make_env(idem_key="idem_bad", authorization_id="auth_bad", channel_id="ch_a", episode_id="ep_WRONG", workflow_version="1.0.0")
        checksum = _make_proposal_fixture(db_path, envs=[good1, good2, bad], channel_id="ch_a", episode_id="ep_a")
        artifact = _sign_proposal_artifact(priv, checksum)

        with pytest.raises(authority.GrantScopeMismatchError):
            authority.activate_authorization_proposal(
                checksum, approval_artifact=artifact, grant_id="g1", grant_version=1, at=AT, db_path=db_path,
            )

        assert _ledger_row_count(db_path) == 0  # not even the two good envelopes got a reservation
        conn = db.get_connection(db_path)
        try:
            for env in (good1, good2, bad):
                rec = store.get_authorization_record(conn, env.authorization_id)
                assert rec.current_state == AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION
        finally:
            conn.close()

    def test_mixed_channel_proposal_fails_closed(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a")
        good = _make_env(idem_key="idem_mc1", authorization_id="auth_mc1", channel_id="ch_a", episode_id="ep_a", workflow_version="1.0.0")
        mixed = _make_env(idem_key="idem_mc2", authorization_id="auth_mc2", channel_id="ch_DIFFERENT", episode_id="ep_a", workflow_version="1.0.0")
        checksum = _make_proposal_fixture(db_path, envs=[good, mixed], channel_id="ch_a", episode_id="ep_a")
        artifact = _sign_proposal_artifact(priv, checksum)

        with pytest.raises(authority.GrantScopeMismatchError):
            authority.activate_authorization_proposal(
                checksum, approval_artifact=artifact, grant_id="g1", grant_version=1, at=AT, db_path=db_path,
            )
        assert _ledger_row_count(db_path) == 0

    def test_mixed_episode_proposal_fails_closed(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a")
        good = _make_env(idem_key="idem_me1", authorization_id="auth_me1", channel_id="ch_a", episode_id="ep_a", workflow_version="1.0.0")
        mixed = _make_env(idem_key="idem_me2", authorization_id="auth_me2", channel_id="ch_a", episode_id="ep_DIFFERENT", workflow_version="1.0.0")
        checksum = _make_proposal_fixture(db_path, envs=[good, mixed], channel_id="ch_a", episode_id="ep_a")
        artifact = _sign_proposal_artifact(priv, checksum)

        with pytest.raises(authority.GrantScopeMismatchError):
            authority.activate_authorization_proposal(
                checksum, approval_artifact=artifact, grant_id="g1", grant_version=1, at=AT, db_path=db_path,
            )
        assert _ledger_row_count(db_path) == 0


# ---------------------------------------------------------------------------
# 10. Existing provider/model/currency/cost checks remain intact
# ---------------------------------------------------------------------------

class TestExistingBindingChecksRemainIntact:

    def test_mismatched_provider_model_still_fails_with_matching_scope(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(
            priv, db_path, grant_id="g1", channel_id="ch_a", episode_id="ep_a",
            bindings=_bindings(provider="other_provider", model="other-model"),
        )
        env = _make_env(channel_id="ch_a", episode_id="ep_a", workflow_version="1.0.0")  # scope matches; binding doesn't

        with pytest.raises(authority.NoMatchingGrantBindingError):
            authority.issue_child_authorization(env, "g1", 1, at=AT, db_path=db_path)

    def test_cost_exceeding_ceiling_still_fails_with_matching_scope(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(
            priv, db_path, grant_id="g1", channel_id="ch_a", episode_id="ep_a",
            bindings=_bindings(max_cost_per_call=Decimal("0.10")),
        )
        env = _make_env(channel_id="ch_a", episode_id="ep_a", workflow_version="1.0.0", cost="0.39")

        with pytest.raises(authority.EnvelopeExceedsGrantLimitsError):
            authority.issue_child_authorization(env, "g1", 1, at=AT, db_path=db_path)

    def test_valid_scope_and_valid_binding_together_succeed(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a")
        env = _make_env(channel_id="ch_a", episode_id="ep_a", workflow_version="1.0.0")
        record = authority.issue_child_authorization(env, "g1", 1, at=AT, db_path=db_path)
        assert record.current_state == AuthorizationState.AUTHORIZED


# ---------------------------------------------------------------------------
# 11. Concurrent activation still cannot oversubscribe the Grant
# ---------------------------------------------------------------------------

class TestConcurrentActivationStillCannotOversubscribe:

    def test_two_racing_matching_scope_activations_against_capacity_for_one(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _grant(priv, db_path, channel_id="ch_a", episode_id="ep_a", max_calls=1, max_total_spend="0.39")

        env_a = _make_env(idem_key="idem_race_a", authorization_id="auth_race_a", channel_id="ch_a", episode_id="ep_a", workflow_version="1.0.0")
        env_b = _make_env(idem_key="idem_race_b", authorization_id="auth_race_b", channel_id="ch_a", episode_id="ep_a", workflow_version="1.0.0")
        checksum_a = _make_proposal_fixture(db_path, envs=[env_a], proposed_version=20, channel_id="ch_a", episode_id="ep_a")
        checksum_b = _make_proposal_fixture(db_path, envs=[env_b], proposed_version=21, channel_id="ch_a", episode_id="ep_a")
        artifact_a = _sign_proposal_artifact(priv, checksum_a, approval_id="appr_race_a", nonce="nonce_race_a")
        artifact_b = _sign_proposal_artifact(priv, checksum_b, approval_id="appr_race_b", nonce="nonce_race_b")

        results = {}
        barrier = threading.Barrier(2)

        def _run(label, checksum, artifact):
            barrier.wait()
            try:
                authority.activate_authorization_proposal(
                    checksum, approval_artifact=artifact, grant_id="g1", grant_version=1, at=AT, db_path=db_path,
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
        assert _ledger_row_count(db_path) == 1  # never 0, never 2
