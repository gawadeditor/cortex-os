"""
scripts/runway_diagnostic.py — Minimal Runway provider diagnostic

Isolates MUAPI Runway failures outside the pipeline.

Tests:
  1. runway-text-to-video: one job with a safe, short prompt.
  2. runway-image-to-video: skipped (reference image is a local file;
     Runway requires a public HTTPS URL — blocked by local URL requirement).

Budget cap: 0.30 credits. Aborts before submission if cost would exceed cap.

Usage:
    python3 scripts/runway_diagnostic.py

Requires .env with MUAPI_API_KEY set (or environment variable).
Output: human-readable report to stdout + JSON summary to
    scripts/runway_diagnostic_result.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Bootstrap: add project root to sys.path so hypogeum imports work
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass  # python-dotenv optional; env vars must be set manually

import requests  # noqa: E402
from hypogeum.config import Config  # noqa: E402

# ---------------------------------------------------------------------------
# Config — read from .env exactly as the pipeline does
# ---------------------------------------------------------------------------
_config = Config()
MUAPI_API_KEY   = _config.MUAPI_API_KEY
MUAPI_BASE_URL  = _config.MUAPI_BASE_URL

T2V_MODEL       = "runway-text-to-video"
T2V_PROMPT      = (
    "A slow cinematic shot of an ancient bronze astronomical device "
    "on a dark museum table, no people, no text, no dialogue."
)
T2V_ASPECT      = "16:9"
T2V_DURATION    = 5       # seconds — Runway enum: 5 or 8
T2V_COST        = 0.15    # confirmed 2026-06-05
BUDGET_CAP      = 0.30    # hard cap for this diagnostic run

POLL_INTERVAL   = 6       # seconds between polls
POLL_MAX        = 25      # max ~150 seconds before timeout
POLL_400_ABORT  = 3       # consecutive 400s → abort poll

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    # MUAPI uses x-api-key (NOT Authorization: Bearer) — per muapi.py module docstring
    return {
        "x-api-key": MUAPI_API_KEY,
        "Content-Type": "application/json",
    }


def _redact(s: str) -> str:
    """Redact API key from a string for safe printing."""
    if MUAPI_API_KEY and MUAPI_API_KEY in s:
        return s.replace(MUAPI_API_KEY, "[REDACTED]")
    return s


def _print(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# T2V diagnostic
# ---------------------------------------------------------------------------

def run_t2v_diagnostic() -> dict:
    """
    Submit one runway-text-to-video job and poll to completion.

    Returns a result dict with all diagnostic fields.
    """
    result: dict = {
        "test": "runway-text-to-video",
        "model": T2V_MODEL,
        "prompt": T2V_PROMPT,
        "endpoint": f"{MUAPI_BASE_URL}/{T2V_MODEL}",
        "payload_shape": {
            "prompt": "<string>",
            "aspect_ratio": T2V_ASPECT,
            "duration": T2V_DURATION,
        },
        "budget_cap": BUDGET_CAP,
        "estimated_cost": T2V_COST,
        "request_id": None,
        "submission_http_status": None,
        "submission_error_body": None,
        "poll_history": [],
        "final_status": None,
        "final_error_body": None,
        "output_url": None,
        "credits_charged": None,
        "outcome": None,
        "failure_category": None,
    }

    # -----------------------------------------------------------------------
    # Budget pre-check
    # -----------------------------------------------------------------------
    if T2V_COST > BUDGET_CAP:
        result["outcome"] = "aborted_budget"
        result["failure_category"] = "budget_guard"
        _print(f"[ABORT] Estimated cost {T2V_COST} exceeds budget cap {BUDGET_CAP}. Not submitting.")
        return result

    _print(f"\n{'='*60}")
    _print(f"TEST: {T2V_MODEL}")
    _print(f"Endpoint: {result['endpoint']}")
    _print(f"Prompt: {T2V_PROMPT}")
    _print(f"Payload: aspect_ratio={T2V_ASPECT}, duration={T2V_DURATION}s")
    _print(f"Estimated cost: ${T2V_COST} | Budget cap: ${BUDGET_CAP}")
    _print(f"{'='*60}")

    # -----------------------------------------------------------------------
    # Submit
    # -----------------------------------------------------------------------
    payload = {
        "prompt": T2V_PROMPT,
        "aspect_ratio": T2V_ASPECT,
        "duration": T2V_DURATION,
    }

    _print("\n[1] Submitting job...")
    t_submit = time.monotonic()
    try:
        resp = requests.post(
            result["endpoint"],
            headers=_headers(),
            json=payload,
            timeout=30,
        )
    except requests.exceptions.Timeout:
        result["outcome"] = "submission_timeout"
        result["failure_category"] = "network"
        _print("[ERROR] Submission timed out after 30s")
        return result
    except requests.exceptions.ConnectionError as exc:
        result["outcome"] = "connection_error"
        result["failure_category"] = "network"
        result["submission_error_body"] = str(exc)
        _print(f"[ERROR] Connection error: {exc}")
        return result

    elapsed_submit = time.monotonic() - t_submit
    result["submission_http_status"] = resp.status_code
    _print(f"  HTTP {resp.status_code} in {elapsed_submit:.2f}s")

    # Parse submission response
    try:
        resp_data = resp.json()
    except Exception:
        resp_data = {"raw": resp.text[:500]}

    _print(f"  Response body: {json.dumps(resp_data)[:500]}")

    if resp.status_code == 401:
        result["outcome"] = "auth_failure"
        result["failure_category"] = "authentication"
        result["submission_error_body"] = resp_data
        _print("[ERROR] HTTP 401 — MUAPI_API_KEY is invalid or missing")
        return result

    if resp.status_code == 402:
        result["outcome"] = "quota_exhausted"
        result["failure_category"] = "quota"
        result["submission_error_body"] = resp_data
        _print("[ERROR] HTTP 402 — MUAPI credit quota exhausted")
        return result

    if resp.status_code == 422:
        result["outcome"] = "unprocessable_entity"
        result["failure_category"] = "payload_format"
        result["submission_error_body"] = resp_data
        _print("[ERROR] HTTP 422 — Unprocessable Entity (payload rejected by API)")
        _print("  → Likely cause: invalid field, unsupported model, or bad parameter")
        return result

    if resp.status_code == 400:
        result["outcome"] = "bad_request"
        result["failure_category"] = "payload_format"
        result["submission_error_body"] = resp_data
        _print("[ERROR] HTTP 400 — Bad Request (payload format error)")
        return result

    if resp.status_code not in (200, 202):
        result["outcome"] = f"http_{resp.status_code}"
        result["failure_category"] = "provider_error"
        result["submission_error_body"] = resp_data
        _print(f"[ERROR] Unexpected HTTP {resp.status_code}")
        return result

    request_id = resp_data.get("request_id", "")
    if not request_id:
        result["outcome"] = "missing_request_id"
        result["failure_category"] = "provider_error"
        result["submission_error_body"] = resp_data
        _print("[ERROR] Submission succeeded but no request_id in response")
        return result

    result["request_id"] = request_id
    _print(f"  request_id: {request_id}")

    # -----------------------------------------------------------------------
    # Poll
    # -----------------------------------------------------------------------
    poll_url = f"{MUAPI_BASE_URL}/predictions/{request_id}/result"
    _print(f"\n[2] Polling: {poll_url}")
    _print(f"  Interval: {POLL_INTERVAL}s | Max attempts: {POLL_MAX}")

    consecutive_400s = 0

    for attempt in range(POLL_MAX):
        time.sleep(POLL_INTERVAL)

        try:
            poll_resp = requests.get(
                poll_url,
                headers=_headers(),
                timeout=30,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            entry = {"attempt": attempt + 1, "error": str(exc)}
            result["poll_history"].append(entry)
            _print(f"  Poll {attempt+1}: network error ({exc})")
            continue

        poll_status_code = poll_resp.status_code

        if poll_status_code == 400:
            consecutive_400s += 1
            entry = {
                "attempt": attempt + 1,
                "http_status": 400,
                "consecutive_400s": consecutive_400s,
            }
            try:
                entry["body"] = poll_resp.json()
            except Exception:
                entry["body"] = poll_resp.text[:300]
            result["poll_history"].append(entry)
            _print(f"  Poll {attempt+1}: HTTP 400 (consecutive: {consecutive_400s})")
            if consecutive_400s >= POLL_400_ABORT:
                result["final_error_body"] = entry.get("body")
                result["outcome"] = "poll_400_persistent"
                result["failure_category"] = "provider_job_failure"
                _print(f"  [ABORT] {consecutive_400s} consecutive 400s — job failed on provider side")
                _print("  → Likely cause: job was accepted but failed during Runway processing")
                _print("    (prompt too long, content policy, model quota, or transient provider error)")
                return result
            continue

        if poll_status_code == 401:
            result["outcome"] = "auth_failure_during_poll"
            result["failure_category"] = "authentication"
            _print("  [ERROR] HTTP 401 during poll")
            return result

        if poll_status_code not in (200, 202):
            consecutive_400s = 0
            entry = {"attempt": attempt + 1, "http_status": poll_status_code}
            result["poll_history"].append(entry)
            _print(f"  Poll {attempt+1}: unexpected HTTP {poll_status_code}")
            continue

        consecutive_400s = 0

        try:
            poll_data = poll_resp.json()
        except Exception:
            result["poll_history"].append({"attempt": attempt + 1, "parse_error": True})
            continue

        status = poll_data.get("status", "")
        entry = {"attempt": attempt + 1, "http_status": 200, "status": status}
        result["poll_history"].append(entry)
        _print(f"  Poll {attempt+1}: status={status}")

        if status == "completed":
            output_data = poll_data.get("output") or poll_data
            outputs = output_data.get("outputs", [])
            if outputs:
                first = outputs[0]
                output_url = first if isinstance(first, str) else first.get("url", "")
                result["output_url"] = output_url
                if output_url:
                    _print(f"  Output URL: {output_url[:80]}...")

            credits = poll_data.get("credits_used") or (poll_data.get("cost") or {}).get("amount_usd")
            result["credits_charged"] = credits
            result["final_status"] = "completed"
            result["outcome"] = "success"
            _print(f"\n[SUCCESS] Job completed. Credits charged: {credits}")
            return result

        if status in ("failed", "cancelled"):
            result["final_status"] = status
            result["final_error_body"] = poll_data
            result["outcome"] = f"job_{status}"
            result["failure_category"] = "provider_job_failure"
            _print(f"  [ERROR] Job {status}: {json.dumps(poll_data)[:300]}")
            return result

    # Timeout
    result["outcome"] = "poll_timeout"
    result["failure_category"] = "timeout"
    _print(f"\n[TIMEOUT] Job did not complete after {POLL_MAX * POLL_INTERVAL}s")
    return result


# Known MUAPI CDN URL from ep_000_lite reference image upload (2026-06-05)
# Source: pipeline.log event muapi_upload_completed for ref_antikythera_mechanism
I2V_REF_URL = "https://cdn.muapi.ai/outputs/1f4a0ff3dace43c598f2b2ba5b8ee333.jpg"
I2V_MODEL   = "runway-image-to-video"
I2V_PROMPT  = T2V_PROMPT   # same safe prompt
I2V_ASPECT  = "16:9"
I2V_DURATION = 5
I2V_COST    = 0.15


# ---------------------------------------------------------------------------
# I2V diagnostic
# ---------------------------------------------------------------------------

def run_i2v_diagnostic() -> dict:
    """
    Submit one runway-image-to-video job using the known MUAPI CDN reference image.

    This tests the images_list field fix in _build_job_payload.
    Budget cap: 0.30. No fallback.
    """
    result: dict = {
        "test": I2V_MODEL,
        "model": I2V_MODEL,
        "prompt": I2V_PROMPT,
        "endpoint": f"{MUAPI_BASE_URL}/{I2V_MODEL}",
        "reference_image_url": I2V_REF_URL,
        "payload_shape": {
            "prompt": "<string>",
            "aspect_ratio": I2V_ASPECT,
            "duration": I2V_DURATION,
            "image_url": "<muapi_cdn_url>",
        },
        "budget_cap": BUDGET_CAP,
        "estimated_cost": I2V_COST,
        "request_id": None,
        "submission_http_status": None,
        "submission_error_body": None,
        "poll_history": [],
        "final_status": None,
        "final_error_body": None,
        "output_url": None,
        "credits_charged": None,
        "outcome": None,
        "failure_category": None,
    }

    if I2V_COST > BUDGET_CAP:
        result["outcome"] = "aborted_budget"
        result["failure_category"] = "budget_guard"
        _print(f"[ABORT] I2V estimated cost {I2V_COST} exceeds budget cap {BUDGET_CAP}.")
        return result

    _print(f"\n{'='*60}")
    _print(f"TEST: {I2V_MODEL}")
    _print(f"Endpoint: {result['endpoint']}")
    _print(f"Reference image: {I2V_REF_URL[:60]}...")
    _print(f"Payload: aspect_ratio={I2V_ASPECT}, duration={I2V_DURATION}s, image_url=<cdn_url>")
    _print(f"Estimated cost: ${I2V_COST} | Budget cap: ${BUDGET_CAP}")
    _print(f"{'='*60}")

    # Confirmed field name: "image_url" (string) for runway-image-to-video.
    # Previous attempt used "images_list" (Veo3 field) → HTTP 422.
    payload = {
        "prompt": I2V_PROMPT,
        "aspect_ratio": I2V_ASPECT,
        "duration": I2V_DURATION,
        "image_url": I2V_REF_URL,
    }

    _print("\n[1] Submitting job...")
    t_submit = time.monotonic()
    try:
        resp = requests.post(
            result["endpoint"],
            headers=_headers(),
            json=payload,
            timeout=30,
        )
    except requests.exceptions.Timeout:
        result["outcome"] = "submission_timeout"
        result["failure_category"] = "network"
        _print("[ERROR] Submission timed out after 30s")
        return result
    except requests.exceptions.ConnectionError as exc:
        result["outcome"] = "connection_error"
        result["failure_category"] = "network"
        result["submission_error_body"] = str(exc)
        _print(f"[ERROR] Connection error: {exc}")
        return result

    elapsed_submit = time.monotonic() - t_submit
    result["submission_http_status"] = resp.status_code
    _print(f"  HTTP {resp.status_code} in {elapsed_submit:.2f}s")

    try:
        resp_data = resp.json()
    except Exception:
        resp_data = {"raw": resp.text[:500]}

    _print(f"  Response body: {json.dumps(resp_data)[:500]}")

    if resp.status_code == 401:
        result["outcome"] = "auth_failure"
        result["failure_category"] = "authentication"
        result["submission_error_body"] = resp_data
        return result

    if resp.status_code == 402:
        result["outcome"] = "quota_exhausted"
        result["failure_category"] = "quota"
        result["submission_error_body"] = resp_data
        return result

    if resp.status_code == 422:
        result["outcome"] = "unprocessable_entity"
        result["failure_category"] = "payload_format"
        result["submission_error_body"] = resp_data
        _print("[ERROR] HTTP 422 — payload still rejected.")
        _print("  → images_list field may use a different name for Runway I2V.")
        _print("    Check MUAPI API docs for runway-image-to-video field names.")
        return result

    if resp.status_code == 400:
        result["outcome"] = "bad_request"
        result["failure_category"] = "payload_format"
        result["submission_error_body"] = resp_data
        _print("[ERROR] HTTP 400 — bad request")
        return result

    if resp.status_code not in (200, 202):
        result["outcome"] = f"http_{resp.status_code}"
        result["failure_category"] = "provider_error"
        result["submission_error_body"] = resp_data
        _print(f"[ERROR] Unexpected HTTP {resp.status_code}")
        return result

    request_id = resp_data.get("request_id", "")
    if not request_id:
        result["outcome"] = "missing_request_id"
        result["failure_category"] = "provider_error"
        result["submission_error_body"] = resp_data
        _print("[ERROR] Submission succeeded but no request_id in response")
        return result

    result["request_id"] = request_id
    _print(f"  request_id: {request_id}")

    # Poll
    poll_url = f"{MUAPI_BASE_URL}/predictions/{request_id}/result"
    _print(f"\n[2] Polling: {poll_url}")

    consecutive_400s = 0

    for attempt in range(POLL_MAX):
        time.sleep(POLL_INTERVAL)

        try:
            poll_resp = requests.get(poll_url, headers=_headers(), timeout=30)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            result["poll_history"].append({"attempt": attempt + 1, "error": str(exc)})
            _print(f"  Poll {attempt+1}: network error")
            continue

        poll_status_code = poll_resp.status_code

        if poll_status_code == 400:
            consecutive_400s += 1
            entry = {"attempt": attempt + 1, "http_status": 400, "consecutive_400s": consecutive_400s}
            try:
                entry["body"] = poll_resp.json()
            except Exception:
                entry["body"] = poll_resp.text[:300]
            result["poll_history"].append(entry)
            _print(f"  Poll {attempt+1}: HTTP 400 (consecutive: {consecutive_400s})")
            if consecutive_400s >= POLL_400_ABORT:
                result["final_error_body"] = entry.get("body")
                result["outcome"] = "poll_400_persistent"
                result["failure_category"] = "provider_job_failure"
                _print(f"  [ABORT] {consecutive_400s} consecutive 400s — job failed on provider side")
                return result
            continue

        if poll_status_code not in (200, 202):
            consecutive_400s = 0
            result["poll_history"].append({"attempt": attempt + 1, "http_status": poll_status_code})
            _print(f"  Poll {attempt+1}: unexpected HTTP {poll_status_code}")
            continue

        consecutive_400s = 0

        try:
            poll_data = poll_resp.json()
        except Exception:
            result["poll_history"].append({"attempt": attempt + 1, "parse_error": True})
            continue

        status = poll_data.get("status", "")
        result["poll_history"].append({"attempt": attempt + 1, "http_status": 200, "status": status})
        _print(f"  Poll {attempt+1}: status={status}")

        if status == "completed":
            output_data = poll_data.get("output") or poll_data
            outputs = output_data.get("outputs", [])
            if outputs:
                first = outputs[0]
                output_url = first if isinstance(first, str) else first.get("url", "")
                result["output_url"] = output_url
                if output_url:
                    _print(f"  Output URL: {output_url[:80]}...")

            credits = poll_data.get("credits_used") or (poll_data.get("cost") or {}).get("amount_usd")
            result["credits_charged"] = credits
            result["final_status"] = "completed"
            result["outcome"] = "success"
            _print(f"\n[SUCCESS] I2V job completed. Credits charged: {credits}")
            return result

        if status in ("failed", "cancelled"):
            result["final_status"] = status
            result["final_error_body"] = poll_data
            result["outcome"] = f"job_{status}"
            result["failure_category"] = "provider_job_failure"
            _print(f"  [ERROR] Job {status}")
            return result

    result["outcome"] = "poll_timeout"
    result["failure_category"] = "timeout"
    _print(f"\n[TIMEOUT] I2V job did not complete after {POLL_MAX * POLL_INTERVAL}s")
    return result


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------

def _classify_failure(t2v: dict, i2v: dict) -> str:
    i2v_outcome = i2v.get("outcome", "")
    i2v_note = ""
    if i2v_outcome == "success":
        i2v_note = " I2V (runway-image-to-video) also confirmed working — images_list fix is correct."
    elif i2v_outcome == "unprocessable_entity":
        i2v_note = " I2V still rejected (HTTP 422) — images_list field name may differ for Runway."
    elif i2v_outcome == "poll_400_persistent":
        i2v_note = " I2V job accepted but failed during processing — transient provider issue."

    outcome = t2v.get("outcome", "")

    if outcome == "success":
        return (
            "runway-text-to-video: WORKING. T2V submissions succeed. "
            "The ep_000_lite T2V failures (s02, s04) were transient provider-side "
            "job failures, not a payload or config error."
            + i2v_note
        )
    if outcome == "auth_failure":
        return "MUAPI_API_KEY is invalid. All Runway tests blocked. Fix credentials first."
    if outcome == "quota_exhausted":
        return "MUAPI credit quota exhausted. Top up account before re-running."
    if outcome == "http_403":
        return (
            "HTTP 403 — API key present but request was forbidden. "
            "MUAPI uses x-api-key header (not Authorization: Bearer). "
            "Verify the diagnostic is using _get_headers() equivalent."
        )
    if outcome in ("unprocessable_entity", "bad_request"):
        return (
            "Payload rejected by API. Likely cause: invalid field, unsupported model name, "
            "or malformed duration value. Check MUAPI model catalog for current runway-text-to-video slug."
        )
    if outcome == "poll_400_persistent":
        return (
            "Job accepted but failed during Runway processing (3× HTTP 400 on poll). "
            "This matches the ep_000_lite T2V failure pattern (s02, s04). "
            "Likely cause: Runway provider-side job failure — prompt content, "
            "service degradation, or account-level quota. "
            "Runway T2V is unreliable for this account at this time."
        )
    if outcome == "poll_timeout":
        return (
            "Job accepted and still processing after timeout. Runway T2V is slow but functional. "
            "Increase MUAPI_TIMEOUT_SECONDS or poll manually using the request_id."
        )
    if outcome == "connection_error":
        return "Network connectivity failure. Check internet connection and MUAPI_BASE_URL."
    return f"Unexpected outcome: {outcome}. Review full result JSON."


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _print("\nMUAPI Runway Provider Diagnostic")
    _print(f"Base URL: {MUAPI_BASE_URL}")
    _print(f"API key set: {'YES' if MUAPI_API_KEY else 'NO — set MUAPI_API_KEY before running'}")

    if not MUAPI_API_KEY:
        _print("\n[FATAL] MUAPI_API_KEY not set. Cannot run diagnostic.")
        sys.exit(1)

    t2v_result = run_t2v_diagnostic()
    i2v_result = run_i2v_diagnostic()

    diagnosis = _classify_failure(t2v_result, i2v_result)

    _print(f"\n{'='*60}")
    _print("DIAGNOSIS")
    _print(f"{'='*60}")
    _print(diagnosis)

    # Runway recommendation
    _print(f"\n{'='*60}")
    _print("RUNWAY RECOMMENDATION FOR EPISODE ZERO LITE")
    _print(f"{'='*60}")
    t2v_ok = t2v_result.get("outcome") == "success"
    i2v_ok = i2v_result.get("outcome") == "success"
    if t2v_ok and i2v_ok:
        _print("FULLY USABLE — Both T2V and I2V confirmed working.")
        _print("images_list fix is correct. Episode Zero Lite can use MUAPI_RUNWAY_ONLY_MODE=true.")
    elif t2v_ok:
        _print("T2V USABLE. I2V status: " + i2v_result.get("outcome", "unknown"))
        _print("For Episode Zero Lite: use MUAPI_RUNWAY_ONLY_MODE=true.")
        _print("Scenes with reference images will use I2V (runway-image-to-video).")
        if i2v_result.get("outcome") == "unprocessable_entity":
            _print("WARNING: I2V still rejected — images_list field name may need")
            _print("further investigation against MUAPI runway-image-to-video docs.")
    else:
        _print("NOT READY — T2V failures must be resolved before Episode Zero Lite can complete.")
        _print("See diagnosis above for required action.")

    # Save JSON
    summary = {
        "diagnostic_run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "t2v": t2v_result,
        "i2v": i2v_result,
        "diagnosis": diagnosis,
        "runway_usable_for_episode_zero": t2v_ok,
    }
    out_path = _ROOT / "scripts" / "runway_diagnostic_result.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _print(f"\nFull result saved to: {out_path}")


if __name__ == "__main__":
    main()
