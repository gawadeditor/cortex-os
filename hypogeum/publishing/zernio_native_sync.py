"""
hypogeum/publishing/zernio_native_sync.py — pull each platform's OWN
analytics (proxied by Zernio) into channel-scoped tables.

The cross-platform /analytics rollup (see zernio_analytics_sync.py) is the
lowest common denominator. Every platform also exposes richer, native
signal that only its own API carries:

    YouTube  channel-insights   -> views / estimatedMinutesWatched / subs
             video-retention    -> per-video second-by-second watch curve
             demographics       -> follower age + gender
    Instagram account-insights  -> reach / views / accounts_engaged
             demographics       -> age / gender / city / country (100+ followers)
             follower-history    -> follower_count / gained / lost
    TikTok   account-insights   -> follower_count / likes / video_count
    Facebook page-insights      -> page_media_view / engagements / follows

Storage:
    channel_platform_insights   one row per (channel, platform, day)
    channel_audience            one row per (channel, platform, dimension, day)
    platform_analytics.retention_curve_json + avg_view_pct  (YouTube per video)

Public API:
    sync_zernio_native_analytics(*, provider=None, db_path=None) -> dict
    get_channel_platform_insights(channel_id, platform, *, db_path=None)
    get_channel_audience(channel_id, *, platform=None, db_path=None)
"""
from __future__ import annotations

import json as _json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger
from hypogeum.publishing.zernio_aggregate_sync import _channel_for_account, _cortex_channels

log = get_logger(agent="zernio_native_sync")

_PLATFORMS = ("youtube", "instagram", "tiktok", "facebook")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _flatten_metrics(raw: dict) -> dict:
    """Zernio wraps native metrics as {metrics: {views: {total: N}, ...}}.
    Flatten to {views: N, ...}; pass through a flat {k: number} too."""
    m = (raw or {}).get("metrics")
    if not isinstance(m, dict):
        return {}
    out: dict = {}
    for k, v in m.items():
        if isinstance(v, dict) and "total" in v:
            out[k] = v["total"]
        elif isinstance(v, (int, float)):
            out[k] = v
    return out


def _normalise_demographics(raw: dict) -> dict:
    """{age:[{dimension,value}], gender:[...]} -> {age:[{label,pct}], ...}
    keeping only non-empty dimensions."""
    demo = (raw or {}).get("demographics")
    if not isinstance(demo, dict):
        return {}
    out: dict = {}
    for dim in ("age", "gender", "city", "country"):
        rows = demo.get(dim)
        if not isinstance(rows, list) or not rows:
            continue
        norm = []
        for r in rows:
            label = r.get("dimension") or r.get("label")
            pct = r.get("value") if r.get("value") is not None else r.get("pct")
            if label is not None and isinstance(pct, (int, float)):
                norm.append({"label": str(label), "pct": round(float(pct), 2)})
        if norm:
            out[dim] = norm
    return out


def _retention_avg_view_pct(curve: list) -> Optional[float]:
    """Mean watchRatio across the retention curve = average % of the video
    watched. Returns 0.0–1.0 or None for an empty/garbage curve."""
    if not isinstance(curve, list) or not curve:
        return None
    vals = [p.get("watchRatio") for p in curve
            if isinstance(p.get("watchRatio"), (int, float))]
    if not vals:
        return None
    return round(min(max(sum(vals) / len(vals), 0.0), 1.0), 4)


# ---------------------------------------------------------------------------
# account map
# ---------------------------------------------------------------------------

def _account_map(provider, db_path, channels: list) -> dict:
    """{(channel_id, platform): zernio_account_id} from the connected-account
    roster. A profile's accounts are named after the channel."""
    try:
        res = provider.get_accounts(db_path=db_path)
        accounts = ((res.get("data") or {}).get("accounts")) or []
    except Exception:  # noqa: BLE001
        # fall back to the /analytics accounts[] shape
        try:
            res = provider.get_analytics(page=1, limit=1, db_path=db_path)
            accounts = ((res.get("data") or {}).get("accounts")) or []
        except Exception:  # noqa: BLE001
            return {}
    # profile_id -> channel via any account name under it
    prof_channel: dict = {}
    for a in accounts:
        pid = a.get("profileId")
        pid = pid.get("_id") if isinstance(pid, dict) else pid
        nm = a.get("name") or a.get("displayName") or a.get("username") or ""
        cid = _channel_for_account(nm, channels)
        if pid and cid:
            prof_channel.setdefault(pid, cid)
    out: dict = {}
    for a in accounts:
        aid = a.get("_id") or a.get("id")
        plat = (a.get("platform") or "").lower()
        if not aid or plat not in _PLATFORMS:
            continue
        pid = a.get("profileId")
        pid = pid.get("_id") if isinstance(pid, dict) else pid
        nm = a.get("name") or a.get("displayName") or a.get("username") or ""
        cid = _channel_for_account(nm, channels) or prof_channel.get(pid)
        if cid:
            out[(cid, plat)] = aid
    return out


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _store_platform_insights(conn, *, channel_id, platform, metrics: dict) -> None:
    if not metrics:
        return
    snap = _today()
    row = conn.execute(
        "SELECT insight_id FROM channel_platform_insights "
        "WHERE channel_id=? AND platform=? AND snapshot_date=?",
        (channel_id, platform, snap),
    ).fetchone()
    iid = row["insight_id"] if row else uuid.uuid4().hex
    conn.execute(
        "INSERT OR REPLACE INTO channel_platform_insights "
        "(insight_id, channel_id, platform, snapshot_date, metrics_json, source, imported_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (iid, channel_id, platform, snap, _json.dumps(metrics), "api", _now()),
    )


def _store_audience(conn, *, channel_id, platform, demographics: dict) -> int:
    snap = _today()
    n = 0
    for dim, breakdown in demographics.items():
        row = conn.execute(
            "SELECT audience_id FROM channel_audience "
            "WHERE channel_id=? AND platform=? AND dimension=? AND snapshot_date=?",
            (channel_id, platform, dim, snap),
        ).fetchone()
        aid = row["audience_id"] if row else uuid.uuid4().hex
        conn.execute(
            "INSERT OR REPLACE INTO channel_audience "
            "(audience_id, channel_id, platform, dimension, snapshot_date, "
            " breakdown_json, source, imported_at) VALUES (?,?,?,?,?,?,?,?)",
            (aid, channel_id, platform, dim, snap, _json.dumps(breakdown),
             "api", _now()),
        )
        n += 1
    return n


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def sync_zernio_native_analytics(*, provider=None, db_path: Optional[Path] = None) -> dict:
    """Fetch every connected account's native analytics and store them.
    Never raises. Returns {ok, accounts, platform_insights, audience_rows,
    retention_videos, errors}."""
    if provider is None:
        from hypogeum.publishing.zernio import ZernioProvider
        provider = ZernioProvider()

    from hypogeum.db import get_connection
    out: dict = {"ok": False, "accounts": 0, "platform_insights": 0,
                 "audience_rows": 0, "retention_videos": 0, "errors": {}}

    channels = _cortex_channels(db_path)
    acct_map = _account_map(provider, db_path, channels)
    out["accounts"] = len(acct_map)
    if not acct_map:
        out["errors"]["account_map"] = "no Zernio account mapped to a Cortex channel"
        return out

    def _fetch(fn_name, **kw):
        try:
            res = getattr(provider, fn_name)(db_path=db_path, **kw)
        except Exception as exc:  # noqa: BLE001
            out["errors"][fn_name] = str(exc)[:160]
            return None
        if res.get("error") or res.get("data") is None:
            out["errors"][fn_name] = f"status={res.get('status_code')}"
            return None
        return res["data"]

    conn = get_connection(Path(db_path) if db_path else None)
    try:
        for (cid, plat), aid in acct_map.items():
            if plat == "youtube":
                d = _fetch("get_youtube_channel_insights", account_id=aid)
                if d:
                    _store_platform_insights(conn, channel_id=cid, platform=plat,
                                             metrics=_flatten_metrics(d))
                    out["platform_insights"] += 1
                dm = _fetch("get_youtube_demographics", account_id=aid)
                if dm:
                    out["audience_rows"] += _store_audience(
                        conn, channel_id=cid, platform=plat,
                        demographics=_normalise_demographics(dm))
            elif plat == "instagram":
                d = _fetch("get_instagram_account_insights", account_id=aid)
                fh = _fetch("get_instagram_follower_history", account_id=aid)
                merged = {**_flatten_metrics(d or {}), **_flatten_metrics(fh or {})}
                if merged:
                    _store_platform_insights(conn, channel_id=cid, platform=plat, metrics=merged)
                    out["platform_insights"] += 1
                dm = _fetch("get_instagram_demographics", account_id=aid)
                if dm:
                    out["audience_rows"] += _store_audience(
                        conn, channel_id=cid, platform=plat,
                        demographics=_normalise_demographics(dm))
            elif plat == "tiktok":
                d = _fetch("get_tiktok_account_insights", account_id=aid)
                if d:
                    _store_platform_insights(conn, channel_id=cid, platform=plat,
                                             metrics=_flatten_metrics(d))
                    out["platform_insights"] += 1
            elif plat == "facebook":
                d = _fetch("get_facebook_page_insights", account_id=aid)
                if d:
                    _store_platform_insights(conn, channel_id=cid, platform=plat,
                                             metrics=_flatten_metrics(d))
                    out["platform_insights"] += 1

        # YouTube per-video retention — every episode with a YouTube video id
        out["retention_videos"] += _sync_youtube_retention(conn, provider, acct_map, out, db_path)
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        out["errors"]["store"] = str(exc)[:200]
    finally:
        conn.close()

    out["ok"] = (out["platform_insights"] > 0 or out["audience_rows"] > 0
                 or out["retention_videos"] > 0)
    log.info("zernio_native_synced", **{k: out[k] for k in
             ("accounts", "platform_insights", "audience_rows", "retention_videos")},
             errors=list(out["errors"]))
    return out


def _sync_youtube_retention(conn, provider, acct_map: dict, out: dict, db_path) -> int:
    """For each episode that has a YouTube platform_analytics row with a
    video id, pull the retention curve and write it back (curve + a mean
    watchRatio into avg_view_pct — the metric the YT INSIGHT_RULES key off,
    which is otherwise always NULL for YouTube)."""
    yt_account = next((aid for (cid, plat), aid in acct_map.items() if plat == "youtube"), None)
    # need a per-channel account id — map channel -> its yt account
    yt_by_channel = {cid: aid for (cid, plat), aid in acct_map.items() if plat == "youtube"}
    if not yt_by_channel:
        return 0
    rows = conn.execute(
        "SELECT pa.record_id, pa.episode_id, pa.platform_video_id, "
        "       (SELECT et.channel_id FROM episode_topics et "
        "        WHERE et.episode_id = pa.episode_id LIMIT 1) AS channel_id "
        "FROM platform_analytics pa "
        "WHERE pa.platform='youtube' AND pa.source='api' "
        "AND pa.platform_video_id IS NOT NULL "
        "AND (pa.retention_curve_json IS NULL OR pa.avg_view_pct IS NULL)"
    ).fetchall()
    done = 0
    for r in rows:
        cid = r["channel_id"]
        aid = yt_by_channel.get(cid) or yt_account
        if not aid:
            continue
        try:
            res = provider.get_youtube_video_retention(
                account_id=aid, video_id=r["platform_video_id"], db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            out["errors"][f"retention:{r['platform_video_id']}"] = str(exc)[:120]
            continue
        data = res.get("data") or {}
        curve = data.get("retentionCurve") or []
        if not curve:
            continue
        avp = _retention_avg_view_pct(curve)
        conn.execute(
            "UPDATE platform_analytics SET retention_curve_json = ?, "
            "avg_view_pct = COALESCE(avg_view_pct, ?) WHERE record_id = ?",
            (_json.dumps(curve), avp, r["record_id"]),
        )
        done += 1
    return done


# ---------------------------------------------------------------------------
# readers
# ---------------------------------------------------------------------------

def get_channel_platform_insights(channel_id: str, platform: str, *,
                                  db_path: Optional[Path] = None) -> Optional[dict]:
    """Latest native metric bag for (channel, platform), or None."""
    from hypogeum.db import get_connection
    conn = get_connection(Path(db_path) if db_path else None)
    try:
        row = conn.execute(
            "SELECT snapshot_date, metrics_json FROM channel_platform_insights "
            "WHERE channel_id=? AND platform=? ORDER BY snapshot_date DESC LIMIT 1",
            (channel_id, platform),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        return {"snapshot_date": row["snapshot_date"],
                "metrics": _json.loads(row["metrics_json"])}
    except Exception:  # noqa: BLE001
        return None


def get_channel_audience(channel_id: str, *, platform: Optional[str] = None,
                         db_path: Optional[Path] = None) -> dict:
    """Latest audience breakdown per (platform, dimension) for a channel:
    {platform: {dimension: [{label, pct}]}}."""
    from hypogeum.db import get_connection
    conn = get_connection(Path(db_path) if db_path else None)
    try:
        q = ("SELECT platform, dimension, snapshot_date, breakdown_json "
             "FROM channel_audience WHERE channel_id=? ")
        params = [channel_id]
        if platform:
            q += "AND platform=? "
            params.append(platform)
        q += "ORDER BY snapshot_date DESC"
        rows = conn.execute(q, params).fetchall()
    finally:
        conn.close()
    out: dict = {}
    seen: set = set()
    for r in rows:
        key = (r["platform"], r["dimension"])
        if key in seen:
            continue
        seen.add(key)
        try:
            out.setdefault(r["platform"], {})[r["dimension"]] = _json.loads(r["breakdown_json"])
        except Exception:  # noqa: BLE001
            continue
    return out
