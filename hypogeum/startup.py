"""
hypogeum/startup.py — Pipeline startup health checks

Single entry point for pre-run readiness verification. Called once at the
top of the pipeline before any agent is invoked.

Checks performed (in order):
  1. ffmpeg availability — shutil.which("ffmpeg"); required by Assembly Agent
  2. YouTube OAuth token — exchange refresh token; skipped when creds not configured
  3. OpenRouter connectivity (GET /models — if unreachable, flag is set in openrouter module)
  4. Required model availability (Sonnet + Opus slugs must appear in model list)
  5. Fallback key presence (if OpenRouter is unreachable and flag is set, ANTHROPIC_API_KEY required)

Outcome classification:
  "healthy"    — all checks pass; OpenRouter reachable, both models confirmed present
  "degraded"   — OpenRouter reachable but models missing, OR OpenRouter unreachable but
                 Anthropic fallback key is configured (run can proceed with reduced confidence)
  "unavailable" — any hard blocker: ffmpeg missing, YouTube token invalid/expired,
                  OR OpenRouter unreachable AND no ANTHROPIC_API_KEY configured

Integration contracts:
  - All environment access goes through the Config object — never os.getenv()
  - All logging goes through hypogeum.logger.get_logger() — never print() or stdlib logging
  - OpenRouter connectivity check goes through hypogeum.openrouter.check_health()
  - Module run-state reset goes through hypogeum.openrouter.reset_run_state()
  - _use_anthropic_for_run flag is read directly from hypogeum.openrouter module state
  - YouTube token check imports refresh_youtube_token lazily — no upload endpoints called

Logging events emitted:
  startup_checks_started    — info  — before any check runs
  startup_checks_completed  — info  — healthy outcome
  startup_checks_completed  — warn  — degraded outcome
  startup_checks_failed     — error — unavailable outcome (emitted before raising)
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Tuple

import hypogeum.openrouter as _openrouter
from hypogeum.config import Config
from hypogeum.logger import get_logger
from hypogeum.openrouter import check_health, reset_run_state


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class StartupError(RuntimeError):
    """
    Raised when the pipeline cannot start because no viable API path exists.

    This happens when OpenRouter is unreachable AND no ANTHROPIC_API_KEY is
    configured. The attached report describes what was checked and why startup
    was aborted.
    """
    def __init__(self, message: str, report: "StartupReport") -> None:
        self.report = report
        super().__init__(message)


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------

@dataclass
class StartupReport:
    """
    Immutable summary of the startup health check results.

    Callers may inspect this to decide whether to proceed with reduced
    confidence (degraded) or to surface operator-facing diagnostics.

    Fields:
        overall_status:       "healthy" | "degraded" | "unavailable"
        openrouter_reachable: True when GET /models returned 200.
        models_verified:      True when both Sonnet and Opus slugs confirmed
                              in the OpenRouter model list.
        fallback_active:      True when _use_anthropic_for_run was set by
                              check_health(), meaning calls will use Anthropic
                              direct API for this run.
        fallback_configured:  True when ANTHROPIC_API_KEY is present in config.
        checked_at:           UTC ISO8601 timestamp of the check.
        warnings:             Human-readable operator warnings (non-fatal issues).
        errors:               Human-readable operator errors (fatal issues).
        ffmpeg_available:     True when ffmpeg binary found on PATH via shutil.which.
        youtube_token_valid:  True when OAuth token exchange succeeded, False when it
                              failed, None when YouTube credentials are not configured
                              (check was skipped).
    """
    overall_status: str
    openrouter_reachable: bool
    models_verified: bool
    fallback_active: bool
    fallback_configured: bool
    checked_at: str
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    ffmpeg_available: bool = True
    youtube_token_valid: Optional[bool] = None  # None = credentials not configured (not checked)


# ---------------------------------------------------------------------------
# Pre-flight check helpers
# ---------------------------------------------------------------------------

def _check_ffmpeg() -> bool:
    """
    Return True if the ffmpeg binary is available on PATH, False otherwise.

    Uses shutil.which so the result reflects the runtime PATH of the process —
    the same PATH that subprocess calls in AssemblyAgent will use.
    """
    return shutil.which("ffmpeg") is not None


def _check_youtube_oauth(config: Config) -> Tuple[bool, Optional[str]]:
    """
    Attempt a YouTube OAuth token exchange using the configured credentials.

    Imports refresh_youtube_token lazily so this module does not take a hard
    dependency on the publishing package at import time.

    Args:
        config: Config instance with YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET,
                and YOUTUBE_REFRESH_TOKEN already populated.

    Returns:
        (True, None) when the refresh token exchange succeeds.
        (False, reason) when the exchange fails; reason is a short diagnostic
        string suitable for inclusion in an operator error message.

    Never raises — all exceptions are caught and converted to (False, reason).
    Caller is responsible for confirming that all three credentials are present
    before invoking this function.
    """
    try:
        from hypogeum.publishing.youtube import refresh_youtube_token
        from hypogeum.publishing.retry import HardFailureError, SoftFailureError
    except ImportError as exc:
        return False, f"Could not import youtube publishing module: {exc}"

    try:
        refresh_youtube_token(config)
        return True, None
    except HardFailureError as exc:
        return False, str(exc)
    except SoftFailureError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Unexpected error during token exchange: {exc}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_startup_checks(
    config: Config,
    episode_id: Optional[str] = None,
) -> StartupReport:
    """
    Execute all startup health checks and return a StartupReport.

    This function must be called once before the first agent invocation in
    a pipeline run. It resets openrouter module state so that stale flags
    from a previous run do not influence the current run.

    Args:
        config:     Loaded Config instance. API keys and model slugs are read
                    from this object — never from os.getenv() directly.
        episode_id: Optional episode identifier for log correlation. If None,
                    logs are emitted without episode context.

    Returns:
        StartupReport with overall_status "healthy" or "degraded".

    Raises:
        StartupError: When overall_status would be "unavailable" — OpenRouter
                      is unreachable AND ANTHROPIC_API_KEY is not configured.
                      The report is attached as StartupError.report.
    """
    log = get_logger(episode_id=episode_id, agent="startup")
    checked_at = _utc_now()

    log.info("startup_checks_started", episode_id=episode_id)

    warnings: list = []
    errors: list = []

    # --- Check 1: ffmpeg availability ---
    # Required by Assembly Agent for all video rendering. Checked before any
    # network call so operators get a fast, local diagnosis first.
    ffmpeg_available = _check_ffmpeg()
    if not ffmpeg_available:
        errors.append(
            "ffmpeg is not installed or not on PATH. "
            "The Assembly Agent requires ffmpeg to render video. "
            "Install ffmpeg (e.g. brew install ffmpeg on macOS) and ensure it "
            "is available in your PATH, then retry."
        )

    # --- Check 2: YouTube OAuth token exchange ---
    # Only attempted when all three OAuth credentials are present. A partial
    # credential set (some keys set, others missing) is treated as unconfigured
    # and results in a warning rather than a hard failure.
    youtube_creds_configured = bool(
        config.YOUTUBE_CLIENT_ID
        and config.YOUTUBE_CLIENT_SECRET
        and config.YOUTUBE_REFRESH_TOKEN
    )
    youtube_token_valid: Optional[bool] = None
    if youtube_creds_configured:
        ok, yt_err = _check_youtube_oauth(config)
        youtube_token_valid = ok
        if not ok:
            errors.append(
                f"YouTube OAuth token refresh failed: {yt_err}. "
                "The refresh token may be expired or the OAuth credentials may be "
                "incorrect. Re-authorize the application at the Google OAuth consent "
                "screen to generate a new YOUTUBE_REFRESH_TOKEN, then retry."
            )
    elif any([config.YOUTUBE_CLIENT_ID, config.YOUTUBE_CLIENT_SECRET, config.YOUTUBE_REFRESH_TOKEN]):
        warnings.append(
            "YouTube credentials are partially configured "
            "(YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN must all "
            "be set). YouTube publishing will be unavailable for this run."
        )

    # --- Check 3: OpenRouter / Anthropic API path ---
    # Always reset before checking so stale state from a prior run does not
    # poison this run's flag.
    reset_run_state()

    # check_health() sets _use_anthropic_for_run if OpenRouter is unreachable.
    healthy = check_health(config, episode_id=episode_id, agent=None)

    # Read flag immediately after check_health() returns — it may have been set.
    fallback_active = _openrouter._use_anthropic_for_run
    fallback_configured = bool(config.ANTHROPIC_API_KEY)

    # Determine granular state from the check results.
    openrouter_reachable = not fallback_active  # flag only set on connection failure
    models_verified = healthy  # check_health() returns True only when both models found

    # --- Classify outcome ---
    # Hard errors from checks 1 and 2 override the API-path classification.
    if errors:
        overall_status = "unavailable"

    elif healthy:
        # OpenRouter reachable, both models confirmed — best case.
        overall_status = "healthy"

    elif not fallback_active:
        # check_health() returned False but did NOT set the flag:
        # OpenRouter is reachable but required models are missing from the list.
        overall_status = "degraded"
        warnings.append(
            "OpenRouter is reachable but one or both required models "
            f"({config.OPENROUTER_SONNET_MODEL_ID}, {config.OPENROUTER_OPUS_MODEL_ID}) "
            "were not found in the model list. Calls may fail at runtime."
        )

    elif fallback_configured:
        # OpenRouter unreachable, but Anthropic direct fallback key is present.
        # Run can proceed; calls will go direct to Anthropic.
        overall_status = "degraded"
        warnings.append(
            "OpenRouter is unreachable. All Claude calls for this run will be "
            "routed to the Anthropic direct API using ANTHROPIC_API_KEY."
        )

    else:
        # OpenRouter unreachable AND no fallback key — cannot run.
        overall_status = "unavailable"
        errors.append(
            "OpenRouter is unreachable and ANTHROPIC_API_KEY is not configured. "
            "There is no viable API path for Claude calls. "
            "Check your network connection and credentials, then retry."
        )

    report = StartupReport(
        overall_status=overall_status,
        openrouter_reachable=openrouter_reachable,
        models_verified=models_verified,
        fallback_active=fallback_active,
        fallback_configured=fallback_configured,
        checked_at=checked_at,
        warnings=warnings,
        errors=errors,
        ffmpeg_available=ffmpeg_available,
        youtube_token_valid=youtube_token_valid,
    )

    if overall_status == "unavailable":
        log.error(
            "startup_checks_failed",
            overall_status=overall_status,
            openrouter_reachable=openrouter_reachable,
            models_verified=models_verified,
            fallback_active=fallback_active,
            fallback_configured=fallback_configured,
            ffmpeg_available=ffmpeg_available,
            youtube_token_valid=youtube_token_valid,
            errors=errors,
        )
        # Emit OIS credential_failure signal for API connectivity failures.
        # Wrapped in broad except so OIS unavailability never blocks startup logging.
        if not openrouter_reachable and not fallback_configured:
            try:
                import hypogeum.ois as _ois
                _ois.emit_signal(
                    signal_type="credential_failure",
                    severity="CRITICAL",
                    message=f"API path unavailable: {errors[0]}",
                    channel_id=None,
                    source_system="startup",
                    blocking=True,
                    metadata={"errors": errors},
                    episode_id=episode_id,
                    config=config,
                )
            except Exception:
                pass
        raise StartupError(
            f"Pipeline startup aborted: {errors[0]}",
            report=report,
        )

    log_fn = log.warn if overall_status == "degraded" else log.info
    log_fn(
        "startup_checks_completed",
        overall_status=overall_status,
        openrouter_reachable=openrouter_reachable,
        models_verified=models_verified,
        fallback_active=fallback_active,
        fallback_configured=fallback_configured,
        ffmpeg_available=ffmpeg_available,
        youtube_token_valid=youtube_token_valid,
        warnings=warnings,
    )

    return report


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    """Return current UTC time as ISO8601 string with microsecond precision."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
