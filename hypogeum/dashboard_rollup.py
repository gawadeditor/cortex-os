"""
hypogeum/dashboard_rollup.py — aggregated KPIs for the Factory Overview and
each Channel Overview.

The Factory Overview answers "how is the whole operation doing?": total
views, watch time, engagement, episodes published, spend. The Channel
Overview answers the same, scoped to one channel.

Everything is derived from the local DB — `platform_analytics` (real
`source='api'` rows only, never mock), `costs`, `publication_intents`,
`runs`. A metric with no data comes back as `None`, and the template shows
an em-dash rather than a fake zero.

Public API:
    get_factory_rollup(db_path=None) -> dict
    get_channel_rollup(channel_id, db_path=None) -> dict
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _db(db_path):
    if db_path is not None:
        return db_path
    from hypogeum.db import _DEFAULT_DB_PATH
    return _DEFAULT_DB_PATH


def _episode_channel_map(conn) -> dict:
    """episode_id -> channel_id, resolved from runs first, then episode_topics."""
    m: dict[str, str] = {}
    try:
        for r in conn.execute(
            "SELECT episode_id, channel_id FROM runs "
            "WHERE episode_id IS NOT NULL AND channel_id IS NOT NULL"):
            m.setdefault(r["episode_id"], r["channel_id"])
    except Exception:  # noqa: BLE001
        pass
    try:
        for r in conn.execute(
            "SELECT episode_id, channel_id FROM episode_topics "
            "WHERE episode_id IS NOT NULL AND channel_id IS NOT NULL"):
            m.setdefault(r["episode_id"], r["channel_id"])
    except Exception:  # noqa: BLE001
        pass
    return m


def _blank_rollup() -> dict:
    return {
        "views": None, "watch_time_s": None, "engagements": None,
        "episodes_published": 0, "spend_all_usd": None, "spend_30d_usd": None,
        "revenue_est_usd": None, "subscribers": None,
        "platforms": [], "by_channel": [],
        "active_runs": 0, "pending_approvals": 0,
    }


def _analytics_slice(conn, episode_ids: Optional[set]) -> dict:
    """Latest per-(episode, platform) snapshot summed. Real API rows only."""
    rows = []
    try:
        rows = conn.execute(
            """
            SELECT pa.episode_id, pa.platform,
                   pa.views, pa.total_watch_time_s, pa.avg_watch_time_s,
                   pa.likes, pa.comments, pa.shares, pa.saves,
                   pa.snapshot_date
            FROM platform_analytics pa
            JOIN (
                SELECT episode_id, platform, MAX(snapshot_date) AS d
                FROM platform_analytics
                WHERE source = 'api'
                GROUP BY episode_id, platform
            ) latest
              ON latest.episode_id = pa.episode_id
             AND latest.platform   = pa.platform
             AND latest.d          = pa.snapshot_date
            WHERE pa.source = 'api'
            """
        ).fetchall()
    except Exception:  # noqa: BLE001
        rows = []

    views = watch = eng = 0
    per_platform: dict[str, dict] = {}
    seen = False
    for r in rows:
        if episode_ids is not None and r["episode_id"] not in episode_ids:
            continue
        seen = True
        v = r["views"] or 0
        w = r["total_watch_time_s"] or ((r["avg_watch_time_s"] or 0) * v)
        e = (r["likes"] or 0) + (r["comments"] or 0) + (r["shares"] or 0) + (r["saves"] or 0)
        views += v
        watch += w
        eng += e
        p = per_platform.setdefault(r["platform"], {"platform": r["platform"], "views": 0, "engagements": 0})
        p["views"] += v
        p["engagements"] += e

    if not seen:
        return {"views": None, "watch_time_s": None, "engagements": None, "platforms": []}
    return {
        "views": views, "watch_time_s": int(watch), "engagements": eng,
        "platforms": sorted(per_platform.values(), key=lambda x: -x["views"]),
    }


def _spend(conn, episode_ids: Optional[set]) -> tuple[Optional[float], Optional[float]]:
    now = datetime.now(timezone.utc)
    try:
        if episode_ids is None:
            allt = conn.execute("SELECT SUM(cost_usd) FROM costs").fetchone()[0]
            d30 = conn.execute(
                "SELECT SUM(cost_usd) FROM costs "
                "WHERE called_at > datetime('now','-30 days')").fetchone()[0]
        else:
            if not episode_ids:
                return 0.0, 0.0
            ph = ",".join("?" * len(episode_ids))
            ids = list(episode_ids)
            allt = conn.execute(
                f"SELECT SUM(cost_usd) FROM costs WHERE episode_id IN ({ph})", ids).fetchone()[0]
            d30 = conn.execute(
                f"SELECT SUM(cost_usd) FROM costs WHERE episode_id IN ({ph}) "
                f"AND called_at > datetime('now','-30 days')", ids).fetchone()[0]
        return (round(allt, 2) if allt else 0.0), (round(d30, 2) if d30 else 0.0)
    except Exception:  # noqa: BLE001
        return None, None
    finally:
        _ = now


def _published_count(conn, channel_id: Optional[str], ep_chan: dict) -> int:
    try:
        rows = conn.execute(
            "SELECT DISTINCT episode_id FROM publication_intents WHERE status='succeeded'"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return 0
    if channel_id is None:
        return len(rows)
    return sum(1 for r in rows if ep_chan.get(r["episode_id"]) == channel_id)


def _active_runs(conn, channel_id: Optional[str]) -> int:
    try:
        if channel_id is None:
            return conn.execute(
                "SELECT COUNT(*) FROM runs WHERE status IN ('running','claiming','in_progress')"
            ).fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM runs WHERE channel_id = ? "
            "AND status IN ('running','claiming','in_progress')", (channel_id,)).fetchone()[0]
    except Exception:  # noqa: BLE001
        return 0


def _pending_approvals(conn) -> int:
    for sql in ("SELECT COUNT(*) FROM approvals WHERE status='pending'",
                "SELECT COUNT(*) FROM finance_approvals WHERE status='pending'"):
        try:
            return conn.execute(sql).fetchone()[0]
        except Exception:  # noqa: BLE001
            continue
    return 0


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------

def get_channel_rollup(channel_id: str, db_path: Optional[Path] = None) -> dict:
    from hypogeum.db import get_connection
    out = _blank_rollup()
    conn = get_connection(_db(db_path))
    try:
        ep_chan = _episode_channel_map(conn)
        my_eps = {e for e, c in ep_chan.items() if c == channel_id}
        a = _analytics_slice(conn, my_eps)
        out.update({k: a[k] for k in ("views", "watch_time_s", "engagements", "platforms")})
        out["spend_all_usd"], out["spend_30d_usd"] = _spend(conn, my_eps)
        out["episodes_published"] = _published_count(conn, channel_id, ep_chan)
        out["active_runs"] = _active_runs(conn, channel_id)
    finally:
        conn.close()
    return out


def get_factory_rollup(db_path: Optional[Path] = None) -> dict:
    from hypogeum.db import get_connection
    out = _blank_rollup()
    conn = get_connection(_db(db_path))
    try:
        ep_chan = _episode_channel_map(conn)
        a = _analytics_slice(conn, None)
        out.update({k: a[k] for k in ("views", "watch_time_s", "engagements", "platforms")})
        out["spend_all_usd"], out["spend_30d_usd"] = _spend(conn, None)
        out["episodes_published"] = _published_count(conn, None, ep_chan)
        out["active_runs"] = _active_runs(conn, None)
        out["pending_approvals"] = _pending_approvals(conn)

        # per-channel breakdown
        try:
            from hypogeum.channel_config import list_channel_profiles
            profiles = list_channel_profiles(db_path=db_path)
        except Exception:  # noqa: BLE001
            profiles = []
        for cp in profiles:
            cid = cp.channel_id
            my_eps = {e for e, c in ep_chan.items() if c == cid}
            ca = _analytics_slice(conn, my_eps)
            csp_all, csp_30 = _spend(conn, my_eps)
            out["by_channel"].append({
                "channel_id": cid, "name": getattr(cp, "name", cid) or cid,
                "views": ca["views"], "watch_time_s": ca["watch_time_s"],
                "episodes_published": _published_count(conn, cid, ep_chan),
                "spend_all_usd": csp_all, "spend_30d_usd": csp_30,
                "active_runs": _active_runs(conn, cid),
            })
    finally:
        conn.close()
    return out
