"""
hypogeum/analytics_store.py — Phase E: Analytics & Learning Store

CRUD layer for:
    platform_analytics  — normalised audience metrics per episode × platform
    analytics_insights  — signals extracted from platform_analytics by the
                          learning engine
    learning_actions    — concrete factory-change recommendations

Import pathways (no API credentials required):
    seed_mock_analytics()    — deterministic synthetic data (demo / CI)
    import_from_dict()       — operator pastes JSON from platform export
    import_from_csv()        — operator pastes CSV from platform dashboard

All functions are non-blocking and return a Result dict
{"ok": True/False, ...} for write operations.

Public API:
    store_platform_metrics(episode_id, platform, metrics, ...)  -> str (record_id)
    get_episode_analytics(episode_id, db_path)                  -> dict
    list_analytics(platform, db_path, limit)                    -> list[dict]
    import_from_dict(episode_id, platform, raw, source, db_path) -> Result
    import_from_csv(episode_id, platform, csv_text, db_path)    -> Result
    seed_mock_analytics(episode_id, platform, db_path)          -> Result

    store_insight(insight_dict, db_path)                        -> str
    get_insight(insight_id, db_path)                            -> dict | None
    list_insights(episode_id, platform, db_path)                -> list[dict]

    store_action(action_dict, db_path)                          -> str
    get_action(action_id, db_path)                              -> dict | None
    list_actions(status, db_path)                               -> list[dict]
    update_action_status(action_id, status, db_path)            -> bool
"""

from __future__ import annotations

import csv
import hashlib
import io
import json as _json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_DB      = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

VALID_PLATFORMS  = frozenset({"youtube", "tiktok", "instagram", "facebook"})
VALID_SOURCES    = frozenset({"manual", "api", "csv", "mock"})
VALID_STATUSES   = frozenset({"pending", "applied", "dismissed"})
VALID_DIMENSIONS = frozenset({
    "thumbnail", "title", "hook", "pacing", "topic",
    "publishing_time", "duration", "description",
    "content_volume",   # channel weekly-cadence recommendation (ADR-016 era)
    # script/video (2026-09-05): the two comparative-only dimensions —
    # see hypogeum.comparative_insights's own module docstring for why
    # these never fit a single-episode absolute-threshold rule the way
    # "hook"/"pacing" do, and only exist relative to another episode of
    # the same channel.
    "script", "video",
})
VALID_ACTION_TYPES = frozenset({"prefer", "avoid", "increase", "decrease", "test", "change"})

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _db(db_path: Optional[Path]) -> Optional[Path]:
    return Path(db_path) if db_path is not None else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _ok(data=None, **kw) -> dict:
    r: dict = {"ok": True}
    if data is not None:
        r["data"] = data
    r.update(kw)
    return r


def _err(error: str, code: str = "error") -> dict:
    return {"ok": False, "error": error, "code": code}


def _row(r) -> dict:
    return dict(r) if r else {}


def _coerce_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).replace(",", "")))
    except (ValueError, TypeError):
        return None


def _coerce_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except (ValueError, TypeError):
        return None


def _parse_duration(s) -> Optional[float]:
    """Parse 'HH:MM:SS', 'MM:SS', or numeric seconds → float seconds."""
    if s is None or s == "":
        return None
    s = str(s).strip()
    try:
        return float(s)
    except ValueError:
        pass
    parts = s.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except (ValueError, IndexError):
        pass
    return None


# ---------------------------------------------------------------------------
# Platform field maps: platform-specific column/key name → normalised field
# Special values beginning with "_" indicate a transform is needed.
# ---------------------------------------------------------------------------

# Each entry: raw_key → target_field | "_ctr_pct" | "_view_pct" | "_dur_str" | "_hours"
_FIELD_MAP: dict[str, dict[str, str]] = {
    "youtube": {
        # Views
        "views": "views", "Views": "views", "view_count": "views",
        "Video views": "views",
        # Impressions
        "impressions": "impressions", "Impressions": "impressions",
        # CTR — expressed as percentage (4.5 = 4.5%) → divide by 100
        "ctr": "ctr",
        "Impressions click-through rate (%)": "_ctr_pct",
        "Click-through rate (%)": "_ctr_pct",
        "CTR (%)": "_ctr_pct",
        "impressions_ctr": "_ctr_pct",
        # Avg view pct — percentage → decimal
        "avg_view_pct": "avg_view_pct",
        "Average view percentage (%)": "_view_pct",
        "Average view percentage": "_view_pct",
        "averageViewPercentage": "_view_pct",
        # Avg watch duration — "MM:SS" or seconds
        "avg_watch_time_s": "avg_watch_time_s",
        "Average view duration": "_dur_str",
        "averageViewDuration": "_dur_str",
        # Total watch time — hours → seconds
        "total_watch_time_s": "total_watch_time_s",
        "Watch time (hours)": "_hours",
        "estimatedMinutesWatched": "_minutes",
        # Social
        "likes": "likes", "Likes": "likes", "like_count": "likes",
        "comments": "comments", "Comments added": "comments",
        "Comments": "comments", "comment_count": "comments",
        "shares": "shares", "Shares": "shares", "share_count": "shares",
        # ID
        "platform_video_id": "platform_video_id",
        "Video ID": "platform_video_id",
    },
    "tiktok": {
        "views": "views", "Video views": "views",
        "Plays": "views", "play_count": "views",
        "avg_view_pct": "avg_view_pct",
        "Video completion rate (%)": "_view_pct",
        "Completion rate (%)": "_view_pct",
        "avg_watch_time_s": "avg_watch_time_s",
        "Average watch time per video (seconds)": "avg_watch_time_s",
        "likes": "likes", "Likes": "likes",
        "comments": "comments", "Comments": "comments",
        "shares": "shares", "Shares": "shares",
        "saves": "saves", "Saves": "saves",
        "platform_video_id": "platform_video_id",
        "Video ID": "platform_video_id",
    },
    "instagram": {
        "views": "views", "Plays": "views", "Video plays": "views",
        "impressions": "impressions", "Impressions": "impressions",
        "Reach": "impressions",
        "avg_view_pct": "avg_view_pct",
        "Video completion rate (%)": "_view_pct",
        "Completion (%)": "_view_pct",
        "avg_watch_time_s": "avg_watch_time_s",
        "likes": "likes", "Likes": "likes",
        "comments": "comments", "Comments": "comments",
        "shares": "shares", "Shares": "shares",
        "saves": "saves", "Saves": "saves",
        "Bookmarks": "saves",
        "platform_video_id": "platform_video_id",
    },
    "facebook": {
        "views": "views", "Video views": "views",
        "impressions": "impressions", "Reach": "impressions",
        "avg_watch_time_s": "avg_watch_time_s",
        "Average video watch time (seconds)": "avg_watch_time_s",
        "avg_view_pct": "avg_view_pct",
        "likes": "likes", "Reactions": "likes", "Post reactions": "likes",
        "comments": "comments", "Comments": "comments",
        "Post comments": "comments",
        "shares": "shares", "Shares": "shares", "Post shares": "shares",
        "platform_video_id": "platform_video_id",
    },
}

# Integer fields
_INT_FIELDS = frozenset({
    "views", "impressions", "likes", "comments", "shares", "saves",
})

# Float fields
_FLOAT_FIELDS = frozenset({
    "ctr", "avg_view_pct", "avg_watch_time_s", "total_watch_time_s",
})


def _normalise_field(target: str, raw_val) -> tuple[str, object]:
    """Apply special transforms; return (final_field_name, coerced_value)."""
    if target == "_ctr_pct":
        v = _coerce_float(raw_val)
        return "ctr", (round(v / 100.0, 6) if v is not None else None)
    if target == "_view_pct":
        v = _coerce_float(raw_val)
        return "avg_view_pct", (round(v / 100.0, 6) if v is not None else None)
    if target == "_dur_str":
        return "avg_watch_time_s", _parse_duration(raw_val)
    if target == "_hours":
        v = _coerce_float(raw_val)
        return "total_watch_time_s", (round(v * 3600, 1) if v is not None else None)
    if target == "_minutes":
        v = _coerce_float(raw_val)
        return "total_watch_time_s", (round(v * 60, 1) if v is not None else None)
    # Direct fields
    if target in _INT_FIELDS:
        return target, _coerce_int(raw_val)
    if target in _FLOAT_FIELDS:
        return target, _coerce_float(raw_val)
    return target, raw_val


def _map_raw(platform: str, raw: dict) -> dict:
    """Map platform-specific keys to normalised schema fields."""
    field_map = _FIELD_MAP.get(platform, {})
    out: dict = {}
    for raw_key, raw_val in raw.items():
        target = field_map.get(raw_key)
        if target is None:
            # Try as-is: might already be a normalised field name
            if raw_key in _INT_FIELDS:
                out[raw_key] = _coerce_int(raw_val)
            elif raw_key in _FLOAT_FIELDS:
                out[raw_key] = _coerce_float(raw_val)
            elif raw_key == "platform_video_id":
                out["platform_video_id"] = str(raw_val) if raw_val else None
            # else: unknown field → goes to raw_json
        else:
            field, value = _normalise_field(target, raw_val)
            if value is not None:
                out[field] = value
    return out


# ---------------------------------------------------------------------------
# Mock data (deterministic, episode_id + platform seed)
# ---------------------------------------------------------------------------

def _seed_int(h: int, offset: int, lo: int, hi: int) -> int:
    frac = ((h >> offset) & 0xFFFF) / 0xFFFF
    return int(lo + (hi - lo) * frac)


def _seed_float(h: int, offset: int, lo: float, hi: float) -> float:
    frac = ((h >> offset) & 0xFFFF) / 0xFFFF
    return lo + (hi - lo) * frac


_MOCK_RANGES: dict[str, dict] = {
    "youtube": dict(
        views=(2_000, 45_000),
        impressions_mult=(8, 18),   # impressions = views * mult
        ctr=(0.012, 0.085),
        avg_view_pct=(0.22, 0.70),
        avg_watch_time_base=600,    # 10 min video assumption
        likes_rate=(0.02, 0.07),
        comments_rate=(0.003, 0.010),
        shares_rate=(0.004, 0.015),
    ),
    "tiktok": dict(
        views=(800, 95_000),
        avg_view_pct=(0.12, 0.60),
        avg_watch_time_base=55,     # ~55s TikTok video
        likes_rate=(0.04, 0.15),
        comments_rate=(0.005, 0.020),
        shares_rate=(0.010, 0.050),
        saves_rate=(0.005, 0.025),
    ),
    "instagram": dict(
        views=(400, 22_000),
        impressions_mult=(1.2, 2.2),
        avg_view_pct=(0.18, 0.62),
        avg_watch_time_base=45,
        likes_rate=(0.03, 0.10),
        comments_rate=(0.002, 0.010),
        saves_rate=(0.010, 0.055),
        shares_rate=(0.004, 0.022),
    ),
    "facebook": dict(
        views=(250, 16_000),
        impressions_mult=(2.0, 5.0),
        avg_watch_time=(5.0, 65.0),
        avg_video_len=300,          # 5 min assumed
        likes_rate=(0.008, 0.045),
        comments_rate=(0.001, 0.006),
        shares_rate=(0.004, 0.018),
    ),
}


def seed_mock_analytics(
    episode_id: str,
    platform:   str,
    db_path:    Optional[Path] = None,
) -> dict:
    """
    Insert deterministic synthetic analytics for episode_id on platform.

    Values are derived from hash(episode_id + platform) so the same
    episode always gets the same mock data. Safe to call repeatedly
    (idempotent: upserts on episode_id × platform × snapshot_date).

    No API calls. No credentials required.
    """
    if platform not in VALID_PLATFORMS:
        return _err(f"unknown platform {platform!r}", "invalid")

    h = int(hashlib.md5(f"{episode_id}:{platform}".encode()).hexdigest(), 16)
    r = _MOCK_RANGES[platform]
    snapshot_date = _today()
    metrics: dict = {}

    if platform == "youtube":
        views = _seed_int(h,  0, *r["views"])
        ctr   = _seed_float(h, 16, *r["ctr"])
        avp   = _seed_float(h, 32, *r["avg_view_pct"])
        awt   = round(avp * r["avg_watch_time_base"], 1)
        mult  = _seed_float(h, 48, *r["impressions_mult"])
        metrics = {
            "views":              views,
            "impressions":        int(views * mult),
            "ctr":                round(ctr, 4),
            "avg_view_pct":       round(avp, 4),
            "avg_watch_time_s":   awt,
            "total_watch_time_s": round(views * awt, 0),
            "likes":              int(views * _seed_float(h, 64,  *r["likes_rate"])),
            "comments":           int(views * _seed_float(h, 80,  *r["comments_rate"])),
            "shares":             int(views * _seed_float(h, 96,  *r["shares_rate"])),
        }

    elif platform == "tiktok":
        views = _seed_int(h,   0, *r["views"])
        avp   = _seed_float(h, 16, *r["avg_view_pct"])
        awt   = round(avp * r["avg_watch_time_base"], 1)
        metrics = {
            "views":            views,
            "avg_view_pct":     round(avp, 4),
            "avg_watch_time_s": awt,
            "total_watch_time_s": round(views * awt, 0),
            "likes":    int(views * _seed_float(h, 32, *r["likes_rate"])),
            "comments": int(views * _seed_float(h, 48, *r["comments_rate"])),
            "shares":   int(views * _seed_float(h, 64, *r["shares_rate"])),
            "saves":    int(views * _seed_float(h, 80, *r["saves_rate"])),
        }

    elif platform == "instagram":
        views = _seed_int(h,   0, *r["views"])
        avp   = _seed_float(h, 16, *r["avg_view_pct"])
        awt   = round(avp * r["avg_watch_time_base"], 1)
        mult  = _seed_float(h, 32, *r["impressions_mult"])
        metrics = {
            "views":              views,
            "impressions":        int(views * mult),
            "avg_view_pct":       round(avp, 4),
            "avg_watch_time_s":   awt,
            "total_watch_time_s": round(views * awt, 0),
            "likes":    int(views * _seed_float(h, 48, *r["likes_rate"])),
            "comments": int(views * _seed_float(h, 64, *r["comments_rate"])),
            "saves":    int(views * _seed_float(h, 80, *r["saves_rate"])),
            "shares":   int(views * _seed_float(h, 96, *r["shares_rate"])),
        }

    elif platform == "facebook":
        views = _seed_int(h,   0, *r["views"])
        awt   = _seed_float(h, 16, *r["avg_watch_time"])
        avp   = round(awt / r["avg_video_len"], 4)
        mult  = _seed_float(h, 32, *r["impressions_mult"])
        metrics = {
            "views":              views,
            "impressions":        int(views * mult),
            "avg_watch_time_s":   round(awt, 1),
            "avg_view_pct":       min(1.0, avp),
            "total_watch_time_s": round(views * awt, 0),
            "likes":    int(views * _seed_float(h, 48, *r["likes_rate"])),
            "comments": int(views * _seed_float(h, 64, *r["comments_rate"])),
            "shares":   int(views * _seed_float(h, 80, *r["shares_rate"])),
        }

    record_id = store_platform_metrics(
        episode_id=episode_id,
        platform=platform,
        metrics=metrics,
        source="mock",
        snapshot_date=snapshot_date,
        db_path=db_path,
    )
    return _ok(record_id=record_id, episode_id=episode_id,
               platform=platform, metrics=metrics)


# ===========================================================================
# platform_analytics CRUD
# ===========================================================================

def store_platform_metrics(
    episode_id:    str,
    platform:      str,
    metrics:       dict,
    source:        str            = "manual",
    snapshot_date: Optional[str]  = None,
    window_days:   int            = 7,
    db_path:       Optional[Path] = None,
) -> str:
    """
    Upsert one analytics record. Returns record_id.
    Idempotent: same (episode_id, platform, snapshot_date) → same record_id.
    """
    if platform not in VALID_PLATFORMS:
        raise ValueError(f"Unknown platform {platform!r}")
    initialize_schema(db_path)
    snap_date = snapshot_date or _today()
    now = _now_iso()

    # Look up existing record_id for this (episode, platform, date)
    conn = get_connection(_db(db_path))
    try:
        row = conn.execute(
            "SELECT record_id FROM platform_analytics "
            "WHERE episode_id=? AND platform=? AND snapshot_date=?",
            [episode_id, platform, snap_date],
        ).fetchone()
        record_id = row["record_id"] if row else _new_id()

        conn.execute(
            """INSERT OR REPLACE INTO platform_analytics
               (record_id, episode_id, platform, snapshot_date, window_days,
                views, impressions, ctr, avg_view_pct, avg_watch_time_s,
                total_watch_time_s, likes, comments, shares, saves,
                reach, follows, reposts, engagement_rate, skip_rate,
                platform_video_id, raw_json, source, imported_at, metadata_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record_id, episode_id, platform, snap_date, window_days,
                metrics.get("views"),
                metrics.get("impressions"),
                metrics.get("ctr"),
                metrics.get("avg_view_pct"),
                metrics.get("avg_watch_time_s"),
                metrics.get("total_watch_time_s"),
                metrics.get("likes"),
                metrics.get("comments"),
                metrics.get("shares"),
                metrics.get("saves"),
                metrics.get("reach"),
                metrics.get("follows"),
                metrics.get("reposts"),
                metrics.get("engagement_rate"),
                metrics.get("skip_rate"),
                metrics.get("platform_video_id"),
                _json.dumps(metrics.get("raw_json", {})) if metrics.get("raw_json") else None,
                source,
                now,
                "v1",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return record_id


def get_episode_analytics(
    episode_id: str,
    db_path:    Optional[Path] = None,
) -> dict:
    """
    Return all analytics records for one episode, keyed by platform.
    Each platform entry is the latest snapshot (by snapshot_date).
    Returns {} if no records found.
    """
    try:
        conn = get_connection(_db(db_path))
        try:
            rows = conn.execute("""
                SELECT * FROM platform_analytics
                WHERE episode_id=?
                ORDER BY platform, snapshot_date DESC
            """, [episode_id]).fetchall()
        finally:
            conn.close()

        out: dict = {}
        for r in rows:
            d = _row(r)
            p = d["platform"]
            if p not in out:   # first row = latest snapshot
                out[p] = d
        return out
    except Exception:
        return {}


def list_analytics(
    platform: Optional[str] = None,
    db_path:  Optional[Path] = None,
    limit:    int             = 100,
) -> list[dict]:
    """
    All analytics records, optionally filtered by platform, newest first.
    """
    try:
        conn = get_connection(_db(db_path))
        try:
            if platform:
                rows = conn.execute(
                    "SELECT * FROM platform_analytics WHERE platform=? "
                    "ORDER BY snapshot_date DESC, imported_at DESC LIMIT ?",
                    [platform, limit],
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM platform_analytics "
                    "ORDER BY snapshot_date DESC, imported_at DESC LIMIT ?",
                    [limit],
                ).fetchall()
        finally:
            conn.close()
        return [_row(r) for r in rows]
    except Exception:
        return []


def import_from_dict(
    episode_id: str,
    platform:   str,
    raw:        dict,
    source:     str            = "manual",
    db_path:    Optional[Path] = None,
) -> dict:
    """
    Normalise raw platform dict and store. Returns Result dict.
    Accepts both normalised field names and platform-specific names.
    """
    if platform not in VALID_PLATFORMS:
        return _err(f"unknown platform {platform!r}", "invalid")
    if not isinstance(raw, dict):
        return _err("raw must be a dict", "invalid")

    normalised = _map_raw(platform, raw)
    if not normalised:
        return _err("no recognisable metric fields found", "invalid")

    snapshot_date = raw.get("snapshot_date") or _today()
    window_days   = int(raw.get("window_days", 7))

    try:
        record_id = store_platform_metrics(
            episode_id=episode_id,
            platform=platform,
            metrics=normalised,
            source=source,
            snapshot_date=snapshot_date,
            window_days=window_days,
            db_path=db_path,
        )
        return _ok(record_id=record_id, fields_imported=list(normalised.keys()))
    except Exception as exc:
        return _err(str(exc))


def import_from_csv(
    episode_id: str,
    platform:   str,
    csv_text:   str,
    db_path:    Optional[Path] = None,
) -> dict:
    """
    Parse a CSV string (platform export) and import each data row as a
    separate analytics record.  Returns Result with imported_count.
    """
    if platform not in VALID_PLATFORMS:
        return _err(f"unknown platform {platform!r}", "invalid")

    try:
        reader = csv.DictReader(io.StringIO(csv_text.strip()))
        rows = list(reader)
    except Exception as exc:
        return _err(f"CSV parse error: {exc}", "invalid")

    if not rows:
        return _err("CSV has no data rows", "invalid")

    imported, errors = 0, []
    for i, row in enumerate(rows):
        result = import_from_dict(episode_id, platform, dict(row), source="csv", db_path=db_path)
        if result["ok"]:
            imported += 1
        else:
            errors.append(f"row {i+1}: {result['error']}")

    if imported == 0:
        return _err("; ".join(errors) or "no rows imported", "invalid")
    return _ok(imported_count=imported, error_count=len(errors), errors=errors[:5])


# ===========================================================================
# analytics_insights CRUD
# ===========================================================================

def store_insight(
    insight: dict,
    db_path: Optional[Path] = None,
) -> str:
    """Insert one insight record. Returns insight_id."""
    initialize_schema(db_path)
    insight_id = insight.get("insight_id") or _new_id()
    conn = get_connection(_db(db_path))
    try:
        conn.execute(
            """INSERT OR IGNORE INTO analytics_insights
               (insight_id, episode_id, platform, created_at,
                insight_type, affected_dim, signal_strength,
                threshold_used, detail, metadata_version)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                insight_id,
                insight["episode_id"],
                insight["platform"],
                insight.get("created_at") or _now_iso(),
                insight["insight_type"],
                insight["affected_dim"],
                float(insight["signal_strength"]),
                insight.get("threshold_used"),
                insight.get("detail"),
                "v1",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return insight_id


def get_insight(
    insight_id: str,
    db_path:    Optional[Path] = None,
) -> Optional[dict]:
    try:
        conn = get_connection(_db(db_path))
        try:
            row = conn.execute(
                "SELECT * FROM analytics_insights WHERE insight_id=?", [insight_id]
            ).fetchone()
        finally:
            conn.close()
        return _row(row) if row else None
    except Exception:
        return None


def list_insights(
    episode_id: Optional[str] = None,
    platform:   Optional[str] = None,
    db_path:    Optional[Path] = None,
) -> list[dict]:
    try:
        clauses, params = [], []
        if episode_id:
            clauses.append("episode_id=?")
            params.append(episode_id)
        if platform:
            clauses.append("platform=?")
            params.append(platform)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        conn = get_connection(_db(db_path))
        try:
            rows = conn.execute(
                f"SELECT * FROM analytics_insights {where} "
                f"ORDER BY created_at DESC",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [_row(r) for r in rows]
    except Exception:
        return []


# ===========================================================================
# learning_actions CRUD
# ===========================================================================

def store_action(
    action:  dict,
    db_path: Optional[Path] = None,
) -> str:
    """Insert one learning_action. Returns action_id."""
    initialize_schema(db_path)
    action_id = action.get("action_id") or _new_id()
    conn = get_connection(_db(db_path))
    try:
        evidence = action.get("evidence_json")
        if isinstance(evidence, dict):
            evidence = _json.dumps(evidence)
        conn.execute(
            """INSERT OR IGNORE INTO learning_actions
               (action_id, created_at, source_episode, dimension,
                action_type, rationale, evidence_json, status,
                metadata_version)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                action_id,
                action.get("created_at") or _now_iso(),
                action.get("source_episode"),
                action["dimension"],
                action["action_type"],
                action["rationale"],
                evidence,
                action.get("status", "pending"),
                "v1",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return action_id


def get_action(
    action_id: str,
    db_path:   Optional[Path] = None,
) -> Optional[dict]:
    try:
        conn = get_connection(_db(db_path))
        try:
            row = conn.execute(
                "SELECT * FROM learning_actions WHERE action_id=?", [action_id]
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        d = _row(row)
        if d.get("evidence_json"):
            try:
                d["evidence"] = _json.loads(d["evidence_json"])
            except Exception:
                d["evidence"] = None
        return d
    except Exception:
        return None


def list_actions(
    status:  Optional[str] = None,
    db_path: Optional[Path] = None,
    limit:   int            = 200,
) -> list[dict]:
    try:
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        conn = get_connection(_db(db_path))
        try:
            rows = conn.execute(
                f"SELECT * FROM learning_actions {where} "
                f"ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            conn.close()
        result = []
        for r in rows:
            d = _row(r)
            if d.get("evidence_json"):
                try:
                    d["evidence"] = _json.loads(d["evidence_json"])
                except Exception:
                    d["evidence"] = None
            result.append(d)
        return result
    except Exception:
        return []


def update_action_status(
    action_id: str,
    status:    str,
    db_path:   Optional[Path] = None,
) -> bool:
    if status not in VALID_STATUSES:
        return False
    try:
        now = _now_iso()
        extra_col = "applied_at" if status == "applied" else (
            "dismissed_at" if status == "dismissed" else None
        )
        conn = get_connection(_db(db_path))
        try:
            if extra_col:
                conn.execute(
                    f"UPDATE learning_actions SET status=?, {extra_col}=? WHERE action_id=?",
                    [status, now, action_id],
                )
            else:
                conn.execute(
                    "UPDATE learning_actions SET status=? WHERE action_id=?",
                    [status, action_id],
                )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False
