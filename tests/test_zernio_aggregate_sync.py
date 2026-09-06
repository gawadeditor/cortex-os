"""Tests for hypogeum.publishing.zernio_aggregate_sync — pulling Zernio's
account-level aggregate analytics into channel_analytics_rollup."""
from __future__ import annotations

import json

import pytest

from hypogeum.db import initialize_schema, get_connection
from hypogeum.publishing import zernio_aggregate_sync as zas


class _FakeProvider:
    def __init__(self, *, decay=None, best=None, freq=None, followers=None, raise_on=None):
        self._decay = decay
        self._best = best
        self._freq = freq
        self._followers = followers
        self._raise_on = raise_on or set()

    def _wrap(self, data):
        if data is None:
            return {"status_code": 500, "data": None, "error": "no data"}
        return {"status_code": 200, "data": data, "error": None}

    def get_content_decay(self, *, db_path=None):
        if "get_content_decay" in self._raise_on:
            raise RuntimeError("boom")
        return self._wrap(self._decay)

    def get_best_time(self, *, db_path=None):
        return self._wrap(self._best)

    def get_posting_frequency(self, *, db_path=None):
        return self._wrap(self._freq)

    def get_follower_stats(self, *, from_date=None, db_path=None):
        return self._wrap(self._followers)


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
    assert set(out["kinds_written"]) >= {"content_decay", "best_time", "posting_frequency", "follower_stats"}

    for kind in ("content_decay", "best_time", "posting_frequency", "follower_stats"):
        roll = zas.get_rollup(kind, scope="factory", db_path=db_path)
        assert roll is not None, kind
        assert roll["payload"]


def test_follower_stats_split_onto_channels(db_path):
    # seed two channel profiles so the name match has something to hit
    from hypogeum.channel_config import create_channel_profile
    for cid, name in [("hypogeum", "The Hypogeum"), ("twinkle_mini_tales", "Twinkle Mini Tales")]:
        create_channel_profile(channel_id=cid, name=name, db_path=db_path)

    prov = _FakeProvider(followers=_FOLLOWERS)
    zas.sync_zernio_aggregates(provider=prov, db_path=db_path)

    hyp = zas.get_rollup("follower_stats", scope="hypogeum", db_path=db_path)
    assert hyp is not None
    plats = {a["platform"] for a in hyp["payload"]["accounts"]}
    assert plats == {"youtube", "instagram"}

    tw = zas.get_rollup("follower_stats", scope="twinkle_mini_tales", db_path=db_path)
    assert tw is not None
    assert tw["payload"]["accounts"][0]["followers"] == 1


def test_partial_failure_still_stores_what_it_got(db_path):
    prov = _FakeProvider(best=_BEST, raise_on={"get_content_decay"})
    out = zas.sync_zernio_aggregates(provider=prov, db_path=db_path)
    assert "best_time" in out["kinds_written"]
    assert "content_decay" not in out["kinds_written"]
    assert "get_content_decay" in out["errors"]


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
