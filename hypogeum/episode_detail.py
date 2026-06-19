"""
hypogeum/episode_detail.py — Episode Detail page data layer (P0 / Steps 1–4).

Public API:
    get_episode_detail(episode_id, db_path=None) -> dict | None

Returns None when the episode does not exist.
Non-blocking: returns None on any DB error.
No network calls. No mock data. Real DB reads only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

# ---------------------------------------------------------------------------
# Stage name → display group mapping
# None = internal/overhead stage hidden from operator view
# ---------------------------------------------------------------------------

_STAGE_TO_GROUP: dict[str, Optional[str]] = {
    "research_director":            "Research",
    "narrative_architect":          "Script",
    "script_engine":                "Script",
    "audio_agent":                  "Voiceover",
    "visual_package_director":      "Visuals",
    "thumbnail_director":           "Visuals",
    "assembly_agent":               "Assembly",
    "asset_archive":                "Assembly",
    "qa_agent":                     "Final Video",
    "compliance_agent":             "Final Video",
    "publishing_agent":             "Publishing",
    # Overhead — not shown in operator timeline
    "startup_checks":               None,
    "executive_producer":           None,
    "finance_agent_authorization":  None,
    "finance_agent_ledger":         None,
    "archive_agent":                None,
}

_GROUP_ORDER = [
    "Research",
    "Script",
    "Voiceover",
    "Visuals",
    "Assembly",
    "Final Video",
    "Publishing",
]

# Human-readable error hints for common failure types
_ERROR_HINTS: dict[str, str] = {
    "rate_limit":  "Provider rate limit reached — credits may be exhausted.",
    "api_error":   "API returned an error — may be a temporary provider issue.",
    "timeout":     "Stage timed out — network or provider may be unavailable.",
    "budget":      "Budget ceiling reached — run halted by cost guard.",
    "credential":  "API credential missing or invalid.",
    "ois_blocked": "Factory blocked by an active operational signal.",
}

# Map pipeline status codes to operator labels
_STATUS_LABELS: dict[str, str] = {
    "complete":   "Complete",
    "completed":  "Complete",
    "failed":     "Failed",
    "running":    "In Production",
    "queued":     "Queued",
    "paused":     "Paused",
    "skipped":    "Skipped",
    "pending":    "Pending",
}

# Map publishing status codes to operator labels
_PUBLISH_STATUS_LABELS: dict[str, str] = {
    "success":  "Published",
    "failed":   "Failed",
    "pending":  "Uploading",
    "skipped":  "Skipped",
    "unknown":  "Unknown",
}

# Canonical platform order for Publishing tab
_PLATFORM_ORDER = ("youtube", "tiktok", "instagram", "facebook")

_PLATFORM_DISPLAY_NAMES: dict[str, str] = {
    "youtube":   "YouTube",
    "tiktok":    "TikTok",
    "instagram": "Instagram",
    "facebook":  "Facebook",
}

# Operator-language translations for publish failure reasons
_PUBLISH_FAILURE_HINTS: dict[str, str] = {
    "rate_limit": "Platform rate limit reached",
    "timeout":    "Upload timed out",
    "api_error":  "Platform rejected upload",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(r) -> dict:
    return dict(r) if r else {}


def _fmt_duration(ms) -> str:
    if ms is None:
        return "—"
    ms = float(ms)
    if ms < 1_000:
        return f"{ms:.0f} ms"
    if ms < 60_000:
        return f"{ms / 1_000:.1f} s"
    return f"{ms / 60_000:.1f} min"


def _translate_error(msg: Optional[str]) -> Optional[str]:
    """Return a short operator-friendly hint from a raw error string."""
    if not msg:
        return None
    m = msg.lower()
    for key, hint in _ERROR_HINTS.items():
        if key in m:
            return hint
    # Return first 120 chars of the raw message as fallback
    return msg[:120] if len(msg) > 120 else msg


def _translate_publish_failure(reason: Optional[str]) -> str:
    """Translate a raw publish failure_reason to operator language."""
    if not reason:
        return "Publishing failed"
    r = reason.lower()
    for key, hint in _PUBLISH_FAILURE_HINTS.items():
        if key in r:
            return hint
    return "Publishing failed"


def _published_at_display(published_at: Optional[str]) -> str:
    """Return a human-readable age string from an ISO 8601 published_at value."""
    if not published_at:
        return "Unknown"
    try:
        from datetime import date
        pub_date = date.fromisoformat(published_at[:10])
        today = date.today()
        delta = (today - pub_date).days
        if delta == 0:
            return "Today"
        if delta == 1:
            return "Yesterday"
        return f"{delta} days ago"
    except Exception:
        return "Unknown"


def _group_status(stages: list[dict]) -> str:
    """
    Aggregate per-stage statuses into a single group status.
    Priority: failed > running > completed > skipped > pending.
    """
    if not stages:
        return "pending"
    statuses = {s.get("status", "pending") for s in stages}
    if "failed" in statuses:
        return "failed"
    if "running" in statuses:
        return "running"
    if all(s in ("completed", "skipped") for s in statuses):
        if "completed" in statuses:
            return "completed"
        return "skipped"
    return "pending"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_episode_detail(
    episode_id: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """
    Aggregate all Episode Detail page data from existing DB tables.

    Reads from: episodes, runs, stage_executions, assets,
                publishing_outcomes, costs, titles.

    Returns None when the episode does not exist or on any DB error.
    Never returns mock data.
    """
    path = Path(db_path) if db_path else _DEFAULT_DB_PATH
    try:
        conn = get_connection(path)
        try:
            return _build_detail(conn, episode_id)
        finally:
            conn.close()
    except Exception:
        return None


def _build_detail(conn, episode_id: str) -> Optional[dict]:
    # ── Episode record ────────────────────────────────────────────────────
    ep_row = conn.execute(
        "SELECT * FROM episodes WHERE episode_id = ?", [episode_id]
    ).fetchone()
    if ep_row is None:
        return None
    ep = _row(ep_row)

    # ── Published title (titles table, selected=1) ─────────────────────
    title_row = conn.execute(
        "SELECT title_text FROM titles WHERE episode_id = ? AND selected = 1 LIMIT 1",
        [episode_id],
    ).fetchone()
    display_title = (
        title_row["title_text"] if title_row
        else ep.get("title") or ep.get("topic") or episode_id
    )

    # ── Most recent run for this episode ───────────────────────────────
    run_row = conn.execute(
        """
        SELECT * FROM runs
        WHERE episode_id = ?
        ORDER BY started_at DESC
        LIMIT 1
        """,
        [episode_id],
    ).fetchone()
    run = _row(run_row) if run_row else {}
    run_id = run.get("run_id")

    # ── Stage executions ────────────────────────────────────────────────
    stages: list[dict] = []
    if run_id:
        rows = conn.execute(
            """
            SELECT stage_exec_id, stage_name, status, started_at, completed_at,
                   duration_ms, skipped, error_message, cost_usd, model_slug
            FROM stage_executions
            WHERE run_id = ?
            ORDER BY started_at ASC
            """,
            [run_id],
        ).fetchall()
        stages = [_row(r) for r in rows]
        for s in stages:
            s["duration_fmt"] = _fmt_duration(s.get("duration_ms"))
            s["error_hint"]   = _translate_error(s.get("error_message"))

    # ── Assets ──────────────────────────────────────────────────────────
    assets: list[dict] = []
    if run_id:
        rows = conn.execute(
            """
            SELECT asset_id, asset_type, provider, file_path, created_at
            FROM assets
            WHERE run_id = ?
            ORDER BY created_at ASC
            """,
            [run_id],
        ).fetchall()
        assets = [_row(r) for r in rows]

    # ── Costs ───────────────────────────────────────────────────────────
    costs: list[dict] = []
    total_cost_usd: float = 0.0
    if run_id:
        rows = conn.execute(
            """
            SELECT cost_id, provider, model, call_type, cost_usd, called_at, stage_name
            FROM costs
            WHERE run_id = ?
            ORDER BY called_at ASC
            """,
            [run_id],
        ).fetchall()
        costs = [_row(r) for r in rows]
        total_cost_usd = round(sum(c.get("cost_usd") or 0.0 for c in costs), 6)

    # ── Publishing outcomes ──────────────────────────────────────────────
    pub_rows = conn.execute(
        """
        SELECT platform, publishing_status, platform_video_id,
               published_at, url, failure_reason
        FROM publishing_outcomes
        WHERE episode_id = ?
        ORDER BY platform ASC
        """,
        [episode_id],
    ).fetchall()
    platforms = []
    pub_outcomes_by_platform: dict[str, dict] = {}
    for r in pub_rows:
        p = _row(r)
        p["status_label"] = _PUBLISH_STATUS_LABELS.get(
            p.get("publishing_status", "unknown"), "Unknown"
        )
        platforms.append(p)
        pub_outcomes_by_platform[p["platform"]] = p

    # ── Credential states (for Publishing tab connection_state) ─────────
    cred_states = _load_cred_states(conn)

    # ── Publishing tab data ─────────────────────────────────────────────
    publishing = _build_publishing_data(pub_outcomes_by_platform, cred_states)

    # ── Thumbnails tab data ──────────────────────────────────────────────
    thumbnails = _build_thumbnails_data(conn, episode_id, run_id)

    # ── Titles tab data ──────────────────────────────────────────────────
    titles_data = _build_titles_data(conn, episode_id)

    # ── Build stage groups for the Production tab ────────────────────────
    stage_groups = _build_stage_groups(stages, assets)

    # ── Current production stage (for in-progress runs) ─────────────────
    current_stage: Optional[str] = None
    if run.get("status") == "running":
        for s in reversed(stages):
            if s.get("status") == "running":
                raw = s.get("stage_name", "")
                current_stage = _STAGE_TO_GROUP.get(raw) or raw.replace("_", " ").title()
                break

    # ── Run timing ──────────────────────────────────────────────────────
    run_duration_fmt: Optional[str] = None
    if run.get("started_at") and run.get("completed_at"):
        try:
            from datetime import datetime
            fmt = "%Y-%m-%dT%H:%M:%S"
            t0 = datetime.fromisoformat(run["started_at"][:19])
            t1 = datetime.fromisoformat(run["completed_at"][:19])
            elapsed_ms = (t1 - t0).total_seconds() * 1_000
            run_duration_fmt = _fmt_duration(elapsed_ms)
        except Exception:
            pass

    return {
        # Identity
        "episode_id":     episode_id,
        "display_title":  display_title,
        "topic":          ep.get("topic"),
        "status":         ep.get("status", "unknown"),
        "status_label":   _STATUS_LABELS.get(ep.get("status", ""), ep.get("status", "—")),
        "created_at":     ep.get("created_at"),
        "published_at":   ep.get("published_at"),

        # Run
        "run_id":              run_id,
        "run_status":          run.get("status"),
        "run_started_at":      run.get("started_at"),
        "run_completed_at":    run.get("completed_at"),
        "run_duration_fmt":    run_duration_fmt,
        "run_error_message":   run.get("error_message"),
        "run_error_hint":      _translate_error(run.get("error_message")),
        "current_stage":       current_stage,

        # Economics
        "total_cost_usd": total_cost_usd,

        # Publishing (header pills — quick status per platform)
        "platforms": platforms,

        # Publishing tab (full card data)
        "publishing": publishing,

        # Thumbnails tab
        "thumbnails": thumbnails,

        # Titles tab
        "titles": titles_data,

        # Production tab
        "stage_groups": stage_groups,

        # Advanced (raw data for developers)
        "raw_stages": stages,
        "assets":     assets,
        "costs":      costs,
    }


def _build_stage_groups(
    stages: list[dict],
    assets: list[dict],
) -> list[dict]:
    """
    Group stage_executions into operator-facing display groups.
    Returns list ordered by _GROUP_ORDER; empty groups are omitted.
    """
    # Bucket stages by display group
    buckets: dict[str, list[dict]] = {g: [] for g in _GROUP_ORDER}
    for s in stages:
        group = _STAGE_TO_GROUP.get(s.get("stage_name", ""), "")
        if group:
            buckets.setdefault(group, []).append(s)

    # Bucket assets by stage name → group
    asset_counts: dict[str, int] = {}
    for a in assets:
        asset_counts[a.get("asset_type", "unknown")] = (
            asset_counts.get(a.get("asset_type", "unknown"), 0) + 1
        )

    groups: list[dict] = []
    for group_name in _GROUP_ORDER:
        grp_stages = buckets.get(group_name, [])
        if not grp_stages:
            continue

        status    = _group_status(grp_stages)
        total_ms  = sum(s.get("duration_ms") or 0 for s in grp_stages)
        total_cost = round(sum(s.get("cost_usd") or 0.0 for s in grp_stages), 6)
        error_msg = next(
            (s.get("error_hint") or s.get("error_message")
             for s in grp_stages if s.get("status") == "failed"),
            None,
        )

        groups.append({
            "group_name":        group_name,
            "status":            status,
            "stage_count":       len(grp_stages),
            "total_duration_ms": total_ms if total_ms else None,
            "duration_fmt":      _fmt_duration(total_ms) if total_ms else "—",
            "total_cost_usd":    total_cost,
            "error_message":     error_msg,
            "stages":            grp_stages,   # full stage rows for Advanced
        })

    return groups


# ---------------------------------------------------------------------------
# Publishing tab data
# ---------------------------------------------------------------------------

def _load_cred_states(conn) -> dict[str, str]:
    """
    Return {platform: credential_status} using the most-recent credential row
    per platform.  status values: "active" | "expired" | "revoked" | "none"
    """
    result = {p: "none" for p in _PLATFORM_ORDER}
    try:
        rows = conn.execute(
            """
            SELECT provider, status
            FROM secret_profiles
            WHERE provider IN ('youtube','tiktok','instagram','facebook')
              AND status IN ('active','expired','revoked')
            ORDER BY created_at DESC
            """
        ).fetchall()
        seen: set = set()
        for r in rows:
            if r["provider"] not in seen:
                result[r["provider"]] = r["status"]
                seen.add(r["provider"])
    except Exception:
        pass
    return result


def _build_publishing_data(
    pub_outcomes: dict[str, dict],
    cred_states: dict[str, str],
) -> dict:
    """
    Merge per-episode publishing_outcomes with platform credential states.

    Only includes a card for a platform when it has a publishing_outcome OR
    an active/expired credential.

    Returns:
        {
          "platform_cards": [ {...} ],
          "summary": {"published": N, "failed": N, "pending": N, "connected": N},
        }
    """
    cards = []
    published = failed = pending = connected = 0

    for platform in _PLATFORM_ORDER:
        outcome     = pub_outcomes.get(platform)
        cred_status = cred_states.get(platform, "none")
        has_outcome = outcome is not None

        # Skip platforms with nothing to show
        if not has_outcome and cred_status == "none":
            continue

        # connection_state: from credential only, not from analytics
        if cred_status == "active":
            connection_state = "connected"
            connected += 1
        elif cred_status in ("expired", "revoked"):
            connection_state = "expired"
        else:
            connection_state = "missing"

        # card_state drives the visual variant
        pub_status = outcome.get("publishing_status") if outcome else None
        if pub_status == "success":
            card_state = "published"
            published += 1
        elif pub_status == "failed":
            card_state = "failed"
            failed += 1
        elif pub_status in ("pending", "uploading") or (has_outcome and pub_status not in (None,)):
            card_state = "pending"
            pending += 1
        elif has_outcome:
            # Record exists with unknown/skipped status
            card_state = "pending"
            pending += 1
        elif connection_state == "connected":
            card_state = "connected_unpublished"
        else:
            card_state = "not_connected"

        # Failure hint
        raw_reason  = outcome.get("failure_reason") if outcome else None
        failure_hint = _translate_publish_failure(raw_reason) if raw_reason else None

        # Date display
        published_at = outcome.get("published_at") if outcome else None

        cards.append({
            "platform":            platform,
            "display_name":        _PLATFORM_DISPLAY_NAMES[platform],
            "card_state":          card_state,
            "publishing_status":   pub_status,
            "platform_video_id":   outcome.get("platform_video_id") if outcome else None,
            "url":                 outcome.get("url") if outcome else None,
            "published_at":        published_at,
            "published_at_display": _published_at_display(published_at),
            "failure_reason":      raw_reason,
            "failure_hint":        failure_hint,
            "connection_state":    connection_state,
        })

    return {
        "platform_cards": cards,
        "summary": {
            "published": published,
            "failed":    failed,
            "pending":   pending,
            "connected": connected,
        },
    }


# ---------------------------------------------------------------------------
# Thumbnails tab data
# ---------------------------------------------------------------------------

# CTR thresholds (stored as 0.0–1.0 in platform_analytics.ctr)
_CTR_WINNER_THRESHOLD       = 0.05   # >= 5% → winner
_CTR_UNDERPERFORMED_THRESHOLD = 0.02  # < 2%  → underperformed


def _build_thumbnails_data(
    conn,
    episode_id: str,
    run_id: Optional[str],
) -> dict:
    """
    Build Thumbnails tab data from thumbnail_candidates + assets + platform_analytics.

    image_path:   joined from assets (asset_type='thumbnail') by position match.
    model/seed:   not stored in the schema → always None.
    performance:  real analytics only (source != 'mock'), applied to selected
                  candidate only (non-selected candidates were never published).

    Returns:
        {
          "candidates": [...],
          "summary": {"count": N, "has_selected": bool, "has_real_data": bool},
        }
    """
    # ── Thumbnail candidates ─────────────────────────────────────────────
    cand_rows = conn.execute(
        """
        SELECT candidate_id, run_id, episode_id, rank,
               text_overlay, placement, visual_prompt, selected
        FROM thumbnail_candidates
        WHERE episode_id = ?
        ORDER BY rank ASC
        """,
        [episode_id],
    ).fetchall()

    if not cand_rows:
        return {
            "candidates": [],
            "summary": {"count": 0, "has_selected": False, "has_real_data": False},
        }

    # ── Thumbnail asset file paths (heuristic: match by creation order → rank) ──
    asset_paths: list[Optional[str]] = []
    if run_id:
        asset_rows = conn.execute(
            """
            SELECT file_path FROM assets
            WHERE run_id = ? AND asset_type = 'thumbnail'
            ORDER BY created_at ASC
            """,
            [run_id],
        ).fetchall()
        asset_paths = [r["file_path"] for r in asset_rows]

    # ── Real analytics for this episode (source != 'mock') ───────────────
    # Prefer YouTube; fall back to any platform with real data.
    analytics_rows = conn.execute(
        """
        SELECT platform, ctr, impressions, views
        FROM platform_analytics
        WHERE episode_id = ? AND source != 'mock'
          AND ctr IS NOT NULL
        ORDER BY CASE platform WHEN 'youtube' THEN 0 ELSE 1 END ASC,
                 snapshot_date DESC
        """,
        [episode_id],
    ).fetchall()

    # Take the first (preferred) platform's most-recent data
    real_ctr: Optional[float]    = None
    real_impressions: Optional[int] = None
    real_views: Optional[int]    = None
    linked_platforms: list[str]  = []

    # Collect all platforms with real data
    seen_plat: set = set()
    for ar in analytics_rows:
        if ar["platform"] not in seen_plat:
            linked_platforms.append(ar["platform"])
            seen_plat.add(ar["platform"])
        if real_ctr is None:  # first row is already preferred platform
            real_ctr        = ar["ctr"]
            real_impressions = int(ar["impressions"]) if ar["impressions"] else None
            real_views       = int(ar["views"])       if ar["views"]       else None

    # ── Build candidate dicts ─────────────────────────────────────────────
    candidates: list[dict] = []
    for i, row in enumerate(cand_rows):
        c = _row(row)
        # Image path by position (heuristic; None if no matching asset)
        c["image_path"] = asset_paths[i] if i < len(asset_paths) else None
        # model/seed not stored in schema
        c["model"] = None
        c["seed"]  = None

        selected = bool(c.get("selected"))

        if selected:
            c["linked_platforms"] = linked_platforms
            if real_ctr is not None:
                c["ctr"]         = round(real_ctr * 100, 2)
                c["impressions"] = real_impressions
                c["views"]       = real_views
                if real_ctr >= _CTR_WINNER_THRESHOLD:
                    c["performance_status"] = "winner"
                elif real_ctr < _CTR_UNDERPERFORMED_THRESHOLD:
                    c["performance_status"] = "underperformed"
                else:
                    c["performance_status"] = "has_data"
            else:
                c["ctr"]         = None
                c["impressions"] = None
                c["views"]       = None
                c["performance_status"] = "awaiting_analytics"
        else:
            c["linked_platforms"] = []
            c["ctr"]              = None
            c["impressions"]      = None
            c["views"]            = None
            c["performance_status"] = "not_published"

        candidates.append(c)

    has_selected  = any(bool(c.get("selected")) for c in candidates)
    has_real_data = real_ctr is not None

    return {
        "candidates": candidates,
        "summary": {
            "count":          len(candidates),
            "has_selected":   has_selected,
            "has_real_data":  has_real_data,
        },
    }


# ---------------------------------------------------------------------------
# Titles tab data
# ---------------------------------------------------------------------------

def _classify_title_format(text: str) -> str:
    """
    Deterministic format classification based on title text only.

    Rules (applied in order):
        contains "?"                     → question
        contains "mystery" (case-insensitive) → mystery
        contains " vs " or "versus"      → vs_comparison
        first char is a digit            → list
        non-empty text                   → direct_statement
        empty / whitespace-only          → unknown
    """
    if not text:
        return "unknown"
    t  = text.strip()
    tl = t.lower()
    if not t:
        return "unknown"
    if "?" in t:
        return "question"
    if "mystery" in tl:
        return "mystery"
    if " vs " in tl or "versus" in tl:
        return "vs_comparison"
    if t[0].isdigit():
        return "list"
    return "direct_statement"


# Human-readable labels for each format type
_FORMAT_LABELS: dict[str, str] = {
    "question":        "Question",
    "mystery":         "Mystery",
    "vs_comparison":   "Comparison",
    "list":            "List",
    "direct_statement": "Statement",
    "unknown":         "Other",
}


def _build_titles_data(conn, episode_id: str) -> dict:
    """
    Build Titles tab data from titles + platform_analytics.

    format_type:  computed from title_text; not stored in schema.
    performance:  real analytics only (source != 'mock'), applied to selected
                  title only (non-selected titles were never published).

    Returns:
        {
          "candidates": [...],
          "summary": {"count": N, "has_selected": bool, "has_real_data": bool},
        }
    """
    # ── Title candidates ─────────────────────────────────────────────────
    title_rows = conn.execute(
        """
        SELECT title_id, run_id, episode_id, title_text, selected, created_at
        FROM titles
        WHERE episode_id = ?
        ORDER BY created_at ASC
        """,
        [episode_id],
    ).fetchall()

    if not title_rows:
        return {
            "candidates": [],
            "summary": {"count": 0, "has_selected": False, "has_real_data": False},
        }

    # ── Real analytics (same query as thumbnails) ─────────────────────────
    analytics_rows = conn.execute(
        """
        SELECT platform, ctr, impressions, views
        FROM platform_analytics
        WHERE episode_id = ? AND source != 'mock'
          AND ctr IS NOT NULL
        ORDER BY CASE platform WHEN 'youtube' THEN 0 ELSE 1 END ASC,
                 snapshot_date DESC
        """,
        [episode_id],
    ).fetchall()

    real_ctr: Optional[float]      = None
    real_impressions: Optional[int] = None
    real_views: Optional[int]       = None

    if analytics_rows:
        best = analytics_rows[0]
        real_ctr        = best["ctr"]
        real_impressions = int(best["impressions"]) if best["impressions"] else None
        real_views       = int(best["views"])       if best["views"]       else None

    # ── Build candidate dicts ─────────────────────────────────────────────
    candidates: list[dict] = []
    for row in title_rows:
        c        = _row(row)
        selected = bool(c.get("selected"))
        c["format_type"]   = _classify_title_format(c.get("title_text", ""))
        c["format_label"]  = _FORMAT_LABELS.get(c["format_type"], "Other")

        if selected:
            if real_ctr is not None:
                c["ctr"]         = round(real_ctr * 100, 2)
                c["impressions"] = real_impressions
                c["views"]       = real_views
                if real_ctr >= _CTR_WINNER_THRESHOLD:
                    c["performance_status"] = "winner"
                elif real_ctr < _CTR_UNDERPERFORMED_THRESHOLD:
                    c["performance_status"] = "underperformed"
                else:
                    c["performance_status"] = "has_data"
            else:
                c["ctr"]              = None
                c["impressions"]      = None
                c["views"]            = None
                c["performance_status"] = "awaiting_analytics"
        else:
            c["ctr"]              = None
            c["impressions"]      = None
            c["views"]            = None
            c["performance_status"] = "not_published"

        candidates.append(c)

    has_selected  = any(bool(c.get("selected")) for c in candidates)
    has_real_data = real_ctr is not None

    return {
        "candidates": candidates,
        "summary": {
            "count":         len(candidates),
            "has_selected":  has_selected,
            "has_real_data": has_real_data,
        },
    }
