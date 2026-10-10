"""
hypogeum/portal_usage.py — the client's spending statement (multi-tenant plan, Phase 7: Billing / Usage).

What Cortex spent from THIS workspace's own provider keys, month by month: by provider, by channel, by day and by
episode, against the workspace's monthly ceiling. (Clients pay their providers directly; Cortex does not resell
credit, so this is a usage statement, not an invoice.) A cost belongs to a workspace through its run's channel
(costs.run_id -> runs.channel_id, falling back to the episode's run) -- never to anything else.

    usage(workspace_id, month, db_path=None, now=None) -> dict
    months(workspace_id, db_path=None, now=None, n=6)  -> [{"month", "total"}]  newest first
    csv_rows(workspace_id, month, db_path=None)        -> [[...]] one row per episode and provider
    valid_month(text, now=None)                         -> 'YYYY-MM' | None
"""
from __future__ import annotations

import calendar
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_CH = ("COALESCE(r.channel_id, (SELECT r2.channel_id FROM runs r2 WHERE r2.episode_id = c.episode_id "
       "AND r2.channel_id IS NOT NULL LIMIT 1))")
_BASE = ("FROM costs c LEFT JOIN runs r ON r.run_id = c.run_id "
         "WHERE strftime('%Y-%m', c.called_at) = ? AND " + _CH + " IN ({marks})")


def _p(db_path):
    return Path(db_path) if db_path else None


def valid_month(text, now: Optional[datetime] = None) -> Optional[str]:
    now = now or datetime.now(timezone.utc)
    m = _MONTH.match(str(text or ""))
    if not m:
        return None
    key = f"{m.group(1)}-{m.group(2)}"
    return key if key <= f"{now:%Y-%m}" and int(m.group(1)) >= 2024 else None


def _channels(workspace_id: str, db_path):
    from hypogeum.workspaces import resources_of  # noqa: PLC0415
    return resources_of(workspace_id, "channel", db_path=_p(db_path))


def _conn(db_path):
    from hypogeum.db import get_connection  # noqa: PLC0415
    return get_connection(_p(db_path))


def months(workspace_id: str, db_path=None, now: Optional[datetime] = None, n: int = 6) -> list:
    now = now or datetime.now(timezone.utc)
    chans = _channels(workspace_id, db_path)
    y, mo = now.year, now.month
    keys = []
    for _ in range(n):
        keys.append(f"{y:04d}-{mo:02d}")
        mo -= 1
        if mo == 0:
            y, mo = y - 1, 12
    totals = {k: 0.0 for k in keys}
    if chans:
        conn = _conn(db_path)
        try:
            marks = ",".join("?" * len(chans))
            for r in conn.execute(
                    "SELECT strftime('%Y-%m', c.called_at) AS m, SUM(c.cost_usd) AS t FROM costs c LEFT JOIN runs r ON r.run_id = c.run_id "
                    f"WHERE {_CH} IN ({marks}) AND strftime('%Y-%m', c.called_at) >= ? GROUP BY m", (*chans, keys[-1])):
                if r["m"] in totals:
                    totals[r["m"]] = float(r["t"] or 0)
        finally:
            conn.close()
    return [{"month": k, "total": round(totals[k], 2)} for k in keys]


def usage(workspace_id: str, month: str, db_path=None, now: Optional[datetime] = None) -> dict:
    from hypogeum import workspace_controls as wc  # noqa: PLC0415
    now = now or datetime.now(timezone.utc)
    chans = _channels(workspace_id, db_path)
    y, mo = int(month[:4]), int(month[5:])
    days = calendar.monthrange(y, mo)[1]
    current = month == f"{now:%Y-%m}"
    out = {"month": month, "total": 0.0, "ceiling": wc.month_ceiling(workspace_id, db_path) if current else 0.0,
           "current": current, "days_in_month": days, "elapsed_days": now.day if current else days,
           "by_provider": [], "by_channel": [], "daily": [], "episodes": [], "calls": 0, "projected": None,
           "avg_per_episode": None}
    if not chans:
        return out
    marks = ",".join("?" * len(chans))
    base = _BASE.format(marks=marks)
    args = (month, *chans)
    conn = _conn(db_path)
    try:
        out["by_provider"] = [{"provider": r["p"], "total": round(float(r["t"] or 0), 2), "calls": r["n"]} for r in conn.execute(
            f"SELECT c.provider AS p, SUM(c.cost_usd) AS t, COUNT(*) AS n {base} GROUP BY c.provider ORDER BY t DESC", args)]
        out["by_channel"] = [{"channel_id": r["ch"], "total": round(float(r["t"] or 0), 2)} for r in conn.execute(
            f"SELECT {_CH} AS ch, SUM(c.cost_usd) AS t {base} GROUP BY ch ORDER BY t DESC", args)]
        daily = {r["d"]: float(r["t"] or 0) for r in conn.execute(
            f"SELECT substr(c.called_at, 1, 10) AS d, SUM(c.cost_usd) AS t {base} GROUP BY d", args)}
        out["daily"] = [{"day": d, "total": round(daily.get(d, 0.0), 2)} for d in
                        (f"{month}-{i:02d}" for i in range(1, (now.day if current else days) + 1))]
        eps = []
        for r in conn.execute(
                f"SELECT c.episode_id AS e, {_CH} AS ch, SUM(c.cost_usd) AS t, MIN(c.called_at) AS first {base} "
                "AND c.episode_id IS NOT NULL GROUP BY c.episode_id ORDER BY first DESC", args):
            row = conn.execute("SELECT title, topic FROM episodes WHERE episode_id = ?", (r["e"],)).fetchone()
            eps.append({"episode_id": r["e"], "channel_id": r["ch"], "total": round(float(r["t"] or 0), 2),
                        "title": ((row["title"] or row["topic"]) if row else None) or r["e"], "first": (r["first"] or "")[:10]})
        out["episodes"] = eps
        out["calls"] = sum(p["calls"] for p in out["by_provider"])
    finally:
        conn.close()
    out["total"] = round(sum(p["total"] for p in out["by_provider"]), 2)
    paid = [e for e in out["episodes"] if e["total"] > 0]
    out["avg_per_episode"] = round(sum(e["total"] for e in paid) / len(paid), 2) if paid else None
    if current and out["elapsed_days"] >= 3 and out["total"] > 0:
        out["projected"] = round(out["total"] / out["elapsed_days"] * days, 2)
    return out


def csv_rows(workspace_id: str, month: str, db_path=None) -> list:
    chans = _channels(workspace_id, db_path)
    rows = [["date", "channel", "episode", "provider", "model", "call_type", "cost_usd"]]
    if not chans:
        return rows
    marks = ",".join("?" * len(chans))
    conn = _conn(db_path)
    try:
        for r in conn.execute(
                f"SELECT substr(c.called_at,1,10) AS d, {_CH} AS ch, c.episode_id AS e, c.provider AS p, c.model AS m, "
                f"c.call_type AS t, c.cost_usd AS u {_BASE.format(marks=marks)} ORDER BY c.called_at", (month, *chans)):
            rows.append([r["d"], r["ch"], r["e"] or "", r["p"], r["m"] or "", r["t"] or "", f"{float(r['u'] or 0):.4f}"])
    finally:
        conn.close()
    return rows
