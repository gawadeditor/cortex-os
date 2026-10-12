from unittest.mock import patch

from hypogeum import market_intel as mi
from hypogeum.db import get_connection, initialize_schema


def _v(ch, i, title, views, short=1, hashtags=("#history", "#shorts"), tags=("history",)):
    return {"video_ref": f"{ch}{i}", "channel_ref": ch, "title": title, "published_at": f"2026-10-{i + 1:02d}T00:00:00Z", "views": views, "likes": 1,
            "comments": 0, "duration_s": 40 if short else 600, "is_short": short, "tags": list(tags), "hashtags": list(hashtags),
            "description_head": "A story. #history #shorts https://x.y", "language": "en"}


def _ch(ref, seed="history facts", subs=1000, views=2_000_000):
    return {"channel_ref": ref, "title": ref.upper(), "created_at": "2026-03-01T00:00:00Z", "country": "US", "subscribers": subs,
            "total_views": views, "video_count": 10, "keywords": "", "topic_categories": [], "niche_seed": seed}


def test_iso_duration_and_language_filter():
    assert mi._iso_duration_s("PT1M5S") == 65 and mi._iso_duration_s("PT2H") == 7200 and mi._iso_duration_s("bad") == 0
    assert mi._is_english("Why the Roman Empire fell in 5 minutes") is True
    assert mi._is_english("गाँव की कहानी") is False
    assert mi._is_english("anything", "en-US") is True and mi._is_english("The story of the king", "hi") is False


def test_analyse_builds_niche_conventions_and_suggestion():
    chs = [_ch("a"), _ch("b"), _ch("c")]
    vids = []
    for ch in ("a", "b", "c"):
        for i, t in enumerate(["Why Rome Fell in 5 Minutes?", "The Secret History of Egypt", "3 Facts About Greece History"]):
            vids.append(_v(ch, i, t, 10_000 * (i + 1)))
    rep = mi.analyse(chs, vids)
    n = rep["niches"][0]
    assert n["niche"] == "history facts" and n["channels"] == 3 and n["videos_read"] == 9
    assert n["title"]["number_pct"] == 67 and n["title"]["question_pct"] == 33 and n["title"]["power_word_pct"] == 33
    assert n["hashtags"][0][0] in ("#history", "#shorts") and n["shorts_share"] == 1.0
    assert rep["suggestions"] and rep["suggestions"][0]["niche"] == "history facts"
    assert any("Hashtags:" in h for h in rep["suggestions"][0]["how"])


def test_channels_surfaced_by_an_unrelated_seed_are_dropped():
    chs = [_ch("a", seed="unsolved mysteries")]
    vids = [_v("a", i, "One Piece Luffy edit", 5000, hashtags=("#anime",), tags=("anime",)) for i in range(5)]
    assert mi.analyse(chs, vids)["niches"] == []


def test_fetch_filters_to_new_channels_with_millions_and_stops_cleanly_on_budget():
    class FakeApi:
        units = 0
        max_units = 99999

        def get(self, path, params, cost=1):
            self.units += cost
            if path == "search":
                return {"items": [{"id": {"videoId": "v1"}}]}
            if path == "videos" and params.get("id") == "v1":
                return {"items": [{"id": "v1", "snippet": {"channelId": "UCnew1", "title": "x"}}]}
            if path == "channels":
                return {"items": [{"id": "UCnew1", "snippet": {"publishedAt": mi._now().isoformat()}, "statistics": {"viewCount": "5000000", "subscriberCount": "100"}}]}
            if path == "playlistItems":
                return {"items": [{"contentDetails": {"videoId": "u1"}}]}
            return {"items": [{"id": "u1", "snippet": {"channelId": "UCnew1", "title": "t", "publishedAt": "2026-10-01T00:00:00Z"},
                               "statistics": {"viewCount": "10"}, "contentDetails": {"duration": "PT30S"}}]}

    fx = mi.fetch(FakeApi(), ["history facts"])
    assert list(fx["channels"]) == ["UCnew1"] and "u1" in fx["videos"] and fx["stopped"] is None
    api = mi._Api("tok", 150)                                    # budget for one search only
    with patch.object(mi.requests, "get") as g:
        g.return_value.status_code = 200
        g.return_value.json.return_value = {"items": []}
        fx2 = mi.fetch(api, ["a", "b", "c"])
    assert fx2["stopped"].startswith("search budget reached")


def test_run_scan_stores_and_latest_report_roundtrips(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    fx = {"channels": {"UC1": {"snippet": {"title": "Chan", "publishedAt": "2026-03-01T00:00:00Z"}, "statistics": {"viewCount": "3000000", "subscriberCount": "500"}}},
          "videos": {}, "seed_by_channel": {"UC1": "history facts"}, "stopped": None, "units": 123}
    for i in range(3):
        fx["videos"][f"v{i}"] = {"id": f"v{i}", "snippet": {"channelId": "UC1", "title": f"Why history repeats {i}?", "publishedAt": f"2026-10-0{i + 1}T00:00:00Z",
                                                          "description": "x #history", "tags": ["history"], "defaultAudioLanguage": "en"},
                                 "statistics": {"viewCount": "1000"}, "contentDetails": {"duration": "PT30S"}}
    with patch.object(mi, "fetch", return_value=fx):
        out = mi.run_scan(db, token="tok", seeds=["history facts"])
    assert out["ok"] and out["channels"] == 1 and out["units"] == 123
    rep = mi.latest_report(db)
    assert rep["channels_total"] == 1 and rep["niches"][0]["niche"] == "history facts"
    conn = get_connection(db)
    assert conn.execute("SELECT COUNT(*) FROM market_videos").fetchone()[0] == 3
    conn.close()


def test_scan_holds_no_database_connection_during_http(tmp_path):
    import sqlite3
    db = tmp_path / "t.db"
    initialize_schema(db)

    def _fetch(api, seeds):
        o = sqlite3.connect(str(db), timeout=0.2)
        try:
            o.execute("CREATE TABLE IF NOT EXISTS _p (x)")
            o.execute("INSERT INTO _p VALUES (1)")
            o.commit()
        finally:
            o.close()
        return {"channels": {}, "videos": {}, "seed_by_channel": {}, "stopped": None, "units": 0}

    with patch.object(mi, "fetch", side_effect=_fetch):
        out = mi.run_scan(db, token="tok", seeds=["x"])
    assert out["error"] is None


def test_market_directive_for_a_matching_niche(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    initialize_schema(db)
    n = {"niche": "ancient mysteries", "channels": 5, "title": {"avg_len": 70, "number_pct": 33, "question_pct": 19, "power_word_pct": 73,
                                                               "top_openers": [("the", 5)]},
         "description": {"avg_len": 480, "with_hashtags_pct": 83, "avg_hashtags": 6.3}, "hashtags": [("#history", 9)], "uploads_per_week": 6.0, "shorts_share": 0.6}
    monkeypatch.setattr(mi, "latest_report", lambda db_path=None: {"niches": [n]})
    monkeypatch.setattr("hypogeum.channel_creative_brief.channel_brief_for", lambda c: {"genre": "Ancient mysteries and archaeology", "content_pillars": []})
    out = mi.learn_channel_market("ch", db_path=db)
    assert out["niche"] == "ancient mysteries" and out["directive"] == "written"
    assert mi.learn_channel_market("ch", db_path=db)["directive"] is None          # unchanged: no duplicate
