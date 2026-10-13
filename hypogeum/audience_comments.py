"""
hypogeum/audience_comments.py — what the audience says it wants, from public comments on a channel's own videos.

    fetch_channel_comments(channel_id)   -> store top-level comments (YouTube Data API, the channel's own OAuth)
    audience_voice(conn, channel_id)     -> {comments, questions, requests, top_terms}      (pure read)

Deterministic: a comment is a *request* when it asks for a topic/part ("do one about", "part 2", "please make")
and a *question* when it asks something. No LLM, nothing invented; with fewer than _MIN_COMMENTS comments it says
so instead of reporting a pattern. Comments on a video with comments disabled (HTTP 403) are skipped silently.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import List, Optional

import requests

from hypogeum.logger import get_logger

log = get_logger(agent="audience_comments")

_URL = "https://www.googleapis.com/youtube/v3/commentThreads"
_TIMEOUT = 30
_MAX_VIDEOS = 25
_PER_VIDEO = 50
_MIN_COMMENTS = 10
_REQUEST_RE = re.compile(
    r"\b(do (one|a video|an? episode) (on|about)|make (one|a video|an? episode)|please (make|do|cover)|"
    r"can you (make|do|cover|explain)|part (2|two|3|three)|next (video|episode)|you should (cover|do|make)|"
    r"what about|would love to see|i want to see|cover )", re.I)
_STOP = set("the a an and or but of to in on at for with is are was were be it this that i you we they he she my your our "
            "his her not no yes so if as by from have has had do does did will would can could just very really more most "
            "video videos channel".split())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_channel_comments(channel_id: str, *, db_path=None, token: Optional[str] = None, force: bool = False) -> dict:
    """At most once a day per channel (one API call per video). Never raises. {fetched, videos, skipped, error}."""
    out = {"channel_id": channel_id, "fetched": 0, "videos": 0, "skipped": 0, "error": None}
    try:
        from hypogeum.db import get_connection, initialize_schema
        from hypogeum.youtube_traffic import _video_ids
        initialize_schema(db_path)
        with get_connection(db_path) as conn:
            last = conn.execute("SELECT MAX(fetched_at) FROM audience_comments WHERE channel_id=?", (channel_id,)).fetchone()[0]
        if last and not force and last[:10] == _now()[:10]:
            return out
        if token is None:
            try:
                from hypogeum.publishing.youtube_privacy import _access_token
                token = _access_token(channel_id, db_path)
            except Exception as exc:  # noqa: BLE001
                out["error"] = f"no YouTube authorization: {str(exc)[:120]}"
                return out
        with get_connection(db_path) as conn:
            vids = _video_ids(conn, channel_id)[:_MAX_VIDEOS]
        for vid in vids:
            try:
                r = requests.get(_URL, headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT,
                                 params={"part": "snippet", "videoId": vid, "maxResults": _PER_VIDEO, "order": "relevance",
                                         "textFormat": "plainText"})
            except requests.RequestException as exc:
                out["error"] = str(exc)[:120]
                continue
            if r.status_code != 200:          # 403: comments disabled / not available — normal, skip
                out["skipped"] += 1
                continue
            out["videos"] += 1
            with get_connection(db_path) as conn:
                for it in r.json().get("items", []):
                    sn = it["snippet"]["topLevelComment"]["snippet"]
                    conn.execute(
                        "INSERT OR REPLACE INTO audience_comments (channel_id, comment_id, video_id, text, likes, published_at, fetched_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (channel_id, it["id"], vid, sn.get("textDisplay", "")[:1000], int(sn.get("likeCount") or 0),
                         sn.get("publishedAt"), _now()))
                    out["fetched"] += 1
                conn.commit()
    except Exception as exc:  # noqa: BLE001 -- learning must never break the worker
        out["error"] = str(exc)[:200]
        log.error("audience_comments_failed", channel_id=channel_id, error=out["error"])
    return out


def audience_voice(conn, channel_id: str) -> Optional[dict]:
    rows = conn.execute("SELECT text, likes, video_id FROM audience_comments WHERE channel_id=?", (channel_id,)).fetchall()
    if len(rows) < _MIN_COMMENTS:
        return None
    questions = [r for r in rows if "?" in r[0]]
    requests_ = sorted((r for r in rows if _REQUEST_RE.search(r[0])), key=lambda r: -r[1])
    words = Counter(w for r in rows for w in set(re.findall(r"[a-z]{4,}", r[0].lower())) if w not in _STOP)
    return {
        "comments": len(rows), "questions": len(questions),
        "requests": [{"text": r[0][:200], "likes": r[1], "video_id": r[2]} for r in requests_[:10]],
        "top_terms": [{"term": w, "comments": c} for w, c in words.most_common(10) if c >= 3],
    }
