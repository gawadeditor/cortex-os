#!/usr/bin/env python3
"""
run_dashboard.py — Hypogeum Cortex Local Dashboard

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
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterator

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from flask import Flask, Response, g, jsonify, redirect, render_template, request, url_for

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
    "tiktok":    "oauth2_token",
    "instagram": "oauth2_token",
    "facebook":  "oauth2_token",
}
_PLATFORM_DISPLAY = {
    "youtube": "YouTube", "tiktok": "TikTok",
    "instagram": "Instagram", "facebook": "Facebook",
}
_VALID_PLATFORMS_CONNECT = frozenset(_PLATFORM_CRED_TYPE)
from hypogeum.prompt_registry import list_prompts, get_prompt
from hypogeum.model_comparison import (
    run_model_comparison,
    query_archive_for_comparison,
    get_model_comparison_result,
    DEFAULT_LLM_MATRIX,
    BENCHMARK_USER_MESSAGE,
)
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
    get_factory_pulse,
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
from hypogeum.setup_preflight import get_setup_preflight_status
from hypogeum.provider_verifier import verify_provider, get_all_provider_statuses
from hypogeum.provider_comparison import (
    run_provider_comparison,
    run_video_readiness_check,
    get_comparison_result,
    COMPARISON_PROMPT,
    COMPARISON_TYPE_IMAGE,
    COMPARISON_TYPE_VIDEO,
    DEFAULT_IMAGE_PROVIDERS,
    DEFAULT_VIDEO_PROVIDERS,
)
from hypogeum.episode_artifacts import (
    get_episode_artifacts,
    list_disk_episodes,
    safe_episode_asset_path,
)
from hypogeum.disk_sync import (
    sync_disk_episode_to_db,
    sync_all_disk_episodes_to_db,
    infer_episode_status,
    load_disk_pkg,
    episode_title_from_disk,
    extract_total_cost,
    extract_render_duration_ms,
    extract_scene_count,
    extract_created_at,
    SYNTHETIC_STAGES,
)
from hypogeum.episode_state import (
    build_episode_state,
    thumbnail_status_label,
    PROD_COMPLETE, PROD_PARTIAL, PROD_FAILED,
    REL_READY, REL_BLOCKED, REL_PENDING,
    BLOCKER_THUMBNAIL_TIG_BLOCKED,
    PUB_NOT_READY, PUB_READY, PUB_PUBLISHED, PUB_PARTIAL,
    ANL_NO_DATA, ANL_MANUAL_IMPORT,
)

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


# ── Plain-language / humanising filters (hypogeum.dashboard_humanize) ────────
# A paying channel owner never sees a raw episode id, an internal blocker
# code, a machine timestamp or a snake_case status. See that module.
from hypogeum import dashboard_humanize as _hz  # noqa: E402


@app.template_filter("episode_name")
def _f_episode_name(episode_id):
    """ep_f38dc597 → 'The Real Atlantis Was Found in Greece'."""
    return _hz.episode_display_name(episode_id, db_path=_db_path())


@app.template_filter("plain_status")
def _f_plain_status(value):
    return _hz.plain_status(value)


@app.template_filter("plain_problem")
def _f_plain_problem(code):
    """Internal blocker code → {label, meaning, action, known}."""
    return _hz.plain_problem(code)


@app.template_filter("plain_problem_label")
def _f_plain_problem_label(code):
    return _hz.plain_problem_label(code)


@app.template_filter("rel_time")
def _f_rel_time(value):
    return _hz.relative_time(value)


@app.template_filter("friendly_dt")
def _f_friendly_dt(value, tz_name=None):
    return _hz.friendly_datetime(value, tz_name=tz_name)


@app.template_filter("friendly_date")
def _f_friendly_date(value):
    return _hz.friendly_date(value)


@app.template_filter("friendly_money")
def _f_friendly_money(value, currency="$"):
    return _hz.friendly_money(value, currency=currency)


@app.template_filter("friendly_platform")
def _f_friendly_platform(value):
    return _hz.friendly_platform(value)


@app.template_filter("plain_learning_action")
def _f_plain_learning_action(value):
    return _hz.plain_learning_action(value)


@app.template_filter("plain_dimension")
def _f_plain_dimension(value):
    return _hz.plain_dimension(value)


@app.template_filter("clean_rationale")
def _f_clean_rationale(value):
    return _hz.clean_rationale(value)


@app.template_filter("signal_words")
def _f_signal_words(value):
    return _hz.signal_strength_words(value)


@app.template_filter("plain_channel_blocker")
def _f_plain_channel_blocker(value):
    return _hz.plain_channel_blocker(value)


@app.template_filter("friendly_week")
def _f_friendly_week(value):
    return _hz.friendly_week(value)


@app.template_filter("channel_name")
def _f_channel_name(channel_id):
    return _hz.channel_display_name(channel_id, db_path=_db_path())


@app.template_filter("stage_name")
def _f_stage_name(value):
    return _hz.plain_stage_name(value)


@app.template_filter("friendly_count")
def _f_friendly_count(value, singular, plural=None):
    return _hz.friendly_count(value, singular, plural)


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


# ── Scope: the left-rail selection — "factory" or a single channel_id ────────
_SCOPE_COOKIE = "cortex_scope"


def _factory_provider_accounts() -> list:
    """The Zernio platform-account pool + which channels bind each — the
    factory-level view of publishing connections (accounts are connected
    per channel via Zernio, on each channel's Publishing tab)."""
    try:
        import json as _json
        from hypogeum.db import get_connection as _gc
        c = _gc(_db_path())
        try:
            acc = [dict(r) for r in c.execute(
                "SELECT provider_account_id, provider_id, platform, display_label, "
                "external_account_id, status, needs_reconnection, metadata_json "
                "FROM provider_accounts ORDER BY platform, display_label")]
            binds = [dict(r) for r in c.execute(
                "SELECT channel_id, provider_account_id, enabled "
                "FROM channel_provider_bindings WHERE capability = 'publishing'")]
        finally:
            c.close()
        chan_name = {ch["channel_id"]: ch["name"] for ch in _nav_channels()}
        bound: dict = {}
        for b in binds:
            if b.get("enabled"):
                bound.setdefault(b["provider_account_id"], []).append(
                    chan_name.get(b["channel_id"], b["channel_id"]))
        for a in acc:
            try:
                meta = _json.loads(a.get("metadata_json") or "{}")
            except Exception:  # noqa: BLE001
                meta = {}
            a["username"] = meta.get("username")
            a["followers_count"] = meta.get("followers_count")
            a["synced_at"] = meta.get("synced_at")
            a["bound_channels"] = sorted(set(bound.get(a["provider_account_id"], [])))
        return acc
    except Exception:  # noqa: BLE001
        return []


def _nav_channels():
    """[{channel_id, name}, ...] — cached per request."""
    if not hasattr(g, "_nav_channels"):
        try:
            from hypogeum.dashboard_api import get_channel_summaries
            g._nav_channels = [
                {"channel_id": c.channel_id, "name": getattr(c, "name", "") or c.channel_id}
                for c in get_channel_summaries(db_path=_db_path())
            ]
        except Exception:  # noqa: BLE001
            g._nav_channels = []
    return g._nav_channels


def _current_scope():
    """'factory' or a channel_id. A /channel/<id>/… path always wins, then
    ?scope=, then the cortex_scope cookie, else 'factory'."""
    ids = {c["channel_id"] for c in _nav_channels()}
    m = re.match(r"^/channel/([^/]+)", request.path)
    if m and m.group(1) in ids:
        return m.group(1)
    q = request.args.get("scope")
    if q == "factory" or (q and q in ids):
        return q
    ck = request.cookies.get(_SCOPE_COOKIE)
    if ck == "factory" or (ck and ck in ids):
        return ck
    return "factory"


def _scope_channel():
    """The channel_id the current view is scoped to, or None for the factory."""
    s = _current_scope()
    return None if s == "factory" else s


@app.context_processor
def _inject_scope():
    scope = _current_scope()
    return {
        "scope":         scope,
        "scope_channel": None if scope == "factory" else scope,
        "nav_channels":  _nav_channels(),
        "nav_path":      request.path,
    }


@app.after_request
def _persist_scope(resp):
    try:
        q = request.args.get("scope")
        if q and (q == "factory" or q in {c["channel_id"] for c in _nav_channels()}):
            resp.set_cookie(_SCOPE_COOKIE, q, max_age=60 * 60 * 24 * 365,
                            samesite="Lax", httponly=True)
    except Exception:  # noqa: BLE001
        pass
    return resp


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


def _build_attention_context() -> dict:
    """
    Aggregate all operator attention items from available state providers.
    Never raises. All sub-calls are non-blocking.
    Returns a dict suitable for both the attention template and /api/attention/summary.
    """
    state, report, state_err = _safe_state()

    try:
        learning = get_learning_state(db_path=_db_path())
    except Exception:
        learning = {"pending_actions_list": []}

    try:
        platforms = _get_platform_detail()
    except Exception:
        platforms = []

    # 1. OIS signals — clear the noise, then translate to operator language.
    digest_signals = None
    try:
        from hypogeum.signal_digest import (
            auto_resolve_stale_signals, digest_signals, present_signal,
        )
        auto_resolve_stale_signals(db_path=_db_path())
        state, report, state_err = _safe_state()  # re-read after auto-resolve
    except Exception:  # noqa: BLE001
        present_signal = None

    all_signals      = (state.active_signals or []) if state else []
    _blocking_raw    = [s for s in all_signals if s.get("blocking")]
    _non_blocking    = [s for s in all_signals if not s.get("blocking")]
    if digest_signals:
        _d = digest_signals(_non_blocking, db_path=_db_path())
        warning_signals = _d["attention"]
        info_notices    = _d["notices"]
        blocking_signals = [present_signal(s, db_path=_db_path()) for s in _blocking_raw]
    else:
        warning_signals  = _non_blocking
        info_notices     = []
        blocking_signals = _blocking_raw

    # 2. Pending approvals (dataclass objects — convert to dicts for template/JSON)
    approvals_raw = (state.pending_approvals or []) if state else []
    approvals     = [_to_dict(a) for a in approvals_raw]

    # 3. Learning items pending human review
    learning_pending = learning.get("pending_actions_list", [])

    # 4. Platform connection issues
    platform_issues = []
    for p in platforms:
        vstatus = p.get("vstatus", "missing")
        if vstatus in ("missing", "partial"):
            missing_keys = p.get("missing", [])
            platform_issues.append({
                "platform":   p["id"],
                "label":      f"{p['label']} — not configured",
                "detail":     f"Missing: {', '.join(missing_keys)}" if missing_keys else "Credentials incomplete",
                "severity":   "warning",
                "action_url": f"/platforms/connect/{p['id']}",
                "action_label": f"Configure {p['label']}",
            })
        elif vstatus == "failed":
            platform_issues.append({
                "platform":   p["id"],
                "label":      f"{p['label']} — verification failed",
                "detail":     p.get("error") or "Last credential verification failed",
                "severity":   "warning",
                "action_url": "/platforms",
                "action_label": "View platforms",
            })
        for cred in p.get("credentials", []):
            if cred.get("expiry_warning"):
                platform_issues.append({
                    "platform":   p["id"],
                    "label":      f"{p['label']} — {cred['label']} expiring",
                    "detail":     cred["expiry_warning"],
                    "severity":   "warning",
                    "action_url": p.get("authorize_url") or "/platforms",
                    "action_label": p.get("authorize_label") or "Re-authorize",
                })

    # 5. Factory readiness blocks from director report
    readiness_blocks = []
    if report and report.blocking_reasons:
        for reason in report.blocking_reasons:
            readiness_blocks.append({
                "label":      "Factory readiness block",
                "detail":     reason,
                "action_url": "/preflight",
                "action_label": "View checklist",
            })

    # 6. Run clearance
    run_clearance_issue = None
    if report and hasattr(report, "run_clearance"):
        rc = report.run_clearance
        if not rc.cleared and not report.blocking_reasons:
            run_clearance_issue = {
                "label":      "Run not cleared",
                "detail":     getattr(rc, "reason", "") or "Factory cannot start new runs",
                "action_url": "/preflight",
                "action_label": "View checklist",
            }

    blocking_count       = len(blocking_signals) + len(readiness_blocks) + (1 if run_clearance_issue else 0)
    action_required_count = len(approvals) + len(learning_pending)
    warning_count        = len(warning_signals) + len(platform_issues)

    return {
        "total":                  blocking_count + action_required_count + warning_count,
        "blocking_count":         blocking_count,
        "action_required_count":  action_required_count,
        "warning_count":          warning_count,
        "blocking_signals":       blocking_signals,
        "warning_signals":        warning_signals,
        "info_notices":           info_notices,
        "approvals":              approvals,
        "learning_pending":       learning_pending,
        "platform_issues":        platform_issues,
        "readiness_blocks":       readiness_blocks,
        "run_clearance_issue":    run_clearance_issue,
        "readiness_level":        (report.readiness_level if report else "UNKNOWN"),
        "readiness_score":        (report.readiness_score if report else 0),
        "state_err":              state_err,
    }


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
    try:
        setup_preflight = get_setup_preflight_status(db_path=db)
    except Exception:
        setup_preflight = {"ready": False, "blockers": [], "blocker_count": 0}
    error = request.args.get("error")
    flash = request.args.get("flash")
    ch    = _scope_channel()
    try:
        queued_topics = list_episode_topics(status="queued", channel_id=ch or None, db_path=db)[:15]
    except Exception:
        queued_topics = []
    try:
        from hypogeum.db import get_worker_control
        worker_running = get_worker_control("worker_status", "stopped", db_path=db) in ("running", "claiming", "idle")
        queue_paused   = get_worker_control("queue_paused", "0", db_path=db) == "1"
    except Exception:  # noqa: BLE001
        worker_running, queue_paused = False, False
    return render_template(
        "create.html",
        channels=channels,
        preflight=preflight,
        setup_preflight=setup_preflight,
        error=error,
        flash=flash,
        queued_topics=queued_topics,
        worker_running=worker_running,
        queue_paused=queue_paused,
    )


@app.route("/create/launch", methods=["POST"])
def create_launch():
    """
    Manual Queue Submission — validates and places one topic onto the
    canonical queue (hypogeum.dashboard_control.enqueue_topic() +
    hypogeum.queue.enqueue()) instead of executing a pipeline directly. The
    submitted item is picked up by run_worker.py's existing poll loop and
    gated exactly like any autonomous item. See
    hypogeum.production_launcher.launch_episode_from_dashboard().
    """
    from urllib.parse import quote_plus
    channel_id   = request.form.get("channel_id", "").strip()
    topic        = request.form.get("topic", "").strip()
    video_type   = request.form.get("video_type", "long_video")
    quality_mode = request.form.get("quality_mode", "balanced")
    platforms    = request.form.getlist("platforms")

    from hypogeum.production_launcher import launch_episode_from_dashboard
    result = launch_episode_from_dashboard(
        channel_id=channel_id,
        topic=topic,
        video_type=video_type,
        quality_mode=quality_mode,
        platforms=platforms,
        db_path=_db_path(),
    )

    if result["status"] == "launched":
        return redirect(url_for("episode_detail_page", episode_id=result["episode_id"]))

    if result["status"] == "already_queued":
        # not an error — the topic is in the queue; bump it to the front so
        # the worker takes it next.
        tid = result.get("topic_id")
        if tid:
            _prioritize_topic(tid)
        return redirect(url_for("create_page") + "?flash=already_queued")

    error_str = "; ".join(result.get("missing", ["unknown error"]))
    return redirect(url_for("create_page") + f"?error={quote_plus(error_str)}")


def _prioritize_topic(topic_id: str) -> bool:
    """Move a queued topic to the front of its channel's queue + set its
    production deadline to now, so the worker's next consume picks it up."""
    try:
        from hypogeum.db import get_connection
        from datetime import datetime, timezone
        conn = get_connection(_db_path())
        try:
            conn.execute(
                "UPDATE episode_topics SET priority = -1000, "
                "production_deadline_at = ?, updated_at = ? "
                "WHERE topic_id = ? AND status IN ('queued','claimed')",
                (datetime.now(timezone.utc).isoformat(),
                 datetime.now(timezone.utc).isoformat(), topic_id),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:  # noqa: BLE001
        return False


@app.route("/create/prioritize", methods=["POST"])
def create_prioritize():
    """'Start now' on a topic that is already queued — bump its priority
    rather than trying (and failing) to enqueue it again."""
    topic_id = request.form.get("topic_id", "").strip()
    ok = _prioritize_topic(topic_id) if topic_id else False
    return redirect(url_for("create_page")
                    + ("?flash=prioritized" if ok else "?error=could+not+prioritize+that+topic"))


@app.route("/api/channels/<channel_id>/activate", methods=["POST"])
def api_channel_activate(channel_id: str):
    """
    Run CALOG activation gates and, if all pass, transition channel to 'active'.

    Uses hypogeum.calog.activate_channel() — all gates are enforced.
    Does NOT bypass CALOG or write state directly.

    Returns {ok, channel_id, state, gates: [{gate, passed, reason}], failure_reason}.
    """
    from hypogeum.calog import activate_channel
    try:
        result = activate_channel(channel_id, db_path=_db_path())
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    gates = [
        {"gate": g.gate, "passed": g.passed, "reason": g.reason}
        for g in result.gate_results
    ]
    if result.success:
        return jsonify({
            "ok":         True,
            "channel_id": channel_id,
            "state":      result.new_state,
            "gates":      gates,
        })
    return jsonify({
        "ok":             False,
        "channel_id":     channel_id,
        "state":          result.new_state,
        "gates":          gates,
        "failure_reason": result.failure_reason,
    }), 422


@app.route("/api/factory/overview")
def api_factory_overview():
    """
    Read-only factory-wide operational rollup: the shared clearance verdict,
    worker heartbeat, every channel's runtime status, aggregate weekly
    budget across channels, and the self-learning state. Never writes.
    """
    from hypogeum.factory_overview import get_factory_overview
    try:
        overview = get_factory_overview(db_path=_db_path())
    except Exception as exc:  # get_factory_overview is fail-soft; be defensive
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "overview": overview})


@app.route("/factory")
def factory_overview_page():
    from hypogeum.factory_overview import get_factory_overview
    try:
        overview = get_factory_overview(db_path=_db_path())
    except Exception:
        overview = {}
    return render_template("factory_overview.html", overview=overview)


@app.route("/api/channels/create", methods=["POST"])
def api_channel_create():
    """
    Create a new channel (Phase 4 — 'create a new channel from the
    dashboard'). Accepts JSON or form fields:
      channel_id, name (required); language, platforms (csv), timezone,
      weekly_video_budget_usd, format ('long'|'shorts'), shorts_per_week,
      publish_day, publish_time, narration_voice_id, description.
    Creates the ChannelProfile bundle + a ChannelOperationsProfile
    (operator_enabled=False — the operator opts the channel into
    autonomous production explicitly afterwards).
    """
    from hypogeum.dashboard_control import create_channel

    src = request.get_json(silent=True) or request.form
    def _f(k, default=None):
        v = src.get(k)
        return v if v not in ("", None) else default

    if isinstance(src, dict) and isinstance(src.get("platforms"), list):
        platforms = [str(p).strip() for p in src["platforms"] if str(p).strip()]
    else:
        platforms = [p.strip() for p in str(_f("platforms", "youtube")).split(",") if p.strip()]
    fmt = (_f("format", "long") or "long").lower()
    if fmt in ("shorts", "shorts_only"):
        spw = int(_f("shorts_per_week", 7) or 7)
        cadence = {"videos_per_week": 0, "shorts_per_week": spw}
    elif fmt == "mixed":
        cadence = {"videos_per_week": int(_f("videos_per_week", 1) or 1),
                   "shorts_per_week": int(_f("shorts_per_week", 0) or 0)}
    else:
        cadence = {"videos_per_week": int(_f("videos_per_week", 1) or 1)}

    pub_sched = {}
    if _f("publish_day") and _f("publish_time"):
        pub_sched = {"day_of_week": _f("publish_day"), "time_of_day": _f("publish_time")}

    def _dict(k):
        v = src.get(k)
        return v if isinstance(v, dict) else {}

    budget = _f("weekly_video_budget_usd")
    ceiling = _f("cost_ceiling_usd_per_episode")
    result = create_channel(
        channel_id=str(_f("channel_id", "")).strip(),
        name=str(_f("name", "")).strip(),
        language=_f("language", "en"),
        enabled_platforms=platforms,
        timezone=_f("timezone", "UTC"),
        weekly_video_budget_usd=float(budget) if budget not in (None, "") else None,
        cost_ceiling_usd_per_episode=float(ceiling) if ceiling not in (None, "") else None,
        cadence=cadence,
        publishing_schedule=pub_sched,
        narration_voice_id=_f("narration_voice_id"),
        description=_f("description", ""),
        brand=_dict("brand"),
        models=_dict("models"),
        publishing=_dict("publishing"),
        db_path=_db_path(),
    )
    status = 200 if result.get("ok") else (422 if result.get("code") in ("invalid", "duplicate") else 500)
    return jsonify(result), status


@app.route("/api/channel-readiness/<channel_id>")
def api_channel_readiness(channel_id: str):
    """Plain-language readiness report — activation gates + runtime blockers +
    budget sanity + Zernio links. Read-only. Used by the Channel Builder
    wizard's Review step and the channel detail page."""
    try:
        from hypogeum.channel_readiness import get_channel_readiness
        return jsonify({"ok": True, "readiness": get_channel_readiness(channel_id, db_path=_db_path())})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/channel-builder/suggest", methods=["POST"])
def api_channel_builder_suggest():
    """
    LLM-assisted visual/brand identity for the wizard. Takes {description,
    format, name} and returns a proposal the client edits. Degrades to
    {ok: False} on any gate/model failure — the wizard then just lets the
    operator fill the fields manually.
    """
    src = request.get_json(silent=True) or {}
    desc = str(src.get("description", "")).strip()
    fmt = str(src.get("format", "long")).strip()
    name = str(src.get("name", "")).strip()
    if not desc:
        return jsonify({"ok": False, "error": "describe the channel first"}), 422

    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "channel_identity",
            "strict": True,
            "schema": {
                "type": "object", "additionalProperties": False,
                "required": ["persona_name", "tone", "target_audience", "visual_style",
                             "voice_guidelines", "content_pillars", "color_palette"],
                "properties": {
                    "persona_name": {"type": "string"},
                    "tone": {"type": "string"},
                    "target_audience": {"type": "string"},
                    "visual_style": {"type": "string"},
                    "voice_guidelines": {"type": "string"},
                    "content_pillars": {"type": "array", "items": {"type": "string"}},
                    "color_palette": {
                        "type": "object", "additionalProperties": False,
                        "required": ["primary", "secondary", "accent"],
                        "properties": {"primary": {"type": "string"},
                                       "secondary": {"type": "string"},
                                       "accent": {"type": "string"}},
                    },
                },
            },
        },
    }
    sys_p = (
        "You are a brand director for an autonomous documentary-media studio. "
        "Given a channel brief, propose ONE distinctive brand identity: a persona, "
        "a specific tone (3-5 adjectives), the target audience, a concrete visual "
        "style (materials, palette mood, camera/lighting language), voice guidelines "
        "for the narrator (2-3 sentences, pacing + register), 3-4 content pillars, "
        "and a 3-colour palette as hex. Be opinionated and specific to THIS brief — "
        "never generic. Output JSON only."
    )
    user_p = f"Channel name: {name or '(unnamed)'}\nFormat: {fmt}\nBrief: {desc}"
    try:
        from hypogeum.config import Config
        from hypogeum.openrouter import call_claude
        cfg = Config()
        res = call_claude(
            model_slug=getattr(cfg, "OPENROUTER_SONNET_MODEL_ID", "anthropic/claude-sonnet-4.6"),
            messages=[{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
            max_tokens=900, config=cfg, agent="channel_builder",
            response_format=schema, allow_model_fallback=True,
        )
        text = getattr(res, "text", None) or getattr(res, "content", None) or ""
        proposal = _json.loads(text)
        return jsonify({"ok": True, "proposal": proposal})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"suggestion unavailable ({exc}); fill the fields manually"}), 200


def _cb_call_claude_json(sys_p: str, user_p: str, schema: dict, *, max_tokens: int = 1100):
    """Shared helper for the wizard's LLM steps. Returns the parsed dict or
    raises. Callers degrade to {ok: False} 200 so the wizard never hard-blocks."""
    from hypogeum.config import Config
    from hypogeum.openrouter import call_claude
    cfg = Config()
    res = call_claude(
        model_slug=getattr(cfg, "OPENROUTER_SONNET_MODEL_ID", "anthropic/claude-sonnet-4.6"),
        messages=[{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
        max_tokens=max_tokens, config=cfg, agent="channel_builder",
        response_format=schema, allow_model_fallback=True,
    )
    text = getattr(res, "text", None) or getattr(res, "content", None) or ""
    return _json.loads(text)


@app.route("/api/channel-builder/genres", methods=["POST"])
def api_channel_builder_genres():
    """
    Step 1 of the wizard: take a rough content concept and propose 5-6
    monetisable niches to choose from. Estimates are the model's general
    knowledge only (a brand-new channel has no analytics) — the payload is
    labelled as such and the wizard shows a warning. Degrades to
    {ok: False} 200.
    """
    src = request.get_json(silent=True) or {}
    concept = str(src.get("concept", "")).strip()
    language = str(src.get("language", "en")).strip() or "en"
    if not concept:
        return jsonify({"ok": False, "error": "describe the idea first"}), 422

    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "channel_genres", "strict": True,
            "schema": {
                "type": "object", "additionalProperties": False,
                "required": ["genres"],
                "properties": {"genres": {
                    "type": "array", "minItems": 4, "maxItems": 6,
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["name", "why_monetisable", "audience",
                                     "example_topics", "revenue_band", "competition"],
                        "properties": {
                            "name": {"type": "string"},
                            "why_monetisable": {"type": "string"},
                            "audience": {"type": "string"},
                            "example_topics": {"type": "array", "items": {"type": "string"}},
                            "revenue_band": {"type": "string",
                                             "enum": ["low", "medium", "high", "very high"]},
                            "competition": {"type": "string",
                                            "enum": ["low", "medium", "high"]},
                        },
                    },
                }},
            },
        },
    }
    sys_p = (
        "You advise operators of an autonomous short-form video factory on which "
        "niche to launch a NEW channel in. Given a rough content concept, return "
        "4-6 concrete sub-niches worth pursuing. For each: a specific name, one "
        "sentence on why it monetises (ad RPM drivers, sponsor demand, product "
        "affinity), the core audience, 3 example video topics, a coarse revenue "
        "band, and a competition level. Base this on general market knowledge — "
        "you have NO analytics for this channel. Prefer niches that suit faceless "
        "AI-produced short documentary video. Output JSON only, in "
        f"{language!r} for human-readable fields."
    )
    try:
        data = _cb_call_claude_json(sys_p, f"Concept: {concept}", schema)
        return jsonify({"ok": True, "genres": data.get("genres", []),
                        "disclaimer": "Estimates from the model's general knowledge — "
                                      "not backed by your channel's data or a live market feed."})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"genre suggestions unavailable ({exc}); "
                                              f"type a niche in manually"}), 200


@app.route("/api/channel-builder/names", methods=["POST"])
def api_channel_builder_names():
    """
    Step 2: propose channel names + a matching social handle for the chosen
    concept/niche. The wizard lets the operator pick one, edit it, and run
    the handle-availability check. Degrades to {ok: False} 200.
    """
    src = request.get_json(silent=True) or {}
    concept = str(src.get("concept", "")).strip()
    genre = str(src.get("genre", "")).strip()
    language = str(src.get("language", "en")).strip() or "en"
    if not concept and not genre:
        return jsonify({"ok": False, "error": "pick a niche first"}), 422

    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "channel_names", "strict": True,
            "schema": {
                "type": "object", "additionalProperties": False,
                "required": ["names"],
                "properties": {"names": {
                    "type": "array", "minItems": 4, "maxItems": 6,
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["name", "handle", "rationale"],
                        "properties": {
                            "name": {"type": "string"},
                            "handle": {"type": "string"},
                            "rationale": {"type": "string"},
                        },
                    },
                }},
            },
        },
    }
    sys_p = (
        "You name new video channels. Given a concept and niche, propose 4-6 "
        "distinct channel names. For each, give a lowercase social handle "
        "(letters/numbers/underscores/periods only, <=25 chars, no leading/"
        "trailing separator, usable identically on YouTube, TikTok, Instagram "
        "and Facebook) and one sentence of rationale. Vary the style: literal, "
        "evocative, wordmark. Avoid names that obviously collide with big "
        "existing brands. Output JSON only; keep the rationale in "
        f"{language!r}."
    )
    user_p = f"Concept: {concept or '(from niche)'}\nNiche: {genre or '(unspecified)'}"
    try:
        data = _cb_call_claude_json(sys_p, user_p, schema)
        return jsonify({"ok": True, "names": data.get("names", [])})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"name suggestions unavailable ({exc}); "
                                              f"type a name in manually"}), 200


_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9._]{1,28}[a-z0-9]$")


def _check_one_handle(url: str, timeout: float = 4.0) -> dict:
    """Best-effort existence probe for a public profile URL. Never raises.
    200 -> taken, 404 -> available, anything else (block page, redirect,
    timeout) -> unknown."""
    import requests as _req
    hdrs = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    try:
        r = _req.get(url, headers=hdrs, timeout=timeout, allow_redirects=True)
        if r.status_code == 404:
            return {"status": "available", "url": url}
        if r.status_code == 200:
            return {"status": "taken", "url": url}
        return {"status": "unknown", "url": url, "note": f"HTTP {r.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "url": url, "note": str(exc)[:80]}


@app.route("/api/channel-builder/handle-availability", methods=["POST"])
def api_channel_builder_handle_availability():
    """
    Best-effort check of one handle across YouTube / TikTok / Instagram /
    Facebook. These sites bot-protect their profile pages, so 'unknown' is a
    normal result — the wizard always tells the operator to confirm manually.
    Never blocks channel creation.
    """
    src = request.get_json(silent=True) or {}
    handle = str(src.get("handle", "")).strip().lstrip("@").lower()
    if not handle:
        return jsonify({"ok": False, "error": "enter a handle first"}), 422
    if not _HANDLE_RE.match(handle):
        return jsonify({"ok": False, "error": "handle: 3-30 chars, a-z 0-9 . _ only, "
                                              "no leading/trailing separator"}), 422

    targets = {
        "youtube":   f"https://www.youtube.com/@{handle}",
        "tiktok":    f"https://www.tiktok.com/@{handle}",
        "instagram": f"https://www.instagram.com/{handle}/",
        "facebook":  f"https://www.facebook.com/{handle}",
    }
    results = {k: _check_one_handle(v) for k, v in targets.items()}
    return jsonify({
        "ok": True, "handle": handle, "results": results,
        "disclaimer": "Best-effort only — these platforms block automated checks, "
                      "so 'unknown' is common. Confirm on each platform before you commit.",
    })


def _draft_platform_profiles(*, name="", handle="", persona="", genre="",
                             concept="", language="en"):
    """Shared LLM call: the profile copy for each platform's own page.
    Raises on failure. Used by the wizard and the channel Publishing page."""
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "platform_profiles", "strict": True,
            "schema": {
                "type": "object", "additionalProperties": False,
                "required": ["youtube_about", "tiktok_bio", "instagram_bio",
                             "facebook_intro", "tagline", "keywords"],
                "properties": {
                    "youtube_about":  {"type": "string"},
                    "tiktok_bio":     {"type": "string"},
                    "instagram_bio":  {"type": "string"},
                    "facebook_intro": {"type": "string"},
                    "tagline":        {"type": "string"},
                    "keywords":       {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    }
    sys_p = (
        "You write the profile copy for a new video channel's pages on each "
        "platform. Return: a YouTube About (2-3 short paragraphs, what the "
        "channel is + upload rhythm + a soft CTA), a TikTok bio (<=80 chars), "
        "an Instagram bio (<=150 chars, may use line breaks), a Facebook Page "
        "intro (1-2 sentences), a one-line tagline, and 6-10 discovery "
        "keywords. Match the persona and niche exactly; no hashtag spam, no "
        f"emoji walls. Write human-readable fields in {language!r}. JSON only."
    )
    user_p = (f"Channel: {name or '(unnamed)'}  @{handle or '(no handle)'}\n"
              f"Persona: {persona or '(none)'}\nNiche: {genre or '(unspecified)'}\n"
              f"Concept: {concept or '(from niche)'}")
    return _cb_call_claude_json(sys_p, user_p, schema, max_tokens=1200)


@app.route("/api/channel-builder/platform-profiles", methods=["POST"])
def api_channel_builder_platform_profiles():
    """
    Draft the copy that lives on each platform's own channel page — the
    YouTube "About", TikTok / Instagram bios (char-limited), Facebook Page
    intro, plus link text. The operator pastes these into each platform (or
    Zernio). Degrades to {ok: False} 200.
    """
    src = request.get_json(silent=True) or {}
    concept = str(src.get("concept", "")).strip()
    genre = str(src.get("genre", "")).strip()
    name = str(src.get("name", "")).strip()
    handle = str(src.get("handle", "")).strip().lstrip("@")
    persona = str(src.get("persona", "")).strip()
    language = str(src.get("language", "en")).strip() or "en"
    if not concept and not name:
        return jsonify({"ok": False, "error": "name the channel first"}), 422

    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "platform_profiles", "strict": True,
            "schema": {
                "type": "object", "additionalProperties": False,
                "required": ["youtube_about", "tiktok_bio", "instagram_bio",
                             "facebook_intro", "tagline", "keywords"],
                "properties": {
                    "youtube_about":  {"type": "string"},
                    "tiktok_bio":     {"type": "string"},
                    "instagram_bio":  {"type": "string"},
                    "facebook_intro": {"type": "string"},
                    "tagline":        {"type": "string"},
                    "keywords":       {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    }
    sys_p = (
        "You write the profile copy for a new video channel's pages on each "
        "platform. Return: a YouTube About (2-3 short paragraphs, what the "
        "channel is + upload rhythm + a soft CTA), a TikTok bio (<=80 chars), "
        "an Instagram bio (<=150 chars, may use line breaks), a Facebook Page "
        "intro (1-2 sentences), a one-line tagline, and 6-10 discovery "
        "keywords. Match the persona and niche exactly; no hashtag spam, no "
        f"emoji walls. Write human-readable fields in {language!r}. JSON only."
    )
    try:
        data = _draft_platform_profiles(name=name, handle=handle, persona=persona,
                                        genre=genre, concept=concept, language=language)
        return jsonify({"ok": True, "profiles": data})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"profile copy unavailable ({exc}); "
                                              f"write it in manually"}), 200


@app.route("/api/channels/<channel_id>/operator", methods=["POST"])
def api_channel_set_operator(channel_id: str):
    """
    Flip a channel's operator switch (operator_enabled + unattended_operation
    _enabled) from the wizard's activation step. This does NOT authorize
    spend — the Operating Grant (offline Ed25519) is still required before
    the worker will run paid generation for this channel.
    """
    src = request.get_json(silent=True) or {}
    on = bool(src.get("enabled", True))
    try:
        from hypogeum.channel_config import update_channel_operations_profile, get_channel_operations_profile
        if get_channel_operations_profile(channel_id, db_path=_db_path()) is None:
            return jsonify({"ok": False, "error": "no operations profile for this channel"}), 404
        update_channel_operations_profile(
            channel_id, db_path=_db_path(),
            operator_enabled=on, unattended_operation_enabled=on,
        )
        return jsonify({"ok": True, "channel_id": channel_id, "operator_enabled": on})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/channels/new")
def channel_builder_page():
    """The guided Channel Builder wizard."""
    from hypogeum.config import Config
    _cfg = Config()
    defaults = {
        "models": {
            "episode_producer": "anthropic/claude-sonnet-4.6",
            "research_director": "anthropic/claude-opus-4.6",
            "thumbnail_planner": "openai/gpt-5.5-pro",
            "brand_guardian_p1": "anthropic/claude-sonnet-4.6",
            "brand_guardian_p2": "openai/gpt-5.5-pro",
            "image_provider": "muapi", "video_provider": "muapi",
        },
        "shorts_ceiling": 20, "long_ceiling": 60,
        "voices": [
            {"id": "j9jfwdrw7BRfcR43Qohk", "label": "Documentary narrator (deep, measured)"},
        ],
        # shown only if the LLM genre step is unavailable — the operator can
        # still pick a starting niche or type their own.
        "fallback_genres": [
            "Explainer / how-things-work", "History & mysteries",
            "Science & space", "True crime & unsolved cases",
            "Personal finance & money", "Health, sleep & longevity",
            "Tech news & AI", "Nature & wildlife facts",
            "Kids' learning (numbers, letters, animals)", "Folklore & mythology",
        ],
    }
    return render_template("channel_builder.html", defaults=defaults)


@app.route("/api/channels/<channel_id>/runtime")
def api_channel_runtime(channel_id: str):
    """
    Read-only per-channel operational snapshot: operator flags, weekly-budget
    headroom for the current week, worker heartbeat, queue depth, latest run,
    next scheduled slot, factory clearance, and the derived list of blockers
    preventing autonomous execution. Never writes.
    """
    from hypogeum.channel_runtime_status import get_channel_runtime_status
    try:
        snapshot = get_channel_runtime_status(channel_id, db_path=_db_path())
    except Exception as exc:  # get_channel_runtime_status is fail-soft, but be defensive
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "runtime": snapshot})


@app.route("/channels")
def channels_page():
    db = _db_path()
    try:
        from hypogeum.dashboard_api import get_channel_summaries
        channels = get_channel_summaries(db_path=db)
    except Exception:
        channels = []
    runtime_by_channel = {}
    try:
        from hypogeum.channel_runtime_status import get_channel_runtime_status
        for ch in channels:
            cid = getattr(ch, "channel_id", None) or (ch.get("channel_id") if isinstance(ch, dict) else None)
            if cid:
                runtime_by_channel[cid] = get_channel_runtime_status(cid, db_path=db)
    except Exception:
        runtime_by_channel = {}
    return render_template("channels.html", channels=channels, runtime_by_channel=runtime_by_channel)


@app.route("/settings")
def settings_page():
    return redirect(url_for("settings_storage_page"))


@app.route("/advanced")
def advanced_page():
    """Factory 'Advanced' — one page collecting the rarely-used admin
    surfaces that used to live in the 'More' dropdown."""
    return render_template("advanced.html")


_CONTENT_PROVIDERS = [
    {"id": "openai",     "label": "OpenAI",     "auth": "api_key",
     "description": "Direct image generation"},
    {"id": "openrouter", "label": "OpenRouter", "auth": "api_key",
     "description": "LLM model routing"},
    {"id": "elevenlabs", "label": "ElevenLabs", "auth": "api_key",
     "description": "Voice generation"},
    {"id": "muapi",      "label": "MUAPI",      "auth": "api_key",
     "description": "Image & video generation hub"},
    {"id": "seedance",   "label": "Seedance",   "auth": "api_key",
     "description": "Video generation", "muapi_backed": True},
    {"id": "veo",        "label": "Veo",        "auth": "api_key",
     "description": "Video generation", "muapi_backed": True},
    {"id": "kling",      "label": "Kling",      "auth": "api_key",
     "description": "Video generation", "muapi_backed": True},
    {"id": "runway",     "label": "Runway",     "auth": "api_key",
     "description": "Video generation", "muapi_backed": True},
    {"id": "freepik",           "label": "Freepik",                         "auth": "api_key",
     "description": "Image generation"},
    {"id": "seedance_official", "label": "Seedance 2.0 (BytePlus ModelArk)", "auth": "api_key",
     "description": "Official Seedance video generation"},
    {"id": "runway_official",   "label": "Runway (Official)",                "auth": "api_key",
     "description": "Runway ML video generation"},
    {"id": "kling_official",    "label": "Kling (Official)",                 "auth": "api_key",
     "description": "Kling AI video generation"},
    {"id": "veo_official",      "label": "Veo (Google)",                     "auth": "api_key",
     "description": "Google Veo video generation"},
    {"id": "facebook",          "label": "Facebook",                         "auth": "api_key",
     "description": "Facebook video publishing"},
    {"id": "instagram",         "label": "Instagram",                        "auth": "api_key",
     "description": "Instagram Reels publishing"},
    {"id": "tiktok",            "label": "TikTok",                           "auth": "api_key",
     "description": "TikTok video publishing"},
]

_CONTENT_PROVIDER_SETUP = {
    "openai": {
        "label": "OpenAI", "description": "Direct image generation (gpt-image-1)",
        "cred_label": "OpenAI API Key",
        "instructions": (
            "Go to platform.openai.com → API Keys → Create new secret key. "
            "Paste the key below. Used when THUMBNAIL_IMAGE_PROVIDER=openai for direct image generation."
        ),
        "placeholder": "sk-...",
        "env_var": "OPENAI_API_KEY",
    },
    "openrouter": {
        "label": "OpenRouter", "description": "LLM model routing",
        "cred_label": "OpenRouter API Key",
        "instructions": (
            "Go to openrouter.ai → Keys → Create Key. "
            "Paste the key below. Used for all LLM calls (research, script, planning)."
        ),
        "placeholder": "sk-or-v1-...",
        "env_var": "OPENROUTER_API_KEY",
    },
    "elevenlabs": {
        "label": "ElevenLabs", "description": "Voice generation",
        "cred_label": "ElevenLabs API Key",
        "instructions": (
            "Go to elevenlabs.io → Profile → API Key. "
            "Paste the key below. Also set ELEVENLABS_VOICE_ID to your chosen voice."
        ),
        "placeholder": "el_...",
        "env_var": "ELEVENLABS_API_KEY",
    },
    "muapi": {
        "label": "MUAPI", "description": "Image & video generation",
        "cred_label": "MUAPI API Key",
        "instructions": (
            "Go to muapi.ai → Dashboard → API Keys → Create Key. "
            "Used for image and video generation (Seedance, Runway, Flux models)."
        ),
        "placeholder": "mu-...",
        "env_var": "MUAPI_API_KEY",
    },
    "seedance": {
        "label": "Seedance", "description": "Video generation via MUAPI",
        "cred_label": "MUAPI API Key (used for Seedance)",
        "instructions": (
            "Seedance is accessed via MUAPI. "
            "Go to muapi.ai → Dashboard → API Keys → Create Key and paste it below. "
            "This is the same key as MUAPI — you only need to set it once."
        ),
        "placeholder": "mu-...",
        "env_var": "MUAPI_API_KEY",
    },
    "veo": {
        "label": "Veo", "description": "Video generation",
        "cred_label": "MUAPI API Key (used for Veo)",
        "instructions": (
            "Veo is accessed via MUAPI. "
            "Go to muapi.ai → Dashboard → API Keys → Create Key and paste it below."
        ),
        "placeholder": "mu-...",
        "env_var": "MUAPI_API_KEY",
    },
    "kling": {
        "label": "Kling", "description": "Video generation",
        "cred_label": "MUAPI API Key (used for Kling)",
        "instructions": (
            "Kling is accessed via MUAPI. "
            "Go to muapi.ai → Dashboard → API Keys → Create Key and paste it below."
        ),
        "placeholder": "mu-...",
        "env_var": "MUAPI_API_KEY",
    },
    "runway": {
        "label": "Runway", "description": "Video generation",
        "cred_label": "MUAPI API Key (used for Runway)",
        "instructions": (
            "Runway is accessed via MUAPI. "
            "Go to muapi.ai → Dashboard → API Keys → Create Key and paste it below."
        ),
        "placeholder": "mu-...",
        "env_var": "MUAPI_API_KEY",
    },
    "freepik": {
        "label": "Freepik", "description": "Image generation",
        "cred_label": "Freepik API Key",
        "instructions": (
            "Go to freepik.com → API → Your API key. "
            "Paste the key below. Used for image generation tasks."
        ),
        "placeholder": "fpk-...",
        "env_var": "FREEPIK_API_KEY",
    },
    "seedance_official": {
        "label": "Seedance 2.0 (BytePlus ModelArk)",
        "description": "Official Seedance 2.0 video generation via BytePlus ModelArk",
        "cred_label": "BytePlus ModelArk API Key",
        "instructions": (
            "Go to the BytePlus ModelArk console → API Keys → Create Key. "
            "This is the direct official API for Seedance 2.0 — separate from MUAPI. "
            "Stored as BYTEPLUS_ARK_API_KEY. "
            "Set BYTEPLUS_ARK_MODEL to the Seedance model ID from your console "
            "(default: seedance-1-lite)."
        ),
        "placeholder": "ark-...",
        "env_var": "BYTEPLUS_ARK_API_KEY",
    },
    "runway_official": {
        "label": "Runway (Official)",
        "description": "Runway ML Gen3 official video generation API",
        "cred_label": "Runway API Key",
        "instructions": (
            "Go to app.runwayml.com → Settings → API Keys → Create a new key. "
            "Stored as RUNWAY_API_KEY. Used for direct Gen3 Turbo video generation "
            "without MUAPI as intermediary."
        ),
        "placeholder": "key-...",
        "env_var": "RUNWAY_API_KEY",
    },
    "kling_official": {
        "label": "Kling (Official)",
        "description": "Kling AI official video generation API (Kuaishou)",
        "cred_label": "Kling API Key",
        "instructions": (
            "Go to klingai.com → API → Manage API → Create Key. "
            "Enter your Access Key ID and Access Key Secret separated by a colon: "
            "'access_key_id:access_key_secret'. "
            "Stored as KLING_API_KEY. JWT authentication is generated automatically."
        ),
        "placeholder": "key_id:key_secret",
        "env_var": "KLING_API_KEY",
    },
    "veo_official": {
        "label": "Veo (Google)",
        "description": "Google Veo video generation via Gemini API",
        "cred_label": "Google API Key",
        "instructions": (
            "Go to aistudio.google.com → Get API key → Create API key. "
            "Stored as GOOGLE_API_KEY. Used for Google Veo video generation "
            "and Gemini model access."
        ),
        "placeholder": "AIza...",
        "env_var": "GOOGLE_API_KEY",
    },
    "facebook": {
        "label": "Facebook",
        "description": "Facebook video publishing via Meta Graph API",
        "cred_label": "Meta Access Token",
        "instructions": (
            "Generate a long-lived Page access token from Meta for Developers "
            "(developers.facebook.com → Tools → Graph API Explorer). "
            "Requires META_APP_ID and META_FACEBOOK_PAGE_ID to publish. "
            "Stored as META_ACCESS_TOKEN. Also set META_APP_ID and META_FACEBOOK_PAGE_ID "
            "in your environment or via their own Secret Profiles."
        ),
        "placeholder": "EAA...",
        "env_var": "META_ACCESS_TOKEN",
    },
    "instagram": {
        "label": "Instagram",
        "description": "Instagram Reels publishing via Meta Graph API",
        "cred_label": "Meta Access Token",
        "instructions": (
            "Generate a long-lived Instagram Page access token from Meta for Developers. "
            "Same token works for both Facebook and Instagram if you have correct permissions. "
            "Also set META_INSTAGRAM_ACCOUNT_ID and META_APP_ID. "
            "Stored as META_ACCESS_TOKEN."
        ),
        "placeholder": "EAA...",
        "env_var": "META_ACCESS_TOKEN",
    },
    "tiktok": {
        "label": "TikTok",
        "description": "TikTok video publishing via TikTok for Developers",
        "cred_label": "TikTok Access Token",
        "instructions": (
            "Generate an access token from developers.tiktok.com → Manage Apps → "
            "your app → Authorization. "
            "Stored as TIKTOK_ACCESS_TOKEN. Also set TIKTOK_CLIENT_KEY and "
            "TIKTOK_CLIENT_SECRET in your environment."
        ),
        "placeholder": "act.xyz...",
        "env_var": "TIKTOK_ACCESS_TOKEN",
    },
}

_VALID_CONTENT_PROVIDERS = frozenset(_CONTENT_PROVIDER_SETUP)

_PLATFORM_PROVIDERS = [
    {"id": "youtube",   "label": "YouTube",   "auth": "oauth2",
     "description": "Video publishing",
     "upload_implemented": True},
    {"id": "tiktok",    "label": "TikTok",    "auth": "oauth2",
     "description": "Short video publishing",
     "upload_implemented": True,
     "test_note": "Availability checked at publish time via creator info API"},
    {"id": "instagram", "label": "Instagram", "auth": "oauth2_meta",
     "description": "Reels via Meta Graph API",
     "upload_implemented": True,
     "requires": ["META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN",
                  "META_INSTAGRAM_ACCOUNT_ID"]},
    {"id": "facebook",  "label": "Facebook",  "auth": "oauth2_meta",
     "description": "Video posts via Meta Graph API",
     "upload_implemented": True,
     "requires": ["META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN",
                  "META_FACEBOOK_PAGE_ID"]},
]


def _build_providers(provider_list, creds_map, cost_map, verif_map=None):
    verif_map = verif_map or {}

    # Pull account/channel IDs and OAuth client ID from config once (best-effort)
    import os as _os
    try:
        from hypogeum.config import Config as _Cfg
        _cfg = _Cfg()
        _yt_client_id    = _cfg.YOUTUBE_CLIENT_ID or ""
        _yt_channel_id   = _cfg.YOUTUBE_CHANNEL_ID or ""
        _fb_page_id      = _cfg.META_FACEBOOK_PAGE_ID or ""
        _ig_account_id   = _cfg.META_INSTAGRAM_ACCOUNT_ID or ""
        _meta_app_id     = _cfg.META_APP_ID or ""
    except Exception:
        _yt_client_id = _yt_channel_id = _fb_page_id = _ig_account_id = _meta_app_id = ""
    # TikTok credentials: read directly from .env so we see actual file values,
    # not stale os.environ entries (Config._load_dotenv skips already-set keys).
    def _env_file_val(key: str) -> str:
        try:
            from hypogeum.db import _PROJECT_ROOT as _pr
            env_path = _pr / ".env"
            if env_path.exists():
                for _line in env_path.read_text().splitlines():
                    _line = _line.strip()
                    if _line.startswith("#") or "=" not in _line:
                        continue
                    _k, _, _v = _line.partition("=")
                    if _k.strip() == key:
                        return _v.strip().split(" #")[0].strip().strip('"\'')
        except Exception:
            pass
        return _os.environ.get(key, "").strip()

    _tk_client_key    = _env_file_val("TIKTOK_CLIENT_KEY")
    _tk_client_secret = _env_file_val("TIKTOK_CLIENT_SECRET")
    _tk_access_token  = _env_file_val("TIKTOK_ACCESS_TOKEN")
    _tk_open_id       = _env_file_val("TIKTOK_OPEN_ID")

    result = []
    for p in provider_list:
        entry = dict(p)
        muapi_backed = bool(p.get("muapi_backed"))
        entry["muapi_backed"] = muapi_backed

        # For MUAPI-backed providers, derive status from MUAPI credential
        lookup_id = "muapi" if muapi_backed else p["id"]
        cred = creds_map.get(lookup_id)
        entry["credential_label"] = getattr(cred, "label", None) if cred else None
        entry["cost_usd"] = cost_map.get(p["id"], 0.0)

        # Per-platform supplemental fields (account IDs, OAuth client ID)
        if p["id"] == "youtube":
            entry["oauth_client_id"]    = _yt_client_id
            entry["oauth_redirect_uri"] = "http://localhost:8080/oauth2callback"
            entry["account_id"]         = _yt_channel_id
            entry["missing_reqs"]       = []  # filled after vstatus is resolved below
        elif p["id"] == "facebook":
            entry["account_id"]   = _fb_page_id
            entry["meta_app_id"]  = _meta_app_id
            entry["missing_reqs"] = [name for name, bad in (
                ("META_APP_ID",           not _meta_app_id),
                ("META_APP_SECRET",       False),
                ("META_FACEBOOK_PAGE_ID", not _fb_page_id),
            ) if bad]
        elif p["id"] == "instagram":
            entry["account_id"]   = _ig_account_id
            entry["meta_app_id"]  = _meta_app_id
            entry["missing_reqs"] = [name for name, bad in (
                ("META_APP_ID",               not _meta_app_id),
                ("META_APP_SECRET",           False),
                ("META_INSTAGRAM_ACCOUNT_ID", not _ig_account_id),
            ) if bad]
        elif p["id"] == "tiktok":
            entry["account_id"]    = _tk_open_id  # open_id is the TikTok account identifier
            entry["missing_reqs"]  = [name for name, bad in (
                ("TIKTOK_CLIENT_KEY",    not _tk_client_key),
                ("TIKTOK_CLIENT_SECRET", not _tk_client_secret),
                ("TIKTOK_ACCESS_TOKEN",  not _tk_access_token),
                ("TIKTOK_OPEN_ID",       not _tk_open_id),
            ) if bad]
            # Compute TikTok source accurately — verifier only checks ACCESS_TOKEN
            # so its source fallback "env" is misleading when only client creds exist.
            if _tk_access_token:
                entry["_tiktok_source_override"] = "env"
            elif _tk_client_key or _tk_client_secret:
                entry["_tiktok_source_override"] = "partial"
            else:
                entry["_tiktok_source_override"] = ""

        # Verification status (replaces simple connected bool)
        v = verif_map.get(lookup_id)
        if muapi_backed:
            # MUAPI-backed: status comes from MUAPI probe; never show "missing" for the sub-provider
            muapi_cred = creds_map.get("muapi")
            if v and v.get("status") == "verified":
                vstatus = "verified"
            elif muapi_cred is not None:
                vstatus = "muapi_configured"
            else:
                vstatus = "missing"
        else:
            if v:
                vstatus = v["status"]
            elif cred is not None:
                vstatus = "saved"
            else:
                vstatus = "missing"

        # Fix missing_reqs for youtube now that vstatus is known
        if p["id"] == "youtube":
            entry["missing_reqs"] = [r for r, bad in (
                ("YOUTUBE_CLIENT_ID",     not _yt_client_id),
                ("YOUTUBE_CLIENT_SECRET", False),
                ("YOUTUBE_REFRESH_TOKEN", vstatus == "missing"),
            ) if bad]

        entry["vstatus"] = vstatus  # verified|failed|not_verifiable|saved|missing|muapi_configured
        entry["verif_error"]            = (v or {}).get("error_message") or (v or {}).get("error")
        entry["verif_checked_at"]       = (v or {}).get("checked_at")
        entry["verif_latency_ms"]       = (v or {}).get("latency_ms")
        entry["verif_source"]           = (v or {}).get("source", "")
        entry["verif_has_secret_profile"] = bool((v or {}).get("has_secret_profile"))
        entry["verif_has_env_var"]      = bool((v or {}).get("has_env_var"))
        # Apply TikTok source override — verifier only knows ACCESS_TOKEN; we know more
        _src_override = entry.pop("_tiktok_source_override", None)
        if _src_override is not None:
            entry["verif_source"] = _src_override
        # Legacy: True only when verified (not just saved)
        entry["connected"] = vstatus == "verified"
        # Distinguish real network probes from format/string checks.
        # youtube probe only checks token string length, not API reachability.
        # facebook/instagram/tiktok now have Meta Graph API / TikTok API probes (Phase 1J).
        real_probe_providers = {
            "openrouter", "elevenlabs", "muapi",
            "seedance", "runway", "kling", "veo",
            "seedance_official", "runway_official", "kling_official", "veo_official",
            "freepik", "facebook", "instagram", "tiktok",
        }
        entry["real_probe"] = p["id"] in real_probe_providers
        result.append(entry)
    return result


@app.route("/providers/models")
def providers_models_page():
    """The tracked generation-model catalogue (Factory Capability Evolution
    substrate) — lifecycle status + recent price changes."""
    from hypogeum import model_registry as _mr
    models = _mr.list_models(db_path=_db_path())

    # Effective (actually-billed) price per model, from real charge history —
    # the listed catalogue price is only what Cortex books, and a model with
    # dynamic pricing lists a floor, not what we pay (e.g. wan2.7 lists $0.10,
    # bills $0.65). See hypogeum.model_effective_pricing.
    try:
        from hypogeum.model_effective_pricing import effective_prices, price_view
        _spend = effective_prices(db_path=_db_path())
    except Exception:  # noqa: BLE001
        _spend, price_view = {}, None
    price_drift = []
    for m in models:
        if price_view is not None:
            pv = price_view(m["model_id"], m.get("price_usd"),
                            spend=_spend.get(m["model_id"]))
            m["price"] = pv
            if pv["flag"]:
                price_drift.append({"model_id": m["model_id"], **pv})
        else:
            m["price"] = {"label": (f"${m['price_usd']:.2f}" if m.get("price_usd") is not None else "—"),
                          "basis": "listed", "flag": False, "n_calls": 0,
                          "listed": m.get("price_usd"), "effective": None}
    price_drift.sort(key=lambda x: (x.get("drift_pct") or 0), reverse=True)

    # Models we've actually paid for that aren't in the MUAPI catalogue —
    # OpenRouter (scripts/planning), ElevenLabs (voice/music), Perplexity
    # (research). So the price view covers every tool, not just video/images.
    _registry_ids = {m["model_id"] for m in models}
    other_spend = []
    for mid, s in sorted(_spend.items(), key=lambda x: -x[1].total_charged):
        if mid in _registry_ids:
            continue
        d = s.as_dict()
        d["label"] = (f"from ${s.min_charge:.3f}" if s.min_charge != s.max_charge
                      else f"${s.effective_price:.3f}") if s.effective_price is not None else "—"
        other_spend.append(d)

    by_life: dict = {}
    for m in models:
        by_life.setdefault(m["lifecycle_status"], []).append(m)
    changed = sorted(
        [m for m in models if m.get("price_changed_at")],
        key=lambda m: m["price_changed_at"], reverse=True)[:20]
    if request.args.get("sync") == "1":
        _mr.sync_from_muapi(db_path=_db_path())
        return redirect("/providers/models")
    try:
        from hypogeum.provider_evidence import leaderboard as _lb
        evidence = _lb(source="bakeoff", min_n=1, db_path=_db_path())
    except Exception:  # noqa: BLE001
        evidence = []
    return render_template(
        "providers_models.html",
        models=models, by_life=by_life, changed=changed, evidence=evidence,
        price_drift=price_drift, other_spend=other_spend,
        total=len(models),
        last_synced=(max((m["last_synced_at"] for m in models), default=None)),
    )


@app.route("/providers")
def providers_page():
    db = _db_path()

    # Build provider_catalog (new data model — drives the UI)
    try:
        from hypogeum.dashboard_api import get_provider_catalog
        catalog = get_provider_catalog(db_path=db)
    except Exception:
        catalog = []
    by_id = {e.provider: e for e in catalog}

    # AI image providers — the four image-generation providers
    _AI_IMAGE_ORDER = ("openai", "openrouter", "muapi", "freepik")
    ai_image_providers = [by_id[p] for p in _AI_IMAGE_ORDER if p in by_id]

    # AI audio providers — separate section
    _AI_AUDIO_ORDER = ("elevenlabs",)
    ai_audio_providers = [by_id[p] for p in _AI_AUDIO_ORDER if p in by_id]

    # Legacy alias kept so any template references to ai_providers still resolve
    ai_providers = ai_image_providers

    # Video providers — paired: (muapi_delegated, direct_placeholder)
    _VIDEO_NAMES = ("seedance", "veo", "kling", "runway")
    video_provider_pairs = []
    for name in _VIDEO_NAMES:
        pair = {
            "name":      name,
            "delegated": by_id.get(name),
            "official":  by_id.get(f"{name}_official"),
        }
        if pair["delegated"] or pair["official"]:
            video_provider_pairs.append(pair)

    # Publishing — keep the existing _build_providers path (OAuth complexity)
    try:
        all_secrets = list_secret_profiles(db_path=db)
        creds_map = {s.provider: s for s in all_secrets if s.status == "active"}
    except Exception:
        creds_map = {}
    try:
        from hypogeum.dashboard_api import get_cost_summary
        cost_map = get_cost_summary(db_path=db).by_provider
    except Exception:
        cost_map = {}
    try:
        verif_map = get_all_provider_statuses(db_path=db)
    except Exception:
        verif_map = {}
    platform_providers = _build_providers(list(_PLATFORM_PROVIDERS), creds_map, cost_map, verif_map)
    factory_accounts = _factory_provider_accounts()

    # Phase 1G/1J — most recent comparison results
    try:
        last_comparison = get_comparison_result(db_path=db,
                                                comparison_type=COMPARISON_TYPE_IMAGE)
    except Exception:
        last_comparison = None
    try:
        last_video_check = get_comparison_result(db_path=db,
                                                 comparison_type=COMPARISON_TYPE_VIDEO)
    except Exception:
        last_video_check = None

    # Providers with Secret Profile connect forms in the Publishing section
    publishing_sp_providers = {"facebook", "instagram", "tiktok"}

    return render_template(
        "providers.html",
        ai_providers=ai_providers,               # legacy alias = ai_image_providers
        ai_image_providers=ai_image_providers,
        ai_audio_providers=ai_audio_providers,
        video_provider_pairs=video_provider_pairs,
        platform_providers=platform_providers,
        factory_accounts=factory_accounts,
        nav_channels=_nav_channels(),
        connectable_providers=_VALID_CONTENT_PROVIDERS,
        publishing_sp_providers=publishing_sp_providers,
        last_comparison=last_comparison,
        comparison_prompt=COMPARISON_PROMPT,
        last_video_check=last_video_check,
        default_image_providers=DEFAULT_IMAGE_PROVIDERS,
        default_video_providers=DEFAULT_VIDEO_PROVIDERS,
    )


# ===========================================================================
# Content Provider Connect (API key entry for AI content providers)
# ===========================================================================

@app.route("/providers/content/connect/<provider>")
def content_provider_connect_page(provider: str):
    if provider not in _VALID_CONTENT_PROVIDERS:
        return render_template("not_found.html", kind="Provider", id=provider), 404
    setup = _CONTENT_PROVIDER_SETUP[provider]
    flash = request.args.get("saved")
    return render_template(
        "connect_content_provider.html",
        provider=provider,
        setup=setup,
        flash=flash,
    )


@app.route("/providers/content/connect/<provider>/save", methods=["POST"])
def content_provider_connect_save(provider: str):
    if provider not in _VALID_CONTENT_PROVIDERS:
        return render_template("not_found.html", kind="Provider", id=provider), 404
    cred  = request.form.get("credential", "").strip()
    label = request.form.get("label", "").strip()
    setup = _CONTENT_PROVIDER_SETUP[provider]
    if not label:
        label = f"{setup['label']} API key"
    db = _db_path()
    if cred:
        try:
            existing = list_secret_profiles(provider=provider, db_path=db)
            active   = [p for p in existing if p.status == "active"]
            if active:
                update_secret_profile(
                    active[0].secret_profile_id,
                    credential_data=cred,
                    label=label,
                    db_path=db,
                )
            else:
                create_secret_profile(
                    provider=provider,
                    label=label,
                    credential_type="api_key",
                    credential_data=cred,
                    db_path=db,
                )
        except Exception:
            pass
    return redirect(
        url_for("content_provider_connect_page", provider=provider) + "?saved=1"
    )


# ===========================================================================
# Factory Setup / Production Preflight
# ===========================================================================

@app.route("/setup")
def setup_page():
    db = _db_path()
    preflight = get_setup_preflight_status(db_path=db)
    try:
        provider_statuses = get_all_provider_statuses(db_path=db)
    except Exception:
        provider_statuses = {}
    return render_template("setup.html", preflight=preflight,
                           provider_statuses=provider_statuses)


@app.route("/api/setup/preflight")
def api_setup_preflight():
    try:
        return jsonify(get_setup_preflight_status(db_path=_db_path()))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


@app.route("/api/observability/health")
def api_observability_health():
    """
    Runtime Observability health endpoint (Phase 3 Production Hardening,
    P0.3). Thin wrapper — all detection logic lives in
    hypogeum.observability.run_all_checks(); this route only exposes it.
    Any unhealthy check has already emitted an OIS signal by the time
    this returns.
    """
    try:
        from hypogeum.observability import run_all_checks
        report = run_all_checks(db_path=_db_path())
        return jsonify(report), (200 if report["healthy"] else 503)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


# ---------------------------------------------------------------------------
# Factory monthly budget ceiling (DB-backed, live-reloadable)
# ---------------------------------------------------------------------------
#
# The DB-backed factory_policies.monthly_ceiling_usd control, distinct
# from /api/setup/budget-ceiling above (which writes FINANCE_BUDGET_CEILING_USD
# to .env, the bootstrap/fallback tier). This is the operator-facing
# production control per policy_cascade.py's own documented precedence
# ("factory_policies -> runtime env"): a change here is read fresh from
# the DB by check_run_clearance() on the worker's very next iteration —
# no worker restart needed, unlike the .env route. Setting the value to
# 0 restores env-fallback behavior exactly.
#
# "Authenticated" per the operator identity field below: this dashboard
# has no login/session system for ANY existing route (confirmed — none
# exists anywhere in this file). Building one exists outside this
# feature's scope ("no broad Dashboard redesign"). operator is an
# optional, self-reported identity label recorded in the audit trail
# "when available" — not cryptographic authentication. This matches the
# existing dashboard's security posture exactly; it does not introduce a
# weaker model than what every other route already has.

_DEFAULT_FACTORY_ID = "hypogeum"


@app.route("/api/factory/budget-ceiling", methods=["GET"])
def api_factory_budget_ceiling_get():
    try:
        from hypogeum.factory_director import get_budget_status
        status = get_budget_status(db_path=_db_path(), factory_id=_DEFAULT_FACTORY_ID)
        status["factory_id"] = _DEFAULT_FACTORY_ID
        return jsonify(status), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


@app.route("/api/factory/budget-ceiling", methods=["POST"])
def api_factory_budget_ceiling_post():
    body = request.get_json(silent=True) or {}

    reason = (body.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "reason is required"}), 400

    raw_value = body.get("monthly_ceiling_usd")
    try:
        new_ceiling = float(raw_value)
    except (TypeError, ValueError):
        return jsonify({"error": "monthly_ceiling_usd must be a number"}), 400

    operator = (body.get("operator") or "operator").strip() or "operator"

    try:
        from hypogeum.factory_director import get_budget_status
        from hypogeum.factory_entity import get_factory_policies, update_factory_policies

        db_path = _db_path()
        previous_ceiling = float(
            get_factory_policies(_DEFAULT_FACTORY_ID, db_path=db_path).get("monthly_ceiling_usd") or 0
        )

        update_factory_policies(
            factory_id=_DEFAULT_FACTORY_ID,
            fields={"monthly_ceiling_usd": new_ceiling},
            reason=reason,
            triggered_by=operator,
            metadata={"source": "dashboard", "route": "/api/factory/budget-ceiling"},
            db_path=db_path,
        )

        status = get_budget_status(db_path=db_path, factory_id=_DEFAULT_FACTORY_ID)
        status["factory_id"] = _DEFAULT_FACTORY_ID
        status["previous_ceiling_usd"] = round(previous_ceiling, 2)
        status["new_ceiling_usd"] = round(new_ceiling, 2)
        return jsonify(status), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


# ---------------------------------------------------------------------------
# Budget ceiling write-back
# ---------------------------------------------------------------------------

_ENV_PATH = _ROOT / ".env"
_BUDGET_KEY = "FINANCE_BUDGET_CEILING_USD"


def _update_budget_ceiling(new_ceiling: float) -> dict:
    """
    Write FINANCE_BUDGET_CEILING_USD to .env and update os.environ.

    Replaces the existing line in .env (preserving comments) or appends.
    Returns {"ok": True, "new_ceiling": float} on success.
    """
    if new_ceiling < 0 or new_ceiling > 9999:
        return {"ok": False, "error": "ceiling must be between 0 and 9999"}

    # Read existing .env
    try:
        if _ENV_PATH.exists():
            lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()
        else:
            lines = []
    except Exception as exc:
        return {"ok": False, "error": f"Cannot read .env: {exc}"}

    new_line = f"{_BUDGET_KEY}={new_ceiling:.2f}"
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"{_BUDGET_KEY}="):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)

    try:
        _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "error": f"Cannot write .env: {exc}"}

    # Update running process immediately (no restart needed)
    os.environ[_BUDGET_KEY] = f"{new_ceiling:.2f}"

    return {"ok": True, "new_ceiling": new_ceiling}


@app.route("/api/setup/budget-ceiling", methods=["POST"])
def api_setup_budget_ceiling():
    body = request.get_json(silent=True) or {}
    try:
        new_ceiling = float(body.get("ceiling", -1))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "ceiling must be a number"}), 400
    result = _update_budget_ceiling(new_ceiling)
    return jsonify(result), 200 if result["ok"] else 400


# ---------------------------------------------------------------------------
# Provider verification
# ---------------------------------------------------------------------------

@app.route("/api/providers/muapi/smoke-test", methods=["POST"])
def api_muapi_smoke_test():
    """
    Phase 1L: Re-run the MUAPI generation smoke test.

    Submits a minimal T3 job (cheapest model, dry-probe prompt), polls for
    completion, and writes an updated provider_live_smoke_report.json.

    Credentials resolved via Secret Profile → env fallback.
    Returns {ok, status, model, latency_ms, endpoint, error}.
    """
    import json as _json
    import time as _time
    from pathlib import Path as _Path
    from datetime import datetime, timezone
    from hypogeum.credential_manager import list_secret_profiles
    from hypogeum.config import Config

    # Resolve MUAPI key
    credential = None
    source = "env"
    try:
        profiles = list_secret_profiles(provider="muapi", db_path=_db_path())
        active = [p for p in profiles if p.status == "active"]
        if active:
            credential = active[0].credential_data
            source = "secret_profile"
    except Exception:
        pass
    if not credential:
        import os
        credential = os.environ.get("MUAPI_API_KEY", "").strip() or None

    if not credential:
        return jsonify({"ok": False, "error": "No MUAPI credential found"}), 400

    # Load config to get model defaults
    try:
        cfg = Config()
        cfg.MUAPI_API_KEY = credential
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Config error: {exc}"}), 500

    import requests as _requests
    t3_model = cfg.MUAPI_T3_MODEL
    endpoint  = f"{cfg.MUAPI_BASE_URL}/{t3_model}"
    prompt    = "abstract atmospheric fog, dark background"

    t0 = _time.monotonic()
    try:
        resp = _requests.post(
            endpoint,
            headers={"x-api-key": credential, "Content-Type": "application/json"},
            json={"prompt": prompt, "aspect_ratio": "16:9"},
            timeout=30,
        )
        latency_ms = int((_time.monotonic() - t0) * 1000)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "latency_ms": int((_time.monotonic() - t0) * 1000)}), 500

    status_code = resp.status_code
    try:
        data = resp.json()
    except Exception:
        data = {}

    request_id = data.get("request_id", "")
    smoke_status = "success" if request_id else f"error_submit_http{status_code}"
    error_msg = None if request_id else (data.get("error") or data.get("message") or f"HTTP {status_code}")

    # Write updated smoke report
    report_dir  = _ROOT / ".hypogeum-assets" / "provider_smoke_tests"
    report_path = report_dir / "provider_live_smoke_report.json"
    report_dir.mkdir(parents=True, exist_ok=True)
    try:
        existing = _json.loads(report_path.read_text()) if report_path.exists() else {}
    except Exception:
        existing = {}
    existing.setdefault("providers", {})
    existing["generated_at"] = datetime.now(timezone.utc).isoformat()
    existing["providers"]["muapi"] = {
        "provider":   "muapi",
        "status":     smoke_status,
        "model":      t3_model,
        "endpoint":   endpoint,
        "latency_ms": latency_ms,
        "error":      error_msg,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        report_path.write_text(_json.dumps(existing, indent=2))
    except Exception:
        pass

    return jsonify({
        "ok":         bool(request_id),
        "status":     smoke_status,
        "model":      t3_model,
        "endpoint":   endpoint,
        "latency_ms": latency_ms,
        "error":      error_msg,
        "credential_source": source,
        "request_id": request_id,
    })


@app.route("/api/providers/verify/<provider>", methods=["POST"])
def api_providers_verify(provider: str):
    known = (
        set(p["id"] for p in _CONTENT_PROVIDERS) |
        set(p["id"] for p in _PLATFORM_PROVIDERS)
    )
    if provider not in known:
        return jsonify({"ok": False, "error": f"unknown provider {provider!r}"}), 404
    try:
        result = verify_provider(provider, db_path=_db_path())
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/providers/compare", methods=["POST"])
def api_providers_compare():
    """
    Phase 1G/1J: Run a controlled thumbnail generation comparison across image providers.

    Credentials resolved via Secret Profile → env fallback per provider.
    Executes synchronously (generation takes 15–60s per provider).
    Returns the full result set when done.
    """
    body = request.get_json(silent=True) or {}
    providers = body.get("providers") or DEFAULT_IMAGE_PROVIDERS
    valid = set(DEFAULT_IMAGE_PROVIDERS)
    providers = [p for p in providers if p in valid]
    if not providers:
        return jsonify({"ok": False, "error": "no valid providers specified"}), 400
    try:
        run = run_provider_comparison(db_path=_db_path(), providers=providers)
        return jsonify({
            "ok":           True,
            "run_id":       run.run_id,
            "run_at":       run.run_at,
            "completed_at": run.completed_at,
            "prompt":       run.prompt,
            "results":      run.results,
            "comparison_type": COMPARISON_TYPE_IMAGE,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/providers/compare/video", methods=["POST"])
def api_providers_compare_video():
    """
    Phase 1J: Video provider readiness check.

    Probes credentials for all configured video providers without generating any video.
    Returns credential_status, operational_readiness, model, latency, cost estimate.
    """
    body = request.get_json(silent=True) or {}
    providers = body.get("providers") or DEFAULT_VIDEO_PROVIDERS
    valid = set(DEFAULT_VIDEO_PROVIDERS)
    providers = [p for p in providers if p in valid]
    if not providers:
        return jsonify({"ok": False, "error": "no valid video providers specified"}), 400
    try:
        run = run_video_readiness_check(db_path=_db_path(), providers=providers)
        return jsonify({
            "ok":           True,
            "run_id":       run.run_id,
            "run_at":       run.run_at,
            "completed_at": run.completed_at,
            "prompt":       run.prompt,
            "results":      run.results,
            "comparison_type": COMPARISON_TYPE_VIDEO,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/providers/compare/video/results")
def api_providers_compare_video_results():
    """Return the most recent completed video readiness check from DB."""
    try:
        result = get_comparison_result(db_path=_db_path(),
                                       comparison_type=COMPARISON_TYPE_VIDEO)
        if result is None:
            return jsonify({"ok": True, "run": None})
        return jsonify({"ok": True, "run": result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500



# ---------------------------------------------------------------------------
# Video Provider Test — read-only inspection of generated clips
# ---------------------------------------------------------------------------

# In-memory cache: {episode_id: {"clips": [...], "computed_at": iso}}
_VIDEO_TEST_CACHE: dict = {}


def _compute_video_test(episode_id: str) -> dict | None:
    """
    Build per-clip analysis for the Video Provider Test page.
    Reads: visual_package, narration_package, clip_catalog.jsonl, clip files.
    Runs: ffprobe (duration), ffmpeg freezedetect (freeze seconds).
    Returns None if episode dir doesn't exist.
    Never raises.
    """
    import subprocess as _sp, re as _re, json as _json, math as _math
    from datetime import datetime, timezone

    ep_dir = _ASSETS_ROOT_PATH / "episodes" / episode_id
    if not ep_dir.exists():
        return None

    def _load(name):
        p = ep_dir / f"{name}_{episode_id}.json"
        try:
            return _json.loads(p.read_text()) if p.exists() else {}
        except Exception:
            return {}

    def _ffprobe_dur(path) -> float | None:
        try:
            r = _sp.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                        capture_output=True, text=True, timeout=15)
            return round(float(r.stdout.strip()), 3) if r.stdout.strip() else None
        except Exception:
            return None

    def _freeze_seconds(path, dur: float) -> tuple[float, float]:
        """Return (freeze_s, motion_score 0-100). Uses ffmpeg freezedetect."""
        try:
            r = _sp.run(
                ["ffmpeg", "-i", str(path),
                 "-vf", "freezedetect=n=0.001:d=0.1",
                 "-f", "null", "-"],
                capture_output=True, text=True, timeout=30)
            stderr = r.stderr
            starts = [float(m) for m in _re.findall(r"freeze_start:\s*([\d.]+)", stderr)]
            ends   = [float(m) for m in _re.findall(r"freeze_end:\s*([\d.]+)", stderr)]
            total  = sum(e - s for s, e in zip(starts, ends))
            if len(starts) > len(ends) and dur:
                total += dur - starts[-1]
            total = round(max(0.0, total), 2)
            motion = round(max(0.0, (1.0 - total / dur) * 100)) if dur else 0
            return total, motion
        except Exception:
            return 0.0, 100

    # Tier → model → cost mapping (from finance records)
    _TIER_MODEL = {
        "1": "veo3-text-to-video",
        "2": "veo3-fast-text-to-video",
        "3": "runway-image-to-video",
    }
    _TIER_COST = {"1": 2.50, "2": 0.60, "3": 0.25}

    # Load packages
    vp    = _load("visual_package")
    np_d  = _load("narration_package")
    vp_scenes = vp.get("scenes", [])
    segs  = {s["beat_reference"]: s["duration_seconds"]
             for s in np_d.get("narration_segments", [])}

    # Load clip catalog
    cat_path = _ASSETS_ROOT_PATH / "catalogs" / "clip_catalog.jsonl"
    clip_cat: dict[str, dict] = {}
    if cat_path.exists():
        for line in cat_path.read_text().splitlines():
            try:
                e = _json.loads(line)
                if e.get("source_episode_id") == episode_id:
                    clip_cat[e["scene_id"]] = e
            except Exception:
                pass

    clips_dir = ep_dir / "video"
    clips_out = []
    total_cost = 0.0

    for i, scene in enumerate(vp_scenes):
        sid   = scene["scene_id"]
        beat  = f"b_{i+1:03d}"
        ps    = scene["production_spec"]["provider_routing"]
        vs    = scene["visual_spec"]
        tier  = str(ps.get("primary_tier", "2"))
        model = _TIER_MODEL.get(tier, "unknown")
        cost  = _TIER_COST.get(tier, 0.0)
        provider   = ps.get("primary_provider", "muapi")
        prompt     = ps.get("primary_prompt", "")
        fb_prompt  = ps.get("fallback_prompt", "")
        nar_dur    = segs.get(beat, 0.0)
        cat_entry  = clip_cat.get(sid, {})

        clip_file = clips_dir / f"sc_{sid}.mp4"
        exists    = clip_file.exists()

        dur = freeze_s = motion_score = None
        if exists:
            dur       = _ffprobe_dur(clip_file)
            freeze_s, motion_score = _freeze_seconds(clip_file, dur or 0)
            total_cost += cost

        # Loop risk
        loops = round(nar_dur / dur, 2) if (dur and dur > 0) else None
        if loops is None:
            loop_risk = "n/a"
        elif loops >= 4:
            loop_risk = "high"
        elif loops >= 2:
            loop_risk = "medium"
        else:
            loop_risk = "low"

        # Per-clip pass/fail
        fails = []
        if not exists:
            fails.append("clip missing")
        elif freeze_s and freeze_s > 0.5:
            fails.append(f"freeze {freeze_s}s")
        if loop_risk == "high":
            fails.append(f"loop {loops}×")
        clip_verdict = "pass" if (exists and not fails) else ("fail" if not exists else "warn")

        clips_out.append({
            "scene_id":     sid,
            "scene_index":  i,
            "tier":         tier,
            "model":        model,
            "provider":     provider,
            "catalog_model": cat_entry.get("model") or "",
            "prompt":       prompt,
            "fallback_prompt": fb_prompt,
            "subject":      vs.get("subject", ""),
            "motion_type":  vs.get("motion_type", ""),
            "duration":     dur,
            "expected_dur": scene["production_spec"].get("duration_seconds", 8),
            "nar_dur":      round(nar_dur, 2),
            "freeze_s":     freeze_s,
            "motion_score": motion_score,
            "loops":        loops,
            "loop_risk":    loop_risk,
            "cost_usd":     cost,
            "file":         clip_file.name if exists else None,
            "exists":       exists,
            "clip_verdict": clip_verdict,
            "fail_reasons": fails,
            "indexed_at":   cat_entry.get("indexed_at", ""),
        })

    present     = [c for c in clips_out if c["exists"]]
    missing     = [c for c in clips_out if not c["exists"]]
    high_risk   = [c for c in present if c["loop_risk"] == "high"]
    med_risk    = [c for c in present if c["loop_risk"] == "medium"]
    low_risk    = [c for c in present if c["loop_risk"] == "low"]
    frozen      = [c for c in present if c["freeze_s"] and c["freeze_s"] > 0.5]
    total_freeze = round(sum(c["freeze_s"] or 0 for c in present), 1)

    return {
        "episode_id":        episode_id,
        "scene_count":       len(vp_scenes),
        "clips_present":     len(present),
        "clips_missing":     len(missing),
        "total_cost_usd":    round(total_cost, 2),
        "planned_cost_usd":  round(sum(c["cost_usd"] for c in clips_out), 2),
        "total_freeze_s":    total_freeze,
        "high_risk_count":   len(high_risk),
        "med_risk_count":    len(med_risk),
        "low_risk_count":    len(low_risk),
        "frozen_clip_count": len(frozen),
        "tier_breakdown":    {
            t: sum(1 for c in present if c["tier"] == t)
            for t in ["1", "2", "3"]
        },
        "clips":             clips_out,
        "computed_at":       datetime.now(timezone.utc).isoformat(),
    }


def _local_episodes_with_video() -> list[dict]:
    """Episodes whose video clips are still on local disk (published episodes
    are moved to the archive after release), newest first."""
    root = _ASSETS_ROOT_PATH / "episodes"
    out = []
    if not root.exists():
        return out
    for d in root.iterdir():
        vid = d / "video"
        if not d.is_dir() or not vid.is_dir():
            continue
        clips = list(vid.glob("*.mp4"))
        if not clips:
            continue
        out.append({
            "episode_id": d.name,
            "clip_count": len(clips),
            "mtime": max((c.stat().st_mtime for c in clips), default=0),
        })
    out.sort(key=lambda e: e["mtime"], reverse=True)
    return out


@app.route("/providers/video-test/clips")
def video_test_clips_page():
    """Legacy per-clip inspector for a produced episode (freeze / duration QA)."""
    available = _local_episodes_with_video()
    episode_id = request.args.get("episode") or (
        available[0]["episode_id"] if available else ""
    )
    cached = None
    if episode_id:
        cached = _VIDEO_TEST_CACHE.get(episode_id)
        if not cached:
            cached = _compute_video_test(episode_id)
            if cached:
                _VIDEO_TEST_CACHE[episode_id] = cached
    return render_template(
        "video_test.html",
        data=cached,
        episode_id=episode_id,
        available=available,
    )


@app.route("/providers/video-test")
def video_test_page():
    """Video Model bake-off: one prompt, several MUAPI models, price first."""
    from hypogeum import video_bakeoff as _bo
    from hypogeum.channel_config import list_channel_profiles, get_model_profile

    channels = list_channel_profiles(status="active", db_path=_db_path())
    sel = request.args.get("channel") or _scope_channel() or (
        channels[0].channel_id if channels else "")
    aspect = request.args.get("aspect") or "16:9"

    prompt_pack = None
    current_model = None
    if sel:
        prompt_pack = _bo.build_channel_scene_prompt(sel, aspect_ratio=aspect, db_path=_db_path())
        ch = next((c for c in channels if c.channel_id == sel), None)
        if ch and ch.model_profile_id:
            mp = get_model_profile(ch.model_profile_id, db_path=_db_path())
            current_model = mp.video_model if mp else None

    run = None
    run_id = request.args.get("run")
    if run_id:
        run = _bo.get_bakeoff(run_id)

    return render_template(
        "video_bakeoff.html",
        channels=[{"channel_id": c.channel_id, "name": getattr(c, "name", "") or c.channel_id}
                  for c in channels],
        selected_channel=sel,
        aspect=aspect,
        models=_bo.list_video_models(),
        prompt_pack=prompt_pack,
        current_model=current_model,
        run=run,
        history=_bo.list_bakeoffs(sel or None, limit=10),
        flash=request.args.get("flash"),
    )


@app.route("/api/providers/video-test/estimate", methods=["POST"])
def api_video_bakeoff_estimate():
    from hypogeum import video_bakeoff as _bo
    body = request.get_json(silent=True) or {}
    return jsonify(_bo.estimate_cost(body.get("models") or []))


@app.route("/providers/video-test/run", methods=["POST"])
def video_bakeoff_run():
    from hypogeum import video_bakeoff as _bo
    channel_id = (request.form.get("channel") or "").strip()
    prompt     = (request.form.get("prompt") or "").strip()
    aspect     = (request.form.get("aspect") or "16:9").strip()
    models     = request.form.getlist("models")
    try:
        run_id = _bo.start_bakeoff(channel_id, prompt, models,
                                   aspect_ratio=aspect, db_path=_db_path())
    except Exception as exc:  # noqa: BLE001
        return redirect(f"/providers/video-test?channel={channel_id}&flash={exc}")
    return redirect(f"/providers/video-test?channel={channel_id}&run={run_id}")


@app.route("/api/providers/video-test/run/<run_id>")
def api_video_bakeoff_status(run_id):
    from hypogeum import video_bakeoff as _bo
    m = _bo.get_bakeoff(run_id)
    if not m:
        return jsonify({"error": "not found"}), 404
    return jsonify(m)


@app.route("/providers/video-test/adopt", methods=["POST"])
def video_bakeoff_adopt():
    from hypogeum.channel_config import (
        list_channel_profiles, get_model_profile, update_model_profile,
        create_model_profile, update_channel_profile,
    )
    channel_id = (request.form.get("channel") or "").strip()
    model      = (request.form.get("model") or "").strip()
    ch = next((c for c in list_channel_profiles(db_path=_db_path())
               if c.channel_id == channel_id), None)
    if not ch or not model:
        return redirect(f"/providers/video-test?channel={channel_id}&flash=adopt+failed")
    mp_id = ch.model_profile_id
    if not mp_id:
        mp = create_model_profile(f"{channel_id} models", db_path=_db_path())
        mp_id = mp.model_profile_id
        try:
            update_channel_profile(channel_id, model_profile_id=mp_id, db_path=_db_path())
        except Exception:  # noqa: BLE001
            pass
    # Capture the model's live price now so the Production Plan can price it
    # even though it is not in muapi.MODEL_CREDIT_COSTS.
    from hypogeum import video_bakeoff as _bo
    price = next((m["price_usd"] for m in _bo.list_video_models() if m["id"] == model), None)
    # Capture the model's REAL delivered clip duration from its own bake-off
    # evidence (video_bakeoff._execute_run ffprobes every generated clip and
    # records duration_s via provider_evidence.record). Without this, the
    # shot-budget/coverage arithmetic assumes 8s for any model outside the
    # hardcoded veo3/veo3.1/runway families -- a shorter-clip model (e.g.
    # seedance's ~5s) then under-provisions every beat and Assembly's
    # coverage-fill fails downstream. None when no evidence exists yet (the
    # model was adopted without ever running in a bake-off comparison here).
    from hypogeum.provider_evidence import model_summary as _model_summary
    duration = _model_summary(model, db_path=_db_path()).get("avg_duration_s")
    update_model_profile(mp_id, db_path=_db_path(),
                         video_model=model, video_model_price_usd=price,
                         video_model_duration_seconds=duration)
    _audit_note(
        f"video model for {channel_id} set to {model} (${price}, "
        f"~{duration}s/clip) via bake-off"
    )
    return redirect(f"/providers/video-test?channel={channel_id}&flash=adopted+{model}")


@app.route("/api/providers/video-test/bakeoff-clip/<run_id>/<path:model>")
def api_video_bakeoff_clip(run_id: str, model: str):
    from flask import send_file
    from hypogeum import video_bakeoff as _bo
    p = _bo.clip_path(run_id, model)
    if not p:
        return jsonify({"error": "clip not found"}), 404
    return send_file(str(p), mimetype="video/mp4", conditional=True)


@app.route("/api/providers/video-test/clip/<episode_id>/<scene_id>")
def api_video_test_clip(episode_id: str, scene_id: str):
    """Serve a single generated clip for the in-browser player. Read-only."""
    from flask import send_file
    clip = _ASSETS_ROOT_PATH / "episodes" / episode_id / "video" / f"sc_{scene_id}.mp4"
    if not clip.exists():
        return jsonify({"error": "clip not found"}), 404
    return send_file(str(clip), mimetype="video/mp4", conditional=True)


@app.route("/api/providers/video-test")
def api_video_test():
    """Per-clip video analysis. No write actions, no provider calls."""
    _avail = _local_episodes_with_video()
    episode_id = request.args.get("episode") or (
        _avail[0]["episode_id"] if _avail else ""
    )
    refresh    = request.args.get("refresh", "0") == "1"
    if refresh:
        _VIDEO_TEST_CACHE.pop(episode_id, None)
    cached = _VIDEO_TEST_CACHE.get(episode_id)
    if not cached:
        cached = _compute_video_test(episode_id)
        if cached:
            _VIDEO_TEST_CACHE[episode_id] = cached
    if not cached:
        return jsonify({"error": "episode not found"}), 404
    return jsonify(cached)


@app.route("/api/providers/compare/results")
def api_providers_compare_results():
    """Return the most recent completed comparison run from DB."""
    try:
        result = get_comparison_result(db_path=_db_path())
        if result is None:
            return jsonify({"ok": True, "run": None})
        return jsonify({"ok": True, "run": result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/providers/compare/image/<run_id>/<provider>")
def api_providers_compare_image(run_id: str, provider: str):
    """Serve a generated comparison thumbnail image."""
    import re
    from flask import send_file, abort
    if not re.fullmatch(r"[0-9a-f\-]{36}", run_id):
        abort(400)
    if provider not in ("openai", "openrouter", "muapi"):
        abort(400)
    img_path = (
        _ROOT / ".hypogeum-assets" / "provider_comparisons"
        / run_id / f"thumb_{provider}.jpg"
    )
    if not img_path.exists():
        abort(404)
    return send_file(str(img_path), mimetype="image/jpeg")


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


def _compute_next_action(state, report, recent_episodes: list, failed_run_id: str = None) -> dict:
    """
    Derive the single most important next action the operator should take.
    Returns {label, url, urgency: "critical"|"warning"|"info", detail}.
    Non-blocking — returns an "info" action on any error.
    """
    try:
        # 1. Blocking OIS signals
        if state and state.active_signals:
            blocking = [s for s in state.active_signals if s.get("blocking")]
            if blocking:
                sig = blocking[0]
                return {
                    "label":   "Resolve blocking signal",
                    "url":     "/signals",
                    "urgency": "critical",
                    "detail":  translate_alert(sig.get("signal_type", "")) + (
                        f" — {sig.get('message', '')[:80]}" if sig.get("message") else ""
                    ),
                }

        # 2. Factory readiness blockers
        if report and report.blocking_reasons:
            return {
                "label":   "Fix factory setup",
                "url":     "/setup",
                "urgency": "critical",
                "detail":  report.blocking_reasons[0][:100],
            }

        # 3. Pending approvals
        if state and state.pending_approvals:
            return {
                "label":   f"Review {len(state.pending_approvals)} pending approval(s)",
                "url":     "/approvals",
                "urgency": "warning",
                "detail":  "Production is waiting for your approval before proceeding.",
            }

        # 4. Episodes stuck at partial (have artifacts but no render)
        # Skip when a known failed run exists — primary_cta already surfaces it
        # and a stale disk artifact must not supersede it.
        stuck = [
            ep for ep in recent_episodes
            if ep.get("_disk_only") and not ep.get("has_render")
        ]
        if stuck and not failed_run_id:
            ep = stuck[0]
            return {
                "label":   f"Resume production for {ep['episode_id']}",
                "url":     f"/episode/{ep['episode_id']}",
                "urgency": "warning",
                "detail":  "Episode has partial artifacts — assembly or narration may need to resume.",
            }

        # 5. Topic queue has items ready
        if state and getattr(state, "run_health", None):
            rh = state.run_health
            if hasattr(rh, "queued_count") and rh.queued_count > 0:
                return {
                    "label":   "Start production from queued topics",
                    "url":     "/episodes",
                    "urgency": "info",
                    "detail":  f"{rh.queued_count} topic(s) are queued and ready.",
                }

        # 6. Default: ready to create
        return {
            "label":   "Create a new episode",
            "url":     "/create",
            "urgency": "info",
            "detail":  "Factory is ready. Queue a topic or start a new episode.",
        }

    except Exception:
        return {
            "label":   "Open dashboard",
            "url":     "/dashboard",
            "urgency": "info",
            "detail":  "",
        }


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

    # Recent episodes: DB rows + any disk-only episodes (like ep_003 from scripts)
    try:
        db_episodes = list_episodes(db_path=_db_path(), limit=10)
    except Exception:
        db_episodes = []
    try:
        disk_eps = list_disk_episodes(assets_root=_ASSETS_ROOT_PATH, limit=20)
        # Supplement: include disk episodes not already in DB list
        db_ep_ids = {e["episode_id"] for e in db_episodes}
        for dep in disk_eps:
            if dep["episode_id"] not in db_ep_ids:
                db_episodes.append({
                    "episode_id": dep["episode_id"],
                    "title":      dep.get("title"),
                    "topic":      dep.get("title"),
                    "status":     "complete" if dep.get("has_render") else "partial",
                    "run_status": "complete" if dep.get("has_render") else "partial",
                    "total_cost_usd": None,
                    "created_at": None,
                    "_disk_only": True,
                    "qa_verdict": dep.get("qa_verdict"),
                    "has_render": dep.get("has_render"),
                })
        db_episodes = db_episodes[:10]
    except Exception:
        pass

    # Next required action — pass failed_run_id so stale disk episodes
    # don't surface when a real DB run failure already needs attention.
    next_action = _compute_next_action(
        state, report, db_episodes,
        failed_run_id=overview.get("failed_run_id") if overview else None,
    )

    try:
        attn = _build_attention_context()
        attention_summary = {
            "total":          attn["total"],
            "blocking":       attn["blocking_count"],
            "action_required": attn["action_required_count"],
            "warnings":       attn["warning_count"],
        }
    except Exception:
        attention_summary = {"total": 0, "blocking": 0, "action_required": 0, "warnings": 0}

    try:
        from hypogeum.dashboard_rollup import get_factory_rollup
        rollup = get_factory_rollup(db_path=_db_path())
    except Exception:  # noqa: BLE001
        rollup = None

    return render_template(
        "exec_dashboard.html",
        state=state,
        report=report,
        error=err,
        exec_alerts=exec_alerts,
        overview=overview,
        recent_episodes=db_episodes,
        next_action=next_action,
        attention_summary=attention_summary,
        rollup=rollup,
    )


@app.route("/attention")
def attention_page():
    ctx = _build_attention_context()
    return render_template("attention.html", **ctx)


@app.route("/api/attention/summary")
def api_attention_summary():
    ctx = _build_attention_context()
    return jsonify({
        "total":           ctx["total"],
        "blocking":        ctx["blocking_count"],
        "action_required": ctx["action_required_count"],
        "warnings":        ctx["warning_count"],
        "sections": {
            "blocking_signals": len(ctx["blocking_signals"]),
            "approvals":        len(ctx["approvals"]),
            "learning_pending": len(ctx["learning_pending"]),
            "platform_issues":  len(ctx["platform_issues"]),
            "readiness_blocks": len(ctx["readiness_blocks"]),
            "warning_signals":  len(ctx["warning_signals"]),
        },
    })


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
# Platform Connections — /platforms dedicated page
# ===========================================================================

def _read_env_file() -> dict:
    """Parse .env into a dict of {key: value}. Does not touch os.environ."""
    try:
        from hypogeum.db import _PROJECT_ROOT
        env_path = _PROJECT_ROOT / ".env"
        result = {}
        if not env_path.exists():
            return result
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().split(" #")[0].strip().strip("\"'")
            result[k] = v
        return result
    except Exception:
        return {}


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "●" * len(value)
    return value[:6] + "…" + value[-3:]


def _get_platform_detail() -> list:
    """
    Per-platform connection detail for the /platforms page, sourced from
    Zernio (GET /v1/accounts snapshot in provider_accounts). Zernio owns
    every social OAuth link for this factory — it publishes to the
    platforms and reports their health — so the retired per-platform
    tokens (youtube-oauth-refresh, meta-oauth-token, tiktok sandbox) are
    no longer shown here. Never raises.
    """
    db = _db_path()

    try:
        from hypogeum.publishing.zernio_accounts import get_zernio_accounts_snapshot
        snap = get_zernio_accounts_snapshot(db)
    except Exception:
        snap = {"by_platform": {}, "has_analytics_access": False, "synced_at": None}
    by_platform = snap.get("by_platform", {}) or {}

    try:
        conn_state = get_platform_connection_state(db_path=db).get("platforms", {})
    except Exception:
        conn_state = {}

    # Zernio API key health (the ONE credential this page still cares about)
    try:
        z_sp = [s for s in list_secret_profiles(db_path=db) if s.provider == "zernio"]
        zernio_api_active = any(s.status == "active" for s in z_sp)
        zernio_api_label = next((s.label for s in z_sp if s.status == "active"),
                                (z_sp[0].label if z_sp else None))
    except Exception:
        zernio_api_active, zernio_api_label = False, None

    _ICON = {"youtube": "YT", "facebook": "FB", "instagram": "IG", "tiktok": "TK"}
    _LABEL = {"youtube": "YouTube", "facebook": "Facebook",
              "instagram": "Instagram", "tiktok": "TikTok"}

    platforms = []
    for pid in ("youtube", "facebook", "instagram", "tiktok"):
        acc = by_platform.get(pid)
        cs = conn_state.get(pid, {})

        if acc is None:
            vstatus, status_label = "missing", "Not reported by Zernio"
        elif acc.get("status") == "active":
            vstatus, status_label = "verified", "Connected"
        elif acc.get("status") == "needs_reconnection":
            vstatus, status_label = "failed", "Needs reconnection on Zernio"
        else:
            vstatus, status_label = "saved", "Disconnected on Zernio"

        followers_age = None
        flu = (acc or {}).get("followers_last_updated")
        if flu:
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(str(flu).replace("Z", "+00:00"))
                followers_age = (datetime.now(timezone.utc) - dt).days
            except Exception:
                followers_age = None

        platforms.append({
            "id": pid,
            "label": _LABEL[pid],
            "icon": _ICON[pid],
            "connected_via": "zernio",
            "vstatus": vstatus,
            "status_label": status_label,
            "username": (acc or {}).get("username", ""),
            "display_label": (acc or {}).get("display_label", ""),
            "profile_url": (acc or {}).get("profile_url", ""),
            "followers_count": (acc or {}).get("followers_count"),
            "followers_age_days": followers_age,
            "verified_at": (str((acc or {}).get("last_verified_at") or "")[:16]
                            .replace("T", " ")),
            "analytics_records": cs.get("real_count", 0),
            "data_state": cs.get("data_state", "none"),
            "has_published": cs.get("has_published", False),
            # kept for the /dashboard "needs attention" scan — empty here so a
            # healthy Zernio link raises no warning
            "missing": [] if acc is not None else ["Zernio account link"],
            "credentials": [],
            "error": status_label if vstatus == "failed" else None,
            "zernio_api_active": zernio_api_active,
            "zernio_api_label": zernio_api_label,
            "note": (
                "Managed by Zernio. Re-authorize this account in the Zernio "
                "dashboard (zernio.com) if it ever needs reconnection."
                if acc is not None else
                "Zernio has not reported this platform yet. Link the account "
                "in the Zernio dashboard, then Sync."
            ),
        })

    return platforms


@app.route("/platforms")
def platform_connections_page():
    try:
        platforms = _get_platform_detail()
    except Exception as exc:
        platforms = []
    return render_template("platform_connections.html", platforms=platforms)


@app.route("/api/platforms/zernio/sync", methods=["POST"])
def api_platforms_zernio_sync():
    """
    Refresh the local Zernio account snapshot (GET /v1/accounts -> the
    provider_accounts rows the /platforms page and the Factory 'Connected
    Platforms' widget read). One live call to Zernio; never mutates Zernio.
    """
    try:
        from hypogeum.publishing.zernio_accounts import sync_zernio_accounts
        res = sync_zernio_accounts(_db_path())
        return jsonify({
            "ok": res["ok"],
            "synced": res["synced"],
            "has_analytics_access": res["has_analytics_access"],
            "error": res["error"],
        }), (200 if res["ok"] else 502)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "synced": 0, "error": str(exc)}), 500


@app.route("/api/platforms/detail")
def api_platform_connections_detail():
    """Per-credential platform detail — no credential values returned."""
    try:
        raw = _get_platform_detail()
        # Strip masked values before returning as JSON
        safe = []
        for p in raw:
            pc = dict(p)
            pc["credentials"] = [
                {k: v for k, v in c.items() if k != "masked"}
                for c in p["credentials"]
            ]
            safe.append(pc)
        return jsonify(safe)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


# ===========================================================================
# F9 — Channel Profile (Control Center)
# ===========================================================================

@app.route("/channel-profile")
def channel_profile_page():
    # Legacy single-channel profile — superseded by per-channel Settings.
    # Send the operator to the channel list (or straight into a channel if
    # there is exactly one).
    chans = _nav_channels()
    if len(chans) == 1:
        return redirect(url_for("channel_detail", channel_id=chans[0]["channel_id"]))
    return redirect(url_for("channels_page"))


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
# Storage & Archive — factory-wide archive root + the asset archive browser
# ===========================================================================

@app.route("/settings/storage")
def settings_storage_page():
    from hypogeum.archive_storage import describe_archive_root, published_episode_states
    try:
        info = describe_archive_root(db_path=_db_path())
    except Exception as exc:  # noqa: BLE001
        info = {"error": str(exc)}
    try:
        episodes = published_episode_states(db_path=_db_path())
    except Exception:  # noqa: BLE001
        episodes = []
    return render_template("settings_storage.html", archive=info, episodes=episodes,
                           saved=request.args.get("saved"),
                           err=request.args.get("err"))


@app.route("/api/settings/archive-root", methods=["POST"])
def api_settings_archive_root():
    from hypogeum.archive_storage import set_archive_root, describe_archive_root
    body = request.get_json(silent=True) or request.form
    path = (body.get("archive_root") or "").strip()
    try:
        set_archive_root(path, db_path=_db_path())
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 422
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "archive": describe_archive_root(db_path=_db_path())})


@app.route("/settings/storage/save", methods=["POST"])
def settings_storage_save():
    from hypogeum.archive_storage import set_archive_root
    try:
        set_archive_root((request.form.get("archive_root") or "").strip(), db_path=_db_path())
    except ValueError as exc:
        return redirect(url_for("settings_storage_page") + "?err=" + str(exc))
    except Exception as exc:  # noqa: BLE001
        return redirect(url_for("settings_storage_page") + "?err=" + str(exc))
    return redirect(url_for("settings_storage_page") + "?saved=1")


@app.route("/archive")
def archive_page():
    from hypogeum.archive_storage import browse_archive, describe_archive_root
    args = request.args
    try:
        page = max(1, int(args.get("page", 1)))
    except ValueError:
        page = 1
    per = 60
    data = browse_archive(
        db_path=_db_path(),
        q=args.get("q", "").strip(),
        asset_type=args.get("asset_type", "").strip(),
        episode_id=args.get("episode_id", "").strip(),
        provider=args.get("provider", "").strip(),
        storage_status=args.get("storage_status", "").strip(),
        reuse_only=args.get("reuse") == "1",
        limit=per, offset=(page - 1) * per,
    )
    try:
        root = describe_archive_root(db_path=_db_path())
    except Exception:  # noqa: BLE001
        root = {}
    return render_template(
        "archive.html", data=data, root=root, page=page, per=per,
        filters={k: args.get(k, "") for k in
                 ("q", "asset_type", "episode_id", "provider", "storage_status", "reuse")},
    )


@app.route("/api/archive")
def api_archive():
    from hypogeum.archive_storage import browse_archive
    a = request.args
    try:
        offset = max(0, int(a.get("offset", 0)))
        limit = max(1, min(int(a.get("limit", 60)), 200))
    except ValueError:
        offset, limit = 0, 60
    return jsonify(browse_archive(
        db_path=_db_path(),
        q=a.get("q", "").strip(), asset_type=a.get("asset_type", "").strip(),
        episode_id=a.get("episode_id", "").strip(), provider=a.get("provider", "").strip(),
        storage_status=a.get("storage_status", "").strip(),
        reuse_only=a.get("reuse") == "1", limit=limit, offset=offset,
    ))


@app.route("/archive/<path:asset_id>")
def archive_asset_page(asset_id):
    from hypogeum.archive_storage import get_archive_asset
    from hypogeum.archive_recall import get_asset_lineage, get_asset_versions
    asset = get_archive_asset(asset_id, db_path=_db_path())
    if not asset:
        return render_template("not_found.html", kind="Archive asset", id=asset_id), 404
    lineage = versions = None
    try:
        lineage = get_asset_lineage(asset_id, db_path=_db_path())
    except Exception:  # noqa: BLE001
        pass
    try:
        versions = get_asset_versions(asset_id, db_path=_db_path())
    except Exception:  # noqa: BLE001
        pass
    return render_template("archive_asset.html", asset=asset,
                           lineage=lineage, versions=versions)


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
        "cred_label":   "TikTok Access Token",
        "instructions": (
            "TikTok uses OAuth 2.0. Go to developers.tiktok.com → "
            "Manage Apps → your app → Keys & Tokens. "
            "Generate an Access Token (requires app review for Upload API scope). "
            "Paste the token below."
        ),
        "credential_type": "oauth2_token",
        "placeholder": "act.abc123...",
    },
    "instagram": {
        "label":        "Instagram",
        "cred_label":   "Meta Long-Lived Access Token",
        "instructions": (
            "Instagram uses the Meta Graph API (OAuth 2.0 derived token). "
            "In Meta for Developers → your app → Tools → Graph API Explorer: "
            "select your app, grant instagram_basic + instagram_content_publish scopes, "
            "generate a long-lived page/user token, then paste it below. "
            "Also set META_INSTAGRAM_ACCOUNT_ID to your Instagram Business account ID."
        ),
        "credential_type": "oauth2_token",
        "placeholder": "EAAGm...",
    },
    "facebook": {
        "label":        "Facebook",
        "cred_label":   "Meta Long-Lived Page Access Token",
        "instructions": (
            "Facebook uses the Meta Graph API (OAuth 2.0 derived token). "
            "In Meta for Developers → your app → Tools → Graph API Explorer: "
            "select your Page, grant pages_manage_posts + pages_read_engagement scopes, "
            "generate a long-lived page token, then paste it below. "
            "Also set META_FACEBOOK_PAGE_ID to your Facebook Page ID."
        ),
        "credential_type": "oauth2_token",
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
# YouTube OAuth 2.0 authorization code flow
# ===========================================================================

_YT_OAUTH_SCOPES = " ".join([
    "https://www.googleapis.com/auth/youtube",                # upload, thumbnails, end screens
    "https://www.googleapis.com/auth/youtube.force-ssl",      # captions.insert (required explicitly)
    "https://www.googleapis.com/auth/yt-analytics.readonly",  # Analytics API
])

_YT_AUTH_URL   = "https://accounts.google.com/o/oauth2/v2/auth"
_YT_TOKEN_URL  = "https://oauth2.googleapis.com/token"


def _yt_redirect_uri() -> str:
    """Build the OAuth callback URL for the current request host."""
    # host_url includes trailing slash: http://localhost:5001/
    base = request.host_url.rstrip("/")
    return f"{base}/platforms/youtube/oauth_callback"


@app.route("/platforms/youtube/authorize")
def youtube_oauth_authorize():
    """
    Start the YouTube OAuth 2.0 authorization code flow.

    Redirects the user to Google's consent screen. Uses prompt=consent
    and access_type=offline so Google issues a refresh_token even if the
    user previously granted access.

    Redirect URI: http://{host}/platforms/youtube/oauth_callback
    Scopes:       yt-analytics.readonly
    """
    import urllib.parse
    from hypogeum.config import Config as _Cfg
    try:
        cfg = _Cfg()
    except Exception as exc:
        return f"Config error: {exc}", 500

    if not (cfg.YOUTUBE_CLIENT_ID and cfg.YOUTUBE_CLIENT_SECRET):
        return ("YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET must be set "
                "before starting the OAuth flow."), 400

    params = {
        "client_id":     cfg.YOUTUBE_CLIENT_ID,
        "redirect_uri":  _yt_redirect_uri(),
        "response_type": "code",
        "scope":         _YT_OAUTH_SCOPES,
        "access_type":   "offline",
        "prompt":        "consent",   # force refresh_token to be issued
    }
    auth_url = _YT_AUTH_URL + "?" + urllib.parse.urlencode(params)
    return redirect(auth_url)


@app.route("/platforms/youtube/oauth_callback")
def youtube_oauth_callback():
    """
    Handle the Google OAuth 2.0 callback.

    Exchanges authorization code for access_token + refresh_token.
    Stores the refresh_token in a SecretProfile with
    credential_type='oauth2_refresh_token' so sync_youtube_analytics
    can use it without requiring a manual .env edit.

    Success → redirects to /providers?youtube_authorized=1
    Error   → renders youtube_oauth_result.html with trace info
    """
    # Business logic lives in the Service layer (hypogeum.oauth_service), not
    # here — this handler only parses the request and renders the outcome.
    from hypogeum.oauth_service import exchange_youtube_code
    redirect_uri = _yt_redirect_uri()

    error = request.args.get("error")
    if error:
        return render_template(
            "youtube_oauth_result.html", ok=False, error=error,
            error_description=request.args.get("error_description", ""),
            redirect_uri=redirect_uri, scope=_YT_OAUTH_SCOPES,
        ), 400

    result = exchange_youtube_code(
        request.args.get("code", ""), redirect_uri, db_path=_db_path())
    if result.ok:
        return redirect(url_for("providers_page") + "?youtube_authorized=1")
    return render_template(
        "youtube_oauth_result.html", ok=False, error=result.error,
        error_description=result.error_description,
        redirect_uri=redirect_uri, scope=_YT_OAUTH_SCOPES,
        **result.detail,
    ), result.http_status


# ---------------------------------------------------------------------------
# Meta (Facebook + Instagram) OAuth 2.0 flow
# ---------------------------------------------------------------------------
# Scopes:
#   pages_manage_posts       — publish video posts to a Facebook Page
#   pages_read_engagement    — required alongside pages_manage_posts
#   pages_show_list          — list Pages the user administers
#   instagram_content_publish — create Instagram Reels / feed posts
#   instagram_basic          — read basic Instagram account info
#
# Flow:
#   1. /platforms/meta/authorize          → redirect to Facebook dialog
#   2. Facebook → /platforms/meta/oauth_callback?code=...
#   3. callback: exchange code → short-lived user token
#   4. callback: exchange short-lived → long-lived token (60-day)
#   5. callback: store in secret_profiles for provider='facebook'
#                and provider='instagram' (same token, both platforms need it)
#   6. callback: verify token against Graph API /me
#   7. redirect to /providers?meta_authorized=1
# ---------------------------------------------------------------------------

_META_GRAPH_VERSION = "v19.0"
_META_AUTH_URL      = f"https://www.facebook.com/{_META_GRAPH_VERSION}/dialog/oauth"
_META_TOKEN_URL_CB  = f"https://graph.facebook.com/{_META_GRAPH_VERSION}/oauth/access_token"
_META_EXCHANGE_URL  = f"https://graph.facebook.com/{_META_GRAPH_VERSION}/oauth/access_token"
_META_SCOPES        = (
    "pages_manage_posts,"
    "pages_read_engagement,"
    "instagram_content_publish,"
    "instagram_basic"
)


def _meta_redirect_uri() -> str:
    base = request.host_url.rstrip("/")
    return f"{base}/platforms/meta/oauth_callback"


@app.route("/platforms/meta/authorize")
def meta_oauth_authorize():
    """
    Start the Meta (Facebook + Instagram) OAuth 2.0 authorization code flow.

    Redirects to the Facebook dialog. Uses a single flow to obtain
    permissions for both Facebook Page publishing and Instagram content
    publishing.

    Prerequisite: META_APP_ID and META_APP_SECRET must be set in .env.
    The redirect URI http(s)://{host}/platforms/meta/oauth_callback must be
    registered as a Valid OAuth Redirect URI in the Meta app's Facebook Login
    settings at developers.facebook.com.
    """
    import urllib.parse
    from hypogeum.config import Config as _Cfg
    try:
        cfg = _Cfg()
    except Exception as exc:
        return f"Config error: {exc}", 500

    if not (cfg.META_APP_ID and cfg.META_APP_SECRET):
        return ("META_APP_ID and META_APP_SECRET must be set "
                "before starting the Meta OAuth flow."), 400

    params = {
        "client_id":     cfg.META_APP_ID,
        "redirect_uri":  _meta_redirect_uri(),
        "response_type": "code",
        "scope":         _META_SCOPES,
    }
    auth_url = _META_AUTH_URL + "?" + urllib.parse.urlencode(params)
    return redirect(auth_url)


@app.route("/platforms/meta/oauth_callback")
def meta_oauth_callback():
    """
    Handle the Meta OAuth 2.0 callback.

    Steps:
      1. Exchange authorization code for a short-lived user access token.
      2. Exchange short-lived token for a long-lived token (60-day expiry).
      3. Upsert a SecretProfile for provider='facebook' and provider='instagram'
         (both use the same long-lived token).
      4. Verify token via GET /me and confirm Page / IG account access.

    Success → /providers?meta_authorized=1
    Error   → meta_oauth_result.html with diagnostics
    """
    import requests as _req
    from hypogeum.config import Config as _Cfg

    error = request.args.get("error")
    if error:
        return render_template(
            "meta_oauth_result.html",
            ok=False,
            error=error,
            error_description=request.args.get("error_description", ""),
            redirect_uri=_meta_redirect_uri(),
            scope=_META_SCOPES,
        ), 400

    code = request.args.get("code")
    if not code:
        return render_template(
            "meta_oauth_result.html",
            ok=False, error="no_code",
            error_description="No authorization code in callback parameters.",
            redirect_uri=_meta_redirect_uri(),
            scope=_META_SCOPES,
        ), 400

    try:
        cfg = _Cfg()
    except Exception as exc:
        return render_template(
            "meta_oauth_result.html",
            ok=False, error="config_error",
            error_description=str(exc),
            redirect_uri=_meta_redirect_uri(),
            scope=_META_SCOPES,
        ), 500

    redirect_uri = _meta_redirect_uri()

    # Step 1: Exchange authorization code → short-lived user access token
    try:
        tok_resp = _req.get(_META_TOKEN_URL_CB, params={
            "client_id":     cfg.META_APP_ID,
            "client_secret": cfg.META_APP_SECRET,
            "redirect_uri":  redirect_uri,
            "code":          code,
        }, timeout=15)
        token_data = tok_resp.json()
    except Exception as exc:
        return render_template(
            "meta_oauth_result.html",
            ok=False, error="network_error",
            error_description=str(exc),
            redirect_uri=redirect_uri,
            scope=_META_SCOPES,
        ), 502

    if not tok_resp.ok or "access_token" not in token_data:
        return render_template(
            "meta_oauth_result.html",
            ok=False,
            error=token_data.get("error", {}).get("type", "token_exchange_failed"),
            error_description=str(token_data.get("error", token_data)),
            redirect_uri=redirect_uri,
            scope=_META_SCOPES,
        ), 400

    short_token = token_data["access_token"]

    # Step 2: Exchange short-lived token → long-lived token (60-day)
    try:
        ll_resp = _req.get(_META_EXCHANGE_URL, params={
            "grant_type":      "fb_exchange_token",
            "client_id":       cfg.META_APP_ID,
            "client_secret":   cfg.META_APP_SECRET,
            "fb_exchange_token": short_token,
        }, timeout=15)
        ll_data = ll_resp.json()
    except Exception as exc:
        return render_template(
            "meta_oauth_result.html",
            ok=False, error="longlived_exchange_error",
            error_description=str(exc),
            redirect_uri=redirect_uri,
            scope=_META_SCOPES,
        ), 502

    if not ll_resp.ok or "access_token" not in ll_data:
        return render_template(
            "meta_oauth_result.html",
            ok=False,
            error=ll_data.get("error", {}).get("type", "longlived_exchange_failed"),
            error_description=str(ll_data.get("error", ll_data)),
            redirect_uri=redirect_uri,
            scope=_META_SCOPES,
        ), 400

    long_user_token = ll_data["access_token"]
    token_expires_in = ll_data.get("expires_in", "unknown")

    # Step 2.5: Attempt to get a Page access token for Facebook publishing.
    # A Page token has implicit pages_manage_posts rights for that page without
    # requiring the user token to carry that scope. Falls back to user token.
    long_fb_token = long_user_token
    page_token_source = "user_token"
    page_id_for_token = getattr(_Cfg(), "META_FACEBOOK_PAGE_ID", "") or ""
    if page_id_for_token:
        try:
            pt_resp = _req.get(
                f"https://graph.facebook.com/{_META_GRAPH_VERSION}/{page_id_for_token}",
                params={"fields": "access_token", "access_token": long_user_token},
                timeout=15,
            )
            if pt_resp.ok:
                pt_data = pt_resp.json()
                short_page_token = pt_data.get("access_token", "")
                if short_page_token:
                    # Exchange page token for long-lived page token
                    lp_resp = _req.get(_META_EXCHANGE_URL, params={
                        "grant_type":        "fb_exchange_token",
                        "client_id":         cfg.META_APP_ID,
                        "client_secret":     cfg.META_APP_SECRET,
                        "fb_exchange_token": short_page_token,
                    }, timeout=15)
                    if lp_resp.ok:
                        lp_data = lp_resp.json()
                        long_fb_token = lp_data.get("access_token", long_user_token)
                        page_token_source = "page_token"
        except Exception:
            pass  # fall back to user token for Facebook

    long_token = long_user_token  # Instagram uses user token
    # Step 3: Persist in secret_profiles.
    # Facebook gets the long-lived Page token (or user token fallback).
    # Instagram gets the long-lived user token.
    db = _db_path()
    _META_LABEL = "meta-oauth-token"
    _tokens_by_provider = {"facebook": long_fb_token, "instagram": long_token}
    try:
        for _provider, _cred in _tokens_by_provider.items():
            existing = list_secret_profiles(provider=_provider, db_path=db)
            active   = [p for p in existing if p.label == _META_LABEL and p.status == "active"]
            if active:
                update_secret_profile(
                    active[0].secret_profile_id,
                    credential_data=_cred,
                    label=_META_LABEL,
                    db_path=db,
                )
            else:
                create_secret_profile(
                    provider=_provider,
                    label=_META_LABEL,
                    credential_type="oauth2_token",
                    credential_data=_cred,
                    db_path=db,
                )
    except Exception as exc:
        return render_template(
            "meta_oauth_result.html",
            ok=False, error="db_error",
            error_description=str(exc),
            redirect_uri=redirect_uri,
            scope=_META_SCOPES,
        ), 500

    # Step 4: Verify token — GET /me
    me_info = {}
    page_info = {}
    ig_info = {}
    try:
        me_resp = _req.get(
            f"https://graph.facebook.com/{_META_GRAPH_VERSION}/me",
            params={"fields": "id,name", "access_token": long_token},
            timeout=10,
        )
        me_info = me_resp.json() if me_resp.ok else {"error": me_resp.text[:200]}

        # Verify Facebook Page ID access
        page_id = getattr(cfg, "META_FACEBOOK_PAGE_ID", None) or ""
        if page_id:
            pg_resp = _req.get(
                f"https://graph.facebook.com/{_META_GRAPH_VERSION}/{page_id}",
                params={"fields": "id,name", "access_token": long_token},
                timeout=10,
            )
            page_info = pg_resp.json() if pg_resp.ok else {"error": pg_resp.text[:200]}

        # Verify Instagram Business Account ID access
        ig_id = getattr(cfg, "META_INSTAGRAM_ACCOUNT_ID", None) or ""
        if ig_id:
            ig_resp = _req.get(
                f"https://graph.facebook.com/{_META_GRAPH_VERSION}/{ig_id}",
                params={"fields": "id,username", "access_token": long_token},
                timeout=10,
            )
            ig_info = ig_resp.json() if ig_resp.ok else {"error": ig_resp.text[:200]}
    except Exception:
        pass  # verification failures do not block storage

    return redirect(
        url_for("providers_page")
        + f"?meta_authorized=1"
        + f"&me={me_info.get('name', '')}"
        + f"&page={page_info.get('name', '')}"
        + f"&ig={ig_info.get('username', '')}"
        + f"&expires_in={token_expires_in}"
    )


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


@app.route("/api/schedule/<schedule_id>/resume", methods=["POST"])
def api_schedule_resume(schedule_id):
    """
    Resume a paused schedule and advance next_run_at to the next future slot.

    Resuming without advancing next_run_at would cause tick() to fire
    immediately if next_run_at is stale (e.g. days in the past). This
    endpoint sets status=active AND computes the next valid future time
    based on cadence and the preferred run hour (06:00 UTC by default).

    Returns {ok, schedule_id, status, next_run_at}.
    """
    from datetime import datetime, timezone, timedelta
    from hypogeum.scheduler import set_schedule_status, set_next_run, compute_next_run
    from hypogeum.db import get_connection

    db = _db_path()
    try:
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT * FROM schedules WHERE schedule_id=?", (schedule_id,)
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    if row is None:
        return jsonify({"ok": False, "error": "schedule not found"}), 404

    row = dict(row)
    cadence = row.get("cadence") or "daily"

    # Compute next future slot: today or tomorrow at 06:00 UTC
    now = datetime.now(timezone.utc)
    candidate = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)

    # For weekly cadence, advance to same weekday next week if candidate is today
    if cadence == "weekly":
        candidate += timedelta(weeks=1)

    # 1. Set status active
    r1 = set_schedule_status(schedule_id, "active", db_path=db)
    if not r1.get("ok"):
        return jsonify(r1), 400

    # 2. Advance next_run_at to future slot
    try:
        set_next_run(schedule_id, candidate, db_path=db)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"resumed but next_run reset failed: {exc}"}), 500

    return jsonify({
        "ok":          True,
        "schedule_id": schedule_id,
        "status":      "active",
        "next_run_at": candidate.isoformat(),
    })


@app.route("/api/platforms/tiktok/refresh", methods=["POST"])
def api_tiktok_refresh():
    """
    Refresh the TikTok access token using the stored refresh_token.

    Uses hypogeum.publishing.tiktok.refresh_tiktok_token() which exchanges
    the stored refresh_token for a new 24-hour access token and writes
    it back to secret_profiles.

    Returns {ok, expires_at, publish_mode, video_publish_scope_missing, note}.
    """
    try:
        from hypogeum.publishing.tiktok import refresh_tiktok_token
        new_token = refresh_tiktok_token(db_path=_db_path())
    except Exception as exc:
        return jsonify({
            "ok":    False,
            "error": str(exc),
            "note":  "Refresh failed. The refresh_token may have expired or the TikTok API is unreachable.",
        }), 400

    # Read back the updated credential to confirm expiry
    import json as _json
    from hypogeum.meta_analytics import get_meta_token, get_meta_token_expiry
    expiry = get_meta_token_expiry("tiktok", db_path=_db_path())

    # Read publish mode from credential_data
    publish_mode = "upload_to_inbox"
    video_publish_scope_missing = True
    try:
        from hypogeum.db import get_connection
        conn = get_connection(_db_path())
        try:
            row = conn.execute(
                "SELECT credential_data FROM secret_profiles WHERE provider='tiktok' AND status='active' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row:
            cred = _json.loads(dict(row)["credential_data"])
            publish_mode = cred.get("publish_mode", "upload_to_inbox")
            video_publish_scope_missing = bool(cred.get("video_publish_scope_missing", True))
    except Exception:
        pass

    return jsonify({
        "ok":                        True,
        "expires_at":                expiry,
        "publish_mode":              publish_mode,
        "video_publish_scope_missing": video_publish_scope_missing,
        "note": (
            "Token refreshed. Publish mode: upload_to_inbox only. "
            "video.publish scope is missing — videos upload to creator inbox "
            "and require manual publish from the TikTok app."
        ),
    })


@app.route("/api/schedules", methods=["GET"])
def api_schedules_list():
    """
    List all schedules.

    Returns [{schedule_id, scope, channel_id, cadence, status,
               next_run_at, last_run_at, label, max_runs_per_day,
               created_at, updated_at}, ...]
    """
    from hypogeum.db import get_connection, initialize_schema
    db = _db_path()
    initialize_schema(db)
    try:
        conn = get_connection(db)
        try:
            rows = conn.execute(
                "SELECT * FROM schedules ORDER BY created_at DESC"
            ).fetchall()
        finally:
            conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/schedules", methods=["POST"])
def api_schedules_create():
    """
    Create a new schedule.

    Body (JSON):
        scope           "factory" | "channel"   (default: "factory")
        channel_id      str | null               (required when scope="channel")
        cadence         "daily" | "weekly" | "manual"  (default: "daily")
        next_run_at     ISO datetime string | null      (null = no auto-fire)
        max_runs_per_day int                     (default: 1)
        label           str                      (optional)

    Returns:
        {ok: true, schedule_id: str}
    """
    from hypogeum.scheduler import create_schedule as _create_schedule
    from datetime import datetime, timezone

    body = request.get_json(silent=True) or {}
    scope            = body.get("scope", "factory")
    channel_id       = body.get("channel_id") or None
    cadence          = body.get("cadence", "daily")
    next_run_at_raw  = body.get("next_run_at")
    max_runs_per_day = int(body.get("max_runs_per_day", 1))
    label            = body.get("label") or None

    if scope not in ("factory", "channel"):
        return jsonify({"ok": False, "error": "scope must be 'factory' or 'channel'",
                        "code": "invalid"}), 400
    if cadence not in ("daily", "weekly", "manual"):
        return jsonify({"ok": False, "error": "cadence must be 'daily', 'weekly', or 'manual'",
                        "code": "invalid"}), 400
    if scope == "channel" and not channel_id:
        return jsonify({"ok": False, "error": "channel_id is required when scope='channel'",
                        "code": "invalid"}), 400

    next_run_at = None
    if next_run_at_raw:
        try:
            next_run_at = datetime.fromisoformat(next_run_at_raw.replace("Z", "+00:00"))
            if next_run_at.tzinfo is None:
                next_run_at = next_run_at.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            return jsonify({"ok": False,
                            "error": f"next_run_at must be an ISO datetime string, got: {next_run_at_raw!r}",
                            "code": "invalid"}), 400

    try:
        schedule_id = _create_schedule(
            db_path=_db_path(),
            scope=scope,
            channel_id=channel_id,
            cadence=cadence,
            next_run_at=next_run_at,
            max_runs_per_day=max_runs_per_day,
            label=label,
        )
        return jsonify({"ok": True, "schedule_id": schedule_id}), 201
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "code": "invalid"}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "code": "server_error"}), 500


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


@app.route("/api/action/resolve-notices", methods=["POST"])
def api_action_resolve_notices():
    """Clear every informational / already-published-episode signal at once."""
    try:
        from hypogeum.signal_digest import auto_resolve_stale_signals
        n = auto_resolve_stale_signals(db_path=_db_path())
        return jsonify({"ok": True, "resolved": n})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


# ===========================================================================
# Phase C — Runs & Episode Operations (JSON API)
# ===========================================================================

@app.route("/api/runs", methods=["GET"])
def api_runs():
    _reconcile_stale_runs()
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
    source     = body.get("source", "manual")
    result = enqueue_topic(
        channel_id, topic_text,
        priority=priority,
        db_path=_db_path(),
        source=source,
    )
    http_status = 200 if result["ok"] else (
        404 if result.get("code") == "not_found" else
        409 if result.get("code") == "duplicate" else
        400
    )
    return jsonify(result), http_status


@app.route("/api/topics/<topic_id>/chain", methods=["GET"])
def api_topic_chain(topic_id: str):
    """
    Return the full entity chain for a topic:
      Topic → Episodes → Release → Publish Jobs

    Used by /topics/<topic_id>/chain dashboard page.
    """
    from hypogeum.db import get_connection as _gc
    db = _db_path()
    try:
        conn = _gc(db)
        try:
            topic = conn.execute(
                """SELECT topic_id, channel_id, topic_text, status, priority, source,
                          target_publish_at, production_deadline_at, created_at, updated_at
                   FROM episode_topics WHERE topic_id=?""", (topic_id,)
            ).fetchone()
            if topic is None:
                return jsonify({"error": "topic not found"}), 404

            episodes = conn.execute(
                """SELECT episode_id, title, topic, status, run_id, total_cost_usd,
                          youtube_video_id, published_at, created_at
                   FROM episodes WHERE topic_id=? ORDER BY created_at""",
                (topic_id,)
            ).fetchall()

            releases = conn.execute(
                """SELECT release_id, episode_id, topic_id, target_publish_at,
                          status, notes, created_at, updated_at
                   FROM releases WHERE topic_id=? ORDER BY target_publish_at""",
                (topic_id,)
            ).fetchall()

            release_ids = [r["release_id"] for r in releases]
            publish_jobs: list = []
            if release_ids:
                placeholders = ",".join("?" * len(release_ids))
                publish_jobs = conn.execute(
                    f"""SELECT calendar_id, release_id, episode_id, short_id, platform,
                               target_publish_at, upload_status, publish_status,
                               platform_video_id, platform_supports_scheduled_publish,
                               publish_method, created_at, updated_at
                        FROM publish_calendar
                        WHERE release_id IN ({placeholders})
                        ORDER BY platform, short_id""",
                    release_ids
                ).fetchall()
            # Also check publish_calendar by episode_id (pre-release rows)
            ep_ids = [e["episode_id"] for e in episodes]
            if ep_ids:
                placeholders = ",".join("?" * len(ep_ids))
                extra_jobs = conn.execute(
                    f"""SELECT calendar_id, release_id, episode_id, short_id, platform,
                               target_publish_at, upload_status, publish_status,
                               platform_video_id, platform_supports_scheduled_publish,
                               publish_method, created_at, updated_at
                        FROM publish_calendar
                        WHERE episode_id IN ({placeholders})
                          AND (release_id IS NULL OR release_id NOT IN ({placeholders}))
                        ORDER BY platform, short_id""",
                    ep_ids + ep_ids
                ).fetchall()
                publish_jobs = list(publish_jobs) + list(extra_jobs)

        finally:
            conn.close()

        def _d(row):
            return dict(row) if row else None

        return jsonify({
            "topic":        _d(topic),
            "episodes":     [_d(e) for e in episodes],
            "releases":     [_d(r) for r in releases],
            "publish_jobs": [_d(j) for j in publish_jobs],
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/topics/<topic_id>/chain")
def topic_chain_page(topic_id: str):
    """Dashboard page: Topic → Episode → Release → Publish Jobs chain view."""
    from hypogeum.db import get_connection as _gc
    db = _db_path()
    try:
        conn = _gc(db)
        try:
            topic = conn.execute(
                """SELECT topic_id, channel_id, topic_text, status, priority, source,
                          target_publish_at, production_deadline_at, created_at
                   FROM episode_topics WHERE topic_id=?""", (topic_id,)
            ).fetchone()
        finally:
            conn.close()
        if topic is None:
            return render_template("not_found.html", kind="Topic", id=topic_id), 404
    except Exception:
        return render_template("not_found.html", kind="Topic", id=topic_id), 404

    chain = {}
    try:
        import requests as _req
        r = _req.get(f"http://127.0.0.1:{request.environ.get('SERVER_PORT', 5001)}"
                     f"/api/topics/{topic_id}/chain", timeout=5)
        chain = r.json()
    except Exception:
        chain = {"topic": dict(topic), "episodes": [], "releases": [], "publish_jobs": []}

    return render_template("topic_chain.html", chain=chain, topic_id=topic_id)


@app.route("/api/topics/<topic_id>/remove", methods=["POST"])
def api_topic_remove(topic_id: str):
    """Remove a queued topic (set status=cancelled)."""
    from hypogeum.db import get_connection as _gc
    db = _db_path()
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn = _gc(db)
        try:
            conn.execute(
                "UPDATE episode_topics SET status='cancelled', updated_at=? WHERE topic_id=? AND status='queued'",
                (now, topic_id),
            )
            conn.commit()
        finally:
            conn.close()
        channel_id = request.form.get("channel_id") or request.args.get("channel_id", "")
        return redirect(url_for("discover_page") + (f"?channel_id={channel_id}" if channel_id else ""))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


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
    _reconcile_stale_runs()
    status = request.args.get("status")
    ch     = request.args.get("channel_id") or _scope_channel()
    runs   = list_runs(db_path=_db_path(), status=status or None, channel_id=ch or None)
    return render_template("runs.html",
                           runs=runs, status_filter=status, fmt_duration=fmt_duration,
                           channel_filter=ch)


def _classify_run_recovery(detail: dict) -> dict:
    """
    Classify a failed run and return operator-facing recovery context.

    Returns a dict with:
      failed_stage    — name of the stage that failed (or None)
      failure_class   — "resumable" | "retryable" | "credential" | "config" | "fatal"
      why             — human sentence: what went wrong
      reuse_summary   — list of strings: what checkpoint files exist and will be reused
      safe_action     — string: what operator action is safe
      blocked_action  — string: what is NOT safe / not available
      can_rerun       — bool: whether the rerun button is available
      vpd_checkpoints — dict: {skeleton: bool, batches: [int]}  (only if VPD failed)
    """
    import re as _re

    run      = detail.get("run", {})
    episode  = detail.get("episode", {})
    stages   = detail.get("stages", [])
    episode_id = run.get("episode_id", "")

    failed = next((s for s in stages if s.get("status") == "failed"), None)
    if not failed and run.get("status") != "failed":
        return {}  # run not failed — no recovery context needed

    failed_stage = (failed or {}).get("stage_name") or run.get("error_message", "")[:40]
    error_msg    = (failed or {}).get("error_message") or run.get("error_message") or ""

    # ── VPD checkpoint detection ──────────────────────────────────────────
    vpd_checkpoints: dict = {}
    if "visual_package_director" in (failed_stage or ""):
        ep_dir = _ASSETS_ROOT_PATH / "episodes" / episode_id
        skeleton_path = ep_dir / f"visual_package_skeleton_{episode_id}.json"
        skeleton_ok   = skeleton_path.exists()

        # Stage A1 meta checkpoint
        a1_ok = (ep_dir / f"vpd_a1_meta_{episode_id}.json").exists() if ep_dir.exists() else False

        # Stage A2 scene batch checkpoints
        a2_files   = sorted(ep_dir.glob(f"vpd_a2_scenes_batch_*_{episode_id}.json")) if ep_dir.exists() else []
        a2_indices = []
        for af in a2_files:
            m = _re.search(r"vpd_a2_scenes_batch_(\d+)_", af.name)
            if m:
                a2_indices.append(int(m.group(1)))

        # Stage B cinematic prompt batch checkpoints
        batch_files   = sorted(ep_dir.glob(f"vpd_prompts_batch_*_{episode_id}.json")) if ep_dir.exists() else []
        batch_indices = []
        b_batch_scene_counts: dict = {}  # batch_idx → scene count in that checkpoint
        for bf in batch_files:
            m = _re.search(r"vpd_prompts_batch_(\d+)_", bf.name)
            if m:
                bidx = int(m.group(1))
                batch_indices.append(bidx)
                try:
                    bd = _json.loads(bf.read_text(encoding="utf-8"))
                    b_batch_scene_counts[bidx] = len(bd.get("scene_prompts", []))
                except Exception:
                    b_batch_scene_counts[bidx] = 0
        # Count completed scenes from Stage A2 batch files
        a2_scene_count = 0
        for af in a2_files:
            try:
                batch_data = _json.loads(af.read_text(encoding="utf-8"))
                a2_scene_count += len(batch_data.get("scenes", []))
            except Exception:
                pass
        # Count completed scenes from Stage B checkpoints (for display)
        b_scenes_done = sum(b_batch_scene_counts.values())

        vpd_checkpoints = {
            "skeleton":            skeleton_ok,
            "a1_meta":             a1_ok,
            "a2_batches":          sorted(a2_indices),
            "a2_batch_count":      len(a2_indices),
            "a2_scene_count":      a2_scene_count,
            "batches":             sorted(batch_indices),
            "b_batch_count":       len(batch_indices),
            "b_batch_scene_counts": b_batch_scene_counts,
            "b_scenes_done":       b_scenes_done,
        }

    # ── Classify failure ──────────────────────────────────────────────────
    is_truncation = (
        "truncat" in error_msg.lower()
        or "max_tokens" in error_msg.lower()
        or "completion_tokens" in error_msg.lower()
    )
    is_schema_error   = "SchemaError" in error_msg or "schema" in error_msg.lower()
    is_credential     = any(k in error_msg for k in ("401", "403", "auth", "credential", "token"))
    is_quota          = any(k in error_msg for k in ("402", "quota", "Quota", "budget", "Budget"))
    is_muapi          = "MUAPI" in error_msg
    is_vpd            = "visual_package_director" in (failed_stage or "")
    is_finance_gate   = "finance gate BLOCKED" in error_msg or "FinanceBudgetBlocked" in error_msg or "FinancePostVPDBlocked" in error_msg

    # ── Finance gate: parse spend/ceiling from the on-disk finance record ─
    finance_gate_detail: dict = {}
    if is_finance_gate:
        _ep = episode_id
        _ep_dir = _ASSETS_ROOT_PATH / "episodes" / _ep
        for _fname in (
            f"finance_post_vpd_authorization_{_ep}.json",
            f"finance_authorization_{_ep}.json",
        ):
            _fp = _ep_dir / _fname
            if _fp.exists():
                try:
                    _fd = _json.loads(_fp.read_text(encoding="utf-8"))
                    finance_gate_detail = {
                        "estimated_spend_usd": _fd.get("estimated_spend_usd", 0.0),
                        "budget_ceiling_usd":  _fd.get("budget_ceiling_usd", 0.0),
                        "overage_usd": round(
                            float(_fd.get("estimated_spend_usd", 0.0))
                            - float(_fd.get("budget_ceiling_usd", 0.0)), 4
                        ),
                        "cost_breakdown": _fd.get("cost_breakdown", {}),
                        "phase": _fd.get("phase", ""),
                    }
                except Exception:
                    pass
                break

    if is_vpd and is_truncation:
        failure_class = "resumable"
        _has_skeleton = vpd_checkpoints.get("skeleton", False)
        _b_done = vpd_checkpoints.get("b_batch_count", 0)
        if _has_skeleton and _b_done > 0:
            _last_b = vpd_checkpoints.get("batches", [])[-1] if vpd_checkpoints.get("batches") else "?"
            why = (
                f"Stage B (cinematic prompts) overflowed at batch {_last_b}: "
                f"model hit max_tokens before completing the prompt batch. "
                f"Re-run resumes from A1/A2 checkpoints; Stage B retries the failed batch with a smaller size. "
                "Earlier completed stages are reused via their own output files on disk."
            )
        elif _has_skeleton:
            why = (
                "Stage B (cinematic prompts) token overflow — no batches completed. "
                "Re-run resumes from A1/A2 checkpoints; Stage B starts with a smaller batch size. "
                "Earlier completed stages are reused via their own output files on disk."
            )
        else:
            why = (
                "Visual Package Director output exceeded the model token limit during Stage A or B. "
                "Stage A uses dynamic batch sizing (A1 + A2 batches + A3 merge). "
                "Re-run resumes from A1/A2 checkpoints at the last completed stage. "
                "Earlier completed stages are reused via their own output files on disk."
            )
        reuse = []
        if vpd_checkpoints.get("skeleton"):
            reuse.append(
                "Stage A skeleton (full scene plan + entities + thumbnail brief) — "
                "already on disk, will not be re-billed"
            )
        else:
            if vpd_checkpoints.get("a1_meta"):
                reuse.append(
                    "Stage A1 meta (entities, palettes, thumbnail brief) — "
                    "already on disk, will not be re-billed"
                )
            else:
                reuse.append("Stage A1 meta — not yet completed, will run on rerun")
            n_a2 = vpd_checkpoints.get("a2_batch_count", 0)
            n_sc = vpd_checkpoints.get("a2_scene_count", 0)
            if n_a2 > 0:
                reuse.append(
                    f"Stage A2 scene batches: {n_a2} batch(es) completed, "
                    f"~{n_sc} scene(s) planned — already on disk, will not be re-billed"
                )
            else:
                reuse.append("Stage A2 scene batches — not yet started, will run on rerun")
        n_b = vpd_checkpoints.get("b_batch_count", 0)
        b_scenes_done = vpd_checkpoints.get("b_scenes_done", 0)
        if n_b > 0:
            scene_counts = vpd_checkpoints.get("b_batch_scene_counts", {})
            for b in vpd_checkpoints.get("batches", []):
                n_sc_b = scene_counts.get(b, 0)
                sc_label = f" ({n_sc_b} scene{'s' if n_sc_b != 1 else ''})" if n_sc_b else ""
                reuse.append(
                    f"Stage B prompt batch {b}{sc_label} — "
                    "cinematic prompts on disk, will not be re-billed"
                )
        if b_scenes_done > 0 and n_b > 0:
            reuse.append(
                f"Total {b_scenes_done} scene prompt(s) completed across {n_b} Stage B batch(es)"
            )
        # reuse stays empty if no checkpoints found — template shows nothing to reuse
        safe_action    = (
            "Click Re-run — same episode_id is preserved so all checkpoints on disk are reused. "
            "Earlier stages (EP, Research, Script) skip via their own output files; "
            "VPD resumes from Stage A skeleton and completed Stage B batches."
        )
        blocked_action = "Do not manually delete checkpoint files before re-running"
    elif is_finance_gate:
        failure_class = "finance_gate"
        est   = finance_gate_detail.get("estimated_spend_usd", 0.0)
        ceil_ = finance_gate_detail.get("budget_ceiling_usd", 0.0)
        over  = finance_gate_detail.get("overage_usd", 0.0)
        why = (
            f"Budget ceiling exceeded. Estimated spend: ${est:.4f} USD — "
            f"ceiling: ${ceil_:.2f} USD — overage: ${over:.4f} USD. "
            "VPD planning is complete and all Stage B cinematic prompts are on disk. "
            "Only video generation (MUAPI) spend would be incurred on next run."
        )
        # Reuse list: VPD work already done
        reuse = []
        if vpd_checkpoints.get("skeleton"):
            reuse.append(
                "Stage A skeleton (full scene plan + entities + thumbnail brief) — "
                "already on disk, will not be re-billed"
            )
        _b_done_fg = vpd_checkpoints.get("b_batch_count", 0)
        if _b_done_fg > 0:
            _sc_fg = vpd_checkpoints.get("b_scenes_done", 0)
            reuse.append(
                f"{_b_done_fg} Stage B prompt batch(es) ({_sc_fg} scene prompts) — "
                "cinematic prompts on disk, will not be re-billed"
            )
            reuse.append(
                "Only MUAPI video generation will incur new spend on next run"
            )
        safe_action = (
            "Approve this run once via the Finance Gate panel below — "
            "VPD checkpoints are preserved and no planning stages will re-run."
        )
        blocked_action = "Do not raise the ceiling permanently if this is a one-off exception"
        can_rerun = bool(episode.get("topic") or episode.get("title"))
    elif is_vpd and not is_truncation:
        # Retryable VPD failure (schema error, MUAPI, etc. — not finance gate).
        # Checkpoints may still exist on disk even though the failure was not a
        # truncation — inspect them and surface them so the operator knows what
        # VPD work will be preserved on rerun.
        _has_skeleton_r = vpd_checkpoints.get("skeleton", False)
        _b_done_r       = vpd_checkpoints.get("b_batch_count", 0)
        _has_any_ckpt   = _has_skeleton_r or vpd_checkpoints.get("a1_meta", False) or _b_done_r > 0

        failure_class  = "retryable"
        why            = f"Visual Package Director raised an error: {error_msg[:120]}"

        if _has_any_ckpt:
            reuse = []
            if _has_skeleton_r:
                reuse.append(
                    "Stage A skeleton (full scene plan + entities + thumbnail brief) — "
                    "already on disk, will not be re-billed"
                )
            else:
                if vpd_checkpoints.get("a1_meta"):
                    reuse.append(
                        "Stage A1 meta (entities, palettes, thumbnail brief) — "
                        "already on disk, will not be re-billed"
                    )
                n_a2 = vpd_checkpoints.get("a2_batch_count", 0)
                n_sc = vpd_checkpoints.get("a2_scene_count", 0)
                if n_a2 > 0:
                    reuse.append(
                        f"Stage A2 scene batches: {n_a2} batch(es), ~{n_sc} scene(s) — "
                        "already on disk, will not be re-billed"
                    )
            if _b_done_r > 0:
                scene_counts_r = vpd_checkpoints.get("b_batch_scene_counts", {})
                for b in vpd_checkpoints.get("batches", []):
                    n_sc_b = scene_counts_r.get(b, 0)
                    sc_label = f" ({n_sc_b} scene{'s' if n_sc_b != 1 else ''})" if n_sc_b else ""
                    reuse.append(
                        f"Stage B prompt batch {b}{sc_label} — "
                        "cinematic prompts on disk, will not be re-billed"
                    )
            safe_action = (
                "Re-run episode — resolve the failure cause first, then re-run. "
                "VPD checkpoints on disk will be reused; planning stages will not re-bill."
            )
        else:
            reuse       = []
            safe_action = "Re-run episode — no VPD checkpoints found; VPD will run from scratch"

        blocked_action = "If BGA is enabled, review VPD output before re-running"
    elif is_quota or (is_muapi and is_quota):
        failure_class  = "config"
        why            = "MUAPI budget or quota exhausted. Add credits or raise MUAPI_EPISODE_BUDGET_CREDITS."
        reuse          = []
        safe_action    = "Add MUAPI credits, then re-run"
        blocked_action = "Re-running without adding credits will fail again"
    elif is_credential:
        failure_class  = "credential"
        why            = f"Credential or authentication failure in {failed_stage}: {error_msg[:120]}"
        reuse          = []
        safe_action    = "Refresh credentials in Platform Connections, then re-run"
        blocked_action = "Re-running without refreshing credentials will fail again"
    elif is_schema_error:
        failure_class  = "fatal"
        why            = f"Schema validation failed in {failed_stage} — output structure did not match expected schema."
        reuse          = []
        safe_action    = "Review error in logs; this requires a developer fix"
        blocked_action = "Re-running will fail again without a code fix"
    elif failed_stage == "thumbnail_director":
        # TD failure: EP/RD/NA/SE completed; VPD may be complete or partial.
        # Surface exactly what happens on rerun so the operator has full clarity.
        _TERM_SUCCESS = {"generated", "reused", "dry_run"}
        failure_class = "retryable"
        _ep_dir = _ASSETS_ROOT_PATH / "episodes" / episode_id
        reuse = []
        _ep_pkg_files = [
            (f"validation_scorecard_{episode_id}.json",
             "Executive Producer — on disk, will not re-bill"),
            (f"research_package_{episode_id}.json",
             "Research Director — on disk, will not re-bill"),
            (f"narrative_package_{episode_id}.json",
             "Narrative Architect — on disk, will not re-bill"),
            (f"script_package_{episode_id}.json",
             "Script Engine — on disk, will not re-bill"),
        ]
        for _fname, _label in _ep_pkg_files:
            if (_ep_dir / _fname).exists():
                reuse.append(_label)
        _vpd_path = _ep_dir / f"visual_package_{episode_id}.json"
        _vpd_complete    = False  # all scenes terminal-success
        _vpd_gen_count   = 0
        _vpd_fail_count  = 0
        _vpd_total_count = 0
        if _vpd_path.exists():
            try:
                _vpd = _json.loads(_vpd_path.read_text(encoding="utf-8"))
                _assets = _vpd.get("asset_manifest", {}).get("assets", [])
                _vpd_total_count = len(_assets)
                _vpd_gen_count   = sum(1 for _a in _assets if _a.get("generation_status") in _TERM_SUCCESS)
                _vpd_fail_count  = _vpd_total_count - _vpd_gen_count
                _vpd_complete    = bool(_assets) and _vpd_fail_count == 0
            except Exception:
                pass
            if _vpd_complete:
                reuse.append(
                    f"Visual Package Director — all {_vpd_gen_count} video clip"
                    f"{'s' if _vpd_gen_count != 1 else ''} on disk, will not re-bill"
                )
            elif _vpd_gen_count > 0:
                reuse.append(
                    f"Visual Package Director — {_vpd_gen_count}/{_vpd_total_count} clips on disk"
                    f" (will be reused); {_vpd_fail_count} clip"
                    f"{'s' if _vpd_fail_count != 1 else ''} failed and will be re-attempted via MUAPI"
                )
            else:
                reuse.append(
                    "Visual Package Director — visual_package on disk; no clips yet generated"
                )
        _is_malformed = (
            "Malformed" in error_msg
            or "could not parse" in error_msg.lower()
            or "malformed" in error_msg.lower()
        )
        if _vpd_complete:
            _td_timing = "Thumbnail Director will execute immediately after re-run starts"
            if _is_malformed:
                why = (
                    "Thumbnail Director hit a malformed API response (transient provider glitch). "
                    f"All {_vpd_gen_count} video clips are on disk. "
                    "Re-run skips directly to Thumbnail Director."
                )
            else:
                why = (
                    f"Thumbnail Director failed: {error_msg[:100]}. "
                    f"All {_vpd_gen_count} video clips are on disk — only TD will re-run."
                )
            safe_action = "Re-run episode — only Thumbnail Director will re-execute"
        else:
            _td_timing = (
                f"Thumbnail Director will execute after MUAPI regenerates "
                f"{_vpd_fail_count} remaining clip{'s' if _vpd_fail_count != 1 else ''}"
            )
            why = (
                f"Thumbnail Director failed: {error_msg[:80]}. "
                f"MUAPI video generation is partially complete "
                f"({_vpd_gen_count}/{_vpd_total_count} clips). "
                f"Re-run resumes MUAPI for the {_vpd_fail_count} failed clip"
                f"{'s' if _vpd_fail_count != 1 else ''}, then runs Thumbnail Director."
            )
            safe_action = (
                f"Re-run episode — MUAPI resumes {_vpd_fail_count} remaining clip"
                f"{'s' if _vpd_fail_count != 1 else ''}, then Thumbnail Director runs"
            )
        reuse.append(_td_timing)
        blocked_action = "Do not delete episode files before re-running"
    else:
        failure_class  = "retryable"
        why            = f"{failed_stage} failed: {error_msg[:160]}"
        reuse          = []
        safe_action    = "Re-run episode — likely a transient API or network error"
        blocked_action = "If the error recurs, investigate logs"

    # finance_gate sets can_rerun inline; all other classes use the default below
    if failure_class != "finance_gate":
        can_rerun = failure_class in ("resumable", "retryable") and bool(episode.get("topic") or episode.get("title"))

    return {
        "failed_stage":       failed_stage,
        "failure_class":      failure_class,
        "why":                why,
        "reuse_summary":      reuse,
        "safe_action":        safe_action,
        "blocked_action":     blocked_action,
        "can_rerun":          can_rerun,
        "vpd_checkpoints":    vpd_checkpoints,
        "finance_gate_detail": finance_gate_detail,
    }


def _episode_is_done(episode_id: str) -> bool:
    """The episode this run was for was later completed or published — a
    failed run of it is history, not a re-run candidate."""
    if not episode_id:
        return False
    try:
        from hypogeum.db import get_connection
        c = get_connection(_db_path())
        try:
            st = c.execute("SELECT status FROM episodes WHERE episode_id=?",
                           (episode_id,)).fetchone()
            if st and st[0] in ("complete", "completed", "published"):
                return True
            pub = c.execute("SELECT 1 FROM publication_intents "
                            "WHERE episode_id=? AND status='succeeded' LIMIT 1",
                            (episode_id,)).fetchone()
            return pub is not None
        finally:
            c.close()
    except Exception:  # noqa: BLE001
        return False


@app.route("/run/<run_id>")
def run_detail_page(run_id):
    detail = get_run_detail(run_id, db_path=_db_path())
    if detail is None:
        return render_template("not_found.html", kind="Run", id=run_id), 404
    recovery = _classify_run_recovery(detail) if detail["run"].get("status") == "failed" else {}
    superseded = (detail["run"].get("status") in ("failed", "cancelled")
                  and _episode_is_done(detail["run"].get("episode_id", "")))
    if superseded and recovery:
        recovery["can_rerun"] = False
        recovery["superseded"] = True
    return render_template("run_detail.html", d=detail, recovery=recovery,
                           superseded=superseded, fmt_duration=fmt_duration)


def _find_active_duplicate_topic(channel_id: str, topic_text: str, db_path) -> dict | None:
    """
    Look up an existing queued/claimed topic matching topic_text for channel_id.
    Returns {topic_id, status, source, priority, created_at} or None.
    """
    from hypogeum.db import get_connection as _gc
    try:
        conn = _gc(db_path)
        try:
            rows = conn.execute(
                """SELECT topic_id, status, source, priority, created_at, topic_text
                   FROM episode_topics
                   WHERE channel_id = ? AND status IN ('queued', 'claimed')
                   ORDER BY created_at ASC""",
                (channel_id,),
            ).fetchall()
        finally:
            conn.close()
        norm = topic_text.lower().split()
        for r in rows:
            row_norm = r["topic_text"].lower().split()
            if row_norm == norm:
                return dict(r)
        return None
    except Exception:
        return None


@app.route("/api/run/<run_id>/rerun", methods=["POST"])
def api_run_rerun(run_id: str):
    """
    Re-enqueue the episode from a failed run for a new pipeline execution.

    Cancels all existing queued (not claimed) episode_topics and run_queue items
    for this episode_id before inserting a fresh pair — ensures exactly ONE
    queued rerun row exists and the worker always picks the most recent intent.

    Returns:
        {ok, topic_id, episode_id, note}  — on success
        {ok, error}                        — on other errors
    """
    detail = get_run_detail(run_id, db_path=_db_path())
    if detail is None:
        return jsonify({"ok": False, "error": "run not found"}), 404

    run     = detail["run"]
    episode = detail["episode"]

    if run.get("status") != "failed":
        return jsonify({"ok": False, "error": "run is not in failed state", "status": run.get("status")}), 409

    topic = episode.get("topic") or episode.get("title", "")
    if not topic:
        return jsonify({"ok": False, "error": "episode has no topic text — cannot re-enqueue"}), 422

    # channel_id is on the runs row (V2 migration); episodes table has no channel_id column
    channel_id   = run.get("channel_id") or episode.get("channel_id") or "hypogeum"
    episode_id   = run.get("episode_id")  # preserve so worker reuses disk checkpoints

    # ── Cancel stale queued items for this episode before inserting fresh ones ──
    # Prevents the worker from picking an older queued row for the same episode
    # instead of this new rerun intent. Does not touch 'claimed' rows.
    if episode_id:
        try:
            from hypogeum.db import (
                get_connection as _gc,
                cancel_queued_topics_for_episode as _cqt,
                cancel_queued_run_queue_items_for_episode as _cqrq,
            )
            from datetime import datetime, timezone as _tz
            _now_iso = datetime.now(_tz.utc).isoformat()
            _conn = _gc(_db_path())
            try:
                _cqt(_conn, episode_id, _now_iso)
                _cqrq(_conn, episode_id, _now_iso)
                _conn.commit()
            finally:
                _conn.close()
        except Exception:
            pass  # non-fatal — stale rows will still lose to claim ordering

    result = enqueue_topic(
        channel_id, topic,
        priority=-1,   # negative = higher urgency than normal queued topics (0–10)
        db_path=_db_path(),
        source="rerun",
        episode_id=episode_id,
    )

    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error", "enqueue failed")}), 500

    # Create an immediate run_queue entry linked to this episode + recovery context.
    # The worker's claim query prefers source='rerun' at highest priority so this
    # item is claimed before any stale scheduled items in the same channel.
    try:
        from hypogeum.queue import enqueue as _enqueue_queue
        _enqueue_queue(
            channel_id=channel_id,
            episode_id=episode_id,
            priority=-10,
            source="rerun",
            recovery_from_run_id=run_id,
            db_path=_db_path(),
        )
    except Exception:
        pass  # non-blocking: episode_topics rerun row will still be picked up

    return jsonify({
        "ok":         True,
        "topic_id":   result.get("topic_id"),
        "episode_id": episode_id,
        "note":       (
            "Episode re-queued with the same episode_id. "
            "Stage A skeleton and completed Stage B prompt batches on disk "
            "will be reused — only incomplete stages will re-run."
        ),
    })


@app.route("/api/run/<run_id>/finance-approve", methods=["POST"])
def api_run_finance_approve(run_id: str):
    """
    Grant a single-use over-budget approval for a finance-gate-blocked run.

    Creates a finance_approvals record (status='pending', expires in 24h).
    On the next pipeline execution for this episode_id, the approval is
    consumed and the run proceeds past the budget ceiling.

    The run is automatically re-queued so the worker picks it up immediately.

    Request body (JSON):
        note: optional string — operator justification for the audit trail

    Returns:
        {ok, approval_id, episode_id, expires_at,
         estimated_spend_usd, ceiling_at_approval, overage_usd, note}
    """
    import uuid as _uuid_mod
    from datetime import datetime, timedelta, timezone as _tz

    detail = get_run_detail(run_id, db_path=_db_path())
    if detail is None:
        return jsonify({"ok": False, "error": "run not found"}), 404

    run     = detail["run"]
    episode = detail["episode"]
    episode_id = run.get("episode_id", "")

    if run.get("status") != "failed":
        return jsonify({"ok": False, "error": "run is not in failed state"}), 409

    # Load cost breakdown from on-disk finance record
    ep_dir = _ASSETS_ROOT_PATH / "episodes" / episode_id
    finance_record = None
    for fname in (
        f"finance_post_vpd_authorization_{episode_id}.json",
        f"finance_authorization_{episode_id}.json",
    ):
        fp = ep_dir / fname
        if fp.exists():
            try:
                finance_record = _json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                pass
            break

    if finance_record is None:
        return jsonify({
            "ok": False,
            "error": "no finance record found on disk for this episode — cannot approve",
        }), 422

    if finance_record.get("verdict") != "BLOCKED":
        return jsonify({
            "ok": False,
            "error": f"finance record verdict is '{finance_record.get('verdict')}', not BLOCKED",
        }), 422

    body = request.get_json(silent=True) or {}
    note = (body.get("note") or "").strip() or None

    estimated  = float(finance_record.get("estimated_spend_usd", 0.0))
    ceiling    = float(finance_record.get("budget_ceiling_usd", 0.0))
    overage    = round(estimated - ceiling, 4)
    now        = datetime.now(_tz.utc)
    expires_at = (now + timedelta(hours=24)).isoformat()
    approval_id = f"fa_{_uuid_mod.uuid4().hex[:16]}"

    from hypogeum.db import get_connection as _gc, insert_finance_approval
    try:
        conn = _gc(_db_path())
        try:
            insert_finance_approval(
                conn,
                approval_id=approval_id,
                episode_id=episode_id,
                run_id=run_id,
                approved_at=now.isoformat(),
                approved_by="operator",
                estimated_spend_usd=estimated,
                ceiling_at_approval=ceiling,
                overage_usd=overage,
                expires_at=expires_at,
                note=note,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"DB write failed: {exc}"}), 500

    # Re-queue with the same episode_id so the worker picks it up immediately
    topic = episode.get("topic") or episode.get("title", "")
    channel_id = run.get("channel_id") or episode.get("channel_id") or "hypogeum"
    if topic:
        try:
            enqueue_topic(channel_id, topic, priority=-1, db_path=_db_path(),
                          source="rerun", episode_id=episode_id)
            from hypogeum.queue import enqueue as _enqueue_queue
            _enqueue_queue(channel_id=channel_id, priority=-10, db_path=_db_path())
        except Exception:
            pass  # non-blocking: approval is stored; worker will pick up on next schedule tick

    return jsonify({
        "ok":                  True,
        "approval_id":         approval_id,
        "episode_id":          episode_id,
        "estimated_spend_usd": estimated,
        "ceiling_at_approval": ceiling,
        "overage_usd":         overage,
        "expires_at":          expires_at,
        "note":                note,
        "message":             (
            f"Approved. Run re-queued with episode_id {episode_id!r}. "
            f"The worker will proceed past the ${ceiling:.2f} ceiling "
            f"(estimated spend ${estimated:.4f}). "
            "Approval expires in 24 h and is single-use."
        ),
    })


@app.route("/api/topics/<topic_id>/cancel", methods=["POST"])
def api_topic_cancel(topic_id: str):
    """
    Cancel a queued topic (set status=cancelled). JSON endpoint.

    Only cancels topics with status='queued'. Claimed/running topics are
    not cancelled here — those require pipeline-level intervention.

    Returns {ok, topic_id, previous_status} or {ok, error, code}.
    """
    from hypogeum.db import get_connection as _gc
    from datetime import datetime, timezone
    db  = _db_path()
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _gc(db)
        try:
            row = conn.execute(
                "SELECT status FROM episode_topics WHERE topic_id=?", (topic_id,)
            ).fetchone()
            if row is None:
                return jsonify({"ok": False, "error": "topic not found", "code": "not_found"}), 404
            prev_status = row["status"]
            if prev_status != "queued":
                return jsonify({
                    "ok":    False,
                    "error": f"Cannot cancel topic with status='{prev_status}'. Only 'queued' topics may be cancelled here.",
                    "code":  "not_queued",
                }), 409
            conn.execute(
                "UPDATE episode_topics SET status='cancelled', updated_at=? WHERE topic_id=? AND status='queued'",
                (now, topic_id),
            )
            conn.commit()
        finally:
            conn.close()
        return jsonify({"ok": True, "topic_id": topic_id, "previous_status": prev_status})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/episode/<episode_id>/qa")
def episode_qa_page(episode_id: str):
    """Read-only QA inspection page for a single episode."""
    qa = _compute_episode_qa(episode_id)
    if qa is None:
        return render_template("not_found.html", kind="Episode", id=episode_id), 404
    return render_template("episode_qa.html", qa=qa, episode_id=episode_id)


@app.route("/api/episode/<episode_id>/qa")
def api_episode_qa(episode_id: str):
    qa = _compute_episode_qa(episode_id)
    if qa is None:
        return jsonify({"error": "episode not found"}), 404
    return jsonify(qa)


def _compute_episode_qa(episode_id: str) -> dict | None:
    """
    Compute QA metrics for an episode from disk artifacts only.
    Returns None if the episode directory does not exist.
    Never raises.
    """
    import subprocess as _sp
    import re as _re
    import math as _math
    from collections import defaultdict as _dd

    ep_dir = _ASSETS_ROOT_PATH / "episodes" / episode_id
    if not ep_dir.exists():
        return None

    def _jp(name):
        p = ep_dir / f"{name}_{episode_id}.json"
        if not p.exists():
            return {}
        try:
            import json as _j
            return _j.loads(p.read_text())
        except Exception:
            return {}

    def _ffprobe_dur(path) -> float | None:
        try:
            r = _sp.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=15)
            return float(r.stdout.strip()) if r.stdout.strip() else None
        except Exception:
            return None

    def _ffprobe_audio_dur(path) -> float | None:
        try:
            r = _sp.run(
                ["ffprobe", "-v", "quiet", "-select_streams", "a:0",
                 "-show_entries", "stream=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=15)
            return float(r.stdout.strip()) if r.stdout.strip() else None
        except Exception:
            return None

    # ── Video file (prefer v3, fall back to v1) ───────────────────────────
    v3 = ep_dir / f"main_{episode_id}_v3.mp4"
    v1 = ep_dir / f"main_{episode_id}.mp4"
    video_file = v3 if v3.exists() else (v1 if v1.exists() else None)
    video_version = "v3" if v3.exists() else ("v1" if v1.exists() else None)
    video_duration = _ffprobe_dur(video_file) if video_file else None
    audio_duration = _ffprobe_audio_dur(video_file) if video_file else None
    av_delta = round(abs(video_duration - audio_duration), 3) if (
        video_duration is not None and audio_duration is not None) else None
    video_size_mb = round(video_file.stat().st_size / 1024 / 1024, 1) if video_file else None

    # ── Scene list from visual_package ───────────────────────────────────
    vp = _jp("visual_package")
    scenes_def = vp.get("scenes", [])
    scene_ids = [s["scene_id"] for s in scenes_def]
    scene_count_expected = len(scene_ids)

    # ── Narration beats → per-scene durations ─────────────────────────────
    np_d = _jp("narration_package")
    segs = np_d.get("narration_segments", [])
    total_narration = round(sum(s["duration_seconds"] for s in segs), 3)
    # Beat b_001 → scene s01, b_002 → s02 etc.
    beat_dur = {}
    for i, s in enumerate(segs):
        sid = f"s{i+1:02d}"
        beat_dur[sid] = s["duration_seconds"]

    # ── Present scene clips (video/ AND video/sh_short_*/) ─────────────────
    clips_dir = ep_dir / "video"
    clip_files: dict[str, float] = {}       # scene_id -> primary clip duration
    extra_clip_count = 0
    if clips_dir.exists():
        _globs = list(clips_dir.glob("sc_*.mp4")) + list(clips_dir.glob("sh_short_*/sc_*.mp4"))
        for c in sorted(_globs):
            stem = c.stem.replace("sc_", "")
            if _re.search(r"__extra\d+$", stem) or stem.endswith(".enhanced"):
                extra_clip_count += 1
                continue
            dur = _ffprobe_dur(c)
            if dur is not None:
                clip_files[stem] = dur

    clip_count_present  = len(clip_files)
    missing_scene_ids   = [sid for sid in scene_ids if sid not in clip_files]

    # ── Pipeline artifacts (the REAL ones the pipeline writes) ─────────────
    frp = _jp("final_render_package")
    _pi = frp.get("package_integrity", {}) or {}
    render_ready = bool(_pi.get("production_ready"))
    render_status = (
        frp.get("render_status")
        or (frp.get("episode_render", {}) or {}).get("render_status")
        or "unknown"
    )
    music_log = frp.get("music_log") or []
    music_sourced = any(
        isinstance(m, dict) and (m.get("source") or m.get("asset_id")) for m in music_log
    )
    _cov = frp.get("coverage_check_summary") or {}
    _acr = frp.get("additional_coverage_required") or {}
    insufficient_beats = (
        _acr.get("insufficient_beats")
        or len(_acr.get("requests", []) or [])
        or len(_cov.get("insufficient_beats", []) or [])
        or 0
    )
    clip_avail_rate = _cov.get("clip_availability_rate", 0) or 0
    manifest_missing = _acr.get("requests", []) or []
    max_hold_manifest = _cov.get("max_scene_hold_seconds", 0) or 0

    # QA verdict — qa_approval_report is the sealed one; qa_report is the raw pass.
    _qa = _jp("qa_approval_report") or _jp("qa_report")
    qa_verdict = _qa.get("verdict") or _qa.get("overall_status")
    qa_failures = list(_qa.get("failures", []) or [])

    # ── Per-scene hold/loop (beats covered by video file) ─────────────────
    # Figure out how many beats fit in the video's audio duration
    if video_duration and segs:
        running = 0.0
        covered_beat_count = 0
        for s in segs:
            running += s["duration_seconds"]
            if running <= video_duration + 0.1:
                covered_beat_count += 1
    else:
        covered_beat_count = len(segs)

    scene_rows = []
    total_hold_s = 0.0
    total_unique_clip_s = 0.0
    for i, sid in enumerate(scene_ids[:covered_beat_count]):
        nar  = beat_dur.get(sid, 0)
        if sid in clip_files:
            clip = clip_files[sid]
            loops = round(nar / clip, 2) if clip > 0 else 0
            hold  = round(max(0, nar - clip), 2)
            total_hold_s += hold
            total_unique_clip_s += clip
        else:
            clip  = 0
            loops = 0
            hold  = round(nar, 2)
            total_hold_s += nar
        scene_rows.append({
            "scene_id":     sid,
            "nar_sec":      round(nar, 2),
            "clip_sec":     round(clip, 2),
            "loops":        loops,
            "hold_sec":     hold,
            "has_clip":     sid in clip_files,
            "in_video":     True,
        })
    # Scenes not covered by video
    for sid in scene_ids[covered_beat_count:]:
        nar = beat_dur.get(sid, 0)
        scene_rows.append({
            "scene_id": sid, "nar_sec": round(nar, 2), "clip_sec": 0,
            "loops": 0, "hold_sec": 0, "has_clip": sid in clip_files,
            "in_video": False,
        })

    freeze_seconds  = round(total_hold_s, 1)
    freeze_pct      = round(freeze_seconds / video_duration * 100, 1) if video_duration else 0

    # ── Repeated clip detection (exact file-size duplicate) ────────────────
    by_size: dict = {}
    if clips_dir.exists():
        for c in clips_dir.glob("sc_*.mp4"):
            sz = c.stat().st_size
            by_size.setdefault(sz, []).append(c.stem.replace("sc_", ""))
    repeated_groups = {sz: sids for sz, sids in by_size.items() if len(sids) > 1}
    repeated_seconds = round(sum(
        clip_files.get(sids[0], 0) * (len(sids) - 1)
        for sids in repeated_groups.values()
    ), 1)

    # ── Caption coverage ──────────────────────────────────────────────────
    srt_v3 = ep_dir / "captions" / "main_v3.srt"
    srt_v1 = ep_dir / "captions" / "main.srt"
    srt_file = srt_v3 if srt_v3.exists() else (srt_v1 if srt_v1.exists() else None)
    cap_blocks = 0
    cap_span_end = 0.0
    total_captioned = 0.0
    if srt_file:
        srt_txt = srt_file.read_text()
        blocks = [b.strip() for b in _re.split(r"\n\n+", srt_txt) if b.strip()]
        cap_blocks = len(blocks)
        for b in blocks:
            for line in b.splitlines():
                m = _re.match(
                    r"(\d+:\d+:\d+[,\.]\d+)\s*-->\s*(\d+:\d+:\d+[,\.]\d+)", line)
                if m:
                    def _ts(t):
                        t = t.replace(",", ".")
                        h, mn, s = t.split(":")
                        return int(h) * 3600 + int(mn) * 60 + float(s)
                    s0, e0 = _ts(m.group(1)), _ts(m.group(2))
                    total_captioned += e0 - s0
                    cap_span_end = max(cap_span_end, e0)
    cap_coverage_pct = round(total_captioned / video_duration * 100, 1) if video_duration else 0
    cap_gap_seconds  = round(max(0, video_duration - cap_span_end), 1) if video_duration else 0

    # ── Verdict ───────────────────────────────────────────────────────────
    checks = []
    def _check(name, ok, warn, detail):
        if ok:
            status = "pass"
        elif warn:
            status = "warn"
        else:
            status = "fail"
        checks.append({"name": name, "status": status, "detail": detail})

    _check("Video file exists",
           ok=video_file is not None, warn=False,
           detail=f"{video_file.name} ({video_size_mb}MB)" if video_file else "No video file found")
    _check("All scenes present",
           ok=len(missing_scene_ids) == 0,
           warn=0 < len(missing_scene_ids) <= 3,
           detail=f"{clip_count_present}/{scene_count_expected} clips present, "
                  f"missing: {missing_scene_ids or 'none'}")
    _check("Freeze/loop < 30%",
           ok=freeze_pct < 10, warn=freeze_pct < 30,
           detail=f"{freeze_seconds}s held/looped ({freeze_pct}% of video)")
    _check("No repeated clips",
           ok=len(repeated_groups) == 0, warn=False,
           detail=f"{len(repeated_groups)} exact duplicate group(s)" if repeated_groups else "No exact duplicates")
    _check("Caption coverage > 95%",
           ok=cap_coverage_pct >= 95, warn=cap_coverage_pct >= 85,
           detail=f"{cap_coverage_pct}% ({cap_blocks} blocks, gap after {cap_span_end:.0f}s)")
    _check("A/V sync delta < 1s",
           ok=av_delta is not None and av_delta < 1.0, warn=False,
           detail=f"delta={av_delta}s" if av_delta is not None else "could not measure")
    _check("Render status complete",
           ok=render_status in ("complete", "completed"), warn=render_status == "partial",
           detail=f"render_status={render_status}, availability={clip_avail_rate*100:.0f}%")
    _check("Pipeline render_package viable",
           ok=render_ready, warn=False,
           detail="production_ready=True" if render_ready
                  else "production_ready=False — assembly did not produce a viable package")
    _check("Coverage sufficient",
           ok=insufficient_beats == 0, warn=0 < insufficient_beats <= 2,
           detail=(f"{insufficient_beats} beat(s) short of visual coverage"
                   if insufficient_beats else
                   f"narration {total_narration:.0f}s vs video "
                   f"{round(total_unique_clip_s):.0f}s covered"))
    _check("Background music sourced",
           ok=music_sourced, warn=False,
           detail=f"{len(music_log)} music_log entr(y/ies)" if music_sourced
                  else "music_log empty — render would be silent")
    if qa_verdict:
        _check("QA Agent verdict",
               ok=str(qa_verdict).upper() in ("APPROVED", "PASS"),
               warn=str(qa_verdict).upper() in ("WARN", "WARNING"),
               detail=f"verdict={qa_verdict}"
                      + (f" — {qa_failures[0]}" if qa_failures else ""))

    fails = sum(1 for c in checks if c["status"] == "fail")
    warns = sum(1 for c in checks if c["status"] == "warn")
    # The QA Agent's own sealed verdict wins when present.
    if qa_verdict and str(qa_verdict).upper() == "REJECTED":
        verdict = "FAIL"
    elif qa_verdict and str(qa_verdict).upper() == "APPROVED" and fails == 0:
        verdict = "PASS" if warns == 0 else "WARN"
    else:
        verdict = "PASS" if fails == 0 and warns == 0 else ("WARN" if fails == 0 else "FAIL")

    narration_video_ratio_pct = (
        round(total_unique_clip_s / total_narration * 100, 1) if total_narration else None
    )

    return {
        "episode_id":           episode_id,
        "video_file":           video_file.name if video_file else None,
        "video_version":        video_version,
        "video_size_mb":        video_size_mb,
        "total_duration_s":     round(video_duration, 1) if video_duration else None,
        "total_duration_min":   round(video_duration / 60, 1) if video_duration else None,
        "total_narration_s":    total_narration,
        "narration_beats_total": len(segs),
        "beats_covered_in_video": covered_beat_count,
        "clip_count_present":   clip_count_present,
        "scene_count_expected": scene_count_expected,
        "missing_scene_ids":    missing_scene_ids,
        "missing_count":        len(missing_scene_ids),
        "repeated_groups":      list(repeated_groups.values()),
        "repeated_seconds":     repeated_seconds,
        "total_hold_seconds":   freeze_seconds,
        "freeze_pct":           freeze_pct,
        "total_unique_clip_s":  round(total_unique_clip_s, 1),
        "max_hold_s":           max_hold_manifest,
        "caption_file":         srt_file.name if srt_file else None,
        "caption_blocks":       cap_blocks,
        "total_captioned_s":    round(total_captioned, 1),
        "caption_coverage_pct": cap_coverage_pct,
        "caption_gap_s":        cap_gap_seconds,
        "audio_duration_s":     round(audio_duration, 3) if audio_duration else None,
        "av_delta_s":           av_delta,
        "render_status":        render_status,
        "clip_avail_rate_pct":  round(clip_avail_rate * 100, 1),
        "scene_rows":           scene_rows,
        "checks":               checks,
        "verdict":              verdict,
        "fail_count":           fails,
        "warn_count":           warns,
        "extra_clip_count":     extra_clip_count,
        "render_ready":         render_ready,
        "qa_verdict":           qa_verdict,
        "qa_failures":          qa_failures,
        "music_sourced":        music_sourced,
        "coverage": {
            "narration_s":        round(total_narration, 1),
            "video_s":            round(total_unique_clip_s, 1),
            "ratio_pct":          narration_video_ratio_pct,
            "insufficient_beats": insufficient_beats,
        },
    }


@app.route("/episode/<episode_id>")
def episode_detail_page(episode_id):
    d = get_episode_detail(episode_id, db_path=_db_path())
    # Fall back to a minimal dict for disk-only episodes (produced via scripts)
    if d is None:
        artifacts = get_episode_artifacts(episode_id, assets_root=_ASSETS_ROOT_PATH)
        if not artifacts["exists"]:
            return render_template("not_found.html", kind="Episode", id=episode_id), 404
        # Minimal dict so episode_detail.html renders without crashing
        d = _make_disk_episode_detail(episode_id, artifacts)
    else:
        # Enrich DB episode with disk artifact info and four-way state
        artifacts = get_episode_artifacts(episode_id, assets_root=_ASSETS_ROOT_PATH)
        d["disk_artifacts"] = artifacts
        # Compute four-way state from disk packages (even for DB episodes)
        if artifacts.get("exists"):
            ep_dir = _ASSETS_ROOT_PATH / "episodes" / episode_id
            _qa_pkg   = load_disk_pkg(ep_dir, "qa_approval_report", episode_id)
            _tn_pkg   = load_disk_pkg(ep_dir, "thumbnail_package", episode_id)
            _bga_pkgs = []
            for _pn in (1, 2):
                _pkg = load_disk_pkg(ep_dir, f"bga_compliance_report_p{_pn}", episode_id)
                if _pkg:
                    _bga_pkgs.append(_pkg)
            _art = {
                "has_render":    artifacts.get("has_render", False),
                "has_thumbnail": artifacts.get("has_thumbnail", False),
            }
            _outcomes = [
                {"publishing_status": p.get("publishing_status"), "platform": p.get("platform")}
                for p in d.get("platforms", [])
            ]
            ep_state = build_episode_state(
                _qa_pkg, _bga_pkgs, _tn_pkg, _art,
                publishing_outcomes=_outcomes or None,
            )
            d.update(ep_state)
            d["thumbnail_status_label"] = thumbnail_status_label(_tn_pkg, _art.get("has_thumbnail", False))
            # Publish checklist for DB episodes too
            _disk_qa_summary = _make_disk_qa_summary(episode_id)
            d["publishing"]["checklist"] = _build_disk_publish_checklist(
                episode_id, ep_state, _disk_qa_summary, artifacts, _tn_pkg
            )
            # Titles from disk if no DB titles
            if not d.get("titles", {}).get("candidates"):
                _tb = load_disk_pkg(ep_dir, "topic_brief", episode_id)
                d["titles"] = _extract_disk_title_candidates(episode_id, ep_dir, _tb)

            # Thumbnails from disk when DB has no candidates (thumbnail_candidates table
            # is only populated by the live pipeline; disk-synced episodes have none)
            if not d.get("thumbnails", {}).get("candidates") and artifacts.get("has_thumbnail"):
                d["thumbnails"] = _make_disk_thumbnails(episode_id, artifacts)

    # Archive summary — where this episode's catalogued assets live now.
    try:
        from hypogeum.db import get_connection as _gc
        _c = _gc(_db_path())
        try:
            _tot = _c.execute("SELECT COUNT(*) FROM archive_index WHERE episode_id=?",
                              (episode_id,)).fetchone()[0]
            _moved = _c.execute("SELECT COUNT(*) FROM archive_index "
                                "WHERE episode_id=? AND moved_at IS NOT NULL",
                                (episode_id,)).fetchone()[0]
            _loc = _c.execute("SELECT archive_location FROM archive_index "
                              "WHERE episode_id=? AND moved_at IS NOT NULL LIMIT 1",
                              (episode_id,)).fetchone()
        finally:
            _c.close()
        _root = None
        if _loc and _loc[0]:
            import os as _os
            _root = _os.path.dirname(_os.path.dirname(_loc[0]))  # …/<channel>/<episode>
        d["archive"] = {
            "total": _tot, "archived": _moved, "local": _tot - _moved,
            "state": ("archived" if _tot and _moved == _tot
                      else "partial" if _moved else "local" if _tot else "none"),
            "location": _root,
        }
    except Exception:  # noqa: BLE001
        d["archive"] = {"total": 0, "archived": 0, "local": 0, "state": "none", "location": None}

    valid_tabs = {"production", "publishing", "thumbnails", "titles",
                  "analytics", "revenue", "learning", "branding", "artifacts"}
    active_tab = request.args.get("tab", "production")
    if active_tab not in valid_tabs:
        active_tab = "production"
    return render_template("episode_detail.html", d=d, active_tab=active_tab)


def _load_disk_pkg(episode_id: str, stem: str) -> dict:
    """Load a JSON package from the episode disk directory. Returns {} on any error."""
    import json as _j
    ep_dir = _ASSETS_ROOT_PATH / "episodes" / episode_id
    path = ep_dir / f"{stem}_{episode_id}.json"
    if not path.exists():
        return {}
    try:
        return _j.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _make_disk_thumbnails(episode_id: str, artifacts: dict) -> dict:
    """Build thumbnails dict from thumbnail_package on disk."""
    pkg = _load_disk_pkg(episode_id, "thumbnail_package")
    if not pkg:
        return {"candidates": [], "summary": {"count": 0, "has_selected": False, "has_real_data": False}}

    # Build candidate list from package candidates or fall back to main_thumbnail
    raw_cands = pkg.get("candidates") or []
    thumb_dir = _ASSETS_ROOT_PATH / "episodes" / episode_id / "thumbnails"

    # Read BGA identity report
    identity = pkg.get("thumbnail_identity_report") or {}
    bga_verdict = identity.get("verdict")
    bga_violations = identity.get("violations") or []

    def _base_cand(rank, selected, text_overlay, placement, visual_prompt,
                   img_file, unverified, score, score_reason, bga_v, bga_viol):
        return {
            "rank":             rank,
            "text_overlay":     text_overlay,
            "placement":        placement,
            "visual_prompt":    visual_prompt,
            "selected":         selected,
            # file_path must be set so the template's {% elif c.file_path %} branch
            # routes each card to /thumbnail/candidate/<rank> instead of falling
            # through to {% elif c.image_path %} which always serves main.jpg.
            "file_path":        img_file,
            "image_path":       img_file,
            "unverified":       unverified,
            "score":            score,
            "score_reason":     score_reason,
            "bga_verdict":      bga_v,
            "bga_violations":   bga_viol,
            # Performance fields — not available for disk-only
            "ctr":              None,
            "impressions":      None,
            "views":            None,
            "performance_status": "awaiting_analytics" if selected else "not_published",
            "linked_platforms": [],
            "model":            None,
            "seed":             None,
        }

    # Pull text_overlay / visual_prompt from generation plan rank-1 candidate
    # when thumbnail_package.candidates is empty (common for disk-synced episodes).
    _gen_plan = _load_disk_pkg(episode_id, "thumbnail_generation_plan")
    _gen_ranked = (_gen_plan.get("main_episode") or {}).get("ranked_candidates") or []
    _gen_rank1 = _gen_ranked[0] if _gen_ranked else {}
    _gen_overlay_raw = _gen_rank1.get("text_overlay") or {}
    _gen_text_overlay = (
        _gen_overlay_raw.get("text") if isinstance(_gen_overlay_raw, dict)
        else str(_gen_overlay_raw)
    ) or None
    _gen_placement    = _gen_overlay_raw.get("placement") if isinstance(_gen_overlay_raw, dict) else None
    _gen_visual       = (_gen_rank1.get("image_prompt") or "")[:160] or None

    candidates = []
    if raw_cands:
        for i, c in enumerate(raw_cands):
            # Resolve by rank (authoritative) then fall back to position index.
            cand_rank = c.get("rank", i + 1)
            file_candidates = [
                f"candidate_{cand_rank}.jpg", f"candidate_{cand_rank}.png",
                f"candidate_{cand_rank}.webp",
                # Legacy fallback: position-based names
                f"candidate_{i+1}.jpg",
            ]
            img_file = None
            for fn in file_candidates:
                p = thumb_dir / fn
                if p.exists() and p.stat().st_size > 0:
                    img_file = str(p)
                    break
            is_sel = bool(c.get("selected"))
            candidates.append(_base_cand(
                rank=cand_rank,
                selected=is_sel,
                text_overlay=c.get("text_overlay"),
                placement=c.get("placement"),
                visual_prompt=(c.get("visual_prompt") or "")[:160] or None,
                img_file=img_file,
                unverified=img_file is None,
                score=c.get("score"),
                score_reason=c.get("score_reason"),
                bga_v=bga_verdict if is_sel else None,
                bga_viol=bga_violations if is_sel else [],
            ))
    else:
        # No candidates in package — synthesise one from main_thumbnail + generation plan
        img_path = None
        for ext in ("jpg", "jpeg", "png", "webp"):
            p = thumb_dir / f"main.{ext}"
            if p.exists() and p.stat().st_size > 0:
                img_path = str(p)
                break
        candidates.append(_base_cand(
            rank=1,
            selected=True,
            text_overlay=_gen_text_overlay,
            placement=_gen_placement,
            visual_prompt=_gen_visual,
            img_file=img_path,
            unverified=img_path is None,
            score=None,
            score_reason=None,
            bga_v=bga_verdict,
            bga_viol=bga_violations,
        ))

    has_selected = any(c["selected"] for c in candidates)
    required = pkg.get("candidates_required", 3)
    generated = pkg.get("candidates_generated", len([c for c in candidates if not c["unverified"]]))
    missing   = max(0, required - len(candidates))

    return {
        "candidates":         candidates,
        "summary":            {"count": len(candidates), "has_selected": has_selected, "has_real_data": False},
        "bga_verdict":        bga_verdict,
        "bga_violations":     bga_violations,
        "from_disk":          True,
        # A/B/C testing status
        "candidates_required": required,
        "candidates_count":    len(candidates),
        "candidates_missing":  missing,
        "candidate_warning":   missing > 0,
        "candidate_warning_msg": (
            f"Only {len(candidates)}/{required} thumbnail candidate{'s' if len(candidates) != 1 else ''} generated — "
            f"A/B/C testing requires {required}"
        ) if missing > 0 else None,
    }


def _make_disk_script_sections(episode_id: str) -> list:
    """Extract readable script sections from script_package JSON."""
    pkg = _load_disk_pkg(episode_id, "script_package")
    if not pkg:
        return []
    sections = []
    # Try scenes list
    scenes = pkg.get("scenes") or pkg.get("script_scenes") or []
    if scenes:
        for sc in scenes:
            sid = sc.get("scene_id") or sc.get("id") or ""
            narration = sc.get("narration_text") or sc.get("narration") or sc.get("text") or ""
            visual = sc.get("visual_description") or sc.get("visual") or ""
            if narration or visual:
                sections.append({
                    "scene_id": sid,
                    "narration": narration[:600] if narration else None,
                    "visual": visual[:300] if visual else None,
                })
    # Fallback: hook/intro
    hook = pkg.get("hook") or pkg.get("intro_hook") or ""
    if hook and not sections:
        sections.append({"scene_id": "hook", "narration": hook[:300], "visual": None})
    return sections[:8]  # show first 8 scenes


def _make_disk_qa_summary(episode_id: str) -> dict:
    """Extract readable QA summary from qa_approval_report JSON."""
    pkg = _load_disk_pkg(episode_id, "qa_approval_report")
    if not pkg:
        pkg = _load_disk_pkg(episode_id, "qa_report")
    if not pkg:
        return {}
    verdict = pkg.get("verdict") or pkg.get("overall_verdict") or pkg.get("status")
    approved = pkg.get("approved")
    checks = pkg.get("checks") or pkg.get("qa_checks") or []
    issues = pkg.get("issues") or pkg.get("failures") or []
    warnings = pkg.get("warnings") or []

    readable_checks = []
    for c in checks[:12]:
        if isinstance(c, dict):
            readable_checks.append({
                "name":   c.get("check_id") or c.get("name") or c.get("check") or "",
                "status": c.get("status") or ("pass" if c.get("passed") else "fail"),
                "detail": (c.get("detail") or c.get("message") or "")[:200],
            })

    return {
        "verdict":  verdict,
        "approved": approved,
        "checks":   readable_checks,
        "issues":   [str(i)[:200] for i in issues[:5]],
        "warnings": [str(w)[:200] for w in warnings[:5]],
        "check_count": len(checks),
    }


_DISK_STATUS_LABELS = {
    "complete":     "Complete",
    "qa_warning":   "QA Warning",
    "needs_review": "Needs Review",
    "partial":      "Partial",
    "failed":       "Failed",
}


def _disk_episode_status(episode_id: str, artifacts: dict) -> str:
    """Derive canonical status from disk packages using disk_sync logic."""
    ep_dir = _ASSETS_ROOT_PATH / "episodes" / episode_id
    qa_pkg = load_disk_pkg(ep_dir, "qa_approval_report", episode_id)
    if not qa_pkg:
        qa_pkg = load_disk_pkg(ep_dir, "qa_report", episode_id)
    thumbnail_pkg = load_disk_pkg(ep_dir, "thumbnail_package", episode_id)
    bga_pkgs = []
    for pn in (1, 2):
        pkg = load_disk_pkg(ep_dir, f"bga_compliance_report_p{pn}", episode_id)
        if pkg:
            bga_pkgs.append(pkg)
    return infer_episode_status(qa_pkg, bga_pkgs, artifacts, thumbnail_pkg=thumbnail_pkg)


def _disk_resume_actions(episode_id: str, artifacts: dict) -> dict:
    """
    Determine which resume/action buttons to show for a disk-only episode.

    Rules:
      show_resume_narration  = narration missing
      show_rebuild_assembly  = render missing OR (qa stale AND artifacts changed)
      show_download_render   = render exists
    """
    ep_dir = _ASSETS_ROOT_PATH / "episodes" / episode_id

    has_render    = bool(artifacts.get("has_render"))
    has_narration = bool(artifacts.get("has_narration"))

    # Narration complete = narration package exists AND segments are generated
    narration_complete = False
    nar_pkg = load_disk_pkg(ep_dir, "narration_package", episode_id)
    if nar_pkg:
        segs = nar_pkg.get("narration_segments") or []
        narration_complete = any(
            s.get("generation_status") == "generated" for s in segs
        )

    return {
        "show_resume_narration": has_narration is False and not narration_complete,
        "show_rebuild_assembly": not has_render,
        "show_download_render":  has_render,
    }


def _extract_disk_title_candidates(
    episode_id: str,
    ep_dir,
    topic_brief: dict,
) -> dict:
    """
    Build a titles dict from disk packages for disk-only episodes.

    Priority:
      1. topic_brief.episode_title (the working title from EP)
      2. script_package.episode_metadata.topic
      No title DB rows exist for disk-only episodes.

    Returns the same shape as episode_detail.get_episode_detail()["titles"].
    """
    candidates = []

    title_text = (
        topic_brief.get("episode_title")
        or None
    )
    if not title_text:
        sp = load_disk_pkg(ep_dir, "script_package", episode_id)
        title_text = (sp.get("episode_metadata") or {}).get("topic") or None

    if title_text:
        from hypogeum.episode_detail import _classify_title_format, _FORMAT_LABELS
        fmt = _classify_title_format(title_text)
        candidates.append({
            "title_id":      f"disk_{episode_id}_title",
            "episode_id":    episode_id,
            "title_text":    title_text,
            "selected":      True,
            "format_type":   fmt,
            "format_label":  _FORMAT_LABELS.get(fmt, "Other"),
            "ctr":           None,
            "impressions":   None,
            "views":         None,
            "performance_status": "awaiting_analytics",
            "from_disk":     True,
        })
        return {
            "candidates": candidates,
            "summary": {"count": 1, "has_selected": True, "has_real_data": False},
            "from_disk": True,
        }

    return {
        "candidates": [],
        "summary": {"count": 0, "has_selected": False, "has_real_data": False},
        "no_title_package": True,
        "from_disk": True,
    }


def _build_disk_publish_checklist(
    episode_id: str,
    ep_state: dict,
    qa_summary: dict,
    artifacts: dict,
    thumbnail_pkg: dict | None,
) -> list[dict]:
    """
    Build a publish readiness checklist for the Publishing tab.

    Each item: {label, status, detail, action_url, action_label}
    status: pass | fail | warn | missing

    Checklist order:
      1. Video render
      2. QA gate
      3. Title present
      4. Description present
      5. Thumbnail file (separate from brand approval)
      6. Thumbnail brand check (TIG) — clearly distinct from file existence
      7. Captions (SRT)
      8. YouTube credential state
      9. Release gate (summary)
    """
    checklist = []

    # 1. Render file
    has_render = bool(artifacts.get("has_render"))
    checklist.append({
        "label":        "Video render",
        "status":       "pass" if has_render else "fail",
        "detail":       "Render file present" if has_render else "No render file — run assembly",
        "action_url":   None,
        "action_label": None,
    })

    # 2. QA gate
    qa_verdict  = (qa_summary.get("verdict") or "").upper()
    qa_approved = bool(qa_summary.get("approved"))
    if qa_approved and qa_verdict == "WARNING":
        checklist.append({
            "label":        "QA gate",
            "status":       "warn",
            "detail":       "Approved with warnings — review QA report before publishing",
            "action_url":   f"/episode/{episode_id}/artifact/qa_approval_report",
            "action_label": "View QA report",
        })
    elif qa_approved:
        checklist.append({
            "label":        "QA gate",
            "status":       "pass",
            "detail":       "QA approved",
            "action_url":   None,
            "action_label": None,
        })
    elif qa_summary:
        checklist.append({
            "label":        "QA gate",
            "status":       "fail",
            "detail":       "QA not approved — see QA report for failures",
            "action_url":   f"/episode/{episode_id}/artifact/qa_approval_report",
            "action_label": "View QA report",
        })
    else:
        checklist.append({
            "label":        "QA gate",
            "status":       "missing",
            "detail":       "No QA report found",
            "action_url":   None,
            "action_label": None,
        })

    # 3. Title present
    # Check QA report's c_metadata_complete check, then fall back to topic_brief
    ep_dir = _ASSETS_ROOT_PATH / "episodes" / episode_id
    topic_brief = load_disk_pkg(ep_dir, "topic_brief", episode_id)
    title_text  = topic_brief.get("episode_title") or None
    if not title_text:
        sp = load_disk_pkg(ep_dir, "script_package", episode_id)
        title_text = (sp.get("episode_metadata") or {}).get("topic") or None
    # Also check QA metadata check
    _meta_check = next(
        (c for c in (qa_summary.get("checks") or [])
         if c.get("check_id") == "c_metadata_complete" and c.get("status") == "pass"),
        None,
    )
    if _meta_check or title_text:
        checklist.append({
            "label":        "Title",
            "status":       "pass",
            "detail":       (f'"{title_text[:60]}"' if title_text else "Title confirmed present by QA"),
            "action_url":   f"/episode/{episode_id}?tab=titles",
            "action_label": "View titles",
        })
    else:
        checklist.append({
            "label":        "Title",
            "status":       "missing",
            "detail":       "No title found in topic brief or script package",
            "action_url":   f"/episode/{episode_id}?tab=titles",
            "action_label": "View titles",
        })

    # 4. Description present
    _desc_check = next(
        (c for c in (qa_summary.get("checks") or [])
         if c.get("check_id") == "c_description_present" and c.get("status") == "pass"),
        None,
    )
    logline = topic_brief.get("logline") or None
    if _desc_check:
        checklist.append({
            "label":        "Description",
            "status":       "pass",
            "detail":       "Description confirmed present by QA",
            "action_url":   None,
            "action_label": None,
        })
    elif logline:
        checklist.append({
            "label":        "Description",
            "status":       "warn",
            "detail":       "Topic logline available as description draft (not yet in publish metadata)",
            "action_url":   None,
            "action_label": None,
        })
    else:
        checklist.append({
            "label":        "Description",
            "status":       "missing",
            "detail":       "No description found — required for YouTube publish",
            "action_url":   None,
            "action_label": None,
        })

    # 5. Thumbnail file (separate from brand approval — file existence only)
    has_thumbnail = bool(artifacts.get("has_thumbnail"))
    if has_thumbnail:
        checklist.append({
            "label":        "Thumbnail file",
            "status":       "pass",
            "detail":       "Thumbnail file present on disk",
            "action_url":   f"/episode/{episode_id}?tab=thumbnails",
            "action_label": "View",
        })
    else:
        checklist.append({
            "label":        "Thumbnail file",
            "status":       "fail",
            "detail":       "No thumbnail file on disk",
            "action_url":   None,
            "action_label": None,
        })

    # 6. Thumbnail brand approval (TIG) — clearly distinct from file existence
    if not has_thumbnail:
        checklist.append({
            "label":        "Thumbnail brand approval (TIG)",
            "status":       "fail",
            "detail":       "Cannot check — no thumbnail file",
            "action_url":   None,
            "action_label": None,
        })
    elif thumbnail_pkg:
        tig = thumbnail_pkg.get("thumbnail_identity_report") or {}
        tig_verdict = (tig.get("verdict") or "").upper()
        if tig_verdict == "BLOCKED":
            violations = tig.get("violations") or []
            blocking   = [v for v in violations if (v.get("severity") or "").upper() == "BLOCKING"]
            detail     = f"File exists but TIG rejected — {blocking[0]['description'][:70]}" if blocking else "File exists but TIG rejected"
            checklist.append({
                "label":        "Thumbnail brand approval (TIG)",
                "status":       "fail",
                "detail":       detail,
                "action_url":   f"/episode/{episode_id}?tab=thumbnails",
                "action_label": "View violations",
            })
        elif tig_verdict == "WARNING":
            n_warn = tig.get("warning_count", 0)
            checklist.append({
                "label":        "Thumbnail brand approval (TIG)",
                "status":       "warn",
                "detail":       f"File exists — TIG approved with {n_warn} warning(s)",
                "action_url":   f"/episode/{episode_id}?tab=thumbnails",
                "action_label": "View warnings",
            })
        elif tig_verdict == "APPROVED":
            checklist.append({
                "label":        "Thumbnail brand approval (TIG)",
                "status":       "pass",
                "detail":       "File exists — approved by brand identity check",
                "action_url":   None,
                "action_label": None,
            })
        else:
            checklist.append({
                "label":        "Thumbnail brand approval (TIG)",
                "status":       "missing",
                "detail":       "File exists — no TIG check recorded",
                "action_url":   f"/episode/{episode_id}?tab=thumbnails",
                "action_label": "View thumbnail",
            })
    else:
        checklist.append({
            "label":        "Thumbnail brand approval (TIG)",
            "status":       "missing",
            "detail":       "File exists — no thumbnail package found (TIG not run)",
            "action_url":   None,
            "action_label": None,
        })

    # 7. Captions
    has_captions = bool(artifacts.get("has_captions"))
    checklist.append({
        "label":        "Captions (SRT)",
        "status":       "pass" if has_captions else "missing",
        "detail":       "Captions file present" if has_captions else "No captions file found",
        "action_url":   None,
        "action_label": None,
    })

    # 8. YouTube credential
    try:
        from hypogeum.credential_manager import list_secret_profiles
        yt_creds = list_secret_profiles(provider="youtube", db_path=_db_path())
        yt_active = [c for c in yt_creds if c.status == "active"]
        if yt_active:
            checklist.append({
                "label":        "YouTube credential",
                "status":       "warn",
                "detail":       "Credential saved — token format checked only, not verified via live API",
                "action_url":   "/platforms/connect/youtube",
                "action_label": "Verify",
            })
        else:
            checklist.append({
                "label":        "YouTube credential",
                "status":       "missing",
                "detail":       "No YouTube credential saved",
                "action_url":   "/platforms/connect/youtube",
                "action_label": "Connect YouTube",
            })
    except Exception:
        checklist.append({
            "label":        "YouTube credential",
            "status":       "missing",
            "detail":       "Could not check credential state",
            "action_url":   "/platforms/connect/youtube",
            "action_label": "Connect YouTube",
        })

    # 9. Release gate summary
    release_status = ep_state.get("release_status")
    release_label  = ep_state.get("release_blocker_label")
    if release_status == REL_READY:
        checklist.append({
            "label":        "Release gate",
            "status":       "pass",
            "detail":       "All release checks passed — ready to publish",
            "action_url":   None,
            "action_label": None,
        })
    elif release_status == REL_PENDING:
        checklist.append({
            "label":        "Release gate",
            "status":       "warn",
            "detail":       release_label or "Pending review before publishing",
            "action_url":   None,
            "action_label": None,
        })
    else:
        checklist.append({
            "label":        "Release gate",
            "status":       "fail",
            "detail":       release_label or "Release blocked",
            "action_url":   None,
            "action_label": None,
        })

    return checklist


def _make_disk_episode_detail(episode_id: str, artifacts: dict) -> dict:
    """Build an episode detail dict from disk artifacts — reads JSON packages for readable content."""
    thumbnails = _make_disk_thumbnails(episode_id, artifacts)
    script_sections = _make_disk_script_sections(episode_id)
    qa_summary = _make_disk_qa_summary(episode_id)

    ep_dir = _ASSETS_ROOT_PATH / "episodes" / episode_id

    # Load packages needed for state computation
    qa_pkg        = load_disk_pkg(ep_dir, "qa_approval_report", episode_id)
    if not qa_pkg:
        qa_pkg    = load_disk_pkg(ep_dir, "qa_report", episode_id)
    thumbnail_pkg = load_disk_pkg(ep_dir, "thumbnail_package", episode_id)
    bga_pkgs      = []
    for _pn in (1, 2):
        _pkg = load_disk_pkg(ep_dir, f"bga_compliance_report_p{_pn}", episode_id)
        if _pkg:
            bga_pkgs.append(_pkg)

    # Topic / title from narrative or script package
    narrative_pkg = _load_disk_pkg(episode_id, "narrative_package")
    topic_brief   = load_disk_pkg(ep_dir, "topic_brief", episode_id)
    topic = (
        narrative_pkg.get("episode_title")
        or narrative_pkg.get("topic")
        or topic_brief.get("episode_title")
        or artifacts.get("display_title")
        or episode_id
    )

    # Titles from topic brief / script sections (fallback when no DB titles)
    disk_title_candidates = _extract_disk_title_candidates(episode_id, ep_dir, topic_brief)

    # Four-way state (production/release/publish/analytics)
    ep_state = build_episode_state(
        qa_pkg=qa_pkg,
        bga_pkgs=bga_pkgs,
        thumbnail_pkg=thumbnail_pkg,
        artifacts=artifacts,
    )

    # Single-value legacy status for DB compat and badge rendering
    status       = _disk_episode_status(episode_id, artifacts)
    status_label = _DISK_STATUS_LABELS.get(status, status.replace("_", " ").title())

    # Extract cost and duration from finance/render packages
    ep_dir     = _ASSETS_ROOT_PATH / "episodes" / episode_id
    cost_usd   = extract_total_cost(ep_dir, episode_id)
    dur_ms     = extract_render_duration_ms(ep_dir, episode_id)
    scene_cnt  = extract_scene_count(ep_dir, episode_id)
    created_at = extract_created_at(ep_dir, episode_id)

    run_dur_fmt = fmt_duration(dur_ms) if dur_ms else None

    # Build synthetic stage groups from discovered packages.
    # Multiple stages sharing a group_label (Script, Visuals) must be merged
    # into one group entry — not appended as separate rows.
    _groups_by_label: dict[str, dict] = {}
    for stage_name, group_label, pkg_stem in SYNTHETIC_STAGES:
        pkg = load_disk_pkg(ep_dir, pkg_stem, episode_id)
        if not pkg:
            continue
        stage_row = {
            "stage_exec_id": f"disk_{episode_id}_{stage_name}",
            "stage_name":    stage_name,
            "status":        "completed",
            "started_at":    created_at,
            "completed_at":  created_at,
            "duration_ms":   dur_ms,
            "duration_fmt":  run_dur_fmt or "—",
            "cost_usd":      None,
            "error_hint":    None,
        }
        if group_label in _groups_by_label:
            _groups_by_label[group_label]["stages"].append(stage_row)
            _groups_by_label[group_label]["stage_count"] += 1
        else:
            _groups_by_label[group_label] = {
                "group_name":        group_label,
                "status":            "completed",
                "stage_count":       1,
                "total_duration_ms": dur_ms,
                "duration_fmt":      run_dur_fmt or "—",
                "total_cost_usd":    0.0,
                "error_message":     None,
                "stages":            [stage_row],
            }
    stage_groups = list(_groups_by_label.values())

    resume_actions = _disk_resume_actions(episode_id, artifacts)

    # Thumbnail label: never say "present" for a rejected thumbnail
    tn_status_label = thumbnail_status_label(thumbnail_pkg, artifacts.get("has_thumbnail", False))

    # Publish checklist for the Publishing tab (disk-only: credential check from DB)
    publish_checklist = _build_disk_publish_checklist(episode_id, ep_state, qa_summary, artifacts, thumbnail_pkg)

    # Runtime decoupling status — read from final_render_package on disk with safe fallbacks.
    _frp_pkg = load_disk_pkg(ep_dir, "final_render_package", episode_id)
    _frp_integrity = _frp_pkg.get("package_integrity", {}) if _frp_pkg else {}
    _main_episode_runtime_status = _frp_integrity.get(
        "main_episode_runtime_status",
        "complete" if artifacts.get("has_render") else "not_planned",
    )
    _shorts_runtime_status = _frp_integrity.get("shorts_runtime_status", "not_planned")

    return {
        "episode_id":       episode_id,
        "display_title":    artifacts.get("display_title") or topic or episode_id,
        "topic":            topic,
        "status":           status,
        "status_label":     status_label,
        "created_at":       created_at,
        "published_at":     None,
        "current_stage":    None,
        "run_id":           None,
        "run_status":       "complete" if artifacts["has_render"] else "partial",
        "run_started_at":   created_at,
        "run_completed_at": created_at,
        "run_error_message": None,
        "run_error_hint":   None,
        "run_duration_fmt": run_dur_fmt,
        "total_cost_usd":   cost_usd,
        "stage_count":      scene_cnt,
        # Runtime decoupling: per-deliverable status for main episode and shorts.
        "main_episode_runtime_status": _main_episode_runtime_status,
        "shorts_runtime_status":       _shorts_runtime_status,
        # Four-way state (production/release/publish/analytics)
        **ep_state,
        "thumbnail_status_label": tn_status_label,
        "platforms":        [],
        "stage_groups":     stage_groups,
        "raw_stages":       [],
        "assets":           [],
        "publishing":       {"summary": {"published": 0, "failed": 0, "pending": 0, "connected": 0},
                             "platform_cards": [],
                             "checklist": publish_checklist},
        "thumbnails":       thumbnails,
        "titles":           disk_title_candidates,
        "analytics":        {"overview": {"has_real_data": False},
                             "platforms": [],
                             "health": {"real_row_count": 0, "mock_row_count": 0,
                                        "missing_platforms": [], "stale_platforms": [],
                                        "has_mock": False}},
        "revenue":          {"summary": {"total_cost_usd": cost_usd, "total_revenue_usd": None,
                                          "profit_usd": None, "roi_pct": None,
                                          "break_even_views": None, "break_even_reason": None,
                                          "has_cost_data": cost_usd is not None,
                                          "has_revenue_data": False},
                             "cost_breakdown": [], "platform_revenue": []},
        "learning":         {"status": "waiting", "records": []},
        "branding":         {"summary": {"assets_found": 0, "assets_missing": 9,
                                          "completeness_pct": 0, "overall_verdict": "Missing"},
                             "thumbnail_audit": {}, "title_audit": {},
                             "platform_audit": [], "asset_inventory": [],
                             "recommendations": []},
        "disk_artifacts":   artifacts,
        "disk_script":      script_sections,
        "disk_qa":          qa_summary,
        "_disk_only":       True,
        **resume_actions,
    }


@app.route("/episode/<episode_id>/artifacts")
def episode_artifacts_page(episode_id):
    """JSON summary of all on-disk artifacts for an episode."""
    artifacts = get_episode_artifacts(episode_id, assets_root=_ASSETS_ROOT_PATH)
    if not artifacts["exists"]:
        return jsonify({"error": "episode artifacts not found"}), 404
    return jsonify(artifacts)


@app.route("/api/sync/episode/<episode_id>", methods=["POST"])
def api_sync_episode(episode_id: str):
    """Sync a single disk episode into the DB so it appears in dashboards."""
    result = sync_disk_episode_to_db(
        episode_id, assets_root=_ASSETS_ROOT_PATH, db_path=_db_path()
    )
    if not result.get("ok"):
        return jsonify(result), 404 if "not found" in (result.get("error") or "") else 500
    return jsonify(result)


@app.route("/api/sync/all", methods=["POST"])
def api_sync_all_episodes():
    """Sync all disk episodes into the DB."""
    results = sync_all_disk_episodes_to_db(
        assets_root=_ASSETS_ROOT_PATH, db_path=_db_path()
    )
    ok_count  = sum(1 for r in results if r.get("ok"))
    err_count = len(results) - ok_count
    return jsonify({"synced": ok_count, "errors": err_count, "results": results})


# ---------------------------------------------------------------------------
# Episode artifact file-serving routes
# All paths are validated via safe_episode_asset_path (traversal guard).
# ---------------------------------------------------------------------------

_ASSETS_ROOT_PATH = _ROOT / ".hypogeum-assets"

_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png",  "webp": "image/webp",
    "mp4": "video/mp4",  "mp3": "audio/mpeg",
    "srt": "text/plain", "json": "application/json",
}


def _send_asset(path: Path | None, download_name: str | None = None, *, inline: bool = False):
    """Send a file or 404.

    inline=True streams for in-browser playback: Range requests are honoured
    (conditional=True) so a <video>/<audio> element can seek, and the file is
    shown inline rather than offered as a download. download_name is still
    used as the suggested filename if the viewer chooses to save.
    """
    from flask import send_file
    if path is None:
        from flask import abort
        abort(404)
    if inline:
        kwargs = {"as_attachment": False, "conditional": True}
        if download_name:
            kwargs["download_name"] = download_name
        return send_file(str(path), **kwargs)
    kwargs = {"as_attachment": bool(download_name)}
    if download_name:
        kwargs["download_name"] = download_name
    return send_file(str(path), **kwargs)


@app.route("/episode/<episode_id>/thumbnail")
def episode_thumbnail(episode_id: str):
    """Serve the main (rank-1) thumbnail image for an episode."""
    for ext in ("jpg", "jpeg", "png", "webp"):
        path = safe_episode_asset_path(
            episode_id, "thumbnail", f"main.{ext}",
            assets_root=_ASSETS_ROOT_PATH,
        )
        if path:
            from flask import send_file
            return send_file(str(path), mimetype=_MIME.get(ext, "image/jpeg"))
    from flask import abort
    abort(404)


@app.route("/episode/<episode_id>/thumbnail/candidate/<int:rank>")
def episode_thumbnail_candidate(episode_id: str, rank: int):
    """Serve a specific thumbnail candidate (candidate_<rank>.jpg)."""
    if rank < 1 or rank > 10:
        from flask import abort
        abort(400)
    for ext in ("jpg", "jpeg", "png", "webp"):
        path = safe_episode_asset_path(
            episode_id, "thumbnail", f"candidate_{rank}.{ext}",
            assets_root=_ASSETS_ROOT_PATH,
        )
        if path:
            from flask import send_file
            return send_file(str(path), mimetype=_MIME.get(ext, "image/jpeg"))
    from flask import abort
    abort(404)


@app.route("/episode/<episode_id>/render")
def episode_render(episode_id: str):
    """Serve the rendered main episode MP4.

    Streams inline for the in-dashboard <video> player by default (Range
    requests honoured so the timeline can be scrubbed). Add ?download=1 to
    get it as a file instead.
    """
    path = safe_episode_asset_path(
        episode_id, "artifact", f"main_{episode_id}.mp4",
        assets_root=_ASSETS_ROOT_PATH,
    )
    if path is None:
        # main MP4 sits directly in episode dir, not in a subdirectory
        candidate = (_ASSETS_ROOT_PATH / "episodes" / episode_id / f"main_{episode_id}.mp4").resolve()
        try:
            candidate.relative_to((_ASSETS_ROOT_PATH / "episodes" / episode_id).resolve())
        except ValueError:
            from flask import abort
            abort(404)
        if candidate.exists() and candidate.stat().st_size > 0:
            path = candidate
    _download = request.args.get("download", "0") == "1"
    return _send_asset(path, download_name=f"{episode_id}_main.mp4", inline=not _download)


@app.route("/episode/<episode_id>/narration")
def episode_narration(episode_id: str):
    """Serve the stitched main narration MP3."""
    path = safe_episode_asset_path(
        episode_id, "narration", "main_narration.mp3",
        assets_root=_ASSETS_ROOT_PATH,
    )
    return _send_asset(path, download_name=f"{episode_id}_narration.mp3")


@app.route("/episode/<episode_id>/captions")
def episode_captions(episode_id: str):
    """Serve the main SRT captions file."""
    path = safe_episode_asset_path(
        episode_id, "captions", "main.srt",
        assets_root=_ASSETS_ROOT_PATH,
    )
    return _send_asset(path, download_name=f"{episode_id}_captions.srt")


@app.route("/episode/<episode_id>/clip/<scene_id>")
def episode_clip(episode_id: str, scene_id: str):
    """Serve a specific video clip by scene_id (e.g. sc_s01)."""
    filename = f"{scene_id}.mp4"
    path = safe_episode_asset_path(
        episode_id, "video", filename,
        assets_root=_ASSETS_ROOT_PATH,
    )
    return _send_asset(path, download_name=filename)


@app.route("/episode/<episode_id>/artifact/<name>")
def episode_artifact_json(episode_id: str, name: str):
    """Serve a JSON package artifact (e.g. 'qa_approval_report')."""
    # name may be the stem only; try both forms
    for filename in (f"{name}_{episode_id}.json", f"{name}.json"):
        path = safe_episode_asset_path(
            episode_id, "artifact", filename,
            assets_root=_ASSETS_ROOT_PATH,
        )
        if path:
            from flask import send_file
            return send_file(str(path), mimetype="application/json")
    from flask import abort
    abort(404)


# ---------------------------------------------------------------------------
# Resume API endpoints
# These spawn the rebuild/resume scripts in a background subprocess so the
# HTTP request returns immediately with {"ok": true, "status": "started"}.
# ---------------------------------------------------------------------------

import subprocess as _subprocess


@app.route("/api/episode/<episode_id>/resume/assembly", methods=["POST"])
def api_episode_resume_assembly(episode_id: str):
    """
    Trigger an assembly+QA rebuild for an episode using existing artifacts.
    Runs rebuild_ep003_assembly_qa.py (or the generic equivalent) as a
    background subprocess.  Returns immediately — callers poll /run/<run_id>
    or refresh the episode page to see updated results.
    """
    artifacts = get_episode_artifacts(episode_id, assets_root=_ASSETS_ROOT_PATH)
    if not artifacts["exists"]:
        return jsonify({"ok": False, "error": "episode not found on disk"}), 404

    script = _ROOT / "scripts" / "rebuild_ep003_assembly_qa.py"
    if not script.exists():
        return jsonify({"ok": False, "error": "rebuild script not found"}), 503

    try:
        import sys as _sys
        proc = _subprocess.Popen(
            [_sys.executable, str(script)],
            cwd=str(_ROOT),
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
        )
        return jsonify({"ok": True, "status": "started", "pid": proc.pid,
                        "episode_id": episode_id})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/episode/<episode_id>/resume/narration", methods=["POST"])
def api_episode_resume_narration(episode_id: str):
    """
    Trigger a narration-only resume for an episode.
    Requires ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID in the environment.
    Spawns resume_narration_ep003.py as a background subprocess.
    """
    artifacts = get_episode_artifacts(episode_id, assets_root=_ASSETS_ROOT_PATH)
    if not artifacts["exists"]:
        return jsonify({"ok": False, "error": "episode not found on disk"}), 404

    # Preflight check — keys must be present
    if not os.environ.get("ELEVENLABS_API_KEY"):
        return jsonify({"ok": False, "error": "ELEVENLABS_API_KEY not set"}), 400
    if not os.environ.get("ELEVENLABS_VOICE_ID"):
        return jsonify({"ok": False, "error": "ELEVENLABS_VOICE_ID not set"}), 400

    script = _ROOT / "scripts" / "resume_narration_ep003.py"
    if not script.exists():
        return jsonify({"ok": False, "error": "resume script not found"}), 503

    try:
        import sys as _sys
        proc = _subprocess.Popen(
            [_sys.executable, str(script)],
            cwd=str(_ROOT),
            env={**os.environ},
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
        )
        return jsonify({"ok": True, "status": "started", "pid": proc.pid,
                        "episode_id": episode_id})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


_TEST_EP_PATTERNS = (
    "ep_test", "ep_smoke", "ep_review", "ep_val_", "ep_xyz", "_lite",
    "ep_cli_", "ep_pub_", "ep_unknown",
)


def _is_test_episode(episode_id: str) -> bool:
    """
    Return True when episode_id looks like a test/validation/fixture, not a
    real production episode. Real production episode ids are `ep_<8 hex>`
    (uuid4-derived) or named slots like `ep_twinkle_sep10` — those are NOT
    test. Fixtures are named (ep_test*, ep_cli_*, ep_pub_*, …) or are bare
    UUIDs with no `ep_` prefix at all (left on disk by the publishing test
    suites).
    """
    import re
    eid = (episode_id or "").lower()
    for pat in _TEST_EP_PATTERNS:
        if pat in eid:
            return True
    # bare UUID / hex blob with no ep_ prefix — never a production episode
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-", eid):
        return True
    if re.match(r"^[0-9a-f]{16,}$", eid):
        return True
    return False


@app.route("/episodes")
def episodes_page():
    channel_id = request.args.get("channel_id") or _scope_channel()
    show_test  = request.args.get("show_test", "0").strip() == "1"

    episodes   = list_episodes(db_path=_db_path(), channel_id=channel_id or None)

    # Include disk-only episodes not yet in DB so the history count is accurate
    try:
        db_ep_ids  = {e["episode_id"] for e in episodes}
        disk_eps   = list_disk_episodes(assets_root=_ASSETS_ROOT_PATH, limit=50)
        for dep in disk_eps:
            ep_id = dep["episode_id"]
            if ep_id in db_ep_ids:
                continue
            ep_dir = _ASSETS_ROOT_PATH / "episodes" / ep_id
            qa_pkg        = load_disk_pkg(ep_dir, "qa_approval_report", ep_id)
            thumbnail_pkg = load_disk_pkg(ep_dir, "thumbnail_package", ep_id)
            bga_pkgs = []
            for pn in (1, 2):
                pkg = load_disk_pkg(ep_dir, f"bga_compliance_report_p{pn}", ep_id)
                if pkg:
                    bga_pkgs.append(pkg)
            artifacts = {
                "has_render":    dep.get("has_render", False),
                "has_thumbnail": dep.get("has_thumbnail", False),
                "has_narration": False,
            }
            status = infer_episode_status(qa_pkg, bga_pkgs, artifacts, thumbnail_pkg=thumbnail_pkg)
            title  = dep.get("title") or episode_title_from_disk(ep_dir, ep_id)
            episodes.append({
                "episode_id":    ep_id,
                "title":         title,
                "topic":         title,
                "status":        status,
                "run_status":    "complete" if dep.get("has_render") else "partial",
                "total_cost_usd": None,
                "created_at":    None,
                "run_id":        None,
                "_disk_only":    True,
                "qa_verdict":    dep.get("qa_verdict"),
                "has_render":    dep.get("has_render"),
            })
    except Exception:
        pass

    # Tag and filter test episodes
    for ep in episodes:
        ep["_is_test"] = _is_test_episode(ep["episode_id"])

    test_count   = sum(1 for ep in episodes if ep["_is_test"])
    if not show_test:
        episodes = [ep for ep in episodes if not ep["_is_test"]]

    # Production queue: queued + claimed, ordered by priority then date
    queue_topics  = list_episode_topics(
        channel_id=channel_id or None, status="queued", db_path=_db_path()
    )
    claimed_topics = list_episode_topics(
        channel_id=channel_id or None, status="claimed", db_path=_db_path()
    )
    from hypogeum.dashboard_api import get_channel_summaries
    channels   = get_channel_summaries(db_path=_db_path())
    preflight  = get_preflight_status(db_path=_db_path())
    try:
        pulse = get_factory_pulse(db_path=_db_path())
    except Exception:
        pulse = None
    return render_template("episodes.html",
                           episodes=episodes,
                           queue_topics=queue_topics,
                           claimed_topics=claimed_topics,
                           channels=channels, preflight=preflight,
                           pulse=pulse,
                           channel_filter=channel_id,
                           show_test=show_test,
                           test_count=test_count)


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


@app.route("/api/factory/pulse", methods=["GET"])
def api_factory_pulse():
    """What the factory is doing right now — worker liveness, current
    activity, in-flight runs, recent discovery, queue. Polled by the
    Factory Pulse strip. Read-only."""
    return jsonify(get_factory_pulse(db_path=_db_path()))


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
    runtime = recommendations = None
    try:
        from hypogeum.channel_runtime_status import get_channel_runtime_status
        runtime = get_channel_runtime_status(channel_id, db_path=db)
    except Exception:
        runtime = None
    try:
        from hypogeum.learning_engine import get_channel_recommendations
        recommendations = get_channel_recommendations(channel_id, db_path=db)
    except Exception:
        recommendations = {}
    try:
        from hypogeum.channel_readiness import get_channel_readiness
        readiness = get_channel_readiness(channel_id, db_path=db)
    except Exception:
        readiness = None
    try:
        from hypogeum.dashboard_rollup import get_channel_rollup
        rollup = get_channel_rollup(channel_id, db_path=db)
    except Exception:  # noqa: BLE001
        rollup = None
    try:
        recent_runs = list_runs(db_path=db, channel_id=channel_id, limit=6)
    except Exception:  # noqa: BLE001
        recent_runs = []
    return render_template("channel_detail.html",
                           channel=channel, brand=brand, model=model, publishing=publishing,
                           runtime=runtime, recommendations=recommendations, readiness=readiness,
                           rollup=rollup, recent_runs=recent_runs)


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


# ── Channel operations / production plan edit ──────────────────────────────

@app.route("/channel/<channel_id>/operations", methods=["GET", "POST"])
def channel_operations_edit(channel_id):
    """Edit the channel's PRODUCTION PLAN from the dashboard: cadence
    (shorts/week, videos/week), weekly video budget, per-episode cost
    ceiling, the autonomous on/off switches, timezone and publish slot.

    On save it also keeps the channel's daily `channel`-scoped schedule in
    sync with the cadence (a shorts-only channel gets exactly one daily
    schedule; switching mix rebuilds it) — the same effect
    tools/configure_hypogeum_shorts_only.py has, done from the UI.
    """
    from datetime import datetime, time, timedelta, timezone as _tz
    from hypogeum.channel_config import (
        get_channel_operations_profile, update_channel_operations_profile,
    )
    from hypogeum.scheduler import create_schedule, set_schedule_status
    from hypogeum.db import get_connection

    db = _db_path()
    prof = get_channel_operations_profile(channel_id, db_path=db)
    if prof is None:
        return render_template("not_found.html", kind="Channel operations profile", id=channel_id), 404

    if request.method == "POST":
        f = request.form
        errors, notes = [], []

        def _int(name, default):
            try:
                return max(0, int(f.get(name, default)))
            except (TypeError, ValueError):
                errors.append(f"{name} must be a whole number")
                return default

        spw = _int("shorts_per_week", (prof.production_cadence or {}).get("shorts_per_week", 0))
        vpw = _int("videos_per_week", (prof.production_cadence or {}).get("videos_per_week", 0))
        if spw == 0 and vpw == 0:
            errors.append("at least one of shorts/week or videos/week must be > 0")

        updates = {
            "production_cadence": {"videos_per_week": vpw, "shorts_per_week": spw},
        }

        try:
            wk = float(f.get("weekly_video_budget_usd", prof.weekly_video_budget_usd or 0))
            if wk <= 0:
                errors.append("weekly video budget must be > 0")
            else:
                updates["weekly_video_budget_usd"] = wk
        except (TypeError, ValueError):
            errors.append("weekly video budget must be a number")

        _cc = (f.get("cost_ceiling_usd_per_episode") or "").strip()
        if _cc == "":
            updates["cost_ceiling_usd_per_episode"] = None
        else:
            try:
                updates["cost_ceiling_usd_per_episode"] = float(_cc)
            except ValueError:
                errors.append("per-episode cost ceiling must be a number or blank")

        tzname = (f.get("timezone") or prof.timezone or "UTC").strip()
        updates["timezone"] = tzname

        pub_day = (f.get("publish_day") or (prof.publishing_schedule or {}).get("day_of_week") or "thursday").strip().lower()
        pub_time = (f.get("publish_time") or (prof.publishing_schedule or {}).get("time_of_day") or "18:00").strip()
        updates["publishing_schedule"] = {"day_of_week": pub_day, "time_of_day": pub_time}

        for flag in ("operator_enabled", "unattended_operation_enabled", "discovery_enabled",
                     "production_enabled", "publishing_enabled", "analytics_sync_enabled"):
            updates[flag] = flag in f

        # shorts-only channel -> a single `short` deliverable bundle sized to
        # the weekly count, and NO weekly bundle-cycle / per-deliverable
        # schedule policy: each produced Short is published immediately
        # (run_worker._process_channel_publishing passes publish_now=True for
        # a videos_per_week==0 channel). A leftover hypogeum_weekly_release_v1
        # bundle_cycle_policy would otherwise block every Short as a "partial
        # bundle" (it demands a main_episode). A mixed channel keeps its
        # existing bundle defs and cycle policy untouched.
        shorts_only = (vpw == 0 and spw >= 1)
        if shorts_only:
            updates["deliverable_bundle_definitions"] = [
                {"bundle_id": "short", "target_buffer_count": spw}
            ]
            updates["bundle_cycle_policy"] = {}
            updates["deliverable_schedule_policy"] = {}

        if errors:
            return render_template("channel_operations_edit.html", channel_id=channel_id,
                                   prof=prof, errors=errors, form=f), 400

        try:
            update_channel_operations_profile(channel_id, db_path=db, **updates)
        except (ValueError, RuntimeError) as exc:
            return render_template("channel_operations_edit.html", channel_id=channel_id,
                                   prof=prof, errors=[str(exc)], form=f), 400

        # keep the channel-scoped daily schedule in sync with the cadence --
        # REUSE the existing schedule row whose label matches the current
        # cadence instead of creating a fresh one every save (that left a
        # trail of disabled duplicates). Every other channel schedule is
        # disabled.
        try:
            from hypogeum.scheduler import set_next_run
            now = datetime.now(_tz.utc)
            nxt = datetime.combine(now.date(), time(14, 0), tzinfo=_tz.utc)
            if nxt <= now:
                nxt += timedelta(days=1)
            cadence = "daily" if shorts_only else "weekly"
            label = f"{channel_id}_{'daily_shorts' if shorts_only else 'weekly'}"

            conn = get_connection(db)
            try:
                existing = conn.execute(
                    "SELECT schedule_id, status, label FROM schedules "
                    "WHERE scope='channel' AND channel_id=? ORDER BY created_at DESC",
                    (channel_id,),
                ).fetchall()
            finally:
                conn.close()

            reuse_id = next((r["schedule_id"] for r in existing if r["label"] == label), None)
            for r in existing:
                if r["schedule_id"] == reuse_id:
                    continue
                if r["status"] != "disabled":
                    set_schedule_status(r["schedule_id"], "disabled", db_path=db)

            if reuse_id:
                set_schedule_status(reuse_id, "active", db_path=db)
                set_next_run(reuse_id, nxt, db_path=db)
                notes.append(f"schedule kept: {cadence} (next {nxt:%Y-%m-%d %H:%M} UTC)")
            else:
                create_schedule(
                    scope="channel", channel_id=channel_id, cadence=cadence,
                    next_run_at=nxt, max_runs_per_day=1, label=label, db_path=db,
                )
                notes.append(f"schedule created: {cadence} (next {nxt:%Y-%m-%d %H:%M} UTC)")
        except Exception as exc:  # noqa: BLE001 -- schedule sync is best-effort
            notes.append(f"schedule not synced automatically: {exc}")

        # surface the operating-grant implication of a budget change
        try:
            from hypogeum.channel_runtime_status import get_channel_runtime_status
            rt = get_channel_runtime_status(channel_id, db_path=db)
            og = rt.get("operating_grant") or {}
            if og.get("active") and og.get("ceiling_matches_config") is False:
                notes.append("weekly budget changed — the Channel Operating Grant's ceiling "
                             "no longer matches; re-sign it (tools/configure_channel_operating_grant.py)")
        except Exception:  # noqa: BLE001
            pass

        _flash = "; ".join(notes) if notes else "saved"
        return redirect(url_for("channel_detail", channel_id=channel_id) + f"?ops_saved={_flash}")

    return render_template("channel_operations_edit.html", channel_id=channel_id,
                           prof=prof, errors=[], form={})


# ── Per-channel Publishing (platform connections) + Settings ───────────────

@app.route("/channel/<channel_id>/publishing", methods=["GET", "POST"])
def channel_publishing_page(channel_id):
    """Connect & bind platform accounts for THIS channel."""
    db = _db_path()
    channel = get_channel_detail(channel_id, db_path=db)
    if channel is None:
        return render_template("not_found.html", kind="Channel", id=channel_id), 404

    from hypogeum.publishing import provider_repository as _pr

    drafted_profiles = None
    if request.method == "POST" and request.form.get("action") == "draft_profiles":
        # Render inline (not a redirect) so the generated copy survives.
        brand = (get_brand_detail(channel["brand_profile_id"], db_path=db)
                 if channel.get("brand_profile_id") else None)
        try:
            drafted_profiles = _draft_platform_profiles(
                name=channel.get("name", ""),
                handle="",
                persona=(getattr(brand, "persona_name", "") or getattr(brand, "tone", "")) if brand else "",
                genre=(getattr(brand, "target_audience", "") or "") if brand else "",
                concept=(getattr(brand, "voice_guidelines", "") or "") if brand else "",
                language=(getattr(brand, "language", "en") or "en") if brand else "en",
            )
        except Exception as exc:  # noqa: BLE001
            drafted_profiles = {"_error": str(exc)}

    if request.method == "POST" and request.form.get("action") != "draft_profiles":
        action   = request.form.get("action")
        platform = request.form.get("platform", "")
        flash = "err"
        try:
            if action == "sync":
                from hypogeum.publishing.zernio_accounts import sync_zernio_accounts
                res = sync_zernio_accounts(db)
                flash = ("synced:%d" % res.get("synced", 0)) if res.get("ok") \
                    else "syncerr:%s" % (res.get("error") or "Zernio not configured")
            elif action == "bind":
                acct_id = request.form.get("provider_account_id", "")
                acct = _pr.get_provider_account(db, provider_account_id=acct_id)
                if acct:
                    # a pre-existing enabled binding for this platform must be
                    # cleared first (the repo refuses a silent swap)
                    cur = _pr.resolve_active_binding(
                        db, channel_id=channel_id, capability="publishing", platform=platform)
                    if cur and cur.get("provider_account_id") != acct_id:
                        _pr.disable_binding(db, binding_id=cur["binding_id"])
                    for cap in ("publishing", "analytics"):
                        _pr.bind_channel_provider(
                            db, channel_id=channel_id, capability=cap, platform=platform,
                            provider_id=acct["provider_id"],
                            provider_profile_id=acct.get("provider_profile_id"),
                            provider_account_id=acct_id)
                    flash = "bound"
            elif action == "unbind":
                for cap in ("publishing", "analytics"):
                    b = _pr.resolve_active_binding(
                        db, channel_id=channel_id, capability=cap, platform=platform)
                    if b:
                        _pr.disable_binding(db, binding_id=b["binding_id"])
                flash = "unbound"
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("channel publishing bind error: %s", exc)
            flash = "err"
        return redirect(
            url_for("channel_publishing_page", channel_id=channel_id,
                    scope=channel_id) + f"&flash={flash}")

    publishing = (get_publishing_detail(channel["publishing_profile_id"], db_path=db)
                  if channel.get("publishing_profile_id") else None)
    wanted = list((getattr(publishing, "enabled_platforms", None) or
                   (publishing.get("enabled_platforms", []) if isinstance(publishing, dict) else []))
                  ) if publishing else []
    if not wanted:
        try:
            from hypogeum.channel_config import get_channel_operations_profile
            _ops = get_channel_operations_profile(channel_id, db_path=db)
            wanted = [p for p, on in (getattr(_ops, "enabled_platforms", {}) or {}).items() if on]
        except Exception:  # noqa: BLE001
            wanted = []

    bound = {}
    try:
        for b in _pr.list_bindings(db, channel_id=channel_id):
            if b.get("enabled") and b.get("capability") == "publishing":
                bound[b["platform"]] = b
    except Exception:  # noqa: BLE001
        pass

    zernio_pool = {}
    try:
        from hypogeum.publishing.zernio_accounts import get_zernio_accounts_snapshot
        zernio_pool = (get_zernio_accounts_snapshot(db) or {}).get("by_platform", {})
    except Exception:  # noqa: BLE001
        zernio_pool = {}

    # candidate accounts per platform from provider_accounts
    accounts_by_platform = {}
    try:
        from hypogeum.db import get_connection as _gc
        _c = _gc(db)
        try:
            for row in _c.execute(
                "SELECT provider_account_id, provider_id, platform, display_label, "
                "external_account_id, status FROM provider_accounts ORDER BY platform"):
                accounts_by_platform.setdefault(row["platform"], []).append(dict(row))
        finally:
            _c.close()
    except Exception:  # noqa: BLE001
        pass

    return render_template("channel_publishing.html",
                           channel=channel, wanted_platforms=wanted or list(zernio_pool.keys()),
                           bound=bound, zernio_pool=zernio_pool,
                           accounts_by_platform=accounts_by_platform,
                           drafted_profiles=drafted_profiles,
                           flash=request.args.get("flash"))


@app.route("/channel/<channel_id>/settings")
def channel_settings_page(channel_id):
    """One place for this channel's brand / models / publishing / production
    plan. Phase 1: a hub linking to the existing edit pages."""
    db = _db_path()
    channel = get_channel_detail(channel_id, db_path=db)
    if channel is None:
        return render_template("not_found.html", kind="Channel", id=channel_id), 404
    brand      = get_brand_detail(channel["brand_profile_id"], db_path=db)      if channel.get("brand_profile_id")      else None
    model      = get_model_detail(channel["model_profile_id"], db_path=db)      if channel.get("model_profile_id")      else None
    publishing = get_publishing_detail(channel["publishing_profile_id"], db_path=db) if channel.get("publishing_profile_id") else None
    try:
        from hypogeum.channel_config import get_channel_operations_profile
        ops = get_channel_operations_profile(channel_id, db_path=db)
    except Exception:  # noqa: BLE001
        ops = None
    grant = {}
    try:
        from hypogeum.channel_runtime_status import get_channel_runtime_status
        grant = (get_channel_runtime_status(channel_id, db_path=db) or {}).get("operating_grant") or {}
    except Exception:  # noqa: BLE001
        grant = {}
    try:
        from hypogeum.grant_signing import can_sign_from_dashboard
        grant_dashboard_signing = can_sign_from_dashboard()
    except Exception:  # noqa: BLE001
        grant_dashboard_signing = False

    # Keyword / competitor research (SEO discovery intelligence)
    keyword_research = None
    keyword_research_stale = True
    try:
        from hypogeum import channel_keyword_research as _ckr
        keyword_research = _ckr.get_keyword_research(channel_id, db_path=db)
        keyword_research_stale = _ckr.is_stale(keyword_research)
    except Exception:  # noqa: BLE001
        pass
    keyword_research_status = _KEYWORD_RESEARCH_STATUS.get(channel_id, {})

    return render_template("channel_settings.html",
                           channel=channel, brand=brand, model=model,
                           publishing=publishing, ops=ops, grant=grant,
                           grant_dashboard_signing=grant_dashboard_signing,
                           keyword_research=keyword_research,
                           keyword_research_stale=keyword_research_stale,
                           keyword_research_status=keyword_research_status)


# In-memory status for the per-channel keyword-research refresh (best-effort;
# the authoritative record is the channel_keyword_research table).
_KEYWORD_RESEARCH_STATUS: dict = {}


@app.route("/channel/<channel_id>/keyword-research/refresh", methods=["POST"])
def channel_keyword_research_refresh(channel_id):
    """Kick off a background competitor + trending keyword research run for
    this channel. ~3 Perplexity queries + 1 structuring call, ~$0.03, cached
    ~14 days — consumed by the metadata agents on the next episode."""
    import threading
    from datetime import datetime, timezone
    db = _db_path()
    channel = get_channel_detail(channel_id, db_path=db)
    if channel is None:
        return render_template("not_found.html", kind="Channel", id=channel_id), 404

    if _KEYWORD_RESEARCH_STATUS.get(channel_id, {}).get("state") == "running":
        return redirect(url_for("channel_settings_page", channel_id=channel_id,
                                scope=channel_id, kw="running"))

    _KEYWORD_RESEARCH_STATUS[channel_id] = {
        "state": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    def _run():
        from hypogeum import channel_keyword_research as _ckr
        try:
            out = _ckr.refresh_keyword_research(channel_id, db_path=db, force=True)
            ok = bool(out) and not out.get("error")
            _KEYWORD_RESEARCH_STATUS[channel_id] = {
                "state": "done" if ok else "error",
                "error": out.get("error") if isinstance(out, dict) else None,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as exc:  # noqa: BLE001
            _KEYWORD_RESEARCH_STATUS[channel_id] = {
                "state": "error", "error": str(exc),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }

    threading.Thread(target=_run, daemon=True).start()
    return redirect(url_for("channel_settings_page", channel_id=channel_id,
                            scope=channel_id, kw="started"))


@app.route("/channel/<channel_id>/operating-grant", methods=["GET", "POST"])
def channel_operating_grant_page(channel_id):
    """Sign / re-sign this channel's ADR-016 Operating Grant from the
    dashboard. The signing itself is a short-lived subprocess that reads
    the offline key and exits — see hypogeum.grant_signing."""
    db = _db_path()
    channel = get_channel_detail(channel_id, db_path=db)
    if channel is None:
        return render_template("not_found.html", kind="Channel", id=channel_id), 404

    from hypogeum.grant_signing import (
        can_sign_from_dashboard, preview_channel_operating_grant,
        sign_channel_operating_grant,
    )
    from hypogeum.channel_package_scaffold import channel_package_exists, scaffold_channel_package

    if request.method == "POST":
        if not can_sign_from_dashboard():
            return redirect(url_for("channel_operating_grant_page",
                                    channel_id=channel_id, scope=channel_id))
        # confirm friction: the operator must type the weekly ceiling
        try:
            from hypogeum.channel_budget_policy import resolve_configured_weekly_limit
            from hypogeum.db import get_connection as _gc
            _c = _gc(db)
            try:
                _tz, weekly_limit, _ref = resolve_configured_weekly_limit(channel_id, conn=_c)
            finally:
                _c.close()
        except Exception:  # noqa: BLE001
            weekly_limit = None
        typed = (request.form.get("confirm_ceiling") or "").strip().lstrip("$").replace(",", "")
        try:
            ok_amount = weekly_limit is not None and abs(float(typed) - float(weekly_limit)) < 0.005
        except ValueError:
            ok_amount = False
        if not ok_amount:
            return render_template("channel_operating_grant.html",
                                   channel=channel, can_sign=True,
                                   preview=preview_channel_operating_grant(channel_id, db),
                                   grant=_grant_status(channel_id, db),
                                   error="the amount you typed doesn't match this "
                                         "channel's weekly budget — nothing signed"), 422

        if db is None and not channel_package_exists(channel_id):
            scaffold_channel_package(channel_id, db_path=db)
        res = sign_channel_operating_grant(
            channel_id,
            approver_identity=f"dashboard:{request.remote_addr or 'local'}",
        )
        try:
            _audit_note(f"operating_grant_signed channel={channel_id} "
                        f"ok={res['ok']} grant={res.get('grant_id')}")
        except Exception:  # noqa: BLE001
            pass
        return redirect(url_for("channel_detail", channel_id=channel_id)
                        + f"?scope={channel_id}&grant="
                        + ("signed" if res["ok"] else "failed"))

    return render_template("channel_operating_grant.html",
                           channel=channel,
                           can_sign=can_sign_from_dashboard(),
                           preview=preview_channel_operating_grant(channel_id, db),
                           grant=_grant_status(channel_id, db),
                           error=None)


def _grant_status(channel_id, db):
    try:
        from hypogeum.channel_runtime_status import get_channel_runtime_status
        return (get_channel_runtime_status(channel_id, db_path=db) or {}).get("operating_grant") or {}
    except Exception:  # noqa: BLE001
        return {}


def _audit_note(msg: str):
    app.logger.info("AUDIT %s", msg)


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
    # Per-channel image-to-video model (entity-conditioned scenes). The T2V
    # model is adopted via the bake-off; the I2V model is set here directly
    # (e.g. runway-image-to-video @ $0.15 instead of the global
    # veo3-image-to-video @ $2.50). Price/duration let planning cost a model
    # that is off muapi's static tables.
    "video_i2v_model", "video_i2v_model_price_usd", "video_i2v_model_duration_seconds",
})

_MODEL_NUMERIC_FIELDS = frozenset({
    "video_i2v_model_price_usd", "video_i2v_model_duration_seconds",
})

@app.route("/model/<model_profile_id>/edit", methods=["GET", "POST"])
def model_edit(model_profile_id):
    db = _db_path()
    if request.method == "POST":
        updates = {}
        for k, v in request.form.items():
            if k not in _MODEL_ALLOWED:
                continue
            if k in _MODEL_NUMERIC_FIELDS:
                v = v.strip()
                try:
                    updates[k] = float(v) if v else None
                except ValueError:
                    updates[k] = None
            else:
                updates[k] = (v.strip() or None) if k.startswith("video_i2v_model") else v
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
    ch = request.args.get("channel_id") or _scope_channel()
    return jsonify(get_analytics_overview(db_path=_db_path(), channel_id=ch or None))


@app.route("/api/analytics/platform/<platform>", methods=["GET"])
def api_analytics_platform(platform):
    if platform not in _VALID_PLATFORMS:
        return jsonify({"error": f"unknown platform {platform!r}"}), 400
    limit = min(int(request.args.get("limit", 50)), 200)
    ch = request.args.get("channel_id") or _scope_channel()
    return jsonify(get_platform_summary(platform, db_path=_db_path(), limit=limit,
                                        channel_id=ch or None))


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
# Intelligence namespace — primary handlers
# Old /analytics/* and /learning routes redirect here.
# ===========================================================================

def _render_analytics_overview():
    """Shared logic for /intelligence/performance. Scopes to the active
    channel when the left rail is on a channel."""
    ch = request.args.get("channel_id") or _scope_channel()
    overview = get_analytics_overview(db_path=_db_path(), channel_id=ch or None)
    from hypogeum.dashboard_api import get_channel_summaries
    channels = get_channel_summaries(db_path=_db_path())
    youtube_connected = False
    try:
        from hypogeum.config import Config as _Cfg
        _cfg = _Cfg()
        youtube_connected = bool(
            _cfg.YOUTUBE_REFRESH_TOKEN
            and _cfg.YOUTUBE_CLIENT_ID
            and _cfg.YOUTUBE_CLIENT_SECRET
        )
    except Exception:
        pass
    return render_template("analytics.html",
                           overview=overview, channels=channels,
                           valid_platforms=sorted(_VALID_PLATFORMS),
                           youtube_connected=youtube_connected)


@app.route("/intelligence")
def intelligence_redirect():
    return redirect("/intelligence/performance", 302)


@app.route("/intelligence/performance")
def intelligence_performance_page():
    return _render_analytics_overview()


@app.route("/intelligence/platform/<platform>")
def intelligence_platform_page(platform):
    if platform not in _VALID_PLATFORMS:
        return render_template("not_found.html", kind="Platform", id=platform), 404
    ch = request.args.get("channel_id") or _scope_channel()
    summary = get_platform_summary(platform, db_path=_db_path(), channel_id=ch or None)
    return render_template("analytics_platform.html",
                           summary=summary, platform=platform)


@app.route("/intelligence/performance/episode/<episode_id>")
def intelligence_episode_page(episode_id):
    data = get_episode_performance(episode_id, db_path=_db_path())
    if data is None:
        return render_template("not_found.html",
                               kind="Episode analytics", id=episode_id), 404
    return render_template("analytics_episode.html", d=data)


@app.route("/intelligence/learning")
def intelligence_learning_page():
    state = get_learning_state(db_path=_db_path())
    # "What Cortex changed and why" — applied learning directives per channel.
    changelog = []
    try:
        from hypogeum.channel_learning import list_channel_changelog
        for c in _nav_channels():
            for d in list_channel_changelog(c["channel_id"], limit=25, db_path=_db_path()):
                d["channel_name"] = c["name"]
                changelog.append(d)
        changelog.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    except Exception:  # noqa: BLE001
        changelog = []
    return render_template("learning.html", state=state, changelog=changelog)


@app.route("/intelligence/sync")
def intelligence_sync_page():
    from hypogeum.platform_sync import get_sync_log, get_last_sync_by_platform
    _sync_ch = request.args.get("channel_id") or _scope_channel()
    try:
        sync_log = get_sync_log(limit=30, db_path=_db_path(), channel_id=_sync_ch or None)
    except Exception:
        sync_log = []
    try:
        last_sync_by_platform = get_last_sync_by_platform(db_path=_db_path(),
                                                          channel_id=_sync_ch or None)
    except Exception:
        last_sync_by_platform = {}

    # Channel scope: drive the cards from this channel's Zernio bindings.
    channel_bindings = {}
    if _sync_ch:
        try:
            from hypogeum.publishing.provider_repository import resolve_active_binding
            from hypogeum.db import get_connection as _gc
            _acc = {}
            _c = _gc(_db_path())
            try:
                for row in _c.execute(
                    "SELECT provider_account_id, display_label, external_account_id, status "
                    "FROM provider_accounts"):
                    _acc[row["provider_account_id"]] = dict(row)
            finally:
                _c.close()
            for p in ("youtube", "tiktok", "instagram", "facebook"):
                b = resolve_active_binding(_db_path(), channel_id=_sync_ch,
                                           capability="publishing", platform=p)
                channel_bindings[p] = {
                    "bound": bool(b),
                    "account": _acc.get(b["provider_account_id"]) if b else None,
                }
        except Exception:  # noqa: BLE001
            channel_bindings = {}

    # Factory scope: aggregate the Zernio account pool + which channels bind each.
    factory_accounts = [] if _sync_ch else _factory_provider_accounts()

    flash = request.args.get("flash")
    return render_template(
        "analytics_sync.html",
        sync_log=sync_log,
        last_sync_by_platform=last_sync_by_platform,
        channel_bindings=channel_bindings,
        factory_accounts=factory_accounts,
        flash=flash,
    )


# ---------------------------------------------------------------------------
# Old analytics/learning routes — 302 to Intelligence namespace
# /analytics/import and /analytics/advanced/import are NOT redirected.
# ---------------------------------------------------------------------------

@app.route("/analytics")
def analytics_page():
    return redirect("/intelligence/performance", 302)


@app.route("/analytics/platform/<platform>")
def analytics_platform_page(platform):
    return redirect(f"/intelligence/platform/{platform}", 302)


@app.route("/analytics/episode/<episode_id>")
def analytics_episode_page(episode_id):
    return redirect(f"/intelligence/performance/episode/{episode_id}", 302)


@app.route("/analytics/import", methods=["GET"])
def analytics_import_redirect():
    return redirect(url_for("analytics_import_page"), 301)


@app.route("/analytics/advanced/import", methods=["GET"])
def analytics_import_page():
    from hypogeum.dashboard_api import get_channel_summaries
    from hypogeum.dashboard_runs import list_episodes
    from hypogeum.youtube_analytics import get_last_youtube_sync
    channels = get_channel_summaries(db_path=_db_path())
    episodes = list_episodes(db_path=_db_path(), limit=50)
    overview = get_analytics_overview(db_path=_db_path())
    flash    = request.args.get("import_result")
    # YouTube connection state — token present in config or env
    youtube_connected = False
    try:
        from hypogeum.config import Config as _Cfg
        _cfg = _Cfg()
        youtube_connected = bool(
            _cfg.YOUTUBE_REFRESH_TOKEN
            and _cfg.YOUTUBE_CLIENT_ID
            and _cfg.YOUTUBE_CLIENT_SECRET
        )
    except Exception:
        pass
    youtube_last_synced = get_last_youtube_sync(db_path=_db_path())
    return render_template("analytics_import.html",
                           channels=channels, episodes=episodes,
                           valid_platforms=sorted(_VALID_PLATFORMS),
                           flash=flash, overview=overview,
                           youtube_connected=youtube_connected,
                           youtube_last_synced=youtube_last_synced)


@app.route("/api/analytics/sync/youtube", methods=["POST"])
def api_analytics_sync_youtube():
    """
    Refresh YouTube OAuth token and pull real analytics for all published videos.
    Stores results in platform_analytics with source='api'.
    Returns {ok, synced_count, skipped_count, errors, synced_at, episodes}.
    """
    from hypogeum.youtube_analytics import sync_youtube_analytics
    from hypogeum.config import Config as _Cfg
    try:
        cfg = _Cfg()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Config error: {exc}"}), 500

    if not (cfg.YOUTUBE_REFRESH_TOKEN
            and cfg.YOUTUBE_CLIENT_ID
            and cfg.YOUTUBE_CLIENT_SECRET):
        return jsonify({
            "ok": False,
            "error": "YouTube not connected — YOUTUBE_REFRESH_TOKEN, "
                     "YOUTUBE_CLIENT_ID, and YOUTUBE_CLIENT_SECRET are required.",
        }), 400

    window_days = 900  # default: full historical sync (~2.5 years)
    if request.is_json and request.json:
        try:
            window_days = int(request.json.get("window_days", 900))
        except (TypeError, ValueError):
            pass
    result = sync_youtube_analytics(cfg, db_path=_db_path(), window_days=window_days)
    status = 200 if result.get("ok") else 502
    return jsonify(result), status


@app.route("/analytics/advanced/import", methods=["POST"])
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


# ===========================================================================
# Analytics Sync — /analytics/sync
# ===========================================================================

@app.route("/analytics/sync", methods=["GET"])
def analytics_sync_page():
    return redirect("/intelligence/sync", 302)


@app.route("/api/analytics/sync", methods=["POST"])
def api_analytics_sync():
    """
    Multi-platform analytics sync. Currently executes YouTube (API).
    Other platforms return status='skipped' — manual import only.
    Returns {ok, results, total_records, synced_at}.
    """
    from hypogeum.platform_sync import run_platform_sync
    body = request.get_json(silent=True) or {}
    try:
        window_days = int(body.get("window_days", 90))
    except (TypeError, ValueError):
        window_days = 90
    platforms = body.get("platforms") or None
    try:
        result = run_platform_sync(
            db_path=_db_path(),
            window_days=window_days,
            triggered_by="manual",
            platforms=platforms,
        )
        status = 200 if result.get("ok") else 207
        return jsonify(result), status
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/analytics/sync/status", methods=["GET"])
def api_analytics_sync_status():
    """
    Per-platform auto-sync status snapshot.
    Returns connection state, last sync timestamp, last result, and whether
    auto-sync is active for each platform. Non-blocking.
    """
    from hypogeum.platform_sync import get_last_sync_by_platform
    from hypogeum.youtube_analytics import get_last_youtube_sync
    from hypogeum.meta_analytics import get_meta_token, get_meta_token_expiry

    youtube_connected = False
    try:
        from hypogeum.config import Config as _Cfg
        _cfg = _Cfg()
        youtube_connected = bool(
            _cfg.YOUTUBE_REFRESH_TOKEN
            and _cfg.YOUTUBE_CLIENT_ID
            and _cfg.YOUTUBE_CLIENT_SECRET
        )
    except Exception:
        pass

    try:
        last_by_platform = get_last_sync_by_platform(db_path=_db_path())
    except Exception:
        last_by_platform = {}

    try:
        yt_last_synced = get_last_youtube_sync(db_path=_db_path())
    except Exception:
        yt_last_synced = None

    # Read actual credential state for Meta platforms from secret_profiles
    def _meta_platform_entry(platform: str) -> dict:
        token   = get_meta_token(platform, db_path=_db_path())
        expiry  = get_meta_token_expiry(platform, db_path=_db_path())
        log     = last_by_platform.get(platform, {})
        connected = bool(token)
        # Check if token is expired
        expired = False
        if expiry:
            try:
                from datetime import datetime, timezone
                exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                expired = exp_dt < datetime.now(timezone.utc)
            except Exception:
                pass
        if expired:
            auto_sync = "credential_expired"
            connected = False
        elif connected:
            auto_sync = "active"
        else:
            auto_sync = "credential_required"
        return {
            "connected":    connected and not expired,
            "auto_sync":    auto_sync,
            "last_sync":    log.get("synced_at"),
            "last_records": log.get("records_imported"),
            "last_status":  log.get("status"),
        }

    # TikTok: has token but it's expired and no public analytics API exists
    def _tiktok_entry() -> dict:
        from hypogeum.meta_analytics import get_meta_token, get_meta_token_expiry
        # Reuse same helper with 'tiktok' provider
        token   = get_meta_token("tiktok", db_path=_db_path())
        expiry  = get_meta_token_expiry("tiktok", db_path=_db_path())
        log     = last_by_platform.get("tiktok", {})
        expired = False
        if expiry:
            try:
                from datetime import datetime, timezone
                exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                expired = exp_dt < datetime.now(timezone.utc)
            except Exception:
                pass
        if expired:
            auto_sync = "credential_expired"
        elif token:
            auto_sync = "manual_import_only"
        else:
            auto_sync = "credential_required"
        return {
            "connected":    bool(token) and not expired,
            "auto_sync":    auto_sync,
            "last_sync":    log.get("synced_at"),
            "last_records": log.get("records_imported"),
            "last_status":  log.get("status"),
        }

    yt_log = last_by_platform.get("youtube", {})
    platforms = {
        "youtube": {
            "connected":    youtube_connected,
            "auto_sync":    "active" if youtube_connected else "oauth_required",
            "last_sync":    yt_last_synced,
            "last_records": yt_log.get("records_imported") if yt_log else None,
            "last_status":  yt_log.get("status") if yt_log else None,
        },
        "tiktok":    _tiktok_entry(),
        "instagram": _meta_platform_entry("instagram"),
        "facebook":  _meta_platform_entry("facebook"),
    }
    return jsonify({"ok": True, "platforms": platforms})


# ===========================================================================
# Topic Discovery — /discover
# ===========================================================================

def _default_channel_id(db_path=None) -> str:
    """Return the first available channel_id, or 'hypogeum' as fallback."""
    try:
        from hypogeum.dashboard_api import get_channel_summaries
        channels = get_channel_summaries(db_path=db_path)
        if channels:
            return channels[0].channel_id
    except Exception:
        pass
    return "hypogeum"


@app.route("/discover", methods=["GET"])
def discover_page():
    from hypogeum.topic_discovery import get_discovery_state
    from hypogeum.db import initialize_schema, get_connection
    db = _db_path()
    initialize_schema(db)  # ensure discovery_state and provenance columns exist

    try:
        from hypogeum.dashboard_api import get_channel_summaries
        channels = get_channel_summaries(db_path=db)
    except Exception:
        channels = []

    channel_id = request.args.get("channel_id") or _default_channel_id(db)
    flash      = request.args.get("flash")

    state = get_discovery_state(channel_id, db_path=db)

    # Build a set of candidate_ids that are already queued, and the full queue
    queued_candidate_ids: set[str] = set()
    queued_topics: list[dict] = []
    try:
        conn = get_connection(db)
        try:
            rows = conn.execute(
                """SELECT topic_id, channel_id, topic_text, status, priority, source,
                          discovery_score, candidate_id, created_at, updated_at,
                          episode_id
                   FROM episode_topics
                   WHERE channel_id = ? AND status = 'queued'
                   ORDER BY priority ASC, created_at ASC""",
                (channel_id,),
            ).fetchall()
            for r in rows:
                queued_topics.append(dict(r))
                if r["candidate_id"]:
                    queued_candidate_ids.add(r["candidate_id"])
        finally:
            conn.close()
    except Exception:
        pass

    return render_template(
        "discover.html",
        channel_id=channel_id,
        channels=channels,
        candidates=state.get("candidates", []),
        cycle_id=state.get("last_cycle_id"),
        discovery_status=state.get("status", "idle"),
        discovery_error=state.get("error"),
        discovery_started_at=state.get("started_at"),
        discovery_completed_at=state.get("completed_at"),
        queued_candidate_ids=list(queued_candidate_ids),
        queued_topics=queued_topics,
        flash=flash,
    )


@app.route("/api/discover/run", methods=["POST"])
def api_discover_run():
    """
    Start a discovery cycle in a background thread.

    Returns immediately with {"ok": true, "status": "running"} so the
    browser does not need to stay open for the cycle to complete.
    State is written to discovery_state and readable from /discover on reload.
    """
    import threading
    from hypogeum.topic_discovery import get_discovery_state, upsert_discovery_state
    from hypogeum.db import initialize_schema
    db = _db_path()
    initialize_schema(db)

    body = request.get_json(silent=True) or {}
    channel_id = body.get("channel_id") or _default_channel_id(db)

    # Refuse to start a second cycle while one is running
    state = get_discovery_state(channel_id, db_path=db)
    if state.get("status") == "running":
        return jsonify({"ok": True, "status": "running",
                        "message": "A discovery cycle is already in progress"})

    def _run_in_background():
        from hypogeum.topic_discovery import run_discovery_cycle
        try:
            run_discovery_cycle(channel_id, db_path=db)
        except Exception:
            pass  # run_discovery_cycle already writes failed state on exception

    t = threading.Thread(target=_run_in_background, daemon=True)
    t.start()

    return jsonify({"ok": True, "status": "running"})


@app.route("/api/discover/state", methods=["GET"])
def api_discover_state():
    """Return the current discovery_state for a channel (for polling)."""
    from hypogeum.topic_discovery import get_discovery_state
    db = _db_path()
    channel_id = request.args.get("channel_id") or _default_channel_id(db)
    state = get_discovery_state(channel_id, db_path=db)
    return jsonify({
        "status":          state.get("status", "idle"),
        "last_cycle_id":   state.get("last_cycle_id"),
        "candidate_count": state.get("candidate_count", 0),
        "started_at":      state.get("started_at"),
        "completed_at":    state.get("completed_at"),
        "error":           state.get("error"),
    })


@app.route("/api/discover/reset", methods=["POST"])
def api_discover_reset():
    """Force-reset discovery_state to idle, unlocking the Run button after a stuck cycle."""
    from hypogeum.topic_discovery import upsert_discovery_state
    from hypogeum.db import initialize_schema
    db = _db_path()
    initialize_schema(db)
    body = request.get_json(silent=True) or {}
    channel_id = body.get("channel_id") or _default_channel_id(db)
    upsert_discovery_state(channel_id, "idle", error=None, db_path=db)
    return jsonify({"ok": True, "status": "idle"})


@app.route("/api/discover/approve/<candidate_id>", methods=["POST"])
def api_discover_approve(candidate_id: str):
    """Approve the selected candidate — enqueues it as an episode_topic."""
    from hypogeum.topic_discovery import approve_candidate
    result = approve_candidate(candidate_id, db_path=_db_path())
    if request.accept_mimetypes.best == "application/json":
        return jsonify(result)
    flash = "approved" if result.get("ok") else result.get("error", "error")
    channel_id = request.args.get("channel_id") or _default_channel_id(_db_path())
    return redirect(url_for("discover_page") + f"?channel_id={channel_id}&flash={flash}")


@app.route("/api/discover/reject/<candidate_id>", methods=["POST"])
def api_discover_reject(candidate_id: str):
    """Reject a topic candidate."""
    from hypogeum.topic_discovery import reject_candidate
    reason = (request.get_json(silent=True) or {}).get("reason") or request.form.get("reason")
    result = reject_candidate(candidate_id, reason=reason, db_path=_db_path())
    if request.accept_mimetypes.best == "application/json":
        return jsonify(result)
    flash = "rejected" if result.get("ok") else result.get("error", "error")
    channel_id = request.args.get("channel_id") or _default_channel_id(_db_path())
    return redirect(url_for("discover_page") + f"?channel_id={channel_id}&flash={flash}")


@app.route("/learning")
def learning_page():
    return redirect("/intelligence/learning", 302)


@app.route("/learning/action/<action_id>/apply", methods=["POST"])
def learning_action_apply(action_id):
    apply_action(action_id, db_path=_db_path())
    return redirect(url_for("intelligence_learning_page"))


@app.route("/learning/action/<action_id>/dismiss", methods=["POST"])
def learning_action_dismiss(action_id):
    dismiss_action(action_id, db_path=_db_path())
    return redirect(url_for("intelligence_learning_page"))


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
# Prompt Registry API (Phase 2B)
# ---------------------------------------------------------------------------

@app.route("/api/prompts", methods=["GET"])
def api_prompts_list():
    """
    List all versioned prompt files with metadata (no body text).

    Returns:
        200  {"prompts": [...]}  — list of prompt metadata records
    """
    try:
        records = list_prompts()
        return jsonify({"prompts": [r.to_dict(include_body=False) for r in records]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/prompts/<name>", methods=["GET"])
def api_prompts_detail(name: str):
    """
    Return full metadata and body text for one prompt file.

    Args:
        name: Prompt name (filename stem, no extension).

    Returns:
        200  {"prompt": {...}}  — full record including body
        404  {"error": "..."}  — prompt not found
    """
    record = get_prompt(name)
    if record is None:
        return jsonify({"error": f"Prompt {name!r} not found"}), 404
    return jsonify({"prompt": record.to_dict(include_body=True)})


@app.route("/prompts", methods=["GET"])
def prompts_page():
    """
    Prompt Registry dashboard page.

    Loads all registered prompt files with full body text (local file reads
    only — no network, no DB).  The body is rendered server-side so the
    operator can read and compare prompts without any JS fetch.
    """
    records = [get_prompt(r.name) for r in list_prompts()]
    records = [r for r in records if r is not None]
    return render_template("prompts.html", prompts=records)


# ---------------------------------------------------------------------------
# Model Output Comparison (Step D)
# ---------------------------------------------------------------------------

@app.route("/compare", methods=["GET"])
def compare_page():
    """
    Model Output Comparison dashboard page.

    Loads prompt registry and last run from DB. If ?episode_id= is provided,
    also queries archive_index for zero-cost provenance rows.
    """
    from datetime import datetime, timezone as _tz
    db = _db_path()

    prompts = [r for r in [get_prompt(r.name) for r in list_prompts()] if r is not None]
    last_run = get_model_comparison_result(db_path=db, run_type="llm")

    episode_id  = request.args.get("episode_id", "").strip()
    archive_rows: list = []
    if episode_id:
        try:
            from hypogeum.db import get_connection as _gc
            c = _gc(db)
            archive_rows = query_archive_for_comparison(episode_id, c)
            c.close()
        except Exception:
            archive_rows = []

    return render_template(
        "compare.html",
        prompts=prompts,
        llm_matrix=DEFAULT_LLM_MATRIX,
        last_run=last_run,
        episode_id=episode_id,
        archive_rows=archive_rows,
        benchmark_user_message=BENCHMARK_USER_MESSAGE,
    )


@app.route("/api/compare/run", methods=["POST"])
def api_compare_run():
    """
    Trigger a live model comparison run.

    Body (JSON):
        prompt_name     str   — registered prompt stem (e.g. "executive_producer")
        provider_matrix list  — [{provider, model}, ...]; defaults to DEFAULT_LLM_MATRIX
        operator_note   str   — optional note stored with the run
        user_message    str   — optional user message; defaults to BENCHMARK_USER_MESSAGE

    Returns:
        200  {"ok": true, "run_id": ..., "status": ..., "results": [...]}
        400  {"ok": false, "error": "..."}  — missing or unknown prompt
        500  {"ok": false, "error": "..."}  — unexpected failure
    """
    from datetime import datetime, timezone as _tz
    body           = request.get_json(silent=True) or {}
    prompt_name    = body.get("prompt_name", "").strip()
    provider_matrix = body.get("provider_matrix") or DEFAULT_LLM_MATRIX
    operator_note  = body.get("operator_note") or None
    user_message   = body.get("user_message") or BENCHMARK_USER_MESSAGE

    if not prompt_name:
        return jsonify({"ok": False, "error": "prompt_name is required"}), 400

    record = get_prompt(prompt_name)
    if record is None or not record.body:
        return jsonify({"ok": False, "error": f"Prompt {prompt_name!r} not found"}), 400

    initiated_at = datetime.now(_tz.utc).isoformat()
    try:
        result = run_model_comparison(
            prompt_text=record.body,
            initiated_at=initiated_at,
            db_path=_db_path(),
            run_type="llm",
            prompt_name=prompt_name,
            user_message=user_message,
            provider_matrix=provider_matrix,
            operator_note=operator_note,
        )
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/compare/results", methods=["GET"])
def api_compare_results():
    """
    Return the most recent completed model comparison run.

    Query params:
        run_type  str  — "llm" (default) | "image" | "video"

    Returns:
        200  {"ok": true,  "run": {...}}  — run found
        200  {"ok": true,  "run": null}   — no completed run yet
        500  {"ok": false, "error": "..."}
    """
    run_type = request.args.get("run_type", "llm")
    try:
        run = get_model_comparison_result(db_path=_db_path(), run_type=run_type)
        return jsonify({"ok": True, "run": run})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ===========================================================================
# Phase 6 — Infrastructure > Workers
# ===========================================================================

# ---------------------------------------------------------------------------
# Worker process controller
#
# run_worker.py runs as a separate OS process. The dashboard manages it via
# a PID file (.hypogeum-assets/worker.pid). The worker's stdout/stderr are
# captured to .hypogeum-assets/worker.log for log inspection from the UI.
#
# Three modes:
#   start    — launch the continuous loop (interval 60 s); PID written to file
#   run-once — launch with --once; process exits after claiming one job; no PID file
#   stop     — SIGTERM the running worker; PID file removed
# ---------------------------------------------------------------------------

_WORKER_PID_FILE = _ASSETS_ROOT_PATH / "worker.pid"
_WORKER_LOG_FILE = _ASSETS_ROOT_PATH / "worker.log"


def _get_worker_pid() -> "Optional[int]":
    """Return the PID of the running worker process, or None if not running."""
    try:
        pid = int(_WORKER_PID_FILE.read_text().strip())
        import signal as _sig
        os.kill(pid, 0)   # raises OSError if process is dead
        return pid
    except (FileNotFoundError, ValueError, OSError):
        return None


def _worker_status_dict() -> dict:
    """Return a plain dict describing the current worker process state."""
    from hypogeum.db import get_worker_control
    db = _db_path()

    pid = _get_worker_pid()
    queue_paused = False
    try:
        queue_paused = get_worker_control("queue_paused", default="0", db_path=db) == "1"
    except Exception:
        pass

    if pid:
        # Worker process is alive — read fine-grained status from DB
        db_status = "running"
        try:
            db_status = get_worker_control("worker_status", default="running", db_path=db)
        except Exception:
            pass

        _status_class_map = {
            "idle":       "idle",
            "claiming":   "running",
            "running":    "running",
            "cancelling": "paused",
            "stopped":    "unknown",
        }
        _status_label_map = {
            "idle":       "Idle",
            "claiming":   "Claiming",
            "running":    "Running",
            "cancelling": "Cancelling",
            "stopped":    "Stopped",
        }
        status_class = _status_class_map.get(db_status, "running")
        status_label = _status_label_map.get(db_status, db_status.title())
        if queue_paused:
            status_label += " (queue paused)"
        return {
            "running":      True,
            "pid":          pid,
            "status":       db_status,
            "status_label": status_label,
            "status_class": status_class,
            "queue_paused": queue_paused,
        }

    # Check if the PID file exists but process is dead (crashed / killed)
    stale = _WORKER_PID_FILE.exists()
    if stale:
        return {"running": False, "pid": None, "status": "crashed",
                "status_label": "Crashed (stale PID file)", "status_class": "error",
                "queue_paused": queue_paused}
    return {"running": False, "pid": None, "status": "stopped",
            "status_label": "Stopped", "status_class": "unknown",
            "queue_paused": queue_paused}


def _read_worker_log_tail(n: int = 40) -> list[str]:
    """Return the last n non-empty lines from worker.log, newest last."""
    try:
        if not _WORKER_LOG_FILE.exists():
            return []
        with _WORKER_LOG_FILE.open("r", errors="replace") as fh:
            lines = fh.readlines()
        return [l.rstrip() for l in lines[-n:] if l.strip()]
    except Exception:
        return []


def _reconcile_stale_runs() -> list[str]:
    """
    Reconcile orphaned runs in the background (best-effort, never raises).

    Reads worker state then delegates to orphan_runs.reconcile_orphaned_runs().
    Returns the list of run_ids that were marked failed (may be empty).
    """
    try:
        from hypogeum.orphan_runs import reconcile_orphaned_runs
        ws = _worker_status_dict()
        return reconcile_orphaned_runs(
            db_path=_db_path(),
            worker_process_alive=ws.get("running", False),
            worker_db_status=ws.get("status", "stopped"),
        )
    except Exception:
        return []


@app.route("/api/worker/status")
def api_worker_status():
    """Return current worker process state (running/stopped/crashed, PID)."""
    _reconcile_stale_runs()
    return jsonify(_worker_status_dict())


@app.route("/api/worker/start", methods=["POST"])
def api_worker_start():
    """
    Start run_worker.py as a background process (continuous 60-second loop).

    Returns {ok, pid, note} on success or {ok, error, pid} if already running.
    The worker's stdout+stderr are appended to worker.log.
    """
    existing_pid = _get_worker_pid()
    if existing_pid:
        return jsonify({
            "ok": False,
            "error": "already_running",
            "pid": existing_pid,
            "note": f"Worker is already running (PID {existing_pid}). Stop it first.",
        }), 409

    _ASSETS_ROOT_PATH.mkdir(parents=True, exist_ok=True)
    # A prior /api/worker/stop leaves stop_requested="1" in worker_control;
    # the daemon consumes+clears it on its first iteration, so without this
    # the freshly started worker would immediately stop itself. An explicit
    # "start" means the operator wants it running now -- clear the flag.
    try:
        from hypogeum.db import set_worker_control
        set_worker_control("stop_requested", "0", db_path=_db_path())
    except Exception:
        pass
    log_fh = open(_WORKER_LOG_FILE, "a")  # append so history is preserved
    try:
        import sys as _sys
        proc = _subprocess.Popen(
            [_sys.executable, str(_ROOT / "run_worker.py")],
            stdout=log_fh,
            stderr=log_fh,
            cwd=str(_ROOT),
        )
    except Exception as exc:
        log_fh.close()
        return jsonify({"ok": False, "error": str(exc)}), 500

    _WORKER_PID_FILE.write_text(str(proc.pid))
    return jsonify({
        "ok": True,
        "pid": proc.pid,
        "note": f"Worker started (PID {proc.pid}). Auto-picks queued jobs every 60 s.",
    })


@app.route("/api/worker/stop", methods=["POST"])
def api_worker_stop():
    """
    Send SIGTERM to the running worker and set stop_requested in the DB.

    The worker handles SIGTERM gracefully (finishes current iteration then exits).
    The DB flag provides a fallback signal in case SIGTERM is missed.
    Returns {ok, pid, note} or {ok, error} if no worker is running.
    """
    import signal as _sig
    from hypogeum.db import set_worker_control
    pid = _get_worker_pid()
    if not pid:
        _WORKER_PID_FILE.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": "not_running",
                        "note": "No worker process found."}), 404

    # Set DB flag first so even if SIGTERM is slow to deliver, the worker will
    # notice on its next iteration check.
    try:
        set_worker_control("stop_requested", "1", db_path=_db_path())
    except Exception:
        pass

    try:
        os.kill(pid, _sig.SIGTERM)
    except OSError as exc:
        _WORKER_PID_FILE.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": str(exc),
                        "note": "Process may have already exited. PID file removed."}), 500

    _WORKER_PID_FILE.unlink(missing_ok=True)
    return jsonify({
        "ok": True,
        "pid": pid,
        "note": f"SIGTERM sent to worker (PID {pid}). It will finish its current job then exit.",
    })


@app.route("/api/worker/cancel-run/<queue_id>", methods=["POST"])
def api_worker_cancel_run(queue_id: str):
    """
    Request cancellation of the currently executing run (identified by queue_id).

    Sets cancel_requested=1 on the run_queue row. The worker polls this flag
    between pipeline stages and raises PipelineCancelledError at the next stage
    boundary, marking the run cancelled and releasing the worker.

    Only works for items with status='claimed' (i.e., actively executing).
    Queued-but-not-yet-claimed items should use DELETE /api/queue/<id> instead.
    """
    from hypogeum.db import request_cancel_for_queue_item
    ok = request_cancel_for_queue_item(queue_id, db_path=_db_path())
    if ok:
        return jsonify({
            "ok": True,
            "queue_id": queue_id,
            "note": (
                "Cancel requested. The worker will stop at the next stage boundary "
                "and mark the run cancelled. Checkpoints are preserved."
            ),
        })
    return jsonify({
        "ok": False,
        "queue_id": queue_id,
        "error": "not_found_or_terminal",
        "note": "Queue item not found or already in a terminal state.",
    }), 404


@app.route("/api/worker/smoke-test", methods=["POST"])
def api_worker_smoke_test():
    """
    Run a controlled, local-safe smoke test of the full scheduler -> queue
    -> worker path via hypogeum.runtime.smoke_run() — no new orchestration
    logic here, this thin wrapper only calls and reports that function.

    smoke_run() enforces dry_run=True internally regardless of arguments:
    run_pipeline is never called, no content is generated, no publishing
    occurs, no provider API calls are made. Safe to run at any time.
    """
    from hypogeum.runtime import smoke_run
    result = smoke_run(db_path=_db_path())

    payload = {
        "ok":                 result.success,
        "success":            result.success,
        "schedule_id":        result.schedule_id,
        "scheduled_run_ids":  result.scheduled_run_ids,
        "queue_ids":          result.queue_ids,
        "steps_completed":    result.steps_completed,
        "failure_reason":     result.failure_reason,
    }
    if result.poll_result is not None:
        pr = result.poll_result
        payload["poll_summary"] = {
            "iterations":   pr.iterations,
            "completed":    pr.completed,
            "failed":       pr.failed,
            "skipped":      pr.skipped,
            "no_topic":     pr.no_topic,
            "cancelled":    pr.cancelled,
            "stop_reason":  pr.stop_reason,
        }
    return jsonify(payload), (200 if result.success else 500)


@app.route("/api/worker/pause-queue", methods=["POST"])
def api_worker_pause_queue():
    """
    Pause the run queue — the worker will skip claiming new jobs.

    Tick and consume (schedule promotion) continue running normally.
    The currently executing run (if any) is NOT affected.
    Use /api/worker/cancel-run/<queue_id> to cancel the active run.
    """
    from hypogeum.db import set_worker_control
    try:
        set_worker_control("queue_paused", "1", db_path=_db_path())
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({
        "ok": True,
        "queue_paused": True,
        "note": "Queue paused. Worker will finish current job then stop claiming.",
    })


@app.route("/api/worker/resume-queue", methods=["POST"])
def api_worker_resume_queue():
    """Resume a paused queue — the worker resumes claiming jobs normally."""
    from hypogeum.db import set_worker_control
    try:
        set_worker_control("queue_paused", "0", db_path=_db_path())
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({
        "ok": True,
        "queue_paused": False,
        "note": "Queue resumed. Worker will claim the next available job on its next tick.",
    })


@app.route("/api/worker/run-once", methods=["POST"])
def api_worker_run_once():
    """
    Start run_worker.py with --once: claims exactly one queued job, runs it, exits.

    Does NOT write a PID file (process is short-lived).
    Useful to immediately process the next queued topic or rerun without
    waiting for the scheduler.
    Returns {ok, pid, note}.
    """
    _ASSETS_ROOT_PATH.mkdir(parents=True, exist_ok=True)
    log_fh = open(_WORKER_LOG_FILE, "a")
    try:
        import sys as _sys
        proc = _subprocess.Popen(
            [_sys.executable, str(_ROOT / "run_worker.py"), "--once"],
            stdout=log_fh,
            stderr=log_fh,
            cwd=str(_ROOT),
        )
    except Exception as exc:
        log_fh.close()
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({
        "ok": True,
        "pid": proc.pid,
        "note": (
            f"Run-once worker started (PID {proc.pid}). "
            "Claims next queued topic, executes it, then exits. "
            "Checkpoints are honoured — failed runs resume from the last completed stage."
        ),
    })


@app.route("/api/worker/logs")
def api_worker_logs():
    """Return the last N lines of worker.log as a JSON array of strings."""
    n = min(int(request.args.get("n", 60)), 300)
    lines = _read_worker_log_tail(n)
    return jsonify({"ok": True, "lines": lines, "log_path": str(_WORKER_LOG_FILE)})

def _build_workers_context() -> dict:
    """
    Collect background worker state from existing DB tables only.
    Never raises. Each worker section is independently guarded.
    Returns {"ok": True, "workers": [...]} for both template and JSON.
    """
    from hypogeum.db import get_connection
    from hypogeum.dashboard_api import get_schedules
    from hypogeum.platform_sync import get_last_sync_by_platform

    db = _db_path()

    def _conn():
        """Open a DB connection, falling back to default path if db is None."""
        path = db or Path(".hypogeum-assets/hypogeum.db")
        return get_connection(path)

    def _ts(raw):
        """Truncate ISO timestamp to seconds and humanise separator."""
        if not raw:
            return None
        return str(raw)[:19].replace("T", " ")

    _unknown = lambda name, source: {
        "worker_name":  name,
        "status":       "unknown",
        "last_run_at":  None,
        "next_run_at":  None,
        "last_outcome": "No worker state records yet",
        "source":       source,
    }

    workers = []

    # ── 1. Discovery ────────────────────────────────────────────────────────
    try:
        conn = _conn()
        try:
            rows = conn.execute(
                """SELECT channel_id, status, started_at, completed_at,
                          error, candidate_count, updated_at
                   FROM discovery_state
                   ORDER BY updated_at DESC LIMIT 1"""
            ).fetchall()
        finally:
            conn.close()
        if rows:
            r = dict(zip(
                ["channel_id","status","started_at","completed_at",
                 "error","candidate_count","updated_at"], rows[0]
            ))
            raw_s = r["status"] or "unknown"
            # Normalise DB cycle statuses to operator-facing worker statuses
            s = {"completed": "idle", "idle": "idle", "running": "running",
                 "failed": "error"}.get(raw_s, "unknown")
            if r["error"]:
                outcome = f"Error: {r['error'][:120]}"
            elif r["candidate_count"]:
                outcome = f"{r['candidate_count']} candidates scored (last cycle: {raw_s})"
            else:
                outcome = f"Last cycle: {raw_s}"
            workers.append({
                "worker_name":  "Discovery",
                "status":       s,
                "last_run_at":  _ts(r["completed_at"] or r["started_at"] or r["updated_at"]),
                "next_run_at":  None,
                "last_outcome": outcome,
                "source":       "discovery_state",
            })
        else:
            workers.append(_unknown("Discovery", "discovery_state"))
    except Exception:
        workers.append(_unknown("Discovery", "discovery_state"))

    # ── 2. Run Scheduler ────────────────────────────────────────────────────
    try:
        schedules = get_schedules(db)
        prod = next(
            (s for s in schedules
             if s.get("scope") == "factory" or "production" in (s.get("label") or "").lower()),
            schedules[0] if schedules else None,
        )
        if prod:
            sched_status = prod.get("status") or "unknown"
            worker_status = "idle" if sched_status == "active" else sched_status
            cadence = prod.get("cadence") or "unknown"
            workers.append({
                "worker_name":  "Run Scheduler",
                "status":       worker_status,
                "last_run_at":  _ts(prod.get("last_run_at")),
                "next_run_at":  _ts(prod.get("next_run_at")),
                "last_outcome": f"Schedule {sched_status} — {cadence} cadence",
                "source":       "schedules",
            })
        else:
            workers.append(_unknown("Run Scheduler", "schedules"))
    except Exception:
        workers.append(_unknown("Run Scheduler", "schedules"))

    # ── 3. Analytics Sync ───────────────────────────────────────────────────
    try:
        last_by_platform = get_last_sync_by_platform(db)
        if last_by_platform:
            most_recent = max(
                last_by_platform.values(),
                key=lambda x: x.get("synced_at") or "",
            )
            records = most_recent.get("records_imported") or 0
            platform = most_recent.get("platform") or "?"
            status = most_recent.get("status") or "unknown"
            outcome = f"{records} records imported ({platform}) — {status}"
            workers.append({
                "worker_name":  "Analytics Sync",
                "status":       "idle",
                "last_run_at":  _ts(most_recent.get("synced_at")),
                "next_run_at":  None,
                "last_outcome": outcome,
                "source":       "platform_sync_log",
            })
        else:
            workers.append({
                **_unknown("Analytics Sync", "platform_sync_log"),
                "last_outcome": "No sync records yet",
            })
    except Exception:
        workers.append(_unknown("Analytics Sync", "platform_sync_log"))

    # ── 4. Learning Worker ──────────────────────────────────────────────────
    try:
        conn = _conn()
        try:
            row = conn.execute(
                """SELECT MAX(created_at),
                          COUNT(*),
                          SUM(CASE WHEN status='applied'   THEN 1 ELSE 0 END),
                          SUM(CASE WHEN status='pending'   THEN 1 ELSE 0 END),
                          SUM(CASE WHEN status='dismissed' THEN 1 ELSE 0 END)
                   FROM learning_actions"""
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            total, applied, pending, dismissed = (row[1] or 0), (row[2] or 0), (row[3] or 0), (row[4] or 0)
            outcome = (f"{total} action{'s' if total != 1 else ''} total — "
                       f"{applied} applied, {pending} pending, {dismissed} dismissed")
            workers.append({
                "worker_name":  "Learning Worker",
                "status":       "idle",
                "last_run_at":  _ts(row[0]),
                "next_run_at":  None,
                "last_outcome": outcome,
                "source":       "learning_actions",
            })
        else:
            workers.append({
                **_unknown("Learning Worker", "learning_actions"),
                "last_outcome": "No learning actions recorded yet",
            })
    except Exception:
        workers.append(_unknown("Learning Worker", "learning_actions"))

    # ── 5. OIS Monitor ──────────────────────────────────────────────────────
    try:
        conn = _conn()
        try:
            row = conn.execute(
                """SELECT MAX(ingested_at),
                          COUNT(*),
                          SUM(CASE WHEN lifecycle_status='open'     THEN 1 ELSE 0 END),
                          SUM(CASE WHEN blocking=1                  THEN 1 ELSE 0 END)
                   FROM signal_records"""
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            total, open_c, blocking = (row[1] or 0), (row[2] or 0), (row[3] or 0)
            outcome = (f"{open_c} open signal{'s' if open_c != 1 else ''} "
                       f"({blocking} blocking) — {total} total recorded")
            workers.append({
                "worker_name":  "OIS Monitor",
                "status":       "idle",
                "last_run_at":  _ts(row[0]),
                "next_run_at":  None,
                "last_outcome": outcome,
                "source":       "signal_records",
            })
        else:
            workers.append({
                **_unknown("OIS Monitor", "signal_records"),
                "last_outcome": "No signal records yet",
            })
    except Exception:
        workers.append(_unknown("OIS Monitor", "signal_records"))

    # ── 6. Publish Health ───────────────────────────────────────────────────
    try:
        conn = _conn()
        try:
            row = conn.execute(
                """SELECT MAX(published_at),
                          COUNT(*),
                          SUM(CASE WHEN publishing_status='success' THEN 1 ELSE 0 END)
                   FROM publishing_outcomes"""
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            total, success = (row[1] or 0), (row[2] or 0)
            outcome = f"{success} successful, {total} total publishing attempt{'s' if total != 1 else ''}"
            workers.append({
                "worker_name":  "Publish Health",
                "status":       "idle",
                "last_run_at":  _ts(row[0]),
                "next_run_at":  None,
                "last_outcome": outcome,
                "source":       "publishing_outcomes",
            })
        else:
            workers.append({
                **_unknown("Publish Health", "publishing_outcomes"),
                "last_outcome": "No publishing records yet",
            })
    except Exception:
        workers.append(_unknown("Publish Health", "publishing_outcomes"))

    # Include raw schedules for the control panel
    raw_schedules = []
    try:
        from hypogeum.dashboard_api import get_schedules as _gs
        raw_schedules = _gs(_db_path()) or []
    except Exception:
        pass

    # ── Queue snapshot ─────────────────────────────────────────────────────
    # Active (queued + claimed) run_queue rows, ordered by claim priority.
    # Provides operator visibility into what the worker will pick next.
    queue_items: list[dict] = []
    try:
        conn = _conn()
        try:
            rows = conn.execute(
                """
                SELECT queue_id, channel_id, episode_id, priority, status,
                       source, recovery_from_run_id, created_at, queued_at,
                       claimed_at, worker_id, cancel_requested
                FROM run_queue
                WHERE status IN ('queued', 'claimed')
                ORDER BY
                    CASE WHEN status = 'claimed' THEN 0 ELSE 1 END ASC,
                    priority ASC,
                    CASE WHEN source = 'rerun' THEN 0 ELSE 1 END ASC,
                    created_at DESC
                LIMIT 20
                """
            ).fetchall()
            cols = [
                "queue_id", "channel_id", "episode_id", "priority", "status",
                "source", "recovery_from_run_id", "created_at", "queued_at",
                "claimed_at", "worker_id", "cancel_requested",
            ]
            for row in rows:
                item = dict(zip(cols, row))
                item["queued_at"]   = _ts(item.get("queued_at"))
                item["created_at"]  = _ts(item.get("created_at"))
                item["claimed_at"]  = _ts(item.get("claimed_at"))
                queue_items.append(item)
        finally:
            conn.close()
    except Exception:
        pass

    # Queue paused state
    queue_paused = False
    try:
        from hypogeum.db import get_worker_control as _gwc
        queue_paused = _gwc("queue_paused", default="0", db_path=db) == "1"
    except Exception:
        pass

    # ── Factory / runtime status ────────────────────────────────────────────
    # Minimum Operational Dashboard Phase 1 — read-only, via the same
    # assess_factory() already used by /attention and /command. Never
    # duplicates factory_director's own logic. The original
    # FactoryDirectorReport is passed straight through to the template — no
    # DTO copy. Jinja's "." operator already tries attribute access before
    # falling back to item access, so factory_status.readiness_level etc.
    # work unchanged whether this is an object or a dict; only its
    # truthiness (object vs None) and the nested run_clearance.reason
    # access are template-visible differences from a flattened dict.
    factory_status = None
    try:
        factory_status = assess_factory(db_path=db)
    except Exception:
        pass

    # ── Latest worker attempt / blocked reason ──────────────────────────────
    # Minimum Operational Dashboard Phase 1 — the most recent run's
    # WorkerAttempt (hypogeum.worker_integration), via the already-existing
    # list_runs() to find the episode_id. Read-only. Passed through as the
    # original WorkerAttempt object — no DTO copy, same rationale as above.
    #
    # Imported lazily inside this read-only helper.
    latest_worker_attempt = None
    try:
        from hypogeum.worker_integration import load_latest_worker_attempt
        recent = list_runs(db_path=db, limit=1)
        if recent and recent[0].get("episode_id"):
            latest_worker_attempt = load_latest_worker_attempt(recent[0]["episode_id"])
    except Exception:
        pass

    # ── Channel operations profiles (read-only summary) ─────────────────────
    # Minimum Operational Dashboard Phase 1 — via
    # hypogeum.channel_config.get_channel_operations_profile(), never
    # invented or duplicated here. Smallest possible wrapper: {channel_id,
    # profile} per channel — a channel with no configured profile still
    # needs to appear (profile=None), which a bare list of
    # ChannelOperationsProfile objects couldn't represent, so this one
    # case keeps a 2-key wrapper rather than passing the object list raw.
    # The ChannelOperationsProfile object itself is never copied/flattened.
    #
    # Local import to keep dependency loading inside the dashboard context.
    channel_operations_profiles: list[dict] = []
    try:
        from hypogeum.channel_config import list_channel_profiles, get_channel_operations_profile
        for ch in list_channel_profiles(db_path=db):
            try:
                profile = get_channel_operations_profile(ch.channel_id, db_path=db)
            except Exception:
                profile = None
            channel_operations_profiles.append({"channel_id": ch.channel_id, "profile": profile})
    except Exception:
        pass

    return {
        "ok": True,
        "workers": workers,
        "schedules": raw_schedules,
        "queue_items": queue_items,
        "queue_paused": queue_paused,
        "factory_status": factory_status,
        "latest_worker_attempt": latest_worker_attempt,
        "channel_operations_profiles": channel_operations_profiles,
    }


@app.route("/infrastructure/workers")
def infrastructure_workers_page():
    ctx = _build_workers_context()
    ctx["worker_process"] = _worker_status_dict()
    ctx["worker_log_lines"] = _read_worker_log_tail(40)
    # queue_paused is already in ctx from _build_workers_context()
    return render_template("infrastructure_workers.html", **ctx)


@app.route("/api/infrastructure/workers")
def api_infrastructure_workers():
    ctx = _build_workers_context()
    ctx["worker_process"] = _worker_status_dict()
    return jsonify(ctx)


# ===========================================================================
# Phase 7 — Infrastructure > Providers
# ===========================================================================

# Operational-readiness → human health string
_OR_HEALTH = {
    "ready":                          "Operational — smoke test passed",
    "ready_via_muapi":                "Operational via MUAPI aggregator",
    "not_tested":                     "Not tested yet",
    "skipped_missing_key":            "Skipped — no credential configured",
    "auth_failed":                    "Authentication failed",
    "generation_failed":              "Generation test failed",
    "billing_or_credits_failed":      "Billing or credits issue",
    "needs_retest":                   "Stale result — re-test recommended",
    "muapi_dependent":                "Dependent on MUAPI (not yet tested)",
    "unavailable_muapi_not_ready":    "Unavailable — MUAPI not ready",
    "not_configured":                 "Placeholder — not yet implemented",
}

# Map capability list → display category
def _cap_to_category(caps: list) -> str:
    for cap, label in [
        ("publishing", "Publishing"),
        ("video",      "Video"),
        ("audio",      "Voice"),
        ("llm",        "LLM"),
        ("image",      "Image"),
        ("analytics",  "Analytics"),
    ]:
        if cap in caps:
            return label
    return "Other"


def _entry_status(entry) -> str:
    """Map ProviderCatalogEntry fields to connected/degraded/disconnected/unknown."""
    cred = entry.credential_status
    op   = entry.operational_readiness
    if op in ("ready", "ready_via_muapi"):
        return "connected"
    if cred == "verified":
        return "connected"
    if cred in ("saved",) and entry.is_configured:
        return "connected"     # configured, not yet verified — treat as nominally connected
    if op in ("auth_failed", "billing_or_credits_failed", "generation_failed"):
        return "degraded"
    if cred == "failed":
        return "degraded"
    if cred == "missing" or op == "skipped_missing_key":
        return "disconnected"
    if entry.is_configured:
        return "connected"
    return "unknown"


def _build_providers_context() -> dict:
    """
    Collect provider health from get_provider_catalog() only.
    Never exposes credential values. Never raises.
    Returns {"ok": True, "providers": [...]}.
    """
    try:
        from hypogeum.dashboard_api import get_provider_catalog
        catalog = get_provider_catalog(db_path=_db_path())
    except Exception:
        return {"ok": False, "providers": [], "error": "Provider catalog unavailable"}

    providers = []
    for entry in catalog:
        status   = _entry_status(entry)
        category = _cap_to_category(entry.capabilities)
        health   = (entry.operational_note
                    or _OR_HEALTH.get(entry.operational_readiness,
                                      entry.operational_readiness or "Unknown"))

        # last_error: only set when there's a meaningful failure signal
        last_error = None
        if status in ("degraded", "disconnected") and entry.operational_note:
            last_error = entry.operational_note[:200]

        providers.append({
            "provider_name":   entry.display_name,
            "provider_id":     entry.provider,
            "category":        category,
            "capabilities":    entry.capabilities,
            "access_mode":     entry.access_mode,
            "status":          status,
            "api_configured":  entry.is_configured,
            "last_success":    None,      # no per-provider success timestamp in current schema
            "last_error":      last_error,
            "health":          health,
            "source":          "provider_catalog",
            # Usage stats (0 = never used; never fabricated)
            "total_attempts":  entry.total_attempts,
            "failure_count":   entry.failure_count,
        })

    # Sort: configured first, then by category, then by name
    providers.sort(key=lambda p: (
        0 if p["api_configured"] else 1,
        p["category"],
        p["provider_name"],
    ))

    return {"ok": True, "providers": providers}


@app.route("/infrastructure/providers")
def infrastructure_providers_page():
    ctx = _build_providers_context()
    return render_template("infrastructure_providers.html", **ctx)


@app.route("/api/infrastructure/providers")
def api_infrastructure_providers():
    return jsonify(_build_providers_context())


# ===========================================================================
# Phase 8 — Infrastructure > Connections
# ===========================================================================

_PLATFORM_VSTATUS_LABEL = {
    "verified": "Verified",
    "saved":    "Saved — not yet verified",
    "partial":  "Partial credentials",
    "missing":  "Not configured",
    "failed":   "Verification failed",
}

_CONN_TO_STATUS = {
    "connected":          "connected",
    "connected_no_data":  "connected",
    "published_no_cred":  "expired",
    "demo_only":          "disconnected",
    "not_connected":      "disconnected",
}


def _build_connections_context() -> dict:
    """
    Aggregate platform connection health from existing state providers.
    Never exposes credential values. Never raises.
    Returns {"ok": True, "connections": [...]}.
    """
    from hypogeum.platform_connections import get_platform_connection_state, PLATFORMS
    from hypogeum.platform_sync import get_last_sync_by_platform
    from hypogeum.db import get_connection

    db = _db_path()

    # ── 1. High-level connection state (credential presence + analytics coverage) ──
    try:
        conn_state = get_platform_connection_state(db_path=db)
        plat_map   = conn_state.get("platforms", {})
    except Exception:
        plat_map = {}

    # ── 2. OAuth verification detail: vstatus, error, expiry warnings, authorize URLs ──
    try:
        detail_list = _get_platform_detail()
        detail_by_id = {d["id"]: d for d in detail_list}
    except Exception:
        detail_by_id = {}

    # ── 3. Last sync per platform ──
    try:
        last_sync = get_last_sync_by_platform(db_path=db)
    except Exception:
        last_sync = {}

    # ── 4. Last publish per platform (max published_at from publishing_outcomes) ──
    last_publish: dict = {}
    try:
        path = db or Path(".hypogeum-assets/hypogeum.db")
        conn = get_connection(path)
        try:
            rows = conn.execute(
                """SELECT platform,
                          MAX(published_at) AS last_at,
                          COUNT(*) AS total,
                          SUM(CASE WHEN publishing_status='success' THEN 1 ELSE 0 END) AS successes
                   FROM publishing_outcomes
                   WHERE platform IN ('youtube','tiktok','instagram','facebook')
                   GROUP BY platform"""
            ).fetchall()
        finally:
            conn.close()
        for r in rows:
            last_publish[r["platform"]] = {
                "last_at":   r["last_at"],
                "total":     r["total"],
                "successes": r["successes"],
            }
    except Exception:
        pass

    connections = []

    for p_id in PLATFORMS:
        ps      = plat_map.get(p_id, {})
        detail  = detail_by_id.get(p_id, {})
        sync    = last_sync.get(p_id, {})
        pub     = last_publish.get(p_id, {})

        # ── Status ──
        conn_key = ps.get("connection_state", "not_connected")
        vstatus  = detail.get("vstatus", "missing")
        status   = _CONN_TO_STATUS.get(conn_key, "unknown")
        # Downgrade to degraded if verifier explicitly failed
        if vstatus == "failed" and status == "connected":
            status = "degraded"

        # ── Token expiry (safe: only the warning string, never the token) ──
        token_expiry = None
        for cred in detail.get("credentials", []):
            if cred.get("expiry_warning"):
                token_expiry = cred["expiry_warning"]
                # If expiry is imminent, mark as degraded
                if "soon" in token_expiry and status == "connected":
                    status = "degraded"
                break

        # ── OAuth status label ──
        oauth_status = _PLATFORM_VSTATUS_LABEL.get(vstatus, vstatus or "Unknown")

        # ── Last sync ──
        last_sync_str = None
        sync_health   = "No sync history"
        if sync:
            ts = sync.get("synced_at") or ""
            last_sync_str = ts[:19].replace("T", " ") if ts else None
            if sync.get("status") == "ok":
                rec = sync.get("records_imported", 0) or 0
                sync_health = f"Last sync successful — {rec} record{'s' if rec != 1 else ''} imported"
            else:
                sync_health = f"Last sync: {sync.get('status', 'unknown')}"

        # ── Last publish ──
        last_pub_str = None
        pub_health   = "Not published"
        if pub:
            ts = pub.get("last_at") or ""
            last_pub_str = ts[:19].replace("T", " ") if ts else None
            suc  = pub.get("successes", 0) or 0
            tot  = pub.get("total", 0) or 0
            pub_health = f"{suc} of {tot} publishing attempt{'s' if tot != 1 else ''} successful"
        elif conn_key == "published_no_cred":
            pub_health = "Published (credential since expired)"

        # ── Last error (verification only — never credential data) ──
        last_error = (detail.get("error") or "")[:200] or None

        # ── Authorize URL (only expose if already tracked) ──
        authorize_url   = detail.get("authorize_url") or None
        authorize_label = detail.get("authorize_label") or None

        connections.append({
            "platform":          detail.get("label") or p_id.title(),
            "platform_id":       p_id,
            "status":            status,
            "oauth_status":      oauth_status,
            "token_expiry":      token_expiry,
            "last_sync":         last_sync_str,
            "last_publish":      last_pub_str,
            "last_error":        last_error,
            "sync_health":       sync_health,
            "publishing_health": pub_health,
            "has_credential":    bool(ps.get("has_credential")),
            "has_published":     bool(ps.get("has_published")),
            "data_state":        ps.get("data_state", "none"),
            "real_count":        ps.get("real_count", 0),
            "authorize_url":     authorize_url,
            "authorize_label":   authorize_label,
            "source":            "platform_connections + provider_verifications + platform_sync_log",
        })

    return {"ok": True, "connections": connections}


@app.route("/infrastructure/connections")
def infrastructure_connections_page():
    ctx = _build_connections_context()
    return render_template("infrastructure_connections.html", **ctx)


@app.route("/api/infrastructure/connections")
def api_infrastructure_connections():
    ctx = _build_connections_context()
    # Strip authorize_url from JSON (internal routes only; not part of the API spec shape)
    safe = {
        "ok": ctx["ok"],
        "connections": [
            {k: v for k, v in c.items() if k not in ("authorize_url", "authorize_label")}
            for c in ctx["connections"]
        ],
    }
    return jsonify(safe)


# ===========================================================================
# Phase 9 — Infrastructure > Config
# ===========================================================================

def _build_config_context() -> dict:
    """
    Read-only factory configuration state from existing DB tables and env.
    Never exposes secrets, API keys, or OAuth tokens.
    Returns {"ok": True, "config": {...}}.
    """
    from hypogeum.dashboard_api import get_channel_summaries, get_schedules
    from hypogeum.setup_preflight import get_setup_preflight_status
    from hypogeum.channel_profile import get_channel_profile
    from hypogeum.db import get_connection

    db = _db_path()

    # ── Channel summaries ───────────────────────────────────────────────────
    try:
        channels = [_to_dict(c) for c in get_channel_summaries(db_path=db)]
    except Exception:
        channels = []

    # ── Schedules ───────────────────────────────────────────────────────────
    try:
        schedules = get_schedules(db_path=db)
    except Exception:
        schedules = []

    # ── Environment health + budget ─────────────────────────────────────────
    try:
        preflight = get_setup_preflight_status(db_path=db)
    except Exception:
        preflight = {}

    # ── Channel profile settings (content_niche, tone_style, targets) ───────
    try:
        ch_profile = get_channel_profile(db_path=db)
    except Exception:
        ch_profile = {}

    # ── Brand / model / publishing profiles (linked via channel_profiles) ───
    brand_data = {}
    model_data = {}
    publishing_data = {}

    try:
        path = db or Path(".hypogeum-assets/hypogeum.db")
        conn = get_connection(path)
        try:
            cp = conn.execute(
                """SELECT channel_id, name, brand_profile_id,
                          model_profile_id, publishing_profile_id
                   FROM channel_profiles LIMIT 1"""
            ).fetchone()

            if cp:
                # Brand profile — non-sensitive fields only
                if cp["brand_profile_id"]:
                    br = conn.execute(
                        """SELECT name, tone, target_audience, visual_style, language,
                                  content_pillars_json, voice_guidelines
                           FROM brand_profiles WHERE brand_profile_id=?""",
                        [cp["brand_profile_id"]],
                    ).fetchone()
                    if br:
                        try:
                            pillars = _json.loads(br["content_pillars_json"] or "[]")
                        except Exception:
                            pillars = []
                        brand_data = {
                            "name":             br["name"] or "—",
                            "tone":             br["tone"] or "Not set",
                            "target_audience":  br["target_audience"] or "Not set",
                            "visual_style":     (br["visual_style"] or "Not set")[:200],
                            "language":         br["language"] or "en",
                            "content_pillars":  pillars,
                            "voice_guidelines": (br["voice_guidelines"] or "Not set")[:200],
                        }

                # Model profile — show model slugs (public), never voice ID value
                if cp["model_profile_id"]:
                    mr = conn.execute(
                        """SELECT name, episode_producer, research_director,
                                  thumbnail_planner, image_provider, video_provider,
                                  narration_voice_id
                           FROM model_profiles WHERE model_profile_id=?""",
                        [cp["model_profile_id"]],
                    ).fetchone()
                    if mr:
                        model_data = {
                            "name":                       mr["name"] or "—",
                            "episode_producer":           mr["episode_producer"] or "—",
                            "research_director":          mr["research_director"] or "—",
                            "thumbnail_planner":          mr["thumbnail_planner"] or "—",
                            "image_provider":             mr["image_provider"] or "—",
                            "video_provider":             mr["video_provider"] or "—",
                            "narration_voice_configured": bool(mr["narration_voice_id"]),
                        }

                # Publishing profile — non-sensitive fields only
                if cp["publishing_profile_id"]:
                    pr = conn.execute(
                        """SELECT name, enabled_platforms_json, schedule_json,
                                  default_tags_json, ai_disclosure_required,
                                  auto_thumbnail, auto_captions
                           FROM publishing_profiles WHERE publishing_profile_id=?""",
                        [cp["publishing_profile_id"]],
                    ).fetchone()
                    if pr:
                        try:
                            platforms = _json.loads(pr["enabled_platforms_json"] or "[]")
                        except Exception:
                            platforms = []
                        try:
                            sched = _json.loads(pr["schedule_json"] or "{}")
                        except Exception:
                            sched = {}
                        try:
                            tags = _json.loads(pr["default_tags_json"] or "[]")
                        except Exception:
                            tags = []
                        publishing_data = {
                            "name":                   pr["name"] or "—",
                            "enabled_platforms":      platforms,
                            "schedule":               sched,
                            "default_tags":           tags,
                            "ai_disclosure_required": bool(pr["ai_disclosure_required"]),
                            "auto_thumbnail":         bool(pr["auto_thumbnail"]),
                            "auto_captions":          bool(pr["auto_captions"]),
                        }
        finally:
            conn.close()
    except Exception:
        pass

    # ── Factory section (derived; factories table is empty) ─────────────────
    if channels:
        primary = channels[0]
        factory = {
            "name":          primary.get("name") or ch_profile.get("channel_name") or "Factory",
            "status":        primary.get("status", "unknown"),
            "channel_count": len(channels),
            "health":        f"{len(channels)} channel{'s' if len(channels) != 1 else ''} configured",
            "content_niche": ch_profile.get("content_niche") or "Not set",
            "tone_style":    ch_profile.get("tone_style") or "Not set",
        }
    else:
        factory = {
            "name":    ch_profile.get("channel_name") or "Factory",
            "status":  "unknown",
            "channel_count": 0,
            "health":  "No channels configured yet",
            "content_niche": ch_profile.get("content_niche") or "Not set",
            "tone_style":    ch_profile.get("tone_style") or "Not set",
        }

    # ── Budget section ───────────────────────────────────────────────────────
    ceiling = preflight.get("budget_ceiling_usd") or 0.0
    budgets = {
        "ceiling_usd":         ceiling,
        "ceiling_configured":  ceiling > 0,
        "estimated_cost_low":  preflight.get("estimated_cost_low", 18.0),
        "estimated_cost_high": preflight.get("estimated_cost_high", 30.0),
    }

    # ── Environment health ───────────────────────────────────────────────────
    env_vars = preflight.get("env_vars", {})
    environment = {
        "ready":            preflight.get("ready", False),
        "blocker_count":    preflight.get("blocker_count", 0),
        "warning_count":    preflight.get("warning_count", 0),
        # Only show key names + present/missing — never the values
        "env_vars":         {k: ("present" if v else "missing") for k, v in env_vars.items()},
        "configured_count": sum(1 for v in env_vars.values() if v),
        "total_required":   len(env_vars),
        "ffmpeg":           "found" if preflight.get("ffmpeg_found") else "missing",
    }

    return {
        "ok": True,
        "config": {
            "factory":     factory,
            "channels":    channels,
            "schedules":   schedules,
            "publishing":  publishing_data,
            "providers":   model_data,
            "budgets":     budgets,
            "brand":       brand_data,
            "environment": environment,
        },
    }


@app.route("/infrastructure/config")
def infrastructure_config_page():
    ctx = _build_config_context()
    return render_template("infrastructure_config.html", **ctx)


@app.route("/api/infrastructure/config")
def api_infrastructure_config():
    return jsonify(_build_config_context())


# ===========================================================================
# Phase 10 — Infrastructure Overview
# ===========================================================================

def _build_infrastructure_summary() -> dict:
    """
    Lightweight summary from all four infrastructure context builders.
    Never raises. Returns {"ok": True, "summary": {...}}.
    """
    try:
        wctx = _build_workers_context()
        workers = {
            "total":   len(wctx["workers"]),
            "idle":    sum(1 for w in wctx["workers"] if w["status"] == "idle"),
            "running": sum(1 for w in wctx["workers"] if w["status"] == "running"),
            "error":   sum(1 for w in wctx["workers"] if w["status"] == "error"),
            "unknown": sum(1 for w in wctx["workers"] if w["status"] in ("unknown", "paused")),
        }
    except Exception:
        workers = {"total": 0, "idle": 0, "running": 0, "error": 0, "unknown": 0}

    try:
        pctx = _build_providers_context()
        providers = {
            "total":        len(pctx["providers"]),
            "connected":    sum(1 for p in pctx["providers"] if p["status"] == "connected"),
            "degraded":     sum(1 for p in pctx["providers"] if p["status"] == "degraded"),
            "disconnected": sum(1 for p in pctx["providers"] if p["status"] == "disconnected"),
        }
    except Exception:
        providers = {"total": 0, "connected": 0, "degraded": 0, "disconnected": 0}

    try:
        cctx = _build_connections_context()
        connections = {
            "total":        len(cctx["connections"]),
            "connected":    sum(1 for c in cctx["connections"] if c["status"] == "connected"),
            "degraded":     sum(1 for c in cctx["connections"] if c["status"] == "degraded"),
            "expired":      sum(1 for c in cctx["connections"] if c["status"] == "expired"),
            "disconnected": sum(1 for c in cctx["connections"] if c["status"] in ("disconnected", "unknown")),
        }
    except Exception:
        connections = {"total": 0, "connected": 0, "degraded": 0, "expired": 0, "disconnected": 0}

    try:
        kctx = _build_config_context()
        cfg = kctx.get("config", {})
        config = {
            "factory_status":  cfg.get("factory", {}).get("status", "unknown"),
            "channel_count":   cfg.get("factory", {}).get("channel_count", 0),
            "schedule_count":  len(cfg.get("schedules", [])),
            "env_ready":       cfg.get("environment", {}).get("ready", False),
            "blocker_count":   cfg.get("environment", {}).get("blocker_count", 0),
        }
    except Exception:
        config = {"factory_status": "unknown", "channel_count": 0,
                  "schedule_count": 0, "env_ready": False, "blocker_count": 0}

    return {
        "ok": True,
        "summary": {
            "workers":     workers,
            "providers":   providers,
            "connections": connections,
            "config":      config,
        },
    }


@app.route("/infrastructure")
def infrastructure_overview_page():
    ctx = _build_infrastructure_summary()
    return render_template("infrastructure.html", **ctx)


@app.route("/api/infrastructure/summary")
def api_infrastructure_summary():
    return jsonify(_build_infrastructure_summary())


# ===========================================================================
# Phase 11 — Infrastructure > Debug
# ===========================================================================

# Static catalogue of diagnostic surfaces. Status is derived at request time
# by probing each route with the test client — no external calls, no DB writes.
_DEBUG_SURFACES = [
    {
        "name":    "Preflight / System Checks",
        "route":   "/preflight",
        "purpose": "Production-readiness audit: env vars, budget ceiling, OIS signals, "
                   "channel configuration, and FFmpeg availability.",
        "safety":  "Read-only. No state changes.",
    },
    {
        "name":    "Model Comparison",
        "route":   "/compare",
        "purpose": "Side-by-side comparison of image/video generation across providers "
                   "using a fixed test prompt. Results cached in model_comparison_runs.",
        "safety":  "View-only page. Actual comparison runs require explicit POST action.",
    },
    {
        "name":    "Video Provider Test",
        "route":   "/providers/video-test",
        "purpose": "Inspect per-clip video generation results from previous provider "
                   "test runs. Useful for validating prompt quality and provider output.",
        "safety":  "Read-only. Displays cached results only.",
    },
    {
        "name":    "Prompt Registry",
        "route":   "/prompts",
        "purpose": "Browse all versioned agent prompts used by the pipeline — "
                   "Executive Producer, Research Director, Script Engine, etc.",
        "safety":  "Read-only. No prompt editing on this page.",
    },
    {
        "name":    "Command Console",
        "route":   "/command",
        "purpose": "Operator command-state overview: factory status, provider health, "
                   "run state, and next required action. ",
        "safety":  "Read-only status display. No commands are executed from this page.",
    },
    {
        "name":    "OIS Signals",
        "route":   "/signals",
        "purpose": "Signal history from the Operational Intelligence System — "
                   "open and resolved alerts with severity and blocking status.",
        "safety":  "Read-only history view.",
    },
    {
        "name":    "Setup Checklist",
        "route":   "/setup",
        "purpose": "Step-by-step first-run setup guide covering API keys, budget, "
                   "channel configuration, and provider verification.",
        "safety":  "Configuration guidance. Credential saves require explicit form submit.",
    },
]


def _build_debug_context() -> dict:
    """
    Build the debug surfaces list with live availability status.
    Checks each route by probing the Flask app internally — no external calls.
    Never raises.
    """
    surfaces = []
    for s in _DEBUG_SURFACES:
        try:
            with app.test_client() as tc:
                r = tc.get(s["route"], follow_redirects=True)
                status = "available" if r.status_code == 200 else "degraded"
        except Exception:
            status = "unknown"
        surfaces.append({
            "name":    s["name"],
            "route":   s["route"],
            "status":  status,
            "purpose": s["purpose"],
            "safety":  s["safety"],
        })
    return {"ok": True, "debug_surfaces": surfaces}


@app.route("/infrastructure/debug")
def infrastructure_debug_page():
    ctx = _build_debug_context()
    return render_template("infrastructure_debug.html", **ctx)


@app.route("/api/infrastructure/debug")
def api_infrastructure_debug():
    return jsonify(_build_debug_context())


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Hypogeum Cortex local dashboard"
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5001,
                        help="Port (default: 5001)")
    args = parser.parse_args()

    print(f"Cortex Dashboard → http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")

    # Reconcile orphaned runs at startup — any run stuck as 'running' with no
    # worker is stale from a previous crash. Mark them failed before serving traffic.
    try:
        from hypogeum.orphan_runs import reconcile_orphaned_runs
        orphaned = reconcile_orphaned_runs(
            db_path=_db_path(),
            worker_process_alive=False,  # worker is never running at dashboard startup
            worker_db_status="stopped",
        )
        if orphaned:
            print(f"  ⚠ Orphaned runs reconciled at startup: {len(orphaned)} marked failed.")
            for rid in orphaned:
                print(f"    {rid}")
    except Exception as _startup_exc:
        print(f"  [startup reconcile skipped: {_startup_exc}]")

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
