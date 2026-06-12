"""
scripts/seedance_diagnostic.py — Minimal Seedance provider diagnostic

Measures Seedance 2.0 via MUAPI outside the pipeline.

Tests:
  1. seedance-v2.0-t2v  — text-to-video
  2. seedance-v2.0-i2v  — image-to-video (uses a known MUAPI CDN image URL)

Both tests use the same prompt and aspect ratio as the Runway diagnostic so
results are directly comparable.

Budget cap: 0.30 credits. Aborts before submission if cost would exceed cap.
If T2V cost is unknown (model not in MODEL_CREDIT_COSTS), cost defaults to
UNKNOWN_COST_ESTIMATE and the budget check uses that estimate.

Usage:
    python3 scripts/seedance_diagnostic.py

Requires .env with MUAPI_API_KEY set (or environment variable).
Output: human-readable report to stdout + JSON summary to
    scripts/seedance_diagnostic_result.json
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
    pass

import requests  # noqa: E402
from hypogeum.config import Config  # noqa: E402

# ---------------------------------------------------------------------------
# Config — read from .env exactly as the pipeline does
# ---------------------------------------------------------------------------
_config = Config()
MUAPI_API_KEY  = _config.MUAPI_API_KEY
MUAPI_BASE_URL = _config.MUAPI_BASE_URL

# ---------------------------------------------------------------------------
# Models under test
# These are the MUAPI model IDs for Seedance 2.0. If the API returns 404 or
# 422 with an "invalid model" message, the model ID is wrong — record it and
# probe the error body for the correct name.
# ---------------------------------------------------------------------------
T2V_MODEL = "seedance-v2.0-t2v"
I2V_MODEL = "seedance-v2.0-i2v"

# Same prompt as Runway diagnostic — enables direct comparison
DIAGNOSTIC_PROMPT = (
    "A slow cinematic shot of an ancient bronze astronomical device "
    "on a dark museum table, no people, no text, no dialogue."
)
ASPECT_RATIO = "16:9"
DURATION     = 5        # seconds — probe whether Seedance accepts this or has its own enum

# Reference image for I2V — reuse the same MUAPI CDN image confirmed in Runway diagnostic
# (flux-dev-image output, confirmed public URL as of 2026-06-06)
I2V_REFERENCE_IMAGE_URL = "https://cdn.muapi.ai/outputs/1f4a0ff3dace43c598f2b2ba5b8ee333.jpg"

# Cost estimates — unknown until first probe; use $0.30 as conservative upper bound
# (Runway confirmed $0.15/clip; Seedance mini announced at ~$0.073/sec = ~$0.37 for 5s)
# Update these after first successful run reports actual credits_charged.
UNKNOWN_COST_ESTIMATE = 0.30   # assume worst case until measured
BUDGET_CAP            = 0.60   # higher cap than Runway — Seedance cost unknown; 2 tests × $0.30

POLL_INTERVAL   = 6     # seconds between polls
POLL_MAX        = 30    # max ~180 seconds — Seedance may be slower than Runway
POLL_400_ABORT  = 3     # consecutive 400s → abort poll


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    return {
        "x-api-key": MUAPI_API_KEY,
        "Content-Type": "application/json",
    }


def _redact(s: str) -> str:
    if MUAPI_API_KEY and MUAPI_API_KEY in s:
        return s.replace(MUAPI_API_KEY, "[REDACTED]")
    return s


def _print(msg: str = "") -> None:
    print(msg, flush=True)


def _empty_result(test_name: str, model: str, endpoint: str) -> dict:
    return {
        "test": test_name,
        "model": model,
        "prompt": DIAGNOSTIC_PROMPT,
        "endpoint": endpoint,
        "payload_shape": {},
        "budget_cap": BUDGET_CAP,
        "estimated_cost": UNKNOWN_COST_ESTIMATE,
        "request_id": None,
        "submission_http_status": None,
        "submission_error_body": None,
        "poll_history": [],
        "final_status": None,
        "final_error_body": None,
        "output_url": None,
        "credits_charged": None,
        "generation_seconds": None,
        "outcome": None,
        "failure_category": None,
        "field_name_notes": "",
    }


# ---------------------------------------------------------------------------
# Poll loop — shared by T2V and I2V
# ---------------------------------------------------------------------------

def _poll(result: dict, request_id: str) -> dict:
    poll_url = f"{MUAPI_BASE_URL}/predictions/{request_id}/result"
    _print(f"\n[2] Polling: {poll_url}")
    _print(f"  Interval: {POLL_INTERVAL}s | Max attempts: {POLL_MAX}")

    consecutive_400s = 0
    t_poll_start = time.monotonic()

    for attempt in range(POLL_MAX):
        time.sleep(POLL_INTERVAL)

        try:
            poll_resp = requests.get(poll_url, headers=_headers(), timeout=30)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            result["poll_history"].append({"attempt": attempt + 1, "error": str(exc)})
            _print(f"  Poll {attempt+1}: network error ({exc})")
            continue

        poll_status_code = poll_resp.status_code

        try:
            poll_data = poll_resp.json()
        except Exception:
            poll_data = {"raw": poll_resp.text[:500]}

        if poll_status_code == 400:
            consecutive_400s += 1
            result["poll_history"].append({
                "attempt": attempt + 1,
                "http_status": 400,
                "body": poll_data,
            })
            _print(f"  Poll {attempt+1}: HTTP 400 (consecutive: {consecutive_400s})")
            if consecutive_400s >= POLL_400_ABORT:
                result["outcome"] = "poll_400_abort"
                result["failure_category"] = "provider_transient"
                result["final_error_body"] = poll_data
                _print(f"[ABORT] {POLL_400_ABORT} consecutive HTTP 400s — provider job failed")
                return result
            continue

        consecutive_400s = 0

        status = poll_data.get("status", "")
        result["poll_history"].append({
            "attempt": attempt + 1,
            "http_status": poll_status_code,
            "status": status,
        })
        _print(f"  Poll {attempt+1}: HTTP {poll_status_code}, status={status!r}")

        if status == "completed":
            elapsed = time.monotonic() - t_poll_start
            result["generation_seconds"] = round(elapsed, 1)

            # Extract output URL — check common field names
            output_url = None
            for field in ("video_url", "output_url", "url", "video", "output"):
                val = poll_data.get(field)
                if val and isinstance(val, str) and val.startswith("http"):
                    output_url = val
                    break
                if val and isinstance(val, list) and val:
                    output_url = val[0]
                    break

            result["output_url"] = output_url
            result["credits_charged"] = poll_data.get("credits_charged")
            result["outcome"] = "success"
            _print(f"\n  [OK] Completed in {elapsed:.1f}s")
            _print(f"  Output URL: {output_url}")
            _print(f"  Credits charged: {result['credits_charged']}")
            return result

        if status in ("failed", "cancelled"):
            result["outcome"] = f"job_{status}"
            result["failure_category"] = "provider_error"
            result["final_error_body"] = poll_data
            result["generation_seconds"] = round(time.monotonic() - t_poll_start, 1)
            _print(f"\n  [FAIL] Job {status}")
            return result

    result["outcome"] = "poll_timeout"
    result["failure_category"] = "timeout"
    result["generation_seconds"] = round(time.monotonic() - t_poll_start, 1)
    _print(f"\n  [TIMEOUT] {POLL_MAX} polls exhausted without completion")
    return result


# ---------------------------------------------------------------------------
# Submit helper — returns (result, request_id_or_None)
# ---------------------------------------------------------------------------

def _submit(result: dict, payload: dict) -> Optional[str]:
    _print(f"\n[1] Submitting job to {result['endpoint']}...")
    _print(f"  Payload: {json.dumps(payload)}")
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
        return None
    except requests.exceptions.ConnectionError as exc:
        result["outcome"] = "connection_error"
        result["failure_category"] = "network"
        result["submission_error_body"] = str(exc)
        _print(f"[ERROR] Connection error: {exc}")
        return None

    elapsed = time.monotonic() - t_submit
    result["submission_http_status"] = resp.status_code
    _print(f"  HTTP {resp.status_code} in {elapsed:.2f}s")

    try:
        resp_data = resp.json()
    except Exception:
        resp_data = {"raw": resp.text[:500]}

    _print(f"  Response body: {json.dumps(resp_data)[:600]}")

    if resp.status_code == 401:
        result["outcome"] = "auth_failure"
        result["failure_category"] = "authentication"
        result["submission_error_body"] = resp_data
        _print("[ERROR] HTTP 401 — MUAPI_API_KEY invalid or missing")
        return None

    if resp.status_code == 402:
        result["outcome"] = "quota_exhausted"
        result["failure_category"] = "quota"
        result["submission_error_body"] = resp_data
        _print("[ERROR] HTTP 402 — MUAPI credit quota exhausted")
        return None

    if resp.status_code == 404:
        result["outcome"] = "model_not_found"
        result["failure_category"] = "unknown_model"
        result["submission_error_body"] = resp_data
        _print("[ERROR] HTTP 404 — model ID not found. Check model name in MUAPI.")
        _print(f"  Tried model: {result['model']}")
        _print("  → Inspect response body above for correct model name or available models.")
        return None

    if resp.status_code == 422:
        result["outcome"] = "unprocessable_entity"
        result["failure_category"] = "payload_format"
        result["submission_error_body"] = resp_data
        _print("[ERROR] HTTP 422 — Payload rejected. Check field names in response body above.")
        _print("  → This is diagnostic data: note which field name the API reports as missing.")
        return None

    if resp.status_code == 400:
        result["outcome"] = "bad_request"
        result["failure_category"] = "payload_format"
        result["submission_error_body"] = resp_data
        _print("[ERROR] HTTP 400 — Bad request")
        return None

    if resp.status_code not in (200, 202):
        result["outcome"] = f"http_{resp.status_code}"
        result["failure_category"] = "provider_error"
        result["submission_error_body"] = resp_data
        _print(f"[ERROR] Unexpected HTTP {resp.status_code}")
        return None

    request_id = resp_data.get("request_id", "")
    if not request_id:
        result["outcome"] = "missing_request_id"
        result["failure_category"] = "provider_error"
        result["submission_error_body"] = resp_data
        _print("[ERROR] Submission succeeded but no request_id in response")
        return None

    result["request_id"] = request_id
    _print(f"  request_id: {request_id}")
    return request_id


# ---------------------------------------------------------------------------
# T2V diagnostic
# ---------------------------------------------------------------------------

def run_t2v_diagnostic(spend_so_far: float) -> dict:
    endpoint = f"{MUAPI_BASE_URL}/{T2V_MODEL}"
    result = _empty_result("seedance-t2v", T2V_MODEL, endpoint)

    # Budget check — cost unknown; use estimate
    remaining = BUDGET_CAP - spend_so_far
    if UNKNOWN_COST_ESTIMATE > remaining:
        result["outcome"] = "aborted_budget"
        result["failure_category"] = "budget_guard"
        _print(f"[ABORT] Estimated cost ${UNKNOWN_COST_ESTIMATE} exceeds remaining budget "
               f"${remaining:.2f}. Not submitting.")
        return result

    _print(f"\n{'='*60}")
    _print(f"TEST 1: {T2V_MODEL}")
    _print(f"Endpoint: {endpoint}")
    _print(f"Prompt: {DIAGNOSTIC_PROMPT}")
    _print(f"Estimated cost: ~${UNKNOWN_COST_ESTIMATE} (unconfirmed) | Budget cap: ${BUDGET_CAP}")
    _print(f"{'='*60}")

    # Probe payload — use the same field shape as Runway T2V to start.
    # If the model requires different fields, the 422 body will tell us.
    payload = {
        "prompt": DIAGNOSTIC_PROMPT,
        "aspect_ratio": ASPECT_RATIO,
        "duration": DURATION,
    }
    result["payload_shape"] = {
        "prompt": "<string>",
        "aspect_ratio": ASPECT_RATIO,
        "duration": DURATION,
    }

    request_id = _submit(result, payload)
    if request_id is None:
        return result

    return _poll(result, request_id)


# ---------------------------------------------------------------------------
# I2V diagnostic
# ---------------------------------------------------------------------------

def run_i2v_diagnostic(spend_so_far: float) -> dict:
    endpoint = f"{MUAPI_BASE_URL}/{I2V_MODEL}"
    result = _empty_result("seedance-i2v", I2V_MODEL, endpoint)
    result["reference_image_url"] = I2V_REFERENCE_IMAGE_URL

    # Budget check
    remaining = BUDGET_CAP - spend_so_far
    if UNKNOWN_COST_ESTIMATE > remaining:
        result["outcome"] = "aborted_budget"
        result["failure_category"] = "budget_guard"
        _print(f"[ABORT] Estimated cost ${UNKNOWN_COST_ESTIMATE} exceeds remaining budget "
               f"${remaining:.2f}. Not submitting.")
        return result

    _print(f"\n{'='*60}")
    _print(f"TEST 2: {I2V_MODEL}")
    _print(f"Endpoint: {endpoint}")
    _print(f"Prompt: {DIAGNOSTIC_PROMPT}")
    _print(f"Reference image: {I2V_REFERENCE_IMAGE_URL}")
    _print(f"Estimated cost: ~${UNKNOWN_COST_ESTIMATE} (unconfirmed) | Budget cap: ${BUDGET_CAP}")
    _print(f"{'='*60}")

    # Probe I2V payload.
    # Veo3 I2V uses "images_list": [url] (array).
    # Runway I2V uses "image_url": url (string).
    # Seedance field name is unknown — using "images_list" first (Veo3 convention).
    # If HTTP 422 → inspect body for correct field name, then change and re-run manually.
    images_field_used = "images_list"
    payload = {
        "prompt": DIAGNOSTIC_PROMPT,
        "aspect_ratio": ASPECT_RATIO,
        "duration": DURATION,
        "images_list": [I2V_REFERENCE_IMAGE_URL],
    }
    result["payload_shape"] = {
        "prompt": "<string>",
        "aspect_ratio": ASPECT_RATIO,
        "duration": DURATION,
        images_field_used: ["<muapi_cdn_url>"],
    }
    result["field_name_notes"] = (
        f"I2V field used: '{images_field_used}' (array, Veo3 convention). "
        "If HTTP 422, inspect submission_error_body for correct field name."
    )

    request_id = _submit(result, payload)
    if request_id is None:
        # Annotate 422 failures specifically — this is the most diagnostic outcome
        if result.get("outcome") == "unprocessable_entity":
            err = result.get("submission_error_body") or {}
            loc = err.get("loc") or err.get("detail", "")
            result["field_name_notes"] += (
                f" — 422 error loc: {loc!r}. "
                "Update I2V_FIELD_NAME in this script and re-run."
            )
        return result

    return _poll(result, request_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _print("=" * 60)
    _print("SEEDANCE 2.0 MUAPI DIAGNOSTIC")
    _print(f"Base URL: {MUAPI_BASE_URL}")
    _print(f"Budget cap: ${BUDGET_CAP}")
    _print(f"API key present: {'yes' if MUAPI_API_KEY else 'NO — MUAPI_API_KEY not set'}")
    _print("=" * 60)

    if not MUAPI_API_KEY:
        _print("\n[FATAL] MUAPI_API_KEY is not set. Set it in .env or as an environment variable.")
        sys.exit(1)

    t_start = time.monotonic()

    # Run T2V
    t2v_result = run_t2v_diagnostic(spend_so_far=0.0)

    # Track spend — use actual credits_charged if available, else estimate
    t2v_spend = t2v_result.get("credits_charged") or (
        UNKNOWN_COST_ESTIMATE if t2v_result.get("outcome") == "success" else 0.0
    )

    # Run I2V only if T2V succeeded or was a model/payload error (not auth/quota)
    skip_i2v = t2v_result.get("failure_category") in ("authentication", "quota")
    if skip_i2v:
        _print("\n[SKIP] I2V test skipped — T2V returned auth/quota failure. Fix first.")
        i2v_result = {"outcome": "skipped", "reason": t2v_result.get("failure_category")}
    else:
        i2v_result = run_i2v_diagnostic(spend_so_far=t2v_spend)

    total_elapsed = time.monotonic() - t_start

    # Determine overall diagnosis
    t2v_ok = t2v_result.get("outcome") == "success"
    i2v_ok = i2v_result.get("outcome") == "success"

    t2v_model_missing = t2v_result.get("failure_category") == "unknown_model"
    i2v_model_missing = i2v_result.get("failure_category") == "unknown_model"
    i2v_wrong_field   = i2v_result.get("outcome") == "unprocessable_entity"

    if t2v_ok and i2v_ok:
        diagnosis = (
            f"{T2V_MODEL}: WORKING. {I2V_MODEL}: WORKING. "
            f"Both Seedance models confirmed operational via MUAPI. "
            f"T2V cost: ${t2v_result.get('credits_charged')} | "
            f"I2V cost: ${i2v_result.get('credits_charged')}. "
            f"Seedance is ready for Hypogeum integration."
        )
    elif t2v_ok and i2v_wrong_field:
        diagnosis = (
            f"{T2V_MODEL}: WORKING. {I2V_MODEL}: FIELD NAME WRONG. "
            f"I2V rejected 'images_list' — inspect i2v.submission_error_body.loc for correct field. "
            f"T2V cost confirmed: ${t2v_result.get('credits_charged')}."
        )
    elif t2v_model_missing:
        diagnosis = (
            f"{T2V_MODEL}: MODEL NOT FOUND (HTTP 404). "
            f"The model ID '{T2V_MODEL}' is not available on this MUAPI account. "
            f"Check MUAPI dashboard for available Seedance model IDs."
        )
    elif t2v_ok:
        diagnosis = (
            f"{T2V_MODEL}: WORKING. {I2V_MODEL}: FAILED ({i2v_result.get('outcome')}). "
            f"T2V cost confirmed: ${t2v_result.get('credits_charged')}. "
            f"Investigate I2V failure separately."
        )
    else:
        diagnosis = (
            f"{T2V_MODEL}: FAILED ({t2v_result.get('outcome')}, "
            f"category={t2v_result.get('failure_category')}). "
            f"Investigate before attempting I2V."
        )

    _print(f"\n\n{'='*60}")
    _print("DIAGNOSIS")
    _print("=" * 60)
    _print(diagnosis)
    _print(f"\nTotal elapsed: {total_elapsed:.1f}s")

    # Build summary
    summary = {
        "diagnostic_run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "t2v": t2v_result,
        "i2v": i2v_result,
        "diagnosis": diagnosis,
        "seedance_usable_for_episode_zero": t2v_ok,
    }

    out_path = Path(__file__).parent / "seedance_diagnostic_result.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _print(f"\nJSON summary written to: {out_path}")


if __name__ == "__main__":
    main()
