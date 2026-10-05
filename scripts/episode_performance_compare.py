#!/usr/bin/env python3
"""
scripts/episode_performance_compare.py — how one episode is doing versus the
same channel's earlier episodes AT THE SAME AGE (day 1 / 3 / 7 after its
public release), so an older episode's head start never skews the verdict.

    python3 scripts/episode_performance_compare.py ep_1139264e
    python3 scripts/episode_performance_compare.py ep_1139264e --json

Per platform and in total: views, engagement (likes+comments+shares),
YouTube average-view %, and the episode's rank among the channel's episodes
at that age; plus the hook pattern each episode was written with (the script
package in the archive), so "did the new hook/script work?" is answerable.

Release time = the YouTube cover-hold release (the real public moment) when
there is one, else the earliest Zernio published_at. Snapshots are daily
(platform_analytics, source='api'); "day N" = the first snapshot on or after
release + N days. Read-only.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / ".hypogeum-assets" / "hypogeum.db"
ARCHIVE = Path("/Volumes/Archive/Cortex")
PLATFORMS = ("youtube", "facebook", "instagram", "tiktok")
AGES = (1, 3, 7)


def _p(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _release(c, ep: str):
    try:
        r = c.execute("SELECT release_at FROM youtube_cover_holds WHERE episode_id=? AND status='released'",
                      (ep,)).fetchone()
        if r and r[0]:
            return _p(r[0])
    except sqlite3.OperationalError:
        pass
    r = c.execute("SELECT MIN(published_at) FROM content_grid_cache WHERE episode_id=? AND published_at IS NOT NULL",
                  (ep,)).fetchone()
    return _p(r[0]) if r and r[0] else None


def _hook(channel: str, ep: str):
    for d in sorted((ARCHIVE / channel).glob(f"{ep}*")) if (ARCHIVE / channel).exists() else []:
        for f in d.glob("script_package_*.json"):
            try:
                sn = (json.load(open(f)).get("script_integrity") or {}).get("short_native") or {}
                return sn.get("hook_pattern")
            except Exception:  # noqa: BLE001
                return None
    return None


def _at_age(c, ep: str, rel: datetime, days: int) -> dict:
    target = (rel + timedelta(days=days)).date().isoformat()
    out = {}
    for plat in PLATFORMS:
        r = c.execute("SELECT views, likes, comments, shares, avg_view_pct, snapshot_date FROM platform_analytics "
                      "WHERE source='api' AND episode_id=? AND platform=? AND snapshot_date>=? "
                      "ORDER BY snapshot_date LIMIT 1", (ep, plat, target)).fetchone()
        if r:
            out[plat] = {"views": r["views"] or 0,
                         "eng": (r["likes"] or 0) + (r["comments"] or 0) + (r["shares"] or 0),
                         "avg_view_pct": r["avg_view_pct"]}
    return out


def compare(ep: str) -> dict:
    c = _conn()
    try:
        row = c.execute("SELECT channel_id FROM content_grid_cache WHERE episode_id=? LIMIT 1", (ep,)).fetchone()
        if not row:
            return {"error": f"{ep} has no published post yet"}
        channel = row[0]
        rel = _release(c, ep)
        now = datetime.now(timezone.utc)
        if rel is None or rel > now:
            return {"episode": ep, "channel": channel, "release": rel.isoformat() if rel else None,
                    "note": "not public yet"}
        age = (now - rel).total_seconds() / 86400
        peers = [r[0] for r in c.execute(
            "SELECT DISTINCT episode_id FROM content_grid_cache WHERE channel_id=? AND published_at IS NOT NULL "
            "AND episode_id != ?", (channel, ep))]
        result = {"episode": ep, "channel": channel, "release": rel.isoformat(), "age_days": round(age, 1),
                  "hook_pattern": _hook(channel, ep), "ages": {}}
        for d in AGES:
            if age < d:
                continue
            mine = _at_age(c, ep, rel, d)
            if not mine:
                continue
            tot = sum(v["views"] for v in mine.values())
            peer_rows = []
            for p in peers:
                prel = _release(c, p)
                if prel is None or (now - prel).days < d:
                    continue
                pm = _at_age(c, p, prel, d)
                if pm:
                    peer_rows.append({"ep": p, "views": sum(v["views"] for v in pm.values()),
                                      "yt": pm.get("youtube", {}).get("views", 0),
                                      "eng": sum(v["eng"] for v in pm.values()),
                                      "yt_avp": pm.get("youtube", {}).get("avg_view_pct"),
                                      "hook": _hook(channel, p)})
            views_sorted = sorted([r["views"] for r in peer_rows] + [tot], reverse=True)
            result["ages"][d] = {
                "views": tot, "by_platform": {k: v["views"] for k, v in mine.items()},
                "engagement": sum(v["eng"] for v in mine.values()),
                "yt_avg_view_pct": mine.get("youtube", {}).get("avg_view_pct"),
                "peer_median_views": statistics.median([r["views"] for r in peer_rows]) if peer_rows else None,
                "peer_median_yt": statistics.median([r["yt"] for r in peer_rows]) if peer_rows else None,
                "peer_median_eng": statistics.median([r["eng"] for r in peer_rows]) if peer_rows else None,
                "peer_median_yt_avg_view_pct": statistics.median([r["yt_avp"] for r in peer_rows if r["yt_avp"]])
                if any(r["yt_avp"] for r in peer_rows) else None,
                "rank": f"{views_sorted.index(tot) + 1} of {len(views_sorted)}",
                "same_hook_peers": [r for r in peer_rows if r["hook"] and r["hook"] == result["hook_pattern"]],
            }
        return result
    finally:
        c.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("episode_id")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = compare(a.episode_id)
    if a.json or "ages" not in r:
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
        return 0
    print(f"{r['episode']} ({r['channel']}) — public {r['release'][:16]}Z, age {r['age_days']}d, hook: {r['hook_pattern']}")
    for d, x in r["ages"].items():
        print(f"\nday {d}: views {x['views']} (median of earlier episodes {x['peer_median_views']}) — rank {x['rank']}")
        print(f"  by platform: {x['by_platform']}")
        print(f"  YouTube: {x['by_platform'].get('youtube', 0)} vs median {x['peer_median_yt']}; "
              f"avg view % {x['yt_avg_view_pct']} vs {x['peer_median_yt_avg_view_pct']}")
        print(f"  engagement {x['engagement']} vs median {x['peer_median_eng']}")
        if x["same_hook_peers"]:
            print("  same hook earlier: " + ", ".join(f"{p['ep']} {p['views']}" for p in x["same_hook_peers"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
