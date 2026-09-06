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


def _sync_zernio(
    channel_id: Optional[str],
    db_path: Optional[Path],
    window_days: int,
    triggered_by: str,
    conn,
) -> list[dict]:
    """One GET /v1/analytics pass covering every published post on all four
    platforms, mapped back to the Cortex episode via the publishing ledger.
    Logs one platform_sync_log row per platform and returns them."""
    import json as _json
    try:
        from hypogeum.publishing.zernio_analytics_sync import sync_zernio_analytics
        out = sync_zernio_analytics(db_path=db_path, window_days=min(window_days, 30))
    except Exception as exc:  # noqa: BLE001
        for p in ("youtube", "tiktok", "instagram", "facebook"):
            _log_sync(conn, platform=p, channel_id=channel_id, status="error",
                      records_imported=0, errors_json=_json.dumps([str(exc)]),
                      window_days=window_days, triggered_by=triggered_by)
        return [{"platform": p, "status": "error", "records_imported": 0,
                 "error": str(exc), "provider": "zernio"} for p in
                ("youtube", "tiktok", "instagram", "facebook")]

    per_platform = out.get("platforms") or {}
    rows = []
    for p in ("youtube", "tiktok", "instagram", "facebook"):
        n = int(per_platform.get(p, 0))
        status = "ok" if out.get("ok") else "error"
        errs = None if out.get("ok") else _json.dumps([out.get("error") or "no data"])
        _log_sync(conn, platform=p, channel_id=channel_id, status=status,
                  records_imported=n, errors_json=errs,
                  window_days=window_days, triggered_by=triggered_by)
        rows.append({"platform": p, "status": status, "records_imported": n,
                     "provider": "zernio"})
    return rows


def run_platform_sync(
    channel_id: Optional[str] = None,
    db_path: Optional[Path] = None,
    window_days: int = 90,
    triggered_by: str = "manual",
    platforms: Optional[list] = None,
) -> dict:
    """
    Run analytics sync across all supported platforms (or a subset).

    Zernio (GET /v1/analytics) is the primary pass — it covers all four
    platforms in one call, keyed to the episode via the publishing ledger.
    YouTube, Meta (Facebook/Instagram) and TikTok then layer richer
    retention data on top wherever their own API credentials are connected.

    Writes platform_sync_log rows per platform per provider.

    Returns:
        {ok, results: [{platform, status, records_imported, error?}], total_records}
    """
    db = _resolve(db_path) or _DEFAULT_DB_PATH
    conn = get_connection(db)
    target = [p for p in _SUPPORTED_PLATFORMS if not platforms or p in platforms]

    results = []
    try:
        # Primary: one Zernio pass for every platform.
        if not platforms or set(platforms) & set(_SUPPORTED_PLATFORMS):
            try:
                results.extend(_sync_zernio(channel_id, db_path, window_days, triggered_by, conn))
            except Exception:  # noqa: BLE001
                pass
            # Account-level aggregate rollups (content-decay / best-time /
            # posting-frequency / follower-stats) — separate endpoints,
            # separate table; a failure here must never fail the per-post sync.
            try:
                from hypogeum.publishing.zernio_aggregate_sync import sync_zernio_aggregates
                sync_zernio_aggregates(db_path=db_path)
            except Exception:  # noqa: BLE001
                pass

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

    # Per platform, count the best single provider rather than summing
    # Zernio + native-API rows for the same platform.
    _best: dict = {}
    for r in results:
        p = r.get("platform")
        _best[p] = max(_best.get(p, 0), r.get("records_imported", 0))
    total = sum(_best.values())
    has_error = any(r["status"] == "error" for r in results)

    # Learning pipeline (2026-09-05 fix): extract_insights()/generate_
    # actions() previously only ran from the manual /analytics/import
    # dashboard path (mock/CSV/JSON) -- a real Zernio sync (this
    # function, the one the worker calls hourly) never triggered them at
    # all, so every episode with genuine synced performance data sat
    # with zero insights/actions until an operator manually re-imported
    # its own analytics as JSON. Re-running per real-analytics episode
    # here is safe to call every sync (extract_insights()/
    # generate_actions() are both dedup-safe -- INSERT OR IGNORE and
    # _has_existing_pending_action() respectively) and cheap at today's
    # data volume. hypogeum.comparative_insights.generate_comparative_
    # insights() (per channel, not per episode -- it needs >=2 real-
    # analytics episodes to compare) runs alongside it.
    if total > 0:
        try:
            _refresh_learning_pipeline(channel_id, db_path)
        except Exception:  # noqa: BLE001 — learning refresh must never fail a sync
            pass

    return {
        "ok":            not has_error,
        "results":       results,
        "total_records": total,
        "synced_at":     _now(),
    }


def _refresh_learning_pipeline(channel_id: Optional[str], db_path: Optional[Path]) -> None:
    """Re-run insight/action extraction for every episode with real
    (source='api') analytics, plus the cross-episode comparison, for
    channel_id (or every channel when None). See the call site's own
    comment for why this runs after every real sync."""
    from hypogeum.learning_engine import extract_insights, generate_actions
    from hypogeum.comparative_insights import generate_comparative_insights

    # Comparative insights first (per channel) -- they write new rows
    # into analytics_insights keyed by episode_id, which the per-episode
    # loop below must then see when it calls generate_actions() for that
    # same episode_id, or a comparative insight would sit un-actioned
    # until the NEXT sync.
    if channel_id is not None:
        channel_ids = [channel_id]
    else:
        from hypogeum.channel_config import list_channel_profiles
        channel_ids = [c.channel_id for c in list_channel_profiles(db_path=db_path)]

    for cid in channel_ids:
        try:
            generate_comparative_insights(cid, db_path=db_path)
        except Exception:  # noqa: BLE001
            pass
        # Zernio best-time rollup -> at most one advisory publishing_time
        # action per channel (dedup-safe, same as the loops below).
        try:
            from hypogeum.learning_engine import generate_schedule_recommendation
            generate_schedule_recommendation(cid, db_path=db_path)
        except Exception:  # noqa: BLE001
            pass

    db = _resolve(db_path) or _DEFAULT_DB_PATH
    conn = get_connection(db)
    try:
        rows = conn.execute(
            "SELECT DISTINCT episode_id FROM platform_analytics WHERE source='api'"
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        episode_id = row["episode_id"]
        try:
            extract_insights(episode_id, db_path=db_path)
            generate_actions(episode_id, db_path=db_path)
        except Exception:  # noqa: BLE001
            continue


def get_sync_log(
    platform: Optional[str] = None,
    limit: int = 20,
    db_path: Optional[Path] = None,
    channel_id: Optional[str] = None,
) -> list[dict]:
    """Return recent sync log rows, newest first. When channel_id is given,
    only that channel's rows (rows with a NULL channel_id are excluded)."""
    db = _resolve(db_path) or _DEFAULT_DB_PATH
    conn = get_connection(db)
    try:
        clauses, params = [], []
        if platform:
            clauses.append("platform = ?"); params.append(platform)
        if channel_id:
            clauses.append("channel_id = ?"); params.append(channel_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = conn.execute(
            f"""SELECT sync_id, platform, channel_id, status, records_imported,
                       errors_json, window_days, triggered_by, synced_at
                FROM platform_sync_log
                {where}
                ORDER BY synced_at DESC LIMIT ?""",
            params,
        ).fetchall()
        cols = ["sync_id", "platform", "channel_id", "status", "records_imported",
                "errors_json", "window_days", "triggered_by", "synced_at"]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def get_last_sync_by_platform(db_path: Optional[Path] = None,
                              channel_id: Optional[str] = None) -> dict[str, dict]:
    """Return the most recent sync log row per platform as {platform: row}.
    Scoped to channel_id when given (NULL-channel rows excluded)."""
    db = _resolve(db_path) or _DEFAULT_DB_PATH
    conn = get_connection(db)
    try:
        _cw = "WHERE channel_id = ?" if channel_id else ""
        _cp = (channel_id,) if channel_id else ()
        rows = conn.execute(
            f"""SELECT platform, status, records_imported, synced_at
               FROM platform_sync_log
               WHERE sync_id IN (
                   SELECT MAX(sync_id) FROM platform_sync_log {_cw} GROUP BY platform
               )
               ORDER BY synced_at DESC""",
            _cp,
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
