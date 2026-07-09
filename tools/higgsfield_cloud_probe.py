#!/usr/bin/env python3
"""
tools/higgsfield_cloud_probe.py

Probe the Higgsfield Cloud REST API to discover available endpoints and capabilities.
Makes ZERO paid generation calls. Saves results to:
  .hypogeum-assets/provider_compare/higgsfield_cloud_probe.json

Key findings from live probing (2026-07-06):
  - All GET requests return 405 regardless of auth (server rejects GET before auth)
  - Auth is confirmed valid by POST /v1/image2video/dop → 422 (not 401)
  - Only I2V endpoint confirmed: POST /v1/image2video/dop (requires prompt + input_images)
  - All T2V candidates return 404 {"detail":"Model not found"}
  - No upscale endpoint available via REST
  - No account/credits endpoint accessible

Usage:
  python3 tools/higgsfield_cloud_probe.py [--verbose]

Exit codes:
  0 — T2V available
  1 — I2V only (expected result per live probe)
  2 — auth failure or network error
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Bootstrap path so we can import from hypogeum/
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.config import Config  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OUTPUT_DIR = _ROOT / ".hypogeum-assets" / "provider_compare"
_OUTPUT_FILE = _OUTPUT_DIR / "higgsfield_cloud_probe.json"

_TARGET_SCENES = ["s02", "s05", "s07", "s10"]
_VISUAL_PACKAGE = (
    _ROOT
    / ".hypogeum-assets"
    / "episodes"
    / "ep_reg_vi_colosseum"
    / "visual_package_ep_reg_vi_colosseum.json"
)

# Endpoint probes — POST with empty/minimal body (GET returns 405 for everything)
# Interpretation of response codes:
#   422 = path exists + auth valid (validation error on missing fields)
#   404 "Model not found" = path exists but model not registered
#   401/403 = auth rejected
#   404 "Not Found" = path does not exist
_ENDPOINT_PROBES: list[dict[str, Any]] = [
    # -----------------------------------------------------------------------
    # Auth validation — known I2V endpoint
    # -----------------------------------------------------------------------
    {"path": "/v1/image2video/dop",   "method": "POST", "body": {},
     "category": "auth_check", "label": "I2V DoP (auth validation)"},

    # -----------------------------------------------------------------------
    # Image-to-Video — known working
    # -----------------------------------------------------------------------
    {"path": "/v1/image2video",       "method": "POST", "body": {},
     "category": "i2v", "label": "I2V namespace"},
    {"path": "/v1/image2video/dop",   "method": "POST", "body": {"params": {}},
     "category": "i2v", "label": "I2V DoP (params required check)"},

    # -----------------------------------------------------------------------
    # Text-to-Video — probing for T2V support
    # -----------------------------------------------------------------------
    {"path": "/v1/text2video",        "method": "POST", "body": {},
     "category": "t2v", "label": "T2V endpoint"},
    {"path": "/v1/video/generate",    "method": "POST", "body": {},
     "category": "t2v", "label": "T2V video/generate"},
    {"path": "/v1/video",             "method": "POST", "body": {},
     "category": "t2v", "label": "T2V /video"},
    {"path": "/v1/generate",          "method": "POST", "body": {},
     "category": "t2v", "label": "T2V /generate"},
    {"path": "/v1/text-to-video",     "method": "POST", "body": {},
     "category": "t2v", "label": "T2V hyphenated"},
    {"path": "/v1/generations",       "method": "POST", "body": {},
     "category": "t2v", "label": "T2V /generations"},

    # -----------------------------------------------------------------------
    # Upscale
    # -----------------------------------------------------------------------
    {"path": "/v1/upscale",           "method": "POST", "body": {},
     "category": "upscale", "label": "Upscale"},
    {"path": "/v1/video/upscale",     "method": "POST", "body": {},
     "category": "upscale", "label": "Video upscale"},
    {"path": "/v1/enhance",           "method": "POST", "body": {},
     "category": "upscale", "label": "Enhance"},
    {"path": "/v1/image2video/upscale","method": "POST", "body": {},
     "category": "upscale", "label": "I2V upscale"},

    # -----------------------------------------------------------------------
    # Models / catalog
    # -----------------------------------------------------------------------
    {"path": "/v1/models",            "method": "POST", "body": {},
     "category": "models", "label": "Model catalog (POST)"},
    {"path": "/v1/models",            "method": "GET",  "body": None,
     "category": "models", "label": "Model catalog (GET)"},
]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _auth_header(cfg: Config) -> str:
    return f"Key {cfg.HF_API_KEY}:{cfg.HF_API_SECRET}"


def _probe_endpoint(
    base_url: str,
    path: str,
    method: str,
    body: Any,
    auth: str,
    timeout: int = 15,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + path
    result: dict[str, Any] = {
        "url": url,
        "method": method,
        "status_code": None,
        "error": None,
        "exists": False,          # path is registered on the server
        "auth_ok": False,         # credentials accepted
        "model_found": False,     # model registered (only for generation paths)
        "t2v_capable": False,     # responds as T2V endpoint (422 without needing images)
        "body_snippet": None,
        "response_keys": [],
    }
    try:
        headers = {
            "Authorization": auth,
            "Accept": "application/json",
            "User-Agent": "hypogeum-probe/1.0",
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        else:
            resp = requests.get(url, headers=headers, timeout=timeout)

        result["status_code"] = resp.status_code

        try:
            rbody = resp.json()
        except Exception:
            rbody = resp.text

        if isinstance(rbody, dict):
            result["response_keys"] = list(rbody.keys())[:20]
            result["body_snippet"] = _truncate(rbody, 500)
        elif isinstance(rbody, list):
            result["response_keys"] = ["<list>"]
            result["body_snippet"] = _truncate(rbody, 500)
        else:
            result["body_snippet"] = str(rbody)[:500]

        code = resp.status_code

        if code == 200:
            result["exists"] = True
            result["auth_ok"] = True
            result["model_found"] = True
        elif code == 405:
            # Method not allowed — path exists, GET rejected (server ignores auth for GET)
            result["exists"] = True
            # Cannot confirm auth_ok from 405 alone
        elif code == 422:
            # Validation error — path exists, auth accepted, just missing fields
            result["exists"] = True
            result["auth_ok"] = True
            # Check if this T2V capable = validation error does NOT mention input_images
            if isinstance(rbody, dict):
                detail = rbody.get("detail", [])
                if isinstance(detail, list):
                    # Flatten all loc paths to find which sub-fields are missing
                    missing_fields = [
                        e.get("loc", [])[-1] if e.get("loc") else ""
                        for e in detail
                    ]
                    full_locs = [tuple(e.get("loc", [])) for e in detail]
                    result["missing_fields"] = missing_fields
                    # T2V capable = validation descended into params-level fields
                    # AND does NOT require input_images.
                    # (If the error is just "params is required" at body level, we
                    # haven't confirmed anything about the model's input shape.)
                    params_level = any(
                        len(loc) >= 3 and loc[0] == "body" and loc[1] == "params"
                        for loc in full_locs
                    )
                    result["t2v_capable"] = (
                        params_level
                        and "input_images" not in missing_fields
                    )
                    result["model_found"] = True
        elif code in (401, 403):
            result["exists"] = True
            result["auth_ok"] = False
        elif code == 404:
            result["exists"] = False
            # "Model not found" detail = path exists, model not registered
            if isinstance(rbody, dict) and "not found" in rbody.get("detail", "").lower():
                if "model" in rbody.get("detail", "").lower():
                    result["exists"] = True  # routing path exists; model absent
                    result["auth_ok"] = True  # auth was accepted before model check

    except requests.exceptions.ConnectionError as exc:
        result["error"] = f"ConnectionError: {exc}"
    except requests.exceptions.Timeout:
        result["error"] = "Timeout"
    except Exception as exc:
        result["error"] = str(exc)

    return result


def _truncate(obj: Any, max_chars: int) -> str:
    s = json.dumps(obj, default=str)
    return s[:max_chars] + ("…" if len(s) > max_chars else "")


# ---------------------------------------------------------------------------
# Scene prompt loader
# ---------------------------------------------------------------------------

def _load_target_scene_prompts() -> dict[str, dict[str, Any]]:
    if not _VISUAL_PACKAGE.exists():
        return {}
    with open(_VISUAL_PACKAGE) as f:
        vp = json.load(f)
    scenes_data: dict[str, dict[str, Any]] = {}
    for scene in vp.get("scenes", []):
        sid = scene.get("scene_id")
        if sid not in _TARGET_SCENES:
            continue
        prod = scene.get("production_spec", {})
        scenes_data[sid] = {
            "scene_id": sid,
            "prompt": prod.get("provider_routing", {}).get("primary_prompt", ""),
            "aspect_ratio": prod.get("aspect_ratio", "16:9"),
            "duration_seconds": prod.get("duration_seconds", 8),
            "resolution": prod.get("resolution", "1920x1080"),
        }
    return scenes_data


# ---------------------------------------------------------------------------
# Capability analysis
# ---------------------------------------------------------------------------

def _analyze_results(probe_results: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, list[dict]] = {}
    for r in probe_results:
        cat = r["category"]
        by_category.setdefault(cat, []).append(r)

    def _any_auth_ok(cat: str) -> bool:
        return any(r["result"]["auth_ok"] for r in by_category.get(cat, []))

    def _any_t2v_capable(cat: str) -> bool:
        return any(r["result"].get("t2v_capable") for r in by_category.get(cat, []))

    # Auth: confirmed by 422 on known I2V endpoint (not 401/403)
    auth_check = by_category.get("auth_check", [])
    auth_valid = any(r["result"]["auth_ok"] for r in auth_check)
    # Also accept if any endpoint confirmed auth_ok
    if not auth_valid:
        auth_valid = any(
            r["result"]["auth_ok"]
            for cat_probes in by_category.values()
            for r in cat_probes
        )

    # T2V: any T2V endpoint returns 422 without input_images requirement
    t2v_supported = _any_t2v_capable("t2v")

    # I2V: known endpoint returns 422 with input_images in required fields
    i2v_supported = any(
        r["result"]["auth_ok"] and not r["result"].get("t2v_capable")
        for r in by_category.get("i2v", [])
        if r["result"]["status_code"] == 422
    )

    # Upscale: any upscale path returns 422 (auth accepted)
    upscale_supported = any(
        r["result"]["auth_ok"] and r["result"]["status_code"] in (200, 422)
        for r in by_category.get("upscale", [])
    )

    # Model list
    models_catalog = []
    for r in by_category.get("models", []):
        if r["result"]["status_code"] == 200:
            models_catalog.append(r["result"]["body_snippet"])

    return {
        "auth_valid": auth_valid,
        "t2v_supported": t2v_supported,
        "i2v_supported": i2v_supported,
        "upscale_supported": upscale_supported,
        "native_1080p_possible": t2v_supported or i2v_supported,
        "models_catalog": models_catalog,
        "known_i2v_models": ["dop (via /v1/image2video/dop)"],
        "known_i2v_dop_model_variants": [
            "dop-lite ($0.14/gen)",
            "dop-turbo ($0.42/gen)",
            "dop-preview ($0.57/gen)",
        ],
    }


# ---------------------------------------------------------------------------
# Generation plan
# ---------------------------------------------------------------------------

_PROVIDER_ASSIGNMENT = {
    "s02": {"provider": "higgsfield", "method": "native_t2v",   "upscale": False},
    "s05": {"provider": "higgsfield", "method": "t2v_upscaled", "upscale": True},
    "s07": {"provider": "muapi",      "method": "native_t2v",   "upscale": False},
    "s10": {"provider": "muapi",      "method": "t2v_upscaled", "upscale": True},
}


def _build_generation_plan(
    scenes: dict[str, dict[str, Any]],
    capabilities: dict[str, Any],
) -> dict[str, Any]:
    plan_entries = []
    for sid in _TARGET_SCENES:
        assignment = _PROVIDER_ASSIGNMENT[sid]
        scene = scenes.get(sid, {})
        prompt = scene.get("prompt", "<not found>")
        duration = scene.get("duration_seconds", 8)
        resolution = scene.get("resolution", "1920x1080")

        provider = assignment["provider"]
        method = assignment["method"]
        upscale = assignment["upscale"]

        if provider == "higgsfield":
            if not capabilities["t2v_supported"]:
                feasible = False
                blocker = "T2V not available via REST API — I2V only (requires reference image)"
                alt = (
                    "Alternative: (a) use MUAPI instead, "
                    "(b) use Higgsfield MCP with correct paid account, "
                    "(c) use Higgsfield I2V with a reference still from the episode"
                )
                cost = None
            else:
                feasible = True
                blocker = None
                alt = None
                cost = None
        else:
            feasible = True
            blocker = None
            alt = None
            cost = 0.60  # MUAPI veo3-fast

        entry = {
            "scene_id": sid,
            "provider": provider,
            "method": method,
            "upscale": upscale,
            "duration_seconds": duration,
            "resolution": resolution,
            "feasible": feasible,
            "estimated_cost_usd": cost,
            "prompt_length_chars": len(prompt),
            "prompt_preview": prompt[:200] + "…" if len(prompt) > 200 else prompt,
        }
        if blocker:
            entry["blocker"] = blocker
        if alt:
            entry["alternative"] = alt
        plan_entries.append(entry)

    return {
        "plan_entries": plan_entries,
        "note": "DRY RUN — no generation submitted",
        "output_dir": str(
            _ROOT / ".hypogeum-assets" / "episodes" / "ep_reg_vi_colosseum" / "provider_compare"
        ),
        "expected_filenames": {sid: f"sc_{sid}.mp4" for sid in _TARGET_SCENES},
    }


# ---------------------------------------------------------------------------
# Print summary
# ---------------------------------------------------------------------------

def _print_summary(capabilities: dict[str, Any], plan: dict[str, Any]) -> None:
    bar = "=" * 60
    print(f"\n{bar}")
    print("HIGGSFIELD CLOUD API PROBE — RESULTS")
    print(bar)

    auth_tag = "VALID" if capabilities["auth_valid"] else "FAILED"
    print(f"\n  Auth:               {auth_tag}")
    print(f"  T2V supported:      {'YES' if capabilities['t2v_supported'] else 'NO'}")
    print(f"  I2V supported:      {'YES' if capabilities['i2v_supported'] else 'NO'}")
    print(f"  Upscale via REST:   {'YES' if capabilities['upscale_supported'] else 'NO'}")
    print(f"  Native 1080p:       {'POSSIBLE' if capabilities['native_1080p_possible'] else 'UNKNOWN'}")

    print(f"\n  Known I2V endpoints:")
    for m in capabilities.get("known_i2v_models", []):
        print(f"    - {m}")
    print(f"\n  DoP model variants (configured in hypogeum/higgsfield.py):")
    for v in capabilities.get("known_i2v_dop_model_variants", []):
        print(f"    - {v}")

    if not capabilities["t2v_supported"]:
        print(f"\n{'!' * 60}")
        print("CONCLUSION: Higgsfield Cloud REST API — I2V only")
        print("  No T2V endpoint found. All T2V paths → 404 'Model not found'.")
        print("  T2V comparison via REST is NOT possible with this account/key.")
        print("!" * 60)

    print(f"\n{bar}")
    print("GENERATION PLAN (dry run — not submitted)")
    print(bar)
    for entry in plan["plan_entries"]:
        status = "FEASIBLE" if entry["feasible"] else "BLOCKED"
        cost_str = f"${entry['estimated_cost_usd']:.2f}" if entry["estimated_cost_usd"] else "unknown"
        up = " + upscale" if entry["upscale"] else ""
        print(f"\n  {entry['scene_id']}  [{entry['provider'].upper()} {entry['method']}{up}]"
              f"  {status}  est. cost={cost_str}")
        print(f"     prompt ({entry['prompt_length_chars']} chars): {entry['prompt_preview']}")
        if entry.get("blocker"):
            print(f"     BLOCKER: {entry['blocker']}")
        if entry.get("alternative"):
            print(f"     ALT:     {entry['alternative']}")

    print(f"\nFull results: {_OUTPUT_FILE}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Higgsfield Cloud API capabilities")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    cfg = Config()
    if not cfg.HF_API_KEY or not cfg.HF_API_SECRET:
        print("ERROR: HF_API_KEY and/or HF_API_SECRET not set in .env", file=sys.stderr)
        return 2

    base_url = cfg.HIGGSFIELD_BASE_URL
    auth = _auth_header(cfg)
    timeout = min(cfg.HIGGSFIELD_TIMEOUT_SECONDS, 15)

    print(f"Probing {base_url} ({len(_ENDPOINT_PROBES)} endpoints)…")

    probe_results: list[dict[str, Any]] = []
    for spec in _ENDPOINT_PROBES:
        r = _probe_endpoint(
            base_url, spec["path"], spec["method"], spec.get("body"),
            auth, timeout=timeout,
        )
        entry = {
            "path": spec["path"],
            "category": spec["category"],
            "label": spec["label"],
            "result": r,
        }
        probe_results.append(entry)
        if args.verbose:
            code = r["status_code"] or r["error"]
            tags = []
            if r["exists"]:
                tags.append("exists")
            if r["auth_ok"]:
                tags.append("auth_ok")
            if r.get("t2v_capable"):
                tags.append("T2V_CAPABLE")
            print(f"  {code:4}  {spec['method']:4}  {spec['path']:<40}  {' '.join(tags)}")
        else:
            c = r["status_code"]
            print(
                "." if c == 200 else
                "V" if c == 422 else
                "M" if c == 405 else
                "x" if c == 404 else
                "A" if c in (401, 403) else
                "?",
                end="", flush=True,
            )
        time.sleep(0.08)

    print()

    capabilities = _analyze_results(probe_results)
    scenes = _load_target_scene_prompts()
    plan = _build_generation_plan(scenes, capabilities)

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": base_url,
        "auth_key_prefix": (cfg.HF_API_KEY or "")[:8] + "…",
        "capabilities": capabilities,
        "generation_plan": plan,
        "raw_probes": probe_results,
    }
    with open(_OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2, default=str)

    _print_summary(capabilities, plan)

    if not capabilities["auth_valid"]:
        print("ERROR: Auth check failed — no 422 from known I2V endpoint", file=sys.stderr)
        return 2

    return 0 if capabilities["t2v_supported"] else 1


if __name__ == "__main__":
    sys.exit(main())
