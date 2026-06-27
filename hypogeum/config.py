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
    OPENROUTER_FALLBACK_MODEL: Optional[str]
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
    MUAPI_T2_MODEL: str                       # default: veo3-fast-text-to-video ($0.60/gen, primary production tier)
    MUAPI_T3_MODEL: str                       # default: veo3-fast-text-to-video ($0.60/gen, cheap-mode alias)
    MUAPI_I2V_MODEL: str                      # default: veo3-image-to-video
    MUAPI_IMAGE_MODEL: str                    # legacy alias — use THUMBNAIL_IMAGE_MODEL
    THUMBNAIL_IMAGE_PROVIDER: str             # default: openrouter (primary)
    THUMBNAIL_IMAGE_MODEL: str               # default: gpt-image-2-text-to-image (MUAPI path only)
    THUMBNAIL_IMAGE_RESOLUTION: str          # default: 2K (GPT Image 2 only)
    THUMBNAIL_IMAGE_QUALITY: str             # default: high (GPT Image 2 only)
    THUMBNAIL_FALLBACK_PROVIDER: str         # default: openai  (fallback #1 when primary fails)
    THUMBNAIL_FALLBACK2_PROVIDER: str        # default: muapi   (fallback #2 when fallback #1 fails)
    THUMBNAIL_FALLBACK_MODEL: str            # default: flux-2-dev (MUAPI-internal model fallback)
    MUAPI_DEFAULT_TIER: int                   # default: 2 (VPD routing fallback)
    MUAPI_TIMEOUT_SECONDS: int                # default: 180 (Veo3 Premium took 128.5s in benchmark)
    MUAPI_POLL_INTERVAL_SECONDS: int          # default: 5
    MUAPI_MAX_POLL_ATTEMPTS: int              # default: 24
    MUAPI_WEBHOOK_URL: Optional[str]
    MUAPI_CREDIT_WARNING_THRESHOLD: Optional[float]  # USD; default: None (off)

    # M5.6 — Cost guardrails
    MUAPI_EPISODE_BUDGET_CREDITS: float   # 0 = unlimited; default: 0
    MUAPI_SCENE_BUDGET_CREDITS: float     # 0 = unlimited; default: 0
    MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE: int  # 0 = unlimited; default: 0
    EPISODE_ZERO_MAX_SCENES: int          # 0 = disabled; default: 0
    MUAPI_CHEAP_MODE: bool                # skip T1/T2, use T3 only; default: false
    MUAPI_RUNWAY_ONLY_MODE: bool          # force T2 for all video scenes (T2=veo3-fast since 2026-06-27); default: false
    MUAPI_DRY_RUN: bool                   # no real API calls; default: false
    MUAPI_ALLOW_PAID_FALLBACKS: bool      # allow automatic paid fallback; default: false
    ENABLE_DEDICATED_SHORT_GENERATION: bool  # allow VPD to plan/generate 9:16 Short scenes; default: false

    # ------------------------------------------------------------------
    # Smoke Test Mode — minimal end-to-end run with capped scene/beat counts
    # ------------------------------------------------------------------
    SMOKE_TEST_MODE: bool                # master switch; default: false
    SMOKE_TEST_MAX_NARRATION_BEATS: int  # 0 = unlimited; default: 0

    # ------------------------------------------------------------------
    # Brand Guardian Agent (BGA v1A) — pre-generation brand compliance gate
    # ------------------------------------------------------------------
    BGA_ENABLED: bool  # run BGA Pass 1 + Pass 2 before MUAPI generation; default: false

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
        self.OPENROUTER_FALLBACK_MODEL = _get_optional("OPENROUTER_FALLBACK_MODEL")
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
        self.MUAPI_T2_MODEL = _get_optional("MUAPI_T2_MODEL", default="veo3-fast-text-to-video")
        self.MUAPI_T3_MODEL = _get_optional("MUAPI_T3_MODEL", default="veo3-fast-text-to-video")
        self.MUAPI_I2V_MODEL = _get_optional("MUAPI_I2V_MODEL", default="veo3-image-to-video")
        self.MUAPI_IMAGE_MODEL = _get_optional("MUAPI_IMAGE_MODEL", default="flux-2-dev")  # legacy
        self.THUMBNAIL_IMAGE_PROVIDER = _get_optional("THUMBNAIL_IMAGE_PROVIDER", default="openrouter")
        self.THUMBNAIL_IMAGE_MODEL = _get_optional(
            "THUMBNAIL_IMAGE_MODEL", default="gpt-image-2-text-to-image"
        )
        self.THUMBNAIL_IMAGE_RESOLUTION = _get_optional("THUMBNAIL_IMAGE_RESOLUTION", default="2K")
        self.THUMBNAIL_IMAGE_QUALITY = _get_optional("THUMBNAIL_IMAGE_QUALITY", default="high")
        self.THUMBNAIL_FALLBACK_PROVIDER = _get_optional("THUMBNAIL_FALLBACK_PROVIDER", default="openai")
        self.THUMBNAIL_FALLBACK2_PROVIDER = _get_optional("THUMBNAIL_FALLBACK2_PROVIDER", default="muapi")
        self.THUMBNAIL_FALLBACK_MODEL = _get_optional("THUMBNAIL_FALLBACK_MODEL", default="flux-2-dev")
        self.MUAPI_DEFAULT_TIER = _get_int("MUAPI_DEFAULT_TIER", default=2)
        self.MUAPI_TIMEOUT_SECONDS = _get_int("MUAPI_TIMEOUT_SECONDS", default=180)
        self.MUAPI_POLL_INTERVAL_SECONDS = _get_int("MUAPI_POLL_INTERVAL_SECONDS", default=5)
        self.MUAPI_MAX_POLL_ATTEMPTS = _get_int("MUAPI_MAX_POLL_ATTEMPTS", default=24)
        self.MUAPI_WEBHOOK_URL = _get_optional("MUAPI_WEBHOOK_URL")
        self.MUAPI_CREDIT_WARNING_THRESHOLD = _get_float("MUAPI_CREDIT_WARNING_THRESHOLD")

        # M5.6 — Cost guardrails
        self.MUAPI_EPISODE_BUDGET_CREDITS = _get_float("MUAPI_EPISODE_BUDGET_CREDITS", default=0.0) or 0.0
        self.MUAPI_SCENE_BUDGET_CREDITS = _get_float("MUAPI_SCENE_BUDGET_CREDITS", default=0.0) or 0.0
        self.MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE = _get_int("MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE", default=0)
        self.EPISODE_ZERO_MAX_SCENES = _get_int("EPISODE_ZERO_MAX_SCENES", default=0)
        self.MUAPI_CHEAP_MODE = _get_bool("MUAPI_CHEAP_MODE", default=False)
        self.MUAPI_RUNWAY_ONLY_MODE = _get_bool("MUAPI_RUNWAY_ONLY_MODE", default=False)
        self.MUAPI_DRY_RUN = _get_bool("MUAPI_DRY_RUN", default=False)
        self.MUAPI_ALLOW_PAID_FALLBACKS = _get_bool("MUAPI_ALLOW_PAID_FALLBACKS", default=False)
        self.ENABLE_DEDICATED_SHORT_GENERATION = _get_bool(
            "ENABLE_DEDICATED_SHORT_GENERATION", default=False
        )

        # --- Smoke Test Mode ---
        self.SMOKE_TEST_MODE = _get_bool("SMOKE_TEST_MODE", default=False)
        self.SMOKE_TEST_MAX_NARRATION_BEATS = _get_int("SMOKE_TEST_MAX_NARRATION_BEATS", default=0)
        if self.SMOKE_TEST_MODE:
            if self.EPISODE_ZERO_MAX_SCENES == 0:
                self.EPISODE_ZERO_MAX_SCENES = 2
            # Force T2 for all scenes in smoke test — T2=veo3-fast-text-to-video since 2026-06-27
            if not self.MUAPI_RUNWAY_ONLY_MODE and not self.MUAPI_CHEAP_MODE:
                self.MUAPI_RUNWAY_ONLY_MODE = True

        # --- Brand Guardian Agent ---
        self.BGA_ENABLED = _get_bool("BGA_ENABLED", default=False)

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
