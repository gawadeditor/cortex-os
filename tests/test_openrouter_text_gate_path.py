"""
tests/test_openrouter_text_gate_path.py — Integration tests for the new,
grant-derived gate path in hypogeum/openrouter.py::call_claude()
(plan §5.5/§6 Commit H).

Mirrors tests/test_elevenlabs_voice_gate_path.py's exact structure and
coverage: (a) supplying channel_id with no real grant leaves the legacy
path completely unaffected; (b) with a synthetic, verified grant
registered, call_claude() routes through hypogeum.submission_gate.submit()
and hypogeum.providers.openrouter_text_adapter.OpenrouterTextAdapter
end-to-end; (c) grant capacity exhaustion fails closed with zero network
calls; (d) a retryable HTTP failure surfaces the exact same _RetryableError
classification the legacy path would, proving the adapter's exception
reconstruction (see that module's own docstring) is faithful.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_openrouter import SONNET, MINIMAL_MESSAGES, make_config, openrouter_200, http_error  # noqa: E402

import hypogeum.adapter_registry as adapter_registry
import hypogeum.authorization_authority as authority
import hypogeum.authorization_store as astore
import hypogeum.channel_config as channel_config
import hypogeum.openrouter as openrouter_module
import hypogeum.workflow_executor as workflow_executor
from hypogeum.approval_artifact import GrantBindingApproval, sign_approval_artifact
from hypogeum.openrouter import CallResult, FallbackFailed, call_claude, reset_run_state

AT = "2026-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def _reset_state():
    reset_run_state()
    adapter_registry.reset_adapter_instance_cache()
    yield
    reset_run_state()
    adapter_registry.reset_adapter_instance_cache()


@pytest.fixture(autouse=True)
def _isolated_assets_root(monkeypatch, tmp_path):
    """_call_openrouter_via_gate() writes its write-ahead receipts under
    the module-level _ASSETS_ROOT constant (mirrors the exact production
    default every other domain's real output_dir/assets_root already
    resolves to -- .hypogeum-assets/episodes/{episode_id}/...). Redirect
    it to this test's own tmp_path, exactly like the db._DEFAULT_DB_PATH
    monkeypatch convention used elsewhere in this suite, so repeated
    test runs (and different tests reusing the same episode_id) never
    collide with real on-disk receipts or with each other."""
    monkeypatch.setattr(openrouter_module, "_ASSETS_ROOT", tmp_path)


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


def _register_grant(priv, db_path, *, model=SONNET, max_calls=5, max_total_spend=Decimal("10.00")):
    bindings = [astore.GrantBinding(
        provider="openrouter", model=model, provider_operation="text.completion",
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
            allowed_bindings=(GrantBindingApproval("openrouter", model, "text.completion"),),
            currency="USD", max_cost_per_call=Decimal("2.50"), max_total_spend=max_total_spend,
            max_calls=max_calls, retry_authorized=False, fallback_authorized=False,
            issued_at=AT, expires_at="2099-01-01T00:00:00Z", approver_identity="operator_x", nonce="nonce_text_gate_1",
        ),
        private_key=priv,
    )
    return authority.register_run_grant(
        grant_id="g_text_gate_test", grant_version=1, channel_id="test_channel", episode_id="ep_gate_test",
        workflow_id="hypogeum_main_production", workflow_version="1.0.0", workflow_checksum="a" * 64,
        production_plan_content_hash=_GRANT_PLAN_HASH,
        bindings=bindings, max_total_spend_all_bindings=max_total_spend, currency="USD",
        expires_at="2099-01-01T00:00:00Z", approval_artifact=artifact, db_path=db_path,
    )



@pytest.fixture(autouse=True)
def _preplan_env_iso(monkeypatch, tmp_path):
    """PRE_PLAN paid provider calls first establish the ONE pre-production
    CHANNEL_WEEKLY hold. Give this channel a resolvable ceiling + policy
    and isolate the ledger DB (provider modules call get_connection() with
    db_path=None)."""
    from decimal import Decimal as _D
    import hypogeum.db as _db
    import hypogeum.policy_cascade as _pc
    import hypogeum.weekly_budget as _wb
    _iso = tmp_path / "_ledger_iso.db"
    _db.initialize_schema(_iso)
    monkeypatch.setattr(_db, "_DEFAULT_DB_PATH", _iso)
    monkeypatch.setattr(_pc, "get_effective_budget_ceiling", lambda *a, **k: 55.0)
    monkeypatch.setattr(_wb, "resolve_channel_weekly_policy", lambda conn, cid: ("UTC", _D("55.00")))

class TestNoGrantLeavesLegacyPathUnaffected:

    def test_channel_id_supplied_but_no_grant_uses_legacy_path(self):
        """Even with channel_id explicitly supplied, if no grant
        resolves (the real 'hypogeum' case, always, until Enforcement
        Cutover) the call falls straight through to the unchanged
        legacy OpenRouter dispatch -- proven here via the same HTTP
        mock tests/test_openrouter.py already uses for the legacy path."""
        cfg = make_config()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200("legacy path")):
            result = call_claude(
                SONNET, MINIMAL_MESSAGES, 512, cfg, episode_id="ep_gate_test",
                channel_id="nobody_has_a_grant_for_this_channel",
            )
        assert isinstance(result, CallResult)
        assert result.content == "legacy path"


class TestGrantDerivedGatePath:

    def test_gate_path_end_to_end(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        cfg = make_config()
        import hypogeum.db as db
        db.initialize_schema(db_path)
        grant = _register_grant(priv, db_path)
        assert grant.status == "active"

        # _call_openrouter_via_gate() resolves everything against the
        # PRODUCTION db_path internally (get_connection()/authority
        # calls with no db_path override, exactly as production code
        # will) -- point the production db_path at this test's isolated
        # file for the duration of this test only.
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200("gate path")):
            result = call_claude(
                SONNET, MINIMAL_MESSAGES, 512, cfg, episode_id="ep_gate_test", channel_id="test_channel",
            )

        assert isinstance(result, CallResult)
        assert result.content == "gate path"
        assert result.model_used == SONNET
        assert result.usage["total_tokens"] == 60

    def test_gate_path_capacity_exhausted_fails_closed_with_zero_network_calls(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        cfg = make_config()
        import hypogeum.db as db
        db.initialize_schema(db_path)
        _register_grant(priv, db_path, max_calls=0, max_total_spend=Decimal("0.00"))
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        with patch("hypogeum.openrouter.requests.post") as mock_post:
            with pytest.raises(FallbackFailed, match="gate"):
                call_claude(
                    SONNET, MINIMAL_MESSAGES, 512, cfg, episode_id="ep_gate_test", channel_id="test_channel",
                )
            assert mock_post.called is False

    def test_gate_path_retryable_failure_reclassified_and_exhausts_then_fallback_fails(self, keypair, tmp_path, monkeypatch):
        """A 500 on every attempt (primary retries, then the one-shot
        different-model fallback -- config.OPENROUTER_FALLBACK_MODEL
        defaults to the same Sonnet slug, matching production default
        behavior, so the fallback attempt is also gated and also fails)
        must surface as FallbackFailed -- the adapter's _RetryableError
        reconstruction (see openrouter_text_adapter.py's docstring)
        drives the SAME outer retry+fallback loop the legacy path uses,
        so retry_delay/time.sleep and the fallback attempt are both
        exercised identically, each as its own gated envelope/
        authorization/submission (a distinct attempt_label/capability
        per attempt -- proven by this test running to completion
        without a spurious CapabilityAlreadyConsumedError)."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        cfg = make_config()
        import hypogeum.db as db
        db.initialize_schema(db_path)
        _register_grant(priv, db_path, max_calls=5, max_total_spend=Decimal("10.00"))
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        with patch("hypogeum.openrouter.requests.post", return_value=http_error(500)), \
             patch("hypogeum.openrouter.time.sleep"):
            with pytest.raises(FallbackFailed, match="fallback model .* also failed"):
                call_claude(
                    SONNET, MINIMAL_MESSAGES, 512, cfg, episode_id="ep_gate_test", channel_id="test_channel",
                )
    def test_failed_call_releases_lease_so_retry_is_not_locked_out(self, keypair, tmp_path, monkeypatch):
        """2026-10-03: an OpenRouter 402 left the text gate's runtime_lease
        active for its 30-min TTL, so the next two runs of the same episode
        failed instantly with "Text gate lease unavailable" (identical call
        -> identical lease owner) and tripped the spend breaker. The gate
        call must release the lease however it ends."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        cfg = make_config()
        import hypogeum.db as db
        from hypogeum.openrouter import CreditsExhausted
        db.initialize_schema(db_path)
        _register_grant(priv, db_path, max_calls=5, max_total_spend=Decimal("10.00"))
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        with patch("hypogeum.openrouter.requests.post", return_value=http_error(402)), \
             patch("hypogeum.openrouter.time.sleep"):
            with pytest.raises(Exception) as first:
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg, episode_id="ep_gate_test", channel_id="test_channel")
        assert "lease unavailable" not in str(first.value)

        conn = db.get_connection(db_path)
        try:
            leases = conn.execute(
                "SELECT status FROM runtime_leases WHERE owner_id LIKE 'openrouter_text_adapter:%'").fetchall()
        finally:
            conn.close()
        assert leases and all(r["status"] == "released" for r in leases), [dict(r) for r in leases]

        reset_run_state()
        with patch("hypogeum.openrouter.requests.post", return_value=openrouter_200("after top-up")):
            try:
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg, episode_id="ep_gate_test", channel_id="test_channel")
            except Exception as exc:  # noqa: BLE001
                assert "lease unavailable" not in str(exc), exc


class TestWorkspaceKeyIsolation:
    """Multi-tenant Phase 2: the key that goes out on the wire is the key of
    the workspace owning the channel of the VERIFIED envelope — a client
    channel never spends the operator's balance."""

    def _setup(self, priv, tmp_path, monkeypatch):
        import hypogeum.db as db
        from hypogeum import workspaces as ws
        from hypogeum.factory_entity import create_factory
        db_path = tmp_path / "auth.db"
        db.initialize_schema(db_path)
        _register_grant(priv, db_path)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        ws._READY.discard(str(db_path))
        create_factory("operator", "Operator", "operator", db_path=db_path)
        ws.backfill_platform_workspace(db_path)
        ws.create_workspace("Acme", workspace_id="acme", db_path=db_path)
        return db_path

    def _auth_headers(self, cfg, monkeypatch):
        seen = []

        def _post(url, *a, **kw):
            seen.append((kw.get("headers") or {}).get("Authorization"))
            return openrouter_200("ok")
        with patch("hypogeum.openrouter.requests.post", side_effect=_post), \
             patch("hypogeum.openrouter.time.sleep"):
            try:
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg, episode_id="ep_gate_test", channel_id="test_channel")
            except Exception:  # noqa: BLE001 — a missing key may fail the call; we only inspect the wire
                pass
        return seen

    def test_client_channel_uses_client_key_never_operator_key(self, keypair, tmp_path, monkeypatch):
        from hypogeum import workspaces as ws
        from hypogeum.credential_manager import create_secret_profile
        priv, _ = keypair
        db_path = self._setup(priv, tmp_path, monkeypatch)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-OPERATOR")
        ws.assign("channel", "test_channel", "acme", db_path=db_path)
        create_secret_profile("openrouter", "acme", "api_key", "sk-or-ACME", workspace_id="acme", db_path=db_path)

        seen = self._auth_headers(make_config({"OPENROUTER_API_KEY": "sk-or-OPERATOR"}), monkeypatch)
        assert seen and all(h == "Bearer sk-or-ACME" for h in seen), seen

    def test_client_without_key_never_falls_back_to_operator(self, keypair, tmp_path, monkeypatch):
        from hypogeum import workspaces as ws
        priv, _ = keypair
        db_path = self._setup(priv, tmp_path, monkeypatch)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-OPERATOR")
        ws.assign("channel", "test_channel", "acme", db_path=db_path)

        seen = self._auth_headers(make_config({"OPENROUTER_API_KEY": "sk-or-OPERATOR"}), monkeypatch)
        assert not any(h and "OPERATOR" in h for h in seen), seen

    def test_operator_channel_keeps_env_key(self, keypair, tmp_path, monkeypatch):
        from hypogeum import workspaces as ws
        priv, _ = keypair
        db_path = self._setup(priv, tmp_path, monkeypatch)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-OPERATOR")
        ws.assign("channel", "test_channel", "operator", db_path=db_path)

        seen = self._auth_headers(make_config({"OPENROUTER_API_KEY": "sk-or-OPERATOR"}), monkeypatch)
        assert seen and all(h == "Bearer sk-or-OPERATOR" for h in seen), seen


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
            priv, db_path, grant_id="g_legacy_text",
            channel_id="test_channel", episode_id="ep_gate_test",
            provider="openrouter", model=SONNET, provider_operation="text.completion",
            max_cost_per_call="2.50",
        )
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
        import hypogeum.openrouter as domain_module

        class _GateProbe(Exception):
            pass

        class _LegacyProbe(Exception):
            pass

        def _gate_probe(*args, **kwargs):
            raise _GateProbe()

        monkeypatch.setattr(domain_module, "_call_claude_via_gate", _gate_probe)
        with patch("hypogeum.openrouter.requests.post", side_effect=_LegacyProbe()):
            with pytest.raises(_GateProbe):
                call_claude(
                    SONNET, MINIMAL_MESSAGES, 512, config,
                    episode_id="ep_gate_test", channel_id="test_channel",
                )
