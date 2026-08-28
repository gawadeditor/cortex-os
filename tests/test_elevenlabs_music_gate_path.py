"""
tests/test_elevenlabs_music_gate_path.py — Integration tests for the new,
grant-derived gate path in hypogeum/elevenlabs.py::generate_music_track()
(plan §5.5/§6 Commit G).

Mirrors tests/test_elevenlabs_voice_gate_path.py's exact structure and
coverage: (a) supplying channel_id with no real grant leaves the legacy
path completely unaffected; (b) with a synthetic, verified grant
registered, generate_music_track() routes through
hypogeum.submission_gate.submit() and
hypogeum.providers.elevenlabs_music_adapter.ElevenlabsMusicAdapter
end-to-end; (c) grant capacity exhaustion fails closed with zero network
calls. A fresh tmp_path has no music-library.json, so every test here is
naturally a cache miss -- the gate path is only ever reached past the
library-search step, exactly as generate_music_track()'s own branch
requires.
"""

from __future__ import annotations

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
from hypogeum.elevenlabs import ElevenLabsError, generate_music_track

AT = "2026-01-01T00:00:00Z"


def _make_config() -> Config:
    env = {
        "OPENROUTER_API_KEY": "test-or-key",
        "ELEVENLABS_API_KEY": "test-xi-key",
        "ELEVENLABS_VOICE_ID": "voice-abc",
        "ELEVENLABS_TIMEOUT_SECONDS": "10",
    }
    with patch.dict(os.environ, env, clear=True):
        return Config()


def _mock_music_response(audio_bytes: bytes = b"\xff\xfb" * 5000, status: int = 200) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.content = audio_bytes
    resp.text = ""
    resp.headers = {}
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


_GRANT_PLAN_HASH = "c" * 64  # grant-bound Production Plan content_hash (Phase 2)


def _register_grant(priv, db_path, *, model, max_calls=5, max_total_spend=Decimal("10.00")):
    bindings = [astore.GrantBinding(
        provider="elevenlabs", model=model, provider_operation="music.track",
        currency="USD", max_cost_per_call=Decimal("2.50"), max_calls=max_calls, max_total_spend=max_total_spend,
    )]
    checksum = authority.compute_grant_checksum(
        channel_id="test_channel", episode_id="ep_gate_test", workflow_id="hypogeum_main_production",
        workflow_version="1.0.0", workflow_checksum="a" * 64,
        production_plan_content_hash=_GRANT_PLAN_HASH, bindings=bindings,
        max_total_spend_all_bindings=max_total_spend, currency="USD", expires_at="2099-01-01T00:00:00Z",
    )
    artifact = sign_approval_artifact(
        dict(
            approval_id="appr1", approval_version=1, approval_type="grant", subject_checksum=checksum,
            channel_id="test_channel", scope_id="ep_gate_test", workflow_id="hypogeum_main_production",
            workflow_version="1.0.0", workflow_checksum="a" * 64, channel_package_version="1.0",
            channel_package_checksum="b" * 64,
            allowed_bindings=(GrantBindingApproval("elevenlabs", model, "music.track"),),
            currency="USD", max_cost_per_call=Decimal("2.50"), max_total_spend=max_total_spend,
            max_calls=max_calls, retry_authorized=False, fallback_authorized=False,
            issued_at=AT, expires_at="2099-01-01T00:00:00Z", approver_identity="operator_x", nonce="nonce_music_gate_1",
        ),
        private_key=priv,
    )
    return authority.register_run_grant(
        grant_id="g_music_gate_test", grant_version=1, channel_id="test_channel", episode_id="ep_gate_test",
        workflow_id="hypogeum_main_production", workflow_version="1.0.0", workflow_checksum="a" * 64,
        production_plan_content_hash=_GRANT_PLAN_HASH,
        bindings=bindings, max_total_spend_all_bindings=max_total_spend, currency="USD",
        expires_at="2099-01-01T00:00:00Z", approval_artifact=artifact, db_path=db_path,
    )


class TestNoGrantLeavesLegacyPathUnaffected:

    def test_channel_id_supplied_but_no_grant_uses_legacy_path(self, tmp_path, monkeypatch):
        """Even with channel_id explicitly supplied, if no grant
        resolves (the real 'hypogeum' case, always, until Enforcement
        Cutover) the call falls straight through to the unchanged
        legacy /v1/music call -- proven here via the same HTTP mock
        tests/test_elevenlabs.py already uses for the legacy path. The
        plan-level weekly reservation (2026-08-26 boundary rule) is
        established first against this test's own isolated DB and
        persisted plan."""
        import hypogeum.db as db
        from test_muapi_upscale_gate_path import _persist_boundary_plan

        db_path = tmp_path / "legacy.db"
        episode_dir = _persist_boundary_plan(
            db_path, tmp_path, channel_id="nobody_has_a_grant_for_this_channel",
            monkeypatch=monkeypatch,
        )
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        config = _make_config()
        with patch("hypogeum.elevenlabs.requests.post") as mock_post, \
             patch("hypogeum.elevenlabs._finance.record_cost"):
            mock_post.return_value = _mock_music_response()
            result = generate_music_track(
                category="Mystery", required_duration_seconds=30.0, config=config,
                episode_id="ep_gate_test", assets_root=tmp_path,
                channel_id="nobody_has_a_grant_for_this_channel",
            )
        assert result.source == "ai_generated"
        assert result.category == "Mystery"


class TestGrantDerivedGatePath:

    def test_gate_path_end_to_end(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        config = _make_config()
        import hypogeum.db as db
        db.initialize_schema(db_path)
        grant = _register_grant(priv, db_path, model=config.ELEVENLABS_MUSIC_MODEL_ID or "music_v1")
        assert grant.status == "active"

        # _generate_music_via_gate() resolves everything against the
        # PRODUCTION db_path internally (get_connection()/authority
        # calls with no db_path override, exactly as production code
        # will) -- point the production db_path at this test's isolated
        # file for the duration of this test only.
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        with patch("hypogeum.elevenlabs.requests.post") as mock_post, \
             patch("hypogeum.elevenlabs._finance.record_cost"):
            mock_post.return_value = _mock_music_response()
            result = generate_music_track(
                category="Mystery", required_duration_seconds=30.0, config=config,
                episode_id="ep_gate_test", assets_root=tmp_path, channel_id="test_channel",
            )

        assert result.source == "ai_generated"
        assert result.category == "Mystery"
        assert result.duration_seconds == 30.0
        assert result.local_path.exists()
        library_path = tmp_path / "channel" / "music" / "music-library.json"
        assert library_path.exists()

    def test_gate_path_capacity_exhausted_fails_closed_with_zero_network_calls(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        config = _make_config()
        import hypogeum.db as db
        db.initialize_schema(db_path)
        _register_grant(
            priv, db_path, model=config.ELEVENLABS_MUSIC_MODEL_ID or "music_v1",
            max_calls=0, max_total_spend=Decimal("0.00"),
        )
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        with patch("hypogeum.elevenlabs.requests.post") as mock_post:
            with pytest.raises(ElevenLabsError, match="gate"):
                generate_music_track(
                    category="Mystery", required_duration_seconds=30.0, config=config,
                    episode_id="ep_gate_test", assets_root=tmp_path, channel_id="test_channel",
                )
            assert mock_post.called is False
class TestLegacyGrantStillRoutesThroughGate:
    """Phase 2 correction: a PRE-PHASE-2 historical grant (checksum
    computed before production_plan_content_hash existed; NULL column)
    still resolves after migration and must keep routing through THIS
    domain's gate -- never silently falling back to the ungated legacy
    provider call."""

    def test_legacy_null_hash_grant_routes_through_gate(self, keypair, tmp_path, monkeypatch):
        from test_phase2_plan_bound_authorization import insert_historical_grant

        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        config = _make_config()
        import hypogeum.db as db
        db.initialize_schema(db_path)
        insert_historical_grant(
            priv, db_path, grant_id="g_legacy_music",
            channel_id="test_channel", episode_id="ep_gate_test",
            provider="elevenlabs", model=config.ELEVENLABS_MUSIC_MODEL_ID or "music_v1", provider_operation="music.track",
            max_cost_per_call="2.50",
        )
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        import hypogeum.elevenlabs as domain_module

        class _GateProbe(Exception):
            pass

        class _LegacyProbe(Exception):
            pass

        def _gate_probe(*args, **kwargs):
            raise _GateProbe()

        monkeypatch.setattr(domain_module, "_generate_music_via_gate", _gate_probe)
        with patch("hypogeum.elevenlabs.requests.post", side_effect=_LegacyProbe()):
            with pytest.raises(_GateProbe):
                generate_music_track(
                    category="Mystery", required_duration_seconds=30.0, config=config,
                    episode_id="ep_gate_test", assets_root=tmp_path,
                    channel_id="test_channel",
                )
