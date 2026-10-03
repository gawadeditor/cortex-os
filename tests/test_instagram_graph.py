"""
tests/test_instagram_graph.py — direct Instagram (Instagram Login) source for
the Clients feature. Fake HTTP, tmp DB, no network.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from hypogeum import client_manager as cm
from hypogeum.credential_manager import get_secret_profile, resolve_credential
from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing import instagram_graph as ig
from hypogeum.publishing.client_content_sync import sync_client_content

IG_ID = "17841400000000001"


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "ig.db"
    initialize_schema(p)
    return p


class FakeGraph:
    def __init__(self, *, media=None, insights=None, fail_metrics=(), refresh=None):
        self.calls = []
        self.media = media if media is not None else [
            {"id": "m1", "caption": "First line\nmore", "media_type": "VIDEO",
             "media_product_type": "REELS", "permalink": "https://instagram.com/reel/m1",
             "timestamp": "2026-09-30T10:00:00+0000", "like_count": 10, "comments_count": 2,
             "thumbnail_url": "https://cdn/t1.jpg"},
            {"id": "m2", "caption": "Photo post", "media_type": "IMAGE",
             "media_product_type": "FEED", "permalink": "https://instagram.com/p/m2",
             "timestamp": "2026-09-20T10:00:00+0000", "like_count": 5, "comments_count": 1,
             "media_url": "https://cdn/i2.jpg"},
        ]
        self.insights = insights or {
            "m1": {"views": 1000, "reach": 800, "likes": 12, "comments": 3, "saved": 4, "shares": 2,
                   "total_interactions": 21, "ig_reels_avg_watch_time": 4500, "reels_skip_rate": 35},
            "m2": {"views": 300, "reach": 250, "likes": 6, "comments": 1, "saved": 1, "shares": 0,
                   "total_interactions": 8, "follows": 1},
        }
        self.fail_metrics = set(fail_metrics)
        self.refresh = refresh

    def __call__(self, url, timeout=30.0):
        self.calls.append(url)
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        assert q.get("access_token"), "every call must carry the token"
        if u.path.endswith("/refresh_access_token"):
            return self.refresh or ig.HttpResponse(200, {"access_token": "NEW", "expires_in": 5184000})
        if u.path.endswith("/me/media"):
            return ig.HttpResponse(200, {"data": self.media})
        if u.path.endswith("/me"):
            return ig.HttpResponse(200, {"user_id": IG_ID, "username": "me", "account_type": "CREATOR"})
        if u.path.endswith("/insights"):
            mid = u.path.split("/")[-2]
            metrics = q["metric"].split(",")
            if self.fail_metrics & set(metrics):
                return ig.HttpResponse(400, {"error": {"type": "OAuthException", "message": "bad metric"}})
            data = [{"name": k, "values": [{"value": v}]} for k, v in self.insights[mid].items() if k in metrics]
            return ig.HttpResponse(200, {"data": data})
        return ig.HttpResponse(404, {})


def _client(db_path, fake):
    return ig.InstagramGraphClient(IG_ID, db_path=db_path, http_get=fake)


def test_token_is_stored_in_credential_manager_and_used(db_path):
    sid = ig.store_account_token(IG_ID, "TOKEN1", username="me", db_path=db_path)
    assert sid == f"instagram_graph:{IG_ID}"
    assert resolve_credential(sid, db_path=db_path) == "TOKEN1"
    fake = FakeGraph()
    _client(db_path, fake).profile()
    assert "access_token=TOKEN1" in fake.calls[0]


def test_reels_and_feed_get_their_own_metric_sets(db_path):
    ig.store_account_token(IG_ID, "T", db_path=db_path)
    fake = FakeGraph()
    c = _client(db_path, fake)
    media = c.recent_media()
    reel = c.media_insights(media[0])
    feed = c.media_insights(media[1])
    assert reel["ig_reels_avg_watch_time"] == 4500 and "follows" not in reel
    assert feed["follows"] == 1 and "ig_reels_avg_watch_time" not in feed
    assert not any("impressions" in u for u in fake.calls)  # deprecated metric never asked


def test_insights_fall_back_to_minimal_set_when_meta_rejects_a_metric(db_path):
    ig.store_account_token(IG_ID, "T", db_path=db_path)
    fake = FakeGraph(fail_metrics={"follows"})
    out = _client(db_path, fake).media_insights(fake.media[1])
    assert out["views"] == 300 and "follows" not in out


def test_metric_mapping_to_cortex_keys():
    # engagement_rate is a PERCENT everywhere in platform_analytics (21/800 -> 2.62%)
    m = ig.to_cortex_metrics(
        {"views": 1000, "reach": 800, "saved": 4, "shares": 2, "total_interactions": 21,
         "ig_reels_avg_watch_time": 4500, "reels_skip_rate": 35}, {"like_count": 10, "comments_count": 2})
    assert m == {"views": 1000, "reach": 800, "likes": 10, "comments": 2, "saves": 4, "shares": 2,
                 "skip_rate": 35, "avg_watch_time_s": 4.5, "engagement_rate": 2.62}


def test_token_refresh_only_after_30_days(db_path):
    ig.store_account_token(IG_ID, "OLD", db_path=db_path)
    fake = FakeGraph()
    c = _client(db_path, fake)
    assert c.refresh_token_if_due() is False and fake.calls == []
    later = datetime.now(timezone.utc) + timedelta(days=31)
    assert c.refresh_token_if_due(now=later) is True
    assert resolve_credential(ig.secret_profile_id_for(IG_ID), db_path=db_path) == "NEW"


def test_failed_refresh_keeps_old_token(db_path):
    ig.store_account_token(IG_ID, "OLD", db_path=db_path)
    fake = FakeGraph(refresh=ig.HttpResponse(400, {"error": {"message": "expired"}}))
    later = datetime.now(timezone.utc) + timedelta(days=31)
    assert _client(db_path, fake).refresh_token_if_due(now=later) is False
    assert resolve_credential(ig.secret_profile_id_for(IG_ID), db_path=db_path) == "OLD"


def test_client_sync_pulls_instagram_without_zernio(db_path, monkeypatch):
    ig.store_account_token(IG_ID, "T", db_path=db_path)
    client = cm.create_client("Muhamed", client_id="me_test", db_path=db_path)
    cm.link_managed_account(client["client_id"], "instagram", handle="me", provider_id="instagram_graph",
                            provider_account_id=IG_ID, native_account_id=IG_ID, db_path=db_path)
    fake = FakeGraph()
    monkeypatch.setattr(ig, "_requests_get", fake)

    class NoZernio:  # must never be called for an instagram_graph account
        def __getattr__(self, name):
            raise AssertionError(f"Zernio called: {name}")

    out = sync_client_content("me_test", provider=NoZernio(), db_path=db_path)
    assert out["ok"] is True, out["errors"]
    assert out["content_items"] == 2 and out["metric_rows"] == 2
    conn = get_connection(db_path)
    try:
        items = {r["platform_post_id"]: dict(r) for r in conn.execute("SELECT * FROM content_items")}
        assert items["m1"]["source"] == "instagram_graph"
        assert items["m1"]["title"] == "First line" and items["m1"]["media_type"] == "reels"
        assert items["m2"]["media_type"] == "image"
        assert items["m2"]["thumbnail_url"] == "https://cdn/i2.jpg"
        rows = conn.execute("SELECT views, reach, client_id FROM platform_analytics "
                            "WHERE client_id='me_test' ORDER BY views DESC").fetchall()
        assert [r["views"] for r in rows] == [1000, 300]
    finally:
        conn.close()
    # idempotent second run: same content items, no duplicates
    out2 = sync_client_content("me_test", provider=NoZernio(), db_path=db_path)
    assert out2["content_items"] == 2
    conn = get_connection(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM content_items").fetchone()[0] == 2
    finally:
        conn.close()


def test_sync_reports_missing_token_without_crashing(db_path):
    cm.create_client("NoToken", client_id="nt", db_path=db_path)
    cm.link_managed_account("nt", "instagram", handle="x", provider_id="instagram_graph",
                            provider_account_id=IG_ID, native_account_id=IG_ID, db_path=db_path)
    out = sync_client_content("nt", provider=object(), db_path=db_path)
    assert out["ok"] is False
    assert any(k.startswith("instagram_graph:") for k in out["errors"])
    assert get_secret_profile(ig.secret_profile_id_for(IG_ID), db_path=db_path) is None


def test_pre_conversion_media_is_expected_and_stops_older_insight_calls(db_path, monkeypatch):
    ig.store_account_token(IG_ID, "T", db_path=db_path)
    cm.create_client("Old", client_id="old", db_path=db_path)
    cm.link_managed_account("old", "instagram", handle="me", provider_id="instagram_graph",
                            provider_account_id=IG_ID, native_account_id=IG_ID, db_path=db_path)

    class PreConv(FakeGraph):
        def __call__(self, url, timeout=30.0):
            if "/insights" in url:
                self.calls.append(url)
                # Meta localises the message — only code/subcode are reliable
                return ig.HttpResponse(400, {"error": {"type": "IGApiException", "code": 100,
                                                       "error_subcode": 2108006, "message": "رسالة"}})
            return super().__call__(url, timeout)

    fake = PreConv()
    monkeypatch.setattr(ig, "_requests_get", fake)
    out = sync_client_content("old", provider=object(), db_path=db_path)
    assert out["errors"] == {}                       # expected, not an error
    assert out["content_items"] == 2 and out["metric_rows"] == 2   # likes/comments still stored
    media_insight_calls = [u for u in fake.calls if "/insights" in u and "/me/insights" not in u]
    assert len(media_insight_calls) == 1                            # stopped after the first
    assert out["ig_pre_conversion_from"] == "2026-09-30T10:00:00+0000"


def test_content_analysis_skips_posts_before_client_start_date(db_path, monkeypatch):
    from hypogeum.publishing import client_content_sync as ccs
    ig.store_account_token(IG_ID, "T", db_path=db_path)
    cm.create_client("Start", client_id="st", managed_since="2026-09-25", db_path=db_path)
    cm.link_managed_account("st", "instagram", handle="me", provider_id="instagram_graph",
                            provider_account_id=IG_ID, native_account_id=IG_ID, db_path=db_path)
    monkeypatch.setattr(ig, "_requests_get", FakeGraph())
    analysed = []

    def fake_analyze(item, client=None, analyze_fn=None, db_path=None):
        analysed.append(item["platform_post_id"])
        return {"ok": True, "cost_usd": 0.01}

    import hypogeum.agents.content_analysis_agent as caa
    monkeypatch.setattr(caa, "analyze_content_item", fake_analyze)
    ccs.sync_client_content("st", provider=object(), analyze=True, db_path=db_path)
    assert analysed == ["m1"]          # m1 is 2026-09-30 (after start); m2 2026-09-20 is skipped


class DemoGraph(FakeGraph):
    def __call__(self, url, timeout=30.0):
        if "/me/insights" in url and "follower_demographics" in url:
            self.calls.append(url)
            dim = parse_qs(urlparse(url).query)["breakdown"][0]
            res = {"age": [("35-44", 60), ("25-34", 40)], "gender": [("M", 75), ("F", 25)],
                   "country": [("EG", 50), ("AE", 50)], "city": [("Dubai, Dubai", 10)]}[dim]
            return ig.HttpResponse(200, {"data": [{"name": "follower_demographics", "total_value": {
                "breakdowns": [{"results": [{"dimension_values": [k], "value": v} for k, v in res]}]}}]})
        return super().__call__(url, timeout)


def test_follower_demographics_normalised(db_path):
    ig.store_account_token(IG_ID, "T", db_path=db_path)
    d = _client(db_path, DemoGraph()).follower_demographics()
    assert d["age"][0] == {"label": "35-44", "value": 60.0, "pct": 60.0}
    assert [r["label"] for r in d["gender"]] == ["Men", "Women"]
    assert {r["label"] for r in d["country"]} == {"Egypt", "UAE"}
    assert d["city"][0]["label"] == "Dubai"


def test_sync_stores_instagram_audience_once_a_day(db_path, monkeypatch):
    from hypogeum.publishing.zernio_native_sync import get_channel_audience
    ig.store_account_token(IG_ID, "T", db_path=db_path)
    cm.create_client("Aud", client_id="aud", db_path=db_path)
    cm.link_managed_account("aud", "instagram", handle="me", provider_id="instagram_graph",
                            provider_account_id=IG_ID, native_account_id=IG_ID, db_path=db_path)
    fake = DemoGraph()
    monkeypatch.setattr(ig, "_requests_get", fake)
    out = sync_client_content("aud", provider=object(), db_path=db_path)
    assert out["audience_rows"] == 4
    aud = get_channel_audience("aud", db_path=db_path)["instagram"]
    assert aud["country"][0]["label"] in ("Egypt", "UAE") and aud["gender"][0]["label"] == "Men"
    n = sum("follower_demographics" in u for u in fake.calls)
    sync_client_content("aud", provider=object(), db_path=db_path)   # same day again
    assert sum("follower_demographics" in u for u in fake.calls) == n
