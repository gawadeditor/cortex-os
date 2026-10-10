"""
hypogeum/client_portal.py — the client-facing dashboard (multi-tenant plan, Phase 6).

A SEPARATE small Flask app, not the 267-route operator dashboard: it has only the handful of pages a client
needs, so "default deny" holds by construction -- there is no route for logs, runs, other workspaces or
platform pages to forget to filter. It binds 127.0.0.1 (run_client_portal.py); exposing it to the internet
(Phase 7) means pointing a tunnel at THIS port only.

Pages (all behind a session; a wrong channel/episode is a 404, same as one that does not exist):
    /login            email form (the answer never reveals whether the email is known)
    /auth/<token>     one-time link -> session cookie
    /                 the workspace's channels, month spend vs ceiling, open alerts
    /channel/<id>     the channel's posts (title, status, schedule, cover)
    /episode/<id>/thumbnail   the episode's designed cover, only for the workspace's own episodes
    POST /logout
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum import portal_auth as pa

_COOKIE = "cortex_portal"

_CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#5f6368;--line:#e5e5ea;--accent:#3b5bdb;--card:#f7f7f9}
@media(prefers-color-scheme:dark){:root{--bg:#121214;--fg:#f2f2f4;--muted:#a1a1aa;--line:#2a2a2e;--accent:#8da2fb;--card:#1b1b1f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:32px 16px 80px}header{border-bottom:1px solid var(--line)}
header div{max-width:980px;margin:0 auto;padding:14px 16px;display:flex;gap:16px;align-items:center}
header b{margin-right:auto}a{color:var(--accent)}h1{font-size:1.6rem;margin:0 0 4px}.muted{color:var(--muted);font-size:.9rem}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.card img{width:100%;aspect-ratio:9/16;object-fit:cover;background:#000;display:block}.card div{padding:10px 12px;font-size:.9rem}
.badge{display:inline-block;font-size:.75rem;padding:1px 8px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
.alert{border:1px solid #d9480f;border-radius:8px;padding:10px 12px;margin:8px 0}.bar{height:8px;background:var(--line);border-radius:99px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent)}input,button{font:inherit;padding:8px 12px;border-radius:8px;border:1px solid var(--line)}
button{background:var(--accent);color:#fff;border-color:var(--accent);cursor:pointer}form.inline{display:inline}
"""

_BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} · Cortex</title><style>{{ css|safe }}</style></head><body>
<header><div><b>Cortex</b>{% if user %}<span class="muted">{{ user.email }}</span>
<form class="inline" method="post" action="/logout"><button type="submit">Sign out</button></form>{% endif %}</div></header>
<main>{{ body|safe }}</main></body></html>"""


def _esc(s) -> str:
    from markupsafe import escape  # noqa: PLC0415
    return str(escape("" if s is None else s))


def create_app(db_path=None, config=None):
    from flask import Flask, abort, make_response, redirect, render_template_string, request  # noqa: PLC0415

    app = Flask(__name__)
    secure_cookie = bool(getattr(config, "PORTAL_COOKIE_SECURE", False))

    def page(title, body, user=None, status=200):
        return render_template_string(_BASE, title=title, body=body, css=_CSS, user=user), status

    def current_user() -> Optional[dict]:
        return pa.resolve_session(request.cookies.get(_COOKIE, ""), db_path)

    def need_user():
        u = current_user()
        if u is None:
            abort(redirect("/login"))
        return u

    @app.after_request
    def _headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Content-Security-Policy"] = ("default-src 'none'; img-src 'self' https://media.zernio.com data:; "
                                                    "style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
        return resp

    def own_channel(user, channel_id) -> str:
        from hypogeum import workspaces as ws  # noqa: PLC0415
        if ws.workspace_of(channel_id, db_path) != user["workspace_id"]:
            abort(404)                                  # not yours looks exactly like not existing
        return channel_id

    def channel_of_episode(episode_id) -> Optional[str]:
        from hypogeum.db import get_connection  # noqa: PLC0415
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            r = conn.execute("SELECT channel_id FROM runs WHERE episode_id = ? AND channel_id IS NOT NULL "
                             "ORDER BY started_at DESC LIMIT 1", (episode_id,)).fetchone()
        finally:
            conn.close()
        return r["channel_id"] if r else None

    @app.get("/login")
    def login_form():
        return page("Sign in", "<h1>Sign in</h1><p class='muted'>Enter your email and we will send you a one-time link.</p>"
                               "<form method='post' action='/login'><input type='email' name='email' required autofocus "
                               "placeholder='you@example.com'> <button type='submit'>Send link</button></form>")

    @app.post("/login")
    def login_submit():
        email = request.form.get("email", "")
        token = pa.request_login_link(email, db_path)
        if token:
            base = (getattr(config, "PORTAL_BASE_URL", None) or request.host_url).rstrip("/")
            pa.deliver_link(email.strip().lower(), f"{base}/auth/{token}", config)
        return page("Check your email", "<h1>Check your email</h1><p>If that address is registered, a sign-in link is on its way. "
                                         "It works once and expires in 15 minutes.</p>")

    @app.get("/auth/<token>")
    def auth(token):
        session = pa.consume_login_link(token, db_path)
        if session is None:
            return page("Link expired", "<h1>This link has expired</h1><p><a href='/login'>Get a new one</a></p>", status=400)
        resp = make_response(redirect("/"))
        resp.set_cookie(_COOKIE, session, max_age=int(pa.SESSION_TTL.total_seconds()), httponly=True,
                        secure=secure_cookie, samesite="Lax")
        return resp

    @app.post("/logout")
    def logout():
        pa.logout(request.cookies.get(_COOKIE, ""), db_path)
        resp = make_response(redirect("/login"))
        resp.delete_cookie(_COOKIE)
        return resp

    @app.get("/")
    def home():
        user = need_user()
        from hypogeum import workspace_controls as wc, workspaces as ws  # noqa: PLC0415
        from hypogeum.db import get_connection  # noqa: PLC0415
        channels = ws.resources_of(user["workspace_id"], "channel", db_path)
        b = wc.budget_status(user["workspace_id"], db_path)
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            alerts = conn.execute("SELECT title FROM operator_alerts WHERE status = 'open' AND workspace_id = ? "
                                  "ORDER BY opened_at DESC LIMIT 20", (user["workspace_id"],)).fetchall()
        except Exception:  # noqa: BLE001
            alerts = []
        finally:
            conn.close()
        pct = 0 if not b["ceiling_usd"] else min(100, round(100 * b["month_spend_usd"] / b["ceiling_usd"]))
        body = "<h1>Your channels</h1>"
        body += "".join(f"<p><a href='/channel/{_esc(c)}'>{_esc(c)}</a></p>" for c in channels) or "<p class='muted'>No channels yet.</p>"
        body += (f"<h2>This month</h2><p>${b['month_spend_usd']:.2f}" +
                 (f" of ${b['ceiling_usd']:.2f}</p><div class='bar'><i style='width:{pct}%'></i></div>" if b["ceiling_usd"] else " spent</p>"))
        if alerts:
            body += "<h2>Needs attention</h2>" + "".join(f"<div class='alert'>{_esc(a['title'])}</div>" for a in alerts)
        return page("Home", body, user)

    @app.get("/channel/<channel_id>")
    def channel(channel_id):
        user = need_user()
        own_channel(user, channel_id)
        from hypogeum.dashboard_content_grid import get_content_grid  # noqa: PLC0415
        try:
            items = get_content_grid(channel_id, db_path)
        except Exception:  # noqa: BLE001
            items = []
        cards = ""
        for it in items:
            thumb = it.get("thumbnail_url") or ""
            if thumb and not (thumb.startswith("/episode/") or thumb.startswith("https://media.zernio.com/")):
                thumb = ""
            when = (it.get("published_at") or it.get("scheduled_for") or "")[:16].replace("T", " ")
            img = '<img src="' + _esc(thumb) + '" alt="">' if thumb else ""
            cards += ("<div class='card'>" + img + "<div><b>" + _esc(it.get("display_title")) + "</b><br>"
                      "<span class='badge'>" + _esc(it.get("status")) + "</span> "
                      "<span class='muted'>" + _esc(when) + " UTC</span></div></div>")
        return page(channel_id, f"<p><a href='/'>← Home</a></p><h1>{_esc(channel_id)}</h1>"
                                 f"<div class='cards'>{cards or '<p class=muted>No posts yet.</p>'}</div>", user)

    @app.get("/episode/<episode_id>/thumbnail")
    def thumbnail(episode_id):
        user = need_user()
        cid = channel_of_episode(episode_id)
        if not cid:
            abort(404)
        own_channel(user, cid)
        from flask import send_file  # noqa: PLC0415
        from hypogeum.episode_artifacts import safe_episode_asset_path  # noqa: PLC0415
        for stem in ("main_episode", "main"):
            for ext in ("jpg", "jpeg", "png", "webp"):
                p = safe_episode_asset_path(episode_id, "thumbnail", f"{stem}.{ext}")
                if p:
                    return send_file(str(p), mimetype="image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}")
        abort(404)

    return app
