"""Tests for hypogeum.public_ingest — no-auth public catalogue import."""
from __future__ import annotations

import pytest

from hypogeum.db import initialize_schema
from hypogeum import client_manager as cm
from hypogeum import public_ingest as pi


def test_channel_videos_url_forms():
    f = pi._channel_videos_url
    assert f("@acme") == "https://www.youtube.com/@acme/videos"
    assert f("acme") == "https://www.youtube.com/@acme/videos"
    assert f("UC1234567890abcdefghij") == "https://www.youtube.com/channel/UC1234567890abcdefghij/videos"
    assert f("https://www.youtube.com/@acme") == "https://www.youtube.com/@acme/videos"
    assert f("https://www.youtube.com/@acme/videos") == "https://www.youtube.com/@acme/videos"
    assert f("https://www.youtube.com/@acme/featured?x=1") == "https://www.youtube.com/@acme/videos"
    assert f("") is None


def _fake_fetch(url, *, limit=60, deep_limit=40):
    return {
        "ok": True, "platform": "youtube", "channel": "Acme TV",
        "channel_url": url, "error": None,
        "counts": {"listed": 2, "deep_ok": 2},
        "items": [
            {"platform_post_id": "V1", "url": "https://youtu.be/V1", "title": "Big hit",
             "thumbnail_url": "http://t/1.jpg", "published_at": "2026-07-01T10:00:00+00:00",
             "views": 90000, "likes": 4000, "comments": 300, "duration_s": 610},
            {"platform_post_id": "V2", "url": "https://youtu.be/V2", "title": "Quiet one",
             "thumbnail_url": None, "published_at": "2026-08-01T10:00:00+00:00",
             "views": 500, "likes": 10, "comments": 1, "duration_s": 240},
        ],
    }


@pytest.fixture()
def client(tmp_path):
    p = tmp_path / "adv.db"
    initialize_schema(p)
    c = cm.create_client("Acme", db_path=p)
    return p, c["client_id"]


def test_import_public_channel_persists_content_and_metrics(client):
    db, cid = client
    r = pi.import_public_channel(cid, "@acme", fetch_fn=_fake_fetch, db_path=db)
    assert r["ok"] and r["added"] == 2 and r["metric_rows"] == 2
    assert r["channel"] == "Acme TV"

    items = cm.list_content_items(cid, db_path=db)
    assert {i["platform_post_id"] for i in items} == {"V1", "V2"}
    assert all(i["source"] == "public" for i in items)

    from hypogeum.analytics_store import get_episode_analytics
    v1 = get_episode_analytics(cm.new_content_id("youtube", "V1"), db_path=db)["youtube"]
    assert v1["views"] == 90000 and v1["client_id"] == cid


def test_non_youtube_platform_rejected(client):
    db, cid = client
    r = pi.import_public_channel(cid, "acme", platform="tiktok",
                                fetch_fn=_fake_fetch, db_path=db)
    assert r["ok"] is False and "CSV" in r["error"]


def test_import_feeds_the_advisory(client):
    db, cid = client
    pi.import_public_channel(cid, "@acme", fetch_fn=_fake_fetch, db_path=db)
    from hypogeum.learning_engine import extract_insights
    v1 = cm.new_content_id("youtube", "V1")
    # source='public' still lands a platform_analytics row extract_insights can read
    insights = extract_insights(v1, db_path=db)
    assert isinstance(insights, list)
