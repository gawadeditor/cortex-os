"""
hypogeum/oembed.py — resolve a public social-post URL to its platform,
post id, and light metadata (title / author / thumbnail).

Used by the Cortex Advisory manual-content-add flow: an operator pastes a
YouTube / TikTok / Instagram / Facebook post URL and Cortex creates a
`content_items` row from it. Analytics attach later on the next Zernio sync
if the account is connected.

No API keys. YouTube and TikTok expose a public oembed endpoint; Instagram
and Facebook require a Graph token, so for those we resolve the post id
from the URL shape only and leave metadata blank.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse, parse_qs, quote

from hypogeum.logger import get_logger

log = get_logger(__name__)

_OEMBED = {
    "youtube": "https://www.youtube.com/oembed?url={url}&format=json",
    "tiktok": "https://www.tiktok.com/oembed?url={url}",
}

_TIMEOUT = 8


def detect_platform(url: str) -> Optional[str]:
    h = (urlparse(url).hostname or "").lower().lstrip("www.")
    if "youtube.com" in h or "youtu.be" in h:
        return "youtube"
    if "tiktok.com" in h:
        return "tiktok"
    if "instagram.com" in h:
        return "instagram"
    if "facebook.com" in h or "fb.watch" in h or "fb.com" in h:
        return "facebook"
    return None


def _youtube_id(url: str) -> Optional[str]:
    p = urlparse(url)
    if (p.hostname or "").endswith("youtu.be"):
        return p.path.lstrip("/").split("/")[0] or None
    qs = parse_qs(p.query)
    if qs.get("v"):
        return qs["v"][0]
    m = re.search(r"/(shorts|embed|live)/([A-Za-z0-9_-]{6,})", p.path)
    return m.group(2) if m else None


def _tiktok_id(url: str) -> Optional[str]:
    m = re.search(r"/video/(\d+)", url) or re.search(r"/photo/(\d+)", url)
    return m.group(1) if m else None


def _instagram_id(url: str) -> Optional[str]:
    m = re.search(r"/(p|reel|reels|tv)/([A-Za-z0-9_-]+)", urlparse(url).path)
    return m.group(2) if m else None


def _facebook_id(url: str) -> Optional[str]:
    p = urlparse(url)
    qs = parse_qs(p.query)
    if qs.get("v"):
        return qs["v"][0]
    m = (re.search(r"/videos/(\d+)", p.path)
         or re.search(r"/posts/(\d+)", p.path)
         or re.search(r"/reel/(\d+)", p.path)
         or re.search(r"/watch/?\?v=(\d+)", url))
    return m.group(1) if m else None


_ID_RESOLVERS = {
    "youtube": _youtube_id,
    "tiktok": _tiktok_id,
    "instagram": _instagram_id,
    "facebook": _facebook_id,
}


def resolve_url(url: str) -> dict:
    """Return {ok, platform, platform_post_id, title, author, thumbnail_url,
    url, error}. `ok` is True when at least a platform + post id resolved."""
    url = (url or "").strip()
    out = {"ok": False, "platform": None, "platform_post_id": None,
           "title": None, "author": None, "thumbnail_url": None,
           "url": url, "error": None}
    if not url.lower().startswith(("http://", "https://")):
        out["error"] = "not a URL"
        return out

    platform = detect_platform(url)
    if not platform:
        out["error"] = "unrecognised platform"
        return out
    out["platform"] = platform

    pid = _ID_RESOLVERS[platform](url)
    out["platform_post_id"] = pid

    endpoint = _OEMBED.get(platform)
    if endpoint:
        try:
            import requests
            r = requests.get(endpoint.format(url=quote(url, safe="")),
                             timeout=_TIMEOUT,
                             headers={"User-Agent": "Cortex-Advisory/1.0"})
            if r.status_code == 200:
                j = r.json()
                out["title"] = j.get("title")
                out["author"] = j.get("author_name")
                out["thumbnail_url"] = j.get("thumbnail_url")
                if not pid and platform == "youtube":
                    out["platform_post_id"] = _youtube_id(j.get("html", "") or "")
            else:
                out["error"] = f"oembed {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"oembed failed: {exc}"
            log.warning("oembed_failed", platform=platform, url=url, error=str(exc))

    out["ok"] = bool(out["platform"] and out["platform_post_id"])
    if not out["ok"] and not out["error"]:
        out["error"] = "could not resolve a post id from the URL"
    return out
