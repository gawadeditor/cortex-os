"""
experiments/video_provider_compare_v1.py — One-clip video provider comparison

Generates the same cinematic prompt through every available MUAPI video model.
No pipeline, no assembly, no upload, no publish.

Models tested (all via MUAPI aggregator — only configured provider):
  veo3-text-to-video       T1  $2.50  8s fixed
  veo3-fast-text-to-video  T2  $0.60  8s fixed
  runway-text-to-video     T2  $0.15  5 or 8s

Direct providers (BytePlus/Seedance, Runway Official, Kling, Veo Google): all
MISSING credentials — skipped automatically.

Output: .hypogeum-assets/experiments/video_provider_compare_v1/
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import requests

from hypogeum.config import Config
from hypogeum.logger import get_logger
from hypogeum.muapi import (
    _submit_http,
    _poll_until_complete,
    _build_job_payload,
    _download_file,
    _get_headers,
    MODEL_CREDIT_COSTS,
)

# ---------------------------------------------------------------------------
# Prompt — VPD Cinematic Architecture v1, scene s02
# Scene: corroded bronze gear fragments on stone table, 1902 Athens
# Rationale: static camera, interior controlled lighting, dense material texture,
# historically precise — maximises signal for quality comparison.
# ---------------------------------------------------------------------------

PROMPT = (
    "The moment a two-thousand-year-old machine introduces itself. "
    "Extreme close-up, bronze gear fragments arranged on a stone examination "
    "table in 1902 Athens, each piece carrying dense green-brown marine patina "
    "with copper-orange exposed at sheared edges where ancient alloy fractured "
    "clean. A single warm amber lamp source enters from the upper left, raking "
    "at a low oblique angle across corroded tooth faces and casting micro-shadow "
    "into every pit and fissure; the surrounding table surface recedes into warm "
    "shadow beyond the lamp's radius. The fragments carry distinct surface "
    "histories — heavy concentric sea-oxidation layering, lighter iron-red "
    "intrusions at the bronze grain boundaries, and a fine calcite crust that "
    "catches the lamp's warmth as pale cream against the dark bronze field. The "
    "stone table is grey-beige marble, stained with iron bleed-out where "
    "fragments rested during recovery. Camera: static, locked. "
    "0-4s: the central fragment fills the lower half of frame as lamp light "
    "resolves gear tooth surface texture, patina detail sharpening from the "
    "brightest lit face inward to shadow. "
    "4-8s: light stabilises at full amber warmth, shadow edges of each tooth "
    "deepening as the eye adjusts to the micro-scale precision. "
    "[4K] [24fps] [macro] [aspect ratio: 16:9] "
    "[depth of field: shallow, fragment surface to stone table] "
    "[color grade: warm amber against cool grey marble] "
    "[without modern display labelling] [without institutional context] "
    "[no fluorescent fill] [no clinical lighting]"
)

ASPECT_RATIO = "16:9"
DURATION     = 8.0   # seconds — Veo3 ignores this (fixed 8s); Runway uses it

# ---------------------------------------------------------------------------
# Provider definitions
# ---------------------------------------------------------------------------

PROVIDERS = [
    {
        "name":      "Veo3 (premium)",
        "model":     "veo3-text-to-video",
        "tier":      "T1",
        "cost_usd":  MODEL_CREDIT_COSTS.get("veo3-text-to-video", 2.50),
        "duration":  "8s fixed (provider)",
    },
    {
        "name":      "Veo3-Fast",
        "model":     "veo3-fast-text-to-video",
        "tier":      "T2",
        "cost_usd":  MODEL_CREDIT_COSTS.get("veo3-fast-text-to-video", 0.675),
        "duration":  "8s fixed (provider)",
    },
    {
        "name":      "Runway Gen3",
        "model":     "runway-text-to-video",
        "tier":      "T2",
        "cost_usd":  MODEL_CREDIT_COSTS.get("runway-text-to-video", 0.15),
        "duration":  "8s requested",
    },
]

OUTPUT_DIR = ROOT / ".hypogeum-assets" / "experiments" / "video_provider_compare_v1"


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class ProviderResult:
    name:          str
    model:         str
    tier:          str
    cost_usd:      float
    duration_req:  str
    success:       bool          = False
    file_path:     Optional[str] = None
    file_size_kb:  Optional[float] = None
    gen_time_s:    Optional[float] = None
    error:         Optional[str] = None
    request_id:    Optional[str] = None


# ---------------------------------------------------------------------------
# Core: submit + poll + download for one model
# ---------------------------------------------------------------------------

def run_one(provider: dict, config: Config) -> ProviderResult:
    result = ProviderResult(
        name=provider["name"],
        model=provider["model"],
        tier=provider["tier"],
        cost_usd=provider["cost_usd"],
        duration_req=provider["duration"],
    )

    log = get_logger(episode_id="compare_v1", agent=provider["model"])
    model = provider["model"]
    endpoint = f"{config.MUAPI_BASE_URL}/{model}"
    payload  = _build_job_payload(PROMPT, model, ASPECT_RATIO, DURATION, None)
    out_path = OUTPUT_DIR / f"{model}.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n  [{provider['name']}] submitting to {endpoint} …")
    t_start = time.monotonic()

    request_id, failure = _submit_http(endpoint, payload, config, model, log)
    if request_id is None:
        result.error = failure
        print(f"  [{provider['name']}] SUBMIT FAILED: {failure}")
        return result

    result.request_id = request_id
    print(f"  [{provider['name']}] job {request_id[:12]}… polling …")

    poll_result, failure = _poll_until_complete(request_id, config, model, log)
    if poll_result is None:
        result.error = failure
        result.gen_time_s = round(time.monotonic() - t_start, 1)
        print(f"  [{provider['name']}] POLL FAILED ({result.gen_time_s}s): {failure}")
        return result

    outputs  = poll_result.get("outputs", [])
    video_url = ""
    if outputs:
        video_url = outputs[0] if isinstance(outputs[0], str) else outputs[0].get("url", "")
    if not video_url:
        result.error = "missing_video_url_in_result"
        result.gen_time_s = round(time.monotonic() - t_start, 1)
        print(f"  [{provider['name']}] NO VIDEO URL in poll result")
        return result

    print(f"  [{provider['name']}] downloading …")
    ok, dl_reason = _download_file(video_url, out_path, log)
    if not ok:
        ok, dl_reason = _download_file(video_url, out_path, log)
    if not ok:
        result.error = f"download: {dl_reason}"
        result.gen_time_s = round(time.monotonic() - t_start, 1)
        print(f"  [{provider['name']}] DOWNLOAD FAILED: {dl_reason}")
        return result

    elapsed = round(time.monotonic() - t_start, 1)
    size_kb  = round(out_path.stat().st_size / 1024, 1)

    result.success    = True
    result.file_path  = str(out_path)
    result.file_size_kb = size_kb
    result.gen_time_s = elapsed
    print(f"  [{provider['name']}] OK  {elapsed}s  {size_kb} KB  → {out_path.name}")
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    config = Config()

    print("=" * 62)
    print("  VIDEO PROVIDER COMPARISON — Hypogeum Cinematic v1")
    print("=" * 62)
    print(f"\nPrompt ({len(PROMPT.split())} words):")
    print(f"  {PROMPT[:120]}…\n")
    print(f"Providers: {len(PROVIDERS)}")
    for p in PROVIDERS:
        print(f"  {p['name']:22}  {p['model']:32}  ${p['cost_usd']:.2f}")
    print(f"\nOutput dir: {OUTPUT_DIR}\n")
    print("-" * 62)

    results: list[ProviderResult] = []
    for provider in PROVIDERS:
        r = run_one(provider, config)
        results.append(r)

    # Save manifest
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "prompt": PROMPT,
        "prompt_words": len(PROMPT.split()),
        "prompt_blueprint": "vpd_cinematic_prompt_skill_v1",
        "scene_ref": "s02",
        "providers": [
            {
                "name":         r.name,
                "model":        r.model,
                "tier":         r.tier,
                "cost_usd":     r.cost_usd,
                "duration_req": r.duration_req,
                "success":      r.success,
                "file_path":    r.file_path,
                "file_size_kb": r.file_size_kb,
                "gen_time_s":   r.gen_time_s,
                "error":        r.error,
                "request_id":   r.request_id,
            }
            for r in results
        ],
    }
    manifest_path = OUTPUT_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # Print summary
    print("\n" + "=" * 62)
    print("  RESULTS")
    print("=" * 62)
    for r in results:
        status = "OK" if r.success else "FAIL"
        time_s = f"{r.gen_time_s}s" if r.gen_time_s else "—"
        size   = f"{r.file_size_kb} KB" if r.file_size_kb else "—"
        err    = f"  ← {r.error}" if r.error else ""
        print(f"  {status:4}  {r.name:22}  {r.model:32}  {time_s:8}  {size:10}  ${r.cost_usd:.2f}{err}")

    print(f"\nManifest: {manifest_path}")
    total_cost = sum(r.cost_usd for r in results if r.success)
    print(f"Total cost (successful): ${total_cost:.2f}")


if __name__ == "__main__":
    main()
