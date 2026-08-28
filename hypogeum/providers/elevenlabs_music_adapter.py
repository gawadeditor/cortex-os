"""
hypogeum/providers/elevenlabs_music_adapter.py — Real ElevenLabs
music-track adapter (music.track domain).

Implements the exact same protocol
hypogeum.providers.fake_provider_adapter.FakeProviderAdapter defines,
mirroring hypogeum.providers.elevenlabs_voice_adapter.ElevenlabsVoiceAdapter's
own structure exactly. Wraps hypogeum.elevenlabs's existing, unchanged
_post_music_with_retry() call rather than re-implementing its retry logic.

Scope: only the POST /v1/music generation call is gated here. The
Channel Music Library search (hypogeum.elevenlabs._search_music_library)
and the post-success library-append (hypogeum.elevenlabs.
_append_to_music_library) both stay entirely with the caller
(hypogeum.elevenlabs._generate_music_via_gate()) -- the adapter's job
ends at "we have a produced, on-disk audio file (or a clean quality-
rejection) for one already-chosen track_id."

track_id and local_path are chosen by the CALLER before compiling the
envelope (unlike the legacy path, which picks a fresh uuid4() only
after a successful generation) -- this is what lets the envelope
carry a deterministic, pre-known output identity, exactly as every
other domain's envelope does. The adapter never invents its own
track_id or file path.

Quality-gate checks (zero-byte audio, Ancient-Civilizations low-
confidence threshold) are performed INSIDE the adapter, mirroring
hypogeum.providers.muapi_image_adapter.MuapiImageAdapter's own
min-bytes check: a failing check returns outcome="rejected" rather
than raising, so hypogeum.submission_gate.submit() releases the grant
reservation cleanly (Correction 4) instead of treating a quality
rejection as unknown acceptance.

_post_music_with_retry() raises ElevenLabsAuthFailure/ElevenLabsError
directly (never a tuple) -- this adapter does not catch and reclassify
them, the same disclosed choice already made for
hypogeum.providers.elevenlabs_voice_adapter.ElevenlabsVoiceAdapter.

operation_payload_schema_version "music.track/1.0" -- the exact JSON
shape operation_payload_bytes must decode to:
{
  "prompt": str, "model_id": str, "duration_ms": int,
  "force_instrumental": bool, "category": str, "track_id": str,
  "local_path": str, "episode_id": str
}
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import hypogeum.capability_boundary as capability_boundary
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
    "ElevenlabsMusicAdapter",
    "encode_music_payload",
    "decode_music_payload",
]

SCHEMA_VERSION = "music.track/1.0"

_REQUIRED_PAYLOAD_FIELDS = (
    "prompt", "model_id", "duration_ms", "force_instrumental", "category", "track_id", "local_path", "episode_id",
)


def encode_music_payload(
    *, prompt: str, model_id: str, duration_ms: int, force_instrumental: bool, category: str,
    track_id: str, local_path: str, episode_id: str,
) -> bytes:
    payload = {
        "prompt": prompt, "model_id": model_id, "duration_ms": duration_ms,
        "force_instrumental": force_instrumental, "category": category, "track_id": track_id,
        "local_path": local_path, "episode_id": episode_id,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_music_payload(payload_bytes: bytes) -> dict:
    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidCapabilityError(f"operation_payload_bytes is not valid JSON: {exc}") from exc
    missing = [f for f in _REQUIRED_PAYLOAD_FIELDS if f not in payload]
    if missing:
        raise InvalidCapabilityError(f"music payload missing required field(s): {missing}")
    return payload


class ElevenlabsMusicAdapter:
    """The one sanctioned real adapter for provider="elevenlabs",
    provider_operation="music.track". Mirrors
    hypogeum.providers.elevenlabs_voice_adapter.ElevenlabsVoiceAdapter's
    own single-use / checksum-verification discipline exactly."""

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
                "ElevenlabsMusicAdapter.invoke() refuses a raw string prompt -- "
                "an AdapterInvocationCapability minted by the submission gate is required"
            )
        if not isinstance(capability, AdapterInvocationCapability):
            raise InvalidCapabilityError(f"expected AdapterInvocationCapability, got {type(capability)!r}")

        ctx = capability_boundary.verify_and_consume_capability(
            capability, expected_schema_version=SCHEMA_VERSION,
            consumed_cache=self._consumed_capability_ids,
        )

        self.invocation_count += 1
        self.received_capabilities.append(capability)

        return self._invoke_real(ctx)

    def _invoke_real(self, ctx: "capability_boundary.VerifiedInvocationContext") -> AdapterResult:
        ctx.assert_live()
        ctx.claim_io_entry()  # durable atomic provider-I/O entry gate
        capability = ctx.capability
        import hashlib

        from hypogeum.config import Config
        from hypogeum.logger import get_logger
        from hypogeum.elevenlabs import (
            _BASE_URL, _MUSIC_ANCIENT_CIV_MIN_BYTES, _MUSIC_ENDPOINT, MUSIC_USD_PER_SEC,
            _auth_headers, _post_music_with_retry,
        )

        payload = decode_music_payload(capability.operation_payload_bytes)
        config = Config()
        log = get_logger("elevenlabs_music_adapter")

        body: dict = {
            "prompt": payload["prompt"], "music_length_ms": payload["duration_ms"],
            "model_id": payload["model_id"], "force_instrumental": payload["force_instrumental"],
        }
        url = f"{_BASE_URL}{_MUSIC_ENDPOINT}"

        # _post_music_with_retry() raises directly on any halt/hard-fail
        # condition -- deliberately not caught here, see module docstring.
        audio_bytes = _post_music_with_retry(
            url=url, body=body, headers=_auth_headers(config), timeout=config.ELEVENLABS_TIMEOUT_SECONDS,
            log=log, episode_id=payload["episode_id"], category=payload["category"],
        )

        if len(audio_bytes) == 0:
            return AdapterResult(
                outcome="rejected", provider_job_id=None, provider_request_id=None,
                charged_amount=None, response_checksum=None, evidence={"failure_reason": "zero_byte_audio"},
            )
        if payload["category"] == "Ancient Civilizations" and len(audio_bytes) < _MUSIC_ANCIENT_CIV_MIN_BYTES:
            return AdapterResult(
                outcome="rejected", provider_job_id=None, provider_request_id=None,
                charged_amount=None, response_checksum=None,
                evidence={"failure_reason": "ancient_civ_low_confidence", "audio_bytes": len(audio_bytes)},
            )

        local_path = Path(payload["local_path"])
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(audio_bytes)

        duration_seconds = payload["duration_ms"] / 1000.0
        request_id = payload["track_id"]

        response_evidence = {"provider_job_id": request_id, "local_path_sha256": hashlib.sha256(str(local_path).encode()).hexdigest()}
        response_checksum = hashlib.sha256(json.dumps(response_evidence, sort_keys=True).encode()).hexdigest()

        # Duration-based cost, mirroring the legacy path's own formula
        # exactly (hypogeum.elevenlabs.generate_music_track()) -- never
        # exceeds the authorized ceiling.
        computed_cost = Decimal(str(round(duration_seconds * MUSIC_USD_PER_SEC, 6)))
        charged_amount = min(computed_cost, capability.max_cost_per_call)

        return AdapterResult(
            outcome="accepted", provider_job_id=request_id, provider_request_id=request_id,
            charged_amount=charged_amount, response_checksum=response_checksum,
            evidence={
                "local_path": str(local_path), "duration_seconds": duration_seconds,
                "audio_bytes": len(audio_bytes),
            },
        )
