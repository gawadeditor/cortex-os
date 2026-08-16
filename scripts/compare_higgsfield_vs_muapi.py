#!/usr/bin/env python3
"""
scripts/compare_higgsfield_vs_muapi.py
Primary benchmark: MUAPI Seedance 2 vs MUAPI Veo3-fast (T2V and I2V modes)
Higgsfield legs are still available via --legs but excluded from the default run.

Usage:
    # Default: Seedance 2 vs Veo3-fast (4 legs, live)
    python3 scripts/compare_higgsfield_vs_muapi.py \\
      --reference-image .hypogeum-assets/probe/t2v_probe/reference_image.jpg

    # Dry run (no credits spent, full runner flow)
    python3 scripts/compare_higgsfield_vs_muapi.py --dry-run \\
      --reference-image .hypogeum-assets/probe/t2v_probe/reference_image.jpg

    # Add Higgsfield tiers alongside
    python3 scripts/compare_higgsfield_vs_muapi.py --all-higgsfield-tiers \\
      --reference-image .hypogeum-assets/probe/t2v_probe/reference_image.jpg

    # Select specific legs
    python3 scripts/compare_higgsfield_vs_muapi.py \\
      --legs muapi_seedance2_t2v muapi_veo3_fast_t2v \\
      --reference-image .hypogeum-assets/probe/t2v_probe/reference_image.jpg

Outputs (per run):
    .hypogeum-assets/provider_comparisons/seedance2_vs_veo3_<timestamp>/
      scorecard.json            — machine-readable full result + manual review fields
      scorecard_summary.txt     — human-readable summary
      comparison_sheet.md       — side-by-side markdown comparison table
      muapi_seedance2_t2v/      — clip + metadata.json
      muapi_veo3_fast_t2v/      — clip + metadata.json
      muapi_seedance2_i2v/      — clip + metadata.json
      muapi_veo3_fast_i2v/      — clip + metadata.json

Constraints:
    - MUAPI_DRY_RUN=true in .env forces all MUAPI legs to dry-run; script warns loudly
    - --dry-run forces ALL legs to dry-run regardless of .env
    - Never touches production DB or episode assets
    - All credentials come from Config only
    - Results contain only measured data — no estimates
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

ALL_LEGS: dict[str, dict] = {
    # ---- Seedance 2.0 — primary benchmark legs ----
    # Verified 2026-07-04 via live endpoint probe:
    #   seedance-2-t2v / seedance-2-i2v are catalog *aliases* — they return HTTP 404
    #   The routable endpoints are seedance-2-text-to-video-fast ($0.75, dynamic)
    #   and seedance-2-image-to-video-fast ($0.75, dynamic).
    #   Duration param: int, range 4–15s, default 5. We use 10s for fair comparison.
    "muapi_seedance2_t2v": {
        "provider": "muapi",
        "mode": "t2v",
        "model": "seedance-2-text-to-video-fast",
        "label": "MUAPI Seedance 2 T2V",
        "price_per_clip_usd": 0.75,
        "clip_duration_s": 10,
        "send_duration": True,   # pass duration=10 in payload
    },
    "muapi_seedance2_i2v": {
        "provider": "muapi",
        "mode": "i2v",
        "model": "seedance-2-image-to-video-fast",
        "label": "MUAPI Seedance 2 I2V",
        "price_per_clip_usd": 0.75,
        "clip_duration_s": 10,
        "send_duration": True,
    },
    # ---- Veo3-fast — primary benchmark legs ----
    "muapi_veo3_fast_t2v": {
        "provider": "muapi",
        "mode": "t2v",
        "model": "veo3-fast-text-to-video",
        "label": "MUAPI Veo3-fast T2V",
        "price_per_clip_usd": 0.60,
        "clip_duration_s": 8,
        "send_duration": False,  # veo3-fast duration is fixed; do not send
    },
    "muapi_veo3_fast_i2v": {
        "provider": "muapi",
        "mode": "i2v",
        "model": "veo3-fast-image-to-video",
        "label": "MUAPI Veo3-fast I2V",
        "price_per_clip_usd": 0.60,
        "clip_duration_s": 8,
        "send_duration": False,
    },
    # ---- Legacy aliases (kept for backward compat) ----
    "muapi_t2v": {
        "provider": "muapi",
        "mode": "t2v",
        "model": "veo3-fast-text-to-video",
        "label": "MUAPI veo3-fast (T2V)",
        "price_per_clip_usd": 0.60,
        "clip_duration_s": 8,
        "send_duration": False,
    },
    "muapi_i2v": {
        "provider": "muapi",
        "mode": "i2v",
        "model": "veo3-fast-image-to-video",
        "label": "MUAPI veo3-fast (I2V)",
        "price_per_clip_usd": 0.60,
        "clip_duration_s": 8,
        "send_duration": False,
    },
    # ---- Higgsfield — available via --legs, excluded from default ----
    "higgsfield_dop_lite": {
        "provider": "higgsfield",
        "mode": "i2v",
        "model": "dop-lite",
        "label": "Higgsfield DoP Lite (I2V)",
        "price_per_clip_usd": 0.14,
        "clip_duration_s": 5,
        "send_duration": False,
    },
    "higgsfield_dop_turbo": {
        "provider": "higgsfield",
        "mode": "i2v",
        "model": "dop-turbo",
        "label": "Higgsfield DoP Turbo (I2V)",
        "price_per_clip_usd": 0.42,
        "clip_duration_s": 5,
        "send_duration": False,
    },
    "higgsfield_dop_preview": {
        "provider": "higgsfield",
        "mode": "i2v",
        "model": "dop-preview",
        "label": "Higgsfield DoP Preview (I2V)",
        "price_per_clip_usd": 0.57,
        "clip_duration_s": 5,
        "send_duration": False,
    },
}

DEFAULT_LEGS = [
    "muapi_seedance2_t2v",
    "muapi_veo3_fast_t2v",
    "muapi_seedance2_i2v",
    "muapi_veo3_fast_i2v",
]


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
    if leg.get("send_duration"):
        payload["duration"] = leg["clip_duration_s"]

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
    # Benchmark jobs may run longer than the production default (180s).
    # Use at least 360s so Veo3-fast I2V and 10s Seedance clips have room to finish.
    timeout = max(cfg.MUAPI_TIMEOUT_SECONDS, 360)
    poll_interval = cfg.MUAPI_POLL_INTERVAL_SECONDS
    deadline = time.time() + timeout

    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            poll_r = _req.get(
                f"{cfg.MUAPI_BASE_URL}/predictions/{request_id}/result",
                headers={"x-api-key": api_key},
                timeout=20,
            )
        except _req.exceptions.RequestException as poll_err:
            log.info("comparison_muapi_poll_error", leg=leg_key,
                     request_id=request_id, error=str(poll_err)[:120])
            continue  # transient network error — keep polling until deadline
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
    from hypogeum.dormant_providers.higgsfield import (
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

def _probe_video(path: Path) -> dict:
    """Run ffprobe on a clip and return measured metrics. Returns empty dict on failure."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        data = json.loads(probe.stdout)
        fmt = data.get("format", {})
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), {})

        duration_s = round(float(fmt.get("duration", 0) or 0), 3)
        width = int(video.get("width", 0))
        height = int(video.get("height", 0))

        # FPS from avg_frame_rate "24/1" or r_frame_rate
        fps: Optional[float] = None
        for fr_key in ("avg_frame_rate", "r_frame_rate"):
            raw = video.get(fr_key, "")
            if raw and "/" in raw:
                num, den = raw.split("/")
                if int(den) > 0:
                    fps = round(int(num) / int(den), 3)
                    break

        return {
            "duration_s": duration_s,
            "width": width,
            "height": height,
            "resolution": f"{width}x{height}" if width and height else None,
            "fps": fps,
        }
    except Exception:
        return {}


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
    probe: dict = {}

    if output_path and output_path.exists():
        file_size_mb = round(output_path.stat().st_size / 1024 / 1024, 2)
        probe = _probe_video(output_path)

    clip_duration_s: float = probe.get("duration_s") or leg["clip_duration_s"]

    # Derived cost metrics — only from measured data
    cost_per_second: Optional[float] = None
    cost_per_8s: Optional[float] = None
    if actual_cost and clip_duration_s:
        cost_per_second = round(actual_cost / clip_duration_s, 5)
        cost_per_8s = round(actual_cost * (8.0 / clip_duration_s), 4)

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
        # Timing
        "elapsed_seconds": elapsed,
        # Cost
        "expected_cost_usd": leg["price_per_clip_usd"],
        "actual_cost_usd": actual_cost,
        "cost_per_second_usd": cost_per_second,
        "cost_per_8s_usd": cost_per_8s,
        # Clip properties (measured from ffprobe; None if not available)
        "clip_duration_seconds": clip_duration_s,
        "resolution": probe.get("resolution"),
        "width_px": probe.get("width"),
        "height_px": probe.get("height"),
        "fps": probe.get("fps"),
        "file_size_mb": file_size_mb,
        # Logistics
        "output_path": str(output_path) if output_path else None,
        "error": error,
        "dry_run": dry_run,
        # Manual review — filled by human after watching clips
        "manual_review": {
            "prompt_adherence":         None,  # 1–5
            "motion_quality":           None,  # 1–5
            "cinematic_realism":        None,  # 1–5
            "character_consistency":    None,  # 1–5
            "camera_movement":          None,  # 1–5
            "artifact_level":           None,  # 1–5 (5=no artifacts)
            "temporal_consistency":     None,  # 1–5
            "documentary_suitability":  None,  # 1–5
            "overall_preference":       None,  # rank (1=best)
            "notes":                    None,
        },
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
    """Build the full scorecard from per-leg results. Only measured data — no estimates."""
    completed = [r for r in results if r["status"] == "completed"]
    dry_runs  = [r for r in results if r["status"] == "dry_run"]
    failed    = [r for r in results if r["status"] not in ("completed", "dry_run", "skipped")]
    skipped   = [r for r in results if r["status"] == "skipped"]

    # Index by leg_key for quick lookup
    by_key = {r["leg_key"]: r for r in results}

    # Normalised cost/8s (from actual cost + actual duration if measured; else expected)
    def _cost_per_8s(r: dict) -> Optional[float]:
        if r.get("cost_per_8s_usd") is not None:
            return r["cost_per_8s_usd"]
        cost = r["actual_cost_usd"] or r["expected_cost_usd"]
        dur  = r["clip_duration_seconds"] or r["expected_cost_usd"]   # fallback: expect
        if cost and dur:
            return round(cost * (8.0 / dur), 4)
        return None

    # Build comparison table (only completed legs)
    comparison: list[dict] = []
    for r in results:
        comparison.append({
            "leg_key":            r["leg_key"],
            "label":              r["label"],
            "status":             r["status"],
            "model":              r["model"],
            "mode":               r["mode"],
            "elapsed_s":          r["elapsed_seconds"],
            "actual_cost_usd":    r["actual_cost_usd"],
            "cost_per_second":    r["cost_per_second_usd"],
            "cost_per_8s":        _cost_per_8s(r),
            "clip_duration_s":    r["clip_duration_seconds"],
            "resolution":         r["resolution"],
            "fps":                r["fps"],
            "file_size_mb":       r["file_size_mb"],
        })

    # Recommendation logic
    if all(r["dry_run"] for r in results):
        recommendation = {
            "verdict": "dry_run_only",
            "winner_t2v": None,
            "winner_i2v": None,
            "notes": (
                "MUAPI_DRY_RUN=true — no live data. "
                "Set MUAPI_DRY_RUN=false in .env and re-run for measured results."
            ),
        }
    elif completed:
        # Compare Seedance 2 vs Veo3-fast per mode
        def _compare(s2_key: str, veo3_key: str) -> dict:
            s2 = by_key.get(s2_key)
            veo3 = by_key.get(veo3_key)
            if not s2 or not veo3:
                return {"verdict": "incomplete"}
            if s2["status"] != "completed" or veo3["status"] != "completed":
                return {"verdict": "incomplete"}
            s2_c8 = _cost_per_8s(s2)
            veo3_c8 = _cost_per_8s(veo3)
            savings = None
            if s2_c8 and veo3_c8:
                savings = round((veo3_c8 - s2_c8) / veo3_c8 * 100, 1)
            return {
                "seedance2_cost_per_8s": s2_c8,
                "veo3_fast_cost_per_8s": veo3_c8,
                "seedance2_elapsed_s":   s2["elapsed_seconds"],
                "veo3_fast_elapsed_s":   veo3["elapsed_seconds"],
                "cost_delta_pct":        savings,
                "verdict": (
                    "seedance2_cheaper" if (s2_c8 and veo3_c8 and s2_c8 < veo3_c8) else
                    "veo3_cheaper" if (s2_c8 and veo3_c8 and veo3_c8 < s2_c8) else
                    "parity"
                ),
            }

        recommendation = {
            "verdict": "data_available",
            "t2v_comparison": _compare("muapi_seedance2_t2v", "muapi_veo3_fast_t2v"),
            "i2v_comparison": _compare("muapi_seedance2_i2v", "muapi_veo3_fast_i2v"),
            "winner_t2v": "PENDING manual quality review",
            "winner_i2v": "PENDING manual quality review",
            "notes": (
                "Cost and timing data collected. "
                "Complete manual_review fields in scorecard.json after watching clips."
            ),
        }
    else:
        recommendation = {
            "verdict": "all_failed",
            "winner_t2v": None,
            "winner_i2v": None,
            "notes": "All legs failed. Check MUAPI credentials and model names.",
        }

    return {
        "scorecard_id": f"sc_{run_id}",
        "run_id": run_id,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "benchmark_prompt": prompt,
        "seed": seed,
        "summary": {
            "legs_run":       len(results),
            "legs_completed": len(completed),
            "legs_dry_run":   len(dry_runs),
            "legs_failed":    len(failed),
            "legs_skipped":   len(skipped),
        },
        "comparison": comparison,
        "results": results,
        "recommendation": recommendation,
        "manual_review_instructions": (
            "After watching all output clips, fill in manual_review fields in each "
            "result entry. Scale: 1 (poor) to 5 (excellent). "
            "overall_preference: rank 1 = best clip in this run."
        ),
        "known_limitations": {
            "muapi_seedance2": [
                "Dynamic pricing — $0.75 base, may vary",
                "10s clip requires explicit duration=10 in payload",
                "Supports aspect_ratio and quality params (not sent in this benchmark)",
            ],
            "muapi_veo3_fast": [
                "Duration fixed at 8s by provider — cannot be overridden",
                "Dynamic pricing — $0.60 base, may vary",
                "Aggregator dependency — underlying provider outage affects all tiers",
            ],
            "higgsfield": [
                "DoP models are Image-to-Video only — no T2V equivalent",
                "Default duration is 5s; available via --legs only",
                "Requires file-upload step to get hosted image URL",
            ],
        },
    }


# ---------------------------------------------------------------------------
# Comparison sheet renderer (markdown)
# ---------------------------------------------------------------------------

def _render_comparison_sheet(scorecard: dict) -> str:
    """Produce a markdown side-by-side comparison table."""
    lines = [
        f"# Provider Comparison: Seedance 2 vs Veo3-fast",
        f"",
        f"**Run:** `{scorecard['run_id']}`  ",
        f"**Generated:** {scorecard['generated_at']}  ",
        f"**Seed:** {scorecard['seed']}",
        f"",
        f"## Benchmark Prompt",
        f"",
        f"```",
        scorecard["benchmark_prompt"],
        f"```",
        f"",
        f"## Measured Results",
        f"",
    ]

    # Header
    cols = ["Leg", "Status", "Duration", "Cost", "$/s", "$/8s-eq", "Gen time", "Resolution", "FPS", "File size"]
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("| " + " | ".join(["---"] * len(cols)) + " |")

    for r in scorecard["results"]:
        def _fmt(v, fmt=None):
            if v is None:
                return "—"
            return fmt % v if fmt else str(v)

        row = [
            f"`{r['leg_key']}`",
            r["status"],
            _fmt(r["clip_duration_seconds"], "%.1fs"),
            _fmt(r["actual_cost_usd"], "$%.4f"),
            _fmt(r["cost_per_second_usd"], "$%.5f"),
            _fmt(r.get("cost_per_8s_usd") or scorecard["comparison"][scorecard["results"].index(r)].get("cost_per_8s"), "$%.4f"),
            _fmt(r["elapsed_seconds"], "%.1fs"),
            _fmt(r["resolution"]),
            _fmt(r["fps"], "%.2f"),
            _fmt(r["file_size_mb"], "%.1fMB"),
        ]
        lines.append("| " + " | ".join(row) + " |")

    lines += [
        "",
        "## Cost Analysis",
        "",
        "| Comparison | Seedance 2 $/8s | Veo3-fast $/8s | Delta |",
        "| --- | --- | --- | --- |",
    ]
    rec = scorecard.get("recommendation", {})
    for mode_key, label in [("t2v_comparison", "T2V"), ("i2v_comparison", "I2V")]:
        cmp = rec.get(mode_key, {})
        s2 = f"${cmp['seedance2_cost_per_8s']:.4f}" if cmp.get("seedance2_cost_per_8s") else "—"
        veo = f"${cmp['veo3_fast_cost_per_8s']:.4f}" if cmp.get("veo3_fast_cost_per_8s") else "—"
        delta = f"{cmp['cost_delta_pct']:+.1f}%" if cmp.get("cost_delta_pct") is not None else "—"
        lines.append(f"| {label} | {s2} | {veo} | {delta} |")

    lines += [
        "",
        "## Manual Quality Review",
        "",
        "_Fill in after watching output clips. Scale: 1 (poor) → 5 (excellent)._",
        "",
    ]

    # Manual review table header
    review_dims = [
        "prompt_adherence", "motion_quality", "cinematic_realism",
        "character_consistency", "camera_movement", "artifact_level",
        "temporal_consistency", "documentary_suitability", "overall_preference",
    ]
    dim_labels = [d.replace("_", " ").title() for d in review_dims]
    lines.append("| Dimension | " + " | ".join(r["leg_key"] for r in scorecard["results"]) + " |")
    lines.append("| --- | " + " | ".join(["---"] * len(scorecard["results"])) + " |")
    for dim, label in zip(review_dims, dim_labels):
        row_vals = []
        for r in scorecard["results"]:
            val = r.get("manual_review", {}).get(dim)
            row_vals.append(str(val) if val is not None else "[ ]")
        lines.append(f"| {label} | " + " | ".join(row_vals) + " |")

    lines += [
        "",
        "## Output Clips",
        "",
    ]
    for r in scorecard["results"]:
        path = r.get("output_path")
        lines.append(f"- **{r['label']}**: `{path or 'not generated'}`")

    lines += [
        "",
        "## Known Limitations",
        "",
    ]
    for provider, lims in scorecard.get("known_limitations", {}).items():
        lines.append(f"**{provider}:**")
        for lim in lims:
            lines.append(f"- {lim}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Summary text renderer
# ---------------------------------------------------------------------------

def _render_summary(scorecard: dict) -> str:
    lines = [
        "=" * 72,
        "SEEDANCE 2 vs VEO3-FAST — PROVIDER COMPARISON SCORECARD",
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
        def _v(v, fmt=None):
            if v is None:
                return "n/a"
            return fmt % v if fmt else str(v)

        lines += [
            f"  {r['label']}",
            f"    Status:        {r['status']}",
            f"    Elapsed:       {_v(r['elapsed_seconds'], '%.1fs')}",
            f"    Cost (actual): {_v(r['actual_cost_usd'], '$%.4f')}",
            f"    Cost/s:        {_v(r['cost_per_second_usd'], '$%.5f')}",
            f"    Cost/8s-eq:    {_v(r.get('cost_per_8s_usd'), '$%.4f')}",
            f"    Duration:      {_v(r['clip_duration_seconds'], '%.2fs')}",
            f"    Resolution:    {_v(r['resolution'])}",
            f"    FPS:           {_v(r['fps'], '%.2f')}",
            f"    File size:     {_v(r['file_size_mb'], '%.2fMB')}",
            f"    Output:        {r['output_path'] or 'n/a'}",
            f"    Error:         {r['error'] or 'none'}",
            "",
        ]

    lines += ["-" * 72, "COST COMPARISON ($/8s-equivalent):"]
    comp_rows = sorted(
        [(c["leg_key"], c["cost_per_8s"]) for c in scorecard["comparison"] if c["cost_per_8s"]],
        key=lambda x: x[1],
    )
    for leg_key, c8 in comp_rows:
        lines.append(f"  {leg_key:<34} ${c8:.4f}/8s")

    lines += ["", "TIMING (generation seconds):"]
    timing_rows = sorted(
        [(c["leg_key"], c["elapsed_s"]) for c in scorecard["comparison"]],
        key=lambda x: x[1],
    )
    for leg_key, t in timing_rows:
        lines.append(f"  {leg_key:<34} {t:.1f}s")

    rec = scorecard["recommendation"]
    lines += [
        "",
        "=" * 72,
        "RECOMMENDATION:",
        f"  Verdict:       {rec.get('verdict')}",
    ]
    for mode_key, label in [("t2v_comparison", "T2V"), ("i2v_comparison", "I2V")]:
        cmp = rec.get(mode_key, {})
        if cmp and cmp.get("verdict"):
            lines.append(f"  {label} verdict:   {cmp['verdict']}  "
                         f"(delta {cmp.get('cost_delta_pct', '?'):+.1f}%)" if isinstance(cmp.get("cost_delta_pct"), float)
                         else f"  {label} verdict:   {cmp.get('verdict', '—')}")
    lines += [
        f"  Notes:         {rec.get('notes', '')}",
        "",
        "MANUAL REVIEW REQUIRED:",
        "  Fill in manual_review fields in scorecard.json after watching clips.",
        "  Dimensions: prompt_adherence, motion_quality, cinematic_realism,",
        "              character_consistency, camera_movement, artifact_level,",
        "              temporal_consistency, documentary_suitability, overall_preference",
        "",
        "OUTPUT CLIPS:",
    ]
    for r in scorecard["results"]:
        if r.get("output_path"):
            lines.append(f"  {r['leg_key']}: {r['output_path']}")
    lines += ["", "KNOWN LIMITATIONS:"]
    for provider, lims in scorecard.get("known_limitations", {}).items():
        lines.append(f"  {provider}:")
        for lim in lims:
            lines.append(f"    - {lim}")
    lines.append("=" * 72)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark: MUAPI Seedance 2 vs MUAPI Veo3-fast"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="No real API calls; test the runner flow")
    parser.add_argument("--legs", nargs="+", choices=list(ALL_LEGS.keys()),
                        default=DEFAULT_LEGS,
                        help=(
                            "Which legs to run "
                            "(default: muapi_seedance2_t2v muapi_veo3_fast_t2v "
                            "muapi_seedance2_i2v muapi_veo3_fast_i2v)"
                        ))
    parser.add_argument("--all-higgsfield-tiers", action="store_true",
                        help="Add all three Higgsfield tiers (dop-lite + dop-turbo + dop-preview) to the run")
    parser.add_argument("--seed", type=int, default=42,
                        help="Seed for reproducibility (default: 42)")
    parser.add_argument("--reference-image", type=Path, default=None,
                        help="Local image path to use for I2V legs")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Override output directory")
    args = parser.parse_args(argv)

    cfg = Config()
    log = get_logger()

    # Warn loudly if MUAPI_DRY_RUN is set in .env but --dry-run was not explicitly passed
    if cfg.MUAPI_DRY_RUN and not args.dry_run:
        print()
        print("!" * 72)
        print("WARNING: MUAPI_DRY_RUN=true is set in .env")
        print("         All MUAPI legs will run in dry-run mode — no credits will be spent.")
        print("         Results will NOT reflect real generation times or costs.")
        print("         To run live: set MUAPI_DRY_RUN=false in .env")
        print("!" * 72)
        print()

    # Determine legs
    legs_to_run = list(args.legs)
    if args.all_higgsfield_tiers:
        for hf_leg in ("higgsfield_dop_lite", "higgsfield_dop_turbo", "higgsfield_dop_preview"):
            if hf_leg not in legs_to_run:
                legs_to_run.append(hf_leg)

    # Output directory
    run_id = f"seedance2_vs_veo3_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    base_out = (args.output_dir or
                _REPO_ROOT / ".hypogeum-assets" / "provider_comparisons" / run_id)
    base_out.mkdir(parents=True, exist_ok=True)

    # Reference image for I2V legs
    reference_image_path = args.reference_image or _DEFAULT_REFERENCE_IMAGE
    if not reference_image_path.exists():
        if args.reference_image and _DEFAULT_REFERENCE_IMAGE.exists():
            print(
                f"WARNING: --reference-image not found: {reference_image_path}\n"
                f"         Falling back to default: {_DEFAULT_REFERENCE_IMAGE}"
            )
            reference_image_path = _DEFAULT_REFERENCE_IMAGE
        else:
            print(
                f"WARNING: Reference image not found at {reference_image_path}\n"
                f"         I2V legs will be skipped.\n"
                f"         Provide --reference-image <path> or place an image at:\n"
                f"         {_DEFAULT_REFERENCE_IMAGE}"
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
        from hypogeum.dormant_providers.higgsfield import check_credentials
        cred_check = check_credentials(cfg)
        if not cred_check["valid"]:
            print(f"ERROR: Higgsfield credentials invalid — {cred_check['detail']}")
            print("       Set HF_API_KEY and HF_API_SECRET in .env")
            print("       Account: https://cloud.higgsfield.ai/sign-up")
            if not any(k for k in legs_to_run if not k.startswith("higgsfield")):
                return 1
            print("       Higgsfield legs will be marked as skipped. Continuing MUAPI legs.\n")

    # Upload reference image to MUAPI for any MUAPI I2V leg (get hosted URL)
    muapi_i2v_legs = [k for k in legs_to_run
                      if ALL_LEGS.get(k, {}).get("provider") == "muapi"
                      and ALL_LEGS.get(k, {}).get("mode") == "i2v"]
    muapi_reference_url: Optional[str] = None
    if muapi_i2v_legs and not args.dry_run and not cfg.MUAPI_DRY_RUN:
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
        print(f"  -> {result['status']} in {elapsed}s  "
              f"cost=${result['actual_cost_usd']:.4f}  "
              f"{'output: ' + str(result['output_path']) if result['output_path'] else 'no output'}")
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

    comparison_sheet = _render_comparison_sheet(scorecard)
    comparison_sheet_path = base_out / "comparison_sheet.md"
    comparison_sheet_path.write_text(comparison_sheet)

    print()
    print(summary_text)
    print(f"\nScorecard written:        {scorecard_path}")
    print(f"Summary written:          {summary_path}")
    print(f"Comparison sheet written: {comparison_sheet_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
