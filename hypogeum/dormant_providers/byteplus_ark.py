"""
hypogeum/byteplus_ark.py — BytePlus ModelArk direct provider client (Phase 1H)

Implements Seedance 2.0 video generation via the official BytePlus ModelArk API.
This replaces the seedance_official placeholder with real direct provider access.

API surface (BytePlus ModelArk, confirmed by official docs):
  Auth:    Authorization: Bearer {BYTEPLUS_ARK_API_KEY}
  Base:    https://ark.ap-southeast.bytepluses.com/api/v3   (international endpoint)
           Config key BYTEPLUS_ARK_BASE_URL overrides if using regional endpoint.
  Create:  POST /videos/generations
  Retrieve: GET  /videos/generations/{task_id}
  Models:  GET  /models   (used for credential probe — lightweight, no generation cost)

Task lifecycle:
  submitted → processing → succeeded
                        → failed

Video generation input:
  model:   BYTEPLUS_ARK_MODEL (default: "seedance-1-lite")
  content: [{"type": "text", "text": "<prompt>"}]
           or [{"type": "text", "text": "<prompt>"},
               {"type": "image_url", "image_url": {"url": "<url>"}}]  for I2V
  parameters:
    resolution: "1920x1080" | "1280x720" | "960x960" | "720x1280"
    duration:   5 | 8 (seconds — provider enforces supported values)
    seed:       optional int

Task result (succeeded):
  id:       task_id
  status:   "succeeded"
  content:  [{"type": "video_url", "video_url": {"url": "<signed_url>"}}]

Integration contracts:
  - Credentials resolved via Config.BYTEPLUS_ARK_API_KEY — never os.getenv()
  - All logging via get_logger() — never print()
  - Task polling: 5s initial, 10s steady, max 5min total
  - Max 1 HTTP retry on 429/5xx transient errors

Public API:
  probe_credential(key) -> (success: bool, error: Optional[str], latency_ms: int)
    Lightweight models-list call to validate the API key.

  submit_video_task(prompt, config, episode_id, *, aspect_ratio, duration_seconds,
                    reference_image_url, agent) -> str
    Create a video generation task. Returns task_id.

  get_task_result(task_id, config, episode_id, *, agent) -> dict
    Retrieve task status. Returns raw API response dict.

  poll_until_complete(task_id, config, episode_id, *, agent, timeout_seconds) -> dict
    Poll until succeeded/failed or timeout. Returns final result dict.

Errors:
  BytePlusArkAuthFailure    — HTTP 401 (bad key)
  BytePlusArkQuotaExhausted — HTTP 402 (billing issue)
  BytePlusArkError          — base for all other errors
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Optional

_DEFAULT_BASE_URL = "https://ark.ap-southeast.bytepluses.com/api/v3"

# Mapping from display aspect ratio to provider resolution string
_ASPECT_TO_RESOLUTION: dict[str, str] = {
    "16:9": "1280x720",
    "9:16": "720x1280",
    "1:1":  "960x960",
}

# Supported duration values (seconds); provider enforces these
_VALID_DURATIONS = frozenset({5, 8})

# Terminal task statuses
_STATUS_SUCCEEDED = "succeeded"
_STATUS_FAILED    = "failed"
_TERMINAL_STATUSES = frozenset({_STATUS_SUCCEEDED, _STATUS_FAILED})

# Poll schedule: start at 5s, then steady at 10s
_POLL_INITIAL_WAIT = 5.0
_POLL_STEADY_WAIT  = 10.0

# Default timeout
_DEFAULT_TIMEOUT_SECONDS = 300.0

# HTTP retry policy
_HTTP_RETRY_MAX   = 1
_HTTP_RETRY_DELAY = 5.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BytePlusArkError(RuntimeError):
    """Base for all BytePlus ModelArk client errors."""


class BytePlusArkAuthFailure(BytePlusArkError):
    """HTTP 401 — API key missing or invalid."""
    def __init__(self) -> None:
        super().__init__(
            "BytePlus ModelArk authentication failed (HTTP 401). "
            "Check BYTEPLUS_ARK_API_KEY."
        )


class BytePlusArkQuotaExhausted(BytePlusArkError):
    """HTTP 402 — Account quota exhausted or billing issue."""
    def __init__(self) -> None:
        super().__init__(
            "BytePlus ModelArk quota exhausted (HTTP 402). "
            "Check account credits and billing at BytePlus console."
        )


# ---------------------------------------------------------------------------
# Credential probe (used by provider_verifier.py)
# ---------------------------------------------------------------------------

def probe_credential(key: str) -> tuple[bool, Optional[str], int]:
    """
    Validate a BytePlus ModelArk API key with a lightweight models-list call.

    Returns (success, error_message, latency_ms).
    Does not raise — all errors reflected in the return value.
    """
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            f"{_DEFAULT_BASE_URL}/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status == 200:
                return True, None, latency_ms
            return False, f"HTTP {resp.status}", latency_ms
    except urllib.error.HTTPError as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        if exc.code == 401:
            return False, "HTTP 401 — invalid API key", latency_ms
        if exc.code == 402:
            return False, "HTTP 402 — quota or billing issue", latency_ms
        return False, f"HTTP {exc.code}", latency_ms
    except Exception as exc:
        return False, str(exc)[:120], int((time.monotonic() - t0) * 1000)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_base_url(config) -> str:
    return getattr(config, "BYTEPLUS_ARK_BASE_URL", None) or _DEFAULT_BASE_URL


def _get_api_key(config) -> str:
    return getattr(config, "BYTEPLUS_ARK_API_KEY", None) or ""


def _get_model(config) -> str:
    return getattr(config, "BYTEPLUS_ARK_MODEL", None) or "seedance-1-lite"


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }


def _round_duration(requested: float) -> int:
    """Round requested duration to nearest supported value (5 or 8)."""
    return 5 if requested <= 6 else 8


def _resolution_for_aspect(aspect_ratio: str) -> str:
    return _ASPECT_TO_RESOLUTION.get(aspect_ratio, "1280x720")


def _http_post(url: str, payload: bytes, api_key: str, timeout: float = 60.0) -> dict:
    """
    POST JSON payload to url. Returns parsed response dict.

    Raises BytePlusArkAuthFailure on 401, BytePlusArkQuotaExhausted on 402,
    BytePlusArkError on other HTTP failures.
    """
    import json as _json
    req = urllib.request.Request(url, data=payload, headers=_headers(api_key), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:200]
        if exc.code == 401:
            raise BytePlusArkAuthFailure()
        if exc.code == 402:
            raise BytePlusArkQuotaExhausted()
        raise BytePlusArkError(
            f"BytePlus ModelArk POST {url} HTTP {exc.code}: {body}"
        )
    except urllib.error.URLError as exc:
        raise BytePlusArkError(f"BytePlus ModelArk connection error: {exc.reason}")


def _http_get(url: str, api_key: str, timeout: float = 30.0) -> dict:
    """
    GET url. Returns parsed response dict.

    Raises BytePlusArkAuthFailure on 401, BytePlusArkError on other failures.
    """
    import json as _json
    req = urllib.request.Request(url, headers=_headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:200]
        if exc.code == 401:
            raise BytePlusArkAuthFailure()
        if exc.code == 402:
            raise BytePlusArkQuotaExhausted()
        raise BytePlusArkError(
            f"BytePlus ModelArk GET {url} HTTP {exc.code}: {body}"
        )
    except urllib.error.URLError as exc:
        raise BytePlusArkError(f"BytePlus ModelArk connection error: {exc.reason}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def submit_video_task(
    prompt: str,
    config,
    episode_id: str,
    *,
    aspect_ratio: str = "16:9",
    duration_seconds: float = 8.0,
    reference_image_url: Optional[str] = None,
    agent: Optional[str] = None,
) -> str:
    """
    Submit a Seedance 2.0 video generation task to BytePlus ModelArk.

    Returns the task_id string. Does not wait for completion.

    Args:
        prompt:              Text description of the video.
        config:              Config object with BYTEPLUS_ARK_API_KEY et al.
        episode_id:          For log correlation.
        aspect_ratio:        "16:9" | "9:16" | "1:1" — mapped to provider resolution.
        duration_seconds:    Requested duration; rounded to 5 or 8.
        reference_image_url: When set, adds image conditioning (I2V path).
        agent:               Optional agent name for log correlation.

    Raises:
        BytePlusArkAuthFailure    — HTTP 401.
        BytePlusArkQuotaExhausted — HTTP 402.
        BytePlusArkError          — Task submission failed.
    """
    import json as _json
    from hypogeum.logger import get_logger
    log = get_logger(episode_id=episode_id, agent=agent or "byteplus_ark")

    api_key  = _get_api_key(config)
    base_url = _get_base_url(config)
    model    = _get_model(config)
    duration = _round_duration(duration_seconds)
    resolution = _resolution_for_aspect(aspect_ratio)

    if not api_key:
        raise BytePlusArkAuthFailure()

    # Build content array
    content: list[dict] = [{"type": "text", "text": prompt}]
    if reference_image_url:
        content.append({"type": "image_url", "image_url": {"url": reference_image_url}})

    payload = _json.dumps({
        "model":   model,
        "content": content,
        "parameters": {
            "resolution": resolution,
            "duration":   duration,
        },
    }).encode("utf-8")

    url = f"{base_url}/videos/generations"
    log.info(
        "byteplus_ark_task_submit",
        model=model,
        resolution=resolution,
        duration=duration,
        has_reference_image=reference_image_url is not None,
    )

    # Retry once on transient errors
    try:
        data = _http_post(url, payload, api_key)
    except BytePlusArkError as exc:
        if "429" in str(exc) or "503" in str(exc) or "500" in str(exc):
            time.sleep(_HTTP_RETRY_DELAY)
            data = _http_post(url, payload, api_key)
        else:
            raise

    task_id = data.get("id") or data.get("task_id") or ""
    if not task_id:
        raise BytePlusArkError(
            f"BytePlus ModelArk: no task_id in response. Keys: {list(data.keys())}"
        )

    log.info("byteplus_ark_task_submitted", task_id=task_id)
    return task_id


def get_task_result(
    task_id: str,
    config,
    episode_id: str,
    *,
    agent: Optional[str] = None,
) -> dict:
    """
    Retrieve the current status of a video generation task.

    Returns the raw API response dict. Does not raise on non-terminal status —
    call this in a poll loop via poll_until_complete().

    Raises BytePlusArkAuthFailure / BytePlusArkError on HTTP failures.
    """
    api_key  = _get_api_key(config)
    base_url = _get_base_url(config)
    url = f"{base_url}/videos/generations/{task_id}"
    return _http_get(url, api_key)


def poll_until_complete(
    task_id: str,
    config,
    episode_id: str,
    *,
    agent: Optional[str] = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """
    Poll the task until it reaches a terminal status (succeeded or failed).

    Returns the final API response dict on succeeded.

    Raises:
        BytePlusArkError — task failed, timed out, or HTTP error.
        BytePlusArkAuthFailure / BytePlusArkQuotaExhausted — propagated from HTTP layer.
    """
    from hypogeum.logger import get_logger
    log = get_logger(episode_id=episode_id, agent=agent or "byteplus_ark")

    deadline = time.monotonic() + timeout_seconds
    wait = _POLL_INITIAL_WAIT

    while time.monotonic() < deadline:
        time.sleep(wait)
        wait = _POLL_STEADY_WAIT

        result = get_task_result(task_id, config, episode_id, agent=agent)
        status = result.get("status", "")

        log.info(
            "byteplus_ark_poll_cycle",
            task_id=task_id,
            status=status,
            remaining_s=round(deadline - time.monotonic()),
        )

        if status == _STATUS_SUCCEEDED:
            log.info("byteplus_ark_task_completed", task_id=task_id)
            return result

        if status == _STATUS_FAILED:
            error_msg = result.get("error") or result.get("message") or "unknown"
            log.warn("byteplus_ark_task_failed", task_id=task_id, error=error_msg)
            raise BytePlusArkError(
                f"BytePlus ModelArk task {task_id!r} failed: {error_msg}"
            )

    raise BytePlusArkError(
        f"BytePlus ModelArk task {task_id!r} did not complete within "
        f"{timeout_seconds:.0f}s"
    )


def extract_video_url(result: dict) -> str:
    """
    Extract the video URL from a succeeded task result.

    Handles two observed response shapes:
      content: [{"type": "video_url", "video_url": {"url": "..."}}]
      content: [{"url": "..."}]

    Returns the URL string, or raises BytePlusArkError if not found.
    """
    content = result.get("content") or result.get("choices") or []
    for item in content:
        if isinstance(item, dict):
            # Shape 1: {"type": "video_url", "video_url": {"url": "..."}}
            if item.get("type") == "video_url":
                url = (item.get("video_url") or {}).get("url", "")
                if url:
                    return url
            # Shape 2: direct {"url": "..."}
            if item.get("url"):
                return item["url"]
    raise BytePlusArkError(
        f"BytePlus ModelArk: no video URL in result. Keys: {list(result.keys())}"
    )
