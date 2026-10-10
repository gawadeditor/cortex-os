"""
hypogeum/dashboard_workspaces.py — the /workspaces dashboard page and its
alert-connection endpoints (multi-tenant plan, Phase 4). A Flask Blueprint
registered by run_dashboard.py.

    GET  /workspaces                                  page
    POST /workspaces/<id>/alerts/link                 -> {ok, url, expires_at, recipient_count}
    GET  /workspaces/<id>/alerts/recipients           -> {count, recipients}
    POST /alerts/recipients/<rid>/test                -> {ok}
    POST /alerts/recipients/<rid>/disconnect          -> {ok}
    POST /alerts/recipients/<rid>/types  {types:[…]}  -> {ok}   (alert checklist)
    POST /workspaces/<id>/resume                      redirect back
    POST /workspaces/<id>/approvals/settings          approval-before-publish settings
    POST /approvals/<aid>/decide  {decision}          -> {ok, status}
    GET  /approvals/<aid>/cover                       the episode's cover image
    POST /workspaces/bot  {token}                     switch the platform bot (verified first)

Operator-only today (the dashboard has no login yet — Phase 6); the page is
how the operator connects each client's Telegram in the managed-service
model: create the link here, send it to the client, they press Start.
"""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, jsonify, redirect, render_template

bp = Blueprint("workspaces", __name__)


def _db():
    p = current_app.config.get("DB_PATH")
    return Path(p) if p else None


KEY_PROVIDERS = ("openrouter", "muapi", "elevenlabs", "perplexity", "anthropic", "zernio", "postiz")


def _keys_of(workspace_id: str, db) -> list[dict]:
    """The workspace's provider keys: provider, status, date — never a value."""
    from hypogeum.credential_manager import list_secret_profiles
    from hypogeum.workspaces import resources_of
    try:
        owned = set(resources_of(workspace_id, "secret_profile", db_path=db))
        return [{"provider": p.provider, "status": p.status, "updated_at": (p.updated_at or "")[:10]}
                for p in list_secret_profiles(db_path=db)
                if p.secret_profile_id in owned and p.status == "active"]
    except Exception:  # noqa: BLE001
        return []


def _all_channels(db) -> list[str]:
    from hypogeum.db import get_connection
    try:
        conn = get_connection(db)
        try:
            return [r[0] for r in conn.execute(
                "SELECT DISTINCT channel_id FROM schedules WHERE channel_id IS NOT NULL ORDER BY 1")]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []


def _portal_users(workspace_id: str, db) -> list[dict]:
    try:
        from hypogeum import portal_auth as pa
        conn = pa._conn(db)
        try:
            return [dict(r) for r in conn.execute(
                "SELECT email, role, status, notify_email FROM portal_users WHERE workspace_id = ? ORDER BY created_at",
                (workspace_id,))]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []


def _workspace_rows(db) -> list[dict]:
    from hypogeum import alert_recipients as ar
    from hypogeum import publish_approvals as pa
    from hypogeum import workspace_controls as wsc
    from hypogeum.workspaces import list_workspaces, resources_of
    paused = wsc.paused_workspaces(db)
    out = []
    for w in list_workspaces(db):
        wid = w["workspace_id"]
        st = wsc.budget_status(wid, db)
        out.append({
            **w,
            "channels": resources_of(wid, "channel", db_path=db),
            "spend": st["month_spend_usd"], "ceiling": st["ceiling_usd"], "over": st["over"],
            "paused": wid in paused or (w["kind"] == "platform_owner" and wsc.operator_production_paused(db)),
            "pause_reason": paused.get(wid, ""),
            "recipients": [{**r, "types": ar._types(r)} for r in ar.recipients_for(wid, db)],
            "approval_settings": pa.get_settings("workspace", wid, db) or dict(pa.DEFAULTS),
            "approvals": pa.list_for(wid, statuses=("pending",), db_path=db),
            "production_start": wsc.production_start(wid, db),
            "provider_keys": _keys_of(wid, db),
            "portal_users": _portal_users(wid, db),
        })
    out.sort(key=lambda r: (r["kind"] != "platform_owner", r["name"].lower()))
    return out


@bp.route("/workspaces")
def workspaces_page():
    from hypogeum import alert_recipients as ar
    db = _db()
    try:
        ar.ensure_operator_recipient(db)   # the .env operator chat gets a checklist row too
    except Exception:  # noqa: BLE001
        pass
    try:
        bot = ar.bot_username()
    except Exception:  # noqa: BLE001 — Telegram unreachable: page still renders
        bot = None
    from flask import request
    msg = request.args.get("bot_msg")
    return render_template("workspaces.html", workspaces=_workspace_rows(db), bot=bot,
                           alert_types=ar.ALERT_TYPES, scope="factory",
                           all_channels=_all_channels(db), key_providers=KEY_PROVIDERS,
                           bot_switch_msg=msg, bot_switch_ok=request.args.get("bot_ok") == "1")


@bp.route("/workspaces/<workspace_id>/alerts/link", methods=["POST"])
def workspace_alert_link(workspace_id):
    from hypogeum import alert_recipients as ar
    db = _db()
    try:
        link = ar.create_link(workspace_id, db_path=db)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Telegram unavailable: {exc}"[:200]}), 502
    if not link.get("url"):
        return jsonify({"ok": False, "error": "The Telegram bot isn't connected on this machine yet."}), 409
    return jsonify({"ok": True, "url": link["url"], "expires_at": link["expires_at"],
                    "recipient_count": len(ar.recipients_for(workspace_id, db))})


@bp.route("/workspaces/<workspace_id>/alerts/recipients")
def workspace_alert_recipients(workspace_id):
    from hypogeum import alert_recipients as ar
    rows = ar.recipients_for(workspace_id, _db())
    return jsonify({"count": len(rows),
                    "recipients": [{"recipient_id": r["recipient_id"], "label": r["label"],
                                    "created_at": r["created_at"]} for r in rows]})


@bp.route("/alerts/recipients/<recipient_id>/test", methods=["POST"])
def alert_recipient_test(recipient_id):
    from hypogeum import alert_recipients as ar
    return jsonify({"ok": ar.send_test(recipient_id, _db())})


@bp.route("/alerts/recipients/<recipient_id>/types", methods=["POST"])
def alert_recipient_types(recipient_id):
    from flask import request
    from hypogeum import alert_recipients as ar
    types = (request.get_json(silent=True) or {}).get("types") or []
    ar.set_alert_types(recipient_id, list(types), _db())
    return jsonify({"ok": True})


@bp.route("/alerts/recipients/<recipient_id>/disconnect", methods=["POST"])
def alert_recipient_disconnect(recipient_id):
    from hypogeum import alert_recipients as ar
    return jsonify({"ok": ar.disconnect(recipient_id, _db())})


@bp.route("/workspaces/<workspace_id>/resume", methods=["POST"])
def workspace_resume(workspace_id):
    """Resume one workspace's production after its spend breaker tripped.
    The platform owner's resume also clears the global switch (same as the
    queue page's Resume); a client's never touches anyone else."""
    from hypogeum.muapi_spend_breaker import clear_breaker
    from hypogeum.workspaces import platform_workspace_id
    db = _db()
    if workspace_id == platform_workspace_id(db):
        clear_breaker(db_path=db)
    else:
        clear_breaker(db_path=db, workspace_id=workspace_id)
    return redirect("/workspaces?scope=factory")


# ── Operator management (2026-10-05): until now creating a workspace,
# moving a channel, setting the monthly ceiling or adding a client's key
# needed scripts/workspaces.py in a terminal. The operator does it here; the
# client-facing login/onboarding stays Phases 6-7.

@bp.route("/workspaces/new", methods=["POST"])
def workspace_create():
    from flask import request
    from hypogeum.workspaces import WorkspaceError, create_workspace
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name is required"}), 400
    try:
        w = create_workspace(name, language=request.form.get("language") or "en", db_path=_db())
    except (WorkspaceError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return redirect(f"/workspaces?scope=factory#ws-{w['workspace_id']}-title")


@bp.route("/workspaces/<workspace_id>/channels", methods=["POST"])
def workspace_assign_channel(workspace_id):
    from flask import request
    from hypogeum.workspaces import assign
    ch = (request.form.get("channel_id") or "").strip()
    if not ch:
        return jsonify({"ok": False, "error": "channel_id is required"}), 400
    try:
        assign("channel", ch, workspace_id, move=True, db_path=_db())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400
    return redirect(f"/workspaces?scope=factory#ws-{workspace_id}-title")


@bp.route("/workspaces/<workspace_id>/ceiling", methods=["POST"])
def workspace_ceiling(workspace_id):
    from flask import request
    from hypogeum.factory_entity import update_factory_policies
    try:
        value = float(request.form.get("ceiling") or 0)
        if value < 0:
            raise ValueError("ceiling can't be negative")
        update_factory_policies(workspace_id, {"monthly_ceiling_usd": value},
                                reason="monthly ceiling set from the Workspaces page", db_path=_db())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400
    return redirect(f"/workspaces?scope=factory#ws-{workspace_id}-title")


@bp.route("/workspaces/<workspace_id>/keys", methods=["POST"])
def workspace_set_key(workspace_id):
    """Store (or replace) a provider key for the workspace — encrypted at
    rest, never shown again. Same logic as scripts/workspaces.py set-key."""
    from flask import request
    from hypogeum.credential_manager import (create_secret_profile, list_secret_profiles,
                                             revoke_secret_profile)
    from hypogeum.workspaces import list_workspaces, resources_of
    provider = (request.form.get("provider") or "").strip()
    key = (request.form.get("key") or "").strip()
    if provider not in KEY_PROVIDERS:
        return jsonify({"ok": False, "error": "unknown provider"}), 400
    if not key or " " in key or len(key) < 16:
        return jsonify({"ok": False, "error": "that doesn't look like an API key"}), 400
    db = _db()
    if workspace_id not in {w["workspace_id"] for w in list_workspaces(db)}:
        return jsonify({"ok": False, "error": "unknown workspace"}), 404
    owned = set(resources_of(workspace_id, "secret_profile", db_path=db))
    for p in list_secret_profiles(provider=provider, db_path=db):
        if p.secret_profile_id in owned and p.status == "active":
            revoke_secret_profile(p.secret_profile_id, db_path=db)
    create_secret_profile(provider, f"{workspace_id} {provider}", "api_key", key,
                          scope="factory", workspace_id=workspace_id, db_path=db)
    del key
    return redirect(f"/workspaces?scope=factory#ws-{workspace_id}-title")


@bp.route("/workspaces/<workspace_id>/invite", methods=["POST"])
def workspace_invite(workspace_id):
    """Let someone into this workspace's client portal and email them a welcome + sign-in link (valid 7 days)."""
    from flask import request
    from hypogeum import portal_auth as pa
    from hypogeum.config import Config
    from hypogeum.workspaces import list_workspaces
    db = _db()
    names = {w["workspace_id"]: w["name"] for w in list_workspaces(db)}
    if workspace_id not in names:
        return jsonify({"ok": False, "error": "unknown workspace"}), 404
    try:
        res = pa.invite_and_send(request.form.get("email", ""), workspace_id, request.form.get("role") or "owner",
                                 db, Config(), names[workspace_id])
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    flag = "sent" if res["emailed"] else "failed"
    return redirect(f"/workspaces?scope=factory&invite={flag}#ws-{workspace_id}-title")


@bp.route("/workspaces/<workspace_id>/users/disable", methods=["POST"])
def workspace_user_disable(workspace_id):
    from flask import request
    from hypogeum import portal_auth as pa
    email = request.form.get("email", "")
    users = {u["email"] for u in _portal_users(workspace_id, _db())}
    if email.strip().lower() not in users:
        return jsonify({"ok": False, "error": "unknown user"}), 404
    pa.disable(email, _db())
    return redirect(f"/workspaces?scope=factory#ws-{workspace_id}-title")


@bp.route("/workspaces/<workspace_id>/production-start", methods=["POST"])
def workspace_production_start(workspace_id):
    from flask import request
    from hypogeum import workspace_controls as wsc
    try:
        wsc.set_production_start(workspace_id, request.form.get("start", ""), db_path=_db())
    except (ValueError, TypeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return redirect(f"/workspaces?scope=factory#ws-{workspace_id}-title")


@bp.route("/workspaces/<workspace_id>/approvals/settings", methods=["POST"])
def workspace_approval_settings(workspace_id):
    from flask import request
    from hypogeum import publish_approvals as pa
    f = request.form
    try:
        pa.set_settings("workspace", workspace_id, required=f.get("required") == "on",
                        on_timeout=f.get("on_timeout", "hold"),
                        timeout_hours=float(f.get("timeout_hours") or 24), db_path=_db())
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return redirect(f"/workspaces?scope=factory#ws-{workspace_id}-title")


@bp.route("/approvals/<approval_id>/decide", methods=["POST"])
def approval_decide(approval_id):
    from flask import request
    from hypogeum import publish_approvals as pa
    decision = (request.get_json(silent=True) or {}).get("decision") or request.form.get("decision")
    if decision not in ("approved", "rejected"):
        return jsonify({"ok": False, "error": "decision must be approved or rejected"}), 400
    ok = pa.decide(approval_id, decision, "dashboard:operator", db_path=_db())
    return jsonify({"ok": ok, "status": (pa.get(approval_id, _db()) or {}).get("status")})


@bp.route("/approvals/<approval_id>/cover")
def approval_cover(approval_id):
    from flask import abort, send_file
    from hypogeum import publish_approvals as pa
    row = pa.get(approval_id, _db())
    path = Path(row["cover_path"]) if row and row.get("cover_path") else None
    if not path or not path.is_file():
        abort(404)
    return send_file(path)


@bp.route("/workspaces/bot", methods=["POST"])
def workspace_change_bot():
    """Switch the platform's Telegram bot: verify the new token with
    Telegram (getMe) BEFORE saving anything, write it to .env (the same place
    scripts/connect_telegram.py writes), then restart the dashboard so the
    alert sender and the bot loop use it. The operator's chat id is kept —
    they press Start on the new bot so it may message them."""
    import json
    import os
    import re
    import subprocess
    import threading
    import urllib.parse
    import urllib.request
    from flask import request
    token = (request.form.get("token") or "").strip()
    back = "/workspaces?scope=factory&bot_msg="
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", token):
        return redirect(back + urllib.parse.quote("That doesn't look like a bot token (123456789:AA…)."))
    try:
        me = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=15))["result"]
    except Exception:  # noqa: BLE001
        return redirect(back + urllib.parse.quote("Telegram rejected this token — nothing was changed."))
    env = Path(__file__).resolve().parent.parent / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    out, seen = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == "TELEGRAM_BOT_TOKEN":
            out.append(f"TELEGRAM_BOT_TOKEN={token}")
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append(f"TELEGRAM_BOT_TOKEN={token}")
    env.write_text("\n".join(out) + "\n")
    os.chmod(env, 0o600)
    from hypogeum.db import set_worker_control
    set_worker_control("telegram_update_offset", "0", db_path=_db())   # new bot, new update sequence

    def _restart():
        import time
        time.sleep(2)
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.hypogeum.dashboard"],
                       capture_output=True)
    threading.Thread(target=_restart, daemon=True).start()
    msg = (f"Switched to @{me['username']}. Open https://t.me/{me['username']} and press Start "
           "so it can message you (the dashboard restarts in a few seconds).")
    return redirect(back + urllib.parse.quote(msg) + "&bot_ok=1")
