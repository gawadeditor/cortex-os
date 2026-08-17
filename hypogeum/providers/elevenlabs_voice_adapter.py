"""
hypogeum/providers/elevenlabs_voice_adapter.py — Real ElevenLabs
narration-segment adapter (voice.narration_segment domain).

Implements the exact same protocol
hypogeum.providers.fake_provider_adapter.FakeProviderAdapter defines,
mirroring hypogeum.providers.muapi_video_adapter.MuapiVideoAdapter's
own structure exactly. Wraps hypogeum.elevenlabs's existing, unchanged
_post_json_with_retry() call rather than re-implementing its retry/
halt-condition logic.

Scope: only the primary TTS-with-timestamps call is gated here. The
alignment fallback cascade (hypogeum.elevenlabs._alignment_fallback_
cascade -> forced_alignment()/whisper_fallback()) stays entirely with
the caller (hypogeum.elevenlabs._generate_narration_via_gate()),
exactly mirroring how the video/thumbnail domains keep their own
post-processing (validation, typography) outside the adapter -- the
adapter's job ends at "we have a produced, on-disk audio segment and
whatever alignment the TTS response itself provided."

_post_json_with_retry() raises ElevenLabsAuthFailure/ElevenLabsQuota
Exhausted/ElevenLabsRateLimitError/ElevenLabsError directly (never a
(result, failure_reason) tuple) -- this adapter does not catch and
reclassify them, the same disclosed choice already made for
hypogeum.providers.muapi_video_adapter.MuapiVideoAdapter (whose
_submit_http() raises MUAPIAuthFailure/MUAPIQuotaExhausted the same
way): an uncaught exception here propagates to hypogeum.submission_
gate.submit()'s own generic exception handler, which classifies it as
unknown_acceptance (a conservative, safe -- if not maximally precise
-- fail-mode). This has zero real-world consequence while the gate
path remains dormant (no real grant exists for any real channel).

operation_payload_schema_version "voice.narration_segment/1.0" -- the
exact JSON shape operation_payload_bytes must decode to:
{
  "text": str, "model_id": str, "voice_id": str, "output_format": str,
  "stability": float, "similarity_boost": float,
  "previous_request_ids": [str] | null,
  "beat_id": str, "episode_id": str, "output_dir": str
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
    "ElevenlabsVoiceAdapter",
    "encode_narration_payload",
    "decode_narration_payload",
]

SCHEMA_VERSION = "voice.narration_segment/1.0"

_REQUIRED_PAYLOAD_FIELDS = (
    "text", "model_id", "voice_id", "stability", "similarity_boost", "beat_id", "episode_id", "output_dir",
)


def encode_narration_payload(
    *, text: str, model_id: str, voice_id: str, output_format: "str | None", stability: float,
    similarity_boost: float, previous_request_ids: "list[str] | None", beat_id: str, episode_id: str,
    output_dir: str,
) -> bytes:
    payload = {
        "text": text, "model_id": model_id, "voice_id": voice_id, "output_format": output_format,
        "stability": stability, "similarity_boost": similarity_boost,
        "previous_request_ids": previous_request_ids, "beat_id": beat_id, "episode_id": episode_id,
        "output_dir": output_dir,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_narration_payload(payload_bytes: bytes) -> dict:
    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidCapabilityError(f"operation_payload_bytes is not valid JSON: {exc}") from exc
    missing = [f for f in _REQUIRED_PAYLOAD_FIELDS if f not in payload]
    if missing:
        raise InvalidCapabilityError(f"narration payload missing required field(s): {missing}")
    return payload


class ElevenlabsVoiceAdapter:
    """The one sanctioned real adapter for provider="elevenlabs",
    provider_operation="voice.narration_segment". Mirrors
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
                "ElevenlabsVoiceAdapter.invoke() refuses a raw string prompt -- "
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
                f"ElevenlabsVoiceAdapter only accepts {SCHEMA_VERSION!r} payloads, "
                f"got {capability.operation_payload_schema_version!r}"
            )

        self.invocation_count += 1
        self._consumed_capability_ids.add(capability.capability_id)
        self.received_capabilities.append(capability)

        return self._invoke_real(capability)

    def _invoke_real(self, capability: AdapterInvocationCapability) -> AdapterResult:
        import base64
        import hashlib

        from hypogeum.config import Config
        from hypogeum.logger import get_logger
        from hypogeum.elevenlabs import (
            _BASE_URL, _TTS_USD_PER_CHAR, _auth_headers, _chars_to_words, _output_format_extension,
            _parse_char_alignment, _post_json_with_retry,
        )
        from hypogeum.elevenlabs import ElevenLabsError

        payload = decode_narration_payload(capability.operation_payload_bytes)
        config = Config()
        log = get_logger("elevenlabs_voice_adapter")

        url = f"{_BASE_URL}/v1/text-to-speech/{payload['voice_id']}/with-timestamps"
        body: dict = {
            "text": payload["text"], "model_id": payload["model_id"],
            "voice_settings": {"stability": payload["stability"], "similarity_boost": payload["similarity_boost"]},
        }
        if payload.get("output_format"):
            body["output_format"] = payload["output_format"]
        if payload.get("previous_request_ids"):
            body["previous_request_ids"] = payload["previous_request_ids"][:3]

        # _post_json_with_retry() raises directly on any halt/hard-fail
        # condition -- deliberately not caught here, see module docstring.
        response = _post_json_with_retry(
            url=url, body=body, headers=_auth_headers(config),
            timeout=config.ELEVENLABS_TIMEOUT_SECONDS, log=log,
        )

        try:
            response_payload = response.json()
        except Exception as exc:
            raise ElevenLabsError(f"Failed to parse ElevenLabs TTS response: {exc}") from exc

        audio_b64 = response_payload.get("audio_base64", "")
        if not audio_b64:
            return AdapterResult(
                outcome="rejected", provider_job_id=None, provider_request_id=None,
                charged_amount=None, response_checksum=None, evidence={"failure_reason": "missing_audio_base64"},
            )
        audio_bytes = base64.b64decode(audio_b64)

        ext = _output_format_extension(payload.get("output_format") or "")
        narration_dir = Path(payload["output_dir"]) / "narration"
        narration_dir.mkdir(parents=True, exist_ok=True)
        local_path = narration_dir / f"{payload['beat_id']}{ext}"
        local_path.write_bytes(audio_bytes)

        request_id = response.headers.get("request-id", f"el_{payload['beat_id']}")

        alignment = (
            _parse_char_alignment(response_payload.get("normalized_alignment"))
            or _parse_char_alignment(response_payload.get("alignment"))
        )
        if alignment is not None:
            word_timings = _chars_to_words(alignment)
            alignment_source = "tts"
        else:
            word_timings = []
            alignment_source = None  # signals the caller to run its own fallback cascade

        duration_seconds = word_timings[-1].end_seconds if word_timings else 0.0
        char_count = len(payload["text"])

        response_evidence = {"provider_job_id": request_id, "local_path_sha256": hashlib.sha256(str(local_path).encode()).hexdigest()}
        response_checksum = hashlib.sha256(json.dumps(response_evidence, sort_keys=True).encode()).hexdigest()

        # Character-based cost, mirroring the legacy path's own formula
        # exactly (hypogeum.elevenlabs.generate_narration_segment()) --
        # never exceeds the authorized ceiling.
        computed_cost = Decimal(str(round(char_count * _TTS_USD_PER_CHAR, 6)))
        charged_amount = min(computed_cost, capability.max_cost_per_call)

        return AdapterResult(
            outcome="accepted", provider_job_id=request_id, provider_request_id=request_id,
            charged_amount=charged_amount,
            response_checksum=response_checksum,
            evidence={
                "local_path": str(local_path), "duration_seconds": duration_seconds,
                "word_timings": [
                    {"word": w.word, "start_seconds": w.start_seconds, "end_seconds": w.end_seconds}
                    for w in word_timings
                ],
                "alignment_source": alignment_source, "character_cost": char_count,
            },
        )
