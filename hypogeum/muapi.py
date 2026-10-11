"""
hypogeum/muapi.py — MUAPI video and image generation client

Single integration point for all MUAPI calls in the pipeline.
Visual Package Director and Thumbnail Director are the only agents that call this module.

Confirmed API surface (verified 2026-06-05 via live API calls):
  Auth:    x-api-key header (NOT Authorization: Bearer)
  Base:    https://api.muapi.ai/api/v1
  Submit:  POST /api/v1/{model-slug}      (model-specific, NOT generic /jobs)
  Poll:    GET  /api/v1/predictions/{request_id}/result
  Upload:  POST /api/v1/upload_file       (multipart; for reference image hosting)
  Statuses: queued | pending | processing | completed | failed | cancelled
  Result:  completed response → {"outputs": ["url"], "status": "completed", ...}
           NOTE: response key is "outputs" (list of URLs), NOT "images" or "video".

The frozen muapi-integration.md spec uses placeholder API details written pre-verification.
This module implements the CONFIRMED surface. See docs/architecture-index.md §MUAPI API
Corrections for the full discrepancy table.

Tier → model mapping (no `tier` parameter exists in the API):
  Tier 1 → config.MUAPI_T1_MODEL  (default: veo3-text-to-video,        $2.50/gen, fixed 8s)  # premium/hero scenes; explicit override
  Tier 2 → config.MUAPI_T2_MODEL  (default: veo3.1-lite-text-to-video, $0.39/gen, fixed 8s, 1080p candidate)  # confirmed 2026-07-06; primary production tier; .env overrides default
  Tier 3 → config.MUAPI_T3_MODEL  (default: veo3.1-lite-text-to-video, $0.39/gen, fixed 8s, 1080p candidate)  # cheap-mode alias, same model as T2
  I2V    → config.MUAPI_I2V_MODEL (default: veo3-image-to-video,      $2.50/gen, fixed 8s)
  Image  → config.MUAPI_IMAGE_MODEL (default: gpt-image-2-text-to-image, $0.09/gen; changed 2026-09-05 from flux-2-dev, which has no "aspect_ratio" input field at all — every reference image silently came back square regardless of the requested orientation)

Duration constraints:
  Veo3 models:   duration is fixed by the provider (8s) — no duration parameter sent
  Runway models: duration must be 5 or 8 (integer enum) — legacy path, not in production routing since 2026-06-27

Path A reference image conditioning:
  generate_reference_image() creates a reference image, downloads it locally,
  uploads it to MUAPI storage to obtain a stable hosted URL, and returns both.
  VPD passes the hosted_url in scene_spec production_spec.reference_image_url.
  submit_scene() detects a non-null reference_image_url and routes to the I2V model.

Integration contracts:
  - All environment access goes through the Config object — never os.getenv()
  - All logging goes through hypogeum.logger.get_logger() — never print() or stdlib logging
  - MUAPI_API_KEY is never logged, printed, or stored in any package or asset file
  - Scene generation is sequential only — no parallel MUAPI calls
  - Two-attempt structure per scene: primary_prompt → fallback_prompt
  - Max 1 HTTP retry per attempt on 429/5xx transient errors

Logging events emitted:
  muapi_job_submitted              — info  — job accepted, request_id received
  muapi_poll_cycle                 — debug — each poll iteration
  muapi_job_completed              — info  — job reached completed status
  muapi_job_failed                 — warn  — job reached failed status
  muapi_fallback_triggered         — warn  — primary attempt failed, trying fallback
  muapi_scene_both_attempts_failed — error — both attempts failed, scene skipped
  muapi_download_completed         — info  — video file written to disk
  muapi_download_failed            — error — download failed (after retry)
  muapi_timeout                    — warn  — job did not complete within allowed window
  muapi_rate_limit_hit             — warn  — 429 received, applying Retry-After
  muapi_hard_failure               — error — 400/403/422 on this attempt
  muapi_auth_failure               — error — 401; halts all generation
  muapi_quota_exhausted            — error — 402; halts all generation
  muapi_health_check_started       — info  — before health check
  muapi_health_check_result        — info/warn/error — health check outcome
  muapi_reference_image_generated  — info  — reference image created and uploaded
  muapi_thumbnail_generated        — info  — thumbnail image generated and saved locally
  muapi_upload_completed           — info  — file uploaded to MUAPI storage
  muapi_credit_warning             — warn  — remaining balance below threshold

M5.6 — Cost guardrail events:
  muapi_scene_reused               — info  — valid existing file found; no submission made
  muapi_cheap_mode_active          — info  — cheap mode forced T3 for this scene
  muapi_runway_only_mode_active    — info  — runway-only mode forced T2 for this scene
  muapi_fallback_skipped           — warn  — cheap mode; no fallback to submit
  muapi_fallback_disabled          — warn  — MUAPI_ALLOW_PAID_FALLBACKS=false; attempt 2 skipped
  muapi_dry_run_submission         — info  — dry-run; fake request_id returned, no API call
  muapi_dry_run_complete           — info  — dry-run; manifest returned without download
  muapi_poll_400_early_abort       — warn  — 3+ consecutive HTTP 400 poll responses; aborting
  muapi_budget_check_passed        — info  — budget guard approved this submission
"""

from __future__ import annotations

import json
import random
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import ClassVar, Optional

import requests
from PIL import Image

from hypogeum.config import Config
from hypogeum.logger import get_logger
import hypogeum.finance as _finance
from hypogeum.brand import visual_identity
from hypogeum import video_dna
from hypogeum import video_prompt_writer
from hypogeum import shadow_observation as _shadow_observation
from hypogeum.execution_envelope import GenerationExecutionEnvelope


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MUAPI models with fixed 8-second output (duration param must NOT be sent)
_VEO3_MODELS: frozenset[str] = frozenset({
    "veo3-text-to-video",
    "veo3-fast-text-to-video",
    "veo3-image-to-video",
})

# Veo3.1 models — explicitly send duration + resolution (1080p supported; $0.39/gen)
# Confirmed 2026-07-06: veo3.1-lite-text-to-video ($0.39) is the correct lower-cost
# path. veo3.1-fast costs $0.78 and does not honour resolution="1080p" (provider
# mismatch confirmed via ffprobe). Duration and resolution MUST be sent explicitly.
_VEO3_1_MODELS: frozenset[str] = frozenset({
    "veo3.1-lite-text-to-video",
    "veo3.1-fast-text-to-video",   # kept for completeness; costs $0.78, ignores resolution
})
_VEO3_1_DEFAULT_DURATION: int = 8
_VEO3_1_DEFAULT_RESOLUTION: str = "1080p"

# MUAPI models that accept a duration enum (5 or 8 only)
_RUNWAY_MODELS: frozenset[str] = frozenset({
    "runway-text-to-video",
    "runway-image-to-video",
})

# MUAPI models that produce images (output field: `images`, not `video`)
_IMAGE_MODELS: frozenset[str] = frozenset({
    "flux-dev-image",
    "flux-schnell-image",
    "flux-kontext-dev-t2i",
    "flux-kontext-dev-i2i",
})

# GPT Image 2 models — require resolution + quality in payload (outputs[] same as others)
_GPT_IMAGE_2_MODELS: frozenset[str] = frozenset({
    "gpt-image-2-text-to-image",
    "gpt-image-2-image-to-image",
})

# Terminal job statuses
_JOB_STATUS_COMPLETED   = "completed"
_JOB_STATUS_FAILED      = "failed"
_JOB_STATUS_CANCELLED   = "cancelled"
_JOB_STATUS_TERMINAL    = frozenset({_JOB_STATUS_COMPLETED, _JOB_STATUS_FAILED, _JOB_STATUS_CANCELLED})
_JOB_STATUS_IN_PROGRESS = frozenset({"queued", "pending", "processing"})

# HTTP status code classification (per spec §7)
# 401 → halt ALL generation (raise exception) -- an invalid/missing API key
#       needs operator intervention, no retry can fix it.
# 402 → bounded, delayed retry first (2026-08-24: an operator-managed Auto
#       Reload on the provider account needs real time to top up; a real
#       balance snapshot alone must never halt generation -- only a
#       persistent 402 across every bounded retry does, and even then as a
#       retryable provider/account failure, never a hardcoded terminal one).
#       See _retry_muapi_quota_exhausted() below.
# 400, 403, 422 → hard fail this attempt only (try fallback)
# 429, 500, 503 → retry once within this attempt
_HALT_FAILURE_CODES: frozenset[int] = frozenset({401, 402})
_ATTEMPT_HARD_FAIL_CODES: frozenset[int] = frozenset({400, 403, 422})
_SOFT_RETRY_CODES: frozenset[int] = frozenset({429, 500, 503})

# Retry policy (per spec §8)
_RETRY_MAX_HTTP           = 1      # per-attempt HTTP retries (separate from 2-attempt structure)
_RETRY_INITIAL_DELAY      = 10.0   # seconds (longer than Perplexity — generation has higher restart cost)
_RETRY_MULTIPLIER         = 2.0
_RETRY_MAX_DELAY          = 120.0  # seconds
_RETRY_JITTER_FRACTION    = 0.25   # ±25%
_RETRY_AFTER_DEFAULT      = 60.0   # seconds (when 429 Retry-After header is absent)

# Download retry
_DOWNLOAD_RETRY_MAX = 1

# Health check
_HEALTH_CHECK_PROMPT  = "abstract atmospheric fog, dark background, slow drift"
_HEALTH_CHECK_TIMEOUT = 10.0  # seconds — response faster than this = available

# Minimum valid video file size (catches zero-byte and truncated downloads)
_MIN_VIDEO_BYTES = 10_000  # 10 KB

# Consecutive HTTP 400 responses on poll before treating as a hard failure
_POLL_400_ABORT_THRESHOLD = 3

# Dry-run fake request ID prefix
_DRY_RUN_REQUEST_ID_PREFIX = "dry-run-"

# Known model costs in MUAPI credits (used for pre-submission budget estimation)
# Confirmed 2026-06-05 via live probe — runway-text-to-video = 12 credits = $0.15/clip (NOT $0.09)
MODEL_CREDIT_COSTS: dict[str, float] = {
    "veo3-text-to-video": 2.5,
    "veo3-image-to-video": 2.5,
    "veo3-fast-text-to-video": 0.6,
    "veo3.1-lite-text-to-video": 0.39,   # confirmed 2026-07-06: $0.39, better quality, 1080p candidate
    "veo3.1-fast-text-to-video": 0.78,  # confirmed 2026-07-06: $0.78 actual cost; ignores resolution param
    "runway-text-to-video": 0.15,   # confirmed 2026-06-05 probe: 12 credits @ $0.0125/credit
    "runway-image-to-video": 0.15,  # assumed same tier as T2V
    "flux-2-dev": 0.015,
    "flux-schnell-image": 0.01,
    "flux-kontext-dev-t2i": 0.02,
    "flux-kontext-dev-i2i": 0.02,
    "bytedance-seedream-v4": 0.09,
    "wan2.7-text-to-image": 0.05,
    # Video T2V/I2V — the MUAPI catalogue marks these dynamic_pricing and
    # lists a floor ($0.10) that is NOT what production settings (portrait,
    # 8s, audio) are billed. These are the effective per-clip prices
    # measured from real production charges (charged_amount), which is
    # stronger evidence than a catalogue probe:
    "wan2.7-text-to-video": 0.65,        # measured 2026-09-03: $0.65 flat, 53/53 calls (5s clip)
    "wan2.7-image-to-video": 0.65,       # same family/tier; keep aligned pending its own measurement
    "seedance-pro-t2v-fast": 0.10,       # measured 2026-09-03: $0.10 flat, 81/81 calls (~5s clip)
    "seedance-pro-i2v-fast": 0.10,       # measured 2026-09-03: $0.10 flat, 18/18 calls
    # GPT Image 2 via MUAPI (confirmed 2026-06-11 — primary thumbnail model)
    "gpt-image-2-text-to-image": 0.09,
    "gpt-image-2-image-to-image": 0.09,
}
_DEFAULT_UNKNOWN_MODEL_COST: float = 2.5  # conservative fallback for unknown models

# Minimum valid image file size
_MIN_IMAGE_BYTES = 1_000   # 1 KB

# Upload endpoint (relative path from base URL)
_UPLOAD_PATH = "upload_file"

# Prompt log limit — prompts logged at debug only, truncated
_PROMPT_LOG_MAX_CHARS = 200


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ThumbnailResult:
    """
    Result of generate_thumbnail().

    TD stores asset_id as main_thumbnail.asset_id or short_thumbnails[i].asset_id
    in the Thumbnail Package. local_path is the filesystem location for Assembly Agent
    to read the final image file.
    """
    asset_id: str
    """Relative path from .hypogeum-assets/ without extension.
    Example: episodes/ep_001/thumbnails/main"""

    local_path: Path
    """Absolute filesystem path to the downloaded image file."""

    asset_class: str = "thumbnail_image"
    """Asset class sentinel — must always be 'thumbnail_image'. Never feed to I2V."""

    model_used: str = ""
    """Exact MUAPI model slug that produced this thumbnail (e.g. 'gpt-image-2-text-to-image')."""

    provider_used: str = "muapi"
    """Provider name — always 'muapi' in the current implementation."""

    used_fallback: bool = False
    """True if the fallback model was used because the primary model failed."""


@dataclass
class ReferenceImageResult:
    """
    Result of generate_reference_image().

    VPD stores asset_id as locked_entities[].reference_image_asset_id in the
    Visual Package. VPD passes hosted_url as scene_spec.production_spec.reference_image_url
    for all subsequent scenes involving this entity.
    """
    asset_id: str
    """Relative path from .hypogeum-assets/ without extension.
    Example: episodes/ep_001/video/ref_entity_pharaoh"""

    hosted_url: str
    """MUAPI-hosted URL suitable for use in images_list[] on I2V job submissions."""

    asset_class: str = "reference_image"
    """Asset class sentinel — must always be 'reference_image'. Never use as thumbnail."""


# Asset-class constants for runtime validation
_ASSET_CLASS_THUMBNAIL = "thumbnail_image"
_ASSET_CLASS_REFERENCE = "reference_image"


def assert_asset_class(result: object, expected_class: str) -> None:
    """
    Raise AssetClassError if result.asset_class != expected_class.

    Use at every handoff boundary to prevent thumbnail images from being
    passed to I2V generation and reference images from being published as thumbnails.
    """
    actual = getattr(result, "asset_class", None)
    if actual != expected_class:
        raise AssetClassError(
            f"Asset class mismatch: expected={expected_class!r} actual={actual!r} "
            f"asset_id={getattr(result, 'asset_id', '?')!r}"
        )


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class MUAPIError(RuntimeError):
    """Base class for all MUAPI client errors."""


class AssetClassError(ValueError):
    """
    Raised when an asset is passed to a consumer that requires a different asset_class.

    Prevents thumbnail images from being fed to I2V generation and reference images
    from being published as thumbnails.
    """


class MUAPIAuthFailure(MUAPIError):
    """
    HTTP 401 — API key missing or invalid.

    This halts ALL MUAPI generation for the current episode run.
    Callers must issue a PDR: severity BLOCKING, type provider_authentication_failure.
    """
    def __init__(self) -> None:
        super().__init__(
            "MUAPI authentication failed (HTTP 401). "
            "Check MUAPI_API_KEY in environment. All scene generation halted."
        )


class MUAPIQuotaExhausted(MUAPIError):
    """
    HTTP 402 — Account quota exhausted or billing issue, raised only after
    _retry_muapi_quota_exhausted() has already retried the SAME real
    request config.MUAPI_QUOTA_RETRY_MAX_ATTEMPTS times with a real
    config.MUAPI_QUOTA_RETRY_DELAY_SECONDS delay between attempts (2026-08-24
    -- an operator-managed Auto Reload needs real time to top up the
    account; a single low-balance snapshot must never halt generation by
    itself).

    Deliberately has no failure_policy override -- defaults to
    FailurePolicy.TRANSIENT (see hypogeum.failure_policy), so a persistent
    402 after every bounded retry is still a retryable provider/account
    failure at the queue level, never a hardcoded terminal one. Callers
    that need an out-of-band signal should still issue a PDR (severity
    BLOCKING, type provider_quota_exhausted) for operator visibility --
    that is about notification, not about permanently killing the episode.
    """
    def __init__(self) -> None:
        super().__init__(
            "MUAPI quota exhausted (HTTP 402) after every bounded retry. "
            "Account billing issue requires resolution. All scene generation halted for now."
        )


def _retry_muapi_quota_exhausted(
    attempt: int, config: "Config", log, scene_id: str,
) -> bool:
    """
    Generic, provider-account-level retry policy for a REAL HTTP 402
    response -- never Hypogeum-specific, never stored as channel identity
    (see Config.MUAPI_QUOTA_RETRY_MAX_ATTEMPTS/MUAPI_QUOTA_RETRY_DELAY_SECONDS,
    runtime/provider-account configuration a future dashboard can edit).

    A real HTTP 402 is not necessarily a permanent failure: an
    operator-managed Auto Reload on the MUAPI account can top up the
    balance, but needs real time to process the payment. This function
    never estimates or invents a balance -- it only decides whether the
    caller should wait and re-issue the SAME real request, so the actual,
    current provider state is rechecked every time, never a cached or
    assumed value.

    Args:
        attempt: 0-indexed count of 402 responses already seen for this
                 one real request (the caller re-issues the request and
                 calls this again for each new 402).
        config:  Loaded Config -- MUAPI_QUOTA_RETRY_MAX_ATTEMPTS/
                 MUAPI_QUOTA_RETRY_DELAY_SECONDS.
        log:     Structured logger -- every retry decision is recorded,
                 never silent.
        scene_id: For log correlation only.

    Returns:
        True  — caller should sleep MUAPI_QUOTA_RETRY_DELAY_SECONDS and
                retry the SAME real request.
        False — bounded retries exhausted; caller should raise
                MUAPIQuotaExhausted().
    """
    max_attempts = max(1, getattr(config, "MUAPI_QUOTA_RETRY_MAX_ATTEMPTS", 3))
    delay_seconds = getattr(config, "MUAPI_QUOTA_RETRY_DELAY_SECONDS", 60.0)
    if attempt + 1 >= max_attempts:
        log.error(
            "muapi_quota_exhausted", scene_id=scene_id, http_status=402,
            attempts_made=attempt + 1, max_attempts=max_attempts,
        )
        return False
    log.warn(
        "muapi_quota_exhausted_retry", scene_id=scene_id, http_status=402,
        attempt=attempt + 1, max_attempts=max_attempts,
        retry_delay_seconds=delay_seconds,
    )
    time.sleep(delay_seconds)
    return True


class MUAPIUnavailable(MUAPIError):
    """
    Health check determined MUAPI cannot be reached or is not authenticated.

    VPD run must not begin. Issue PDR: severity BLOCKING.
    """
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            f"MUAPI is unavailable: {reason}. VPD run cannot proceed. Issue PDR."
        )


class MUAPIBudgetExhausted(MUAPIError):
    """
    M5.6 — Episode or scene budget limit reached.

    Raised before submitting a paid job when the configured credit limit would be
    exceeded. Halts all further MUAPI generation for this episode run. Callers
    should finalize whatever scenes succeeded and continue with downstream agents
    if at least some scenes were generated.
    """
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            f"MUAPI budget exhausted: {reason}. No further paid submissions will be made."
        )


# ---------------------------------------------------------------------------
# M5.6 — Budget Guard
# ---------------------------------------------------------------------------

@dataclass
class MUAPIBudgetGuard:
    """
    Per-episode budget guard. Tracks estimated and actual credit spend.

    Create one instance per pipeline run. Pass it to submit_scene(),
    generate_reference_image(), and generate_thumbnail() via the budget_guard
    keyword argument. The guard checks limits before every paid submission and
    raises MUAPIBudgetExhausted to halt generation cleanly.

    Limits (0 = unlimited):
      episode_budget_credits    — total credits allowed across all scenes
      scene_budget_credits      — max credits per scene_id across all attempts
      max_paid_attempts         — max paid job submissions per scene_id

    Logging: all budget checks and decisions are logged via the provided log.
    """
    episode_budget_credits: float
    scene_budget_credits: float
    max_paid_attempts: int
    log: object

    _estimated_spend: float = field(default=0.0, init=False, repr=False)
    _actual_spend: float = field(default=0.0, init=False, repr=False)
    _scene_attempts: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _scene_estimated_spend: dict[str, float] = field(default_factory=dict, init=False, repr=False)

    def check_and_record_attempt(self, scene_id: str, model: str) -> None:
        """
        Check all budget limits before submitting a paid job.

        Records the attempt and estimated cost if the check passes.
        Raises MUAPIBudgetExhausted if any limit would be exceeded.
        """
        cost = MODEL_CREDIT_COSTS.get(model, _DEFAULT_UNKNOWN_MODEL_COST)
        attempts_so_far = self._scene_attempts.get(scene_id, 0)
        scene_spend_so_far = self._scene_estimated_spend.get(scene_id, 0.0)

        if self.max_paid_attempts > 0 and attempts_so_far >= self.max_paid_attempts:
            raise MUAPIBudgetExhausted(
                f"scene {scene_id!r} already used {attempts_so_far} paid attempt(s) "
                f"(max_paid_attempts={self.max_paid_attempts})"
            )

        if self.scene_budget_credits > 0 and scene_spend_so_far + cost > self.scene_budget_credits:
            raise MUAPIBudgetExhausted(
                f"scene {scene_id!r} would exceed scene budget "
                f"({scene_spend_so_far + cost:.2f} > {self.scene_budget_credits:.2f} credits)"
            )

        if self.episode_budget_credits > 0 and self._estimated_spend + cost > self.episode_budget_credits:
            raise MUAPIBudgetExhausted(
                f"episode budget would be exceeded by {scene_id!r}: "
                f"{self._estimated_spend + cost:.2f} > {self.episode_budget_credits:.2f} credits "
                f"(model={model}, cost={cost})"
            )

        self._scene_attempts[scene_id] = attempts_so_far + 1
        self._scene_estimated_spend[scene_id] = scene_spend_so_far + cost
        self._estimated_spend += cost

        self.log.info(
            "muapi_budget_check_passed",
            scene_id=scene_id,
            model=model,
            estimated_cost=cost,
            cumulative_estimated=round(self._estimated_spend, 4),
            episode_budget=self.episode_budget_credits if self.episode_budget_credits > 0 else "unlimited",
            scene_attempt=self._scene_attempts[scene_id],
        )

    def record_actual_spend(self, credits_used: float) -> None:
        """Record confirmed credits_used from a completed download event."""
        self._actual_spend += credits_used

    @property
    def estimated_spend(self) -> float:
        return self._estimated_spend

    @property
    def actual_spend(self) -> float:
        return self._actual_spend


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_muapi_health(
    config: Config,
    episode_id: Optional[str] = None,
    agent: Optional[str] = None,
) -> str:
    """
    Verify MUAPI reachability and authentication before scene generation.

    Submits a minimal Tier 3 job and verifies a request_id is returned within
    the health check timeout. Does NOT wait for the job to complete.

    Args:
        config:     Loaded Config instance.
        episode_id: Optional — for log correlation.
        agent:      Optional — for log correlation.

    Returns:
        "available"   — job submitted and request_id received within 10 seconds.
        "degraded"    — job submitted and request_id received, but response took > 10s,
                        or elevated error rate observed.
        "unavailable" — 401, 402, 403, 503, connection failure, or no valid response.

    Raises:
        Nothing — all errors reflected in return value.
        MUAPIAuthFailure and MUAPIQuotaExhausted are caught and return "unavailable".
    """
    log = get_logger(episode_id=episode_id, agent=agent or "muapi_health")
    t3_model = config.MUAPI_T3_MODEL
    endpoint = f"{config.MUAPI_BASE_URL}/{t3_model}"
    payload = {
        "prompt": _HEALTH_CHECK_PROMPT,
        "aspect_ratio": "16:9",
    }

    log.info(
        "muapi_health_check_started",
        model=t3_model,
        endpoint=endpoint,
    )

    t0 = time.monotonic()
    try:
        resp = requests.post(
            endpoint,
            headers=_get_headers(config),
            json=payload,
            timeout=_HEALTH_CHECK_TIMEOUT + 5.0,  # slightly longer than health check threshold
        )
    except requests.exceptions.Timeout:
        log.error("muapi_health_check_result", status="unavailable", reason="timeout")
        return "unavailable"
    except requests.exceptions.ConnectionError as exc:
        log.error(
            "muapi_health_check_result",
            status="unavailable",
            reason="connection_error",
            error=str(exc),
        )
        return "unavailable"
    except Exception as exc:
        log.error(
            "muapi_health_check_result",
            status="unavailable",
            reason="unexpected_error",
            error=str(exc),
        )
        return "unavailable"

    elapsed = time.monotonic() - t0

    if resp.status_code == 401:
        log.error("muapi_health_check_result", status="unavailable", reason="http_401")
        return "unavailable"
    if resp.status_code == 402:
        log.error("muapi_health_check_result", status="unavailable", reason="http_402")
        return "unavailable"
    if resp.status_code == 403:
        log.error("muapi_health_check_result", status="unavailable", reason="http_403")
        return "unavailable"
    if resp.status_code == 503:
        log.error("muapi_health_check_result", status="unavailable", reason="http_503")
        return "unavailable"
    if resp.status_code not in (200, 202):
        log.error(
            "muapi_health_check_result",
            status="unavailable",
            reason=f"http_{resp.status_code}",
        )
        return "unavailable"

    try:
        data = resp.json()
        request_id = data.get("request_id", "")
    except Exception:
        log.error("muapi_health_check_result", status="unavailable", reason="invalid_json")
        return "unavailable"

    if not request_id:
        log.error(
            "muapi_health_check_result",
            status="unavailable",
            reason="no_request_id",
        )
        return "unavailable"

    if elapsed > _HEALTH_CHECK_TIMEOUT:
        log.warn(
            "muapi_health_check_result",
            status="degraded",
            reason="slow_response",
            response_time_ms=int(elapsed * 1000),
        )
        return "degraded"

    log.info(
        "muapi_health_check_result",
        status="available",
        response_time_ms=int(elapsed * 1000),
    )
    return "available"


_HYPOGEUM_WORKFLOW_ID = "hypogeum_main_production"
_HYPOGEUM_WORKFLOW_VERSION = "1.0.0"
_VIDEO_PROVIDER = "muapi"
_VIDEO_PROVIDER_OPERATION = "video.text_to_video"
_VIDEO_COMPILER_IDENTITY = "hypogeum.video_prompt_writer.assemble_video_scene_prompt"
_VIDEO_ENVELOPE_SCHEMA_VERSION = "1.0"


def _canonical_json_bytes(payload: dict) -> bytes:
    import json as _json
    return _json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _load_post_vpd_finance_plan_hash(
    episode_id: str, output_dir: Path,
) -> "tuple[Optional[str], Optional[str]]":
    """
    Phase 2 plan-bound financial authorization: read the bound Production
    Plan content_hash back from the episode's post-VPD finance record
    (finance_post_vpd_authorization_{episode_id}.json, written by
    run_post_vpd_authorization() only after verifying the exact persisted
    plan). Returns (hash, None) on success or (None, denial_reason) when
    the record is missing, unreadable, or carries no bound hash -- every
    such case fails closed at the paid video boundary before any network
    call. Hash source: top-level 'bound_production_plan_content_hash'
    (Phase 2 records), falling back to
    'cost_breakdown.production_plan_content_hash' (records written since
    the 2026-08-24 plan correction).
    """
    path = Path(output_dir) / f"finance_post_vpd_authorization_{episode_id}.json"
    if not path.exists():
        return None, (
            f"finance_record_missing: no post-VPD finance authorization record at "
            f"{path.name} -- a paid submission requires a Finance-authorized plan "
            f"(run_post_vpd_authorization writes this record)"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"finance_record_unreadable: {path.name} could not be read: {exc}"
    if not isinstance(payload, dict):
        return None, f"finance_record_unreadable: {path.name} is not a JSON object"
    bound_hash = payload.get("bound_production_plan_content_hash")
    if not bound_hash:
        cost_breakdown = payload.get("cost_breakdown")
        if isinstance(cost_breakdown, dict):
            bound_hash = cost_breakdown.get("production_plan_content_hash")
    if not isinstance(bound_hash, str) or not bound_hash:
        return None, (
            f"finance_record_unbound: {path.name} carries no "
            f"production_plan_content_hash -- refusing paid submission against an "
            f"authorization whose plan identity is unprovable"
        )
    return bound_hash, None


def _resolve_authorization_grant(channel_id: str, episode_id: Optional[str], *, at: str):
    """Resolve the RunAuthorizationGrant that authorizes a paid MUAPI call
    for (channel_id, episode_id) -- shared by every muapi provider-gate
    resolution site.

    Resolution order mirrors hypogeum.openrouter's own exactly (ADR-016):
    an execution that hypogeum.runtime_admission has ADMITTED under a
    Channel Operating Grant -- persisted state, read via
    get_admission_binding(), never a caller flag -- MUST route through the
    operating grant with NO paid legacy fallback. Otherwise fall back to a
    per-episode 'run' grant.

    Returns (grant, admitted_under_operating_grant):
      grant not None                 -> route via the gate with this grant.
      grant None,  admitted True     -> admitted but the operating grant no
                                        longer resolves -> caller MUST fail
                                        closed (no legacy path).
      grant None,  admitted False    -> no authorization at all.
    """
    import hypogeum.authorization_authority as _authority

    admitted = False
    if episode_id:
        try:
            from hypogeum.runtime_admission import get_admission_binding  # noqa: PLC0415
            admitted = get_admission_binding(channel_id, episode_id) is not None
        except Exception:  # noqa: BLE001 -- admission table absent => not admitted
            admitted = False
    if admitted:
        return _authority.resolve_active_operating_grant(channel_id, at=at), True
    # A completed episode under an open, persisted repair window
    # (hypogeum.episode_repair, bounded per repair and per episode) routes
    # through the channel's operating grant exactly like an admitted one --
    # never a caller flag, and no paid legacy fallback.
    if episode_id:
        try:
            from hypogeum.episode_repair import repair_window_open  # noqa: PLC0415
            if repair_window_open(channel_id, episode_id):
                return _authority.resolve_active_operating_grant(channel_id, at=at), True
        except Exception:  # noqa: BLE001 -- unknown => no repair routing
            pass
    return _authority.resolve_active_grant(channel_id, episode_id or "unknown_episode", at=at), False


def _scene_failure_is_systemic(reason: Optional[str]) -> bool:
    """True when a scene-generation failure will recur identically on every
    retry (billing / account credit, grant-wide or binding spend capacity
    exhausted, no resolvable grant) — so submit_scene()'s per-scene retry
    loop must stop rather than burn its remaining attempts. A prompt/content
    or transient provider failure returns False (worth another attempt,
    especially with the simpler fallback prompt)."""
    t = (reason or "").lower()
    if "insufficient" in t and any(m in t for m in ("credit", "balance", "top up", "top-up", "top_up")):
        return True
    return any(m in t for m in (
        "grant-wide spend capacity exhausted",
        "binding spend capacity exhausted",
        "grant capacity exhausted",
        "no_active_authorization_grant",
        "admitted_autonomous_execution_without_active_operating_grant",
        "no_authoritative_channel_identity",
        "quota exhausted", "quotaexhausted",
    ))


def _submit_scene_via_gate(
    *, channel_id: str, grant, prompt: str, tier: int, reference_image_url: Optional[str],
    scene_id: str, short_id: Optional[str], aspect_ratio: str, requested_duration: float,
    episode_id: str, output_dir: Path, config: Config, log, is_fallback: bool,
    repair_fallback_ok: bool = False, recovery_generation: int = 0,
) -> "tuple[Optional[dict], Optional[str]]":
    """
    The grant-derived gate path (Correction 3/5.5): compile an
    immutable envelope from live, current sources -> resolve/issue a
    child authorization against the caller-resolved active `grant` ->
    submit through hypogeum.submission_gate.submit(), which internally
    resolves and invokes hypogeum.providers.muapi_video_adapter.
    MuapiVideoAdapter. Never reached unless a real, verified grant
    already resolves for (channel_id, episode_id) -- see submit_scene()
    for the branch that decides whether to call this function at all.

    Returns the exact same (manifest_entry_or_None, failure_reason)
    shape _attempt_generation() returns, so submit_scene()'s two-attempt
    orchestration and every caller's result handling are unaffected by
    which path actually ran.

    repair_fallback_ok (operator-approved 2026-09-04, Option B): clip
    repair (hypogeum.agents.clip_repair_agent) has no access to the
    original entity reference images at repair time, so a scene the
    Production Plan declared as I2V (a "locked entity" scene) can never
    be resubmitted with a matching model from that call site -- it was
    hard-rejected as model_mismatch every time, retrying forever without
    ever succeeding (observed 2026-09-04, ep_90382de2). When True AND
    reference_image_url is None AND the resolved model is the channel's
    OWN I2V model would have been (i.e. the plan really did declare the
    I2V path, not some other legitimate model drift) AND the resolved
    T2V price is <= the plan's declared price, a model/price mismatch is
    downgraded to a logged fallback instead of a denial. This can only
    ever substitute a CHEAPER model than what Finance authorized for
    this asset -- it can never spend more than the plan approved -- and
    only clip_repair_agent's two repair call sites pass this flag at
    all; every other caller (VPD's own Phase 2 executor included) is
    completely unaffected.
    """
    import hashlib

    import hypogeum.adapter_registry as adapter_registry
    import hypogeum.authorization_authority as authority
    import hypogeum.authorization_store as authorization_store
    import hypogeum.budget_scopes as bs
    import hypogeum.channel_config as channel_config
    import hypogeum.db as db
    import hypogeum.submission_gate as submission_gate
    import hypogeum.weekly_budget as weekly_budget
    import hypogeum.workflow_executor as workflow_executor
    from hypogeum.deterministic_allocation import check_output_collision
    from hypogeum.providers.muapi_video_adapter import (
        SCHEMA_VERSION as _VIDEO_SCHEMA_VERSION, MuapiVideoAdapter, encode_video_payload,
    )
    from hypogeum.provider_receipt import WriteAheadReceiptStore

    model = _resolve_scene_model(tier, reference_image_url, config, channel_id)
    at = datetime.now(timezone.utc).isoformat()

    package_identity = channel_config.compute_channel_package_identity(channel_id)
    if not package_identity.get("available"):
        return None, "channel_package_identity_unavailable"
    workflow_binding = workflow_executor.resolve_workflow_binding(
        channel_id, _HYPOGEUM_WORKFLOW_ID, _HYPOGEUM_WORKFLOW_VERSION,
    )

    dest_path = scene_file_path(output_dir, scene_id, short_id)
    expected_output_identity = _compute_asset_id(episode_id, scene_id, short_id)

    # ------------------------------------------------------------------
    # Plan-conformance check (2026-08-24 correction) -- the common paid
    # network boundary rejects an undeclared asset, a changed prompt, or a
    # changed model BEFORE any envelope is compiled or any network call is
    # made. Not a second, parallel authorization registry: this is a
    # lookup against the SAME persisted hypogeum.production_plan.
    # ProductionPlan the Finance gate already authorized, at the ONE real
    # submission boundary every video-generation caller (VPD's own Phase 2
    # executor, clip_repair_agent's missing-clip and coverage-fill repair,
    # any future caller) already passes through once channel_id is
    # supplied. This is what makes Assembly's own repair calls incapable
    # of inventing an asset the plan never declared, without moving those
    # call sites out of assembly_agent.py.
    # ------------------------------------------------------------------
    from hypogeum.production_plan import load_production_plan, prompt_fingerprint  # noqa: PLC0415

    production_plan = load_production_plan(episode_id, output_dir)
    if production_plan is None:
        return None, (
            f"no_production_plan: no compiled Production Plan found for "
            f"episode {episode_id!r} -- refusing to submit an asset with no "
            f"authorized plan to check it against"
        )
    declared_asset = production_plan.asset_by_id(expected_output_identity)
    if declared_asset is None:
        return None, (
            f"undeclared_asset: {expected_output_identity!r} was never declared "
            f"in the authorized Production Plan (content_hash="
            f"{production_plan.content_hash[:12]})"
        )
    real_prompt_fingerprint = prompt_fingerprint(prompt)
    if real_prompt_fingerprint != declared_asset.prompt_fingerprint:
        # The freshly re-assembled prompt no longer matches the one the plan
        # was compiled (and Finance-authorized) against — invariably because
        # a DNA / video_prompt_writer change since then produces different
        # text for the same scene. For a fresh episode both are computed
        # with today's code and this never fires. For clip_repair rescuing
        # an OLD plan's missing scene (repair_fallback_ok=True — the same
        # operator-approved 2026-09-04 flag the model-mismatch downgrade
        # just below uses), fall back to the plan's OWN declared prompt: it
        # is the authorized text, so this can only ever use LESS-new content
        # than the caller assembled, never invent an asset the plan didn't
        # declare. Every other caller still fails closed.
        if not repair_fallback_ok:
            return None, (
                f"prompt_mismatch: real prompt fingerprint for {expected_output_identity!r} "
                f"does not match the plan's declared fingerprint for this asset identity"
            )
        log.warn(
            "clip_repair_prompt_drift_using_plan_declared",
            scene_id=scene_id, asset_id=expected_output_identity,
            reason="assembled prompt drifted from the plan's Finance-authorized text "
                   "(DNA/prompt-writer change since compile); using the plan's own",
        )
        prompt = declared_asset.prompt
    _repair_i2v_fallback = False
    if model != declared_asset.model:
        # The channel's OWN I2V model, recomputed the same way the plan
        # itself would have -- confirms this mismatch really is "plan
        # declared I2V, repair has no reference image", not some other,
        # unrelated model drift that must still fail closed.
        _channel_i2v_model = _resolve_scene_model(tier, "repair-fallback-probe", config, channel_id)
        _repair_i2v_fallback = (
            repair_fallback_ok
            and reference_image_url is None
            and declared_asset.model == _channel_i2v_model
        )
        if not _repair_i2v_fallback:
            return None, (
                f"model_mismatch: {model!r} does not match the plan's declared "
                f"model {declared_asset.model!r} for {expected_output_identity!r}"
            )
        log.warn(
            "clip_repair_i2v_to_t2v_fallback",
            scene_id=scene_id,
            declared_model=declared_asset.model,
            fallback_model=model,
            reason="no entity reference image available at repair time (operator-approved 2026-09-04, Option B)",
        )

    # ------------------------------------------------------------------
    # Phase 2 plan-bound financial authorization -- three-way agreement
    # between (1) the grant's registered production_plan_content_hash,
    # (2) the freshly loaded, integrity-verified persisted Production
    # Plan, and (3) the post-VPD finance record's bound hash. Every
    # disagreement fails closed HERE, before any envelope is compiled,
    # any authorization is minted, and any paid provider call is made.
    # ------------------------------------------------------------------
    # A Channel Operating Grant (ADR-016 grant_kind='channel_operating') is
    # signed ONCE per channel, before any episode's Production Plan exists, so
    # it carries no production_plan_content_hash by design. Its plan binding is
    # enforced differently and just as strictly: the plan-bound weekly
    # reservation established at admission + issue_child_authorization()'s
    # envelope.dependency_checksums.plan == verified-plan-hash check below (see
    # authorization_authority.issue_child_authorization, grant_kind branch).
    # The three-way grant-hash agreement below applies only to per-episode
    # 'run' grants, which ARE signed against a specific compiled plan.
    _is_operating_grant = getattr(grant, "grant_kind", "run") == "channel_operating"
    grant_plan_hash = getattr(grant, "production_plan_content_hash", None)
    if not _is_operating_grant:
        if not grant_plan_hash:
            return None, (
                f"grant_plan_unbound: grant {grant.grant_id}@{grant.grant_version} "
                f"carries no production_plan_content_hash -- grants registered before "
                f"plan binding fail closed; register a new RunAuthorizationGrant bound "
                f"to this episode's verified persisted Production Plan"
            )
        if grant_plan_hash != production_plan.content_hash:
            return None, (
                f"grant_plan_mismatch: grant {grant.grant_id}@{grant.grant_version} is bound "
                f"to content_hash={grant_plan_hash[:12]} but the verified persisted plan has "
                f"content_hash={production_plan.content_hash[:12]} -- changing or replacing "
                f"the plan invalidates authorization; re-register the grant"
            )
    finance_plan_hash, finance_denial = _load_post_vpd_finance_plan_hash(episode_id, output_dir)
    if finance_denial is not None:
        return None, finance_denial
    if finance_plan_hash != production_plan.content_hash:
        return None, (
            f"finance_record_mismatch: post-VPD finance authorization is bound to "
            f"content_hash={finance_plan_hash[:12]} but the verified persisted plan has "
            f"content_hash={production_plan.content_hash[:12]} -- the plan changed after "
            f"Finance authorized it"
        )

    # plan_checksum ties this envelope's dependency_checksums back to the
    # REAL compiled, Finance-authorized Production Plan -- never an ad-hoc
    # per-call hash independent of anything Finance actually reviewed.
    plan_checksum = production_plan.content_hash

    # ------------------------------------------------------------------
    # Phase 3: channel attribution (2026-08-25) -- the verified persisted
    # Production Plan declares the channel it was compiled for. A paid
    # submission whose channel context disagrees with the plan's own
    # attribution fails closed here, before any envelope, authorization,
    # reservation, or network activity. Channel identity is never inferred
    # from episode metadata: both sides of this comparison are explicit,
    # authoritative values.
    # ------------------------------------------------------------------
    if production_plan.channel_id != channel_id:
        return None, (
            f"plan_channel_mismatch: verified persisted Production Plan "
            f"(content_hash={production_plan.content_hash[:12]}) declares "
            f"channel_id={production_plan.channel_id!r} but this submission "
            f"context carries channel_id={channel_id!r} -- refusing paid "
            f"submission without exact channel attribution"
        )

    structured_spec_checksum = hashlib.sha256(_canonical_json_bytes({
        "aspect_ratio": aspect_ratio, "requested_duration": requested_duration,
        "reference_image_url": reference_image_url,
    })).hexdigest()

    operation_payload_bytes = encode_video_payload(
        prompt=prompt, model=model, aspect_ratio=aspect_ratio, duration_seconds=requested_duration,
        reference_image_url=reference_image_url, scene_id=scene_id, short_id=short_id,
        episode_id=episode_id, output_dir=str(output_dir),
    )
    operation_payload_content = operation_payload_bytes.decode("utf-8")
    operation_payload_sha256 = hashlib.sha256(operation_payload_bytes).hexdigest()

    max_cost = Decimal(str(_resolve_gate_unit_price(model, channel_id, episode_id)))

    # ------------------------------------------------------------------
    # Phase 3: plan-bound price cross-check (2026-08-25) -- the envelope's
    # per-call cost must be a number the VERIFIED PLAN itself authorizes
    # for this asset. The tier model registry and the compiled plan are
    # independent sources; any drift between them (a re-priced model, an
    # unknown model falling back to the default ceiling while the plan
    # carries a real price) is a fail-closed denial rather than a silently
    # different per-call authorization.
    # ------------------------------------------------------------------
    plan_unit_price = getattr(declared_asset, "unit_price_usd", None)
    if plan_unit_price is None:
        return None, (
            f"plan_price_missing: declared asset {expected_output_identity!r} in the "
            f"verified persisted plan carries no unit_price_usd -- refusing to "
            f"submit against an unpriced asset"
        )
    plan_unit_price_dec = Decimal(str(plan_unit_price))
    if plan_unit_price_dec <= 0:
        return None, (
            f"plan_price_invalid: declared asset {expected_output_identity!r} in the "
            f"verified persisted plan prices at {plan_unit_price_dec} USD -- a "
            f"non-positive price can never be authorized spend"
        )
    if plan_unit_price_dec != max_cost:
        if not (_repair_i2v_fallback and max_cost <= plan_unit_price_dec):
            return None, (
                f"plan_price_mismatch: verified persisted plan prices asset "
                f"{expected_output_identity!r} at {plan_unit_price_dec} USD but tier "
                f"model {model!r} resolves to {max_cost} USD -- refusing to submit "
                f"an amount the plan did not authorize"
            )
        log.warn(
            "clip_repair_i2v_to_t2v_fallback_price",
            scene_id=scene_id,
            declared_price_usd=str(plan_unit_price_dec),
            fallback_price_usd=str(max_cost),
        )

    # ------------------------------------------------------------------
    # Phase 3 correction (2026-08-25): the WEEKLY reservation amount is
    # the verified persisted plan's OWN maximum_bounded_cost_usd -- taken
    # from the integrity-verified plan object above, never from caller
    # input or the tier registry -- and it is reserved ONCE per exact
    # verified episode plan per channel-week. Every scene, Short,
    # primary/fallback attempt, retry, resume, and repair of that same
    # verified plan replays the one row; a different episode or a
    # different verified plan hash necessarily gets its own row. Phase 2
    # per-asset/model/prompt conformance above still independently
    # controls each individual provider submission.
    # ------------------------------------------------------------------
    raw_plan_max = getattr(production_plan, "maximum_bounded_cost_usd", None)
    if raw_plan_max is None:
        return None, (
            f"plan_maximum_missing: verified persisted Production Plan "
            f"(content_hash={production_plan.content_hash[:12]}) carries no "
            f"maximum_bounded_cost_usd -- refusing paid video without an "
            f"authoritative weekly reservation amount"
        )
    try:
        plan_max = Decimal(str(raw_plan_max))
    except Exception:  # noqa: BLE001 -- malformed pricing is itself fail-closed
        return None, (
            f"plan_maximum_malformed: verified persisted Production Plan "
            f"maximum_bounded_cost_usd {raw_plan_max!r} is not a valid decimal amount"
        )
    if not plan_max.is_finite() or plan_max <= 0:
        return None, (
            f"plan_maximum_invalid: verified persisted Production Plan "
            f"maximum_bounded_cost_usd is {plan_max}; zero/negative/non-finite "
            f"can never authorize spend"
        )

    # recovery_generation > 0 => a FRESH idempotency/authorization identity
    # (":recovery{n}"), so a scene whose ":primary"/":fallback" attempt
    # stranded (e.g. a poll timeout that left the authorization at
    # 'submitting', unrecoverable by reconcile) can still be regenerated
    # instead of the whole episode being lost. It is a genuinely new paid
    # attempt: its own child authorization, envelope, call/episode budget
    # rows and grant-ledger reservation. The plan-scoped WEEKLY key below is
    # unchanged (same plan => replays the one weekly reservation).
    if recovery_generation > 0:
        _key_suffix = f"recovery{recovery_generation}"
    else:
        _key_suffix = "fallback" if is_fallback else "primary"
    authorization_id = f"{episode_id}:{scene_id}:{_key_suffix}"
    idempotency_key = authorization_id

    # The WEEKLY identity is plan-scoped, never scene-scoped: identical for
    # every submission of this verified plan in this channel/episode, so
    # all of them replay the ONE reservation (db.reserve_budget's UNIQUE
    # (scope_kind, scope_ref, idempotency_key) replay), while any different
    # episode or plan hash produces a distinct key and row.
    from hypogeum.plan_reservation import plan_weekly_idempotency_key  # noqa: PLC0415

    weekly_plan_key = plan_weekly_idempotency_key(
        channel_id, episode_id, production_plan.content_hash
    )

    # ------------------------------------------------------------------
    # Phase 3: dry-run zero-network short-circuit (2026-08-25).
    # MUAPI_DRY_RUN makes no real provider call by construction, so it must
    # consume zero CHANNEL_WEEKLY budget -- and it must not enter the gate
    # path at all, because the pre-Phase-3 behavior (reserve CALL/EPISODE,
    # mint child authorization, then poll a fake dry-run job id until
    # timeout -> unknown_acceptance) would leave reservations held and a
    # ledger reservation trail behind a submission that could never have
    # been charged. This mirrors the legacy path exactly: same placeholder
    # clip, same generation_status="generated" contract downstream
    # Assembly/QA validate (see _generate_dry_run_placeholder_clip()), but
    # reached BEFORE any envelope compile, authorization, lease, receipt,
    # or budget reservation -- structurally incapable of touching either
    # the network or the weekly ledger.
    # ------------------------------------------------------------------
    if config.MUAPI_DRY_RUN:
        _generate_dry_run_placeholder_clip(dest_path, aspect_ratio, requested_duration, log)
        log.info(
            "muapi_gate_dry_run_complete",
            scene_id=scene_id,
            channel_id=channel_id,
            model=model,
        )
        return {
            "scene_id": scene_id,
            "asset_id": expected_output_identity,
            "generation_status": "generated",
            "provider": "muapi",
            "tier_used": tier,
            "model_used": model,
            "prompt_used": prompt,
            "fallback_used": is_fallback,
            "failure_reason": None,
            "request_id": f"{_DRY_RUN_REQUEST_ID_PREFIX}{scene_id}",
        }, None

    conn = db.get_connection()
    weekly: Optional[weekly_budget.WeeklyReservationOutcome] = None
    # runtime_lease bookkeeping — released in the finally once the gate call
    # returns. The lease only guards against a concurrent execution of the
    # SAME envelope; holding it for its full 30-min TTL after submit()
    # returns serves nothing and locks out this run's own in-assembly
    # clip-repair of a scene whose first generation failed (observed twice
    # 2026-09-03: Herculaneum s09, Egyptian Tomb s06 — "gate_lease_unavailable").
    _held_lease: Optional[tuple] = None  # (resource_id, owner_id, fencing_token)

    def _release_weekly_reservation(release_conn, *, at_ts: str) -> bool:
        """Compensating release for a pre-provider failure of the attempt
        that CREATED this attempt's plan-level weekly reservation. Gated on
        `created`: a replayed row belongs to whichever earlier attempt
        created it (it may be another in-flight attempt's live reservation,
        an unknown-acceptance hold, or committed spend) and is never
        released here. Releasing an already-released/committed row would be
        a harmless no-op anyway, but the ownership gate comes first."""
        if weekly is None:
            return False
        # Ownership is enforced inside the release helper itself: a
        # replayed outcome (created=False) is refused there.
        return weekly_budget.release_channel_weekly_reservation(
            release_conn, weekly, at=at_ts,
        )

    try:
        # --------------------------------------------------------------
        # Phase 3: atomic weekly check-and-reserve (2026-08-25) --
        # BEFORE child authorization and any grant-ledger side effect.
        # ONE plan-scoped reservation per verified episode plan per
        # channel-week (amount = verified maximum_bounded_cost_usd), under
        # one BEGIN IMMEDIATE with immutable window evidence; every
        # failure mode here is a clean, zero-network denial. Later
        # submissions of the same verified plan replay this row
        # (created=False) and can never double-reserve or release it.
        # --------------------------------------------------------------
        try:
            weekly = weekly_budget.ensure_channel_weekly_reservation(
                conn,
                channel_id=channel_id,
                amount=plan_max,
                currency="USD",
                idempotency_key=weekly_plan_key,
                now_utc=datetime.now(timezone.utc),
            )
        except weekly_budget.WeeklyBudgetError as exc:
            log.warn(
                "muapi_weekly_budget_denied", scene_id=scene_id,
                channel_id=channel_id, episode_id=episode_id, reason=str(exc),
            )
            return None, f"weekly_budget_denied: {exc}"
        except bs.BudgetError as exc:
            log.warn(
                "muapi_weekly_budget_exhausted", scene_id=scene_id,
                channel_id=channel_id, episode_id=episode_id, reason=str(exc),
            )
            return None, f"weekly_budget_exhausted: {exc}"

        try:
            envelope = GenerationExecutionEnvelope.create(
                envelope_schema_version=_VIDEO_ENVELOPE_SCHEMA_VERSION, envelope_version=1, channel_id=channel_id,
                channel_package_version=package_identity["version"], channel_package_checksum=package_identity["checksum"],
                execution_id=episode_id, workflow_version=workflow_binding["workflow_version"],
                plan_version="live", plan_checksum=plan_checksum, planned_shot_id=scene_id,
                structured_spec_checksum=structured_spec_checksum, compiler_identity=_VIDEO_COMPILER_IDENTITY,
                compiler_version="1", operation_payload_content=operation_payload_content,
                operation_payload_sha256=operation_payload_sha256, operation_payload_schema_version=_VIDEO_SCHEMA_VERSION,
                content_provenance={
                    "compiler": _VIDEO_COMPILER_IDENTITY, "compiler_version": "1",
                    "source_work_item_id": scene_id, "rendered_at": at,
                },
                provider=_VIDEO_PROVIDER, model=model, provider_operation=_VIDEO_PROVIDER_OPERATION,
                deterministic_output_path=str(dest_path), expected_output_identity=expected_output_identity,
                authorization_id=authorization_id, authorization_version=1, max_cost_per_call=max_cost,
                run_level_budget_ref=episode_id, episode_level_budget_ref=episode_id, currency="USD",
                retry_authorized=False, fallback_authorized=False, idempotency_key=idempotency_key,
                created_at=at, expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
                dependency_checksums={
                    "channel_package": package_identity["checksum"], "plan": plan_checksum,
                    "structured_spec": structured_spec_checksum, "compiler": hashlib.sha256(_VIDEO_COMPILER_IDENTITY.encode()).hexdigest(),
                },
                audit_metadata={"production_plan_content_hash": production_plan.content_hash},
                aspect_ratio=aspect_ratio, duration_seconds=Decimal(str(requested_duration)),
            )
            authorization = authority.issue_child_authorization(
                envelope, grant.grant_id, grant.grant_version, at=at,
                # Phase 2 plan-bound authorization: independently re-verified by
                # the authority (grant hash == this verified persisted plan hash
                # == envelope dependency_checksums.plan) BEFORE any ledger
                # reservation. Not a tautology: this value comes from the
                # integrity-verified load_production_plan() read above, and the
                # grant's own registered hash must equal it exactly.
                expected_production_plan_content_hash=production_plan.content_hash,
            )
        except Exception as exc:  # noqa: BLE001 -- any compile/authorize failure here is a clean non-network denial
            _release_weekly_reservation(conn, at_ts=datetime.now(timezone.utc).isoformat())
            log.warn("muapi_gate_authorization_failed", scene_id=scene_id, reason=str(exc))
            return None, f"gate_authorization_failed: {exc}"

        # Durable envelope storage (Part 1) -- required before submit()
        # will accept this envelope at all (Step 1.6:
        # _check_stored_envelope_durable_integrity()). Every real
        # submission through this path persists its own envelope in the
        # same call it compiles it, so there is never a staleness
        # window between compilation and durable storage.
        try:
            authorization_store.persist_execution_envelope(conn, envelope)
        except Exception:
            _release_weekly_reservation(conn, at_ts=datetime.now(timezone.utc).isoformat())
            raise

        adapter_registry.register_adapter(
            _VIDEO_PROVIDER, model, _VIDEO_PROVIDER_OPERATION, "1.0", MuapiVideoAdapter,
        )
        lease_owner_id = f"muapi_video_adapter:{scene_id}"
        from datetime import timedelta
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        lease = db.acquire_lease(conn, envelope.envelope_id, lease_owner_id, acquired_at=at, expires_at=lease_expires_at)
        if lease is None:
            _release_weekly_reservation(conn, at_ts=datetime.now(timezone.utc).isoformat())
            return None, "gate_lease_unavailable"
        _held_lease = (envelope.envelope_id, lease_owner_id, lease["fencing_token"])

        receipt_store = WriteAheadReceiptStore(output_dir)
        live_sources = submission_gate.LiveSources(
            channel_package_checksum=package_identity["checksum"], plan_checksum=plan_checksum,
            structured_spec_checksum=structured_spec_checksum, compiler_version="1",
        )
        # Phase 3 correction: the WEEKLY reservation is deliberately NOT in
        # the gate's scope list. Its lifecycle is plan-scoped and owned by
        # THIS caller (created-vs-replayed is known only here): the gate's
        # per-scene CALL/EPISODE scopes keep their own commit/release/hold
        # accounting, while the weekly row is committed on first provider
        # acceptance for the plan, held through unknown/rejected outcomes,
        # and released only by its creating attempt on a pre-provider
        # failure (see the compensating-release sites above/below).
        scopes = [
            # episode-scoped: a bare scene_id makes CALL a cumulative cross-episode
            # budget (db.reserve_budget sums a scope_ref's non-released reservations
            # for all time), so scene "s01" of episode 2 is denied by episode 1's
            # committed reservation. See the openrouter gate for the same fix.
            bs.ScopeRequest(bs.BudgetScopeKind.CALL, f"{episode_id}:{scene_id}", max_cost, "USD"),
            bs.ScopeRequest(bs.BudgetScopeKind.EPISODE, episode_id, grant.max_total_spend_all_bindings or max_cost, "USD"),
        ]
        result = submission_gate.submit(
            envelope, live_sources=live_sources, authorization=authorization, lease_conn=conn,
            lease_owner_id=lease_owner_id, lease_fencing_token=lease["fencing_token"], receipt_store=receipt_store,
            execution_id=episode_id, attempt_id=idempotency_key, budget_scopes=scopes,
            required_provider=_VIDEO_PROVIDER, required_model=model, at=at,
            expected_duration_seconds=Decimal(str(requested_duration)), expected_aspect_ratio=aspect_ratio,
            expected_output_identity=expected_output_identity, expected_deterministic_output_path=str(dest_path),
            output_already_exists=check_output_collision(dest_path),
        )
        # hypogeum.submission_gate.submit() itself never persists
        # AuthorizationRecord state past AUTHORIZED (it is structurally
        # forbidden from calling authorization_store.
        # persist_authorization_record() -- see tests/test_
        # authorization_authority_trust_boundary.py's static proof: the
        # gate settles receipt/grant-ledger/budget evidence for a real
        # execution, never the record's own persisted lifecycle state).
        # This domain caller, which already has both the envelope's
        # authorization_id and the gate's real outcome in hand, is
        # responsible for recording it -- generic across every future
        # video caller, never episode/scene-specific. Only reached when
        # a real boundary was actually entered (adapter_invoked=True);
        # a pre-boundary denial (bad lease, budget, etc.) leaves the
        # record correctly untouched at AUTHORIZED.
        if result.adapter_invoked:
            adapter_outcome = result.adapter_result.outcome if result.adapter_result is not None else "unknown_acceptance"
            authorization_store.record_submission_outcome(
                conn, envelope.authorization_id, outcome=adapter_outcome, submitting_at=at,
                submitted_at=at if adapter_outcome == "accepted" else None,
                provider_job_id=(result.adapter_result.provider_job_id if result.adapter_result is not None else None),
            )
            # Phase 3 correction: first provider ACCEPTANCE for this verified
            # plan transitions the plan-level weekly reservation from
            # 'reserved' to 'committed'. The full plan maximum stays counted
            # either way (the weekly sum counts reserved and committed rows
            # at their reserved amount -- never the unverified lower actual
            # charge); committing is the lifecycle/audit transition.
            # Idempotent: sibling scenes' acceptances replay as
            # "already_committed_same". Unknown/rejected outcomes keep the
            # full reservation held (conservative policy).
            if adapter_outcome == "accepted":
                bs.commit_reservation(
                    conn,
                    weekly.reservation_id,
                    authorized_ceiling=plan_max,
                    charged_amount=plan_max,
                    at=at,
                )
            conn.commit()
    finally:
        # The gate call has returned; this envelope's execution is settled.
        # Release the concurrency lease now so a legitimate retry or this
        # run's own clip-repair isn't locked out for the lease TTL.
        if _held_lease is not None:
            try:
                db.release_lease(
                    conn, _held_lease[0], _held_lease[1], _held_lease[2],
                    released_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception:  # noqa: BLE001 - best-effort; TTL still bounds it
                pass
        conn.close()

    if not result.approved:
        # Phase 3 compensating release — OWNERSHIP-GATED: only the attempt
        # that CREATED this attempt's plan-level weekly reservation may
        # release it, and only for a failure BEFORE the provider boundary
        # (adapter_invoked=False). A replaying attempt (created=False)
        # returns without touching the row: it may be another in-flight
        # attempt's live reservation, an unknown-acceptance hold, or
        # committed spend. Once the provider boundary was entered
        # (adapter_invoked=True) the hold persists by conservative policy —
        # unknown acceptance keeps it held; a provider rejection also keeps
        # it held (only pre-provider failures release).
        if not result.adapter_invoked:
            release_conn = db.get_connection()
            try:
                _release_weekly_reservation(release_conn, at_ts=datetime.now(timezone.utc).isoformat())
            finally:
                release_conn.close()
        return None, f"gate_denied[{result.denied_at_check}]: {result.reason}"

    # Record actual video spend to the Finance Controller. The gate path
    # (production since ADR-016) never did this — only the legacy
    # _attempt_generation() path at record_cost()#scene_generation did — so
    # every MUAPI scene since ~2026-08 was billed for real but logged as $0,
    # leaving runs.total_cost_usd / the episode rollup understated by ~10x.
    # Priced identically to the compiled plan (_resolve_gate_unit_price).
    # Non-blocking: record_cost swallows every error.
    _finance.record_cost(
        provider=_VIDEO_PROVIDER,
        model=model,
        call_type="scene_generation",
        cost_usd=_resolve_gate_unit_price(model, channel_id, episode_id),
        asset_id=expected_output_identity,
        episode_id=episode_id,
    )

    return {
        "scene_id": scene_id, "asset_id": expected_output_identity, "generation_status": "generated",
        "provider": _VIDEO_PROVIDER, "tier_used": tier, "model_used": model, "prompt_used": prompt,
        "fallback_used": is_fallback, "failure_reason": None,
        "request_id": result.adapter_result.provider_job_id if result.adapter_result else None,
    }, None


# ---------------------------------------------------------------------------
# Stuck-authorization reconciliation (2026-09-05)
# ---------------------------------------------------------------------------

def _recover_completed_scene_clip(
    *, result: dict, episode_id: str, scene_id: str, short_id: Optional[str],
    output_dir: Path, model: str, request_id: str, log,
) -> str:
    """Download the clip of a MUAPI job that completed after our poll timed out.
    Returns "recovered_clip" or "still_unresolved". Never raises."""
    try:
        dest = scene_file_path(output_dir, scene_id, short_id)
        if dest.exists():
            return "recovered_clip"      # already recovered: never download or charge twice
        outputs = result.get("outputs") or []
        url = (outputs[0] if outputs and isinstance(outputs[0], str)
               else (outputs[0].get("url", "") if outputs else ""))
        if not url:
            log.warn("reconcile_recovery_no_output_url", episode_id=episode_id, scene_id=scene_id)
            return "still_unresolved"
        dest.parent.mkdir(parents=True, exist_ok=True)
        ok, _why = _download_file(url, dest, log)
        if not ok:
            ok, _why = _download_file(url, dest, log)
        if not ok:
            dest.unlink(missing_ok=True)
            return "still_unresolved"
        valid, _val = _validate_video_file(dest, 0.0)
        if not valid:
            dest.unlink(missing_ok=True)
            return "still_unresolved"
        try:   # the original call never got to record its cost (that happens after the download)
            _finance.record_cost(
                provider="muapi", model=model, call_type="scene_generation",
                cost_usd=MODEL_CREDIT_COSTS.get(model, _DEFAULT_UNKNOWN_MODEL_COST),
                tokens_or_credits=None,
                asset_id=_compute_asset_id(episode_id, scene_id, short_id),
            )
        except Exception as exc:  # noqa: BLE001 -- the clip is safe on disk; a cost-record hiccup must not undo that
            log.warn("reconcile_recovery_cost_record_failed", episode_id=episode_id, scene_id=scene_id, error=str(exc)[:200])
        log.warn(
            "reconcile_scene_authorization_recovered_clip",
            episode_id=episode_id, scene_id=scene_id, provider_job_id=request_id,
        )
        return "recovered_clip"
    except Exception as exc:  # noqa: BLE001
        log.warn("reconcile_recovery_failed", episode_id=episode_id, scene_id=scene_id, error=str(exc)[:200])
        return "still_unresolved"


def reconcile_stuck_scene_authorization(
    *,
    episode_id: str,
    scene_id: str,
    short_id: Optional[str],
    is_fallback: bool,
    output_dir: Path,
    config: Config,
    log,
) -> str:
    """
    Attempt to release a scene's authorization if it is stuck in
    UNKNOWN_ACCEPTANCE_REQUIRES_RECONCILIATION -- the state a poll
    timeout after a successful submission leaves behind (see
    hypogeum.providers.muapi_video_adapter.MuapiVideoAdapter's own "never
    silently release budget/grant capacity for a call whose provider-side
    outcome is genuinely unknown" comment). Without this, the SAME
    idempotency_key can never be resubmitted -- every future clip_repair
    attempt for this exact scene fails closed forever with
    "authorization state is 'submitting', not 'authorized'", observed
    live 2026-09-04/05 on two consecutive hypogeum episodes
    (ep_edaa9625, ep_14a830cc), both losing an otherwise-complete episode
    to one stuck scene.

    This is the reconciliation the architecture always intended
    ("an explicit reconciliation event resolves it" --
    hypogeum.submission_gate.submit()'s own comment) but that was never
    actually wired up anywhere -- hypogeum.provider_receipt already had
    the full state model and one safe writer
    (reconcile_unknown_acceptance_as_not_submitted), just no caller and
    no way to determine WHICH resolution applies. This function is that
    caller: it re-polls the SAME provider_job_id already durably
    recorded (a read-only status check, never a new paid submission) to
    find out.

    Deliberately narrow in scope -- only ever releases, never silently
    recovers a secretly-completed job:
      - No provider_job_id was ever durably recorded at all (the
        provider boundary was never reached) -> safe to release.
      - The provider now reports job_status_failed/cancelled for that
        exact job_id -> definitively no output was ever produced ->
        safe to release.
      - The job actually COMPLETED on this re-poll (2026-10-10) -> its clip
        file is downloaded to the scene's canonical path and its cost is
        recorded once; the authorization/budget reservation is left exactly
        as it is (never released, never committed here -- conservative), and
        the caller's resume-safety path then reuses the file. Idempotent: if
        the file is already there nothing is downloaded or charged again.
        Reported "recovered_clip".
      - Any other outcome (still pending, timeout again, an unexpected
        HTTP status) -> left untouched and reported "still_unresolved".

    Returns:
        "not_applicable"  — no attempt was ever recorded, or the
                             recorded state isn't the stuck one at all
                             (nothing for this function to do).
        "released"        — the stuck authorization was released;
                             a fresh submission for this scene can now
                             proceed normally.
        "recovered_clip"  — the job had completed at the provider; its clip is
                             now on disk (see above).
        "still_unresolved" — genuinely still ambiguous; left alone.

    Never raises -- a caller (clip_repair_agent) should treat any
    outcome other than "released" exactly as it already does today
    (scene stays missing), so this is purely additive.
    """
    from hypogeum.provider_receipt import (  # noqa: PLC0415
        AlreadyReconciledWithDifferentResolutionError,
        RecoveryState,
        WriteAheadReceiptStore,
        reconcile_unknown_acceptance_as_not_submitted,
    )

    idempotency_key = f"{episode_id}:{scene_id}:{'fallback' if is_fallback else 'primary'}"
    try:
        receipt_store = WriteAheadReceiptStore(output_dir)
        events = receipt_store.read_events(episode_id, idempotency_key)
        if not events:
            return "not_applicable"
        if receipt_store.classify_recovery_state(episode_id, idempotency_key) != (
            RecoveryState.UNKNOWN_ACCEPTANCE_REQUIRES_RECONCILIATION
        ):
            return "not_applicable"

        last = events[-1]
        ack_event = next(
            (
                e for e in reversed(events)
                if e.event_type.value in ("provider_acknowledged", "unknown_acceptance")
                and e.evidence.get("provider_job_id")
            ),
            None,
        )
        provider_job_id = ack_event.evidence.get("provider_job_id") if ack_event else None

        if provider_job_id is None:
            proof = (
                "no provider_job_id was ever durably recorded for this attempt -- "
                "the provider boundary was never reached"
            )
        else:
            result, poll_reason = _poll_until_complete(provider_job_id, config, scene_id, log)
            if result is not None:
                # The job finished AFTER our poll gave up: the clip exists and is
                # already paid for. Pull it down so the normal resume-safety path
                # (submit_scene -> "reused") picks it up -- losing it burned a whole
                # episode (2026-10-10: ep_014876fc, 4 clips done at MUAPI, QA rejected).
                return _recover_completed_scene_clip(
                    result=result, episode_id=episode_id, scene_id=scene_id, short_id=short_id,
                    output_dir=output_dir, model=last.model, request_id=provider_job_id, log=log,
                )
            if not str(poll_reason).startswith(("job_status_failed", "job_status_cancelled")):
                return "still_unresolved"
            proof = (
                f"MUAPI confirmed this job will never deliver a result on reconciliation "
                f"re-poll (request_id={provider_job_id}): {poll_reason}"
            )

        from types import SimpleNamespace  # noqa: PLC0415

        import hypogeum.db as db  # noqa: PLC0415

        envelope_stub = SimpleNamespace(
            envelope_id=last.envelope_id, envelope_version=last.envelope_version,
            authorization_id=last.authorization_id, authorization_version=last.authorization_version,
            provider=last.provider, model=last.model, provider_operation=last.provider_operation,
            operation_payload_sha256=last.provider_bound_content_sha256,
            max_cost_per_call=last.max_cost_per_call, currency=last.currency,
        )

        budget_conn = db.get_connection()
        try:
            reservation_rows = budget_conn.execute(
                "SELECT reservation_id FROM budget_reservations "
                "WHERE idempotency_key = ? AND status = 'reserved'",
                (idempotency_key,),
            ).fetchall()
            reservation_ids = [r[0] for r in reservation_rows]
            try:
                reconcile_unknown_acceptance_as_not_submitted(
                    receipt_store, budget_conn, execution_id=episode_id, envelope=envelope_stub,
                    idempotency_key=idempotency_key, reservation_ids=reservation_ids,
                    proof=proof, at=datetime.now(timezone.utc).isoformat(),
                )
            except AlreadyReconciledWithDifferentResolutionError:
                return "still_unresolved"
        finally:
            budget_conn.close()

        log.info(
            "reconcile_scene_authorization_released",
            episode_id=episode_id, scene_id=scene_id, idempotency_key=idempotency_key,
            reservation_count=len(reservation_ids),
        )
        return "released"
    except Exception as exc:  # noqa: BLE001 -- reconciliation is best-effort, never fatal
        log.warn(
            "reconcile_scene_authorization_error",
            episode_id=episode_id, scene_id=scene_id, error=str(exc),
        )
        return "still_unresolved"


def submit_scene(
    scene_spec: dict,
    config: Config,
    episode_id: str,
    output_dir: Path,
    *,
    episode_id_for_asset: Optional[str] = None,
    agent: Optional[str] = None,
    budget_guard: Optional[MUAPIBudgetGuard] = None,
    shadow_evidence: Optional["_shadow_observation.ShadowEvidence"] = None,
    observation_only: bool = False,
    channel_id: Optional[str] = None,
    allow_i2v_to_t2v_repair_fallback: bool = False,
) -> dict:
    """
    Generate a video clip for one scene and return the asset manifest entry.

    Implements the two-attempt structure from spec §8:
      Attempt 1: primary_prompt + primary_tier
      Attempt 2: fallback_prompt + fallback_tier (if Attempt 1 fails and fallbacks are enabled)

    M5.6 guardrails applied:
      - Resume safety: if a valid downloaded file already exists, returns immediately (no submission)
      - Cheap mode:    MUAPI_CHEAP_MODE=true forces T3 for all attempts; skips reference image routing
      - Paid fallback: MUAPI_ALLOW_PAID_FALLBACKS=false disables Attempt 2
      - Budget guard:  budget_guard.check_and_record_attempt() is called before each submission

    scene_spec keys expected:
      scene_id (str)
      visual_spec.aspect_ratio (str: "16:9" or "9:16")
      visual_spec.duration_seconds (float: desired — will be rounded to 5 or 8)
      production_spec.primary_prompt (str)
      production_spec.primary_tier (int: 1, 2, or 3)
      production_spec.fallback_prompt (str)
      production_spec.fallback_tier (int: 1, 2, or 3)
      production_spec.reference_image_url (str | None: if set, I2V model is used)
      short_id (str | None: None = main episode, "sh_01" etc. = Short)

    Returns a dict with:
      scene_id, asset_id, generation_status ("generated" | "generation_failed" | "reused"),
      provider, tier_used, model_used, prompt_used, fallback_used, failure_reason

      "dry_run" is no longer a distinct status value (changed 2026-07-18):
      MUAPI_DRY_RUN now writes a real placeholder clip and reports
      "generated" like a real success, so callers never need a special case
      — see _generate_dry_run_placeholder_clip()'s docstring. Some callers
      (clip_repair_agent.py, video_generation_agent.py, pipeline.py) still
      list "dry_run" in their accepted-status sets for backward
      compatibility with historical manifests; that costs nothing since
      "generated" is already in every one of those sets too.

    Raises:
      MUAPIAuthFailure    — HTTP 401 anywhere in the call chain.
      MUAPIQuotaExhausted — HTTP 402 anywhere in the call chain.
      MUAPIBudgetExhausted — episode/scene budget limit reached (M5.6).

    shadow_evidence (Commit 4, 2026-08-15): optional
    hypogeum.shadow_observation.ShadowEvidence bundle. Every attempt this
    call makes is *automatically* evaluated by the non-mutating shared
    submission gate (hypogeum.shadow_observation.evaluate_shadow())
    immediately before the real HTTP submission -- this happens whether
    or not shadow_evidence is supplied at all, matching the Core
    Shadow-Mode Rule ("every MUAPI provider-bound operation must
    automatically produce a gate observation"). Omitting shadow_evidence
    entirely (the default, and every existing caller's current calling
    convention, unchanged) simply means every check evaluates with no
    envelope and no other evidence, producing one UNVERIFIABLE decision
    per attempt with blocker "missing_generation_execution_envelope" --
    this is expected and correct for a legacy call, not an error. The
    decision is durably recorded via a hypogeum.shadow_observation.
    ShadowObservationStore rooted at this call's own output_dir (or
    shadow_evidence.shadow_store, if the caller supplied one). This
    NEVER blocks, delays, retries, or otherwise changes this call's real
    behavior in any way -- shadow mode is observation only.

    observation_only (added after Commit 4, 2026-08-15): when True,
    every attempt this call makes stops immediately after the shadow
    decision above is persisted -- _submit_http() (the real network
    boundary) is never reached, no budget_guard state is mutated, no
    media file is written, and the returned dict's generation_status is
    "observation_only", never "generated"/"reused"/"generation_failed".
    This is the ONLY way to collect a genuine zero-provider-call shadow
    observation from a real production call path; shadow_evidence=...
    alone (the default False here) still reaches the real network call
    exactly as it always has. Defaults to False so every existing
    caller's behavior is byte-for-byte unchanged.

    channel_id (added for the Authorization Authority gate path,
    plan §5.5/§6 Commit F; enforcement tightened by the Production
    Enforcement Cutover, video domain only): optional. Supplying it AT
    ALL commits this call to the gate, unconditionally -- there is no
    legacy fallback from that point on. When a real, verified
    hypogeum.authorization_authority.RunAuthorizationGrant resolves for
    (channel_id, this call's episode_id): compile an immutable envelope
    from live sources -> issue a child authorization against the grant
    -> hypogeum.submission_gate.submit() (which resolves and invokes
    hypogeum.providers.muapi_video_adapter.MuapiVideoAdapter
    internally). When no grant resolves: returns generation_failed
    immediately with failure_reason "no_active_authorization_grant" --
    never the legacy Attempt 1/2 structure below. Every existing caller
    that omits channel_id entirely is completely unaffected (this is
    the exact, deliberate scope boundary that keeps every other
    provider domain, and every video caller not yet migrated to pass
    channel_id, unchanged and dormant). The gate path currently
    attempts only the primary prompt/tier -- fallback-prompt retry
    through the gate is not yet wired; a gate-path failure returns
    generation_failed directly rather than falling through to the
    legacy two-attempt structure, since a grant-derived attempt that
    fails at the provider boundary must never silently reattempt
    outside the Authority's own accounting.

    allow_i2v_to_t2v_repair_fallback (operator-approved 2026-09-04,
    Option B): forwarded to _submit_scene_via_gate()'s
    repair_fallback_ok -- see that function's own docstring. Defaults
    to False, so every existing caller is unaffected; only
    hypogeum.agents.clip_repair_agent's two repair call sites pass
    True. Has no effect on the legacy (channel_id=None) path.
    """
    # Multi-tenant Phase 2: spend with the keys of the workspace that owns
    # channel_id (hypogeum.workspace_keys); no channel = caller's config.
    if channel_id:
        from hypogeum.workspace_keys import config_for_channel
        config = config_for_channel(config, channel_id)
    scene_id = scene_spec["scene_id"]
    short_id = scene_spec.get("short_id")
    visual_spec = scene_spec.get("visual_spec", {})
    prod_spec = scene_spec.get("production_spec", {})

    aspect_ratio = visual_spec.get("aspect_ratio", "16:9")
    requested_duration = float(visual_spec.get("duration_seconds", 8.0))
    # Structured 9-section assembly (Scene/Camera/Subject/Motion/Lighting/
    # Visual Priorities/Video DNA/Brand DNA/Forbidden) — see
    # hypogeum/video_prompt_writer.py. Never concatenate raw strings here;
    # any cinematic-quality change belongs in hypogeum/video_dna.py or
    # hypogeum/brand/visual_identity.py, not in this function.
    primary_prompt = video_prompt_writer.assemble_video_scene_prompt(
        prod_spec["primary_prompt"], channel_id=channel_id)
    fallback_prompt = video_prompt_writer.assemble_video_scene_prompt(
        prod_spec["fallback_prompt"], channel_id=channel_id)

    eid = episode_id_for_asset or episode_id
    log = get_logger(episode_id=episode_id, agent=agent or "muapi")
    visual_identity.log_palette_injection(log, mode="video_scene", injected=True)

    # M5.6 — Resume safety: reuse valid existing file without any paid submission
    dest_path = scene_file_path(output_dir, scene_id, short_id)
    if dest_path.exists() and dest_path.stat().st_size >= _MIN_VIDEO_BYTES:
        log.info(
            "muapi_scene_reused",
            scene_id=scene_id,
            file_path=str(dest_path),
            file_size_bytes=dest_path.stat().st_size,
        )
        return {
            "scene_id": scene_id,
            "asset_id": _compute_asset_id(eid, scene_id, short_id),
            "generation_status": "reused",
            "provider": "muapi",
            "tier_used": None,
            "model_used": "cached",
            "prompt_used": None,
            "fallback_used": False,
            "failure_reason": None,
        }

    # M5.6 — Cheap mode: force T3, skip reference image conditioning
    cheap_mode = config.MUAPI_CHEAP_MODE
    runway_only = config.MUAPI_RUNWAY_ONLY_MODE
    if cheap_mode:
        primary_tier = 3
        fallback_tier = 3
        reference_image_url = None
        log.info("muapi_cheap_mode_active", scene_id=scene_id, forced_tier=3)
    elif runway_only:
        # Runway-only mode: force T2 for all video scenes; keep reference_image_url
        # so I2V conditioning still applies (override MUAPI_I2V_MODEL separately
        # to runway-image-to-video if I2V cost must also be capped at $0.15).
        primary_tier = 2
        fallback_tier = 2
        reference_image_url = prod_spec.get("reference_image_url")
        log.info("muapi_runway_only_mode_active", scene_id=scene_id, forced_tier=2)
    else:
        primary_tier = int(prod_spec.get("primary_tier", config.MUAPI_DEFAULT_TIER))
        fallback_tier = int(prod_spec.get("fallback_tier", config.MUAPI_DEFAULT_TIER))
        reference_image_url = prod_spec.get("reference_image_url")

    # Grant-derived gate path (Correction 3/5.5; enforced, Production
    # Enforcement Cutover -- video domain only). Once a caller supplies
    # channel_id at all, it has identified a channel context and is
    # COMMITTED to the gate: there is no legacy fallback from this point
    # on, regardless of whether a grant actually resolves. This is the
    # one, minimal, Factory-Core-generic change the cutover required --
    # it applies to any (channel_id, episode_id), never anything
    # hypogeum-specific, and callers that omit channel_id entirely
    # (every caller not yet migrated to pass one) are completely
    # unaffected, exactly as before -- see this function's own
    # docstring and tests/test_muapi_gate_path.py for the full
    # before/after contract.
    if channel_id is not None:
        grant, _admitted = _resolve_authorization_grant(
            channel_id, eid, at=datetime.now(timezone.utc).isoformat()
        )
        if grant is None:
            log.warn(
                "muapi_gate_enforced_no_active_grant",
                scene_id=scene_id, channel_id=channel_id, admitted_under_operating_grant=_admitted,
            )
            return {
                "scene_id": scene_id, "asset_id": None, "generation_status": "generation_failed",
                "provider": "muapi", "tier_used": None, "model_used": None, "prompt_used": None,
                "fallback_used": False,
                "failure_reason": (
                    "admitted_autonomous_execution_without_active_operating_grant: "
                    f"channel_id={channel_id!r} is admitted but no Channel Operating Grant "
                    "resolves -- fail closed, no legacy path"
                    if _admitted else
                    f"no_active_authorization_grant: channel_id={channel_id!r} identifies an enforced "
                    "channel context with no legacy fallback available -- register a RunAuthorizationGrant "
                    "(or a Channel Operating Grant) before retrying"
                ),
            }
        # Bounded per-scene retry so ONE flaky scene doesn't cost the whole
        # episode (operator directive 2026-09-06: "regenerate the failed
        # scene, don't reject the episode; respect the paid work of every
        # stage"). Attempt 1 = primary prompt (:primary key). Attempt 2 =
        # the simpler fallback prompt (:fallback key). Attempt 3+ =
        # recovery_generation>0 => a FRESH :recovery{n} key, which is what
        # lets a scene whose earlier authorization stranded at 'submitting'
        # (reconcile can't clear it) still be regenerated. Total attempts
        # capped by MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE (>=1); each retry is a
        # real paid call, bounded overall by the grant ledger, the weekly
        # reservation and hypogeum.muapi_spend_breaker. A SYSTEMIC failure
        # (billing / grant capacity / no grant) stops the loop immediately —
        # it would recur identically on every attempt.
        _max_attempts = max(1, int(getattr(config, "MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE", 1) or 1))
        failure_reason = None
        for _attempt in range(_max_attempts):
            # 2026-10-11 (ep_48391bc5): a retry re-sends the PRIMARY prompt. The authorized
            # Production Plan declares only the primary prompt's fingerprint for each asset,
            # so the old ':fallback' attempt (a different prompt) was rejected 'prompt_mismatch'
            # every single time (24 of 24 in the logs) — and it overwrote the real failure
            # reason, so the stranded-authorization recovery below never fired. A scene whose
            # first attempt merely TIMED OUT was therefore lost for good even though the job
            # finished at the provider (already paid).
            _p, _fb, _rec = primary_prompt, False, 0
            manifest, failure_reason = _submit_scene_via_gate(
                channel_id=channel_id, grant=grant, prompt=_p, tier=primary_tier,
                reference_image_url=reference_image_url, scene_id=scene_id, short_id=short_id,
                aspect_ratio=aspect_ratio, requested_duration=requested_duration, episode_id=eid,
                output_dir=output_dir, config=config, log=log, is_fallback=_fb,
                repair_fallback_ok=allow_i2v_to_t2v_repair_fallback, recovery_generation=_rec,
            )
            if manifest is not None:
                if _attempt > 0:
                    log.info(
                        "muapi_gate_path_recovered_on_retry",
                        scene_id=scene_id, attempt=_attempt + 1,
                    )
                return manifest
            log.warn(
                "muapi_gate_path_attempt_failed",
                scene_id=scene_id, attempt=_attempt + 1, reason=failure_reason,
            )
            if _scene_failure_is_systemic(failure_reason):
                log.warn(
                    "muapi_gate_path_systemic_stop",
                    scene_id=scene_id, attempt=_attempt + 1, reason=failure_reason,
                )
                break
            if _attempt + 1 >= _max_attempts:
                break
            # Before ANY further paid attempt ask the provider what became of this one. A poll
            # that merely timed out (provider slow) usually means the job FINISHES later,
            # already paid for — buying the scene again would pay twice. reconcile re-polls the
            # recorded job id (read-only): clip recovered -> use it; job still running -> do
            # NOT re-buy (clip_repair reconciles again later); job confirmed dead -> the
            # identity is released and the retry below can use it; anything else (a definite
            # rejection, a plain denial) -> no automatic retry (a rejected identity is never
            # auto-retried).
            _outcome = reconcile_stuck_scene_authorization(
                episode_id=eid, scene_id=scene_id, short_id=short_id, is_fallback=False,
                output_dir=output_dir, config=config, log=log,
            )
            if _outcome == "recovered_clip" and dest_path.exists() and dest_path.stat().st_size >= _MIN_VIDEO_BYTES:
                log.info("muapi_gate_path_recovered_from_provider_job", scene_id=scene_id)
                return {
                    "scene_id": scene_id, "asset_id": _compute_asset_id(eid, scene_id, short_id),
                    "generation_status": "reused", "provider": "muapi", "tier_used": None,
                    "model_used": "cached", "prompt_used": None, "fallback_used": False,
                    "failure_reason": None,
                }
            if _outcome != "released":
                log.warn("muapi_gate_path_no_automatic_retry", scene_id=scene_id, reconcile=_outcome)
                break

        # Stranded-authorization recovery (2026-09-06). A ':primary'/':fallback'
        # attempt whose poll timed out post-submission leaves that identity's
        # AuthorizationRecord permanently at 'submitting' (an honest
        # "acceptance unknown"), and reconcile_stuck_scene_authorization()
        # deliberately will NOT clear it when the job turns out to have
        # completed. submission_gate check 03_authorization_active_and_unconsumed
        # then denies EVERY retry on that same identity identically
        # ("authorization state is 'submitting', not 'authorized'") — losing a
        # whole otherwise-complete episode to one scene (observed live:
        # ep_f1b11fed s09 2026-09-06, and the ep_e736775e / ep_4a982634 class
        # before it). Unlike a flaky-scene retry this is deterministic on those
        # identities but a FRESH ':recovery{n}' identity — its own clean
        # authorization/envelope/budget rows — fixes it deterministically, so
        # spend up to _MAX_STRANDED_AUTH_RECOVERIES extra paid attempts on it
        # even after the ordinary MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE budget is
        # spent. Still bounded by the grant ledger, the plan weekly reservation
        # and hypogeum.muapi_spend_breaker. A systemic failure never reaches
        # here (the loop above already broke on it).
        _STRANDED_AUTH_MARKERS = (
            "03_authorization_active_and_unconsumed",
            "authorization state is 'submitting'",
            # a prior attempt on this identity left non-terminal receipt
            # evidence, so the same idempotency_key can never be resubmitted
            # — a fresh ':recovery{n}' key has none and is eligible.
            "05_idempotency_key_unconsumed",
            "already has attempt evidence",
        )
        _MAX_STRANDED_AUTH_RECOVERIES = 1
        _stranded_recoveries = 0
        while (
            manifest is None
            and any(mk in str(failure_reason) for mk in _STRANDED_AUTH_MARKERS)
            and not _scene_failure_is_systemic(failure_reason)
            and _stranded_recoveries < _MAX_STRANDED_AUTH_RECOVERIES
        ):
            # Same rule as the loop above: ask the provider first. Only a confirmed-dead job
            # (reconcile -> "released") may be bought again; a recovered clip is simply used,
            # a still-running job is never re-bought, and a rejected identity ("not_applicable")
            # is never auto-retried (2026-10-11: this block used to re-send a DIFFERENT prompt
            # that the plan always refused; now it re-sends the authorized one, so it must be
            # this careful).
            _o = reconcile_stuck_scene_authorization(
                episode_id=eid, scene_id=scene_id, short_id=short_id, is_fallback=False,
                output_dir=output_dir, config=config, log=log,
            )
            if _o == "recovered_clip" and dest_path.exists() and dest_path.stat().st_size >= _MIN_VIDEO_BYTES:
                return {
                    "scene_id": scene_id, "asset_id": _compute_asset_id(eid, scene_id, short_id),
                    "generation_status": "reused", "provider": "muapi", "tier_used": None,
                    "model_used": "cached", "prompt_used": None, "fallback_used": False,
                    "failure_reason": None,
                }
            if _o != "released":
                break
            _stranded_recoveries += 1
            _recovery_generation = _max_attempts + _stranded_recoveries  # always a fresh :recovery{n}
            log.info(
                "muapi_gate_path_stranded_auth_recovery",
                scene_id=scene_id, recovery_generation=_recovery_generation,
                prior_reason=failure_reason,
            )
            manifest, failure_reason = _submit_scene_via_gate(
                channel_id=channel_id, grant=grant, prompt=primary_prompt, tier=primary_tier,
                reference_image_url=reference_image_url, scene_id=scene_id, short_id=short_id,
                aspect_ratio=aspect_ratio, requested_duration=requested_duration, episode_id=eid,
                output_dir=output_dir, config=config, log=log, is_fallback=False,
                repair_fallback_ok=allow_i2v_to_t2v_repair_fallback,
                recovery_generation=_recovery_generation,
            )
            if manifest is not None:
                log.info(
                    "muapi_gate_path_recovered_via_stranded_auth_recovery",
                    scene_id=scene_id, recovery_generation=_recovery_generation,
                )
                return manifest
            log.warn(
                "muapi_gate_path_attempt_failed",
                scene_id=scene_id, attempt=f"recovery{_recovery_generation}", reason=failure_reason,
            )

        return {
            "scene_id": scene_id, "asset_id": None, "generation_status": "generation_failed",
            "provider": "muapi", "tier_used": None, "model_used": None, "prompt_used": None,
            "fallback_used": False, "failure_reason": f"gate_path: {failure_reason}",
        }

    # Legacy two-attempt path (channel_id is None). Fail closed if this is
    # POST-plan paid work without an authoritative channel identity
    # (2026-08-27): once a Production Plan file exists for the episode, a
    # real MUAPI video call requires channel identity + the plan-level
    # weekly reservation (reached via the gate branch above when channel_id
    # is supplied). The ONLY exception is MUAPI_DRY_RUN -- the legacy path
    # then just writes a zero-network placeholder. Pre-plan / no-plan
    # callers (tests, tools, shadow observation) are unaffected.
    if not getattr(config, "MUAPI_DRY_RUN", False):
        from hypogeum.production_plan import PlanLifecycle, classify_plan_lifecycle
        if classify_plan_lifecycle(eid, output_dir) is not PlanLifecycle.PRE_PLAN:
            log.warn(
                "muapi_legacy_video_path_blocked_post_plan_no_channel",
                scene_id=scene_id, episode_id=eid,
            )
            return {
                "scene_id": scene_id, "asset_id": None,
                "generation_status": "generation_failed",
                "provider": "muapi", "tier_used": None, "model_used": None,
                "prompt_used": None, "fallback_used": False,
                "failure_reason": (
                    "no_authoritative_channel_identity_post_plan: a persisted "
                    "Production Plan exists for this episode but submit_scene() "
                    "was reached without channel_id -- refusing real paid video "
                    "outside plan-scoped weekly enforcement (supply channel_id, "
                    "or set MUAPI_DRY_RUN for offline runs)"
                ),
            }

    # Attempt 1 — primary prompt
    manifest, failure_reason_1 = _attempt_generation(
        prompt=primary_prompt,
        tier=primary_tier,
        reference_image_url=reference_image_url,
        scene_id=scene_id,
        short_id=short_id,
        aspect_ratio=aspect_ratio,
        requested_duration=requested_duration,
        episode_id=eid,
        output_dir=output_dir,
        config=config,
        log=log,
        is_fallback=False,
        budget_guard=budget_guard,
        shadow_evidence=shadow_evidence,
        observation_only=observation_only,
    )
    if manifest is not None:
        return manifest

    # M5.6 — Paid fallback control
    allow_fallbacks = config.MUAPI_ALLOW_PAID_FALLBACKS
    if cheap_mode:
        # Cheap mode already at cheapest tier — no fallback to submit
        allow_fallbacks = False
        log.warn(
            "muapi_fallback_skipped",
            scene_id=scene_id,
            reason="cheap_mode_no_fallback",
            failure_reason=failure_reason_1,
        )

    if not allow_fallbacks:
        log.warn(
            "muapi_fallback_disabled",
            scene_id=scene_id,
            failure_reason=failure_reason_1,
        )
        return {
            "scene_id": scene_id,
            "asset_id": None,
            "generation_status": "generation_failed",
            "provider": "muapi",
            "tier_used": None,
            "model_used": None,
            "prompt_used": None,
            "fallback_used": False,
            "failure_reason": f"attempt1={failure_reason_1}, fallback_disabled",
        }

    # Attempt 2 — fallback prompt
    log.warn(
        "muapi_fallback_triggered",
        scene_id=scene_id,
        failure_reason=failure_reason_1,
        fallback_tier=fallback_tier,
    )
    manifest, failure_reason_2 = _attempt_generation(
        prompt=fallback_prompt,
        tier=fallback_tier,
        reference_image_url=reference_image_url,
        scene_id=scene_id,
        short_id=short_id,
        aspect_ratio=aspect_ratio,
        requested_duration=requested_duration,
        episode_id=eid,
        output_dir=output_dir,
        config=config,
        log=log,
        is_fallback=True,
        budget_guard=budget_guard,
        shadow_evidence=shadow_evidence,
        observation_only=observation_only,
    )
    if manifest is not None:
        return manifest

    # Both attempts failed
    log.error(
        "muapi_scene_both_attempts_failed",
        scene_id=scene_id,
        failure_reason_attempt_1=failure_reason_1,
        failure_reason_attempt_2=failure_reason_2,
    )
    return {
        "scene_id": scene_id,
        "asset_id": None,
        "generation_status": "generation_failed",
        "provider": "muapi",
        "tier_used": None,
        "model_used": None,
        "prompt_used": None,
        "fallback_used": True,
        "failure_reason": f"attempt1={failure_reason_1}, attempt2={failure_reason_2}",
    }


# Valid video aspect ratios and their corresponding reference image aspect ratios.
# Reference images must match the video format so they can be used as I2V seeds
# without letterboxing or distortion.
_VIDEO_TO_REF_ASPECT: dict[str, str] = {
    "16:9": "16:9",
    "9:16": "9:16",
}

# Accepted legacy value — kept for dry-run / test backwards-compat only.
# Production callers must pass an explicit video_aspect_ratio.
_REF_ASPECT_FALLBACK = "1:1"

_IMAGE_PROVIDER_OPERATION = "image.reference"
_IMAGE_COMPILER_IDENTITY = "hypogeum.muapi.generate_reference_image"


def _generate_reference_image_via_gate(
    *, channel_id: str, grant, entity_id: str, prompt: str, aspect_ratio: str,
    episode_id: str, output_dir: Path, config: Config, log,
) -> "tuple[Optional[ReferenceImageResult], Optional[str]]":
    """The grant-derived gate path for reference-image generation --
    mirrors _submit_scene_via_gate()'s structure exactly (see that
    function's own docstring for the full rationale). Never reached
    unless a real, verified grant already resolves for (channel_id,
    episode_id)."""
    import hashlib

    import hypogeum.adapter_registry as adapter_registry
    import hypogeum.authorization_authority as authority
    import hypogeum.authorization_store as authorization_store
    import hypogeum.budget_scopes as bs
    import hypogeum.channel_config as channel_config
    import hypogeum.db as db
    import hypogeum.submission_gate as submission_gate
    import hypogeum.workflow_executor as workflow_executor
    from datetime import timedelta
    from hypogeum.providers.muapi_image_adapter import (
        SCHEMA_VERSION as _IMAGE_SCHEMA_VERSION, MuapiImageAdapter, encode_image_payload,
    )
    from hypogeum.provider_receipt import WriteAheadReceiptStore

    model = config.MUAPI_IMAGE_MODEL
    at = datetime.now(timezone.utc).isoformat()

    package_identity = channel_config.compute_channel_package_identity(channel_id)
    if not package_identity.get("available"):
        return None, "channel_package_identity_unavailable"
    workflow_binding = workflow_executor.resolve_workflow_binding(
        channel_id, _HYPOGEUM_WORKFLOW_ID, _HYPOGEUM_WORKFLOW_VERSION,
    )

    plan_checksum = hashlib.sha256(_canonical_json_bytes({"entity_id": entity_id, "prompt": prompt})).hexdigest()
    structured_spec_checksum = hashlib.sha256(_canonical_json_bytes({"aspect_ratio": aspect_ratio})).hexdigest()

    operation_payload_bytes = encode_image_payload(
        prompt=prompt, model=model, aspect_ratio=aspect_ratio, entity_id=entity_id,
        episode_id=episode_id, output_dir=str(output_dir),
    )
    operation_payload_content = operation_payload_bytes.decode("utf-8")
    operation_payload_sha256 = hashlib.sha256(operation_payload_bytes).hexdigest()

    expected_output_identity = f"episodes/{episode_id}/video/ref_{entity_id}"
    dest_path = output_dir / "video" / f"ref_{entity_id}.jpg"
    max_cost = Decimal(str(MODEL_CREDIT_COSTS.get(model, 0.015)))
    authorization_id = f"{episode_id}:ref_{entity_id}"

    # Explicit protected POST_PLAN binding: the exact canonical Production
    # Plan content_hash this paid invocation belongs to (empty when
    # pre-plan). hypogeum.capability_boundary re-verifies it equals the
    # freshly-loaded canonical plan before provider I/O.
    from hypogeum.production_plan import canonical_episode_dir as _canon_dir  # noqa: PLC0415
    from hypogeum.production_plan import load_production_plan as _load_canon_plan  # noqa: PLC0415
    _bound_plan = _load_canon_plan(episode_id, _canon_dir(episode_id))
    _plan_audit_metadata = (
        {"production_plan_content_hash": _bound_plan.content_hash} if _bound_plan is not None else {}
    )

    try:
        envelope = GenerationExecutionEnvelope.create(
            envelope_schema_version=_VIDEO_ENVELOPE_SCHEMA_VERSION, envelope_version=1, channel_id=channel_id,
            channel_package_version=package_identity["version"], channel_package_checksum=package_identity["checksum"],
            execution_id=episode_id, workflow_version=workflow_binding["workflow_version"],
            plan_version="live", plan_checksum=plan_checksum, planned_shot_id=f"ref_{entity_id}",
            structured_spec_checksum=structured_spec_checksum, compiler_identity=_IMAGE_COMPILER_IDENTITY,
            compiler_version="1", operation_payload_content=operation_payload_content,
            operation_payload_sha256=operation_payload_sha256, operation_payload_schema_version=_IMAGE_SCHEMA_VERSION,
            content_provenance={
                "compiler": _IMAGE_COMPILER_IDENTITY, "compiler_version": "1",
                "source_work_item_id": f"ref_{entity_id}", "rendered_at": at,
            },
            provider=_VIDEO_PROVIDER, model=model, provider_operation=_IMAGE_PROVIDER_OPERATION,
            deterministic_output_path=str(dest_path), expected_output_identity=expected_output_identity,
            authorization_id=authorization_id, authorization_version=1, max_cost_per_call=max_cost,
            run_level_budget_ref=episode_id, episode_level_budget_ref=episode_id, currency="USD",
            retry_authorized=False, fallback_authorized=False, idempotency_key=authorization_id,
            created_at=at, expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
            dependency_checksums={
                "channel_package": package_identity["checksum"], "plan": plan_checksum,
                "structured_spec": structured_spec_checksum, "compiler": hashlib.sha256(_IMAGE_COMPILER_IDENTITY.encode()).hexdigest(),
            },
            audit_metadata=_plan_audit_metadata,
            aspect_ratio=aspect_ratio,
        )
        authorization = authority.issue_child_authorization(envelope, grant.grant_id, grant.grant_version, at=at)
    except Exception as exc:  # noqa: BLE001 -- any compile/authorize failure here is a clean non-network denial
        log.warn("muapi_gate_authorization_failed", entity_id=entity_id, reason=str(exc))
        return None, f"gate_authorization_failed: {exc}"

    conn = db.get_connection()
    _held_lease: Optional[tuple] = None
    try:
        authorization_store.persist_execution_envelope(conn, envelope)

        adapter_registry.register_adapter(_VIDEO_PROVIDER, model, _IMAGE_PROVIDER_OPERATION, "1.0", MuapiImageAdapter)
        lease_owner_id = f"muapi_image_adapter:ref_{entity_id}"
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        lease = db.acquire_lease(conn, envelope.envelope_id, lease_owner_id, acquired_at=at, expires_at=lease_expires_at)
        if lease is None:
            return None, "gate_lease_unavailable"
        _held_lease = (envelope.envelope_id, lease_owner_id, lease["fencing_token"])

        receipt_store = WriteAheadReceiptStore(output_dir)
        live_sources = submission_gate.LiveSources(
            channel_package_checksum=package_identity["checksum"], plan_checksum=plan_checksum,
            structured_spec_checksum=structured_spec_checksum, compiler_version="1",
        )
        scopes = [
            bs.ScopeRequest(bs.BudgetScopeKind.CALL, f"{episode_id}:ref_{entity_id}", max_cost, "USD"),
            bs.ScopeRequest(bs.BudgetScopeKind.EPISODE, episode_id, grant.max_total_spend_all_bindings or max_cost, "USD"),
        ]
        result = submission_gate.submit(
            envelope, live_sources=live_sources, authorization=authorization, lease_conn=conn,
            lease_owner_id=lease_owner_id, lease_fencing_token=lease["fencing_token"], receipt_store=receipt_store,
            execution_id=episode_id, attempt_id=authorization_id, budget_scopes=scopes,
            required_provider=_VIDEO_PROVIDER, required_model=model, at=at,
            expected_aspect_ratio=aspect_ratio, expected_output_identity=expected_output_identity,
            expected_deterministic_output_path=str(dest_path), output_already_exists=dest_path.exists(),
        )
    finally:
        if _held_lease is not None:
            try:
                db.release_lease(
                    conn, _held_lease[0], _held_lease[1], _held_lease[2],
                    released_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception:  # noqa: BLE001
                pass
        conn.close()

    if not result.approved:
        return None, f"gate_denied[{result.denied_at_check}]: {result.reason}"

    hosted_url = result.adapter_result.evidence.get("hosted_url") if result.adapter_result else None
    if not hosted_url:
        return None, "gate_accepted_but_no_hosted_url_in_evidence"

    _finance.record_cost(
        provider=_VIDEO_PROVIDER,
        model=model,
        call_type="reference_image_generation",
        cost_usd=MODEL_CREDIT_COSTS.get(model, 0.015),
        asset_id=expected_output_identity,
        episode_id=episode_id,
    )

    return ReferenceImageResult(asset_id=expected_output_identity, hosted_url=hosted_url), None


def generate_reference_image(
    entity_id: str,
    visual_spec_text: str,
    config: Config,
    episode_id: str,
    output_dir: Path,
    *,
    video_aspect_ratio: str = "16:9",
    agent: Optional[str] = None,
    budget_guard: Optional[MUAPIBudgetGuard] = None,
    channel_id: Optional[str] = None,
) -> ReferenceImageResult:
    """
    Generate a reference image for a final-locked entity (Path A).

    channel_id (added for the Authorization Authority gate path,
    plan §5.5/§6 Commit F -- mirrors submit_scene()'s own channel_id
    parameter exactly): optional. When supplied AND a real, verified
    RunAuthorizationGrant already resolves for (channel_id, episode_id),
    the call routes through hypogeum.authorization_authority + hypogeum.
    submission_gate instead of the legacy path below. Every existing
    caller omits channel_id and is completely unaffected; even a caller
    that supplies it falls straight through to the unchanged legacy
    path whenever no real grant exists (true for every real channel
    until a human operator explicitly runs the Enforcement Cutover).

    Submits an image generation job, polls for completion, downloads the image
    locally, uploads it to MUAPI storage for a stable hosted URL, and returns
    both the local asset_id and the hosted_url.

    The hosted_url is passed by VPD in scene_spec.production_spec.reference_image_url
    for all subsequent scenes containing this entity.

    Args:
        entity_id:          Identifier for the entity (e.g., "pharaoh_amenhotep").
        visual_spec_text:   Text description of the entity for image generation.
        config:             Loaded Config instance.
        episode_id:         Episode identifier.
        output_dir:         Episode asset directory (.hypogeum-assets/episodes/{ep_id}/).
        video_aspect_ratio: Aspect ratio of the target video ("16:9" or "9:16").
                            Reference image is generated in the same aspect ratio so it
                            can be used as an I2V seed without distortion.
                            Default: "16:9" (main episode).
        agent:              Optional agent name for log correlation.
        budget_guard:       Optional M5.6 budget guard. Blocks if episode budget is exceeded.

    Returns:
        ReferenceImageResult with asset_id, hosted_url, and asset_class="reference_image".

    Raises:
        AssetClassError     — video_aspect_ratio not in supported set ("16:9", "9:16").
        MUAPIAuthFailure    — HTTP 401.
        MUAPIQuotaExhausted — HTTP 402.
        MUAPIBudgetExhausted — Episode budget limit reached (M5.6).
        MUAPIError          — Image generation failed after both attempts.
    """
    # Multi-tenant Phase 2: spend with the keys of the workspace that owns
    # channel_id (hypogeum.workspace_keys); no channel = caller's config.
    if channel_id:
        from hypogeum.workspace_keys import config_for_channel
        config = config_for_channel(config, channel_id)
    if video_aspect_ratio not in _VIDEO_TO_REF_ASPECT:
        raise AssetClassError(
            f"Unsupported video_aspect_ratio={video_aspect_ratio!r} for reference image. "
            f"Supported: {sorted(_VIDEO_TO_REF_ASPECT)}"
        )
    ref_aspect_ratio = _VIDEO_TO_REF_ASPECT[video_aspect_ratio]

    log = get_logger(episode_id=episode_id, agent=agent or "muapi")

    # Grant-derived gate path (Correction 3/5.5) -- see submit_scene()'s
    # own identical branch for the full rationale. Skipped entirely
    # unless channel_id is supplied AND a real, verified grant resolves.
    if channel_id is not None:
        grant, _admitted = _resolve_authorization_grant(
            channel_id, episode_id, at=datetime.now(timezone.utc).isoformat()
        )
        if _admitted and grant is None:
            raise MUAPIError(
                f"Reference image for entity {entity_id!r}: channel {channel_id!r} is admitted "
                "under a Channel Operating Grant but none resolves -- fail closed, no legacy path"
            )
        if grant is not None:
            gate_result, gate_failure_reason = _generate_reference_image_via_gate(
                channel_id=channel_id, grant=grant, entity_id=entity_id, prompt=visual_spec_text,
                aspect_ratio=ref_aspect_ratio, episode_id=episode_id, output_dir=output_dir,
                config=config, log=log,
            )
            if gate_result is not None:
                return gate_result
            raise MUAPIError(f"Reference image gate path failed for entity {entity_id!r}: {gate_failure_reason}")

    image_model = config.MUAPI_IMAGE_MODEL
    endpoint = f"{config.MUAPI_BASE_URL}/{image_model}"
    payload = {
        "prompt": visual_spec_text,
        "aspect_ratio": ref_aspect_ratio,
    }

    # M5.6 — Budget check before paid image submission (skipped in dry-run)
    scene_label = f"ref_{entity_id}"
    if budget_guard is not None and not config.MUAPI_DRY_RUN:
        budget_guard.check_and_record_attempt(scene_label, image_model)

    # Submit image generation (with HTTP retry on 429/5xx)
    request_id, failure_reason = _submit_http(
        endpoint=endpoint,
        payload=payload,
        config=config,
        scene_id=f"ref_{entity_id}",
        log=log,
    )
    if request_id is None:
        raise MUAPIError(
            f"Reference image generation failed for entity {entity_id!r}: {failure_reason}"
        )

    # Poll for completion
    result, failure_reason = _poll_until_complete(
        request_id=request_id,
        config=config,
        scene_id=f"ref_{entity_id}",
        log=log,
    )
    if result is None:
        raise MUAPIError(
            f"Reference image polling failed for entity {entity_id!r}: {failure_reason}"
        )

    # Extract image URL from result
    outputs = result.get("outputs", [])
    if not outputs:
        raise MUAPIError(
            f"Reference image result has no outputs[] for entity {entity_id!r}"
        )
    image_url = outputs[0] if isinstance(outputs[0], str) else outputs[0].get("url", "")
    if not image_url:
        raise MUAPIError(
            f"Reference image URL is empty for entity {entity_id!r}"
        )

    # Download image locally
    video_dir = output_dir / "video"
    video_dir.mkdir(parents=True, exist_ok=True)
    local_path = video_dir / f"ref_{entity_id}.jpg"

    success, dl_reason = _download_file(image_url, local_path, log)
    if not success:
        # Retry once
        success, dl_reason = _download_file(image_url, local_path, log)
        if not success:
            raise MUAPIError(
                f"Reference image download failed for entity {entity_id!r}: {dl_reason}"
            )

    if local_path.stat().st_size < _MIN_IMAGE_BYTES:
        raise MUAPIError(
            f"Reference image file is suspiciously small ({local_path.stat().st_size} bytes) "
            f"for entity {entity_id!r}"
        )

    # Upload to MUAPI storage for a stable hosted URL (required for I2V images_list)
    hosted_url = _upload_file(local_path, config, log)

    asset_id = f"episodes/{episode_id}/video/ref_{entity_id}"

    ref_model = config.MUAPI_IMAGE_MODEL
    log.info(
        "muapi_reference_image_generated",
        entity_id=entity_id,
        asset_id=asset_id,
        hosted_url=_mask_url(hosted_url),
        ref_aspect_ratio=ref_aspect_ratio,
    )
    _finance.record_cost(
        provider="muapi",
        model=ref_model,
        call_type="reference_image_generation",
        cost_usd=MODEL_CREDIT_COSTS.get(ref_model, 0.015),
        asset_id=asset_id,
    )

    return ReferenceImageResult(asset_id=asset_id, hosted_url=hosted_url)


# Target pixel dimensions for each supported aspect ratio.
_THUMBNAIL_TARGETS: dict[str, tuple[int, int]] = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
}

# Fonts tried in order for text overlay rendering (first found wins).
# Preference: Raleway-like clean light/regular sans-serif. Heavy Impact-style
# fonts were replaced after brand audit — the subtitle must be clean and
# integrated, not a caption slapped over the image.
_OVERLAY_FONT_CANDIDATES: list[str] = [
    "/Library/Fonts/Raleway-Regular.ttf",                                # ideal if installed
    "/Library/Fonts/Raleway-Light.ttf",
    "/System/Library/Fonts/Supplemental/Gill Sans.ttc",                  # clean macOS sans
    "/System/Library/Fonts/Helvetica Neue.ttc",                          # light/regular fallback
    "/System/Library/Fonts/Avenir.ttc",                                   # clean geometric sans
    "/System/Library/Fonts/Supplemental/Optima.ttc",                     # elegant fallback
    "/System/Library/Fonts/Supplemental/Futura.ttc",
    "/System/Library/Fonts/Supplemental/Trebuchet MS.ttf",               # weight-neutral fallback
    "/System/Library/Fonts/Supplemental/Tahoma Bold.ttf",               # last resort
]

# Bold font candidates for emphasis word in _apply_typography_plan.
# The emphasis word must have genuine Bold weight to dominate at 320px viewing size.
# Preference: Helvetica Neue Bold (very common macOS) → Arial Bold → Gill Sans Bold.
_OVERLAY_BOLD_FONT_CANDIDATES: list[str] = [
    "/Library/Fonts/Raleway-ExtraBold.ttf",
    "/Library/Fonts/Raleway-Bold.ttf",
    "/System/Library/Fonts/Helvetica Neue.ttc",                          # includes Bold variant
    "/System/Library/Fonts/Supplemental/Gill Sans Bold.ttf",
    "/System/Library/Fonts/Supplemental/Futura.ttc",
    "/System/Library/Fonts/Supplemental/Tahoma Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
]

# Fonts tried in order for the channel wordmark (Cinzel-like serif, all-caps).
# Copperplate is the closest macOS system match to Cinzel SemiBold (small caps, engraved look).
# Didot and Baskerville are elegant fallbacks; Times New Roman Bold is the last resort.
_WORDMARK_FONT_CANDIDATES: list[str] = [
    "/System/Library/Fonts/Supplemental/Copperplate.ttc",
    "/System/Library/Fonts/Supplemental/Didot.ttc",
    "/System/Library/Fonts/Supplemental/Baskerville.ttc",
    "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
]

def _hex_to_rgb(hex_str: str) -> tuple:
    hex_str = hex_str.lstrip("#")
    return tuple(int(hex_str[i:i + 2], 16) for i in (0, 2, 4))


# Brand palette values used in wordmark compositing — sourced from
# hypogeum.brand.visual_identity (single canonical palette). Do not
# hardcode brand hex values here again.
_wordmark_palette = visual_identity.get_palette()
_WORDMARK_COLOR_HYPOGEUM  = _hex_to_rgb(_wordmark_palette["warm_ivory"]["hex"])      # warm_gold — dominant line
_WORDMARK_COLOR_THE_CHRON = _hex_to_rgb(_wordmark_palette["aged_gold"]["hex"])       # antique_gold — small lines
_WORDMARK_SHADOW_COLOR    = _hex_to_rgb(_wordmark_palette["near_black"]["hex"])      # void_black — shadow


def _normalize_thumbnail(
    local_path: Path,
    aspect_ratio: str,
    target_dims: Optional[tuple[int, int]] = None,
) -> None:
    """
    Resize and center-crop a downloaded thumbnail to the canonical pixel dimensions
    for the given aspect ratio.

    Target sizes:
      "16:9" → 1280 × 720 (YouTube main episode thumbnail)
      "9:16" → 720 × 1280 (Shorts / vertical thumbnail)

    target_dims (optional): explicit (width, height) override for callers that
    need a non-canonical size — e.g. the short-native vertical cover, which
    ships at 1080×1920 (feed cover + in-player frame, not a YouTube-Studio
    16:9 thumbnail). Must match the aspect_ratio's orientation. When given it
    is used verbatim in place of the _THUMBNAIL_TARGETS lookup.

    Algorithm: scale-to-overfill then center-crop — no letterbox, no pillarbox.
    The image fills the target frame completely; a small amount of source content
    may be cropped from two sides.

    No-ops:
      - aspect_ratio not in _THUMBNAIL_TARGETS (and no target_dims) → returns immediately.
      - Image already exactly at target dimensions → returns immediately (no re-encode).

    Overwrites local_path in place with JPEG quality=95.
    """
    target = target_dims or _THUMBNAIL_TARGETS.get(aspect_ratio)
    if target is None:
        return
    target_w, target_h = target

    img = Image.open(local_path).convert("RGB")
    src_w, src_h = img.size
    if (src_w, src_h) == (target_w, target_h):
        return

    # Scale so the image overfills the target on both axes (no letterbox).
    scale = max(target_w / src_w, target_h / src_h)
    new_w = round(src_w * scale)
    new_h = round(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)

    # Center-crop to exact target.
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    img = img.crop((left, top, left + target_w, top + target_h))

    img.save(local_path, format="JPEG", quality=95)


def _apply_text_overlay(local_path: Path, text: str, placement: str) -> None:
    """
    Render a text overlay band on a thumbnail image in place.

    Renders hook text directly on the image with no background band.
    Legibility comes from bold font, stroke outline, and drop shadow only.

    Args:
        local_path: Absolute path to the JPEG thumbnail (modified in place).
        text:       Overlay text (3–5 words). Rendered in ALL CAPS.
        placement:  "top" or "bottom". Anything else is treated as "bottom".

    No-ops silently if:
      - PIL is unavailable
      - text is empty or whitespace-only
      - local_path does not exist
    """
    if not text or not text.strip():
        return
    if not local_path.exists():
        return

    try:
        from PIL import Image, ImageDraw, ImageFont

        img = Image.open(local_path).convert("RGB")
        img_w, img_h = img.size

        display_text = text.strip().upper()

        # No background band — text sits directly on image.
        # Legibility from font size, stroke, and shadow only.
        font_size = max(24, round(img_h * 0.065))

        font = None
        for font_path in _OVERLAY_BOLD_FONT_CANDIDATES:
            try:
                font = ImageFont.truetype(font_path, font_size)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()

        img_out = img.convert("RGB")
        draw = ImageDraw.Draw(img_out)

        # Position: bottom-left, same zone as typography_plan default
        pad_x = round(img_w * 0.07)
        pad_y = round(img_h * 0.08)
        try:
            bbox = draw.textbbox((0, 0), display_text, font=font)
            text_h = bbox[3] - bbox[1]
        except AttributeError:
            _, text_h = draw.textsize(display_text, font=font)

        text_x = pad_x
        if placement == "top":
            text_y = pad_y
        else:
            text_y = img_h - text_h - pad_y

        _TEXT_COLOR = (245, 225, 175)  # warm parchment
        _STROKE     = (8, 5, 2)
        _SHADOW     = (4, 2, 0)
        shadow_off  = max(3, round(img_h * 0.004))

        # Stroke pass — 8 directions, 2px distance
        for dist in (2, 1):
            for dx, dy in ((-dist, 0), (dist, 0), (0, -dist), (0, dist),
                           (-dist, -dist), (dist, -dist), (-dist, dist), (dist, dist)):
                draw.text((text_x + dx, text_y + dy), display_text, font=font, fill=_STROKE)
        draw.text((text_x + shadow_off, text_y + shadow_off), display_text, font=font, fill=_SHADOW)
        draw.text((text_x, text_y), display_text, font=font, fill=_TEXT_COLOR)

        img_out.save(local_path, format="JPEG", quality=95)

    except Exception:
        # Text overlay is a visual enhancement — never block thumbnail delivery
        pass


def _apply_wordmark_overlay(
    local_path: Path,
    hook_placement: Optional[str] = None,
) -> None:
    """
    Composite the three-line THE HYPOGEUM CHRONICLES wordmark onto a thumbnail
    in place, matching the brand board visual identity.

    Layout (upper-to-mid frame, centred horizontally):
        THE              — small, antique_gold
        HYPOGEUM         — large dominant, warm_gold
        — CHRONICLES —   — medium, antique_gold, em-dash decorators

    Args:
        local_path:     Absolute path to the JPEG thumbnail (modified in place).
        hook_placement: "top" | "bottom" | None — position of the hook text band.
                        When "top", the wordmark starts below the hook band so the
                        two text elements do not overlap.
                        When "bottom" or None, wordmark starts at 12% from top.

    Rendering decisions derived from brand_board.jpg and brand_bible_hypogeum.json:
      - Colour: warm_gold (#E7D6B3) for HYPOGEUM, antique_gold (#C49A42) for THE /
        CHRONICLES lines, matching the brand board dominant gold treatment
      - Font: Copperplate (closest macOS system match to Cinzel SemiBold — all-caps
        serif with engraved character); falls back to Didot → Baskerville → Times
        New Roman Bold → PIL default
      - Position: centred, starting at 12% from top (upper-to-mid frame per brand)
      - Shadow: 2-px offset in void_black (#080C0E) for legibility on dark backgrounds
      - No opaque band — wordmark sits directly on the image, relying on the
        dark background produced by the generation prompt

    No-ops silently if PIL is unavailable or local_path does not exist.
    """
    if not local_path.exists():
        return

    try:
        from PIL import Image, ImageDraw, ImageFont

        img = Image.open(local_path).convert("RGB")
        img_w, img_h = img.size

        # ── Font loading ─────────────────────────────────────────────────────
        def _load_font(size: int) -> ImageFont.FreeTypeFont:
            for path in _WORDMARK_FONT_CANDIDATES:
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    continue
            return ImageFont.load_default()

        # Wordmark target: 8–10% of total thumbnail area (1280×720 = 921,600 px²).
        # Previous sizes produced a wordmark that dominated the frame (25–30% of height).
        # New sizes produce a clean small-brand watermark: story first, brand second.
        # At 720p: HYPOGEUM ≈ 40px, THE ≈ 20px, CHRONICLES ≈ 22px, total ≈ 88px
        # Footprint ≈ 900 × 88 ≈ 79,200 px² ≈ 8.6% of frame area.
        size_hypogeum = max(18, round(img_h * 0.055))
        size_the      = max(10, round(img_h * 0.027))
        size_chron    = max(12, round(img_h * 0.030))

        font_hyp   = _load_font(size_hypogeum)
        font_the   = _load_font(size_the)
        font_chron = _load_font(size_chron)

        # ── Text lines ───────────────────────────────────────────────────────
        line_the   = "THE"
        line_hyp   = "HYPOGEUM"
        line_chron = "— CHRONICLES —"   # em-dash on each side

        # ── Measure all three lines for centring ─────────────────────────────
        img_draw = ImageDraw.Draw(img)

        def _text_w(text: str, font) -> int:
            try:
                bb = img_draw.textbbox((0, 0), text, font=font)
                return bb[2] - bb[0]
            except AttributeError:
                return img_draw.textsize(text, font=font)[0]

        def _text_h(text: str, font) -> int:
            try:
                bb = img_draw.textbbox((0, 0), text, font=font)
                return bb[3] - bb[1]
            except AttributeError:
                return img_draw.textsize(text, font=font)[1]

        w_the   = _text_w(line_the,   font_the)
        w_hyp   = _text_w(line_hyp,   font_hyp)
        w_chron = _text_w(line_chron, font_chron)

        h_the   = _text_h(line_the,   font_the)
        h_hyp   = _text_h(line_hyp,   font_hyp)
        h_chron = _text_h(line_chron, font_chron)

        # ── Vertical layout ───────────────────────────────────────────────────
        # The hook text band is 20% of image height (see _apply_text_overlay).
        # When it sits at the top, shift the wordmark below it to avoid overlap.
        line_gap  = max(4, round(img_h * 0.014))
        if hook_placement == "top":
            start_y = round(img_h * 0.22)
        else:
            start_y = round(img_h * 0.12)

        y_the   = start_y
        y_hyp   = y_the + h_the + line_gap
        y_chron = y_hyp + h_hyp + line_gap

        # ── Horizontal centring ───────────────────────────────────────────────
        x_the   = (img_w - w_the)   // 2
        x_hyp   = (img_w - w_hyp)   // 2
        x_chron = (img_w - w_chron) // 2

        # ── Draw with shadow pass then colour pass ────────────────────────────
        shadow = _WORDMARK_SHADOW_COLOR
        offset = max(1, round(img_h * 0.003))   # ~2px on 720p

        def _draw_line(text, x, y, font, colour):
            img_draw.text((x + offset, y + offset), text, font=font, fill=shadow)
            img_draw.text((x, y), text, font=font, fill=colour)

        _draw_line(line_the,   x_the,   y_the,   font_the,   _WORDMARK_COLOR_THE_CHRON)
        _draw_line(line_hyp,   x_hyp,   y_hyp,   font_hyp,   _WORDMARK_COLOR_HYPOGEUM)
        _draw_line(line_chron, x_chron, y_chron, font_chron, _WORDMARK_COLOR_THE_CHRON)

        img.save(local_path, format="JPEG", quality=95)

    except Exception:
        # Wordmark compositing is a visual enhancement — never block thumbnail delivery
        pass


def _apply_typography_plan(local_path: Path, typography_plan: dict) -> None:
    """
    Render stacked typography from a typography_plan onto a thumbnail in place.

    Replaces the legacy full-width text overlay band. Renders headline + emphasis
    in the zone reserved by the composition_plan, with:
      - Small localized dark backing only behind the text (no full-width band)
      - Headline: smaller, lighter weight
      - Emphasis: larger (headline_size × emphasis_scale), bold
      - Warm cream/gold colour palette (never pure white)
      - Alignment: left or center per typography_plan.alignment

    No-ops silently if PIL is unavailable, local_path does not exist, or both
    headline and emphasis are empty.
    """
    headline        = str(typography_plan.get("headline", "")).strip().upper()
    emphasis        = str(typography_plan.get("emphasis", "")).strip().upper()
    alignment       = typography_plan.get("alignment", "left")
    zone            = typography_plan.get("zone", "lower_left")
    emphasis_scale  = float(typography_plan.get("emphasis_scale", 1.7))
    use_stroke      = bool(typography_plan.get("emphasis_stroke", True))

    if not headline and not emphasis:
        return
    if not local_path.exists():
        return

    try:
        from PIL import Image, ImageDraw, ImageFont

        img = Image.open(local_path).convert("RGB")
        img_w, img_h = img.size

        # Zone → pixel anchor (top-left of text block)
        _ZONE_ANCHORS: dict[str, tuple[float, float]] = {
            "lower_left":   (0.07, 0.62),
            "lower_right":  (0.52, 0.62),
            "lower_center": (0.12, 0.62),
            "upper_left":   (0.07, 0.15),
            "upper_right":  (0.52, 0.15),
            "upper_center": (0.12, 0.15),
            "center":       (0.12, 0.40),
            "full_frame":   (0.07, 0.62),
        }
        ax_frac, ay_frac = _ZONE_ANCHORS.get(zone, (0.07, 0.62))
        anchor_x = round(img_w * ax_frac)
        anchor_y = round(img_h * ay_frac)

        # Headline base: 7% of frame height — larger than before for readability at 320px.
        # Emphasis = base × emphasis_scale (1.4–1.7) → character height ≈ 10–12% of frame.
        base_size = max(24, round(img_h * 0.070))
        emph_size = max(36, round(base_size * emphasis_scale))

        def _load_font(candidates: list, size: int) -> "ImageFont.FreeTypeFont":
            for fp in candidates:
                try:
                    return ImageFont.truetype(fp, size)
                except Exception:
                    continue
            return ImageFont.load_default()

        # Headline uses regular weight; emphasis uses bold weight for visual dominance
        font_head = _load_font(_OVERLAY_FONT_CANDIDATES,      base_size)
        font_emph = _load_font(_OVERLAY_BOLD_FONT_CANDIDATES, emph_size)

        # Measure text
        draw = ImageDraw.Draw(img.convert("RGBA"))

        def _measure(text: str, font) -> tuple[int, int]:
            try:
                bb = draw.textbbox((0, 0), text, font=font)
                return bb[2] - bb[0], bb[3] - bb[1]
            except AttributeError:
                return draw.textsize(text, font=font)

        head_w, head_h = _measure(headline, font_head) if headline else (0, 0)
        emph_w, emph_h = _measure(emphasis, font_emph) if emphasis else (0, 0)
        line_gap = max(6, round(img_h * 0.012))

        block_w = max(head_w, emph_w)
        block_h = (head_h + line_gap + emph_h) if (headline and emphasis) else (head_h or emph_h)

        # Horizontal position
        max_x = img_w - round(img_w * 0.07)
        if alignment == "center":
            cx = img_w // 2
            head_x = cx - head_w // 2
            emph_x = cx - emph_w // 2
        elif alignment == "right":
            head_x = max_x - head_w
            emph_x = max_x - emph_w
        else:
            head_x = anchor_x
            emph_x = anchor_x

        head_y = anchor_y
        emph_y = anchor_y + head_h + line_gap if headline else anchor_y

        # No backing box — legibility comes from stroke, shadow, and font weight only.
        img_out  = img.convert("RGB")
        draw_out = ImageDraw.Draw(img_out)

        _HEAD_COLOR = (210, 195, 160)   # warm cream — headline
        _EMPH_COLOR = (245, 225, 175)   # bright warm parchment — emphasis
        _SHADOW     = (4, 2, 0)         # near-black shadow
        _STROKE     = (8, 5, 2)         # very dark stroke for contrast on any background
        shadow_off  = max(3, round(img_h * 0.004))  # slightly heavier shadow; no backing to rely on

        def _draw_emphasis(text: str, x: int, y: int, font) -> None:
            if use_stroke:
                # 2px stroke: two passes at offset 1 and 2 in all 8 directions
                for dist in (2, 1):
                    for dx, dy in ((-dist, 0), (dist, 0), (0, -dist), (0, dist),
                                   (-dist, -dist), (dist, -dist), (-dist, dist), (dist, dist)):
                        draw_out.text((x + dx, y + dy), text, font=font, fill=_STROKE)
            draw_out.text((x + shadow_off, y + shadow_off), text, font=font, fill=_SHADOW)
            draw_out.text((x, y), text, font=font, fill=_EMPH_COLOR)

        def _draw_headline(text: str, x: int, y: int, font) -> None:
            # Headline also gets a stroke pass for legibility without backing
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                draw_out.text((x + dx, y + dy), text, font=font, fill=_STROKE)
            draw_out.text((x + 2, y + 2), text, font=font, fill=_SHADOW)
            draw_out.text((x, y), text, font=font, fill=_HEAD_COLOR)

        if headline:
            _draw_headline(headline, head_x, head_y, font_head)
        if emphasis:
            _draw_emphasis(emphasis, emph_x, emph_y, font_emph)

        img_out.save(local_path, format="JPEG", quality=95)

    except Exception:
        # Typography rendering is a visual enhancement — never block thumbnail delivery
        pass


# ---------------------------------------------------------------------------
# Direct provider image generation functions
# ---------------------------------------------------------------------------

def _generate_thumbnail_openai(
    deliverable_id: str,
    image_prompt: str,
    aspect_ratio: str,
    config: "Config",
    local_path: Path,
    log,
) -> str:
    """
    Generate a thumbnail via the OpenAI Images API directly.

    POST {OPENAI_IMAGE_BASE_URL}/images/generations
    Model: config.OPENAI_IMAGE_MODEL (default: gpt-image-1)

    VERIFIED API details (OpenAI, as of 2025):
      endpoint    POST https://api.openai.com/v1/images/generations  ✓
      auth        Authorization: Bearer {key}                         ✓
      model       gpt-image-1 (official name; gpt-image-2 = MUAPI-internal slug) ✓
      sizes       1024x1024, 1536x1024, 1024x1536                    ✓
      quality     low | medium | high | auto                          ✓
      response    {"data": [{"b64_json": "..."}]}                     ✓

    Returns the model slug used. Raises MUAPIAuthFailure on 401,
    MUAPIError on any other failure.
    """
    import json
    import base64
    import urllib.request
    import urllib.error

    if not config.OPENAI_API_KEY:
        raise MUAPIAuthFailure()

    size_map = {"16:9": "1536x1024", "9:16": "1024x1536"}
    size = size_map.get(aspect_ratio, "1024x1024")
    model = config.OPENAI_IMAGE_MODEL

    payload = json.dumps({
        "model":   model,
        "prompt":  image_prompt,
        "n":       1,
        "size":    size,
        "quality": config.THUMBNAIL_IMAGE_QUALITY,  # "high"
        # response_format is not supported by gpt-image-1 (DALL-E 2/3 only).
        # gpt-image-1 always returns b64_json without this parameter.
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{config.OPENAI_IMAGE_BASE_URL}/images/generations",
        data=payload,
        headers={
            "Authorization": f"Bearer {config.OPENAI_API_KEY}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )

    log.info("openai_thumbnail_submit", deliverable_id=deliverable_id,
             model=model, size=size)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code == 401:
            raise MUAPIAuthFailure()
        if exc.code == 402:
            raise MUAPIQuotaExhausted()
        raise MUAPIError(
            f"OpenAI Images API HTTP {exc.code} for {deliverable_id!r}: {body}"
        )

    data = result.get("data", [])
    if not data or not data[0].get("b64_json"):
        raise MUAPIError(
            f"OpenAI Images API returned no image data for {deliverable_id!r}"
        )

    image_bytes = base64.b64decode(data[0]["b64_json"])
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(image_bytes)

    if local_path.stat().st_size < _MIN_IMAGE_BYTES:
        raise MUAPIError(
            f"OpenAI image suspiciously small ({local_path.stat().st_size} B) "
            f"for {deliverable_id!r}"
        )

    log.info("openai_thumbnail_generated", deliverable_id=deliverable_id,
             model=model, size_kb=round(local_path.stat().st_size / 1024))
    return model


def _discover_openrouter_image_models(api_key: str) -> list:
    """
    Query OpenRouter for models that support image output.

    GET https://openrouter.ai/api/v1/models?output_modalities=image

    Returns a list of model ID strings, or [] on any error.
    Does not raise — caller decides how to handle an empty list.
    """
    import json
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models?output_modalities=image",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return [m["id"] for m in body.get("data", []) if m.get("id")]
    except Exception:
        return []


def _generate_thumbnail_openrouter(
    deliverable_id: str,
    image_prompt: str,
    aspect_ratio: str,
    config: "Config",
    local_path: Path,
    log,
) -> str:
    """
    Generate a thumbnail via OpenRouter's chat completions endpoint with image modality.

    Per OpenRouter docs, image generation uses:
      POST https://openrouter.ai/api/v1/chat/completions
      with modalities: ["image"] or ["image", "text"]

    Note: the OpenAI-style images endpoint does not exist on OpenRouter.

    VERIFICATION STATUS:
      endpoint    POST /api/v1/chat/completions       CONFIRMED (OpenRouter docs)
      auth        Authorization: Bearer               CONFIRMED
      headers     HTTP-Referer, X-Title               CONFIRMED
      modalities  ["image"]                           CONFIRMED (OpenRouter docs)
      model       openai/gpt-image-1                  CONFIRMED format; availability UNVERIFIED
      response    choices[0].message.images[]         LIVE-TESTED 2026-06-21
                  content is null; image is in message.images[0].image_url.url
                  as a data: URI (base64). Parser also checks message.content[]
                  as fallback for other models.
      model_discovery  GET /api/v1/models?output_modalities=image  CONFIRMED (OpenRouter docs)

    Response parser handles:
      - data: URI  "data:image/png;base64,<b64>"  → base64 decode → write
      - plain URL  "https://..."                  → _download_file() → write

    Raises MUAPIAuthFailure on 401, MUAPIError on any other failure.
    """
    import json
    import base64
    import urllib.request
    import urllib.error

    if not config.OPENROUTER_API_KEY:
        raise MUAPIAuthFailure()

    model = config.OPENROUTER_IMAGE_MODEL

    payload = json.dumps({
        "model":      model,
        "messages":   [{"role": "user", "content": image_prompt}],
        "modalities": ["image"],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            "Content-Type":  "application/json",
            "HTTP-Referer":  config.OPENROUTER_APP_URL or "https://hypogeum.ai",
            "X-Title":       config.OPENROUTER_APP_NAME or "hypogeum-brain",
        },
        method="POST",
    )

    log.info("openrouter_thumbnail_submit", deliverable_id=deliverable_id, model=model)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code == 401:
            raise MUAPIAuthFailure()
        if exc.code == 402:
            raise MUAPIQuotaExhausted()
        raise MUAPIError(
            f"OpenRouter chat/completions HTTP {exc.code} for {deliverable_id!r}: {body}"
        )

    # Extract image URL from response.
    #
    # LIVE-TESTED response shape (Phase 1C smoke test, 2026-06-21):
    #   choices[0].message.content  = null
    #   choices[0].message.images   = [{"type":"image_url","image_url":{"url":"data:..."}}]
    #
    # The image is in message.images[], NOT message.content[].
    # content is null. We check images[] first, then content[] as fallback
    # for any future model that returns image parts in content instead.
    choices = result.get("choices", [])
    if not choices:
        raise MUAPIError(
            f"OpenRouter returned no choices for {deliverable_id!r}. "
            f"Raw keys: {list(result.keys())}"
        )

    message   = choices[0].get("message", {})
    image_url: Optional[str] = None

    # Primary: message.images[] (observed behaviour with openai/gpt-5-image-mini)
    images = message.get("images") or []
    for img in images:
        if isinstance(img, dict) and img.get("type") == "image_url":
            image_url = (img.get("image_url") or {}).get("url", "")
            if image_url:
                break

    # Fallback: message.content[] parts (anticipated behaviour for other models)
    if not image_url:
        content = message.get("content") or []
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    image_url = (part.get("image_url") or {}).get("url", "")
                    if image_url:
                        break
        elif isinstance(content, str) and content:
            raise MUAPIError(
                f"OpenRouter returned text content, not image, for {deliverable_id!r}. "
                f"Model may not support image modality: {config.OPENROUTER_IMAGE_MODEL}"
            )

    if not image_url:
        raise MUAPIError(
            f"OpenRouter: no image URL found in message.images or message.content "
            f"for {deliverable_id!r}. "
            f"Message keys: {list(message.keys())}"
        )

    local_path.parent.mkdir(parents=True, exist_ok=True)

    if image_url.startswith("data:"):
        # data: URI — "data:image/png;base64,<b64data>"
        try:
            b64_part = image_url.split(",", 1)[1]
            local_path.write_bytes(base64.b64decode(b64_part))
        except Exception as exc:
            raise MUAPIError(
                f"OpenRouter: failed to decode data URI for {deliverable_id!r}: {exc}"
            )
    else:
        # Plain CDN URL
        success, reason = _download_file(image_url, local_path, log)
        if not success:
            raise MUAPIError(
                f"OpenRouter image download failed for {deliverable_id!r}: {reason}"
            )

    if local_path.stat().st_size < _MIN_IMAGE_BYTES:
        raise MUAPIError(
            f"OpenRouter image suspiciously small ({local_path.stat().st_size} B) "
            f"for {deliverable_id!r}"
        )

    log.info("openrouter_thumbnail_generated", deliverable_id=deliverable_id,
             model=model, size_kb=round(local_path.stat().st_size / 1024))
    return model


def _expected_thumbnail_model(provider: str, config: "Config") -> str:
    """
    Model that will be attempted for a given thumbnail provider — used only for
    pre-submission and fallback logging (thumbnail_provider_attempt /
    thumbnail_provider_failed_trying_next), never for dispatch logic itself.
    config.THUMBNAIL_IMAGE_MODEL remains the single authoritative source for the
    MUAPI/GPT-Image-2 model string; this helper does not hardcode it separately.
    """
    return {
        "openai": config.OPENAI_IMAGE_MODEL,
        "openrouter": config.OPENROUTER_IMAGE_MODEL,
        "muapi": config.THUMBNAIL_IMAGE_MODEL,
    }.get(provider, "unknown")


def generate_thumbnail(
    deliverable_id: str,
    image_prompt: str,
    aspect_ratio: str,
    config: Config,
    episode_id: str,
    output_dir: Path,
    *,
    agent: Optional[str] = None,
    budget_guard: Optional[MUAPIBudgetGuard] = None,
    text_overlay: Optional[dict] = None,
    typography_plan: Optional[dict] = None,
    typography_integration: str = "composited",
    channel_id: Optional[str] = None,
    thumbnail_target_dims: Optional[tuple[int, int]] = None,
) -> ThumbnailResult:
    """
    Generate a thumbnail image for one deliverable (main episode or a Short).

    thumbnail_target_dims (optional): explicit (width, height) for the final
    normalized file, overriding the canonical per-aspect size. Forwarded only
    to the "muapi" provider branch (the one short-native vertical covers use).
    The short-native 9:16 cover passes (1080, 1920).

    channel_id (added for the Authorization Authority gate path, plan
    §5.5/§6 Commit F): optional, forwarded only to the "muapi" provider
    branch. When supplied AND a real, verified RunAuthorizationGrant
    resolves for (channel_id, episode_id), that branch routes through
    the gate instead of its legacy path; the "openai"/"openrouter"
    branches are unaffected and out of scope for this program.

    Tries each provider in config.THUMBNAIL_IMAGE_PROVIDER / _FALLBACK_PROVIDER /
    _FALLBACK2_PROVIDER order (default: muapi/GPT Image 2 primary, then openrouter,
    then openai — see docs/validation/thumbnail_image_generation_validation/
    GPT_IMAGE_2_ROUTING_AUDIT.md), polls for completion, and downloads the image
    locally. No upload step — thumbnails are local-only assets (unlike reference
    images, they are not used as I2V conditioning).

    Args:
        deliverable_id: Identifier for the thumbnail deliverable. Use "main" for
                        the main episode; use the short_id (e.g. "sh_01") for Shorts.
        image_prompt:   Provider-ready image generation prompt (30–60 words).
        aspect_ratio:   "16:9" for main episode thumbnails; "9:16" for Shorts.
        config:         Loaded Config instance.
        episode_id:     Episode identifier.
        output_dir:     Episode asset directory (.hypogeum-assets/episodes/{ep_id}/).
        agent:          Optional agent name for log correlation.
        budget_guard:   Optional M5.6 budget guard. Blocks if episode budget is exceeded.
        text_overlay:     Optional dict with "text" (str) and "placement" ("top"|"bottom").
                          Legacy compositor. When typography_plan is provided this is
                          ignored for the text rendering step (typography_plan takes
                          authority) but retained for schema backwards compatibility.
        typography_plan:  Optional dict from thumbnail_typography_planner. When present,
                          _apply_typography_plan() is called instead of _apply_text_overlay(),
                          rendering stacked headline + emphasis in the reserved zone with a
                          localized dark backing (no full-width band).

    Returns:
        ThumbnailResult with asset_id and local_path.

    Raises:
        MUAPIAuthFailure     — HTTP 401.
        MUAPIQuotaExhausted  — HTTP 402.
        MUAPIBudgetExhausted — Episode budget limit reached (M5.6).
        MUAPIError           — Image generation or download failed.
    """
    # Multi-tenant Phase 2: spend with the keys of the workspace that owns
    # channel_id (hypogeum.workspace_keys); no channel = caller's config.
    if channel_id:
        from hypogeum.workspace_keys import config_for_channel
        config = config_for_channel(config, channel_id)
    log = get_logger(episode_id=episode_id, agent=agent or "muapi")

    # Provider priority chain: primary → fallback1 → fallback2
    # Canonical default: muapi/GPT Image 2 primary → openrouter → openai
    # (configurable via env vars; see GPT_IMAGE_2_ROUTING_AUDIT.md for the decision).
    _chain_raw = [
        config.THUMBNAIL_IMAGE_PROVIDER,
        config.THUMBNAIL_FALLBACK_PROVIDER,
        config.THUMBNAIL_FALLBACK2_PROVIDER,
    ]
    # Deduplicate preserving order; drop empty strings
    _seen: set = set()
    provider_chain = []
    for _p in _chain_raw:
        if _p and _p not in _seen:
            _seen.add(_p)
            provider_chain.append(_p)

    last_exc: Optional[Exception] = None
    for attempt_idx, provider in enumerate(provider_chain):
        is_fallback = attempt_idx > 0
        log.info(
            "thumbnail_provider_attempt",
            provider=provider,
            model=_expected_thumbnail_model(provider, config),
            status="fallback" if is_fallback else "primary",
            fallback_index=attempt_idx,
            deliverable_id=deliverable_id,
        )
        try:
            result = _generate_thumbnail_one_provider(
                provider=provider,
                deliverable_id=deliverable_id,
                image_prompt=image_prompt,
                aspect_ratio=aspect_ratio,
                config=config,
                output_dir=output_dir,
                episode_id=episode_id,
                budget_guard=budget_guard,
                text_overlay=text_overlay,
                typography_plan=typography_plan,
                typography_integration=typography_integration,
                log=log,
                is_fallback=is_fallback,
                channel_id=channel_id,
                thumbnail_target_dims=thumbnail_target_dims,
            )
            if is_fallback:
                log.info(
                    "thumbnail_provider_fallback_succeeded",
                    provider=provider,
                    fallback_index=attempt_idx,
                    original_error=str(last_exc)[:120] if last_exc else None,
                )
            return result
        except MUAPIBudgetExhausted:
            raise  # budget constraint — never fall through
        except Exception as exc:
            last_exc = exc
            _next_provider = provider_chain[attempt_idx + 1] if attempt_idx + 1 < len(provider_chain) else None
            log.warn(
                "thumbnail_provider_failed_trying_next",
                provider=provider,
                model=_expected_thumbnail_model(provider, config),
                fallback_index=attempt_idx,
                next_provider=_next_provider,
                next_model=_expected_thumbnail_model(_next_provider, config) if _next_provider else None,
                error=str(exc)[:160],
            )

    # Re-raise the original exception from the last provider so callers
    # receive the specific type (MUAPIAuthFailure, MUAPIQuotaExhausted, etc.)
    # rather than a generic MUAPIError wrapper.
    if last_exc is not None:
        raise last_exc  # type: ignore[misc]
    raise MUAPIError("All thumbnail providers exhausted")


# ---------------------------------------------------------------------------
# Internal: single-provider thumbnail dispatch
# ---------------------------------------------------------------------------

def _generate_thumbnail_one_provider(
    provider: str,
    deliverable_id: str,
    image_prompt: str,
    aspect_ratio: str,
    config: "Config",
    output_dir: Path,
    episode_id: str,
    budget_guard: Optional[MUAPIBudgetGuard],
    text_overlay: Optional[dict],
    typography_plan: Optional[dict],
    typography_integration: str,
    log,
    is_fallback: bool,
    channel_id: Optional[str] = None,
    thumbnail_target_dims: Optional[tuple[int, int]] = None,
) -> "ThumbnailResult":
    """
    Try one thumbnail provider. Returns ThumbnailResult on success.
    Raises on any failure — caller decides whether to try the next provider.
    MUAPIBudgetExhausted is always re-raised by the caller without retrying.

    channel_id (Commit F): forwarded only to the "muapi" branch's
    _attempt_thumbnail_generation() call -- the "openai"/"openrouter"
    branches are out of scope for this program's gate migration.
    """
    local_path = output_dir / "thumbnails" / f"{deliverable_id}.jpg"

    if provider == "openai":
        model = _generate_thumbnail_openai(
            deliverable_id=deliverable_id,
            image_prompt=image_prompt,
            aspect_ratio=aspect_ratio,
            config=config,
            local_path=local_path,
            log=log,
        )
        _normalize_thumbnail(local_path, aspect_ratio)
        if aspect_ratio == "16:9":
            _apply_wordmark_overlay(local_path)
        log.info("thumbnail_generated", deliverable_id=deliverable_id,
                 model=model, provider="openai", used_fallback=is_fallback)
        thumb_asset_id = f"episodes/{episode_id}/thumbnails/{deliverable_id}"
        _finance.record_cost(
            provider="openai",
            model=model,
            call_type="thumbnail_generation",
            cost_usd=MODEL_CREDIT_COSTS.get(model, 0.04),
            asset_id=thumb_asset_id,
        )
        return ThumbnailResult(
            asset_id=thumb_asset_id,
            local_path=local_path,
            model_used=model,
            provider_used="openai",
            used_fallback=is_fallback,
        )

    if provider == "openrouter":
        model = _generate_thumbnail_openrouter(
            deliverable_id=deliverable_id,
            image_prompt=image_prompt,
            aspect_ratio=aspect_ratio,
            config=config,
            local_path=local_path,
            log=log,
        )
        _normalize_thumbnail(local_path, aspect_ratio)
        if aspect_ratio == "16:9":
            _apply_wordmark_overlay(local_path)
        log.info("thumbnail_generated", deliverable_id=deliverable_id,
                 model=model, provider="openrouter", used_fallback=is_fallback)
        thumb_asset_id = f"episodes/{episode_id}/thumbnails/{deliverable_id}"
        _finance.record_cost(
            provider="openrouter",
            model=model,
            call_type="thumbnail_generation",
            cost_usd=MODEL_CREDIT_COSTS.get(model, 0.04),
            asset_id=thumb_asset_id,
        )
        return ThumbnailResult(
            asset_id=thumb_asset_id,
            local_path=local_path,
            model_used=model,
            provider_used="openrouter",
            used_fallback=is_fallback,
        )

    if provider != "muapi":
        raise ValueError(
            f"Unknown thumbnail provider {provider!r}. "
            f"Valid values: 'openrouter', 'openai', 'muapi'."
        )

    primary_model = config.THUMBNAIL_IMAGE_MODEL
    fallback_model = config.THUMBNAIL_FALLBACK_MODEL  # MUAPI-internal model fallback

    image_model, used_muapi_fallback = _attempt_thumbnail_generation(
        deliverable_id=deliverable_id,
        image_prompt=image_prompt,
        aspect_ratio=aspect_ratio,
        primary_model=primary_model,
        fallback_model=fallback_model,
        config=config,
        output_dir=output_dir,
        episode_id=episode_id,
        budget_guard=budget_guard,
        text_overlay=text_overlay,
        typography_plan=typography_plan,
        typography_integration=typography_integration,
        log=log,
        channel_id=channel_id,
        thumbnail_target_dims=thumbnail_target_dims,
    )
    thumb_asset_id = f"episodes/{episode_id}/thumbnails/{deliverable_id}"
    _finance.record_cost(
        provider="muapi",
        model=image_model,
        call_type="thumbnail_generation",
        cost_usd=MODEL_CREDIT_COSTS.get(image_model, 0.09),
        asset_id=thumb_asset_id,
    )
    return ThumbnailResult(
        asset_id=thumb_asset_id,
        local_path=output_dir / "thumbnails" / f"{deliverable_id}.jpg",
        model_used=image_model,
        provider_used="muapi",
        used_fallback=is_fallback or used_muapi_fallback,
    )


# ---------------------------------------------------------------------------
# Internal: single generation attempt
# ---------------------------------------------------------------------------

_DRY_RUN_PLACEHOLDER_RESOLUTIONS: dict[str, tuple[int, int]] = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
}


def _generate_dry_run_placeholder_clip(
    dest_path: Path,
    aspect_ratio: str,
    duration_seconds: float,
    log,
) -> None:
    """
    Write a deterministic, structurally-valid placeholder video clip (solid
    dark color, no audio stream) to dest_path via ffmpeg's lavfi color
    source -- the same technique hypogeum.agents.video_renderer.py already
    uses for its own missing-clip black-frame fallback, reused here rather
    than reimplemented.

    Added 2026-07-18 per explicit product decision: MUAPI_DRY_RUN must
    produce a render package with the exact same structural contract
    Assembly/QA validate for a real run (clip_availability_rate=100%,
    generation_status="generated") -- never a special-cased "dry_run"
    status that downstream code has to know about. The only difference
    from a real provider-generated clip is the pixel content itself; QA's
    production_ready / clip_availability logic is never touched.

    Raises MUAPIError if ffmpeg fails -- a broken placeholder is a genuine
    failure, not something to silently paper over.
    """
    width, height = _DRY_RUN_PLACEHOLDER_RESOLUTIONS.get(aspect_ratio, (1280, 720))
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=0x141414:s={width}x{height}:r=30:d={duration_seconds:.3f}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(dest_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as exc:
        raise MUAPIError(
            f"Dry-run placeholder clip generation failed for {dest_path}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise MUAPIError(
            f"Dry-run placeholder clip generation failed for {dest_path}: "
            f"ffmpeg exit {result.returncode}: {result.stderr[-500:]}"
        )
    log.info(
        "muapi_dry_run_placeholder_clip_written",
        dest_path=str(dest_path),
        width=width,
        height=height,
        duration_seconds=duration_seconds,
    )


def _run_shadow_observation(
    *,
    envelope: Optional[GenerationExecutionEnvelope],
    shadow_evidence: Optional["_shadow_observation.ShadowEvidence"],
    output_dir: Path,
    episode_id: str,
    short_id: Optional[str],
    prompt: str,
    model: str,
    aspect_ratio: str,
    requested_duration: float,
    scene_id: str,
    log,
    observation_only: bool = False,
) -> None:
    """
    The one place hypogeum.muapi calls into hypogeum.shadow_observation.
    Always runs, always non-mutating, and never allowed to affect the
    real generation attempt that follows it -- every exception this
    function's own body could raise is caught here, at the outermost
    level, and turned into a structured error log event
    (muapi_shadow_observation_failed) rather than propagating. This is
    what makes "Shadow logging failure must be visible as structured
    evidence... never silently ignored" (Part 3 requirement 9) hold
    without also making shadow-mode instrumentation itself capable of
    breaking a real (or dry-run) generation call.

    Dominance-leak validation: if envelope.channel_id == "hypogeum" and
    the caller did not already supply a dominance_leak_validator, this
    function loads hypogeum.hypogeum_channel_validators's Hypogeum-
    specific validator -- never inlined here, and never imported by
    hypogeum.shadow_observation or hypogeum.submission_gate (Factory
    Core stays channel-agnostic; only this integration layer knows
    Hypogeum exists).

    observation_only: forwarded to evaluate_shadow() as
    mode="shadow_observation_only" (vs. the default "shadow") so the
    persisted ShadowDecision is auditable evidence of which execution
    context produced it -- a genuine zero-provider-call observation
    versus ordinary shadow instrumentation that accompanied a real (or
    dry-run) attempt. This function's own behavior is otherwise
    identical in both modes; the actual network-call skip happens in
    the caller (_attempt_generation()), not here.
    """
    try:
        evidence = shadow_evidence or _shadow_observation.ShadowEvidence()
        dominance_validator = evidence.dominance_leak_validator
        if dominance_validator is None and envelope is not None and envelope.channel_id == "hypogeum":
            sibling_prompts = evidence.sibling_prompts

            def dominance_validator(env, _siblings=sibling_prompts):  # noqa: PLC0415
                from hypogeum import hypogeum_channel_validators  # noqa: PLC0415
                loaded = hypogeum_channel_validators.load_dominance_leak_validator(
                    env.channel_id, sibling_prompts=_siblings,
                )
                return loaded(env)

        decision = _shadow_observation.evaluate_shadow(
            envelope, at=datetime.now(timezone.utc).isoformat(),
            execution_id=episode_id, deliverable=short_id,
            actual_provider_bound_content=prompt, actual_provider="muapi", actual_model=model,
            actual_provider_operation="video.text_to_video",
            live_sources=evidence.live_sources, authorization=evidence.authorization,
            lease_conn=evidence.lease_conn, lease_owner_id=evidence.lease_owner_id,
            lease_fencing_token=evidence.lease_fencing_token,
            receipt_store=evidence.receipt_store, budget_scopes=evidence.budget_scopes,
            dominance_leak_validator=dominance_validator,
            expected_aspect_ratio=aspect_ratio,
            expected_deterministic_output_path=str(scene_file_path(output_dir, scene_id, short_id)),
            output_already_exists=False,
            mode="shadow_observation_only" if observation_only else "shadow",
        )
        store = evidence.shadow_store or _shadow_observation.ShadowObservationStore(output_dir)
        store.record(decision)
        log.info(
            "muapi_shadow_observation_recorded",
            scene_id=scene_id, decision_id=decision.decision_id, verdict=decision.verdict,
            first_failing_check=decision.first_failing_check,
        )
    except Exception as exc:  # noqa: BLE001 — must be visible, must never break real generation
        log.error(
            "muapi_shadow_observation_failed",
            scene_id=scene_id, error=f"{type(exc).__name__}: {exc}",
        )


def _attempt_generation(
    prompt: str,
    tier: int,
    reference_image_url: Optional[str],
    scene_id: str,
    short_id: Optional[str],
    aspect_ratio: str,
    requested_duration: float,
    episode_id: str,
    output_dir: Path,
    config: Config,
    log,
    is_fallback: bool,
    budget_guard: Optional[MUAPIBudgetGuard] = None,
    shadow_evidence: Optional["_shadow_observation.ShadowEvidence"] = None,
    observation_only: bool = False,
) -> tuple[Optional[dict], Optional[str]]:
    """
    Execute one generation attempt: submit → poll → download → validate.

    Returns (manifest_entry, None) on success.
    Returns (None, failure_reason) on failure.

    Raises MUAPIAuthFailure / MUAPIQuotaExhausted on halt conditions.
    Raises MUAPIBudgetExhausted if budget_guard blocks this attempt (M5.6).

    observation_only: when True, this attempt terminates immediately
    after the shadow decision below is persisted -- see the early
    return statement placed between the shadow-observation call and
    the real HTTP submission further down. No budget_guard state is
    mutated, no network call is made, no file is written. Defaults to
    False (unchanged prior behavior).
    """
    model = _tier_to_model(tier, reference_image_url, config)
    endpoint = f"{config.MUAPI_BASE_URL}/{model}"
    payload = _build_job_payload(
        prompt, model, aspect_ratio, requested_duration, reference_image_url,
        video_resolution=config.MUAPI_VIDEO_RESOLUTION,
    )

    # M5.6 — Budget check before paid submission (skipped in dry-run — no
    # real cost -- and skipped in observation_only mode, for the same
    # reason: no submission is about to happen, so recording an attempt/
    # estimated spend against budget_guard's in-memory counters would
    # misrepresent spend that never occurred).
    if budget_guard is not None and not config.MUAPI_DRY_RUN and not observation_only:
        budget_guard.check_and_record_attempt(scene_id, model)

    # --- Commit 4 (2026-08-15): non-mutating shadow-mode gate observation.
    # Runs unconditionally, on every attempt, immediately before the real
    # network boundary -- the Core Shadow-Mode Rule requires this happen
    # "automatically", never opt-in. Never blocks, delays, retries, or
    # changes anything about the actual submission below: this entire
    # block only evaluates and records; a failure inside it is itself
    # caught and logged, never allowed to affect generation. See
    # hypogeum.shadow_observation.evaluate_shadow()'s own docstring for
    # why an evaluation exception becomes a structured UNVERIFIABLE
    # decision rather than propagating.
    _run_shadow_observation(
        envelope=shadow_evidence.envelope if shadow_evidence else None,
        shadow_evidence=shadow_evidence, output_dir=output_dir, episode_id=episode_id,
        short_id=short_id, prompt=prompt, model=model, aspect_ratio=aspect_ratio,
        requested_duration=requested_duration, scene_id=scene_id, log=log,
        observation_only=observation_only,
    )

    # --- Observation-only interception (post-Commit-4, 2026-08-15).
    # Structurally the ONLY statement between the shadow-decision write
    # above and the real network boundary below: when observation_only
    # is True, this attempt returns here, unconditionally, before
    # endpoint/payload are ever handed to _submit_http(). No exception,
    # retry, fallback, or adapter path exists that can reach the network
    # call from this branch -- the function returns control to its
    # caller on this line. See tests/test_muapi_submission_gate_shadow.py
    # for the AST-level proof that this statement precedes every
    # _submit_http() call reachable from this function.
    if observation_only:
        log.info("muapi_observation_only_terminated", scene_id=scene_id, model=model)
        return {
            "scene_id": scene_id,
            "asset_id": None,
            "generation_status": "observation_only",
            "provider": "muapi",
            "tier_used": tier,
            "model_used": model,
            "prompt_used": None,
            "fallback_used": is_fallback,
            "failure_reason": None,
        }, None

    # Submit (with HTTP retry on transient errors)
    request_id, failure_reason = _submit_http(endpoint, payload, config, scene_id, log)
    if request_id is None:
        return None, failure_reason

    # M5.6, extended 2026-07-18 — Dry-run: skip poll/download, but still
    # produce a real, structurally-valid placeholder clip and report
    # generation_status="generated" (not a special "dry_run" status) so
    # Assembly/QA validate the exact same contract as a real run. See
    # _generate_dry_run_placeholder_clip()'s own docstring for why.
    if request_id.startswith(_DRY_RUN_REQUEST_ID_PREFIX):
        dest_path = scene_file_path(output_dir, scene_id, short_id)
        _generate_dry_run_placeholder_clip(dest_path, aspect_ratio, requested_duration, log)
        log.info(
            "muapi_dry_run_complete",
            scene_id=scene_id,
            request_id=request_id,
            model=model,
        )
        return {
            "scene_id": scene_id,
            "asset_id": _compute_asset_id(episode_id, scene_id, short_id),
            "generation_status": "generated",
            "provider": "muapi",
            "tier_used": tier,
            "model_used": model,
            "prompt_used": prompt,
            "fallback_used": is_fallback,
            "failure_reason": None,
            "request_id": request_id,
        }, None

    # Poll until terminal status
    result, failure_reason = _poll_until_complete(request_id, config, scene_id, log)
    if result is None:
        return None, failure_reason

    # Extract video URL (never logged — may be signed/temporary)
    _outputs = result.get("outputs", [])
    video_url = (_outputs[0] if isinstance(_outputs[0], str) else _outputs[0].get("url", "")) if _outputs else ""
    if not video_url:
        return None, "missing_video_url_in_result"

    # Resolve file destination
    dest_path = scene_file_path(output_dir, scene_id, short_id)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Download (with 1 retry)
    success, dl_reason = _download_file(video_url, dest_path, log)
    if not success:
        success, dl_reason = _download_file(video_url, dest_path, log)
        if not success:
            log.error(
                "muapi_download_failed",
                scene_id=scene_id,
                failure_reason=dl_reason,
                download_url_masked=_mask_url(video_url),
            )
            return None, f"download_failed: {dl_reason}"

    # Validate downloaded file
    valid, val_reason = _validate_video_file(dest_path, requested_duration)
    if not valid:
        dest_path.unlink(missing_ok=True)
        return None, f"validation_failed: {val_reason}"

    # Log cost if available
    credits_used = result.get("credits_used") or result.get("cost", {}).get("amount_usd")

    log.info(
        "muapi_download_completed",
        scene_id=scene_id,
        asset_id=_compute_asset_id(episode_id, scene_id, short_id),
        file_size_bytes=dest_path.stat().st_size if dest_path.exists() else 0,
        model_used=model,
        credits_used=credits_used,
    )

    # M5.6 — Record actual spend after confirmed download
    if budget_guard is not None and credits_used:
        budget_guard.record_actual_spend(float(credits_used))

    computed_asset_id = _compute_asset_id(episode_id, scene_id, short_id)
    cost_usd = MODEL_CREDIT_COSTS.get(model, _DEFAULT_UNKNOWN_MODEL_COST)
    _finance.record_cost(
        provider="muapi",
        model=model,
        call_type="scene_generation",
        cost_usd=cost_usd,
        tokens_or_credits=float(credits_used) if credits_used else None,
        asset_id=computed_asset_id,
    )

    return {
        "scene_id": scene_id,
        "asset_id": computed_asset_id,
        "generation_status": "generated",
        "provider": "muapi",
        "tier_used": tier,
        "model_used": model,
        "prompt_used": prompt,
        "fallback_used": is_fallback,
        "failure_reason": None,
        "request_id": request_id,
    }, None


# ---------------------------------------------------------------------------
# Internal: thumbnail generation helpers
# ---------------------------------------------------------------------------

_THUMBNAIL_PROVIDER_OPERATION = "image.thumbnail"
_THUMBNAIL_COMPILER_IDENTITY = "hypogeum.muapi._generate_thumbnail_with_model"


def _generate_thumbnail_via_gate(
    *, channel_id: str, grant, model: str, deliverable_id: str, image_prompt: str,
    aspect_ratio: str, episode_id: str, output_dir: Path, config: Config, local_path: Path, log,
) -> None:
    """
    The grant-derived gate path for thumbnail generation (muapi
    provider branch only) -- mirrors _submit_scene_via_gate()'s
    structure exactly (see that function's own docstring for the full
    rationale). Same void/write-to-local_path/raise-MUAPIError-on-
    failure contract as _generate_thumbnail_with_model(), so it is a
    drop-in replacement at its one call site in
    _attempt_thumbnail_generation(). Never reached unless a real,
    verified grant already resolves for (channel_id, episode_id).
    """
    import hashlib

    import hypogeum.adapter_registry as adapter_registry
    import hypogeum.authorization_authority as authority
    import hypogeum.authorization_store as authorization_store
    import hypogeum.budget_scopes as bs
    import hypogeum.channel_config as channel_config
    import hypogeum.db as db
    import hypogeum.submission_gate as submission_gate
    import hypogeum.workflow_executor as workflow_executor
    from datetime import timedelta
    from hypogeum.providers.muapi_thumbnail_adapter import (
        SCHEMA_VERSION as _THUMBNAIL_SCHEMA_VERSION, MuapiThumbnailAdapter, encode_thumbnail_payload,
    )
    from hypogeum.provider_receipt import WriteAheadReceiptStore

    at = datetime.now(timezone.utc).isoformat()
    is_gpt_image_2 = model in _GPT_IMAGE_2_MODELS

    package_identity = channel_config.compute_channel_package_identity(channel_id)
    if not package_identity.get("available"):
        raise MUAPIError(f"channel_package_identity_unavailable for {deliverable_id!r}")
    workflow_binding = workflow_executor.resolve_workflow_binding(
        channel_id, _HYPOGEUM_WORKFLOW_ID, _HYPOGEUM_WORKFLOW_VERSION,
    )

    plan_checksum = hashlib.sha256(_canonical_json_bytes({"deliverable_id": deliverable_id, "prompt": image_prompt})).hexdigest()
    structured_spec_checksum = hashlib.sha256(_canonical_json_bytes({"aspect_ratio": aspect_ratio, "model": model})).hexdigest()

    operation_payload_bytes = encode_thumbnail_payload(
        prompt=image_prompt, model=model, aspect_ratio=aspect_ratio, deliverable_id=deliverable_id,
        episode_id=episode_id, output_dir=str(output_dir), is_gpt_image_2=is_gpt_image_2,
        resolution=config.THUMBNAIL_IMAGE_RESOLUTION if is_gpt_image_2 else None,
        quality=config.THUMBNAIL_IMAGE_QUALITY if is_gpt_image_2 else None,
    )
    operation_payload_content = operation_payload_bytes.decode("utf-8")
    operation_payload_sha256 = hashlib.sha256(operation_payload_bytes).hexdigest()

    expected_output_identity = f"episodes/{episode_id}/thumbnails/{deliverable_id}"
    max_cost = Decimal(str(MODEL_CREDIT_COSTS.get(model, 0.015)))
    authorization_id = f"{episode_id}:thumb_{deliverable_id}"

    # Explicit protected POST_PLAN binding (see _generate_reference_image_via_gate).
    from hypogeum.production_plan import canonical_episode_dir as _canon_dir  # noqa: PLC0415
    from hypogeum.production_plan import load_production_plan as _load_canon_plan  # noqa: PLC0415
    _bound_plan = _load_canon_plan(episode_id, _canon_dir(episode_id))
    _plan_audit_metadata = (
        {"production_plan_content_hash": _bound_plan.content_hash} if _bound_plan is not None else {}
    )

    try:
        envelope = GenerationExecutionEnvelope.create(
            envelope_schema_version=_VIDEO_ENVELOPE_SCHEMA_VERSION, envelope_version=1, channel_id=channel_id,
            channel_package_version=package_identity["version"], channel_package_checksum=package_identity["checksum"],
            execution_id=episode_id, workflow_version=workflow_binding["workflow_version"],
            plan_version="live", plan_checksum=plan_checksum, planned_shot_id=f"thumb_{deliverable_id}",
            structured_spec_checksum=structured_spec_checksum, compiler_identity=_THUMBNAIL_COMPILER_IDENTITY,
            compiler_version="1", operation_payload_content=operation_payload_content,
            operation_payload_sha256=operation_payload_sha256, operation_payload_schema_version=_THUMBNAIL_SCHEMA_VERSION,
            content_provenance={
                "compiler": _THUMBNAIL_COMPILER_IDENTITY, "compiler_version": "1",
                "source_work_item_id": f"thumb_{deliverable_id}", "rendered_at": at,
            },
            provider=_VIDEO_PROVIDER, model=model, provider_operation=_THUMBNAIL_PROVIDER_OPERATION,
            deterministic_output_path=str(local_path), expected_output_identity=expected_output_identity,
            authorization_id=authorization_id, authorization_version=1, max_cost_per_call=max_cost,
            run_level_budget_ref=episode_id, episode_level_budget_ref=episode_id, currency="USD",
            retry_authorized=False, fallback_authorized=False, idempotency_key=authorization_id,
            created_at=at, expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
            dependency_checksums={
                "channel_package": package_identity["checksum"], "plan": plan_checksum,
                "structured_spec": structured_spec_checksum, "compiler": hashlib.sha256(_THUMBNAIL_COMPILER_IDENTITY.encode()).hexdigest(),
            },
            audit_metadata=_plan_audit_metadata,
            aspect_ratio=aspect_ratio,
        )
        authorization = authority.issue_child_authorization(envelope, grant.grant_id, grant.grant_version, at=at)
    except Exception as exc:  # noqa: BLE001 -- any compile/authorize failure here is a clean non-network denial
        raise MUAPIError(f"Thumbnail gate authorization failed for {deliverable_id!r}: {exc}") from exc

    conn = db.get_connection()
    _held_lease: Optional[tuple] = None
    try:
        authorization_store.persist_execution_envelope(conn, envelope)

        adapter_registry.register_adapter(_VIDEO_PROVIDER, model, _THUMBNAIL_PROVIDER_OPERATION, "1.0", MuapiThumbnailAdapter)
        lease_owner_id = f"muapi_thumbnail_adapter:{deliverable_id}"
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        lease = db.acquire_lease(conn, envelope.envelope_id, lease_owner_id, acquired_at=at, expires_at=lease_expires_at)
        if lease is None:
            raise MUAPIError(f"Thumbnail gate lease unavailable for {deliverable_id!r}")
        _held_lease = (envelope.envelope_id, lease_owner_id, lease["fencing_token"])

        receipt_store = WriteAheadReceiptStore(output_dir)
        live_sources = submission_gate.LiveSources(
            channel_package_checksum=package_identity["checksum"], plan_checksum=plan_checksum,
            structured_spec_checksum=structured_spec_checksum, compiler_version="1",
        )
        scopes = [
            bs.ScopeRequest(bs.BudgetScopeKind.CALL, f"{episode_id}:thumb_{deliverable_id}", max_cost, "USD"),
            bs.ScopeRequest(bs.BudgetScopeKind.EPISODE, episode_id, grant.max_total_spend_all_bindings or max_cost, "USD"),
        ]
        result = submission_gate.submit(
            envelope, live_sources=live_sources, authorization=authorization, lease_conn=conn,
            lease_owner_id=lease_owner_id, lease_fencing_token=lease["fencing_token"], receipt_store=receipt_store,
            execution_id=episode_id, attempt_id=authorization_id, budget_scopes=scopes,
            required_provider=_VIDEO_PROVIDER, required_model=model, at=at,
            expected_aspect_ratio=aspect_ratio, expected_output_identity=expected_output_identity,
            expected_deterministic_output_path=str(local_path), output_already_exists=local_path.exists(),
        )
    finally:
        if _held_lease is not None:
            try:
                db.release_lease(
                    conn, _held_lease[0], _held_lease[1], _held_lease[2],
                    released_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception:  # noqa: BLE001
                pass
        conn.close()

    if not result.approved:
        raise MUAPIError(f"Thumbnail gate denied for {deliverable_id!r}: [{result.denied_at_check}] {result.reason}")


def _generate_thumbnail_with_model(
    model: str,
    deliverable_id: str,
    image_prompt: str,
    aspect_ratio: str,
    config: Config,
    local_path: Path,
    budget_guard: Optional[MUAPIBudgetGuard],
    log,
) -> None:
    """
    Generate a thumbnail using the specified model and save it to local_path.

    Handles two model families:
      GPT Image 2 (_GPT_IMAGE_2_MODELS): sends resolution + quality params;
        extracts URL from result["image_url"] (flat string field).
      Flux / other image models: sends prompt + aspect_ratio only;
        extracts URL from result["outputs"][0].

    Raises MUAPIError on any failure (submission, polling, download, file size).
    """
    endpoint = f"{config.MUAPI_BASE_URL}/{model}"

    if model in _GPT_IMAGE_2_MODELS:
        payload: dict = {
            "prompt": image_prompt,
            "aspect_ratio": aspect_ratio,
            "resolution": config.THUMBNAIL_IMAGE_RESOLUTION,
            "quality": config.THUMBNAIL_IMAGE_QUALITY,
        }
    else:
        payload = {
            "prompt": image_prompt,
            "aspect_ratio": aspect_ratio,
        }

    # Budget check before paid submission (skipped in dry-run)
    if budget_guard is not None and not config.MUAPI_DRY_RUN:
        budget_guard.check_and_record_attempt(deliverable_id, model)

    log.info(
        "muapi_thumbnail_submit",
        deliverable_id=deliverable_id,
        model=model,
        aspect_ratio=aspect_ratio,
    )

    request_id, failure_reason = _submit_http(endpoint, payload, config, deliverable_id, log)
    if request_id is None:
        raise MUAPIError(
            f"Thumbnail submission failed for {deliverable_id!r} "
            f"(model={model}): {failure_reason}"
        )

    # Dry-run: skip poll and download, same as the video path (submit_scene()
    # above). Without this check, a dry-run request_id (never actually
    # submitted to a real endpoint) gets polled anyway and 404s forever —
    # the exact bug _submit_http()'s own docstring warns callers to guard
    # against. Post-processing (_post_process(), called by our caller)
    # still needs a real image file on disk to normalize/overlay, so a
    # minimal placeholder is written to local_path here rather than
    # skipping local_path entirely.
    if request_id.startswith(_DRY_RUN_REQUEST_ID_PREFIX):
        target = _THUMBNAIL_TARGETS.get(aspect_ratio, (1280, 720))
        local_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", target, color=(20, 20, 20)).save(local_path, "JPEG", quality=90)
        log.info(
            "muapi_thumbnail_dry_run_complete",
            deliverable_id=deliverable_id,
            request_id=request_id,
            model=model,
        )
        return

    result, failure_reason = _poll_until_complete(request_id, config, deliverable_id, log)
    if result is None:
        raise MUAPIError(
            f"Thumbnail polling failed for {deliverable_id!r} "
            f"(model={model}): {failure_reason}"
        )

    # Extract image URL — all models return outputs[] array via MUAPI
    outputs = result.get("outputs", [])
    if not outputs:
        raise MUAPIError(
            f"Thumbnail result has no outputs[] for {deliverable_id!r} (model={model})"
        )
    image_url = outputs[0] if isinstance(outputs[0], str) else outputs[0].get("url", "")

    if not image_url:
        raise MUAPIError(
            f"Thumbnail image URL is empty for {deliverable_id!r} (model={model})"
        )

    local_path.parent.mkdir(parents=True, exist_ok=True)
    success, dl_reason = _download_file(image_url, local_path, log)
    if not success:
        success, dl_reason = _download_file(image_url, local_path, log)
        if not success:
            raise MUAPIError(
                f"Thumbnail download failed for {deliverable_id!r}: {dl_reason}"
            )

    if local_path.stat().st_size < _MIN_IMAGE_BYTES:
        raise MUAPIError(
            f"Thumbnail file is suspiciously small ({local_path.stat().st_size} bytes) "
            f"for {deliverable_id!r} (model={model})"
        )


def _attempt_thumbnail_generation(
    deliverable_id: str,
    image_prompt: str,
    aspect_ratio: str,
    primary_model: str,
    fallback_model: str,
    config: Config,
    output_dir: Path,
    episode_id: str,
    budget_guard: Optional[MUAPIBudgetGuard],
    text_overlay: Optional[dict],
    log,
    typography_plan: Optional[dict] = None,
    typography_integration: str = "composited",
    channel_id: Optional[str] = None,
    thumbnail_target_dims: Optional[tuple[int, int]] = None,
) -> tuple[str, bool]:
    """
    Attempt thumbnail generation with primary_model; fall back to fallback_model on failure.

    typography_integration controls the text compositing path:
      "embedded"   — image was generated with text inside it; PIL typography is skipped.
                     Only the wordmark overlay is applied via PIL.
      "composited" — PIL renders typography on top of the generated image (legacy path).

    Returns (model_used, used_fallback).
    Raises MUAPIError if all attempts fail.
    Raises MUAPIAuthFailure / MUAPIQuotaExhausted on halt conditions.

    channel_id (added for the Authorization Authority gate path, plan
    §5.5/§6 Commit F -- mirrors submit_scene()'s own channel_id
    parameter exactly, scoped to the muapi-provider branch only). See
    _generate_thumbnail_via_gate()'s own docstring.
    """
    local_path = output_dir / "thumbnails" / f"{deliverable_id}.jpg"
    integrated = (typography_integration == "embedded")

    def _generate_one(model: str) -> None:
        if channel_id is not None:
            grant, _admitted = _resolve_authorization_grant(
                channel_id, episode_id, at=datetime.now(timezone.utc).isoformat()
            )
            if _admitted and grant is None:
                raise MUAPIError(
                    f"Thumbnail {deliverable_id!r}: channel {channel_id!r} is admitted under a "
                    "Channel Operating Grant but none resolves -- fail closed, no legacy path"
                )
            if grant is not None:
                _generate_thumbnail_via_gate(
                    channel_id=channel_id, grant=grant, model=model, deliverable_id=deliverable_id,
                    image_prompt=image_prompt, aspect_ratio=aspect_ratio, episode_id=episode_id,
                    output_dir=output_dir, config=config, local_path=local_path, log=log,
                )
                return
        _generate_thumbnail_with_model(
            model=model, deliverable_id=deliverable_id, image_prompt=image_prompt,
            aspect_ratio=aspect_ratio, config=config, local_path=local_path,
            budget_guard=budget_guard, log=log,
        )

    def _post_process(model: str, used_fallback: bool) -> tuple[str, bool]:
        _normalize_thumbnail(local_path, aspect_ratio, target_dims=thumbnail_target_dims)
        if integrated:
            # Integrated typography mode: image contains text rendered by the model.
            # PIL compositor is disabled — trust the image. Wordmark only.
            log.info(
                "muapi_thumbnail_integrated_typography",
                deliverable_id=deliverable_id,
                note="PIL text compositor skipped — typography embedded in generated image",
            )
        elif typography_plan:
            # Composited mode: PIL renders stacked headline+emphasis on image surface.
            _apply_typography_plan(local_path, typography_plan)
            log.info(
                "muapi_thumbnail_typography_plan_applied",
                deliverable_id=deliverable_id,
                zone=typography_plan.get("zone"),
                headline=typography_plan.get("headline"),
                emphasis=typography_plan.get("emphasis"),
            )
        elif text_overlay:
            # Legacy mode: PIL renders single-line text on image surface.
            text = text_overlay.get("text", "")
            placement = text_overlay.get("placement", "bottom")
            _apply_text_overlay(local_path, text, placement)
            log.info(
                "muapi_thumbnail_text_overlay_applied",
                deliverable_id=deliverable_id,
                text=text,
                placement=placement,
            )
        # Wordmark always composited via PIL (TIG-07 requirement).
        if aspect_ratio == "16:9":
            hook_placement = None if integrated else (
                text_overlay.get("placement") if text_overlay else None
            )
            _apply_wordmark_overlay(local_path, hook_placement=hook_placement)
            log.info(
                "muapi_thumbnail_wordmark_applied",
                deliverable_id=deliverable_id,
            )
        log.info(
            "muapi_thumbnail_generated",
            deliverable_id=deliverable_id,
            model=model,
            provider="muapi",
            used_fallback=used_fallback,
            aspect_ratio=aspect_ratio,
        )
        return model, used_fallback

    # Primary attempt
    try:
        _generate_one(primary_model)
        return _post_process(primary_model, False)
    except (MUAPIAuthFailure, MUAPIQuotaExhausted, MUAPIBudgetExhausted):
        raise  # halt conditions propagate immediately
    except MUAPIError as exc:
        if not fallback_model:
            raise MUAPIError(
                f"Thumbnail generation failed for {deliverable_id!r} "
                f"(primary={primary_model}, no fallback configured): {exc}"
            ) from exc
        log.warn(
            "muapi_thumbnail_fallback_triggered",
            deliverable_id=deliverable_id,
            primary_model=primary_model,
            fallback_model=fallback_model,
            reason=str(exc),
        )

    # Fallback attempt
    _generate_one(fallback_model)
    return _post_process(fallback_model, True)


# ---------------------------------------------------------------------------
# Internal: HTTP submission (with 1 retry on transient errors)
# ---------------------------------------------------------------------------

def _submit_http(
    endpoint: str,
    payload: dict,
    config: Config,
    scene_id: str,
    log,
) -> tuple[Optional[str], Optional[str]]:
    """
    POST to a MUAPI generation endpoint and return (request_id, None) on success
    or (None, failure_reason) on failure.

    Retries once on 429/500/503. Raises on 401/402.

    M5.6 — Dry-run mode: if MUAPI_DRY_RUN is true, returns a fake request_id immediately
    without making any network call. The fake id is prefixed with "dry-run-" so callers
    can detect it and skip the poll/download steps.
    """
    # M5.6 — Dry-run: no real API call
    if config.MUAPI_DRY_RUN:
        fake_id = f"{_DRY_RUN_REQUEST_ID_PREFIX}{scene_id}-{int(time.monotonic() * 1000) % 1_000_000}"
        log.info(
            "muapi_dry_run_submission",
            scene_id=scene_id,
            fake_request_id=fake_id,
            endpoint=endpoint,
        )
        return fake_id, None

    for http_try in range(_RETRY_MAX_HTTP + 1):
        retry_after = None

        try:
            resp = requests.post(
                endpoint,
                headers=_get_headers(config),
                json=payload,
                timeout=30,
            )
        except requests.exceptions.Timeout:
            if http_try < _RETRY_MAX_HTTP:
                delay = _retry_delay(http_try, None)
                log.warn(
                    "muapi_submission_timeout_retry",
                    scene_id=scene_id,
                    retry_delay_seconds=round(delay, 2),
                )
                time.sleep(delay)
                continue
            return None, "submission_timeout"
        except requests.exceptions.ConnectionError as exc:
            if http_try < _RETRY_MAX_HTTP:
                delay = _retry_delay(http_try, None)
                log.warn(
                    "muapi_submission_connection_error_retry",
                    scene_id=scene_id,
                    error=str(exc),
                    retry_delay_seconds=round(delay, 2),
                )
                time.sleep(delay)
                continue
            return None, "connection_error"

        # Halt failures — 401 raises immediately (no retry can fix a bad
        # API key). 402 gets a bounded, delayed retry first -- a real
        # provider account balance snapshot alone must never halt
        # generation; see _retry_muapi_quota_exhausted()'s own docstring.
        if resp.status_code == 401:
            log.error("muapi_auth_failure", scene_id=scene_id, http_status=401)
            raise MUAPIAuthFailure()
        if resp.status_code == 402:
            quota_attempt = 0
            while resp.status_code == 402 and _retry_muapi_quota_exhausted(quota_attempt, config, log, scene_id):
                quota_attempt += 1
                resp = requests.post(endpoint, headers=_get_headers(config), json=payload, timeout=30)
            if resp.status_code == 402:
                raise MUAPIQuotaExhausted()
            if resp.status_code == 401:
                log.error("muapi_auth_failure", scene_id=scene_id, http_status=401)
                raise MUAPIAuthFailure()
            # else: a retry succeeded with a real, different status --
            # falls through to the normal handling below, exactly as if
            # this had been the response from the start.

        # Soft retry conditions
        if resp.status_code in _SOFT_RETRY_CODES:
            if resp.status_code == 429:
                try:
                    retry_after = float(
                        resp.headers.get("Retry-After", _RETRY_AFTER_DEFAULT)
                    )
                except (TypeError, ValueError):
                    retry_after = _RETRY_AFTER_DEFAULT
                log.warn(
                    "muapi_rate_limit_hit",
                    scene_id=scene_id,
                    retry_after_seconds=retry_after,
                )
            if http_try < _RETRY_MAX_HTTP:
                delay = _retry_delay(http_try, retry_after)
                log.warn(
                    "muapi_submission_soft_failure_retry",
                    scene_id=scene_id,
                    http_status=resp.status_code,
                    retry_delay_seconds=round(delay, 2),
                )
                time.sleep(delay)
                continue
            return None, f"http_{resp.status_code}"

        # Hard failures for this attempt — no retry within this attempt, but Attempt 2 possible
        if resp.status_code in _ATTEMPT_HARD_FAIL_CODES:
            # Carry the provider's own error body — a 400/422 almost always
            # names the exact bad/missing field (e.g. a model whose I2V
            # reference field is image_url, not images_list). Without it the
            # operator sees only "http_422" and cannot act.
            _body = (resp.text or "")[:400]
            log.error(
                "muapi_hard_failure",
                scene_id=scene_id,
                http_status=resp.status_code,
                error_type=f"http_{resp.status_code}",
                endpoint=endpoint,
                response_body=_body,
            )
            return None, f"http_{resp.status_code}: {_body}"

        # Success — any 200/202
        if resp.status_code in (200, 202):
            try:
                data = resp.json()
                request_id = data.get("request_id", "")
            except Exception:
                return None, "invalid_submission_response"

            if not request_id:
                return None, "missing_request_id"

            return request_id, None

        # Unexpected status
        return None, f"http_{resp.status_code}"

    return None, "submission_retries_exhausted"


# ---------------------------------------------------------------------------
# Internal: polling
# ---------------------------------------------------------------------------

def _poll_until_complete(
    request_id: str,
    config: Config,
    scene_id: str,
    log,
) -> tuple[Optional[dict], str]:
    """
    Poll GET /api/v1/predictions/{request_id}/result until the job reaches a
    terminal status.

    Returns (result_dict, "ok") on completion.
    Returns (None, failure_reason) on failure or timeout.

    Timeout is treated as a job failure (triggers Attempt 2 in the caller).
    """
    poll_url = f"{config.MUAPI_BASE_URL}/predictions/{request_id}/result"
    max_attempts = config.MUAPI_MAX_POLL_ATTEMPTS
    interval = config.MUAPI_POLL_INTERVAL_SECONDS
    consecutive_400s = 0  # M5.6 — HTTP 400 early abort counter

    for attempt in range(max_attempts):
        time.sleep(interval)

        try:
            resp = requests.get(
                poll_url,
                headers=_get_headers(config),
                timeout=30,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            log.warn(
                "muapi_poll_connection_error",
                scene_id=scene_id,
                request_id=request_id,
                poll_attempt=attempt + 1,
                error=str(exc),
            )
            continue  # retry on next interval

        if resp.status_code == 401:
            raise MUAPIAuthFailure()
        if resp.status_code == 402:
            quota_attempt = 0
            while resp.status_code == 402 and _retry_muapi_quota_exhausted(quota_attempt, config, log, scene_id):
                quota_attempt += 1
                resp = requests.get(poll_url, headers=_get_headers(config), timeout=30)
            if resp.status_code == 402:
                raise MUAPIQuotaExhausted()
            if resp.status_code == 401:
                raise MUAPIAuthFailure()
            # else: a retry succeeded with a real, different status --
            # falls through to the normal polling handling below.

        # M5.6 — HTTP 400 early abort: persistent 400s indicate a bad job state
        if resp.status_code == 400:
            consecutive_400s += 1
            log.warn(
                "muapi_poll_unexpected_status",
                scene_id=scene_id,
                request_id=request_id,
                http_status=resp.status_code,
                poll_attempt=attempt + 1,
                consecutive_400s=consecutive_400s,
                response_body=resp.text[:500],
            )
            # A 400 whose body carries a TERMINAL job status (e.g. MUAPI
            # returns {"detail":{"status":"failed","error":"Insufficient
            # credits..."}}) is a DEFINITIVE provider failure, not an
            # ambiguous transport error — surface it as job_status_<status>
            # (same contract as the 200-path terminal handling below) so the
            # adapter can cleanly reject it (releasing grant/budget capacity
            # that was never charged) instead of stranding it as
            # unknown-acceptance. The provider's own error text is carried
            # through so clip_repair_agent._is_billing_contradiction() can
            # still detect a systemic account/billing wall and stop the batch.
            try:
                _detail = resp.json().get("detail") or {}
                _term = str(_detail.get("status", "")).lower()
            except Exception:  # noqa: BLE001
                _detail, _term = {}, ""
            if _term in (_JOB_STATUS_FAILED, _JOB_STATUS_CANCELLED):
                log.warn(
                    "muapi_job_failed",
                    scene_id=scene_id, request_id=request_id,
                    job_status=_term, poll_attempt=attempt + 1, http_status=400,
                )
                _err = str(_detail.get("error", ""))[:200]
                return None, f"job_status_{_term}: {_err}" if _err else f"job_status_{_term}"
            if consecutive_400s >= _POLL_400_ABORT_THRESHOLD:
                log.warn(
                    "muapi_poll_400_early_abort",
                    scene_id=scene_id,
                    request_id=request_id,
                    consecutive_400s=consecutive_400s,
                    poll_attempt=attempt + 1,
                )
                # Carry the provider's own response text through the return
                # value, not just a generic code — callers that batch many
                # submissions (e.g. clip_repair_agent.fill_confirmed_missing_
                # shots) need the real reason to distinguish a systemic
                # account/billing rejection (which will recur on every
                # subsequent shot) from a shot-specific content rejection.
                return None, f"http_400_persistent: {resp.text[:300]}"
            continue

        if resp.status_code not in (200, 202):
            consecutive_400s = 0  # reset on non-400 unexpected status
            log.warn(
                "muapi_poll_unexpected_status",
                scene_id=scene_id,
                request_id=request_id,
                http_status=resp.status_code,
                poll_attempt=attempt + 1,
            )
            continue

        consecutive_400s = 0  # reset on successful poll response

        try:
            data = resp.json()
        except Exception:
            continue

        status = data.get("status", "")

        log.info(
            "muapi_poll_cycle",
            scene_id=scene_id,
            request_id=request_id,
            poll_attempt=attempt + 1,
            current_status=status,
        )

        if status == _JOB_STATUS_COMPLETED:
            output = data.get("output") or data
            log.info(
                "muapi_job_completed",
                scene_id=scene_id,
                request_id=request_id,
                poll_attempts=attempt + 1,
            )
            return output, "ok"

        if status in (_JOB_STATUS_FAILED, _JOB_STATUS_CANCELLED):
            log.warn(
                "muapi_job_failed",
                scene_id=scene_id,
                request_id=request_id,
                job_status=status,
                poll_attempts=attempt + 1,
            )
            return None, f"job_status_{status}"

        # Still in progress (queued / pending / processing) — continue polling

    # Timeout: poll_attempts > max
    log.warn(
        "muapi_timeout",
        scene_id=scene_id,
        request_id=request_id,
        poll_attempts=max_attempts,
        timeout_seconds=max_attempts * interval,
    )
    return None, "timeout"


# ---------------------------------------------------------------------------
# Internal: file download
# ---------------------------------------------------------------------------

def _download_file(
    url: str,
    dest_path: Path,
    log,
) -> tuple[bool, str]:
    """
    Download a file from url to dest_path. Returns (True, "") on success.
    Returns (False, reason) on failure. Never logs the URL (may be signed).
    Validates that the HTTPS URL is used before downloading.
    """
    if not url.lower().startswith("https://"):
        return False, "non_https_url"

    try:
        resp = requests.get(url, timeout=120, stream=True)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        return False, f"download_connection_error: {exc}"

    if resp.status_code != 200:
        return False, f"download_http_{resp.status_code}"

    try:
        with dest_path.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
    except OSError as exc:
        return False, f"file_write_error: {exc}"

    return True, ""


# ---------------------------------------------------------------------------
# Internal: video file validation
# ---------------------------------------------------------------------------

def _validate_video_file(
    path: Path,
    requested_duration_seconds: float,
) -> tuple[bool, str]:
    """
    Validate a downloaded video file.

    Checks:
    1. File size > MIN_VIDEO_BYTES
    2. File starts with MP4 container signature (ftyp box present in first 12 bytes)

    Duration validation is omitted in V1 — requires ffprobe or equivalent media library.
    A WARNING is logged by the caller if duration cannot be verified.
    """
    if not path.exists():
        return False, "file_does_not_exist"

    size = path.stat().st_size
    if size < _MIN_VIDEO_BYTES:
        return False, f"file_too_small: {size} bytes"

    # Check for MP4 ftyp box: bytes 4-7 should be b'ftyp'
    try:
        with path.open("rb") as fh:
            header = fh.read(12)
        if len(header) >= 8 and header[4:8] != b"ftyp":
            # Some MP4s have a moov box first — also acceptable; skip strict check
            # and rely on size check as primary guard
            pass
    except OSError:
        return False, "file_read_error"

    return True, ""


# ---------------------------------------------------------------------------
# Internal: file upload to MUAPI storage
# ---------------------------------------------------------------------------

def _upload_file(
    local_path: Path,
    config: Config,
    log,
) -> str:
    """
    Upload a local file to MUAPI storage and return the hosted URL.

    Used for reference images that must be accessible as a URL for I2V
    images_list parameter. The uploaded URL is stable for the duration of
    a generation session.

    Returns the hosted URL string. Raises MUAPIError on failure.
    """
    upload_url = f"{config.MUAPI_BASE_URL}/{_UPLOAD_PATH}"

    def _do_upload():
        with local_path.open("rb") as fh:
            # Do NOT pass Content-Type here — requests sets it automatically to
            # multipart/form-data with boundary when `files` is present.
            return requests.post(
                upload_url,
                headers={"x-api-key": config.MUAPI_API_KEY},
                files={"file": (local_path.name, fh, "image/jpeg")},
                timeout=60,
            )

    try:
        resp = _do_upload()
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        raise MUAPIError(f"File upload failed: {exc}") from exc

    if resp.status_code == 401:
        raise MUAPIAuthFailure()
    if resp.status_code == 402:
        quota_attempt = 0
        while resp.status_code == 402 and _retry_muapi_quota_exhausted(quota_attempt, config, log, local_path.name):
            quota_attempt += 1
            try:
                resp = _do_upload()
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                raise MUAPIError(f"File upload failed: {exc}") from exc
        if resp.status_code == 402:
            raise MUAPIQuotaExhausted()
        if resp.status_code == 401:
            raise MUAPIAuthFailure()
        # else: a retry succeeded with a real, different status -- falls
        # through to the normal handling below.
    if resp.status_code not in (200, 201):
        raise MUAPIError(
            f"File upload returned HTTP {resp.status_code} for {local_path.name}"
        )

    try:
        data = resp.json()
        # Try common field names for the hosted URL
        hosted_url = (
            data.get("url")
            or data.get("file_url")
            or data.get("upload_url")
            or data.get("data", {}).get("url", "")
        )
    except Exception:
        raise MUAPIError("File upload response could not be parsed")

    if not hosted_url:
        raise MUAPIError(
            f"File upload response contains no URL field. "
            f"Keys received: {list(data.keys()) if isinstance(data, dict) else 'non-dict'}"
        )

    log.info(
        "muapi_upload_completed",
        file_name=local_path.name,
        hosted_url=_mask_url(hosted_url),
    )
    return hosted_url


# ---------------------------------------------------------------------------
# Internal: helpers
# ---------------------------------------------------------------------------

def _get_headers(config: Config) -> dict:
    """Build request headers. API key is never logged."""
    return {
        "x-api-key": config.MUAPI_API_KEY,
        "Content-Type": "application/json",
    }


def _tier_to_model(tier: int, reference_image_url: Optional[str], config: Config) -> str:
    """
    Map a quality tier (1/2/3) and optional reference image to a MUAPI model slug.

    If reference_image_url is set, returns the I2V model regardless of tier
    (reference image conditioning always uses the I2V endpoint).
    """
    if reference_image_url:
        return config.MUAPI_I2V_MODEL
    if tier == 1:
        return config.MUAPI_T1_MODEL
    if tier == 3:
        return config.MUAPI_T3_MODEL
    return config.MUAPI_T2_MODEL  # default and tier 2


def _resolve_scene_model(
    tier: int,
    reference_image_url: Optional[str],
    config: Config,
    channel_id: Optional[str] = None,
) -> str:
    """The model slug for one scene, honouring a channel's adopted per-channel
    video model (hypogeum.channel_config.ModelProfile.video_model, set via the
    Video Model bake-off) when one exists.

    Resolution order:
      1. reference_image_url set -> an I2V model (always; conditioning path):
         the channel's adopted video_i2v_model when it has one, else the
         global config.MUAPI_I2V_MODEL.
      2. A global cost/safety mode is active (SMOKE_TEST_MODE / MUAPI_CHEAP_MODE
         / MUAPI_RUNWAY_ONLY_MODE) -> _tier_to_model(). These explicit operator
         overrides win over a channel preference.
      3. channel_id given and that channel has an adopted video_model -> it.
      4. Otherwise -> _tier_to_model() (the global MUAPI_T{1,2,3}_MODEL config).

    Both the Production Plan compiler (hypogeum.production_plan) and the paid
    submission gate (_submit_scene_via_gate) call this, so the plan declares
    and the submission produces the SAME model -- the plan's model-match check
    stays satisfied.
    """
    if reference_image_url:
        # A channel that adopted its own I2V model (ModelProfile.video_i2v_model
        # — e.g. runway-image-to-video @ $0.15) uses it instead of the global
        # config.MUAPI_I2V_MODEL (veo3-image-to-video @ $2.50), so a cheap-T2V
        # channel is not forced to 40x cost on every entity-consistency scene.
        if channel_id:
            try:
                from hypogeum.channel_config import resolve_channel_i2v_model
                i2v_override, _i2v_price = resolve_channel_i2v_model(channel_id)
                if i2v_override:
                    return i2v_override
            except Exception:  # noqa: BLE001
                pass
        return config.MUAPI_I2V_MODEL
    if (getattr(config, "SMOKE_TEST_MODE", False)
            or getattr(config, "MUAPI_CHEAP_MODE", False)
            or getattr(config, "MUAPI_RUNWAY_ONLY_MODE", False)):
        return _tier_to_model(tier, reference_image_url, config)
    if channel_id:
        try:
            from hypogeum.channel_config import resolve_channel_video_model
            override, _price = resolve_channel_video_model(channel_id)
            if override:
                return override
        except Exception:  # noqa: BLE001
            pass
    return _tier_to_model(tier, reference_image_url, config)


def _resolve_gate_unit_price(
    model: str, channel_id: Optional[str], episode_id: str,
) -> float:
    """The per-call price ceiling for `model`, resolved the SAME way
    hypogeum.production_plan.resolve_video_price() prices it for the compiled
    Production Plan: the static MODEL_CREDIT_COSTS table first, then a
    channel's adopted-model price (Video Model bake-off, persisted on the
    ModelProfile), then the synced model registry. Only when nothing
    authoritative is available does it fall back to the budget guard's
    conservative default -- so a bake-off model the plan priced from the live
    catalogue is not re-priced to the $2.50 unknown-model ceiling here and
    rejected as a plan_price_mismatch against its own authorized plan.
    """
    price = MODEL_CREDIT_COSTS.get(model)
    if price is not None:
        return float(price)
    if channel_id:
        try:
            from hypogeum.channel_config import (  # noqa: PLC0415
                resolve_channel_i2v_model, resolve_channel_video_model,
            )
            ch_model, ch_price = resolve_channel_video_model(channel_id)
            if ch_model and model == ch_model and ch_price and float(ch_price) > 0:
                return float(ch_price)
            i2v_model, i2v_price = resolve_channel_i2v_model(channel_id)
            if i2v_model and model == i2v_model and i2v_price and float(i2v_price) > 0:
                return float(i2v_price)
        except Exception:  # noqa: BLE001
            pass
    try:
        from hypogeum.model_registry import price_of  # noqa: PLC0415
        reg = price_of(model)
        if reg is not None and float(reg) > 0:
            return float(reg)
    except Exception:  # noqa: BLE001
        pass
    return float(_DEFAULT_UNKNOWN_MODEL_COST)


def get_requested_clip_duration_seconds(
    config: Config, channel_id: Optional[str] = None,
) -> float:
    """
    Return the clip duration a fresh generation request will actually
    receive from the model that will actually be used — the normalized
    provider-capability input Cinematic Director's shot-budget arithmetic
    needs (coverage_capacity_per_shot).

    `channel_id` (optional): when given and that channel has adopted a video
    model via the Video Model bake-off (hypogeum.channel_config.
    resolve_channel_video_model), that model — not the tier default — is what
    submit_scene() will actually use, so its own MEASURED duration
    (ModelProfile.video_model_duration_seconds, captured from real bake-off
    clips via ffprobe) is authoritative when present. A channel with no
    adopted model, or an adopted model with no measured duration on record
    yet, falls through to the family-membership defaults below unchanged —
    this is purely additive, never a behavior change for hypogeum or any
    channel that hasn't adopted a model.

    Reuses the same model-family membership sets _build_job_payload() uses
    for the real request, so the two can never silently disagree about what
    a given model actually delivers. Never branches on a specific model
    name outside this module — callers only ever get a float back.
    """
    model = _tier_to_model(config.MUAPI_DEFAULT_TIER, None, config)
    if channel_id:
        try:
            from hypogeum.channel_config import (  # noqa: PLC0415
                resolve_channel_video_duration, resolve_channel_video_model,
            )
            ch_model, _ch_price = resolve_channel_video_model(channel_id)
            if ch_model:
                model = ch_model
                ch_duration = resolve_channel_video_duration(channel_id)
                if ch_duration and ch_duration > 0:
                    return float(ch_duration)
        except Exception:  # noqa: BLE001
            pass
    if model in _VEO3_1_MODELS:
        return float(_VEO3_1_DEFAULT_DURATION)
    if model in _VEO3_MODELS:
        return 8.0
    if model in _RUNWAY_MODELS:
        # Runway's duration enum is 5 or 8, rounded from the requested
        # value; production always requests the standard 8s scene length
        # (production_spec.duration_seconds), which rounds to 8 — see
        # _build_job_payload()'s own Runway branch.
        return 8.0
    # Unregistered model family with no measured duration on record — the
    # conservative default. A channel that adopts such a model gets an
    # under-provisioned plan until its measured duration is captured (see
    # the bake-off "adopt" flow, which populates it automatically).
    return 8.0


def _build_job_payload(
    prompt: str,
    model: str,
    aspect_ratio: str,
    requested_duration: float,
    reference_image_url: Optional[str],
    video_resolution: str = _VEO3_1_DEFAULT_RESOLUTION,
) -> dict:
    """
    Build the request payload for a MUAPI generation job.

    Duration handling:
      - Veo3 models (legacy): no duration parameter (fixed 8s by provider)
      - Veo3.1 models: duration + resolution sent explicitly (confirmed 2026-07-06)
      - Runway models: duration = 5 or 8 (integer enum; rounded from requested)

    Resolution handling:
      - Veo3.1: resolution sent explicitly (default "1080p"; overridable via video_resolution)
      - All other models: resolution field omitted (provider determines from aspect_ratio)

    I2V conditioning — field names differ by provider:
      - Veo3 / Veo3.1 I2V:  images_list = [url]  (array)
      - Runway I2V:          image_url   = url    (single string)
      - Seedance I2V:        image_url   = url    (single string; 2026-09-01 live probe
                            of seedance-pro-i2v-fast — 422 without it, HTTP 200 with it)
      - Only included when reference_image_url is non-empty
    """
    payload: dict = {
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
    }

    if model in _VEO3_1_MODELS:
        # Veo3.1: explicitly send duration and resolution (1080p supported; $0.39/gen)
        payload["duration"] = _VEO3_1_DEFAULT_DURATION
        payload["resolution"] = video_resolution
        if reference_image_url:
            payload["images_list"] = [reference_image_url]

    elif model in _VEO3_MODELS:
        # Veo3 (legacy): fixed 8s, no duration param, no resolution param
        if reference_image_url:
            # I2V model: images_list takes array of URLs (max 1)
            payload["images_list"] = [reference_image_url]
        # duration intentionally omitted

    elif model in _RUNWAY_MODELS:
        # Runway: duration must be 5 or 8 (enum)
        duration = 5 if requested_duration <= 6.0 else 8
        payload["duration"] = duration
        if reference_image_url:
            # Runway I2V (runway-image-to-video): uses "image_url" (single string).
            # Confirmed 2026-06-06 via live probe — 422 body: {"loc": ["body","image_url"],
            # "msg": "Field required"}. NOTE: Veo3/Veo3.1 I2V uses "images_list" (array) — different.
            payload["image_url"] = reference_image_url

    elif "seedance" in model:
        # Seedance family (seedance-pro-t2v-fast, seedance-pro-i2v-fast,
        # seedance-lite-i2v, seedance-2-mini-*, ...). Substring-matched so a
        # channel can adopt any current Seedance variant via the bake-off
        # without this file changing. Duration is left to the provider default
        # (~5s, ffprobe-confirmed 2026-09-01). The I2V variants take the
        # reference image as image_url = "<url>" (single string) — NOT
        # images_list (array); confirmed 2026-09-01 by a live probe of
        # seedance-pro-i2v-fast (422 body: {"loc":["body","image_url"],
        # "msg":"Field required"}; image_url -> HTTP 200). Same field name as
        # Runway I2V, different from Veo3/Veo3.1 (images_list).
        if reference_image_url:
            payload["image_url"] = reference_image_url

    elif "wan" in model:
        # Wan family (wan2.7-text-to-video / wan2.7-image-to-video, and any
        # future wan2.x/wan3.x variant — substring-matched, same rationale
        # as the Seedance branch above). The I2V variant takes the
        # reference image as image_url = "<url>" (single string) — the
        # request had NO reference-image field at all before this branch
        # existed, confirmed 2026-09-04 by a live bake-off probe of
        # wan2.7-image-to-video (422 body: {"loc":["body","image_url"],
        # "msg":"Field required"}). Same field name as Runway/Seedance I2V,
        # different from Veo3/Veo3.1 (images_list).
        if reference_image_url:
            payload["image_url"] = reference_image_url

    # For image models: no duration or images_list
    return payload


def scene_file_path(output_dir: Path, scene_id: str, short_id: Optional[str]) -> Path:
    """
    Compute the destination path for a scene video file.

    The single canonical source of truth for this convention — every other
    module that needs to locate or predict a scene's video file (clip_repair_
    agent.py, source_enhancement.py, etc.) must call this function rather
    than reimplementing the "sh_{short_id}/sc_{scene_id}.mp4" pattern
    inline. A prior inline duplicate in clip_repair_agent.py silently
    diverged (missing the "sh_" prefix), causing real generated Shorts
    repair clips to be misreported as failed (2026-08-05 fix).
    """
    if short_id:
        return output_dir / "video" / f"sh_{short_id}" / f"sc_{scene_id}.mp4"
    return output_dir / "video" / f"sc_{scene_id}.mp4"


def _compute_asset_id(episode_id: str, scene_id: str, short_id: Optional[str]) -> str:
    """
    Compute the asset_id (relative path from .hypogeum-assets/, no extension).
    Example: episodes/ep_001/video/sc_s01b01
    """
    if short_id:
        return f"episodes/{episode_id}/video/sh_{short_id}/sc_{scene_id}"
    return f"episodes/{episode_id}/video/sc_{scene_id}"


def _mask_url(url: str) -> str:
    """Strip query parameters from a URL for safe logging (signed URLs contain secrets)."""
    if "?" in url:
        return url[: url.index("?")] + "?[masked]"
    return url


def _retry_delay(attempt: int, retry_after: Optional[float]) -> float:
    """
    Compute the delay before the next retry attempt.

    Exponential backoff with ±25% jitter (per spec §8).
    If retry_after is set (from 429 Retry-After header), uses the larger value.
    """
    base = min(_RETRY_INITIAL_DELAY * (_RETRY_MULTIPLIER ** attempt), _RETRY_MAX_DELAY)
    jitter = base * _RETRY_JITTER_FRACTION * (2.0 * random.random() - 1.0)
    delay = base + jitter
    if retry_after is not None:
        delay = max(delay, float(retry_after))
    return delay
