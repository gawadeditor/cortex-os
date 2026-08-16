"""
hypogeum/providers/muapi_upscale_adapter.py — Real MUAPI AI Video
Upscaler adapter (upscale.video domain).

Implements the exact same protocol
hypogeum.providers.fake_provider_adapter.FakeProviderAdapter defines,
mirroring hypogeum.providers.muapi_video_adapter.MuapiVideoAdapter's
own structure exactly. Wraps hypogeum.providers.muapi_video_upscaler.
MuapiVideoUpscalerProvider's existing, unchanged enhance() method
(upload -> submit -> poll -> download, all internal to that one call)
as ONE atomic gated operation -- the same shape MuapiVideoAdapter
already uses for muapi.py's own multi-step submit/poll/download
sequence.

Unlike every other domain, EnhancementProvider.enhance()'s own
contract (hypogeum/providers/enhancement.py) explicitly forbids
raising for ordinary provider-side failures -- HTTP errors, timeouts,
rejected jobs must all surface as EnhancementResult(success=False,
failure_reason=...), never an exception, "so hypogeum/source_
enhancement.py's fail-closed policy can handle every provider
uniformly." This adapter honors that contract at its OWN boundary too:
enhance() never raises for a real provider-side failure, so
_invoke_real() never needs to catch and reclassify an exception the
way the OpenRouter/Perplexity adapters do -- a failed EnhancementResult
converts directly to outcome="rejected" evidence. A genuinely
unanticipated exception (a real bug) still propagates uncaught,
correctly classified as unknown_acceptance by hypogeum.submission_gate
.submit(), consistent with every other adapter's disclosed choice.

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
from decimal import Decimal
from pathlib import Path

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
    provider_operation="upscale.video". Mirrors
    hypogeum.providers.muapi_video_adapter.MuapiVideoAdapter's own
    single-use / checksum-verification discipline exactly."""

    def __init__(self):
        self.invocation_count = 0
        self._consumed_capability_ids: set = set()
        self.received_capabilities: list = []

    def invoke(self, capability: AdapterInvocationCapability, *, mode: "str | None" = None) -> AdapterResult:
        if isinstance(capability, str):
            raise RawPromptRejectedError(
                "MuapiUpscaleAdapter.invoke() refuses a raw string prompt -- "
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
                f"MuapiUpscaleAdapter only accepts {SCHEMA_VERSION!r} payloads, "
                f"got {capability.operation_payload_schema_version!r}"
            )

        self.invocation_count += 1
        self._consumed_capability_ids.add(capability.capability_id)
        self.received_capabilities.append(capability)

        return self._invoke_real(capability)

    def _invoke_real(self, capability: AdapterInvocationCapability) -> AdapterResult:
        import hashlib

        from hypogeum.config import Config
        from hypogeum.logger import get_logger
        from hypogeum.providers.muapi_video_upscaler import MuapiVideoUpscalerProvider

        payload = decode_upscale_payload(capability.operation_payload_bytes)
        config = Config()
        log = get_logger("muapi_upscale_adapter")

        provider = MuapiVideoUpscalerProvider()
        result = provider.enhance(
            source_path=Path(payload["source_path"]), dest_path=Path(payload["dest_path"]),
            target_width=payload["target_width"], target_height=payload["target_height"],
            target_resolution_label=payload["target_resolution_label"], config=config,
            episode_id=payload["episode_id"], scene_id=payload["scene_id"], log=log,
        )

        if not result.success:
            return AdapterResult(
                outcome="rejected", provider_job_id=result.request_id, provider_request_id=result.request_id,
                charged_amount=None, response_checksum=None,
                evidence={"failure_reason": result.failure_reason or "enhancement_failed"},
            )

        response_evidence = {"provider_job_id": result.request_id, "output_path_sha256": hashlib.sha256(str(result.output_path).encode()).hexdigest()}
        response_checksum = hashlib.sha256(json.dumps(response_evidence, sort_keys=True).encode()).hexdigest()

        # Real provider-reported cost, capped at the authorized ceiling --
        # falls back to the ceiling itself (conservative "assume full
        # ceiling charged") when the provider's response omitted cost,
        # mirroring MuapiVideoAdapter's own established fallback.
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
