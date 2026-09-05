"""
tests/test_operating_grant_domains.py — ADR-016 provider-domain
enforcement for the Channel Operating Grant (2026-08-29).

A signed Channel Operating Grant carries `allowed_provider_domains`.
Child authorization must enforce that a paid call's CANONICAL domain
(hypogeum.provider_domains) is present in that signed list -- a matching
(provider, model, provider_operation) binding is NOT sufficient on its
own. These tests prove every gap fails closed BEFORE any ledger
reservation or capability issuance.

Zero network. Temp DBs. Test-only Ed25519 keys.
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import hypogeum.authorization_authority as authority
import hypogeum.authorization_store as store
import hypogeum.db as db
from hypogeum.approval_artifact import GrantBindingApproval, sign_approval_artifact
from hypogeum.execution_envelope import GenerationExecutionEnvelope
from hypogeum.provider_domains import (
    PROVIDER_OPERATION_DOMAINS, UnknownProviderOperationError, resolve_provider_domain,
)

AT = "2026-08-29T00:00:00Z"
_CH = "hypogeum"
_WF_ID = "hypogeum_main_production"
_WF_VER = "1.0.0"
_WF_SUM = "a" * 64
_CP_VER = "v1"
_CP_SUM = "b" * 64
_CADENCE = "videos_per_week:1"
_CEIL = Decimal("135.00")

# Every canonical (provider, model, operation) the eight real gate paths use.
_CANON = [
    ("openrouter", "anthropic/claude-sonnet-4.6", "text.completion", "openrouter_text"),
    ("perplexity", "sonar-pro", "research.query", "perplexity_research"),
    ("elevenlabs", "eleven_multilingual_v2", "voice.narration_segment", "elevenlabs_voice"),
    ("elevenlabs", "music_v1", "music.track", "elevenlabs_music"),
    ("muapi", "veo3.1-lite-text-to-video", "video.text_to_video", "muapi_video"),
    ("muapi", "gpt-image-2-text-to-image", "image.reference", "muapi_image"),
    ("muapi", "gpt-image-2-text-to-image", "image.thumbnail", "muapi_thumbnail"),
    ("muapi", "veo3.1-lite-text-to-video", "upscale.video", "muapi_upscale"),
]


@pytest.fixture
def keypair(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(authority, "_load_trusted_public_key", lambda: priv.public_key())
    return priv


def _bindings(triples):
    return [
        store.GrantBinding(
            provider=p, model=m, provider_operation=o, currency="USD",
            max_cost_per_call=Decimal("3.00"), max_calls=1000, max_total_spend=Decimal("135.00"),
        )
        for (p, m, o) in triples
    ]


def _register_operating_grant(priv, db_path, *, domains, binding_triples,
                              grant_id="op_g", tamper_domains=None):
    gb = _bindings(binding_triples)
    _total = authority.operating_grant_total_capacity(
        _CEIL, "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z",
    )
    checksum = authority.compute_grant_checksum(
        channel_id=_CH, episode_id="*", workflow_id=_WF_ID, workflow_version=_WF_VER,
        workflow_checksum=_WF_SUM, production_plan_content_hash=None, bindings=gb,
        max_total_spend_all_bindings=_total, currency="USD", expires_at="2099-01-01T00:00:00Z",
        grant_kind="channel_operating", channel_package_version=_CP_VER, channel_package_checksum=_CP_SUM,
        allowed_provider_domains=list(domains), cadence_snapshot=_CADENCE,
        weekly_exposure_ceiling_usd=_CEIL, valid_from="2026-01-01T00:00:00Z",
        operating_period_ref="hypogeum:test",
    )
    fields = dict(
        approval_id=f"appr_{grant_id}", approval_version=1, approval_type="channel_operating_grant",
        subject_checksum=checksum, channel_id=_CH, scope_id="*", workflow_id=_WF_ID,
        workflow_version=_WF_VER, workflow_checksum=_WF_SUM, channel_package_version=_CP_VER,
        channel_package_checksum=_CP_SUM,
        allowed_bindings=tuple(GrantBindingApproval(b.provider, b.model, b.provider_operation) for b in gb),
        currency="USD", max_cost_per_call=Decimal("3.00"), max_total_spend=_total, max_calls=1000,
        retry_authorized=False, fallback_authorized=False, issued_at=AT, expires_at="2099-01-01T00:00:00Z",
        approver_identity="operator_test", nonce=f"nonce_{grant_id}",
    )
    art = sign_approval_artifact(fields, private_key=priv)
    grant = authority.register_channel_operating_grant(
        grant_id=grant_id, grant_version=1, channel_id=_CH, workflow_id=_WF_ID,
        workflow_version=_WF_VER, workflow_checksum=_WF_SUM, channel_package_version=_CP_VER,
        channel_package_checksum=_CP_SUM, allowed_provider_domains=list(domains),
        cadence_snapshot=_CADENCE, weekly_exposure_ceiling_usd=_CEIL, bindings=gb, currency="USD",
        valid_from="2026-01-01T00:00:00Z", expires_at="2099-01-01T00:00:00Z",
        operating_period_ref="hypogeum:test", approval_artifact=art, db_path=db_path,
    )
    if tamper_domains is not None:
        conn = db.get_connection(db_path)
        try:
            import json
            conn.execute("UPDATE run_authorization_grants SET allowed_provider_domains_json=? WHERE grant_id=?",
                         (json.dumps(sorted(tamper_domains)), grant_id))
            conn.commit()
        finally:
            conn.close()
    return grant


def _env(provider, model, operation, *, idem="idem_dom_1", auth_id="auth_dom_1"):
    prompt = f"compiled {operation} {idem}"
    return GenerationExecutionEnvelope.create(
        envelope_schema_version="1.0", envelope_version=1, channel_id=_CH,
        channel_package_version=_CP_VER, channel_package_checksum=_CP_SUM, execution_id="ep_dom",
        workflow_version=_WF_VER, plan_version="1.0", plan_checksum="c" * 64, planned_shot_id="s1",
        structured_spec_checksum="d" * 64, compiler_identity="pkg", compiler_version="v1",
        operation_payload_content=prompt, operation_payload_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        operation_payload_schema_version=f"{operation}/1.0",
        content_provenance={"compiler": "pkg", "compiler_version": "v1", "source_work_item_id": "s1", "rendered_at": AT},
        provider=provider, model=model, provider_operation=operation,
        deterministic_output_path=f"/tmp/{idem}.out", expected_output_identity=f"asset_{idem}",
        authorization_id=auth_id, authorization_version=1, max_cost_per_call=Decimal("2.50"),
        run_level_budget_ref="run_1", episode_level_budget_ref="ep_dom", currency="USD",
        retry_authorized=False, fallback_authorized=False, idempotency_key=idem, created_at=AT,
        expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
        dependency_checksums={"channel_package": _CP_SUM, "plan": "c" * 64,
                              "structured_spec": "d" * 64, "compiler": "e" * 64},
    )


def _issue(env, grant, db_path):
    return authority.issue_child_authorization(env, grant.grant_id, grant.grant_version, at=AT, db_path=db_path)


# ---------------------------------------------------------------------------
# Canonical mapping
# ---------------------------------------------------------------------------

class TestCanonicalMapping:
    def test_all_eight_operations_map(self):
        for p, _m, o, expected in _CANON:
            assert resolve_provider_domain(p, o) == expected

    def test_unknown_operation_fails_closed(self):
        with pytest.raises(UnknownProviderOperationError):
            resolve_provider_domain("muapi", "video.magic")
        with pytest.raises(UnknownProviderOperationError):
            resolve_provider_domain("nobody", "text.completion")

    def test_aliases_do_not_resolve(self):
        for bad in [("MUAPI", "video.text_to_video"), ("muapi", "video.text_to_video "),
                    (" muapi", "video.text_to_video"), ("muapi", "Video.Text_To_Video"),
                    ("openrouter", "text.completions")]:
            with pytest.raises(UnknownProviderOperationError):
                resolve_provider_domain(*bad)


# ---------------------------------------------------------------------------
# Enforcement in issue_child_authorization
# ---------------------------------------------------------------------------

class TestDomainEnforcement:
    def _db(self, tmp_path):
        d = tmp_path / "dom.db"
        db.initialize_schema(d)
        return d

    def test_valid_binding_denied_when_domain_absent_from_signed_grant(self, keypair, tmp_path):
        """The grant has a binding for muapi/veo/video.text_to_video, but
        its signed allowed_provider_domains does NOT include muapi_video.
        The binding matches -- the call is still denied."""
        d = self._db(tmp_path)
        grant = _register_operating_grant(
            keypair, d,
            domains=["openrouter_text"],                                  # muapi_video NOT signed
            binding_triples=[("muapi", "veo3.1-lite-text-to-video", "video.text_to_video")],
        )
        env = _env("muapi", "veo3.1-lite-text-to-video", "video.text_to_video")
        with pytest.raises(authority.GrantScopeMismatchError, match="allowed_provider_domains"):
            _issue(env, grant, d)
        # zero ledger consumption rows on denial
        conn = db.get_connection(d)
        try:
            n = conn.execute("SELECT COUNT(*) FROM run_authorization_grant_consumption").fetchone()[0]
        finally:
            conn.close()
        assert n == 0

    @pytest.mark.parametrize("provider,model,operation,domain", _CANON)
    def test_each_operation_denied_when_its_domain_is_absent(self, keypair, tmp_path,
                                                             provider, model, operation, domain):
        d = self._db(tmp_path)
        other_domains = [dm for dm in {v for v in PROVIDER_OPERATION_DOMAINS.values()} if dm != domain]
        grant = _register_operating_grant(
            keypair, d, domains=other_domains,                       # every domain EXCEPT this one
            binding_triples=[(provider, model, operation)],          # binding present
        )
        env = _env(provider, model, operation)
        with pytest.raises(authority.GrantScopeMismatchError):
            _issue(env, grant, d)

    @pytest.mark.parametrize("provider,model,operation,domain", _CANON)
    def test_each_operation_authorized_when_its_domain_is_present(self, keypair, tmp_path,
                                                                  provider, model, operation, domain):
        d = self._db(tmp_path)
        grant = _register_operating_grant(
            keypair, d, domains=[domain], binding_triples=[(provider, model, operation)],
        )
        env = _env(provider, model, operation)
        rec = _issue(env, grant, d)
        assert rec.current_state.value == "authorized"

    def test_unknown_operation_fails_closed(self, keypair, tmp_path):
        d = self._db(tmp_path)
        grant = _register_operating_grant(
            keypair, d, domains=["muapi_video"],
            binding_triples=[("muapi", "veo3.1-lite-text-to-video", "video.exotic")],
        )
        env = _env("muapi", "veo3.1-lite-text-to-video", "video.exotic")
        with pytest.raises(authority.GrantScopeMismatchError):
            _issue(env, grant, d)

    def test_foreign_domain_in_signed_list_still_denies_this_call(self, keypair, tmp_path):
        d = self._db(tmp_path)
        grant = _register_operating_grant(
            keypair, d, domains=["elevenlabs_music", "perplexity_research"],
            binding_triples=[("openrouter", "anthropic/claude-sonnet-4.6", "text.completion")],
        )
        env = _env("openrouter", "anthropic/claude-sonnet-4.6", "text.completion")
        with pytest.raises(authority.GrantScopeMismatchError):
            _issue(env, grant, d)

    def test_row_tampered_domain_list_fails_grant_resolution(self, keypair, tmp_path):
        """Widening allowed_provider_domains post-signature breaks the
        grant checksum -> resolve_active_operating_grant returns None and
        issue_child_authorization cannot even find an active grant."""
        d = self._db(tmp_path)
        grant = _register_operating_grant(
            keypair, d, domains=["openrouter_text"],
            binding_triples=[("muapi", "veo3.1-lite-text-to-video", "video.text_to_video")],
            tamper_domains=["openrouter_text", "muapi_video"],
        )
        assert authority.resolve_active_operating_grant(_CH, at=AT, db_path=d) is None
        env = _env("muapi", "veo3.1-lite-text-to-video", "video.text_to_video")
        with pytest.raises(authority.AuthorizationAuthorityError):
            _issue(env, grant, d)

    def test_run_grant_is_unaffected(self, keypair, tmp_path):
        """grant_kind='run' has no domain list -> the domain check is a
        no-op; existing run-grant behaviour is unchanged."""
        from hypogeum.authorization_authority import _validate_operating_grant_domain
        d = self._db(tmp_path)
        env = _env("muapi", "veo3.1-lite-text-to-video", "video.text_to_video")

        class _RunGrant:
            grant_kind = "run"
            grant_id = "rg"
            grant_version = 1
            allowed_provider_domains = None
        assert _validate_operating_grant_domain(_RunGrant(), env) == ""
