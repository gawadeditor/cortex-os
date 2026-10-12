from unittest.mock import MagicMock

from hypogeum import platform_learning as pl
from hypogeum import platform_timeline_sync as pts
from hypogeum.db import get_connection, initialize_schema


def _rows(plat, posts, final, days=4, ep="ep_x", day0=0.5):
    out = []
    for i in range(posts):
        for d in range(days):
            v = int(final * (day0 if d == 0 else 1.0)) if d < 2 else final
            out.append({"post_id": f"{plat}{i}", "platform": plat, "date": f"2026-10-1{d}", "episode_id": f"{ep}{i}", "views": v,
                        "reach": v, "likes": 2, "comments": 0, "shares": 1, "saves": 0})
    return out


def test_platform_stats_share_engagement_and_day0():
    st = pl.platform_stats(_rows("facebook", 6, 400) + _rows("tiktok", 6, 40))
    assert st["facebook"]["posts"] == 6 and st["facebook"]["avg_views"] == 400
    assert round(st["facebook"]["share"], 2) == round(2400 / 2640, 2)
    assert st["facebook"]["day0_share"] == 0.5 and st["tiktok"]["engagement_rate"] == round(18 / 240, 4)


def test_directive_needs_two_platforms_with_enough_posts():
    assert pl.directive_text(pl.platform_stats(_rows("facebook", 6, 400))) is None
    assert pl.directive_text(pl.platform_stats(_rows("facebook", 6, 400) + _rows("tiktok", 3, 40))) is None   # tiktok < 5 posts
    t = pl.directive_text(pl.platform_stats(_rows("facebook", 6, 400) + _rows("tiktok", 6, 40)))
    assert t and "Facebook is where this channel's audience actually is" in t and "10.0x" in t


def test_pattern_by_platform_is_evidence_gated():
    rows = _rows("facebook", 6, 400)
    traits = {f"ep_x{i}": ("a" if i < 3 else "b") for i in range(6)}
    assert pl.pattern_by_platform(rows, traits)["facebook"]["a"]["n"] == 3
    assert pl.pattern_by_platform(rows, {f"ep_x{i}": "a" for i in range(2)}).get("facebook", {}) == {}


def test_timeline_sync_stores_rows_and_the_facebook_curve_and_skips_fresh_posts(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    initialize_schema(db)
    monkeypatch.setattr("hypogeum.dashboard_content_grid._resolve_posts",
                        lambda conn, ch: [{"post_id": "P1", "episode_id": "ep_1", "deliverable_id": "main_episode"}])
    tl = [{"date": "2026-10-10", "platform": "facebook", "views": 238, "reach": 208, "likes": 0, "retentionCurve": {str(i): 1.0 - i * 0.02 for i in range(30)}},
          {"date": "2026-10-11", "platform": "facebook", "views": 239, "reach": 209},
          {"date": "2026-10-10", "platform": "tiktok", "views": 72, "likes": 1}]
    prov = MagicMock()
    prov._get_analytics_resource.return_value = {"data": {"timeline": tl}}
    out = pts.sync_channel_timelines("ch", provider=prov, db_path=db)
    assert out["posts"] == 1 and out["rows"] == 3 and out["curves"] == 1
    conn = get_connection(db)
    assert conn.execute("SELECT COUNT(*) FROM post_timeline").fetchone()[0] == 3
    assert conn.execute("SELECT seconds FROM post_retention WHERE platform='facebook'").fetchone()[0] == 30
    conn.close()
    again = pts.sync_channel_timelines("ch", provider=prov, db_path=db)          # read < 20h ago: not fetched again
    assert again["posts"] == 0 and prov._get_analytics_resource.call_count == 1


def test_timeline_sync_holds_no_connection_during_http(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "t.db"
    initialize_schema(db)
    monkeypatch.setattr("hypogeum.dashboard_content_grid._resolve_posts",
                        lambda conn, ch: [{"post_id": "P1", "episode_id": "ep_1", "deliverable_id": "main_episode"}])

    def _fetch(*a, **k):
        o = sqlite3.connect(str(db), timeout=0.2)
        try:
            o.execute("CREATE TABLE IF NOT EXISTS _p (x)")
            o.execute("INSERT INTO _p VALUES (1)")
            o.commit()
        finally:
            o.close()
        return {"data": {"timeline": []}}

    prov = MagicMock()
    prov._get_analytics_resource.side_effect = _fetch
    out = pts.sync_channel_timelines("ch", provider=prov, db_path=db)
    assert out["error"] is None
