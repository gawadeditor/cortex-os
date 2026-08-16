"""
hypogeum/runway_official.py — Runway ML official direct provider client (Phase 1J)

Credential: RUNWAY_API_KEY
Auth:        Authorization: Bearer {key}  +  X-Runway-Version: 2024-09-13
Base URL:    https://api.dev.runwayml.com/v1

Public API:
    probe_credential(key) -> (success, error, latency_ms)
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Optional

_DEFAULT_BASE_URL  = "https://api.dev.runwayml.com/v1"
_RUNWAY_VERSION    = "2024-09-13"
_PROBE_TIMEOUT     = 12.0


class RunwayOfficialError(RuntimeError):
    """Base for Runway official client errors."""


class RunwayOfficialAuthFailure(RunwayOfficialError):
    def __init__(self) -> None:
        super().__init__("Runway ML authentication failed (HTTP 401). Check RUNWAY_API_KEY.")


def probe_credential(key: str) -> tuple[bool, Optional[str], int]:
    """
    Validate a Runway ML API key.

    Sends GET /v1/tasks/nonexistent — any response except 401/403 confirms
    the key is accepted by the gateway (404 = auth OK, task not found).
    Returns (success, error_message, latency_ms).
    Does not raise.
    """
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            f"{_DEFAULT_BASE_URL}/tasks/probe-check",
            headers={
                "Authorization":    f"Bearer {key}",
                "X-Runway-Version": _RUNWAY_VERSION,
            },
        )
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            latency_ms = int((time.monotonic() - t0) * 1000)
            # 200 or 404 both indicate the key was accepted
            return True, None, latency_ms
    except urllib.error.HTTPError as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        if exc.code in (401, 403):
            return False, f"HTTP {exc.code} — invalid API key", latency_ms
        # 404 = key accepted, task not found — treat as verified
        if exc.code == 404:
            return True, None, latency_ms
        return False, f"HTTP {exc.code}", latency_ms
    except Exception as exc:
        return False, str(exc)[:120], int((time.monotonic() - t0) * 1000)
