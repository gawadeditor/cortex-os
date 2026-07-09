"""
hypogeum/higgsfield.py — Higgsfield AI video generation client

Official API surface (verified 2026-07-06 via docs.higgsfield.ai + live probe):
  Auth:     Authorization: Key {api_key}:{api_secret}  (both required)
  Base:     https://platform.higgsfield.ai
  Submit:   POST /{model_id}                  e.g. POST /higgsfield-ai/dop/standard
  Poll:     GET  /requests/{request_id}/status
  Cancel:   POST /requests/{request_id}/cancel
  Upload:   POST /files/generate-upload-url → PUT to presigned URL
  Webhook:  ?hf_webhook={url} query param on submit
  Statuses: queued | in_progress | completed | failed | nsfw | cancelled  (all lowercase)
  Submit:   response → {"status":"queued","request_id":"...","status_url":"...","cancel_url":"..."}
  Result:   completed → {"status":"completed","request_id":"...","video":{"url":"https://...mp4"}}

DoP model tiers (Image-to-Video only — requires reference image_url):
  dop-lite      cheapest, fastest             ~$0.14/gen   path: /higgsfield-ai/dop/lite
  dop-standard  standard quality              ~$0.28/gen   path: /higgsfield-ai/dop/standard
  dop-turbo     faster, higher quality        ~$0.42/gen   path: /higgsfield-ai/dop/turbo

NOTE: Higgsfield Cloud REST API is I2V only. There is no Text-to-Video endpoint.
Documentation: https://docs.higgsfield.ai/docs/guides/video.md ("Generate Videos from Images")
The "Generate Videos from Text" guide does not exist in the official documentation.

Request body (flat, no params wrapper):
  {"prompt": "...", "image_url": "https://cdn-url-to-reference-image.jpg", "duration": 5}

Credential env vars (separate key+secret required):
  HF_API_KEY     — Higgsfield Cloud API key ID
  HF_API_SECRET  — Higgsfield Cloud API secret

Account creation:
  https://cloud.higgsfield.ai/sign-up → Dashboard → API Keys

Integration contracts:
  - All env access → Config object, never os.getenv()
  - All logging → hypogeum.logger.get_logger(), never print()
  - HF_API_KEY and HF_API_SECRET are never logged or stored in output files
  - HIGGSFIELD_DRY_RUN=true returns fake request_id without API calls
  - Comparison runner is the only caller of this module — not wired into pipeline

Error classes:
  HiggsFieldAuthError     — 401 (bad key/secret) — fatal, halt immediately
  HiggsFieldQuotaError    — 402 / credit exhausted — fatal
  HiggsFieldContentError  — NSFW status — skip, do not retry
  HiggsFieldError         — all other failures (retriable)

Logging events:
  hf_job_submitted        — info   — job accepted, request_id received
  hf_poll_cycle           — debug  — each poll tick
  hf_job_completed        — info   — Completed status received
  hf_job_failed           — warn   — Failed status received
  hf_nsfw_rejected        — warn   — NSFW status; generation rejected
  hf_auth_failure         — error  — 401; credentials invalid
  hf_quota_exhausted      — error  — 402; account has no credits
  hf_download_completed   — info   — video file written to disk
  hf_download_failed      — error  — download failed
  hf_timeout              — warn   — job did not complete within window
  hf_upload_started       — info   — reference image upload started
  hf_upload_completed     — info   — reference image hosted at CDN URL
  hf_upload_failed        — error  — reference image upload failed
  hf_dry_run_submission   — info   — dry-run; no real API call made
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Optional

import requests

from hypogeum.config import Config
from hypogeum.logger import get_logger

_BASE_URL = "https://platform.higgsfield.ai"
_DEFAULT_TIMEOUT_SECONDS = 180
_DEFAULT_POLL_INTERVAL = 5
_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB download chunks

# Pricing (USD per generation) — verified 2026-07-06 from official docs + live probe
PRICING: dict[str, float] = {
    "dop-lite":     0.14,   # 5s clip  — path: /higgsfield-ai/dop/lite
    "dop-standard": 0.28,   # 5s clip  — path: /higgsfield-ai/dop/standard
    "dop-turbo":    0.42,   # 5s clip  — path: /higgsfield-ai/dop/turbo
}

# Map from public model name → API path variant (used in POST /{vendor}/{product}/{variant})
_DOP_VARIANT: dict[str, str] = {
    "dop-lite":     "lite",
    "dop-standard": "standard",
    "dop-turbo":    "turbo",
}

# All DoP models are Image-to-Video and require a reference image_url
DOP_MODELS = frozenset(PRICING.keys())


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

class HiggsFieldError(Exception):
    """Base error for all Higgsfield API failures."""


class HiggsFieldAuthError(HiggsFieldError):
    """401 — credentials rejected; halt all generation."""


class HiggsFieldQuotaError(HiggsFieldError):
    """402 or credit exhaustion — no credits available."""


class HiggsFieldContentError(HiggsFieldError):
    """NSFW — generation rejected by content moderation."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _auth_header(cfg: Config) -> dict[str, str]:
    key = getattr(cfg, "HF_API_KEY", None) or ""
    secret = getattr(cfg, "HF_API_SECRET", None) or ""
    return {"Authorization": f"Key {key}:{secret}"}


def _classify_http(status_code: int) -> str:
    if status_code == 401:
        return "auth"
    if status_code == 402:
        return "quota"
    if status_code in (429, 503):
        return "transient"
    if status_code >= 500:
        return "transient"
    if status_code >= 400:
        return "hard"
    return "ok"


# ---------------------------------------------------------------------------
# File upload — required to get a hosted CDN URL for the reference image
# ---------------------------------------------------------------------------

def upload_reference_image(cfg: Config, image_path: Path) -> str:
    """Upload a local image to Higgsfield CDN and return the public URL.

    Steps:
      1. POST /files/generate-upload-url → {upload_url, public_url}
      2. PUT to upload_url with image bytes
      3. Return public_url

    Raises HiggsFieldError on failure.
    """
    log = get_logger()
    log.info("hf_upload_started", image_path=str(image_path))

    content_type = "image/jpeg" if image_path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    headers = _auth_header(cfg)

    # Step 1 — request presigned upload URL
    r = requests.post(
        f"{_BASE_URL}/files/generate-upload-url",
        json={"content_type": content_type},
        headers={**headers, "Content-Type": "application/json"},
        timeout=30,
    )
    cls = _classify_http(r.status_code)
    if cls == "auth":
        log.error("hf_auth_failure", status=r.status_code)
        raise HiggsFieldAuthError(f"Auth failed on upload-url: HTTP {r.status_code}")
    if cls != "ok":
        raise HiggsFieldError(f"Failed to get upload URL: HTTP {r.status_code} — {r.text[:200]}")

    data = r.json()
    upload_url: str = data["upload_url"]
    public_url: str = data["public_url"]

    # Step 2 — upload image bytes
    image_bytes = image_path.read_bytes()
    put_r = requests.put(
        upload_url,
        data=image_bytes,
        headers={"Content-Type": content_type},
        timeout=60,
    )
    if put_r.status_code not in (200, 204):
        log.error("hf_upload_failed", status=put_r.status_code)
        raise HiggsFieldError(f"Image upload PUT failed: HTTP {put_r.status_code}")

    log.info("hf_upload_completed", public_url=public_url)
    return public_url


# ---------------------------------------------------------------------------
# Core generation — submit + poll + download
# ---------------------------------------------------------------------------

def submit_dop_generation(
    cfg: Config,
    *,
    model: str,
    prompt: str,
    image_url: str,
    seed: Optional[int] = None,
    duration: int = 5,
    dry_run: bool = False,
) -> dict:
    """Submit a DoP image-to-video generation request.

    Returns a result dict:
      {
        "request_id": str,
        "status": "Completed"|"Failed"|"NSFW"|"Cancelled"|"timeout",
        "output_url": str | None,
        "cost_usd": float,
        "model": str,
        "submit_time": float,
        "complete_time": float,
        "elapsed_seconds": float,
        "dry_run": bool,
      }
    """
    log = get_logger()

    if model not in DOP_MODELS:
        raise HiggsFieldError(f"Unknown DoP model '{model}'. Valid: {sorted(DOP_MODELS)}")

    if dry_run or getattr(cfg, "HIGGSFIELD_DRY_RUN", False):
        fake_id = f"dry_hf_{uuid.uuid4().hex[:12]}"
        log.info("hf_dry_run_submission", model=model, fake_request_id=fake_id)
        return {
            "request_id": fake_id,
            "status": "dry_run",
            "output_url": None,
            "cost_usd": PRICING[model],
            "model": model,
            "submit_time": time.time(),
            "complete_time": time.time(),
            "elapsed_seconds": 0.0,
            "dry_run": True,
        }

    headers = {**_auth_header(cfg), "Content-Type": "application/json"}

    # Official API (2026-07-06): POST /{vendor}/{product}/{variant} with flat body.
    # Docs: https://docs.higgsfield.ai/docs/guides/video.md
    # Body: {"prompt": "...", "image_url": "...", "duration": N}  — no "params" wrapper.
    variant = _DOP_VARIANT[model]
    payload: dict = {
        "prompt": prompt,
        "image_url": image_url,
        "duration": duration,
    }
    if seed is not None:
        payload["seed"] = seed

    submit_time = time.time()

    r = requests.post(
        f"{_BASE_URL}/higgsfield-ai/dop/{variant}",
        json=payload,
        headers=headers,
        timeout=30,
    )

    cls = _classify_http(r.status_code)
    if cls == "auth":
        log.error("hf_auth_failure", status=r.status_code, model=model)
        raise HiggsFieldAuthError(f"Higgsfield auth failed: HTTP {r.status_code}")
    if cls == "quota":
        log.error("hf_quota_exhausted", status=r.status_code, model=model)
        raise HiggsFieldQuotaError(f"Higgsfield quota exhausted: HTTP {r.status_code}")
    if cls != "ok":
        raise HiggsFieldError(f"Submit failed: HTTP {r.status_code} — {r.text[:400]}")

    resp = r.json()
    # Official submit response: {"status":"queued","request_id":"...","status_url":"..."}
    request_id: str = resp.get("request_id") or resp.get("id") or ""
    if not request_id:
        raise HiggsFieldError(f"Submit response missing request_id: {r.text[:400]}")
    log.info("hf_job_submitted", request_id=request_id, model=model, duration=duration)

    # Poll until terminal state
    timeout = getattr(cfg, "HIGGSFIELD_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS)
    poll_interval = getattr(cfg, "HIGGSFIELD_POLL_INTERVAL_SECONDS", _DEFAULT_POLL_INTERVAL)
    deadline = time.time() + timeout
    output_url: Optional[str] = None

    while time.time() < deadline:
        time.sleep(poll_interval)
        status_r = requests.get(
            f"{_BASE_URL}/requests/{request_id}/status",
            headers=_auth_header(cfg),
            timeout=20,
        )
        if status_r.status_code == 401:
            log.error("hf_auth_failure", status=401, request_id=request_id)
            raise HiggsFieldAuthError("Auth failed during poll")

        if status_r.status_code != 200:
            log.info("hf_poll_cycle", request_id=request_id,
                     status=status_r.status_code, note="non-200 poll response; retrying")
            continue

        data = status_r.json()
        # Statuses are lowercase: queued | in_progress | completed | failed | nsfw | cancelled
        # Verified 2026-07-04 from live API responses
        status: str = data.get("status", "")
        log.info("hf_poll_cycle", request_id=request_id, status=status)

        if status == "completed":
            # Completed response: {"status": "completed", "video": {"url": "https://...mp4"}}
            video = data.get("video", {})
            output_url = video.get("url") if video else None
            complete_time = time.time()
            log.info("hf_job_completed", request_id=request_id,
                     elapsed=round(complete_time - submit_time, 1), output_url=output_url)
            return {
                "request_id": request_id,
                "status": "Completed",
                "output_url": output_url,
                "cost_usd": PRICING[model],
                "model": model,
                "submit_time": submit_time,
                "complete_time": complete_time,
                "elapsed_seconds": round(complete_time - submit_time, 1),
                "dry_run": False,
            }

        if status == "failed":
            complete_time = time.time()
            log.warn("hf_job_failed", request_id=request_id,
                     elapsed=round(complete_time - submit_time, 1))
            return {
                "request_id": request_id,
                "status": "Failed",
                "output_url": None,
                "cost_usd": 0.0,  # Failed generations are typically refunded
                "model": model,
                "submit_time": submit_time,
                "complete_time": complete_time,
                "elapsed_seconds": round(complete_time - submit_time, 1),
                "dry_run": False,
            }

        if status == "nsfw":
            log.warn("hf_nsfw_rejected", request_id=request_id)
            raise HiggsFieldContentError(f"Generation rejected as NSFW: {request_id}")

        if status == "cancelled":
            return {
                "request_id": request_id,
                "status": "Cancelled",
                "output_url": None,
                "cost_usd": 0.0,
                "model": model,
                "submit_time": submit_time,
                "complete_time": time.time(),
                "elapsed_seconds": round(time.time() - submit_time, 1),
                "dry_run": False,
            }
        # queued / in_progress — keep polling

    log.warn("hf_timeout", request_id=request_id, timeout_seconds=timeout, model=model)
    return {
        "request_id": request_id,
        "status": "timeout",
        "output_url": None,
        "cost_usd": 0.0,
        "model": model,
        "submit_time": submit_time,
        "complete_time": time.time(),
        "elapsed_seconds": round(time.time() - submit_time, 1),
        "dry_run": False,
    }


def download_clip(output_url: str, dest_path: Path) -> Path:
    """Download the generated video from the CDN URL to dest_path."""
    log = get_logger()
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    r = requests.get(output_url, stream=True, timeout=120)
    if r.status_code != 200:
        log.error("hf_download_failed", status=r.status_code, url=output_url)
        raise HiggsFieldError(f"Download failed: HTTP {r.status_code}")

    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=_CHUNK_SIZE):
            f.write(chunk)

    log.info("hf_download_completed",
             path=str(dest_path), size_bytes=dest_path.stat().st_size)
    return dest_path


def check_credentials(cfg: Config) -> dict:
    """Probe Higgsfield credentials without spending credits.

    Returns {"valid": bool, "error": str|None, "detail": str}
    """
    log = get_logger()
    key = getattr(cfg, "HF_API_KEY", None)
    secret = getattr(cfg, "HF_API_SECRET", None)

    if not key or not secret:
        return {
            "valid": False,
            "error": "missing_credentials",
            "detail": "HF_API_KEY and HF_API_SECRET must both be set",
        }

    # Auth probe: POST /higgsfield-ai/dop/lite with empty body.
    # 422 = path exists + auth accepted (validation error on missing fields) → valid
    # 401/403 = auth rejected → invalid
    # The server rejects ALL GET requests with 405 before checking auth, so GET probes
    # cannot distinguish valid from invalid credentials.
    try:
        r = requests.post(
            f"{_BASE_URL}/higgsfield-ai/dop/lite",
            json={},
            headers={**_auth_header(cfg), "Content-Type": "application/json"},
            timeout=15,
        )
        if r.status_code == 422:
            # Validation error on empty body = auth passed, path exists
            return {"valid": True, "error": None, "detail": "HTTP 422 — credentials valid (validation error on empty body)"}
        if r.status_code in (401, 403):
            log.error("hf_auth_failure", status=r.status_code, phase="credential_check")
            return {"valid": False, "error": "auth_rejected", "detail": f"HTTP {r.status_code} — invalid key/secret"}
        if r.status_code == 200:
            return {"valid": True, "error": None, "detail": "HTTP 200 — credentials valid"}
        return {
            "valid": False,
            "error": f"http_{r.status_code}",
            "detail": f"Unexpected status {r.status_code}: {r.text[:100]}",
        }
    except requests.RequestException as e:
        return {"valid": False, "error": "network_error", "detail": str(e)}
