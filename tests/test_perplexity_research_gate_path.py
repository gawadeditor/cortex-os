"""
tests/test_perplexity_research_gate_path.py — Integration tests for the
new, grant-derived gate path in hypogeum/perplexity.py::query_perplexity()
(plan §5.5/§6 Commit I).

Mirrors tests/test_openrouter_text_gate_path.py's exact structure and
coverage: (a) supplying channel_id with no real grant leaves the legacy
path completely unaffected; (b) with a synthetic, verified grant
registered, query_perplexity() routes through hypogeum.submission_gate.
submit() and hypogeum.providers.perplexity_adapter.PerplexityAdapter
end-to-end; (c) grant capacity exhaustion fails closed with zero network
calls; (d) a retryable soft failure (empty choices) surfaces the exact
same PerplexitySoftFailureExhausted after retry exhaustion the legacy
path would, proving the adapter's tuple-shape reconstruction (see that
module's own docstring) is faithful.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_perplexity import make_config, perplexity_200, http_error  # noqa: E402

import hypogeum.adapter_registry as adapter_registry
import hypogeum.authorization_authority as authority
import hypogeum.authorization_store as astore
import hypogeum.channel_config as channel_config
import hypogeum.perplexity as perplexity_module
import hypogeum.workflow_executor as workflow_executor
from hypogeum.approval_artifact import GrantBindingApproval, sign_approval_artifact
from hypogeum.perplexity import PerplexityResult, PerplexitySoftFailureExhausted, query_perplexity

AT = "2026-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def _reset_adapter_cache():
    adapter_registry.reset_adapter_instance_cache()
    yield
    adapter_registry.reset_adapter_instance_cache()


@pytest.fixture(autouse=True)
def _isolated_assets_root(monkeypatch, tmp_path):
    """_single_request_via_gate() writes its write-ahead receipts under
    the module-level _ASSETS_ROOT constant (mirrors the exact production
    default every other domain's real output_dir/assets_root already
    resolves to). Redirect it to this test's own tmp_path, exactly like
    the db._DEFAULT_DB_PATH monkeypatch convention used elsewhere, so
    different tests reusing the same episode_id never collide with real
    on-disk receipts or with each other."""
    monkeypatch.setattr(perplexity_module, "_ASSETS_ROOT", tmp_path)


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


def _register_grant(priv, db_path, *, model="sonar-pro", max_calls=5, max_total_spend=Decimal("10.00")):
    bindings = [astore.GrantBinding(
        provider="perplexity", model=model, provider_operation="research.query",
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
            allowed_bindings=(GrantBindingApproval("perplexity", model, "research.query"),),
            currency="USD", max_cost_per_call=Decimal("2.50"), max_total_spend=max_total_spend,
            max_calls=max_calls, retry_authorized=False, fallback_authorized=False,
            issued_at=AT, expires_at="2099-01-01T00:00:00Z", approver_identity="operator_x", nonce="nonce_research_gate_1",
        ),
        private_key=priv,
    )
    return authority.register_run_grant(
        grant_id="g_research_gate_test", grant_version=1, channel_id="test_channel", episode_id="ep_gate_test",
        workflow_id="hypogeum_main_production", workflow_version="1.0.0", workflow_checksum="a" * 64,
        bindings=bindings, max_total_spend_all_bindings=max_total_spend, currency="USD",
        expires_at="2099-01-01T00:00:00Z", approval_artifact=artifact, db_path=db_path,
    )


class TestNoGrantLeavesLegacyPathUnaffected:

    def test_channel_id_supplied_but_no_grant_uses_legacy_path(self):
        """Even with channel_id explicitly supplied, if no grant
        resolves (the real 'hypogeum' case, always, until Enforcement
        Cutover) the call falls straight through to the unchanged
        legacy Perplexity dispatch -- proven here via the same HTTP
        mock tests/test_perplexity.py already uses for the legacy path."""
        cfg = make_config()
        with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200("legacy path")):
            result = query_perplexity(
                "what happened", cfg, phase_id="phase_1", query_index=0, episode_id="ep_gate_test",
                channel_id="nobody_has_a_grant_for_this_channel",
            )
        assert isinstance(result, PerplexityResult)
        assert result.content == "legacy path"


class TestGrantDerivedGatePath:

    def test_gate_path_end_to_end(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        cfg = make_config()
        import hypogeum.db as db
        db.initialize_schema(db_path)
        grant = _register_grant(priv, db_path, model=cfg.PERPLEXITY_MODEL)
        assert grant.status == "active"

        # _single_request_via_gate() resolves everything against the
        # PRODUCTION db_path internally (get_connection()/authority
        # calls with no db_path override, exactly as production code
        # will) -- point the production db_path at this test's isolated
        # file for the duration of this test only.
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        with patch("hypogeum.perplexity.requests.post", return_value=perplexity_200("gate path")):
            result = query_perplexity(
                "what happened", cfg, phase_id="phase_1", query_index=0, episode_id="ep_gate_test",
                channel_id="test_channel",
            )

        assert isinstance(result, PerplexityResult)
        assert result.content == "gate path"
        assert result.phase_id == "phase_1"
        assert result.query_index == 0
        assert result.usage["total_tokens"] == 230

    def test_gate_path_capacity_exhausted_fails_closed_with_zero_network_calls(self, keypair, tmp_path, monkeypatch):
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        cfg = make_config()
        import hypogeum.db as db
        db.initialize_schema(db_path)
        _register_grant(priv, db_path, model=cfg.PERPLEXITY_MODEL, max_calls=0, max_total_spend=Decimal("0.00"))
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        with patch("hypogeum.perplexity.requests.post") as mock_post:
            with pytest.raises(PerplexitySoftFailureExhausted, match="gate"):
                query_perplexity(
                    "what happened", cfg, phase_id="phase_1", query_index=0, episode_id="ep_gate_test",
                    channel_id="test_channel",
                )
            assert mock_post.called is False

    def test_gate_path_soft_failure_reclassified_and_exhausts(self, keypair, tmp_path, monkeypatch):
        """Empty choices[] on every attempt must surface as
        PerplexitySoftFailureExhausted after RETRY_MAX retries -- the
        adapter's soft-failure tuple reconstruction (see
        perplexity_adapter.py's own docstring) drives the SAME outer
        retry loop the legacy path uses, so retry_delay/time.sleep is
        exercised identically, each attempt its own gated envelope/
        capability (proven by completing without a spurious
        CapabilityAlreadyConsumedError)."""
        priv, _pub = keypair
        db_path = tmp_path / "auth.db"
        cfg = make_config()
        import hypogeum.db as db
        db.initialize_schema(db_path)
        _register_grant(priv, db_path, model=cfg.PERPLEXITY_MODEL, max_calls=5, max_total_spend=Decimal("10.00"))
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)

        empty_choices = perplexity_200()
        empty_choices.json.return_value = {"choices": [], "citations": [], "usage": {}}

        with patch("hypogeum.perplexity.requests.post", return_value=empty_choices), \
             patch("hypogeum.perplexity.time.sleep"):
            with pytest.raises(PerplexitySoftFailureExhausted, match="failure_type='empty_choices'"):
                query_perplexity(
                    "what happened", cfg, phase_id="phase_1", query_index=0, episode_id="ep_gate_test",
                    channel_id="test_channel",
                )
