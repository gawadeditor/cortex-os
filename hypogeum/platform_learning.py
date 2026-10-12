"""
hypogeum/platform_learning.py — which platform actually delivers for each channel, how fast a post takes off there, and
which opening patterns do best on each — measured from the day-by-day timelines (hypogeum/platform_timeline_sync.py).

    platform_stats(rows)                    -> per platform: posts, avg/median final views, share of views, engagement, day-0 share
    pattern_by_platform(rows, traits)       -> hook pattern x platform average views (evidence-gated)
    directive_text(stats, patterns)         -> the `platform_playbook` directive (evidence-gated)
    learn_channel_platforms(channel_id)     -> read, derive, write directive            (never raises)

Pure functions over rows; the only I/O is two short reads and one short directive write. The playbook is read by the
writers and the Topic Ideator, and shown on the Analytics page.
"""
from __future__ import annotations

import statistics
from typing import Dict, List, Optional

from hypogeum.logger import get_logger

log = get_logger(agent="platform_learning")

MIN_POSTS_PER_PLATFORM = 5
MIN_PLATFORMS = 2
MIN_PATTERN_POSTS = 3
DIRECTIVE_DIMENSION = "platform_playbook"
_NAMES = {"facebook": "Facebook", "instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube"}


def _final(rows: List[dict]) -> Dict[tuple, dict]:
    """(post, platform) -> final (largest) cumulative numbers + the first-day numbers."""
    by: Dict[tuple, List[dict]] = {}
    for r in rows:
        by.setdefault((r["post_id"], r["platform"]), []).append(r)
    out = {}
    for k, rs in by.items():
        rs.sort(key=lambda r: r["date"])
        last = rs[-1]
        views = max((r["views"] or 0) for r in rs)
        first = rs[0]["views"] or 0
        out[k] = {"post_id": k[0], "platform": k[1], "episode_id": last.get("episode_id"), "views": views, "reach": last.get("reach") or 0,
                  "likes": last.get("likes") or 0, "comments": last.get("comments") or 0, "shares": last.get("shares") or 0,
                  "saves": last.get("saves") or 0, "days": len(rs), "day0_share": (first / views) if views and len(rs) >= 3 else None}
    return out


def platform_stats(rows: List[dict]) -> dict:
    fin = _final(rows)
    by: Dict[str, List[dict]] = {}
    for f in fin.values():
        by.setdefault(f["platform"], []).append(f)
    total = sum(f["views"] for f in fin.values()) or 0
    stats = {}
    for plat, fs in by.items():
        views = [f["views"] for f in fs]
        eng = sum(f["likes"] + f["comments"] + f["shares"] + f["saves"] for f in fs)
        d0 = [f["day0_share"] for f in fs if f["day0_share"] is not None]
        stats[plat] = {"posts": len(fs), "avg_views": round(statistics.mean(views)), "median_views": round(statistics.median(views)),
                       "views": sum(views), "share": round(sum(views) / total, 3) if total else 0.0,
                       "engagement_rate": round(eng / sum(views), 4) if sum(views) else 0.0,
                       "day0_share": round(statistics.mean(d0), 2) if d0 else None}
    return stats


def pattern_by_platform(rows: List[dict], traits: Dict[str, str]) -> Dict[str, Dict[str, dict]]:
    """{platform: {hook_pattern: {n, avg_views}}} for patterns with enough posts on that platform."""
    fin = _final(rows)
    acc: Dict[str, Dict[str, List[int]]] = {}
    for f in fin.values():
        pat = traits.get(f["episode_id"] or "")
        if pat and pat != "none":
            acc.setdefault(f["platform"], {}).setdefault(pat, []).append(f["views"])
    return {plat: {pat: {"n": len(v), "avg_views": round(statistics.mean(v))} for pat, v in pats.items() if len(v) >= MIN_PATTERN_POSTS}
            for plat, pats in acc.items()}


def directive_text(stats: dict, patterns: Optional[dict] = None) -> Optional[str]:
    solid = {p: s for p, s in stats.items() if s["posts"] >= MIN_POSTS_PER_PLATFORM}
    if len(solid) < MIN_PLATFORMS:
        return None
    ranked = sorted(solid.items(), key=lambda kv: -kv[1]["avg_views"])
    parts = []
    for plat, s in ranked:
        bits = [f"{s['avg_views']:,} views per post on average ({s['share'] * 100:.0f}% of all views, {s['posts']} posts)"]
        if s["engagement_rate"]:
            bits.append(f"{s['engagement_rate'] * 100:.1f}% of viewers like/comment/share/save")
        if s["day0_share"] is not None:
            bits.append(f"{s['day0_share'] * 100:.0f}% of a post's views arrive on the first day")
        parts.append(f"{_NAMES.get(plat, plat)}: " + ", ".join(bits))
    top, bottom = ranked[0], ranked[-1]
    text = ("MEASURED PER PLATFORM (day-by-day timelines of this channel's own posts): " + "; ".join(parts) + ". "
            f"{_NAMES.get(top[0], top[0])} is where this channel's audience actually is "
            f"({top[1]['avg_views'] / max(bottom[1]['avg_views'], 1):.1f}x the views per post of {_NAMES.get(bottom[0], bottom[0])}). ")
    for plat, pats in (patterns or {}).items():
        if len(pats) >= 2:
            best = max(pats.items(), key=lambda kv: kv[1]["avg_views"])
            worst = min(pats.items(), key=lambda kv: kv[1]["avg_views"])
            if best[1]["avg_views"] > 1.3 * max(worst[1]["avg_views"], 1):
                text += (f"On {_NAMES.get(plat, plat)} the opening pattern \"{best[0]}\" averages {best[1]['avg_views']:,} views "
                         f"({best[1]['n']} posts) against {worst[1]['avg_views']:,} for \"{worst[0]}\". ")
    return text + "Write for the platform that delivers; these are measured numbers, not assumptions."


def learn_channel_platforms(channel_id: str, *, db_path=None) -> dict:
    out = {"channel_id": channel_id, "posts": 0, "platforms": 0, "directive": None}
    try:
        from hypogeum.channel_learning import get_active_directives, record_directive
        from hypogeum.db import get_connection, initialize_schema
        initialize_schema(db_path)
        conn = get_connection(db_path)
        try:
            rows = [dict(r) for r in conn.execute("SELECT * FROM post_timeline WHERE channel_id = ?", (channel_id,))]
            traits = {r[0]: r[1] for r in conn.execute("SELECT episode_id, hook_pattern FROM episode_traits")}
        finally:
            conn.close()
        stats = platform_stats(rows)
        out["posts"] = sum(s["posts"] for s in stats.values())
        out["platforms"] = len(stats)
        text = directive_text(stats, pattern_by_platform(rows, traits))
        if text:
            active = get_active_directives(channel_id, dimension=DIRECTIVE_DIMENSION, db_path=db_path)
            if not active or active[0]["directive_text"] != text:
                record_directive(channel_id, DIRECTIVE_DIMENSION, "prefer", text, db_path=db_path)
                out["directive"] = "written"
    except Exception as exc:  # noqa: BLE001
        log.error("platform_learning_failed", channel_id=channel_id, error=str(exc)[:200])
    return out
