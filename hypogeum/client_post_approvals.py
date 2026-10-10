"""
hypogeum/client_post_approvals.py — the operator approves (or skips) a client's drafted posts.

The personal-branding client's calendar (hypogeum/agents/brand_strategy_agent.py) holds one drafted post per
date. A draft goes  draft -> approved -> published  (or skipped). Nothing is ever published without an
explicit approval -- the operator's standing rule. Approval comes from Telegram (buttons on the alert) or from
the plan page (/client/<id>/plan); both end up here.

    due_drafts(db_path, today)              -> [{client_id, client_name, post}]  drafts due in the next WINDOW_DAYS
    expire_past(db_path, today)             -> number of stale drafts marked skipped
    set_status(client_id, date, status, db_path) -> bool                          'approved' | 'skipped'
    alert_text(item)                        -> (title, body)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

WINDOW_DAYS = 7
MAX_BODY = 1800
_FINAL = ("published", "skipped")


def _p(db_path):
    return Path(db_path) if db_path else None


def _clients(db_path):
    from hypogeum import client_manager as cm  # noqa: PLC0415
    return [c for c in cm.list_clients(db_path=_p(db_path)) if (c.get("status") or "active") == "active"]


def due_drafts(db_path=None, today: Optional[date] = None) -> list:
    """Drafts that still wait for a decision and fall within the next WINDOW_DAYS (today included)."""
    from hypogeum.dashboard_clients import calendars_of  # noqa: PLC0415
    today = today or datetime.now(timezone.utc).date()
    last = (today + timedelta(days=WINDOW_DAYS)).isoformat()
    out = []
    for c in _clients(db_path):
        brief = c.get("brief") or {}
        if not brief.get("brand_strategy"):
            continue
        for cal in calendars_of(brief).values():
            for p in cal.get("posts") or []:
                if (p.get("status") or "draft") == "draft" and today.isoformat() <= p.get("date", "") <= last and (p.get("draft") or "").strip():
                    out.append({"client_id": c["client_id"], "client_name": c.get("name") or c["client_id"], "post": p})
    return sorted(out, key=lambda x: x["post"]["date"])


def expire_past(db_path=None, today: Optional[date] = None) -> int:
    """A draft whose date has passed without a decision can no longer be published on its day: mark it skipped
    (kept, with the reason), so it stops counting as pending and stops asking for attention."""
    from hypogeum import client_manager as cm  # noqa: PLC0415
    from hypogeum.dashboard_clients import calendars_of  # noqa: PLC0415
    today = today or datetime.now(timezone.utc).date()
    n = 0
    for c in _clients(db_path):
        brief = dict(c.get("brief") or {})
        cals = calendars_of(brief)
        changed = 0
        for cal in cals.values():
            for p in cal.get("posts") or []:
                if (p.get("status") or "draft") in ("draft", "approved") and p.get("date", "9999") < today.isoformat():
                    p["status"] = "skipped"
                    p["skipped_reason"] = "its date passed without being posted"
                    p["skipped_at"] = datetime.now(timezone.utc).isoformat()
                    changed += 1
        if changed:
            brief["content_calendars"] = cals
            brief.pop("content_calendar", None)
            cm.update_client(c["client_id"], brief=brief, db_path=_p(db_path))
            n += changed
    return n


def set_status(client_id: str, post_date: str, status: str, db_path=None) -> bool:
    """Approve or skip one draft. Only a draft/approved post can change; published/skipped are final."""
    if status not in ("approved", "skipped"):
        raise ValueError("status must be 'approved' or 'skipped'")
    from hypogeum import client_manager as cm  # noqa: PLC0415
    from hypogeum.dashboard_clients import calendars_of  # noqa: PLC0415
    client = cm.get_client(client_id, db_path=_p(db_path))
    if not client:
        return False
    brief = dict(client.get("brief") or {})
    cals = calendars_of(brief)
    for cal in cals.values():
        for p in cal.get("posts") or []:
            if p.get("date") == post_date:
                if (p.get("status") or "draft") in _FINAL:
                    return False
                p["status"] = status
                p[f"{status}_at"] = datetime.now(timezone.utc).isoformat()
                brief["content_calendars"] = cals
                brief.pop("content_calendar", None)
                return bool(cm.update_client(client_id, brief=brief, db_path=_p(db_path)))
    return False


def alert_text(item: dict) -> tuple:
    p = item["post"]
    plats = " + ".join(x.capitalize() for x in (p.get("platforms") or [])) or "-"
    title = f"مسودة بوست محتاجة موافقتك: {plats} · {p['date']}"
    draft = (p.get("draft") or "").strip()
    if len(draft) > MAX_BODY:
        draft = draft[:MAX_BODY].rstrip() + "…"
    needs = f"\n\nمحتاج مادة منك: {p['needs']}" if p.get("needs") and str(p["needs"]).lower() not in ("none", "") else ""
    body = (f"{item['client_name']} · {p.get('pillar') or ''} · {p.get('format') or ''}\n\n{draft}{needs}\n\n"
            f"التعديل من الداشبورد: /client/{item['client_id']}/plan")
    return title, body
