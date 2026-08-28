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


def _persist_boundary_plan(db_path, tmp_path, *, channel_id="test_channel",
                           episode_id="ep_gate_test", ceiling=25.0,
                           monkeypatch=None):
    """2026-08-26 plan-boundary rule: every paid enhancement with a
    channel_id first reserves the verified persisted Production Plan's
    weekly maximum. Tests exercising enhance(channel_id=...) therefore need
    a real channel profile (with a weekly ceiling) plus an integrity-
    verifiable persisted plan in the episode dir they hand to enhance().
    Returns the episode dir to pass as enhance(output_dir=...).

    monkeypatch (2026-08-27): when supplied, classify_plan_lifecycle()'s
    canonical episode dir is pointed at this tmp episode dir so the tmp
    plan is seen as canonical (not a misdirected path)."""
    from dataclasses import asdict
    import hypogeum.db as db
    from hypogeum.production_plan import (
        PLAN_SCHEMA_VERSION, ROLE_REQUIRED, PlannedAsset,
        ProductionPlan, payload_content_hash, persist_production_plan,
        prompt_fingerprint,
    )

    if monkeypatch is not None:
        monkeypatch.setattr(
            "hypogeum.production_plan.canonical_episode_dir",
            lambda ep, _root=tmp_path: _root / "episodes" / ep,
        )

    db.initialize_schema(db_path)
    if channel_config.get_channel_profile(channel_id, db_path=db_path) is None:
        channel_config.create_channel_profile(
            channel_id=channel_id, name="Gate Path Channel", db_path=db_path,
        )
    if channel_config.get_channel_operations_profile(channel_id, db_path=db_path) is None:
        channel_config.create_channel_operations_profile(
            channel_id=channel_id, channel_name="Gate Path Channel",
            timezone="UTC", weekly_video_budget_usd=float(ceiling),
            db_path=db_path,
        )

    episode_dir = tmp_path / "episodes" / episode_id
    episode_dir.mkdir(parents=True, exist_ok=True)
    prompt = "cinematic shot"
    asset = PlannedAsset(
        deliverable_id="main_episode", beat_id="b_001", shot_id="b_001__primary",
        asset_id=f"asset_{episode_id}_s01", scene_id="s01", short_id=None,
        role=ROLE_REQUIRED, prompt=prompt, prompt_fingerprint=prompt_fingerprint(prompt),
        shot_signature="", aspect_ratio="16:9", requested_duration_seconds=8.0,
        conservative_usable_duration_seconds=8.0, provider="muapi",
        model="veo3.1-lite-text-to-video", unit_price_usd=0.15,
        enhancement_price_usd=0.05, max_chargeable_attempts=1,
    )
    kwargs = dict(
        schema_version=PLAN_SCHEMA_VERSION,
        channel_id=channel_id,
        episode_id=episode_id,
        deliverable_contract_id="contract_v1",
        deliverable_ids=["main_episode"],
        thumbnail_main_candidate_count=3,
        thumbnail_short_candidate_cap=3,
        thumbnail_attempts_per_candidate=1,
        thumbnail_unit_price_usd=0.09,
        thumbnail_cost_normal_usd=0.36,
        thumbnail_cost_maximum_usd=1.08,
        narration_char_count=0,
        narration_price_per_char_usd=0.0,
        narration_cost_usd=0.0,
        narration_is_realized=False,
        music_section_count=0,
        music_seconds_per_section=120.0,
        music_price_per_second_usd=0.0,
        music_cost_usd=0.0,
        planning_call_estimate_usd=0.05,
        enhancement_cost_maximum_usd=0.05,
        normal_expected_cost_usd=10.00,
        maximum_bounded_cost_usd=10.00,
    )
    payload = {**kwargs, "assets": [asdict(asset)]}
    plan = ProductionPlan(
        assets=[asset], content_hash=payload_content_hash(payload),
        compiled_at="2026-08-26T00:00:00+00:00", **kwargs,
    )
    persist_production_plan(plan, episode_dir)
    return episode_dir


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


_GRANT_PLAN_HASH = "c" * 64  # grant-bound Production Plan content_hash (Phase 2)


def _register_grant(priv, db_path, *, max_calls=5, max_total_spend=Decimal("10.00")):
    bindings = [astore.GrantBinding(
        provider=PROVIDER_NAME, model=MODEL_NAME, provider_operation="upscale.video",
        currency="USD", max_cost_per_call=Decimal("0.10"), max_calls=max_calls, max_total_spend=max_total_spend,
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
        production_plan_content_hash=_GRANT_PLAN_HASH,
        bindings=bindings, max_total_spend_all_bindings=max_total_spend, currency="USD",
        expires_at="2099-01-01T00:00:00Z", approval_artifact=artifact, db_path=db_path,
    )


class TestNoGrantFailsClosed:

    def test_channel_id_supplied_but_no_grant_fails_closed_zero_network(self, tmp_path, monkeypatch):
        """2026-08-28 hardening: the post-plan no-grant direct-HTTP legacy
        path was REMOVED. A plan-level weekly reservation is financial
        capacity, never provider-execution authorization. With a channel_id
        supplied but no active grant, enhance() fails closed with zero
        network calls -- returned as a failed EnhancementResult, never
        raised (EnhancementProvider.enhance()'s no-raise contract)."""
        import hypogeum.db as db
        import requests as _requests
        db_path = tmp_path / "legacy.db"
        episode_dir = _persist_boundary_plan(
            db_path, tmp_path, channel_id="nobody_has_a_grant_for_this_channel",
            monkeypatch=monkeypatch,
        )
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        config = make_config()
        source = tmp_path / "source.mp4"
        _make_clip(source, 1280, 720)
        dest = tmp_path / "dest.mp4"

        with patch.object(_requests, "post", side_effect=AssertionError("network reached with no grant")), \
             patch.object(_requests, "get", side_effect=AssertionError("network reached with no grant")):
            provider = MuapiVideoUpscalerProvider()
            result = provider.enhance(
                source_path=source, dest_path=dest, target_width=1920, target_height=1080,
                target_resolution_label="1080p", config=config, episode_id="ep_gate_test", scene_id="s01",
                log=MagicMock(), channel_id="nobody_has_a_grant_for_this_channel",
                output_dir=episode_dir,
            )

        assert isinstance(result, EnhancementResult)
        assert result.success is False
        assert "no_active_grant" in result.failure_reason

    def test_pre_plan_real_upscale_fails_closed(self, tmp_path, monkeypatch):
        """No compiled Production Plan at the canonical episode dir ->
        PRE_PLAN -> fail closed before any reservation or network. There
        is no authorized pre-plan real-network upscale path."""
        import hypogeum.db as db
        import requests as _requests
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", tmp_path / "empty.db")
        monkeypatch.setattr(
            "hypogeum.production_plan.canonical_episode_dir",
            lambda ep: tmp_path / "episodes" / ep,
        )
        config = make_config()
        source = tmp_path / "source.mp4"
        _make_clip(source, 1280, 720)
        with patch.object(_requests, "post", side_effect=AssertionError("network reached pre-plan")), \
             patch.object(_requests, "get", side_effect=AssertionError("network reached pre-plan")):
            result = MuapiVideoUpscalerProvider().enhance(
                source_path=source, dest_path=tmp_path / "dest.mp4", target_width=1920, target_height=1080,
                target_resolution_label="1080p", config=config, episode_id="ep_no_plan", scene_id="s01",
                log=MagicMock(), channel_id="test_channel",
            )
        assert result.success is False
        assert "pre_plan_unauthorized" in result.failure_reason


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

        # Plan-level weekly reservation (2026-08-26 boundary rule): the
        # reservation precedes the gate path too.
        episode_dir = _persist_boundary_plan(db_path, tmp_path, monkeypatch=monkeypatch)

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

        import requests as _requests
        with patch.object(_requests, "post", side_effect=[upload_resp, submit_resp]), \
             patch.object(_requests, "get", side_effect=[poll_resp, download_resp]), \
             patch("hypogeum.finance.record_cost"):
            provider = MuapiVideoUpscalerProvider()
            result = provider.enhance(
                source_path=source, dest_path=dest, target_width=1920, target_height=1080,
                target_resolution_label="1080p", config=config, episode_id="ep_gate_test", scene_id="s01",
                log=MagicMock(), channel_id="test_channel", output_dir=episode_dir,
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
        import requests as _requests
        db.initialize_schema(db_path)
        _register_grant(priv, db_path, max_calls=0, max_total_spend=Decimal("0.00"))
        episode_dir = _persist_boundary_plan(db_path, tmp_path, monkeypatch=monkeypatch)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        with patch.object(_requests, "post") as mock_post, \
             patch.object(_requests, "get") as mock_get:
            provider = MuapiVideoUpscalerProvider()
            result = provider.enhance(
                source_path=source, dest_path=dest, target_width=1920, target_height=1080,
                target_resolution_label="1080p", config=config, episode_id="ep_gate_test", scene_id="s01",
                log=MagicMock(), channel_id="test_channel", output_dir=episode_dir,
            )
            assert mock_post.called is False
            assert mock_get.called is False

        # EnhancementProvider.enhance()'s own contract forbids raising for
        # provider-side/gate-side failures -- a denial surfaces as a
        # failed EnhancementResult, never an exception.
        assert result.success is False
        assert "gate" in result.failure_reason
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
        config = make_config()
        import hypogeum.db as db
        db.initialize_schema(db_path)
        insert_historical_grant(
            priv, db_path, grant_id="g_legacy_upscale",
            channel_id="test_channel", episode_id="ep_gate_test",
            provider=PROVIDER_NAME, model=MODEL_NAME, provider_operation="upscale.video",
            max_cost_per_call="0.10",
        )
        episode_dir = _persist_boundary_plan(db_path, tmp_path, monkeypatch=monkeypatch)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        class _GateProbe(Exception):
            pass

        class _LegacyProbe(Exception):
            pass

        def _gate_probe(*args, **kwargs):
            raise _GateProbe()

        monkeypatch.setattr(MuapiVideoUpscalerProvider, "_enhance_via_gate", _gate_probe)
        source = tmp_path / "source_legacy.mp4"
        _make_clip(source, 1280, 720)
        import requests as _requests
        with patch.object(_requests, "post", side_effect=_LegacyProbe()), \
             patch.object(_requests, "get", side_effect=_LegacyProbe()), \
             patch("hypogeum.finance.record_cost"):
            with pytest.raises(_GateProbe):
                MuapiVideoUpscalerProvider().enhance(
                    source_path=source, dest_path=tmp_path / "dest_legacy.mp4",
                    target_width=1920, target_height=1080,
                    target_resolution_label="1080p", config=config,
                    episode_id="ep_gate_test", scene_id="s01",
                    log=MagicMock(), channel_id="test_channel",
                    output_dir=episode_dir,
                )
