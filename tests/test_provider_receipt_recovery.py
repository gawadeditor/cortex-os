"""
tests/test_provider_receipt_recovery.py — Unit tests for
hypogeum/provider_receipt.py + hypogeum/db.py's runtime_leases helpers
(Commit 2, 2026-08-15)

Every test uses a temporary isolated filesystem root (tmp_path) and/or a
temporary isolated SQLite database (tmp_path / "test.db"). No test reads
or mutates live episode state, and no test imports or invokes a
network/provider client.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

import hypogeum.db as db
from hypogeum.provider_receipt import (
    DuplicateEventError,
    ELIGIBLE_FOR_SUBMISSION_STATES,
    EventType,
    LeaseRequiredError,
    MissingAcknowledgementError,
    ProviderReceiptEvent,
    ReceiptValidationError,
    RecoveryState,
    WriteAheadReceiptStore,
)

EXEC_ID = "exec_test_1"
IDEM_KEY = "idem_test_1"


def _event(event_type: EventType, event_seq: int, evidence: dict = None, **overrides) -> ProviderReceiptEvent:
    kwargs = dict(
        envelope_id="env_1", envelope_version=1, authorization_id="auth_1",
        authorization_version=1, attempt_id="attempt_1", idempotency_key=IDEM_KEY,
        event_type=event_type, event_seq=event_seq, at="2026-01-01T00:00:00Z",
        provider="muapi", model="veo3.1-lite-text-to-video", provider_operation="video.text_to_video",
        provider_bound_content_sha256="a" * 64, max_cost_per_call=Decimal("0.39"),
        currency="USD", evidence=evidence or {},
    )
    kwargs.update(overrides)
    return ProviderReceiptEvent(**kwargs)


def _store(tmp_path: Path) -> WriteAheadReceiptStore:
    return WriteAheadReceiptStore(tmp_path)


def _db_conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.initialize_schema(db_path)
    return db.get_connection(db_path)


class TestWriteAheadBeforeSubmissionEligible:

    def test_evidence_durably_stored_before_eligible(self, tmp_path):
        store = _store(tmp_path)
        assert store.classify_recovery_state(EXEC_ID, IDEM_KEY) == RecoveryState.AUTHORIZED_NO_ATTEMPT
        assert RecoveryState.AUTHORIZED_NO_ATTEMPT in ELIGIBLE_FOR_SUBMISSION_STATES

        store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0), execution_id=EXEC_ID)
        state = store.classify_recovery_state(EXEC_ID, IDEM_KEY)
        assert state == RecoveryState.ATTEMPT_PREPARED_NOT_ENTERED
        # once prepared, it is no longer "authorized but nothing prepared" --
        # not eligible for a *second* fresh preparation attempt
        assert state not in ELIGIBLE_FOR_SUBMISSION_STATES

    def test_reading_before_any_prepare_raises_nothing_and_returns_empty(self, tmp_path):
        store = _store(tmp_path)
        assert store.read_events(EXEC_ID, "never_prepared") == []


class TestTransactionFailureLeavesNoPartialEvidence:

    def test_invalid_event_construction_writes_nothing(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(ReceiptValidationError):
            _event(EventType.ABOUT_TO_SUBMIT, 0, currency="not-a-currency")
        attempt_dir = tmp_path / "episodes" / EXEC_ID / "receipts" / IDEM_KEY
        assert not attempt_dir.exists()

    def test_wrong_event_type_for_prepare_writes_nothing(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(ReceiptValidationError):
            store.prepare_attempt(_event(EventType.SUBMITTED, 0, {"provider_job_id": "x"}), execution_id=EXEC_ID)
        attempt_dir = tmp_path / "episodes" / EXEC_ID / "receipts" / IDEM_KEY
        assert not attempt_dir.exists() or not any(attempt_dir.iterdir())

    def test_link_failure_leaves_no_final_file(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        import os as os_module
        real_link = os_module.link

        def _boom(*a, **kw):
            raise OSError("simulated disk failure during link")

        monkeypatch.setattr(os_module, "link", _boom)
        with pytest.raises(OSError):
            store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0), execution_id=EXEC_ID)
        monkeypatch.setattr(os_module, "link", real_link)

        final_path = tmp_path / "episodes" / EXEC_ID / "receipts" / IDEM_KEY / "0000_about_to_submit.json"
        assert not final_path.exists()
        assert store.classify_recovery_state(EXEC_ID, IDEM_KEY) == RecoveryState.AUTHORIZED_NO_ATTEMPT


class TestRuntimeLeases:

    def test_two_workers_cannot_acquire_same_active_lease(self, tmp_path):
        conn = _db_conn(tmp_path)
        lease_a = db.acquire_lease(conn, "env_1", "worker_a", acquired_at="2026-01-01T00:00:00Z", expires_at="2026-01-01T00:05:00Z")
        assert lease_a is not None
        lease_b = db.acquire_lease(conn, "env_1", "worker_b", acquired_at="2026-01-01T00:00:01Z", expires_at="2026-01-01T00:05:01Z")
        assert lease_b is None

    def test_expired_lease_takeover_advances_fencing_token(self, tmp_path):
        conn = _db_conn(tmp_path)
        lease_a = db.acquire_lease(conn, "env_1", "worker_a", acquired_at="2026-01-01T00:00:00Z", expires_at="2026-01-01T00:05:00Z")
        assert lease_a["fencing_token"] == 1
        lease_b = db.acquire_lease(conn, "env_1", "worker_b", acquired_at="2026-01-01T00:10:00Z", expires_at="2026-01-01T00:15:00Z")
        assert lease_b is not None
        assert lease_b["fencing_token"] == 2
        assert lease_b["owner_id"] == "worker_b"

    def test_stale_owner_rejected_after_takeover(self, tmp_path):
        conn = _db_conn(tmp_path)
        db.acquire_lease(conn, "env_1", "worker_a", acquired_at="2026-01-01T00:00:00Z", expires_at="2026-01-01T00:05:00Z")
        db.acquire_lease(conn, "env_1", "worker_b", acquired_at="2026-01-01T00:10:00Z", expires_at="2026-01-01T00:15:00Z")

        assert db.validate_lease(conn, "env_1", "worker_a", 1, at="2026-01-01T00:11:00Z") is False
        assert db.renew_lease(conn, "env_1", "worker_a", 1, renewed_at="2026-01-01T00:11:00Z", new_expires_at="2026-01-01T00:20:00Z") is False
        assert db.release_lease(conn, "env_1", "worker_a", 1, released_at="2026-01-01T00:11:00Z") is False

        assert db.validate_lease(conn, "env_1", "worker_b", 2, at="2026-01-01T00:11:00Z") is True

    def test_renewal_requires_owner_and_fencing_token(self, tmp_path):
        conn = _db_conn(tmp_path)
        db.acquire_lease(conn, "env_1", "worker_a", acquired_at="2026-01-01T00:00:00Z", expires_at="2026-01-01T00:05:00Z")
        assert db.renew_lease(conn, "env_1", "worker_a", 1, renewed_at="2026-01-01T00:01:00Z", new_expires_at="2026-01-01T00:10:00Z") is True
        assert db.renew_lease(conn, "env_1", "other_worker", 1, renewed_at="2026-01-01T00:02:00Z", new_expires_at="2026-01-01T00:20:00Z") is False
        assert db.renew_lease(conn, "env_1", "worker_a", 99, renewed_at="2026-01-01T00:02:00Z", new_expires_at="2026-01-01T00:20:00Z") is False

    def test_release_is_auditable_not_deleted(self, tmp_path):
        conn = _db_conn(tmp_path)
        db.acquire_lease(conn, "env_1", "worker_a", acquired_at="2026-01-01T00:00:00Z", expires_at="2026-01-01T00:05:00Z")
        assert db.release_lease(conn, "env_1", "worker_a", 1, released_at="2026-01-01T00:01:00Z") is True
        row = db.get_lease(conn, "env_1")
        assert row is not None
        assert row["status"] == "released"
        assert row["released_at"] == "2026-01-01T00:01:00Z"


class TestLeaseProtectedAppend:

    def test_wrong_owner_cannot_append_protected_evidence(self, tmp_path):
        conn = _db_conn(tmp_path)
        store = _store(tmp_path)
        store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0), execution_id=EXEC_ID)
        db.acquire_lease(conn, "env_1", "worker_a", acquired_at="2026-01-01T00:00:00Z", expires_at="2026-01-01T00:05:00Z")

        with pytest.raises(LeaseRequiredError):
            store.append_event(
                _event(EventType.SUBMISSION_ATTEMPT_STARTED, 1),
                execution_id=EXEC_ID, lease_conn=conn, lease_owner_id="worker_wrong", lease_fencing_token=1,
            )

    def test_stale_fencing_token_cannot_append_after_takeover(self, tmp_path):
        conn = _db_conn(tmp_path)
        store = _store(tmp_path)
        store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0), execution_id=EXEC_ID)
        db.acquire_lease(conn, "env_1", "worker_a", acquired_at="2026-01-01T00:00:00Z", expires_at="2026-01-01T00:05:00Z")
        db.acquire_lease(conn, "env_1", "worker_b", acquired_at="2026-01-01T00:10:00Z", expires_at="2026-01-01T00:15:00Z")

        with pytest.raises(LeaseRequiredError):
            store.append_event(
                _event(EventType.SUBMISSION_ATTEMPT_STARTED, 1),
                execution_id=EXEC_ID, lease_conn=conn, lease_owner_id="worker_a", lease_fencing_token=1,
            )

        # current owner with current token succeeds
        result = store.append_event(
            _event(EventType.SUBMISSION_ATTEMPT_STARTED, 1),
            execution_id=EXEC_ID, lease_conn=conn, lease_owner_id="worker_b", lease_fencing_token=2,
        )
        assert result.event_type == EventType.SUBMISSION_ATTEMPT_STARTED


class TestDuplicateAndReplay:

    def test_duplicate_idempotency_key_different_content_fails_closed(self, tmp_path):
        store = _store(tmp_path)
        store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0, {"note": "first"}), execution_id=EXEC_ID)
        with pytest.raises(DuplicateEventError):
            store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0, {"note": "different"}), execution_id=EXEC_ID)

    def test_exact_replay_is_idempotent(self, tmp_path):
        store = _store(tmp_path)
        event = _event(EventType.ABOUT_TO_SUBMIT, 0, {"note": "same"})
        result_1 = store.prepare_attempt(event, execution_id=EXEC_ID)
        result_2 = store.prepare_attempt(event, execution_id=EXEC_ID)
        assert result_1.to_dict() == result_2.to_dict()
        events = store.read_events(EXEC_ID, IDEM_KEY)
        assert len(events) == 1  # replay did not create a second record


class TestAppendOnlyImmutability:

    def test_history_is_append_only(self, tmp_path):
        store = _store(tmp_path)
        store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0), execution_id=EXEC_ID)
        store.append_event(_event(EventType.SUBMISSION_ATTEMPT_STARTED, 1), execution_id=EXEC_ID)
        store.append_event(_event(EventType.ACCEPTED, 2, {"provider_job_id": "job_1"}), execution_id=EXEC_ID)
        events = store.read_events(EXEC_ID, IDEM_KEY)
        assert [e.event_type for e in events] == [
            EventType.ABOUT_TO_SUBMIT, EventType.SUBMISSION_ATTEMPT_STARTED, EventType.ACCEPTED,
        ]
        assert [e.event_seq for e in events] == [0, 1, 2]

    def test_historical_content_cannot_be_overwritten(self, tmp_path):
        store = _store(tmp_path)
        store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0), execution_id=EXEC_ID)
        store.append_event(_event(EventType.SUBMISSION_ATTEMPT_STARTED, 1, {"note": "original"}), execution_id=EXEC_ID)
        with pytest.raises(DuplicateEventError):
            store.append_event(_event(EventType.SUBMISSION_ATTEMPT_STARTED, 1, {"note": "tampered"}), execution_id=EXEC_ID)
        events = store.read_events(EXEC_ID, IDEM_KEY)
        assert events[1].evidence["note"] == "original"

    def test_no_module_function_ever_deletes_a_non_temp_file(self):
        import hypogeum.provider_receipt as mod
        source = open(mod.__file__).read()
        # the only unlink() calls in this module target .tmp_ files
        for line in source.splitlines():
            if "unlink(" in line or "os.remove(" in line:
                assert "tmp_path" in line, f"unexpected non-temp-file deletion: {line.strip()}"


class TestAcknowledgementRequiresEvidence:

    def test_accepted_without_job_id_or_request_id_raises(self, tmp_path):
        with pytest.raises(MissingAcknowledgementError):
            _event(EventType.ACCEPTED, 1, evidence={})

    def test_submitted_without_evidence_raises(self, tmp_path):
        with pytest.raises(MissingAcknowledgementError):
            _event(EventType.SUBMITTED, 1, evidence={"unrelated": "x"})

    def test_accepted_with_request_id_is_sufficient(self, tmp_path):
        event = _event(EventType.ACCEPTED, 1, evidence={"provider_request_id": "req_1"})
        assert event.event_type == EventType.ACCEPTED


class TestCrashScenarios:

    def test_crash_before_provider_boundary_is_safely_unsubmitted(self, tmp_path):
        store = _store(tmp_path)
        store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0), execution_id=EXEC_ID)
        # process "crashes" here -- nothing further is ever appended.
        state = store.classify_recovery_state(EXEC_ID, IDEM_KEY)
        assert state == RecoveryState.ATTEMPT_PREPARED_NOT_ENTERED
        assert state not in ELIGIBLE_FOR_SUBMISSION_STATES  # never silently treated as fresh/safe-to-retry-as-new

    def test_crash_after_possible_boundary_becomes_unknown_acceptance(self, tmp_path):
        store = _store(tmp_path)
        store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0), execution_id=EXEC_ID)
        store.append_event(_event(EventType.SUBMISSION_ATTEMPT_STARTED, 1), execution_id=EXEC_ID)
        # crash here: the network call may or may not have reached the provider.
        assert store.classify_recovery_state(EXEC_ID, IDEM_KEY) == RecoveryState.SUBMITTING_NO_ACKNOWLEDGEMENT

        # an external monitor later detects the timeout and records it explicitly
        store.append_event(_event(EventType.UNKNOWN_ACCEPTANCE, 2, {"detected_by": "recovery_scan"}), execution_id=EXEC_ID)
        assert store.classify_recovery_state(EXEC_ID, IDEM_KEY) == RecoveryState.UNKNOWN_ACCEPTANCE_REQUIRES_RECONCILIATION

    def test_unknown_acceptance_is_not_automatically_retryable(self, tmp_path):
        assert RecoveryState.UNKNOWN_ACCEPTANCE_REQUIRES_RECONCILIATION not in ELIGIBLE_FOR_SUBMISSION_STATES
        assert RecoveryState.SUBMITTING_NO_ACKNOWLEDGEMENT not in ELIGIBLE_FOR_SUBMISSION_STATES

    def test_reconciliation_resolves_uncertainty_preserving_original_evidence(self, tmp_path):
        store = _store(tmp_path)
        store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0), execution_id=EXEC_ID)
        store.append_event(_event(EventType.SUBMISSION_ATTEMPT_STARTED, 1), execution_id=EXEC_ID)
        unknown_event = _event(EventType.UNKNOWN_ACCEPTANCE, 2, {"detected_by": "recovery_scan"})
        store.append_event(unknown_event, execution_id=EXEC_ID)

        store.append_event(
            _event(EventType.RECONCILED, 3, {"resolution": "confirmed_not_submitted", "checked_via": "provider job list"}),
            execution_id=EXEC_ID,
        )
        assert store.classify_recovery_state(EXEC_ID, IDEM_KEY) == RecoveryState.TERMINALLY_FAILED_BEFORE_ACCEPTANCE

        # the original unknown_acceptance record is untouched
        events = store.read_events(EXEC_ID, IDEM_KEY)
        preserved = [e for e in events if e.event_type == EventType.UNKNOWN_ACCEPTANCE]
        assert len(preserved) == 1
        assert preserved[0].evidence == unknown_event.evidence

    def test_reconciliation_confirming_accepted(self, tmp_path):
        store = _store(tmp_path)
        store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0), execution_id=EXEC_ID)
        store.append_event(_event(EventType.SUBMISSION_ATTEMPT_STARTED, 1), execution_id=EXEC_ID)
        store.append_event(_event(EventType.UNKNOWN_ACCEPTANCE, 2), execution_id=EXEC_ID)
        store.append_event(
            _event(EventType.RECONCILED, 3, {"resolution": "confirmed_accepted", "provider_job_id": "job_found_later"}),
            execution_id=EXEC_ID,
        )
        assert store.classify_recovery_state(EXEC_ID, IDEM_KEY) == RecoveryState.ACKNOWLEDGED_SUBMITTED

    def test_terminal_failure_before_acceptance(self, tmp_path):
        store = _store(tmp_path)
        store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0), execution_id=EXEC_ID)
        store.append_event(_event(EventType.TERMINAL_FAILURE, 1, {"failure_reason": "local budget check failed"}), execution_id=EXEC_ID)
        assert store.classify_recovery_state(EXEC_ID, IDEM_KEY) == RecoveryState.TERMINALLY_FAILED_BEFORE_ACCEPTANCE

    def test_full_happy_path_to_completed_terminal(self, tmp_path):
        store = _store(tmp_path)
        store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0), execution_id=EXEC_ID)
        store.append_event(_event(EventType.SUBMISSION_ATTEMPT_STARTED, 1), execution_id=EXEC_ID)
        store.append_event(_event(EventType.ACCEPTED, 2, {"provider_job_id": "job_1"}), execution_id=EXEC_ID)
        assert store.classify_recovery_state(EXEC_ID, IDEM_KEY) == RecoveryState.ACKNOWLEDGED_SUBMITTED
        store.append_event(_event(EventType.SUBMITTED, 3, {"provider_job_id": "job_1"}), execution_id=EXEC_ID)
        store.append_event(_event(EventType.DOWNLOADED, 4, {"output_checksum": "b" * 64}), execution_id=EXEC_ID)
        assert store.classify_recovery_state(EXEC_ID, IDEM_KEY) == RecoveryState.DOWNLOADED
        store.append_event(_event(EventType.REGISTERED, 5, {"registration_id": "reg_1"}), execution_id=EXEC_ID)
        assert store.classify_recovery_state(EXEC_ID, IDEM_KEY) == RecoveryState.COMPLETED_TERMINAL


class TestRestartReconstructsState:

    def test_new_store_instance_same_path_reconstructs_identical_state(self, tmp_path):
        store_1 = _store(tmp_path)
        store_1.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0), execution_id=EXEC_ID)
        store_1.append_event(_event(EventType.SUBMISSION_ATTEMPT_STARTED, 1), execution_id=EXEC_ID)
        store_1.append_event(_event(EventType.UNKNOWN_ACCEPTANCE, 2), execution_id=EXEC_ID)

        # simulate process restart: a brand new store object, no shared in-memory state
        store_2 = _store(tmp_path)
        assert store_2.classify_recovery_state(EXEC_ID, IDEM_KEY) == RecoveryState.UNKNOWN_ACCEPTANCE_REQUIRES_RECONCILIATION
        assert len(store_2.read_events(EXEC_ID, IDEM_KEY)) == 3

    def test_recovery_classification_never_reads_in_memory_flags(self):
        import hypogeum.provider_receipt as mod
        source = open(mod.__file__).read()
        # classify_recovery_state's own implementation (_classify) takes
        # only the already-loaded event list as input -- no module-level
        # mutable cache exists to read instead.
        assert "_ATTEMPT_CACHE" not in source
        assert "_STATE_CACHE" not in source


class TestExactDecimalRoundTrip:

    def test_cost_survives_file_round_trip_exactly(self, tmp_path):
        store = _store(tmp_path)
        event = _event(EventType.ABOUT_TO_SUBMIT, 0, max_cost_per_call=Decimal("34.32"))
        store.prepare_attempt(event, execution_id=EXEC_ID)
        reloaded = store.read_events(EXEC_ID, IDEM_KEY)[0]
        assert reloaded.max_cost_per_call == Decimal("34.32")
        assert isinstance(reloaded.max_cost_per_call, Decimal)

    def test_cost_stored_as_string_never_float_in_raw_json(self, tmp_path):
        store = _store(tmp_path)
        store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0, max_cost_per_call=Decimal("0.1")), execution_id=EXEC_ID)
        path = tmp_path / "episodes" / EXEC_ID / "receipts" / IDEM_KEY / "0000_about_to_submit.json"
        raw = json.loads(path.read_text())
        assert raw["max_cost_per_call"] == "0.1"
        assert isinstance(raw["max_cost_per_call"], str)


class TestMalformedContradictoryDataFailsClosed:

    def test_malformed_checksum_fails_closed(self, tmp_path):
        with pytest.raises(ReceiptValidationError):
            _event(EventType.ABOUT_TO_SUBMIT, 0, provider_bound_content_sha256="not-a-checksum")

    def test_negative_cost_fails_closed(self, tmp_path):
        with pytest.raises(ReceiptValidationError):
            _event(EventType.ABOUT_TO_SUBMIT, 0, max_cost_per_call=Decimal("-1"))

    def test_secret_looking_evidence_key_fails_closed(self, tmp_path):
        with pytest.raises(ReceiptValidationError):
            _event(EventType.ABOUT_TO_SUBMIT, 0, evidence={"api_key": "sk-should-never-be-here"})

    def test_about_to_submit_with_wrong_seq_fails_closed(self, tmp_path):
        with pytest.raises(ReceiptValidationError):
            _event(EventType.ABOUT_TO_SUBMIT, 5)

    def test_reconciled_without_valid_resolution_fails_closed(self, tmp_path):
        with pytest.raises(ReceiptValidationError):
            _event(EventType.RECONCILED, 3, evidence={"resolution": "maybe"})


class TestMigrationPreservesExistingData:

    def test_migration_preserves_existing_rows(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.initialize_schema(db_path)
        conn = db.get_connection(db_path)
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status) VALUES (?, ?, ?, ?)",
            ("run_1", "ep_1", "2026-01-01T00:00:00Z", "running"),
        )
        conn.commit()
        conn.close()

        # re-running initialize_schema (the "migration") must not touch existing data
        db.initialize_schema(db_path)
        conn = db.get_connection(db_path)
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", ("run_1",)).fetchone()
        assert row is not None
        assert row["episode_id"] == "ep_1"
        conn.close()

    def test_migration_is_idempotent_and_repeatable(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.initialize_schema(db_path)
        db.initialize_schema(db_path)
        db.initialize_schema(db_path)  # three times, must not raise or duplicate schema objects
        conn = db.get_connection(db_path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='runtime_leases'"
        ).fetchall()]
        assert tables == ["runtime_leases"]
        conn.close()

    def test_runtime_leases_table_created_by_migration(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.initialize_schema(db_path)
        conn = db.get_connection(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(runtime_leases)").fetchall()}
        assert {"resource_id", "owner_id", "fencing_token", "status", "acquired_at", "expires_at", "released_at"} <= cols
        conn.close()


class TestNoNetworkPath:

    def test_module_imports_no_network_capable_library(self):
        import hypogeum.provider_receipt as mod
        source = open(mod.__file__).read()
        for forbidden in ("import requests", "import httpx", "urllib.request", "socket"):
            assert forbidden not in source, forbidden

    def test_module_never_calls_wall_clock(self):
        import hypogeum.provider_receipt as mod
        source = open(mod.__file__).read()
        for forbidden in ("datetime.now(", "time.time(", "utcnow("):
            assert forbidden not in source, forbidden

    def test_no_secrets_persisted_in_written_files(self, tmp_path):
        store = _store(tmp_path)
        store.prepare_attempt(_event(EventType.ABOUT_TO_SUBMIT, 0, {"note": "clean"}), execution_id=EXEC_ID)
        path = tmp_path / "episodes" / EXEC_ID / "receipts" / IDEM_KEY / "0000_about_to_submit.json"
        raw_text = path.read_text().lower()
        for forbidden in ("api_key", "secret", "password", "credential"):
            assert forbidden not in raw_text
