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

# Cache the MUAPI model catalogue so we don't re-fetch on every page load.
_MODELS_CACHE: dict = {"at": 0.0, "models": None}
_MODELS_TTL = 900  # seconds


def _fallback_models() -> list[dict]:
    """Used only when the live MUAPI catalogue can't be reached."""
    from hypogeum.muapi import MODEL_CREDIT_COSTS
    ids = ["runway-text-to-video", "veo3.1-lite-text-to-video",
           "veo3-fast-text-to-video", "veo3.1-fast-text-to-video", "veo3-text-to-video"]
    return [{"id": i, "price_usd": round(float(MODEL_CREDIT_COSTS.get(i, 2.5)), 4),
             "family": i.split("-")[0], "notes": "", "dynamic_pricing": False}
            for i in ids if i in MODEL_CREDIT_COSTS]


def _fetch_muapi_models(config) -> Optional[list[dict]]:
    """GET the MUAPI model catalogue (all categories). Returns raw model dicts
    or None on any failure. No generation, no spend."""
    import requests
    try:
        base = getattr(config, "MUAPI_BASE_URL", "https://api.muapi.ai/api/v1")
        key = getattr(config, "MUAPI_API_KEY", "") or ""
        r = requests.get(f"{base}/models", headers={"x-api-key": key}, timeout=20)
        r.raise_for_status()
        data = r.json()
        return data.get("models") if isinstance(data, dict) else data
    except Exception as exc:  # noqa: BLE001
        log.error("video_bakeoff_model_fetch_failed", error=str(exc))
        return None


def list_video_models(config=None, *, force_refresh: bool = False) -> list[dict]:
    """Every MUAPI text-to-video model with its live price, cheapest first.
    Cached for 15 min; falls back to a small built-in list if MUAPI is
    unreachable."""
    if config is None:
        from hypogeum.config import Config
        config = Config()

    now = time.monotonic()
    if (not force_refresh and _MODELS_CACHE["models"] is not None
            and now - _MODELS_CACHE["at"] < _MODELS_TTL):
        return _MODELS_CACHE["models"]

    raw = _fetch_muapi_models(config)
    if not raw:
        models = _fallback_models()
    else:
        models = []
        for m in raw:
            if (m.get("category") or "").strip().lower() != "text to video":
                continue
            try:
                price = round(float(m.get("cost")), 4)
            except (TypeError, ValueError):
                continue
            models.append({
                "id":              m.get("name", ""),
                "price_usd":       price,
                "family":          m.get("family", "") or "",
                "notes":           (m.get("description") or "")[:140],
                "dynamic_pricing": bool(m.get("dynamic_pricing")),
            })
        models = [m for m in models if m["id"]]
        models.sort(key=lambda m: m["price_usd"])
        if not models:
            models = _fallback_models()

    _MODELS_CACHE.update(at=now, models=models)
    return models


def _model_price_map(config=None) -> dict:
    return {m["id"]: m["price_usd"] for m in list_video_models(config)}


def _valid_model_ids(config=None) -> set:
    return set(_model_price_map(config))


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

# Neutral fallback scene, used only when a channel has no brand profile.
_FALLBACK_SCENE = {
    "scene": "A single subject in a defined space with clear depth and one dominant light source.",
    "style": "restrained, photographic, natural colour",
    "camera": "controlled slow push in",
    "subject": "the primary subject, held in the light",
    "motion": "subtle environmental motion only",
    "lighting": "one directional light source, motivated, with visible falloff",
}


def _scene_from_brand(brand) -> dict:
    """Compose a representative, channel-specific scene from a BrandProfile."""
    style = (getattr(brand, "visual_style", "") or "").strip()
    tone = (getattr(brand, "tone", "") or "").strip().lower()
    pillars = getattr(brand, "content_pillars", None) or []
    subject_hint = (pillars[0] if pillars else "the main subject").strip()
    # Trim an over-long pillar to its first clause.
    for sep in (" — ", " - ", ":", ";", ","):
        if sep in subject_hint:
            subject_hint = subject_hint.split(sep)[0].strip()
            break

    gentle = any(w in tone for w in ("gentle", "whimsical", "soothing", "calm", "warm", "playful"))
    scene = {
        "scene": (
            f"A representative establishing shot for this channel: {subject_hint}, "
            f"rendered in the channel's visual world."
        ),
        "style": style or _FALLBACK_SCENE["style"],
        "camera": "slow gentle drift" if gentle else "controlled slow push in",
        "subject": subject_hint,
        "motion": (
            "soft ambient motion — drifting particles, a slight sway"
            if gentle else "subtle environmental motion only"
        ),
        "lighting": (
            "warm, soft, low-contrast key light"
            if gentle else "one directional light source, motivated, with visible falloff"
        ),
    }
    return scene


def build_channel_scene_prompt(
    channel_id: str,
    *,
    aspect_ratio: str = "16:9",
    db_path: Optional[Path] = None,
) -> dict:
    """A representative, fully-editable scene prompt for `channel_id`, built
    from that channel's brand profile (visual style, tone, content pillars).
    Returns {channel_id, aspect_ratio, brand_name, scene_fields, prompt}."""
    from hypogeum.channel_config import get_channel_profile, get_brand_profile

    brand = None
    brand_name = None
    try:
        ch = get_channel_profile(channel_id, db_path=db_path)
        if ch and ch.brand_profile_id:
            brand = get_brand_profile(ch.brand_profile_id, db_path=db_path)
            brand_name = getattr(brand, "name", None)
    except Exception:  # noqa: BLE001
        brand = None

    scene = _scene_from_brand(brand) if brand else dict(_FALLBACK_SCENE)

    _LABELS = {
        "scene": "SCENE", "style": "STYLE", "camera": "CAMERA",
        "subject": "SUBJECT", "motion": "MOTION", "lighting": "LIGHTING",
    }
    prompt = "\n".join(f"{_LABELS[k]}: {scene[k]}" for k in
                       ("scene", "style", "camera", "subject", "motion", "lighting"))

    return {
        "channel_id":   channel_id,
        "aspect_ratio": aspect_ratio,
        "brand_name":   brand_name,
        "scene_fields": scene,
        "prompt":       prompt,
    }


# ---------------------------------------------------------------------------
# Cost preview
# ---------------------------------------------------------------------------

def estimate_cost(models: list, config=None) -> dict:
    """{lines, total_usd, clip_count, any_dynamic} for the selected models
    (one clip each). Prices come from the live MUAPI catalogue."""
    catalogue = {m["id"]: m for m in list_video_models(config)}
    lines, total = [], 0.0
    for m in models:
        cm = catalogue.get(m, {})
        price = round(float(cm.get("price_usd", 2.5)), 4)
        lines.append({"model": m, "price_usd": price,
                      "dynamic": bool(cm.get("dynamic_pricing"))})
        total += price
    return {
        "lines": lines,
        "total_usd": round(total, 4),
        "clip_count": len(models),
        "any_dynamic": any(l["dynamic"] for l in lines),
    }


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
                  config, req_dur: float, price: float) -> dict:
    """Submit → poll → download one clip for `model`. Returns a result dict.
    Never raises."""
    from hypogeum.muapi import (
        _build_job_payload, _submit_http, _poll_until_complete, _download_file,
        _validate_video_file, _DRY_RUN_REQUEST_ID_PREFIX,
        _generate_dry_run_placeholder_clip,
    )
    price = round(float(price), 4)
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

    price_map = _model_price_map(config)
    models = [m for m in dict.fromkeys(models) if m in price_map]
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
            price_map.get(model, 2.5),
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
