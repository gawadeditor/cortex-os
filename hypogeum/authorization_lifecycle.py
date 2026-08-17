"""
hypogeum/authorization_lifecycle.py — Authorization lifecycle state machine
(Factory Core)

Implements Commit 1 of docs/architecture/AUTHORIZED_GENERATION_ENVELOPE_
IMPLEMENTATION_BLUEPRINT.md: the state model specified by docs/
architecture/AUTHORIZED_GENERATION_ENVELOPE.md §Phase 4.

Pure state-machine logic only. No persistence, no network access, no
submission gate, no episode-file I/O. This module never mutates a
hypogeum.execution_envelope.GenerationExecutionEnvelope -- it maintains
its own, separate, append-only history of what happened to one
authorization over time, referencing an envelope only by
(envelope_id, envelope_version).

Every transition is deterministic given its inputs: this module never
calls a wall clock internally. Callers supply `at` explicitly (an
ISO-8601 string), which is what keeps tests deterministic and is also
what a future persistence layer will want to control.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

__all__ = [
    "AuthorizationState",
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATES",
    "REQUIRED_TRANSITION_EVIDENCE_KEYS",
    "AuthorizationLifecycleError",
    "IllegalTransitionError",
    "TerminalStateError",
    "MissingTransitionEvidenceError",
    "TransitionEvent",
    "AuthorizationRecord",
    "create_superseding_authorization",
    "correct_technical_validation_failure",
    "get_technical_validation_failure_reason",
]


class AuthorizationState(str, Enum):
    DRAFT = "draft"
    COMPILED = "compiled"
    PREFLIGHT_FAILED = "preflight_failed"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    AWAITING_EXPLICIT_AUTHORIZATION = "awaiting_explicit_authorization"
    AUTHORIZED = "authorized"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    DOWNLOADED = "downloaded"
    TECHNICAL_VALIDATION_FAILED = "technical_validation_failed"
    PENDING_EDITORIAL_REVIEW = "pending_editorial_review"
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    ALLOCATED = "allocated"
    CONSUMED = "consumed"
    INVALIDATED_SOURCE_CHANGED = "invalidated_source_changed"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"


# Exactly the edges documented in AUTHORIZED_GENERATION_ENVELOPE.md §4.1's
# diagram plus its immediately-following prose ("awaiting_authorization,
# authorized, and submitting may each also transition directly to
# invalidated_source_changed or cancelled"). Never redesigned here --
# every edge below is traceable to that document.
S = AuthorizationState
ALLOWED_TRANSITIONS: frozenset = frozenset({
    (S.DRAFT, S.COMPILED),

    (S.COMPILED, S.PREFLIGHT_FAILED),
    (S.COMPILED, S.AWAITING_AUTHORIZATION),
    (S.COMPILED, S.SUPERSEDED),

    (S.AWAITING_AUTHORIZATION, S.AUTHORIZED),
    (S.AWAITING_AUTHORIZATION, S.AWAITING_EXPLICIT_AUTHORIZATION),
    (S.AWAITING_AUTHORIZATION, S.INVALIDATED_SOURCE_CHANGED),
    (S.AWAITING_AUTHORIZATION, S.CANCELLED),

    # Reachable only from awaiting_authorization, only after 88/88 (or
    # N/N) Observation-Only has passed (plan §5.5/§6 Commit M) --
    # itself transitions to authorized ONLY via a separate, later,
    # explicit-approval event (a verified ApprovalArtifact, never
    # automatically) -- see hypogeum.authorization_authority.
    # issue_authorization_from_explicit_approval(). The same
    # invalidation/cancellation safety valves every other pre-authorized
    # state has remain available while a proposal sits here.
    (S.AWAITING_EXPLICIT_AUTHORIZATION, S.AUTHORIZED),
    (S.AWAITING_EXPLICIT_AUTHORIZATION, S.INVALIDATED_SOURCE_CHANGED),
    (S.AWAITING_EXPLICIT_AUTHORIZATION, S.CANCELLED),

    (S.AUTHORIZED, S.SUBMITTING),
    (S.AUTHORIZED, S.INVALIDATED_SOURCE_CHANGED),
    (S.AUTHORIZED, S.CANCELLED),

    (S.SUBMITTING, S.SUBMITTED),
    (S.SUBMITTING, S.INVALIDATED_SOURCE_CHANGED),
    (S.SUBMITTING, S.CANCELLED),

    (S.SUBMITTED, S.DOWNLOADED),

    (S.DOWNLOADED, S.TECHNICAL_VALIDATION_FAILED),
    (S.DOWNLOADED, S.PENDING_EDITORIAL_REVIEW),

    (S.PENDING_EDITORIAL_REVIEW, S.QUALIFIED),
    (S.PENDING_EDITORIAL_REVIEW, S.REJECTED),

    (S.QUALIFIED, S.ALLOCATED),

    (S.ALLOCATED, S.CONSUMED),
})

TERMINAL_STATES: frozenset = frozenset({
    S.PREFLIGHT_FAILED,
    S.TECHNICAL_VALIDATION_FAILED,
    S.REJECTED,
    S.CONSUMED,
    S.INVALIDATED_SOURCE_CHANGED,
    S.SUPERSEDED,
    S.CANCELLED,
})

# Required evidence keys per transition (§4.2's ownership table + Commit 1
# requirement 5: "Require appropriate evidence for transitions that need:
# authorization; provider submission; download; validation; human review;
# allocation; invalidation; supersession."). Every transition requires at
# least one evidence key -- there is no "free" transition.
REQUIRED_TRANSITION_EVIDENCE_KEYS: Mapping[tuple, tuple] = {
    (S.DRAFT, S.COMPILED): ("compiler_identity", "compiler_version"),

    (S.COMPILED, S.PREFLIGHT_FAILED): ("failure_reason",),
    (S.COMPILED, S.AWAITING_AUTHORIZATION): ("preflight_result",),
    (S.COMPILED, S.SUPERSEDED): ("superseding_envelope_id",),

    (S.AWAITING_AUTHORIZATION, S.AUTHORIZED): ("authorized_by", "authorization_method"),
    (S.AWAITING_AUTHORIZATION, S.AWAITING_EXPLICIT_AUTHORIZATION): (
        "observation_only_pass_count", "observation_only_total_count", "proposal_artifact_checksum",
    ),
    (S.AWAITING_AUTHORIZATION, S.INVALIDATED_SOURCE_CHANGED): (
        "changed_dependency", "old_checksum", "new_checksum",
    ),
    (S.AWAITING_AUTHORIZATION, S.CANCELLED): ("cancelled_by", "reason"),

    (S.AWAITING_EXPLICIT_AUTHORIZATION, S.AUTHORIZED): (
        "authorized_by", "authorization_method", "approval_artifact_id", "approval_subject_checksum",
    ),
    (S.AWAITING_EXPLICIT_AUTHORIZATION, S.INVALIDATED_SOURCE_CHANGED): (
        "changed_dependency", "old_checksum", "new_checksum",
    ),
    (S.AWAITING_EXPLICIT_AUTHORIZATION, S.CANCELLED): ("cancelled_by", "reason"),

    (S.AUTHORIZED, S.SUBMITTING): ("gate_checks_passed",),
    (S.AUTHORIZED, S.INVALIDATED_SOURCE_CHANGED): (
        "changed_dependency", "old_checksum", "new_checksum",
    ),
    (S.AUTHORIZED, S.CANCELLED): ("cancelled_by", "reason"),

    (S.SUBMITTING, S.SUBMITTED): ("provider_job_id",),
    (S.SUBMITTING, S.INVALIDATED_SOURCE_CHANGED): (
        "changed_dependency", "old_checksum", "new_checksum",
    ),
    (S.SUBMITTING, S.CANCELLED): ("cancelled_by", "reason"),

    (S.SUBMITTED, S.DOWNLOADED): ("output_checksum",),

    (S.DOWNLOADED, S.TECHNICAL_VALIDATION_FAILED): ("failure_reason",),
    (S.DOWNLOADED, S.PENDING_EDITORIAL_REVIEW): ("technical_validation_result",),

    (S.PENDING_EDITORIAL_REVIEW, S.QUALIFIED): ("reviewer", "verdict"),
    (S.PENDING_EDITORIAL_REVIEW, S.REJECTED): ("reviewer", "verdict"),

    (S.QUALIFIED, S.ALLOCATED): ("allocated_to",),

    (S.ALLOCATED, S.CONSUMED): ("registration_id",),
}


class AuthorizationLifecycleError(Exception):
    """Base class for every lifecycle failure in this module."""


class IllegalTransitionError(AuthorizationLifecycleError):
    """Raised when (from_state, to_state) is not in ALLOWED_TRANSITIONS."""

    def __init__(self, from_state: AuthorizationState, to_state: AuthorizationState):
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"illegal transition: {from_state.value} -> {to_state.value}")


class TerminalStateError(AuthorizationLifecycleError):
    """Raised when attempting any transition out of a terminal state."""

    def __init__(self, state: AuthorizationState):
        self.state = state
        super().__init__(f"{state.value} is a terminal state; no further transition is possible")


class MissingTransitionEvidenceError(AuthorizationLifecycleError):
    """Raised when a transition's required evidence keys are not all
    present (or present with a falsy value) in the supplied evidence
    dict."""

    def __init__(self, from_state: AuthorizationState, to_state: AuthorizationState, missing_keys: tuple):
        self.from_state = from_state
        self.to_state = to_state
        self.missing_keys = missing_keys
        super().__init__(
            f"transition {from_state.value} -> {to_state.value} is missing required evidence key(s): {missing_keys}"
        )


@dataclass(frozen=True)
class TransitionEvent:
    """One immutable, append-only record of a state transition. Never
    edited after creation -- AuthorizationRecord.transition() always
    produces a *new* AuthorizationRecord whose history is the old
    history plus exactly one new TransitionEvent, never a mutation of an
    existing one."""

    from_state: AuthorizationState
    to_state: AuthorizationState
    at: str
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class AuthorizationRecord:
    """The mutable-over-time (but never mutated in place) record of one
    authorization's lifecycle. References a
    hypogeum.execution_envelope.GenerationExecutionEnvelope by identity
    only (envelope_id/envelope_version) -- never embeds or mutates the
    envelope itself.

    Every transition returns a brand new AuthorizationRecord; the
    original instance is left untouched, matching the architecture's
    "immutable transition events" requirement. A caller that wants
    "the current record" always uses the most recently returned value;
    nothing in this module deletes or hides an older one.
    """

    authorization_id: str
    authorization_version: int
    envelope_id: str
    envelope_version: int
    current_state: AuthorizationState
    history: tuple  # tuple[TransitionEvent, ...]
    superseded_authorization_id: Optional[str] = None

    @classmethod
    def create(
        cls, *, authorization_id: str, authorization_version: int, envelope_id: str, envelope_version: int,
        superseded_authorization_id: Optional[str] = None,
    ) -> "AuthorizationRecord":
        """A brand-new authorization record, starting in `draft` with an
        empty history. Does not itself constitute a transition -- the
        first real transition (draft -> compiled) is a separate call.

        authorization_version is REQUIRED, never defaulted: a record's
        own authorization_version must always be the exact value the
        matching GenerationExecutionEnvelope.authorization_version
        already carries (it is part of that envelope's own identity
        payload) -- hypogeum.submission_gate.submit()'s
        04_authorization_version_match check compares the two exactly
        and denies on any mismatch. A silent default here previously
        let every caller accidentally build version-1 records for
        version-N envelopes; every caller must now say explicitly which
        version this record is for."""
        return cls(
            authorization_id=authorization_id,
            authorization_version=authorization_version,
            envelope_id=envelope_id,
            envelope_version=envelope_version,
            current_state=AuthorizationState.DRAFT,
            history=(),
            superseded_authorization_id=superseded_authorization_id,
        )

    def transition(
        self, to_state: AuthorizationState, *, evidence: Mapping[str, Any], at: str,
    ) -> "AuthorizationRecord":
        """Returns a new AuthorizationRecord advanced to to_state. Raises
        (never silently no-ops or auto-corrects):
          - TerminalStateError if self.current_state is already terminal
            (this is what prevents double authorization, double
            submission, submission after consumption, submission after
            invalidation, and every other "the record is already done"
            case -- there is exactly one guard, not one per case).
          - IllegalTransitionError if (current_state, to_state) is not a
            documented edge.
          - MissingTransitionEvidenceError if any required evidence key
            for this exact transition is absent or falsy.
        Never mutates self or any prior TransitionEvent."""
        if self.current_state in TERMINAL_STATES:
            raise TerminalStateError(self.current_state)

        edge = (self.current_state, to_state)
        if edge not in ALLOWED_TRANSITIONS:
            raise IllegalTransitionError(self.current_state, to_state)

        required_keys = REQUIRED_TRANSITION_EVIDENCE_KEYS.get(edge, ())
        missing = tuple(k for k in required_keys if not evidence.get(k))
        if missing:
            raise MissingTransitionEvidenceError(self.current_state, to_state, missing)

        event = TransitionEvent(
            from_state=self.current_state, to_state=to_state, at=at, evidence=dict(evidence),
        )
        return AuthorizationRecord(
            authorization_id=self.authorization_id,
            authorization_version=self.authorization_version,
            envelope_id=self.envelope_id,
            envelope_version=self.envelope_version,
            current_state=to_state,
            history=self.history + (event,),
            superseded_authorization_id=self.superseded_authorization_id,
        )

    def is_terminal(self) -> bool:
        return self.current_state in TERMINAL_STATES


# One edge, one required-evidence set, defined LOCALLY here -- deliberately
# NOT added to the module-wide ALLOWED_TRANSITIONS/REQUIRED_TRANSITION_
# EVIDENCE_KEYS maps. Those maps are asserted, structurally, to make every
# one of their edges reachable via the ordinary AuthorizationRecord.
# transition() call (tests/test_authorization_lifecycle.py::
# TestLegalTransitionsSucceed::test_every_documented_transition_succeeds
# iterates ALLOWED_TRANSITIONS and requires every edge to succeed through
# transition() itself) -- an edge whose source is a terminal state can
# never satisfy that, since transition()'s single, unconditional guard
# (`if self.current_state in TERMINAL_STATES: raise TerminalStateError`)
# always fires first. Keeping this edge out of the shared maps preserves
# that guarantee, and the AUTHORIZED_GENERATION_ENVELOPE.md §4.1 diagram,
# completely unchanged.
_TECHNICAL_VALIDATION_CORRECTION_REQUIRED_EVIDENCE_KEYS = (
    "technical_validation_result", "correction_reason", "corrected_by",
)


def get_technical_validation_failure_reason(record: AuthorizationRecord) -> Optional[str]:
    """Reads the exact `failure_reason` evidence of a record's own MOST
    RECENT (DOWNLOADED -> TECHNICAL_VALIDATION_FAILED) transition --
    the single, deterministic source of truth for "what reason was
    this record actually closed for," never inferred, guessed, or
    accepted from a caller. Returns None if the record has no such
    transition in its history (e.g. is not currently, and never was,
    TECHNICAL_VALIDATION_FAILED). Used both by correct_technical_
    validation_failure() below (to stamp the evidence trail) and by
    hypogeum.authorization_store.correct_technical_validation_failure()
    (to enforce that only ONE specific, known tooling-defect signature
    is ever eligible for correction -- see that function's own
    docstring)."""
    original_failure_event = next(
        (h for h in reversed(record.history) if h.to_state == AuthorizationState.TECHNICAL_VALIDATION_FAILED), None,
    )
    return original_failure_event.evidence.get("failure_reason") if original_failure_event else None


def correct_technical_validation_failure(
    record: AuthorizationRecord, *, evidence: Mapping[str, Any], at: str,
) -> AuthorizationRecord:
    """
    The ONE narrow, explicit exception to AuthorizationRecord.
    transition()'s blanket "terminal states never transition further"
    guard. Exists for exactly one scenario: a record was moved to
    TECHNICAL_VALIDATION_FAILED because of a genuine defect in the
    VALIDATING caller's own tooling (e.g. a crash before any real
    inspection ran) -- never because of a real defect in the
    underlying asset -- and real technical validation evidence now
    exists, produced separately, proving the asset is actually valid.

    Every other terminal state (PREFLIGHT_FAILED, REJECTED, CONSUMED,
    INVALIDATED_SOURCE_CHANGED, SUPERSEDED, CANCELLED) -- and every
    OTHER record at TECHNICAL_VALIDATION_FAILED for a genuine asset
    defect -- remains fully, unconditionally terminal: this function
    only ever accepts a record already at TECHNICAL_VALIDATION_FAILED,
    only ever produces PENDING_EDITORIAL_REVIEW, and is not a general
    escape hatch usable from any other call site or state.
    AuthorizationRecord.transition()'s own guard is completely
    untouched by this function's existence -- every other real call
    site in this codebase still hits it, unweakened.

    This function itself is content-agnostic about WHAT the original
    failure reason was or whether the fresh evidence is trustworthy --
    it is pure state-machine mechanics (guard + evidence-key presence +
    the one transition). Content-based policy (the original reason must
    exactly match a known tooling-defect signature; the fresh evidence
    must show a real, structured pass, not merely an empty failures
    list) is deliberately NOT enforced here -- see hypogeum.
    authorization_store.correct_technical_validation_failure(), the
    only sanctioned caller, which enforces it before ever reaching this
    function.

    original_failure_reason is never accepted as caller-supplied
    evidence -- it is read directly from the record's OWN stored
    history via get_technical_validation_failure_reason() (the exact
    evidence its own TECHNICAL_VALIDATION_FAILED transition already
    carries), so a caller cannot misattribute a different original
    reason than what genuinely happened, and it is always included in
    the new evidence dict verbatim, preserved.
    """
    if record.current_state != AuthorizationState.TECHNICAL_VALIDATION_FAILED:
        raise TerminalStateError(record.current_state)

    missing = tuple(
        k for k in _TECHNICAL_VALIDATION_CORRECTION_REQUIRED_EVIDENCE_KEYS if not evidence.get(k)
    )
    if missing:
        raise MissingTransitionEvidenceError(
            record.current_state, AuthorizationState.PENDING_EDITORIAL_REVIEW, missing,
        )

    original_failure_reason = get_technical_validation_failure_reason(record)

    full_evidence = {**dict(evidence), "original_failure_reason": original_failure_reason}
    event = TransitionEvent(
        from_state=record.current_state, to_state=AuthorizationState.PENDING_EDITORIAL_REVIEW,
        at=at, evidence=full_evidence,
    )
    return AuthorizationRecord(
        authorization_id=record.authorization_id, authorization_version=record.authorization_version,
        envelope_id=record.envelope_id, envelope_version=record.envelope_version,
        current_state=AuthorizationState.PENDING_EDITORIAL_REVIEW, history=record.history + (event,),
        superseded_authorization_id=record.superseded_authorization_id,
    )


def create_superseding_authorization(
    old_record: AuthorizationRecord, *, new_authorization_id: str, new_authorization_version: int,
    new_envelope_id: str, new_envelope_version: int, at: str, evidence: Mapping[str, Any],
) -> tuple:
    """Implements §4.4's source-change-invalidation flow at the record
    level: transitions old_record to `superseded` (never edits its
    checksum or content in place) and returns a brand-new
    AuthorizationRecord referencing the new envelope, linked back to the
    old one via superseded_authorization_id.

    old_record must currently be in `compiled` -- the one state
    §4.1's diagram documents a direct edge to `superseded` from. A
    record already further along (authorized/submitting/etc.) that needs
    to be superseded due to a source change is invalidated via
    transition(INVALIDATED_SOURCE_CHANGED, ...) instead; this function is
    specifically the "we recompiled before ever authorizing this one"
    path.

    Returns (updated_old_record, new_record). The old record's full
    history, including this final transition, remains immutable audit
    evidence -- it is returned, never discarded."""
    updated_old = old_record.transition(
        AuthorizationState.SUPERSEDED,
        evidence={**evidence, "superseding_envelope_id": new_envelope_id},
        at=at,
    )
    new_record = AuthorizationRecord.create(
        authorization_id=new_authorization_id,
        authorization_version=new_authorization_version,
        envelope_id=new_envelope_id,
        envelope_version=new_envelope_version,
        superseded_authorization_id=old_record.authorization_id,
    )
    return updated_old, new_record
