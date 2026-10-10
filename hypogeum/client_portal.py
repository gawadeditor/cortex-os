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
:root{--bg:#090b11;--bg-2:#0c0f16;--surface:#14171d;--raised:#1b1f27;--border:#262b33;--border-strong:#363d47;
--text:#f4f6f9;--muted:#a4aebd;--faint:#6b7481;--accent:#f0a838;--accent-ink:#1a1206;--accent-bg:rgba(240,168,56,.13);
--accent-line:rgba(240,168,56,.45);--good:#4ec26f;--good-bg:rgba(78,194,111,.12);--warn:#e5a93a;--warn-bg:rgba(229,169,58,.13);
--critical:#f0574a;--critical-bg:rgba(240,87,74,.12);--radius:13px;--radius-sm:8px;--gap:20px;
--font:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI","Helvetica Neue",Arial,sans-serif;
--mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;--ease:cubic-bezier(.16,.84,.28,1)}
*{box-sizing:border-box}html{color-scheme:dark}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 var(--font);min-height:100vh;
background-image:radial-gradient(1100px 520px at 72% -30%,rgba(240,168,56,.14),transparent 62%)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
::selection{background:var(--accent-line);color:#fff}
:focus-visible{outline:none;box-shadow:0 0 0 2px var(--bg),0 0 0 4px var(--accent);border-radius:6px}
header{border-bottom:1px solid var(--border);background:rgba(9,11,17,.8);backdrop-filter:blur(8px);position:sticky;top:0;z-index:5}
header .bar{max-width:1080px;margin:0 auto;padding:12px 20px;display:flex;gap:14px;align-items:center}
.brand{font-weight:800;font-size:15px;letter-spacing:.07em;text-transform:uppercase;color:var(--text);display:inline-flex;gap:9px;align-items:center;margin-right:auto}
.brand:hover{text-decoration:none}.brand:before{content:"";width:9px;height:9px;border-radius:2px;background:var(--accent);box-shadow:0 0 12px var(--accent-line)}
.who{color:var(--muted);font-size:13px}
main{max-width:1080px;margin:0 auto;padding:36px 20px 90px}
h1{font-size:30px;letter-spacing:-.03em;margin:0 0 6px;line-height:1.15}h2{font-size:12px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);margin:34px 0 12px;font-weight:700}
.sub{color:var(--muted);margin:0 0 4px}.crumb{font-size:13px;color:var(--muted);margin-bottom:14px;display:inline-block}
.grid{display:grid;gap:var(--gap);grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}
.panel{background:linear-gradient(180deg,rgba(255,255,255,.03),transparent 40%),var(--raised);border:1px solid var(--border);border-radius:var(--radius);padding:20px;
box-shadow:0 8px 24px -6px rgba(0,0,0,.55),0 2px 6px rgba(0,0,0,.4)}
.chan{display:block;color:var(--text);transition:border-color .2s var(--ease),transform .2s var(--ease)}
.chan:hover{text-decoration:none;border-color:var(--accent-line);transform:translateY(-2px)}
.chan .name{font-size:17px;font-weight:700;letter-spacing:-.01em}.chan .go{font-size:12px;color:var(--accent);margin-top:10px;display:block;font-weight:600}
.kpi{font-size:34px;font-weight:700;letter-spacing:-.03em;font-variant-numeric:tabular-nums}.kpi small{font-size:15px;color:var(--muted);font-weight:500;letter-spacing:0}
.bar-track{height:8px;background:var(--border);border-radius:99px;overflow:hidden;margin-top:14px}
.bar-track i{display:block;height:100%;background:linear-gradient(90deg,var(--accent-line),var(--accent));border-radius:99px}
.alert{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--critical);background:var(--critical-bg);color:#ffd7d3;border-radius:var(--radius-sm);padding:11px 14px;margin:8px 0;font-size:14px}
.alert:before{content:"";flex:none;width:8px;height:8px;border-radius:99px;background:var(--critical);margin-top:7px;box-shadow:0 0 8px var(--critical)}
.ok{border:1px solid var(--border);border-radius:var(--radius-sm);padding:11px 14px;color:var(--muted);font-size:14px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:var(--gap);margin-top:8px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;display:flex;flex-direction:column;transition:border-color .2s var(--ease)}
.card:hover{border-color:var(--border-strong)}.card.attn{border-color:var(--critical);border-width:2px}
.poster{aspect-ratio:9/16;background:var(--bg);display:flex;align-items:center;justify-content:center;color:var(--faint);font-size:12px}
.poster img{width:100%;height:100%;object-fit:contain;display:block}
.cbody{padding:12px 13px 13px;display:flex;flex-direction:column;gap:8px;flex:1}
.title{font-size:13px;font-weight:600;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.when{font-size:11px;color:var(--faint)}
.pill{align-self:flex-start;font-size:10px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;padding:2px 8px;border-radius:999px;background:var(--border);color:var(--muted)}
.pill.published{background:var(--good-bg);color:var(--good)}.pill.scheduled{background:var(--accent-bg);color:var(--accent)}.pill.bad{background:var(--critical-bg);color:var(--critical)}
.plats{display:flex;flex-wrap:wrap;gap:5px;margin-top:auto;padding-top:4px}
.plat{font-size:10px;font-weight:600;padding:2px 7px;border-radius:5px;border:1px solid var(--border);color:var(--muted)}
.plat.on{color:var(--good);border-color:var(--good)}
form.inline{display:inline;margin:0}
input[type=email]{font:inherit;padding:11px 14px;border-radius:var(--radius-sm);border:1px solid var(--border-strong);background:var(--surface);color:var(--text);min-width:260px}
input[type=email]:focus{border-color:var(--accent)}
.btn{font:inherit;font-weight:600;padding:10px 16px;border-radius:var(--radius-sm);border:1px solid var(--accent);background:var(--accent);color:var(--accent-ink);cursor:pointer;transition:transform .15s var(--ease),box-shadow .15s}
.btn:hover{transform:translateY(-1px);box-shadow:0 8px 24px -8px rgba(240,168,56,.55)}
.btn.ghost{background:transparent;color:var(--muted);border-color:var(--border-strong);font-size:13px;padding:6px 12px}.btn.ghost:hover{color:var(--text);box-shadow:none}
.center{max-width:460px;margin:9vh auto 0}
@media(max-width:600px){main{padding:24px 16px 70px}h1{font-size:25px}input[type=email]{min-width:0;width:100%;margin-bottom:10px}.cards{grid-template-columns:repeat(2,1fr)}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
"""

_BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} · Cortex</title><style>{{ css|safe }}</style></head><body>
<header><div class="bar"><a class="brand" href="/">Cortex</a>{% if user %}<span class="who">{{ user.email }}</span>
<form class="inline" method="post" action="/logout"><button class="btn ghost" type="submit">Sign out</button></form>{% endif %}</div></header>
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

    def auth_line(channel_id) -> str:
        from hypogeum.client_grants import grant_state  # noqa: PLC0415
        g = grant_state(channel_id, db_path)
        if not g["active"]:
            return "<span class='pill'>Waiting for spending authorization</span>"
        lim = (" · up to $" + _esc(g["weekly_ceiling_usd"]) + "/week") if g.get("weekly_ceiling_usd") else ""
        return "<span class='pill published'>Production authorized</span> <span class='when'>" + str(g["days_left"]) + " days left" + lim + "</span>"

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
        return page("Sign in", "<div class='center panel'><h1>Sign in</h1><p class='sub'>Enter your email and we will send you a one-time link.</p>"
                               "<form method='post' action='/login' style='margin-top:18px'><input type='email' name='email' required autofocus "
                               "placeholder='you@example.com'> <button class='btn' type='submit'>Send link</button></form></div>")

    @app.post("/login")
    def login_submit():
        email = request.form.get("email", "")
        token = pa.request_login_link(email, db_path)
        if token:
            base = (getattr(config, "PORTAL_BASE_URL", None) or request.host_url).rstrip("/")
            pa.deliver_link(email.strip().lower(), f"{base}/auth/{token}", config)
        return page("Check your email", "<div class='center panel'><h1>Check your email</h1><p class='sub'>If that address is registered, a sign-in link is on its way. "
                                         "It works once and expires in 15 minutes.</p></div>")

    @app.get("/auth/<token>")
    def auth(token):
        session = pa.consume_login_link(token, db_path)
        if session is None:
            return page("Link expired", "<div class='center panel'><h1>This link has expired</h1><p class='sub'><a href='/login'>Get a new one</a></p></div>", status=400)
        resp = make_response(redirect("/"))
        resp.set_cookie(_COOKIE, session, max_age=int(pa.SESSION_TTL.total_seconds()), httponly=True,
                        secure=secure_cookie, samesite="Lax")
        return resp

    @app.post("/notify")
    def notify():
        user = need_user()
        pa.set_notify(user["user_id"], request.form.get("on") == "1", db_path)
        return redirect("/")

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
        names = {}
        try:
            for r in get_connection(Path(db_path) if db_path else None).execute("SELECT channel_id, name FROM channel_profiles"):
                names[r[0]] = r[1]
        except Exception:  # noqa: BLE001
            pass
        body = "<h1>Your channels</h1><p class='sub'>Everything we are producing and publishing for you.</p><h2>Channels</h2>"
        chans = "".join("<a class='panel chan' href='/channel/" + _esc(c) + "'><span class='name'>" + _esc(names.get(c) or c) +
                        "</span><div style='margin-top:8px'>" + auth_line(c) + "</div><span class='go'>View posts →</span></a>" for c in channels)
        body += "<div class='grid'>" + (chans or "<div class='ok'>No channels yet.</div>") + "</div>"
        body += "<h2>This month</h2><div class='panel'><div class='kpi'>$" + format(b["month_spend_usd"], ".2f")
        if b["ceiling_usd"]:
            body += " <small>of $" + format(b["ceiling_usd"], ".2f") + "</small></div><div class='bar-track'><i style='width:" + str(pct) + "%'></i></div>"
        else:
            body += " <small>spent</small></div>"
        body += "</div><h2>Needs attention</h2>"
        body += "".join("<div class='alert'>" + _esc(a["title"]) + "</div>" for a in alerts) or "<div class='ok'>Nothing needs your attention.</div>"
        conn2 = get_connection(Path(db_path) if db_path else None)
        try:
            on = conn2.execute("SELECT notify_email FROM portal_users WHERE user_id = ?", (user["user_id"],)).fetchone()
        finally:
            conn2.close()
        on = bool(on and on[0])
        body += ("<h2>Notifications</h2><div class='panel'><form method='post' action='/notify' class='inline'>"
                 "<span class='sub'>Email me about problems: <b>" + ("on" if on else "off") + "</b></span> "
                 "<input type='hidden' name='on' value='" + ("0" if on else "1") + "'> "
                 "<button class='btn ghost' type='submit'>Turn " + ("off" if on else "on") + "</button></form></div>")
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
            st = str(it.get("status") or "")
            pill = "published" if st == "published" else ("scheduled" if st == "scheduled" else ("bad" if it.get("needs_attention") else ""))
            img = '<img src="' + _esc(thumb) + '" alt="" loading="lazy">' if thumb else "no cover"
            plats = "".join("<span class='plat" + (" on" if pl.get("status") == "published" else "") + "'>" + _esc(pl.get("platform")) + "</span>"
                            for pl in (it.get("platforms") or []))
            cards += ("<div class='card" + (" attn" if it.get("needs_attention") else "") + "'><div class='poster'>" + img + "</div>"
                      "<div class='cbody'><div class='title'>" + _esc(it.get("display_title")) + "</div>"
                      "<span class='pill " + pill + "'>" + _esc(st) + "</span><div class='when'>" + _esc(when) + " UTC</div>"
                      "<div class='plats'>" + plats + "</div></div></div>")
        cname = channel_id
        try:
            from hypogeum.db import get_connection  # noqa: PLC0415
            r = get_connection(Path(db_path) if db_path else None).execute(
                "SELECT name FROM channel_profiles WHERE channel_id = ?", (channel_id,)).fetchone()
            cname = (r[0] if r and r[0] else channel_id)
        except Exception:  # noqa: BLE001
            pass
        return page(cname, "<a class='crumb' href='/'>← All channels</a><h1>" + _esc(cname) + "</h1><p class='sub'>Your posts and where they stand.</p><p>" + auth_line(channel_id) + "</p>"
                           "<div class='cards'>" + (cards or "<div class='ok'>No posts yet.</div>") + "</div>", user)

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
