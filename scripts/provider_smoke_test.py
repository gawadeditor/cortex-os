"""
scripts/provider_smoke_test.py — Phase 1C: Provider Live Smoke Test

Verifies connectivity and response parsing for each image provider.
One request per provider. No quality evaluation. No benchmark ranking.

Providers tested:
  openai     — POST /v1/images/generations, model gpt-image-1, parser: b64_json
  openrouter — POST /api/v1/chat/completions, modalities:["image"], parser: data_uri or url
  muapi      — POST /api/v1/gpt-image-2-text-to-image + poll, parser: outputs[]

Prompt: "black square with a small gold circle in the center"

Outputs (under .hypogeum-assets/provider_smoke_tests/):
  {provider}/output.jpg       — image if success
  {provider}/request.json     — payload with secrets redacted
  {provider}/response_meta.json — raw response metadata with secrets redacted
  provider_live_smoke_report.json — consolidated report

Run:
    python3 scripts/provider_smoke_test.py

No secrets printed. No retries beyond one. Skips if key missing.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.config import Config

SMOKE_PROMPT   = "black square with a small gold circle in the center"
OUTPUT_BASE    = Path(__file__).resolve().parent.parent / ".hypogeum-assets" / "provider_smoke_tests"
POLL_INTERVAL  = 5   # seconds between MUAPI polls
POLL_MAX       = 30  # max poll attempts (150s timeout)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(s: str, *secrets: Optional[str]) -> str:
    """Replace each secret value with *** in s."""
    for secret in secrets:
        if secret and len(secret) > 4:
            s = s.replace(secret, "***")
    return s


def _save(directory: Path, filename: str, data) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(json.dumps(data, indent=2))


def _get_requests():
    """Import requests (already a project dependency via muapi.py)."""
    try:
        import requests
        return requests
    except ImportError as exc:
        raise RuntimeError("requests is required: pip install requests") from exc


# ---------------------------------------------------------------------------
# OpenAI direct smoke test
# ---------------------------------------------------------------------------

def smoke_openai(config: Config) -> dict:
    """
    POST https://api.openai.com/v1/images/generations
    model: gpt-image-1, quality: low (cheapest), size: 1024x1024
    parser: b64_json → base64 decode → write bytes
    """
    provider   = "openai"
    out_dir    = OUTPUT_BASE / provider

    if not config.OPENAI_API_KEY:
        result = {
            "provider": provider, "status": "skipped_no_key",
            "model": config.OPENAI_IMAGE_MODEL, "endpoint": f"{config.OPENAI_IMAGE_BASE_URL}/images/generations",
            "latency_ms": None, "parser_path": "b64_json",
            "note": "OPENAI_API_KEY not set",
        }
        _save(out_dir, "response_meta.json", result)
        return result

    endpoint = f"{config.OPENAI_IMAGE_BASE_URL}/images/generations"
    model    = config.OPENAI_IMAGE_MODEL
    payload  = {
        "model":           model,
        "prompt":          SMOKE_PROMPT,
        "n":               1,
        "size":            "1024x1024",   # square — cheapest
        "quality":         "low",         # lowest cost
        "response_format": "b64_json",
    }

    _save(out_dir, "request.json", {
        "endpoint": endpoint,
        "method":   "POST",
        "headers":  {"Authorization": "Bearer ***", "Content-Type": "application/json"},
        "payload":  payload,
    })

    requests = _get_requests()
    t0 = time.monotonic()
    try:
        resp = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {config.OPENAI_API_KEY}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=180,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        result = {
            "provider": provider, "status": "error_connection", "model": model,
            "endpoint": endpoint, "latency_ms": latency_ms, "parser_path": "b64_json",
            "error": _redact(str(exc), config.OPENAI_API_KEY),
        }
        _save(out_dir, "response_meta.json", result)
        return result

    # Capture response metadata (no secret in body, but redact just in case)
    raw_text = _redact(resp.text[:4000], config.OPENAI_API_KEY)

    if resp.status_code != 200:
        result = {
            "provider": provider, "status": f"error_http_{resp.status_code}",
            "model": model, "endpoint": endpoint, "latency_ms": latency_ms,
            "parser_path": "b64_json", "http_status": resp.status_code,
            "response_body_truncated": raw_text,
        }
        _save(out_dir, "response_meta.json", result)
        return result

    try:
        body  = resp.json()
        data  = body.get("data", [])
        entry = data[0] if data else {}
        b64   = entry.get("b64_json", "")
        if not b64:
            raise ValueError(f"No b64_json in response. Keys: {list(entry.keys())}")
        image_bytes = base64.b64decode(b64)
    except Exception as exc:
        result = {
            "provider": provider, "status": "error_parse",
            "model": model, "endpoint": endpoint, "latency_ms": latency_ms,
            "parser_path": "b64_json", "error": str(exc),
            "response_keys": list(resp.json().keys()) if resp.text else [],
        }
        _save(out_dir, "response_meta.json", result)
        return result

    _save(out_dir, "output.jpg", image_bytes)

    result = {
        "provider": provider, "status": "success",
        "model": model, "endpoint": endpoint, "latency_ms": latency_ms,
        "parser_path": "b64_json",
        "image_bytes": len(image_bytes),
        "image_path": str(out_dir / "output.jpg"),
        "response_meta": {
            "http_status":     resp.status_code,
            "data_count":      len(data),
            "revised_prompt":  entry.get("revised_prompt", "")[:200],
        },
    }
    _save(out_dir, "response_meta.json", result)
    return result


# ---------------------------------------------------------------------------
# OpenRouter smoke test
# ---------------------------------------------------------------------------

def smoke_openrouter(config: Config) -> dict:
    """
    POST https://openrouter.ai/api/v1/chat/completions
    modalities: ["image"]
    parser: choices[0].message.images[] → image_url → data: URI (base64)
    Live-tested shape: content=null, image in message.images[0].image_url.url
    """
    provider   = "openrouter"
    out_dir    = OUTPUT_BASE / provider
    endpoint   = "https://openrouter.ai/api/v1/chat/completions"
    model      = config.OPENROUTER_IMAGE_MODEL

    if not config.OPENROUTER_API_KEY:
        result = {
            "provider": provider, "status": "skipped_no_key",
            "model": model, "endpoint": endpoint,
            "latency_ms": None, "parser_path": "choices[0].message.content[] image_url",
            "note": "OPENROUTER_API_KEY not set",
        }
        _save(out_dir, "response_meta.json", result)
        return result

    payload = {
        "model":      model,
        "messages":   [{"role": "user", "content": SMOKE_PROMPT}],
        "modalities": ["image"],
    }

    _save(out_dir, "request.json", {
        "endpoint": endpoint,
        "method":   "POST",
        "headers":  {
            "Authorization": "Bearer ***",
            "Content-Type":  "application/json",
            "HTTP-Referer":  config.OPENROUTER_APP_URL or "https://hypogeum.ai",
            "X-Title":       config.OPENROUTER_APP_NAME or "hypogeum-brain",
        },
        "payload": payload,
    })

    requests = _get_requests()
    t0 = time.monotonic()
    try:
        resp = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                "Content-Type":  "application/json",
                "HTTP-Referer":  config.OPENROUTER_APP_URL or "https://hypogeum.ai",
                "X-Title":       config.OPENROUTER_APP_NAME or "hypogeum-brain",
            },
            json=payload,
            timeout=180,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        result = {
            "provider": provider, "status": "error_connection", "model": model,
            "endpoint": endpoint, "latency_ms": latency_ms,
            "parser_path": "choices[0].message.content[] image_url",
            "error": _redact(str(exc), config.OPENROUTER_API_KEY),
        }
        _save(out_dir, "response_meta.json", result)
        return result

    raw_text = _redact(resp.text[:4000], config.OPENROUTER_API_KEY)

    if resp.status_code != 200:
        result = {
            "provider": provider, "status": f"error_http_{resp.status_code}",
            "model": model, "endpoint": endpoint, "latency_ms": latency_ms,
            "parser_path": "choices[0].message.content[] image_url",
            "http_status": resp.status_code,
            "response_body_truncated": raw_text,
            "note": "Check model availability: GET /api/v1/models?output_modalities=image",
        }
        _save(out_dir, "response_meta.json", result)
        return result

    # Record raw response shape regardless of parse outcome
    try:
        body = resp.json()
    except Exception:
        result = {
            "provider": provider, "status": "error_not_json", "model": model,
            "endpoint": endpoint, "latency_ms": latency_ms,
            "parser_path": "choices[0].message.content[] image_url",
            "response_body_truncated": raw_text,
        }
        _save(out_dir, "response_meta.json", result)
        return result

    choices = body.get("choices", [])
    message = choices[0].get("message", {}) if choices else {}
    content = message.get("content")     # may be None
    images  = message.get("images") or []

    response_shape_observed = {
        "top_level_keys":  list(body.keys()),
        "choices_count":   len(choices),
        "content_type":    type(content).__name__,
        "images_count":    len(images),
        "message_keys":    list(message.keys()) if message else [],
    }
    if isinstance(content, list):
        response_shape_observed["content_part_types"] = [
            p.get("type") for p in content if isinstance(p, dict)
        ]
    if images:
        response_shape_observed["images_part_types"] = [
            i.get("type") for i in images if isinstance(i, dict)
        ]

    # Parse image URL — live-tested shape: image in message.images[], not message.content[]
    # content is None; images[] = [{"type":"image_url","image_url":{"url":"data:..."}}]
    try:
        if not choices:
            raise ValueError(f"No choices in response. Keys: {list(body.keys())}")

        image_url = None

        # Primary: message.images[] (confirmed shape for openai/gpt-5-image-mini)
        for img in images:
            if isinstance(img, dict) and img.get("type") == "image_url":
                image_url = (img.get("image_url") or {}).get("url", "")
                if image_url:
                    break

        # Fallback: message.content[] parts
        if not image_url and isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    image_url = (part.get("image_url") or {}).get("url", "")
                    if image_url:
                        break

        if isinstance(content, str) and content and not image_url:
            raise ValueError(
                f"Content is string not image — model may not support image modality. "
                f"Content preview: {content[:200]}"
            )

        if not image_url:
            raise ValueError(
                f"No image URL found in message.images or message.content. "
                f"Message keys: {list(message.keys())}"
            )
    except Exception as exc:
        result = {
            "provider": provider, "status": "error_parse_shape", "model": model,
            "endpoint": endpoint, "latency_ms": latency_ms,
            "parser_path": "choices[0].message.images[] image_url",
            "error": str(exc),
            "response_shape_observed": response_shape_observed,
        }
        _save(out_dir, "response_meta.json", result)
        return result

    # Download / decode image
    parser_path_used = None
    try:
        if image_url.startswith("data:"):
            parser_path_used = "data_uri"
            b64_part = image_url.split(",", 1)[1]
            image_bytes = base64.b64decode(b64_part)
        else:
            parser_path_used = "url"
            img_resp = requests.get(image_url, timeout=60)
            img_resp.raise_for_status()
            image_bytes = img_resp.content
    except Exception as exc:
        result = {
            "provider": provider, "status": "error_image_download", "model": model,
            "endpoint": endpoint, "latency_ms": latency_ms,
            "parser_path": parser_path_used or "unknown",
            "error": _redact(str(exc), config.OPENROUTER_API_KEY),
            "response_shape_observed": response_shape_observed,
        }
        _save(out_dir, "response_meta.json", result)
        return result

    _save(out_dir, "output.jpg", image_bytes)

    result = {
        "provider": provider, "status": "success",
        "model": model, "endpoint": endpoint, "latency_ms": latency_ms,
        "parser_path": parser_path_used,
        "image_bytes": len(image_bytes),
        "image_path": str(out_dir / "output.jpg"),
        "response_shape_observed": response_shape_observed,
    }
    _save(out_dir, "response_meta.json", result)
    return result


# ---------------------------------------------------------------------------
# MUAPI smoke test
# ---------------------------------------------------------------------------

def smoke_muapi(config: Config) -> dict:
    """
    POST https://api.muapi.ai/api/v1/gpt-image-2-text-to-image
    Poll GET /api/v1/predictions/{request_id}/result until completed.
    parser: outputs[] → CDN URL → download
    """
    provider   = "muapi"
    out_dir    = OUTPUT_BASE / provider
    model      = "gpt-image-2-text-to-image"
    endpoint   = f"{config.MUAPI_BASE_URL}/{model}"
    poll_base  = f"{config.MUAPI_BASE_URL}/predictions"

    if not config.MUAPI_API_KEY:
        result = {
            "provider": provider, "status": "skipped_no_key",
            "model": model, "endpoint": endpoint,
            "latency_ms": None, "parser_path": "outputs[]",
            "note": "MUAPI_API_KEY not set",
        }
        _save(out_dir, "response_meta.json", result)
        return result

    payload = {
        "prompt":       SMOKE_PROMPT,
        "aspect_ratio": "1:1",
        "resolution":   "2K",
        "quality":      "high",
    }

    _save(out_dir, "request.json", {
        "endpoint": endpoint,
        "method":   "POST",
        "headers":  {"x-api-key": "***", "Content-Type": "application/json"},
        "payload":  payload,
        "polling":  f"{poll_base}/{{request_id}}/result",
    })

    requests = _get_requests()
    # MUAPI auth: x-api-key header — NOT Authorization: Bearer (which returns 403)
    auth_header = {"x-api-key": config.MUAPI_API_KEY, "Content-Type": "application/json"}

    # Submit
    t0 = time.monotonic()
    try:
        submit_resp = requests.post(endpoint, headers=auth_header, json=payload, timeout=30)
        if submit_resp.status_code == 401:
            raise RuntimeError("HTTP 401 — check MUAPI_API_KEY")
        if submit_resp.status_code == 402:
            raise RuntimeError("HTTP 402 — MUAPI quota exhausted")
        submit_resp.raise_for_status()
        submit_body = submit_resp.json()
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        result = {
            "provider": provider, "status": "error_submit", "model": model,
            "endpoint": endpoint, "latency_ms": latency_ms, "parser_path": "outputs[]",
            "error": _redact(str(exc), config.MUAPI_API_KEY),
        }
        _save(out_dir, "response_meta.json", result)
        return result

    request_id = submit_body.get("id") or submit_body.get("request_id")
    if not request_id:
        result = {
            "provider": provider, "status": "error_no_request_id", "model": model,
            "endpoint": endpoint, "latency_ms": int((time.monotonic() - t0) * 1000),
            "parser_path": "outputs[]",
            "submit_response_keys": list(submit_body.keys()),
        }
        _save(out_dir, "response_meta.json", result)
        return result

    print(f"    muapi  submit ok  request_id={request_id}")

    # Poll
    poll_url   = f"{poll_base}/{request_id}/result"
    final_body = None
    for attempt in range(POLL_MAX):
        time.sleep(POLL_INTERVAL)
        try:
            poll_resp = requests.get(poll_url, headers=auth_header, timeout=30)
        except Exception:
            continue
        if poll_resp.status_code not in (200, 201):
            continue
        poll_body = poll_resp.json()
        status    = poll_body.get("status", "")
        print(f"    muapi  poll {attempt+1:02d}  status={status}")
        if status == "completed":
            final_body = poll_body
            break
        if status in ("failed", "cancelled"):
            break

    latency_ms = int((time.monotonic() - t0) * 1000)

    if final_body is None:
        result = {
            "provider": provider, "status": "error_poll_timeout_or_failed",
            "model": model, "endpoint": endpoint, "latency_ms": latency_ms,
            "parser_path": "outputs[]", "request_id": request_id,
            "poll_attempts": attempt + 1,
            "last_status": poll_body.get("status") if "poll_body" in dir() else "unknown",
        }
        _save(out_dir, "response_meta.json", result)
        return result

    # Extract URL from outputs[]
    outputs   = final_body.get("outputs", [])
    image_url = outputs[0] if outputs and isinstance(outputs[0], str) else None
    if not image_url and outputs and isinstance(outputs[0], dict):
        image_url = outputs[0].get("url")

    if not image_url:
        result = {
            "provider": provider, "status": "error_no_output_url",
            "model": model, "endpoint": endpoint, "latency_ms": latency_ms,
            "parser_path": "outputs[]", "request_id": request_id,
            "outputs_value": str(outputs)[:300],
            "response_keys": list(final_body.keys()),
        }
        _save(out_dir, "response_meta.json", result)
        return result

    # Download
    try:
        img_resp   = requests.get(image_url, timeout=60)
        img_resp.raise_for_status()
        image_bytes = img_resp.content
    except Exception as exc:
        result = {
            "provider": provider, "status": "error_image_download",
            "model": model, "endpoint": endpoint, "latency_ms": latency_ms,
            "parser_path": "outputs[]", "request_id": request_id,
            "error": _redact(str(exc), config.MUAPI_API_KEY),
        }
        _save(out_dir, "response_meta.json", result)
        return result

    _save(out_dir, "output.jpg", image_bytes)

    cost = final_body.get("cost", {})
    result = {
        "provider": provider, "status": "success",
        "model": model, "endpoint": endpoint, "latency_ms": latency_ms,
        "parser_path": "outputs[]",
        "request_id": request_id,
        "poll_attempts": attempt + 1,
        "image_bytes": len(image_bytes),
        "image_path": str(out_dir / "output.jpg"),
        "response_meta": {
            "http_status":  200,
            "cost_usd":     cost.get("amount_usd"),
            "cost_credits": cost.get("amount_credits"),
            "execution_ms": final_body.get("executionTime"),
            "outputs_count": len(outputs),
        },
    }
    _save(out_dir, "response_meta.json", result)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"\n{'='*62}")
    print("  Phase 1C — Provider Live Smoke Test")
    print(f"{'='*62}")
    print(f"  prompt: {SMOKE_PROMPT!r}")
    print(f"  output: {OUTPUT_BASE}")
    print()

    try:
        config = Config()
    except Exception as exc:
        print(f"  ERROR: Config() failed: {exc}")
        return 1

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

    smoke_fns = [
        ("openai",     smoke_openai),
        ("openrouter", smoke_openrouter),
        ("muapi",      smoke_muapi),
    ]

    results = []
    for name, fn in smoke_fns:
        print(f"  [{name}]  running...")
        started_at = _now_iso()
        try:
            r = fn(config)
        except Exception as exc:
            r = {
                "provider": name, "status": "error_unexpected",
                "model": "unknown", "endpoint": "unknown",
                "latency_ms": None, "parser_path": "unknown",
                "error": str(exc),
            }
        r["started_at"] = started_at
        results.append(r)
        status = r.get("status", "?")
        latency = r.get("latency_ms")
        lat_str = f"{latency} ms" if latency is not None else "—"
        print(f"         status={status}  latency={lat_str}")
        if status.startswith("error") or status == "skipped_no_key":
            err = r.get("error") or r.get("note") or r.get("response_body_truncated", "")
            if err:
                print(f"         {str(err)[:100]}")

    # Build report
    report = {
        "report_version":  "1.0",
        "phase":           "1C",
        "generated_at":    _now_iso(),
        "smoke_prompt":    SMOKE_PROMPT,
        "summary": {
            "total":    len(results),
            "success":  sum(1 for r in results if r.get("status") == "success"),
            "skipped":  sum(1 for r in results if "skipped" in r.get("status", "")),
            "failed":   sum(1 for r in results if r.get("status", "").startswith("error")),
        },
        "providers": {r["provider"]: r for r in results},
    }

    report_path = OUTPUT_BASE / "provider_live_smoke_report.json"
    report_path.write_text(json.dumps(report, indent=2))

    # Print summary
    print(f"\n  {'─'*58}")
    s = report["summary"]
    print(f"  success={s['success']}  skipped={s['skipped']}  failed={s['failed']}")
    print(f"\n  Report: {report_path}")
    print(f"{'='*62}\n")
    return 0 if s["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
