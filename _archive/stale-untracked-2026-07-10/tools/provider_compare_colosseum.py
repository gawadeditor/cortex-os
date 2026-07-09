#!/usr/bin/env python3
"""
tools/provider_compare_colosseum.py

Provider comparison: Higgsfield Cloud REST I2V vs MUAPI T2V
for the 4 missing Colosseum scenes.

Labelled correctly:
  s02  Higgsfield Cloud REST  I2V  (DoP model, reference still from sc_s04 frame 0)
  s05  Higgsfield Cloud REST  I2V  (DoP model, reference still from thumbnails/main.jpg)
  s07  MUAPI                  T2V  (veo3-fast-text-to-video)
  s10  MUAPI                  T2V  (veo3-fast-text-to-video)

Output: .hypogeum-assets/provider_compare/colosseum_missing_scenes/
Results log: .hypogeum-assets/provider_compare/colosseum_missing_scenes/compare_results.json

Behaviour:
  - Makes no paid calls by default (--dry-run)
  - Pass --execute to run real generation
  - Skips scenes whose output file already exists
  - Does NOT touch the main episode render or production clips in video/

Usage:
  python3 tools/provider_compare_colosseum.py --dry-run     # validate + plan, no API calls
  python3 tools/provider_compare_colosseum.py --execute     # run real generation
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.config import Config  # noqa: E402
from hypogeum.logger import get_logger  # noqa: E402
import hypogeum.higgsfield as hf  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_EP_DIR     = _ROOT / ".hypogeum-assets" / "episodes" / "ep_reg_vi_colosseum"
_VP_FILE    = _EP_DIR / "visual_package_ep_reg_vi_colosseum.json"
_VIDEO_DIR  = _EP_DIR / "video"
_THUMB_DIR  = _EP_DIR / "thumbnails"
_OUT_DIR    = _ROOT / ".hypogeum-assets" / "provider_compare" / "colosseum_missing_scenes"
_RESULTS    = _OUT_DIR / "compare_results.json"

# Reference stills — extracted once, stored in output dir
_REF_S02    = _OUT_DIR / "ref_s02_from_sc_s04_frame0.jpg"   # underground corridor
_REF_S05    = _OUT_DIR / "ref_s05_from_thumbnail_main.jpg"  # Colosseum exterior

# Source for reference images
_REF_S02_SRC_CLIP = _VIDEO_DIR / "sc_s04.mp4"
_REF_S05_SRC_IMG  = _THUMB_DIR / "main.jpg"

# MUAPI constants (bypassing MUAPI_DRY_RUN for comparison calls only)
_MUAPI_BASE = "https://api.muapi.ai/api/v1"
_MUAPI_T2V_MODEL = "veo3-fast-text-to-video"
_MUAPI_COST_USD = 0.60
_MUAPI_POLL_INTERVAL = 5
_MUAPI_MAX_POLL_ATTEMPTS = 36  # 3 min

# ---------------------------------------------------------------------------
# Scene plan
# ---------------------------------------------------------------------------

_SCENES: list[dict[str, Any]] = [
    {
        "scene_id": "s02",
        "provider": "higgsfield",
        "mode": "I2V",
        "model": "dop-standard",
        "reference_still": None,         # resolved at runtime
        "reference_source": "sc_s04.mp4 frame 0",
        "output_file": _OUT_DIR / "sc_s02.mp4",
        "estimated_cost_usd": hf.PRICING["dop-standard"],
    },
    {
        "scene_id": "s05",
        "provider": "higgsfield",
        "mode": "I2V",
        "model": "dop-standard",
        "reference_still": None,
        "reference_source": "thumbnails/main.jpg",
        "output_file": _OUT_DIR / "sc_s05.mp4",
        "estimated_cost_usd": hf.PRICING["dop-standard"],
    },
    {
        "scene_id": "s07",
        "provider": "muapi",
        "mode": "T2V",
        "model": _MUAPI_T2V_MODEL,
        "reference_still": None,
        "reference_source": None,
        "output_file": _OUT_DIR / "sc_s07.mp4",
        "estimated_cost_usd": _MUAPI_COST_USD,
    },
    {
        "scene_id": "s10",
        "provider": "muapi",
        "mode": "T2V",
        "model": _MUAPI_T2V_MODEL,
        "reference_still": None,
        "reference_source": None,
        "output_file": _OUT_DIR / "sc_s10.mp4",
        "estimated_cost_usd": _MUAPI_COST_USD,
    },
]


# ---------------------------------------------------------------------------
# Prompt loader
# ---------------------------------------------------------------------------

def _load_prompts() -> dict[str, str]:
    with open(_VP_FILE) as f:
        vp = json.load(f)
    prompts: dict[str, str] = {}
    for scene in vp.get("scenes", []):
        sid = scene.get("scene_id")
        if sid in ("s02", "s05", "s07", "s10"):
            prompt = (
                scene.get("production_spec", {})
                     .get("provider_routing", {})
                     .get("primary_prompt", "")
            )
            prompts[sid] = prompt
    return prompts


# ---------------------------------------------------------------------------
# Reference still preparation
# ---------------------------------------------------------------------------

def _prepare_reference_stills() -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)

    # s02: extract frame 0 from sc_s04.mp4
    if not _REF_S02.exists():
        if not _REF_S02_SRC_CLIP.exists():
            raise FileNotFoundError(f"Reference source clip not found: {_REF_S02_SRC_CLIP}")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(_REF_S02_SRC_CLIP),
             "-vframes", "1", "-q:v", "2", str(_REF_S02)],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg frame extract failed: {result.stderr.decode()[:300]}")
        print(f"  ref_s02: extracted frame 0 from {_REF_S02_SRC_CLIP.name} → {_REF_S02.name}")
    else:
        print(f"  ref_s02: {_REF_S02.name} already exists")

    # s05: copy thumbnail
    if not _REF_S05.exists():
        if not _REF_S05_SRC_IMG.exists():
            raise FileNotFoundError(f"Reference source image not found: {_REF_S05_SRC_IMG}")
        _REF_S05.write_bytes(_REF_S05_SRC_IMG.read_bytes())
        print(f"  ref_s05: copied {_REF_S05_SRC_IMG.name} → {_REF_S05.name}")
    else:
        print(f"  ref_s05: {_REF_S05.name} already exists")


# ---------------------------------------------------------------------------
# Higgsfield I2V generation
# ---------------------------------------------------------------------------

def _run_higgsfield(
    cfg: Config,
    scene: dict[str, Any],
    prompt: str,
    ref_image_path: Path,
    dry_run: bool,
) -> dict[str, Any]:
    log = get_logger()
    sid = scene["scene_id"]
    model = scene["model"]
    out_path: Path = scene["output_file"]

    record: dict[str, Any] = {
        "scene_id": sid,
        "provider": "higgsfield",
        "mode": "I2V",
        "model": model,
        "reference_image_local": str(ref_image_path),
        "reference_source": scene["reference_source"],
        "reference_cdn_url": None,
        "prompt_length_chars": len(prompt),
        "resolution": "1280x720",   # DoP default
        "duration_seconds": 5,
        "submit_time": None,
        "complete_time": None,
        "latency_seconds": None,
        "output_path": str(out_path),
        "file_size_bytes": None,
        "cost_usd": hf.PRICING[model],
        "dry_run": dry_run,
        "request_id": None,
        "status": None,
        "error": None,
    }

    if out_path.exists():
        record["status"] = "skipped_already_exists"
        record["file_size_bytes"] = out_path.stat().st_size
        print(f"  [{sid}] SKIP — output already exists: {out_path.name}")
        return record

    if dry_run:
        record["status"] = "dry_run"
        print(f"  [{sid}] DRY RUN — would upload {ref_image_path.name} then submit {model}")
        return record

    try:
        # 1. Upload reference image → CDN URL
        print(f"  [{sid}] uploading reference image {ref_image_path.name}…")
        cdn_url = hf.upload_reference_image(cfg, ref_image_path)
        record["reference_cdn_url"] = cdn_url
        print(f"  [{sid}] CDN URL: {cdn_url[:80]}…")

        # 2. Submit generation
        print(f"  [{sid}] submitting {model} I2V…")
        record["submit_time"] = time.time()
        result = hf.submit_dop_generation(
            cfg,
            model=model,
            prompt=prompt,
            image_url=cdn_url,
            duration=5,
            dry_run=False,
        )
        record["request_id"] = result.get("request_id")
        record["complete_time"] = result.get("complete_time")
        record["latency_seconds"] = result.get("elapsed_seconds")
        record["status"] = result.get("status")
        record["cost_usd"] = result.get("cost_usd", hf.PRICING[model])

        if result.get("status") == "Completed" and result.get("output_url"):
            # 3. Download
            print(f"  [{sid}] downloading…")
            hf.download_clip(result["output_url"], out_path)
            record["file_size_bytes"] = out_path.stat().st_size
            print(f"  [{sid}] done — {record['file_size_bytes'] // 1024} KB  latency={result.get('elapsed_seconds')}s")
        elif result.get("status") == "timeout":
            # Server may have completed after client timeout — log request_id for recovery.
            record["error"] = (
                f"client timeout at {cfg.HIGGSFIELD_TIMEOUT_SECONDS}s — "
                f"check server: GET /requests/{result.get('request_id')}/status"
            )
            print(f"  [{sid}] CLIENT TIMEOUT — server may still be running; "
                  f"request_id={result.get('request_id')}")
        else:
            record["error"] = f"generation ended with status={result.get('status')}"
            print(f"  [{sid}] FAILED — {record['error']}")

    except hf.HiggsFieldAuthError as e:
        record["status"] = "auth_error"
        record["error"] = str(e)
        print(f"  [{sid}] AUTH ERROR: {e}", file=sys.stderr)
        raise  # fatal — stop all generation
    except hf.HiggsFieldQuotaError as e:
        record["status"] = "quota_error"
        record["error"] = str(e)
        print(f"  [{sid}] QUOTA ERROR: {e}", file=sys.stderr)
        raise  # fatal
    except Exception as e:
        record["status"] = "error"
        record["error"] = str(e)
        print(f"  [{sid}] ERROR: {e}", file=sys.stderr)

    return record


# ---------------------------------------------------------------------------
# MUAPI T2V generation (direct HTTP — bypasses MUAPI_DRY_RUN from .env)
# ---------------------------------------------------------------------------

def _muapi_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _run_muapi(
    cfg: Config,
    scene: dict[str, Any],
    prompt: str,
    dry_run: bool,
) -> dict[str, Any]:
    sid = scene["scene_id"]
    model = _MUAPI_T2V_MODEL
    out_path: Path = scene["output_file"]

    record: dict[str, Any] = {
        "scene_id": sid,
        "provider": "muapi",
        "mode": "T2V",
        "model": model,
        "reference_image_local": None,
        "reference_source": None,
        "reference_cdn_url": None,
        "prompt_length_chars": len(prompt),
        "resolution": "1920x1080",  # Veo3 native
        "duration_seconds": 8,      # Veo3 fixed
        "submit_time": None,
        "complete_time": None,
        "latency_seconds": None,
        "output_path": str(out_path),
        "file_size_bytes": None,
        "cost_usd": _MUAPI_COST_USD,
        "dry_run": dry_run,
        "request_id": None,
        "status": None,
        "error": None,
    }

    if out_path.exists():
        record["status"] = "skipped_already_exists"
        record["file_size_bytes"] = out_path.stat().st_size
        print(f"  [{sid}] SKIP ��� output already exists: {out_path.name}")
        return record

    if dry_run:
        record["status"] = "dry_run"
        print(f"  [{sid}] DRY RUN — would submit {model} T2V")
        return record

    api_key = cfg.MUAPI_API_KEY
    if not api_key:
        record["status"] = "error"
        record["error"] = "MUAPI_API_KEY not set"
        return record

    endpoint = f"{_MUAPI_BASE}/{model}"
    # veo3 models do NOT accept a duration param — fixed 8s output
    payload = {"prompt": prompt}

    print(f"  [{sid}] submitting {model} T2V…")
    record["submit_time"] = time.time()

    try:
        resp = requests.post(endpoint, headers=_muapi_headers(api_key),
                             json=payload, timeout=30)
        if resp.status_code == 401:
            record["status"] = "auth_error"
            record["error"] = f"MUAPI 401 — invalid API key"
            print(f"  [{sid}] MUAPI AUTH ERROR", file=sys.stderr)
            return record
        if resp.status_code not in (200, 201, 202):
            record["status"] = "submit_error"
            record["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            print(f"  [{sid}] MUAPI submit error: {record['error']}", file=sys.stderr)
            return record

        resp_data = resp.json()
        request_id: str = (
            resp_data.get("id") or
            resp_data.get("request_id") or
            resp_data.get("prediction_id") or ""
        )
        if not request_id:
            record["status"] = "error"
            record["error"] = f"No request_id in response: {resp.text[:200]}"
            return record

        record["request_id"] = request_id
        print(f"  [{sid}] request_id={request_id}, polling…")

        # Poll
        poll_url = f"{_MUAPI_BASE}/predictions/{request_id}/result"
        for attempt in range(_MUAPI_MAX_POLL_ATTEMPTS):
            time.sleep(_MUAPI_POLL_INTERVAL)
            poll_resp = requests.get(poll_url, headers=_muapi_headers(api_key), timeout=30)
            if poll_resp.status_code not in (200, 202):
                continue
            data = poll_resp.json()
            status: str = data.get("status", "")
            print(f"  [{sid}] poll {attempt+1}: {status}")

            if status == "completed":
                record["complete_time"] = time.time()
                record["latency_seconds"] = round(record["complete_time"] - record["submit_time"], 1)
                record["status"] = "Completed"

                # Extract video URL.
                # MUAPI veo3 response: {"outputs": ["https://cdn.muapi.ai/...mp4"], ...}
                # Fallback shapes: {"output": ...}, {"video": {"url": ...}}, {"video": "url"}
                video_url: Optional[str] = None
                outputs_list = data.get("outputs")
                if isinstance(outputs_list, list) and outputs_list:
                    video_url = outputs_list[0] if isinstance(outputs_list[0], str) else None
                if not video_url:
                    output = data.get("output") or data.get("video") or {}
                    if isinstance(output, dict):
                        video_url = output.get("url") or output.get("video_url")
                    elif isinstance(output, str):
                        video_url = output
                    elif isinstance(output, list) and output:
                        video_url = output[0] if isinstance(output[0], str) else output[0].get("url")

                if not video_url:
                    record["status"] = "error"
                    record["error"] = f"No video URL in completed response: {str(data)[:200]}"
                    print(f"  [{sid}] no video URL in response", file=sys.stderr)
                    return record

                # Download
                print(f"  [{sid}] downloading from MUAPI CDN…")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                dl = requests.get(video_url, stream=True, timeout=120)
                dl.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in dl.iter_content(chunk_size=8 * 1024 * 1024):
                        f.write(chunk)
                record["file_size_bytes"] = out_path.stat().st_size
                print(f"  [{sid}] done — {record['file_size_bytes'] // 1024} KB  latency={record['latency_seconds']}s")
                return record

            if status == "failed":
                record["complete_time"] = time.time()
                record["latency_seconds"] = round(record["complete_time"] - record["submit_time"], 1)
                record["status"] = "Failed"
                record["error"] = data.get("error", "unknown failure")
                print(f"  [{sid}] MUAPI generation failed: {record['error']}", file=sys.stderr)
                return record

            if status not in ("queued", "pending", "processing", ""):
                record["status"] = "error"
                record["error"] = f"Unexpected poll status: {status}"
                return record

        # Timed out
        record["status"] = "timeout"
        record["error"] = f"Poll timed out after {_MUAPI_MAX_POLL_ATTEMPTS * _MUAPI_POLL_INTERVAL}s"
        print(f"  [{sid}] MUAPI poll timeout", file=sys.stderr)

    except requests.RequestException as e:
        record["status"] = "network_error"
        record["error"] = str(e)
        print(f"  [{sid}] network error: {e}", file=sys.stderr)

    return record


# ---------------------------------------------------------------------------
# Results I/O
# ---------------------------------------------------------------------------

def _load_results() -> list[dict[str, Any]]:
    if _RESULTS.exists():
        with open(_RESULTS) as f:
            return json.load(f)
    return []


def _save_results(results: list[dict[str, Any]]) -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(_RESULTS, "w") as f:
        json.dump(results, f, indent=2, default=str)


def _print_summary(results: list[dict[str, Any]]) -> None:
    bar = "=" * 70
    print(f"\n{bar}")
    print("PROVIDER COMPARISON — Higgsfield Cloud REST I2V vs MUAPI T2V")
    print(f"Episode: ep_reg_vi_colosseum  |  Missing scenes: s02 s05 s07 s10")
    print(bar)
    for r in results:
        sid = r["scene_id"]
        prov = r["provider"].upper()
        mode = r["mode"]
        model = r["model"]
        status = r.get("status", "?")
        latency = r.get("latency_seconds")
        size = r.get("file_size_bytes")
        cost = r.get("cost_usd")
        ref = r.get("reference_source") or "none"
        lat_str = f"{latency}s" if latency else "—"
        size_str = f"{size // (1024*1024)}MB" if size else "—"
        cost_str = f"${cost:.2f}" if cost is not None else "—"
        print(f"\n  {sid}  [{prov} {mode}]  {model}")
        print(f"     status={status}  latency={lat_str}  size={size_str}  cost={cost_str}")
        if r.get("reference_source"):
            print(f"     reference: {ref}")
        if r.get("error"):
            print(f"     error: {r['error']}")
    print(f"\nResults: {_RESULTS}")
    total = sum(r.get("cost_usd") or 0 for r in results if r.get("status") not in ("dry_run", "skipped_already_exists"))
    print(f"Total cost (completed/failed): ${total:.2f}")
    print(bar)


# ---------------------------------------------------------------------------
# Review mode
# ---------------------------------------------------------------------------

def _probe_clip(path: Path) -> dict[str, Any]:
    """Run ffprobe on a clip and return {width, height, duration, fps, codec, audio}."""
    info: dict[str, Any] = {"exists": path.exists(), "path": str(path)}
    if not path.exists():
        return info

    info["size_bytes"] = path.stat().st_size

    try:
        # Full stream + format probe in one call
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            info["probe_error"] = r.stderr[:200]
            return info

        data = json.loads(r.stdout)
        fmt = data.get("format", {})
        info["duration"] = float(fmt.get("duration", 0))
        info["format_name"] = fmt.get("format_name", "")

        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video" and "width" not in info:
                info["width"] = stream.get("width")
                info["height"] = stream.get("height")
                info["codec"] = stream.get("codec_name")
                r_frame = stream.get("r_frame_rate", "0/1")
                try:
                    num, den = r_frame.split("/")
                    info["fps"] = round(int(num) / int(den), 2)
                except Exception:
                    info["fps"] = r_frame
            if stream.get("codec_type") == "audio" and "audio_codec" not in info:
                info["audio_codec"] = stream.get("codec_name")
                info["audio_sample_rate"] = stream.get("sample_rate")

    except Exception as e:
        info["probe_error"] = str(e)

    return info


def _review() -> int:
    bar = "=" * 70
    print(f"\n{bar}")
    print("PROVIDER COMPARISON REVIEW")
    print("Higgsfield Cloud REST I2V  vs  MUAPI T2V")
    print(f"Episode: ep_reg_vi_colosseum  |  Scenes: s02 s05 s07 s10")
    print(bar)

    if not _RESULTS.exists():
        print(f"\nNo results log found at {_RESULTS}")
        print("Run --execute first.")
        return 1

    with open(_RESULTS) as f:
        results = json.load(f)

    # Load prompts for display
    try:
        prompts = _load_prompts()
    except Exception:
        prompts = {}

    all_ok = True
    for r in sorted(results, key=lambda x: x["scene_id"]):
        sid = r["scene_id"]
        prov = r["provider"].upper()
        mode = r["mode"]
        model = r["model"]
        status = r.get("status", "?")
        out_path = Path(r.get("output_path", ""))

        print(f"\n{'─' * 70}")
        print(f"  {sid}  [{prov} {mode}]  {model}")
        print(f"  Status:    {status}")
        print(f"  Cost:      ${r.get('cost_usd', 0):.2f}")
        print(f"  Latency:   {r.get('latency_seconds', '—')}s")
        if r.get("reference_source"):
            print(f"  Reference: {r['reference_source']}")
        if r.get("request_id"):
            print(f"  RequestID: {r['request_id']}")

        # File probe
        probe = _probe_clip(out_path)
        if probe.get("exists"):
            size_mb = probe["size_bytes"] / (1024 * 1024)
            dur = probe.get("duration", 0)
            w, h = probe.get("width", "?"), probe.get("height", "?")
            fps = probe.get("fps", "?")
            codec = probe.get("codec", "?")
            audio = probe.get("audio_codec", "none")
            print(f"  File:      {out_path.name}  ({size_mb:.1f} MB)")
            print(f"  Video:     {w}×{h}  {dur:.2f}s  {fps}fps  codec={codec}")
            print(f"  Audio:     {audio}")
            if dur < 1.0:
                print(f"  ⚠  Duration suspiciously short ({dur:.2f}s)")
                all_ok = False
        else:
            print(f"  File:      MISSING — {out_path}")
            if r.get("error"):
                print(f"  Error:     {r['error']}")
            all_ok = False

        # Prompt preview
        prompt = prompts.get(sid, "")
        if prompt:
            print(f"  Prompt:    ({len(prompt)} chars) {prompt[:120]}…")

        if r.get("note"):
            print(f"  Note:      {r['note']}")

    print(f"\n{'─' * 70}")
    # Cross-provider comparison table
    print("\nCOMPARISON TABLE")
    print(f"  {'Scene':<6}  {'Provider':<22}  {'Mode':<4}  {'Dur':>6}  {'Res':>10}  {'Latency':>9}  {'Size':>7}  {'Cost':>6}")
    print(f"  {'─'*6}  {'─'*22}  {'─'*4}  {'─'*6}  {'─'*10}  {'─'*9}  {'─'*7}  {'─'*6}")
    total_cost = 0.0
    for r in sorted(results, key=lambda x: x["scene_id"]):
        sid = r["scene_id"]
        prov_label = f"{r['provider']} {r['mode']}"
        model_short = r["model"].replace("veo3-fast-text-to-video", "veo3-fast").replace("dop-standard", "dop-std")
        out_path = Path(r.get("output_path", ""))
        probe = _probe_clip(out_path)
        dur_str = f"{probe.get('duration', 0):.1f}s" if probe.get("exists") else "—"
        res_str = f"{probe.get('width','?')}×{probe.get('height','?')}" if probe.get("exists") else "—"
        lat = r.get("latency_seconds")
        lat_str = f"{lat}s" if lat else "—"
        size = probe.get("size_bytes", 0)
        size_str = f"{size / (1024*1024):.1f}MB" if size else "—"
        cost = r.get("cost_usd", 0) or 0
        total_cost += cost
        cost_str = f"${cost:.2f}"
        status_tag = "" if r.get("status") == "Completed" else f" [{r.get('status')}]"
        print(f"  {sid:<6}  {prov_label:<22}  {r['mode']:<4}  {dur_str:>6}  {res_str:>10}  {lat_str:>9}  {size_str:>7}  {cost_str:>6}{status_tag}")
    print(f"  {'─'*6}  {'─'*22}  {'─'*4}  {'─'*6}  {'─'*10}  {'─'*9}  {'─'*7}  {'─'*6}")
    print(f"  {'TOTAL':<6}  {'':22}  {'':4}  {'':6}  {'':10}  {'':9}  {'':7}  ${total_cost:.2f}")

    print(f"\nOutput dir: {_OUT_DIR}")
    print(f"Results:    {_RESULTS}")
    print(bar)

    return 0 if all_ok else 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Higgsfield I2V vs MUAPI T2V — missing Colosseum scene comparison"
    )
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Validate plan without making API calls (default)")
    parser.add_argument("--execute", action="store_true",
                        help="Run real generation (overrides --dry-run)")
    parser.add_argument("--review", action="store_true",
                        help="Review completed results and probe output files")
    args = parser.parse_args()

    if args.review:
        return _review()

    dry_run = not args.execute

    cfg = Config()

    print(f"\n{'DRY RUN — no API calls' if dry_run else 'EXECUTE — real generation'}")
    print(f"Output: {_OUT_DIR}\n")

    # Validate credentials
    print("Checking credentials…")
    hf_check = hf.check_credentials(cfg)
    if not hf_check["valid"]:
        print(f"  Higgsfield: INVALID — {hf_check['detail']}", file=sys.stderr)
        if not dry_run:
            return 2
    else:
        print(f"  Higgsfield: OK — {hf_check['detail']}")

    if not cfg.MUAPI_API_KEY:
        print("  MUAPI: MUAPI_API_KEY not set", file=sys.stderr)
        if not dry_run:
            return 2
    else:
        print(f"  MUAPI: key present ({cfg.MUAPI_API_KEY[:8]}…)")

    # Load prompts
    print("\nLoading prompts from visual package…")
    prompts = _load_prompts()
    for sid in ("s02", "s05", "s07", "s10"):
        p = prompts.get(sid, "")
        print(f"  {sid}: {len(p)} chars — {p[:80]}…")

    # Prepare reference stills
    print("\nPreparing reference stills…")
    try:
        _prepare_reference_stills()
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        if not dry_run:
            return 2

    # Assign reference paths to scene plan
    _SCENES[0]["reference_still"] = _REF_S02   # s02
    _SCENES[1]["reference_still"] = _REF_S05   # s05

    # Confirm total estimated cost
    total_est = sum(s["estimated_cost_usd"] for s in _SCENES)
    print(f"\nEstimated cost: ${total_est:.2f}  ({len(_SCENES)} generations)")
    if not dry_run:
        print("Starting generation in 3s… (Ctrl+C to abort)")
        time.sleep(3)

    # Run generations
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_results = _load_results()
    done_ids = {r["scene_id"] for r in all_results if r.get("status") == "Completed"}

    run_results: list[dict[str, Any]] = []
    error_count = 0

    for scene in _SCENES:
        sid = scene["scene_id"]
        if sid in done_ids:
            print(f"\n[{sid}] already Completed in results log — skipping")
            continue

        prompt = prompts.get(sid, "")
        if not prompt:
            print(f"\n[{sid}] ERROR: no prompt found", file=sys.stderr)
            run_results.append({"scene_id": sid, "status": "error", "error": "no prompt"})
            continue

        print(f"\n[{sid}] ———————————————————————————————————————")
        try:
            if scene["provider"] == "higgsfield":
                rec = _run_higgsfield(cfg, scene, prompt,
                                      scene["reference_still"], dry_run)
            else:
                rec = _run_muapi(cfg, scene, prompt, dry_run)
        except (hf.HiggsFieldAuthError, hf.HiggsFieldQuotaError) as e:
            print(f"FATAL: {e}", file=sys.stderr)
            run_results.append({"scene_id": sid, "status": "fatal_error", "error": str(e)})
            error_count += 1
            break  # stop all generation on fatal errors

        run_results.append(rec)
        if rec.get("status") not in ("Completed", "dry_run", "skipped_already_exists"):
            error_count += 1

    # Merge with any prior results and save
    prior = {r["scene_id"]: r for r in all_results}
    for r in run_results:
        prior[r["scene_id"]] = r
    merged = list(prior.values())
    _save_results(merged)

    _print_summary(merged)

    return 1 if error_count else 0


if __name__ == "__main__":
    sys.exit(main())
