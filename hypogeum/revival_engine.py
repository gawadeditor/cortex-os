"""
hypogeum/revival_engine.py — diagnose WHY a channel is stuck and where its hidden wins are.

Step 1 of the Revival Engine (diagnose -> prescribe -> execute -> measure). Same honesty
discipline as hypogeum.client_advisory: deterministic, no LLM, only real (source='api')
numbers, a finding is reported only when the sample supports it, and every layer the
platforms do NOT give us is listed as a blind spot instead of being guessed.

Public API:
    diagnose_channel(channel_id, *, db_path=None) -> dict
      {
        ok, reason, channel_id,
        platforms: {platform: {videos, layers: [layer...], primary_leak}},
        buried_gems: [gem...],      # strong retention, starved distribution
        source_contrast: [...],     # YouTube: a traffic source whose viewers watch far more
        blind_spots: [str],
      }
    A layer is {layer, status: ok|weak|blind|insufficient, finding, evidence, confidence}.

Layers, in the order a viewer meets them: distribution -> hook -> retention -> conversion.
Percentile ranks are used wherever scales differ per platform, so no platform-specific
absolute threshold is hard-coded except the clearly named, heuristic constants below.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from hypogeum.logger import get_logger

log = get_logger(agent="revival_engine")

_MIN_VIDEOS = 6              # below this a ranking says nothing
_GEM_RETENTION_PCTL = 0.70   # top 30% retention ...
_GEM_REACH_PCTL = 0.40       # ... but bottom 40% reach
_GEM_MIN_AGE_DAYS = 3        # younger posts have not finished being distributed
_MIN_TREND_SPREAD_DAYS = 7
_SKIP_WEAK = 70.0            # IG skip_rate % — heuristic from public Reels guidance, not a platform fact
_CONVERT_WEAK = 0.005        # follows per reached account below which reach is not turning into audience
_SOURCE_MIN_VIEWS = 50
_SOURCE_MIN_LIFT = 1.2       # a source's avg_view_pct vs the dominant source's
_SOURCE_MAX_SHARE = 0.15
_DOMINANT_SHARE = 0.90


def _num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _median(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _pctl(sorted_vals: List[float], v: float) -> float:
    """Fraction of values strictly below v (0..1)."""
    if not sorted_vals:
        return 0.0
    return sum(1 for x in sorted_vals if x < v) / len(sorted_vals)


def _conf(n: int) -> str:
    return "high" if n >= 20 else "medium" if n >= _MIN_VIDEOS * 2 else "low"


def _layer(layer: str, status: str, finding: str, evidence: dict, n: int) -> dict:
    return {"layer": layer, "status": status, "finding": finding,
            "evidence": evidence, "confidence": _conf(n)}


def _age_days(published_at: Optional[str], now: datetime) -> Optional[float]:
    if not published_at:
        return None
    try:
        dt = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds() / 86400
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def _load_units(conn, channel_id: str) -> Dict[str, List[dict]]:
    """{platform: [one unit per (episode, platform): its latest real snapshot]}."""
    from hypogeum.episode_channel_resolver import build_episode_channel_map

    chan = build_episode_channel_map(conn)
    pub = {r[0]: r[1] for r in conn.execute("SELECT episode_id, published_at FROM episodes")}
    rows = conn.execute(
        "SELECT episode_id, platform, snapshot_date, window_days, views, reach, impressions, ctr, "
        "avg_view_pct, avg_watch_time_s, shares, saves, follows, skip_rate, retention_curve_json "
        "FROM platform_analytics WHERE source='api' AND client_id IS NULL "
        "ORDER BY snapshot_date, window_days").fetchall()
    latest: Dict[tuple, dict] = {}
    for r in rows:
        if chan.get(r[0]) != channel_id:
            continue
        fresh = {
            "views": _num(r[4]), "reach": _num(r[5]), "impressions": _num(r[6]), "ctr": _num(r[7]),
            "avg_view_pct": _num(r[8]), "avg_watch_time_s": _num(r[9]), "shares": _num(r[10]),
            "saves": _num(r[11]), "follows": _num(r[12]), "skip_rate": _num(r[13]),
            "curve": r[14] or None,
        }
        u = latest.setdefault((r[0], r[1]), {
            "episode_id": r[0], "platform": r[1], "published_at": pub.get(r[0]),
            **{k: None for k in fresh}})
        # rows ascend by date: a newer snapshot that lacks a field (a 7-day row often has no
        # retention) must not erase an older one that has it. Cumulative counts keep their max.
        for k, v in fresh.items():
            if v is None:
                continue
            u[k] = max(u[k], v) if k in ("views", "reach") and u[k] is not None else v
    by_platform: Dict[str, List[dict]] = {}
    for u in latest.values():
        by_platform.setdefault(u["platform"], []).append(u)
    return by_platform


def _followers_gained(conn, channel_id: str) -> Dict[str, Optional[float]]:
    """{platform: followers gained over all stored daily snapshots, None if never reported}."""
    out: Dict[str, Optional[float]] = {}
    for platform, total, seen in conn.execute(
            "SELECT platform, SUM(json_extract(metrics_json,'$.followers_gained')), "
            "COUNT(json_extract(metrics_json,'$.followers_gained')) "
            "FROM channel_platform_insights WHERE channel_id=? GROUP BY platform", (channel_id,)):
        out[platform] = float(total or 0) if seen else None
    return out


def _reach_of(u: dict) -> Optional[float]:
    return u["reach"] if u["reach"] else u["views"]


# ---------------------------------------------------------------------------
# layers
# ---------------------------------------------------------------------------

def _curve_points(curve_json) -> List[tuple]:
    try:
        pts = json.loads(curve_json) if curve_json else []
        return sorted((float(p["elapsedVideoTimeRatio"]), float(p["audienceWatchRatio"]))
                      for p in pts if "elapsedVideoTimeRatio" in p and "audienceWatchRatio" in p)
    except Exception:  # noqa: BLE001
        return []


def _watch_ratio_at(points: List[tuple], t: float) -> Optional[float]:
    if not points:
        return None
    return min(points, key=lambda p: abs(p[0] - t))[1]


def _distribution_layer(units: List[dict], now: datetime) -> dict:
    reach = [_reach_of(u) for u in units if _reach_of(u) is not None]
    n = len(reach)
    if n < _MIN_VIDEOS:
        return _layer("distribution", "insufficient", f"only {n} measured posts", {"posts": n}, n)
    dated = sorted((u for u in units if _age_days(u["published_at"], now) is not None and _reach_of(u)),
                   key=lambda u: _age_days(u["published_at"], now))
    ev = {"median_reach": _median(reach), "posts": n}
    spread = (_age_days(dated[-1]["published_at"], now) - _age_days(dated[0]["published_at"], now)) if dated else 0
    if len(dated) >= _MIN_VIDEOS and spread >= _MIN_TREND_SPREAD_DAYS:  # same-day posts say nothing about a trend
        third = max(2, len(dated) // 3)
        newest = _median([_reach_of(u) / max(_age_days(u["published_at"], now), 1) for u in dated[:third]])
        oldest = _median([_reach_of(u) / max(_age_days(u["published_at"], now), 1) for u in dated[-third:]])
        ev.update(newest_reach_per_day=newest, oldest_reach_per_day=oldest)
        if oldest and newest is not None and newest < 0.5 * oldest:
            return _layer("distribution", "weak",
                          "recent posts are reaching less than half the daily audience older posts did",
                          ev, n)
    return _layer("distribution", "ok", "reach is not shrinking across posts", ev, n)


def _hook_layer(platform: str, units: List[dict]) -> dict:
    skips = [u["skip_rate"] for u in units if u["skip_rate"] is not None]
    if skips:
        med = _median(skips)
        st = "weak" if med >= _SKIP_WEAK else "ok"
        return _layer("hook", st,
                      f"median {med:.0f}% of viewers scroll away early" if st == "weak"
                      else f"median early scroll-away is {med:.0f}%",
                      {"median_skip_rate_pct": med, "posts": len(skips)}, len(skips))
    ctrs = [u["ctr"] for u in units if u["ctr"]]
    if ctrs:
        return _layer("hook", "ok", f"median CTR {_median(ctrs):.1%}", {"median_ctr": _median(ctrs)}, len(ctrs))
    return _layer("hook", "blind",
                  f"{platform} gives us neither click-through nor early-swipe data, so the hook cannot be judged yet",
                  {}, 0)


def _retention_layer(units: List[dict]) -> dict:
    pcts = [u["avg_view_pct"] for u in units if u["avg_view_pct"]]
    if len(pcts) < _MIN_VIDEOS:
        return _layer("retention", "blind" if not pcts else "insufficient",
                      "no average-view data" if not pcts else f"only {len(pcts)} posts with retention",
                      {"posts": len(pcts)}, len(pcts))
    ev = {"median_avg_view_pct": _median(pcts), "posts": len(pcts)}
    early, late = [], []
    for u in units:
        pts = _curve_points(u["curve"])
        a, b = _watch_ratio_at(pts, 0.10), _watch_ratio_at(pts, 0.75)
        if a and b:
            early.append(a)
            late.append(b)
    if len(early) >= 3:
        ev.update(median_watching_at_10pct=_median(early), median_watching_at_75pct=_median(late))
        if _median(late) < 0.5 * _median(early):
            return _layer("retention", "weak", "more than half of the audience that stays past the opening still leaves before the end",
                          ev, len(pcts))
    return _layer("retention", "ok", "viewers who start mostly keep watching", ev, len(pcts))


def _conversion_layer(units: List[dict], gained: Optional[float]) -> dict:
    reach_sum = sum(_reach_of(u) or 0 for u in units)
    if gained is None:
        return _layer("conversion", "blind", "followers gained is not reported for this platform",
                      {"reach": reach_sum}, len(units))
    if reach_sum < 500:
        return _layer("conversion", "insufficient", "not enough reach yet to judge follower conversion",
                      {"reach": reach_sum}, len(units))
    rate = gained / reach_sum
    ev = {"followers_gained": gained, "follows_per_reached_account": round(rate, 5), "reach": reach_sum}
    if rate < _CONVERT_WEAK:
        return _layer("conversion", "weak",
                      f"{reach_sum:,.0f} people reached, only {gained:,.0f} became followers", ev, len(units))
    return _layer("conversion", "ok", "reach is turning into audience", ev, len(units))


# ---------------------------------------------------------------------------
# hidden wins
# ---------------------------------------------------------------------------

def _buried_gems(platform: str, units: List[dict], now: datetime) -> List[dict]:
    scored = [u for u in units if u["avg_view_pct"] and _reach_of(u)]
    if len(scored) < _MIN_VIDEOS:
        return []
    ret_sorted = sorted(u["avg_view_pct"] for u in scored)
    reach_sorted = sorted(_reach_of(u) for u in scored)
    out = []
    for u in scored:
        age = _age_days(u["published_at"], now)
        if age is not None and age < _GEM_MIN_AGE_DAYS:
            continue
        rp, dp = _pctl(ret_sorted, u["avg_view_pct"]), _pctl(reach_sorted, _reach_of(u))
        if rp >= _GEM_RETENTION_PCTL and dp <= _GEM_REACH_PCTL:
            out.append({
                "episode_id": u["episode_id"], "platform": platform,
                "reach": _reach_of(u), "avg_view_pct": u["avg_view_pct"],
                "retention_percentile": round(rp, 2), "reach_percentile": round(dp, 2),
                "why": "viewers who see it watch more than on most posts, but it was shown to few",
                "suggested_action": "repackage (new cover + title/caption) and re-post, or re-cut its strongest moment as a Short",
            })
    return sorted(out, key=lambda g: g["retention_percentile"] - g["reach_percentile"], reverse=True)


def _source_contrast(conn, channel_id: str) -> List[dict]:
    row = conn.execute("SELECT MAX(snapshot_date) FROM youtube_traffic_insights WHERE channel_id=?",
                       (channel_id,)).fetchone()
    if not row or not row[0]:
        return []
    srcs = conn.execute(
        "SELECT key, views, avg_view_pct FROM youtube_traffic_insights "
        "WHERE channel_id=? AND snapshot_date=? AND dimension='traffic_type' AND views>0",
        (channel_id, row[0])).fetchall()
    total = sum(s[1] for s in srcs)
    if not total:
        return []
    top = max(srcs, key=lambda s: s[1])
    if top[1] / total < _DOMINANT_SHARE or not top[2]:
        return []
    out = []
    for key, views, pct in srcs:
        if key == top[0] or views < _SOURCE_MIN_VIEWS or not pct:
            continue
        if pct >= top[2] * _SOURCE_MIN_LIFT and views / total <= _SOURCE_MAX_SHARE:
            out.append({
                "source": key, "views": views, "share": round(views / total, 3),
                "avg_view_pct": pct, "dominant_source": top[0], "dominant_avg_view_pct": top[2],
                "finding": f"{key} viewers watch {pct:.0f}% vs {top[2]:.0f}% from {top[0]}, yet are only "
                           f"{views / total:.0%} of views — the intent is there, the volume is not",
            })
    return out


_PACKAGING_MIN_IMPRESSIONS = 500
_PACKAGING_WEAK_RATIO = 0.6   # a video whose CTR is under 60% of the channel's own weighted CTR


def _youtube_packaging(conn, channel_id: str):
    """(hook layer, weak-packaging candidates) from the Reporting API reach data, or (None, [])."""
    from hypogeum.youtube_reach import channel_ctr

    data = channel_ctr(conn, channel_id)
    if not data:
        return None, []
    ep_of = {r[0]: r[1] for r in conn.execute(
        "SELECT platform_video_id, episode_id FROM platform_analytics WHERE platform='youtube' AND platform_video_id IS NOT NULL")}
    cands = [{"video_id": v["video_id"], "episode_id": ep_of.get(v["video_id"]), "impressions": v["impressions"],
              "ctr": round(v["ctr"], 4), "channel_ctr": round(data["ctr"], 4),
              "suggested_action": "new thumbnail + title (A/B against the current one)"}
             for v in data["videos"]
             if v["impressions"] >= _PACKAGING_MIN_IMPRESSIONS and v["ctr"] is not None
             and v["ctr"] < _PACKAGING_WEAK_RATIO * data["ctr"]]
    n = len(data["videos"])
    layer = _layer("hook", "weak" if cands else "ok",
                   (f"{len(cands)} videos are shown often but clicked far less than this channel's own average ({data['ctr']:.1%})"
                    if cands else f"click-through is {data['ctr']:.1%} across {data['impressions']:,} impressions"),
                   {"ctr": data["ctr"], "impressions": data["impressions"], "videos": n}, n)
    return layer, cands


_PLATFORM_BLINDS = {
    "youtube": "YouTube: click-through comes from the Reporting API (hypogeum/youtube_reach.py) and has not arrived yet — "
               "the first report can take ~2 days after the job is created, and the Shorts feed has no thumbnail to click, "
               "so a Shorts-only channel may never get this number",
    "tiktok": "TikTok: only views are collected — no reach, retention or follows per post",
}


def diagnose_channel(channel_id: str, *, db_path: Optional[Path] = None) -> dict:
    """Layered diagnosis + buried gems for one channel. Never raises."""
    from hypogeum.db import get_connection

    now = datetime.now(timezone.utc)
    try:
        with get_connection(db_path) as conn:
            by_platform = _load_units(conn, channel_id)
            contrast = _source_contrast(conn, channel_id)
            gained = _followers_gained(conn, channel_id)
            yt_hook, packaging = _youtube_packaging(conn, channel_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("revival diagnose failed", extra={"channel_id": channel_id, "error": str(exc)})
        return {"ok": False, "reason": f"could not read analytics: {exc}", "channel_id": channel_id}

    if not by_platform:
        return {"ok": False, "reason": "no real analytics for this channel yet", "channel_id": channel_id}

    platforms, gems, blinds = {}, [], []
    for platform, units in sorted(by_platform.items()):
        layers = [
            _distribution_layer(units, now),
            _hook_layer(platform, units),
            _retention_layer(units),
            _conversion_layer(units, gained.get(platform)),
        ]
        if platform == "youtube" and yt_hook:
            layers[1] = yt_hook
        weak = [l for l in layers if l["status"] == "weak"]
        platforms[platform] = {
            "videos": len(units), "layers": layers,
            "primary_leak": weak[0]["layer"] if weak else None,
        }
        gems += _buried_gems(platform, units, now)
        if any(l["status"] == "blind" for l in layers) and platform in _PLATFORM_BLINDS and not (platform == "youtube" and yt_hook):
            blinds.append(_PLATFORM_BLINDS[platform])

    return {"ok": True, "reason": "", "channel_id": channel_id, "platforms": platforms,
            "buried_gems": gems, "packaging_candidates": packaging, "source_contrast": contrast, "blind_spots": blinds}
