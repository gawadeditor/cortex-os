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

import hypogeum.authorization_authority as authority
import hypogeum.authorization_store as astore
import hypogeum.channel_config as channel_config
import hypogeum.workflow_executor as workflow_executor
from hypogeum.approval_artifact import GrantBindingApproval, sign_approval_artifact
from hypogeum.muapi import submit_scene

AT = "2026-01-01T00:00:00Z"


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


class TestNoGrantLeavesLegacyPathUnaffected:

    def test_channel_id_supplied_but_no_grant_uses_legacy_path(self, tmp_path):
        """Even with channel_id explicitly supplied, if no grant
        resolves (the real 'hypogeum' case, always, until Enforcement
        Cutover) the call falls straight through to the unchanged
        legacy attempt structure -- proven here via the same HTTP mocks
        tests/test_muapi.py already uses for the legacy path."""
        config = make_config()
        spec = make_scene_spec(scene_id="s01b01")
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", return_value=(True, "")), \
             patch("hypogeum.muapi._validate_video_file", return_value=(True, "")):
            mock_post.return_value = _mock_submit_ok("req_legacy")
            mock_get.return_value = _mock_poll_completed()
            result = submit_scene(spec, config, "ep_gate_test", tmp_path, channel_id="nobody_has_a_grant_for_this_channel")
        assert result["generation_status"] == "generated"


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
