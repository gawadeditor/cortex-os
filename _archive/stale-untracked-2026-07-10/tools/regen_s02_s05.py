#!/usr/bin/env python3
"""
tools/regen_s02_s05.py

QA-fix regen: submit S02 (locked-off motion prompt v2) and S05 (clean T2I reference)
to Higgsfield DoP standard, poll to completion, download, update compare_results.json.

CDN URLs for references already uploaded — no re-upload needed.
Uses _regen_state.json for s02_cdn_url, s05_cdn_url, s02_prompt_v2.
S05 prompt loaded from visual_package JSON (same source as compare script).

Usage:
  python3 tools/regen_s02_s05.py [--dry-run]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.config import Config  # noqa: E402
import hypogeum.higgsfield as hf  # noqa: E402

_OUT_DIR   = _ROOT / ".hypogeum-assets" / "provider_compare" / "colosseum_missing_scenes"
_RESULTS   = _OUT_DIR / "compare_results.json"
_STATE     = _OUT_DIR / "_regen_state.json"
_VP_FILE   = _ROOT / ".hypogeum-assets" / "episodes" / "ep_reg_vi_colosseum" / "visual_package_ep_reg_vi_colosseum.json"

TIMEOUT = 300   # s05 took 185s last time — give plenty of headroom


def _load_s05_prompt() -> str:
    with open(_VP_FILE) as f:
        vp = json.load(f)
    for scene in vp.get("scenes", []):
        if scene.get("scene_id") == "s05":
            return (
                scene.get("production_spec", {})
                     .get("provider_routing", {})
                     .get("primary_prompt", "")
            )
    raise RuntimeError("s05 prompt not found in visual_package JSON")


def _submit_and_poll(cfg: Config, scene_id: str, prompt: str, cdn_url: str) -> dict:
    """Submit DoP standard I2V, poll until complete, return result dict."""
    print(f"[{scene_id}] submitting dop-standard I2V (timeout={TIMEOUT}s)…")
    t0 = time.time()

    # Submit
    headers = {
        **hf._auth_header(cfg),
        "Content-Type": "application/json",
    }
    payload = {"prompt": prompt, "image_url": cdn_url, "duration": 5}
    r = requests.post(
        f"{hf._BASE_URL}/higgsfield-ai/dop/standard",
        json=payload,
        headers=headers,
        timeout=30,
    )
    if r.status_code == 401:
        raise RuntimeError(f"[{scene_id}] AUTH REJECTED (401)")
    if r.status_code == 402:
        raise RuntimeError(f"[{scene_id}] QUOTA EXHAUSTED (402)")
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"[{scene_id}] submit failed HTTP {r.status_code}: {r.text[:300]}")

    resp = r.json()
    request_id = resp.get("request_id") or resp.get("id") or ""
    if not request_id:
        raise RuntimeError(f"[{scene_id}] no request_id in submit response: {resp}")

    print(f"[{scene_id}] queued — request_id={request_id}")

    # Poll
    poll_url = f"{hf._BASE_URL}/requests/{request_id}/status"
    attempt = 0
    while True:
        elapsed = time.time() - t0
        if elapsed > TIMEOUT:
            print(f"[{scene_id}] CLIENT TIMEOUT after {elapsed:.0f}s — request_id={request_id}")
            return {
                "status": "timeout",
                "request_id": request_id,
                "elapsed_seconds": elapsed,
                "output_url": None,
            }
        time.sleep(hf._DEFAULT_POLL_INTERVAL)
        attempt += 1
        pr = requests.get(poll_url, headers=hf._auth_header(cfg), timeout=15)
        if pr.status_code != 200:
            print(f"[{scene_id}] poll HTTP {pr.status_code} — retrying")
            continue
        data = pr.json()
        status = (data.get("status") or "").lower()
        print(f"[{scene_id}] poll #{attempt} ({elapsed:.0f}s) → {status}")

        if status == "completed":
            video = data.get("video") or {}
            output_url = video.get("url") if isinstance(video, dict) else None
            if not output_url:
                # fallback: check top-level url
                output_url = data.get("url") or data.get("output_url")
            return {
                "status": "Completed",
                "request_id": request_id,
                "elapsed_seconds": round(time.time() - t0, 1),
                "complete_time": time.time(),
                "output_url": output_url,
            }
        if status in ("failed", "nsfw", "cancelled"):
            return {
                "status": status,
                "request_id": request_id,
                "elapsed_seconds": round(time.time() - t0, 1),
                "output_url": None,
            }
        # queued / in_progress — keep polling


def _download(url: str, out_path: Path) -> None:
    print(f"  downloading → {out_path.name}…")
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=hf._CHUNK_SIZE):
            if chunk:
                f.write(chunk)
    print(f"  saved: {out_path.name} ({out_path.stat().st_size // 1024} KB)")


def _update_results(scene_id: str, updates: dict) -> None:
    results = json.loads(_RESULTS.read_text()) if _RESULTS.exists() else []
    for i, rec in enumerate(results):
        if rec.get("scene_id") == scene_id:
            results[i] = {**rec, **updates}
            break
    else:
        results.append({"scene_id": scene_id, **updates})
    _RESULTS.write_text(json.dumps(results, indent=2))
    print(f"  compare_results.json updated for {scene_id}")


def main(dry_run: bool = False) -> None:
    cfg = Config()
    state = json.loads(_STATE.read_text())

    s02_cdn_url   = state["s02_cdn_url"]
    s05_cdn_url   = state["s05_cdn_url"]
    s02_prompt_v2 = state["s02_prompt_v2"]
    s05_prompt    = _load_s05_prompt()

    s05_ref_clean = _ROOT / state["s05_ref_clean_path"]

    print(f"\n=== S02 (v2 — locked-off motion) ===")
    print(f"  CDN: {s02_cdn_url[:70]}…")
    print(f"  prompt_chars: {len(s02_prompt_v2)}")

    print(f"\n=== S05 (clean T2I reference) ===")
    print(f"  CDN: {s05_cdn_url[:70]}…")
    print(f"  prompt_chars: {len(s05_prompt)}")
    print(f"  ref_clean: {s05_ref_clean.name} (exists={s05_ref_clean.exists()})")

    if dry_run:
        print("\nDRY RUN — no API calls. Exiting.")
        return

    # --- S02 ---
    print("\n--- Generating S02 v2 ---")
    s02_out = _OUT_DIR / "sc_s02.mp4"
    s02_backup = _OUT_DIR / "sc_s02_v1_backup.mp4"

    # Backup v1 if present
    if s02_out.exists() and not s02_backup.exists():
        s02_out.rename(s02_backup)
        print(f"  backed up sc_s02.mp4 → sc_s02_v1_backup.mp4")

    s02_result = _submit_and_poll(cfg, "s02", s02_prompt_v2, s02_cdn_url)

    if s02_result["status"] == "Completed" and s02_result.get("output_url"):
        _download(s02_result["output_url"], s02_out)
        _update_results("s02", {
            "request_id": s02_result["request_id"],
            "reference_cdn_url": s02_cdn_url,
            "reference_source": "sc_s04.mp4 frame 0 (same as v1)",
            "reference_image_local": str(_OUT_DIR / "ref_s02_from_sc_s04_frame0.jpg"),
            "prompt_length_chars": len(s02_prompt_v2),
            "submit_time": time.time() - s02_result["elapsed_seconds"],
            "complete_time": s02_result.get("complete_time"),
            "latency_seconds": s02_result["elapsed_seconds"],
            "output_url": s02_result["output_url"],
            "file_size_bytes": s02_out.stat().st_size,
            "status": "Completed",
            "error": None,
            "regenerated": True,
            "regen_reason": "v1 camera motion too fast; v2 uses locked-off minimal-motion prompt",
            "output_path": str(s02_out),
        })
    elif s02_result["status"] == "timeout":
        print(f"[s02] TIMEOUT — server may still be running. request_id={s02_result['request_id']}")
        print(f"[s02] Recovery: GET {hf._BASE_URL}/requests/{s02_result['request_id']}/status")
        _update_results("s02", {
            "request_id": s02_result["request_id"],
            "status": "timeout",
            "error": f"client timeout — check server request_id={s02_result['request_id']}",
            "regenerated": True,
        })
    else:
        print(f"[s02] FAILED: {s02_result}")
        _update_results("s02", {
            "request_id": s02_result.get("request_id"),
            "status": s02_result["status"],
            "error": f"generation failed with status={s02_result['status']}",
            "regenerated": True,
        })

    # --- S05 ---
    print("\n--- Generating S05 v2 ---")
    s05_out = _OUT_DIR / "sc_s05.mp4"
    s05_backup = _OUT_DIR / "sc_s05_v1_backup.mp4"

    if s05_out.exists() and not s05_backup.exists():
        s05_out.rename(s05_backup)
        print(f"  backed up sc_s05.mp4 → sc_s05_v1_backup.mp4")

    s05_result = _submit_and_poll(cfg, "s05", s05_prompt, s05_cdn_url)

    if s05_result["status"] == "Completed" and s05_result.get("output_url"):
        _download(s05_result["output_url"], s05_out)
        _update_results("s05", {
            "request_id": s05_result["request_id"],
            "reference_cdn_url": s05_cdn_url,
            "reference_source": "ref_s05_t2i_clean.jpg (T2I generated, no branding)",
            "reference_image_local": str(s05_ref_clean),
            "prompt_length_chars": len(s05_prompt),
            "submit_time": time.time() - s05_result["elapsed_seconds"],
            "complete_time": s05_result.get("complete_time"),
            "latency_seconds": s05_result["elapsed_seconds"],
            "output_url": s05_result["output_url"],
            "file_size_bytes": s05_out.stat().st_size,
            "status": "Completed",
            "error": None,
            "regenerated": True,
            "regen_reason": "v1 used thumbnail with branding text; v2 uses clean T2I-generated reference",
            "output_path": str(s05_out),
        })
    elif s05_result["status"] == "timeout":
        print(f"[s05] TIMEOUT — server may still be running. request_id={s05_result['request_id']}")
        print(f"[s05] Recovery: GET {hf._BASE_URL}/requests/{s05_result['request_id']}/status")
        _update_results("s05", {
            "request_id": s05_result["request_id"],
            "status": "timeout",
            "error": f"client timeout — check server request_id={s05_result['request_id']}",
            "regenerated": True,
        })
    else:
        print(f"[s05] FAILED: {s05_result}")
        _update_results("s05", {
            "request_id": s05_result.get("request_id"),
            "status": s05_result["status"],
            "error": f"generation failed with status={s05_result['status']}",
            "regenerated": True,
        })

    # --- S10: mark audio stripped in results ---
    s10_silent = _OUT_DIR / "sc_s10_silent.mp4"
    if s10_silent.exists():
        _update_results("s10", {
            "audio_stripped": True,
            "silent_output_path": str(s10_silent),
            "silent_file_size_bytes": s10_silent.stat().st_size,
            "note": (
                "URL was in outputs[0] list; fixed extraction and downloaded manually. "
                "Audio stripped in QA pass: sc_s10_silent.mp4 (video copy, no re-encode)."
            ),
        })

    print("\nDone.")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    main(dry_run=args.dry_run)
