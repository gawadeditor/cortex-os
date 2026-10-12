import json
from unittest.mock import patch

from hypogeum import channel_plan as cpn
from hypogeum import market_intel as mi
from hypogeum.db import initialize_schema


def _niche(name, channels, med, shorts=0.9, upw=7.0, how=None):
    return {"niche": name, "channels": channels, "videos_read": 100, "median_channel_views": 5_000_000, "median_subscribers": 50_000,
            "median_views_per_video": med, "median_age_days": 120, "shorts_share": shorts, "uploads_per_week": upw,
            "title": {"avg_len": 60, "avg_words": 9.0, "number_pct": 40, "question_pct": 30, "emoji_pct": 5, "caps_word_pct": 5, "power_word_pct": 30,
                      "hashtag_in_title_pct": 0, "top_openers": [("why", 5), ("the", 4)], "top_terms": ["rome", "empire", "fall"]},
            "description": {"avg_len": 400, "with_hashtags_pct": 90, "avg_hashtags": 8.0, "with_link_pct": 3},
            "hashtags": [("#history", 20), ("#shorts", 15)], "tags": [("history", 9)], "tags_per_video": 8.0,
            "examples": [{"title": "Why Rome Fell", "views": 900_000, "channel": "X", "video": "v1"}],
            "channel_examples": [{"title": "Chan", "ref": "UC1", "created_at": "2026-03-01", "subscribers": 1000, "total_views": 3_000_000,
                                  "views_per_sub": 3000.0, "shorts_share": 0.9}],
            "how": how or {"best_hours_utc": [13, 14], "best_days": ["Fri"], "median_length_s": 40, "under_60s_pct": 90, "cta_pct": 20,
                           "title_templates": [{"pattern": "Why N …", "count": 3, "example": "Why Rome Fell"}], "views_per_video_top25_pct": 120_000}}


def test_questionnaire_validation_and_seeds():
    ok = {"goal": "income", "families": ["history", "finance"], "language": "en", "audience": "adults", "format": "shorts",
          "effort": "steady", "risk": "balanced", "money": "ads"}
    assert cpn.validate(ok) is None
    assert cpn.validate({**ok, "goal": "nope"}) and cpn.validate({**ok, "families": ["x"]})
    seeds = cpn.seeds_for(ok)
    assert "ancient mysteries" in seeds and "personal finance tips" in seeds and "bedtime story for kids" not in seeds
    kids = cpn.seeds_for({**ok, "families": [], "audience": "adults"})
    assert not any(s in cpn.FAMILIES["kids"]["seeds"] for s in kids)           # kids niches excluded unless the audience is kids


def test_build_plan_ranks_by_goal_risk_and_format_and_builds_a_blueprint():
    report = {"niches": [_niche("history facts", 9, 40_000), _niche("bedtime story for kids", 4, 90_000), _niche("personal finance tips", 7, 20_000, shorts=1.0)],
              "channels_total": 20, "videos_read": 1000}
    ans = {"goal": "income", "families": ["finance"], "language": "en", "audience": "adults", "format": "shorts", "effort": "steady", "risk": "proven", "money": "ads"}
    plan = cpn.build_plan(ans, report, cost_per_episode=15.0)
    assert plan["options"][0]["niche"] == "personal finance tips"           # chosen family + high-CPM + proven
    o = plan["options"][0]
    bp = o["blueprint"]
    assert bp["uploads_per_week"] == 7 and bp["cost"]["per_month_usd"] == round(7 * 4.3 * 15.0)
    assert bp["post_at_utc"] == ["13:00", "14:00"] and bp["post_at_uae"] == ["17:00", "18:00"]
    assert bp["monetisation"]["projected_views_90d"] == int(7 * 13 * 20_000) and "verdict" in bp["monetisation"]
    pf = o["prefill"]
    assert pf["genre"] == "Personal Finance Tips" and pf["format"] == "shorts" and pf["shorts_per_week"] == 7 and pf["brand"]["content_pillars"]
    assert any("high CPM" in w for w in o["why"])
    kids = cpn.build_plan({**ans, "families": ["kids"], "audience": "kids", "goal": "specific"}, report)["options"][0]
    assert kids["niche"] == "bedtime story for kids" and any("made-for-kids" in w for w in kids["why"])


def test_empty_research_gives_an_honest_empty_plan():
    plan = cpn.build_plan({"goal": "income"}, {"niches": [], "channels_total": 0, "videos_read": 0})
    assert plan["options"] == [] and "did not find enough" in plan["caveats"][0]


def test_start_plan_runs_stores_and_reports_progress(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    ans = {"goal": "income", "families": ["history"], "language": "en", "audience": "adults", "format": "shorts", "effort": "steady",
           "risk": "balanced", "money": "ads"}
    assert cpn.start_plan({"goal": "income"}, db)["ok"] is False
    channels = [{"channel_ref": f"UC{i}", "title": f"C{i}", "created_at": "2026-03-01T00:00:00Z", "country": "US", "subscribers": 1000, "total_views": 3_000_000,
                 "video_count": 10, "keywords": "", "topic_categories": [], "niche_seed": "history facts"} for i in range(3)]
    vids = [{"video_ref": f"v{i}{j}", "channel_ref": f"UC{i}", "title": f"Why Rome fell {j}?", "published_at": f"2026-10-0{j + 1}T13:00:00Z", "views": 10_000 * (j + 1),
             "likes": 1, "comments": 0, "duration_s": 40, "is_short": 1, "tags": ["history"], "hashtags": ["#history", "#shorts"],
             "description_head": "x #history #shorts", "language": "en"} for i in range(3) for j in range(4)]
    with patch.object(mi, "get_token", return_value=("tok", None)), \
         patch.object(mi, "fetch", return_value={"channels": {}, "videos": {}, "seed_by_channel": {}, "stopped": None, "units": 321}), \
         patch.object(mi, "_rows_from_fetch", return_value=(channels, vids)):
        out = cpn.start_plan(ans, db, background=False)
    g = cpn.get_plan(out["plan_id"], db)
    assert g["status"] == "done" and g["plan"]["options"][0]["niche"] == "history facts" and g["plan"]["units_used"] == 321
    assert g["progress"]["stage"] == "done"


def test_a_missing_authorization_fails_cleanly(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    ans = {"goal": "income", "families": [], "language": "en", "audience": "adults", "format": "shorts", "effort": "steady", "risk": "balanced", "money": "ads"}
    with patch.object(mi, "get_token", return_value=(None, "no YouTube authorization available: x")):
        out = cpn.start_plan(ans, db, background=False)
    g = cpn.get_plan(out["plan_id"], db)
    assert g["status"] == "failed" and "authorization" in g["error"]


def test_routes_page_api_and_wizard_prefill(tmp_path):
    import run_dashboard as server
    db = tmp_path / "t.db"
    initialize_schema(db)
    old = server.app.config.get("DB_PATH")
    server.app.config["DB_PATH"] = str(db)
    try:
        c = server.app.test_client()
        r = c.get("/channels/plan")
        assert r.status_code == 200 and "What do you want this channel to do for you?" in r.get_data(as_text=True)
        assert c.post("/api/channel-plan/start", json={"answers": {"goal": "x"}}).status_code == 400
        assert c.get("/api/channel-plan/cp_missing").status_code == 404
        # a finished plan pre-fills the wizard
        pid = "cp_test"
        from hypogeum.db import get_connection
        conn = get_connection(db)
        plan = {"options": [{"niche": "history facts", "prefill": {"concept": "C", "genre": "History Facts", "language": "en", "format": "shorts",
                                                                    "shorts_per_week": 7, "videos_per_week": 1, "brand": {"tone": "calm"}, "weekly_video_budget_usd": 150}}],
                "caveats": [], "channels_researched": 1, "videos_read": 1}
        conn.execute("INSERT INTO channel_plans (plan_id, created_at, status, answers_json, plan_json) VALUES (?,?,?,?,?)",
                     (pid, "2026-10-13T00:00:00+00:00", "done", "{}", json.dumps(plan)))
        conn.commit()
        conn.close()
        t = c.get(f"/channels/new?plan={pid}&option=0").get_data(as_text=True)
        assert '"genre": "History Facts"' in t and "const PREFILL" in t
        assert "const PREFILL = null" in c.get("/channels/new").get_data(as_text=True)
    finally:
        server.app.config["DB_PATH"] = old
