"""tests/test_client_publishing.py — approved posts of a MANAGED client go to Instagram once, safely."""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import hypogeum.db as db
from hypogeum import client_manager as cm
from hypogeum import client_media as cmed
from hypogeum import client_publishing as cp
from hypogeum import operator_alerts as oa
from hypogeum.publishing import postiz as pz

NOW = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)
JPG = b"\xff\xd8\xff\xe0" + b"0" * 2000
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 4000


class FakeProvider:
    def __init__(self, integrations=None, publish=None, upload=None, states=None):
        self.integrations = integrations if integrations is not None else [
            {"id": "int1", "identifier": "instagram", "profile": "gawadeditor", "disabled": False}]
        self.publish_exc, self.upload_exc, self.states = publish, upload, states or {}
        self.sent, self.uploads = [], []

    def list_integrations(self, **k):
        return self.integrations

    def upload_local_media(self, path, *, mime_type, **k):
        if self.upload_exc:
            raise self.upload_exc
        self.uploads.append((Path(path).name, mime_type))
        return {"id": "m1", "path": "https://x/uploads/m1.jpg"}

    def publish_client_post(self, **k):
        if self.publish_exc:
            raise self.publish_exc
        self.sent.append(k)
        return {"post_id": f"pz{len(self.sent)}", "integration_id": k["integration_id"]}

    def get_post(self, post_id, *, around, **k):
        return self.states.get(post_id)


@pytest.fixture()
def env(tmp_path):
    p = tmp_path / "pub.db"
    db.initialize_schema(p)
    media = tmp_path / "pic.jpg"
    media.write_bytes(JPG)

    def post(d, **kw):
        base = {"date": d, "platforms": ["linkedin", "instagram"], "status": "approved", "draft": "A caption without gaps.",
                "needs": "A clip", "media": {"path": str(media), "kind": "image", "name": "pic.jpg", "size": len(JPG), "mime": "image/jpeg"}}
        base.update(kw)
        return base
    brief = {"brand_strategy": {"version": 1}, "content_calendars": {"2026-10": {"month": "2026-10", "posts": [
        post("2026-10-14"), post("2026-10-15", platforms=["instagram"]), post("2026-10-20")]}}}
    cm.create_client("Me", client_id="me", engagement_mode="manage", brief=brief, db_path=p)
    cm.create_client("Watching", client_id="obs", engagement_mode="observe", brief=brief, db_path=p)
    conn = db.get_connection(p)
    conn.execute("PRAGMA foreign_keys = OFF")
    for cid in ("me", "obs"):
        conn.execute("INSERT INTO managed_accounts (managed_account_id, client_id, platform, handle, provider_id, enabled, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,1,?,?)", (f"a_{cid}", cid, "instagram", "gawadeditor", "instagram_graph", NOW.isoformat(), NOW.isoformat()))
    conn.commit()
    conn.close()
    return p, post


def _post(p, d, client="me"):
    from hypogeum.dashboard_clients import calendars_of
    return next(x for cal in calendars_of(cm.get_client(client, db_path=p)["brief"]).values() for x in cal["posts"] if x["date"] == d)


def test_issues_name_every_reason_a_post_cannot_go_out(env, tmp_path):
    _, post = env
    assert cp.issues(post("2026-10-14")) == []
    assert any("[placeholder]" in i or "[Gaza" in i for i in cp.issues(post("d", draft="Our film [film title] won")))
    assert any("empty" in i for i in cp.issues(post("d", draft="  ")))
    assert any("2200" in i for i in cp.issues(post("d", draft="x" * 2300)))
    no_media = post("d", media=None, needs="A before/after shot")
    assert any("no material" in i and "before/after" in i for i in cp.issues(no_media))
    assert any("missing" in i for i in cp.issues(post("d", media={"path": str(tmp_path / "gone.jpg"), "size": 1})))


def test_an_approved_due_post_is_sent_once_with_its_caption_and_material(env):
    p, _ = env
    prov = FakeProvider()
    r = cp.publish_due(p, NOW, prov)
    assert [(x["date"], x["state"]) for x in r] == [("2026-10-14", "scheduled")]   # 10-15 (17:00 UTC) is 35h away, 10-20 further: not due yet
    s = prov.sent[0]
    assert s["platform"] == "instagram" and s["integration_id"] == "int1" and s["content"] == "A caption without gaps."
    assert s["media"] == [{"id": "m1", "path": "https://x/uploads/m1.jpg"}] and prov.uploads[0] == ("pic.jpg", "image/jpeg")
    assert s["scheduled_for"] >= NOW + timedelta(minutes=10)                     # never in the past
    assert _post(p, "2026-10-14")["auto"]["instagram"]["post_id"] == "pz1"
    assert cp.publish_due(p, NOW, prov) == [] and len(prov.sent) == 1             # a second round sends nothing again
    later = cp.publish_due(p, NOW + timedelta(days=1), prov)                       # a day on, 10-15 comes into the 12-hour window
    assert [(x["date"], x["state"]) for x in later] == [("2026-10-15", "scheduled")] and len(prov.sent) == 2


def test_only_managed_clients_and_only_approved_posts_are_touched(env):
    p, _ = env
    prov = FakeProvider()
    cp.publish_due(p, NOW, prov)
    assert _post(p, "2026-10-14", "obs").get("auto") is None                     # the observe-only client is untouched
    from hypogeum.client_post_approvals import set_status
    set_status("me", "2026-10-20", "skipped", p)
    assert cp.publish_due(p, NOW + timedelta(days=6), prov) == []                # a skipped post is never sent
    from hypogeum.dashboard_clients import update_plan_post
    update_plan_post("me", "2026-10-15", status="draft", db_path=p)              # back to draft: not approved any more
    assert cp.publish_due(p, NOW + timedelta(days=1), prov) == []


def test_a_blocked_post_says_why_and_is_retried_after_the_blocker_is_fixed(env):
    p, post = env
    prov = FakeProvider(integrations=[])
    r = cp.publish_due(p, NOW, prov)
    assert r[0]["state"] == "blocked" and "not connected in Postiz" in r[0]["issues"][0] and prov.sent == []
    assert _post(p, "2026-10-14")["auto"]["instagram"]["state"] == "blocked"
    ok = FakeProvider()
    assert cp.publish_due(p, NOW, ok)[0]["state"] == "scheduled"                  # blocked is not final: it is re-checked


def test_an_uncertain_send_is_never_repeated_and_a_rejection_is_reported(env):
    p, _ = env
    assert cp.publish_due(p, NOW, FakeProvider(publish=pz.PostizAmbiguousClientPostError("timeout after sending")))[0]["state"] == "ambiguous"
    healthy = FakeProvider()
    assert cp.publish_due(p, NOW, healthy) == [] and healthy.sent == []           # left alone, however often the loop runs
    r = cp.publish_due(p, NOW + timedelta(days=1), FakeProvider(publish=pz.PostizPublishRejectedError("HTTP 400")))
    assert r[0]["state"] == "failed" and "400" in r[0]["error"]
    assert _post(p, "2026-10-15")["auto"]["instagram"]["state"] == "failed"
    assert cp.publish_due(p, NOW + timedelta(days=1), healthy) == []              # failed is final until the operator clears it


def test_reconcile_marks_published_and_finishes_instagram_only_posts(env):
    p, _ = env
    prov = FakeProvider()
    cp.publish_due(p, NOW, prov)
    cp.publish_due(p, NOW + timedelta(days=1), prov)
    prov.states = {"pz1": {"state": "PUBLISHED", "release_url": "https://instagram.com/p/abc"}, "pz2": {"state": "ERROR"}}
    out = {x["date"]: x["state"] for x in cp.reconcile(p, NOW + timedelta(days=1, hours=1), prov)}
    assert out == {"2026-10-14": "published", "2026-10-15": "failed"}
    both = _post(p, "2026-10-14")                                                  # Instagram + LinkedIn: LinkedIn is still manual
    assert both["status"] == "approved" and both["auto"]["instagram"]["release_url"].endswith("abc")
    assert _post(p, "2026-10-15")["auto"]["instagram"]["state"] == "failed"


def test_the_alert_check_reports_blocked_failed_and_uncertain_posts(env):
    p, _ = env
    cp.publish_due(p, NOW, FakeProvider(integrations=[]))
    keys = oa.check_client_publishing(p, NOW)
    assert set(keys) == {"clientmedia:me:2026-10-14"} and "Postiz" in keys["clientmedia:me:2026-10-14"][1]
    from hypogeum import alert_recipients as ar
    assert ar.alert_type_of("clientmedia:me:x") == "approvals" and ar.alert_type_of("clientpost:me:x") == "posts"
    assert "obs" not in "".join(keys)


# --- uploads --------------------------------------------------------------------------------------------

def test_uploads_are_checked_by_extension_signature_and_size(tmp_path):
    ok = cmed.save_post_media("me", "2026-10-14", "../../evil name.JPG", io.BytesIO(JPG), root=tmp_path)
    assert ok["kind"] == "image" and Path(ok["path"]).parent == tmp_path / "me" and ".." not in Path(ok["path"]).name and Path(ok["path"]).read_bytes() == JPG
    assert cmed.save_post_media("me", "2026-10-14", "clip.mp4", io.BytesIO(MP4), root=tmp_path)["kind"] == "video"
    for name, data, why in (("x.exe", b"MZ", "JPG"), ("x.jpg", MP4, "do not match"), ("x.jpg", b"", "empty")):
        with pytest.raises(cmed.MediaRejected, match=why):
            cmed.save_post_media("me", "2026-10-14", name, io.BytesIO(data), root=tmp_path)
    big = io.BytesIO(b"\xff\xd8\xff" + b"0" * (cmed.MAX_IMAGE + 10))
    with pytest.raises(cmed.MediaRejected, match="too large"):
        cmed.save_post_media("me", "2026-10-14", "big.jpg", big, root=tmp_path)
    assert not [f for f in (tmp_path / "me").iterdir() if f.stat().st_size > cmed.MAX_IMAGE]    # a rejected upload leaves nothing behind
    with pytest.raises(cmed.MediaRejected):
        cmed.save_post_media("../me", "2026-10-14", "a.jpg", io.BytesIO(JPG), root=tmp_path)


# --- the Postiz call itself -----------------------------------------------------------------------------

class _T:
    def __init__(self, status=201, body=None, exc=None):
        self.status, self.body, self.exc, self.calls = status, body, exc, []

    def post(self, *, url, headers, json_body, timeout):
        self.calls.append((url, json_body))
        if self.exc:
            raise self.exc
        return pz.PostizTransportResponse(status_code=self.status, json_body=self.body)


def _provider(t, monkeypatch):
    monkeypatch.setattr(pz, "_resolve_postiz_credential", lambda *a, **k: "key")
    return pz.PostizProvider(transport=t, base_url="https://pz/api/public/v1")


def test_publish_client_post_builds_the_body_and_separates_certain_from_uncertain_failures(monkeypatch):
    when = datetime(2026, 10, 14, 17, 0, tzinfo=timezone.utc)
    kw = dict(integration_id="int1", platform="instagram", content="Hi\nthere", media=[{"id": "m", "path": "p"}], scheduled_for=when)
    t = _T(201, [{"integration": "int1", "postId": "P9"}])
    assert _provider(t, monkeypatch).publish_client_post(identifier="instagram-standalone", **kw)["post_id"] == "P9"
    url, body = t.calls[0]
    assert url.endswith("/posts") and body["type"] == "schedule" and body["posts"][0]["settings"]["__type"] == "instagram-standalone"
    assert body["posts"][0]["value"][0]["image"] == [{"id": "m", "path": "p"}] and "<p>Hi</p><p>there</p>" == body["posts"][0]["value"][0]["content"]
    with pytest.raises(pz.PostizPublishRejectedError):
        _provider(_T(400, {"message": "bad"}), monkeypatch).publish_client_post(**kw)
    for bad in (_T(500, {}), _T(429, {}), _T(201, [{"nothing": 1}]), _T(exc=pz.PostizTransportError("reset"))):
        with pytest.raises(pz.PostizAmbiguousClientPostError):
            _provider(bad, monkeypatch).publish_client_post(**kw)
    with pytest.raises(pz.PostizScheduleValidationError):
        _provider(_T(), monkeypatch).publish_client_post(**dict(kw, scheduled_for=datetime(2026, 10, 14, 17, 0)))


# --- the plan page: attaching material --------------------------------------------------------------------

def test_the_plan_page_attaches_material_and_clears_a_blocked_state(env, tmp_path, monkeypatch):
    import run_dashboard as server
    p, _ = env
    monkeypatch.setattr(cmed, "_root", lambda root=None: tmp_path / "media")
    old = server.app.config.get("DB_PATH")
    server.app.config["DB_PATH"] = str(p)
    try:
        c = server.app.test_client()
        cp._store(p, "me", "2026-10-14", lambda x: cp._set_auto(x, "instagram", state="blocked", issues=["no material attached"]))
        r = c.post("/client/me/plan/media", data={"date": "2026-10-14", "file": (io.BytesIO(MP4), "reel.mp4")},
                   content_type="multipart/form-data")
        assert r.status_code == 302 and "Material+attached" in r.headers["Location"] or "Material%20attached" in r.headers["Location"] or "Material attached" in r.headers["Location"]
        post = _post(p, "2026-10-14")
        assert post["media"]["kind"] == "video" and Path(post["media"]["path"]).is_file() and post.get("auto") is None   # blocker cleared
        bad = c.post("/client/me/plan/media", data={"date": "2026-10-14", "file": (io.BytesIO(b"nope"), "x.txt")}, content_type="multipart/form-data")
        assert "JPG" in bad.headers["Location"] and _post(p, "2026-10-14")["media"]["kind"] == "video"           # a rejected file changes nothing
        assert c.post("/client/ghost/plan/media", data={"date": "2026-10-14"}).status_code == 404
        page = c.get("/client/me/plan?scope=client:me").get_data(as_text=True)
        assert "Attach material" in page and "reel.mp4" in page
    finally:
        server.app.config["DB_PATH"] = old


def test_one_bad_post_does_not_stop_the_others_and_a_repeated_block_does_not_rewrite(env, monkeypatch):
    p, _ = env
    from hypogeum.dashboard_clients import calendars_of
    cp._store(p, "me", "2026-10-14", lambda x: x.update(time="not-a-time"))
    prov = FakeProvider()
    r = cp.publish_due(p, NOW + timedelta(days=1), prov)
    assert [x["date"] for x in r] == ["2026-10-15"] and _post(p, "2026-10-14")["auto"]["instagram"]["state"] == "blocked"
    stamp = _post(p, "2026-10-14")["auto"]["instagram"]["updated_at"]
    cp.publish_due(p, NOW + timedelta(days=1), prov)
    assert _post(p, "2026-10-14")["auto"]["instagram"]["updated_at"] == stamp                      # same reason: not rewritten
    blocked = FakeProvider(integrations=[])
    cp._store(p, "me", "2026-10-20", lambda x: x.update(status="approved"))
    cp.publish_due(p, NOW + timedelta(days=6), blocked)
    first = _post(p, "2026-10-20")["auto"]["instagram"]["updated_at"]
    cp.publish_due(p, NOW + timedelta(days=6), blocked)
    assert _post(p, "2026-10-20")["auto"]["instagram"]["updated_at"] == first
