"""
hypogeum/providers/muapi_upscale_adapter.py — Real MUAPI AI Video
Upscaler adapter (upscale.video domain).

Implements the exact same protocol
hypogeum.providers.fake_provider_adapter.FakeProviderAdapter defines,
mirroring hypogeum.providers.muapi_video_adapter.MuapiVideoAdapter's
own structure. THE raw MUAPI upscale HTTP sequence (upload -> submit ->
poll -> download -> record cost) lives in this module's private
_execute_raw_upscale_http() and NOWHERE ELSE -- there is no
provider-level method any caller can invoke to reach that network
boundary. The only path to it is MuapiUpscaleAdapter.invoke(), and only
after invoke() has proven, against authoritative persisted state, that
the AdapterInvocationCapability it was handed was actually issued by a
successful hypogeum.submission_gate.submit() execution:

  * hypogeum.authorization_store.get_execution_envelope() must return a
    durably-stored, integrity-verified GenerationExecutionEnvelope for
    (capability.envelope_id, capability.envelope_version), and every
    identity family on the capability (provider, model, operation,
    payload hash, max cost, currency, attempt/idempotency identity,
    channel) must match it;
  * hypogeum.authorization_store.get_authorization_record() must return
    an AUTHORIZED child-authorization record bound to that same
    envelope and derived from a real signed run grant
    (parent_grant_id + grant_reservation_id evidence);
  * the durable hypogeum.provider_receipt.WriteAheadReceiptStore trail
    for (episode_id, idempotency_key) must already contain the gate's
    own ABOUT_TO_SUBMIT (event_seq 0) and SUBMISSION_ATTEMPT_STARTED
    (event_seq 1) write-ahead records, matching the capability, and
    classify_recovery_state() must be exactly
    SUBMITTING_NO_ACKNOWLEDGEMENT (never AUTHORIZED_NO_ATTEMPT -- a
    forged direct call has no trail -- and never an already-acknowledged
    or terminal state -- a replay);
  * the persisted execution envelope's audit_metadata must bind the
    verified Production Plan content_hash and the plan-weekly
    reservation id, and load_production_plan() from the CANONICAL
    episode directory must still produce that exact content_hash for
    that exact channel;
  * lease owner + fencing token must match the gate's own
    SUBMISSION_ATTEMPT_STARTED evidence, the capability's
    budget_reservation_ids must match the gate's own ABOUT_TO_SUBMIT
    evidence, and issued_at must equal that ABOUT_TO_SUBMIT's timestamp.

None of this trusts the recomputable capability_checksum as proof of
authenticity -- the checksum is integrity only. A completely fabricated
capability whose checksum is honestly recomputed with the same public
function is rejected here because no authoritative persisted issuance
backs it.

Single-use is durable and restart-safe: before the network boundary,
invoke() atomically appends its own consumption-claim receipt event
(a per-invocation nonce, os.link create-if-absent). A second invocation
-- concurrent, or after a process restart with a brand-new adapter
instance -- finds either that claim (or a later ACCEPTED/REJECTED the
gate wrote) already durable and fails closed before provider I/O. The
in-memory _consumed_capability_ids set is only a same-process fast
path, never the authority.

operation_payload_schema_version "upscale.video/1.0" -- the exact JSON
shape operation_payload_bytes must decode to:
{
  "source_path": str, "dest_path": str, "target_width": int,
  "target_height": int, "target_resolution_label": str,
  "episode_id": str, "scene_id": str
}
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import hypogeum.capability_boundary as capability_boundary
from hypogeum.capability_boundary import (
    CapabilityAuthenticityError,
    CapabilityExpiredError,
    VerifiedInvocationContext,
)
from hypogeum.providers.fake_provider_adapter import (
    AdapterInvocationCapability,
    AdapterResult,
    CapabilityAlreadyConsumedError,
    InvalidCapabilityError,
    RawPromptRejectedError,
    compute_capability_checksum,
)

__all__ = [
    "SCHEMA_VERSION",
    "MuapiUpscaleAdapter",
    "CapabilityAuthenticityError",
    "CapabilityExpiredError",
    "encode_upscale_payload",
    "decode_upscale_payload",
]

SCHEMA_VERSION = "upscale.video/1.0"

_REQUIRED_PAYLOAD_FIELDS = (
    "source_path", "dest_path", "target_width", "target_height",
    "target_resolution_label", "episode_id", "scene_id",
)


def encode_upscale_payload(
    *, source_path: str, dest_path: str, target_width: int, target_height: int,
    target_resolution_label: str, episode_id: str, scene_id: str,
) -> bytes:
    payload = {
        "source_path": source_path, "dest_path": dest_path, "target_width": target_width,
        "target_height": target_height, "target_resolution_label": target_resolution_label,
        "episode_id": episode_id, "scene_id": scene_id,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_upscale_payload(payload_bytes: bytes) -> dict:
    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidCapabilityError(f"operation_payload_bytes is not valid JSON: {exc}") from exc
    missing = [f for f in _REQUIRED_PAYLOAD_FIELDS if f not in payload]
    if missing:
        raise InvalidCapabilityError(f"upscale payload missing required field(s): {missing}")
    return payload


class MuapiUpscaleAdapter:
    """The one sanctioned real adapter for provider="muapi",
    provider_operation="upscale.video"."""

    def __init__(self):
        self.invocation_count = 0
        self._consumed_capability_ids: set = set()
        self.received_capabilities: list = []

    def invoke(
        self, capability: AdapterInvocationCapability, *, mode: "str | None" = None,
        on_provider_acknowledged: "object | None" = None,
    ) -> AdapterResult:
        # on_provider_acknowledged: accepted for protocol uniformity with
        # every other registered adapter -- this domain has no
        # intermediate submit-then-poll acknowledgement phase, so it is
        # intentionally never called here.
        if isinstance(capability, str):
            raise RawPromptRejectedError(
                "MuapiUpscaleAdapter.invoke() refuses a raw string prompt -- "
                "an AdapterInvocationCapability minted by the submission gate is required"
            )

        # Authoritative verification (checksum integrity + persisted
        # envelope + signed-grant child authorization + write-ahead
        # receipt trail + CURRENT lease + capability TTL + plan-weekly
        # reservation identity/state) AND durable, atomic, restart- and
        # concurrency-safe single-use consumption -- all owned by
        # hypogeum.capability_boundary, identical for every adapter. The
        # in-memory _consumed_capability_ids set is a same-process cache
        # only, never the single-use authority.
        ctx = capability_boundary.verify_and_consume_capability(
            capability, expected_schema_version=SCHEMA_VERSION,
            consumed_cache=self._consumed_capability_ids,
        )

        self.invocation_count += 1
        self.received_capabilities.append(capability)

        return self._invoke_real(ctx)

    def _invoke_real(self, ctx: VerifiedInvocationContext) -> AdapterResult:
        import hashlib
        import time

        from hypogeum.config import Config

        # ctx.assert_live() re-checks, against durable state, that this
        # context's consumption claim is still the head of the receipt
        # trail and the capability has not expired -- so a hand-built
        # context (bypassing capability_boundary.verify_and_consume_
        # capability) cannot be used to reach the network here.
        ctx.assert_live()
        capability = ctx.capability

        t0 = time.time()
        from hypogeum.workspace_keys import config_for_channel  # Phase 2: owner's keys
        result = _execute_raw_upscale_http(
            ctx=ctx, config=config_for_channel(Config(), ctx.envelope.channel_id, ctx._db_path), t0=t0)

        if not result.success:
            return AdapterResult(
                outcome="rejected", provider_job_id=result.request_id, provider_request_id=result.request_id,
                charged_amount=None, response_checksum=None,
                evidence={"failure_reason": result.failure_reason or "enhancement_failed"},
            )

        response_evidence = {
            "provider_job_id": result.request_id,
            "output_path_sha256": hashlib.sha256(str(result.output_path).encode()).hexdigest(),
        }
        response_checksum = hashlib.sha256(json.dumps(response_evidence, sort_keys=True).encode()).hexdigest()

        charged_amount = (
            min(Decimal(str(result.cost_usd)), capability.max_cost_per_call)
            if result.cost_usd is not None else capability.max_cost_per_call
        )

        return AdapterResult(
            outcome="accepted", provider_job_id=result.request_id, provider_request_id=result.request_id,
            charged_amount=charged_amount, response_checksum=response_checksum,
            evidence={
                "output_path": str(result.output_path), "cost_usd": result.cost_usd,
                "generation_time_s": result.generation_time_s, "provider": result.provider,
                "model": result.model, "provider_version": result.provider_version,
            },
        )


def _execute_raw_upscale_http(*, ctx: VerifiedInvocationContext, config, t0: float):
    """
    RAW MUAPI upscale HTTP sequence: upload -> submit -> poll ->
    download -> record cost. Its ONLY input is a
    hypogeum.capability_boundary.VerifiedInvocationContext, and the FIRST
    thing it does is ctx.assert_live() -- an independent re-verification
    against authoritative durable state (the context's consumption claim
    must still be the head of the receipt trail; the capability must not
    have expired). A caller that constructs a VerifiedInvocationContext
    by hand (skipping verify_and_consume_capability) is rejected here
    before any network I/O -- type alone is not authorization. Every HTTP
    argument comes from the capability's own gate-bound, checksum-
    and envelope-bound operation_payload_bytes.
    """
    import time

    import requests

    from hypogeum.providers.enhancement import EnhancementResult
    from hypogeum.providers.muapi_video_upscaler import (
        MODEL_NAME, PROVIDER_NAME, PROVIDER_VERSION,
        _RESULT_PATH_TMPL, _SUBMIT_PATH, _TERMINAL_FAILURE_STATUSES,
        _TERMINAL_SUCCESS_STATUSES, _UPLOAD_PATH,
    )

    if not isinstance(ctx, VerifiedInvocationContext):
        raise CapabilityAuthenticityError(
            "_execute_raw_upscale_http requires a hypogeum.capability_boundary.VerifiedInvocationContext"
        )
    # Independent re-verification against authoritative durable state --
    # a hand-constructed context (bypassing verify_and_consume_capability)
    # is rejected here before any network I/O.
    ctx.assert_live()

    capability = ctx.capability
    payload = decode_upscale_payload(capability.operation_payload_bytes)
    source_path = Path(payload["source_path"])
    dest_path = Path(payload["dest_path"])
    target_resolution_label = payload["target_resolution_label"]
    episode_id = payload["episode_id"]

    def _fail(reason: str, *, request_id=None, cost_usd=None) -> EnhancementResult:
        return EnhancementResult(
            success=False, request_id=request_id, provider=PROVIDER_NAME, model=MODEL_NAME,
            provider_version=PROVIDER_VERSION, cost_usd=cost_usd, failure_reason=reason,
            generation_time_s=time.time() - t0,
        )

    if not getattr(config, "MUAPI_API_KEY", None):
        return _fail("MUAPI_API_KEY not configured")

    headers = {"x-api-key": config.MUAPI_API_KEY}
    base_url = config.MUAPI_BASE_URL

    # Durable atomic provider-I/O entry gate, at the deepest controlled
    # boundary immediately before the first paid HTTP call. Exactly one
    # caller (this attempt, this process) can ever win; a second
    # sequential call, a concurrent call, a reconstructed context, or a
    # fresh adapter after restart all fail closed here or earlier.
    ctx.claim_io_entry()

    try:
        with source_path.open("rb") as fh:
            up_resp = requests.post(
                f"{base_url}/{_UPLOAD_PATH}", headers=headers,
                files={"file": (source_path.name, fh, "video/mp4")},
                timeout=config.MUAPI_TIMEOUT_SECONDS,
            )
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        return _fail(f"upload_failed: {exc}")

    if up_resp.status_code not in (200, 201):
        return _fail(f"upload_http_{up_resp.status_code}")
    try:
        up_json = up_resp.json()
    except Exception:
        return _fail("upload_response_not_json")

    hosted_url = (
        up_json.get("url") or up_json.get("file_url") or up_json.get("upload_url")
        or up_json.get("data", {}).get("url", "")
    )
    if not hosted_url:
        return _fail("upload_response_missing_url")

    try:
        sub_resp = requests.post(
            f"{base_url}/{_SUBMIT_PATH}",
            headers={**headers, "Content-Type": "application/json"},
            json={"video_url": hosted_url, "resolution": target_resolution_label, "copy_audio": True},
            timeout=30,
        )
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        return _fail(f"submit_failed: {exc}")

    if sub_resp.status_code != 200:
        return _fail(f"submit_http_{sub_resp.status_code}")

    sub_json = sub_resp.json()
    request_id = sub_json.get("request_id") or sub_json.get("id")
    cost_usd = (sub_json.get("cost") or {}).get("amount_usd")
    if not request_id:
        return _fail("submit_response_missing_request_id", request_id=request_id, cost_usd=cost_usd)

    result_url = f"{base_url}/{_RESULT_PATH_TMPL.format(request_id=request_id)}"
    max_attempts = getattr(config, "MUAPI_MAX_POLL_ATTEMPTS", 24)
    poll_interval = getattr(config, "MUAPI_POLL_INTERVAL_SECONDS", 5)
    terminal_body = None
    for _ in range(max_attempts):
        try:
            poll_resp = requests.get(result_url, headers=headers, timeout=30)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            time.sleep(poll_interval)
            continue
        try:
            body = poll_resp.json()
        except Exception:
            time.sleep(poll_interval)
            continue
        status = None
        if poll_resp.status_code == 200:
            status = body.get("status")
        elif poll_resp.status_code == 400:
            status = (body.get("detail") or {}).get("status")
        if status in _TERMINAL_SUCCESS_STATUSES or status in _TERMINAL_FAILURE_STATUSES:
            terminal_body = body
            break
        time.sleep(poll_interval)

    if terminal_body is None:
        return _fail("poll_timeout_no_terminal_status", request_id=request_id, cost_usd=cost_usd)

    status = terminal_body.get("status") or (terminal_body.get("detail") or {}).get("status")
    if status not in _TERMINAL_SUCCESS_STATUSES:
        err = terminal_body.get("error") or (terminal_body.get("detail") or {}).get("error")
        return _fail(f"provider_failed: {err or status}", request_id=request_id, cost_usd=cost_usd)

    outputs = terminal_body.get("outputs", [])
    video_url = (outputs[0] if isinstance(outputs[0], str) else outputs[0].get("url", "")) if outputs else ""
    if not video_url:
        return _fail("completed_with_no_output_url", request_id=request_id, cost_usd=cost_usd)

    try:
        dl = requests.get(video_url, timeout=config.MUAPI_TIMEOUT_SECONDS)
        if dl.status_code != 200:
            return _fail(f"download_http_{dl.status_code}", request_id=request_id, cost_usd=cost_usd)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(dl.content)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        return _fail(f"download_failed: {exc}", request_id=request_id, cost_usd=cost_usd)
    except OSError as exc:
        # The job already completed (and was charged) -- a local write
        # failure (2026-10-05: "[Errno 28] No space left on device") used to
        # escape as an exception, losing request_id and the spend record.
        try:
            dest_path.unlink()
        except OSError:
            pass
        import hypogeum.finance as _finance  # noqa: PLC0415
        _finance.record_cost(
            provider=PROVIDER_NAME, model=MODEL_NAME, call_type="source_enhancement",
            cost_usd=cost_usd or 0.0, episode_id=episode_id,
        )
        return _fail(f"local_write_failed_after_charge: {exc}", request_id=request_id, cost_usd=cost_usd)

    import hypogeum.finance as _finance  # noqa: PLC0415

    _finance.record_cost(
        provider=PROVIDER_NAME, model=MODEL_NAME, call_type="source_enhancement",
        cost_usd=cost_usd or 0.0, episode_id=episode_id,
    )

    return EnhancementResult(
        success=True, request_id=request_id, output_path=dest_path,
        provider=PROVIDER_NAME, model=MODEL_NAME, provider_version=PROVIDER_VERSION,
        cost_usd=cost_usd, generation_time_s=time.time() - t0,
    )
