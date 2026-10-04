"""
hypogeum/agents/content_analysis_agent.py — Cortex Advisory content analysis.

Reads the creative choices in one already-published client post (metadata +
thumbnail image) and stores a structured `content_profile`. This is NOT a
production agent — no schema in hypogeum.schemas, no pipeline stage. It runs
opportunistically during client_content_sync, cost-gated by the client's
monthly analysis budget.

Public API:
    analyze_content_item(content_item, *, config=None, client=None,
                         analyze_fn=None, db_path=None) -> dict
        content_item : a hypogeum.client_manager content_items row (dict)
        analyze_fn   : injectable (payload: dict, image: Optional[tuple]) -> str
                       returning the model's raw JSON string. Defaults to a
                       Claude vision call. Tests inject a stub.

    Returns {ok, profile, cost_usd, model_slug, error}.

`analysis_depth='full_video'` raises NotImplementedError with a TODO — a
later phase adds yt-dlp fetch -> Whisper transcript -> vision-on-frames.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Callable, Optional

import requests

from hypogeum.logger import get_logger
from hypogeum.prompt_loader import load_prompt

log = get_logger(agent="content_analysis_agent")

_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "content_analysis.txt"
_SYSTEM_PROMPT = load_prompt(_PROMPT_FILE)
_MODEL = "anthropic/claude-sonnet-4.6"
_MAX_TOKENS = 1024
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)
_MAX_IMG_BYTES = 4_500_000

_CAPTION_LEN = {"none", "short", "medium", "long"}
_FORMAT_TAGS = {"talking_head", "b_roll", "motion_graphic", "carousel", "static",
                "text_heavy", "screen_recording", "vlog"}


def _fetch_image(url: str) -> Optional[tuple[str, str]]:
    """Return (base64, media_type) for a thumbnail URL, or None."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Cortex-Advisory/1.0"})
        if r.status_code != 200 or not r.content or len(r.content) > _MAX_IMG_BYTES:
            return None
        ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ct not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            ct = "image/jpeg"
        return base64.b64encode(r.content).decode("ascii"), ct
    except Exception as exc:  # noqa: BLE001
        log.warn("thumbnail_fetch_failed", url=url, error=str(exc))
        return None


def _default_analyze_fn(config):
    from hypogeum.openrouter import call_claude

    def _run(payload: dict, image: Optional[tuple[str, str]]) -> str:
        content: list[dict] = []
        if image:
            b64, media_type = image
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{b64}"}})
        content.append({"type": "text",
                        "text": "Analyse this post. Input:\n" + json.dumps(payload)})
        result = call_claude(
            model_slug=_MODEL,
            messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": content}],
            max_tokens=_MAX_TOKENS, config=config,
            episode_id=payload.get("_content_id"), agent="content_analysis_agent",
        )
        return result.content, float(getattr(result, "estimated_cost_usd", 0.0) or 0.0)

    return _run


def _coerce(profile: dict) -> dict:
    """Fill any missing field with a safe default so downstream code is total."""
    p = dict(profile or {})
    hook = dict(p.get("hook") or {})
    hook.setdefault("promise", "")
    hook["strength_1_10"] = _clamp_int(hook.get("strength_1_10"), 1, 10, 5)
    hook.setdefault("type", "none")
    cap = dict(p.get("caption") or {})
    cap["length"] = cap.get("length") if cap.get("length") in _CAPTION_LEN else "none"
    cap["has_cta"] = bool(cap.get("has_cta"))
    cap.setdefault("hashtag_strategy", "none")
    thumb = dict(p.get("thumbnail") or {})
    thumb["present"] = bool(thumb.get("present"))
    thumb.setdefault("text_legibility", "n/a")
    thumb.setdefault("contrast", "n/a")
    thumb.setdefault("subject", "n/a")
    thumb.setdefault("clutter", "n/a")
    thumb["score_1_10"] = _clamp_int(thumb.get("score_1_10"), 0, 10, 0)
    fmt = [t for t in (p.get("format_tags") or []) if t in _FORMAT_TAGS][:3]
    topics = [str(t).strip().lower() for t in (p.get("topic_tags") or []) if str(t).strip()][:5]
    return {"hook": hook, "caption": cap, "thumbnail": thumb,
            "format_tags": fmt or ["b_roll"], "topic_tags": topics,
            "notes": str(p.get("notes") or "")[:200]}


def _clamp_int(v, lo, hi, default):
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _parse(raw: str) -> dict:
    s = (raw or "").strip()
    m = _FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()
    return json.loads(s)


def analyze_content_item(content_item: dict, *, config=None, client: Optional[dict] = None,
                         analyze_fn: Optional[Callable] = None,
                         db_path: Optional[Path] = None) -> dict:
    out = {"ok": False, "profile": None, "cost_usd": 0.0,
           "model_slug": _MODEL, "error": None}
    depth = (client or {}).get("analysis_depth", "metadata")
    if depth == "full_video":
        raise NotImplementedError(
            "full_video analysis not implemented — TODO: yt-dlp fetch -> "
            "Whisper transcript (hypogeum.elevenlabs) -> vision-on-frames reviewer")

    cid = content_item.get("content_id")
    payload = {
        "_content_id": cid,
        "platform": content_item.get("platform"),
        "media_type": content_item.get("media_type") or "video",
        "title": content_item.get("title") or "",
        "caption": content_item.get("caption") or "",
        "has_thumbnail": bool(content_item.get("thumbnail_url")),
    }
    image = _fetch_image(content_item.get("thumbnail_url") or "")
    payload["has_thumbnail"] = bool(image)

    if analyze_fn is None:
        if config is None:
            from hypogeum.config import Config
            config = Config()
        # spend with the keys of the workspace owning this client (Phase 2)
        from hypogeum.workspace_keys import config_for_client
        config = config_for_client(config, (client or {}).get("client_id") or content_item.get("client_id"))
        analyze_fn = _default_analyze_fn(config)

    try:
        res = analyze_fn(payload, image)
        raw, cost = res if isinstance(res, tuple) else (res, 0.0)
        profile = _coerce(_parse(raw))
        out.update(ok=True, profile=profile, cost_usd=float(cost or 0.0))
    except NotImplementedError:
        raise
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
        log.warn("content_analysis_failed", content_id=cid, error=str(exc))
        return out

    if cid:
        try:
            from hypogeum.client_manager import store_content_profile
            store_content_profile(cid, profile, analysis_depth=depth,
                                  model_slug=_MODEL, cost_usd=out["cost_usd"],
                                  db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            log.warn("content_profile_store_failed", content_id=cid, error=str(exc))
    return out
