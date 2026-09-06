"""Tests for hypogeum.monetization_eligibility — measuring a channel's
distance from each platform's payout bar."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from hypogeum.db import initialize_schema, get_connection
from hypogeum.monetization_eligibility import (
    compute_channel_monetization, compute_factory_monetization,
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _seed_followers(db, channel_id, per_platform):
    conn = get_connection(db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO channel_analytics_rollup (rollup_id, scope, "
            "metric_kind, snapshot_date, payload_json, source, imported_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, channel_id, "follower_stats",
             datetime.now(timezone.utc).date().isoformat(),
             json.dumps({"accounts": [{"platform": p, "followers": f}
                                      for p, f in per_platform.items()]}),
             "api", _now()),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_yt_insight(db, channel_id, metrics):
    conn = get_connection(db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO channel_platform_insights (insight_id, channel_id, "
            "platform, snapshot_date, metrics_json, source, imported_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, channel_id, "youtube",
             datetime.now(timezone.utc).date().isoformat(),
             json.dumps(metrics), "api", _now()),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "mon.db"
    initialize_schema(p)
    from hypogeum.channel_config import create_channel_profile
    create_channel_profile(channel_id="hypogeum", name="The Hypogeum", db_path=p)
    return p


def test_far_from_every_bar(db):
    _seed_followers(db, "hypogeum", {"youtube": 10, "tiktok": 0, "instagram": 5, "facebook": 4})
    _seed_yt_insight(db, "hypogeum", {"estimatedMinutesWatched": 1528, "views": 3603})
    r = compute_channel_monetization("hypogeum", db_path=db)
    yt = r["platforms"]["youtube"]
    assert yt["eligible"] is False
    subs = next(x for x in yt["requirements"] if x["key"] == "yt_subscribers")
    assert subs["current"] == 10 and subs["target"] == 1000 and subs["pct"] == 1.0
    hours = next(x for x in yt["requirements"] if x["key"] == "yt_watch_hours")
    assert hours["current"] == 25.5  # 1528 / 60
    assert "to go" in yt["headline"]


def test_youtube_eligible_when_all_met(db):
    _seed_followers(db, "hypogeum", {"youtube": 1500})
    _seed_yt_insight(db, "hypogeum", {"estimatedMinutesWatched": 300000, "views": 50000})
    # 300000 / 60 = 5000 watch hours > 4000
    r = compute_channel_monetization("hypogeum", db_path=db)
    yt = r["platforms"]["youtube"]
    assert yt["eligible"] is True
    assert yt["headline"].startswith("Eligible")


def test_youtube_shorts_path_satisfies_one_of(db):
    _seed_followers(db, "hypogeum", {"youtube": 2000})
    _seed_yt_insight(db, "hypogeum", {"estimatedMinutesWatched": 100, "views": 12_000_000})
    r = compute_channel_monetization("hypogeum", db_path=db)
    assert r["platforms"]["youtube"]["eligible"] is True


def test_untracked_metric_reported_not_guessed(db):
    # no follower rollup, no insight → everything untracked
    r = compute_channel_monetization("hypogeum", db_path=db)
    ig = r["platforms"]["instagram"]
    req = ig["requirements"][0]
    assert req["current"] is None and req["tracked"] is False
    assert req["met"] is False
    assert "data" in ig["headline"].lower()


def test_factory_rollup_counts_eligible_pairs(db):
    _seed_followers(db, "hypogeum", {"youtube": 5000})
    _seed_yt_insight(db, "hypogeum", {"estimatedMinutesWatched": 300000, "views": 1})
    f = compute_factory_monetization(db_path=db)
    assert "hypogeum" in f["channels"]
    assert f["eligible_pairs"] >= 1
    assert f["total_pairs"] == 4  # one per platform
