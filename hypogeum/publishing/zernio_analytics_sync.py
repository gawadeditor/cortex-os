"""
hypogeum/publishing/zernio_analytics_sync.py — pull real platform
performance from Zernio into platform_analytics.

Zernio is the publishing aggregator AND the analytics source: its
GET /v1/analytics endpoint returns per-post, per-platform metrics for
every published post on the connected accounts (YouTube / TikTok /
Instagram / Facebook). This module fetches that, maps each Zernio post
back to the Cortex episode that produced it, and upserts a
`platform_analytics` row with `source='api'` keyed by the real
`episode_id` — which is what the learning engine reads.

Mapping:
    Zernio post.latePostId  ==  publishing_receipts.provider_publication_id
    -> publishing_attempts.attempt_id -> .episode_id / .channel_id

Posts whose latePostId matches no receipt (e.g. manually-published
back-catalogue) are reported as `unmatched` and skipped — never guessed.

Metric translation (Zernio key -> platform_analytics column):
    views                       -> views
    impressions                 -> impressions
    clicks / impressions        -> ctr           (only when impressions > 0)
    likes / comments / shares / saves -> same
    igReelsAvgWatchTime   (ms)  -> avg_watch_time_s      (/ 1000)
    igReelsVideoViewTotalTime (ms) -> total_watch_time_s (/ 1000)
    avg_watch_time_s / videoDurationSeconds -> avg_view_pct
    reach                       -> reach          (FB / IG only; NULL elsewhere)
    follows                     -> follows
    reposts                     -> reposts
    engagementRate              -> engagement_rate  (kept in PERCENT, 2.66 == 2.66%)
    reelsSkipRate               -> skip_rate       (Instagram Reels only; NULL elsewhere)
    platformPostId              -> platform_video_id
    (full metric dict)          -> raw_json

Public API:
    fetch_all_zernio_analytics(*, provider=None, db_path=None) -> list[dict]
    sync_zernio_analytics(*, provider=None, db_path=None, window_days=7) -> dict
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="zernio_analytics_sync")

_MAX_PAGES = 20
_PAGE_LIMIT = 100


def _num(v):
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_all_zernio_analytics(*, provider=None, db_path: Optional[Path] = None) -> list[dict]:
    """Every published post Zernio has analytics for, one dict per
    post-platform pair: {late_post_id, zernio_post_id, platform,
    platform_post_id, platform_post_url, published_at, content, metrics}.
    Never raises — returns [] on a transport/credential failure."""
    if provider is None:
        from hypogeum.publishing.zernio import ZernioProvider
        provider = ZernioProvider()

    out: list[dict] = []
    page = 1
    while page <= _MAX_PAGES:
        try:
            res = provider.get_analytics(page=page, limit=_PAGE_LIMIT, db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            log.error("zernio_analytics_fetch_failed", page=page, error=str(exc))
            break
        if res.get("error") or not res.get("data"):
            log.warn("zernio_analytics_page_empty", page=page,
                     status=res.get("status_code"), error=str(res.get("error"))[:200])
            break
        data = res["data"]
        posts = data.get("posts") or []
        for p in posts:
            plats = p.get("platforms") or []
            if plats:
                for pl in plats:
                    out.append({
                        "late_post_id": p.get("latePostId"),
                        "zernio_post_id": p.get("_id"),
                        "platform": (pl.get("platform") or p.get("platform") or "").lower(),
                        "platform_post_id": pl.get("platformPostId"),
                        "platform_post_url": pl.get("platformPostUrl") or p.get("platformPostUrl"),
                        "published_at": p.get("publishedAt"),
                        "content": p.get("content") or "",
                        "metrics": pl.get("analytics") or p.get("analytics") or {},
                    })
            else:
                out.append({
                    "late_post_id": p.get("latePostId"),
                    "zernio_post_id": p.get("_id"),
                    "platform": (p.get("platform") or "").lower(),
                    "platform_post_id": None,
                    "platform_post_url": p.get("platformPostUrl"),
                    "published_at": p.get("publishedAt"),
                    "content": p.get("content") or "",
                    "metrics": p.get("analytics") or {},
                })
        pagination = data.get("pagination") or {}
        pages = pagination.get("pages")
        if not pages or page >= int(pages):
            break
        page += 1

    log.info("zernio_analytics_fetched", post_platform_rows=len(out), pages=page)
    return out


def _late_post_id_to_episode(db_path: Optional[Path]) -> dict:
    """{provider_publication_id: (episode_id, channel_id)} from the
    publishing ledger."""
    from hypogeum.db import get_connection
    conn = get_connection(Path(db_path) if db_path else None)
    try:
        rows = conn.execute(
            "SELECT DISTINCT r.provider_publication_id AS ppid, a.episode_id, a.channel_id "
            "FROM publishing_receipts r "
            "JOIN publishing_attempts a ON a.attempt_id = r.attempt_id "
            "WHERE r.provider_publication_id IS NOT NULL AND a.episode_id IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    mapping: dict = {}
    for row in rows:
        mapping.setdefault(row["ppid"], (row["episode_id"], row["channel_id"]))
    return mapping


def _translate_metrics(raw: dict, *, platform_post_id: Optional[str],
                       platform: str = "") -> dict:
    views = _num(raw.get("views"))
    impressions = _num(raw.get("impressions"))
    clicks = _num(raw.get("clicks"))
    ctr = (clicks / impressions) if (clicks is not None and impressions) else None

    # Zernio only reports real watch-time for Instagram Reels; for the other
    # platforms it echoes igReels* as 0, which is not a measurement.
    avg_watch_s = total_watch_s = None
    if platform == "instagram":
        _a = _num(raw.get("igReelsAvgWatchTime"))
        _t = _num(raw.get("igReelsVideoViewTotalTime"))
        avg_watch_s = round(_a / 1000.0, 2) if _a else None
        total_watch_s = round(_t / 1000.0, 2) if _t else None

    duration = _num(raw.get("videoDurationSeconds"))
    avg_view_pct = None
    if avg_watch_s and duration and duration > 0:
        avg_view_pct = round(min(avg_watch_s / duration, 1.0), 4)

    def _int(key):
        v = _num(raw.get(key))
        return int(v) if v is not None else None

    # Zernio reports reach for Facebook + Instagram; for YouTube / TikTok it
    # echoes 0, which is "no such metric on this platform", not a measurement.
    reach = _int("reach") if platform in ("facebook", "instagram") else None
    # reelsSkipRate is an Instagram-Reels-only field; same rule as watch time.
    skip_rate = None
    if platform == "instagram":
        _s = _num(raw.get("reelsSkipRate"))
        skip_rate = round(_s, 2) if _s is not None else None
    # engagementRate is Zernio-computed and already a percentage (2.66 == 2.66%).
    _er = _num(raw.get("engagementRate"))
    engagement_rate = round(_er, 4) if _er is not None else None

    return {
        "views": int(views) if views is not None else None,
        "impressions": int(impressions) if impressions is not None else None,
        "ctr": round(ctr, 4) if ctr is not None else None,
        "avg_view_pct": avg_view_pct,
        "avg_watch_time_s": avg_watch_s,
        "total_watch_time_s": total_watch_s,
        "likes": _int("likes"),
        "comments": _int("comments"),
        "shares": _int("shares"),
        "saves": _int("saves"),
        "reach": reach,
        "follows": _int("follows"),
        "reposts": _int("reposts"),
        "engagement_rate": engagement_rate,
        "skip_rate": skip_rate,
        "platform_video_id": platform_post_id,
        "raw_json": raw,
    }


_SUM_KEYS = ("views", "impressions", "likes", "comments", "shares", "saves",
             "reach", "follows", "reposts", "total_watch_time_s")
_RATE_KEYS = ("avg_view_pct", "avg_watch_time_s", "engagement_rate", "skip_rate")


def _aggregate_episode_platform(posts: list[dict]) -> dict:
    """Fold every post of one (episode, platform) into one metrics row.

    An episode publishes several posts per platform (main video + each
    short), but platform_analytics holds ONE row per (episode, platform,
    snapshot_date) and all its readers assume that. Storing posts one by
    one made each overwrite the last, so the row reflected whichever post
    Zernio listed last — not the episode. Counts are summed; rates are
    view-weighted (plain mean when no post has views); ctr is recomputed
    from the implied clicks. platform_video_id is the most-viewed post, so
    per-video enrichment (YouTube retention) lands on the episode's main
    video. raw_json keeps every post's own metrics.
    """
    if len(posts) == 1:
        return posts[0]

    def _sum(key):
        vals = [p[key] for p in posts if p.get(key) is not None]
        return sum(vals) if vals else None

    def _weighted(key):
        pairs = [(p[key], p.get("views") or 0) for p in posts if p.get(key) is not None]
        if not pairs:
            return None
        weight = sum(w for _, w in pairs)
        if weight:
            return round(sum(v * w for v, w in pairs) / weight, 4)
        return round(sum(v for v, _ in pairs) / len(pairs), 4)

    out = {k: _sum(k) for k in _SUM_KEYS}
    out.update({k: _weighted(k) for k in _RATE_KEYS})
    clicks = [p["ctr"] * p["impressions"] for p in posts
              if p.get("ctr") is not None and p.get("impressions")]
    out["ctr"] = (round(sum(clicks) / out["impressions"], 4)
                  if clicks and out["impressions"] else None)
    lead = max(posts, key=lambda p: p.get("views") or 0)
    out["platform_video_id"] = lead.get("platform_video_id")
    out["raw_json"] = {"posts": [
        {"platform_post_id": p.get("platform_video_id"), "metrics": p.get("raw_json")}
        for p in posts
    ]}
    return out


def sync_zernio_analytics(*, provider=None, db_path: Optional[Path] = None,
                          window_days: int = 7) -> dict:
    """
    Fetch every Zernio analytics post, map it to its Cortex episode, and
    upsert a `platform_analytics` row (source='api', today's snapshot).

    Returns:
        {ok, matched, rows_written, unmatched, unmatched_samples,
         by_channel: {channel_id: rows}, platforms: {platform: rows}}
    Never raises.
    """
    from hypogeum.analytics_store import store_platform_metrics, VALID_PLATFORMS

    rows = fetch_all_zernio_analytics(provider=provider, db_path=db_path)
    if not rows:
        return {"ok": False, "matched": 0, "rows_written": 0, "unmatched": 0,
                "unmatched_samples": [], "by_channel": {}, "platforms": {},
                "error": "no analytics returned by Zernio"}

    late_map = _late_post_id_to_episode(db_path)

    matched = written = unmatched = 0
    unmatched_samples: list[str] = []
    by_channel: dict = {}
    platforms: dict = {}

    grouped: dict = {}
    for r in rows:
        platform = r["platform"]
        if platform not in VALID_PLATFORMS:
            continue
        ep = late_map.get(r["late_post_id"])
        if not ep:
            unmatched += 1
            if len(unmatched_samples) < 8:
                unmatched_samples.append(
                    f"{platform}: {(r['content'] or '')[:50]!r} ({r['published_at']})"
                )
            continue
        matched += 1
        metrics = _translate_metrics(
            r["metrics"], platform_post_id=r["platform_post_id"], platform=platform,
        )
        # Skip a fully-empty metric row (Zernio knows the post but has no
        # numbers yet) so a real zero doesn't overwrite a later real value
        # ambiguously — but keep it if any signal at all is present.
        if not any(metrics.get(k) for k in
                   ("views", "impressions", "likes", "comments", "shares",
                    "avg_watch_time_s", "total_watch_time_s",
                    "reach", "follows", "engagement_rate")):
            continue
        grouped.setdefault((ep, platform), []).append(metrics)

    for ((episode_id, channel_id), platform), posts in grouped.items():
        metrics = _aggregate_episode_platform(posts)
        try:
            store_platform_metrics(
                episode_id, platform, metrics, source="api",
                window_days=window_days, db_path=db_path,
            )
            written += 1
            by_channel[channel_id] = by_channel.get(channel_id, 0) + 1
            platforms[platform] = platforms.get(platform, 0) + 1
        except Exception as exc:  # noqa: BLE001
            log.error("zernio_analytics_store_failed", episode_id=episode_id,
                      platform=platform, error=str(exc))

    log.info("zernio_analytics_synced", matched=matched, rows_written=written,
             unmatched=unmatched, by_channel=by_channel)
    return {
        "ok": True, "matched": matched, "rows_written": written,
        "unmatched": unmatched, "unmatched_samples": unmatched_samples,
        "by_channel": by_channel, "platforms": platforms,
    }
