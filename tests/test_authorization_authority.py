"""
tests/test_authorization_authority.py — Focused tests for
hypogeum/authorization_authority.py: verified grant registration,
child-authorization issuance (the 7 fail-closed cases from
requirement 7), idempotency, and the one-off explicit-approval path.

Every test monkeypatches _load_trusted_public_key() to a test keypair
-- production code has no parameter anywhere on this module's public
API that would let a caller substitute a different "trusted" key.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import hypogeum.authorization_authority as authority
import hypogeum.authorization_store as store
from hypogeum.approval_artifact import GrantBindingApproval, sign_approval_artifact
from hypogeum.execution_envelope import GenerationExecutionEnvelope

AT = "2026-01-01T00:00:00Z"


@pytest.fixture
def keypair(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    monkeypatch.setattr(authority, "_load_trusted_public_key", lambda: pub)
    return priv, pub


def _bindings(**overrides):
    base = dict(
        provider="fake", model="fake-model", provider_operation="video.text_to_video",
        currency="USD", max_cost_per_call=Decimal("0.39"), max_calls=2, max_total_spend=Decimal("1.00"),
    )
    base.update(overrides)
    return [store.GrantBinding(**base)]


def _sign_grant_artifact(priv, *, channel_id="test_channel", episode_id="ep_test", bindings=None,
                          expires_at="2099-01-01T00:00:00Z", **overrides):
    bindings = bindings or _bindings()
    checksum = authority.compute_grant_checksum(
        channel_id=channel_id, episode_id=episode_id, workflow_id="wf", workflow_version="1.0.0",
        workflow_checksum="a" * 64, bindings=bindings, max_total_spend_all_bindings=Decimal("1.00"),
        currency="USD", expires_at=expires_at,
    )
    fields = dict(
        approval_id="appr1", approval_version=1, approval_type="grant", subject_checksum=checksum,
        channel_id=channel_id, scope_id=episode_id, workflow_id="wf", workflow_version="1.0.0",
        workflow_checksum="a" * 64, channel_package_version="1.0", channel_package_checksum="b" * 64,
        allowed_bindings=tuple(GrantBindingApproval(b.provider, b.model, b.provider_operation) for b in bindings),
        currency="USD", max_cost_per_call=Decimal("0.39"), max_total_spend=Decimal("1.00"), max_calls=2,
        retry_authorized=False, fallback_authorized=False, issued_at=AT, expires_at=expires_at,
        approver_identity="operator_x", nonce="nonce_grant_1",
    )
    fields.update(overrides)
    return sign_approval_artifact(fields, private_key=priv), checksum, bindings


def _register_grant(priv, db_path, *, channel_id="test_channel", episode_id="ep_test", bindings=None,
                     grant_id="g1", expires_at="2099-01-01T00:00:00Z"):
    bindings = bindings or _bindings()
    artifact, checksum, bindings = _sign_grant_artifact(
        priv, channel_id=channel_id, episode_id=episode_id, bindings=bindings, expires_at=expires_at,
    )
    return authority.register_run_grant(
        grant_id=grant_id, grant_version=1, channel_id=channel_id, episode_id=episode_id,
        workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64, bindings=bindings,
        max_total_spend_all_bindings=Decimal("1.00"), currency="USD", expires_at=expires_at,
        approval_artifact=artifact, db_path=db_path,
    )


def _make_env(*, channel_id="test_channel", provider="fake", model="fake-model",
              provider_operation="video.text_to_video", cost="0.39", idem_key="idem_1",
              authorization_id="auth_1"):
    prompt = f"a compiled prompt for {idem_key}"
    return GenerationExecutionEnvelope.create(
        envelope_schema_version="1.0", envelope_version=1, channel_id=channel_id,
        channel_package_version="1.0", channel_package_checksum="b" * 64, execution_id="ep_test",
        workflow_version="1.0.0", plan_version="1.0", plan_checksum="c" * 64, planned_shot_id="shot_1",
        structured_spec_checksum="d" * 64, compiler_identity="pkg.compile", compiler_version="v1",
        operation_payload_content=prompt, operation_payload_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        operation_payload_schema_version="video.text_to_video/1.0",
        content_provenance={
            "compiler": "pkg.compile", "compiler_version": "v1", "source_work_item_id": "shot_1", "rendered_at": AT,
        },
        provider=provider, model=model, provider_operation=provider_operation,
        deterministic_output_path=f"/tmp/{idem_key}.mp4", expected_output_identity=f"asset_{idem_key}",
        authorization_id=authorization_id, authorization_version=1, max_cost_per_call=Decimal(cost),
        run_level_budget_ref="run_1", episode_level_budget_ref="ep_test", currency="USD",
        retry_authorized=False, fallback_authorized=False, idempotency_key=idem_key, created_at=AT,
        expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
        dependency_checksums={"channel_package": "b" * 64, "plan": "c" * 64, "structured_spec": "d" * 64, "compiler": "e" * 64},
    )


class TestRegisterRunGrantRequiresVerifiedApproval:

    def test_valid_artifact_registers(self, keypair, tmp_path):
        priv, _pub = keypair
        grant = _register_grant(priv, tmp_path / "auth.db")
        assert grant.status == "active"

    def test_wrong_keypair_rejected(self, tmp_path, monkeypatch):
        priv = Ed25519PrivateKey.generate()
        wrong_pub = Ed25519PrivateKey.generate().public_key()
        monkeypatch.setattr(authority, "_load_trusted_public_key", lambda: wrong_pub)
        with pytest.raises(authority.InvalidApprovalSignatureError):
            _register_grant(priv, tmp_path / "auth.db")

    def test_tampered_artifact_rejected(self, keypair, tmp_path):
        import dataclasses
        priv, _pub = keypair
        artifact, checksum, bindings = _sign_grant_artifact(priv)
        tampered = dataclasses.replace(artifact, max_calls=999)
        with pytest.raises(authority.InvalidApprovalSignatureError):
            authority.register_run_grant(
                grant_id="g1", grant_version=1, channel_id="test_channel", episode_id="ep_test",
                workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64, bindings=bindings,
                max_total_spend_all_bindings=Decimal("1.00"), currency="USD", expires_at="2099-01-01T00:00:00Z",
                approval_artifact=tampered, db_path=tmp_path / "auth.db",
            )

    def test_subject_checksum_mismatch_rejected(self, keypair, tmp_path):
        priv, _pub = keypair
        artifact, _checksum, bindings = _sign_grant_artifact(priv, subject_checksum="0" * 64)
        with pytest.raises(authority.ApprovalSubjectMismatchError):
            authority.register_run_grant(
                grant_id="g1", grant_version=1, channel_id="test_channel", episode_id="ep_test",
                workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64, bindings=bindings,
                max_total_spend_all_bindings=Decimal("1.00"), currency="USD", expires_at="2099-01-01T00:00:00Z",
                approval_artifact=artifact, db_path=tmp_path / "auth.db",
            )

    def test_wrong_approval_type_rejected(self, keypair, tmp_path):
        priv, _pub = keypair
        bindings = _bindings()
        checksum = authority.compute_grant_checksum(
            channel_id="test_channel", episode_id="ep_test", workflow_id="wf", workflow_version="1.0.0",
            workflow_checksum="a" * 64, bindings=bindings, max_total_spend_all_bindings=Decimal("1.00"),
            currency="USD", expires_at="2099-01-01T00:00:00Z",
        )
        artifact, _c, _b = _sign_grant_artifact(priv, approval_type="authorization_proposal", subject_checksum=checksum)
        with pytest.raises(authority.ApprovalTypeMismatchError):
            authority.register_run_grant(
                grant_id="g1", grant_version=1, channel_id="test_channel", episode_id="ep_test",
                workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64, bindings=bindings,
                max_total_spend_all_bindings=Decimal("1.00"), currency="USD", expires_at="2099-01-01T00:00:00Z",
                approval_artifact=artifact, db_path=tmp_path / "auth.db",
            )

    def test_replayed_nonce_rejected(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(priv, db_path, grant_id="g1")
        # Second grant, DIFFERENT payload, but artifact re-signed with the
        # SAME nonce as the first one's artifact -- must be rejected even
        # though the signature itself is genuinely valid.
        artifact, checksum, bindings = _sign_grant_artifact(priv, episode_id="ep_other")
        import dataclasses
        replayed = dataclasses.replace(artifact, nonce="nonce_grant_1")  # same nonce as g1's artifact
        with pytest.raises(store.ApprovalReplayError):
            authority.register_run_grant(
                grant_id="g2", grant_version=1, channel_id="test_channel", episode_id="ep_other",
                workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64, bindings=bindings,
                max_total_spend_all_bindings=Decimal("1.00"), currency="USD", expires_at="2099-01-01T00:00:00Z",
                approval_artifact=replayed, db_path=db_path,
            )


class TestResolveActiveGrant:

    def test_resolves_verified_active_grant(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(priv, db_path)
        resolved = authority.resolve_active_grant("test_channel", "ep_test", at=AT, db_path=db_path)
        assert resolved is not None
        assert resolved.grant_id == "g1"

    def test_no_grant_for_scope_returns_none(self, tmp_path):
        assert authority.resolve_active_grant("nobody", "nowhere", at=AT, db_path=tmp_path / "auth.db") is None

    def test_expired_grant_returns_none(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(priv, db_path, expires_at="2026-01-01T00:00:00Z")
        assert authority.resolve_active_grant("test_channel", "ep_test", at="2026-06-01T00:00:00Z", db_path=db_path) is None

    def test_hand_inserted_row_without_verified_artifact_is_unusable(self, tmp_path):
        """A grant row inserted by direct SQL (bypassing register_run_
        grant() entirely) has no matching verified approval_artifacts
        row -- resolve_active_grant() must treat it as unusable."""
        import sqlite3
        db_path = tmp_path / "auth.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        store.insert_grant(
            conn, grant_id="forged", grant_version=1, grant_checksum="c" * 64, channel_id="test_channel",
            episode_id="ep_test", workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64,
            bindings=_bindings(), max_total_spend_all_bindings=Decimal("1.00"), currency="USD",
            expires_at="2099-01-01T00:00:00Z", approval_id="never_signed", approval_version=1,
        )
        conn.close()
        assert authority.resolve_active_grant("test_channel", "ep_test", at=AT, db_path=db_path) is None


class TestIssueChildAuthorizationFailClosedCases:
    """The 7 fail-closed cases from requirement 7."""

    def test_missing_grant(self, keypair, tmp_path):
        env = _make_env()
        with pytest.raises(authority.GrantNotFoundError):
            authority.issue_child_authorization(env, "no-such-grant", 1, at=AT, db_path=tmp_path / "auth.db")

    def test_revoked_grant(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(priv, db_path)
        authority.revoke_grant("g1", 1, revoked_by="x", reason="test", at=AT, db_path=db_path)
        env = _make_env()
        with pytest.raises(authority.GrantRevokedError):
            authority.issue_child_authorization(env, "g1", 1, at=AT, db_path=db_path)

    def test_expired_grant(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(priv, db_path, expires_at="2026-01-01T00:00:00Z")
        env = _make_env()
        with pytest.raises(authority.GrantExpiredError):
            authority.issue_child_authorization(env, "g1", 1, at="2026-06-01T00:00:00Z", db_path=db_path)

    def test_mismatched_binding(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(priv, db_path)
        env = _make_env(provider="totally-different-provider")
        with pytest.raises(authority.NoMatchingGrantBindingError):
            authority.issue_child_authorization(env, "g1", 1, at=AT, db_path=db_path)

    def test_stale_envelope_exceeds_binding_cost_limit(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(priv, db_path)
        env = _make_env(cost="999.00")
        with pytest.raises(authority.EnvelopeExceedsGrantLimitsError):
            authority.issue_child_authorization(env, "g1", 1, at=AT, db_path=db_path)

    def test_exhausted_calls(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(priv, db_path)  # max_calls=2
        authority.issue_child_authorization(_make_env(idem_key="idem_1", authorization_id="a1"), "g1", 1, at=AT, db_path=db_path)
        authority.issue_child_authorization(_make_env(idem_key="idem_2", authorization_id="a2"), "g1", 1, at=AT, db_path=db_path)
        with pytest.raises(authority.GrantCapacityExhaustedError):
            authority.issue_child_authorization(_make_env(idem_key="idem_3", authorization_id="a3"), "g1", 1, at=AT, db_path=db_path)

    def test_exhausted_spend(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        bindings = _bindings(max_calls=100, max_total_spend=Decimal("0.50"))
        _register_grant(priv, db_path, bindings=bindings)
        authority.issue_child_authorization(_make_env(idem_key="idem_1", authorization_id="a1", cost="0.39"), "g1", 1, at=AT, db_path=db_path)
        with pytest.raises(authority.GrantCapacityExhaustedError):
            authority.issue_child_authorization(_make_env(idem_key="idem_2", authorization_id="a2", cost="0.39"), "g1", 1, at=AT, db_path=db_path)

    def test_ambiguous_same_idempotency_key_different_envelope(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(priv, db_path)
        env1 = _make_env(idem_key="shared_idem", authorization_id="a1")
        authority.issue_child_authorization(env1, "g1", 1, at=AT, db_path=db_path)
        env2 = _make_env(idem_key="shared_idem", authorization_id="a2")  # different envelope, same idem key
        with pytest.raises(store.LedgerReservationConflictError):
            authority.issue_child_authorization(env2, "g1", 1, at=AT, db_path=db_path)


class TestIssueChildAuthorizationHappyPathAndIdempotency:

    def test_issues_authorized_record_bound_to_grant(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        grant = _register_grant(priv, db_path)
        env = _make_env()
        record = authority.issue_child_authorization(env, "g1", 1, at=AT, db_path=db_path)
        assert record.current_state.value == "authorized"
        assert record.authorization_id == env.authorization_id
        evidence = record.history[-1].evidence
        assert evidence["parent_grant_id"] == "g1"
        assert evidence["parent_grant_version"] == 1
        assert evidence["parent_grant_checksum"] == grant.grant_checksum
        assert "grant_reservation_id" in evidence

    def test_idempotent_replay_returns_same_record(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(priv, db_path)
        env = _make_env()
        r1 = authority.issue_child_authorization(env, "g1", 1, at=AT, db_path=db_path)
        r2 = authority.issue_child_authorization(env, "g1", 1, at=AT, db_path=db_path)
        assert r1.authorization_id == r2.authorization_id
        assert r1.history == r2.history

    def test_get_authorization_reads_durable_record(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(priv, db_path)
        env = _make_env()
        issued = authority.issue_child_authorization(env, "g1", 1, at=AT, db_path=db_path)
        fetched = authority.get_authorization(issued.authorization_id, db_path=db_path)
        assert fetched.current_state == issued.current_state


class TestMultiChannelGenericity:

    def test_second_synthetic_channel_and_episode_works_identically(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        _register_grant(priv, db_path, channel_id="another_channel", episode_id="ep_other", grant_id="g_other")
        env = _make_env(channel_id="another_channel")
        record = authority.issue_child_authorization(env, "g_other", 1, at=AT, db_path=db_path)
        assert record.current_state.value == "authorized"
        resolved = authority.resolve_active_grant("another_channel", "ep_other", at=AT, db_path=db_path)
        assert resolved is not None


class TestIssueAuthorizationFromExplicitApproval:

    def test_valid_approval_issues_authorized_record(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        env = _make_env()
        fields = dict(
            approval_id="proposal_1", approval_version=1, approval_type="authorization_proposal",
            subject_checksum=env.envelope_id, channel_id="test_channel", scope_id="ep_test",
            workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64,
            channel_package_version="1.0", channel_package_checksum="b" * 64, allowed_bindings=(),
            currency="USD", max_cost_per_call=Decimal("0.39"), max_total_spend=Decimal("0.39"), max_calls=1,
            retry_authorized=False, fallback_authorized=False, issued_at=AT, expires_at="2099-01-01T00:00:00Z",
            approver_identity="human_operator", nonce="nonce_proposal_1",
        )
        artifact = sign_approval_artifact(fields, private_key=priv)
        record = authority.issue_authorization_from_explicit_approval(env, approval_artifact=artifact, at=AT, db_path=db_path)
        assert record.current_state.value == "authorized"
        assert record.history[-1].evidence["authorization_method"] == "explicit_human_approval"
        assert "parent_grant_id" not in record.history[-1].evidence

    def test_subject_checksum_must_match_envelope_id_exactly(self, keypair, tmp_path):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        env = _make_env()
        fields = dict(
            approval_id="proposal_1", approval_version=1, approval_type="authorization_proposal",
            subject_checksum="0" * 64,  # wrong -- does not match env.envelope_id
            channel_id="test_channel", scope_id="ep_test", workflow_id="wf", workflow_version="1.0.0",
            workflow_checksum="a" * 64, channel_package_version="1.0", channel_package_checksum="b" * 64,
            allowed_bindings=(), currency="USD", max_cost_per_call=Decimal("0.39"),
            max_total_spend=Decimal("0.39"), max_calls=1, retry_authorized=False, fallback_authorized=False,
            issued_at=AT, expires_at="2099-01-01T00:00:00Z", approver_identity="human_operator",
            nonce="nonce_proposal_2",
        )
        artifact = sign_approval_artifact(fields, private_key=priv)
        with pytest.raises(authority.ApprovalSubjectMismatchError):
            authority.issue_authorization_from_explicit_approval(env, approval_artifact=artifact, at=AT, db_path=db_path)
