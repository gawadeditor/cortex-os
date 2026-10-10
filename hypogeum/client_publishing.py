"""
hypogeum/client_publishing.py — publishes the operator-approved posts of a managed client (Instagram, via Postiz).

Rules that make it safe on a real, public account:
  * only a post the operator APPROVED is ever considered, and only for a client in engagement_mode 'manage'
    (an explicit switch; every client starts in 'observe' and nothing here touches it);
  * a post is sent AT MOST ONCE: its outcome is written next to the post (post["auto"]["instagram"]) and a send
    that may have gone through ("ambiguous") is never repeated -- it is reported to the operator instead;
  * a caption that still holds a [placeholder], is empty or too long, or a post without its material is BLOCKED
    with a plain reason (reported on Telegram), never sent half-finished.
LinkedIn is not automated yet: a post for Instagram + LinkedIn stays 'approved' until the operator marks it posted.

    publish_due(db_path, now, provider)  -> [{client_id, date, state, ...}]   schedule approved posts that are due
    reconcile(db_path, now, provider)    -> [...]                              scheduled -> published / failed
    issues(post)                         -> [str]                              why a post cannot be sent yet
    start_client_publishing_loop(db_path)
"""
from __future__ import annotations

import re
import threading
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="client_publishing")

AUTOMATED = ("instagram",)
CAPTION_MAX = 2200
LEAD_HOURS = 12
DEFAULT_TIME = "17:00"
PLACEHOLDER = re.compile(r"\[[^\]\n]{1,80}\]")
_SENT = ("scheduled", "published", "ambiguous", "failed")     # states that are never sent again automatically


def _p(db_path):
    return Path(db_path) if db_path else None


def issues(post: dict) -> list:
    from hypogeum.client_media import media_issue  # noqa: PLC0415
    out = []
    caption = (post.get("draft") or "").strip()
    if not caption:
        out.append("the caption is empty")
    if len(caption) > CAPTION_MAX:
        out.append(f"the caption is {len(caption)} characters (Instagram allows {CAPTION_MAX})")
    if PLACEHOLDER.search(caption):
        out.append("the caption still has a [placeholder] to fill in: " + PLACEHOLDER.search(caption).group(0))
    m = media_issue(post.get("media"))
    if m:
        out.append(m + (f" (needs: {post['needs']})" if post.get("needs") and str(post["needs"]).lower() != "none" else ""))
    return out


def post_time(client: dict, post: dict) -> datetime:
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    hhmm = str(post.get("time") or DEFAULT_TIME)
    h, m = (int(x) for x in hhmm.split(":")[:2])
    tz = ZoneInfo(client.get("timezone") or "UTC")
    d = _date.fromisoformat(post["date"])
    return datetime(d.year, d.month, d.day, h, m, tzinfo=tz).astimezone(timezone.utc)


def _handle(db_path, client_id: str) -> Optional[str]:
    from hypogeum.db import get_connection  # noqa: PLC0415
    conn = get_connection(_p(db_path))
    try:
        r = conn.execute("SELECT handle FROM managed_accounts WHERE client_id=? AND platform='instagram' AND enabled=1 LIMIT 1",
                         (client_id,)).fetchone()
    finally:
        conn.close()
    return r["handle"] if r else None


def _integration(provider, handle: str, db_path) -> Optional[dict]:
    for i in provider.list_integrations(db_path=_p(db_path)) or []:
        if str(i.get("identifier") or "").startswith("instagram") and not i.get("disabled") \
                and str(i.get("profile") or "").lower().lstrip("@") == handle.lower().lstrip("@"):
            return i
    return None


def _managed(db_path) -> list:
    from hypogeum import client_manager as cm  # noqa: PLC0415
    return [c for c in cm.list_clients(db_path=_p(db_path))
            if (c.get("status") or "active") == "active" and c.get("engagement_mode") == "manage"]


def _store(db_path, client_id: str, post_date: str, mutate) -> bool:
    from hypogeum import client_manager as cm  # noqa: PLC0415
    from hypogeum.dashboard_clients import calendars_of  # noqa: PLC0415
    client = cm.get_client(client_id, db_path=_p(db_path))
    brief = dict(client.get("brief") or {})
    cals = calendars_of(brief)
    for cal in cals.values():
        for p in cal.get("posts") or []:
            if p.get("date") == post_date:
                mutate(p)
                brief["content_calendars"] = cals
                brief.pop("content_calendar", None)
                return bool(cm.update_client(client_id, brief=brief, db_path=_p(db_path)))
    return False


def _set_auto(post: dict, platform: str, **fields) -> None:
    auto = dict(post.get("auto") or {})
    cur = dict(auto.get(platform) or {})
    cur.update(fields)
    cur["updated_at"] = datetime.now(timezone.utc).isoformat()
    auto[platform] = cur
    post["auto"] = auto


def publish_due(db_path=None, now: Optional[datetime] = None, provider=None, lead_hours: int = LEAD_HOURS) -> list:
    from hypogeum.publishing import postiz as pz  # noqa: PLC0415
    now = now or datetime.now(timezone.utc)
    results = []
    for client in _managed(db_path):
        cid = client["client_id"]
        from hypogeum.dashboard_clients import calendars_of  # noqa: PLC0415
        posts = [p for cal in calendars_of(client.get("brief") or {}).values() for p in cal.get("posts") or []]
        for post in posts:
            auto = (post.get("auto") or {}).get("instagram") or {}
            if (post.get("status") or "draft") != "approved" or "instagram" not in (post.get("platforms") or []) \
                    or auto.get("state") in _SENT or not post.get("date"):
                continue
            when = post_time(client, post)
            if when - now > timedelta(hours=lead_hours) or post["date"] < now.date().isoformat():
                continue                                    # not due yet / its day is gone (expire_past skips it)
            when = max(when, now + timedelta(minutes=10))
            problems = issues(post)
            handle = _handle(db_path, cid)
            provider = provider or pz.PostizProvider()
            integration = None
            if not problems:
                if not handle:
                    problems = ["the client has no Instagram account linked"]
                else:
                    try:
                        integration = _integration(provider, handle, db_path)
                    except Exception as exc:  # noqa: BLE001
                        problems = [f"could not reach Postiz: {str(exc)[:80]}"]
                    else:
                        if integration is None:
                            problems = [f"@{handle} is not connected in Postiz (add it as an Instagram channel)"]
            if problems:
                _store(db_path, cid, post["date"], lambda p, pr=problems: _set_auto(p, "instagram", state="blocked", issues=pr))
                results.append({"client_id": cid, "date": post["date"], "state": "blocked", "issues": problems})
                continue
            media = post["media"]
            try:
                up = provider.upload_local_media(Path(media["path"]), mime_type=media.get("mime") or "application/octet-stream",
                                                 db_path=_p(db_path))
                sent = provider.publish_client_post(integration_id=integration["id"], platform="instagram", content=post["draft"].strip(),
                                                    media=[up], scheduled_for=when, identifier=integration.get("identifier"),
                                                    db_path=_p(db_path))
                state, extra = "scheduled", {"post_id": sent["post_id"], "scheduled_for": when.isoformat(), "integration_id": integration["id"], "issues": []}
            except pz.PostizAmbiguousClientPostError as exc:
                state, extra = "ambiguous", {"error": str(exc)[:200], "scheduled_for": when.isoformat()}
            except pz.PostizPublishRejectedError as exc:
                state, extra = "failed", {"error": str(exc)[:200]}
            except pz.PostizMediaError as exc:                # nothing was posted: stay blocked, try again next round
                state, extra = "blocked", {"issues": [f"the upload to Postiz failed: {str(exc)[:80]}"]}
            except Exception as exc:  # noqa: BLE001
                state, extra = "blocked", {"issues": [f"unexpected error: {type(exc).__name__}"]}
            _store(db_path, cid, post["date"], lambda p, s=state, e=extra: _set_auto(p, "instagram", state=s, **e))
            log.info("client_post_" + state, client_id=cid, date=post["date"])
            results.append({"client_id": cid, "date": post["date"], "state": state, **extra})
    return results


def reconcile(db_path=None, now: Optional[datetime] = None, provider=None) -> list:
    from hypogeum.publishing import postiz as pz  # noqa: PLC0415
    now = now or datetime.now(timezone.utc)
    results = []
    for client in _managed(db_path):
        cid = client["client_id"]
        from hypogeum.dashboard_clients import calendars_of  # noqa: PLC0415
        for cal in calendars_of(client.get("brief") or {}).values():
            for post in cal.get("posts") or []:
                auto = (post.get("auto") or {}).get("instagram") or {}
                if auto.get("state") != "scheduled" or not auto.get("post_id"):
                    continue
                provider = provider or pz.PostizProvider()
                try:
                    when = datetime.fromisoformat(auto["scheduled_for"])
                    info = provider.get_post(auto["post_id"], around=when, db_path=_p(db_path))
                except Exception:  # noqa: BLE001
                    continue                                    # Postiz unreachable: look again next round
                state = (info or {}).get("state")
                if state == "PUBLISHED":
                    only_ig = set(post.get("platforms") or []) <= set(AUTOMATED)

                    def done(p, url=info.get("release_url"), only_ig=only_ig):
                        _set_auto(p, "instagram", state="published", release_url=url)
                        if only_ig:
                            p["status"] = "published"
                            p["published_marked_at"] = datetime.now(timezone.utc).isoformat()
                    _store(db_path, cid, post["date"], done)
                    results.append({"client_id": cid, "date": post["date"], "state": "published", "release_url": info.get("release_url")})
                elif state == "ERROR" or (info is None and now > when + timedelta(hours=2)):
                    msg = "Postiz marked the post as failed" if state == "ERROR" else "the post is no longer in Postiz"
                    _store(db_path, cid, post["date"], lambda p, m=msg: _set_auto(p, "instagram", state="failed", error=m))
                    results.append({"client_id": cid, "date": post["date"], "state": "failed", "error": msg})
    return results


def start_client_publishing_loop(db_path=None, interval_s: int = 600) -> Optional[threading.Thread]:
    stop = threading.Event()

    def _loop():
        stop.wait(60)
        while not stop.is_set():
            try:
                if _managed(db_path):                      # nothing to do (and no Postiz call) until a client is switched to 'manage'
                    from hypogeum.publishing import postiz as pz  # noqa: PLC0415
                    prov = pz.PostizProvider()
                    publish_due(db_path, provider=prov)
                    for r in reconcile(db_path, provider=prov):
                        if r["state"] == "published":
                            try:
                                from hypogeum.agents import brand_strategy_agent as bsa  # noqa: PLC0415
                                bsa.draft_next_posts(r["client_id"], db_path=_p(db_path))
                            except Exception:  # noqa: BLE001 — drafting is budget-gated and optional
                                pass
            except Exception as exc:  # noqa: BLE001
                log.warn("client_publishing_loop_failed", error=str(exc)[:200])
            stop.wait(interval_s)

    t = threading.Thread(target=_loop, name="client-publishing", daemon=True)
    t.stop_event = stop  # type: ignore[attr-defined]
    t.start()
    log.info("client_publishing_loop_started")
    return t
