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
  Image  → config.MUAPI_IMAGE_MODEL (default: flux-dev-image,         ~$0.01/gen)

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

import random
import subprocess
import time
from dataclasses import dataclass, field
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
# 401, 402 → halt ALL generation (raise exception)
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
    HTTP 402 — Account quota exhausted or billing issue.

    This halts ALL MUAPI generation. Requires out-of-band resolution.
    Callers must issue a PDR: severity BLOCKING, type provider_quota_exhausted.
    """
    def __init__(self) -> None:
        super().__init__(
            "MUAPI quota exhausted (HTTP 402). "
            "Account billing issue requires resolution. All scene generation halted."
        )


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


def submit_scene(
    scene_spec: dict,
    config: Config,
    episode_id: str,
    output_dir: Path,
    *,
    episode_id_for_asset: Optional[str] = None,
    agent: Optional[str] = None,
    budget_guard: Optional[MUAPIBudgetGuard] = None,
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
    """
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
    primary_prompt = video_prompt_writer.assemble_video_scene_prompt(prod_spec["primary_prompt"])
    fallback_prompt = video_prompt_writer.assemble_video_scene_prompt(prod_spec["fallback_prompt"])

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
) -> ReferenceImageResult:
    """
    Generate a reference image for a final-locked entity (Path A).

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
    if video_aspect_ratio not in _VIDEO_TO_REF_ASPECT:
        raise AssetClassError(
            f"Unsupported video_aspect_ratio={video_aspect_ratio!r} for reference image. "
            f"Supported: {sorted(_VIDEO_TO_REF_ASPECT)}"
        )
    ref_aspect_ratio = _VIDEO_TO_REF_ASPECT[video_aspect_ratio]

    log = get_logger(episode_id=episode_id, agent=agent or "muapi")
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


def _normalize_thumbnail(local_path: Path, aspect_ratio: str) -> None:
    """
    Resize and center-crop a downloaded thumbnail to the canonical pixel dimensions
    for the given aspect ratio.

    Target sizes:
      "16:9" → 1280 × 720 (YouTube main episode thumbnail)
      "9:16" → 720 × 1280 (Shorts / vertical thumbnail)

    Algorithm: scale-to-overfill then center-crop — no letterbox, no pillarbox.
    The image fills the target frame completely; a small amount of source content
    may be cropped from two sides.

    No-ops:
      - aspect_ratio not in _THUMBNAIL_TARGETS → returns immediately (no change).
      - Image already exactly at target dimensions → returns immediately (no re-encode).

    Overwrites local_path in place with JPEG quality=95.
    """
    target = _THUMBNAIL_TARGETS.get(aspect_ratio)
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
) -> ThumbnailResult:
    """
    Generate a thumbnail image for one deliverable (main episode or a Short).

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
) -> "ThumbnailResult":
    """
    Try one thumbnail provider. Returns ThumbnailResult on success.
    Raises on any failure — caller decides whether to try the next provider.
    MUAPIBudgetExhausted is always re-raised by the caller without retrying.
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
) -> tuple[Optional[dict], Optional[str]]:
    """
    Execute one generation attempt: submit → poll → download → validate.

    Returns (manifest_entry, None) on success.
    Returns (None, failure_reason) on failure.

    Raises MUAPIAuthFailure / MUAPIQuotaExhausted on halt conditions.
    Raises MUAPIBudgetExhausted if budget_guard blocks this attempt (M5.6).
    """
    model = _tier_to_model(tier, reference_image_url, config)
    endpoint = f"{config.MUAPI_BASE_URL}/{model}"
    payload = _build_job_payload(
        prompt, model, aspect_ratio, requested_duration, reference_image_url,
        video_resolution=config.MUAPI_VIDEO_RESOLUTION,
    )

    # M5.6 — Budget check before paid submission (skipped in dry-run — no real cost)
    if budget_guard is not None and not config.MUAPI_DRY_RUN:
        budget_guard.check_and_record_attempt(scene_id, model)

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
    }, None


# ---------------------------------------------------------------------------
# Internal: thumbnail generation helpers
# ---------------------------------------------------------------------------

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
    """
    local_path = output_dir / "thumbnails" / f"{deliverable_id}.jpg"
    integrated = (typography_integration == "embedded")

    def _post_process(model: str, used_fallback: bool) -> tuple[str, bool]:
        _normalize_thumbnail(local_path, aspect_ratio)
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
        _generate_thumbnail_with_model(
            model=primary_model,
            deliverable_id=deliverable_id,
            image_prompt=image_prompt,
            aspect_ratio=aspect_ratio,
            config=config,
            local_path=local_path,
            budget_guard=budget_guard,
            log=log,
        )
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
    _generate_thumbnail_with_model(
        model=fallback_model,
        deliverable_id=deliverable_id,
        image_prompt=image_prompt,
        aspect_ratio=aspect_ratio,
        config=config,
        local_path=local_path,
        budget_guard=budget_guard,
        log=log,
    )
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

        # Halt failures — raise immediately (propagates through all callers)
        if resp.status_code == 401:
            log.error("muapi_auth_failure", scene_id=scene_id, http_status=401)
            raise MUAPIAuthFailure()
        if resp.status_code == 402:
            log.error("muapi_quota_exhausted", scene_id=scene_id, http_status=402)
            raise MUAPIQuotaExhausted()

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
            log.error(
                "muapi_hard_failure",
                scene_id=scene_id,
                http_status=resp.status_code,
                error_type=f"http_{resp.status_code}",
            )
            return None, f"http_{resp.status_code}"

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
            raise MUAPIQuotaExhausted()

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
            if consecutive_400s >= _POLL_400_ABORT_THRESHOLD:
                log.warn(
                    "muapi_poll_400_early_abort",
                    scene_id=scene_id,
                    request_id=request_id,
                    consecutive_400s=consecutive_400s,
                    poll_attempt=attempt + 1,
                )
                return None, "http_400_persistent"
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

    try:
        with local_path.open("rb") as fh:
            # Do NOT pass Content-Type here — requests sets it automatically to
            # multipart/form-data with boundary when `files` is present.
            resp = requests.post(
                upload_url,
                headers={"x-api-key": config.MUAPI_API_KEY},
                files={"file": (local_path.name, fh, "image/jpeg")},
                timeout=60,
            )
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        raise MUAPIError(f"File upload failed: {exc}") from exc

    if resp.status_code == 401:
        raise MUAPIAuthFailure()
    if resp.status_code == 402:
        raise MUAPIQuotaExhausted()
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
      - Veo3 / Veo3.1 I2V: images_list = [url]  (array)
      - Runway I2V:         image_url   = url    (single string; confirmed 2026-06-06 live probe:
                            422 body {"loc":["body","image_url"],"msg":"Field required"})
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
