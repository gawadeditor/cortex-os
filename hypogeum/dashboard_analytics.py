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

def get_analytics_overview(db_path: Optional[Path] = None) -> dict:
    """
    Factory-wide analytics summary: per-platform KPIs, recent snapshots,
    episode performance table, action / insight counts.
    Non-blocking.
    """
    try:
        from hypogeum.db import get_connection
        from hypogeum.dashboard_live import _db as _resolve_db
        db = _resolve_db(db_path)

        conn = get_connection(db)
        try:
            # Platform breakdown from view
            try:
                plat_rows = conn.execute("SELECT * FROM v_platform_kpis").fetchall()
            except Exception:
                plat_rows = []

            # Episode performance from view
            try:
                ep_rows = conn.execute(
                    "SELECT * FROM v_episode_performance ORDER BY episode_created_at DESC LIMIT 30"
                ).fetchall()
            except Exception:
                ep_rows = []

            recent = conn.execute(
                "SELECT episode_id, platform, snapshot_date, views, ctr, avg_view_pct, "
                "avg_watch_time_s, imported_at, source "
                "FROM platform_analytics "
                "ORDER BY imported_at DESC LIMIT 10"
            ).fetchall()

            pending_count = conn.execute(
                "SELECT COUNT(*) AS n FROM learning_actions WHERE status='pending'"
            ).fetchone()["n"]

            total_insights = conn.execute(
                "SELECT COUNT(*) AS n FROM analytics_insights"
            ).fetchone()["n"]

        finally:
            conn.close()

        platform_breakdown = {}
        for r in plat_rows:
            d = dict(r)
            platform_breakdown[d["platform"]] = d

        return {
            "platform_breakdown":    platform_breakdown,
            "recent_snapshots":      [dict(r) for r in recent],
            "episodes":              [dict(r) for r in ep_rows],
            "pending_action_count":  pending_count,
            "total_insights":        total_insights,
        }
    except Exception:
        return {
            "platform_breakdown":   {},
            "recent_snapshots":     [],
            "episodes":             [],
            "pending_action_count": 0,
            "total_insights":       0,
        }


# ===========================================================================
# Per-platform
# ===========================================================================

def get_platform_summary(
    platform: str,
    db_path:  Optional[Path] = None,
    limit:    int             = 50,
) -> dict:
    """
    KPIs and episode list for one platform.
    Non-blocking.
    """
    try:
        records = list_analytics(platform=platform, db_path=db_path, limit=limit)

        # Aggregate KPIs
        views          = _safe_sum(r.get("views")           for r in records)
        total_watch    = _safe_sum(r.get("total_watch_time_s") for r in records)
        avg_ctr        = _safe_avg([r.get("ctr")            for r in records])
        avg_view_pct   = _safe_avg([r.get("avg_view_pct")   for r in records])
        avg_watch_time = _safe_avg([r.get("avg_watch_time_s") for r in records])

        # Best / worst by avg_view_pct
        scored = [r for r in records if r.get("avg_view_pct") is not None]
        best  = max(scored, key=lambda r: r["avg_view_pct"]) if scored else None
        worst = min(scored, key=lambda r: r["avg_view_pct"]) if scored else None

        return {
            "platform": platform,
            "kpis": {
                "episodes_tracked": len({r["episode_id"] for r in records}),
                "total_snapshots":  len(records),
                "avg_ctr":          avg_ctr,
                "avg_view_pct":     avg_view_pct,
                "avg_watch_time_s": avg_watch_time,
                "total_views":      int(views),
                "total_watch_time_s": total_watch,
            },
            "episodes":      records,
            "best_episode":  best,
            "worst_episode": worst,
        }
    except Exception:
        return {
            "platform": platform,
            "kpis":     {},
            "episodes": [],
            "best_episode":  None,
            "worst_episode": None,
        }


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
        ep_actions = [a for a in actions if a.get("source_episode") == episode_id]

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

def get_learning_state(db_path: Optional[Path] = None) -> dict:
    """
    Full learning dashboard state: factory summary, pending actions,
    applied actions, recommendations by dimension.
    """
    try:
        state   = get_factory_learning_state(db_path=db_path)
        pending = list_actions(status="pending",   db_path=db_path)
        applied = list_actions(status="applied",   db_path=db_path)
        recs    = get_recommendations(db_path=db_path)

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
