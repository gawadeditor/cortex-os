"""
hypogeum/provider_verifier.py — Provider Credential Verification (F15)

Verify provider API keys by making lightweight HTTP probes. Results are
persisted in the provider_verifications table so the UI shows current status
without re-probing on every page load.

Credential source priority:
  1. SecretProfile in DB (allows per-channel override)
  2. Environment variable (the primary credential for pipeline use)

Providers that share the MUAPI key (seedance, runway, kling, veo) are tested
via the MUAPI probe. freepik has no stable probe — marked not_verifiable.

Public API:
    verify_provider(provider, db_path=None) -> dict
    get_all_provider_statuses(db_path=None) -> dict  # {provider: status_dict}
    STATUS_MISSING        = "missing"
    STATUS_SAVED          = "saved"
    STATUS_VERIFIED       = "verified"
    STATUS_FAILED         = "failed"
    STATUS_NOT_VERIFIABLE = "not_verifiable"
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

STATUS_MISSING        = "missing"
STATUS_SAVED          = "saved"         # credential stored, never tested
STATUS_VERIFIED       = "verified"
STATUS_FAILED         = "failed"
STATUS_NOT_VERIFIABLE = "not_verifiable"

# Env var name for each provider's credential
_PROVIDER_ENV_VAR: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "muapi":      "MUAPI_API_KEY",
    "seedance":   "MUAPI_API_KEY",   # Seedance is accessed via MUAPI
    "runway":     "MUAPI_API_KEY",
    "kling":      "MUAPI_API_KEY",
    "veo":        "MUAPI_API_KEY",
    "freepik":    "FREEPIK_API_KEY",
    "youtube":    "YOUTUBE_REFRESH_TOKEN",
}

# Providers that delegate their probe to another provider's test
_VERIFY_VIA: dict[str, str] = {
    "seedance": "muapi",
    "runway":   "muapi",
    "kling":    "muapi",
    "veo":      "muapi",
}

# Providers with no HTTP probe (not_verifiable)
_NOT_VERIFIABLE = frozenset({"freepik"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# HTTP probes (standalone — do not depend on SecretProfile)
# ---------------------------------------------------------------------------

def _probe_openrouter(key: str) -> tuple[bool, Optional[str], int]:
    """Returns (success, error, latency_ms)."""
    import urllib.request
    import urllib.error
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status == 200:
                return True, None, latency_ms
            return False, f"HTTP {resp.status}", latency_ms
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}", int((time.monotonic() - t0) * 1000)
    except Exception as exc:
        return False, str(exc)[:120], int((time.monotonic() - t0) * 1000)


def _probe_elevenlabs(key: str) -> tuple[bool, Optional[str], int]:
    import urllib.request
    import urllib.error
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/user",
            headers={"xi-api-key": key},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status == 200:
                return True, None, latency_ms
            return False, f"HTTP {resp.status}", latency_ms
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}", int((time.monotonic() - t0) * 1000)
    except Exception as exc:
        return False, str(exc)[:120], int((time.monotonic() - t0) * 1000)


def _probe_muapi(key: str) -> tuple[bool, Optional[str], int]:
    import urllib.request
    import urllib.error
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            "https://api.muapi.ai/api/v1/user/balance",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status in (200, 201):
                return True, None, latency_ms
            return False, f"HTTP {resp.status}", latency_ms
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}", int((time.monotonic() - t0) * 1000)
    except Exception as exc:
        return False, str(exc)[:120], int((time.monotonic() - t0) * 1000)


def _probe_youtube(key: str) -> tuple[bool, Optional[str], int]:
    """Check that a YouTube refresh token is non-empty."""
    t0 = time.monotonic()
    if key and len(key) > 10:
        return True, None, int((time.monotonic() - t0) * 1000)
    return False, "Token appears empty or too short", int((time.monotonic() - t0) * 1000)


_PROBES = {
    "openrouter": _probe_openrouter,
    "elevenlabs": _probe_elevenlabs,
    "muapi":      _probe_muapi,
    "seedance":   _probe_muapi,   # same key
    "runway":     _probe_muapi,
    "kling":      _probe_muapi,
    "veo":        _probe_muapi,
    "youtube":    _probe_youtube,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def verify_provider(
    provider: str,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Probe the provider's credential, persist the result, and return a dict:
        {status, error, latency_ms, source, checked_at}

    status values:
        "verified"        — probe succeeded
        "failed"          — probe returned error
        "not_verifiable"  — no probe implementation
        "missing"         — no credential found (env var and SecretProfile both absent)
    """
    from hypogeum.db import get_connection, initialize_schema, upsert_provider_verification
    from hypogeum.credential_manager import list_secret_profiles

    db = Path(db_path) if db_path else _DEFAULT_DB_PATH
    initialize_schema(db)
    now = _now_iso()

    # Not verifiable short-circuit
    if provider in _NOT_VERIFIABLE:
        _persist(db, provider, STATUS_NOT_VERIFIABLE, now, None, "No probe implemented", "env")
        return {
            "provider":   provider,
            "status":     STATUS_NOT_VERIFIABLE,
            "error":      "No HTTP probe implemented for this provider",
            "latency_ms": None,
            "source":     "env",
            "checked_at": now,
        }

    # Find credential
    credential = None
    source = "env"

    # 1. Try SecretProfile first
    try:
        profiles = list_secret_profiles(provider=provider, db_path=db)
        active = [p for p in profiles if p.status == "active"]
        if active:
            credential = active[0].credential_data
            source = "secret_profile"
    except Exception:
        pass

    # 2. Fall back to env var
    if not credential:
        env_var = _PROVIDER_ENV_VAR.get(provider)
        if env_var:
            credential = os.environ.get(env_var, "").strip() or None
            source = "env"

    if not credential:
        _persist(db, provider, STATUS_MISSING, now, None, "No credential found", source)
        return {
            "provider":   provider,
            "status":     STATUS_MISSING,
            "error":      "No credential found (env var not set, no active SecretProfile)",
            "latency_ms": None,
            "source":     source,
            "checked_at": now,
        }

    # Run probe
    probe = _PROBES.get(provider)
    if probe is None:
        _persist(db, provider, STATUS_NOT_VERIFIABLE, now, None, "No probe", source)
        return {
            "provider":   provider,
            "status":     STATUS_NOT_VERIFIABLE,
            "error":      f"No probe registered for provider {provider!r}",
            "latency_ms": None,
            "source":     source,
            "checked_at": now,
        }

    success, error, latency_ms = probe(credential)
    status = STATUS_VERIFIED if success else STATUS_FAILED
    _persist(db, provider, status, now, latency_ms, error, source)

    # On success, also update last_tested_at on the SecretProfile if used
    if success and source == "secret_profile":
        try:
            profiles = list_secret_profiles(provider=provider, db_path=db)
            active = [p for p in profiles if p.status == "active"]
            if active:
                from hypogeum.credential_manager import update_secret_profile
                update_secret_profile(active[0].secret_profile_id, db_path=db)
        except Exception:
            pass

    return {
        "provider":   provider,
        "status":     status,
        "error":      error,
        "latency_ms": latency_ms,
        "source":     source,
        "checked_at": now,
    }


def get_all_provider_statuses(db_path: Optional[Path] = None) -> dict:
    """
    Return {provider: status_dict} for all known providers.

    status_dict keys: status, error, latency_ms, source, checked_at,
                      has_env_var, has_secret_profile
    """
    from hypogeum.db import get_connection, initialize_schema, get_all_provider_verifications
    from hypogeum.credential_manager import list_secret_profiles

    db = Path(db_path) if db_path else _DEFAULT_DB_PATH
    initialize_schema(db)

    # Stored verification results
    try:
        conn = get_connection(db)
        try:
            stored = get_all_provider_verifications(conn)
        finally:
            conn.close()
    except Exception:
        stored = {}

    # Active SecretProfiles
    try:
        all_profiles = list_secret_profiles(db_path=db)
        secret_providers = {p.provider for p in all_profiles if p.status == "active"}
    except Exception:
        secret_providers = set()

    all_providers = set(_PROVIDER_ENV_VAR) | secret_providers
    result: dict = {}

    for provider in sorted(all_providers):
        env_var = _PROVIDER_ENV_VAR.get(provider)
        has_env = bool(os.environ.get(env_var, "").strip()) if env_var else False
        has_secret = provider in secret_providers
        has_any = has_env or has_secret

        verification = stored.get(provider)
        if verification:
            st = verification["status"]
        elif has_any:
            st = STATUS_SAVED
        else:
            st = STATUS_MISSING

        result[provider] = {
            "status":             st,
            "error":              verification.get("error_message") if verification else None,
            "latency_ms":         verification.get("latency_ms") if verification else None,
            "source":             verification.get("source", "env") if verification else "env",
            "checked_at":         verification.get("checked_at") if verification else None,
            "has_env_var":        has_env,
            "has_secret_profile": has_secret,
            "not_verifiable":     provider in _NOT_VERIFIABLE,
        }

    return result


def _persist(
    db: Path,
    provider: str,
    status: str,
    checked_at: str,
    latency_ms: Optional[int],
    error_message: Optional[str],
    source: str,
) -> None:
    from hypogeum.db import get_connection, upsert_provider_verification
    try:
        conn = get_connection(db)
        try:
            upsert_provider_verification(
                conn, provider, status, checked_at,
                latency_ms, error_message, source,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
