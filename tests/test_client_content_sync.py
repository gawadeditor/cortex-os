"""Tests for hypogeum.publishing.client_content_sync — pulling an external
client's published content + performance from Zernio."""
from __future__ import annotations

import pytest

from hypogeum.db import initialize_schema, get_connection
from hypogeum import client_manager as cm
from hypogeum.publishing import client_content_sync as ccs


def _post(pid, platform, *, views=100, likes=5, er=2.5, caption="Watch this thing",
          thumb="http://t/x.jpg", published="2026-09-01T10:00:00Z"):
    a = {"views": views, "impressions": views, "likes": likes, "comments": 1,
         "shares": 0, "saves": 0, "clicks": 3, "engagementRate": er,
         "reach": views, "follows": 0}
    return {
        "_id": f"z_{pid}", "latePostId": None, "content": caption,
        "thumbnailUrl": thumb, "mediaType": "video", "publishedAt": published,
        "platformPostUrl": f"https://{platform}.example/{pid}",
        "platforms": [{"platform": platform, "platformPostId": pid,
                       "platformPostUrl": f"https://{platform}.example/{pid}",
                       "analytics": dict(a)}],
    }


class _FakeProvider:
    def __init__(self, external_posts, *, retention=None, demographics=None,
                 comments=None, insights=None):
        self._ext = external_posts
        self._retention = retention or {}
        self._demo = demographics
        self._comments = comments or {}
        self._insights = insights or {"metrics": {"views": {"total": 3603}}}

    def get_analytics(self, *, page=1, limit=100, source=None, account_id=None,
                      profile_id=None, post_id=None, from_date=None, to_date=None,
                      db_path=None):
        if source == "external" and page == 1:
            return {"status_code": 200,
                    "data": {"posts": self._ext, "pagination": {"pages": 1}}, "error": None}
        return {"status_code": 200, "data": {"posts": [], "pagination": {"pages": 1}}, "error": None}

    def get_youtube_video_retention(self, *, account_id, video_id, start_date=None,
                                    end_date=None, db_path=None):
        c = self._retention.get(video_id)
        return {"status_code": 200, "data": {"retentionCurve": c or []}, "error": None} if c is not None \
            else {"status_code": 404, "data": None, "error": "no"}

    def get_youtube_channel_insights(self, *, account_id, db_path=None):
        return {"status_code": 200, "data": self._insights, "error": None}

    def get_youtube_demographics(self, *, account_id, db_path=None):
        if self._demo is None:
            return {"status_code": 404, "data": None, "error": "no"}
        return {"status_code": 200, "data": self._demo, "error": None}

    def get_post_comments(self, *, account_id, post_id, limit=50, db_path=None):
        return {"status_code": 200,
                "data": {"comments": self._comments.get(post_id, [])}, "error": None}

    # aggregate endpoints
    def get_best_time(self, *, profile_id=None, db_path=None):
        return {"status_code": 200, "data": {"slots": [
            {"day_of_week": 5, "hour": 14, "avg_engagement": 3.2, "post_count": 4}]}, "error": None}

    def get_content_decay(self, *, profile_id=None, db_path=None):
        return {"status_code": 404, "data": None, "error": "no"}

    def get_posting_frequency(self, *, profile_id=None, db_path=None):
        return {"status_code": 404, "data": None, "error": "no"}

    def get_follower_stats(self, *, from_date=None, profile_id=None, db_path=None):
        return {"status_code": 404, "data": None, "error": "no"}


@pytest.fixture()
def client(tmp_path):
    p = tmp_path / "adv.db"
    initialize_schema(p)
    c = cm.create_client("Acme", db_path=p)
    cm.link_managed_account(c["client_id"], "youtube", provider_account_id="zacc_yt",
                            provider_profile_id="zprof", handle="@acme", db_path=p)
    return p, c["client_id"]


def test_external_posts_become_content_items_with_metrics(client):
    db, cid = client
    prov = _FakeProvider([_post("V1", "youtube", views=900, er=4.0),
                          _post("V2", "youtube", views=50, er=0.4)])
    out = ccs.sync_client_content(cid, provider=prov, db_path=db)
    assert out["ok"] is True
    assert out["content_items"] == 2 and out["metric_rows"] == 2

    items = cm.list_content_items(cid, db_path=db)
    assert {i["platform_post_id"] for i in items} == {"V1", "V2"}
    assert items[0]["caption"] == "Watch this thing"

    from hypogeum.analytics_store import get_episode_analytics
    v1_id = cm.new_content_id("youtube", "V1")
    rec = get_episode_analytics(v1_id, db_path=db)["youtube"]
    assert rec["views"] == 900 and rec["client_id"] == cid


def test_learning_pipeline_picks_up_client_content(client):
    db, cid = client
    prov = _FakeProvider([_post("V1", "youtube", views=5000, likes=400, er=8.0)])
    ccs.sync_client_content(cid, provider=prov, db_path=db)
    from hypogeum.learning_engine import extract_insights
    v1_id = cm.new_content_id("youtube", "V1")
    insights = extract_insights(v1_id, db_path=db)
    types = {i["insight_type"] for i in insights}
    assert "strong_engagement" in types and "high_engagement" in types


def test_retention_and_demographics_land_client_scoped(client):
    db, cid = client
    curve = [{"elapsedVideoTimeRatio": 0.0, "audienceWatchRatio": 1.0},
             {"elapsedVideoTimeRatio": 1.0, "audienceWatchRatio": 0.4}]
    prov = _FakeProvider(
        [_post("V1", "youtube")],
        retention={"V1": curve},
        demographics={"demographics": {"age": [{"dimension": "25-34", "value": 55.0}],
                                       "gender": [{"dimension": "male", "value": 70.0}]}},
    )
    out = ccs.sync_client_content(cid, provider=prov, db_path=db)
    assert out["retention"] == 1 and out["audience_rows"] >= 1

    from hypogeum.publishing.zernio_native_sync import get_channel_audience
    aud = get_channel_audience(cid, db_path=db)
    assert aud["youtube"]["gender"][0]["pct"] == 70.0

    conn = get_connection(db)
    try:
        r = conn.execute("SELECT avg_view_pct FROM platform_analytics WHERE client_id=? "
                         "AND retention_curve_json IS NOT NULL", (cid,)).fetchone()
    finally:
        conn.close()
    assert r and 0.6 < r["avg_view_pct"] < 0.8  # mean(1.0, 0.4) = 0.7


def test_no_accounts_returns_not_ok(tmp_path):
    p = tmp_path / "x.db"
    initialize_schema(p)
    c = cm.create_client("Empty", db_path=p)
    out = ccs.sync_client_content(c["client_id"], provider=_FakeProvider([]), db_path=p)
    assert out["ok"] is False and "accounts" in out["errors"]


def test_best_time_rollup_client_scoped(client):
    db, cid = client
    ccs.sync_client_content(cid, provider=_FakeProvider([_post("V1", "youtube")]), db_path=db)
    from hypogeum.publishing.zernio_aggregate_sync import get_rollup
    roll = get_rollup("best_time", scope=f"client:{cid}", db_path=db)
    assert roll and roll["payload"][0]["day_of_week"] == 5
