"""tests/test_client_review.py — the monthly strategy review: measured, proposed, never applied unasked."""
from __future__ import annotations

import json
from datetime import date

import pytest

import hypogeum.db as db
from hypogeum import client_manager as cm
from hypogeum import client_review as cr
from hypogeum import operator_actions as oact
from hypogeum import operator_alerts as oa
from hypogeum.agents import brand_strategy_agent as bsa

TODAY = date(2026, 11, 3)


def _strategy(version=1, created="2026-10-03", pillar="Editing decisions"):
    return {"version": version, "created_at": created, "positioning": "Documentary editor.", "pillars": [{"name": pillar, "why": "w", "formats": ["Text"]},
            {"name": "Before / after", "why": "w", "formats": ["Video"]}], "platforms": {"linkedin": {"role": "primary", "language": "en"}},
            "cadence": {"posts_per_week": 2, "plan": "Tue/Thu"}, "kpis": ["k"], "cta": "c", "guardrails": [], "evidence": [], "open_questions": []}


def _llm(changes=None, keep=None, strategy=None, calls=None):
    def run(system, user, max_tokens, *, agent, client_id):
        if calls is not None:
            calls.append(json.loads(user.split("Input:\n", 1)[1]))
        s = strategy or dict(_strategy(), pillars=[{"name": "Editing decisions", "why": "it worked", "formats": ["Text"]},
                                                  {"name": "Behind the scenes", "why": "new", "formats": ["Reel"]}])
        return json.dumps({"summary": "October: text posts beat the baseline.", "changes": changes if changes is not None else
                           [{"what": "Post more text", "why": "it earns comments", "evidence": "4 text posts averaged 9 comments vs 3 baseline"}],
                           "keep": keep or ["Before/after stays"], "strategy": s}), 0.02
    return run


@pytest.fixture()
def env(tmp_path):
    p = tmp_path / "rv.db"
    db.initialize_schema(p)
    brief = {"brand_profile": {"answered_at": "2026-10-03", "posts_per_week": 2}, "brand_strategy": _strategy(),
             "content_calendars": {"2026-10": {"month": "2026-10", "posts": [{"date": "2026-10-13", "status": "published", "pillar": "Editing decisions"},
                                                                          {"date": "2026-10-15", "status": "skipped", "pillar": "Before / after"}]}}}
    cm.create_client("Me", client_id="me", brief=brief, db_path=p)
    conn = db.get_connection(p)
    conn.execute("PRAGMA foreign_keys = OFF")
    rows = [("c1", "linkedin", "2026-10-13", "Text post about silence", 9, 3200), ("c2", "linkedin", "2026-10-20", "Another text post", 11, 4100),
            ("c3", "linkedin", "2026-10-27", "Award post", 2, 800), ("c4", "linkedin", "2026-08-10", "Old post", 3, 1000), ("c5", "instagram", "2026-09-20", "Old reel", 1, 200)]
    for cid, plat, d, cap, comments, imp in rows:
        conn.execute("INSERT INTO content_items (content_id, client_id, platform, platform_post_id, published_at, caption, source, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (cid, "me", plat, cid, d, cap, "linkedin_export", d, d))
        conn.execute("INSERT INTO platform_analytics (record_id, episode_id, platform, snapshot_date, window_days, comments, impressions, source, client_id, content_id, imported_at, metadata_version) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (f"r_{cid}", f"client:me", plat, d, 1, comments, imp, "api", "me", cid, d, "1"))
    conn.commit()
    conn.close()
    return p


def test_the_month_is_measured_against_a_90_day_baseline_per_platform(env):
    r = cr.month_results("me", "2026-10", env)
    li = r["linkedin"]
    assert li["last_month"]["posts"] == 3 and li["measured"] == 3 and li["last_month"]["avg_comments"] == pytest.approx(7.33, abs=0.01)
    assert li["baseline_90d"]["posts"] == 1 and li["baseline_90d"]["avg_comments"] == 3.0
    assert r["instagram"]["last_month"]["posts"] == 0 and r["instagram"]["baseline_90d"]["posts"] == 1


def test_a_review_is_a_proposal_the_strategy_is_untouched_until_applied(env):
    calls = []
    prop = cr.propose_review("me", "2026-10", db_path=env, llm=_llm(calls=calls))
    assert prop["status"] == "pending" and not prop["thin_data"] and prop["changes"][0]["evidence"]
    sent = calls[0]
    assert sent["current_strategy"]["version"] == 1 and sent["thin_data"] is False and sent["calendar"]["by_status"] == {"published": 1, "skipped": 1}
    brief = cm.get_client("me", db_path=env)["brief"]
    assert brief["brand_strategy"]["version"] == 1 and brief["strategy_proposal"]["strategy"]["pillars"][1]["name"] == "Behind the scenes"
    assert brief["reviews"]["2026-10"]["status"] == "pending"
    assert cr.apply_proposal("me", env) is True
    after = cm.get_client("me", db_path=env)["brief"]
    assert after["brand_strategy"]["version"] == 2 and "monthly review 2026-10" in after["brand_strategy"]["author"]
    assert after["brand_strategy_history"][-1]["version"] == 1 and "strategy_proposal" not in after and after["reviews"]["2026-10"]["status"] == "applied"
    assert cr.apply_proposal("me", env) is False                                   # decided once


def test_thin_data_keeps_only_evidence_backed_changes_and_says_so(env):
    conn = db.get_connection(env)
    conn.execute("DELETE FROM platform_analytics WHERE content_id IN ('c1','c2','c3')")
    conn.commit()
    conn.close()
    calls = []
    prop = cr.propose_review("me", "2026-10", db_path=env, llm=_llm(
        changes=[{"what": "Drop before/after", "why": "feels weak", "evidence": ""}, {"what": "Keep text", "why": "x", "evidence": "1 post"}], calls=calls))
    assert prop["thin_data"] is True and calls[0]["thin_data"] is True
    assert [c["what"] for c in prop["changes"]] == ["Keep text"]                  # the guess without evidence is dropped


def test_discard_and_a_strategy_that_changed_since_the_proposal_never_apply(env):
    cr.propose_review("me", "2026-10", db_path=env, llm=_llm())
    assert cr.discard_proposal("me", env) is True
    brief = cm.get_client("me", db_path=env)["brief"]
    assert brief["brand_strategy"]["version"] == 1 and brief["reviews"]["2026-10"]["status"] == "discarded"
    cr.propose_review("me", "2026-10", db_path=env, llm=_llm())
    bsa_brief = cm.get_client("me", db_path=env)["brief"]
    bsa_brief["brand_strategy"] = dict(bsa_brief["brand_strategy"], version=2)     # someone regenerated the strategy meanwhile
    cm.update_client("me", brief=bsa_brief, db_path=env)
    assert cr.apply_proposal("me", env) is False                                   # stale: refuses to overwrite


def test_due_reviews_run_once_a_day_for_last_month_and_respect_new_strategies(env):
    calls = []
    assert cr.run_due_reviews(env, TODAY, _llm(calls=calls)) == ["me"] and len(calls) == 1
    assert cr.run_due_reviews(env, TODAY, _llm(calls=calls)) == [] and len(calls) == 1          # already reviewed 2026-10
    b = cm.get_client("me", db_path=env)["brief"]
    b["reviews"] = {}
    b["review_attempt"] = TODAY.isoformat()
    cm.update_client("me", brief=b, db_path=env)
    assert cr.run_due_reviews(env, TODAY, _llm(calls=calls)) == []                               # one attempt per day, even after a failure
    b["review_attempt"] = ""
    b["brand_strategy"] = _strategy(created="2026-10-28")                                        # a strategy only 6 days old
    cm.update_client("me", brief=b, db_path=env)
    assert cr.run_due_reviews(env, TODAY, _llm(calls=calls)) == []


def test_a_failed_model_call_is_logged_and_changes_nothing(env):
    def boom(*a, **k):
        raise RuntimeError("402")
    with pytest.raises(bsa.BrandGenerationError):
        cr.propose_review("me", "2026-10", db_path=env, llm=boom)
    brief = cm.get_client("me", db_path=env)["brief"]
    assert "strategy_proposal" not in brief and brief["brand_strategy"]["version"] == 1 and brief["generation_log"][-1]["ok"] is False


def test_the_proposal_reaches_telegram_with_buttons_and_the_buttons_decide(env, monkeypatch):
    cr.propose_review("me", "2026-10", db_path=env, llm=_llm())
    alerts = oa.check_client_reviews(env, None)
    key = "clientreview:me:2026-10"
    assert set(alerts) == {key}
    title, body = alerts[key]
    assert "2026-10" in title and "4 text posts averaged 9 comments" in body and "/client/me/plan" in body
    kb = oact.buttons_for(key)["inline_keyboard"][0]
    assert [b["callback_data"] for b in kb] == ["act:review_apply:me", "act:review_discard:me"]
    from hypogeum import alert_recipients as ar
    assert ar.alert_type_of(key) == "approvals"
    monkeypatch.setattr(oact, "_operator_chats", lambda db_path: {"1"})
    assert "اتفعّلت" in oact.handle("1", "act:review_apply:me", db_path=env)
    assert cm.get_client("me", db_path=env)["brief"]["brand_strategy"]["version"] == 2
    assert oa.check_client_reviews(env, None) == {}
    assert "مش موجود" in oact.handle("1", "act:review_apply:me", db_path=env)
    assert "can't run" in oact.handle("9", "act:review_discard:me", db_path=env)


def test_the_plan_page_shows_the_proposal_and_decides(env):
    import run_dashboard as server
    cr.propose_review("me", "2026-10", db_path=env, llm=_llm())
    old = server.app.config.get("DB_PATH")
    server.app.config["DB_PATH"] = str(env)
    try:
        c = server.app.test_client()
        page = c.get("/client/me/plan?scope=client:me").get_data(as_text=True)
        assert "a proposal waits for your decision" in page and "Apply the new strategy" in page and "4 text posts averaged 9 comments" in page
        r = c.post("/client/me/plan/review", data={"action": "apply"})
        assert r.status_code == 302 and cm.get_client("me", db_path=env)["brief"]["brand_strategy"]["version"] == 2
        assert "waits for your decision" not in c.get("/client/me/plan?scope=client:me").get_data(as_text=True)
        assert c.post("/client/ghost/plan/review", data={"action": "apply"}).status_code == 404
    finally:
        server.app.config["DB_PATH"] = old
