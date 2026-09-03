"""
hypogeum/episode_detail.py — Episode Detail page data layer (P0 / Steps 1–5).

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
    "complete":     "Complete",
    "completed":    "Complete",
    "qa_warning":   "QA Warning",
    "needs_review": "Needs Review",
    "partial":      "Partial",
    "failed":       "Failed",
    "running":      "In Production",
    "queued":       "Queued",
    "paused":       "Paused",
    "skipped":      "Skipped",
    "pending":      "Pending",
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

_CALL_TYPE_LABELS: dict[str, str] = {
    "text_generation":  "Text Generation",
    "image_generation": "Image Generation",
    "video_generation": "Video Generation",
    "audio_generation": "Audio Generation",
    "embedding":        "Embedding",
    "api_call":         "API Call",
}

# Learning tab — domain mapping for analytics_insights.affected_dim
_INSIGHT_DIM_TO_DOMAIN: dict[str, str] = {
    "thumbnail":       "thumbnail",
    "title":           "title",
    "hook":            "title",
    "pacing":          "topic",
    "topic":           "topic",
    "publishing_time": "platform",
    "duration":        "topic",
    "description":     "title",
}

# Learning tab — domain mapping for learning_records.signal_type
_SIGNAL_TYPE_TO_DOMAIN: dict[str, str] = {
    "performance":     "platform",
    "gate_outcome":    "topic",
    "cost_efficiency": "cost",
    "quality_verdict": "topic",
    "topic_score":     "topic",
    "thumbnail_ctr":   "thumbnail",
}

# Learning tab — action_type → readable verb
_ACTION_TYPE_LABELS: dict[str, str] = {
    "prefer":   "Repeat",
    "avoid":    "Avoid",
    "increase": "Increase",
    "decrease": "Reduce",
    "test":     "Test",
    "change":   "Change",
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


def _signal_to_confidence(signal_strength: float) -> str:
    """Convert a -1.0..+1.0 signal strength to Low / Medium / High label."""
    abs_s = abs(signal_strength)
    if abs_s >= 0.65:
        return "High"
    if abs_s >= 0.35:
        return "Medium"
    return "Low"


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
            return _build_detail(conn, episode_id, db_path=path)
        finally:
            conn.close()
    except Exception:
        return None


def _build_detail(conn, episode_id: str, db_path: Optional[Path] = None) -> Optional[dict]:
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

    # The `costs` rows above are the per-LLM-call breakdown only; they carry
    # no MUAPI (video/upscale) spend. Use the complete grant-ledger figure
    # for the headline number.
    try:
        from hypogeum.model_effective_pricing import episode_billed_spend
        _billed = episode_billed_spend(db_path=db_path, episode_ids=[episode_id])
        if episode_id in _billed:
            total_cost_usd = _billed[episode_id]
    except Exception:
        pass

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

    # Episodes published through Zernio have no publishing_outcomes row —
    # rebuild the per-platform outcome from the submission ledger.
    for plat, syn in _zernio_publish_outcomes(conn, episode_id).items():
        if plat not in pub_outcomes_by_platform:
            pub_outcomes_by_platform[plat] = syn
            platforms.append(syn)

    # ── Credential states (for Publishing tab connection_state) ─────────
    cred_states = _load_cred_states(conn)

    # ── Publishing tab data ─────────────────────────────────────────────
    publishing = _build_publishing_data(pub_outcomes_by_platform, cred_states)

    # ── Thumbnails tab data ──────────────────────────────────────────────
    thumbnails = _build_thumbnails_data(conn, episode_id, run_id)

    # ── Branding / short-native flag (needed by Titles + Branding tabs) ──
    _short_native = _is_short_native_episode(conn, run.get("channel_id"))

    # ── Titles tab data ──────────────────────────────────────────────────
    titles_data = _build_titles_data(
        conn, episode_id, short_native=_short_native, display_title=display_title,
    )

    # ── Analytics tab data ───────────────────────────────────────────────
    analytics_data = _build_analytics_data(conn, episode_id)

    # ── Revenue tab data ─────────────────────────────────────────────────
    revenue_data = _build_revenue_data(conn, episode_id, run_id, db_path=db_path)

    # ── Learning tab data ────────────────────────────────────────────────
    learning_data = _build_learning_data(conn, episode_id, run_id)

    # ── Branding tab data ─────────────────────────────────────────────────
    branding_data = _build_branding_data(
        conn, episode_id, run_id, short_native=_short_native,
    )

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

        # Analytics tab
        "analytics": analytics_data,

        # Revenue tab
        "revenue": revenue_data,

        # Learning tab
        "learning": learning_data,

        # Branding tab
        "branding": branding_data,

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
    Return {platform: credential_status}. status: "active" | "expired" |
    "revoked" | "none".

    A platform counts as connected when EITHER an active Zernio publishing
    binding exists for it (the current model — accounts are connected per
    channel in Zernio) OR a legacy direct-OAuth secret profile is active
    (kept only for older single-channel installs).
    """
    result = {p: "none" for p in _PLATFORM_ORDER}
    # ── Zernio publishing bindings (current) ──────────────────────────────
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT pa.platform
            FROM channel_provider_bindings b
            JOIN provider_accounts pa ON pa.provider_account_id = b.provider_account_id
            WHERE b.capability = 'publishing' AND b.enabled = 1
              AND pa.platform IN ('youtube','tiktok','instagram','facebook')
            """
        ).fetchall()
        for r in rows:
            result[r["platform"]] = "active"
    except Exception:
        pass
    # ── Legacy direct-OAuth secret profiles ──────────────────────────────
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
            if r["provider"] not in seen and result.get(r["provider"]) == "none":
                result[r["provider"]] = r["status"]
                seen.add(r["provider"])
    except Exception:
        pass
    return result


def _zernio_publish_outcomes(conn, episode_id: str) -> dict[str, dict]:
    """
    Synthesise per-platform publishing outcomes from the Zernio submission
    ledger (publishing_attempts + publishing_receipts) for episodes that
    published through Zernio and therefore have no publishing_outcomes row.
    Never raises.
    """
    out: dict[str, dict] = {}
    try:
        rows = conn.execute(
            """
            SELECT a.platform, a.state,
                   r.provider_publication_id, r.native_platform_content_id,
                   r.provider_status, r.acknowledged_at
            FROM publishing_attempts a
            LEFT JOIN publishing_receipts r ON r.attempt_id = a.attempt_id
            WHERE a.episode_id = ?
            ORDER BY a.updated_at DESC
            """,
            [episode_id],
        ).fetchall()
    except Exception:
        return out
    _STATE_MAP = {
        "succeeded": "success", "acknowledged": "success",
        "failed_definitive": "failed", "cancelled": None,
        "submitting": "pending", "prepared": "pending", "ambiguous": "pending",
    }
    for r in rows:
        plat = r["platform"]
        if plat in out:
            continue  # newest row wins
        mapped = _STATE_MAP.get((r["state"] or "").lower(), "pending")
        if mapped is None:
            continue
        out[plat] = {
            "platform": plat,
            "publishing_status": mapped,
            "platform_video_id": r["native_platform_content_id"] or r["provider_publication_id"],
            "published_at": r["acknowledged_at"],
            "url": None,
            "failure_reason": None,
            "status_label": _PUBLISH_STATUS_LABELS.get(mapped, "Unknown"),
        }
    return out


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


def _build_titles_data(conn, episode_id: str, *, short_native: bool = False,
                       display_title: Optional[str] = None) -> dict:
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

    Short-native episodes never run the long-form ``script_engine →
    title_candidates`` stage — each platform gets one optimised title
    generated at publish time, so there is no candidate set to compare.
    For those we return ``not_applicable`` with the published title.
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

    if not title_rows and short_native:
        _pub_title = (display_title or "").strip()
        return {
            "not_applicable": True,
            "na_reason": (
                "This is a short vertical video. Short-form videos get a single "
                "optimised title per platform, written when the video is published — "
                "there's no set of alternative titles to compare here."
            ),
            "published_title": _pub_title or None,
            "candidates": [],
            "summary": {"count": 0, "has_selected": False, "has_real_data": False},
        }

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


# ---------------------------------------------------------------------------
# Revenue tab data
# ---------------------------------------------------------------------------

def _build_revenue_data(conn, episode_id: str, run_id: Optional[str],
                        db_path: Optional[Path] = None) -> dict:
    """
    Build Revenue tab data for one episode.

    Cost data:  costs table (all rows for this episode_id).
    Revenue:    platform_analytics WHERE source != 'mock' AND revenue_usd IS NOT NULL
                Only if revenue_usd column exists; detected via PRAGMA table_info.
                Also checks for rpm/cpm for break-even estimation.

    Returns:
        {
          "summary": { total_cost_usd, total_revenue_usd, profit_usd, roi_pct,
                       break_even_views, break_even_reason,
                       has_cost_data, has_revenue_data },
          "cost_breakdown": [ {label, provider, stage, amount_usd, created_at} ],
          "platform_revenue": [ {platform, display_name, views, revenue_usd,
                                  rpm, cpm, source} ],
        }
    """
    import math

    # ── Detect optional revenue columns in platform_analytics ────────────
    try:
        col_rows = conn.execute("PRAGMA table_info(platform_analytics)").fetchall()
        col_names = {r["name"] for r in col_rows}
    except Exception:
        col_names = set()

    has_revenue_col = "revenue_usd" in col_names
    has_rpm_col     = "rpm"         in col_names
    has_cpm_col     = "cpm"         in col_names

    # ── Cost breakdown (all runs for this episode) ────────────────────────
    cost_rows: list[dict] = []
    total_cost_usd: Optional[float] = None
    has_cost_data = False

    try:
        rows = conn.execute(
            """
            SELECT provider, model, call_type, cost_usd, called_at, stage_name
            FROM costs
            WHERE episode_id = ?
            ORDER BY called_at ASC
            """,
            [episode_id],
        ).fetchall()

        if rows:
            has_cost_data = True
            cost_total = 0.0
            for r in rows:
                d = _row(r)
                stage_raw = d.get("stage_name") or ""
                group     = _STAGE_TO_GROUP.get(stage_raw) if stage_raw else None
                stage_display = (
                    group
                    or (stage_raw.replace("_", " ").title() if stage_raw else None)
                )
                call_type = d.get("call_type") or ""
                label     = _CALL_TYPE_LABELS.get(
                    call_type,
                    call_type.replace("_", " ").title() if call_type else "API Call",
                )
                amount = float(d.get("cost_usd") or 0.0)
                cost_total += amount
                cost_rows.append({
                    "label":      label,
                    "provider":   d.get("provider"),
                    "stage":      stage_display,
                    "amount_usd": round(amount, 6),
                    "created_at": d.get("called_at"),
                })
            total_cost_usd = round(cost_total, 6)
    except Exception:
        pass

    # costs rows are LLM-only — the ROI math needs the full bill (MUAPI too).
    try:
        from hypogeum.model_effective_pricing import episode_billed_spend
        _billed = episode_billed_spend(db_path=db_path, episode_ids=[episode_id])
        if episode_id in _billed:
            total_cost_usd = _billed[episode_id]
            has_cost_data = True
    except Exception:
        pass

    # ── Platform revenue (real rows only, revenue_usd column must exist) ──
    platform_revenue: list[dict] = []
    total_revenue_usd: Optional[float] = None
    has_revenue_data = False

    if has_revenue_col:
        try:
            extra_cols = ""
            if has_rpm_col:
                extra_cols += ", rpm"
            if has_cpm_col:
                extra_cols += ", cpm"

            rev_rows = conn.execute(
                f"""
                SELECT platform, views, revenue_usd{extra_cols}, source
                FROM platform_analytics
                WHERE episode_id = ? AND source != 'mock'
                  AND revenue_usd IS NOT NULL
                ORDER BY CASE platform
                         WHEN 'youtube' THEN 0 WHEN 'tiktok' THEN 1
                         WHEN 'instagram' THEN 2 ELSE 3 END ASC,
                         snapshot_date DESC
                """,
                [episode_id],
            ).fetchall()

            seen_plat: set = set()
            rev_total = 0.0
            for r in rev_rows:
                d  = _row(r)
                pl = d.get("platform", "")
                if pl in seen_plat:
                    continue
                seen_plat.add(pl)

                rev_usd = float(d.get("revenue_usd") or 0.0)
                rev_total += rev_usd
                has_revenue_data = True

                platform_revenue.append({
                    "platform":     pl,
                    "display_name": _PLATFORM_DISPLAY_NAMES.get(pl, pl.title()),
                    "views":        int(d["views"]) if d.get("views") is not None else None,
                    "revenue_usd":  round(rev_usd, 2),
                    "rpm": float(d["rpm"]) if has_rpm_col and d.get("rpm") is not None else None,
                    "cpm": float(d["cpm"]) if has_cpm_col and d.get("cpm") is not None else None,
                    "source": d.get("source"),
                })

            if has_revenue_data:
                total_revenue_usd = round(rev_total, 2)
        except Exception:
            pass

    # ── Derived financials ───────────────────────────────────────────────
    profit_usd:        Optional[float] = None
    roi_pct:           Optional[float] = None
    break_even_views:  Optional[int]   = None
    break_even_reason: Optional[str]   = None

    if (has_revenue_data
            and total_revenue_usd is not None
            and total_cost_usd is not None):
        profit_usd = round(total_revenue_usd - total_cost_usd, 2)
        if total_cost_usd > 0:
            roi_pct = round((profit_usd / total_cost_usd) * 100, 1)

    # Break-even: requires both revenue_usd column AND rpm or cpm column
    if has_revenue_col and (has_rpm_col or has_cpm_col):
        best_rate: Optional[float] = None
        for pr in platform_revenue:
            for rate_key in ("rpm", "cpm"):
                val = pr.get(rate_key)
                if val is not None and float(val) > 0:
                    if best_rate is None or float(val) > best_rate:
                        best_rate = float(val)
        if best_rate is not None and total_cost_usd is not None and total_cost_usd > 0:
            break_even_views = math.ceil(total_cost_usd / (best_rate / 1000))
        else:
            break_even_reason = "Break-even unavailable — CPM/RPM missing"
    else:
        break_even_reason = "Break-even unavailable — CPM/RPM missing"

    return {
        "summary": {
            "total_cost_usd":    total_cost_usd,
            "total_revenue_usd": total_revenue_usd,
            "profit_usd":        profit_usd,
            "roi_pct":           roi_pct,
            "break_even_views":  break_even_views,
            "break_even_reason": break_even_reason,
            "has_cost_data":     has_cost_data,
            "has_revenue_data":  has_revenue_data,
        },
        "cost_breakdown":   cost_rows,
        "platform_revenue": platform_revenue,
    }


# ---------------------------------------------------------------------------
# Learning tab data
# ---------------------------------------------------------------------------

def _build_learning_data(conn, episode_id: str, run_id: Optional[str]) -> dict:
    """
    Build Learning tab data for one episode.

    Sources (real data only — source != 'mock'):
        analytics_insights, learning_records, learning_actions,
        platform_analytics, thumbnail_candidates, titles, costs.

    Returns:
        {
          "summary":          {has_real_data, has_learning_records, overall_verdict,
                               what_worked, what_underperformed,
                               what_to_repeat, what_to_avoid},
          "thumbnail":        {selected_text, visual_prompt, ctr_pct, verdict,
                               recommendation, has_thumbnail},
          "title":            {selected_text, format_type, format_label, ctr_pct,
                               verdict, recommendation, has_title},
          "platform":         {has_data, best_by_views, best_by_ctr,
                               best_by_retention, platform_cards,
                               missing_platforms, missing_warning},
          "financial":        {has_cost_data, total_cost_usd, cost_verdict,
                               has_revenue_data, revenue_verdict},
          "recommendations":  [{domain, text, confidence, reason}],
        }
    """
    # ── Real analytics: latest snapshot per platform, source != 'mock' ────
    try:
        latest_rows = conn.execute(
            """
            SELECT pa.platform, pa.views, pa.ctr, pa.avg_view_pct,
                   pa.avg_watch_time_s, pa.likes, pa.comments, pa.shares
            FROM platform_analytics pa
            INNER JOIN (
                SELECT platform, MAX(snapshot_date) AS max_date
                FROM platform_analytics
                WHERE episode_id = ? AND source != 'mock'
                GROUP BY platform
            ) lx ON pa.platform = lx.platform AND pa.snapshot_date = lx.max_date
            WHERE pa.episode_id = ? AND pa.source != 'mock'
            """,
            [episode_id, episode_id],
        ).fetchall()
        real_by_platform: dict[str, dict] = {_row(r)["platform"]: _row(r) for r in latest_rows}
    except Exception:
        real_by_platform = {}

    has_real_data = bool(real_by_platform)

    # ── Learning records ──────────────────────────────────────────────────
    try:
        lr_rows = conn.execute(
            """
            SELECT signal_type, signal_value, signal_label, recommendation
            FROM learning_records
            WHERE episode_id = ?
            ORDER BY recorded_at DESC
            """,
            [episode_id],
        ).fetchall()
        learning_records_list = [_row(r) for r in lr_rows]
    except Exception:
        learning_records_list = []

    has_learning_records = bool(learning_records_list)

    # ── Analytics insights ────────────────────────────────────────────────
    try:
        ai_rows = conn.execute(
            """
            SELECT insight_type, affected_dim, signal_strength, threshold_used, detail
            FROM analytics_insights
            WHERE episode_id = ?
            ORDER BY ABS(signal_strength) DESC
            """,
            [episode_id],
        ).fetchall()
        insights = [_row(r) for r in ai_rows]
    except Exception:
        insights = []

    # ── Learning actions (for this episode) ───────────────────────────────
    try:
        la_rows = conn.execute(
            """
            SELECT action_id, dimension, action_type, rationale, status
            FROM learning_actions
            WHERE source_episode = ?
            ORDER BY created_at DESC
            """,
            [episode_id],
        ).fetchall()
        actions = [_row(r) for r in la_rows]
    except Exception:
        actions = []

    # ── Selected thumbnail ────────────────────────────────────────────────
    try:
        tn_row = conn.execute(
            """
            SELECT text_overlay, visual_prompt
            FROM thumbnail_candidates
            WHERE episode_id = ? AND selected = 1
            LIMIT 1
            """,
            [episode_id],
        ).fetchone()
        sel_thumbnail = _row(tn_row) if tn_row else {}
    except Exception:
        sel_thumbnail = {}

    # ── Selected title ────────────────────────────────────────────────────
    try:
        title_row = conn.execute(
            "SELECT title_text FROM titles WHERE episode_id = ? AND selected = 1 LIMIT 1",
            [episode_id],
        ).fetchone()
        sel_title_text: Optional[str] = title_row["title_text"] if title_row else None
    except Exception:
        sel_title_text = None

    # ── Cost total ────────────────────────────────────────────────────────
    total_cost_usd_l: Optional[float] = None
    try:
        cost_row = conn.execute(
            "SELECT SUM(cost_usd) AS total FROM costs WHERE episode_id = ?",
            [episode_id],
        ).fetchone()
        if cost_row and cost_row["total"] is not None:
            total_cost_usd_l = round(float(cost_row["total"]), 6)
    except Exception:
        pass

    # ── Best platform per metric ──────────────────────────────────────────
    best_by_views:     Optional[dict] = None
    best_by_ctr:       Optional[dict] = None
    best_by_retention: Optional[dict] = None

    if real_by_platform:
        views_pairs = [
            (p, int(d["views"])) for p, d in real_by_platform.items()
            if d.get("views") is not None
        ]
        ctr_pairs = [
            (p, float(d["ctr"])) for p, d in real_by_platform.items()
            if d.get("ctr") is not None
        ]
        ret_pairs = [
            (p, float(d["avg_view_pct"])) for p, d in real_by_platform.items()
            if d.get("avg_view_pct") is not None
        ]
        if views_pairs:
            best_p, best_v = max(views_pairs, key=lambda x: x[1])
            best_by_views = {
                "platform":     best_p,
                "display_name": _PLATFORM_DISPLAY_NAMES.get(best_p, best_p.title()),
                "value":        best_v,
                "label":        "views",
            }
        if ctr_pairs:
            best_p, best_v = max(ctr_pairs, key=lambda x: x[1])
            best_by_ctr = {
                "platform":     best_p,
                "display_name": _PLATFORM_DISPLAY_NAMES.get(best_p, best_p.title()),
                "value":        round(best_v * 100, 2),
                "label":        "CTR",
            }
        if ret_pairs:
            best_p, best_v = max(ret_pairs, key=lambda x: x[1])
            best_by_retention = {
                "platform":     best_p,
                "display_name": _PLATFORM_DISPLAY_NAMES.get(best_p, best_p.title()),
                "value":        round(best_v * 100, 1),
                "label":        "avg retention",
            }

    # ── Thumbnail verdict ─────────────────────────────────────────────────
    best_ctr_val: Optional[float] = None
    if real_by_platform:
        ctr_vals = [
            float(d["ctr"]) for d in real_by_platform.values()
            if d.get("ctr") is not None
        ]
        if ctr_vals:
            best_ctr_val = max(ctr_vals)

    ctr_pct_ep: Optional[float] = (
        round(best_ctr_val * 100, 2) if best_ctr_val is not None else None
    )

    if best_ctr_val is None:
        tn_verdict = "Waiting for analytics"
        tn_rec     = "Import analytics to see how this thumbnail performed."
    elif best_ctr_val >= _CTR_WINNER_THRESHOLD:
        tn_verdict = "Strong thumbnail"
        tn_rec     = "Keep this visual approach for similar topics."
    elif best_ctr_val >= _CTR_UNDERPERFORMED_THRESHOLD:
        tn_verdict = "Acceptable thumbnail"
        tn_rec     = "Test a stronger visual hook in the next episode."
    else:
        tn_verdict = "Weak thumbnail"
        tn_rec     = "Replace thumbnail approach — current style underperformed."

    prompt_preview = (sel_thumbnail.get("visual_prompt") or "")[:120] or None

    thumbnail_learning = {
        "selected_text":  sel_thumbnail.get("text_overlay"),
        "visual_prompt":  prompt_preview,
        "ctr_pct":        ctr_pct_ep,
        "verdict":        tn_verdict,
        "recommendation": tn_rec,
        "has_thumbnail":  bool(sel_thumbnail),
    }

    # ── Title verdict ─────────────────────────────────────────────────────
    title_fmt   = _classify_title_format(sel_title_text) if sel_title_text else "unknown"
    title_label = _FORMAT_LABELS.get(title_fmt, "Other")

    if best_ctr_val is None:
        title_verdict = "Waiting for analytics"
        title_rec     = "Import analytics to see how this title performed."
    elif best_ctr_val >= _CTR_WINNER_THRESHOLD:
        title_verdict = "Strong title"
        title_rec     = (
            f"This {title_label.lower()} format worked well — use similar approach."
        )
    elif best_ctr_val >= _CTR_UNDERPERFORMED_THRESHOLD:
        title_verdict = "Acceptable title"
        title_rec     = "Title performed adequately — refine the hook for better click-through."
    else:
        title_verdict = "Weak title"
        title_rec     = "Title underperformed — try a stronger hook or different format."

    title_learning = {
        "selected_text":  sel_title_text,
        "format_type":    title_fmt,
        "format_label":   title_label,
        "ctr_pct":        ctr_pct_ep,
        "verdict":        title_verdict,
        "recommendation": title_rec,
        "has_title":      bool(sel_title_text),
    }

    # ── Platform learning ─────────────────────────────────────────────────
    platform_cards: list[dict] = []
    for p_key in _PLATFORM_ORDER:
        real = real_by_platform.get(p_key)
        if not real:
            continue
        platform_cards.append({
            "platform":      p_key,
            "display_name":  _PLATFORM_DISPLAY_NAMES[p_key],
            "views":         int(real["views"]) if real.get("views") is not None else None,
            "ctr_pct":       round(float(real["ctr"]) * 100, 2) if real.get("ctr") is not None else None,
            "retention_pct": round(float(real["avg_view_pct"]) * 100, 1) if real.get("avg_view_pct") is not None else None,
            "likes":         int(real["likes"]) if real.get("likes") is not None else None,
        })

    missing_platforms = [
        _PLATFORM_DISPLAY_NAMES.get(p, p.title())
        for p in _PLATFORM_ORDER
        if p not in real_by_platform
    ]

    platform_learning = {
        "has_data":          has_real_data,
        "best_by_views":     best_by_views,
        "best_by_ctr":       best_by_ctr,
        "best_by_retention": best_by_retention,
        "platform_cards":    platform_cards,
        "missing_platforms": missing_platforms,
        "missing_warning":   bool(missing_platforms) and has_real_data,
    }

    # ── Financial learning ────────────────────────────────────────────────
    cost_verdict: Optional[str] = None
    if total_cost_usd_l is not None:
        if total_cost_usd_l < 0.10:
            cost_verdict = "Low production cost"
        elif total_cost_usd_l < 1.00:
            cost_verdict = "Moderate production cost"
        else:
            cost_verdict = "High production cost"

    revenue_verdict = "Revenue data not available yet"
    has_revenue_data_l = False
    try:
        col_rows = conn.execute("PRAGMA table_info(platform_analytics)").fetchall()
        col_names = {r["name"] for r in col_rows}
        if "revenue_usd" in col_names:
            rev_row = conn.execute(
                """
                SELECT SUM(revenue_usd) AS total
                FROM platform_analytics
                WHERE episode_id = ? AND source != 'mock'
                  AND revenue_usd IS NOT NULL
                """,
                [episode_id],
            ).fetchone()
            if rev_row and rev_row["total"] is not None:
                total_rev = float(rev_row["total"])
                has_revenue_data_l = True
                if total_cost_usd_l is not None:
                    profit = total_rev - total_cost_usd_l
                    if profit > 0:
                        revenue_verdict = (
                            f"Profitable — ${total_rev:.2f} revenue, ${profit:.2f} profit"
                        )
                    else:
                        revenue_verdict = (
                            f"Not yet profitable — ${total_rev:.2f} revenue,"
                            f" ${abs(profit):.2f} shortfall"
                        )
                else:
                    revenue_verdict = f"Revenue imported: ${total_rev:.2f}"
    except Exception:
        pass

    financial_learning = {
        "has_cost_data":   total_cost_usd_l is not None,
        "total_cost_usd":  total_cost_usd_l,
        "cost_verdict":    cost_verdict,
        "has_revenue_data": has_revenue_data_l,
        "revenue_verdict": revenue_verdict,
    }

    # ── Recommendations ───────────────────────────────────────────────────
    recommendations: list[dict] = []

    # From analytics_insights (strongest signals first)
    for ins in insights[:6]:
        strength = float(ins.get("signal_strength") or 0.0)
        if abs(strength) < 0.2:
            continue
        detail = ins.get("detail") or ins.get("threshold_used") or ""
        if not detail:
            continue
        domain = _INSIGHT_DIM_TO_DOMAIN.get(ins.get("affected_dim", ""), "topic")
        recommendations.append({
            "domain":     domain,
            "text":       detail,
            "confidence": _signal_to_confidence(strength),
            "reason":     (
                ins.get("insight_type", "analytics signal")
                .replace("_", " ").title()
            ),
        })

    # From learning_actions
    for act in actions[:4]:
        rationale = act.get("rationale") or ""
        if not rationale:
            continue
        dim       = act.get("dimension", "")
        verb      = _ACTION_TYPE_LABELS.get(act.get("action_type", ""), "Note")
        domain    = _INSIGHT_DIM_TO_DOMAIN.get(dim, dim or "topic")
        recommendations.append({
            "domain":     domain,
            "text":       f"{verb}: {rationale}",
            "confidence": "Medium",
            "reason":     "Learning engine recommendation",
        })

    # From learning_records (recommendation field)
    for lr in learning_records_list[:4]:
        rec = lr.get("recommendation") or ""
        if not rec:
            continue
        sig_type = lr.get("signal_type") or ""
        domain   = _SIGNAL_TYPE_TO_DOMAIN.get(sig_type, "topic")
        sig_val  = lr.get("signal_value")
        conf     = _signal_to_confidence(float(sig_val)) if sig_val is not None else "Medium"
        recommendations.append({
            "domain":     domain,
            "text":       rec,
            "confidence": conf,
            "reason":     sig_type.replace("_", " ").title() if sig_type else "Factory signal",
        })

    # Derived recommendations when no external signals are available
    if not recommendations:
        if best_ctr_val is None:
            recommendations.append({
                "domain":     "thumbnail",
                "text":       "Import analytics to evaluate thumbnail and title performance.",
                "confidence": "Low",
                "reason":     "No analytics data available yet",
            })
        elif best_ctr_val >= _CTR_WINNER_THRESHOLD:
            recommendations.append({
                "domain":     "thumbnail",
                "text":       "Replicate this thumbnail style — click-through exceeded target.",
                "confidence": "High",
                "reason":     f"CTR {round(best_ctr_val*100, 2)}% — above 5% threshold",
            })
            recommendations.append({
                "domain":     "title",
                "text":       (
                    f"Continue using {title_label.lower()} title format — performed well."
                ),
                "confidence": "Medium",
                "reason":     "CTR above threshold with this format",
            })
        elif best_ctr_val < _CTR_UNDERPERFORMED_THRESHOLD:
            recommendations.append({
                "domain":     "thumbnail",
                "text":       "Test a more visually distinct thumbnail for the next episode.",
                "confidence": "Medium",
                "reason":     f"CTR {round(best_ctr_val*100, 2)}% — below 2% threshold",
            })
            recommendations.append({
                "domain":     "title",
                "text":       "Try a stronger hook or a different title format next time.",
                "confidence": "Medium",
                "reason":     "Click-through rate below target",
            })
        else:
            recommendations.append({
                "domain":     "thumbnail",
                "text":       "Test a stronger visual hook in the next episode.",
                "confidence": "Low",
                "reason":     f"CTR {round(best_ctr_val*100, 2)}% — room to improve",
            })

        if best_by_views and len(platform_cards) > 1:
            recommendations.append({
                "domain":     "platform",
                "text":       (
                    f"Prioritise {best_by_views['display_name']}"
                    f" — highest views this episode."
                ),
                "confidence": "Medium",
                "reason":     f"{best_by_views['value']:,} views",
            })

        if total_cost_usd_l is not None and not has_revenue_data_l:
            recommendations.append({
                "domain":     "revenue",
                "text": (
                    "Import platform revenue data to calculate return on"
                    " production investment."
                ),
                "confidence": "Low",
                "reason": "Production cost tracked; revenue not yet imported",
            })

    # ── Summary: what worked / underperformed / repeat / avoid ───────────
    what_worked:        list[str] = []
    what_underperformed: list[str] = []
    what_to_repeat:     list[str] = []
    what_to_avoid:      list[str] = []
    overall_verdict: str

    if not has_real_data:
        overall_verdict = "Waiting for analytics"
    else:
        pos_insights = [i for i in insights if (i.get("signal_strength") or 0) > 0.2]
        neg_insights = [i for i in insights if (i.get("signal_strength") or 0) < -0.2]

        for ins in pos_insights[:3]:
            detail = ins.get("detail") or (
                ins.get("insight_type", "").replace("_", " ").title()
            )
            if detail:
                what_worked.append(detail)

        for ins in neg_insights[:3]:
            detail = ins.get("detail") or (
                ins.get("insight_type", "").replace("_", " ").title()
            )
            if detail:
                what_underperformed.append(detail)

        # Derive from analytics if no insights cover it
        if not what_worked and best_ctr_val is not None:
            if best_ctr_val >= _CTR_WINNER_THRESHOLD:
                what_worked.append(
                    f"Strong click-through rate ({round(best_ctr_val*100, 2)}%)"
                )
            elif best_by_views:
                what_worked.append(
                    f"Published successfully on {best_by_views['display_name']}"
                )

        if not what_underperformed and best_ctr_val is not None:
            if best_ctr_val < _CTR_UNDERPERFORMED_THRESHOLD:
                what_underperformed.append(
                    f"Click-through rate was low ({round(best_ctr_val*100, 2)}%)"
                    " — thumbnail or title may need improvement"
                )

        if best_ctr_val is not None and best_ctr_val >= _CTR_WINNER_THRESHOLD:
            what_to_repeat.append(
                f"Thumbnail style with {title_label.lower()} title format"
            )
        elif best_by_views:
            what_to_repeat.append(f"Publishing to {best_by_views['display_name']}")

        what_to_avoid = [
            ins.get("detail") for ins in neg_insights[:2]
            if ins.get("detail")
        ]

        # Overall verdict
        if best_ctr_val is not None and best_ctr_val >= _CTR_WINNER_THRESHOLD:
            overall_verdict = "Strong episode — above-target click-through"
        elif best_ctr_val is not None and best_ctr_val < _CTR_UNDERPERFORMED_THRESHOLD:
            overall_verdict = "Below target — review thumbnail and title"
        elif has_learning_records:
            overall_verdict = "Learning recorded — review recommendations below"
        else:
            overall_verdict = "Analytics available — lessons derived"

    return {
        "summary": {
            "has_real_data":         has_real_data,
            "has_learning_records":  has_learning_records,
            "overall_verdict":       overall_verdict,
            "what_worked":           what_worked,
            "what_underperformed":   what_underperformed,
            "what_to_repeat":        what_to_repeat,
            "what_to_avoid":         what_to_avoid,
        },
        "thumbnail":       thumbnail_learning,
        "title":           title_learning,
        "platform":        platform_learning,
        "financial":       financial_learning,
        "recommendations": recommendations,
    }


# ---------------------------------------------------------------------------
# Branding tab data
# ---------------------------------------------------------------------------

_BRANDING_ASSET_TYPES = [
    "logo", "logo_bug", "watermark", "intro", "outro",
    "lower_third", "channel_package", "thumbnail_template", "title_template",
]

_BRANDING_COMPLETE_THRESHOLD       = 90   # >= 90% → Complete
_BRANDING_MOSTLY_COMPLETE_THRESHOLD = 60  # >= 60% → Mostly Complete
_BRANDING_PARTIAL_THRESHOLD        = 30   # >= 30% → Partial


def _is_short_native_episode(conn, channel_id) -> bool:
    """True when the channel produces only short vertical videos
    (production_cadence.videos_per_week == 0 && shorts_per_week >= 1).
    Never raises."""
    if not channel_id:
        return False
    try:
        row = conn.execute(
            "SELECT production_cadence_json FROM channel_operations_profiles "
            "WHERE channel_id = ?", (channel_id,),
        ).fetchone()
        if not row or not row["production_cadence_json"]:
            return False
        import json as _json  # noqa: PLC0415
        cad = _json.loads(row["production_cadence_json"])
        return (int(cad.get("videos_per_week") or 0) == 0
                and int(cad.get("shorts_per_week") or 0) >= 1)
    except Exception:  # noqa: BLE001
        return False


def _build_branding_data(conn, episode_id: str, run_id: Optional[str],
                         *, short_native: bool = False) -> dict:
    """
    Build Branding tab data for one episode.

    Short vertical videos use each platform's own auto-generated cover and
    carry no logo bug / intro / outro / lower-third, so the full-length
    branding audit does not apply — return a clean "not applicable" state
    instead of flagging every asset as missing.

    Sources: assets, thumbnail_candidates, titles, publishing_outcomes.
    No mock analytics, no hallucinated judgments. Missing evidence stated explicitly.

    Returns:
        {
          "summary":         {assets_found, assets_missing, completeness_pct, overall_verdict},
          "thumbnail_audit": {thumbnail_exists, branding_markers, consistent_style, verdict},
          "title_audit":     {title_exists, title_text, style_classification,
                              matches_positioning, verdict},
          "platform_audit":  [{platform, display_name, published, custom_thumbnail,
                               title_present, description_present, branding_completeness}],
          "asset_inventory": [{asset_type, source, created_at, status}],
          "recommendations": [{domain, recommendation, confidence, reason}],
        }
    """
    if short_native:
        return {
            "not_applicable": True,
            "na_reason": (
                "This is a short vertical video. Short-form videos use each "
                "platform's own auto-generated cover and don't carry a logo "
                "bug, intro, outro or lower-third — there's nothing to check "
                "here."
            ),
            "summary": {
                "assets_found": 0, "assets_missing": 0,
                "completeness_pct": 100, "overall_verdict": "Not applicable",
            },
            "thumbnail_audit": None,
            "title_audit": None,
            "platform_audit": [],
            "asset_inventory": [],
            "recommendations": [],
        }

    # ── Asset Inventory ───────────────────────────────────────────────
    asset_inventory: list[dict] = []
    found_types: set = set()

    if run_id:
        placeholders = ",".join("?" * len(_BRANDING_ASSET_TYPES))
        try:
            rows = conn.execute(
                f"""
                SELECT asset_type, provider, file_path, created_at
                FROM assets
                WHERE run_id = ?
                  AND asset_type IN ({placeholders})
                ORDER BY created_at ASC
                """,
                [run_id] + list(_BRANDING_ASSET_TYPES),
            ).fetchall()
            for r in rows:
                d = _row(r)
                atype = d.get("asset_type", "")
                found_types.add(atype)
                asset_inventory.append({
                    "asset_type": atype,
                    "source":     d.get("provider") or "unknown",
                    "created_at": d.get("created_at"),
                    "status":     "available" if d.get("file_path") else "referenced",
                })
        except Exception:
            pass

    # ── Branding Overview ─────────────────────────────────────────────
    total_types    = len(_BRANDING_ASSET_TYPES)
    assets_found   = len(found_types)
    assets_missing = total_types - assets_found
    completeness_pct = round(assets_found / total_types * 100) if total_types else 0

    if completeness_pct >= _BRANDING_COMPLETE_THRESHOLD:
        overall_verdict = "Complete"
    elif completeness_pct >= _BRANDING_MOSTLY_COMPLETE_THRESHOLD:
        overall_verdict = "Mostly Complete"
    elif completeness_pct >= _BRANDING_PARTIAL_THRESHOLD:
        overall_verdict = "Partial"
    else:
        overall_verdict = "Missing"

    summary = {
        "assets_found":     assets_found,
        "assets_missing":   assets_missing,
        "completeness_pct": completeness_pct,
        "overall_verdict":  overall_verdict,
    }

    # ── Selected thumbnail ────────────────────────────────────────────
    sel_thumbnail: dict = {}
    try:
        tn_row = conn.execute(
            """
            SELECT text_overlay, visual_prompt
            FROM thumbnail_candidates
            WHERE episode_id = ? AND selected = 1
            LIMIT 1
            """,
            [episode_id],
        ).fetchone()
        sel_thumbnail = _row(tn_row) if tn_row else {}
    except Exception:
        pass

    # ── Thumbnail Branding Audit ──────────────────────────────────────
    if not sel_thumbnail:
        thumbnail_audit: dict = {
            "thumbnail_exists": False,
            "branding_markers": False,
            "consistent_style": None,
            "verdict":          "Not Available",
        }
    else:
        has_overlay = bool(sel_thumbnail.get("text_overlay"))
        has_prompt  = bool(sel_thumbnail.get("visual_prompt"))
        has_markers = has_overlay or has_prompt

        if has_overlay and has_prompt:
            tn_verdict = "Strong Branding"
        elif has_markers:
            tn_verdict = "Acceptable Branding"
        else:
            tn_verdict = "Weak Branding"

        thumbnail_audit = {
            "thumbnail_exists": True,
            "branding_markers": has_markers,
            "consistent_style": None,   # insufficient evidence without brand profile
            "verdict":          tn_verdict,
        }

    # ── Selected title ────────────────────────────────────────────────
    sel_title_text: Optional[str] = None
    try:
        title_row = conn.execute(
            "SELECT title_text FROM titles WHERE episode_id = ? AND selected = 1 LIMIT 1",
            [episode_id],
        ).fetchone()
        sel_title_text = title_row["title_text"] if title_row else None
    except Exception:
        pass

    # ── Title Branding Audit ──────────────────────────────────────────
    if not sel_title_text:
        title_audit: dict = {
            "title_exists":         False,
            "title_text":           None,
            "style_classification": None,
            "matches_positioning":  None,
            "verdict":              "Not Available",
        }
    else:
        fmt       = _classify_title_format(sel_title_text)
        fmt_label = _FORMAT_LABELS.get(fmt, "Other")

        if fmt in ("question", "mystery", "vs_comparison"):
            title_verdict = "On Brand"
        else:
            title_verdict = "Acceptable"

        title_audit = {
            "title_exists":         True,
            "title_text":           sel_title_text,
            "style_classification": fmt_label,
            "matches_positioning":  None,   # insufficient evidence without brand profile
            "verdict":              title_verdict,
        }

    # ── Publishing Branding Audit ─────────────────────────────────────
    pub_rows_raw: list = []
    try:
        pub_rows_raw = conn.execute(
            """
            SELECT platform, publishing_status
            FROM publishing_outcomes
            WHERE episode_id = ?
            """,
            [episode_id],
        ).fetchall()
    except Exception:
        pass

    pub_by_platform: dict[str, dict] = {_row(r)["platform"]: _row(r) for r in pub_rows_raw}

    has_sel_thumbnail = bool(sel_thumbnail)
    has_sel_title     = bool(sel_title_text)

    platform_audit: list[dict] = []
    for p in _PLATFORM_ORDER:
        pub          = pub_by_platform.get(p)
        is_published = pub is not None and pub.get("publishing_status") == "success"

        custom_thumbnail    = has_sel_thumbnail if is_published else None
        title_present       = has_sel_title
        description_present = None   # not stored in current schema

        if is_published:
            verifiable = [custom_thumbnail, title_present]
            score: Optional[int] = round(sum(1 for x in verifiable if x) / len(verifiable) * 100)
        else:
            score = None

        platform_audit.append({
            "platform":              p,
            "display_name":          _PLATFORM_DISPLAY_NAMES[p],
            "published":             is_published,
            "custom_thumbnail":      custom_thumbnail,
            "title_present":         title_present,
            "description_present":   description_present,
            "branding_completeness": score,
        })

    # ── Branding Recommendations ──────────────────────────────────────
    recommendations: list[dict] = []

    tn_v = thumbnail_audit["verdict"]
    if tn_v == "Not Available":
        recommendations.append({
            "domain":         "thumbnail",
            "recommendation": "No thumbnail selected. Select a branded thumbnail before publishing.",
            "confidence":     "High",
            "reason":         "No thumbnail candidate selected for this episode",
        })
    elif tn_v == "Weak Branding":
        recommendations.append({
            "domain":         "thumbnail",
            "recommendation": "Add text overlay or visual prompt to strengthen thumbnail branding.",
            "confidence":     "Medium",
            "reason":         "Thumbnail present but no branding markers found",
        })

    title_v = title_audit["verdict"]
    if title_v == "Not Available":
        recommendations.append({
            "domain":         "title",
            "recommendation": "No title selected. Choose a title to complete branding before publishing.",
            "confidence":     "High",
            "reason":         "No title candidate selected for this episode",
        })

    critical_missing = [t for t in ("logo", "intro", "outro") if t not in found_types]
    if critical_missing:
        recommendations.append({
            "domain":         "assets",
            "recommendation": (
                f"Critical branding assets missing: {', '.join(critical_missing)}."
                " Upload these to ensure consistent brand presentation."
            ),
            "confidence":     "High",
            "reason":         f"{len(critical_missing)} critical asset(s) not found for this episode",
        })
    elif assets_missing > 0:
        missing_names = [t for t in _BRANDING_ASSET_TYPES if t not in found_types]
        sample = ", ".join(missing_names[:3])
        suffix = "..." if len(missing_names) > 3 else ""
        recommendations.append({
            "domain":         "assets",
            "recommendation": f"Optional branding assets not found: {sample}{suffix}.",
            "confidence":     "Low",
            "reason":         f"{assets_missing} branding asset(s) not found",
        })

    low_completeness = [
        p for p in platform_audit
        if p["published"] and p["branding_completeness"] is not None
        and p["branding_completeness"] < 100
    ]
    if low_completeness:
        recommendations.append({
            "domain":         "publishing",
            "recommendation": "Ensure thumbnails and titles are consistent across all published platforms.",
            "confidence":     "Medium",
            "reason":         f"{len(low_completeness)} published platform(s) with incomplete branding",
        })

    if not recommendations:
        recommendations.append({
            "domain":         "branding",
            "recommendation": "Insufficient branding evidence.",
            "confidence":     "Low",
            "reason":         "No branding assets or selections found for this episode",
        })

    return {
        "summary":         summary,
        "thumbnail_audit": thumbnail_audit,
        "title_audit":     title_audit,
        "platform_audit":  platform_audit,
        "asset_inventory": asset_inventory,
        "recommendations": recommendations,
    }


# ---------------------------------------------------------------------------
# Analytics tab data
# ---------------------------------------------------------------------------

_ANALYTICS_STALE_DAYS = 7  # snapshots older than this are flagged stale


def _build_analytics_data(conn, episode_id: str) -> dict:
    """
    Build Analytics tab data for one episode.

    Real analytics:  source IN ('manual','api','csv')  — source != 'mock'
    Mock analytics:  source = 'mock' — counted in health panel ONLY, never in KPIs.

    For multi-snapshot episodes the LATEST snapshot per platform is used for
    metric display (avoids double-counting across 7d/28d windows).
    The EARLIEST snapshot date is shown as "first analytics" in Publishing
    Performance.

    Returns:
        {
          "overview":   aggregate totals/averages across all real platforms,
          "platforms":  per-platform breakdown (all 4 canonical platforms),
          "health":     row counts, missing platforms, stale flags,
        }
    """
    from datetime import date as _date

    today = _date.today()

    # ── Publishing outcomes for this episode ─────────────────────────────
    pub_rows = conn.execute(
        """
        SELECT platform, publishing_status, platform_video_id, published_at, url
        FROM publishing_outcomes
        WHERE episode_id = ?
        """,
        [episode_id],
    ).fetchall()
    pub_by_platform: dict[str, dict] = {_row(r)["platform"]: _row(r) for r in pub_rows}
    # Episodes published through Zernio have no publishing_outcomes row —
    # rebuild the publish state from the submission ledger (see
    # _zernio_publish_outcomes / the Publishing tab).
    for _plat, _syn in _zernio_publish_outcomes(conn, episode_id).items():
        pub_by_platform.setdefault(_plat, _syn)

    # ── Latest real snapshot per platform ───────────────────────────────
    latest_rows = conn.execute(
        """
        SELECT pa.platform, pa.views, pa.impressions, pa.ctr,
               pa.avg_view_pct, pa.avg_watch_time_s, pa.total_watch_time_s,
               pa.likes, pa.comments, pa.shares, pa.saves,
               pa.platform_video_id, pa.snapshot_date
        FROM platform_analytics pa
        INNER JOIN (
            SELECT platform, MAX(snapshot_date) AS max_date
            FROM platform_analytics
            WHERE episode_id = ? AND source != 'mock'
            GROUP BY platform
        ) lx ON pa.platform = lx.platform AND pa.snapshot_date = lx.max_date
        WHERE pa.episode_id = ? AND pa.source != 'mock'
        """,
        [episode_id, episode_id],
    ).fetchall()
    real_by_platform: dict[str, dict] = {_row(r)["platform"]: _row(r) for r in latest_rows}

    # ── Earliest snapshot per platform (for Publishing Performance) ──────
    first_rows = conn.execute(
        """
        SELECT platform, MIN(snapshot_date) AS first_snapshot
        FROM platform_analytics
        WHERE episode_id = ? AND source != 'mock'
        GROUP BY platform
        """,
        [episode_id],
    ).fetchall()
    first_snap: dict[str, str] = {r["platform"]: r["first_snapshot"] for r in first_rows}

    # ── Row counts (real vs mock) ────────────────────────────────────────
    counts = conn.execute(
        """
        SELECT
            SUM(CASE WHEN source != 'mock' THEN 1 ELSE 0 END) AS real_count,
            SUM(CASE WHEN source  = 'mock' THEN 1 ELSE 0 END) AS mock_count
        FROM platform_analytics
        WHERE episode_id = ?
        """,
        [episode_id],
    ).fetchone()
    real_row_count = int(counts["real_count"] or 0) if counts else 0
    mock_row_count = int(counts["mock_count"] or 0) if counts else 0

    # ── Per-platform breakdown ───────────────────────────────────────────
    missing_platforms: list[str] = []
    stale_platforms: list[str]   = []
    platform_cards: list[dict]   = []

    for p_key in _PLATFORM_ORDER:
        real = real_by_platform.get(p_key)
        pub  = pub_by_platform.get(p_key)

        published        = pub is not None and pub.get("publishing_status") == "success"
        has_data         = real is not None
        latest_snap      = real["snapshot_date"] if real else None

        # Staleness: real data exists but last snapshot is old
        is_stale = False
        if latest_snap:
            try:
                delta = (today - _date.fromisoformat(latest_snap)).days
                if delta >= _ANALYTICS_STALE_DAYS:
                    is_stale = True
                    stale_platforms.append(p_key)
            except Exception:
                pass

        # Missing: no real analytics data at all for this platform
        if not has_data:
            missing_platforms.append(p_key)

        # Days since publish
        days_since_publish: Optional[int] = None
        published_at = pub.get("published_at") if pub else None
        if published_at:
            try:
                days_since_publish = (today - _date.fromisoformat(published_at[:10])).days
            except Exception:
                pass

        # Derived metrics
        ctr_pct          = round(real["ctr"] * 100, 2) if real and real.get("ctr") is not None else None
        avg_retention    = round(real["avg_view_pct"] * 100, 1) if real and real.get("avg_view_pct") is not None else None
        total_watch_h    = round(real["total_watch_time_s"] / 3600, 1) if real and real.get("total_watch_time_s") is not None else None
        avg_watch_s      = real.get("avg_watch_time_s") if real else None

        # platform_video_id: prefer publishing_outcomes, fall back to analytics
        video_id = (pub.get("platform_video_id") if pub else None) or (real.get("platform_video_id") if real else None)

        platform_cards.append({
            "platform":            p_key,
            "display_name":        _PLATFORM_DISPLAY_NAMES[p_key],
            "published":           published,
            "published_at":        published_at,
            "platform_video_id":   video_id,
            "url":                 pub.get("url") if pub else None,
            "days_since_publish":  days_since_publish,
            "first_snapshot":      first_snap.get(p_key),
            "latest_snapshot":     latest_snap,
            "views":               int(real["views"]) if real and real.get("views") is not None else None,
            "impressions":         int(real["impressions"]) if real and real.get("impressions") is not None else None,
            "ctr_pct":             ctr_pct,
            "avg_retention_pct":   avg_retention,
            "total_watch_time_h":  total_watch_h,
            "avg_watch_time_s":    avg_watch_s,
            "likes":               int(real["likes"]) if real and real.get("likes") is not None else None,
            "comments":            int(real["comments"]) if real and real.get("comments") is not None else None,
            "shares":              int(real["shares"]) if real and real.get("shares") is not None else None,
            "saves":               int(real["saves"]) if real and real.get("saves") is not None else None,
            "has_data":            has_data,
            "is_stale":            is_stale,
        })

    # ── Overview: aggregate across platforms with real data ───────────────
    has_real = bool(real_by_platform)

    def _sum(field: str) -> Optional[int]:
        vals = [p[field] for p in platform_cards if p.get(field) is not None]
        return sum(vals) if vals else None

    def _avg(field: str) -> Optional[float]:
        vals = [p[field] for p in platform_cards if p.get(field) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    overview: dict = {
        "total_views":         _sum("views"),
        "total_impressions":   _sum("impressions"),
        "avg_ctr":             _avg("ctr_pct"),
        "total_watch_time_h":  _sum("total_watch_time_h"),
        "avg_view_duration_s": _avg("avg_watch_time_s"),
        "avg_retention_pct":   _avg("avg_retention_pct"),
        "total_likes":         _sum("likes"),
        "total_comments":      _sum("comments"),
        "total_shares":        _sum("shares"),
        "has_real_data":       has_real,
    }

    return {
        "overview":  overview,
        "platforms": platform_cards,
        "health": {
            "real_row_count":    real_row_count,
            "mock_row_count":    mock_row_count,
            "has_mock":          mock_row_count > 0,
            "missing_platforms": missing_platforms,
            "stale_platforms":   stale_platforms,
        },
    }
