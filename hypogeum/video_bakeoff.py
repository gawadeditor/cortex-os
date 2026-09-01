"""
hypogeum/video_bakeoff.py — operator video-model bake-off.

Lets an operator take ONE scene prompt (assembled from a channel's canonical
Video DNA, editable), run it through several MUAPI video models, see the
per-model price BEFORE spending, and then compare the actual clips
side-by-side (cost, latency, duration, motion). The winner can be adopted
as that channel's video model (hypogeum.channel_config.ModelProfile.video_model).

This is an operator tool, not the production path: it deliberately bypasses
the grant / weekly-budget / submission-gate machinery that real episode
generation goes through. Spend is guarded by an explicit operator confirm
in the dashboard, not by a signed authorization.

Public API:
    list_video_models(config=None)                 -> list[dict]
    build_channel_scene_prompt(channel_id, *, aspect_ratio, db_path=None) -> dict
    estimate_cost(models)                           -> dict
    run_bakeoff(channel_id, prompt, models, *, aspect_ratio, db_path=None,
                config=None) -> dict
    get_bakeoff(run_id)                             -> dict | None
    list_bakeoffs(channel_id=None, limit=20)        -> list[dict]
    clip_path(run_id, model)                        -> Path | None
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BAKEOFF_ROOT = _PROJECT_ROOT / ".hypogeum-assets" / "video_bakeoffs"


# ---------------------------------------------------------------------------
# Model catalogue
# ---------------------------------------------------------------------------

def list_video_models(config=None) -> list[dict]:
    """Every text-to-video model MUAPI exposes a price for, cheapest first."""
    from hypogeum.muapi import (
        MODEL_CREDIT_COSTS, _VEO3_MODELS, _VEO3_1_MODELS, _RUNWAY_MODELS,
    )
    _NOTES = {
        "veo3-text-to-video":        "Premium — highest fidelity; reserve for hero / opening shots.",
        "veo3.1-lite-text-to-video": "Current primary production model. 1080p, 8s.",
        "veo3.1-fast-text-to-video": "Costs more than lite; ignores the resolution parameter.",
        "runway-text-to-video":      "Cheapest. Lower fidelity and motion range.",
    }
    text_to_video = {
        m for m in (_VEO3_MODELS | _VEO3_1_MODELS | _RUNWAY_MODELS)
        if "image-to-video" not in m
    }
    out = []
    for model in sorted(text_to_video, key=lambda m: MODEL_CREDIT_COSTS.get(m, 99)):
        if model not in MODEL_CREDIT_COSTS:
            continue
        family = ("veo3.1" if model in _VEO3_1_MODELS
                  else "veo3" if model in _VEO3_MODELS else "runway")
        out.append({
            "id":         model,
            "price_usd":  round(float(MODEL_CREDIT_COSTS[model]), 4),
            "family":     family,
            "notes":      _NOTES.get(model, ""),
        })
    return out


def _valid_model_ids(config=None) -> set:
    return {m["id"] for m in list_video_models(config)}


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

# Topic-agnostic representative scene. The channel-specific character comes
# from the canonical Video / Camera / Motion / Lighting / Historical DNA and
# the brand identity that assemble_video_scene_prompt() appends; the operator
# is free to rewrite any of this before running.
_REPRESENTATIVE_SCENE = {
    "scene": (
        "A weathered stone chamber lit by a single shaft of daylight from a "
        "high opening. Dust drifts through the beam. Carved relief detail is "
        "visible on the far wall."
    ),
    "camera": "controlled slow push in",
    "subject": "the carved far wall, relief detail held in the light",
    "motion": "drifting dust; the light shaft barely shifts",
    "lighting": "one hard daylight shaft from a high opening, warm, steep falloff",
}


def build_channel_scene_prompt(
    channel_id: str,
    *,
    aspect_ratio: str = "16:9",
    db_path: Optional[Path] = None,
) -> dict:
    """Assemble a representative, fully-formed scene prompt for `channel_id`
    using the canonical DNA + brand identity. Returns
    {channel_id, aspect_ratio, scene_fields, prompt}."""
    from hypogeum.video_prompt_writer import assemble_video_scene_prompt
    scene = dict(_REPRESENTATIVE_SCENE)
    try:
        prompt = assemble_video_scene_prompt(scene)
    except Exception as exc:  # noqa: BLE001
        log.error("video_bakeoff_prompt_assembly_failed", channel_id=channel_id, error=str(exc))
        # Fall back to just the authored scene lines.
        prompt = "\n".join(f"{k.capitalize()}: {v}" for k, v in scene.items())
    return {
        "channel_id":   channel_id,
        "aspect_ratio": aspect_ratio,
        "scene_fields": scene,
        "prompt":       prompt,
    }


# ---------------------------------------------------------------------------
# Cost preview
# ---------------------------------------------------------------------------

def estimate_cost(models: list) -> dict:
    """{lines: [{model, price_usd}], total_usd} for the selected models
    (one clip each)."""
    from hypogeum.muapi import MODEL_CREDIT_COSTS, _DEFAULT_UNKNOWN_MODEL_COST
    lines = []
    total = 0.0
    for m in models:
        price = round(float(MODEL_CREDIT_COSTS.get(m, _DEFAULT_UNKNOWN_MODEL_COST)), 4)
        lines.append({"model": m, "price_usd": price})
        total += price
    return {"lines": lines, "total_usd": round(total, 4), "clip_count": len(models)}


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def _run_dir(run_id: str) -> Path:
    return _BAKEOFF_ROOT / run_id


def clip_path(run_id: str, model: str) -> Optional[Path]:
    p = _run_dir(run_id) / f"{_safe(model)}.mp4"
    return p if p.exists() else None


def _safe(model: str) -> str:
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in model)


def _motion_score(path: Path, duration: float) -> Optional[float]:
    """0-100 via ffmpeg freezedetect; None if ffmpeg unavailable."""
    import re as _re
    import subprocess as _sp
    try:
        r = _sp.run(
            ["ffmpeg", "-i", str(path), "-vf", "freezedetect=n=0.001:d=0.1",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=40,
        )
        starts = [float(m) for m in _re.findall(r"freeze_start:\s*([\d.]+)", r.stderr)]
        ends = [float(m) for m in _re.findall(r"freeze_end:\s*([\d.]+)", r.stderr)]
        frozen = sum(e - s for s, e in zip(starts, ends))
        if len(starts) > len(ends) and duration:
            frozen += duration - starts[-1]
        if not duration:
            return None
        return round(max(0.0, (1.0 - max(0.0, frozen) / duration) * 100))
    except Exception:  # noqa: BLE001
        return None


def _probe_duration(path: Path) -> Optional[float]:
    import subprocess as _sp
    try:
        r = _sp.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        return round(float(r.stdout.strip()), 2) if r.stdout.strip() else None
    except Exception:  # noqa: BLE001
        return None


def _generate_one(prompt: str, model: str, aspect_ratio: str, dest: Path,
                  config, req_dur: float) -> dict:
    """Submit → poll → download one clip for `model`. Returns a result dict.
    Never raises."""
    from hypogeum.muapi import (
        MODEL_CREDIT_COSTS, _DEFAULT_UNKNOWN_MODEL_COST, _build_job_payload,
        _submit_http, _poll_until_complete, _download_file, _validate_video_file,
        _DRY_RUN_REQUEST_ID_PREFIX, _generate_dry_run_placeholder_clip,
    )
    price = round(float(MODEL_CREDIT_COSTS.get(model, _DEFAULT_UNKNOWN_MODEL_COST)), 4)
    started = time.monotonic()
    base = {"model": model, "price_usd": price, "ok": False, "error": None,
            "clip_file": None, "latency_s": None, "duration_s": None,
            "motion_score": None}

    try:
        endpoint = f"{config.MUAPI_BASE_URL}/{model}"
        payload = _build_job_payload(
            prompt, model, aspect_ratio, req_dur, None,
            video_resolution=getattr(config, "MUAPI_VIDEO_RESOLUTION", "1080p"),
        )
        request_id, fail = _submit_http(endpoint, payload, config, f"bakeoff-{_safe(model)}", log)
        if request_id is None:
            base["error"] = f"submit failed: {fail}"
            return base

        dest.parent.mkdir(parents=True, exist_ok=True)

        if request_id.startswith(_DRY_RUN_REQUEST_ID_PREFIX):
            _generate_dry_run_placeholder_clip(dest, aspect_ratio, req_dur, log)
        else:
            result, fail = _poll_until_complete(request_id, config, f"bakeoff-{_safe(model)}", log)
            if result is None:
                base["error"] = f"generation failed: {fail}"
                return base
            outs = result.get("outputs", [])
            url = ""
            if outs:
                url = outs[0] if isinstance(outs[0], str) else outs[0].get("url", "")
            if not url:
                base["error"] = "no video url in result"
                return base
            ok, dl = _download_file(url, dest, log)
            if not ok:
                ok, dl = _download_file(url, dest, log)
            if not ok:
                base["error"] = f"download failed: {dl}"
                return base
            valid, vr = _validate_video_file(dest, req_dur)
            if not valid:
                dest.unlink(missing_ok=True)
                base["error"] = f"invalid clip: {vr}"
                return base

        dur = _probe_duration(dest)
        base.update({
            "ok":           True,
            "clip_file":    dest.name,
            "latency_s":    round(time.monotonic() - started, 1),
            "duration_s":   dur,
            "motion_score": _motion_score(dest, dur or req_dur),
        })
        return base
    except Exception as exc:  # noqa: BLE001
        log.error("video_bakeoff_generate_error", model=model, error=str(exc))
        base["error"] = str(exc)
        return base


def run_bakeoff(
    channel_id: str,
    prompt: str,
    models: list,
    *,
    aspect_ratio: str = "16:9",
    db_path: Optional[Path] = None,
    config=None,
) -> dict:
    """Generate one clip per model for `prompt` and persist the run.
    Returns the run manifest dict."""
    if config is None:
        from hypogeum.config import Config
        config = Config()

    valid = _valid_model_ids(config)
    models = [m for m in dict.fromkeys(models) if m in valid]
    if not models:
        raise ValueError("no valid video models selected")
    if not (prompt or "").strip():
        raise ValueError("prompt is empty")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_dir = _run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    req_dur = 8.0

    results = []
    for model in models:
        log.info("video_bakeoff_model_start", run_id=run_id, model=model, channel_id=channel_id)
        results.append(_generate_one(
            prompt, model, aspect_ratio, run_dir / f"{_safe(model)}.mp4", config, req_dur,
        ))

    manifest = {
        "run_id":         run_id,
        "channel_id":     channel_id,
        "created_at":     datetime.now(timezone.utc).isoformat(),
        "aspect_ratio":   aspect_ratio,
        "prompt":         prompt,
        "dry_run":        bool(getattr(config, "MUAPI_DRY_RUN", False)),
        "models":         models,
        "results":        results,
        "total_cost_usd": round(sum(r["price_usd"] for r in results if r["ok"]), 4),
        "spent_cost_usd": round(sum(r["price_usd"] for r in results if r["ok"]
                                    and not getattr(config, "MUAPI_DRY_RUN", False)), 4),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    log.info("video_bakeoff_complete", run_id=run_id, channel_id=channel_id,
             models=len(models), ok=sum(1 for r in results if r["ok"]))
    return manifest


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_bakeoff(run_id: str) -> Optional[dict]:
    p = _run_dir(run_id) / "manifest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None


def list_bakeoffs(channel_id: Optional[str] = None, limit: int = 20) -> list[dict]:
    if not _BAKEOFF_ROOT.exists():
        return []
    runs = []
    for d in _BAKEOFF_ROOT.iterdir():
        m = get_bakeoff(d.name) if d.is_dir() else None
        if not m:
            continue
        if channel_id and m.get("channel_id") != channel_id:
            continue
        runs.append({
            "run_id":         m["run_id"],
            "channel_id":     m.get("channel_id"),
            "created_at":     m.get("created_at"),
            "models":         m.get("models", []),
            "ok_count":       sum(1 for r in m.get("results", []) if r.get("ok")),
            "total_cost_usd": m.get("total_cost_usd", 0.0),
            "dry_run":        m.get("dry_run", False),
        })
    runs.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return runs[:limit]
