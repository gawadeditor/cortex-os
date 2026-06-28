"""
hypogeum/platform_sync.py — Cross-platform analytics sync orchestrator.

Coordinates per-platform analytics sync and writes audit rows to
platform_sync_log. Currently supports YouTube (OAuth API). Other platforms
require manual import — no public analytics API exists.

Public API:
    run_platform_sync(channel_id=None, db_path=None, window_days=90) -> dict
    get_sync_log(platform=None, limit=20, db_path=None) -> list[dict]
    get_last_sync_by_platform(db_path=None) -> dict[str, dict]
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection

_SUPPORTED_PLATFORMS = ("youtube", "tiktok", "instagram", "facebook")

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve(db_path: Optional[Path]) -> Optional[Path]:
    return Path(db_path) if db_path is not None else None


def _log_sync(
    conn,
    *,
    platform: str,
    channel_id: Optional[str],
    status: str,
    records_imported: int,
    errors_json: Optional[str],
    window_days: int,
    triggered_by: str,
) -> str:
    sync_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO platform_sync_log
           (sync_id, platform, channel_id, status, records_imported,
            errors_json, window_days, triggered_by, synced_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            sync_id, platform, channel_id, status, records_imported,
            errors_json, window_days, triggered_by, _now(),
        ),
    )
    return sync_id


def _sync_youtube(
    channel_id: Optional[str],
    db_path: Optional[Path],
    window_days: int,
    triggered_by: str,
    conn,
) -> dict:
    """Run YouTube sync, log result. Returns per-platform result dict."""
    import json as _json
    try:
        from hypogeum.config import Config
        cfg = Config()
    except Exception as exc:
        _log_sync(conn, platform="youtube", channel_id=channel_id,
                  status="error", records_imported=0,
                  errors_json=_json.dumps([str(exc)]),
                  window_days=window_days, triggered_by=triggered_by)
        return {"platform": "youtube", "status": "error",
                "records_imported": 0, "error": str(exc)}

    if not (cfg.YOUTUBE_REFRESH_TOKEN
            and cfg.YOUTUBE_CLIENT_ID
            and cfg.YOUTUBE_CLIENT_SECRET):
        _log_sync(conn, platform="youtube", channel_id=channel_id,
                  status="not_connected", records_imported=0, errors_json=None,
                  window_days=window_days, triggered_by=triggered_by)
        return {"platform": "youtube", "status": "not_connected",
                "records_imported": 0,
                "error": "YouTube OAuth credentials not configured"}

    try:
        from hypogeum.youtube_analytics import sync_youtube_analytics
        result = sync_youtube_analytics(cfg, db_path=db_path, window_days=window_days)
        records = result.get("synced_count", 0)
        errors  = result.get("errors") or []
        status  = "ok" if result.get("ok") else "error"
        _log_sync(conn, platform="youtube", channel_id=channel_id,
                  status=status, records_imported=records,
                  errors_json=_json.dumps(errors) if errors else None,
                  window_days=window_days, triggered_by=triggered_by)
        return {"platform": "youtube", "status": status,
                "records_imported": records, "errors": errors}
    except Exception as exc:
        _log_sync(conn, platform="youtube", channel_id=channel_id,
                  status="error", records_imported=0,
                  errors_json=_json.dumps([str(exc)]),
                  window_days=window_days, triggered_by=triggered_by)
        return {"platform": "youtube", "status": "error",
                "records_imported": 0, "error": str(exc)}


def _sync_meta(
    platform: str,
    channel_id: Optional[str],
    db_path: Optional[Path],
    window_days: int,
    triggered_by: str,
    conn,
) -> dict:
    """Run Meta Graph API sync for facebook or instagram, log result."""
    import json as _json
    try:
        from hypogeum.config import Config
        cfg = Config()
    except Exception as exc:
        _log_sync(conn, platform=platform, channel_id=channel_id,
                  status="error", records_imported=0,
                  errors_json=_json.dumps([str(exc)]),
                  window_days=window_days, triggered_by=triggered_by)
        return {"platform": platform, "status": "error", "records_imported": 0, "error": str(exc)}

    from hypogeum.meta_analytics import get_meta_token, sync_meta_analytics

    token = get_meta_token(platform, db_path=db_path)
    if not token:
        _log_sync(conn, platform=platform, channel_id=channel_id,
                  status="not_connected", records_imported=0, errors_json=None,
                  window_days=window_days, triggered_by=triggered_by)
        return {"platform": platform, "status": "not_connected", "records_imported": 0,
                "error": f"{platform} credential not found"}

    try:
        result   = sync_meta_analytics(platform, cfg, db_path=db_path, window_days=window_days)
        records  = result.get("synced_count", 0)
        errors   = result.get("errors") or []
        status   = "ok" if result.get("ok") else "error"
        note     = result.get("note")
        if note and not errors:
            errors = [note]
        _log_sync(conn, platform=platform, channel_id=channel_id,
                  status=status, records_imported=records,
                  errors_json=_json.dumps(errors) if errors else None,
                  window_days=window_days, triggered_by=triggered_by)
        return {"platform": platform, "status": status,
                "records_imported": records, "errors": errors}
    except Exception as exc:
        _log_sync(conn, platform=platform, channel_id=channel_id,
                  status="error", records_imported=0,
                  errors_json=_json.dumps([str(exc)]),
                  window_days=window_days, triggered_by=triggered_by)
        return {"platform": platform, "status": "error",
                "records_imported": 0, "error": str(exc)}


def _sync_tiktok(
    channel_id: Optional[str],
    db_path: Optional[Path],
    window_days: int,
    triggered_by: str,
    conn,
) -> dict:
    """Run TikTok Video API analytics sync, log result."""
    import json as _json
    try:
        from hypogeum.config import Config
        cfg = Config()
    except Exception as exc:
        _log_sync(conn, platform="tiktok", channel_id=channel_id,
                  status="error", records_imported=0,
                  errors_json=_json.dumps([str(exc)]),
                  window_days=window_days, triggered_by=triggered_by)
        return {"platform": "tiktok", "status": "error", "records_imported": 0, "error": str(exc)}

    from hypogeum.meta_analytics import sync_tiktok_analytics
    try:
        result  = sync_tiktok_analytics(cfg, db_path=db_path, window_days=window_days)
        records = result.get("synced_count", 0)
        errors  = result.get("errors") or []
        note    = result.get("note")
        if note and not errors:
            errors = [note]
        status  = "ok" if result.get("ok") else "error"
        _log_sync(conn, platform="tiktok", channel_id=channel_id,
                  status=status, records_imported=records,
                  errors_json=_json.dumps(errors) if errors else None,
                  window_days=window_days, triggered_by=triggered_by)
        return {"platform": "tiktok", "status": status,
                "records_imported": records, "errors": errors}
    except Exception as exc:
        _log_sync(conn, platform="tiktok", channel_id=channel_id,
                  status="error", records_imported=0,
                  errors_json=_json.dumps([str(exc)]),
                  window_days=window_days, triggered_by=triggered_by)
        return {"platform": "tiktok", "status": "error", "records_imported": 0, "error": str(exc)}


def run_platform_sync(
    channel_id: Optional[str] = None,
    db_path: Optional[Path] = None,
    window_days: int = 90,
    triggered_by: str = "manual",
    platforms: Optional[list] = None,
) -> dict:
    """
    Run analytics sync across all supported platforms (or a subset).

    Writes one platform_sync_log row per platform. YouTube, Meta
    (Facebook/Instagram), and TikTok all sync via API.

    Returns:
        {ok, results: [{platform, status, records_imported, error?}], total_records}
    """
    db = _resolve(db_path) or _DEFAULT_DB_PATH
    conn = get_connection(db)
    target = [p for p in _SUPPORTED_PLATFORMS if not platforms or p in platforms]

    results = []
    try:
        for platform in target:
            if platform == "youtube":
                r = _sync_youtube(channel_id, db_path, window_days, triggered_by, conn)
            elif platform in ("facebook", "instagram"):
                r = _sync_meta(platform, channel_id, db_path, window_days, triggered_by, conn)
            elif platform == "tiktok":
                r = _sync_tiktok(channel_id, db_path, window_days, triggered_by, conn)
            else:
                import json as _json
                _log_sync(conn, platform=platform, channel_id=channel_id,
                          status="skipped", records_imported=0, errors_json=None,
                          window_days=window_days, triggered_by=triggered_by)
                r = {"platform": platform, "status": "skipped", "records_imported": 0}
            results.append(r)
        conn.commit()
    finally:
        conn.close()

    total = sum(r.get("records_imported", 0) for r in results)
    has_error = any(r["status"] == "error" for r in results)
    return {
        "ok":            not has_error,
        "results":       results,
        "total_records": total,
        "synced_at":     _now(),
    }


def get_sync_log(
    platform: Optional[str] = None,
    limit: int = 20,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Return recent sync log rows, newest first."""
    db = _resolve(db_path) or _DEFAULT_DB_PATH
    conn = get_connection(db)
    try:
        if platform:
            rows = conn.execute(
                """SELECT sync_id, platform, channel_id, status, records_imported,
                          errors_json, window_days, triggered_by, synced_at
                   FROM platform_sync_log
                   WHERE platform = ?
                   ORDER BY synced_at DESC LIMIT ?""",
                (platform, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT sync_id, platform, channel_id, status, records_imported,
                          errors_json, window_days, triggered_by, synced_at
                   FROM platform_sync_log
                   ORDER BY synced_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        cols = ["sync_id", "platform", "channel_id", "status", "records_imported",
                "errors_json", "window_days", "triggered_by", "synced_at"]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def get_last_sync_by_platform(db_path: Optional[Path] = None) -> dict[str, dict]:
    """Return the most recent sync log row per platform as {platform: row}."""
    db = _resolve(db_path) or _DEFAULT_DB_PATH
    conn = get_connection(db)
    try:
        rows = conn.execute(
            """SELECT platform, status, records_imported, synced_at
               FROM platform_sync_log
               WHERE sync_id IN (
                   SELECT MAX(sync_id) FROM platform_sync_log GROUP BY platform
               )
               ORDER BY synced_at DESC"""
        ).fetchall()
        result = {}
        for r in rows:
            result[r[0]] = {
                "platform":         r[0],
                "status":           r[1],
                "records_imported": r[2],
                "synced_at":        r[3],
            }
        return result
    except Exception:
        return {}
    finally:
        conn.close()
