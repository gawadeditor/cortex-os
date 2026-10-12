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

    def get_post_comments(self, *, account_id, post_id, limit=50, db_path=None):
        return self._wrap(f"comments_{post_id}")


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
    assert aud["youtube"]["age"][1]["label"] == "65+"  # normalised from YouTube's "65-"
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
    assert abs(rec["avg_watch_time_s"] - 0.6333 * 42) < 0.1


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


def test_post_comments_stored_owner_flag_split(db_path):
    from hypogeum.analytics_store import store_platform_metrics
    conn = get_connection(db_path)
    try:
        conn.execute("INSERT INTO episode_topics (topic_id, channel_id, episode_id, "
                     "topic_text, status, created_at, updated_at) VALUES "
                     "('tc','hypogeum','ep_c','c','consumed','c','c')")
        conn.commit()
    finally:
        conn.close()
    store_platform_metrics("ep_c", "youtube",
                           {"views": 500, "comments": 2, "platform_video_id": "VIDC"},
                           source="api", db_path=db_path)
    prov = _FakeProvider(comments_VIDC={"comments": [
        {"id": "c1", "text": "This is the best explanation I've seen — do one on the Antikythera mechanism next!",
         "from": {"name": "@viewer1", "isOwner": False}, "likeCount": 4, "createdAt": "2026-09-05T12:00:00Z"},
        {"id": "c2", "text": "Sources in the description.",
         "from": {"name": "@TheHypogeum", "isOwner": True}, "likeCount": 1, "createdAt": "2026-09-05T11:00:00Z"},
    ]})
    out = zns.sync_zernio_native_analytics(provider=prov, db_path=db_path)
    assert out["comments"] == 2
    assert out["audience_comments"] == 1

    ep_c = zns.get_episode_comments("ep_c", db_path=db_path)
    assert len(ep_c) == 1  # owner excluded
    assert "Antikythera" in ep_c[0]["text"]

    recent = zns.get_recent_audience_comments("hypogeum", db_path=db_path)
    assert len(recent) == 1

    from hypogeum.audience_profile import summarize_audience_voice
    voice = summarize_audience_voice("hypogeum", db_path=db_path)
    assert voice is not None and "Antikythera" in voice
    assert "4♥" in voice


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


def test_empty_curve_videos_do_not_starve_videos_with_data(db_path, monkeypatch):
    """Regression (2026-10-11): tiny videos return an empty curve forever; they used to fill the
    per-sync call cap so the videos that have a curve were never fetched."""
    from hypogeum.analytics_store import store_platform_metrics
    conn = get_connection(db_path)
    try:
        for i in range(5):
            conn.execute("INSERT INTO episode_topics (topic_id, channel_id, episode_id, topic_text, status, "
                         "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                         (f"t{i}", "hypogeum", f"ep_{i}", "x", "consumed", "x", "x"))
        conn.commit()
    finally:
        conn.close()
    for i in range(4):   # tiny videos first in insertion order
        store_platform_metrics(f"ep_{i}", "youtube", {"views": 1, "platform_video_id": f"TINY{i}"},
                               source="api", db_path=db_path)
    store_platform_metrics("ep_4", "youtube", {"views": 300, "platform_video_id": "BIG"}, source="api", db_path=db_path)
    monkeypatch.setattr(zns, "_MAX_RETENTION_CALLS_PER_SYNC", 1)
    curve = [{"elapsedRatio": 0.0, "watchRatio": 1.0}, {"elapsedRatio": 1.0, "watchRatio": 0.5}]
    prov = _FakeProvider(retention_BIG={"retentionCurve": curve, "durationSeconds": 30})
    out = zns.sync_zernio_native_analytics(provider=prov, db_path=db_path)
    assert out["retention_videos"] == 1


def test_aggregate_sync_holds_no_database_connection_while_fetching(db_path):
    """Regression (2026-10-12): the hourly aggregate sync kept an uncommitted write open while the next scope's HTTP
    requests ran, holding the database write lock for 20-30s."""
    import sqlite3
    import hypogeum.publishing.zernio_aggregate_sync as agg

    fetched = {"n": 0}

    def _fake_fetch(provider, db, *, profile_id, errors):
        other = sqlite3.connect(str(db_path), timeout=0.2)        # would raise 'database is locked' if a write were open
        try:
            other.execute("CREATE TABLE IF NOT EXISTS _probe (x)")
            other.execute("INSERT INTO _probe VALUES (1)")
            other.commit()
        finally:
            other.close()
        fetched["n"] += 1
        return {"best_time": {"slots": []}}

    orig = (agg._fetch_scope, agg._cortex_channels, agg._zernio_profile_channel_map)
    agg._fetch_scope = _fake_fetch
    agg._cortex_channels = lambda db: ["hypogeum", "twinkle_mini_tales"]
    agg._zernio_profile_channel_map = lambda prov, db, ch: {"p1": "hypogeum", "p2": "twinkle_mini_tales"}
    try:
        out = agg.sync_zernio_aggregates(provider=object(), db_path=db_path)
    finally:
        agg._fetch_scope, agg._cortex_channels, agg._zernio_profile_channel_map = orig
    assert fetched["n"] == 3 and out["ok"]
