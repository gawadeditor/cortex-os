"""Tests for hypogeum.publishing.zernio_analytics_sync — mapping Zernio
GET /v1/analytics posts back to Cortex episodes and into platform_analytics."""
from __future__ import annotations

import pytest

from hypogeum.db import initialize_schema, get_connection
from hypogeum.publishing import zernio_analytics_sync as zas
from hypogeum.analytics_store import get_episode_analytics


class _FakeProvider:
    """Stands in for ZernioProvider.get_analytics — returns one page."""

    def __init__(self, posts):
        self._posts = posts

    def get_analytics(self, *, page=1, limit=100, db_path=None):
        if page > 1:
            return {"status_code": 200, "data": {"posts": [], "pagination": {"pages": 1}}, "error": None}
        return {
            "status_code": 200,
            "data": {
                "overview": {"totalPosts": len(self._posts)},
                "posts": self._posts,
                "pagination": {"page": 1, "limit": limit, "total": len(self._posts), "pages": 1},
                "hasAnalyticsAccess": True,
            },
            "error": None,
        }


def _post(late_id, platform, ppid, *, views=100, impressions=120, likes=5,
          comments=1, ig_avg_ms=0, ig_total_ms=0, duration=None):
    return {
        "_id": f"zernio_{platform}_{late_id}",
        "latePostId": late_id,
        "content": f"caption for {late_id}",
        "publishedAt": "2026-09-02T19:00:00.000Z",
        "status": "published",
        "analytics": {
            "views": views, "impressions": impressions, "likes": likes,
            "comments": comments, "shares": 0, "saves": 0, "clicks": 3,
            "igReelsAvgWatchTime": ig_avg_ms, "igReelsVideoViewTotalTime": ig_total_ms,
            "videoDurationSeconds": duration,
        },
        "platforms": [{
            "platform": platform,
            "status": "published",
            "platformPostId": ppid,
            "platformPostUrl": f"https://{platform}.example/{ppid}",
            "analytics": {
                "views": views, "impressions": impressions, "likes": likes,
                "comments": comments, "shares": 0, "saves": 0, "clicks": 3,
                "igReelsAvgWatchTime": ig_avg_ms, "igReelsVideoViewTotalTime": ig_total_ms,
                "videoDurationSeconds": duration,
            },
        }],
    }


def _ledger_row(conn, *, platform, episode_id="ep_abc", channel="twinkle",
                late_post_id="LATE1", suffix=""):
    aid = f"att_{platform}{suffix}"
    iid = f"int_{platform}{suffix}"
    rid = f"r_{platform}{suffix}"
    conn.execute(
        "INSERT INTO publication_intents (publication_intent_id, channel_id, episode_id, "
        "deliverable_id, platform, status, created_at, updated_at) "
        f"VALUES ('{iid}','{channel}','{episode_id}','main_episode','{platform}','succeeded','x','x')"
    )
    conn.execute(
        "INSERT INTO publishing_attempts (attempt_id, publication_intent_id, channel_id, "
        "episode_id, deliverable_id, platform, provider_id, provider_request_key, "
        "attempt_number, state, created_at, updated_at) "
        f"VALUES ('{aid}','{iid}','{channel}','{episode_id}','main_episode','{platform}',"
        f"'zernio','rk_{platform}{suffix}',1,'succeeded','x','x')"
    )
    conn.execute(
        "INSERT INTO publishing_receipts (receipt_id, attempt_id, provider_id, platform, "
        "provider_publication_id, native_platform_content_id, provider_status, "
        f"acknowledged_at, raw_receipt_metadata_json) VALUES "
        f"('{rid}','{aid}','zernio','{platform}','{late_post_id}',NULL,'published','x','{{}}')"
    )


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "t.db"
    initialize_schema(p)
    conn = get_connection(p)
    try:
        _ledger_row(conn, platform="youtube")
        conn.commit()
    finally:
        conn.close()
    return p


def test_matched_post_writes_platform_analytics(db_path):
    provider = _FakeProvider([_post("LATE1", "youtube", "yt_vid_1", views=250, impressions=400)])
    out = zas.sync_zernio_analytics(provider=provider, db_path=db_path)
    assert out["ok"] is True
    assert out["matched"] == 1
    assert out["rows_written"] == 1
    assert out["by_channel"] == {"twinkle": 1}

    rec = get_episode_analytics("ep_abc", db_path=db_path)
    assert rec["youtube"]["views"] == 250
    assert rec["youtube"]["source"] == "api"
    assert rec["youtube"]["platform_video_id"] == "yt_vid_1"
    # clicks 3 / impressions 400
    assert abs(rec["youtube"]["ctr"] - 0.0075) < 1e-6


def test_unmatched_post_is_reported_not_written(db_path):
    provider = _FakeProvider([_post("UNKNOWN_LATE", "tiktok", "tt1")])
    out = zas.sync_zernio_analytics(provider=provider, db_path=db_path)
    assert out["matched"] == 0
    assert out["unmatched"] == 1
    assert out["rows_written"] == 0
    assert get_episode_analytics("ep_abc", db_path=db_path) == {}


def test_instagram_watch_time_translated_other_platforms_not(db_path):
    conn = get_connection(db_path)
    try:
        _ledger_row(conn, platform="instagram")
        _ledger_row(conn, platform="facebook")
        conn.commit()
    finally:
        conn.close()

    provider = _FakeProvider([
        _post("LATE1", "instagram", "ig1", ig_avg_ms=15000, ig_total_ms=300000, duration=30),
        _post("LATE1", "facebook", "fb1", ig_avg_ms=0, ig_total_ms=0),
    ])
    zas.sync_zernio_analytics(provider=provider, db_path=db_path)
    rec = get_episode_analytics("ep_abc", db_path=db_path)
    assert rec["instagram"]["avg_watch_time_s"] == 15.0
    assert rec["instagram"]["total_watch_time_s"] == 300.0
    assert abs(rec["instagram"]["avg_view_pct"] - 0.5) < 1e-6
    assert rec["facebook"]["avg_watch_time_s"] is None


def test_empty_metrics_post_skipped(db_path):
    p = _post("LATE1", "youtube", "yt1", views=0, impressions=0, likes=0,
              comments=0, ig_avg_ms=0, ig_total_ms=0)
    out = zas.sync_zernio_analytics(provider=_FakeProvider([p]), db_path=db_path)
    assert out["matched"] == 1
    assert out["rows_written"] == 0  # nothing real to record yet


def test_no_data_from_provider(db_path):
    class _Empty:
        def get_analytics(self, **k):
            return {"status_code": 500, "data": None, "error": "boom"}

    out = zas.sync_zernio_analytics(provider=_Empty(), db_path=db_path)
    assert out["ok"] is False
    assert out["rows_written"] == 0
