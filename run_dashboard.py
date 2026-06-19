#!/usr/bin/env python3
"""
run_dashboard.py — Hypogeum Factory OS Local Dashboard

Phase A (read-only monitoring):
    GET /           Renders the full dashboard HTML page
    GET /api/state  Returns FactoryState + Director report as JSON

Phase B (operator control surface):
  Read / detail
    GET /api/channel/<channel_id>
    GET /api/brand/<brand_profile_id>
    GET /api/model/<model_profile_id>
    GET /api/publishing/<publishing_profile_id>
    GET /api/schedule/<schedule_id>
    GET /api/secrets          — status-only; credential_data never returned

  Edit (POST)
    POST /api/channel/<channel_id>
    POST /api/brand/<brand_profile_id>
    POST /api/model/<model_profile_id>
    POST /api/publishing/<publishing_profile_id>
    POST /api/schedule/<schedule_id>/status

  Safe actions (POST)
    POST /api/action/approve/<approval_id>
    POST /api/action/reject/<approval_id>
    POST /api/action/resolve-signal/<signal_id>

Usage:
    python3 run_dashboard.py
    python3 run_dashboard.py --port 5002
    python3 run_dashboard.py --host 0.0.0.0

No external CDN. No JavaScript framework.
"""

from __future__ import annotations

import argparse
import dataclasses
import json as _json
import sys
import time
from pathlib import Path
from typing import Iterator

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

from hypogeum.command_state import get_command_state
from hypogeum.decision_inputs import get_decision_inputs_state
from hypogeum.decision_engine import get_decision_state
from hypogeum.decision_reviews import (
    sync_decisions_to_reviews,
    list_decision_reviews,
    get_decision_review,
    accept_decision,
    reject_decision,
)
from hypogeum.decision_execution import (
    create_execution_plan_from_review,
    list_execution_plans,
    get_execution_plan,
    approve_execution_plan,
    reject_execution_plan as reject_execution_plan_fn,
    cancel_execution_plan,
    dry_run_execution_plan,
)
from hypogeum.dashboard_api import get_factory_state
from hypogeum.factory_director import assess_factory
from hypogeum.dashboard_executive import (
    get_executive_overview,
    translate_alert,
    translate_approval_type,
)
from hypogeum.platform_connections import (
    get_platform_connection_state,
    detect_real_vs_mock_analytics,
)
from hypogeum.channel_profile import get_channel_profile, update_channel_profile
from hypogeum.credential_manager import (
    create_secret_profile,
    list_secret_profiles,
    update_secret_profile,
)

_PLATFORM_CRED_TYPE = {
    "youtube":   "oauth2_token",
    "tiktok":    "api_key",
    "instagram": "api_key",
    "facebook":  "api_key",
}
_PLATFORM_DISPLAY = {
    "youtube": "YouTube", "tiktok": "TikTok",
    "instagram": "Instagram", "facebook": "Facebook",
}
_VALID_PLATFORMS_CONNECT = frozenset(_PLATFORM_CRED_TYPE)
from hypogeum.dashboard_control import (
    # reads
    get_channel_detail,
    get_brand_detail,
    get_model_detail,
    get_publishing_detail,
    get_schedule_detail,
    list_secret_status,
    # edits
    edit_channel_profile,
    edit_brand_profile,
    edit_model_profile,
    edit_publishing_profile,
    set_schedule_status,
    # actions
    action_approve,
    action_reject,
    action_resolve_signal,
    enqueue_topic,
)
from hypogeum.analytics_store import (
    seed_mock_analytics,
    import_from_dict,
    import_from_csv,
    list_analytics,
    VALID_PLATFORMS as _VALID_PLATFORMS,
)
from hypogeum.learning_engine import (
    extract_insights,
    generate_actions,
    apply_action,
    dismiss_action,
)
from hypogeum.dashboard_analytics import (
    get_analytics_overview,
    get_platform_summary,
    get_episode_performance,
    get_learning_state,
)
from hypogeum.dashboard_live import (
    get_active_runs,
    get_run_live,
    get_queue_snapshot,
    get_live_metrics,
    get_recent_failures,
    tail_log,
)
from hypogeum.dashboard_runs import (
    list_runs,
    get_run_detail,
    list_episodes,
    list_episode_topics,
    list_approvals_history,
    list_signals_history,
    get_preflight_status,
    fmt_duration,
)
from hypogeum.episode_detail import get_episode_detail

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

_TEMPLATE_DIR = _ROOT / "hypogeum" / "templates"

app = Flask(__name__, template_folder=str(_TEMPLATE_DIR))
app.config["TEMPLATES_AUTO_RELOAD"] = True


@app.template_filter("pct_bar")
def pct_bar_filter(value, width=16):
    """0.0–1.0 → '▓▓▓▓▓░░░░░░░░░░ 31%'"""
    if value is None:
        return "—"
    v = max(0.0, min(1.0, float(value)))
    filled = round(v * width)
    return "▓" * filled + "░" * (width - filled) + f" {v:.0%}"


@app.template_filter("sort_by_signal")
def sort_by_signal_filter(insights):
    """Sort insights by abs(signal_strength) descending — strongest first."""
    return sorted(insights, key=lambda x: abs(x.get("signal_strength") or 0), reverse=True)


@app.template_filter("human_label")
def human_label_filter(value):
    """Convert snake_case insight_type to human-readable label."""
    if not value:
        return value
    _acronyms = {"ctr", "cta", "api", "url"}
    return " ".join(
        w.upper() if w.lower() in _acronyms else w.title()
        for w in str(value).split("_")
    )


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _to_dict(obj) -> object:
    """Recursively convert dataclasses to dicts for JSON serialisation."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_dict(getattr(obj, f.name))
                for f in dataclasses.fields(obj)}
    if isinstance(obj, list):
        return [_to_dict(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj


def _db_path():
    """Return configured DB path override, or None to use default."""
    p = app.config.get("DB_PATH")
    return Path(p) if p else None


def _safe_state():
    """
    Return (state, report, error_msg).
    Never raises — on any failure returns degraded placeholder objects.
    """
    try:
        state  = get_factory_state(db_path=_db_path())
        report = assess_factory()
        return state, report, None
    except Exception as exc:
        return None, None, str(exc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _safe_command():
    """Return command state dict, never raises."""
    try:
        return get_command_state(db_path=_db_path())
    except Exception as exc:
        return {
            "checked_at": "",
            "factory_status": {"level": "UNKNOWN", "reason": str(exc),
                               "director_score": 0, "director_level": "UNKNOWN",
                               "blocking_reasons": [], "warnings": []},
            "providers": [],
            "run_state": {"active_runs": [], "queued_runs": [],
                          "last_completed": None, "recent_failures": [], "blockers": []},
            "episode_readiness": {"can_launch": False, "next_topic": {},
                                  "blockers": [str(exc)], "missing_inputs": [],
                                  "required_approvals": [], "publishing_ready": False,
                                  "pending_approval_count": 0},
            "attention_queue": [],
        }


@app.route("/")
def index():
    return redirect(url_for("legacy_dashboard"), 302)


# ===========================================================================
# F13 — Operator Product Routes
# ===========================================================================

@app.route("/create", methods=["GET"])
def create_page():
    db = _db_path()
    try:
        from hypogeum.dashboard_api import get_channel_summaries
        channels = get_channel_summaries(db_path=db)
    except Exception:
        channels = []
    try:
        preflight = get_preflight_status(db_path=db)
    except Exception:
        preflight = {}
    return render_template("create.html", channels=channels, preflight=preflight)


@app.route("/create/launch", methods=["POST"])
def create_launch():
    channel_id   = request.form.get("channel_id", "").strip()
    topic        = request.form.get("topic", "").strip()
    video_type   = request.form.get("video_type", "long_video")
    quality_mode = request.form.get("quality_mode", "balanced")
    platforms    = request.form.getlist("platforms")
    # Production launch is not yet wired to the pipeline runner.
    # Redirect back with a flash indicating the job was queued.
    if channel_id and topic:
        result = enqueue_topic(channel_id, topic, priority=0, db_path=_db_path())
        code = "ok" if result["ok"] else result.get("code", "error")
        return redirect(url_for("create_page") + f"?launched={code}")
    return redirect(url_for("create_page") + "?launched=invalid")


@app.route("/channels")
def channels_page():
    db = _db_path()
    try:
        from hypogeum.dashboard_api import get_channel_summaries
        channels = get_channel_summaries(db_path=db)
    except Exception:
        channels = []
    return render_template("channels.html", channels=channels)


@app.route("/settings")
def settings_page():
    return redirect(url_for("channel_profile_page"))


_CONTENT_PROVIDERS = [
    {"id": "openrouter", "label": "OpenRouter", "auth": "api_key",
     "description": "LLM model routing"},
    {"id": "elevenlabs", "label": "ElevenLabs", "auth": "api_key",
     "description": "Voice generation"},
    {"id": "muapi",      "label": "MUAPI",      "auth": "api_key",
     "description": "Image & video generation"},
    {"id": "seedance",   "label": "Seedance",   "auth": "api_key",
     "description": "Video generation via MUAPI"},
    {"id": "veo",        "label": "Veo",        "auth": "api_key",
     "description": "Video generation"},
    {"id": "kling",      "label": "Kling",      "auth": "api_key",
     "description": "Video generation"},
    {"id": "runway",     "label": "Runway",     "auth": "api_key",
     "description": "Video generation"},
    {"id": "freepik",    "label": "Freepik",    "auth": "api_key",
     "description": "Image generation"},
]

_PLATFORM_PROVIDERS = [
    {"id": "youtube",   "label": "YouTube",   "auth": "oauth2",
     "description": "Video publishing"},
    {"id": "tiktok",    "label": "TikTok",    "auth": "api_key",
     "description": "Short video publishing"},
    {"id": "instagram", "label": "Instagram", "auth": "api_key",
     "description": "Reels & posts"},
    {"id": "facebook",  "label": "Facebook",  "auth": "api_key",
     "description": "Video posts"},
]


def _build_providers(provider_list, creds_map, cost_map):
    result = []
    for p in provider_list:
        entry = dict(p)
        cred = creds_map.get(p["id"])
        entry["connected"] = cred is not None
        entry["credential_label"] = getattr(cred, "label", None) if cred else None
        entry["cost_usd"] = cost_map.get(p["id"], 0.0)
        if p["id"] == "youtube":
            entry["oauth_client_id"] = None
            entry["oauth_redirect_uri"] = "http://localhost:8080/oauth2callback"
        result.append(entry)
    return result


@app.route("/providers")
def providers_page():
    db = _db_path()
    try:
        all_secrets = list_secret_profiles(db_path=db)
        creds_map = {s.provider: s for s in all_secrets if s.status == "active"}
    except Exception:
        creds_map = {}
    try:
        from hypogeum.dashboard_api import get_cost_summary
        costs = get_cost_summary(db_path=db)
        cost_map = costs.by_provider
    except Exception:
        cost_map = {}
    content_providers  = _build_providers(list(_CONTENT_PROVIDERS),  creds_map, cost_map)
    platform_providers = _build_providers(list(_PLATFORM_PROVIDERS), creds_map, cost_map)
    return render_template(
        "providers.html",
        content_providers=content_providers,
        platform_providers=platform_providers,
    )


@app.route("/command")
def command_page():
    cmd = _safe_command()
    try:
        di = get_decision_inputs_state(db_path=_db_path())
    except Exception:
        di = {"readiness": {"has_real_inputs": False, "missing_inputs": [],
                            "ready_for_decision_engine": False}}
    try:
        ds = get_decision_state(db_path=_db_path())
    except Exception:
        ds = {"engine_status": "not_ready", "summary": "", "decisions": [],
              "insufficient_data": []}
    try:
        pending_reviews = list_decision_reviews(status="pending", db_path=_db_path())
    except Exception:
        pending_reviews = []
    try:
        exec_plans = (
            list_execution_plans(status="draft",    db_path=_db_path()) +
            list_execution_plans(status="approved", db_path=_db_path())
        )
    except Exception:
        exec_plans = []
    return render_template("command.html", cmd=cmd, di=di, ds=ds,
                           pending_reviews=pending_reviews,
                           exec_plans=exec_plans)


@app.route("/api/command-state")
def api_command_state():
    try:
        cmd = get_command_state(db_path=_db_path())
        return jsonify(cmd)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


@app.route("/dashboard")
def legacy_dashboard():
    state, report, err = _safe_state()

    # Build pre-translated alert list (signals + pending approvals)
    exec_alerts = []
    if state:
        for sig in (state.active_signals or []):
            exec_alerts.append({
                "level":        "critical" if sig.get("severity") == "CRITICAL" else "warning",
                "label":        translate_alert(sig.get("signal_type", "")),
                "detail":       sig.get("message", ""),
                "signal_type":  sig.get("signal_type", ""),
                "signal_id":    sig.get("signal_id", ""),
                "approval_id":  None,
                "approval_type": None,
            })
        for approval in (state.pending_approvals or []):
            atype = getattr(approval, "approval_type", "")
            aid   = getattr(approval, "approval_id",   "")
            exec_alerts.append({
                "level":        "warning",
                "label":        translate_approval_type(atype),
                "detail":       f"{atype} — {aid}",
                "signal_type":  None,
                "approval_id":  aid,
                "approval_type": atype,
            })

    # Fetch executive overview (non-blocking; empty dict on any failure)
    try:
        overview = get_executive_overview(db_path=_db_path())
    except Exception:
        overview = {}

    return render_template(
        "exec_dashboard.html",
        state=state,
        report=report,
        error=err,
        exec_alerts=exec_alerts,
        overview=overview,
    )


@app.route("/api/state")
def api_state():
    state, report, err = _safe_state()
    if err or state is None:
        return jsonify({"error": err or "state unavailable"}), 503

    payload = {
        "factory_state":  _to_dict(state),
        "director_report": _to_dict(report),
    }
    return jsonify(payload)


@app.route("/api/platform-connections")
def api_platform_connections():
    """
    Returns platform connection state and real-vs-mock analytics counts.
    No credentials returned — status only.
    """
    try:
        state  = get_platform_connection_state(db_path=_db_path())
        detail = detect_real_vs_mock_analytics(db_path=_db_path())
        return jsonify({"connections": state, "analytics_integrity": detail})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


# ===========================================================================
# F9 — Channel Profile (Control Center)
# ===========================================================================

@app.route("/channel-profile")
def channel_profile_page():
    try:
        profile = get_channel_profile(db_path=_db_path())
    except Exception:
        profile = {}
    flash = request.args.get("saved")
    return render_template("channel_profile.html", profile=profile, flash=flash)


@app.route("/api/channel-profile")
def api_channel_profile():
    try:
        return jsonify(get_channel_profile(db_path=_db_path()))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


@app.route("/api/channel-profile/update", methods=["POST"])
def api_channel_profile_update():
    body = request.get_json(silent=True) or {}
    result = update_channel_profile(body, db_path=_db_path())
    http = 200 if result["ok"] else 400
    return jsonify(result), http


@app.route("/channel-profile/save", methods=["POST"])
def channel_profile_save():
    updates = {k: v for k, v in request.form.items()}
    update_channel_profile(updates, db_path=_db_path())
    return redirect(url_for("channel_profile_page") + "?saved=1")


# ===========================================================================
# F9 — Platform Connection Setup
# ===========================================================================

_PLATFORM_SETUP = {
    "youtube": {
        "label":        "YouTube",
        "cred_label":   "OAuth 2.0 Access Token",
        "instructions": (
            "Go to Google Cloud Console → APIs & Services → Credentials. "
            "Create an OAuth 2.0 client ID, complete the consent flow, "
            "and paste the access token below."
        ),
        "credential_type": "oauth2_token",
        "placeholder": "ya29.A0...",
    },
    "tiktok": {
        "label":        "TikTok",
        "cred_label":   "TikTok API Key",
        "instructions": (
            "Go to TikTok for Developers → Manage Apps → your app → Keys. "
            "Copy your Access Token and paste it below."
        ),
        "credential_type": "api_key",
        "placeholder": "act.abc123...",
    },
    "instagram": {
        "label":        "Instagram",
        "cred_label":   "Instagram Graph API Token",
        "instructions": (
            "Go to Meta for Developers → your app → Instagram → Generate Token. "
            "Paste the long-lived access token below."
        ),
        "credential_type": "api_key",
        "placeholder": "EAAGm...",
    },
    "facebook": {
        "label":        "Facebook",
        "cred_label":   "Facebook Page Access Token",
        "instructions": (
            "Go to Meta for Developers → your app → Tools → Graph API Explorer. "
            "Select your Page and generate a long-lived token. Paste it below."
        ),
        "credential_type": "api_key",
        "placeholder": "EAAGm...",
    },
}


@app.route("/platforms/connect/<platform>")
def platform_connect_page(platform: str):
    if platform not in _VALID_PLATFORMS_CONNECT:
        return render_template("not_found.html", kind="Platform", id=platform), 404
    setup = _PLATFORM_SETUP[platform]
    flash = request.args.get("saved")
    return render_template("connect_platform.html",
                           platform=platform, setup=setup, flash=flash)


@app.route("/api/platforms/connect/<platform>/save", methods=["POST"])
def api_platform_connect_save(platform: str):
    if platform not in _VALID_PLATFORMS_CONNECT:
        return jsonify({"ok": False, "error": "unknown platform", "code": "invalid"}), 400

    body  = request.get_json(silent=True) or {}
    cred  = (body.get("credential") or "").strip()
    label = body.get("label") or f"{_PLATFORM_DISPLAY.get(platform, platform)} credential"

    if not cred:
        return jsonify({"ok": False, "error": "credential is required", "code": "invalid"}), 400

    ctype = _PLATFORM_CRED_TYPE.get(platform, "api_key")
    db    = _db_path()

    try:
        existing = list_secret_profiles(provider=platform, db_path=db)
        active   = [p for p in existing if p.status == "active"]
        if active:
            update_secret_profile(
                active[0].secret_profile_id,
                credential_data=cred,
                label=label,
                db_path=db,
            )
            return jsonify({"ok": True, "action": "updated"})

        create_secret_profile(
            provider=platform,
            label=label,
            credential_type=ctype,
            credential_data=cred,
            db_path=db,
        )
        return jsonify({"ok": True, "action": "created"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "code": "db_error"}), 500


@app.route("/platforms/connect/<platform>/save", methods=["POST"])
def platform_connect_save_form(platform: str):
    if platform not in _VALID_PLATFORMS_CONNECT:
        return render_template("not_found.html", kind="Platform", id=platform), 404
    cred  = request.form.get("credential", "").strip()
    label = request.form.get("label") or f"{_PLATFORM_DISPLAY.get(platform, platform)} credential"
    if cred:
        ctype = _PLATFORM_CRED_TYPE.get(platform, "api_key")
        db    = _db_path()
        try:
            existing = list_secret_profiles(provider=platform, db_path=db)
            active   = [p for p in existing if p.status == "active"]
            if active:
                update_secret_profile(
                    active[0].secret_profile_id,
                    credential_data=cred, label=label, db_path=db,
                )
            else:
                create_secret_profile(
                    provider=platform, label=label,
                    credential_type=ctype, credential_data=cred, db_path=db,
                )
        except Exception:
            pass
    return redirect(url_for("legacy_dashboard"))


# ===========================================================================
# Phase B — Read / detail routes (GET, read-only)
# ===========================================================================

@app.route("/api/channel/<channel_id>", methods=["GET"])
def api_channel_detail(channel_id):
    data = get_channel_detail(channel_id, db_path=_db_path())
    if data is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@app.route("/api/brand/<brand_profile_id>", methods=["GET"])
def api_brand_detail(brand_profile_id):
    data = get_brand_detail(brand_profile_id, db_path=_db_path())
    if data is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@app.route("/api/model/<model_profile_id>", methods=["GET"])
def api_model_detail(model_profile_id):
    data = get_model_detail(model_profile_id, db_path=_db_path())
    if data is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@app.route("/api/publishing/<publishing_profile_id>", methods=["GET"])
def api_publishing_detail(publishing_profile_id):
    data = get_publishing_detail(publishing_profile_id, db_path=_db_path())
    if data is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@app.route("/api/schedule/<schedule_id>", methods=["GET"])
def api_schedule_detail(schedule_id):
    data = get_schedule_detail(schedule_id, db_path=_db_path())
    if data is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@app.route("/api/secrets", methods=["GET"])
def api_secrets():
    """Status-only secret profile list. credential_data is never returned."""
    return jsonify(list_secret_status(db_path=_db_path()))


# ===========================================================================
# Phase B — Edit routes (POST-only)
# ===========================================================================

@app.route("/api/channel/<channel_id>", methods=["POST"])
def api_channel_edit(channel_id):
    updates = request.get_json(silent=True) or {}
    result = edit_channel_profile(channel_id, updates, db_path=_db_path())
    status = 200 if result["ok"] else (404 if result.get("code") == "not_found" else 400)
    return jsonify(result), status


@app.route("/api/brand/<brand_profile_id>", methods=["POST"])
def api_brand_edit(brand_profile_id):
    updates = request.get_json(silent=True) or {}
    result = edit_brand_profile(brand_profile_id, updates, db_path=_db_path())
    status = 200 if result["ok"] else (404 if result.get("code") == "not_found" else 400)
    return jsonify(result), status


@app.route("/api/model/<model_profile_id>", methods=["POST"])
def api_model_edit(model_profile_id):
    updates = request.get_json(silent=True) or {}
    result = edit_model_profile(model_profile_id, updates, db_path=_db_path())
    status = 200 if result["ok"] else (404 if result.get("code") == "not_found" else 400)
    return jsonify(result), status


@app.route("/api/publishing/<publishing_profile_id>", methods=["POST"])
def api_publishing_edit(publishing_profile_id):
    updates = request.get_json(silent=True) or {}
    result = edit_publishing_profile(publishing_profile_id, updates, db_path=_db_path())
    status = 200 if result["ok"] else (404 if result.get("code") == "not_found" else 400)
    return jsonify(result), status


@app.route("/api/schedule/<schedule_id>/status", methods=["POST"])
def api_schedule_status(schedule_id):
    body = request.get_json(silent=True) or {}
    status_val = body.get("status", "")
    result = set_schedule_status(schedule_id, status_val, db_path=_db_path())
    http_status = 200 if result["ok"] else (
        404 if result.get("code") == "not_found" else
        400 if result.get("code") == "invalid" else
        500
    )
    return jsonify(result), http_status


# ===========================================================================
# Phase B — Action routes (POST-only)
# ===========================================================================

@app.route("/api/action/approve/<approval_id>", methods=["POST"])
def api_action_approve(approval_id):
    body    = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "system:dashboard")
    notes   = body.get("notes")
    result  = action_approve(approval_id, user_id=user_id, notes=notes, db_path=_db_path())
    http_status = 200 if result["ok"] else (
        404 if result.get("code") == "not_found" else
        409 if result.get("code") == "already_decided" else
        403 if result.get("code") == "permission_denied" else
        400
    )
    return jsonify(result), http_status


@app.route("/api/action/reject/<approval_id>", methods=["POST"])
def api_action_reject(approval_id):
    body    = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "system:dashboard")
    reason  = body.get("reason")
    result  = action_reject(approval_id, user_id=user_id, reason=reason, db_path=_db_path())
    http_status = 200 if result["ok"] else (
        404 if result.get("code") == "not_found" else
        409 if result.get("code") == "already_decided" else
        403 if result.get("code") == "permission_denied" else
        400
    )
    return jsonify(result), http_status


@app.route("/api/action/resolve-signal/<signal_id>", methods=["POST"])
def api_action_resolve_signal(signal_id):
    body    = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "system:dashboard")
    result  = action_resolve_signal(signal_id, user_id=user_id, db_path=_db_path())
    http_status = 200 if result["ok"] else (
        404 if result.get("code") == "not_found" else 400
    )
    return jsonify(result), http_status


# ===========================================================================
# Phase C — Runs & Episode Operations (JSON API)
# ===========================================================================

@app.route("/api/runs", methods=["GET"])
def api_runs():
    status = request.args.get("status")
    limit  = min(int(request.args.get("limit", 100)), 500)
    return jsonify(list_runs(db_path=_db_path(), status=status or None, limit=limit))


@app.route("/api/run/<run_id>", methods=["GET"])
def api_run_detail(run_id):
    detail = get_run_detail(run_id, db_path=_db_path())
    if detail is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(detail)


@app.route("/api/episodes", methods=["GET"])
def api_episodes():
    limit = min(int(request.args.get("limit", 100)), 500)
    return jsonify(list_episodes(db_path=_db_path(), limit=limit))


@app.route("/api/topics", methods=["GET"])
def api_topics():
    channel_id = request.args.get("channel_id")
    status     = request.args.get("status")
    limit      = min(int(request.args.get("limit", 200)), 500)
    return jsonify(list_episode_topics(
        channel_id=channel_id or None,
        status=status or None,
        db_path=_db_path(),
        limit=limit,
    ))


@app.route("/api/topics", methods=["POST"])
def api_topic_enqueue():
    body       = request.get_json(silent=True) or {}
    channel_id = body.get("channel_id", "")
    topic_text = body.get("topic_text", "")
    priority   = int(body.get("priority", 0))
    result = enqueue_topic(channel_id, topic_text, priority=priority, db_path=_db_path())
    http_status = 200 if result["ok"] else (
        404 if result.get("code") == "not_found" else
        409 if result.get("code") == "duplicate" else
        400
    )
    return jsonify(result), http_status


@app.route("/api/approvals", methods=["GET"])
def api_approvals_history():
    status = request.args.get("status")
    limit  = min(int(request.args.get("limit", 200)), 500)
    return jsonify(list_approvals_history(
        db_path=_db_path(), status=status or None, limit=limit
    ))


@app.route("/api/signals", methods=["GET"])
def api_signals_history():
    status = request.args.get("status")
    limit  = min(int(request.args.get("limit", 200)), 500)
    return jsonify(list_signals_history(
        db_path=_db_path(), status=status or None, limit=limit
    ))


@app.route("/api/preflight", methods=["GET"])
def api_preflight():
    return jsonify(get_preflight_status(db_path=_db_path()))


# ===========================================================================
# Phase C — Runs & Episode Operations (HTML)
# ===========================================================================

@app.route("/runs")
def runs_page():
    status = request.args.get("status")
    runs   = list_runs(db_path=_db_path(), status=status or None)
    return render_template("runs.html",
                           runs=runs, status_filter=status, fmt_duration=fmt_duration)


@app.route("/run/<run_id>")
def run_detail_page(run_id):
    detail = get_run_detail(run_id, db_path=_db_path())
    if detail is None:
        return render_template("not_found.html", kind="Run", id=run_id), 404
    return render_template("run_detail.html", d=detail, fmt_duration=fmt_duration)


@app.route("/episode/<episode_id>")
def episode_detail_page(episode_id):
    d = get_episode_detail(episode_id, db_path=_db_path())
    if d is None:
        return render_template("not_found.html", kind="Episode", id=episode_id), 404
    valid_tabs = {"production", "publishing", "thumbnails", "titles", "analytics", "revenue", "learning", "branding"}
    active_tab = request.args.get("tab", "production")
    if active_tab not in valid_tabs:
        active_tab = "production"
    return render_template("episode_detail.html", d=d, active_tab=active_tab)


@app.route("/episodes")
def episodes_page():
    channel_id = request.args.get("channel_id")
    episodes   = list_episodes(db_path=_db_path())
    topics     = list_episode_topics(
        channel_id=channel_id or None, db_path=_db_path()
    )
    from hypogeum.dashboard_api import get_channel_summaries
    channels   = get_channel_summaries(db_path=_db_path())
    preflight  = get_preflight_status(db_path=_db_path())
    return render_template("episodes.html",
                           episodes=episodes, topics=topics,
                           channels=channels, preflight=preflight,
                           channel_filter=channel_id)


@app.route("/episodes/enqueue", methods=["POST"])
def episode_enqueue():
    channel_id = request.form.get("channel_id", "")
    topic_text = request.form.get("topic_text", "")
    priority   = int(request.form.get("priority", 0))
    result = enqueue_topic(channel_id, topic_text, priority=priority, db_path=_db_path())
    # Redirect back regardless — flash message baked into query param
    code = "ok" if result["ok"] else result.get("code", "error")
    return redirect(url_for("episodes_page") + f"?enqueue={code}")


@app.route("/approvals")
def approvals_page():
    status    = request.args.get("status")
    approvals = list_approvals_history(
        db_path=_db_path(), status=status or None
    )
    return render_template("approvals_history.html",
                           approvals=approvals, status_filter=status)


@app.route("/signals")
def signals_page():
    status  = request.args.get("status")
    signals = list_signals_history(
        db_path=_db_path(), status=status or None
    )
    return render_template("signals_history.html",
                           signals=signals, status_filter=status)


@app.route("/preflight")
def preflight_page():
    result = get_preflight_status(db_path=_db_path())
    return render_template("preflight.html", preflight=result)


# ===========================================================================
# Phase D — Live Operations (JSON API)
# ===========================================================================

@app.route("/api/live", methods=["GET"])
def api_live():
    return jsonify({
        "active_runs":   get_active_runs(db_path=_db_path()),
        "queue":         get_queue_snapshot(db_path=_db_path()),
        "metrics":       get_live_metrics(db_path=_db_path()),
        "failures":      get_recent_failures(db_path=_db_path(), limit=5),
    })


@app.route("/api/run/<run_id>/live", methods=["GET"])
def api_run_live(run_id):
    data = get_run_live(run_id, db_path=_db_path())
    if data is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@app.route("/api/queue", methods=["GET"])
def api_queue():
    return jsonify(get_queue_snapshot(db_path=_db_path()))


@app.route("/api/failures", methods=["GET"])
def api_failures():
    limit = min(int(request.args.get("limit", 20)), 100)
    return jsonify(get_recent_failures(db_path=_db_path(), limit=limit))


@app.route("/api/run/<run_id>/logs", methods=["GET"])
def api_run_logs(run_id):
    """Last N log lines for a run (JSON array, no streaming)."""
    n = min(int(request.args.get("n", 100)), 500)
    detail = get_run_detail(run_id, db_path=_db_path())
    episode_id = detail["run"]["episode_id"] if detail else None
    lines = tail_log(episode_id=episode_id, n=n)
    return jsonify(lines)


@app.route("/api/run/<run_id>/log-stream")
def api_run_log_stream(run_id):
    """
    Server-Sent Events: streams log lines for a running episode then
    polls for new lines every 3 s until max_ticks are consumed.

    max_ticks can be set low via ?max_ticks=N for tests.
    """
    detail = get_run_detail(run_id, db_path=_db_path())
    episode_id = detail["run"]["episode_id"] if detail else None

    from pathlib import Path as _Path
    _PROJECT_ROOT2 = _Path(__file__).resolve().parent
    log_path = _PROJECT_ROOT2 / ".hypogeum-assets" / "pipeline.log"

    max_ticks = int(request.args.get("max_ticks", 120))  # default ~6 min

    def _generate() -> Iterator[str]:
        # Emit historical lines first
        for entry in tail_log(episode_id=episode_id, n=80, log_path=log_path):
            yield f"data: {_json.dumps(entry)}\n\n"

        last_pos = log_path.stat().st_size if log_path.exists() else 0
        for _ in range(max_ticks):
            time.sleep(3)
            if log_path.exists():
                with log_path.open("rb") as fh:
                    fh.seek(last_pos)
                    raw = fh.read().decode("utf-8", errors="replace")
                    last_pos = fh.tell() if last_pos < fh.seek(0, 2) else last_pos
                for line in raw.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = _json.loads(line)
                        if episode_id and entry.get("episode_id") != episode_id:
                            continue
                        yield f"data: {_json.dumps(entry)}\n\n"
                    except Exception:
                        pass

            # Check if run finished — if so, close stream
            live = get_run_live(run_id, db_path=_db_path())
            if live and live["run"].get("status") not in ("running",):
                yield f"data: {_json.dumps({'_stream_end': True, 'status': live['run'].get('status')})}\n\n"
                return

        yield f"data: {_json.dumps({'_stream_end': True, 'reason': 'timeout'})}\n\n"

    return Response(
        _generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )


# ===========================================================================
# Phase D — Live Operations (HTML)
# ===========================================================================

@app.route("/live")
def live_page():
    metrics  = get_live_metrics(db_path=_db_path())
    active   = get_active_runs(db_path=_db_path())
    failures = get_recent_failures(db_path=_db_path(), limit=5)
    queue    = get_queue_snapshot(db_path=_db_path())
    return render_template("live.html",
                           metrics=metrics, active=active,
                           failures=failures, queue=queue)


@app.route("/run/<run_id>/live")
def run_live_page(run_id):
    data = get_run_live(run_id, db_path=_db_path())
    if data is None:
        return render_template("not_found.html", kind="Run", id=run_id), 404
    return render_template("run_live.html", d=data, run_id=run_id,
                           fmt_duration=fmt_duration)


@app.route("/queue")
def queue_page():
    queue    = get_queue_snapshot(db_path=_db_path())
    metrics  = get_live_metrics(db_path=_db_path())
    return render_template("queue.html", queue=queue, metrics=metrics)


@app.route("/failures")
def failures_page():
    limit    = min(int(request.args.get("limit", 20)), 100)
    failures = get_recent_failures(db_path=_db_path(), limit=limit)
    return render_template("failures.html", failures=failures)


# ===========================================================================
# Phase B — HTML operator UI routes
# ===========================================================================

# ── Channel detail ─────────────────────────────────────────────────────────

@app.route("/channel/<channel_id>")
def channel_detail(channel_id):
    db = _db_path()
    channel = get_channel_detail(channel_id, db_path=db)
    if channel is None:
        return render_template("not_found.html", kind="Channel", id=channel_id), 404
    brand     = get_brand_detail(channel["brand_profile_id"], db_path=db)     if channel.get("brand_profile_id")      else None
    model     = get_model_detail(channel["model_profile_id"], db_path=db)     if channel.get("model_profile_id")      else None
    publishing = get_publishing_detail(channel["publishing_profile_id"], db_path=db) if channel.get("publishing_profile_id") else None
    return render_template("channel_detail.html",
                           channel=channel, brand=brand, model=model, publishing=publishing)


@app.route("/channel/<channel_id>/edit", methods=["GET", "POST"])
def channel_edit(channel_id):
    db = _db_path()
    if request.method == "POST":
        _ALLOWED = {"name", "description"}
        updates = {k: v for k, v in request.form.items() if k in _ALLOWED}
        edit_channel_profile(channel_id, updates, db_path=db)
        return redirect(url_for("channel_detail", channel_id=channel_id))
    channel = get_channel_detail(channel_id, db_path=db)
    if channel is None:
        return render_template("not_found.html", kind="Channel", id=channel_id), 404
    return render_template("channel_edit.html", channel=channel)


# ── Brand profile edit ─────────────────────────────────────────────────────

_BRAND_ALLOWED = frozenset({
    "name", "persona_name", "tone", "target_audience",
    "visual_style", "language", "voice_guidelines",
})

@app.route("/brand/<brand_profile_id>/edit", methods=["GET", "POST"])
def brand_edit(brand_profile_id):
    db = _db_path()
    if request.method == "POST":
        updates = {k: v for k, v in request.form.items()
                   if k in _BRAND_ALLOWED and v.strip() != ""}
        # Always include blank fields too (allow clearing)
        for k in _BRAND_ALLOWED:
            if k in request.form and k not in updates:
                updates[k] = request.form[k]
        edit_brand_profile(brand_profile_id, updates, db_path=db)
        return redirect(url_for("brand_edit", brand_profile_id=brand_profile_id))
    brand = get_brand_detail(brand_profile_id, db_path=db)
    if brand is None:
        return render_template("not_found.html", kind="Brand profile", id=brand_profile_id), 404
    return render_template("brand_edit.html", brand=brand)


# ── Model profile edit ─────────────────────────────────────────────────────

_MODEL_ALLOWED = frozenset({
    "name", "episode_producer", "research_director", "thumbnail_planner",
    "brand_guardian_p1", "brand_guardian_p2", "narration_voice_id",
    "image_provider", "video_provider",
})

@app.route("/model/<model_profile_id>/edit", methods=["GET", "POST"])
def model_edit(model_profile_id):
    db = _db_path()
    if request.method == "POST":
        updates = {k: v for k, v in request.form.items() if k in _MODEL_ALLOWED}
        edit_model_profile(model_profile_id, updates, db_path=db)
        return redirect(url_for("model_edit", model_profile_id=model_profile_id))
    model = get_model_detail(model_profile_id, db_path=db)
    if model is None:
        return render_template("not_found.html", kind="Model profile", id=model_profile_id), 404
    return render_template("model_edit.html", model=model)


# ── Publishing profile edit ────────────────────────────────────────────────

@app.route("/publishing/<publishing_profile_id>/edit", methods=["GET", "POST"])
def publishing_edit(publishing_profile_id):
    db = _db_path()
    if request.method == "POST":
        updates = {}
        if "name" in request.form:
            updates["name"] = request.form["name"]
        platforms_raw = request.form.get("enabled_platforms", "")
        updates["enabled_platforms"] = [
            p.strip() for p in platforms_raw.split(",") if p.strip()
        ]
        updates["ai_disclosure_required"] = "ai_disclosure_required" in request.form
        updates["auto_thumbnail"]         = "auto_thumbnail" in request.form
        updates["auto_captions"]          = "auto_captions" in request.form
        edit_publishing_profile(publishing_profile_id, updates, db_path=db)
        return redirect(url_for("publishing_edit",
                                publishing_profile_id=publishing_profile_id))
    pub = get_publishing_detail(publishing_profile_id, db_path=db)
    if pub is None:
        return render_template("not_found.html", kind="Publishing profile",
                               id=publishing_profile_id), 404
    return render_template("publishing_edit.html", publishing=pub)


# ===========================================================================
# Phase E — Analytics & Learning (JSON API)
# ===========================================================================

@app.route("/api/analytics/overview", methods=["GET"])
def api_analytics_overview():
    return jsonify(get_analytics_overview(db_path=_db_path()))


@app.route("/api/analytics/platform/<platform>", methods=["GET"])
def api_analytics_platform(platform):
    if platform not in _VALID_PLATFORMS:
        return jsonify({"error": f"unknown platform {platform!r}"}), 400
    limit = min(int(request.args.get("limit", 50)), 200)
    return jsonify(get_platform_summary(platform, db_path=_db_path(), limit=limit))


@app.route("/api/analytics/episode/<episode_id>", methods=["GET"])
def api_analytics_episode(episode_id):
    data = get_episode_performance(episode_id, db_path=_db_path())
    if data is None:
        return jsonify({"error": "no analytics found"}), 404
    return jsonify(data)


@app.route("/api/analytics/insights/<episode_id>", methods=["GET"])
def api_analytics_insights(episode_id):
    from hypogeum.analytics_store import list_insights
    return jsonify(list_insights(episode_id=episode_id, db_path=_db_path()))


@app.route("/api/analytics/import", methods=["POST"])
def api_analytics_import():
    body       = request.get_json(silent=True) or {}
    episode_id = body.get("episode_id", "")
    platform   = body.get("platform", "")
    raw        = body.get("metrics") or body.get("raw") or {}
    if not episode_id:
        return jsonify({"ok": False, "error": "episode_id required", "code": "invalid"}), 400
    result = import_from_dict(episode_id, platform, raw, source="manual", db_path=_db_path())
    status = 200 if result["ok"] else (400 if result.get("code") == "invalid" else 500)
    return jsonify(result), status


@app.route("/api/analytics/import/manual", methods=["POST"])
def api_analytics_import_manual():
    """
    F10: Explicit manual analytics import endpoint.
    Accepts the exact field set documented in F10 spec.
    watch_time maps to total_watch_time_s.
    Always inserts source='manual'.
    """
    body = request.get_json(silent=True) or {}
    episode_id = (body.get("episode_id") or "").strip()
    platform   = (body.get("platform") or "").strip()

    if not episode_id:
        return jsonify({"ok": False, "error": "episode_id required", "code": "invalid"}), 400
    if platform not in _VALID_PLATFORMS:
        return jsonify({"ok": False, "error": f"unknown platform {platform!r}", "code": "invalid"}), 400

    metrics: dict = {}
    for f in ("views", "impressions", "likes", "comments", "shares"):
        v = body.get(f)
        if v is not None:
            try:
                metrics[f] = int(float(str(v)))
            except (ValueError, TypeError):
                pass
    for f in ("ctr", "avg_view_pct"):
        v = body.get(f)
        if v is not None:
            try:
                metrics[f] = float(v)
            except (ValueError, TypeError):
                pass
    if body.get("watch_time") is not None:
        try:
            metrics["total_watch_time_s"] = float(body["watch_time"])
        except (ValueError, TypeError):
            pass
    if body.get("platform_video_id"):
        metrics["platform_video_id"] = str(body["platform_video_id"])

    snapshot_date = body.get("snapshot_date") or None
    result = import_from_dict(
        episode_id, platform, metrics,
        source="manual",
        db_path=_db_path(),
    )
    if snapshot_date and result.get("ok") and result.get("record_id"):
        pass  # snapshot_date handled inside import_from_dict via metrics
    http = 200 if result["ok"] else (400 if result.get("code") == "invalid" else 500)
    return jsonify(result), http


@app.route("/api/analytics/import/csv", methods=["POST"])
def api_analytics_import_csv():
    """
    F10: CSV analytics import endpoint.
    Accepts JSON body with episode_id, platform, csv_text.
    Validates required columns; inserts rows with source='csv'.
    """
    body       = request.get_json(silent=True) or {}
    episode_id = (body.get("episode_id") or "").strip()
    platform   = (body.get("platform") or "").strip()
    csv_text   = body.get("csv_text") or ""

    if not episode_id:
        return jsonify({"ok": False, "error": "episode_id required", "code": "invalid"}), 400
    if platform not in _VALID_PLATFORMS:
        return jsonify({"ok": False, "error": f"unknown platform {platform!r}", "code": "invalid"}), 400
    if not csv_text.strip():
        return jsonify({"ok": False, "error": "csv_text required", "code": "invalid"}), 400

    result = import_from_csv(episode_id, platform, csv_text, db_path=_db_path())
    http = 200 if result["ok"] else (400 if result.get("code") == "invalid" else 500)
    return jsonify(result), http


@app.route("/api/analytics/seed-mock/<episode_id>", methods=["POST"])
def api_analytics_seed_mock(episode_id):
    body     = request.get_json(silent=True) or {}
    platform = body.get("platform", "")
    if platform not in _VALID_PLATFORMS:
        # Seed all platforms when not specified
        results = {}
        for p in sorted(_VALID_PLATFORMS):
            results[p] = seed_mock_analytics(episode_id, p, db_path=_db_path())
        return jsonify({"ok": True, "results": results})
    result = seed_mock_analytics(episode_id, platform, db_path=_db_path())
    status = 200 if result["ok"] else 400
    return jsonify(result), status


@app.route("/api/learning", methods=["GET"])
def api_learning():
    return jsonify(get_learning_state(db_path=_db_path()))


@app.route("/api/learning/recommendations", methods=["GET"])
def api_learning_recommendations():
    from hypogeum.learning_engine import get_recommendations
    return jsonify(get_recommendations(db_path=_db_path()))


@app.route("/api/learning/action/<action_id>/apply", methods=["POST"])
def api_learning_action_apply(action_id):
    result = apply_action(action_id, db_path=_db_path())
    status = 200 if result["ok"] else (
        404 if result.get("code") == "not_found" else 400
    )
    return jsonify(result), status


@app.route("/api/learning/action/<action_id>/dismiss", methods=["POST"])
def api_learning_action_dismiss(action_id):
    result = dismiss_action(action_id, db_path=_db_path())
    status = 200 if result["ok"] else (
        404 if result.get("code") == "not_found" else 400
    )
    return jsonify(result), status


# ===========================================================================
# Phase E — Analytics & Learning (HTML)
# ===========================================================================

@app.route("/analytics")
def analytics_page():
    overview   = get_analytics_overview(db_path=_db_path())
    # Fetch channel list for seed-mock form
    from hypogeum.dashboard_api import get_channel_summaries
    channels   = get_channel_summaries(db_path=_db_path())
    return render_template("analytics.html",
                           overview=overview, channels=channels,
                           valid_platforms=sorted(_VALID_PLATFORMS))


@app.route("/analytics/platform/<platform>")
def analytics_platform_page(platform):
    if platform not in _VALID_PLATFORMS:
        return render_template("not_found.html", kind="Platform", id=platform), 404
    summary = get_platform_summary(platform, db_path=_db_path())
    return render_template("analytics_platform.html",
                           summary=summary, platform=platform)


@app.route("/analytics/episode/<episode_id>")
def analytics_episode_page(episode_id):
    data = get_episode_performance(episode_id, db_path=_db_path())
    if data is None:
        return render_template("not_found.html",
                               kind="Episode analytics", id=episode_id), 404
    return render_template("analytics_episode.html", d=data)


@app.route("/analytics/import", methods=["GET"])
def analytics_import_page():
    from hypogeum.dashboard_api import get_channel_summaries
    from hypogeum.dashboard_runs import list_episodes
    channels = get_channel_summaries(db_path=_db_path())
    episodes = list_episodes(db_path=_db_path(), limit=50)
    overview = get_analytics_overview(db_path=_db_path())
    flash    = request.args.get("import_result")
    return render_template("analytics_import.html",
                           channels=channels, episodes=episodes,
                           valid_platforms=sorted(_VALID_PLATFORMS),
                           flash=flash, overview=overview)


@app.route("/analytics/import", methods=["POST"])
def analytics_import_submit():
    episode_id = request.form.get("episode_id", "").strip()
    platform   = request.form.get("platform", "").strip()
    method     = request.form.get("method", "json")

    if not episode_id or platform not in _VALID_PLATFORMS:
        return redirect(url_for("analytics_import_page") + "?import_result=invalid")

    if method == "mock":
        result = seed_mock_analytics(episode_id, platform, db_path=_db_path())
    elif method == "csv":
        csv_text = request.form.get("csv_text", "")
        result   = import_from_csv(episode_id, platform, csv_text, db_path=_db_path())
    else:  # json
        raw_text = request.form.get("json_text", "{}")
        try:
            import json as _j
            raw = _j.loads(raw_text)
        except Exception:
            return redirect(url_for("analytics_import_page") + "?import_result=json_error")
        result = import_from_dict(episode_id, platform, raw, source="manual", db_path=_db_path())

    if result["ok"]:
        # Auto-extract insights after import
        try:
            extract_insights(episode_id, db_path=_db_path())
            generate_actions(episode_id, db_path=_db_path())
        except Exception:
            pass
        return redirect(url_for("analytics_episode_page", episode_id=episode_id)
                        + "?import_result=ok")

    code = result.get("code", "error")
    return redirect(url_for("analytics_import_page") + f"?import_result={code}")


@app.route("/learning")
def learning_page():
    state = get_learning_state(db_path=_db_path())
    return render_template("learning.html", state=state)


@app.route("/learning/action/<action_id>/apply", methods=["POST"])
def learning_action_apply(action_id):
    apply_action(action_id, db_path=_db_path())
    return redirect(url_for("learning_page"))


@app.route("/learning/action/<action_id>/dismiss", methods=["POST"])
def learning_action_dismiss(action_id):
    dismiss_action(action_id, db_path=_db_path())
    return redirect(url_for("learning_page"))


# ===========================================================================
# F10 — Published Video Import
# ===========================================================================

_VALID_PUBLISH_STATUSES = frozenset({"success", "failed", "pending", "skipped", "unknown"})


def _import_publishing_outcome(body: dict, db_path) -> dict:
    """
    Write one publishing_outcome row. Returns Result dict.
    Fields: episode_id, run_id?, platform, publishing_status,
            platform_video_id?, published_at?, url?, failure_reason?
    """
    import uuid
    from datetime import datetime, timezone

    episode_id        = (body.get("episode_id") or "").strip()
    platform          = (body.get("platform") or "").strip()
    publishing_status = (body.get("publishing_status") or "unknown").strip()

    if not episode_id:
        return {"ok": False, "error": "episode_id required", "code": "invalid"}
    if platform not in _VALID_PLATFORMS:
        return {"ok": False, "error": f"unknown platform {platform!r}", "code": "invalid"}
    if publishing_status not in _VALID_PUBLISH_STATUSES:
        return {"ok": False, "error": f"invalid publishing_status {publishing_status!r}", "code": "invalid"}

    from hypogeum.db import get_connection, initialize_schema
    db = db_path
    initialize_schema(db)
    conn = get_connection(db)
    try:
        existing = conn.execute(
            "SELECT outcome_id FROM publishing_outcomes WHERE episode_id=? AND platform=?",
            [episode_id, platform],
        ).fetchone()
        outcome_id = existing["outcome_id"] if existing else str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO publishing_outcomes
               (outcome_id, episode_id, run_id, platform, publishing_status,
                platform_video_id, published_at, url, failure_reason,
                recorded_at, metadata_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                outcome_id,
                episode_id,
                body.get("run_id") or None,
                platform,
                publishing_status,
                body.get("platform_video_id") or None,
                body.get("published_at") or None,
                body.get("url") or None,
                body.get("failure_reason") or None,
                now,
                "v1",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "outcome_id": outcome_id, "episode_id": episode_id, "platform": platform}


@app.route("/api/published/import/manual", methods=["POST"])
def api_published_import_manual():
    body   = request.get_json(silent=True) or {}
    result = _import_publishing_outcome(body, _db_path())
    http   = 200 if result["ok"] else (400 if result.get("code") == "invalid" else 500)
    return jsonify(result), http


@app.route("/published/import", methods=["GET"])
def published_import_page():
    from hypogeum.dashboard_runs import list_episodes
    episodes = list_episodes(db_path=_db_path(), limit=50)
    flash    = request.args.get("import_result")
    error    = request.args.get("error")
    return render_template(
        "published_import.html",
        episodes=episodes,
        valid_platforms=sorted(_VALID_PLATFORMS),
        valid_statuses=sorted(_VALID_PUBLISH_STATUSES),
        flash=flash,
        error=error,
    )


@app.route("/published/import", methods=["POST"])
def published_import_submit():
    body = {
        "episode_id":        request.form.get("episode_id", "").strip(),
        "run_id":            request.form.get("run_id", "").strip() or None,
        "platform":          request.form.get("platform", "").strip(),
        "publishing_status": request.form.get("publishing_status", "unknown").strip(),
        "platform_video_id": request.form.get("platform_video_id", "").strip() or None,
        "published_at":      request.form.get("published_at", "").strip() or None,
        "url":               request.form.get("url", "").strip() or None,
        "failure_reason":    request.form.get("failure_reason", "").strip() or None,
    }
    result = _import_publishing_outcome(body, _db_path())
    if result["ok"]:
        return redirect(url_for("published_import_page") + "?import_result=ok")
    return redirect(url_for("published_import_page") + f"?error={result.get('error','error')}")


# ===========================================================================
# F2 — Decision Inputs (JSON API + HTML)
# ===========================================================================

@app.route("/api/decision-inputs", methods=["GET"])
def api_decision_inputs():
    try:
        state = get_decision_inputs_state(db_path=_db_path())
        return jsonify(state)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


@app.route("/api/decisions", methods=["GET"])
def api_decisions():
    try:
        state = get_decision_state(db_path=_db_path())
        return jsonify(state)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


@app.route("/decisions")
def decisions_page():
    try:
        state = get_decision_state(db_path=_db_path())
    except Exception as exc:
        state = {
            "checked_at": "", "engine_status": "not_ready",
            "summary": f"Error: {exc}",
            "decisions": [], "insufficient_data": [],
        }
    return render_template("decisions.html", state=state)


# ===========================================================================
# F4 — Decision Review Persistence
# ===========================================================================

@app.route("/api/decision-reviews", methods=["GET"])
def api_decision_reviews():
    status = request.args.get("status")
    reviews = list_decision_reviews(status=status or None, db_path=_db_path())
    return jsonify(reviews)


@app.route("/api/decision-reviews/sync", methods=["POST"])
def api_decision_reviews_sync():
    result = sync_decisions_to_reviews(db_path=_db_path())
    http_status = 200 if result.get("ok") else 503
    return jsonify(result), http_status


@app.route("/api/decision-reviews/<review_id>/accept", methods=["POST"])
def api_decision_reviews_accept(review_id):
    body = request.get_json(silent=True) or {}
    result = accept_decision(review_id, note=body.get("note", ""), db_path=_db_path())
    http_status = 200 if result["ok"] else (404 if result.get("code") == "not_found" else 400)
    return jsonify(result), http_status


@app.route("/api/decision-reviews/<review_id>/reject", methods=["POST"])
def api_decision_reviews_reject(review_id):
    body = request.get_json(silent=True) or {}
    result = reject_decision(review_id, note=body.get("note", ""), db_path=_db_path())
    http_status = 200 if result["ok"] else (404 if result.get("code") == "not_found" else 400)
    return jsonify(result), http_status


@app.route("/decision-reviews")
def decision_reviews_page():
    try:
        pending  = list_decision_reviews(status="pending",  db_path=_db_path())
        accepted = list_decision_reviews(status="accepted", db_path=_db_path())
        rejected = list_decision_reviews(status="rejected", db_path=_db_path())
    except Exception:
        pending = accepted = rejected = []
    return render_template("decision_reviews.html",
                           pending=pending, accepted=accepted, rejected=rejected)


# ---------------------------------------------------------------------------
# F5: Execution Plans
# ---------------------------------------------------------------------------

@app.route("/api/execution-plans", methods=["GET"])
def api_execution_plans_list():
    status = request.args.get("status")
    plans = list_execution_plans(status=status or None, db_path=_db_path())
    return jsonify(plans)


@app.route("/api/execution-plans/<plan_id>", methods=["GET"])
def api_execution_plan_get(plan_id):
    plan = get_execution_plan(plan_id, db_path=_db_path())
    if plan is None:
        return jsonify({"ok": False, "code": "not_found"}), 404
    return jsonify(plan)


@app.route("/api/execution-plans/from-review/<review_id>", methods=["POST"])
def api_execution_plan_from_review(review_id):
    result = create_execution_plan_from_review(review_id, db_path=_db_path())
    if not result["ok"]:
        code = result.get("code", "error")
        http = 404 if code == "review_not_found" else 400
        return jsonify(result), http
    return jsonify(result)


@app.route("/api/execution-plans/<plan_id>/approve", methods=["POST"])
def api_execution_plan_approve(plan_id):
    body   = request.get_json(silent=True) or {}
    result = approve_execution_plan(plan_id, note=body.get("note", ""), db_path=_db_path())
    http   = 200 if result["ok"] else (404 if result.get("code") == "not_found" else 400)
    return jsonify(result), http


@app.route("/api/execution-plans/<plan_id>/reject", methods=["POST"])
def api_execution_plan_reject(plan_id):
    body   = request.get_json(silent=True) or {}
    result = reject_execution_plan_fn(plan_id, note=body.get("note", ""), db_path=_db_path())
    http   = 200 if result["ok"] else (404 if result.get("code") == "not_found" else 400)
    return jsonify(result), http


@app.route("/api/execution-plans/<plan_id>/cancel", methods=["POST"])
def api_execution_plan_cancel(plan_id):
    body   = request.get_json(silent=True) or {}
    result = cancel_execution_plan(plan_id, note=body.get("note", ""), db_path=_db_path())
    http   = 200 if result["ok"] else (404 if result.get("code") == "not_found" else 400)
    return jsonify(result), http


@app.route("/api/execution-plans/<plan_id>/dry-run", methods=["POST"])
def api_execution_plan_dry_run(plan_id):
    result = dry_run_execution_plan(plan_id, db_path=_db_path())
    http   = 200 if result["ok"] else (404 if result.get("code") == "not_found" else 400)
    return jsonify(result), http


@app.route("/execution-plans")
def execution_plans_page():
    try:
        draft     = list_execution_plans(status="draft",     db_path=_db_path())
        approved  = list_execution_plans(status="approved",  db_path=_db_path())
        rejected  = list_execution_plans(status="rejected",  db_path=_db_path())
        cancelled = list_execution_plans(status="cancelled", db_path=_db_path())
    except Exception:
        draft = approved = rejected = cancelled = []
    return render_template(
        "execution_plans.html",
        draft=draft, approved=approved,
        rejected=rejected, cancelled=cancelled,
    )


@app.route("/decision-inputs")
def decision_inputs_page():
    try:
        state = get_decision_inputs_state(db_path=_db_path())
    except Exception as exc:
        state = {
            "checked_at": "",
            "discovery":  {"has_data": False, "total_topics": 0, "by_status": {},
                           "channels_with_topics": 0, "recent_consumed": []},
            "episodes":   {"has_data": False, "total_episodes": 0, "completed_episodes": 0,
                           "failed_episodes": 0, "by_status": {}, "recent_completed": [],
                           "avg_run_duration_s": None, "total_produced_assets": 0},
            "publishing": {"has_data": False, "total_outcomes": 0, "by_status": {},
                           "by_platform": {}, "successful": 0, "recent_outcomes": []},
            "costs":      {"has_data": False, "total_episodes_costed": 0, "total_cost_usd": 0.0,
                           "avg_cost_per_episode": None, "by_provider": {}, "by_model": {},
                           "recent_episodes": [], "total_tokens": 0.0},
            "audience":   {"has_real_data": False, "total_snapshots": 0, "real_snapshots": 0,
                           "mock_snapshots": 0, "by_platform": {}, "recent_real": [],
                           "mock_warning": None},
            "readiness":  {"has_real_inputs": False, "missing_inputs": [],
                           "ready_for_decision_engine": False},
            "error":      str(exc),
        }
    return render_template("decision_inputs.html", state=state)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Hypogeum Factory OS local dashboard"
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5001,
                        help="Port (default: 5001)")
    args = parser.parse_args()

    print(f"Factory OS Dashboard → http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
