"""
hypogeum/providers/muapi_video_adapter.py — Real MUAPI video-generation
adapter (video.text_to_video domain).

Implements the exact same protocol
hypogeum.providers.fake_provider_adapter.FakeProviderAdapter defines:
invoke(capability, ...) -> AdapterResult, single-use, rejects anything
but a valid, checksum-verified AdapterInvocationCapability. This is the
ONLY code path in the entire repository permitted to reach a real MUAPI
video-generation HTTP endpoint through this adapter's registration --
resolved and instantiated exclusively by hypogeum.submission_gate.
submit() via hypogeum.adapter_registry, never imported or constructed
by any domain caller.

Deliberately wraps hypogeum.muapi's existing, already-tested submit/
poll/download/validate functions (_submit_http, _poll_until_complete,
_download_file, _validate_video_file) rather than re-implementing
their retry/timeout/error-handling logic here -- those functions are
unchanged by this commit; this adapter is a thin translation layer
between the capability/AdapterResult protocol and that existing,
battle-tested HTTP chain.

operation_payload_schema_version "video.text_to_video/1.0" -- the
exact JSON shape operation_payload_bytes must decode to:
{
  "prompt": str, "model": str, "aspect_ratio": str,
  "duration_seconds": float, "reference_image_url": str | None,
  "scene_id": str, "short_id": str | None, "episode_id": str,
  "output_dir": str
}
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

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
    "MuapiVideoAdapter",
    "encode_video_payload",
    "decode_video_payload",
]

SCHEMA_VERSION = "video.text_to_video/1.0"

_REQUIRED_PAYLOAD_FIELDS = (
    "prompt", "model", "aspect_ratio", "duration_seconds", "scene_id", "episode_id", "output_dir",
)


def encode_video_payload(
    *, prompt: str, model: str, aspect_ratio: str, duration_seconds: float,
    reference_image_url: "str | None", scene_id: str, short_id: "str | None",
    episode_id: str, output_dir: str,
) -> bytes:
    """Canonical JSON encoding of the video domain's typed payload --
    the exact bytes hypogeum.muapi.submit_scene()'s grant-derived path
    puts into GenerationExecutionEnvelope.operation_payload_content
    (encoded to bytes by hypogeum.submission_gate._mint_capability())."""
    payload = {
        "prompt": prompt, "model": model, "aspect_ratio": aspect_ratio,
        "duration_seconds": duration_seconds, "reference_image_url": reference_image_url,
        "scene_id": scene_id, "short_id": short_id, "episode_id": episode_id, "output_dir": output_dir,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_video_payload(payload_bytes: bytes) -> dict:
    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidCapabilityError(f"operation_payload_bytes is not valid JSON: {exc}") from exc
    missing = [f for f in _REQUIRED_PAYLOAD_FIELDS if f not in payload]
    if missing:
        raise InvalidCapabilityError(f"video payload missing required field(s): {missing}")
    return payload


class MuapiVideoAdapter:
    """The one sanctioned real adapter for provider="muapi",
    provider_operation="video.text_to_video". Mirrors FakeProviderAdapter's
    single-use / checksum-verification discipline exactly; only its
    "accepted" path differs, calling the real MUAPI HTTP chain instead
    of returning a canned response."""

    def __init__(self):
        self.invocation_count = 0
        self._consumed_capability_ids: set = set()
        self.received_capabilities: list = []

    def invoke(self, capability: AdapterInvocationCapability, *, mode: "str | None" = None) -> AdapterResult:
        """`mode` is accepted (and ignored, beyond being a compatibility
        no-op) only so this adapter satisfies the exact call signature
        hypogeum.submission_gate.submit() currently uses uniformly for
        every registered adapter (Commit E's `adapter_instance.invoke(
        capability, mode=adapter_mode)`); a real adapter has no test-
        control "modes" -- its only inputs are the capability's own
        already-authorized fields."""
        if isinstance(capability, str):
            raise RawPromptRejectedError(
                "MuapiVideoAdapter.invoke() refuses a raw string prompt -- "
                "an AdapterInvocationCapability minted by the submission gate is required"
            )
        if not isinstance(capability, AdapterInvocationCapability):
            raise InvalidCapabilityError(f"expected AdapterInvocationCapability, got {type(capability)!r}")

        recomputed = compute_capability_checksum(capability)
        if recomputed != capability.capability_checksum:
            raise InvalidCapabilityError(
                f"capability {capability.capability_id!r} checksum mismatch: "
                f"stored {capability.capability_checksum!r} != recomputed {recomputed!r}"
            )
        if capability.capability_id in self._consumed_capability_ids:
            raise CapabilityAlreadyConsumedError(capability.capability_id)
        if capability.operation_payload_schema_version != SCHEMA_VERSION:
            raise InvalidCapabilityError(
                f"MuapiVideoAdapter only accepts {SCHEMA_VERSION!r} payloads, "
                f"got {capability.operation_payload_schema_version!r}"
            )

        self.invocation_count += 1
        self._consumed_capability_ids.add(capability.capability_id)
        self.received_capabilities.append(capability)

        return self._invoke_real(capability)

    def _invoke_real(self, capability: AdapterInvocationCapability) -> AdapterResult:
        # Local import: keeps this adapter module free of a hard,
        # module-load-time dependency on hypogeum.muapi's own (much
        # larger) import surface, and avoids any import-cycle risk
        # (hypogeum.muapi will, in a later step of this same commit,
        # import hypogeum.adapter_registry for registration).
        import hashlib

        from hypogeum.config import Config
        from hypogeum.logger import get_logger
        from hypogeum.muapi import (
            _build_job_payload, _download_file, _poll_until_complete,
            _submit_http, _validate_video_file, scene_file_path,
        )

        payload = decode_video_payload(capability.operation_payload_bytes)
        config = Config()
        log = get_logger("muapi_video_adapter")

        endpoint = f"{config.MUAPI_BASE_URL}/{payload['model']}"
        job_payload = _build_job_payload(
            payload["prompt"], payload["model"], payload["aspect_ratio"], payload["duration_seconds"],
            payload.get("reference_image_url"), video_resolution=config.MUAPI_VIDEO_RESOLUTION,
        )

        request_id, failure_reason = _submit_http(endpoint, job_payload, config, payload["scene_id"], log)
        if request_id is None:
            return AdapterResult(
                outcome="rejected", provider_job_id=None, provider_request_id=None,
                charged_amount=None, response_checksum=None, evidence={"failure_reason": failure_reason},
            )

        result, failure_reason = _poll_until_complete(request_id, config, payload["scene_id"], log)
        if result is None:
            # A poll timeout/failure after a successful submission is
            # exactly "possible acceptance, no definitive ack" --
            # submission_gate.submit() already treats any exception
            # raised here as unknown_acceptance; we raise rather than
            # return "rejected" so that safety property applies (never
            # silently release budget/grant capacity for a call whose
            # provider-side outcome is genuinely unknown).
            raise RuntimeError(f"MUAPI poll did not reach a terminal state: {failure_reason}")

        outputs = result.get("outputs", [])
        video_url = (outputs[0] if isinstance(outputs[0], str) else outputs[0].get("url", "")) if outputs else ""
        if not video_url:
            return AdapterResult(
                outcome="rejected", provider_job_id=request_id, provider_request_id=request_id,
                charged_amount=None, response_checksum=None, evidence={"failure_reason": "missing_video_url_in_result"},
            )

        dest_path = scene_file_path(Path(payload["output_dir"]), payload["scene_id"], payload.get("short_id"))
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        success, dl_reason = _download_file(video_url, dest_path, log)
        if not success:
            success, dl_reason = _download_file(video_url, dest_path, log)
        if not success:
            raise RuntimeError(f"MUAPI download failed after acceptance: {dl_reason}")

        valid, val_reason = _validate_video_file(dest_path, payload["duration_seconds"])
        if not valid:
            dest_path.unlink(missing_ok=True)
            raise RuntimeError(f"MUAPI downloaded file failed validation after acceptance: {val_reason}")

        # MUAPI's completed-job response does not always report an
        # explicit cost. When it doesn't, fall back to the capability's
        # own authorized max_cost_per_call as the charged amount --
        # never None: hypogeum.budget_scopes.commit_reservation()
        # requires a concrete Decimal to compare against the ceiling,
        # and "assume the full authorized ceiling was charged" is the
        # conservative default (never silently under-charges).
        credits_used = result.get("credits_used") or result.get("cost", {}).get("amount_usd")
        response_payload = {"provider_job_id": request_id, "video_url_sha256": hashlib.sha256(video_url.encode()).hexdigest()}
        response_checksum = hashlib.sha256(json.dumps(response_payload, sort_keys=True).encode()).hexdigest()

        return AdapterResult(
            outcome="accepted", provider_job_id=request_id, provider_request_id=request_id,
            charged_amount=(Decimal(str(credits_used)) if credits_used else capability.max_cost_per_call),
            response_checksum=response_checksum,
            evidence={"dest_path": str(dest_path), "credits_used": credits_used},
        )
