#!/usr/bin/env python3
"""
scripts/compare_higgsfield_vs_muapi.py
Provider comparison: Higgsfield DoP (I2V) vs MUAPI Veo3-fast (T2V and I2V)

Usage:
    # Dry run (no credits spent, tests the full runner flow)
    python3 scripts/compare_higgsfield_vs_muapi.py --dry-run

    # Live run — all three legs: MUAPI T2V, MUAPI I2V, Higgsfield dop-lite
    python3 scripts/compare_higgsfield_vs_muapi.py

    # Live run — select specific legs only
    python3 scripts/compare_higgsfield_vs_muapi.py --legs muapi_t2v higgsfield_dop_lite

    # Live run — all Higgsfield tiers (dop-lite + dop-turbo + dop-preview)
    python3 scripts/compare_higgsfield_vs_muapi.py --all-higgsfield-tiers

    # Override seed for reproducibility
    python3 scripts/compare_higgsfield_vs_muapi.py --seed 42 --dry-run

Outputs:
    .hypogeum-assets/provider_comparisons/higgsfield_vs_muapi_<timestamp>/
      scorecard.json          — full machine-readable result
      scorecard_summary.txt   — human-readable summary
      muapi_t2v/              — clip + metadata if live
      muapi_i2v/              — clip + metadata if live
      higgsfield_dop_lite/    — clip + metadata if live
      higgsfield_dop_turbo/   — clip + metadata if live (if --all-higgsfield-tiers)
      higgsfield_dop_preview/ — clip + metadata if live (if --all-higgsfield-tiers)

Hard constraints:
    - MUAPI_DRY_RUN in .env still takes effect (if set, MUAPI legs are also dry)
    - --dry-run flag forces ALL legs to dry-run regardless of .env
    - Never touches production DB or episode assets
    - All credentials come from Config only
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from hypogeum.config import Config
from hypogeum.logger import get_logger

# ---------------------------------------------------------------------------
# Benchmark prompt — same scene used for every leg.
# Chosen to stress historical-documentary requirements:
# torchlit underground space, period-accurate materials, cinematic motion.
# ---------------------------------------------------------------------------

BENCHMARK_PROMPT = (
    "Extreme low angle looking upward through a circular Roman brick shaft, "
    "amber torchlight raking across the worn brick walls, individual mortar joints "
    "deeply recessed and darkened by centuries of moisture, corroded iron staple "
    "anchors set at irregular intervals, a pale disc of arena light visible "
    "at the shaft summit far above. Suspended dust motes drift in slow thermal "
    "convection through the shaft column, catching the amber light as bright "
    "gold specks. Camera: controlled slow crane tilt upward through the shaft. "
    "The thermal drift of dust motes gives the frame continuous micro-motion "
    "against the held stone. Colour grade: warm amber-copper torchlight against "
    "deep unlit brick shadow. 4K, 24fps, depth of field: medium, no vignette, "
    "no modern concrete or steel, no electric lighting, no fluorescent wash, "
    "no ambient fill, no visible modern infrastructure."
)

# Reference image for I2V paths — generated or supplied externally
_DEFAULT_REFERENCE_IMAGE = (
    _REPO_ROOT / ".hypogeum-assets" / "probe" / "t2v_probe" / "reference_image.jpg"
)

# ---------------------------------------------------------------------------
# Leg definitions
# ---------------------------------------------------------------------------

ALL_LEGS = {
    "muapi_t2v": {
        "provider": "muapi",
        "mode": "t2v",
        "model": "veo3-fast-text-to-video",
        "label": "MUAPI veo3-fast (T2V)",
        "price_per_clip_usd": 0.60,
        "clip_duration_s": 8,
    },
    "muapi_i2v": {
        "provider": "muapi",
        "mode": "i2v",
        "model": "veo3-fast-image-to-video",
        "label": "MUAPI veo3-fast (I2V)",
        "price_per_clip_usd": 0.60,
        "clip_duration_s": 8,
    },
    "higgsfield_dop_lite": {
        "provider": "higgsfield",
        "mode": "i2v",
        "model": "dop-lite",
        "label": "Higgsfield DoP Lite (I2V)",
        "price_per_clip_usd": 0.14,
        "clip_duration_s": 5,
    },
    "higgsfield_dop_turbo": {
        "provider": "higgsfield",
        "mode": "i2v",
        "model": "dop-turbo",
        "label": "Higgsfield DoP Turbo (I2V)",
        "price_per_clip_usd": 0.42,
        "clip_duration_s": 5,
    },
    "higgsfield_dop_preview": {
        "provider": "higgsfield",
        "mode": "i2v",
        "model": "dop-preview",
        "label": "Higgsfield DoP Preview (I2V)",
        "price_per_clip_usd": 0.57,
        "clip_duration_s": 5,
    },
}

DEFAULT_LEGS = ["muapi_t2v", "muapi_i2v", "higgsfield_dop_lite"]


# ---------------------------------------------------------------------------
# MUAPI leg runner
# ---------------------------------------------------------------------------

def _run_muapi_leg(
    cfg: Config,
    leg_key: str,
    leg: dict,
    out_dir: Path,
    prompt: str,
    seed: Optional[int],
    reference_image_url: Optional[str],
    dry_run: bool,
) -> dict:
    """Run one MUAPI leg and return a result dict."""
    import requests as _req

    log = get_logger()
    mode = leg["mode"]
    model = leg["model"]
    submit_time = time.time()

    if dry_run or cfg.MUAPI_DRY_RUN:
        fake_id = f"dry_muapi_{uuid.uuid4().hex[:10]}"
        log.info("comparison_muapi_dry_run", leg=leg_key, model=model)
        return _build_result(
            leg_key=leg_key,
            leg=leg,
            status="dry_run",
            output_path=None,
            request_id=fake_id,
            submit_time=submit_time,
            complete_time=time.time(),
            actual_cost=leg["price_per_clip_usd"],
            error=None,
            dry_run=True,
        )

    api_key = cfg.MUAPI_API_KEY
    if not api_key:
        return _build_result(leg_key, leg, "skipped", None, None,
                             submit_time, time.time(), 0.0,
                             "MUAPI_API_KEY not configured", False)

    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    payload: dict = {"prompt": prompt}
    if mode == "i2v" and reference_image_url:
        payload["images_list"] = [reference_image_url]
    if seed is not None:
        payload["seed"] = seed

    endpoint_slug = model
    submit_r = _req.post(
        f"{cfg.MUAPI_BASE_URL}/{endpoint_slug}",
        json=payload,
        headers=headers,
        timeout=30,
    )

    if submit_r.status_code not in (200, 201, 202):
        return _build_result(leg_key, leg, "failed", None, None,
                             submit_time, time.time(), 0.0,
                             f"submit HTTP {submit_r.status_code}: {submit_r.text[:200]}", False)

    resp = submit_r.json()
    request_id = (resp.get("request_id") or resp.get("id") or
                  resp.get("prediction_id") or "unknown")
    actual_cost = float(resp.get("cost", {}).get("amount_usd", 0) or 0)
    log.info("comparison_muapi_submitted", leg=leg_key,
             request_id=request_id, model=model)

    # Poll
    timeout = cfg.MUAPI_TIMEOUT_SECONDS
    poll_interval = cfg.MUAPI_POLL_INTERVAL_SECONDS
    deadline = time.time() + timeout

    while time.time() < deadline:
        time.sleep(poll_interval)
        poll_r = _req.get(
            f"{cfg.MUAPI_BASE_URL}/predictions/{request_id}/result",
            headers={"x-api-key": api_key},
            timeout=20,
        )
        if poll_r.status_code != 200:
            continue

        pdata = poll_r.json()
        status = (pdata.get("status") or "").lower()
        log.info("comparison_muapi_poll", leg=leg_key,
                 request_id=request_id, status=status)

        if status == "completed":
            outputs = pdata.get("outputs", [])
            output_url = outputs[0] if outputs else None
            complete_time = time.time()

            dest_path: Optional[Path] = None
            if output_url:
                dest_path = out_dir / f"{leg_key}.mp4"
                try:
                    dl = _req.get(output_url, stream=True, timeout=120)
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(dest_path, "wb") as f:
                        for chunk in dl.iter_content(chunk_size=8 * 1024 * 1024):
                            f.write(chunk)
                except Exception as dl_err:
                    log.error("comparison_muapi_download_failed",
                              leg=leg_key, error=str(dl_err))
                    dest_path = None

            return _build_result(leg_key, leg, "completed", dest_path, request_id,
                                 submit_time, complete_time,
                                 actual_cost or leg["price_per_clip_usd"], None, False)

        if status == "failed":
            return _build_result(leg_key, leg, "failed", None, request_id,
                                 submit_time, time.time(), 0.0,
                                 "provider reported failed status", False)

    return _build_result(leg_key, leg, "timeout", None, request_id,
                         submit_time, time.time(), 0.0, "polling timeout", False)


# ---------------------------------------------------------------------------
# Higgsfield leg runner
# ---------------------------------------------------------------------------

def _run_higgsfield_leg(
    cfg: Config,
    leg_key: str,
    leg: dict,
    out_dir: Path,
    prompt: str,
    seed: Optional[int],
    reference_image_path: Optional[Path],
    dry_run: bool,
) -> dict:
    """Run one Higgsfield DoP leg and return a result dict."""
    from hypogeum.higgsfield import (
        HiggsFieldAuthError,
        HiggsFieldError,
        HiggsFieldQuotaError,
        download_clip,
        submit_dop_generation,
        upload_reference_image,
    )

    log = get_logger()
    model = leg["model"]
    submit_time = time.time()

    if not getattr(cfg, "HF_API_KEY", None) or not getattr(cfg, "HF_API_SECRET", None):
        return _build_result(leg_key, leg, "skipped", None, None,
                             submit_time, time.time(), 0.0,
                             "HF_API_KEY or HF_API_SECRET not configured", dry_run)

    # Upload reference image to get a CDN URL
    image_url: Optional[str] = None
    if not dry_run and not cfg.HIGGSFIELD_DRY_RUN:
        if reference_image_path and reference_image_path.exists():
            try:
                image_url = upload_reference_image(cfg, reference_image_path)
            except HiggsFieldError as e:
                return _build_result(leg_key, leg, "failed", None, None,
                                     submit_time, time.time(), 0.0,
                                     f"image upload failed: {e}", False)
        else:
            return _build_result(leg_key, leg, "skipped", None, None,
                                 submit_time, time.time(), 0.0,
                                 "no reference image available for I2V", dry_run)

    try:
        result = submit_dop_generation(
            cfg,
            model=model,
            prompt=prompt,
            image_url=image_url or "https://placeholder.invalid/image.jpg",
            seed=seed,
            duration=leg["clip_duration_s"],
            dry_run=(dry_run or getattr(cfg, "HIGGSFIELD_DRY_RUN", False)),
        )
    except HiggsFieldAuthError as e:
        return _build_result(leg_key, leg, "auth_failed", None, None,
                             submit_time, time.time(), 0.0, str(e), False)
    except HiggsFieldQuotaError as e:
        return _build_result(leg_key, leg, "quota_exhausted", None, None,
                             submit_time, time.time(), 0.0, str(e), False)
    except HiggsFieldError as e:
        return _build_result(leg_key, leg, "failed", None, None,
                             submit_time, time.time(), 0.0, str(e), False)

    status = result["status"]
    complete_time = result["complete_time"]
    actual_cost = result["cost_usd"]

    dest_path: Optional[Path] = None
    if status == "Completed" and result.get("output_url"):
        dest_path = out_dir / f"{leg_key}.mp4"
        try:
            download_clip(result["output_url"], dest_path)
        except HiggsFieldError as e:
            log.error("comparison_hf_download_failed", leg=leg_key, error=str(e))
            dest_path = None

    normalised_status = {
        "Completed": "completed",
        "Failed": "failed",
        "timeout": "timeout",
        "dry_run": "dry_run",
        "NSFW": "nsfw_rejected",
        "Cancelled": "cancelled",
    }.get(status, status.lower())

    return _build_result(
        leg_key, leg, normalised_status, dest_path,
        result.get("request_id"), submit_time, complete_time,
        actual_cost, None, result.get("dry_run", False)
    )


# ---------------------------------------------------------------------------
# Result builder
# ---------------------------------------------------------------------------

def _build_result(
    leg_key: str,
    leg: dict,
    status: str,
    output_path: Optional[Path],
    request_id: Optional[str],
    submit_time: float,
    complete_time: float,
    actual_cost: float,
    error: Optional[str],
    dry_run: bool,
) -> dict:
    elapsed = round(complete_time - submit_time, 1)
    file_size_mb: Optional[float] = None
    clip_duration_s: Optional[float] = None

    if output_path and output_path.exists():
        file_size_mb = round(output_path.stat().st_size / 1024 / 1024, 2)
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format",
                 str(output_path)],
                capture_output=True, text=True, timeout=15
            )
            fmt = json.loads(probe.stdout).get("format", {})
            clip_duration_s = round(float(fmt.get("duration", 0)), 2)
        except Exception:
            pass

    return {
        "leg_key": leg_key,
        "provider": leg["provider"],
        "mode": leg["mode"],
        "model": leg["model"],
        "label": leg["label"],
        "status": status,
        "request_id": request_id,
        "submit_time_utc": datetime.fromtimestamp(submit_time, tz=timezone.utc).isoformat(),
        "complete_time_utc": datetime.fromtimestamp(complete_time, tz=timezone.utc).isoformat(),
        "elapsed_seconds": elapsed,
        "expected_cost_usd": leg["price_per_clip_usd"],
        "actual_cost_usd": actual_cost,
        "clip_duration_seconds": clip_duration_s or leg["clip_duration_s"],
        "file_size_mb": file_size_mb,
        "output_path": str(output_path) if output_path else None,
        "error": error,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# Scorecard builder
# ---------------------------------------------------------------------------

def _build_scorecard(
    results: list[dict],
    run_id: str,
    prompt: str,
    seed: Optional[int],
) -> dict:
    """Build the full scorecard from per-leg results."""
    completed = [r for r in results if r["status"] in ("completed", "dry_run")]
    failed = [r for r in results if r["status"] not in ("completed", "dry_run", "skipped")]
    skipped = [r for r in results if r["status"] == "skipped"]

    # Cost-per-8s normalisation (Higgsfield clips are 5s, MUAPI is 8s)
    def cost_per_8s(r: dict) -> float:
        raw = r["actual_cost_usd"] or r["expected_cost_usd"]
        dur = r["clip_duration_seconds"] or 5
        return round(raw * (8.0 / dur), 4) if dur > 0 else raw

    timing = {r["leg_key"]: r["elapsed_seconds"] for r in results}
    costs = {r["leg_key"]: cost_per_8s(r) for r in results}

    # Simple recommendation logic
    recommendation: dict = {}
    muapi_t2v = next((r for r in results if r["leg_key"] == "muapi_t2v"), None)
    hf_lite = next((r for r in results if r["leg_key"] == "higgsfield_dop_lite"), None)
    hf_turbo = next((r for r in results if r["leg_key"] == "higgsfield_dop_turbo"), None)
    hf_preview = next((r for r in results if r["leg_key"] == "higgsfield_dop_preview"), None)

    muapi_cost_8s = costs.get("muapi_t2v", 0.60)
    hf_lite_cost_8s = costs.get("higgsfield_dop_lite", 0.22)

    if all(r["dry_run"] for r in results):
        recommendation = {
            "verdict": "dry_run_only",
            "main_provider": "pending_live_test",
            "fallback_provider": "pending_live_test",
            "cost_threshold_usd_per_8s": None,
            "notes": "Run without --dry-run to get live timing and quality data.",
        }
    elif hf_lite and hf_lite["status"] == "completed":
        savings_pct = round((1 - hf_lite_cost_8s / muapi_cost_8s) * 100, 1) if muapi_cost_8s else 0
        recommendation = {
            "verdict": "data_available",
            "main_provider": "see_quality_review",
            "fallback_provider": "muapi_t2v",
            "cost_per_8s": {
                "muapi_t2v": muapi_cost_8s,
                "higgsfield_dop_lite": costs.get("higgsfield_dop_lite"),
                "higgsfield_dop_turbo": costs.get("higgsfield_dop_turbo"),
                "higgsfield_dop_preview": costs.get("higgsfield_dop_preview"),
            },
            "higgsfield_dop_lite_savings_vs_muapi_t2v_pct": savings_pct,
            "notes": (
                "Review output clips manually for quality before switching production default. "
                "Higgsfield DoP is I2V only — requires reference image pipeline. "
                "MUAPI T2V is the simpler production path (no reference image needed)."
            ),
        }
    else:
        recommendation = {
            "verdict": "insufficient_data",
            "main_provider": "muapi_t2v",
            "fallback_provider": "muapi_t2v",
            "notes": "Higgsfield legs failed or were skipped. Keep MUAPI as default.",
        }

    return {
        "scorecard_id": f"sc_{run_id}",
        "run_id": run_id,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "benchmark_prompt": prompt,
        "seed": seed,
        "legs_run": len(results),
        "legs_completed": len(completed),
        "legs_failed": len(failed),
        "legs_skipped": len(skipped),
        "results": results,
        "timing_seconds": timing,
        "cost_per_8s_usd": costs,
        "recommendation": recommendation,
        "dimensions": {
            "cost_per_8s_usd": costs,
            "elapsed_seconds": timing,
            "quality_rating": {
                k: "PENDING — manual review required"
                for k in timing
            },
            "cinematic_motion": {
                k: "PENDING — manual review required"
                for k in timing
            },
            "prompt_adherence": {
                k: "PENDING — manual review required"
                for k in timing
            },
            "historical_documentary_suitability": {
                k: "PENDING — manual review required"
                for k in timing
            },
            "freeze_frame_risk": {
                k: "PENDING — inspect output clip"
                for k in timing
            },
            "watermark": {
                "muapi_t2v": "none (paid account)",
                "muapi_i2v": "none (paid account)",
                "higgsfield_dop_lite": "none on paid plan",
                "higgsfield_dop_turbo": "none on paid plan",
                "higgsfield_dop_preview": "none on paid plan",
            },
            "commercial_rights": {
                "muapi_t2v": "allowed (paid)",
                "muapi_i2v": "allowed (paid)",
                "higgsfield_dop_lite": "allowed (paid plan)",
                "higgsfield_dop_turbo": "allowed (paid plan)",
                "higgsfield_dop_preview": "allowed (paid plan)",
            },
            "api_reliability": {
                "muapi_t2v": "CONFIRMED — production-validated 2026-06-27",
                "muapi_i2v": "CONFIRMED — production-validated 2026-06-27",
                "higgsfield_dop_lite": "PENDING — first live test",
                "higgsfield_dop_turbo": "PENDING — first live test",
                "higgsfield_dop_preview": "PENDING — first live test",
            },
            "seed_support": {
                "muapi": "YES — seed parameter accepted",
                "higgsfield": "YES — seed 1-1,000,000",
            },
            "duration_options": {
                "muapi_veo3_fast": "8s (fixed by provider)",
                "higgsfield_dop": "5s default (8s unconfirmed)",
            },
            "t2v_available": {
                "muapi": True,
                "higgsfield": False,
            },
            "i2v_available": {
                "muapi": True,
                "higgsfield": True,
            },
            "reference_image_required": {
                "muapi_t2v": False,
                "muapi_i2v": True,
                "higgsfield_dop_lite": True,
                "higgsfield_dop_turbo": True,
                "higgsfield_dop_preview": True,
            },
        },
        "known_limitations": {
            "higgsfield": [
                "DoP models are Image-to-Video only — no T2V equivalent",
                "Default duration is 5s; 8s support unconfirmed by official docs",
                "Rate limits not publicly documented",
                "Platform launched ~2024; fewer production references vs MUAPI",
                "Requires file-upload step to get hosted image URL before generation",
            ],
            "muapi": [
                "Dynamic pricing — costs may change without notice",
                "Veo3-fast clips fixed at 8s (no shorter option)",
                "Aggregator dependency — underlying provider outage affects all tiers",
            ],
        },
    }


# ---------------------------------------------------------------------------
# Summary text renderer
# ---------------------------------------------------------------------------

def _render_summary(scorecard: dict) -> str:
    lines = [
        "=" * 72,
        "HIGGSFIELD vs MUAPI — PROVIDER COMPARISON SCORECARD",
        f"Run ID: {scorecard['run_id']}",
        f"Generated: {scorecard['generated_at']}",
        "=" * 72,
        "",
        "BENCHMARK PROMPT (truncated):",
        f"  {scorecard['benchmark_prompt'][:140]}...",
        f"  Seed: {scorecard['seed']}",
        "",
        "RESULTS PER LEG:",
        "-" * 72,
    ]

    for r in scorecard["results"]:
        lines += [
            f"  {r['label']}",
            f"    Status:        {r['status']}",
            f"    Elapsed:       {r['elapsed_seconds']}s",
            f"    Cost (actual): ${r['actual_cost_usd']:.4f}",
            f"    Cost/8s:       ${scorecard['cost_per_8s_usd'].get(r['leg_key'], 0):.4f}",
            f"    Clip duration: {r['clip_duration_seconds']}s",
            f"    File size:     {r['file_size_mb']}MB" if r['file_size_mb'] else "    File size:     n/a",
            f"    Output:        {r['output_path'] or 'n/a'}",
            f"    Error:         {r['error'] or 'none'}",
            "",
        ]

    lines += [
        "-" * 72,
        "COST COMPARISON (normalised to 8s clip):",
    ]
    for leg_key, cost in sorted(scorecard["cost_per_8s_usd"].items(),
                                 key=lambda x: x[1]):
        lines.append(f"  {leg_key:<32} ${cost:.4f}/8s")

    lines += [
        "",
        "TIMING COMPARISON:",
    ]
    for leg_key, t in sorted(scorecard["timing_seconds"].items(), key=lambda x: x[1]):
        lines.append(f"  {leg_key:<32} {t:.1f}s")

    rec = scorecard["recommendation"]
    lines += [
        "",
        "=" * 72,
        "RECOMMENDATION:",
        f"  Verdict:          {rec.get('verdict')}",
        f"  Main provider:    {rec.get('main_provider')}",
        f"  Fallback:         {rec.get('fallback_provider')}",
        f"  Notes:            {rec.get('notes', '')}",
        "",
        "DIMENSIONS REQUIRING MANUAL REVIEW:",
        "  - Visual quality rating",
        "  - Cinematic motion quality",
        "  - Prompt adherence",
        "  - Historical-documentary suitability",
        "  - Freeze-frame risk",
        "",
        "KNOWN LIMITATIONS:",
        "  Higgsfield:",
    ]
    for lim in scorecard.get("known_limitations", {}).get("higgsfield", []):
        lines.append(f"    • {lim}")
    lines += ["  MUAPI:"]
    for lim in scorecard.get("known_limitations", {}).get("muapi", []):
        lines.append(f"    • {lim}")

    lines += [
        "",
        "OUTPUT CLIPS FOR MANUAL REVIEW:",
        f"  {Path(scorecard['results'][0]['output_path']).parent if scorecard['results'] and scorecard['results'][0]['output_path'] else 'n/a'}",
        "=" * 72,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare Higgsfield DoP vs MUAPI Veo3-fast"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="No real API calls; test the runner flow")
    parser.add_argument("--legs", nargs="+", choices=list(ALL_LEGS.keys()),
                        default=DEFAULT_LEGS,
                        help="Which legs to run (default: muapi_t2v muapi_i2v higgsfield_dop_lite)")
    parser.add_argument("--all-higgsfield-tiers", action="store_true",
                        help="Run all three Higgsfield tiers (dop-lite + dop-turbo + dop-preview)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Seed for reproducibility (default: 42)")
    parser.add_argument("--reference-image", type=Path, default=None,
                        help="Local image path to use for I2V legs")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Override output directory")
    args = parser.parse_args(argv)

    cfg = Config()
    log = get_logger()

    # Determine legs
    legs_to_run = list(args.legs)
    if args.all_higgsfield_tiers:
        for hf_leg in ("higgsfield_dop_lite", "higgsfield_dop_turbo", "higgsfield_dop_preview"):
            if hf_leg not in legs_to_run:
                legs_to_run.append(hf_leg)

    # Output directory
    run_id = f"hf_vs_muapi_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    base_out = (args.output_dir or
                _REPO_ROOT / ".hypogeum-assets" / "provider_comparisons" / run_id)
    base_out.mkdir(parents=True, exist_ok=True)

    # Reference image for I2V legs
    reference_image_path = args.reference_image or _DEFAULT_REFERENCE_IMAGE
    if not reference_image_path.exists():
        print(
            f"WARNING: Reference image not found at {reference_image_path}\n"
            f"         I2V legs (muapi_i2v, higgsfield_*) will be skipped.\n"
            f"         Provide --reference-image <path> or place an image at:\n"
            f"         {reference_image_path}"
        )

    print(f"\nComparison run: {run_id}")
    print(f"Output dir:     {base_out}")
    print(f"Dry run:        {args.dry_run}")
    print(f"Seed:           {args.seed}")
    print(f"Legs:           {legs_to_run}")
    print()

    # Pre-flight credential check
    hf_legs = [k for k in legs_to_run if k.startswith("higgsfield")]
    if hf_legs and not args.dry_run:
        from hypogeum.higgsfield import check_credentials
        cred_check = check_credentials(cfg)
        if not cred_check["valid"]:
            print(f"ERROR: Higgsfield credentials invalid — {cred_check['detail']}")
            print("       Set HF_API_KEY and HF_API_SECRET in .env")
            print("       Account: https://cloud.higgsfield.ai/sign-up")
            if not any(k for k in legs_to_run if not k.startswith("higgsfield")):
                return 1
            print("       Higgsfield legs will be marked as skipped. Continuing MUAPI legs.\n")

    # Upload reference image to MUAPI for I2V leg (get hosted URL)
    muapi_reference_url: Optional[str] = None
    if "muapi_i2v" in legs_to_run and not args.dry_run and not cfg.MUAPI_DRY_RUN:
        if reference_image_path.exists():
            try:
                import requests as _req
                with open(reference_image_path, "rb") as f:
                    img_data = f.read()
                # Upload to MUAPI file storage
                up_r = _req.post(
                    f"{cfg.MUAPI_BASE_URL}/upload_file",
                    headers={"x-api-key": cfg.MUAPI_API_KEY},
                    files={"file": ("reference.jpg", img_data, "image/jpeg")},
                    timeout=60,
                )
                if up_r.status_code == 200:
                    muapi_reference_url = up_r.json().get("url") or up_r.json().get("file_url")
                    print(f"Reference image uploaded to MUAPI: {muapi_reference_url}")
                else:
                    print(f"MUAPI image upload failed ({up_r.status_code}); I2V leg may fail")
            except Exception as e:
                print(f"MUAPI image upload error: {e}; I2V leg may fail")

    # Run legs sequentially
    results: list[dict] = []
    for leg_key in legs_to_run:
        leg = ALL_LEGS[leg_key]
        leg_out_dir = base_out / leg_key
        leg_out_dir.mkdir(parents=True, exist_ok=True)

        print(f"Running leg: {leg['label']} ...", flush=True)
        start = time.time()

        if leg["provider"] == "muapi":
            result = _run_muapi_leg(
                cfg, leg_key, leg, leg_out_dir,
                BENCHMARK_PROMPT, args.seed,
                muapi_reference_url if leg["mode"] == "i2v" else None,
                args.dry_run,
            )
        else:  # higgsfield
            result = _run_higgsfield_leg(
                cfg, leg_key, leg, leg_out_dir,
                BENCHMARK_PROMPT, args.seed,
                reference_image_path,
                args.dry_run,
            )

        elapsed = round(time.time() - start, 1)
        print(f"  → {result['status']} in {elapsed}s  "
              f"cost=${result['actual_cost_usd']:.4f}  "
              f"{'✓ ' + str(result['output_path']) if result['output_path'] else '✗ no output'}")
        results.append(result)

        # Write per-leg metadata
        meta_path = leg_out_dir / "metadata.json"
        meta_path.write_text(json.dumps(result, indent=2))

    # Build and write scorecard
    scorecard = _build_scorecard(results, run_id, BENCHMARK_PROMPT, args.seed)
    scorecard_path = base_out / "scorecard.json"
    scorecard_path.write_text(json.dumps(scorecard, indent=2))

    summary_text = _render_summary(scorecard)
    summary_path = base_out / "scorecard_summary.txt"
    summary_path.write_text(summary_text)

    print()
    print(summary_text)
    print(f"\nScorecard written: {scorecard_path}")
    print(f"Summary written:   {summary_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
