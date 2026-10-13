"""
hypogeum/revival_autopilot.py — the Revival Engine acting on its own, inside hard limits.

A channel's policy (table revival_policy) is one of:
    off       nothing runs
    observe   diagnoses and proposes only (what the dashboard showed before)
    auto      decides and executes without being asked:

      * forward-looking findings (what the audience asks for, reach that does not become followers) become learning
        directives the factory's own agents already read (hypogeum.channel_learning) — no human step;
      * a post that deserves a second chance gets a replacement cover generated from the post's LIVE title and
        description and checked by a model for contradiction (hypogeum.revival_executor); when a platform's API can
        set the cover, `_live_apply()` holds that function, otherwise the operator gets ONE alert with the finished
        cover (an Instagram/TikTok/Facebook cover can only be replaced from the app);
      * applied actions are measured after MEASURE_AFTER_DAYS against the channel's other posts (revival_actions).

Hard limits: MAX_COVER_ATTEMPTS generations per action, MAX_LIVE_CHANGES_PER_DAY live changes per channel, one cover
generation per channel per run (a generation takes minutes and the worker loop must stay responsive).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="revival_autopilot")

MODES = ("off", "observe", "auto")
MAX_COVER_ATTEMPTS = 2
MAX_LIVE_CHANGES_PER_DAY = 1
_COVER_KINDS = ("repackage_post", "new_thumbnail_title")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_mode(channel_id: str, *, db_path: Optional[Path] = None) -> str:
    from hypogeum.db import get_connection, initialize_schema
    initialize_schema(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT autonomy FROM revival_policy WHERE channel_id=?", (channel_id,)).fetchone()
    return row[0] if row else "off"


def set_mode(channel_id: str, mode: str, *, db_path: Optional[Path] = None) -> str:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    from hypogeum.db import get_connection, initialize_schema
    initialize_schema(db_path)
    with get_connection(db_path) as conn:
        conn.execute("INSERT INTO revival_policy (channel_id, autonomy, updated_at) VALUES (?,?,?) "
                     "ON CONFLICT(channel_id) DO UPDATE SET autonomy=excluded.autonomy, updated_at=excluded.updated_at",
                     (channel_id, mode, _now()))
        conn.commit()
    return mode


def _live_apply():
    """{platform: function(action_id, db_path=) -> {ok, reason}} for platforms whose API can set a cover. Looked up at
    call time so a platform appears here only when its apply function exists."""
    from hypogeum import revival_executor as ex
    fn = getattr(ex, "apply_youtube_cover", None)
    return {"youtube": fn} if fn else {}


def _settle(conn, action_id: str, note: dict) -> None:
    conn.execute("UPDATE revival_actions SET status='applied', applied_at=?, baseline_json=? WHERE action_id=?",
                 (_now(), json.dumps(note), action_id))
    conn.commit()


def _directive_for(a: dict) -> Optional[tuple]:
    """(dimension, text) the factory's own agents read, for a forward-looking action; None if the action is not one."""
    ev = a.get("evidence") or {}
    if a["kind"] == "audience_topic":
        return "topic", (f"A viewer asked for this: “{ev.get('text', '')}”. Prefer topics that answer requests "
                         "like it over topics nobody asked for.")
    if a["kind"] == "conversion_cta":
        return "description", (f"On {a['platform']} many people are reached but almost none follow. Close every caption "
                               "with one specific reason to follow (what they will get next), never a generic 'follow for more'.")
    return None


def _live_changes_today(conn, channel_id: str) -> int:
    today = _now()[:10]
    return conn.execute("SELECT COUNT(*) FROM revival_actions WHERE channel_id=? AND status IN ('applied','measured') "
                        "AND kind IN ('repackage_post','new_thumbnail_title') AND substr(applied_at,1,10)=?",
                        (channel_id, today)).fetchone()[0]


def _alert_hand(a: dict, db_path) -> None:
    """The one thing only a person can finish: replace a cover inside an app. One alert per action."""
    try:
        from hypogeum.operator_alerts import alert
        alert(f"revival:cover:{a['action_id']}",
              f"غلاف جاهز لبوست على {a['platform']}",
              f"الحلقة {a['target_ref']}: الغلاف الجديد متشال ومفحوص. {a['platform']} ما بتسمحش بتغيير الغلاف من الـ API — "
              "استبدله من التطبيق. الصورة في صفحة Revival.",
              db_path=db_path)
    except Exception as exc:  # noqa: BLE001 -- an alert must never break the loop
        log.warn("revival_alert_failed", action_id=a["action_id"], error=str(exc)[:120])


def run_channel(channel_id: str, *, db_path: Optional[Path] = None) -> dict:
    """One autopilot pass for a channel. Never raises. Returns what it did."""
    from hypogeum.db import get_connection
    from hypogeum import revival_executor as ex
    from hypogeum.revival_actions import decide, list_actions, propose_actions

    out = {"channel_id": channel_id, "mode": "off", "proposed": 0, "directives": 0, "covers": 0, "live": 0, "alerts": 0}
    try:
        out["mode"] = mode = get_mode(channel_id, db_path=db_path)
        if mode == "off":
            return out
        out["proposed"] = len(propose_actions(channel_id, db_path=db_path))
        if mode != "auto":
            return out

        for a in list_actions(channel_id, db_path=db_path):
            if a["status"] == "proposed":
                decide(a["action_id"], True, db_path=db_path)

        generated_one = False
        apply_fns = _live_apply()
        for a in list_actions(channel_id, db_path=db_path):
            if a["status"] != "approved":
                continue
            directive = _directive_for(a)
            if directive:
                from hypogeum.channel_learning import record_directive
                record_directive(channel_id, directive[0], "prefer", directive[1], db_path=db_path)
                with get_connection(db_path) as conn:
                    _settle(conn, a["action_id"], {"directive": directive[0]})
                out["directives"] += 1
            elif a["kind"] == "search_led_topic":
                # the search_demand directive is written by hypogeum.youtube_traffic from the same measurement
                with get_connection(db_path) as conn:
                    _settle(conn, a["action_id"], {"directive": "search_demand (youtube_traffic)"})
                out["directives"] += 1
            elif a["kind"] in _COVER_KINDS:
                draft = a.get("draft") or {}
                if draft.get("status") != "ready":
                    failed_before = int(draft.get("attempts", 0)) + (1 if draft.get("status") == "failed" else 0)
                    if generated_one or failed_before >= MAX_COVER_ATTEMPTS:
                        continue
                    if not ex.reserve_cover_draft(a["action_id"], db_path=db_path)["ok"]:
                        continue
                    ex.generate_cover_draft(a["action_id"], db_path=db_path)
                    generated_one = True
                    out["covers"] += 1
                    draft = next(x for x in list_actions(channel_id, db_path=db_path)
                                 if x["action_id"] == a["action_id"]).get("draft") or {}
                    if draft.get("status") == "failed":           # remember how many tries this action has used
                        with get_connection(db_path) as conn:
                            ex._set_draft(conn, a["action_id"], {**draft, "attempts": failed_before})
                if draft.get("status") != "ready":
                    continue
                fn = apply_fns.get(a["platform"])
                if fn:
                    with get_connection(db_path) as conn:
                        if _live_changes_today(conn, channel_id) >= MAX_LIVE_CHANGES_PER_DAY:
                            continue
                    if fn(a["action_id"], db_path=db_path).get("ok"):
                        out["live"] += 1
                else:
                    _alert_hand(a, db_path)
                    out["alerts"] += 1
    except Exception as exc:  # noqa: BLE001 -- the learning loop must never break the worker
        log.error("revival_autopilot_failed", channel_id=channel_id, error=str(exc)[:200])
        out["error"] = str(exc)[:200]
    return out
