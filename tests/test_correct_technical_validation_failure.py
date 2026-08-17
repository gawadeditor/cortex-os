"""
tests/test_correct_technical_validation_failure.py — Focused tests for
the narrow, explicit exception to the terminal-state guard:
hypogeum.authorization_lifecycle.correct_technical_validation_failure()
and hypogeum.authorization_store.correct_technical_validation_failure().

Built after a real incident: a Phase 4 validation script crashed
(AttributeError on a nonexistent log.debug()) before ffprobe ever ran,
and the exception was mistakenly treated as a genuine technical
failure, moving 64 real, valid AuthorizationRecords to the terminal
TECHNICAL_VALIDATION_FAILED state. These tests guard the correction
path built to recover from that -- and prove every OTHER terminal
state, and every OTHER TECHNICAL_VALIDATION_FAILED record (a genuine
asset defect), remains fully, unconditionally terminal.
"""

from __future__ import annotations

import sqlite3

import pytest

from hypogeum import authorization_store as store
from hypogeum.authorization_lifecycle import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    AuthorizationRecord,
    AuthorizationState,
    MissingTransitionEvidenceError,
    TerminalStateError,
    correct_technical_validation_failure,
)

S = AuthorizationState


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "test.db"))
    c.row_factory = sqlite3.Row
    store.ensure_tables(c)
    yield c
    c.close()


def _failed_record(authorization_id="auth1", *, failure_reason="ffprobe_failed: AttributeError: x") -> AuthorizationRecord:
    rec = AuthorizationRecord.create(authorization_id=authorization_id, authorization_version=1, envelope_id="env1", envelope_version=1)
    rec = rec.transition(S.COMPILED, evidence={"compiler_identity": "x", "compiler_version": "y"}, at="t0")
    rec = rec.transition(S.AWAITING_AUTHORIZATION, evidence={"preflight_result": "ok"}, at="t0")
    rec = rec.transition(S.AUTHORIZED, evidence={"authorized_by": "x", "authorization_method": "y"}, at="t0")
    rec = rec.transition(S.SUBMITTING, evidence={"gate_checks_passed": True}, at="t0")
    rec = rec.transition(S.SUBMITTED, evidence={"provider_job_id": "job1"}, at="t0")
    rec = rec.transition(S.DOWNLOADED, evidence={"output_checksum": "c" * 64}, at="t0")
    rec = rec.transition(S.TECHNICAL_VALIDATION_FAILED, evidence={"failure_reason": failure_reason}, at="t1")
    return rec


class TestSharedStateMachineUnaffected:
    """The new edge must never be added to ALLOWED_TRANSITIONS -- the
    canonical §4.1 diagram, and every existing structural test that
    iterates it, must remain byte-for-byte unaffected."""

    def test_no_new_edge_in_allowed_transitions(self):
        assert (S.TECHNICAL_VALIDATION_FAILED, S.PENDING_EDITORIAL_REVIEW) not in ALLOWED_TRANSITIONS

    def test_technical_validation_failed_still_a_terminal_state(self):
        assert S.TECHNICAL_VALIDATION_FAILED in TERMINAL_STATES

    def test_ordinary_transition_call_still_raises_terminal_state_error(self):
        rec = _failed_record()
        with pytest.raises(TerminalStateError):
            rec.transition(S.PENDING_EDITORIAL_REVIEW, evidence={"technical_validation_result": {}}, at="t2")

    def test_every_other_terminal_state_rejected_by_the_correction_function(self):
        for terminal in TERMINAL_STATES - {S.TECHNICAL_VALIDATION_FAILED}:
            rec = AuthorizationRecord(
                authorization_id="a", authorization_version=1, envelope_id="e", envelope_version=1,
                current_state=terminal, history=(),
            )
            with pytest.raises(TerminalStateError):
                correct_technical_validation_failure(
                    rec, evidence={"technical_validation_result": {}, "correction_reason": "x", "corrected_by": "y"}, at="t2",
                )


class TestLifecycleCorrectionFunction:

    def test_corrects_to_pending_editorial_review(self):
        rec = _failed_record()
        corrected = correct_technical_validation_failure(
            rec,
            evidence={
                "technical_validation_result": {"width": 1280, "height": 720, "failures": []},
                "correction_reason": "log.debug() AttributeError in validating tool, not a real asset defect",
                "corrected_by": "test_operator",
            },
            at="t2",
        )
        assert corrected.current_state == S.PENDING_EDITORIAL_REVIEW
        assert len(corrected.history) == len(rec.history) + 1
        last = corrected.history[-1]
        assert last.from_state == S.TECHNICAL_VALIDATION_FAILED
        assert last.evidence["original_failure_reason"] == "ffprobe_failed: AttributeError: x"

    def test_original_failure_evidence_preserved_untouched(self):
        rec = _failed_record()
        corrected = correct_technical_validation_failure(
            rec, evidence={"technical_validation_result": {"failures": []}, "correction_reason": "x", "corrected_by": "y"}, at="t2",
        )
        original_event = corrected.history[-2]
        assert original_event.to_state == S.TECHNICAL_VALIDATION_FAILED
        assert original_event.evidence == {"failure_reason": "ffprobe_failed: AttributeError: x"}

    def test_missing_evidence_fails_closed(self):
        rec = _failed_record()
        with pytest.raises(MissingTransitionEvidenceError):
            correct_technical_validation_failure(rec, evidence={"technical_validation_result": {}}, at="t2")

    def test_wrong_source_state_fails_closed(self):
        rec = AuthorizationRecord.create(authorization_id="a", authorization_version=1, envelope_id="e", envelope_version=1)
        with pytest.raises(TerminalStateError):
            correct_technical_validation_failure(
                rec, evidence={"technical_validation_result": {}, "correction_reason": "x", "corrected_by": "y"}, at="t2",
            )


class TestStoreCorrectionFunction:

    def test_happy_path(self, conn):
        rec = _failed_record()
        store.persist_authorization_record(conn, rec)
        corrected = store.correct_technical_validation_failure(
            conn, "auth1",
            technical_validation_result={"width": 1280, "height": 720, "failures": []},
            correction_reason="tooling defect, not asset defect", corrected_by="test_operator", at="t2",
        )
        assert corrected.current_state == S.PENDING_EDITORIAL_REVIEW
        reloaded = store.get_authorization_record(conn, "auth1")
        assert reloaded.current_state == S.PENDING_EDITORIAL_REVIEW

    def test_refuses_to_correct_using_evidence_that_itself_shows_failures(self, conn):
        rec = _failed_record()
        store.persist_authorization_record(conn, rec)
        with pytest.raises(store.TechnicalValidationCorrectionError):
            store.correct_technical_validation_failure(
                conn, "auth1",
                technical_validation_result={"failures": ["duration_out_of_tolerance: ..."]},
                correction_reason="x", corrected_by="y", at="t2",
            )
        assert store.get_authorization_record(conn, "auth1").current_state == S.TECHNICAL_VALIDATION_FAILED

    def test_fails_closed_when_not_at_technical_validation_failed(self, conn):
        rec = AuthorizationRecord.create(authorization_id="auth2", authorization_version=1, envelope_id="env2", envelope_version=1)
        store.persist_authorization_record(conn, rec)
        with pytest.raises(store.TechnicalValidationCorrectionError):
            store.correct_technical_validation_failure(
                conn, "auth2", technical_validation_result={}, correction_reason="x", corrected_by="y", at="t2",
            )

    def test_missing_record(self, conn):
        with pytest.raises(store.AuthorizationRecordNotFoundError):
            store.correct_technical_validation_failure(
                conn, "nope", technical_validation_result={}, correction_reason="x", corrected_by="y", at="t2",
            )

    def test_idempotent_replay_after_editorial_review_advances(self, conn):
        rec = _failed_record()
        store.persist_authorization_record(conn, rec)
        first = store.correct_technical_validation_failure(
            conn, "auth1", technical_validation_result={"failures": []},
            correction_reason="x", corrected_by="y", at="t2",
        )
        # A later human verdict advances the record past PENDING_EDITORIAL_REVIEW.
        store.record_editorial_verdict(conn, "auth1", reviewer="human_1", verdict="approved", at="t3")
        # Replaying the correction call now must be a safe no-op, not an error.
        replayed = store.correct_technical_validation_failure(
            conn, "auth1", technical_validation_result={"failures": []},
            correction_reason="x", corrected_by="y", at="t4",
        )
        assert replayed.current_state == S.QUALIFIED
