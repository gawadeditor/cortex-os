"""
hypogeum/elevenlabs.py — ElevenLabs narration and alignment client

Single integration point for all ElevenLabs calls in the pipeline.
Audio Agent is the only agent that calls narration functions.
Assembly Agent is the only agent that calls music generation functions.

Confirmed API surface (verified 2026-06-02 via SDK source analysis):
  Auth:    xi-api-key: <key> header (NOT Authorization: Bearer)
  Base:    https://api.elevenlabs.io
  TTS:     POST v1/text-to-speech/{voice_id}/with-timestamps   → JSON {audio_base64, alignment, normalized_alignment}
  Align:   POST v1/forced-alignment                            → JSON {words: [{text, start, end, loss}]}
  Models:  GET  v1/models                                      → JSON [{model_id, model_rates, ...}]
  Music:   POST v1/music                                       → binary audio stream (Iterator[bytes])
  Whisper: POST https://api.openai.com/v1/audio/transcriptions → JSON {text, words: [{word, start, end}]}

Field name: the TTS response uses audio_base64 (no underscore before 6).
Verified 2026-06-05 via live API call. The SDK's Fern-generated types may differ.

Alignment cascade (applied in generate_narration_segment):
  1. normalized_alignment from /with-timestamps (preferred — text-normalized)
  2. alignment from /with-timestamps (if normalized is null)
  3. forced_alignment() — POST v1/forced-alignment with audio file + transcript
  4. whisper_fallback() — POST OpenAI /v1/audio/transcriptions
  5. alignment_source = "none", word_timings = [] (Audio Agent handles gracefully)

Retry policy:
  429 / 5xx → retry once with Retry-After or exponential backoff
  401       → ElevenLabsAuthFailure (halt all narration generation)
  400 / 422 → inspect body; quota keywords → ElevenLabsQuotaExhausted; else hard fail
  No retry in the official SDK — implemented here.

Integration contracts:
  - All environment access goes through the Config object — never os.getenv()
  - All logging goes through hypogeum.logger.get_logger() — never print() or stdlib logging
  - ELEVENLABS_API_KEY is never logged, printed, or stored in any output file
  - ELEVENLABS_VOICE_ID is a permanent channel identity lock — never change after first production run

Logging events emitted:
  elevenlabs_tts_started           — info  — before each TTS request
  elevenlabs_tts_completed         — info  — TTS succeeded, audio written to disk
  elevenlabs_tts_retry             — warn  — 429/5xx, retrying once
  elevenlabs_alignment_null        — warn  — both alignment fields null from TTS
  elevenlabs_forced_alignment_start — info — before forced-alignment call
  elevenlabs_forced_alignment_done  — info — forced-alignment returned word timings
  elevenlabs_forced_alignment_failed — warn — forced-alignment failed; escalating to Whisper
  elevenlabs_whisper_start          — info — before Whisper transcription call
  elevenlabs_whisper_done           — info — Whisper returned word timings
  elevenlabs_whisper_failed         — error — Whisper failed; alignment_source = "none"
  elevenlabs_auth_failure           — error — 401; halts all narration generation
  elevenlabs_quota_exhausted        — error — credit exhaustion detected; halts generation
  elevenlabs_cost_fetched           — info  — model cost multiplier retrieved from API
  elevenlabs_cost_fetch_failed      — warn  — cost multiplier fetch failed; using 1.0 fallback

Music generation logging events (generate_music_track):
  elevenlabs_music_library_search   — info  — library search performed before generation
  elevenlabs_music_generation_start — info  — generation request being sent
  elevenlabs_music_generation_done  — info  — generation response received, audio written
  elevenlabs_music_retry            — warn  — 429/5xx, retrying once
  elevenlabs_music_hard_failure     — error — 401/402/403/422/400; PDR required
  elevenlabs_music_validation_fail  — warn  — response validation failed (zero bytes etc)
  elevenlabs_music_library_written  — info  — track stored in library
  elevenlabs_music_pdr_issued       — error — section will proceed without music
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

import requests

from hypogeum.config import Config
from hypogeum.logger import get_logger
import hypogeum.finance as _finance
from hypogeum.execution_envelope import GenerationExecutionEnvelope
from hypogeum.prompt_loader import load_music_categories

_MUSIC_CATEGORIES_FILE = Path(__file__).resolve().parent / "prompts" / "music_categories.json"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL     = "https://api.elevenlabs.io"
_OPENAI_BASE  = "https://api.openai.com/v1"

# Default voice settings (applied when config values are None)
_DEFAULT_STABILITY         = 0.5
_DEFAULT_SIMILARITY_BOOST  = 0.75

# Default narration model (frozen M3.1 decision — benchmark against eleven_v3 pre-production)
_DEFAULT_MODEL_ID = "eleven_multilingual_v2"

# Cost estimates (USD) — used for cost recording only; not billing.
# eleven_multilingual_v2 at creator tier: ~$0.30 per 1,000 characters.
TTS_USD_PER_CHAR   = 3e-4
# music_v1: approximate per-second cost for generated music.
MUSIC_USD_PER_SEC  = 0.02

# Default output format
_DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"

# HTTP status classifications
_HALT_CODES      = frozenset({401})           # halt ALL narration generation
_HARD_FAIL_CODES = frozenset({400, 422})       # hard fail this beat, no retry
_SOFT_RETRY_CODES = frozenset({429, 500, 502, 503})

# Retry policy
_RETRY_MAX          = 1         # one retry per call
_RETRY_INITIAL_DELAY = 5.0      # seconds (shorter than MUAPI — TTS is faster to restart)
_RETRY_MULTIPLIER   = 2.0
_RETRY_MAX_DELAY    = 60.0
_RETRY_JITTER       = 0.20      # ±20%
_RETRY_AFTER_DEFAULT = 30.0     # seconds when 429 Retry-After header is absent

# Quota detection keywords in response body (case-insensitive)
_QUOTA_KEYWORDS = frozenset({"quota", "credits", "limit exceeded", "insufficient"})

# SRT subtitle grouping
_SRT_MAX_WORDS_PER_LINE = 7     # words per subtitle block
_SRT_MIN_GAP_SECONDS    = 0.05  # minimum gap between subtitle blocks

# File extension map for output formats
_FORMAT_EXTENSIONS = {
    "mp3": ".mp3",
    "wav": ".wav",
    "pcm": ".pcm",
    "opus": ".opus",
    "ulaw": ".ulaw",
    "alaw": ".alaw",
}

# Module-level cache for model cost multipliers
_model_cost_cache: dict[str, float] = {}

# ---------------------------------------------------------------------------
# Music generation constants (C1–C9 verified 2026-06-03)
# ---------------------------------------------------------------------------

# C1: Verified endpoint (NOT /v1/sound-generation)
_MUSIC_ENDPOINT = "/v1/music"

# C3: Duration range for /v1/music API (milliseconds)
_MUSIC_MIN_DURATION_MS  = 3_000      # 3 seconds minimum
_MUSIC_MAX_DURATION_MS  = 300_000    # 300 seconds cap recommendation

# C5: Required for background tracks — no vocals
_MUSIC_FORCE_INSTRUMENTAL = True

# C6: Confirmed default model
_MUSIC_DEFAULT_MODEL_ID = "music_v1"

# Ancient Civilizations elevated confidence check byte threshold
_MUSIC_ANCIENT_CIV_MIN_BYTES = 50_000

# Music category slug mapping (from §10.A)
_MUSIC_CATEGORY_SLUGS: dict[str, str] = {
    "Mystery":              "mystery",
    "Discovery":            "discovery",
    "Investigation":        "investigation",
    "Ancient Civilizations": "ancient-civilizations",
    "Tension":              "tension",
    "Resolution":           "resolution",
}

# Category base prompts (from §5.C)
# Music category prompts loaded from hypogeum/prompts/music_categories.json
_MUSIC_CATEGORY_PROMPTS: dict[str, str] = load_music_categories(_MUSIC_CATEGORIES_FILE)

# Tone arc extension truncation limit (§15 security consideration)
_TONE_ARC_MAX_CHARS = 100

# Music library default file name
_MUSIC_LIBRARY_FILENAME = "music-library.json"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CharAlignment:
    """
    Character-level alignment data from ElevenLabs /with-timestamps.
    Three parallel lists — index i covers the same character.
    """
    characters: List[str]
    character_start_times_seconds: List[float]
    character_end_times_seconds: List[float]


@dataclass
class WordTiming:
    """
    Word-level timing — reconstructed from CharAlignment or returned by
    forced_alignment / whisper_fallback.
    """
    word: str
    start_seconds: float
    end_seconds: float


@dataclass
class NarrationSegmentResult:
    """
    Result of generate_narration_segment().

    Audio Agent stores asset_id in the Narration Package.
    word_timings drives SRT generation via build_srt().
    alignment_source records the fallback depth for diagnostics.
    """
    beat_id: str
    """Identifier from Script Package narration_beats[]."""

    asset_id: str
    """Relative path from .hypogeum-assets/ without extension.
    Example: episodes/ep_001/narration/b_001"""

    local_path: Path
    """Absolute filesystem path to the downloaded audio file."""

    duration_seconds: float
    """Audio duration in seconds (from word_timings, or 0.0 if unavailable)."""

    request_id: str
    """ElevenLabs request ID. Pass in previous_request_ids for adjacent beats
    to improve prosody continuity across segment boundaries."""

    character_cost: int
    """Number of characters in the text — used for cost accounting."""

    word_timings: List[WordTiming]
    """Word-level timings for SRT generation. Empty list when all alignment
    paths failed (alignment_source = "none")."""

    alignment_source: str
    """One of: "tts" | "forced_alignment" | "whisper" | "none"."""


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

@dataclass
class MusicTrackResult:
    """
    Result of generate_music_track().

    Assembly Agent uses track_id and asset_id to populate music_log[]
    in the Final Render Package.
    """
    track_id: str
    """UUID generated at storage time."""

    category: str
    """Music category (e.g. 'Mystery', 'Ancient Civilizations')."""

    asset_id: str
    """Relative path from .hypogeum-assets/ without extension.
    Example: channel/music/mystery/mystery_{track_id}"""

    local_path: Path
    """Absolute filesystem path to the stored audio file."""

    source: str
    """'channel_library' if retrieved from library; 'ai_generated' if newly generated."""

    duration_seconds: float
    """Requested (and expected) duration in seconds."""

    prompt_used: Optional[str] = None
    """Full prompt sent to ElevenLabs (None for library cache hits)."""


class ElevenLabsError(RuntimeError):
    """Base class for all ElevenLabs client errors."""


class ElevenLabsAuthFailure(ElevenLabsError):
    """
    HTTP 401 — API key missing or invalid.

    Halts ALL narration generation for the current episode run.
    Audio Agent must issue a PDR: severity BLOCKING, type provider_authentication_failure.
    """
    def __init__(self) -> None:
        super().__init__(
            "ElevenLabs authentication failed (HTTP 401). "
            "Check ELEVENLABS_API_KEY in environment. All narration generation halted."
        )


class ElevenLabsQuotaExhausted(ElevenLabsError):
    """
    Credit quota exhausted.

    Detected by inspecting response body for quota-related keywords
    (ElevenLabs does not use a distinct HTTP code for quota exhaustion).
    Halts ALL narration generation. Requires out-of-band account resolution.
    """
    def __init__(self, detail: str = "") -> None:
        msg = "ElevenLabs credit quota exhausted. All narration generation halted."
        if detail:
            msg += f" Detail: {detail}"
        super().__init__(msg)


class ElevenLabsRateLimitError(ElevenLabsError):
    """
    HTTP 429 — rate limit exceeded.

    Raised when both the initial attempt and the one retry hit 429.
    Callers may pause and retry after a longer delay.
    """
    def __init__(self) -> None:
        super().__init__(
            "ElevenLabs rate limit exceeded (HTTP 429) after retry. "
            "Pause generation and retry after a delay."
        )


# ---------------------------------------------------------------------------
# Internal: HTTP helpers
# ---------------------------------------------------------------------------

def _auth_headers(config: Config) -> dict:
    """Return auth headers. ELEVENLABS_API_KEY is never logged or returned to callers."""
    if not config.ELEVENLABS_API_KEY:
        raise ElevenLabsAuthFailure()
    return {
        "xi-api-key": config.ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }


def _retry_delay(attempt: int, retry_after: Optional[float] = None) -> float:
    """Compute the delay before a retry."""
    if retry_after is not None:
        return min(retry_after, _RETRY_MAX_DELAY)
    base = min(_RETRY_INITIAL_DELAY * (_RETRY_MULTIPLIER ** attempt), _RETRY_MAX_DELAY)
    jitter = base * _RETRY_JITTER * (2 * random.random() - 1)
    return max(0.0, base + jitter)


def _detect_quota_exhaustion(response: requests.Response) -> bool:
    """
    Inspect a non-2xx response body for quota/credit exhaustion signals.
    ElevenLabs does not use a distinct HTTP status for quota — look at the body.
    """
    try:
        body_text = response.text.lower()
    except Exception:
        return False
    return any(kw in body_text for kw in _QUOTA_KEYWORDS)


def _post_json_with_retry(
    url: str,
    body: dict,
    headers: dict,
    timeout: int,
    log,
) -> requests.Response:
    """
    POST JSON body with one retry on 429 / 5xx. Raises on halt or hard-fail conditions.
    Returns the successful response object.
    """
    for attempt in range(_RETRY_MAX + 1):
        response = requests.post(url, json=body, headers=headers, timeout=timeout)

        if response.status_code in _HALT_CODES:
            log.error("elevenlabs_auth_failure", url=url, status=response.status_code)
            raise ElevenLabsAuthFailure()

        if response.status_code in _HARD_FAIL_CODES:
            if _detect_quota_exhaustion(response):
                log.error(
                    "elevenlabs_quota_exhausted",
                    url=url,
                    status=response.status_code,
                    body_preview=response.text[:200],
                )
                raise ElevenLabsQuotaExhausted(detail=response.text[:200])
            # Hard fail — do not retry
            raise ElevenLabsError(
                f"ElevenLabs hard failure (HTTP {response.status_code}) at {url}: "
                f"{response.text[:200]}"
            )

        if response.status_code in _SOFT_RETRY_CODES:
            if attempt < _RETRY_MAX:
                retry_after_header = response.headers.get("Retry-After")
                retry_after = float(retry_after_header) if retry_after_header else None
                delay = _retry_delay(attempt, retry_after)
                log.warn(
                    "elevenlabs_tts_retry",
                    url=url,
                    status=response.status_code,
                    attempt=attempt + 1,
                    delay_seconds=round(delay, 2),
                )
                time.sleep(delay)
                continue
            # Exhausted retries on 429
            if response.status_code == 429:
                raise ElevenLabsRateLimitError()
            raise ElevenLabsError(
                f"ElevenLabs server error (HTTP {response.status_code}) after retry."
            )

        if response.status_code >= 200 and response.status_code < 300:
            return response

        # Any other unexpected code
        raise ElevenLabsError(
            f"ElevenLabs unexpected response (HTTP {response.status_code}) at {url}: "
            f"{response.text[:200]}"
        )

    # Should not be reached
    raise ElevenLabsError("ElevenLabs request failed — retry logic exhausted.")  # pragma: no cover


def _post_multipart_with_retry(
    url: str,
    files: dict,
    data: dict,
    auth_headers: dict,
    timeout: int,
    log,
) -> requests.Response:
    """
    POST multipart/form-data with one retry on 429 / 5xx.
    auth_headers must not include Content-Type (requests sets it with boundary).
    """
    # Remove Content-Type so requests can set it with the multipart boundary
    headers = {k: v for k, v in auth_headers.items() if k != "Content-Type"}

    for attempt in range(_RETRY_MAX + 1):
        response = requests.post(url, files=files, data=data, headers=headers, timeout=timeout)

        if response.status_code in _HALT_CODES:
            log.error("elevenlabs_auth_failure", url=url, status=response.status_code)
            raise ElevenLabsAuthFailure()

        if response.status_code in _HARD_FAIL_CODES:
            if _detect_quota_exhaustion(response):
                raise ElevenLabsQuotaExhausted(detail=response.text[:200])
            raise ElevenLabsError(
                f"ElevenLabs multipart hard failure (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            )

        if response.status_code in _SOFT_RETRY_CODES:
            if attempt < _RETRY_MAX:
                retry_after_header = response.headers.get("Retry-After")
                retry_after = float(retry_after_header) if retry_after_header else None
                delay = _retry_delay(attempt, retry_after)
                time.sleep(delay)
                continue
            if response.status_code == 429:
                raise ElevenLabsRateLimitError()
            raise ElevenLabsError(
                f"ElevenLabs multipart server error (HTTP {response.status_code}) after retry."
            )

        if 200 <= response.status_code < 300:
            return response

        raise ElevenLabsError(
            f"ElevenLabs multipart unexpected response (HTTP {response.status_code}): "
            f"{response.text[:200]}"
        )

    raise ElevenLabsError("ElevenLabs multipart request failed.")  # pragma: no cover


# ---------------------------------------------------------------------------
# Internal: alignment parsing and word reconstruction
# ---------------------------------------------------------------------------

def _parse_char_alignment(raw: Optional[dict]) -> Optional[CharAlignment]:
    """
    Parse a raw alignment dict from the TTS response into a CharAlignment.
    Returns None if raw is None or structurally invalid.
    All three lists must be non-empty and equal length.
    """
    if not raw:
        return None
    chars  = raw.get("characters", [])
    starts = raw.get("character_start_times_seconds", [])
    ends   = raw.get("character_end_times_seconds", [])
    if not chars or len(chars) != len(starts) or len(chars) != len(ends):
        return None
    return CharAlignment(
        characters=list(chars),
        character_start_times_seconds=[float(x) for x in starts],
        character_end_times_seconds=[float(x) for x in ends],
    )


def _chars_to_words(alignment: CharAlignment) -> List[WordTiming]:
    """
    Reconstruct word-level timings from character-level alignment.

    Groups consecutive non-space characters into words. Whitespace characters
    are consumed but not included in any word. The word's start time is the
    start of its first character; its end time is the end of its last character.
    """
    words: List[WordTiming] = []
    current_chars: List[str] = []
    word_start: Optional[float] = None
    word_end: Optional[float] = None

    for ch, t_start, t_end in zip(
        alignment.characters,
        alignment.character_start_times_seconds,
        alignment.character_end_times_seconds,
    ):
        if ch in (" ", "\t", "\n", "\r"):
            # Flush current word if any
            if current_chars and word_start is not None:
                words.append(WordTiming(
                    word="".join(current_chars),
                    start_seconds=word_start,
                    end_seconds=word_end,
                ))
                current_chars = []
                word_start = None
                word_end = None
        else:
            if not current_chars:
                word_start = t_start
            current_chars.append(ch)
            word_end = t_end

    # Flush final word
    if current_chars and word_start is not None:
        words.append(WordTiming(
            word="".join(current_chars),
            start_seconds=word_start,
            end_seconds=word_end,
        ))

    return words


def _output_format_extension(output_format: str) -> str:
    """Derive file extension from an ElevenLabs output_format string."""
    prefix = output_format.split("_")[0].lower()
    return _FORMAT_EXTENSIONS.get(prefix, ".mp3")


# ---------------------------------------------------------------------------
# Internal: SRT formatting
# ---------------------------------------------------------------------------

def _format_srt_timestamp(seconds: float) -> str:
    """Format float seconds as SRT timestamp: HH:MM:SS,mmm"""
    total_ms = int(seconds * 1000)
    h  = total_ms // 3_600_000
    m  = (total_ms % 3_600_000) // 60_000
    s  = (total_ms % 60_000) // 1_000
    ms = total_ms % 1_000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_to_srt_blocks(
    words: List[WordTiming],
    time_offset: float = 0.0,
    max_words: int = _SRT_MAX_WORDS_PER_LINE,
) -> List[tuple[float, float, str]]:
    """
    Group a flat list of WordTimings into subtitle blocks.

    Returns list of (start_seconds, end_seconds, text) tuples,
    with time_offset applied to all timestamps.
    Each block contains at most max_words words.
    """
    if not words:
        return []

    blocks: List[tuple[float, float, str]] = []
    i = 0
    while i < len(words):
        chunk = words[i : i + max_words]
        start = chunk[0].start_seconds + time_offset
        end   = chunk[-1].end_seconds + time_offset
        text  = " ".join(w.word for w in chunk)
        blocks.append((start, end, text))
        i += max_words

    return blocks


# ---------------------------------------------------------------------------
# Public: generate_narration_segment
# ---------------------------------------------------------------------------

_HYPOGEUM_WORKFLOW_ID = "hypogeum_main_production"
_HYPOGEUM_WORKFLOW_VERSION = "1.0.0"
_VOICE_PROVIDER = "elevenlabs"
_VOICE_PROVIDER_OPERATION = "voice.narration_segment"
_VOICE_COMPILER_IDENTITY = "hypogeum.elevenlabs.generate_narration_segment"
_VOICE_ENVELOPE_SCHEMA_VERSION = "1.0"


def _canonical_json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _generate_narration_via_gate(
    *, channel_id: str, grant, beat_id: str, text: str, episode_id: str, output_dir: Path,
    config: Config, log, previous_request_ids: Optional[List[str]] = None,
) -> NarrationSegmentResult:
    """
    The grant-derived gate path for narration generation -- mirrors
    hypogeum.muapi._submit_scene_via_gate()'s structure exactly (see
    that function's own docstring for the full rationale). Never
    reached unless a real, verified grant already resolves for
    (channel_id, episode_id). Raises ElevenLabsError on any
    compile/authorize/gate-denial failure, matching the legacy path's
    own exception contract.
    """
    import hashlib
    from datetime import timedelta

    import hypogeum.adapter_registry as adapter_registry
    import hypogeum.authorization_authority as authority
    import hypogeum.authorization_store as authorization_store
    import hypogeum.budget_scopes as bs
    import hypogeum.channel_config as channel_config
    import hypogeum.db as db
    import hypogeum.submission_gate as submission_gate
    import hypogeum.workflow_executor as workflow_executor
    from hypogeum.providers.elevenlabs_voice_adapter import (
        SCHEMA_VERSION as _VOICE_SCHEMA_VERSION, ElevenlabsVoiceAdapter, encode_narration_payload,
    )
    from hypogeum.provider_receipt import WriteAheadReceiptStore

    voice_id = config.ELEVENLABS_VOICE_ID
    model_id = config.ELEVENLABS_MODEL_ID or _DEFAULT_MODEL_ID
    output_format = config.ELEVENLABS_OUTPUT_FORMAT or _DEFAULT_OUTPUT_FORMAT
    stability = config.ELEVENLABS_STABILITY if config.ELEVENLABS_STABILITY is not None else _DEFAULT_STABILITY
    similarity = config.ELEVENLABS_SIMILARITY_BOOST if config.ELEVENLABS_SIMILARITY_BOOST is not None else _DEFAULT_SIMILARITY_BOOST

    if not voice_id:
        raise ElevenLabsError(
            "ELEVENLABS_VOICE_ID is not set. This is a permanent channel identity lock — pin it before first production run."
        )

    at = datetime.now(timezone.utc).isoformat()

    package_identity = channel_config.compute_channel_package_identity(channel_id)
    if not package_identity.get("available"):
        raise ElevenLabsError(f"channel_package_identity_unavailable for beat {beat_id!r}")
    workflow_binding = workflow_executor.resolve_workflow_binding(
        channel_id, _HYPOGEUM_WORKFLOW_ID, _HYPOGEUM_WORKFLOW_VERSION,
    )

    plan_checksum = hashlib.sha256(_canonical_json_bytes({"beat_id": beat_id, "text": text})).hexdigest()
    structured_spec_checksum = hashlib.sha256(_canonical_json_bytes({"voice_id": voice_id, "model_id": model_id})).hexdigest()

    operation_payload_bytes = encode_narration_payload(
        text=text, model_id=model_id, voice_id=voice_id, output_format=output_format,
        stability=stability, similarity_boost=similarity, previous_request_ids=previous_request_ids,
        beat_id=beat_id, episode_id=episode_id, output_dir=str(output_dir),
    )
    operation_payload_content = operation_payload_bytes.decode("utf-8")
    operation_payload_sha256 = hashlib.sha256(operation_payload_bytes).hexdigest()

    expected_output_identity = f"episodes/{episode_id}/narration/{beat_id}"
    dest_path = output_dir / "narration" / f"{beat_id}{_output_format_extension(output_format)}"
    char_count = len(text)
    max_cost = Decimal(str(round(char_count * TTS_USD_PER_CHAR, 6)))
    authorization_id = f"{episode_id}:narr_{beat_id}"

    # Explicit protected POST_PLAN binding: the exact canonical Production
    # Plan content_hash this paid invocation belongs to (empty when
    # pre-plan -- the first narration is pre-plan). capability_boundary
    # re-verifies it equals the freshly-loaded canonical plan.
    from hypogeum.production_plan import canonical_episode_dir as _canon_dir  # noqa: PLC0415
    from hypogeum.production_plan import load_production_plan as _load_canon_plan  # noqa: PLC0415
    _bound_plan = _load_canon_plan(episode_id, _canon_dir(episode_id))
    _plan_audit_metadata = (
        {"production_plan_content_hash": _bound_plan.content_hash} if _bound_plan is not None else {}
    )

    try:
        envelope = GenerationExecutionEnvelope.create(
            envelope_schema_version=_VOICE_ENVELOPE_SCHEMA_VERSION, envelope_version=1, channel_id=channel_id,
            channel_package_version=package_identity["version"], channel_package_checksum=package_identity["checksum"],
            execution_id=episode_id, workflow_version=workflow_binding["workflow_version"],
            plan_version="live", plan_checksum=plan_checksum, planned_shot_id=f"narr_{beat_id}",
            structured_spec_checksum=structured_spec_checksum, compiler_identity=_VOICE_COMPILER_IDENTITY,
            compiler_version="1", operation_payload_content=operation_payload_content,
            operation_payload_sha256=operation_payload_sha256, operation_payload_schema_version=_VOICE_SCHEMA_VERSION,
            content_provenance={
                "compiler": _VOICE_COMPILER_IDENTITY, "compiler_version": "1",
                "source_work_item_id": f"narr_{beat_id}", "rendered_at": at,
            },
            provider=_VOICE_PROVIDER, model=model_id, provider_operation=_VOICE_PROVIDER_OPERATION,
            deterministic_output_path=str(dest_path), expected_output_identity=expected_output_identity,
            authorization_id=authorization_id, authorization_version=1, max_cost_per_call=max_cost,
            run_level_budget_ref=episode_id, episode_level_budget_ref=episode_id, currency="USD",
            retry_authorized=False, fallback_authorized=False, idempotency_key=authorization_id,
            created_at=at, expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
            dependency_checksums={
                "channel_package": package_identity["checksum"], "plan": plan_checksum,
                "structured_spec": structured_spec_checksum, "compiler": hashlib.sha256(_VOICE_COMPILER_IDENTITY.encode()).hexdigest(),
            },
            audit_metadata=_plan_audit_metadata,
        )
        authorization = authority.issue_child_authorization(envelope, grant.grant_id, grant.grant_version, at=at)
    except Exception as exc:  # noqa: BLE001 -- any compile/authorize failure here is a clean non-network denial
        raise ElevenLabsError(f"Narration gate authorization failed for beat {beat_id!r}: {exc}") from exc

    conn = db.get_connection()
    try:
        authorization_store.persist_execution_envelope(conn, envelope)

        adapter_registry.register_adapter(_VOICE_PROVIDER, model_id, _VOICE_PROVIDER_OPERATION, "1.0", ElevenlabsVoiceAdapter)
        lease_owner_id = f"elevenlabs_voice_adapter:{beat_id}"
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        lease = db.acquire_lease(conn, envelope.envelope_id, lease_owner_id, acquired_at=at, expires_at=lease_expires_at)
        if lease is None:
            raise ElevenLabsError(f"Narration gate lease unavailable for beat {beat_id!r}")

        receipt_store = WriteAheadReceiptStore(output_dir)
        live_sources = submission_gate.LiveSources(
            channel_package_checksum=package_identity["checksum"], plan_checksum=plan_checksum,
            structured_spec_checksum=structured_spec_checksum, compiler_version="1",
        )
        scopes = [
            bs.ScopeRequest(bs.BudgetScopeKind.CALL, f"narr_{beat_id}", max_cost, "USD"),
            bs.ScopeRequest(bs.BudgetScopeKind.EPISODE, episode_id, grant.max_total_spend_all_bindings or max_cost, "USD"),
        ]
        result = submission_gate.submit(
            envelope, live_sources=live_sources, authorization=authorization, lease_conn=conn,
            lease_owner_id=lease_owner_id, lease_fencing_token=lease["fencing_token"], receipt_store=receipt_store,
            execution_id=episode_id, attempt_id=authorization_id, budget_scopes=scopes,
            required_provider=_VOICE_PROVIDER, required_model=model_id, at=at,
            expected_output_identity=expected_output_identity, expected_deterministic_output_path=str(dest_path),
            output_already_exists=dest_path.exists(),
        )
    finally:
        conn.close()

    if not result.approved:
        raise ElevenLabsError(f"Narration gate denied for beat {beat_id!r}: [{result.denied_at_check}] {result.reason}")

    evidence = result.adapter_result.evidence if result.adapter_result else {}
    local_path = Path(evidence["local_path"])
    word_timings = [WordTiming(**w) for w in evidence.get("word_timings", [])]
    alignment_source = evidence.get("alignment_source")

    if alignment_source is None:
        log.warn("elevenlabs_alignment_null", beat_id=beat_id, action="escalating_to_forced_alignment")
        word_timings, alignment_source = _alignment_fallback_cascade(
            beat_id=beat_id, audio_path=local_path, transcript=text, config=config, episode_id=episode_id, log=log,
        )

    duration_seconds = word_timings[-1].end_seconds if word_timings else 0.0
    _finance.record_cost(
        provider=_VOICE_PROVIDER, model=model_id, call_type="narration",
        cost_usd=float(result.adapter_result.charged_amount) if result.adapter_result and result.adapter_result.charged_amount else round(char_count * TTS_USD_PER_CHAR, 6),
        tokens_or_credits=float(char_count), asset_id=expected_output_identity,
    )

    return NarrationSegmentResult(
        beat_id=beat_id, asset_id=expected_output_identity, local_path=local_path,
        duration_seconds=duration_seconds,
        request_id=result.adapter_result.provider_job_id if result.adapter_result else f"el_{beat_id}",
        character_cost=char_count, word_timings=word_timings, alignment_source=alignment_source,
    )


def generate_narration_segment(
    beat_id: str,
    text: str,
    config: Config,
    episode_id: str,
    output_dir: Path,
    *,
    previous_request_ids: Optional[List[str]] = None,
    agent: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> NarrationSegmentResult:
    """
    Generate one narration audio segment for a Script Package beat.

    channel_id (added for the Authorization Authority gate path, plan
    §5.5/§6 Commit G -- mirrors hypogeum.muapi.submit_scene()'s own
    channel_id parameter exactly): optional. When supplied AND a real,
    verified RunAuthorizationGrant already resolves for (channel_id,
    episode_id), the call routes through hypogeum.authorization_
    authority + hypogeum.submission_gate instead of the legacy path
    below. Every existing caller omits channel_id and is completely
    unaffected; even a caller that supplies it falls straight through
    to the unchanged legacy path whenever no real grant exists.

    Calls POST v1/text-to-speech/{voice_id}/with-timestamps. Decodes the
    base64 audio and writes it to disk. Parses alignment data and reconstructs
    word timings for SRT generation. On null alignment, escalates through the
    forced_alignment → whisper_fallback cascade.

    Args:
        beat_id:              Identifier from Script Package narration_beats[].
        text:                 Narration text to synthesize.
        config:               Loaded Config instance.
        episode_id:           Episode identifier for log correlation and file paths.
        output_dir:           Episode asset directory (.hypogeum-assets/episodes/{ep_id}/).
        previous_request_ids: Up to 3 ElevenLabs request IDs from preceding beats.
                              Pass these to improve prosody continuity at segment boundaries.
        agent:                Optional agent name for log correlation.

    Returns:
        NarrationSegmentResult with audio written to disk and word_timings populated.

    Raises:
        ElevenLabsAuthFailure    — HTTP 401 (halt all generation).
        ElevenLabsQuotaExhausted — Credit exhaustion detected (halt all generation).
        ElevenLabsRateLimitError — 429 after retry.
        ElevenLabsError          — Any other generation failure.
    """
    log = get_logger(episode_id=episode_id, agent=agent or "elevenlabs")

    # Weekly reservation boundary -- established BEFORE the gate branch so
    # BOTH the grant-derived gate path and the legacy path carry it.
    #  * POST_PLAN regeneration: the plan-bound CHANNEL_WEEKLY hold
    #    (channel_id MANDATORY; no dry-run exemption).
    #  * PRE_PLAN first narration (Audio Agent runs before VPD): the ONE
    #    pre-production CHANNEL_WEEKLY hold.
    # Either way, fail closed on any weekly denial -> zero network.
    from hypogeum.production_plan import PlanLifecycle, classify_plan_lifecycle
    if classify_plan_lifecycle(episode_id, output_dir) is not PlanLifecycle.PRE_PLAN:
        # POST_PLAN regeneration -- channel_id MANDATORY (channel_id=None
        # fails closed inside ensure_verified_plan_weekly_reservation).
        from hypogeum.plan_reservation import ensure_verified_plan_weekly_reservation
        ensure_verified_plan_weekly_reservation(
            channel_id=channel_id, episode_id=episode_id, config=config, log=log,
            output_dir=output_dir,
        )
    elif channel_id:
        # PRE_PLAN first narration: the ONE pre-production CHANNEL_WEEKLY
        # hold. A channel-less pre-plan call has nothing to bind to.
        from hypogeum.preplan_reservation import ensure_preproduction_reservation
        try:
            ensure_preproduction_reservation(
                channel_id=channel_id, episode_id=episode_id, config=config, log=log,
                output_dir=output_dir,
            )
        except Exception as exc:  # noqa: BLE001 -- fail closed, zero network
            raise ElevenLabsError(f"pre_plan_reservation_denied: {exc}") from exc

    # Grant-derived gate path (Correction 3/5.5) -- mirrors hypogeum.
    # muapi.submit_scene()'s own branch exactly. Only reached when
    # channel_id is supplied AND a real, verified grant already
    # resolves for (channel_id, episode_id).
    if channel_id is not None:
        import hypogeum.authorization_authority as _authority
        grant = _authority.resolve_active_grant(channel_id, episode_id, at=datetime.now(timezone.utc).isoformat())
        if grant is not None:
            return _generate_narration_via_gate(
                channel_id=channel_id, grant=grant, beat_id=beat_id, text=text, episode_id=episode_id,
                output_dir=output_dir, config=config, log=log, previous_request_ids=previous_request_ids,
            )

    voice_id      = config.ELEVENLABS_VOICE_ID
    model_id      = config.ELEVENLABS_MODEL_ID or _DEFAULT_MODEL_ID
    output_format = config.ELEVENLABS_OUTPUT_FORMAT or _DEFAULT_OUTPUT_FORMAT
    stability     = config.ELEVENLABS_STABILITY if config.ELEVENLABS_STABILITY is not None else _DEFAULT_STABILITY
    similarity    = config.ELEVENLABS_SIMILARITY_BOOST if config.ELEVENLABS_SIMILARITY_BOOST is not None else _DEFAULT_SIMILARITY_BOOST
    timeout       = config.ELEVENLABS_TIMEOUT_SECONDS

    if not voice_id:
        raise ElevenLabsError(
            "ELEVENLABS_VOICE_ID is not set. "
            "This is a permanent channel identity lock — pin it before first production run."
        )

    log.info(
        "elevenlabs_tts_started",
        beat_id=beat_id,
        model_id=model_id,
        text_length=len(text),
        has_previous_context=bool(previous_request_ids),
    )

    url = f"{_BASE_URL}/v1/text-to-speech/{voice_id}/with-timestamps"

    body: dict = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity,
        },
    }
    if output_format:
        body["output_format"] = output_format
    if previous_request_ids:
        body["previous_request_ids"] = previous_request_ids[:3]

    headers = _auth_headers(config)
    response = _post_json_with_retry(
        url=url,
        body=body,
        headers=headers,
        timeout=timeout,
        log=log,
    )

    # Parse response
    try:
        payload = response.json()
    except Exception as exc:
        raise ElevenLabsError(
            f"Failed to parse ElevenLabs TTS response for beat {beat_id!r}: {exc}"
        )

    # Decode audio
    audio_b64 = payload.get("audio_base64", "")
    if not audio_b64:
        raise ElevenLabsError(
            f"ElevenLabs TTS response missing audio_base64 for beat {beat_id!r}"
        )

    try:
        audio_bytes = base64.b64decode(audio_b64)
    except Exception as exc:
        raise ElevenLabsError(
            f"Failed to decode base64 audio for beat {beat_id!r}: {exc}"
        )

    # Write audio to disk
    ext = _output_format_extension(output_format)
    narration_dir = output_dir / "narration"
    narration_dir.mkdir(parents=True, exist_ok=True)
    local_path = narration_dir / f"{beat_id}{ext}"
    local_path.write_bytes(audio_bytes)

    # Capture request ID from response headers for prosody chaining
    request_id = response.headers.get("request-id", f"el_{beat_id}")

    # Parse alignment — prefer normalized (text-normalized version matches audio timing)
    alignment = (
        _parse_char_alignment(payload.get("normalized_alignment"))
        or _parse_char_alignment(payload.get("alignment"))
    )

    if alignment is not None:
        word_timings = _chars_to_words(alignment)
        alignment_source = "tts"
    else:
        log.warn(
            "elevenlabs_alignment_null",
            beat_id=beat_id,
            action="escalating_to_forced_alignment",
        )
        word_timings, alignment_source = _alignment_fallback_cascade(
            beat_id=beat_id,
            audio_path=local_path,
            transcript=text,
            config=config,
            episode_id=episode_id,
            log=log,
        )

    duration_seconds = word_timings[-1].end_seconds if word_timings else 0.0
    asset_id = f"episodes/{episode_id}/narration/{beat_id}"

    char_count = len(text)
    log.info(
        "elevenlabs_tts_completed",
        beat_id=beat_id,
        asset_id=asset_id,
        duration_seconds=round(duration_seconds, 3),
        word_count=len(word_timings),
        alignment_source=alignment_source,
        character_cost=char_count,
    )
    _finance.record_cost(
        provider="elevenlabs",
        model=config.ELEVENLABS_MODEL_ID or _DEFAULT_MODEL_ID,
        call_type="narration",
        cost_usd=round(char_count * TTS_USD_PER_CHAR, 6),
        tokens_or_credits=float(char_count),
        asset_id=asset_id,
    )

    return NarrationSegmentResult(
        beat_id=beat_id,
        asset_id=asset_id,
        local_path=local_path,
        duration_seconds=duration_seconds,
        request_id=request_id,
        character_cost=char_count,
        word_timings=word_timings,
        alignment_source=alignment_source,
    )


# ---------------------------------------------------------------------------
# Internal: alignment fallback cascade
# ---------------------------------------------------------------------------

def _alignment_fallback_cascade(
    beat_id: str,
    audio_path: Path,
    transcript: str,
    config: Config,
    episode_id: str,
    log,
) -> tuple[List[WordTiming], str]:
    """
    Attempt forced_alignment, then whisper_fallback.
    Returns (word_timings, alignment_source).
    Never raises — the final fallback is alignment_source = "none" with empty timings.
    """
    # Tier 1: forced alignment
    try:
        log.info("elevenlabs_forced_alignment_start", beat_id=beat_id)
        word_timings = forced_alignment(
            beat_id=beat_id,
            audio_path=audio_path,
            transcript=transcript,
            config=config,
            episode_id=episode_id,
        )
        log.info(
            "elevenlabs_forced_alignment_done",
            beat_id=beat_id,
            word_count=len(word_timings),
        )
        return word_timings, "forced_alignment"
    except (ElevenLabsAuthFailure, ElevenLabsQuotaExhausted):
        raise  # halt conditions — propagate immediately
    except Exception as exc:
        log.warn(
            "elevenlabs_forced_alignment_failed",
            beat_id=beat_id,
            error=str(exc),
            action="escalating_to_whisper",
        )

    # Tier 2: Whisper transcription
    try:
        log.info("elevenlabs_whisper_start", beat_id=beat_id)
        word_timings = whisper_fallback(
            beat_id=beat_id,
            audio_path=audio_path,
            config=config,
            episode_id=episode_id,
        )
        log.info(
            "elevenlabs_whisper_done",
            beat_id=beat_id,
            word_count=len(word_timings),
        )
        return word_timings, "whisper"
    except Exception as exc:
        log.error(
            "elevenlabs_whisper_failed",
            beat_id=beat_id,
            error=str(exc),
            action="proceeding_without_alignment",
        )

    return [], "none"


# ---------------------------------------------------------------------------
# Public: forced_alignment
# ---------------------------------------------------------------------------

def forced_alignment(
    beat_id: str,
    audio_path: Path,
    transcript: str,
    config: Config,
    episode_id: str,
    *,
    agent: Optional[str] = None,
) -> List[WordTiming]:
    """
    Obtain word-level timestamps by posting audio + transcript to ElevenLabs
    POST v1/forced-alignment.

    Use when /with-timestamps returns null alignment data. Returns word-level
    timings directly (no character reconstruction needed).

    Args:
        beat_id:     Beat identifier for log correlation.
        audio_path:  Local path to the already-generated audio file.
        transcript:  The original narration text for alignment.
        config:      Loaded Config instance.
        episode_id:  Episode identifier for log correlation.
        agent:       Optional agent name.

    Returns:
        List[WordTiming] — may be empty if the API returns no words.

    Raises:
        ElevenLabsAuthFailure    — HTTP 401.
        ElevenLabsQuotaExhausted — Credit exhaustion.
        ElevenLabsError          — Any other failure.
    """
    log = get_logger(episode_id=episode_id, agent=agent or "elevenlabs")

    url = f"{_BASE_URL}/v1/forced-alignment"
    headers = _auth_headers(config)
    timeout = config.ELEVENLABS_TIMEOUT_SECONDS

    with open(audio_path, "rb") as audio_file:
        files = {"audio": (audio_path.name, audio_file, "audio/mpeg")}
        data  = {"text": transcript}
        response = _post_multipart_with_retry(
            url=url,
            files=files,
            data=data,
            auth_headers=headers,
            timeout=timeout,
            log=log,
        )

    try:
        payload = response.json()
    except Exception as exc:
        raise ElevenLabsError(
            f"Failed to parse forced-alignment response for beat {beat_id!r}: {exc}"
        )

    raw_words = payload.get("words", [])
    word_timings = [
        WordTiming(
            word=w.get("text", ""),
            start_seconds=float(w.get("start", 0.0)),
            end_seconds=float(w.get("end", 0.0)),
        )
        for w in raw_words
        if w.get("text", "").strip()
    ]

    return word_timings


# ---------------------------------------------------------------------------
# Public: whisper_fallback
# ---------------------------------------------------------------------------

def whisper_fallback(
    beat_id: str,
    audio_path: Path,
    config: Config,
    episode_id: str,
    *,
    agent: Optional[str] = None,
) -> List[WordTiming]:
    """
    Transcribe audio via OpenAI Whisper to recover word-level timestamps.

    Final fallback in the alignment cascade. Requires OPENAI_API_KEY.
    Uses POST https://api.openai.com/v1/audio/transcriptions with
    response_format=verbose_json and timestamp_granularities=["word"].

    Args:
        beat_id:     Beat identifier for log correlation.
        audio_path:  Local path to the audio file.
        config:      Loaded Config instance.
        episode_id:  Episode identifier for log correlation.
        agent:       Optional agent name.

    Returns:
        List[WordTiming] from Whisper word-level timestamps.

    Raises:
        ElevenLabsError — OPENAI_API_KEY not set, or Whisper call failed.
    """
    log = get_logger(episode_id=episode_id, agent=agent or "elevenlabs")

    if not config.OPENAI_API_KEY:
        raise ElevenLabsError(
            f"Whisper fallback requires OPENAI_API_KEY but it is not set "
            f"(beat {beat_id!r}). Set OPENAI_API_KEY to enable Whisper recovery."
        )

    url = f"{_OPENAI_BASE}/audio/transcriptions"
    headers = {
        "Authorization": f"Bearer {config.OPENAI_API_KEY}",
    }
    timeout = config.ELEVENLABS_TIMEOUT_SECONDS

    with open(audio_path, "rb") as audio_file:
        files = {"file": (audio_path.name, audio_file, "audio/mpeg")}
        data = {
            "model": config.WHISPER_MODEL,
            "response_format": "verbose_json",
            "timestamp_granularities[]": "word",
        }
        response = requests.post(
            url,
            files=files,
            data=data,
            headers=headers,
            timeout=timeout,
        )

    if response.status_code == 401:
        raise ElevenLabsError(
            f"Whisper authentication failed (HTTP 401) for beat {beat_id!r}. "
            "Check OPENAI_API_KEY."
        )
    if not (200 <= response.status_code < 300):
        raise ElevenLabsError(
            f"Whisper transcription failed (HTTP {response.status_code}) "
            f"for beat {beat_id!r}: {response.text[:200]}"
        )

    try:
        payload = response.json()
    except Exception as exc:
        raise ElevenLabsError(
            f"Failed to parse Whisper response for beat {beat_id!r}: {exc}"
        )

    raw_words = payload.get("words", [])
    word_timings = [
        WordTiming(
            word=w.get("word", ""),
            start_seconds=float(w.get("start", 0.0)),
            end_seconds=float(w.get("end", 0.0)),
        )
        for w in raw_words
        if w.get("word", "").strip()
    ]

    return word_timings


# ---------------------------------------------------------------------------
# Public: build_srt
# ---------------------------------------------------------------------------

def build_srt(
    segments: List[NarrationSegmentResult],
    time_offsets_seconds: Optional[List[float]] = None,
) -> str:
    """
    Build a complete SRT subtitle file from a list of narration segments.

    Each segment contributes its word_timings as a series of subtitle blocks.
    If time_offsets_seconds is provided, segment i's timestamps are shifted by
    time_offsets_seconds[i] — use this when the Audio Agent has computed exact
    positions in the stitched narration track.

    If time_offsets_seconds is None, offsets are computed sequentially:
    each segment starts immediately after the previous one ends.

    Segments with empty word_timings are skipped.

    Args:
        segments:             Ordered list of NarrationSegmentResult.
        time_offsets_seconds: Optional per-segment time offsets (seconds).

    Returns:
        Complete SRT string, or "" if no word timings are available.
    """
    if not segments:
        return ""

    # Compute sequential offsets if not provided
    if time_offsets_seconds is None:
        offsets: List[float] = []
        cumulative = 0.0
        for seg in segments:
            offsets.append(cumulative)
            cumulative += seg.duration_seconds
    else:
        offsets = list(time_offsets_seconds)

    # Collect all subtitle blocks across all segments
    all_blocks: List[tuple[float, float, str]] = []
    for seg, offset in zip(segments, offsets):
        if not seg.word_timings:
            continue
        blocks = _words_to_srt_blocks(
            words=seg.word_timings,
            time_offset=offset,
            max_words=_SRT_MAX_WORDS_PER_LINE,
        )
        all_blocks.extend(blocks)

    if not all_blocks:
        return ""

    # Apply minimum gap between consecutive blocks
    adjusted: List[tuple[float, float, str]] = []
    for i, (start, end, text) in enumerate(all_blocks):
        if adjusted:
            prev_end = adjusted[-1][1]
            if start < prev_end + _SRT_MIN_GAP_SECONDS:
                start = prev_end + _SRT_MIN_GAP_SECONDS
        adjusted.append((start, end, text))

    # Format as SRT
    lines: List[str] = []
    for idx, (start, end, text) in enumerate(adjusted, start=1):
        lines.append(str(idx))
        lines.append(
            f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}"
        )
        lines.append(text)
        lines.append("")  # blank line separator

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public: fetch_model_cost_multiplier
# ---------------------------------------------------------------------------

def fetch_model_cost_multiplier(
    model_id: str,
    config: Config,
    episode_id: Optional[str] = None,
    *,
    agent: Optional[str] = None,
) -> float:
    """
    Retrieve the character cost multiplier for a model from GET v1/models.

    Results are cached module-wide for the process lifetime to avoid
    repeated API calls within a single pipeline run.

    A multiplier of 1.0 means 1 credit per character. Turbo/Flash models
    typically return 0.5 (50% cheaper). Use this for dynamic cost accounting
    rather than hardcoded per-model pricing.

    Returns 1.0 on any failure (safe default — does not affect generation).

    Args:
        model_id:   ElevenLabs model ID to look up.
        config:     Loaded Config instance.
        episode_id: Optional episode identifier for log correlation.
        agent:      Optional agent name for log correlation.

    Returns:
        float — character cost multiplier (1.0 fallback on error).
    """
    log = get_logger(episode_id=episode_id or "unknown", agent=agent or "elevenlabs")

    if model_id in _model_cost_cache:
        return _model_cost_cache[model_id]

    if not config.ELEVENLABS_API_KEY:
        return 1.0

    url = f"{_BASE_URL}/v1/models"
    try:
        response = requests.get(
            url,
            headers={
                "xi-api-key": config.ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
            },
            timeout=config.ELEVENLABS_TIMEOUT_SECONDS,
        )
        if not (200 <= response.status_code < 300):
            log.warn(
                "elevenlabs_cost_fetch_failed",
                model_id=model_id,
                status=response.status_code,
                fallback=1.0,
            )
            return 1.0

        models = response.json()
        for m in models:
            if m.get("model_id") == model_id:
                rates = m.get("model_rates", {})
                multiplier = float(rates.get("character_cost_multiplier", 1.0))
                _model_cost_cache[model_id] = multiplier
                log.info(
                    "elevenlabs_cost_fetched",
                    model_id=model_id,
                    character_cost_multiplier=multiplier,
                )
                return multiplier

        # Model not found in list — use 1.0
        _model_cost_cache[model_id] = 1.0
        log.warn(
            "elevenlabs_cost_fetch_failed",
            model_id=model_id,
            reason="model_not_found_in_v1_models",
            fallback=1.0,
        )
        return 1.0

    except Exception as exc:
        log.warn(
            "elevenlabs_cost_fetch_failed",
            model_id=model_id,
            error=str(exc),
            fallback=1.0,
        )
        return 1.0


# ---------------------------------------------------------------------------
# Public: generate_music_track  (M4.1 — corrected per C1–C9 verified 2026-06-03)
# ---------------------------------------------------------------------------

_MUSIC_PROVIDER_OPERATION = "music.track"
_MUSIC_COMPILER_IDENTITY = "hypogeum.elevenlabs.generate_music_track"
_MUSIC_ENVELOPE_SCHEMA_VERSION = "1.0"


def _generate_music_via_gate(
    *, channel_id: str, grant, category: str, required_duration_seconds: float, episode_id: str,
    assets_root: Path, config: Config, log, tone_arc: Optional[str] = None,
) -> MusicTrackResult:
    """
    The grant-derived gate path for music generation -- mirrors
    hypogeum.elevenlabs._generate_narration_via_gate()'s structure
    (which itself mirrors hypogeum.muapi._submit_scene_via_gate()).
    Never reached unless a real, verified grant already resolves for
    (channel_id, episode_id), and only on a Channel Music Library
    cache MISS -- see generate_music_track()'s own branch, which
    performs the library search before ever calling this function, so
    a cache hit never touches the Authority or the gate at all.

    Unlike every other domain's gate path, the deterministic output
    identity (track_id, local_path) is chosen HERE, before the
    envelope is compiled, rather than being derivable from a caller-
    supplied stable id (scene_id/entity_id/beat_id) -- music has no
    such stable identity per generation event. This mirrors the legacy
    path's own uuid4() track_id generation, just moved earlier so the
    envelope can carry a deterministic path the same way every other
    domain's envelope does. Raises ElevenLabsError on any
    compile/authorize/gate-denial/quality-rejection failure, matching
    the legacy path's own exception contract.
    """
    import hashlib
    from datetime import timedelta

    import hypogeum.adapter_registry as adapter_registry
    import hypogeum.authorization_authority as authority
    import hypogeum.authorization_store as authorization_store
    import hypogeum.budget_scopes as bs
    import hypogeum.channel_config as channel_config
    import hypogeum.db as db
    import hypogeum.submission_gate as submission_gate
    import hypogeum.workflow_executor as workflow_executor
    from hypogeum.providers.elevenlabs_music_adapter import (
        SCHEMA_VERSION as _MUSIC_SCHEMA_VERSION, ElevenlabsMusicAdapter, encode_music_payload,
    )
    from hypogeum.provider_receipt import WriteAheadReceiptStore

    prompt = _build_music_prompt(category=category, tone_arc=tone_arc)
    model_id = config.ELEVENLABS_MUSIC_MODEL_ID or _MUSIC_DEFAULT_MODEL_ID

    duration_ms = int(required_duration_seconds * 1000)
    duration_ms = max(_MUSIC_MIN_DURATION_MS, min(duration_ms, _MUSIC_MAX_DURATION_MS))
    actual_duration_seconds = duration_ms / 1000.0

    track_id = str(uuid.uuid4())
    category_slug = _MUSIC_CATEGORY_SLUGS.get(category, category.lower().replace(" ", "-"))
    music_dir = assets_root / "channel" / "music" / category_slug
    local_path = music_dir / f"{category_slug}_{track_id}.mp3"
    expected_output_identity = f"channel/music/{category_slug}/{category_slug}_{track_id}"

    at = datetime.now(timezone.utc).isoformat()

    package_identity = channel_config.compute_channel_package_identity(channel_id)
    if not package_identity.get("available"):
        raise ElevenLabsError(f"channel_package_identity_unavailable for music category {category!r}")
    workflow_binding = workflow_executor.resolve_workflow_binding(
        channel_id, _HYPOGEUM_WORKFLOW_ID, _HYPOGEUM_WORKFLOW_VERSION,
    )

    plan_checksum = hashlib.sha256(_canonical_json_bytes({"category": category, "track_id": track_id})).hexdigest()
    structured_spec_checksum = hashlib.sha256(_canonical_json_bytes({
        "duration_ms": duration_ms, "force_instrumental": _MUSIC_FORCE_INSTRUMENTAL, "model_id": model_id,
    })).hexdigest()

    operation_payload_bytes = encode_music_payload(
        prompt=prompt, model_id=model_id, duration_ms=duration_ms, force_instrumental=_MUSIC_FORCE_INSTRUMENTAL,
        category=category, track_id=track_id, local_path=str(local_path), episode_id=episode_id,
    )
    operation_payload_content = operation_payload_bytes.decode("utf-8")
    operation_payload_sha256 = hashlib.sha256(operation_payload_bytes).hexdigest()

    max_cost = Decimal(str(round(actual_duration_seconds * MUSIC_USD_PER_SEC, 6)))
    authorization_id = f"{episode_id}:music:{category_slug}:{track_id}"

    # Explicit protected POST_PLAN binding (see _generate_narration_via_gate).
    from hypogeum.production_plan import canonical_episode_dir as _canon_dir  # noqa: PLC0415
    from hypogeum.production_plan import load_production_plan as _load_canon_plan  # noqa: PLC0415
    _bound_plan = _load_canon_plan(episode_id, _canon_dir(episode_id))
    _plan_audit_metadata = (
        {"production_plan_content_hash": _bound_plan.content_hash} if _bound_plan is not None else {}
    )

    try:
        envelope = GenerationExecutionEnvelope.create(
            envelope_schema_version=_MUSIC_ENVELOPE_SCHEMA_VERSION, envelope_version=1, channel_id=channel_id,
            channel_package_version=package_identity["version"], channel_package_checksum=package_identity["checksum"],
            execution_id=episode_id, workflow_version=workflow_binding["workflow_version"],
            plan_version="live", plan_checksum=plan_checksum, planned_shot_id=f"music_{category_slug}",
            structured_spec_checksum=structured_spec_checksum, compiler_identity=_MUSIC_COMPILER_IDENTITY,
            compiler_version="1", operation_payload_content=operation_payload_content,
            operation_payload_sha256=operation_payload_sha256, operation_payload_schema_version=_MUSIC_SCHEMA_VERSION,
            content_provenance={
                "compiler": _MUSIC_COMPILER_IDENTITY, "compiler_version": "1",
                "source_work_item_id": f"music_{category_slug}", "rendered_at": at,
            },
            provider=_VOICE_PROVIDER, model=model_id, provider_operation=_MUSIC_PROVIDER_OPERATION,
            deterministic_output_path=str(local_path), expected_output_identity=expected_output_identity,
            authorization_id=authorization_id, authorization_version=1, max_cost_per_call=max_cost,
            run_level_budget_ref=episode_id, episode_level_budget_ref=episode_id, currency="USD",
            retry_authorized=False, fallback_authorized=False, idempotency_key=authorization_id,
            created_at=at, expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
            dependency_checksums={
                "channel_package": package_identity["checksum"], "plan": plan_checksum,
                "structured_spec": structured_spec_checksum, "compiler": hashlib.sha256(_MUSIC_COMPILER_IDENTITY.encode()).hexdigest(),
            },
            audit_metadata=_plan_audit_metadata,
        )
        authorization = authority.issue_child_authorization(envelope, grant.grant_id, grant.grant_version, at=at)
    except Exception as exc:  # noqa: BLE001 -- any compile/authorize failure here is a clean non-network denial
        raise ElevenLabsError(f"Music gate authorization failed for category {category!r}: {exc}") from exc

    conn = db.get_connection()
    try:
        authorization_store.persist_execution_envelope(conn, envelope)

        adapter_registry.register_adapter(_VOICE_PROVIDER, model_id, _MUSIC_PROVIDER_OPERATION, "1.0", ElevenlabsMusicAdapter)
        lease_owner_id = f"elevenlabs_music_adapter:{track_id}"
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        lease = db.acquire_lease(conn, envelope.envelope_id, lease_owner_id, acquired_at=at, expires_at=lease_expires_at)
        if lease is None:
            raise ElevenLabsError(f"Music gate lease unavailable for category {category!r}")

        receipt_store = WriteAheadReceiptStore(assets_root / "episodes" / episode_id)
        live_sources = submission_gate.LiveSources(
            channel_package_checksum=package_identity["checksum"], plan_checksum=plan_checksum,
            structured_spec_checksum=structured_spec_checksum, compiler_version="1",
        )
        scopes = [
            bs.ScopeRequest(bs.BudgetScopeKind.CALL, f"music_{track_id}", max_cost, "USD"),
            bs.ScopeRequest(bs.BudgetScopeKind.EPISODE, episode_id, grant.max_total_spend_all_bindings or max_cost, "USD"),
        ]
        result = submission_gate.submit(
            envelope, live_sources=live_sources, authorization=authorization, lease_conn=conn,
            lease_owner_id=lease_owner_id, lease_fencing_token=lease["fencing_token"], receipt_store=receipt_store,
            execution_id=episode_id, attempt_id=authorization_id, budget_scopes=scopes,
            required_provider=_VOICE_PROVIDER, required_model=model_id, at=at,
            expected_output_identity=expected_output_identity, expected_deterministic_output_path=str(local_path),
            output_already_exists=local_path.exists(),
        )
    finally:
        conn.close()

    if not result.approved:
        evidence = result.adapter_result.evidence if result.adapter_result else {}
        failure_reason = evidence.get("failure_reason")
        if failure_reason == "zero_byte_audio":
            raise ElevenLabsError(
                f"ElevenLabs Music returned zero bytes for category {category!r}. PDR required for this section."
            )
        if failure_reason == "ancient_civ_low_confidence":
            raise ElevenLabsError(
                f"ElevenLabs Music: Ancient Civilizations low confidence "
                f"(audio_bytes={evidence.get('audio_bytes')} < {_MUSIC_ANCIENT_CIV_MIN_BYTES}). "
                "PDR required for this section."
            )
        raise ElevenLabsError(f"Music gate denied for category {category!r}: [{result.denied_at_check}] {result.reason}")

    evidence = result.adapter_result.evidence
    now_iso = datetime.now(timezone.utc).isoformat()
    new_entry = {
        "track_id": track_id, "category": category, "path": expected_output_identity,
        "source": "ai_generated", "provider": _VOICE_PROVIDER, "model_id": model_id,
        "duration_seconds": evidence["duration_seconds"], "prompt_used": prompt,
        "generated_at": now_iso, "usage_count": 0, "last_used_date": None,
    }
    library_path = assets_root / "channel" / "music" / _MUSIC_LIBRARY_FILENAME
    _append_to_music_library(library_path=library_path, entry=new_entry, log=log)

    _finance.record_cost(
        provider=_VOICE_PROVIDER, model=model_id, call_type="music",
        cost_usd=float(result.adapter_result.charged_amount) if result.adapter_result.charged_amount else round(evidence["duration_seconds"] * MUSIC_USD_PER_SEC, 6),
        tokens_or_credits=evidence["duration_seconds"], asset_id=expected_output_identity,
    )

    return MusicTrackResult(
        track_id=track_id, category=category, asset_id=expected_output_identity,
        local_path=Path(evidence["local_path"]), source="ai_generated",
        duration_seconds=evidence["duration_seconds"], prompt_used=prompt,
    )


def generate_music_track(
    category: str,
    required_duration_seconds: float,
    config: Config,
    episode_id: str,
    assets_root: Path,
    *,
    tone_arc: Optional[str] = None,
    agent: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> MusicTrackResult:
    """
    Source a background music track for a given category and duration.

    Library-first: searches the Channel Music Library before calling ElevenLabs.
    On a cache hit: returns the existing track without any API call.
    On a cache miss: calls POST /v1/music (C1), stores the result, updates the library.

    Args:
        category:                  Music category; one of the 6 defined categories.
        required_duration_seconds: Minimum required duration in seconds.
        config:                    Loaded Config instance.
        episode_id:                Episode identifier for log correlation.
        assets_root:               Path to .hypogeum-assets/ root directory.
        tone_arc:                  Optional episode tone arc from Visual Package
                                   episode_metadata. Appended to base prompt (≤100 chars).
        agent:                     Optional agent name for log correlation.

    Returns:
        MusicTrackResult with track stored on disk and library updated.

    Raises:
        ElevenLabsError            — Generation failed; caller must issue PDR and proceed
                                     without music. This is a non-blocking failure path.
        ElevenLabsAuthFailure      — HTTP 401 (hard failure; no further music generation).
    """
    log = get_logger(episode_id=episode_id, agent=agent or "elevenlabs_music")

    if not config.ELEVENLABS_API_KEY:
        raise ElevenLabsAuthFailure()

    library_path = assets_root / "channel" / "music" / _MUSIC_LIBRARY_FILENAME

    # ------------------------------------------------------------------
    # §5.A Library search gate
    # ------------------------------------------------------------------
    log.info(
        "elevenlabs_music_library_search",
        episode_id=episode_id,
        category=category,
        required_duration_seconds=required_duration_seconds,
    )

    hit = _search_music_library(
        library_path=library_path,
        category=category,
        required_duration_seconds=required_duration_seconds,
        assets_root=assets_root,
        log=log,
        episode_id=episode_id,
    )
    if hit is not None:
        return hit

    # Weekly reservation boundary -- established BEFORE the gate branch so
    # BOTH the grant-derived gate path and the legacy path carry it. Music
    # runs AFTER VPD on any real run, so this is normally the plan-bound
    # CHANNEL_WEEKLY hold; the PRE_PLAN branch exists only for direct
    # test/tool calls before a plan is compiled. channel_id MANDATORY when
    # supplied; no dry-run exemption; fail closed -> zero network.
    from hypogeum.production_plan import PlanLifecycle, classify_plan_lifecycle
    _music_episode_dir = assets_root / "episodes" / episode_id
    if classify_plan_lifecycle(episode_id, _music_episode_dir) is not PlanLifecycle.PRE_PLAN:
        from hypogeum.plan_reservation import ensure_verified_plan_weekly_reservation
        ensure_verified_plan_weekly_reservation(
            channel_id=channel_id, episode_id=episode_id, config=config, log=log,
            output_dir=_music_episode_dir,
        )
    elif channel_id:
        from hypogeum.preplan_reservation import ensure_preproduction_reservation
        try:
            ensure_preproduction_reservation(
                channel_id=channel_id, episode_id=episode_id, config=config, log=log,
                output_dir=_music_episode_dir,
            )
        except Exception as exc:  # noqa: BLE001 -- fail closed, zero network
            raise ElevenLabsError(f"pre_plan_reservation_denied: {exc}") from exc

    # Grant-derived gate path (Correction 3/5.5). Only reached on a cache
    # MISS, when channel_id is supplied AND a real, verified grant already
    # resolves for (channel_id, episode_id).
    if channel_id is not None:
        import hypogeum.authorization_authority as _authority
        grant = _authority.resolve_active_grant(channel_id, episode_id, at=datetime.now(timezone.utc).isoformat())
        if grant is not None:
            return _generate_music_via_gate(
                channel_id=channel_id, grant=grant, category=category,
                required_duration_seconds=required_duration_seconds, episode_id=episode_id,
                assets_root=assets_root, config=config, log=log, tone_arc=tone_arc,
            )

    # ------------------------------------------------------------------
    # Cache miss — generate via ElevenLabs Music POST /v1/music
    # ------------------------------------------------------------------
    prompt = _build_music_prompt(category=category, tone_arc=tone_arc)
    model_id = config.ELEVENLABS_MUSIC_MODEL_ID or _MUSIC_DEFAULT_MODEL_ID

    # C3: Convert seconds to milliseconds; clamp to API range
    duration_ms = int(required_duration_seconds * 1000)
    if duration_ms < _MUSIC_MIN_DURATION_MS:
        duration_ms = _MUSIC_MIN_DURATION_MS
    if duration_ms > _MUSIC_MAX_DURATION_MS:
        duration_ms = _MUSIC_MAX_DURATION_MS

    # Fixed 2026-07-19: cost recording, library metadata, and the returned
    # MusicTrackResult must reflect the actual clamped/generated duration,
    # never the pre-clamp required_duration_seconds request. Using the
    # unclamped value here previously (a) overstated cost_usd by up to ~3x
    # whenever a request exceeded _MUSIC_MAX_DURATION_MS, and (b) wrote a
    # false duration_seconds into music-library.json, which
    # _search_music_library()'s own `duration_seconds >= required_duration`
    # filter then trusted at face value — a stored 300s file falsely
    # eligible for a future request up to whatever the original (wrong,
    # possibly much larger) value claimed. required_duration_seconds
    # remains correct to use for the ORIGINAL request itself (already sent
    # above) and for search-side comparisons elsewhere; only the
    # downstream bookkeeping of what was actually generated changes here.
    actual_duration_seconds = duration_ms / 1000.0

    body = {
        "prompt": prompt,          # C2: 'prompt' not 'text'
        "music_length_ms": duration_ms,  # C3: ms not duration_seconds
        "model_id": model_id,      # C6: music_v1
        "force_instrumental": _MUSIC_FORCE_INSTRUMENTAL,  # C5: no vocals
    }
    # C4: prompt_influence is NOT included (not a music API parameter)

    headers = _auth_headers(config)
    url = f"{_BASE_URL}{_MUSIC_ENDPOINT}"  # C1: /v1/music

    log.info(
        "elevenlabs_music_generation_start",
        episode_id=episode_id,
        category=category,
        duration_ms=duration_ms,
        prompt_length_chars=len(prompt),
        model_id=model_id,
    )

    # Use music-specific retry (§8: max 1, initial 5s, multiplier 3, jitter ±20%)
    audio_bytes = _post_music_with_retry(
        url=url,
        body=body,
        headers=headers,
        timeout=config.ELEVENLABS_TIMEOUT_SECONDS,
        log=log,
        episode_id=episode_id,
        category=category,
    )

    # ------------------------------------------------------------------
    # Validation (C8: Option A — byte length only in V1)
    # ------------------------------------------------------------------
    if len(audio_bytes) == 0:
        log.warn(
            "elevenlabs_music_validation_fail",
            episode_id=episode_id,
            category=category,
            failure_reason="zero_byte_audio",
            audio_bytes=0,
        )
        raise ElevenLabsError(
            f"ElevenLabs Music returned zero bytes for category {category!r}. "
            "PDR required for this section."
        )

    # §6.B Ancient Civilizations elevated confidence check (C8: byte-only check)
    if category == "Ancient Civilizations":
        if len(audio_bytes) < _MUSIC_ANCIENT_CIV_MIN_BYTES:
            log.warn(
                "elevenlabs_music_validation_fail",
                episode_id=episode_id,
                category=category,
                failure_reason="ancient_civ_low_confidence",
                audio_bytes=len(audio_bytes),
                min_required_bytes=_MUSIC_ANCIENT_CIV_MIN_BYTES,
            )
            raise ElevenLabsError(
                f"ElevenLabs Music: Ancient Civilizations low confidence "
                f"(audio_bytes={len(audio_bytes)} < {_MUSIC_ANCIENT_CIV_MIN_BYTES}). "
                "PDR required for this section."
            )

    log.info(
        "elevenlabs_music_generation_done",
        episode_id=episode_id,
        category=category,
        audio_bytes=len(audio_bytes),
        duration_ms=duration_ms,
    )

    # ------------------------------------------------------------------
    # §10.A Asset storage — channel-scoped, not episode-scoped
    # ------------------------------------------------------------------
    track_id = str(uuid.uuid4())
    category_slug = _MUSIC_CATEGORY_SLUGS.get(category, category.lower().replace(" ", "-"))
    music_dir = assets_root / "channel" / "music" / category_slug
    music_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{category_slug}_{track_id}.mp3"
    local_path = music_dir / filename
    local_path.write_bytes(audio_bytes)

    asset_id = f"channel/music/{category_slug}/{category_slug}_{track_id}"

    log.info(
        "elevenlabs_music_library_written",
        episode_id=episode_id,
        track_id=track_id,
        category=category,
        provider="elevenlabs",
        duration_seconds=actual_duration_seconds,
        path=asset_id,
        audio_bytes=len(audio_bytes),
    )

    # ------------------------------------------------------------------
    # §10.B music-library.json update (atomic write: temp + rename)
    # ------------------------------------------------------------------
    now_iso = datetime.now(timezone.utc).isoformat()
    new_entry = {
        "track_id": track_id,
        "category": category,
        "path": asset_id,
        "source": "ai_generated",
        "provider": "elevenlabs",
        "model_id": model_id,
        "duration_seconds": actual_duration_seconds,
        "prompt_used": prompt,
        "generated_at": now_iso,
        "usage_count": 0,
        "last_used_date": None,
    }
    _append_to_music_library(library_path=library_path, entry=new_entry, log=log)

    _finance.record_cost(
        provider="elevenlabs",
        model=model_id,
        call_type="music",
        cost_usd=round(actual_duration_seconds * MUSIC_USD_PER_SEC, 6),
        tokens_or_credits=actual_duration_seconds,
        asset_id=asset_id,
    )

    return MusicTrackResult(
        track_id=track_id,
        category=category,
        asset_id=asset_id,
        local_path=local_path,
        source="ai_generated",
        duration_seconds=actual_duration_seconds,
        prompt_used=prompt,
    )


# ---------------------------------------------------------------------------
# Internal: music library helpers
# ---------------------------------------------------------------------------

def _probe_audio_duration_seconds(path: Path) -> Optional[float]:
    """
    Return the real on-disk duration of an audio file via ffprobe's
    container-format duration, or None if the file is missing, unreadable,
    or ffprobe itself is unavailable.

    Added 2026-07-19 as the defensive half of the duration-metadata fix:
    even though generate_music_track() now writes accurate duration_seconds
    at creation time (see actual_duration_seconds above), a stale entry from
    before that fix, or metadata corrupted by any other future cause,
    must never be enough on its own to produce a false cache hit — see
    _search_music_library()'s verification loop below.
    """
    if not path.exists():
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                str(path),
            ],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except (ValueError, TypeError):
        return None


def _search_music_library(
    library_path: Path,
    category: str,
    required_duration_seconds: float,
    assets_root: Path,
    log,
    episode_id: str,
) -> Optional[MusicTrackResult]:
    """
    Search the music library for a suitable track.
    Returns MusicTrackResult on hit; None on miss.
    Updates usage_count and last_used_date on hit.

    Trusts stored duration_seconds metadata as the source of truth at read
    time (2026-07-19 decision). generate_music_track() writes accurate
    duration_seconds at creation time (the actual clamped/generated
    duration, never the pre-clamp request — see that function's own
    comments), so metadata is correct-by-construction going forward.
    Historical entries written before that fix are corrected once via
    scripts/repair_music_library_durations.py (uses the same
    _probe_audio_duration_seconds() helper), not on every lookup — an
    ffprobe subprocess call per candidate on every cache search was
    evaluated and rejected as unnecessary defense-in-depth for a bug the
    write-time fix already prevents; see that script's module docstring
    for the full reasoning.
    """
    library = _load_music_library(library_path)
    tracks = library.get("tracks", [])

    # Filter: matching category and sufficient duration
    candidates = [
        t for t in tracks
        if t.get("category") == category
        and float(t.get("duration_seconds", 0)) >= required_duration_seconds
    ]

    if not candidates:
        log.info(
            "elevenlabs_music_library_search",
            episode_id=episode_id,
            category=category,
            required_duration_seconds=required_duration_seconds,
            result="miss",
        )
        return None

    # §5.A: prefer least recently used
    def _sort_key(t):
        d = t.get("last_used_date")
        if d is None:
            return ""
        return d

    candidates.sort(key=_sort_key)
    chosen = candidates[0]
    track_id = chosen["track_id"]

    # Update usage stats
    chosen["usage_count"] = int(chosen.get("usage_count", 0)) + 1
    chosen["last_used_date"] = datetime.now(timezone.utc).isoformat()

    _write_music_library(library_path=library_path, library=library)

    category_slug = _MUSIC_CATEGORY_SLUGS.get(category, category.lower().replace(" ", "-"))
    asset_id = chosen.get("path", f"channel/music/{category_slug}/{category_slug}_{track_id}")
    local_path = assets_root / f"{asset_id}.mp3"

    log.info(
        "elevenlabs_music_library_search",
        episode_id=episode_id,
        category=category,
        required_duration_seconds=required_duration_seconds,
        result="hit",
        track_id=track_id,
    )

    log.info(
        "elevenlabs_music_library_written",
        episode_id=episode_id,
        track_id=track_id,
        action="usage_count_incremented",
        new_usage_count=chosen["usage_count"],
    )

    return MusicTrackResult(
        track_id=track_id,
        category=category,
        asset_id=asset_id,
        local_path=local_path,
        source="channel_library",
        duration_seconds=float(chosen.get("duration_seconds", 0)),
        prompt_used=None,
    )


def _load_music_library(library_path: Path) -> dict:
    """Load music-library.json; return empty library if file missing or corrupt."""
    if not library_path.exists():
        return {"schema_version": "1", "last_updated": "", "tracks": []}
    try:
        data = json.loads(library_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "tracks" not in data:
            return {"schema_version": "1", "last_updated": "", "tracks": []}
        return data
    except Exception:
        return {"schema_version": "1", "last_updated": "", "tracks": []}


def _write_music_library(library_path: Path, library: dict) -> None:
    """Write library to a temp file then rename (atomic write, §10.B)."""
    library["last_updated"] = datetime.now(timezone.utc).isoformat()
    library_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = library_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(library, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(library_path)


def _append_to_music_library(library_path: Path, entry: dict, log) -> None:
    """Load, append entry, and write atomically."""
    library = _load_music_library(library_path)
    library.setdefault("tracks", []).append(entry)
    _write_music_library(library_path=library_path, library=library)
    log.info(
        "elevenlabs_music_library_written",
        track_id=entry["track_id"],
        action="added",
    )


def repair_music_library_durations(
    library_path: Path,
    assets_root: Path,
    log,
    dry_run: bool = True,
) -> dict:
    """
    One-time repair pass for music-library.json entries whose stored
    duration_seconds does not match their real on-disk file duration.

    Added 2026-07-19 alongside the fix to generate_music_track() (which now
    writes accurate duration_seconds at creation time going forward). This
    function is the migration path for entries written before that fix —
    it is the ONLY place in the module that probes real file duration via
    ffprobe; _search_music_library() trusts metadata at read time by
    design (evaluated and decided 2026-07-19: an ffprobe subprocess call
    on every cache-lookup candidate was rejected as unnecessary
    defense-in-depth for a bug the write-time fix already prevents, at the
    cost of adding a new subprocess dependency/failure mode to a
    previously fast, pure-JSON hot-ish path). Run this script once after
    upgrading, or periodically as an audit/debug tool -- never as part of
    the normal generation or cache-lookup path.

    Args:
        library_path: Path to music-library.json.
        assets_root:  Root of .hypogeum-assets/ tree (for resolving each
                      track's real file path).
        log:          Logger instance.
        dry_run:      If True (default), reports mismatches without
                      writing any changes. Set False to apply corrections.

    Returns:
        {
            "total_tracks": int,
            "checked": int,
            "mismatched": list[dict]  # {track_id, claimed, real, path}
            "unreadable": list[str]   # track_ids whose file could not be probed
            "repaired": int,          # count actually corrected (0 if dry_run)
        }
    """
    library = _load_music_library(library_path)
    tracks = library.get("tracks", [])

    mismatched: list = []
    unreadable: list = []
    repaired = 0

    for track in tracks:
        asset_id = track.get("path", "")
        file_path = assets_root / f"{asset_id}.mp3"
        real_duration = _probe_audio_duration_seconds(file_path)

        if real_duration is None:
            unreadable.append(track.get("track_id", "?"))
            log.warn(
                "elevenlabs_music_repair_unreadable",
                track_id=track.get("track_id"),
                path=str(file_path),
            )
            continue

        claimed_duration = float(track.get("duration_seconds", 0))
        if abs(real_duration - claimed_duration) > 1.0:  # >1s tolerance for encoding jitter
            mismatched.append({
                "track_id": track.get("track_id"),
                "claimed_duration_seconds": claimed_duration,
                "real_duration_seconds": real_duration,
                "path": str(file_path),
            })
            log.warn(
                "elevenlabs_music_repair_mismatch",
                track_id=track.get("track_id"),
                claimed_duration_seconds=claimed_duration,
                real_duration_seconds=real_duration,
                dry_run=dry_run,
            )
            if not dry_run:
                track["duration_seconds"] = real_duration
                repaired += 1

    if not dry_run and repaired > 0:
        _write_music_library(library_path=library_path, library=library)

    result = {
        "total_tracks": len(tracks),
        "checked": len(tracks),
        "mismatched": mismatched,
        "unreadable": unreadable,
        "repaired": repaired,
    }
    log.info("elevenlabs_music_repair_completed", dry_run=dry_run, **{
        k: (v if not isinstance(v, list) else len(v)) for k, v in result.items()
    })
    return result


def _build_music_prompt(category: str, tone_arc: Optional[str]) -> str:
    """
    Construct the music generation prompt from category + optional tone arc.
    Tone arc extension capped at _TONE_ARC_MAX_CHARS per §15 security consideration.
    """
    base = _MUSIC_CATEGORY_PROMPTS.get(category, category)
    if not tone_arc or not tone_arc.strip():
        return base
    arc = tone_arc.strip()[:_TONE_ARC_MAX_CHARS]
    return f"{base} Episode tone: {arc}"


def _post_music_with_retry(
    url: str,
    body: dict,
    headers: dict,
    timeout: int,
    log,
    episode_id: str,
    category: str,
) -> bytes:
    """
    POST to /v1/music with one retry on 429 / 5xx (§8 music retry policy).
    Returns audio bytes on success.
    Raises ElevenLabsError on hard failure or retry exhaustion.
    Raises ElevenLabsAuthFailure on 401.
    """
    _MUSIC_SOFT_RETRY_CODES = frozenset({429, 500, 503})
    _MUSIC_HARD_FAIL_CODES  = frozenset({400, 401, 402, 403, 422})

    for attempt in range(2):  # max 1 retry = 2 attempts
        try:
            response = requests.post(
                url,
                json=body,
                headers=headers,
                timeout=timeout,
                stream=True,
            )
        except Exception as exc:
            # Network timeout / connection error — treat as soft retry on attempt 0
            if attempt == 0:
                delay = _retry_delay(attempt)
                log.warn(
                    "elevenlabs_music_retry",
                    episode_id=episode_id,
                    category=category,
                    attempt=attempt + 1,
                    trigger="network_error",
                    delay_seconds=round(delay, 2),
                    error=str(exc),
                )
                time.sleep(delay)
                continue
            raise ElevenLabsError(
                f"ElevenLabs Music network error after retry for {category!r}: {exc}"
            )

        if response.status_code == 401:
            log.error(
                "elevenlabs_music_hard_failure",
                episode_id=episode_id,
                category=category,
                http_status=401,
                error_class="auth_failure",
                action="pdr_issued",
            )
            raise ElevenLabsAuthFailure()

        if response.status_code in _MUSIC_HARD_FAIL_CODES:
            log.error(
                "elevenlabs_music_hard_failure",
                episode_id=episode_id,
                category=category,
                http_status=response.status_code,
                error_class="hard_failure",
                action="pdr_issued",
            )
            raise ElevenLabsError(
                f"ElevenLabs Music hard failure (HTTP {response.status_code}) "
                f"for category {category!r}: {response.text[:200]}"
            )

        if response.status_code in _MUSIC_SOFT_RETRY_CODES:
            if attempt == 0:
                retry_after_header = response.headers.get("Retry-After")
                retry_after = float(retry_after_header) if retry_after_header else None
                delay = _retry_delay(attempt, retry_after)
                log.warn(
                    "elevenlabs_music_retry",
                    episode_id=episode_id,
                    category=category,
                    attempt=attempt + 1,
                    trigger_status_code=response.status_code,
                    delay_seconds=round(delay, 2),
                )
                time.sleep(delay)
                continue
            raise ElevenLabsError(
                f"ElevenLabs Music server error (HTTP {response.status_code}) "
                f"after retry for {category!r}."
            )

        if 200 <= response.status_code < 300:
            # C7: Response is always binary audio — read directly from body
            return response.content

        raise ElevenLabsError(
            f"ElevenLabs Music unexpected response (HTTP {response.status_code}) "
            f"for {category!r}: {response.text[:200]}"
        )

    raise ElevenLabsError(  # pragma: no cover
        f"ElevenLabs Music request failed after retry for {category!r}."
    )
