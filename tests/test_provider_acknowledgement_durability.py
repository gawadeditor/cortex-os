"""
tests/test_provider_acknowledgement_durability.py — Focused tests for
Task 1 of the grant/durability hardening pass: the provider request/
job ID is now persisted durably IMMEDIATELY after the submission
endpoint returns it, strictly before any subsequent polling/waiting
begins, via a new EventType.PROVIDER_ACKNOWLEDGED receipt event and an
on_provider_acknowledged callback threaded through the adapter
protocol (hypogeum.providers.fake_provider_adapter.FakeProviderAdapter
and hypogeum.providers.muapi_video_adapter.MuapiVideoAdapter).

All fixtures use hypogeum.providers.fake_provider_adapter.
FakeProviderAdapter -- structurally incapable of network access. No
test makes a real provider call or reads/mutates the live
.hypogeum-assets/hypogeum.db.
"""

from __future__ import annotations

import hashlib

import pytest

from hypogeum.provider_receipt import EventType, RecoveryState, WriteAheadReceiptStore
from tests.test_submission_gate import GateHarness, _make_env


class TestRequestIdPersistedBeforePolling:

    def test_success_writes_provider_acknowledged_before_accepted(self, tmp_path):
        harness = GateHarness(tmp_path)
        env = _make_env(idempotency_key="idem_ack_1", authorization_id="auth_ack_1")
        harness.acquire_lease(env)

        result = harness.submit(env, adapter_mode="success")
        assert result.approved is True

        events = harness.store.read_events(env.execution_id, env.idempotency_key)
        by_type = [e.event_type for e in events]
        assert EventType.PROVIDER_ACKNOWLEDGED in by_type
        ack = next(e for e in events if e.event_type == EventType.PROVIDER_ACKNOWLEDGED)
        accepted = next(e for e in events if e.event_type == EventType.ACCEPTED)
        assert ack.event_seq < accepted.event_seq
        assert ack.evidence["provider_job_id"] == f"fake_job_{env.idempotency_key}"
        assert accepted.evidence["provider_job_id"] == ack.evidence["provider_job_id"]

    def test_ack_event_requires_a_real_id_structurally(self, tmp_path):
        """MissingAcknowledgementError fires if PROVIDER_ACKNOWLEDGED is
        ever constructed without a job/request id -- it is now a member
        of _ACKNOWLEDGEMENT_EVENT_TYPES, the exact same structural
        guarantee ACCEPTED/SUBMITTED already have."""
        import pytest
        from hypogeum.provider_receipt import MissingAcknowledgementError, ProviderReceiptEvent
        from decimal import Decimal

        with pytest.raises(MissingAcknowledgementError):
            ProviderReceiptEvent(
                envelope_id="e1", envelope_version=1, authorization_id="a1", authorization_version=1,
                attempt_id="idem_1", idempotency_key="idem_1", event_type=EventType.PROVIDER_ACKNOWLEDGED,
                event_seq=2, at="2026-01-01T00:00:00Z", provider="fake", model="fake-model",
                provider_operation="video.text_to_video", provider_bound_content_sha256="a" * 64,
                max_cost_per_call=Decimal("0.39"), currency="USD", evidence={},
            )


class TestPostSubmitTimeoutRetainsRequestId:

    def test_unknown_acceptance_evidence_carries_the_same_request_id(self, tmp_path):
        harness = GateHarness(tmp_path)
        env = _make_env(idempotency_key="idem_ack_2", authorization_id="auth_ack_2")
        harness.acquire_lease(env)

        result = harness.submit(env, adapter_mode="post_submit_timeout")
        assert result.approved is False
        assert result.adapter_invoked is True

        events = harness.store.read_events(env.execution_id, env.idempotency_key)
        by_type = [e.event_type for e in events]
        assert EventType.PROVIDER_ACKNOWLEDGED in by_type
        assert EventType.UNKNOWN_ACCEPTANCE in by_type

        ack = next(e for e in events if e.event_type == EventType.PROVIDER_ACKNOWLEDGED)
        unknown = next(e for e in events if e.event_type == EventType.UNKNOWN_ACCEPTANCE)
        assert unknown.event_seq > ack.event_seq
        assert unknown.evidence["provider_job_id"] == ack.evidence["provider_job_id"]
        assert unknown.evidence["provider_request_id"] == ack.evidence["provider_request_id"]
        assert unknown.evidence["provider_job_id"] is not None  # a REAL id, not absent

    def test_recovery_state_is_requires_reconciliation(self, tmp_path):
        harness = GateHarness(tmp_path)
        env = _make_env(idempotency_key="idem_ack_3", authorization_id="auth_ack_3")
        harness.acquire_lease(env)

        harness.submit(env, adapter_mode="post_submit_timeout")
        state = harness.store.classify_recovery_state(env.execution_id, env.idempotency_key)
        assert state == RecoveryState.UNKNOWN_ACCEPTANCE_REQUIRES_RECONCILIATION

    def test_reservation_never_auto_released(self, tmp_path):
        import hypogeum.budget_scopes as bs

        harness = GateHarness(tmp_path)
        env = _make_env(idempotency_key="idem_ack_4", authorization_id="auth_ack_4")
        harness.acquire_lease(env)

        result = harness.submit(env, adapter_mode="post_submit_timeout")
        for reservation_id in result.budget_reservation_ids:
            row = harness.conn.execute(
                "SELECT status FROM budget_reservations WHERE reservation_id = ?", (reservation_id,),
            ).fetchone()
            assert row["status"] == "reserved"  # never released, never committed


class TestRestartCanReconcileUsingRetainedRequestId:

    def test_fresh_receipt_store_instance_reads_the_same_request_id(self, tmp_path):
        """Simulates a process restart: a brand-new WriteAheadReceiptStore
        object (no in-memory state carried over) pointed at the same
        assets_root can still recover the exact request_id a status-only
        reconciliation would need -- it was never held only in memory."""
        harness = GateHarness(tmp_path)
        env = _make_env(idempotency_key="idem_ack_5", authorization_id="auth_ack_5")
        harness.acquire_lease(env)
        harness.submit(env, adapter_mode="post_submit_timeout")

        restarted_store = WriteAheadReceiptStore(tmp_path)  # a genuinely new instance
        events = restarted_store.read_events(env.execution_id, env.idempotency_key)
        ack = next(e for e in events if e.event_type == EventType.PROVIDER_ACKNOWLEDGED)
        assert ack.evidence["provider_job_id"] is not None
        assert restarted_store.classify_recovery_state(
            env.execution_id, env.idempotency_key,
        ) == RecoveryState.UNKNOWN_ACCEPTANCE_REQUIRES_RECONCILIATION

    def test_whole_process_crash_mid_poll_still_classifies_correctly(self, tmp_path):
        """The most severe case: the process is killed BETWEEN writing
        PROVIDER_ACKNOWLEDGED and ever appending a 4th event (no
        exception ever ran in-process to write UNKNOWN_ACCEPTANCE
        itself). Constructed directly via prepare_attempt()/append_event()
        (bypassing submit() entirely) to simulate exactly that partial
        write-ahead trail. Recovery classification must still correctly
        flag this as requiring reconciliation, never as merely
        'submitting, no acknowledgement' (which would omit the fact
        that a real ID already exists)."""
        from decimal import Decimal

        from hypogeum.provider_receipt import ProviderReceiptEvent

        store = WriteAheadReceiptStore(tmp_path)
        common = dict(
            envelope_id="e1", envelope_version=1, authorization_id="a1", authorization_version=1,
            attempt_id="idem_crash", idempotency_key="idem_crash", at="2026-01-01T00:00:00Z",
            provider="fake", model="fake-model", provider_operation="video.text_to_video",
            provider_bound_content_sha256="a" * 64, max_cost_per_call=Decimal("0.39"), currency="USD",
        )
        store.prepare_attempt(
            ProviderReceiptEvent(**common, event_type=EventType.ABOUT_TO_SUBMIT, event_seq=0, evidence={}),
            execution_id="exec_crash",
        )
        store.append_event(
            ProviderReceiptEvent(**common, event_type=EventType.SUBMISSION_ATTEMPT_STARTED, event_seq=1, evidence={}),
            execution_id="exec_crash",
        )
        store.append_event(
            ProviderReceiptEvent(
                **common, event_type=EventType.PROVIDER_ACKNOWLEDGED, event_seq=2,
                evidence={"provider_job_id": "real_job_123", "provider_request_id": "real_job_123"},
            ),
            execution_id="exec_crash",
        )
        # Nothing further is ever written -- simulates the process dying
        # here, mid-poll, with no chance to run any except-block at all.

        state = store.classify_recovery_state("exec_crash", "idem_crash")
        assert state == RecoveryState.UNKNOWN_ACCEPTANCE_REQUIRES_RECONCILIATION

        events = store.read_events("exec_crash", "idem_crash")
        ack = next(e for e in events if e.event_type == EventType.PROVIDER_ACKNOWLEDGED)
        assert ack.evidence["provider_job_id"] == "real_job_123"


class TestMuapiVideoAdapterCallsAcknowledgementBeforePolling:
    """Direct, no-network unit test against the REAL adapter this whole
    incident is about -- hypogeum.providers.muapi_video_adapter.
    MuapiVideoAdapter -- mocking only hypogeum.muapi._submit_http() and
    _poll_until_complete() (never touching any real socket/requests
    call), proving the callback fires with the real request_id
    strictly BEFORE _poll_until_complete() is ever called."""

    def test_callback_fires_before_poll_is_called(self, monkeypatch):
        import hypogeum.muapi as muapi_module
        from decimal import Decimal

        from hypogeum.providers.fake_provider_adapter import AdapterInvocationCapability, compute_capability_checksum
        from hypogeum.providers.muapi_video_adapter import MuapiVideoAdapter, encode_video_payload

        call_order = []

        def _fake_submit_http(endpoint, payload, config, scene_id, log):
            call_order.append("submit")
            return "real_request_id_789", None

        def _fake_poll_until_complete(request_id, config, scene_id, log):
            call_order.append("poll")
            assert request_id == "real_request_id_789"
            raise RuntimeError("simulated: never called for real in this test's assertions below")

        from tests._capability_boundary_stub import install_passthrough
        install_passthrough(monkeypatch)
        monkeypatch.setattr(muapi_module, "_submit_http", _fake_submit_http)
        monkeypatch.setattr(muapi_module, "_poll_until_complete", _fake_poll_until_complete)

        payload_bytes = encode_video_payload(
            prompt="a fully assembled prompt", model="veo3.1-lite-text-to-video", aspect_ratio="16:9",
            duration_seconds=8.0, reference_image_url=None, scene_id="s01", short_id=None,
            episode_id="ep_test", output_dir="/tmp/does-not-exist",
        )
        capability_fields = dict(
            capability_id="cap_ack_1", envelope_id="env_ack_1", envelope_version=1,
            attempt_id="attempt_ack_1", idempotency_key="idem_ack_direct",
            lease_owner_id="worker_ack_1", lease_fencing_token=1,
            provider="muapi", model="veo3.1-lite-text-to-video", provider_operation="video.text_to_video",
            operation_payload_bytes=payload_bytes,
            operation_payload_sha256=hashlib.sha256(payload_bytes).hexdigest(),
            operation_payload_schema_version="video.text_to_video/1.0",
            max_cost_per_call=Decimal("0.39"), currency="USD",
            budget_reservation_ids=("resv_ack_1",), issued_at="2026-08-17T00:00:00+00:00",
            expires_at="2026-08-17T00:05:00+00:00",
        )
        checksum = compute_capability_checksum(AdapterInvocationCapability(capability_checksum="", **capability_fields))
        capability = AdapterInvocationCapability(capability_checksum=checksum, **capability_fields)

        acknowledged = {}

        def _on_ack(*, provider_job_id=None, provider_request_id=None):
            acknowledged["provider_job_id"] = provider_job_id
            acknowledged["provider_request_id"] = provider_request_id
            call_order.append("acknowledged")

        adapter = MuapiVideoAdapter()
        with pytest.raises(RuntimeError):
            adapter.invoke(capability, mode="real", on_provider_acknowledged=_on_ack)

        assert call_order == ["submit", "acknowledged", "poll"]  # ack strictly between submit and poll
        assert acknowledged["provider_job_id"] == "real_request_id_789"
        assert acknowledged["provider_request_id"] == "real_request_id_789"


class TestUnrelatedUnknownAcceptanceModeUnaffected:
    """mode='unknown_acceptance' simulates never even getting an ID
    (e.g. the submission call itself never returned) -- distinct from
    'post_submit_timeout', and must remain distinguishable: no
    PROVIDER_ACKNOWLEDGED event, no provider_job_id in evidence."""

    def test_no_acknowledgement_event_and_no_id_in_evidence(self, tmp_path):
        harness = GateHarness(tmp_path)
        env = _make_env(idempotency_key="idem_ack_6", authorization_id="auth_ack_6")
        harness.acquire_lease(env)

        result = harness.submit(env, adapter_mode="unknown_acceptance")
        assert result.approved is False

        events = harness.store.read_events(env.execution_id, env.idempotency_key)
        by_type = [e.event_type for e in events]
        assert EventType.PROVIDER_ACKNOWLEDGED not in by_type
        unknown = next(e for e in events if e.event_type == EventType.UNKNOWN_ACCEPTANCE)
        assert unknown.evidence.get("provider_job_id") is None
