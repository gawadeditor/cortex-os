"""
hypogeum/dashboard_executive.py — F7/F8: Executive Dashboard data layer.

Aggregates channel-owner-facing data: real analytics only (source != 'mock'),
production queue state, and platform connection status.

Hard rules enforced here:
  - Mock analytics (source='mock') NEVER appear in executive KPIs.
  - Subscribers, views, revenue, watch time show '—' when no real data exists.
  - Platform cards show 'Not connected' / 'Demo only' states explicitly.

Public API:
    get_executive_overview(db_path=None)     -> dict
    translate_alert(signal_type: str)        -> str
    translate_approval_type(approval_type)   -> str
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


# ---------------------------------------------------------------------------
# Translation tables: internal identifiers → operator language
# ---------------------------------------------------------------------------

_SIGNAL_LABELS: dict[str, str] = {
    "credential_failure":       "API key missing or invalid",
    "budget_exhaustion":        "Monthly AI budget exceeded",
    "budget_blocked":           "Monthly AI budget exceeded",
    "provider_failure":         "Content provider not responding",
    "provider_failure_spike":   "Content provider failing at high rate",
    "acceptance_audit_failed":  "System not cleared for production",
    "run_failure":              "Production run failed",
    "rate_limit":               "Provider rate limit reached",
    "schema_error":             "Configuration error detected",
    "archive_staleness":        "Content archive has stopped growing",
    "gate_failure":             "Brand compliance check failed",
    "cost_spike":               "Unusual cost spike detected",
    "ois_block":                "Operational check is blocking production",
}

_APPROVAL_LABELS: dict[str, str] = {
    "human_review":      "Content requires human review",
    "budget_approval":   "Budget approval required",
    "publish_approval":  "Publishing approval required",
    "override_approval": "Manual override needs approval",
}


def translate_alert(signal_type: str) -> str:
    """Map internal signal_type to human-readable operator label."""
    label = _SIGNAL_LABELS.get(signal_type)
    if label:
        return label
    return signal_type.replace("_", " ").title()


def translate_approval_type(approval_type: str) -> str:
    """Map internal approval_type to human-readable operator label."""
    label = _APPROVAL_LABELS.get(approval_type)
    if label:
        return label
    return approval_type.replace("_", " ").title()


# ---------------------------------------------------------------------------
# Executive overview — real data only
# ---------------------------------------------------------------------------

def get_executive_overview(db_path: Optional[Path] = None) -> dict:
    """
    Fetch executive-level data for the channel-owner dashboard.

    INTEGRITY CONTRACT:
      - analytics KPIs are computed from platform_analytics WHERE source != 'mock'
      - mock rows are counted separately to drive the demo_warning banner
      - platform connection states come from platform_connections module

    Returns a dict with keys:
        recent_episodes:  list of dicts (up to 5, real episodes only)
        production:       current episode/stage/queue
        analytics:        aggregate real-only audience metrics
        platform_state:   full connection state from platform_connections
        channel_profile:  channel identity and publishing targets
        views_chart:      pre-normalised bar data for views trend (max=100)
        demo_warning:     str or None

    Non-blocking — returns empty defaults on any DB error.
    """
    result: dict = {
        "recent_episodes": [],
        "production": {
            "current_episode":  None,
            "current_stage":    None,
            "progress_pct":     0,
            "queue_size":       0,
        },
        "analytics": {
            "subscribers":   None,
            "views":         None,
            "watch_time_h":  None,
            "revenue_usd":   None,
            "growth_pct":    None,
        },
        "platform_state":  None,
        "channel_profile": None,
        "views_chart":     [],
        "demo_warning":    None,
    }

    db = Path(db_path) if db_path else _DEFAULT_DB_PATH
    if not db.exists():
        return result

    # ── Channel profile ───────────────────────────────────────────────────────
    try:
        from hypogeum.channel_profile import get_channel_profile
        result["channel_profile"] = get_channel_profile(db_path=db)
    except Exception:
        pass

    # ── Platform connection state ─────────────────────────────────────────────
    try:
        from hypogeum.platform_connections import get_platform_connection_state
        result["platform_state"] = get_platform_connection_state(db_path=db)
        result["demo_warning"]   = result["platform_state"].get("demo_warning")
    except Exception:
        pass

    # ── Live production state ─────────────────────────────────────────────────
    try:
        from hypogeum.dashboard_live import get_active_runs, get_queue_snapshot
        active = get_active_runs(db_path=db)
        queue  = get_queue_snapshot(db_path=db)

        if active:
            latest = active[0]
            result["production"]["current_episode"] = latest.get("episode_id")
            result["production"]["current_stage"]   = latest.get("current_stage")
            result["production"]["progress_pct"]    = latest.get("progress_pct") or 0

        result["production"]["queue_size"] = queue.get("queued_count", 0) or 0
    except Exception:
        pass

    # ── DB queries: real analytics only ──────────────────────────────────────
    try:
        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            _load_recent_episodes(conn, result)
            _load_real_analytics_summary(conn, result)
        finally:
            conn.close()
    except Exception:
        pass

    # ── Views trend (pre-normalised for chart rendering) ──────────────────────
    result["views_chart"] = _build_views_chart(result["recent_episodes"])

    return result


def _build_views_chart(episodes: list) -> list:
    """
    Return a list of {label, value, pct} dicts for the views bar chart.
    pct is normalised 0-100 relative to max views in the set.
    Entries with no real views show pct=0.
    """
    if not episodes:
        return []
    max_v = max((ep.get("views") or 0 for ep in episodes), default=0)
    chart = []
    for ep in episodes:
        v = ep.get("views") or 0
        title = ep.get("title", "")
        label = (title[:10] + "…") if len(title) > 10 else title
        chart.append({
            "label": label or "—",
            "value": v,
            "pct":   round(v / max_v * 100) if max_v else 0,
        })
    return chart


# ---------------------------------------------------------------------------
# DB loaders — all enforce source != 'mock'
# ---------------------------------------------------------------------------

def _load_recent_episodes(conn, result: dict) -> None:
    """
    Last 5 episodes with REAL analytics only (source != 'mock').
    Episodes that have no real analytics show '—' for metrics but still appear
    if they were actually produced (they exist in the episodes table).
    """
    try:
        rows = conn.execute(
            """
            SELECT e.episode_id, e.title, e.status, e.created_at,
                   COALESCE(SUM(CASE WHEN pa.source != 'mock' THEN pa.views  ELSE 0 END), 0)
                       AS real_views,
                   AVG(CASE WHEN pa.source != 'mock' THEN pa.ctr         END) AS real_ctr,
                   AVG(CASE WHEN pa.source != 'mock' THEN pa.avg_view_pct END) AS real_ret
            FROM episodes e
            LEFT JOIN platform_analytics pa USING(episode_id)
            GROUP BY e.episode_id
            ORDER BY e.created_at DESC
            LIMIT 5
            """
        ).fetchall()
        result["recent_episodes"] = [
            {
                "title":        r["title"] or r["episode_id"],
                "published_at": r["created_at"],
                "status":       r["status"],
                "views":        int(r["real_views"]) if r["real_views"] else None,
                "ctr":          round(r["real_ctr"] * 100, 1) if r["real_ctr"] else None,
                "retention":    round(r["real_ret"] * 100, 1) if r["real_ret"] else None,
            }
            for r in rows
        ]
    except Exception:
        pass


def _load_real_analytics_summary(conn, result: dict) -> None:
    """
    Aggregate audience metrics for YouTube — REAL rows only (source != 'mock').
    Sets all fields to None when no real data is available, so the template
    displays '—' rather than fabricated numbers.
    """
    try:
        row = conn.execute(
            """
            SELECT SUM(views)                 AS total_views,
                   AVG(avg_watch_time_s)/3600 AS avg_watch_h
            FROM platform_analytics
            WHERE platform = 'youtube'
              AND source   != 'mock'
            """
        ).fetchone()
        if row and row["total_views"]:
            result["analytics"]["views"]       = int(row["total_views"])
            result["analytics"]["watch_time_h"] = round(row["avg_watch_h"] or 0, 1)
    except Exception:
        pass
