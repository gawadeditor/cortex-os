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
openai is a direct provider using OPENAI_API_KEY (image generation + Whisper).

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
    "openai":            "OPENAI_API_KEY",
    "openrouter":        "OPENROUTER_API_KEY",
    "elevenlabs":        "ELEVENLABS_API_KEY",
    "muapi":             "MUAPI_API_KEY",
    "seedance":          "MUAPI_API_KEY",          # delegated via MUAPI
    "runway":            "MUAPI_API_KEY",
    "kling":             "MUAPI_API_KEY",
    "veo":               "MUAPI_API_KEY",
    "freepik":           "FREEPIK_API_KEY",
    "youtube":           "YOUTUBE_REFRESH_TOKEN",
    "seedance_official": "BYTEPLUS_ARK_API_KEY",   # Phase 1H
    # Phase 1J — official direct providers
    "runway_official":   "RUNWAY_API_KEY",
    "kling_official":    "KLING_API_KEY",
    "veo_official":      "GOOGLE_API_KEY",
    # Phase 1J — publishing providers (probe via access token)
    "facebook":          "META_ACCESS_TOKEN",
    "instagram":         "META_ACCESS_TOKEN",
    "tiktok":            "TIKTOK_ACCESS_TOKEN",
}

# Providers that delegate their probe to another provider's test
_VERIFY_VIA: dict[str, str] = {
    "seedance": "muapi",
    "runway":   "muapi",
    "kling":    "muapi",
    "veo":      "muapi",
}

# Providers with no HTTP probe (not_verifiable)
# freepik removed — now has a real probe (Phase 1J)
_NOT_VERIFIABLE: frozenset = frozenset()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# HTTP probes (standalone — do not depend on SecretProfile)
# ---------------------------------------------------------------------------

def _probe_openai(key: str) -> tuple[bool, Optional[str], int]:
    """GET https://api.openai.com/v1/models — lightweight auth check."""
    import urllib.request
    import urllib.error
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            "https://api.openai.com/v1/models",
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
    """
    Validate an MUAPI key via GET /api/v1/user/balance.

    MUAPI auth: x-api-key header (NOT Authorization: Bearer).
    Falls back to a no-cost model-list call if the balance endpoint
    returns 404 (endpoint may not exist on all MUAPI tiers).
    """
    import urllib.request
    import urllib.error
    t0 = time.monotonic()
    # Correct header: x-api-key, not Authorization: Bearer
    _headers = {"x-api-key": key}
    try:
        req = urllib.request.Request(
            "https://api.muapi.ai/api/v1/user/balance",
            headers=_headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status in (200, 201):
                return True, None, latency_ms
            return False, f"HTTP {resp.status}", latency_ms
    except urllib.error.HTTPError as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        if exc.code == 401:
            return False, "HTTP 401 — invalid API key", latency_ms
        if exc.code == 404:
            # Balance endpoint absent on this MUAPI tier — key may still be valid.
            # Try the models list as a secondary no-cost probe.
            return _probe_muapi_models(key, t0)
        # 403 from balance = key format may be wrong or insufficient tier
        return False, f"HTTP {exc.code}", latency_ms
    except Exception as exc:
        return False, str(exc)[:120], int((time.monotonic() - t0) * 1000)


def _probe_muapi_models(key: str, t0: float) -> tuple[bool, Optional[str], int]:
    """
    Secondary MUAPI probe: GET /api/v1/models with x-api-key.
    Used when the balance endpoint returns 404.
    """
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(
            "https://api.muapi.ai/api/v1/models",
            headers={"x-api-key": key},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return (resp.status in (200, 201)), None if resp.status in (200, 201) else f"HTTP {resp.status}", latency_ms
    except urllib.error.HTTPError as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        if exc.code == 401:
            return False, "HTTP 401 — invalid API key", latency_ms
        # Any other response (403/404) means key reached server — treat as saved/unverifiable
        return False, f"HTTP {exc.code} from /models", latency_ms
    except Exception as exc:
        return False, str(exc)[:120], int((time.monotonic() - t0) * 1000)


def _probe_youtube(key: str) -> tuple[bool, Optional[str], int]:
    """Check that a YouTube refresh token is non-empty."""
    t0 = time.monotonic()
    if key and len(key) > 10:
        return True, None, int((time.monotonic() - t0) * 1000)
    return False, "Token appears empty or too short", int((time.monotonic() - t0) * 1000)


def _probe_byteplus_ark(key: str) -> tuple[bool, Optional[str], int]:
    """Validate a BytePlus ModelArk API key (Phase 1H)."""
    from hypogeum.byteplus_ark import probe_credential
    return probe_credential(key)


def _probe_freepik(key: str) -> tuple[bool, Optional[str], int]:
    """
    Validate a Freepik API key via the AI resources endpoint (Phase 1J).
    GET https://api.freepik.com/v1/ai/resources?page_size=1
    """
    import urllib.request
    import urllib.error
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            "https://api.freepik.com/v1/ai/resources?page_size=1",
            headers={"X-Freepik-API-Key": key},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status == 200:
                return True, None, latency_ms
            return False, f"HTTP {resp.status}", latency_ms
    except urllib.error.HTTPError as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        if exc.code in (401, 403):
            return False, f"HTTP {exc.code} — invalid API key", latency_ms
        # 404 means key accepted, endpoint differs — still counts as verified
        if exc.code == 404:
            return True, None, latency_ms
        return False, f"HTTP {exc.code}", latency_ms
    except Exception as exc:
        return False, str(exc)[:120], int((time.monotonic() - t0) * 1000)


def _probe_runway_official(key: str) -> tuple[bool, Optional[str], int]:
    """Validate a Runway ML API key (Phase 1J)."""
    from hypogeum.runway_official import probe_credential
    return probe_credential(key)


def _probe_kling_official(key: str) -> tuple[bool, Optional[str], int]:
    """Validate a Kling AI key (Phase 1J). Format: key_id:key_secret"""
    from hypogeum.kling_official import probe_credential
    return probe_credential(key)


def _probe_veo_official(key: str) -> tuple[bool, Optional[str], int]:
    """Validate a Google API key for Veo (Phase 1J)."""
    from hypogeum.veo_official import probe_credential
    return probe_credential(key)


def _probe_meta_token(credential: str) -> tuple[bool, Optional[str], int]:
    """
    Validate a Meta Graph API access token.
    Works for both facebook and instagram providers (same token).
    Probes GET https://graph.facebook.com/v19.0/me?access_token={token}
    """
    import urllib.request
    import urllib.error
    import json as _json
    # credential may be raw token or JSON blob from multi-field form
    token = credential
    try:
        blob = _json.loads(credential)
        token = blob.get("META_ACCESS_TOKEN", credential)
    except Exception:
        pass
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            f"https://graph.facebook.com/v19.0/me?access_token={token}&fields=id",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status == 200:
                return True, None, latency_ms
            return False, f"HTTP {resp.status}", latency_ms
    except urllib.error.HTTPError as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return False, f"HTTP {exc.code}: {body}", latency_ms
    except Exception as exc:
        return False, str(exc)[:120], int((time.monotonic() - t0) * 1000)


def _probe_tiktok(credential: str) -> tuple[bool, Optional[str], int]:
    """
    Validate a TikTok access token.
    credential may be raw token or JSON blob {"TIKTOK_ACCESS_TOKEN": "..."}.
    """
    import urllib.request
    import urllib.error
    import json as _json
    token = credential
    try:
        blob = _json.loads(credential)
        token = blob.get("access_token") or blob.get("TIKTOK_ACCESS_TOKEN") or credential
    except Exception:
        pass
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            "https://open.tiktokapis.com/v2/user/info/?fields=open_id,display_name",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status == 200:
                return True, None, latency_ms
            return False, f"HTTP {resp.status}", latency_ms
    except urllib.error.HTTPError as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return False, f"HTTP {exc.code}", latency_ms
    except Exception as exc:
        return False, str(exc)[:120], int((time.monotonic() - t0) * 1000)


_PROBES = {
    "openai":            _probe_openai,
    "openrouter":        _probe_openrouter,
    "elevenlabs":        _probe_elevenlabs,
    "muapi":             _probe_muapi,
    "seedance":          _probe_muapi,              # delegated via MUAPI
    "runway":            _probe_muapi,
    "kling":             _probe_muapi,
    "veo":               _probe_muapi,
    "freepik":           _probe_freepik,            # Phase 1J — now has real probe
    "youtube":           _probe_youtube,
    "seedance_official": _probe_byteplus_ark,       # Phase 1H
    "runway_official":   _probe_runway_official,    # Phase 1J
    "kling_official":    _probe_kling_official,     # Phase 1J
    "veo_official":      _probe_veo_official,       # Phase 1J
    "facebook":          _probe_meta_token,         # Phase 1J — publishing
    "instagram":         _probe_meta_token,         # Phase 1J — publishing
    "tiktok":            _probe_tiktok,             # Phase 1J — publishing
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
