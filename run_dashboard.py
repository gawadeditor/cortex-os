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
import os
import re
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
    "tiktok":    "oauth2_token",
    "instagram": "oauth2_token",
    "facebook":  "oauth2_token",
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
    try:
        setup_preflight = get_setup_preflight_status(db_path=db)
    except Exception:
        setup_preflight = {"ready": False, "blockers": [], "blocker_count": 0}
    error = request.args.get("error")
    # Surface topic queue so the operator can start from a queued topic
    try:
        queued_topics = list_episode_topics(status="queued", db_path=db, limit=10)
    except Exception:
        queued_topics = []
    return render_template(
        "create.html",
        channels=channels,
        preflight=preflight,
        setup_preflight=setup_preflight,
        error=error,
        queued_topics=queued_topics,
    )


@app.route("/create/launch", methods=["POST"])
def create_launch():
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

    error_str = "; ".join(result.get("missing", ["unknown error"]))
    return redirect(url_for("create_page") + f"?error={quote_plus(error_str)}")


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


def _compute_next_action(state, report, recent_episodes: list) -> dict:
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
        stuck = [
            ep for ep in recent_episodes
            if ep.get("_disk_only") and not ep.get("has_render")
        ]
        if stuck:
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

    # Next required action derived from factory state
    next_action = _compute_next_action(state, report, db_episodes)

    return render_template(
        "exec_dashboard.html",
        state=state,
        report=report,
        error=err,
        exec_alerts=exec_alerts,
        overview=overview,
        recent_episodes=db_episodes,
        next_action=next_action,
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
    Build per-platform, per-credential connection detail for /platforms page.
    Returns list of platform dicts — never raises.
    """
    import os as _os
    from datetime import datetime, timezone

    env = _read_env_file()
    db  = _db_path()

    # Secret profiles keyed by provider
    try:
        all_sp = list_secret_profiles(db_path=db)
    except Exception:
        all_sp = []
    sp_by_provider = {}
    for p in all_sp:
        sp_by_provider.setdefault(p.provider, []).append(p)

    # Verification map
    try:
        from hypogeum.provider_verifier import get_all_provider_statuses
        verif = get_all_provider_statuses(db_path=db)
    except Exception:
        verif = {}

    def _age_days(iso: str) -> int | None:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - dt).days
        except Exception:
            return None

    def _cred(key: str, label: str, env_dict: dict,
              sp_list: list, sp_label_filter: str | None = None,
              is_id: bool = False) -> dict:
        """Build one credential row."""
        env_val = env_dict.get(key, "")
        # Find matching secret profile
        sp = None
        for profile in sp_list:
            if sp_label_filter and profile.label != sp_label_filter:
                continue
            sp = profile
            break

        if sp and sp.status == "active":
            source   = "secret_profile"
            present  = True
            masked   = _mask(sp.credential_data or "")
            age      = _age_days(sp.created_at or "")
            sp_label = sp.label
        elif env_val:
            source   = "env"
            present  = True
            masked   = key if is_id else _mask(env_val)  # IDs shown plain
            age      = None
            sp_label = None
        else:
            source   = "missing"
            present  = False
            masked   = ""
            age      = None
            sp_label = None

        return {
            "key":       key,
            "label":     label,
            "source":    source,
            "present":   present,
            "masked":    masked,
            "sp_label":  sp_label,
            "age_days":  age,
            "is_id":     is_id,
        }

    now_utc = datetime.now(timezone.utc)

    platforms = []

    # ── YouTube ──────────────────────────────────────────────────────────────
    yt_sp = sp_by_provider.get("youtube", [])
    yt_refresh = next((p for p in yt_sp if p.label == "youtube-oauth-refresh"), None)
    yt_v = verif.get("youtube", {})
    yt_creds = [
        _cred("YOUTUBE_CLIENT_ID",     "OAuth Client ID",     env, [], is_id=False),
        _cred("YOUTUBE_CLIENT_SECRET", "OAuth Client Secret", env, []),
        _cred("YOUTUBE_REFRESH_TOKEN", "Refresh Token",       env,
              [yt_refresh] if yt_refresh else []),
        _cred("YOUTUBE_CHANNEL_ID",    "Channel ID",          env, [], is_id=True),
    ]
    yt_missing = [c["key"] for c in yt_creds if not c["present"]]
    platforms.append({
        "id":              "youtube",
        "label":           "YouTube",
        "icon":            "YT",
        "vstatus":         yt_v.get("status", "missing"),
        "verified_at":     (yt_v.get("checked_at") or "")[:16].replace("T", " "),
        "latency_ms":      yt_v.get("latency_ms"),
        "real_probe":      False,
        "error":           yt_v.get("error"),
        "credentials":     yt_creds,
        "missing":         yt_missing,
        "account_id":      env.get("YOUTUBE_CHANNEL_ID", ""),
        "account_label":   "Channel ID",
        "authorize_url":   "/platforms/youtube/authorize",
        "authorize_label": "Re-authorize with Google",
        "note":            "Refresh token does not expire unless revoked.",
    })

    # ── Facebook ─────────────────────────────────────────────────────────────
    fb_sp  = sp_by_provider.get("facebook", [])
    fb_tok = next((p for p in fb_sp if p.status == "active"), None)
    fb_v   = verif.get("facebook", {})
    fb_token_age = _age_days(fb_tok.created_at) if fb_tok else None
    fb_token_expiry_warning = None
    if fb_token_age is not None:
        remaining = 60 - fb_token_age
        if remaining <= 7:
            fb_token_expiry_warning = f"Expires in ~{remaining}d — re-authorize soon"
        elif remaining <= 14:
            fb_token_expiry_warning = f"Expires in ~{remaining}d"
    fb_creds = [
        _cred("META_APP_ID",          "App ID",              env, [], is_id=True),
        _cred("META_APP_SECRET",      "App Secret",          env, []),
        _cred("META_FACEBOOK_PAGE_ID","Facebook Page ID",    env, [], is_id=True),
        {**_cred("META_ACCESS_TOKEN", "Long-lived Token",    env,
                 fb_sp if fb_sp else []),
         "expiry_warning": fb_token_expiry_warning,
         "age_days": fb_token_age},
    ]
    fb_missing = [c["key"] for c in fb_creds if not c["present"]]
    platforms.append({
        "id":              "facebook",
        "label":           "Facebook",
        "icon":            "FB",
        "vstatus":         fb_v.get("status", "missing"),
        "verified_at":     (fb_v.get("checked_at") or "")[:16].replace("T", " "),
        "latency_ms":      fb_v.get("latency_ms"),
        "real_probe":      True,
        "error":           fb_v.get("error"),
        "credentials":     fb_creds,
        "missing":         fb_missing,
        "account_id":      env.get("META_FACEBOOK_PAGE_ID", ""),
        "account_label":   "Page ID",
        "authorize_url":   "/platforms/meta/authorize",
        "authorize_label": "Re-authorize with Meta",
        "note":            "Long-lived token valid ~60 days from issue date.",
    })

    # ── Instagram ────────────────────────────────────────────────────────────
    ig_sp  = sp_by_provider.get("instagram", [])
    ig_tok = next((p for p in ig_sp if p.status == "active"), None)
    ig_v   = verif.get("instagram", {})
    ig_token_age = _age_days(ig_tok.created_at) if ig_tok else None
    ig_token_expiry_warning = None
    if ig_token_age is not None:
        remaining = 60 - ig_token_age
        if remaining <= 7:
            ig_token_expiry_warning = f"Expires in ~{remaining}d — re-authorize soon"
        elif remaining <= 14:
            ig_token_expiry_warning = f"Expires in ~{remaining}d"
    ig_creds = [
        _cred("META_APP_ID",               "App ID",              env, [], is_id=True),
        _cred("META_APP_SECRET",           "App Secret",          env, []),
        _cred("META_INSTAGRAM_ACCOUNT_ID", "Instagram Account ID",env, [], is_id=True),
        {**_cred("META_ACCESS_TOKEN", "Long-lived Token",    env,
                 ig_sp if ig_sp else []),
         "expiry_warning": ig_token_expiry_warning,
         "age_days": ig_token_age},
    ]
    ig_missing = [c["key"] for c in ig_creds if not c["present"]]
    platforms.append({
        "id":              "instagram",
        "label":           "Instagram",
        "icon":            "IG",
        "vstatus":         ig_v.get("status", "missing"),
        "verified_at":     (ig_v.get("checked_at") or "")[:16].replace("T", " "),
        "latency_ms":      ig_v.get("latency_ms"),
        "real_probe":      True,
        "error":           ig_v.get("error"),
        "credentials":     ig_creds,
        "missing":         ig_missing,
        "account_id":      env.get("META_INSTAGRAM_ACCOUNT_ID", ""),
        "account_label":   "Account ID",
        "authorize_url":   "/platforms/meta/authorize",
        "authorize_label": "Re-authorize with Meta",
        "note":            "Long-lived token valid ~60 days from issue date.",
    })

    # ── TikTok ───────────────────────────────────────────────────────────────
    tk_sp  = sp_by_provider.get("tiktok", [])
    tk_v   = verif.get("tiktok", {})
    tk_creds = [
        _cred("TIKTOK_CLIENT_KEY",    "Client Key",    env, []),
        _cred("TIKTOK_CLIENT_SECRET", "Client Secret", env, []),
        _cred("TIKTOK_ACCESS_TOKEN",  "Access Token",  env, tk_sp),
        _cred("TIKTOK_OPEN_ID",       "Open ID",       env, [], is_id=True),
    ]
    tk_missing = [c["key"] for c in tk_creds if not c["present"]]
    # TikTok is only connected when ACCESS_TOKEN + OPEN_ID are both verified
    tk_has_token = any(c["present"] for c in tk_creds if c["key"] == "TIKTOK_ACCESS_TOKEN")
    tk_has_oid   = any(c["present"] for c in tk_creds if c["key"] == "TIKTOK_OPEN_ID")
    if tk_v.get("status") == "verified" and tk_has_token and tk_has_oid:
        tk_vstatus = "verified"
    elif tk_has_token or tk_has_oid:
        tk_vstatus = "saved"
    elif tk_creds[0]["present"] or tk_creds[1]["present"]:
        tk_vstatus = "partial"   # has client creds only
    else:
        tk_vstatus = "missing"
    platforms.append({
        "id":              "tiktok",
        "label":           "TikTok",
        "icon":            "TK",
        "vstatus":         tk_vstatus,
        "verified_at":     (tk_v.get("checked_at") or "")[:16].replace("T", " "),
        "latency_ms":      tk_v.get("latency_ms"),
        "real_probe":      True,
        "error":           tk_v.get("error"),
        "credentials":     tk_creds,
        "missing":         tk_missing,
        "account_id":      env.get("TIKTOK_OPEN_ID", ""),
        "account_label":   "Open ID",
        "authorize_url":   None,   # TikTok OAuth not yet implemented in dashboard
        "authorize_label": None,
        "note":            "OAuth not yet wired. Add CLIENT_KEY + CLIENT_SECRET to .env, "
                           "then complete OAuth via TikTok Developer Portal.",
    })

    return platforms


@app.route("/platforms")
def platform_connections_page():
    try:
        platforms = _get_platform_detail()
    except Exception as exc:
        platforms = []
    return render_template("platform_connections.html", platforms=platforms)


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
    import requests as _req
    from hypogeum.config import Config as _Cfg

    # Google error redirect (user denied, etc.)
    error = request.args.get("error")
    if error:
        return render_template(
            "youtube_oauth_result.html",
            ok=False,
            error=error,
            error_description=request.args.get("error_description", ""),
            redirect_uri=_yt_redirect_uri(),
            scope=_YT_OAUTH_SCOPES,
        ), 400

    code = request.args.get("code")
    if not code:
        return render_template(
            "youtube_oauth_result.html",
            ok=False,
            error="no_code",
            error_description="No authorization code in callback parameters.",
            redirect_uri=_yt_redirect_uri(),
            scope=_YT_OAUTH_SCOPES,
        ), 400

    try:
        cfg = _Cfg()
    except Exception as exc:
        return render_template(
            "youtube_oauth_result.html",
            ok=False, error="config_error",
            error_description=str(exc),
            redirect_uri=_yt_redirect_uri(),
            scope=_YT_OAUTH_SCOPES,
        ), 500

    redirect_uri = _yt_redirect_uri()
    token_payload = {
        "code":          code,
        "client_id":     cfg.YOUTUBE_CLIENT_ID,
        "client_secret": cfg.YOUTUBE_CLIENT_SECRET,
        "redirect_uri":  redirect_uri,
        "grant_type":    "authorization_code",
    }

    try:
        tok_resp = _req.post(_YT_TOKEN_URL, data=token_payload, timeout=15)
        token_data = tok_resp.json()
    except Exception as exc:
        return render_template(
            "youtube_oauth_result.html",
            ok=False, error="network_error",
            error_description=str(exc),
            redirect_uri=redirect_uri,
            scope=_YT_OAUTH_SCOPES,
        ), 502

    if not tok_resp.ok:
        return render_template(
            "youtube_oauth_result.html",
            ok=False,
            error=token_data.get("error", "token_exchange_failed"),
            error_description=token_data.get("error_description", ""),
            redirect_uri=redirect_uri,
            scope=_YT_OAUTH_SCOPES,
            client_id=cfg.YOUTUBE_CLIENT_ID,
        ), 400

    refresh_token = token_data.get("refresh_token", "")
    if not refresh_token:
        return render_template(
            "youtube_oauth_result.html",
            ok=False, error="no_refresh_token",
            error_description=(
                "Token exchange succeeded but no refresh_token was returned. "
                "Ensure prompt=consent is sent and the app has not previously "
                "been authorized (or revoke access and retry)."
            ),
            redirect_uri=redirect_uri,
            scope=_YT_OAUTH_SCOPES,
        ), 400

    # Persist: upsert a SecretProfile using credential_type='oauth2_token' with
    # label='youtube-oauth-refresh' to distinguish from a pasted access token.
    db = _db_path()
    try:
        existing = list_secret_profiles(provider="youtube", db_path=db)
        refresh_profiles = [
            p for p in existing
            if p.label == "youtube-oauth-refresh" and p.status == "active"
        ]
        if refresh_profiles:
            update_secret_profile(
                refresh_profiles[0].secret_profile_id,
                credential_data=refresh_token,
                label="youtube-oauth-refresh",
                db_path=db,
            )
        else:
            create_secret_profile(
                provider="youtube",
                label="youtube-oauth-refresh",
                credential_type="oauth2_token",
                credential_data=refresh_token,
                db_path=db,
            )
    except Exception as exc:
        return render_template(
            "youtube_oauth_result.html",
            ok=False, error="db_error",
            error_description=str(exc),
            redirect_uri=redirect_uri,
            scope=_YT_OAUTH_SCOPES,
        ), 500

    return redirect(url_for("providers_page") + "?youtube_authorized=1")


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


def _send_asset(path: Path | None, download_name: str | None = None):
    """Send a file or 404."""
    from flask import send_file
    if path is None:
        from flask import abort
        abort(404)
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
    """Serve the rendered main episode MP4."""
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
    return _send_asset(path, download_name=f"{episode_id}_main.mp4")


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
    "ep_test", "ep_smoke", "ep_review", "ep_val_",
    "ep_xyz", "_lite",
)


def _is_test_episode(episode_id: str) -> bool:
    """
    Return True when episode_id matches a known test/validation naming pattern.
    Production episodes are ep_000, ep_001, ep_002, ep_003, ep_NNN format.
    Test episodes use names like ep_test*, ep_smoke*, ep_review*, ep_val_*, ep_xyz.
    Hash-like IDs (ep_be9664f5 — 8+ hex chars after ep_) are also treated as test.
    """
    import re
    eid = episode_id.lower()
    for pat in _TEST_EP_PATTERNS:
        if pat in eid:
            return True
    # Hash-like ID: ep_ followed by 7+ hex characters
    if re.match(r"^ep_[0-9a-f]{7,}$", eid):
        return True
    return False


@app.route("/episodes")
def episodes_page():
    channel_id = request.args.get("channel_id")
    show_test  = request.args.get("show_test", "0").strip() == "1"

    episodes   = list_episodes(db_path=_db_path())

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

    topics     = list_episode_topics(
        channel_id=channel_id or None, db_path=_db_path()
    )
    from hypogeum.dashboard_api import get_channel_summaries
    channels   = get_channel_summaries(db_path=_db_path())
    preflight  = get_preflight_status(db_path=_db_path())
    return render_template("episodes.html",
                           episodes=episodes, topics=topics,
                           channels=channels, preflight=preflight,
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
