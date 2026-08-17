"""
tests/test_reconcile_superseded_authorization.py — Focused tests for
hypogeum.authorization_store.reconcile_superseded_authorization() and
reconcile_all_superseded_authorizations(): the mechanism Phase 3 of the
ep_f3619022 replacement-proposal work uses to retire an AUTHORIZED/
SUBMITTING authorization whose own compiled operation_payload was
produced by the (now-fixed) buggy compiler, once a corrected
replacement authorization has already been durably persisted under a
DIFFERENT authorization_id.

All fixtures use an isolated tmp_path SQLite DB. No test touches the
live .hypogeum-assets/hypogeum.db, and no test makes a network call.
"""

from __future__ import annotations

import sqlite3

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


def _authorized_record(authorization_id: str, *, at="t0") -> AuthorizationRecord:
    rec = AuthorizationRecord.create(
        authorization_id=authorization_id, authorization_version=3,
        envelope_id=f"env_{authorization_id}", envelope_version=1,
    )
    rec = rec.transition(AuthorizationState.COMPILED, evidence={"compiler_identity": "x", "compiler_version": "y"}, at=at)
    rec = rec.transition(AuthorizationState.AWAITING_AUTHORIZATION, evidence={"preflight_result": "ok"}, at=at)
    rec = rec.transition(
        AuthorizationState.AUTHORIZED, evidence={"authorized_by": "x", "authorization_method": "y"}, at=at,
    )
    return rec


def _submitting_record(authorization_id: str, *, at="t0") -> AuthorizationRecord:
    rec = _authorized_record(authorization_id, at=at)
    return rec.transition(AuthorizationState.SUBMITTING, evidence={"gate_checks_passed": True}, at=at)


def _linked_new_record(new_authorization_id: str, old_authorization_id: str) -> AuthorizationRecord:
    return AuthorizationRecord.create(
        authorization_id=new_authorization_id, authorization_version=1,
        envelope_id=f"env_{new_authorization_id}", envelope_version=1,
        superseded_authorization_id=old_authorization_id,
    )


class TestReconcileSupersededAuthorizationHappyPath:

    def test_supersedes_authorized_record(self, conn):
        old = _authorized_record("old1")
        store.persist_authorization_record(conn, old)
        new = _linked_new_record("new1", "old1")
        store.persist_authorization_record(conn, new)

        result = store.reconcile_superseded_authorization(
            conn, "old1", new_authorization_id="new1", at="t1",
            changed_dependency="operation_payload_compiler", old_checksum="deadbeef", new_checksum="cafefeed",
        )
        assert result.current_state == AuthorizationState.INVALIDATED_SOURCE_CHANGED
        reloaded = store.get_authorization_record(conn, "old1")
        assert reloaded.current_state == AuthorizationState.INVALIDATED_SOURCE_CHANGED
        last = reloaded.history[-1]
        assert last.evidence["superseding_authorization_id"] == "new1"
        assert last.evidence["old_checksum"] == "deadbeef"
        assert last.evidence["new_checksum"] == "cafefeed"

    def test_supersedes_submitting_record(self, conn):
        old = _submitting_record("old2")
        store.persist_authorization_record(conn, old)
        new = _linked_new_record("new2", "old2")
        store.persist_authorization_record(conn, new)

        result = store.reconcile_superseded_authorization(
            conn, "old2", new_authorization_id="new2", at="t1",
            changed_dependency="operation_payload_compiler", old_checksum="a", new_checksum="b",
        )
        assert result.current_state == AuthorizationState.INVALIDATED_SOURCE_CHANGED


class TestReconcileSupersededAuthorizationFailsClosed:

    def test_missing_old_record(self, conn):
        with pytest.raises(store.AuthorizationRecordNotFoundError):
            store.reconcile_superseded_authorization(
                conn, "nope", new_authorization_id="new1", at="t1",
                changed_dependency="x", old_checksum="a", new_checksum="b",
            )

    def test_missing_new_record(self, conn):
        old = _authorized_record("old3")
        store.persist_authorization_record(conn, old)
        with pytest.raises(store.AuthorizationRecordNotFoundError):
            store.reconcile_superseded_authorization(
                conn, "old3", new_authorization_id="ghost", at="t1",
                changed_dependency="x", old_checksum="a", new_checksum="b",
            )
        # Old record must remain untouched -- zero writes on a failed precondition.
        assert store.get_authorization_record(conn, "old3").current_state == AuthorizationState.AUTHORIZED

    def test_new_record_does_not_claim_supersession(self, conn):
        old = _authorized_record("old4")
        store.persist_authorization_record(conn, old)
        # new4 exists but does NOT name old4 as superseded_authorization_id.
        unlinked_new = AuthorizationRecord.create(
            authorization_id="new4", authorization_version=1, envelope_id="env_new4", envelope_version=1,
        )
        store.persist_authorization_record(conn, unlinked_new)
        with pytest.raises(store.SupersessionConflictError):
            store.reconcile_superseded_authorization(
                conn, "old4", new_authorization_id="new4", at="t1",
                changed_dependency="x", old_checksum="a", new_checksum="b",
            )
        assert store.get_authorization_record(conn, "old4").current_state == AuthorizationState.AUTHORIZED

    def test_old_record_in_wrong_state_rejected(self, conn):
        # Still at AWAITING_AUTHORIZATION -- not AUTHORIZED/SUBMITTING.
        old = AuthorizationRecord.create(
            authorization_id="old5", authorization_version=3, envelope_id="env_old5", envelope_version=1,
        )
        old = old.transition(AuthorizationState.COMPILED, evidence={"compiler_identity": "x", "compiler_version": "y"}, at="t0")
        old = old.transition(AuthorizationState.AWAITING_AUTHORIZATION, evidence={"preflight_result": "ok"}, at="t0")
        store.persist_authorization_record(conn, old)
        new = _linked_new_record("new5", "old5")
        store.persist_authorization_record(conn, new)
        with pytest.raises(store.SupersessionConflictError):
            store.reconcile_superseded_authorization(
                conn, "old5", new_authorization_id="new5", at="t1",
                changed_dependency="x", old_checksum="a", new_checksum="b",
            )


class TestReconcileSupersededAuthorizationIdempotency:

    def test_replay_same_new_id_is_a_no_op(self, conn):
        old = _authorized_record("old6")
        store.persist_authorization_record(conn, old)
        new = _linked_new_record("new6", "old6")
        store.persist_authorization_record(conn, new)

        first = store.reconcile_superseded_authorization(
            conn, "old6", new_authorization_id="new6", at="t1",
            changed_dependency="x", old_checksum="a", new_checksum="b",
        )
        second = store.reconcile_superseded_authorization(
            conn, "old6", new_authorization_id="new6", at="t2",
            changed_dependency="x", old_checksum="a", new_checksum="b",
        )
        assert first.history == second.history  # no duplicate transition appended

    def test_replay_different_new_id_fails_closed(self, conn):
        old = _authorized_record("old7")
        store.persist_authorization_record(conn, old)
        new = _linked_new_record("new7", "old7")
        store.persist_authorization_record(conn, new)
        store.reconcile_superseded_authorization(
            conn, "old7", new_authorization_id="new7", at="t1",
            changed_dependency="x", old_checksum="a", new_checksum="b",
        )
        other_new = _linked_new_record("other7", "old7")
        store.persist_authorization_record(conn, other_new)
        with pytest.raises(store.SupersessionConflictError):
            store.reconcile_superseded_authorization(
                conn, "old7", new_authorization_id="other7", at="t3",
                changed_dependency="x", old_checksum="a", new_checksum="b",
            )


class TestReconcileAllSupersededAuthorizationsAtomicity:

    def test_bulk_happy_path(self, conn):
        for i in (1, 2, 3):
            store.persist_authorization_record(conn, _authorized_record(f"bulk_old{i}"))
            store.persist_authorization_record(conn, _linked_new_record(f"bulk_new{i}", f"bulk_old{i}"))
        conn.commit()
        mapping = {f"bulk_old{i}": f"bulk_new{i}" for i in (1, 2, 3)}
        results = store.reconcile_all_superseded_authorizations(
            conn, mapping, at="t1", changed_dependency="x", old_checksum="a", new_checksum="b",
        )
        assert len(results) == 3
        for i in (1, 2, 3):
            assert store.get_authorization_record(conn, f"bulk_old{i}").current_state == AuthorizationState.INVALIDATED_SOURCE_CHANGED

    def test_one_failing_pair_aborts_the_entire_batch(self, conn):
        store.persist_authorization_record(conn, _authorized_record("ok_old1"))
        store.persist_authorization_record(conn, _linked_new_record("ok_new1", "ok_old1"))
        store.persist_authorization_record(conn, _authorized_record("bad_old2"))
        conn.commit()
        # bad_new2 is never persisted -- this pair must fail closed.
        mapping = {"ok_old1": "ok_new1", "bad_old2": "bad_new2"}
        with pytest.raises(store.AuthorizationRecordNotFoundError):
            store.reconcile_all_superseded_authorizations(
                conn, mapping, at="t1", changed_dependency="x", old_checksum="a", new_checksum="b",
            )
        # Zero writes -- even the pair with a valid, fully-linked new record must be untouched.
        assert store.get_authorization_record(conn, "ok_old1").current_state == AuthorizationState.AUTHORIZED

    def test_bulk_idempotent_replay_over_already_superseded_entries(self, conn):
        store.persist_authorization_record(conn, _authorized_record("idem_old1"))
        store.persist_authorization_record(conn, _linked_new_record("idem_new1", "idem_old1"))
        conn.commit()
        mapping = {"idem_old1": "idem_new1"}
        store.reconcile_all_superseded_authorizations(
            conn, mapping, at="t1", changed_dependency="x", old_checksum="a", new_checksum="b",
        )
        # Replaying the exact same batch again must succeed as a no-op, not raise.
        results = store.reconcile_all_superseded_authorizations(
            conn, mapping, at="t2", changed_dependency="x", old_checksum="a", new_checksum="b",
        )
        assert results[0].current_state == AuthorizationState.INVALIDATED_SOURCE_CHANGED
