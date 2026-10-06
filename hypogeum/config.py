"""
hypogeum/config.py — Environment configuration loader

Single read point for all environment variables in the hypogeum-brain pipeline.
All pipeline code must import Config from this module.
No agent, client, or utility may call os.getenv() directly.

Loading sequence:
  1. Parse .env file from project root (does NOT override existing env vars)
  2. Type-cast and validate all known variables
  3. Fail immediately with a clear error if any always-required variable is absent
  4. Expose validate_for_milestone(milestone) for pre-flight checks at each milestone boundary

Model slugs (confirmed T0.1 — 2026-06-01):
  Sonnet 4.6 → anthropic/claude-sonnet-4.6
  Opus 4.6   → anthropic/claude-opus-4.6
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Project root (parent of this file's parent directory)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Confirmed model slugs from T0.1 (GET https://openrouter.ai/api/v1/models, 2026-06-01)
_DEFAULT_SONNET_SLUG = "anthropic/claude-sonnet-4.6"
_DEFAULT_OPUS_SLUG   = "anthropic/claude-opus-4.6"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing."""
    def __init__(self, name: str, purpose: str) -> None:
        self.var_name = name
        super().__init__(
            f"\n\n"
            f"ConfigError: Required environment variable '{name}' is not set.\n"
            f"  Purpose: {purpose}\n"
            f"  Action:  Copy .env.example to .env and populate '{name}' before running.\n"
        )


class ConfigTypeError(RuntimeError):
    """Raised when an environment variable cannot be parsed to its expected type."""
    def __init__(self, name: str, expected: str, got: str) -> None:
        super().__init__(
            f"\n\n"
            f"ConfigTypeError: Environment variable '{name}' has an invalid value.\n"
            f"  Expected: {expected}\n"
            f"  Got:      {got!r}\n"
        )


class MilestoneConfigError(RuntimeError):
    """Raised by validate_for_milestone() when milestone-required variables are absent."""
    def __init__(self, milestone: str, missing: list[tuple[str, str]]) -> None:
        lines = "\n".join(f"  - {name}: {purpose}" for name, purpose in missing)
        super().__init__(
            f"\n\n"
            f"MilestoneConfigError: Cannot start milestone {milestone} — "
            f"required variables are not set:\n"
            f"{lines}\n"
            f"  Action: Populate these variables in .env before running {milestone}.\n"
        )


# ---------------------------------------------------------------------------
# Milestone requirements table
# Each entry: (attribute_name, human-readable purpose shown in error messages)
# Only hard-required variables appear here. Optional and conditional vars are omitted.
# TikTok vars are not listed — pipeline degrades gracefully; use Config.tiktok_available.
# OPENAI_API_KEY is not listed — only needed if Whisper fallback fires at runtime,
# or if THUMBNAIL_IMAGE_PROVIDER=openai for direct image generation.
# ---------------------------------------------------------------------------

_MILESTONE_REQUIREMENTS: dict[str, list[tuple[str, str]]] = {
    "M0": [
        ("OPENROUTER_API_KEY", "Routes all LLM calls via OpenRouter (required by all 9 agents)"),
    ],
    "M1": [
        ("OPENROUTER_API_KEY", "Routes all LLM calls via OpenRouter"),
        ("PERPLEXITY_API_KEY", "Research Director — web discovery queries"),
    ],
    "M2": [
        ("OPENROUTER_API_KEY", "Routes all LLM calls via OpenRouter"),
        ("PERPLEXITY_API_KEY", "Research Director — web discovery queries"),
        ("MUAPI_API_KEY",      "Visual Package Director — video clip generation"),
    ],
    "M3": [
        ("OPENROUTER_API_KEY",    "Routes all LLM calls via OpenRouter"),
        ("PERPLEXITY_API_KEY",    "Research Director — web discovery queries"),
        ("ELEVENLABS_API_KEY",    "Audio Agent — narration generation"),
        ("ELEVENLABS_VOICE_ID",   "Audio Agent — pinned channel voice (irreversible identity decision)"),
    ],
    "M4": [
        ("OPENROUTER_API_KEY",    "Routes all LLM calls via OpenRouter"),
        ("PERPLEXITY_API_KEY",    "Research Director — web discovery queries"),
        ("MUAPI_API_KEY",         "Visual Package Director — video clip generation"),
        ("ELEVENLABS_API_KEY",    "Audio Agent (narration) + Assembly Agent (music generation)"),
        ("ELEVENLABS_VOICE_ID",   "Audio Agent — pinned channel voice"),
    ],
    "M5": [
        ("OPENROUTER_API_KEY",        "Routes all LLM calls via OpenRouter"),
        ("PERPLEXITY_API_KEY",        "Research Director — web discovery queries"),
        ("MUAPI_API_KEY",             "Visual Package Director — video clip generation"),
        ("ELEVENLABS_API_KEY",        "Audio Agent + Assembly Agent"),
        ("ELEVENLABS_VOICE_ID",       "Audio Agent — pinned channel voice"),
        ("YOUTUBE_CLIENT_ID",         "Publishing Agent — YouTube OAuth 2.0"),
        ("YOUTUBE_CLIENT_SECRET",     "Publishing Agent — YouTube OAuth 2.0"),
        ("YOUTUBE_REFRESH_TOKEN",     "Publishing Agent — YouTube offline access token"),
        ("YOUTUBE_CHANNEL_ID",        "Publishing Agent — target YouTube channel"),
        ("META_APP_ID",               "Publishing Agent — Meta Graph API authentication"),
        ("META_APP_SECRET",           "Publishing Agent — Meta Graph API authentication"),
        ("META_ACCESS_TOKEN",         "Publishing Agent — Instagram Reels + Facebook Video"),
        ("META_INSTAGRAM_ACCOUNT_ID", "Publishing Agent — Instagram Reels upload target"),
        ("META_FACEBOOK_PAGE_ID",     "Publishing Agent — Facebook Video upload target"),
        ("META_STAGING_BASE_URL",     "Publishing Agent — public URL staging for Instagram Reels"),
    ],
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_dotenv(path: Path) -> None:
    """
    Parse a .env file and populate os.environ for any key not already set.
    Does NOT override variables present in the actual environment.
    Handles: blank lines, # comments, inline # comments, quoted values.
    No external dependencies.
    """
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip inline comment (space then #)
            if " #" in value:
                value = value[: value.index(" #")].strip()
            # Strip matching surrounding quotes
            if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


def _get_required(name: str, purpose: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(name, purpose)
    return value


def _get_optional(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name, "").strip()
    return value if value else default


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigTypeError(name, "integer (e.g. 120)", raw)


def _get_float(name: str, default: Optional[float] = None) -> Optional[float]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigTypeError(name, "float between 0.0 and 1.0 (e.g. 0.75)", raw)


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise ConfigTypeError(name, "boolean (true/false, 1/0, yes/no)", raw)


# ---------------------------------------------------------------------------
# Config class
# ---------------------------------------------------------------------------

class Config:
    """
    Typed, validated environment configuration.

    Instantiate once at pipeline startup. All pipeline code accesses
    environment variables via attributes on this object — never via os.getenv().

    Always-required variables (OPENROUTER_API_KEY) are validated on __init__.
    Milestone-gated variables are validated lazily via validate_for_milestone().
    """

    # ------------------------------------------------------------------
    # OpenRouter (always required at M0+)
    # ------------------------------------------------------------------
    OPENROUTER_API_KEY: str
    OPENROUTER_SONNET_MODEL_ID: str    # default: confirmed T0.1 slug
    OPENROUTER_OPUS_MODEL_ID: str      # default: confirmed T0.1 slug
    OPENROUTER_APP_NAME: str
    OPENROUTER_APP_URL: Optional[str]
    OPENROUTER_FALLBACK_MODEL: str      # default: anthropic/claude-sonnet-4.6 (2026-08-04 —
                                         # must never equal any agent's own primary model; a
                                         # "fallback" that retries the same model provides zero
                                         # real redundancy — see openrouter.py's own docstring
                                         # and tests/test_openrouter.py::
                                         # TestThumbnailDirectorFallbackConfiguration)
    OPENROUTER_IMAGE_MODEL: str        # default: openai/gpt-5-image-mini (THUMBNAIL_IMAGE_PROVIDER=openrouter)
    ANTHROPIC_API_KEY: Optional[str]  # Required when OpenRouter fallback is triggered

    # ------------------------------------------------------------------
    # Perplexity (required at M1+)
    # ------------------------------------------------------------------
    PERPLEXITY_API_KEY: Optional[str]
    PERPLEXITY_MODEL: str              # default: sonar-pro
    PERPLEXITY_MAX_TOKENS: Optional[int]

    # ------------------------------------------------------------------
    # MUAPI (required at M2+)
    # ------------------------------------------------------------------
    MUAPI_API_KEY: Optional[str]
    MUAPI_BASE_URL: str                       # default: https://api.muapi.ai/api/v1
    MUAPI_T1_MODEL: str                       # default: veo3-text-to-video ($2.50/gen, premium)
    MUAPI_T2_MODEL: str                       # default: veo3.1-lite-text-to-video ($0.39/gen, primary production tier)
    MUAPI_T3_MODEL: str                       # default: veo3.1-lite-text-to-video ($0.39/gen, cheap-mode alias)
    MUAPI_VIDEO_RESOLUTION: str               # default: 1080p (sent explicitly for veo3.1 models)
    MUAPI_I2V_MODEL: str                      # default: veo3-image-to-video
    MUAPI_IMAGE_MODEL: str                    # legacy alias — use THUMBNAIL_IMAGE_MODEL
    THUMBNAIL_IMAGE_PROVIDER: str             # default: muapi (primary — canonical GPT Image 2 route;
                                               # see docs/validation/thumbnail_image_generation_validation/
                                               # GPT_IMAGE_2_ROUTING_AUDIT.md)
    THUMBNAIL_IMAGE_MODEL: str               # default: gpt-image-2-text-to-image (MUAPI path only)
    THUMBNAIL_SHORT_CANDIDATE_CAP: int        # default: 3 — Factory Core conservative maximum-bound
                                               # cap for Shorts' thumbnail candidate count (schema has
                                               # no upper bound; real count is decided by Thumbnail
                                               # Director's own Phase-1 planning, which has not run yet
                                               # when hypogeum.production_plan compiles — see that
                                               # module's own docstring)
    THUMBNAIL_IMAGE_RESOLUTION: str          # default: 2K (GPT Image 2 only)
    THUMBNAIL_IMAGE_QUALITY: str             # default: high (GPT Image 2 only)
    THUMBNAIL_FALLBACK_PROVIDER: str         # default: openrouter (fallback #1 when primary fails)
    THUMBNAIL_FALLBACK2_PROVIDER: str        # default: openai     (fallback #2 when fallback #1 fails)
    THUMBNAIL_FALLBACK_MODEL: str            # default: flux-2-dev (MUAPI-internal model fallback)
    MUAPI_DEFAULT_TIER: int                   # default: 2 (VPD routing fallback)
    MUAPI_TIMEOUT_SECONDS: int                # default: 180 (Veo3 Premium took 128.5s in benchmark)
    MUAPI_POLL_INTERVAL_SECONDS: int          # default: 5
    MUAPI_MAX_POLL_ATTEMPTS: int              # default: 24
    MUAPI_QUOTA_RETRY_MAX_ATTEMPTS: int       # default: 3 (real HTTP 402 -- bounded retry, allows operator-managed Auto Reload to complete)
    MUAPI_QUOTA_RETRY_DELAY_SECONDS: float    # default: 60.0 (seconds between real HTTP 402 retries)
    CINEMATIC_DIRECTOR_MAX_RETRIES: int       # default: 2 (bounded transient-retry allowance for automatic batched cinematic planning; exhausted retries fail closed -- never a duration-blind fallback plan)
    MUAPI_WEBHOOK_URL: Optional[str]
    MUAPI_CREDIT_WARNING_THRESHOLD: Optional[float]  # USD; default: None (off)
    MUAPI_HOURLY_SPEND_LIMIT_USD: Optional[float]  # USD/hour; default 45.0 — runaway-spend breaker (0/None = off)
    MUAPI_CONSECUTIVE_FAILURE_LIMIT: int  # default 3 — consecutive failed runs before the breaker trips (0 = off)
    MAX_EPISODE_TOTAL_FAILURES: int  # default 3 — hard per-episode attempt ceiling in queue.process_next (0 = off)

    # ------------------------------------------------------------------
    # Source Enhancement Stage (2026-08-02) — provider-agnostic. Never
    # reference MUAPI here; hypogeum/source_enhancement.py only ever talks
    # to hypogeum/providers/enhancement.py's abstraction. Which concrete
    # provider that resolves to is this ENHANCEMENT_PROVIDER string.
    # ------------------------------------------------------------------
    ENHANCEMENT_ENABLED: bool                 # default: true
    ENHANCEMENT_PROVIDER: str                 # default: muapi_ai_video_upscaler (registry key)
    ENHANCEMENT_REQUIRED_WIDTH: int           # default: 1920 (source clip pixel requirement)
    ENHANCEMENT_REQUIRED_HEIGHT: int          # default: 1080
    ENHANCEMENT_TARGET_RESOLUTION_LABEL: str  # default: 1080p (provider-facing target label)

    # M5.6 — Cost guardrails
    MUAPI_EPISODE_BUDGET_CREDITS: float   # 0 = unlimited; default: 0
    MUAPI_SCENE_BUDGET_CREDITS: float     # 0 = unlimited; default: 0
    MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE: int  # 0 = unlimited; default: 0
    EPISODE_ZERO_MAX_SCENES: int          # 0 = disabled; default: 0
    MUAPI_CHEAP_MODE: bool                # skip T1/T2, use T3 only; default: false
    MUAPI_RUNWAY_ONLY_MODE: bool          # force T2 for all video scenes (T2=veo3-fast since 2026-06-27); default: false
    MUAPI_DRY_RUN: bool                   # no real API calls; default: false
    MUAPI_ALLOW_PAID_FALLBACKS: bool      # allow automatic paid fallback; default: false

    # Queue auto-retry (Roadmap priority 3 — recovery hardening)
    QUEUE_MAX_AUTO_RETRIES: int           # default: 2 (3 total attempts including original)
    QUEUE_RETRY_BACKOFF_MINUTES: str      # comma-separated minutes per retry; default: "5,20"
    ENABLE_DEDICATED_SHORT_GENERATION: bool  # allow VPD to plan/generate 9:16 Short scenes; default: false

    # Short-Native production mode — one vertical ~45-60s deliverable per run
    # (no long-form arc, no derived Shorts). Env-forced for tests/manual runs;
    # run_pipeline also turns it on per-run for a shorts-only channel.
    SHORT_NATIVE_MODE: bool                 # default: false
    SHORT_NATIVE_MAX_SCENES: int            # scene cap in short-native runs; default: 8 (one per beat)
    SHORT_NATIVE_MAX_NARRATION_WORDS: int   # total narration budget; default: 150
    SHORT_NATIVE_ASPECT_RATIO: str          # deliverable aspect ratio; default: "9:16"
    SHORT_NATIVE_MAX_BEAT_SECONDS: float    # per-beat narration ceiling so one 8s clip covers it; default: 8.5
    SHORT_NATIVE_WORDS_PER_SECOND: float    # narration-rate estimate for the per-beat budget; default: 2.9

    # ------------------------------------------------------------------
    # Higgsfield AI (comparison / evaluation only — not wired into pipeline)
    # ------------------------------------------------------------------
    HF_API_KEY: Optional[str]            # Higgsfield Cloud API key ID
    HF_API_SECRET: Optional[str]         # Higgsfield Cloud API secret
    HIGGSFIELD_BASE_URL: str             # default: https://platform.higgsfield.ai
    HIGGSFIELD_TIMEOUT_SECONDS: int      # default: 180
    HIGGSFIELD_POLL_INTERVAL_SECONDS: int  # default: 5
    HIGGSFIELD_DRY_RUN: bool             # default: false — if true, no real API calls

    # ------------------------------------------------------------------
    # Smoke Test Mode — minimal end-to-end run with capped scene/beat counts
    # ------------------------------------------------------------------
    SMOKE_TEST_MODE: bool                # master switch; default: false
    SMOKE_TEST_MAX_NARRATION_BEATS: int  # 0 = unlimited; default: 0

    HYPOGEUM_ARCHIVE_ROOT: str           # fallback archive root; default: "" (built-in)
    COVER_GUARD_SSH_TARGET: str          # "user@host" of the always-on cover release guard; "" disables
    COVER_GUARD_SSH_KEY: str             # private key path for that SSH target; "" = ssh default
    POSTIZ_API_URL: str                  # Postiz public API base (…/api/public/v1); used by hypogeum.publishing.postiz

    # ------------------------------------------------------------------
    # Brand Guardian Agent (BGA v1A) — pre-generation brand compliance gate
    # ------------------------------------------------------------------
    BGA_ENABLED: bool  # run BGA Pass 1 + Pass 2 before MUAPI generation; default: false

    # ------------------------------------------------------------------
    # Expensive-run safety gate (2026-07-13) — see hypogeum/production_gate.py
    # ------------------------------------------------------------------
    PRODUCTION_MODE: str          # "validation" | "production"; default: validation
    FULL_PRODUCTION_ENABLED: bool  # must be explicitly true to run a full paid episode; default: false

    # ------------------------------------------------------------------
    # Per-agent text reasoning model assignments (configurable)
    # ------------------------------------------------------------------
    RD_MODEL: str                # Research Director synthesis model; default: anthropic/claude-opus-4.6
    TD_PLANNING_MODEL: str       # thumbnail director planning brain; default: openai/gpt-5.5-pro
    TD_PLANNING_MAX_TOKENS: int  # thumbnail planning max output tokens; default: 16384
    BGA_PASS1_MODEL: str         # BGA Pass 1 (VPD visual scenes); default: anthropic/claude-sonnet-4.6
    BGA_PASS2_MODEL: str         # BGA Pass 2 (thumbnail plans, visual identity); default: openai/gpt-5.5-pro
    EP_MODEL: str                # Executive Producer; default: anthropic/claude-sonnet-4.6

    # ------------------------------------------------------------------
    # Finance Agent (R50) — budget gate + cost ledger
    # ------------------------------------------------------------------
    FINANCE_BUDGET_CEILING_USD: float    # per-episode budget ceiling; 0.0 = no ceiling (no gate)
    FINANCE_WARNING_THRESHOLD_PCT: float # warn when estimate exceeds this % of ceiling; default: 0.80
    FINANCE_LEDGER_PATH: Optional[str]   # path to append-only episode cost ledger (JSONL); None = disabled

    # ------------------------------------------------------------------
    # ElevenLabs — narration + music (required at M3+)
    # ------------------------------------------------------------------
    ELEVENLABS_API_KEY: Optional[str]
    ELEVENLABS_VOICE_ID: Optional[str]
    ELEVENLABS_MODEL_ID: str               # default: eleven_multilingual_v2
    ELEVENLABS_OUTPUT_FORMAT: str          # default: mp3_44100_128
    ELEVENLABS_STABILITY: Optional[float]  # default: 0.5 applied in elevenlabs.py
    ELEVENLABS_SIMILARITY_BOOST: Optional[float]  # default: 0.75 applied in elevenlabs.py
    ELEVENLABS_TIMEOUT_SECONDS: int        # default: 240 (SDK default)
    ELEVENLABS_MUSIC_MODEL_ID: str         # default: music_v1
    ELEVENLABS_MUSIC_DEFAULT_DURATION: int # default: 120 (seconds; multiply by 1000 for API ms)

    # ------------------------------------------------------------------
    # VODER — local, free music generation (2026-09-20). Evaluated as an
    # ElevenLabs Music alternative; see hypogeum/voder_music.py. Voice
    # modes (TTS/cloning) are NOT wired in — VODER's length control on
    # those was found unreliable during evaluation.
    # ------------------------------------------------------------------
    MUSIC_PROVIDER: str                    # "elevenlabs" (default) or "voder"
    VODER_HOME: Optional[str]              # path to the VODER checkout; None disables voder
    VODER_TTM_TIMEOUT_SECONDS: int         # default: 900 (music gen; first run also downloads models)

    # ------------------------------------------------------------------
    # BytePlus ModelArk — Seedance 2.0 official direct provider (Phase 1H)
    # ------------------------------------------------------------------
    BYTEPLUS_ARK_API_KEY: Optional[str]
    BYTEPLUS_ARK_BASE_URL: str   # default: international endpoint
    BYTEPLUS_ARK_MODEL: str      # default: seedance-1-lite

    # ------------------------------------------------------------------
    # Phase 1J — Official direct providers
    # ------------------------------------------------------------------
    RUNWAY_API_KEY:     Optional[str]   # Runway ML official API
    KLING_API_KEY:      Optional[str]   # Kling AI — format: "key_id:key_secret"
    GOOGLE_API_KEY:     Optional[str]   # Google Veo / Gemini API

    # ------------------------------------------------------------------
    # OpenAI — direct image generation (THUMBNAIL_IMAGE_PROVIDER=openai)
    #          + Whisper fallback for caption alignment
    # ------------------------------------------------------------------
    OPENAI_API_KEY: Optional[str]
    OPENAI_IMAGE_BASE_URL: str         # default: https://api.openai.com/v1
    OPENAI_IMAGE_MODEL: str            # default: gpt-image-1 (official OpenAI model ID; gpt-image-2 is MUAPI-internal only)
    WHISPER_MODEL: str                 # default: whisper-1

    # ------------------------------------------------------------------
    # YouTube (required at M5)
    # ------------------------------------------------------------------
    YOUTUBE_CLIENT_ID: Optional[str]
    YOUTUBE_CLIENT_SECRET: Optional[str]
    YOUTUBE_REFRESH_TOKEN: Optional[str]
    YOUTUBE_CHANNEL_ID: Optional[str]
    YOUTUBE_DEFAULT_CATEGORY_ID: int   # default: 27 (Education)

    # ------------------------------------------------------------------
    # TikTok (conditional — graceful skip on unavailability)
    # ------------------------------------------------------------------
    TIKTOK_CLIENT_KEY: Optional[str]
    TIKTOK_CLIENT_SECRET: Optional[str]
    TIKTOK_ACCESS_TOKEN: Optional[str]
    TIKTOK_OPEN_ID: Optional[str]

    # ------------------------------------------------------------------
    # Meta Graph API (required at M5)
    # ------------------------------------------------------------------
    META_APP_ID: Optional[str]
    META_APP_SECRET: Optional[str]
    META_ACCESS_TOKEN: Optional[str]
    META_INSTAGRAM_ACCOUNT_ID: Optional[str]
    META_FACEBOOK_PAGE_ID: Optional[str]
    META_GRAPH_API_VERSION: str        # default: v19.0
    META_STAGING_BASE_URL: Optional[str]

    # ------------------------------------------------------------------
    # Notifications (P2-02 — all optional; delivery skipped when absent)
    # ------------------------------------------------------------------
    TELEGRAM_BOT_TOKEN: Optional[str]
    TELEGRAM_CHAT_ID: Optional[str]
    TELEGRAM_RAW_SIGNALS: bool         # default: False — operator_alerts owns Telegram
    NOTIFY_EMAIL_FROM: Optional[str]
    NOTIFY_EMAIL_TO: Optional[str]
    NOTIFY_SMTP_HOST: Optional[str]
    NOTIFY_SMTP_PORT: int              # default: 587
    NOTIFY_SMTP_PASSWORD: Optional[str]
    NOTIFY_SEVERITY_THRESHOLD: str     # default: "WARNING" (send WARNING+CRITICAL)

    def __init__(self) -> None:
        _load_dotenv(_PROJECT_ROOT / ".env")

        # --- OpenRouter (always required) ---
        self.OPENROUTER_API_KEY = _get_required(
            "OPENROUTER_API_KEY",
            "Routes all LLM calls via OpenRouter (required by all 9 agents)"
        )
        # Drain key (2026-10-05): an old key whose leftover balance is spent
        # first; OPENROUTER_MAIN_KEY takes over once it's marked exhausted.
        # See hypogeum/openrouter_drain.py.
        self.OPENROUTER_MAIN_KEY = self.OPENROUTER_API_KEY
        self.OPENROUTER_DRAIN_ACTIVE = False
        _drain = _get_optional("OPENROUTER_DRAIN_KEY", default="") or ""
        if _drain and _drain != self.OPENROUTER_API_KEY:
            from hypogeum.openrouter_drain import is_exhausted as _drain_exhausted
            if not _drain_exhausted():
                self.OPENROUTER_API_KEY = _drain
                self.OPENROUTER_DRAIN_ACTIVE = True
        self.OPENROUTER_SONNET_MODEL_ID = _get_optional(
            "OPENROUTER_SONNET_MODEL_ID", default=_DEFAULT_SONNET_SLUG
        )
        self.OPENROUTER_OPUS_MODEL_ID = _get_optional(
            "OPENROUTER_OPUS_MODEL_ID", default=_DEFAULT_OPUS_SLUG
        )
        self.OPENROUTER_APP_NAME = _get_optional(
            "OPENROUTER_APP_NAME", default="hypogeum-brain"
        )
        self.OPENROUTER_APP_URL = _get_optional("OPENROUTER_APP_URL")
        # Must never silently resolve to the same slug as an agent's own
        # primary model (a fresh clone with no .env override, or an .env.example
        # left blank, must not regress into the 2026-08-04 primary==fallback
        # bug) — defaults to a genuinely distinct, already-proven OpenRouter
        # model rather than leaving this unset.
        self.OPENROUTER_FALLBACK_MODEL = _get_optional(
            "OPENROUTER_FALLBACK_MODEL", default=_DEFAULT_SONNET_SLUG
        )
        self.OPENROUTER_IMAGE_MODEL = _get_optional(
            "OPENROUTER_IMAGE_MODEL", default="openai/gpt-5-image-mini"
        )
        self.ANTHROPIC_API_KEY = _get_optional("ANTHROPIC_API_KEY")

        # --- Perplexity ---
        self.PERPLEXITY_API_KEY = _get_optional("PERPLEXITY_API_KEY")
        self.PERPLEXITY_MODEL = _get_optional("PERPLEXITY_MODEL", default="sonar-pro")
        self.PERPLEXITY_MAX_TOKENS = _get_int("PERPLEXITY_MAX_TOKENS", default=0) or None

        # --- MUAPI ---
        self.MUAPI_API_KEY = _get_optional("MUAPI_API_KEY")
        self.MUAPI_BASE_URL = _get_optional(
            "MUAPI_BASE_URL", default="https://api.muapi.ai/api/v1"
        )
        self.MUAPI_T1_MODEL = _get_optional("MUAPI_T1_MODEL", default="veo3-text-to-video")
        self.MUAPI_T2_MODEL = _get_optional("MUAPI_T2_MODEL", default="veo3.1-lite-text-to-video")
        self.MUAPI_T3_MODEL = _get_optional("MUAPI_T3_MODEL", default="veo3.1-lite-text-to-video")
        self.MUAPI_VIDEO_RESOLUTION = _get_optional("MUAPI_VIDEO_RESOLUTION", default="1080p")
        self.MUAPI_I2V_MODEL = _get_optional("MUAPI_I2V_MODEL", default="veo3-image-to-video")
        # Default changed 2026-09-05 (flux-2-dev -> gpt-image-2-text-to-image):
        # flux-2-dev's real MUAPI catalogue input_fields are
        # ["prompt", "width", "height"] -- it has NO "aspect_ratio" field at
        # all, so the aspect_ratio value hypogeum.muapi.generate_reference_
        # image() has always sent it was silently ignored and every
        # reference image came back the model's own square default (1024x1024)
        # regardless of the channel's real orientation -- confirmed live
        # 2026-09-05 generating a real portrait (9:16) reference image and
        # getting 1024x1024 back. gpt-image-2-text-to-image's real
        # input_fields ARE ["prompt", "aspect_ratio", "resolution", "quality"]
        # -- the exact field this codebase already sends -- and it is
        # already the trusted, currently-in-production THUMBNAIL_IMAGE_MODEL,
        # so this reuses an already-proven model/payload combination rather
        # than introducing a new one.
        self.MUAPI_IMAGE_MODEL = _get_optional("MUAPI_IMAGE_MODEL", default="gpt-image-2-text-to-image")  # legacy
        # Canonical routing decision (docs/validation/thumbnail_image_generation_validation/
        # GPT_IMAGE_2_ROUTING_AUDIT.md): MUAPI gpt-image-2-text-to-image is the primary
        # thumbnail provider/model. openrouter and openai are genuine-failure fallbacks only.
        self.THUMBNAIL_IMAGE_PROVIDER = _get_optional("THUMBNAIL_IMAGE_PROVIDER", default="muapi")
        self.THUMBNAIL_SHORT_CANDIDATE_CAP = _get_int("THUMBNAIL_SHORT_CANDIDATE_CAP", default=3)
        self.THUMBNAIL_IMAGE_MODEL = _get_optional(
            "THUMBNAIL_IMAGE_MODEL", default="gpt-image-2-text-to-image"
        )
        self.THUMBNAIL_IMAGE_RESOLUTION = _get_optional("THUMBNAIL_IMAGE_RESOLUTION", default="2K")
        self.THUMBNAIL_IMAGE_QUALITY = _get_optional("THUMBNAIL_IMAGE_QUALITY", default="high")
        self.THUMBNAIL_FALLBACK_PROVIDER = _get_optional("THUMBNAIL_FALLBACK_PROVIDER", default="openrouter")
        self.THUMBNAIL_FALLBACK2_PROVIDER = _get_optional("THUMBNAIL_FALLBACK2_PROVIDER", default="openai")
        self.THUMBNAIL_FALLBACK_MODEL = _get_optional("THUMBNAIL_FALLBACK_MODEL", default="flux-2-dev")
        self.MUAPI_DEFAULT_TIER = _get_int("MUAPI_DEFAULT_TIER", default=2)
        self.QUEUE_MAX_AUTO_RETRIES = _get_int("QUEUE_MAX_AUTO_RETRIES", default=2)
        # Hard per-episode attempt ceiling (queue.process_next backstop): an
        # episode with this many terminally-failed runs is abandoned (topic
        # skipped, episode superseded) regardless of failure_policy or which
        # queue path re-picked it — a deterministic failure must not loop.
        self.MAX_EPISODE_TOTAL_FAILURES = _get_int("MAX_EPISODE_TOTAL_FAILURES", default=3)
        self.QUEUE_RETRY_BACKOFF_MINUTES = _get_optional(
            "QUEUE_RETRY_BACKOFF_MINUTES", default="5,20",
        )
        self.MUAPI_TIMEOUT_SECONDS = _get_int("MUAPI_TIMEOUT_SECONDS", default=180)
        self.MUAPI_POLL_INTERVAL_SECONDS = _get_int("MUAPI_POLL_INTERVAL_SECONDS", default=5)
        self.MUAPI_MAX_POLL_ATTEMPTS = _get_int("MUAPI_MAX_POLL_ATTEMPTS", default=24)
        self.MUAPI_QUOTA_RETRY_MAX_ATTEMPTS = _get_int("MUAPI_QUOTA_RETRY_MAX_ATTEMPTS", default=3)
        self.MUAPI_QUOTA_RETRY_DELAY_SECONDS = _get_float("MUAPI_QUOTA_RETRY_DELAY_SECONDS", default=60.0)
        self.CINEMATIC_DIRECTOR_MAX_RETRIES = _get_int("CINEMATIC_DIRECTOR_MAX_RETRIES", default=2)
        self.MUAPI_WEBHOOK_URL = _get_optional("MUAPI_WEBHOOK_URL")
        self.MUAPI_CREDIT_WARNING_THRESHOLD = _get_float("MUAPI_CREDIT_WARNING_THRESHOLD")
        # Runaway-spend circuit breaker (hypogeum.muapi_spend_breaker): the
        # worker auto-pauses NEW production (queue_paused=1, publishing
        # continues) when EITHER rolling-hour MUAPI spend crosses the limit
        # OR that many episode runs fail consecutively — so a failure cascade
        # can't drain the vendor account. 0/None disables that check.
        self.MUAPI_HOURLY_SPEND_LIMIT_USD = _get_float(
            "MUAPI_HOURLY_SPEND_LIMIT_USD", default=45.0,
        )
        self.MUAPI_CONSECUTIVE_FAILURE_LIMIT = _get_int(
            "MUAPI_CONSECUTIVE_FAILURE_LIMIT", default=3,
        )

        # --- Source Enhancement Stage (provider-agnostic) ---
        self.ENHANCEMENT_ENABLED = _get_bool("ENHANCEMENT_ENABLED", default=True)
        self.ENHANCEMENT_PROVIDER = _get_optional(
            "ENHANCEMENT_PROVIDER", default="muapi_ai_video_upscaler"
        )
        self.ENHANCEMENT_REQUIRED_WIDTH = _get_int("ENHANCEMENT_REQUIRED_WIDTH", default=1920)
        self.ENHANCEMENT_REQUIRED_HEIGHT = _get_int("ENHANCEMENT_REQUIRED_HEIGHT", default=1080)
        self.ENHANCEMENT_TARGET_RESOLUTION_LABEL = _get_optional(
            "ENHANCEMENT_TARGET_RESOLUTION_LABEL", default="1080p"
        )

        # Automatic repair of a finished episode the production output gate
        # rejects for under-resolution clips (hypogeum.episode_repair): spends
        # under the channel's signed operating grant, capped per repair.
        self.EPISODE_REPAIR_ENABLED = _get_bool("EPISODE_REPAIR_ENABLED", default=True)
        self.EPISODE_REPAIR_MAX_USD = _get_float("EPISODE_REPAIR_MAX_USD", default=1.0) or 0.0

        # M5.6 — Cost guardrails
        self.MUAPI_EPISODE_BUDGET_CREDITS = _get_float("MUAPI_EPISODE_BUDGET_CREDITS", default=0.0) or 0.0
        self.MUAPI_SCENE_BUDGET_CREDITS = _get_float("MUAPI_SCENE_BUDGET_CREDITS", default=0.0) or 0.0
        self.MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE = _get_int("MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE", default=0)
        self.EPISODE_ZERO_MAX_SCENES = _get_int("EPISODE_ZERO_MAX_SCENES", default=0)
        self.MUAPI_CHEAP_MODE = _get_bool("MUAPI_CHEAP_MODE", default=False)
        self.MUAPI_RUNWAY_ONLY_MODE = _get_bool("MUAPI_RUNWAY_ONLY_MODE", default=False)
        self.MUAPI_DRY_RUN = _get_bool("MUAPI_DRY_RUN", default=False)
        self.YOUTUBE_DRY_RUN = _get_bool("YOUTUBE_DRY_RUN", default=False)
        # Direct YouTube Data API publishing (the legacy pre-Zernio path).
        # OFF by default: every platform, YouTube included, now publishes
        # through Zernio (AUTO_ZERNIO_PUBLISHING_ENABLED). When False the
        # startup YouTube-OAuth check is skipped entirely — a stale/expired
        # Google refresh token is not a problem the operator needs to see.
        self.YOUTUBE_DIRECT_PUBLISH_ENABLED = _get_bool(
            "YOUTUBE_DIRECT_PUBLISH_ENABLED", default=False
        )
        self.MUAPI_ALLOW_PAID_FALLBACKS = _get_bool("MUAPI_ALLOW_PAID_FALLBACKS", default=False)
        self.ENABLE_DEDICATED_SHORT_GENERATION = _get_bool(
            "ENABLE_DEDICATED_SHORT_GENERATION", default=True
        )

        # --- Short-Native production mode ---
        # Default scene cap is one per narration beat (a ~50s short is 6-7
        # beats): each beat is covered by its own single 8s video clip, so
        # Assembly never has to reach the coverage-fill path for a
        # short-native episode.
        self.SHORT_NATIVE_MAX_SCENES = _get_int("SHORT_NATIVE_MAX_SCENES", default=8)
        self.SHORT_NATIVE_MAX_NARRATION_WORDS = _get_int(
            "SHORT_NATIVE_MAX_NARRATION_WORDS", default=150
        )
        self.SHORT_NATIVE_MAX_BEAT_SECONDS = _get_float(
            "SHORT_NATIVE_MAX_BEAT_SECONDS", default=8.5
        ) or 8.5
        self.SHORT_NATIVE_WORDS_PER_SECOND = _get_float(
            "SHORT_NATIVE_WORDS_PER_SECOND", default=2.9
        ) or 2.9
        self.SHORT_NATIVE_ASPECT_RATIO = _get_optional(
            "SHORT_NATIVE_ASPECT_RATIO", default="9:16"
        ) or "9:16"
        self.SHORT_NATIVE_MODE = _get_bool("SHORT_NATIVE_MODE", default=False)
        if self.SHORT_NATIVE_MODE:
            self.apply_short_native_preset()

        # --- Assembly / Render ---
        # FFMPEG_BINARY: path/name of the ffmpeg executable Assembly/Video Renderer
        # invoke. Defaults to "ffmpeg" (resolved via PATH). Must be a build with
        # libass (the "subtitles"/"ass" filters) for burned-in Short captions to
        # work at all -- see hypogeum.agents.video_renderer.assert_subtitle_filter_available().
        # Never hardcode "ffmpeg" in a subprocess call; always read this.
        self.FFMPEG_BINARY = _get_optional("FFMPEG_BINARY", default="ffmpeg")
        # HYPOGEUM_ARCHIVE_ROOT: absolute filesystem path where published
        # episode assets are relocated after publish + catalog. This is only
        # the fallback default -- the operator-editable value lives in the
        # factory_settings DB table (key 'archive_root') and is resolved by
        # hypogeum.archive_storage.resolve_archive_root(). Empty => the
        # built-in default (<project>/.hypogeum-assets/archive).
        self.HYPOGEUM_ARCHIVE_ROOT = _get_optional("HYPOGEUM_ARCHIVE_ROOT", default="")
        # COVER_GUARD_SSH_TARGET / COVER_GUARD_SSH_KEY: the server that releases
        # held YouTube Shorts at their slot even while this machine sleeps
        # (scripts/server/cover_release_guard.py). The dashboard pushes the
        # hold/confirmation state there over SSH and pulls back what it
        # released -- see hypogeum.publishing.cover_guard_sync. Empty => the
        # in-dashboard loop is the only releaser (previous behaviour).
        self.COVER_GUARD_SSH_TARGET = _get_optional("COVER_GUARD_SSH_TARGET", default="")
        self.COVER_GUARD_SSH_KEY = _get_optional("COVER_GUARD_SSH_KEY", default="")
        # POSTIZ_API_URL: the self-hosted Postiz public API that
        # hypogeum.publishing.postiz.PostizProvider talks to. The API key is
        # never read here -- it lives in Credential Manager (provider='postiz').
        self.POSTIZ_API_URL = _get_optional(
            "POSTIZ_API_URL", default="https://postiz.cortexchannels.com/api/public/v1"
        )
        # REQUIRE_SHORT_BURNED_CAPTIONS: fail closed (raise, never silently
        # render without them) when a Short's caption asset cannot be
        # resolved to its own canonical short_id. Default True -- burned-in
        # captions are a Hypogeum Channel Package requirement, not optional.
        # See docs/operations/audits/EP_F3619022_ASSET_CONTINUITY_AND_BURNED_CAPTIONS_AUDIT_2026-08-22.md.
        self.REQUIRE_SHORT_BURNED_CAPTIONS = _get_bool("REQUIRE_SHORT_BURNED_CAPTIONS", default=True)
        # END_PAGE_DURATION_SECONDS: how long the static end-page card is held (default 20s).
        self.END_PAGE_DURATION_SECONDS = _get_int("END_PAGE_DURATION_SECONDS", default=20)
        # CLIP_REPAIR_ENABLED: run the clip repair pipeline before QA rejection (default True).
        self.CLIP_REPAIR_ENABLED = _get_bool("CLIP_REPAIR_ENABLED", default=True)
        # CLIP_REPAIR_MAX_GENERATED: max extra clips to generate in Strategy B (default 5).
        self.CLIP_REPAIR_MAX_GENERATED = _get_int("CLIP_REPAIR_MAX_GENERATED", default=5)
        # CLIP_REPAIR_MAX_SLOT_RETRIES: bounded retries of the SAME missing scene in Strategy B
        # on a transient regeneration failure (default 2 = three attempts total per scene).
        # A retry does not count toward CLIP_REPAIR_MAX_GENERATED — it exists so a single
        # scene whose MUAPI generation blips (timeout / quota / an auth still settling) does
        # not stay missing and sink an otherwise-complete, otherwise-paid episode at the
        # coverage check (available_duration_seconds=0). Mirrors COVERAGE_FILL_MAX_SLOT_RETRIES.
        self.CLIP_REPAIR_MAX_SLOT_RETRIES = _get_int(
            "CLIP_REPAIR_MAX_SLOT_RETRIES", default=2
        )
        # COVERAGE_CHECK_ENABLED: gate assembly on per-beat visual-duration coverage before
        # any rendering (default True). See hypogeum/agents/coverage_check.py — approved
        # production policy (2026-08-01): Video Renderer must never compensate for missing
        # duration; insufficient beats block assembly and request additional MUAPI coverage.
        self.COVERAGE_CHECK_ENABLED = _get_bool("COVERAGE_CHECK_ENABLED", default=True)
        # COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT: safety cap (2026-08-05: no longer a fixed
        # target — clip_repair_agent.fill_coverage_deficits() now plans the number of extra
        # clips per beat from the beat's own measured deficit, recalculated after each real
        # clip; this is only the finite ceiling that stops one stubborn beat from generating
        # unbounded cost when its deficit genuinely can't close within a reasonable spend).
        self.COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT = _get_int(
            "COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT", default=2
        )
        # COVERAGE_FILL_MAX_SLOT_RETRIES: bounded retries of the SAME planned clip slot on a
        # transient generation failure (default 1 = one retry, two attempts total per slot).
        # A retry never advances to a new slot and never counts toward
        # COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT — it exists so one transient MUAPI failure
        # doesn't permanently cost a beat one of its mathematically required clips.
        self.COVERAGE_FILL_MAX_SLOT_RETRIES = _get_int(
            "COVERAGE_FILL_MAX_SLOT_RETRIES", default=1
        )
        # COVERAGE_FILL_ASSUMED_CLIP_DURATION_SECONDS: planning-only estimate (default 8.0,
        # the fixed provider clip length) used purely for the upfront "how many clips will
        # this beat probably need" cost-visibility estimate logged before submission. Never
        # the actual stopping condition — that is always the real, ffprobe-measured duration
        # of each clip as it's generated.
        self.COVERAGE_FILL_ASSUMED_CLIP_DURATION_SECONDS = _get_float(
            "COVERAGE_FILL_ASSUMED_CLIP_DURATION_SECONDS", default=8.0
        )
        # CINEMATIC_SECONDS_PER_SHOT (2026-08-06 directing-rule): editorial-pacing constant
        # for hypogeum.agents.cinematic_director.compute_shot_budget() -- how much screen
        # time one directorial shot idea is expected to comfortably cover before a cut is
        # warranted. Not a physical clip-length constant (generated clips are a fixed ~8s
        # regardless); this is what makes the shot budget scale with narration duration
        # instead of being a fixed "three shots per beat" rule. Channel Package may override.
        self.CINEMATIC_SECONDS_PER_SHOT = _get_float("CINEMATIC_SECONDS_PER_SHOT", default=10.0)
        # CINEMATIC_MAX_SHOTS_PER_BEAT: finite safety cap on compute_shot_budget()'s output
        # so one very long beat cannot request unbounded coverage (default 6).
        self.CINEMATIC_MAX_SHOTS_PER_BEAT = _get_int("CINEMATIC_MAX_SHOTS_PER_BEAT", default=6)
        # CINEMATIC_MIN_SHOTS_PER_BEAT (2026-08-08): explicit, configurable floor on
        # compute_shot_budget()'s output, replacing a previously hardcoded "at least 1"
        # literal. Independent of coverage_minimum/editorial_minimum — see
        # cinematic_director.compute_shot_budget()'s own docstring for how the three
        # terms combine.
        self.CINEMATIC_MIN_SHOTS_PER_BEAT = _get_int("CINEMATIC_MIN_SHOTS_PER_BEAT", default=1)
        # CINEMATIC_ESTIMATED_WORDS_PER_MINUTE: Audio Agent now runs before Cinematic
        # Director (Voice-before-Visual, 2026-08-08), so real, measured per-beat duration
        # is normally available. This WPM value only feeds the fallback word-count
        # estimate used for a beat_id genuinely missing from the measured narration
        # package (see cinematic_director.run_cinematic_director()). Default 145 WPM
        # calibrated from this channel's real production narration (ep_f3619022
        # seg_b_001: 58 words / 24.427s measured = ~142.5 WPM).
        self.CINEMATIC_ESTIMATED_WORDS_PER_MINUTE = _get_float(
            "CINEMATIC_ESTIMATED_WORDS_PER_MINUTE", default=145.0
        )

        # --- Higgsfield (comparison only) ---
        self.HF_API_KEY = _get_optional("HF_API_KEY")
        self.HF_API_SECRET = _get_optional("HF_API_SECRET")
        self.HIGGSFIELD_BASE_URL = _get_optional(
            "HIGGSFIELD_BASE_URL", default="https://platform.higgsfield.ai"
        )
        self.HIGGSFIELD_TIMEOUT_SECONDS = _get_int("HIGGSFIELD_TIMEOUT_SECONDS", default=180)
        self.HIGGSFIELD_POLL_INTERVAL_SECONDS = _get_int("HIGGSFIELD_POLL_INTERVAL_SECONDS", default=5)
        self.HIGGSFIELD_DRY_RUN = _get_bool("HIGGSFIELD_DRY_RUN", default=False)

        # --- Smoke Test Mode ---
        self.SMOKE_TEST_MODE = _get_bool("SMOKE_TEST_MODE", default=False)
        self.SMOKE_TEST_MAX_NARRATION_BEATS = _get_int("SMOKE_TEST_MAX_NARRATION_BEATS", default=0)
        if self.SMOKE_TEST_MODE:
            if self.EPISODE_ZERO_MAX_SCENES == 0:
                self.EPISODE_ZERO_MAX_SCENES = 2
            # Force T2 for all scenes in smoke test — T2=veo3.1-lite-text-to-video since 2026-07-06
            if not self.MUAPI_RUNWAY_ONLY_MODE and not self.MUAPI_CHEAP_MODE:
                self.MUAPI_RUNWAY_ONLY_MODE = True

        # --- Brand Guardian Agent ---
        self.BGA_ENABLED = _get_bool("BGA_ENABLED", default=False)

        # --- Expensive-run safety gate ---
        self.PRODUCTION_MODE = _get_optional("PRODUCTION_MODE", default="validation") or "validation"
        self.FULL_PRODUCTION_ENABLED = _get_bool("FULL_PRODUCTION_ENABLED", default=False)

        # --- Thumbnail Identity Guardian ---
        self.TIG_ENABLED = _get_bool("TIG_ENABLED", default=True)
        self.TIG_MODEL = _get_optional(
            "TIG_MODEL", default="anthropic/claude-sonnet-4.6"
        )
        # TIG_FAIL_OPEN=false (default) → BLOCKED/error verdict makes production_ready=False.
        # TIG_FAIL_OPEN=true  → BLOCKED/error is logged but does not block production_ready.
        # Default is fail-closed for ep_000.
        self.TIG_FAIL_OPEN = _get_bool("TIG_FAIL_OPEN", default=False)

        # --- Per-agent text reasoning model assignments ---
        self.RD_MODEL = _get_optional(
            "RD_MODEL", default="anthropic/claude-opus-4.6"
        )
        self.TD_PLANNING_MODEL = _get_optional(
            "TD_PLANNING_MODEL", default="openai/gpt-5.5-pro"
        )
        self.TD_PLANNING_MAX_TOKENS = _get_int("TD_PLANNING_MAX_TOKENS", default=16384)
        self.BGA_PASS1_MODEL = _get_optional(
            "BGA_PASS1_MODEL", default="anthropic/claude-sonnet-4.6"
        )
        self.BGA_PASS2_MODEL = _get_optional(
            "BGA_PASS2_MODEL", default="openai/gpt-5.5-pro"
        )
        self.EP_MODEL = _get_optional(
            "EP_MODEL", default="anthropic/claude-sonnet-4.6"
        )

        # --- Finance Agent ---
        self.FINANCE_BUDGET_CEILING_USD = _get_float("FINANCE_BUDGET_CEILING_USD", default=0.0) or 0.0
        self.FINANCE_WARNING_THRESHOLD_PCT = _get_float("FINANCE_WARNING_THRESHOLD_PCT", default=0.80) or 0.80
        self.FINANCE_LEDGER_PATH = _get_optional("FINANCE_LEDGER_PATH")

        # --- Authorization Authority (hypogeum/authorization_authority.py) ---
        # Base64-encoded Ed25519 PUBLIC key only -- the matching PRIVATE
        # key never runs inside this (or any) production process; it is
        # held only by the offline scripts/approve_*.py operator tooling.
        # A public key is safe to store in ordinary deployment config: it
        # can verify a signature but cannot forge one.
        self.AUTHORIZATION_APPROVAL_PUBLIC_KEY = _get_optional("AUTHORIZATION_APPROVAL_PUBLIC_KEY")
        # Path to the Ed25519 PRIVATE key PEM used to sign a Channel Operating
        # Grant. Only ever read by a short-lived signing subprocess
        # (tools/configure_channel_operating_grant.py) that the dashboard
        # spawns and that then exits -- no long-running process (dashboard,
        # worker, pipeline) imports the signer or holds the key. When the
        # file is absent/unreadable, dashboard signing is unavailable and the
        # operator is shown the CLI command instead (air-gapped deployments).
        self.AUTHORIZATION_APPROVAL_PRIVATE_KEY_PATH = _get_optional(
            "AUTHORIZATION_APPROVAL_PRIVATE_KEY_PATH",
            default="approval_ed25519_private_key.pem")

        # --- ElevenLabs ---
        self.ELEVENLABS_API_KEY = _get_optional("ELEVENLABS_API_KEY")
        self.ELEVENLABS_VOICE_ID = _get_optional("ELEVENLABS_VOICE_ID")
        self.ELEVENLABS_MODEL_ID = _get_optional(
            "ELEVENLABS_MODEL_ID", default="eleven_multilingual_v2"
        )
        self.ELEVENLABS_OUTPUT_FORMAT = _get_optional(
            "ELEVENLABS_OUTPUT_FORMAT", default="mp3_44100_128"
        )
        self.ELEVENLABS_STABILITY = _get_float("ELEVENLABS_STABILITY")
        self.ELEVENLABS_SIMILARITY_BOOST = _get_float("ELEVENLABS_SIMILARITY_BOOST")
        self.ELEVENLABS_TIMEOUT_SECONDS = _get_int("ELEVENLABS_TIMEOUT_SECONDS", default=240)
        self.ELEVENLABS_MUSIC_MODEL_ID = _get_optional(
            "ELEVENLABS_MUSIC_MODEL_ID", default="music_v1"
        )
        self.ELEVENLABS_MUSIC_DEFAULT_DURATION = _get_int(
            "ELEVENLABS_MUSIC_DEFAULT_DURATION", default=120
        )

        # --- VODER (local music generation) ---
        self.MUSIC_PROVIDER = (_get_optional("MUSIC_PROVIDER", default="elevenlabs") or "elevenlabs").lower()
        self.VODER_HOME = _get_optional("VODER_HOME")
        self.VODER_TTM_TIMEOUT_SECONDS = _get_int("VODER_TTM_TIMEOUT_SECONDS", default=900)

        # Background music is a required deliverable: when True (default),
        # a render with any section left without music is an assembly QA
        # FAILURE, not a warning. Set MUSIC_REQUIRED=false only for a
        # channel/run that deliberately ships silent.
        self.MUSIC_REQUIRED = _get_bool("MUSIC_REQUIRED", default=True)

        # --- Phase 1J official providers ---
        self.RUNWAY_API_KEY  = _get_optional("RUNWAY_API_KEY")
        self.KLING_API_KEY   = _get_optional("KLING_API_KEY")
        self.GOOGLE_API_KEY  = _get_optional("GOOGLE_API_KEY")

        # --- BytePlus ModelArk (Phase 1H) ---
        self.BYTEPLUS_ARK_API_KEY = _get_optional("BYTEPLUS_ARK_API_KEY")
        self.BYTEPLUS_ARK_BASE_URL = _get_optional(
            "BYTEPLUS_ARK_BASE_URL",
            default="https://ark.ap-southeast.bytepluses.com/api/v3",
        )
        self.BYTEPLUS_ARK_MODEL = _get_optional("BYTEPLUS_ARK_MODEL", default="seedance-1-lite")

        # --- OpenAI (direct image + Whisper) ---
        self.OPENAI_API_KEY = _get_optional("OPENAI_API_KEY")
        self.OPENAI_IMAGE_BASE_URL = _get_optional(
            "OPENAI_IMAGE_BASE_URL", default="https://api.openai.com/v1"
        )
        self.OPENAI_IMAGE_MODEL = _get_optional("OPENAI_IMAGE_MODEL", default="gpt-image-1")
        self.WHISPER_MODEL = _get_optional("WHISPER_MODEL", default="whisper-1")

        # --- YouTube ---
        self.YOUTUBE_CLIENT_ID = _get_optional("YOUTUBE_CLIENT_ID")
        self.YOUTUBE_CLIENT_SECRET = _get_optional("YOUTUBE_CLIENT_SECRET")
        self.YOUTUBE_REFRESH_TOKEN = _get_optional("YOUTUBE_REFRESH_TOKEN")
        self.YOUTUBE_CHANNEL_ID = _get_optional("YOUTUBE_CHANNEL_ID")
        self.YOUTUBE_DEFAULT_CATEGORY_ID = _get_int(
            "YOUTUBE_DEFAULT_CATEGORY_ID", default=27
        )

        # --- TikTok ---
        self.TIKTOK_CLIENT_KEY = _get_optional("TIKTOK_CLIENT_KEY")
        self.TIKTOK_CLIENT_SECRET = _get_optional("TIKTOK_CLIENT_SECRET")
        self.TIKTOK_ACCESS_TOKEN = _get_optional("TIKTOK_ACCESS_TOKEN")
        self.TIKTOK_OPEN_ID = _get_optional("TIKTOK_OPEN_ID")

        # --- Meta ---
        self.META_APP_ID = _get_optional("META_APP_ID")
        self.META_APP_SECRET = _get_optional("META_APP_SECRET")
        self.META_ACCESS_TOKEN = _get_optional("META_ACCESS_TOKEN")
        self.META_INSTAGRAM_ACCOUNT_ID = _get_optional("META_INSTAGRAM_ACCOUNT_ID")
        self.META_FACEBOOK_PAGE_ID = _get_optional("META_FACEBOOK_PAGE_ID")
        self.META_GRAPH_API_VERSION = _get_optional(
            "META_GRAPH_API_VERSION", default="v19.0"
        )
        self.META_STAGING_BASE_URL = _get_optional("META_STAGING_BASE_URL")

        # --- Notifications ---
        self.TELEGRAM_BOT_TOKEN = _get_optional("TELEGRAM_BOT_TOKEN")
        self.TELEGRAM_CHAT_ID = _get_optional("TELEGRAM_CHAT_ID")
        # Raw OIS signals (disk_low / repeated_failure re-emit every ~15 min)
        # are not routed to Telegram unless asked; hypogeum.operator_alerts
        # sends one message per real blocker instead.
        self.TELEGRAM_RAW_SIGNALS = _get_bool("TELEGRAM_RAW_SIGNALS", default=False)
        self.NOTIFY_EMAIL_FROM = _get_optional("NOTIFY_EMAIL_FROM")
        self.NOTIFY_EMAIL_TO = _get_optional("NOTIFY_EMAIL_TO")
        self.NOTIFY_SMTP_HOST = _get_optional("NOTIFY_SMTP_HOST")
        self.NOTIFY_SMTP_PORT = _get_int("NOTIFY_SMTP_PORT", default=587)
        self.NOTIFY_SMTP_PASSWORD = _get_optional("NOTIFY_SMTP_PASSWORD")
        self.NOTIFY_SEVERITY_THRESHOLD = _get_optional(
            "NOTIFY_SEVERITY_THRESHOLD", default="WARNING"
        )

        # --- Autonomous Discovery (M5.1) ---
        # Master switch — defaults off so existing deployments are unaffected.
        self.AUTO_DISCOVERY_ENABLED = _get_bool("AUTO_DISCOVERY_ENABLED", default=False)
        # Run discovery when queued topic depth per channel falls below this value.
        self.AUTO_DISCOVERY_THRESHOLD = _get_int("AUTO_DISCOVERY_THRESHOLD", default=2)
        # If True, the top-scored candidate is auto-approved and enqueued without
        # operator review. Defaults False — requires explicit opt-in for full autonomy.
        self.AUTO_DISCOVERY_AUTO_APPROVE = _get_bool("AUTO_DISCOVERY_AUTO_APPROVE", default=False)

        # --- Autonomous Publishing (Zernio release-flow trigger) ---
        # Master switch — defaults off so existing deployments are unaffected.
        # Channel-scoped fail-closed behavior (no bindings/schedule policy/
        # publishing_enabled=False) applies underneath regardless of this
        # flag; this only controls whether run_worker.py ever calls the
        # trigger at all. See run_worker.py::_process_channel_publishing().
        self.AUTO_ZERNIO_PUBLISHING_ENABLED = _get_bool("AUTO_ZERNIO_PUBLISHING_ENABLED", default=False)

        # --- Discovery Director (v2) ---
        # Controls which engine run_discovery_cycle() dispatches to.
        # 'v1' = legacy heuristic (default — no behaviour change for existing deployments).
        # 'v2' = Discovery Director (LLM-driven ideation + initial_scorer).
        self.DISCOVERY_ENGINE = _get_optional("DISCOVERY_ENGINE", default="v1")
        # Absolute score floor: candidates with initial_composite >= this are verify-eligible.
        self.DISCOVERY_VERIFY_THRESHOLD = _get_float("DISCOVERY_VERIFY_THRESHOLD", default=0.65)
        # Delta from cycle best: candidates within this range of best_score also qualify.
        self.DISCOVERY_VERIFY_DELTA = _get_float("DISCOVERY_VERIFY_DELTA", default=0.10)

        # --- Discovery Scoring Weights (v2 initial_scorer.py) ---
        # All scorer constants live here so tuning never requires a source-code edit.
        # initial_scorer.py reads these via os.getenv() at call time.
        # Defaults match the structural scorer in initial_scorer.py exactly.
        self.DISCOVERY_BASE_SCORE = _get_float("DISCOVERY_BASE_SCORE", default=0.50) or 0.50
        self.DISCOVERY_HOOK_LONG_WEIGHT = _get_float("DISCOVERY_HOOK_LONG_WEIGHT", default=0.10) or 0.10
        self.DISCOVERY_HOOK_SHORT_WEIGHT = _get_float("DISCOVERY_HOOK_SHORT_WEIGHT", default=0.05) or 0.05
        self.DISCOVERY_HOOK_PRESENT_WEIGHT = _get_float("DISCOVERY_HOOK_PRESENT_WEIGHT", default=0.02) or 0.02
        self.DISCOVERY_DIFF_LONG_WEIGHT = _get_float("DISCOVERY_DIFF_LONG_WEIGHT", default=0.10) or 0.10
        self.DISCOVERY_DIFF_SHORT_WEIGHT = _get_float("DISCOVERY_DIFF_SHORT_WEIGHT", default=0.05) or 0.05
        self.DISCOVERY_NOVELTY_PENALTY = _get_float("DISCOVERY_NOVELTY_PENALTY", default=0.20) or 0.20
        self.DISCOVERY_SIGNAL_BOOST_TOP_PERFORMER = _get_float("DISCOVERY_SIGNAL_BOOST_TOP_PERFORMER", default=0.12) or 0.12
        self.DISCOVERY_SIGNAL_BOOST_HIGH_RETENTION = _get_float("DISCOVERY_SIGNAL_BOOST_HIGH_RETENTION", default=0.10) or 0.10
        self.DISCOVERY_SIGNAL_PENALTY_KEYWORD = _get_float("DISCOVERY_SIGNAL_PENALTY_KEYWORD", default=0.25) or 0.25
        self.DISCOVERY_ANALYTICS_DELTA_CAP = _get_float("DISCOVERY_ANALYTICS_DELTA_CAP", default=0.20) or 0.20

        # --- Log Rotation (Phase 3 Production Hardening, P0.1) ---
        # pipeline.log rotates once it reaches this size. Default 50MB.
        self.LOG_MAX_BYTES = _get_int("LOG_MAX_BYTES", default=50 * 1024 * 1024)
        # Number of gzip-compressed archives kept before the oldest is deleted.
        self.LOG_BACKUP_COUNT = _get_int("LOG_BACKUP_COUNT", default=10)

        # --- Recovery System (Phase 3 Production Hardening, P0.2) ---
        # Number of scheduled DB backups kept before the oldest is deleted.
        self.BACKUP_RETENTION_COUNT = _get_int("BACKUP_RETENTION_COUNT", default=14)
        # Directory backups (and .env snapshots) are written to.
        self.BACKUP_DIR = _get_optional("BACKUP_DIR")

        # --- Runtime Observability (Phase 3 Production Hardening, P0.3) ---
        # worker_control.worker_status is touched every iteration of
        # run_worker.py's main loop regardless of whether work was found —
        # this threshold is how stale that timestamp can get before the
        # worker (and, since they're the same process, the scheduler) is
        # considered offline. Default 300s = 5x the worker's default 60s
        # poll interval.
        self.WORKER_HEARTBEAT_STALE_SECONDS = _get_int(
            "WORKER_HEARTBEAT_STALE_SECONDS", default=300
        )
        self.QUEUE_GROWTH_WARNING_THRESHOLD = _get_int(
            "QUEUE_GROWTH_WARNING_THRESHOLD", default=20
        )
        self.STUCK_RUN_THRESHOLD_MINUTES = _get_int(
            "STUCK_RUN_THRESHOLD_MINUTES", default=120
        )
        self.REPEATED_FAILURE_THRESHOLD = _get_int(
            "REPEATED_FAILURE_THRESHOLD", default=3
        )
        self.PROVIDER_FAILURE_THRESHOLD = _get_int(
            "PROVIDER_FAILURE_THRESHOLD", default=3
        )
        self.DISK_FREE_WARNING_PCT = _get_float("DISK_FREE_WARNING_PCT", default=10.0) or 10.0
        self.WORKER_MEMORY_WARNING_MB = _get_int(
            "WORKER_MEMORY_WARNING_MB", default=2048
        )

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    def apply_channel_config(self, channel_config: "ChannelConfig") -> "Config":  # type: ignore[name-defined]
        """
        Override model/voice settings with values from a DB channel profile.

        Only fields that are explicitly set (non-None, non-empty) in the
        channel's ModelProfile replace the current Config values. .env values
        remain as-is for any field the channel profile does not specify.

        Args:
            channel_config: Resolved ChannelConfig from load_channel_config().

        Returns:
            self — for optional chaining.
        """
        models = channel_config.models
        if models is None:
            return self

        if models.episode_producer:
            self.EP_MODEL = models.episode_producer
        if models.research_director:
            self.RD_MODEL = models.research_director
        if models.thumbnail_planner:
            self.TD_PLANNING_MODEL = models.thumbnail_planner
        if models.brand_guardian_p1:
            self.BGA_PASS1_MODEL = models.brand_guardian_p1
        if models.brand_guardian_p2:
            self.BGA_PASS2_MODEL = models.brand_guardian_p2
        if models.narration_voice_id:
            self.ELEVENLABS_VOICE_ID = models.narration_voice_id

        return self

    def apply_short_native_preset(self, clip_seconds: Optional[float] = None) -> "Config":
        """
        Put this Config into short-native mode: one vertical ~45-60s
        deliverable per run, no long-form arc, no derived Shorts.

        Idempotent. Called from __init__ when SHORT_NATIVE_MODE=true in the
        environment, and from pipeline.run_pipeline() when the resolved
        channel's production_cadence is shorts-only.

        `clip_seconds` (optional): the real per-clip length of the model this
        channel will actually use. SHORT_NATIVE_MAX_SCENES defaults to 8 —
        implicitly "8 clips x 8s = a full ~45-60s short". A channel on a
        shorter-clip model (seedance ~5s) needs proportionally MORE scenes to
        cover the same runtime and hold the full story arc; with the default 8
        the ending beats get no video and the short is cut off. When given,
        the scene cap is scaled by 8.0 / clip_seconds (never below the
        configured value).

        Returns self for chaining.
        """
        self.SHORT_NATIVE_MODE = True
        # Reuse the fully-plumbed scene-cap machinery (scene_planner /
        # visual_package_director already honor EPISODE_ZERO_MAX_SCENES, and
        # pipeline already forces skip_shorts when it is > 0).
        cap = self.SHORT_NATIVE_MAX_SCENES if self.SHORT_NATIVE_MAX_SCENES > 0 else 2
        if clip_seconds and clip_seconds > 0 and clip_seconds < 8.0:
            # Model-aware call (from the pipeline, which knows the channel's
            # real clip length): scale the cap up and make it authoritative —
            # a shorter clip needs MORE scenes for the same story, so this is
            # the one case where the cap is allowed to rise, not only tighten.
            import math as _math
            self.EPISODE_ZERO_MAX_SCENES = _math.ceil(cap * 8.0 / float(clip_seconds))
        elif self.EPISODE_ZERO_MAX_SCENES <= 0 or self.EPISODE_ZERO_MAX_SCENES > cap:
            self.EPISODE_ZERO_MAX_SCENES = cap
        # One native vertical IS the whole deliverable — never derive Shorts.
        self.ENABLE_DEDICATED_SHORT_GENERATION = False
        return self

    @property
    def tiktok_available(self) -> bool:
        """
        True if all four TikTok credentials are present.
        Publishing Agent uses this instead of checking each var individually.
        A False return is not an error — TikTok publishing degrades gracefully.
        """
        return all([
            self.TIKTOK_CLIENT_KEY,
            self.TIKTOK_CLIENT_SECRET,
            self.TIKTOK_ACCESS_TOKEN,
            self.TIKTOK_OPEN_ID,
        ])

    # ------------------------------------------------------------------
    # Milestone validation
    # ------------------------------------------------------------------

    def validate_for_milestone(self, milestone: str) -> None:
        """
        Verify all hard-required variables for the given milestone are set.
        Call this at the top of each milestone's pipeline entry point.

        Args:
            milestone: One of "M0", "M1", "M2", "M3", "M4", "M5" (case-insensitive).

        Raises:
            MilestoneConfigError: Lists every missing variable with its purpose.
            ValueError: If milestone name is not recognised.
        """
        key = milestone.upper()
        requirements = _MILESTONE_REQUIREMENTS.get(key)
        if requirements is None:
            valid = list(_MILESTONE_REQUIREMENTS.keys())
            raise ValueError(
                f"Unknown milestone {milestone!r}. Valid values: {valid}"
            )
        missing = [
            (name, purpose)
            for name, purpose in requirements
            if not getattr(self, name, None)
        ]
        if missing:
            raise MilestoneConfigError(milestone, missing)

    # ------------------------------------------------------------------
    # Safe representation (never prints secret values)
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        def _status(val: object) -> str:
            return "SET" if val else "not set"

        return (
            "Config(\n"
            f"  # OpenRouter\n"
            f"  OPENROUTER_API_KEY          = {_status(self.OPENROUTER_API_KEY)}\n"
            f"  OPENROUTER_SONNET_MODEL_ID  = {self.OPENROUTER_SONNET_MODEL_ID}\n"
            f"  OPENROUTER_OPUS_MODEL_ID    = {self.OPENROUTER_OPUS_MODEL_ID}\n"
            f"  # Perplexity\n"
            f"  PERPLEXITY_API_KEY          = {_status(self.PERPLEXITY_API_KEY)}\n"
            f"  PERPLEXITY_MODEL            = {self.PERPLEXITY_MODEL}\n"
            f"  # MUAPI\n"
            f"  MUAPI_API_KEY               = {_status(self.MUAPI_API_KEY)}\n"
            f"  MUAPI_BASE_URL              = {self.MUAPI_BASE_URL}\n"
            f"  MUAPI_T1_MODEL              = {self.MUAPI_T1_MODEL}\n"
            f"  MUAPI_T2_MODEL              = {self.MUAPI_T2_MODEL}\n"
            f"  MUAPI_T3_MODEL              = {self.MUAPI_T3_MODEL}\n"
            f"  MUAPI_I2V_MODEL             = {self.MUAPI_I2V_MODEL}\n"
            f"  MUAPI_IMAGE_MODEL           = {self.MUAPI_IMAGE_MODEL}\n"
            f"  MUAPI_DEFAULT_TIER          = {self.MUAPI_DEFAULT_TIER}\n"
            f"  MUAPI_TIMEOUT_SECONDS       = {self.MUAPI_TIMEOUT_SECONDS}\n"
            f"  MUAPI_POLL_INTERVAL_SECONDS = {self.MUAPI_POLL_INTERVAL_SECONDS}\n"
            f"  MUAPI_MAX_POLL_ATTEMPTS     = {self.MUAPI_MAX_POLL_ATTEMPTS}\n"
            f"  MUAPI_QUOTA_RETRY_MAX_ATTEMPTS = {self.MUAPI_QUOTA_RETRY_MAX_ATTEMPTS}\n"
            f"  MUAPI_QUOTA_RETRY_DELAY_SECONDS = {self.MUAPI_QUOTA_RETRY_DELAY_SECONDS}\n"
            f"  # ElevenLabs\n"
            f"  ELEVENLABS_API_KEY          = {_status(self.ELEVENLABS_API_KEY)}\n"
            f"  ELEVENLABS_VOICE_ID         = {_status(self.ELEVENLABS_VOICE_ID)}\n"
            f"  ELEVENLABS_MUSIC_DEFAULT_DURATION = {self.ELEVENLABS_MUSIC_DEFAULT_DURATION}s\n"
            f"  # Whisper\n"
            f"  OPENAI_API_KEY              = {_status(self.OPENAI_API_KEY)}\n"
            f"  WHISPER_MODEL               = {self.WHISPER_MODEL}\n"
            f"  # YouTube\n"
            f"  YOUTUBE_CLIENT_ID           = {_status(self.YOUTUBE_CLIENT_ID)}\n"
            f"  YOUTUBE_CHANNEL_ID          = {_status(self.YOUTUBE_CHANNEL_ID)}\n"
            f"  YOUTUBE_DEFAULT_CATEGORY_ID = {self.YOUTUBE_DEFAULT_CATEGORY_ID}\n"
            f"  # TikTok\n"
            f"  tiktok_available            = {self.tiktok_available}\n"
            f"  # Meta\n"
            f"  META_APP_ID                 = {_status(self.META_APP_ID)}\n"
            f"  META_GRAPH_API_VERSION      = {self.META_GRAPH_API_VERSION}\n"
            f"  META_STAGING_BASE_URL       = {_status(self.META_STAGING_BASE_URL)}\n"
            ")"
        )
