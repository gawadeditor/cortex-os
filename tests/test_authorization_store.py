"""
tests/test_authorization_store.py — Focused tests for
hypogeum/authorization_store.py: grant/reservation/approval-artifact
persistence, the reservation lifecycle, and the boundary-revalidation
function hypogeum.submission_gate imports.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from hypogeum import authorization_store as store
from hypogeum.authorization_lifecycle import AuthorizationRecord, AuthorizationState


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "test.db"))
    c.row_factory = sqlite3.Row
    store.ensure_tables(c)
    yield c
    c.close()


def _bindings(**overrides):
    base = dict(
        provider="fake", model="fake-model", provider_operation="video.text_to_video",
        currency="USD", max_cost_per_call=Decimal("0.39"), max_calls=2, max_total_spend=Decimal("1.00"),
    )
    base.update(overrides)
    return [store.GrantBinding(**base)]


class TestGrantAtomicRegistration:

    def test_register_and_resolve(self, conn):
        g = store.insert_grant(
            conn, grant_id="g1", grant_version=1, grant_checksum="c" * 64, channel_id="test_channel",
            episode_id="ep_test", workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64,
            bindings=_bindings(), max_total_spend_all_bindings=Decimal("1.00"), currency="USD",
            expires_at="2099-01-01T00:00:00Z", approval_id="appr1", approval_version=1,
        )
        assert g.status == "active"
        fetched = store.get_grant(conn, "g1", 1)
        assert fetched.grant_checksum == "c" * 64
        assert fetched.find_binding("fake", "fake-model", "video.text_to_video") is not None
        assert fetched.find_binding("fake", "other-model", "video.text_to_video") is None

    def test_idempotent_replay_same_checksum(self, conn):
        kwargs = dict(
            grant_id="g1", grant_version=1, grant_checksum="c" * 64, channel_id="test_channel",
            episode_id="ep_test", workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64,
            bindings=_bindings(), max_total_spend_all_bindings=Decimal("1.00"), currency="USD",
            expires_at="2099-01-01T00:00:00Z", approval_id="appr1", approval_version=1,
        )
        g1 = store.insert_grant(conn, **kwargs)
        g2 = store.insert_grant(conn, **kwargs)
        assert g1.grant_checksum == g2.grant_checksum

    def test_different_checksum_same_key_raises(self, conn):
        base = dict(
            grant_id="g1", grant_version=1, channel_id="test_channel", episode_id="ep_test",
            workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64, bindings=_bindings(),
            max_total_spend_all_bindings=Decimal("1.00"), currency="USD", expires_at="2099-01-01T00:00:00Z",
            approval_id="appr1", approval_version=1,
        )
        store.insert_grant(conn, grant_checksum="c" * 64, **base)
        with pytest.raises(store.GrantAlreadyExistsWithDifferentContentError):
            store.insert_grant(conn, grant_checksum="d" * 64, **base)

    def test_find_active_grant_for_scope_returns_most_recent(self, conn):
        base = dict(
            channel_id="test_channel", episode_id="ep_test", workflow_id="wf", workflow_version="1.0.0",
            workflow_checksum="a" * 64, bindings=_bindings(), max_total_spend_all_bindings=Decimal("1.00"),
            currency="USD", expires_at="2099-01-01T00:00:00Z", approval_id="appr1", approval_version=1,
        )
        store.insert_grant(conn, grant_id="g1", grant_version=1, grant_checksum="c" * 64, **base)
        found = store.find_active_grant_for_scope(conn, "test_channel", "ep_test")
        assert found.grant_id == "g1"

    def test_revoke_hides_from_active_lookups(self, conn):
        base = dict(
            channel_id="test_channel", episode_id="ep_test", workflow_id="wf", workflow_version="1.0.0",
            workflow_checksum="a" * 64, bindings=_bindings(), max_total_spend_all_bindings=Decimal("1.00"),
            currency="USD", expires_at="2099-01-01T00:00:00Z", approval_id="appr1", approval_version=1,
        )
        store.insert_grant(conn, grant_id="g1", grant_version=1, grant_checksum="c" * 64, **base)
        revoked = store.revoke_grant_row(conn, "g1", 1, revoked_by="operator_x", reason="done", at="2026-01-01T00:00:00Z")
        assert revoked is True
        assert store.find_active_grant_for_scope(conn, "test_channel", "ep_test") is None
        # revoking an already-revoked grant is a no-op, never raises
        assert store.revoke_grant_row(conn, "g1", 1, revoked_by="x", reason="again", at="2026-01-01T00:00:01Z") is False


class TestReservationLedgerIdempotencyAndConflict:

    def test_idempotent_replay_same_envelope(self, conn):
        r1 = store.insert_ledger_reservation(
            conn, reservation_id="res1", grant_id="g1", grant_version=1, provider="fake", model="fake-model",
            provider_operation="video.text_to_video", idempotency_key="idem1", envelope_id="env1",
            authorization_id="auth1", reserved_amount=Decimal("0.39"), at="2026-01-01T00:00:00Z",
        )
        r2 = store.insert_ledger_reservation(
            conn, reservation_id="res1_dup", grant_id="g1", grant_version=1, provider="fake", model="fake-model",
            provider_operation="video.text_to_video", idempotency_key="idem1", envelope_id="env1",
            authorization_id="auth1", reserved_amount=Decimal("0.39"), at="2026-01-01T00:00:00Z",
        )
        assert r1.reservation_id == r2.reservation_id == "res1"

    def test_conflicting_envelope_id_raises(self, conn):
        store.insert_ledger_reservation(
            conn, reservation_id="res1", grant_id="g1", grant_version=1, provider="fake", model="fake-model",
            provider_operation="video.text_to_video", idempotency_key="idem1", envelope_id="env1",
            authorization_id="auth1", reserved_amount=Decimal("0.39"), at="2026-01-01T00:00:00Z",
        )
        with pytest.raises(store.LedgerReservationConflictError):
            store.insert_ledger_reservation(
                conn, reservation_id="res2", grant_id="g1", grant_version=1, provider="fake", model="fake-model",
                provider_operation="video.text_to_video", idempotency_key="idem1", envelope_id="DIFFERENT",
                authorization_id="auth1", reserved_amount=Decimal("0.39"), at="2026-01-01T00:00:00Z",
            )


class TestReservationLifecycle:

    def _reserve(self, conn, reservation_id="res1", amount=Decimal("0.39")):
        return store.insert_ledger_reservation(
            conn, reservation_id=reservation_id, grant_id="g1", grant_version=1, provider="fake",
            model="fake-model", provider_operation="video.text_to_video", idempotency_key=reservation_id,
            envelope_id=f"env_{reservation_id}", authorization_id=f"auth_{reservation_id}",
            reserved_amount=amount, at="2026-01-01T00:00:00Z",
        )

    def test_reserved_to_boundary_entered_to_committed(self, conn):
        self._reserve(conn)
        ok1 = store.transition_ledger_state(conn, "res1", from_state="reserved", to_state="boundary_entered", at="t1")
        assert ok1
        ok2 = store.transition_ledger_state(conn, "res1", from_state="boundary_entered", to_state="committed", at="t2", charged_amount=Decimal("0.39"))
        assert ok2
        row = store.get_ledger_reservation(conn, "res1")
        assert row.state == "committed"
        assert row.charged_amount == Decimal("0.39")

    def test_duplicate_settlement_attempt_no_ops(self, conn):
        self._reserve(conn)
        store.transition_ledger_state(conn, "res1", from_state="reserved", to_state="boundary_entered", at="t1")
        first = store.transition_ledger_state(conn, "res1", from_state="boundary_entered", to_state="committed", at="t2", charged_amount=Decimal("0.39"))
        second = store.transition_ledger_state(conn, "res1", from_state="boundary_entered", to_state="released", at="t3")
        assert first is True
        assert second is False  # row was no longer in 'boundary_entered' -- no double-settle
        assert store.get_ledger_reservation(conn, "res1").state == "committed"

    def test_transition_from_wrong_state_returns_false_not_raise(self, conn):
        self._reserve(conn)
        # row is 'reserved', not 'boundary_entered' -- must no-op, not raise
        result = store.transition_ledger_state(conn, "res1", from_state="boundary_entered", to_state="committed", at="t1")
        assert result is False
        assert store.get_ledger_reservation(conn, "res1").state == "reserved"

    def test_unknown_acceptance_never_auto_released(self, conn):
        self._reserve(conn)
        store.transition_ledger_state(conn, "res1", from_state="reserved", to_state="boundary_entered", at="t1")
        store.transition_ledger_state(conn, "res1", from_state="boundary_entered", to_state="possible_unknown_acceptance", at="t2")
        row = store.get_ledger_reservation(conn, "res1")
        assert row.state == "possible_unknown_acceptance"
        # still counts against capacity
        count, total = store.sum_active_ledger_consumption(conn, "g1", "fake", "fake-model", "video.text_to_video")
        assert count == 1
        assert total == Decimal("0.39")

    def test_released_frees_capacity(self, conn):
        self._reserve(conn)
        store.transition_ledger_state(conn, "res1", from_state="reserved", to_state="released", at="t1")
        count, total = store.sum_active_ledger_consumption(conn, "g1", "fake", "fake-model", "video.text_to_video")
        assert count == 0
        assert total == Decimal("0")

    def test_reserved_state_counts_against_capacity_immediately(self, conn):
        self._reserve(conn)
        count, total = store.sum_active_ledger_consumption(conn, "g1", "fake", "fake-model", "video.text_to_video")
        assert count == 1
        assert total == Decimal("0.39")

    def test_reconciled_confirmed_not_submitted_frees_capacity(self, conn):
        self._reserve(conn)
        store.transition_ledger_state(conn, "res1", from_state="reserved", to_state="boundary_entered", at="t1")
        store.transition_ledger_state(conn, "res1", from_state="boundary_entered", to_state="possible_unknown_acceptance", at="t2")
        store.transition_ledger_state(
            conn, "res1", from_state="possible_unknown_acceptance", to_state="reconciled", at="t3",
            reconciliation_resolution="confirmed_not_submitted",
        )
        count, total = store.sum_active_ledger_consumption(conn, "g1", "fake", "fake-model", "video.text_to_video")
        assert count == 0 and total == Decimal("0")

    def test_reconciled_confirmed_accepted_still_counts(self, conn):
        self._reserve(conn)
        store.transition_ledger_state(conn, "res1", from_state="reserved", to_state="boundary_entered", at="t1")
        store.transition_ledger_state(conn, "res1", from_state="boundary_entered", to_state="possible_unknown_acceptance", at="t2")
        store.transition_ledger_state(
            conn, "res1", from_state="possible_unknown_acceptance", to_state="reconciled", at="t3",
            reconciliation_resolution="confirmed_accepted", charged_amount=Decimal("0.39"),
        )
        count, total = store.sum_active_ledger_consumption(conn, "g1", "fake", "fake-model", "video.text_to_video")
        assert count == 1 and total == Decimal("0.39")


class TestRevalidateGrantReservation:

    def _setup_active_grant_and_reservation(self, conn, expires_at="2099-01-01T00:00:00Z"):
        store.insert_grant(
            conn, grant_id="g1", grant_version=1, grant_checksum="c" * 64, channel_id="test_channel",
            episode_id="ep_test", workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64,
            bindings=_bindings(), max_total_spend_all_bindings=Decimal("1.00"), currency="USD",
            expires_at=expires_at, approval_id="appr1", approval_version=1,
        )
        store.insert_ledger_reservation(
            conn, reservation_id="res1", grant_id="g1", grant_version=1, provider="fake", model="fake-model",
            provider_operation="video.text_to_video", idempotency_key="idem1", envelope_id="env1",
            authorization_id="auth1", reserved_amount=Decimal("0.39"), at="2026-01-01T00:00:00Z",
        )

    def test_success_transitions_to_boundary_entered(self, conn):
        self._setup_active_grant_and_reservation(conn)
        store.revalidate_grant_reservation(
            conn, parent_grant_id="g1", parent_grant_version=1, parent_grant_checksum="c" * 64,
            grant_reservation_id="res1", at="2026-01-01T00:05:00Z",
        )
        assert store.get_ledger_reservation(conn, "res1").state == "boundary_entered"

    def test_missing_grant_fails_closed(self, conn):
        store.insert_ledger_reservation(
            conn, reservation_id="res1", grant_id="ghost", grant_version=1, provider="fake", model="fake-model",
            provider_operation="video.text_to_video", idempotency_key="idem1", envelope_id="env1",
            authorization_id="auth1", reserved_amount=Decimal("0.39"), at="2026-01-01T00:00:00Z",
        )
        with pytest.raises(store.GrantNotFoundError):
            store.revalidate_grant_reservation(
                conn, parent_grant_id="ghost", parent_grant_version=1, parent_grant_checksum="c" * 64,
                grant_reservation_id="res1", at="2026-01-01T00:05:00Z",
            )

    def test_revoked_grant_fails_closed(self, conn):
        self._setup_active_grant_and_reservation(conn)
        store.revoke_grant_row(conn, "g1", 1, revoked_by="x", reason="test", at="2026-01-01T00:01:00Z")
        with pytest.raises(store.GrantRevokedError):
            store.revalidate_grant_reservation(
                conn, parent_grant_id="g1", parent_grant_version=1, parent_grant_checksum="c" * 64,
                grant_reservation_id="res1", at="2026-01-01T00:05:00Z",
            )

    def test_expired_grant_fails_closed(self, conn):
        self._setup_active_grant_and_reservation(conn, expires_at="2026-01-01T00:00:00Z")
        with pytest.raises(store.GrantExpiredError):
            store.revalidate_grant_reservation(
                conn, parent_grant_id="g1", parent_grant_version=1, parent_grant_checksum="c" * 64,
                grant_reservation_id="res1", at="2026-06-01T00:00:00Z",
            )

    def test_checksum_mismatch_fails_closed(self, conn):
        self._setup_active_grant_and_reservation(conn)
        with pytest.raises(store.GrantChecksumMismatchError):
            store.revalidate_grant_reservation(
                conn, parent_grant_id="g1", parent_grant_version=1, parent_grant_checksum="STALE" + "c" * 59,
                grant_reservation_id="res1", at="2026-01-01T00:05:00Z",
            )

    def test_reservation_not_found_fails_closed(self, conn):
        self._setup_active_grant_and_reservation(conn)
        with pytest.raises(store.ReservationNotFoundError):
            store.revalidate_grant_reservation(
                conn, parent_grant_id="g1", parent_grant_version=1, parent_grant_checksum="c" * 64,
                grant_reservation_id="does-not-exist", at="2026-01-01T00:05:00Z",
            )

    def test_reservation_already_settled_fails_closed(self, conn):
        self._setup_active_grant_and_reservation(conn)
        store.revalidate_grant_reservation(
            conn, parent_grant_id="g1", parent_grant_version=1, parent_grant_checksum="c" * 64,
            grant_reservation_id="res1", at="2026-01-01T00:05:00Z",
        )
        with pytest.raises(store.ReservationStateMismatchError):
            store.revalidate_grant_reservation(
                conn, parent_grant_id="g1", parent_grant_version=1, parent_grant_checksum="c" * 64,
                grant_reservation_id="res1", at="2026-01-01T00:06:00Z",
            )


class TestApprovalArtifactReplayDefense:

    def test_replayed_nonce_raises_even_for_different_approval_id(self, conn):
        from hypogeum.approval_artifact import sign_approval_artifact
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        priv = Ed25519PrivateKey.generate()
        fields = dict(
            approval_id="appr1", approval_version=1, approval_type="grant", subject_checksum="c" * 64,
            channel_id="test_channel", scope_id="ep_test", workflow_id="wf", workflow_version="1.0.0",
            workflow_checksum="a" * 64, channel_package_version="1.0", channel_package_checksum="b" * 64,
            allowed_bindings=(), currency="USD", max_cost_per_call=Decimal("0.39"),
            max_total_spend=Decimal("1.00"), max_calls=2, retry_authorized=False, fallback_authorized=False,
            issued_at="2026-01-01T00:00:00Z", expires_at="2099-01-01T00:00:00Z",
            approver_identity="operator_x", nonce="shared_nonce",
        )
        a1 = sign_approval_artifact(fields, private_key=priv)
        a2 = sign_approval_artifact({**fields, "approval_id": "appr2"}, private_key=priv)
        store.persist_approval_artifact(conn, a1)
        with pytest.raises(store.ApprovalReplayError):
            store.persist_approval_artifact(conn, a2)


class TestAuthorizationRecordPersistence:

    def test_round_trip_preserves_history(self, conn):
        rec = AuthorizationRecord.create(authorization_id="auth1", envelope_id="env1", envelope_version=1)
        rec = rec.transition(AuthorizationState.COMPILED, evidence={"compiler_identity": "x", "compiler_version": "1"}, at="t1")
        store.persist_authorization_record(conn, rec)
        reloaded = store.get_authorization_record(conn, "auth1")
        assert reloaded.current_state == AuthorizationState.COMPILED
        assert len(reloaded.history) == 1

    def test_repeated_persist_upserts_latest_state(self, conn):
        rec = AuthorizationRecord.create(authorization_id="auth1", envelope_id="env1", envelope_version=1)
        store.persist_authorization_record(conn, rec)
        rec2 = rec.transition(AuthorizationState.COMPILED, evidence={"compiler_identity": "x", "compiler_version": "1"}, at="t1")
        store.persist_authorization_record(conn, rec2)
        reloaded = store.get_authorization_record(conn, "auth1")
        assert reloaded.current_state == AuthorizationState.COMPILED

    def test_missing_record_returns_none(self, conn):
        assert store.get_authorization_record(conn, "does-not-exist") is None


class TestAuthorizationProposalPersistence:
    """The immutable, checksummed proposal artifact (plan §6 Commit M) --
    what a future, offline-signed ApprovalArtifact will bind to via
    subject_checksum."""

    def _make(self, *, proposal_id="prop_1", **overrides):
        kwargs = dict(
            channel_id="hypogeum", episode_id="ep_test", proposed_version=2, superseded_version=1,
            workflow_id="hypogeum_main_production", workflow_version="1.0.0", workflow_checksum="a" * 64,
            channel_package_version="1.0", channel_package_checksum="b" * 64,
            authorization_ids=["a2", "a1"],
            provider_model_operation_bindings=[("muapi", "veo3.1-lite-text-to-video", "video.text_to_video")],
            max_cost_per_call_ceiling=Decimal("2.50"), max_total_spend=Decimal("5.00"), currency="USD",
        )
        kwargs.update(overrides)
        checksum = store.compute_proposal_checksum(**kwargs)
        return store.AuthorizationProposal(
            proposal_id=proposal_id, created_at="2026-01-01T00:00:00Z", proposal_checksum=checksum, **kwargs,
        )

    def test_round_trip_preserves_every_field(self, conn):
        proposal = self._make()
        store.persist_authorization_proposal(conn, proposal)
        reloaded = store.get_authorization_proposal(conn, "prop_1")
        assert reloaded.proposal_checksum == proposal.proposal_checksum
        assert reloaded.authorization_ids == ("a1", "a2")  # sorted, order-independent
        assert reloaded.provider_model_operation_bindings == proposal.provider_model_operation_bindings
        assert reloaded.max_cost_per_call_ceiling == Decimal("2.50")
        assert reloaded.max_total_spend == Decimal("5.00")
        assert reloaded.superseded_version == 1

    def test_missing_proposal_returns_none(self, conn):
        assert store.get_authorization_proposal(conn, "does-not-exist") is None

    def test_checksum_is_order_independent_over_authorization_ids(self):
        base = dict(
            channel_id="hypogeum", episode_id="ep_test", proposed_version=2, superseded_version=1,
            workflow_id="hypogeum_main_production", workflow_version="1.0.0", workflow_checksum="a" * 64,
            channel_package_version="1.0", channel_package_checksum="b" * 64,
            provider_model_operation_bindings=[("muapi", "veo3.1-lite-text-to-video", "video.text_to_video")],
            max_cost_per_call_ceiling=Decimal("2.50"), max_total_spend=Decimal("5.00"), currency="USD",
        )
        c1 = store.compute_proposal_checksum(authorization_ids=["a1", "a2"], **base)
        c2 = store.compute_proposal_checksum(authorization_ids=["a2", "a1"], **base)
        assert c1 == c2

    def test_checksum_changes_when_max_total_spend_changes(self):
        base = dict(
            channel_id="hypogeum", episode_id="ep_test", proposed_version=2, superseded_version=1,
            workflow_id="hypogeum_main_production", workflow_version="1.0.0", workflow_checksum="a" * 64,
            channel_package_version="1.0", channel_package_checksum="b" * 64,
            authorization_ids=["a1", "a2"],
            provider_model_operation_bindings=[("muapi", "veo3.1-lite-text-to-video", "video.text_to_video")],
            max_cost_per_call_ceiling=Decimal("2.50"), currency="USD",
        )
        c1 = store.compute_proposal_checksum(max_total_spend=Decimal("5.00"), **base)
        c2 = store.compute_proposal_checksum(max_total_spend=Decimal("5.01"), **base)
        assert c1 != c2

    def test_duplicate_proposed_version_for_same_scope_rejected(self, conn):
        store.persist_authorization_proposal(conn, self._make())
        with pytest.raises(sqlite3.IntegrityError):
            store.persist_authorization_proposal(conn, self._make(proposal_id="prop_2"))
