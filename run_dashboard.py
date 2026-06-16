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
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from flask import Flask, jsonify, redirect, render_template, request, url_for

from hypogeum.dashboard_api import get_factory_state
from hypogeum.factory_director import assess_factory
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

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

_TEMPLATE_DIR = _ROOT / "hypogeum" / "templates"

app = Flask(__name__, template_folder=str(_TEMPLATE_DIR))
app.config["TEMPLATES_AUTO_RELOAD"] = True


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

@app.route("/")
def index():
    state, report, err = _safe_state()
    return render_template(
        "dashboard.html",
        state=state,
        report=report,
        error=err,
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
