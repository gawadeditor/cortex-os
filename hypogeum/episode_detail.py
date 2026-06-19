"""
hypogeum/episode_detail.py — Episode Detail page data layer (P0 / Step 1).

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
    for r in pub_rows:
        p = _row(r)
        p["status_label"] = _PUBLISH_STATUS_LABELS.get(
            p.get("publishing_status", "unknown"), "Unknown"
        )
        platforms.append(p)

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

        # Publishing
        "platforms": platforms,

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
