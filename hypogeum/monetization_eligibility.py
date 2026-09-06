"""
hypogeum/monetization_eligibility.py — "how far is each channel from
earning money on each platform, and which one number moves it forward?"

Every platform gates payouts behind an audience-size + recent-activity
bar. This module holds those bars and measures each channel's current
standing against them from the analytics we already ingest
(channel_platform_insights, channel_analytics_rollup follower_stats,
platform_analytics). Where we don't yet track a required metric (e.g.
Facebook per-video watch minutes) the requirement is reported as
`current=None` / "not tracked" rather than guessed.

Thresholds are the widely-published public criteria (2024–2025 policy);
they vary by country and change over time — `_note` on each program says
so. Adjust `PLATFORM_REQUIREMENTS` when a platform changes its rules.

Public API:
    compute_channel_monetization(channel_id, *, db_path=None) -> dict
    compute_factory_monetization(*, db_path=None) -> dict
"""
from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="monetization_eligibility")

# Each program: `all_of` requirements must ALL be met; `one_of` (optional)
# means at least ONE of that group must also be met. `key` maps to a
# metric this module knows how to measure for the channel.
PLATFORM_REQUIREMENTS: dict = {
    "youtube": {
        "program": "YouTube Partner Program — ad revenue",
        "note": ("1,000 subscribers plus 4,000 public watch-hours in 12 months "
                 "OR 10M Shorts views in 90 days. An earlier fan-funding tier "
                 "exists at 500 subs + 3,000 watch-hours (or 3M Shorts views). "
                 "Criteria vary by country."),
        "all_of": [
            {"key": "yt_subscribers", "label": "Subscribers", "target": 1000, "unit": "subs"},
        ],
        "one_of": [
            {"key": "yt_watch_hours", "label": "Public watch-hours (rolling year)",
             "target": 4000, "unit": "hours"},
            {"key": "yt_shorts_views_90d", "label": "Shorts views (90 days)",
             "target": 10_000_000, "unit": "views"},
        ],
    },
    "tiktok": {
        "program": "TikTok Creator Rewards Program",
        "note": ("10,000 followers and 100,000 authentic video views in the last "
                 "30 days. Also requires 18+, original content over 1 minute, and "
                 "a supported region."),
        "all_of": [
            {"key": "tt_followers", "label": "Followers", "target": 10000, "unit": "followers"},
            {"key": "tt_views_30d", "label": "Video views (30 days)",
             "target": 100000, "unit": "views"},
        ],
    },
    "instagram": {
        "program": "Instagram monetization (Subscriptions / Gifts / bonuses)",
        "note": ("Subscriptions require roughly 10,000 followers; Gifts on Reels "
                 "require an account in good standing. Bonus programs are "
                 "invite-only and not guaranteed at any follower count."),
        "all_of": [
            {"key": "ig_followers", "label": "Followers", "target": 10000, "unit": "followers"},
        ],
    },
    "facebook": {
        "program": "Facebook Content Monetization / In-Stream Ads",
        "note": ("Around 10,000 followers plus 60,000 watch-minutes in 60 days "
                 "(or 5+ active videos). We do not yet track Facebook per-video "
                 "watch time, so only the follower bar is measured here."),
        "all_of": [
            {"key": "fb_followers", "label": "Followers", "target": 10000, "unit": "followers"},
        ],
    },
}


def _conn(db_path):
    from hypogeum.db import get_connection
    return get_connection(Path(db_path) if db_path else None)


def _latest_follower_stats(channel_id: str, conn) -> dict:
    """{platform: followers} from the channel-scoped follower_stats rollup,
    falling back to channel_platform_insights."""
    out: dict = {}
    try:
        row = conn.execute(
            "SELECT payload_json FROM channel_analytics_rollup "
            "WHERE scope=? AND metric_kind='follower_stats' "
            "ORDER BY snapshot_date DESC LIMIT 1", (channel_id,),
        ).fetchone()
        if row:
            for a in (_json.loads(row["payload_json"]).get("accounts") or []):
                p = (a.get("platform") or "").lower()
                f = a.get("followers")
                if p and isinstance(f, (int, float)):
                    out[p] = max(out.get(p, 0), int(f))
    except Exception:  # noqa: BLE001
        pass
    # fill gaps from channel_platform_insights
    try:
        for r in conn.execute(
            "SELECT platform, metrics_json FROM channel_platform_insights WHERE channel_id=?",
            (channel_id,),
        ).fetchall():
            m = _json.loads(r["metrics_json"])
            f = m.get("follower_count")
            if f is None:
                f = m.get("page_follows")
            if isinstance(f, (int, float)) and r["platform"] not in out:
                out[r["platform"]] = int(f)
    except Exception:  # noqa: BLE001
        pass
    return out


def _platform_insight(channel_id: str, platform: str, conn) -> dict:
    try:
        r = conn.execute(
            "SELECT metrics_json FROM channel_platform_insights "
            "WHERE channel_id=? AND platform=? ORDER BY snapshot_date DESC LIMIT 1",
            (channel_id, platform),
        ).fetchone()
        return _json.loads(r["metrics_json"]) if r else {}
    except Exception:  # noqa: BLE001
        return {}


def _views_last_days(channel_id: str, platform: str, days: int, conn) -> Optional[int]:
    """Sum of the latest per-episode view count for this channel+platform,
    for episodes published within `days`. None if we have no api rows."""
    try:
        rows = conn.execute(
            "SELECT MAX(pa.views) AS v FROM platform_analytics pa "
            "WHERE pa.platform=? AND pa.source='api' AND pa.views IS NOT NULL "
            "AND pa.episode_id IN ("
            "  SELECT et.episode_id FROM episode_topics et WHERE et.channel_id=?"
            "  AND (et.target_publish_at IS NULL OR et.target_publish_at >= date('now', ?))"
            ") GROUP BY pa.episode_id",
            (platform, channel_id, f"-{int(days)} days"),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    return int(sum((r["v"] or 0) for r in rows))


def _measure(channel_id: str, conn) -> dict:
    """{metric_key: value_or_None} for every key referenced in
    PLATFORM_REQUIREMENTS."""
    fs = _latest_follower_stats(channel_id, conn)
    yt = _platform_insight(channel_id, "youtube", conn)

    yt_subs = fs.get("youtube")
    yt_minutes = yt.get("estimatedMinutesWatched")
    yt_watch_hours = round(yt_minutes / 60.0, 1) if isinstance(yt_minutes, (int, float)) else None
    yt_shorts_views = yt.get("views")
    if yt_shorts_views is None:
        yt_shorts_views = _views_last_days(channel_id, "youtube", 90, conn)

    return {
        "yt_subscribers": yt_subs,
        "yt_watch_hours": yt_watch_hours,
        "yt_shorts_views_90d": yt_shorts_views if isinstance(yt_shorts_views, (int, float)) else None,
        "tt_followers": fs.get("tiktok"),
        "tt_views_30d": _views_last_days(channel_id, "tiktok", 30, conn),
        "ig_followers": fs.get("instagram"),
        "fb_followers": fs.get("facebook"),
    }


def _eval_req(req: dict, value) -> dict:
    target = req["target"]
    met = value is not None and value >= target
    pct = None
    if value is not None and target:
        pct = round(min(value / target, 1.0) * 100, 1)
    return {
        "key": req["key"], "label": req["label"], "unit": req["unit"],
        "target": target, "current": value, "pct": pct, "met": met,
        "tracked": value is not None,
        "remaining": (max(target - value, 0) if value is not None else None),
    }


def compute_channel_monetization(channel_id: str, *,
                                 db_path: Optional[Path] = None) -> dict:
    """Per-platform monetization standing for one channel:
        {platform: {program, note, eligible, requirements: [...],
                    headline: "<the single number to move next>"}}
    Never raises."""
    out: dict = {"channel_id": channel_id, "platforms": {}}
    try:
        conn = _conn(db_path)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
        return out
    try:
        metrics = _measure(channel_id, conn)
    finally:
        conn.close()

    for platform, spec in PLATFORM_REQUIREMENTS.items():
        all_reqs = [_eval_req(r, metrics.get(r["key"])) for r in spec.get("all_of", [])]
        one_reqs = [_eval_req(r, metrics.get(r["key"])) for r in spec.get("one_of", [])]
        all_met = all(r["met"] for r in all_reqs) if all_reqs else True
        one_met = (any(r["met"] for r in one_reqs)) if one_reqs else True
        eligible = all_met and one_met

        # headline gap: the unmet requirement closest to done (highest pct),
        # preferring the mandatory ones; for the one_of group, the single
        # closest option.
        candidates = [r for r in all_reqs if not r["met"] and r["tracked"]]
        if one_reqs and not one_met:
            closest_one = max((r for r in one_reqs if r["tracked"]),
                              key=lambda r: r["pct"] or 0, default=None)
            if closest_one:
                candidates.append(closest_one)
        headline = None
        if eligible:
            headline = "Eligible — all criteria met."
        elif candidates:
            g = max(candidates, key=lambda r: r["pct"] or 0)
            headline = (f"{g['current']:,} / {g['target']:,} {g['unit']} "
                        f"— {g['remaining']:,} to go ({g['label']}).")
        else:
            headline = "Not enough data yet to measure the gap."

        out["platforms"][platform] = {
            "program": spec["program"], "note": spec["note"],
            "eligible": eligible,
            "requirements": all_reqs + [{**r, "one_of": True} for r in one_reqs],
            "headline": headline,
        }
    return out


def compute_factory_monetization(*, db_path: Optional[Path] = None) -> dict:
    """Every channel's standing, plus a factory count of how many
    (channel, platform) pairs are eligible."""
    from hypogeum.channel_config import list_channel_profiles
    try:
        channels = [c.channel_id for c in list_channel_profiles(db_path=db_path)]
    except Exception:  # noqa: BLE001
        channels = []
    per_channel = {cid: compute_channel_monetization(cid, db_path=db_path)
                   for cid in channels}
    eligible_pairs = sum(
        1 for c in per_channel.values()
        for p in c.get("platforms", {}).values() if p.get("eligible")
    )
    total_pairs = sum(len(c.get("platforms", {})) for c in per_channel.values())
    return {"channels": per_channel,
            "eligible_pairs": eligible_pairs, "total_pairs": total_pairs}
