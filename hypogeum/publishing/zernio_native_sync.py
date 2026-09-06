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
        if isinstance(v, dict):
            for probe in ("total", "value", "average"):
                if probe in v and isinstance(v[probe], (int, float)):
                    out[k] = v[probe]
                    break
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
            val = r.get("value") if r.get("value") is not None else r.get("pct")
            if label is not None and isinstance(val, (int, float)):
                # YouTube's oldest age bucket comes back as "65-" meaning
                # "65 and over" — read it the way a human would.
                lbl = "65+" if str(label) in ("65-", "65-100") else str(label)
                norm.append({"label": lbl, "value": float(val)})
        if not norm:
            continue
        # YouTube returns age/gender as viewerPercentage but city/country as
        # absolute view counts — normalise everything to a percent of the
        # dimension total so downstream can treat "pct" uniformly.
        total = sum(x["value"] for x in norm)
        if total > 0 and (total > 101 or dim in ("city", "country")):
            for x in norm:
                x["value"] = x["value"] / total * 100.0
        out[dim] = [{"label": x["label"], "pct": round(x["value"], 2)} for x in norm]
    return out


def _retention_avg_view_pct(curve: list) -> Optional[float]:
    """Average fraction of the video watched, from YouTube's retention
    curve. Each point's `audienceWatchRatio` is segment views / video views
    (can exceed 1 on a re-watched segment); clamp to 1 and take the mean —
    a monotonic proxy for average view percentage. Returns 0.0–1.0 or None
    for an empty curve.

    Accepts either YouTube's raw key (`audienceWatchRatio`) or a
    pre-normalised `watchRatio` (what our own tests / a future normaliser
    might pass)."""
    if not isinstance(curve, list) or not curve:
        return None
    vals = []
    for p in curve:
        v = p.get("audienceWatchRatio")
        if v is None:
            v = p.get("watchRatio")
        if isinstance(v, (int, float)):
            vals.append(min(max(float(v), 0.0), 1.0))
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


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

    # ---- PHASE 1: fetch everything into memory (NO db connection held —
    # these are ~20 HTTP calls and must not straddle a write lock) ----
    pi_rows: list[tuple] = []          # (channel_id, platform, metrics)
    aud_specs: list[tuple] = []        # (channel_id, platform, demographics)
    for (cid, plat), aid in acct_map.items():
        if plat == "youtube":
            d = _fetch("get_youtube_channel_insights", account_id=aid)
            if d:
                pi_rows.append((cid, plat, _flatten_metrics(d)))
            dm = _fetch("get_youtube_demographics", account_id=aid)
            if dm:
                aud_specs.append((cid, plat, _normalise_demographics(dm)))
        elif plat == "instagram":
            d = _fetch("get_instagram_account_insights", account_id=aid)
            fh = _fetch("get_instagram_follower_history", account_id=aid)
            merged = {**_flatten_metrics(d or {}), **_flatten_metrics(fh or {})}
            if merged:
                pi_rows.append((cid, plat, merged))
            dm = _fetch("get_instagram_demographics", account_id=aid)
            if dm:
                aud_specs.append((cid, plat, _normalise_demographics(dm)))
        elif plat == "tiktok":
            d = _fetch("get_tiktok_account_insights", account_id=aid)
            if d:
                pi_rows.append((cid, plat, _flatten_metrics(d)))
        elif plat == "facebook":
            d = _fetch("get_facebook_page_insights", account_id=aid)
            if d:
                pi_rows.append((cid, plat, _flatten_metrics(d)))

    retention_updates = _fetch_youtube_retention(provider, acct_map, out, db_path)
    comment_rows = _fetch_post_comments(provider, acct_map, out, db_path)

    # ---- PHASE 2: one short write transaction ----
    conn = get_connection(Path(db_path) if db_path else None)
    try:
        for cid, plat, metrics in pi_rows:
            _store_platform_insights(conn, channel_id=cid, platform=plat, metrics=metrics)
            out["platform_insights"] += 1
        for cid, plat, demo in aud_specs:
            out["audience_rows"] += _store_audience(
                conn, channel_id=cid, platform=plat, demographics=demo)
        for record_id, curve, avp in retention_updates:
            conn.execute(
                "UPDATE platform_analytics SET retention_curve_json = ?, "
                "avg_view_pct = COALESCE(avg_view_pct, ?) WHERE record_id = ?",
                (_json.dumps(curve), avp, record_id),
            )
        out["retention_videos"] = len({rid for rid, _, _ in retention_updates})
        for c in comment_rows:
            conn.execute(
                "INSERT OR REPLACE INTO post_comments (comment_id, episode_id, "
                "channel_id, platform, platform_post_id, author, is_owner, text, "
                "like_count, published_at, source, imported_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (c["comment_id"], c["episode_id"], c["channel_id"], c["platform"],
                 c["platform_post_id"], c["author"], 1 if c["is_owner"] else 0,
                 c["text"], c["like_count"], c["published_at"], "api", _now()),
            )
        out["comments"] = len(comment_rows)
        out["audience_comments"] = len([c for c in comment_rows if not c["is_owner"]])
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        out["errors"]["store"] = str(exc)[:200]
    finally:
        conn.close()

    out["ok"] = (out["platform_insights"] > 0 or out["audience_rows"] > 0
                 or out["retention_videos"] > 0 or out.get("comments", 0) > 0)
    log.info("zernio_native_synced", **{k: out[k] for k in
             ("accounts", "platform_insights", "audience_rows", "retention_videos")},
             comments=out.get("comments", 0),
             audience_comments=out.get("audience_comments", 0),
             errors=list(out["errors"]))
    return out


_MAX_COMMENT_POST_CALLS_PER_SYNC = 15


def _fetch_post_comments(provider, acct_map: dict, out: dict, db_path) -> list:
    """Pull comment text for published posts. Returns [dict] rows for
    post_comments. Deduped by (channel, platform_post_id), only the post's
    own channel's account, capped per run. No DB connection during HTTP."""
    from hypogeum.db import get_connection
    by_channel_plat = {(cid, plat): aid for (cid, plat), aid in acct_map.items()}
    if not by_channel_plat:
        return []

    conn = get_connection(Path(db_path) if db_path else None)
    try:
        rows = conn.execute(
            "SELECT DISTINCT pa.episode_id, pa.platform, pa.platform_video_id, "
            "       (SELECT et.channel_id FROM episode_topics et "
            "        WHERE et.episode_id = pa.episode_id LIMIT 1) AS channel_id "
            "FROM platform_analytics pa "
            "WHERE pa.source='api' AND pa.platform_video_id IS NOT NULL "
            "AND (pa.comments IS NULL OR pa.comments > 0)"
        ).fetchall()
    finally:
        conn.close()

    out_rows: list = []
    seen: set = set()
    calls = 0
    for r in rows:
        cid, plat, pid = r["channel_id"], r["platform"], r["platform_video_id"]
        key = (cid, plat, pid)
        if not cid or key in seen or (cid, plat) not in by_channel_plat:
            continue
        seen.add(key)
        if calls >= _MAX_COMMENT_POST_CALLS_PER_SYNC:
            break
        calls += 1
        try:
            res = provider.get_post_comments(
                account_id=by_channel_plat[(cid, plat)], post_id=pid, db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            out["errors"][f"comments:{pid}"] = str(exc)[:120]
            continue
        data = res.get("data") or {}
        comments = data.get("comments") or data.get("data") or []
        for cm in comments:
            frm = cm.get("from") or {}
            author = frm.get("name") if isinstance(frm, dict) else str(frm)
            is_owner = bool(frm.get("isOwner")) if isinstance(frm, dict) else False
            text = (cm.get("text") or cm.get("content") or cm.get("message")
                    or cm.get("body") or "").strip()
            if not text:
                continue
            cmid = str(cm.get("id") or cm.get("commentId") or f"{pid}:{hash(text)}")
            out_rows.append({
                "comment_id": cmid, "episode_id": r["episode_id"], "channel_id": cid,
                "platform": plat, "platform_post_id": pid, "author": author,
                "is_owner": is_owner, "text": text[:2000],
                "like_count": cm.get("likeCount") or cm.get("likes"),
                "published_at": (cm.get("createdAt") or cm.get("timestamp") or "")[:32] or None,
            })
    return out_rows


def get_episode_comments(episode_id: str, *, include_owner: bool = False,
                         db_path: Optional[Path] = None) -> list:
    """All stored comments for an episode, newest first. Excludes the
    channel's own comments/replies unless include_owner=True."""
    from hypogeum.db import get_connection
    conn = get_connection(Path(db_path) if db_path else None)
    try:
        q = ("SELECT platform, author, is_owner, text, like_count, published_at "
             "FROM post_comments WHERE episode_id=? ")
        if not include_owner:
            q += "AND is_owner=0 "
        q += "ORDER BY published_at DESC"
        rows = conn.execute(q, (episode_id,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_recent_audience_comments(channel_id: str, *, limit: int = 20,
                                 db_path: Optional[Path] = None) -> list:
    """Recent NON-owner comments across a channel's episodes, newest first."""
    from hypogeum.db import get_connection
    conn = get_connection(Path(db_path) if db_path else None)
    try:
        rows = conn.execute(
            "SELECT platform, author, text, like_count, published_at, episode_id "
            "FROM post_comments WHERE channel_id=? AND is_owner=0 "
            "ORDER BY published_at DESC LIMIT ?",
            (channel_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


_MAX_RETENTION_CALLS_PER_SYNC = 12   # incremental — the rest catch up next run


def _fetch_youtube_retention(provider, acct_map: dict, out: dict, db_path) -> list:
    """Pull retention curves for YouTube videos still missing one. Returns
    [(record_id, curve, avg_view_pct)] to apply in the write phase.

    ONE call per (channel, video) — platform_analytics has many rows per
    video (snapshots); they all get the same curve. Only the video's OWN
    channel's YouTube account is queried (a cross-account query is a
    guaranteed 404). Capped per run and holds no DB connection during the
    HTTP calls."""
    from hypogeum.db import get_connection
    yt_by_channel = {cid: aid for (cid, plat), aid in acct_map.items() if plat == "youtube"}
    if not yt_by_channel:
        return []

    conn = get_connection(Path(db_path) if db_path else None)
    try:
        rows = conn.execute(
            "SELECT pa.record_id, pa.platform_video_id, "
            "       (SELECT et.channel_id FROM episode_topics et "
            "        WHERE et.episode_id = pa.episode_id LIMIT 1) AS channel_id "
            "FROM platform_analytics pa "
            "WHERE pa.platform='youtube' AND pa.source='api' "
            "AND pa.platform_video_id IS NOT NULL "
            "AND pa.retention_curve_json IS NULL"
        ).fetchall()
    finally:
        conn.close()

    # group record_ids by (channel, video); skip videos whose channel has no
    # YouTube account mapped.
    by_video: dict = {}
    for r in rows:
        cid = r["channel_id"]
        if cid not in yt_by_channel:
            continue
        by_video.setdefault((cid, r["platform_video_id"]), []).append(r["record_id"])

    updates: list = []
    for i, ((cid, vid), record_ids) in enumerate(by_video.items()):
        if i >= _MAX_RETENTION_CALLS_PER_SYNC:
            break
        try:
            res = provider.get_youtube_video_retention(
                account_id=yt_by_channel[cid], video_id=vid, db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            out["errors"][f"retention:{vid}"] = str(exc)[:120]
            continue
        curve = (res.get("data") or {}).get("retentionCurve") or []
        if not curve:
            continue
        avp = _retention_avg_view_pct(curve)
        for rid in record_ids:
            updates.append((rid, curve, avp))
    return updates


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
