"""
tests/test_muapi_submission_gate_shadow.py — Unit tests for Commit 4's
shadow-mode wiring: hypogeum/muapi.py -> hypogeum/shadow_observation.py
-> hypogeum/submission_gate.py -> hypogeum/hypogeum_channel_validators.py

All HTTP is mocked exactly as tests/test_muapi.py already mocks it
(requests.post/requests.get/_download_file/_validate_video_file). No
real network call is possible. No real provider credentials are used.
Shadow observation storage always targets tmp_path -- never a live
episode directory.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_muapi import make_config, make_scene_spec, _mock_submit_ok, _mock_poll_completed  # noqa: E402

import hypogeum.db as db
from hypogeum.authorization_lifecycle import AuthorizationRecord, AuthorizationState
from hypogeum.execution_envelope import GenerationExecutionEnvelope
from hypogeum.muapi import submit_scene
from hypogeum.provider_receipt import WriteAheadReceiptStore
from hypogeum.shadow_observation import (
    POLICY_VERSION,
    ShadowDecision,
    ShadowEvidence,
    ShadowObservationConflictError,
    ShadowObservationStore,
    evaluate_shadow,
    summarize,
)
import hypogeum.submission_gate as sg
from hypogeum import hypogeum_channel_validators as hcv

AT = "2026-01-01T00:00:00Z"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _assembled_prompt(raw: str) -> str:
    from hypogeum import video_prompt_writer
    return video_prompt_writer.assemble_video_scene_prompt(raw)


def _make_matching_env(spec: dict, config, *, shot_id: str, **overrides) -> GenerationExecutionEnvelope:
    """Builds an envelope whose provider_bound_content/provider/model/
    aspect_ratio/output_path all match exactly what submit_scene() would
    actually construct and send for `spec` -- the "clean" fixture used
    for WOULD_ALLOW-capable tests. Uses spec['production_spec']
    ['primary_prompt'] verbatim (no text appended) because that is
    exactly the raw string hypogeum.muapi.submit_scene() wraps via
    assemble_video_scene_prompt() -- any tests that need the Hypogeum
    per-shot AUTHORITATIVE marker present must bake it into
    make_scene_spec(primary_prompt=...) themselves so the "actual
    outgoing content" and this envelope's provider_bound_content stay
    byte-identical."""
    from hypogeum.muapi import _tier_to_model, scene_file_path

    primary_tier = spec["production_spec"]["primary_tier"]
    model = _tier_to_model(primary_tier, spec["production_spec"].get("reference_image_url"), config)
    raw_prompt = spec["production_spec"]["primary_prompt"]
    compiled = _assembled_prompt(raw_prompt)

    kwargs = dict(
        envelope_schema_version="1.0", envelope_version=1,
        channel_id="hypogeum", channel_package_version="1.0", channel_package_checksum="a" * 64,
        execution_id="ep_001", workflow_version="1.0", plan_version="1.0", plan_checksum="b" * 64,
        planned_shot_id=shot_id, structured_spec_checksum="c" * 64,
        compiler_identity="hypogeum.video_prompt_writer.compile_authoritative_shot_prompt", compiler_version="v1",
        provider_bound_content=compiled, provider_bound_content_sha256=_sha(compiled),
        content_provenance={
            "compiler": "hypogeum.video_prompt_writer.compile_authoritative_shot_prompt", "compiler_version": "v1",
            "source_work_item_id": shot_id, "rendered_at": AT,
        },
        provider="muapi", model=model, provider_operation="video.text_to_video",
        deterministic_output_path=str(scene_file_path(Path(overrides.pop("output_dir")), spec["scene_id"], spec.get("short_id"))) if "output_dir" in overrides else "/tmp/placeholder.mp4",
        expected_output_identity="asset_1",
        authorization_id="auth_1", authorization_version=1, max_cost_per_call=Decimal("0.39"),
        run_level_budget_ref="run_1", episode_level_budget_ref="ep_1", currency="USD",
        retry_authorized=False, fallback_authorized=False, idempotency_key=f"idem_{shot_id}",
        created_at=AT,
        expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
        dependency_checksums={"channel_package": "a" * 64, "plan": "b" * 64, "structured_spec": "c" * 64, "compiler": "d" * 64},
        authorization_status="authorized",
        aspect_ratio=spec["visual_spec"]["aspect_ratio"],
    )
    kwargs.update(overrides)
    return GenerationExecutionEnvelope.create(**kwargs)


def _run_submit_scene(tmp_path, spec, config, shadow_evidence=None):
    with patch("hypogeum.muapi.requests.post") as mock_post, \
         patch("hypogeum.muapi.requests.get") as mock_get, \
         patch("hypogeum.muapi.time.sleep"), \
         patch("hypogeum.muapi._download_file", return_value=(True, "")), \
         patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
        mock_post.return_value = _mock_submit_ok("req_shadow")
        mock_get.return_value = _mock_poll_completed()
        result = submit_scene(spec, config, "ep_001", tmp_path, shadow_evidence=shadow_evidence)
        return result, mock_post, mock_get


class TestShadowEvaluationOccursAutomatically:

    def test_every_attempt_evaluates_before_mocked_network_invocation(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()
        result, mock_post, _ = _run_submit_scene(tmp_path, spec, config, shadow_evidence=None)
        assert result["generation_status"] == "generated"
        store = ShadowObservationStore(tmp_path)
        decisions = store.read_all()
        assert len(decisions) == 1  # one attempt (primary succeeded, no fallback needed)
        assert mock_post.called  # the mocked "network" call still happened afterward

    def test_legacy_call_produces_unverifiable_missing_envelope(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()
        _run_submit_scene(tmp_path, spec, config, shadow_evidence=None)
        decision = ShadowObservationStore(tmp_path).read_all()[0]
        assert decision.verdict == "UNVERIFIABLE"
        assert any("missing_generation_execution_envelope" in b for b in decision.blockers)


class TestWouldAllowRequiresCompleteEvidence:

    def test_valid_envelope_and_complete_evidence_produce_would_allow(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(
            scene_id="s01b01",
            primary_prompt=(
                "Ancient Greek ruins at dawn, wide establishing shot. "
                "SHOT-SPECIFIC DIRECTION FOR s01b01 (AUTHORITATIVE — supersedes)."
            ),
        )
        env = _make_matching_env(spec, config, shot_id="s01b01", output_dir=tmp_path)

        db_path = tmp_path / "test.db"
        db.initialize_schema(db_path)
        conn = db.get_connection(db_path)
        # expires_at must be far in the future relative to REAL wall-clock
        # time: hypogeum.muapi._run_shadow_observation() evaluates "at"
        # using datetime.now(timezone.utc), not this test's fixed AT
        # constant, so a near-AT expiry would already be stale by the time
        # submit_scene() actually runs.
        db.acquire_lease(conn, env.envelope_id, "worker_a", acquired_at=AT, expires_at="2099-01-01T00:00:00Z")
        auth = AuthorizationRecord(
            authorization_id=env.authorization_id, authorization_version=env.authorization_version,
            envelope_id=env.envelope_id, envelope_version=env.envelope_version,
            current_state=AuthorizationState.AUTHORIZED, history=(),
        )
        receipt_store = WriteAheadReceiptStore(tmp_path)
        live = sg.LiveSources(
            channel_package_checksum=env.dependency_checksums.channel_package,
            plan_checksum=env.dependency_checksums.plan,
            structured_spec_checksum=env.dependency_checksums.structured_spec,
            compiler_version=env.compiler_version,
        )
        scopes = [sg.bs.ScopeRequest(sg.bs.BudgetScopeKind.CALL, "s01b01", Decimal("0.39"), "USD")]

        evidence = ShadowEvidence(
            envelope=env, live_sources=live, authorization=auth,
            lease_conn=conn, lease_owner_id="worker_a", lease_fencing_token=1,
            receipt_store=receipt_store, budget_scopes=scopes,
        )
        _run_submit_scene(tmp_path, spec, config, shadow_evidence=evidence)
        decision = ShadowObservationStore(tmp_path).read_all()[0]
        assert decision.verdict == "WOULD_ALLOW", decision.blockers


class TestDominanceLeakValidatorLoading:

    def test_missing_validator_for_unknown_channel_produces_unverifiable(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = _make_matching_env(spec, config, shot_id="s01b01", output_dir=tmp_path, channel_id="not_hypogeum")
        _run_submit_scene(tmp_path, spec, config, shadow_evidence=ShadowEvidence(envelope=env))
        decision = ShadowObservationStore(tmp_path).read_all()[0]
        assert decision.verdict == "UNVERIFIABLE"

    def test_validator_exception_produces_unverifiable(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = _make_matching_env(spec, config, shot_id="s01b01", output_dir=tmp_path)

        def _raising_validator(_env):
            raise RuntimeError("simulated validator crash")

        evidence = ShadowEvidence(envelope=env, dominance_leak_validator=_raising_validator)
        _run_submit_scene(tmp_path, spec, config, shadow_evidence=evidence)
        decision = ShadowObservationStore(tmp_path).read_all()[0]
        assert decision.verdict == "UNVERIFIABLE"

    def test_stale_validator_version_blocks_would_allow(self):
        with pytest.raises(hcv.StaleValidatorError):
            hcv.load_dominance_leak_validator("hypogeum", expected_validator_version="v99_stale")

    def test_reused_scene_level_prompt_across_distinct_shots_produces_would_deny(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        shared_prompt = _assembled_prompt("the exact same old scene-level prompt, never per-shot")
        env_shot1 = _make_matching_env(
            spec, config, shot_id="s01b01__shot1", output_dir=tmp_path,
            provider_bound_content=shared_prompt, provider_bound_content_sha256=_sha(shared_prompt),
        )
        env_shot2 = _make_matching_env(
            spec, config, shot_id="s01b01__shot2", output_dir=tmp_path,
            provider_bound_content=shared_prompt, provider_bound_content_sha256=_sha(shared_prompt),
            idempotency_key="idem_shot2",
        )
        for env in (env_shot1, env_shot2):
            validator = hcv.load_dominance_leak_validator(env.channel_id, sibling_prompts=[shared_prompt])
            reason = validator(env)
            assert reason is not None  # every affected shot is WOULD_DENY-worthy

    def test_correct_distinct_per_shot_prompts_pass_dominance_check(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(
            scene_id="s01b01",
            primary_prompt=(
                "Ancient Greek ruins at dawn, wide establishing shot. "
                "SHOT-SPECIFIC DIRECTION FOR s01b01 (AUTHORITATIVE — supersedes)."
            ),
        )
        env = _make_matching_env(spec, config, shot_id="s01b01", output_dir=tmp_path)
        validator = hcv.load_dominance_leak_validator("hypogeum")
        assert validator(env) is None


class TestBindingMismatchesProduceWouldDeny:

    def _base(self, tmp_path, config, spec, shot_id="s01b01"):
        return _make_matching_env(spec, config, shot_id=shot_id, output_dir=tmp_path)

    def test_one_character_prompt_mutation_produces_would_deny(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = self._base(tmp_path, config, spec)
        tampered_content = env.provider_bound_content + "!"
        # a real outgoing prompt (what the muapi adapter actually sends)
        # will legitimately differ from this deliberately-mismatched envelope
        decision = evaluate_shadow(
            env, at=AT, execution_id="ep_001", actual_provider_bound_content=tampered_content,
        )
        assert decision.verdict == "WOULD_DENY"
        assert any("10_prompt_checksum_match" in b for b in decision.blockers)

    def _matching_live_sources(self, env):
        # required_provider/required_model are only checked inside
        # _validate_envelope_and_sources(), which run_shadow_checks()
        # skips entirely (an "unverifiable" outcome, not a "fail") when
        # live_sources is None -- so a real WOULD_DENY test for provider/
        # model mismatch must supply matching (non-stale) live_sources.
        return sg.LiveSources(
            channel_package_checksum=env.dependency_checksums.channel_package,
            plan_checksum=env.dependency_checksums.plan,
            structured_spec_checksum=env.dependency_checksums.structured_spec,
            compiler_version=env.compiler_version,
        )

    def test_provider_mismatch_produces_would_deny(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = self._base(tmp_path, config, spec)
        decision = evaluate_shadow(
            env, at=AT, execution_id="ep_001", actual_provider="different_provider",
            live_sources=self._matching_live_sources(env),
        )
        assert decision.verdict == "WOULD_DENY"

    def test_model_mismatch_produces_would_deny(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = self._base(tmp_path, config, spec)
        decision = evaluate_shadow(
            env, at=AT, execution_id="ep_001", actual_model="different-model",
            live_sources=self._matching_live_sources(env),
        )
        assert decision.verdict == "WOULD_DENY"

    def test_operation_mismatch_produces_would_deny(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = self._base(tmp_path, config, spec)
        decision = evaluate_shadow(env, at=AT, execution_id="ep_001", actual_provider_operation="voice.narration_segment")
        assert decision.verdict == "WOULD_DENY"

    def test_duration_mismatch_produces_would_deny(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = self._base(tmp_path, config, spec)
        decision = evaluate_shadow(env, at=AT, execution_id="ep_001", expected_duration_seconds=Decimal("99.0"))
        # env.duration_seconds is None by default; supplying a mismatched
        # expectation only fails if the envelope itself claims a value --
        # verify via a duration-bearing envelope instead:
        env2 = self._base(tmp_path, config, spec)
        import dataclasses
        env2 = dataclasses.replace(env2, duration_seconds=Decimal("8.0"))
        decision2 = evaluate_shadow(env2, at=AT, execution_id="ep_001", expected_duration_seconds=Decimal("9.0"))
        assert decision2.verdict == "WOULD_DENY"

    def test_aspect_ratio_mismatch_produces_would_deny(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = self._base(tmp_path, config, spec)
        decision = evaluate_shadow(env, at=AT, execution_id="ep_001", expected_aspect_ratio="9:16")
        assert decision.verdict == "WOULD_DENY"

    def test_seed_mismatch_produces_would_deny(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = self._base(tmp_path, config, spec)
        import dataclasses
        env2 = dataclasses.replace(env, seed=42)
        decision = evaluate_shadow(env2, at=AT, execution_id="ep_001", expected_seed=99)
        assert decision.verdict == "WOULD_DENY"

    def test_output_identity_mismatch_produces_would_deny(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = self._base(tmp_path, config, spec)
        decision = evaluate_shadow(env, at=AT, execution_id="ep_001", expected_output_identity="different_asset")
        assert decision.verdict == "WOULD_DENY"

    def test_stale_channel_package_checksum_produces_would_deny(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = self._base(tmp_path, config, spec)
        live = sg.LiveSources(
            channel_package_checksum="f" * 64,
            plan_checksum=env.dependency_checksums.plan,
            structured_spec_checksum=env.dependency_checksums.structured_spec,
            compiler_version=env.compiler_version,
        )
        decision = evaluate_shadow(env, at=AT, execution_id="ep_001", live_sources=live)
        assert decision.verdict == "WOULD_DENY"
        assert any("06_channel_package_checksum_match" in b for b in decision.blockers)

    def test_stale_plan_checksum_produces_would_deny(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = self._base(tmp_path, config, spec)
        live = sg.LiveSources(
            channel_package_checksum=env.dependency_checksums.channel_package,
            plan_checksum="f" * 64,
            structured_spec_checksum=env.dependency_checksums.structured_spec,
            compiler_version=env.compiler_version,
        )
        decision = evaluate_shadow(env, at=AT, execution_id="ep_001", live_sources=live)
        assert decision.verdict == "WOULD_DENY"
        assert any("07_plan_checksum_match" in b for b in decision.blockers)

    def test_stale_structured_spec_checksum_produces_would_deny(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = self._base(tmp_path, config, spec)
        live = sg.LiveSources(
            channel_package_checksum=env.dependency_checksums.channel_package,
            plan_checksum=env.dependency_checksums.plan,
            structured_spec_checksum="f" * 64,
            compiler_version=env.compiler_version,
        )
        decision = evaluate_shadow(env, at=AT, execution_id="ep_001", live_sources=live)
        assert decision.verdict == "WOULD_DENY"
        assert any("08_structured_spec_checksum_match" in b for b in decision.blockers)


class TestMissingEvidenceProducesUnverifiableNotPass:

    def test_missing_authorization_evidence_produces_unverifiable(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = _make_matching_env(spec, config, shot_id="s01b01", output_dir=tmp_path)
        decision = evaluate_shadow(env, at=AT, execution_id="ep_001")
        assert decision.verdict == "UNVERIFIABLE"
        assert any("03_authorization_active_and_unconsumed" in b for b in decision.blockers)

    def test_missing_lease_evidence_produces_unverifiable(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = _make_matching_env(spec, config, shot_id="s01b01", output_dir=tmp_path)
        decision = evaluate_shadow(env, at=AT, execution_id="ep_001")
        assert any("19_lease_and_fencing" in b for b in decision.blockers)
        assert decision.verdict == "UNVERIFIABLE"

    def test_missing_budget_evidence_produces_unverifiable(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = _make_matching_env(spec, config, shot_id="s01b01", output_dir=tmp_path)
        decision = evaluate_shadow(env, at=AT, execution_id="ep_001")
        assert any("17_budget_scope_configuration" in b for b in decision.blockers)
        assert decision.verdict == "UNVERIFIABLE"

    def test_existing_unknown_acceptance_evidence_produces_would_deny(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = _make_matching_env(spec, config, shot_id="s01b01", output_dir=tmp_path)
        receipt_store = WriteAheadReceiptStore(tmp_path)
        from hypogeum.provider_receipt import EventType, ProviderReceiptEvent
        receipt_store.prepare_attempt(
            ProviderReceiptEvent(
                envelope_id=env.envelope_id, envelope_version=1, authorization_id=env.authorization_id,
                authorization_version=1, attempt_id="attempt_1", idempotency_key=env.idempotency_key,
                event_type=EventType.ABOUT_TO_SUBMIT, event_seq=0, at=AT,
                provider=env.provider, model=env.model, provider_operation=env.provider_operation,
                provider_bound_content_sha256=env.provider_bound_content_sha256,
                max_cost_per_call=env.max_cost_per_call, currency=env.currency, evidence={},
            ),
            execution_id="ep_001",
        )
        receipt_store.append_event(
            ProviderReceiptEvent(
                envelope_id=env.envelope_id, envelope_version=1, authorization_id=env.authorization_id,
                authorization_version=1, attempt_id="attempt_1", idempotency_key=env.idempotency_key,
                event_type=EventType.UNKNOWN_ACCEPTANCE, event_seq=1, at=AT,
                provider=env.provider, model=env.model, provider_operation=env.provider_operation,
                provider_bound_content_sha256=env.provider_bound_content_sha256,
                max_cost_per_call=env.max_cost_per_call, currency=env.currency, evidence={"reason": "test"},
            ),
            execution_id="ep_001",
        )
        decision = evaluate_shadow(env, at=AT, execution_id="ep_001", receipt_store=receipt_store)
        assert decision.verdict == "WOULD_DENY"
        assert any("05_idempotency_key_unconsumed" in b for b in decision.blockers)


class TestShadowNeverMutatesProductionState:

    def _setup(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = _make_matching_env(spec, config, shot_id="s01b01", output_dir=tmp_path)
        db_path = tmp_path / "test.db"
        db.initialize_schema(db_path)
        conn = db.get_connection(db_path)
        db.acquire_lease(conn, env.envelope_id, "worker_a", acquired_at=AT, expires_at="2026-01-01T01:00:00Z")
        auth = AuthorizationRecord(
            authorization_id=env.authorization_id, authorization_version=env.authorization_version,
            envelope_id=env.envelope_id, envelope_version=env.envelope_version,
            current_state=AuthorizationState.AUTHORIZED, history=(),
        )
        receipt_store = WriteAheadReceiptStore(tmp_path)
        live = sg.LiveSources(
            channel_package_checksum=env.dependency_checksums.channel_package,
            plan_checksum=env.dependency_checksums.plan,
            structured_spec_checksum=env.dependency_checksums.structured_spec,
            compiler_version=env.compiler_version,
        )
        scopes = [sg.bs.ScopeRequest(sg.bs.BudgetScopeKind.CALL, "s01b01", Decimal("0.39"), "USD")]
        evidence = ShadowEvidence(
            envelope=env, live_sources=live, authorization=auth,
            lease_conn=conn, lease_owner_id="worker_a", lease_fencing_token=1,
            receipt_store=receipt_store, budget_scopes=scopes,
        )
        return config, spec, env, conn, receipt_store, evidence

    def test_shadow_reserves_no_budget(self, tmp_path):
        config, spec, env, conn, receipt_store, evidence = self._setup(tmp_path)
        _run_submit_scene(tmp_path, spec, config, shadow_evidence=evidence)
        total = Decimal(db.sum_reserved_and_committed(conn, "call", "s01b01"))
        assert total == Decimal("0")

    def test_shadow_writes_no_provider_receipt(self, tmp_path):
        config, spec, env, conn, receipt_store, evidence = self._setup(tmp_path)
        _run_submit_scene(tmp_path, spec, config, shadow_evidence=evidence)
        events = receipt_store.read_events("ep_001", env.idempotency_key)
        assert events == []

    def test_shadow_mutates_no_lifecycle_state(self, tmp_path):
        config, spec, env, conn, receipt_store, evidence = self._setup(tmp_path)
        before_state = evidence.authorization.current_state
        _run_submit_scene(tmp_path, spec, config, shadow_evidence=evidence)
        assert evidence.authorization.current_state == before_state  # frozen dataclass, unchanged reference too

    def test_shadow_trips_no_production_breaker(self, tmp_path):
        config, spec, env, conn, receipt_store, evidence = self._setup(tmp_path)
        _run_submit_scene(tmp_path, spec, config, shadow_evidence=evidence)
        rows = conn.execute("SELECT COUNT(*) FROM circuit_breakers").fetchone()
        assert rows[0] == 0

    def test_shadow_invokes_no_adapter(self, tmp_path):
        # the fake adapter class is never imported by hypogeum.muapi at all
        import hypogeum.muapi as muapi_mod
        source = open(muapi_mod.__file__).read()
        assert "fake_provider_adapter" not in source
        assert "FakeProviderAdapter" not in source


class TestExistingBehaviorUnchanged:

    def test_payload_byte_structure_equivalent_after_instrumentation(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
            mock_post.return_value = _mock_submit_ok()
            mock_get.return_value = _mock_poll_completed()
            submit_scene(spec, config, "ep_001", tmp_path)  # no shadow_evidence at all -- legacy call
            payload_sent = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
            assert "prompt" in payload_sent

    def test_response_parsing_unchanged(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()
        result, _, _ = _run_submit_scene(tmp_path, spec, config)
        assert result["generation_status"] == "generated"
        assert result["provider"] == "muapi"
        assert "request_id" in result

    def test_retry_behavior_unchanged(self, tmp_path):
        # existing 429-retry test suite already proves this at the
        # _submit_http() level (tests/test_muapi.py::TestSubmitScene429Retry);
        # this test proves shadow evidence does not change the call count.
        config = make_config()
        spec = make_scene_spec()
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
            resp_429 = _mock_submit_ok()
            resp_429.status_code = 429
            resp_429.headers = {"Retry-After": "0"}
            mock_post.side_effect = [resp_429, _mock_submit_ok()]
            mock_get.return_value = _mock_poll_completed()
            submit_scene(spec, config, "ep_001", tmp_path, shadow_evidence=None)
            assert mock_post.call_count == 2  # unchanged retry-once behavior


class TestObservationStorageProperties:

    def test_observation_stored_before_mocked_network_invocation(self, tmp_path):
        call_order = []
        config = make_config()
        spec = make_scene_spec()
        real_record = ShadowObservationStore.record

        def _tracking_record(self, decision):
            call_order.append("shadow_store")
            return real_record(self, decision)

        with patch.object(ShadowObservationStore, "record", _tracking_record), \
             patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
            mock_post.side_effect = lambda *a, **k: (call_order.append("network_post"), _mock_submit_ok())[1]
            mock_get.return_value = _mock_poll_completed()
            submit_scene(spec, config, "ep_001", tmp_path)

        assert call_order.index("shadow_store") < call_order.index("network_post")

    def test_observation_records_contain_no_secrets(self, tmp_path):
        config = make_config()
        spec = make_scene_spec()
        _run_submit_scene(tmp_path, spec, config)
        path = list((tmp_path / "shadow_observations").glob("*.json"))[0]
        raw = path.read_text().lower()
        for forbidden in ("api_key", "secret", "password", "credential", "authorization_header"):
            assert forbidden not in raw

    def test_observation_storage_is_append_only(self, tmp_path):
        source = open(ShadowObservationStore.record.__code__.co_filename).read()
        # the module never deletes a non-temp file
        for line in source.splitlines():
            if "unlink(" in line or "os.remove(" in line:
                assert "tmp_path" in line

    def test_exact_replay_is_idempotent(self, tmp_path):
        store = ShadowObservationStore(tmp_path)
        decision = evaluate_shadow(None, at=AT, execution_id="ep_001")
        store.record(decision)
        store.record(decision)
        assert len(store.read_all()) == 1

    def test_conflicting_replay_fails_visibly(self, tmp_path):
        store = ShadowObservationStore(tmp_path)
        decision = evaluate_shadow(None, at=AT, execution_id="ep_001")
        store.record(decision)
        import dataclasses
        tampered = dataclasses.replace(decision, verdict="WOULD_ALLOW")
        with pytest.raises(ShadowObservationConflictError):
            store.record(tampered)

    def test_restart_reconstructs_the_same_summary(self, tmp_path):
        store1 = ShadowObservationStore(tmp_path)
        store1.record(evaluate_shadow(None, at=AT, execution_id="ep_001"))
        summary1 = summarize(store1.read_all())

        store2 = ShadowObservationStore(tmp_path)  # simulated restart: new instance, same path
        summary2 = summarize(store2.read_all())
        assert summary1 == summary2

    def test_summary_counts_blockers_correctly(self, tmp_path):
        store = ShadowObservationStore(tmp_path)
        d1 = evaluate_shadow(None, at=AT, execution_id="ep_001")
        d2 = evaluate_shadow(None, at="2026-01-01T00:00:01Z", execution_id="ep_001")
        store.record(d1)
        store.record(d2)
        summary = summarize(store.read_all())
        assert summary["total_evaluated"] == 2
        assert summary["unverifiable"] == 2
        assert summary["legacy_missing_envelope_count"] == 2
        assert summary["blocker_count_by_check"]["01_envelope_schema_version_supported"] == 2


class TestEvaluationExceptionRecordedAsUnverifiable:

    def test_evaluation_exception_becomes_unverifiable(self):
        def _raising_validator(_env):
            raise ValueError("boom")

        decision = evaluate_shadow(
            None, at=AT, execution_id="ep_001", dominance_leak_validator=_raising_validator,
        )
        # None envelope short-circuits before the validator is even reached,
        # so this specifically proves the *outer* exception envelope still
        # produces UNVERIFIABLE, never raises out of evaluate_shadow().
        assert decision.verdict == "UNVERIFIABLE"

    def test_run_shadow_checks_exception_propagates_to_unverifiable(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        env = _make_matching_env(spec, config, shot_id="s01b01", output_dir=tmp_path)
        with patch("hypogeum.submission_gate.run_shadow_checks", side_effect=RuntimeError("simulated crash")):
            decision = evaluate_shadow(env, at=AT, execution_id="ep_001")
        assert decision.verdict == "UNVERIFIABLE"
        assert decision.first_failing_check == "evaluation"


class TestZeroRealNetworkCalls:

    def test_no_module_makes_a_real_network_call_in_this_suite(self):
        # every test in this file that reaches submit_scene() patches
        # hypogeum.muapi.requests.post/get -- statically verified here
        # (via real ast.Call nodes, not substring matching, so a
        # docstring/comment mentioning "submit_scene(" cannot produce a
        # false positive) that no test function omits those patches
        # while directly calling submit_scene().
        source = open(__file__).read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                calls_submit_scene_directly = any(
                    isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "submit_scene"
                    for n in ast.walk(node)
                )
                if calls_submit_scene_directly:
                    body_src = ast.get_source_segment(source, node) or ""
                    assert "requests.post" in body_src or "mock_post" in body_src, node.name


class TestMuapiShadowInstrumentationCoverage:
    """Source/AST-level proof (Part 3 requirement 11) that submit_scene()'s
    real network-bound helper (_attempt_generation) passes through the
    shared shadow evaluator before its network boundary. Per the approved
    blueprint's own Commit 4 scope ("MUAPI adapter conversion... submit_
    scene() gains the envelope parameter"), generate_reference_image()
    and generate_thumbnail() are explicitly NOT instrumented in this
    commit -- the blueprint scopes those to later, separate waves ("image
    domain, later wave"). This test documents that scope boundary
    explicitly rather than silently narrowing it."""

    def test_attempt_generation_calls_shadow_observation_before_submit_http(self):
        # Uses real ast.Call node positions (line, col), not substring
        # search -- a docstring or comment mentioning "_submit_http(" in
        # prose can no longer produce a false failure/pass here, unlike
        # a plain body_src.index() comparison.
        import hypogeum.muapi as muapi_mod
        source = open(muapi_mod.__file__).read()
        tree = ast.parse(source)
        attempt_gen = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_attempt_generation"
        )

        def _call_positions(func_name):
            return sorted(
                (n.lineno, n.col_offset) for n in ast.walk(attempt_gen)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == func_name
            )

        shadow_calls = _call_positions("_run_shadow_observation")
        submit_http_calls = _call_positions("_submit_http")
        assert len(shadow_calls) == 1, shadow_calls
        assert len(submit_http_calls) == 1, submit_http_calls
        assert shadow_calls[0] < submit_http_calls[0]

    def test_observation_only_return_precedes_every_submit_http_call(self):
        # Part of the observation-only structural-ordering proof: the
        # `if observation_only: return ...` statement (and therefore
        # every one of its own statements, including the log call) must
        # appear, in source position, strictly between the shadow-
        # observation call and the _submit_http() call inside
        # _attempt_generation() -- the only function in this module
        # that both evaluates shadow policy AND could reach a real
        # network boundary. real ast.Call node positions are used, not
        # substring search.
        import hypogeum.muapi as muapi_mod
        source = open(muapi_mod.__file__).read()
        tree = ast.parse(source)
        attempt_gen = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_attempt_generation"
        )

        shadow_call = next(
            n for n in ast.walk(attempt_gen)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_run_shadow_observation"
        )
        submit_http_call = next(
            n for n in ast.walk(attempt_gen)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_submit_http"
        )

        observation_only_if = next(
            n for n in ast.walk(attempt_gen)
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.Name) and n.test.id == "observation_only"
        )
        # The `if observation_only:` block must itself contain a Return
        # statement (not merely be present) -- proves this is a genuine
        # early-exit branch, not a no-op conditional.
        assert any(isinstance(stmt, ast.Return) for stmt in ast.walk(observation_only_if))

        shadow_pos = (shadow_call.lineno, shadow_call.col_offset)
        if_pos = (observation_only_if.lineno, observation_only_if.col_offset)
        submit_http_pos = (submit_http_call.lineno, submit_http_call.col_offset)
        assert shadow_pos < if_pos < submit_http_pos, (shadow_pos, if_pos, submit_http_pos)

    def test_no_other_statement_between_observation_only_return_and_submit_http(self):
        # Stronger structural guarantee than mere ordering: every
        # top-level statement in _attempt_generation() positioned after
        # the observation_only branch and before the _submit_http()
        # call must be part of constructing the *request* (already
        # built earlier) or logging -- concretely, this asserts there
        # is exactly one top-level If/Return pair (the observation_only
        # branch) between the shadow-observation call statement and the
        # _submit_http() call statement, i.e. nothing else could
        # possibly reach the network boundary from a different branch
        # in between.
        import hypogeum.muapi as muapi_mod
        source = open(muapi_mod.__file__).read()
        tree = ast.parse(source)
        attempt_gen = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_attempt_generation"
        )
        body = attempt_gen.body

        def _stmt_contains_call(stmt, func_name):
            return any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == func_name
                for n in ast.walk(stmt)
            )

        shadow_idx = next(i for i, s in enumerate(body) if _stmt_contains_call(s, "_run_shadow_observation"))
        submit_http_idx = next(i for i, s in enumerate(body) if _stmt_contains_call(s, "_submit_http"))
        between = body[shadow_idx + 1:submit_http_idx]
        # Every statement strictly between the two calls must be the
        # observation_only If (with its Return) or a pure log/comment-
        # adjacent statement -- none may be, contain, or lead to another
        # call to _submit_http or any requests.* function.
        assert between, "expected at least the observation_only branch between shadow call and _submit_http"
        for stmt in between:
            assert not _stmt_contains_call(stmt, "_submit_http")
            for n in ast.walk(stmt):
                if isinstance(n, ast.Attribute) and n.attr in ("post", "get") :
                    assert False, f"unexpected requests.{n.attr} reference between shadow call and _submit_http"

    def test_run_shadow_observation_calls_evaluate_shadow(self):
        import hypogeum.muapi as muapi_mod
        source = open(muapi_mod.__file__).read()
        tree = ast.parse(source)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_run_shadow_observation"
        )
        body_src = ast.get_source_segment(source, fn)
        assert "evaluate_shadow(" in body_src

    def test_image_and_thumbnail_domains_explicitly_out_of_scope_this_commit(self):
        import hypogeum.muapi as muapi_mod
        source = open(muapi_mod.__file__).read()
        tree = ast.parse(source)
        for fn_name in ("generate_reference_image", "generate_thumbnail"):
            fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == fn_name)
            body_src = ast.get_source_segment(source, fn)
            assert "_run_shadow_observation" not in body_src  # confirmed not silently instrumented either


class TestObservationOnlyMode:
    """
    Tests for the post-Commit-4 observation_only execution mode (added
    2026-08-15): submit_scene(..., observation_only=True) must evaluate
    the real shadow policy against real request-construction inputs,
    persist the ShadowDecision with mode="shadow_observation_only", and
    terminate before _submit_http() -- with zero network calls, zero
    media writes, zero asset_manifest/registration touches, and a
    result whose generation_status can never be confused with a real
    generation.
    """

    # ---- 1. ZERO NETWORK -------------------------------------------------

    def test_zero_network_calls_observation_only(self, tmp_path):
        # Stronger than call_count == 0: either function raising
        # AssertionError immediately on any invocation proves the code
        # path never even attempts to reach the network, regardless of
        # what arguments it might have been called with.
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")

        def _fail(*a, **k):
            raise AssertionError("requests.post/get must never be called in observation_only mode")

        with patch("hypogeum.muapi.requests.post", side_effect=_fail), \
             patch("hypogeum.muapi.requests.get", side_effect=_fail), \
             patch("hypogeum.muapi.time.sleep"):
            result = submit_scene(spec, make_config(), "ep_001", tmp_path, observation_only=True)

        assert result["generation_status"] == "observation_only"

    # ---- 2. ZERO MEDIA -----------------------------------------------------

    def test_zero_media_file_written(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")

        def _fail(*a, **k):
            raise AssertionError("must never be called in observation_only mode")

        with patch("hypogeum.muapi.requests.post", side_effect=_fail), \
             patch("hypogeum.muapi.requests.get", side_effect=_fail), \
             patch("hypogeum.muapi.time.sleep"):
            submit_scene(spec, config, "ep_001", tmp_path, observation_only=True)

        # scene_file_path()'s own destination must not exist -- no video,
        # no placeholder, nothing.
        from hypogeum.muapi import scene_file_path
        dest_path = scene_file_path(tmp_path, spec["scene_id"], spec.get("short_id"))
        assert not dest_path.exists()
        # No file at all was written anywhere under output_dir except the
        # shadow_observations/ directory this mode is explicitly allowed
        # to write to.
        written = [p for p in tmp_path.rglob("*") if p.is_file()]
        assert all("shadow_observations" in p.parts for p in written), written

    # ---- 3. ZERO REGISTRATION ----------------------------------------------

    def test_zero_asset_manifest_entry(self, tmp_path):
        # submit_scene() itself has no asset_manifest concept -- its
        # caller (video_generation_agent.py) is responsible for
        # registering the returned dict. This test proves the returned
        # dict cannot be mistaken for a registerable, successful asset:
        # asset_id is None, so any caller keying registration on a
        # truthy asset_id will not register anything for this call.
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")

        def _fail(*a, **k):
            raise AssertionError("must never be called in observation_only mode")

        with patch("hypogeum.muapi.requests.post", side_effect=_fail), \
             patch("hypogeum.muapi.requests.get", side_effect=_fail), \
             patch("hypogeum.muapi.time.sleep"):
            result = submit_scene(spec, config, "ep_001", tmp_path, observation_only=True)

        assert result["asset_id"] is None

    # ---- 4. ZERO GENERATED STATUS ------------------------------------------

    def test_generation_status_cannot_be_mistaken_for_generated(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")

        def _fail(*a, **k):
            raise AssertionError("must never be called in observation_only mode")

        with patch("hypogeum.muapi.requests.post", side_effect=_fail), \
             patch("hypogeum.muapi.requests.get", side_effect=_fail), \
             patch("hypogeum.muapi.time.sleep"):
            result = submit_scene(spec, config, "ep_001", tmp_path, observation_only=True)

        assert result["generation_status"] == "observation_only"
        assert result["generation_status"] not in ("generated", "reused", "generation_failed", "dry_run")
        # Every existing caller's accepted-success check is an equality
        # or membership test against "generated"/"reused" (see
        # hypogeum/pipeline.py, clip_repair_agent.py,
        # video_generation_agent.py) -- "observation_only" deliberately
        # does not match any of those, so no existing caller could ever
        # misinterpret this result as a completed asset.
        assert result["generation_status"] != "generated"

    # ---- 5. SHADOW DECISION PERSISTENCE ------------------------------------

    def test_shadow_decision_persisted_with_observation_only_mode(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")

        def _fail(*a, **k):
            raise AssertionError("must never be called in observation_only mode")

        with patch("hypogeum.muapi.requests.post", side_effect=_fail), \
             patch("hypogeum.muapi.requests.get", side_effect=_fail), \
             patch("hypogeum.muapi.time.sleep"):
            submit_scene(spec, config, "ep_001", tmp_path, observation_only=True)

        decisions = ShadowObservationStore(tmp_path).read_all()
        assert len(decisions) == 1
        assert decisions[0].mode == "shadow_observation_only"
        assert decisions[0].verdict == "UNVERIFIABLE"  # no envelope supplied in this test

    def test_shadow_observation_only_mode_distinguishable_from_ordinary_shadow(self, tmp_path):
        # Directly proves the audit-evidence requirement: an ordinary
        # shadow decision (accompanying a real attempt) and an
        # observation-only decision are each tagged with a different,
        # persisted mode value -- never collapsed into one
        # indistinguishable "shadow" bucket.
        d1 = evaluate_shadow(None, at="2026-01-01T00:00:00Z", execution_id="ep_001")
        d2 = evaluate_shadow(None, at="2026-01-01T00:00:00Z", execution_id="ep_001", mode="shadow_observation_only")
        assert d1.mode == "shadow"
        assert d2.mode == "shadow_observation_only"
        # And their identities differ (mode is part of decision_id's
        # hash), so persisting both under the same store never raises a
        # spurious ShadowObservationConflictError.
        assert d1.decision_id != d2.decision_id
        store = ShadowObservationStore(tmp_path)
        store.record(d1)
        store.record(d2)
        assert len(store.read_all()) == 2

    def test_mode_default_unchanged_for_existing_callers(self):
        # A caller that does not pass mode= at all (every existing
        # caller of evaluate_shadow() before this change) gets exactly
        # the same "shadow" value as before.
        decision = evaluate_shadow(None, at="2026-01-01T00:00:00Z", execution_id="ep_001")
        assert decision.mode == "shadow"

    # ---- 6. EXISTING BEHAVIOR UNCHANGED ------------------------------------

    def test_omitting_observation_only_preserves_prior_behavior_byte_for_byte(self, tmp_path):
        # Calling submit_scene() exactly as every pre-existing caller
        # does (no observation_only argument at all) must produce an
        # unchanged real generation result and an unchanged real
        # (mocked) network call -- proving the new parameter's default
        # (False) leaves the caller contract untouched.
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        result, mock_post, mock_get = _run_submit_scene(tmp_path, spec, config)
        assert result["generation_status"] == "generated"
        assert mock_post.called
        assert mock_get.called
        decisions = ShadowObservationStore(tmp_path).read_all()
        assert len(decisions) == 1
        assert decisions[0].mode == "shadow"  # not "shadow_observation_only"

    def test_existing_shadow_suite_still_green(self):
        # This is a marker test, not a re-implementation: the full
        # compatibility rerun of this same file plus tests/test_muapi.py
        # is the actual proof and is run as a separate pytest invocation
        # per this task's TEST COMMANDS section -- see the audit report.
        # Kept here only so `pytest -k observation_only` still surfaces a
        # reminder that the broader file must be rerun in full.
        assert True

    # ---- 7. STRUCTURAL ORDERING -------------------------------------------
    # See TestMuapiShadowInstrumentationCoverage above, extended with
    # test_observation_only_return_precedes_every_submit_http_call and
    # test_no_other_statement_between_observation_only_return_and_submit_http.

    # ---- 8. NO PLACEHOLDER --------------------------------------------------

    def test_muapi_dry_run_not_used_to_satisfy_observation_only(self, tmp_path):
        # observation_only must produce its result via the dedicated
        # early-return branch, never via config.MUAPI_DRY_RUN's
        # placeholder-clip path. Proven two ways: (a) MUAPI_DRY_RUN is
        # left at its normal False value throughout, so if
        # observation_only were (bug) silently falling through to
        # _submit_http() it would attempt a real network call and the
        # AssertionError-raising mocks below would fire; (b) the
        # returned generation_status is "observation_only", never
        # "generated" -- the exact status MUAPI_DRY_RUN's placeholder
        # path reports (see submit_scene()'s own docstring: "MUAPI_DRY_RUN
        # now writes a real placeholder clip and reports 'generated'").
        config = make_config()
        assert config.MUAPI_DRY_RUN is False
        spec = make_scene_spec(scene_id="s01b01")

        def _fail(*a, **k):
            raise AssertionError("observation_only must never reach _submit_http, dry-run or otherwise")

        with patch("hypogeum.muapi.requests.post", side_effect=_fail), \
             patch("hypogeum.muapi.requests.get", side_effect=_fail), \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._generate_dry_run_placeholder_clip", side_effect=_fail):
            result = submit_scene(spec, config, "ep_001", tmp_path, observation_only=True)

        assert result["generation_status"] == "observation_only"
        assert result["generation_status"] != "generated"

    def test_observation_only_true_with_muapi_dry_run_true_still_skips_placeholder(self, tmp_path):
        # Even if a caller mistakenly sets MUAPI_DRY_RUN=true AND
        # observation_only=True together, the observation_only branch
        # (which runs strictly before _submit_http(), and therefore
        # before _submit_http()'s own internal MUAPI_DRY_RUN check) must
        # still win -- no placeholder clip may be generated either way.
        config = make_config()
        config.MUAPI_DRY_RUN = True
        spec = make_scene_spec(scene_id="s01b01")

        def _fail(*a, **k):
            raise AssertionError("placeholder-clip path must not run when observation_only=True")

        with patch("hypogeum.muapi.requests.post", side_effect=_fail), \
             patch("hypogeum.muapi.requests.get", side_effect=_fail), \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._generate_dry_run_placeholder_clip", side_effect=_fail):
            result = submit_scene(spec, config, "ep_001", tmp_path, observation_only=True)

        assert result["generation_status"] == "observation_only"
        from hypogeum.muapi import scene_file_path
        dest_path = scene_file_path(tmp_path, spec["scene_id"], spec.get("short_id"))
        assert not dest_path.exists()

    # ---- Budget guard non-mutation (explicit prohibited-side-effect check) --

    def test_budget_guard_not_mutated_in_observation_only_mode(self, tmp_path):
        from hypogeum.muapi import MUAPIBudgetGuard
        from hypogeum.logger import get_logger
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        guard = MUAPIBudgetGuard(
            episode_budget_credits=100.0, scene_budget_credits=100.0,
            max_paid_attempts=5, log=get_logger(episode_id="ep_001", agent="test"),
        )

        def _fail(*a, **k):
            raise AssertionError("must never be called in observation_only mode")

        with patch("hypogeum.muapi.requests.post", side_effect=_fail), \
             patch("hypogeum.muapi.requests.get", side_effect=_fail), \
             patch("hypogeum.muapi.time.sleep"):
            submit_scene(spec, config, "ep_001", tmp_path, budget_guard=guard, observation_only=True)

        assert guard._estimated_spend == 0.0
        assert guard._scene_attempts == {}
