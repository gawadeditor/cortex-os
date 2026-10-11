from unittest.mock import MagicMock, patch

from hypogeum import youtube_traffic as yt
from hypogeum.db import get_connection, initialize_schema


def _rows(terms, search_views=60, feed_views=900):
    r = [
        {"dimension": "traffic_type", "key": "SHORTS", "subkey": "", "views": feed_views, "minutes_watched": 1, "avg_view_duration_s": 20, "avg_view_pct": 70.0},
        {"dimension": "traffic_type", "key": "YT_SEARCH", "subkey": "", "views": search_views, "minutes_watched": 1, "avg_view_duration_s": 30, "avg_view_pct": 96.0},
    ]
    for t, v, p in terms:
        r.append({"dimension": "search_term", "key": t, "subkey": "", "views": v, "minutes_watched": 0, "avg_view_duration_s": 10, "avg_view_pct": p})
    return r


def test_findings_mix_gap_and_unbranded_terms():
    f = yt.derive_findings(_rows([("valley of the kings", 5, 120.0), ("hypogeum malta", 3, 50.0), ("zero watched", 4, 0.0)]), "hypogeum")
    assert f["search_share"] == round(60 / 960, 3)
    assert f["retention_gap"] == 26.0
    assert [t["key"] for t in f["search_terms"]] == ["valley of the kings"]       # brand + 0%-watched terms excluded
    assert [t["key"] for t in f["brand_terms"]] == ["hypogeum malta"]


def test_directive_needs_real_evidence():
    weak = yt.derive_findings(_rows([("a", 2, 50.0), ("b", 2, 60.0)]), "ch")           # < MIN_TERMS
    assert yt.directive_text(weak) is None
    strong = yt.derive_findings(_rows([("valley of the kings", 5, 120.0), ("voynich manuscript", 4, 90.0), ("herculaneum", 3, 70.0)]), "ch")
    txt = yt.directive_text(strong)
    assert txt and '"valley of the kings" (5 views, 100% watched)' in txt and "measured queries" in txt   # pct capped at 100


def test_parse_row():
    r = yt._row("search_term", ["valley of the kings", 5, 3, 67, 160.9])
    assert r["key"] == "valley of the kings" and r["views"] == 5 and r["avg_view_pct"] == 160.9


def test_learn_channel_traffic_stores_writes_directive_and_is_daily(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    rows = _rows([("valley of the kings", 5, 120.0), ("voynich manuscript", 4, 90.0), ("herculaneum", 3, 70.0)])
    with patch.object(yt, "fetch_channel_traffic", return_value=rows) as fetch:
        out = yt.learn_channel_traffic("ch", db_path=db)
        assert out["fetched"] and out["directive"] == "written"
        again = yt.learn_channel_traffic("ch", db_path=db)            # same day: no second fetch, no duplicate directive
        assert not again["fetched"] and again["directive"] is None
        assert fetch.call_count == 1
    from hypogeum.channel_learning import get_active_directives
    assert len(get_active_directives("ch", dimension="search_demand", db_path=db)) == 1
    assert yt.search_terms_for_metadata("ch", db_path=db)[0]["term"] == "valley of the kings"


def test_no_authorization_is_a_clean_noop(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    with patch.object(yt, "fetch_channel_traffic", return_value=None):
        out = yt.learn_channel_traffic("ch", db_path=db)
    assert out["fetched"] is False and out["error"] and out["directive"] is None


def test_report_retries_transient_5xx_then_succeeds():
    bad, good = MagicMock(status_code=500, text="x"), MagicMock(status_code=200)
    good.json.return_value = {"rows": [["a", 1, 0, 0, 0]]}
    with patch.object(yt.requests, "get", side_effect=[bad, good]), patch.object(yt.time, "sleep"):
        assert yt._report("tok", {}) == [["a", 1, 0, 0, 0]]
