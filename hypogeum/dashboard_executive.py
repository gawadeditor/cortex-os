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


# ---------------------------------------------------------------------------
# Factory Health / Needs Attention / Primary CTA helpers
# ---------------------------------------------------------------------------

def _build_factory_health(active_count: int, queue_size: int, failed_count: int = 0) -> dict:
    """Translate production state into operator-readable factory health."""
    if failed_count > 0:
        status = "Degraded"
    elif active_count > 0:
        status = "Ready"
    elif queue_size > 0:
        status = "Ready"
    else:
        status = "Needs Setup"
    return {
        "status_label": status,
        "active_count": active_count,
        "queued_count": queue_size,
        "failed_count": failed_count,
    }


def _build_needs_attention(
    platform_state,
    recent_episodes: list,
    channel_profile,
    failed_run_id: Optional[str],
) -> list:
    """Return operator-language attention items. Never expose raw IDs or OIS terms."""
    items: list[dict] = []

    if platform_state:
        pmap = platform_state.get("platforms", {})
        for pkey in ("youtube", "tiktok", "instagram", "facebook"):
            p    = pmap.get(pkey, {})
            cs   = p.get("connection_state", "not_connected")
            pname = p.get("name", pkey.title())
            if cs == "not_connected":
                items.append({
                    "label":        f"{pname} is not connected",
                    "severity":     "info",
                    "action_url":   f"/platforms/connect/{pkey}",
                    "action_label": "Connect",
                })
            elif cs == "published_no_cred":
                items.append({
                    "label":        f"{pname} credential expired — reconnect to resume publishing",
                    "severity":     "warning",
                    "action_url":   f"/platforms/connect/{pkey}",
                    "action_label": "Reconnect",
                })

        if not platform_state.get("any_real_data"):
            items.append({
                "label":        "No real analytics imported yet",
                "severity":     "info",
                "action_url":   "/analytics/import",
                "action_label": "Import analytics",
            })

    if failed_run_id:
        items.append({
            "label":        "Most recent production run failed",
            "severity":     "warning",
            "action_url":   f"/run/{failed_run_id}",
            "action_label": "View run",
        })

    if not recent_episodes:
        items.append({
            "label":        "No episodes produced yet — start your first video",
            "severity":     "info",
            "action_url":   "/episodes",
            "action_label": "View episodes",
        })

    if channel_profile:
        ytv = channel_profile.get("yt_videos_per_week") or 0
        ttk = channel_profile.get("tiktok_per_week") or 0
        if not ytv and not ttk:
            items.append({
                "label":        "No publishing targets set",
                "severity":     "info",
                "action_url":   "/channel-profile",
                "action_label": "Set targets",
            })

    return items


def _build_primary_cta(
    recent_episodes: list,
    production: dict,
    needs_attention: list,
    failed_run_id: Optional[str],
) -> dict:
    """Return single primary call-to-action based on current factory state."""
    current_ep = production.get("current_episode") if production else None

    if failed_run_id:
        return {
            "label":       "Review issue",
            "url":         f"/run/{failed_run_id}",
            "description": "A recent production run failed and needs attention.",
        }

    if current_ep:
        return {
            "label":       "Continue production",
            "url":         f"/episode/{current_ep}",
            "description": "An episode is currently in production.",
        }

    if not recent_episodes:
        return {
            "label":       "Start first video",
            "url":         "/episodes",
            "description": "Get started by producing your first episode.",
        }

    no_analytics = any(
        "No real analytics" in item.get("label", "")
        for item in needs_attention
    )
    if no_analytics:
        return {
            "label":       "Import analytics",
            "url":         "/analytics/import",
            "description": "Import platform analytics to see how your videos are performing.",
        }

    return {
        "label":       "View recent episodes",
        "url":         "/episodes",
        "description": "Review your recent episode performance.",
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
            "display_title":    None,
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
        "factory_health":  {"status_label": "Needs Setup", "active_count": 0, "queued_count": 0},
        "needs_attention": [],
        "primary_cta":     {},
        "failed_run_id":   None,
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

    # ── Live production state — episode_topics is the source of truth ────────
    # run_queue serves the autonomous worker scheduler and is empty in
    # dashboard-driven workflows. episode_topics is what /episodes and /create
    # use, so the dashboard must read the same table.
    _et_claimed_count = 0
    try:
        from hypogeum.db import get_connection as _et_conn_fn
        _et_conn = _et_conn_fn(db)
        try:
            _qr = _et_conn.execute(
                "SELECT COUNT(*) AS n FROM episode_topics WHERE status = 'queued'"
            ).fetchone()
            _cr = _et_conn.execute(
                "SELECT COUNT(*) AS n FROM episode_topics WHERE status = 'claimed'"
            ).fetchone()
            result["production"]["queue_size"] = (_qr["n"] if _qr else 0)
            _et_claimed_count = (_cr["n"] if _cr else 0)
        finally:
            _et_conn.close()
    except Exception:
        pass

    # Best-effort: stage/progress detail from the active pipeline run
    try:
        from hypogeum.dashboard_live import get_active_runs
        active = get_active_runs(db_path=db)
        if active:
            latest = active[0]
            result["production"]["current_episode"] = latest.get("episode_id")
            result["production"]["current_stage"]   = latest.get("current_stage")
            result["production"]["progress_pct"]    = latest.get("progress_pct") or 0
    except Exception:
        pass

    # ── DB queries: real analytics only ──────────────────────────────────────
    failed_run_id: Optional[str] = None
    failed_run_count: int = 0
    try:
        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            _load_recent_episodes(conn, result)
            _load_real_analytics_summary(conn, result)
            _load_current_episode_title(conn, result)
            failed_run_id    = _load_failed_run_id(conn)
            failed_run_count = _load_failed_run_count(conn)
        finally:
            conn.close()
    except Exception:
        pass

    # ── Factory health, needs attention, primary CTA ──────────────────────────
    # active_count = claimed episode_topics (matches /episodes "In Production" count)
    result["factory_health"] = _build_factory_health(
        _et_claimed_count, result["production"]["queue_size"], failed_run_count
    )
    result["needs_attention"] = _build_needs_attention(
        result["platform_state"],
        result["recent_episodes"],
        result["channel_profile"],
        failed_run_id,
    )
    result["primary_cta"] = _build_primary_cta(
        result["recent_episodes"],
        result["production"],
        result["needs_attention"],
        failed_run_id,
    )

    result["failed_run_id"] = failed_run_id

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
    Includes episodes from episodes table and from publishing_outcomes (so
    manually-imported published videos appear even without a pipeline run).
    """
    try:
        rows = conn.execute(
            """
            WITH base AS (
                SELECT episode_id, title, status, created_at FROM episodes
                UNION
                SELECT po.episode_id,
                       po.episode_id AS title,
                       po.publishing_status AS status,
                       po.published_at AS created_at
                FROM publishing_outcomes po
                WHERE po.episode_id NOT IN (SELECT episode_id FROM episodes)
            )
            SELECT b.episode_id, b.title, b.status, b.created_at,
                   COALESCE(SUM(CASE WHEN pa.source != 'mock' THEN pa.views  ELSE 0 END), 0)
                       AS real_views,
                   AVG(CASE WHEN pa.source != 'mock' THEN pa.ctr         END) AS real_ctr,
                   AVG(CASE WHEN pa.source != 'mock' THEN pa.avg_view_pct END) AS real_ret,
                   MAX(po.platform_video_id) AS platform_video_id,
                   MAX(po.url)               AS url
            FROM base b
            LEFT JOIN platform_analytics pa ON pa.episode_id = b.episode_id
            LEFT JOIN publishing_outcomes po ON po.episode_id = b.episode_id
            GROUP BY b.episode_id
            ORDER BY b.created_at DESC
            LIMIT 5
            """
        ).fetchall()
        result["recent_episodes"] = [
            {
                "episode_id":        r["episode_id"],
                "title":             r["title"] or r["episode_id"],
                "published_at":      r["created_at"],
                "status":            r["status"],
                "views":             int(r["real_views"]) if r["real_views"] else None,
                "ctr":               round(r["real_ctr"] * 100, 1) if r["real_ctr"] else None,
                "retention":         round(r["real_ret"] * 100, 1) if r["real_ret"] else None,
                "platform_video_id": r["platform_video_id"],
                "url":               r["url"],
            }
            for r in rows
        ]
    except Exception:
        pass


def _load_current_episode_title(conn, result: dict) -> None:
    """Populate display_title for the current in-production episode."""
    ep_id = result["production"].get("current_episode")
    if not ep_id:
        return
    try:
        title_row = conn.execute(
            "SELECT title_text FROM titles WHERE episode_id = ? AND selected = 1 LIMIT 1",
            [ep_id],
        ).fetchone()
        if title_row:
            result["production"]["display_title"] = title_row["title_text"]
            return
        ep_row = conn.execute(
            "SELECT title, topic FROM episodes WHERE episode_id = ?", [ep_id]
        ).fetchone()
        if ep_row:
            result["production"]["display_title"] = (
                ep_row["title"] or ep_row["topic"] or ep_id
            )
    except Exception:
        pass


def _load_failed_run_id(conn) -> Optional[str]:
    """Return the run_id of the most recent failed run, or None."""
    try:
        row = conn.execute(
            "SELECT run_id FROM runs WHERE status = 'failed' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return row["run_id"] if row else None
    except Exception:
        return None


def _load_failed_run_count(conn) -> int:
    """Return the count of failed runs in the last 48 hours."""
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM runs
            WHERE status = 'failed'
              AND started_at >= datetime('now', '-48 hours')
            """
        ).fetchone()
        return int(row["cnt"]) if row and row["cnt"] else 0
    except Exception:
        return 0


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
