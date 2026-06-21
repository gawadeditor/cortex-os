"""
hypogeum/veo_official.py — Google Veo official direct provider client (Phase 1J)

Credential: GOOGLE_API_KEY
Auth:        ?key={api_key} query parameter (Google AI / Gemini API convention)
Base URL:    https://generativelanguage.googleapis.com/v1beta

Probe:       GET /models?key={api_key}  — lists available models, confirms auth.
             Checks the response for Veo-family model IDs.

Public API:
    probe_credential(key) -> (success, error, latency_ms)
    list_veo_models(key)  -> list[str]  — model IDs containing "veo"
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Optional

_BASE_URL      = "https://generativelanguage.googleapis.com/v1beta"
_PROBE_TIMEOUT = 12.0


class VeoOfficialError(RuntimeError):
    """Base for Google Veo client errors."""


class VeoOfficialAuthFailure(VeoOfficialError):
    def __init__(self) -> None:
        super().__init__("Google Veo authentication failed (HTTP 400/401/403). Check GOOGLE_API_KEY.")


def probe_credential(key: str) -> tuple[bool, Optional[str], int]:
    """
    Validate a Google API key via the Gemini models list.

    Returns (success, error_message, latency_ms). Does not raise.
    A successful 200 with any model list confirms the key is valid.
    """
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            f"{_BASE_URL}/models?key={key}&pageSize=1",
        )
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status == 200:
                return True, None, latency_ms
            return False, f"HTTP {resp.status}", latency_ms
    except urllib.error.HTTPError as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        if exc.code in (400, 401, 403):
            try:
                body = json.loads(exc.read().decode("utf-8", errors="replace"))
                msg  = (body.get("error") or {}).get("message") or f"HTTP {exc.code}"
            except Exception:
                msg = f"HTTP {exc.code}"
            return False, msg, latency_ms
        return False, f"HTTP {exc.code}", latency_ms
    except Exception as exc:
        return False, str(exc)[:120], int((time.monotonic() - t0) * 1000)


def list_veo_models(key: str) -> list:
    """
    Return model IDs containing 'veo' from the Google AI model list.

    Returns [] on any error (non-blocking).
    """
    try:
        req = urllib.request.Request(f"{_BASE_URL}/models?key={key}&pageSize=100")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data   = json.loads(resp.read().decode("utf-8"))
            models = data.get("models", [])
            return [m["name"] for m in models if "veo" in m.get("name", "").lower()]
    except Exception:
        return []
