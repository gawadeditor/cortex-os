"""tests/test_brand_strategy_agent.py — automatic strategy + calendar generation.
Fake LLM, tmp DB, no network."""
from __future__ import annotations

import json
from datetime import date

import pytest

from hypogeum import client_manager as cm
from hypogeum.agents import brand_strategy_agent as bsa
from hypogeum.db import initialize_schema
from hypogeum.dashboard_clients import calendars_of, get_client_plan

STRATEGY = {
    "positioning": "Documentary editor and colorist.",
    "pillars": [{"name": "Before / after", "why": "proof", "formats": ["split video"]},
                {"name": "Editing decisions", "why": "thinking", "formats": ["text"]}],
    "platforms": {"linkedin": {"role": "primary", "language": "English"},
                  "youtube": {"role": "not connected", "language": "English"}},
    "cadence": {"posts_per_week": 9, "plan": "Tue + Thu"},
    "kpis": ["Inquiries"], "cta": "DM me", "guardrails": ["No politics"],
    "evidence": ["857 impressions on one post"], "open_questions": [],
}


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "b.db"
    initialize_schema(p)
    cm.create_client("Me", client_id="me", db_path=p)
    cm.link_managed_account("me", "linkedin", handle="me", provider_id="linkedin_export",
                            provider_account_id="me", native_account_id="me", db_path=p)
    cm.update_client("me", db_path=p, brief={"brand_profile": {
        "answered_at": "2026-10-03", "posts_per_week": 2, "languages": ["en"], "avoid": ["Politics"]}})
    return p


class FakeLLM:
    def __init__(self, reply, cost=0.05):
        self.reply, self.cost, self.calls = reply, cost, []

    def __call__(self, system, user, max_tokens, *, agent, client_id):
        self.calls.append({"system": system, "user": user, "agent": agent})
        reply = self.reply(user) if callable(self.reply) else self.reply
        return (reply if isinstance(reply, str) else json.dumps(reply)), self.cost


def test_month_slots_are_deterministic_and_skip_past_days():
    slots = bsa.month_slots("2026-11", 2, ["linkedin"], today=date(2026, 10, 3))
    assert [s["date"] for s in slots][:3] == ["2026-11-03", "2026-11-05", "2026-11-10"]
    assert all(s["day"] in ("Tue", "Thu") for s in slots) and len(slots) == 8
    oct_slots = bsa.month_slots("2026-10", 2, ["linkedin"], today=date(2026, 10, 20))
    assert oct_slots[0]["date"] == "2026-10-22"
    assert bsa.next_month(date(2026, 12, 15)) == "2027-01"


def test_strategy_requires_questionnaire(db):
    cm.update_client("me", db_path=db, brief={"brand_profile": {}})
    with pytest.raises(bsa.BrandGenerationError, match="questionnaire"):
        bsa.generate_strategy("me", db_path=db, llm=FakeLLM(STRATEGY))


def test_generate_strategy_coerces_versions_and_logs_cost(db):
    llm = FakeLLM("```json\n" + json.dumps(STRATEGY) + "\n```")
    s1 = bsa.generate_strategy("me", db_path=db, llm=llm)
    assert s1["version"] == 1 and s1["positioning"]
    assert "youtube" not in s1["platforms"]                 # not a connected platform
    assert s1["cadence"]["posts_per_week"] == 2             # questionnaire wins over the model's 9
    assert "Politics" in llm.calls[0]["user"]               # profile reached the model
    s2 = bsa.generate_strategy("me", db_path=db, llm=FakeLLM(STRATEGY))
    brief = cm.get_client("me", db_path=db)["brief"]
    assert s2["version"] == 2 and brief["brand_strategy_history"][0]["version"] == 1
    assert [e["ok"] for e in brief["generation_log"]] == [True, True]
    assert bsa.generation_spend_this_month(brief) == pytest.approx(0.10)


def test_bad_model_output_is_logged_and_raises(db):
    with pytest.raises(bsa.BrandGenerationError):
        bsa.generate_strategy("me", db_path=db, llm=FakeLLM("not json at all"))
    brief = cm.get_client("me", db_path=db)["brief"]
    assert brief["generation_log"][-1]["ok"] is False and "brand_strategy" not in brief


def test_budget_gate_blocks_generation(db):
    cm.update_client("me", db_path=db, monthly_analysis_budget_usd=0.05)
    bsa.generate_strategy("me", db_path=db, llm=FakeLLM(STRATEGY, cost=0.06))
    with pytest.raises(bsa.BrandGenerationError, match="budget"):
        bsa.generate_strategy("me", db_path=db, llm=FakeLLM(STRATEGY))


def test_generate_calendar_fills_slots_and_keeps_other_months(db):
    bsa.generate_strategy("me", db_path=db, llm=FakeLLM(STRATEGY))
    # an existing October calendar must survive generating November
    b = cm.get_client("me", db_path=db)["brief"]
    b["content_calendar"] = {"month": "2026-10", "posts": [{"date": "2026-10-06", "draft": "Oct post",
                                                             "platforms": ["linkedin"], "lang": "en"}]}
    cm.update_client("me", db_path=db, brief=b)

    def reply(user):
        slots = json.loads(user.split("Input:\n", 1)[1])["slots"]
        return {"posts": [{"date": s["date"], "platforms": ["linkedin", "tiktok"], "lang": "en",
                           "pillar": "Before / after", "format": "text", "needs": None,
                           "draft": f"Post for {s['date']}"} for s in slots]}
    cal = bsa.generate_calendar("me", month="2026-11", db_path=db, llm=FakeLLM(reply), today=date(2026, 10, 3))
    assert len(cal["posts"]) == 8 and cal["posts"][0]["platforms"] == ["linkedin"]   # tiktok filtered out
    assert all(p["status"] == "draft" for p in cal["posts"])
    cals = calendars_of(cm.get_client("me", db_path=db)["brief"])
    assert set(cals) == {"2026-10", "2026-11"} and cals["2026-10"]["posts"][0]["draft"] == "Oct post"
    plan = get_client_plan("me", db_path=db, month="2026-11")
    assert plan["month"] == "2026-11" and plan["months"] == ["2026-10", "2026-11"]


def test_generate_route_runs_in_background_and_reports(db, monkeypatch):
    import run_dashboard as rd
    import threading
    monkeypatch.setattr(bsa, "_default_llm", lambda config=None: FakeLLM(STRATEGY))
    started = []
    real_thread = threading.Thread

    class SyncThread(real_thread):
        def start(self):
            started.append(self.name)
            self.run()
    monkeypatch.setattr(threading, "Thread", SyncThread)
    old = rd.app.config.get("DB_PATH")
    rd.app.config["DB_PATH"] = str(db)
    try:
        cl = rd.app.test_client()
        r = cl.post("/client/me/plan/generate", data={"what": "strategy"})
        assert r.status_code == 302 and started
        g = get_client_plan("me", db_path=db)["generation"]
        assert g["state"] == "done" and "Strategy v1" in g["message"]
        page = cl.get("/client/me/plan?scope=client:me").get_data(as_text=True)
        assert "Regenerate strategy" in page and "calendar" in page
    finally:
        rd.app.config["DB_PATH"] = old
