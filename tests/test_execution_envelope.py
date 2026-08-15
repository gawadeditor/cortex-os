"""
tests/test_execution_envelope.py — Unit tests for
hypogeum/execution_envelope.py (Commit 1, 2026-08-14)

Every test constructs envelopes purely in memory. No test imports
hypogeum.muapi, requests, httpx, or any network-capable module, and none
touches the filesystem or an episode directory.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from hypogeum.execution_envelope import (
    ContentProvenance,
    DependencyChecksums,
    EnvelopeIntegrityError,
    EnvelopeSchemaError,
    ExpirationPolicy,
    GenerationExecutionEnvelope,
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _base_kwargs(**overrides) -> dict:
    prompt = overrides.pop("operation_payload_content", "a fully compiled prompt string")
    kwargs = dict(
        envelope_schema_version="1.0",
        envelope_version=1,
        channel_id="test_channel",
        channel_package_version="1.0",
        channel_package_checksum="a" * 64,
        execution_id="exec_001",
        workflow_version="1.0",
        plan_version="1.0",
        plan_checksum="b" * 64,
        planned_shot_id="work_item_1",
        structured_spec_checksum="c" * 64,
        compiler_identity="pkg.module.compile_fn",
        compiler_version="deadbeefcafe",
        operation_payload_content=prompt,
        operation_payload_sha256=_sha256(prompt),
        operation_payload_schema_version=overrides.pop("operation_payload_schema_version", "video.text_to_video/1.0"),
        content_provenance={
            "compiler": "pkg.module.compile_fn", "compiler_version": "deadbeefcafe",
            "source_work_item_id": "work_item_1", "rendered_at": "2026-01-01T00:00:00Z",
        },
        provider="test_provider",
        model="test-model-v1",
        provider_operation="video.text_to_video",
        deterministic_output_path="/tmp/does-not-need-to-exist.mp4",
        expected_output_identity="asset_placeholder",
        authorization_id="auth_1",
        authorization_version=1,
        max_cost_per_call=Decimal("0.39"),
        run_level_budget_ref="run_budget_1",
        episode_level_budget_ref="episode_budget_1",
        currency="USD",
        retry_authorized=False,
        fallback_authorized=False,
        idempotency_key="idem_1",
        created_at="2026-01-01T00:00:00Z",
        expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
        dependency_checksums={
            "channel_package": "a" * 64, "plan": "b" * 64, "structured_spec": "c" * 64, "compiler": "d" * 64,
        },
    )
    kwargs.update(overrides)
    return kwargs


def _make(**overrides) -> GenerationExecutionEnvelope:
    return GenerationExecutionEnvelope.create(**_base_kwargs(**overrides))


class TestDeterministicIdentity:

    def test_insertion_order_does_not_affect_canonical_bytes_or_id(self):
        kwargs_a = _base_kwargs()
        kwargs_b = dict(reversed(list(kwargs_a.items())))  # different dict insertion order
        env_a = GenerationExecutionEnvelope.create(**kwargs_a)
        env_b = GenerationExecutionEnvelope.create(**kwargs_b)
        assert env_a.canonical_json_bytes() == env_b.canonical_json_bytes()
        assert env_a.envelope_id == env_b.envelope_id

    def test_nested_dependency_checksums_key_order_does_not_affect_id(self):
        deps_forward = {"channel_package": "a" * 64, "plan": "b" * 64, "structured_spec": "c" * 64, "compiler": "d" * 64}
        deps_reversed = dict(reversed(list(deps_forward.items())))
        env_a = _make(dependency_checksums=deps_forward)
        env_b = _make(dependency_checksums=deps_reversed)
        assert env_a.envelope_id == env_b.envelope_id

    def test_prompt_change_changes_checksum_and_id(self):
        env_a = _make()
        prompt_b = env_a.operation_payload_content + "!"
        env_b = _make(operation_payload_content=prompt_b, operation_payload_sha256=_sha256(prompt_b))
        assert env_a.operation_payload_sha256 != env_b.operation_payload_sha256
        assert env_a.envelope_id != env_b.envelope_id

    def test_plan_checksum_change_changes_id(self):
        env_a = _make()
        env_b = _make(plan_checksum="e" * 64)
        assert env_a.envelope_id != env_b.envelope_id

    def test_channel_package_checksum_change_changes_id(self):
        env_a = _make()
        env_b = _make(channel_package_checksum="f" * 64)
        assert env_a.envelope_id != env_b.envelope_id

    def test_compiler_version_change_changes_id(self):
        env_a = _make()
        env_b = _make(compiler_version="differentversion")
        assert env_a.envelope_id != env_b.envelope_id

    def test_provider_model_operation_change_changes_id(self):
        base = _make()
        assert base.envelope_id != _make(provider="other_provider").envelope_id
        assert base.envelope_id != _make(model="other-model").envelope_id
        assert base.envelope_id != _make(provider_operation="voice.narration_segment").envelope_id

    def test_duration_aspect_ratio_seed_output_identity_change_changes_id(self):
        base = _make()
        assert base.envelope_id != _make(duration_seconds=Decimal("9.0")).envelope_id
        assert base.envelope_id != _make(aspect_ratio="9:16").envelope_id
        assert base.envelope_id != _make(seed=42).envelope_id
        assert base.envelope_id != _make(expected_output_identity="different_asset").envelope_id

    def test_mutable_authorization_status_does_not_change_id(self):
        env_a = _make()
        env_b = _make(authorization_status="authorized")
        assert env_a.envelope_id == env_b.envelope_id

    def test_created_at_and_audit_metadata_do_not_change_id(self):
        env_a = _make()
        env_b = _make(created_at="2030-06-01T12:00:00Z", audit_metadata={"created_by": "someone", "creation_reason": "x"})
        assert env_a.envelope_id == env_b.envelope_id

    def test_content_provenance_rendered_at_does_not_change_id(self):
        env_a = _make()
        cp = dict(env_a.to_dict()["content_provenance"])
        cp["rendered_at"] = "2099-01-01T00:00:00Z"
        env_b = _make(content_provenance=cp)
        assert env_a.envelope_id == env_b.envelope_id


class TestPromptIntegrity:

    def test_prompt_bytes_survive_round_trip_exactly(self):
        prompt = "SCENE CONTINUITY: exact bytes\nwith a newline\tand a tab — em dash too."
        env = _make(operation_payload_content=prompt, operation_payload_sha256=_sha256(prompt))
        d = env.to_dict()
        restored = GenerationExecutionEnvelope.from_dict(d)
        assert restored.operation_payload_content == prompt

    def test_empty_prompt_fails_integrity_validation(self):
        with pytest.raises(EnvelopeSchemaError) as exc:
            _make(operation_payload_content="", operation_payload_sha256=_sha256(""))
        assert exc.value.field_name == "operation_payload_content"

    def test_prompt_checksum_mismatch_fails_integrity_validation(self):
        with pytest.raises(EnvelopeIntegrityError) as exc:
            _make(operation_payload_sha256="0" * 64)
        assert exc.value.check == "operation_payload_sha256"

    def test_provider_adapter_cannot_reconstruct_prompt(self):
        """The canonical serialization never normalizes/trims/rewrites
        the prompt -- to_dict()'s operation_payload_content is byte-for-byte
        what was submitted at construction time."""
        prompt = "  leading and trailing whitespace preserved  "
        env = _make(operation_payload_content=prompt, operation_payload_sha256=_sha256(prompt))
        assert env.to_dict()["operation_payload_content"] == prompt


class TestFailClosedConstruction:

    def test_missing_required_field_fails_closed(self):
        kwargs = _base_kwargs()
        del kwargs["plan_checksum"]
        with pytest.raises(EnvelopeSchemaError) as exc:
            GenerationExecutionEnvelope.create(**kwargs)
        assert exc.value.field_name == "required_fields"

    def test_unsupported_schema_version_fails_closed(self):
        with pytest.raises(EnvelopeSchemaError) as exc:
            _make(envelope_schema_version="99.0")
        assert exc.value.field_name == "envelope_schema_version"

    def test_malformed_checksum_fails_closed(self):
        with pytest.raises(EnvelopeSchemaError) as exc:
            _make(plan_checksum="not-a-checksum")
        assert exc.value.field_name == "plan_checksum"

    def test_malformed_checksum_wrong_length_fails_closed(self):
        with pytest.raises(EnvelopeSchemaError):
            _make(structured_spec_checksum="ab" * 10)

    def test_negative_cost_fails_closed(self):
        with pytest.raises(EnvelopeSchemaError) as exc:
            _make(max_cost_per_call=Decimal("-0.01"))
        assert exc.value.field_name == "max_cost_per_call"

    def test_float_cost_rejected_in_favor_of_decimal(self):
        with pytest.raises(EnvelopeSchemaError):
            _make(max_cost_per_call=0.39)

    def test_unsupported_currency_representation_fails_closed(self):
        with pytest.raises(EnvelopeSchemaError) as exc:
            _make(currency="US Dollars")
        assert exc.value.field_name == "currency"

    def test_inconsistent_provider_operation_format_fails_closed(self):
        with pytest.raises(EnvelopeSchemaError) as exc:
            _make(provider_operation="not_a_domain_dot_operation")
        assert exc.value.field_name == "provider_operation"

    def test_incomplete_dependency_identity_fails_closed(self):
        kwargs = _base_kwargs()
        kwargs["dependency_checksums"] = {"channel_package": "a" * 64, "plan": "b" * 64}  # missing keys
        with pytest.raises(EnvelopeSchemaError) as exc:
            GenerationExecutionEnvelope.create(**kwargs)
        assert exc.value.field_name == "dependency_identity"

    def test_retry_authorized_without_budget_ref_fails_closed(self):
        with pytest.raises(EnvelopeSchemaError) as exc:
            _make(retry_authorized=True)
        assert exc.value.field_name == "retry_budget_ref"

    def test_fallback_authorized_without_full_fields_fails_closed(self):
        with pytest.raises(EnvelopeSchemaError) as exc:
            _make(fallback_authorized=True)
        assert exc.value.field_name == "fallback_authorized"

    def test_supplied_envelope_id_mismatch_fails_closed(self):
        with pytest.raises(EnvelopeIntegrityError) as exc:
            GenerationExecutionEnvelope.create(envelope_id="0" * 64, **_base_kwargs())
        assert exc.value.check == "envelope_id"

    def test_unrecognized_authorization_status_fails_closed(self):
        with pytest.raises(EnvelopeSchemaError) as exc:
            _make(authorization_status="not_a_real_state")
        assert exc.value.field_name == "authorization_status"


class TestExactDecimalMoney:

    def test_cost_is_exact_decimal_not_binary_float(self):
        env = _make(max_cost_per_call=Decimal("0.1") + Decimal("0.2"))
        assert env.max_cost_per_call == Decimal("0.3")
        assert isinstance(env.max_cost_per_call, Decimal)

    def test_cost_survives_string_round_trip_exactly(self):
        env = _make(max_cost_per_call=Decimal("34.32"))
        restored = GenerationExecutionEnvelope.from_dict(env.to_dict())
        assert restored.max_cost_per_call == Decimal("34.32")


class TestDeterministicSerialization:

    def test_serialization_deterministic_across_repeated_construction(self):
        bytes_1 = _make().canonical_json_bytes()
        bytes_2 = _make().canonical_json_bytes()
        assert bytes_1 == bytes_2

    def test_canonical_bytes_are_valid_sorted_compact_json(self):
        raw = _make().canonical_json_bytes()
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)
        # sort_keys + compact separators means re-dumping with the same
        # settings must reproduce byte-identical output.
        redumped = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        assert redumped == raw

    def test_envelope_id_is_sha256_of_canonical_bytes(self):
        env = _make()
        assert env.envelope_id == hashlib.sha256(env.canonical_json_bytes()).hexdigest()


class TestNoNetworkOrEpisodeDependency:

    def test_module_imports_no_network_capable_library(self):
        import hypogeum.execution_envelope as mod
        source = open(mod.__file__).read()
        for forbidden in ("import requests", "import httpx", "urllib.request", "socket"):
            assert forbidden not in source, forbidden
