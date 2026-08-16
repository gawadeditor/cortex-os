"""
tests/test_reconcile_unknown_acceptance.py — Focused tests for
hypogeum.provider_receipt.reconcile_unknown_acceptance_as_not_submitted()
and the structural proof that hypogeum.providers.muapi_video_adapter.
MuapiVideoAdapter._invoke_real() decodes its payload BEFORE ever
calling _submit_http() (Phase 1 requirement 1 of the pre-HTTP
reconciliation this function exists to support).

Every test uses a temporary isolated filesystem root (tmp_path) and a
temporary isolated SQLite database. No test reads or mutates live
episode state, and no test imports or invokes a network/provider
client.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from pathlib import Path

import pytest

import hypogeum.budget_scopes as bs
import hypogeum.db as db
from hypogeum.provider_receipt import (
    AlreadyReconciledWithDifferentResolutionError,
    ELIGIBLE_FOR_SUBMISSION_STATES,
    EventType,
    ProviderReceiptEvent,
    RecoveryState,
    WriteAheadReceiptStore,
    reconcile_unknown_acceptance_as_not_submitted,
)
from hypogeum.providers.muapi_video_adapter import MuapiVideoAdapter

EXEC_ID = "exec_test_1"
IDEM_KEY = "idem_test_1"


class _FakeEnvelope:
    envelope_id = "env_1"
    envelope_version = 1
    authorization_id = "auth_1"
    authorization_version = 3
    provider = "muapi"
    model = "veo3.1-lite-text-to-video"
    provider_operation = "video.text_to_video"
    operation_payload_sha256 = "a" * 64
    max_cost_per_call = Decimal("0.39")
    currency = "USD"


def _event(event_type: EventType, event_seq: int, evidence: dict = None, **overrides) -> ProviderReceiptEvent:
    kwargs = dict(
        envelope_id="env_1", envelope_version=1, authorization_id="auth_1",
        authorization_version=3, attempt_id=IDEM_KEY, idempotency_key=IDEM_KEY,
        event_type=event_type, event_seq=event_seq, at="2026-01-01T00:00:00Z",
        provider="muapi", model="veo3.1-lite-text-to-video", provider_operation="video.text_to_video",
        provider_bound_content_sha256="a" * 64, max_cost_per_call=Decimal("0.39"),
        currency="USD", evidence=evidence or {},
    )
    kwargs.update(overrides)
    return ProviderReceiptEvent(**kwargs)


def _prepared_store_with_unknown_acceptance(tmp_path: Path) -> WriteAheadReceiptStore:
    store = WriteAheadReceiptStore(tmp_path)
    store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0), execution_id=EXEC_ID)
    store.append_event(_event(EventType.SUBMISSION_ATTEMPT_STARTED, 1), execution_id=EXEC_ID)
    store.append_event(
        _event(EventType.UNKNOWN_ACCEPTANCE, 2, {"detected_by": "submission_gate", "reason": "adapter raised: x"}),
        execution_id=EXEC_ID,
    )
    return store


def _db_conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.initialize_schema(db_path)
    return db.get_connection(db_path)


def _reserve_call_and_episode(conn):
    from hypogeum.budget_scopes import BudgetScopeKind, ScopeRequest, reserve_all_scopes
    reserve_all_scopes(
        conn,
        [
            ScopeRequest(BudgetScopeKind.CALL, IDEM_KEY, Decimal("0.39"), "USD"),
            ScopeRequest(BudgetScopeKind.EPISODE, "ep_test", Decimal("0.39"), "USD"),
        ],
        idempotency_key=IDEM_KEY, amount=Decimal("0.39"), currency="USD",
        at="2026-01-01T00:00:00Z", reservation_id_prefix=IDEM_KEY,
    )


class TestStructuralProofDecodeBeforeSubmitHttp:

    def test_decode_video_payload_appears_before_submit_http_in_source(self):
        source = inspect.getsource(MuapiVideoAdapter._invoke_real)
        decode_pos = source.index("decode_video_payload(")
        submit_pos = source.index("_submit_http(")
        assert decode_pos < submit_pos, (
            "MuapiVideoAdapter._invoke_real() no longer decodes the payload before calling "
            "_submit_http() -- the pre-HTTP reconciliation proof this test guards no longer holds"
        )


class TestReconcileUnknownAcceptanceAsNotSubmitted:

    def test_appends_reconciled_event_and_reclassifies_recovery_state(self, tmp_path):
        store = _prepared_store_with_unknown_acceptance(tmp_path)
        conn = _db_conn(tmp_path)
        try:
            _reserve_call_and_episode(conn)

            before = store.classify_recovery_state(EXEC_ID, IDEM_KEY)
            assert before == RecoveryState.UNKNOWN_ACCEPTANCE_REQUIRES_RECONCILIATION

            event = reconcile_unknown_acceptance_as_not_submitted(
                store, conn, execution_id=EXEC_ID, envelope=_FakeEnvelope(), idempotency_key=IDEM_KEY,
                reservation_ids=[f"{IDEM_KEY}:call", f"{IDEM_KEY}:episode"],
                proof="decode_video_payload() raised before _submit_http() was reached (structurally proven)",
                at="2026-01-02T00:00:00Z",
            )
            assert event.event_type == EventType.RECONCILED
            assert event.evidence["resolution"] == "confirmed_not_submitted"

            after = store.classify_recovery_state(EXEC_ID, IDEM_KEY)
            assert after == RecoveryState.TERMINALLY_FAILED_BEFORE_ACCEPTANCE
            assert after not in ELIGIBLE_FOR_SUBMISSION_STATES  # never retryable through this reconciliation
        finally:
            conn.close()

    def test_releases_reservations_still_reserved(self, tmp_path):
        store = _prepared_store_with_unknown_acceptance(tmp_path)
        conn = _db_conn(tmp_path)
        try:
            _reserve_call_and_episode(conn)

            reconcile_unknown_acceptance_as_not_submitted(
                store, conn, execution_id=EXEC_ID, envelope=_FakeEnvelope(), idempotency_key=IDEM_KEY,
                reservation_ids=[f"{IDEM_KEY}:call", f"{IDEM_KEY}:episode"],
                proof="proven pre-HTTP failure", at="2026-01-02T00:00:00Z",
            )

            call_row = db.get_budget_reservation(conn, f"{IDEM_KEY}:call")
            episode_row = db.get_budget_reservation(conn, f"{IDEM_KEY}:episode")
            assert call_row["status"] == "released"
            assert episode_row["status"] == "released"
            assert call_row["committed_amount"] is None
            assert episode_row["committed_amount"] is None
        finally:
            conn.close()

    def test_preserves_original_events_history_untouched(self, tmp_path):
        store = _prepared_store_with_unknown_acceptance(tmp_path)
        conn = _db_conn(tmp_path)
        try:
            before_events = store.read_events(EXEC_ID, IDEM_KEY)
            reconcile_unknown_acceptance_as_not_submitted(
                store, conn, execution_id=EXEC_ID, envelope=_FakeEnvelope(), idempotency_key=IDEM_KEY,
                reservation_ids=[], proof="proven pre-HTTP failure", at="2026-01-02T00:00:00Z",
            )
            after_events = store.read_events(EXEC_ID, IDEM_KEY)
            # Original 3 events preserved byte-identical, plus exactly one new RECONCILED event appended.
            assert len(after_events) == len(before_events) + 1
            for b, a in zip(before_events, after_events[:len(before_events)]):
                assert b.canonical_json_bytes() == a.canonical_json_bytes()
            assert after_events[-1].event_type == EventType.RECONCILED
        finally:
            conn.close()

    def test_idempotent_replay_does_not_duplicate_or_reraise(self, tmp_path):
        store = _prepared_store_with_unknown_acceptance(tmp_path)
        conn = _db_conn(tmp_path)
        try:
            first = reconcile_unknown_acceptance_as_not_submitted(
                store, conn, execution_id=EXEC_ID, envelope=_FakeEnvelope(), idempotency_key=IDEM_KEY,
                reservation_ids=[], proof="proven pre-HTTP failure", at="2026-01-02T00:00:00Z",
            )
            second = reconcile_unknown_acceptance_as_not_submitted(
                store, conn, execution_id=EXEC_ID, envelope=_FakeEnvelope(), idempotency_key=IDEM_KEY,
                reservation_ids=[], proof="different proof text on replay -- irrelevant, no new event",
                at="2026-01-03T00:00:00Z",
            )
            assert first.canonical_json_bytes() == second.canonical_json_bytes()
            events = store.read_events(EXEC_ID, IDEM_KEY)
            assert sum(1 for e in events if e.event_type == EventType.RECONCILED) == 1
        finally:
            conn.close()

    def test_conflicting_prior_resolution_fails_closed(self, tmp_path):
        store = _prepared_store_with_unknown_acceptance(tmp_path)
        store.append_event(
            _event(EventType.RECONCILED, 3, {"resolution": "confirmed_accepted"}), execution_id=EXEC_ID,
        )
        conn = _db_conn(tmp_path)
        try:
            with pytest.raises(AlreadyReconciledWithDifferentResolutionError):
                reconcile_unknown_acceptance_as_not_submitted(
                    store, conn, execution_id=EXEC_ID, envelope=_FakeEnvelope(), idempotency_key=IDEM_KEY,
                    reservation_ids=[], proof="proven pre-HTTP failure", at="2026-01-02T00:00:00Z",
                )
            events = store.read_events(EXEC_ID, IDEM_KEY)
            assert sum(1 for e in events if e.event_type == EventType.RECONCILED) == 1
        finally:
            conn.close()
