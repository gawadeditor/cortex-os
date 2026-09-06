"""
hypogeum/publishing/client_content_sync.py — pull an external client's
published content + performance from Zernio into the Cortex Advisory layer.

For every active client, for every enabled managed_account, this:
  1. fetches the account's posts via GET /v1/analytics?source=external
     (posts the client published on the platform, NOT via Zernio),
  2. upserts a `content_items` row per post (title / caption / thumbnail /
     url / published_at),
  3. stores per-post metrics into `platform_analytics` keyed by the
     content_id, with `client_id` set for exact attribution,
  4. pulls the account-level native signals (retention curves, demographics,
     comments, follower stats, best-time) into the client-scoped rows /
     `channel_analytics_rollup` scope='client:<id>'.

The learning engine's per-`source='api'` refresh (`_refresh_learning_pipeline`)
then produces insights/actions for those content_ids automatically.

Public API:
    sync_client_content(client_id, *, provider=None, db_path=None) -> dict
    sync_all_clients(*, provider=None, db_path=None) -> dict
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger
from hypogeum.publishing.zernio_analytics_sync import _translate_metrics
from hypogeum.publishing.zernio_native_sync import (
    _normalise_demographics, _flatten_metrics, _retention_avg_view_pct,
)

log = get_logger(agent="client_content_sync")

_MAX_PAGES = 20
_PAGE_LIMIT = 100
_MAX_RETENTION_CALLS = 10          # per account per run — incremental
_PLATFORM_KEYS = ("youtube", "instagram", "tiktok", "facebook")


def _post_platform_rows(post: dict) -> list[dict]:
    """One dict per (post, platform) — mirrors fetch_all_zernio_analytics
    but keeps the thumbnail + media type we need for content_items."""
    out = []
    base = {
        "zernio_post_id": post.get("_id"),
        "late_post_id": post.get("latePostId"),
        "content": post.get("content") or "",
        "thumbnail_url": post.get("thumbnailUrl"),
        "media_type": post.get("mediaType"),
        "published_at": post.get("publishedAt"),
        "url": post.get("platformPostUrl"),
    }
    plats = post.get("platforms") or []
    if plats:
        for pl in plats:
            out.append({**base,
                        "platform": (pl.get("platform") or post.get("platform") or "").lower(),
                        "platform_post_id": pl.get("platformPostId"),
                        "url": pl.get("platformPostUrl") or base["url"],
                        "metrics": pl.get("analytics") or post.get("analytics") or {}})
    else:
        out.append({**base,
                    "platform": (post.get("platform") or "").lower(),
                    "platform_post_id": post.get("platformPostId"),
                    "metrics": post.get("analytics") or {}})
    return out


def _fetch_external_posts(provider, account_id: str, db_path, errors: dict) -> list[dict]:
    posts: list[dict] = []
    page = 1
    while page <= _MAX_PAGES:
        try:
            res = provider.get_analytics(page=page, limit=_PAGE_LIMIT,
                                         source="external", account_id=account_id,
                                         db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            errors[f"get_analytics:{account_id}:{page}"] = str(exc)[:160]
            break
        data = res.get("data") if not res.get("error") else None
        if not isinstance(data, dict):
            errors[f"get_analytics:{account_id}:{page}"] = f"status={res.get('status_code')}"
            break
        batch = data.get("posts") or []
        posts.extend(batch)
        pg = (data.get("pagination") or {})
        if not batch or (pg.get("pages") and page >= int(pg["pages"])):
            break
        page += 1
    return posts


def sync_client_content(client_id: str, *, provider=None,
                        db_path: Optional[Path] = None) -> dict:
    """Sync one client. Never raises."""
    from hypogeum import client_manager as cm
    from hypogeum.analytics_store import store_content_metrics
    from hypogeum.db import get_connection

    if provider is None:
        from hypogeum.publishing.zernio import ZernioProvider
        provider = ZernioProvider()

    out: dict = {"client_id": client_id, "ok": False, "accounts": 0,
                 "content_items": 0, "metric_rows": 0, "retention": 0,
                 "audience_rows": 0, "comments": 0, "errors": {}}

    client = cm.get_client(client_id, db_path=db_path)
    if not client or client.get("status") == "archived":
        out["errors"]["client"] = "no active client"
        return out
    accounts = cm.list_managed_accounts(client_id, db_path=db_path)
    out["accounts"] = len(accounts)
    if not accounts:
        out["errors"]["accounts"] = "no managed accounts linked"
        return out

    seen_content: set = set()

    for acct in accounts:
        aid = acct.get("provider_account_id")
        plat = acct.get("platform")
        if not aid:
            out["errors"][f"{plat}:no_provider_account_id"] = "link an account first"
            continue

        # ---- per-post ----
        for post in _fetch_external_posts(provider, aid, db_path, out["errors"]):
            for r in _post_platform_rows(post):
                if r["platform"] not in _PLATFORM_KEYS or not r["platform_post_id"]:
                    continue
                content_id = cm.upsert_content_item(
                    client_id, r["platform"],
                    platform_post_id=str(r["platform_post_id"]),
                    managed_account_id=acct["managed_account_id"],
                    zernio_post_id=r["zernio_post_id"], url=r["url"],
                    title=(r["content"] or "").split("\n", 1)[0][:200] or None,
                    caption=r["content"] or None,
                    thumbnail_url=r["thumbnail_url"], media_type=r["media_type"],
                    published_at=r["published_at"], source="zernio_external",
                    db_path=db_path,
                )
                if content_id not in seen_content:
                    seen_content.add(content_id)
                    out["content_items"] += 1
                metrics = _translate_metrics(r["metrics"],
                                             platform_post_id=str(r["platform_post_id"]),
                                             platform=r["platform"])
                if any(metrics.get(k) for k in ("views", "impressions", "likes",
                                                "comments", "reach", "engagement_rate")):
                    try:
                        store_content_metrics(content_id, client_id, r["platform"],
                                              metrics, source="api", db_path=db_path)
                        out["metric_rows"] += 1
                    except Exception as exc:  # noqa: BLE001
                        out["errors"][f"store:{content_id}"] = str(exc)[:120]

        # ---- account-level native signals (client-scoped) ----
        out["retention"] += _sync_account_retention(provider, client_id, aid, plat, db_path, out)
        out["audience_rows"] += _sync_account_native(provider, client_id, aid, plat, db_path, out)

    _sync_account_rollups(provider, client_id, accounts, db_path, out)

    out["ok"] = out["content_items"] > 0 or out["metric_rows"] > 0
    log.info("client_content_synced", client_id=client_id,
             **{k: out[k] for k in ("accounts", "content_items", "metric_rows",
                                    "retention", "audience_rows", "comments")},
             errors=list(out["errors"]))
    return out


def _sync_account_retention(provider, client_id, account_id, platform, db_path, out) -> int:
    """YouTube per-video retention → platform_analytics.retention_curve_json
    + avg_view_pct, for this client's YouTube content items missing one."""
    if platform != "youtube":
        return 0
    from hypogeum.db import get_connection
    conn = get_connection(Path(db_path) if db_path else None)
    try:
        rows = conn.execute(
            "SELECT DISTINCT pa.content_id, ci.platform_post_id "
            "FROM platform_analytics pa JOIN content_items ci ON ci.content_id = pa.content_id "
            "WHERE pa.client_id=? AND pa.platform='youtube' "
            "AND ci.platform_post_id IS NOT NULL AND pa.retention_curve_json IS NULL",
            (client_id,),
        ).fetchall()
    finally:
        conn.close()
    import json as _json
    done = 0
    for i, r in enumerate(rows):
        if i >= _MAX_RETENTION_CALLS:
            break
        try:
            res = provider.get_youtube_video_retention(
                account_id=account_id, video_id=r["platform_post_id"], db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            out["errors"][f"retention:{r['platform_post_id']}"] = str(exc)[:100]
            continue
        curve = (res.get("data") or {}).get("retentionCurve") or []
        if not curve:
            continue
        avp = _retention_avg_view_pct(curve)
        conn2 = get_connection(Path(db_path) if db_path else None)
        try:
            conn2.execute(
                "UPDATE platform_analytics SET retention_curve_json=?, "
                "avg_view_pct=COALESCE(avg_view_pct, ?) WHERE content_id=? AND platform='youtube'",
                (_json.dumps(curve), avp, r["content_id"]),
            )
            conn2.commit()
        finally:
            conn2.close()
        done += 1
    return done


def _sync_account_native(provider, client_id, account_id, platform, db_path, out) -> int:
    """Account-level insights + demographics + comments into the
    channel_id-NOT-NULL tables using channel_id = client_id."""
    import json as _json
    import uuid
    from datetime import datetime, timezone
    from hypogeum.db import get_connection

    now = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).date().isoformat()
    audience_rows = 0

    fn_map = {
        "youtube": ("get_youtube_channel_insights", "get_youtube_demographics"),
        "instagram": ("get_instagram_account_insights", "get_instagram_demographics"),
        "tiktok": ("get_tiktok_account_insights", None),
        "facebook": ("get_facebook_page_insights", None),
    }
    ins_fn, demo_fn = fn_map.get(platform, (None, None))

    metrics = {}
    if ins_fn:
        try:
            res = getattr(provider, ins_fn)(account_id=account_id, db_path=db_path)
            if not res.get("error"):
                metrics.update(_flatten_metrics(res.get("data") or {}))
        except Exception as exc:  # noqa: BLE001
            out["errors"][f"{ins_fn}:{client_id}"] = str(exc)[:120]
    if platform == "instagram":
        try:
            fh = provider.get_instagram_follower_history(account_id=account_id, db_path=db_path)
            if not fh.get("error"):
                metrics.update(_flatten_metrics(fh.get("data") or {}))
        except Exception:  # noqa: BLE001
            pass

    conn = get_connection(Path(db_path) if db_path else None)
    try:
        if metrics:
            row = conn.execute(
                "SELECT insight_id FROM channel_platform_insights "
                "WHERE channel_id=? AND platform=? AND snapshot_date=?",
                (client_id, platform, today),
            ).fetchone()
            iid = row["insight_id"] if row else uuid.uuid4().hex
            conn.execute(
                "INSERT OR REPLACE INTO channel_platform_insights "
                "(insight_id, channel_id, platform, snapshot_date, metrics_json, "
                " source, imported_at) VALUES (?,?,?,?,?,?,?)",
                (iid, client_id, platform, today, _json.dumps(metrics), "api", now),
            )

        if demo_fn:
            try:
                dm = getattr(provider, demo_fn)(account_id=account_id, db_path=db_path)
                demo = _normalise_demographics(dm.get("data") or {}) if not dm.get("error") else {}
            except Exception:  # noqa: BLE001
                demo = {}
            for dim, breakdown in demo.items():
                row = conn.execute(
                    "SELECT audience_id FROM channel_audience "
                    "WHERE channel_id=? AND platform=? AND dimension=? AND snapshot_date=?",
                    (client_id, platform, dim, today),
                ).fetchone()
                aid2 = row["audience_id"] if row else uuid.uuid4().hex
                conn.execute(
                    "INSERT OR REPLACE INTO channel_audience "
                    "(audience_id, channel_id, platform, dimension, snapshot_date, "
                    " breakdown_json, source, imported_at) VALUES (?,?,?,?,?,?,?,?)",
                    (aid2, client_id, platform, dim, today, _json.dumps(breakdown),
                     "api", now),
                )
                audience_rows += 1

        # comments — for this client's content items on this platform
        cids = conn.execute(
            "SELECT DISTINCT ci.content_id, ci.platform_post_id FROM content_items ci "
            "WHERE ci.client_id=? AND ci.platform=? AND ci.platform_post_id IS NOT NULL LIMIT 15",
            (client_id, platform),
        ).fetchall()
        for r in cids:
            try:
                cres = provider.get_post_comments(account_id=account_id,
                                                  post_id=r["platform_post_id"], db_path=db_path)
            except Exception:  # noqa: BLE001
                continue
            comments = (cres.get("data") or {}).get("comments") or (cres.get("data") or {}).get("data") or []
            for cm_ in comments:
                frm = cm_.get("from") or {}
                text = (cm_.get("text") or cm_.get("content") or cm_.get("message") or "").strip()
                if not text:
                    continue
                cmid = str(cm_.get("id") or f"{r['platform_post_id']}:{hash(text)}")
                conn.execute(
                    "INSERT OR REPLACE INTO post_comments (comment_id, episode_id, channel_id, "
                    "platform, platform_post_id, author, is_owner, text, like_count, "
                    "published_at, source, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cmid, r["content_id"], client_id, platform, r["platform_post_id"],
                     (frm.get("name") if isinstance(frm, dict) else None),
                     1 if (isinstance(frm, dict) and frm.get("isOwner")) else 0,
                     text[:2000], cm_.get("likeCount") or cm_.get("likes"),
                     (cm_.get("createdAt") or "")[:32] or None, "api", now),
                )
                out["comments"] += 1
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        out["errors"][f"native:{platform}"] = str(exc)[:150]
    finally:
        conn.close()
    return audience_rows


def _sync_account_rollups(provider, client_id, accounts, db_path, out) -> None:
    """best-time / content-decay / posting-frequency / follower-stats for the
    client, into channel_analytics_rollup scope='client:<id>'. Zernio's
    aggregate endpoints scope by profileId, so use the first account's."""
    from hypogeum.publishing.zernio_aggregate_sync import (
        _store_rollup, _normalise_best_time, _normalise_content_decay,
        _normalise_posting_frequency, _normalise_follower_stats,
    )
    from hypogeum.db import get_connection

    profile_id = next((a.get("provider_profile_id") for a in accounts
                       if a.get("provider_profile_id")), None)
    if not profile_id:
        return
    scope = f"client:{client_id}"
    conn = get_connection(Path(db_path) if db_path else None)
    try:
        for kind, fn, norm in (
            ("best_time", "get_best_time", _normalise_best_time),
            ("content_decay", "get_content_decay", _normalise_content_decay),
            ("posting_frequency", "get_posting_frequency", _normalise_posting_frequency),
            ("follower_stats", "get_follower_stats", _normalise_follower_stats),
        ):
            try:
                res = getattr(provider, fn)(profile_id=profile_id, db_path=db_path)
                payload = norm(res.get("data") or {}) if not res.get("error") else None
            except Exception as exc:  # noqa: BLE001
                out["errors"][f"{fn}:{client_id}"] = str(exc)[:120]
                payload = None
            if payload is not None:
                _store_rollup(conn, scope=scope, metric_kind=kind, payload=payload)
        conn.commit()
    finally:
        conn.close()


def sync_all_clients(*, provider=None, db_path: Optional[Path] = None) -> dict:
    from hypogeum import client_manager as cm
    out = {"clients": 0, "results": []}
    for c in cm.list_clients(status="active", db_path=db_path):
        r = sync_client_content(c["client_id"], provider=provider, db_path=db_path)
        out["clients"] += 1
        out["results"].append({k: r[k] for k in ("client_id", "ok", "content_items",
                                                 "metric_rows", "errors")})
    return out
