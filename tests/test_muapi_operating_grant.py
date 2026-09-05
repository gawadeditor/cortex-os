"""
tests/test_muapi_operating_grant.py — the MUAPI provider gates resolve a
Channel Operating Grant (ADR-016) for an ADMITTED autonomous episode, not
only a per-episode 'run' grant.

hypogeum.muapi._resolve_authorization_grant() is the one shared resolution
helper the 3 muapi gate sites (submit_scene / _generate_reference_image /
_generate_thumbnail_with_model) call. Its contract mirrors
hypogeum.openrouter's own resolution order exactly:

  - episode ADMITTED under an operating grant (get_admission_binding not
    None) -> resolve the operating grant; NO paid legacy fallback;
  - otherwise                                   -> resolve the per-episode
    'run' grant (unchanged prior behaviour).

Zero network. Temp DB, injected clock, test keypair.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import hypogeum.authorization_authority as authority
import hypogeum.authorization_store as store
import hypogeum.channel_config as channel_config
import hypogeum.db as db
import hypogeum.muapi as muapi
import hypogeum.runtime_admission as adm
from hypogeum.approval_artifact import GrantBindingApproval, sign_approval_artifact

_CH = "opgrant_chan"
_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
_AT = _NOW.isoformat()


def _cfg(**over):
    base = dict(FINANCE_BUDGET_CEILING_USD=80.0, MUAPI_DEFAULT_TIER=2,
                MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE=3)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "opg.db"
    db.initialize_schema(db_path)
    monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(authority, "_load_trusted_public_key", lambda: priv.public_key())
    channel_config.create_channel_profile(channel_id=_CH, name="OpGrant", db_path=db_path)
    channel_config.create_channel_operations_profile(
        channel_id=_CH, channel_name="OpGrant", timezone="UTC", operator_enabled=True,
        production_cadence={"videos_per_week": 0, "shorts_per_week": 7},
        weekly_video_budget_usd=80.0, db_path=db_path,
    )
    return SimpleNamespace(db_path=db_path, priv=priv)


def _op_bindings():
    return [store.GrantBinding(
        provider="muapi", model="veo3.1-lite-text-to-video",
        provider_operation="video.text_to_video", currency="USD",
        max_cost_per_call=Decimal("2.50"), max_calls=2000, max_total_spend=Decimal("400.00"),
    )]


def _sign_operating_grant(priv, db_path, *, grant_id="op_g", weekly_ceiling=Decimal("80.00"),
                          domains=("muapi_video",), workflow_version="1.0.0"):
    bindings = _op_bindings()
    _total = authority.operating_grant_total_capacity(
        weekly_ceiling, "2026-08-01T00:00:00Z", "2099-01-01T00:00:00Z",
    )
    checksum = authority.compute_grant_checksum(
        channel_id=_CH, episode_id="*", workflow_id="hypogeum_main_production",
        workflow_version=workflow_version, workflow_checksum="a" * 64,
        production_plan_content_hash=None, bindings=bindings,
        max_total_spend_all_bindings=_total, currency="USD",
        expires_at="2099-01-01T00:00:00Z", grant_kind="channel_operating",
        channel_package_version="1.0", channel_package_checksum="b" * 64,
        allowed_provider_domains=list(domains), cadence_snapshot="",
        weekly_exposure_ceiling_usd=weekly_ceiling, valid_from="2026-08-01T00:00:00Z",
        operating_period_ref=f"{_CH}:2026",
    )
    artifact = sign_approval_artifact(dict(
        approval_id=f"appr_{grant_id}", approval_version=1,
        approval_type="channel_operating_grant", subject_checksum=checksum,
        channel_id=_CH, scope_id="*", workflow_id="hypogeum_main_production",
        workflow_version=workflow_version, workflow_checksum="a" * 64,
        channel_package_version="1.0", channel_package_checksum="b" * 64,
        allowed_bindings=(GrantBindingApproval("muapi", "veo3.1-lite-text-to-video", "video.text_to_video"),),
        currency="USD", max_cost_per_call=Decimal("2.50"), max_total_spend=_total,
        max_calls=2000, retry_authorized=True, fallback_authorized=True,
        issued_at="2026-08-01T00:00:00Z", expires_at="2099-01-01T00:00:00Z",
        approver_identity="operator", nonce=f"n_{grant_id}",
    ), private_key=priv)
    return authority.register_channel_operating_grant(
        grant_id=grant_id, grant_version=1, channel_id=_CH,
        workflow_id="hypogeum_main_production", workflow_version=workflow_version,
        workflow_checksum="a" * 64, channel_package_version="1.0", channel_package_checksum="b" * 64,
        allowed_provider_domains=list(domains), cadence_snapshot="",
        weekly_exposure_ceiling_usd=weekly_ceiling, bindings=bindings, currency="USD",
        valid_from="2026-08-01T00:00:00Z", expires_at="2099-01-01T00:00:00Z",
        operating_period_ref=f"{_CH}:2026", approval_artifact=artifact, db_path=db_path,
    )


class TestResolveAuthorizationGrant:
    def test_admitted_episode_resolves_operating_grant(self, env):
        _sign_operating_grant(env.priv, env.db_path)
        adm.bind_episode_admission(_CH, "ep_op_1", config=_cfg(), worker_id="w1",
                                   now=_NOW, db_path=env.db_path)
        grant, admitted = muapi._resolve_authorization_grant(_CH, "ep_op_1", at=_AT)
        assert admitted is True
        assert grant is not None
        assert getattr(grant, "grant_kind", "run") == "channel_operating"
        assert grant.grant_id == "op_g"

    def test_admitted_but_operating_grant_gone_returns_none_admitted_true(self, env):
        g = _sign_operating_grant(env.priv, env.db_path)
        adm.bind_episode_admission(_CH, "ep_op_2", config=_cfg(), worker_id="w1",
                                   now=_NOW, db_path=env.db_path)
        authority.revoke_grant(g.grant_id, g.grant_version, revoked_by="t", reason="t",
                               at=_AT, db_path=env.db_path)
        grant, admitted = muapi._resolve_authorization_grant(_CH, "ep_op_2", at=_AT)
        assert admitted is True
        assert grant is None  # caller MUST fail closed -- no legacy path

    def test_not_admitted_falls_back_to_episode_run_grant(self, env):
        # no admission row, no operating grant, no run grant
        grant, admitted = muapi._resolve_authorization_grant(_CH, "ep_op_3", at=_AT)
        assert admitted is False
        assert grant is None  # resolve_active_grant found nothing -- legacy check applies

    def test_missing_admission_table_is_not_admitted(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty.db"
        db.initialize_schema(empty)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", empty)
        grant, admitted = muapi._resolve_authorization_grant("x", "ep", at=_AT)
        assert admitted is False


class TestSubmitSceneGateResolution:
    def test_admitted_no_operating_grant_fails_closed_named_reason(self, env, tmp_path, monkeypatch):
        _sign_operating_grant(env.priv, env.db_path)
        adm.bind_episode_admission(_CH, "ep_ss", config=_cfg(), worker_id="w1",
                                   now=_NOW, db_path=env.db_path)
        # kill the operating grant so it no longer resolves
        conn = db.get_connection(env.db_path)
        try:
            conn.execute("UPDATE run_authorization_grants SET status='revoked' WHERE grant_id='op_g'")
            conn.commit()
        finally:
            conn.close()
        spec = {"scene_id": "s01", "visual_spec": {"aspect_ratio": "9:16", "duration_seconds": 8.0},
                "production_spec": {"primary_prompt": "SCENE: a stone corridor",
                                    "fallback_prompt": "SCENE: a stone corridor"}}
        res = muapi.submit_scene(spec, _cfg(MUAPI_DRY_RUN=False, MUAPI_CHEAP_MODE=False,
                                            MUAPI_RUNWAY_ONLY_MODE=False),
                                 "ep_ss", tmp_path, channel_id=_CH)
        assert res["generation_status"] == "generation_failed"
        assert "admitted_autonomous_execution_without_active_operating_grant" in res["failure_reason"]
