"""
tests/test_muapi_gate_path.py — Integration tests for the new,
grant-derived gate path in hypogeum/muapi.py::submit_scene() (plan
§5.5/§6 Commit F).

Proves: (a) supplying channel_id with no real grant leaves the legacy
path completely unaffected (every test in tests/test_muapi.py already
proves this too, since none of them pass channel_id at all -- these
tests prove it explicitly for a caller that DOES pass channel_id but
still has no grant); (b) with a synthetic, verified grant registered,
submit_scene() routes through hypogeum.submission_gate.submit() and
hypogeum.providers.muapi_video_adapter.MuapiVideoAdapter end-to-end,
using mocked HTTP exactly as tests/test_muapi.py already does -- no
real network call is possible.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_muapi import make_config, make_scene_spec, _mock_submit_ok, _mock_poll_completed  # noqa: E402

import hypogeum.adapter_registry as adapter_registry
import hypogeum.authorization_authority as authority
import hypogeum.authorization_store as astore
import hypogeum.channel_config as channel_config
import hypogeum.workflow_executor as workflow_executor
from hypogeum.approval_artifact import GrantBindingApproval, sign_approval_artifact
from hypogeum.muapi import submit_scene

AT = "2026-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def _reset_adapter_cache():
    adapter_registry.reset_adapter_instance_cache()
    yield
    adapter_registry.reset_adapter_instance_cache()


@pytest.fixture
def keypair(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    monkeypatch.setattr(authority, "_load_trusted_public_key", lambda: pub)
    return priv, pub


@pytest.fixture(autouse=True)
def _isolated_channel_identity(monkeypatch):
    """The gate path resolves live channel-package/workflow identity --
    isolate both to fixed, deterministic values so this test never
    touches the real 'hypogeum' channel's DB-backed profiles or the
    real channel_packages/hypogeum/ artifact."""
    monkeypatch.setattr(
        channel_config, "compute_channel_package_identity",
        lambda channel_id, db_path=None: {"available": True, "checksum": "b" * 64, "version": "1.0"},
    )
    monkeypatch.setattr(
        workflow_executor, "resolve_workflow_binding",
        lambda channel_id, workflow_id, workflow_version, **kw: {
            "workflow_id": workflow_id, "workflow_version": workflow_version,
            "workflow_checksum": "a" * 64, "registry_definition_schema_version": "1.0",
            "registration_identity": "test-registration", "evaluation_timestamp": AT,
        },
    )


def _persist_matching_plan(output_dir, *, episode_id="ep_gate_test", scene_id="s01b01",
                            model, prompt="Ancient Greek ruins at dawn, wide establishing shot.") -> None:
    """
    hypogeum.muapi._submit_scene_via_gate() now checks the real scene
    submission against a persisted hypogeum.production_plan.ProductionPlan
    before compiling an envelope at all (2026-08-24 correction) -- these
    gate-path integration tests exercise the grant/envelope/submission_gate
    machinery itself, not plan compilation (that has its own coverage in
    test_production_plan.py), so this persists a minimal, directly
    constructed plan declaring exactly the one asset each test submits.
    """
    from hypogeum.muapi import _compute_asset_id
    from hypogeum.production_plan import (
        PLAN_SCHEMA_VERSION,
        ROLE_REQUIRED,
        PlannedAsset,
        ProductionPlan,
        persist_production_plan,
        prompt_fingerprint,
    )
    from hypogeum.video_prompt_writer import assemble_video_scene_prompt

    # The declared fingerprint must match the FULLY ASSEMBLED prompt
    # submit_scene() actually submits (it always runs the raw
    # production_spec.primary_prompt through assemble_video_scene_prompt()
    # before calling _submit_scene_via_gate() -- see that function's own
    # plan-conformance check).
    assembled_prompt = assemble_video_scene_prompt(prompt)
    asset = PlannedAsset(
        deliverable_id="main_episode", beat_id="b_001", shot_id="b_001__primary",
        asset_id=_compute_asset_id(episode_id, scene_id, None), scene_id=scene_id, short_id=None,
        role=ROLE_REQUIRED, prompt=assembled_prompt, prompt_fingerprint=prompt_fingerprint(assembled_prompt),
        shot_signature="", aspect_ratio="16:9", requested_duration_seconds=8.0,
        conservative_usable_duration_seconds=8.0, provider="muapi", model=model,
        unit_price_usd=0.39, enhancement_price_usd=0.05, max_chargeable_attempts=2,
    )
    plan = ProductionPlan(
        schema_version=PLAN_SCHEMA_VERSION, channel_id="test_channel", episode_id=episode_id,
        deliverable_contract_id="test", deliverable_ids=["main_episode"], assets=[asset],
        thumbnail_main_candidate_count=3, thumbnail_short_candidate_cap=3,
        thumbnail_attempts_per_candidate=2, thumbnail_unit_price_usd=0.09,
        thumbnail_cost_normal_usd=0.27, thumbnail_cost_maximum_usd=0.27,
        narration_char_count=0, narration_price_per_char_usd=0.0003,
        narration_cost_usd=0.0, narration_is_realized=False,
        music_section_count=0, music_seconds_per_section=120.0,
        music_price_per_second_usd=0.02, music_cost_usd=0.0,
        planning_call_estimate_usd=0.05, enhancement_cost_maximum_usd=0.05,
        normal_expected_cost_usd=0.71, maximum_bounded_cost_usd=0.90,
        content_hash="a" * 64, compiled_at="2026-08-24T00:00:00+00:00",
    )
    persist_production_plan(plan, Path(output_dir))


def _register_grant(priv, db_path, *, model, max_calls=5, max_total_spend=Decimal("10.00")):
    bindings = [astore.GrantBinding(
        provider="muapi", model=model, provider_operation="video.text_to_video",
        currency="USD", max_cost_per_call=Decimal("2.50"), max_calls=max_calls, max_total_spend=max_total_spend,
    )]
    checksum = authority.compute_grant_checksum(
        channel_id="test_channel", episode_id="ep_gate_test", workflow_id="hypogeum_main_production",
        workflow_version="1.0.0", workflow_checksum="a" * 64, bindings=bindings,
        max_total_spend_all_bindings=max_total_spend, currency="USD", expires_at="2099-01-01T00:00:00Z",
    )
    artifact = sign_approval_artifact(
        dict(
            approval_id="appr1", approval_version=1, approval_type="grant", subject_checksum=checksum,
            channel_id="test_channel", scope_id="ep_gate_test", workflow_id="hypogeum_main_production",
            workflow_version="1.0.0", workflow_checksum="a" * 64, channel_package_version="1.0",
            channel_package_checksum="b" * 64,
            allowed_bindings=(GrantBindingApproval("muapi", model, "video.text_to_video"),),
            currency="USD", max_cost_per_call=Decimal("2.50"), max_total_spend=max_total_spend,
            max_calls=max_calls, retry_authorized=False, fallback_authorized=False,
            issued_at=AT, expires_at="2099-01-01T00:00:00Z", approver_identity="operator_x", nonce="nonce_gate_1",
        ),
        private_key=priv,
    )
    return authority.register_run_grant(
        grant_id="g_gate_test", grant_version=1, channel_id="test_channel", episode_id="ep_gate_test",
        workflow_id="hypogeum_main_production", workflow_version="1.0.0", workflow_checksum="a" * 64,
        bindings=bindings, max_total_spend_all_bindings=max_total_spend, currency="USD",
        expires_at="2099-01-01T00:00:00Z", approval_artifact=artifact, db_path=db_path,
    )


class TestOmittedChannelIdLeavesLegacyPathUnaffected:

    def test_channel_id_omitted_entirely_uses_legacy_path(self, tmp_path):
        """A caller that never identifies a channel at all (channel_id
        omitted, the default -- every video-generation caller not yet
        migrated to pass one) is completely unaffected by the
        Production Enforcement Cutover -- proven here via the same
        HTTP mocks tests/test_muapi.py already uses for the legacy
        path."""
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
            mock_post.return_value = _mock_submit_ok("req_legacy")
            mock_get.return_value = _mock_poll_completed()
            result = submit_scene(spec, config, "ep_gate_test", tmp_path)
        assert result["generation_status"] == "generated"


class TestChannelIdSuppliedWithNoGrantIsEnforcedNotBypassed:
    """Production Enforcement Cutover (video domain only): once a
    caller supplies channel_id at all, it is committed to the gate --
    a missing grant is a clean, zero-provider-call denial, never a
    silent fallback to the legacy paid path. This is the exact
    behavior change the cutover requires; it applies to any
    (channel_id, episode_id), never anything hypogeum-specific."""

    def test_channel_id_supplied_but_no_grant_fails_closed_with_zero_provider_calls(self, tmp_path):
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get:
            result = submit_scene(spec, config, "ep_gate_test", tmp_path, channel_id="nobody_has_a_grant_for_this_channel")
            assert mock_post.called is False
            assert mock_get.called is False
        assert result["generation_status"] == "generation_failed"
        assert result["asset_id"] is None
        assert "no_active_authorization_grant" in result["failure_reason"]

    def test_denial_reason_names_the_channel_id(self, tmp_path):
        """The failure_reason must name the exact channel_id that
        triggered enforcement -- generic across any channel, never a
        hardcoded 'hypogeum' string."""
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        with patch("hypogeum.muapi.requests.post"), patch("hypogeum.muapi.requests.get"):
            result = submit_scene(spec, config, "ep_gate_test", tmp_path, channel_id="some_other_channel")
        assert "some_other_channel" in result["failure_reason"]


class TestGrantDerivedGatePath:

    def test_gate_path_end_to_end(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        import hypogeum.db as db
        db.initialize_schema(db_path)
        grant = _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL)
        assert grant.status == "active"

        # _submit_scene_via_gate() resolves everything against the
        # PRODUCTION db_path internally (get_connection()/authority
        # calls with no db_path override, exactly as production code
        # will) -- point the production db_path at this test's isolated
        # file for the duration of this test only.
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        _persist_matching_plan(tmp_path, model=config.MUAPI_T2_MODEL)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
            mock_post.return_value = _mock_submit_ok("req_gate")
            mock_get.return_value = _mock_poll_completed()
            result = submit_scene(spec, config, "ep_gate_test", tmp_path, channel_id="test_channel")

        assert result["generation_status"] == "generated", result
        assert result["provider"] == "muapi"

    def test_accepted_call_advances_authorization_past_authorized(self, keypair, tmp_path, monkeypatch):
        """The lifecycle-reconciliation fix: a real, accepted, charged
        execution through the gate must not leave its AuthorizationRecord
        stranded at AUTHORIZED. hypogeum.muapi._submit_scene_via_gate()
        must call hypogeum.authorization_store.record_submission_
        outcome() itself (submission_gate.py is structurally forbidden
        from persisting AuthorizationRecord state -- see
        tests/test_authorization_authority_trust_boundary.py)."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        import hypogeum.db as db
        db.initialize_schema(db_path)
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        _persist_matching_plan(tmp_path, model=config.MUAPI_T2_MODEL)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
            mock_post.return_value = _mock_submit_ok("req_lifecycle")
            mock_get.return_value = _mock_poll_completed()
            result = submit_scene(spec, config, "ep_gate_test", tmp_path, channel_id="test_channel")

        assert result["generation_status"] == "generated", result
        conn = db.get_connection(db_path)
        try:
            authorization_id = "ep_gate_test:s01b01:primary"
            record = astore.get_authorization_record(conn, authorization_id)
            assert record is not None
            from hypogeum.authorization_lifecycle import AuthorizationState
            assert record.current_state == AuthorizationState.SUBMITTED
            submitted_event = next(e for e in record.history if e.to_state == AuthorizationState.SUBMITTED)
            assert submitted_event.evidence["provider_job_id"] == "req_lifecycle"
        finally:
            conn.close()

    def test_gate_path_capacity_exhausted_fails_closed_with_zero_network_calls(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        import hypogeum.db as db
        db.initialize_schema(db_path)
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL, max_calls=0, max_total_spend=Decimal("0.00"))
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get:
            result = submit_scene(spec, config, "ep_gate_test", tmp_path, channel_id="test_channel")
            assert mock_post.called is False
            assert mock_get.called is False

        assert result["generation_status"] == "generation_failed"
        assert "gate_path" in result["failure_reason"]

    def test_exact_provider_request_preserves_prompt_bytes_and_execution_binding(self, keypair, tmp_path, monkeypatch):
        """The validated provider request submitted through the gate
        must carry the exact same prompt bytes and execution binding
        (aspect_ratio, duration, model) submit_scene() would have sent
        via the legacy path -- the cutover changes control flow only,
        never the request semantics."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        import hypogeum.db as db
        db.initialize_schema(db_path)
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        _persist_matching_plan(tmp_path, model=config.MUAPI_T2_MODEL)

        import hypogeum.video_prompt_writer as video_prompt_writer
        expected_prompt = video_prompt_writer.assemble_video_scene_prompt(
            spec["production_spec"]["primary_prompt"],
        )

        captured = {}

        def _capture_post(url, headers=None, json=None, **kw):
            captured["url"] = url
            captured["body"] = json
            return _mock_submit_ok("req_exact")

        with patch("hypogeum.muapi.requests.post", side_effect=_capture_post), \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
            mock_get.return_value = _mock_poll_completed()
            result = submit_scene(spec, config, "ep_gate_test", tmp_path, channel_id="test_channel")

        assert result["generation_status"] == "generated", result
        assert captured["body"]["prompt"] == expected_prompt
        assert captured["body"]["aspect_ratio"] == spec["visual_spec"]["aspect_ratio"]

    def test_second_submission_with_same_idempotency_key_cannot_auto_retry(self, keypair, tmp_path, monkeypatch):
        """Duplicate/unknown acceptance can never trigger an automatic
        second real provider call for the same attempt -- a second
        submit_scene() call for the identical scene, after a first
        real submission already succeeded, must be denied (idempotency
        key already has attempt evidence), not silently reattempted."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        import hypogeum.db as db
        db.initialize_schema(db_path)
        _register_grant(priv, db_path, model=config.MUAPI_T2_MODEL, max_calls=5, max_total_spend=Decimal("10.00"))
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        _persist_matching_plan(tmp_path, model=config.MUAPI_T2_MODEL)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
            mock_post.return_value = _mock_submit_ok("req_first")
            mock_get.return_value = _mock_poll_completed()
            first = submit_scene(spec, config, "ep_gate_test", tmp_path, channel_id="test_channel")
        assert first["generation_status"] == "generated", first

        # Simulate resume: delete the downloaded file so submit_scene()'s
        # own resume-safety check doesn't short-circuit before ever
        # reaching the gate -- the SAME idempotency_key must still be
        # rejected as already-consumed, proving no automatic retry path
        # exists through the enforced gate.
        from hypogeum.muapi import scene_file_path
        dest_path = scene_file_path(tmp_path, "s01b01", None)
        if dest_path.exists():
            dest_path.unlink()

        with patch("hypogeum.muapi.requests.post") as mock_post2, \
             patch("hypogeum.muapi.requests.get") as mock_get2:
            second = submit_scene(spec, config, "ep_gate_test", tmp_path, channel_id="test_channel")
            assert mock_post2.called is False
            assert mock_get2.called is False
        assert second["generation_status"] == "generation_failed"

    def test_future_episode_and_channel_use_the_same_generic_mechanism(self, keypair, tmp_path, monkeypatch):
        """Enforcement is Factory-Core-generic: a completely different,
        synthetic (channel_id, episode_id) pair -- never 'hypogeum' or
        'ep_gate_test' -- gets the exact same fail-closed-with-zero-
        calls behavior with no episode-specific code anywhere in the
        mechanism."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        import hypogeum.db as db
        db.initialize_schema(db_path)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get:
            result = submit_scene(
                spec, config, "ep_some_future_episode", tmp_path,
                channel_id="some_future_channel_never_seen_before",
            )
            assert mock_post.called is False
            assert mock_get.called is False
        assert result["generation_status"] == "generation_failed"
        assert "no_active_authorization_grant" in result["failure_reason"]
        assert "some_future_channel_never_seen_before" in result["failure_reason"]
