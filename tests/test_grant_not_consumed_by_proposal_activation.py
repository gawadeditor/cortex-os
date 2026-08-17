"""
tests/test_grant_not_consumed_by_proposal_activation.py — Structural
proof of a real, documented architectural gap discovered on the
ep_f3619022 v4 replacement batch (2026-08-17): a RunAuthorizationGrant
(grant_ep_f3619022_v4_replacement) was registered via hypogeum.
authorization_authority.register_run_grant() alongside that batch, but
the batch's 85 AuthorizationRecords were AUTHORIZED via hypogeum.
authorization_authority.activate_authorization_proposal() -- a
DIFFERENT authorization-issuance path than issue_child_authorization(),
the one path that actually binds a grant. As a direct, structural
consequence:
  - the AUTHORIZED-transition evidence produced by
    activate_authorization_proposal() never carries parent_grant_id/
    parent_grant_version/parent_grant_checksum/grant_reservation_id;
  - hypogeum.submission_gate._grant_binding_evidence() therefore
    returns None for every such record;
  - hypogeum.submission_gate.submit() never calls
    revalidate_grant_reservation() or _settle_grant_reservation() for
    any of them;
  - the grant's own ledger (run_authorization_grant_consumption) never
    accumulates a row for any of the batch's real calls.

This is documented (not fixed) in hypogeum.submission_gate.
_grant_binding_evidence()'s own docstring and hypogeum.
authorization_authority.activate_authorization_proposal()'s own
docstring -- this file proves it structurally, generically, using
synthetic fixtures (never the real episode's own data), and contrasts
it against issue_child_authorization()'s own grant-bound path to prove
the distinction is real, not merely "always returns None regardless
of how a record was authorized."

No test here makes a network call, mutates the real
.hypogeum-assets/hypogeum.db, or resumes/repairs anything -- this is
diagnostic-only, exactly mirroring the real incident's own resolution:
documented and tested, never repaired by direct database mutation.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import hypogeum.authorization_authority as authority
import hypogeum.authorization_store as store
import hypogeum.db as db
import hypogeum.submission_gate as gate
from hypogeum.approval_artifact import sign_approval_artifact
from tests.test_authorization_authority import _bindings, _make_env, _register_grant

AT = "2026-01-01T00:00:00Z"


@pytest.fixture
def keypair(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    monkeypatch.setattr(authority, "_load_trusted_public_key", lambda: pub)
    return priv, pub


def _make_awaiting_explicit_record(db_path, authorization_id, env):
    conn = db.get_connection(db_path)
    try:
        store.ensure_tables(conn)
        rec = authority.AuthorizationRecord.create(
            authorization_id=authorization_id, authorization_version=env.authorization_version,
            envelope_id=env.envelope_id, envelope_version=env.envelope_version,
        )
        rec = rec.transition(authority.AuthorizationState.COMPILED, evidence={"compiler_identity": "x", "compiler_version": "1"}, at=AT)
        rec = rec.transition(authority.AuthorizationState.AWAITING_AUTHORIZATION, evidence={"preflight_result": "ok"}, at=AT)
        rec = rec.transition(
            authority.AuthorizationState.AWAITING_EXPLICIT_AUTHORIZATION,
            evidence={"observation_only_pass_count": 1, "observation_only_total_count": 1, "proposal_artifact_checksum": "z" * 64},
            at=AT,
        )
        store.persist_execution_envelope(conn, env)
        store.persist_authorization_record(conn, rec)
        conn.commit()
    finally:
        conn.close()
    return rec


def _make_proposal_fixture(db_path, *, envs, proposed_version=2):
    for env in envs:
        _make_awaiting_explicit_record(db_path, env.authorization_id, env)
    authorization_ids = [env.authorization_id for env in envs]
    bindings = [("fake", "fake-model", "video.text_to_video")]
    checksum = store.compute_proposal_checksum(
        channel_id="test_channel", episode_id="ep_test", proposed_version=proposed_version, superseded_version=1,
        workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64,
        channel_package_version="1.0", channel_package_checksum="b" * 64,
        authorization_ids=authorization_ids, provider_model_operation_bindings=bindings,
        max_cost_per_call_ceiling=Decimal("0.39"), max_total_spend=Decimal("0.39") * len(envs), currency="USD",
    )
    proposal = store.AuthorizationProposal(
        proposal_id=f"proposal_ep_test_v{proposed_version}", channel_id="test_channel", episode_id="ep_test",
        proposed_version=proposed_version, superseded_version=1, workflow_id="wf", workflow_version="1.0.0",
        workflow_checksum="a" * 64, channel_package_version="1.0", channel_package_checksum="b" * 64,
        authorization_ids=authorization_ids, provider_model_operation_bindings=bindings,
        max_cost_per_call_ceiling=Decimal("0.39"), max_total_spend=Decimal("0.39") * len(envs), currency="USD",
        created_at=AT, proposal_checksum=checksum,
    )
    conn = db.get_connection(db_path)
    try:
        store.persist_authorization_proposal(conn, proposal)
        conn.commit()
    finally:
        conn.close()
    return checksum


def _sign_proposal_artifact(priv, checksum, *, approval_id="proposal_appr_1", nonce="nonce_prop_1"):
    fields = dict(
        approval_id=approval_id, approval_version=1, approval_type="authorization_proposal",
        subject_checksum=checksum, channel_id="test_channel", scope_id="ep_test",
        workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64,
        channel_package_version="1.0", channel_package_checksum="b" * 64, allowed_bindings=(),
        currency="USD", max_cost_per_call=Decimal("0.39"), max_total_spend=Decimal("100"), max_calls=100,
        retry_authorized=False, fallback_authorized=False, issued_at=AT, expires_at="2099-01-01T00:00:00Z",
        approver_identity="human_operator", nonce=nonce,
    )
    return sign_approval_artifact(fields, private_key=priv)


class TestProposalActivationNeverCarriesGrantEvidence:

    def test_authorized_evidence_has_no_grant_fields(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        envs = [_make_env(idem_key=f"idem_{i}", authorization_id=f"auth_{i}") for i in range(3)]
        checksum = _make_proposal_fixture(db_path, envs=envs)
        artifact = _sign_proposal_artifact(priv, checksum)

        records = authority.activate_authorization_proposal(checksum, approval_artifact=artifact, at=AT, db_path=db_path)
        assert len(records) == 3
        for rec in records:
            evidence = rec.history[-1].evidence
            assert evidence["authorization_method"] == "explicit_human_approval_proposal_batch"
            for grant_field in ("parent_grant_id", "parent_grant_version", "parent_grant_checksum", "grant_reservation_id"):
                assert grant_field not in evidence

    def test_grant_binding_evidence_returns_none_for_proposal_activated_records(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        env = _make_env(idem_key="idem_solo", authorization_id="auth_solo")
        checksum = _make_proposal_fixture(db_path, envs=[env])
        artifact = _sign_proposal_artifact(priv, checksum)

        records = authority.activate_authorization_proposal(checksum, approval_artifact=artifact, at=AT, db_path=db_path)
        assert gate._grant_binding_evidence(records[0]) is None

    def test_a_grant_registered_alongside_is_never_consumed_by_the_batch(self, keypair, tmp_path):
        """The exact real-incident shape: a grant exists, durably and
        validly registered, but the batch activated via
        activate_authorization_proposal() never touches its ledger."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(priv, db_path, grant_id="g_replacement")

        env = _make_env(idem_key="idem_solo2", authorization_id="auth_solo2")
        checksum = _make_proposal_fixture(db_path, envs=[env], proposed_version=3)
        artifact = _sign_proposal_artifact(priv, checksum, approval_id="proposal_appr_2", nonce="nonce_prop_2")
        authority.activate_authorization_proposal(checksum, approval_artifact=artifact, at=AT, db_path=db_path)

        conn = db.get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) c FROM run_authorization_grant_consumption WHERE grant_id = ?", ("g_replacement",),
            ).fetchone()
            assert row["c"] == 0
        finally:
            conn.close()


class TestIssueChildAuthorizationContrastsByActuallyBindingAGrant:
    """Proves the distinction in TestProposalActivationNeverCarriesGrant
    Evidence is real -- the OTHER authorization-issuance path DOES
    carry grant evidence, and _grant_binding_evidence() DOES return it,
    for a record produced that way. If this test were failing (e.g.
    _grant_binding_evidence() always returned None regardless of
    input), the tests above would be meaningless."""

    def test_grant_bound_record_carries_full_grant_evidence(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        grant = _register_grant(priv, db_path, grant_id="g_bound")
        env = _make_env(idem_key="idem_bound", authorization_id="auth_bound")

        record = authority.issue_child_authorization(env, "g_bound", 1, at=AT, db_path=db_path)
        evidence = gate._grant_binding_evidence(record)
        assert evidence is not None
        assert evidence["parent_grant_id"] == "g_bound"
        assert evidence["parent_grant_checksum"] == grant.grant_checksum
        assert "grant_reservation_id" in evidence
