"""
scripts/repair_ep_colosseum.py

Repair runner for ep_20260629075717_c7abfd ("The Hidden City Beneath Rome's Colosseum Floor").

Generates:
  - 4 missing clips: s01, s04, s12, s16  → sc_s01.mp4 ... (original slot)
  - 6 danger repair clips: s13, s14, s15, s20, s24, s26 → sc_s13_repair.mp4 ...

Then:
  - Pre-concatenates each danger pair: sc_sXX.mp4 + sc_sXX_repair.mp4 → sc_sXX.mp4 (backed up first)
  - Reports QA results (do NOT upload)

Model: veo3-fast-text-to-video  ($0.60/clip, fixed 8s, no duration param)
Budget cap: 10 clips × $0.60 = $6.00 max.

MUAPI_DRY_RUN must be false before calling this script.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT   = Path(__file__).resolve().parent.parent
EP_ID       = "ep_20260629075717_c7abfd"
EP_DIR      = REPO_ROOT / ".hypogeum-assets" / "episodes" / EP_ID
VIDEO_DIR   = EP_DIR / "video"
VP_PATH     = EP_DIR / f"visual_package_{EP_ID}.json"

MODEL       = "veo3-fast-text-to-video"
ASPECT      = "16:9"
PRICE_USD   = 0.60
BUDGET_CAP  = 6.00
POLL_INTERVAL = 8   # seconds between poll ticks
POLL_TIMEOUT  = 360 # seconds max per clip

# Scenes that need new clips generated (missing from disk)
MISSING_SCENES = ["s01", "s04", "s12", "s16"]

# Danger scenes that already have a clip but need a second clip to reduce hold
DANGER_SCENES = ["s13", "s14", "s15", "s20", "s24", "s26"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_env() -> dict:
    env_path = REPO_ROOT / ".env"
    result = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _guard_dry_run(env: dict) -> None:
    val = env.get("MUAPI_DRY_RUN", "true").lower()
    if val in ("1", "true", "yes"):
        print("[ABORT] MUAPI_DRY_RUN is still true. Set it to false in .env before running this script.")
        sys.exit(1)


def _get_api_key(env: dict) -> str:
    key = env.get("MUAPI_API_KEY", "")
    if not key:
        print("[ABORT] MUAPI_API_KEY not set in .env")
        sys.exit(1)
    return key


def _load_prompts(vp: dict, scene_ids: list[str]) -> dict[str, str]:
    """Extract fallback prompts for each target scene."""
    prompts = {}
    for scene in vp.get("scenes", []):
        sid = scene.get("scene_id")
        if sid not in scene_ids:
            continue
        routing = scene.get("production_spec", {}).get("provider_routing", {})
        prompt = routing.get("fallback_prompt") or routing.get("primary_prompt", "")
        if not prompt:
            print(f"[WARN] No prompt found for {sid}")
        prompts[sid] = prompt
    return prompts


def _submit(api_key: str, prompt: str, base_url: str) -> str | None:
    """Submit a T2V job to MUAPI. Returns request_id or None on failure."""
    import urllib.request, urllib.error
    url = f"{base_url}/{MODEL}"
    payload = json.dumps({"prompt": prompt, "aspect_ratio": ASPECT}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
            rid = body.get("request_id") or body.get("id") or ""
            if rid:
                print(f"  submitted → request_id={rid}")
                return rid
            print(f"  [WARN] submit OK but no request_id in response: {body}")
            return None
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")[:300]
        print(f"  [ERROR] submit HTTP {e.code}: {body_text}")
        return None
    except Exception as exc:
        print(f"  [ERROR] submit exception: {exc}")
        return None


def _poll(api_key: str, request_id: str, base_url: str) -> tuple[str, str | None]:
    """
    Poll until terminal status or timeout.
    Returns (status, output_url).
    status: "completed" | "failed" | "cancelled" | "timeout"
    """
    import urllib.request, urllib.error
    poll_url = f"{base_url}/predictions/{request_id}/result"
    deadline = time.monotonic() + POLL_TIMEOUT
    tick = 0
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        tick += 1
        req = urllib.request.Request(
            poll_url,
            headers={"x-api-key": api_key},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"    poll tick {tick}: HTTP {e.code} (retrying)")
            continue
        except Exception as exc:
            print(f"    poll tick {tick}: {exc} (retrying)")
            continue

        status = body.get("status", "")
        print(f"    poll tick {tick}: {status}")
        if status == "completed":
            outputs = body.get("outputs", [])
            url = outputs[0] if outputs else None
            return "completed", url
        if status == "failed":
            return "failed", None
        if status == "cancelled":
            return "cancelled", None
    return "timeout", None


def _download(url: str, dest: Path) -> bool:
    """Download video URL to dest. Returns True on success."""
    import urllib.request
    try:
        print(f"  downloading → {dest.name}")
        urllib.request.urlretrieve(url, dest)
        size = dest.stat().st_size
        if size < 10_000:
            print(f"  [WARN] file too small ({size} bytes) — possibly corrupt")
            dest.unlink(missing_ok=True)
            return False
        print(f"  saved {size // 1024} KB → {dest}")
        return True
    except Exception as exc:
        print(f"  [ERROR] download failed: {exc}")
        return False


def _ffmpeg_concat(clip_a: Path, clip_b: Path, output: Path) -> bool:
    """Concatenate two MP4 clips using ffmpeg concat demuxer. Returns True on success."""
    concat_list = output.parent / f"_concat_{output.stem}.txt"
    concat_list.write_text(f"file '{clip_a}'\nfile '{clip_b}'\n")
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    concat_list.unlink(missing_ok=True)
    if result.returncode != 0:
        print(f"  [ERROR] ffmpeg concat failed:\n{result.stderr[-400:]}")
        return False
    size = output.stat().st_size
    print(f"  concat → {output.name} ({size // 1024} KB)")
    return True


def _probe_duration(path: Path) -> float:
    """Return actual clip duration in seconds via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    try:
        data = json.loads(result.stdout)
        return round(float(data["format"]["duration"]), 2)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    env = _load_env()
    _guard_dry_run(env)
    api_key = _get_api_key(env)
    base_url = env.get("MUAPI_BASE_URL", "https://api.muapi.ai/api/v1")

    with open(VP_PATH) as f:
        vp = json.load(f)

    all_target = MISSING_SCENES + DANGER_SCENES
    prompts = _load_prompts(vp, all_target)

    print(f"\n{'='*60}")
    print(f"Repair runner: {EP_ID}")
    print(f"Model: {MODEL}  Budget cap: ${BUDGET_CAP:.2f}  Clips: {len(all_target)}")
    print(f"{'='*60}\n")

    spent = 0.0
    results: list[dict] = []

    # -----------------------------------------------------------------------
    # Phase 1 — Generate missing clips
    # -----------------------------------------------------------------------
    print("=== PHASE 1: Missing clips ===")
    for sid in MISSING_SCENES:
        dest = VIDEO_DIR / f"sc_{sid}.mp4"
        if dest.exists() and dest.stat().st_size >= 10_000:
            print(f"[SKIP] {sid} already exists ({dest.stat().st_size // 1024} KB)")
            results.append({"scene_id": sid, "type": "missing", "status": "already_present", "path": str(dest)})
            continue

        prompt = prompts.get(sid, "")
        if not prompt:
            print(f"[SKIP] {sid} has no prompt")
            results.append({"scene_id": sid, "type": "missing", "status": "no_prompt"})
            continue

        if spent + PRICE_USD > BUDGET_CAP:
            print(f"[ABORT] Budget cap ${BUDGET_CAP:.2f} reached — stopping")
            break

        print(f"\n--- {sid} (missing) ---")
        print(f"  prompt: {prompt[:100]}...")
        rid = _submit(api_key, prompt, base_url)
        if not rid:
            results.append({"scene_id": sid, "type": "missing", "status": "submit_failed"})
            continue

        status, url = _poll(api_key, rid, base_url)
        if status != "completed" or not url:
            print(f"  [FAIL] {sid}: {status}")
            results.append({"scene_id": sid, "type": "missing", "status": status})
            continue

        if _download(url, dest):
            spent += PRICE_USD
            dur = _probe_duration(dest)
            results.append({"scene_id": sid, "type": "missing", "status": "generated",
                             "path": str(dest), "duration_s": dur})
            print(f"  DONE: {sid} → {dur}s  [spent ${spent:.2f}]")
        else:
            results.append({"scene_id": sid, "type": "missing", "status": "download_failed"})

    # -----------------------------------------------------------------------
    # Phase 2 — Danger repair clips
    # -----------------------------------------------------------------------
    print("\n=== PHASE 2: Danger repair clips ===")
    for sid in DANGER_SCENES:
        repair_dest = VIDEO_DIR / f"sc_{sid}_repair.mp4"
        if repair_dest.exists() and repair_dest.stat().st_size >= 10_000:
            print(f"[SKIP] {sid} repair already exists ({repair_dest.stat().st_size // 1024} KB)")
            results.append({"scene_id": sid, "type": "danger_repair", "status": "already_present",
                             "path": str(repair_dest)})
            continue

        prompt = prompts.get(sid, "")
        if not prompt:
            print(f"[SKIP] {sid} has no prompt")
            results.append({"scene_id": sid, "type": "danger_repair", "status": "no_prompt"})
            continue

        if spent + PRICE_USD > BUDGET_CAP:
            print(f"[ABORT] Budget cap ${BUDGET_CAP:.2f} reached — stopping")
            break

        print(f"\n--- {sid} (danger repair) ---")
        print(f"  prompt: {prompt[:100]}...")
        rid = _submit(api_key, prompt, base_url)
        if not rid:
            results.append({"scene_id": sid, "type": "danger_repair", "status": "submit_failed"})
            continue

        status, url = _poll(api_key, rid, base_url)
        if status != "completed" or not url:
            print(f"  [FAIL] {sid} repair: {status}")
            results.append({"scene_id": sid, "type": "danger_repair", "status": status})
            continue

        if _download(url, repair_dest):
            spent += PRICE_USD
            dur = _probe_duration(repair_dest)
            results.append({"scene_id": sid, "type": "danger_repair", "status": "generated",
                             "path": str(repair_dest), "duration_s": dur})
            print(f"  DONE: {sid}_repair → {dur}s  [spent ${spent:.2f}]")
        else:
            results.append({"scene_id": sid, "type": "danger_repair", "status": "download_failed"})

    # -----------------------------------------------------------------------
    # Phase 3 — Pre-concat danger pairs
    # -----------------------------------------------------------------------
    print("\n=== PHASE 3: Pre-concat danger pairs ===")
    for sid in DANGER_SCENES:
        original = VIDEO_DIR / f"sc_{sid}.mp4"
        repair   = VIDEO_DIR / f"sc_{sid}_repair.mp4"
        backup   = VIDEO_DIR / f"sc_{sid}_original.mp4"
        combined = VIDEO_DIR / f"sc_{sid}_combined.mp4"

        if not original.exists():
            print(f"[SKIP] {sid}: original clip missing — cannot concat")
            continue
        if not repair.exists():
            print(f"[SKIP] {sid}: repair clip missing — cannot concat")
            continue

        # Backup original if not already backed up
        if not backup.exists():
            shutil.copy2(original, backup)
            print(f"  backed up → sc_{sid}_original.mp4")

        # Concat original + repair → combined
        ok = _ffmpeg_concat(original, repair, combined)
        if not ok:
            print(f"  [ERROR] concat failed for {sid}")
            continue

        # Replace original with combined so timeline_builder picks up 16s clip
        shutil.copy2(combined, original)
        combined_dur = _probe_duration(original)
        print(f"  replaced sc_{sid}.mp4 with combined ({combined_dur}s)")

    # -----------------------------------------------------------------------
    # Phase 4 — QA report
    # -----------------------------------------------------------------------
    print("\n=== PHASE 4: QA Report ===")

    all_clips = sorted(VIDEO_DIR.glob("sc_s*.mp4"))
    # Count only canonical clips (not _repair, _original, _combined suffixes)
    canonical = [p for p in all_clips if not any(
        p.stem.endswith(sfx) for sfx in ("_repair", "_original", "_combined")
    )]
    print(f"Canonical clips on disk: {len(canonical)}/33")

    max_hold = 0.0
    max_hold_scene = ""

    # Load narration beats to compute holds
    np_path = EP_DIR / f"narration_package_{EP_ID}.json"
    with open(np_path) as f:
        np_data = json.load(f)

    beat_dur: dict[str, float] = {}
    for beat in np_data.get("narration_beats", []):
        bid = beat.get("beat_id", "")
        dur = float(beat.get("duration_seconds", 0) or 0)
        beat_dur[bid] = dur

    # Map scene → narration beat
    for scene in vp.get("scenes", []):
        sid = scene.get("scene_id", "")
        narr_ref = scene.get("narration_reference", "")
        clip_path = VIDEO_DIR / f"sc_{sid}.mp4"
        if not clip_path.exists():
            continue
        narr_dur = beat_dur.get(narr_ref, 0.0)
        clip_dur = _probe_duration(clip_path)
        hold = max(0.0, narr_dur - clip_dur)
        if hold > max_hold:
            max_hold = hold
            max_hold_scene = sid

    print(f"Max hold: {max_hold:.1f}s (scene {max_hold_scene})")
    print(f"Total spent: ${spent:.2f}")

    # Summary table
    print("\n--- Generation results ---")
    for r in results:
        status = r.get("status", "?")
        dur = f" {r.get('duration_s', '')}s" if "duration_s" in r else ""
        print(f"  {r['scene_id']:6s} [{r['type']:14s}] {status}{dur}")

    print(f"\n{'='*60}")
    print("Repair complete. Do NOT upload. Run assembly separately.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
