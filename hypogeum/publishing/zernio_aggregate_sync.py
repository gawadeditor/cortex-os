"""
hypogeum/publishing/zernio_aggregate_sync.py — pull Zernio's *account-level*
aggregate analytics (the endpoints that are not per-post) into
channel_analytics_rollup.

Zernio computes four rollups over the whole connected account, not per
post:

    GET /v1/analytics/content-decay       -> how fast a post's engagement
                                             accumulates after publishing
    GET /v1/analytics/best-time           -> day-of-week × hour slots
                                             ranked by historical engagement
    GET /v1/analytics/posting-frequency   -> per-platform posts/week vs
                                             engagement rate
    GET /v1/accounts/follower-stats        -> per-account follower counts
                                             + time series

The first three are inherently factory-wide (Zernio's account spans every
Cortex channel), so they are stored with scope='factory'. follower-stats
carries a per-account breakdown, which we additionally split onto the
Cortex channel that owns each account (best-effort name match; anything
unmatched still lands in the factory row).

Consumers:
    - hypogeum.learning_engine.generate_schedule_recommendation() reads the
      best_time rollup and proposes a publishing_time learning action.
    - the dashboard analytics/intelligence pages surface all four.

Public API:
    sync_zernio_aggregates(*, provider=None, db_path=None) -> dict
    get_rollup(metric_kind, *, scope='factory', db_path=None) -> Optional[dict]
"""
from __future__ import annotations

import json as _json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="zernio_aggregate_sync")

_KINDS = ("content_decay", "best_time", "posting_frequency", "follower_stats")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _normalise_content_decay(data: dict) -> Optional[list]:
    buckets = (data or {}).get("buckets")
    if not isinstance(buckets, list) or not buckets:
        return None
    out = []
    for b in buckets:
        out.append({
            "bucket_order": b.get("bucket_order"),
            "bucket_label": b.get("bucket_label"),
            "avg_pct_of_final": b.get("avg_pct_of_final"),
            "post_count": b.get("post_count"),
        })
    return out


def _normalise_best_time(data: dict) -> Optional[list]:
    slots = (data or {}).get("slots")
    if not isinstance(slots, list) or not slots:
        return None
    out = []
    for s in slots:
        out.append({
            "day_of_week": s.get("day_of_week"),
            "hour": s.get("hour"),
            "avg_engagement": s.get("avg_engagement"),
            "post_count": s.get("post_count"),
        })
    return out


# Zernio's best-time slots are ranked from OUR OWN past posts' engagement at
# the times we happened to publish -- not from when the audience is online --
# so a slot backed by one or two posts is noise, not a finding (2026-09-30:
# twinkle's "best" slot, Wed 21:00, sat at #1 for three weeks on n=1;
# hypogeum Mon 10:00 read 21.0 on n=2 and settled at 4.6 by n=14). Zernio
# counts each platform as a post, so 8 ~= two episodes across 4 platforms.
BEST_TIME_MIN_POSTS = 8


def reliable_best_time_slots(payload, *, min_posts: int = BEST_TIME_MIN_POSTS) -> list:
    """The best-time rollup's slots with at least `min_posts` posts behind
    them, highest avg_engagement first. The single place every consumer
    (dashboard pages, client advisory, learning engine) filters slots."""
    slots = [s for s in (payload or [])
             if isinstance(s, dict)
             and isinstance(s.get("avg_engagement"), (int, float))
             and (s.get("post_count") or 0) >= min_posts
             and s.get("day_of_week") is not None and s.get("hour") is not None]
    return sorted(slots, key=lambda s: -s["avg_engagement"])


def _normalise_posting_frequency(data: dict) -> Optional[list]:
    freq = (data or {}).get("frequency")
    if not isinstance(freq, list) or not freq:
        return None
    out = []
    for f in freq:
        out.append({
            "platform": (f.get("platform") or "").lower(),
            "posts_per_week": f.get("posts_per_week"),
            "avg_engagement_rate": f.get("avg_engagement_rate"),
            "avg_engagement": f.get("avg_engagement"),
            "weeks_count": f.get("weeks_count"),
        })
    return out


def _normalise_follower_stats(data: dict) -> Optional[dict]:
    if not isinstance(data, dict):
        return None
    accounts = data.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        return None
    norm_accounts = []
    for a in accounts:
        norm_accounts.append({
            "platform": (a.get("platform") or "").lower(),
            "name": a.get("name") or a.get("displayName") or "",
            "followers": (a.get("currentFollowers")
                          if a.get("currentFollowers") is not None
                          else a.get("followers")),
        })
    return {
        "accounts": norm_accounts,
        "date_range": data.get("dateRange"),
        "granularity": data.get("granularity"),
    }


def _channel_for_account(account_name: str, channels: list) -> Optional[str]:
    """Best-effort match of a Zernio account name to a Cortex channel_id.
    `channels` is a list of (channel_id, display_name)."""
    an = _norm(account_name)
    if not an:
        return None
    for cid, display in channels:
        for candidate in (cid, display):
            cn = _norm(candidate)
            if cn and (cn in an or an in cn):
                return cid
    return None


def _store_rollup(conn, *, scope: str, metric_kind: str, payload, source: str = "api") -> None:
    snap = _today()
    row = conn.execute(
        "SELECT rollup_id FROM channel_analytics_rollup "
        "WHERE scope=? AND metric_kind=? AND snapshot_date=?",
        (scope, metric_kind, snap),
    ).fetchone()
    rid = row["rollup_id"] if row else uuid.uuid4().hex
    conn.execute(
        "INSERT OR REPLACE INTO channel_analytics_rollup "
        "(rollup_id, scope, metric_kind, snapshot_date, payload_json, source, imported_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (rid, scope, metric_kind, snap, _json.dumps(payload), source, _now()),
    )


def _cortex_channels(db_path) -> list:
    """[(channel_id, display_name)] for the name match."""
    try:
        from hypogeum.channel_config import list_channel_profiles
        return [(p.channel_id,
                 getattr(p, "name", "") or getattr(p, "channel_name", "") or "")
                for p in list_channel_profiles(db_path=db_path)]
    except Exception:  # noqa: BLE001
        return []


def _zernio_profile_channel_map(provider, db_path, channels: list) -> dict:
    """{zernio_profile_id: cortex_channel_id}. Zernio groups every connected
    account under a `profileId`; the /analytics response lists those accounts
    with their names. A profile maps to the Cortex channel that any of its
    account names matches (both channels' accounts are named after them)."""
    try:
        res = provider.get_analytics(page=1, limit=1, db_path=db_path)
        accounts = ((res.get("data") or {}).get("accounts")) or []
    except Exception:  # noqa: BLE001
        return {}
    by_profile: dict = {}
    for a in accounts:
        pid = a.get("profileId")
        name = a.get("name") or a.get("displayName") or a.get("username") or ""
        if not pid:
            continue
        by_profile.setdefault(pid, []).append(name)
    out: dict = {}
    for pid, names in by_profile.items():
        for nm in names:
            cid = _channel_for_account(nm, channels)
            if cid:
                out[pid] = cid
                break
    return out


def _fetch_scope(provider, db_path, *, profile_id, errors: dict) -> dict:
    """Fetch + normalise all four aggregate endpoints for one scope
    (profile_id=None → whole account). Returns {kind: payload_or_None}."""
    def _f(fn_name, **kw):
        try:
            res = getattr(provider, fn_name)(db_path=db_path, **kw)
        except Exception as exc:  # noqa: BLE001
            errors[f"{fn_name}:{profile_id or 'factory'}"] = str(exc)[:160]
            return None
        if res.get("error") or res.get("data") is None:
            errors[f"{fn_name}:{profile_id or 'factory'}"] = f"status={res.get('status_code')}"
            return None
        return res["data"]

    pid_kw = {"profile_id": profile_id} if profile_id else {}
    return {
        "content_decay": _normalise_content_decay(_f("get_content_decay", **pid_kw) or {}),
        "best_time": _normalise_best_time(_f("get_best_time", **pid_kw) or {}),
        "posting_frequency": _normalise_posting_frequency(_f("get_posting_frequency", **pid_kw) or {}),
        "follower_stats": _normalise_follower_stats(_f("get_follower_stats", **pid_kw) or {}),
    }


def sync_zernio_aggregates(*, provider=None, db_path: Optional[Path] = None) -> dict:
    """Fetch the four Zernio aggregate endpoints — once for the whole
    account (scope='factory') and once per connected profile mapped to its
    Cortex channel (scope=channel_id) — and upsert today's snapshot into
    channel_analytics_rollup. The per-channel pass matters: the two channels
    have genuinely different best-time / cadence curves. Never raises."""
    if provider is None:
        from hypogeum.publishing.zernio import ZernioProvider
        provider = ZernioProvider()

    from hypogeum.db import get_connection
    out: dict = {"ok": False, "kinds_written": [], "scopes": [], "errors": {}}

    channels = _cortex_channels(db_path)
    profile_map = _zernio_profile_channel_map(provider, db_path, channels)

    # scope -> profile_id (None = whole account)
    scopes: dict = {"factory": None}
    for pid, cid in profile_map.items():
        scopes[cid] = pid

    conn = get_connection(Path(db_path) if db_path else None)
    try:
        for scope, pid in scopes.items():
            payloads = _fetch_scope(provider, db_path, profile_id=pid, errors=out["errors"])
            wrote_any = False
            for kind, payload in payloads.items():
                if payload is None:
                    continue
                _store_rollup(conn, scope=scope, metric_kind=kind, payload=payload)
                wrote_any = True
                if kind not in out["kinds_written"]:
                    out["kinds_written"].append(kind)
            if wrote_any:
                out["scopes"].append(scope)
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        out["errors"]["store"] = str(exc)[:200]
    finally:
        conn.close()

    out["ok"] = bool(out["scopes"])
    log.info("zernio_aggregates_synced", scopes=out["scopes"],
             kinds=out["kinds_written"], errors=list(out["errors"]))
    return out


def get_rollup(metric_kind: str, *, scope: str = "factory",
               db_path: Optional[Path] = None) -> Optional[dict]:
    """Most recent rollup payload for (scope, metric_kind), or None.
    Returns {snapshot_date, payload, imported_at}."""
    if metric_kind not in _KINDS:
        raise ValueError(f"unknown metric_kind {metric_kind!r}")
    from hypogeum.db import get_connection
    conn = get_connection(Path(db_path) if db_path else None)
    try:
        row = conn.execute(
            "SELECT snapshot_date, payload_json, imported_at "
            "FROM channel_analytics_rollup WHERE scope=? AND metric_kind=? "
            "ORDER BY snapshot_date DESC LIMIT 1",
            (scope, metric_kind),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        payload = _json.loads(row["payload_json"])
    except Exception:  # noqa: BLE001
        return None
    return {"snapshot_date": row["snapshot_date"], "payload": payload,
            "imported_at": row["imported_at"]}
