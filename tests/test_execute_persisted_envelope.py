"""
tests/test_execute_persisted_envelope.py — Focused, fake-adapter-only
tests for hypogeum/execute_persisted_envelope.py: the missing executor
that submits an already-persisted, already-AUTHORIZED
GenerationExecutionEnvelope through the real, unmodified
hypogeum.submission_gate.submit() path.

All fixtures live under pytest's own tmp_path -- never the live
ep_f3619022 episode directory, never the live .hypogeum-assets/
hypogeum.db. Every test uses hypogeum.providers.fake_provider_adapter.
FakeProviderAdapter -- zero network access, zero real provider calls.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from test_migrate_legacy_manifest_to_envelopes import (  # noqa: E402
    _bootstrap_channel, _config, _entry, _render_prompt, _scene, _shot,
    _with_resolved_workflow_identity, _write_episode_fixture,
)

import migrate_legacy_manifest_to_envelopes as mle  # noqa: E402

import hypogeum.authorization_store as astore  # noqa: E402
import hypogeum.db as db  # noqa: E402
import hypogeum.execute_persisted_envelope as executor  # noqa: E402
import hypogeum.adapter_registry as adapter_registry  # noqa: E402
from hypogeum.authorization_lifecycle import AuthorizationRecord, AuthorizationState  # noqa: E402
from hypogeum.execution_envelope import GenerationExecutionEnvelope  # noqa: E402
from hypogeum.logger import get_logger  # noqa: E402
from hypogeum.providers.fake_provider_adapter import FakeProviderAdapter  # noqa: E402

AT = "2026-01-01T00:00:00Z"
_LOG = get_logger(episode_id="ep_test", agent="test_executor")


@pytest.fixture(autouse=True)
def _reset_adapter_cache():
    """FakeProviderAdapter is a process-wide singleton per class
    (hypogeum.adapter_registry.get_or_create_adapter_instance()) --
    without resetting it, a capability_id that happens to be
    deterministically identical across two different test fixtures
    (same shot/manifest content) would collide against the SAME
    adapter instance's _consumed_capability_ids set, producing a false
    CapabilityAlreadyConsumedError in an unrelated test. Same pattern
    tests/test_muapi_gate_path.py already established."""
    adapter_registry.reset_adapter_instance_cache()
    yield
    adapter_registry.reset_adapter_instance_cache()


def _make_one_entry_episode(tmp_path: Path, episode_id: str = "ep_test"):
    shot = _shot("b_001__shot1", "b_001")
    scene = _scene("s01", "b_001", [shot], primary_tier=2)
    rendered = _render_prompt(scene, shot)
    entry = _entry(shot["shot_id"], "b_001", rendered["prompt_checksum"], auth_index=1)
    manifest_path, _plan_md5 = _write_episode_fixture(tmp_path, episode_id, [entry], [scene])
    plan_path = tmp_path / "episodes" / episode_id / f"cinematic_coverage_plan_{episode_id}.json"
    visual_package_path = tmp_path / "episodes" / episode_id / f"visual_package_{episode_id}.json"
    return manifest_path, plan_path, visual_package_path


def _make_authorized_fixture(tmp_path, monkeypatch, *, authorization_version=2):
    """Compiles one real entry via the real migration-tool compiler,
    persists its envelope, and builds an AUTHORIZED AuthorizationRecord
    for it directly (this test file is about the EXECUTOR, not the
    activation mechanism -- that has its own dedicated tests in
    tests/test_authorization_authority.py)."""
    _with_resolved_workflow_identity(monkeypatch)
    manifest_path, plan_path, visual_package_path = _make_one_entry_episode(tmp_path)
    db_path = tmp_path / "auth.db"
    db.initialize_schema(db_path)
    _bootstrap_channel(db_path)

    args = mle.build_arg_parser().parse_args([
        "--episode-id", "ep_test", "--dry-run", "--manifest-path", str(manifest_path),
        "--assets-root", str(tmp_path), "--db-path", str(db_path),
    ])
    report = mle.compile_manifest_to_envelopes(
        args, config=_config(), authorization_version=authorization_version, include_envelope_dicts=True,
        tag_as_reconstructed=False,
    )
    entry = report["entries"][0]
    assert entry["envelope_fields_valid"] and entry["dependency_identity_available"], entry
    envelope = GenerationExecutionEnvelope.from_dict(entry["envelope_dict"])
    authorization_id = envelope.authorization_id

    conn = db.get_connection(db_path)
    try:
        astore.persist_execution_envelope(conn, envelope)
        rec = AuthorizationRecord.create(
            authorization_id=authorization_id, authorization_version=envelope.authorization_version,
            envelope_id=envelope.envelope_id, envelope_version=envelope.envelope_version,
        )
        rec = rec.transition(AuthorizationState.COMPILED, evidence={"compiler_identity": "x", "compiler_version": "1"}, at=AT)
        rec = rec.transition(AuthorizationState.AWAITING_AUTHORIZATION, evidence={"preflight_result": "ok"}, at=AT)
        rec = rec.transition(
            AuthorizationState.AUTHORIZED,
            evidence={
                "authorized_by": "test_operator", "authorization_method": "explicit_human_approval_proposal_batch",
                "approval_artifact_id": "test_approval_1", "approval_subject_checksum": "f" * 64,
            },
            at=AT,
        )
        astore.persist_authorization_record(conn, rec)
        conn.commit()
    finally:
        conn.close()

    return dict(
        authorization_id=authorization_id, envelope=envelope, db_path=db_path, assets_root=tmp_path,
        manifest_path=manifest_path, plan_path=plan_path, visual_package_path=visual_package_path,
    )


def _run(fixture, **overrides):
    kwargs = dict(
        authorization_id=fixture["authorization_id"], assets_root=fixture["assets_root"],
        manifest_path=fixture["manifest_path"], plan_path=fixture["plan_path"],
        visual_package_path=fixture["visual_package_path"], channel_id="hypogeum",
        episode_budget_scope_ceiling=Decimal("33.15"), config=_config(), log=_LOG,
        db_path=fixture["db_path"], adapter_class=FakeProviderAdapter,
    )
    kwargs.update(overrides)
    return executor.submit_persisted_authorized_envelope(**kwargs)


class TestSuccessfulExecution:

    def test_accepted_outcome_persists_receipt_and_charges_exactly_max_cost(self, tmp_path, monkeypatch):
        fixture = _make_authorized_fixture(tmp_path, monkeypatch)
        result = _run(fixture, adapter_mode="success")

        assert result.approved is True
        assert result.adapter_invoked is True
        assert result.outcome == "accepted"
        assert result.charged_amount == str(fixture["envelope"].max_cost_per_call)
        assert result.provider_job_id is not None
        assert result.deterministic_output_path == fixture["envelope"].deterministic_output_path

        conn = db.get_connection(fixture["db_path"])
        try:
            rec = astore.get_authorization_record(conn, fixture["authorization_id"])
            assert rec.current_state == AuthorizationState.SUBMITTED
        finally:
            conn.close()


class TestPreBoundaryDenialZeroCalls:

    def test_output_collision_denies_before_adapter_invocation(self, tmp_path, monkeypatch):
        fixture = _make_authorized_fixture(tmp_path, monkeypatch)
        dest = Path(fixture["envelope"].deterministic_output_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"pre-existing unrelated file")

        result = _run(fixture, adapter_mode="success")
        assert result.approved is False
        assert result.adapter_invoked is False
        assert result.denied_at_check == "16_output_collision"
        # File left completely untouched -- never overwritten.
        assert dest.read_bytes() == b"pre-existing unrelated file"


class TestDuplicatePrevention:

    def test_second_submission_after_accepted_is_denied_never_reinvoked(self, tmp_path, monkeypatch):
        fixture = _make_authorized_fixture(tmp_path, monkeypatch)
        first = _run(fixture, adapter_mode="success")
        assert first.approved is True

        with pytest.raises(executor.ExecutePersistedEnvelopeError):
            _run(fixture, adapter_mode="success")


class TestUnknownAcceptance:

    def test_unknown_acceptance_denies_and_never_auto_retries(self, tmp_path, monkeypatch):
        fixture = _make_authorized_fixture(tmp_path, monkeypatch)
        result = _run(fixture, adapter_mode="unknown_acceptance")

        assert result.approved is False
        assert result.adapter_invoked is True
        assert result.outcome == "unknown_acceptance"

        # A second call for the SAME authorization_id must never
        # silently reattempt the provider boundary -- record is no
        # longer AUTHORIZED (moved to SUBMITTING), so this fails closed
        # at the pre-boundary state check, zero further adapter calls.
        with pytest.raises(executor.ExecutePersistedEnvelopeError):
            _run(fixture, adapter_mode="success")


class TestStaleAuthorization:

    def test_non_authorized_state_fails_closed_before_any_call(self, tmp_path, monkeypatch):
        fixture = _make_authorized_fixture(tmp_path, monkeypatch)
        conn = db.get_connection(fixture["db_path"])
        try:
            rec = astore.get_authorization_record(conn, fixture["authorization_id"])
            rec2 = rec.transition(
                AuthorizationState.INVALIDATED_SOURCE_CHANGED,
                evidence={"changed_dependency": "x", "old_checksum": "y", "new_checksum": "z"}, at=AT,
            )
            astore.persist_authorization_record(conn, rec2)
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(executor.ExecutePersistedEnvelopeError):
            _run(fixture, adapter_mode="success")

    def test_missing_record_fails_closed(self, tmp_path, monkeypatch):
        fixture = _make_authorized_fixture(tmp_path, monkeypatch)
        with pytest.raises(executor.ExecutePersistedEnvelopeError):
            _run(fixture, authorization_id="genauth_ep_test_test:999", adapter_mode="success")


class TestStaleDependencies:

    def test_stale_channel_package_denies_before_adapter_invocation(self, tmp_path, monkeypatch):
        fixture = _make_authorized_fixture(tmp_path, monkeypatch)

        import hypogeum.submission_gate as sg

        def _fake_live_sources(**kwargs):
            return sg.LiveSources(
                channel_package_checksum="0" * 64, plan_checksum=fixture["envelope"].dependency_checksums.plan,
                structured_spec_checksum=fixture["envelope"].dependency_checksums.structured_spec,
                compiler_version=fixture["envelope"].compiler_version,
            )

        monkeypatch.setattr(executor, "resolve_live_sources", _fake_live_sources)
        result = _run(fixture, adapter_mode="success")
        assert result.approved is False
        assert result.adapter_invoked is False
        assert "channel_package" in (result.reason or "").lower() or "channel_package" in (result.denied_at_check or "")


class TestLeaseFencingFailure:

    def test_lease_already_held_denies_with_zero_calls(self, tmp_path, monkeypatch):
        fixture = _make_authorized_fixture(tmp_path, monkeypatch)
        conn = db.get_connection(fixture["db_path"])
        try:
            held = db.acquire_lease(
                conn, fixture["envelope"].envelope_id, "someone_else", acquired_at=AT,
                expires_at="2099-01-01T00:00:00Z",
            )
            assert held is not None
            conn.commit()
        finally:
            conn.close()

        result = _run(fixture, adapter_mode="success")
        assert result.approved is False
        assert result.adapter_invoked is False
        assert result.denied_at_check == "lease_unavailable"


class TestRestartRecovery:

    def test_replay_after_accepted_is_denied_not_reinvoked_but_original_receipt_intact(self, tmp_path, monkeypatch):
        fixture = _make_authorized_fixture(tmp_path, monkeypatch)
        first = _run(fixture, adapter_mode="success")
        assert first.approved is True
        first_job_id = first.provider_job_id

        # Simulate a process restart: nothing in-memory survives, only
        # durable state. A fresh call for the same authorization_id must
        # recognize it is no longer AUTHORIZED and refuse to touch the
        # provider boundary again -- the original acceptance evidence
        # must remain exactly as it was.
        with pytest.raises(executor.ExecutePersistedEnvelopeError):
            _run(fixture, adapter_mode="success")

        conn = db.get_connection(fixture["db_path"])
        try:
            rec = astore.get_authorization_record(conn, fixture["authorization_id"])
            submitted_evidence = next(h.evidence for h in rec.history if h.to_state == AuthorizationState.SUBMITTED)
            assert submitted_evidence["provider_job_id"] == first_job_id
        finally:
            conn.close()
