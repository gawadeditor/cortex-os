"""
hypogeum/channel_profile.py — F9: Channel Control Center profile store.

Manages a single-row settings table (channel_profile_settings) that holds
channel identity and weekly publishing targets for the executive dashboard.

Defaults seed the row on first get_channel_profile() call so the row always
exists afterwards.  The profile_id is always 'default' (single-factory model).

Public API:
    get_channel_profile(db_path=None)          -> dict
    update_channel_profile(updates, db_path)   -> dict  {"ok": True, ...}
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
_PROFILE_ID = "default"

_ALLOWED_FIELDS = frozenset({
    "channel_name", "logo_path", "description",
    "primary_color", "secondary_color", "accent_color",
    "content_niche", "tone_style",
    "yt_videos_per_week", "yt_shorts_per_week",
    "tiktok_per_week", "instagram_per_week", "facebook_per_week",
})

_INT_FIELDS = frozenset({
    "yt_videos_per_week", "yt_shorts_per_week",
    "tiktok_per_week", "instagram_per_week", "facebook_per_week",
})

_DEFAULTS = {
    "profile_id":          _PROFILE_ID,
    "channel_name":        None,
    "logo_path":           None,
    "description":         None,
    "primary_color":       "#3fb950",
    "secondary_color":     "#58a6ff",
    "accent_color":        "#d29922",
    "content_niche":       None,
    "tone_style":          None,
    "yt_videos_per_week":  2,
    "yt_shorts_per_week":  3,
    "tiktok_per_week":     5,
    "instagram_per_week":  3,
    "facebook_per_week":   2,
    "metadata_version":    "v1",
}


def _resolve(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path else _DEFAULT_DB_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_channel_profile(db_path: Optional[Path] = None) -> dict:
    """
    Return the channel profile dict.  Inserts the default row if it doesn't
    exist yet.  Non-blocking — returns Python defaults on any error.
    """
    db = _resolve(db_path)
    try:
        initialize_schema(db)
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT * FROM channel_profile_settings WHERE profile_id=?",
                [_PROFILE_ID],
            ).fetchone()

            if row is None:
                _seed_default(conn)
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM channel_profile_settings WHERE profile_id=?",
                    [_PROFILE_ID],
                ).fetchone()
        finally:
            conn.close()

        if row:
            return dict(row)
    except Exception:
        pass

    return dict(_DEFAULTS)


def update_channel_profile(
    updates: dict,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Update allowed fields in channel_profile_settings.
    Ignored keys: silently skipped.  Integer fields coerced.
    Returns {"ok": True, "updated": [...]} or {"ok": False, "error": "..."}.
    """
    safe = _coerce(updates)
    if not safe:
        return {"ok": False, "error": "No valid fields to update", "code": "invalid"}

    db = _resolve(db_path)
    try:
        initialize_schema(db)
        conn = get_connection(db)
        try:
            # Ensure default row exists
            row = conn.execute(
                "SELECT profile_id FROM channel_profile_settings WHERE profile_id=?",
                [_PROFILE_ID],
            ).fetchone()
            if row is None:
                _seed_default(conn)

            cols   = ", ".join(f"{k}=?" for k in safe)
            values = list(safe.values()) + [_now(), _PROFILE_ID]
            conn.execute(
                f"UPDATE channel_profile_settings SET {cols}, updated_at=? WHERE profile_id=?",
                values,
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "updated": list(safe.keys())}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "code": "db_error"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _seed_default(conn) -> None:
    now = _now()
    conn.execute(
        """INSERT OR IGNORE INTO channel_profile_settings
           (profile_id, channel_name, logo_path, description,
            primary_color, secondary_color, accent_color,
            content_niche, tone_style,
            yt_videos_per_week, yt_shorts_per_week, tiktok_per_week,
            instagram_per_week, facebook_per_week,
            created_at, updated_at, metadata_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_PROFILE_ID, None, None, None,
         "#3fb950", "#58a6ff", "#d29922",
         None, None,
         2, 3, 5, 3, 2,
         now, now, "v1"),
    )


def _coerce(raw: dict) -> dict:
    """Filter to allowed fields, coerce integers, drop None strings."""
    out = {}
    for k, v in raw.items():
        if k not in _ALLOWED_FIELDS:
            continue
        if k in _INT_FIELDS:
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                pass
        else:
            out[k] = str(v).strip() if v is not None else None
    return out
