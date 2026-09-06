"""
hypogeum/public_ingest.py — pull a creator's PUBLIC catalogue with no account
access, for the Cortex Advisory "before signing / prospect" workflow.

YouTube: yt-dlp against the channel's /videos tab. A flat pass lists every
video (id / title / thumbnail) instantly; a bounded deep pass then fills in
public view / like / comment counts and the upload date per video.

Instagram / TikTok / Facebook have no reliable no-auth catalogue endpoint —
those stay on the CSV import + paste-a-URL paths.

Public API:
    fetch_youtube_channel(url_or_handle, *, limit=60, deep_limit=40) -> dict
        {ok, platform, channel, channel_url, items: [ {platform_post_id, url,
         title, thumbnail_url, published_at, views, likes, comments,
         duration_s} ], counts, error}
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="public_ingest")

_MAX_LIMIT = 200
_MAX_DEEP = 80

# The default web client trips YouTube's "captcha challenge before playback"
# anti-bot on repeated datacenter requests; the android player clients still
# return public metadata (view/like/comment counts, upload date). Kept narrow
# on purpose — mixing in web/ios clients makes yt-dlp raise "format not
# available" during selection even with skip_download.
_YT_EXTRACTOR_ARGS = {"youtube": {"player_client": ["android", "android_vr"]}}


def _channel_videos_url(raw: str) -> Optional[str]:
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("@"):
        return f"https://www.youtube.com/{s}/videos"
    if not s.startswith(("http://", "https://")):
        # bare handle or channel id
        if s.startswith("UC") and len(s) >= 20:
            return f"https://www.youtube.com/channel/{s}/videos"
        return f"https://www.youtube.com/@{s.lstrip('@')}/videos"
    s = s.split("?", 1)[0].rstrip("/")
    if re.search(r"/(videos|shorts|streams|featured)$", s):
        s = s.rsplit("/", 1)[0]
    return s + "/videos"


def _iso_from_entry(e: dict) -> Optional[str]:
    ts = e.get("timestamp")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    ud = e.get("upload_date")  # YYYYMMDD
    if ud and re.fullmatch(r"\d{8}", str(ud)):
        return f"{ud[:4]}-{ud[4:6]}-{ud[6:8]}T00:00:00+00:00"
    return None


def _best_thumb(e: dict) -> Optional[str]:
    if e.get("thumbnail"):
        return e["thumbnail"]
    thumbs = e.get("thumbnails") or []
    if thumbs:
        return sorted(thumbs, key=lambda t: (t.get("width") or 0))[-1].get("url")
    vid = e.get("id")
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None


def fetch_youtube_channel(url_or_handle: str, *, limit: int = 60,
                          deep_limit: int = 40) -> dict:
    out = {"ok": False, "platform": "youtube", "channel": None,
           "channel_url": None, "items": [], "counts": {}, "error": None}
    ch_url = _channel_videos_url(url_or_handle)
    if not ch_url:
        out["error"] = "empty channel reference"
        return out
    out["channel_url"] = ch_url
    limit = max(1, min(int(limit or 60), _MAX_LIMIT))
    deep_limit = max(0, min(int(deep_limit or 0), _MAX_DEEP, limit))

    try:
        import yt_dlp
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"yt-dlp not available: {exc}"
        return out

    flat_opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                 "extract_flat": "in_playlist", "playlistend": limit,
                 "ignoreerrors": True, "extractor_args": _YT_EXTRACTOR_ARGS}
    try:
        with yt_dlp.YoutubeDL(flat_opts) as y:
            info = y.extract_info(ch_url, download=False)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"channel fetch failed: {exc}"
        log.warn("public_ingest_flat_failed", url=ch_url, error=str(exc))
        return out
    if not info:
        out["error"] = "channel not found or has no public videos"
        return out

    out["channel"] = info.get("channel") or info.get("uploader") or info.get("title")
    entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
    items: dict = {}
    for e in entries[:limit]:
        vid = e["id"]
        items[vid] = {
            "platform_post_id": vid,
            "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
            "title": e.get("title"),
            "thumbnail_url": _best_thumb(e),
            "published_at": _iso_from_entry(e),
            "views": e.get("view_count"),
            "likes": None, "comments": None,
            "duration_s": e.get("duration"),
        }

    # bounded deep pass for the public counts flat mode omits. YouTube's
    # anti-bot is flaky per request, so each video gets one retry and a
    # short pause; partial results are fine (the flat data is the backbone).
    import time
    deep_ids = list(items)[:deep_limit]
    deep_ok = 0
    if deep_ids:
        deep_opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                     "ignoreerrors": True, "extractor_args": _YT_EXTRACTOR_ARGS}
        with yt_dlp.YoutubeDL(deep_opts) as y:
            for i, vid in enumerate(deep_ids):
                d = None
                for attempt in (1, 2):
                    try:
                        d = y.extract_info(f"https://www.youtube.com/watch?v={vid}",
                                           download=False)
                    except Exception:  # noqa: BLE001
                        d = None
                    if d and d.get("view_count") is not None:
                        break
                    time.sleep(0.5 * attempt)
                if not d:
                    continue
                it = items[vid]
                if d.get("view_count") is not None:
                    it["views"] = d["view_count"]
                    deep_ok += 1
                it["likes"] = d.get("like_count") if d.get("like_count") is not None else it["likes"]
                it["comments"] = d.get("comment_count") if d.get("comment_count") is not None else it["comments"]
                it["duration_s"] = d.get("duration", it["duration_s"])
                it["published_at"] = _iso_from_entry(d) or it["published_at"]
                if d.get("thumbnail"):
                    it["thumbnail_url"] = d["thumbnail"]
                if i < len(deep_ids) - 1:
                    time.sleep(0.35)

    rows = list(items.values())
    out["items"] = rows
    out["counts"] = {
        "listed": len(rows),
        "deep_attempted": len(deep_ids),
        "deep_ok": deep_ok,
        "with_views": len([r for r in rows if r["views"] is not None]),
        "with_date": len([r for r in rows if r["published_at"]]),
    }
    out["ok"] = bool(rows)
    if not rows:
        out["error"] = "no public videos found"
    log.info("public_ingest_youtube", channel=out["channel"], **out["counts"])
    return out


def import_public_channel(client_id: str, url_or_handle: str, *,
                          platform: str = "youtube", limit: int = 60,
                          deep_limit: int = 40, fetch_fn=None, db_path=None) -> dict:
    """Fetch a public channel and persist it as this client's content_items
    (source='public') + client-scoped platform_analytics rows. YouTube only
    for now — other platforms have no no-auth catalogue endpoint.
    `fetch_fn(url, limit=, deep_limit=)` is injectable for tests."""
    from hypogeum import client_manager as cm
    from hypogeum.analytics_store import store_content_metrics

    res = {"ok": False, "channel": None, "added": 0, "metric_rows": 0, "error": None}
    if platform != "youtube":
        res["error"] = f"{platform} has no public catalogue import — use CSV / paste-URL"
        return res
    fetched = (fetch_fn or fetch_youtube_channel)(
        url_or_handle, limit=limit, deep_limit=deep_limit)
    res["channel"] = fetched.get("channel")
    if not fetched["ok"]:
        res["error"] = fetched["error"]
        return res

    for it in fetched["items"]:
        try:
            cid = cm.upsert_content_item(
                client_id, "youtube", platform_post_id=it["platform_post_id"],
                url=it["url"], title=it.get("title"),
                thumbnail_url=it.get("thumbnail_url"), media_type="video",
                published_at=it.get("published_at"), source="public", db_path=db_path)
            res["added"] += 1
        except Exception as exc:  # noqa: BLE001
            log.warn("public_import_upsert_failed", vid=it["platform_post_id"],
                     error=str(exc))
            continue
        metrics = {k: v for k, v in (("views", it.get("views")),
                                     ("likes", it.get("likes")),
                                     ("comments", it.get("comments"))) if v is not None}
        if metrics:
            try:
                # content_items.source is 'public'; platform_analytics.source
                # stays within its own CHECK set — 'manual' = operator-run import.
                store_content_metrics(cid, client_id, "youtube", metrics,
                                      source="manual", db_path=db_path)
                res["metric_rows"] += 1
            except Exception as exc:  # noqa: BLE001
                log.warn("public_import_metrics_failed", vid=it["platform_post_id"],
                         error=str(exc))
    res["ok"] = res["added"] > 0
    log.info("public_channel_imported", client_id=client_id,
             channel=res["channel"], added=res["added"], metric_rows=res["metric_rows"])
    return res
