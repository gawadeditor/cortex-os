"""
hypogeum/providers/muapi_thumbnail_adapter.py — Real MUAPI thumbnail
adapter (image.thumbnail domain, muapi-provider branch only).

Implements the exact same protocol
hypogeum.providers.fake_provider_adapter.FakeProviderAdapter defines,
mirroring hypogeum.providers.muapi_video_adapter.MuapiVideoAdapter's
own structure exactly. Wraps hypogeum.muapi's existing, unchanged
submit/poll/download chain (_submit_http, _poll_until_complete,
_download_file) rather than re-implementing it. Scope: the `muapi`
provider branch of the thumbnail chain only -- the `openai`/
`openrouter` provider branches (hypogeum.muapi._generate_thumbnail_
openai/_generate_thumbnail_openrouter) are untouched, matching the
same scope decision the video/image domains already made (only the
muapi-provider legacy path gets a parallel gated path in this
program).

operation_payload_schema_version "image.thumbnail/1.0" -- the exact
JSON shape operation_payload_bytes must decode to:
{
  "prompt": str, "model": str, "aspect_ratio": str,
  "deliverable_id": str, "episode_id": str, "output_dir": str,
  "is_gpt_image_2": bool, "resolution": str | None, "quality": str | None
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
    "MuapiThumbnailAdapter",
    "encode_thumbnail_payload",
    "decode_thumbnail_payload",
]

SCHEMA_VERSION = "image.thumbnail/1.0"

_REQUIRED_PAYLOAD_FIELDS = ("prompt", "model", "aspect_ratio", "deliverable_id", "episode_id", "output_dir", "is_gpt_image_2")


def encode_thumbnail_payload(
    *, prompt: str, model: str, aspect_ratio: str, deliverable_id: str, episode_id: str,
    output_dir: str, is_gpt_image_2: bool, resolution: "str | None" = None, quality: "str | None" = None,
) -> bytes:
    payload = {
        "prompt": prompt, "model": model, "aspect_ratio": aspect_ratio,
        "deliverable_id": deliverable_id, "episode_id": episode_id, "output_dir": output_dir,
        "is_gpt_image_2": is_gpt_image_2, "resolution": resolution, "quality": quality,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_thumbnail_payload(payload_bytes: bytes) -> dict:
    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidCapabilityError(f"operation_payload_bytes is not valid JSON: {exc}") from exc
    missing = [f for f in _REQUIRED_PAYLOAD_FIELDS if f not in payload]
    if missing:
        raise InvalidCapabilityError(f"thumbnail payload missing required field(s): {missing}")
    return payload


class MuapiThumbnailAdapter:
    """The one sanctioned real adapter for provider="muapi",
    provider_operation="image.thumbnail". Mirrors
    hypogeum.providers.muapi_video_adapter.MuapiVideoAdapter's own
    single-use / checksum-verification discipline exactly."""

    def __init__(self):
        self.invocation_count = 0
        self._consumed_capability_ids: set = set()
        self.received_capabilities: list = []

    def invoke(
        self, capability: AdapterInvocationCapability, *, mode: "str | None" = None,
        on_provider_acknowledged: "object | None" = None,
    ) -> AdapterResult:
        # on_provider_acknowledged: accepted for protocol uniformity with
        # every other registered adapter (hypogeum.submission_gate.submit()
        # passes it to every adapter's invoke() uniformly) -- this domain
        # has no intermediate submit-then-poll acknowledgement phase, so it
        # is intentionally never called here.
        if isinstance(capability, str):
            raise RawPromptRejectedError(
                "MuapiThumbnailAdapter.invoke() refuses a raw string prompt -- "
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
                f"MuapiThumbnailAdapter only accepts {SCHEMA_VERSION!r} payloads, "
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
        from hypogeum.muapi import _download_file, _poll_until_complete, _submit_http, _MIN_IMAGE_BYTES

        payload = decode_thumbnail_payload(capability.operation_payload_bytes)
        config = Config()
        log = get_logger("muapi_thumbnail_adapter")

        deliverable_id = payload["deliverable_id"]
        endpoint = f"{config.MUAPI_BASE_URL}/{payload['model']}"
        if payload["is_gpt_image_2"]:
            job_payload = {
                "prompt": payload["prompt"], "aspect_ratio": payload["aspect_ratio"],
                "resolution": payload.get("resolution"), "quality": payload.get("quality"),
            }
        else:
            job_payload = {"prompt": payload["prompt"], "aspect_ratio": payload["aspect_ratio"]}

        request_id, failure_reason = _submit_http(endpoint, job_payload, config, deliverable_id, log)
        if request_id is None:
            return AdapterResult(
                outcome="rejected", provider_job_id=None, provider_request_id=None,
                charged_amount=None, response_checksum=None, evidence={"failure_reason": failure_reason},
            )

        result, failure_reason = _poll_until_complete(request_id, config, deliverable_id, log)
        if result is None:
            raise RuntimeError(f"MUAPI thumbnail poll did not reach a terminal state: {failure_reason}")

        outputs = result.get("outputs", [])
        image_url = outputs[0] if isinstance(outputs[0], str) else (outputs[0].get("url", "") if outputs else "")
        if not image_url:
            return AdapterResult(
                outcome="rejected", provider_job_id=request_id, provider_request_id=request_id,
                charged_amount=None, response_checksum=None, evidence={"failure_reason": "missing_image_url_in_result"},
            )

        local_path = Path(payload["output_dir"]) / "thumbnails" / f"{deliverable_id}.jpg"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        success, dl_reason = _download_file(image_url, local_path, log)
        if not success:
            success, dl_reason = _download_file(image_url, local_path, log)
        if not success:
            raise RuntimeError(f"MUAPI thumbnail download failed after acceptance: {dl_reason}")

        if local_path.stat().st_size < _MIN_IMAGE_BYTES:
            raise RuntimeError(
                f"MUAPI thumbnail file is suspiciously small ({local_path.stat().st_size} bytes) after acceptance"
            )

        credits_used = result.get("credits_used") or result.get("cost", {}).get("amount_usd")
        response_payload = {"provider_job_id": request_id, "local_path_sha256": hashlib.sha256(str(local_path).encode()).hexdigest()}
        response_checksum = hashlib.sha256(json.dumps(response_payload, sort_keys=True).encode()).hexdigest()

        return AdapterResult(
            outcome="accepted", provider_job_id=request_id, provider_request_id=request_id,
            charged_amount=(Decimal(str(credits_used)) if credits_used else capability.max_cost_per_call),
            response_checksum=response_checksum,
            evidence={"local_path": str(local_path), "credits_used": credits_used},
        )
