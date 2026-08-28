"""
tests/test_capability_boundary.py — the ONE shared authoritative
capability verifier + durable single-use consumer
(hypogeum.capability_boundary), exercised directly and provider-
agnostically.

Covers, against a REAL grant-derived submission-gate issuance (built via
the upscale domain's gate path, which persists an execution envelope + a
signed-grant child authorization + the write-ahead receipt trail):

  * a fully fabricated AdapterInvocationCapability with a freshly
    recomputed public checksum is rejected before any provider I/O;
  * capability TTL enforced against an INJECTED clock: valid inside the
    window; rejected when expired; rejected when issued in the future
    beyond clock skew; rejected exactly at the expiry boundary;
  * CURRENT lease/fencing is authoritative -- releasing the lease after
    issuance makes the capability unusable even though the receipt
    evidence still names the old owner/token;
  * durable, atomic single-use: a second verify (same process, and a
    fresh call simulating a process restart) is rejected; two concurrent
    verifies claim consumption exactly once;
  * every identity family (envelope id/version, provider, model,
    operation, payload hash, currency, max cost, attempt/idempotency,
    lease owner + fencing token, budget reservation ids, issued_at) is
    verified against the persisted issuance -- a per-family forgery with
    a fully recomputed checksum still fails closed;
  * MuapiVideoAdapter (the sibling that never "exposed" the hole but
    carried the identical one) now rejects a fabricated capability too.

No real provider call; temp DBs, injected clocks, and network sentinels
only.
"""
from __future__ import annotations

import dataclasses
import hashlib
import sys
import threading
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import hypogeum.adapter_registry as adapter_registry
import hypogeum.authorization_authority as authority
import hypogeum.capability_boundary as cb
import hypogeum.channel_config as channel_config
import hypogeum.db as db
import hypogeum.production_plan as pp
import hypogeum.providers.muapi_video_upscaler as up
import hypogeum.submission_gate as submission_gate
import hypogeum.workflow_executor as workflow_executor
from hypogeum.capability_boundary import (
    CapabilityAuthenticityError,
    CapabilityExpiredError,
    verify_and_consume_capability,
)
from hypogeum.providers.fake_provider_adapter import (
    AdapterInvocationCapability,
    CapabilityAlreadyConsumedError,
    InvalidCapabilityError,
    compute_capability_checksum,
)

from test_muapi_upscale_gate_path import _make_clip, _persist_boundary_plan, _register_grant  # noqa: E402

_EP = "ep_gate_test"
_SCENE = "s01"


@pytest.fixture(autouse=True)
def _reset_cache():
    adapter_registry.reset_adapter_instance_cache()
    yield
    adapter_registry.reset_adapter_instance_cache()


@pytest.fixture(autouse=True)
def _isolated_identity(monkeypatch):
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


class _Stop(Exception):
    pass


@pytest.fixture
def issued(tmp_path, monkeypatch):
    """Drive the real upscale gate up to _mint_capability(); capture the
    genuine capability + lease coordinates; leave the durable receipt
    trail in the exact pre-adapter state."""
    db_path = tmp_path / "auth.db"
    db.initialize_schema(db_path)
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(authority, "_load_trusted_public_key", lambda: priv.public_key())
    episode_dir = _persist_boundary_plan(db_path, tmp_path, monkeypatch=monkeypatch)
    _register_grant(priv, db_path)
    monkeypatch.setattr(db, "_DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(up, "_ASSETS_ROOT", tmp_path)
    monkeypatch.setattr(cb, "_DEFAULT_ASSETS_ROOT", tmp_path)

    src = tmp_path / "src.mp4"
    _make_clip(src, 1280, 720)

    holder = {}
    real_mint = submission_gate._mint_capability

    def _capturing(envelope, attempt_id, owner, token, reservation_ids, at, expires_at):
        cap = real_mint(envelope, attempt_id, owner, token, reservation_ids, at, expires_at)
        holder.update(cap=cap, envelope_id=envelope.envelope_id, owner=owner, token=token, at=at)
        raise _Stop()

    submission_gate._mint_capability = _capturing
    try:
        with pytest.raises(_Stop):
            up.MuapiVideoUpscalerProvider().enhance(
                source_path=src, dest_path=tmp_path / "dst.mp4", target_width=1920, target_height=1080,
                target_resolution_label="1080p", config=_cfg(), episode_id=_EP, scene_id=_SCENE,
                log=MagicMock(), channel_id="test_channel", output_dir=episode_dir,
            )
    finally:
        submission_gate._mint_capability = real_mint

    holder["db_path"] = db_path
    holder["tmp_path"] = tmp_path
    holder["issued_dt"] = cb._parse_iso(holder["cap"].issued_at)
    return SimpleNamespace(**holder)


def _cfg(**over):
    base = dict(MUAPI_DRY_RUN=False, MUAPI_API_KEY="k", MUAPI_BASE_URL="https://muapi.test",
               MUAPI_TIMEOUT_SECONDS=5, MUAPI_MAX_POLL_ATTEMPTS=2, MUAPI_POLL_INTERVAL_SECONDS=0)
    base.update(over)
    return SimpleNamespace(**base)


def _clock(dt):
    return lambda: dt


# ---------------------------------------------------------------------------
# fabrication
# ---------------------------------------------------------------------------

class TestFabricatedCapabilityRejected:
    def test_recomputed_checksum_fabrication_rejected(self, issued):
        real = issued.cap
        forged = dataclasses.replace(real, envelope_id="forged-" + real.envelope_id,
                                     capability_id="forged:" + real.capability_id)
        forged = dataclasses.replace(forged, capability_checksum=compute_capability_checksum(forged))
        assert compute_capability_checksum(forged) == forged.capability_checksum
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(forged, expected_schema_version=real.operation_payload_schema_version)

    def test_wholesale_fabrication_no_persisted_state_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", tmp_path / "empty.db")
        db.initialize_schema(tmp_path / "empty.db")
        monkeypatch.setattr(cb, "_DEFAULT_ASSETS_ROOT", tmp_path)
        payload = b'{"prompt":"x"}'
        f = dict(
            capability_id="c", envelope_id="e", envelope_version=1, attempt_id="a", idempotency_key="a",
            lease_owner_id="o", lease_fencing_token=1, provider="muapi", model="m", provider_operation="upscale.video",
            operation_payload_bytes=payload, operation_payload_sha256=hashlib.sha256(payload).hexdigest(),
            operation_payload_schema_version="upscale.video/1.0", max_cost_per_call=Decimal("0.10"),
            currency="USD", budget_reservation_ids=["r"], issued_at="2026-08-28T00:00:00+00:00",
            expires_at="2026-08-28T00:15:00+00:00", capability_checksum="P",
        )
        cap = AdapterInvocationCapability(**f)
        cap = dataclasses.replace(cap, capability_checksum=compute_capability_checksum(cap))
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(cap)


# ---------------------------------------------------------------------------
# TTL / injected clock
# ---------------------------------------------------------------------------

class TestCapabilityTTL:
    def test_valid_inside_window(self, issued):
        ctx = verify_and_consume_capability(issued.cap, clock=_clock(issued.issued_dt + timedelta(seconds=5)))
        assert ctx.capability.capability_id == issued.cap.capability_id

    def test_expired_rejected(self, issued):
        with pytest.raises(CapabilityExpiredError):
            verify_and_consume_capability(
                issued.cap, clock=_clock(cb._parse_iso(issued.cap.expires_at) + timedelta(seconds=1)),
            )

    def test_exact_expiry_boundary_rejected(self, issued):
        with pytest.raises(CapabilityExpiredError):
            verify_and_consume_capability(issued.cap, clock=_clock(cb._parse_iso(issued.cap.expires_at)))

    def test_future_issued_beyond_skew_rejected(self, issued):
        with pytest.raises(CapabilityExpiredError):
            verify_and_consume_capability(
                issued.cap,
                clock=_clock(issued.issued_dt - timedelta(seconds=cb.CLOCK_SKEW_SECONDS + 30)),
            )

    def test_expires_at_strictly_after_issued_at(self, issued):
        assert cb._parse_iso(issued.cap.expires_at) > cb._parse_iso(issued.cap.issued_at)
        forged = dataclasses.replace(issued.cap, expires_at=issued.cap.issued_at)
        forged = dataclasses.replace(forged, capability_checksum=compute_capability_checksum(forged))
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(forged)


# ---------------------------------------------------------------------------
# CURRENT lease / fencing
# ---------------------------------------------------------------------------

class TestCurrentLeaseIsAuthoritative:
    def test_released_lease_makes_capability_unusable(self, issued):
        conn = db.get_connection(issued.db_path)
        assert db.release_lease(
            conn, issued.envelope_id, issued.owner, issued.token, released_at="2099-01-01T00:00:00+00:00",
        )
        conn.close()
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(issued.cap)

    def test_wrong_fencing_token_rejected(self, issued):
        forged = dataclasses.replace(issued.cap, lease_fencing_token=issued.token + 5)
        forged = dataclasses.replace(forged, capability_checksum=compute_capability_checksum(forged))
        with pytest.raises(InvalidCapabilityError):
            verify_and_consume_capability(forged)


# ---------------------------------------------------------------------------
# durable single-use: restart + concurrency
# ---------------------------------------------------------------------------

class TestDurableSingleUse:
    def test_second_verify_same_process_rejected(self, issued):
        verify_and_consume_capability(issued.cap)
        with pytest.raises(CapabilityAlreadyConsumedError):
            verify_and_consume_capability(issued.cap)

    def test_second_verify_fresh_no_cache_rejected_after_restart(self, issued):
        verify_and_consume_capability(issued.cap, consumed_cache=set())
        # brand-new empty cache == process restart; durable receipt claim
        # is the authority.
        with pytest.raises(CapabilityAlreadyConsumedError):
            verify_and_consume_capability(issued.cap, consumed_cache=set())

    def test_two_concurrent_verifies_claim_exactly_once(self, issued):
        results = {}

        def _run(tag):
            try:
                verify_and_consume_capability(issued.cap, consumed_cache=set())
                results[tag] = "ok"
            except CapabilityAlreadyConsumedError:
                results[tag] = "rejected"
            except Exception as exc:  # noqa: BLE001
                results[tag] = type(exc).__name__

        ts = [threading.Thread(target=_run, args=(i,)) for i in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        assert sorted(results.values()) == ["ok", "rejected"]


# ---------------------------------------------------------------------------
# per-identity-family binding
# ---------------------------------------------------------------------------

class TestPerIdentityFamily:
    @pytest.mark.parametrize("field,bad", [
        ("envelope_id", "other-envelope"),
        ("envelope_version", 2),
        ("provider", "openai"),
        ("model", "other-model"),
        ("provider_operation", "generate.video"),
        ("currency", "EUR"),
        ("max_cost_per_call", Decimal("999.00")),
        ("attempt_id", "ep_gate_test:upscale:FOREIGN"),
        ("idempotency_key", "ep_gate_test:upscale:FOREIGN"),
        ("lease_owner_id", "attacker"),
        ("lease_fencing_token", 424242),
        ("budget_reservation_ids", ("someone-elses",)),
        ("issued_at", "2000-01-01T00:00:00+00:00"),
    ])
    def test_single_family_forgery_recomputed_checksum_rejected(self, issued, field, bad):
        forged = dataclasses.replace(issued.cap, **{field: bad})
        forged = dataclasses.replace(forged, capability_checksum=compute_capability_checksum(forged))
        assert compute_capability_checksum(forged) == forged.capability_checksum
        with pytest.raises(InvalidCapabilityError):
            verify_and_consume_capability(forged)

    def test_payload_hash_family_forgery_rejected(self, issued):
        other = b'{"prompt":"totally different"}'
        forged = dataclasses.replace(
            issued.cap, operation_payload_bytes=other,
            operation_payload_sha256=hashlib.sha256(other).hexdigest(),
        )
        forged = dataclasses.replace(forged, capability_checksum=compute_capability_checksum(forged))
        with pytest.raises(InvalidCapabilityError):
            verify_and_consume_capability(forged)


# ---------------------------------------------------------------------------
# sibling adapter: MuapiVideoAdapter
# ---------------------------------------------------------------------------

class TestMuapiVideoAdapterRejectsForgery:
    def test_fabricated_video_capability_rejected_before_network(self, tmp_path, monkeypatch):
        import hypogeum.muapi as muapi_module
        from hypogeum.providers.muapi_video_adapter import MuapiVideoAdapter, encode_video_payload

        monkeypatch.setattr(db, "_DEFAULT_DB_PATH", tmp_path / "empty.db")
        db.initialize_schema(tmp_path / "empty.db")
        monkeypatch.setattr(cb, "_DEFAULT_ASSETS_ROOT", tmp_path)
        seam = MagicMock(side_effect=AssertionError("network reached"))
        monkeypatch.setattr(muapi_module, "_submit_http", seam)

        payload = encode_video_payload(
            prompt="assembled", model="veo3.1-lite-text-to-video", aspect_ratio="16:9",
            duration_seconds=8.0, reference_image_url=None, scene_id="s01", short_id=None,
            episode_id="ep_x", output_dir=str(tmp_path),
        )
        f = dict(
            capability_id="forge:v", envelope_id="forge-env", envelope_version=1, attempt_id="a",
            idempotency_key="a", lease_owner_id="o", lease_fencing_token=1, provider="muapi",
            model="veo3.1-lite-text-to-video", provider_operation="video.text_to_video",
            operation_payload_bytes=payload, operation_payload_sha256=hashlib.sha256(payload).hexdigest(),
            operation_payload_schema_version="video.text_to_video/1.0", max_cost_per_call=Decimal("0.39"),
            currency="USD", budget_reservation_ids=("r",), issued_at="2026-08-28T00:00:00+00:00",
            expires_at="2026-08-28T00:15:00+00:00", capability_checksum="P",
        )
        cap = AdapterInvocationCapability(**f)
        cap = dataclasses.replace(cap, capability_checksum=compute_capability_checksum(cap))
        with pytest.raises(CapabilityAuthenticityError):
            MuapiVideoAdapter().invoke(cap)
        seam.assert_not_called()


# ---------------------------------------------------------------------------
# helpers for the post-plan weekly-binding + I/O-entry tests
# ---------------------------------------------------------------------------

def _weekly_rows(db_path):
    conn = db.get_connection(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM budget_reservations WHERE scope_kind='channel_weekly'"
        )]
    finally:
        conn.close()


def _exec_sql(db_path, sql, params=()):
    conn = db.get_connection(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _receipts_dir(issued):
    from hypogeum.provider_receipt import resolve_episode_receipts_root
    return resolve_episode_receipts_root(issued.tmp_path, _EP)


def _canon_plan_file(issued):
    canon = issued.tmp_path / "episodes" / _EP
    return next(canon.glob("production_plan_*.json"))


def _swap_canonical_plan(issued, *, maximum):
    """Replace the canonical Production Plan with a DIFFERENT one
    (different maximum_bounded_cost_usd -> different content_hash).
    Returns the new plan's content_hash."""
    from test_plan_lifecycle_canonical_authority import _make_plan
    from hypogeum.production_plan import persist_production_plan
    canon = issued.tmp_path / "episodes" / _EP
    for f in canon.glob("production_plan_*.json"):
        f.unlink()
    plan_b = _make_plan(_EP, "test_channel", maximum)
    persist_production_plan(plan_b, canon)
    return plan_b.content_hash


def _seed_weekly_reservation(issued, *, plan_hash, amount):
    import hypogeum.weekly_budget as weekly_budget
    from hypogeum.plan_reservation import plan_weekly_idempotency_key
    conn = db.get_connection(issued.db_path)
    try:
        weekly_budget.ensure_channel_weekly_reservation(
            conn, channel_id="test_channel", amount=Decimal(str(amount)), currency="USD",
            idempotency_key=plan_weekly_idempotency_key("test_channel", _EP, plan_hash),
            now_utc=issued.issued_dt,
        )
        conn.commit()
    finally:
        conn.close()


def _strip_envelope_plan_binding(issued):
    """Rewrite the persisted execution_envelopes row so its
    audit_metadata no longer carries production_plan_content_hash
    (recomputing the storage checksum so get_execution_envelope still
    accepts it -- audit_metadata is excluded from envelope_id)."""
    import hashlib
    import json as _json
    conn = db.get_connection(issued.db_path)
    try:
        row = conn.execute(
            "SELECT canonical_payload_bytes FROM execution_envelopes WHERE envelope_id = ?",
            (issued.envelope_id,),
        ).fetchone()
        data = _json.loads(row["canonical_payload_bytes"])
        data.get("audit_metadata", {}).pop("production_plan_content_hash", None)
        new_bytes = _json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        conn.execute(
            "UPDATE execution_envelopes SET canonical_payload_bytes = ?, canonical_payload_checksum = ? "
            "WHERE envelope_id = ?",
            (new_bytes, hashlib.sha256(new_bytes).hexdigest(), issued.envelope_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# BLOCKER 1 — mandatory post-plan plan + CHANNEL_WEEKLY binding
# ---------------------------------------------------------------------------

class TestPostPlanWeeklyBinding:
    def test_valid_weekly_reservation_passes(self, issued):
        rows = _weekly_rows(issued.db_path)
        assert len(rows) == 1 and rows[0]["status"] in ("reserved", "committed")
        ctx = verify_and_consume_capability(issued.cap)
        assert ctx.capability.capability_id == issued.cap.capability_id

    def test_missing_weekly_reservation_rejected(self, issued):
        _exec_sql(issued.db_path, "DELETE FROM budget_reservations WHERE scope_kind='channel_weekly'")
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(issued.cap)

    def test_released_weekly_reservation_rejected(self, issued):
        _exec_sql(issued.db_path,
                  "UPDATE budget_reservations SET status='released' WHERE scope_kind='channel_weekly'")
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(issued.cap)

    def test_wrong_amount_weekly_reservation_rejected(self, issued):
        _exec_sql(issued.db_path,
                  "UPDATE budget_reservations SET reserved_amount='999.99' WHERE scope_kind='channel_weekly'")
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(issued.cap)

    def test_foreign_channel_weekly_reservation_rejected(self, issued):
        _exec_sql(issued.db_path,
                  "UPDATE budget_reservations SET channel_id='some_other_channel' WHERE scope_kind='channel_weekly'")
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(issued.cap)

    def test_foreign_week_reservation_rejected(self, issued):
        # The reservation names a DIFFERENT ISO-week window than the
        # authoritative current one the verifier computes from the
        # channel's timezone + the (in-TTL) clock.
        _exec_sql(issued.db_path,
                  "UPDATE budget_reservations SET scope_ref='test_channel:2020-W01' "
                  "WHERE scope_kind='channel_weekly'")
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(issued.cap)

    def test_substituted_wrong_plan_reservation_rejected(self, issued):
        # The reservation is keyed to a DIFFERENT Production Plan hash than
        # the authoritative canonical plan -> the verifier's computed
        # plan-weekly idempotency_key does not match any current-week row.
        _exec_sql(issued.db_path,
                  "UPDATE budget_reservations "
                  "SET idempotency_key='plan-weekly:test_channel:ep_gate_test:0000substituted0000' "
                  "WHERE scope_kind='channel_weekly'")
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(issued.cap)

    def test_verify_never_creates_a_second_weekly_reservation(self, issued):
        before = _weekly_rows(issued.db_path)
        verify_and_consume_capability(issued.cap)
        after = _weekly_rows(issued.db_path)
        assert [r["reservation_id"] for r in before] == [r["reservation_id"] for r in after]

    # --- the task's exact 6 canonical-plan-identity adversarial tests ---

    def test_1_plan_a_everything_passes(self, issued):
        # Plan A envelope/capability + Plan A reservation + Plan A canonical plan.
        ctx = verify_and_consume_capability(issued.cap)
        assert ctx.capability.capability_id == issued.cap.capability_id

    def test_2_plan_a_capability_rejected_when_canonical_becomes_plan_b(self, issued):
        # Plan A envelope/capability stays persisted; canonical plan becomes
        # Plan B; a COMPLETELY VALID current-week Plan B reservation exists
        # (Plan A's own reservation released as it would be on supersede).
        _exec_sql(issued.db_path, "DELETE FROM budget_reservations WHERE scope_kind='channel_weekly'")
        plan_b_hash = _swap_canonical_plan(issued, maximum=12.00)
        _seed_weekly_reservation(issued, plan_hash=plan_b_hash, amount=12.00)
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(issued.cap)

    def test_3_envelope_without_exact_plan_binding_rejected(self, issued):
        _strip_envelope_plan_binding(issued)  # audit_metadata plan hash removed
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(issued.cap)

    def test_4_bound_hash_matches_but_reservation_is_for_another_plan_rejected(self, issued):
        _exec_sql(issued.db_path,
                  "UPDATE budget_reservations "
                  "SET idempotency_key='plan-weekly:test_channel:ep_gate_test:0000another-plan0000' "
                  "WHERE scope_kind='channel_weekly'")
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(issued.cap)

    def test_5_bound_and_reservation_both_match_canonical_passes(self, issued):
        ctx = verify_and_consume_capability(issued.cap)
        assert ctx.capability.capability_id == issued.cap.capability_id

    def test_6_noncanonical_same_hash_plan_copy_still_rejected(self, issued, monkeypatch):
        # The existing canonical-path rule: a same-hash plan copy at a
        # noncanonical directory is INCONSISTENT_PATH, never an authority.
        import shutil
        from hypogeum.plan_reservation import (
            PlanReservationInconsistentPathError, ensure_verified_plan_weekly_reservation,
        )
        canon = issued.tmp_path / "episodes" / _EP
        copy_dir = issued.tmp_path / "elsewhere"
        copy_dir.mkdir()
        shutil.copy(_canon_plan_file(issued), copy_dir / _canon_plan_file(issued).name)
        with pytest.raises(PlanReservationInconsistentPathError):
            ensure_verified_plan_weekly_reservation(
                channel_id="test_channel", episode_id=_EP, config=SimpleNamespace(),
                log=SimpleNamespace(info=lambda *a, **k: None), output_dir=copy_dir,
            )

    def test_verify_never_creates_a_second_weekly_reservation_recursive(self, issued, monkeypatch):
        from hypogeum.provider_receipt import WriteAheadReceiptStore
        from hypogeum.providers.muapi_upscale_adapter import MuapiUpscaleAdapter
        before = _weekly_rows(issued.db_path)
        up_resp = MagicMock(status_code=200); up_resp.json.return_value = {"url": "https://cdn/u.mp4"}
        sub_resp = MagicMock(status_code=200)
        sub_resp.json.return_value = {"request_id": "r1", "status": "processing", "cost": {"amount_usd": 0.07}}
        poll_resp = MagicMock(status_code=200)
        poll_resp.json.return_value = {"status": "completed", "outputs": ["https://cdn/out.mp4"]}
        dl_resp = MagicMock(status_code=200, content=b"\x00" * 64)
        with patch("requests.post", side_effect=[up_resp, sub_resp]), \
             patch("requests.get", side_effect=[poll_resp, dl_resp]), \
             patch("hypogeum.finance.record_cost"):
            MuapiUpscaleAdapter().invoke(issued.cap)
        assert _weekly_rows(issued.db_path) == before

    def test_pre_plan_no_canonical_plan_skips_weekly_binding(self, issued):
        # Remove the canonical plan file -> classify_plan_lifecycle -> PRE_PLAN
        # -> weekly binding is NOT required (openrouter/perplexity case).
        canon = issued.tmp_path / "episodes" / _EP
        for f in canon.glob("production_plan_*.json"):
            f.unlink()
        _exec_sql(issued.db_path, "DELETE FROM budget_reservations WHERE scope_kind='channel_weekly'")
        ctx = verify_and_consume_capability(issued.cap)
        assert ctx.capability.capability_id == issued.cap.capability_id


# ---------------------------------------------------------------------------
# BLOCKER 2 — authoritative receipt trail, not caller-selectable
# ---------------------------------------------------------------------------

class TestAuthoritativeReceiptLocation:
    def test_verify_takes_no_receipt_store_argument(self, issued):
        with pytest.raises(TypeError):
            verify_and_consume_capability(issued.cap, receipt_store=object())

    def test_identical_trail_at_a_foreign_path_is_never_consulted(self, issued, monkeypatch):
        import shutil
        auth = issued.tmp_path / "episodes"
        foreign = issued.tmp_path / "attacker_copy"
        shutil.copytree(auth, foreign / "episodes")
        # remove the authoritative trail; leave the byte-identical copy
        shutil.rmtree(auth)
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(issued.cap)
        # even if the caller could point _DEFAULT_ASSETS_ROOT at the copy,
        # that is a deployment constant, not caller input -- but prove the
        # copy IS a valid trail (so the failure above is purely location):
        monkeypatch.setattr(cb, "_DEFAULT_ASSETS_ROOT", foreign)
        ctx = verify_and_consume_capability(issued.cap)
        assert ctx.capability.capability_id == issued.cap.capability_id

    # --- the task's exact 6 receipt-substitution adversarial tests ---

    def test_1_real_canonical_receipt_directory_passes(self, issued):
        ctx = verify_and_consume_capability(issued.cap)
        assert ctx.capability.capability_id == issued.cap.capability_id

    def test_2_canonical_receipt_path_symlinked_to_empty_target_rejected(self, issued):
        auth_ep = issued.tmp_path / "episodes" / _EP
        empty = issued.tmp_path / "empty_target"
        empty.mkdir()
        auth_ep.rename(issued.tmp_path / "episodes" / f"{_EP}__real")
        auth_ep.symlink_to(empty, target_is_directory=True)
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(issued.cap)

    def test_3_canonical_receipt_path_symlinked_to_populated_byte_identical_trail_rejected(self, issued):
        import shutil
        auth_ep = issued.tmp_path / "episodes" / _EP
        real = issued.tmp_path / "episodes" / f"{_EP}__real"
        auth_ep.rename(real)
        foreign = issued.tmp_path / "foreign_populated"
        shutil.copytree(real, foreign)  # byte-identical valid trail
        auth_ep.symlink_to(foreign, target_is_directory=True)
        # sanity: the foreign copy IS a complete, valid trail
        assert list(foreign.glob("receipts/*/*.json"))
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(issued.cap)

    def test_4_intermediate_episodes_dir_symlinked_to_populated_foreign_rejected(self, issued):
        import shutil
        episodes = issued.tmp_path / "episodes"
        real = issued.tmp_path / "episodes__real"
        episodes.rename(real)
        foreign = issued.tmp_path / "foreign_episodes"
        shutil.copytree(real, foreign)
        episodes.symlink_to(foreign, target_is_directory=True)
        assert list((foreign / _EP).glob("receipts/*/*.json"))
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(issued.cap)

    def test_5_copied_same_content_trail_at_ordinary_noncanonical_path_ignored(self, issued):
        import shutil
        auth = issued.tmp_path / "episodes"
        shutil.copytree(auth, issued.tmp_path / "ordinary_copy" / "episodes")
        shutil.rmtree(auth)  # authoritative location now empty; copy is an ordinary dir
        with pytest.raises(CapabilityAuthenticityError):
            verify_and_consume_capability(issued.cap)

    @pytest.mark.parametrize("shape", ["bare", "episodes", "episode", "receipts"])
    def test_6_every_gate_callsite_root_shape_normalizes_to_one_authoritative_path(self, tmp_path, shape):
        # Every production gate call-site builds WriteAheadReceiptStore
        # from one of these root shapes; all must normalize to the SAME
        # authoritative receipts path capability_boundary reads from --
        # proven directly here, not via the autouse fixture.
        from hypogeum.provider_receipt import resolve_episode_receipts_root
        base = tmp_path / "assets"
        ep = "ep_norm"
        authoritative = resolve_episode_receipts_root(base, ep)  # capability_boundary's own resolution
        shapes = {
            "bare": base,                                 # openrouter/perplexity/upscale gate: _ASSETS_ROOT
            "episodes": base / "episodes",
            "episode": base / "episodes" / ep,            # muapi video/image/thumbnail/voice gate: output_dir
            "receipts": base / "episodes" / ep / "receipts",
        }
        assert resolve_episode_receipts_root(shapes[shape], ep) == authoritative


# ---------------------------------------------------------------------------
# BLOCKER 3 — durable atomic provider-I/O entry claim
# ---------------------------------------------------------------------------

class TestProviderIoEntryClaim:
    def _ctx(self, issued):
        return verify_and_consume_capability(issued.cap)

    def test_sequential_second_io_claim_rejected(self, issued):
        ctx = self._ctx(issued)
        ctx.claim_io_entry()
        with pytest.raises(CapabilityAlreadyConsumedError):
            ctx.claim_io_entry()

    def test_concurrent_invocations_enter_provider_io_at_most_once(self, issued):
        # Two independent invocations (== two processes) each verify + claim
        # I/O entry. The consumption claim upstream already serialises them;
        # the I/O-entry claim is the deepest additional durable gate.
        results = {}

        def _run(tag):
            try:
                c = verify_and_consume_capability(issued.cap, consumed_cache=set())
                c.claim_io_entry()
                results[tag] = "entered_io"
            except CapabilityAlreadyConsumedError:
                results[tag] = "rejected"
            except Exception as exc:  # noqa: BLE001
                results[tag] = type(exc).__name__

        ts = [threading.Thread(target=_run, args=(i,)) for i in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        assert sorted(results.values()) == ["entered_io", "rejected"]

    def test_reconstructed_context_cannot_reuse_the_io_claim(self, issued):
        ctx = self._ctx(issued)
        ctx.claim_io_entry()
        rebuilt = cb.VerifiedInvocationContext(
            capability=ctx.capability, envelope=ctx.envelope, operation_payload=ctx.operation_payload,
            execution_id=ctx.execution_id, idempotency_key=ctx.idempotency_key,
            claim_nonce=ctx.claim_nonce, _assets_root=ctx._assets_root, _db_path=ctx._db_path,
            _clock=ctx._clock,
        )
        with pytest.raises(CapabilityAlreadyConsumedError):
            rebuilt.claim_io_entry()

    def test_fresh_verify_after_io_claim_is_spent(self, issued):
        ctx = self._ctx(issued)
        ctx.claim_io_entry()
        # process restart == fresh verify with an empty cache
        with pytest.raises(CapabilityAlreadyConsumedError):
            verify_and_consume_capability(issued.cap, consumed_cache=set())

    def test_crash_after_io_claim_never_silently_retryable(self, issued):
        ctx = self._ctx(issued)
        ctx.claim_io_entry()
        # "crash": nothing else written. Neither a retained context nor a
        # fresh verify may re-enter provider I/O.
        with pytest.raises(CapabilityAlreadyConsumedError):
            ctx.claim_io_entry()
        with pytest.raises(CapabilityAlreadyConsumedError):
            verify_and_consume_capability(issued.cap)

    def test_io_claim_does_not_touch_budget_reservations(self, issued):
        ctx = self._ctx(issued)
        before = _weekly_rows(issued.db_path)
        ctx.claim_io_entry()
        after = _weekly_rows(issued.db_path)
        assert before == after

    def test_upscale_real_invoke_claims_io_entry_exactly_once(self, issued, monkeypatch):
        from hypogeum.provider_receipt import WriteAheadReceiptStore
        from hypogeum.providers.muapi_upscale_adapter import MuapiUpscaleAdapter

        up_resp = MagicMock(status_code=200)
        up_resp.json.return_value = {"url": "https://cdn/u.mp4"}
        sub_resp = MagicMock(status_code=200)
        sub_resp.json.return_value = {"request_id": "r1", "status": "processing", "cost": {"amount_usd": 0.07}}
        poll_resp = MagicMock(status_code=200)
        poll_resp.json.return_value = {"status": "completed", "outputs": ["https://cdn/out.mp4"]}
        dl_resp = MagicMock(status_code=200, content=b"\x00" * 64)
        with patch("requests.post", side_effect=[up_resp, sub_resp]), \
             patch("requests.get", side_effect=[poll_resp, dl_resp]), \
             patch("hypogeum.finance.record_cost"):
            res = MuapiUpscaleAdapter().invoke(issued.cap)
        assert res.outcome == "accepted"
        events = WriteAheadReceiptStore(issued.tmp_path).read_events(_EP, issued.cap.idempotency_key)
        io_claims = [e for e in events if e.evidence.get("phase") == cb._IO_ENTRY_PHASE]
        assert len(io_claims) == 1
