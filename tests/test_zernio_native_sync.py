"""Tests for hypogeum.publishing.zernio_native_sync — platform-native
account analytics (YouTube channel-insights + retention, IG/YT
demographics, per-account follower series) into channel-scoped tables."""
from __future__ import annotations

import json

import pytest

from hypogeum.db import initialize_schema, get_connection
from hypogeum.publishing import zernio_native_sync as zns


_ACCOUNTS = [
    {"_id": "acc_yt_hyp", "platform": "youtube", "name": "The Hypogeum",
     "profileId": {"_id": "prof_hyp", "name": "Hypogeum"}},
    {"_id": "acc_ig_hyp", "platform": "instagram", "name": "The Hypogeum",
     "profileId": {"_id": "prof_hyp", "name": "Hypogeum"}},
    {"_id": "acc_tt_tw", "platform": "tiktok", "name": "twinkle_mini_tales",
     "profileId": {"_id": "prof_tw", "name": "Twinkle"}},
]


class _FakeProvider:
    def __init__(self, **data):
        self._d = data

    def _wrap(self, key):
        v = self._d.get(key)
        if v is None:
            return {"status_code": 404, "data": None, "error": "not found"}
        return {"status_code": 200, "data": v, "error": None}

    def get_accounts(self, *, db_path=None):
        return {"status_code": 200, "data": {"accounts": _ACCOUNTS}, "error": None}

    def get_analytics(self, *, page=1, limit=100, db_path=None):
        return {"status_code": 200, "data": {"accounts": _ACCOUNTS}, "error": None}

    def get_youtube_channel_insights(self, *, account_id, db_path=None):
        return self._wrap("yt_channel")

    def get_youtube_demographics(self, *, account_id, db_path=None):
        return self._wrap("yt_demo")

    def get_youtube_video_retention(self, *, account_id, video_id, start_date=None,
                                    end_date=None, db_path=None):
        return self._wrap(f"retention_{video_id}")

    def get_instagram_account_insights(self, *, account_id, db_path=None):
        return self._wrap("ig_acct")

    def get_instagram_demographics(self, *, account_id, db_path=None):
        return self._wrap("ig_demo")

    def get_instagram_follower_history(self, *, account_id, db_path=None):
        return self._wrap("ig_fh")

    def get_tiktok_account_insights(self, *, account_id, db_path=None):
        return self._wrap("tt_acct")

    def get_facebook_page_insights(self, *, account_id, db_path=None):
        return self._wrap("fb_page")


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "native.db"
    initialize_schema(p)
    from hypogeum.channel_config import create_channel_profile
    create_channel_profile(channel_id="hypogeum", name="The Hypogeum", db_path=p)
    create_channel_profile(channel_id="twinkle_mini_tales", name="Twinkle Mini Tales", db_path=p)
    return p


_YT_CHANNEL = {"metrics": {"views": {"total": 3603},
                           "estimatedMinutesWatched": {"total": 1528},
                           "subscribersGained": {"total": 8},
                           "subscribersLost": {"total": 1}}}
_YT_DEMO = {"demographics": {
    "age": [{"dimension": "55-64", "value": 26.7}, {"dimension": "65-", "value": 39.4}],
    "gender": [{"dimension": "male", "value": 78.7}, {"dimension": "female", "value": 21.3}],
}}
_IG_ACCT = {"metrics": {"reach": {"total": 287}, "views": {"total": 366},
                        "accounts_engaged": {"total": 4}}}
_IG_FH = {"metrics": {"follower_count": {"total": 5}, "followers_gained": {"total": 1}}}
_TT_ACCT = {"metrics": {"follower_count": {"total": 0}, "likes_count": {"total": 30},
                        "video_count": {"total": 9}}}


def test_channel_insights_and_demographics_stored_per_channel(db_path):
    prov = _FakeProvider(yt_channel=_YT_CHANNEL, yt_demo=_YT_DEMO,
                         ig_acct=_IG_ACCT, ig_fh=_IG_FH, tt_acct=_TT_ACCT)
    out = zns.sync_zernio_native_analytics(provider=prov, db_path=db_path)
    assert out["ok"] is True
    assert out["accounts"] == 3

    yt = zns.get_channel_platform_insights("hypogeum", "youtube", db_path=db_path)
    assert yt["metrics"]["views"] == 3603
    assert yt["metrics"]["estimatedMinutesWatched"] == 1528

    ig = zns.get_channel_platform_insights("hypogeum", "instagram", db_path=db_path)
    assert ig["metrics"]["reach"] == 287
    assert ig["metrics"]["follower_count"] == 5  # merged follower-history

    tt = zns.get_channel_platform_insights("twinkle_mini_tales", "tiktok", db_path=db_path)
    assert tt["metrics"]["video_count"] == 9

    aud = zns.get_channel_audience("hypogeum", platform="youtube", db_path=db_path)
    assert aud["youtube"]["age"][1]["label"] == "65-"
    assert aud["youtube"]["gender"][0]["pct"] == 78.7


def test_youtube_video_retention_backfills_avg_view_pct(db_path):
    # seed a youtube platform_analytics row with a video id and no retention
    from hypogeum.analytics_store import store_platform_metrics
    conn = get_connection(db_path)
    try:
        conn.execute("INSERT INTO episode_topics (topic_id, channel_id, episode_id, "
                     "topic_text, status, created_at, updated_at) VALUES "
                     "('t1','hypogeum','ep_x','x','consumed','x','x')")
        conn.commit()
    finally:
        conn.close()
    store_platform_metrics("ep_x", "youtube",
                           {"views": 500, "platform_video_id": "VID123"},
                           source="api", db_path=db_path)

    curve = [{"elapsedRatio": 0.0, "watchRatio": 1.0},
             {"elapsedRatio": 0.5, "watchRatio": 0.6},
             {"elapsedRatio": 1.0, "watchRatio": 0.3}]
    prov = _FakeProvider(retention_VID123={"retentionCurve": curve, "durationSeconds": 42})
    out = zns.sync_zernio_native_analytics(provider=prov, db_path=db_path)
    assert out["retention_videos"] == 1

    from hypogeum.analytics_store import get_episode_analytics
    rec = get_episode_analytics("ep_x", db_path=db_path)["youtube"]
    assert rec["retention_curve_json"]
    # mean watchRatio (1.0 + 0.6 + 0.3)/3 = 0.633
    assert abs(rec["avg_view_pct"] - 0.6333) < 1e-3


def test_empty_retention_curve_is_skipped(db_path):
    from hypogeum.analytics_store import store_platform_metrics
    conn = get_connection(db_path)
    try:
        conn.execute("INSERT INTO episode_topics (topic_id, channel_id, episode_id, "
                     "topic_text, status, created_at, updated_at) VALUES "
                     "('t2','hypogeum','ep_y','y','consumed','y','y')")
        conn.commit()
    finally:
        conn.close()
    store_platform_metrics("ep_y", "youtube",
                           {"views": 3, "platform_video_id": "VIDLOW"},
                           source="api", db_path=db_path)
    prov = _FakeProvider(retention_VIDLOW={"retentionCurve": []})
    out = zns.sync_zernio_native_analytics(provider=prov, db_path=db_path)
    assert out["retention_videos"] == 0


def test_no_mapped_accounts_returns_not_ok(db_path):
    class _Empty:
        def get_accounts(self, *, db_path=None):
            return {"status_code": 200, "data": {"accounts": []}, "error": None}
        def get_analytics(self, *, page=1, limit=1, db_path=None):
            return {"status_code": 200, "data": {"accounts": []}, "error": None}
    out = zns.sync_zernio_native_analytics(provider=_Empty(), db_path=db_path)
    assert out["ok"] is False
    assert "account_map" in out["errors"]


def test_partial_platform_failure_isolated(db_path):
    # only YouTube data present; IG/TT endpoints 404 → still ok, YT stored
    prov = _FakeProvider(yt_channel=_YT_CHANNEL)
    out = zns.sync_zernio_native_analytics(provider=prov, db_path=db_path)
    assert out["ok"] is True
    assert zns.get_channel_platform_insights("hypogeum", "youtube", db_path=db_path) is not None
    assert zns.get_channel_platform_insights("hypogeum", "instagram", db_path=db_path) is None
