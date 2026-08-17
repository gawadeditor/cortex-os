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
state, every genuine asset failure, and every UNRELATED tooling
failure remains fully, unconditionally terminal. The store-layer
function is the one that enforces content policy (exact known-defect-
signature match; independently-produced, structurally complete
passing evidence) -- the lifecycle-layer function is pure state-
machine mechanics and is content-agnostic by design (see its own
docstring), so only the store-layer tests below exercise that policy.
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
    get_technical_validation_failure_reason,
)

S = AuthorizationState

# The one real, exact string this whole mechanism exists to correct.
KNOWN_SIGNATURE = "ffprobe_failed: AttributeError: 'Logger' object has no attribute 'debug'"


def _passing_technical_validation_result(**overrides) -> dict:
    base = {
        "check": "ffprobe_geometry_duration_fps",
        "observed": {"width": 1280, "height": 720, "fps": 24.0, "duration_seconds": 8.0},
        "failures": [],
    }
    base.update(overrides)
    return base


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "test.db"))
    c.row_factory = sqlite3.Row
    store.ensure_tables(c)
    yield c
    c.close()


def _failed_record(authorization_id="auth1", *, failure_reason=KNOWN_SIGNATURE) -> AuthorizationRecord:
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


class TestGetTechnicalValidationFailureReason:

    def test_reads_the_exact_stored_reason(self):
        rec = _failed_record(failure_reason=KNOWN_SIGNATURE)
        assert get_technical_validation_failure_reason(rec) == KNOWN_SIGNATURE

    def test_none_when_never_reached_technical_validation_failed(self):
        rec = AuthorizationRecord.create(authorization_id="a", authorization_version=1, envelope_id="e", envelope_version=1)
        assert get_technical_validation_failure_reason(rec) is None


class TestLifecycleCorrectionFunction:
    """Pure mechanics -- content-agnostic by design. Content policy
    (which original reasons are eligible, what fresh evidence must
    look like) is enforced only at the store layer, tested below."""

    def test_corrects_to_pending_editorial_review(self):
        rec = _failed_record()
        corrected = correct_technical_validation_failure(
            rec,
            evidence={
                "technical_validation_result": _passing_technical_validation_result(),
                "correction_reason": "log.debug() AttributeError in validating tool, not a real asset defect",
                "corrected_by": "test_operator",
            },
            at="t2",
        )
        assert corrected.current_state == S.PENDING_EDITORIAL_REVIEW
        assert len(corrected.history) == len(rec.history) + 1
        last = corrected.history[-1]
        assert last.from_state == S.TECHNICAL_VALIDATION_FAILED
        assert last.evidence["original_failure_reason"] == KNOWN_SIGNATURE

    def test_original_failure_evidence_preserved_untouched(self):
        rec = _failed_record()
        corrected = correct_technical_validation_failure(
            rec, evidence={"technical_validation_result": {"failures": []}, "correction_reason": "x", "corrected_by": "y"}, at="t2",
        )
        original_event = corrected.history[-2]
        assert original_event.to_state == S.TECHNICAL_VALIDATION_FAILED
        assert original_event.evidence == {"failure_reason": KNOWN_SIGNATURE}

    def test_caller_supplied_original_failure_reason_is_ignored_and_overwritten(self):
        rec = _failed_record()
        corrected = correct_technical_validation_failure(
            rec,
            evidence={
                "technical_validation_result": {"failures": []},
                "correction_reason": "x", "corrected_by": "y",
                "original_failure_reason": "a lie -- this key must never be trusted from the caller",
            },
            at="t2",
        )
        assert corrected.history[-1].evidence["original_failure_reason"] == KNOWN_SIGNATURE

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


class TestStoreCorrectionFunctionHappyPath:

    def test_happy_path(self, conn):
        rec = _failed_record()
        store.persist_authorization_record(conn, rec)
        corrected = store.correct_technical_validation_failure(
            conn, "auth1",
            technical_validation_result=_passing_technical_validation_result(),
            correction_reason="tooling defect, not asset defect", corrected_by="test_operator", at="t2",
        )
        assert corrected.current_state == S.PENDING_EDITORIAL_REVIEW
        reloaded = store.get_authorization_record(conn, "auth1")
        assert reloaded.current_state == S.PENDING_EDITORIAL_REVIEW

    def test_missing_record(self, conn):
        with pytest.raises(store.AuthorizationRecordNotFoundError):
            store.correct_technical_validation_failure(
                conn, "nope", technical_validation_result=_passing_technical_validation_result(),
                correction_reason="x", corrected_by="y", at="t2",
            )

    def test_idempotent_replay_after_editorial_review_advances(self, conn):
        rec = _failed_record()
        store.persist_authorization_record(conn, rec)
        store.correct_technical_validation_failure(
            conn, "auth1", technical_validation_result=_passing_technical_validation_result(),
            correction_reason="x", corrected_by="y", at="t2",
        )
        # A later human verdict advances the record past PENDING_EDITORIAL_REVIEW.
        store.record_editorial_verdict(conn, "auth1", reviewer="human_1", verdict="approved", at="t3")
        # Replaying the correction call now must be a safe no-op, not an error.
        replayed = store.correct_technical_validation_failure(
            conn, "auth1", technical_validation_result=_passing_technical_validation_result(),
            correction_reason="x", corrected_by="y", at="t4",
        )
        assert replayed.current_state == S.QUALIFIED


class TestGenuineFailuresRemainTerminal:
    """The exact negative-test requirement: a genuine asset defect, and
    an UNRELATED tooling failure (a real crash, but a different one
    than the single known, fixed defect), must both remain terminal --
    never correctable through this mechanism."""

    def test_genuine_asset_defect_original_reason_is_rejected(self, conn):
        rec = _failed_record(failure_reason="duration_out_of_tolerance: expected 8.0s (+/-0.75), got 3.2s")
        store.persist_authorization_record(conn, rec)
        with pytest.raises(store.TechnicalValidationCorrectionError):
            store.correct_technical_validation_failure(
                conn, "auth1", technical_validation_result=_passing_technical_validation_result(),
                correction_reason="x", corrected_by="y", at="t2",
            )
        assert store.get_authorization_record(conn, "auth1").current_state == S.TECHNICAL_VALIDATION_FAILED

    def test_unrelated_tooling_failure_original_reason_is_rejected(self, conn):
        # A real crash, but NOT the one known, fixed defect -- e.g. ffprobe
        # itself missing from PATH. Must remain just as terminal as a
        # genuine asset defect; this mechanism is not a general "any
        # exception means retry" escape hatch.
        rec = _failed_record(failure_reason="ffprobe_failed: RuntimeError: ffprobe not found on PATH -- install ffmpeg")
        store.persist_authorization_record(conn, rec)
        with pytest.raises(store.TechnicalValidationCorrectionError):
            store.correct_technical_validation_failure(
                conn, "auth1", technical_validation_result=_passing_technical_validation_result(),
                correction_reason="x", corrected_by="y", at="t2",
            )
        assert store.get_authorization_record(conn, "auth1").current_state == S.TECHNICAL_VALIDATION_FAILED

    def test_missing_original_failure_reason_is_rejected(self, conn):
        # Constructed directly (not via _failed_record) with an empty
        # failure_reason -- the required-evidence-key rule on the
        # ORIGINAL transition would already forbid this in practice
        # (falsy values fail MissingTransitionEvidenceError), but the
        # store-layer policy check must independently refuse a missing/
        # falsy reason too, never treating "no reason on file" as
        # vacuously eligible.
        rec = AuthorizationRecord.create(authorization_id="auth1", authorization_version=1, envelope_id="env1", envelope_version=1)
        rec = rec.transition(S.COMPILED, evidence={"compiler_identity": "x", "compiler_version": "y"}, at="t0")
        rec = rec.transition(S.AWAITING_AUTHORIZATION, evidence={"preflight_result": "ok"}, at="t0")
        rec = rec.transition(S.AUTHORIZED, evidence={"authorized_by": "x", "authorization_method": "y"}, at="t0")
        rec = rec.transition(S.SUBMITTING, evidence={"gate_checks_passed": True}, at="t0")
        rec = rec.transition(S.SUBMITTED, evidence={"provider_job_id": "job1"}, at="t0")
        rec = rec.transition(S.DOWNLOADED, evidence={"output_checksum": "c" * 64}, at="t0")
        # Directly append a TECHNICAL_VALIDATION_FAILED event whose evidence
        # carries a falsy failure_reason -- bypassing transition()'s own
        # evidence-presence check, to prove the STORE layer's own defense
        # is independent of it, not merely relying on it.
        from hypogeum.authorization_lifecycle import TransitionEvent
        forced_event = TransitionEvent(from_state=S.DOWNLOADED, to_state=S.TECHNICAL_VALIDATION_FAILED, at="t1", evidence={"failure_reason": ""})
        rec = AuthorizationRecord(
            authorization_id=rec.authorization_id, authorization_version=rec.authorization_version,
            envelope_id=rec.envelope_id, envelope_version=rec.envelope_version,
            current_state=S.TECHNICAL_VALIDATION_FAILED, history=rec.history + (forced_event,),
        )
        store.persist_authorization_record(conn, rec)
        with pytest.raises(store.TechnicalValidationCorrectionError):
            store.correct_technical_validation_failure(
                conn, "auth1", technical_validation_result=_passing_technical_validation_result(),
                correction_reason="x", corrected_by="y", at="t2",
            )


class TestFreshEvidenceMustBeStructurallyComplete:
    """'Independently produced successful ffprobe evidence with
    explicit pass criteria -- not merely an empty failures field.'"""

    def test_bare_empty_failures_list_alone_is_rejected(self, conn):
        rec = _failed_record()
        store.persist_authorization_record(conn, rec)
        with pytest.raises(store.TechnicalValidationCorrectionError):
            store.correct_technical_validation_failure(
                conn, "auth1", technical_validation_result={"failures": []},
                correction_reason="x", corrected_by="y", at="t2",
            )
        assert store.get_authorization_record(conn, "auth1").current_state == S.TECHNICAL_VALIDATION_FAILED

    def test_failures_none_is_rejected_not_treated_as_falsy_pass(self, conn):
        rec = _failed_record()
        store.persist_authorization_record(conn, rec)
        with pytest.raises(store.TechnicalValidationCorrectionError):
            store.correct_technical_validation_failure(
                conn, "auth1", technical_validation_result=_passing_technical_validation_result(failures=None),
                correction_reason="x", corrected_by="y", at="t2",
            )

    def test_failures_non_empty_is_rejected(self, conn):
        rec = _failed_record()
        store.persist_authorization_record(conn, rec)
        with pytest.raises(store.TechnicalValidationCorrectionError):
            store.correct_technical_validation_failure(
                conn, "auth1",
                technical_validation_result=_passing_technical_validation_result(failures=["duration_out_of_tolerance: ..."]),
                correction_reason="x", corrected_by="y", at="t2",
            )
        assert store.get_authorization_record(conn, "auth1").current_state == S.TECHNICAL_VALIDATION_FAILED

    def test_missing_check_name_is_rejected(self, conn):
        rec = _failed_record()
        store.persist_authorization_record(conn, rec)
        result = _passing_technical_validation_result()
        del result["check"]
        with pytest.raises(store.TechnicalValidationCorrectionError):
            store.correct_technical_validation_failure(
                conn, "auth1", technical_validation_result=result, correction_reason="x", corrected_by="y", at="t2",
            )

    def test_missing_observed_dict_is_rejected(self, conn):
        rec = _failed_record()
        store.persist_authorization_record(conn, rec)
        result = _passing_technical_validation_result()
        del result["observed"]
        with pytest.raises(store.TechnicalValidationCorrectionError):
            store.correct_technical_validation_failure(
                conn, "auth1", technical_validation_result=result, correction_reason="x", corrected_by="y", at="t2",
            )

    @pytest.mark.parametrize("missing_key", ["width", "height", "fps", "duration_seconds"])
    def test_missing_individual_observed_field_is_rejected(self, conn, missing_key):
        rec = _failed_record()
        store.persist_authorization_record(conn, rec)
        result = _passing_technical_validation_result()
        result["observed"] = {k: v for k, v in result["observed"].items() if k != missing_key}
        with pytest.raises(store.TechnicalValidationCorrectionError):
            store.correct_technical_validation_failure(
                conn, "auth1", technical_validation_result=result, correction_reason="x", corrected_by="y", at="t2",
            )

    def test_null_individual_observed_field_is_rejected(self, conn):
        rec = _failed_record()
        store.persist_authorization_record(conn, rec)
        result = _passing_technical_validation_result()
        result["observed"] = {**result["observed"], "width": None}
        with pytest.raises(store.TechnicalValidationCorrectionError):
            store.correct_technical_validation_failure(
                conn, "auth1", technical_validation_result=result, correction_reason="x", corrected_by="y", at="t2",
            )
