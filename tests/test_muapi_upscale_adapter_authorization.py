"""
tests/test_muapi_upscale_adapter_authorization.py — adversarial coverage
for the MUAPI upscale authorization boundary (2026-08-28 authenticity
hardening).

Invariants proven here:

  * There is NO provider-level method that reaches the MUAPI network
    boundary (the removed _execute_upscale_http() stays removed); the raw
    HTTP sequence lives ONLY in
    hypogeum.providers.muapi_upscale_adapter._execute_raw_upscale_http()
    and refuses to run without the verified execution envelope
    MuapiUpscaleAdapter.invoke() produces.

  * A COMPLETELY FABRICATED AdapterInvocationCapability whose checksum is
    honestly recomputed with the same public compute_capability_checksum()
    is REJECTED before any network I/O -- authenticity is proven against
    authoritative persisted state (execution_envelopes, authorization_
    records, the WriteAheadReceiptStore trail), never against the
    recomputable checksum.

  * Single-use is durable and restart-safe: a fresh MuapiUpscaleAdapter
    instance (simulated process restart) rejects a capability the gate
    already drove to completion; two concurrent invocations of one
    capability execute the provider at most once.

  * Every identity family the capability carries (envelope id/version,
    provider, model, operation, payload hash, currency, max cost,
    attempt/idempotency identity, lease owner + fencing token, budget
    reservation ids, issued_at) is verified against the persisted
    issuance -- a per-family forgery with a fully recomputed, internally
    consistent checksum still fails closed.

  * The public MuapiVideoUpscalerProvider.enhance() has no caller-
    controlled bypass kwarg.
"""
from __future__ import annotations

import dataclasses
import hashlib
import sys
import threading
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import hypogeum.adapter_registry as adapter_registry
import hypogeum.authorization_authority as authority
import hypogeum.capability_boundary as capability_boundary
import hypogeum.channel_config as channel_config
import hypogeum.db as db
import hypogeum.production_plan as pp
import hypogeum.providers.muapi_upscale_adapter as adapter_mod
import hypogeum.providers.muapi_video_upscaler as up
import hypogeum.submission_gate as submission_gate
import hypogeum.workflow_executor as workflow_executor
from hypogeum.providers.enhancement import EnhancementResult
from hypogeum.providers.fake_provider_adapter import (
    AdapterInvocationCapability,
    AdapterResult,
    CapabilityAlreadyConsumedError,
    InvalidCapabilityError,
    RawPromptRejectedError,
    compute_capability_checksum,
)
from hypogeum.capability_boundary import CapabilityExpiredError, VerifiedInvocationContext
from hypogeum.providers.muapi_upscale_adapter import (
    SCHEMA_VERSION,
    CapabilityAuthenticityError,
    MuapiUpscaleAdapter,
    _execute_raw_upscale_http,
    encode_upscale_payload,
)

from test_muapi import make_config  # noqa: E402
from test_muapi_upscale_gate_path import _make_clip, _persist_boundary_plan, _register_grant  # noqa: E402

_EP = "ep_gate_test"
_SCENE = "s01"


class _Log:
    def info(self, *a, **k): pass
    def warn(self, *a, **k): pass
    def error(self, *a, **k): pass


def _cfg(**over):
    base = dict(MUAPI_DRY_RUN=False, MUAPI_API_KEY="k", MUAPI_BASE_URL="https://muapi.test",
               MUAPI_TIMEOUT_SECONDS=5, MUAPI_MAX_POLL_ATTEMPTS=2, MUAPI_POLL_INTERVAL_SECONDS=0)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _reset_adapter_cache():
    adapter_registry.reset_adapter_instance_cache()
    yield
    adapter_registry.reset_adapter_instance_cache()


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
            "registration_identity": "test-registration", "evaluation_timestamp": "2026-01-01T00:00:00Z",
        },
    )


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    """A fully wired, isolated gate environment: isolated DB, a real
    channel + weekly ceiling, an integrity-verified persisted Production
    Plan at the (redirected) canonical episode dir, a signed run grant,
    and _ASSETS_ROOT redirected for the write-ahead receipt trail."""
    db_path = tmp_path / "auth.db"
    db.initialize_schema(db_path)

    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(authority, "_load_trusted_public_key", lambda: priv.public_key())

    episode_dir = _persist_boundary_plan(db_path, tmp_path, monkeypatch=monkeypatch)
    _register_grant(priv, db_path)
    monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(up, "_ASSETS_ROOT", tmp_path)
    monkeypatch.setattr(capability_boundary, "_DEFAULT_ASSETS_ROOT", tmp_path)

    src = tmp_path / "src.mp4"
    _make_clip(src, 1280, 720)
    dst = tmp_path / "dst.mp4"
    return SimpleNamespace(
        tmp_path=tmp_path, db_path=db_path, episode_dir=episode_dir, src=src, dst=dst,
    )


def _enhance(env, config=None):
    return up.MuapiVideoUpscalerProvider().enhance(
        source_path=env.src, dest_path=env.dst, target_width=1920, target_height=1080,
        target_resolution_label="1080p", config=config or make_config(), episode_id=_EP,
        scene_id=_SCENE, log=MagicMock(), channel_id="test_channel", output_dir=env.episode_dir,
    )


class _CaptureStop(Exception):
    pass


def _capture_capability_pre_invoke(env, monkeypatch):
    """Run the real gate up to _mint_capability(), capture the genuine
    minted capability, and stop before the adapter is invoked. Leaves the
    durable receipt trail in exactly the state the gate leaves it in
    immediately before calling MuapiUpscaleAdapter.invoke():
    [ABOUT_TO_SUBMIT, SUBMISSION_ATTEMPT_STARTED]."""
    holder = {}
    real_mint = submission_gate._mint_capability

    def _capturing_mint(*a, **k):
        cap = real_mint(*a, **k)
        holder["cap"] = cap
        raise _CaptureStop()

    # Restore ONLY _mint_capability afterwards -- never monkeypatch.undo(),
    # which would also revert the gate_env fixture's db/_ASSETS_ROOT/
    # canonical-dir patches that the later real invoke() still needs.
    submission_gate._mint_capability = _capturing_mint
    try:
        with pytest.raises(_CaptureStop):
            _enhance(env)
    finally:
        submission_gate._mint_capability = real_mint
    return holder["cap"]


def _drive_capability_to_completion(env, monkeypatch):
    """Run the full real gate once, stubbing only the raw network fn, so
    the durable trail ends in ACCEPTED. Returns the genuine capability."""
    holder = {}
    real_invoke_real = MuapiUpscaleAdapter._invoke_real

    def _recording_invoke_real(self, ctx):
        ctx.assert_live()
        holder["cap"] = ctx.capability
        return AdapterResult(
            outcome="accepted", provider_job_id="req-done", provider_request_id="req-done",
            charged_amount=Decimal("0.07"), response_checksum="c" * 64,
            evidence={"output_path": str(env.dst), "cost_usd": 0.07, "generation_time_s": 0.01,
                      "provider": "muapi", "model": "ai-video-upscaler", "provider_version": "1.0"},
        )

    monkeypatch.setattr(MuapiUpscaleAdapter, "_invoke_real", _recording_invoke_real)
    result = _enhance(env)
    monkeypatch.setattr(MuapiUpscaleAdapter, "_invoke_real", real_invoke_real)
    assert result.success is True, result.failure_reason
    return holder["cap"]


# ---------------------------------------------------------------------------
# 1 — no provider-level path to raw HTTP
# ---------------------------------------------------------------------------

class TestNoProviderMethodReachesRawHTTP:
    def test_provider_has_no_execute_upscale_http(self):
        prov = up.MuapiVideoUpscalerProvider()
        assert not hasattr(prov, "_execute_upscale_http")

    def test_raw_http_fn_refuses_a_non_context(self, tmp_path):
        sentinel = MagicMock(side_effect=AssertionError("network reached"))
        with patch("requests.post", sentinel), patch("requests.get", sentinel):
            for bad_ctx in (None, "not-a-context", object(), 123):
                with pytest.raises(CapabilityAuthenticityError):
                    _execute_raw_upscale_http(ctx=bad_ctx, config=_cfg(), t0=0.0)
        sentinel.assert_not_called()

    def test_raw_http_fn_refuses_a_hand_built_context(self, tmp_path, monkeypatch):
        """A caller loads a real persisted envelope + fabricates/reuses a
        capability + hand-builds a VerifiedInvocationContext and calls the
        raw fn directly. ctx.assert_live() re-checks the durable trail
        (the context's claim_nonce must be the HEAD) -> rejected before
        the network. Type alone is not authorization."""
        monkeypatch.setattr(capability_boundary, "_DEFAULT_ASSETS_ROOT", tmp_path)
        payload = encode_upscale_payload(
            source_path=str(tmp_path / "s.mp4"), dest_path=str(tmp_path / "d.mp4"),
            target_width=1920, target_height=1080, target_resolution_label="1080p",
            episode_id="ep_x", scene_id="s01",
        )
        cap = AdapterInvocationCapability(
            capability_id="c", envelope_id="e", envelope_version=1, attempt_id="a",
            idempotency_key="a", lease_owner_id="o", lease_fencing_token=1,
            provider=up.PROVIDER_NAME, model=up.MODEL_NAME, provider_operation=up._UPSCALE_PROVIDER_OPERATION,
            operation_payload_bytes=payload, operation_payload_sha256=hashlib.sha256(payload).hexdigest(),
            operation_payload_schema_version=SCHEMA_VERSION, max_cost_per_call=Decimal("0.10"),
            currency="USD", budget_reservation_ids=["r"], issued_at="2026-08-28T00:00:00Z",
            expires_at="2099-01-01T00:00:00Z", capability_checksum="x",
        )
        forged_ctx = VerifiedInvocationContext(
            capability=cap, envelope=None, operation_payload={}, execution_id="ep_x",
            idempotency_key="a", claim_nonce="attacker-picked", _assets_root=tmp_path,
            _db_path=None, _clock=capability_boundary.now_utc,
        )
        sentinel = MagicMock(side_effect=AssertionError("network reached"))
        with patch("requests.post", sentinel), patch("requests.get", sentinel):
            with pytest.raises(CapabilityAuthenticityError):
                _execute_raw_upscale_http(ctx=forged_ctx, config=_cfg(), t0=0.0)
        sentinel.assert_not_called()


# ---------------------------------------------------------------------------
# 2 — a fabricated, recomputed-checksum capability is rejected
# ---------------------------------------------------------------------------

class TestForgedCapabilityRejected:
    def _forge(self, tmp_path, **over):
        src = tmp_path / "src.mp4"; src.write_bytes(b"\x00" * 32)
        payload = encode_upscale_payload(
            source_path=str(src), dest_path=str(tmp_path / "dst.mp4"), target_width=1920,
            target_height=1080, target_resolution_label="1080p", episode_id=_EP, scene_id=_SCENE,
        )
        fields = dict(
            capability_id="forged:cap", envelope_id="forged-envelope", envelope_version=1,
            attempt_id=f"{_EP}:upscale:{_SCENE}", idempotency_key=f"{_EP}:upscale:{_SCENE}",
            lease_owner_id="muapi_upscale_adapter:s01", lease_fencing_token=1,
            provider=up.PROVIDER_NAME, model=up.MODEL_NAME, provider_operation=up._UPSCALE_PROVIDER_OPERATION,
            operation_payload_bytes=payload, operation_payload_sha256=hashlib.sha256(payload).hexdigest(),
            operation_payload_schema_version=SCHEMA_VERSION, max_cost_per_call=Decimal("0.10"),
            currency="USD", budget_reservation_ids=["call:s01", "episode:ep"],
            issued_at="2026-08-28T00:00:00Z", expires_at="2099-01-01T00:00:00Z",
            capability_checksum="PLACEHOLDER",
        )
        fields.update(over)
        cap = AdapterInvocationCapability(**fields)
        # honestly recompute a MATCHING checksum with the public function
        return dataclasses.replace(cap, capability_checksum=compute_capability_checksum(cap))

    def test_fully_fabricated_capability_is_rejected_before_network(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", tmp_path / "empty.db")
        db.initialize_schema(tmp_path / "empty.db")
        monkeypatch.setattr(up, "_ASSETS_ROOT", tmp_path)
        monkeypatch.setattr(pp, "canonical_episode_dir", lambda ep: tmp_path / "episodes" / ep)

        forged = self._forge(tmp_path)
        # its checksum is genuinely valid
        assert compute_capability_checksum(forged) == forged.capability_checksum

        sentinel = MagicMock(side_effect=AssertionError("network reached"))
        with patch("requests.post", sentinel), patch("requests.get", sentinel):
            with pytest.raises(CapabilityAuthenticityError):
                MuapiUpscaleAdapter().invoke(forged)
        sentinel.assert_not_called()

    def test_fabricated_pointed_at_real_envelope_but_foreign_attempt_is_rejected(self, gate_env, monkeypatch):
        real_cap = _capture_capability_pre_invoke(gate_env, monkeypatch)
        # keep the real (persisted) envelope_id, but forge a different
        # attempt/idempotency identity -> no receipt trail for it
        forged = self._forge(
            gate_env.tmp_path, envelope_id=real_cap.envelope_id, envelope_version=real_cap.envelope_version,
            attempt_id="ep_gate_test:upscale:FOREIGN", idempotency_key="ep_gate_test:upscale:FOREIGN",
        )
        sentinel = MagicMock(side_effect=AssertionError("network reached"))
        with patch("requests.post", sentinel), patch("requests.get", sentinel):
            with pytest.raises(InvalidCapabilityError):
                MuapiUpscaleAdapter().invoke(forged)
        sentinel.assert_not_called()

    def test_self_consistent_envelope_without_authorization_record_is_rejected(self, tmp_path, monkeypatch):
        """Persist a self-consistent execution envelope + a hand-written
        receipt trail, but NO authorization_records row -> rejected at the
        signed-grant child-authorization anchor."""
        import hypogeum.authorization_store as astore
        from hypogeum.execution_envelope import GenerationExecutionEnvelope
        from hypogeum.provider_receipt import EventType, ProviderReceiptEvent, WriteAheadReceiptStore

        dbp = tmp_path / "e.db"
        db.initialize_schema(dbp)
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", dbp)
        monkeypatch.setattr(up, "_ASSETS_ROOT", tmp_path)
        monkeypatch.setattr(pp, "canonical_episode_dir", lambda ep: tmp_path / "episodes" / ep)
        _persist_boundary_plan(dbp, tmp_path, monkeypatch=monkeypatch)
        plan = pp.load_production_plan(_EP, tmp_path / "episodes" / _EP)

        src = tmp_path / "src.mp4"; src.write_bytes(b"\x00" * 32)
        payload = encode_upscale_payload(
            source_path=str(src), dest_path=str(tmp_path / "dst.mp4"), target_width=1920,
            target_height=1080, target_resolution_label="1080p", episode_id=_EP, scene_id=_SCENE,
        )
        p_sha = hashlib.sha256(payload).hexdigest()
        aid = f"{_EP}:upscale:{_SCENE}"
        at = "2026-08-28T00:00:00+00:00"
        env = GenerationExecutionEnvelope.create(
            envelope_schema_version="1.0", envelope_version=1, channel_id="test_channel",
            channel_package_version="1.0", channel_package_checksum="b" * 64, execution_id=_EP,
            workflow_version="1.0.0", plan_version="live", plan_checksum="d" * 64, planned_shot_id=_SCENE,
            structured_spec_checksum="e" * 64, compiler_identity=up._UPSCALE_COMPILER_IDENTITY,
            compiler_version="1", operation_payload_content=payload.decode(), operation_payload_sha256=p_sha,
            operation_payload_schema_version=SCHEMA_VERSION,
            content_provenance={"compiler": up._UPSCALE_COMPILER_IDENTITY, "compiler_version": "1",
                                "source_work_item_id": _SCENE, "rendered_at": at},
            provider=up.PROVIDER_NAME, model=up.MODEL_NAME, provider_operation=up._UPSCALE_PROVIDER_OPERATION,
            deterministic_output_path=str(tmp_path / "dst.mp4"),
            expected_output_identity=f"episodes/{_EP}/upscale/{_SCENE}", authorization_id=aid,
            authorization_version=1, max_cost_per_call=Decimal("0.10"), run_level_budget_ref=_EP,
            episode_level_budget_ref=_EP, currency="USD", retry_authorized=False, fallback_authorized=False,
            idempotency_key=aid, created_at=at,
            expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
            dependency_checksums={"channel_package": "b" * 64, "plan": "d" * 64,
                                  "structured_spec": "e" * 64, "compiler": "f" * 64},
            audit_metadata={"production_plan_content_hash": plan.content_hash,
                            "plan_weekly_reservation_id": "made-up-reservation"},
        )
        conn = db.get_connection()
        astore.persist_execution_envelope(conn, env)
        conn.close()

        store = WriteAheadReceiptStore(tmp_path / "episodes" / _EP)
        common = dict(
            envelope_id=env.envelope_id, envelope_version=1, authorization_id=aid, authorization_version=1,
            attempt_id=aid, idempotency_key=aid, provider=up.PROVIDER_NAME, model=up.MODEL_NAME,
            provider_operation=up._UPSCALE_PROVIDER_OPERATION, provider_bound_content_sha256=p_sha,
            max_cost_per_call=Decimal("0.10"), currency="USD",
        )
        store.prepare_attempt(ProviderReceiptEvent(event_type=EventType.ABOUT_TO_SUBMIT, event_seq=0,
                              at=at, evidence={"reservation_ids": ["r1"]}, **common), execution_id=_EP)
        store.append_event(ProviderReceiptEvent(event_type=EventType.SUBMISSION_ATTEMPT_STARTED, event_seq=1,
                           at=at, evidence={"lease_owner_id": "o", "lease_fencing_token": 1}, **common),
                           execution_id=_EP)

        cap = AdapterInvocationCapability(
            capability_id=f"{aid}:x", envelope_id=env.envelope_id, envelope_version=1, attempt_id=aid,
            idempotency_key=aid, lease_owner_id="o", lease_fencing_token=1, provider=up.PROVIDER_NAME,
            model=up.MODEL_NAME, provider_operation=up._UPSCALE_PROVIDER_OPERATION,
            operation_payload_bytes=payload, operation_payload_sha256=p_sha,
            operation_payload_schema_version=SCHEMA_VERSION, max_cost_per_call=Decimal("0.10"),
            currency="USD", budget_reservation_ids=["r1"], issued_at=at, expires_at="2099-01-01T00:00:00Z",
            capability_checksum="P",
        )
        cap = dataclasses.replace(cap, capability_checksum=compute_capability_checksum(cap))

        sentinel = MagicMock(side_effect=AssertionError("network reached"))
        with patch("requests.post", sentinel), patch("requests.get", sentinel):
            with pytest.raises(CapabilityAuthenticityError):
                MuapiUpscaleAdapter().invoke(cap)
        sentinel.assert_not_called()


# ---------------------------------------------------------------------------
# 3 — restart-safe / concurrency-safe single use
# ---------------------------------------------------------------------------

class TestDurableSingleUse:
    def test_fresh_adapter_after_restart_rejects_completed_capability(self, gate_env, monkeypatch):
        cap = _drive_capability_to_completion(gate_env, monkeypatch)

        sentinel = MagicMock(side_effect=AssertionError("network reached on replay"))
        with patch("requests.post", sentinel), patch("requests.get", sentinel):
            with pytest.raises(CapabilityAlreadyConsumedError):
                MuapiUpscaleAdapter().invoke(cap)   # brand-new instance == process restart
        sentinel.assert_not_called()

    def test_two_concurrent_invocations_execute_provider_at_most_once(self, gate_env, monkeypatch):
        cap = _capture_capability_pre_invoke(gate_env, monkeypatch)

        calls = []
        barrier = threading.Barrier(2)

        def _slow_raw(*, ctx, config, t0):
            barrier.wait(timeout=5)
            calls.append(ctx.capability.capability_id)
            return EnhancementResult(
                success=True, request_id="req-conc", output_path=Path(gate_env.dst),
                provider="muapi", model="ai-video-upscaler", provider_version="1.0",
                cost_usd=0.07, generation_time_s=0.01,
            )

        monkeypatch.setattr(adapter_mod, "_execute_raw_upscale_http", _slow_raw)

        outcomes = {}

        def _run(tag):
            try:
                outcomes[tag] = MuapiUpscaleAdapter().invoke(cap).outcome
            except Exception as exc:  # noqa: BLE001
                outcomes[tag] = type(exc).__name__
                try:
                    barrier.wait(timeout=5)  # release the peer if we lost before entering _slow_raw
                except threading.BrokenBarrierError:
                    pass

        t1 = threading.Thread(target=_run, args=("a",))
        t2 = threading.Thread(target=_run, args=("b",))
        t1.start(); t2.start(); t1.join(10); t2.join(10)

        assert len(calls) <= 1
        assert sorted(outcomes.values()) == ["CapabilityAlreadyConsumedError", "accepted"]

    def test_valid_capability_reaches_raw_http_exactly_once(self, gate_env, monkeypatch):
        cap = _capture_capability_pre_invoke(gate_env, monkeypatch)

        seen = []

        def _rec_raw(*, ctx, config, t0):
            from hypogeum.providers.muapi_upscale_adapter import decode_upscale_payload
            seen.append(decode_upscale_payload(ctx.capability.operation_payload_bytes))
            return EnhancementResult(
                success=True, request_id="req-ok", output_path=Path(gate_env.dst),
                provider="muapi", model="ai-video-upscaler", provider_version="1.0",
                cost_usd=0.07, generation_time_s=0.01,
            )

        monkeypatch.setattr(adapter_mod, "_execute_raw_upscale_http", _rec_raw)

        adapter = MuapiUpscaleAdapter()
        res = adapter.invoke(cap)
        assert res.outcome == "accepted"
        assert len(seen) == 1
        assert seen[0]["episode_id"] == _EP
        assert seen[0]["target_resolution_label"] == "1080p"

        with pytest.raises(CapabilityAlreadyConsumedError):
            MuapiUpscaleAdapter().invoke(cap)
        assert len(seen) == 1


# ---------------------------------------------------------------------------
# 4 — per-identity-family binding (fully recomputed forged data)
# ---------------------------------------------------------------------------

class TestPerIdentityFamilyBinding:
    @pytest.mark.parametrize("field,bad", [
        ("envelope_id", "some-other-envelope"),
        ("envelope_version", 2),
        ("provider", "openai"),
        ("model", "some-other-model"),
        ("provider_operation", "generate.video"),
        ("currency", "EUR"),
        ("max_cost_per_call", Decimal("999.00")),
        ("lease_owner_id", "attacker"),
        ("lease_fencing_token", 999),
        ("budget_reservation_ids", ["call:someone-elses"]),
        ("issued_at", "2000-01-01T00:00:00+00:00"),
    ])
    def test_single_family_forgery_with_recomputed_checksum_fails_closed(self, gate_env, monkeypatch, field, bad):
        base = _capture_capability_pre_invoke(gate_env, monkeypatch)
        forged = dataclasses.replace(base, **{field: bad})
        forged = dataclasses.replace(forged, capability_checksum=compute_capability_checksum(forged))
        assert compute_capability_checksum(forged) == forged.capability_checksum  # honestly valid checksum

        sentinel = MagicMock(side_effect=AssertionError("network reached"))
        with patch("requests.post", sentinel), patch("requests.get", sentinel):
            with pytest.raises(InvalidCapabilityError):
                MuapiUpscaleAdapter().invoke(forged)
        sentinel.assert_not_called()

    def test_payload_hash_family_forgery_fails_closed(self, gate_env, monkeypatch):
        base = _capture_capability_pre_invoke(gate_env, monkeypatch)
        other_payload = encode_upscale_payload(
            source_path="/x", dest_path="/y", target_width=1920, target_height=1080,
            target_resolution_label="4k", episode_id=_EP, scene_id=_SCENE,
        )
        forged = dataclasses.replace(
            base, operation_payload_bytes=other_payload,
            operation_payload_sha256=hashlib.sha256(other_payload).hexdigest(),
        )
        forged = dataclasses.replace(forged, capability_checksum=compute_capability_checksum(forged))
        sentinel = MagicMock(side_effect=AssertionError("network reached"))
        with patch("requests.post", sentinel), patch("requests.get", sentinel):
            with pytest.raises(InvalidCapabilityError):
                MuapiUpscaleAdapter().invoke(forged)
        sentinel.assert_not_called()


# ---------------------------------------------------------------------------
# 5 — simple invariants retained
# ---------------------------------------------------------------------------

class TestBasicInvariants:
    def test_raw_string_prompt_is_refused(self):
        with pytest.raises(RawPromptRejectedError):
            MuapiUpscaleAdapter().invoke("just a prompt string")

    def test_wrong_schema_version_is_rejected(self, gate_env, monkeypatch):
        base = _capture_capability_pre_invoke(gate_env, monkeypatch)
        forged = dataclasses.replace(base, operation_payload_schema_version="generate.video/1.0")
        forged = dataclasses.replace(forged, capability_checksum=compute_capability_checksum(forged))
        with pytest.raises(InvalidCapabilityError):
            MuapiUpscaleAdapter().invoke(forged)

    @pytest.mark.parametrize("kw", [
        {"_via_gate_adapter": True}, {"skip_authorization": True}, {"already_authorized": True},
        {"_internal": True}, {"authorized": True}, {"bypass": True},
    ])
    def test_enhance_rejects_internal_looking_kwargs(self, tmp_path, kw):
        with pytest.raises(TypeError):
            up.MuapiVideoUpscalerProvider().enhance(
                source_path=tmp_path / "s.mp4", dest_path=tmp_path / "d.mp4", target_width=1920,
                target_height=1080, target_resolution_label="1080p", config=_cfg(), episode_id=_EP,
                scene_id=_SCENE, log=_Log(), channel_id="test_channel", **kw,
            )
