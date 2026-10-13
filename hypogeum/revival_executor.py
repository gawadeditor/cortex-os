"""
hypogeum/revival_executor.py — carry out an APPROVED revival action, within hard limits.

Two executors, both opt-in per action (the operator presses a button for each) and neither publishes anything:

    generate_cover_draft(action_id)   a designed 9:16 cover draft for a post, saved locally for the operator to upload
                                      by hand (YouTube accepts a Short's cover only through Studio). Costs about
                                      DRAFT_COST_USD; at most DRAFTS_PER_DAY_CAP drafts a day factory-wide; one draft
                                      per action; refuses while MUAPI_DRY_RUN is on.
    queue_topic(action_id, text)      puts the operator-edited topic into the channel's normal backlog (source
                                      'revival', default priority — it waits its turn like any other topic). The
                                      scheduler alone decides when it is produced. At most REVIVAL_QUEUE_CAP revival
                                      topics may be open (queued/claimed) per channel.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="revival_executor")

REVIVAL_QUEUE_CAP = 2
DRAFT_COST_USD = 0.10            # one gpt-image-2 generation + one Sonnet call (measured by the 2026-09 vertical-cover backfill)
DRAFTS_PER_DAY_CAP = 5
_STALE_RUN_S = 15 * 60          # a 'running' draft this old died with its process; allow a retry
DRAFT_DIR = Path(__file__).resolve().parent.parent / ".hypogeum-assets" / "revival_drafts"
_COVER_KINDS = ("repackage_post", "new_thumbnail_title")
_TOPIC_KINDS = ("audience_topic", "search_led_topic")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dry_run() -> bool:
    from hypogeum.config import Config
    return bool(getattr(Config(), "MUAPI_DRY_RUN", False))


def _get(conn, action_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM revival_actions WHERE action_id=?", (action_id,)).fetchone()
    return dict(row) if row else None


def _set_draft(conn, action_id: str, draft: dict) -> None:
    conn.execute("UPDATE revival_actions SET draft_json=? WHERE action_id=?", (json.dumps(draft), action_id))
    conn.commit()


# ---------------------------------------------------------------------------
# topics
# ---------------------------------------------------------------------------

def queue_topic(action_id: str, topic_text: str, *, db_path: Optional[Path] = None) -> dict:
    from hypogeum.db import get_connection
    from hypogeum.topics import DuplicateTopicError, enqueue_topic

    topic_text = (topic_text or "").strip()
    with get_connection(db_path) as conn:
        a = _get(conn, action_id)
        if not a or a["status"] != "approved" or a["kind"] not in _TOPIC_KINDS:
            return {"ok": False, "reason": "this action is not an approved topic proposal"}
        if len(topic_text) < 8:
            return {"ok": False, "reason": "write the topic as a real title (at least a few words)"}
        open_n = conn.execute("SELECT COUNT(*) FROM episode_topics WHERE channel_id=? AND source='revival' "
                              "AND status IN ('queued','claimed')", (a["channel_id"],)).fetchone()[0]
        if open_n >= REVIVAL_QUEUE_CAP:
            return {"ok": False, "reason": f"{open_n} revival topics are already waiting for this channel "
                                           f"(limit {REVIVAL_QUEUE_CAP}) — let them be produced first"}
    try:
        topic_id = enqueue_topic(a["channel_id"], topic_text, source="revival", db_path=db_path)
    except DuplicateTopicError:
        return {"ok": False, "reason": "this topic is already in the queue"}
    with get_connection(db_path) as conn:
        conn.execute("UPDATE revival_actions SET status='applied', applied_at=?, baseline_json=? WHERE action_id=?",
                     (_now(), json.dumps({"topic_id": topic_id, "topic_text": topic_text}), action_id))
        conn.commit()
    log.info("revival_topic_queued", action_id=action_id, channel_id=a["channel_id"], topic_id=topic_id)
    return {"ok": True, "topic_id": topic_id}


# ---------------------------------------------------------------------------
# cover drafts
# ---------------------------------------------------------------------------

def _episode_of(a: dict) -> Optional[str]:
    ev = json.loads(a["evidence_json"]) if a.get("evidence_json") else {}
    return ev.get("episode_id") or (a["target_ref"] if str(a["target_ref"]).startswith("ep_") else None)


def reserve_cover_draft(action_id: str, *, db_path: Optional[Path] = None) -> dict:
    """Check every limit and mark the draft 'running'. Returns {ok, reason, episode_id}."""
    from hypogeum.db import get_connection

    if _dry_run():
        return {"ok": False, "reason": "MUAPI_DRY_RUN is on — a real image would not be generated"}
    with get_connection(db_path) as conn:
        a = _get(conn, action_id)
        if not a or a["status"] != "approved" or a["kind"] not in _COVER_KINDS:
            return {"ok": False, "reason": "this action is not an approved cover proposal"}
        draft = json.loads(a["draft_json"]) if a.get("draft_json") else None
        stale = bool(draft and draft.get("status") == "running"
                     and (datetime.now(timezone.utc) - datetime.fromisoformat(draft["started_at"])).total_seconds() > _STALE_RUN_S)
        if draft and draft.get("status") in ("running", "ready") and not stale:
            return {"ok": False, "reason": f"a draft is already {draft['status']}"}
        today = _now()[:10]
        used = sum(1 for r in conn.execute("SELECT draft_json FROM revival_actions WHERE draft_json IS NOT NULL")
                   if (json.loads(r[0]).get("started_at") or "")[:10] == today)
        if used >= DRAFTS_PER_DAY_CAP:
            return {"ok": False, "reason": f"daily limit of {DRAFTS_PER_DAY_CAP} cover drafts reached"}
        ep = _episode_of(a)
        if not ep:
            return {"ok": False, "reason": "cannot tell which episode this cover is for"}
        _set_draft(conn, action_id, {"status": "running", "started_at": _now(), "cost_usd": DRAFT_COST_USD})
    return {"ok": True, "episode_id": ep}


_JUDGE_PROMPT = Path(__file__).resolve().parent / "prompts" / "revival_cover_check.txt"


def judge_cover(hook_text: str, title: str, description: str, ep: str, config, work: Path) -> dict:
    """Model check that the cover text is faithful to the video it will sit on. Fails CLOSED: any error = not approved."""
    try:
        from hypogeum.agent import run_agent
        from hypogeum.prompt_loader import load_prompt
        out = run_agent(
            agent_name="revival_cover_check",
            model_slug=getattr(config, "OPENROUTER_SONNET_MODEL_ID", "anthropic/claude-sonnet-4.6") or "anthropic/claude-sonnet-4.6",
            system_prompt=load_prompt(_JUDGE_PROMPT),
            input_package={"cover_text": hook_text, "video_title": title, "video_description": description[:900]},
            output_schema="revival_cover_check", episode_id=ep, max_tokens=300, config=config, output_dir=work, channel_id=None)
        return {"verdict": out["verdict"], "reason": out["reason"]}
    except Exception as exc:  # noqa: BLE001
        return {"verdict": "fail", "reason": f"consistency check unavailable: {str(exc)[:100]}"}


def _hook_text_from(work: Path) -> Optional[str]:
    for p in sorted(work.glob("**/*vertical_thumbnail_plan*.json")):
        try:
            return json.loads(p.read_text())["hook_text"]
        except Exception:  # noqa: BLE001
            continue
    return None


def _published_copy(a: dict, ep: str, db_path) -> Optional[tuple]:
    """(title, description) of the post as it is LIVE on YouTube, so the cover is grounded in what the video actually says
    (the episode row holds only a working title; a cover written from it alone once contradicted the video's thesis)."""
    if a.get("platform") != "youtube":
        return None
    try:
        import requests
        from hypogeum.db import get_connection
        from hypogeum.publishing.youtube_privacy import _access_token
        with get_connection(db_path) as conn:
            row = conn.execute("SELECT platform_video_id FROM platform_analytics WHERE episode_id=? AND platform='youtube' "
                               "AND platform_video_id IS NOT NULL LIMIT 1", (ep,)).fetchone()
        if not row:
            return None
        r = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=30,
                         headers={"Authorization": f"Bearer {_access_token(a['channel_id'], db_path)}"},
                         params={"part": "snippet", "id": row[0]})
        sn = r.json()["items"][0]["snippet"]
        return sn["title"], sn.get("description", "")[:900]
    except Exception as exc:  # noqa: BLE001 -- fall back to the episode row
        log.warn("revival_published_copy_unavailable", episode_id=ep, error=str(exc)[:120])
        return None


def generate_cover_draft(action_id: str, *, db_path: Optional[Path] = None) -> dict:
    """Generate the draft (blocking, ~1 minute). Call reserve_cover_draft first. Never raises."""
    from hypogeum.agents.vertical_thumbnail import build_vertical_thumbnail_package, synthesize_concept_brief
    from hypogeum.config import Config
    from hypogeum.db import get_connection

    started = _now()
    reason = "unknown error"
    try:
        with get_connection(db_path) as conn:
            a = _get(conn, action_id)
            ep = _episode_of(a)
            row = conn.execute("SELECT title, topic FROM episodes WHERE episode_id=?", (ep,)).fetchone()
        title, topic = (row["title"] or "", row["topic"] or "") if row else ("", "")
        live = _published_copy(a, ep, db_path)
        if live:
            title, topic = live[0], (live[1] or topic)
        config = Config()
        with tempfile.TemporaryDirectory(prefix=f"revival_{action_id}_") as td:
            work = Path(td)
            if not synthesize_concept_brief(ep, a["channel_id"], title, topic, work, config):
                reason = "could not build a thumbnail concept for this episode"
            else:
                pkg = build_vertical_thumbnail_package(episode_id=ep, output_dir=work, config=config,
                                                       channel_id=a["channel_id"], authorize_via_channel=False)
                if not pkg.get("generation_metadata", {}).get("vertical_cover"):
                    reason = "the cover generator fell back to its stub (generation or quality check failed)"
                else:
                    asset = Path(pkg["main_thumbnail"]["asset_id"]).name
                    found = next((p for suf in (".png", ".jpg", ".jpeg", ".webp")
                                  for p in (work / f"{asset}{suf}", work / "thumbnails" / f"{asset}{suf}") if p.exists()), None)
                    hook = _hook_text_from(work)
                    verdict = judge_cover(hook or "", title, topic, ep, config, work) if hook else \
                        {"verdict": "fail", "reason": "could not read the cover text to check it"}
                    if found is None:
                        reason = "the generated cover file was not found"
                    elif verdict["verdict"] != "pass":
                        reason = f"rejected by the consistency check ({hook!r}): {verdict['reason']}"
                    else:
                        DRAFT_DIR.mkdir(parents=True, exist_ok=True)
                        dest = DRAFT_DIR / f"{action_id}{found.suffix.lower()}"
                        shutil.copyfile(found, dest)
                        with get_connection(db_path) as conn:
                            _set_draft(conn, action_id, {"status": "ready", "path": str(dest), "started_at": started,
                                                         "finished_at": _now(), "cost_usd": DRAFT_COST_USD,
                                                         "hook_text": hook, "check": verdict["reason"]})
                        return {"ok": True, "path": str(dest)}
    except Exception as exc:  # noqa: BLE001 -- a failed draft must never break the dashboard or the worker
        reason = str(exc)[:200]
        log.error("revival_cover_draft_failed", action_id=action_id, error=reason)
    with get_connection(db_path) as conn:
        _set_draft(conn, action_id, {"status": "failed", "reason": reason, "started_at": started, "finished_at": _now()})
    return {"ok": False, "reason": reason}


def apply_youtube_cover(action_id: str, *, db_path: Optional[Path] = None) -> dict:
    """Set the checked, ready draft as the live cover through the YouTube API (verified 2026-10-13: it changes a Short's
    served image). What viewers saw before is saved next to the draft. Marks the action applied (baseline captured now)."""
    import io
    import requests
    from hypogeum.db import get_connection
    from hypogeum.publishing.youtube_privacy import _access_token
    from hypogeum.revival_actions import mark_applied

    with get_connection(db_path) as conn:
        a = _get(conn, action_id)
        if not a or a["status"] != "approved" or a["platform"] != "youtube" or a["kind"] not in _COVER_KINDS:
            return {"ok": False, "reason": "not an approved YouTube cover action"}
        draft = json.loads(a["draft_json"]) if a.get("draft_json") else {}
        if draft.get("status") != "ready" or not draft.get("check"):
            return {"ok": False, "reason": "no consistency-checked draft is ready"}
        ep = _episode_of(a)
        row = conn.execute("SELECT platform_video_id FROM platform_analytics WHERE episode_id=? AND platform='youtube' "
                           "AND platform_video_id IS NOT NULL LIMIT 1", (ep,)).fetchone()
    if not row:
        return {"ok": False, "reason": "cannot find the YouTube video for this episode"}
    vid = row[0]
    try:
        token = _access_token(a["channel_id"], db_path)
        data = Path(draft["path"]).read_bytes()
        if len(data) > 2_000_000:                      # YouTube's thumbnail size limit
            from PIL import Image
            buf = io.BytesIO()
            Image.open(io.BytesIO(data)).convert("RGB").save(buf, "JPEG", quality=85)
            data = buf.getvalue()
        try:                                           # keep what viewers saw before, for the record
            (DRAFT_DIR / f"{action_id}.before.jpg").write_bytes(
                requests.get(f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg", timeout=30).content)
        except Exception:  # noqa: BLE001
            pass
        r = requests.post("https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
                          params={"videoId": vid, "uploadType": "media"},
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"}, data=data, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)[:160]}
    if r.status_code != 200:
        return {"ok": False, "reason": f"YouTube refused the cover ({r.status_code}): {r.text[:140]}"}
    mark_applied(action_id, db_path=db_path)
    log.info("revival_youtube_cover_applied", action_id=action_id, video_id=vid)
    return {"ok": True, "video_id": vid}
