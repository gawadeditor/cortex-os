"""
tests/test_muapi_upscale_gate_path.py — Integration tests for the new,
grant-derived gate path in hypogeum/providers/muapi_video_upscaler.py::
MuapiVideoUpscalerProvider.enhance() (plan §5.5/§6 Commit J).

Mirrors tests/test_muapi_gate_path.py's exact structure and coverage:
(a) supplying channel_id with no real grant leaves the legacy path
completely unaffected; (b) with a synthetic, verified grant registered,
enhance() routes through hypogeum.submission_gate.submit() and
hypogeum.providers.muapi_upscale_adapter.MuapiUpscaleAdapter end-to-end,
using mocked HTTP exactly as tests/test_source_enhancement.py's own
MuapiVideoUpscalerProvider tests already do; (c) grant capacity
exhaustion fails closed with zero network calls -- returned as
EnhancementResult(success=False, ...), never raised, honoring
EnhancementProvider.enhance()'s own no-raise contract.
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_muapi import make_config  # noqa: E402

import hypogeum.adapter_registry as adapter_registry
import hypogeum.authorization_authority as authority
import hypogeum.authorization_store as astore
import hypogeum.channel_config as channel_config
import hypogeum.providers.muapi_video_upscaler as upscaler_module
import hypogeum.workflow_executor as workflow_executor
from hypogeum.approval_artifact import GrantBindingApproval, sign_approval_artifact
from hypogeum.providers.enhancement import EnhancementResult
from hypogeum.providers.muapi_video_upscaler import PROVIDER_NAME, MODEL_NAME, MuapiVideoUpscalerProvider

AT = "2026-01-01T00:00:00Z"


def _make_clip(path: Path, width: int, height: int, duration: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s={width}x{height}:r=24:d={duration}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, timeout=30,
    )


@pytest.fixture(autouse=True)
def _reset_adapter_cache():
    adapter_registry.reset_adapter_instance_cache()
    yield
    adapter_registry.reset_adapter_instance_cache()


@pytest.fixture(autouse=True)
def _isolated_assets_root(monkeypatch, tmp_path):
    """_enhance_via_gate() writes its write-ahead receipts under the
    module-level _ASSETS_ROOT constant (mirrors the exact production
    default every other domain's real output_dir/assets_root already
    resolves to). Redirect it to this test's own tmp_path, exactly like
    the db._DEFAULT_DB_PATH monkeypatch convention used elsewhere."""
    monkeypatch.setattr(upscaler_module, "_ASSETS_ROOT", tmp_path)


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


def _register_grant(priv, db_path, *, max_calls=5, max_total_spend=Decimal("10.00")):
    bindings = [astore.GrantBinding(
        provider=PROVIDER_NAME, model=MODEL_NAME, provider_operation="upscale.video",
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
            allowed_bindings=(GrantBindingApproval(PROVIDER_NAME, MODEL_NAME, "upscale.video"),),
            currency="USD", max_cost_per_call=Decimal("0.10"), max_total_spend=max_total_spend,
            max_calls=max_calls, retry_authorized=False, fallback_authorized=False,
            issued_at=AT, expires_at="2099-01-01T00:00:00Z", approver_identity="operator_x", nonce="nonce_upscale_gate_1",
        ),
        private_key=priv,
    )
    return authority.register_run_grant(
        grant_id="g_upscale_gate_test", grant_version=1, channel_id="test_channel", episode_id="ep_gate_test",
        workflow_id="hypogeum_main_production", workflow_version="1.0.0", workflow_checksum="a" * 64,
        bindings=bindings, max_total_spend_all_bindings=max_total_spend, currency="USD",
        expires_at="2099-01-01T00:00:00Z", approval_artifact=artifact, db_path=db_path,
    )


class TestNoGrantLeavesLegacyPathUnaffected:

    def test_channel_id_supplied_but_no_grant_uses_legacy_path(self, tmp_path):
        """Even with channel_id explicitly supplied, if no grant
        resolves (the real 'hypogeum' case, always, until Enforcement
        Cutover) the call falls straight through to the unchanged
        legacy upload/submit/poll/download sequence -- proven here via
        the same HTTP mocks tests/test_source_enhancement.py already
        uses for the legacy path."""
        config = make_config()
        source = tmp_path / "source.mp4"
        _make_clip(source, 1280, 720)
        dest = tmp_path / "dest.mp4"

        upload_resp = MagicMock(status_code=200)
        upload_resp.json.return_value = {"url": "https://cdn.muapi.ai/outputs/uploaded.mp4"}
        submit_resp = MagicMock(status_code=200)
        submit_resp.json.return_value = {"request_id": "req-legacy", "status": "processing", "cost": {"amount_usd": 0.08}}
        poll_resp = MagicMock(status_code=200)
        poll_resp.json.return_value = {"status": "completed", "outputs": ["https://cdn.muapi.ai/outputs/result.mp4"]}
        download_resp = MagicMock(status_code=200, content=source.read_bytes())

        with patch("hypogeum.providers.muapi_video_upscaler.requests.post", side_effect=[upload_resp, submit_resp]), \
             patch("hypogeum.providers.muapi_video_upscaler.requests.get", side_effect=[poll_resp, download_resp]), \
             patch("hypogeum.finance.record_cost"):
            provider = MuapiVideoUpscalerProvider()
            result = provider.enhance(
                source_path=source, dest_path=dest, target_width=1920, target_height=1080,
                target_resolution_label="1080p", config=config, episode_id="ep_gate_test", scene_id="s01",
                log=MagicMock(), channel_id="nobody_has_a_grant_for_this_channel",
            )

        assert isinstance(result, EnhancementResult)
        assert result.success is True
        assert result.request_id == "req-legacy"


class TestGrantDerivedGatePath:

    def test_gate_path_end_to_end(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        config = make_config()
        source = tmp_path / "source.mp4"
        _make_clip(source, 1280, 720)
        dest = tmp_path / "dest.mp4"
        import hypogeum.db as db
        db.initialize_schema(db_path)
        grant = _register_grant(priv, db_path)
        assert grant.status == "active"

        # _enhance_via_gate() resolves everything against the
        # PRODUCTION db_path internally (get_connection()/authority
        # calls with no db_path override, exactly as production code
        # will) -- point the production db_path at this test's isolated
        # file for the duration of this test only.
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        upload_resp = MagicMock(status_code=200)
        upload_resp.json.return_value = {"url": "https://cdn.muapi.ai/outputs/uploaded.mp4"}
        submit_resp = MagicMock(status_code=200)
        submit_resp.json.return_value = {"request_id": "req-gate", "status": "processing", "cost": {"amount_usd": 0.08}}
        poll_resp = MagicMock(status_code=200)
        poll_resp.json.return_value = {"status": "completed", "outputs": ["https://cdn.muapi.ai/outputs/result.mp4"]}
        download_resp = MagicMock(status_code=200, content=source.read_bytes())

        with patch("hypogeum.providers.muapi_video_upscaler.requests.post", side_effect=[upload_resp, submit_resp]), \
             patch("hypogeum.providers.muapi_video_upscaler.requests.get", side_effect=[poll_resp, download_resp]), \
             patch("hypogeum.finance.record_cost"):
            provider = MuapiVideoUpscalerProvider()
            result = provider.enhance(
                source_path=source, dest_path=dest, target_width=1920, target_height=1080,
                target_resolution_label="1080p", config=config, episode_id="ep_gate_test", scene_id="s01",
                log=MagicMock(), channel_id="test_channel",
            )

        assert result.success is True, result.failure_reason
        assert result.request_id == "req-gate"
        assert result.cost_usd == 0.08
        assert dest.exists()

    def test_gate_path_capacity_exhausted_fails_closed_with_zero_network_calls(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        config = make_config()
        source = tmp_path / "source.mp4"
        _make_clip(source, 1280, 720)
        dest = tmp_path / "dest.mp4"
        import hypogeum.db as db
        db.initialize_schema(db_path)
        _register_grant(priv, db_path, max_calls=0, max_total_spend=Decimal("0.00"))
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        with patch("hypogeum.providers.muapi_video_upscaler.requests.post") as mock_post, \
             patch("hypogeum.providers.muapi_video_upscaler.requests.get") as mock_get:
            provider = MuapiVideoUpscalerProvider()
            result = provider.enhance(
                source_path=source, dest_path=dest, target_width=1920, target_height=1080,
                target_resolution_label="1080p", config=config, episode_id="ep_gate_test", scene_id="s01",
                log=MagicMock(), channel_id="test_channel",
            )
            assert mock_post.called is False
            assert mock_get.called is False

        # EnhancementProvider.enhance()'s own contract forbids raising for
        # provider-side/gate-side failures -- a denial surfaces as a
        # failed EnhancementResult, never an exception.
        assert result.success is False
        assert "gate" in result.failure_reason
