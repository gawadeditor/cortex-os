"""tests/test_linkedin_api.py — LinkedIn personal-profile publishing: sign-in, send-once posting, alerts, dashboard card."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import hypogeum.db as db
from hypogeum import client_manager as cm
from hypogeum import client_publishing as cp
from hypogeum import operator_alerts as oa
from hypogeum.publishing import linkedin_api as li

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)
JPG = b"\xff\xd8\xff\xe0" + b"0" * 2000


class Net:
    """Scripted LinkedIn: records every call, answers by URL."""

    def __init__(self, post_status=201, exc=None):
        self.calls, self.post_status, self.exc = [], post_status, exc

    def __call__(self, method, url, *, headers=None, data=None, json_body=None, timeout=0):
        self.calls.append((method, url, json_body))
        if url == li.TOKEN_URL:
            return li.HttpResponse(200, {"access_token": "TOK", "expires_in": 5184000})
        if url.endswith("/v2/userinfo"):
            return li.HttpResponse(200, {"sub": "abc123", "name": "Mo Gawad"})
        if "images?action=initializeUpload" in url:
            return li.HttpResponse(200, {"value": {"uploadUrl": "https://upload.example/u1", "image": "urn:li:image:I1"}})
        if method == "PUT":
            return li.HttpResponse(201)
        if url.endswith("/rest/posts"):
            if self.exc:
                raise self.exc
            return li.HttpResponse(self.post_status, {"message": "nope"} if self.post_status >= 400 else None,
                                   {"x-restli-id": "urn:li:share:777"})
        raise AssertionError(url)


@pytest.fixture()
def env(tmp_path):
    p = tmp_path / "li.db"
    db.initialize_schema(p)
    media = tmp_path / "pic.jpg"
    media.write_bytes(JPG)

    def post(d, **kw):
        base = {"date": d, "platforms": ["linkedin"], "status": "approved", "draft": "A post (with brackets) and #tags.", "time": "09:00"}
        base.update(kw)
        return base
    brief = {"content_calendars": {"2026-10": {"month": "2026-10", "posts": [post("2026-10-14")]}}}
    cm.create_client("Me", client_id="me", engagement_mode="manage", brief=brief, db_path=p)
    return p, post, media


def _signin(p, net=None):
    li.store_app_credentials("APPID", "APPSECRET", db_path=p)
    return li.sign_in("me", "CODE", "http://127.0.0.1:5001/linkedin/callback", db_path=p, send=net or Net())


def _post(p, d="2026-10-14"):
    from hypogeum.dashboard_clients import calendars_of
    return next(x for cal in calendars_of(cm.get_client("me", db_path=p)["brief"]).values() for x in cal["posts"] if x["date"] == d)


def test_escape_keeps_hashtags_and_escapes_markup():
    assert li.escape_commentary("A (b) [c] #tag") == "A \\(b\\) \\[c\\] #tag"
    assert li.escape_commentary("5 # 6 @x_y") == "5 \\# 6 \\@x\\_y"


def test_sign_in_stores_token_and_status_never_leaks_it(env):
    p, _, _ = env
    net = Net()
    r = _signin(p, net)
    assert r["name"] == "Mo Gawad"
    st = li.token_status("me", now=NOW - timedelta(days=0), db_path=p)
    assert st["connected"] and st["name"] == "Mo Gawad" and "TOK" not in json.dumps(st)
    far = datetime.now(timezone.utc) + timedelta(days=55)
    assert li.token_status("me", now=far, db_path=p)["renew_soon"] is True
    assert li.token_status("me", now=datetime.now(timezone.utc) + timedelta(days=61), db_path=p)["connected"] is False


def test_pasted_values_lose_stray_punctuation(env):
    p, _, _ = env
    li.store_app_credentials("78eand4adosxdi،\u200f ", " WPL_AP1.abc.def==، ", db_path=p)
    assert li.app_credentials(p) == {"client_id": "78eand4adosxdi", "client_secret": "WPL_AP1.abc.def=="}


def test_sign_in_without_app_is_a_plain_error(env):
    p, _, _ = env
    with pytest.raises(li.LinkedInError):
        li.sign_in("me", "C", "http://x", db_path=p, send=Net())


def test_publish_due_sends_once_and_marks_published(env):
    p, _, _ = env
    _signin(p)
    net = Net()
    res = cp.publish_linkedin_due(p, NOW, publisher_for=lambda cid: li.LinkedInPublisher(cid, db_path=p, send=net))
    assert res[0]["state"] == "published"
    sent = [c for c in net.calls if c[1].endswith("/rest/posts")]
    assert len(sent) == 1 and sent[0][2]["commentary"].startswith("A post \\(with brackets\\)") and sent[0][2]["author"] == "urn:li:person:abc123"
    post = _post(p)
    assert post["status"] == "published" and post["auto"]["linkedin"]["release_url"].endswith("urn:li:share:777/")
    assert cp.publish_linkedin_due(p, NOW, publisher_for=lambda cid: li.LinkedInPublisher(cid, db_path=p, send=net)) == []
    assert len([c for c in net.calls if c[1].endswith("/rest/posts")]) == 1


def test_not_due_yet_and_observe_clients_are_left_alone(env):
    p, _, _ = env
    _signin(p)
    net = Net()
    pf = lambda cid: li.LinkedInPublisher(cid, db_path=p, send=net)
    assert cp.publish_linkedin_due(p, datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc), publisher_for=pf) == []
    cm.update_client("me", engagement_mode="observe", db_path=p)
    assert cp.publish_linkedin_due(p, NOW, publisher_for=pf) == [] and not net.calls


def test_image_is_uploaded_then_attached(env):
    p, post, media = env
    _signin(p)
    _post(p)
    cp._store(p, "me", "2026-10-14", lambda x: x.update(media={"path": str(media), "kind": "image", "name": "pic.jpg", "size": len(JPG), "mime": "image/jpeg"}))
    net = Net()
    res = cp.publish_linkedin_due(p, NOW, publisher_for=lambda cid: li.LinkedInPublisher(cid, db_path=p, send=net))
    assert res[0]["state"] == "published"
    body = next(c for c in net.calls if c[1].endswith("/rest/posts"))[2]
    assert body["content"] == {"media": {"id": "urn:li:image:I1"}}


def test_blocked_reasons_are_plain_and_not_rewritten_every_round(env):
    p, _, _ = env
    res = cp.publish_linkedin_due(p, NOW, publisher_for=lambda cid: pytest.fail("must not send"))
    assert res[0]["state"] == "blocked" and "not connected" in res[0]["issues"][0]
    _signin(p)
    cp._store(p, "me", "2026-10-14", lambda x: x.update(draft="Fill in [award name] first"))
    r2 = cp.publish_linkedin_due(p, NOW, publisher_for=lambda cid: pytest.fail("must not send"))
    assert "[award name]" in r2[0]["issues"][0]
    cp._store(p, "me", "2026-10-14", lambda x: x.update(draft="ok", media={"path": str(Path(p).parent / "v.mp4"), "kind": "video", "size": 0}))
    assert cp.issues(_post(p), "linkedin")      # video / missing file never goes out half-finished


def test_ambiguous_and_refused_outcomes_are_recorded_and_alerted(env):
    p, _, _ = env
    _signin(p)
    amb = Net(exc=li.LinkedInAmbiguousError("timeout"))
    assert cp.publish_linkedin_due(p, NOW, publisher_for=lambda c: li.LinkedInPublisher(c, db_path=p, send=amb))[0]["state"] == "ambiguous"
    alerts = oa.check_client_publishing(p, NOW)
    assert any(k.startswith("clientpost:me:2026-10-14:linkedin") for k in alerts) and "LinkedIn" in list(alerts.values())[0][0]
    again = Net()
    assert cp.publish_linkedin_due(p, NOW, publisher_for=lambda c: li.LinkedInPublisher(c, db_path=p, send=again)) == []   # never re-sent


def test_rejected_5xx_vs_4xx(env):
    p, _, _ = env
    _signin(p)
    net = Net(post_status=422)
    assert cp.publish_linkedin_due(p, NOW, publisher_for=lambda c: li.LinkedInPublisher(c, db_path=p, send=net))[0]["state"] == "failed"
    with pytest.raises(li.LinkedInAmbiguousError):
        li.LinkedInPublisher("me", db_path=p, send=Net(post_status=503)).publish("x")


def test_renewal_alert_appears_a_week_before_expiry(env):
    p, _, _ = env
    _signin(p)
    assert not any(k.startswith("linkedin_renew") for k in oa.check_client_publishing(p, NOW))
    late = datetime.now(timezone.utc) + timedelta(days=55)
    assert "linkedin_renew:me" in oa.check_client_publishing(p, late)


def test_dashboard_card_and_callback_state(env):
    import run_dashboard as rd
    p, _, _ = env
    old = rd.app.config.get("DB_PATH")
    rd.app.config["DB_PATH"] = str(p)
    rd.app.config["TESTING"] = True
    try:
        cl = rd.app.test_client()
        assert b"Client Secret" in cl.get("/client/me/accounts").data
        r = cl.post("/client/me/accounts/linkedin_app", data={"app_client_id": "APPID", "app_client_secret": "APPSECRET"})
        assert r.status_code == 302 and li.app_credentials(p)["client_id"] == "APPID"
        page = cl.get("/client/me/accounts").data
        assert b"Sign in with LinkedIn" in page and b"APPSECRET" not in page
        go = cl.get("/client/me/accounts/linkedin_connect")
        assert go.status_code == 302 and go.headers["Location"].startswith(li.AUTH_URL) and "client_id=APPID" in go.headers["Location"]
        assert cl.get("/linkedin/callback?code=x&state=forged").status_code == 400
    finally:
        rd.app.config["DB_PATH"] = old
