"""
hypogeum/hook_learning.py — which opening-line patterns actually HOLD viewers on this channel.

Every script picks a `hook_pattern` for its first beat (hypogeum/hook_patterns.py). Until now nobody checked whether
a pattern works. This joins each YouTube video's measured retention (how much of the audience is still there at 5s,
and how much of the video is watched overall) to the pattern its script used, aggregates per pattern, and — only
with enough evidence — writes a `hook_patterns` directive the writers read on the next episode.

Runs in the worker learning cycle. Slow I/O (reading scripts, possibly from the NAS) happens with NO database
connection open (see project memory: never hold a DB transaction across slow I/O). Never raises.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from typing import Dict, List, Optional

from hypogeum.logger import get_logger
from hypogeum.moment_learning import _roots, _youtube_videos
from hypogeum.retention_moments import _points, find_episode_dir

log = get_logger(agent="hook_learning")

MIN_PER_PATTERN = 3
MIN_PATTERNS = 2
MIN_GAP_PTS = 8.0                 # difference in mean avg-view % that counts as a real signal
DIRECTIVE_DIMENSION = "hook_patterns"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_trait(o):
    """(hook_pattern, first spoken line) from a script_package dict."""
    pat, text = None, None

    def walk(x):
        nonlocal pat, text
        if isinstance(x, dict):
            if pat is None and isinstance(x.get("hook_pattern"), str):
                pat = x["hook_pattern"]
            if text is None and "beat_id" in x and isinstance(x.get("text"), str):
                text = x["text"]
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(o)
    return pat, text


def traits_for_episode(ep_dir) -> Optional[dict]:
    try:
        f = next(iter(sorted(ep_dir.glob("script_package_*.json"))), None)
        if f is None:
            return None
        pat, text = _find_trait(json.loads(f.read_text()))
        return {"hook_pattern": pat, "hook_text": text} if pat else None
    except Exception:  # noqa: BLE001
        return None


def early_hold(curve: list, duration_s: float, at_s: float = 5.0) -> Optional[float]:
    """Share of the opening audience still watching at `at_s` seconds (ratio at that second / ratio at the very start)."""
    pts = _points(curve)
    if not pts or duration_s <= 0 or at_s >= duration_s:
        return None
    start = max(y for _, y in pts[:3]) or None
    if not start:
        return None
    x = at_s / duration_s
    return min(pts, key=lambda p: abs(p[0] - x))[1] / start


def aggregate(rows: List[dict]) -> dict:
    by: Dict[str, List[dict]] = {}
    for r in rows:
        if r.get("hook_pattern") and r["hook_pattern"] != "none":
            by.setdefault(r["hook_pattern"], []).append(r)
    stats = {}
    for pat, rs in by.items():
        if len(rs) < MIN_PER_PATTERN:
            continue
        holds = [r["hold5"] for r in rs if r["hold5"] is not None]
        stats[pat] = {"n": len(rs), "avg_view_pct": round(statistics.mean(r["avg_view_pct"] for r in rs) * 100, 1),
                      "hold5": round(statistics.mean(holds) * 100, 1) if holds else None,
                      "views": round(statistics.mean(r["views"] for r in rs)),
                      "example": max(rs, key=lambda r: r["avg_view_pct"]).get("hook_text")}
    return stats


def directive_text(stats: dict) -> Optional[str]:
    if len(stats) < MIN_PATTERNS:
        return None
    ranked = sorted(stats.items(), key=lambda kv: -kv[1]["avg_view_pct"])
    best, worst = ranked[0], ranked[-1]
    if best[1]["avg_view_pct"] - worst[1]["avg_view_pct"] < MIN_GAP_PTS:
        return None

    def line(k, v):
        hold = f", {v['hold5']:.0f}% still watching at 5s" if v["hold5"] is not None else ""
        return f'"{k}" ({v["n"]} videos: {v["avg_view_pct"]:.0f}% of the video watched on average{hold})'

    ex = f' A line that worked: "{(best[1]["example"] or "")[:140]}".' if best[1].get("example") else ""
    return (f"MEASURED HOOK PATTERNS ON THIS CHANNEL: " + "; ".join(line(k, v) for k, v in ranked) + f". "
            f"Prefer \"{best[0]}\" and avoid \"{worst[0]}\" for the opening line unless the story clearly fits another pattern.{ex} "
            f"This is a measured comparison on this channel's own videos, not a style preference.")


def learn_channel_hooks(channel_id: str, *, db_path=None) -> dict:
    out = {"channel_id": channel_id, "videos": 0, "patterns": 0, "directive": None}
    try:
        from hypogeum.channel_learning import get_active_directives, record_directive
        from hypogeum.db import get_connection, initialize_schema
        initialize_schema(db_path)
        conn = get_connection(db_path)
        try:
            vids = _youtube_videos(conn, channel_id)
            cached = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT episode_id, hook_pattern, hook_text FROM episode_traits")}
        finally:
            conn.close()                                   # nothing below holds a connection
        rows, to_cache = [], {}
        for v in vids:
            ep = v["ep"]
            pat, text = cached.get(ep, (None, None))
            if not pat:
                d = find_episode_dir(ep, _roots(channel_id))
                t = traits_for_episode(d) if d is not None else None
                if t:
                    pat, text = t["hook_pattern"], t["hook_text"]
                    to_cache[ep] = (pat, text)
            if not pat:
                continue
            dur = v["aw"] / v["vp"]
            rows.append({"hook_pattern": pat, "hook_text": text, "avg_view_pct": v["vp"], "views": v["views"],
                         "hold5": early_hold(json.loads(v["cur"]), dur)})
        out["videos"] = len(rows)
        stats = aggregate(rows)
        out["patterns"] = len(stats)
        text = directive_text(stats)
        conn = get_connection(db_path)
        try:
            for ep, (pat, tx) in to_cache.items():
                conn.execute("INSERT OR REPLACE INTO episode_traits (episode_id, hook_pattern, hook_text, cached_at) VALUES (?,?,?,?)",
                             (ep, pat, tx, _now()))
            conn.commit()
        finally:
            conn.close()
        if text:
            active = get_active_directives(channel_id, dimension=DIRECTIVE_DIMENSION, db_path=db_path)
            if not active or active[0]["directive_text"] != text:
                record_directive(channel_id, DIRECTIVE_DIMENSION, "prefer", text, db_path=db_path)
                out["directive"] = "written"
    except Exception as exc:  # noqa: BLE001
        log.error("hook_learning_failed", channel_id=channel_id, error=str(exc)[:200])
    return out
