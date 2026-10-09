"""
hypogeum/dashboard_analytics.py — Phase E: Read-Only Analytics Aggregation

All reads. No writes. Safe to call from GET routes.

Public API:
    get_analytics_overview(db_path)             -> dict
    get_platform_summary(platform, db_path)     -> dict
    get_episode_performance(episode_id, db_path) -> dict | None
    get_learning_state(db_path)                 -> dict
"""

from __future__ import annotations

import re as _re
from pathlib import Path
from typing import Optional

from hypogeum.analytics_store import (
    list_analytics,
    get_episode_analytics,
    list_insights,
    list_actions,
)
from hypogeum.learning_engine import get_factory_learning_state, get_recommendations


def _safe_avg(vals: list):
    """Return avg of non-None values, or None when no data available."""
    clean = [v for v in vals if v is not None]
    return round(sum(clean) / len(clean), 4) if clean else None


def _safe_sum(vals: list) -> float:
    clean = [v for v in vals if v is not None]
    return round(sum(clean), 2) if clean else 0.0


# ===========================================================================
# Overview
# ===========================================================================

def _analytics_key_channel_map(conn) -> dict:
    """Thin wrapper over the shared resolver (episode_channel_resolver.py)
    -- kept as a local name since every caller in this file already says
    `_analytics_key_channel_map(conn)`. See that module's docstring for
    why this used to be four separate, silently-diverging copies of the
    same heuristic (dashboard_analytics, dashboard_rollup, learning_engine,
    monetization_eligibility)."""
    from hypogeum.episode_channel_resolver import build_episode_channel_map
    return build_episode_channel_map(conn)


def _episode_title_map(conn, episode_ids: set) -> dict:
    """{episode_id: best human title} for a set of ids. Never raises."""
    if not episode_ids:
        return {}
    out: dict = {}
    ph = ",".join("?" * len(episode_ids))
    ids = list(episode_ids)
    for sql in (
        f"SELECT episode_id, title_text FROM titles WHERE selected = 1 AND episode_id IN ({ph})",
        f"SELECT episode_id, title FROM episodes WHERE title IS NOT NULL AND episode_id IN ({ph})",
        f"SELECT episode_id, topic FROM episodes WHERE topic IS NOT NULL AND episode_id IN ({ph})",
        f"SELECT episode_id, topic_text FROM episode_topics WHERE topic_text IS NOT NULL AND episode_id IN ({ph})",
    ):
        try:
            for r in conn.execute(sql, ids):
                if r[0] not in out and r[1]:
                    out[r[0]] = r[1]
        except Exception:  # noqa: BLE001
            pass
    return out


def get_analytics_overview(db_path: Optional[Path] = None,
                           channel_id: Optional[str] = None) -> dict:
    """
    Analytics summary: per-platform KPIs, recent snapshots, episode
    performance table, action / insight counts. Factory-wide by default;
    scoped to one channel when ``channel_id`` is given (rows whose key
    cannot be attributed to a channel are then excluded). Non-blocking.

    Both scopes go through `_overview_from_analytics` -- the factory-wide
    path used to read two SQL views (`v_platform_kpis`/
    `v_episode_performance`, schema.sql) that summed EVERY daily snapshot
    per episode+platform instead of the latest one alone, and never
    filtered to `source='api'` -- overcounting real views up to ~7x on
    live data and letting mock-seeded rows count as real KPIs (a direct
    violation of this project's own "mock is never a real KPI" rule,
    enforced correctly everywhere else in this module). Fixed 2026-09-15.
    """
    try:
        from hypogeum.db import get_connection
        from hypogeum.dashboard_live import _db as _resolve_db
        db = _resolve_db(db_path)

        conn = get_connection(db)
        try:
            return _overview_from_analytics(conn, channel_id)
        finally:
            conn.close()
    except Exception:
        return {
            "platform_breakdown":   {},
            "recent_snapshots":     [],
            "episodes":             [],
            "pending_action_count": 0,
            "total_insights":       0,
        }


def _uae_label(iso: Optional[str]) -> Optional[str]:
    """'2026-10-09T20:06:24+00:00' -> '09 Oct 2026, 00:06 (UAE)'; None if unparseable."""
    if not iso:
        return None
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("Asia/Dubai")).strftime("%d %b %Y, %H:%M") + " (UAE)"
    except Exception:  # noqa: BLE001
        return None


def _overview_from_analytics(conn, channel_id: Optional[str] = None) -> dict:
    """Analytics overview computed straight from platform_analytics (real
    `source='api'` rows only, deduplicated to the latest snapshot per
    (episode, platform) -- the same aggregation `get_platform_summary()`
    already does). Factory-wide (`channel_id=None`) includes every real
    row, including a legacy title-keyed one the resolver can't place to a
    channel -- its views still count toward the real platform totals even
    though it can't be listed as a named episode. Scoped to one channel
    otherwise (rows whose key doesn't resolve to it are excluded)."""
    empty = {"platform_breakdown": {}, "recent_snapshots": [], "episodes": [],
             "pending_action_count": 0, "total_insights": 0,
             "channel_scoped": channel_id is not None}

    rows = []
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT episode_id, platform, snapshot_date, views, ctr, avg_view_pct, "
            "avg_watch_time_s, total_watch_time_s, likes, comments, shares, saves, "
            "imported_at, source FROM platform_analytics WHERE source='api' "
            "ORDER BY snapshot_date DESC")]
    except Exception:  # noqa: BLE001
        return empty

    if channel_id is not None:
        kmap = _analytics_key_channel_map(conn)
        mine = {k for k, c in kmap.items() if c == channel_id}
        if not mine:
            return empty
        rows = [r for r in rows if r["episode_id"] in mine]
        if not rows:
            return empty

    # latest snapshot per (key, platform); first_seen = earliest snapshot per
    # episode, the closest thing to a publish date that analytics rows carry.
    latest: dict = {}
    first_seen: dict = {}
    for r in rows:
        latest.setdefault((r["episode_id"], r["platform"]), r)
        d = r["snapshot_date"] or ""
        if d and (r["episode_id"] not in first_seen or d < first_seen[r["episode_id"]]):
            first_seen[r["episode_id"]] = d
    last_synced = max((r["imported_at"] for r in rows if r.get("imported_at")), default=None)

    titles = _episode_title_map(conn, {k for k, _ in latest})

    # Key names mirror what the old v_platform_kpis / v_episode_performance
    # views used to return -- analytics.html reads `episodes_tracked` /
    # `avg_view_pct` / `platforms_tracked` / `total_views` / `avg_ctr` /
    # `insight_count`, none of which this drops.
    platform_breakdown: dict = {}
    episodes: dict = {}
    for (key, plat), r in latest.items():
        pb = platform_breakdown.setdefault(plat, {
            "platform": plat, "episodes_tracked": 0, "total_views": 0,
            "avg_ctr": None, "avg_view_pct": None})
        pb["episodes_tracked"] += 1
        pb["total_views"] += r["views"] or 0
        _ctrs = [x for x in (pb.get("_ctrs") or []) + [r["ctr"]] if x is not None]
        _rets = [x for x in (pb.get("_rets") or []) + [r["avg_view_pct"]] if x is not None]
        pb["_ctrs"], pb["_rets"] = _ctrs, _rets
        pb["avg_ctr"] = round(sum(_ctrs) / len(_ctrs), 4) if _ctrs else None
        pb["avg_view_pct"] = round(sum(_rets) / len(_rets), 4) if _rets else None

        ep = episodes.setdefault(key, {
            "episode_id": key, "episode_title": titles.get(key) or key,
            "platforms_tracked": 0, "total_views": 0,
            "avg_ctr": None, "avg_view_pct": None, "insight_count": 0,
            "first_seen": first_seen.get(key)})
        ep["platforms_tracked"] += 1
        ep["total_views"] += r["views"] or 0
        if r["ctr"] is not None:
            ep["avg_ctr"] = r["ctr"]
        if r["avg_view_pct"] is not None:
            ep["avg_view_pct"] = r["avg_view_pct"]

    for pb in platform_breakdown.values():
        pb.pop("_ctrs", None)
        pb.pop("_rets", None)

    # Don't list platform captions / post-ids that were synced but never
    # linked to an episode Cortex made — they'd render as fake episodes.
    # Their views still counted in the platform totals above.
    _real_ep = _re.compile(r"^(ep[_-][a-z0-9]|[0-9a-f]{8}-[0-9a-f]{4})", _re.I)
    _eps = [e for e in episodes.values()
            if _real_ep.match(str(e.get("episode_id") or ""))
            or titles.get(e.get("episode_id"))]
    # Newest first: ranking by views buried the latest episodes (they have
    # the fewest views yet) and made the page look stale.
    _eps = sorted(_eps, key=lambda e: (e.get("first_seen") or "", e["total_views"] or 0), reverse=True)

    pending_count = total_insights = 0
    try:
        pending_count = conn.execute(
            "SELECT COUNT(*) AS n FROM learning_actions WHERE status='pending'"
        ).fetchone()["n"]
        total_insights = conn.execute(
            "SELECT COUNT(*) AS n FROM analytics_insights"
        ).fetchone()["n"]
    except Exception:  # noqa: BLE001
        pass

    return {
        "platform_breakdown":   platform_breakdown,
        "recent_snapshots":     rows[:10],
        "last_synced":          last_synced,
        "last_synced_uae":      _uae_label(last_synced),
        "episodes":             _eps,
        "pending_action_count": pending_count if channel_id is None else 0,
        "total_insights":       total_insights if channel_id is None else 0,
        "channel_scoped":       channel_id is not None,
    }


# ===========================================================================
# Per-platform
# ===========================================================================

def get_platform_summary(
    platform: str,
    db_path:  Optional[Path] = None,
    limit:    int             = 50,
    channel_id: Optional[str] = None,
) -> dict:
    """
    KPIs and one-row-per-episode list for one platform. Deduplicates the
    daily snapshots to the latest per episode (so views aren't summed across
    dates). Scopes to a channel when channel_id is given. Non-blocking.
    """
    _empty = {"platform": platform, "kpis": {}, "episodes": [],
              "best_episode": None, "worst_episode": None,
              "channel_scoped": bool(channel_id)}
    try:
        records = list_analytics(platform=platform, db_path=db_path, limit=500)

        if channel_id:
            try:
                from hypogeum.db import get_connection
                from hypogeum.dashboard_live import _db as _resolve_db
                conn = get_connection(_resolve_db(db_path))
                try:
                    kmap = _analytics_key_channel_map(conn)
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001
                kmap = {}
            mine = {k for k, c in kmap.items() if c == channel_id}
            records = [r for r in records if r.get("episode_id") in mine]
            if not records:
                return _empty

        # latest snapshot per episode
        latest: dict = {}
        for r in sorted(records, key=lambda x: x.get("snapshot_date") or ""):
            latest[r.get("episode_id")] = r
        rows = sorted(latest.values(), key=lambda r: -(r.get("views") or 0))

        views          = _safe_sum(r.get("views") for r in rows)
        total_watch    = _safe_sum(r.get("total_watch_time_s") for r in rows)
        avg_ctr        = _safe_avg([r.get("ctr") for r in rows])
        avg_view_pct   = _safe_avg([r.get("avg_view_pct") for r in rows])
        avg_watch_time = _safe_avg([r.get("avg_watch_time_s") for r in rows])

        scored = [r for r in rows if r.get("avg_view_pct") is not None]
        best  = max(scored, key=lambda r: r["avg_view_pct"]) if scored else None
        worst = min(scored, key=lambda r: r["avg_view_pct"]) if scored else None

        return {
            "platform": platform,
            "kpis": {
                "episodes_tracked":   len(rows),
                "total_snapshots":    len(records),
                "avg_ctr":            avg_ctr,
                "avg_view_pct":       avg_view_pct,
                "avg_watch_time_s":   avg_watch_time,
                "total_views":        int(views),
                "total_watch_time_s": total_watch,
            },
            "episodes":      rows,
            "best_episode":  best,
            "worst_episode": worst,
            "channel_scoped": bool(channel_id),
        }
    except Exception:
        return _empty


# ===========================================================================
# Per-episode
# ===========================================================================

def get_episode_performance(
    episode_id: str,
    db_path:    Optional[Path] = None,
) -> Optional[dict]:
    """
    Full analytics picture for one episode: per-platform metrics,
    insights, actions, production cost.
    Returns None if no analytics records exist for this episode.
    Non-blocking.
    """
    try:
        platforms = get_episode_analytics(episode_id, db_path=db_path)
        if not platforms:
            return None

        insights = list_insights(episode_id=episode_id, db_path=db_path)
        actions  = list_actions(db_path=db_path)

        # Get episode metadata and production cost
        from hypogeum.db import get_connection
        from hypogeum.dashboard_live import _db as _resolve_db
        db = _resolve_db(db_path)
        conn = get_connection(db)
        try:
            ep_row = conn.execute(
                "SELECT title, topic, created_at FROM episodes WHERE episode_id=?",
                [episode_id],
            ).fetchone()
            cost_row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) AS total FROM costs WHERE episode_id=?",
                [episode_id],
            ).fetchone()
        finally:
            conn.close()

        episode_meta = dict(ep_row) if ep_row else {}
        production_cost = round(cost_row["total"] or 0.0, 6) if cost_row else 0.0

        # A learning_action's source_episode is sometimes the episode's
        # legacy title text, not its real id (see episode_channel_resolver.py's
        # docstring) -- match either so an action on a title-keyed row isn't
        # silently invisible on this episode's own performance page.
        _keys = {episode_id}
        if episode_meta.get("title"):
            _keys.add(episode_meta["title"])
        ep_actions = [a for a in actions if a.get("source_episode") in _keys]

        # Group insights by dimension
        insights_by_dim: dict[str, list] = {}
        for ins in insights:
            insights_by_dim.setdefault(ins["affected_dim"], []).append(ins)

        return {
            "episode_id":       episode_id,
            "episode":          episode_meta,
            "platforms":        platforms,
            "insights":         insights,
            "insights_by_dim":  insights_by_dim,
            "actions":          ep_actions,
            "production_cost":  production_cost,
        }
    except Exception:
        return None


# ===========================================================================
# Learning state (delegates to learning_engine)
# ===========================================================================

def get_learning_state(db_path: Optional[Path] = None, *,
                       channel_id: Optional[str] = None) -> dict:
    """
    Full learning dashboard state: factory summary, pending actions,
    applied actions, recommendations by dimension. The factory summary
    (`get_factory_learning_state`'s counts/platform-coverage/dimension
    breakdown) stays factory-wide either way -- it's an operational health
    snapshot, not a per-channel actionable list. `pending_actions_list` /
    `applied_actions_list` / `recommendations` ARE filtered to
    `channel_id` when given (every other Intelligence page already
    accepts and applies channel scope; this one silently didn't, even
    though get_recommendations already supported it -- fixed 2026-09-15).
    """
    try:
        from hypogeum.learning_engine import _action_channel
        state   = get_factory_learning_state(db_path=db_path)
        pending = list_actions(status="pending",   db_path=db_path)
        applied = list_actions(status="applied",   db_path=db_path)

        episode_map = None
        if channel_id is not None:
            from hypogeum.db import get_connection
            from hypogeum.dashboard_live import _db as _resolve_db
            from hypogeum.episode_channel_resolver import build_episode_channel_map
            extra_keys = set()
            for a in (pending + applied):
                ep = a.get("source_episode") or (a.get("evidence") or {}).get("source_episode")
                if isinstance(ep, list):
                    extra_keys.update(e for e in ep if e)
                elif ep:
                    extra_keys.add(ep)
            conn = get_connection(_resolve_db(db_path))
            try:
                episode_map = build_episode_channel_map(conn, extra_keys=extra_keys)
            finally:
                conn.close()
            pending = [a for a in pending
                       if _action_channel(a, episode_map=episode_map) == channel_id]
            applied = [a for a in applied
                       if _action_channel(a, episode_map=episode_map) == channel_id]

        recs = get_recommendations(db_path=db_path, channel_id=channel_id)

        # Read existing pipeline-internal learning_records too
        try:
            from hypogeum.db import get_connection
            from hypogeum.dashboard_live import _db as _resolve_db
            db = _resolve_db(db_path)
            conn = get_connection(db)
            try:
                lr_rows = conn.execute(
                    "SELECT learning_id, episode_id, signal_type, signal_value, "
                    "signal_label, recommendation, recorded_at "
                    "FROM learning_records ORDER BY recorded_at DESC LIMIT 20"
                ).fetchall()
            finally:
                conn.close()
            learning_records = [dict(r) for r in lr_rows]
        except Exception:
            learning_records = []

        return {
            **state,
            "pending_actions_list":  pending,
            "applied_actions_list":  applied,
            "recommendations":       recs,
            "learning_records":      learning_records,
        }
    except Exception:
        return {
            "pending_actions_list": [],
            "applied_actions_list": [],
            "recommendations":      {},
            "learning_records":     [],
        }


# ---------------------------------------------------------------------------
# Growth & Monetization dashboard (2026-09-06) — where each channel stands
# against every platform's payout bar, plus who is actually watching.
# ---------------------------------------------------------------------------

_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def get_growth_dashboard(channel_id: Optional[str] = None,
                         db_path: Optional[Path] = None) -> dict:
    """Read-only. Assembles the Monetization / Growth page:
      - monetization: per (channel, platform) distance from the payout bar
      - audience: demographics + native platform metrics per channel
      - best_time: the channel's own best-time-to-post slots
      - comments: recent audience comments
      - content_decay: how fast a post's engagement lands (factory-wide)
    Scopes to one channel when channel_id is given, else every channel."""
    out: dict = {"channel_scoped": bool(channel_id), "channels": []}

    try:
        from hypogeum.monetization_eligibility import (
            compute_channel_monetization, compute_factory_monetization,
        )
        from hypogeum.channel_config import list_channel_profiles
    except Exception:  # noqa: BLE001
        return out

    if channel_id:
        target_ids = [channel_id]
    else:
        try:
            target_ids = [c.channel_id for c in list_channel_profiles(db_path=db_path)]
        except Exception:  # noqa: BLE001
            target_ids = []

    try:
        from hypogeum.publishing.zernio_native_sync import (
            get_channel_audience, get_channel_platform_insights,
            get_recent_audience_comments,
        )
        from hypogeum.publishing.zernio_aggregate_sync import get_rollup
        from hypogeum.audience_profile import summarize_observed_audience
    except Exception:  # noqa: BLE001
        get_channel_audience = get_channel_platform_insights = None  # type: ignore
        get_recent_audience_comments = get_rollup = summarize_observed_audience = None  # type: ignore

    for cid in target_ids:
        row: dict = {"channel_id": cid}
        try:
            row["monetization"] = compute_channel_monetization(cid, db_path=db_path)
        except Exception:  # noqa: BLE001
            row["monetization"] = {"platforms": {}}
        try:
            row["audience"] = get_channel_audience(cid, db_path=db_path) if get_channel_audience else {}
        except Exception:  # noqa: BLE001
            row["audience"] = {}
        try:
            row["observed_audience"] = (summarize_observed_audience(cid, db_path=db_path)
                                        if summarize_observed_audience else None)
        except Exception:  # noqa: BLE001
            row["observed_audience"] = None
        row["platform_insights"] = {}
        if get_channel_platform_insights:
            for p in ("youtube", "tiktok", "instagram", "facebook"):
                try:
                    v = get_channel_platform_insights(cid, p, db_path=db_path)
                    if v:
                        row["platform_insights"][p] = v
                except Exception:  # noqa: BLE001
                    pass
        try:
            bt = get_rollup("best_time", scope=cid, db_path=db_path) if get_rollup else None
            from hypogeum.publishing.zernio_aggregate_sync import reliable_best_time_slots
            slots = reliable_best_time_slots((bt or {}).get("payload"))[:5] if bt else []
            row["best_time"] = [
                {"day": _DOW[int(s["day_of_week"])] if 0 <= int(s["day_of_week"]) < 7 else "?",
                 "hour": int(s["hour"]), "engagement": round(s.get("avg_engagement") or 0, 1),
                 "posts": s.get("post_count")}
                for s in slots
            ]
        except Exception:  # noqa: BLE001
            row["best_time"] = []
        try:
            row["comments"] = (get_recent_audience_comments(cid, limit=8, db_path=db_path)
                               if get_recent_audience_comments else [])
        except Exception:  # noqa: BLE001
            row["comments"] = []
        out["channels"].append(row)

    try:
        fac = compute_factory_monetization(db_path=db_path)
        out["eligible_pairs"] = fac.get("eligible_pairs", 0)
        out["total_pairs"] = fac.get("total_pairs", 0)
    except Exception:  # noqa: BLE001
        out["eligible_pairs"] = out["total_pairs"] = 0

    try:
        decay = get_rollup("content_decay", scope="factory", db_path=db_path) if get_rollup else None
        out["content_decay"] = (decay or {}).get("payload", []) if decay else []
    except Exception:  # noqa: BLE001
        out["content_decay"] = []

    return out
