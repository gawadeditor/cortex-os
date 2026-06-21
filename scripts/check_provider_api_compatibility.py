"""
scripts/check_provider_api_compatibility.py — Phase 1B Provider API Compatibility Check

Builds request payloads for each image provider WITHOUT sending them.
Validates endpoint, headers, model, size/aspect-ratio, and response parser expectations.
Outputs provider_api_compatibility_report.json.

No generation. No network calls. No secrets printed.

Run:
    python3 scripts/check_provider_api_compatibility.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Verification metadata — what we know vs what we've observed vs what is uncertain
# ---------------------------------------------------------------------------

PROVIDER_API_SPECS = {
    "openai": {
        "endpoint":           "https://api.openai.com/v1/images/generations",
        "method":             "POST",
        "auth_header":        "Authorization: Bearer {OPENAI_API_KEY}",
        "content_type":       "application/json",
        "model":              "gpt-image-1",
        "model_note":         "gpt-image-1 is the official OpenAI API model ID (April 2025). "
                              "gpt-image-2 is MUAPI's internal model slug — not an OpenAI API name.",
        "supported_sizes": {
            "16:9": "1536x1024",
            "9:16": "1024x1536",
            "1:1":  "1024x1024",
        },
        "quality_values":     ["low", "medium", "high", "auto"],
        "response_format":    "b64_json",
        "response_shape":     {"data": [{"b64_json": "<base64>", "revised_prompt": "<str>"}]},
        "verification": {
            "endpoint":         {"status": "CONFIRMED",    "source": "OpenAI API docs"},
            "auth":             {"status": "CONFIRMED",    "source": "OpenAI API docs"},
            "model_gpt_image_1":{"status": "CONFIRMED",    "source": "OpenAI April 2025 release"},
            "model_gpt_image_2":{"status": "DOES NOT EXIST as OpenAI API model",
                                 "note": "gpt-image-2 is MUAPI's slug for their GPT-Image wrapper"},
            "sizes":            {"status": "CONFIRMED",    "source": "OpenAI API docs"},
            "b64_json":         {"status": "CONFIRMED",    "source": "Supported since DALL-E 2"},
            "quality_param":    {"status": "CONFIRMED",    "source": "OpenAI API docs"},
            "response_shape":   {"status": "CONFIRMED",    "source": "OpenAI API docs"},
        },
        "ready_to_call": True,
        "blocking_issues": [],
    },

    "openrouter": {
        "endpoint":           "https://openrouter.ai/api/v1/chat/completions",
        "method":             "POST",
        "INVALID_ENDPOINT":   "https://openrouter.ai/api/v1/images/generations — DOES NOT EXIST on OpenRouter",
        "auth_header":        "Authorization: Bearer {OPENROUTER_API_KEY}",
        "content_type":       "application/json",
        "extra_headers":      {
            "HTTP-Referer": "{OPENROUTER_APP_URL}",
            "X-Title":      "{OPENROUTER_APP_NAME}",
        },
        "model":              "openai/gpt-image-1",
        "model_note":         "OpenRouter model IDs use 'provider/model' format. "
                              "Discover image-capable models via GET /api/v1/models?output_modalities=image",
        "modalities":         ["image"],
        "model_discovery":    "GET https://openrouter.ai/api/v1/models?output_modalities=image",
        "response_shape": {
            "choices": [{"message": {"content": [
                {"type": "image_url", "image_url": {"url": "<data:image/...;base64,...> or <https://...>"}}
            ]}}]
        },
        "verification": {
            "endpoint_chat_completions": {"status": "CONFIRMED", "source": "OpenRouter docs"},
            "endpoint_images_generations": {
                "status": "INVALID — does not exist on OpenRouter",
                "note":   "Phase 1A incorrectly assumed this endpoint. Corrected in Phase 1B."
            },
            "auth":             {"status": "CONFIRMED",    "source": "OpenRouter docs"},
            "extra_headers":    {"status": "CONFIRMED",    "source": "OpenRouter attribution requirement"},
            "modalities_param": {"status": "CONFIRMED",    "source": "OpenRouter docs"},
            "model_format":     {"status": "CONFIRMED",    "source": "OpenRouter provider/model convention"},
            "model_availability": {
                "status": "UNVERIFIED",
                "note":   "Use GET /api/v1/models?output_modalities=image to list available models"
            },
            "response_shape":   {
                "status": "UNVERIFIED",
                "note":   "Expected: choices[0].message.content[].type='image_url'. "
                          "Parser handles data: URI (base64) and plain URL. Needs live test."
            },
        },
        "ready_to_call": False,
        "blocking_issues": [
            "response shape unverified — choices[0].message.content image_url format needs live test",
            "openai/gpt-image-1 model availability on OpenRouter needs verification via model discovery",
        ],
    },

    "muapi": {
        "endpoint":           "https://api.muapi.ai/api/v1/gpt-image-2-text-to-image",
        "method":             "POST",
        "auth_header":        "Authorization: Bearer {MUAPI_API_KEY}",
        "content_type":       "application/json",
        "model":              "gpt-image-2-text-to-image",
        "model_note":         "MUAPI internal slug. Not an OpenAI model ID. "
                              "MUAPI wraps gpt-image-1 under this slug.",
        "supported_sizes":    "aspect_ratio string (e.g. '16:9'), resolution '2K'",
        "quality_values":     ["high"],
        "response_format":    "outputs[] array with hosted CDN URL",
        "response_shape":     {"outputs": ["<cdn_url>"], "status": "completed", "cost": {}},
        "polling":            True,
        "polling_endpoint":   "https://api.muapi.ai/api/v1/predictions/{request_id}/result",
        "verification": {
            "endpoint":         {"status": "CONFIRMED", "source": "Live-tested 2026-06-21"},
            "auth":             {"status": "CONFIRMED", "source": "Live-tested"},
            "model_slug":       {"status": "CONFIRMED", "source": "Live-tested"},
            "outputs_array":    {"status": "CONFIRMED", "source": "Live-tested — fixed URL extraction bug"},
            "polling":          {"status": "CONFIRMED", "source": "Live-tested"},
            "resolution_param": {"status": "CONFIRMED", "source": "Live-tested (2K)"},
            "quality_param":    {"status": "CONFIRMED", "source": "Live-tested (high)"},
        },
        "ready_to_call": True,
        "blocking_issues": [],
    },
}


# ---------------------------------------------------------------------------
# Payload builders (no network calls)
# ---------------------------------------------------------------------------

def _build_openai_payload(aspect_ratio: str = "16:9", model: str = "gpt-image-1") -> dict:
    spec = PROVIDER_API_SPECS["openai"]
    size = spec["supported_sizes"].get(aspect_ratio, "1024x1024")
    return {
        "model":           model,
        "prompt":          "<IMAGE_PROMPT>",
        "n":               1,
        "size":            size,
        "quality":         "high",
        "response_format": "b64_json",
    }


def _build_openrouter_payload(aspect_ratio: str = "16:9", model: str = "openai/gpt-image-1") -> dict:
    # OpenRouter image generation uses chat/completions with modalities, NOT images/generations
    return {
        "model":      model,
        "messages":   [{"role": "user", "content": "<IMAGE_PROMPT>"}],
        "modalities": ["image"],
    }


def _build_muapi_payload(aspect_ratio: str = "16:9", model: str = "gpt-image-2-text-to-image") -> dict:
    return {
        "prompt":       "<IMAGE_PROMPT>",
        "aspect_ratio": aspect_ratio,
        "resolution":   "2K",
        "quality":      "high",
    }


# ---------------------------------------------------------------------------
# Per-provider compatibility checks
# ---------------------------------------------------------------------------

def _check_openai() -> dict:
    spec = PROVIDER_API_SPECS["openai"]
    payload_16_9 = _build_openai_payload("16:9")
    payload_9_16 = _build_openai_payload("9:16")

    issues = []
    warnings = []

    # Endpoint structure
    assert spec["endpoint"].startswith("https://api.openai.com/v1/"), "Wrong base URL"

    # Model validation
    if "gpt-image-2" in spec["model"] and "gpt-image-1" not in spec["model"]:
        issues.append("WRONG MODEL: gpt-image-2 is not an OpenAI API model; must be gpt-image-1")

    # Size validation
    for ar, expected_size in [("16:9", "1536x1024"), ("9:16", "1024x1536")]:
        actual = spec["supported_sizes"].get(ar)
        if actual != expected_size:
            issues.append(f"Size mismatch for {ar}: expected {expected_size}, got {actual}")

    # Response parser
    response_parser = {
        "extract_b64": "result['data'][0]['b64_json']",
        "decode":      "base64.b64decode(b64_json)",
        "write_bytes": "local_path.write_bytes(decoded)",
        "fallback":    "None — b64_json confirmed supported",
    }

    return {
        "provider":          "openai",
        "endpoint":          spec["endpoint"],
        "method":            spec["method"],
        "auth_header_shape": "Authorization: Bearer ***",
        "extra_headers":     {},
        "payload_16_9":      payload_16_9,
        "payload_9_16":      payload_9_16,
        "response_parser":   response_parser,
        "verification":      spec["verification"],
        "ready_to_call":     spec["ready_to_call"] and len(issues) == 0,
        "issues":            issues,
        "warnings":          warnings,
    }


def _check_openrouter() -> dict:
    spec = PROVIDER_API_SPECS["openrouter"]
    payload_16_9 = _build_openrouter_payload("16:9")
    payload_9_16 = _build_openrouter_payload("9:16")

    issues = spec["blocking_issues"][:]
    warnings = []

    # Endpoint must be chat/completions, never images/generations
    if "images/generations" in spec["endpoint"]:
        issues.append("WRONG ENDPOINT: OpenRouter uses /chat/completions, not /images/generations")
    if not spec["endpoint"].endswith("/chat/completions"):
        issues.append(f"Unexpected endpoint: {spec['endpoint']}")

    # Must use modalities param
    if "image" not in spec.get("modalities", []):
        issues.append("modalities must include 'image'")

    # Model format check
    if "/" not in spec["model"]:
        issues.append("OpenRouter model must be 'provider/model' format")

    warnings.append(
        "response shape UNVERIFIED: expecting choices[0].message.content[].type='image_url'. "
        "Parser handles data: URI and plain URL. Needs live test before production use."
    )
    warnings.append(
        f"Model availability UNVERIFIED: run GET {spec['model_discovery']} "
        "to confirm openai/gpt-image-1 is listed."
    )

    response_parser = {
        "step_1":   "result['choices'][0]['message']['content'] → list of parts",
        "step_2":   "find part where type == 'image_url'",
        "step_3a":  "if url starts with 'data:' → base64 decode after comma",
        "step_3b":  "else → _download_file(url) → write_bytes",
        "shape_verified": False,
        "note": "UNVERIFIED — response shape documented but not live-tested",
    }

    return {
        "provider":               "openrouter",
        "endpoint":               spec["endpoint"],
        "invalid_endpoint_note":  spec["INVALID_ENDPOINT"],
        "method":                 spec["method"],
        "auth_header_shape":      "Authorization: Bearer ***",
        "extra_headers":          {"HTTP-Referer": "<OPENROUTER_APP_URL>", "X-Title": "<OPENROUTER_APP_NAME>"},
        "modalities":             spec["modalities"],
        "model_discovery_url":    spec["model_discovery"],
        "payload_16_9":           payload_16_9,
        "payload_9_16":           payload_9_16,
        "response_parser":        response_parser,
        "verification":           spec["verification"],
        "ready_to_call":          False,
        "issues":                 issues,
        "warnings":               warnings,
    }


def _check_muapi() -> dict:
    spec = PROVIDER_API_SPECS["muapi"]
    payload_16_9 = _build_muapi_payload("16:9")
    payload_9_16 = _build_muapi_payload("9:16")

    issues: list[str] = []
    warnings: list[str] = []

    response_parser = {
        "step_1": "poll until result['status'] == 'completed'",
        "step_2": "result['outputs'][0]  →  CDN URL string",
        "step_3": "_download_file(cdn_url, local_path) → write file",
        "note":   "outputs[] is always a list. Never result['image_url'] (bug fixed 2026-06-21)",
    }

    return {
        "provider":          "muapi",
        "endpoint":          spec["endpoint"],
        "polling_endpoint":  spec["polling_endpoint"],
        "method":            spec["method"],
        "auth_header_shape": "Authorization: Bearer ***",
        "extra_headers":     {},
        "payload_16_9":      payload_16_9,
        "payload_9_16":      payload_9_16,
        "response_parser":   response_parser,
        "verification":      spec["verification"],
        "ready_to_call":     spec["ready_to_call"],
        "issues":            issues,
        "warnings":          warnings,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_compatibility_check() -> dict:
    """Build payloads and validate all three providers. Returns full report dict."""
    openai_check   = _check_openai()
    openrouter_check = _check_openrouter()
    muapi_check    = _check_muapi()

    providers = [openai_check, openrouter_check, muapi_check]

    ready     = [p["provider"] for p in providers if p["ready_to_call"]]
    not_ready = [p["provider"] for p in providers if not p["ready_to_call"]]
    all_issues = {
        p["provider"]: p["issues"]
        for p in providers if p["issues"]
    }

    return {
        "report_version":  "1.0",
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "summary": {
            "providers_checked":   [p["provider"] for p in providers],
            "ready_to_call":       ready,
            "not_ready":           not_ready,
            "total_issues":        sum(len(p["issues"]) for p in providers),
            "total_warnings":      sum(len(p["warnings"]) for p in providers),
            "blocking_issues":     all_issues,
        },
        "providers": {
            p["provider"]: p for p in providers
        },
        "model_name_clarification": {
            "openai_api_model":    "gpt-image-1  (official OpenAI API model ID)",
            "openrouter_model":    "openai/gpt-image-1  (OpenRouter provider/model format)",
            "muapi_model_slug":    "gpt-image-2-text-to-image  (MUAPI internal — NOT an OpenAI name)",
            "note":                "gpt-image-2 does not exist as an OpenAI API model. "
                                   "It is MUAPI's internal naming convention for their "
                                   "GPT-Image wrapper service.",
        },
        "endpoint_corrections": {
            "openrouter_phase_1a_error":
                "/api/v1/images/generations was used in Phase 1A — this endpoint does not exist on OpenRouter.",
            "openrouter_phase_1b_correction":
                "Corrected to POST /api/v1/chat/completions with modalities: ['image'] per OpenRouter docs.",
            "model_discovery":
                "Use GET /api/v1/models?output_modalities=image to list available OpenRouter image models.",
        },
    }


def main() -> int:
    print(f"\n{'='*62}")
    print("  Provider API Compatibility Check — Phase 1B")
    print(f"{'='*62}\n")

    report = run_compatibility_check()

    # Print summary
    s = report["summary"]
    print(f"  Providers checked:  {', '.join(s['providers_checked'])}")
    print(f"  Ready to call:      {', '.join(s['ready_to_call']) or 'none'}")
    print(f"  Not ready:          {', '.join(s['not_ready']) or 'none'}")
    print(f"  Total issues:       {s['total_issues']}")
    print(f"  Total warnings:     {s['total_warnings']}")

    if s["blocking_issues"]:
        print(f"\n  BLOCKING ISSUES:")
        for provider, issues in s["blocking_issues"].items():
            for issue in issues:
                print(f"    [{provider}] {issue}")

    # Print per-provider status
    print()
    for provider, data in report["providers"].items():
        status = "✓ READY" if data["ready_to_call"] else "✗ NOT READY"
        print(f"  {provider:12s}  {status}")
        for w in data.get("warnings", []):
            print(f"               WARNING: {w[:80]}")

    # Write JSON
    out_path = Path(__file__).resolve().parent.parent / "provider_api_compatibility_report.json"
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\n  Report written: {out_path}")
    print(f"{'='*62}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
