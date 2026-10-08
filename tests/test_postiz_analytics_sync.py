"""Per-post analytics for Postiz-published posts -> platform_analytics."""
from __future__ import annotations

from datetime import datetime, timezone

from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing import postiz_analytics_sync as pas


def _series(label, *totals):
    return {"label": label, "data": [{"total": t, "date": f"2026-10-0{i + 1}"} for i, t in enumerate(totals)]}


def test_translate_sums_daily_totals_and_keeps_unknown_labels_raw():
    out = pas.translate_postiz_series(
        [_series("Views", 10, 5), _series("Likes", 2), _series("Mystery", 9),
         _series("Estimated Minutes Watched", 3), _series("Reach", 40)],
        platform="youtube")
    assert out["views"] == 15 and out["likes"] == 2
    assert out["total_watch_time_s"] == 180.0
    assert "reach" not in out  # reach is a Facebook/Instagram metric only
    assert out["raw_json"]["Mystery"] == 9 and "mystery" not in out


def test_translate_tolerates_junk():
    assert pas.translate_postiz_series(None, platform="youtube") == {"raw_json": {}}
    assert pas.translate_postiz_series([{"label": "Views", "data": "x"}, "z"], platform="youtube") == {"raw_json": {}}


def _seed(db, post_id, episode_id, platform, channel="ch"):
    initialize_schema(db)
    conn = get_connection(db)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT OR IGNORE INTO episodes (episode_id, title, created_at, status) VALUES (?,?,?,'complete')",
                 (episode_id, "T", now))
    conn.execute(
        """INSERT INTO publication_intents (publication_intent_id, channel_id, episode_id, deliverable_id,
           platform, status, created_at, updated_at) VALUES (?,?,?,?,?,'succeeded',?,?)""",
        (f"i_{post_id}", channel, episode_id, "short_001", platform, now, now))
    conn.execute(
        """INSERT INTO publishing_attempts (attempt_id, publication_intent_id, channel_id, episode_id,
           deliverable_id, platform, provider_id, state, created_at, updated_at)
           VALUES (?,?,?,?,?,?,'postiz','succeeded',?,?)""",
        (f"a_{post_id}", f"i_{post_id}", channel, episode_id, "short_001", platform, now, now))
    conn.execute(
        """INSERT INTO publishing_receipts (receipt_id, attempt_id, provider_id, platform,
           provider_publication_id, acknowledged_at) VALUES (?,?,'postiz',?,?,?)""",
        (f"r_{post_id}", f"a_{post_id}", platform, post_id, now))
    conn.commit()
    conn.close()


class _Prov:
    def __init__(self, by_post, fail=None):
        self.by_post, self.fail, self.calls = by_post, fail, []

    def get_post_analytics(self, post_id, *, days=30, db_path=None):
        self.calls.append(post_id)
        if self.fail:
            raise RuntimeError(self.fail)
        return self.by_post[post_id]


def _rows(db):
    conn = get_connection(db)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT episode_id, platform, views, likes, source FROM platform_analytics")]
    finally:
        conn.close()


def test_sync_writes_an_api_row_per_episode_platform(tmp_path):
    db = tmp_path / "a.db"
    _seed(db, "pzA", "ep_1", "youtube")
    prov = _Prov({"pzA": [_series("Views", 100), _series("Likes", 7)]})
    out = pas.sync_postiz_analytics(provider=prov, db_path=db)
    assert out["ok"] and out["rows_written"] == 1
    assert _rows(db) == [{"episode_id": "ep_1", "platform": "youtube", "views": 100, "likes": 7, "source": "api"}]


def test_no_postiz_posts_is_a_quiet_noop(tmp_path):
    db = tmp_path / "a.db"
    initialize_schema(db)
    prov = _Prov({})
    out = pas.sync_postiz_analytics(provider=prov, db_path=db)
    assert out["ok"] and prov.calls == []


def test_rate_limit_stops_the_run_without_raising(tmp_path):
    db = tmp_path / "a.db"
    _seed(db, "pzA", "ep_1", "youtube")
    _seed(db, "pzB", "ep_2", "youtube")
    prov = _Prov({}, fail="GET analytics HTTP 429")
    out = pas.sync_postiz_analytics(provider=prov, db_path=db)
    assert out["ok"] is False and "429" in out["error"] and len(prov.calls) == 1


def test_max_posts_caps_api_calls(tmp_path):
    db = tmp_path / "a.db"
    for i in range(5):
        _seed(db, f"pz{i}", f"ep_{i}", "youtube")
    prov = _Prov({f"pz{i}": [_series("Views", 1)] for i in range(5)})
    pas.sync_postiz_analytics(provider=prov, db_path=db, max_posts=2)
    assert len(prov.calls) == 2
