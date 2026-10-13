"""Tests for hypogeum.revival_engine (Revival Engine step 1: diagnosis + buried gems)."""
from __future__ import annotations

import json

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.revival_engine import diagnose_channel

CH = "chan_a"


def _seed(db, videos, *, gained=None, traffic=()):
    """videos: [(views, avg_view_pct, skip_rate, curve|None)] — one IG episode each."""
    initialize_schema(db)
    with get_connection(db) as c:
        for i, (views, pct, skip, curve) in enumerate(videos):
            ep = f"ep_{i}"
            c.execute("INSERT INTO episode_topics(topic_id, channel_id, topic_text, episode_id, created_at, updated_at) "
                      "VALUES (?,?,?,?,?,?)", (f"t{i}", CH, f"topic {i}", ep, "2026-01-01", "2026-01-01"))
            c.execute("INSERT INTO episodes(episode_id, title, topic, created_at, status, published_at) "
                      "VALUES (?,?,?,?,?,?)", (ep, f"t{i}", f"t{i}", "2026-01-01", "published", "2026-09-01T10:00:00Z"))
            c.execute("INSERT INTO platform_analytics(record_id, episode_id, platform, snapshot_date, window_days, views, reach, "
                      "avg_view_pct, skip_rate, retention_curve_json, source, imported_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (f"r{i}", ep, "instagram", "2026-10-10", 30, views, views, pct, skip,
                       json.dumps(curve) if curve else None, "api", "2026-10-10"))
        if gained is not None:
            c.execute("INSERT INTO channel_platform_insights(insight_id, channel_id, platform, snapshot_date, metrics_json, source, imported_at) "
                      "VALUES ('i1',?,?,?,?,?,?)", (CH, "instagram", "2026-10-10", json.dumps({"followers_gained": gained}), "api", "2026-10-10"))
        for key, views, pct in traffic:
            c.execute("INSERT INTO youtube_traffic_insights(channel_id, snapshot_date, dimension, key, subkey, views, avg_view_pct, window_days, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (CH, "2026-10-10", "traffic_type", key, "", views, pct, 89, "2026-10-10"))
        c.commit()


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "rev.db"


def test_no_data_is_not_ok(db):
    initialize_schema(db)
    r = diagnose_channel(CH, db_path=db)
    assert r["ok"] is False


def test_conversion_leak_and_buried_gem(db):
    vids = [(1000, 0.2, 40.0, None)] * 5 + [(50, 0.9, 30.0, None)] + [(1000, 0.2, 40.0, None)]
    _seed(db, vids, gained=2)
    r = diagnose_channel(CH, db_path=db)
    assert r["ok"]
    ig = r["platforms"]["instagram"]
    conv = next(l for l in ig["layers"] if l["layer"] == "conversion")
    assert conv["status"] == "weak"
    assert ig["primary_leak"] in ("sharing", "conversion")  # seeded posts have zero shares, so sharing is flagged first
    assert [g["episode_id"] for g in r["buried_gems"]] == ["ep_5"]


def test_small_sample_is_insufficient_not_guessed(db):
    _seed(db, [(100, 0.5, 40.0, None)] * 3, gained=0)
    r = diagnose_channel(CH, db_path=db)
    dist = next(l for l in r["platforms"]["instagram"]["layers"] if l["layer"] == "distribution")
    assert dist["status"] == "insufficient"
    assert r["buried_gems"] == []


def test_missing_followers_signal_is_blind(db):
    _seed(db, [(1000, 0.3, 40.0, None)] * 7)
    conv = next(l for l in diagnose_channel(CH, db_path=db)["platforms"]["instagram"]["layers"] if l["layer"] == "conversion")
    assert conv["status"] == "blind"


def test_source_contrast_needs_dominant_source(db):
    _seed(db, [(1000, 0.3, 40.0, None)] * 7,
          traffic=[("SHORTS", 9000, 60.0), ("YT_SEARCH", 400, 90.0)])
    r = diagnose_channel(CH, db_path=db)
    assert [c["source"] for c in r["source_contrast"]] == ["YT_SEARCH"]
    # balanced mix -> nothing to report
    with get_connection(db) as c:
        c.execute("UPDATE youtube_traffic_insights SET views=5000")
        c.commit()
    assert diagnose_channel(CH, db_path=db)["source_contrast"] == []


def test_youtube_reach_feeds_packaging_layer(db):
    _seed(db, [(1000, 0.3, 40.0, None)] * 7)
    with get_connection(db) as c:
        c.execute("UPDATE platform_analytics SET platform='youtube', platform_video_id='v'||episode_id")
        for vid, imp, ctr in [("vep_0", 5000, 0.05), ("vep_1", 5000, 0.05), ("vep_2", 5000, 0.05), ("vep_3", 5000, 0.01),
                              ("vep_4", 100, 0.001)]:
            c.execute("INSERT INTO youtube_reach_daily VALUES (?,?,?,?,?,?)", (CH, "2026-10-09", vid, imp, ctr, "2026-10-10"))
        c.commit()
    r = diagnose_channel(CH, db_path=db)
    hook = next(l for l in r["platforms"]["youtube"]["layers"] if l["layer"] == "hook")
    assert hook["status"] == "weak"
    # the under-clicked video qualifies; the tiny-impression one is too thin to judge
    assert [c["video_id"] for c in r["packaging_candidates"]] == ["vep_3"]
    assert r["packaging_candidates"][0]["episode_id"] == "ep_3"
    assert r["blind_spots"] == []


def test_parse_reach_csv_and_store_roundtrip(db):
    from hypogeum.youtube_reach import channel_ctr, parse_reach_csv, _store
    rows = parse_reach_csv("date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
                           "20261010,UC,a,1000,0.10\n20261010,UC,b,3000,0.02\nbad,row\n")
    assert [(r["day"], r["video_id"]) for r in rows] == [("2026-10-10", "a"), ("2026-10-10", "b")]
    initialize_schema(db)
    with get_connection(db) as c:
        assert _store(c, CH, rows) == 2
        _store(c, CH, rows)  # idempotent
        d = channel_ctr(c, CH)
    assert d["impressions"] == 4000 and round(d["ctr"], 4) == 0.04


def test_sharing_layer_and_audience_voice(db):
    _seed(db, [(1000, 0.3, 40.0, None)] * 7)
    with get_connection(db) as c:
        c.execute("UPDATE platform_analytics SET shares=1, saves=0")           # 7 per 7000 reach = 0.1%
        for i in range(12):
            text = "Please make a video about the Roman aqueducts?" if i < 3 else f"great video number {i} amazing history"
            c.execute("INSERT INTO audience_comments VALUES (?,?,?,?,?,?,?)", (CH, f"c{i}", "v", text, i, None, "2026-10-10"))
        c.commit()
    r = diagnose_channel(CH, db_path=db)
    sharing = next(l for l in r["platforms"]["instagram"]["layers"] if l["layer"] == "sharing")
    assert sharing["status"] == "weak"
    assert r["audience_voice"]["comments"] == 12 and len(r["audience_voice"]["requests"]) == 3
    from hypogeum.revival_actions import _proposals
    assert [p["kind"] for p in _proposals(r) if p["kind"] == "audience_topic"] == ["audience_topic"] * 3


def test_audience_voice_needs_enough_comments(db):
    _seed(db, [(1000, 0.3, 40.0, None)] * 7)
    assert diagnose_channel(CH, db_path=db)["audience_voice"] is None


def test_later_zero_does_not_erase_earlier_cumulative_count(db):
    _seed(db, [(1000, 0.3, 40.0, None)] * 7)
    with get_connection(db) as c:
        c.execute("UPDATE platform_analytics SET shares=5")
        c.execute("INSERT INTO platform_analytics(record_id, episode_id, platform, snapshot_date, window_days, views, reach, shares, source, imported_at) "
                  "SELECT 'z'||record_id, episode_id, platform, '2026-10-12', 7, 900, 900, 0, 'api', '2026-10-12' FROM platform_analytics")
        c.commit()
    sharing = next(l for l in diagnose_channel(CH, db_path=db)["platforms"]["instagram"]["layers"] if l["layer"] == "sharing")
    assert sharing["evidence"].get("shares_plus_saves") == 35
