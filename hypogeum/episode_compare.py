"""
hypogeum/episode_compare.py — how one episode is doing versus the same
channel's earlier episodes AT THE SAME AGE (day 1 / 3 / 7 after its public
release), so an older episode's head start never skews the verdict. Shown on
the episode page and by scripts/episode_performance_compare.py.

Release time = the YouTube cover-hold release (the real public moment) when
there is one, else the earliest Zernio published_at. Snapshots are daily
(platform_analytics, source='api'); "day N" = the first snapshot on or after
release + N days. Read-only.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

ARCHIVE = Path("/Volumes/Archive/Cortex")
def _default_db():
    import hypogeum.db as _db
    return _db._DEFAULT_DB_PATH


PLATFORMS = ("youtube", "facebook", "instagram", "tiktok")
AGES = (1, 3, 7)


def _p(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _conn(db_path=None):
    c = sqlite3.connect(f"file:{db_path or _default_db()}?mode=ro", uri=True)
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


def compare(ep: str, db_path=None) -> dict:
    c = _conn(db_path)
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


