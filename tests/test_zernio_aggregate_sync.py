"""Tests for hypogeum.publishing.zernio_aggregate_sync — pulling Zernio's
account-level aggregate analytics into channel_analytics_rollup."""
from __future__ import annotations

import json

import pytest

from hypogeum.db import initialize_schema, get_connection
from hypogeum.publishing import zernio_aggregate_sync as zas


class _FakeProvider:
    """`best` / `decay` / `freq` may be a plain payload (same for every
    scope) or a {profile_id_or_None: payload} dict for per-profile data.
    `accounts` is the /analytics accounts[] used to build the profile map."""

    def __init__(self, *, decay=None, best=None, freq=None, followers=None,
                 raise_on=None, accounts=None):
        self._decay = decay
        self._best = best
        self._freq = freq
        self._followers = followers
        self._raise_on = raise_on or set()
        self._accounts = accounts or []

    def _wrap(self, data):
        if data is None:
            return {"status_code": 500, "data": None, "error": "no data"}
        return {"status_code": 200, "data": data, "error": None}

    @staticmethod
    def _pick(spec, profile_id):
        if isinstance(spec, dict) and ("slots" not in spec and "buckets" not in spec
                                       and "frequency" not in spec and "accounts" not in spec):
            return spec.get(profile_id)
        return spec

    def get_analytics(self, *, page=1, limit=100, db_path=None):
        return {"status_code": 200,
                "data": {"posts": [], "pagination": {"pages": 1},
                         "accounts": self._accounts}, "error": None}

    def get_content_decay(self, *, profile_id=None, db_path=None):
        if "get_content_decay" in self._raise_on:
            raise RuntimeError("boom")
        return self._wrap(self._pick(self._decay, profile_id))

    def get_best_time(self, *, profile_id=None, db_path=None):
        return self._wrap(self._pick(self._best, profile_id))

    def get_posting_frequency(self, *, profile_id=None, db_path=None):
        return self._wrap(self._pick(self._freq, profile_id))

    def get_follower_stats(self, *, from_date=None, profile_id=None, db_path=None):
        return self._wrap(self._pick(self._followers, profile_id))


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "agg.db"
    initialize_schema(p)
    return p


_DECAY = {"buckets": [
    {"bucket_order": 0, "bucket_label": "0-6h", "avg_pct_of_final": 53.6, "post_count": 27},
    {"bucket_order": 1, "bucket_label": "6-12h", "avg_pct_of_final": 75.8, "post_count": 27},
]}
_BEST = {"slots": [
    {"day_of_week": 0, "hour": 10, "avg_engagement": 21, "post_count": 2},
    {"day_of_week": 2, "hour": 18, "avg_engagement": 9.25, "post_count": 4},
    {"day_of_week": 6, "hour": 14, "avg_engagement": 99, "post_count": 1},  # single post — must be ignored downstream
]}
_FREQ = {"frequency": [
    {"platform": "youtube", "posts_per_week": 12, "avg_engagement_rate": 16.3, "avg_engagement": 9.7, "weeks_count": 1},
]}
_FOLLOWERS = {
    "accounts": [
        {"platform": "youtube", "name": "The Hypogeum", "currentFollowers": 10},
        {"platform": "instagram", "name": "The Hypogeum", "currentFollowers": 5},
        {"platform": "tiktok", "name": "twinkle_mini_tales", "currentFollowers": 1},
        {"platform": "youtube", "name": "Some Unrelated Brand", "currentFollowers": 3},
    ],
    "dateRange": {"from": "2026-08-07"}, "granularity": "day",
}


def test_all_four_kinds_written_to_factory_scope(db_path):
    prov = _FakeProvider(decay=_DECAY, best=_BEST, freq=_FREQ, followers=_FOLLOWERS)
    out = zas.sync_zernio_aggregates(provider=prov, db_path=db_path)
    assert out["ok"] is True
    assert "factory" in out["scopes"]
    assert set(out["kinds_written"]) >= {"content_decay", "best_time", "posting_frequency", "follower_stats"}

    for kind in ("content_decay", "best_time", "posting_frequency", "follower_stats"):
        roll = zas.get_rollup(kind, scope="factory", db_path=db_path)
        assert roll is not None, kind
        assert roll["payload"]


_ACCOUNTS = [
    {"platform": "youtube", "name": "The Hypogeum", "profileId": "prof_hyp"},
    {"platform": "tiktok", "name": "TheHypogeum", "profileId": "prof_hyp"},
    {"platform": "instagram", "name": "twinkle_mini_tales", "profileId": "prof_tw"},
    {"platform": "youtube", "name": "Some Unrelated Brand", "profileId": "prof_other"},
]


def _seed_channels(db_path):
    from hypogeum.channel_config import create_channel_profile
    for cid, name in [("hypogeum", "The Hypogeum"),
                      ("twinkle_mini_tales", "Twinkle Mini Tales")]:
        create_channel_profile(channel_id=cid, name=name, db_path=db_path)


def test_per_profile_best_time_scoped_to_channel(db_path):
    _seed_channels(db_path)
    hyp_best = {"slots": [{"day_of_week": 0, "hour": 10, "avg_engagement": 21, "post_count": 4}]}
    tw_best = {"slots": [{"day_of_week": 5, "hour": 14, "avg_engagement": 2.3, "post_count": 4}]}
    prov = _FakeProvider(
        accounts=_ACCOUNTS,
        best={None: _BEST, "prof_hyp": hyp_best, "prof_tw": tw_best},
    )
    out = zas.sync_zernio_aggregates(provider=prov, db_path=db_path)
    assert set(out["scopes"]) >= {"factory", "hypogeum", "twinkle_mini_tales"}

    hyp = zas.get_rollup("best_time", scope="hypogeum", db_path=db_path)
    assert hyp["payload"][0]["day_of_week"] == 0 and hyp["payload"][0]["hour"] == 10
    tw = zas.get_rollup("best_time", scope="twinkle_mini_tales", db_path=db_path)
    assert tw["payload"][0]["day_of_week"] == 5 and tw["payload"][0]["hour"] == 14


def test_no_profile_map_still_writes_factory(db_path):
    # no accounts => no per-channel scopes, factory only
    prov = _FakeProvider(best=_BEST)
    out = zas.sync_zernio_aggregates(provider=prov, db_path=db_path)
    assert out["scopes"] == ["factory"]
    assert zas.get_rollup("best_time", scope="factory", db_path=db_path) is not None


def test_partial_failure_still_stores_what_it_got(db_path):
    prov = _FakeProvider(best=_BEST, raise_on={"get_content_decay"})
    out = zas.sync_zernio_aggregates(provider=prov, db_path=db_path)
    assert "best_time" in out["kinds_written"]
    assert "content_decay" not in out["kinds_written"]
    assert any(k.startswith("get_content_decay") for k in out["errors"])


def test_upsert_is_idempotent_per_day(db_path):
    prov = _FakeProvider(best=_BEST)
    zas.sync_zernio_aggregates(provider=prov, db_path=db_path)
    zas.sync_zernio_aggregates(provider=prov, db_path=db_path)
    conn = get_connection(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM channel_analytics_rollup "
            "WHERE scope='factory' AND metric_kind='best_time'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_no_data_returns_not_ok(db_path):
    out = zas.sync_zernio_aggregates(provider=_FakeProvider(), db_path=db_path)
    assert out["ok"] is False
    assert out["kinds_written"] == []


def test_reliable_best_time_slots_drops_thin_slots_and_sorts():
    from hypogeum.publishing.zernio_aggregate_sync import reliable_best_time_slots, BEST_TIME_MIN_POSTS
    assert BEST_TIME_MIN_POSTS == 8
    payload = [
        {"day_of_week": 2, "hour": 21, "avg_engagement": 3.0, "post_count": 1},   # n=1 noise
        {"day_of_week": 0, "hour": 10, "avg_engagement": 4.6, "post_count": 14},
        {"day_of_week": 2, "hour": 18, "avg_engagement": 4.9, "post_count": 15},
        {"day_of_week": 5, "hour": 14, "avg_engagement": 3.0, "post_count": 4},   # below 8
        {"day_of_week": None, "hour": 9, "avg_engagement": 9, "post_count": 20},  # malformed
    ]
    out = reliable_best_time_slots(payload)
    assert [(s["day_of_week"], s["hour"]) for s in out] == [(2, 18), (0, 10)]
    assert reliable_best_time_slots(None) == []
