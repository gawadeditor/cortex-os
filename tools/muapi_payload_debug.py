#!/usr/bin/env python3
"""
tools/muapi_payload_debug.py — Print exact outgoing MUAPI payload before generation.

Reads config from .env, builds the payload exactly as submit_scene() would, and
prints it to stdout. Makes ZERO API calls and spends ZERO credits.

Usage:
    python3 tools/muapi_payload_debug.py
    python3 tools/muapi_payload_debug.py --tier 1
    python3 tools/muapi_payload_debug.py --model veo3.1-fast-text-to-video
    python3 tools/muapi_payload_debug.py --model veo3-fast-text-to-video   # legacy
    python3 tools/muapi_payload_debug.py --resolution 720p
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.config import Config              # noqa: E402
from hypogeum.muapi import (                    # noqa: E402
    _build_job_payload,
    _tier_to_model,
    MODEL_CREDIT_COSTS,
    _DEFAULT_UNKNOWN_MODEL_COST,
    _VEO3_MODELS,
    _VEO3_1_MODELS,
    _RUNWAY_MODELS,
)

_SAMPLE_PROMPT = (
    "Medium shot looking down a Roman brick hypogeum tunnel — "
    "single torch in foreground, iron cage bars at mid-distance, "
    "shadowed figure with back to camera. Slow locked-off push. "
    "[4K] [24fps] [no vignette]"
)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tier", type=int, default=2, choices=[1, 2, 3],
                   help="Quality tier (default: 2 = T2_MODEL)")
    p.add_argument("--model", default=None,
                   help="Override model slug directly (bypasses tier lookup)")
    p.add_argument("--resolution", default=None,
                   help="Override video resolution (default: from config / MUAPI_VIDEO_RESOLUTION)")
    p.add_argument("--aspect-ratio", default="16:9")
    p.add_argument("--duration", type=float, default=8.0)
    p.add_argument("--ref-image", default=None,
                   help="Reference image URL (triggers I2V path)")
    args = p.parse_args()

    cfg = Config()

    # Resolve model
    if args.model:
        model = args.model
    else:
        model = _tier_to_model(args.tier, args.ref_image, cfg)

    # Resolve resolution
    video_resolution = args.resolution or cfg.MUAPI_VIDEO_RESOLUTION

    # Build payload exactly as submit_scene() would
    payload = _build_job_payload(
        prompt=_SAMPLE_PROMPT,
        model=model,
        aspect_ratio=args.aspect_ratio,
        requested_duration=args.duration,
        reference_image_url=args.ref_image,
        video_resolution=video_resolution,
    )

    endpoint = f"{cfg.MUAPI_BASE_URL}/{model}"
    cost = MODEL_CREDIT_COSTS.get(model, _DEFAULT_UNKNOWN_MODEL_COST)

    # Detect model family
    if model in _VEO3_1_MODELS:
        family = "veo3.1"
    elif model in _VEO3_MODELS:
        family = "veo3 (legacy)"
    elif model in _RUNWAY_MODELS:
        family = "runway"
    else:
        family = "unknown/image"

    print("=" * 62)
    print("MUAPI PAYLOAD DEBUG")
    print("=" * 62)
    print(f"endpoint       : POST {endpoint}")
    print(f"model          : {model}")
    print(f"model_family   : {family}")
    print(f"cost_per_gen   : ${cost:.2f}")
    print(f"video_resolution (config): {cfg.MUAPI_VIDEO_RESOLUTION}")
    print(f"dry_run        : {cfg.MUAPI_DRY_RUN}")
    print()
    print("OUTGOING PAYLOAD:")
    print(json.dumps(payload, indent=2))
    print()
    print("HEADERS (auth field masked):")
    print(json.dumps({
        "Content-Type": "application/json",
        "x-api-key": f"{cfg.MUAPI_API_KEY[:6]}…[masked]" if cfg.MUAPI_API_KEY else "NOT SET",
    }, indent=2))
    print("=" * 62)

    # Warn on mismatches
    issues = []
    if "resolution" not in payload and model in _VEO3_1_MODELS:
        issues.append("WARN: resolution missing from payload for veo3.1 model")
    if "duration" not in payload and model in _VEO3_1_MODELS:
        issues.append("WARN: duration missing from payload for veo3.1 model")
    if payload.get("resolution") and payload["resolution"] != "1080p":
        issues.append(f"NOTE: resolution={payload['resolution']} (not 1080p)")
    if not cfg.MUAPI_API_KEY:
        issues.append("ERROR: MUAPI_API_KEY not set — generation would fail")

    if issues:
        print()
        for i in issues:
            print(i)


if __name__ == "__main__":
    main()
