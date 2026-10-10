"""tests/test_client_post_approvals.py — a client's drafted posts: ask the operator, never publish unasked."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

import hypogeum.db as db
from hypogeum import client_manager as cm
from hypogeum import client_post_approvals as cpa
from hypogeum import operator_actions as oact
from hypogeum import operator_alerts as oa

TODAY = date(2026, 10, 10)


def _post(d, status=None, draft="Hello world", **kw):
    p = {"date": d, "platforms": ["linkedin", "instagram"], "pillar": "Editing decisions", "format": "Text post", "draft": draft}
    if status:
        p["status"] = status
    p.update(kw)
    return p


@pytest.fixture()
def env(tmp_path):
    p = tmp_path / "ap.db"
    db.initialize_schema(p)
    brief = {"brand_strategy": {"version": 1}, "content_calendars": {"2026-10": {"month": "2026-10", "posts": [
        _post("2026-10-06"), _post("2026-10-08", "approved"), _post("2026-10-13", needs="A clip from an AI ad"),
        _post("2026-10-15"), _post("2026-10-20"), _post("2026-10-24"), _post("2026-10-14", "published"), _post("2026-10-16", draft="  ")]}}}
    cm.create_client("Me", client_id="me", brief=brief, db_path=p)
    cm.create_client("No Strategy", client_id="nostrat", brief={"content_calendars": {"2026-10": {"posts": [_post("2026-10-13")]}}}, db_path=p)
    return p


def _status(p, d):
    from hypogeum.dashboard_clients import calendars_of
    for cal in calendars_of(cm.get_client("me", db_path=p)["brief"]).values():
        for post in cal["posts"]:
            if post["date"] == d:
                return post.get("status") or "draft"


def test_only_undecided_non_empty_drafts_inside_the_window_are_due(env):
    due = cpa.due_drafts(env, TODAY)
    assert [i["post"]["date"] for i in due] == ["2026-10-13", "2026-10-15"]        # 10-20 is beyond 7 days, 10-16 is empty, 10-14 is published
    assert all(i["client_id"] == "me" for i in due)                                # a client without a strategy never asks


def test_past_drafts_are_skipped_with_a_reason_and_published_ones_are_untouched(env):
    assert cpa.expire_past(env, TODAY) == 2                                        # 10-06 (draft) and 10-08 (approved but never posted)
    assert _status(env, "2026-10-06") == "skipped" and _status(env, "2026-10-08") == "skipped"
    assert _status(env, "2026-10-14") == "published" and _status(env, "2026-10-13") == "draft"
    assert cpa.expire_past(env, TODAY) == 0                                        # idempotent


def test_decisions_are_final_once_published_or_skipped(env):
    assert cpa.set_status("me", "2026-10-13", "approved", env) is True and _status(env, "2026-10-13") == "approved"
    assert cpa.set_status("me", "2026-10-13", "skipped", env) is True              # an approved post can still be skipped
    assert cpa.set_status("me", "2026-10-13", "approved", env) is False            # skipped is final
    assert cpa.set_status("me", "2026-10-14", "skipped", env) is False             # published is final
    assert cpa.set_status("me", "2099-01-01", "approved", env) is False and cpa.set_status("ghost", "2026-10-13", "approved", env) is False
    with pytest.raises(ValueError):
        cpa.set_status("me", "2026-10-15", "published", env)                       # this module can never mark a post published


def test_the_alert_carries_the_draft_the_needs_and_a_dashboard_pointer(env):
    item = [i for i in cpa.due_drafts(env, TODAY) if i["post"]["date"] == "2026-10-13"][0]
    title, body = cpa.alert_text(item)
    assert "2026-10-13" in title and "Linkedin + Instagram" in title
    assert "Hello world" in body and "A clip from an AI ad" in body and "/client/me/plan" in body
    long = dict(item, post=dict(item["post"], draft="x" * 5000))
    assert len(cpa.alert_text(long)[1]) < 2400                                      # Telegram's message limit


def test_the_check_expires_old_drafts_and_asks_for_the_due_ones(env):
    out = oa.check_client_drafts(env, datetime(2026, 10, 10, 12, tzinfo=timezone.utc))
    assert set(out) == {"clientdraft:me:2026-10-13", "clientdraft:me:2026-10-15"}
    assert _status(env, "2026-10-06") == "skipped"
    from hypogeum import alert_recipients as ar
    assert ar.alert_type_of("clientdraft:me:2026-10-13") == "approvals"
    kb = oact.buttons_for("clientdraft:me:2026-10-13")["inline_keyboard"][0]
    assert [b["text"] for b in kb] == ["✅ وافق", "⏭ تخطّى"] and "me|2026-10-13" in kb[0]["callback_data"]


def test_the_telegram_buttons_decide_only_for_operator_chats(env, monkeypatch):
    monkeypatch.setattr(oact, "_operator_chats", lambda db_path: {"111"})
    ok = oact.handle("111", "act:post_approve:me|2026-10-13", db_path=env)
    assert "اتوافق" in ok and _status(env, "2026-10-13") == "approved"
    assert "تخطّينا" in oact.handle("111", "act:post_skip:me|2026-10-15", db_path=env) and _status(env, "2026-10-15") == "skipped"
    assert "مش موجودة" in oact.handle("111", "act:post_approve:me|2026-10-15", db_path=env)          # already decided
    assert "can't run" in oact.handle("999", "act:post_approve:me|2026-10-20", db_path=env)         # a client's chat is refused
    assert _status(env, "2026-10-20") == "draft"
