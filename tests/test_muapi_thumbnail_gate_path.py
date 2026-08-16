"""
tests/test_muapi_thumbnail_gate_path.py — Integration tests for the
new, grant-derived gate path in hypogeum/muapi.py::generate_thumbnail()
(muapi provider branch only) (plan §5.5/§6 Commit F, thumbnail domain).

Mirrors tests/test_muapi_gate_path.py's structure exactly (video
domain) -- see that file's own docstring for the full rationale.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_muapi import make_config  # noqa: E402

import hypogeum.authorization_authority as authority
import hypogeum.authorization_store as astore
import hypogeum.channel_config as channel_config
import hypogeum.workflow_executor as workflow_executor
from hypogeum.approval_artifact import GrantBindingApproval, sign_approval_artifact
from hypogeum.muapi import generate_thumbnail

AT = "2026-01-01T00:00:00Z"


@pytest.fixture
def keypair(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    monkeypatch.setattr(authority, "_load_trusted_public_key", lambda: pub)
    return priv, pub


@pytest.fixture(autouse=True)
def _isolated_channel_identity(monkeypatch):
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
        provider="muapi", model=model, provider_operation="image.thumbnail",
        currency="USD", max_cost_per_call=Decimal("0.10"), max_calls=max_calls, max_total_spend=max_total_spend,
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
            allowed_bindings=(GrantBindingApproval("muapi", model, "image.thumbnail"),),
            currency="USD", max_cost_per_call=Decimal("0.10"), max_total_spend=max_total_spend,
            max_calls=max_calls, retry_authorized=False, fallback_authorized=False,
            issued_at=AT, expires_at="2099-01-01T00:00:00Z", approver_identity="operator_x", nonce="nonce_thumb_gate_1",
        ),
        private_key=priv,
    )
    return authority.register_run_grant(
        grant_id="g_thumb_gate_test", grant_version=1, channel_id="test_channel", episode_id="ep_gate_test",
        workflow_id="hypogeum_main_production", workflow_version="1.0.0", workflow_checksum="a" * 64,
        bindings=bindings, max_total_spend_all_bindings=max_total_spend, currency="USD",
        expires_at="2099-01-01T00:00:00Z", approval_artifact=artifact, db_path=db_path,
    )


def _dl_side_effect(url, dest_path, log):
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1280, 720), color=(80, 80, 80)).save(str(dest_path), "JPEG", quality=90)
    return True, ""


def _thumbnail_config():
    # Ensure the muapi provider is first in the chain and its model is
    # NOT a GPT Image 2 model, so the adapter's simpler job_payload
    # branch is exercised (matches make_config()'s existing default).
    return make_config()


class TestNoGrantLeavesLegacyPathUnaffected:

    def test_channel_id_supplied_but_no_grant_uses_legacy_path(self, tmp_path):
        config = _thumbnail_config()
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", side_effect=_dl_side_effect):
            from test_muapi import _mock_submit_ok, _mock_poll_completed
            mock_post.return_value = _mock_submit_ok("req_legacy")
            mock_get.return_value = _mock_poll_completed()
            result = generate_thumbnail(
                "main", "an ancient tomb entrance at dusk", "16:9", config, "ep_gate_test", tmp_path,
                channel_id="nobody_has_a_grant_for_this_channel",
            )
        assert result.provider_used == "muapi"
        assert result.local_path.exists()


class TestGrantDerivedGatePath:

    def test_gate_path_end_to_end(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        config = _thumbnail_config()

        import hypogeum.db as db
        db.initialize_schema(db_path)
        _register_grant(priv, db_path, model=config.THUMBNAIL_IMAGE_MODEL)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get, \
             patch("hypogeum.muapi.time.sleep"), \
             patch("hypogeum.muapi._download_file", side_effect=_dl_side_effect):
            from test_muapi import _mock_submit_ok, _mock_poll_completed
            mock_post.return_value = _mock_submit_ok("req_gate")
            mock_get.return_value = _mock_poll_completed()
            result = generate_thumbnail(
                "main", "an ancient tomb entrance at dusk", "16:9", config, "ep_gate_test", tmp_path,
                channel_id="test_channel",
            )

        assert result.provider_used == "muapi"
        assert result.used_fallback is False
        assert result.local_path.exists()

    def test_gate_path_capacity_exhausted_fails_closed_with_zero_network_calls(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        config = _thumbnail_config()

        import hypogeum.db as db
        db.initialize_schema(db_path)
        _register_grant(priv, db_path, model=config.THUMBNAIL_IMAGE_MODEL, max_calls=0, max_total_spend=Decimal("0.00"))
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        # config.THUMBNAIL_FALLBACK_PROVIDER/_FALLBACK2_PROVIDER are also
        # pinned to "muapi" by make_config() -- force a single-provider
        # chain so the gate-denied primary attempt doesn't get retried
        # under the *legacy* path by the outer provider_chain loop
        # (that would still make zero network calls, but the assertion
        # below is cleaner if the error type is exactly the gate denial).
        config.THUMBNAIL_FALLBACK_PROVIDER = ""
        config.THUMBNAIL_FALLBACK2_PROVIDER = ""
        config.THUMBNAIL_FALLBACK_MODEL = config.THUMBNAIL_IMAGE_MODEL

        from hypogeum.muapi import MUAPIError
        with patch("hypogeum.muapi.requests.post") as mock_post, \
             patch("hypogeum.muapi.requests.get") as mock_get:
            with pytest.raises(MUAPIError):
                generate_thumbnail(
                    "main", "an ancient tomb entrance at dusk", "16:9", config, "ep_gate_test", tmp_path,
                    channel_id="test_channel",
                )
            assert mock_post.called is False
            assert mock_get.called is False
