"""
hypogeum/dashboard_rollup.py — aggregated KPIs for the Factory Overview and
each Channel Overview.

The Factory Overview answers "how is the whole operation doing?": total
views, watch time, engagement, episodes live, spend. The Channel
Overview answers the same, scoped to one channel.

Everything is derived from the local DB — `platform_analytics` (real
`source='api'` rows only, never mock), `costs`, `content_grid_cache`,
`runs`. A metric with no data comes back as `None`, and the template shows
an em-dash rather than a fake zero.

`episodes_published` means confirmed LIVE on at least one platform (see
`_live_count`), not merely "Zernio accepted the submission" -- that
signal includes posts still scheduled for the future, which made this
number disagree with the Analytics page's own episode count until the
2026-09-15 fix.

Public API:
    get_factory_rollup(db_path=None) -> dict
    get_channel_rollup(channel_id, db_path=None) -> dict
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def _billed_maps(db_path) -> tuple[dict, dict]:
    """(all-time, last-30-days) {episode_id: usd} from the complete
    grant-consumption ledger — costs.cost_usd alone omits every MUAPI charge
    (~70% of the bill). Never raises."""
    try:
        from hypogeum.model_effective_pricing import spend_breakdown
        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        return (spend_breakdown(db_path=db_path)["by_episode"],
                spend_breakdown(db_path=db_path, since=since)["by_episode"])
    except Exception:  # noqa: BLE001
        return {}, {}


def _spend(episode_ids: Optional[set], all_map: dict, d30_map: dict
           ) -> tuple[Optional[float], Optional[float]]:
    try:
        if episode_ids is None:
            allt = sum(all_map.values())
            d30 = sum(d30_map.values())
        else:
            if not episode_ids:
                return 0.0, 0.0
            allt = sum(v for k, v in all_map.items() if k in episode_ids)
            d30 = sum(v for k, v in d30_map.items() if k in episode_ids)
        return round(allt, 2), round(d30, 2)
    except Exception:  # noqa: BLE001
        return None, None


def _live_count(conn, channel_id: Optional[str], ep_chan: dict) -> int:
    """Episodes confirmed LIVE on at least one platform -- the one episode
    count meant to read the same everywhere in the dashboard (this card,
    the exec dashboard, and close to -- though not always identical to,
    given real-world sync lag -- the Analytics page's own "episodes with
    data", which is a subset of this). A `publication_intents`/
    `publishing_attempts` row of 'succeeded' only means Zernio ACCEPTED
    the submission, which is true the moment a post is merely scheduled;
    it is not proof of being live, which is why that signal is not used
    here (see project memory on the 2026-09-15 "submitted vs live"
    dashboard-wording fix).

    Two signals, unioned:
      - content_grid_cache.status == 'published' -- Zernio's own live
        status, refreshed whenever the Grid page is opened.
      - a platform_analytics source='api' row -- real performance data
        is only possible for something that actually went out, so its
        mere presence proves liveness even before the Grid page has ever
        cached this channel's posts.
    Never raises."""
    live: set[str] = set()
    try:
        if channel_id is None:
            rows = conn.execute(
                "SELECT DISTINCT episode_id FROM content_grid_cache WHERE status = 'published'"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT episode_id FROM content_grid_cache "
                "WHERE channel_id = ? AND status = 'published'",
                (channel_id,),
            ).fetchall()
        live.update(r["episode_id"] for r in rows if r["episode_id"])
    except Exception:  # noqa: BLE001
        pass
    try:
        for r in conn.execute(
            "SELECT DISTINCT episode_id FROM platform_analytics WHERE source = 'api'"
        ):
            eid = r["episode_id"]
            # platform_analytics.episode_id is sometimes raw title/caption
            # text for a row whose late_post_id matched no receipt (see
            # dashboard_analytics._analytics_key_channel_map's docstring) --
            # skip anything that isn't a real, resolved episode, or the
            # factory-wide total stops being the sum of the per-channel
            # ones (the invariant tests/test_dashboard_rollup.py checks).
            if not eid or eid not in ep_chan:
                continue
            if channel_id is None or ep_chan[eid] == channel_id:
                live.add(eid)
    except Exception:  # noqa: BLE001
        pass
    return len(live)


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


def _channel_view_series(conn, episode_ids: Optional[set]) -> list:
    """Per-episode total real views, oldest→newest, for a channel sparkline.
    Up to 12 points. Never raises."""
    if not episode_ids:
        return []
    try:
        ph = ",".join("?" * len(episode_ids))
        rows = conn.execute(
            f"""SELECT pa.episode_id, COALESCE(SUM(pa.views),0) AS v
                FROM platform_analytics pa
                WHERE pa.source='api' AND pa.episode_id IN ({ph})
                GROUP BY pa.episode_id
                ORDER BY MAX(pa.snapshot_date) ASC LIMIT 12""",
            list(episode_ids),
        ).fetchall()
        return [int(r["v"] or 0) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _channel_weekly_budget(channel_id: str, db_path) -> dict:
    """{used, ceiling, pct} for the channel's current-week video budget.
    Never raises."""
    try:
        from hypogeum.channel_runtime_status import get_channel_runtime_status
        b = (get_channel_runtime_status(channel_id, db_path=db_path) or {}).get("weekly_budget", {})
        if b.get("configured"):
            return {"used": round(b.get("reserved_committed_usd") or 0, 2),
                    "ceiling": round(b.get("ceiling_usd") or 0, 2),
                    "pct": round(b.get("pct_used") or 0, 1)}
    except Exception:  # noqa: BLE001
        pass
    return {}


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
        all_map, d30_map = _billed_maps(_db(db_path))
        out["spend_all_usd"], out["spend_30d_usd"] = _spend(my_eps, all_map, d30_map)
        out["episodes_published"] = _live_count(conn, channel_id, ep_chan)
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
        all_map, d30_map = _billed_maps(_db(db_path))
        out["spend_all_usd"], out["spend_30d_usd"] = _spend(None, all_map, d30_map)
        out["episodes_published"] = _live_count(conn, None, ep_chan)
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
            csp_all, csp_30 = _spend(my_eps, all_map, d30_map)
            out["by_channel"].append({
                "channel_id": cid, "name": getattr(cp, "name", cid) or cid,
                "views": ca["views"], "watch_time_s": ca["watch_time_s"],
                "episodes_published": _live_count(conn, cid, ep_chan),
                "spend_all_usd": csp_all, "spend_30d_usd": csp_30,
                "active_runs": _active_runs(conn, cid),
                "views_series": _channel_view_series(conn, my_eps),
                "weekly_budget": _channel_weekly_budget(cid, db_path),
            })
    finally:
        conn.close()
    return out
