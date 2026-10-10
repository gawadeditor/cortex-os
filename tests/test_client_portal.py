"""tests/test_client_portal.py — Phase 6: who gets in, and that tenants never see each other."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import hypogeum.db as db
from hypogeum import portal_auth as pa
from hypogeum import workspaces as ws
from hypogeum.client_portal import create_app


@pytest.fixture()
def env(tmp_path):
    p = tmp_path / "portal.db"
    db.initialize_schema(p)
    ws.create_workspace("Platform", workspace_id="plat", kind="platform_owner", db_path=p)
    ws.create_workspace("Acme", workspace_id="acme", db_path=p)
    ws.create_workspace("Beta", workspace_id="beta", db_path=p)
    ws.assign("channel", "acme_ch", "acme", db_path=p)
    ws.assign("channel", "beta_ch", "beta", db_path=p)
    ws.assign("channel", "plat_ch", "plat", db_path=p)
    pa.invite("a@acme.com", "acme", db_path=p)
    pa.invite("b@beta.com", "beta", db_path=p)
    return p


def _login(app, p, email):
    tok = pa.request_login_link(email, p)
    c = app.test_client()
    r = c.get(f"/auth/{tok}")
    assert r.status_code == 302
    return c


def test_everything_needs_a_session(env):
    c = create_app(env).test_client()
    for url in ("/", "/channel/acme_ch", "/episode/ep1/thumbnail"):
        r = c.get(url)
        assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert c.get("/login").status_code == 200


def test_a_link_works_once_expires_and_unknown_emails_get_nothing(env):
    tok = pa.request_login_link("a@acme.com", env)
    assert pa.consume_login_link(tok, env) is not None
    assert pa.consume_login_link(tok, env) is None                                   # single use
    old = pa.request_login_link("a@acme.com", env, now=datetime.now(timezone.utc) - timedelta(minutes=16))
    assert pa.consume_login_link(old, env) is None                                   # expired
    assert pa.request_login_link("nobody@x.com", env) is None and pa.request_login_link("not an email", env) is None
    app = create_app(env)
    known = app.test_client().post("/login", data={"email": "a@acme.com"})
    unknown = app.test_client().post("/login", data={"email": "ghost@x.com"})
    assert known.status_code == unknown.status_code == 200 and known.data == unknown.data   # no enumeration


def test_login_requests_are_rate_limited(env):
    got = [pa.request_login_link("a@acme.com", env) for _ in range(pa.MAX_LINKS_PER_HOUR + 2)]
    assert sum(1 for t in got if t) == pa.MAX_LINKS_PER_HOUR


def test_a_client_sees_only_their_own_channels_and_others_are_404(env):
    app = create_app(env)
    a, b = _login(app, env, "a@acme.com"), _login(app, env, "b@beta.com")
    home_a = a.get("/").get_data(as_text=True)
    assert "acme_ch" in home_a and "beta_ch" not in home_a and "plat_ch" not in home_a
    assert "beta_ch" in b.get("/").get_data(as_text=True)
    assert a.get("/channel/beta_ch").status_code == 404 and a.get("/channel/plat_ch").status_code == 404
    assert a.get("/channel/does_not_exist").status_code == 404                      # same as "not yours"
    assert b.get("/channel/acme_ch").status_code == 404


def test_episode_cover_is_scoped_to_the_workspace(env):
    conn = db.get_connection(env)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO runs (run_id, episode_id, started_at, status, channel_id) VALUES ('r1','ep_beta','2026-10-10','completed','beta_ch')")
    conn.commit()
    conn.close()
    app = create_app(env)
    assert _login(app, env, "a@acme.com").get("/episode/ep_beta/thumbnail").status_code == 404
    assert _login(app, env, "a@acme.com").get("/episode/unknown/thumbnail").status_code == 404


def test_logout_disable_and_session_expiry_end_access(env):
    app = create_app(env)
    c = _login(app, env, "a@acme.com")
    assert c.get("/").status_code == 200
    c.post("/logout")
    assert c.get("/").status_code == 302
    c2 = _login(app, env, "a@acme.com")
    pa.disable("a@acme.com", env)
    assert c2.get("/").status_code == 302                                           # disabled: session is gone
    assert pa.request_login_link("a@acme.com", env) is None
    sess = pa.consume_login_link(pa.request_login_link("b@beta.com", env), env)
    assert pa.resolve_session(sess, env, now=datetime.now(timezone.utc) + timedelta(days=8)) is None


def test_one_email_cannot_belong_to_two_workspaces(env):
    with pytest.raises(ValueError):
        pa.invite("a@acme.com", "beta", db_path=env)


def test_the_portal_has_no_operator_routes():
    app = create_app(None)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert rules <= {"/login", "/auth/<token>", "/logout", "/", "/channel/<channel_id>", "/episode/<episode_id>/thumbnail",
                     "/static/<path:filename>", "/notify"}


def test_deliver_link_uses_smtp_only_when_configured_and_never_raises(monkeypatch):
    from types import SimpleNamespace as NS
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=0): sent.append(("connect", host, port))
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): sent.append(("tls",))
        def login(self, u, p): sent.append(("login", u))
        def send_message(self, m): sent.append(("msg", m["To"], m["From"], "/auth/tok" in m.get_body(preferencelist=("plain",)).get_content()))

    monkeypatch.setattr(pa.smtplib, "SMTP", FakeSMTP)
    assert pa.deliver_link("a@acme.com", "https://x/auth/tok", NS(PORTAL_SMTP_HOST=None)) is False and sent == []
    cfg = NS(PORTAL_SMTP_HOST="smtp.resend.com", PORTAL_SMTP_PORT=587, PORTAL_SMTP_USER="resend",
             PORTAL_SMTP_PASSWORD="k", PORTAL_SMTP_FROM="Cortex <login@cortexchannels.com>")
    assert pa.deliver_link("a@acme.com", "https://x/auth/tok", cfg) is True
    assert ("login", "resend") in sent and ("msg", "a@acme.com", "Cortex <login@cortexchannels.com>", True) in sent
    monkeypatch.setattr(pa.smtplib, "SMTP", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert pa.deliver_link("a@acme.com", "https://x/auth/tok", cfg) is False


# --- Phase 7: Operating Grants for client channels (the platform operator signs) -----------------------

def _fake_state(active, days=60, weekly="45.00"):
    return {"active": active, "days_left": days if active else 0, "weekly_ceiling_usd": weekly if active else None,
            "expires_at": "2027-01-01T00:00:00+00:00" if active else None}


def test_the_portal_tells_the_client_whether_production_is_authorized(env, monkeypatch):
    from hypogeum import client_grants as cg
    app = create_app(env)
    c = _login(app, env, "a@acme.com")
    monkeypatch.setattr(cg, "grant_state", lambda ch, db=None, now=None: _fake_state(False))
    assert "Waiting for spending authorization" in c.get("/").get_data(as_text=True)
    monkeypatch.setattr(cg, "grant_state", lambda ch, db=None, now=None: _fake_state(True))
    html = c.get("/").get_data(as_text=True)
    assert "Production authorized" in html and "60 days left" in html and "$45.00/week" in html


def test_signing_preflight_blocks_a_client_channel_with_no_keys_and_ownerless_channels(env, monkeypatch):
    from hypogeum import client_grants as cg
    pf = cg.signing_preflight("acme_ch", env)
    assert pf["client"] and any("no key for" in b for b in pf["blockers"])
    assert cg.signing_preflight("plat_ch", env) == {"blockers": [], "warnings": [], "workspace_id": "plat", "client": False}
    ownerless = cg.signing_preflight("nobody_ch", env)
    assert ownerless["blockers"] and ownerless["workspace_id"] is None
    from hypogeum import workspace_keys as wk
    monkeypatch.setattr(wk, "workspace_key", lambda provider, ws_id, db_path=None: "k")
    ok = cg.signing_preflight("acme_ch", env)
    assert ok["blockers"] == [] and ok["client"]


def test_only_client_channels_without_a_grant_need_one_and_it_reaches_the_operator(env, monkeypatch):
    from hypogeum import client_grants as cg
    from hypogeum import operator_alerts as oa
    monkeypatch.setattr(cg, "grant_state", lambda ch, db=None, now=None: _fake_state(False))
    rows = cg.channels_needing_grant(env)
    assert {r["channel_id"] for r in rows} == {"acme_ch", "beta_ch"}                 # never the platform's own channel
    alerts = oa.check_client_grants(env, datetime.now(timezone.utc))
    assert set(alerts) == {"clientgrant:acme_ch", "clientgrant:beta_ch"}
    monkeypatch.setattr(cg, "grant_state", lambda ch, db=None, now=None: _fake_state(True, days=10))
    assert {r["reason"] for r in cg.channels_needing_grant(env)} == {"grant expires in 10 days"}
    monkeypatch.setattr(cg, "grant_state", lambda ch, db=None, now=None: _fake_state(True, days=60))
    assert cg.channels_needing_grant(env) == [] and oa.check_client_grants(env, datetime.now(timezone.utc)) == {}


# --- emails: invitation + client alerts ---------------------------------------------------------------

class _Mail:
    def __init__(self, monkeypatch):
        self.sent = []
        outer = self

        class SMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): pass
            def login(self, *a): pass
            def send_message(self, m): outer.sent.append((m["To"], m["Subject"], m.get_body(preferencelist=("plain",)).get_content(), m.get_body(preferencelist=("html",)).get_content()))
        monkeypatch.setattr(pa.smtplib, "SMTP", SMTP)


CFG_MAIL = type("C", (), {"PORTAL_SMTP_HOST": "smtp.x", "PORTAL_SMTP_PORT": 587, "PORTAL_SMTP_USER": "u",
                          "PORTAL_SMTP_PASSWORD": "p", "PORTAL_SMTP_FROM": "Cortex <l@mail.x>",
                          "PORTAL_BASE_URL": "https://app.example.com"})()


def test_invite_emails_a_week_long_single_use_link(env, monkeypatch):
    mail = _Mail(monkeypatch)
    res = pa.invite_and_send("new@acme.com", "acme", "owner", env, CFG_MAIL, "Acme")
    assert res["emailed"] and res["link"].startswith("https://app.example.com/auth/")
    to, subject, body, html = mail.sent[0]
    assert to == "new@acme.com" and "Acme" in subject and res["link"] in body and res["link"] in html and "#f0a838" in html
    token = res["link"].rsplit("/", 1)[1]
    assert pa.consume_login_link(token, env, now=datetime.now(timezone.utc) + timedelta(days=6)) is not None   # still valid on day 6
    assert pa.consume_login_link(token, env) is None                                                          # single use
    off = pa.invite_and_send("other@acme.com", "acme", "owner", env, type("C", (), {"PORTAL_SMTP_HOST": None})(), "Acme")
    assert off["emailed"] is False and off["link"]                                                            # link still handed back


def test_client_alerts_are_emailed_to_the_workspace_only_unless_opted_out(env, monkeypatch):
    mail = _Mail(monkeypatch)
    assert pa.send_workspace_alert_email("acme", "🔴 Production stopped\nbecause X", env, CFG_MAIL) is True
    assert [m[0] for m in mail.sent] == ["a@acme.com"] and mail.sent[0][1] == "Production stopped"     # never beta's user
    uid = pa.resolve_session(pa.consume_login_link(pa.request_login_link("a@acme.com", env), env), env)["user_id"]
    pa.set_notify(uid, False, env)
    mail.sent.clear()
    assert pa.send_workspace_alert_email("acme", "🔴 again", env, CFG_MAIL) is False and mail.sent == []
    assert pa.send_workspace_alert_email("plat", "🔴 operator thing", env, CFG_MAIL) is False               # platform owner: Telegram only


def test_the_portal_lets_a_user_turn_alert_emails_off_and_on(env):
    app = create_app(env)
    c = _login(app, env, "a@acme.com")
    assert "Email me about problems: <b>on</b>" in c.get("/").get_data(as_text=True)
    c.post("/notify", data={"on": "0"})
    assert "Email me about problems: <b>off</b>" in c.get("/").get_data(as_text=True)
    assert pa.workspace_emails("acme", env) == []
    c.post("/notify", data={"on": "1"})
    assert pa.workspace_emails("acme", env) == ["a@acme.com"]


def test_email_templates_are_multipart_branded_and_escape_their_input():
    from hypogeum import email_templates as et
    for subject, text, html in (et.sign_in("https://x/auth/t"), et.invitation("<b>Acme</b>", "https://x/auth/t", "https://x"),
                                et.alert("🔴 Title <script>\nbody & more", "https://x")):
        assert subject and text and "#f0a838" in html and "Cortex" in html and "<script>" not in html
    assert "&lt;b&gt;Acme&lt;/b&gt;" in et.invitation("<b>Acme</b>", "https://x/auth/t", "https://x")[2]
    assert "Resolved" in et.alert("✅ Back to normal\nfixed", "https://x")[2]
