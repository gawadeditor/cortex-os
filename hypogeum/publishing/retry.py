"""
hypogeum/publishing/retry.py — Shared retry logic for all publishing platform clients

Three retry tiers:
  video_upload              — 1 retry, 10s initial, 3× multiplier, 120s max, ±20% jitter
  thumbnail_caption_publish — 1 retry, 5s fixed, no jitter
  poll                      — N attempts at fixed interval (15s Instagram, 10s TikTok)

Hard failures (no retry regardless of tier): 400, 401, 403, quotaExceeded,
container ERROR/EXPIRED, publish FAILED.

Soft failures (retry eligible): 429, 500, 503, network timeout.

Retry-After header is always honored on 429 responses.
"""

from __future__ import annotations

import random
import time
from typing import Callable, Optional, TypeVar

from hypogeum.logger import get_logger

_log = get_logger(agent="retry")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hard failure HTTP status codes — never retry regardless of tier
_HARD_FAILURE_CODES = frozenset({400, 401, 403})

# Soft failure HTTP status codes — retry eligible
_SOFT_FAILURE_CODES = frozenset({429, 500, 503})

# Video upload tier
_VIDEO_UPLOAD_MAX_RETRIES = 1
_VIDEO_UPLOAD_INITIAL_DELAY = 10.0   # seconds
_VIDEO_UPLOAD_MULTIPLIER = 3.0
_VIDEO_UPLOAD_MAX_DELAY = 120.0
_VIDEO_UPLOAD_JITTER = 0.20          # ±20%

# Thumbnail/caption/publish tier
_TCP_MAX_RETRIES = 1
_TCP_DELAY = 5.0                     # fixed, no jitter

# Poll tier defaults
_INSTAGRAM_POLL_INTERVAL = 15.0      # seconds
_TIKTOK_POLL_INTERVAL = 10.0         # seconds


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class HardFailureError(RuntimeError):
    """
    Raised when a platform API returns a hard failure that must not be retried.

    Attributes:
        status_code   — HTTP status code (or None for non-HTTP hard failures)
        error_code    — Platform-specific error code string (e.g. "quotaExceeded")
        message       — Human-readable description
    """
    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        error_code: Optional[str] = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(message)


class SoftFailureError(RuntimeError):
    """
    Raised when a platform API returns a soft failure that is retry eligible.

    Attributes:
        status_code   — HTTP status code (or None for network errors)
        retry_after   — Seconds to wait before retry (from Retry-After header, or None)
        message       — Human-readable description
    """
    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(message)


class PollTimeoutError(RuntimeError):
    """Raised when polling exhausts all attempts without reaching a terminal status."""


# ---------------------------------------------------------------------------
# Response classification
# ---------------------------------------------------------------------------

def classify_http_status(
    status_code: int,
    error_code: Optional[str] = None,
    retry_after: Optional[float] = None,
) -> None:
    """
    Classify an HTTP response status code and raise the appropriate exception.

    Args:
        status_code:  HTTP status code from the platform response.
        error_code:   Optional platform-specific error code (e.g. "quotaExceeded").
        retry_after:  Optional Retry-After header value in seconds (for 429).

    Raises:
        HardFailureError — for 400, 401, 403 (including quotaExceeded), or any
                           explicitly non-retryable error code.
        SoftFailureError — for 429, 500, 503.
        Nothing          — for 2xx (success).
    """
    if status_code in _HARD_FAILURE_CODES:
        raise HardFailureError(
            f"HTTP {status_code} — hard failure (no retry)",
            status_code=status_code,
            error_code=error_code,
        )
    if status_code in _SOFT_FAILURE_CODES:
        raise SoftFailureError(
            f"HTTP {status_code} — soft failure (retry eligible)",
            status_code=status_code,
            retry_after=retry_after,
        )


# ---------------------------------------------------------------------------
# Jitter helper
# ---------------------------------------------------------------------------

def _apply_jitter(delay: float, jitter_fraction: float) -> float:
    """Apply ±jitter_fraction random jitter to delay. Returns non-negative value."""
    delta = delay * jitter_fraction
    jittered = delay + random.uniform(-delta, delta)
    return max(0.0, jittered)


# ---------------------------------------------------------------------------
# Retry tiers
# ---------------------------------------------------------------------------

T = TypeVar("T")


def retry_video_upload(
    fn: Callable[[], T],
    episode_id: Optional[str] = None,
    platform: str = "unknown",
    _sleep: Callable[[float], None] = time.sleep,
) -> T:
    """
    Execute fn() with video upload retry policy.
    1 retry, 10s initial, 3× multiplier, 120s max, ±20% jitter.
    Hard failures propagate immediately without retry.

    Args:
        fn:         Callable that performs the upload. Must raise HardFailureError
                    or SoftFailureError on non-success.
        episode_id: For log correlation.
        platform:   Platform name for log correlation.
        _sleep:     Injectable sleep function (for testing).

    Returns:
        Return value of fn() on success.

    Raises:
        HardFailureError — on hard failure (first or retry attempt).
        SoftFailureError — if all retries exhausted on soft failure.
        Any other exception raised by fn().
    """
    log = get_logger(episode_id=episode_id, agent="retry")
    delay = _VIDEO_UPLOAD_INITIAL_DELAY

    for attempt in range(_VIDEO_UPLOAD_MAX_RETRIES + 1):
        try:
            return fn()
        except HardFailureError:
            raise
        except SoftFailureError as exc:
            if attempt >= _VIDEO_UPLOAD_MAX_RETRIES:
                log.error(
                    "retry_exhausted",
                    platform=platform,
                    tier="video_upload",
                    attempts=attempt + 1,
                    status_code=exc.status_code,
                )
                raise
            # Honor Retry-After if present
            actual_delay = exc.retry_after if exc.retry_after is not None else delay
            actual_delay = _apply_jitter(actual_delay, _VIDEO_UPLOAD_JITTER)
            actual_delay = min(actual_delay, _VIDEO_UPLOAD_MAX_DELAY)
            log.warn(
                "retry_triggered",
                platform=platform,
                tier="video_upload",
                attempt=attempt + 1,
                delay_seconds=round(actual_delay, 2),
                status_code=exc.status_code,
            )
            _sleep(actual_delay)
            delay = min(delay * _VIDEO_UPLOAD_MULTIPLIER, _VIDEO_UPLOAD_MAX_DELAY)

    # Should not reach here
    raise RuntimeError("retry_video_upload: unexpected exit from retry loop")


def retry_thumbnail_caption_publish(
    fn: Callable[[], T],
    episode_id: Optional[str] = None,
    platform: str = "unknown",
    operation: str = "unknown",
    _sleep: Callable[[float], None] = time.sleep,
) -> T:
    """
    Execute fn() with thumbnail/caption/publish retry policy.
    1 retry, 5s fixed delay, no jitter.
    Hard failures propagate immediately.

    Args:
        fn:         Callable that performs the operation.
        episode_id: For log correlation.
        platform:   Platform name for log correlation.
        operation:  Operation name (thumbnail/caption/publish) for log correlation.
        _sleep:     Injectable sleep function (for testing).

    Returns:
        Return value of fn() on success.

    Raises:
        HardFailureError — on hard failure.
        SoftFailureError — if retry exhausted.
    """
    log = get_logger(episode_id=episode_id, agent="retry")

    for attempt in range(_TCP_MAX_RETRIES + 1):
        try:
            return fn()
        except HardFailureError:
            raise
        except SoftFailureError as exc:
            if attempt >= _TCP_MAX_RETRIES:
                log.error(
                    "retry_exhausted",
                    platform=platform,
                    tier="thumbnail_caption_publish",
                    operation=operation,
                    attempts=attempt + 1,
                    status_code=exc.status_code,
                )
                raise
            actual_delay = exc.retry_after if exc.retry_after is not None else _TCP_DELAY
            log.warn(
                "retry_triggered",
                platform=platform,
                tier="thumbnail_caption_publish",
                operation=operation,
                attempt=attempt + 1,
                delay_seconds=actual_delay,
                status_code=exc.status_code,
            )
            _sleep(actual_delay)

    raise RuntimeError("retry_thumbnail_caption_publish: unexpected exit from retry loop")


def poll_with_interval(
    fn: Callable[[], Optional[str]],
    max_attempts: int,
    interval_seconds: float,
    platform: str = "unknown",
    episode_id: Optional[str] = None,
    terminal_statuses: tuple = ("FINISHED", "ERROR", "EXPIRED", "PUBLISH_COMPLETE", "FAILED"),
    _sleep: Callable[[float], None] = time.sleep,
) -> str:
    """
    Poll fn() up to max_attempts times at interval_seconds fixed interval.
    Returns the terminal status string when reached.

    Args:
        fn:               Callable that returns a status string or None if not ready.
        max_attempts:     Maximum number of poll attempts.
        interval_seconds: Fixed interval between polls.
        platform:         Platform name for log correlation.
        episode_id:       For log correlation.
        terminal_statuses: Status values that terminate polling.
        _sleep:           Injectable sleep function (for testing).

    Returns:
        Terminal status string.

    Raises:
        PollTimeoutError — max_attempts exhausted without terminal status.
        HardFailureError — if fn() raises HardFailureError.
    """
    log = get_logger(episode_id=episode_id, agent="retry")

    for attempt in range(1, max_attempts + 1):
        status = fn()
        log.info(
            "poll_attempt",
            platform=platform,
            attempt=attempt,
            max_attempts=max_attempts,
            status=status,
        )
        if status in terminal_statuses:
            return status
        if attempt < max_attempts:
            _sleep(interval_seconds)

    raise PollTimeoutError(
        f"{platform} poll exhausted {max_attempts} attempts without terminal status"
    )
