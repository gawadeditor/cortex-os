"""
hypogeum/youtube_traffic.py — where YouTube views COME FROM, and what viewers searched, learned automatically.

The platform numbers the factory already keeps say how many views and how long people watch. They do not say how
viewers FOUND the video. YouTube's own Analytics API does (traffic-source types, the exact search terms behind
YT_SEARCH views, external referrers, related-video referrers, subscribed vs not, device). Zernio does not proxy
these, so this module calls the YouTube Analytics API directly with the channel's own OAuth token
(`youtube-oauth-refresh:<channel_id>`, scope yt-analytics.readonly), stores one snapshot per channel per day in
`youtube_traffic_insights`, and turns it into measured, evidence-gated guidance:

    sync_channel_traffic(channel_id)      -> fetch + store today's snapshot        (network, read-only)
    derive_findings(rows)                 -> source mix, search demand, retention gap (pure)
    learn_channel_traffic(channel_id)     -> sync, derive, write a `search_demand` directive      (never raises)

The `search_demand` directive is read by the Topic Ideator (topics people actually search for) and the canonical
metadata agent (the exact phrases real viewers typed). First real data (hypogeum, 2026-10): 90% of views came from
the Shorts feed (avg 70% watched) while the ~4% that came from search watched 96% — search-led topics retain best.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from hypogeum.logger import get_logger

log = get_logger(agent="youtube_traffic")

_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
_TIMEOUT = 30
WINDOW_DAYS = 89                      # the API's own limit for most reports
MIN_SEARCH_VIEWS_TOTAL = 12           # evidence gate for a search_demand directive
MIN_TERMS = 3
DIRECTIVE_DIMENSION = "search_demand"
_METRICS = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _report(token: str, params: dict) -> Optional[list]:
    """One Analytics API call; the rows or None on any failure (logged, never raised)."""
    r = None
    for attempt in range(3):                      # Google's reports API returns transient 5xx now and then
        try:
            r = requests.get(_URL, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            log.warn("youtube_traffic_network_error", error=str(exc)[:160])
            return None
        if r.status_code < 500:
            break
        time.sleep(1.5 * (attempt + 1))
    if r is None or r.status_code != 200:
        log.warn("youtube_traffic_report_failed", status=getattr(r, "status_code", None), body=(r.text[:160] if r is not None else ""))
        return None
    try:
        return r.json().get("rows") or []
    except ValueError:
        return None


def _queries(start: str, end: str) -> List[tuple]:
    base = {"ids": "channel==MINE", "startDate": start, "endDate": end}
    m = _METRICS
    return [
        ("traffic_type", {**base, "metrics": m, "dimensions": "insightTrafficSourceType", "sort": "-views"}),
        ("search_term", {**base, "metrics": m, "dimensions": "insightTrafficSourceDetail",
                         "filters": "insightTrafficSourceType==YT_SEARCH", "sort": "-views", "maxResults": 25}),
        ("ext_url", {**base, "metrics": m, "dimensions": "insightTrafficSourceDetail",
                     "filters": "insightTrafficSourceType==EXT_URL", "sort": "-views", "maxResults": 25}),
        ("related_video", {**base, "metrics": m, "dimensions": "insightTrafficSourceDetail",
                           "filters": "insightTrafficSourceType==RELATED_VIDEO", "sort": "-views", "maxResults": 25}),
        ("subscribed", {**base, "metrics": m, "dimensions": "subscribedStatus", "sort": "-views"}),
        ("device", {**base, "metrics": m, "dimensions": "deviceType", "sort": "-views"}),
    ]


def _row(dimension: str, r: list) -> dict:
    """Analytics rows are [dims..., views, minutes, avgDuration, avgPct]."""
    key, subkey, nums = str(r[0]), "", r[1:]
    nums = (list(nums) + [None] * 4)[:4]
    return {"dimension": dimension, "key": key, "subkey": subkey, "views": int(nums[0] or 0),
            "minutes_watched": float(nums[1] or 0), "avg_view_duration_s": float(nums[2] or 0),
            "avg_view_pct": float(nums[3] or 0)}


def fetch_channel_traffic(channel_id: str, *, db_path=None, token: Optional[str] = None) -> Optional[List[dict]]:
    """Fetch every traffic report for the channel. None when the channel has no usable authorization."""
    if token is None:
        try:
            from hypogeum.publishing.youtube_privacy import _access_token
            token = _access_token(channel_id, db_path)
        except Exception as exc:  # noqa: BLE001 -- no/dead authorization is a normal state, reported by the caller
            log.warn("youtube_traffic_no_token", channel_id=channel_id, error=str(exc)[:160])
            return None
    today = datetime.now(timezone.utc).date()
    start, end = (today - timedelta(days=WINDOW_DAYS)).isoformat(), (today - timedelta(days=1)).isoformat()
    out: List[dict] = []
    for dimension, params in _queries(start, end):
        rows = _report(token, params)
        for r in rows or []:
            try:
                out.append(_row(dimension, r))
            except (TypeError, ValueError, IndexError):
                continue
    return out


def store_snapshot(conn, channel_id: str, rows: List[dict], *, snapshot_date: Optional[str] = None) -> int:
    snap = snapshot_date or datetime.now(timezone.utc).date().isoformat()
    conn.execute("DELETE FROM youtube_traffic_insights WHERE channel_id = ? AND snapshot_date = ?", (channel_id, snap))
    for r in rows:
        conn.execute(
            "INSERT OR REPLACE INTO youtube_traffic_insights (channel_id, snapshot_date, dimension, key, subkey, views, "
            "minutes_watched, avg_view_duration_s, avg_view_pct, window_days, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (channel_id, snap, r["dimension"], r["key"], r["subkey"], r["views"], r["minutes_watched"],
             r["avg_view_duration_s"], r["avg_view_pct"], WINDOW_DAYS, _now()))
    return len(rows)


def _latest_rows(conn, channel_id: str) -> List[dict]:
    snap = conn.execute("SELECT MAX(snapshot_date) FROM youtube_traffic_insights WHERE channel_id = ?", (channel_id,)).fetchone()[0]
    if not snap:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM youtube_traffic_insights WHERE channel_id = ? AND snapshot_date = ?", (channel_id, snap))]


# ---------------------------------------------------------------------------
# findings (pure)
# ---------------------------------------------------------------------------

_PRETTY = {"SHORTS": "the Shorts feed", "YT_SEARCH": "YouTube search", "YT_OTHER_PAGE": "other YouTube pages",
           "YT_CHANNEL": "channel pages", "SUBSCRIBER": "subscribers' feeds", "RELATED_VIDEO": "suggested videos",
           "EXT_URL": "external websites", "NOTIFICATION": "notifications", "PLAYLIST": "playlists",
           "SOUND_PAGE": "the sound page", "END_SCREEN": "end screens"}


def _brand_tokens(channel_id: str) -> set:
    toks = {t for t in channel_id.lower().replace("_", " ").split() if len(t) > 3}
    return toks | {"hypogeum", "twinkle"}


def derive_findings(rows: List[dict], channel_id: str = "") -> dict:
    types = sorted((r for r in rows if r["dimension"] == "traffic_type"), key=lambda r: -r["views"])
    total = sum(r["views"] for r in types)
    mix = [{"source": r["key"], "share": round(r["views"] / total, 3), "views": r["views"],
            "avg_view_pct": r["avg_view_pct"], "avg_view_duration_s": r["avg_view_duration_s"]}
           for r in types if total and r["views"]]
    brands = _brand_tokens(channel_id)
    terms = [r for r in rows if r["dimension"] == "search_term" and r["views"] >= 2 and r["key"] and r["avg_view_pct"] > 0]
    unbranded = [r for r in terms if not any(b in r["key"].lower() for b in brands)]
    search = next((m for m in mix if m["source"] == "YT_SEARCH"), None)
    feed = next((m for m in mix if m["source"] == "SHORTS"), None)
    return {"total_views": total, "mix": mix, "search_terms": unbranded, "brand_terms": [r for r in terms if r not in unbranded],
            "search_share": search["share"] if search else 0.0,
            "retention_gap": (round(search["avg_view_pct"] - feed["avg_view_pct"], 1) if search and feed else None),
            "search_total_views": sum(r["views"] for r in terms)}


def directive_text(f: dict) -> Optional[str]:
    """Evidence-gated: needs real search demand, not a couple of stray queries."""
    terms = f["search_terms"]
    if len(terms) < MIN_TERMS or f["search_total_views"] < MIN_SEARCH_VIEWS_TOTAL:
        return None
    best = sorted(terms, key=lambda r: (-r["avg_view_pct"] if r["views"] >= 3 else 0, -r["views"]))[:8]
    listing = "; ".join(f'"{r["key"]}" ({r["views"]} views, {min(r["avg_view_pct"], 100):.0f}% watched)' for r in best)
    mix = ", ".join(f'{_PRETTY.get(m["source"], m["source"])} {m["share"] * 100:.0f}% ({m["avg_view_pct"]:.0f}% watched)'
                    for m in f["mix"][:4])
    gap = ""
    if f["retention_gap"] is not None and f["retention_gap"] > 5:
        gap = (f" People who arrive by search watch {f['retention_gap']:.0f} points more of the video than people "
               f"in the Shorts feed, so topics people actively search for retain best.")
    return (f"MEASURED FROM YOUTUBE (last {WINDOW_DAYS} days): where this channel's views come from — {mix}. Real viewers "
            f"found the videos by typing: {listing}.{gap} Prefer topics that match what people really search, and when "
            f"it is truthful use these exact phrases in titles, descriptions and tags. Do not invent search demand: "
            f"these are measured queries, not guesses.")


def search_terms_for_metadata(channel_id: str, *, db_path=None, limit: int = 10) -> List[dict]:
    """Measured viewer search terms for the metadata agent: [{term, views, avg_view_pct}], best first. [] when none."""
    try:
        from hypogeum.db import get_connection, initialize_schema
        initialize_schema(db_path)
        conn = get_connection(db_path)
        try:
            f = derive_findings(_latest_rows(conn, channel_id), channel_id)
        finally:
            conn.close()
        ts = sorted(f["search_terms"], key=lambda r: (-r["views"], -r["avg_view_pct"]))[:limit]
        return [{"term": r["key"], "views": r["views"], "avg_view_pct": round(min(r["avg_view_pct"], 100), 1)} for r in ts]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def learn_channel_traffic(channel_id: str, *, db_path=None, force: bool = False, token: Optional[str] = None) -> dict:
    """Fetch (at most once per day unless forced), store, derive, write the directive. Never raises."""
    out = {"channel_id": channel_id, "fetched": False, "rows": 0, "directive": None, "error": None}
    try:
        from hypogeum.channel_learning import get_active_directives, record_directive
        from hypogeum.db import get_connection, initialize_schema
        initialize_schema(db_path)
        today = datetime.now(timezone.utc).date().isoformat()
        conn = get_connection(db_path)
        try:
            have_today = conn.execute("SELECT 1 FROM youtube_traffic_insights WHERE channel_id = ? AND snapshot_date = ? LIMIT 1",
                                      (channel_id, today)).fetchone()
        finally:
            conn.close()
        if force or not have_today:
            rows = fetch_channel_traffic(channel_id, db_path=db_path, token=token)
            if rows is None:
                out["error"] = "no usable YouTube authorization for this channel"
                return out
            conn = get_connection(db_path)
            try:
                out["rows"] = store_snapshot(conn, channel_id, rows)
                conn.commit()
            finally:
                conn.close()
            out["fetched"] = True
        conn = get_connection(db_path)
        try:
            f = derive_findings(_latest_rows(conn, channel_id), channel_id)
        finally:
            conn.close()
        text = directive_text(f)
        if text:
            active = get_active_directives(channel_id, dimension=DIRECTIVE_DIMENSION, db_path=db_path)
            if not active or active[0]["directive_text"] != text:
                record_directive(channel_id, DIRECTIVE_DIMENSION, "prefer", text, db_path=db_path)
                out["directive"] = "written"
    except Exception as exc:  # noqa: BLE001 -- learning must never break the worker
        out["error"] = str(exc)[:200]
        log.error("youtube_traffic_failed", channel_id=channel_id, error=out["error"])
    return out
