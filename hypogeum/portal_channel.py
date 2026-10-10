"""
hypogeum/portal_channel.py — the data behind a client's channel pages (overview, episodes, analytics).

Read-only, and every function is scoped by ONE channel id the caller has already proven belongs to the client's
workspace. It reuses the operator dashboard's own roll-up and episode->channel resolver, so the numbers on the
two sides are the same numbers. Nothing operational leaks: no run ids, stage names, errors, provider models or
platform internals -- only results, status in plain words, and the client's own spend.

    overview(channel_id, workspace_id, db_path)  -> {rollup, weekly, series, platforms, latest, checks}
    episodes(channel_id, db_path)                -> [{episode_id, title, status, label, when, cost, views, platforms}]
    episode(channel_id, episode_id, db_path)     -> detail | None            (None = not this channel's episode)
    analytics(channel_id, days, db_path)         -> {totals, platforms, top, daily}
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_LABELS = {"complete": ("Finished", "good"), "completed": ("Finished", "good"), "running": ("In production", "warn"),
           "claimed": ("In production", "warn"), "queued": ("Queued", "warn"), "failed": ("Needs attention", "critical"),
           "error": ("Needs attention", "critical")}


def _p(db_path):
    return Path(db_path) if db_path else None


def _conn(db_path):
    from hypogeum.db import get_connection  # noqa: PLC0415
    return get_connection(_p(db_path))


def _episode_ids(conn, channel_id: str) -> set:
    from hypogeum.episode_channel_resolver import build_episode_channel_map  # noqa: PLC0415
    return {e for e, c in build_episode_channel_map(conn).items() if c == channel_id}


def _latest_by_episode(conn, ids: set) -> dict:
    """{episode_id: {views, likes, comments, shares, saves, watch_s, platforms: {platform: {...}}}} from the latest
    API snapshot of each (episode, platform) -- the same rule the operator roll-up uses."""
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT pa.episode_id, pa.platform, pa.views, pa.likes, pa.comments, pa.shares, pa.saves,
                   pa.total_watch_time_s, pa.avg_watch_time_s, pa.snapshot_date
            FROM platform_analytics pa JOIN (SELECT episode_id, platform, MAX(snapshot_date) AS d FROM platform_analytics
                 WHERE source = 'api' GROUP BY episode_id, platform) latest
              ON latest.episode_id = pa.episode_id AND latest.platform = pa.platform AND latest.d = pa.snapshot_date
            WHERE pa.source = 'api' AND pa.episode_id IN ({ph})""", list(ids)).fetchall()
    out: dict = {}
    for r in rows:
        e = out.setdefault(r["episode_id"], {"views": 0, "likes": 0, "comments": 0, "shares": 0, "saves": 0, "watch_s": 0, "platforms": {}})
        v = r["views"] or 0
        w = r["total_watch_time_s"] or ((r["avg_watch_time_s"] or 0) * v)
        for k, val in (("views", v), ("likes", r["likes"] or 0), ("comments", r["comments"] or 0), ("shares", r["shares"] or 0),
                       ("saves", r["saves"] or 0), ("watch_s", int(w))):
            e[k] += val
        pl = e["platforms"].setdefault(r["platform"], {"views": 0, "likes": 0, "comments": 0})
        pl["views"] += v
        pl["likes"] += r["likes"] or 0
        pl["comments"] += r["comments"] or 0
    return out


def _runs(channel_id: str, db_path, limit: int = 200) -> list:
    """One row per titled episode (its most recent run); superseded retries and phantom failures never show."""
    from hypogeum.dashboard_runs import list_runs  # noqa: PLC0415
    out, seen = [], set()
    for r in list_runs(db_path=_p(db_path), channel_id=channel_id, limit=limit):
        ep = r.get("episode_id")
        st = (r.get("status") or "").lower()
        untitled = not (r.get("episode_title") or r.get("episode_topic"))
        if r.get("superseded") or r.get("episode_status") == "superseded" or not ep or ep in seen \
                or (st in ("failed", "error") and (untitled or not r.get("episode_status"))):
            continue
        seen.add(ep)
        out.append(r)
    return out


def _label(r: dict) -> tuple:
    st = (r.get("episode_status") or r.get("status") or "").lower()
    return _LABELS.get(st, (st.replace("_", " ").capitalize() or "Unknown", "warn"))


def episodes(channel_id: str, db_path=None) -> list:
    conn = _conn(db_path)
    try:
        runs = _runs(channel_id, db_path)
        stats = _latest_by_episode(conn, {r["episode_id"] for r in runs})
    finally:
        conn.close()
    out = []
    for r in runs:
        label, tone = _label(r)
        s = stats.get(r["episode_id"]) or {}
        out.append({"episode_id": r["episode_id"], "title": r.get("episode_title") or r.get("episode_topic") or r["episode_id"],
                    "label": label, "tone": tone, "when": (r.get("completed_at") or r.get("started_at") or "")[:16].replace("T", " "),
                    "cost": float(r.get("episode_billed_usd") or 0.0), "views": s.get("views"), "platforms": sorted((s.get("platforms") or {}))})
    return out


def episode(channel_id: str, episode_id: str, db_path=None) -> Optional[dict]:
    conn = _conn(db_path)
    try:
        if episode_id not in _episode_ids(conn, channel_id):
            return None
        runs = [r for r in _runs(channel_id, db_path) if r["episode_id"] == episode_id]
        stats = _latest_by_episode(conn, {episode_id}).get(episode_id) or {}
        ep = conn.execute("SELECT title, topic, published_at, duration_seconds, short_count FROM episodes WHERE episode_id = ?", (episode_id,)).fetchone()
        spend = [{"provider": r["provider"], "total": round(float(r["t"] or 0), 2)} for r in conn.execute(
            "SELECT provider, SUM(cost_usd) AS t FROM costs WHERE episode_id = ? GROUP BY provider ORDER BY t DESC", (episode_id,))]
        try:
            posts = [{"platforms": __import__("json").loads(r["platforms_json"] or "[]"), "status": r["status"], "when": (r["published_at"] or r["scheduled_for"] or "")[:16].replace("T", " "),
                      "deliverable": r["deliverable_id"]} for r in conn.execute(
                "SELECT deliverable_id, status, scheduled_for, published_at, platforms_json FROM content_grid_cache WHERE episode_id = ? AND channel_id = ?", (episode_id, channel_id))]
        except Exception:  # noqa: BLE001
            posts = []
    finally:
        conn.close()
    if not runs and not ep:
        return None
    label, tone = _label(runs[0]) if runs else ("Finished", "good")
    title = (ep["title"] or ep["topic"]) if ep else (runs[0].get("episode_title") if runs else episode_id)
    return {"episode_id": episode_id, "title": title or episode_id, "label": label, "tone": tone,
            "published": (ep["published_at"] or "")[:10] if ep else "", "duration": ep["duration_seconds"] if ep else None,
            "shorts": ep["short_count"] if ep else None, "stats": stats, "spend": spend, "spend_total": round(sum(s["total"] for s in spend), 2), "posts": posts}


def _daily(conn, ids: set, days: Optional[int]) -> list:
    """Views gained per day across the channel: each (episode, platform)'s snapshots are cumulative, so the day's
    gain is the difference between consecutive snapshots (the first snapshot is not counted as a one-day gain)."""
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT episode_id, platform, snapshot_date, views FROM platform_analytics WHERE source = 'api' AND episode_id IN ({ph}) "
                        "ORDER BY episode_id, platform, snapshot_date", list(ids)).fetchall()
    gain: dict = {}
    prev: dict = {}
    for r in rows:
        key = (r["episode_id"], r["platform"])
        v = r["views"] or 0
        if key in prev:
            d = r["snapshot_date"][:10]
            gain[d] = gain.get(d, 0) + max(0, v - prev[key])
        prev[key] = v
    if not gain:
        return []
    last = max(gain)
    first = min(gain) if days is None else max(min(gain), (date.fromisoformat(last) - timedelta(days=days - 1)).isoformat())
    out, d = [], date.fromisoformat(first)
    while d <= date.fromisoformat(last):
        out.append({"day": d.isoformat(), "views": gain.get(d.isoformat(), 0)})
        d += timedelta(days=1)
    return out


def analytics(channel_id: str, days: Optional[int] = 30, db_path=None) -> dict:
    conn = _conn(db_path)
    try:
        ids = _episode_ids(conn, channel_id)
        stats = _latest_by_episode(conn, ids)
        daily = _daily(conn, ids, days)
        titles = {r["episode_id"]: (r["title"] or r["topic"] or r["episode_id"]) for r in conn.execute(
            f"SELECT episode_id, title, topic FROM episodes WHERE episode_id IN ({','.join('?' * len(ids))})", list(ids))} if ids else {}
    finally:
        conn.close()
    plat: dict = {}
    for s in stats.values():
        for p, v in s["platforms"].items():
            a = plat.setdefault(p, {"platform": p, "views": 0, "likes": 0, "comments": 0})
            for k in ("views", "likes", "comments"):
                a[k] += v[k]
    totals = {k: sum(s[k] for s in stats.values()) for k in ("views", "likes", "comments", "shares", "saves", "watch_s")}
    top = sorted(({"episode_id": e, "title": titles.get(e, e), "views": s["views"], "likes": s["likes"], "comments": s["comments"]}
                  for e, s in stats.items()), key=lambda x: -x["views"])[:8]
    return {"totals": totals, "episodes_measured": len(stats), "platforms": sorted(plat.values(), key=lambda x: -x["views"]),
            "top": top, "daily": daily, "days": days, "gained": sum(d["views"] for d in daily)}


def overview(channel_id: str, workspace_id: str, db_path=None) -> dict:
    from hypogeum.dashboard_rollup import _channel_weekly_budget, get_channel_rollup  # noqa: PLC0415
    roll = get_channel_rollup(channel_id, db_path=_p(db_path))
    weekly = _channel_weekly_budget(channel_id, _p(db_path))
    conn = _conn(db_path)
    try:
        series = _daily(conn, _episode_ids(conn, channel_id), 30)
    finally:
        conn.close()
    return {"rollup": roll, "weekly": weekly, "series": [d["views"] for d in series], "latest": episodes(channel_id, db_path)[:6],
            "checks": readiness(channel_id, workspace_id, db_path)}


def readiness(channel_id: str, workspace_id: str, db_path=None) -> list:
    """Plain-language 'can this channel run on its own?' for a client: only things the client can understand and
    (for the first two) the operator will act on. [{level: ok|warn, title, detail}]"""
    from hypogeum import client_grants as cg, workspace_keys as wk, workspace_controls as wc  # noqa: PLC0415
    items = []
    g = cg.grant_state(channel_id, db_path)
    items.append({"level": "ok" if g["active"] else "warn", "title": "Production is authorized" if g["active"] else "Waiting for spending authorization",
                  "detail": (f"{g['days_left']} days left" + (f", up to ${g['weekly_ceiling_usd']} a week." if g.get("weekly_ceiling_usd") else ".")) if g["active"]
                  else "Cortex signs this once your keys and publishing account are in place."})
    missing = [p for p in ("openrouter", "muapi", "elevenlabs", "perplexity") if not wk.workspace_key(p, workspace_id, db_path=_p(db_path))] \
        if cg.signing_preflight(channel_id, db_path).get("client") else []
    items.append({"level": "warn" if missing else "ok", "title": "Your keys are connected" if not missing else "Some keys are missing",
                  "detail": "All provider keys are in place." if not missing else "Missing: " + ", ".join(missing) + ". Add them under Setup."})
    try:
        paused = wc.is_paused(workspace_id, db_path)
    except Exception:  # noqa: BLE001
        paused = False
    items.append({"level": "warn" if paused else "ok", "title": "Production is paused" if paused else "Production is running",
                  "detail": "New episodes are on hold until it is resumed." if paused else "New episodes are made on the channel's schedule."})
    conn = _conn(db_path)
    try:
        sch = conn.execute("SELECT cadence FROM schedules WHERE channel_id = ? AND status = 'active' LIMIT 1", (channel_id,)).fetchone()
    finally:
        conn.close()
    items.append({"level": "ok" if sch else "warn", "title": "A schedule is set" if sch else "No active schedule",
                  "detail": f"Cadence: {sch['cadence']}." if sch else "Cortex has no production schedule for this channel yet."})
    return items


def revenue(channel_id: str, db_path=None) -> list:
    """Distance to each platform's monetization programme, from the operator dashboard's own growth data:
    [{platform, program, note, eligible, headline, requirements: [{label, unit, current, target, pct, remaining, met, tracked}]}].
    Criteria are the platforms' published ones and vary by country; only measured requirements get a bar."""
    from hypogeum.dashboard_analytics import get_growth_dashboard  # noqa: PLC0415
    try:
        d = get_growth_dashboard(channel_id=channel_id, db_path=_p(db_path))
        platforms = ((d.get("channels") or [{}])[0].get("monetization") or {}).get("platforms") or {}
    except Exception:  # noqa: BLE001
        return []
    out = []
    for plat, v in platforms.items():
        reqs = [{"label": r.get("label"), "unit": r.get("unit"), "current": r.get("current"), "target": r.get("target"),
                 "pct": min(100.0, float(r.get("pct") or 0)), "remaining": r.get("remaining"), "met": bool(r.get("met")),
                 "tracked": bool(r.get("tracked", True)), "one_of": bool(r.get("one_of"))} for r in (v.get("requirements") or [])]
        out.append({"platform": plat, "program": v.get("program"), "note": v.get("note"), "eligible": bool(v.get("eligible")),
                    "headline": v.get("headline"), "requirements": reqs})
    return out
