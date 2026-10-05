"""hypogeum.episode_compare — same-age comparison against the channel's
earlier episodes, and the episode page's Analytics tab showing it."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypogeum import episode_compare as ec
from hypogeum.db import get_connection, initialize_schema


def _seed(db, ep, published, views_by_day):
    conn = get_connection(db)
    conn.execute("INSERT INTO content_grid_cache (post_id, channel_id, episode_id, deliverable_id, title, status, "
                 "published_at, platforms_json, cached_at) VALUES (?, 'ch', ?, 'main_episode', ?, 'published', ?, '[]', ?)",
                 (f"p_{ep}", ep, ep, published.isoformat(), published.isoformat()))
    for d, v in views_by_day.items():
        conn.execute("INSERT INTO platform_analytics (record_id, episode_id, platform, snapshot_date, views, "
                     "source, imported_at) VALUES (?, ?, 'youtube', ?, ?, 'api', ?)",
                     (f"{ep}_{d}", ep, (published + timedelta(days=d)).date().isoformat(), v, published.isoformat()))
    conn.commit()
    conn.close()


def test_compares_at_the_same_age(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    now = datetime.now(timezone.utc)
    _seed(db, "old", now - timedelta(days=30), {1: 100, 3: 300, 7: 700, 25: 5000})
    _seed(db, "new", now - timedelta(days=4), {1: 400, 3: 900})
    r = ec.compare("new", db_path=db)
    assert set(r["ages"]) == {1, 3}                      # day 7 not reached yet
    assert r["ages"][1]["views"] == 400 and r["ages"][1]["peer_median_views"] == 100
    assert r["ages"][1]["rank"] == "1 of 2"
    assert ec.compare("missing", db_path=db).get("error")
