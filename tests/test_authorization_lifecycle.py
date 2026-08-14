"""
tests/test_authorization_lifecycle.py — Unit tests for
hypogeum/authorization_lifecycle.py (Commit 1, 2026-08-14)

Every test uses an explicit, fixed `at` timestamp string -- never
datetime.now() -- so nothing here depends on wall-clock timing. No test
imports a network-capable module or reaches a provider/persistence path
(neither exists yet in this commit).
"""

from __future__ import annotations

import itertools

import pytest

from hypogeum.authorization_lifecycle import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    AuthorizationRecord,
    AuthorizationState,
    IllegalTransitionError,
    MissingTransitionEvidenceError,
    REQUIRED_TRANSITION_EVIDENCE_KEYS,
    TerminalStateError,
    create_superseding_authorization,
)

S = AuthorizationState
FIXED_TIME = "2026-01-01T00:00:00Z"


def _new_record() -> AuthorizationRecord:
    return AuthorizationRecord.create(authorization_id="auth_1", envelope_id="env_1", envelope_version=1)


def _advance(record: AuthorizationRecord, to_state: AuthorizationState, **evidence_overrides) -> AuthorizationRecord:
    required_keys = REQUIRED_TRANSITION_EVIDENCE_KEYS.get((record.current_state, to_state), ())
    evidence = {k: f"value_for_{k}" for k in required_keys}
    evidence.update(evidence_overrides)
    return record.transition(to_state, evidence=evidence, at=FIXED_TIME)


_HAPPY_PATH = [
    S.COMPILED, S.AWAITING_AUTHORIZATION, S.AUTHORIZED, S.SUBMITTING,
    S.SUBMITTED, S.DOWNLOADED, S.PENDING_EDITORIAL_REVIEW, S.QUALIFIED,
    S.ALLOCATED, S.CONSUMED,
]


class TestLegalTransitionsSucceed:

    def test_every_documented_transition_succeeds(self):
        for from_state, to_state in ALLOWED_TRANSITIONS:
            record = AuthorizationRecord(
                authorization_id="a", authorization_version=1, envelope_id="e",
                envelope_version=1, current_state=from_state, history=(),
            )
            result = _advance(record, to_state)
            assert result.current_state == to_state

    def test_full_happy_path_reaches_consumed(self):
        record = _new_record()
        for state in _HAPPY_PATH:
            record = _advance(record, state)
        assert record.current_state == S.CONSUMED
        assert record.is_terminal()
        assert len(record.history) == len(_HAPPY_PATH)


class TestIllegalTransitionsFail:

    def test_every_undocumented_transition_fails(self):
        all_states = list(AuthorizationState)
        undocumented = [
            (a, b) for a, b in itertools.product(all_states, all_states)
            if (a, b) not in ALLOWED_TRANSITIONS and a not in TERMINAL_STATES
        ]
        assert undocumented, "sanity check: there must be some undocumented pairs to test"
        for from_state, to_state in undocumented:
            record = AuthorizationRecord(
                authorization_id="a", authorization_version=1, envelope_id="e",
                envelope_version=1, current_state=from_state, history=(),
            )
            with pytest.raises(IllegalTransitionError):
                record.transition(to_state, evidence={"anything": "x"}, at=FIXED_TIME)

    def test_skipping_a_stage_fails(self):
        record = _new_record()
        with pytest.raises(IllegalTransitionError):
            record.transition(S.SUBMITTED, evidence={"x": "y"}, at=FIXED_TIME)


class TestTerminalStates:

    def test_all_seven_documented_terminal_states(self):
        assert TERMINAL_STATES == frozenset({
            S.PREFLIGHT_FAILED, S.TECHNICAL_VALIDATION_FAILED, S.REJECTED,
            S.CONSUMED, S.INVALIDATED_SOURCE_CHANGED, S.SUPERSEDED, S.CANCELLED,
        })

    def test_terminal_states_cannot_transition(self):
        for terminal in TERMINAL_STATES:
            record = AuthorizationRecord(
                authorization_id="a", authorization_version=1, envelope_id="e",
                envelope_version=1, current_state=terminal, history=(),
            )
            with pytest.raises(TerminalStateError):
                record.transition(S.DRAFT, evidence={"x": "y"}, at=FIXED_TIME)

    def test_consumed_authorization_cannot_submit_again(self):
        record = _new_record()
        for state in _HAPPY_PATH:
            record = _advance(record, state)
        assert record.current_state == S.CONSUMED
        with pytest.raises(TerminalStateError):
            record.transition(S.SUBMITTING, evidence={"gate_checks_passed": True}, at=FIXED_TIME)

    def test_invalidated_authorization_cannot_submit(self):
        record = _new_record()
        record = _advance(record, S.COMPILED)
        record = _advance(record, S.AWAITING_AUTHORIZATION)
        record = _advance(record, S.AUTHORIZED)
        record = _advance(record, S.INVALIDATED_SOURCE_CHANGED)
        assert record.current_state == S.INVALIDATED_SOURCE_CHANGED
        with pytest.raises(TerminalStateError):
            record.transition(S.SUBMITTING, evidence={"gate_checks_passed": True}, at=FIXED_TIME)


class TestSourceChangeInvalidation:

    def test_source_change_invalidates_instead_of_modifying(self):
        record = _new_record()
        record = _advance(record, S.COMPILED)
        record = _advance(record, S.AWAITING_AUTHORIZATION)
        record = _advance(
            record, S.INVALIDATED_SOURCE_CHANGED,
            changed_dependency="plan", old_checksum="a" * 64, new_checksum="b" * 64,
        )
        assert record.current_state == S.INVALIDATED_SOURCE_CHANGED
        assert record.is_terminal()
        # the record's envelope reference is unchanged -- invalidation
        # never edits envelope_id/envelope_version in place
        assert record.envelope_id == "env_1"
        assert record.envelope_version == 1
        last_event = record.history[-1]
        assert last_event.evidence["changed_dependency"] == "plan"

    def test_supersession_links_to_new_envelope_and_preserves_old_record(self):
        old_record = _new_record()
        old_record = _advance(old_record, S.COMPILED)

        updated_old, new_record = create_superseding_authorization(
            old_record, new_authorization_id="auth_2", new_envelope_id="env_2",
            new_envelope_version=1, at=FIXED_TIME, evidence={"reason": "recompiled before authorization"},
        )

        assert updated_old.current_state == S.SUPERSEDED
        assert updated_old.is_terminal()
        assert updated_old.envelope_id == "env_1"  # old record's own envelope reference untouched
        assert updated_old.history[-1].evidence["superseding_envelope_id"] == "env_2"

        assert new_record.current_state == S.DRAFT
        assert new_record.envelope_id == "env_2"
        assert new_record.superseded_authorization_id == "auth_1"

        # the original object passed in is never mutated
        assert old_record.current_state == S.COMPILED


class TestTransitionEvidence:

    def test_missing_required_evidence_blocks_transition(self):
        record = _new_record()
        with pytest.raises(MissingTransitionEvidenceError) as exc:
            record.transition(S.COMPILED, evidence={}, at=FIXED_TIME)
        assert "compiler_identity" in exc.value.missing_keys

    def test_authorization_requires_authorized_by_and_method(self):
        record = _new_record()
        record = _advance(record, S.COMPILED)
        record = _advance(record, S.AWAITING_AUTHORIZATION)
        with pytest.raises(MissingTransitionEvidenceError) as exc:
            record.transition(S.AUTHORIZED, evidence={"authorized_by": "someone"}, at=FIXED_TIME)
        assert "authorization_method" in exc.value.missing_keys

    def test_human_review_requires_reviewer_and_verdict(self):
        record = _new_record()
        for state in (
            S.COMPILED, S.AWAITING_AUTHORIZATION, S.AUTHORIZED, S.SUBMITTING,
            S.SUBMITTED, S.DOWNLOADED, S.PENDING_EDITORIAL_REVIEW,
        ):
            record = _advance(record, state)
        with pytest.raises(MissingTransitionEvidenceError):
            record.transition(S.QUALIFIED, evidence={"reviewer": "Mohamed"}, at=FIXED_TIME)


class TestAppendOnlyDeterministicHistory:

    def test_history_is_append_only_and_previous_events_are_immutable(self):
        record = _new_record()
        record_after_1 = _advance(record, S.COMPILED)
        record_after_2 = _advance(record_after_1, S.AWAITING_AUTHORIZATION)

        assert record.history == ()
        assert len(record_after_1.history) == 1
        assert len(record_after_2.history) == 2
        # the earlier record's history tuple is untouched by the later transition
        assert record_after_1.history == record_after_2.history[:1]

    def test_transition_does_not_mutate_the_original_record(self):
        record = _new_record()
        _advance(record, S.COMPILED)
        assert record.current_state == S.DRAFT
        assert record.history == ()

    def test_history_deterministic_given_same_inputs(self):
        r1 = _new_record()
        r1 = _advance(r1, S.COMPILED, compiler_identity="x", compiler_version="y")
        r2 = _new_record()
        r2 = _advance(r2, S.COMPILED, compiler_identity="x", compiler_version="y")
        assert r1.history == r2.history


class TestNoNetworkPath:

    def test_module_imports_no_network_capable_library(self):
        import hypogeum.authorization_lifecycle as mod
        source = open(mod.__file__).read()
        for forbidden in ("import requests", "import httpx", "urllib.request", "socket"):
            assert forbidden not in source, forbidden

    def test_module_never_calls_wall_clock(self):
        import hypogeum.authorization_lifecycle as mod
        source = open(mod.__file__).read()
        for forbidden in ("datetime.now(", "time.time(", "utcnow("):
            assert forbidden not in source, forbidden
