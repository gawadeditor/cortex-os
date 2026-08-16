"""
tests/test_approval_artifact.py — Focused tests for
hypogeum/approval_artifact.py: Ed25519 signing/verification,
forgery/tamper/replay resistance.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hypogeum.approval_artifact import (
    ApprovalArtifactError,
    GrantBindingApproval,
    compute_approval_subject_checksum,
    sign_approval_artifact,
    verify_approval_artifact,
)


def _fields(**overrides) -> dict:
    base = dict(
        approval_id="appr1", approval_version=1, approval_type="grant",
        subject_checksum=compute_approval_subject_checksum({"a": 1}),
        channel_id="test_channel", scope_id="ep_test", workflow_id="wf", workflow_version="1.0.0",
        workflow_checksum="a" * 64, channel_package_version="1.0", channel_package_checksum="b" * 64,
        allowed_bindings=(GrantBindingApproval("fake", "fake-model", "video.text_to_video"),),
        currency="USD", max_cost_per_call=Decimal("0.39"), max_total_spend=Decimal("10.00"), max_calls=5,
        retry_authorized=False, fallback_authorized=False,
        issued_at="2026-01-01T00:00:00Z", expires_at="2026-02-01T00:00:00Z",
        approver_identity="operator_x", nonce="nonce_1",
    )
    base.update(overrides)
    return base


@pytest.fixture
def keypair():
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


class TestSigningAndVerification:

    def test_valid_signature_verifies(self, keypair):
        priv, pub = keypair
        artifact = sign_approval_artifact(_fields(), private_key=priv)
        assert verify_approval_artifact(artifact, public_key=pub) is True

    def test_invalid_approval_type_rejected_at_signing(self, keypair):
        priv, _pub = keypair
        with pytest.raises(ApprovalArtifactError):
            sign_approval_artifact(_fields(approval_type="not_a_real_type"), private_key=priv)

    def test_canonical_bytes_exclude_signature_field(self, keypair):
        priv, _pub = keypair
        artifact = sign_approval_artifact(_fields(), private_key=priv)
        assert b"signature" not in artifact.canonical_bytes()


class TestForgeryAndTamperResistance:

    def test_wrong_keypair_fails(self, keypair):
        priv, _pub = keypair
        wrong_priv = Ed25519PrivateKey.generate()
        artifact = sign_approval_artifact(_fields(), private_key=priv)
        assert verify_approval_artifact(artifact, public_key=wrong_priv.public_key()) is False

    def test_self_generated_keypair_cannot_forge_artifact_trusted_by_a_different_key(self):
        """A production caller with no access to the real private key
        cannot manufacture a valid approval by generating its own
        keypair -- the RESULTING artifact only verifies against ITS
        OWN public key, never against whatever public key the real
        deployment trusts."""
        attacker_priv = Ed25519PrivateKey.generate()
        real_deployment_pub = Ed25519PrivateKey.generate().public_key()
        forged = sign_approval_artifact(_fields(), private_key=attacker_priv)
        assert verify_approval_artifact(forged, public_key=real_deployment_pub) is False

    @pytest.mark.parametrize("field,value", [
        ("max_calls", 999),
        ("max_total_spend", Decimal("99999.00")),
        ("expires_at", "2099-01-01T00:00:00Z"),
        ("approver_identity", "someone_else"),
        ("subject_checksum", "0" * 64),
        ("channel_id", "different_channel"),
    ])
    def test_any_altered_field_invalidates_signature(self, keypair, field, value):
        priv, pub = keypair
        artifact = sign_approval_artifact(_fields(), private_key=priv)
        tampered = dataclasses.replace(artifact, **{field: value})
        assert verify_approval_artifact(tampered, public_key=pub) is False

    def test_tampered_signature_bytes_fail_without_crashing(self, keypair):
        priv, pub = keypair
        artifact = sign_approval_artifact(_fields(), private_key=priv)
        garbage = dataclasses.replace(artifact, signature=b"\x00" * 64)
        assert verify_approval_artifact(garbage, public_key=pub) is False

    def test_malformed_signature_length_fails_without_crashing(self, keypair):
        priv, pub = keypair
        artifact = sign_approval_artifact(_fields(), private_key=priv)
        malformed = dataclasses.replace(artifact, signature=b"too-short")
        assert verify_approval_artifact(malformed, public_key=pub) is False


class TestSubjectChecksumBinding:

    def test_subject_checksum_is_deterministic(self):
        payload = {"a": 1, "b": [1, 2, 3]}
        assert compute_approval_subject_checksum(payload) == compute_approval_subject_checksum(payload)

    def test_subject_checksum_independent_of_key_order(self):
        assert compute_approval_subject_checksum({"a": 1, "b": 2}) == compute_approval_subject_checksum({"b": 2, "a": 1})

    def test_different_payload_different_checksum(self):
        assert compute_approval_subject_checksum({"a": 1}) != compute_approval_subject_checksum({"a": 2})
