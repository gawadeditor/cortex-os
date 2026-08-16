"""
tests/test_submission_gate.py — Unit tests for hypogeum/submission_gate.py
(Commit 3, 2026-08-15)

Every test uses isolated fixture data (in-memory envelopes/authorization
records) and a temporary filesystem/database root. No test reads or
mutates the live episode, and no test imports or invokes a real
provider/network-capable module -- only hypogeum.providers.
fake_provider_adapter.FakeProviderAdapter, which is itself structurally
incapable of network access.
"""

from __future__ import annotations

import dataclasses
import hashlib
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

import hypogeum.adapter_registry as adapter_registry
import hypogeum.budget_scopes as bs
import hypogeum.db as db
import hypogeum.submission_gate as sg
from hypogeum.authorization_lifecycle import AuthorizationRecord, AuthorizationState
from hypogeum.execution_envelope import GenerationExecutionEnvelope
from hypogeum.provider_receipt import ELIGIBLE_FOR_SUBMISSION_STATES, EventType, ProviderReceiptEvent, RecoveryState, WriteAheadReceiptStore
from hypogeum.providers.fake_provider_adapter import AdapterInvocationCapability, FakeProviderAdapter

EXEC_ID = "exec_test"
AT = "2026-01-01T00:00:00Z"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_env(**overrides) -> GenerationExecutionEnvelope:
    prompt = overrides.pop("operation_payload_content", "a fully compiled prompt")
    kwargs = dict(
        envelope_schema_version="1.0", envelope_version=1,
        channel_id="test_channel", channel_package_version="1.0", channel_package_checksum="a" * 64,
        execution_id=EXEC_ID, workflow_version="1.0", plan_version="1.0", plan_checksum="b" * 64,
        planned_shot_id="shot_1", structured_spec_checksum="c" * 64,
        compiler_identity="pkg.compile", compiler_version="v1",
        operation_payload_content=prompt, operation_payload_sha256=_sha(prompt),
        operation_payload_schema_version=overrides.pop("operation_payload_schema_version", "video.text_to_video/1.0"),
        content_provenance={
            "compiler": "pkg.compile", "compiler_version": "v1",
            "source_work_item_id": "shot_1", "rendered_at": AT,
        },
        provider="fake", model="fake-model", provider_operation="video.text_to_video",
        deterministic_output_path="/tmp/does-not-exist/out.mp4", expected_output_identity="asset_1",
        authorization_id="auth_1", authorization_version=1, max_cost_per_call=Decimal("0.39"),
        run_level_budget_ref="run_1", episode_level_budget_ref="ep_1", currency="USD",
        retry_authorized=False, fallback_authorized=False, idempotency_key="idem_1",
        created_at=AT,
        expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
        dependency_checksums={"channel_package": "a" * 64, "plan": "b" * 64, "structured_spec": "c" * 64, "compiler": "d" * 64},
        authorization_status="authorized",
    )
    kwargs.update(overrides)
    return GenerationExecutionEnvelope.create(**kwargs)


def _make_auth(envelope: GenerationExecutionEnvelope, *, state=AuthorizationState.AUTHORIZED, **overrides) -> AuthorizationRecord:
    kwargs = dict(
        authorization_id=envelope.authorization_id, authorization_version=envelope.authorization_version,
        envelope_id=envelope.envelope_id, envelope_version=envelope.envelope_version,
        current_state=state, history=(),
    )
    kwargs.update(overrides)
    return AuthorizationRecord(**kwargs)


def _live_sources_for(envelope: GenerationExecutionEnvelope, **overrides) -> sg.LiveSources:
    kwargs = dict(
        channel_package_checksum=envelope.dependency_checksums.channel_package,
        plan_checksum=envelope.dependency_checksums.plan,
        structured_spec_checksum=envelope.dependency_checksums.structured_spec,
        compiler_version=envelope.compiler_version,
    )
    kwargs.update(overrides)
    return sg.LiveSources(**kwargs)


def _default_scopes(envelope: GenerationExecutionEnvelope) -> list:
    return [
        bs.ScopeRequest(bs.BudgetScopeKind.CALL, envelope.planned_shot_id, Decimal("0.39"), "USD"),
        bs.ScopeRequest(bs.BudgetScopeKind.RUN, envelope.run_level_budget_ref, Decimal("10.00"), "USD"),
        bs.ScopeRequest(bs.BudgetScopeKind.EPISODE, envelope.episode_level_budget_ref, Decimal("34.32"), "USD"),
    ]


class GateHarness:
    """Bundles a temp DB connection, receipt store, and fake adapter, and
    provides submit() with all the boilerplate context pre-filled --
    individual tests only override what they're testing."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        db_path = tmp_path / "test.db"
        db.initialize_schema(db_path)
        self.conn = db.get_connection(db_path)
        self.store = WriteAheadReceiptStore(tmp_path)
        # submit() resolves its adapter internally via the Adapter
        # Registry, keyed by the envelope's own (provider, model,
        # provider_operation) -- register the fake adapter under the
        # exact key _make_env()'s defaults produce ("fake", "fake-model",
        # "video.text_to_video"), against the SAME db_path self.conn
        # points to, so submit()'s internal lookup (via lease_conn) finds it.
        adapter_registry.reset_adapter_instance_cache()
        adapter_registry.register_adapter(
            "fake", "fake-model", "video.text_to_video", "1.0", FakeProviderAdapter, db_path=db_path,
        )
        self.adapter = adapter_registry.get_or_create_adapter_instance(FakeProviderAdapter)

    def acquire_lease(self, envelope, owner="worker_a", token_expires="2026-01-01T01:00:00Z"):
        return db.acquire_lease(self.conn, envelope.envelope_id, owner, acquired_at=AT, expires_at=token_expires)

    def submit(self, envelope, **overrides):
        kwargs = dict(
            live_sources=_live_sources_for(envelope),
            authorization=_make_auth(envelope),
            lease_conn=self.conn, lease_owner_id="worker_a", lease_fencing_token=1,
            receipt_store=self.store, execution_id=EXEC_ID, attempt_id="attempt_1",
            budget_scopes=_default_scopes(envelope),
            adapter_mode="success", at=AT,
            expected_deterministic_output_path=envelope.deterministic_output_path,
        )
        kwargs.update(overrides)
        return sg.submit(envelope, **kwargs)


@pytest.fixture
def harness(tmp_path):
    h = GateHarness(tmp_path)
    return h


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:

    def test_fully_valid_envelope_passes_and_invokes_adapter_once(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env)
        assert result.approved is True
        assert harness.adapter.invocation_count == 1


# ---------------------------------------------------------------------------
# Adapter resolution -- no caller-supplied adapter; resolved internally
# via the Adapter Registry, keyed by the envelope's own (provider,
# model, provider_operation).
# ---------------------------------------------------------------------------

class TestAdapterResolutionDenials:

    def test_unregistered_provider_model_operation_blocks_with_zero_calls(self, harness):
        env = _make_env(provider="nobody-registered-this-provider")
        harness.acquire_lease(env)
        result = harness.submit(env)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        assert "14_model_provider_operation_match" in result.denied_at_check

    def test_adapter_implementation_drift_blocks_with_zero_calls(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        # Simulate "the adapter's source changed after registration" --
        # the DB row still holds the checksum computed at registration
        # time, but the live class's checksum (recomputed by submit() on
        # every call) no longer matches it.
        harness.conn.execute(
            "UPDATE adapter_registrations SET adapter_implementation_checksum = ? "
            "WHERE provider = 'fake' AND model = 'fake-model' AND provider_operation = 'video.text_to_video'",
            ("stale" * 16,),
        )
        harness.conn.commit()
        result = harness.submit(env)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        assert "14_model_provider_operation_match" in result.denied_at_check
        assert "drift" in result.reason


# ---------------------------------------------------------------------------
# 2-21. Individual denial checks -- each must produce zero adapter calls
# ---------------------------------------------------------------------------

class TestEnvelopeAndSourceDenials:

    def test_prompt_mutation_blocks_with_zero_calls(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        tampered = dataclasses.replace(env, operation_payload_content=env.operation_payload_content + "!")
        result = harness.submit(tampered)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        assert "02_envelope_integrity" in result.denied_at_check

    def test_stale_channel_package_checksum_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env, live_sources=_live_sources_for(env, channel_package_checksum="f" * 64))
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        assert result.denied_at_check == "06_channel_package_checksum_match"

    def test_stale_plan_checksum_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env, live_sources=_live_sources_for(env, plan_checksum="f" * 64))
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        assert result.denied_at_check == "07_plan_checksum_match"

    def test_stale_structured_spec_checksum_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env, live_sources=_live_sources_for(env, structured_spec_checksum="f" * 64))
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        assert result.denied_at_check == "08_structured_spec_checksum_match"

    def test_stale_compiler_version_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env, live_sources=_live_sources_for(env, compiler_version="different"))
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        assert result.denied_at_check == "09_compiler_identity_version_match"

    def test_missing_dependency_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        # simulate "missing" by pointing live sources at empty/placeholder checksums
        result = harness.submit(env, live_sources=_live_sources_for(env, structured_spec_checksum="0" * 64))
        assert result.approved is False
        assert harness.adapter.invocation_count == 0

    def test_provider_mismatch_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env, required_provider="different_provider")
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        assert result.denied_at_check == "14_model_provider_operation_match"

    def test_model_mismatch_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env, required_model="different-model")
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        assert result.denied_at_check == "14_model_provider_operation_match"

    def test_duration_mismatch_blocks(self, harness):
        env = _make_env(duration_seconds=Decimal("8.0"))
        harness.acquire_lease(env)
        result = harness.submit(env, expected_duration_seconds=Decimal("9.0"))
        assert result.approved is False
        assert harness.adapter.invocation_count == 0

    def test_aspect_ratio_mismatch_blocks(self, harness):
        env = _make_env(aspect_ratio="16:9")
        harness.acquire_lease(env)
        result = harness.submit(env, expected_aspect_ratio="9:16")
        assert result.approved is False
        assert harness.adapter.invocation_count == 0

    def test_seed_mismatch_blocks(self, harness):
        env = _make_env(seed=42)
        harness.acquire_lease(env)
        result = harness.submit(env, expected_seed=99)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0

    def test_output_identity_mismatch_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env, expected_output_identity="different_asset")
        assert result.approved is False
        assert harness.adapter.invocation_count == 0

    def test_output_already_exists_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env, output_already_exists=True)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        assert result.denied_at_check == "16_output_collision"

    def test_unsupported_schema_version_blocks(self, harness):
        env = _make_env()
        tampered = dataclasses.replace(env, envelope_schema_version="99.0")
        harness.acquire_lease(env)
        result = harness.submit(tampered)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        assert result.denied_at_check == "01_envelope_schema_version_supported"

    def test_invalid_envelope_id_blocks(self, harness):
        env = _make_env()
        tampered = dataclasses.replace(env, envelope_id="0" * 64)
        harness.acquire_lease(env)
        result = harness.submit(tampered)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        assert result.denied_at_check == "02_envelope_integrity"


class TestAuthorizationDenials:

    def test_unauthorized_state_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env, authorization=_make_auth(env, state=AuthorizationState.AWAITING_AUTHORIZATION))
        assert result.approved is False
        assert harness.adapter.invocation_count == 0

    def test_expired_authorization_blocks(self, harness):
        env = _make_env(expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": "2025-01-01T00:00:00Z"})
        harness.acquire_lease(env)
        result = harness.submit(env)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0

    def test_consumed_authorization_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env, authorization=_make_auth(env, state=AuthorizationState.CONSUMED))
        assert result.approved is False
        assert harness.adapter.invocation_count == 0

    def test_cancelled_authorization_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env, authorization=_make_auth(env, state=AuthorizationState.CANCELLED))
        assert result.approved is False
        assert harness.adapter.invocation_count == 0

    def test_superseded_authorization_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env, authorization=_make_auth(env, state=AuthorizationState.SUPERSEDED))
        assert result.approved is False
        assert harness.adapter.invocation_count == 0

    def test_source_invalidated_authorization_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env, authorization=_make_auth(env, state=AuthorizationState.INVALIDATED_SOURCE_CHANGED))
        assert result.approved is False
        assert harness.adapter.invocation_count == 0


class TestBindingMismatchDenials:

    def test_cost_ceiling_mismatch_blocks(self, harness):
        env = _make_env(max_cost_per_call=Decimal("0.39"))
        harness.acquire_lease(env)
        scopes = [bs.ScopeRequest(bs.BudgetScopeKind.CALL, env.planned_shot_id, Decimal("0.10"), "USD")]
        result = harness.submit(env, budget_scopes=scopes)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0

    def test_currency_mismatch_blocks(self, harness):
        env = _make_env(currency="USD")
        harness.acquire_lease(env)
        scopes = [bs.ScopeRequest(bs.BudgetScopeKind.CALL, env.planned_shot_id, Decimal("1.00"), "EUR")]
        result = harness.submit(env, budget_scopes=scopes)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0


class TestLeaseDenials:

    def test_missing_lease_blocks(self, harness):
        env = _make_env()
        # never acquired
        result = harness.submit(env)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        assert result.denied_at_check == "19_lease_and_fencing"

    def test_stale_expired_lease_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env, token_expires="2025-01-01T00:00:00Z")
        result = harness.submit(env)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0

    def test_wrong_owner_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env, owner="worker_a")
        result = harness.submit(env, lease_owner_id="worker_wrong")
        assert result.approved is False
        assert harness.adapter.invocation_count == 0

    def test_wrong_fencing_token_blocks(self, harness):
        env = _make_env()
        harness.acquire_lease(env, owner="worker_a")
        result = harness.submit(env, lease_fencing_token=99)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0

    def test_lease_takeover_after_initial_validation_blocks_stale_worker(self, harness):
        env = _make_env()
        # worker_a's lease expires quickly...
        harness.acquire_lease(env, owner="worker_a", token_expires="2026-01-01T00:00:10Z")
        # ...and worker_b legitimately takes over once it has expired.
        takeover = db.acquire_lease(
            harness.conn, env.envelope_id, "worker_b",
            acquired_at="2026-01-01T00:00:30Z", expires_at="2026-01-01T01:00:00Z",
        )
        assert takeover is not None and takeover["fencing_token"] == 2
        # worker_a, unaware it lost the lease, still tries to submit with its stale token=1
        result = harness.submit(env, lease_owner_id="worker_a", lease_fencing_token=1)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0


class TestRecoveryIdempotencyDenials:

    def test_existing_acknowledged_submission_blocks_duplicate_invocation(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        first = harness.submit(env)
        assert first.approved is True
        assert harness.adapter.invocation_count == 1

        second = harness.submit(env, attempt_id="attempt_2")
        assert second.approved is False
        assert harness.adapter.invocation_count == 1  # not called again

    def test_unknown_acceptance_blocks_retry(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env, adapter_mode="unknown_acceptance")
        assert result.approved is False
        assert harness.adapter.invocation_count == 1
        assert result.recovery_state == RecoveryState.UNKNOWN_ACCEPTANCE_REQUIRES_RECONCILIATION

        retry_result = harness.submit(env, attempt_id="attempt_2")
        assert retry_result.approved is False
        assert harness.adapter.invocation_count == 1  # never invoked again

    def test_conflicting_idempotency_key_blocks_execution(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        harness.store.prepare_attempt(
            ProviderReceiptEvent(
                envelope_id=env.envelope_id, envelope_version=1, authorization_id=env.authorization_id,
                authorization_version=1, attempt_id="preexisting", idempotency_key=env.idempotency_key,
                event_type=EventType.ABOUT_TO_SUBMIT, event_seq=0, at=AT,
                provider=env.provider, model=env.model, provider_operation=env.provider_operation,
                provider_bound_content_sha256=env.operation_payload_sha256,
                max_cost_per_call=env.max_cost_per_call, currency=env.currency, evidence={"note": "pre-existing"},
            ),
            execution_id=EXEC_ID,
        )
        result = harness.submit(env)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0


class TestRetryFallbackDenials:

    def test_unauthorized_retry_blocks_execution(self, harness):
        env = _make_env(retry_authorized=False)
        harness.acquire_lease(env)
        retry_ctx = sg.RetryContext(prior_attempt_id="attempt_0", retry_budget_ref="retry_budget_1", new_attempt_id="attempt_1")
        result = harness.submit(env, retry_context=retry_ctx)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0

    def test_unauthorized_fallback_blocks_execution(self, harness):
        env = _make_env(fallback_authorized=False)
        harness.acquire_lease(env)
        fallback_ctx = sg.FallbackContext(fallback_provider="fake", fallback_model="fake-model-2", fallback_budget_ref="fallback_budget_1")
        result = harness.submit(env, fallback_context=fallback_ctx)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0

    def test_authorized_retry_requires_safe_recovery_state(self, harness):
        env = _make_env(retry_authorized=True, retry_budget_ref="retry_budget_1")
        harness.acquire_lease(env)
        harness.submit(env, adapter_mode="unknown_acceptance")
        assert harness.adapter.invocation_count == 1

        retry_ctx = sg.RetryContext(prior_attempt_id="attempt_1", retry_budget_ref="retry_budget_1", new_attempt_id="attempt_2")
        # unknown_acceptance is NOT a retry-safe state
        result = harness.submit(env, attempt_id="attempt_2", retry_context=retry_ctx)
        assert result.approved is False
        assert harness.adapter.invocation_count == 1


# ---------------------------------------------------------------------------
# Exact request preservation / non-bypassability
# ---------------------------------------------------------------------------

class TestExactRequestPreservation:

    def test_validated_request_preserves_prompt_bytes(self, harness):
        prompt = "SCENE CONTINUITY: exact bytes\nwith a newline"
        env = _make_env(operation_payload_content=prompt, operation_payload_sha256=_sha(prompt))
        harness.acquire_lease(env)
        harness.submit(env)
        capability = harness.adapter.received_capabilities[0]
        assert capability.operation_payload_bytes == prompt.encode("utf-8")

    def test_caller_cannot_override_provider_bound_fields(self, harness):
        """submit()'s public signature has no parameter that lets a
        caller inject a different provider/model/prompt into the
        capability after gate validation, and no parameter that lets a
        caller select or supply an adapter -- the capability is always
        minted from the envelope's own already-validated fields, and the
        adapter is always resolved internally via the Adapter Registry."""
        import inspect
        sig = inspect.signature(sg.submit)
        forbidden_override_params = {
            "capability_provider", "capability_model", "capability_prompt",
            "override_operation_payload_content", "adapter",
        }
        assert not (forbidden_override_params & set(sig.parameters))


class TestFakeAdapterRawInputRejection:

    def test_adapter_rejects_direct_raw_prompt_invocation(self):
        adapter = FakeProviderAdapter()
        from hypogeum.providers.fake_provider_adapter import RawPromptRejectedError
        with pytest.raises(RawPromptRejectedError):
            adapter.invoke("a raw prompt string, not a capability")


class TestCapabilitySingleUseAndBinding:

    def test_capability_cannot_be_reused(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        harness.submit(env)
        capability = harness.adapter.received_capabilities[0]
        from hypogeum.providers.fake_provider_adapter import CapabilityAlreadyConsumedError
        with pytest.raises(CapabilityAlreadyConsumedError):
            harness.adapter.invoke(capability, mode="success")

    def test_capability_cannot_be_applied_to_a_different_envelope(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        harness.submit(env)
        capability = harness.adapter.received_capabilities[0]
        tampered = dataclasses.replace(capability, envelope_id="different_envelope_id")
        from hypogeum.providers.fake_provider_adapter import InvalidCapabilityError
        fresh_adapter = FakeProviderAdapter()
        with pytest.raises(InvalidCapabilityError):
            fresh_adapter.invoke(tampered, mode="success")

    def test_capability_cannot_be_applied_to_a_different_attempt(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        harness.submit(env)
        capability = harness.adapter.received_capabilities[0]
        tampered = dataclasses.replace(capability, attempt_id="different_attempt_id")
        from hypogeum.providers.fake_provider_adapter import InvalidCapabilityError
        fresh_adapter = FakeProviderAdapter()
        with pytest.raises(InvalidCapabilityError):
            fresh_adapter.invoke(tampered, mode="success")


# ---------------------------------------------------------------------------
# Pre-boundary vs post-boundary evidence
# ---------------------------------------------------------------------------

class TestPreAndPostBoundaryEvidence:

    def test_pre_boundary_failure_produces_zero_adapter_calls(self, harness):
        env = _make_env()
        # no lease acquired -- denial happens before step 9
        result = harness.submit(env)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        events = harness.store.read_events(EXEC_ID, env.idempotency_key)
        assert events == []  # nothing was even write-ahead persisted

    def test_post_boundary_timeout_produces_one_possible_call_and_unknown_acceptance_evidence(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env, adapter_mode="unknown_acceptance")
        assert result.approved is False
        assert harness.adapter.invocation_count == 1
        events = harness.store.read_events(EXEC_ID, env.idempotency_key)
        event_types = [e.event_type for e in events]
        assert EventType.SUBMISSION_ATTEMPT_STARTED in event_types
        assert EventType.UNKNOWN_ACCEPTANCE in event_types

    def test_restart_reconstructs_same_blocked_recovery_state(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        harness.submit(env, adapter_mode="unknown_acceptance")

        # simulate restart: brand new store instance pointed at the same path
        new_store = WriteAheadReceiptStore(harness.tmp_path)
        assert new_store.classify_recovery_state(EXEC_ID, env.idempotency_key) == RecoveryState.UNKNOWN_ACCEPTANCE_REQUIRES_RECONCILIATION


# ---------------------------------------------------------------------------
# No network path
# ---------------------------------------------------------------------------

class TestNoNetworkPath:

    def test_no_real_provider_module_imported(self):
        forbidden_import_targets = (
            "requests", "httpx", "urllib.request", "socket",
            "hypogeum.muapi", "hypogeum.veo_official", "hypogeum.publishing",
        )
        for mod_name in ("hypogeum.submission_gate", "hypogeum.budget_scopes", "hypogeum.providers.fake_provider_adapter"):
            import importlib
            mod = importlib.import_module(mod_name)
            source = open(mod.__file__).read()
            import_lines = [
                line.strip() for line in source.splitlines()
                if line.strip().startswith("import ") or line.strip().startswith("from ")
            ]
            for line in import_lines:
                for forbidden in forbidden_import_targets:
                    assert forbidden not in line, f"{mod_name} has a forbidden import: {line!r}"

    def test_only_fake_adapter_is_importable_from_providers_package_this_commit(self):
        import hypogeum.providers.fake_provider_adapter  # noqa: F401
        # no muapi adapter module exists yet in this commit
        import importlib
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("hypogeum.providers.muapi_adapter")


# ---------------------------------------------------------------------------
# Grant-derived authorization boundary revalidation + settlement
# (Correction 3/4, hypogeum.authorization_authority + authorization_store)
# ---------------------------------------------------------------------------

class TestGrantDerivedAuthorizationBoundary:
    """Proves submit()'s one new, narrow Step 8.5: a grant-derived
    AuthorizationRecord's parent grant + reservation is revalidated
    immediately before the provider boundary, and the reservation is
    settled according to the adapter's outcome -- while an
    explicit-approval-derived (grant-free) authorization (every test
    above this class) is completely unaffected, since it carries no
    grant evidence at all."""

    def _register_grant_and_issue(self, harness, env, *, max_calls=5, max_total_spend=None):
        import hypogeum.authorization_authority as authority
        import hypogeum.authorization_store as astore
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from hypogeum.approval_artifact import GrantBindingApproval, sign_approval_artifact

        if max_total_spend is None:
            max_total_spend = env.max_cost_per_call * max_calls

        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()
        authority._load_trusted_public_key = lambda: pub

        db_path = harness.tmp_path / "test.db"
        bindings = [astore.GrantBinding(
            provider=env.provider, model=env.model, provider_operation=env.provider_operation,
            currency=env.currency, max_cost_per_call=env.max_cost_per_call,
            max_calls=max_calls, max_total_spend=max_total_spend,
        )]
        grant_checksum = authority.compute_grant_checksum(
            channel_id=env.channel_id, episode_id="ep_test", workflow_id="wf", workflow_version="1.0.0",
            workflow_checksum="a" * 64, bindings=bindings, max_total_spend_all_bindings=max_total_spend,
            currency=env.currency, expires_at="2099-01-01T00:00:00Z",
        )
        artifact = sign_approval_artifact(
            dict(
                approval_id="appr1", approval_version=1, approval_type="grant", subject_checksum=grant_checksum,
                channel_id=env.channel_id, scope_id="ep_test", workflow_id="wf", workflow_version="1.0.0",
                workflow_checksum="a" * 64, channel_package_version="1.0", channel_package_checksum="b" * 64,
                allowed_bindings=(GrantBindingApproval(env.provider, env.model, env.provider_operation),),
                currency=env.currency, max_cost_per_call=env.max_cost_per_call, max_total_spend=max_total_spend,
                max_calls=max_calls, retry_authorized=False, fallback_authorized=False,
                issued_at=AT, expires_at="2099-01-01T00:00:00Z", approver_identity="operator_x", nonce="nonce_1",
            ),
            private_key=priv,
        )
        grant = authority.register_run_grant(
            grant_id="g1", grant_version=1, channel_id=env.channel_id, episode_id="ep_test",
            workflow_id="wf", workflow_version="1.0.0", workflow_checksum="a" * 64, bindings=bindings,
            max_total_spend_all_bindings=max_total_spend, currency=env.currency, expires_at="2099-01-01T00:00:00Z",
            approval_artifact=artifact, db_path=db_path,
        )
        record = authority.issue_child_authorization(env, "g1", 1, at=AT, db_path=db_path)
        return record, grant, authority, astore, db_path

    def test_grant_derived_submission_succeeds_and_settles_committed(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        record, grant, authority, astore, db_path = self._register_grant_and_issue(harness, env)

        result = harness.submit(env, authorization=record)
        assert result.approved is True
        assert harness.adapter.invocation_count == 1

        conn = harness.conn
        reservation_id = record.history[-1].evidence["grant_reservation_id"]
        reservation = astore.get_ledger_reservation(conn, reservation_id)
        assert reservation.state == "committed"
        assert reservation.charged_amount == env.max_cost_per_call

    def test_grant_revoked_after_issuance_denies_at_boundary_with_zero_calls(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        record, grant, authority, astore, db_path = self._register_grant_and_issue(harness, env)

        # Revoke AFTER issuance but BEFORE submission -- "issuance read
        # active before revocation" must not be sufficient (Correction 3).
        authority.revoke_grant("g1", 1, revoked_by="operator_x", reason="test", at=AT, db_path=db_path)

        result = harness.submit(env, authorization=record)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        assert "03_authorization_active_and_unconsumed" in result.denied_at_check

        reservation_id = record.history[-1].evidence["grant_reservation_id"]
        reservation = astore.get_ledger_reservation(harness.conn, reservation_id)
        assert reservation.state == "reserved"  # never advanced to boundary_entered

    def test_grant_reservation_settles_released_on_rejection(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        record, grant, authority, astore, db_path = self._register_grant_and_issue(harness, env)

        result = harness.submit(env, authorization=record, adapter_mode="rejected")
        assert result.approved is False
        reservation_id = record.history[-1].evidence["grant_reservation_id"]
        reservation = astore.get_ledger_reservation(harness.conn, reservation_id)
        assert reservation.state == "released"

    def test_grant_reservation_settles_possible_unknown_acceptance_and_is_never_auto_released(self, harness):
        env = _make_env()
        harness.acquire_lease(env)
        record, grant, authority, astore, db_path = self._register_grant_and_issue(harness, env)

        result = harness.submit(env, authorization=record, adapter_mode="unknown_acceptance")
        assert result.approved is False
        reservation_id = record.history[-1].evidence["grant_reservation_id"]
        reservation = astore.get_ledger_reservation(harness.conn, reservation_id)
        assert reservation.state == "possible_unknown_acceptance"

    def test_explicit_approval_authorization_has_no_grant_evidence_and_is_unaffected(self, harness):
        """The default harness authorization (_make_auth(), empty
        history) must behave exactly as every other test in this file
        already proves -- Step 8.5 must be a complete no-op for it."""
        env = _make_env()
        harness.acquire_lease(env)
        result = harness.submit(env)
        assert result.approved is True
        assert harness.adapter.invocation_count == 1


# ---------------------------------------------------------------------------
# Reconstructed-binding block (plan §5 -- Commit M precondition fix)
# ---------------------------------------------------------------------------

class TestReconstructedBindingDenial:
    """audit_metadata['workflow_binding_reconstructed'] is True for every
    envelope tools/migrate_legacy_manifest_to_envelopes.py constructs --
    it must be gate-denied for real submission with zero adapter calls,
    but MUST remain fully evaluable by run_shadow_checks()/
    evaluate_shadow() (Observation-Only), since that diagnostic path is
    the entire reason the tag exists."""

    def test_reconstructed_binding_blocks_real_submission_with_zero_calls(self, harness):
        env = _make_env(audit_metadata={"workflow_binding_reconstructed": True})
        harness.acquire_lease(env)
        result = harness.submit(env)
        assert result.approved is False
        assert harness.adapter.invocation_count == 0
        assert result.denied_at_check == "21_workflow_binding_not_reconstructed"

    def test_non_reconstructed_binding_is_unaffected(self, harness):
        """False, absent, or any other value never trips the check --
        only a literal True does (never a truthy-but-wrong type). Each
        variant uses its own idempotency_key/authorization_id so the
        three submissions in this loop don't collide with each other on
        check 05 (idempotency_key_unconsumed), which is orthogonal to
        what this test is actually proving."""
        for i, audit_metadata in enumerate(({}, {"workflow_binding_reconstructed": False}, {"workflow_binding_reconstructed": "true"})):
            env = _make_env(
                audit_metadata=audit_metadata, planned_shot_id=f"shot_variant_{i}",
                authorization_id=f"auth_variant_{i}", idempotency_key=f"idem_variant_{i}",
            )
            harness.acquire_lease(env)
            result = harness.submit(env, authorization=_make_auth(env))
            assert result.approved is True, (audit_metadata, result.reason)

    def test_reconstructed_envelope_remains_fully_evaluable_by_shadow_checks(self, harness):
        """run_shadow_checks() never calls _check_workflow_binding_not_
        reconstructed() -- it is structurally absent from the shared
        check functions run_shadow_checks() reuses, so a reconstructed
        envelope's every OTHER dimension is still evaluated normally
        (whatever evidence was actually supplied -- "unverifiable" for
        anything omitted, exactly as run_shadow_checks()'s own contract
        promises), and no CheckOutcome for check 21 appears at all
        (there is nothing to report -- the check simply never runs in
        this path)."""
        env = _make_env(audit_metadata={"workflow_binding_reconstructed": True})
        outcomes = sg.run_shadow_checks(
            env, live_sources=_live_sources_for(env), required_provider="fake", required_model="fake-model",
            expected_deterministic_output_path=env.deterministic_output_path,
            authorization=_make_auth(env), at=AT,
        )
        assert not any(o.status == "fail" for o in outcomes), [o for o in outcomes if o.status == "fail"]
        assert not any(o.check == "21_workflow_binding_not_reconstructed" for o in outcomes)
