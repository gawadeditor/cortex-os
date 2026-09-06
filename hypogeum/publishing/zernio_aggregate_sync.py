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


def sync_zernio_aggregates(*, provider=None, db_path: Optional[Path] = None) -> dict:
    """Fetch all four Zernio aggregate endpoints and upsert today's snapshot
    into channel_analytics_rollup. Never raises."""
    if provider is None:
        from hypogeum.publishing.zernio import ZernioProvider
        provider = ZernioProvider()

    from hypogeum.db import get_connection
    out: dict = {"ok": False, "kinds_written": [], "errors": {}}

    def _fetch(fn_name, *args, **kwargs):
        try:
            res = getattr(provider, fn_name)(*args, db_path=db_path, **kwargs)
        except Exception as exc:  # noqa: BLE001
            out["errors"][fn_name] = str(exc)[:200]
            return None
        if res.get("error") or res.get("data") is None:
            out["errors"][fn_name] = f"status={res.get('status_code')}"
            return None
        return res["data"]

    decay = _normalise_content_decay(_fetch("get_content_decay") or {})
    best = _normalise_best_time(_fetch("get_best_time") or {})
    freq = _normalise_posting_frequency(_fetch("get_posting_frequency") or {})
    followers = _normalise_follower_stats(_fetch("get_follower_stats") or {})

    conn = get_connection(Path(db_path) if db_path else None)
    try:
        if decay is not None:
            _store_rollup(conn, scope="factory", metric_kind="content_decay", payload=decay)
            out["kinds_written"].append("content_decay")
        if best is not None:
            _store_rollup(conn, scope="factory", metric_kind="best_time", payload=best)
            out["kinds_written"].append("best_time")
        if freq is not None:
            _store_rollup(conn, scope="factory", metric_kind="posting_frequency", payload=freq)
            out["kinds_written"].append("posting_frequency")
        if followers is not None:
            _store_rollup(conn, scope="factory", metric_kind="follower_stats", payload=followers)
            out["kinds_written"].append("follower_stats")
            # split per channel
            try:
                from hypogeum.channel_config import list_channel_profiles
                profiles = list_channel_profiles(db_path=db_path)
                channels = [(p.channel_id,
                             getattr(p, "name", "") or getattr(p, "channel_name", "") or "")
                            for p in profiles]
            except Exception:  # noqa: BLE001
                channels = []
            per_channel: dict = {}
            for acct in followers["accounts"]:
                cid = _channel_for_account(acct["name"], channels)
                if cid:
                    per_channel.setdefault(cid, []).append(acct)
            for cid, accts in per_channel.items():
                _store_rollup(conn, scope=cid, metric_kind="follower_stats",
                              payload={"accounts": accts,
                                       "date_range": followers.get("date_range"),
                                       "granularity": followers.get("granularity")})
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        out["errors"]["store"] = str(exc)[:200]
    finally:
        conn.close()

    out["ok"] = bool(out["kinds_written"])
    log.info("zernio_aggregates_synced", kinds=out["kinds_written"], errors=list(out["errors"]))
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
