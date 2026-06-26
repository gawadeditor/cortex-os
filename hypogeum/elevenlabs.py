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
import time
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import requests

from hypogeum.config import Config
from hypogeum.logger import get_logger
import hypogeum.finance as _finance
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
_TTS_USD_PER_CHAR   = 3e-4
# music_v1: approximate per-second cost for generated music.
_MUSIC_USD_PER_SEC  = 0.02

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

def generate_narration_segment(
    beat_id: str,
    text: str,
    config: Config,
    episode_id: str,
    output_dir: Path,
    *,
    previous_request_ids: Optional[List[str]] = None,
    agent: Optional[str] = None,
) -> NarrationSegmentResult:
    """
    Generate one narration audio segment for a Script Package beat.

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
        cost_usd=round(char_count * _TTS_USD_PER_CHAR, 6),
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

def generate_music_track(
    category: str,
    required_duration_seconds: float,
    config: Config,
    episode_id: str,
    assets_root: Path,
    *,
    tone_arc: Optional[str] = None,
    agent: Optional[str] = None,
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
        duration_seconds=required_duration_seconds,
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
        "duration_seconds": required_duration_seconds,
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
        cost_usd=round(required_duration_seconds * _MUSIC_USD_PER_SEC, 6),
        tokens_or_credits=required_duration_seconds,
        asset_id=asset_id,
    )

    return MusicTrackResult(
        track_id=track_id,
        category=category,
        asset_id=asset_id,
        local_path=local_path,
        source="ai_generated",
        duration_seconds=required_duration_seconds,
        prompt_used=prompt,
    )


# ---------------------------------------------------------------------------
# Internal: music library helpers
# ---------------------------------------------------------------------------

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
