"""
hypogeum/kling_official.py — Kling AI official direct provider client (Phase 1J)

Credential: KLING_API_KEY stored as "{access_key_id}:{access_key_secret}"
Auth:        JWT generated from access_key_id + access_key_secret (HS256)
Base URL:    https://api.klingai.com

JWT generation (per official Kling docs):
  header:  {"alg": "HS256", "typ": "JWT"}
  payload: {"iss": access_key_id, "exp": now+1800, "nbf": now-5}
  sign:    HMAC-SHA256(header_b64.payload_b64, access_key_secret)

Credential storage format in Secret Profile:
  Combined string: "{access_key_id}:{access_key_secret}"
  Connect form splits on first colon only.

Public API:
    parse_kling_key(credential) -> (key_id, key_secret)
    generate_jwt(key_id, key_secret) -> str
    probe_credential(credential) -> (success, error, latency_ms)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from typing import Optional

_DEFAULT_BASE_URL = "https://api.klingai.com"
_PROBE_TIMEOUT    = 12.0


class KlingOfficialError(RuntimeError):
    """Base for Kling official client errors."""


class KlingOfficialAuthFailure(KlingOfficialError):
    def __init__(self) -> None:
        super().__init__("Kling AI authentication failed. Check KLING_API_KEY format (key_id:key_secret).")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def parse_kling_key(credential: str) -> tuple[str, str]:
    """
    Split a combined Kling credential into (key_id, key_secret).

    Expected format: "{access_key_id}:{access_key_secret}"
    Raises ValueError if the format is invalid.
    """
    if not credential or ":" not in credential:
        raise ValueError(
            "KLING_API_KEY must be in format 'access_key_id:access_key_secret'. "
            f"Got: {credential[:20]!r}…"
        )
    key_id, key_secret = credential.split(":", 1)
    return key_id.strip(), key_secret.strip()


def generate_jwt(key_id: str, key_secret: str) -> str:
    """Generate a Kling API JWT token (valid 30 minutes)."""
    now = int(time.time())
    header  = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps({"iss": key_id, "exp": now + 1800, "nbf": now - 5}, separators=(",", ":")).encode())
    message = f"{header}.{payload}"
    sig     = _b64url(hmac.new(key_secret.encode(), message.encode(), hashlib.sha256).digest())
    return f"{message}.{sig}"


def probe_credential(credential: str) -> tuple[bool, Optional[str], int]:
    """
    Validate a Kling AI credential.

    credential format: "{access_key_id}:{access_key_secret}"

    Probes GET /v1/videos/text2video?pageNum=1&pageSize=1 — a list endpoint
    that confirms auth without generating any content or incurring cost.
    Returns (success, error_message, latency_ms). Does not raise.
    """
    t0 = time.monotonic()
    try:
        key_id, key_secret = parse_kling_key(credential)
    except ValueError as exc:
        return False, str(exc), 0

    try:
        jwt_token = generate_jwt(key_id, key_secret)
    except Exception as exc:
        return False, f"JWT generation failed: {exc}", 0

    try:
        req = urllib.request.Request(
            f"{_DEFAULT_BASE_URL}/v1/videos/text2video?pageNum=1&pageSize=1",
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status == 200:
                return True, None, latency_ms
            return False, f"HTTP {resp.status}", latency_ms
    except urllib.error.HTTPError as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        if exc.code in (401, 403):
            return False, f"HTTP {exc.code} — authentication rejected", latency_ms
        # Any other HTTP response still confirms JWT was formed correctly
        # and the key was accepted at the API gateway
        if exc.code in (404, 405, 400):
            return True, None, latency_ms
        return False, f"HTTP {exc.code}", latency_ms
    except Exception as exc:
        return False, str(exc)[:120], int((time.monotonic() - t0) * 1000)
