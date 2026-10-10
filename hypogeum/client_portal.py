"""
hypogeum/client_portal.py — the client-facing dashboard (multi-tenant plan, Phase 6/7).

A SEPARATE small Flask app, not the 267-route operator dashboard: it has only the handful of pages a client
needs, so "default deny" holds by construction -- there is no route for logs, runs, other workspaces or
platform pages to forget to filter. It binds 127.0.0.1 (run_client_portal.py); a tunnel exposes THIS port only.

It is rendered with the operator dashboard's OWN design system: the same `_base_style.html`, icon set, hero
macro and stat/card/table classes (templates under hypogeum/templates/portal/), so the two look like one product.

Pages (all behind a session; a wrong channel/episode is a 404, same as one that does not exist):
    /login  /auth/<token>  POST /logout          sign in with an emailed one-time link
    /                       overview: channels, month spend vs ceiling, posts, what needs attention
    /channel/<id>           the channel's posts (cover, status, schedule, platforms) + its authorization
    /usage  /usage.csv      the spending statement (hypogeum/portal_usage.py)
    /welcome …              first-run setup (hypogeum/portal_setup.py)
    /episode/<id>/thumbnail an episode's designed cover, only for the workspace's own episodes
    POST /notify            email-alert opt out/in
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum import portal_auth as pa

_COOKIE = "cortex_portal"
_TEMPLATES = str(Path(__file__).resolve().parent / "templates")
_PROVIDER_LABELS = {"openrouter": "OpenRouter", "muapi": "MUAPI", "elevenlabs": "ElevenLabs", "perplexity": "Perplexity",
                    "voder": "Music (local)", "anthropic": "Anthropic"}
_MSG = {"verified": "connected and verified.", "saved": "key saved. It will be checked the first time it is used."}


def create_app(db_path=None, config=None):
    from flask import (Flask, abort, g, make_response, redirect, render_template, request,  # noqa: PLC0415
                       send_file)

    app = Flask(__name__, template_folder=_TEMPLATES)
    secure_cookie = bool(getattr(config, "PORTAL_COOKIE_SECURE", False))
    dbp = Path(db_path) if db_path else None

    # ---- plumbing ------------------------------------------------------------------------------------------
    @app.before_request
    def _guards():
        g.nonce = secrets.token_urlsafe(16)
        if request.method == "POST":                       # the cookie is SameSite=Lax; this is the second lock
            site = request.headers.get("Sec-Fetch-Site")
            origin = request.headers.get("Origin")
            if site is not None:                           # sent by every modern browser: the authoritative answer
                bad = site not in ("same-origin", "none")
            elif origin:
                oh = origin.split("://", 1)[-1].split("/", 1)[0]
                base = (getattr(config, "PORTAL_BASE_URL", None) or "").split("://", 1)[-1].split("/", 1)[0]
                bad = oh not in ({request.host, request.headers.get("X-Forwarded-Host", ""), base} - {""})
            else:
                bad = False
            if bad:
                from hypogeum.logger import get_logger  # noqa: PLC0415
                get_logger(agent="client_portal").warn("portal_cross_origin_refused", site=site, origin=origin, host=request.host, path=request.path)
                abort(403)

    @app.after_request
    def _headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"      # "no-referrer" makes browsers send Origin: null on our own forms
        resp.headers["Cache-Control"] = "no-store, no-transform"   # no-transform: Cloudflare must not rewrite emails or inject scripts
        resp.headers["Content-Security-Policy"] = (
            f"default-src 'none'; img-src 'self' https://media.zernio.com data:; style-src 'unsafe-inline'; "
            f"script-src 'nonce-{getattr(g, 'nonce', '')}'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        return resp

    @app.errorhandler(Exception)
    def _friendly(exc):
        from werkzeug.exceptions import HTTPException  # noqa: PLC0415
        if isinstance(exc, HTTPException):
            return exc
        import sqlite3  # noqa: PLC0415
        from hypogeum.logger import get_logger  # noqa: PLC0415
        busy = isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc)
        get_logger(agent="client_portal").warn("portal_request_failed", path=request.path, error=type(exc).__name__, busy=busy)
        return render_template("portal/message.html", title="Please try again", heading="One moment",
                               text=("Cortex is busy for a few seconds. Please try again." if busy else
                                     "Something went wrong on our side. Please try again in a minute."),
                               link_text="Back", link_href="/"), (503 if busy else 500)

    @app.context_processor
    def _nonce():
        return {"nonce": getattr(g, "nonce", "")}

    def current_user() -> Optional[dict]:
        return pa.resolve_session(request.cookies.get(_COOKIE, ""), dbp)

    def need_user():
        u = current_user()
        if u is None:
            abort(redirect("/login"))
        return u

    def conn():
        from hypogeum.db import get_connection  # noqa: PLC0415
        return get_connection(dbp)

    def channel_names() -> dict:
        c = conn()
        try:
            return {r[0]: r[1] for r in c.execute("SELECT channel_id, name FROM channel_profiles")}
        except Exception:  # noqa: BLE001
            return {}
        finally:
            c.close()

    def workspace_name(ws_id) -> str:
        from hypogeum import workspaces as ws  # noqa: PLC0415
        return next((w["name"] for w in ws.list_workspaces(dbp) if w["workspace_id"] == ws_id), ws_id)

    def shell(user, active, title, crumb=None) -> dict:
        """Everything the layout (rail + topbar) needs."""
        from hypogeum import portal_setup as ps, workspaces as ws  # noqa: PLC0415
        names = channel_names()
        chans = ws.resources_of(user["workspace_id"], "channel", db_path=dbp)
        try:
            left = sum(1 for x in ps.steps(user["workspace_id"], dbp) if not x["done"])
        except Exception:  # noqa: BLE001
            left = 0
        return {"user": user, "active": active, "title": title, "crumb": crumb, "workspace_name": workspace_name(user["workspace_id"]),
                "nav_channels": [{"channel_id": c, "name": names.get(c) or c} for c in chans],
                "setup_left": left if user["role"] == "owner" else 0}

    def own_channel(user, channel_id) -> str:
        from hypogeum import workspaces as ws  # noqa: PLC0415
        if ws.workspace_of(channel_id, dbp) != user["workspace_id"]:
            abort(404)                                  # not yours looks exactly like not existing
        return channel_id

    def archived_dir(channel_id, episode_id) -> Optional[Path]:
        try:
            from hypogeum.archive_storage import resolve_archive_root  # noqa: PLC0415
            base = resolve_archive_root(dbp) / channel_id
            for pattern in (f"{episode_id} - *", episode_id):
                for d in sorted(base.glob(pattern)):
                    if d.is_dir():
                        return d
        except Exception:  # noqa: BLE001
            return None
        return None

    def channel_of_episode(episode_id) -> Optional[str]:
        c = conn()
        try:
            r = c.execute("SELECT channel_id FROM runs WHERE episode_id = ? AND channel_id IS NOT NULL "
                          "ORDER BY started_at DESC LIMIT 1", (episode_id,)).fetchone()
        finally:
            c.close()
        return r["channel_id"] if r else None

    # ---- sign in -------------------------------------------------------------------------------------------
    @app.get("/login")
    def login_form():
        return render_template("portal/login.html", title="Sign in")

    @app.post("/login")
    def login_submit():
        email = request.form.get("email", "")
        token = pa.request_login_link(email, dbp)
        if token:
            base = (getattr(config, "PORTAL_BASE_URL", None) or request.host_url).rstrip("/")
            pa.deliver_link(email.strip().lower(), f"{base}/auth/{token}", config)
        return render_template("portal/message.html", title="Check your email", heading="Check your email",
                               text="If that address is registered, a sign-in link is on its way. It works once and expires in 15 minutes.")

    @app.get("/auth/<token>")
    def auth(token):
        session = pa.consume_login_link(token, dbp)
        if session is None:
            return render_template("portal/message.html", title="Link expired", heading="This link has expired",
                                   text="Sign-in links work once and only for a short time.", link_text="Get a new link", link_href="/login"), 400
        resp = make_response(redirect("/"))
        resp.set_cookie(_COOKIE, session, max_age=int(pa.SESSION_TTL.total_seconds()), httponly=True,
                        secure=secure_cookie, samesite="Lax")
        return resp

    @app.post("/logout")
    def logout():
        pa.logout(request.cookies.get(_COOKIE, ""), dbp)
        resp = make_response(redirect("/login"))
        resp.delete_cookie(_COOKIE)
        return resp

    @app.post("/notify")
    def notify():
        user = need_user()
        pa.set_notify(user["user_id"], request.form.get("on") == "1", dbp)
        return redirect("/")

    # ---- overview ------------------------------------------------------------------------------------------
    @app.get("/")
    def home():
        user = need_user()
        from hypogeum import client_grants as cg, portal_setup as ps, workspace_controls as wc, workspaces as ws  # noqa: PLC0415
        wid = user["workspace_id"]
        names = channel_names()
        chans = ws.resources_of(wid, "channel", db_path=dbp)
        b = wc.budget_status(wid, dbp)
        c = conn()
        try:
            en = next((w.get("language") == "en" for w in ws.list_workspaces(dbp) if w["workspace_id"] == wid), True)
            alerts = []
            for r in c.execute("SELECT title, body, title_en, body_en FROM operator_alerts WHERE status = 'open' AND workspace_id = ? "
                               "ORDER BY opened_at DESC LIMIT 20", (wid,)):
                use_en = en and r["title_en"]                  # an English workspace sees the English text, not the operator's Arabic
                alerts.append({"title": r["title_en"] if use_en else r["title"], "detail": (r["body_en"] if use_en else r["body"]) or ""})
        except Exception:  # noqa: BLE001
            alerts = []
        posts, counts = [], {"published": 0, "scheduled": 0}
        if chans:
            marks = ",".join("?" * len(chans))
            try:
                rows = c.execute(f"SELECT channel_id, title, status, scheduled_for, published_at FROM content_grid_cache "
                                 f"WHERE channel_id IN ({marks}) ORDER BY COALESCE(published_at, scheduled_for) DESC", chans).fetchall()
            except Exception:  # noqa: BLE001
                rows = []
            for r in rows:
                if r["status"] in counts:
                    counts[r["status"]] += 1
            posts = [{"title": r["title"] or "(untitled)", "channel": names.get(r["channel_id"]) or r["channel_id"], "status": r["status"] or "",
                      "when": ((r["published_at"] or r["scheduled_for"] or "")[:16].replace("T", " ") + " UTC") if (r["published_at"] or r["scheduled_for"]) else ""}
                     for r in rows[:8]]
        c.close()
        sts = ps.steps(wid, dbp)
        left = sum(1 for x in sts if not x["done"])
        paused = wc.is_paused(wid, dbp)
        try:
            notify_on = bool(pa.workspace_emails(wid, dbp).count(user["email"]))
        except Exception:  # noqa: BLE001
            notify_on = True
        pct = 0 if not b["ceiling_usd"] else min(100, round(100 * b["month_spend_usd"] / b["ceiling_usd"]))
        return render_template("portal/home.html", **shell(user, "home", "Overview"), paused=paused, spend=b["month_spend_usd"],
                               ceiling=b["ceiling_usd"], pct=pct, counts=counts, posts=posts, alerts=alerts, notify=notify_on,
                               setup_total=len(sts),
                               channels=[{"channel_id": ch, "name": names.get(ch) or ch, "auth": cg.grant_state(ch, dbp)} for ch in chans])

    # ---- one channel ---------------------------------------------------------------------------------------
    @app.get("/channel/<channel_id>")
    def channel(channel_id):
        user = need_user()
        own_channel(user, channel_id)
        from hypogeum import client_grants as cg, portal_usage as pu  # noqa: PLC0415
        from hypogeum.dashboard_content_grid import get_content_grid  # noqa: PLC0415
        try:
            raw = get_content_grid(channel_id, dbp)
        except Exception:  # noqa: BLE001
            raw = []
        items = []
        for it in raw:
            thumb = it.get("thumbnail_url") or ""
            if thumb and not (thumb.startswith("/episode/") or thumb.startswith("https://media.zernio.com/")):
                thumb = ""
            when = (it.get("published_at") or it.get("scheduled_for") or "")[:16].replace("T", " ")
            items.append({"title": it.get("display_title") or "(untitled)", "status": str(it.get("status") or ""), "when": when, "thumb": thumb,
                          "needs_attention": bool(it.get("needs_attention")), "is_landscape": bool(it.get("is_landscape")),
                          "platforms": [{"platform": p.get("platform"), "status": p.get("status")} for p in (it.get("platforms") or [])]})
        c = conn()
        try:
            r = c.execute("SELECT name, description FROM channel_profiles WHERE channel_id = ?", (channel_id,)).fetchone()
        except Exception:  # noqa: BLE001
            r = None
        finally:
            c.close()
        name = (r["name"] if r and r["name"] else channel_id)
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        spend = next((x["total"] for x in pu.usage(user["workspace_id"], month, dbp)["by_channel"] if x["channel_id"] == channel_id), 0.0)
        return render_template("portal/channel.html", **shell(user, f"channel:{channel_id}", name, name), name=name,
                               description=(r["description"] if r else None), items=items, spend=spend,
                               published=sum(1 for i in items if i["status"] == "published"),
                               scheduled=sum(1 for i in items if i["status"] == "scheduled"), auth=cg.grant_state(channel_id, dbp))

    @app.get("/episode/<episode_id>/thumbnail")
    def thumbnail(episode_id):
        user = need_user()
        cid = channel_of_episode(episode_id)
        if not cid:
            abort(404)
        own_channel(user, cid)
        from hypogeum.episode_artifacts import safe_episode_asset_path  # noqa: PLC0415
        archived = archived_dir(cid, episode_id)
        for stem in ("main_episode", "main"):
            for ext in ("jpg", "jpeg", "png", "webp"):
                p = safe_episode_asset_path(episode_id, "thumbnail", f"{stem}.{ext}")
                if not p and archived is not None:               # the working copy moves to the archive after publishing
                    cand = (archived / "thumbnails" / f"{stem}.{ext}")
                    try:
                        cand = cand.resolve()
                        cand.relative_to(archived.resolve())
                        if cand.is_file() and cand.stat().st_size > 0:
                            p = cand
                    except (OSError, ValueError):
                        p = None
                if p:
                    return send_file(str(p), mimetype="image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}")
        abort(404)

    # ---- usage ---------------------------------------------------------------------------------------------
    @app.get("/usage")
    def usage_page():
        user = need_user()
        from hypogeum import portal_usage as pu  # noqa: PLC0415
        month = pu.valid_month(request.args.get("month")) or datetime.now(timezone.utc).strftime("%Y-%m")
        u = pu.usage(user["workspace_id"], month, dbp)
        ms = [{**m, "label": datetime.strptime(m["month"], "%Y-%m").strftime("%b %Y")} for m in pu.months(user["workspace_id"], dbp)]
        top = lambda rows: max([r["total"] for r in rows] + [0.01])  # noqa: E731
        return render_template("portal/usage.html", **shell(user, "usage", "Usage", "Usage"), u=u, month=month, months=ms,
                               label=datetime.strptime(month, "%Y-%m").strftime("%B %Y"), names=channel_names(), labels=_PROVIDER_LABELS,
                               pct=(0 if not u["ceiling"] else min(100, round(100 * u["total"] / u["ceiling"]))), peak=top(u["daily"]),
                               ptop=top(u["by_provider"]), ctop=top(u["by_channel"]))

    @app.get("/usage.csv")
    def usage_csv():
        user = need_user()
        from hypogeum import portal_usage as pu  # noqa: PLC0415
        month = pu.valid_month(request.args.get("month")) or datetime.now(timezone.utc).strftime("%Y-%m")
        import csv, io  # noqa: PLC0415
        buf = io.StringIO()
        w = csv.writer(buf)
        for row in pu.csv_rows(user["workspace_id"], month, dbp):
            w.writerow([("'" + c) if isinstance(c, str) and c[:1] in ("=", "+", "-", "@") else c for c in row])   # no spreadsheet formulas
        resp = make_response(buf.getvalue())
        resp.headers["Content-Type"] = "text/csv; charset=utf-8"
        resp.headers["Content-Disposition"] = f"attachment; filename=cortex-usage-{month}.csv"
        return resp

    # ---- setup ---------------------------------------------------------------------------------------------
    def render_welcome(user, k=None, r=None, tg_link=None):
        from hypogeum import portal_setup as ps  # noqa: PLC0415
        sts = ps.steps(user["workspace_id"], dbp)
        msg = None
        if r == "telegram_failed":
            msg = {"ok": False, "text": "Could not create the Telegram link right now. Please try again in a minute."}
        elif k in ps.PROVIDERS and r in _MSG:
            msg = {"ok": True, "text": f"{ps.PROVIDERS[k]['label']} {_MSG[r]}"}
        elif k in ps.PROVIDERS and r == "invalid":
            msg = {"ok": False, "text": f"{ps.PROVIDERS[k]['label']} rejected that key. Check it and try again."}
        elif r == "telegram_failed":
            msg = {"ok": False, "text": "Could not create the Telegram link right now. Please try again in a minute."}
        elif r == "limit":
            msg = {"ok": False, "text": "Too many attempts. Please try again in an hour."}
        return render_template("portal/welcome.html", **shell(user, "setup", "Setup", "Setup"), steps=sts, done=sum(1 for x in sts if x["done"]),
                               first_open=next((x["n"] for x in sts if not x["done"]), None), owner=user["role"] == "owner",
                               keys=ps.key_status(user["workspace_id"], dbp), providers=ps.PROVIDERS, msg=msg, tg_link=tg_link)

    @app.get("/welcome")
    def welcome():
        return render_welcome(need_user(), request.args.get("k"), request.args.get("r"))

    @app.post("/welcome/keys/<provider>")
    def welcome_key(provider):
        user = need_user()
        if user["role"] != "owner":
            abort(403)
        from hypogeum import portal_setup as ps  # noqa: PLC0415
        if provider not in ps.PROVIDERS:
            abort(404)
        res = ps.save_key(user["workspace_id"], provider, request.form.get("key", ""), dbp, actor=user["email"])
        code = res["status"] if res["ok"] else ("limit" if "Too many" in res["message"] else "invalid")
        return redirect(f"/welcome?k={provider}&r={code}")

    @app.post("/welcome/telegram")
    def welcome_telegram():
        user = need_user()
        if user["role"] != "owner":
            abort(403)
        from hypogeum import alert_recipients as ar  # noqa: PLC0415
        try:
            link = ar.create_link(user["workspace_id"], db_path=dbp).get("url")
        except Exception:  # noqa: BLE001
            link = None
        if not link:
            return render_welcome(user, k="telegram", r="telegram_failed")
        return render_welcome(user, tg_link=link)

    return app
