"""
tests/test_elevenlabs_voice_gate_path.py — Integration tests for the new,
grant-derived gate path in hypogeum/elevenlabs.py::generate_narration_segment()
(plan §5.5/§6 Commit G).

Mirrors tests/test_muapi_gate_path.py's exact structure and coverage:
(a) supplying channel_id with no real grant leaves the legacy path
completely unaffected; (b) with a synthetic, verified grant registered,
generate_narration_segment() routes through hypogeum.submission_gate.submit()
and hypogeum.providers.elevenlabs_voice_adapter.ElevenlabsVoiceAdapter
end-to-end, using the same mocked-HTTP discipline tests/test_elevenlabs.py
already uses -- no real network call is possible; (c) grant capacity
exhaustion fails closed with zero network calls.
"""

from __future__ import annotations

import base64
import os
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hypogeum.authorization_authority as authority
import hypogeum.authorization_store as astore
import hypogeum.channel_config as channel_config
import hypogeum.workflow_executor as workflow_executor
from hypogeum.approval_artifact import GrantBindingApproval, sign_approval_artifact
from hypogeum.config import Config
from hypogeum.elevenlabs import ElevenLabsError, generate_narration_segment

AT = "2026-01-01T00:00:00Z"
_FAKE_AUDIO_B64 = base64.b64encode(b"\xff\xfb\x90\x00" * 100).decode()


def _make_config(voice_id: str = "voice-abc") -> Config:
    env = {
        "OPENROUTER_API_KEY": "test-or-key",
        "ELEVENLABS_API_KEY": "test-xi-key",
        "ELEVENLABS_VOICE_ID": voice_id,
        "ELEVENLABS_TIMEOUT_SECONDS": "10",
    }
    with patch.dict(os.environ, env, clear=True):
        return Config()


def _mock_tts_response() -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "audio_base64": _FAKE_AUDIO_B64,
        "normalized_alignment": {
            "characters": list("hello world"),
            "character_start_times_seconds": [i * 0.1 for i in range(11)],
            "character_end_times_seconds": [i * 0.1 + 0.1 for i in range(11)],
        },
        "alignment": None,
    }
    resp.headers = {"request-id": "req-gate"}
    resp.text = ""
    return resp


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
        provider="elevenlabs", model=model, provider_operation="voice.narration_segment",
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
            allowed_bindings=(GrantBindingApproval("elevenlabs", model, "voice.narration_segment"),),
            currency="USD", max_cost_per_call=Decimal("2.50"), max_total_spend=max_total_spend,
            max_calls=max_calls, retry_authorized=False, fallback_authorized=False,
            issued_at=AT, expires_at="2099-01-01T00:00:00Z", approver_identity="operator_x", nonce="nonce_voice_gate_1",
        ),
        private_key=priv,
    )
    return authority.register_run_grant(
        grant_id="g_voice_gate_test", grant_version=1, channel_id="test_channel", episode_id="ep_gate_test",
        workflow_id="hypogeum_main_production", workflow_version="1.0.0", workflow_checksum="a" * 64,
        bindings=bindings, max_total_spend_all_bindings=max_total_spend, currency="USD",
        expires_at="2099-01-01T00:00:00Z", approval_artifact=artifact, db_path=db_path,
    )


class TestNoGrantLeavesLegacyPathUnaffected:

    def test_channel_id_supplied_but_no_grant_uses_legacy_path(self, tmp_path):
        """Even with channel_id explicitly supplied, if no grant
        resolves (the real 'hypogeum' case, always, until Enforcement
        Cutover) the call falls straight through to the unchanged
        legacy TTS call -- proven here via the same HTTP mock
        tests/test_elevenlabs.py already uses for the legacy path."""
        config = _make_config()
        with patch("hypogeum.elevenlabs.requests.post") as mock_post:
            mock_post.return_value = _mock_tts_response()
            result = generate_narration_segment(
                beat_id="b_gate_01", text="hello world", config=config,
                episode_id="ep_gate_test", output_dir=tmp_path,
                channel_id="nobody_has_a_grant_for_this_channel",
            )
        assert result.request_id == "req-gate"
        assert result.alignment_source == "tts"


class TestGrantDerivedGatePath:

    def test_gate_path_end_to_end(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        config = _make_config()
        import hypogeum.db as db
        db.initialize_schema(db_path)
        grant = _register_grant(priv, db_path, model=config.ELEVENLABS_MODEL_ID or "eleven_multilingual_v2")
        assert grant.status == "active"

        # _generate_narration_via_gate() resolves everything against the
        # PRODUCTION db_path internally (get_connection()/authority
        # calls with no db_path override, exactly as production code
        # will) -- point the production db_path at this test's isolated
        # file for the duration of this test only.
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        with patch("hypogeum.elevenlabs.requests.post") as mock_post:
            mock_post.return_value = _mock_tts_response()
            result = generate_narration_segment(
                beat_id="b_gate_01", text="hello world", config=config,
                episode_id="ep_gate_test", output_dir=tmp_path, channel_id="test_channel",
            )

        assert result.request_id == "req-gate"
        assert result.alignment_source == "tts"
        assert result.asset_id == "episodes/ep_gate_test/narration/b_gate_01"
        assert (tmp_path / "narration" / "b_gate_01.mp3").exists()

    def test_gate_path_capacity_exhausted_fails_closed_with_zero_network_calls(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        config = _make_config()
        import hypogeum.db as db
        db.initialize_schema(db_path)
        _register_grant(
            priv, db_path, model=config.ELEVENLABS_MODEL_ID or "eleven_multilingual_v2",
            max_calls=0, max_total_spend=Decimal("0.00"),
        )
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        with patch("hypogeum.elevenlabs.requests.post") as mock_post:
            with pytest.raises(ElevenLabsError, match="gate"):
                generate_narration_segment(
                    beat_id="b_gate_01", text="hello world", config=config,
                    episode_id="ep_gate_test", output_dir=tmp_path, channel_id="test_channel",
                )
            assert mock_post.called is False
