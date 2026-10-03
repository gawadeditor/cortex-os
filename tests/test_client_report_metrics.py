"""
tests/test_client_report_metrics.py — the client report must reflect posts
that have real numbers even without an engagement RATE (old Instagram posts
have only likes/comments; LinkedIn reports impressions, not views), never
compare raw counts across platforms, and only show payout progress for
platforms the client is actually on. tmp DB only.
"""
from __future__ import annotations

import pytest

from hypogeum import client_manager as cm
from hypogeum.analytics_store import store_content_metrics
from hypogeum.client_advisory import generate_client_advisory
from hypogeum.dashboard_clients import get_client_overview
from hypogeum.db import initialize_schema


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "r.db"
    initialize_schema(p)
    cm.create_client("Me", client_id="me", db_path=p)
    cm.link_managed_account("me", "instagram", handle="me", provider_id="instagram_graph",
                            provider_account_id="ig1", native_account_id="ig1", db_path=p)
    cm.link_managed_account("me", "linkedin", handle="me", provider_id="linkedin_export",
                            provider_account_id="me", native_account_id="me", db_path=p)
    return p


def _post(db, platform, pid, metrics):
    cid = cm.upsert_content_item("me", platform, platform_post_id=pid,
                                 published_at="2021-05-01T10:00:00+00:00",
                                 source="instagram_graph" if platform == "instagram" else "linkedin_export",
                                 db_path=db)
    store_content_metrics(cid, "me", platform, metrics, source="api", db_path=db)


def test_interaction_only_posts_count_and_rank_within_one_platform(db):
    for i, likes in enumerate((90, 60, 40, 10, 5, 2)):
        _post(db, "instagram", f"ig{i}", {"likes": likes, "comments": 1})
    _post(db, "linkedin", "share:1", {"impressions": 1000, "engagement_rate": 2.0})
    adv = generate_client_advisory("me", db_path=db)
    cat = adv["catalogue"]
    assert cat["with_metrics"] == 7 and cat["total"] == 7
    assert cat["basis"] == {"metric": "interactions", "unit": "", "platform": "instagram"}
    assert cat["scored"] == 6                       # LinkedIn's post is NOT mixed into the ranking
    assert "on Instagram" in adv["summary"] and "interactions" in adv["summary"]
    assert "no reach data" in adv["summary"]        # basis is disclosed


def test_engagement_rate_basis_unchanged_when_enough_rates(db):
    for i, er in enumerate((5.0, 4.0, 3.0, 2.0, 1.0)):
        _post(db, "instagram", f"ig{i}", {"likes": 10, "reach": 100, "engagement_rate": er})
    adv = generate_client_advisory("me", db_path=db)
    assert adv["catalogue"]["basis"]["metric"] == "engagement"
    assert "% engagement" in adv["summary"]


def test_overview_kpis_show_impressions_and_interactions(db):
    _post(db, "instagram", "ig1", {"likes": 30, "comments": 4})
    _post(db, "linkedin", "share:1", {"impressions": 857, "engagement_rate": 6.65})
    k = get_client_overview("me", db_path=db)["kpis"]
    assert k["total_views"] == 0
    assert k["total_impressions"] == 857 and k["interactions"] == 34
    assert k["with_metrics"] == 2 and k["engagement_basis"] == 1


def test_monetization_only_for_platforms_the_client_is_on(db):
    _post(db, "instagram", "ig1", {"likes": 30})
    mon = generate_client_advisory("me", db_path=db).get("monetization") or {}
    plats = set((mon.get("platforms") or {}).keys())
    assert plats <= {"instagram", "linkedin"} and "youtube" not in plats and "tiktok" not in plats


def test_plan_page_renders_and_updates_posts(db, tmp_path):
    import run_dashboard as rd
    from hypogeum.dashboard_clients import get_client_plan
    c = cm.get_client("me", db_path=db)
    cm.update_client("me", db_path=db, brief={
        "brand_profile": {"goal": "عملاء أكتر", "awards": [{"name": "Gold", "year": 2026, "project": "P", "role": "Editor"}]},
        "brand_strategy": {"positioning": "محرر وثائقي", "pillars": [{"name": "قبل وبعد", "why": "دليل"}],
                           "platforms": {"linkedin": {"role": "أساسي", "language": "عربي"}}},
        "content_calendar": {"month": "2026-10", "posts": [
            {"date": "2026-10-06", "day": "الثلاثاء", "platforms": ["linkedin"], "lang": "en",
             "pillar": "x", "format": "text", "needs": None, "draft": "Hello"}]}})
    old = rd.app.config.get("DB_PATH")
    rd.app.config["DB_PATH"] = str(db)
    try:
        cl = rd.app.test_client()
        page = cl.get("/client/me/plan?scope=client:me").get_data(as_text=True)
        assert "محرر وثائقي" in page and "Hello" in page and "Gold" in page
        r = cl.post("/client/me/plan/post", data={"date": "2026-10-06", "draft": "Hello edited", "action": "published"})
        assert r.status_code == 302
        p = get_client_plan("me", db_path=db)["posts"][0]
        assert p["draft"] == "Hello edited" and p["status"] == "published"
        assert get_client_plan("me", db_path=db)["counts"]["published"] == 1
        assert cl.get("/client/nope/plan").status_code == 404
    finally:
        rd.app.config["DB_PATH"] = old


def test_platform_advice_is_per_platform_and_sample_sized(db):
    from hypogeum.client_advisory import generate_platform_advice
    # Instagram: 3 carousels clearly beat 3 single images; all old -> dormant
    for i, (mt, likes) in enumerate([("carousel_album", 60), ("carousel_album", 50), ("carousel_album", 55),
                                     ("image", 10), ("image", 12), ("image", 8)]):
        cid = cm.upsert_content_item("me", "instagram", platform_post_id=f"ig{i}", media_type=mt,
                                     caption="Some caption", published_at="2016-05-01T10:00:00+00:00",
                                     source="instagram_graph", db_path=db)
        store_content_metrics(cid, "me", "instagram", {"likes": likes}, source="api", db_path=db)
    _post(db, "linkedin", "share:1", {"impressions": 900, "engagement_rate": 6.0})
    adv = generate_platform_advice("me", db_path=db)
    assert set(adv) == {"instagram", "linkedin"}
    ig = adv["instagram"]
    axes = {f["axis"]: f for f in ig["findings"]}
    assert "activity" in axes and "cadence" not in axes           # dormancy not repeated as cadence
    assert axes["format"]["best"] == "Carousel" and "3 vs 3 posts" in axes["format"]["evidence"]
    assert any("carousel" in a for a in ig["actions"])
    # a lone LinkedIn post never produces a format/language comparison
    assert not any(f["axis"] in ("format", "language") for f in adv["linkedin"]["findings"])


def test_advice_page_shows_platform_cards(db):
    import run_dashboard as rd
    _post(db, "linkedin", "share:1", {"impressions": 900, "engagement_rate": 6.0})
    old = rd.app.config.get("DB_PATH")
    rd.app.config["DB_PATH"] = str(db)
    try:
        page = rd.app.test_client().get("/client/me/advice?scope=client:me").get_data(as_text=True)
        assert "What to do on LinkedIn" in page and "What to do on Instagram" in page
        assert "Not enough to advise on yet" not in page
    finally:
        rd.app.config["DB_PATH"] = old
