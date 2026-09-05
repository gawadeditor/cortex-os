"""
hypogeum/dashboard_humanize.py — plain-language + humanising helpers for
the operator dashboard.

A paying channel owner is NOT an engineer. They must never be shown:
  - a raw episode id            (ep_f38dc597)         → its real title
  - an internal blocker code    (A_VIDEO, F_THUMBNAIL)→ a plain label + fix
  - a channel slug              (twinkle_mini_tales)  → its display name
  - a machine timestamp         (2026-09-03T08:00:00+00:00) → "in 3 hours"
  - a snake_case status         (blocked_metadata)    → "Missing details"

Every helper here is import-light, never raises, and degrades to a sane
string. Registered as Jinja filters + a context processor by
run_dashboard.py.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Episode identity — never show a raw id where a human title exists
# ─────────────────────────────────────────────────────────────────────────────

_EP_TITLE_CACHE: dict = {}
_ID_RE = re.compile(
    r"^(?:"
    r"ep[_-][a-z0-9_]+"                                  # ep_f38dc597 / ep_twinkle_sep10
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"  # bare UUID
    r"|[0-9a-f]{12,}"                                     # long hex blob
    r")$",
    re.IGNORECASE,
)


def looks_like_episode_id(value) -> bool:
    return bool(value) and bool(_ID_RE.match(str(value).strip()))


def episode_display_name(episode_id, *, db_path=None,
                         fallback: str = "Untitled episode") -> str:
    """Resolve an episode id to its real title (falls back to its topic,
    then a friendly generic). Cached; only real titles are cached so a
    not-yet-titled episode is re-checked on the next request."""
    if not episode_id:
        return fallback
    episode_id = str(episode_id).strip()
    if not looks_like_episode_id(episode_id):
        # already a human string — leave it alone
        return episode_id
    key = (str(db_path or ""), episode_id)
    if key in _EP_TITLE_CACHE:
        return _EP_TITLE_CACHE[key]
    title = None
    try:
        from hypogeum.db import get_connection
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            row = conn.execute(
                "SELECT title, topic FROM episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if row:
                title = ((row["title"] or "").strip()
                         or (row["topic"] or "").strip()
                         or None)
            if not title:
                # the queued topic that spawned this episode still carries its name
                trow = conn.execute(
                    "SELECT topic_text FROM episode_topics "
                    "WHERE episode_id = ? AND topic_text IS NOT NULL "
                    "ORDER BY rowid DESC LIMIT 1",
                    (episode_id,),
                ).fetchone()
                if trow and (trow["topic_text"] or "").strip():
                    title = trow["topic_text"].strip()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        title = None
    if title:
        _EP_TITLE_CACHE[key] = title
        return title
    return fallback


def clear_episode_title_cache() -> None:
    _EP_TITLE_CACHE.clear()


# Episode ids embedded *inside* a free-text sentence (error messages, signal
# text, decision evidence). "ep_af078f34" / 'ep_twinkle_sep10' — optional
# surrounding quotes are consumed so we don't end up double-quoting.
_INLINE_EP_RE = re.compile(r"['\"]?\bep[_-][a-z0-9]{6,}\b['\"]?", re.IGNORECASE)
# a bare run/queue UUID an operator has no use for, with any leading label
_INLINE_UUID_RE = re.compile(
    r"\b(?:run|queue|worker|item|id)?\s*['\"]?"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}['\"]?", re.IGNORECASE)


def plain_ids(text, *, db_path=None) -> str:
    """Rewrite raw episode ids inside a sentence to the episode's real
    title in quotes, and drop bare run/queue UUIDs. For anything shown to
    a channel owner: signal text, run errors, decision evidence."""
    if not text:
        return ""
    s = str(text)

    def _ep(m):
        raw = m.group(0).strip("'\"")
        name = episode_display_name(raw, db_path=db_path, fallback="")
        return f'"{name}"' if name and name != raw else "this episode"

    s = _INLINE_EP_RE.sub(_ep, s)
    s = _INLINE_UUID_RE.sub("this run", s)
    # tidy the gaps left behind: "( )", doubled spaces, "run this run"
    s = re.sub(r"\(\s*\)", "", s)
    s = re.sub(r"\b(run|queue item)\s+this run\b", "this run", s, flags=re.I)
    s = re.sub(r"\bepisode\s+this episode\b", "this episode", s, flags=re.I)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s[0].upper() + s[1:] if s else s


_CHANNEL_NAME_CACHE: dict = {}


def channel_display_name(channel_id, *, db_path=None) -> str:
    """twinkle_mini_tales → 'Twinkle Mini Tales'. Falls back to a
    title-cased slug so it never shows raw snake_case."""
    if not channel_id:
        return ""
    cid = str(channel_id).strip()
    key = (str(db_path or ""), cid)
    if key in _CHANNEL_NAME_CACHE:
        return _CHANNEL_NAME_CACHE[key]
    name = None
    try:
        from hypogeum.db import get_connection
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            for tbl in ("channel_profiles", "channels", "channel_operations_profiles"):
                try:
                    row = conn.execute(
                        f"SELECT name FROM {tbl} WHERE channel_id = ?", (cid,)
                    ).fetchone()
                except Exception:  # noqa: BLE001
                    continue
                if row and (row["name"] or "").strip():
                    name = row["name"].strip()
                    break
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        name = None
    if not name:
        name = re.sub(r"[_\-]+", " ", cid).strip().title()
    _CHANNEL_NAME_CACHE[key] = name
    return name


# ─────────────────────────────────────────────────────────────────────────────
# Plain-language problem catalogue
#
# Each entry: (label, what_it_means, what_to_do). `label` is a calm noun
# phrase; `what_to_do` is one plain sentence. Keyed by the internal code,
# case-insensitively, with a few aliases.
# ─────────────────────────────────────────────────────────────────────────────

_PROBLEMS: dict[str, tuple[str, str, str]] = {
    # ── production output gate ──────────────────────────────────────────
    "a_video": (
        "Video quality check",
        "The finished video didn't meet the resolution or format the "
        "platforms need.",
        "Cortex will re-render the affected clips automatically on the next run.",
    ),
    # ── publish readiness capabilities ─────────────────────────────────
    "a_metadata": (
        "Missing publish details",
        "The title, description or caption for this episode isn't ready yet.",
        "Open the episode and fill in the missing publish details, then retry.",
    ),
    "b_long_render": (
        "Main video not ready",
        "The full-length video file hasn't finished rendering.",
        "Wait for production to finish, or re-run the episode if it stalled.",
    ),
    "c_shorts_renders": (
        "Short clips not ready",
        "One or more of the short vertical clips hasn't finished rendering.",
        "Wait for production to finish, or re-run the episode if it stalled.",
    ),
    "d_publishing_binding": (
        "Channel not connected to this platform",
        "This channel isn't linked to the social account for one of the "
        "target platforms.",
        "Go to the channel's Publishing tab and connect the account in Zernio.",
    ),
    "e_media_staging_ready": (
        "Upload preparation failed",
        "The video couldn't be uploaded to the staging area before publishing.",
        "This is usually temporary — retry the publish in a few minutes.",
    ),
    "f_thumbnail": (
        "Thumbnail not approved",
        "The episode's thumbnail hasn't passed the brand check yet.",
        "Open the episode's Thumbnail tab and approve or regenerate it.",
    ),
    "channel_scope": (
        "Wrong channel",
        "This action was aimed at the wrong channel.",
        "Reload the page and try again from the correct channel.",
    ),
    # ── episode_state blockers ─────────────────────────────────────────
    "missing_render": (
        "Video not assembled",
        "The final video file wasn't produced — assembly didn't complete.",
        "Re-run the episode; if it keeps failing, open it to see which step stopped.",
    ),
    "qa_failed": (
        "Quality review failed",
        "The automated quality review found a problem serious enough to hold "
        "the episode.",
        "Open the episode's Quality tab to see what failed, then re-run or fix it.",
    ),
    "qa_warning": (
        "Quality review — needs a look",
        "The episode passed quality review but with warnings worth checking "
        "before it goes public.",
        "Open the Quality tab, review the warnings, and approve if they're fine.",
    ),
    "script_bga_blocked": (
        "Script held by brand check",
        "The script didn't match the channel's brand and tone rules.",
        "Review the script against the brand guide, or adjust the brand rules.",
    ),
    "thumbnail_tig_blocked": (
        "Thumbnail rejected by brand check",
        "The thumbnail didn't match the channel's visual identity.",
        "Open the Thumbnail tab and regenerate it, or adjust the brand identity.",
    ),
    # ── publish flow outcomes ─────────────────────────────────────────
    "blocked_metadata": (
        "Missing publish details",
        "The episode can't be published because required title/description/"
        "caption information is missing.",
        "Open the episode and complete the publish details.",
    ),
    "blocked_readiness": (
        "Not ready to publish",
        "One or more pre-publish checks haven't passed yet.",
        "Open the episode to see which check is holding it.",
    ),
    "blocked_schedule": (
        "No release time set",
        "Cortex couldn't work out when to release this episode.",
        "Set a daily release time for the channel in its publishing settings.",
    ),
    "blocked_publish": (
        "Publishing didn't go through",
        "The platforms rejected the upload or it failed partway.",
        "This is often temporary — Cortex will retry automatically.",
    ),
    "blocked_output": (
        "Video quality check",
        "The finished video didn't meet what the platforms need.",
        "Cortex will re-render the affected parts automatically.",
    ),
    # ── run failure reasons ───────────────────────────────────────────
    "other": (
        "Production stopped unexpectedly",
        "A production run ended early for a reason Cortex couldn't categorise.",
        "Open the run to see the last step that ran, then re-run the episode.",
    ),
    "timeout": (
        "A step took too long",
        "Part of production ran past its time limit and was stopped.",
        "Re-run the episode — this is usually a slow provider, not a real fault.",
    ),
    "provider_error": (
        "A creative tool failed",
        "One of the AI providers (video, voice or images) returned an error.",
        "Re-run the episode; if it repeats, check that provider on the Providers page.",
    ),
    "budget_exceeded": (
        "Weekly budget reached",
        "This channel has spent its production budget for the week.",
        "Raise the weekly budget in the channel's production plan, or wait for the reset.",
    ),
}

_PROBLEM_ALIASES = {
    "failed": "other",
    "error": "other",
    "render_missing": "missing_render",
    "qa_gate_failed": "qa_failed",
    "instagram_caption_too_long": "blocked_metadata",
    "validation_error": "blocked_metadata",
    "stale_reconciliation": "blocked_publish",
}


# Free-text error phrases → a plain (label, meaning, action). Matched as a
# case-insensitive substring against the raw error message, in order.
_ERROR_PHRASES: list[tuple[str, tuple[str, str, str]]] = [
    ("blocked by qa", (
        "Quality review held the episode",
        "The automated quality review found a problem serious enough to stop "
        "the episode before publishing — often a missing video or music track.",
        "Open the episode's Quality tab to see what failed, then re-run it.",
    )),
    ("assembly did not produce a viable package", (
        "Final video didn't come together",
        "The assembly step finished without a usable video file.",
        "Re-run the episode; if it repeats, open the run to see where it stalled.",
    )),
    ("already completed under admission", (
        "This episode was already produced",
        "Cortex refused to run it a second time because it finished once already.",
        "No action needed — look at the finished episode instead.",
    )),
    ("run orphaned", (
        "Production stopped when the worker restarted",
        "The production process was interrupted — usually a worker restart, not a real fault.",
        "Cortex re-queues these automatically; no action needed unless it repeats.",
    )),
    ("killed by operator", (
        "Production was restarted mid-run",
        "Someone restarted production while this episode was being made.",
        "Cortex resumes from where it stopped — no work or money is lost.",
    )),
    ("worker restart", (
        "Production stopped when the worker restarted",
        "The production process was interrupted by a restart.",
        "Cortex resumes automatically.",
    )),
    ("production plan already exists", (
        "Couldn't re-plan a half-finished episode",
        "This episode was partly produced before, and its saved plan doesn't match a fresh one.",
        "Move the old plan file aside and let it re-run, or start the episode fresh.",
    )),
    ("content_hash", (
        "Saved plan mismatch",
        "A saved production plan no longer matches what Cortex would produce now.",
        "Re-run the episode from a clean state.",
    )),
    ("does not supply exactly one valid measured duration", (
        "Voiceover timing didn't line up",
        "The narration timings for one or more parts of the script weren't recorded.",
        "Re-run the episode — this is usually a one-off in the voice provider.",
    )),
    ("instagram_caption_too_long", (
        "Caption too long for Instagram",
        "The description was longer than Instagram allows.",
        "Fixed — captions are now trimmed for short videos.",
    )),
    ("no active grant", (
        "Payment not authorized",
        "This channel has no signed authorization for paid production.",
        "Sign the channel's payment authorization on its Settings page.",
    )),
    ("timeout", (
        "A step took too long",
        "Part of production ran past its time limit.",
        "Re-run the episode — usually a slow provider, not a real fault.",
    )),
    ("rate limit", (
        "A provider was busy",
        "One of the AI providers asked us to slow down.",
        "Cortex retries automatically after a short wait.",
    )),
]


def _problem_key(code) -> Optional[str]:
    if not code:
        return None
    k = re.sub(r"[^a-z0-9_]+", "_", str(code).strip().lower()).strip("_")
    if k in _PROBLEMS:
        return k
    if k in _PROBLEM_ALIASES:
        return _PROBLEM_ALIASES[k]
    k2 = re.sub(r"_(gate|check|blocked|failed|error)$", "", k)
    if k2 in _PROBLEMS:
        return k2
    return None


def plain_problem(code, *, default_label: str = "Production stopped") -> dict:
    """Return {label, meaning, action, known} for an internal problem code or
    a raw error message."""
    key = _problem_key(code)
    if key is not None:
        label, meaning, action = _PROBLEMS[key]
        return {"label": label, "meaning": meaning, "action": action, "known": True}

    raw = str(code or "").strip()
    low = raw.lower()
    for phrase, (label, meaning, action) in _ERROR_PHRASES:
        if phrase in low:
            return {"label": label, "meaning": meaning, "action": action, "known": True}

    # short codes (a_video, blocked_x) that slipped through: de-snake them
    if raw and len(raw) <= 40 and " " not in raw:
        pretty = re.sub(r"[_\-]+", " ", raw).strip().capitalize()
        return {"label": pretty, "meaning": "", "action": "Open the item to see the details.", "known": False}

    # a long free-text error we don't recognise — never dump it as the label
    return {
        "label": default_label,
        "meaning": "Cortex hit a problem it couldn't categorise.",
        "action": "Open the full log to see exactly what happened, then re-run the episode.",
        "known": False,
    }


def plain_problem_label(code) -> str:
    return plain_problem(code)["label"]


# ─────────────────────────────────────────────────────────────────────────────
# Status vocabulary — snake_case machine state → calm human phrase
# ─────────────────────────────────────────────────────────────────────────────

_STATUS_WORDS: dict[str, str] = {
    "in_progress": "In production",
    "running": "Running",
    "queued": "Waiting in queue",
    "claimed": "Starting",
    "pending": "Waiting",
    "complete": "Finished",
    "completed": "Finished",
    "published": "Published",
    "partial": "Partly published",
    "failed": "Stopped",
    "cancelled": "Cancelled",
    "superseded": "Replaced",
    "skipped": "Skipped",
    "consumed": "Used",
    "draft": "Draft",
    "active": "Active",
    "paused": "Paused",
    "idle": "Idle — waiting for the next slot",
    "blocked": "Held",
    "awaiting_review": "Waiting for your review",
    "awaiting_publish_approval": "Waiting for your approval",
    "needs_review": "Needs your review",
    "approved": "Approved",
    "rejected": "Rejected",
    "resolved": "Resolved",
    "processing": "Processing on the platform",
    "acknowledged": "Accepted by the platform",
    "succeeded": "Done",
    "submitting": "Sending to the platform",
    "not_planned": "Not scheduled",
    "ready": "Ready",
    "not_ready": "Not ready",
    "api_connected": "Connected",
    "manual_import": "Imported by hand",
    "no_data": "No data yet",
    "in_progress": "In production",
    "in_production": "In production",
}


def plain_status(value, *, default: Optional[str] = None) -> str:
    if value is None or value == "":
        return default if default is not None else "—"
    k = str(value).strip().lower()
    if k in _STATUS_WORDS:
        return _STATUS_WORDS[k]
    return re.sub(r"[_\-]+", " ", k).strip().capitalize()


# ─────────────────────────────────────────────────────────────────────────────
# Time — machine timestamps → what a person actually wants to know
# ─────────────────────────────────────────────────────────────────────────────

def _parse_dt(value) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            # tolerate "2026-09-03 08:00:00" and date-only
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(s[:19], fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def relative_time(value, *, now: Optional[datetime] = None) -> str:
    """'in 3 hours' / '5 minutes ago' / 'just now'."""
    dt = _parse_dt(value)
    if dt is None:
        return "—"
    now = now or datetime.now(timezone.utc)
    delta = dt - now
    secs = delta.total_seconds()
    future = secs > 0
    secs = abs(secs)

    if secs < 45:
        return "just now" if not future else "any moment now"
    mins, hours, days = secs / 60, secs / 3600, secs / 86400
    if mins < 45:
        n = round(mins); unit = "minute" if n == 1 else "minutes"
    elif hours < 22:
        n = round(hours); unit = "hour" if n == 1 else "hours"
    elif days < 6:
        n = round(days); unit = "day" if n == 1 else "days"
    elif days < 45:
        n = round(days / 7); unit = "week" if n == 1 else "weeks"
    else:
        n = round(days / 30); unit = "month" if n == 1 else "months"
    return f"in {n} {unit}" if future else f"{n} {unit} ago"


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def friendly_datetime(value, *, tz_name: Optional[str] = None) -> str:
    """'3 Sep, 8:00 AM' in the given timezone (default UTC). For when an
    exact wall-clock time matters (a scheduled release)."""
    dt = _parse_dt(value)
    if dt is None:
        return "—"
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo(tz_name))
        except Exception:  # noqa: BLE001
            pass
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{dt.day} {_MONTHS[dt.month - 1]}, {hour}:{dt.minute:02d} {ampm}"


def friendly_date(value) -> str:
    dt = _parse_dt(value)
    if dt is None:
        return "—"
    return f"{dt.day} {_MONTHS[dt.month - 1]} {dt.year}"


# ─────────────────────────────────────────────────────────────────────────────
# Misc small humanisers
# ─────────────────────────────────────────────────────────────────────────────

def friendly_money(value, *, currency: str = "$") -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    if n == 0:
        return f"{currency}0"
    if abs(n) >= 1000:
        return f"{currency}{n/1000:.1f}k"
    return f"{currency}{n:,.0f}" if n == int(n) else f"{currency}{n:,.2f}"


def friendly_count(value, singular: str, plural: Optional[str] = None) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "—"
    word = singular if n == 1 else (plural or singular + "s")
    return f"{n:,} {word}"


_LEARNING_ACTIONS = {
    "avoid": ("Do less of this", "down"),
    "reduce": ("Do less of this", "down"),
    "change": ("Adjust the approach", "change"),
    "adjust": ("Adjust the approach", "change"),
    "increase": ("Do more of this", "up"),
    "boost": ("Do more of this", "up"),
    "keep": ("Keep doing this", "up"),
    "maintain": ("Keep doing this", "up"),
}


def plain_learning_action(action_type):
    """('avoid'|'change'|'increase'|…) → {label, direction}."""
    k = str(action_type or "").strip().lower()
    label, direction = _LEARNING_ACTIONS.get(k, (k.capitalize() or "Review", "change"))
    return {"label": label, "direction": direction}


_DIMENSIONS = {
    "topic": "Topic choice",
    "pacing": "Pacing",
    "thumbnail": "Thumbnail",
    "title": "Title",
    "hook": "Opening hook",
    "retention": "Retention",
    "cta": "Call to action",
    "audio": "Audio",
    "structure": "Story structure",
    "length": "Length",
    "publishing_time": "Publishing time",
    "script": "Script",
    "video": "Video",
}


def plain_dimension(value) -> str:
    k = str(value or "").strip().lower()
    return _DIMENSIONS.get(k, re.sub(r"[_\-]+", " ", k).strip().capitalize())


_STAGE_NAMES = {
    "executive_producer": "Topic check",
    "research_director": "Research",
    "narrative_architect": "Story structure",
    "script_engine": "Script",
    "script": "Script",
    "thumbnail_strategy": "Thumbnail plan",
    "visual_package_director": "Visuals",
    "vpd": "Visuals",
    "vpd_stage_a": "Visuals — planning",
    "vpd_stage_b": "Visuals — generating clips",
    "thumbnail_director": "Thumbnail",
    "audio_agent": "Voiceover & music",
    "audio": "Voiceover & music",
    "narration": "Voiceover",
    "assembly_agent": "Final video",
    "assembly": "Final video",
    "publishing_agent": "Publishing",
    "publishing": "Publishing",
    "archive_agent": "Archiving",
    "archive": "Archiving",
    "research": "Research",
    "visuals": "Visuals",
    "startup_checks": "Startup checks",
    "finance_agent_authorization": "Budget approval",
    "finance_authorization": "Budget approval",
    "finance_approval": "Budget approval",
    "source_enhancement": "Video polish",
    "coverage_planning": "Shot planning",
    "coverage_fill": "Extra shots",
    "clip_repair": "Clip fixes",
    "qa_agent": "Quality review",
    "qa": "Quality review",
    "compliance_agent": "Compliance check",
    "brand_guardian": "Brand check",
    "cross_platform_readiness": "Publish checks",
}


def plain_stage_name(value) -> str:
    k = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    if k in _STAGE_NAMES:
        return _STAGE_NAMES[k]
    # strip a trailing _agent / _director / _engine and retry
    k2 = re.sub(r"_(agent|director|engine|stage)$", "", k)
    if k2 in _STAGE_NAMES:
        return _STAGE_NAMES[k2]
    return re.sub(r"[_\-]+", " ", k).strip().capitalize()


# ── Channel runtime blockers (free-text sentences from channel_runtime_status)
_CHANNEL_BLOCKER_PATTERNS = [
    (r"consecutive failed runs",
     "Several production runs failed in a row — this channel needs a look before it runs again."),
    (r"last run failed",
     "The most recent production run didn't finish."),
    (r"operator[_ ]enabled is False",
     "This channel hasn't been switched on for production yet."),
    (r"unattended[_ ]operation[_ ]enabled is False",
     "Automatic production is turned off for this channel."),
    (r"production[_ ]enabled is False",
     "Production is turned off for this channel."),
    (r"publishing[_ ]schedule is empty",
     "No release time is set for this channel."),
    (r"weekly video budget.*exhaust",
     "This channel has used its production budget for the week."),
    (r"no active channel schedule",
     "This channel has no daily schedule, so nothing triggers it automatically."),
    (r"worker heartbeat is stale",
     "The production worker isn't running right now."),
    (r"factory clearance denied",
     "The factory is paused, so this channel can't run."),
    (r"calog channel state",
     "This channel isn't marked active yet."),
    (r"no channel[_ ]operations[_ ]profile|channel profile does not exist",
     "This channel's setup isn't finished."),
    (r"operating grant|\bgrant\b",
     "This channel's production authorization needs to be signed or renewed."),
]


def plain_channel_blocker(text) -> str:
    if not text:
        return ""
    s = str(text)
    for pat, plain in _CHANNEL_BLOCKER_PATTERNS:
        if re.search(pat, s, re.IGNORECASE):
            return plain
    # unknown: drop any "(parenthetical engineer note)" and tidy
    s = re.sub(r"\s*\([^)]*\)", "", s).strip()
    s = re.sub(r"\s*—\s*needs attention$", "", s).strip()
    return s[0].upper() + s[1:] if s else s


_WEEK_KEY_RE = re.compile(r"^(\d{4})-W(\d{1,2})$")


def friendly_week(week_key) -> str:
    """'2026-W36' → 'this week' when it is, else 'week of 1 Sep'."""
    m = _WEEK_KEY_RE.match(str(week_key or "").strip())
    if not m:
        return str(week_key or "")
    year, week = int(m.group(1)), int(m.group(2))
    try:
        monday = datetime.fromisocalendar(year, week, 1)
    except ValueError:
        return str(week_key)
    now = datetime.now(timezone.utc)
    this_week = now.isocalendar()
    if (year, week) == (this_week[0], this_week[1]):
        return "this week"
    return f"week of {monday.day} {_MONTHS[monday.month - 1]}"


def friendly_platform(value) -> str:
    return {
        "youtube": "YouTube", "tiktok": "TikTok",
        "instagram": "Instagram", "facebook": "Facebook",
    }.get(str(value or "").strip().lower(), str(value or "").title())


# ── Learning rationale: strip the machine tail an owner shouldn't read ──
_RATIONALE_TAIL = re.compile(
    r"\s*[.—\-]*\s*Signal(?:s)? from [a-z, ]+"
    r"(?:\s*\(avg(?:erage)? (?:signal )?strength:?\s*-?\d*\.?\d+\))?\.?\s*$",
    re.IGNORECASE,
)
_STRENGTH_PAREN = re.compile(
    r"\s*\(avg(?:erage)? (?:signal )?strength:?\s*-?\d*\.?\d+\)\.?\s*$", re.IGNORECASE)


def clean_rationale(text) -> str:
    """Drop the '… Signal from tiktok (avg strength: -0.50)' tail — the
    platform + strength are shown as UI chips instead."""
    if not text:
        return ""
    s = str(text).strip()
    s = _RATIONALE_TAIL.sub("", s)
    s = _STRENGTH_PAREN.sub("", s)
    return s.strip().rstrip("—-").strip() or str(text).strip()


_PROVIDER_NAMES = {
    "muapi": "MUAPI (media generation)",
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
    "elevenlabs": "ElevenLabs (voice)",
    "perplexity": "Perplexity (research)",
    "youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram",
    "facebook": "Facebook", "zernio": "Zernio",
}

_MODEL_NAMES = {
    "anthropic/claude-sonnet-4.6": "Claude Sonnet 4.6",
    "anthropic/claude-opus-4.6": "Claude Opus 4.6",
    "claude-sonnet-4.6": "Claude Sonnet 4.6",
    "claude-opus-4.6": "Claude Opus 4.6",
    "veo3-text-to-video": "Veo 3 (premium video)",
    "veo3.1-lite-text-to-video": "Veo 3.1 Lite (video)",
    "veo3-fast-text-to-video": "Veo 3 Fast (video)",
    "wan2.7-text-to-video": "Wan 2.7 (portrait video)",
    "wan2.7-image-to-video": "Wan 2.7 (image→video)",
    "seedance-pro-t2v-fast": "Seedance Pro (video)",
    "seedance-pro-i2v-fast": "Seedance Pro (image→video)",
    "ai-video-upscaler": "AI video upscaler",
    "eleven_multilingual_v2": "ElevenLabs Multilingual v2",
    "music_v1": "ElevenLabs Music",
}


def friendly_provider(value) -> str:
    """'muapi' → 'MUAPI (media generation)'. Unknown → title-cased."""
    if not value:
        return "—"
    k = str(value).strip().lower()
    if k in _PROVIDER_NAMES:
        return _PROVIDER_NAMES[k]
    return str(value).replace("_", " ").replace("-", " ").title()


def friendly_model(value) -> str:
    """A provider model slug → a name an operator recognises.
    'anthropic/claude-sonnet-4.6' → 'Claude Sonnet 4.6'."""
    if not value:
        return "—"
    k = str(value).strip()
    if k in _MODEL_NAMES:
        return _MODEL_NAMES[k]
    if k.lower() in _MODEL_NAMES:
        return _MODEL_NAMES[k.lower()]
    tail = k.split("/")[-1]
    return tail.replace("_", " ").replace("-", " ").strip().title() or "—"


_TECH_TOKEN_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(
        list(_PROVIDER_NAMES) + list(_MODEL_NAMES), key=len, reverse=True))
    + r")\b"
)


_EVIDENCE_RULES = [
    (re.compile(r"episode_readiness\.can_launch\s*=\s*True", re.I),
     "The factory is cleared to start a run"),
    (re.compile(r"episode_readiness\.can_launch\s*=\s*False", re.I),
     "The factory is not cleared to start a run yet"),
    (re.compile(r"episode_topics\.queued\s*=\s*(\d+)", re.I),
     lambda m: f"{m.group(1)} topic{'s' if m.group(1) != '1' else ''} waiting in the queue"),
    (re.compile(r"episode_topics\.consumed\s*=\s*(\d+)", re.I),
     lambda m: f"{m.group(1)} topics already produced"),
    (re.compile(r"episode_topics\.total\s*=\s*0", re.I),
     "No topics have ever been queued"),
    (re.compile(r"episode_topics\.total\s*=\s*(\d+)", re.I),
     lambda m: f"{m.group(1)} topics in total"),
    (re.compile(r"episodes\.completed\s*=\s*(\d+)", re.I),
     lambda m: f"{m.group(1)} episodes finished production"),
    (re.compile(r"publishing_outcomes\.total\s*=\s*0", re.I),
     "Nothing has been published yet"),
    (re.compile(r"avg_cost_per_episode\s*=\s*\$?([\d.]+)", re.I),
     lambda m: f"Each episode costs about ${float(m.group(1)):.2f} on average"),
    (re.compile(r"total_cost_usd\s*=\s*\$?([\d.]+)", re.I),
     lambda m: f"${float(m.group(1)):.2f} spent so far"),
    (re.compile(r"threshold\s*=\s*\$?([\d.]+)", re.I),
     lambda m: f"the alert threshold is ${float(m.group(1)):.2f}"),
    (re.compile(r"real_snapshots\s*=\s*(\d+)", re.I),
     lambda m: f"{m.group(1)} real analytics readings collected"),
    (re.compile(r"mock_snapshots\s*=\s*(\d+)", re.I),
     lambda m: f"{m.group(1)} demo readings (not counted)"),
    (re.compile(r"minimum_for_trends\s*=\s*(\d+)", re.I),
     lambda m: f"{m.group(1)} are needed before trends are reliable"),
    (re.compile(r"blocked by QA Agent.*verdict=REJECTED", re.I | re.S),
     "The quality review stopped an episode before publishing"),
    (re.compile(r"^Run\s+[0-9a-f]{4,}[…\.]+\s*[—-]\s*episode\s+\S+\s*$", re.I),
     "A production run stopped early"),
]


def plain_evidence(text) -> str:
    """Turn a decision-engine 'evidence' line (a raw DB/attribute
    expression) into a sentence an operator can read. Falls back to
    plain_tech + light cleanup for anything unmatched."""
    if not text:
        return ""
    s = str(text).strip()
    for rx, repl in _EVIDENCE_RULES:
        m = rx.search(s)
        if m:
            return repl(m) if callable(repl) else repl
    # generic: "some.thing.here = value" -> "some thing here: value"
    m = re.match(r"^([a-z_][a-z0-9_.]*)\s*=\s*(.+)$", s, re.I)
    if m:
        key = m.group(1).split(".")[-1].replace("_", " ")
        return f"{key}: {m.group(2)}"
    return plain_tech(s)


def plain_tech(text) -> str:
    """Swap any known provider / model slug inside a free-text string for a
    name an operator recognises. Leaves everything else untouched."""
    if not text:
        return ""
    s = str(text)

    def _sub(m):
        tok = m.group(1)
        return _MODEL_NAMES.get(tok) or _PROVIDER_NAMES.get(tok.lower()) or tok

    return plain_ids(_TECH_TOKEN_RE.sub(_sub, s))


def signal_strength_words(value) -> dict:
    """avg_signal_strength (−1..1) → {label, level 0-3, direction}."""
    try:
        v = float(value)
    except Exception:  # noqa: BLE001 — a missing/undefined evidence field
        # (Jinja's Undefined raises its OWN UndefinedError from __float__,
        # never TypeError/ValueError -- confirmed live 2026-09-05: the
        # first real "content_volume" action ever generated,
        # hypogeum.learning_engine.generate_cadence_recommendation()'s
        # evidence_json, has never carried an avg_signal_strength key at
        # all, so /intelligence/learning 500'd the moment a real one
        # existed, having rendered an always-empty pending list until
        # then) must render as "no signal shown", not crash the page —
        # this filter's whole job is to be safe against exactly that.
        return {"label": "", "level": 0, "direction": "flat"}
    mag = abs(v)
    if mag >= 0.35:
        level, word = 3, "strong"
    elif mag >= 0.15:
        level, word = 2, "clear"
    elif mag >= 0.04:
        level, word = 1, "slight"
    else:
        level, word = 0, "faint"
    direction = "up" if v > 0 else ("down" if v < 0 else "flat")
    tail = "positive signal" if v > 0 else ("negative signal" if v < 0 else "no clear signal")
    return {"label": f"{word} {tail}", "level": level, "direction": direction}
